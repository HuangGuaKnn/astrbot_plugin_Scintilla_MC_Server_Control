# -*- coding: utf-8 -*-
"""v0.24.3 回归 · N01：重试许可必须是**结构化证据**，不是状态字面。

背景（GPT v0.24.2 复核 N01）
============================

自定义物品名可以就叫 ``Unknown command. Type "/help" for help.`` ——

    Gave 1 [Unknown command. Type "/help" for help.] to Steve      ← 这是**真成功**

旧判据是整段子串扫描，于是这条回执被判 ``failed``；而 ``failed`` 在旧契约里是
「确定没生效、重发安全」那一档 → 工作流进下一轮，同一条命令被发两次
（离线实证：同一条命令下发 2 次，agent_calls=2）。

本用例守三件事
==============

1. **显示名不参与错误扫描**：判据按**行首锚定**，成功回执里的自定义名不再翻盘；
2. **成功与错误同现**的多行回执 → ``unknown``：既不谎报成功，也不许自动重发；
3. **工作流只认结构化重试许可**（``retry_safe``）：缺字段一律停手问人 ——
   而且不是只测判据函数，还真的跑一遍 ``_exec_commands``，实证「只下发一次」。

fail-closed 声明
================

缺运行依赖（import 失败）判 **FAIL**，不判 SKIP：
「导入失败即跳过」的用例在门禁里等于**永远绿**，那是 N11 同源的问题。
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
    from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (
        classify_command_output,
        is_not_run_failure_output,
        is_unknown_command_output,
    )
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


#: 真成功，但**显示名**正好是服务端的「命令不存在」原文
CUSTOM_NAME_OUT = 'Gave 1 [Unknown command. Type "/help" for help.] to Steve'
#: 纯「命令不存在」：服务端独占一行的提示
STANDALONE_OUT = 'Unknown command. Type "/help" for help.'
#: 成功与错误同现的多行混合回执
MIXED_OUT = 'Gave 1 [Diamond] to Steve\nUnknown command. Type "/help" for help.'
#: 服务端**动手之前**就拒绝（没有副作用）
NOT_RUN_OUT = "No player was found"
#: 运行期错误词：只说明结果不是成功，**推不出**没执行
RUNTIME_OUT = "An unexpected error occurred"


# ===================== 一、文本层：显示名不参与错误扫描 =====================

def group_text() -> None:
    print("---- 一、文本层：行首锚定 + 结构化「没执行」证据 ----")

    check("★成功回执里的自定义显示名**不**算「命令不存在」",
          is_unknown_command_output(CUSTOM_NAME_OUT) is False, "被整段扫描命中")

    r = classify_command_output("give Steve diamond 1", CUSTOM_NAME_OUT)
    check("★该回执判 success（旧判据在这里给 failed → 触发重发）",
          r.status == "success", f"{r.status} / {r.reason}")
    check("该结果不可重试、也**没有**「确定没生效」证据",
          r.retryable is False and r.confirmed_not_run is False,
          f"retryable={r.retryable} confirmed_not_run={r.confirmed_not_run}")

    check("纯「命令不存在」仍被识别", is_unknown_command_output(STANDALONE_OUT) is True)
    r2 = classify_command_output("modcmd:zap Steve", STANDALONE_OUT)
    check("★纯「命令不存在」判 failed，且带结构化证据（命令都没解析出来 = 没生效）",
          r2.status == "failed" and r2.confirmed_not_run is True,
          f"{r2.status} confirmed_not_run={r2.confirmed_not_run}")
    check("该档 retryable=False（重写同一条没意义，但不许当成「结果未知」）",
          r2.retryable is False, str(r2.retryable))

    r3 = classify_command_output("give Steve diamond 1", MIXED_OUT)
    check("★成功 + 「命令不存在」同现的多行回执 → unknown（不谎报成功）",
          r3.status == "unknown", f"{r3.status} / {r3.reason}")
    check("★混合回执**没有**「确定没生效」证据（否则就是重复副作用入口）",
          r3.confirmed_not_run is False and r3.retryable is False,
          f"confirmed_not_run={r3.confirmed_not_run} retryable={r3.retryable}")

    check("「动手前就拒绝」的短语被判为确定没生效", is_not_run_failure_output(NOT_RUN_OUT) is True)
    check("运行期错误词**不**算确定没生效（N01 靶心）",
          is_not_run_failure_output(RUNTIME_OUT) is False)
    r4 = classify_command_output("give Steve diamond 1", RUNTIME_OUT)
    check("★运行期错误 → failed 但 confirmed_not_run=False（与语法错误分道）",
          r4.status == "failed" and r4.confirmed_not_run is False,
          f"{r4.status} confirmed_not_run={r4.confirmed_not_run}")
    r5 = classify_command_output("give Steve diamond 1", NOT_RUN_OUT)
    check("未生效短语 → failed + confirmed_not_run=True",
          r5.status == "failed" and r5.confirmed_not_run is True,
          f"{r5.status} confirmed_not_run={r5.confirmed_not_run}")


# ===================== 二、熔断判据：只认结构化证据 =====================

def group_halt() -> None:
    print("---- 二、熔断判据：status 字面不再构成重试许可 ----")
    wf = MCWorkflow.__new__(MCWorkflow)

    r = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": "failed"}])
    check("★failed **无**结构化证据 → 熔断（旧契约在这里放行重发）",
          bool(r) and r[1] is False, str(r))
    check("该档措辞不许写成「结果未知」（事实是「不能证明没生效」）",
          bool(r) and "结果未知" not in r[0] and "没有证据" in r[0], (r or ("",))[0][:110])

    r2 = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": "failed",
                                 "retry_safe": True}])
    check("failed + retry_safe=True → 不熔断（有证据才放行）", r2 is None, str(r2))

    r3 = wf._halt_on_uncertain([{"command": "x", "ok": False, "status": "skipped"}])
    check("skipped（根本没发送）→ 不熔断", r3 is None, str(r3))

    r4 = wf._halt_on_uncertain([{"command": "x", "ok": True}])
    check("★{ok:True} 但缺 status → 仍熔断（fail-closed 未破）",
          bool(r4) and r4[1] is False, str(r4))

    r5 = wf._halt_on_uncertain([{"command": "a", "ok": False, "status": "failed", "retry_safe": True},
                                {"command": "b", "ok": False, "status": "failed"}])
    check("★混批：只要有一条没证据就整体停手", bool(r5) and r5[1] is False, str(r5))


# ===================== 三、执行循环实证：只发一次 =====================

class StubRcon:
    """假 RCON：记录**真正下发过**的命令，并可指定第 N 次抛什么异常。"""

    def __init__(self, out: str, *, boundary: bool = True, received: bool = True,
                 fail_at=None, exc=None):
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

    def _cfg(self, key, default=None):
        return default


def make_workflow(rcon) -> MCWorkflow:
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon)
    wf.logger = logging.getLogger("test.v0243")
    wf._apply_player_to_commands = lambda cmds, player: cmds
    return wf


async def one_exec(out: str, cmd: str = "give Steve diamond 1"):
    rcon = StubRcon(out)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands([{"command": cmd}], "Steve", ["Steve"])
    return wf, rcon, reports


async def group_end_to_end() -> None:
    print("---- 三、执行循环实证：同一命令只下发一次 ----")

    try:
        wf, rcon, rep = await one_exec(CUSTOM_NAME_OUT)
    except Exception as e:  # noqa: BLE001 —— 桩环境不齐也算失败，不静默跳过
        check("执行循环可跑（桩环境下 _exec_commands 不抛异常）", False,
              f"{type(e).__name__}: {e}")
        return

    check("★自定义显示名的成功回执：判 ok=True", rep[0]["ok"] is True, str(rep[0]))
    check("★只下发一次（旧判据在这里下发两次）", len(rcon.sent) == 1, str(rcon.sent))
    check("该报告不熔断（已成功）", wf._halt_on_uncertain(rep) is None)

    wf2, rcon2, rep2 = await one_exec(MIXED_OUT)
    check("★混合回执 → status=unknown（不谎报成功）", rep2[0]["status"] == "unknown", str(rep2[0]))
    check("★混合回执同样只下发一次", len(rcon2.sent) == 1, str(rcon2.sent))
    halt2 = wf2._halt_on_uncertain(rep2)
    check("混合回执必须熔断（既不重发、也不宣称成功）",
          bool(halt2) and halt2[1] is False, str(halt2)[:140])

    wf3, rcon3, rep3 = await one_exec(STANDALONE_OUT)
    check("★纯「命令不存在」→ failed + retry_safe=True（结构化证据）",
          rep3[0]["status"] == "failed" and rep3[0].get("retry_safe") is True, str(rep3[0]))
    check("有证据这一档不熔断（可以安全重写后重试）",
          wf3._halt_on_uncertain(rep3) is None)
    check("命令不存在时也只下发一次", len(rcon3.sent) == 1, str(rcon3.sent))

    wf4, rcon4, rep4 = await one_exec(RUNTIME_OUT)
    check("★运行期错误 → failed 且**无** retry_safe（N01 靶心）",
          rep4[0]["status"] == "failed" and not rep4[0].get("retry_safe"), str(rep4[0]))
    halt4 = wf4._halt_on_uncertain(rep4)
    check("★无证据的 failed 必须熔断（旧契约在这一档自动重发）",
          bool(halt4) and halt4[1] is False, str(halt4)[:140])


# ===================== 四、牙齿：口径不许被改回去 =====================

def group_teeth() -> None:
    print("---- 四、牙齿：静态口径不许被改回子串扫描 ----")
    cr = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")
    wf = (PLUGIN_DIR / "core" / "workflow.py").read_text(encoding="utf-8")

    check("★「命令不存在」不再按整段子串扫描",
          "any(m in low for m in _UNKNOWN_COMMAND_MARKERS)" not in cr)
    check("★改走行首锚定正则", "_UNKNOWN_COMMAND_LINE_RE.search(output)" in cr)
    check("★「确定没生效」判据有独立实现（区分运行期错误词）",
          cr.count("def is_not_run_failure_output") == 1)
    check("★熔断判据只认结构化重试许可",
          "if not self._retry_safe_report(r)" in wf and
          wf.count("def _retry_safe_report") == 1)
    check("★执行报告的写入点都带 retry_safe（≥4 处）",
          wf.count('"retry_safe"') >= 4, str(wf.count('"retry_safe"')))
    check("执行循环仍把 confirmed_not_run 带进报告",
          'getattr(res, "confirmed_not_run", False)' in wf)
    check("v0.22.8 口径未破：_halt_on_uncertain 仍只 1 次实现、≥2 处调用",
          wf.count("def _halt_on_uncertain") == 1 and
          wf.count("_halt_on_uncertain(exec_reports)") >= 2)


async def main_async() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · N01 执行事实与重试边界（结构化重试许可）")
    print("=" * 78)
    if not IMPORT_OK:
        # fail-closed：导入失败就是失败，不许变成「跳过 = 通过」
        check(f"插件可导入（缺运行依赖必须判 FAIL 而不是 SKIP）", False, IMPORT_ERR)
        print("\n失败项：" + " / ".join(FAIL))
        return 1
    group_text()
    group_halt()
    await group_end_to_end()
    group_teeth()
    print("\n================ 汇总 ================")
    print("失败 %d 项" % len(FAIL))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
