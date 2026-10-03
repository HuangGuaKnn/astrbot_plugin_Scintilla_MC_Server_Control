# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第二批）回归：七处「不报错、但悄悄坏掉」的收口。

本轮修的都不是崩溃，而是**静默失效** —— 功能看着还在，实际已经不干活了：

  A. 喊话闸门的配置键没进后端白名单：界面上填 200 / 5，保存时后端认不出、
     判成「未接受」，界面长期误报「请重载插件」，而设置其实压根没落盘。
     （前端提交键 ⊆ 后端接受键 —— 这里加一条通用守卫，别再手工同步两份清单。）

  B. 日志监听：轮转判定只看「文件变短」→ 换了等长的文件就永远错位；轮询一直
     抛异常也只是安静地不播报，没人知道；stop() 无超时；单行几十万字符照进正则。

  C. 热重载：0.6 秒延迟窗口里如果已经发生过一次重载，本次排程仍然会再重载一遍
     （它带收口豁免，取消不了）→ 用一次性凭证让它自己作废。

  D. 后台任务收口：超时未退出的任务被从册子里清空后「查无此人」，快照自相矛盾。

  E. 知识库向量任务：启动补算与写入后的补算用了两个不同的任务名，
     按名字配对的 policy="skip" 形同虚设，两条路径能同时改同一份索引。

  F. 落盘告警：save_prompts / reset_prompt / save_colors 三个配置写接口
     丢了写盘结果，界面永远显示「已保存」，重启后改动凭空消失。

运行：
  <AstrBot python> tests\\test_v0235_review_round2.py
"""
from __future__ import annotations

import ast
import asyncio
import gc
import importlib.util
import json
import logging
import re
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

PLUGIN = PLUGIN_DIR
MAIN = PLUGIN / "main.py"
WEB_API = PLUGIN / "core" / "web_api.py"
SCHEMA = PLUGIN / "_conf_schema.json"
HTML = PLUGIN / "pages" / "mc_control" / "index.html"

import io  # noqa: E402

SRC = io.open(MAIN, encoding="utf-8").read()
API_SRC = io.open(WEB_API, encoding="utf-8").read()
HTML_SRC = io.open(HTML, encoding="utf-8").read()
TREE = ast.parse(SRC)
API_TREE = ast.parse(API_SRC)

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def dict_literal(tree: ast.AST, name: str) -> dict:
    """从模块源码里取某个模块级常量的字面量值（不 import，免受副作用影响）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == name:
                    try:
                        return ast.literal_eval(node.value)
                    except ValueError:
                        return {}
    return {}


def method_src(tree: ast.AST, name: str) -> str:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    return ""


# ==================================================================== A. 喊话闸门

def part_a() -> None:
    print("================ [A] 喊话闸门：配置键前后端闭环 ================")
    ints = dict_literal(API_TREE, "INT_RANGES")
    floats = dict_literal(API_TREE, "FLOAT_RANGES")

    check("后端整数白名单收了 say_max_chars", "say_max_chars" in ints, str(list(ints)))
    check("后端浮点白名单收了 say_cooldown_seconds",
          "say_cooldown_seconds" in floats, str(list(floats)))
    check("长度下限允许 0（0 = 不限）", ints.get("say_max_chars", (9, 9))[0] == 0)
    check("冷却下限允许 0（0 = 不限）", floats.get("say_cooldown_seconds", (9.0, 9.0))[0] == 0.0)

    schema = json.loads(io.open(SCHEMA, encoding="utf-8").read())
    items = schema["commands"]["items"]
    lo, hi = ints["say_max_chars"]
    check("schema 默认长度落在白名单区间内",
          lo <= items["say_max_chars"]["default"] <= hi,
          f"{items['say_max_chars']['default']} not in ({lo},{hi})")
    flo, fhi = floats["say_cooldown_seconds"]
    check("schema 默认冷却落在白名单区间内",
          flo <= items["say_cooldown_seconds"]["default"] <= fhi,
          f"{items['say_cooldown_seconds']['default']} not in ({flo},{fhi})")

    # 闸门读的键名必须就是上面这三个地方约定的键名
    gate = method_src(TREE, "_say_gate")
    check("_say_gate 读的正是 say_max_chars", 'self._cfg("say_max_chars"' in gate)
    check("_say_gate 读的正是 say_cooldown_seconds",
          'self._cfg("say_cooldown_seconds"' in gate)

    # 前端提交链路
    block = HTML_SRC.split("const CFG_FIELDS = [", 1)[1].split("];", 1)[0]
    fields = re.findall(r'\[\s*"([\w]+)"\s*,\s*"([\w]+)"\s*,\s*"([bnlps])"', block)
    keys = {k: ty for _id, k, ty in fields}
    check("前端提交 say_max_chars（n 型）", keys.get("say_max_chars") == "n", str(keys.get("say_max_chars")))
    check("前端提交 say_cooldown_seconds（n 型）",
          keys.get("say_cooldown_seconds") == "n", str(keys.get("say_cooldown_seconds")))
    check("设置页有这两个输入框",
          'id="cfg_cmd_say_max"' in HTML_SRC and 'id="cfg_cmd_say_cd"' in HTML_SRC)

    # 通用守卫：所有数字型设置键都必须被后端接受，否则又是「保存成功但没保存」
    allowed = set(ints) | set(floats)
    missing = sorted(k for k, ty in keys.items() if ty == "n" and k not in allowed)
    check("全部数字型设置键都被后端白名单接受（防再犯）", not missing, str(missing))


# ==================================================================== B. 日志监听

def _load_watcher():
    spec = importlib.util.spec_from_file_location(
        "v235r2_log_watcher", PLUGIN / "core" / "log_watcher.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ensure_astrbot_api() -> None:
    """log_watcher 从 astrbot.api 取日志器 —— 没装 AstrBot 时注一个最小替身。"""
    try:
        import astrbot.api  # noqa: F401
    except Exception:
        import types
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("v235r2.stub")
        pkg = types.ModuleType("astrbot")
        pkg.api = api
        sys.modules["astrbot"] = pkg
        sys.modules["astrbot.api"] = api


_ensure_astrbot_api()
LW = _load_watcher()


def _watcher(root: Path, events: list):
    (root / "logs").mkdir(parents=True, exist_ok=True)

    async def on_event(etype, player, detail):
        events.append((etype, player, detail))

    w = LW.LogWatcher(str(root), on_event, poll_interval=0.01)
    w._enc = "utf-8"
    return w


def _logfile(root: Path) -> Path:
    return root / "logs" / "latest.log"


async def part_b_rotate(tmp: Path) -> None:
    print("================ [B1] 日志轮转：等长换文件也能认出来 ================")
    events: list = []
    w = _watcher(tmp / "rot", events)
    p = _logfile(tmp / "rot")

    # 两个**等长**的文件：旧的只有填充行，新的开头就有一句可解析的聊天。
    # 修复前：size == _pos → 直接 return，永远读不到新文件的内容。
    chat = b"[16:30:01] [Server thread/INFO]: <Steve> hi\n"
    old = b"# old filler\n" + b"x" * 400
    new = chat + b"y" * (len(old) - len(chat))
    assert len(old) == len(new), (len(old), len(new))
    p.write_bytes(old)
    w._sig = w._file_sig()
    w._head = w._head_sig()
    w._pos = len(old)

    # 对照组：不换文件 → 什么都不该发生
    await w._poll()
    check("文件没变时安静（不产出事件）", events == [], str(events))

    p.write_bytes(new)                       # 同尺寸换文件
    await w._poll()
    check("等长换文件被识别为轮转（读到了新内容）",
          any(e[0] == "chat" and e[1] == "Steve" for e in events), str(events))
    check("轮转后位置按新文件重算", w._pos == len(chat), str(w._pos))

    # 经典轮转（文件变短）仍照旧生效
    events2: list = []
    w2 = _watcher(tmp / "rot2", events2)
    p2 = _logfile(tmp / "rot2")
    p2.write_bytes(b"#" * 200 + b"\n")
    w2._pos = 200
    p2.write_bytes(chat)
    await w2._poll()
    check("文件变短仍判为轮转", any(e[0] == "chat" for e in events2), str(events2))


async def part_b_health(tmp: Path) -> None:
    print("================ [B2] 日志监听健康度与轮询异常留痕 ================")
    events: list = []
    w = _watcher(tmp / "health", events)
    p = _logfile(tmp / "health")
    p.write_bytes(b"[16:30:01] [Server thread/INFO]: <Steve> hi\n")

    h0 = w.health()
    check("health 必备字段齐全",
          {"running", "path", "pos", "encoding", "error_count", "last_error"} <= set(h0),
          str(set(h0)))
    check("未 start 时 running=False", h0["running"] is False)
    check("health 里带上了日志路径", str(p) == h0["path"], h0["path"])

    await w.start()
    await asyncio.sleep(0.05)
    check("start 后 running=True", w.health()["running"] is True)
    await w.stop()
    check("stop 后 running=False", w.health()["running"] is False)

    # 轮询连续失败 → 记数 + 留痕；恢复正常 → 清零
    w2 = _watcher(tmp / "err", events)
    async def boom():
        raise OSError("模拟读盘失败")
    w2._poll = boom
    w2._running = True
    w2._task = asyncio.ensure_future(w2._loop())
    await asyncio.sleep(0.06)
    h1 = w2.health()
    check("连续失败被记数", h1["error_count"] >= 1, str(h1))
    check("最近错误留痕（含异常类型与原因）",
          "OSError" in h1["last_error"] and "模拟读盘失败" in h1["last_error"],
          h1["last_error"])
    check("循环没被异常打断（任务仍在跑）", not w2._task.done())
    w2._poll = LW.LogWatcher._poll.__get__(w2)     # 恢复真实轮询
    await asyncio.sleep(0.06)
    check("恢复正常后计数清零", w2.health()["error_count"] == 0, str(w2.health()))
    w2._running = False
    w2._task.cancel()
    try:
        await w2._task
    except asyncio.CancelledError:
        pass


async def part_b_stop_timeout(tmp: Path) -> None:
    print("================ [B3] stop() 不能被卡住的监听协程拖住 ================")
    events: list = []
    w = _watcher(tmp / "stuck", events)
    (tmp / "stuck").mkdir(parents=True, exist_ok=True)

    async def stubborn():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(30)        # 吞掉取消：模拟不理会取消的 IO
    w._poll = stubborn
    w._running = True
    w._task = asyncio.ensure_future(w._loop())
    await asyncio.sleep(0.02)

    old_timeout = LW.STOP_TIMEOUT
    LW.STOP_TIMEOUT = 0.3                  # 缩短等待，用例不必真等 5 秒
    try:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(w.stop(), timeout=5.0)
            finished = True
        except asyncio.TimeoutError:
            finished = False
        elapsed = time.monotonic() - t0
    finally:
        LW.STOP_TIMEOUT = old_timeout

    check("stop() 在超时后放弃等待并返回（没卡死）", finished, f"elapsed={elapsed:.2f}s")
    check("放弃等待只花掉超时那点时间", elapsed < 3.0, f"elapsed={elapsed:.2f}s")
    check("stop() 后任务句柄已摘除", w._task is None)
    check("stop() 后 running=False", w.health()["running"] is False)


async def part_b_long_line(tmp: Path) -> None:
    print("================ [B4] 超长单行截断 ================")
    events: list = []
    w = _watcher(tmp / "long", events)
    p = _logfile(tmp / "long")
    huge = b"[16:30:01] [Server thread/INFO]: <Steve> " + b"z" * 30000 + b"\n"
    p.write_bytes(huge)
    w._pos = 0
    await w._poll()
    check("超长行仍能解析出事件（不是整行丢弃）",
          any(e[0] == "chat" for e in events), str(events)[:120])
    if events:
        detail = events[0][2]
        check("解析用的行已被截断到 MAX_LINE_CHARS",
              len(detail) <= LW.MAX_LINE_CHARS,
              f"len={len(detail)} max={LW.MAX_LINE_CHARS}")
    check("截断上限是「够用且不失控」的量级",
          isinstance(LW.MAX_LINE_CHARS, int) and 1024 <= LW.MAX_LINE_CHARS <= 65536,
          str(LW.MAX_LINE_CHARS))


# ==================================================================== C. 热重载

class _FakeLogger:
    def __init__(self, sink: list):
        self.sink = sink

    def info(self, fmt, *a):
        self.sink.append(("info", fmt % a if a else fmt))

    def warning(self, fmt, *a):
        self.sink.append(("warn", fmt % a if a else fmt))


def _reload_fake(ns: dict, drop_spawn: bool = False):
    helpers = (
        "    def _plugin_manager(self):\n"
        "        return 'PM'\n"
        "    def _spawn_bg(self, name, coro, **kw):\n"
        "        if self.drop_spawn:\n"
        "            coro.close()\n"
        "            return None\n"
        "        t = asyncio.ensure_future(coro)\n"
        "        self.tasks.append(t)\n"
        "        return t\n"
    )
    body = method_src(TREE, "_schedule_hot_reload")
    exec("class Fake:\n" + helpers + "\n".join("    " + l for l in body.splitlines()) + "\n", ns)
    f = ns["Fake"]()
    f.tasks = []
    f.drop_spawn = drop_spawn
    f._hot_reload_token = None
    f.log = []
    f.logger = _FakeLogger(f.log)
    return f


async def part_c() -> None:
    print("================ [C] 热重载：过期排程自行作废 ================")
    calls: list = []
    ns: dict = {
        "asyncio": asyncio,
        "plugin_dirs_for": lambda pm, n: ["x"],
        "is_patch_installed": lambda: True,
        "perform_hot_reload": lambda pm, t: _fake_reload(calls, pm, t),
    }

    # ① 无人打扰：排程 → 凭证被认领 → 真的重载一次
    f1 = _reload_fake(ns)
    msg = f1._schedule_hot_reload(None, delay=0.02)
    check("排程时武装凭证", f1._hot_reload_token is not None)
    check("回文案明确说已安排", "已安排重载" in msg, msg)
    await asyncio.sleep(0.08)
    check("无干扰时执行了一次重载", calls == ["PM"], str(calls))
    check("执行后凭证已认领（清空）", f1._hot_reload_token is None)

    # ② 窗口期内已有另一次重载 → 本次自行放弃
    calls.clear()
    f2 = _reload_fake(ns)
    f2._schedule_hot_reload(None, delay=0.05)
    f2._hot_reload_token = None            # 等价于 terminate() 被调过一次
    await asyncio.sleep(0.12)
    check("凭证失效后不再动手（不重复重载）", calls == [], str(calls))
    check("跳过时写了日志说明原因",
          any("过期" in t for _lv, t in f2.log), str(f2.log))

    # ③ 被 skip 丢弃时：不能谎报「已安排」，也不能动正在跑的那一轮的凭证
    calls.clear()
    f3 = _reload_fake(ns, drop_spawn=True)
    tok = object()
    f3._hot_reload_token = tok
    msg3 = f3._schedule_hot_reload(None, delay=0.02)
    check("被丢弃时明说没排上（不谎报「已安排」）",
          "已安排" not in msg3 and "没重复排" in msg3, msg3)
    check("被丢弃时不改动正在跑的那一轮凭证", f3._hot_reload_token is tok)

    # ④ 静态：延迟窗口里的死代码不能留（曾经有两个 _runner 定义）
    check("_schedule_hot_reload 里只有一个 _runner 定义",
          method_src(TREE, "_schedule_hot_reload").count("async def _runner") == 1)
    check("全仓只有一个 async def _runner（无残留死代码）",
          SRC.count("async def _runner") == 1, str(SRC.count("async def _runner")))
    term = method_src(TREE, "terminate")
    check("terminate() 会作废延迟窗口里的排程",
          "_hot_reload_token = None" in term)


async def _fake_reload(calls: list, pm, target):
    calls.append(pm)
    return True, "ok"


# ==================================================================== D. 收口留档

async def part_d(PLUGIN) -> None:
    print("================ [D] 收口放弃等待的任务要留名字 ================")
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("v235r2_bg", PLUGIN / "core" / "bg_tasks.py")
    bgt = ilu.module_from_spec(spec)
    spec.loader.exec_module(bgt)

    log = logging.getLogger("v235r2_bg")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    recs: list[str] = []

    class R(logging.Handler):
        def emit(self, record):
            recs.append(record.getMessage())

    log.addHandler(R())
    bg = bgt.BackgroundTasks(log, shutdown_timeout=0.05)

    async def stubborn():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(30)        # 吞掉取消 = 收口等不到它

    task = bg.spawn("stubborn", stubborn())
    await asyncio.sleep(0.02)
    await bg.shutdown()
    snap = bg.snapshot()
    check("超时未退出的任务名被留档",
          "stubborn" in snap.get("gave_up_at_shutdown", ()), str(snap))
    check("留档在快照里可查（不再自相矛盾）",
          isinstance(snap.get("gave_up_at_shutdown"), (list, tuple)))
    check("册子照旧清空（收口行为不变）", snap["alive"] == 0, str(snap))
    check("警告日志点名了是哪个任务",
          any("stubborn" in m for m in recs), str(recs))

    task.cancel()
    await asyncio.sleep(0.01)
    gc.collect()


# ==================================================================== E. 向量任务名

def part_e() -> None:
    print("================ [E] 知识库向量补算：任务名必须全局唯一 ================")
    name = None
    for node in ast.walk(TREE):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "KB_VECTORS_TASK" for t in node.targets
        ):
            name = ast.literal_eval(node.value)
    check("KB_VECTORS_TASK 常量在位", isinstance(name, str) and name, str(name))
    check("启动补算与写入补算都用同一个常量",
          SRC.count("_spawn_bg(self.KB_VECTORS_TASK") == 2,
          str(SRC.count("_spawn_bg(self.KB_VECTORS_TASK")))
    # 旧名字只允许出现在解释性注释里（源码字符串字面量中必须绝迹，否则又是两个槽位）
    stale = [n.value for n in ast.walk(TREE)
             if isinstance(n, ast.Constant) and n.value == "kb_vec_debounce"]
    check("旧的第二个任务名不再是活代码里的字符串（不会再各跑一份）",
          not stale, str(stale))


# ==================================================================== F. 落盘告警

def part_f() -> None:
    print("================ [F] 落盘失败必须浮出水面 ================")
    n = API_SRC.count('"save_warning": self._cfg_save_warning()')
    check("三个配置写接口都带了 save_warning", n >= 3, str(n))
    check("save_warning 语义统一由 _cfg_save_warning 给出",
          "def _cfg_save_warning" in API_SRC)
    check("main._save_config 会把落盘结果记在实例上",
          "_last_cfg_save_ok" in SRC and "_last_cfg_save_error" in SRC)
    check("提示词保存接入了落盘告警", 'showSaveWarn(r, $("pr_notice"))' in HTML_SRC)
    check("配色保存接入了落盘告警", 'showSaveWarn(r, $("clr_notice"))' in HTML_SRC)
    check("设置页保存接入了落盘告警", "showSaveWarn(r," in HTML_SRC and "saveWarnText" in HTML_SRC)


# ==================================================================== 主流程

async def _amain(tmp: Path) -> None:
    await part_b_rotate(tmp)
    await part_b_health(tmp)
    await part_b_stop_timeout(tmp)
    await part_b_long_line(tmp)
    await part_c()
    await part_d(PLUGIN)


def main() -> int:
    import tempfile

    part_a()
    part_e()
    part_f()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(_amain(Path(d)))

    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[FAIL] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[PASS] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
