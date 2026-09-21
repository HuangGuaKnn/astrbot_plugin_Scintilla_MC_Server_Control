# -*- coding: utf-8 -*-
"""v0.22.8 回归：**结果不确定 → 工作流绝不自动重试**（核验 P1 收口）。

靶心（v0.22.7 遗留的 P1）：
  · 模组命令返回「非空、无错误标记、也无成功证据」的输出 → ``inferred_success``
  · 它 ``ok=False`` / ``accepted=True`` —— 而 ``accepted`` 只表示「同批次后续命令可以继续发」
  · 但复杂工作流的重试判据是 ``all_ok = all(r["ok"] for r in exec_reports)``
  · 于是本轮被判「没做完」→ 实现器重新生成命令 → **再发一次**
  · 对未被 ``NON_IDEMPOTENT_COMMANDS`` 覆盖的模组命令，这就是重复副作用

改后口径只有一句话：**「是否生效」不确定，就不许自动重试。**
  · ``unknown`` / ``inferred_success`` / ``dispatched_unconfirmed`` → 立即停手
  · ``failed`` / ``syntax_error`` → 确定没生效，仍允许纠错重试（**不得扩大熔断范围**）

跑法：<python> tests\\test_v0228_no_retry_on_uncertain.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

FAIL: list[str] = []

add_sys_paths()
try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import (
        UNCERTAIN_STATUSES,
        MCWorkflow,
    )
    from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (
        classify_command_output,
        is_non_idempotent_command,
    )
    IMPORT_OK = True
    IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    IMPORT_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}"
          + (f"  <- {detail}" if detail and not ok else ""))


#: 一条**未被非幂等清单覆盖**的模组命令（P1 的靶心样本）
MOD_CMD = "tacz give Steve modern_kinetic_gun 1"
#: 模组回显：非空、无错误标记、也没有原版成功标记 —— 于是落 inferred_success
MOD_OUT = "[TACZ] 已为 Steve 装配 Modern Kinetic Gun（模组回显，无原版成功标记）"
#: 确定没生效的语法错误（允许纠错重试的对照样本）
SYNTAX_ERR = "Expected whitespace to end one argument, but found trailing data"


class StubRcon:
    def __init__(self, out="", *, boundary=True, received=True):
        self._out = out
        self.last_boundary_confirmed = boundary
        self.last_response_received = received
        self.sent: list[str] = []

    async def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        return self._out


class StubPlugin:
    def __init__(self, rcon):
        self._rcon = rcon
        self.feedbacks: list[str] = []
        self._knowledge = None
        self._dictionary = None

    def _cfg(self, key, default=None):
        return default

    async def _get_rcon(self):
        return self._rcon

    async def _send_feedback(self, rcon, tip):
        self.feedbacks.append(tip)


class StubAgent:
    """只回固定命令的假实现器；记录每个 Agent 被调用的次数。"""

    def __init__(self, commands):
        self.commands = list(commands)
        self.implement_calls = 0
        self.correct_calls = 0

    async def judge(self, request, kb_text, umo=None):
        return {"sufficient": True}

    async def engineer(self, request, kb_text, dict_text, umo=None):
        return {"templates": []}

    async def implement(self, request, player, kb_text, failures,
                        retry_hint, umo=None, online_players=None):
        self.implement_calls += 1
        return {"commands": [{"command": c} for c in self.commands],
                "success": True, "reasoning": "stub 实现器"}

    async def correct(self, request, kb_text, failures, kb_text2=None, umo=None):
        self.correct_calls += 1
        return {"corrections": [], "retry_hint": "换个写法"}


async def _fake_resolve(player, request):
    """隔离玩家名解析：本测试只验「重试语义」这一件事。"""
    return ((player or "Steve").strip(), [])


def make_workflow(rcon, agent) -> MCWorkflow:
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon)
    wf.logger = logging.getLogger("test.v0228")
    wf.agent = agent
    wf._resolve_player = _fake_resolve
    return wf


async def run_complex(rcon, agent, request="给 Steve 一把模组枪"):
    wf = make_workflow(rcon, agent)
    return await wf._run_complex(request, "Steve", "")


# ===================== 用例 =====================

def group_premise() -> None:
    print("---- 零、判定前提：这条模组命令确实落 inferred_success ----")
    res = classify_command_output(MOD_CMD, MOD_OUT,
                                  boundary_confirmed=True, response_received=True)
    check("模组命令未被非幂等清单覆盖（P1 的成立条件）",
          not is_non_idempotent_command(MOD_CMD), MOD_CMD)
    check("★非空 + 无错误标记 + 无成功证据 → inferred_success",
          res.status == "inferred_success", res.status)
    check("★inferred_success 的 ok=False、accepted=True（正是 P1 的矛盾点）",
          res.ok is False and res.accepted is True,
          f"ok={res.ok} accepted={res.accepted}")


def group_unit() -> None:
    print("---- 一、统一判据 _halt_on_uncertain（唯一实现）----")
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(StubRcon())
    wf.logger = logging.getLogger("test.v0228.unit")

    for st, ok in (("success", True), ("failed", False),
                   ("syntax_error", False), ("skipped", False)):
        r = wf._halt_on_uncertain([{"command": "x", "ok": ok, "status": st}])
        check(f"{st} → 不熔断（确定没生效的重试安全）", r is None, str(r))

    for st in ("unknown", "inferred_success", "dispatched_unconfirmed"):
        r = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": st}])
        check(f"★{st} → 熔断（返回 (回执, False)）",
              bool(r) and r[1] is False, str(r))

    r = wf._halt_on_uncertain([{"command": "x", "ok": True, "status": "success", "output": ""},
                               {"command": "y", "ok": False, "status": "inferred_success", "output": ""}])
    check("★混批：一条未确认即整体停手（不因为「另一条成功了」就重跑）",
          bool(r) and r[1] is False, str(r))

    r_unk = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": "unknown", "output": ""}])
    check("unknown 的措辞是「结果未知」", "结果未知" in r_unk[0], r_unk[0][:90])
    r_inf = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": "inferred_success", "output": ""}])
    check("inferred_success 的措辞是「未确认」而非「未知」",
          "未确认" in r_inf[0] and "结果未知" not in r_inf[0], r_inf[0][:90])
    check("熔断回执一律含「不会自动重试」",
          "不会自动重试" in r_unk[0] and "不会自动重试" in r_inf[0], "")


def group_static() -> None:
    print("---- 二、静态契约：判据只有一处实现 ----")
    src = (PLUGIN_DIR / "core" / "workflow.py").read_text(encoding="utf-8")
    check("★_halt_on_uncertain 只定义 1 次（不许两套真相）",
          src.count("def _halt_on_uncertain") == 1, str(src.count("def _halt_on_uncertain")))
    check("★实现循环与纠错循环都走统一判据（调用点 ≥ 2）",
          src.count("_halt_on_uncertain(exec_reports)") >= 2,
          str(src.count("_halt_on_uncertain(exec_reports)")))
    check("旧写法（两处各自 any(r.get(\"unknown\")) 熔断）已清零",
          'any(r.get("unknown") for r in exec_reports)' not in src)
    check("UNCERTAIN_STATUSES = {unknown, inferred_success, dispatched_unconfirmed}",
          UNCERTAIN_STATUSES == frozenset(
              {"unknown", "inferred_success", "dispatched_unconfirmed"}),
          str(sorted(UNCERTAIN_STATUSES)))
    cr = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")
    check("accepted 的文档写明「不构成自动重试的依据」",
          "不构成自动重试的依据" in cr)
    check("模块文档同样点明「不得当作可以重试的依据」",
          "不得把它当作「可以重试」的依据" in cr)


async def group_inferred() -> None:
    print("---- 三、靶心：inferred_success 不得触发自动重试 ----")
    rcon = StubRcon(MOD_OUT)
    agent = StubAgent([MOD_CMD])
    text, ok = await run_complex(rcon, agent)

    check("★实现器只被调用 1 次（旧实现会进下一轮重新生成命令）",
          agent.implement_calls == 1, f"calls={agent.implement_calls}")
    check("★命令只下发 1 次（重复副作用被挡住）",
          len(rcon.sent) == 1, str(rcon.sent))
    check("★纠错 Agent 完全没被调用（未确认 ≠ 普通失败）",
          agent.correct_calls == 0, f"calls={agent.correct_calls}")
    check("★回执写明「不会自动重试」", "不会自动重试" in text, text[:200])
    check("★回执写明「未确认」，不谎称成功", "未确认" in text, text[:200])
    check("★对外不得宣称成功（返回 False）", ok is False, str(ok))
    check("回执保留命令明细（不许含糊带过）", MOD_CMD in text, text[:200])


async def group_unconfirmed_msg() -> None:
    print("---- 四、dispatched_unconfirmed（边界未确认）同样不重试 ----")
    rcon = StubRcon("", boundary=False, received=True)
    agent = StubAgent(['tellraw @a {"text":"集合啦"}'])
    text, ok = await run_complex(rcon, agent)

    check("★消息命令边界未确认 → 不重发（避免重复广播）",
          agent.implement_calls == 1 and len(rcon.sent) == 1,
          f"impl={agent.implement_calls} sent={len(rcon.sent)}")
    check("回执含「未确认」", "未确认" in text, text[:200])
    check("对外不得宣称成功", ok is False, str(ok))


async def group_syntax_retryable() -> None:
    print("---- 五、对照：确定没生效 → 仍允许纠错重试（熔断不得扩大化）----")
    rcon = StubRcon(SYNTAX_ERR)
    agent = StubAgent([MOD_CMD])
    text, ok = await run_complex(rcon, agent)

    check("★语法错误 → 仍进下一轮实现（未被新熔断误拦）",
          agent.implement_calls > 1, f"impl={agent.implement_calls}")
    check("★语法错误 → 仍交给纠错 Agent（可安全重写重试）",
          agent.correct_calls >= 1, f"corr={agent.correct_calls}")
    check("语法错误路径不得谎称成功", ok is False, str(ok))


async def group_unknown_still_halts() -> None:
    print("---- 六、非幂等命令空响应 → 仍然立即熔断（v0.22.3 口径未破）----")
    rcon = StubRcon("", boundary=True, received=True)
    agent = StubAgent(["give Steve diamond 1"])
    text, ok = await run_complex(rcon, agent)

    check("★非幂等空响应 → 实现器 1 次、命令 1 条",
          agent.implement_calls == 1 and len(rcon.sent) == 1,
          f"impl={agent.implement_calls} sent={len(rcon.sent)}")
    check("回执措辞是「结果未知」", "结果未知" in text, text[:200])
    check("对外不得宣称成功", ok is False, str(ok))


async def main_async() -> None:
    await group_inferred()
    await group_unconfirmed_msg()
    await group_syntax_retryable()
    await group_unknown_still_halts()


def main() -> int:
    if not IMPORT_OK:
        print(f"[SKIP] 无法导入插件模块（缺 AstrBot 运行时）：{IMPORT_ERR}")
        return 0
    group_premise()
    group_unit()
    group_static()
    asyncio.run(main_async())

    print("=" * 60)
    if FAIL:
        print(f"❌ 失败 {len(FAIL)} 项：")
        for d in FAIL:
            print("   -", d)
        return 1
    print("✅ 全部通过：结果不确定（unknown / inferred_success / dispatched_unconfirmed）"
          "一律停手不重试；确定没生效（failed / syntax_error）仍可纠错重试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
