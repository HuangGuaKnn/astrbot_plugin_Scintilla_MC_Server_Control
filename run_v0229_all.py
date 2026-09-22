# -*- coding: utf-8 -*-
"""v0.22.9 一键复现：跑全部 test_*.py 与 ui_*.py，输出逐文件结果与汇总。

跑法：<python> run_v0229_all.py
（ui_*.py 需要真实 Edge + Playwright，本机已具备）
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def run_group(pattern: str, title: str):
    files = sorted((ROOT / "tests").glob(pattern))
    print("=" * 68)
    print(f"{title}（{len(files)} 个文件）")
    print("=" * 68)
    fails = []
    for f in files:
        t0 = time.time()
        r = subprocess.run([PY, str(f)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=str(ROOT))
        cost = time.time() - t0
        ok = r.returncode == 0
        print(f"[{'PASS' if ok else 'FAIL'}] {f.name}  ({cost:.1f}s)")
        if not ok:
            fails.append(f.name)
            for line in (r.stdout or "").strip().splitlines()[-12:]:
                print("    " + line)
    return files, fails


def main() -> None:
    total_fail: list[str] = []
    for pattern, title in (("test_*.py", "回归 / 单元测试"),
                           ("ui_*.py", "WebUI 真浏览器链路")):
        files, fails = run_group(pattern, title)
        total_fail += fails
        print(f"→ {title}：{len(files) - len(fails)}/{len(files)} 通过\n")
    print("=" * 68)
    if total_fail:
        print("失败文件：", ", ".join(total_fail))
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
