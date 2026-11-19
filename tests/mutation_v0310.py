"""v0.31.0：在隔离副本改坏巡检修复，每条只接受指定断言变红。"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from _paths import PLUGIN_DIR
from mutation_v0270 import exercise, run_test
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("criteria", nargs="*", help="仅运行指定判据，如 P21 P22")
    args = parser.parse_args(argv)
    criteria = set(args.criteria)
    if criteria - {m.criterion for m in MUTATIONS}:
        parser.error("请输入有效的判据编号")
    tests = [m for m in MUTATIONS if not criteria or m.criterion in criteria]
    originals = {m.path: (PLUGIN_DIR / m.path).read_bytes() for m in tests}
    with tempfile.TemporaryDirectory(prefix="scintilla-v0310-mutation-") as temporary:
        root = Path(temporary) / PLUGIN_DIR.name
        # StarTools 会创建插件数据目录。子进程只使用这轮的临时目录。
        os.environ["ASTRBOT_ROOT"] = str(Path(temporary) / "astrbot")
        shutil.copytree(PLUGIN_DIR, root,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "_shots", "*.pyc", "*.log"))
        for command in sorted({tuple(m.command) for m in tests}):
            baseline = run_test(root, list(command))
            if baseline.returncode:
                print(f"[BASELINE FAIL] {command[-1]}\n{baseline.stdout}{baseline.stderr}", flush=True)
                return 1
            print(f"[BASELINE PASS] {command[-1]}", flush=True)
        results = []
        for mutation in tests:
            source = (root / mutation.path).read_text(encoding="utf-8")
            offset = source.find(mutation.before)
            line = source.count("\n", 0, offset) + 1 if offset >= 0 else "?"
            print(f"[TARGET] {mutation.criterion} {mutation.path}:{line} → {mutation.case}", flush=True)
            results.append(exercise(root, mutation))
    for path, original in originals.items():
        if (PLUGIN_DIR / path).read_bytes() != original:
            raise RuntimeError(f"源工作区发生变化：{path}")
        print(f"[SOURCE UNCHANGED] {path} {hashlib.sha256(original).hexdigest()}", flush=True)
    print(f"定向变异 {sum(results)}/{len(results)} 变红，隔离副本逐条字节级还原", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
