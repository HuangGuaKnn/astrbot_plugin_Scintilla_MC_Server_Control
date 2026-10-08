# -*- coding: utf-8 -*-
"""v0.24.2 回归：配置热应用与监听器生命周期（GPT 全面复核 F03 + F04 + F05）。

F03｜单独切换「异地 RCON 模式」保存成功，本地能力却照跑
  · 病灶：`core/web_api.py` 的两个热应用触发集里都没有 `remote_rcon_mode`，
    而界面**马上**展示「已按设计禁用」。`_restart_event_listener` /
    `_sync_server_context` 本身早支持该模式（会停监听器、清词典与知识库），
    只是保存入口不调用它们。
  · 修法：两个触发集补 `remote_rcon_mode`；另加两道防御门 —— 事件回调在异地模式下
    直接返回（不转发/不桥接），`_kb()` 在异地模式下不再吐存量知识库对象。

F04｜并发保存会留下失去引用的监听器
  · 病灶：`_restart_event_listener` 的「取旧 → await stop → 清字段 → 建新」没有插件级
    互斥；LogWatcher 自己的实例内锁保护不了「换对象」。实测 created=2 / running=2。
  · 修法：插件级生命周期锁（`_listener_lock()`，惰性取用）覆盖整段；`terminate` 复用同一把。

F05｜热重载清理在原生锁之外交错
  · 病灶：补丁先 deep_clean、再调原生 reload（原生 `_pm_lock` 在里面），两次并发重载
    会交错：后一次的清理落在前一次的装载过程中，同一次装载混进两代模块对象。
  · 修法：管理器级锁（挂在 PluginManager 上 —— 本模块自己在清理范围内，模块级变量会被换掉），
    把「清理 + 装载」整段串行化；「主动重载」不再自己预先清一次（那既是锁外清理，
    又会与已加锁的 pm.reload 自锁死），改走同一通道。

跑法：<python> tests\test_v0242_lifecycle.py
"""
from __future__ import annotations

import asyncio
import collections
import importlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []
add_sys_paths()

PASS_N: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS_N.append(desc)
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


try:
    MOD = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.main")
    MAIN_OK = True
    MAIN_ERR = ""
except Exception as e:  # noqa: BLE001
    MOD = None
    MAIN_OK = False
    MAIN_ERR = f"{type(e).__name__}: {e}"


def plugin_cls():
    """从 main 模块里找出插件类（不写死类名）。"""
    for v in vars(MOD).values():
        if isinstance(v, type) and hasattr(v, "_restart_event_listener"):
            return v
    return None


class FakeWatcher:
    """记账用的假 LogWatcher：记 created / started / stopped。"""

    created: list = []

    def __init__(self, root, on_event):
        self.root = root
        self.on_event = on_event
        self.started = False
        self.stopped = False
        FakeWatcher.created.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        await asyncio.sleep(0.03)          # 模拟慢 stop，放大并发窗口
        self.stopped = True


class StubPlugin:
    """只带 _restart_event_listener 需要的依赖（不跑真插件装配）。"""

    def __init__(self, *, listener=True, remote=False, dir_ok=True):
        self.cfg = {"enable_event_listener": listener, "server_dir": "X:/fake_server"}
        self.remote = remote
        self.dir_ok = dir_ok
        self._watcher = None
        self._evt_listener_lock = asyncio.Lock()
        self.logger = logging.getLogger("test.v0242.lifecycle")
        self._recent_events = collections.deque(maxlen=5)
        self.bridged: list = []

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def is_remote_mode(self):
        return self.remote

    def server_dir_check(self, max_age=0):
        return {"ok": self.dir_ok, "errors": ["结构不符（测试替身）"], "suggest_path": ""}

    async def _maybe_bridge_to_group(self, etype, player, detail):
        self.bridged.append((etype, player, detail))
        return False


def stub(cls, **kw):
    """造替身，并把**真的** _listener_lock 绑上去 —— 替身不该假造生命周期锁。"""
    p = StubPlugin(**kw)
    p._listener_lock = cls._listener_lock.__get__(p, type(p))
    p._on_server_event = cls._on_server_event.__get__(p, type(p))
    return p


def live_watchers():
    return [w for w in FakeWatcher.created if w.started and not w.stopped]


async def lifecycle_cases(cls) -> None:
    print("---- 一、F03：只切异地模式键，本地能力必须真的停/起 ----")
    FakeWatcher.created.clear()
    orig = getattr(MOD, "LogWatcher", None)
    MOD.LogWatcher = FakeWatcher
    try:
        p = stub(cls, listener=True, remote=False)
        msg = await cls._restart_event_listener(p)
        check("本地模式：监听器真的起来了", len(live_watchers()) == 1 and p._watcher is not None, msg)

        p.remote = True                                   # ← 只改这一个键
        msg = await cls._restart_event_listener(p)
        check("★切到异地模式：旧监听器**被停掉**（不再是界面说一套、实际还在播）",
              len(live_watchers()) == 0 and p._watcher is None, msg)
        check("★切到异地模式：回执说明「未启动 + 原因」", "异地" in msg, msg)

        p.remote = False                                  # ← 再切回来
        msg = await cls._restart_event_listener(p)
        check("★切回本地模式：监听器**重新起来**（不必重载插件）",
              len(live_watchers()) == 1 and p._watcher is not None, msg)

        # 防御门①：异地模式下事件回调直接返回（不缓存、不转发、不桥接）
        p.remote = True
        p._recent_events.clear()
        await cls._on_server_event(p, "chat", "Steve", "hi")
        check("★异地模式下事件回调防御门：不缓存/不桥接",
              len(p._recent_events) == 0 and p.bridged == [],
              f"{list(p._recent_events)}｜{p.bridged}")
        p.remote = False
        await cls._on_server_event(p, "chat", "Steve", "hi")
        check("本地模式下事件回调照常缓存", len(p._recent_events) == 1, list(p._recent_events))
    finally:
        MOD.LogWatcher = orig

    print("\n---- 二、F04：并发重启只留一个真监听器 ----")
    FakeWatcher.created.clear()
    orig = getattr(MOD, "LogWatcher", None)
    MOD.LogWatcher = FakeWatcher
    try:
        p = stub(cls, listener=True, remote=False)
        await asyncio.gather(cls._restart_event_listener(p), cls._restart_event_listener(p))
        check("★并发两次重启：活着的监听器**恰好 1 个**",
              len(live_watchers()) == 1, f"created={len(FakeWatcher.created)} live={len(live_watchers())}")
        check("★并发两次重启：字段持有的就是那个活着的实例",
              p._watcher is not None and p._watcher in live_watchers(), repr(p._watcher))
        check("★并发两次重启：没有失去引用的孤儿（全部已被 stop）",
              all(w.stopped for w in FakeWatcher.created if w is not p._watcher),
              [w.stopped for w in FakeWatcher.created])
    finally:
        MOD.LogWatcher = orig

    check("生命周期锁是同一把（惰性取用不换对象）",
          cls._listener_lock(p) is cls._listener_lock(p))

    print("\n---- 三、源代码级判据（防回退）----")
    wa = (Path(__file__).resolve().parents[1] / "core" / "web_api.py").read_text(encoding="utf-8")
    check("F03：监听器触发集含 remote_rcon_mode",
          '"enable_event_listener", "server_dir", "remote_rcon_mode"' in wa)
    check("F03：词典/知识库触发集含 remote_rcon_mode",
          '"server_dir", "dictionary_enabled", "knowledge_enabled", "remote_rcon_mode"' in wa)
    check("F03：_kb() 有异地防御门", "is_remote_mode" in wa.split("def _kb(")[1][:600])
    mn = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    check("F04：_restart_event_listener 走生命周期锁",
          "async with self._listener_lock():" in mn)
    check("F04：terminate 复用同一把锁", "async with self._listener_lock():" in mn.split("async def terminate")[1][:1400])


def hot_reload_cases() -> None:
    print("\n---- 四、F05：重载期串行化 ----")
    try:
        hr = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.hot_reload")
        from astrbot.core.star.star_manager import PluginManager
    except Exception as e:  # noqa: BLE001
        skip(f"取不到 AstrBot 的 PluginManager：{e}")
        return

    lk1 = hr._reload_lock(PluginManager)
    check("锁挂在 PluginManager 上（跨重载存活，不随本模块被清掉）",
          hasattr(PluginManager, hr._RELOAD_LOCK_ATTR), hr._RELOAD_LOCK_ATTR)
    check("同一把锁：两次取用是同一对象", lk1 is hr._reload_lock(PluginManager))

    order: list = []

    def fake_deep_clean(pm, name=None, **kw):
        order.append("clean")
        return {"modules": 1, "pycache": 0, "dirs": [], "module_names": [], "prefixes": []}

    async def fake_original(self, name=None):
        order.append("load-begin")
        await asyncio.sleep(0.05)
        order.append("load-end")
        return True, "ok"

    class FakePM:
        pass

    saved = (getattr(PluginManager, hr._IMPL_ATTR, None), getattr(PluginManager, hr._ORIG_ATTR, None),
             getattr(PluginManager, "reload", None))
    installed = hr.install_reload_patch()
    try:
        setattr(PluginManager, hr._IMPL_ATTR, staticmethod(fake_deep_clean))
        setattr(PluginManager, hr._ORIG_ATTR, fake_original)
        pm = FakePM()

        async def run_two():
            await asyncio.gather(PluginManager.reload(pm), PluginManager.reload(pm))

        asyncio.run(run_two())
        interleaved = "clean" in order[1:2] and order[:3] != ["clean", "load-begin", "load-end"]
        check("★并发两次重载不交错（清理不会落进别人的装载过程）",
              order[:4] == ["clean", "load-begin", "load-end", "clean"], str(order))
        check("两个回合都跑完了（没有死锁）", order.count("load-end") == 2, str(order))
    finally:
        if saved[0] is not None:
            setattr(PluginManager, hr._IMPL_ATTR, saved[0])
        if saved[1] is not None:
            setattr(PluginManager, hr._ORIG_ATTR, saved[1])
        if not installed and saved[2] is not None:
            PluginManager.reload = saved[2]

    src = (Path(__file__).resolve().parents[1] / "core" / "hot_reload.py").read_text(encoding="utf-8")
    check("主动入口不再自己预先清一次（8 空格缩进那行已消失）",
          "\n        summary = deep_clean(pm, name)\n" not in src)
    check("主动入口按补丁是否安装分流", "if is_patch_installed():" in src)


def main() -> int:
    if not MAIN_OK:
        skip(f"取不到插件主模块：{MAIN_ERR}")
        hot_reload_cases()
    else:
        cls = plugin_cls()
        if cls is None:
            skip("main 模块里找不到带 _restart_event_listener 的插件类")
        else:
            print("=" * 78)
            print("v0.24.2 回归 · 配置热应用与监听器生命周期（F03 / F04 / F05）")
            print("=" * 78)
            asyncio.run(lifecycle_cases(cls))
        hot_reload_cases()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
