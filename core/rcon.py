"""异步 Minecraft RCON 客户端（纯标准库实现，零第三方依赖）。

RCON 协议（Source RCON Protocol）帧结构：
    [int32 length][int32 request_id][int32 type][payload bytes][\\x00\\x00]
    length = 4(request_id) + 4(type) + len(payload) + 2
    type=3 登录认证；type=2 命令；type=0 响应数据；type=2 认证成功响应

并发与协议约束（v0.22.2 修复 / v0.22.3 加固）：
    * **整条事务串行化**：连接认证 → 发送 → 读完响应，全部在一把
      ``asyncio.Lock`` 内完成。多个入口（聊天工具 / WebUI / 工作流）即使
      同时打这条连接，也只能排队，不会互相读走对方的包。
    * **可靠的响应结束边界（结束哨兵）**：RCON 协议本身**没有**「响应到此结束」
      的标记，所以「静默多久算读完」永远只是猜测 —— 首包后 220ms 才到次包、
      或次包只先到了半截，都会被猜错。v0.22.3 默认改用 **结束哨兵（sentinel）**：
      发完真实命令后紧跟一条无害探测命令；服务端按序处理命令、TCP 按序送达，
      于是「收到探测命令的第一帧」等价于「上一条命令的响应已全部到达」，
      这是协议层可证的边界，与网络延迟、分包间隔、半包停顿都无关。
      若某些服务端完全不回应探测命令，可把 ``rcon_end_mode`` 设为 ``idle``
      退回静默窗口（**降级策略，只覆盖常见情况**）；插件在连续多次拿不到哨兵时
      也会自动降级并打日志，绝不静默假装读完了。
    * **绝不静默返回半条响应**：拿到了部分输出却没等到结束边界时，抛
      :class:`RconPartialResponseError`（继承 :class:`RconTimeoutError`，
      语义同样是**结果未知**），并废弃连接。
    * **严格校验请求 id**：收到不属于本请求的包说明连接上还残留着上一次的
      数据，此时**废弃连接**（而不是把残留内容混进本命令的输出）。
    * **协议异常一律废弃连接**：包长度非法、包尾缺少 ``\\x00\\x00``、包数超过上限、
      读取中断、半包被取消…… 任何「流已错位」的情形都关掉连接，绝不留
      ``connected == True`` 而底层读取状态已经错位的连接。
    * **超时语义**：超时且没有收到任何响应时抛 :class:`RconTimeoutError`，
      调用方必须报告「结果未知」，不得宣称执行成功、也不得自动重发
      （发物品等非幂等操作重发会造成重复副作用）。同时连接被废弃，避免
      复用状态未知的连接。
    * **合法空响应**：确有收到响应包但内容为空 → 正常返回空字符串，与
      「没收到响应」是两件事。
"""
from __future__ import annotations

import asyncio
import struct
import time

PACKET_LOGIN = 3
PACKET_COMMAND = 2
PACKET_RESPONSE = 0
PACKET_AUTH_RESPONSE = 2  # 登录成功响应类型

#: 结束边界策略：``sentinel``（默认，结束哨兵，可靠）/ ``idle``（静默窗口，降级）。
DEFAULT_END_MODE = "sentinel"

#: 哨兵模式用的探测命令（空命令：服务端照样回一个响应包，且它本身没有任何副作用）。
DEFAULT_PROBE_COMMAND = ""

#: ``idle`` 降级模式下的静默窗口（秒）：收到包后再等这么久没有新包才算读完。
#: 只是降级策略 —— 网络延迟 / 长分包间隔超出该窗口时仍会误判，
#: 因此默认不用它，只在服务端不支持哨兵时才退回。
IDLE_PROBE = 0.5

#: 连续多少次「拿到输出却没等到哨兵」后自动降级为 idle（0 = 不自动降级）。
PROBE_AUTO_DEGRADE_AFTER = 3

#: 单条命令最多接收的包数（防御异常服务端无限刷包）。
MAX_PACKETS = 256

#: 单个 RCON 包体的长度上限（防畸形长度字段）。
MAX_PACKET_BYTES = 1024 * 1024



class RconError(Exception):
    """RCON 通信错误。"""


class RconTimeoutError(RconError):
    """超时未拿到响应：命令可能已执行、也可能没有，结果未知。

    调用方**不得**把它当作执行成功，也不得自动重发非幂等命令。
    """

    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial or ""


class RconPartialResponseError(RconTimeoutError):
    """只拿到部分响应、且没等到可靠的结束边界：响应可能被截断，**结果未知**。

    刻意继承 :class:`RconTimeoutError` —— 语义一致（命令可能已经执行过了），
    于是所有「结果未知、不得自动重发」的处理路径都自动生效，不必逐处补分支。
    已收到的部分输出放在 ``partial``。
    """


class AsyncRcon:
    """异步 RCON 客户端（同一实例可被并发调用，内部自动排队）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25575,
        password: str = "",
        timeout: float = 5.0,
        end_mode: str = DEFAULT_END_MODE,
        probe_command: str = DEFAULT_PROBE_COMMAND,
        idle_probe: float = IDLE_PROBE,
        logger=None,
    ):
        #: 插件日志器（由 main.py 注入 AstrBot 的 logger）。本模块刻意**不依赖**
        #: astrbot 包，保持纯标准库、可独立单测；不注入时只是不打降级告警。
        self.logger = logger
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        #: 结束边界策略：只有显式写 ``idle`` 才降级，其他任何值（含笔误）都用可靠的哨兵。
        self.end_mode = "idle" if str(end_mode or "").strip().lower() == "idle" else "sentinel"
        self.probe_command = str(probe_command or "")
        try:
            self.idle_probe = max(0.05, float(idle_probe))
        except (TypeError, ValueError):
            self.idle_probe = IDLE_PROBE
        self._probe_misses = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._req_id = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self) -> "AsyncRcon":
        """建立连接并完成 RCON 登录认证。

        注意：``command()`` 会在自己的锁内调用本方法；如果要在外部直接发起
        连接，请一次性完成、不要与 ``command()`` 并发调用。
        """
        if self._connected and self._writer is not None:
            return self

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise RconError(f"无法连接 RCON 服务器 {self.host}:{self.port}: {e}") from e

        req_id = self._next_id()
        try:
            await self._send_packet(req_id, PACKET_LOGIN, self.password)
            resp_id, resp_type, _ = await self._read_packet()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            self._close_socket()
            raise RconError(f"RCON 登录失败: {e}") from e

        if resp_id == -1:
            self._close_socket()
            raise RconError("RCON 认证失败：密码错误")
        # 认证成功：服务器返回与请求一致的 request_id（类型通常为 AUTH_RESPONSE）
        if resp_id != req_id:
            self._close_socket()
            raise RconError(
                f"RCON 登录响应异常 (id={resp_id}, type={resp_type})"
            )
        self._connected = True
        return self

    async def _send_packet(self, req_id: int, ptype: int, payload: str) -> None:
        data = payload.encode("utf-8")
        packet = (
            struct.pack("<iii", 10 + len(data), req_id, ptype)
            + data
            + b"\x00\x00"
        )
        assert self._writer is not None
        self._writer.write(packet)
        await self._writer.drain()

    async def _read_packet(self) -> tuple[int, int, str]:
        assert self._reader is not None
        header = await asyncio.wait_for(
            self._reader.readexactly(4), timeout=self.timeout
        )
        (length,) = struct.unpack("<i", header)
        if length < 10 or length > MAX_PACKET_BYTES:
            # 长度字段非法 = 这条流已经错位：不能只报错了事，必须废弃连接
            self._close_socket()
            raise RconError(f"RCON 数据包长度异常：{length}，连接已废弃")
        body = await asyncio.wait_for(
            self._reader.readexactly(length), timeout=self.timeout
        )
        if not body.endswith(b"\x00\x00"):
            # 不收尾的包 = 半包 / 协议错位，同样废弃连接
            self._close_socket()
            raise RconError("RCON 数据包结尾缺少 \\x00\\x00，连接已废弃")
        req_id, ptype = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return req_id, ptype, payload

    # ------------------------------------------------------------ 命令事务

    async def command(self, command: str, timeout: float | None = None) -> str:
        """执行一条服务器命令，返回输出文本。

        串行化：整条事务（必要时建连认证 → 发送 → 读完响应）在同一把锁内完成，
        并发调用只会排队，不会互相读走对方的响应包。

        返回：
            str —— 服务器给出的输出（允许为空字符串，那是「合法的空响应」）。

        抛出：
            RconTimeoutError        —— 一个响应包都没收到（结果未知）。
            RconPartialResponseError—— 只收到部分响应、没等到可靠结束边界（结果未知）。
            RconError               —— 发送 / 读取失败、响应错乱等。
        """
        async with self._lock:
            return await self._command_locked(command, timeout)

    async def _command_locked(self, command: str, timeout: float | None = None) -> str:
        if not self._connected or self._writer is None or self._reader is None:
            await self.connect()

        req_id = self._next_id()
        probe_id: int | None = None
        try:
            await self._send_packet(req_id, PACKET_COMMAND, command)
            if self.end_mode == "sentinel":
                # 结束哨兵：紧跟一条无害探测命令。服务端按序处理命令、TCP 按序送达，
                # 于是「收到哨兵的第一帧」= 「本次命令的响应已全部到达」。
                probe_id = self._next_id()
                await self._send_packet(probe_id, PACKET_COMMAND, self.probe_command)
        except (OSError, asyncio.TimeoutError) as e:
            self._close_socket()
            raise RconError(f"RCON 发送失败: {e}") from e

        budget = float(timeout or self.timeout)
        deadline = time.monotonic() + budget
        chunks: list[str] = []
        probe_seen = False

        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if probe_id is not None:
                # 哨兵模式：唯一的可靠结束边界就是哨兵本身，因此一直等到总预算耗尽
                wait = remain
            else:
                # 降级模式：一个包都没来时用剩余总预算等，已经收到包就短探一次分片
                wait = min(self.idle_probe, remain) if chunks else remain
            try:
                resp_id, resp_type, payload = await asyncio.wait_for(
                    self._read_packet(), timeout=max(0.05, wait)
                )
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                # 半包被取消：连接状态不确定，废弃
                self._close_socket()
                raise
            except (asyncio.IncompleteReadError, OSError) as e:
                self._close_socket()
                raise RconError(f"RCON 读取失败: {e}") from e

            if probe_id is not None and resp_id == probe_id:
                probe_seen = True
                break
            if resp_id != req_id:
                # 连接上残留着别的请求的数据：绝不串台，废弃连接
                self._close_socket()
                raise RconError(
                    f"RCON 响应错乱：收到不匹配的请求 id（期望 {req_id}，实收 {resp_id}），已重置连接"
                )
            if resp_type != PACKET_RESPONSE:
                continue
            chunks.append(payload)
            if len(chunks) > MAX_PACKETS:
                self._close_socket()
                raise RconError(
                    f"RCON 响应包数超过上限 {MAX_PACKETS}，无法确认响应完整，连接已废弃"
                )

        if probe_seen:
            # 拿到可靠结束边界：响应一定收全了（哪怕一个包都没有 = 合法空响应）
            self._probe_misses = 0
            return "".join(chunks).strip("\x00").strip()

        if probe_id is None and chunks:
            # 降级模式：静默窗口内没有新包，按「读完了」处理（文档已声明其不可靠）
            return "".join(chunks).strip("\x00").strip()

        if chunks:
            # 哨兵模式：拿到部分输出却没等到结束边界 → 绝不静默当成功返回
            partial = "".join(chunks)
            self._close_socket()
            self._count_probe_miss()
            raise RconPartialResponseError(
                f"RCON 响应未收全（{budget:g} 秒内没有等到结束哨兵，"
                f"只收到 {len(chunks)} 个包），结果未知",
                partial=partial,
            )

        # 一个包都没收到：命令是否已执行无法判断，别复用这条连接
        self._close_socket()
        self._count_probe_miss()
        raise RconTimeoutError(
            f"RCON 服务器 {budget:g} 秒内没有响应，执行结果未知"
        )

    def _count_probe_miss(self) -> None:
        """哨兵连续拿不到 → 自动降级为 idle 静默窗口（并留下明确日志）。

        「拿不到哨兵」通常意味着服务端不回应探测命令（个别非原版实现），
        这种情况下宁可退回旧版行为也不要让每条命令都失败；降级会打日志说明
        「多包响应的结束判断不再可靠」，便于排查。
        """
        if self.end_mode != "sentinel" or PROBE_AUTO_DEGRADE_AFTER <= 0:
            return
        self._probe_misses += 1
        if self._probe_misses >= PROBE_AUTO_DEGRADE_AFTER:
            self.end_mode = "idle"
            if self.logger is not None:
                self.logger.warning(
                    "RCON 连续 %d 次没有收到结束哨兵响应，已自动降级为 idle 静默窗口模式"
                    "（%.2fs）：多包响应的结束判断不再绝对可靠。若确认本服务端不回应探测命令，"
                    "可把 rcon_end_mode 设为 idle 消除本告警。",
                    self._probe_misses,
                    self.idle_probe,
                )

    def _close_socket(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._connected = False

    async def close(self) -> None:
        async with self._lock:
            self._close_socket()
