"""WebUI 玩家操作、服务端名单回读和 JSONL 审计。"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import server_card
from .command_result import CommandResult, classify_command_output
from .rcon import RconError
from .strict_bool import strict_bool
from .version_caps import ITEM_PREFLATTEN_CUTOVER, VersionInfo


ACTIONS = ("kick", "ban", "unban", "whitelist_add", "whitelist_remove", "ban_ip", "unban_ip")
#: IP 封禁动作：目标是 IP 而不是玩家名，校验与回读都另走一条
_IP_ACTIONS = frozenset({"ban_ip", "unban_ip"})
_WRITE_PREFIX = {
    "kick": "kick",
    "ban": "ban",
    "unban": "pardon",
    "whitelist_add": "whitelist add",
    "whitelist_remove": "whitelist remove",
    "ban_ip": "ban-ip",
    "unban_ip": "pardon-ip",
}
_READBACK_COMMAND = {
    "kick": "list",
    "ban": "banlist players",
    "unban": "banlist players",
    "whitelist_add": "whitelist list",
    "whitelist_remove": "whitelist list",
    "ban_ip": "banlist ips",
    "unban_ip": "banlist ips",
}
_PLAYER_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
#: `banlist ips` 的确定形态：`<ip> was banned by <source>: <reason>`。
#: 只认这种，不单独抓「看起来像 IP 的串」—— 理由里可能就写着别人的 IP。
_IP_ENTRY = re.compile(r"(?P<ip>[0-9A-Fa-f:.]+)\s+was banned by\s+(?P<src>[^:]+):\s*(?P<reason>.*)", re.I)
_BAN_COUNT = re.compile(r"There are\s+(\d+)\b", re.I)
_NO_BANS = re.compile(r"There are no (?:IP )?bans?|total banned IPs:\s*$", re.I)


def normalize_ip(text: str) -> str:
    """校验并规范化 IP。原版 `ban-ip` 只接受单个地址，不支持 CIDR 网段。"""
    if not isinstance(text, str):
        raise ValueError("IP 需要字符串")
    value = text.strip()
    if not value:
        raise ValueError("IP 不能为空")
    if "/" in value:
        raise ValueError("原版 ban-ip 只接受单个地址，不支持 CIDR 网段")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ValueError("不是合法的 IPv4 / IPv6 地址") from exc


def _parse_ip_bans(text: str) -> list[str] | None:
    """解析 `banlist ips`。只认「IP + was banned by」的确定形态，**不许猜**。

    声明数量与条目数不符时返回 None（沿用 v0.26 对 `/list` 的口径）：
    少一条 IP 就意味着页面显示不全，而 IP 列表是拿来做安全判断的，宁可报「认不出」。
    """
    clean = server_card.strip_color_codes(text or "").strip()
    if not clean:
        return None
    if _NO_BANS.search(clean):
        return []
    found: list[str] = []
    for match in _IP_ENTRY.finditer(clean):
        candidate = match.group("ip")
        try:
            address = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    declared = _BAN_COUNT.search(clean)
    if declared and int(declared.group(1)) != len(found):
        return None
    return found or None
_IPV4 = re.compile(r"(?<!\d)\d+(?:\.\d+){3}")
_IP_CANDIDATE = re.compile(r"(?<![\w:])[a-fA-F0-9:.]+(?:%[A-Za-z0-9_.-]+)?(?![\w:])")
_LEGACY_BANS = re.compile(r"There are (\d+) total banned players:\s*(.*)", re.DOTALL)
_MODERN_BANS = re.compile(r"There are (\d+) ban(?:s|\(s\)):\s*(.*)", re.DOTALL)
_BAN_ENTRY = re.compile(r"([A-Za-z0-9_]{1,16}) was banned by [^:\r\n]+: [^\r\n]*")
_WHITELIST = re.compile(
    r"There are (\d+)(?: \(out of \d+ seen\))? whitelisted player(?:s|\(s\)):\s*(.*)",
    re.DOTALL,
)
_UNKNOWN = "未知"
_DETAIL_FIELDS = ("reason", "created", "expires", "source")
_AUDIT_FIELDS = ("ts", "actor", "action", "player", "command", "state", "confirmed", "raw_excerpt")
_AUDIT_LIMIT = 100


def _redact(text: str) -> str:
    clean = server_card.strip_color_codes(text)

    def redact_ipv6(match: re.Match) -> str:
        value = match.group()
        candidate = value.rstrip(".")
        if ":" in candidate:
            host, _, port = candidate.rpartition(":")
            alternatives = (candidate,)
            if len(port) <= 5 and port.isdecimal() and int(port) <= 65535:
                alternatives += (host,)
            for address in alternatives:
                try:
                    if isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address):
                        return "[IP 已隐藏]" + value[len(address):]
                except ValueError:
                    continue
        return value

    return _IPV4.sub("[IP 已隐藏]", _IP_CANDIDATE.sub(redact_ipv6, clean))


def build_command(action, player, reason, caps: VersionInfo) -> str:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("不支持的玩家操作")
    if action in _IP_ACTIONS:
        target = normalize_ip(player)          # 非法地址在这里抛 ValueError
    elif not isinstance(player, str) or not _PLAYER_NAME.fullmatch(player):
        raise ValueError("玩家名须由 1–16 个字母、数字或下划线组成")
    else:
        target = player
    if not isinstance(reason, str):
        raise ValueError("reason 需要字符串")
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in reason):
        raise ValueError("理由不能包含换行或控制字符")
    # 理由里允许出现 IP：本版起支持 IP 封禁，管理员写「封掉 192.0.2.9 的挂」是正常需求。
    # 控制字符仍然禁止（上面一步已挡），因为那会破坏命令行的单行语义。
    if "\u00a7" in reason:
        raise ValueError("理由不能包含颜色控制码")
    reason = reason.strip()
    if len(reason) > 200:
        raise ValueError("理由最多 200 个字符")
    command = f"{_WRITE_PREFIX[action]} {target}"
    # 2026-11-17 矩阵：1.7.10/1.12.2 的无理由 kick 可进入目标查询，ban 实际也接受理由。
    # MVP 对这两个旧版本省略理由；1.13.2 起已验证 kick 带理由进入目标查询、ban 带理由可保存。
    # 八台均接受 pardon；较新版本均拒绝 unban 别名，旧世代由版本守卫拒绝该别名。
    if action in ("kick", "ban", "ban_ip") and reason and caps.at_least(ITEM_PREFLATTEN_CUTOVER):
        command += f" {reason}"
    return command


@dataclass
class _Reply:
    rcon: object | None = None
    raw: str = ""
    error: str = ""
    blocked: str = ""
    may_have_run: bool = False
    verdict: CommandResult | None = None


def _guard(plugin, command: str) -> str:
    return plugin._guard_command_for_version(command, source="web_player_admin")


async def _send(plugin, command: str, rcon=None) -> _Reply:
    blocked = _guard(plugin, command)
    if blocked:
        return _Reply(blocked=blocked)
    try:
        if rcon is None:
            rcon = await plugin._get_rcon()
        # 默认空哨兵是 RCON 帧的结束标记。配置了非空命令时同样过版本守卫。
        probe = getattr(rcon, "probe_command", "")
        if getattr(rcon, "end_mode", "") == "sentinel" and probe:
            blocked = _guard(plugin, probe)
            if blocked:
                return _Reply(blocked=blocked)
        raw = str(await rcon.command(command) or "")
    except Exception as exc:  # RCON 连接、认证、超时均可能抛出异常。
        may_have_run = rcon is not None and (
            not isinstance(exc, RconError) or exc.command_may_have_run
        )
        return _Reply(
            rcon=rcon, raw=getattr(exc, "partial", ""),
            error=str(exc) or type(exc).__name__, may_have_run=may_have_run,
        )
    verdict = classify_command_output(
        command,
        raw,
        boundary_confirmed=getattr(rcon, "last_boundary_confirmed", None),
        response_received=bool(getattr(rcon, "last_response_received", False)),
    )
    return _Reply(rcon=rcon, raw=raw, verdict=verdict)


def _status(state: str, note: str, raw: str) -> dict:
    """构造状态字典。**这里刻意不脱敏**：
    WebUI 是管理员专属页面，服主本来就有 RCON 权限，藏 IP 只会让 ban-ip 难用；
    聊天工具走的是另一条链路、不经过这里；唯一继续脱敏的是审计文件（_write_audit）。
    """
    return {
        "state": state,
        "state_label": server_card.STATE_LABEL[state],
        "note": note,
        "raw": raw,
    }


def _valid_names(names: list[str], expected_count: int) -> bool:
    return (
        len(names) == expected_count
        and len({name.casefold() for name in names}) == expected_count
        and all(_PLAYER_NAME.fullmatch(name) for name in names)
    )


def _joined_names(tail: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s+and\s+|,\s*", tail)] if tail else []


def _parse_bans(text: str) -> list[str] | None:
    # 旧 Forge 是逗号 / and 分隔的纯名单。新世代是带描述的条目。
    # 矩阵上只有 NeoForge 在多条现代记录间保留换行。其余服务端会粘合理由和下一条
    # 玩家名，无法可靠确定边界；这类回包交给 judge_reply 说明「认不出」。
    if text == "There are no bans":
        return []
    legacy = _LEGACY_BANS.fullmatch(text)
    if legacy:
        names = _joined_names(legacy[2])
        return names if _valid_names(names, int(legacy[1])) else None
    modern = _MODERN_BANS.fullmatch(text)
    if not modern:
        return None
    names = []
    for line in modern[2].splitlines():
        entry = _BAN_ENTRY.fullmatch(line.strip())
        if entry is None:
            return None
        names.append(entry[1])
    return names if _valid_names(names, int(modern[1])) else None


def _parse_whitelist(text: str) -> list[str] | None:
    if text == "There are no whitelisted players":
        return []
    match = _WHITELIST.fullmatch(text)
    if not match:
        return None
    names = _joined_names(match[2])
    return names if _valid_names(names, int(match[1])) else None


async def _read_roster(plugin, command: str, rcon=None) -> dict:
    reply = await _send(plugin, command, rcon)
    players = None
    online = None
    complete = reply.verdict is not None and reply.verdict.boundary_confirmed is not False
    if complete and reply.verdict.status not in ("failed", "syntax_error"):
        text = server_card.strip_color_codes(reply.raw).strip()
        if command == "list":
            online = server_card.parse_list_reply(text)
            if online is not None and _valid_names(online["players"], online["online"]):
                players = online["players"]
        elif command == "banlist players":
            players = _parse_bans(text)
        elif command == "banlist ips":
            players = _parse_ip_bans(text)
        else:
            players = _parse_whitelist(text)

    if reply.blocked:
        state, note = server_card.STATE_UNSUPPORTED, reply.blocked
    else:
        state, note = server_card.judge_reply(
            command, reply.raw, parsed=players is not None, error=reply.error
        )
        if state == server_card.STATE_UNRECOGNIZED and not complete and reply.verdict is not None:
            note = "服务端响应边界未确认，名单可能不完整"
        if command == "list" and state == server_card.STATE_OK and not players:
            state, note = server_card.STATE_EMPTY, "服务器当前没有玩家在线"

    result = {"command": command, "players": players or [], **_status(state, note, reply.raw)}
    if command == "list":
        result.update(online=online["online"] if online else None, max=online["max"] if online else None)
    return result


async def get_online(plugin) -> dict:
    return await _read_roster(plugin, "list")


def _ban_details(plugin) -> tuple[dict[str, dict] | None, str]:
    server_dir = str(plugin._cfg("server_dir", "") or "").strip()
    if not server_dir:
        return None, "未知（server_dir 未配置，读不到 banned-players.json）"
    if strict_bool(plugin._cfg("remote_rcon_mode", False), False, key="remote_rcon_mode"):
        return None, "未知（异地 RCON 模式下读不到 banned-players.json）"
    path = Path(server_dir) / "banned-players.json"
    try:
        if path.is_symlink():
            return None, "未知（banned-players.json 是符号链接）"
        with io.open(path, encoding="utf-8-sig") as stream:
            records = json.load(stream)
    except (OSError, ValueError):
        return None, "未知（无法读取 banned-players.json）"
    if not isinstance(records, list):
        return None, "未知（banned-players.json 格式无法识别）"

    details = {}
    for record in records:
        name = record.get("name") if isinstance(record, dict) else None
        if not isinstance(name, str) or not _PLAYER_NAME.fullmatch(name) or name.casefold() in details:
            return None, "未知（banned-players.json 格式无法识别）"
        details[name.casefold()] = record
    return details, ""


def _ip_bans(plugin) -> tuple[dict[str, dict] | None, str]:
    """读 `banned-ips.json` —— IP 封禁的权威名单。

    v0.28.0 起**解锁**：这个文件以前被列进禁区（含 IP），但 WebUI 是管理员专属页面，
    而 `ban-ip` 是服主真实会用的功能 —— 藏起来只会让人去翻文件、或者靠肉眼认日志。

    容忍单条脏数据：一条格式不对的记录跳过，不让整份名单作废（名单宁可少一条，
    也别因为一个坏字段变成"全都看不到"）；但整个文件读不出来时如实返回 None。
    """
    server_dir = str(plugin._cfg("server_dir", "") or "").strip()
    if not server_dir:
        return None, "未知（server_dir 未配置，读不到 banned-ips.json）"
    if strict_bool(plugin._cfg("remote_rcon_mode", False), False, key="remote_rcon_mode"):
        return None, "未知（异地 RCON 模式下读不到 banned-ips.json）"
    path = Path(server_dir) / "banned-ips.json"
    try:
        if path.is_symlink():
            return None, "未知（banned-ips.json 是符号链接）"
        with io.open(path, encoding="utf-8-sig") as stream:
            records = json.load(stream)
    except (OSError, ValueError):
        return None, "未知（无法读取 banned-ips.json）"
    if not isinstance(records, list):
        return None, "未知（banned-ips.json 格式无法识别）"
    details: dict[str, dict] = {}
    skipped = 0
    for record in records:
        ip = record.get("ip") if isinstance(record, dict) else None
        if not isinstance(ip, str) or not ip.strip():
            skipped += 1
            continue
        details[ip.strip()] = record
    note = f"（{skipped} 条记录格式异常，已跳过）" if skipped else ""
    return details, note


async def _append_ip_bans(plugin, result: dict) -> None:
    """把 IP 封禁名单并进 `get_bans` 的结果。以文件为准，回包只作交叉验证。"""
    details, detail_note = _ip_bans(plugin)
    listed = await _read_roster(plugin, "banlist ips")
    from_reply = list(listed.get("players") or [])
    if details:
        ips = [ip for ip in details]
        source = "banned-ips.json"
        if from_reply:
            # 文件与回包不一致时以文件为准，但要标明（不许静默合并）
            diff = {ip for ip in from_reply if ip not in details}
            if diff:
                detail_note = (detail_note + "；" if detail_note else "") + \
                    f"RCON 回包里有 {len(diff)} 个地址不在文件中"
    else:
        ips = from_reply
        source = "banlist ips"
        if not ips:
            return
    for ip in ips:
        record = (details or {}).get(ip)
        item = {
            "kind": "ip",
            "name": ip,
            "detail_available": record is not None,
            "removable": True,          # IP 本身就是校验过的目标，逐条可解封
        }
        for field in _DETAIL_FIELDS:
            value = record.get(field) if record is not None else None
            item[field] = value if isinstance(value, str) and value.strip() else _UNKNOWN
        result["items"].append(item)
    result["ip_count"] = len(ips)
    result["ip_source"] = source
    if detail_note:
        result["ip_note"] = detail_note


async def get_bans(plugin) -> dict:
    """封禁名单 = 玩家封禁 + IP 封禁（v0.28.0 起两者并列，用 `kind` 区分）。"""
    result = await _player_bans(plugin)
    await _append_ip_bans(plugin, result)
    return result


async def _player_bans(plugin) -> dict:
    """封禁名单：RCON 的 ``banlist`` 为准，``banned-players.json`` 只补详情。

    v0.26.1（2026-11-17 现场发现）：RCON 回包**多条封禁粘连**时
    （``... 演示用：看看封禁名单块123 was banned by Server: ...``）切分点无法确定 ——
    旧实现直接整卡空着，用户只看到一段原始回包，哪怕 ``banned-players.json``
    就在手边、名单精确到人。主人当时一句「感觉有问题」，说的就是这个。

    现在改为：**回包认不出但文件可读时，名单改用文件里的记录**。
    文件是确定性数据（不需要猜边界），因此每一条都能单独解封；
    但 ``state`` 仍如实保持「回执认不出」，并在 note 里写清名单来自哪里 ——
    不粉饰，也不让人对着一段原文干瞪眼。
    """
    listed = await _read_roster(plugin, "banlist players")
    result = {key: listed[key] for key in ("state", "state_label", "note", "raw")}
    result.update(items=[], detail_note="", mismatch_count=0, roster_source="banlist players")

    details, result["detail_note"] = _ban_details(plugin)
    if listed["state"] == server_card.STATE_OK:
        names = list(listed["players"])
        if details is not None:
            keys = {name.casefold() for name in names}
            result["mismatch_count"] = len(keys.symmetric_difference(details))
    elif details:
        names = [str(record["name"]) for record in details.values()
                 if isinstance(record.get("name"), str)]
        result["roster_source"] = "banned-players.json"
        if listed["state"] == server_card.STATE_EMPTY:
            reason = "RCON 回包显示没有封禁记录，但文件里有记录"
        else:
            reason = f"{listed['note'] or '回执认不出'}；RCON 回包无法可靠切分"
        result["note"] = f"{reason} —— 名单改用 banned-players.json（文件里的记录是确定的，逐条可解封）"
    else:
        # 回包认不出、文件也读不到：这时只能如实认输，别猜
        return result

    for name in names:
        detail = (details or {}).get(name.casefold())
        item = {
            "name": name,
            "detail_available": detail is not None,
            # 能不能点「解封」：名字本身要合法。前端据此启停按钮，后端另有校验（双保险）
            "removable": bool(_PLAYER_NAME.fullmatch(name)),
        }
        for field in _DETAIL_FIELDS:
            value = detail.get(field) if detail is not None else None
            item[field] = value if isinstance(value, str) and value.strip() else _UNKNOWN
        result["items"].append(item)

    if listed["state"] == server_card.STATE_EMPTY and details is not None:
        result["state"] = server_card.STATE_EMPTY
        result["state_label"] = server_card.STATE_LABEL[server_card.STATE_EMPTY]
    if not result["detail_note"] and any(not item["detail_available"] for item in result["items"]):
        result["detail_note"] = "未知（文件中缺少对应的玩家记录）"
    return result


def _audit_path() -> Path:
    from astrbot.api.star import StarTools

    return Path(StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control")) / "player_audit.jsonl"


def _write_audit(stream, actor: str, result: dict) -> None:
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    actor = _redact(actor)
    if "mode" in result:
        record = {
            "ts": ts,
            "actor": actor,
            "mode": result["mode"],
            "command": _redact(result["command"]),
            "state": result["state"],
            "accepted": result["accepted"],
            "warnings": [warning["code"] for warning in result["warnings"]],
            "raw_excerpt": _redact(result["raw"])[:200],
        }
    else:
        record = {
            "ts": ts,
            "actor": actor,
            "action": result["action"],
            "player": result["player"],
            "command": result["command"],
            "state": result["state"],
            "confirmed": result["confirmed"],
            # 审计是长期留档、可能被导出分享的文件 —— 它这一份**继续脱敏**，
            # 与 WebUI 实时显示（真 IP，管理员要用）刻意不同（v0.28.0 口径）。
            "raw_excerpt": _redact(result["raw"])[:200],
        }
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()


def get_audit(limit: int = 20) -> list[dict]:
    rows = deque(maxlen=min(max(limit, 1), _AUDIT_LIMIT))
    try:
        with io.open(_audit_path(), encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("confirmed"), bool):
                    continue
                if not all(isinstance(row.get(field), str) for field in _AUDIT_FIELDS if field != "confirmed"):
                    continue
                rows.append({
                    field: row[field] if field == "confirmed" else _redact(row[field])
                    for field in _AUDIT_FIELDS
                })
    except FileNotFoundError:
        pass
    return list(reversed(rows))


async def _apply_action(plugin, result: dict) -> None:
    command = result["command"]
    reply = await _send(plugin, command)
    action = result["action"]
    if reply.blocked:
        result.update(_status(server_card.STATE_UNSUPPORTED, reply.blocked, reply.raw))
        return
    if reply.error:
        note = reply.error
        if reply.may_have_run:
            note = f"写操作回执未确认：{note}"
            result["readback"] = await _read_roster(plugin, _READBACK_COMMAND[action], reply.rcon)
        result.update(_status(server_card.STATE_UNREACHABLE, note, reply.raw))
        return

    # WebUI 回执给真实原文（含 IP）：管理员要用它做 ban-ip 判断；审计里那一份仍脱敏
    result["raw"] = reply.raw
    readback = await _read_roster(plugin, _READBACK_COMMAND[action], reply.rcon)
    result["readback"] = readback
    if not reply.verdict.ok:
        state, note = server_card.judge_reply(command, reply.raw, parsed=False)
        if reply.verdict.status not in ("failed", "syntax_error") and state != server_card.STATE_UNSUPPORTED:
            note = f"已发出但未确认：{reply.verdict.reason}"
        result.update(_status(state, note, reply.raw))
        return

    if readback["state"] not in (server_card.STATE_OK, server_card.STATE_EMPTY):
        result.update(_status(readback["state"], f"已发出但未确认：{readback['note']}", reply.raw))
        return
    # IP 动作要比规范化后的地址（`::1` 与 `0:0:0:0:0:0:0:1` 是同一个地址，
    # 而服务端与用户写法未必一致）；玩家名仍按大小写不敏感比较。
    if action in _IP_ACTIONS:
        try:
            wanted = normalize_ip(result["player"])
        except ValueError:
            wanted = result["player"]
        present = wanted in set(readback["players"])
    else:
        present = result["player"].casefold() in {name.casefold() for name in readback["players"]}
    if present != (action in ("ban", "ban_ip", "whitelist_add")):
        result.update(_status(server_card.STATE_UNRECOGNIZED, "已发出但未确认：回读名单未证实操作生效", reply.raw))
        return

    result.update(_status(server_card.STATE_OK, "已回读确认", reply.raw), ok=True, confirmed=True)


async def run_action(
    plugin, action, player, reason="", *, actor="webui", expected_command=None
) -> dict:
    """在同一条 RCON 连接上执行、回读并记录审计。"""
    lock = getattr(plugin, "_player_admin_lock", None)
    if lock is None:
        lock = plugin._player_admin_lock = asyncio.Lock()
    async with lock:
        command = build_command(action, player, reason, plugin._resolve_version_info())
        if expected_command is not None and expected_command != command:
            raise ValueError("服务端能力或命令已变化，请重新预览")
        result = {
            "action": action, "player": player, "command": command, "confirmed": False,
            "ok": False, "readback": None,
            **_status(server_card.STATE_UNRECOGNIZED, "", ""),
        }
        try:
            audit = io.open(_audit_path(), "a", encoding="utf-8", newline="\n")
        except OSError:
            result.update(_status(server_card.STATE_UNRECOGNIZED, "审计文件不可写，操作未发送", ""))
            return result
        try:
            await _apply_action(plugin, result)
        except asyncio.CancelledError:
            result.update(_status(server_card.STATE_UNREACHABLE, "请求中断，操作结果未确认", result["raw"]))
            raise
        finally:
            try:
                with audit:
                    _write_audit(audit, actor, result)
            except OSError:
                result["audit_recorded"] = False
                result["note"] = f"{result['note']}；审计记录写入失败"
        return result
