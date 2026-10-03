# -*- coding: utf-8 -*-
"""v0.23.0 一键复现：静态检查 + 全部 test_*.py + 全部 ui_*.py，输出逐文件结果与汇总。

跑法：<python> run_v0230_all.py
（ui_*.py 需要真实 Edge + Playwright；CI 上没有浏览器，那两个用例会自动跳过浏览器层，
  只跑后端契约层 —— 见 tests/test_settings_whitelist_contract.py 顶部的说明）

顺序：① 静态（编译 / schema / 版本一致性）→ ② 单元与回归 → ③ WebUI 真浏览器链路。
任一步失败即退出码 1，方便直接挂到发版前置检查上。
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 解释器选择：测试依赖 AstrBot 运行时（UI 用例还要真实 Edge）。
# 用系统 Python 直接跑会因缺依赖而失败 —— 优先挑 AstrBot 自带解释器。
# 注意：不能用 `import astrbot` 当探针 —— AstrBot 的 astrbot 包在 backend/app/ 下，
# 不在 site-packages 里，裸 import 必然 ModuleNotFoundError。改用目录特征判定。
ASTRBOT_PY_CANDIDATES = [
    os.environ.get("ASTRBOT_PYTHON", ""),
    r"%USERPROFILE%\AppData\Local\AstrBot\backend\python\python.exe",
    sys.executable,
]


def _looks_like_astrbot_python(exe: Path) -> bool:
    """AstrBot 自带解释器的特征：同级 backend/ 下存在 app/astrbot 包目录。"""
    try:
        return (exe.parent.parent / "app" / "astrbot").is_dir()
    except OSError:
        return False


def _is_internal_doc(p: str) -> bool:
    """docs/ 下属于「内部核验 / 交接」的文件：留在仓库备查，但不该进用户包。"""
    if not p.startswith("docs/"):
        return False
    name = p[len("docs/"):]
    return (name.startswith("VERIFY_") or name.startswith("CONSULT_")
            or name.endswith(".diff") or "实施单" in name)


def check_export_ignore() -> list:
    """发布包卫生：**把发布包真的打出来看**（`git archive HEAD` → tar → 列名单）。

    为什么不用 `git check-attr` 逐条问：gitattributes 的属性**不递归** ——
    `/tests/ export-ignore` 只落在目录本身，问 `tests/xxx.py` 一律得到 unspecified
    （第四轮实测），照着写只会得到一堆假阴性。而 `git archive` 正是发版流程里生成附件的
    那一条命令：**它说包里有谁，才算数**。

    第四轮复核的意义在这条上最直白：旧检查只断言「.gitattributes 里有 /tests/ 字样」，
    而实打实解包发现 `docs/v0.22.6_实施单补充*.md` 一直被跟踪、也一直躺在发布包里。
    现在两头都看：**该挡的必须不在**（测试 / 开发脚本 / 内部核验文档），
    **该留的必须还在**（补一条 export-ignore 顺手挡掉用户文件，同样是发布事故）。
    """
    out: list = []
    if not (ROOT / ".gitattributes").exists():
        print("[WARN] 没有 .gitattributes —— 发布包会带上测试与内部文档")
        return out
    try:
        r = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                           cwd=str(ROOT), capture_output=True)   # 二进制，别加 text=True
    except Exception:                                            # noqa: BLE001
        print("[WARN] 没有 git（或不是仓库）—— 跳过发布包卫生实测")
        return out
    if r.returncode != 0:
        print("[WARN] git archive HEAD 不可用 —— 跳过发布包卫生实测")
        return out
    try:
        # 中文文件名按 UTF-8 解（别让它落到系统 locale 上）
        names = tarfile.open(fileobj=io.BytesIO(r.stdout),
                             encoding="utf-8", errors="replace").getnames()
    except Exception as e:                                       # noqa: BLE001
        print(f"[WARN] 解包发布包失败：{e}")
        return out
    files = [n for n in names if not n.endswith("/")]
    leaked = [n for n in files if n.startswith("tests/")
              or (n.startswith("run_") and n.endswith(".py")) or _is_internal_doc(n)]
    must_keep = ["main.py", "metadata.yaml", "_conf_schema.json",
                 "docs/configure.md", "docs/faq.md", "docs/usage.md", "docs/gallery.md"]
    missing = [m for m in must_keep if m not in files]
    if not any(n.startswith("docs/images/") for n in files):
        missing.append("docs/images/*")
    if leaked:
        print(f"[FAIL] 发布包里混进不该带的东西：{leaked[:6]}"
              + (f" …共 {len(leaked)} 个" if len(leaked) > 6 else ""))
        out.append("发布包卫生：漏挡内部文件")
    if missing:
        print(f"[FAIL] 发布包里少了用户要的东西：{missing}")
        out.append("发布包卫生：误挡用户可见文件")
    if not leaked and not missing:
        print(f"[PASS] 发布包卫生：实测 git archive 共 {len(files)} 个文件 —— "
              f"测试 / 开发脚本 / 内部核验文档 0 个，用户可见文件齐全")
    return out


def pick_python() -> str:
    for cand in ASTRBOT_PY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file() and _looks_like_astrbot_python(p):
            return str(p)
    print("⚠ 未找到 AstrBot 自带解释器，回退到当前解释器；部分用例可能因缺依赖失败。")
    print("  可用 ASTRBOT_PYTHON 环境变量指定，例如：")
    print(r"  $env:ASTRBOT_PYTHON='%USERPROFILE%\AppData\Local\AstrBot\backend\python\python.exe'")
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

    # 发布包卫生：不该进包的东西由 .gitattributes 的 export-ignore 控制。
    # v0.23.5 第四轮：从「查文本关键词」升级为**逐条问 git 本人**（见 check_export_ignore）。
    fails.extend(check_export_ignore())
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
