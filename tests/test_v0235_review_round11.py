# -*- coding: utf-8 -*-
"""v0.23.5 第十一轮回归（GPT 第十轮核验 · M/N：两处「换代后仍会写共享状态」的竞态）。

针对 GPT 第十轮复核单里两个仍未收口的真实竞态：
  - M：`_prime_state()` 在线程里直接写 `self._enc` —— 首代编码探测超时后重启，
    旧线程迟到返回会把新代已经拿到的编码覆盖（进而影响后续 UTF-8/GBK 解码选择）；
  - N：`_io_wait()` 的 `except RuntimeError` 分支无条件 `self._io_inflight -= 1` ——
    旧代提交因执行器关闭而失败时，若新代已有一笔 IO 在飞，会把新代计数错误减为 0，
    破坏「最多一笔在飞」的保护。

修复判据（本文件，先红后绿）：
  - 静态 M：`_prime_state` 不再写任何共享字段、编码随返回值（第 5 元）带出；
    `start()` 先记代号 → 传代号 → 兑现后 `_stale(prime_gen)` 复核 → 最后才写 `_enc`；
  - 静态 N：`_io_wait` 的 RuntimeError 分支减计数先验「IO 代」（形状钉死）；
    全文件每一处 `_io_inflight -= 1` 都在 `_io_inflight_gen == io_gen` 守卫之下。
  - 行为 M1：首代探测卡住（真线程）→ prime 超时 → 重启拿到新编码 → 放行旧线程
    迟到返回 → 新代编码不许被覆盖（修复前现场：`_enc` 被迟到结果覆盖成 `utf-8`）。
  - 行为 N1：旧代提交（执行器已关闭）失败瞬间，新代已接管且有一笔 IO 在飞 →
    减计数不许动新代（修复前现场：`_io_inflight` 1 → 0）；对照 N2：同代关闭照常减。
  - 行为 N3（同场竞态：真线程 + 真执行器 + 真异常）：stop() 关掉旧执行器、start()
    换代、新代一笔 IO 真在飞、旧代提交在已 shutdown 的执行器上真抛 RuntimeError ——
    新代计数必须保持 1（修复前：被减成 0、「忙」判据翻假、收尾计数变 -1）。

运行：<python> tests/test_v0235_review_round11.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
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
    """取某个函数 / 方法的源码片段（ast 定位；round10 同款）。"""
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


# ==================== M：_prime_state 编码结果的代际归位 ====================
def part_m_static() -> None:
    print("========== [M] _prime_state 编码结果归位：静态面 ==========")
    prime_src = fn_src(LW_SRC, "_prime_state", cls="LogWatcher")
    check("静态：_prime_state 不再写任何共享字段（含 _enc —— 迟到线程只许带出返回值）",
          re.search(r"self\.[A-Za-z_][A-Za-z0-9_]*\s*=(?!=)", prime_src) is None,
          prime_src[-280:].replace("\n", "\\n"))
    check("静态：编码随返回值带出（5 元组 pos, sig, head, present, enc）",
          "return pos, sig, head, present, enc" in prime_src
          and "str, bool, str]" in prime_src,
          prime_src[:200].replace("\n", "\\n"))
    start_src = fn_src(LW_SRC, "start", cls="LogWatcher")
    i_def = start_src.find("prime_gen = self._task_gen")
    i_call = start_src.find("self._io_wait(self._prime_state")
    i_gen = start_src.find("gen=prime_gen")
    i_stale = start_src.find("self._stale(prime_gen)")
    i_write = start_src.find("self._enc = enc")
    check("静态：start() 先记代号 → 传代号 → 兑现后复核代号 → 最后才写 _enc",
          -1 < i_def < i_call and -1 < i_gen and i_call < i_stale < i_write,
          f"def@{i_def} call@{i_call} gen@{i_gen} stale@{i_stale} write@{i_write}")
    check("静态：初始化结果落字段统一走 5 元组解包",
          "present, enc = primed" in start_src, start_src[-320:].replace("\n", "\\n"))


async def _late_prime_scenario_async():
    """复刻「首代编码探测超时 → 重启 → 旧线程迟到返回」的完整时序。

    返回 dict：entered / enc_after_start / enc_after_late / w。
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    log = _prepare_log(root)
    log.write_bytes("[12:00:00] [Server thread/INFO]: <Steve> 你好\n".encode("gbk"))
    w, _seen = _make_watcher(root)

    entered, release, late_done = threading.Event(), threading.Event(), threading.Event()
    calls = {"n": 0}

    def sniff():
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            release.wait(15)        # 首代探测：卡住（复刻「慢盘上探测迟迟不回来」）
            late_done.set()
            return "utf-8"          # 迟到返回一个「错」的编码（覆盖成功才看得到）
        return "gbk"

    w._sniff_encoding = sniff
    info = {"entered": False, "enc_after_start": None, "enc_after_late": None, "w": w}
    old_to = LW.IO_TIMEOUT
    LW.IO_TIMEOUT = 0.3
    try:
        await w.start()                          # 第 1 代：prime 超时（线程卡在探测里）
        info["entered"] = entered.wait(3)
        await w.stop()                           # 收掉第 1 代（旧线程仍在池里卡着）
        await w.start()                          # 第 2 代：探测立即成功，拿到 gbk
        info["enc_after_start"] = w._enc
        release.set()                            # 放行旧线程：迟到返回
        for _ in range(100):                     # 等它真的返回（事件驱动，别靠猜）
            if late_done.is_set():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)                 # 让迟到返回值交进那笔没人消费的 future
        info["enc_after_late"] = w._enc
    finally:
        LW.IO_TIMEOUT = old_to
        release.set()
        try:
            await w.stop()
        except BaseException:                    # noqa: BLE001
            pass
        td.cleanup()
    return info


def part_m_behavior() -> None:
    print("---- [M1] 行为：首代探测卡住超时 → 重启 → 旧线程迟到返回（不许覆盖新代编码） ----")
    info = asyncio.run(_late_prime_scenario_async())
    w = info["w"]
    check("前置：首代编码探测线程已进入（并卡住）", info["entered"], repr(info["entered"]))
    check("前置：第 2 代 start() 后编码已确定（gbk）",
          info["enc_after_start"] == "gbk", repr(info["enc_after_start"]))
    check("★旧线程迟到返回：新代编码不被覆盖（修复前现场 _enc='utf-8'）",
          info["enc_after_late"] == "gbk", repr(info["enc_after_late"]))
    check("对照：health() 如实报新代编码（覆盖会在此一并显形）",
          w.health()["encoding"] == "gbk", w.health()["encoding"])


# ==================== N：执行器关闭分支的代际守卫 ====================
def part_n_static() -> None:
    print("========== [N] 执行器关闭分支的代际守卫：静态面 ==========")
    iw = fn_src(LW_SRC, "_io_wait", cls="LogWatcher")
    i_re = iw.find("except RuntimeError")
    seg = iw[i_re: i_re + 800] if i_re != -1 else ""
    shape = re.search(
        r"except RuntimeError:\n(?:[ \t]*#[^\n]*\n)*[ \t]*with self\._io_lock:\n"
        r"(?:[ \t]*#[^\n]*\n)*[ \t]*if self\._io_inflight_gen == io_gen:\n"
        r"[ \t]*self\._io_inflight -= 1",
        seg)
    check("静态：执行器关闭分支的减计数先验 IO 代（with 锁 → if 代符 → 减）",
          i_re != -1 and shape is not None, f"i_re={i_re} seg={seg[:220]!r}")
    lines = LW_SRC.splitlines()
    bad, guarded = [], 0
    for idx, ln in enumerate(lines):
        if "_io_inflight -= 1" in ln:
            window = "\n".join(lines[max(0, idx - 3): idx])
            if "if self._io_inflight_gen == io_gen:" in window:
                guarded += 1
            else:
                bad.append(idx + 1)
    check("静态：全文件每一处减计数都有代际守卫（两处，无裸减残留）",
          guarded == 2 and not bad, f"guarded={guarded} bad_lines={bad}")


class _FakeClosedExecutor:
    """伪装「已 shutdown 的执行器」：submit 抛 RuntimeError（与真实 ThreadPoolExecutor
    shutdown 后的提交行为一致），可先做一次侧写（复刻旧代提交瞬间的并行状态变化）。"""

    def __init__(self, on_submit=None):
        self._on_submit = on_submit
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        if self._on_submit is not None:
            self._on_submit()
        raise RuntimeError("cannot schedule new futures after shutdown")

    def shutdown(self, *args, **kwargs):
        pass


async def _runtime_err_takeover_async():
    """旧代提交失败瞬间：新代已接管、且新代有一笔 IO 在飞（修复前：新代计数被减成 0）。"""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    _prepare_log(root)
    w, _seen = _make_watcher(root)
    w._io_gen = 1
    w._io_inflight = 0
    w._io_inflight_gen = 1
    w._task_gen = 2                      # 旧代（gen=1）已过期，新代已登记

    def takeover():
        w._io_gen = 2                    # 新代接管
        w._io_inflight_gen = 2
        w._io_inflight = 1               # 新代一笔 IO 在飞

    w._executor = _FakeClosedExecutor(on_submit=takeover)
    got = await w._io_wait(lambda: b"never", what="旧代提交", gen=1)
    out = {"w": w, "got": got}
    td.cleanup()
    return out


async def _runtime_err_same_gen_async():
    """对照：同代（没有换代）执行器关闭 —— 减计数照常（功能没被改坏）。"""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    _prepare_log(root)
    w, _seen = _make_watcher(root)
    w._io_gen = 1
    w._io_inflight = 0
    w._io_inflight_gen = 1
    w._task_gen = 1
    w._executor = _FakeClosedExecutor()
    got = await w._io_wait(lambda: b"never", what="同代提交", gen=1)
    out = {"w": w, "got": got}
    td.cleanup()
    return out


def part_n_behavior() -> None:
    print("---- [N1] 行为：旧代提交失败瞬间新代已有 IO 在飞 → 计数不许被动 ----")
    out = asyncio.run(_runtime_err_takeover_async())
    w, got = out["w"], out["got"]
    check("前置：调用按「没读到」降级返回 None", got is None, repr(got))
    check("★新代在飞计数保持 1（修复前现场：被裸减为 0）",
          w._io_inflight == 1, f"_io_inflight={w._io_inflight}")
    check("★单飞闸门仍判「忙」（修复前现场：_io_busy 翻假）",
          w._io_busy is True and w.health()["io_inflight"] == 1,
          f"busy={w._io_busy} health={w.health()['io_inflight']}")
    check("在飞计数所属代仍为新代（未被改写）",
          w._io_inflight_gen == 2, str(w._io_inflight_gen))
    check("对照：旧代（gen=1）过期 → 跳过记账不写新代（io_skipped 保持 0）",
          w.io_skipped == 0, str(w.io_skipped))

    print("---- [N2] 对照：同代执行器关闭 → 减计数照常（功能没被改坏） ----")
    out2 = asyncio.run(_runtime_err_same_gen_async())
    w2, got2 = out2["w"], out2["got"]
    check("对照：同代关闭照常减计数（_io_inflight 回到 0）",
          got2 is None and w2._io_inflight == 0, f"got={got2!r} n={w2._io_inflight}")
    check("对照：同代跳过记账照常 +1（io_skipped=1）", w2.io_skipped == 1, str(w2.io_skipped))


class _GateThenShutdownExecutor(ThreadPoolExecutor):
    """真实执行器 + 闸门：submit 先报到、等放行；放行后若已被 shutdown，
    CPython 会在 submit 里**原地**抛 RuntimeError（真实现场，不是伪装的异常）。"""

    def __init__(self):
        super().__init__(max_workers=1, thread_name_prefix="r11-gate")
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.passed = False
        self.raised = False

    def submit(self, fn, *args, **kwargs):
        self.arrived.set()
        self.release.wait(15)
        self.passed = True                    # 已放行、真正走到了提交那一刻
        try:
            return super().submit(fn, *args, **kwargs)
        except RuntimeError:
            self.raised = True                # 真实现场：执行器已 shutdown
            raise


async def _stop_start_close_scenario_async():
    """同场竞态：stop() 关旧执行器 × start() 换代 × 新代一笔 IO 真在飞 × 旧代提交真失败。"""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    _prepare_log(root)
    w, _seen = _make_watcher(root)

    # —— 舞台：第 1 代（旧代）持有闸门执行器，旧代提交将卡在 submit 闸门里 ——
    gated = _GateThenShutdownExecutor()
    w._executor = gated
    w._io_gen = 1
    w._io_inflight = 0
    w._io_inflight_gen = 1
    w._task_gen = 1

    box_old: dict = {}

    def old_call():
        try:
            box_old["rc"] = asyncio.run(w._io_wait(lambda: b"x", what="旧代提交", gen=1))
        except BaseException as e:                  # noqa: BLE001
            box_old["err"] = e

    th_old = threading.Thread(target=old_call, daemon=True)
    th_old.start()
    reached = False
    try:
        reached = await asyncio.wait_for(asyncio.to_thread(gated.arrived.wait), 5)
    except asyncio.TimeoutError:
        reached = False

    state = {"reached": reached, "probed": None, "inflight_after": None,
             "busy_after": None, "inflight_end": None, "box_old": box_old,
             "box_new": {}, "th_old_alive": None, "th_new_alive": None,
             "gated_raised": None}
    if not reached:
        gated.release.set()
        try:
            await asyncio.wait_for(asyncio.to_thread(th_old.join), 3)
        except asyncio.TimeoutError:
            pass
        state["th_old_alive"] = th_old.is_alive()
        td.cleanup()
        return state

    # —— 旧代卡在 submit；主线程做一次**真实**的 stop() + start() 换代 ——
    await w.stop()                                  # 关掉 gated（wait=False）
    await w.start()                                 # 第 2 代：新执行器 + 初始化成功
    state["probed"] = w._probed
    w._running = False                              # 静默轮询循环（防它给断言添噪音）
    for _ in range(60):
        if w._task.done() and w._io_inflight == 0:
            break
        await asyncio.sleep(0.02)

    # —— 新代一笔 IO **真在飞**（执行器线程真堵在函数里，闸门由本测试控制） ——
    new_entered, hold = threading.Event(), threading.Event()

    def holding():
        new_entered.set()
        hold.wait(15)
        return b"y"

    box_new: dict = {}

    def new_call():
        try:
            box_new["rc"] = asyncio.run(w._io_wait(holding, what="新代IO", gen=w._task_gen))
        except BaseException as e:                  # noqa: BLE001
            box_new["err"] = e

    th_new = threading.Thread(target=new_call, daemon=True)
    th_new.start()
    try:
        new_up = await asyncio.wait_for(asyncio.to_thread(new_entered.wait), 5)
    except asyncio.TimeoutError:
        new_up = False

    if new_up:
        # —— 放行旧代：它在已 shutdown 的 gated 上提交 → 真 RuntimeError ——
        gated.release.set()
        try:
            await asyncio.wait_for(asyncio.to_thread(th_old.join), 8)
        except asyncio.TimeoutError:
            pass
        state["inflight_after"] = w._io_inflight
        state["busy_after"] = w._io_busy
        # —— 收尾：放行新代 IO、等兑现；计数归零 ——
        hold.set()
        try:
            await asyncio.wait_for(asyncio.to_thread(th_new.join), 8)
        except asyncio.TimeoutError:
            pass
        for _ in range(40):
            if w._io_inflight == 0:
                break
            await asyncio.sleep(0.05)
        state["inflight_end"] = w._io_inflight
    else:
        gated.release.set()
        hold.set()
        await asyncio.sleep(0.2)

    state["box_new"] = box_new
    state["gated_raised"] = gated.raised
    state["th_old_alive"] = th_old.is_alive()
    state["th_new_alive"] = th_new.is_alive()
    try:
        await w.stop()
    except BaseException:                            # noqa: BLE001
        pass
    td.cleanup()
    return state


def part_n3_behavior() -> None:
    print("---- [N3] 行为：stop/start × executor-close 同场竞态（真线程 + 真执行器 + 真异常） ----")
    st = asyncio.run(_stop_start_close_scenario_async())
    check("前置：旧代提交已进入 submit 闸门（卡住）",
          bool(st.get("reached")), repr(st.get("box_old")))
    if not st.get("reached"):
        return
    ok_pre = (st.get("probed") is True and st.get("inflight_after") is not None)
    check("前置：真 stop/start 完成（probed=True）且新代一笔 IO 真在飞",
          ok_pre, f"probed={st.get('probed')} inflight={st.get('inflight_after')}")
    if not ok_pre:
        return
    check("★旧代提交在已 shutdown 的执行器上真失败（RuntimeError 被就地降级为「没读到」）",
          st["gated_raised"] is True and st["box_old"].get("rc", "sentinel") is None,
          f"raised={st['gated_raised']} box={st['box_old']!r}")
    check("★新代在飞计数保持 1（修复前现场：被裸减为 0）",
          st["inflight_after"] == 1, f"inflight={st['inflight_after']}")
    check("★单飞闸门仍判「忙」（修复前现场：_io_busy 翻假）",
          st["busy_after"] is True, f"busy={st['busy_after']}")
    check("收尾：放行后新代 IO 正常兑现（b'y'）、计数归零（修复前现场：-1）",
          st["box_new"].get("rc") == b"y" and st["inflight_end"] == 0,
          repr({k: st[k] for k in ("box_new", "inflight_end")}))
    check("无泄漏：两根线程都已退出",
          st["th_old_alive"] is False and st["th_new_alive"] is False,
          f"old={st['th_old_alive']} new={st['th_new_alive']}")


def main() -> None:
    print("=" * 72)
    print("v0.23.5 第十一轮回归（GPT 第十轮核验 · M/N：两处「换代后仍会写共享状态」）")
    print("=" * 72)
    part_m_static()
    part_m_behavior()
    part_n_static()
    part_n_behavior()
    part_n3_behavior()
    print("=" * 72)
    if _fail:
        print(f"通过 {_pass} 项，失败 {len(_fail)} 项：")
        for name in _fail:
            print(f"  [FAIL] {name}")
        sys.exit(1)
    print(f"通过 {_pass} 项，失败 0 项")
    print("全部通过：_prime_state 编码归位 / _io_wait 执行器关闭减计数的代际守卫 —— "
          "两处竞态都在「换代后不许写共享状态」之下")
    sys.exit(0)


if __name__ == "__main__":
    main()
