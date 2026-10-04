# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第十二轮）回归：`_poll()`「文件在 + 缺失清零」提交点的半提交收口。

GPT 第十一轮核验确认第十一轮两处修复（`_prime_state` 零共享写 / executor 关闭分支的
减计数守卫）均通过，另指明一处未被第十一轮覆盖的 P3 竞态：

  K. **「文件在 + 缺失清零」写在 `stat()` 异步等待之前**（`core/log_watcher.py`
     原 802 行处）。换代若发生在 `stat()` 等待期间，这笔已经落账的半个提交不会被
     stat 之后的 stale 检查撤销 —— 旧代阵亡后可能留下 `file_present=True` 但
     `_last_size=0` / `_sig=None` 的不一致组合（第九轮 I2 窗口只钉了 `_last_size` /
     `_sig`，这两个字段漏网）。

修复判据（本文件，先红后绿）：
  - 静态 K：`_poll()` 里「文件在 + 缺失清零」的提交点位于 stat 的 IO `await` 与其后
    代际校验**之后**（旧位置不再有提交语句）；提交点唯一；仍位于 `st is None` 超时
    分支之前（超时轮语义与原实现一致）；无裸恢复点契约保持。
  - 行为 K：① stat 窗口换代（本轮核心）—— `file_present` / `missing_polls` /
    `_last_size` / `_sig` 一个都不许写；② 同场景不换代（对照）—— 提交照常落账，
    功能未被改坏；③ `stat` OSError（同代）—— 只记错误账，不落「文件在」半笔；
    ④ `stat` 超时（同代）——「文件在 + 缺失清零」照常提交（保持既有语义）；
    ⑤ `OSError` + 换代组合 —— 迟到异常一分账都不许记；⑥ 旧代阵亡后新代自愈。

运行：
  <AstrBot python> tests\\test_v0235_review_round12.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import re
import sys
import tempfile
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


async def _stat_window_poll(w, stale_gen=41, new_gen=None):
    """让下一次 `_poll(stale_gen)` 挂在与 stat 对应的第 2 笔 IO 上；挂住期间把任务代
    拨到 `new_gen`（None = 不换代，作对照），再放行。返回 (窗口到达, 任务正常收尾)。
    替换的是实例属性 `_io_wait`（对象一次性使用，不恢复）。
    """
    entered, gate = asyncio.Event(), asyncio.Event()
    orig = w._io_wait
    calls = {"n": 0}

    async def slow(fn, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
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
        if new_gen is not None:
            w._task_gen = new_gen               # 模拟：等待期间发生换代
    gate.set()
    ok = True
    try:
        await asyncio.wait_for(task, 3.0)
    except asyncio.TimeoutError:
        task.cancel()
        ok = False
    except BaseException:                       # noqa: BLE001
        ok = False
    return reached, ok


# ==================== K 静态面：提交点在 stat 代际校验之后 ====================
def part_k_static() -> None:
    print("========== [K] 半提交收口（静态面）：提交点后移到 stat 代际校验之后 ==========")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")

    i_stat = poll_src.find('what="读取日志文件属性"')
    i_stale = poll_src.find("self._stale(gen)", i_stat) if i_stat != -1 else -1
    i_fp = poll_src.find("self.file_present = True", i_stat) if i_stat != -1 else -1
    i_mp = poll_src.find("self.missing_polls = 0", i_stat) if i_stat != -1 else -1
    i_none = poll_src.find("if st is None:", i_stat) if i_stat != -1 else -1
    check("静态：stat IO 与恢复点校验可定位（前置）",
          i_stat != -1 and i_stale != -1,
          f"stat={i_stat} stale={i_stale}")
    if i_stat == -1 or i_stale == -1:
        return

    pre_fp = poll_src[:i_stat].count("self.file_present = True")
    pre_mp = poll_src[:i_stat].count("self.missing_polls = 0")
    check("静态：stat 等待之前不再有「文件在 + 缺失清零」提交（旧半提交点已移除）",
          pre_fp == 0 and pre_mp == 0,
          f"pre file_present={pre_fp} pre missing_polls={pre_mp}")

    check("静态：提交点位于 stat 的代际校验之后（两个字段都过闸）",
          -1 not in (i_fp, i_mp) and i_stat < i_stale < i_fp and i_stale < i_mp,
          f"stat={i_stat} stale={i_stale} fp={i_fp} mp={i_mp}")

    check("静态：提交点唯一（_poll 内 file_present = True / missing_polls = 0 各恰好 1 处）",
          poll_src.count("self.file_present = True") == 1
          and poll_src.count("self.missing_polls = 0") == 1,
          f"fp×{poll_src.count('self.file_present = True')} "
          f"mp×{poll_src.count('self.missing_polls = 0')}")

    check("静态：提交点仍在 `st is None` 超时分支之前（超时轮语义与原实现一致）",
          -1 not in (i_fp, i_mp, i_none) and i_fp < i_none and i_mp < i_none,
          f"fp={i_fp} mp={i_mp} none={i_none}")

    markers = [m.start() for m in re.finditer(
        r"await self\._io_wait|await self\.on_event", poll_src)]
    bare = []
    for a, b in zip(markers, markers[1:]):
        if "self._stale(gen)" not in poll_src[a:b]:
            bare.append(poll_src[a:a + 46].replace("\n", " ").strip())
    check("静态：无裸恢复点契约保持（每个 IO 恢复点后都有代际检查）",
          not bare and len(markers) >= 10,
          f"裸恢复点 {len(bare)} 处：{bare[:3]}")

    check("静态：带 `第十二轮` 对账标记（可 grep）", "第十二轮" in poll_src)


# ==================== K0 基准：普通一轮照常提交 ====================
def part_k0_plain_poll() -> None:
    print("---- [K0] 基准：普通一轮（不换代、不挂 IO、gen=None）照常提交 ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False
            w.missing_polls = 5
            await asyncio.wait_for(w._poll(), 3.0)     # gen=None：不在代际协议内
            check("基准：提交照常（present=True / missing=0）",
                  w.file_present is True and w.missing_polls == 0,
                  f"present={w.file_present} missing={w.missing_polls}")
            check("基准：_last_size / _sig 同步落账（10 / (ino,10)）",
                  w._last_size == 10 and w._sig is not None and w._sig[1] == 10,
                  f"_last_size={w._last_size} _sig={w._sig!r}")

    asyncio.run(scenario())


# ==================== K 行为：stat 窗口换代（本轮核心） ====================
def part_k1_window_stat_swap() -> None:
    print("---- [K1] stat 窗口换代：半提交收口（file_present / missing_polls 不许被旧代写） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False          # 前置：上一轮如实报「不在」
            w.missing_polls = 5             # 前置：文件曾缺 5 轮，回来的确认轮死在 stat 窗口
            reached, ok = await _stat_window_poll(w, stale_gen=41, new_gen=42)
            check("前置：stat 窗口到达且放行（旧代任务收尾）", reached and ok)
            if not reached:
                return
            check("★半提交收口：file_present / missing_polls 保持旧代阵亡前状态",
                  w.file_present is False and w.missing_polls == 5,
                  f"present={w.file_present} missing={w.missing_polls}")
            check("★半提交收口：_last_size / _sig 同样未被写（第九轮 I2 契约复跑）",
                  w._last_size == 0 and w._sig is None,
                  f"_last_size={w._last_size} _sig={w._sig!r}")
            # 新代自愈：旧代不落账 ≠ 功能坏了 —— 新代自己补一轮照常提交
            await asyncio.wait_for(w._poll(42), 3.0)
            check("自愈对照：新代正常一轮照常完成「文件在 + 缺失清零」提交",
                  w.file_present is True and w.missing_polls == 0
                  and w._last_size == 10 and w._sig is not None and w._sig[1] == 10,
                  f"present={w.file_present} missing={w.missing_polls} "
                  f"_last_size={w._last_size} _sig={w._sig!r}")

    asyncio.run(scenario())


def part_k2_control_no_swap() -> None:
    print("---- [K2] 对照：同场景不换代 —— 提交照常落账（功能未被改坏） ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False
            w.missing_polls = 5
            reached, ok = await _stat_window_poll(w, stale_gen=41, new_gen=None)
            check("对照前置：stat 窗口到达、放行后 poll 正常收尾（未换代）", reached and ok)
            if not reached:
                return
            check("对照：提交照常 —— True / 0 / _last_size=10 / _sig=(ino,10) 全落",
                  w.file_present is True and w.missing_polls == 0
                  and w._last_size == 10 and w._sig is not None and w._sig[1] == 10,
                  f"present={w.file_present} missing={w.missing_polls} "
                  f"_last_size={w._last_size} _sig={w._sig!r}")
            real = log.stat()
            real_ino = int(getattr(real, "st_ino", 0) or 0)
            check("对照：_sig 的 ino 与真文件一致",
                  w._sig is not None and w._sig[0] == real_ino,
                  f"sig={w._sig!r} real_ino={real_ino}")

    asyncio.run(scenario())


def part_k3_stat_oserror_same_gen() -> None:
    print("---- [K3] 对照：stat OSError（同代）—— 只记错误账，不落「文件在」半笔 ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False
            w.missing_polls = 5
            orig = w._io_wait
            calls = {"n": 0}

            def boom():
                raise OSError("stat-down")

            async def slow(fn, *a, **k):
                calls["n"] += 1
                if calls["n"] == 2:
                    return await orig(boom, what="读取日志文件属性", gen=41)
                return await orig(fn, *a, **k)

            w._io_wait = slow
            w._task_gen = 41
            await asyncio.wait_for(w._poll(41), 3.0)
            check("OSError 轮：错误照记（error_count=1、last_error 留痕）",
                  w.error_count == 1 and "stat-down" in w.last_error,
                  f"error_count={w.error_count} last_error={w.last_error!r}")
            check("OSError 轮：「文件在 + 缺失清零」不落半笔（维持既有观测）",
                  w.file_present is False and w.missing_polls == 5
                  and w._last_size == 0,
                  f"present={w.file_present} missing={w.missing_polls} "
                  f"_last_size={w._last_size}")

    asyncio.run(scenario())


def part_k4_timeout_same_gen() -> None:
    print("---- [K4] 对照：stat 超时（同代）—— 「文件在 + 缺失清零」照常提交 ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False
            w.missing_polls = 5
            orig = w._io_wait
            calls = {"n": 0}

            def slow_stat():
                time.sleep(0.3)
                return w.log_path.stat()

            async def slow(fn, *a, **k):
                calls["n"] += 1
                if calls["n"] == 2:
                    return await orig(slow_stat, what="读取日志文件属性", gen=41)
                return await orig(fn, *a, **k)

            w._io_wait = slow
            w._task_gen = 41
            old_to = LW.IO_TIMEOUT
            LW.IO_TIMEOUT = 0.05
            try:
                await asyncio.wait_for(w._poll(41), 3.0)
            finally:
                LW.IO_TIMEOUT = old_to
            await asyncio.sleep(0.4)         # 等超时线程自然收尾（不测它，只是不留噪声）
            check("超时轮：错误照记（error_count / io_timeouts）",
                  w.error_count == 1 and w.io_timeouts == 1,
                  f"error_count={w.error_count} io_timeouts={w.io_timeouts}")
            check("超时轮：「文件在 + 缺失清零」照常提交（保持既有语义）",
                  w.file_present is True and w.missing_polls == 0
                  and w._last_size == 0,
                  f"present={w.file_present} missing={w.missing_polls} "
                  f"_last_size={w._last_size}")

    asyncio.run(scenario())


def part_k5_stat_oserror_swap() -> None:
    print("---- [K5] stat OSError + 换代组合：旧代迟到的异常一分账都不许记 ----")

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _prepare_log(root)
            log.write_bytes(b"x" * 10)
            w, _seen = _make_watcher(root)
            w.file_present = False
            w.missing_polls = 5
            entered, gate = asyncio.Event(), asyncio.Event()
            orig = w._io_wait
            calls = {"n": 0}

            def boom():
                raise OSError("stat-late")

            async def slow(fn, *a, **k):
                calls["n"] += 1
                if calls["n"] == 2:
                    entered.set()
                    await gate.wait()
                    return await orig(boom, what="读取日志文件属性", gen=41)
                return await orig(fn, *a, **k)

            w._io_wait = slow
            w._task_gen = 41
            task = asyncio.create_task(w._poll(41))
            reached = True
            try:
                await asyncio.wait_for(entered.wait(), 3.0)
            except asyncio.TimeoutError:
                reached = False
            else:
                w._task_gen = 42            # 等待期间换代
            gate.set()
            ok = True
            try:
                await asyncio.wait_for(task, 3.0)
            except asyncio.TimeoutError:
                task.cancel()
                ok = False
            check("前置：stat 窗口到达且旧代收尾（异常路径）", reached and ok)
            if not reached:
                return
            check("★换代后：迟到 OSError 不记账（error_count / last_error 不动）",
                  w.error_count == 0 and w.last_error == "",
                  f"error_count={w.error_count} last_error={w.last_error!r}")
            check("★换代后：file_present / missing_polls 同样一个都不写",
                  w.file_present is False and w.missing_polls == 5,
                  f"present={w.file_present} missing={w.missing_polls}")

    asyncio.run(scenario())


def main() -> None:
    print("=" * 72)
    print("v0.23.5 第十二轮回归（GPT 第十一轮核验 · K：stat 提交点半提交收口）")
    print("=" * 72)
    part_k_static()
    part_k0_plain_poll()
    part_k1_window_stat_swap()
    part_k2_control_no_swap()
    part_k3_stat_oserror_same_gen()
    part_k4_timeout_same_gen()
    part_k5_stat_oserror_swap()
    print("=" * 72)
    if _fail:
        print(f"通过 {_pass} 项，失败 {len(_fail)} 项：")
        for name in _fail:
            print(f"  [FAIL] {name}")
        sys.exit(1)
    print(f"通过 {_pass} 项，失败 0 项")
    print("全部通过：「文件在 + 缺失清零」提交点归入代际协议 —— "
          "整笔落或一笔不落，stat 窗口换代不再留下半提交。")


if __name__ == "__main__":
    main()
