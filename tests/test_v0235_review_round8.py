# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第八轮）回归：监听任务代际隔离 —— stop() 超时后旧任务不得复活。

GPT 第七轮核验在 3b14caa + 第七轮修复的工作区上复查后，确认六项收口有效，并指出
一处仍需修复的实际缺陷（P2）：

  G. **旧监听任务可能在 `stop()` 超时后与新任务并发运行**。第七轮只把 IO 计数
     按代隔离了（卡死的旧代 IO 不再挡新代），但 `_loop()` 任务本身没有代际判据：
     `on_event` 回调若吞掉 `CancelledError`，`stop()` 超时会把旧任务放进
     `_orphaned`；随后新的 `start()` 又把 `_running` 置回 `True` —— 旧任务从吞掉
     取消的地方继续轮询，与新任务并发读写同一个 watcher 的状态
     （`_pos` / `_partial` / 事件派发相互交错）。

修复判据（本文件，先红后绿）：
  - 静态：`_loop()` / `_poll()` 接收任务代号；`start()` 换代并交给 `_loop`；
    `_stop_locked()` 在取消任务**之前**先让旧代失效；`_poll` 在「数据读回后」与
    「事件派发前」都检查代际；`health()` 摊出 `task_gen`。
  - 行为 1：回调吞掉取消 → `stop()` 超时（旧任务进 `_orphaned`）→ 重新 `start()`
    （`_running` 又被置回 True）—— 旧任务必须在下一个代际检查点自行退出。
  - 行为 2：把 `_running` 手工置回 True（复刻「新 start 已置位、旧任务还没到检查
    点」的窗口）并写入第二行 —— 旧任务不得复活去读它：`_pos` / `read_bytes` /
    事件派发一个都不许动。
  - 对照：常规 `stop()` 仍是干净取消（不进孤儿名单）；同对象连续 3 轮
    start/stop 正常收口；任务代单调递增；`health()` 可见。

运行：
  <AstrBot python> tests\\test_v0235_review_round8.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402

LW_SRC = io.open(PLUGIN_DIR / "core" / "log_watcher.py", encoding="utf-8").read()

LINE1 = b"[12:00:00] [Server thread/INFO]: <Steve> one\n"      # 45 字节
LINE2 = b"[12:00:01] [Server thread/INFO]: <Steve> two\n"      # 45 字节

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ｜ {extra}" if extra else ""))


def fn_src(src: str, name: str, cls: str | None = None) -> str:
    """取某个函数 / 方法的源码片段（ast 定位，避免正则误抓同名的别处）。"""
    tree = ast.parse(src)
    node = None
    if cls:
        for top in ast.walk(tree):
            if isinstance(top, ast.ClassDef) and top.name == cls:
                for sub in top.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        node = sub
    else:
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and top.name == name:
                node = top
    assert node is not None, f"找不到 {cls}.{name}"
    return ast.get_source_segment(src, node) or ""


def _make_watcher(root, interval=0.05):
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    return LW.LogWatcher(str(root), on_event, poll_interval=interval), seen


def _swallowing_callback():
    """造一个「第 1 次调用吞掉 CancelledError」的回调（GPT 指出的场景）。

    第 1 次调用：置 gate、睡 30 秒等 `stop()` 把取消投进来 —— 收到取消后**吞掉**，
    再拖 0.6 秒（确保 `stop()` 的等待（测试里缩到 0.2s）超时、任务进了 `_orphaned`）
    才返回。修复前：任务从吞掉取消处继续跑；修复后：它到代际检查点必须自行退出。
    """
    calls: list = []
    swallowed: list = []
    gate = asyncio.Event()

    async def on_event(etype, player, detail):
        calls.append((etype, player, detail))
        if len(calls) == 1:
            gate.set()
            try:
                await asyncio.sleep(30)          # 等 stop() 把取消投进来
            except asyncio.CancelledError:
                swallowed.append(True)           # ★ 吞掉取消（修复要防的就是它）
                await asyncio.sleep(0.6)         # 拖到 stop() 超时、进孤儿名单之后

    return on_event, calls, swallowed, gate


def _prepare_log(root: Path) -> Path:
    (root / "logs").mkdir()
    log = root / "logs" / "latest.log"
    log.write_bytes(b"")
    return log


# ==================== G 静态面 ====================
def part_g_static() -> None:
    print("========== [G] 任务代际隔离：静态面 ==========")
    loop_src = fn_src(LW_SRC, "_loop", cls="LogWatcher")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    start_src = fn_src(LW_SRC, "start", cls="LogWatcher")
    stop_src = fn_src(LW_SRC, "_stop_locked", cls="LogWatcher")
    health_src = fn_src(LW_SRC, "health", cls="LogWatcher")

    args_of: dict = {}
    for n in ast.walk(ast.parse(LW_SRC)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ("_loop", "_poll"):
            args_of.setdefault(n.name, [a.arg for a in n.args.args])
    check("静态：_loop() 接收任务代号参数（gen）",
          "gen" in (args_of.get("_loop") or []), str(args_of.get("_loop")))
    check("静态：_poll() 接收任务代号参数（gen）",
          "gen" in (args_of.get("_poll") or []), str(args_of.get("_poll")))
    check("静态：_loop 每轮比对任务代（gen == / != self._task_gen）",
          ("gen == self._task_gen" in loop_src) or ("gen != self._task_gen" in loop_src))
    check("静态：start() 换新代并把代号交给 _loop",
          "_task_gen += 1" in start_src
          and ("self._loop(self._task_gen)" in start_src or "_loop(gen)" in start_src))
    check("静态：stop 路径在取消任务**之前**先让旧代失效",
          "_task_gen += 1" in stop_src
          and stop_src.index("_task_gen += 1") < stop_src.index("task.cancel()")
          and stop_src.index("_task_gen += 1") < stop_src.index("if task is None"))
    check("静态：_poll 至少两处代际检查（self._stale(gen)）",
          poll_src.count("self._stale(gen)") >= 2,
          f"count={poll_src.count('self._stale(gen)')}")
    idx_on = poll_src.index("await self.on_event")
    idx_read = poll_src.index("self.read_bytes += len(data)")
    check("静态：读回数据后、事件派发前都有代际检查（恢复点一个都走不过去）",
          "self._stale(gen)" in poll_src[idx_read: idx_on]
          and "self._stale(gen)" in poll_src[max(0, idx_on - 500): idx_on])
    check("静态：health() 摊出 task_gen", "task_gen" in health_src)


# ==================== G 行为 1：吞取消 → 超时孤儿 → 重启后必须自行退出 ====================
def part_g1_restart_after_swallowed_cancel() -> None:
    print("========== [G] 行为 1：吞掉取消 → stop 超时进孤儿 → 重启后旧任务自行退出 ==========")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            on_event, calls, swallowed, gate = _swallowing_callback()

            w = LW.LogWatcher(str(root), on_event, poll_interval=0.05)
            old_to = LW.STOP_TIMEOUT
            LW.STOP_TIMEOUT = 0.2                # 缩到 0.2s：保证走「超时 + 孤儿」路径
            old_task = None
            try:
                await w.start()
                with open(log, "ab") as fh:
                    fh.write(LINE1)
                try:
                    await asyncio.wait_for(gate.wait(), timeout=5.0)
                    entered = True
                except asyncio.TimeoutError:
                    entered = False
                check("前置：日志行被消费、回调进入（准备吞取消）", entered)
                if not entered:
                    return

                old_task = w._task
                await w.stop()                   # 取消被吞 → 0.2s 超时 → 旧任务进 _orphaned
                check("前置：取消被回调吞掉、stop 超时留下活着的旧任务",
                      bool(swallowed)
                      and (old_task in getattr(w, "_orphaned", set()) or not old_task.done()),
                      f"swallowed={len(swallowed)} done={old_task.done()}")

                await w.start()                  # 新代：_running 又被置回 True（旧任务此刻仍活着）
                await asyncio.wait({old_task}, timeout=3.0)
                check("★重启后旧任务自行退出（代际隔离：取消被吞也不复活）",
                      old_task.done(),
                      "旧任务在 stop 超时 + 新 start 之后仍然存活 —— 新旧监听并发")
                check("旧任务正常收尾（非取消、无未取异常，_reap_orphan 无告警可打）",
                      old_task.done() and not old_task.cancelled()
                      and old_task.exception() is None)
                await asyncio.sleep(0.1)         # 让 done 回调（_reap_orphan）跑掉
                check("孤儿登记随任务结束被回收（_orphaned 清空）",
                      len(getattr(w, "_orphaned", set())) == 0,
                      str(len(getattr(w, "_orphaned", set()))))
                check("health() 摊出任务代（整数、与内部字段一致）",
                      isinstance(w.health().get("task_gen"), int)
                      and w.health().get("task_gen") == getattr(w, "_task_gen", None))
            finally:
                LW.STOP_TIMEOUT = old_to
                await w.stop()
                if old_task is not None and not old_task.done():
                    old_task.cancel()
                    await asyncio.wait({old_task}, timeout=1.0)

    asyncio.run(scenario())


# ==================== G 行为 2：_running 置回 True 的窗口里不得再读文件 ====================
def part_g2_orphan_must_not_resume_polling() -> None:
    print("========== [G] 行为 2：_running 置回 True 的窗口里，旧任务不得再读文件 ==========")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            on_event, calls, swallowed, gate = _swallowing_callback()

            w = LW.LogWatcher(str(root), on_event, poll_interval=0.05)
            old_to = LW.STOP_TIMEOUT
            LW.STOP_TIMEOUT = 0.2
            old_task = None
            try:
                await w.start()
                with open(log, "ab") as fh:
                    fh.write(LINE1)
                try:
                    await asyncio.wait_for(gate.wait(), timeout=5.0)
                    entered = True
                except asyncio.TimeoutError:
                    entered = False
                check("前置：日志行被消费、回调进入（准备吞取消）", entered)
                if not entered:
                    return

                old_task = w._task
                await w.stop()                   # 超时 → 孤儿
                base_pos, base_read = w._pos, w.read_bytes
                check("前置：第一行已被消费（基准就位）",
                      base_pos == len(LINE1) and base_read == len(LINE1),
                      f"pos={base_pos} read={base_read}")

                # 复刻「新的 start() 已把 _running 置回 True，而旧任务还没到检查点」
                # 的窗口：不真起新任务（那会顺带把第二行合法消费掉），只摆出条件。
                w._running = True
                with open(log, "ab") as fh:
                    fh.write(LINE2)
                await asyncio.sleep(1.2)         # 给「复活的旧任务」足够时间去犯错
                check("★旧任务不得复活：不吃第二行（旧写法这里会再次读文件 / 派发事件）",
                      len(calls) == 1 and w._pos == base_pos and w.read_bytes == base_read,
                      f"calls={len(calls)} pos={w._pos} read={w.read_bytes} "
                      f"(base pos={base_pos} read={base_read})")
                check("旧任务已在代际检查点退出", old_task.done(), "done=False")
            finally:
                LW.STOP_TIMEOUT = old_to
                w._running = False
                if old_task is not None and not old_task.done():
                    old_task.cancel()
                    await asyncio.wait({old_task}, timeout=1.0)
                await w.stop()

    asyncio.run(scenario())


# ==================== G 对照：常规 stop 依然干净 ====================
def part_g3_control_normal_stop() -> None:
    print("========== [G] 对照：常规 stop 依然干净（不进孤儿名单） ==========")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, seen = _make_watcher(root)
            await w.start()
            task = w._task
            await w.stop()
            check("对照：任务按时退出、被取消、不进孤儿名单、_running 归 False",
                  task.done() and task.cancelled()
                  and task not in getattr(w, "_orphaned", set()) and not w._running,
                  f"done={task.done()} cancelled={task.cancelled()}")

    asyncio.run(scenario())


# ==================== G 对照：三轮复用 + 任务代单调 ====================
def part_g4_control_reuse_and_gen_monotonic() -> None:
    print("========== [G] 对照：同对象复用 3 轮 + 任务代单调递增 ==========")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, seen = _make_watcher(root)
            gens: list = []
            ok = True
            for _ in range(3):
                await w.start()
                gens.append(getattr(w, "_task_gen", None))
                ok = ok and bool(w._running) and w._task is not None and not w._task.done()
                await w.stop()
                ok = ok and (not w._running) and (w._task is None)
            check("对照：连续 3 轮 start/stop 均正常收口", ok)
            check("★任务代只增不减（三轮各不同、单调递增）",
                  all(isinstance(g, int) for g in gens)
                  and gens == sorted(gens) and len(set(gens)) == 3,
                  str(gens))
            hg = w.health().get("task_gen")
            check("health() 摊出任务代（整数、与内部字段一致）",
                  isinstance(hg, int) and hg == getattr(w, "_task_gen", None),
                  f"health={hg!r}")

    asyncio.run(scenario())


def main() -> int:
    print("=" * 72)
    print("v0.23.5 第八轮回归（GPT 第七轮核验 · G 组：监听任务代际隔离）")
    print("=" * 72)
    part_g_static()
    part_g1_restart_after_swallowed_cancel()
    part_g2_orphan_must_not_resume_polling()
    part_g3_control_normal_stop()
    part_g4_control_reuse_and_gen_monotonic()
    print("=" * 72)
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for f in _fail:
            print(f"  - {f}")
        return 1
    print("全部通过：旧代任务即使吞掉取消也无法复活（新旧并发被代际检查点封死）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
