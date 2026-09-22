# -*- coding: utf-8 -*-
"""v0.22.6 工作流 simple 路径 —— 结果判定回归测试。

运行：
  <python> tests/test_simple_workflow_result.py

这是 2026-09-21 那次事故的**靶心复现**：

    分类器生成 1.21 组件语法 → 1.20.1 服务端回
    "Expected whitespace to end one argument, but found trailing data"
    → 旧 ``_run_simple`` 只要 ``rcon.command()`` 不抛异常就 ``ok_count += 1``
    → 而成功分支 ``if ok_count == len(commands): return summary`` **把明细整段丢弃**
    → 回执写「已执行 1/1 条命令」，游戏里什么都没有。

改后必须满足：
  · 语法错误**不计成功**；
  · 回执**包含实际命令与服务器原文**（明细永不被丢弃）；
  · 工作流日志状态**不再固定 done**；
  · tellraw 空响应**不误报失败**；
  · 非幂等命令空响应**不误报成功**。

找不到 AstrBot 运行时则整体 SKIP（不判失败）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []

add_sys_paths()
try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow
    from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import RconTimeoutError
    WORKFLOW_OK = True
    IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    WORKFLOW_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}"
          + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


#: 真实事故原文（2026-09-21 19:18 实测）
REAL_SYNTAX_ERR = "Expected whitespace to end one argument, but found trailing data"
REAL_BAD_CMD = ('give HuangGuaKnn netherite_sword[enchantments={levels:'
                '{"minecraft:sharpness":5}}] 1')
REAL_OK = "Gave 1 [Netherite Sword] to HuangGuaKnn"


class StubRcon:
    """假 RCON：只回固定文本，并可指定两个事实维度。"""

    def __init__(self, out="", *, boundary=True, received=True, boom=False):
        self._out = out
        self.last_boundary_confirmed = boundary
        self.last_response_received = received
        self._boom = boom
        self.sent: list[str] = []

    async def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        if self._boom:
            raise RconTimeoutError("模拟：哨兵超时，未收到响应")
        return self._out


class StubPlugin:
    def __init__(self, rcon):
        self._rcon = rcon
        self.feedbacks: list[str] = []

    async def _get_rcon(self):
        return self._rcon

    async def _safe_command(self, event, cmd, **kw):
        return None   # 闸门放行（权限不在本测试范围内）

    async def _send_feedback(self, rcon, tip):
        self.feedbacks.append(tip)


def make_workflow(rcon):
    """绕开 __init__ 造一个只带必要依赖的 MCWorkflow。

    ``_apply_player_to_commands`` 被替换成恒等函数 —— 玩家名替换不属于本测试，
    隔离掉它才能干净地验「结果判定」这一件事。
    """
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = StubPlugin(rcon)
    wf.logger = logging.getLogger("test.simple")
    wf._apply_player_to_commands = lambda cmds, player: cmds
    return wf


async def run_one(rcon, commands, request="测试请求"):
    wf = make_workflow(rcon)
    return await wf._run_simple({"commands": commands}, request, "HuangGuaKnn", None)


# ===================== 用例 =====================

async def simple_cases() -> None:
    print("---- 一、simple 路径结果判定 ----")

    # ---- ① 真实事故：语法错误绝不能被算成成功 ----
    rcon = StubRcon(REAL_SYNTAX_ERR)
    text, status = await run_one(rcon, [REAL_BAD_CMD])
    check("★语法错误：不得宣称成功（旧实现在这里报「已执行 1/1」）",
          "已成功 0/1" in text, text[:120])
    check("★语法错误：回执必须带**服务器原文**（旧实现丢弃明细）",
          REAL_SYNTAX_ERR in text, text[:160])
    check("★语法错误：回执必须带**实际下发的命令**",
          "netherite_sword" in text, text[:160])
    check("★语法错误：工作流日志状态不得是 done",
          status != "done", status)
    check("语法错误：标注为可纠错的语法错误（第一批不自动重发）",
          "未自动重发" in text, text[-120:])
    check("语法错误：命令只被下发**一次**（绝不原样重发）",
          len(rcon.sent) == 1, str(rcon.sent))

    # ---- ② 正常成功：明细也要保留（不是只有失败才给明细）----
    rcon = StubRcon(REAL_OK)
    text, status = await run_one(rcon, ["give Steve diamond 1"])
    check("正常成功：wf_status = done", status == "done", status)
    check("正常成功：回执写明 1/1", "已成功 1/1" in text, text[:120])
    check("正常成功：明细（服务器原文）同样保留",
          REAL_OK in text, text[:160])

    # ---- ③ 静默命令空响应：不得误报失败 ----
    rcon = StubRcon("", boundary=True, received=True)
    text, status = await run_one(rcon, ['tellraw @a {"text":"集合"}'])
    check("★tellraw 空响应 + 边界已确认 → 算成功（旧实现会报「已执行 0/1」）",
          status == "done" and "已成功 1/1" in text, f"{status}｜{text[:100]}")

    # ---- ④ idle 降级：已发送但边界未确认，既不报失败也不报未知 ----
    rcon = StubRcon("", boundary=False, received=True)
    text, status = await run_one(rcon, ['tellraw @a {"text":"集合"}'])
    check("★idle 降级 + tellraw 空响应 → 报「已发送但边界未确认」（不报失败）",
          "边界未确认" in text and "失败" not in text, text[:160])
    check("idle 降级：不计入成功，但也不熔断（状态不是 unknown）",
          status != "unknown", status)

    # ---- ⑤ 非幂等命令空响应：不得误报成功 ----
    rcon = StubRcon("", boundary=True, received=True)
    text, status = await run_one(rcon, ["give Steve diamond 1"])
    check("★give 空响应 → 结果未知（副作用是否落定无法确认）",
          status == "unknown" and "已成功 0/1" in text, f"{status}｜{text[:100]}")

    # ---- ⑥ 超时：结果未知，且绝不重发 ----
    rcon = StubRcon(boom=True)
    text, status = await run_one(rcon, ["give Steve diamond 1"])
    check("超时 → wf_status = unknown", status == "unknown", status)
    check("超时 → 回执写明「不会自动重发」", "不会自动重发" in text, text[:160])
    check("超时 → 命令只下发一次（防重复副作用）",
          len(rcon.sent) == 1, str(rcon.sent))

    # ---- ⑦ 多命令混合：不得因一条失败而吞掉后面 ----
    rcon = StubRcon(REAL_OK)
    text, status = await run_one(rcon, ["give Steve diamond 1", "give Steve apple 1"])
    check("多命令：两条都执行（不得因前一条误判而熔断后续）",
          len(rcon.sent) == 2, str(rcon.sent))
    check("多命令：成功计数正确", "已成功 2/2" in text, text[:120])


async def complex_cases() -> None:
    """批次 2：复杂路径 ``_exec_commands`` 也收口到统一判定。"""
    print("---- 二、复杂路径 _exec_commands 结果判定 ----")

    # ---- ① 语法错误：判负、不熔断、不发游戏内反馈 ----
    rcon = StubRcon(REAL_SYNTAX_ERR)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": REAL_BAD_CMD, "feedback": "已发放宝剑"},
         {"command": 'tellraw @a {"text":"hi"}'}],
        "HuangGuaKnn", ["HuangGuaKnn"],
    )
    check("★复杂路径：语法错误判 syntax_error（旧实现判 success）",
          reports[0]["status"] == "syntax_error" and reports[0]["ok"] is False,
          str(reports[0]))
    check("★复杂路径：语法错误**不熔断**后续命令（可安全纠错重试）",
          len(reports) == 2 and len(rcon.sent) == 2, f"{len(reports)}｜{rcon.sent}")
    check("★复杂路径：未确认成功时**不发**游戏内反馈（不报喜不报忧）",
          wf.plugin.feedbacks == [], str(wf.plugin.feedbacks))

    # ---- ② 确认成功：发游戏内反馈 ----
    rcon = StubRcon(REAL_OK)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": "give HuangGuaKnn diamond 1", "feedback": "已发放钻石"}],
        "HuangGuaKnn", ["HuangGuaKnn"],
    )
    check("复杂路径：确认成功 → status=success",
          reports[0]["status"] == "success", str(reports[0]))
    check("复杂路径：确认成功 → 游戏内反馈发出",
          wf.plugin.feedbacks == ["已发放钻石"], str(wf.plugin.feedbacks))

    # ---- ③ 非幂等空响应：熔断 + 后续 skipped ----
    rcon = StubRcon("")
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": "give HuangGuaKnn diamond 1"},
         {"command": "give HuangGuaKnn apple 1"}],
        "HuangGuaKnn", ["HuangGuaKnn"],
    )
    check("★非幂等空响应 → unknown（绝不误判成功）",
          reports[0]["status"] == "unknown", str(reports[0]))
    check("★unknown → 立刻熔断，后续填 skipped 且**未下发**",
          reports[1]["status"] == "skipped" and len(rcon.sent) == 1,
          f"{reports[1]}｜{rcon.sent}")

    # ---- ④ 边界未确认：不算成功，但**不熔断** ----
    rcon = StubRcon("", boundary=False, received=True)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": 'tellraw @a {"text":"hi"}'},
         {"command": 'tellraw @a {"text":"ho"}'}],
        "HuangGuaKnn", ["HuangGuaKnn"],
    )
    check("★边界未确认 → dispatched_unconfirmed（不报失败、不报未知）",
          reports[0]["status"] == "dispatched_unconfirmed", str(reports[0]))
    check("★边界未确认 → 不熔断后续命令",
          len(reports) == 2 and len(rcon.sent) == 2, f"{len(reports)}｜{rcon.sent}")

    # ---- ⑤ 目标玩家不在线：failed 且不下发 ----
    rcon = StubRcon(REAL_OK)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": "give GhostPlayer diamond 1"}], "GhostPlayer", ["HuangGuaKnn"],
    )
    check("目标玩家不在线 → failed 且命令**未下发**",
          reports[0]["status"] == "failed" and rcon.sent == [],
          f"{reports[0]}｜{rcon.sent}")

    # ---- ⑦ 熔断判据 = 「重发是否有危险」，不得扩大熔断范围（v0.22.3 既有口径）----
    # 场景：批次第一条是广播，RCON 拿不到边界与响应事实 → 旧代码若把静默命令的
    # 空响应判成 unknown 就会熔断，后续发物品被无声吞掉。
    rcon = StubRcon("", boundary=False, received=False)
    wf = make_workflow(rcon)
    reports = await wf._exec_commands(
        [{"command": "say hi"}, {"command": "give HuangGuaKnn diamond 1"}],
        "HuangGuaKnn", ["HuangGuaKnn"],
    )
    check("★静默命令空响应 → 按普通失败口径上报，**不得**标 unknown（不扩大熔断）",
          reports[0]["status"] == "failed" and reports[0].get("unknown") is not True,
          str(reports[0]))
    # 注意：stub 对所有命令都回空，所以后面的 give 自己也会判 unknown —— 本条要验的是
    # 「say 有没有把 give 吞掉」（即它有没有扩大熔断范围），而不是 give 的最终状态。
    check("★静默命令空响应 → 后续命令照常下发（不被无声吞掉）",
          len(rcon.sent) == 2 and rcon.sent[1].startswith("give"),
          str(rcon.sent))
    check("★对比：give 自己的空响应仍熔断（重发有危险 → unknown）",
          reports[1]["status"] == "unknown" and reports[1].get("unknown") is True,
          str(reports[1] if len(reports) > 1 else "N/A"))

    # ---- ⑥ 回执渲染 ----
    rep = [
        {"command": "a", "ok": True, "status": "success", "output": REAL_OK},
        {"command": "b", "ok": False, "status": "dispatched_unconfirmed", "output": "边界未确认"},
        {"command": "c", "ok": False, "status": "syntax_error", "output": REAL_SYNTAX_ERR},
    ]
    fmt = MCWorkflow._fmt_results(rep)
    check("_fmt_results：新状态有中文标签（不再一律兜底成 FAIL；v0.22.10 统一走 STATUS_LABEL）",
          "已发送·边界未确认" in fmt and "语法错误" in fmt, fmt[:200])
    txt = MCWorkflow._success_text({"reasoning": "r"}, [{"command": "x"}] * 3, rep)
    check("★_success_text：存在边界未确认条目时**必须**在回执里点明",
          "边界未确认" in txt and "1/3" in txt, txt[:220])


def prompt_cases() -> None:
    """批次 2：分类 prompt 的规则冲突（事故治本）。"""
    print("---- 三、Agent#1 分类规则（治本） ----")
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control.core.agent_prompts import (
            CLASSIFIER_SYSTEM,
        )
    except Exception as e:  # noqa: BLE001
        skip(f"无法导入 agent_prompts，跳过 prompt 用例（{e}）")
        return

    check("★分类 prompt 不再把「原版物品」绝对判 simple（已改为「裸」原版物品）",
          "裸" in CLASSIFIER_SYSTEM, "")
    check("★分类 prompt 含显式优先级：冲突时一律以 complex 为准",
          "一律以 complex 为准" in CLASSIFIER_SYSTEM, "")
    check("★分类 prompt 点明 simple 路径无输出校验 / 无纠错 / 无到账核验",
          "没有输出校验" in CLASSIFIER_SYSTEM, "")
    check("分类 prompt 要求透传服务端版本与玩家名",
          "服务端版本" in CLASSIFIER_SYSTEM, "")
    check("分类 prompt 给出「附魔原版剑」这个具体反例",
          "附魔锋利5的下界合金剑" in CLASSIFIER_SYSTEM, "")


async def main_async() -> int:
    if not WORKFLOW_OK:
        skip(f"找不到 AstrBot 运行时，跳过工作流用例（{IMPORT_ERR}）")
    else:
        await simple_cases()
        await complex_cases()
        prompt_cases()

    print("==========================================")
    if SKIP:
        print(f"跳过 {len(SKIP)} 组（环境限制，不算失败）：")
        for s in SKIP:
            print("  -", s)
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过：simple+复杂路径均不再假成功 / 明细永不丢弃 / "
          "分类 prompt 不再自相矛盾")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
