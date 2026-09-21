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
      于是「命令响应之后又收到探测命令的第一帧」等价于「上一条命令的响应已全部到达」，
      这是协议层可证的边界，与网络延迟、分包间隔、半包停顿都无关。
      **顺序是硬要求**：只有先收到本次命令的响应包（payload 可以为空），哨兵才算边界；
      若哨兵抢在命令响应之前到达，说明服务端不保证按序回应 —— 此时报「结果未知」并
      废弃连接，绝不把它当成「合法空响应」（否则迟到包会留给下一次调用，静默串台）。
      若某些服务端完全不回应探测命令，可把 ``rcon_end_mode`` 设为 ``idle``
      退回静默窗口（**降级策略，只覆盖常见情况**）；插件在连续多次拿不到哨兵时
      也会自动降级并打日志，绝不静默假装读完了。
    * **绝不静默返回半条响应**：拿到了部分输出却没等到结束边界时，抛
      :class:`RconPartialResponseError`（继承 :class:`RconTimeoutError`，
      语义同样是**结果未知**），并废弃连接。
    * **降级模式（idle 静默窗口）绝不让不确定性传染**：``idle`` 是给「完全不回应探测
      命令」的服务端准备的下策，判定「静默窗口内没新包 = 读完了」本身就可能截断
      （服务端分包间隔大于窗口时）。因此 v0.22.5 要求：**静默窗口一到就把这条连接
      关掉**，并把 ``last_boundary_confirmed`` 置为 False —— 这一次的结果可能不完整，
      但**绝不允许**残留包留给下一次调用（否则第一条命令静默假成功、第二条命令才报
      「响应 id 不匹配」，锅还挂在第二条头上）。想彻底消除截断风险就别用 idle：
      ``sentinel`` 是协议层可证的边界。
    * **边界状态必须反映「最近一次」，且从「未确认」起算**：``last_boundary_confirmed``
      三态 —— ``True`` 只在哨兵按序到齐时写入；每条命令**开始时先置 ``False``**，
      于是超时、零响应、协议异常、idle 窗口到期等「没走到确认点」的路径都不会沿用
      上一次的 ``True``；实例建好但**尚未执行任何命令**时为 ``None``（暂无结果），
      页面据此显示「未执行过命令」，而不是冒充「边界可靠」。
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
        #: v0.22.5（复审 P2 整改）：最近一次命令的结束边界是否**已被确认**。三态：
        #: ``True`` = 哨兵按序到齐，内容完整可靠；``False`` = 本次没能确认可靠边界
        #: （idle 静默窗口到期 / 超时 / 协议异常），内容可能不完整；``None`` = 本实例
        #: 还没执行过任何命令，**没有最近一次结果可谈**（不得默认成 True 冒充可靠）。
        self.last_boundary_confirmed: bool | None = None
        #: v0.22.6：最近一次命令**是否收到过属于本命令的响应**（「发出去了」的证据）。
        #: 与 ``last_boundary_confirmed`` 是**两个独立维度**，不能互相代替：
        #: idle 降级模式下「收到响应但边界未确认」是合法组合 —— 此时消息类命令
        #: 应报「已发送·边界未确认」，既不是「失败」、也不是「结果未知」。
        #: 三态同构：``None`` = 本实例还没执行过任何命令。
        self.last_response_received: bool | None = None
        self._idle_unconfirmed_count = 0
        #: v0.22.5：实例是否已被插件「退役」（reset 时摘下来的旧实例）。退役后仍允许
        #: 跑完手头那一条命令（不粗暴掐断在途调用），但用完立即关闭，绝不回到连接池。
        self._retired = False
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
        if self._retired:
            # v0.22.5：退役实例绝不重新建连，否则 reset 之后旧实例又悄悄占据一条新连接。
            raise RconError("该 RCON 实例已退役（连接已重建），请改用插件当前实例")
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
            try:
                return await self._command_locked(command, timeout)
            finally:
                # v0.22.5：本实例已被 reset 退役 → 用完立即释放，不留下无人认领的连接。
                if self._retired:
                    self._close_socket()

    def retire(self) -> None:
        """把实例标记为「已退役」（v0.22.5）：reset 换新实例时对旧实例调用。

        不在这里直接关连接：旧实例可能正握着在途命令，粗暴切断会把它变成「结果未知」。
        退役后由它自己在那条命令结束时关闭（重连时也会被 ``_retired`` 拦住）。
        """
        self._retired = True

    @property
    def in_flight(self) -> bool:
        """是否有命令正握着锁执行（v0.22.5）。

        ``retire()`` 之后连接由「谁还握着这条命令」决定何时关闭，调用方必须能问出这件事：
        没有在途命令就不能干等，否则退役实例的 socket 无人回收（静默泄漏）。
        """
        return self._lock.locked()

    async def _command_locked(self, command: str, timeout: float | None = None) -> str:
        if not self._connected or self._writer is None or self._reader is None:
            await self.connect()

        # v0.22.5（复审 P2 整改）：本条命令的边界状态**从「未确认」起算**。
        # 否则「上一次 sentinel 成功 → True」会被超时 / 协议异常等路径原样沿用，
        # 让 UI 显示「边界可靠」而实际最近一次执行结果未知（与事实相反）。
        self.last_boundary_confirmed = False
        # v0.22.6：同理，「是否收到过本命令响应」也必须从「没有」起算，
        # 否则上一次的 True 会被超时路径沿用，让空响应冒充「已送达」。
        self.last_response_received = False

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
        command_seen = False

        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if probe_id is not None:
                # 哨兵模式：唯一的可靠结束边界是「命令响应 → 哨兵响应」都按序到达，
                # 因此一直等到总预算耗尽（不做静默猜测）
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

            if resp_id == req_id:
                # 服务端已经处理到本次命令 —— 这是「顺序正确」的证据。
                # 注意：非 PACKET_RESPONSE 类型也照样算证据（服务端确实回了这一条）。
                command_seen = True
                # v0.22.6：这是「命令确实送达并被服务端处理过」的唯一证据位。
                # 与 last_boundary_confirmed 独立 —— idle 降级模式下本字段为 True
                # 而边界为 False，此时消息类命令应报「已发送·边界未确认」。
                self.last_response_received = True
                if resp_type == PACKET_RESPONSE:
                    chunks.append(payload)
                    if len(chunks) > MAX_PACKETS:
                        self._close_socket()
                        raise RconError(
                            f"RCON 响应包数超过上限 {MAX_PACKETS}，无法确认响应完整，连接已废弃"
                        )
                continue

            if probe_id is not None and resp_id == probe_id:
                if not command_seen:
                    # v0.22.4（核验 P1）：哨兵先于命令响应到达 = 服务端没有按序回应。
                    # 此时**既不能**把它当「合法空响应」（命令可能已执行也可能没执行），
                    # **也不能**把迟到的命令响应留在连接里让下一次调用去踩。
                    self._close_socket()
                    self._count_probe_miss("结束哨兵先于命令响应到达（响应乱序）")
                    raise RconPartialResponseError(
                        "RCON 响应顺序异常：结束哨兵先于命令响应到达，"
                        "无法确认命令是否已执行，结果未知（已废弃连接）"
                    )
                probe_seen = True
                break

            # 其它请求 id：连接上残留着别的请求的数据，绝不串台，废弃连接
            self._close_socket()
            raise RconError(
                f"RCON 响应错乱：收到不匹配的请求 id（期望 {req_id}，实收 {resp_id}），已重置连接"
            )

        if probe_seen:
            # 「命令响应（可为空 payload）→ 哨兵」都到齐了，这才是可靠结束边界
            self._probe_misses = 0
            self.last_boundary_confirmed = True
            return "".join(chunks).strip("\x00").strip()

        if probe_id is None and chunks:
            # 降级模式（idle 静默窗口）：没有可确认的结束边界，只是「窗口内没有新包了」。
            # v0.22.5（核验 P1）：静默窗口到期**必须关掉这条连接**，绝不能把半截输出当
            # 正常结果之后还继续复用 —— 否则残留包会留给下一次调用，表现为「第一条命令
            # 静默假成功、第二条命令报响应 id 不匹配」，错误还挂在第二条命令头上。
            # 这里仍然返回已收到的文本（保持 idle 的可用性，否则该模式每条命令都报废），
            # 但结果标记为「边界未确认」，并重建连接让下一条命令从干净的状态开始。
            # （一个包都没收到的情形不在这里：它和 sentinel 一样属于「结果未知」，走末尾
            #   的 RconTimeoutError，绝不能返回空串冒充「合法的空响应」。）
            self._close_socket()
            self.last_boundary_confirmed = False
            self._idle_unconfirmed_count += 1
            if self.logger is not None:
                self.logger.warning(
                    "RCON idle 降级模式：静默窗口 %.2fs 内没等到新包，无法确认响应边界，"
                    "本次输出（%d 个包）可能被截断且已废弃连接（下一条命令将重新建连）。"
                    "如需绝对可靠请把 rcon_end_mode 设回 sentinel。",
                    self.idle_probe,
                    len(chunks),
                )
            return "".join(chunks).strip("\x00").strip()

        if chunks:
            # 哨兵模式：拿到部分输出却没等到结束边界 → 绝不静默当成功返回
            partial = "".join(chunks)
            self._close_socket()
            self._count_probe_miss(f"只收到 {len(chunks)} 个响应包、未等到结束哨兵")
            raise RconPartialResponseError(
                f"RCON 响应未收全（{budget:g} 秒内没有等到结束哨兵，"
                f"只收到 {len(chunks)} 个包），结果未知",
                partial=partial,
            )

        # 一个包都没收到：命令是否已执行无法判断，别复用这条连接。
        # （这里**不算**哨兵失败：服务器整体失联与「哨兵机制是否被支持」无关，
        #   不该因为它就把完整性保证降级掉。）
        self._close_socket()
        raise RconTimeoutError(
            f"RCON 服务器 {budget:g} 秒内没有响应，执行结果未知"
        )

    def _count_probe_miss(self, reason: str) -> None:
        """连续多次无法确认响应边界 → 自动降级为 idle 静默窗口（并留下明确日志）。

        只统计「有证据表明服务端在正常收发、只是边界确认不了」的情形：
          · 收到了命令响应，却始终等不到结束哨兵（服务端不回应探测命令）；
          · 哨兵先于命令响应到达（服务端不保证按序回应）。
        服务器整体失联（一个包都没收到）**不计入**：那跟哨兵机制无关。

        降级的代价要说清楚：静默窗口只是降级策略，服务端分包间隔大于该窗口时
        长响应可能被截断。整改完成可在 WebUI 一键重建连接回到 sentinel。
        """
        if self.end_mode != "sentinel" or PROBE_AUTO_DEGRADE_AFTER <= 0:
            return
        self._probe_misses += 1
        if self._probe_misses >= PROBE_AUTO_DEGRADE_AFTER:
            self.end_mode = "idle"
            if self.logger is not None:
                self.logger.warning(
                    "RCON 连续 %d 次无法确认响应结束边界（最近一次：%s），"
                    "已自动降级为 idle 静默窗口模式（%.2fs）：多包响应的结束判断不再绝对可靠，"
                    "长响应可能被截断。排查完可回 WebUI 点「重建连接」恢复 sentinel。",
                    self._probe_misses,
                    reason,
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
