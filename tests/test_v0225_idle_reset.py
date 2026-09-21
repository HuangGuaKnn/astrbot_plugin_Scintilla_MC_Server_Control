"""v0.22.5 回归用例：idle 降级模式不污染后续调用 / RCON 重建不泄漏旧连接。

对应 v0.22.4 核验报告里剩下的两个问题（改前必须复现、改后必须全绿）：

  P1  idle 静默窗口到期后仍复用脏连接
      —— 服务端 0.3s 才补发后半截，窗口 0.1s。旧实现第一次调用返回 FIRST（半截当成功）
         且 connected=True，下一条命令才吃到残留包、报响应 id 不匹配：错误挂在第二条
         命令头上，第一条的结果「静默假成功」。
  P2  /rcon/reset 丢弃旧实例时不关连接
      —— 旧实例的 StreamWriter 被无声遗弃；连点按钮 / 有在途命令时更明显。
         另测：重建瞬间正在跑的那条命令**不该**被判成失败（不粗暴掐断在途调用）。

运行（建议用 AstrBot 自带解释器；重建用例需要 astrbot 包）：
  <python> tests/test_v0225_idle_reset.py
找不到 AstrBot 运行目录时自动跳过重建用例，RCON 协议用例照常执行。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

PLUGIN = PLUGIN_DIR
FAIL: list[str] = []
SKIP: list[str] = []
NUL2 = bytes([0, 0])


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rcon_mod = _load("v5_rcon", "core/rcon.py")
AsyncRcon = rcon_mod.AsyncRcon
RconError = rcon_mod.RconError
RconTimeoutError = rcon_mod.RconTimeoutError
RconPartialResponseError = rcon_mod.RconPartialResponseError

# 插件层用例需要 AstrBot 运行时。**导入必须放在模块加载阶段**：astrbot 的包初始化
# 一旦落在已经跑起来的事件循环里就会阻塞（本轮实测如此），所以这里先导好、只标记可用性。
HAS_APP = True
_APP_ERR = ""
try:
    require_app()
    add_sys_paths()
    import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402
except Exception as e:  # noqa: BLE001
    HAS_APP = False
    _APP_ERR = str(e)
    m = None


# ---------------------------------------------------------------- 假 RCON 服务

def pkt(req_id: int, ptype: int, payload: str) -> bytes:
    body = payload.encode("utf-8")
    return struct.pack("<iii", 10 + len(body), req_id, ptype) + body + NUL2


async def _read(r):
    n = struct.unpack("<i", await r.readexactly(4))[0]
    b = await r.readexactly(n)
    i, t = struct.unpack("<ii", b[:8])
    return i, t, b[8:-2].decode("utf-8", errors="replace")


_BG: set = set()


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)


async def _late(w, i, text, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        w.write(pkt(i, 0, text))
        await w.drain()
    except (ConnectionError, OSError):
        pass


CONNS: list = []          # 记录每条被服务端接受的连接，供「旧连接是否被关」断言


def make_server(mode: str):
    state = {"phase": 0}

    async def handler(r, w):
        CONNS.append(w)
        try:
            while True:
                i, t, s = await _read(r)
                if t == 3:
                    w.write(pkt(i, 2, ""))
                    await w.drain()
                    continue
                if mode == "once_then_silent":
                    # 第一条正式命令的事务（命令 + 它的哨兵）正常处理；之后收了命令一个字都不回。
                    # 用来验证「上一次成功 → 这一次失联」时边界状态必须翻回 False。
                    if s == "":
                        if state["phase"] <= 1:
                            w.write(pkt(i, 0, ""))
                            await w.drain()
                        continue
                    state["phase"] += 1
                    if state["phase"] == 1:
                        w.write(pkt(i, 0, "REPLY:" + s))
                        await w.drain()
                    continue
                if mode == "split" and s == "multi":
                    # 关键场景：立刻回半截 FIRST，0.3s 后才补 LAST。
                    # idle 窗口 0.1s < 0.3s → 客户端会把 FIRST 当「读完了」。
                    # 只拆这一条命令：其它命令正常回，用来验证「下一条命令干不干净」。
                    w.write(pkt(i, 0, "FIRST"))
                    await w.drain()
                    _spawn(_late(w, i, "LAST", 0.3))
                    continue
                if mode == "reorder":
                    # 乱序服务端：哨兵（空命令）立刻回，正式命令故意延后 → 哨兵抢先到达
                    if s == "":
                        w.write(pkt(i, 0, ""))
                        await w.drain()
                    else:
                        _spawn(_late(w, i, "REPLY:" + s, 0.05))
                    continue
                if mode == "silent":
                    continue          # 收了命令但一个字都不回（服务器失联的样子）
                if mode == "slow_reply":
                    await asyncio.sleep(0.3)      # 慢服务端：命令在途期间被打断才会暴露问题
                    w.write(pkt(i, 0, "REPLY:" + s))
                    await w.drain()
                    continue
                w.write(pkt(i, 0, "REPLY:" + s))
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass
    return handler


async def serve(mode: str):
    srv = await asyncio.start_server(make_server(mode), "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


# ---------------------------------------------------------------- P1：idle 脏连接

async def idle_cases() -> None:
    print("=========== P1：idle 降级模式不得污染后续调用 ===========")

    srv, port = await serve("split")
    c = AsyncRcon(port=port, timeout=1.0, end_mode="idle", idle_probe=0.1)
    await c.connect()

    out1 = await c.command("multi", timeout=1.0)
    check("idle 半截响应：仍然返回已收到的内容（不把该模式整条打死）", out1 == "FIRST", repr(out1))
    check("idle 半截响应：结果被标记为「边界未确认」", c.last_boundary_confirmed is False, str(c.last_boundary_confirmed))
    check("idle 半截响应：连接已废弃（旧实现 connected=True，残留包留给下一次）",
          c.connected is False, f"connected={c.connected}")
    await asyncio.sleep(0.45)   # 等迟到包真正发出（此时它应打在被废弃的连接上 → 无人接收）

    conns_after_first = len(CONNS)
    out2 = await c.command("multi2", timeout=1.0)
    check("下一条命令干净返回（不吃上一条的残留 LAST）", out2 == "REPLY:multi2", repr(out2))
    check("下一条命令确实重建了新连接（旧连接已被废弃，不带着脏状态复用）",
          len(CONNS) > conns_after_first, f"连接数 {conns_after_first} → {len(CONNS)}")

    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 对照组：idle 模式正常单包响应仍然是可靠的常规路径 ----
    srv, port = await serve("plain")
    c = AsyncRcon(port=port, timeout=0.6, end_mode="idle", idle_probe=0.1)
    await c.connect()
    out = await c.command("hello", timeout=0.6)
    check("对照组：idle 单包响应正常返回", out == "REPLY:hello", repr(out))
    out = await c.command("hello2", timeout=0.6)
    check("对照组：idle 可连续调用（每次重建连接后照常工作）", out == "REPLY:hello2", repr(out))
    check("对照组：idle 单包也标记为「边界未确认」（诚实标注，不冒充可靠边界）",
          c.last_boundary_confirmed is False, str(c.last_boundary_confirmed))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 回归：idle 下一个包都没收到仍必须是「结果未知」而不是空串 ----
    srv, port = await serve("silent")
    c = AsyncRcon(port=port, timeout=0.4, end_mode="idle", idle_probe=0.1)
    await c.connect()
    try:
        await c.command("hello", timeout=0.4)
        check("idle 零响应：必须抛 RconTimeoutError", False, "居然没抛异常")
    except RconTimeoutError as e:
        check("idle 零响应仍抛 RconTimeoutError（绝不返回空串冒充合法空响应）",
              isinstance(e, RconTimeoutError), type(e).__name__)
    except RconError as e:
        check("idle 零响应仍抛 RconTimeoutError", False, f"{type(e).__name__}: {e}")
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 回归：哨兵模式半截响应照旧报「结果未知」（v0.22.3 行为不变）----
    srv, port = await serve("reorder")
    c = AsyncRcon(port=port, timeout=0.5, end_mode="sentinel")
    await c.connect()
    try:
        await c.command("say hello", timeout=0.5)
        check("哨兵乱序（哨兵先于命令响应到达）：必须抛 RconPartialResponseError", False, "居然没抛异常")
    except RconPartialResponseError:
        check("哨兵乱序仍抛 RconPartialResponseError（v0.22.3 行为保持）", True)
    except RconError as e:
        check("哨兵乱序仍抛 RconPartialResponseError", False, f"{type(e).__name__}: {e}")
    await c.close()
    srv.close()
    await srv.wait_closed()


async def boundary_state_cases() -> None:
    """v0.22.5 复审 P2：last_boundary_confirmed 必须是「最近一次」的诚实三态。

    改前的毛病：只有「哨兵成功」写 True、「idle 返回」写 False，
    超时 / 零响应 / 协议异常路径**什么都不写** → 上一次的 True 被原样沿用，
    页面于是显示「边界可靠」，而最近一次执行其实结果未知（与事实相反）。
    """
    print("=========== 复审 P2：边界状态必须反映「最近一次」 ===========")

    # ---- ① 尚未执行过命令 → None（暂无结果），不得默认成 True ----
    srv, port = await serve("plain")
    c = AsyncRcon(port=port, timeout=1.0)
    check("尚未执行任何命令 → None（不是 True，页面据此说「尚未执行过命令」）",
          c.last_boundary_confirmed is None, str(c.last_boundary_confirmed))
    await c.connect()
    check("只建连、还没发命令 → 仍然 None（连上 ≠ 确认过边界）",
          c.last_boundary_confirmed is None, str(c.last_boundary_confirmed))

    # ---- ② 哨兵到齐 → True ----
    out = await c.command("ping", timeout=1.0)
    check("哨兵到齐 → True（这才是「可靠边界」唯一来源）",
          out == "REPLY:ping" and c.last_boundary_confirmed is True,
          f"{out!r} confirmed={c.last_boundary_confirmed}")
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- ③ 关键回归：上一次成功 → 下一次超时，不得沿用 True ----
    srv, port = await serve("silent")
    c = AsyncRcon(port=port, timeout=0.5)
    await c.connect()
    try:
        await c.command("say hi", timeout=0.4)
        check("零响应：必须抛 RconTimeoutError", False, "居然没抛异常")
    except RconTimeoutError:
        check("零响应：必须抛 RconTimeoutError", True)
    check("零响应后 → False（改前这里沿用上一次的 True，页面谎报「边界可靠」）",
          c.last_boundary_confirmed is False, str(c.last_boundary_confirmed))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- ④ 关键回归：上一次成功 → 下一次哨兵乱序（协议异常）→ False ----
    srv, port = await serve("reorder")
    c = AsyncRcon(port=port, timeout=0.5)
    await c.connect()
    check("乱序用例起点：还没发命令 → None", c.last_boundary_confirmed is None,
          str(c.last_boundary_confirmed))
    try:
        await c.command("say hello", timeout=0.5)
        check("乱序：必须抛 RconPartialResponseError", False, "居然没抛异常")
    except RconPartialResponseError:
        check("乱序：必须抛 RconPartialResponseError", True)
    check("协议异常后 → False（命令没执行完就弃连，绝不留 True）",
          c.last_boundary_confirmed is False, str(c.last_boundary_confirmed))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- ⑤ 关键回归：先成功、再失联（零响应）→ 最近一次必须是 False ----
    # [NOTE 实现假设] 零响应路径**不**抛 RconPartialResponseError，而是抛 RconTimeoutError：
    #   代码注释的理由是「服务器整体失联与哨兵机制是否被支持无关，不该因此降级完整性保证」。
    #   但严格说：先发出探测命令、再一个包都不回来，这与「服务端不支持哨兵」在网络层面
    #   不可区分（两个包要一起丢）。于是同一个不可靠服务端会反复被当成「失联」，
    #   probe_misses 永不累加 → 自动降级可能迟迟不触发（只影响可用性，不影响数据正确性：
    #   该路径永远是「结果未知」，绝不静默假成功）。此行为待主人裁决，故这里同时接受两种异常。
    srv, port = await serve("once_then_silent")
    c = AsyncRcon(port=port, timeout=0.5)
    await c.connect()
    out = await c.command("ok_once", timeout=0.5)
    check("先来一条成功的（True 的起点）",
          out == "REPLY:ok_once" and c.last_boundary_confirmed is True,
          f"{out!r} confirmed={c.last_boundary_confirmed}")
    try:
        await c.command("second", timeout=0.4)
        check("失联后：必须抛 RconTimeoutError / RconPartialResponseError", False, "居然没抛异常")
    except (RconTimeoutError, RconPartialResponseError):
        check("失联后：必须抛 RconTimeoutError / RconPartialResponseError", True)
    check("成功后再遇「结果未知」 → False（改前这里沿用上一次的 True，字段与最近一次执行结果相反）",
          c.last_boundary_confirmed is False, str(c.last_boundary_confirmed))
    await c.close()
    srv.close()
    await srv.wait_closed()


# ---------------------------------------------------------------- P2：重建不泄漏

async def reset_cases() -> None:
    print("=========== P2：RCON 重建 / 退役语义 ===========")

    # ---- 退役实例：不许再建新连，且用完立即关闭 ----
    srv, port = await serve("plain")
    c = AsyncRcon(port=port, timeout=1.0)
    await c.connect()
    check("退役前 connected=True", c.connected is True)
    c.retire()
    try:
        await c.connect()
        check("退役实例拒绝重新建连", False, "居然允许重连")
    except RconError:
        check("退役实例拒绝重新建连（reset 后不会又悄悄占一条新连接）", True)
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 退役但仍有在途命令：让那条命令跑完，再自动关闭 ----
    srv, port = await serve("slow_reply")
    c = AsyncRcon(port=port, timeout=1.0)
    await c.connect()

    async def late_retire():
        await asyncio.sleep(0.15)
        c.retire()

    task = asyncio.create_task(late_retire())
    out = await c.command("hi", timeout=1.0)          # 0.3s 后才回，退役发生在它执行中
    await task
    check("重建时正在执行的命令不被掐断（照常拿到结果）", out == "REPLY:hi", repr(out))
    check("退役实例跑完那条命令后自动关闭（不留无人认领的连接）", c.connected is False, f"connected={c.connected}")
    srv.close()
    await srv.wait_closed()

    # ---- 插件层：reset_rcon 必须显式关闭旧连接（而不是无声丢弃）----
    if not HAS_APP:
        skip(f"插件层 reset_rcon 用例（缺 AstrBot 运行目录：{_APP_ERR}）")
        return

    srv, port = await serve("plain")
    plug = object.__new__(m.McControlPlugin)
    plug.config = {"rcon_host": "127.0.0.1", "rcon_port": port, "rcon_timeout": 1.0}
    plug._cfg_index = {}
    plug.logger = logging.getLogger("test")
    plug._rcon = None
    plug._rcon_lock = asyncio.Lock()

    old = await plug._get_rcon()
    await old.connect()
    check("夹具有效：reset 前旧实例已连上",
          old.connected is True and old._writer is not None,
          f"connected={old.connected} writer={old._writer}")
    socks_before = len(CONNS)
    new = await plug.reset_rcon()
    check("reset 后插件持有新实例", new is not old and plug._rcon is new)
    check("空闲旧实例被 reset 显式关闭（旧实现：socket 被无声遗弃，连着 StreamWriter 一起泄漏）",
          old.connected is False and old._writer is None, f"old.connected={old.connected}")
    check("reset 后旧实例已退役（不能偷偷重连）", old._retired is True)

    # ---- 连点重建：并发 reset 不报错、不泄漏、最终只留一个实例 ----
    results = await asyncio.gather(*[plug.reset_rcon() for _ in range(5)], return_exceptions=True)
    errs = [r for r in results if isinstance(r, Exception)]
    ok_objs = [r for r in results if not isinstance(r, Exception)]
    check("连点 5 次重建全部成功（并发不炸、不自锁）", not errs, str(errs[:2]))
    check("连点重建后插件只认最后一个实例", plug._rcon is ok_objs[-1], "插件持有的不是最后一次的实例")
    check("被换下的空闲实例全部关闭（无 connected=True 的孤儿）",
          all(r is plug._rcon or r.connected is False for r in ok_objs[:-1]), "存在未关闭的孤儿连接")
    check("没有重复建连空窗（摘旧建新同锁完成，连接数不膨胀）",
          len(CONNS) <= socks_before + 2, f"{socks_before} → {len(CONNS)}")

    # 连点重建后，插件当前实例必须能照常干活；上面拿到的 `new` 此刻已被后续 reset 退役
    cur = plug._rcon
    out = await cur.command("ping", timeout=1.0)
    check("重建后的（当前）实例可以正常执行命令", out == "REPLY:ping", repr(out))
    try:
        await new.command("ping", timeout=1.0)
        check("已被换下的实例不得再执行命令", False, "退役实例居然还能发命令")
    except Exception as e:  # noqa: BLE001 — 注意：测试独立加载的 rcon 与包内 rcon 是两个类对象，故按类名判定
        check("已被换下的实例拒绝执行命令（不会偷偷另起连接）",
              type(e).__name__ == "RconError", f"{type(e).__name__}: {e}")
    await plug.reset_rcon()
    srv.close()
    await srv.wait_closed()


async def main() -> int:
    await idle_cases()
    await boundary_state_cases()
    await reset_cases()
    if FAIL:
        print("\n✗ 失败项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"\n✓ 全部通过（跳过 {len(SKIP)} 组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
