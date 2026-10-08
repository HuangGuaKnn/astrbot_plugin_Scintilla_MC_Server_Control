# -*- coding: utf-8 -*-
"""v0.24.3 回归 · N02 + N03：配置代际与生命周期。

GPT v0.24.2 复核原句
====================

* **N02**：完整配置热应用未串行，旧服务端上下文能覆盖新配置。并发保存时慢刷新提交
  旧 context；旧刷新还能恢复异地模式已关闭的本地能力。要求：插件级锁覆盖配置快照、
  写入及全部热应用，其他刷新入口共用；或使用代际号，每个 await 后提交状态前检查是否
  仍为当前代际；`web_api.py:1645` 的 `_kbman` 也应具有当前模式兜底。
* **N03**：terminate 后，已排队的 restart 会重新启动监听器。要求：terminate 第一个
  await 前设置不可逆终止标志；initialize/restart/terminate 使用同一生命周期入口，
  拿锁后与发布新 watcher 前均检查状态，排队旧请求必须失效。

本用例守四件事
==============

1. **配置写入即换代**，且「本地上下文签名」只随**真正相关**的键变化（改无关项不误伤）。
2. **慢刷新不许盖回**：指纹 / 词典算到一半发生新保存 → 身份与词典**都不提交**
   （两个 await 窗口各一条真跑用例）。
3. **终止不可逆**：terminate 与 restart 交错的两种次序下，最终都必须 watcher=None、
   没有活着的监听任务。
4. **预设入口兜底**：本地文件能力不可用时，`_kbman()` 一律视为没有管理器。

fail-closed 声明：缺运行依赖（import 失败）判 **FAIL**，不判 SKIP。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

FAIL: list[str] = []

add_sys_paths()
try:
    import importlib

    M = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.main")
    WA = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.web_api")
    IMPORT_OK = True
    IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    IMPORT_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"

log = logging.getLogger("v0243-cfg-lifecycle")
log.addHandler(logging.NullHandler())

MAIN_SRC = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
API_SRC = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------- 替身 ----------------

def make_plugin(cfg: dict | None = None):
    p = M.McControlPlugin.__new__(M.McControlPlugin)
    p.config = dict(cfg or {
        "server_dir": "/tmp/sd-never", "remote_rcon_mode": False,
        "knowledge_enabled": True, "dictionary_enabled": True,
        "enable_event_listener": True,
    })
    p._cfg_index = {}
    p.logger = log
    p._save_config = lambda: True
    p._kbman = None
    p._knowledge = None
    p._server_identity = None
    p._dictionary = None
    p._watcher = None
    p._rcon = None
    p._hot_reload_token = None
    p._terminated = False
    p._bg = FakeBG()
    p.is_remote_mode = lambda: bool(p._cfg("remote_rcon_mode", False))
    p.server_dir_check = lambda **kw: {"ok": True, "errors": []}
    p._kb_engine = lambda: "bm25"
    p._kb_semantic = lambda: False
    p._kb_rerank = lambda: False
    p._apply_active_knowledge = lambda: None
    p._resolve_version_info = lambda: type("V", (), {"mc": None})()
    p._on_server_event = lambda *a, **k: None
    return p


class FakeKBMan:
    def __init__(self, kdir, kid, server_dir, **kw):
        self.server_id = kid
        self.server_dir = server_dir

    def set_server_id(self, kid, server_dir=None, weak=None):
        self.server_id = kid


class FakeBG:
    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.tasks = []

    async def shutdown(self):
        await asyncio.sleep(self.delay)
        return len(self.tasks)

    def spawn(self, *a, **k):
        return None


class FakeWatcher:
    all: list = []

    def __init__(self, server_dir, on_event):
        self.server_dir = server_dir
        self.running = False
        FakeWatcher.all.append(self)

    async def start(self):
        await asyncio.sleep(0.01)          # 自然 await —— 给 terminate 留插入窗口
        self.running = True

    async def stop(self):
        await asyncio.sleep(0)
        self.running = False


IDENT = {"fingerprint": "FP-1", "summary": "srv A", "weak": False, "kind": "forge"}


def patch_module(**kw):
    """临时替换 main 模块级符号，返回 (原值字典)。"""
    old = {}
    for name, val in kw.items():
        old[name] = getattr(M, name)
        setattr(M, name, val)
    return old


def restore(old: dict) -> None:
    for name, val in old.items():
        setattr(M, name, val)


# ===================== 一、代际与签名 =====================

def group_gen() -> None:
    print("---- 一、配置代际 / 本地上下文签名 ----")
    p = make_plugin()
    check("代际初值为 0", p._cfg_gen() == 0, str(p._cfg_gen()))
    p._set_cfg_batch({"chat_bridge_targets": ["a"]})
    n1 = p._cfg_gen()
    check("★写入一次配置 → 代际 +1", n1 == 1, str(n1))
    sig = p._ctx_sig()
    p._set_cfg_batch({"chat_bridge_targets": ["b"]})
    check("★再写一次 → 代际 +1", p._cfg_gen() == 2, str(p._cfg_gen()))
    check("★改无关项不改签名（不该让一次重算白跑）", p._ctx_sig() == sig)
    check("签名未变时「取代判据」为空", p._stale_context(sig, n1) == "")

    for key, val in (("server_dir", "/tmp/other"), ("remote_rcon_mode", True),
                     ("dictionary_enabled", False), ("knowledge_enabled", False),
                     ("enable_event_listener", False)):
        q = make_plugin()
        s0 = q._ctx_sig()
        q._set_cfg_batch({key: val})
        check(f"★{key} 变化 → 签名改变 + 取代判据有话说",
              q._ctx_sig() != s0 and bool(q._stale_context(s0, 0)), key)
    check("签名覆盖的关键键都在 _CTX_KEYS 里（可审计）",
          {"server_dir", "remote_rcon_mode"} <= {k for k, _ in M.McControlPlugin._CTX_KEYS})


# ===================== 二、慢刷新不许盖回（N02 靶心） =====================

def group_stale_sync() -> None:
    print("\n---- 二、慢刷新不许盖回新配置 ----")

    def runner(p, *, csi_mutate=None, dict_mutate=None):
        class FakeDict:
            def __init__(self, sd, cache):
                self.sd = sd
                self.mc_hint = None

            def build(self):
                if dict_mutate:
                    dict_mutate(p)
                return {"mods": 0, "items": 0, "recipes": 0}

        def fake_csi(server_dir, cache):
            if csi_mutate:
                csi_mutate(p)
            return dict(IDENT)

        old = patch_module(compute_server_identity=fake_csi,
                           KnowledgePresetManager=FakeKBMan,
                           ItemDictionary=FakeDict)
        try:
            return run(p._sync_server_context(rebuild_dictionary=True))
        finally:
            restore(old)

    # ① 对照：无并发写入 → 正常提交
    p = make_plugin()
    txt = runner(p)
    check("★① 对照：无并发写入时身份正常提交",
          bool(p._server_identity) and p._server_identity.get("fingerprint") == "FP-1",
          repr(p._server_identity))
    check("① 对照：回执里没有任何「作废」字样", "取代" not in txt, txt[:120])

    # ② 指纹算到一半被新保存取代 → 不许提交身份、不许建知识库
    p = make_plugin()
    txt = runner(p, csi_mutate=lambda q: q.config.__setitem__("server_dir", "/tmp/sd-new"))
    check("★② 指纹算到一半换了 server_dir → 身份**不提交**",
          p._server_identity is None and p._kbman is None, repr(p._server_identity))
    check("★② 回执如实说明「已被取代」", "取代" in txt, txt[:160])

    # ③ 词典建到一半被新保存取代 → 不许挂上词典
    p = make_plugin()
    txt = runner(p, dict_mutate=lambda q: q.config.__setitem__("remote_rcon_mode", True))
    check("★③ 词典建到一半开了异地模式 → 词典**不挂上**", p._dictionary is None)
    check("★③ 回执如实说明「已被取代」", "取代" in txt, txt[:160])

    # ④ ③ 的终态：被取代的那一次**什么都没提交**（词典保持原样）
    #    注：身份段在此之前就已提交，那是**当时**配置下的合法结果 ——
    #    真实场景里随后那次保存会自己再跑一遍 _sync_server_context（异地模式下会把
    #    知识库/词典清空），所以终态仍与最新配置一致；这里只断言本次不覆盖。
    check("★④ 被取代的那一次不许留下任何半成品：词典仍为空",
          p._dictionary is None, repr(p._dictionary))


# ===================== 三、终止不可逆（N03 靶心） =====================

def group_lifecycle() -> None:
    print("\n---- 三、终止不可逆：排队中的 restart 不许复活监听器 ----")
    old = patch_module(LogWatcher=FakeWatcher)
    try:
        # ① terminate 与 restart 同时排队
        FakeWatcher.all.clear()
        p = make_plugin()
        p._rcon = None
        p._hot_reload_token = "x"
        p._bg = FakeBG()

        async def case1():
            return await asyncio.gather(p.terminate(), p._restart_event_listener())

        r = run(case1())
        check("★① terminate 与 restart 交错后：watcher 为 None",
              p._watcher is None, repr(p._watcher))
        check("★① 没有任何活着的监听任务",
              not any(w.running for w in FakeWatcher.all),
              repr([w.running for w in FakeWatcher.all]))
        check("① restart 如实回报「已终止」，不谎报成功",
              "终止" in r[1], r[1])

        # ② restart 先拿到锁、正在启动时 terminate 插入
        FakeWatcher.all.clear()
        p = make_plugin()
        p._rcon = None
        p._hot_reload_token = "x"
        p._bg = FakeBG()

        async def case2():
            t = asyncio.create_task(p._restart_event_listener())
            await asyncio.sleep(0.002)
            await p.terminate()
            return await t

        msg2 = run(case2())
        check("★② restart 已在启动途中被 terminate 撞上：watcher 仍为 None",
              p._watcher is None, repr(p._watcher))
        check("★② 那个刚起来的监听器必须当场被收掉（不许存活）",
              not any(w.running for w in FakeWatcher.all),
              repr([w.running for w in FakeWatcher.all]))
        check("② 回执不谎报成功（没写成「已重启并生效」）",
              "已重启并生效" not in msg2, msg2)

        # ③ 已终止实例：发布口一律拒绝（initialize 侧也走它）
        p = make_plugin()
        p._terminated = True

        async def pub():
            async with p._listener_lock():
                return await p._publish_watcher("/tmp/sd-never")

        check("★③ 已终止实例再发布 → False", run(pub()) is False)
        check("③ 已终止实例的 restart 也拒绝并说明原因",
              "终止" in run(p._restart_event_listener()))
        check("③ 终止态判据在标记未置位时为 False", make_plugin()._terminated_now() is False)
    finally:
        restore(old)


# ===================== 四、预设入口兜底 + 静态牙齿 =====================

def group_api() -> None:
    print("\n---- 四、预设入口兜底 / 静态牙齿 ----")
    api = WA.McControlWebApi.__new__(WA.McControlWebApi)

    class FakePlugin:
        def __init__(self, man, reason):
            self._kbman = man
            self._reason = reason

        def local_files_degraded_reason(self):
            return self._reason

    man = object()
    api.plugin = FakePlugin(man, "异地 RCON 模式：本地能力已按设计禁用")
    check("★异地/目录失效时 _kbman() 返回 None（不许被复活对象重新开放本地操作）",
          api._kbman() is None)
    api.plugin._reason = ""
    check("本地能力正常时 _kbman() 返回真身", api._kbman() is man)
    api.plugin._reason = "未配置服务器目录"
    check("未配置目录时同样返回 None", api._kbman() is None)

    class Bare:
        _kbman = man

    api.plugin = Bare()
    check("替身插件（没有降级原因方法）时不炸、按原样返回", api._kbman() is man)

    # 静态牙齿
    import re as _re

    term = MAIN_SRC.split("async def terminate(self):", 1)[1][:900]
    # 剥掉注释行再找 —— 否则会被皮莉卡自己注释里写的「第一个 await」骗到
    code = "\n".join(l for l in term.split("\n") if not l.strip().startswith("#"))
    flag_at = code.find("self._terminated = True")
    _m = _re.search(r"^\s*await ", code, _re.M)
    first_await = _m.start() if _m else 10 ** 9
    check("★terminate 的终止标记写在**第一个 await 之前**",
          0 <= flag_at < first_await, f"flag@{flag_at} await@{first_await}")
    check("★initialize 也走同一生命周期入口（不再各写一份发布代码）",
          "await self._publish_watcher(server_dir)" in MAIN_SRC
          and MAIN_SRC.count("LogWatcher(") == 1, str(MAIN_SRC.count("LogWatcher(")))
    check("★_restart_event_listener 里两处复核（拿锁后 / 发布前）",
          MAIN_SRC.count("_stale_note(self, sig, gen)") >= 4
          and MAIN_SRC.count("_is_terminated(") >= 6,
          f"stale={MAIN_SRC.count('_stale_note(self, sig, gen)')} "
          f"term={MAIN_SRC.count('_is_terminated(')}")
    sync_body = MAIN_SRC.split("async def _sync_server_context", 1)[1]
    commit_at = sync_body.find("self._server_identity = ident")
    stale_at = sync_body.find("_stale_note(self, sig, gen)")
    check("★身份提交点前面有取代复核", 0 <= stale_at < commit_at,
          f"stale@{stale_at} commit@{commit_at}")
    check("★词典先建到局部变量、复核后才提交（不再边建边挂）",
          "dic = ItemDictionary(server_dir" in sync_body
          and sync_body.find("self._dictionary = dic") > sync_body.find("stats = await asyncio.to_thread(dic.build)"))
    check("★save_settings 有「已被更新保存取代」守卫",
          "def _superseded()" in API_SRC and API_SRC.count("not superseded") >= 4,
          str(API_SRC.count("not superseded")))
    check("web_api 有插件签名入口（替身缺方法也不炸）",
          "def _plugin_ctx_sig" in API_SRC)


def main() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · N02 + N03 配置代际与生命周期")
    print("=" * 78)
    if not IMPORT_OK:
        check("插件可导入（缺运行依赖必须判 FAIL 而不是 SKIP）", False, IMPORT_ERR)
        print("\n失败项：" + " / ".join(FAIL))
        return 1
    group_gen()
    group_stale_sync()
    group_lifecycle()
    group_api()
    print("\n================ 汇总 ================")
    print("失败 %d 项" % len(FAIL))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
