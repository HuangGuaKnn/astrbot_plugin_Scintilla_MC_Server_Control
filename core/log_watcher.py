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
import time
from pathlib import Path
from typing import Awaitable, Callable

# 规范要求：插件日志器必须来自 astrbot.api（不得使用标准库 logging）。
from astrbot.api import logger

# ---- v0.23.5：读取上限 ----
# 原先这里 `data = f.read()` 是**无上限**的：整合包把日志刷屏或甩出一段崩溃堆栈时，
# 一次轮询就会把整段积压读进内存（几十 MB 也照读），而 Windows 上日志被独占写入、
# 读得越慢积压越多，正反馈。
#
#: 单次读取上限。一次最多读这么多，读不完下次接着读；下面的截断逻辑保证只消费完整行。
MAX_READ_BYTES = 1024 * 1024
#: 允许滞后的上限。积压超过它说明消费已追不上产出，直接跳到文件尾部并记一条日志 ——
#: 丢的是历史事件，换来的是「绝不 OOM、也绝不让待处理数据越滚越大」。
MAX_LAG_BYTES = 8 * 1024 * 1024

#: v0.23.5 第三轮：文件头指纹的取样字节数。**读不满就不给指纹**（见 `_head_sig`）——
#: 新文件刚写到一半时，「读多少算多少」会让同一份文件头每轮算出不同指纹，
#: 于是每轮都判一次轮转、从头重读，把已经播报过的事件再播一遍。
HEAD_BYTES = 256
#: v0.23.5 第三轮：内容锚点长度（见 `__init__` 的 `_anchor`）。太小容易偶合，
#: 太大则每轮多读几个字节 —— 64 字节足够，且必然覆盖到换行边界。
ANCHOR_BYTES = 64

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
# 处理策略（用户定案 v0.17.3）：这行日志**同时**算「玩家指令调用」与对应的语义事件，
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


#: v0.23.5：`stop()` 等待监听任务退出的超时（秒）。超时只记警告 ——
#: 「关不掉一个读日志的协程」不该把插件 terminate() 卡住。
STOP_TIMEOUT = 5.0

#: v0.23.5：单行超过这么多字符就截断再交给正则。
#: 某些模组会把整段堆栈挤成一行（几十万字符），正则不关心行尾之后的内容，
#: 留着只是白占内存与回溯时间。
MAX_LINE_CHARS = 8192


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
        # v0.23.5 外部复核：轮转不能只看「文件变短」——
        # _sig = (st_ino, st_size)：文件被换掉时 st_ino 会变；
        # _head = 文件头 256 字节的指纹：尺寸恰好相同的换文件也能认出来。
        self._sig: tuple[int, int] | None = None
        self._head: str = ""
        # v0.23.5 第三轮：**内容锚点**。头指纹只能认「尺寸恰好相同」或「换了文件」，
        # 认不出「同一 inode 原地重写、新内容比旧 offset 更长」—— 那种情况下 _pos
        # 之后的字节全是新的，从旧位置续读就是错位内容，而外表毫无异常。
        # 这里记下「上一次消费掉的最后 64 字节」及其偏移：每轮 poll 回读同一位置
        # 比对，字节不一致 = 那段内容被重写过 → 轮转。
        self._anchor: bytes = b""
        self._anchor_at: int = -1
        # 超长单行的余部处置：跳过一次读取上限后，下一轮从断点读到换行为止的残余
        # 不能当成新行解析（那是半截行），这里标记「丢弃到下一个换行为止」。
        self._skip_to_newline: bool = False
        # 健康度留痕（见 health()）：轮询连续失败次数 + 最近一条错误
        self.error_count: int = 0
        self.last_error: str = ""
        # v0.23.5 第三轮：文件缺失轮数 / 文件在不在 / 最近一次真正读到内容的时刻。
        # 「文件暂时不在」是轮转窗口里的正常现象，不该记 error；但「一直不在」
        # 必须看得见 —— 否则界面绿灯、pos 不动、事件安静地消失。
        self.missing_polls: int = 0
        self.file_present: bool = True
        self.last_read_at: float = 0.0
        # 放弃等待的监听任务（stop() 超时时留下）。丢引用会让半途的任务被 GC 掉，
        # 留着还能在 health() / 诊断里看见「有个没退出的」。
        self._orphaned: set[asyncio.Task] = set()
        # 最近一次 stat 到的文件大小（health() 用它算 lag_bytes）
        self._last_size: int = 0

    async def start(self) -> None:
        """从日志文件末尾开始监听（不回溯历史日志）。"""
        try:
            if self.log_path.exists():
                self._pos = self.log_path.stat().st_size
            else:
                self._pos = 0
        except OSError:
            self._pos = 0
        self._sig = self._file_sig()
        self._head = self._head_sig()
        # v0.23.5 第三轮：锚点/跳过标记随之重置。重新开监听是从**文件尾**起步，
        # 旧锚点对新 _pos 没有意义，留着会误判一次轮转（→ 从 0 重读 → 重复播报）。
        self._anchor, self._anchor_at = b"", -1
        self._skip_to_newline = False
        self.file_present = self.log_path.exists()
        self.missing_polls = 0
        # 探测日志编码（UTF-8 / GBK），避免固定 UTF-8 导致中文乱码
        try:
            self._enc = self._sniff_encoding()
        except Exception:
            self._enc = "utf-8"
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止监听。

        v0.23.5 外部复核：`await self._task` 此前没有超时 —— 若监听协程恰好卡在
        文件 IO 上不理会取消，terminate() 就会一直挂在这里。

        v0.23.5 第三轮：**换掉 `wait_for`**。`wait_for` 的超时会被「吞掉取消」的
        子任务骗过：`Task.cancel()` 发现自己在等一个 future 时会把取消**委托**给被
        等的任务（CPython `Task.cancel` 的 `_fut_waiter` 分支），子任务吞掉
        `CancelledError` 后父任务再也看不到取消，`timeouts.timeout` 于是永远转不成
        `TimeoutError`。实测（3.12.12）：子任务吞一次取消，`wait_for(t, 0.05)` 会一路
        等到子任务自己结束才返回（0.35s），返回的还是子任务的值 —— 即 5 秒的超时
        承诺是纸面的。`asyncio.wait` 才是「无论如何按时返回」的原语：它不取消、不等待
        收尾，超时就把任务留在 pending 里交给调用方处置。
        """
        self._running = False
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            _done, pending = await asyncio.wait({task}, timeout=STOP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:                           # noqa: BLE001
            logger.warning("日志监听任务停止时异常（已忽略）：%s", e)
            return
        if pending:
            # 保住引用：让它继续跑完（_running 已置 False，它下一轮会自己退出），
            # 但**别丢引用** —— 半途被 GC 掉的任务连痕迹都不剩，诊断时查无此人。
            self._orphaned.add(task)
            task.add_done_callback(self._orphaned.discard)
            logger.warning(
                "日志监听任务在 %.1fs 内未退出，已放弃等待（多半卡在文件 IO 上）；"
                "任务本身会在本轮读取返回后自行退出（进程退出时会一并回收）",
                STOP_TIMEOUT,
            )

    def health(self) -> dict:
        """监听健康度（v0.23.5 外部复核）。

        「日志还好不好读」此前完全不可见：文件被删、编码认错、轮询一直抛异常，
        界面与工具都只会安静地少播报几条事件。这里把位置、编码、错误计数摊开。

        v0.23.5 第三轮补：`file_present` / `missing_polls` / `lag_bytes` ——
        文件「一直不在」和「一直追不上」是两种不同的坏法，此前在健康度里都看不见
        （pos 停着、error_count 是 0、界面绿灯）。文件暂缺属轮转窗口的正常现象，
        所以走 missing_polls 计数而不是 error_count。
        """
        return {
            "running": bool(
                self._running and self._task is not None and not self._task.done()
            ),
            "path": str(self.log_path),
            "pos": int(self._pos),
            "encoding": self._enc or "",
            "error_count": int(self.error_count),
            "last_error": self.last_error,
            "file_present": bool(self.file_present),
            "missing_polls": int(self.missing_polls),
            "lag_bytes": max(0, int(self._last_size) - int(self._pos)),
            "last_read_at": float(self.last_read_at),
            "orphaned_tasks": len(self._orphaned),
        }

    def _file_sig(self) -> tuple[int, int] | None:
        """(st_ino, st_size) —— 文件被换掉时 st_ino 会变（Windows 上为文件索引）。"""
        try:
            st = self.log_path.stat()
        except OSError:
            return None
        return (int(getattr(st, "st_ino", 0) or 0), int(st.st_size))

    def _head_sig(self) -> str:
        """文件头 256 字节的指纹，用来识别「尺寸恰好相同的换文件」。

        v0.23.5 第三轮：**读满 HEAD_BYTES 才给指纹**（读不满返回空串）。此前是
        「读多少算多少」：新文件刚写到一半时，同一份文件头会算出不同的指纹，于是
        每轮都判一次轮转、从头重读一遍，把已经播报过的事件再播一遍。读不满就不比，
        那段时间由 inode / 长度 / 内容锚点三条判据顶着。
        """
        try:
            with open(self.log_path, "rb") as f:
                head = f.read(HEAD_BYTES)
        except OSError:
            return ""
        if len(head) < HEAD_BYTES:
            return ""
        return hash(head).to_bytes(8, "big", signed=True).hex()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._poll()
                # 恢复正常就清零：health() 里的计数表示「当前连续失败几次」
                self.error_count = 0
            except Exception as e:                       # noqa: BLE001
                # 日志读取失败不中断监听循环；但要留痕，别让「监听已死」瞒着所有人
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.error_count == 1 or self.error_count % 60 == 0:
                    logger.warning(
                        "日志监听轮询异常（连续第 %d 次，文件 %s）：%s",
                        self.error_count, self.log_path, e,
                    )
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        if not self.log_path.exists():
            # v0.23.5 第三轮：文件暂时不在，是轮转窗口里的**正常现象**（旧文件已删、
            # 新文件还没建），所以不记 error；但计数要落进 health()，让「一直不在」
            # 看得见 —— 此前这种情况就是安静地 return，界面绿灯、pos 不动、事件消失。
            self.file_present = False
            self.missing_polls += 1
            return
        self.file_present = True
        self.missing_polls = 0
        try:
            st = self.log_path.stat()
        except OSError as e:
            self.error_count += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return
        size = st.st_size
        self._last_size = size
        sig = (int(getattr(st, "st_ino", 0) or 0), size)
        head = ""
        # v0.23.5 外部复核：轮转判定不能只看「文件变短」。服主把旧日志挪走、新开的
        # 服务端恰好写到同样长度时 size 不变，_pos 停在旧位置 → 从此读到的都是错位
        # 内容（甚至一直读到文件尾就不再产出），而外表毫无异常。
        #
        # v0.23.5 第三轮：补上第四种判据，并放宽头指纹的适用条件。此前头指纹只在
        # `size == _pos` 时才比，于是「**同一 inode 原地重写、新内容比旧 offset 更长**」
        # 整类漏掉：既不判轮转、又从旧 _pos 续读错位字节（服主手动清空 latest.log
        # 后服务端继续写、日志被外部工具重排，都落进这一类）。四种情况都算轮转：
        #   ① 文件变短 ② st_ino 变了 ③ 文件头指纹变了 ④ 旧 offset 前的内容锚点变了
        rotated = size < self._pos
        if not rotated and self._sig is not None and sig[0] != self._sig[0]:
            rotated = True
        # ④ 锚点比对（主力判据）：回读「上次消费掉的最后 ANCHOR_BYTES 字节」，
        #    字节不一致 = 那段内容被重写过。正常追加绝不会动 _pos 之前的内容，
        #    所以这条既灵敏又不误报；代价是每轮多一次 64 字节的读。
        anchor_ok = (
            bool(self._anchor)
            and self._anchor_at >= 0
            and self._pos == self._anchor_at + len(self._anchor)
            and size >= self._pos
        )
        if not rotated and anchor_ok:
            try:
                with open(self.log_path, "rb") as f:
                    f.seek(self._anchor_at)
                    now_anchor = f.read(len(self._anchor))
            except OSError:
                now_anchor = None
            if now_anchor is not None and now_anchor != self._anchor:
                rotated = True
        # ③ 头指纹：不再要求 size == _pos。正常追加时文件头永不改变，而这次读取
        #    本来每轮都要做（下面刷新 self._head 用的是同一次调用），等于零额外成本。
        if not rotated and self._head:
            head = self._head_sig()
            rotated = bool(head) and head != self._head
        if rotated:
            # latest.log 被重建 → 从头读取（重新嗅探编码）
            self._pos = 0
            self._anchor, self._anchor_at = b"", -1
            self._skip_to_newline = False
            try:
                self._enc = self._sniff_encoding()
            except Exception:
                pass
            logger.info("检测到日志轮转（%s），已从头开始读取", self.log_path)
        self._sig = sig
        if not head:
            head = self._head_sig()
        self._head = head
        if size == self._pos:
            return
        # v0.23.5：滞后保护。上面的读取上限让每次最多前进 1 MiB —— 若消费速度
        # 长期低于产出速度（模组刷屏），积压会无限增长、_pos 越落越远。
        # 超过上限就直接追到尾部：宁可漏掉这段的播报事件，也不能让内存里
        # 挂着一个永远清不完的待处理区。
        lag = size - self._pos
        if lag > MAX_LAG_BYTES:
            logger.warning(
                "日志积压 %.1f MiB 超过上限（%d MiB），跳过历史直接追到文件尾部；"
                "这段的播报事件会缺失",
                lag / 1048576.0, MAX_LAG_BYTES // 1048576,
            )
            self._pos = size
            # 跳过去的这段没被消费，锚点对它没有意义（留着会误判一次轮转）
            self._anchor, self._anchor_at = b"", -1
            self._skip_to_newline = False
            return
        try:
            # 二进制读取：编码在解码阶段逐行自适应（GBK/UTF-8）。
            # 读多少有上限，见 MAX_READ_BYTES。
            with open(self.log_path, "rb") as f:
                f.seek(self._pos)
                data = f.read(MAX_READ_BYTES)
        except OSError as e:
            # 打开/读取失败是**真错误**（权限、被独占、盘掉了）→ 留痕，
            # 别再像以前那样安静地 return 让监听看起来一切正常。
            self.error_count += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return
        if not data:
            return
        if self._skip_to_newline:
            # v0.23.5 第三轮：上一轮判定为超长单行、已丢掉前半段 → 这里继续丢到
            # 下一个换行（含）为止。否则下一轮从断点读到的**半截行**会被当成一条
            # 新日志去跑正则（撞上事件正则的概率低，但语义是错的）。
            nl = data.find(b"\n")
            if nl == -1:
                self._pos += len(data)
                return
            self._pos += nl + 1
            self._skip_to_newline = False
            data = data[nl + 1:]
            if not data:
                return
        # 只消费完整行；末尾可能未写完的半行留待下次读取
        if not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut == -1:
                # 整段一个换行都没有 = 超长单行（某些模组会把整段堆栈挤成一行）。
                # 这里**绝不能**让 _pos 原地不动：读满上限却又不前进 = 永久卡死，
                # 监听循环从此再无产出，而且外表看不出任何异常。
                if len(data) >= MAX_READ_BYTES:
                    logger.warning(
                        "日志出现 %d 字节的无换行片段（疑似超长单行），已整段跳过；"
                        "该行余部会在下一个换行处一并丢弃",
                        len(data),
                    )
                    self._pos += len(data)
                    # 余部显式丢弃（见上面的 `_skip_to_newline` 分支）
                    self._skip_to_newline = True
                return
            data = data[: cut + 1]
        self._pos += len(data)
        # v0.23.5 第三轮：记下刚消费掉的最后一段当**内容锚点**，下轮回读比对 ——
        # 用来认「既不换 inode、也不变短」的原地重写（见上面判据 ④）。
        self._anchor = data[-ANCHOR_BYTES:]
        self._anchor_at = self._pos - len(self._anchor)
        self.last_read_at = time.monotonic()
        for line in self._decode_chunk(data).splitlines():
            if not line.strip():
                continue
            if len(line) > MAX_LINE_CHARS:
                # v0.23.5：超长单行先截断再解析（正则只看行首那段），
                # 免得几十万字符的行拖慢正则回溯、白占内存。
                line = line[:MAX_LINE_CHARS]
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
