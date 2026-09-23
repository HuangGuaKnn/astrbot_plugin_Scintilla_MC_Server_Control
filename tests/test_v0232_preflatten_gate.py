# -*- coding: utf-8 -*-
"""v0.23.2 · 预扁平化（<1.13）自动生成守门。

运行：
  <python> tests/test_v0232_preflatten_gate.py

背景（GPT 续单裁决，docs/CONSULT_gpt_cross_version_2.md）
========================================================
前单约束写着「覆盖 1.12 ~ 1.21+」，但实现里语法世代只有两档
（``legacy_nbt`` / ``components``），``legacy_nbt`` 实际表示的是 **1.13~1.20.4**。
于是 1.12.2 服务端会被**静默当作 1.13+** 生成命令 —— 而 1.13 的 ``give`` 取消了
data 参数、NBT 移到物品 ID 之后、数量后置，旧 ``execute`` 也被重写。
用户会拿到一条「看似正常、实际必然不兼容」的命令。

裁决：**A + D**（收窄承诺 + 显式警告），不实现 ``legacy_preflatten``（P1 阶段再做）。

本测试必须钉死五件事
====================
1. ``<1.13`` 不再落进 ``legacy_nbt``，而是 ``unsupported_preflatten``；
2. 已知 ``<1.13`` 时**手填语法世代也不放行**（那是 1.13~1.20.4 的写法）；
3. **版本未知的逃生出口不许被误伤**（异地模式仍能靠手填世代放行）；
4. 注入片段必须**明确拒绝自动生成**，且**不给**任何 NBT / 组件模板；
5. **代码侧硬拦**：1.12.2 下带数据的命令**根本不调用 rcon.command()**
   —— 只靠提示词不算数（裁决 Q4 原话）。

v0.23.2 第二版（GPT 核验 P0-1 / P0-2 / P0-3 + Q6）
====================================================
6. **物品类命令一律拦**（``give`` / ``clear`` / ``item`` / ``replaceitem``）：
   原先放行的「不带数据的 give」也拦 —— 物品 ID 本身可能已扁平化改名；
7. **``mc_execute_command`` 必须守门**：它是 ``@filter.llm_tool``，
   命令由 LLM 生成、不等于「用户手写」，不能绕过版本层；
8. **全仓执行入口统一**：用 AST 扫描证明 10 个入口都调用 ``_guard_command_for_version()``；
9. ``function_system`` 分水岭已登记（Q6：现在加入能力清单、暂不实现生成）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

import ast  # noqa: E402

from astrbot_plugin_Scintilla_MC_Server_Control import main as plugin_main  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import version_caps as vc  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow  # noqa: E402

_fail: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


#: 1.12.2 上的「带 NBT」写法 —— 1.13+ 的顺序，在 1.12.2 必然解析失败
PREFLATTEN_BAD_CMD = (
    'give Steve minecraft:diamond_sword{Enchantments:[{id:"minecraft:sharpness",lvl:5}]} 1'
)
#: 不带数据的物品命令。**v0.23.2 第二版起也拦**（GPT 核验 P0-2）：
#: 参数位置虽然一致，但 `minecraft:diamond_sword` 本身可能就是扁平化后的现代 ID
#: （1.12 里没有 `red_wool`，只有 `wool` + data 14），故一律拒绝。
PREFLATTEN_ITEM_CMD = "give Steve minecraft:diamond_sword 1"
#: 1.12.2 上仍然合法、可照常生成的简单命令（白名单）
PREFLATTEN_OK_CMD = "time set day"


# ===================== 一、语法世代三态之外多一态 =====================

def syntax_cases() -> None:
    print("---- 一、语法世代：<1.13 单独成态 ----")
    check("★1.12.2 → unsupported_preflatten（**不再**落进 legacy_nbt）",
          vc.item_syntax_for((1, 12, 2)) == vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN,
          str(vc.item_syntax_for((1, 12, 2))))
    check("1.8 → unsupported_preflatten", vc.item_syntax_for((1, 8)) == vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN)
    check("★1.13 本身 → legacy_nbt（分水岭本身算新语法）",
          vc.item_syntax_for((1, 13)) == vc.ITEM_SYNTAX_LEGACY,
          str(vc.item_syntax_for((1, 13))))
    check("1.13.2 → legacy_nbt", vc.item_syntax_for((1, 13, 2)) == vc.ITEM_SYNTAX_LEGACY)
    check("★回归：1.20.4 → legacy_nbt / 1.20.5 → components（v0.22.6 契约不许破）",
          vc.item_syntax_for((1, 20, 4)) == vc.ITEM_SYNTAX_LEGACY
          and vc.item_syntax_for((1, 20, 5)) == vc.ITEM_SYNTAX_COMPONENTS)
    check("回归：版本未知 → unknown", vc.item_syntax_for(None) == vc.ITEM_SYNTAX_UNKNOWN)
    check("分水岭常量 = 1.13", vc.ITEM_PREFLATTEN_CUTOVER == (1, 13))
    check("预扁平化世代常量存在（P1 阶段实现时复用）",
          vc.ITEM_SYNTAX_PREFLATTEN == "legacy_preflatten")


def override_cases() -> None:
    print("---- 二、手填语法世代：已知 <1.13 不放行，未知仍放行 ----")
    known = vc.resolve_version_info("1.12.2", "")
    check("★已知 1.12.2 + 手填 legacy_nbt → **仍然拒绝**（legacy_nbt 是 1.13~1.20.4 的写法）",
          vc.resolve_item_syntax(known, "legacy_nbt")[0] == vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN,
          str(vc.resolve_item_syntax(known, "legacy_nbt")))
    check("★已知 1.12.2 + 手填 components → 同样拒绝",
          vc.resolve_item_syntax(known, "components")[0] == vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN)
    check("已知 1.12.2 + auto → 拒绝，来源标 unsupported",
          vc.resolve_item_syntax(known, "auto") == (vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN, "unsupported"),
          str(vc.resolve_item_syntax(known, "auto")))

    # ---- 回归：补丁 3 的逃生出口（异地模式）不许被误伤 ----
    unknown = vc.resolve_version_info("", "")
    check("★回归：版本未知 + 手填 legacy_nbt → **仍然放行**（异地模式逃生出口）",
          vc.resolve_item_syntax(unknown, "legacy_nbt") == (vc.ITEM_SYNTAX_LEGACY, "override"),
          str(vc.resolve_item_syntax(unknown, "legacy_nbt")))
    check("★回归：版本未知 + auto → 仍拒绝",
          vc.resolve_item_syntax(unknown, "auto")[0] == vc.ITEM_SYNTAX_UNKNOWN)


# ===================== 三、注入片段 =====================

def context_cases() -> None:
    print("---- 三、注入片段：明确拒绝，且不给模板 ----")
    ctx = vc.build_version_context(vc.resolve_version_info("1.12.2", ""))
    check("★含「暂不支持自动生成命令」", "暂不支持自动生成命令" in ctx, ctx[:220])
    check("★点名 1.13 的原因（give 参数顺序 + execute + 扁平化）",
          "give" in ctx and "execute" in ctx and "扁平化" in ctx, ctx[:400])
    check("★要求 LLM 输出 success=false 并给用户明确说法",
          "success=false" in ctx and "1.13" in ctx, "")
    check("★**不得**给出 NBT 模板（给了就等于诱导生成错命令）",
          "Enchantments:[{id:" not in ctx, ctx[:400])
    check("★**不得**给出物品组件模板", "enchantments={levels:" not in ctx, "")
    check("给出白名单例外（time / say 等仍可生成）",
          "`say`" in ctx or "`time`" in ctx, "")
    check("★片段不为空（空串 = 让 LLM 凭记忆猜）", ctx.strip() != "")

    # 对照组：1.13+ 行为不变
    ctx20 = vc.build_version_context(vc.resolve_version_info("1.20.1", ""))
    check("★对照：1.20.1 仍给 NBT 模板（预扁平化改动不得外溢）",
          "Enchantments:[{id:" in ctx20, "")
    check("对照：1.20.1 片段不含「暂不支持自动生成命令」",
          "暂不支持自动生成命令" not in ctx20, "")


def capability_snapshot_cases() -> None:
    print("---- 四、能力快照（WebUI 用）----")
    caps = vc.describe_capabilities(vc.resolve_version_info("1.12.2", ""))
    check("★supported = False", caps["supported"] is False, str(caps.get("supported")))
    check("★support_note 说明低于 1.13 不支持", "不支持" in caps["support_note"], caps["support_note"])
    check("item_syntax = unsupported_preflatten",
          caps["item_syntax"] == vc.ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN)
    check("带 preflatten_cutover = 1.13", caps["preflatten_cutover"] == "1.13")
    check("带 verified_on（诚实标注真机矩阵）", "1.20.1" in caps["verified_on"], caps.get("verified_on"))
    caps20 = vc.describe_capabilities(vc.resolve_version_info("1.20.1", ""))
    check("★对照：1.20.1 supported = True", caps20["supported"] is True)
    check("对照：1.20.1 support_note = 支持自动命令生成",
          caps20["support_note"] == "支持自动命令生成", caps20["support_note"])


def cutover_cases() -> None:
    print("---- 五、分水岭清单：结构化（裁决 Q6）+ 1.13 条目补全 ----")
    required = ("version", "surface", "title", "affects", "before", "after",
                "action_before", "action_after", "tested", "detail")
    missing = [(c["version"], k) for c in vc.CUTOVERS for k in required if k not in c]
    check("★每条分水岭都带结构化字段", not missing, str(missing[:4]))

    c13 = next(c for c in vc.CUTOVERS if c["version"] == (1, 13))
    check("★1.13 条目补上了 give（旧版只写 execute，这是本次追问的起点）",
          "give" in c13["affects"], str(c13["affects"]))
    check("★1.13 条目写明 before 侧 = unsupported（1.12 不自动生成）",
          c13["action_before"] == "unsupported" and c13["before"] == "preflatten",
          f"{c13['action_before']}/{c13['before']}")
    check("★1.13 详情里点名 give 的参数顺序变化（数量/数据值位置）",
          "数量" in c13["detail"] and "数据值" in c13["detail"], c13["detail"][:200])
    check("1.20.5 条目归类为 item_format（与命令语法分层）",
          next(c for c in vc.CUTOVERS if c["version"] == (1, 20, 5))["surface"] == "item_format")
    check("1.21.2 条目归类为 attribute_id",
          next(c for c in vc.CUTOVERS if c["version"] == (1, 21, 2))["surface"] == "attribute_id")
    check("★新增 1.14（execute 条件子命令）", any(c["version"] == (1, 14) for c in vc.CUTOVERS))
    check("★新增 1.20.2（药水类 NBT 字段），且标注未真机验证",
          any(c["version"] == (1, 20, 2) and c["tested"] is False for c in vc.CUTOVERS))
    check("回归：1.20.5 分水岭仍在清单里（v0.22.6 契约）",
          any(c["version"] == (1, 20, 5) for c in vc.CUTOVERS))
    # ---- v0.23.2 第二版（GPT 核验 Q6）：function 体系独立登记 ----
    fs = next((c for c in vc.CUTOVERS if c.get("surface") == "function_system"), None)
    check("★新增 function_system 分水岭条目（Q6：现在登记、暂不实现生成）", fs is not None)
    check("★function_system 归 1.13、affects 含 function、before 侧 unsupported",
          fs is not None and fs["version"] == (1, 13) and "function" in fs["affects"]
          and fs["action_before"] == "unsupported", str(fs and fs.get("affects")))
    check("★function_system 标注未真机验证（tested=False）",
          fs is not None and fs["tested"] is False)
    check("★function_system 与 command_syntax 分层（surface 不同）",
          fs is not None and fs["surface"] == "function_system")


def gate_judgement_cases() -> None:
    print("---- 六、命令族判据 ----")
    check("★带 NBT 的 give → 拦", bool(vc.preflatten_block_reason(PREFLATTEN_BAD_CMD)))
    check("★★不带数据的 give 也拦（P0-2：物品 ID 扁平化风险）",
          bool(vc.preflatten_block_reason(PREFLATTEN_ITEM_CMD)),
          vc.preflatten_block_reason(PREFLATTEN_ITEM_CMD))
    check("★物品类拒绝文案点名「旧版物品 ID/data 映射」",
          "旧版物品 ID/data 映射" in vc.preflatten_block_reason(PREFLATTEN_ITEM_CMD),
          vc.preflatten_block_reason(PREFLATTEN_ITEM_CMD))
    for cmd in ("execute as @a run say hi", "effect give Steve minecraft:speed 10 1",
                "difficulty hard", "summon minecraft:zombie", "setblock 1 2 3 stone",
                "clear Steve minecraft:diamond_sword{Enchantments:[]}",
                "clear Steve", "give Steve wool 1", "clear Steve wool",
                "item replace entity Steve slot.weapon.mainhand with diamond",
                "replaceitem entity Steve slot.weapon.mainhand diamond"):
        check(f"拦：{cmd[:44]}", bool(vc.preflatten_block_reason(cmd)))
    for cmd in ("say hello", "time set day", "weather clear", "list",
                "gamemode creative Steve", "op Steve",
                'tellraw @a {"text":"hi"}', 'title @a title {"text":"hi"}',
                "kick Steve", "ban Steve", "pardon Steve", "banlist"):
        check(f"放：{cmd[:44]}", vc.preflatten_block_reason(cmd) == "",
              vc.preflatten_block_reason(cmd))
    check("★tellraw / title / banlist 在 1.12 白名单内（插件自身入口依赖）",
          {"tellraw", "title", "banlist"} <= vc.PREFLATTEN_SAFE_COMMANDS)
    check("★旧常量 PREFLATTEN_DATA_ONLY_COMMANDS 已移除（避免误用）",
          not hasattr(vc, "PREFLATTEN_DATA_ONLY_COMMANDS"))
    check("★物品类集合含 give/clear/item/replaceitem",
          set(vc.PREFLATTEN_ITEM_COMMANDS) == {"give", "clear", "item", "replaceitem"},
          str(sorted(vc.PREFLATTEN_ITEM_COMMANDS)))
    check("斜杠前缀也能正确识别", bool(vc.preflatten_block_reason("/execute as @a run say hi")))
    check("空命令 → 有原因（不静默放行）", bool(vc.preflatten_block_reason("")))


# ===================== 七、代码侧硬拦（裁决 Q4 的核心） =====================

class StubRcon:
    def __init__(self, out="Gave 1 [Diamond Sword] to Steve"):
        self._out = out
        self.last_boundary_confirmed = True
        self.last_response_received = True
        self.sent: list[str] = []

    async def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        return self._out


class StubPlugin:
    def __init__(self, rcon, version=""):
        self._rcon = rcon
        self._version = version
        self.feedbacks: list[str] = []

    async def _get_rcon(self):
        return self._rcon

    async def _safe_command(self, event, cmd, **kw):
        return None

    async def _send_feedback(self, rcon, tip):
        self.feedbacks.append(tip)

    def _resolve_version_info(self):
        return vc.resolve_version_info(self._version, "")

    def _version_context_text(self):
        return vc.build_version_context(self._resolve_version_info())

    def _cfg(self, key, default=None):
        return default


def make_workflow(rcon, version="", preflatten=False):
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon, version)
    wf.logger = logging.getLogger("test.preflatten")
    wf._apply_player_to_commands = lambda cmds, player: cmds
    wf._preflatten = preflatten
    return wf


def _marker_from_version(version: str) -> bool:
    """复刻 _refresh_version_context 的判据，验证标记算法本身。"""
    wf = make_workflow(StubRcon(), version)
    try:
        info = wf.plugin._resolve_version_info()
        return bool(info.mc is not None and tuple(info.mc) < vc.ITEM_PREFLATTEN_CUTOVER)
    except Exception:
        return False


async def gate_execution_cases() -> None:
    print("---- 七、代码侧硬拦：1.12.2 下带数据命令**不发送** ----")

    # ① 1.12.2 + 附魔请求 → 一条命令都不许发
    rcon = StubRcon()
    wf = make_workflow(rcon, "1.12.2", preflatten=True)
    text, status = await wf._run_simple({"commands": [PREFLATTEN_BAD_CMD]}, "附魔剑", "Steve", None)
    check("★★1.12.2 + 附魔 give → **rcon.command 一次都没被调用**（裁决 Q4 原话）",
          rcon.sent == [], str(rcon.sent))
    check("★回执说明「因服务端版本不支持自动生成而未发送」",
          "不支持" in text, text[:200])
    check("★回执不给成功口径（不得出现「已成功 1/1」）",
          "已成功 0/1" in text, text[:120])
    check("工作流状态不是 done", status != "done", status)

    # ② 1.12.2 + 简单命令 → 正常发送
    rcon = StubRcon("Set the time to day")
    wf = make_workflow(rcon, "1.12.2", preflatten=True)
    text, status = await wf._run_simple({"commands": ["time set day"]}, "白天", "Steve", None)
    check("★1.12.2 + time set day → 正常发送（白名单例外）",
          rcon.sent == ["time set day"], str(rcon.sent))
    check("1.12.2 + time set day → 正常成功口径", status == "done", status)

    # ③ 1.20.1 + 附魔请求 → 不受影响（回归）
    rcon = StubRcon("Gave 1 [Netherite Sword] to Steve")
    wf = make_workflow(rcon, "1.20.1", preflatten=False)
    text, status = await wf._run_simple({"commands": [PREFLATTEN_BAD_CMD]}, "附魔剑", "Steve", None)
    check("★对照：1.20.1 下同一条命令照常发送（守门不得外溢）",
          len(rcon.sent) == 1, str(rcon.sent))

    # ④ 混合批次：拦掉非法、放行合法
    rcon = StubRcon("ok")
    wf = make_workflow(rcon, "1.12.2", preflatten=True)
    text, status = await wf._run_simple(
        {"commands": ["say 集合", PREFLATTEN_BAD_CMD, "weather clear"]}, "混合", "Steve", None)
    check("★混合批次只发合法命令",
          rcon.sent == ["say 集合", "weather clear"], str(rcon.sent))
    check("★混合批次回执同时给出「已成功 N/3」与拦截说明",
          "已成功" in text and "/3" in text
          and "1 条因服务端版本不支持自动生成而未发送" in text, text[:260])
    check("混合批次：被拦的命令在明细里标为「未发送」",
          "[未发送]" in text, text[:400])

    # ⑤ 复杂路径 _exec_commands 同样守门
    rcon = StubRcon("ok")
    wf = make_workflow(rcon, "1.12.2", preflatten=True)
    wf._online_players = lambda: _async(["Steve"])
    reports = await wf._exec_commands(
        [{"command": PREFLATTEN_BAD_CMD, "feedback": ""}, {"command": "say hi", "feedback": ""}],
        "Steve", ["Steve"])
    check("★★复杂路径：非法命令未被发送",
          PREFLATTEN_BAD_CMD not in rcon.sent, str(rcon.sent))
    check("★复杂路径：非法命令记为 skipped 且原因含「暂不支持该版本的自动命令生成」",
          any(r.get("status") == "skipped"
              and "暂不支持该版本的自动命令生成" in str(r.get("output"))
              for r in reports),
          str(reports[:2]))
    check("复杂路径：合法命令照常发送", "say hi" in rcon.sent, str(rcon.sent))


async def _async(v):
    return v


def marker_cases() -> None:
    print("---- 八、标记算法（_refresh_version_context 的判据）----")
    check("★1.12.2 → 标记为 True", _marker_from_version("1.12.2") is True)
    check("1.13 → 标记为 False", _marker_from_version("1.13") is False)
    check("1.20.1 → 标记为 False", _marker_from_version("1.20.1") is False)
    check("★版本未知（异地模式）→ 标记为 False（未知不按预扁平化处理）",
          _marker_from_version("") is False)
    check("声明解析不出（'最新版'）→ 标记为 False（走 unknown 分支）",
          _marker_from_version("最新版") is False)


# ===================== 九、统一版本守门（GPT 核验 P0-1 / P0-3） =====================

class GuardStub:
    """把插件类里**真实**的版本守门方法绑到替身上（与 test_tool_guard.py 同一手法）。"""

    def __init__(self, version: str = ""):
        self._version = version
        self.logger = logging.getLogger("test.guard")

    def _resolve_version_info(self):
        return vc.resolve_version_info(self._version, "")


for _name in ("_preflatten_block_reason", "_guard_command_for_version"):
    _fn = getattr(plugin_main.McControlPlugin, _name, None)
    if _fn is not None:
        setattr(GuardStub, _name, _fn)


def _guard(version: str, command: str, **kw) -> str:
    return GuardStub(version)._guard_command_for_version(command, **kw)


def guard_cases() -> None:
    print("---- 九、统一版本守门（P0-1：mc_execute_command 也拦）----")
    check("★统一守门函数存在于插件类",
          hasattr(plugin_main.McControlPlugin, "_guard_command_for_version"))

    # ① 1.12.2：受影响命令一律拦（mc_execute_command 走的就是这条路径）
    for cmd in ("give Steve red_wool 1",
                "give Steve minecraft:diamond_sword{Enchantments:[]} 1",
                "execute as @a run say hi", "effect give Steve minecraft:speed 10 1",
                "summon minecraft:zombie", "setblock 1 2 3 stone"):
        check(f"★★1.12.2 拦：{cmd[:40]}", bool(_guard("1.12.2", cmd)))

    # ② 1.12.2：白名单命令放行（插件自身入口依赖它们）
    for cmd in ("time set day", "weather clear", "say hi", "list", "kick Steve",
                "ban Steve", "pardon Steve", 'tellraw @a {"text":"hi"}',
                'title @a title {"text":"hi"}'):
        check(f"★1.12.2 放：{cmd[:40]}", _guard("1.12.2", cmd) == "")

    # ③ 拒绝文案统一
    msg = _guard("1.12.2", "give Steve wool 1")
    check("★拒绝文案含「暂不支持该服务端版本的自动命令生成」",
          "暂不支持该服务端版本的自动命令生成" in msg, msg)
    check("★拒绝文案给出两条出路（手动执行 / 升级 1.13+）",
          "手动执行适配该版本的命令" in msg and "1.13 及以上" in msg, msg)

    # ④ 不外溢：1.13+ / 未知 / 解析不出 一律放行
    for ver in ("1.13", "1.20.1", "1.21", ""):
        check(f"★对照：{ver or '未知'} + 附魔 give → 放行（守门不外溢）",
              _guard(ver, PREFLATTEN_BAD_CMD) == "")
    check("对照：'最新版'（解析不出）→ 放行", _guard("最新版", PREFLATTEN_BAD_CMD) == "")

    # ⑤ manual 通道（P1 预留）
    check("manual=True → 放行（人工原始命令通道，P1 预留）",
          _guard("1.12.2", "execute as @a run say hi", manual=True) == "")

    # ⑥ AST 扫描：全仓执行入口必须统一守门
    src = Path(plugin_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = {
        "mc_execute_command", "mc_broadcast", "mc_give_item", "mcs_say", "mcs_title_cmd",
        "mcs_kick_cmd", "mcs_ban_cmd", "mcs_unban_cmd", "mc_kick", "mc_ban",
    }
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in targets:
            seg = ast.get_source_segment(src, node) or ""
            found[node.name] = "_guard_command_for_version" in seg
    missing = sorted(x for x in targets if not found.get(x))
    check(f"★★全部 {len(targets)} 个执行入口都调用 _guard_command_for_version()",
          not missing, f"未守门：{missing}")
    check("★mc_execute_command 已守门（GPT 核验 P0-1 的核心）",
          found.get("mc_execute_command") is True)

    # ⑦ 旧口径（「只拦自动构造路径」）已从文档里去掉
    doc = plugin_main.McControlPlugin._preflatten_block_reason.__doc__ or ""
    check("★_preflatten_block_reason 文档不再声称「只拦自动构造路径」",
          "所有执行入口" in doc and "用户显式写下的命令（mc_execute_command）不拦" not in doc,
          doc[:160])


def main() -> int:
    syntax_cases()
    override_cases()
    context_cases()
    capability_snapshot_cases()
    cutover_cases()
    gate_judgement_cases()
    asyncio.run(gate_execution_cases())
    marker_cases()
    guard_cases()
    print("==========================================")
    if _fail:
        print(f"FAILED {len(_fail)} 项：")
        for f in _fail:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{_pass} 项）：<1.13 从「看似支持」变成「明确不支持」，"
          f"物品类命令（含裸 give）与所有执行入口都被统一守门拦下"
          f"（不会调用 rcon.command）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
