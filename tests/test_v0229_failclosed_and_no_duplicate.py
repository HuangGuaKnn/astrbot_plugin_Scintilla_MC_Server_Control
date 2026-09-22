# -*- coding: utf-8 -*-
"""v0.22.9 回归：状态判定 fail-closed ＋ 同一任务内不重发已生效命令。

两条 P1（来自 GPT 对 v0.22.8 的复核裁决）：

P1-a｜``_status_of`` 缺 ``status`` 时按 ``ok`` 回推 = 「默认成功」
  · 只要有一条执行报告漏填 ``status``、又恰好带 ``ok=True``，它就能绕过
    ``_halt_on_uncertain`` 的熔断 —— 与 v0.22.5 假成功事故同一种坏默认值。
  · 改后：只认 ``command_result.KNOWN_STATUSES``，其余（缺字段 / 拼写错 /
    将来新状态忘了登记）一律 ``unknown`` → 熔断（fail-closed）。
  · 同源要求：``_fmt_results`` 不许再写第二份状态回推。

P1-b｜命令全部 confirmed success、但实现器自评「没做完」→ 仍进下一轮
  · 实现器重新生成同一批命令 → 原实现照发 → **重复副作用**。
  · 改后：本任务内**已确认成功**的命令一律不重发；整批都是重复 → 停手问人
    （既不自动重试，也不悄悄跳过）；部分重复 → 只发差额，并如实记账。

跑法：<python> tests\\test_v0229_failclosed_and_no_duplicate.py
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
        KNOWN_STATUSES,
        STATUS_LABEL,
        classify_command_output,
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


#: 能判成 ``success`` 的真实回显（命中通用成功标记 "gave "）
OK_OUT = "Gave 1 [Diamond Sword] to Steve"
#: 模组回显：非空、无错误标记、无成功证据 → ``inferred_success``（v0.22.8 熔断口径的对照）
MOD_CMD = "tacz give Steve modern_kinetic_gun 1"
MOD_OUT = "[TACZ] 已为 Steve 装配 Modern Kinetic Gun（模组回显，无原版成功标记）"
SYNTAX_ERR = "Expected whitespace to end one argument, but found trailing data"

CMD_A = "give Steve diamond 1"
CMD_B = "give Steve netherite_ingot 1"


class StubRcon:
    def __init__(self, out=OK_OUT, *, boundary=True, received=True):
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
    """可编排多轮的假实现器：每轮给不同命令，自评 success 也可逐轮指定。"""

    def __init__(self, rounds, success_flags=None):
        self.rounds = [list(r) for r in rounds]
        self.success_flags = list(success_flags) if success_flags else []
        self.implement_calls = 0
        self.correct_calls = 0
        self.seen_failures: list[str] = []

    @staticmethod
    def _at(seq, i, default):
        if not seq:
            return default
        return seq[i] if i < len(seq) else seq[-1]

    async def judge(self, request, kb_text, umo=None):
        return {"sufficient": True}

    async def engineer(self, request, kb_text, dict_text, umo=None):
        return {"templates": []}

    async def implement(self, request, player, kb_text, failures,
                        retry_hint, umo=None, online_players=None):
        i = self.implement_calls
        self.implement_calls += 1
        self.seen_failures.append(failures or "")
        return {
            "commands": [{"command": c} for c in self._at(self.rounds, i, [])],
            "success": self._at(self.success_flags, i, True),
            "reasoning": "stub 实现器",
        }

    async def correct(self, request, kb_text, failures, kb_text2=None, umo=None):
        self.correct_calls += 1
        return {"corrections": [], "retry_hint": "换个写法"}


async def _fake_resolve(player, request):
    """隔离玩家名解析：本测试只验「状态判定 + 重发语义」两件事。"""
    return ((player or "Steve").strip(), [])


def make_workflow(rcon, agent) -> MCWorkflow:
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon)
    wf.logger = logging.getLogger("test.v0229")
    wf.agent = agent
    wf._resolve_player = _fake_resolve
    return wf


async def run_complex(rcon, agent, request="给 Steve 发一把钻石剑"):
    wf = make_workflow(rcon, agent)
    return await wf._run_complex(request, "Steve", "")


# ===================== 用例 =====================

def group_premise() -> None:
    print("---- 零、判定前提：样本回显确实落 success / inferred_success ----")
    r_ok = classify_command_output(CMD_A, OK_OUT,
                                   boundary_confirmed=True, response_received=True)
    check("★成功样本命中通用成功标记 → success",
          r_ok.status == "success" and r_ok.ok is True, r_ok.status)
    r_mod = classify_command_output(MOD_CMD, MOD_OUT,
                                    boundary_confirmed=True, response_received=True)
    check("模组样本仍是 inferred_success（v0.22.8 熔断口径的对照）",
          r_mod.status == "inferred_success", r_mod.status)


def group_fail_closed() -> None:
    print("---- 一、P1-a：_status_of 缺字段 / 未知值一律 unknown ----")
    for desc, rep in (
        ("缺 status + ok=True（旧实现回推成 success 的靶心）", {"ok": True}),
        ("缺 status + ok=False", {"ok": False}),
        ("status 为空串", {"status": "", "ok": True}),
        ("status=None", {"status": None, "ok": True}),
        ("status 拼写/大小写错（SUCCESS）", {"status": "SUCCESS", "ok": True}),
        ("status 是从未见过的状态（将来忘了登记）", {"status": "partially_applied", "ok": True}),
    ):
        got = MCWorkflow._status_of(dict(rep))
        check(f"★{desc} → unknown", got == "unknown", got)

    for st in sorted(KNOWN_STATUSES):
        check(f"已知状态 {st} 原样返回",
              MCWorkflow._status_of({"status": st}) == st)

    check("★KNOWN_STATUSES 就是标签表的键（单一真相）",
          KNOWN_STATUSES == frozenset(STATUS_LABEL),
          str(sorted(KNOWN_STATUSES)))
    check("★所有「结果不确定」状态都在已知集合里（不会自己把自己熔断掉）",
          UNCERTAIN_STATUSES <= KNOWN_STATUSES,
          str(sorted(UNCERTAIN_STATUSES - KNOWN_STATUSES)))


def group_fail_closed_effect() -> None:
    print("---- 二、P1-a 靶心：缺 status 的报告不得再绕过熔断 ----")
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(StubRcon())
    wf.logger = logging.getLogger("test.v0229.unit")

    r = wf._halt_on_uncertain([{"command": "x", "ok": True}])
    check("★{ok:True} 且无 status → 熔断（旧实现这里会放行）",
          bool(r) and r[1] is False, str(r))
    check("★熔断回执含「不会自动重试」", bool(r) and "不会自动重试" in r[0], str(r)[:90])

    r2 = wf._halt_on_uncertain([{"command": "x", "ok": True, "status": "success"}])
    check("对照：status=success 仍不熔断（fail-closed 没扩大化）", r2 is None, str(r2))

    fmt = MCWorkflow._fmt_results([{"command": "x", "ok": True}])
    check("★回执渲染与判据同源：缺 status 显示「未知」，不再显示 OK",
          "[未知]" in fmt and "[OK]" not in fmt, fmt)
    fmt_ok = MCWorkflow._fmt_results([{"command": "y", "ok": True, "status": "success"}])
    check("对照：status=success 的回执仍是 [OK]", "[OK]" in fmt_ok, fmt_ok)


def group_static() -> None:
    print("---- 三、静态契约：状态回推只有一份实现 ----")
    src = (PLUGIN_DIR / "core" / "workflow.py").read_text(encoding="utf-8")
    cr = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")

    check("★_status_of 只定义 1 次", src.count("def _status_of") == 1,
          str(src.count("def _status_of")))
    check("★按 ok 回推状态的旧写法已清零（0 处）",
          'r.get("status") or ("success"' not in src,
          str(src.count('r.get("status") or ("success"')))
    check("★回执渲染调用 _status_of（与熔断判据同源）",
          src.count("MCWorkflow._status_of(r)") >= 1, str(src.count("MCWorkflow._status_of(r)")))
    check("KNOWN_STATUSES 只在 command_result.py 定义 1 次",
          cr.count("KNOWN_STATUSES = frozenset(STATUS_LABEL)") == 1 and
          "KNOWN_STATUSES = frozenset(STATUS_LABEL)" not in src)
    check("v0.22.8 口径未破：_halt_on_uncertain 仍只 1 次实现、2 处调用",
          src.count("def _halt_on_uncertain") == 1 and
          src.count("_halt_on_uncertain(exec_reports)") >= 2)
    check("展示标签表与判据分离（_tag 只影响措辞，不参与判定）",
          '"success": "OK"' in src and "_status_of" in src)


def group_norm() -> None:
    print("---- 四、命令规范化：只折叠空白与前导斜杠 ----")
    check("前导斜杠 + 多空格 → 折叠成同一条",
          MCWorkflow._norm_cmd("  /give   Steve  diamond 1 ") == "give Steve diamond 1",
          MCWorkflow._norm_cmd("  /give   Steve  diamond 1 "))
    check("★大小写保留（宁可漏判，也不误拦语义不同的命令）",
          MCWorkflow._norm_cmd("Give Steve Diamond") != MCWorkflow._norm_cmd("give steve diamond"))
    check("空命令归一为空串（不参与比对）", MCWorkflow._norm_cmd("   ") == "")


async def group_reissue_halted() -> None:
    print("---- 五、P1-b 靶心：自评未完成 → 重发同一批命令必须被拦 ----")
    rcon = StubRcon(OK_OUT)
    agent = StubAgent([[CMD_A], [CMD_A]], success_flags=[False, True])
    text, ok = await run_complex(rcon, agent)

    check("★命令只下发 1 次（旧实现会照发第二遍 → 重复副作用）",
          len(rcon.sent) == 1, str(rcon.sent))
    check("实现器确实进了第 2 轮（说明拦的是「重发」而不是「不迭代」）",
          agent.implement_calls == 2, f"calls={agent.implement_calls}")
    check("★回执写明「已生效·未重发」并列出被拦命令",
          "已生效·未重发" in text and CMD_A in text, text[:220])
    check("★回执给出人工裁决指引（不许悄悄跳过）",
          "请先在游戏内确认实际结果" in text, text[:260])
    check("★对外不得宣称成功", ok is False, str(ok))
    check("这条路径不该调用纠错 Agent", agent.correct_calls == 0, f"corr={agent.correct_calls}")


async def group_reissue_with_new() -> None:
    print("---- 六、混批：重复的被跳过，新命令照常执行 ----")
    rcon = StubRcon(OK_OUT)
    agent = StubAgent([[CMD_A], [CMD_A, CMD_B]], success_flags=[False, True])
    text, ok = await run_complex(rcon, agent)

    check("★已生效命令不重发、新命令照发（sent = A、B 各一次）",
          rcon.sent == [CMD_A, CMD_B], str(rcon.sent))
    check("★成功计数只算本轮真实执行的命令（1/1，不把跳过算成生效）",
          "1/1" in text, text[:160])
    check("重复被跳过不改变成功结论", ok is True, str(ok))
    check("回执不误报「未重发」停手文案", "已生效·未重发" not in text, text[:160])


async def group_slash_duplicate() -> None:
    print("---- 七、带斜杠/多空格的「同一条命令」同样被拦 ----")
    rcon = StubRcon(OK_OUT)
    agent = StubAgent([[CMD_A], ["  /give   Steve   diamond   1  "]], success_flags=[False, True])
    text, ok = await run_complex(rcon, agent)

    check("★规范化后判为同一条 → 不重发", len(rcon.sent) == 1, str(rcon.sent))
    check("停手文案出现", "已生效·未重发" in text and ok is False, text[:200])


async def group_fresh_round() -> None:
    print("---- 八、对照：下一轮给的是**新**命令 → 迭代不受影响 ----")
    rcon = StubRcon(OK_OUT)
    agent = StubAgent([[CMD_A], [CMD_B]], success_flags=[False, True])
    text, ok = await run_complex(rcon, agent)

    check("★两条不同命令各发一次（守门没误杀正常迭代）",
          rcon.sent == [CMD_A, CMD_B], str(rcon.sent))
    check("最终仍能正常宣告成功", ok is True and "任务执行成功" in text, text[:160])
    check("回执不含停手文案", "已生效·未重发" not in text)


async def group_fact_note() -> None:
    print("---- 九、既成事实提示：下一轮必须告诉实现器别重复 ----")
    rcon = StubRcon(OK_OUT)
    agent = StubAgent([[CMD_A], [CMD_B]], success_flags=[False, True])
    await run_complex(rcon, agent)

    note = agent.seen_failures[1] if len(agent.seen_failures) > 1 else ""
    check("★第 2 轮上下文含「禁止重复生成」的既成事实提示",
          "禁止重复生成" in note, note[:220])
    check("★提示里点名了已生效的那条命令", CMD_A in note, note[:220])
    check("第 1 轮不该带这种提示（那时还没有已生效命令）",
          "禁止重复生成" not in (agent.seen_failures[0] if agent.seen_failures else ""),
          (agent.seen_failures[0][:120] if agent.seen_failures else ""))


async def group_v0228_intact() -> None:
    print("---- 十、v0.22.8 口径未被破坏 ----")
    rcon = StubRcon(MOD_OUT)
    agent = StubAgent([[MOD_CMD]])
    text, ok = await run_complex(rcon, agent)
    check("★inferred_success 仍立即熔断（实现器 1 次、命令 1 条）",
          agent.implement_calls == 1 and len(rcon.sent) == 1,
          f"impl={agent.implement_calls} sent={len(rcon.sent)}")
    check("对外不得宣称成功", ok is False, str(ok))

    rcon2 = StubRcon(SYNTAX_ERR)
    agent2 = StubAgent([[MOD_CMD]])
    await run_complex(rcon2, agent2)
    check("★语法错误仍允许纠错重试（熔断没扩大化）",
          agent2.implement_calls > 1 and agent2.correct_calls >= 1,
          f"impl={agent2.implement_calls} corr={agent2.correct_calls}")

    rcon3 = StubRcon("", boundary=True, received=True)
    agent3 = StubAgent([["give Steve diamond 1"]])
    text3, ok3 = await run_complex(rcon3, agent3)
    check("★非幂等空响应仍熔断（v0.22.3 口径未破）",
          agent3.implement_calls == 1 and len(rcon3.sent) == 1 and ok3 is False,
          f"impl={agent3.implement_calls} sent={len(rcon3.sent)} ok={ok3}")


async def main_async() -> None:
    await group_reissue_halted()
    await group_reissue_with_new()
    await group_slash_duplicate()
    await group_fresh_round()
    await group_fact_note()
    await group_v0228_intact()


def main() -> None:
    print("=" * 68)
    print("v0.22.9：状态判定 fail-closed ＋ 不重发已生效命令")
    print("=" * 68)
    if not IMPORT_OK:
        print("[FAIL] 导入失败：", IMPORT_ERR)
        sys.exit(1)

    group_premise()
    group_fail_closed()
    group_fail_closed_effect()
    group_static()
    group_norm()
    asyncio.run(main_async())

    print("=" * 68)
    if FAIL:
        print(f"结果：{len(FAIL)} 项未通过")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("结果：全部通过 ✅")


if __name__ == "__main__":
    main()
