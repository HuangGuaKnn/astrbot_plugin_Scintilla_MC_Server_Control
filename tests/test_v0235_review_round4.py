# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第四轮）回归：判据不可靠 + 失败被当成成功（续）。

前三批的分工：round2 收「不报错、但悄悄坏掉的**接口**」；round3 收「**判据本身**
站不住」与「失败被包装成成功」；本批收第四轮裁下来的七处，都是**同一类病** ——
「有一处真相，却没人去看」或者「看的地方各自抄了一份判据」：

  A. 向量补算的脏信号散落七处（KB 写入点 + WebUI 六个写接口）各挂一次钩子，
     漏一处就永久不补算。现在从 `save()` 这个**唯一写盘出口**统一发出
     （`_notify_write`），任何入口的写入都跑不掉。
  B. 批次文本与哈希**不同源**：哈希在 `set_vectors()` 里现算，而文本是若干次
     await 之前取的快照 —— 等待嵌入期间被改过内容的条目，会把「新文本的哈希」
     配给「旧文本的向量」，从此 `missing()` 认定「无需重算」= 永久错配。
     现在哈希与 texts 在同一瞬间（await 之前）取好。
  C. 检索口径两套：条目索引按 `_searchable()`（排除禁用 / 待审批），
     语义候选只查「条目表里在不在」—— 被禁用的条目仍被语义通道排进候选。
     现在两边同一口径。
  D. `_loop` 无条件把 `error_count` 清零，而 `_poll` 是「自己记错、正常返回」，
     于是 health() 里的计数**永远是 0**。现在先记基线、按「没新增错误」判定。
  E. `_poll` 里的文件 IO 是同步的 —— 真被卡住时占住的是**整个事件循环**，
     `stop()` 那句 5 秒 wall-clock 承诺就成了纸面保证。现在 IO 全在 to_thread 里。
  F. 预设「移动」：源库清空失败被 `except: pass` 吞掉，最后照报 ok=True ——
     界面以为源库已空。现在带上 warning（动作仍算成功，但不许闷着）。
  G. `_make_text_part` 的第三格回退（官方类在、但构造失败）以前退回裸 dict ——
     那正是它要修的那个 issue（整轮 LLM 请求全挂）。现在宁可不挂这一块。
  H. `GET /state` 不带落盘健康度：写接口的告警活不过一次刷新。现在随 GET 给出去，
     前端加载知识库页时渲染。

运行：
  <AstrBot python> tests\\test_v0235_review_round4.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import stat
import sys
import tempfile
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
MAIN_SRC = io.open(PLUGIN / "main.py", encoding="utf-8").read()
KB_SRC = io.open(PLUGIN / "core" / "knowledge_base.py", encoding="utf-8").read()
LW_SRC = io.open(PLUGIN / "core" / "log_watcher.py", encoding="utf-8").read()
WEB_SRC = io.open(PLUGIN / "core" / "web_api.py", encoding="utf-8").read()
UI_SRC = io.open(PLUGIN / "pages" / "mc_control" / "index.html", encoding="utf-8").read()

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
    """按 AST 摘出某个函数/方法的**源码原文**（静态断言用，不复制逻辑）。

    同名方法可能存在于多个类（如 `save`：SemanticIndex 与 ModKnowledgeBase 各一个），
    所以必要时指定 cls 精确取。
    """
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
    """把 main.py 的函数本体摘出来跑（main.py 不能在测试里 import，见 round3 说明）。"""
    ns: dict = dict(globs)
    ns.setdefault("asyncio", asyncio)
    body = []
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            body.append(node)
    assert len(body) == len(names), f"main.py 里缺函数：{names}"
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 "<main-fn>", "exec"), ns)
    return ns


class _RecLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __getattr__(self, _name):
        def _rec(*args, **kwargs):
            if args:
                self.lines.append(str(args[0]))
        return _rec


def make_watcher(tmp: Path):
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    w = LW.LogWatcher(str(tmp), on_event, poll_interval=0.01)
    w._enc = "utf-8"
    return w, seen


def run_loop_briefly(w, seconds: float = 0.06) -> None:
    """让 `_loop` 真跑一小会儿（不是复制它的逻辑，是跑真源码）。"""
    async def _main():
        w._running = True
        task = asyncio.create_task(w._loop())
        await asyncio.sleep(seconds)
        w._running = False
        await task
    asyncio.run(_main())


# ==================================================================== A. 写入即发脏信号

def part_a() -> None:
    print("================ [A] 向量补算的脏信号：唯一出口 = save() ================")
    save_src = fn_src(KB_SRC, "save", cls="ModKnowledgeBase")
    check("KB.save() 落盘后统一发脏信号（任何写入入口都跑不掉）",
          "self._notify_write()" in save_src)
    note_src = fn_src(KB_SRC, "_notify_write", cls="ModKnowledgeBase")
    check("脏信号出口自己把回调包住（补算炸了也不许影响写入）",
          "try:" in note_src and "except Exception" in note_src)
    check("main.py：_apply_active_knowledge 给 KB 实例挂上补算出口",
          "kb.on_write = self._kb_touch_vectors" in MAIN_SRC)
    check("main.py：两处管理器构造都传了 on_write（切预设 / 重建也不会丢）",
          MAIN_SRC.count("on_write=self._kb_touch_vectors") == 2,
          str(MAIN_SRC.count("on_write=self._kb_touch_vectors")))
    check("WebUI 的六个写接口不再各自挂一遍钩子（判据只留一处）",
          "_kb_touch_vectors" not in WEB_SRC)

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p")
        hits: list[int] = []
        kb.on_write = lambda: hits.append(1)
        kb.save_entry("topic a", "内容 a", status="verified")
        check("新增条目 → 发一次脏信号", len(hits) == 1, str(len(hits)))
        for label, fn in (
            ("禁用条目", lambda: kb.set_enabled("topic a", False)),
            ("纠错（就地改内容）", lambda: kb.correct_entry("topic a", "改过的内容")),
            ("重新启用", lambda: kb.set_enabled("topic a", True)),
            ("删除条目", lambda: kb.delete_entry("topic a")),
        ):
            before = len(hits)
            fn()
            check(f"{label} → 也发脏信号（旧写法只在新增时补算）", len(hits) > before,
                  f"{before} → {len(hits)}")

        def boom():
            raise RuntimeError("补算回调炸了")
        kb.on_write = boom
        check("回调抛异常时这次写入仍然是成功的（返回 True）", kb.save() is True)
        check("回调抛异常时落盘健康度不被污染（写盘本身没失败）",
              kb.save_health()["ok"] is True)

        cb_hits: list[int] = []
        cb = lambda: cb_hits.append(1)          # noqa: E731
        man = KnowledgePresetManager(str(Path(td) / "kdir"), "stub-sid", str(td), on_write=cb)
        check("管理器把回调下发给 KB 实例", man.kb is not None and man.kb.on_write is cb)
        man.reload_active()
        check("重建 / 切预设之后回调仍在（不是装配一次就丢）",
              man.kb is not None and man.kb.on_write is cb)


# ==================================================================== B. 哈希与文本同源

def part_b() -> None:
    print("================ [B] 哈希与文本同源：等待嵌入期间改内容不许错配 ================")
    build_src = fn_src(KB_SRC, "_build_vectors_locked", cls="ModKnowledgeBase")
    i = build_src.index("hashes = [")
    j = build_src.index("vectors = await self.embed_fn(texts)")
    check("静态：哈希在 await 之前、与 texts 同一瞬间取",
          i < j, f"hash@{i} vs await@{j}")
    NL = chr(10)
    post_code = NL.join(l for l in build_src[j:].splitlines()
                        if not l.strip().startswith("#"))        # 注释里提到不算
    check("静态：提交前复检只做过滤，不再现算哈希（否则就是新的错配源）",
          "content_hash" not in post_code, post_code[:300])

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p", semantic_enabled=True)
        kb.save_entry("topic a", "旧内容 a", status="verified")
        kb.save_entry("topic b", "旧内容 b", status="verified")

        async def embed(texts):
            # 模拟「等嵌入返回的这段时间里主人纠错改了 topic b 的内容」
            kb.correct_entry("topic b", "全新内容 b")
            return [[1.0] + [0.0] * 15 for _ in texts]

        kb.embed_fn = embed
        res = asyncio.run(kb.build_vectors(force=True))
        entries = kb._data["entries"]
        check("构建成功、两条都进索引", res.get("added") == 2 and len(kb._sem.topics) == 2,
              str(res))
        check("等待期间被改过内容的条目 → 下次仍会被认成脏（旧写法会永久错配）",
              kb._sem.needs("topic b", entries["topic b"]) is True)
        check("没被改过的条目不误判为脏（哈希确实在起作用，不做无谓重算）",
              kb._sem.needs("topic a", entries["topic a"]) is False)
        res2 = asyncio.run(kb.build_vectors())
        check("第二轮只补算那一条被改的", res2.get("added") == 1, str(res2))


# ==================================================================== C. 检索口径统一

def part_c() -> None:
    print("================ [C] 检索口径统一：禁用 / 待审批不进取向量候选 ================")
    search_src = fn_src(KB_SRC, "search", cls="ModKnowledgeBase")
    check("静态：候选过滤按 _searchable()（与词法索引同一口径）",
          "_searchable()" in search_src and "searchable" in search_src,
          search_src[:400])

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p", semantic_enabled=True)
        kb.save_entry("topic a", "黄铜锭怎么合成", status="verified")
        kb.save_entry("topic b", "黄铜锭怎么合成", status="verified")
        kb.set_enabled("topic b", False)
        # 等价于「禁用那一刻向量还留在索引里」：直接把它塞回去（旧代码的现场）
        kb._sem.set_vectors(["topic b"], kb._data["entries"], [[1.0] + [0.0] * 15])
        kb._rank_candidates = lambda q, query_vec=None: ["topic b", "topic a"]
        try:
            out = kb.search("黄铜锭")
        finally:
            del kb._rank_candidates
        topics = [e.get("topic") for e in out]
        check("被禁用的条目即使向量还在索引里，也搜不出来", "topic b" not in topics, str(topics))
        check("正常条目照常搜得到（不是整锅倒掉）", "topic a" in topics, str(topics))

        kb.save_entry("topic c", "黄铜锭合成", status="pending")
        kb._sem.set_vectors(["topic c"], kb._data["entries"], [[1.0] + [0.0] * 15])
        kb._rank_candidates = lambda q, query_vec=None: ["topic c"]
        try:
            out2 = kb.search("黄铜锭")
        finally:
            del kb._rank_candidates
        check("待审批条目同样不进结果（auto_apply 关闭时的半成品）",
              all(e.get("topic") != "topic c" for e in out2), str(out2))


# ==================================================================== D. 错误计数不被抹掉

def part_d() -> None:
    print("================ [D] 监听循环不许抹掉 _poll 记下的错误 ================")
    loop_src = fn_src(LW_SRC, "_loop")
    check("静态：先记基线、再按「没新增错误」判定是否复位",
          "before = self.error_count" in loop_src
          and "if self.error_count == before:" in loop_src)
    check("静态：不再无条件清零", "await self._poll()\n                self.error_count = 0" not in loop_src)

    with tempfile.TemporaryDirectory() as td:
        # ① _poll 自己记错、正常返回（不抛异常）→ 计数必须留下
        w, _ = make_watcher(Path(td))

        async def bad_poll():
            w.error_count += 1
            w.last_error = "stat 挂了"
        w._poll = bad_poll
        run_loop_briefly(w)
        check("带错误的轮次跑过之后 error_count 没被抹成 0（旧写法必为 0）",
              w.error_count > 0, str(w.error_count))
        check("health() 里也看得见（界面不再绿灯）",
              w.health()["error_count"] > 0 and bool(w.health()["last_error"]),
              str(w.health()))

        # ② 恢复正常的一轮 → 才复位
        w2, _ = make_watcher(Path(td) / "b")
        w2.error_count = 3

        async def ok_poll():
            w2.last_error = ""
        w2._poll = ok_poll
        run_loop_briefly(w2)
        check("恢复正常的一轮 → 计数复位（不是「永远不清零」的反面错误）",
              w2.error_count == 0, str(w2.error_count))


def part_d2() -> None:
    print("---- [D2] 真链路：读日志一直失败，health 必须一直红着 ----")
    with tempfile.TemporaryDirectory() as td:
        w, _ = make_watcher(Path(td))
        (Path(td) / "logs" / "latest.log").write_bytes(b"[12:00:00] [Server thread/INFO]: <Steve> a\n")

        def locked(*a, **k):
            raise OSError("文件被别的进程独占")
        had = "open" in LW.__dict__                      # 模块里本没有 open，是内置的
        real_open = LW.__dict__.get("open")
        LW.open = locked
        try:
            w.error_count = 0
            run_loop_briefly(w)
        finally:
            if had:
                LW.open = real_open
            else:
                del LW.open
        h = w.health()
        check("读日志一直失败 → error_count 真的累计起来（旧写法永远是 0）",
              h["error_count"] > 0, str(h))
        check("last_error 记得下这次失败的原因", "OSError" in str(h["last_error"]), str(h))


# ==================================================================== E. 文件 IO 进线程

def part_e() -> None:
    print("================ [E] _poll 的文件 IO 全部移出事件循环 ================")
    poll_src = fn_src(LW_SRC, "_poll")
    check("静态：_poll 里不再直接 stat / exists", "self.log_path.stat()" not in poll_src
          and "self.log_path.exists()" not in poll_src)
    check("静态：_poll 里不再自己 open 日志文件", "with open(" not in poll_src)
    check("静态：都是 to_thread（事件循环不再被同步 IO 占住）",
          all(s in poll_src for s in (
              "asyncio.to_thread(self.log_path.exists",
              "asyncio.to_thread(self.log_path.stat)",
              "asyncio.to_thread(self._head_sig)",
              "asyncio.to_thread(self._sniff_encoding)",
              "asyncio.to_thread(self._read_at",
          )), poll_src[:500])
    read_src = fn_src(LW_SRC, "_read_at")
    check("静态：块读仍走可替换的 open（测试与注入点都还活着）",
          "open(self.log_path, \"rb\")" in read_src)
    check("静态：读数异常照样往上抛（由 _poll 决定算不算真错误）",
          "except" not in read_src)


# ==================================================================== F. 预设移动失败可见

def part_f() -> None:
    print("================ [F] 预设「移动」：源库清空失败不许闷着 ================")
    move_src = fn_src(KB_SRC, "transfer", cls="KnowledgePresetManager")
    check("静态：移动分支不再 `except: pass`", "except Exception:\n                pass" not in move_src)
    check("静态：失败时给出 warning 字段", "warning" in move_src and "out[\"warning\"]" in move_src)

    with tempfile.TemporaryDirectory() as td:
        kdir = Path(td) / "kdir"
        man = KnowledgePresetManager(str(kdir), "sid", str(td))
        a = man.create("源库")
        b = man.create("目标库")
        src_p = man.preset_path(a["id"])
        d = json.loads(src_p.read_text(encoding="utf-8"))
        d.setdefault("entries", {})["topic t1"] = {
            "content": "内容", "status": "verified", "enabled": True,
        }
        src_p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

        # 负面对照：正常移动 → 源库清空、不带 warning
        r_ok = man.transfer(a["id"], b["id"], mode="move")
        check("正常移动：成功且不带 warning", r_ok.get("ok") is True and "warning" not in r_ok,
              str(r_ok))
        check("正常移动：源库确实空了",
              not json.loads(src_p.read_text(encoding="utf-8")).get("entries"), str(r_ok))

        # 复现场景：源文件写不进去（Windows 只读属性即写失败）
        a2 = man.create("源库2")
        b2 = man.create("目标库2")
        src2 = man.preset_path(a2["id"])
        d2 = json.loads(src2.read_text(encoding="utf-8"))
        d2.setdefault("entries", {})["topic t2"] = {
            "content": "内容2", "status": "verified", "enabled": True,
        }
        src2.write_text(json.dumps(d2, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(src2, stat.S_IREAD)
        try:
            r = man.transfer(a2["id"], b2["id"], mode="move")
        finally:
            os.chmod(src2, stat.S_IREAD | stat.S_IWRITE)
        check("复制确实成功 → 动作仍算成功（不报 False 骗人重搬一次）",
              r.get("ok") is True, str(r))
        check("但必须明说源库没清空", "warning" in r and "清空失败" in str(r.get("warning")), str(r))
        check("目标库确实拿到了条目",
              "topic t2" in json.loads(man.preset_path(b2["id"]).read_text(encoding="utf-8"))["entries"])
        check("源库条目原样还在（如实反映现场）",
              "topic t2" in json.loads(src2.read_text(encoding="utf-8"))["entries"])


# ==================================================================== G. TextPart 第三格

def part_g() -> None:
    print("================ [G] 官方 TextPart 构造失败：宁可不挂，也不退回裸 dict ================")
    mk_src = fn_src(MAIN_SRC, "_make_text_part")
    check("静态：第三格回退不再返回 dict",
          "return None" in mk_src and mk_src.count("return {\"type\": \"text\"") == 1,
          mk_src[-320:])
    hint_src = fn_src(MAIN_SRC, "_append_user_hint")
    check("静态：调用方把 None 剔除（不许把 None / dict 挂进请求）",
          "parts[:] = [p for p in parts if p is not None]" in hint_src)
    check("静态：剔除时留日志（不是安静吞掉）", "self.logger.warning" in hint_src)

    class BoomPart:
        """官方类在、但形状对不上（构造即抛）。"""
        def __init__(self, text: str = "") -> None:
            raise TypeError("unexpected keyword argument 'text'")

    class Req:
        def __init__(self) -> None:
            self.extra_user_content_parts: list = []

    class Host:
        def __init__(self) -> None:
            self.logger = _RecLogger()

    ns = load_funcs("_make_text_part", "_append_user_hint", TextPart=BoomPart)
    part = ns["_make_text_part"]("权限前置提醒")
    check("官方类存在但构造失败 → 返回 None（旧写法会丢一个裸 dict 出去）",
          part is None, repr(part))
    req = Req()
    ns["_append_user_hint"](Host(), req, ["第一块", "第二块"])
    check("构造失败时宁可不挂，也不挂 None / dict", req.extra_user_content_parts == [],
          str(req.extra_user_content_parts))
    host = Host()
    ns["_append_user_hint"](host, req, ["第一块"])
    check("并且留了日志（能查得到「提示少了一条」）",
          any("跳过" in x or "构造失败" in x for x in host.logger.lines), str(host.logger.lines))

    ns_old = load_funcs("_make_text_part", TextPart=None)
    fb = ns_old["_make_text_part"]("回退块")
    check("更老版本没有该模块 → 仍退回 dict（那种核心自带 dict 兼容分支）",
          fb == {"type": "text", "text": "回退块"}, repr(fb))

    ns_new = load_funcs("_make_text_part", TextPart=type("TextPart", (), {
        "__init__": lambda self, text="": setattr(self, "text", text),
    }))
    obj = ns_new["_make_text_part"]("正常块")
    check("有官方类且构造成功 → 用官方对象（新老核心都能吃）",
          obj is not None and getattr(obj, "text", None) == "正常块", repr(obj))


# ==================================================================== H. GET 也带落盘健康度

def part_h() -> None:
    print("================ [H] GET /state 也要带落盘健康度 ================")
    get_src = fn_src(WEB_SRC, "get_state", cls="McControlWebApi")
    check("静态：GET 回包带 save_health（刷新页面也看得见）",
          "\"save_health\": kb.save_health()" in get_src, get_src[-500:])
    check("静态：开关状态那份告警复用同一判据（不另写一份）",
          "self._state_save_warning(kb)" in get_src)
    check("静态：前端加载知识库时渲染它",
          "renderKbSaveHealth(r)" in UI_SRC and "function renderKbSaveHealth" in UI_SRC)
    check("静态：前端只在真有问题时说话（不打扰）",
          'save_health' in UI_SRC and "h.ok === false" in UI_SRC)

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(Path(td) / "kdir"), server_id="sid", preset_id="p")
        h0 = kb.save_health()
        check("健康度字段形状固定（ok / error / at）",
              set(h0) == {"ok", "error", "at"}, str(h0))
        check("正常情况如实报成功", h0["ok"] is True and h0["error"] == "")
        # 写不进去的现场：父目录不存在
        kb.file_path = Path(td) / "missing_dir" / "kb.json"
        ok = kb.save()
        h1 = kb.save_health()
        check("写盘失败 → save() 返回 False（不再假成功）", ok is False, repr(ok))
        check("save_health 如实报错（GET 就靠它说话）",
              h1["ok"] is False and bool(h1["error"]), str(h1))


def part_i() -> None:
    print("================ [I] 指纹回写失败要并进落盘健康度（取证 ⑨）================")
    with tempfile.TemporaryDirectory() as td:
        kdir = Path(td) / "kdir"
        man = KnowledgePresetManager(str(kdir), "sid", str(td))
        a = man.create("库")
        p = man.preset_path(a["id"])
        r0 = man.bind(a["id"], "fp-ok")
        check("正常绑定：save_ok=True 且无错误文案",
              r0.get("save_ok") is True and not r0.get("save_error"), str(r0))

        os.chmod(p, stat.S_IREAD)                     # 预设文件写不进去（Windows 只读即失败）
        try:
            r = man.bind(a["id"], "fp-boom")
        finally:
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
        check("指纹回写失败 → save_ok=False（旧写法这里仍是 True，界面看不到）",
              r.get("save_ok") is False, str(r))
        check("错误文案点明是哪一步失败", "指纹回写失败" in str(r.get("save_error")), str(r))
        check("管理器自身的健康度也同步（不看这次回包的调用方也跑不掉）",
              man.last_save_ok is False and "指纹回写失败" in str(man.last_save_error),
              f"{man.last_save_ok} / {man.last_save_error}")

        # 反面对照：预设文件「本来就不在」是正常情况，不许报成失败
        b = man.create("库2")
        os.remove(man.preset_path(b["id"]))
        r2 = man.bind(b["id"], "fp-gone")
        check("预设文件不在（已被删）→ 不算失败（不许误报）",
              r2.get("save_ok") is True and not r2.get("save_error"), str(r2))


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    part_d2()
    part_e()
    part_f()
    part_g()
    part_h()
    part_i()
    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[FAIL] 失败项:")
        for n in _fail:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
