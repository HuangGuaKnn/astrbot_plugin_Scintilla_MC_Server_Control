# -*- coding: utf-8 -*-
"""v0.23.5 第十轮回归（GPT 第九轮核验 · J/K/L：三处「旧代迟到记账」边界的代际保护）。

针对 GPT 第九轮复核单里三个仍未收口的边界：
  - J：`_poll()` 最终 read 的 `except OSError` 分支没有先验代 —— 旧代任务在等待
    期间被换代后，迟到的 OSError 仍会把错误记进新代的 `error_count` / `last_error`；
  - K：`_io_wait()` 自己的超时记账（`io_timeouts` / `last_error`）没有代际保护 ——
    旧代 IO 在新代启动后才超时，仍会给当前 watcher 记一笔；
  - L：owner loop「停止但未关闭」会被 `_life_lock()` 当成可接管 —— 旧循环里挂起的
    生命周期调用恢复后仍可能写状态 / 创建旧任务（第十轮收紧：只有 `is_closed()`
    才自动接管，停转未关闭一律拒绝）。

修复判据（本文件，先红后绿）：
  - 静态 J：最终 read 的 `except OSError` 分支里，`_stale(gen)` 在记账之前（正则钉形状）；
  - 静态 K：`_io_wait` 签名带 `gen`；超时分支记账前先验代；函数内共享记账全过守卫；
  - 静态 L：`is_closed` 为唯一自动接管条件、`not owner.is_running` 退出判定、
    拒绝文案带「尚未关闭」与转发指引；
  - 行为 J1：最终 read 被 gate 挂住 → 换代 → 放行抛 OSError → 一个字段都不写；
    对照 J2：同代 OSError 照常记账（功能没被改坏）。
  - 行为 K1：锚点回读进入等待 → 超时兑现前换代 → `io_timeouts` / `last_error` 不动；
    对照 K2：同代超时照常记账。
  - 行为 L1：loop1 停转未关闭，跨循环 stop() / start() 均被 RuntimeError 拒绝、
    watcher 状态与锁对象一律不动；对照 L2：owner 自己的循环内 stop 照常可用。

运行：<python> tests/test_v0235_review_round10.py
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
    """取某个函数 / 方法的源码片段（ast 定位；round9 同款）。"""
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


def _prepare_log(root: Path) -> Path:
    (root / "logs").mkdir()
    log = root / "logs" / "latest.log"
    log.write_bytes(b"")
    return log


# ==================== J：最终 read 的 OSError 换代保护 ====================
def part_j_static() -> None:
    print("========== [J] 最终 read 的 OSError：静态面 ==========")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    idx = poll_src.find("读取日志新增内容")
    seg = poll_src[idx: idx + 1000] if idx != -1 else ""
    shape = re.search(
        r"except OSError as e:\n(?:[ \t]*#[^\n]*\n)*[ \t]*if self\._stale\(gen\):\n[ \t]*return",
        seg)
    check("静态：最终 read 的 except OSError 分支先验代（验代 → return 在记账之前）",
          idx != -1 and shape is not None, f"idx={idx} seg_len={len(seg)}")


async def _final_read_scenario_async(regen: bool):
    """复刻「最终 read 被 gate 挂住」：文件 400 字节、干净前置，第 4 笔 IO = 最终 read。

    `regen=True`：挂住期间把任务代拨到 2（复刻 stop()+start() 换代）；
    `regen=False`：不换代（同代对照）。
    返回 (w, reached, poll_err)。
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    log = _prepare_log(root)
    log.write_bytes(b"h" * 400)
    w, _seen = _make_watcher(root)
    w._pos = 0
    w._sig = None
    w._anchor, w._anchor_at = b"", -1
    w._head = ""
    w._short_sig, w._short_len = "", 0

    def late_boom(offset, size):
        raise OSError("late-read")

    w._read_at = late_boom

    entered, gate = asyncio.Event(), asyncio.Event()
    orig = w._io_wait
    calls = {"n": 0}

    async def slow(fn, *a, **k):
        calls["n"] += 1
        if calls["n"] == 4:                  # 第 4 笔 = 最终 read（exists/stat/头指纹之后）
            entered.set()
            await gate.wait()
        return await orig(fn, *a, **k)

    w._io_wait = slow
    w._task_gen = 1
    task = asyncio.create_task(w._poll(1))
    reached, poll_err = True, None
    try:
        await asyncio.wait_for(entered.wait(), 3.0)
    except asyncio.TimeoutError:
        reached = False
    else:
        if regen:
            w._task_gen = 2                  # 复刻：等待期间 stop()+start() 换代
    gate.set()
    try:
        await asyncio.wait_for(task, 3.0)
    except asyncio.TimeoutError:
        task.cancel()
        reached = False
    except BaseException as e:               # noqa: BLE001
        poll_err = e
    td.cleanup()
    return w, reached, poll_err


def part_j_behavior() -> None:
    print("---- [J1] 行为：最终 read 挂住 → 换代 → 放行抛 OSError（不许记账） ----")
    w, reached, poll_err = asyncio.run(_final_read_scenario_async(regen=True))
    check("前置：最终 read 窗口到达（挂住期间换代）", reached, repr(poll_err))
    if reached:
        check("★换代后最终 read 抛 OSError：error_count 不被写（修复前现场 =1）",
              w.error_count == 0, f"error_count={w.error_count}")
        check("★last_error 不被写（修复前现场 'OSError: late-read'）",
              w.last_error == "", repr(w.last_error))

    print("---- [J2] 对照：同代最终 read 抛 OSError → 照常记账 ----")
    w2, reached2, poll_err2 = asyncio.run(_final_read_scenario_async(regen=False))
    check("前置：最终 read 窗口到达（同代）", reached2, repr(poll_err2))
    if reached2:
        check("对照：同代 OSError 照常记账（error_count=1 + last_error 写明）",
              w2.error_count == 1 and w2.last_error == "OSError: late-read",
              f"error_count={w2.error_count} last_error={w2.last_error!r}")


# ==================== K：_io_wait 超时记账的代际保护 ====================
def part_k_static() -> None:
    print("========== [K] _io_wait 超时记账：静态面 ==========")
    iw = fn_src(LW_SRC, "_io_wait", cls="LogWatcher")
    check("静态：_io_wait 签名带 gen 参数（结果归属声明）",
          'gen: "int | None"' in iw[:400], iw[:160].replace("\n", "\\n"))
    ti = iw.find("except asyncio.TimeoutError")
    seg = iw[ti: ti + 900] if ti != -1 else ""
    i_stale = seg.find("self._stale(gen)")
    i_to = seg.find("self.io_timeouts += 1")
    check("静态：超时分支记账前先验代（io_timeouts 之前有 _stale(gen)）",
          ti != -1 and i_stale != -1 and i_to != -1 and i_stale < i_to,
          f"ti={ti} stale@{i_stale} to@{i_to}")
    check("静态：_io_wait 内共享记账全过代际守卫（_stale(gen) ≥ 3 处）",
          iw.count("self._stale(gen)") >= 3, str(iw.count("self._stale(gen)")))


async def _timeout_scenario_async(regen: bool):
    """复刻「锚点回读进入等待 → 超时兑现前换代」：文件 400 字节、锚点判据成立。

    慢函数只对锚点回读（size == ANCHOR_BYTES）生效 —— 同代对照场景里后续的
    头指纹补读 / 最终 read 保持即时，避免叠出第二笔超时干扰计数。
    返回 (w, reached, poll_err)。
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    log = _prepare_log(root)
    log.write_bytes(b"q" * 400)
    w, _seen = _make_watcher(root)
    w._sig = None
    w._pos = 100 + LW.ANCHOR_BYTES           # 让锚点判据成立（_pos == at + len）
    w._anchor = b"A" * LW.ANCHOR_BYTES
    w._anchor_at = 100
    w._head = ""
    w._short_sig, w._short_len = "", 0

    def slow_read(offset, size):
        if size == LW.ANCHOR_BYTES:           # 只慢锚点回读这一笔
            time.sleep(0.3)
        return b"A" * size
    w._read_at = slow_read

    hit_evt = asyncio.Event()
    orig = w._io_wait

    async def spy(fn, *a, **k):
        if k.get("what") == "回读内容锚点":
            hit_evt.set()
        return await orig(fn, *a, **k)

    w._io_wait = spy
    w._task_gen = 1
    old_to = LW.IO_TIMEOUT
    LW.IO_TIMEOUT = 0.15
    task = asyncio.create_task(w._poll(1))
    reached, poll_err = True, None
    try:
        await asyncio.wait_for(hit_evt.wait(), 2.0)   # 确认锚点窗口已进入等待
        if regen:
            w._task_gen = 2                  # 复刻：超时兑现前换代
        try:
            await asyncio.wait_for(task, 3.0)
        except asyncio.TimeoutError:
            task.cancel()
            reached = False
        except BaseException as e:           # noqa: BLE001
            poll_err = e
    except asyncio.TimeoutError:
        reached = False
    finally:
        LW.IO_TIMEOUT = old_to
    td.cleanup()
    return w, reached, poll_err


def part_k_behavior() -> None:
    print("---- [K1] 行为：锚点回读超时（等待期间换代）→ 不记账 ----")
    w, reached, poll_err = asyncio.run(_timeout_scenario_async(regen=True))
    check("前置：锚点回读窗口进入等待（超时兑现前换代）", reached, repr(poll_err))
    if reached:
        check("★换代后超时不记账：io_timeouts == 0（修复前现场 =1）",
              w.io_timeouts == 0, f"io_timeouts={w.io_timeouts}")
        check("★last_error 不被写（修复前现场 '文件 IO 超时（回读内容锚点 > 0.15s）'）",
              w.last_error == "", repr(w.last_error))

    print("---- [K2] 对照：同代超时 → 照常记账 ----")
    w2, reached2, poll_err2 = asyncio.run(_timeout_scenario_async(regen=False))
    check("前置：锚点回读窗口进入等待（同代）", reached2, repr(poll_err2))
    if reached2:
        check("对照：同代超时照常记账（io_timeouts=1 + last_error 写明）",
              w2.io_timeouts == 1 and "超时" in w2.last_error,
              f"io_timeouts={w2.io_timeouts} last_error={w2.last_error!r}")


# ==================== L：owner loop 停转未关闭 → 拒绝接管 ====================
def part_l_static() -> None:
    print("========== [L] owner loop 停转未关闭：静态面 ==========")
    life_src = fn_src(LW_SRC, "_life_lock", cls="LogWatcher")
    check("静态：接管只认 is_closed；停转未关闭不再静默接管（not owner.is_running 退出判定）",
          "owner.is_closed()" in life_src and "not owner.is_running" not in life_src)
    check("静态：拒绝文案说明「尚未关闭也不许接管」+ 转发 / 关闭指引",
          "尚未关闭" in life_src and "run_coroutine_threadsafe" in life_src
          and "loop.close()" in life_src, life_src[-400:].replace("\n", "\\n"))


def part_l_behavior() -> None:
    print("========== [L] 行为：停转未关闭的 owner loop 不许被接管 ==========")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _prepare_log(root)
        w, _seen = _make_watcher(root)
        loop1 = asyncio.new_event_loop()
        ok, err = True, None
        try:
            loop1.run_until_complete(w.start())
        except BaseException as e:           # noqa: BLE001
            ok, err = False, e
        check("前置：loop1（停转未关闭）上 start 成功", ok and w._running is True, repr(err))
        if not ok:
            loop1.close()
            return
        lk_before = w._life_lk
        check("前置：owner 记录为 loop1，且它未关闭、已停转",
              w._owner_loop is loop1 and (not loop1.is_closed())
              and (not loop1.is_running()))

        box: dict = {}

        def cross_stop():
            try:
                asyncio.run(w.stop())
                box["stop"] = "returned"
            except BaseException as e:       # noqa: BLE001
                box["stop"] = e

        th = threading.Thread(target=cross_stop, daemon=True)
        th.start()
        th.join(6.0)
        r = box.get("stop")
        check("★停转未关闭：跨循环 stop() 被明确拒绝（修复前现场：静默接管并执行）",
              (not th.is_alive()) and isinstance(r, RuntimeError),
              f"alive={th.is_alive()} r={r!r}")
        check("★watcher 状态未被动（running 仍 True、锁对象未换、owner 仍 loop1）",
              w._running is True and w._life_lk is lk_before and w._owner_loop is loop1,
              f"running={w._running} lk_same={w._life_lk is lk_before}")
        check("拒绝文案仍给转发指引（run_coroutine_threadsafe）",
              isinstance(r, RuntimeError) and "run_coroutine_threadsafe" in str(r), str(r))

        box2: dict = {}

        def cross_start():
            try:
                asyncio.run(w.start())
                box2["start"] = "returned"
            except BaseException as e:       # noqa: BLE001
                box2["start"] = e

        th2 = threading.Thread(target=cross_start, daemon=True)
        th2.start()
        th2.join(6.0)
        r2 = box2.get("start")
        check("★start() 同样被拒（同一条生命周期锁路径；修复前现场：静默接管并建新任务）",
              (not th2.is_alive()) and isinstance(r2, RuntimeError), f"r2={r2!r}")

        # 收尾：owner 自己的循环内调用照常可用（同循环复用那把锁）
        closed_ok, cerr = True, None
        try:
            loop1.run_until_complete(w.stop())
        except BaseException as e:           # noqa: BLE001
            closed_ok, cerr = False, e
        check("对照：owner 循环（自己）内 stop 照常可用、收尾干净",
              closed_ok and w._running is False, repr(cerr))
        loop1.close()


def main() -> None:
    print("=" * 72)
    print("v0.23.5 第十轮回归（GPT 第九轮核验 · J/K/L：旧代迟到记账的代际保护）")
    print("=" * 72)
    part_j_static()
    part_j_behavior()
    part_k_static()
    part_k_behavior()
    part_l_static()
    part_l_behavior()
    print("=" * 72)
    if _fail:
        print(f"通过 {_pass} 项，失败 {len(_fail)} 项：")
        for name in _fail:
            print(f"  [FAIL] {name}")
        sys.exit(1)
    print(f"通过 {_pass} 项，失败 0 项")
    print("全部通过：最终 read 的 OSError / _io_wait 超时 / owner 停转未关闭 —— "
          "三处边界都过代际守卫")
    sys.exit(0)


if __name__ == "__main__":
    main()
