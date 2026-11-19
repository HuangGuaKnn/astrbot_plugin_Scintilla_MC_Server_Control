# -*- coding: utf-8 -*-
"""v0249 mutation catalog. Runs in the shared isolated runner."""
from __future__ import annotations

import argparse
import sys


class Case:
    def __init__(self, cid: str, rel: str, old: str, new: str, test: str, note: str):
        self.cid = cid
        self.rel = rel
        self.old = old
        self.new = new
        self.test = test
        self.note = note


# 多行锚点一律按 **LF** 写（仓库文件是纯 LF），
# run_case() 里保留了「LF 找不到就试 CRLF」的自动适配，无害。
CASES = [
    Case(
        "M1", "main.py",
        "        if name in _KNOWLEDGE_TOOLS:",
        "        if False:",
        "tests/test_v0249_master_switch.py",
        "_master_gate 的知识工具分派失效 → 两栏各管各栏的判据必须红",
    ),
    Case(
        "M2", "main.py",
        "        elif name in _DICTIONARY_TOOLS:\n"
        "            # 词典类工具：先说「工具栏」再说「词典栏」，两条都关了也说得清是哪个先挡的\n"
        "            if not self._tools_master_enabled():",
        "        elif name in _DICTIONARY_TOOLS:\n"
        "            # 词典类工具：先说「工具栏」再说「词典栏」，两条都关了也说得清是哪个先挡的\n"
        "            if False:",
        "tests/test_v0249_master_switch.py",
        "工具栏总开关放过词典工具 → 词典工具的双闸门判据必须红",
    ),
    Case(
        "M3", "main.py",
        # v0.24.9 后续：回执文案收成模块级常量（单一来源），锚点跟着挪到常量定义那一行
        '    "「LLM 工具」总开关已关闭（WebUI → 插件设置 → AI 能力 → LLM 工具开关）："',
        '    "「词典与知识库」总开关已关闭（WebUI → 插件设置 → AI 能力 → LLM 工具开关）："',
        "tests/test_v0249_master_switch.py",
        "回执把「LLM 工具」写错成另一栏 → 「回执点名「LLM 工具」」必须红",
    ),
    Case(
        "M4", "main.py",
        '        if not self._cfg_bool("dictionary_enabled", True):\n'
        '            return "物品词典已在插件配置中停用',
        "        if not self._knowledge_master_enabled():\n"
        "            return _MSG_KB_MASTER_OFF\n"
        '        if not self._cfg_bool("dictionary_enabled", True):\n'
        '            return "物品词典已在插件配置中停用',
        "tests/test_v0248_dictionary_reason.py",
        "词典缺失的归因重新让总开关抢答 → 单项归因判据必须红",
    ),
    Case(
        "M5", "core/web_api.py",
        '        "enable_all_tools", "enable_all_knowledge",',
        '        "enable_all_knowledge",',
        "tests/test_settings_whitelist_contract.py",
        "后端保存白名单漏登记 enable_all_tools → 「没有『提交了但后端不认』的键」必须红",
    ),
    Case(
        "M6", "main.py",
        "        elif not self._tools_master_enabled():\n"
        "            return _MSG_TOOLS_MASTER_OFF",
        "        elif not self._tools_master_enabled():\n"
        '            return ""',
        "tests/test_v0249_master_switch.py",
        "_master_gate 放过工具栏关闭时的纯工具 → 可用性和拒绝回执的判据必须红",
    ),
]


def main(argv: list[str]) -> int:
    from mutation_all import entry, registered_cases

    parser = argparse.ArgumentParser(description=__doc__)
    cases = registered_cases(sys.modules[__name__])
    parser.add_argument("--only", choices=[c.criterion for c in cases])
    args = parser.parse_args(argv)
    selected = [c for c in cases if not args.only or c.criterion == args.only]
    return entry({"v0249": selected}, legacy=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
