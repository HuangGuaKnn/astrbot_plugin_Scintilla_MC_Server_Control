"""LLM 工具「调用侧」护栏（v0.21.0）。

解决两个真实翻车现场（2026-09-12 QQ 群里发金剑那次）：

1. **参数误封装**——LLM 有时把参数多套一层 ``{"arguments": {...}}``，
   而 AstrBot 是 ``handler(event, **tool_args)`` 直接展开调用的，于是抛出
   ``TypeError: got an unexpected keyword argument 'arguments'``。
   工具根本没跑起来、权限闸门压根没触达，但 LLM 只看到一句 ``error: ...``，
   就判断成「用法不对」→ 反复重试、越套越深。
   本模块的 :func:`tolerant_tool` 会先剥壳再调用，让调用真正落到闸门上；
   壳里确实没有可用参数时，返回明确标注「参数问题、与权限无关」的提示。

2. **拦截必须终局**——权限闸门拒绝时若只回一句平铺直叙的说明，LLM 容易理解成
   「换个参数 / 换个工具就能过」→ 继续硬试。
   :func:`deny_result` 输出「终局化」文案（明确声明：不是参数问题、重试与换工具都无效、
   立即停止调用并如实告知用户）；:class:`DenyLatch` 则做会话级闩锁：
   同一轮对话里一旦被拦，后续任何命令类工具调用直接短路，不再触达闸门。
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from typing import Any, Callable

#: 权限拦截的标记头（终局结果；LLM 被明确要求见到它就必须停手）
MARK_DENY = "【权限拦截 · 终局结果】"
#: 会话闩锁命中的标记头（此前已被拦，本次调用直接被短路）
MARK_LATCH = "【权限拦截 · 会话闩锁】"
#: 工具参数问题的标记头（与权限无关，属于调用格式错误）
MARK_PARAM = "【工具参数错误 · 非权限问题】"

#: 权限前置提醒的触发时机（v0.23.3）：always / on_demand / off
HINT_MODES: tuple[str, ...] = ("always", "on_demand", "off")

#: 「近期调用过 MC 工具」的记忆窗口（秒）—— 按需注入的第一路信号
HINT_RECALL_TTL = 600.0

#: 「按需注入」的话题关键词（v0.23.3）—— 第二路信号。
#:
#: 只收「基本只可能出现在 MC 语境」的词：宁可少注入一次，也别吵到日常闲聊。
#: 真正的兜底始终是闸门 —— 提示注入只是「省一次往返」的优化，漏了也无害。
HINT_TOPIC_KEYWORDS: tuple[str, ...] = (
    "mc", "minecraft", "我的世界", "麦块", "tacz", "rcon", "nbt",
    "服务器", "指令", "发放", "给我发", "发一把", "发把", "发个",
    "物品id", "词典", "模组", "整合包", "配方", "合成",
    "附魔", "满配",
    "广播", "喊话", "踢人", "封禁", "白名单", "在线玩家", "任务链",
)

#: 命令类工具（白名单策略下非管理员一律不可用；闩锁对它们生效）
COMMAND_TOOLS: tuple[str, ...] = (
    "mc_execute_command",
    "mc_give_item",
    "mc_broadcast",
    "mc_workflow",
)

#: 用户可用的替代方式（插件自带功能，不受命令工具策略限制）
ALTERNATIVES = "mcs 喊话 / mcs 状态 / mcs 查询 / mcs 绑定 / mcs 帮助"

#: 给 LLM 的硬性指令（必须足够强硬，否则模型会继续尝试）
_STOP_RULES = (
    "给 AI 的硬性指令（必须遵守）：\n"
    "1) 这是**权限判定**，不是参数错误、不是用法错误、不是工具选择错误。"
    "改写参数重试、换一个命令类工具再试、多试几次，结论都不会变。\n"
    f"2) 立刻停止调用任何命令类工具（{' / '.join(COMMAND_TOOLS)} 等），不要再发起同类调用。\n"
    "3) 直接用一两句话（可带你的角色语气）告诉用户：本次操作已被权限拦截 + 简要原因 + "
    "可用的替代方式。不要复述这段内部说明，更不要声称操作已完成或已发放。"
)


def unwrap_nested_arguments(kwargs: dict | None, max_depth: int = 4) -> dict:
    """剥掉 LLM 常见的「多套一层 arguments」参数外壳。

    支持 ``{"arguments": {...}}``、``{"arguments": "{...}"}``（JSON 字符串）
    以及多层嵌套（实测出现过 4 层）。剥不动就原样返回。
    """
    cur: Any = dict(kwargs or {})
    for _ in range(max(0, max_depth)):
        if not isinstance(cur, dict) or list(cur.keys()) != ["arguments"]:
            break
        inner = cur["arguments"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                break
        if not isinstance(inner, dict):
            break
        cur = dict(inner)
    return cur if isinstance(cur, dict) else {}


def param_notice(tool: str, accepted: list[str], received: list[str]) -> str:
    """参数问题提示（明确标注与权限无关，避免 LLM 归错因）。"""
    return (
        f"{MARK_PARAM}\n"
        f"工具 {tool} 只接受参数：{', '.join(accepted) or '（无）'}；"
        f"本次收到：{', '.join(received) or '（无）'}。\n"
        "这是调用格式问题，与权限无关。请按参数名平铺传入"
        '（不要把参数再包进 {"arguments": {...}} 里），然后重新调用一次。'
    )


def deny_result(
    reason: str,
    *,
    tool: str = "",
    command: str = "",
    sender: str = "",
    repeat: int = 1,
) -> str:
    """权限闸门拒绝的「终局」返回文案。"""
    lines = [MARK_DENY]
    if tool:
        lines.append(f"· 被拦工具：{tool}")
    if command:
        lines.append(f"· 被拦命令：{command}")
    if sender:
        lines.append(f"· 请求者：{sender}（不在管理员列表 admin_ids 中）")
    lines.append(f"· 原因：{reason}")
    lines.append("· 执行结果：未执行任何操作（不存在「换个参数就能过」的组合）。")
    if repeat > 1:
        lines.append(f"· 注意：本轮对话中同类调用已被拦截 {repeat} 次，继续尝试不会改变结果。")
    lines.append("")
    lines.append(_STOP_RULES)
    lines.append("")
    lines.append(
        f"用户可用的替代方式：{ALTERNATIVES}（插件自带功能，不受本策略限制）；"
        "或请管理员把 TA 的账号加入 admin_ids / 把策略切换为 blacklist。"
    )
    return "\n".join(lines)


def latch_result(reason: str, *, tool: str = "", hits: int = 1) -> str:
    """会话闩锁命中时的返回文案（不执行、不再重复解释）。"""
    return (
        f"{MARK_LATCH}\n"
        f"· 本次调用：{tool or '（命令类工具）'} —— 已被直接短路，未执行、未触达服务器。\n"
        f"· 原因：本会话此前已被权限闸门拦下（{reason}），闩锁有效期内的同类调用一律短路。\n"
        f"· 这是第 {hits} 次短路。\n"
        "\n"
        "给 AI 的硬性指令：立刻停止调用任何命令类工具，直接回复用户"
        "（说明被权限拦截、给出替代方式），不要再解释内部细节。"
    )


class DenyLatch:
    """会话级「拦截闩锁」：一次对话内被拦过，后续命令类调用直接短路。

    key 通常是 ``unified_msg_origin + 请求者 ID``，TTL 默认 300 秒
    （既覆盖同一次任务的多轮硬试，又不会把用户长时间关在门外）。
    """

    def __init__(self, ttl: float = 300.0, max_keys: int = 512) -> None:
        self.ttl = float(ttl)
        self.max_keys = int(max_keys)
        self._items: dict[str, tuple[float, str, int]] = {}

    # ---- 内部 ----
    def _prune(self, now: float) -> None:
        dead = [k for k, (exp, _, _) in self._items.items() if exp <= now]
        for k in dead:
            self._items.pop(k, None)
        if len(self._items) > self.max_keys:
            for k, _ in sorted(self._items.items(), key=lambda kv: kv[1][0])[
                : len(self._items) - self.max_keys
            ]:
                self._items.pop(k, None)

    # ---- 对外 ----
    def record(self, key: str, reason: str) -> int:
        """登记一次拦截；返回该 key 本轮累计被拦次数。"""
        if not key:
            return 1
        now = time.time()
        self._prune(now)
        hits = self._items.get(key, (0.0, reason, 0))[2] + 1
        self._items[key] = (now + self.ttl, reason, hits)
        return hits

    def peek(self, key: str) -> tuple[str, int] | None:
        """命中闩锁时返回 (原因, 短路次数)，否则 None。短路次数会自增。"""
        if not key:
            return None
        now = time.time()
        self._prune(now)
        item = self._items.get(key)
        if item is None:
            return None
        exp, reason, hits = item
        if exp <= now:
            self._items.pop(key, None)
            return None
        hits += 1
        self._items[key] = (exp, reason, hits)
        return reason, hits

    def clear(self, key: str) -> None:
        self._items.pop(key or "", None)

    def count(self, key: str) -> int:
        item = self._items.get(key or "")
        return item[2] if item else 0


def tolerant_tool(fn: Callable) -> Callable:
    """让 LLM 工具函数容忍「参数误封装 / 多余参数」。

    * 先剥 ``{"arguments": {...}}`` 外壳（可多层）；
    * 只把函数真正接受的参数传进去，多余的参数丢弃并记日志，
      从根上避免 ``TypeError: unexpected keyword argument``；
    * 若剥完壳之后一个可用参数都没有（说明壳里不是本工具的参数），
      返回 :func:`param_notice`，让 LLM 明确知道「这是参数问题、不是权限问题」。
    """

    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # pragma: no cover - 理论不可达
        params = []
    accepted = [p for p in params if p not in ("self", "event")]
    tool_name = getattr(fn, "__name__", "tool")

    @functools.wraps(fn)
    async def wrapper(self, event, *args, **kwargs):
        # v0.23.3：只要 LLM 确实「试图调用」MC 工具就先记一笔 —— 供按需提示注入
        # 判定用（本会话近期调用过 MC 工具 → 后续请求补上前置提醒）。
        # 放在最前面：无论后面参数剥壳顺不顺利，这次「试图调用」都算数。
        if tool_name.startswith("mc"):
            _mark = getattr(self, "_mark_mc_tool_used", None)
            if callable(_mark):
                try:
                    _mark(event)
                except Exception:
                    pass
        raw = unwrap_nested_arguments(kwargs)
        clean = {k: v for k, v in raw.items() if k in accepted}
        dropped = [k for k in raw if k not in accepted]
        if dropped:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning(
                    "[工具参数] %s 收到未定义的参数 %s（已忽略；原始入参 %s）",
                    tool_name, dropped, list(kwargs.keys()),
                )
            if not clean and not args and raw:
                # 壳子里没有本工具认识的参数 → 大概率是参数名写错/整包塞错
                return param_notice(tool_name, accepted, list(raw.keys()))
        return await fn(self, event, *args, **clean)

    # 供调试/回归用例识别
    wrapper.__mc_tolerant__ = True  # type: ignore[attr-defined]
    return wrapper
