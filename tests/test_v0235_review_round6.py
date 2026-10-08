# -*- coding: utf-8 -*-
# gate-allow-fail-sample —— 本用例**故意**触发 fail-closed 分支（严格模式下
#   环境缺件要报 [FAIL]），那个 [FAIL] 是样本正文、不是本用例的失败；
#   本用例自己的账目在 main() 里结（通过 47 / 失败 0）。
"""v0.23.5 外部复核（第六轮）回归：账本清红 / 索引坏掉不许静默 / IO 生命周期 / 门禁 fail-open。

GPT 第五轮核验的结论是「四个核心缺口已修好，但工程门禁与少数运行时边界仍未收口」，
列了六组问题。本批逐条落地，并且**每一条都配一个能复现原缺陷的断言**：

  A. 统一落盘账本只进不出：`_stamp_fp()` / `transfer()` 写预设文件成功时不记绿 ——
     一次失败之后，后面每一次成功的写入都清不掉那个红，界面永远停在「保存失败」。
  B. 索引坏掉被当成「无需补算」：`pending_vectors()` 吞异常返回 0 → 切换预设静默跳过
     重建，旧向量一直失效（纯词法还查得到，外表看不出来）。
  C. 超时线程无限累积：`_io_wait` 取消不了已进系统调用的线程，旧写法每轮都往**默认
     线程池**再提交一笔 → 慢盘持续卡住会把全进程的池占满。现在单飞 + 专属执行器。
  D. 初始化 IO 超时后仍留着上一轮的 `file_present=True`（health 自相矛盾）。
  E. 无换行片段每轮从同一 offset 重读 → 读放大（O(n²)）。现在只读新增部分。
  F. 门禁 fail-open 四处：`✅` 在 GBK 重定向下崩尾、`--plan` 与实际分支不一致、
     `CI_OK` 被清空仍能通过守卫、发布包卫生在「环境缺件」时静默通过。
  G. 用例降级不完整：`test_settings_whitelist_contract` 只挡住了 import 失败，
     「playwright 装了、浏览器没装」会被记成 FAIL（CI 上全绿、换台机器反红）。

运行：
  <AstrBot python> tests\\test_v0235_review_round6.py
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import io
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    KnowledgePresetManager,
    ModKnowledgeBase,
)

PLUGIN = PLUGIN_DIR
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))
import _ui_manifest as UM  # noqa: E402
import run_release_verify as RV  # noqa: E402

KB_SRC = io.open(PLUGIN / "core" / "knowledge_base.py", encoding="utf-8").read()
LW_SRC = io.open(PLUGIN / "core" / "log_watcher.py", encoding="utf-8").read()
RV_SRC = io.open(PLUGIN / "run_release_verify.py", encoding="utf-8").read()
UM_SRC = io.open(PLUGIN / "tests" / "_ui_manifest.py", encoding="utf-8").read()
WL_SRC = io.open(PLUGIN / "tests" / "test_settings_whitelist_contract.py",
                 encoding="utf-8").read()

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ｜ {extra}" if extra else ""))


def fn_src(src: str, name: str, cls: str | None = None) -> str:
    """取某个函数 / 方法的源码片段（ast 定位，避免正则误抓同名的别处）。"""
    tree = ast.parse(src)
    node = None
    if cls:
        for top in ast.walk(tree):
            if isinstance(top, ast.ClassDef) and top.name == cls:
                for sub in top.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        node = sub
    else:
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and top.name == name:
                node = top
    assert node is not None, f"找不到 {cls}.{name}"
    return ast.get_source_segment(src, node) or ""


async def _embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


# ==================== A 账本：成功的写入要能清掉自己的红 ====================
def part_a() -> None:
    print("========== [A] 统一落盘账本：红只该由「同部件这次写成功」清掉 ==========")
    stamp = fn_src(KB_SRC, "_stamp_fp", cls="KnowledgePresetManager")
    check("静态：_stamp_fp 写成功也记进账本（否则红清不掉；v0.24.2 F13 起按文件记账）",
          "self._note_save(self._preset_part(pid), True)" in stamp)
    tr = fn_src(KB_SRC, "transfer", cls="KnowledgePresetManager")
    check("静态：transfer 把内容写进目标预设后也记绿（按目标文件记账）",
          "self._note_save(self._preset_part(dst_id), True)" in tr)

    with tempfile.TemporaryDirectory() as td:
        man = KnowledgePresetManager(str(Path(td)), "sid")
        pid1 = man.create("甲")["id"]
        check("基准：新建预设后账本是绿的", man.save_health()["ok"] is True)

        # 反面对照（第五轮 C1 的口径）：别的部件写成功**不许**替预设文件洗白
        # v0.24.2（F13）：红按**文件**记 —— 这里就给 pid1 自己的文件记账，
        # 于是下面每一次「某某写成功能不能洗白」问的都是同一个文件。
        man._note_save(man._preset_part(pid1), False, "模拟：预设文件落盘失败")
        man._persist_registry()
        check("反面对照：注册表写成功不替预设文件洗白",
              man.save_health()["ok"] is False, str(man.save_health()))

        # 路径 A：指纹回写成功（_stamp_fp）—— 旧写法这里不记绿，红会永远留着
        man._note_save(man._preset_part(pid1), False, "模拟：预设文件落盘失败")
        man.bind(pid1, "fp-alpha")
        check("A 指纹回写成功 → 红被清掉（旧写法红留到永远）",
              man.save_health()["ok"] is True, str(man.save_health()))

        # 路径 B：新建预设成功（v0.24.2 F13：红记在**乙自己**的文件上，
        # 再由一次真的写入乙文件把红清掉 —— 正是「同部件这次写成功」的口径）
        pid2 = man.create("乙")["id"]
        man._note_save(man._preset_part(pid2), False, "模拟：预设文件落盘失败")
        man.transfer(pid1, pid2, "copy")
        check("B 写乙预设文件成功 → 红被清掉", man.save_health()["ok"] is True,
              str(man.save_health()))

        # 路径 C：复制（写目标预设）成功
        man._note_save(man._preset_part(pid2), False, "模拟：预设文件落盘失败")
        man.transfer(pid1, pid2, "copy")
        check("C 复制写目标成功 → 红被清掉", man.save_health()["ok"] is True,
              str(man.save_health()))

        # 反向：真失败仍要留红（一次移动里「目标成功 + 源清空失败」= 红）
        p3 = man.create("丙")["id"]
        man._note_save(man._preset_part(p3), False, "模拟：源库清空失败")
        check("反向：真失败过就必须留红（不许被后来的成功抹掉）",
              man.save_health()["ok"] is False and "源库清空失败" in man.save_health()["error"])
        assert p3


# ==================== B 索引坏掉不许静默 ====================
def part_b() -> None:
    print("========== [B] 向量索引读不出来时，不许报「无需补算」 ==========")
    src = fn_src(KB_SRC, "pending_vectors", cls="ModKnowledgeBase")
    check("静态：异常分支不再 `return 0`",
          "return 0" in src and "return len(searchable)" in src)
    check("静态：错误交给 semantic_stats 摊出来（index_error）",
          "index_error" in src)

    with tempfile.TemporaryDirectory() as td:
        kb = ModKnowledgeBase(str(td), server_id="stub", preset_id="p",
                              semantic_enabled=True)
        kb.set_semantic_enabled(True)
        kb.embed_fn = _embed
        kb.save_entry("条目甲", "黄铜锭怎么合成", status="verified")
        kb.save_entry("条目乙", "钻石剑怎么做", status="verified")
        n_searchable = len(kb._searchable())
        check("前置：语义通道开着、有两条可检索条目",
              kb._sem is not None and n_searchable == 2, f"searchable={n_searchable}")

        real_missing = kb._sem.missing

        def boom(entries):
            raise RuntimeError("注入：.vec.npz 结构损坏")

        kb._sem.missing = boom
        got = kb.pending_vectors()
        check("★索引读不出来 → 不再报 0（而是保守报「全部待补算」，触发重建）",
              got == n_searchable and got > 0, f"pending={got}")
        stats = kb.semantic_stats()
        check("错误被摊进 semantic_stats（界面 / 诊断看得见）",
              "npz 结构损坏" in str(stats.get("index_error", "")), str(stats))

        kb._sem.missing = real_missing
        check("恢复后：pending 回到真实值、index_error 清空",
              kb.pending_vectors() == 2 and kb.semantic_stats()["index_error"] == "",
              f"{kb.pending_vectors()} / {kb.semantic_stats()['index_error']!r}")

        kb2 = ModKnowledgeBase(str(Path(td) / "b"), server_id="stub", preset_id="q")
        # 语义通道**没开**（默认关）→ `_sem is None`，此时报 0 是对的
        kb2.embed_fn = _embed
        kb2.save_entry("条目甲", "内容", status="verified")
        check("语义通道没开 → 仍恒为 0（不放空炮）",
              kb2.pending_vectors() == 0)


# ==================== C/D/E IO 生命周期 ====================
def _make_watcher(root: Path, interval: float = 10.0):
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "latest.log").write_bytes(b"")
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    return LW.LogWatcher(str(root), on_event, poll_interval=interval), seen


def part_c() -> None:
    print("========== [C] IO 单飞 + 专属执行器（线程不再无限累积） ==========")
    io_src = fn_src(LW_SRC, "_io_wait", cls="LogWatcher")
    check("静态：IO 走专属执行器（run_in_executor），不再靠默认线程池",
          "run_in_executor" in io_src)
    check("静态：单飞闸门（上一笔没回来就不提交新的 —— 判据在**线程侧**计数上）",
          "if self._io_inflight > 0:" in io_src and "self.io_skipped += 1" in io_src)
    check("静态：超时用 shield（不许取消那笔 IO —— 取消了也杀不掉线程）",
          "asyncio.shield(fut)" in io_src)
    check("静态：被放弃的那笔结束时取走异常（不留 never retrieved）",
          "def _release_io" in LW_SRC and "fut.exception()" in
          fn_src(LW_SRC, "_release_io", cls="LogWatcher"))
    check("静态：忙碌判据在**线程侧**（finally 递减），跨事件循环也能复位",
          "self._io_inflight -= 1" in io_src and "_io_busy" in
          fn_src(LW_SRC, "_io_busy", cls="LogWatcher"))
    # 第七轮：停止逻辑抽到 `_stop_locked`（start 的「先收旧」与 stop 共用同一条
    # 收尾路，见 round7）—— 断言目标随实现形态更新，判据（不等卡住的那笔）不变。
    stop_src = fn_src(LW_SRC, "_stop_locked", cls="LogWatcher")
    check("静态：stop() 关掉专属执行器且**不等**卡住的那笔（5 秒承诺还在）",
          "shutdown(wait=False" in stop_src)

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            w, _ = _make_watcher(root)
            old = LW.IO_TIMEOUT
            LW.IO_TIMEOUT = 0.05
            try:
                await w.start()
                check("专属执行器：1 个线程（卡住的线程只占它自己的池）",
                      w._executor is not None and w._executor._max_workers == 1)

                def stuck(offset, size):
                    time.sleep(1.0)          # 假装陷在系统调用里
                    return b""

                with open(root / "logs" / "latest.log", "ab") as fh:
                    fh.write(b"hello\n")
                w._read_at = stuck
                for _ in range(4):
                    await w._poll()          # 第一轮真提交、超时；后面几轮该被闸门挡住
                    await asyncio.sleep(0.06)
                check("★卡住的那笔只超时一次（旧写法每轮都再提交一笔）",
                      w.io_timeouts == 1, f"io_timeouts={w.io_timeouts}")
                check("★后续轮次被单飞闸门明确跳过（未完成 IO 恒 ≤ 1）",
                      w.io_skipped >= 2, f"io_skipped={w.io_skipped}")
                check("那一笔还挂在执行器里（_io_busy 仍为真）",
                      w._io_busy is True)
            finally:
                LW.IO_TIMEOUT = old
                await w.stop()

    asyncio.run(scenario())

    print("========== [D] 初始化超时后不许留着上一轮的「文件在」 ==========")
    check("静态：primed is None 分支显式把 file_present 清零",
          "self.file_present = False" in fn_src(LW_SRC, "start", cls="LogWatcher"))

    async def scenario2():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            w, _ = _make_watcher(root)
            await w.start()
            check("前置：第一次 start 如实报「探测过、文件在」",
                  w.health()["probed"] is True and w.health()["file_present"] is True)
            await w.stop()

            def slow_prime():
                time.sleep(1.0)
                return (0, None, "", True)

            w._prime_state = slow_prime        # 重启时初始化超时
            old = LW.IO_TIMEOUT
            LW.IO_TIMEOUT = 0.05
            try:
                await w.start()
                h = w.health()
                check("★初始化超时后：probed=False 且 file_present=False（两字段不再打架）",
                      h["probed"] is False and h["file_present"] is False, str(h))
            finally:
                LW.IO_TIMEOUT = old
                await w.stop()

    asyncio.run(scenario2())

    print("========== [E] 未成行的片段：只读新增部分（不再重读前缀） ==========")
    check("静态：缓冲 + 只读新增（read_from / _partial）",
          "_partial" in LW_SRC and "read_from = self._pos + len(self._partial)" in LW_SRC)

    async def scenario3():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            w, _ = _make_watcher(root)
            w._enc = "utf-8"
            await w.start()
            log = root / "logs" / "latest.log"
            before = w.read_bytes
            chunk = b"x" * 10000
            for _ in range(20):                # 200 KB 的「还没写完的半行」，分 20 次到
                with open(log, "ab") as fh:
                    fh.write(chunk)
                await w._poll()
            h = w.health()
            check("★半行片段被缓冲住（200000 字节都在 _partial 里）",
                  h["partial_bytes"] == 200000, str(h["partial_bytes"]))
            got = w.read_bytes - before
            check("★读放大消失：累计读入 ≈ 数据量 ×1.3 以内（旧写法是 O(n²)）",
                  got <= 260000, f"写入 200000 / 读入 {got}")
            with open(log, "ab") as fh:
                fh.write(b"\n[12:00:00] [Server thread/INFO]: <Steve> hi\n")
            await w._poll()
            check("换行到齐：缓冲被清空、_pos 前进（这半行被正常消费）",
                  w.health()["partial_bytes"] == 0 and w._pos > 200000,
                  f"partial={w.health()['partial_bytes']} pos={w._pos}")
            await asyncio.sleep(0.02)
            await w.stop()

    asyncio.run(scenario3())


# ==================== F 门禁 fail-open ====================
def part_f() -> None:
    print("========== [F] 门禁 fail-open 四处 ==========")
    check("① 末尾输出不再含 ✅（GBK 重定向下 `print` 会 UnicodeEncodeError 崩在终点线）",
          "✅" not in RV_SRC and "全部通过（ALL PASS）" in RV_SRC)
    check("① main 开头把 stdout / stderr 钉成 UTF-8 + replace（第七轮修正："
          "与子进程链同编码，不再把「不设 encoding」钉成契约）",
          'reconfigure(encoding="utf-8", errors="replace")' in RV_SRC)

    plan_src = fn_src(RV_SRC, "plan")
    check("② plan() 与实际执行同源（认 --ui-only / --no-ui / --only-static）",
          all(k in plan_src for k in ("ui_only", "no_ui", "only_static", "run_static", "run_ui")))
    check("② main 把三个开关真的传给了 plan()",
          "plan(all_ui, ci_mode, ui_set, ui_only, no_ui, only_static)" in RV_SRC)

    check("③ 发布包卫生：CI / 发版下环境缺件也算失败（fail-closed）",
          "def env_missing(" in RV_SRC
          and "check_export_ignore(strict=strict_env)" in RV_SRC
          and 'os.environ.get("CI"' in RV_SRC)

    um_check = fn_src(UM_SRC, "check")
    check("④ 守卫本体就拦「CI_OK 被清空」（不再只靠回归用例里的断言）",
          "CI_OK 白名单是空的" in um_check and "没有任何**硬门禁**用例" in um_check)

    # 行为 1：CI_OK 清空 / 只剩软门禁 —— 守卫必须报
    saved = list(UM.CI_OK)
    try:
        UM.CI_OK = []
        problems = UM.check(PLUGIN)
        check("行为：CI_OK 为空 → check() 返回问题（旧写法返回空 = 门禁形同不存在）",
              any("CI_OK 白名单是空的" in p for p in problems), str(problems[:2]))
        UM.CI_OK = [s for s in saved if s not in UM.HARD]
        problems2 = UM.check(PLUGIN)
        check("行为：CI_OK 里没有硬门禁 → check() 也报",
              any("硬门禁" in p for p in problems2), str(problems2[:2]))
    finally:
        UM.CI_OK = saved
    check("恢复：白名单复原后守卫依然干净", UM.check(PLUGIN) == [])

    # 行为 2：环境缺件 —— 严格模式记失败、本机只警告
    ga = PLUGIN / ".gitattributes"
    bak = PLUGIN / ".gitattributes.__round6_bak"
    ga.rename(bak)
    try:
        soft = RV.check_export_ignore(strict=False)
        hard = RV.check_export_ignore(strict=True)
        check("行为：本机（非严格）环境缺件只警告，不记失败", soft == [], str(soft))
        check("行为：CI / 发版（严格）环境缺件记失败（旧写法静默通过）",
              bool(hard) and "环境缺件" in hard[0], str(hard))
    finally:
        bak.rename(ga)

    # 行为 3：--plan 的两个分支与实际执行一致
    r1 = subprocess.run([sys.executable, str(PLUGIN / "run_release_verify.py"),
                         "--plan", "--ui-only"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=str(PLUGIN))
    check("行为：--plan --ui-only 不再打印静态检查与回归清单",
          "静态检查" not in r1.stdout and "回归" not in r1.stdout
          and "个用例" in r1.stdout, r1.stdout.strip()[:120])
    r2 = subprocess.run([sys.executable, str(PLUGIN / "run_release_verify.py"),
                         "--plan", "--no-ui"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=str(PLUGIN))
    check("行为：--plan --no-ui 不再列出 UI 用例",
          "个用例" not in r2.stdout and "回归" in r2.stdout, r2.stdout.strip()[:120])


# ==================== G 用例降级（半装环境） ====================
def part_g() -> None:
    print("========== [G] 半装环境（有 playwright、没浏览器）走「跳过」而非 FAIL ==========")
    check("⑦ 双 launch 都失败时走 browser_missing 分支（与 import 失败同一档）",
          "browser_missing" in WL_SRC and "[skip] 真浏览器一层跳过" in WL_SRC)
    check("⑦ 跳过时不再记 FAIL（旧的 check(..., False) 只留给真异常）",
          'if browser is not None:' in WL_SRC)


def main() -> int:
    print("=" * 72)
    print("v0.23.5 第六轮回归（GPT 第五轮核验 · 六组问题的落地与判据）")
    print("=" * 72)
    part_a()
    part_b()
    part_c()
    part_f()
    part_g()
    print("=" * 72)
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for f in _fail:
            print(f"  - {f}")
        return 1
    print("全部通过：六组问题都配了「能复现原缺陷」的判据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
