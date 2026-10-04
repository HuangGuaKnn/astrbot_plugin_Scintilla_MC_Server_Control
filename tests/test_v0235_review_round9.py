# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第九轮）回归：生命周期锁的 owner loop / 全部恢复点验代 / 记账保护。

GPT 第八轮核验在 3b14caa + 第七、八轮修复的工作区上复查后，确认第八轮主要目标
（监听任务代际隔离）已经通过，并指出两处仍需修复的 P2 边界（另有一条低优先级登记）：

  H. **跨事件循环或跨线程调用 `start()` / `stop()` 可能死锁**。生命周期锁靠
     `asyncio.Lock._loop` 私有属性判断「有没有绑在别的事件循环」——锁还没经历
     过竞争时该属性可能迟迟不绑定（首个持有者直接 acquire、没有等待者）；B 线程
     `asyncio.run(w.stop())` 会复用到同一把锁并等待，A 释放锁时跨线程唤醒 B 循环
     上的等待 Future 不保证送达，B 线程持续卡住。另外「检测到不同循环就静默换锁」
     本身也让互斥悄悄失效。
  I. **任务代际检查没有覆盖 `_poll()` 的所有异步恢复点**。`exists()` / `stat()` /
     锚点读取 / 文件头读取 / 短文件快照 / 编码探测这些 IO `await` 返回后没有验代：
     旧任务恰在等待期间被停止并换代，恢复后仍会写 `file_present` / `missing_polls` /
     `_last_size` / `_sig` / `_head` / `_short_sig` / `_enc` 等状态；`_loop()` 还会
     在 `_poll` 返回后把（届时已属于新代的）错误计数清零、在异常分支给新代记账。

修复判据（本文件，先红后绿）：
  - 静态 H：`_life_lock` 不再读 `asyncio.Lock._loop` 私有属性；显式记录
    `_owner_loop`；owner 已关闭 / 已停转 → 接管换锁；owner 活着却换循环 →
    明确 `RuntimeError`（消息给出 `run_coroutine_threadsafe` 转发办法）；
    `_stop_locked` 对「任务属于别的循环」直接安静收尾（不跨循环 wait/cancel）。
  - 行为 H：① 后台线程跑着 owner loop 时，另一线程 `asyncio.run(w.stop())` 必须
    在有限时间内抛明确 `RuntimeError`（既不死锁、也不静默执行，watcher 状态不动）；
    ② `run_coroutine_threadsafe(w.stop(), owner_loop)` 转发可用（正确做法）；
    ③ owner 循环已关闭（反复 asyncio.run / 热重载形态）→ 新循环正常接管，且不再
    走「停止异常（Event loop is closed）」的脏路径；④ 同循环内多轮 start/stop
    正常、锁对象复用、owner 记录为当前循环。
  - 静态 I：`_poll` 的每个 IO / 派发恢复点之后、下一个恢复点之前都有
    `self._stale(gen)`（无裸恢复点）；入口检查仍在；`_loop` 在 `_poll` 返回后与
    异常分支里都先验代、再碰 `error_count` / `last_error`。
  - 行为 I：逐窗复刻「IO 等待期间换代」——exists / stat / 锚点 / 头指纹 /
    短文件快照 / 编码探测六处恢复后，对应状态字段一个都不许写；`_loop` 的
    「换代后不清零」「换代后不记异常」两条记账保护；附同代对照（功能未被改坏）。

低优先级边界（本轮不处理、登记备查）：`on_event` 若永久吞掉取消且永不返回，
任务代也无法强行终止它 —— 保证进程级退出需要给回调加超时或独立任务隔离，
那会改变「逐条等待回调完成」的既有语义，另行评估。

运行：
  <AstrBot python> tests\\test_v0235_review_round9.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402

LW_SRC = io.open(PLUGIN_DIR / "core" / "log_watcher.py", encoding="utf-8").read()

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


class _RecLogger:
    """日志替身：把 `logger.xxx(...)` 的模板记进 lines（round4 同款手法）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __getattr__(self, _name):
        def _rec(*args, **kwargs):
            if args:
                self.lines.append(str(args[0]))
        return _rec


def _make_watcher(root, interval=0.05):
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    return LW.LogWatcher(str(root), on_event, poll_interval=interval), seen


def _prepare_log(root: Path) -> Path:
    (root / "logs").mkdir()
    log = root / "logs" / "latest.log"
    log.write_bytes(b"")
    return log


async def _window_run(w, window_n: int, stale_gen: int = 41, new_gen: int = 42) -> bool:
    """让下一次 `_poll(stale_gen)` 在第 `window_n` 笔 IO 上挂住；挂住期间把任务代
    拨到 `new_gen`（复刻「等待期间 stop() 换代」），再放行。

    返回「窗口是否按预期到达并放行」——False 表示前置失败（用例会显式报红）。
    替换的是实例属性 `_io_wait`（对象一次性使用，不恢复）。
    """
    entered, gate = asyncio.Event(), asyncio.Event()
    orig = w._io_wait
    calls = {"n": 0}

    async def slow(fn, *a, **k):
        calls["n"] += 1
        if calls["n"] == window_n:
            entered.set()
            await gate.wait()
        return await orig(fn, *a, **k)

    w._io_wait = slow
    w._task_gen = stale_gen
    task = asyncio.create_task(w._poll(stale_gen))
    reached = True
    try:
        await asyncio.wait_for(entered.wait(), 3.0)
    except asyncio.TimeoutError:
        reached = False
    else:
        w._task_gen = new_gen               # 模拟：等待期间发生换代
    gate.set()
    try:
        await asyncio.wait_for(task, 3.0)
    except asyncio.TimeoutError:
        task.cancel()
        reached = False
    return reached


# ==================== H 静态面：owner loop 显式记录 ====================
def part_h_static() -> None:
    print("========== [H] 生命周期锁：owner loop 显式记录（静态面） ==========")
    life_src = fn_src(LW_SRC, "_life_lock", cls="LogWatcher")
    stop_src = fn_src(LW_SRC, "_stop_locked", cls="LogWatcher")
    check("静态：不再读 asyncio.Lock._loop 私有属性（旧探测整个移除）",
          'getattr(lk, "_loop"' not in LW_SRC)
    check("静态：显式记录 owner loop（__init__ 字段 + _life_lock 里读取）",
          "self._owner_loop: asyncio.AbstractEventLoop | None = None" in LW_SRC
          and "owner = self._owner_loop" in life_src)
    check("静态：owner 已关闭 / 已停转 → 接管换锁（is_closed / is_running）",
          "owner.is_closed()" in life_src and "owner.is_running()" in life_src)
    check("静态：owner 活着却换循环 → 明确 RuntimeError（附转发指引）",
          "raise RuntimeError" in life_src and "run_coroutine_threadsafe" in life_src)
    check("静态：_stop_locked 对「任务属于别的循环」安静收尾（不跨循环 wait/cancel）",
          "task.get_loop() is not asyncio.get_running_loop()" in stop_src)


# ==================== H 行为 1：跨活动循环 → 明确拒绝 ====================
def part_h1_reject_cross_loop() -> None:
    print("========== [H] 行为 1：跨活动循环调用 → 明确 RuntimeError（不死锁、不静默执行） ==========")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _prepare_log(root)
        w, _seen = _make_watcher(root)
        loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                loop.close()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        started_ok, err = True, None
        try:
            fut = asyncio.run_coroutine_threadsafe(w.start(), loop)
            fut.result(timeout=5)
        except BaseException as e:               # noqa: BLE001
            started_ok, err = False, e
        check("前置：owner loop（后台线程）里 start 成功", started_ok, repr(err))
        if not started_ok:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=3)
            return
        time.sleep(0.1)                          # 让 loop 平稳跑到 selector 阻塞

        box: dict = {}

        def cross_call():
            try:
                asyncio.run(w.stop())
                box["r"] = "returned"
            except BaseException as e:           # noqa: BLE001
                box["r"] = e

        th = threading.Thread(target=cross_call, daemon=True)
        th.start()
        th.join(6.0)
        alive, r = th.is_alive(), box.get("r")
        check("★跨活动循环调用 stop()：明确抛 RuntimeError（修复前：静默执行 / 可能挂死）",
              (not alive) and isinstance(r, RuntimeError),
              f"alive={alive} result={r!r}")
        check("异常信息给出正确做法（run_coroutine_threadsafe 转发到 owner loop）",
              isinstance(r, RuntimeError) and "run_coroutine_threadsafe" in str(r),
              str(r))
        check("★watcher 状态未被跨循环调用动过（被拒绝 ≠ 静默执行）",
              w._running is True, f"running={w._running}")

        fwd_ok, ferr = True, None
        try:
            f2 = asyncio.run_coroutine_threadsafe(w.stop(), loop)
            f2.result(timeout=5)
        except BaseException as e:               # noqa: BLE001
            fwd_ok, ferr = False, e
        check("★转发路径可用：run_coroutine_threadsafe(w.stop(), owner_loop) 正常停止",
              fwd_ok and w._running is False, f"ok={fwd_ok} err={ferr!r}")
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        check("测试线程收尾干净（owner loop 已停、线程已退）", not t.is_alive())


# ==================== H 行为 2：owner 已关闭 → 接管（干净路径） ====================
def part_h2_takeover_after_closed() -> None:
    print("========== [H] 行为 2：owner 循环已关闭 → 新循环接管（不再走「停止异常」脏路径） ==========")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _prepare_log(root)
        w, _seen = _make_watcher(root)

        asyncio.run(w.start())                   # loop1：run 结束即关闭（任务被收尾取消）
        check("前置：start 后 running；任务会被 asyncio.run 收尾取消",
              w._running is True and w._task is not None and w._task.done(),
              f"done={None if w._task is None else w._task.done()}")

        rec = _RecLogger()
        old_logger, old_to = LW.logger, LW.STOP_TIMEOUT
        LW.logger = rec
        LW.STOP_TIMEOUT = 0.3
        err = None
        try:
            try:
                asyncio.run(w.stop())            # loop2：owner 已关闭 → 接管
            except BaseException as e:           # noqa: BLE001
                err = e
        finally:
            LW.logger = old_logger
            LW.STOP_TIMEOUT = old_to
        check("★owner 循环关闭后：新循环里 stop() 正常返回（接管，不抛）", err is None, repr(err))
        check("收尾干净：running=False、孤儿名单为空",
              w._running is False and len(w._orphaned) == 0)
        check("★接管收尾不再走「停止异常（Event loop is closed）」脏路径",
              not any("停止时异常" in s for s in rec.lines), str(rec.lines))


# ==================== H 行为 3：同循环复用 ====================
def part_h3_same_loop_reuse() -> None:
    print("========== [H] 行为 3：同循环复用（锁对象不换、owner 记录正确） ==========")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, _seen = _make_watcher(root)
            ok, err, lk1 = True, None, None
            try:
                await w.start()
                lk1 = w._life_lk
                await w.stop()
                for _ in range(2):
                    await w.start()
                    await w.stop()
            except BaseException as e:           # noqa: BLE001
                ok, err = False, e
            check("对照：同循环连续 3 轮 start/stop 正常收口", ok, repr(err))
            check("owner 记录为当前事件循环",
                  getattr(w, "_owner_loop", None) is asyncio.get_running_loop())
            check("同循环内锁对象保持同一把（接管只发生在换循环时）",
                  w._life_lk is not None and w._life_lk is lk1)

    asyncio.run(scenario())


# ==================== I 静态面：无裸恢复点 ====================
def part_i_static() -> None:
    print("========== [I] 代际检查全覆盖（静态面）：不得有裸恢复点 ==========")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    loop_src = fn_src(LW_SRC, "_loop", cls="LogWatcher")

    markers = [m.start() for m in re.finditer(
        r"await self\._io_wait|await self\.on_event", poll_src)]
    bare = []
    for a, b in zip(markers, markers[1:]):
        if "self._stale(gen)" not in poll_src[a:b]:
            bare.append(poll_src[a:a + 46].replace("\n", " ").strip())
    check("静态：每个 IO / 派发恢复点之后、下一个恢复点之前都有代际检查（无裸恢复点）",
          not bare, f"裸恢复点 {len(bare)} 处：{bare[:3]}")
    check("静态：恢复点数量符合预期（IO 9 处 + 事件派发 1 处）",
          len(markers) >= 10, f"markers={len(markers)}")
    check("静态：入口处先验代（进 _poll 的第一道闸仍在）",
          bool(markers) and "self._stale(gen)" in poll_src[: markers[0]])

    i_poll = loop_src.find("await self._poll(gen)")
    i_stale = loop_src.find("self._stale(gen)")
    i_zero = loop_src.find("self.error_count = 0")
    check("静态：_loop 在 _poll 返回后先验代、再碰 error_count（清零之前）",
          -1 not in (i_poll, i_stale, i_zero) and i_poll < i_stale < i_zero,
          f"poll={i_poll} stale={i_stale} zero={i_zero}")
    i_exc = loop_src.find("except Exception as e:")
    i_s2 = loop_src.find("self._stale(gen)", i_exc) if i_exc != -1 else -1
    i_cnt = loop_src.find("self.error_count += 1", i_exc) if i_exc != -1 else -1
    check("静态：_loop 异常分支先验代、再记账（error_count += 1 之前）",
          -1 not in (i_exc, i_s2, i_cnt) and i_exc < i_s2 < i_cnt,
          f"exc={i_exc} stale={i_s2} count={i_cnt}")


# ==================== I 行为：六个恢复点窗口 ====================
def part_i1_window_exists() -> None:
    print("---- [I1] 恢复点窗口：exists（换代后不许写 file_present / missing_polls） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)                   # 文件在（但旧代不许写结论）
            w, _seen = _make_watcher(root)
            reached = await _window_run(w, window_n=1)
            check("前置：exists 窗口到达（IO 挂住期间换代）", reached)
            if not reached:
                return
            check("★恢复点拒绝：file_present / missing_polls 一个都没写",
                  w.file_present is False and w.missing_polls == 0,
                  f"present={w.file_present} missing={w.missing_polls}")

    asyncio.run(scenario())


def part_i2_window_stat() -> None:
    print("---- [I2] 恢复点窗口：stat（换代后不许写 _last_size / _sig） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            reached = await _window_run(w, window_n=2)
            check("前置：stat 窗口到达", reached)
            if not reached:
                return
            check("★恢复点拒绝：_last_size / _sig 未被写（旧代的结果不进新代）",
                  w._last_size == 0 and w._sig is None,
                  f"_last_size={w._last_size} _sig={w._sig!r}")

    asyncio.run(scenario())


def part_i3_window_anchor() -> None:
    print("---- [I3] 恢复点窗口：锚点回读（换代后不许写 _anchor / 误判轮转） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"q" * 400)
            w, _seen = _make_watcher(root)
            w._sig = None
            w._pos = 100 + LW.ANCHOR_BYTES       # 让 anchor_ok 成立（_pos == at + len）
            w._anchor = b"A" * LW.ANCHOR_BYTES
            w._anchor_at = 100
            w._head = ""                          # 跳过别的判据，只留锚点回读
            reached = await _window_run(w, window_n=3)
            check("前置：锚点回读窗口到达", reached)
            if not reached:
                return
            check("★恢复点拒绝：锚点未被写（也不触发误判轮转）",
                  w._anchor == b"A" * LW.ANCHOR_BYTES and w._anchor_at == 100,
                  f"anchor={w._anchor[:8]!r}… at={w._anchor_at}")

    asyncio.run(scenario())


def part_i4_window_head() -> None:
    print("---- [I4] 恢复点窗口：头指纹回读（换代后不许写 _head） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"z" * 400)
            w, _seen = _make_watcher(root)
            w._sig = None
            w._pos = 400
            w._anchor, w._anchor_at = b"", -1
            w._head = "zz"                        # 非空 → 走头指纹回读
            reached = await _window_run(w, window_n=3)
            check("前置：头指纹回读窗口到达", reached)
            if not reached:
                return
            check("★恢复点拒绝：_head 保持旧值（旧代的指纹不进新代）",
                  w._head == "zz", f"_head={w._head!r}")

    asyncio.run(scenario())


def part_i5_window_short_snapshot() -> None:
    print("---- [I5] 恢复点窗口：短文件快照（换代后不许写 _short_sig / _short_len） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"b" * 100)           # < HEAD_BYTES，短路走快照分支
            w, _seen = _make_watcher(root)
            w._sig = None
            w._pos = 100
            w._anchor, w._anchor_at = b"", -1
            w._head = ""
            w._short_sig = "PRE"
            w._short_len = 3
            reached = await _window_run(w, window_n=3)
            check("前置：短文件快照回读窗口到达", reached)
            if not reached:
                return
            check("★恢复点拒绝：快照未被写（_short_sig / _short_len 保持）",
                  w._short_sig == "PRE" and w._short_len == 3,
                  f"sig={w._short_sig!r} len={w._short_len}")

    asyncio.run(scenario())


def part_i6_window_encoding() -> None:
    print("---- [I6] 恢复点窗口：编码探测（换代后不许写 _enc） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"e" * 400)
            w, _seen = _make_watcher(root)
            w._sig = None
            w._pos = 10 ** 9                      # size < _pos → 直接判轮转，通向编码探测
            w._anchor, w._anchor_at = b"", -1
            w._head = ""
            w._short_len = 0
            w._enc = "KEEP-OLD"
            reached = await _window_run(w, window_n=3)
            check("前置：编码探测窗口到达", reached)
            if not reached:
                return
            check("★恢复点拒绝：_enc 保持旧值（探测结果属于旧代，丢弃）",
                  w._enc == "KEEP-OLD", f"_enc={w._enc!r}")

    asyncio.run(scenario())


# ==================== I 行为：_loop 记账保护 ====================
def part_i7_loop_guard() -> None:
    print("---- [I7] _loop 记账保护：换代后不清零 / 不记新异常；同代对照照常清零 ----")

    async def scenario_clear():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, _seen = _make_watcher(root, interval=0.01)
            w._running = True
            w._task_gen = 3
            w.error_count = 5

            async def poll_then_bump(gen=None):
                w._task_gen = 4                   # 复刻：poll 返回瞬间发生换代
            w._poll = poll_then_bump
            await asyncio.wait_for(w._loop(3), 3.0)
            check("★换代后旧任务不许清零（error_count 保持 5 —— 旧写法会抹成 0）",
                  w.error_count == 5, f"error_count={w.error_count}")

    asyncio.run(scenario_clear())

    async def scenario_exc():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, _seen = _make_watcher(root, interval=0.01)
            w._running = True
            w._task_gen = 7
            w.error_count = 0
            w.last_error = ""

            async def boom_poll(gen=None):
                w._task_gen = 8                   # 换代之后才炸
                raise RuntimeError("模拟轮询炸")
            w._poll = boom_poll
            await asyncio.wait_for(w._loop(7), 3.0)
            check("★换代后旧任务的异常不记进新代的账（error_count / last_error 不动）",
                  w.error_count == 0 and w.last_error == "",
                  f"error_count={w.error_count} last_error={w.last_error!r}")

    asyncio.run(scenario_exc())

    async def scenario_control():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_log(root)
            w, _seen = _make_watcher(root, interval=0.01)
            w._running = True
            w._task_gen = 9
            w.error_count = 4

            async def ok_poll(gen=None):
                pass                              # 同代、不新增错误
            w._poll = ok_poll
            try:
                await asyncio.wait_for(w._loop(9), 0.4)
            except asyncio.TimeoutError:
                pass                              # 同代循环持续在跑，由 wait_for 收掉
            w._running = False
            check("对照：同一代内平静返回 → 计数照常清零（功能没被改坏）",
                  w.error_count == 0, f"error_count={w.error_count}")

    asyncio.run(scenario_control())


def main() -> None:
    print("=" * 72)
    print("v0.23.5 第九轮回归（GPT 第八轮核验 · H/I：生命周期锁跨循环 / 恢复点验代全覆盖）")
    print("=" * 72)
    part_h_static()
    part_h1_reject_cross_loop()
    part_h2_takeover_after_closed()
    part_h3_same_loop_reuse()
    part_i_static()
    part_i1_window_exists()
    part_i2_window_stat()
    part_i3_window_anchor()
    part_i4_window_head()
    part_i5_window_short_snapshot()
    part_i6_window_encoding()
    part_i7_loop_guard()
    print("=" * 72)
    if _fail:
        print(f"通过 {_pass} 项，失败 {len(_fail)} 项：")
        for name in _fail:
            print(f"  [FAIL] {name}")
        sys.exit(1)
    print(f"通过 {_pass} 项，失败 0 项")
    print("全部通过：生命周期锁跨循环有明确归宿（拒绝 / 接管 / 转发），"
          "恢复点先验代、记账不越权")
    sys.exit(0)


if __name__ == "__main__":
    main()
