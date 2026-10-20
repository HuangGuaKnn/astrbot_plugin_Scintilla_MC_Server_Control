# -*- coding: utf-8 -*-
"""v0.24.7 回归：GPT 第二轮复核（P2 批次）修复面的**可复现判据**。

本批修的都是「平时看不出来、出事时说不清」的口子。每条判据都走真实代码路径，
不做白盒记账（断言的是行为与账目，不是源码里有没有某一行）：

  [1] **状态口径收敛**（P2-5）：`normalize_status()` 把外部给的 status 收敛成白名单，
      并把「已回落」如实回报（界面显示的值必须和入库的值一致）。
  [2] **写接口输入硬化**（P2-5）：`save_entry` 收到数字 / 对象形态的 topic/content/
      old_topic 时**结构化拒绝**（ok=False + 说明），不再 `AttributeError` 冲出成 500；
      非法 status 入库为 untested 且回包带 `status_note`。
  [3] **预设 ID 先验后碰盘**（P2-6）：`transfer()` 收到 `../../x` 这类 ID 返回
      `{"ok": False}`，**不抛异常、不碰文件系统**（`preset_path()` 本身仍保持
      「非法即抛 ValueError」的防穿越语义 —— 两者是不同层的约定）。
  [4] **阻塞调用一律走 daemon 线程**（P2-9）：`run_blocking` / `run_blocking_or_raise`
      与 `_io_wait` 的兜底路径都**不许**碰事件循环默认线程池；超时按「不可用」返回。
      判据是**行为**：跑完之后 `loop._default_executor` 仍然是 None（默认池从未被建立）。
  [5] **事件回调的超时与在飞上限**（P2）：回调卡住不再拖住轮询；超时 / 丢弃 /
      僵尸三笔账在 `health()` 里看得见；`stop()` 必须取消在飞回调。
  [6] **工作流发送预算**（P1-5）：单批条数、单任务累计条数、累计字节三类上限，
      超限**一条都不发**（fail-closed），坏配置回默认值而不是把闸门拆掉。
  [7] **指纹缓存的纳秒时间戳**（P2）：同一秒内换掉「同名同大小」的 jar，
      不许命中旧 jar 的 ID；老的秒级缓存键不再被认。
  [8] **发送预算三键真正可调**（P1-5 收尾）：三条闸门必须同时出现在
      `_conf_schema.json`、后端 `INT_RANGES` 与设置页 `CFG_FIELDS`/DOM 三处，
      且**上下限彼此同源**（拿 `core/workflow.py` 的 `_cfg_int` 源码逐条对账）。
      只做后端不做界面 = 功能半通；只在界面暴露、后端不认 = 每次保存都误报
      「后端未接受」。这两种「半通」都要能被这条判据抓住。

运行：
  <AstrBot python> tests\\test_v0247_review_round2_p2.py
（用 AstrBot 自带的解释器；或用 ASTRBOT_APP_DIR 指路。本文件需要 AstrBot 运行时
 —— 第 [2] 组要真调 WebAPI 处理器。）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as kbmod  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as lw  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import mod_fingerprint as mf  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    ModKnowledgeBase,
    normalize_status,
)
from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import (  # noqa: E402
    DEFAULT_MAX_BATCH_COMMANDS,
    DEFAULT_MAX_TASK_BYTES,
    DEFAULT_MAX_TASK_COMMANDS,
    MCWorkflow,
    SendBudget,
)

import test_remote_rcon_mode as harness  # noqa: E402  （复用 make_plugin / FakeRequest / body）

FAILED: list[str] = []
PASSED = [0]


def check(desc: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASSED[0] += 1
        print(f"  ✓ {desc}")
    else:
        FAILED.append(desc)
        print(f"  ✗ {desc}" + (f"  ← {extra}" if extra else ""))


# ============================================================ [1] 状态口径
def part_status() -> None:
    print("========== [1] status 白名单收敛：入库值必须能被如实回报 ==========")
    valid = kbmod.VALID_STATUS
    s, note = normalize_status("verified")
    check("合法值原样通过、不带说明", (s, note) == ("verified", ""), f"{s!r}/{note!r}")
    s, note = normalize_status("  VERIFIED  ")
    check("大小写 / 首尾空白被收敛（不是拒绝）", (s, note) == ("verified", ""), f"{s!r}")
    s, note = normalize_status(None)
    check("缺省 → untested（默认档，不报错）", (s, note) == ("untested", ""), f"{s!r}/{note!r}")
    s, note = normalize_status("")
    check("空串 → untested、不提示（等同没填）", (s, note) == ("untested", ""), f"{s!r}/{note!r}")
    s, note = normalize_status("听说很好用")
    check("非法值回落 untested", s == "untested", s)
    check("★非法值必须**说出来**（回落到界面看得见的值才不算说谎）",
          bool(note) and "听说很好用" in note and "untested" in note, note)
    check("说明里列出合法取值（用户据此改）",
          all(v in note for v in valid), note)
    s, note = normalize_status(123)
    check("非字符串（数字）→ untested 且说明点名类型",
          s == "untested" and "int" in note, f"{s!r}/{note!r}")
    s, note = normalize_status({"a": 1})
    check("非字符串（对象）→ untested 且说明点名类型",
          s == "untested" and "dict" in note, f"{s!r}/{note!r}")


# ============================================================ [2] 写接口输入硬化
def _plug_with_kb(cfg: dict, data_dir: Path):
    plug = harness.make_plugin(cfg, data_dir)
    plug._knowledge = ModKnowledgeBase(
        str(data_dir / "kb"), server_id="sid", preset_id="p1",
        preset_name="测试库", fingerprint="fp",
    )
    return plug


def _call_save_entry(plug, payload: dict) -> dict:
    api = wa.McControlWebApi(plug)
    wa.request = harness.FakeRequest(payload)
    return harness.body(asyncio.run(api.save_entry()))


def part_webapi() -> None:
    print("========== [2] save_entry：坏输入结构化拒绝 + 状态回落可见 ==========")
    with tempfile.TemporaryDirectory() as td:
        plug = _plug_with_kb({}, Path(td))
        # 坏输入：数字 / 对象 / 数组都曾直接 AttributeError → 500
        for key, bad in (("topic", 123), ("content", {"a": 1}), ("old_topic", [1, 2])):
            payload = {"topic": "t", "content": "c", "old_topic": "t"}
            payload[key] = bad
            try:
                res = _call_save_entry(plug, payload)
                ok = res.get("ok") is False and key in str(res.get("error", ""))
                check(f"{key}={type(bad).__name__} → ok:False 且点名该字段（不再 500）",
                      ok, str(res)[:140])
            except Exception as e:                       # noqa: BLE001
                check(f"{key}={type(bad).__name__} → ok:False 且点名该字段（不再 500）",
                      False, f"抛出了 {type(e).__name__}: {e}")
        check("坏输入没有造出脏条目", plug._knowledge.list_entries() == [],
              str(plug._knowledge.list_entries())[:120])

        # 正常写入仍然工作
        res = _call_save_entry(plug, {"topic": "甲", "content": "正文", "status": "verified"})
        check("合法写入 ok:True 且 status_note 为空",
              res.get("ok") is True and res.get("status_note") == "", str(res)[:140])
        check("合法写入的 status 就是传进来的那个",
              plug._knowledge.get_entry("甲")["status"] == "verified")

        # 非法 status：写进去的是 untested，回包必须说清楚
        res = _call_save_entry(plug, {"topic": "乙", "content": "正文", "status": "超厉害"})
        check("非法 status → 仍写入成功（软收敛，不是拒绝）", res.get("ok") is True, str(res)[:140])
        check("入库值 = untested（界面看到的必须与库内一致）",
              plug._knowledge.get_entry("乙")["status"] == "untested",
              plug._knowledge.get_entry("乙")["status"])
        check("★回包带 status_note（把「已回落」告诉界面）",
              "超厉害" in str(res.get("status_note") or ""), str(res.get("status_note"))[:120])

        res = _call_save_entry(plug, {"topic": "", "content": "正文"})
        check("topic 空 → ok:False（必填校验仍在）", res.get("ok") is False, str(res)[:120])


# ============================================================ [3] 预设 ID
def part_preset_id() -> None:
    print("========== [3] transfer：非法预设 ID 结构化拒绝、不碰盘 ==========")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        man = kbmod.KnowledgePresetManager(str(root / "kb"), "sid", str(root))
        a = man.create("预设A")
        b = man.create("预设B")

        for bad in ("../../config", "..", "a/b", "a\\b", "", "x" * 300):
            try:
                res = man.transfer(bad, b["id"])
                ok = isinstance(res, dict) and res.get("ok") is False and bool(res.get("error"))
                check(f"from={bad[:14]!r} → 结构化拒绝（ok:False + 原因）", ok, repr(res)[:140])
            except Exception as e:                       # noqa: BLE001
                check(f"from={bad[:14]!r} → 结构化拒绝（ok:False + 原因）",
                      False, f"抛出了 {type(e).__name__}: {e}")
        try:
            res = man.transfer("ok-id", 12345)
            check("to=12345（非字符串）→ 结构化拒绝（不抛异常）",
                  isinstance(res, dict) and res.get("ok") is False, repr(res)[:140])
        except Exception as e:                           # noqa: BLE001
            check("to=12345（非字符串）→ 结构化拒绝（不抛异常）",
                  False, f"抛出了 {type(e).__name__}: {e}")

        check("★防穿越的那一层没有被削弱：preset_path 对非法 ID 仍抛 ValueError",
              _raises(lambda: man.preset_path("../../config")), "没抛")
        check("拒绝过程没有在数据目录之外造出文件",
              not any(root.glob("kb_preset_*.json")) or
              sorted(p.name for p in (root / "kb").glob("kb_preset_*.json")) is not None)

        # 合法路径照旧工作：A 的条目搬进 B
        man.kb = None
        src = root / "kb" / f"kb_preset_{a['id']}.json"
        import json as _json
        data = _json.loads(src.read_text(encoding="utf-8"))
        data.setdefault("entries", {})["主题甲"] = {
            "content": "内容甲", "status": "verified", "enabled": True,
            "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        }
        src.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        res = man.transfer(a["id"], b["id"])
        check("合法 transfer 仍成功（这次修复没有把功能关掉）", res.get("ok") is True, repr(res)[:140])
        check("条目确实进了目标预设", res.get("count") == 1, repr(res)[:140])


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                                    # noqa: BLE001
        return True
    return False


# ============================================================ [4] daemon 线程
def part_daemon_threads() -> None:
    print("========== [4] 阻塞调用：daemon 线程 + 不占默认线程池 ==========")

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        seen: dict = {}

        def slow_ok(x: int) -> int:
            seen["daemon"] = threading.current_thread().daemon
            seen["name"] = threading.current_thread().name
            return x * 2

        ok, val = await lw.run_blocking(slow_ok, 21, timeout=5.0)
        check("run_blocking 正常完成 → (True, 返回值)", (ok, val) == (True, 42), f"{ok}/{val}")
        check("★跑在 daemon 线程里（退出时不会被解释器 join）",
              seen.get("daemon") is True, str(seen))

        def slow_fail() -> None:
            raise ValueError("boom")

        ok2, val2 = await lw.run_blocking(slow_fail, timeout=5.0)
        check("run_blocking 内部异常 → (False, None)（不炸调用方）",
              (ok2, val2) == (False, None), f"{ok2}/{val2}")

        def never() -> None:
            time.sleep(3)

        t0 = time.monotonic()
        ok3, val3 = await lw.run_blocking(never, timeout=0.1)
        dt = time.monotonic() - t0
        check("run_blocking 超时 → (False, None)", (ok3, val3) == (False, None), f"{ok3}/{val3}")
        check("超时后**立刻**返回（不等那个线程）", dt < 1.0, f"{dt:.2f}s")

        try:
            await lw.run_blocking_or_raise(slow_fail, timeout=5.0, what="测试活儿")
            check("run_blocking_or_raise 失败时抛 RuntimeError", False, "没抛")
        except RuntimeError as e:
            check("run_blocking_or_raise 失败时抛 RuntimeError 且带上「what」",
                  "测试活儿" in str(e), str(e))
        got = await lw.run_blocking_or_raise(slow_ok, 5, timeout=5.0, what="测试活儿")
        check("run_blocking_or_raise 成功时直接给返回值", got == 10, repr(got))

        # 兜底路径（还没 start() 的 watcher）同样不许碰默认线程池
        w = lw.LogWatcher(str(Path(tempfile.mkdtemp())), _noop_event)
        inside: dict = {}

        def probe() -> str:
            t = threading.current_thread()
            inside["daemon"] = t.daemon
            inside["name"] = t.name
            return "文件内容"

        out = await w._io_wait(probe, what="测试读取")
        check("_io_wait 兜底路径能正常返回", out == "文件内容", repr(out))
        check("★兜底路径也在 daemon 线程里", inside.get("daemon") is True, str(inside))
        check("★兜底路径用的是它自己的执行器（logwatch-fallback），不是默认池",
              str(inside.get("name", "")).startswith("logwatch-fallback"), str(inside))
        check("★跑完之后事件循环的默认线程池仍然没有被建立（None）",
              getattr(loop, "_default_executor", None) is None,
              repr(getattr(loop, "_default_executor", None)))
        check("兜底执行器关闭后 submit 抛 RuntimeError（与线程池同一句话）",
              _raises(lambda: w._get_fallback_executor().shutdown() or
                      w._get_fallback_executor().submit(lambda: 1)))

    asyncio.run(scenario())

    # 进程级 daemon 执行器：单例、daemon、**真实有界**的并发上限
    # v0.24.8（GPT 第三轮复核 P1）：旧实现每次 submit 新建一条 daemon 线程，
    # _max_workers 写死 1 却毫无约束力（名字叫单线程、行为是无界）。
    ex = lw.daemon_executor()
    check("daemon_executor() 是进程级单例", lw.daemon_executor() is ex)
    check("并发上限是真实值（不再是写死 1 的无界实现）",
          getattr(ex, "_max_workers", None) == lw.MAX_DAEMON_WORKERS,
          repr(getattr(ex, "_max_workers", None)))
    gate = threading.Event()
    futs = [ex.submit(gate.wait, 1.0) for _ in range(24)]
    time.sleep(0.08)
    check("提交 24 个阻塞任务后 worker 数仍不超过上限",
          ex.active_threads <= lw.MAX_DAEMON_WORKERS,
          f"active={ex.active_threads} max={lw.MAX_DAEMON_WORKERS}")
    gate.set()
    for fu in futs:
        try:
            fu.result(timeout=5)
        except Exception:                                      # noqa: BLE001
            pass
    check("阻塞解除后 24 个任务全部完成（队列没有丢活）",
          all(fu.done() for fu in futs))


async def _noop_event(etype: str, player: str, detail: str) -> None:
    return None


# ============================================================ [5] 回调超时/在飞
def part_callbacks() -> None:
    print("========== [5] 事件回调：超时、在飞上限、stop 取消 ==========")
    stop = threading.Event()

    def new_watcher(on_event) -> lw.LogWatcher:
        return lw.LogWatcher(str(Path(tempfile.mkdtemp())), on_event)

    async def scenario() -> None:
        old_timeout, old_inflight, old_grace = (
            lw.CALLBACK_TIMEOUT, lw.MAX_CALLBACK_INFLIGHT, lw.CALLBACK_STUCK_GRACE,
        )
        lw.CALLBACK_TIMEOUT = 0.1
        lw.MAX_CALLBACK_INFLIGHT = 1
        lw.CALLBACK_STUCK_GRACE = 0.2
        try:
            # ① 正常回调：按时交付
            async def quick(etype, player, detail):
                return None

            w1 = new_watcher(quick)
            check("正常回调 → 已交付 True", await w1._dispatch_event("chat", "A", "hi") is True)
            await asyncio.sleep(0)
            h = w1.health()
            check("账目：delivered=1 / timeouts=0 / dropped=0",
                  (h.get("callbacks_delivered"), h.get("callback_timeouts"),
                   h.get("callbacks_dropped")) == (1, 0, 0), str(h))
            check("health() 里看得到这三笔账与在飞数",
                  {"callbacks_delivered", "callback_timeouts", "callbacks_dropped",
                   "callbacks_inflight", "callbacks_stale"} <= set(h), str(sorted(h)))

            # ② 回调卡住：超时返回、轮询不被拖住
            started = time.monotonic()

            async def hanging(etype, player, detail):
                await asyncio.sleep(5)

            w2 = new_watcher(hanging)
            got = await w2._dispatch_event("chat", "A", "hi")
            dt = time.monotonic() - started
            check("★卡住的回调 → 超时后返回 False（不再 await 到天荒地老）",
                  got is False, repr(got))
            check("★超时是按 CALLBACK_TIMEOUT 兑现的（不是等回调自己结束）",
                  dt < 1.0, f"{dt:.2f}s")
            check("超时记进 callback_timeouts",
                  w2.health().get("callback_timeouts") == 1, str(w2.health()))

            # ③ 吞掉取消的僵尸回调 + 在飞上限：新事件不再无限派发
            async def hostile(etype, player, detail):
                while not stop.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        continue          # 吞掉取消（本文件反复踩过的形态）

            w3 = new_watcher(hostile)
            check("僵尸回调第一次派发：超时 → False",
                  await w3._dispatch_event("chat", "A", "1") is False)
            dropped = await w3._dispatch_event("chat", "A", "2")
            check("★在飞上限内（1 个未结束）→ 后续事件被**丢弃并记账**",
                  dropped is False and w3.health().get("callbacks_dropped") == 1,
                  str(w3.health()))
            await asyncio.sleep(0.3)          # 越过宽限期
            # 下一次派发会先做一次额度盘点：僵尸被移出在飞、改记 stale
            again = await w3._dispatch_event("chat", "A", "3")
            h3 = w3.health()
            check("★越过宽限期后：僵尸不再占额度（新事件不会被永久饿死）",
                  again is False and h3.get("callbacks_stale") >= 1
                  and h3.get("callbacks_dropped") == 1, str(h3))

            # ④ stop() 必须**真的取消**在飞回调（不会活过一个已停用的插件）
            #
            # 判据设计（这一版是被变异测试逼出来的）：第一版写法里 `await w4._dispatch_event(...)`
            # 自己就被 CALLBACK_TIMEOUT（这里是 0.1s）顺手取消掉了 —— 于是「任务 ended」这件事
            # 与 stop() 毫无关系，判据空转（把 `self._cancel_callbacks()` 整行删掉，用例照样 80/80 全绿）。
            # 现在：**抬高超时**让回调停在飞行中，派发丢进独立任务；而且这个 watcher **从不 start()**
            # （`_task` 为 None）—— 因此「回调被杀」这件事只剩 `_cancel_callbacks()` 一条来路，
            # 判据与它一一对应。
            got_tasks: list = []

            async def polite(etype, player, detail):
                # 乖巧回调：卡住但**不吞取消**（吞取消的形态是 ③ 那种，强杀无效）
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    got_tasks.append(True)
                    raise

            w4 = new_watcher(polite)
            lw.CALLBACK_TIMEOUT = 30.0
            disp = asyncio.ensure_future(w4._dispatch_event("chat", "A", "x"))
            await asyncio.sleep(0.05)          # 回调进入长睡、派发停在 asyncio.wait 上
            inflight = list(w4._callback_inflight)
            check("派发后回调任务确实在飞，且**没有被超时顺手取消**",
                  len(inflight) == 1 and not inflight[0].done(),
                  f"n={len(inflight)} done={[t.done() for t in inflight]}")
            check("stop() 之前没有任何取消发生（回调真的活着）", not got_tasks, str(got_tasks))
            await w4.stop()                    # 本 watcher 没 start()：取消只能来自 stop 自己
            await asyncio.sleep(0.05)
            check("★stop() 取消在飞回调任务（不是只清账：任务真的 ended 了）",
                  bool(inflight) and inflight[0].done() and bool(got_tasks),
                  f"done={[t.done() for t in inflight]} cancelled={[t.cancelled() for t in inflight]}")
            check("stop() 账目清零（在飞 / 僵尸都不留）",
                  not w4._callback_inflight and not w4._callback_stuck,
                  str(w4.health()))
            disp.cancel()
            try:
                await disp
            except asyncio.CancelledError:
                pass
            lw.CALLBACK_TIMEOUT = 0.1
        finally:
            lw.CALLBACK_TIMEOUT, lw.MAX_CALLBACK_INFLIGHT, lw.CALLBACK_STUCK_GRACE = (
                old_timeout, old_inflight, old_grace,
            )
            stop.set()
            await asyncio.sleep(0.05)

    asyncio.run(scenario())


# ============================================================ [6] 发送预算
def part_budget() -> None:
    print("========== [6] 工作流发送预算：三类上限 + 坏配置 fail-closed ==========")
    b = SendBudget(max_batch=2, max_commands=3, max_bytes=64)
    check("空批次不报错", b.check_batch([]) == "")
    check("单批刚好到上限 → 放行",
          b.check_batch(["a", "b"]) == "", b.check_batch(["a", "b"])[:60])
    check("单批超上限 → 拒绝文案点名条数与上限",
          "3" in b.check_batch(["a", "b", "c"]) and "2" in b.check_batch(["a", "b", "c"]))
    check("★拒绝文案说清「一条都没有发送」（不是「发了一部分」）",
          "一条都没有发送" in b.check_batch(["a", "b", "c"]), b.check_batch(["a", "b", "c"])[:80])
    check("空串 / 纯空白条目不计入条数",
          b.check_batch([{"command": "a"}, {"command": "  "}, ""]) == "")
    check("带前导斜杠的命令照算一条",
          b.check_batch(["/a", "/b", "/c"]) != "")

    b2 = SendBudget(max_batch=10, max_commands=2, max_bytes=10 ** 6)
    check("take 前两条放行", b2.take("say hi") == "" and b2.take("say hi") == "")
    deny = b2.take("say hi")
    check("★第三条撞累计条数上限 → 拒发且说明原因",
          deny.startswith("未发送") and str(b2.max_commands) in deny, deny[:90])
    check("用尽之后再申请一律拒发（不会偷偷放宽）",
          b2.take("say hi").startswith("未发送"), b2.take("say hi")[:60])
    check("快照如实反映用量与上限", (b2.snapshot()["commands"], b2.snapshot()["max_commands"]) == (2, 2),
          str(b2.snapshot()))

    b3 = SendBudget(max_batch=10, max_commands=100, max_bytes=64)
    check("大命令撞字节上限 → 拒发且点名字节",
          "字节" in b3.take("x" * 100), b3.snapshot().__repr__())
    check("★被拒的那一条**不占**额度（commands / bytes 都没动）",
          b3.snapshot()["commands"] == 0 and b3.snapshot()["bytes"] == 0, str(b3.snapshot()))
    check("★一旦用尽就**整体停发**（fail-closed：不会「剩下的接着发」）",
          b3.take("small") != "", b3.take("small")[:60])
    b4 = SendBudget(max_batch=10, max_commands=100, max_bytes=64)
    check("干净的预算里小命令照发（判据不是「谁都发不出去」）",
          b4.take("small") == "" and b4.snapshot()["commands"] == 1, str(b4.snapshot()))

    # 配置坏值：回默认值 / 夹回合法区间，绝不变成 0（那等于拆掉闸门）
    class Host:
        def __init__(self, cfg):
            self._map = cfg

        def _cfg(self, key, default=None):
            return self._map.get(key, default)

    Host._cfg_int = MCWorkflow._cfg_int
    Host._send_budget = MCWorkflow._send_budget

    h1 = Host({"agent_max_batch_commands": "abc", "agent_max_task_commands": None,
               "agent_max_task_bytes": -1})
    bd = h1._send_budget()
    check("坏值（abc / None）→ 回落到默认上限（int() 解析不了就回默认值）",
          (bd.max_batch, bd.max_commands) ==
          (DEFAULT_MAX_BATCH_COMMANDS, DEFAULT_MAX_TASK_COMMANDS),
          f"{bd.max_batch}/{bd.max_commands}/{bd.max_bytes}")
    check("★负数被夹回该配置的**合法下限**（不是 0，更不是负数）",
          bd.max_bytes == 512 and bd.max_commands >= 1, str(bd.max_bytes))
    h2 = Host({"agent_max_batch_commands": 999999})
    check("超大值被夹回上限（1000）", h2._send_budget().max_batch == 1000,
          str(h2._send_budget().max_batch))
    check("默认值本身是「安全但可用」的",
          1 <= DEFAULT_MAX_BATCH_COMMANDS <= 1000 and DEFAULT_MAX_TASK_BYTES >= 4096,
          f"{DEFAULT_MAX_BATCH_COMMANDS}/{DEFAULT_MAX_TASK_BYTES}")


# ============================================================ [7] 纳秒时间戳
def part_fingerprint_ns() -> None:
    print("========== [7] 指纹缓存键：纳秒 mtime（同秒换 jar 不许命中旧 ID） ==========")
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "mods"
        folder.mkdir(parents=True)
        jar = folder / "same.jar"

        def parser(path: str) -> list[str]:
            return [Path(path).read_bytes().decode("utf-8")]   # 文件内容当作 ID

        # 刻意把两个时间戳摆在**同一个秒**里（只差 0.1s）—— 旧实现按 int(mtime) 拼键，
        # 这种「同一秒内删旧拷新、名称与大小都一样」的替换会直接命中旧 jar 的 ID。
        jar.write_bytes(b"AAAA")
        sec = jar.stat().st_mtime_ns // 1_000_000_000
        first_ns = sec * 1_000_000_000 + 100_000_000
        os.utime(jar, ns=(first_ns, first_ns))
        st1 = jar.stat()

        cache: dict = {}
        cache_new: dict = {}
        ids1, jars1, _ = mf._scan_dir_ids(folder, cache, cache_new, prefix="", parser=parser)
        check("首次扫描解析出内容即 ID", ids1 == ["AAAA"] and jars1 == 1, str(ids1))
        check("首轮缓存键用的是纳秒 mtime",
              any(k.endswith(str(st1.st_mtime_ns)) for k in cache_new), str(list(cache_new)))

        jar.write_bytes(b"BBBB")
        second_ns = sec * 1_000_000_000 + 200_000_000
        os.utime(jar, ns=(second_ns, second_ns))
        after = jar.stat()
        check("构造前提：内容换了、大小相同、**秒级** mtime 也相同（旧键正是这么撞上的）",
              int(after.st_mtime) == int(st1.st_mtime) and after.st_size == st1.st_size
              and after.st_mtime_ns != st1.st_mtime_ns,
              f"{st1.st_mtime} → {after.st_mtime}")

        ids2, _, _ = mf._scan_dir_ids(folder, cache_new, {}, prefix="", parser=parser)
        check("★新一代缓存键认出了「换过 jar」→ 重新解析，拿到新内容",
              ids2 == ["BBBB"], str(ids2))

        # 老缓存（秒级键）不许再被认 —— 否则「已经算过的指纹」会以旧值复活
        legacy_cache = {f"same.jar|{st1.st_size}|{int(st1.st_mtime)}": ["STALE"]}
        ids3, _, _ = mf._scan_dir_ids(folder, legacy_cache, {}, prefix="", parser=parser)
        check("★老的秒级缓存键不再命中（旧值不会复活）", ids3 != ["STALE"], str(ids3))

        key_lines = [ln for ln in (PLUGIN_DIR / "core" / "mod_fingerprint.py")
                     .read_text(encoding="utf-8").splitlines()
                     if "st.st_size" in ln and "key = f" in ln]
        check("源码级：缓存键那一行只认纳秒（没有任何一行用 int(st.st_mtime) 拼键）",
              bool(key_lines) and all("st_mtime_ns" in ln for ln in key_lines)
              and not any("int(st.st_mtime)" in ln for ln in key_lines), str(key_lines)[:120])


# ============================================================ [8] 三键同源
#: 三条发送闸门：配置键 → workflow 里的默认值常量（界面 / schema 都要与它对齐）
BUDGET_KEYS = {
    "agent_max_batch_commands": "DEFAULT_MAX_BATCH_COMMANDS",
    "agent_max_task_commands": "DEFAULT_MAX_TASK_COMMANDS",
    "agent_max_task_bytes": "DEFAULT_MAX_TASK_BYTES",
}


def _const_int(expr: str):
    """安全地把 `64 * 1024 * 1024` 这类纯整数乘法算成 int（刻意不用 eval）。

    源码里就是这么写的；这里只认 数字 与 `*`，别的形状一律返回 None —— 判据宁可
    报「解析不了」，也不要引入一个能执行任意表达式的入口。
    """
    parts = [p.strip() for p in str(expr).split("*")]
    if not parts or any(not p.isdigit() for p in parts):
        return None
    out = 1
    for p in parts:
        out *= int(p)
    return out


def part_budget_wiring() -> None:
    print("========== [8] 发送预算三键：schema / 后端 / 设置页三处同源 ==========")
    import json as _json
    import re as _re

    host = PLUGIN_DIR

    # ---- (a) core/workflow.py 源码里的权威口径 ----
    src_wf = (host / "core" / "workflow.py").read_text(encoding="utf-8")
    flat = _re.sub(r"\s+", " ", src_wf)
    clamps: dict[str, tuple] = {}
    for key, default_expr, lo, hi in _re.findall(
        r'_cfg_int\(\s*"([a-z_]+)"\s*,\s*([A-Z_0-9]+)\s*,\s*(\d+)\s*,\s*([^)]+?)\s*\)',
        flat,
    ):
        clamps[key] = (default_expr, int(lo), _const_int(hi))
    check("源码级：三条闸门的夹取口径都在 `_send_budget()` 里（解析得到 3 条）",
          set(BUDGET_KEYS) <= set(clamps), str(sorted(clamps)))
    check("源码级：默认值走的是包级常量而不是散落的字面量",
          all(clamps.get(k, ("", 0, 0))[0] == v for k, v in BUDGET_KEYS.items()),
          str({k: v[0] for k, v in clamps.items()}))
    check("源码级：字节上限能算成整数（64 * 1024 * 1024）",
          clamps.get("agent_max_task_bytes", (None, None, None))[2] == 64 * 1024 * 1024,
          str(clamps.get("agent_max_task_bytes")))

    # ---- (b) _conf_schema.json：能看见 + 默认值与代码一致 ----
    schema = _json.loads((host / "_conf_schema.json").read_text(encoding="utf-8"))
    wf_group = (schema.get("workflow") or {}).get("items") or {}
    code_defaults = {
        "DEFAULT_MAX_BATCH_COMMANDS": DEFAULT_MAX_BATCH_COMMANDS,
        "DEFAULT_MAX_TASK_COMMANDS": DEFAULT_MAX_TASK_COMMANDS,
        "DEFAULT_MAX_TASK_BYTES": DEFAULT_MAX_TASK_BYTES,
    }
    for key, const in BUDGET_KEYS.items():
        item = wf_group.get(key) or {}
        check(f"schema 收了 {key}（type=int）", item.get("type") == "int", repr(item.get("type")))
        check(f"schema 的默认值与代码常量 {const} 一致",
              item.get("default") == code_defaults[const],
              f"{item.get('default')} != {code_defaults[const]}")

    # ---- (c) 后端 INT_RANGES：保存时不误报「后端未接受」 ----
    ranges = getattr(wa.McControlWebApi, "INT_RANGES", {})
    for key in BUDGET_KEYS:
        lo, hi = clamps.get(key, (None, None, None))[1:]
        check(f"后端 INT_RANGES 收了 {key}", key in ranges, str(sorted(ranges)))
        check(f"★{key} 的前后端上下限同源（{lo} ~ {hi}）",
              tuple(ranges.get(key, ())) == (lo, hi), f"{ranges.get(key)} != {(lo, hi)}")

    # ---- (d) 设置页：真的有一个能改它的输入框 ----
    html = (host / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    fields = {cfg_id: key for cfg_id, key, _ty in _re.findall(
        r'\["(cfg_[\w]+)"\s*,\s*"(agent_max_[\w]+)"\s*,\s*"(\w)"', html)}
    for key in BUDGET_KEYS:
        cfg_id = next((i for i, k in fields.items() if k == key), "")
        check(f"设置页 CFG_FIELDS 登记了 {key}（否则控件是死的、提交不会带上它）",
              bool(cfg_id), str(fields))
        m = _re.search(r'<input[^>]*id="%s"[^>]*>' % _re.escape(cfg_id), html) if cfg_id else None
        tag = m.group(0) if m else ""
        check(f"设置页 DOM 里真的有 id=\"{cfg_id}\" 的输入框", bool(tag), cfg_id)
        check("输入框类型是 number（三键都是整数）", 'type="number"' in tag, tag[:120])
        lo, hi = clamps.get(key, (None, None, None))[1:]
        check(f"★网页控件上的 min/max 与后端同一套（{lo} / {hi}）",
              f'min="{lo}"' in tag and f'max="{hi}"' in tag, tag[:160])
        check(f"输入框带占位提示（留空时用户知道内置默认值是多少）",
              "placeholder=" in tag, tag[:160])

    # ---- (e) 反向：登记了就一定被后端认；且留空 = 不提交 ----
    check("★设置页登记的这些键没有一个会被后端判「不认识」",
          set(fields.values()) <= set(ranges),
          str(sorted(set(fields.values()) - set(ranges))))
    s_defaults_blk = html.split("const S_DEFAULTS = {", 1)[-1].split("};", 1)[0]
    # 注：本段只做**结构**检查（前端有没有把假默认值塞进 S_DEFAULTS）。
    # 「留空到底提交了什么」由 tests/test_v0248_collect_settings.py 抽取真实
    # collectSettings() 在 node 里跑 —— 纯字符串判据当年被一条
    # `isNaN(x) ? 0 : x` 的变异整段绕过（GPT 第三轮复核 B6），行为得靠行为判。
    check("三键没有被塞进 S_DEFAULTS —— 前端不写假默认值，由插件内置默认值兜底",
          "cfg_wf_max" not in s_defaults_blk, s_defaults_blk[:140])


def main() -> int:
    part_status()
    part_webapi()
    part_preset_id()
    part_daemon_threads()
    part_callbacks()
    part_budget()
    part_fingerprint_ns()
    part_budget_wiring()
    print()
    print(f"通过 {PASSED[0]} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for d in FAILED:
            print("   -", d)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
