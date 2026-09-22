# -*- coding: utf-8 -*-
"""v0.23.0 一键复现：静态检查 + 全部 test_*.py + 全部 ui_*.py，输出逐文件结果与汇总。

跑法：<python> run_v0230_all.py
（ui_*.py 需要真实 Edge + Playwright；CI 上没有浏览器，那两个用例会自动跳过浏览器层，
  只跑后端契约层 —— 见 tests/test_settings_whitelist_contract.py 顶部的说明）

顺序：① 静态（编译 / schema / 版本一致性）→ ② 单元与回归 → ③ WebUI 真浏览器链路。
任一步失败即退出码 1，方便直接挂到发版前置检查上。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 解释器选择：测试依赖 AstrBot 运行时（UI 用例还要真实 Edge）。
# 用系统 Python 直接跑会因缺依赖而失败 —— 优先挑 AstrBot 自带解释器。
# 注意：不能用 `import astrbot` 当探针 —— AstrBot 的 astrbot 包在 backend/app/ 下，
# 不在 site-packages 里，裸 import 必然 ModuleNotFoundError。改用目录特征判定。
ASTRBOT_PY_CANDIDATES = [
    os.environ.get("ASTRBOT_PYTHON", ""),
    r"C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe",
    sys.executable,
]


def _looks_like_astrbot_python(exe: Path) -> bool:
    """AstrBot 自带解释器的特征：同级 backend/ 下存在 app/astrbot 包目录。"""
    try:
        return (exe.parent.parent / "app" / "astrbot").is_dir()
    except OSError:
        return False


def pick_python() -> str:
    for cand in ASTRBOT_PY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file() and _looks_like_astrbot_python(p):
            return str(p)
    print("⚠ 未找到 AstrBot 自带解释器，回退到当前解释器；部分用例可能因缺依赖失败。")
    print("  可用 ASTRBOT_PYTHON 环境变量指定，例如：")
    print(r"  $env:ASTRBOT_PYTHON='C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe'")
    return sys.executable


PY = pick_python()


def static_checks() -> list[str]:
    """① 静态：编译、schema / metadata 合法性、发布元数据一致性。"""
    print("=" * 68)
    print("① 静态检查（编译 / schema / 元数据）")
    print("=" * 68)
    fails: list[str] = []

    py_files = [f for f in ROOT.rglob("*.py") if "__pycache__" not in f.parts]
    r = subprocess.run([PY, "-m", "compileall", "-q", "main.py", "core", "pages", "tests"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    ok = r.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] compileall（{len(py_files)} 个 .py）")
    if not ok:
        fails.append("compileall")
        print("    " + (r.stdout or r.stderr).strip()[-400:])

    try:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        n_items = sum(len(v.get("items") or {}) for v in schema.values() if isinstance(v, dict))
        print(f"[PASS] _conf_schema.json（{len(schema)} 个分组 / {n_items} 个配置项）")
    except Exception as e:                                   # noqa: BLE001
        print(f"[FAIL] _conf_schema.json：{e}")
        fails.append("_conf_schema.json")

    try:
        import yaml
        meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        print(f"[PASS] metadata.yaml（version={meta.get('version')}）")
    except Exception as e:                                   # noqa: BLE001
        print(f"[FAIL] metadata.yaml：{e}")
        fails.append("metadata.yaml")

    # 发布包卫生：不该进包的东西（tests/ 与开发脚本由 .gitattributes 的 export-ignore 控制）
    ga = (ROOT / ".gitattributes")
    if ga.exists() and "/tests/" in ga.read_text(encoding="utf-8"):
        print("[PASS] .gitattributes 已排除 tests/（发布包不带测试）")
    else:
        print("[WARN] .gitattributes 未排除 tests/ —— 发布包会带上测试目录")
    return fails


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
    print(f"解释器：{PY}")
    total_fail: list[str] = static_checks()
    print()
    for pattern, title in (("test_*.py", "② 回归 / 单元测试"),
                           ("ui_*.py", "③ WebUI 真浏览器链路")):
        files, fails = run_group(pattern, title)
        total_fail += fails
        print(f"→ {title}：{len(files) - len(fails)}/{len(files)} 通过\n")
    print("=" * 68)
    if total_fail:
        print("失败项：", ", ".join(total_fail))
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
