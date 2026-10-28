# -*- coding: utf-8 -*-
"""v0.24.9 回归：RCON 读失败时的**诊断现场**（F9 悬案的可观测性）。

背景
====
真机偶发 ``RCON 读取失败: 0 bytes read on a total of 4 expected bytes``：
三次「保存设置 → 切换服务端」里出现两次，第三次没复现 → 间歇性。
「服务端掐掉空闲连接」已被专项实验排除（空转 5 分钟不掉线）；试过的
「``reset_rcon()`` 里主动建连」会破坏「连接数不膨胀」的既有不变式，已回退。
**根因未证伪前不动行为**，只加观测：读失败那一刻把 reader/writer 的本地状态、
连接年龄、连接代数一次打进日志。本用例就是证明**这条观测路径真的会输出**，
而不是写了个永不触发的死代码。

做法
====
起一个「握手后立刻挂断」的假 RCON 服务端 → 客户端在 ``_read_packet`` 上撞
``IncompleteReadError`` → 断言：
  1) 抛出的是 RconError（行为没变，仍然是一次失败）；
  2) 日志里出现 ``[RCON 读失败诊断]``；
  3) 诊断内容含 ``is_closing`` / ``at_eof`` / ``连接年龄`` 三项关键证据；
  4) logger 为 None 时**不抛**（单元测试常用裸对象装配，诊断不能变成新崩点）。

运行：<AstrBot python> tests\\test_v0249_rcon_diag.py
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import AsyncRcon, RconError  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


async def _hangup_server() -> tuple[int, asyncio.AbstractServer]:
    """握手（读掉客户端发来的认证包）之后立刻挂断 —— 精确制造「读包时 0 字节」。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """**认证成功**，但在收到命令包之后不回包、直接挂断 —— 精确制造「读包时 0 字节」。

        第一版这里连认证都不回就挂断，结果失败发生在**认证阶段**，压根走不到
        `_read_packet` 的诊断分支（用例自己先红了，反而说明诊断没白写）。
        """
        try:
            # 1) 读认证包：长度(4) + body(id 4 + type 4 + password + \0\0)
            header = await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
            (length,) = struct.unpack("<i", header)
            body = await asyncio.wait_for(reader.readexactly(length), timeout=2.0)
            (rid,) = struct.unpack("<i", body[:4])
            # 2) 回认证成功（PACKET_AUTH_RESPONSE = 2）
            resp = struct.pack("<ii", rid, 2) + b"\x00\x00"
            writer.write(struct.pack("<i", len(resp)) + resp)
            await writer.drain()
            # 3) 把随后的命令包读掉，然后**不回**、直接挂断
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return port, server


async def main_async() -> int:
    logger = logging.getLogger("rcon-diag-test")
    logger.setLevel(logging.DEBUG)
    col = _Collect()
    logger.addHandler(col)

    port, server = await _hangup_server()
    try:
        rcon = AsyncRcon(host="127.0.0.1", port=port, password="pw", timeout=2.0, logger=logger)
        print("[1] 对端握手后挂断 → 读包失败必须有诊断日志")
        raised = None
        try:
            await rcon.command("list")
        except RconError as e:
            raised = e
        except Exception as e:  # noqa: BLE001
            raised = e
        check("抛出的仍是 RconError（行为没变）", isinstance(raised, RconError),
              f"{type(raised).__name__}: {raised}")
        joined = "\n".join(col.lines)
        check("★日志里出现了 [RCON 读失败诊断]", "[RCON 读失败诊断]" in joined, joined[-300:] or "(无日志)")
        check("诊断含 is_closing（本地 writer 状态）", "is_closing=" in joined, joined[-300:])
        check("诊断含 at_eof（reader 是否已见 EOF）", "at_eof=" in joined, joined[-300:])
        check("诊断含连接年龄", "连接年龄=" in joined, joined[-300:])
        check("诊断含原因（异常类型）", "原因=" in joined, joined[-300:])

        print("\n[2] 诊断内容实样")
        for ln in col.lines:
            if "[RCON 读失败诊断]" in ln:
                print("      " + ln[:220])
                break
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    print("\n[3] logger 为 None 时不抛（裸对象装配）")
    port2, server2 = await _hangup_server()
    try:
        quiet = AsyncRcon(host="127.0.0.1", port=port2, password="pw", timeout=2.0, logger=None)
        ok = False
        try:
            await quiet.command("list")
        except RconError:
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            print("      非预期异常:", type(e).__name__, e)
        check("★仍抛 RconError（不是 AttributeError 之类）", ok)
    finally:
        server2.close()
        try:
            await server2.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （读失败一定留现场；诊断自己不会变成新崩点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
