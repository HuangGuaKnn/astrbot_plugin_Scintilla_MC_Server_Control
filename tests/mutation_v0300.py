"""v0.30.0：在隔离副本改坏 P16/P17/P18/P20，仅目标断言变红才算检出。"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from _paths import PLUGIN_DIR
from mutation_v0270 import exercise, run_test


HTML = "pages/mc_control/index.html"
LOG = "core/log_watcher.py"
CONSOLE = "core/web_console.py"
PLAYER_TEST = "tests/test_v0260_player_admin.py"
CONSOLE_TEST = "tests/test_v0270_web_console.py"
UI_TEST = "tests/ui_player_admin_check.py"


@dataclass(frozen=True)
class Mutation:
    criterion: str
    description: str
    path: str
    before: str
    after: str
    failure: str
    test: str
    case: str

    @property
    def command(self):
        return [sys.executable, "-B", "-X", "utf8", self.test, self.case]


MUTATIONS = (
    Mutation(
        "P16", "删除在线行动作过滤", HTML,
        'const ONLINE_ACTIONS = Object.keys(PLAYER_ACTIONS)\n'
        '  .filter(action => action !== "unban" && action !== "unban_ip" && action !== "ban_ip");',
        'const ONLINE_ACTIONS = Object.keys(PLAYER_ACTIONS);',
        "P16 在线玩家名动作应恰为四项", UI_TEST, "P16",
    ),
    Mutation(
        "P17", "禁用首轮回填", LOG,
        "        if not self._ip_backfill_pending:\n            return True",
        "        if True:\n            return True",
        "P17 首轮必须回填两个玩家的正确地址", PLAYER_TEST,
        "PlayerAdminCheck.test_p17_first_poll_backfills_two_ips_without_replaying_events",
    ),
    Mutation(
        "P17", "回填错误地派发历史事件", LOG,
        "        for line in self._decode_chunk(data).splitlines():\n            self._note_login_ip(line)",
        "        for line in self._decode_chunk(data).splitlines():\n"
        "            self._note_login_ip(line)\n"
        "            await self.on_event('join', 'Alex', '')",
        "Expected mock to not have been awaited", PLAYER_TEST,
        "PlayerAdminCheck.test_p17_first_poll_backfills_two_ips_without_replaying_events",
    ),
    Mutation(
        "P17", "每批新日志清空 IP 索引", LOG,
        "        self.last_read_at = time.monotonic()\n        for line in self._decode_chunk(data).splitlines():",
        "        self.last_read_at = time.monotonic()\n        self._recent_ips.clear()\n"
        "        for line in self._decode_chunk(data).splitlines():",
        "P17 跨轮询保留历史 IP，不能每批清空", PLAYER_TEST,
        "PlayerAdminCheck.test_p17_first_poll_backfills_two_ips_without_replaying_events",
    ),
    Mutation(
        "P18", "从 A 组删除 reset", CONSOLE,
        '    "reset": "插件命令，具体语义依插件而定；可能重置数据或重启服务器，中断在线服务。",\n',
        "", "P18 服务中断别称须命中 A 组提示", CONSOLE_TEST,
        "WebConsoleCheck.test_p18_service_aliases_warn_without_changing_dispatch",
    ),
    Mutation(
        "P18", "降低 reset 提示强度", CONSOLE,
        'level = "medium" if name in ("save-all", "save-on") else "high"',
        'level = "medium" if name in ("save-all", "save-on", "reset") else "high"',
        "P18 A 组提示强度须为 high", CONSOLE_TEST,
        "WebConsoleCheck.test_p18_service_aliases_warn_without_changing_dispatch",
    ),
    Mutation(
        "P18", "删除 reset 插件语义说明", CONSOLE,
        '"插件命令，具体语义依插件而定；可能重置数据或重启服务器，中断在线服务。"',
        '"会重启服务器。"', "P18 提示须说明插件语义", CONSOLE_TEST,
        "WebConsoleCheck.test_p18_service_aliases_warn_without_changing_dispatch",
    ),
    Mutation(
        "P18", "删除终端常驻的表外插件命令说明", HTML,
        '；<strong>插件命令不在表内，同样会立即执行。</strong>',
        "。", "P18 常驻说明缺少插件命令立即执行提醒", UI_TEST, "P18",
    ),
    Mutation(
        "P20", "恢复旧手动颜色卡", HTML,
        '  <div class="card" id="srv_color_card" tabindex="-1">',
        '  <div class="card"><h3><span class="dot" style="background:var(--amber)"></span>'
        '文本颜色 · 手动设置 <span class="right"><button type="button" class="jump" '
        'id="jump_text_color">可视化调色 →</button></span></h3></div>\n'
        '  <div class="card" id="srv_color_card" tabindex="-1">',
        "P20 文本颜色卡片应恰为 1", UI_TEST, "P20",
    ),
    Mutation(
        "P20", "使用默认值覆盖聊天栏颜色初值", HTML,
        '    const v = r.colors[key] || CLR_DEF[key];',
        '    const v = key === "color_say" ? CLR_DEF[key] : r.colors[key] || CLR_DEF[key];',
        "P20 三个颜色控件须使用接口初值", UI_TEST, "P20",
    ),
    Mutation(
        "P20", "移除文本编辑到取色器的同步", HTML,
        '      if(/^#[0-9a-fA-F]{6}$/.test(v)) $(pick).value = v;',
        "", "P20 文本编辑须同步取色器", UI_TEST, "P20",
    ),
    Mutation(
        "P20", "保存全部设置遗漏颜色草稿", HTML,
        '  if(colorDraftReady) Object.assign(settings, collectColors());',
        "", "P20 保存全部设置未提交唯一可视化草稿", UI_TEST, "P20",
    ),
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("criteria", nargs="*", help="仅运行指定判据，例如 P17 P18")
    args = parser.parse_args(argv)
    selected = [m for m in MUTATIONS if not args.criteria or m.criterion in args.criteria]
    if not selected or set(args.criteria) - {m.criterion for m in selected}:
        parser.error("请输入有效的判据号")
    original_hashes = {m.path: hashlib.sha256((PLUGIN_DIR / m.path).read_bytes()).hexdigest()
                       for m in selected}
    with tempfile.TemporaryDirectory(prefix="scintilla-v0300-mutation-") as temporary:
        root = Path(temporary) / PLUGIN_DIR.name
        shutil.copytree(PLUGIN_DIR, root,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "_shots", "*.pyc", "*.log"))
        for command in sorted({tuple(m.command) for m in selected}):
            baseline = run_test(root, list(command))
            if baseline.returncode:
                print(f"[BASELINE FAIL] {command[-1]}\n{baseline.stdout}{baseline.stderr}", flush=True)
                return 1
            print(f"[BASELINE PASS] {command[-1]}", flush=True)
        results = []
        for mutation in selected:
            source = (root / mutation.path).read_text(encoding="utf-8")
            offset = source.find(mutation.before)
            line = source.count("\n", 0, offset) + 1 if offset >= 0 else "?"
            print(f"[TARGET] {mutation.criterion} {mutation.path}:{line} → {mutation.case}", flush=True)
            results.append(exercise(root, mutation))
    for path, sha in original_hashes.items():
        if hashlib.sha256((PLUGIN_DIR / path).read_bytes()).hexdigest() != sha:
            raise RuntimeError(f"源工作区发生变化：{path}")
        print(f"[SOURCE UNCHANGED] {path} {sha}", flush=True)
    print(f"定向变异 {sum(results)}/{len(results)} 变红，隔离副本逐条字节级还原", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
