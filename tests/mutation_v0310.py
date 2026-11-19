"""v0.31.0：在隔离副本改坏巡检修复，每条只接受指定断言变红。"""
from __future__ import annotations

import argparse
import sys

from mutation_v0300 import CONSOLE, CONSOLE_TEST, HTML, PLAYER_TEST, UI_TEST, Mutation


PLAYER = "core/player_admin.py"
WORKFLOW = "core/workflow.py"
WORKFLOW_TEST = "tests/test_simple_workflow_result.py"
CONSOLE_CASE = "WebConsoleCheck.test_p22_world_write_subcommands_warn_without_flagging_queries"
MUTATIONS = (
    Mutation(
        "P21", "绕过封禁记录的 IP 校验", PLAYER,
        "            ip = normalize_ip(raw_ip)", "            ip = raw_ip",
        "P21 只有 normalize_ip 通过的目标可以解封", PLAYER_TEST,
        "PlayerAdminCheck.test_p21_invalid_ip_bans_remain_visible_without_unban_action",
    ),
    Mutation(
        "P21", "异常 IP 行重新渲染解封按钮", HTML,
        "    const actionCell = isIp && !canAct", "    const actionCell = false",
        "P21 异常 IP 行不得渲染解封按钮", UI_TEST, "P21",
    ),
    Mutation(
        "P22", "从 C 组删除 data remove", CONSOLE,
        '    ("data", "remove"): "会删除目标的 NBT 数据，删除不可撤销。",\n',
        "", "P22 写子命令须命中 C 组提示", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "降低 C 组写子命令的提示强度", CONSOLE,
        'warnings.append(_warning("world", world_change, "high"))',
        'warnings.append(_warning("world", world_change, "medium"))',
        "P22 C 组提示强度须为 high", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "删除不可撤销说明", CONSOLE,
        "会删除目标的 NBT 数据，删除不可撤销。", "会删除目标的 NBT 数据。",
        "P22 C 组提示须说明不可撤销", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "将 data get 误列为 C 组写操作", CONSOLE,
        '_WORLD_SUBCOMMANDS = {\n',
        '_WORLD_SUBCOMMANDS = {\n    ("data", "get"): "不可撤销。",\n',
        "P22 查询和引号中的命令文字不可触发写操作提示", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P23", "将设置页总开关恢复为可编辑", HTML,
        'id="cfg_wf_enabled" disabled', 'id="cfg_wf_enabled"',
        "P23 设置页两个工作流开关必须只读", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "设置页批量保存重新写入工作流总开关", HTML,
        '  ["cfg_wf_provider","llm_provider_id","s"],',
        '  ["cfg_wf_enabled","agent_workflow_enabled","b",true],\n'
        '  ["cfg_wf_provider","llm_provider_id","s"],',
        "P23 设置页批量保存不得包含工作流开关", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "设置页跳转按钮未绑定", HTML,
        '  $("cfg_wf_jump").onclick = ()=>switchTab("workflow");', "",
        "P23 跳转必须打开工作流页", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "保存过程中仍可再次操作开关", HTML,
        "  for(const control of controls) control.disabled = true;", "",
        "P23 保存期间两开关必须共同禁用", UI_TEST, "P23",
    ),
    Mutation(
        "P24", "跳过最近任务持久化", WORKFLOW,
        '        self._persist_recent_logs()', "",
        "P24 两条日志重建实例后仍可读取", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "取消 200 条保留上限", WORKFLOW,
        "        if len(self.logs) > RECENT_LOG_LIMIT:", "        if False:",
        "P24 210条后删去最旧一半", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "详情直接截断并写入真实 IP", WORKFLOW,
        '_redact(str(row.get("detail", row.get("detail_excerpt", ""))))[:200]',
        'str(row.get("detail", row.get("detail_excerpt", "")))[:200]',
        "P24 与审计同源遮蔽 IPv4/IPv6", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "将历史运行记录计入当前任务", HTML,
        'logs.filter(l=>l.status==="running" && !l.historical).length',
        'logs.filter(l=>l.status==="running").length',
        "P24 恢复的历史任务不得计为正在执行", UI_TEST, "P24",
    ),
)


def main(argv: list[str]) -> int:
    from mutation_all import entry, registered_cases

    parser = argparse.ArgumentParser(description=__doc__)
    cases = registered_cases(sys.modules[__name__])
    parser.add_argument("criteria", nargs="*", help="P/T criterion IDs")
    args = parser.parse_args(argv)
    criteria = set(args.criteria)
    if criteria - {c.criterion for c in cases}:
        parser.error("unknown criterion")
    selected = [c for c in cases if not criteria or c.criterion in criteria]
    return entry({"v0310": selected}, legacy=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
