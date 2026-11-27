"""定向改坏玩家管理实现，只有对应断言明确变红才算检出。"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

CORE = "core/player_admin.py"
LWCORE = "core/log_watcher.py"      # 与上面 CORE 同一风格：相对仓库根的路径

API = "core/web_api.py"
HTML = "pages/mc_control/index.html"
BACKEND_TEST = "tests/test_v0260_player_admin.py"
#: v0.28.0：审计脱敏这条判据现在由终端用例守（v0.26 的 P07 只验「一行一条」）
TERMINAL_TEST = "tests/test_v0270_web_console.py"
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
            test_class = {BACKEND_TEST: "PlayerAdminCheck", TERMINAL_TEST: "WebConsoleCheck"}.get(self.test)
            args.append(f"{test_class}.{self.case}" if test_class else self.case)
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
        'if action in ("kick", "ban", "ban_ip") and reason and caps.at_least(ITEM_PREFLATTEN_CUTOVER):',
        'if action in ("kick", "ban", "ban_ip") and reason:',
        "P2 1.7.10 kick 原因世代分叉", BACKEND_TEST, "test_p02_reason_depends_on_generation",
    ),
    Mutation(
        "P3", "踢出后无视仍在名单里的玩家", CORE,
        '    if present != (action in ("ban", "ban_ip", "whitelist_add")):',
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
        '    if present != (action in ("ban", "ban_ip", "whitelist_add")):',
        '    if present != (action in ("ban", "whitelist_add")) and not action.startswith("whitelist_"):',
        "P5 白名单增删须确认目标成员关系", BACKEND_TEST,
        "test_p05_whitelist_requires_membership_readback",
    ),
    Mutation(
        "P6", "写接口放行非管理员", API,
        "    async def player_action(self):\n        actor = self._player_admin_username()\n        if not actor:\n            return self._player_admin_forbidden()",
        "    async def player_action(self):\n        actor = self._player_admin_username()\n        if False:\n            return self._player_admin_forbidden()",
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
        "P7", "终端审计回执写入时不脱敏", CORE,
        '"raw_excerpt": _redact(result["raw"])[:200],\n        }\n    else:', '"raw_excerpt": result["raw"][:200],\n        }\n    else:',
        "T7 审计摘要须先脱敏、再截取 200 字符", TERMINAL_TEST,
        "test_t07_every_line_is_audited_even_when_rejected",
    ),
    Mutation(
        "P8", "名单失效时按钮仍可点", HTML,
        'const disabled = enabled ? "" : "disabled";', 'const disabled = "";',
        "P8 名单不可用时在线玩家操作没有全部 disabled", UI_TEST, "P8",
    ),
    # v0.25.4（2026-11-26 现场）：异地模式读不到本机文件时必须点明原因，否则用户只看到
    # 「回执认不出」，以为是 bug。删掉这段说明，对应断言必须红。
    Mutation(
        "P22", "异地模式认不出却不解释", CORE,
        '        hint = _remote_unverified_hint(plugin, listed, "banned-players.json")\n'
        '        if hint:\n'
        '            result["note"] = hint\n'
        '            result["note_tone"] = "warn"          # 前端据此用设置页那套琥珀色警示样式',
        '        hint = ""',
        "P22 异地模式认不出必须点明原因", BACKEND_TEST,
        "test_bans_without_readable_file_still_refuse_to_guess",
    ),
    # v0.25.4（2026-11-26 第二次反馈）：提示必须是「警示」色调，否则灰字仍会被当成普通说明。
    Mutation(
        "P23", "异地提示丢了警示色调", CORE,
        '            result["note_tone"] = "warn"          # 前端据此用设置页那套琥珀色警示样式\n',
        '',
        "P23 异地提示必须走警示色调", BACKEND_TEST,
        "test_bans_without_readable_file_still_refuse_to_guess",
    ),
    # v0.29.0：玩家 IP 索引。守「进服行真的被记下来了」——
    # 不记的话在线名单永远显示「IP 未知」，一键封禁也就没了对象。
    Mutation(
        "P15", "进服 IP 不被记进索引", LWCORE,
        '        self._recent_ips[match.group("player").casefold()] = (ip, time.monotonic())',
        '        pass',
        "P15 进服行必须写入 IP 索引", BACKEND_TEST, "test_p15_login_ip_index_is_read_only_and_case_insensitive",
    ),
    # v0.29.0：IP 封禁。守两件事 —— 网段不许放过（原版 ban-ip 只吃单地址）、
    # 回读必须查 IP 名单而不是玩家名单。
    # 注：不能把「删掉 `if "/" in value`」当变异 —— `ipaddress.ip_address("x/24")` 本身
    # 就会抛错，那个分支只让报错更准，删了用例照样绿（第一版就选错了目标，实测过）。
    # 真正该守的是「地址有没有被校验并规范化」。
    Mutation(
        "P14", "地址不校验也不规范化（非法串与 IPv6 写法直接透传）", CORE,
        '        return str(ipaddress.ip_address(value))',
        '        return value',
        "ban-ip 2001:db8::1", BACKEND_TEST, "test_p14_ip_ban_actions",
    ),
    Mutation(
        "P14", "IP 封禁回读查了玩家名单", CORE,
        '    "ban_ip": "banlist ips",',
        '    "ban_ip": "banlist players",',
        "'banlist players' != 'banlist ips'", BACKEND_TEST, "test_p14_ip_ban_actions",
    ),
    # v0.28.0 口径反转：WebUI 现在**必须**显示真实地址（服主要靠它封 IP）。
    # 原来那两条「注入地址 / 跳过脱敏」已经失去意义 —— 守的方向改成「别又把手遮回去」。
    Mutation(
        "P9", "前端又把地址遮回去", HTML,
        'const escServer = value => esc(value);',
        'const escServer = value => esc(String(value ?? "").replace(/\\d+\\.\\d+\\.\\d+\\.\\d+/g, "[地址已隐藏]"));',
        "P9 封禁原因里的真实 IPv4 应显示在界面", UI_TEST, "P9",
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
        '    if present != (action in ("ban", "ban_ip", "whitelist_add")):',
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
    # v0.28.0 口径反转：IP 封禁名单从「禁止读」改成「必须读」——
    # 守的方向调成「别把它弄丢」：把读取的文件名改坏，P13 正向断言必须红。
    Mutation(
        "P13", "IP 封禁名单不再读取（banned-ips.json 被弄丢）", CORE,
        '    path = Path(server_dir) / "banned-ips.json"',
        '    path = Path(server_dir) / "banned-ips-disabled.json"',
        "P13 必须真的读取 IP 封禁名单", BACKEND_TEST,
        "test_p13_ip_ban_file_is_supported_but_only_that_one",
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
        "WL", "玩家管理快照漏掉白名单", API,
        '        result["whitelist"] = await player_admin.get_whitelist(self.plugin)\n',
        "",
        "WL 管理员快照缺少白名单", BACKEND_TEST,
        "test_whitelist_snapshot_preserves_complete_empty_and_failed_reads",
    ),
    Mutation(
        "WL", "在线行对白名单成员仍提供加入操作", HTML,
        '...ONLINE_ACTIONS, whitelistMembers.has(name.toLowerCase()) ? "whitelist_remove" : "whitelist_add",',
        '...ONLINE_ACTIONS, "whitelist_add",',
        "P16 在线玩家应恰有踢出、封禁和对应的白名单操作", UI_TEST, "P16",
    ),
    Mutation(
        "WL", "白名单回读失败被当成未入名单", HTML,
        'const actions = whitelistMembers === null ? ONLINE_ACTIONS : [',
        'const actions = whitelistMembers === null ? [...ONLINE_ACTIONS, "whitelist_add"] : [',
        "WL 回读失败时不能猜测在线玩家白名单状态", UI_TEST, "WL",
    ),
    Mutation(
        "v0.26.1", "兜底时把 removable 一律关掉（解封按钮全灰）", CORE,
        '            "removable": bool(_PLAYER_NAME.fullmatch(name)),',
        '            "removable": False,',
        "False is not true", BACKEND_TEST,
        "test_bans_fall_back_to_file_when_rcon_reply_is_glued",
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
    return entry({"v0260": selected}, legacy=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
