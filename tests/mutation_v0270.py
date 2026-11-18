"""隔离快照内定向改坏控制台。只有约定的断言变红才算检出。"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from _paths import PLUGIN_DIR


CORE = "core/web_console.py"
API = "core/web_api.py"
PLAYER_ADMIN = "core/player_admin.py"
HTML = "pages/mc_control/index.html"
BACKEND_TEST = "tests/test_v0270_web_console.py"
UI_TEST = "tests/ui_web_console_check.py"


@dataclass(frozen=True)
class Mutation:
    criterion: str
    description: str
    path: str
    before: str
    after: str
    case: str
    failure: str
    test: str = BACKEND_TEST

    @property
    def command(self):
        case = f"WebConsoleCheck.{self.case}" if self.test == BACKEND_TEST else self.case
        return [sys.executable, "-B", "-X", "utf8", self.test, case]


MUTATIONS = (
    Mutation(
        "T1", "过滤 stop 命令", CORE,
        "                            await _apply(plugin, line, item, current_mode)",
        "                            if line != 'stop':\n"
        "                                await _apply(plugin, line, item, current_mode)\n"
        "                            else:\n"
        "                                _reject(item, '命令被过滤')",
        "test_t01_send_stop_and_exact_command", "T1 服务端回执须保留",
    ),
    Mutation(
        "T2", "间接提示丢弃嵌套命令原文", CORE,
        'f"嵌套命令：{segment}"', '"间接执行有风险"',
        "test_t02_indirect_text_in_preview", "T2 提示须显示完整的嵌套命令或函数名",
    ),
    Mutation(
        "T3", "倒序发送多行命令", CORE,
        "enumerate(zip(lines, items))", "enumerate(zip(reversed(lines), items))",
        "test_t03_multiline_preserves_order_and_replies", "T3 多行逐条按输入顺序发送",
    ),
    Mutation(
        "T3", "界面只渲染第一条命令回执", HTML,
        "appendConsoleResults(response.results);", "appendConsoleResults(response.results.slice(0, 1));",
        "T3", "T3 回执须按顺序分别显示标题", UI_TEST,
    ),
    Mutation(
        "T4", "选择器正则不识别 @e", CORE,
        "@([earpn])", "@([arpn])",
        "test_t04_selector_scope_strength_and_filter", "T4 目标选择器须给出对应提示强度",
    ),
    Mutation(
        "T5", "分析命令时忽略引号边界", CORE,
        "        if char in \"\\\"'\":", "        if False:",
        "test_t05_literals_do_not_trigger_selector_warning", "T5 引号内或单词内部的文本不可触发选择器提示",
    ),
    Mutation(
        "T6", "命令不存在一律判认不出", CORE,
        "        if state != server_card.STATE_UNSUPPORTED or reply.verdict.status == \"unknown\":",
        "        if True:",
        "test_t06_response_states", "T6 四态须沿用共享判定",
    ),
    Mutation(
        "T6", "空回包判正常", CORE,
        "item.update(player_admin._status(server_card.STATE_UNRECOGNIZED, note, reply.raw))",
        "item.update(player_admin._status(server_card.STATE_OK, note, reply.raw))",
        "test_t06_response_states", "T6 四态须沿用共享判定",
    ),
    Mutation(
        "T6", "请求超时判该世代不支持", CORE,
        "item.update(player_admin._status(server_card.STATE_UNREACHABLE, note, reply.raw),",
        "item.update(player_admin._status(server_card.STATE_UNSUPPORTED, note, reply.raw),",
        "test_t06_response_states", "T6 四态须沿用共享判定",
    ),
    Mutation(
        "T7", "跳过实际审计写入", CORE,
        '        player_admin._write_audit(stream, actor, {\n'
        '            **item, "mode": current_mode, "warnings": batch + item["warnings"],\n'
        '        })',
        "        pass",
        "test_t07_every_line_is_audited_even_when_rejected", "T7 每条命令包括版本或模式拒绝均留一条审计",
    ),
    Mutation(
        "T8", "把找不到日志判成正常", CORE,
        'return _log_result(server_card.STATE_UNREACHABLE, "找不到 logs/latest.log',
        'return _log_result(server_card.STATE_OK, "找不到 logs/latest.log',
        "test_t08_log_missing_empty_local_remote_and_rotated", "T8 日志文件缺失须单独报告",
    ),
    Mutation(
        "T8", "停用轮转判定", CORE,
        "    if rotated:\n", "    if False:\n",
        "test_t08_log_missing_empty_local_remote_and_rotated", "T8 日志轮转须单独报告",
    ),
    Mutation(
        "T8", "暂停期间继续轮询", HTML,
        " && !consoleLogPaused && !document.hidden", " && !document.hidden",
        "T8", "T8 暂停期间不得轮询", UI_TEST,
    ),
    Mutation(
        "T9", "取消终端 POST 管理员校验", API,
        "        if not actor:\n            return self._player_admin_forbidden()\n        request_type",
        "        if False:\n            return self._player_admin_forbidden()\n        request_type",
        "test_t09_permission_applies_to_all_routes", "T9 每条控制台路由均拒绝非管理员或无效令牌",
    ),
    # v0.28.0 口径反转：WebUI 显示真地址、**审计继续脱敏**。所以守的是这两条：
    Mutation(
        "T10", "日志尾又把手遮回去（WebUI 该显示真实地址）", CORE,
        "    lines = watcher._decode_chunk(sample).splitlines()[-_LOG_LINES:]",
        "    lines = [player_admin._redact(line) for line in watcher._decode_chunk(sample).splitlines()[-_LOG_LINES:]]",
        "test_t10_webui_shows_real_addresses_while_audit_stays_redacted", "T10 日志尾应显示真实地址",
    ),
    Mutation(
        "T10", "审计不再脱敏（留档里出现明文地址）", PLAYER_ADMIN,
        '"raw_excerpt": _redact(result["raw"])[:200],', '"raw_excerpt": result["raw"][:200],',
        "test_t07_every_line_is_audited_even_when_rejected", "T7 审计摘要里地址必须被隐藏",
    ),
    Mutation(
        "T11", "删除非实时终端告知", HTML,
        "这不是实时终端（RCON 一问一答）。", "",
        "T11", "T11 界面须说明 RCON 一问一答", UI_TEST,
    ),
    Mutation(
        "T11", "删除命令立即执行告知", HTML,
        "<strong>命令会立即在服务端执行。</strong>", "",
        "T11", "T11 界面须说明命令立即执行", UI_TEST,
    ),
    Mutation(
        "T12", "插入把回执送进上下文的调用", CORE,
        "    reply = await player_admin._send(plugin, command)",
        "    reply = await player_admin._send(plugin, command)\n"
        "    plugin.context.messages.append(reply.raw)",
        "test_t12_console_has_no_llm_context_dependency", "T12 控制台不可向 LLM 上下文送入回执",
    ),
    Mutation(
        "T13", "普通命令长度上限放宽一字", CORE,
        "MAX_COMMAND_LENGTH = 256", "MAX_COMMAND_LENGTH = 257",
        "test_t13_length_limits_and_whitelist", "T13 只送出 256 字与 200 字边界内的命令",
    ),
    Mutation(
        "T13", "广播命令长度上限放宽一字", CORE,
        "MAX_BROADCAST_LENGTH = 200", "MAX_BROADCAST_LENGTH = 201",
        "test_t13_length_limits_and_whitelist", "T13 只送出 256 字与 200 字边界内的命令",
    ),
    Mutation(
        "T14", "拦截命中风险提示的命令", CORE,
        "                        if audit_ok:", '                        if audit_ok and not item["warnings"]:',
        "test_t14_soft_hints_never_block_dispatch", "T14 多种提示同时命中仍须发出两条命令",
    ),
    Mutation(
        "T14", "浏览器禁止提交有提示的命令", HTML,
        '  if(mode === "unknown" || consoleBusy || consoleDenied || !command.trim()) return;',
        '  if(mode === "unknown" || consoleBusy || consoleDenied || !command.trim() || '
        'input.classList.contains("is-warning")) return;',
        "T14", "T14 有提示的命令也要逐条显示回执", UI_TEST,
    ),
)


def run_test(root: Path, command: list[str]):
    return subprocess.run(
        command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def exercise(root: Path, mutation: Mutation) -> bool:
    path = root / mutation.path
    original = path.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    newline = "\r\n" if b"\r\n" in original else "\n"
    before = mutation.before.replace("\n", newline).encode("utf-8")
    after = mutation.after.replace("\n", newline).encode("utf-8")
    if original.count(before) != 1 or before == after:
        print(f"[INVALID] {mutation.criterion} {mutation.description} 锚点必须恰好一处", flush=True)
        return False

    try:
        path.write_bytes(original.replace(before, after, 1))
        result = run_test(root, mutation.command)
        failures = re.findall(
            r"(?ms)^AssertionError: (.*?)(?=^={8,}|^-{8,}|\Z)", result.stderr,
        )
        if result.returncode == 1 and any(mutation.failure in block for block in failures):
            print(f"[RED] {mutation.criterion} {mutation.description} → AssertionError: {mutation.failure}",
                  flush=True)
            return True
        print(f"[INVALID] {mutation.criterion} {mutation.description} rc={result.returncode}", flush=True)
        print((result.stdout + result.stderr)[-2400:], flush=True)
        return False
    finally:
        path.write_bytes(original)
        restored = path.read_bytes()
        if restored != original or hashlib.sha256(restored).hexdigest() != sha:
            raise RuntimeError(f"{mutation.path} 原字节未恢复")
        print(f"[SHA-256] {mutation.path} {sha} 已恢复", flush=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("criteria", nargs="*", help="只运行指定判据，如 T2 T10")
    args = parser.parse_args(argv)
    selected = [mutation for mutation in MUTATIONS if not args.criteria or mutation.criterion in args.criteria]
    if not selected or set(args.criteria) - {mutation.criterion for mutation in selected}:
        parser.error("请输入有效的判据号")
    with tempfile.TemporaryDirectory(prefix="scintilla-console-mutation-") as temporary:
        root = Path(temporary) / PLUGIN_DIR.name
        shutil.copytree(
            PLUGIN_DIR, root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "_shots", "*.pyc", "*.log"),
        )
        for test in sorted({mutation.test for mutation in selected}):
            baseline = run_test(root, [sys.executable, "-B", "-X", "utf8", test])
            if baseline.returncode:
                print(f"[BASELINE FAIL] {test}\n{baseline.stdout}{baseline.stderr}", flush=True)
                return 1
            print(f"[BASELINE PASS] {test}", flush=True)
        results = [exercise(root, mutation) for mutation in selected]
    print(f"定向变异 {sum(results)}/{len(results)} 变红，逐条字节级还原", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
