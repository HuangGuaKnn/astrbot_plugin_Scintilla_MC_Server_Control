# -*- coding: utf-8 -*-
"""v0.24.8 回归（2026-10-21 真机）：空闲后被服务端悄悄断开的 RCON 连接。

真机现象
========
1.7.10-forge 上跑完一轮检查、空了半小时，**第一次** `mc_list_players` 返回：

    RCON 读取失败: 0 bytes read on a total of 4 expected bytes

紧接着第二次就正常了。根因：服务端早已关掉这条空闲连接（FIN 已到），
而客户端仍标记「已连接」—— 于是把命令发进了一条死管道，读到 0 字节。

修法（`core/rcon.py`）
=====================
发送**之前**先看两个本地标志：`StreamWriter.is_closing()` 与 `StreamReader.at_eof()`
（都由 asyncio 在连接断开时置位，读内存、不发包）。任何一个为真 → 关掉旧连接、重建。
**不重发任何命令** —— 这条判据只决定「要不要先换一根管道」，与幂等性无关。

运行：<AstrBot python> tests\\test_v0248_stale_socket.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import AsyncRcon  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


class _Reader:
    def __init__(self, eof: bool = False, boom: bool = False):
        self._eof = eof
        self._boom = boom

    def at_eof(self) -> bool:
        if self._boom:
            raise RuntimeError("读不动")
        return self._eof


class _Writer:
    def __init__(self, closing: bool = False):
        self._closing = closing

    def is_closing(self) -> bool:
        return self._closing


def _rc(connected: bool = True, eof: bool = False, closing: bool = False,
        boom: bool = False) -> AsyncRcon:
    rc = AsyncRcon(host="127.0.0.1", port=1, password="x")
    rc._connected = connected
    rc._reader = _Reader(eof=eof, boom=boom)
    rc._writer = _Writer(closing=closing)
    return rc


def part_one() -> None:
    print("---- 一、判定：什么时候必须先重建 ----")
    cases = [
        ("连接还活着（对端没关）", _rc(True, eof=False, closing=False), False),
        ("对端已 FIN（at_eof）", _rc(True, eof=True, closing=False), True),
        ("本端已在关（is_closing）", _rc(True, eof=False, closing=True), True),
        ("两个都真", _rc(True, eof=True, closing=True), True),
        ("还没连（_connected=False）", _rc(False), True),
        ("判定本身抛异常 → fail-closed", _rc(True, boom=True), True),
    ]
    for name, rc, want in cases:
        got = rc._peer_closed()
        check(f"{name} → {'需要重建' if want else '可以直接发'}", got is want, repr(got))
    rc = AsyncRcon()
    check("刚 new 出来（无 reader/writer）→ 需要重建", rc._peer_closed() is True)


def part_two() -> None:
    print("\n---- 二、死管道上读失败后连接必须被标记为断开 ----")
    rc = _rc(True, eof=False, closing=False)

    async def _boom(_timeout=None):
        raise asyncio.IncompleteReadError(partial=b"", expected=4)

    rc._reader.readexactly = _boom  # type: ignore[attr-defined]

    async def _write(packet: bytes) -> None:
        return None

    class _W2(_Writer):
        def write(self, data: bytes) -> None:  # noqa: D102
            return None

        async def drain(self) -> None:  # noqa: D102
            return None

    rc._writer = _W2()

    from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import RconError

    err = None
    try:
        asyncio.run(rc.command("list"))
    except RconError as e:  # noqa: PERF203
        err = e
    check("★死管道读失败 → 抛 RconError（phase=read，不当成空响应）",
          isinstance(err, RconError) and err.phase == RconError.PHASE_READ,
          repr(err))
    check("★并且连接被标记为断开（下一次调用会重建，不会继续往死管道里发）",
          rc.connected is False, f"connected={rc.connected}")


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 真机：空闲后服务端悄悄断开 → 发送前先认出来")
    print("=" * 78)
    part_one()
    part_two()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （隔半天第一次使唤不会再先报一次错）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
