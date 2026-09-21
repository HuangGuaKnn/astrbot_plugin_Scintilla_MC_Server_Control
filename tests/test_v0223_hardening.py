"""v0.22.3 加固回归用例：RCON 结束边界 / 工作流「结果未知」熔断 / execute 权限 fail-closed。

对应 v0.22.2 核验时复现出的三个 P1（原样保留复现手法，改后必须全绿）：

  P1-a  RCON 分包截断：150ms 静默窗口判结束 → 首包 220ms 后才到次包时只拿到 FIRST，
        下一条命令还会吃到残留包（connected=True 但流已错位）。
  P1-b  工作流把「结果未知」当普通失败 → 同一个 give 被发两次（重复副作用）。
  P1-c  execute 权限解析 fail-open → ``execute as run run stop``、超深 execute 包装均放行。

运行（建议用 AstrBot 自带解释器；工作流用例需要 astrbot 包）：
  <python> tests/test_v0223_hardening.py
找不到 AstrBot 运行目录时自动跳过工作流用例，RCON 与权限用例照常执行。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import struct
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

PLUGIN = PLUGIN_DIR
FAIL: list[str] = []
SKIP: list[str] = []
NUL2 = bytes([0, 0])   # RCON 包尾的 \x00\x00（这里用构造避免源码转义）


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


rcon_mod = _load("h_rcon", "core/rcon.py")
jc = _load("h_jc", "core/java_commands.py")
AsyncRcon = rcon_mod.AsyncRcon
RconError = rcon_mod.RconError
RconTimeoutError = rcon_mod.RconTimeoutError
RconPartialResponseError = rcon_mod.RconPartialResponseError


# ---------------------------------------------------------------- 假 RCON 服务

def pkt(req_id: int, ptype: int, payload: str) -> bytes:
    body = payload.encode("utf-8")
    return struct.pack("<iii", 10 + len(body), req_id, ptype) + body + NUL2


async def _read(r):
    n = struct.unpack("<i", await r.readexactly(4))[0]
    b = await r.readexactly(n)
    i, t = struct.unpack("<ii", b[:8])
    return i, t, b[8:-2].decode("utf-8", errors="replace")


async def _respond(i, s, mode, w) -> None:
    """按脚本模式回应。哨兵 = 空命令（插件默认 probe_command），其余为普通命令。"""
    probe = s == ""
    if mode == "reorder" and not probe:
        # 真·乱序：命令响应延后 50ms，哨兵立刻回 —— 客户端会先看到哨兵
        task = asyncio.create_task(_late_reply(w, i, "REPLY:" + s))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return
    if mode == "delayed220" and s == "multi":      # 首包后 220ms 才到次包（核验复现的原始场景）
        w.write(pkt(i, 0, "FIRST"))
        await w.drain()
        await asyncio.sleep(0.22)
        w.write(pkt(i, 0, "LAST"))
        await w.drain()
        return
    if mode == "partial_then_rest" and s == "multi":   # 次包只先发半截，220ms 后补完
        w.write(pkt(i, 0, "FIRST") + pkt(i, 0, "LAST")[:4])
        await w.drain()
        await asyncio.sleep(0.22)
        w.write(pkt(i, 0, "LAST")[4:])
        await w.drain()
        return
    if mode == "slow1s" and s == "multi":          # 首包后 1s 才到次包
        w.write(pkt(i, 0, "ONE"))
        await w.drain()
        await asyncio.sleep(1.0)
        w.write(pkt(i, 0, "TWO"))
        await w.drain()
        return
    if mode == "half_header" and s == "x":         # 只发 3 字节包头就停住
        w.write(struct.pack("<i", 20)[:3])
        await w.drain()
        await asyncio.sleep(5)
        return
    if mode == "bad_length" and s == "x":          # 非法长度字段
        w.write(struct.pack("<i", 4) + bytes(4))
        await w.drain()
        return
    if mode == "no_terminator" and s == "x":       # 包尾没有 NUL2
        body = bytes([88] * 8)
        w.write(struct.pack("<i", len(body)) + body)
        await w.drain()
        return
    if mode == "flood" and s == "x":               # 包数超上限
        for _ in range(400):
            w.write(pkt(i, 0, "x"))
        await w.drain()
        return
    if mode == "probe_unanswered" and probe:    # 只回命令、绝不回哨兵
        return
    if mode == "empty" and s == "x":            # 合法空响应
        w.write(pkt(i, 0, ""))
        await w.drain()
        return
    w.write(pkt(i, 0, "" if probe else "REPLY:" + s))
    await w.drain()


_BG_TASKS: set = set()


async def _late_reply(w, i, text, delay=0.05) -> None:
    """乱序服务端用：把某条命令的响应延后发出。"""
    await asyncio.sleep(delay)
    try:
        w.write(pkt(i, 0, text))
        await w.drain()
    except (ConnectionError, OSError):
        pass


def make_server(mode: str):
    async def handler(r, w):
        try:
            while True:
                i, t, s = await _read(r)
                if t == 3:
                    w.write(pkt(i, 2, ""))
                    await w.drain()
                    continue
                await _respond(i, s, mode, w)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            w.close()
    return handler


async def serve(mode: str):
    srv = await asyncio.start_server(make_server(mode), "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


# ---------------------------------------------------------------- RCON 用例

async def rcon_cases() -> None:
    print("=========== P1-a：RCON 结束边界 / 协议异常 ===========")

    # ---- 延迟分包：220ms 才到次包（旧版只拿到 FIRST，并把残留包留给下一条命令）----
    srv, port = await serve("delayed220")
    c = AsyncRcon(port=port, timeout=2.0)
    await c.connect()
    out = await c.command("multi", timeout=2.0)
    check("首包后 220ms 才到次包：完整收齐 FIRST+LAST", out == "FIRSTLAST", repr(out))
    nxt = await c.command("next", timeout=2.0)
    check("收齐后下一条命令干净（旧版会吃到残留包并报错）", nxt == "REPLY:next", repr(nxt))
    check("完成后连接仍可用（connected=True）", c.connected is True)
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 半包停顿：次包只先到半截 ----
    srv, port = await serve("partial_then_rest")
    c = AsyncRcon(port=port, timeout=2.0)
    await c.connect()
    out = await c.command("multi", timeout=2.0)
    check("次包先到半截、220ms 后补完：依然完整收齐", out == "FIRSTLAST", repr(out))
    check("半包补完后连接未被误废", c.connected is True)
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 1s 延迟分包 ----
    srv, port = await serve("slow1s")
    c = AsyncRcon(port=port, timeout=3.0)
    await c.connect()
    out = await c.command("multi", timeout=3.0)
    check("首包后 1s 才到次包：完整收齐 ONE+TWO", out == "ONETWO", repr(out))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 半包超时：绝不能静默返回部分结果，且必须废弃连接 ----
    srv, port = await serve("half_header")
    c = AsyncRcon(port=port, timeout=0.4)
    await c.connect()
    try:
        got = await c.command("x", timeout=0.4)
        check("只收到 3 字节包头 → 不得返回结果", False, f"竟然返回了 {got!r}")
    except RconTimeoutError as e:
        check("只收到 3 字节包头 → 抛结果未知（不静默返回部分）", True)
        check("半包超时文案点明「结果未知」", "未知" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("只收到 3 字节包头 → 抛结果未知", False, f"抛了别的：{e!r}")
    check("半包超时后连接被废弃（不留 connected=True 的错位连接）", c.connected is False)
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 非法长度 / 缺包尾 ----
    for mode, label in (("bad_length", "非法长度字段"), ("no_terminator", "包尾缺少 NUL2")):
        srv, port = await serve(mode)
        c = AsyncRcon(port=port, timeout=0.6)
        await c.connect()
        try:
            got = await c.command("x", timeout=0.6)
            check(f"{label} → 必须报错", False, f"竟然返回了 {got!r}")
        except RconError as e:
            check(f"{label} → 抛 RconError", True)
            check(f"{label} → 连接被废弃", c.connected is False, f"connected={c.connected} err={e}")
        except Exception as e:  # noqa: BLE001
            check(f"{label} → 抛 RconError", False, f"抛了别的：{e!r}")
        await c.close()
        srv.close()
        await srv.wait_closed()

    # ---- 包数超上限：必须报错，不能静默返回部分 ----
    srv, port = await serve("flood")
    c = AsyncRcon(port=port, timeout=2.0)
    await c.connect()
    try:
        got = await c.command("x", timeout=2.0)
        check("包数超过上限 → 必须报错", False, f"竟然返回了 {len(got)} 字节")
    except RconError as e:
        check("包数超过上限 → 抛 RconError（不静默返回部分）", "上限" in str(e), str(e))
        check("包数超限后连接被废弃", c.connected is False)
    except Exception as e:  # noqa: BLE001
        check("包数超过上限 → 抛 RconError", False, f"抛了别的：{e!r}")
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 合法空响应：哨兵确认「确实没有输出」≠ 超时 ----
    srv, port = await serve("empty")
    c = AsyncRcon(port=port, timeout=1.0)
    await c.connect()
    empty = await c.command("x", timeout=1.0)
    check("合法空响应 → 返回空字符串且不抛错", empty == "", repr(empty))
    check("空响应后连接仍可用", c.connected is True)
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 服务端不回应哨兵：绝不静默当成功，连续多次后自动降级 ----
    class _Log:
        """最小日志器替身：验证降级告警确实发出（插件注入 AstrBot logger）。"""

        def __init__(self):
            self.records = []

        def warning(self, msg, *a):
            self.records.append(msg % a if a else msg)

    log = _Log()
    srv, port = await serve("probe_unanswered")
    c = AsyncRcon(port=port, timeout=0.4, logger=log)
    await c.connect()
    errs: list[Exception] = []
    for _ in range(3):
        try:
            await c.command("np", timeout=0.4)
        except RconError as e:  # noqa: BLE001
            errs.append(e)
    check("不回应哨兵：拿到输出但边界不明 → 抛结果未知（不静默当成功）",
          len(errs) == 3 and all(isinstance(e, RconTimeoutError) for e in errs),
          repr([type(e).__name__ for e in errs]))
    check("结果未知异常携带已收到的部分输出（便于人工核对）",
          all(getattr(e, "partial", "") == "REPLY:np" for e in errs),
          repr([getattr(e, "partial", "") for e in errs]))
    check("部分响应异常是 RconTimeoutError 的子类（上层「不得重发」路径自动生效）",
          all(isinstance(e, RconPartialResponseError) for e in errs))
    check("连续 3 次拿不到哨兵 → 自动降级为 idle 模式", c.end_mode == "idle", c.end_mode)
    check("自动降级会留下明确告警（日志器由插件注入，不依赖标准库 logging）",
          len(log.records) == 1 and "哨兵" in log.records[0], repr(log.records))
    out = await c.command("np", timeout=0.4)
    check("降级后命令恢复正常返回（不至于让服务端彻底不可用）", out == "REPLY:np", repr(out))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 显式 idle 降级模式仍可用 ----
    srv, port = await serve("normal")
    c = AsyncRcon(port=port, timeout=1.0, end_mode="idle", idle_probe=0.1)
    await c.connect()
    out = await c.command("hello")
    check("显式 idle 模式：命令正常返回", out == "REPLY:hello", repr(out))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 乱序服务端：哨兵先于命令响应到达（v0.22.4 核验 P1）----
    class _Log:
        def __init__(self):
            self.records = []

        def warning(self, msg, *a):
            self.records.append(msg % a if a else msg)

    log2 = _Log()
    srv, port = await serve("reorder")
    c = AsyncRcon(port=port, timeout=0.6, logger=log2)
    await c.connect()
    try:
        got = await c.command("say hi", timeout=0.6)
        check("哨兵先于命令响应 → 不得静默当成功", False, f"竟然返回了 {got!r}")
    except RconTimeoutError as e:
        check("哨兵先于命令响应 → 抛结果未知（RconPartialResponseError）",
              isinstance(e, RconPartialResponseError), type(e).__name__)
        check("顺序异常文案点明原因", "顺序" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("哨兵先于命令响应 → 抛结果未知", False, f"抛了别的：{e!r}")
    check("顺序异常后连接被废弃（迟到包不留到下一次调用）", c.connected is False)

    # 连续 3 次顺序异常 → 自动降级 idle 自愈（并有日志，可见可控）
    for _ in range(2):
        try:
            await c.command("say hi", timeout=0.6)
        except Exception:  # noqa: BLE001
            pass
    check("连续 3 次顺序异常 → 自动降级为 idle（自愈）", c.end_mode == "idle", c.end_mode)
    check("顺序异常导致的降级会写日志说明", bool(log2.records) and "降级" in log2.records[-1],
          repr(log2.records[-1:]))
    out2 = await c.command("say hi", timeout=0.6)
    check("降级后（单请求不存在乱序）命令恢复正常返回", out2 == "REPLY:say hi", repr(out2))
    await c.close()
    srv.close()
    await srv.wait_closed()

    # ---- 任务取消：连接状态不确定 → 必须废弃 ----
    srv, port = await serve("half_header")
    c = AsyncRcon(port=port, timeout=5.0)
    await c.connect()
    task = asyncio.create_task(c.command("x", timeout=5.0))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        check("取消中的命令 → 传播取消", False, f"抛了别的：{e!r}")
    check("命令被取消 → 连接被废弃（不留错位连接）", c.connected is False)
    await c.close()
    srv.close()
    await srv.wait_closed()


# ---------------------------------------------------------------- 权限用例

def permission_cases() -> None:
    print("=========== P1-c：execute 权限解析 fail-closed ===========")
    deny_cases = [
        "execute as run run stop",                       # 首个 run 是玩家名，不是分界
        "execute as run run gamemode spectator @s",
        "execute run " * 9 + "stop",                     # 超过深度上限
        "execute run " * 17 + "stop",                    # 核验复现的 17 层
        "execute run execute run stop",
        "execute if score a b ??? run stop",             # 条件边界判不出来
        "execute garbage run stop",                      # 未知子命令
        "execute run",                                   # run 之后没有命令
    ]
    for cmd in deny_cases:
        r = jc.check_command(cmd, policy="blacklist", is_admin=False)
        check(f"拒绝（不放行）：{cmd[:44]!r}", r is not None, "放行了！")

    allow_cases = [
        "give Steve diamond 1",
        "execute as @a run say hi",
        "execute as @a run give @s diamond 1",
        "execute run " * 8 + "give Steve diamond 1",     # 恰好 8 层：仍在上限内
        "execute if block ~ ~-1 ~ stone run say hi",
        "execute positioned as @a ~ ~ ~ run say hi",
        "execute store result score @s obj run time query daytime",
        "execute as @a run tellraw @a {\"text\":\"hi there\"}",   # 参数里带空格也不能误判
    ]
    for cmd in allow_cases:
        r = jc.check_command(cmd, policy="blacklist", is_admin=False)
        check(f"正常命令仍放行：{cmd[:50]!r}", r is None, str(r))

    check("解析失败时 is_danger_command 按危险处理（fail-closed）",
          jc.is_danger_command("execute garbage run stop") is True)
    check("解析失败时 effective_level 标记为无法安全判定",
          jc.effective_level("execute run " * 9 + "stop")[2] is True)
    check("管理员不受策略限制",
          jc.check_command("execute as run run stop", policy="blacklist", is_admin=True) is None)
    check("白名单仍整体拦非管理员",
          jc.check_command("give Steve diamond 1", policy="whitelist", is_admin=False) is not None)
    check("危险命令解包判定仍一致（命名空间 / 多层包装）",
          jc.is_danger_command("execute as @a at @s run minecraft:gamemode spectator @s")
          and jc.is_danger_command("minecraft:stop"))


# ---------------------------------------------------------------- 工作流用例

class FakeRcon:
    """按脚本依次回应：脚本项是异常就抛，否则当返回值。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[str] = []

    async def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        action = self.script.pop(0) if self.script else RuntimeError("脚本用尽")
        if isinstance(action, Exception):
            raise action
        return action


class FakeAgent:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.implement_calls = 0
        self.correct_calls = 0

    async def judge(self, *a, **k):
        return {"sufficient": True}

    async def implement(self, *a, **k):
        self.implement_calls += 1
        cmds = self.rounds.pop(0) if self.rounds else []
        return {"commands": [{"command": c} for c in cmds], "success": True, "reasoning": "r"}

    async def correct(self, *a, **k):
        self.correct_calls += 1
        return {"corrections": [], "retry_hint": "换一种写法再试", "lessons": "l"}


class FakePlugin:
    def __init__(self, rcon):
        self._rcon = rcon
        self._knowledge = None
        self._dictionary = None
        self.logger = logging.getLogger("test.v0223")

    async def _get_rcon(self):
        return self._rcon

    async def _send_feedback(self, rcon, text):
        return None


def workflow_cases() -> bool:
    """跑工作流用例；找不到 AstrBot 运行目录则跳过并返回 False。"""
    add_sys_paths()
    try:
        require_app()
        from astrbot_plugin_Scintilla_MC_Server_Control.core import rcon as pkg_rcon  # noqa: PLC0415
        from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import (  # noqa: PLC0415
            MCWorkflow,
        )
    except Exception as e:  # noqa: BLE001
        skip(f"工作流用例（需要 AstrBot 运行目录：{e}）")
        return False

    # 关键：必须用「插件包内」的异常类。importlib 独立加载出来的模块会造出另一个
    # 同名类，工作流的 except 抓不住它，测试会误判成「没有熔断」。
    global RconError, RconTimeoutError
    RconError = pkg_rcon.RconError
    RconTimeoutError = pkg_rcon.RconTimeoutError

    def build(rcon, agent, cfg):
        wf = object.__new__(MCWorkflow)
        wf.plugin = FakePlugin(rcon)
        wf.agent = agent
        wf.logger = logging.getLogger("test.v0223")
        wf.logs = deque(maxlen=30)
        wf._tasks = set()
        wf._cfg = lambda key, default=None: cfg.get(key, default)

        async def _resolve(player, request):
            return (player or "Steve"), ["Steve", "A", "B"]

        wf._resolve_player = _resolve
        return wf

    print("=========== P1-b：工作流「结果未知」熔断 ===========")

    # ---- 单条命令结果未知：只发一次、不重试、后续命令一条都不发 ----
    rcon = FakeRcon([RconTimeoutError("服务器没响应")])
    agent = FakeAgent([["give Steve diamond 1", "summon zombie", "effect give Steve speed 30"]])
    wf = build(rcon, agent, {"agent_max_implement_rounds": 3, "agent_max_correct_rounds": 2})
    text, ok = asyncio.run(wf._run_complex("给史蒂夫钻石", "Steve", "umo"))
    check("结果未知：触发未知的那条命令只发送了一次", rcon.sent == ["give Steve diamond 1"], repr(rcon.sent))
    check("结果未知：后续命令一条都没发（避免重复副作用）", len(rcon.sent) == 1, repr(rcon.sent))
    check("结果未知：不再进入下一轮实现", agent.implement_calls == 1, str(agent.implement_calls))
    check("结果未知：不进入纠错循环", agent.correct_calls == 0, str(agent.correct_calls))
    check("结果未知：返回文本明确「暂停 + 结果未知」", "暂停" in text and "结果未知" in text, text)
    check("结果未知：工作流返回失败", ok is False)

    # ---- 三态标记：unknown / skipped ----
    rcon2 = FakeRcon([RconTimeoutError("服务器没响应")])
    wf2 = build(rcon2, FakeAgent([[]]), {})
    reports = asyncio.run(
        wf2._exec_commands(
            [{"command": "give A 1"}, {"command": "give B 1"}], "Steve", ["A", "B"]
        )
    )
    check("三态：结果未知标为 status=unknown 且 unknown=True",
          reports[0].get("status") == "unknown" and reports[0].get("unknown") is True,
          repr(reports[0]))
    check("三态：后续未发送的命令标为 status=skipped",
          len(reports) == 2 and reports[1].get("status") == "skipped", repr(reports))
    fmt = MCWorkflow._fmt_results(reports)
    check("结果明细用 [未知] / [未发送] 区分（不再一律 FAIL）",
          "未知" in fmt and "未发送" in fmt, fmt)

    # ---- 合法空响应 + 非幂等命令：不能当失败交给 Agent 重试（v0.22.4） ----
    rcon5 = FakeRcon(["", ""])
    wf5 = build(rcon5, FakeAgent([[]]), {})
    reports5 = asyncio.run(
        wf5._exec_commands(
            [{"command": "give A diamond 1"}, {"command": "give B diamond 1"}], "Steve", ["A", "B"]
        )
    )
    check("空响应 + 非幂等命令 → 标为结果未知（不当普通失败）",
          reports5[0].get("status") == "unknown" and "空响应" in reports5[0].get("output", ""),
          repr(reports5[0]))
    check("空响应 + 非幂等命令 → 后续命令同样不再发送（避免重复副作用）",
          rcon5.sent == ["give A diamond 1"] and reports5[1].get("status") == "skipped",
          repr(rcon5.sent))

    # 对照组：普通命令拿到空响应仍按既有口径（失败），不额外扩大熔断范围
    rcon6 = FakeRcon(["", ""])
    wf6 = build(rcon6, FakeAgent([[]]), {})
    reports6 = asyncio.run(
        wf6._exec_commands([{"command": "say hi"}, {"command": "say again"}], "Steve", ["A", "B"])
    )
    check("对照组：普通命令空响应仍按既有口径（failed，不误熔断）",
          reports6[0].get("status") == "failed" and len(reports6) == 2, repr(reports6))
    check("对照组：普通命令空响应后仍继续执行后续命令",
          rcon6.sent == ["say hi", "say again"], repr(rcon6.sent))

    # ---- 纠错循环里出现未知：同样必须熔断 ----
    rcon3 = FakeRcon([RconError("执行异常"), RconTimeoutError("服务器没响应")])
    agent3 = FakeAgent([["give Steve diamond 1"], ["give Steve diamond 1"]])
    wf3 = build(rcon3, agent3, {"agent_max_implement_rounds": 1, "agent_max_correct_rounds": 2})
    text3, ok3 = asyncio.run(wf3._run_complex("给钻石", "Steve", "umo"))
    check("明确失败 → 允许按既有流程重试（本级仍发出 2 次）", len(rcon3.sent) == 2, repr(rcon3.sent))
    check("纠错循环中遇到结果未知 → 立即熔断，不再重发", len(rcon3.sent) == 2, repr(rcon3.sent))
    check("纠错循环中遇到结果未知 → 不再调用实现器", agent3.implement_calls == 2, str(agent3.implement_calls))
    check("纠错循环中遇到结果未知 → 返回暂停文案", "暂停" in text3, text3)
    check("纠错循环中遇到结果未知 → 工作流返回失败", ok3 is False)

    # ---- 明确失败不能被误当成「未知」而熔断 ----
    rcon4 = FakeRcon([RconError("boom"), RconError("boom")])
    agent4 = FakeAgent([["say hi"], ["say hi"]])
    wf4 = build(rcon4, agent4, {"agent_max_implement_rounds": 2, "agent_max_correct_rounds": 0})
    text4, ok4 = asyncio.run(wf4._run_complex("测试", "Steve", "umo"))
    check("明确失败不会被误熔断（仍按普通失败重试到底）",
          agent4.implement_calls == 2 and "暂停" not in text4 and ok4 is False, text4)
    return True


def main() -> int:
    asyncio.run(rcon_cases())
    permission_cases()
    workflow_cases()

    print("==========================================")
    if SKIP:
        print(f"跳过 {len(SKIP)} 组（环境限制，不算失败）：")
        for s in SKIP:
            print("  -", s)
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过：RCON 结束边界 / 协议异常废弃连接 / 结果未知熔断 / execute fail-closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
