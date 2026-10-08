# -*- coding: utf-8 -*-
"""v0.24.2 回归：**执行事实**统一（GPT 全面复核 F01 + F02）。

F01｜工作流把「发送后的断连」判成失败 → 重复发送非幂等命令
  · 病灶：`core/workflow.py` 的通用异常分支写 `status="failed"`，而 `failed` 在
    `_halt_on_uncertain` 里属于「确定没生效、重发安全」那一档 → 通信中断被当成可重试。
    同文件另一处循环（以及 `main.py:_exec_checked`）早就按阶段分、把这类记 `unknown`。
  · 修法：`RconError` 按构造时标注的阶段判（`command_may_have_run()`）：建连/认证 = failed
    （可继续），发送/读取/协议 = unknown + 停批；**一切内部异常同样 unknown**（本地报错
    也不能证明命令没发出去）。

F02｜成功回执里出现 `expected` / `unknown item` 就被判「可安全重发」
  · 病灶：语法词按裸词匹配、短语按子串匹配 —— 玩家名叫 `Expected`、物品展示名叫
    `Unknown Item` 时，真成功被翻成 `syntax_error, retryable=True`。
  · 修法：语法词与那批短语改**行首锚定**；成功与错误文本同时出现（多行混合）按
    `unknown` 保守处理，不许宣称「从未执行」。

判据要点：命令**是否真的只下发了一条**（不是只看状态字段）；熔断是否真的会发生。
跑法：<python> tests\test_v0242_execution_facts.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []

add_sys_paths()
try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (
        classify_command_output,
    )
    from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import RconError, RconTimeoutError
    from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow
    IMPORT_OK = True
    IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    IMPORT_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


REAL_OK = "Gave 1 [Diamond Sword] to Steve"
REAL_SYNTAX = "Expected whitespace to end one argument, but found trailing data"


class StubRcon:
    """假 RCON：可指定第 N 次调用抛什么异常，并记录**真正下发过**的命令。"""

    def __init__(self, out=REAL_OK, *, boundary=True, received=True, fail_at=None, exc=None):
        self._out = out
        self.last_boundary_confirmed = boundary
        self.last_response_received = received
        self._fail_at = fail_at
        self._exc = exc
        self.sent: list[str] = []

    async def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        if self._exc is not None and len(self.sent) - 1 == self._fail_at:
            raise self._exc
        return self._out


class StubPlugin:
    def __init__(self, rcon):
        self._rcon = rcon
        self.feedbacks: list[str] = []

    async def _get_rcon(self):
        return self._rcon

    async def _send_feedback(self, rcon, tip):
        self.feedbacks.append(tip)


def make_workflow(rcon) -> MCWorkflow:
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon)
    wf.logger = logging.getLogger("test.v0242")
    wf._apply_player_to_commands = lambda cmds, player: cmds
    return wf


CMDS = [{"command": "give Steve diamond 1"},
        {"command": "give Steve netherite_ingot 1"},
        {"command": "say done"}]


async def run_exec(cmds, player, online, *, fail_at=None, exc=None, out=REAL_OK):
    """跑一次 _exec_commands；撞上本测试没搭的环境（例如 B5 give 改道）就返回 None。"""
    rcon = StubRcon(out, fail_at=fail_at, exc=exc)
    try:
        reports = await wf_exec(rcon, cmds, player, online)
    except AttributeError as e:
        return None, rcon, e
    return reports, rcon, None


async def wf_exec(rcon, cmds, player, online):
    wf = make_workflow(rcon)
    return await wf._exec_commands(cmds, player, online)


# ===================== 一、F02：纯函数层 =====================
def group_f02() -> None:
    print("---- 一、F02：合法成功回执不得被当成可重发的语法错误 ----")
    ok_cases = (
        ("give Expected diamond 1", "Gave 1 [Diamond] to Expected", "玩家名 Expected"),
        ("give Steve minecraft:foo 1", "Gave 1 [Unknown Item] to Steve", "物品展示名 Unknown Item"),
        ("give Steve diamond 1", "Gave 1 [Diamond] to Error", "玩家名 Error"),
    )
    for cmd, out, tag in ok_cases:
        r = classify_command_output(cmd, out, boundary_confirmed=True, response_received=True)
        check(f"★{tag}：判 success 且不可重试", r.status == "success" and not getattr(r, "retryable", False),
              f"status={r.status} retryable={getattr(r, 'retryable', None)}")

    for cmd, out, tag in (
        ("give Steve diamond 1", REAL_SYNTAX, "真解析错误（行首 Expected）"),
        ("give Steve foo:bar 1", "Unknown item 'foo:bar'", "真解析错误（行首 Unknown item）"),
        ("give Steve foo:bar 1", "Unknown or incomplete command, see below for error\n/foo <--[HERE]", "未知/不完整命令"),
    ):
        r = classify_command_output(cmd, out, boundary_confirmed=True, response_received=True)
        check(f"{tag}：仍判 syntax_error 且可重试", r.status == "syntax_error" and r.retryable is True,
              f"status={r.status} retryable={getattr(r, 'retryable', None)}")

    mixed = REAL_OK + "\n" + REAL_SYNTAX
    r = classify_command_output("give Steve diamond 1", mixed,
                                boundary_confirmed=True, response_received=True)
    check("★混合回执（成功 + 解析错误）：判 unknown 且不可重试（不宣称从未执行）",
          r.status == "unknown" and not getattr(r, "retryable", False),
          f"status={r.status} retryable={getattr(r, 'retryable', None)}")
    r = classify_command_output("say hi", "An unexpected error occurred",
                                boundary_confirmed=True, response_received=True)
    check("运行期错误（unexpected）不落 syntax_error", r.status != "syntax_error", r.status)


# ===================== 二、F01：工作流传输层 =====================
async def group_f01() -> None:
    print("---- 二、F01：发送后不确定 → unknown + 停批（不再判 failed 可重发）----")
    online = ["Steve"]

    # ① 发送阶段失败（命令可能已到达）→ 只下发 1 条，后续 skipped，且熔断判定为「不确定」
    reports, rcon, err = await run_exec(CMDS, "Steve", online, fail_at=0,
                                        exc=RconError("模拟：写失败", phase=RconError.PHASE_SEND))
    if reports is None:
        skip(f"传输层用例（环境未搭全）：{err}")
    else:
        check("★send 阶段异常 → 本条 unknown", reports[0]["status"] == "unknown"
              and reports[0].get("unknown") is True, str(reports[0]))
        check("★send 阶段异常 → **只下发一条**，其余 skipped", len(rcon.sent) == 1
              and all(r["status"] == "skipped" for r in reports[1:]) and len(reports) == 3,
              f"sent={rcon.sent} reports={[r['status'] for r in reports]}")
        wf = make_workflow(rcon)
        check("★send 阶段异常 → 熔断生效（_halt_on_uncertain 不再是 None）",
              wf._halt_on_uncertain(reports) is not None)

    # ② 读取阶段失败（第 2 条）：第 1 条成功、第 2 条 unknown、第 3 条未发
    reports, rcon, err = await run_exec(CMDS, "Steve", online, fail_at=1,
                                        exc=RconError("模拟：读失败", phase=RconError.PHASE_READ))
    if reports is None:
        skip(f"读取阶段用例：{err}")
    else:
        check("read 阶段异常：前一条成功、本条 unknown、后一条 skipped",
              reports[0]["status"] == "success" and reports[1]["status"] == "unknown"
              and reports[2]["status"] == "skipped" and len(rcon.sent) == 2,
              f"sent={rcon.sent} reports={[r['status'] for r in reports]}")

    # ③ 建连阶段失败（命令根本没发出去）→ 判 failed 安全，后续**照常下发**
    reports, rcon, err = await run_exec(CMDS, "Steve", online, fail_at=0,
                                        exc=RconError("模拟：连不上", phase=RconError.PHASE_CONNECT))
    if reports is None:
        skip(f"建连阶段用例：{err}")
    else:
        check("connect 阶段异常 → failed（可安全重试档）", reports[0]["status"] == "failed",
              str(reports[0]))
        check("connect 阶段异常 → 后续命令照常下发（不扩大熔断）",
              len(rcon.sent) == 3, f"sent={rcon.sent}")

    # ④ 超时：既有口径不许退化
    reports, rcon, err = await run_exec(CMDS, "Steve", online, fail_at=0,
                                        exc=RconTimeoutError("模拟超时"))
    if reports is None:
        skip(f"超时用例：{err}")
    else:
        check("超时 → unknown + 只下发一条", reports[0]["status"] == "unknown" and len(rcon.sent) == 1,
              f"sent={rcon.sent}")

    # ⑤ 内部异常（非 RCON）：本地报错也不能证明命令没发出去 → 保守 unknown + 停批
    reports, rcon, err = await run_exec(CMDS, "Steve", online, fail_at=0, exc=ValueError("模拟内部异常"))
    if reports is None:
        skip(f"内部异常用例：{err}")
    else:
        check("★内部异常 → unknown + 停批（旧实现判 failed → 会被自动重发）",
              reports[0]["status"] == "unknown" and len(rcon.sent) == 1
              and reports[1]["status"] == "skipped",
              f"sent={rcon.sent} reports={[r['status'] for r in reports]}")


# ===================== 三、守卫有牙 =====================
def group_teeth() -> None:
    print("---- 三、守卫有牙 ----")
    src = (Path(__file__).resolve().parents[1] / "core" / "workflow.py").read_text(encoding="utf-8")
    check("旧写法（通用异常判 failed）已清零",
          '"status": "failed", "output": str(e)[:300]' not in src)
    check("新增统一收尾 _unknown_and_skip 存在", "_unknown_and_skip" in src)
    check("RconError 已按阶段分（command_may_have_run）", "command_may_have_run" in src)
    cr = (Path(__file__).resolve().parents[1] / "core" / "command_result.py").read_text(encoding="utf-8")
    check("语法词已改行首锚定", r'^\s*expected\b' in cr)
    check("行首短语表存在（unknown item 等）", "_SYNTAX_LINE_RE" in cr)


async def main_async() -> int:
    if not IMPORT_OK:
        print(f"[SKIP] 找不到 AstrBot 运行时：{IMPORT_ERR}")
        return 0
    print("=" * 78)
    print("v0.24.2 回归 · 执行事实统一（F01 传输层 + F02 文本层）")
    print("=" * 78)
    group_f02()
    await group_f01()
    group_teeth()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


PASS_N: list[str] = []
_orig_check = check


def check(desc, ok, detail=""):        # noqa: F811 —— 统计通过数用
    if ok:
        PASS_N.append(desc)
    _orig_check(desc, ok, detail)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
