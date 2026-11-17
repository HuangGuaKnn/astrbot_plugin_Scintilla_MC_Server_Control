"""定向改坏玩家管理实现，只有对应断言明确变红才算检出。"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = "core/player_admin.py"
API = "core/web_api.py"
HTML = "pages/mc_control/index.html"
BACKEND_TEST = "tests/test_v0260_player_admin.py"
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
    case: str = ""

    @property
    def command(self):
        args = [sys.executable, "-B", "-X", "utf8", self.test]
        if self.case:
            args.append(f"PlayerAdminCheck.{self.case}" if self.test == BACKEND_TEST else self.case)
        return args


MUTATIONS = (
    Mutation(
        "P1", "名单丢弃原文", CORE,
        'result = {"command": command, "players": players or [], **_status(state, note, reply.raw)}',
        'result = {"command": command, "players": players or [], **_status(state, note, "")}',
        "P1 unrecognized 原文", BACKEND_TEST, "test_p01_online_states",
    ),
    Mutation(
        "P2", "一律给踢出和封禁加原因", CORE,
        'if action in ("kick", "ban") and reason and caps.at_least(ITEM_PREFLATTEN_CUTOVER):',
        'if action in ("kick", "ban") and reason:',
        "P2 1.7.10 kick 原因世代分叉", BACKEND_TEST, "test_p02_reason_depends_on_generation",
    ),
    Mutation(
        "P3", "踢出后无视仍在名单里的玩家", CORE,
        '    if present != (action in ("ban", "whitelist_add")):',
        '    if present != (action in ("ban", "whitelist_add")) and action != "kick":',
        "P3 踢出仅在目标从完整名单消失时确认", BACKEND_TEST,
        "test_p03_kick_requires_absence_in_complete_list",
    ),
    Mutation(
        "P4", "删除正常封禁和解封的名单回读", CORE,
        '    readback = await _read_roster(plugin, _READBACK_COMMAND[action], reply.rcon)',
        '    readback = ({"state": "unrecognized", "note": "无名单", "raw": ""}\n'
        '                if action in ("ban", "unban") else\n'
        '                await _read_roster(plugin, _READBACK_COMMAND[action], reply.rcon))',
        "P4 封禁/解封必须回读 banlist players", BACKEND_TEST,
        "test_p04_ban_and_unban_require_banlist",
    ),
    Mutation(
        "P5", "白名单写回执跳过成员验证", CORE,
        '    if present != (action in ("ban", "whitelist_add")):',
        '    if present != (action in ("ban", "whitelist_add")) and not action.startswith("whitelist_"):',
        "P5 白名单增删须确认目标成员关系", BACKEND_TEST,
        "test_p05_whitelist_requires_membership_readback",
    ),
    Mutation(
        "P6", "写接口放行非管理员", API,
        "        if not actor:\n            return self._player_admin_forbidden()",
        "        if False:\n            return self._player_admin_forbidden()",
        "P6 action 拒绝直接调用者", BACKEND_TEST,
        "test_p06_direct_routes_require_dashboard_administrator",
    ),
    Mutation(
        "P6", "前端不再要求显式授权", HTML,
        "const admin = bans?.authorized === true;", "const admin = Boolean(bans);",
        "P6 非管理员仍有玩家管理按钮", UI_TEST, "P6",
    ),
    Mutation(
        "P7", "删除 JSONL 写入", CORE,
        "_write_audit(audit, actor, result)", "pass",
        "P7 一次写操作严格一行", BACKEND_TEST, "test_p07_all_dispatched_writes_are_audited",
    ),
    Mutation(
        "P7", "审计原文不截断", CORE,
        '"raw_excerpt": result["raw"][:200]', '"raw_excerpt": result["raw"]',
        "P7 审计原文截断到 200 字符", BACKEND_TEST, "test_p07_all_dispatched_writes_are_audited",
    ),
    Mutation(
        "P8", "名单失效时按钮仍可点", HTML,
        'const disabled = enabled ? "" : "disabled";', 'const disabled = "";',
        "P8 名单不可用时在线玩家操作没有全部 disabled", UI_TEST, "P8",
    ),
    Mutation(
        "P9", "注入模拟地址到在线名单组件", HTML,
        'return `<div class="player-actions">${buttons.join("")}</div>`;',
        'return `<div class="player-actions">192.0.2.9${buttons.join("")}</div>`;',
        "P9 DOM 出现 IPv4 地址", UI_TEST, "P9",
    ),
    Mutation(
        "P9", "跳过服务器文本的地址脱敏", HTML,
        "const escServer = value => esc(redactServerText(value));",
        "const escServer = value => esc(value);",
        "P9 DOM 出现 IPv4 地址", UI_TEST, "P9",
    ),
    Mutation(
        "P10", "忽略命令拒绝，只看名单", CORE,
        "    if not reply.verdict.ok:", "    if False:",
        "P10 服务端拒绝不可报成功", BACKEND_TEST,
        "test_p10_rejected_write_never_succeeds_even_if_target_absent",
    ),
    Mutation(
        "P11", "文件无详情时填空串", CORE,
        '_UNKNOWN = "未知"', '_UNKNOWN = ""',
        "P11 未配置目录时详情必须明写未知", BACKEND_TEST,
        "test_p11_remote_banlist_preserves_runtime_names_and_unknown_details",
    ),
    Mutation(
        "P11", "改用文件作为名单来源", CORE,
        '        names = list(listed["players"])',
        '        names = [str(r["name"]) for r in (details or {}).values() if isinstance(r.get("name"), str)]',
        "P11 名单来自运行时", BACKEND_TEST,
        "test_p11_remote_banlist_preserves_runtime_names_and_unknown_details",
    ),
    Mutation(
        "P11", "隐藏运行时与文件差异条数", CORE,
        'result["mismatch_count"] = len(keys.symmetric_difference(details))',
        'result["mismatch_count"] = 0',
        "P11 明示运行时与文件差异", BACKEND_TEST,
        "test_p11_local_file_enriches_but_cannot_change_membership",
    ),
    Mutation(
        "P12", "解封时忽略回读中的目标", CORE,
        '    if present != (action in ("ban", "whitelist_add")):',
        '    if present != (action in ("ban", "whitelist_add")) and action != "unban":',
        "P12 仍在封禁名单不可报已解封", BACKEND_TEST,
        "test_p12_unban_on_banlist_must_remove_target",
    ),
    Mutation(
        "P12", "前端把未确认画成成功", HTML,
        "const confirmed = result?.ok === true && result?.confirmed === true;",
        "const confirmed = result?.ok === true;",
        "P12 未确认的解封被报为成功", UI_TEST, "P12",
    ),
    Mutation(
        "P13", "源码出现 IP 封禁文件名", CORE,
        '    path = Path(server_dir) / "banned-players.json"',
        '    path = Path(server_dir) / "banned-players.json"  # banned-ips.json',
        "P13 源码禁止触及 IP 封禁文件", BACKEND_TEST, "test_p13_no_ip_ban_file",
    ),
    # v0.26.1（2026-11-17 现场）：主人手动在控制台 ban 了 123，两条封禁在 RCON 回包里
    # 粘成一行，旧实现整卡空着 —— 明明 banned-players.json 就在手边。
    Mutation(
        "v0.26.1", "回包粘连时不再用文件兜底", CORE,
        '        names = [str(record["name"]) for record in details.values()\n                 if isinstance(record.get("name"), str)]',
        "        names = []",
        "demoplayer", BACKEND_TEST,
        "test_bans_fall_back_to_file_when_rcon_reply_is_glued",
    ),
    Mutation(
        "v0.26.1", "兜底时把 removable 一律关掉（解封按钮全灰）", CORE,
        '            "removable": bool(_PLAYER_NAME.fullmatch(name)),',
        '            "removable": False,',
        "False is not true", BACKEND_TEST,
        "test_bans_fall_back_to_file_when_rcon_reply_is_glued",
    ),
)


def run_test(command):
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def exercise(mutation):
    path = ROOT / mutation.path
    original = path.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    source = original.decode("utf-8")
    before, after = mutation.before, mutation.after
    if "\r\n" in source:
        before, after = (text.replace("\n", "\r\n") for text in (before, after))
    if before == after or source.count(before) != 1:
        print(f"[INVALID] {mutation.criterion} {mutation.description} 锚点必须恰好一处")
        return False

    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write(source.replace(before, after, 1))
        result = run_test(mutation.command)
        matching = ""
        for block in result.stderr.split("\n======================================================================\n"):
            _, separator, assertion = block.partition("AssertionError:")
            if separator and mutation.failure in assertion:
                first_line = assertion.splitlines()[0].strip()
                matching = f"AssertionError: {first_line}"
                if mutation.failure not in first_line:
                    matching += f" ｜ {mutation.failure}"
                break
        red = result.returncode == 1 and bool(matching)
        if red:
            print(f"[RED] {mutation.criterion} {mutation.description} → {matching}")
        else:
            print(f"[INVALID] {mutation.criterion} {mutation.description} rc={result.returncode}")
            print((result.stdout + result.stderr)[-2300:])
        return red
    finally:
        with io.open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write(source)
        restored = path.read_bytes()
        if restored != original or hashlib.sha256(restored).hexdigest() != original_sha:
            raise RuntimeError(f"{mutation.path} 原字节未恢复")
        print(f"[SHA-256] {mutation.path} {original_sha} 已恢复")


def main():
    # -B prevents writing mutated bytecode. Fresh modules in the subprocess also
    # need to avoid the original checkout's existing timestamp-based cache.
    os.environ["PYTHONPYCACHEPREFIX"] = str(
        Path(os.environ.get("TEMP", os.environ.get("TMPDIR", ".")))
        / f"scintilla-mutation-pycache-{os.getpid()}"
    )
    if not MUTATIONS:
        print("[INVALID] 没有配置变异")
        return 1
    commands = {tuple(mutation.command[:5]) for mutation in MUTATIONS}
    for command in sorted(commands):
        baseline = run_test(command)
        if baseline.returncode:
            print(f"[BASELINE FAIL] {command[-1]}\n{baseline.stdout}{baseline.stderr}")
            return 1
        print(f"[BASELINE PASS] {command[-1]}")
    results = [exercise(mutation) for mutation in MUTATIONS]
    total = len(results)
    print(f"定向变异 {sum(results)}/{total} 变红，逐条字节级还原")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
