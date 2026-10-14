# -*- coding: utf-8 -*-
"""Issue #9 P1 级缺陷专项回归测试（v0.24.4）。

覆盖：
1. positioned as / rotated as token 消费数量正确，内层命令鉴权不被绕过
2. tp / 模组命令遇到 unknown 熔断停止后续，不标 retry_safe
3. B5 give 重写命令跨轮准确记账与去重，防止二次下发
4. 绑定账号离线时不被模糊子串提升给同名前缀玩家
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.java_commands import (
    unwrap_command,
    effective_level,
)
from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow
from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import CommandResult

FAIL: list[str] = []

def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[PASS] {desc}")
    else:
        print(f"[FAIL] {desc} -> {detail}")
        FAIL.append(f"{desc}: {detail}")

def test_p1_1_execute_tokens():
    print("---- 1. P1-1: positioned as / rotated as 词法边界与解包 ----")
    # positioned as <targets> run stop
    cmd1 = "execute positioned as @a run stop"
    inner1 = unwrap_command(cmd1)
    check("positioned as @a run stop 解包出 stop", inner1 == "stop", f"inner={inner1}")
    name1, lvl1, _ = effective_level(cmd1)
    check("positioned as @a run stop 判定为 stop (4级)", (name1, lvl1) == ("stop", 4), f"got {name1}:{lvl1}")

    # rotated as <targets> run op Steve
    cmd2 = "execute rotated as @p run op Steve"
    inner2 = unwrap_command(cmd2)
    check("rotated as @p run op Steve 解包出 op Steve", inner2 == "op Steve", f"inner={inner2}")
    name2, lvl2, _ = effective_level(cmd2)
    check("rotated as @p run op Steve 判定为 op (3级)", (name2, lvl2) == ("op", 3), f"got {name2}:{lvl2}")

    # positioned as @a run give Steve diamond 1
    cmd3 = "execute positioned as @a run give Steve diamond 1"
    inner3 = unwrap_command(cmd3)
    check("positioned as @a run give 解包正常", inner3 == "give Steve diamond 1", f"inner={inner3}")

def test_p1_2_unknown_retry_safe():
    print("---- 2. P1-2: tp/模组未知结果熔断，不给 retry_safe ----")
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = MagicMock()
    wf.logger = logging.getLogger("test.p1_2")

    # 模拟 _exec_commands 遇到 tp 的空响应产出报告
    rep_tp = [
        {"command": "tp Steve ~1 ~ ~", "ok": False, "unknown": True, "status": "unknown", "output": "未知命令类型的空响应"},
        {"command": "give Steve diamond 1", "ok": False, "status": "skipped", "output": "因前一条命令结果未知，为避免重复副作用，本条未发送。"}
    ]
    halt = wf._halt_on_uncertain(rep_tp)
    check("tp unknown 触发 uncertain 熔断暂停", halt is not None and halt[1] is False, f"halt={halt}")
    check("tp 未被赋予 retry_safe", not rep_tp[0].get("retry_safe"), f"rep={rep_tp[0]}")

def test_p1_3_b5_ledger_dedup():
    print("---- 3. P1-3: B5 规范化命令跨轮去重与记账 ----")
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = MagicMock()
    wf.logger = logging.getLogger("test.p1_3")
    wf._preflatten = True
    
    # 模拟 plugin._legacy_give_plan
    plan_mock = MagicMock()
    plan_mock.ok = True
    plan_mock.command = "give Steve minecraft:diamond 1 0"
    wf.plugin._legacy_give_plan = MagicMock(return_value=plan_mock)

    # 1. 记账
    commands = [{"command": "give Steve diamond 1"}]
    reports = [{"command": "give Steve minecraft:diamond 1 0", "status": "success", "ok": True}]
    newly, err = wf._newly_confirmed(commands, reports)
    check("newly_confirmed 无契约错误", err is None, f"err={err}")
    check("原始命令进入账本", "give Steve diamond 1" in newly, f"newly={newly}")
    check("重写规范命令也进入账本", "give Steve minecraft:diamond 1 0" in newly, f"newly={newly}")

    # 2. 次轮去重：实现器次轮输出规范化命令
    next_commands = [{"command": "give Steve minecraft:diamond 1 0"}]
    fresh, dups = wf._split_confirmed(next_commands, newly)
    check("次轮规范化命令被成功拦截去重", len(fresh) == 0 and len(dups) == 1, f"fresh={fresh}, dups={dups}")

    # 3. 反向去重：账本里只有规范命令时，简写命令也能被拦截
    ledger_only_norm = {"give Steve minecraft:diamond 1 0"}
    fresh2, dups2 = wf._split_confirmed([{"command": "give Steve diamond 1"}], ledger_only_norm)
    check("账本含规范名时简写命令也能去重拦截", len(fresh2) == 0 and len(dups2) == 1, f"fresh2={fresh2}, dups2={dups2}")

def test_p1_4_bound_player_no_substring():
    print("---- 4. P1-4: 绑定玩家离线时禁止子串模糊提升 ----")
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = MagicMock()
    wf.logger = logging.getLogger("test.p1_4")
    
    # 在线玩家只有 Steve2
    async def mock_online():
        return ["Steve2"]
    wf._online_players = mock_online

    async def run_res():
        # source == "bound"
        res_bound, _ = await wf._resolve_player("Steve", "给我一个钻石", player_source="bound")
        # source == "explicit"
        res_explicit, _ = await wf._resolve_player("Steve", "给Steve一个钻石", player_source="explicit")
        return res_bound, res_explicit

    res_bound, res_explicit = asyncio.run(run_res())
    check("bound 来源的 Steve 离线时保持原名 Steve（不被篡改为 Steve2）", res_bound == "Steve", f"got {res_bound}")
    check("explicit 来源的昵称允许模糊匹配提升为 Steve2", res_explicit == "Steve2", f"got {res_explicit}")

def main():
    test_p1_1_execute_tokens()
    test_p1_2_unknown_retry_safe()
    test_p1_3_b5_ledger_dedup()
    test_p1_4_bound_player_no_substring()
    if FAIL:
        print(f"\n[FAILED] {len(FAIL)} checks failed!")
        return 1
    print("\n[ALL PASS] Issue #9 P1 4项全量通过！")
    return 0

if __name__ == "__main__":
    sys.exit(main())
