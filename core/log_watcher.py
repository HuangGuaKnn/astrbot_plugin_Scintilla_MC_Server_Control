"""Minecraft 服务器日志监听器。

轮询读取 <服务器目录>/logs/latest.log 的新增内容（Windows 兼容，不依赖
inotify），用正则解析出聊天、玩家进出、死亡、成就等事件。

日志行示例（Forge 1.18.2）：
    [16:30:01] [Server thread/INFO] [minecraft/DedicatedServer]: Steve joined the game
    [16:30:05] [Server thread/INFO] [minecraft/DedicatedServer]: <Steve> hello everyone
    [16:31:00] [Server thread/INFO] [minecraft/DedicatedServer]: Steve was slain by Zombie
    [16:32:00] [Server thread/INFO] [minecraft/DedicatedServer]: Steve has made the advancement [Getting an Upgrade]
    [16:33:00] [Server thread/INFO]: * Steve 挥了挥手
    [16:34:00] [Server thread/INFO]: Steve whispers to Alex: 你好

事件类型：
    chat=普通聊天  whisper=私聊(/msg)  me=/me 动作
    join/leave/death/advancement/command=其他（command=玩家执行游戏内指令）

v0.17.3：整合包把 /kill、/advancement grant 的反馈同样写成「[玩家: 反馈]」，
这里让这类日志**同时**产出「玩家指令调用」与「死亡 / 成就」两个事件，
两个播报开关各收各的、互不串台（一次 /kill 既播指令又播死亡）。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable

# 事件类型常量
EVENT_CHAT = "chat"
EVENT_WHISPER = "whisper"
EVENT_ME = "me"
EVENT_JOIN = "join"
EVENT_LEAVE = "leave"
EVENT_DEATH = "death"
EVENT_ADVANCEMENT = "advancement"
EVENT_COMMAND = "command"

_CHAT_PATTERN = re.compile(r"^<(?P<player>[A-Za-z0-9_]{1,16})>\s*(?P<message>.*)$")
# /me <文本>：日志形如「* Steve 挖到了钻石」
_ME_PATTERN = re.compile(r"^\*\s*(?P<player>[A-Za-z0-9_]{1,16})\s+(?P<message>.+)$")
# 私聊（/msg、/tell、/w）：不同版本日志格式不同，统一宽容匹配。
#   Steve -> Alex: hello
#   Steve whispers to Alex: hello
#   Steve whispers to you: hello
_WHISPER_PATTERN = re.compile(
    r"^(?P<player>[A-Za-z0-9_]{1,16})\s+(?:->|whispers to)\s+"
    r"(?P<target>[A-Za-z0-9_]{1,16}|you|themselves):\s*(?P<message>.+)$"
)
# 1.19+ 聊天会在行首带 [Not Secure]，解析前剥掉
_NOT_SECURE = re.compile(r"^\[Not Secure\]\s*")
_JOIN_PATTERN = re.compile(r"^(?P<player>[A-Za-z0-9_]{1,16}) joined the game$")
_LEAVE_PATTERN = re.compile(r"^(?P<player>[A-Za-z0-9_]{1,16}) left the game$")
_ADVANCEMENT_PATTERN = re.compile(
    r"^(?P<player>[A-Za-z0-9_]{1,16})\s+has (?:made the advancement|"
    r"completed the challenge|reached the goal)"
)
# 玩家在游戏内执行指令，日志格式随服务端/整合包而变，这里兼容两类：
#
#  ① 原版 / Forge 独立服务端：
#     Steve issued server command: /gamemode creative
#     部分核心或插件写作：Alex executed command: /home  /  Bob ran command: /spawn
#     注意：本插件经 RCON 下发的指令日志里不会带玩家名，因此不会被误判成玩家指令。
#
#  ② 整合包（实测 Forge 1.20.1）：
#     [Steve: Set own game mode to Creative Mode]
#     这类服务端不写「issued server command」原文，而是把指令的**反馈文案**用
#     [玩家名: 反馈] 包一层落盘（死亡、难度、游戏模式、/give、/title 等都是这个形态）。
#     代价是拿不到指令原文，只能播报反馈；且完全静默的指令（无反馈）不会出现在日志里。
#     RCON 下发的指令在这类服务端同样不落盘，故无误报风险。
_COMMAND_PATTERN = re.compile(
    r"^(?P<player>[A-Za-z0-9_]{1,16})\s+"
    r"(?:issued server command|executed command|ran command):\s*(?P<cmd>/\S.*)$"
)
_COMMAND_FEEDBACK_PATTERN = re.compile(
    r"^\[(?P<player>[A-Za-z0-9_]{1,16}):\s*(?P<detail>.+)\]$"
)
# 反馈文案 → 附加语义事件 的识别表（v0.17.3）
#
# 部分整合包还会把 /kill、/advancement grant 的反馈也写成「[玩家: 反馈]」形态：
#     [Steve: Killed Steve]                                 ← /kill，反馈里含死亡语义
#     [Steve: Granted 6 advancements to Steve]              ← /advancement grant，含成就语义
#
# 处理策略（主人定案 v0.17.3）：这行日志**同时**算「玩家指令调用」与对应的语义事件，
# 即 /kill 会既播报「⌨ 玩家指令调用」又播报「💀 玩家死亡」，
# /advancement grant 会既播报「⌨ 玩家指令调用」又播报「🏆 成就 / 进度」。
# 这样各开关互不串台：想只看指令就关掉死亡 / 成就，想只看结果就关掉指令。
_FEEDBACK_EVENT_MAP = (
    (
        re.compile(r"^(?:Granted|Revoked)\s+\d+\s+(?:advancements?|recipes?)\b", re.I),
        EVENT_ADVANCEMENT,
    ),
    (re.compile(r"^Killed\s+[A-Za-z0-9_]{1,16}$"), EVENT_DEATH),
)
_DEATH_PATTERN = re.compile(
    r"^(?P<player>[A-Za-z0-9_]{1,16})\s+(?P<verb>was|died|blew up|fell|hit the ground|"
    r"drowned|burned|suffocated|starved|went up|went off|experienced kinetic|"
    r"was killed|was slain|was shot|was pricked|was squashed|was pummeled|was impaled|"
    r"was stung|was roasted|was frozen|was melted|was struck|was knocked|was doomed|"
    r"was withered|was burnt|was thrown|was blown|was blasted|was obliterated|"
    r"was destroyed|was pierced|was crushed|was skewered|was shredded|was sliced|"
    r"was smashed|was trampled|was fireballed|walked into|tried to swim|tried to fly|"
    r"tried to hurt|suffocated in|fell out|was poked|was killed by)\b"
)

# 提取日志行中冒号后的消息主体（兼容 Forge/Vanilla 日志前缀）
_MSG_EXTRACT = re.compile(r"\]\s*:\s*(.*)$")


class LogWatcher:
    """轮询监听服务器日志文件。"""

    def __init__(
        self,
        server_dir: str,
        on_event: Callable[[str, str, str], Awaitable[None]],
        poll_interval: float = 1.0,
    ):
        self.log_path = Path(server_dir) / "logs" / "latest.log"
        self.on_event = on_event
        self.poll_interval = poll_interval
        self._pos = 0
        self._task: asyncio.Task | None = None
        self._running = False
        # 已识别的日志编码（中文 Windows 下 Forge 常用 GBK 写日志）
        self._enc: str = ""

    async def start(self) -> None:
        """从日志文件末尾开始监听（不回溯历史日志）。"""
        try:
            if self.log_path.exists():
                self._pos = self.log_path.stat().st_size
            else:
                self._pos = 0
        except OSError:
            self._pos = 0
        # 探测日志编码（UTF-8 / GBK），避免固定 UTF-8 导致中文乱码
        try:
            self._enc = self._sniff_encoding()
        except Exception:
            self._enc = "utf-8"
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._poll()
            except Exception:
                # 日志读取失败不中断监听循环
                pass
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        if not self.log_path.exists():
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size < self._pos:
            # 日志轮转：latest.log 被重建，从头读取（重新嗅探编码）
            self._pos = 0
            try:
                self._enc = self._sniff_encoding()
            except Exception:
                pass
        if size == self._pos:
            return
        try:
            # 二进制读取：编码在解码阶段逐行自适应（GBK/UTF-8）
            with open(self.log_path, "rb") as f:
                f.seek(self._pos)
                data = f.read()
        except OSError:
            return
        if not data:
            return
        # 只消费完整行；末尾可能未写完的半行留待下次读取
        if not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut == -1:
                return
            data = data[: cut + 1]
        self._pos += len(data)
        for line in self._decode_chunk(data).splitlines():
            if not line.strip():
                continue
            for etype, player, detail in self._parse_line_multi(line):
                try:
                    await self.on_event(etype, player, detail)
                except Exception:
                    # 单个事件的回调失败不影响后续事件处理
                    pass

    # ============ 编码自适应（UTF-8 / GBK） ============
    # Minecraft/Forge 在中文 Windows 下常以 GBK(cp936) 写日志（System.out 默认
    # 字符集）。若固定按 UTF-8 读取，中文会变成乱码，导致「!群」这类中文前缀
    # 永远匹配不上。
    # 注意：不能简单地「能解码就用」——例如 GBK 的「群」(Èº) 恰好也是
    # 合法的 UTF-8 序列（会解成「Ⱥ」），因此这里改用「可读性评分」择优。

    @staticmethod
    def _text_score(text: str) -> int:
        """给解码结果打「可读性」分：中文/ASCII 加分，乱码区（拉丁扩展/希腊/西里尔）扣分。"""
        score = 0
        for ch in text:
            o = ord(ch)
            if o < 0x80:
                score += 2                      # ASCII 正常
            elif 0x4E00 <= o <= 0x9FFF:         # CJK 统一汉字
                score += 3
            elif 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
                score += 3                      # 中日韩标点 / 全角
            elif o in (0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2014, 0x00B7):
                score += 3                      # 常用中文标点
            elif 0x00C0 <= o <= 0x024F:         # 拉丁扩展（GBK 误解 UTF-8 的典型产物）
                score -= 2
            elif 0x0370 <= o <= 0x03FF or 0x0400 <= o <= 0x04FF:
                score -= 2                      # 希腊 / 西里尔（乱码高发区）
            elif o == 0xFFFD:                   # 替换符 = 解码失败
                score -= 5
            else:
                score += 1
        return score

    def _pick(self, cands: list) -> str:
        """从 [(编码, 文本)] 候选中择优，返回文本（并记忆编码）。"""
        if len(cands) == 1:
            self._enc = cands[0][0]
            return cands[0][1]
        cands.sort(
            key=lambda it: (self._text_score(it[1]), it[0] == self._enc),
            reverse=True,
        )
        self._enc = cands[0][0]
        return cands[0][1]

    def _sniff_encoding(self) -> str:
        """按文件已有内容嗅探编码（采样 + 评分），用于启动/日志轮转时定基调。"""
        try:
            with open(self.log_path, "rb") as f:
                sample = f.read(262144)
        except OSError:
            return self._enc or "utf-8"
        if not sample:
            return self._enc or "utf-8"
        cut = sample.rfind(b"\n")
        if cut != -1:
            sample = sample[: cut + 1]
        if not sample:
            return self._enc or "utf-8"
        cands = []
        for enc in ("utf-8", "gbk"):
            try:
                cands.append((enc, sample.decode(enc)))
            except (UnicodeDecodeError, LookupError):
                continue
        if not cands:
            return self._enc or "utf-8"
        if len(cands) == 1:
            return cands[0][0]
        cands.sort(
            key=lambda it: (self._text_score(it[1]), it[0] == self._enc),
            reverse=True,
        )
        return cands[0][0]

    def _decode_chunk(self, data: bytes) -> str:
        """解码一整块日志字节（含多行），自动兼容 UTF-8 / GBK。"""
        if not data:
            return ""
        cands = []
        for enc in ("utf-8", "gbk"):
            try:
                cands.append((enc, data.decode(enc)))
            except (UnicodeDecodeError, LookupError):
                continue
        if not cands:
            self._enc = "utf-8"
            return data.decode("utf-8", errors="replace")
        return self._pick(cands)

    @staticmethod
    def _parse_line(line: str):
        """解析一行日志，返回 (event_type, player, detail) 或 None。"""
        line = line.strip()
        if not line:
            return None
        m = _MSG_EXTRACT.search(line)
        if not m:
            return None
        msg = m.group(1).strip()
        msg = _NOT_SECURE.sub("", msg)
        if not msg:
            return None

        cm = _CHAT_PATTERN.match(msg)
        if cm:
            return (EVENT_CHAT, cm.group("player"), cm.group("message"))

        mm = _ME_PATTERN.match(msg)
        if mm:
            return (EVENT_ME, mm.group("player"), mm.group("message"))

        wm = _WHISPER_PATTERN.match(msg)
        if wm:
            return (EVENT_WHISPER, wm.group("player"), wm.group("message"))

        jm = _JOIN_PATTERN.match(msg)
        if jm:
            return (EVENT_JOIN, jm.group("player"), "")

        lm = _LEAVE_PATTERN.match(msg)
        if lm:
            return (EVENT_LEAVE, lm.group("player"), "")

        am = _ADVANCEMENT_PATTERN.match(msg)
        if am:
            return (EVENT_ADVANCEMENT, am.group("player"), msg)

        cmd = _COMMAND_PATTERN.match(msg)
        if cmd:
            # 原版格式：detail 统一带斜杠，播报时直接展示指令原文
            return (EVENT_COMMAND, cmd.group("player"), cmd.group("cmd").strip())

        cf = _COMMAND_FEEDBACK_PATTERN.match(msg)
        if cf:
            # 整合包格式：只有反馈文案，取玩家名 + 反馈正文（用于播报）
            detail = cf.group("detail").strip()
            if detail:
                # 这里仍按「指令调用」返回；含死亡 / 成就语义时由
                # _parse_line_multi 追加一个附加事件（两个开关各收各的）
                return (EVENT_COMMAND, cf.group("player"), detail)

        dm = _DEATH_PATTERN.match(msg)
        if dm:
            return (EVENT_DEATH, dm.group("player"), msg)

        return None

    @classmethod
    def _parse_line_multi(cls, line: str) -> list:
        """解析一行日志 → 事件列表（一行可能对应多个事件）。

        v0.17.3：整合包的「[玩家: 反馈]」形态若含死亡 / 成就语义
        （如 /kill 的 "Killed <玩家>"、/advancement grant 的 "Granted N advancements"），
        则在「玩家指令调用」之外**追加**一个死亡 / 成就事件，
        让两个开关各自独立生效、互不串台。
        """
        ev = cls._parse_line(line)
        if not ev:
            return []
        etype, player, detail = ev
        if etype == EVENT_COMMAND:
            for pat, extra_type in _FEEDBACK_EVENT_MAP:
                if pat.match(detail):
                    return [ev, (extra_type, player, detail)]
        return [ev]
