"""RCON 协议链路 + 权限规范化回归用例（v0.22.2 修复验收）。

覆盖审查报告已复现的 4 条问题：
  P1-1 共享连接并发互相干扰 → 客户端内部 asyncio.Lock 串行化整条事务
  P1-2 长响应截断 / 遗留包串入下一条 → 多包收齐 + 严格校验请求 id
  P2-3 超时被报成「已执行」 → RconTimeoutError（结果未知），连接废弃
  P2-4 黑名单包装绕过 → 先解包 execute + 去命名空间，再做规则匹配

不需要真实 Minecraft 服务端：本机会起一个 loopback 假 RCON 服务。
直接运行：<解释器> tests/test_rcon_protocol.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

PLUGIN = PLUGIN_DIR
FAIL: list[str] = []


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rcon_mod = _load("t_rcon", "core/rcon.py")
jc = _load("t_jc", "core/java_commands.py")
AsyncRcon = rcon_mod.AsyncRcon
RconError = rcon_mod.RconError
RconTimeoutError = rcon_mod.RconTimeoutError


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  ← {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- 假 RCON 服务

def pkt(req_id: int, ptype: int, payload: str) -> bytes:
    body = payload.encode("utf-8")
    return struct.pack("<iii", 10 + len(body), req_id, ptype) + body + b"\x00\x00"


async def _read(r) -> tuple[int, int, str]:
    n = struct.unpack("<i", await r.readexactly(4))[0]
    b = await r.readexactly(n)
    i, t = struct.unpack("<ii", b[:8])
    return i, t, b[8:-2].decode("utf-8", errors="replace")


async def fake_server(r, w) -> None:
    """按请求内容返回不同「故障脚本」。"""
    try:
        while True:
            i, t, s = await _read(r)
            if t == 3:  # 登录
                w.write(pkt(i, 2, ""))
            elif s == "multi":  # 三包响应（审查复现的 ONE/TWO/THREE）
                for piece in ("ONE", "TWO", "THREE"):
                    w.write(pkt(i, 0, piece))
            elif s == "split":  # 两包拼接
                w.write(pkt(i, 0, "REPLY:"))
                w.write(pkt(i, 0, "split"))
            elif s == "empty":  # 合法空响应
                w.write(pkt(i, 0, ""))
            elif s == "silent":  # 永不回复
                pass
            elif s == "stray":  # 发一个「别人的」请求 id，模拟残留包
                w.write(pkt(i + 777, 0, "LEFTOVER"))
            else:
                await asyncio.sleep(0.01)
                w.write(pkt(i, 0, "REPLY:" + s))
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        w.close()


async def _serve():
    srv = await asyncio.start_server(fake_server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port


async def _run_async_cases() -> None:
    """所有网络用例都在同一个事件循环里跑（假服务与客户端必须同 loop）。"""
    srv, port = await _serve()

    # ---- P1-1 并发串行化：两条命令各拿各的，不串台、不炸 readexactly ----
    c = AsyncRcon(port=port, timeout=1.0)
    await c.connect()
    a, b = await asyncio.gather(c.command("a"), c.command("b"), return_exceptions=True)
    check("并发两命令：都成功返回", not any(isinstance(x, Exception) for x in (a, b)), f"{a!r} {b!r}")
    check("并发两命令：结果没有串台", a == "REPLY:a" and b == "REPLY:b", f"{a!r} {b!r}")
    check("并发调用后连接仍在线可继续用", c.connected and await c.command("c") == "REPLY:c")

    # ---- P1-2 多包收齐，不截断、不遗留 ----
    out = await c.command("multi")
    check("三包响应完整收齐（ONE+TWO+THREE）", sorted([out[:3], out[3:6], out[6:]]) == sorted(["ONE", "TWO", "THREE"]) and len(out) == 11, repr(out))
    check("多包之后下一条命令干净（无残留 THREEREPLY）", await c.command("next") == "REPLY:next")
    check("两包拼接完整", await c.command("split") == "REPLY:split")

    # ---- 响应 id 不匹配 → 判定错乱并废弃连接（不串台） ----
    try:
        await c.command("stray")
        check("响应 id 不匹配 → 抛 RconError", False, "未抛异常")
    except RconError as e:
        check("响应 id 不匹配 → 抛 RconError 且提示重置连接", "请求 id" in str(e), str(e))
    check("错乱后连接被废弃（connected=False，不复用脏连接）", c.connected is False)
    await c.close()

    # ---- P2-3 空响应 vs 无响应，语义必须分开 ----
    c2 = AsyncRcon(port=port, timeout=0.5)
    await c2.connect()
    empty = await c2.command("empty")
    check("合法空响应 → 返回空字符串且不抛错", empty == "", repr(empty))
    check("空响应后连接仍可用", c2.connected is True)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        await c2.command("silent", timeout=0.3)
        check("无响应 → 抛 RconTimeoutError", False, "未抛异常")
    except RconTimeoutError as e:
        elapsed = loop.time() - t0
        check("无响应 → 抛 RconTimeoutError（结果未知）", True)
        check("超时确实按预算返回（<1.5s）", elapsed < 1.5, f"{elapsed:.2f}s")
        check("超时异常文案点明「结果未知」", "未知" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("无响应 → 抛 RconTimeoutError", False, f"抛了别的：{e!r}")
    check("超时后连接被废弃（不复用状态不确定的连接）", c2.connected is False)
    await c2.close()

    # ---- 超时后能自动重连（下一条命令不应永久失联） ----
    c3 = AsyncRcon(port=port, timeout=0.5)
    await c3.connect()
    try:
        await c3.command("silent", timeout=0.2)
    except RconTimeoutError:
        pass
    check("超时废弃后：下一次命令自动重连成功", await c3.command("ok") == "REPLY:ok")
    await c3.close()

    srv.close()
    await srv.wait_closed()


def main() -> int:
    asyncio.run(_run_async_cases())

    # ------------------------------------------------------------ P2-4 权限规范化
    print("\n=========== P2-4：黑名单包装绕过回归 ===========")
    bypass_cases = [
        "gamemode spectator @a",
        "minecraft:gamemode spectator @a",
        "minecraft:gamemode spectator @a",  # 命名空间
        "execute run gamemode spectator @a",
        "execute as @a run gamemode spectator @s",
        "execute as @a at @s run minecraft:gamemode spectator @s",  # 多层包装 + 命名空间
        "execute run execute run stop",
        "minecraft:stop",
        "execute run ban Steve",
        "/execute run op Steve",
        "  /minecraft:kick  Steve  ",
    ]
    for cmd in bypass_cases:
        r = jc.check_command(cmd, policy="blacklist", is_admin=False)
        check(f"黑名单拦住：{cmd!r}", r is not None, "放行了！")

    # 正常命令不能误伤
    pass_cases = [
        "gamemode creative",
        "give Steve diamond 1",
        "say hello",
        "minecraft:give Steve diamond 1",
        "execute as @a run say hi",
        "list",
    ]
    for cmd in pass_cases:
        r = jc.check_command(cmd, policy="blacklist", is_admin=False)
        check(f"正常命令仍放行：{cmd!r}", r is None, str(r))

    # 判定函数与等级解析口径一致
    check("is_danger_command 对三种写法一致",
          jc.is_danger_command("gamemode spectator @a")
          and jc.is_danger_command("minecraft:gamemode spectator @a")
          and jc.is_danger_command("execute run gamemode spectator @a"))
    check("effective_level 命名空间 + 包装解包正确",
          jc.effective_level("execute as @a run give @s diamond")[:2] == ("give", 2)
          and jc.effective_level("execute run minecraft:ban @s")[:2] == ("ban", 3))
    check("管理员不受策略限制", jc.check_command("execute run stop", policy="blacklist", is_admin=True) is None)
    check("白名单仍整体拦非管理员", jc.check_command("give Steve diamond 1", policy="whitelist", is_admin=False) is not None)

    print("\n==========================================")
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过 ✅  （RCON 串行化 / 多包 / 超时语义 / 权限规范化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
