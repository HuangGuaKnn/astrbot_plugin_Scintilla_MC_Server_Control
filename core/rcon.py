"""异步 Minecraft RCON 客户端（纯标准库实现，零第三方依赖）。

RCON 协议（Source RCON Protocol）帧结构：
    [int32 length][int32 request_id][int32 type][payload bytes][\\x00\\x00]
    length = 4(request_id) + 4(type) + len(payload) + 2
    type=3 登录认证；type=2 命令；type=0 响应数据；type=2 认证成功响应
"""
from __future__ import annotations

import asyncio
import struct
import time

PACKET_LOGIN = 3
PACKET_COMMAND = 2
PACKET_RESPONSE = 0
PACKET_AUTH_RESPONSE = 2  # 登录成功响应类型


class RconError(Exception):
    """RCON 通信错误。"""


class AsyncRcon:
    """异步 RCON 客户端。"""

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
        self._req_id = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self) -> "AsyncRcon":
        """建立连接并完成 RCON 登录认证。"""
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
        body = await asyncio.wait_for(
            self._reader.readexactly(length), timeout=self.timeout
        )
        req_id, ptype = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return req_id, ptype, payload

    async def command(self, command: str, timeout: float | None = None) -> str:
        """执行一条服务器命令，返回输出文本。失败时抛 RconError。"""
        if not self._connected or self._writer is None:
            await self.connect()

        req_id = self._next_id()
        try:
            await self._send_packet(req_id, PACKET_COMMAND, command)
        except (OSError, asyncio.TimeoutError) as e:
            self._connected = False
            raise RconError(f"RCON 发送失败: {e}") from e

        chunks: list[str] = []
        deadline = time.monotonic() + (timeout or self.timeout)
        # 长输出可能被拆成多个包，循环读取直到拿到匹配请求 id 的包
        while time.monotonic() < deadline:
            try:
                resp_id, resp_type, payload = await asyncio.wait_for(
                    self._read_packet(),
                    timeout=max(0.1, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                break
            except (asyncio.IncompleteReadError, OSError) as e:
                self._connected = False
                raise RconError(f"RCON 读取失败: {e}") from e
            if resp_type == PACKET_RESPONSE:
                chunks.append(payload)
                if resp_id == req_id:
                    # 可能还有后续分包，短等一次收尾
                    try:
                        r2, t2, p2 = await asyncio.wait_for(
                            self._read_packet(), timeout=0.15
                        )
                        if t2 == PACKET_RESPONSE:
                            chunks.append(p2)
                    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                        pass
                    break
        return "".join(chunks).strip("\x00").strip()

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
        self._close_socket()
