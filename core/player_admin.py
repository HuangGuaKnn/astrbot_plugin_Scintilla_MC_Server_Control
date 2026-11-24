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
from .log_watcher import LoginIpResult, LogWatcher
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
_LAN_IPV4 = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
#: `banlist ips` 的确定形态：`<ip> was banned by <source>: <reason>`。
#: 只认这种，不单独抓「看起来像 IP 的串」—— 理由里可能就写着别人的 IP。
_IP_ENTRY = re.compile(r"(?P<ip>[0-9A-Fa-f:.]+)\s+was banned by\s+(?P<src>[^:]+):\s*(?P<reason>.*)", re.I)
_BAN_COUNT = re.compile(r"There are\s+(\d+)\b", re.I)
_NO_BANS = re.compile(r"There are no (?:IP )?bans?|There are (?:0 )?total banned IPs:\s*", re.I)


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
    if _NO_BANS.fullmatch(clean):
        return []
    # 先取声明数量，再把「There are N bans:」这类前缀剥掉再扫 IP。
    # 不剥的话，IP 字符类 `[0-9A-Fa-f:.]+` 会把前缀末尾那个**冒号**吞进候选
    # （拿到 ":203.0.113.99"）→ ip_address() 报错 → 跳过 → 声明数与实际数对不上
    # → 整段被判 None。实测 1.13.2 的 `banlist ips` 就是
    # `There are 1 bans:203.0.113.99 was banned by Recon: ...`，
    # 于是每次 IP 封禁的回读都拿不到名单，界面永远显示「已发出但未确认」。
    declared = _BAN_COUNT.search(clean)
    body = re.sub(r"^There are\s+\d+\b[^:]*:\s*", "", clean, flags=re.I)

    found: list[str] = []
    for match in _IP_ENTRY.finditer(body):
        candidate = match.group("ip")
        try:
            address = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    if declared and int(declared.group(1)) != len(found):
        return None
    return found or None
_IPV4 = re.compile(r"(?<!\d)\d+(?:\.\d+){3}")
_IP_CANDIDATE = re.compile(r"(?<![\w:])[a-fA-F0-9:.]+(?:%[A-Za-z0-9_.-]+)?(?![\w:])")
_LEGACY_BANS = re.compile(r"There are (\d+) total banned players:\s*(.*)", re.DOTALL)
_MODERN_BANS = re.compile(r"There are (\d+) ban(?:s|\(s\)):\s*(.*)", re.DOTALL)
_BAN_ENTRY_NAME = re.compile(r"([A-Za-z0-9_]{1,16}) was banned by ")
_BAN_ENTRY_IP = re.compile(r"([0-9A-Fa-f:.]+(?:%[A-Za-z0-9_.-]+)?) was banned by ")
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
            if candidate.endswith(":"):
                alternatives += (candidate[:-1],)
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
    """构造管理员 WebUI 使用的状态字典，保留服务器原文。"""
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
    elif command in ("banlist players", "banlist ips"):
        result["boundary_confirmed"] = complete and reply.verdict.boundary_confirmed is True
    return result


def _player_ip_details(record: LoginIpResult) -> dict:
    scope = ""
    if record.ip:
        address = ipaddress.ip_address(record.ip)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.is_loopback:
            scope = "loopback"
        elif address.is_link_local:
            scope = "link_local"
        elif any(address in net for net in _LAN_IPV4):
            scope = "private"
    # v0.35.2：回环 / 内网 / 链路本地地址**允许**封禁 —— 服务端的 banned-ips.json 随时可 pardon-ip
    # 撤销，不影响 RCON 控制面板（走的是另一个端口），所以「封了就把自己关在门外」是错的判断。
    # scope 保留下来只用于前端提示影响范围，不再作为拦截条件。
    return {**record._asdict(), "scope": scope, "can_ban": bool(record.ip)}


async def _lookup_player_ips(plugin, players: list[str], *, force: bool = False) -> dict:
    if strict_bool(plugin._cfg("remote_rcon_mode", False), False, key="remote_rcon_mode"):
        return {name: _player_ip_details(LoginIpResult("remote")) for name in players}
    server_dir = str(plugin._cfg("server_dir", "") or "").strip()
    if not server_dir:
        return {name: _player_ip_details(LoginIpResult("unreadable")) for name in players}
    log_path = Path(server_dir) / "logs" / "latest.log"
    watcher = getattr(plugin, "_watcher", None)
    if not isinstance(watcher, LogWatcher) or watcher.log_path != log_path:
        # 关闭实时播报时仍可搜索本地日志。按目录复用缓存，不启动监听任务。
        watcher = getattr(plugin, "_web_ip_lookup", None)
        if watcher is None or watcher.log_path != log_path:
            watcher = plugin._web_ip_lookup = LogWatcher(server_dir, getattr(plugin, "_on_server_event", None))
    records = await watcher.lookup_player_ips(players, force=force)
    if strict_bool(plugin._cfg("remote_rcon_mode", False), False, key="remote_rcon_mode"):
        return {name: _player_ip_details(LoginIpResult("remote")) for name in players}
    if str(plugin._cfg("server_dir", "") or "").strip() != server_dir:
        return {name: _player_ip_details(LoginIpResult("incomplete")) for name in players}
    return {name: _player_ip_details(record) for name, record in records.items()}


async def search_player_ip(plugin, player: str) -> dict:
    if not isinstance(player, str) or not _PLAYER_NAME.fullmatch(player):
        raise ValueError("玩家名须为 1–16 个字母、数字或下划线")
    return (await _lookup_player_ips(plugin, [player], force=True))[player]


async def get_online(plugin) -> dict:
    """在线名单及最近进服 IP。监听索引未命中时批量查询本地日志。"""
    result = await _read_roster(plugin, "list")
    players = result.get("players") or []
    if not players:
        return result
    details = await _lookup_player_ips(plugin, players)
    result["ip_details"] = details
    watcher = getattr(plugin, "_watcher", None)
    if not isinstance(watcher, LogWatcher) and callable(getattr(watcher, "recent_ip", None)):
        # 兼容只实现旧索引接口的监听器。
        result["ips"] = {name: watcher.recent_ip(name) for name in players}
    elif isinstance(watcher, LogWatcher) or any(record["ip"] for record in details.values()):
        result["ips"] = {name: record["ip"] for name, record in details.items()}
    return result


async def _snapshot_ip_matches(plugin, target: str) -> dict:
    """在写入前核对在线名单和这些玩家最近一次进服的地址。"""
    roster = await _read_roster(plugin, "list")
    if roster["state"] not in (server_card.STATE_OK, server_card.STATE_EMPTY):
        return {"state": "unavailable", "players": []}
    players = roster["players"]
    if not players:
        return {"state": "complete", "players": []}

    # 主动读本地日志，避免玩家刚重连时只看到上一轮的缓存地址。
    details = await _lookup_player_ips(plugin, players, force=True)
    known = {
        name: record["ip"] for name, record in details.items()
        if record["state"] == "found" and record["ip"] and not record["limited"]
    }
    if not known:
        return {"state": "unavailable", "players": []}
    return {
        "state": "complete" if len(known) == len(players) else "partial",
        "players": [name for name in players if known.get(name) == target],
    }


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


def _verify_glued_roster(listed: dict, records) -> None:
    """本地条目能完整重建黏连回包时，补上回包中的顺序和状态。"""
    if listed["state"] != server_card.STATE_UNRECOGNIZED or listed.get("boundary_confirmed") is not True:
        return
    raw = listed["raw"]
    match = _MODERN_BANS.fullmatch(server_card.strip_color_codes(raw).strip())
    if not match or int(match[1]) < 2 or records is None or len(records) != int(match[1]):
        return

    is_ip = listed["command"] == "banlist ips"
    field, entry_pattern, filename = (
        ("ip", _BAN_ENTRY_IP, "banned-ips.json") if is_ip
        else ("name", _BAN_ENTRY_NAME, "banned-players.json")
    )
    details = {}
    for record in records:
        value = record.get(field) if isinstance(record, dict) else None
        if not isinstance(value, str):
            return
        try:
            key = normalize_ip(value) if is_ip else value.casefold()
        except ValueError:
            return
        if key in details:
            return
        details[key] = record

    body = match[2]
    offset = 0
    names = []
    while details:
        entry = entry_pattern.match(body, offset)
        if not entry:
            return
        name = entry[1]
        try:
            name_key = normalize_ip(name) if is_ip else name.casefold()
        except ValueError:
            return
        record = details.pop(name_key, None)
        if record is None:
            return
        source, reason = record.get("source"), record.get("reason")
        if not isinstance(source, str) or not isinstance(reason, str):
            return
        expected = server_card.strip_color_codes(f"{name} was banned by {source}: {reason}")
        if not body.startswith(expected, offset):
            return
        names.append(name_key if is_ip else name)
        offset += len(expected)
        while offset < len(body) and body[offset].isspace():
            offset += 1
    if offset == len(body):
        listed.update(_status(server_card.STATE_OK, f"回包与本地 {filename} 一致", raw),
                      players=names, roster_source=filename)


def _ip_bans(plugin) -> tuple[list | None, str]:
    """读 `banned-ips.json` —— IP 封禁的权威名单。

    v0.28.0 起**解锁**：这个文件以前被列进禁区（含 IP），但 WebUI 是管理员专属页面，
    而 `ban-ip` 是服主真实会用的功能 —— 藏起来只会让人去翻文件、或者靠肉眼认日志。

    保留每条原始记录（包括缺失 IP、非对象和重复记录），由展示层标注格式异常，
    不因为单条脏数据藏掉整行；但整个文件读不出来时如实返回 None。
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
    return records, ""


async def _append_ip_bans(plugin, result: dict) -> None:
    """把 IP 封禁名单并进 `get_bans` 的结果。以文件为准，回包只作交叉验证。"""
    listed = await _read_roster(plugin, "banlist ips")
    records, detail_note = _ip_bans(plugin)
    _verify_glued_roster(listed, records)
    result["ip_readback"] = listed
    from_reply = list(listed.get("players") or [])
    from_file = bool(records)
    if from_file:
        source = "banned-ips.json"
    else:
        records = [{"ip": ip} for ip in from_reply]
        source = "banlist ips"
        if not records:
            return
    valid_ips = set()
    malformed = 0
    for record in records:
        raw_ip = record.get("ip") if isinstance(record, dict) else None
        try:
            ip = normalize_ip(raw_ip)
            removable = True
            valid_ips.add(ip)
        except ValueError:
            ip = raw_ip if isinstance(raw_ip, str) and raw_ip else json.dumps(record, ensure_ascii=False)
            removable = False
            malformed += 1
        item = {
            "kind": "ip",
            "name": ip,
            "detail_available": from_file and isinstance(record, dict),
            "removable": removable,
        }
        if not removable:
            item["raw_record"] = json.dumps(record, ensure_ascii=False)
            item["note"] = "格式异常，不能在这里解封；原记录：" + item["raw_record"]
        for field in _DETAIL_FIELDS:
            value = record.get(field) if isinstance(record, dict) else None
            item[field] = value if isinstance(value, str) and value.strip() else _UNKNOWN
        result["items"].append(item)
    if malformed:
        detail_note = f"{malformed} 条记录格式异常，已保留原记录，不能在这里解封"
    if from_file:
        notes = [detail_note] if detail_note else []
        if listed["state"] in (server_card.STATE_OK, server_card.STATE_EMPTY):
            reply_ips = set(from_reply)
            if reply_ips - valid_ips:
                notes.append(f"RCON 回包里有 {len(reply_ips - valid_ips)} 个地址不在文件中")
            if valid_ips - reply_ips:
                notes.append(f"本地文件中有 {len(valid_ips - reply_ips)} 个地址未出现在 RCON 回包中")
        else:
            notes.append("IP 名单来源：banned-ips.json")
        detail_note = "；".join(notes)
    result["ip_count"] = len(records)
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
    仅在文件和完整回包逐条一致时确认回包，其他情况保留原回包状态。
    """
    listed = await _read_roster(plugin, "banlist players")
    details, detail_note = _ban_details(plugin)
    _verify_glued_roster(listed, details.values() if details is not None else None)
    result = {key: listed[key] for key in ("state", "state_label", "note", "raw")}
    result.update(items=[], detail_note=detail_note, mismatch_count=0,
                  roster_source=listed.get("roster_source", "banlist players"))

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


def _audit_target(row: dict) -> tuple[str, str]:
    action, player, command = row["action"], row["player"], row["command"]
    if action in _IP_ACTIONS:
        try:
            address = normalize_ip(player)
        except ValueError:
            pass  # 兼容旧记录中的 [IP 已隐藏]。
        else:
            prefix = f"{_WRITE_PREFIX[action]} {address}"
            if command == prefix or command.startswith(prefix + " "):
                return address, prefix + _redact(command[len(prefix):])
            return address, _redact(command)
    return _redact(player), _redact(command)


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
        player, command = _audit_target(result)
        record = {
            "ts": ts,
            "actor": actor,
            "action": result["action"],
            "player": player,
            "command": command,
            "state": result["state"],
            "confirmed": result["confirmed"],
            "raw_excerpt": _redact(result["raw"])[:200],
        }
        if "ip_matches" in result:
            record["ip_matches"] = result["ip_matches"]
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
                record = {field: row[field] for field in _AUDIT_FIELDS}
                record["player"], record["command"] = _audit_target(row)
                record["actor"] = _redact(row["actor"])
                record["raw_excerpt"] = _redact(row["raw_excerpt"])
                matches = row.get("ip_matches")
                if row["action"] == "ban_ip" and isinstance(matches, dict):
                    state, names = matches.get("state"), matches.get("players")
                    if (state in ("complete", "partial", "unavailable") and isinstance(names, list)
                            and all(isinstance(name, str) for name in names)
                            and _valid_names(names, len(names))):
                        record["ip_matches"] = {"state": state, "players": names}
                rows.append(record)
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

    result["raw"] = reply.raw
    readback = await _read_roster(plugin, _READBACK_COMMAND[action], reply.rcon)
    result["readback"] = readback
    if not reply.verdict.ok:
        state, note = server_card.judge_reply(command, reply.raw, parsed=False)
        if reply.verdict.status not in ("failed", "syntax_error") and state != server_card.STATE_UNSUPPORTED:
            note = f"已发出但未确认：{reply.verdict.reason}"
        result.update(_status(state, note, reply.raw))
        return

    if (readback["state"] == server_card.STATE_UNRECOGNIZED and readback.get("boundary_confirmed") is True
            and (action in ("ban", "unban") or action in _IP_ACTIONS)):
        if action in _IP_ACTIONS:
            records, _ = _ip_bans(plugin)
        else:
            details, _ = _ban_details(plugin)
            records = details.values() if details is not None else None
        _verify_glued_roster(readback, records)
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

    note = "已回读确认"
    if readback.get("roster_source"):
        note += f"（{readback['note']}）"
    result.update(_status(server_card.STATE_OK, note, reply.raw), ok=True, confirmed=True)


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
            if action == "ban_ip":
                result["ip_matches"] = {"state": "unavailable", "players": []}
                try:
                    result["ip_matches"] = await asyncio.wait_for(
                        _snapshot_ip_matches(plugin, normalize_ip(player)), timeout=8,
                    )
                except (asyncio.TimeoutError, OSError):
                    pass  # 关联未能完成，继续封禁并记录「无法核对」。
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
