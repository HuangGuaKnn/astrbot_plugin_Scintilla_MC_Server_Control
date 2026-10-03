# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第三轮）回归：判据不可靠 + 失败被当成成功。

与第二批（`test_v0235_review_round2.py`）的分工：那一批收的是「不报错、但悄悄
坏掉」的**接口**（配置键、任务名、白名单）；这一批收的是**判据本身站不住**、
以及**失败被包装成成功**：

  A. 日志轮转的判据只有「变短 / 换 inode / 尺寸相等且头指纹变了」三档，
     于是「同一 inode 原地重写、新内容比旧 offset 更长」整类漏掉 —— 从此读到的
     全是错位字节。现在补第四档：**内容锚点**（回读上次消费掉的最后 64 字节）。
     本文件的样本刻意做成「骗过另外三档」：inode 不变、文件头前 300 字节不变、
     长度反而更长。
  B. 日志监听：文件一直不在 / 读取一直失败都不可见；超长单行的余部会被当成新行
     解析。现在进 health()（在场标志 / 缺失轮数 / 滞后字节），余部丢弃到换行。
  C. stop()：`asyncio.wait_for` 的超时会被「吞掉取消」的子任务骗过（`Task.cancel`
     把取消委托给被等的任务），5 秒的承诺是纸面的。改用 `asyncio.wait` 并留引用。
  D. 知识库：等待嵌入期间被删掉的条目会被写回向量索引 → 幽灵 topic → search()
     取 _entry_view 直接 KeyError（整次检索报错）；写入后的补算被 policy=skip
     白丢且没有补偿。现在提交前复检 + 脏标记补跑。
  E. 落盘失败不再假成功：向量索引 / 知识条目 / 预设注册表三处，写盘失败必须
     出现在工具结果、日志、接口响应与状态里。
  F. 知识库开关状态接口把外部值**直传** `KnowledgeState.set()`（内部是裸 `bool()`，
     `bool("false")` 仍是 True）——四个入口都改了，偏偏漏了这一处。现在走严格解析，
     并补状态落盘告警 `_state_save_warning()`。
     （这一条此前**一条断言都没有**，是本文件 [G] 段新补的；核验单自曝的「第 9 个落点」。）

关于手法：main.py 不能被测试直接 import（要 astrbot 运行环境），所以对 `Plugin`
上的纯逻辑函数（如 `_kb_vector_rounds`）采取**按 AST 摘出函数本体**再跑行为测试的
做法 —— 测的是真源码，不是复制品。

运行：
  <AstrBot python> tests\\test_v0235_review_round3.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as KB  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as WA  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    KnowledgePresetManager,
    ModKnowledgeBase,
    SemanticIndex,
)

PLUGIN = PLUGIN_DIR
MAIN_SRC = io.open(PLUGIN / "main.py", encoding="utf-8").read()
WEB_SRC = io.open(PLUGIN / "core" / "web_api.py", encoding="utf-8").read()
WF_SRC = io.open(PLUGIN / "core" / "workflow.py", encoding="utf-8").read()

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


def extract_fn(name: str):
    """把 main.py 里的某个函数**本体**摘出来（见文件头说明）。"""
    for node in ast.walk(ast.parse(MAIN_SRC)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns: dict = {"asyncio": asyncio}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                         f"<{name}>", "exec"), ns)
            return ns[name]
    raise AssertionError(f"main.py 里找不到 {name}")


def make_watcher(tmp: Path):
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    w = LW.LogWatcher(str(tmp), on_event, poll_interval=0.01)
    w._enc = "utf-8"
    return w, seen


class _RecLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __getattr__(self, _name):
        def _rec(*args, **kwargs):
            if args:
                self.lines.append(str(args[0]))
        return _rec


# ==================================================================== A. 轮转判据

def part_a() -> None:
    print("================ [A] 日志轮转：同一 inode 原地重写也必须认出来 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, seen = make_watcher(tmp)
        p = tmp / "logs" / "latest.log"
        # 前 6 行（≥300 字节）两版**逐字节相同**，且都不含可解析事件 —— 头指纹无从分辨
        head = b"".join(
            (f"[12:00:{i:02d}] [Server thread/INFO]: loading mod #{i:03d} padding line\n").encode()
            for i in range(6)
        )
        assert len(head) >= 300, len(head)
        old = head + b"[12:00:05] [Server thread/INFO] [minecraft/DedicatedServer]: <Steve> old line\n"
        p.write_bytes(old)
        asyncio.run(w._poll())
        check("首次读取拿到旧日志里的事件", ("chat", "Steve", "old line") in seen)
        check("首次读取后已建立内容锚点（第四档判据的输入条件）",
              bool(w._anchor) and w._anchor_at >= 0 and w._pos == w._anchor_at + len(w._anchor))
        pos_before = w._pos
        ino_before = p.stat().st_ino
        head_before = w._head_sig()
        seen.clear()

        # 原地重写（同一文件、同一 inode）：前 300 字节不变、总长度更长
        new = head + (b"[12:00:07] [Server thread/INFO] [minecraft/DedicatedServer]: "
                      b"<Alex> rewritten in place\n" * 3)
        p.write_bytes(new)
        check("样本有效性：inode 未变 / 头指纹未变 / 长度更长（①②③三档都失灵）",
              p.stat().st_ino == ino_before and w._head_sig() == head_before
              and len(new) > pos_before,
              f"ino同一={p.stat().st_ino == ino_before} 头同一={w._head_sig() == head_before} "
              f"{len(new)}>{pos_before}")
        asyncio.run(w._poll())
        check("原地重写被认出来（新内容被读到）", any(ev[1] == "Alex" for ev in seen), str(seen[:2]))
        check("位置按新文件重算（不再停在旧 offset 上）", w._pos <= len(new))
        check("旧内容不再重复产出（真判轮转 = 从头读一次，不是接着旧位置读）",
              not any(ev[2] == "old line" for ev in seen))


# ==================================================================== B. 可见性

def part_b() -> None:
    print("================ [B] 文件缺失 / 读取失败 / 超长单行余部 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, _seen = make_watcher(tmp)
        check("health 带上了在场标志 / 缺失轮数 / 滞后字节 / 读取时刻",
              {"file_present", "missing_polls", "lag_bytes", "last_read_at"} <= set(w.health()))

        asyncio.run(w._poll())
        h = w.health()
        check("文件不在：如实报不在，但**不算** IO 错误（轮转窗口是正常现象）",
              h["file_present"] is False and h["missing_polls"] == 1 and h["error_count"] == 0,
              str(h))
        p = tmp / "logs" / "latest.log"
        p.write_bytes(b"[12:00:00] [Server thread/INFO] [minecraft/DedicatedServer]: <Steve> back\n")
        asyncio.run(w._poll())
        h = w.health()
        check("文件回来后计数复位",
              h["file_present"] is True and h["missing_polls"] == 0 and h["lag_bytes"] == 0)
        check("last_read_at 记下了真正读到内容的时刻", h["last_read_at"] > 0)

        # 读取失败：文件真实存在（stat 成功、长度 > 0），但 open 必失败
        # （真实对应「日志被别的进程独占」）。这里用模块级 open 桩来制造，
        # 比 chmod / 独占句柄稳定，且不影响本进程其它文件操作。
        w2, _ = make_watcher(tmp)
        w2.log_path = p

        def _locked(*_a, **_k):
            raise PermissionError("被独占写入")

        # 往模块命名空间里塞一个 open 名字（遮蔽内建），只影响 core.log_watcher 内部；
        # 用完立刻删掉，别污染其它用例。
        LW.open = _locked
        try:
            asyncio.run(w2._poll())
        finally:
            del LW.open
        h2 = w2.health()
        check("打开/读取失败会记数并留痕（不再安静 return）",
              h2["error_count"] >= 1 and bool(h2["last_error"]), str(h2.get("last_error")))

        # 超长单行：余部必须整段丢弃，不能当新行解析
        old_max = LW.MAX_READ_BYTES
        LW.MAX_READ_BYTES = 64
        try:
            w3, seen3 = make_watcher(tmp)
            p3 = tmp / "logs" / "latest.log"
            giant = (b"[12:00:00] [Server thread/INFO]: " + b"y" * 300
                     + b"[12:00:00] [Server thread/INFO]: <Zombie> ghost of a half line\n")
            # 这条「正常日志」必须短于被压低的单次读取上限（64 字节）：否则它自己
            # 也会被切成无换行片段，测的就不是「余部丢弃」而是这份被压小的上限了。
            good = b"[12:00:20] [Server thread/INFO]: <Steve> after\n"
            p3.write_bytes(giant + good)
            for _ in range(12):
                asyncio.run(w3._poll())
            check("超长片段的余部没被当成新行（没有幽灵事件）",
                  not any("ghost of a half line" in str(ev[2]) for ev in seen3), str(seen3[:3]))
            check("超长片段之后的那条正常日志照常读出来",
                  any(ev[1] == "Steve" and ev[2] == "after" for ev in seen3), str(seen3))
            check("跳过标记在收尾后清干净（不会一直吞后续内容）",
                  w3._skip_to_newline is False)
        finally:
            LW.MAX_READ_BYTES = old_max


# ==================================================================== C. stop()

def part_c() -> None:
    print("================ [C] stop() 不被吞取消的子任务骗过 ================")
    rec = _RecLogger()
    old_logger, old_timeout = LW.logger, LW.STOP_TIMEOUT
    LW.logger, LW.STOP_TIMEOUT = rec, 0.3
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "logs").mkdir(parents=True, exist_ok=True)

            async def on_event(*_a):
                pass

            w = LW.LogWatcher(str(tmp), on_event, poll_interval=0.01)

            async def stubborn():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await asyncio.sleep(30)      # ← 吞掉取消：就是骗过 wait_for 的形态
                    raise

            async def scenario():
                w._running = True
                w._task = asyncio.create_task(stubborn())
                # 让任务真的跑起来、停在 sleep 里再取消 —— 对「还没开始执行」的任务
                # 取消会把 CancelledError 直接投到第一行，那条路径测不到吞取消。
                await asyncio.sleep(0.05)
                t0 = time.monotonic()
                await w.stop()
                dt = time.monotonic() - t0
                health = w.health()
                orphans = list(w._orphaned)
                for t in orphans:                 # 收拾干净，别留 pending 任务警告
                    t.cancel()
                await asyncio.gather(*orphans, return_exceptions=True)
                return dt, health

            dt, health = asyncio.run(scenario())
            check(f"stop() 在超时（{LW.STOP_TIMEOUT}s）附近返回，没有被拖住", dt < 1.0, f"{dt:.2f}s")
            check("确实等满了超时才放弃（不是立刻返回）", dt >= 0.25, f"{dt:.2f}s")
            check("放弃等待的任务留了引用（没被 GC 掉，诊断看得见）",
                  health.get("orphaned_tasks") == 1, str(health.get("orphaned_tasks")))
            check("stop() 后 running=False", health.get("running") is False)
            check("留痕日志点名了「未退出」这件事",
                  any("未退出" in x for x in rec.lines), str(rec.lines[-1:]))

        # 对照实验：同一形态下 wait_for 的 timeout **不会**生效
        async def contrast() -> float:
            async def stubborn2():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.6)
                    raise
            t = asyncio.create_task(stubborn2())
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(t, timeout=0.1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            dt = time.monotonic() - t0
            if not t.done():
                t.cancel()
            return dt

        dt2 = asyncio.run(contrast())
        check("对照实验：wait_for(timeout=0.1) 被拖到 ~0.6s（所以我们不用它）",
              dt2 > 0.4, f"{dt2:.2f}s")
    finally:
        LW.logger, LW.STOP_TIMEOUT = old_logger, old_timeout


# ==================================================================== D. 幽灵 topic

def part_d() -> None:
    print("================ [D] 等待嵌入期间被删的条目不许进索引 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kb = ModKnowledgeBase(str(tmp), server_id="stub", preset_id="p", semantic_enabled=True)
        kb.save_entry("topic a", "内容 a", status="verified")
        kb.save_entry("topic b", "内容 b", status="verified")
        state = {"deleted": False, "content": 0}

        async def embed(texts):
            # 模拟「等嵌入返回的这段时间里，主人把 topic a 删了」
            state["content"] += 1
            if not state["deleted"]:
                state["deleted"] = kb.delete_entry("topic a")
            return [[1.0] + [0.0] * 15 for _ in texts]

        kb.embed_fn = embed
        res = asyncio.run(kb.build_vectors(force=True))
        check("构建照常返回成功", bool(res.get("ok")), str(res))
        check("样例前提：删除确实发生在构建过程中", state["deleted"] is True)
        check("被删的条目没有留在语义索引里（无幽灵 topic）",
              "topic a" not in kb._sem.topics, str(kb._sem.topics))
        check("剩下的条目照常建好向量", "topic b" in kb._sem.topics, str(kb._sem.topics))
        check("检索不炸（未修时这里 KeyError）", isinstance(kb.search("topic a"), list))

        # 负面对照：候选池里直接塞进幽灵 topic（等价于旧代码留下的脏索引）
        entries = kb._searchable()
        kb._sem.set_vectors(["topic ghost"], entries, [[1.0] + [0.0] * 15])
        kb._rank_candidates = lambda q, query_vec=None: ["topic ghost"]      # 注入脏候选
        try:
            out = kb.search("query")
            raised = False
        except KeyError:
            raised = True
        finally:
            del kb._rank_candidates
        check("负面对照：脏候选进了池子也不炸 KeyError", raised is False)
        check("脏候选被过滤掉，不出现在结果里",
              all(e.get("topic") != "topic ghost" for e in out), str(out))


# ==================================================================== E. 补跑闭环

def part_e() -> None:
    print("================ [E] 向量补算：构建期间的写入会被补跑带走 ================")
    rounds_fn = extract_fn("_kb_vector_rounds")

    class Stub:
        def __init__(self, dirty_round: int = 0) -> None:
            self._vec_pending = False
            self.logger = _RecLogger()
            self.rounds: list[bool] = []          # 每轮的 force 取值
            self.dirty_round = dirty_round

        async def _kb_build_semantic(self, force: bool = False) -> dict:
            self.rounds.append(force)
            if len(self.rounds) == self.dirty_round:
                self._vec_pending = True          # 构建期间又来了写入
            return {"ok": True, "save_warning": "磁盘满" if len(self.rounds) == 1 else ""}

    s1 = Stub(dirty_round=0)
    asyncio.run(rounds_fn(s1, 0.0, False))
    check("没有并发写入时只跑一轮", len(s1.rounds) == 1, str(s1.rounds))
    check("收尾时脏标记是干净的", s1._vec_pending is False)

    s2 = Stub(dirty_round=1)
    asyncio.run(rounds_fn(s2, 0.0, False))
    check("构建期间来的写入会被自动补跑（不再白丢）",
          len(s2.rounds) == 2, str(s2.rounds))
    check("补跑之后脏标记归零", s2._vec_pending is False)

    s3 = Stub(dirty_round=0)
    asyncio.run(rounds_fn(s3, 0.0, True))
    check("force 只作用于第一轮（补跑回到增量）", s3.rounds == [True], str(s3.rounds))
    check("落盘告警从轮次入口浮到日志里",
          any("落盘告警" in x for x in s3.logger.lines), str(s3.logger.lines))

    # 结构：写入方必须「先置标记、再排程」，否则被 skip 掉的请求还是白丢
    touch_src = None
    for node in ast.walk(ast.parse(MAIN_SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == "_kb_touch_vectors":
            touch_src = ast.get_source_segment(MAIN_SRC, node) or ""
    check("写入方先置脏标记、再排程（顺序反了就等于没修）",
          bool(touch_src)
          and touch_src.index("self._vec_pending = True") < touch_src.index("_spawn_bg("))
    check("向量补算的启动/写入两条路径共用同一个入口",
          MAIN_SRC.count("self._kb_vector_rounds(") == 2)


# ==================================================================== F. 落盘失败

def part_f() -> None:
    print("================ [F] 落盘失败不许假成功 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # F1 向量索引
        idx = SemanticIndex(dim=2)
        idx.set_vectors(["a"], {"a": {"content": "x", "enabled": True}}, [[1.0, 0.0]])
        ok = idx.save(tmp / "no_such_dir" / "vec.npz")
        check("向量索引落盘失败：如实返回 False 并留痕",
              ok is False and idx.last_save_ok is False and bool(idx.last_save_error))
        ok2 = idx.save(tmp / "vec.npz")
        check("换个可写路径即成功（不是把失败记死了）",
              ok2 is True and idx.last_save_ok is True and idx.last_save_error == "")

        # F2 知识条目：把 data_dir 指到不存在的深层目录，写盘必然失败
        kb = ModKnowledgeBase(str(tmp / "kb"), preset_id="p")
        kb.save_entry("t", "c", status="verified")
        check("正常路径下条目落盘成功（对照组）", kb.save_health()["ok"] is True, str(kb.save_health()))
        # 落盘目标改成「父目录不存在」的路径（KB 用的是 file_path，不是 data_dir）
        kb.file_path = tmp / "missing" / "deeper" / "kb_p.json"
        kb.save_entry("t2", "c2", status="verified")
        h = kb.save_health()
        check("条目落盘失败进 save_health（工具据此改口「只在内存」）",
              h["ok"] is False and bool(h["error"]), str(h))

        # F3 预设注册表
        man = KnowledgePresetManager(str(tmp / "kdir"), "fp-stub", str(tmp))
        c = man.create("新预设")
        check("正常路径下新建预设成功落盘（对照组）", c.get("save_ok") is True, str(c.get("save_error")))
        man.reg_path = tmp / "nope" / "registry.json"      # 父目录不存在 → 写必然失败
        r = man.switch(c["id"])
        check("切换预设：动作照旧成功，但 save_ok=False + 说清原因",
              r.get("ok") is True and r.get("save_ok") is False and bool(r.get("save_error")),
              str(r))
        c2 = man.create("再一个")
        check("新建预设：落盘失败同样带出来", c2.get("save_ok") is False, str(c2.get("save_error")))
        r2 = man.remove(c2["id"])
        check("删除预设：落盘失败同样带出来", r2.get("save_ok") is False, str(r2.get("save_error")))
        st = man.status()
        check("状态接口带出注册表落盘情况（界面才能长期显示）",
              st.get("registry_save_ok") is False and bool(st.get("registry_save_error")), str(st))
        check("失败原因是人话（含「落盘失败」字样）",
              "落盘失败" in str(r.get("save_error")), str(r.get("save_error")))

        # F4 接线：工具结果 / 工作流 / 接口都得看落盘结果
        check("两个知识工具都看落盘结果",
              MAIN_SRC.count('getattr(self._knowledge, "last_save_ok", True)') == 2,
              str(MAIN_SRC.count('getattr(self._knowledge, "last_save_ok", True)')))
        check("工作流写库（模板 + 纠错）都看落盘结果",
              WF_SRC.count('getattr(kb, "last_save_ok", True)') == 2,
              str(WF_SRC.count('getattr(kb, "last_save_ok", True)')))
        check("WebAPI 的五个预设接口都带上了落盘失败提示",
              WEB_SRC.count("self._save_fail_note(") == 5,
              str(WEB_SRC.count("self._save_fail_note(")))
        check("提示文案说明了「重启会回退」",
              "重启后会回退" in WEB_SRC)


# ==================================================================== G. 开关状态接口

class _FakeReq:
    """`astrbot.api.web.request` 的最小替身（只用来喂 JSON 请求体）。"""

    def __init__(self, payload) -> None:
        self.payload = payload

    async def json(self):
        return self.payload


class _FakePlugin:
    pass


def call_set_state(api, kb, payload):
    """真跑一遍 `set_state`（把模块级 request 临时换成替身，跑完还原）。"""
    api.plugin._knowledge = kb
    old = WA.request
    WA.request = _FakeReq(payload)
    try:
        resp = asyncio.run(api.set_state())
        return json.loads(bytes(resp.body).decode("utf-8"))
    finally:
        WA.request = old


def part_g() -> None:
    print("================ [G] 知识库开关状态：外部值不许直传 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kb = KB.ModKnowledgeBase(str(tmp / "kb"), preset_id="p")
        api = WA.McControlWebApi(_FakePlugin())

        r = call_set_state(api, kb, {"enabled": "false"})
        check('字符串 "false" → 开关真的关掉（旧代码会读成「开」）',
              r.get("ok") is True and (r.get("state") or {}).get("enabled") is False, str(r))
        r = call_set_state(api, kb, {"learning": "0"})
        check('字符串 "0" → learning=False',
              (r.get("state") or {}).get("learning") is False, str(r))
        r = call_set_state(api, kb, {"auto_apply": "off"})
        check('词表 "off" → auto_apply=False',
              (r.get("state") or {}).get("auto_apply") is False, str(r))

        r = call_set_state(api, kb, {"enabled": "maybe"})
        check('无法判定的 "maybe" → 报错，绝不猜',
              r.get("ok") is False and "布尔" in str(r.get("error")), str(r))
        check("报错之后开关没被动过（仍是 False，不是被猜成 True）",
              kb.state.enabled is False)

        r = call_set_state(api, kb, {"enabled": ""})
        check("空串 = 本次不动这一项（不是写 False）",
              r.get("ok") is True and kb.state.enabled is False, str(r))
        check("正常落盘 → 不带告警", str(r.get("save_warning", "")) == "", str(r))

        # 落盘失败：把状态文件指到一个「非空目录」上（Windows 上 replace 必失败）
        bad = tmp / "state_dir"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "occupied.txt").write_text("occupied", encoding="utf-8")
        kb.state.file_path = bad
        r = call_set_state(api, kb, {"enabled": "true"})
        w = str(r.get("save_warning", ""))
        check("状态落盘失败 → 响应带 save_warning", bool(w), str(r))
        check("告警点明「落盘失败」", "落盘失败" in w, w)
        check("告警提醒「重启后会退回」", "重启后会退回" in w, w)

        class _Broken:
            """没有 last_save_ok 的旧对象 —— 不许把接口炸了。"""

        check("没有 last_save_ok 的对象 → 空串（不抛异常）",
              api._state_save_warning(_Broken()) == "")

        print("\n---- 源码静态检查 ----")
        check("set_state 走严格解析（parse_bool）",
              "parsed[key] = parse_bool(raw)" in WEB_SRC)
        # 只看 set_state 的函数体，并且**先 AST 反编译**（注释里出现的 `bool()` 字样不算代码），
        # 再用词边界断言 —— 别把 `parse_bool(...)` 里的子串算成裸 bool()。
        _fn = next((n for n in ast.walk(ast.parse(WEB_SRC))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_state"), None)
        _code = ast.unparse(_fn) if _fn is not None else ""
        check("set_state 函数体里没有任何裸 bool()（只剩 parse_bool）",
              bool(_code) and "parse_bool(" in _code
              and not re.search(r"(?<![\w.])bool\(", _code),
              str([m.group(0) for m in re.finditer(r"[\w.]*bool\(", _code)]))
        check("状态落盘告警接进了 set_state 的响应",
              "self._state_save_warning(kb)" in WEB_SRC)


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()
    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[FAIL] 失败项:")
        for n in _fail:
            print("  -", n)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
