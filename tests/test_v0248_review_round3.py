# -*- coding: utf-8 -*-
"""v0.24.8 回归（随 v0.24.8 发布）：GPT 第三轮复核 P1-P5 的可复现判据。

上一版（v0.24.7）把「严格解析」只贴在了 WebUI 写接口边界，配置热路径与执行器
仍然各说各话。本文件把五处修复钉成行为断言，而不是「源码里有没有某一行」：

  [1] **执行器并发有界**（P1）：提交 32 个阻塞任务，同时存活的 worker 数不超过
      `MAX_DAEMON_WORKERS`；且 32 个任务最终全部完成（有界不等于丢活）。
  [2] **停用会连僵尸回调一起取消**（P2）：只存在于 `_callback_stuck` 里的任务
      也必须被 cancel，并记进 `callbacks_abandoned`（`health()` 里看得见）。
      两道终止门（事件入口 / 每次发送前）用源码断言钉住。
  [3] **广播有「未确认」态**（P3）：送达集合只剩 `success`；`inferred_success` /
      `dispatched_unconfirmed` 单独成组；后端把 RCON 的结束边界传进判定；
      前端对 `confirmed === false` 用黄字而不是成功绿。
  [4] **布尔解析只有一份实现**（P4）：`web_api.parse_bool` 就是 `strict_bool.parse_bool`
      本人；`bool(self._cfg(...))` 在状态读取里只剩「非空字符串」那一处；
      知识库开关载入与工作流开关都走 `strict_bool`。
  [5] **预设接口输入校验**（P5）：`_str_field` 对数字 / 对象结构化拒绝，
      `required=True` 时空串与缺省都报错；五个接口都改用了它。

运行：
  <AstrBot python> tests\\test_v0248_review_round3.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import subprocess
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as lw  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import strict_bool as sb  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402

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


# ============================================================ [1] P1 执行器
def part_p1_executor() -> None:
    print("================ [1] 阻塞执行器：并发有界、不丢活（P1） ================")
    check("MAX_DAEMON_WORKERS 是个像样的常数（1 < N <= 8）",
          1 < lw.MAX_DAEMON_WORKERS <= 8, str(lw.MAX_DAEMON_WORKERS))

    ex = lw._DaemonThreadExecutor(thread_name_prefix="fix1-test")
    peak = [0]
    futs = []

    def job(i: int) -> int:
        peak[0] = max(peak[0], ex.active_threads)
        import time as _t
        _t.sleep(0.05)
        return i

    try:
        for i in range(32):
            futs.append(ex.submit(job, i))
            peak[0] = max(peak[0], ex.active_threads)
        check("提交 32 个任务后 worker 数从未超过上限",
              peak[0] <= lw.MAX_DAEMON_WORKERS,
              f"peak={peak[0]} max={lw.MAX_DAEMON_WORKERS}")
        check("_max_workers 等于真实上限（不再是写死 1）",
              ex._max_workers == lw.MAX_DAEMON_WORKERS, repr(ex._max_workers))
        results = [f.result(timeout=10) for f in futs]
        check("32 个任务全部完成且结果正确（有界不等于丢活）",
              results == list(range(32)), str(results[:5]))
    finally:
        ex.shutdown(wait=False)
    check("关闭后 submit 抛 RuntimeError（与线程池同一句话）",
          _raises(lambda: ex.submit(job, 0)))

    # v0.24.8 追加：submit 与 shutdown 的原子性。
    # 旧写法在锁外入队 —— 关闭那一瞬间提交的任务可能排在哨兵**之后**：
    # worker 领到哨兵退出，这笔任务永远不执行、Future 永远不 resolve
    # （界面上就是「一直转圈」）。这里用一个线程猛提交、主线程同时关闭，
    # 只断言一条不变量：所有被接受的 Future 最终都必须完成（被拒的抛 RuntimeError）。
    ex2 = lw._DaemonThreadExecutor(thread_name_prefix="fix1-race")
    accepted = []
    rejected = [0]

    def quick() -> int:
        time.sleep(0.001)
        return 1

    def hammer() -> None:
        for _ in range(400):
            try:
                accepted.append(ex2.submit(quick))
            except RuntimeError:
                rejected[0] += 1
                return

    th = threading.Thread(target=hammer, name="submit-hammer", daemon=True)
    th.start()
    time.sleep(0.01)
    ex2.shutdown(wait=False)
    th.join(5.0)
    for f in accepted:
        try:
            f.result(timeout=5.0)
        except Exception:                                      # noqa: BLE001
            pass
    stuck = sum(1 for f in accepted if not f.done())
    check("关闭瞬间提交的任务只会「被拒」或「被执行」，不存在永不 resolve 的孤儿",
          bool(accepted) and stuck == 0,
          f"accepted={len(accepted)} rejected={rejected[0]} stuck={stuck}")
    check("关闭后 pending 归零（队列里的活都收完了，不是被丢掉）",
          ex2.pending == 0, f"pending={ex2.pending}")


def _raises(fn) -> bool:
    try:
        fn()
    except RuntimeError:
        return True
    except Exception:                                          # noqa: BLE001
        return False
    return False


# ============================================================ [2] P2 停用
def part_p2_stop() -> None:
    print("================ [2] 停用：僵尸回调也要取消 + 记账（P2） ================")

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as td:
            w = lw.LogWatcher(str(Path(td)), lambda *a: None)

            async def never() -> None:
                await asyncio.sleep(3600)

            inflight = asyncio.create_task(never())
            zombie = asyncio.create_task(never())
            w._callback_inflight[inflight] = 0.0            # 在飞
            w._callback_stuck.add(zombie)                    # 已超时、只留在 stuck 里
            w._cancel_callbacks()
            await asyncio.sleep(0)                           # 让取消真的落地
            check("在飞回调被取消", inflight.cancelled())
            check("★只在 _callback_stuck 里的僵尸也被取消", zombie.cancelled())
            check("放弃数入账（callbacks_abandoned == 2）",
                  w.callbacks_abandoned == 2, str(w.callbacks_abandoned))
            check("health() 里看得见放弃数",
                  w.health().get("callbacks_abandoned") == 2,
                  str(w.health().get("callbacks_abandoned")))
            check("取消之后两个集合都清空（不留悬空引用）",
                  not w._callback_inflight and not w._callback_stuck)

    asyncio.run(scenario())

    main_src = src("main.py")
    check("事件入口有终止门（_on_server_event 读 _is_terminated）",
          "async def _on_server_event" in main_src
          and "_is_terminated(self)" in main_src.split("async def _on_server_event", 1)[1][:600])
    check("每次发送前有终止门（_send_to_targets 循环内检查）",
          "_is_terminated(self)" in main_src.split("async def _send_to_targets", 1)[1][:900])


# ============================================================ [3] P3 广播
def part_p3_broadcast() -> None:
    print("================ [3] 广播：未确认态不许冒充成功（P3） ================")
    check("送达集合只剩 success",
          wa._BROADCAST_OK_STATUSES == frozenset({"success"}),
          str(sorted(wa._BROADCAST_OK_STATUSES)))
    check("inferred_success 归入「未确认」组",
          "inferred_success" in wa._BROADCAST_UNCONFIRMED_STATUSES)
    check("dispatched_unconfirmed 归入「未确认」组",
          "dispatched_unconfirmed" in wa._BROADCAST_UNCONFIRMED_STATUSES)
    check("两个集合互不相交",
          not (wa._BROADCAST_OK_STATUSES & wa._BROADCAST_UNCONFIRMED_STATUSES))

    code = src("core/web_api.py")
    check("判定时读取 RCON 的结束边界（last_boundary_confirmed）",
          'boundary_confirmed=getattr(rcon, "last_boundary_confirmed"' in code)
    check("回包带 confirmed 字段", '"confirmed": confirmed' in code)

    html = src("pages/mc_control/index.html")
    check("前端对 confirmed === false 走单独分支", "r.confirmed === false" in html)
    check("前端未确认态用黄字 var(--warn)（不是成功绿）",
          "var(--warn)" in html.split("r.confirmed === false", 1)[1][:600])


# ============================================================ [4] P4 布尔
def part_p4_bool() -> None:
    print("================ [4] 布尔解析：只有一份实现（P4） ================")
    check("web_api.parse_bool 就是 strict_bool.parse_bool（再导出，不是副本）",
          wa.parse_bool is sb.parse_bool)
    check('parse_bool("false") is False', sb.parse_bool("false") is False)
    check('strict_bool("false", True) is False（不许 fail-open）',
          sb.strict_bool("false", True) is False)
    check('strict_bool("0", True) is False', sb.strict_bool("0", True) is False)
    check('strict_bool("maybe", True) 回落 default=True',
          sb.strict_bool("maybe", True) is True)
    check('strict_bool("maybe", False) 回落 default=False',
          sb.strict_bool("maybe", False) is False)

    warnings: list[str] = []

    class _Log:
        def warning(self, fmt, *args):
            warnings.append(fmt % args if args else fmt)

    sb.strict_bool("maybe", True, key="remote_rcon_mode", logger=_Log())
    check("无法判定时留 WARNING 且点名配置键",
          bool(warnings) and "remote_rcon_mode" in warnings[0], str(warnings[:1]))

    code = src("core/web_api.py")
    # v0.24.8 补：旧判据只扫 `bool(self._cfg(`，漏掉了 `bool(self.plugin._cfg(`
    # —— 改完 18 处后仍有一处 `remote_rcon_mode` 走裸 bool()（字符串 "false" 仍是开）。
    # 判据必须两种写法都扫，否则「收成唯一实现」只是自述。
    bare = [ln for ln in code.split("\n")
            if ("bool(self._cfg(" in ln or "bool(self.plugin._cfg(" in ln)
            and "strict_bool(" not in ln]
    check("配置读取里只剩非空字符串那一处裸 bool（rcon_password）",
          len(bare) == 1 and "rcon_password" in bare[0], str(bare[:2]))
    class _P:
        def _cfg(self, k, d=None):
            return "false"

    api = wa.McControlWebApi.__new__(wa.McControlWebApi)
    api.plugin = _P()
    check('remote_rcon_mode="false" 时，保存提醒不把它当开启（行为判据）',
          api._settings_save_warnings() == [], str(api._settings_save_warnings())[:160])
    check("web_api 不再自带一套 parse_bool 实现", "def parse_bool" not in code)

    kb = src("core/knowledge_base.py")
    check("知识库开关载入走 strict_bool（三行）",
          kb.count('strict_bool(data.get(') == 3, str(kb.count('strict_bool(data.get(')))
    wf = src("core/workflow.py")
    check("工作流 enabled 走 strict_bool",
          'strict_bool(self._cfg("agent_workflow_enabled", True), True)' in wf)
    main_src = src("main.py")
    check("_cfg_bool 转调 strict_bool（不再自己 try/parse_bool）",
          "return strict_bool(self._cfg(key, default), default, key=key, logger=self.logger)"
          in main_src)


# ============================================================ [5] P5 输入
def part_p5_input() -> None:
    print("================ [5] 预设接口：非字符串结构化拒绝（P5） ================")
    f = wa.McControlWebApi._str_field
    v, e = f({"id": 5}, "id", required=True)
    check("数字 → 报错并说明类型", v == "" and "int" in e, e)
    v, e = f({"name": {"a": 1}}, "name")
    check("对象 → 报错并说明类型", v == "" and "dict" in e, e)
    v, e = f({"id": "  abc  "}, "id", required=True)
    check("字符串 → 去空白后返回", (v, e) == ("abc", ""), repr((v, e)))
    v, e = f({"id": "   "}, "id", required=True)
    check("required 且全是空白 → 报错", v == "" and "必填" in e, e)
    v, e = f({}, "id", required=True)
    check("required 且缺省 → 报错", v == "" and "必填" in e, e)
    v, e = f({}, "id")
    check("非 required 且缺省 → 空串且不报错", (v, e) == ("", ""))

    code = src("core/web_api.py")
    check("五个预设接口都用了 _str_field（switch/create/rename/delete/bind）",
          code.count("self._str_field(data,") >= 6, str(code.count("self._str_field(data,")))
    check("不再有 man.get(str(copy_from)) 这种静默转换",
          "man.get(str(copy_from))" not in code)


# ============================================================ [6] 自审 S1
def part_p6_write_bool() -> None:
    print("================ [6] 写接口布尔：非法输入拒收 + 两条读路径 key 一致（自审 S1） ================")

    code = src("core/web_api.py")
    check("★写接口的 remote_rcon_mode 走 parse_bool（不是静默回落的 strict_bool）",
          'remote_mode = parse_bool(parsed["remote_rcon_mode"])' in code)
    check("★非法布尔会明确拒收（ok:False，并说清只认哪些词表），不是静默当成 False",
          '「异地 RCON 模式」不是合法布尔值' in code)
    check("同一配置项的两条读取路径都带 key=remote_rcon_mode（WARNING 里说得清是谁）",
          code.count('key="remote_rcon_mode"') == 2,
          str(code.count('key="remote_rcon_mode"')))


# ============================================================ [7] 自审 S2
def part_p7_root_hygiene() -> None:
    print("================ [7] 仓库卫生：根目录取证日志绝不入库（自审 S2） ================")

    gi = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8")
    for rule in ("/verify_*.txt", "/verify_*.log", "/_recheck_*.log", "/_mut*.log"):
        check(f".gitignore 覆盖根目录临时日志 {rule}", rule in gi)

    prefixes = ("verify_", "_recheck_", "_mut")
    suffixes = (".log", ".txt", ".json")
    leftovers = sorted(
        q.name for q in PLUGIN_DIR.iterdir()
        if q.is_file() and q.name.startswith(prefixes) and q.name.endswith(suffixes)
    )
    check("★仓库根目录不留本机取证日志（含 C:\\Users\\<账户> 这类绝对路径，绝不入库）",
          not leftovers, str(leftovers))

    # ---- S3：卫生检查自己报的数字必须是真的（v0.24.8 自审抓到的判据 bug）----
    # `git archive` 出的 tar 里，**目录条目不以 "/" 结尾**（是不是目录看 typeflag）。
    # 原来 `files = [n for n in names if not n.endswith("/")]` 把 11 个目录当成文件，
    # 于是「实测共 82 个文件」是假的（真值 71）。判据里的数字假了，基于名单的断言就不可信。
    import contextlib
    import io as _io
    import re as _re
    import tarfile as _tarfile

    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    import run_release_verify as rrv                            # noqa: PLC0415

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        problems = rrv.check_export_ignore()
    out = buf.getvalue()

    raw = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                         cwd=str(PLUGIN_DIR), capture_output=True).stdout
    tf = _tarfile.open(fileobj=_io.BytesIO(raw), encoding="utf-8", errors="replace")
    real = sum(1 for m in tf.getmembers() if m.isfile())
    ndir = sum(1 for m in tf.getmembers() if not m.isfile())
    m = _re.search(r"共 (\d+) 个文件", out)
    check("★发布包卫生报的「文件数」= 归档里真正的文件数（目录不算）",
          bool(m) and int(m.group(1)) == real,
          f"报 {m.group(1) if m else '?'}，实为 {real}（另 {ndir} 个目录条目）")
    check("★发布包卫生自身零告警（该挡的不在、该留的都在）",
          problems == [], str(problems)[:160])


def main() -> None:
    part_p1_executor()
    part_p2_stop()
    part_p3_broadcast()
    part_p4_bool()
    part_p5_input()
    part_p6_write_bool()
    part_p7_root_hygiene()
    print("\n================ 汇总 ================")
    print(f"通过 {PASSED[0]} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for d in FAILED:
            print(f"  - {d}")
        raise SystemExit(1)
    print("[ok] 全部通过")


if __name__ == "__main__":
    main()
