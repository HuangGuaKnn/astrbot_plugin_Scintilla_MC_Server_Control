"""异步 Minecraft RCON 客户端（纯标准库实现，零第三方依赖）。

RCON 协议（Source RCON Protocol）帧结构：
    [int32 length][int32 request_id][int32 type][payload bytes][\\x00\\x00]
    length = 4(request_id) + 4(type) + len(payload) + 2
    type=3 登录认证；type=2 命令；type=0 响应数据；type=2 认证成功响应

并发与协议约束（v0.22.2 修复）：
    * **整条事务串行化**：连接认证 → 发送 → 读完响应，全部在一把
      ``asyncio.Lock`` 内完成。多个入口（聊天工具 / WebUI / 工作流）即使
      同时打这条连接，也只能排队，不会互相读走对方的包。
    * **多包响应**：一条命令的输出可能被服务端拆成多个包。收完第一个匹配
      请求 id 的包后会短等一次继续收尾，不再「只补读一包」。
    * **严格校验请求 id**：收到不属于本请求的包说明连接上还残留着上一次的
      数据，此时**废弃连接**（而不是把残留内容混进本命令的输出）。
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

#: 收完一个匹配包后，再短等这么久探测是否还有后续分片。
IDLE_PROBE = 0.15

#: 单条命令最多接收的包数（防御异常服务端无限刷包）。
MAX_PACKETS = 256


class RconError(Exception):
    """RCON 通信错误。"""


class RconTimeoutError(RconError):
    """超时未拿到响应：命令可能已执行、也可能没有，结果未知。

    调用方**不得**把它当作执行成功，也不得自动重发非幂等命令。
    """

    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial or ""


class AsyncRcon:
    """异步 RCON 客户端（同一实例可被并发调用，内部自动排队）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25575,
        password: str = "",
        timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
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
        if length < 10 or length > 1024 * 1024:
            raise RconError(f"RCON 数据包长度异常：{length}")
        body = await asyncio.wait_for(
            self._reader.readexactly(length), timeout=self.timeout
        )
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
            RconTimeoutError —— 超时且未收到任何响应，**结果未知**。
            RconError        —— 发送 / 读取失败、响应错乱等。
        """
        async with self._lock:
            return await self._command_locked(command, timeout)

    async def _command_locked(self, command: str, timeout: float | None = None) -> str:
        if not self._connected or self._writer is None or self._reader is None:
            await self.connect()

        req_id = self._next_id()
        try:
            await self._send_packet(req_id, PACKET_COMMAND, command)
        except (OSError, asyncio.TimeoutError) as e:
            self._close_socket()
            raise RconError(f"RCON 发送失败: {e}") from e

        budget = float(timeout or self.timeout)
        deadline = time.monotonic() + budget
        chunks: list[str] = []
        received = False

        while len(chunks) < MAX_PACKETS:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            # 一个包都还没收到 → 用剩余总预算等；已收到匹配包 → 只短等一次探测分片
            wait = min(IDLE_PROBE, remain) if received else remain
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

            if resp_id != req_id:
                # 连接上残留着别的请求的数据：绝不串台，废弃连接
                self._close_socket()
                raise RconError(
                    f"RCON 响应错乱：收到不匹配的请求 id（期望 {req_id}，实收 {resp_id}），已重置连接"
                )
            if resp_type != PACKET_RESPONSE:
                continue
            received = True
            chunks.append(payload)

        if received:
            return "".join(chunks).strip("\x00").strip()

        # 一个包都没收到：命令是否已执行无法判断，别复用这条连接
        self._close_socket()
        raise RconTimeoutError(
            f"RCON 服务器 {budget:g} 秒内没有响应，执行结果未知"
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
