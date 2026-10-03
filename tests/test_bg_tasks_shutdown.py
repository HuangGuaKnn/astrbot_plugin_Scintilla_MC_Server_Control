"""v0.23.x 回归用例：后台任务登记册（core/bg_tasks.py）行为契约。

对应核验报告 P1 · ⑥「后台任务统一收口」：

  * 插件 terminate() 之前，四处裸 create_task 的任务**谁都不持有引用**，
    卸载 / 热重载后照跑不误 —— 握着已终止的插件往会话发通知、写向量、跑流水线。
  * 同一个具名任务能被并发拉起（知识库向量构建有三个触发点），
    而 build_vectors() 并发跑同一个 SemanticIndex 会互相踩。
  * 协程异常没人取，只在日志里留一句 Task exception was never retrieved。

本用例只测纯逻辑；bg_tasks.py 按上架规范从 astrbot.api 取日志器，
所以拿不到 astrbot 时会注一个最小替身 —— 没装 AstrBot 的机器也能跑。

  <python> tests/test_bg_tasks_shutdown.py
"""
from __future__ import annotations

import asyncio
import gc
import importlib.util
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

PLUGIN = Path(__file__).resolve().parent.parent
FAIL: list[str] = []
SKIP: list[str] = []


def _ensure_astrbot_api() -> bool:
    """确保 ``from astrbot.api import logger`` 能成立。返回是否用了替身。"""
    try:
        import astrbot.api  # noqa: F401
        return False
    except Exception:
        import types
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("bg_tasks_test.stub")
        pkg = types.ModuleType("astrbot")
        pkg.api = api
        sys.modules["astrbot"] = pkg
        sys.modules["astrbot.api"] = api
        return True


STUBBED_ASTRBOT = _ensure_astrbot_api()


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


spec = importlib.util.spec_from_file_location("v23_bg_tasks", PLUGIN / "core" / "bg_tasks.py")
bgt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bgt)


class Recorder(logging.Handler):
    """把登记册的日志抓下来，断言「异常真的被记了」而不是悄悄消失。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level: int = logging.NOTSET) -> list[str]:
        # getMessage() 已经把 args 格式化好了，不能再 % 一次
        return [r.getMessage() for r in self.records if r.levelno >= level]


def _mk():
    log = logging.getLogger("bg_tasks_test")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    rec = Recorder()
    log.addHandler(rec)
    return bgt.BackgroundTasks(log, shutdown_timeout=1.0), rec


# ---------------------------------------------------------------- 1. replace

async def t_replace():
    bg, rec = _mk()
    started, killed = [], []

    async def slow(tag):
        started.append(tag)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            killed.append(tag)
            raise

    first = bg.spawn("job", slow("first"), policy="replace")
    await asyncio.sleep(0)
    second = bg.spawn("job", slow("second"), policy="replace")
    await asyncio.sleep(0.05)

    check("replace：旧任务被取消", "first" in killed, f"killed={killed}")
    check("replace：新任务在跑", bg.is_running("job"), f"snapshot={bg.snapshot()}")
    check("replace：槽位指向新任务", bg.get("job") is second, "槽位没换成新任务")
    check("replace：旧任务不再是槽位值", bg.get("job") is not first, "槽位还留着旧任务")
    check("replace：快照里只有一个在跑", bg.snapshot()["alive"] == 1, str(bg.snapshot()))
    check("replace：取消异常没被当成错误记录",
          not [m for m in rec.messages(logging.WARNING)], str(rec.messages(logging.WARNING)))
    await bg.shutdown()


# ------------------------------------------------------------------- 2. skip

async def t_skip():
    bg, rec = _mk()
    runs = []

    async def slow():
        runs.append(1)
        await asyncio.sleep(30)

    first = bg.spawn("job", slow(), policy="skip")
    await asyncio.sleep(0)
    dropped = bg.spawn("job", slow(), policy="skip")
    await asyncio.sleep(0.05)

    check("skip：第二次返回 None（被丢弃）", dropped is None, f"got {dropped!r}")
    check("skip：第一个任务仍在跑", first is not None and not first.done(), "原任务被殃及")
    check("skip：协程只启动了一次", len(runs) == 1, f"runs={runs}")
    check("skip：槽位没被换", bg.get("job") is first, "槽位被替换了")
    await bg.shutdown()


# --------------------------------------------------------------- 3. parallel

async def t_parallel():
    bg, rec = _mk()
    alive = []

    async def slow(tag):
        alive.append(tag)
        await asyncio.sleep(30)

    tasks = [bg.spawn("flow", slow(i), policy="parallel") for i in range(3)]
    await asyncio.sleep(0.05)

    check("parallel：三个任务都起来了", all(t is not None for t in tasks), str(tasks))
    check("parallel：互不取消", all(not t.done() for t in tasks), "有任务被提前取消")
    check("parallel：都登记在组里", len(alive) == 3, f"alive={alive}")
    snap = bg.snapshot()
    check("parallel：快照记录该组", snap["groups"].get("flow") == ["running"] * 3, str(snap))

    n = await bg.shutdown()
    check("parallel：收口取消三个", n == 3, f"cancelled={n}")
    check("parallel：收口后组清空", bg.snapshot()["groups"] == {}, str(bg.snapshot()))


# -------------------------------------------------------------- 4. shutdown

async def t_shutdown():
    bg, rec = _mk()
    seen = []

    async def slow(name):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen.append(name)
            raise

    bg.spawn("a", slow("a"))
    bg.spawn("b", slow("b"), policy="parallel")
    bg.spawn("c", slow("c"), policy="parallel")
    await asyncio.sleep(0.05)

    n = await bg.shutdown()
    check("shutdown：取消数量正确", n == 3, f"cancelled={n}")
    check("shutdown：三个都收到取消", sorted(seen) == ["a", "b", "c"], f"seen={seen}")
    check("shutdown：册子已清空", bg.snapshot()["alive"] == 0, str(bg.snapshot()))
    check("shutdown：标记 closing", bg.closing is True, "closing 未置位")

    n2 = await bg.shutdown()
    check("shutdown：幂等（第二次 0）", n2 == 0, f"second={n2}")
    check("shutdown：正常取消不写警告日志",
          not [m for m in rec.messages(logging.WARNING)], str(rec.messages(logging.WARNING)))


# ------------------------------------------------------ 5. closing 后丢弃协程

async def t_spawn_after_closing():
    bg, rec = _mk()
    await bg.shutdown()

    async def never():
        raise AssertionError("收口后 spawn 的协程不该被跑起来")

    coro = never()
    out = bg.spawn("late", coro)
    await asyncio.sleep(0.02)

    check("closing 后 spawn 返回 None", out is None, f"got {out!r}")
    check("closing 后协程被 close（cr_frame 清空）", coro.cr_frame is None,
          "协程没被关闭 —— 会吃 never awaited 警告")


# ------------------------------------------- 6. 丢弃即 close，不留 never-awaited

async def t_no_never_awaited_warning():
    bg, rec = _mk()
    bg.spawn("busy", asyncio.sleep(30), policy="replace")
    await asyncio.sleep(0)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dropped = bg.spawn("busy", asyncio.sleep(30), policy="skip")   # 会被丢弃
        del dropped
        gc.collect()
        noisy = [w for w in caught if issubclass(w.category, RuntimeWarning)
                 and "never awaited" in str(w.message)]

    check("被丢弃的协程不产生 never-awaited 警告", not noisy, str([str(w.message) for w in noisy]))
    await bg.shutdown()


# ------------------------------------------------------- 7. 异常被登记册吃掉

async def t_exception_recorded():
    bg, rec = _mk()

    async def boom():
        raise ValueError("登记册应该记下我")

    bg.spawn("boom", boom())
    await asyncio.sleep(0.05)

    msgs = rec.messages(logging.WARNING)
    hit = [m for m in msgs if "boom" in m and "登记册应该记下我" in m]
    check("任务异常被记成带名字的警告", bool(hit), f"msgs={msgs}")
    check("异常任务跑完自动从册子摘掉", bg.get("boom") is None, str(bg.snapshot()))


# ------------------------------------------------- 8. 暂无事件循环时安全丢弃

def t_no_running_loop():
    bg, rec = _mk()

    async def never():
        raise AssertionError("没有事件循环时不该跑")

    coro = never()
    out = bg.spawn("noloop", coro)
    check("无事件循环：返回 None", out is None, f"got {out!r}")
    check("无事件循环：协程被 close", coro.cr_frame is None, "协程没关闭")


# ------------------------------------------------------------- 9. 策略兜底

async def t_bad_policy():
    bg, rec = _mk()

    async def slow():
        await asyncio.sleep(30)

    first = bg.spawn("p", slow(), policy="replace")
    await asyncio.sleep(0)
    second = bg.spawn("p", slow(), policy="whatever")     # 未知 → 退化为 replace
    await asyncio.sleep(0.05)

    check("未知策略退化为 replace", second is not None and not first.done() is False,
          "未知策略没有按 replace 处理")
    check("未知策略写了警告日志", any("未知策略" in m for m in rec.messages(logging.WARNING)),
          str(rec.messages(logging.WARNING)))
    await bg.shutdown()


# ------------------------------------------------- 10. adopt：收编外部任务

async def t_adopt():
    bg, rec = _mk()
    task = asyncio.create_task(asyncio.sleep(30))
    bg.adopt("ext", task)
    await asyncio.sleep(0.02)

    check("adopt：外部任务进了册子", bg.snapshot()["alive"] == 1, str(bg.snapshot()))
    n = await bg.shutdown()
    check("adopt：收口时一并取消", n == 1 and task.cancelled(), f"n={n} cancelled={task.cancelled()}")


# ------------------------------------------------------- 11. 慢任务超时不卡死

async def t_shutdown_timeout_not_hang():
    log = logging.getLogger("bg_tasks_test_slow")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    rec = Recorder()
    log.addHandler(rec)
    bg = bgt.BackgroundTasks(log, shutdown_timeout=0.2)

    async def stubborn():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 故意吞掉取消、赖着不走：收口不能被它卡住
            await asyncio.sleep(30)

    bg.spawn("stubborn", stubborn())
    await asyncio.sleep(0.02)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    n = await bg.shutdown()
    spent = loop.time() - t0

    check("超时收口：取消计数仍返回", n == 1, f"n={n}")
    check("超时收口：不卡死（< 1.0s 返回）", spent < 1.0, f"spent={spent:.3f}s")
    check("超时收口：写了未退出警告", any("未退出" in m for m in rec.messages(logging.WARNING)),
          str(rec.messages(logging.WARNING)))


# ------------------------------------------- 13. 收口豁免（热重载那类任务）

async def t_shutdown_exemption():
    """热重载正在 ``await pm.reload()``，而 reload 会触发本插件的 terminate()。

    收口时若把它取消，重载就被从中间掐断 —— 插件停在半加载状态。
    所以这类任务必须豁免：不取消、不等待，交由它自行结束。
    """
    bg, rec = _mk()
    cancelled, finished = [], []
    loop = asyncio.get_running_loop()

    async def exempt_job():
        try:
            await asyncio.sleep(0.25)        # 模拟「正在 await pm.reload()」
        except asyncio.CancelledError:
            cancelled.append("exempt")
            raise
        finished.append("exempt")

    async def ordinary():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append("ordinary")
            raise

    bg.spawn("hot_reload", exempt_job(), policy="skip", cancel_on_shutdown=False)
    bg.spawn("kb_semantic", ordinary(), policy="skip")
    await asyncio.sleep(0.02)

    t0 = loop.time()
    n = await bg.shutdown()
    spent = loop.time() - t0

    check("豁免：普通任务照常取消", n == 1 and "ordinary" in cancelled, f"n={n} cancelled={cancelled}")
    check("豁免：豁免任务没被取消", "exempt" not in cancelled, f"cancelled={cancelled}")
    check("豁免：不等待它（< 0.2s 返回）", spent < 0.2, f"spent={spent:.3f}s（豁免任务要 0.25s）")
    check("豁免：写了豁免日志", any("收口豁免" in m for m in rec.messages(logging.INFO)),
          str(rec.messages(logging.INFO)))

    await asyncio.sleep(0.35)
    check("豁免：任务是自行跑完的（没被打断）", finished == ["exempt"], f"finished={finished}")
    check("豁免：跑完后册子仍干净", bg.snapshot()["alive"] == 0, str(bg.snapshot()))


# ---------------------------------------------------------------- 14. 快照

async def t_snapshot_shape():
    bg, rec = _mk()

    async def slow():
        await asyncio.sleep(30)

    bg.spawn("x", slow())
    bg.spawn("g", slow(), policy="parallel")
    await asyncio.sleep(0.02)
    snap = bg.snapshot()

    # v0.23.5：快照可以**增**键（本轮加了收口放弃留档 gave_up_at_shutdown），
    # 但四个基础键必须始终在位 —— 断言「⊆」而不是「==」，否则每加一项诊断信息
    # 都得改测试，护栏就从「防丢键」退化成了「防进步」。
    want = {"closing", "named", "groups", "alive"}
    check("快照：含 closing/named/groups/alive 四键",
          want <= set(snap), str(set(snap)))
    check("快照：收口放弃留档键在位（v0.23.5）",
          "gave_up_at_shutdown" in snap, str(set(snap)))
    check("快照：Alive 计数正确", snap["alive"] == 2, str(snap))
    await bg.shutdown()


CASES = [
    ("replace 覆盖旧任务", t_replace()),
    ("skip 丢弃重复任务", t_skip()),
    ("parallel 同名并发", t_parallel()),
    ("shutdown 收口", t_shutdown()),
    ("closing 后丢弃", t_spawn_after_closing()),
    ("丢弃即 close", t_no_never_awaited_warning()),
    ("异常被记录", t_exception_recorded()),
    ("无循环安全丢弃", None),
    ("未知策略兜底", t_bad_policy()),
    ("adopt 收编", t_adopt()),
    ("超时不卡死", t_shutdown_timeout_not_hang()),
    ("收口豁免（热重载）", t_shutdown_exemption()),
    ("快照结构", t_snapshot_shape()),
]


def main() -> int:
    print("=" * 68)
    print("后台任务登记册 · 行为契约（core/bg_tasks.py）")
    if STUBBED_ASTRBOT:
        print("（未找到 astrbot，已注入最小替身提供 api.logger）")
    print("=" * 68)
    for label, coro in CASES:
        if coro is None:
            t_no_running_loop()
            continue
        print(f"\n--- {label} ---")
        try:
            asyncio.run(coro)
        except Exception as e:  # 用例自身炸了也要记账，不能静默
            check(f"{label}（用例执行）", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 68)
    if SKIP:
        print(f"跳过 {len(SKIP)} 项")
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过：")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
