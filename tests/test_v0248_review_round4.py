# -*- coding: utf-8 -*-
"""v0.24.8 追加回归：GPT 第四轮复核（2057）那五条的**行为**判据。

为什么单独再写一份
==================
第三轮那份（`test_v0248_review_round3.py`）钉的是「并发有界」「取消僵尸回调」
「广播未确认态」「布尔唯一实现」这些**主张**；第四轮复核指出的是同一批主张里
仍然漏风的几个具体口子 —— 它们恰好都不是「所以源码里有没有某一行」能证明的：

  [1] **执行器排队无界**：固定 worker 池只让「同时跑几条」有界，队列本身
      还可以无限长；`run_blocking` 超时后那笔活儿仍躺在队列里，等 worker 有空
      再照跑。判据必须**真的把队列灌满**，看拒收与撤回。
  [2] **事件播报的布尔**：`notify_join_leave="false"` 实测仍会发播报。
      判据必须**真的调一次 `_on_server_event`**，看它有没有去发送。
  [3] **WebAPI 顶层不是对象**：请求体是数组 / 字符串时 `.get()` 直接
      AttributeError → 500。判据必须**真的发一个数组体**进去看回包。
  [4] **「无接收者」+ 边界未确认**：空服回执必须先看边界，不能无条件给成功。
  [5] **知识超长**：WebUI 与两个 LLM 工具共用同一处上限。

每条都按「改坏它，这条必须变红」设计（对应 `tests/mutation_v0248.py` 的 M7–M11）。
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as kbmod  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as lw      # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa          # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (       # noqa: E402
    classify_command_output,
)
from astrbot_plugin_Scintilla_MC_Server_Control.main import (                       # noqa: E402
    EVENT_CHAT,
    EVENT_JOIN,
    EVENT_LEAVE,
    McControlPlugin,
)

FAILED: list[str] = []
PASSED = [0]


def check(desc: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


def src(rel: str) -> str:
    return (PLUGIN_DIR / rel).read_text(encoding="utf-8")


class _QuietLog:
    def warning(self, fmt, *args):
        pass

    def info(self, fmt, *args):
        pass


# ============================================================ [1] 执行器队列
def part_p1_queue_bound() -> None:
    print("================ [1] 执行器：队列有界、关闭真取消（P1 收尾） ================")
    check("排队上限是个像样的常数（1 < N <= 1024）",
          1 < lw.MAX_DAEMON_QUEUE <= 1024, str(lw.MAX_DAEMON_QUEUE))

    ex = lw._DaemonThreadExecutor(thread_name_prefix="q4", max_workers=2, max_queue=8)
    gate = threading.Event()
    ran: list[int] = []

    def block(i: int) -> int:
        gate.wait(20)
        ran.append(i)
        return i

    futs = [ex.submit(block, i) for i in range(2)]
    time.sleep(0.25)                                   # 让两个 worker 真的领走
    rejected = 0
    for i in range(20):
        try:
            futs.append(ex.submit(block, 100 + i))
        except RuntimeError:
            rejected += 1
    queued = futs[2:]

    check("★队列到顶即拒收（灌 20 笔、上限 8 → 拒 12 笔）",
          rejected == 12, f"rejected={rejected}")
    check("★pending 不超过「并发上限 + 排队上限」（不再随提交量线性长）",
          ex.pending <= 2 + 8, f"pending={ex.pending}")
    check("拒收次数如实记账（否则只是偶发『不可用』，没人查得到）",
          ex.rejected == rejected, f"{ex.rejected} vs {rejected}")
    check("队列深度从不超过上限", ex._queue.qsize() <= 8, str(ex._queue.qsize()))
    check("排队中的任务此刻都没开始（用来验证下一步的『取消』是真的）",
          all(not f.done() for f in queued))

    ex.shutdown(cancel_futures=True)
    check("★shutdown(cancel_futures=True) 真的取消了排队任务",
          all(f.cancelled() for f in queued),
          str([f.cancelled() for f in queued][:5]))
    check("取消后 pending 只剩正在执行的两笔", ex.pending == 2, str(ex.pending))

    gate.set()
    for f in futs[:2]:
        f.result(timeout=10)
    time.sleep(0.3)
    check("★被取消的任务一笔都没执行（不是『先跑完再退』）",
          sorted(ran) == [0, 1], str(ran))
    check("收尾后 pending 归零", ex.pending == 0, str(ex.pending))


def part_p1_timeout_withdraw() -> None:
    """`run_blocking` 超时：那笔活儿必须从队列里撤下来，不能等 worker 有空再白跑。"""
    print("---------------- [1b] run_blocking 超时撤回 ----------------")
    old = lw._DAEMON_RUNNER
    ex = lw._DaemonThreadExecutor(thread_name_prefix="to4", max_workers=1, max_queue=4)
    lw._DAEMON_RUNNER = ex
    gate = threading.Event()
    ran: list[int] = []
    try:
        ex.submit(gate.wait, 20)                       # 占住唯一的 worker
        time.sleep(0.25)
        ok, val = asyncio.run(
            lw.run_blocking(lambda: ran.append(1), timeout=0.3, what="排队撤回探针")
        )
        check("超时返回 (False, None)", (ok, val) == (False, None), repr((ok, val)))
        gate.set()                                     # 放开工位，让队列里的活儿有机会被领走
        time.sleep(0.5)
        check("★超时后被撤回的任务没有白跑（ran 仍为空）", ran == [], str(ran))
        check("撤回的那笔不占积压（pending 归零）", ex.pending == 0, str(ex.pending))
    finally:
        gate.set()
        ex.shutdown(wait=False)
        lw._DAEMON_RUNNER = old


# ============================================================ [2] 事件播报布尔
class _EvtFake:
    """只带 `_on_server_event` 需要的那点装配（真的调用插件里的实现，不复制一份）。"""

    EVENT_TO_NOTIFY_GROUP = McControlPlugin.EVENT_TO_NOTIFY_GROUP

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sent: list = []
        self._recent_events = collections.deque(maxlen=50)
        self.logger = _QuietLog()
        self._terminated = False

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def is_remote_mode(self) -> bool:
        return False

    async def _maybe_bridge_to_group(self, etype, player, detail) -> bool:
        return False

    def _notify_target_allows(self, target, group) -> bool:
        return True

    async def _send_to_targets(self, targets, chain) -> bool:
        self.sent.append(list(targets))
        return True


_EvtFake._cfg_bool = McControlPlugin._cfg_bool
_EvtFake._on_server_event = McControlPlugin._on_server_event


def _fire(cfg: dict, etype: str, player: str = "Steve", detail: str = "") -> list:
    fake = _EvtFake(cfg)
    asyncio.run(fake._on_server_event(etype, player, detail))
    return fake.sent


def part_p2_event_bool() -> None:
    print("================ [2] 事件播报：字符串 \"false\" 必须真的关掉（P4 收尾） ================")
    base = {"enable_event_listener": True, "notify_targets": ["aiocqhttp:GroupMessage:1"]}

    check("★notify_join_leave=\"false\" → 进服不播报",
          _fire({**base, "notify_join_leave": "false"}, EVENT_JOIN) == [])
    check("★notify_join_leave=\"false\" → 离服同样不播报",
          _fire({**base, "notify_join_leave": "false"}, EVENT_LEAVE) == [])
    check("notify_join_leave=\"false\"（小写/带空格）也认得出",
          _fire({**base, "notify_join_leave": " FALSE "}, EVENT_JOIN) == [])
    check("notify_join_leave=true → 照常播报（没把开关改成一刀切）",
          len(_fire({**base, "notify_join_leave": True}, EVENT_JOIN)) == 1)
    check("notify_join_leave=\"true\" → 照常播报",
          len(_fire({**base, "notify_join_leave": "true"}, EVENT_JOIN)) == 1)
    check("★enable_event_listener=\"false\" → 一切都静默",
          _fire({**base, "enable_event_listener": "false",
                 "notify_join_leave": True}, EVENT_JOIN) == [])
    check("notify_chat=\"false\" → 聊天不播报",
          _fire({**base, "notify_join_leave": True, "notify_chat": "false"},
                EVENT_CHAT) == [])
    check("notify_targets 为空 → 不播报（老行为未被改动）",
          _fire({"enable_event_listener": True, "notify_targets": [],
                 "notify_join_leave": True}, EVENT_JOIN) == [])
    check("无法判定的值（\"maybe\"）按默认值走，且不静默反转语义",
          len(_fire({**base, "notify_join_leave": "maybe"}, EVENT_JOIN)) == 1)

    main_src = src("main.py")
    body = main_src.split("async def _on_server_event", 1)[1][:2000]
    check("六条播报分支一律走 _cfg_bool（不再裸读配置真值）",
          body.count("self._cfg_bool(") >= 6, str(body.count("self._cfg_bool(")))


# ============================================================ [3] WebAPI 请求体
class _FakeRequest:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def json(self):
        return self.payload


class _FakeKb:
    def __init__(self) -> None:
        self.calls: list = []

    def set_enabled(self, topic, flag):
        self.calls.append(("toggle", topic, flag))
        return {"topic": topic, "enabled": flag}

    def delete_entry(self, topic):
        self.calls.append(("delete", topic))
        return True

    def approve_entry(self, topic, flag):
        self.calls.append(("approve", topic, flag))
        return {"topic": topic, "status": "verified" if flag else "untested"}

    def save_entry(self, topic, content, **kw):
        self.calls.append(("save", topic))
        return {"topic": topic, "content": content[:20]}

    def rename_exists(self, old, new) -> bool:
        return False

    def get_entry(self, topic):
        return {"topic": topic}

    def save_health(self) -> dict:
        return {"ok": True}


class _FakePlugin:
    def __init__(self, kb) -> None:
        self._knowledge = kb


def _api(kb=None) -> wa.McControlWebApi:
    api = wa.McControlWebApi.__new__(wa.McControlWebApi)
    api.plugin = _FakePlugin(kb if kb is not None else _FakeKb())
    return api


def _call(handler, payload) -> dict:
    wa.request = _FakeRequest(payload)
    resp = asyncio.run(handler())
    return json.loads(resp.body)


def part_p3_body_shape() -> None:
    print("================ [3] WebAPI：顶层不是对象 / topic 是数字都要说人话（P3/P5 收尾） ================")
    original = wa.request
    try:
        kb = _FakeKb()
        api = _api(kb)
        r = _call(api.toggle_entry, [1, 2, 3])
        check("★数组请求体 → ok:False 且点明「需要 JSON 对象」",
              r.get("ok") is False and "对象" in (r.get("error") or ""), str(r))
        r = _call(api.toggle_entry, "topic=x")
        check("★字符串请求体 → ok:False（不再 AttributeError 成 500）",
              r.get("ok") is False and "对象" in (r.get("error") or ""), str(r))
        r = _call(api.delete_entry, 123)
        check("★数字请求体 → ok:False", r.get("ok") is False, str(r))
        check("上面三次都没有碰到知识库（脏输入不进业务层）", kb.calls == [], str(kb.calls))

        for handler, name in ((api.toggle_entry, "toggle"),
                              (api.delete_entry, "delete"),
                              (api.approve_entry, "approve")):
            r = _call(handler, {"topic": 123, "enabled": True, "approve": True})
            check(f"★{name}: topic=123 → ok:False 且说明收到 int",
                  r.get("ok") is False and "int" in (r.get("error") or ""), str(r))
            r = _call(handler, {"topic": {"a": 1}})
            check(f"★{name}: topic=对象 → ok:False 且说明收到 dict",
                  r.get("ok") is False and "dict" in (r.get("error") or ""), str(r))
        check("非法 topic 一笔都没落到知识库", kb.calls == [], str(kb.calls))

        r = _call(api.toggle_entry, {"topic": " 某方案 ", "enabled": "false"})
        check("合法 topic 去空白后照常生效，enabled 走严格布尔",
              r.get("ok") is True and kb.calls[-1] == ("toggle", "某方案", False),
              str((r.get("ok"), kb.calls[-1:])))
        r = _call(api.toggle_entry, {"topic": "某方案", "enabled": "maybe"})
        check("enabled 给垃圾值 → 明确拒收（不是静默当 False）",
              r.get("ok") is False and "布尔" in (r.get("error") or ""), str(r))
    finally:
        wa.request = original


# ============================================================ [4] 无接收者 + 边界
def part_p4_no_recipient_boundary() -> None:
    print("================ [4] 空服回执：边界没确认时不许给绿字（P3 收尾） ================")
    cmd = 'tellraw @a {"text":"hi"}'
    unknowable = "No player was found"

    r = classify_command_output(cmd, unknowable, response_received=True,
                                boundary_confirmed=False)
    check("★无接收者 + 边界未确认 → dispatched_unconfirmed（不冒充成功）",
          r.status == "dispatched_unconfirmed", r.status)
    check("★仍保留 no_recipient 事实（界面能同时说「没人收」）",
          getattr(r, "no_recipient", False) is True, str(getattr(r, "no_recipient", None)))
    check("未确认态的说明里点名边界未确认", "边界" in (r.reason or ""), r.reason)

    r = classify_command_output(cmd, unknowable, response_received=True,
                                boundary_confirmed=True)
    check("边界已确认 → 无接收者仍是成功（空服广播不是失败）",
          r.status == "success" and getattr(r, "no_recipient", False) is True, r.status)

    code = src("core/command_result.py")
    seg = code.split("is_no_recipient_output(command, out_s)", 1)[1][:900]
    check("★判定顺序：边界检查在无条件 success 之前",
          seg.find("boundary_confirmed is False") != -1
          and seg.find("boundary_confirmed is False") < seg.find('res.status = "success"'),
          seg[:120])
    check("广播的送达集合仍只有 success",
          wa._BROADCAST_OK_STATUSES == frozenset({"success"}),
          str(sorted(wa._BROADCAST_OK_STATUSES)))


# ============================================================ [5] 知识长度上限
def part_p5_knowledge_length() -> None:
    print("================ [5] 知识条目：超长当场拒收，界面 / 工具同一句话（P5 收尾） ================")
    check("上限是明面上的常数",
          kbmod.MAX_TOPIC_CHARS > 0 and kbmod.MAX_CONTENT_CHARS > 0,
          f"{kbmod.MAX_TOPIC_CHARS}/{kbmod.MAX_CONTENT_CHARS}")
    # 上限值**钉死**：它是产品口径（CHANGELOG / 界面提示 / 工具回执都按这两个数字说话），
    # 不是「代码里随便取多少都行」的实现细节。写成 `kbmod.MAX_*` 就等于让用例跟着实现跑 ——
    # 把上限放大到 10 亿，用例照样全绿（变异 M13 实跑 SURVIVED，就是这条漏洞）。
    check("★主题上限 = 200 字符、正文上限 = 20000 字符（与文案同源）",
          kbmod.MAX_TOPIC_CHARS == 200 and kbmod.MAX_CONTENT_CHARS == 20000,
          f"{kbmod.MAX_TOPIC_CHARS}/{kbmod.MAX_CONTENT_CHARS}")
    check("正常条目放行", kbmod.knowledge_length_error("方案", "短正文") == "")
    t_over = kbmod.knowledge_length_error("x" * (kbmod.MAX_TOPIC_CHARS + 1), "短正文")
    check("主题超 1 字符 → 拒收并给出上限与超限量",
          "上限" in t_over and str(kbmod.MAX_TOPIC_CHARS) in t_over, t_over)
    c_over = kbmod.knowledge_length_error("方案", "y" * (kbmod.MAX_CONTENT_CHARS + 1))
    check("正文超 1 字符 → 拒收并说明「拆成一条方案一条知识」",
          "上限" in c_over and "拆" in c_over, c_over)
    check("恰好等于上限 → 放行（边界不越界）",
          kbmod.knowledge_length_error("x" * kbmod.MAX_TOPIC_CHARS,
                                       "y" * kbmod.MAX_CONTENT_CHARS) == "")

    original = wa.request
    try:
        kb = _FakeKb()
        api = _api(kb)
        r = _call(api.save_entry, {"topic": "方案",
                                   "content": "y" * (kbmod.MAX_CONTENT_CHARS + 1)})
        check("★WebUI 写接口超长 → ok:False 且给的是同一句说明",
              r.get("ok") is False and "上限" in (r.get("error") or ""), str(r)[:200])
        check("超长条目没有写进知识库", kb.calls == [], str(kb.calls))
    finally:
        wa.request = original

    check("★LLM 工具与 WebUI 共用同一处上限（不是各写一套）",
          McControlPlugin._knowledge_too_long("方案", "y" * (kbmod.MAX_CONTENT_CHARS + 1))
          == kbmod.knowledge_length_error("方案", "y" * (kbmod.MAX_CONTENT_CHARS + 1)))
    check("工具侧也拒收超长主题",
          "上限" in McControlPlugin._knowledge_too_long("x" * 999, "短"))


# ============================================================ [6] 设置项字符串
def part_p6_settings_str_field() -> None:
    print("================ [6] 设置写接口：字符串字段不再静默 str() 化（P3 收尾） ================")
    api = _api()
    parsed, err = api._validate_settings({"rcon_host": {"a": 1}})
    check("★对象塞进字符串项 → 明确拒收并点名类型",
          parsed is None and "字符串" in (err or "") and "dict" in (err or ""), str(err))
    parsed, err = api._validate_settings({"rcon_host": 12345})
    check("数字塞进字符串项 → 同样拒收（端口类走 INT_RANGES，不在这里兜）",
          parsed is None and "字符串" in (err or ""), str(err))
    parsed, err = api._validate_settings({"rcon_host": " 1.2.3.4 "})
    check("正常字符串去空白后通过",
          err is None and parsed.get("rcon_host") == "1.2.3.4", str((parsed, err)))
    parsed, err = api._validate_settings({"notify_join_leave": "false"})
    check("布尔项沿用严格解析（\"false\" → False）",
          err is None and parsed.get("notify_join_leave") is False, str((parsed, err)))


# ============================================================ [7] 状态写入的布尔
def part_p7_state_set_bool() -> None:
    print("================ [7] 知识库状态写入：set() 不再留第二种布尔语义（P4 收尾） ================")
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="kbstate_")
    st = kbmod.KnowledgeState(tmpdir)
    check("新目录默认三项全开（默认值不被静默改写）",
          (st.enabled, st.learning, st.auto_apply) == (True, True, True),
          str(st.as_dict()))

    out = st.set(enabled="false")
    check("★set(enabled=\"false\") → 真的关（旧实现 bool(\"false\") 是 True）",
          st.enabled is False and out.get("enabled") is False, str(out))
    on_disk = json.loads((Path(tmpdir) / "knowledge_state.json").read_text(encoding="utf-8"))
    check("★落盘的是 JSON 真布尔，不是字符串",
          on_disk.get("enabled") is False, str(on_disk))

    st.set(learning="maybe")
    check("★读不出结论（\"maybe\"）→ 按「关」处理，方向保守",
          st.learning is False, str(st.as_dict()))

    st.set(auto_apply=1)
    check("传 0/1 这类老习惯仍按严格解析走（1 → True）",
          st.auto_apply is True, str(st.as_dict()))

    st.set(enabled=None)
    check("None = 不动这一项（不是「改成 False」）",
          st.enabled is False, str(st.as_dict()))


# ============================================================ [8] 条目级布尔 + 前端显示
def part_p8_entry_bool_and_ui() -> None:
    print("================ [8] 知识条目布尔：文件里写 \"false\" 也真的关（P4 收尾·2） ================")
    import re
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "kb"
        kb = kbmod.ModKnowledgeBase(str(data_dir), server_id="fp", preset_id="p1",
                                    preset_name="测试库", fingerprint="fp")
        topic = "tac 弹匣"
        kb.save_entry(topic, "扩容弹匣 30 发", source="webui", status="verified", manual=True)
        check("正常条目在可检索集合里", topic in kb._searchable())

        # ① 落盘后手工把条目级 enabled 改成字符串，重新 load
        raw = json.loads(kb.file_path.read_text(encoding="utf-8"))
        raw["entries"][topic]["enabled"] = "false"
        raw["entries"]["手写条目"] = {"content": "手写的一条知识", "status": "verified",
                                     "enabled": " FALSE ", "created_at": "", "updated_at": ""}
        kb.file_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        kb2 = kbmod.ModKnowledgeBase(str(data_dir), server_id="fp", preset_id="p1",
                                     preset_name="测试库", fingerprint="fp")
        check("★enabled=\"false\" 的条目不再进检索池", topic not in kb2._searchable())
        check("★load() 把它收敛成 JSON 真布尔（不把字符串留在库里）",
              kb2.get_entry(topic)["enabled"] is False, str(kb2.get_entry(topic)))
        check("带空白的 \" FALSE \" 同样收敛成 False",
              kb2.get_entry("手写条目")["enabled"] is False, str(kb2.get_entry("手写条目")))
        # v0.24.8 第四轮自审（A2）：上面那条只证明「读出来是真布尔」——下游 _searchable()/
        # get_entry() 各自也走了严格解析，所以**删掉 load() 的收口它们照样绿**（实测假绿）。
        # 收口点的意义恰恰是「下游不必各自再解析」，所以必须直接看内存里的 _data。
        check("★收口点在 load()：内存 _data 里已是真布尔（不靠下游各自再解析）",
              kb2._data["entries"][topic]["enabled"] is False,
              repr(kb2._data["entries"][topic]["enabled"]))
        hits = kb2.search("扩容弹匣")
        check("★检索结果里也搜不到被禁用的条目",
              all(str(h.get("topic")) != topic for h in hits), str(hits[:2]))

        # ② 绕过 load 直接改内存：_searchable() 自己也得严格判
        kb2._data["entries"][topic]["enabled"] = "false"
        check("★绕过 load 直接写内存，_searchable() 仍按严格解析排除",
              topic not in kb2._searchable())

        # ③ set_enabled 的入参同样不许软
        out = kb2.set_enabled(topic, "false")
        check("★set_enabled(topic, \"false\") → 落成 False（旧实现是 True）",
              (out or {}).get("enabled") is False, str(out))
        out = kb2.set_enabled(topic, "maybe")
        check("写入口读不出结论 → 回落「禁用」并留 WARNING（方向保守）",
              (out or {}).get("enabled") is False, str(out))
        on_disk = json.loads(kb2.file_path.read_text(encoding="utf-8"))
        check("落盘值是真布尔（文件里不再长回 \"false\"）",
              on_disk["entries"][topic]["enabled"] is False, str(on_disk["entries"][topic]))

    html = src("pages/mc_control/index.html")
    # 这两条是**存在性**判据（第六轮复核 B6 逐条实测过：把函数体整段换成 `return !!v;`、
    # 或删掉数字分支，它们照样绿）—— 留着是为了「入口还在」，真正的**逐格行为**判据在
    # `tests/test_v0248_same_source_bool.py`（从页面抽取 parseCfgBool 原文，在 node 里跟
    # 后端 strict_bool 逐格比对，外加五种变异必须报红）。
    check("设置页布尔渲染的入口还在（`parseCfgBool(v, dft)`）",
          "parseCfgBool(v, dft)" in html)
    _weak = [ln for ln in html.splitlines() if ".checked" in ln and "!!v" in ln]
    check("设置页不再对配置值直接取两次非（弱判据；行为判据见 same_source_bool）",
          not _weak, str(_weak[:2]))


# ============================================================ [9] 根目录 .log 兜底
def part_p9_root_log_ignored_by_git() -> None:
    print("================ [9] 根目录 .log：不看名字，实测 git 真的忽略（第四轮自审·A3） ================")
    import subprocess
    # 为什么不能只查 .gitignore 里有没有那几行：名字是随手起的（`_r4_verify.log` 就这样漏过一次），
    # 「按名字一条条补」的规则永远追不上起名的手速。这里直接问 git —— 与 `git add -A` 同一条判定链。
    probe = PLUGIN_DIR / "_r4_probe_tmp.log"
    try:
        probe.write_text("C:\\Users\\<账户>\\.astrbot\\data 本机绝对路径\r\n", encoding="utf-8")
        r = subprocess.run(["git", "check-ignore", "-q", probe.name],
                           cwd=str(PLUGIN_DIR), capture_output=True)
        check("★仓库根目录任意 .log 都被忽略（兜底 /*.log，而不是按名字一条条补）",
              r.returncode == 0,
              f"git check-ignore rc={r.returncode}：这个名字没被兜住，`git add -A` 会把它带走")
    finally:
        if probe.exists():
            probe.unlink()
    gi = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8")
    check("兜底规则 /*.log 仍在（且是根目录锚定，不会误伤子目录里的 .log）",
          "/*.log" in gi and not any(
              ln.strip() == "*.log" for ln in gi.splitlines()))


def main() -> None:
    part_p1_queue_bound()
    part_p1_timeout_withdraw()
    part_p2_event_bool()
    part_p3_body_shape()
    part_p4_no_recipient_boundary()
    part_p5_knowledge_length()
    part_p6_settings_str_field()
    part_p7_state_set_bool()
    part_p8_entry_bool_and_ui()
    part_p9_root_log_ignored_by_git()
    print("\n================ 汇总 ================")
    print(f"通过 {PASSED[0]} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for d in FAILED:
            print(f"  - {d}")
        raise SystemExit(1)
    print("[ok] 全部通过")


if __name__ == "__main__":
    main()
