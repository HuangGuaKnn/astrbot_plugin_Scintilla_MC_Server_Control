"""管理员 Web 终端。执行回执和日志仅供 WebUI 与本地审计使用。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import stat
import unicodedata
from pathlib import Path

from . import player_admin, server_card
from .java_commands import CommandParseError, _skip_execute_clauses, strip_namespace
from .log_watcher import LogWatcher, run_blocking
from .strict_bool import strict_bool


MAX_COMMAND_LENGTH = 256
MAX_BROADCAST_LENGTH = 200
MAX_BATCH = 20
_LOG_LINES = 100
_LOG_BYTES = 128 * 1024
_ANCHOR_BYTES = 64
_CURSOR_RE = re.compile(r"([0-9a-f]{32})\.([0-9]{1,20})\.([0-9a-f]{32})")

_DIAGNOSTICS = frozenset({
    ("list",), ("tps",), ("mspt",), ("seed",), ("version",), ("plugins",),
    ("banlist", "players"), ("whitelist", "list"),
    ("time", "query", "daytime"), ("time", "query", "gametime"), ("time", "query", "day"),
})
_SERVICE = {
    "stop": "会关闭服务器，所有在线玩家将断线。",
    "restart": "可能立即重启服务器，所有在线玩家可能断线。",
    "reload": "会重载服务端配置或数据包，可能中断在线服务。",
    "reset": "插件命令，具体语义依插件而定；可能重置数据或重启服务器，中断在线服务。",
    "rl": "Bukkit 的 reload 别名，会重载服务端及插件，可能中断在线服务。",
    "shutdown": "插件或服务端扩展命令，具体语义依实现而定；可能关闭服务器，中断在线服务。",
    "reboot": "插件或服务端扩展命令，具体语义依实现而定；可能重启服务器，中断在线服务。",
    "save-off": "会关闭自动保存，崩溃或异常退出时可能丢失进度。",
    "save-all": "会写盘保存世界，可能短暂增加服务器负载。",
    "save-on": "会开启自动保存并改变服务器运行状态。",
}
_ADMIN = {
    "op": "会授予玩家管理员权限。",
    "deop": "会撤销玩家的管理员权限。",
    "ban": "会封禁玩家账号，在线目标可能立即断线。",
    "ban-ip": "会封禁目标 IP，同一网络出口的其他玩家也可能受影响。",
    "pardon": "会解除玩家账号封禁。",
    "unban": "会解除玩家账号封禁。",
    "pardon-ip": "会解除目标 IP 的封禁。",
    "kick": "会断开匹配玩家的连接。",
}
_WHITELIST = {
    "on": "会启用白名单，限制玩家加入。",
    "off": "会关闭白名单保护。",
    "reload": "会用磁盘内容重载白名单，改变允许加入的玩家。",
    "add": "会允许名单之外的玩家加入。",
    "remove": "会从白名单移除玩家。",
}
_WORLD = {
    "fill": "会批量覆盖方块，原有方块与内容可能丢失。",
    "clone": "会覆盖目标区域；移动模式也会清除源区域。",
    "setblock": "会替换目标方块，容器内容可能丢失。",
    "forceload": "会改变常加载区块，可能持续增加负载。",
    "kill": "会杀死或清除匹配的目标，历史状态无法通过此命令恢复。",
    "clear": "会移除目标背包物品，历史状态无法通过此命令恢复。",
    "gamerule": "修改游戏规则会改变后续世界行为。",
    "difficulty": "会改变世界难度和游戏行为。",
    "time": "会修改世界时间，影响昼夜和相关机制。",
    "weather": "会修改世界天气，影响相关游戏机制。",
    "spreadplayers": "会批量改变目标位置，可能把玩家或实体分散到不同地点。",
}
# C 组的多词命令按词法分析后的首词及第二词匹配，查询命令不在此表。
_WORLD_SUBCOMMANDS = {
    ("data", "remove"): "会删除目标的 NBT 数据，删除不可撤销。",
    ("data", "merge"): "会合并并覆盖目标的 NBT 数据，变更不可撤销。",
    ("data", "modify"): "会修改目标的 NBT 数据，变更不可撤销。",
    ("item", "replace"): "会替换目标物品栏中的物品，原物品的损失不可撤销。",
    ("worldborder", "set"): "会设置世界边界大小，因边界收缩造成的损失不可撤销。",
}
_BROADCAST = frozenset({"say", "tellraw", "title", "actionbar", "me", "teammsg", "tm"})
_BROADCAST_TEXT = {
    "say": "会向全服广播",
    "me": "会向全服展示动作消息",
    "tellraw": "会向指定玩家发送聊天消息",
    "title": "会在指定玩家的画面上显示或更新标题信息",
    "actionbar": "会在指定玩家的操作栏显示消息",
    "teammsg": "会向当前队伍发送消息",
    "tm": "会向当前队伍发送消息",
}
_SELECTORS = {
    "e": ("high", "所有匹配的实体，可能包含掉落物、盔甲架、宠物、村民和经验球。破坏性命令可能永久清除多个目标"),
    "a": ("high", "所有匹配的在线玩家"),
    "r": ("medium", "随机一个匹配的玩家"),
    "p": ("medium", "最近的匹配玩家"),
    "n": ("low", "最近的匹配实体（支持该选择器的版本）"),
}
_SELECTOR_RE = re.compile(r"@([earpn])(?:\[.*\])?\Z")


def mode(plugin) -> str:
    key = "web_console_whitelist_mode"
    return "whitelist" if strict_bool(plugin._cfg(key, False), True, key=key) else "full"


def _tokens(text: str) -> list[tuple[int, str]]:
    """保留原文字段偏移，同时尊重引号、转义和括号中的空格。"""
    tokens = []
    start = None
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char.isspace() and depth == 0:
            if start is not None:
                tokens.append((start, text[start:index]))
                start = None
            continue
        if start is None:
            start = index
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
    if start is not None:
        tokens.append((start, text[start:]))
    return tokens


def _warning(code: str, text: str, level: str) -> dict:
    return {"code": code, "text": text, "level": level}


def _warnings(text: str) -> tuple[list[dict], str, str]:
    warnings = []
    # 分析副本可以去除前导斜杠，送给 RCON 的原始行保持不变。
    segment = text.lstrip()
    segment = segment.removeprefix("/")
    selector_tokens = []
    for _ in range(len(segment) + 1):
        tokens = _tokens(segment)
        if not tokens:
            return warnings, "", segment
        words = [token for _, token in tokens]
        name = strip_namespace(words[0]).lower()
        subcommand = words[1].lower() if len(words) > 1 else ""
        if name == "execute" or (name == "return" and subcommand == "run"):
            if name == "execute":
                try:
                    inner = _skip_execute_clauses(words, 1)
                except CommandParseError:
                    warnings.append(_warning("indirect", f"无法判定嵌套命令边界，原输入：{segment}", "high"))
                    selector_tokens.extend(words[1:])
                    break
                if inner is None:
                    warnings.append(_warning("indirect", "execute 可按多个目标执行条件或存储结果。", "medium"))
                    selector_tokens.extend(words[1:])
                    break
                # 现有语法解析器保留每个 token，仅压缩 token 之间的空白。
                # 以 token 数取得分界后，从原输入切出嵌套命令原文。
                body_count = len(_tokens(inner))
                body_index = len(tokens) - body_count
            else:
                body_index = 2
                if len(tokens) <= body_index:
                    break
            selector_tokens.extend(words[1:body_index])
            segment = segment[tokens[body_index][0]:]
            warnings.append(_warning("indirect", f"嵌套命令：{segment}", "high"))
            continue
        if name in ("function", "schedule"):
            warnings.append(_warning(
                "indirect", f"函数或调度指令：{segment}。函数中的命令只能由服务端解析。",
                "high",
            ))
        if name in _SERVICE:
            level = "medium" if name in ("save-all", "save-on") else "high"
            warnings.append(_warning("service", f"{name} {_SERVICE[name]}", level))
        if name in _ADMIN or (name == "whitelist" and subcommand in _WHITELIST):
            reason = _ADMIN.get(name) or _WHITELIST[subcommand]
            warnings.append(_warning("administration", reason, "high"))
        if name in _WORLD and not (
            (name in ("difficulty", "gamerule") and len(words) < (3 if name == "gamerule" else 2))
            or (name == "time" and subcommand not in ("set", "add"))
            or (name == "forceload" and subcommand == "query")
        ):
            warnings.append(_warning("world", _WORLD[name], "high"))
        world_change = _WORLD_SUBCOMMANDS.get((name, subcommand))
        if world_change:
            warnings.append(_warning("world", world_change, "high"))
        if name in _BROADCAST:
            excerpt = segment[:MAX_BROADCAST_LENGTH]
            warnings.append(_warning("broadcast", f"{_BROADCAST_TEXT[name]}：{excerpt}", "medium"))
        # 自由消息字面量不是实体参数。data get 的选择器只用于读取。
        if name not in ("function", "schedule") and (name, subcommand) != ("data", "get"):
            selector_tokens.extend(words[1:])
        break
    for token in dict.fromkeys(selector_tokens):
        match = _SELECTOR_RE.fullmatch(token)
        if match:
            selector = match[1]
            level, scope = _SELECTORS[selector]
            warnings.append(_warning("selector_" + selector, f"{token} 作用范围：{scope}。", level))
    return warnings, name, segment


def _reject(item: dict, note: str, state: str = server_card.STATE_UNRECOGNIZED) -> None:
    item.update(player_admin._status(state, note, ""), accepted=False)


def _prepare(command, current_mode: str) -> tuple[list[str], dict]:
    if not isinstance(command, str):
        raise ValueError("command 需要字符串")
    lines = [line for line in re.split(r"\r\n|\r|\n", command) if line.strip()]
    if not lines:
        raise ValueError("请输入命令")
    if len(lines) > MAX_BATCH:
        raise ValueError(f"一次最多提交 {MAX_BATCH} 条命令")
    batch_warnings = []
    if len(lines) > 1:
        batch_warnings.append(_warning("multiline", f"将按顺序执行以下 {len(lines)} 条命令。", "medium"))
    results = []
    for line in lines:
        shown_command = line[:MAX_COMMAND_LENGTH].encode(
            "utf-8", errors="replace"
        ).decode("utf-8")
        item = {"command": shown_command, "warnings": [], "accepted": True}
        results.append(item)
        if len(line) > MAX_COMMAND_LENGTH:
            _reject(item, f"每条命令最多 {MAX_COMMAND_LENGTH} 个字符")
            continue
        if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in line):
            _reject(item, "命令不能包含控制字符")
            continue
        warnings, name, segment = _warnings(line)
        item["warnings"] = warnings
        if name in _BROADCAST and len(segment) > MAX_BROADCAST_LENGTH:
            _reject(item, f"广播指令最多 {MAX_BROADCAST_LENGTH} 个字符")
        elif current_mode == "whitelist" and tuple(line.split()) not in _DIAGNOSTICS:
            _reject(item, "当前仅接受白名单中的只读诊断命令", server_card.STATE_UNSUPPORTED)
    return lines, {
        "ok": any(item["accepted"] for item in results),
        "authorized": True,
        "mode": current_mode,
        "results": results,
        "warnings": batch_warnings,
    }


def preview(plugin, command) -> dict:
    return _prepare(command, mode(plugin))[1]


def _audit_path() -> Path:
    return player_admin._audit_path().with_name("console_audit.jsonl")


async def _apply(plugin, command: str, item: dict, current_mode: str) -> None:
    # _send 调用 _guard；任何风险提示都只用于展示。
    if current_mode == "whitelist":
        try:
            rcon = await plugin._get_rcon()
        except Exception as exc:
            item.update(player_admin._status(
                server_card.STATE_UNREACHABLE, str(exc) or type(exc).__name__, "",
            ), accepted=False)
            return
        probe = getattr(rcon, "probe_command", "")
        if getattr(rcon, "end_mode", "") == "sentinel" and probe and tuple(probe.split()) not in _DIAGNOSTICS:
            _reject(
                item, "RCON 结束哨兵的探测命令不在只读白名单中，请检查 RCON 设置",
                server_card.STATE_UNSUPPORTED,
            )
            return
        reply = await player_admin._send(plugin, command, rcon=rcon)
    else:
        reply = await player_admin._send(plugin, command)
    if reply.blocked:
        _reject(item, reply.blocked, server_card.STATE_UNSUPPORTED)
    elif reply.error:
        note = f"服务器可能已执行，请核对后再提交。{reply.error}" if reply.may_have_run else reply.error
        item.update(player_admin._status(server_card.STATE_UNREACHABLE, note, reply.raw),
                    accepted=reply.may_have_run)
    elif not reply.raw.strip():
        # 即使分类器从协议边界确认静默成功，终端也要明确告诉用户文本为空。
        note = f"服务端没有回任何文本（空回包）。{reply.verdict.reason}"
        item.update(player_admin._status(server_card.STATE_UNRECOGNIZED, note, reply.raw))
    elif reply.verdict.ok:
        item.update(player_admin._status(server_card.STATE_OK, reply.verdict.reason, reply.raw))
    else:
        state, note = server_card.judge_reply(command, reply.raw, parsed=False)
        if state != server_card.STATE_UNSUPPORTED or reply.verdict.status == "unknown":
            state, note = server_card.STATE_UNRECOGNIZED, reply.verdict.reason
        item.update(player_admin._status(state, note, reply.raw))


def _record(stream, actor: str, current_mode: str, item: dict, batch: list[dict]) -> bool:
    try:
        player_admin._write_audit(stream, actor, {
            **item, "mode": current_mode, "warnings": batch + item["warnings"],
        })
    except OSError:
        item["audit_recorded"] = False
        item["note"] += "；审计记录写入失败"
        return False
    return True


async def execute(plugin, command, *, actor: str) -> dict:
    lock = getattr(plugin, "_web_console_lock", None)
    if lock is None:
        lock = plugin._web_console_lock = asyncio.Lock()
    async with lock:
        current_mode = mode(plugin)
        lines, result = _prepare(command, current_mode)
        items = result["results"]
        batch = result["warnings"]
        try:
            audit = io.open(_audit_path(), "a", encoding="utf-8", newline="\n")
        except OSError:
            for item in items:
                _reject(item, "审计文件不可写，命令未发送")
            return {**result, "ok": False}
        with audit:
            audit_ok = True
            for index, (line, item) in enumerate(zip(lines, items)):
                try:
                    if item["accepted"]:
                        if audit_ok:
                            await _apply(plugin, line, item, current_mode)
                        else:
                            _reject(item, "上一条命令的审计写入失败，此命令未发送")
                except asyncio.CancelledError:
                    item.update(player_admin._status(
                        server_card.STATE_UNREACHABLE, "请求中断，命令可能已发出，结果未确认", "",
                    ))
                    _record(audit, actor, current_mode, item, batch)
                    for pending in items[index + 1:]:
                        if pending["accepted"]:
                            _reject(pending, "请求中断，命令未发送")
                        _record(audit, actor, current_mode, pending, batch)
                    raise
                audit_ok = _record(audit, actor, current_mode, item, batch) and audit_ok
        result["ok"] = any(item["accepted"] for item in items)
        return result


def _log_result(state: str, note: str, lines: list[str], cursor: str) -> dict:
    return {
        "ok": state != server_card.STATE_UNREACHABLE,
        "authorized": True,
        "state": state,
        "state_label": server_card.STATE_LABEL[state],
        "note": note,
        "lines": lines,
        "cursor": cursor,
    }


def _anchor(stream, offset: int) -> str:
    size = min(offset, _ANCHOR_BYTES)
    stream.seek(offset - size)
    return hashlib.blake2s(stream.read(size), digest_size=16).hexdigest()


def _read_tail(watcher: LogWatcher, cursor: str) -> dict:
    try:
        path = watcher.log_path.resolve()
        root = watcher.log_path.parent.parent.resolve()
        if not path.is_relative_to(root) or not stat.S_ISREG(path.stat().st_mode):
            return _log_result(server_card.STATE_UNREACHABLE, "日志来源不是服务端目录内的普通文件。", [], cursor)
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            identity = hashlib.blake2s(
                f"{path}:{metadata.st_dev}:{metadata.st_ino}".encode("utf-8"), digest_size=16,
            ).hexdigest()
            size = metadata.st_size
            old = _CURSOR_RE.fullmatch(cursor)
            rotated = False
            if old:
                old_size = int(old[2])
                rotated = old[1] != identity or old_size > size or old[3] != _anchor(stream, old_size)
            next_cursor = f"{identity}.{size}.{_anchor(stream, size)}"
            offset = max(0, size - _LOG_BYTES)
            stream.seek(offset)
            sample = stream.read(size - offset)
    except FileNotFoundError:
        return _log_result(server_card.STATE_UNREACHABLE, "找不到 logs/latest.log，可能正在轮转。", [], cursor)
    except (OSError, RuntimeError, ValueError):
        return _log_result(server_card.STATE_UNREACHABLE, "无法读取 logs/latest.log。", [], cursor)
    if offset:
        sample = sample.partition(b"\n")[2]
    sample = sample[:sample.rfind(b"\n") + 1]
    # WebUI 是管理员专属页：日志尾给真实内容（含 IP），管理员要靠它判断该封哪个地址；
    # 聊天工具不经过这里；审计文件那一份仍脱敏（见 player_admin._write_audit）。
    lines = watcher._decode_chunk(sample).splitlines()[-_LOG_LINES:]
    if rotated:
        return _log_result(server_card.STATE_UNRECOGNIZED, "日志已轮转，已切换到当前文件。", lines, next_cursor)
    if not lines:
        return _log_result(server_card.STATE_UNRECOGNIZED, "当前日志没有可显示的完整行。", [], next_cursor)
    return _log_result(server_card.STATE_OK, "", lines, next_cursor)


async def log_tail(plugin, cursor: str = "") -> dict:
    current_mode = mode(plugin)
    root = str(plugin._cfg("server_dir", "") or "").strip()
    if not _CURSOR_RE.fullmatch(cursor):
        cursor = ""
    if strict_bool(plugin._cfg("remote_rcon_mode", False), False, key="remote_rcon_mode") or not root:
        result = _log_result(server_card.STATE_UNSUPPORTED, "没有可读取的本地服务端日志来源。", [], "")
    else:
        # 只用现成的路径和编码处理，在独立快照上读取，不改变事件监听器位置或启动转发。
        watcher = LogWatcher(root, on_event=None)
        live = getattr(plugin, "_watcher", None)
        if live is not None and live.log_path == watcher.log_path:
            watcher._enc = live._enc
        read, result = await run_blocking(_read_tail, watcher, cursor, timeout=3, what="终端日志尾")
        if not read:
            result = _log_result(server_card.STATE_UNREACHABLE, "日志读取暂不可用。", [], cursor)
    return {**result, "mode": current_mode}
