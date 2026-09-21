# -*- coding: utf-8 -*-
"""统一命令结果判定（v0.22.6）。

背景
====
v0.22.5 线上实测暴露「复杂 NBT 假成功」：分类器生成了 1.21 的物品组件语法
``netherite_sword[enchantments={levels:{...}}]`` 发给 1.20.1 服务端，服务器回：

    Expected whitespace to end one argument, but found trailing data

而 ``core/workflow.py::_run_simple`` 只要 ``rcon.command()`` 不抛异常就 ``ok_count += 1``，
且**成功分支丢弃明细** —— 于是回执写「已执行 1/1 条命令」，游戏里什么都没有。

本模块把「什么算成功」收口成唯一实现，供所有执行型入口复用。

三个维度互相独立
================
任何一个维度都不能代替另外两个：

===============  ================================================
status           这条命令的结果是什么
response_received  有没有收到属于**本命令**的响应（「发出去了」的证据）
boundary_confirmed 响应是否收到**可靠结束边界**（「读完整了」的证据）
===============  ================================================

``response_received=True`` + ``boundary_confirmed=False`` 是**合法组合**：
idle 降级模式下服务端确实答了这一条，但静默窗口到期，不保证内容完整。
此时对消息类命令应报「已发送·边界未确认」，既不是「失败」、也不是「结果未知」。

为什么错误用黑名单、成功用「非空且无错误标记」
============================================
严格白名单（必须命中 ``Gave`` 之类才算成功）语义更干净，但会和既有**熔断**叠加出事：
``_exec_commands`` 里某条判 ``unknown`` 会让后续命令全部 ``skipped``，
而模组命令的反馈文本千奇百怪 —— 「发枪 + 发弹药 + 反馈」这类多命令任务，
第一条误判就吞掉后面全部。因此：

* 错误：黑名单（``_SYNTAX_MARKERS`` / ``_FAILED_MARKERS``），命中即判负；
* 成功：非空 + 无错误标记 → ``success``，置信度记为 ``inferred``；
* 显式成功标记（``Gave`` 等）只用于把置信度抬到 ``explicit``。

若日后要收紧为严格白名单，把 ``STRICT_SUCCESS_MATCHING`` 置 ``True`` 即可
（未命中显式成功标记的非空输出会判 ``unknown``），无需改动调用方。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: 未命中显式成功标记的非空输出是否判 ``unknown``。
#: 默认 ``False`` —— 见模块 docstring「为什么错误用黑名单」一节。
STRICT_SUCCESS_MATCHING = False

CommandStatus = Literal[
    "success",
    "dispatched_unconfirmed",
    "failed",
    "syntax_error",
    "unknown",
    "skipped",
]

# ===================== 命令集合（三组，禁止混用） =====================
#
# 1) 静默集合：成功时**本来就不回文本**，空响应即成功。
# 2) 幂等集合：成功时**通常有回显**，但重复执行无额外副作用 ——
#    因此空响应也可当成功（兼容某些服务端/插件不回显的情形）。
# 3) 非幂等集合：重复执行会**叠加副作用**，空响应**永远不能**当成功。
#
# 注意：幂等 ≠ 静默。time / weather / gamerule / gamemode / difficulty
# 在 vanilla 里都是有回显的（"Set the time to Day"），它们属于幂等集合。

#: 成功时设计上就不回文本的命令（空响应 = 成功）
SILENT_EMPTY_SUCCESS_COMMANDS = frozenset({
    "tellraw", "say", "title", "subtitle", "actionbar", "tm", "teammsg",
})

#: 幂等命令：重复执行无额外副作用（空响应也可当成功）
IDEMPOTENT_COMMANDS = frozenset({
    "time", "weather", "gamerule", "difficulty", "gamemode",
    "kill", "clear", "deop", "op", "whitelist", "pardon", "banlist",
    "save-all", "save-off", "save-on", "reload", "seed", "list",
    "data", "scoreboard", "tag", "bossbar", "forceload", "difficulty",
})

#: 非幂等命令：重复执行会产生**额外**副作用
NON_IDEMPOTENT_COMMANDS = frozenset({
    "give", "item", "summon", "effect", "xp", "experience", "fill",
    "setblock", "clone", "function", "random", "spreadplayers",
    "enchant", "damage", "loot", "place", "advancement", "recipe",
})

# ===================== 文本特征 =====================

#: 「命令不存在」——**必须**先于其它 unknown/syntax 判定。
#: 与 "Unknown or incomplete command" 只差几个词，语义相反：
#: 前者是命令本身不存在（模组没装 / 版本不支持），重试徒劳；
#: 后者是语法写错（命令存在），可以安全纠错。
_UNKNOWN_COMMAND_MARKERS = (
    'unknown command. type "/help"',
    'unknown command. type \\"/help\\"',
    "unknown command. type",
)

#: 明确的解析/语法错误 —— 命令**从未执行**，因此可安全重写重试。
_SYNTAX_MARKERS = (
    "unknown or incomplete command",
    "expected whitespace",
    "trailing data",
    "incorrect argument",
    "could not parse",
    "failed to parse",
    "invalid argument",
    "unknown item",
    "unknown enchantment",
    "unknown component",
    "unknown recipe",
    "unknown entity",
    "malformed",
    "expected ",
    "无法解析",
    "语法错误",
)

#: 明确失败（非语法）—— 服务器拒绝执行，重试通常无意义。
_FAILED_MARKERS = (
    "no entity was found",
    "no player was found",
    "player not found",
    "entity not found",
    "permission denied",
    "no such",
    "does not exist",
    "not found",
    "you do not have",
    "unable to",
    "failed to",
    "cannot ",
    "can't ",
    "denied",
    "invalid",
    "error",
    "失败",
    "不存在",
    "找不到",
)

#: 显式成功标记 —— 只用来把置信度抬到 ``explicit``，不作为成功门槛。
_SUCCESS_MARKERS = (
    "gave ",
    "set the time",
    "changed the weather",
    "set the weather",
    "set difficulty",
    "set the gamerule",
    "set own game mode",
    "teleported ",
    "summoned ",
    "applied ",
    "cleared ",
    "killed ",
    "granted ",
    "enabled ",
    "disabled ",
    "placed ",
    "added ",
    "已发放",
    "已执行",
)


@dataclass
class CommandResult:
    """一条命令的执行结果。

    ``status`` / ``response_received`` / ``boundary_confirmed`` 是三个独立维度，
    任何一个都不能代替另外两个。
    """

    command: str
    status: CommandStatus
    output: str = ""
    reason: str = ""
    retryable: bool = False
    boundary_confirmed: bool | None = None
    response_received: bool = False
    attempt: int = 1
    #: ``explicit`` = 命中显式成功标记；``inferred`` = 非空且无错误标记（推断成功）；
    #: ``""`` = 与成功无关。
    confidence: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否可对外宣称「成功」。``dispatched_unconfirmed`` **不算**成功。"""
        return self.status == "success"

    @property
    def accepted(self) -> bool:
        """是否可视为「命令已被服务端接受」（用于决定要不要熔断后续命令）。

        ``dispatched_unconfirmed`` 表示消息确实发出去了，只是边界未确认 ——
        不应因此中止同批次的后续命令。
        """
        return self.status in ("success", "dispatched_unconfirmed")

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


# ===================== 复杂任务路由（v0.22.6） =====================
#
# 背景：分类器规则自相矛盾 —— core/agent_prompts.py 里
#   · "simple" ：仅用原版命令即可完成（发原版物品 diamond_sword…）
#   · "complex"：复杂 NBT 结构（附魔、枪械附件 Attachments…）
# 「附魔下界合金剑」**同时命中两条**，实测 LLM 选了 simple，
# 而 simple 是两条路径里唯一没有输出校验、没有纠错、没有游戏内反馈的一条。
#
# 因此不能只靠 prompt 守规矩：分类之后、执行之前要加一道**确定性**守门。

#: 用户请求里的复杂特征（中英文都要查 —— 不能只查中文）
_COMPLEX_REQUEST_MARKERS = (
    "附魔", "附魔书", "nbt", "组件", "满配", "属性", "属性修饰符",
    "自定义名称", "模型数据", "custommodeldata", "枪械", "配件", "附件",
    "attachments", "enchant", "attribute", "modifier",
)

#: 携带物品数据的命令 —— **只对这类命令**检查物品参数。
#: 不做全局字符扫描：裸 ``[`` 是选择器语法，``kill @e[type=item]`` /
#: ``tp @p[tag=foo]`` / ``gamemode creative @a[team=red]`` 全是正常简单命令，
#: 一旦按 ``[`` 触发，就会白跑一整套 Agent 流水线。
_ITEM_COMMANDS = frozenset({"give", "item", "replaceitem", "clear", "loot", "enchant"})

#: 物品参数里的复杂结构特征
_COMPLEX_ITEM_MARKERS = (
    "enchantments", "components", "levels:", "attributemodifiers",
    "custommodeldata", "attachments", "display", "candestroy",
    "canplaceon", "trim", "storedenchantments",
)


def effective_command_segment(command: str) -> tuple[str, str]:
    """把命令拆成 ``(有效命令名, 其后的参数原文)``，并穿透 execute 包装。

    全模块**唯一**的命令名解析器 —— 判定与路由共用，不维护第二份。
    """
    cmd = (command or "").strip()
    while cmd.startswith("/"):
        cmd = cmd[1:].lstrip()

    # 穿透 execute ... run <真命令>（可嵌套，取最后一个 run）
    low = cmd.lower()
    if low.startswith("execute"):
        idx = low.rfind(" run ")
        if idx == -1:
            return "execute", cmd[len("execute"):].lstrip()
        cmd = cmd[idx + 5:].lstrip()

    parts = cmd.split(None, 1)
    name = parts[0].lower() if parts else ""
    if ":" in name:
        name = name.split(":", 1)[1]
    return name, (parts[1] if len(parts) > 1 else "")


def looks_like_complex_item_command(command: str) -> bool:
    """命令是否为「携带复杂物品数据」的物品类命令（**命令上下文感知**）。

    只解析物品参数，不做全局字符扫描 —— 否则选择器语法会被大量误判。
    """
    name, args = effective_command_segment(command)
    if name not in _ITEM_COMMANDS:
        return False
    low = args.lower()
    return any(m in low for m in _COMPLEX_ITEM_MARKERS)


def looks_like_complex_request(request: str) -> bool:
    """用户请求文本是否命中复杂特征（第一层路由信号）。"""
    low = (request or "").lower()
    return any(m in low for m in _COMPLEX_REQUEST_MARKERS)


def looks_like_complex_task(request: str, commands: list | None = None) -> bool:
    """请求与生成命令**都要**检查，任一命中即判复杂。

    只做「升级」判定（simple → complex），不反向降级。
    """
    if looks_like_complex_request(request):
        return True
    for c in commands or []:
        if isinstance(c, dict):
            c = c.get("command") or ""
        if looks_like_complex_item_command(str(c)):
            return True
    return False


# ===================== 命令名解析 =====================


def base_name(command: str) -> str:
    """取命令的**有效命令名**（小写、去命名空间、穿透 execute 包装）。

    必须解析命令名而不是做字符串前缀匹配 —— 否则
    ``minecraft:tellraw`` / ``/tellraw`` / ``execute as @a run tellraw``
    都会被漏掉，而 ``tellrawx`` 这种不存在的命令又会被误当成 tellraw。
    """
    return effective_command_segment(command)[0]


def is_silent_command(command: str) -> bool:
    """成功时设计上就不回文本（空响应 = 成功）。"""
    return base_name(command) in SILENT_EMPTY_SUCCESS_COMMANDS


def is_idempotent_command(command: str) -> bool:
    return base_name(command) in IDEMPOTENT_COMMANDS


def is_non_idempotent_command(command: str) -> bool:
    """重复执行会产生额外副作用 —— 空响应**永远不能**当成功。"""
    return base_name(command) in NON_IDEMPOTENT_COMMANDS


# ===================== 文本分类 =====================


def _low(output: str) -> str:
    return (output or "").strip().lower()


def is_unknown_command_output(output: str) -> bool:
    """``Unknown command. Type "/help" for help.`` —— 命令本身不存在。"""
    low = _low(output)
    if not low:
        return False
    if any(m in low for m in _UNKNOWN_COMMAND_MARKERS):
        return True
    # 兜底形态：以 unknown command 开头且提到 help
    return low.startswith("unknown command") and "help" in low


def is_syntax_error_output(output: str) -> bool:
    """明确的解析/语法错误 —— 命令从未执行，可安全重写重试。"""
    low = _low(output)
    if not low or is_unknown_command_output(low):
        return False
    return any(m in low for m in _SYNTAX_MARKERS)


def is_explicit_failure_output(output: str) -> bool:
    low = _low(output)
    if not low or is_unknown_command_output(low) or is_syntax_error_output(low):
        return False
    return any(m in low for m in _FAILED_MARKERS)


def is_explicit_success_output(output: str) -> bool:
    """命中显式成功标记（``Gave`` 等）。只提升置信度，不作为成功门槛。"""
    low = _low(output)
    if not low:
        return False
    if is_unknown_command_output(low) or is_syntax_error_output(low):
        return False
    if is_explicit_failure_output(low):
        return False
    return any(m in low for m in _SUCCESS_MARKERS)


# ===================== 总入口 =====================


def classify_command_output(
    command: str,
    output: str,
    *,
    boundary_confirmed: bool | None = None,
    response_received: bool = False,
    attempt: int = 1,
) -> CommandResult:
    """把一次 ``rcon.command()`` 的返回判成 ``CommandResult``。

    判定顺序（**成功识别必须早于 unknown 兜底**）：

    1. 空响应 → 按命令类型 + ``response_received`` + ``boundary_confirmed`` 三态判
    2. ``Unknown command`` → ``failed``（命令不存在，重试徒劳）
    3. 明确解析错误 → ``syntax_error``（可安全重写重试）
    4. 其它明确失败 → ``failed``
    5. 非空且无错误标记 → ``success``（``inferred``；命中显式标记则 ``explicit``）
    6. 兜底 → ``unknown``
    """
    out_s = str(output or "").strip()
    res = CommandResult(
        command=command,
        status="unknown",
        output=out_s,
        boundary_confirmed=boundary_confirmed,
        response_received=bool(response_received),
        attempt=attempt,
    )

    # ---- 1) 空响应：三个维度一起看 ----
    if not out_s:
        name = base_name(command)
        if name in NON_IDEMPOTENT_COMMANDS:
            # 非幂等命令收到「合法空响应」也不能当成功：副作用是否落定无法确认
            res.status = "unknown"
            res.reason = "非幂等命令收到空响应，无法确认副作用是否落定"
            res.retryable = False
            return res
        if name in SILENT_EMPTY_SUCCESS_COMMANDS or name in IDEMPOTENT_COMMANDS:
            if boundary_confirmed is True:
                res.status = "success"
                res.confidence = "empty"
                res.reason = "静默/幂等命令，响应边界已确认，空响应即成功"
                return res
            if response_received:
                res.status = "dispatched_unconfirmed"
                res.reason = "已收到本命令响应，但响应边界未确认（可能处于 idle 降级模式）"
                return res
            res.status = "unknown"
            res.reason = "空响应且未收到本命令响应，无法确认命令是否送达"
            return res
        res.status = "unknown"
        res.reason = "未知命令类型的空响应"
        return res

    # ---- 2) 命令不存在（必须先于其它 unknown/syntax） ----
    if is_unknown_command_output(out_s):
        res.status = "failed"
        res.reason = "该命令在此服务端不存在（模组未安装 / 版本不支持 / 命名空间错误）"
        res.retryable = False
        return res

    # ---- 3) 明确解析错误：命令从未执行，可安全重写 ----
    if is_syntax_error_output(out_s):
        res.status = "syntax_error"
        res.reason = "服务端明确拒绝解析（命令未执行，可安全重写后重试）"
        res.retryable = True
        return res

    # ---- 4) 其它明确失败 ----
    if is_explicit_failure_output(out_s):
        res.status = "failed"
        res.reason = "服务端明确拒绝执行"
        res.retryable = False
        return res

    # ---- 5) 成功识别（必须早于 unknown 兜底） ----
    if is_explicit_success_output(out_s):
        res.status = "success"
        res.confidence = "explicit"
        res.reason = "命中显式成功标记"
        return res
    if not STRICT_SUCCESS_MATCHING:
        res.status = "success"
        res.confidence = "inferred"
        res.reason = "非空输出且无任何错误标记（推断成功）"
        return res

    # ---- 6) 兜底 ----
    res.status = "unknown"
    res.reason = "输出文本无法判定（严格模式：未命中显式成功标记）"
    return res


#: 状态 → 中文短标签（回执/日志统一措辞）
STATUS_LABEL = {
    "success": "成功",
    "dispatched_unconfirmed": "已发送·边界未确认",
    "failed": "失败",
    "syntax_error": "语法错误",
    "unknown": "结果未知",
    "skipped": "未发送",
}


def format_results(results: list[CommandResult]) -> str:
    """把结果列表渲染成可读明细（成功分支**也必须**带上，不允许丢明细）。"""
    lines = []
    for r in results:
        tag = STATUS_LABEL.get(r.status, r.status)
        lines.append(f"[{tag}] {r.command}")
        if r.output:
            lines.append(f"    服务器返回：{r.output}")
        elif r.reason:
            lines.append(f"    （{r.reason}）")
    return "\n".join(lines)
