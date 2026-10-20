# -*- coding: utf-8 -*-
"""v0.24.8 回归（第十轮复核 P2-4）：直接手跑用例时的控制台编码 —— 覆盖到**每一个** test_*.py。

为什么要有这一支
================
干净 shell（没有 `PYTHONUTF8` / `PYTHONIOENCODING`）里，Windows 的 stdout 是 **GBK**。
用例末尾只要打一个非 GBK 字符（比如 `✅`），`print` 就抛 `UnicodeEncodeError`：

    通过 34 项，失败 0 项
    全部通过 ✅ …
    UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
    → 退出码 1

**全部通过、退出码却是 1** —— 比真失败更难查：看着像坏了，其实是输出。

第九轮给 `tests/_paths.py` 加了 import 时兜底（扛不住就换 UTF-8 + `errors="replace"`），
但那只帮到**导入它**的用例。第十轮复核点出：仓库里仍有 6 个 `test_*.py` 不导入它
（复核说 7 个；皮莉卡实测 6 个，见下方清单）。这一支把覆盖补齐，并把它钉住：

1. `tests/` 下每个 `test_*.py` 都要能拿到兜底（导入 `_paths`，或显式调 `ensure_utf8_console`）；
2. **行为判据**：干净 GBK shell 里 `import _paths` 后打印 `✅` 必须 rc=0；
3. **阳性对照**：同一环境里**不导入** `_paths` 直接打印 `✅` 必须 rc=1
   —— 证明第 2 条抓的是「兜底本身」，不是「这台机器刚好是 UTF-8」；
   本机默认就是 UTF-8 时这一条**不适用**（不判红、也不假装通过）。

运行：
    <AstrBot python> tests\\test_v0248_direct_run_console.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


def _clean_env() -> dict:
    """干净 shell：去掉 UTF-8 相关的两个环境变量（本机 AstrBot 进程里带着 `PYTHONUTF8=1`，
    子进程会继承 —— 不去掉就复现不出来，第九轮探针第一版就是这么自己骗自己的）。"""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(PLUGIN_DIR),
                          capture_output=True, env=_clean_env())


# ============================================================ 一、覆盖清单
def part_one() -> None:
    print("---- 一、每个 test_*.py 都要能拿到编码兜底 ----")
    files = sorted(p.name for p in TESTS.glob("test_*.py"))
    missing = []
    for name in files:
        text = (TESTS / name).read_text(encoding="utf-8", errors="replace")
        if "_paths" not in text and "ensure_utf8_console" not in text:
            missing.append(name)
    check(f"★{len(files)} 个 test_*.py 全部接上兜底（导入 _paths 或显式调 ensure_utf8_console）",
          not missing, "没接上的：" + str(missing))
    print(f"       清单：{len(files)} 个；未接上 {len(missing)} 个")


# ============================================================ 二、行为判据
def part_two() -> None:
    print("\n---- 二、行为：干净 GBK shell 里导入兜底后打 ✅ 必须 rc=0 ----")
    enc = _run("import sys;print(sys.stdout.encoding)").stdout.decode("utf-8", "replace").strip()
    print(f"       干净 shell 的 stdout 编码：{enc}")
    r = _run("import sys;sys.path.insert(0,'tests');import _paths;print('\\u2705 ok')")
    check("★import _paths 之后打印 ✅ → rc=0（编码问题不该让通过的结果变成失败）",
          r.returncode == 0,
          f"rc={r.returncode}；{(r.stderr or b'').decode('utf-8', 'replace')[-200:]}")
    print("\n---- 三、阳性对照：不导入兜底时，同一个环境里必须**真的**会炸 ----")
    r2 = _run("print('\\u2705 ok')")
    if r2.returncode == 0:
        # 本机干净 shell 本来就是 UTF-8（比如开了「Beta: 使用 Unicode UTF-8」）→ 这一条不适用
        print(f"       [SKIP] 本机干净 shell 的 stdout 编码是 {enc}，本来就扛得住 ✅ —— "
              "阳性对照不适用（不判红，也不假装通过）")
    else:
        check("★不导入兜底时打印 ✅ → rc=1 且报 UnicodeEncodeError（证明兜底真的在起作用）",
              r2.returncode != 0 and b"UnicodeEncodeError" in r2.stderr,
              f"rc={r2.returncode}；stderr={(r2.stderr or b'').decode('utf-8', 'replace')[-160:]}")


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 第十轮复核：直接手跑用例的控制台编码覆盖")
    print("=" * 78)
    part_one()
    part_two()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （每个 test_*.py 都能在 GBK 控制台上干净退出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
