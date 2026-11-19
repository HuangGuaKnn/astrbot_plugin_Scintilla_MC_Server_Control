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

N11：找不到 AstrBot 运行时**不算跳过**（跳过=绿）。导入失败一律记 FAIL、rc=1。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


# N11：**本文件不提供 skip()**。跳过在门禁里等于绿（rc=0），任何「跑不动就跳过」的写法
# 都会把代码错误静默转成通过。跑不动就记 FAIL；`SKIP` 只为汇总格式保留，必须恒为 0
# —— main_async 末尾有一条自检盯着它。


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
        check("导入 core.agent_prompts（prompt 用例的前置）", False, f"{type(e).__name__}: {e}")
        return  # N11：导入失败=真失败，不是跳过

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


def p24_persistence_cases() -> None:
    """P24 使用完整构造但所有文件均在临时目录；不读取实际插件记录。"""
    from astrbot_plugin_Scintilla_MC_Server_Control.core import workflow as module

    def plugin():
        return SimpleNamespace(logger=Mock(), context=SimpleNamespace(), config={},
                               _cfg=lambda key, default=None: default)

    fields = {"ts", "kind", "target", "status", "detail_excerpt"}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "workflow_recent.jsonl"
        with patch.object(module, "_recent_log_path", return_value=path):
            wf = MCWorkflow(plugin())
            wf._log(kind="simple", player="Steve", status="done", request="不落盘的原始请求",
                    detail="已完成；192.0.2.9 与 2001:db8::42 " + "长" * 240, cost="1.0s")
            wf._log(kind="complex", player="192.0.2.10", status="failed",
                    request="另一个原始请求", detail="失败；[2001:db8::99]:25565")
            # 从磁盘重建真实实例；严禁“恢复日志”顺带恢复任务。
            with (patch.object(MCWorkflow, "dispatch", new=AsyncMock()) as dispatch,
                  patch.object(MCWorkflow, "_run_simple", new=AsyncMock()) as simple,
                  patch.object(MCWorkflow, "_run_complex_async", new=AsyncMock()) as complex_task,
                  patch.object(asyncio, "create_task") as spawn):
                rebuilt = MCWorkflow(plugin())
            rows = rebuilt.recent_logs(200)
            check("P24 两条日志重建实例后仍可读取（删除持久化调用必须红）",
                  len(rows) == 2, str(rows))
            check("P24 启动只读记录，不执行或重新排队任务",
                  not any(call.called for call in (dispatch, simple, complex_task, spawn))
                  and not rebuilt._tasks)
            check("P24 重载保持最新在前与 WebUI request/player/detail 字段",
                  len(rows) == 2 and rows[0]["kind"] == "complex"
                  and rows[1]["kind"] == "simple"
                  and rows[0]["player"] == "[IP 已隐藏]"
                  and rows[0]["request"] == "历史任务 · [IP 已隐藏]"
                  and rows[0]["detail"] == rows[0]["detail_excerpt"]
                  and all(row["historical"] is True for row in rows), str(rows))
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            records = [json.loads(line) for line in text.splitlines()]
            check("P24 JSONL 只落五个完整字段，不存原始请求/cost/执行命令",
                  len(records) == 2 and all(set(row) == fields for row in records)
                  and "原始请求" not in text, text[:200])
            check("P24 与审计同源遮蔽 IPv4/IPv6，详情先脱敏后截断至200字符",
                  len(records) == 2 and len(records[0]["detail_excerpt"]) == 200
                  and records[0]["detail_excerpt"].count("[IP 已隐藏]") == 2
                  and all(ip not in text for ip in
                          ("192.0.2.9", "192.0.2.10", "2001:db8::42", "2001:db8::99")), text[:250])
            check("P24 当前内存继续保留现有 request/detail/cost，不改任务回执",
                  wf.recent_logs(2)[1]["request"] == "不落盘的原始请求"
                  and wf.recent_logs(2)[1]["cost"] == "1.0s"
                  and "192.0.2.9" in wf.recent_logs(2)[1]["detail"])

        # 210条越过200后删旧一半：保留第100至209条，共110条。
        path = Path(td) / "rolling.jsonl"
        with patch.object(module, "_recent_log_path", return_value=path):
            rolling = MCWorkflow(plugin())
            for index in range(210):
                rolling._log(kind="simple", player=f"Player{index}", status="done",
                             detail=f"record {index}")
            latest = MCWorkflow(plugin()).recent_logs(500)
            disk = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
            check("P24 210条后删去最旧一半，内存/磁盘/重载均为110条且≤200",
                  len(rolling.logs) == len(latest) == len(disk) == 110,
                  f"memory={len(rolling.logs)} reload={len(latest)} disk={len(disk)}")
            check("P24 截断保留最新区间与顺序（209→100），不误删新任务",
                  [row["target"] for row in latest] == [f"Player{i}" for i in range(209, 99, -1)])

        # 存量损坏、末行截断和额外敏感字段：只恢复五字段记录，坏行不执行。
        path = Path(td) / "damaged.jsonl"
        valid = {"ts": "2026-11-19 00:00:00", "kind": "simple", "target": "Steve",
                 "status": "done", "detail_excerpt": "192.0.2.11", "request": "不能恢复这个字段"}
        path.write_text("not json\n" + json.dumps(valid, ensure_ascii=False)
                        + '\n{"ts": "truncated"', encoding="utf-8")
        host = plugin()
        with patch.object(module, "_recent_log_path", return_value=path):
            recovered = MCWorkflow(host)
            rows = recovered.recent_logs()
            check("P24 损坏行与残行跳过，合法记录仍恢复并重新脱敏",
                  len(rows) == 1 and rows[0]["detail"] == "[IP 已隐藏]"
                  and rows[0]["request"] == "历史任务 · Steve", str(rows))
            check("P24 存量损坏只记一次警告", host.logger.warning.call_count == 1)
            recovered._log(status="error", detail="new")
            clean = path.read_text(encoding="utf-8")
            check("P24 下一次写入修复损坏快照，默认kind/target仍完整",
                  len(clean.splitlines()) == 2
                  and all(set(json.loads(line)) == fields for line in clean.splitlines()))

        # 所有老测试的 __new__ 替身不应在 _log 时偷偷解析真实插件数据目录。
        isolated = MCWorkflow.__new__(MCWorkflow)
        isolated.logs = deque(maxlen=30)
        with patch.object(module, "_recent_log_path", side_effect=AssertionError("触及真实目录")) as resolve:
            isolated._log(status="done", detail="memory only")
        check("P24 未完整初始化的隔离实例只写内存，不解析真实插件数据目录",
              len(isolated.logs) == 1 and not resolve.called)


async def p24_write_failure_cases() -> None:
    """P24 写失败不改 dispatch/simple 的执行结果，且连续失败只告警一次。"""
    from astrbot_plugin_Scintilla_MC_Server_Control.core import workflow as module

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "workflow_recent.jsonl"
        rcon = StubRcon(REAL_OK)
        host = StubPlugin(rcon)
        host.logger = Mock()
        host.context = SimpleNamespace()
        host.config = {}
        host._cfg = lambda key, default=None: default
        with patch.object(module, "_recent_log_path", return_value=path):
            wf = MCWorkflow(host)
            wf._log(kind="simple", player="Steve", status="done", detail="existing")
            previous = path.read_bytes() if path.exists() else b""
            wf.agent.classify = AsyncMock(return_value={"type": "simple", "commands": ["give Steve diamond 1"]})
            wf._refresh_version_context = lambda: None
            wf._decide_player = AsyncMock(return_value={"abort": False, "player": "Steve", "source": "explicit"})
            wf._apply_player_to_commands = lambda commands, player: commands
            event = SimpleNamespace(unified_msg_origin="p24-test")
            with patch.object(Path, "replace", side_effect=PermissionError("192.0.2.50 denied")):
                results = [await wf.dispatch(event, "give Steve diamond 1", "Steve") for _ in range(2)]
            check("P24 落盘失败不改变任务执行，两次任务各发一次且返回完成",
                  rcon.sent == ["give Steve diamond 1", "give Steve diamond 1"]
                  and all("[MC工作流·完成]" in result for result in results), str(results))
            check("P24 连续写失败只记一条脱敏警告，内存仍保留新日志",
                  host.logger.warning.call_count == 1 and len(wf.logs) == 3
                  and "192.0.2.50" not in str(host.logger.warning.call_args),
                  str(host.logger.warning.call_args))
            check("P24 原子替换失败保留上一份完整快照并清理临时文件",
                  bool(previous) and path.read_bytes() == previous
                  and not any(path.parent.glob(path.name + ".*.tmp")))


async def main_async() -> int:
    if WORKFLOW_OK:
        await simple_cases()
        await complex_cases()
        prompt_cases()
        p24_persistence_cases()
        await p24_write_failure_cases()
    else:
        # N11：导入失败**不是**「环境限制」。导入断了说明环境或被测代码坏了，必须红。
        check("导入插件运行时（core.workflow / core.rcon）", False,
              f"{IMPORT_ERR or '未知原因'} —— 导入失败不许算「环境限制」")
    check("★本文件零跳过（N11：跳过=绿，跑不动就红）", not SKIP, " / ".join(SKIP))

    print("==========================================")
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过：simple+复杂路径均不再假成功 / 明细永不丢弃 / "
          "分类 prompt 不再自相矛盾")
    return 0


async def p24_only() -> None:
    """定向变异入口：失败汇总用 AssertionError，保留可定位的 P24 判据名称。"""
    if not WORKFLOW_OK:
        raise AssertionError(f"P24 插件运行时导入失败：{IMPORT_ERR}")
    p24_persistence_cases()
    await p24_write_failure_cases()
    if FAIL:
        raise AssertionError("P24 判据失败：" + "；".join(FAIL))
    print("P24 PASS: 最近任务持久化、保留上限、脱敏、只读恢复和写失败隔离")


if __name__ == "__main__":
    if sys.argv[1:] == ["P24"]:
        asyncio.run(p24_only())
    else:
        sys.exit(asyncio.run(main_async()))
