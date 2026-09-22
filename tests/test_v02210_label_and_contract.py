# -*- coding: utf-8 -*-
"""v0.22.10 回归：展示标签单一真相 ＋ 记账 fail-closed（等长校验 / 判据同源）。

两条来自 GPT 对 v0.22.9 的核验裁决（取舍 5、取舍 6）：

取舍 5｜展示标签表有两份
  · ``core/workflow.py::_fmt_results`` 自带一份 ``_tag``（``OK`` / ``FAIL`` /
    ``未知``…），与 ``core/command_result.py::STATUS_LABEL``（``成功`` /
    ``失败`` / ``结果未知``…）是两套措辞 —— 同一份判定在两处显示成不同字样。
  · 改后：全仓只留 ``STATUS_LABEL`` 一份；回执、单命令明细、日志一起变。

取舍 6｜``zip(commands, exec_reports)`` 缺显式长度校验
  · 旧实现靠 ``zip()`` 隐式配对，长度不一致时**静默截断**：多出来的命令
    既没记账、也没人发现。
  · 改后：结构对不上就 fail-closed —— 不记账、不猜、停手问人。

附带收口（皮莉卡自曝，GPT 未点名）｜记账判据同源
  · 旧实现按 ``r.get("ok")`` 记账，是**第二套成功口径**：报告漏填 ``status``
    又带 ``ok=True``（v0.22.9 刚在熔断判据里堵掉的坏形状）会被记成「已生效」
    → 下一轮同一条命令被当重复**跳过** → 主人要的东西少执行一次。
  · 改后：记账只认 ``_status_of(r) == "success"``，与熔断判据同一双眼睛。

跑法：<python> tests\\test_v02210_label_and_contract.py
"""
from __future__ import annotations

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


def _label_of(report: dict) -> str:
    """从回执行里取出方括号标签（形如 ``[成功] give … → …``）。"""
    line = MCWorkflow._fmt_results([report]).splitlines()[0]
    return line.split("]", 1)[0][1:]


# ===================== 一、展示标签单一真相 =====================

def group_label() -> None:
    print("---- 一、展示标签单一真相（取舍 5）----")

    check("success → [成功]（不再是自己那套 OK）",
          _label_of({"command": "give a b", "status": "success"}) == "成功")
    check("failed → [失败]（不再是 FAIL）",
          _label_of({"command": "x", "status": "failed"}) == "失败")
    check("unknown → [结果未知]",
          _label_of({"command": "x", "status": "unknown"}) == "结果未知")
    check("★缺 status 且 ok=True → [结果未知]，不出现任何成功字样",
          _label_of({"command": "x", "ok": True}) == "结果未知")
    check("inferred_success → [已执行·未确认]",
          _label_of({"command": "x", "status": "inferred_success"}) == "已执行·未确认")
    check("dispatched_unconfirmed → [已发送·边界未确认]",
          _label_of({"command": "x", "status": "dispatched_unconfirmed"}) == "已发送·边界未确认")
    check("skipped → [未发送]",
          _label_of({"command": "x", "status": "skipped"}) == "未发送")
    check("syntax_error → [语法错误]",
          _label_of({"command": "x", "status": "syntax_error"}) == "语法错误")

    rendered = {_label_of({"command": "c", "status": st}) for st in KNOWN_STATUSES}
    check("★七态渲染出的标签集合 == STATUS_LABEL 值域（回执无自带措辞）",
          rendered == set(STATUS_LABEL.values()), str(sorted(rendered)))
    check("★回执里不再出现旧措辞 OK / FAIL / [未知]",
          not any(t in MCWorkflow._fmt_results([{"command": "x", "status": "success"}])
                  for t in ("[OK]", "[FAIL]", "[未知]")))


# ===================== 二、记账等长校验（fail-closed） =====================

def group_contract_length() -> None:
    print("---- 二、记账等长校验（取舍 6）----")
    cmds = [{"command": "give Steve diamond 1"}, {"command": "say hi"}]
    reps = [
        {"command": "give Steve diamond 1", "status": "success"},
        {"command": "say hi", "status": "failed"},
    ]

    got, err = MCWorkflow._newly_confirmed(cmds, reps)
    check("对照：等长且正常 → 只记 success 那条、无结构异常",
          got == {"give Steve diamond 1"} and err is None, str((sorted(got), err)))

    got2, err2 = MCWorkflow._newly_confirmed(cmds, reps[:1])
    check("★报告少于命令 → 结构异常且一条都不记（旧实现 zip 静默截断）",
          got2 == set() and bool(err2), str((sorted(got2), err2)))
    check("★异常说明写清两侧数量（便于定位）",
          "2 条命令" in (err2 or "") and "1 份报告" in (err2 or ""), str(err2))

    got3, err3 = MCWorkflow._newly_confirmed(cmds[:1], reps)
    check("★命令少于报告 → 结构异常且一条都不记",
          got3 == set() and bool(err3), str((sorted(got3), err3)))

    got4, err4 = MCWorkflow._newly_confirmed([], [])
    check("对照：空对空不算结构异常", got4 == set() and err4 is None, str((got4, err4)))

    halt = MCWorkflow._contract_halt_text(err2 or "")
    check("★结构异常回执：写明不会自动重试", "不会自动重试" in halt)
    check("★结构异常回执：写明重复副作用与漏发两种风险",
          "重复副作用" in halt and "漏发" in halt)
    check("结构异常回执：带上原因原文（不吞事实）", (err2 or "") in halt)
    check("结构异常回执：不谎称任务已完成", "已完成" not in halt)


# ===================== 三、记账与熔断同一双眼睛 =====================

def group_same_eye() -> None:
    print("---- 三、记账判据与熔断判据同源 ----")
    one = [{"command": "give Steve diamond 1"}]

    got, err = MCWorkflow._newly_confirmed(one, [{"command": "give Steve diamond 1", "ok": True}])
    check("★{ok:True} 缺 status → 不记账（旧实现按 ok 记成已生效 → 下一轮误跳过）",
          got == set() and err is None, str((sorted(got), err)))

    got_ok, _ = MCWorkflow._newly_confirmed(
        one, [{"command": "give Steve diamond 1", "ok": True, "status": "success"}])
    check("对照：带 status=success → 正常记账",
          got_ok == {"give Steve diamond 1"}, str(sorted(got_ok)))

    bad = ("unknown", "inferred_success", "dispatched_unconfirmed", "skipped", "syntax_error")
    check("★不确定 / 未发送 / 语法错误 一律不记账（只认 success）",
          all(MCWorkflow._newly_confirmed(one, [{"command": "give Steve diamond 1", "status": st}])[0] == set()
              for st in bad))
    check("★记账判据用的正是 _status_of（同源，不另开一套回推）",
          MCWorkflow._newly_confirmed(one, [{"command": "give Steve diamond 1", "status": "success"}])[0]
          == ({"give Steve diamond 1"} if MCWorkflow._status_of({"status": "success"}) == "success" else set()))

    check("口径自查：UNCERTAIN_STATUSES 与「不记账集合」相容",
          UNCERTAIN_STATUSES <= KNOWN_STATUSES, str(sorted(UNCERTAIN_STATUSES)))


# ===================== 四、闭环：记账 → 下一轮拦截 =====================

def group_ledger_closure() -> None:
    print("---- 四、闭环：记进账的命令下一轮确实被拦下 ----")
    wf = MCWorkflow.__new__(MCWorkflow)
    confirmed, err = MCWorkflow._newly_confirmed(
        [{"command": "/give   Steve diamond 1"}, {"command": "say hi"}],
        [{"command": "give Steve diamond 1", "status": "success"},
         {"command": "say hi", "status": "failed"}],
    )
    check("记账把带斜杠/多空格的命令规范成同一把钥匙",
          confirmed == {"give Steve diamond 1"} and err is None, str((sorted(confirmed), err)))

    fresh, dups = wf._split_confirmed(
        [{"command": "  /give Steve   diamond 1 "}, {"command": "say ok"}], confirmed)
    check("★闭环：上一轮确认成功的命令，下一轮被拦下（不重发）",
          len(dups) == 1 and len(fresh) == 1, str((fresh, dups)))
    check("闭环：没做过的新命令正常放行",
          [c["command"].strip() for c in fresh] == ["say ok"], str(fresh))


# ===================== 五、静态契约 =====================

def group_static() -> None:
    print("---- 五、静态契约：标签一份、记账不猜 ----")
    src = (PLUGIN_DIR / "core" / "workflow.py").read_text(encoding="utf-8")
    cr = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")

    check("★workflow 自带的 _tag 标签表已清零", "_tag = {" not in src)
    check("★回执标签取自 STATUS_LABEL", "STATUS_LABEL.get(status" in src)
    check("★workflow 已导入 STATUS_LABEL（同源）",
          "from .command_result import (" in src and "    STATUS_LABEL,\n" in src)
    check("STATUS_LABEL 只在 command_result.py 定义 1 次",
          cr.count("STATUS_LABEL = {") == 1 and "STATUS_LABEL = {" not in src)
    check("★记账不再按 ok 回推（代码里 0 处 if r.get(\"ok\")）",
          'if r.get("ok")' not in src, str(src.count('if r.get("ok")')))
    check("★_newly_confirmed 有显式等长校验",
          "if len(commands) != len(exec_reports):" in src)
    check("★两处调用点都处理结构异常（记账 + 停手）",
          src.count("newly, contract_err = self._newly_confirmed(") == 2 and
          src.count("return self._contract_halt_text(contract_err), False") == 2)
    check("v0.22.9 口径未破：_status_of 仍只 1 次实现",
          src.count("def _status_of") == 1)
    check("v0.22.8 口径未破：_halt_on_uncertain 仍只 1 次实现、≥2 处调用",
          src.count("def _halt_on_uncertain") == 1 and
          src.count("_halt_on_uncertain(exec_reports)") >= 2)
    check("v0.22.9 口径未破：不重发守门仍两处调用",
          src.count("_split_confirmed(commands, confirmed)") >= 2)


# ===================== 六、回归：旧 fail-closed 未被削弱 =====================

def group_regression() -> None:
    print("---- 六、回归：v0.22.9 fail-closed 未被削弱 ----")
    check("缺 status → _status_of 仍 unknown", MCWorkflow._status_of({"ok": True}) == "unknown")
    check("status=success 原样返回", MCWorkflow._status_of({"status": "success"}) == "success")
    check("未知状态串 → unknown", MCWorkflow._status_of({"status": "SUCCESS"}) == "unknown")
    check("回执缺 command / output 字段不崩（v0.22.9 回执安全）",
          isinstance(MCWorkflow._fmt_results([{"status": "unknown"}]), str))
    check("回执仍带服务器返回内容",
          "服务器原文" in MCWorkflow._fmt_results(
              [{"command": "c", "status": "failed", "output": "服务器原文"}]))
    check("熔断回执仍含明细（v0.22.8 口径未破）",
          "不会自动重试" in (MCWorkflow.__new__(MCWorkflow)._halt_on_uncertain(
              [{"command": "c", "status": "unknown"}]) or ("", False))[0])


def main() -> None:
    print("=" * 68)
    print("v0.22.10：展示标签单一真相 ＋ 记账 fail-closed")
    print("=" * 68)
    if not IMPORT_OK:
        print("[FAIL] 导入失败：", IMPORT_ERR)
        sys.exit(1)

    group_label()
    group_contract_length()
    group_same_eye()
    group_ledger_closure()
    group_static()
    group_regression()

    print("=" * 68)
    if FAIL:
        print(f"结果：{len(FAIL)} 项未通过")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("结果：全部通过 ✅")


if __name__ == "__main__":
    main()
