# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第五轮）回归：越权检索 / 补算缺口 / 失败可见性 / 监听生命周期 / 发版门禁。

第四轮修的是「有一处真相，却没人去看」；本批（GPT 第五轮）修的是**同一类病的另外几处**，
外加一处**流程**上的洞：

  A. 精排越权：`_search_with_rerank` 把 `_rank_candidates` 的输出**整段**当候选池，
     被禁用 / 待审批 / 已删除的条目照样进池、照样精排、照样返回 —— 精排开关一开，
     「手工禁用」形同虚设；而且精排是网络往返，回来时条目可能已经变了。
  B. 补算缺口：`copy / move / switch` 之后新实例可能「条目在、向量不在」，
     既没有写入也不是启动，补算钩子永远不响 —— 复制过来的知识长期搜不到。
  C. 失败可见性：预设文件写失败会被紧随其后的「注册表写成功」覆盖成 True；
     移动的「源库清空失败」warning 在 WebAPI 层被丢掉；指纹回写的失败进不了写入响应。
  D. 监听生命周期：`start()` 不幂等（旧任务永远停不掉）、初始化文件 IO 占着事件循环、
     没探测过却自称「文件在」、孤儿任务的异常没人取。
  E. 发版门禁：release.yml 只跑 test_*.py，**UI 契约类一条都没跑** —— 打 tag 直接发版
     可以绕过整个 UI 契约层；`.github` 里的清单还是手写的第二份。
     第五轮上云实测（第一次把 UI 用例真搬上 ubuntu-latest，run 37144699301）又逮到
     下一层问题：**9 件契约类里 5 件在 CI 上跑不过**（一件硬编码本机路径 = 真 bug；
     其余判据里含本机环境）→ 于是清单多一份 `CI_OK` 白名单，CI 与发版只跑实测能跑的，
     其余留本机全量复现；`--ui-set=ci-*` 让 tests.yml 的两个 job 也走同一套跑测循环。

运行：
  <AstrBot python> tests\\test_v0235_review_round5.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as KB  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    KnowledgePresetManager,
    ModKnowledgeBase,
)

PLUGIN = PLUGIN_DIR
ROOT = PLUGIN          # 仓库根 = 插件目录（run_release_verify.py / .github 都在这里）
sys.path.insert(0, str(PLUGIN))
import run_release_verify as RV  # noqa: E402

RELEASE_VERIFY_SRC = io.open(ROOT / "run_release_verify.py", encoding="utf-8").read()

MAIN_SRC = io.open(PLUGIN / "main.py", encoding="utf-8").read()
KB_SRC = io.open(PLUGIN / "core" / "knowledge_base.py", encoding="utf-8").read()
LW_SRC = io.open(PLUGIN / "core" / "log_watcher.py", encoding="utf-8").read()
WEB_SRC = io.open(PLUGIN / "core" / "web_api.py", encoding="utf-8").read()
REL_YML = io.open(PLUGIN / ".github" / "workflows" / "release.yml", encoding="utf-8").read()
TESTS_YML = io.open(PLUGIN / ".github" / "workflows" / "tests.yml", encoding="utf-8").read()
ALL_RUNNER = io.open(PLUGIN / "run_v0230_all.py", encoding="utf-8").read()

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name} {extra}")


def fn_src(src: str, name: str, cls: str = "") -> str:
    """按 AST 摘出某个函数 / 方法的**源码原文**（静态断言用，不复制逻辑）。"""
    def _find(container) -> str:
        for node in container:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(src, node) or ""
            if isinstance(node, ast.ClassDef):
                got = _find(node.body)
                if got:
                    return got
        return ""

    if cls:
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                got = _find(node.body)
                if got:
                    return got
        raise AssertionError(f"找不到 {cls}.{name}")
    got = _find(ast.parse(src).body)
    if not got:
        raise AssertionError(f"找不到 {name}")
    return got


def load_funcs(*names: str, **globs):
    """把 main.py 的函数本体摘出来跑（main.py 不能在测试里 import）。"""
    ns: dict = dict(globs)
    ns.setdefault("asyncio", asyncio)
    body = []
    for node in ast.walk(ast.parse(MAIN_SRC)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            body.append(node)
    assert len(body) == len(names), f"main.py 里缺函数：{names}"
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<main-fn>", "exec"), ns)
    return ns


def make_watcher(tmp: Path):
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    w = LW.LogWatcher(str(tmp), on_event, poll_interval=0.01)
    w._enc = "utf-8"
    return w, seen


def run_loop_briefly(w, seconds: float = 0.08) -> None:
    """让 `_loop` 真跑一小会儿（跑真源码，不是复制它的逻辑）。"""
    w._running = True

    async def _run():
        t = asyncio.create_task(w._loop())
        await asyncio.sleep(seconds)
        w._running = False
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


# ==================================================================== A. 精排越权

async def _embed(texts):
    return [[1.0] + [0.0] * 15 for _ in texts]


def part_a() -> None:
    print("================ [A] 精排不许越过「禁用 / 待审批 / 已删除」 ================")
    static = fn_src(KB_SRC, "_search_with_rerank", cls="ModKnowledgeBase")
    check("静态：进池前就按 _searchable() 过滤（不是建完 docs 再想补救）",
          "if t in self._searchable()][:RERANK_POOL]" in static)
    check("静态：精排回来后**重取**条目表再判一遍（网络往返期间可能已变）",
          static.count("entries = self._searchable()") >= 2)
    check("静态：池里的 topic 现在不在可检索集合里 → 跳过（旧写法直接 KeyError）",
          "if topic not in entries:" in static)

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p",
                              semantic_enabled=True, rerank_enabled=True)
        kb.embed_fn = _embed
        kb.save_entry("条目甲", "黄铜锭怎么合成", status="verified")
        kb.save_entry("条目乙", "黄铜锭怎么合成", status="verified")
        kb.save_entry("条目丙", "黄铜锭怎么合成", status="pending")     # 待审批
        kb.set_enabled("条目乙", False)                                # 手工禁用

        # 现场：语义通道把三条都召回了（禁用 / 待审批的向量还留在索引里）
        kb._rank_candidates = lambda q, qv=None: ["条目乙", "条目丙", "条目甲"]
        docs_seen: list = []

        async def rerank(query, docs):
            docs_seen.append(list(docs))
            return [(i, 1.0 - i * 0.1) for i in range(len(docs))]

        kb.rerank_fn = rerank
        out = asyncio.run(kb.asearch("黄铜锭"))
        topics = [e["topic"] for e in out]
        check("被禁用 / 待审批的条目**进不了精排池**（而不是排完再筛）",
              docs_seen and len(docs_seen[0]) == 1, str(docs_seen))
        check("结果里没有禁用 / 待审批的条目", "条目乙" not in topics and "条目丙" not in topics,
              str(topics))

        # 精排是一网络往返：等待期间主人把它禁用了 / 删了
        async def rerank_disable(query, docs):
            kb.set_enabled("条目甲", False)
            return [(0, 0.99)]

        kb.rerank_fn = rerank_disable
        out2 = asyncio.run(kb.asearch("黄铜锭"))
        check("等待精排期间被禁用 → 回来照样不放行",
              all(e["topic"] != "条目甲" for e in out2), str([e["topic"] for e in out2]))

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p",
                              semantic_enabled=True, rerank_enabled=True)
        kb.embed_fn = _embed
        for t in ("条目甲", "条目乙", "条目丙"):
            kb.save_entry(t, "黄铜锭怎么合成", status="verified")
        kb._rank_candidates = lambda q, qv=None: ["条目甲", "条目乙", "条目丙"]

        async def rerank_delete(query, docs):
            kb.delete_entry("条目乙")          # 等待期间被删掉
            return [(1, 0.99), (0, 0.5)]       # 还给「已删掉的那条」打最高分

        kb.rerank_fn = rerank_delete
        try:
            out = asyncio.run(kb.asearch("黄铜锭"))
            crashed = ""
        except Exception as e:                              # noqa: BLE001
            out, crashed = [], f"{type(e).__name__}: {e}"
        check("等待精排期间被删除 → 不崩（旧写法 KeyError：整次检索炸掉）",
              crashed == "", crashed)
        check("也不返回已删除的条目",
              "条目乙" not in [e["topic"] for e in out], str([e["topic"] for e in out]))


# ============================================================ B. 切 / 复制 / 移动后补算

def part_b() -> None:
    print("================ [B] 切换 / 复制 / 移动预设后缺的向量要补上 ================")
    check("静态：semantic_stats 的 pending 读 pending_vectors（口径只此一处）",
          "pending = self.pending_vectors()" in fn_src(KB_SRC, "semantic_stats",
                                                       cls="ModKnowledgeBase"))
    check("静态：pending_vectors() 是独立方法（别处也能读同一判据）",
          "def pending_vectors" in KB_SRC)
    apply_src = fn_src(MAIN_SRC, "_apply_active_knowledge")
    check("静态：切完预设就地判定「缺向量 → 补算」",
          "kb.pending_vectors()" in apply_src and "self._kb_touch_vectors()" in apply_src,
          apply_src[-200:])

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p",
                              semantic_enabled=True)
        kb.embed_fn = _embed
        kb.save_entry("条目甲", "黄铜锭怎么合成", status="verified")
        check("新条目还没算向量 → pending_vectors() 如实报 1", kb.pending_vectors() == 1)
        check("semantic_stats 与它同源", kb.semantic_stats()["pending"] == 1)
        asyncio.run(kb.build_vectors())
        check("补算完归零", kb.pending_vectors() == 0
              and kb.semantic_stats()["pending"] == 0)

        kb2 = ModKnowledgeBase(str(td), server_id="stub", preset_id="q")
        kb2.save_entry("条目甲", "内容", status="verified")
        check("语义通道没开 → 恒为 0（不放空炮）", kb2.pending_vectors() == 0)

    # 端到端：复制出来的预设，切过去就该自动补算
    with tempfile.TemporaryDirectory() as td:
        dd = Path(td)
        man = KnowledgePresetManager(str(dd), "sid", str(dd), semantic_enabled=True)
        src = man.active_preset()["id"]
        man.kb.embed_fn = _embed
        man.kb.save_entry("条目甲", "黄铜锭怎么合成", status="verified")
        new = man.create("副本", copy_from=src)
        man.switch(new["id"])

        calls = {"touch": 0}

        class Host:
            def __init__(self):
                self._kbman = man
                self._knowledge = None
                self.semantic = True

            def _kb_semantic(self):
                return self.semantic

            def _kb_touch_vectors(self):
                calls["touch"] += 1

        fn = load_funcs("_apply_active_knowledge")["_apply_active_knowledge"]
        host = Host()
        fn(host)
        check("复制过来的条目确实缺向量（现场成立）", man.kb.pending_vectors() > 0)
        check("切过去就排了补算（旧写法：一直挂着，永远搜不到）",
              calls["touch"] == 1, str(calls))

        host.semantic = False
        fn(host)
        check("语义通道关着 → 不排补算（不烧额度）", calls["touch"] == 1, str(calls))

        man.kb.embed_fn = _embed          # 切换后是新实例，注入照旧得跟上
        asyncio.run(man.kb.build_vectors())
        host.semantic = True
        fn(host)
        check("向量齐了 → 不再重复排（幂等）", calls["touch"] == 1, str(calls))


# ================================================== C. 落盘失败 / warning 不许被吃掉

def part_c() -> None:
    print("================ [C] 失败可见性：统一落盘健康度 + warning 不许丢 ================")
    with tempfile.TemporaryDirectory() as td:
        kd = Path(td)
        man = KnowledgePresetManager(str(kd), "sid", str(kd))
        r0 = man.create("甲")
        check("对照组：正常新建 → 聚合 save_ok=True", r0.get("save_ok") is True,
              str(r0.get("save_error")))

        # 预设文件必然写不进去（父目录不存在），注册表照旧写得进去
        good_dir = man.data_dir
        man.data_dir = kd / "没有这个目录" / "更深"
        r1 = man.create("乙")
        check("预设文件写失败 → 本次回包 save_ok=False",
              r1.get("save_ok") is False and "预设文件写入失败" in str(r1.get("save_error")),
              str(r1.get("save_error")))
        h = man.save_health()
        check("失败没被紧接着的「注册表写成功」抹掉（第五轮 P2 核心）",
              h["ok"] is False and h["parts"]["preset_file"]["ok"] is False
              and h["parts"]["registry"]["ok"] is True, str(h))
        st = man.status()
        check("状态接口读的是**聚合**口径（旧写法这里显示 True）",
              st.get("registry_save_ok") is False
              and st.get("save_health", {}).get("parts", {}).get("preset_file", {}).get("ok")
              is False, str(st.get("save_health")))
        check("失败原因写明是哪一步（人话）",
              "预设文件写入失败" in str(h["error"]), str(h["error"]))

        man.data_dir = good_dir
        r2 = man.create("丙")
        check("这个部件自己下次写成功 → 红被清掉（不是记死）",
              r2.get("save_ok") is True
              and man.save_health()["parts"]["preset_file"]["ok"] is True,
              str(man.save_health()))

        man.reg_path = kd / "没有这个目录" / "registry.json"
        r3 = man.create("丁")
        h3 = man.save_health()
        check("反方向也成立：注册表红了，不会因为预设文件写成功而变绿",
              r3.get("save_ok") is False and h3["parts"]["registry"]["ok"] is False
              and h3["parts"]["preset_file"]["ok"] is True, str(h3))

    create_src = fn_src(KB_SRC, "create", cls="KnowledgePresetManager")
    remove_src = fn_src(KB_SRC, "remove", cls="KnowledgePresetManager")
    persist_src = fn_src(KB_SRC, "_persist_registry", cls="KnowledgePresetManager")
    check("静态：create / remove 都走同一本账（不再就地合并、被下一次成功覆盖）",
          create_src.count("_note_save(self.SAVE_PART_PRESET_FILE") == 1
          and remove_src.count("_note_save(self.SAVE_PART_PRESET_FILE") == 1)
    check("静态：注册表与预设文件记的是**不同部件**（互不覆盖）",
          "_note_save(self.SAVE_PART_REGISTRY" in persist_src
          and "SAVE_PART_PRESET_FILE = \"preset_file\"" in KB_SRC)

    # 指纹回写失败 → 同一次写入的响应也不许说「存好了」
    # 注入方式必须**跨平台**（第五轮第一次在 CI 上跑这条用例时踩到的坑）：
    #   · Windows：把预设文件设成只读 → 条目落盘走「写 .tmp 再 rename」，rename 顶不动
    #     只读文件 → 失败；但失败发生在**条目落盘**那一步，不是这条用例想测的指纹那一步；
    #   · Linux：rename 只看**目录**权限 → 只读的目标文件被新的可写文件顶替，条目落盘
    #     成功、指纹回写也成功 → 断言直接红（本机绿、CI 红）。
    # 所以改成按**目标路径**注入：只让「写到预设文件本体」的那一次（= `_stamp_fp` 里的
    # `write_text`，条目落盘写的是 `.tmp` 兄弟文件）抛 PermissionError ——
    # 抛的异常类型与磁盘满 / 只读盘时一样，走的是被测代码里那条真正的 except 分支。
    with tempfile.TemporaryDirectory() as td:
        kd = Path(td)
        man = KnowledgePresetManager(str(kd), "sid2", str(kd))
        man.create("甲")
        kb = man.kb
        kb.fingerprint = None                      # 模拟「首次写入要绑指纹」
        p = man.preset_path(kb.preset_id)
        p.write_text("{}", encoding="utf-8")
        real_write_text = Path.write_text
        hit = {"n": 0}

        def _flaky_write_text(self, *a, **k):
            if Path(self) == p:                    # 预设文件本体（不是 .tmp 兄弟）
                hit["n"] += 1
                raise PermissionError("注入：指纹回写时盘不可写（模拟只读 / 磁盘满）")
            return real_write_text(self, *a, **k)

        Path.write_text = _flaky_write_text
        try:
            kb.save_entry("新条目", "内容", status="verified")
        finally:
            Path.write_text = real_write_text
        check("注入确实命中了指纹回写那一次写入（第 2 步，不是条目落盘）",
              hit["n"] == 1, f"命中 {hit['n']} 次")
        check("指纹回写失败 → 这次写入的响应也不许说「存好了」",
              kb.last_save_ok is False and "指纹回写失败" in kb.last_save_error,
              f"{kb.last_save_ok} / {kb.last_save_error}")

    # 移动：源库清空失败，界面不许显示成「移动成功」
    transfer_src = fn_src(KB_SRC, "transfer", cls="KnowledgePresetManager")
    check("静态：源库清空失败记进统一账本（不是只喊一句 warning）",
          "_note_save(self.SAVE_PART_PRESET_FILE, False" in transfer_src)
    api_src = fn_src(WEB_SRC, "transfer_preset", cls="")
    check("静态：warning 进 notice_text（界面才看得见）",
          "r.get(\"warning\")" in api_src and "notice_text" in api_src and "⚠" in api_src)
    check("静态：也给结构化字段（会读它的调用方拿得到）",
          "\"warning\": warn" in api_src)

    with tempfile.TemporaryDirectory() as td:
        kd = Path(td)
        man = KnowledgePresetManager(str(kd), "sid3", str(kd))
        a = man.create("源")
        b = man.create("目标")
        src_p = man.preset_path(a["id"])
        d = json.loads(src_p.read_text(encoding="utf-8"))
        d.setdefault("entries", {})["条目甲"] = {
            "content": "内容", "status": "verified", "enabled": True,
            "updated_at": "", "mod": "general", "kind": "instance",
        }
        src_p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.chmod(src_p, stat.S_IREAD)
        try:
            r = man.transfer(a["id"], b["id"], "move")
        finally:
            os.chmod(src_p, stat.S_IREAD | stat.S_IWRITE)
        check("移动：动作照旧算成功，但 warning 说清「源库没清干净」",
              r.get("ok") is True and bool(r.get("warning")), str(r.get("warning")))
        check("移动：落盘字段与其它预设接口同形（save_ok / save_error）",
              r.get("save_ok") is False and bool(r.get("save_error")), str(r))


# ====================================================== D. 日志监听的生命周期与 IO

def part_d() -> None:
    print("================ [D] 日志监听：start() 幂等 / 初始化 IO / 探测真假 ================")
    start_src = fn_src(LW_SRC, "start", cls="LogWatcher")
    check("静态：start() 幂等 —— 重复调用先收掉上一轮（旧任务否则永远停不掉）",
          # 第七轮：收旧改走 `_stop_locked`（生命周期锁内的同一条收尾路，
          # 不再在公开 stop() 上重入）—— 判据不变，目标随实现形态更新。
          "await self._stop_locked()" in start_src)
    check("静态：初始化定位整块走线程 + 超时（不再占着事件循环）",
          # 第六轮：调用形态从 `asyncio.to_thread(...)` 收成统一入口
          # `self._io_wait(self._prime_state, ...)`（IO 走专属执行器 + 单飞）
          "self._io_wait(self._prime_state" in start_src)
    prime_src = fn_src(LW_SRC, "_prime_state", cls="LogWatcher")
    check("静态：exists / stat / 头指纹 / 编码探测都在 _prime_state 里（线程内跑）",
          all(k in prime_src for k in ("self.log_path.exists()", "self.log_path.stat()",
                                       "self._file_sig()", "self._head_sig()",
                                       "self._sniff_encoding()")), prime_src[:200])
    io_src = fn_src(LW_SRC, "_io_wait", cls="LogWatcher")
    check("静态：_io_wait 用 asyncio.wait_for + IO_TIMEOUT（超时按「没读到」降级）",
          "asyncio.wait_for" in io_src and "IO_TIMEOUT" in io_src
          and "self.io_timeouts += 1" in io_src)
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    check("静态：_poll 每一处文件 IO 都带超时",
          poll_src.count("self._io_wait(") >= 6, str(poll_src.count("self._io_wait(")))
    reap_src = fn_src(LW_SRC, "_reap_orphan", cls="LogWatcher")
    check("静态：孤儿任务结束要**取走异常**（旧写法只 discard → never retrieved）",
          "task.exception()" in reap_src
          and "task.add_done_callback(self._orphaned.discard)" not in LW_SRC)
    health_src = fn_src(LW_SRC, "health", cls="LogWatcher")
    check("静态：health() 摊开 probed / io_timeouts",
          "\"probed\"" in health_src and "\"io_timeouts\"" in health_src)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, seen = make_watcher(tmp)
        h0 = w.health()
        check("还没 start() → 不自称「文件在」（旧写法默认 True）",
              h0["file_present"] is False and h0["probed"] is False, str(h0))

        p = tmp / "logs" / "latest.log"
        p.write_bytes(b"[12:00:00] [Server thread/INFO]: <Steve> jiou\n")

        async def _two_starts():
            await w.start()
            first = w._task
            await w.start()
            return first, w._task

        first, second = asyncio.run(_two_starts())
        check("重复 start()：旧任务被收掉，不留在后台（旧写法覆盖句柄 = 永远停不掉）",
              first is not second and first.done(), f"{first.done()}")
        check("start() 后如实报「探测过、文件在」",
              w.health()["probed"] is True and w.health()["file_present"] is True)
        asyncio.run(w.stop())

        # 从文件尾起步 + 新行照常播报
        seen.clear()
        w2, seen = make_watcher(tmp)
        asyncio.run(w2.start())
        run_loop_briefly(w2)
        check("start() 不回溯历史日志（旧行不算新事件）", seen == [], str(seen))
        with io.open(p, "ab") as f:
            f.write(b"[12:00:01] [Server thread/INFO]: <Steve> hello\n")
        run_loop_briefly(w2)
        check("起监听后新增的行照旧播报", len(seen) == 1, str(seen))
        asyncio.run(w2.stop())

        # 初始化定位失败（_pos = -1）→ 落到文件尾，绝不从 0 重播
        seen.clear()
        w3, seen = make_watcher(tmp)
        w3._pos = -1
        run_loop_briefly(w3)
        check("位置未知时不从 0 重播历史（直接落到文件尾）",
              seen == [] and w3._pos == p.stat().st_size, f"{seen} / {w3._pos}")

    with tempfile.TemporaryDirectory() as td4:
        w4, _ = make_watcher(Path(td4))

        async def _io_timeout():
            old = LW.IO_TIMEOUT
            LW.IO_TIMEOUT = 0.05
            try:
                def slow() -> int:
                    # 第六轮起 `_io_wait` 收的是**同步函数**（在线程里跑）
                    time.sleep(1.0)
                    return 0
                return await w4._io_wait(slow, what="假装卡住的读盘")
            finally:
                LW.IO_TIMEOUT = old

        got = asyncio.run(_io_timeout())
        check("单次 IO 超时：返回 None（不是永远等下去）", got is None)
        check("超时留痕：io_timeouts 计数 + last_error 写明", w4.io_timeouts == 1
              and "超时" in w4.last_error, w4.last_error)

        async def _orphans():
            async def boom():
                raise RuntimeError("孤儿任务炸了")

            t1 = asyncio.create_task(boom())
            await asyncio.sleep(0.01)
            w4._orphaned.add(t1)
            w4._reap_orphan(t1)                 # 不许抛
            t2 = asyncio.create_task(asyncio.sleep(5))
            t2.cancel()
            await asyncio.sleep(0.01)
            w4._orphaned.add(t2)
            w4._reap_orphan(t2)                 # 已取消的同样不许抛
            return t1, t2

        t1, t2 = asyncio.run(_orphans())
        check("孤儿任务收尾：异常被取走（不再 never retrieved）、引用被清",
              isinstance(t1.exception(), RuntimeError) and t1 not in w4._orphaned
              and t2 not in w4._orphaned)


# ============================================ E. 发版门禁：与 CI 同一套 + 清单唯一来源

def part_e() -> None:
    print("================ [E] 发版门禁：UI 契约类不许被绕过 ================")
    check("静态：release.yml 跑 run_release_verify.py（不再只跑 test_*.py）",
          "run_release_verify.py" in REL_YML)
    check("静态：release.yml 装了浏览器（UI 契约类用例要用）",
          "playwright install" in REL_YML and "playwright" in REL_YML)
    check("静态：tests.yml 的清单由 _ui_manifest 现算（不再手写第二份）",
          "--emit-env" in TESTS_YML and "--guard" in TESTS_YML
          and "ui_broadcast_order_check" not in TESTS_YML,
          "tests.yml 里还留着硬编码名单")
    check("静态：回归 job 与发版前置跑同一脚本",
          "run_release_verify.py --no-ui" in TESTS_YML)
    check("静态：本机全量复现脚本共用同一套循环（没有第二份跑测实现）",
          "import run_release_verify as RV" in ALL_RUNNER
          and "import subprocess" not in ALL_RUNNER)
    check("静态：验证脚本逐文件超时 + 连进程树一起收（UI 用例会留浏览器孤儿）",
          "TimeoutExpired" in RELEASE_VERIFY_SRC and "taskkill" in RELEASE_VERIFY_SRC)

    # 清单守卫：每个 ui_*.py 都必须有归属（ui_theme_flash_trace 当年就是漏网的）
    sys.path.insert(0, str(PLUGIN / "tests"))
    import _ui_manifest as UM  # noqa: E402
    from _ui_manifest import HARD, SOFT, TOOLS, unclassified  # noqa: E402
    check("清单守卫：没有未归类的 ui_*.py", unclassified(PLUGIN) == [],
          str(unclassified(PLUGIN)))
    check("清单覆盖三档（硬门禁 / 观察期 / 取证）",
          len(HARD) >= 9 and len(SOFT) >= 4 and len(TOOLS) >= 1)
    check("取证脚本也被清单收编（旧守卫只看 *_check.py，它就漏了）",
          "ui_theme_flash_trace" in TOOLS)

    # 第四份名单 CI_OK：能上 CI 的和只能在本机跑的分开（第五轮上云实测后的收口）
    check("静态：CI 白名单非空且都在判定档里（CI 上真有人跑）",
          len(UM.ci_hard()) > 0 and set(UM.ci_hard() + UM.ci_soft()) <= set(HARD) | set(SOFT),
          f"{UM.ci_hard()} / {UM.ci_soft()}")
    check("静态：确实把 CI 上跑不过的挑了出来（不是把 9 件全塞进 CI 硬门禁）",
          len(UM.local_only()) >= 1 and set(UM.local_only()) <= set(HARD) | set(SOFT) | set(TOOLS),
          str(UM.local_only()))
    check("静态：release.yml 用 --ci（与 CI 同款子集，不再声称「UI 契约类全跑」）",
          "run_release_verify.py --ci" in REL_YML)
    check("静态：tests.yml 的两个 UI job 都走统一跑测器（逐文件超时同一套）",
          "--ui-set=ci-hard" in TESTS_YML and "--ui-set=ci-soft" in TESTS_YML
          and "for b in $HARD_UI" not in TESTS_YML,
          "tests.yml 里还有自己写的 shell 循环")
    bad = UM.check(PLUGIN)
    saved_ci_ok = list(UM.CI_OK)
    UM.CI_OK.append("ui_不存在的用例")
    try:
        problems = UM.check(PLUGIN)
    finally:
        UM.CI_OK[:] = saved_ci_ok
    check("守卫：CI 白名单里混进不存在 / 没定档的名字会被抓出来",
          bad == [] and any("ui_不存在的用例" in p for p in problems), str(problems))

    def _run(args, timeout=180):
        return subprocess.run([RV.PY, str(PLUGIN / "run_release_verify.py")] + args,
                              cwd=str(PLUGIN), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    r = _run(["--guard"])
    check("--guard 真跑：清单干净即退出码 0", r.returncode == 0, r.stdout[-300:])
    r = _run(["--emit-env"])
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    check("--emit-env 只输出环境变量行（混进日志会污染 $GITHUB_ENV）",
          r.returncode == 0
          and all(ln.startswith(("HARD_UI=", "SOFT_UI=", "UI_LOCAL_ONLY=")) for ln in lines)
          and len(lines) == 3, str(lines))
    r = _run(["--list-ui"])
    check("--list-ui 把四份名单与实测依据打出来（CI 日志里看得见谁没跑、为什么）",
          r.returncode == 0 and "本机专属" in r.stdout and "37144699301" in r.stdout,
          r.stdout[-200:])
    r = _run(["--plan"])
    check("--plan 列出将要跑什么（含 UI 硬门禁 + 本机专属名单）",
          r.returncode == 0 and "ui_theme_check.py" in r.stdout
          and "test_v0235_review_round5.py" in r.stdout
          and "本机专属" in r.stdout, r.stdout[-200:])

    # 超时机制真的生效：挂死的用例必须被掐掉，而不是把门禁一起拖住
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "tests").mkdir(parents=True, exist_ok=True)
        (tmp / "tests" / "hang.py").write_text(
            "import time\nwhile True:\n    time.sleep(0.5)\n", encoding="utf-8")
        old_root = RV.ROOT
        RV.ROOT = tmp
        try:
            ok, cost, out, timed_out = RV.run_file("tests/hang.py", 1.5)
        finally:
            RV.ROOT = old_root
        check("挂死的用例被超时掐掉并记为失败（不是无限等）",
              ok is False and timed_out is True and cost < 20, f"{ok}/{timed_out}/{cost:.1f}s")


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    for name in _fail:
        print(f"  [XX] {name}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
