# -*- coding: utf-8 -*-
"""v0.24.3 回归 · N11：门禁用例**不许把「跑不动」转成「跳过」**。

背景（GPT v0.24.2 复核 N11）
============================

跳过在门禁里等于绿（rc=0）。于是「导入失败 return 0」「替身撞 AttributeError 就 skip」
这类写法，会把**代码错误**静默转成通过 —— GPT 实测：把 `core/workflow.py` 的异常分支改回
旧的 `status="failed"`（F01 的病灶）之后，用例仍走出 **20 PASS / 1 SKIP，rc=0**。

本用例守三件事
==============

1. **静态**：仓库里那三条「导入失败即跳过」的用例不许再有跳过出口
   （`skip(` 调用 / `_skip.append` / `[SKIP]` 打印），且必须看得见 fail-closed 的 FAIL 分支。
2. **活体**：把每条用例的运行时标志（`IMPORT_OK` / `WORKFLOW_OK`）置 False，再调它**自己的
   入口** —— 必须返回非 0。这条是真在跑被测对象，不是扫文本。
3. **替身**：`test_v0242_execution_facts.py` 的替身缺成员时（等价于被测代码被改名/删属性），
   必须记 FAIL 而不是跳过；同时保留一条**阳性对照**，证明这套牙不是「一律判红」。

fail-closed 声明
================

本用例自身导入失败判 **FAIL**，不判 SKIP。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
PASSN = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASSN
    if ok:
        PASSN += 1
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def load(name: str, filename: str):
    """按路径把被测用例当模块加载（模块名带前缀，避免和门禁自己撞名）。"""
    spec = importlib.util.spec_from_file_location(f"_n11_{name}", TESTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


add_sys_paths()

#: 三条 N11 同源病灶文件：文件 → 负责「导入失败」的那个标志
CASES = {
    "test_v0242_execution_facts.py": "IMPORT_OK",
    "test_simple_workflow_result.py": "WORKFLOW_OK",
    "test_complex_routing.py": "WORKFLOW_OK",
}


#: 文件 → （旧跳过写法的**原文**，修好之后必须看得见的 fail-closed 证据）
#: 判据写成原文而不是关键词：关键词会撞名（`_unknown_and_skip` 里就有 `_skip`，
#: `check()` 的打印是 `f"[{'PASS' if ok else 'FAIL'}]"` 而不是裸 `[FAIL]`）——
#: 判据粗了会把自己人判红 —— 这条是写第一版时实测踩过的坑。
BANNED_AND_MUST = {
    "test_v0242_execution_facts.py": ("[SKIP]", "本文件零跳过"),
    "test_simple_workflow_result.py": ("不判失败", "本文件零跳过"),
    "test_complex_routing.py": ("端到端组已跳过", "记 FAIL"),
}


def group_static() -> None:
    print("---- 一、静态：不许有跳过出口，且必须有 fail-closed 的 FAIL 分支 ----")
    for fn, (banned, must) in BANNED_AND_MUST.items():
        text = (TESTS / fn).read_text(encoding="utf-8")
        check(f"{fn}：没有 skip( 调用", not any(
            line.strip().startswith("skip(") for line in text.splitlines()))
        check(f"{fn}：没有 _skip 记账", "_skip.append(" not in text and "_skip:" not in text)
        check(f"{fn}：旧跳过写法「{banned}」已清零", banned not in text)
        check(f"{fn}：看得见 fail-closed 证据「{must}」", must in text)


def group_live() -> None:
    print("\n---- 二、活体：标志置 False 后入口必须返非 0（旧行为返 0）----")
    # 子用例自己会刷一屏日志，这里静音（门禁日志留给它自己单独跑的时候看）。
    # ① 执行事实用例（async 入口）
    m = load("ef", "test_v0242_execution_facts.py")
    m.IMPORT_OK = False
    m.IMPORT_ERR = "N11 活体：模拟 core 模块改名"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = asyncio.run(m.main_async())
    check("test_v0242_execution_facts：IMPORT_OK=False → rc != 0", rc != 0, f"rc={rc}")
    check("  └ 且记账里没有「跳过」", not getattr(m, "SKIP", []) and bool(m.FAIL),
          f"FAIL={m.FAIL} SKIP={getattr(m, 'SKIP', None)}")

    # ② 简单/复杂路径用例（async 入口）
    m2 = load("swr", "test_simple_workflow_result.py")
    m2.WORKFLOW_OK = False
    m2.IMPORT_ERR = "N11 活体：模拟 core.workflow 导入失败"
    with contextlib.redirect_stdout(io.StringIO()):
        rc2 = asyncio.run(m2.main_async())
    check("test_simple_workflow_result：WORKFLOW_OK=False → rc != 0", rc2 != 0, f"rc={rc2}")
    check("  └ 零跳过自检仍是空的", not m2.SKIP, str(m2.SKIP))

    # ③ 转轨用例（同步入口）
    m3 = load("cr", "test_complex_routing.py")
    m3.WORKFLOW_OK = False
    m3.IMPORT_ERR = "N11 活体：模拟 core.workflow 导入失败"
    with contextlib.redirect_stdout(io.StringIO()):
        rc3 = m3.main()
    check("test_complex_routing：WORKFLOW_OK=False → rc != 0（端到端组不再只跳过）",
          rc3 != 0, f"rc={rc3}")


def group_stub() -> None:
    print("\n---- 三、替身：缺成员必须记 FAIL；阳性对照证明不是一律判红 ----")
    m = load("ef2", "test_v0242_execution_facts.py")
    if not m.IMPORT_OK:
        check("替身牙需要 AstrBot 运行时", False, m.IMPORT_ERR)
        return

    healthy_make = m.make_workflow

    def boom(_rcon):
        raise AttributeError("N11 替身：模拟被测代码改名（_unknown_and_skip 之类）")

    m.make_workflow = boom
    n_fail0 = len(m.FAIL)
    with contextlib.redirect_stdout(io.StringIO()):   # 这条是故意造的红，别混进本用例日志
        reports, rcon = asyncio.run(m.must_run("模拟替身缺成员", m.CMDS, "Steve", ["Steve"]))
    check("★替身缺成员：must_run 返回 (None, None) 而不是抛出", reports is None and rcon is None)
    check("★替身缺成员：记成 FAIL（不是 SKIP）", len(m.FAIL) == n_fail0 + 1
          and "不许算跳过" in m.FAIL[-1] and not m.SKIP, f"FAIL={m.FAIL[-2:]} SKIP={m.SKIP}")

    m.make_workflow = healthy_make
    with contextlib.redirect_stdout(io.StringIO()):
        reports2, rcon2 = asyncio.run(m.must_run("阳性对照", m.CMDS, "Steve", ["Steve"]))
    check("★阳性对照：替身正常时照样拿到报告、不追加 FAIL",
          reports2 is not None and rcon2 is not None and len(m.FAIL) == n_fail0 + 1,
          f"reports={reports2} FAIL={len(m.FAIL)}")


def main() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · N11：门禁用例不许把「跑不动」转成「跳过」")
    print("=" * 78)
    group_static()
    group_live()
    group_stub()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSN, len(FAIL)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
