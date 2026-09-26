# -*- coding: utf-8 -*-
"""「拦截即终局」护栏回归用例（v0.21.0）。

背景（2026-09-12 QQ 群里要金剑那次）：
  非管理员让 AI 发金剑，工具确实被权限闸门拦了，但 AI 把返回的
  「白名单策略下非管理员不能用命令工具」当成「用法不对/工具报错」，
  于是换参数、换命令接着硬试。

本用例覆盖四道防线：
  A. 参数误封装（{"arguments": {...}}）→ 先剥壳再判定，不再误判为权限/用法问题；
  B. 闸门拒绝 → 终局化文案（明确「不是参数问题、重试与换工具都无效」）；
  C. 会话闩锁 → 被拦后同会话的后续命令类调用被直接短路；
  D. 权限前置提醒 → 按需注入（v0.23.3）：日常闲聊不打扰，仅当消息涉及 MC 或本会话刚调过 MC 工具时才提前告知；
  E. 所有命令类工具都挂了 tolerant 装饰器（防漏挂）。

运行（必须用 AstrBot 自带 python，插件依赖 astrbot 包）：
  python tests\\test_tool_guard.py
"""
import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402
add_sys_paths()
require_app()
PLUGIN = PLUGIN_DIR


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from astrbot_plugin_Scintilla_MC_Server_Control.core import tool_guard as tg  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.tool_guard import COMMAND_TOOLS  # noqa: E402

FAIL = []


def check(desc, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAIL.append(desc)
    print(f"[{tag}] {desc:<62}{extra}")


# ===================== A. 参数误封装剥壳 =====================
print('=========== A. 参数误封装（{"arguments": {...}}） ===========')
u = tg.unwrap_nested_arguments
check("单层 dict 外壳", u({"arguments": {"command": "give Steve diamond 1"}})
      == {"command": "give Steve diamond 1"})
check("JSON 字符串外壳", u({"arguments": '{"command": "time set noon"}'})
      == {"command": "time set noon"})
check("四层嵌套（实测踩过）",
      u({"arguments": {"arguments": '{"arguments": {"arguments": {"player": "Steve"}}}'}})
      == {"player": "Steve"})
check("非 arguments 键原样返回", u({"command": "say hi"}) == {"command": "say hi"})
check("arguments 非 dict/非 JSON 原样返回", u({"arguments": 123}) == {"arguments": 123})
check("空入参返回空 dict", u(None) == {})

# ===================== B/C. 闩锁本体 =====================
print("\n=========== B. 会话闩锁 DenyLatch ===========")
latch = tg.DenyLatch(ttl=0.6)
check("未登记时 peek 为空", latch.peek("k") is None)
check("首次登记计数=1", latch.record("k", "白名单") == 1)
check("再次登记计数=2", latch.record("k", "白名单") == 2)
r = latch.peek("k")
check("命中返回 (原因, 短路次数) 且次数自增",
      bool(r) and r[0] == "白名单" and r[1] == 3, f"→ {r}")
time.sleep(0.7)
check("TTL 到期后自动解除", latch.peek("k") is None)

# ===================== D. 闸门 / 终局文案（真实插件方法） =====================
print("\n=========== C. 插件闸门：_safe_command / _admin_gate ===========")
import astrbot_plugin_Scintilla_MC_Server_Control.main as main  # noqa: E402


class FakeEvent:
    """最小可用事件替身（只需要插件用到的那几个接口）。"""

    def __init__(self, sender="10001", umo="webchat:FriendMessage:10001", text=""):
        self._sender = sender
        self.unified_msg_origin = umo
        self.message_str = text  # v0.23.3：按需提示注入要读本轮消息文本
        self._extra = {}

    def get_sender_id(self):
        return self._sender

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class FakeSelf:
    """只提供被调用方法所需属性的替身（不实例化整个 Star）。"""

    def __init__(self, admins=("9001",), policy="whitelist"):
        self.config = {
            "permission": {
                "admin_ids": list(admins),
                "danger_command_policy": policy,
                "permission_hint_injection": True,  # 旧键（v0.23.3 前）：模拟旧配置验证兼容路径
                "permission_latch": True,
                "permission_latch_ttl": 300,
            }
        }
        self._cfg_index = {k: "permission" for k in self.config["permission"]}
        self._admins = {str(x) for x in admins}
        self.logger = logging.getLogger("mc-test")
        self._deny_latch = tg.DenyLatch(ttl=300)
        self._mc_tool_recent = {}  # v0.23.3：按需提示注入的会话记忆表
        self._enabled = {t: True for t in COMMAND_TOOLS}

    def _cfg(self, key, default=None):
        box = self.config.get(self._cfg_index.get(key, ""), {})
        if isinstance(box, dict) and key in box:
            return box[key]
        return self.config.get(key, default)

    def _tool_enabled(self, name):
        return bool(self._enabled.get(name, True))

    def _is_admin(self, event):
        return bool(self._admins) and str(event.get_sender_id()) in self._admins

    def _sender_id(self, event):
        return str(event.get_sender_id())

    def _is_whitelist_policy(self):
        return str(self._cfg("danger_command_policy", "whitelist")) != "blacklist"

    def _preflatten_block_reason(self, command):
        """v0.23.2：版本能力守门不在本测试范围 —— 恒放行。

        本文件只验权限闸门；「1.12.2 下带数据命令不发送」由
        tests/test_v0232_preflatten_gate.py 专项覆盖。
        """
        return ""


# 把插件类里真实的闸门方法绑到替身上（不实例化 Star，但逻辑一模一样）
for _name in ("_latch", "_latch_enabled", "_latch_key", "_latch_hit", "_deny",
              "_admin_gate", "_safe_command", "_inject_permission_hint",
              "_append_user_hint",
              # v0.23.3：按需提示注入的整条判定链（真方法绑过来，逻辑一字不差）
              "_hint_mode", "_mc_tool_recall_ttl", "_mark_mc_tool_used",
              "_mc_tool_recent_hit", "_event_text", "_hint_keywords",
              "_hint_topic_hit", "_hint_trigger", "_emit_hint",
              # v0.23.2 第二版（GPT 核验 P0-1/P0-3）：所有执行入口都要过统一版本守门，
              # 本文件挂真实方法才能覆盖 mc_give_item / mc_execute_command 的新调用链。
              # 替身的 _preflatten_block_reason 恒返回 ""，故这些用例仍只验权限闸门。
              "_guard_command_for_version"):
    _fn = getattr(main.McControlPlugin, _name, None)
    if _fn is not None:
        setattr(FakeSelf, _name, _fn)


P = main.McControlPlugin
guest = FakeEvent("10001")
admin = FakeEvent("9001")

s_guest = FakeSelf(policy="whitelist")
deny = asyncio.run(P._safe_command(s_guest, guest, "give Steve golden_sword 1",
                                   tool="mc_give_item"))
check("白名单 + 非管理员 → 拦截", bool(deny) and deny.startswith(tg.MARK_DENY))
check("拦截文案声明「不是参数问题」", "不是参数错误" in deny and "改写参数重试" in deny)
check("拦截文案含被拦工具与命令", "mc_give_item" in deny and "golden_sword" in deny)
check("拦截文案给出替代方式", "mcs 喊话" in deny)
check("管理员放行",
      asyncio.run(P._safe_command(FakeSelf(), admin, "give Steve golden_sword 1")) is None)

s_black = FakeSelf(policy="blacklist")
check("黑名单 + 非管理员 + 普通命令 → 放行",
      asyncio.run(P._safe_command(s_black, guest, "give Steve golden_sword 1")) is None)
check("黑名单 + 非管理员 + 危险命令 → 拦截",
      (asyncio.run(P._safe_command(s_black, guest, "stop")) or "").startswith(tg.MARK_DENY))

# 会话闩锁：被拦一次后，同会话同请求者的后续调用直接短路
l_s = FakeSelf(policy="whitelist")
d1 = asyncio.run(P._safe_command(l_s, guest, "give Steve diamond 1"))
d2 = asyncio.run(P._safe_command(l_s, guest, "give Steve golden_sword 1"))
check("首次拦截 → 终局文案", d1.startswith(tg.MARK_DENY))
check("同会话再试 → 闩锁短路", d2.startswith(tg.MARK_LATCH), "→ " + d2.split("·")[1].strip())
check("闩锁短路文案要求立刻停手", "立刻停止调用" in d2)
check("闩锁不影响管理员",
      asyncio.run(P._safe_command(l_s, admin, "give Steve diamond 1")) is None)
other = asyncio.run(P._safe_command(
    l_s, FakeEvent("10001", "webchat:FriendMessage:20002"), "give Steve diamond 1"))
check("闩锁只锁本会话（其他会话仍是终局拦截、非短路）",
      other.startswith(tg.MARK_DENY))

# _admin_gate：踢人/封禁/热重载/词典/知识 等管理工具
a_s = FakeSelf()
g1 = P._admin_gate(a_s, guest, tool="mc_kick", action="踢人")
g2 = P._admin_gate(a_s, guest, tool="mc_ban", action="封禁")
check("非管理员踢人 → 终局拦截", g1.startswith(tg.MARK_DENY) and "mc_kick" in g1)
check("管理工具被拦同样记闩锁", g2.startswith(tg.MARK_LATCH))
check("管理员踢人放行", P._admin_gate(a_s, admin, tool="mc_kick", action="踢人") is None)

# 关掉闩锁后只做终局文案、不短路
off = FakeSelf()
off.config["permission"]["permission_latch"] = False
o1 = asyncio.run(P._safe_command(off, guest, "give Steve diamond 1"))
o2 = asyncio.run(P._safe_command(off, guest, "give Steve diamond 1"))
check("关闭闩锁 → 两次都是终局拦截（不短路）",
      o1.startswith(tg.MARK_DENY) and o2.startswith(tg.MARK_DENY))

# ===================== E. 权限前置提醒 =====================
print("\n=========== D. 权限前置提醒（挂到用户消息的额外内容块） ===========")

SYSTEM_PROMPT = "你是一个 Minecraft 服务器助手。"


class FakeReq:
    """ProviderRequest 的最小替身。

    v0.22.1 整改：提示**不再写进 system_prompt**（含请求者 ID 的动态内容会废掉
    前缀缓存），改挂到 extra_user_content_parts —— 与 AstrBot 自身系统提醒同一注入点。
    """

    def __init__(self):
        self.system_prompt = SYSTEM_PROMPT
        self.extra_user_content_parts: list = []

    @property
    def hint_text(self) -> str:
        out = []
        for part in self.extra_user_content_parts:
            out.append(part.get("text", "") if isinstance(part, dict) else getattr(part, "text", ""))
        return "\n\n".join(out)


h_s = FakeSelf()
ev = FakeEvent("10001", text="给我发一把钻石剑")  # 命中 MC 话题关键词
req = FakeReq()
asyncio.run(P._inject_permission_hint(h_s, ev, req))
check("on_demand（默认）：命中 MC 话题 → 注入前置提醒", "权限前置提醒" in req.hint_text)
check("提醒里点名不可用的工具", "mc_give_item" in req.hint_text)
check("提醒里给出替代方式", "mcs 喊话" in req.hint_text)
check("文案里不再出现请求者 ID（v0.23.3）", "10001" not in req.hint_text)
check("提示挂在用户消息内容块上（不是系统提示词）",
      len(req.extra_user_content_parts) == 1
      and isinstance(req.extra_user_content_parts[0], dict)
      and req.extra_user_content_parts[0].get("type") == "text")
check("system_prompt 一字未动（动态改写会破坏前缀缓存）",
      req.system_prompt == SYSTEM_PROMPT)
req2 = FakeReq()
asyncio.run(P._inject_permission_hint(h_s, ev, req2))
check("同一事件只注入一次（多轮工具循环不重复膨胀）",
      "权限前置提醒" not in req2.hint_text and not req2.extra_user_content_parts)

# ---- v0.23.3 核心：日常闲聊不再被注入（主人反馈的误伤场景）----
chat_s = FakeSelf()
req_chat = FakeReq()
asyncio.run(P._inject_permission_hint(
    chat_s, FakeEvent("10001", text="今天天气真好呀，陪我聊聊天～"), req_chat))
check("on_demand：日常闲聊零注入（误伤修复）",
      "权限前置提醒" not in req_chat.hint_text and not req_chat.extra_user_content_parts)

# ---- 第一路信号：本会话刚试图调用过 MC 工具 → 后续请求补上提醒 ----
tool_s = FakeSelf()
ev_tool = FakeEvent("10001", umo="chat:GroupMessage:1")
tool_s._mark_mc_tool_used(ev_tool)
check("调用过 MC 工具 → 该会话被记住", tool_s._mc_tool_recent_hit(ev_tool) is True)
req_tool = FakeReq()
asyncio.run(P._inject_permission_hint(
    tool_s, FakeEvent("10001", umo="chat:GroupMessage:1", text="那接下来呢"), req_tool))
check("on_demand：本会话近期调用过 MC 工具 → 注入（即便本轮没提 MC）",
      "权限前置提醒" in req_tool.hint_text)
check("会话隔离：另一会话不受影响",
      tool_s._mc_tool_recent_hit(FakeEvent("10001", umo="chat:GroupMessage:2")) is False)

# ---- 埋点走真链路：tolerant_tool 装饰器自动记一笔 ----
mark_s = FakeSelf()
asyncio.run(P.mc_give_item(
    mark_s, FakeEvent("10001", umo="chat:GroupMessage:9", text="给我发钻石"),
    player="Steve", item="diamond"))
check("tolerant_tool 自动埋点：工具被调用后会话即被记住",
      mark_s._mc_tool_recent_hit(FakeEvent("10001", umo="chat:GroupMessage:9")) is True)

# ---- 三档触发时机 ----
def _set_mode(s, value):
    """改替身配置并同步 _cfg_index（真实插件的分组索引随配置一起更新）。"""
    s.config.setdefault("permission", {})["permission_hint_mode"] = value
    s._cfg_index["permission_hint_mode"] = "permission"
    return s


always_s = _set_mode(FakeSelf(), "always")
req_always = FakeReq()
asyncio.run(P._inject_permission_hint(
    always_s, FakeEvent("10001", text="陪我聊聊天"), req_always))
check("always 档：闲聊也注入（旧行为可一键恢复）", "权限前置提醒" in req_always.hint_text)
off_s = _set_mode(FakeSelf(), "off")
req_off = FakeReq()
asyncio.run(P._inject_permission_hint(
    off_s, FakeEvent("10001", text="给我发一把钻石剑"), req_off))
check("off 档：即便命中 MC 话题也不注入",
      "权限前置提醒" not in req_off.hint_text and not req_off.extra_user_content_parts)
legacy_s = FakeSelf()
legacy_s.config["permission"]["permission_hint_injection"] = False
check("旧键 false → 等价 off（兼容）", legacy_s._hint_mode() == "off")
check("旧键 true（未设 mode）→ 落到 on_demand（老用户自动受益）",
      FakeSelf()._hint_mode() == "on_demand")
check("mode 大小写不敏感且优先于旧键", _set_mode(FakeSelf(), "ALWAYS")._hint_mode() == "always")
check("非法 mode → 回落 on_demand", _set_mode(FakeSelf(), "乱填的值")._hint_mode() == "on_demand")

req3 = FakeReq()
asyncio.run(P._inject_permission_hint(
    h_s, FakeEvent("9001", text="给我发一把钻石剑"), req3))
check("管理员不注入权限提醒", "权限前置提醒" not in req3.hint_text
      and not req3.extra_user_content_parts)
req5 = FakeReq()
del req5.extra_user_content_parts  # 老版本 AstrBot 没这个字段
asyncio.run(P._inject_permission_hint(
    h_s, FakeEvent("10002", text="给我发一把钻石剑"), req5))
check("老版本无 extra_user_content_parts → 宁可不提醒，也不改写 system_prompt",
      req5.system_prompt == SYSTEM_PROMPT)

# ===================== F. 装饰器覆盖检查 =====================
print("\n=========== E. 命令类工具 tolerant 装饰器覆盖 ===========")
for t in COMMAND_TOOLS:
    fn = getattr(P, t, None)
    check(f"{t} 已挂 tolerant 装饰器", bool(getattr(fn, "__mc_tolerant__", False)))

# 真机验证剥壳后确实能落到闸门上（模拟 AstrBot 的 handler(event, **args) 展开）
wrapped = P.mc_execute_command.__wrapped__ if hasattr(P.mc_execute_command, "__wrapped__") else None
check("tolerant 保留原函数（functools.wraps）", wrapped is not None)


class ShellEvent(FakeEvent):
    pass


async def _call_with_shell():
    """LLM 把参数包成 {"arguments": {...}} 时，装饰器应剥壳后正常进入闸门。"""
    return await P.mc_give_item(FakeSelf(policy="whitelist"), ShellEvent("10001"),
                                arguments={"player": "Steve", "item": "golden_sword"})


res = asyncio.run(_call_with_shell())
check("误封装参数仍能触达闸门（返回权限拦截而非 TypeError）",
      isinstance(res, str) and res.startswith(tg.MARK_DENY), "→ " + str(res)[:40])

res2 = asyncio.run(P.mc_give_item(FakeSelf(policy="whitelist"), ShellEvent("10001"),
                                  arguments={"unknown_param": 1}))
check("壳内参数完全无法识别 → 标注「参数问题、与权限无关」",
      isinstance(res2, str) and res2.startswith(tg.MARK_PARAM))

print("\n================ 汇总 ================")
print(f"失败 {len(FAIL)} 项" + ("" if not FAIL else "：" + " / ".join(FAIL)))
sys.exit(1 if FAIL else 0)
