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
import hashlib
import re
import threading
from queue import Empty, Full, Queue
import time
from concurrent.futures import Future, ThreadPoolExecutor
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

# ---- v0.24.7（GPT 第二轮复核 P2）：事件回调的超时与在飞上限 ----
#: 单次 `on_event` 回调的等待上限（秒）。回调里是平台发送 / 聊天桥接推送，
#: 平台侧一卡（网络不通 / 会话不存在 / 限流）就永不返回 —— 旧实现直接 await，
#: 于是**整个轮询停摆**，`stop()` 也只能干等。
CALLBACK_TIMEOUT = 10.0
#: 同时在飞的回调任务上限。超时只是「不再等」：Python 杀不掉已经在跑的协程
#: （它可能吞掉取消），所以必须有硬上限，否则卡住的回调会无限堆积。
MAX_CALLBACK_INFLIGHT = 4
#: 超时回调的「僵尸宽限期」（秒）：超过它仍未结束的回调**不再占用在飞额度**
#: —— 否则一个永久卡死的回调会把后续事件全部饿死（轮询还在跑，事件一条都不派发）。
#: 它仍会被记账（`callbacks_stale`）并保留引用，卡死这件事必须留痕。
CALLBACK_STUCK_GRACE = 60.0
#: 卡死回调的引用上限（只用于留痕；到顶就丢最旧的一条并告警，防止集合无界增长）。
MAX_STUCK_CALLBACKS = 32

#: daemon 阻塞执行器的并发上限（v0.24.8 · GPT 第三轮复核 P1）。
#: 旧实现是 `submit()` 里 `threading.Thread(...)` 一发一条 —— 名字叫「单线程」，
#: 实际线程数等于未完成提交数：WebUI 首屏的状态 / 指纹 / 进程表全落到同一个
#: 进程级执行器上，慢盘时线程数无界增长。这里改成固定 worker 池（队列排队），
#: 既堵住无界，又不把短 IO 串成一条线（串行会让 WebUI 首屏明显变慢）。
MAX_DAEMON_WORKERS = 4

#: 排队上限（v0.24.8 · GPT 第四轮复核 P1「队列仍无界」）。
#: 固定 worker 池只让「同时跑几条」有界 —— **队列本身还是无界的**：实测连续提交
#: 50 笔阻塞任务，`pending` 就到 50 并一直躺在那里（`run_blocking` 超时只是「不再等」，
#: 任务仍在队列里排队），内存随提交量线性增长；而 `shutdown(cancel_futures=True)`
#: 此前只投哨兵，队列里的活儿照样先于哨兵执行（停用之后还在跑过期任务）。
#: 现在两道闸一起上：**到顶即拒收**（`RuntimeError`，与线程池关闭同一句话）、
#: **关闭时真取消**（把队列里未开始的任务逐个 cancel）。宁可让调用方拿到
#: 「不可用」，也不让队列变成第二处无界增长点。
MAX_DAEMON_QUEUE = 64


class _DaemonThreadExecutor:
    """有界并发的 **daemon** 执行器（v0.24.7；v0.24.8 改为固定 worker 池）。

    为什么不再用 ``concurrent.futures.ThreadPoolExecutor``：它的工作线程是**非 daemon**
    的，解释器退出时会走 ``threading._register_atexit`` 去 join 它们。日志 IO 一旦卡在
    系统调用里（慢盘 / 网络盘 / 文件被别的进程独占），Python 杀不掉那个线程，
    ``shutdown(wait=False)`` 也拦不住退出流程 —— 表现为「停用它之后进程迟迟不退」。

    v0.24.7 的替代品是「每次 submit 新建一条 daemon 线程」，虽然解决了退出问题，
    却把并发上限变成了「未完成提交数」—— 名字叫单线程、行为是无界，这是假的。

    现在：``max_workers`` 条**常驻** worker 线程从队列取任务，提交方只入队立即返回。
    对调用方语义保持一致：

      * ``submit()`` 仍返回 ``concurrent.futures.Future``（``run_in_executor`` 只要求这个）；
      * 关闭之后再 ``submit()`` 抛 ``RuntimeError``（与线程池同一句话）；
      * **队列有界**：排队到顶即拒收（同样是 ``RuntimeError``，不让队列无界增长）；
      * 关闭时投递哨兵让 worker 自然退出，**从不 join**（``wait`` 参数刻意忽略）；
      * ``shutdown(cancel_futures=True)`` 会**真的取消**队列里还没开始的任务。
    """

    def __init__(self, thread_name_prefix: str = "logwatch",
                 max_workers: int = MAX_DAEMON_WORKERS,
                 max_queue: int = MAX_DAEMON_QUEUE) -> None:
        self._prefix = thread_name_prefix
        #: 保留这个名字：回归用例与运维脚本按线程池口径读过它。
        #: 值是**真实**的并发上限（以前写死 1，实际却是无界）。
        self._max_workers = max(1, int(max_workers))
        #: 排队上限（不含正在执行的）；到顶即拒收。
        self._max_queue = max(1, int(max_queue))
        self._queue: "Queue" = Queue(maxsize=self._max_queue)
        self._shutdown = False
        self._lock = threading.Lock()
        self._threads: set = set()
        #: 已入队、未完成（排队 + 执行中）的任务数 —— 饱和时给运维一个可看的数。
        self._pending = 0
        #: 因队列到顶被拒收的累计次数（v0.24.8）：拒收是**有意的**保护动作，
        #: 但必须留痕 —— 否则它只表现为「某些请求偶发说不可用」，没人查得到原因。
        self._rejected = 0
        for i in range(self._max_workers):
            th = threading.Thread(target=self._worker_loop, name=f"{self._prefix}-{i}",
                                  daemon=True)
            with self._lock:
                self._threads.add(th)
            th.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except Empty:
                # v0.24.8（GPT 第四轮 P1）：**队列空且已关闭 → 退出**。
                # 哨兵只是「立刻退出」的加速路径，不是唯一路径 —— 否则一旦
                # 关闭瞬间队列正好是满的（哨兵塞不进去），worker 就会永远
                # 卡在 get() 上（daemon 线程，进程退出前一直挂着）。
                # 注意顺序：先**排空**队列再退，已在队列里的活儿照旧执行
                # （与 ``ThreadPoolExecutor.shutdown(wait=False)`` 同义）。
                if self._shutdown:
                    with self._lock:
                        self._threads.discard(threading.current_thread())
                    return
                continue
            if item is None:                                  # 关闭哨兵
                with self._lock:
                    self._threads.discard(threading.current_thread())
                return
            fut, fn, args, kwargs = item
            if not fut.set_running_or_notify_cancel():         # 调用方已取消
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                continue
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:                       # noqa: BLE001
                try:
                    fut.set_exception(e)
                except Exception:                            # noqa: BLE001
                    pass
            finally:
                # 记账放在**完成后**：pending 才是「排队 + 执行中」的真实积压，
                # 而不是「还没被领走的条数」（后者会把正在跑的活儿漏掉）。
                with self._lock:
                    self._pending = max(0, self._pending - 1)

    def submit(self, fn, *args, **kwargs) -> Future:
        # v0.24.8：**入队与关闭判定必须在同一把锁里**。旧写法先查 `_shutdown`
        # 再出锁入队 —— 关掉的那一瞬间，任务可能正好排在哨兵之后：worker 已经
        # 领到哨兵退出，这笔任务永远不会执行，调用方的 Future 也永远不会 resolve
        # （在 WebUI 上就是「一直转圈」，比报错更难查）。SimpleQueue 入队不阻塞，
        # 因此锁内入队不会拖慢提交路径。
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            fut: Future = Future()
            try:
                # v0.24.8（GPT 第四轮 P1）：**有界入队**，到顶即拒收。
                # 用 put_nowait 而不是 put：锁内阻塞等待空位会把提交路径
                # （以及 shutdown）一起拖住，比拒收更糟。
                self._queue.put_nowait((fut, fn, args, kwargs))
            except Full:
                self._rejected += 1
                logger.warning(
                    "daemon 执行器队列已满（上限 %d 笔，累计拒收 %d 次）—— "
                    "本次调用按「不可用」处理；持续出现说明有阻塞调用卡住了 worker",
                    self._max_queue, self._rejected,
                )
                raise RuntimeError(
                    f"daemon 执行器队列已满（上限 {self._max_queue} 笔）"
                ) from None
            self._pending += 1
        return fut

    @property
    def pending(self) -> int:
        """已入队、未完成（排队 + 执行中）的任务数。"""
        with self._lock:
            return self._pending

    @property
    def active_threads(self) -> int:
        """当前存活 worker 数（诊断 / 回归用）。"""
        with self._lock:
            return sum(1 for th in self._threads if th.is_alive())

    @property
    def capacity(self) -> int:
        """排队上限（不含正在执行的）。"""
        return self._max_queue

    @property
    def rejected(self) -> int:
        """因队列到顶被拒收的累计次数。"""
        with self._lock:
            return self._rejected

    def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
        """标记关闭；``cancel_futures=True`` 时**真的取消**队列里还没开始的任务。

        ``wait=True`` 会把这里唯一的改进毁掉：卡在系统调用里的那笔 IO 是 daemon 线程，
        进程退出时由解释器直接回收，这正是我们要的行为。

        v0.24.8（GPT 第四轮复核 P1）：``cancel_futures`` 以前是句空话 —— 只投哨兵，
        队列里的活儿照样**先于哨兵**执行。于是「停用日志监听」之后，积压的过期 IO
        还会一条条跑完（甚至写已停用实例的状态）。现在按线程池的语义来：
        关闭瞬间还在排队的任务逐个 ``cancel()``，``pending`` 同步减回去。
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except Empty:
                        break
                    if item is None:
                        continue
                    if item[0].cancel():          # 还没被 worker 领走 → 取消得掉
                        self._pending = max(0, self._pending - 1)
            # 哨兵与「关闭」标记在同一把锁里投递：此后不可能再有任务排在哨兵后面。
            # 已在队列里的任务仍会先于哨兵执行（与线程池 shutdown(wait=False) 同义）。
            # 队列满时用 put_nowait：worker 已能靠「队列空 + 已关闭」自行退出，
            # 哨兵塞不进去也不影响收尾（塞不进去就少一条加速路径，不能死锁）。
            for _ in range(self._max_workers):
                try:
                    self._queue.put_nowait(None)
                except Full:
                    break


#: v0.24.7：进程级 daemon 执行器（懒建）。给「**可能卡住的阻塞调用**」用 ——
#: 这类调用绝不该跑在事件循环的默认线程池上（那是全进程共享、且线程非 daemon）。
_DAEMON_RUNNER: "_DaemonThreadExecutor | None" = None
_DAEMON_RUNNER_LOCK = threading.Lock()


def daemon_executor() -> "_DaemonThreadExecutor":
    """进程级、有界并发的 daemon 执行器（懒建，v0.24.7；v0.24.8 有界化）。"""
    global _DAEMON_RUNNER
    if _DAEMON_RUNNER is None:
        with _DAEMON_RUNNER_LOCK:
            if _DAEMON_RUNNER is None:
                _DAEMON_RUNNER = _DaemonThreadExecutor(thread_name_prefix="blocking-io")
    return _DAEMON_RUNNER


async def run_blocking(fn, *args, timeout: float = 8.0, what: str = ""):
    """把**可能卡住的阻塞调用**丢进 daemon 线程，并给它一个等待上限。

    v0.24.7（GPT 第二轮复核 P2-9）：`asyncio.to_thread` 用的是事件循环的默认线程池
    （线程**非 daemon**）—— 一次卡在系统调用里的阻塞调用，既会和知识库 / 词典构建
    抢那几条共享线程，又会在退出时被解释器 join（进程迟迟不退）。这里统一改走
    daemon 线程，并返回 ``(ok, value)``：

      * 正常完成 → ``(True, 返回值)``；
      * 超时 / 抛错 → ``(False, None)``，并记一条 warn（线程杀不掉，只能不再等）。

    注意：超时**不代表**那个调用已经结束，调用方必须能接受「结果永远不回来」。

    ``what``（v0.24.7 补）：给日志用的中文名 —— 调用方是 ``functools.partial`` /
    lambda 时 ``fn.__name__`` 说不清是什么活儿，日志里得点名（例如「物品词典构建」）。
    """
    loop = asyncio.get_running_loop()
    name = what or getattr(fn, "__name__", str(fn))
    try:
        fut = loop.run_in_executor(daemon_executor(), fn, *args)
    except RuntimeError as e:                                # 执行器已关（理论上不会）
        logger.warning("阻塞调用 %s 无法提交（%s）", name, e)
        return False, None
    try:
        return True, await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        # v0.24.8（GPT 第四轮复核 P1 收尾）：**超时就把排队中的那笔撤下来**。
        # `wait_for` 超时只是「我不再等」—— 任务本身还在队列里排队，等 worker 有空
        # 就会照跑（过期 IO 白烧一次磁盘，还可能写已停用实例的状态）。
        # `Future.cancel()` 对「还没被 worker 领走」的任务是真的有效：worker 侧
        # `set_running_or_notify_cancel()` 会返回 False 并直接跳过（见 `_worker_loop`）。
        # 已经在跑的取消不掉（Python 杀不了线程），那时 cancel() 返回 False，如实记账。
        if fut.cancel():
            logger.warning("阻塞调用 %s 超过 %g 秒未返回，已把它从队列里撤回"
                           "（尚未开始执行，不会白跑）", name, timeout)
        else:
            logger.warning("阻塞调用 %s 超过 %g 秒未返回，本次按「不可用」处理"
                           "（已在执行中，无法中断；它跑在 daemon 线程里，"
                           "不会拖住进程退出）", name, timeout)
        return False, None
    except Exception as e:                                   # noqa: BLE001
        logger.warning("阻塞调用 %s 失败：%s: %s", name, type(e).__name__, e)
        return False, None


async def run_blocking_or_raise(fn, *args, timeout: float = 8.0, what: str = "阻塞调用"):
    """`run_blocking` 的「要结果、要异常」版本（v0.24.7）。

    v0.24.7 收口：仓库里最后几处 ``asyncio.to_thread``（物品词典构建、整合包指纹
    扫描）也一并改走 daemon 线程 —— 否则「阻塞调用一律不占默认线程池、线程一律
    daemon」这条承诺还留着口子，而那些正是**最慢、最该防卡住**的调用。

    与 `run_blocking` 的区别只在失败时：这里**抛 RuntimeError**，让调用方原来那套
    ``except Exception as e: 记进 results / parts`` 的诊断语句原样有效（信息不降级）；
    真正的异常类型与原因由 `run_blocking` 记进日志。
    """
    ok, value = await run_blocking(fn, *args, timeout=timeout, what=what)
    if not ok:
        raise RuntimeError(
            f"{what}未在 {timeout:g} 秒内返回（超时或执行出错，详见插件日志）"
        )
    return value


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

# v0.23.5 第五轮：单次文件 IO 的等待上限。IO 都在线程里跑（事件循环不会再被占住），
# 但**这一次 await** 仍可能永远不返回 —— 慢盘 / 网络盘 / 被独占的文件上，`_poll`
# 会停在原地不产出，`stop()` 也救不回已经交出去的线程。超过这个秒数就按「这一次
# 没读到」处理，让轮询与停止都不至于无限期挂着。
IO_TIMEOUT = 15.0

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
        # v0.23.5 第五轮：**还没探测** ≠「文件在」。旧写法默认 True —— 一个还没
        # start()（或 start() 的初始化 IO 没成功）的监听器，健康度上会显示「文件在」。
        self.file_present: bool = False
        # 是否成功探测过一次：health() 用它把「还没看过」和「看过了、文件在」分开
        self._probed: bool = False
        # 位置未知标记（v0.23.5 第五轮）：`_pos = -1` 表示 start() 没能定位到文件尾，
        # `_poll` 第一轮会直接落到文件尾 —— 位置未知时**绝不从 0 读**，那会把整份
        # 历史日志当成新事件重播一遍。
        # 文件 IO 超时次数（health() 摊开给界面与诊断看）
        self.io_timeouts: int = 0
        self.last_read_at: float = 0.0
        # 放弃等待的监听任务（stop() 超时时留下）。丢引用会让半途的任务被 GC 掉，
        # 留着还能在 health() / 诊断里看见「有个没退出的」。
        self._orphaned: set[asyncio.Task] = set()
        # ---- v0.23.5 第八轮（GPT 第七轮 P2）：监听**任务代** ----
        # 第七轮把 IO 计数按代隔离了（卡死的旧代 IO 不再挡新代），但 `_loop` 任务
        # 本身没有代际判据：`on_event` 回调若吞掉 `CancelledError`，`stop()` 超时
        # 只会把旧任务放进 `_orphaned` 等人；随后新的 `start()` 又把 `_running`
        # 置回 True —— 旧任务从吞掉取消的地方继续轮询，与新任务并发读写同一个
        # watcher 的状态（_pos / _partial / 事件派发相互交错）。
        # 对策：每次 start() 换一代并交给 `_loop`；stop() 在取消**之前**先让旧代
        # 失效；`_loop` 每轮、`_poll` 各关键节点逐一比对，代不符立即退出 ——
        # 取消被吞也复活不了（见 `_stale`）。
        self._task_gen: int = 0
        # 最近一次 stat 到的文件大小（health() 用它算 lag_bytes）
        self._last_size: int = 0
        # ---- v0.23.5 第六轮（GPT 第五轮 P2）：IO 生命周期 ----
        # `_io_wait` 能让协程按时返回，却**取消不了已经陷进系统调用的线程**。旧写法
        # 每轮都往默认线程池提交一笔：慢盘持续卡住时未完成的 IO 越堆越多，最后把
        # 整个进程的默认线程池（含知识库那些 to_thread）一起拖下水。现在：
        #   ① 单飞 —— 上一笔没回来就不再提交新的（未完成数钉在 1）；
        #   ② 专属有界执行器 —— 卡住的线程只占它自己那个池，不污染默认池。
        # 单飞的判据放在**线程侧**（`_io_inflight`，由真正跑 IO 的那根线程在
        # finally 里递减）—— 不用事件循环回调复位：回调绑定的是提交时那个循环，
        # 跨循环（测试里反复 asyncio.run / 热重载 / 循环已关闭）不保证被调度，
        # 那样「忙碌」可能永远为真、把监听永久卡死（比旧版更糟的失败模式）。
        self._io_lock = threading.Lock()
        self._io_inflight: int = 0
        # v0.23.5 第七轮（GPT 第六轮 P2）：在飞计数绑定到「代」—— stop() 后再
        # start() 会换一代（`_io_gen += 1`），旧代遗留在计数字段里的「卡死一笔」
        # 不会再挡住新代的 IO 闸门（换代时由下一次 `_io_wait` 清零）。
        self._io_gen: int = 0                 # 当前代（每次 start() +1）
        self._io_inflight_gen: int = 0        # 在飞计数属于哪一代
        self._io_task: asyncio.Future | None = None
        # v0.24.7：daemon 线程执行器（见 `_DaemonThreadExecutor`）—— 对外语义与
        # 单线程的 ThreadPoolExecutor 一致，但不会拖着进程退出。
        self._executor: "_DaemonThreadExecutor | None" = None
        self._fallback_ex: "_DaemonThreadExecutor | None" = None
        #: 生命周期锁（第七轮）：start / stop 互斥；惰性创建 + 跨事件循环自动换新
        self._life_lk: asyncio.Lock | None = None
        #: 生命周期方法的 owner loop（第九轮）：**显式记录** start / stop 归属哪个
        #: 事件循环。不再靠 `asyncio.Lock._loop` 私有属性做「事后探测」—— 锁还没
        #: 经历过竞争时该属性可能迟迟不绑定（首个持有者直接 acquire、没有等待者），
        #: 跨线程场景会因此复用到同一把锁、把等待者挂死在别的循环上（见 `_life_lock`）。
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        #: 因上一笔还没回来而**放弃提交**的次数（health() 摊开看）
        self.io_skipped: int = 0
        #: 未成行片段的缓冲（第六轮）：无换行时不前进 `_pos`，但也不许每轮重读同一段前缀
        self._partial: bytes = b""
        #: 读日志的累计字节数（第六轮：读放大可见，也是断言依据）
        self.read_bytes: int = 0
        #: v0.23.5 第七轮（GPT 第六轮 P2）：**短文件前缀快照**。文件不足
        #: `HEAD_BYTES` 时头指纹恒为空串（读不满就不比），「同 inode、同长度、
        #: 内容被原地替换」的短文件轮转会整类漏掉。这里保存最近一次读到的
        #: 「已知前缀」（≤ 255 字节）的摘要与长度：每轮回读同窗口比对，不一致 =
        #: 内容被重写过 = 轮转。只比**固定窗口的前缀**而不是整文件，正常追加
        #: （只动尾巴）不会误报。
        self._short_sig: str = ""
        self._short_len: int = 0
        # ---- v0.24.7（GPT 第二轮复核 P2）：事件回调的超时 / 在飞账本 ----
        #: 在飞回调任务 → 宽限期截止时刻（monotonic）
        self._callback_inflight: dict = {}
        #: 超时后仍不结束的「僵尸回调」（保留引用留痕，不占发放额度）
        self._callback_stuck: set = set()
        #: 已成功交付（回调正常返回）的条数
        self.callbacks_delivered: int = 0
        #: 超时放弃等待的条数
        self.callback_timeouts: int = 0
        #: 在飞额度满而**丢弃**的条数（事件没派发出去，只记账）
        self.callbacks_dropped: int = 0
        #: v0.24.8：停用 / 重载时被放弃的未完成回调数（含卡死的僵尸）。
        #: 「停用后还在发消息」是用户看得见的事故，但旧实现的 health() 里
        #: callbacks_stale 在 clear() 之后归零 —— 丢了几条，事后查不出来。
        self.callbacks_abandoned: int = 0

    def _stale(self, gen: "int | None") -> bool:
        """这个监听任务代号是否已过期（v0.23.5 第八轮，GPT 第七轮 P2）。

        `stop()` 使旧代失效（`_task_gen += 1`）、`start()` 登记新代；`_loop` /
        `_poll` 在关键节点调用本方法 —— 只要代与当前不符，**哪怕旧任务吞掉了
        取消、甚至 `_running` 已被再次置 True**（新的 start 已发生），也立即停下：
        不再读文件、不再改状态、不再派发事件。
        v0.23.5 第九轮（GPT 第八轮 P2）：检查面从「入口 / 读回数据后 / 派发前」
        扩到 `_poll` 的**每一处 IO `await` 恢复点** —— 旧任务在 `exists` / `stat` /
        锚点回读 / 头指纹 / 短文件快照 / 编码探测的等待期间被换代后，恢复执行的
        第一件事就是验代：代不符立即返回，`file_present` / `missing_polls` /
        `_last_size` / `_sig` / `_head` / `_short_sig` / `_enc` 等一个都不写。

        `gen is None` 表示调用方不在代际协议内（老测试 / 手工诊断直接调 `_poll()`），
        此时不做代际否决（行为与旧版一致）。
        """
        return gen is not None and gen != self._task_gen

    def _life_lock(self) -> asyncio.Lock:
        """start / stop 共用的生命周期锁（第七轮：并发调用串行化）。

        v0.23.5 第九轮（GPT 第八轮 P2）：改为**显式记录 owner loop**，不再用
        `asyncio.Lock._loop` 私有属性做「事后探测」。旧写法有个真实的死锁窗口：
        锁还没有经历过任何竞争时 `_loop` 可能迟迟不绑定（首个持有者直接 acquire、
        没有等待者），跨线程场景于是会复用到同一把锁 —— A 循环执行慢速 `start()`
        并持有锁时，B 线程 `asyncio.run(w.stop())` 拿到「尚未显示绑定」的同一把锁
        并开始等待；A 释放锁是在 A 的线程里跨线程唤醒 B 循环上的等待 Future，
        不保证被送达，B 线程会持续卡住。现在三种情形分开处理：

          - 首用 / owner 还是当前循环 → 复用（正常路径）；
          - owner 循环**已关闭**（测试反复 asyncio.run、热重载换循环）→
            接管：换一把新锁（旧锁随旧循环退役，不存在需要唤醒的等待者）；
          - owner 循环仍活着（运行中，或**停转但未关闭**）却换了循环 → 跨循环
            误用，抛**明确的 RuntimeError**（不再静默换锁让互斥悄悄失效，更不许
            复用同一把锁互相卡死）。第十轮收紧：停转未关闭不再接管 —— 其中可能
            仍有挂起的 start / 锁等待任务，旧循环恢复运行后会继续写状态、创建
            旧任务；只有已关闭的循环才允许自动接管。
            其他线程的正确做法：把调用转发到 owner 循环上执行 ——
            `asyncio.run_coroutine_threadsafe(w.stop(), owner_loop)`。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 不在事件循环里（手工诊断直调）：无从做归属判断，按旧行为兜底
            if self._life_lk is None:
                self._life_lk = asyncio.Lock()
            return self._life_lk
        owner = self._owner_loop
        if owner is None or owner is loop:
            # 首用 / 同循环复用（正常路径）：owner 记录本循环，锁对象不换
            if owner is None:
                self._owner_loop = loop
            if self._life_lk is None:
                self._life_lk = asyncio.Lock()
            return self._life_lk
        if owner.is_closed():
            # owner 循环已关闭（反复 asyncio.run / 热重载形态）：彻底退役，
            # 接管换锁 —— 关闭过的循环不会再调度任何任务，不存在要唤醒的等待者。
            self._owner_loop = loop
            self._life_lk = asyncio.Lock()
            return self._life_lk
        raise RuntimeError(
            "LogWatcher 的 start() / stop() 只能在它所属的事件循环里调用："
            f"当前 owner loop {owner!r} 尚未关闭，本次调用却来自另一个循环 {loop!r}。"
            "（owner 循环停转但未关闭时也一律拒绝：其中可能仍有挂起的生命周期调用，"
            "静默接管会让旧循环恢复后继续写状态。）跨线程 / 跨循环请转发到 owner "
            "循环执行：asyncio.run_coroutine_threadsafe(w.stop(), owner_loop)"
            "（start 同理）；若旧循环确已弃用，请先把它关闭（loop.close()），"
            "新循环才能自动接管。"
        )

    async def start(self) -> None:
        """从日志文件末尾开始监听（不回溯历史日志）。

        v0.23.5 第五轮（GPT 第四轮复核遗留）：
        - **幂等**：重复调用 `start()` 不再覆盖旧任务句柄（旧任务那时就再也 `stop()`
          不到了），先把上一轮收掉。
        - **初始化 IO 全部移出事件循环**：`exists / stat / 头指纹 / 编码探测` 都是文件
          IO，慢盘上会把整个事件循环（连同别处的超时回调）一起拖住。
        - `file_present` 初始值改 `False`：**还没探测** ≠「文件在」，健康度上两者必须
          分得开（见 `health()["probed"]`）。

        v0.23.5 第七轮（GPT 第六轮 P2）：
        - 整段动作套**生命周期锁**，与 `stop()` 互斥（不再有「停止正拆、启动正装」
          的交错窗口）；
        - 「先收旧」走 `_stop_locked()`（同一把锁内，不在公开 `stop()` 上重新拿锁）；
        - **换代**（`_io_gen += 1`）：上一代卡死在系统调用里的那笔 IO，其「在飞」
          标记从此与新代无关（见 `_io_wait` / `_io_busy`）—— 复用同一对象不会再
          被旧代挡停。

        v0.23.5 第八轮（GPT 第七轮 P2）：
        - 再换一个**任务代**（`_task_gen += 1`）并把代号交给 `_loop` —— 它是
          「吞掉取消的旧任务」与「重启后的新任务」之间的硬隔离点（见 `_stale`）。
        """
        async with self._life_lock():
            # 幂等保护：重复调用先收掉上一轮（与 stop() 同一套收尾路）
            await self._stop_locked()
            self._io_gen += 1
            # v0.23.5 第六轮（GPT 第五轮 P2）：IO 走**专属、有界**的执行器（1 个线程）——
            # 卡住的线程只占它自己的池，不再占默认线程池（那是全进程共享的）。
            # v0.24.7（GPT 第二轮复核 P2）：换成 **daemon 线程**版实现 —— 线程池的
            # 工作线程是非 daemon 的，卡在系统调用里时解释器退出会一直 join 它们，
            # 「停用之后进程迟迟不退」就是它。对外语义（返回 Future / 关闭后 RuntimeError）
            # 与 max_workers=1 完全一致。
            if self._executor is None:
                self._executor = _DaemonThreadExecutor(thread_name_prefix="logwatch")
            # v0.23.5 第十一轮（GPT 第十轮 P2/P3）：本次初始化的**任务代号**先记下 ——
            # 编码探测结果兑现后先复核它仍属当前代，才准许落共享字段。编码改由
            # `_prime_state` **随返回值带出**（不再在线程里直写）：旧代探测线程
            # 迟到返回时，结果只会落进一笔没人消费的 future。
            prime_gen = self._task_gen
            primed = await self._io_wait(self._prime_state, what="初始化定位日志文件",
                                         gen=prime_gen)
            # v0.23.5 第十一轮（GPT 第十轮 P2/P3）：结果兑现后复核代号 —— 只有仍属
            # 当前代才准许继续（整个 start() 持生命周期锁，正常路径恒真；这是跨循环
            # 僵尸调用的最后一道闸）。
            if self._stale(prime_gen):
                return
            if primed is None:
                # 定位失败：位置标成「未知」，第一轮轮询直接落到文件尾（不重播历史）；
                # `probed` 归 False —— 这一轮**确实没探测成功**，健康度不许自称看过了
                self._pos = -1
                self._sig, self._head = None, ""
                self._probed = False
                # v0.23.5 第六轮（GPT 第五轮 P2）：初始化超时/失败时**不许留着上一轮的
                # 「文件在」** —— 否则健康度会同时报 probed=false 与 file_present=true，
                # 两个字段互相打架（重启 watcher 且初始化超时时就会撞上）。
                self.file_present = False
            else:
                pos, sig, head, present, enc = primed
                self._pos, self._sig, self._head = pos, sig, head
                self.file_present = present
                self._probed = True
                self._enc = enc
            # v0.23.5 第三轮：锚点/跳过标记随之重置。重新开监听是从**文件尾**起步，
            # 旧锚点对新 _pos 没有意义，留着会误判一次轮转（→ 从 0 重读 → 重复播报）。
            self._anchor, self._anchor_at = b"", -1
            self._skip_to_newline = False
            # v0.23.5 第六轮：新监听从文件尾起步，旧的未成行缓冲对新 _pos 没有意义
            self._partial = b""
            # v0.23.5 第七轮：短文件快照同理 —— 留着旧内容会在重启后的第一轮
            # 误判一次轮转，把已经播报过的历史整段重播。
            self._short_sig, self._short_len = "", 0
            self.missing_polls = 0
            self._running = True
            # v0.23.5 第八轮（GPT 第七轮 P2）：登记新代，并把代号交给 `_loop`。
            self._task_gen += 1
            self._task = asyncio.create_task(self._loop(self._task_gen))

    def _prime_state(self) -> tuple[int, tuple[int, int] | None, str, bool, str]:
        """`start()` 需要的那几件文件活儿（**同步版**，只在 `asyncio.to_thread` 里跑）。

        抽出来是为了让初始化 IO 与轮询 IO 走同一条线：`exists / stat / 头指纹 /
        编码探测` 一个都不许占着事件循环（GPT 第四轮 P2）。

        v0.23.5 第十一轮（GPT 第十轮 P2/P3）：编码探测结果**随返回值交给调用方**
        （第 5 个元素）—— 本函数不再写任何共享字段。旧代探测线程迟到返回时，结果
        只会落进一笔没人消费的 future，不会再覆盖新代已经拿到的编码。
        """
        try:
            present = self.log_path.exists()
            pos = self.log_path.stat().st_size if present else 0
        except OSError:
            present, pos = False, 0
        sig = self._file_sig()
        head = self._head_sig()
        # 探测日志编码（UTF-8 / GBK），避免固定 UTF-8 导致中文乱码。
        # 只计算、不落字段：兑现与否由 `start()` 按代号复核后决定。
        try:
            enc = self._sniff_encoding()
        except Exception:                                # noqa: BLE001
            enc = "utf-8"
        return pos, sig, head, present, enc

    async def _io_wait(self, fn, *args, what: str = "", gen: "int | None" = None):
        """给一次文件 IO 加**超时**，并把它关进「最多一笔在飞」的笼子里。

        v0.23.5 第五轮：IO 已经在 `asyncio.to_thread` 里跑了，真正卡住事件循环的风险
        没了；但**这一次 await 本身**仍可能永不返回（慢盘 / 网络盘 / 被独占的文件），
        于是轮询停摆、`stop()` 也只能干等。

        v0.23.5 第六轮（GPT 第五轮 P2）：超时只是「不再等」—— Python 杀不掉已经陷进
        系统调用的线程，它会一直留在池里。旧写法每轮都再提交一笔，慢盘持续卡住时
        未完成 IO 会无限累积，最终把**默认线程池**（全进程共享）占满。现在：
          ① **单飞**：`_io_busy` 期间不再提交新 IO（未完成数最多 1），调用方按
             「这一轮没读到」处理；
          ② **专属执行器**：`start()` 里建的 `ThreadPoolExecutor(max_workers=1)`
             —— 卡住的线程只占它自己的池，不污染默认池；`stop()` 只 `shutdown(wait=False)`。
        被放弃的那一笔由 `_release_io` 在**真正结束**时收尾（取走异常、解除忙碌标记）。

        v0.23.5 第十轮（GPT 第九轮 P2）：新增 `gen` 参数 —— 调用方声明「这笔 IO
        的结果归属哪一代任务」。超时记账（`io_timeouts` / `last_error`）与跳过
        记账（`io_skipped`）一律先验代：旧代任务在新代启动后才兑现的超时 / 异常，
        一个共享健康字段都不许写。`gen is None`（老直调 / 手工诊断）不做代际
        否决，行为与旧版一致。

        v0.23.5 第十一轮（GPT 第十轮 P2）：执行器关闭（stop() 之后提交失败）
        的减计数路径同样按「IO 代」核对 —— 旧代提交失败不许动新代的在飞计数。
        """
        # v0.23.5 第七轮（GPT 第六轮 P2）：在飞计数绑定「代」—— 上一代（已经
        # stop 的某个执行器）遗留的卡死一笔不属于新代，换代即清零，不再把新代的
        # 所有 IO 挡在闸门外；旧的卡死线程回来时也会因代不符而不动新代计数（见 `_run`）。
        # 注意：本变量是**IO 代**（在飞计数口径）；参数 `gen` 是**任务代**
        #（`_stale` 代际否决口径）—— 两者是不同的计数体系，不要混用。
        io_gen = self._io_gen
        with self._io_lock:
            if self._io_inflight_gen != io_gen:
                self._io_inflight = 0
                self._io_inflight_gen = io_gen
            if self._io_inflight > 0:
                # 上一笔还没回来 → 不叠新的（这正是旧写法把线程堆起来的入口）
                # v0.23.5 第十轮：跳过记账同样过代际守卫（旧代不许写共享计数）
                if not self._stale(gen):
                    self.io_skipped += 1
                return None
            self._io_inflight += 1

        def _run():
            """真正跑 IO 的那一层：结束时**在 finally 里**把在飞计数减回去。

            放在线程侧是为了跨事件循环也能复位（回调可能根本没机会被调度）。
            v0.23.5 第七轮：减计数前核对「代」—— 旧代线程自然结束时不误动新代的计数。
            """
            try:
                return fn(*args)
            finally:
                with self._io_lock:
                    if self._io_inflight_gen == io_gen:
                        self._io_inflight -= 1

        loop = asyncio.get_running_loop()
        ex = self._executor
        if ex is None:
            # 兜底：还没 start()（或执行器已关）时用它自己的兜底执行器。
            # v0.24.7（GPT 第二轮复核 P2-9）：这里**不再走 `asyncio.to_thread`** ——
            # 那条路用的是事件循环的默认线程池（全进程共享，线程还**非 daemon**），
            # 「卡住的 IO 不占默认池、不拖住退出」这条承诺会被兜底路径悄悄绕过。
            fut: asyncio.Future = loop.run_in_executor(self._get_fallback_executor(), _run)
        else:
            try:
                fut = loop.run_in_executor(ex, _run)
            except RuntimeError:
                # 执行器刚在 stop() 里关掉 —— 这一轮按「没读到」处理。
                # v0.23.5 第十一轮（GPT 第十轮 P2）：减计数前核对「IO 代」—— 旧代
                # 提交在新代已接管、且新代有一笔 IO 在飞时失败时，不许把新代的
                # 在飞计数减掉（那会把「最多一笔在飞」的单飞闸门整扇打开）。
                with self._io_lock:
                    if self._io_inflight_gen == io_gen:
                        self._io_inflight -= 1
                # v0.23.5 第十轮：跳过记账同样过代际守卫
                if not self._stale(gen):
                    self.io_skipped += 1
                return None
        self._io_task = fut
        fut.add_done_callback(self._release_io)
        try:
            # shield：`wait_for` 超时**不许**取消这笔 IO（取消也杀不掉线程，只会让
            # 结果无处可去，还给收尾添一句「never retrieved」）
            return await asyncio.wait_for(asyncio.shield(fut), IO_TIMEOUT)
        except asyncio.TimeoutError:
            # v0.23.5 第十轮（GPT 第九轮 P2）：超时记账先验代 —— 旧代 IO 在新代
            # 启动后才兑现的超时，不许记进新代的 `io_timeouts` / `last_error`
            #（日志仍照打：那是「真实发生过」的诊断留痕，不是健康度）。
            if not self._stale(gen):
                self.io_timeouts += 1
                self.last_error = f"文件 IO 超时（{what or '未知操作'} > {IO_TIMEOUT:g}s）"
            logger.warning("日志监听：%s 超过 %g 秒未返回，本轮按「没读到」处理；"
                           "在它返回前不再提交新的读取（最多 1 笔在飞）",
                           what or "文件 IO", IO_TIMEOUT)
            return None

    def _get_fallback_executor(self) -> "_DaemonThreadExecutor":
        """兜底执行器（还没 start() / 执行器刚被 stop() 关掉时用）。

        v0.24.7（GPT 第二轮复核 P2-9）：兜底路径此前是 `asyncio.to_thread` ——
        它跑在事件循环的**默认线程池**上（全进程共享，线程还**非 daemon**），
        「卡住的 IO 只占自己的池、不拖住进程退出」这条承诺在这里被绕过了。
        现在兜底也是一个自有的 daemon 单线程执行器，语义与主执行器完全一致；
        它随 watcher 实例存活（**不**在 stop() 里关掉 —— 孤儿旧代任务还在用它）。
        """
        ex = self._fallback_ex
        if ex is None:
            ex = _DaemonThreadExecutor(thread_name_prefix="logwatch-fallback")
            self._fallback_ex = ex
        return ex

    @property
    def _io_busy(self) -> bool:
        """还有一笔 IO 在飞（判据是**线程侧**的在飞计数，与事件循环无关）。

        v0.23.5 第七轮：只认**当前代**的计数 —— 旧代（已 stop 的执行器）遗留的
        卡死一笔不该让新代永远「忙」（那正是复用同一 watcher 全面停摆的根因）。
        """
        with self._io_lock:
            if self._io_inflight_gen != self._io_gen:
                return False
            return self._io_inflight > 0

    def _release_io(self, fut) -> None:
        """收尾一笔 IO：取走异常、解除引用（v0.23.5 第六轮）。

        忙碌判据不在这里（它在 `_run` 的 finally 里、线程侧）—— 这个回调只负责
        不让被放弃的那笔留下 "Task exception was never retrieved"（与
        `_reap_orphan` 同一个道理）。
        """
        if fut is self._io_task:
            self._io_task = None
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except Exception:                                # noqa: BLE001
            return
        if exc is not None:
            logger.warning("日志监听：被放弃的那次 IO 以异常收尾：%s: %s",
                           type(exc).__name__, exc)

    def _reap_orphan(self, task: asyncio.Task) -> None:
        """孤儿任务收尾（v0.23.5 第五轮）：取走异常并解除引用。

        此前只 `self._orphaned.discard` —— 任务若带着异常结束，异常没有任何人取过，
        解释器 GC 时会骂一句「Task exception was never retrieved」，而它到底为什么
        没退出反而没人看（GPT 第四轮 P2）。
        """
        self._orphaned.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:                                # noqa: BLE001 —— 取不到也不许炸
            return
        if exc is not None:
            logger.warning("日志监听孤儿任务异常收尾：%s: %s",
                           type(exc).__name__, exc)

    async def stop(self) -> None:
        """停止监听（公开入口：拿生命周期锁后走 `_stop_locked`）。

        v0.23.5 第七轮（GPT 第六轮 P2）：停止逻辑抽到 `_stop_locked` —— `start()`
        的「先收旧」与 `stop()` 共用同一条收尾路（同一把生命周期锁内），
        「停止正拆、启动正装」的竞态窗口随之关闭。
        """
        async with self._life_lock():
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        """停止监听的实际逻辑（**调用方须已持有生命周期锁**）。

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

        v0.23.5 第八轮（GPT 第七轮 P2）：**取消任务之前先让旧任务代失效**
        （`_task_gen += 1`）—— 这是对「吞掉 CancelledError 的旧任务」的硬隔离：
        哪怕它一直活到新的 `start()` 把 `_running` 置回 True 之后，也会在任一
        代际检查点（`_loop` 每轮 / `_poll` 各落点）自行退出。
        """
        self._running = False
        # v0.24.7（GPT 第二轮复核 P2）：回调现在是**独立任务**（见 _dispatch_event），
        # 不再挂在轮询任务上 —— 这里必须显式取消，否则「停用 / 重载」之后还会有一条
        # 回调继续往会话里推消息（活过一个已经停用的插件）。
        self._cancel_callbacks()
        # v0.23.5 第八轮：先失效旧代、再取消 —— 取消可能被回调吞掉，代际不会。
        self._task_gen += 1
        task, self._task = self._task, None
        # v0.23.5 第六轮：专属执行器跟着停 —— `wait=False` 表示**不等**卡在系统调用里
        # 的那笔 IO（它待在它自己的池里；等它会毁掉 stop() 的 5 秒承诺）。
        ex, self._executor = self._executor, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:                        # 3.8 及更早没有 cancel_futures
                ex.shutdown(wait=False)
        self._io_task = None
        if task is None:
            return
        # v0.23.5 第九轮（GPT 第八轮 P2）：任务属于**别的**事件循环时（反复
        # asyncio.run 的「接管」收尾、热重载换循环），这里已没有能做的事 ——
        # 跨循环 `asyncio.wait` 等不到回调（对 closed 循环实测直接抛
        # 「Event loop is closed」，被吞成误导性的「停止异常」日志），跨线程
        # `cancel()` 也不是线程安全的。旧循环要么已关闭（asyncio.run 取消过
        # 所有任务）、要么已停转；任务代已失效，它即便复活也会在下一个代际
        # 检查点自行退出。保持安静，直接收尾。
        if task.get_loop() is not asyncio.get_running_loop():
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
            # 保住引用：让它继续跑完（_running 已置 False 且旧代已失效，它到下一个
            # 检查点会自己退出），但**别丢引用** —— 半途被 GC 掉的任务连痕迹都不剩，
            # 诊断时查无此人。
            self._orphaned.add(task)
            task.add_done_callback(self._reap_orphan)
            logger.warning(
                "日志监听任务在 %.1fs 内未退出，已放弃等待（多半卡在文件 IO 上）；"
                "任务本身会在回到下一个代际检查点时自行退出（进程退出时会一并回收）",
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
            # v0.23.5 第五轮：把「还没探测过」单独摊出来 —— 一个还没 start() 的监听器
            # 与「文件真的不在」是两回事，混在一个布尔里就分不清了。
            "probed": bool(self._probed),
            "missing_polls": int(self.missing_polls),
            "io_timeouts": int(self.io_timeouts),
            # v0.23.5 第六轮：单飞后的两笔账 —— 因为上一笔没回来而跳过多少轮、
            # 还有多少字节的半行挂在缓冲里；读放大也能从 read_bytes 上看出来。
            "io_skipped": int(self.io_skipped),
            "io_busy": bool(self._io_busy),
            # v0.23.5 第七轮：只报**当前代**的在飞数（旧代残留对新一代没有意义）
            "io_inflight": (int(self._io_inflight)
                            if self._io_inflight_gen == self._io_gen else 0),
            "partial_bytes": len(self._partial),
            "read_bytes": int(self.read_bytes),
            "lag_bytes": max(0, int(self._last_size) - int(self._pos)),
            "last_read_at": float(self.last_read_at),
            "orphaned_tasks": len(self._orphaned),
            # v0.23.5 第八轮：监听任务代 —— 「第几代在跑」一眼可见（诊断孤儿 /
            # 新旧并发时对照用；每次 start() / stop() 都会前进）
            "task_gen": int(self._task_gen),
            # v0.24.7（GPT 第二轮复核 P2）：回调派发的四笔账 —— 回调卡住时
            # 旧实现在这里什么都看不见（轮询停摆、pos 不动、error_count 是 0）。
            "callbacks_delivered": int(self.callbacks_delivered),
            "callback_timeouts": int(self.callback_timeouts),
            "callbacks_dropped": int(self.callbacks_dropped),
            "callbacks_inflight": len(self._callback_inflight),
            "callbacks_stale": len(self._callback_stuck),
            "callbacks_abandoned": int(self.callbacks_abandoned),
            # v0.24.8：进程级阻塞执行器的积压（排队 + 执行中）。有界池的代价是
            # 可能排队 —— 饱和时必须能从 health() 看见，而不是只表现为「页面变慢」。
            # 执行器尚未建立（没发生慢调用）时按 0 报，不为一次健康检查拉起 4 条线程。
            "daemon_pending": (_DAEMON_RUNNER.pending if _DAEMON_RUNNER is not None else 0),
            # v0.24.8（GPT 第四轮 P1）：有界队列的两个数也要能看见 ——
            # 「拒收过几次」「上限是多少」决定了「偶发不可用」是不是队列打满。
            "daemon_rejected": (_DAEMON_RUNNER.rejected if _DAEMON_RUNNER is not None else 0),
            "daemon_capacity": (_DAEMON_RUNNER.capacity if _DAEMON_RUNNER is not None else 0),
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

    async def _loop(self, gen: "int | None" = None) -> None:
        # v0.23.5 第八轮（GPT 第七轮 P2）：**任务代**。循环每轮都比对 `gen` 与当前代
        # —— `stop()` 先使旧代失效，哪怕旧任务吞掉了取消、又赶上 `_running` 被新的
        # `start()` 置回 True，它也过不了这道检查（旧写法正是从这里继续跟随
        # `_running` 复活的）。`gen is None` 表示直调（老测试 `w._loop()`），按当前代处理。
        if gen is None:
            gen = self._task_gen
        while self._running and gen == self._task_gen:
            try:
                before = self.error_count
                await self._poll(gen)
                # v0.23.5 第九轮（GPT 第八轮 P2）：`_poll` 返回后先验代 —— 换代
                #（stop + 重开）之后的旧任务连错误计数也不许碰：清零是「这一代
                # 恢复正常」的记账，旧代早已不是当前的一员。
                if self._stale(gen):
                    return
                # v0.23.5 第四轮：**不能无条件清零**。`_poll()` 遇到 stat / read
                # 失败时是「自己递增计数后正常 return」（不抛异常），原先在这里
                # 直接 `= 0` 会把刚记上的错误当场抹掉 —— health()["error_count"]
                # 于是永远是 0，只剩 last_error 能看见最近一次错误（GPT 第四轮）。
                # 判据：这一轮**没新增**错误，才算「恢复正常」。
                if self.error_count == before:
                    self.error_count = 0
            except Exception as e:                       # noqa: BLE001
                # v0.23.5 第九轮（GPT 第八轮 P2）：旧代抛出的异常同样不许记账 ——
                # `error_count` / `last_error` 属于**当前代**；换代后立即静默退出
                #（不 sleep、不继续），旧任务到不了下一轮。
                if self._stale(gen):
                    return
                # 日志读取失败不中断监听循环；但要留痕，别让「监听已死」瞒着所有人
                self.error_count += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.error_count == 1 or self.error_count % 60 == 0:
                    logger.warning(
                        "日志监听轮询异常（连续第 %d 次，文件 %s）：%s",
                        self.error_count, self.log_path, e,
                    )
            await asyncio.sleep(self.poll_interval)

    def _read_at(self, offset: int, size: int) -> bytes:
        """同步块读（只在 `_poll` 的 `asyncio.to_thread` 里调用）。

        v0.23.5 第四轮：抽出来是为了让 `_poll` 的 IO 全部跑在线程里 ——
        同步 IO 占住事件循环时，`stop()` 的 5 秒 wall-clock 承诺是给不出来的。
        `OSError` 一律向上抛：由调用方决定「算真错误」还是「当作没读到」。
        """
        with open(self.log_path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    async def _poll(self, gen: "int | None" = None) -> None:
        # v0.23.5 第八轮（GPT 第七轮 P2）：旧代任务不得碰任何状态 —— 进 poll 先验代。
        # 吞掉取消的旧任务正是从 `await` 恢复处继续往里走的。
        # v0.23.5 第九轮（GPT 第八轮 P2）：检查面扩到**每一处 IO `await` 的恢复点**
        #（`exists` / `stat` / 锚点回读 / 头指纹 / 短文件快照 / 编码探测 + 原有的
        # 「读回数据后」与「每个派发点前」）—— 旧任务在等待期间被换代，恢复后
        # 一个状态字段都不许再写。下面所有插入点带同一句 `第九轮` 注释，可 grep 对账。
        # v0.23.5 第十二轮（GPT 第十一轮 P3）：「文件在 + 缺失清零」的提交点也纳入
        # 代际协议 —— 从 stat 等待之前后移到其恢复点校验之后（可 grep `第十二轮` 对账）。
        if self._stale(gen):
            return
        # v0.23.5 第六轮（GPT 第五轮 P2）：上一笔 IO 还没回来（慢盘 / 网络盘 / 被独占
        # 的文件）→ 本轮直接跳过，不再往池子里叠一笔。旧写法每轮都提交新的，
        # 未完成 IO 会无限累积；超时留痕由 `_io_wait` 负责（io_timeouts / last_error）。
        if self._io_busy:
            self.io_skipped += 1
            return
        # v0.23.5 第四轮：**文件 IO 全部移到线程里**。这里原本是同步的
        # `exists / stat / open / read`，一旦底层 IO 真被卡住（慢盘、网络盘、
        # 被独占的文件），阻塞的是**整个事件循环** —— 于是 `stop()` 里那句
        # `asyncio.wait(timeout=5)` 也给不出 wall-clock 保证：事件循环根本没机会
        # 执行超时回调（GPT 第四轮）。判定逻辑一行没动，只是让 IO 不再占着循环。
        present = await self._io_wait(self.log_path.exists, what="检查日志文件是否在",
                                      gen=gen)
        # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代 —— 旧任务不得再写任何
        # 状态（被 stop() 换代后恢复执行的旧任务在这里立即退场，不碰
        # file_present / missing_polls）
        if self._stale(gen):
            return
        if present is None:
            # 超时：**这一轮没读到** —— 不碰 file_present / missing_polls
            # （超时 ≠ 文件不在，混为一谈会让健康度报假警）
            return
        if not present:
            # v0.23.5 第三轮：文件暂时不在，是轮转窗口里的**正常现象**（旧文件已删、
            # 新文件还没建），所以不记 error；但计数要落进 health()，让「一直不在」
            # 看得见 —— 此前这种情况就是安静地 return，界面绿灯、pos 不动、事件消失。
            self.file_present = False
            self.missing_polls += 1
            return
        # v0.23.5 第十二轮（GPT 第十一轮 P3）：「文件在 + 缺失清零」的提交点已**后移**到
        # stat 返回、且代际校验通过之后（见下方注释）。旧位置写在等待之前：换代发生在
        # stat 等待期间时，这笔已落账的半提交不会被任何后续 stale 检查撤销 —— 旧代
        # 阵亡后，health() 会留下「file_present 报『在』、但 _last_size / _sig 从未更新」
        # 的不一致组合。此处不再写任何字段。
        try:
            st = await self._io_wait(self.log_path.stat, what="读取日志文件属性", gen=gen)
        except OSError as e:
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代（异常分支同样不许记账）
            if self._stale(gen):
                return
            self.error_count += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return
        # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代（超时 / 正常返回都先挡）
        if self._stale(gen):
            return
        # v0.23.5 第十二轮（GPT 第十一轮 P3）：**到这里才提交「文件在 + 缺失清零」** ——
        # 与 `_last_size` / `_sig` 同属这一轮 poll 的观测结果：过闸才落账、整笔落或
        # 一笔不落。异常分支不提交（exists 与 stat 之间文件被删 / 被换的 TOCTOU 时刻
        # 正是 OSError 高发区，维持既有观测比抢落「在」更保守；下一轮 exists 会给出真结论）。
        self.file_present = True
        self.missing_polls = 0
        if st is None:
            # 超时（_io_wait 已留痕：io_timeouts + last_error）→ 这一轮当作没读到
            self.error_count += 1
            return
        size = st.st_size
        self._last_size = size
        sig = (int(getattr(st, "st_ino", 0) or 0), size)
        if self._pos < 0:
            # v0.23.5 第五轮：位置未知（start() 的初始化 IO 超时/被拒）→ **直接落到
            # 文件尾**。位置未知时从 0 读 = 把整份历史日志当新事件重播一遍，
            # 比漏几条严重得多；「不回溯历史日志」是 start() 的既有承诺。
            self._pos = size
            self._sig = sig
            self._anchor, self._anchor_at = b"", -1
            self._skip_to_newline = False
            self._partial = b""
            return
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
        # v0.23.5 第七轮（GPT 第六轮 P2）：判据①扩展到**半行缓冲** ——「已读到」
        # 的范围是 `_pos + len(_partial)`（缓冲里的字节同样来自文件）。文件被原地
        # 截断到 `_pos < size < _pos + len(_partial)` 时旧判据看不见它：之后从旧
        # 偏移读到的永远是空 / 错位内容，新日志漏播、旧半行被拼到新内容上。
        rotated = size < self._pos + len(self._partial)
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
                now_anchor = await self._io_wait(self._read_at, self._anchor_at,
                                                 len(self._anchor), what="回读内容锚点",
                                                 gen=gen)
            except OSError:
                now_anchor = None
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
            if self._stale(gen):
                return
            if now_anchor is not None and now_anchor != self._anchor:
                rotated = True
        # ③ 头指纹：不再要求 size == _pos。正常追加时文件头永不改变，而这次读取
        #    本来每轮都要做（下面刷新 self._head 用的是同一次调用），等于零额外成本。
        if not rotated and self._head:
            head = await self._io_wait(self._head_sig, what="读取文件头指纹", gen=gen)
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
            if self._stale(gen):
                return
            rotated = bool(head) and head != self._head
        # ③b v0.23.5 第七轮（GPT 第六轮 P2）：**短文件前缀快照**。文件不足
        #     HEAD_BYTES 时头指纹恒为空串（读不满就不给指纹），「同 inode、同长度、
        #     内容被原地替换」整类漏掉 —— 旧内容被换掉之后，新内容永远不会被读取。
        #     对策：保存「最近一次读到的已知前缀」（≤ 255 字节的摘要 + 长度），
        #     每轮回读**同窗口**比对；只比固定窗口的前缀（不是整文件），正常追加
        #     不会误报（追加不改前缀）。文件长过 HEAD_BYTES 后交给头指纹，快照清除。
        if size < HEAD_BYTES:
            try:
                snap = await self._io_wait(self._read_at, 0, size,
                                           what="回读短文件内容快照", gen=gen)
            except OSError:
                snap = None
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
            if self._stale(gen):
                return
            if snap is not None and len(snap) == size:
                if (not rotated and self._short_len > 0
                        and len(snap) >= self._short_len
                        and hashlib.md5(snap[: self._short_len]).hexdigest()
                        != self._short_sig):
                    rotated = True
                if snap:
                    self._short_sig = hashlib.md5(snap).hexdigest()
                    self._short_len = len(snap)
                else:
                    self._short_sig, self._short_len = "", 0
        else:
            if not rotated and self._short_len > 0:
                try:
                    snip = await self._io_wait(self._read_at, 0, self._short_len,
                                               what="回读短文件前缀快照", gen=gen)
                except OSError:
                    snip = None
                # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
                if self._stale(gen):
                    return
                if (snip is not None and len(snip) == self._short_len
                        and hashlib.md5(snip).hexdigest() != self._short_sig):
                    rotated = True
            self._short_sig, self._short_len = "", 0
        if rotated:
            # latest.log 被重建 → 从头读取（重新嗅探编码）
            self._pos = 0
            self._anchor, self._anchor_at = b"", -1
            self._skip_to_newline = False
            self._partial = b""
            # v0.23.5 第七轮：头指纹一并清掉 —— 它认的是**旧文件**的头，新文件的
            # 头在本轮重读后重新记录；留着会在下一轮误触发一次「轮转」（重复播报）。
            self._head = ""
            try:
                enc = await self._io_wait(self._sniff_encoding, what="探测日志编码", gen=gen)
            except Exception:
                enc = None
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
            if self._stale(gen):
                return
            if enc:
                self._enc = enc
            logger.info("检测到日志轮转（%s），已从头开始读取", self.log_path)
        self._sig = sig
        if not head:
            head = await self._io_wait(self._head_sig, what="读取文件头指纹", gen=gen)
            # v0.23.5 第九轮（GPT 第八轮 P2）：IO 恢复点先验代
            if self._stale(gen):
                return
        # 超时（None）时保留上一次的头指纹：清空等于把判据③关掉一轮，没必要
        self._head = head if head is not None else self._head
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
            self._partial = b""
            return
        if self._skip_to_newline:
            # 丢余部时不该留着半行缓冲（它的前缀已按超长行丢弃）
            self._partial = b""
        # v0.23.5 第六轮（GPT 第五轮 P2）：**只读新增部分** —— 未成行的片段留在
        # `_partial` 里，下一轮从它末尾接着读。旧写法每轮都从同一 offset 重读整段
        # 前缀，行越长读得越狠（读放大）。
        read_from = self._pos + len(self._partial)
        room = MAX_READ_BYTES - len(self._partial)
        if room <= 0:
            room = MAX_READ_BYTES
        try:
            # 二进制读取：编码在解码阶段逐行自适应（GBK/UTF-8）。
            # 读多少有上限，见 MAX_READ_BYTES。
            data = await self._io_wait(self._read_at, read_from, room,
                                       what="读取日志新增内容", gen=gen)
        except OSError as e:
            # v0.23.5 第十轮（GPT 第九轮 P2）：IO 恢复点先验代 —— 等待期间被
            # 换代的旧任务，这笔迟到的 OSError 属于旧代；`error_count` /
            # `last_error` 是新代健康度，不许被它写。
            if self._stale(gen):
                return
            # 打开/读取失败是**真错误**（权限、被独占、盘掉了）→ 留痕，
            # 别再像以前那样安静地 return 让监听看起来一切正常。
            self.error_count += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return
        if not data:
            return
        # v0.23.5 第八轮（GPT 第七轮 P2）：数据读回来了，但监听器可能已经换代
        #（stop() 超时 + 新 start()）—— 这一轮结果整段作废：不推进 `_pos` / 锚点 /
        # 缓存，也不派发。吞掉取消的旧任务恢复执行时首先撞上的就是这组检查。
        if self._stale(gen):
            return
        self.read_bytes += len(data)
        if self._partial:
            # 接上上一轮留下的半行，再走原来的「只消费完整行」逻辑
            data = self._partial + data
            self._partial = b""
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
                # v0.23.5 第六轮（GPT 第五轮 P2）：这是「还没写完的半行」，不是超长行。
                # 旧写法直接 return、`_pos` 不动 → 下一轮把这段前缀**整段重读**一遍，
                # 行一边长一边重读 = 读放大。现在把它存进 `_partial`，下一轮只读新增
                # 部分；`_pos` 仍不动（它表示「已消费到哪」，这半行还没被消费）。
                self._partial = data
                return
            # v0.23.5 第七轮（GPT 第六轮 P2）：截断掉的「尾巴」必须存回 `_partial`
            # —— 否则下一轮会从 `_pos` 重新把它整段读一遍（读放大），与第六轮
            # 「只读新增」的设计承诺相悖。下一轮的 `read_from = _pos + len(_partial)`
            # 会自动跳过这段已经读进内存的尾巴。
            tail = data[cut + 1:]
            data = data[: cut + 1]
            self._partial = tail
        self._pos += len(data)
        # v0.23.5 第三轮：记下刚消费掉的最后一段当**内容锚点**，下轮回读比对 ——
        # 用来认「既不换 inode、也不变短」的原地重写（见上面判据 ④）。
        self._anchor = data[-ANCHOR_BYTES:]
        self._anchor_at = self._pos - len(self._anchor)
        self.last_read_at = time.monotonic()
        for line in self._decode_chunk(data).splitlines():
            # v0.23.5 第八轮（GPT 第七轮 P2）：逐行消费也验代（旧任务恢复后不得再往下走）
            if self._stale(gen):
                return
            if not line.strip():
                continue
            if len(line) > MAX_LINE_CHARS:
                # v0.23.5：超长单行先截断再解析（正则只看行首那段），
                # 免得几十万字符的行拖慢正则回溯、白占内存。
                line = line[:MAX_LINE_CHARS]
            for etype, player, detail in self._parse_line_multi(line):
                # v0.23.5 第八轮（GPT 第七轮 P2）：每个派发点前验代 —— 旧任务吞掉
                # 取消后，恢复执行的位置就在某个 `await self.on_event(...)` 内，
                # 这里一挡，它再也派发不出下一条（连「下一行」也见不到上面那处检查）。
                if self._stale(gen):
                    return
                # v0.24.7（GPT 第二轮复核 P2）：改成带**超时 + 在飞上限**的派发。
                # 旧实现直接 `await self.on_event(...)`：回调里做的是平台发送 / 桥接
                # 推送，平台侧一卡（网络不通、会话不存在、限流）它就永不返回 ——
                # 轮询整体停摆、stop() 只能干等，健康度上还什么都看不见。
                await self._dispatch_event(etype, player, detail)

    # ============ v0.24.7：事件回调的超时与在飞上限 ============

    def _callback_slot_free(self) -> bool:
        """在飞回调是否还有额度（顺手回收已结束的、以及过了宽限期的僵尸）。

        僵尸（超时后仍不结束）**不再占额度**：一个永久卡死的回调若继续占坑，
        后续事件会被全部饿死 —— 那等于把「回调卡住」从「卡住轮询」换成
        「静默丢弃所有事件」，两种都不能要。所以额度只统计还在正常期限内的，
        卡死的那批改由 `callbacks_stale` 记账、引用保留，卡死这件事必须留痕。
        """
        now = time.monotonic()
        for task, deadline in list(self._callback_inflight.items()):
            if task.done():
                self._callback_inflight.pop(task, None)
                continue
            if deadline <= now:
                self._callback_inflight.pop(task, None)
                self._remember_stuck(task)
                logger.warning(
                    "日志监听：一个事件回调超过 %g 秒仍未结束，不再占用在飞额度"
                    "（它可能吞掉了取消；已记入 callbacks_stale）", CALLBACK_STUCK_GRACE,
                )
        return len(self._callback_inflight) < MAX_CALLBACK_INFLIGHT

    def _remember_stuck(self, task) -> None:
        """把卡死的回调放进留痕集合；集合到顶就丢最旧的一条（防无界增长）。"""
        self._callback_stuck.add(task)
        while len(self._callback_stuck) > MAX_STUCK_CALLBACKS:
            oldest = next(iter(self._callback_stuck))
            self._callback_stuck.discard(oldest)
            logger.warning("日志监听：卡死回调留痕已到上限 %d，丢弃最旧的一条引用",
                           MAX_STUCK_CALLBACKS)

    async def _safe_callback(self, etype: str, player: str, detail: str) -> None:
        """回调包装：**吞掉**普通异常（单条事件失败不影响后续），但让取消穿透。"""
        try:
            await self.on_event(etype, player, detail)
        except asyncio.CancelledError:
            raise
        except Exception:                                        # noqa: BLE001
            pass

    def _reap_callback(self, task) -> None:
        """回调任务收尾：解除引用并取出异常（免得留一堆 never-retrieved 噪声）。"""
        self._callback_inflight.pop(task, None)
        self._callback_stuck.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:                                        # noqa: BLE001
            return
        if exc is not None:
            logger.warning("日志监听：事件回调以异常收尾：%s: %s",
                           type(exc).__name__, exc)

    async def _dispatch_event(self, etype: str, player: str, detail: str) -> bool:
        """派发一条事件给 ``on_event``，**带超时与在飞上限**（v0.24.7 · GPT 第二轮复核 P2）。

        为什么不能直接 await：回调是插件侧的用户代码，里面做平台发送 / 桥接推送，
        平台侧卡住时它永不返回 —— 日志轮询会连带停摆（事件不再更新），
        `stop()` 也只能等它，健康度上没有任何指标能看出「是回调卡了」。

        现在的口径：

          * 回调跑成**独立任务**，用 ``asyncio.wait`` 等它 10 秒。刻意**不用**
            ``wait_for`` —— 子任务若吞掉 ``CancelledError``（本文件反复踩过的形态），
            ``wait_for`` 会一直等它收尾，超时形同虚设；``asyncio.wait`` 到期即返回。
          * 超时 → 取消该任务、记账、告警，然后**立刻返回**，轮询继续跑；
          * 在飞任务数达上限 → 丢弃这条事件并记账（超时杀不掉、重复取消也可能被吞，
            所以必须有硬上限，否则它们会无限堆积）。

        返回是否已交付（``False`` = 超时或丢弃）。
        """
        if not self._callback_slot_free():
            self.callbacks_dropped += 1
            logger.warning(
                "日志监听：事件回调在飞数已达上限 %d，本条事件（%s）不再派发"
                "（回调卡住时不许无限堆积）", MAX_CALLBACK_INFLIGHT, etype,
            )
            return False
        task = asyncio.ensure_future(self._safe_callback(etype, player, detail))
        self._callback_inflight[task] = time.monotonic() + CALLBACK_STUCK_GRACE
        task.add_done_callback(self._reap_callback)
        try:
            done, _pending = await asyncio.wait({task}, timeout=CALLBACK_TIMEOUT)
        except asyncio.CancelledError:
            # 自己（轮询任务）被取消：把这条回调一起带走，别再往下派发
            task.cancel()
            raise
        except Exception as e:                                   # noqa: BLE001
            logger.warning("日志监听：等待事件回调时出错（已忽略）：%s", e)
            return False
        if task in done:
            self.callbacks_delivered += 1
            return True
        self.callback_timeouts += 1
        logger.warning(
            "日志监听：事件回调（%s）超过 %g 秒未返回，已放弃等待并取消；轮询继续"
            "（在飞上限 %d 个）", etype, CALLBACK_TIMEOUT, MAX_CALLBACK_INFLIGHT,
        )
        task.cancel()
        return False

    def _cancel_callbacks(self) -> None:
        """取消全部在飞回调（stop() 用）。

        v0.24.7：回调已改成独立任务，**不再**挂在轮询任务上 —— 老实现里 stop()
        取消轮询时回调会随之消失，现在必须显式取消，否则一次「停用 / 重载」之后
        还会有一条回调继续往会话里推消息（活过一个已停用的插件）。
        v0.24.8（GPT 第三轮复核 P2）：卡死的那批**必须一起取消**。
        旧实现只遍历 `_callback_inflight`，而僵尸在超时那一刻就被移进
        `_callback_stuck` 了 —— 于是「停用」之后恰恰是那条已经卡住的回调还活着：
        它一旦从卡顿里醒来，照样把消息推给会话（活过一个已停用的插件）。
        取消对它们未必有效，所以还要记账进 `callbacks_abandoned`（进 health()）：
        「停用时丢了几条」必须事后查得到，不能靠 clear() 把痕迹抹掉。
        """
        victims = set(self._callback_inflight) | set(self._callback_stuck)
        for task in victims:
            try:
                task.cancel()
            except Exception:                                    # noqa: BLE001
                pass
        if victims:
            self.callbacks_abandoned += len(victims)
            logger.warning(
                "日志监听：停用时放弃 %d 个未完成回调（其中卡死僵尸 %d 个）—— "
                "已记入 callbacks_abandoned", len(victims), len(self._callback_stuck),
            )
        self._callback_inflight.clear()
        self._callback_stuck.clear()

    # ============ 编码自适应（UTF-8 / GBK） ============
    # Minecraft/Forge 在中文 Windows 下常以 GBK(cp936) 写日志（System.out 默认
    # 字符集）。若固定按 UTF-8 读取，中文会变成乱码，导致「!群」这类中文前缀
    # 永远匹配不上。
    # 注意：不能简单地「能解码就用」——例如 GBK 的「群」(Èº) 恰好也是
    # 合法的 UTF-8 序列（会解成「Ⱥ」），因此这里改用「可读性评分」择优。

    @classmethod
    def _text_score(cls, text: str, enc: str = "") -> int:
        """给解码结果打「可读性」分：中文/ASCII 加分，乱码区（拉丁扩展/希腊/西里尔）扣分。

        v0.24.4（Issue #9 P2）：
        UTF-8 汉字占 3 字节，GBK 汉字占 2 字节。按字符计分时，UTF-8 两个汉字（6 字节）
        若碰巧被 GBK 误解码为 3 个冷门汉字（6 字节），GBK 会因字符数多而虚高反超。
        因此引入 enc 差异化权重（UTF-8 CJK 5分，GBK CJK 3分），补齐字节密度差异。
        """
        score = 0
        cjk_bonus = 5 if enc == "utf-8" else 3
        for ch in text:
            o = ord(ch)
            if o < 0x80:
                score += 2                      # ASCII 正常
            elif 0x4E00 <= o <= 0x9FFF:         # CJK 统一汉字
                score += cjk_bonus
            elif 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
                score += cjk_bonus              # 中日韩标点 / 全角
            elif o in (0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2014, 0x00B7):
                score += cjk_bonus              # 常用中文标点
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
            key=lambda it: (
                self._text_score(it[1], it[0]) + (5 if (self._enc and it[0] == self._enc) else 0),
                it[0] == self._enc,
                it[0] == "utf-8",
            ),
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
            key=lambda it: (
                self._text_score(it[1], it[0]) + (5 if (self._enc and it[0] == self._enc) else 0),
                it[0] == self._enc,
                it[0] == "utf-8",
            ),
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
