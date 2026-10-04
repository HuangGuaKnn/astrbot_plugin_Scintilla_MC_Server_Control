# -*- coding: utf-8 -*-
"""发版前的**一键验证**：静态检查 + 全部 `tests/test_*.py` + UI 契约类（硬门禁）。

为什么要独立成一个脚本（v0.23.5 第五轮，GPT 复核找到的洞）：
`release.yml` 的 verify 只跑 `compileall` + `tests/test_*.py` —— 契约类 UI 用例
（`tests/ui_*_check.py` 里的硬门禁那几个）在**发版流程里一条都没跑**，而在 tests.yml
里它们是硬门禁。于是「打个 tag 直接发版」可以绕过整个 UI 契约层：跨 workflow 没有
`needs`，tests.yml 就算红了也拦不住发版。现在发版与 CI 跑同一个脚本、同一份清单。

**但「同一份清单」不能让 UI 用例凭空变得可移植**（第五轮上云实测）：
9 件契约类第一次在 `ubuntu-latest` 上跑就红了 5 件 —— 其中一件是文件里硬编码了本机
路径（真 bug，已修），其余是「判据里含本机环境」（本机宿主页 / Edge 通道）。
所以清单里多了 `CI_OK` 白名单：**CI 与发版只跑实测能跑的**，其余留在本机全量复现
（`run_v0230_all.py`）。这条边界的详细依据写在 `tests/_ui_manifest.py` 顶部。

跑法：
  <python> run_release_verify.py             # 本机全跑：静态 + 回归 + UI 硬门禁（全部契约类）
  <python> run_release_verify.py --ci        # 与 CI / 发版一致：只跑 CI_OK 里的那几件 UI
  <python> run_release_verify.py --plan      # 只打印「将要跑什么」，一条都不执行
  <python> run_release_verify.py --guard     # 只做 UI 归类守卫（清单唯一来源）
  <python> run_release_verify.py --list-ui   # 只打印四份名单（含 CI 可跑 / 本机专属）
  <python> run_release_verify.py --emit-env  # 给 CI 打印 HARD_UI / SOFT_UI / UI_LOCAL_ONLY
  <python> run_release_verify.py --ui-only --ui-set=ci-hard   # 只跑 UI（workflows 用）
  <python> run_release_verify.py --all-ui    # 连观察期 / 取证脚本也一起跑（本机复现）

逐文件**超时**（v0.23.5 第五轮）：超时按失败处理，并且**连子进程树一起收掉** ——
UI 用例会拉起浏览器，只杀父进程会留下一堆孤儿进程继续占着端口，下一个用例就跟着倒。
超时秒数可用 `PIRIKA_TEST_TIMEOUT` / `PIRIKA_UI_TIMEOUT` 覆盖。
`--ui-only` 让 tests.yml 的两个 UI job 也走这套超时（此前它们自己写 shell 循环，
同样的循环、不同的判据 —— 又是「一份判据两处实现」）。
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(TESTS))
from _ui_manifest import (  # noqa: E402
    HARD,
    SOFT,
    TOOLS,
    ci_hard,
    ci_soft,
    check as check_ui_manifest,
    local_only,
    ui_stems,
)

#: `--ui-set=` 的取值 → 实际要跑的 UI 用例（唯一一处「集合怎么算」）
UI_SETS = {
    "hard": lambda: list(HARD),          # 本机：全部契约类
    "soft": lambda: list(SOFT),
    "ci-hard": ci_hard,                  # CI / 发版：实测能跑的契约类
    "ci-soft": ci_soft,
    "all": lambda: ui_stems(ROOT),
}

# 逐文件超时（秒）：正常一条用例 1~30 秒，UI 用例含浏览器冷启动也就 40 秒上下。
# 给足两个数量级的余量，同时把「挂死」钉在有限的等待里。
TIMEOUT_TEST = float(os.environ.get("PIRIKA_TEST_TIMEOUT", "600"))
TIMEOUT_UI = float(os.environ.get("PIRIKA_UI_TIMEOUT", "900"))

# 解释器选择：测试依赖 AstrBot 运行时（UI 用例还要真实浏览器）。
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


def pick_python(quiet: bool = False) -> str:
    for cand in ASTRBOT_PY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file() and _looks_like_astrbot_python(p):
            return str(p)
    if not quiet:
        # 打到 **stderr**：CI 里 `--emit-env >> "$GITHUB_ENV"` 吃的是 stdout，
        # 一行提示混进去就是一条非法的环境变量行（GPT 第五轮）
        print("⚠ 未找到 AstrBot 自带解释器，回退到当前解释器；部分用例可能因缺依赖失败。",
              file=sys.stderr)
        print("  可用 ASTRBOT_PYTHON 环境变量指定，例如：", file=sys.stderr)
        print(r"  $env:ASTRBOT_PYTHON='C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe'",
              file=sys.stderr)
    return sys.executable


PY = pick_python(quiet=any(a in sys.argv for a in ("--emit-env", "--guard")))


def kill_tree(proc: subprocess.Popen) -> None:
    """把一个子进程**连子孙一起**收掉（超时清理用）。

    为什么不能只 `proc.kill()`：UI 用例会拉起 Edge / Chromium，父进程一死，浏览器
    进程就成了孤儿，继续占着调试端口与页面 —— 下一个用例连不上，失败原因还看不出来。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:                            # noqa: BLE001
                proc.kill()
    except Exception:                                    # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                # noqa: BLE001
            pass


def run_file(rel: str, timeout: float, env_extra: dict | None = None):
    """跑一个用例文件，返回 (是否通过, 用时, 尾部输出, 是否超时)。

    超时视为失败（并已 kill_tree 收干净）—— 「跑到天荒地老」和「红」对门禁是一回事，
    区别只在于前者会把整条流水线一起拖住。
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    kwargs = {"cwd": str(ROOT), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
              "text": True, "encoding": "utf-8", "errors": "replace", "env": env}
    if os.name != "nt":
        kwargs["start_new_session"] = True               # 让 kill_tree 能整组收掉
    t0 = time.time()
    proc = subprocess.Popen([PY, str(ROOT / rel)], **kwargs)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=20)
        except Exception:                                # noqa: BLE001
            out = ""
    cost = time.time() - t0
    ok = (not timed_out) and proc.returncode == 0
    return ok, cost, (out or ""), timed_out


def _is_internal_doc(p: str) -> bool:
    """docs/ 下属于「内部核验 / 交接」的文件：留在仓库备查，但不该进用户包。"""
    if not p.startswith("docs/"):
        return False
    name = p[len("docs/"):]
    return (name.startswith("VERIFY_") or name.startswith("CONSULT_")
            or name.endswith(".diff") or "实施单" in name)


def check_export_ignore(strict: bool = False) -> list:
    """发布包卫生：**把发布包真的打出来看**（`git archive HEAD` → tar → 列名单）。

    为什么不用 `git check-attr` 逐条问：gitattributes 的属性**不递归** ——
    `/tests/ export-ignore` 只落在目录本身，问 `tests/xxx.py` 一律得到 unspecified，
    照着写只会得到一堆假阴性。而 `git archive` 正是发版流程里生成附件的那一条命令：
    **它说包里有谁，才算数**。两头都看：该挡的必须不在（测试 / 开发脚本 / 内部文档），
    该留的必须还在（补一条 export-ignore 顺手挡掉用户文件，同样是发布事故）。

    v0.23.5 第六轮（GPT 第五轮 P2）：**环境缺件不再等于「这项检查通过」**。
    `strict=True`（CI / 发版）时，`.gitattributes` 缺失、没有 git、`git archive`
    不可用、tar 解析失败都记一条失败 —— 否则「环境里没 git」就成了卫生检查的
    免死金牌（fail-open）。本机开发环境仍只警告，不折腾开发者。
    """
    out: list = []

    def env_missing(msg: str) -> list:
        if strict:
            print(f"[FAIL] 发布包卫生无法实测：{msg}")
            return ["发布包卫生：环境缺件，卫生未实测"]
        print(f"[WARN] {msg}")
        return []

    if not (ROOT / ".gitattributes").exists():
        return env_missing("没有 .gitattributes —— 发布包会带上测试与内部文档")
    try:
        r = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                           cwd=str(ROOT), capture_output=True)   # 二进制，别加 text=True
    except Exception:                                            # noqa: BLE001
        return env_missing("没有 git（或不是仓库）—— 跳过发布包卫生实测")
    if r.returncode != 0:
        return env_missing("git archive HEAD 不可用 —— 跳过发布包卫生实测")
    try:
        # 中文文件名按 UTF-8 解（别让它落到系统 locale 上）
        names = tarfile.open(fileobj=io.BytesIO(r.stdout),
                             encoding="utf-8", errors="replace").getnames()
    except Exception as e:                                       # noqa: BLE001
        return env_missing(f"解包发布包失败：{e}")
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


def static_checks(strict_env: bool = False) -> list[str]:
    """① 静态：编译、schema / metadata 合法性、UI 归类守卫、发布包卫生。

    `strict_env`（CI / 发版）= 发布包卫生的**环境缺件**也算失败（见
    `check_export_ignore` 的说明）—— 本机手跑时留 WARN，不折腾开发者。
    """
    print("=" * 68)
    print("① 静态检查（编译 / schema / 元数据 / 归类守卫 / 发布包卫生）")
    print("=" * 68)
    fails: list[str] = []

    py_files = [f for f in ROOT.rglob("*.py") if "__pycache__" not in f.parts]
    # 仓库根的两个脚本（发版/全量复现）与 main.py 同属「没有测试 import 它」的一类 ——
    # 不编译就等于「语法错了要等主人亲手跑一次才知道」（第五轮复核）
    r = subprocess.run([PY, "-m", "compileall", "-q", "main.py", "core", "pages", "tests",
                        "run_release_verify.py", "run_v0230_all.py"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(ROOT))
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

    manifest_problems = check_ui_manifest(ROOT)
    if manifest_problems:
        for p in manifest_problems:
            print(f"[FAIL] {p}")
        fails.append("UI 归类守卫")
    else:
        print(f"[PASS] UI 归类守卫（硬门禁 {len(HARD)} / 观察期 {len(SOFT)} / "
              f"取证 {len(TOOLS)}；其中 CI 可跑 {len(ci_hard())}+{len(ci_soft())} 件，"
              f"本机专属 {len(local_only())} 件，无未归类用例）")

    fails.extend(check_export_ignore(strict=strict_env))
    return fails


def ui_plan(all_ui: bool = False, ci: bool = False, ui_set: str = "") -> list[str]:
    """本次要跑的 UI 用例。

    默认（本机）只跑硬门禁那几个（全部契约类）；`ci=True` 或 `--ui-set=ci-*` 时
    只跑 `tests/_ui_manifest.py` 里 **CI_OK** 白名单内的 —— 也就是“本机能跑、别人
    的机器上也能跑”的那些（第五轮上云实测的结论）。
    """
    if ui_set:
        if ui_set not in UI_SETS:
            raise SystemExit(f"--ui-set 只接受 {sorted(UI_SETS)}，收到 {ui_set!r}")
        return UI_SETS[ui_set]()
    if all_ui:
        return ui_stems(ROOT)
    if ci:
        return ci_hard()
    return list(HARD)


def test_plan() -> list[str]:
    return sorted(p.name for p in TESTS.glob("test_*.py"))


def plan(all_ui: bool = False, ci: bool = False, ui_set: str = "",
         ui_only: bool = False, no_ui: bool = False, only_static: bool = False) -> None:
    """打印「这一轮到底会跑什么」。

    v0.23.5 第六轮（GPT 第五轮 P2）：计划必须与 `main()` 的实际分支**同源** ——
    旧版不论 `--ui-only` 还是 `--no-ui`，都把「静态检查 + 全部回归」照抄一遍，
    与实际执行对不上。门禁的第一条性质是「它说的话可信」，计划也算它说的话。
    """
    run_static = not ui_only
    run_tests = not ui_only and not only_static
    run_ui = not no_ui and not only_static
    print("将要执行：")
    print(f"  解释器：{PY}")
    if run_static:
        print(f"  ① 静态检查：compileall / _conf_schema.json / metadata.yaml / UI 归类守卫 / 发布包卫生"
              + ("（严格：环境缺件也算失败）" if (ci or os.environ.get("CI")) else ""))
    if run_tests:
        print(f"  ② 回归 {len(test_plan())} 个 test_*.py（单文件超时 {TIMEOUT_TEST:g}s）")
    uis = ui_plan(all_ui, ci, ui_set) if run_ui else []
    if run_ui:
        print(f"  ③ UI {len(uis)} 个用例（单文件超时 {TIMEOUT_UI:g}s）")
    if not (run_static or run_tests or run_ui):
        print("  （什么都不跑：--ui-only / --no-ui / --only-static 互相抵消了）")
    if run_tests:
        for name in test_plan():
            print(f"      · tests/{name}")
    for name in uis:
        print(f"      · tests/{name}.py")
    if run_ui and not all_ui:
        skip = [s for s in (list(HARD) + list(SOFT)) if s not in uis]
        if skip:
            print(f"  ④ 本机专属（不在 CI 上跑，改了它们得在本机复现）："
                  f"{len(skip)} 件 —— {', '.join(skip)}")
            print(f"      依据见 tests/_ui_manifest.py 顶部的 CI 实测记录")


def list_ui() -> None:
    """打印四份名单 —— CI 日志里一眼看清「跑了什么、没跑什么、为什么」。"""
    print(f"硬门禁（契约类）  {len(HARD):>2} 件：{', '.join(HARD)}")
    print(f"观察期（渲染类）  {len(SOFT):>2} 件：{', '.join(SOFT)}")
    print(f"取证脚本          {len(TOOLS):>2} 件：{', '.join(TOOLS)}")
    print(f"CI 上跑           {len(ci_hard()) + len(ci_soft()):>2} 件："
          f"{', '.join(ci_hard() + ci_soft())}")
    print(f"本机专属（不上 CI）{len(local_only()):>2} 件：{', '.join(local_only())}")
    print("依据：run 37144699301（第五轮第一次把 UI 用例搬上 ubuntu-latest，"
          "9 件契约类红了 5 件）—— 详见 tests/_ui_manifest.py 顶部。")


def main(argv: list[str]) -> int:
    # v0.23.5 第六轮（GPT 第五轮 P2）：Windows 上 stdout 被重定向（管道 / 任务计划 /
    # 某些 runner）时走的是 ANSI 代码页（cp936），任何非 GBK 字符都会让**最后一行**
    # 输出崩成 UnicodeEncodeError —— 明明全绿却以 traceback 收场。
    #
    # 只把**错误策略**钉成 replace、不动编码：这样中文在任何下游都照旧可读
    # （改了编码反而会让按本地代码页读管道的人看到乱码），而个别编不出来的字符
    # 降级成 `?` 而不是抛异常 —— 输出编码绝不该影响门禁的退出码。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:                                    # noqa: BLE001
            pass
    only_plan = "--plan" in argv
    only_guard = "--guard" in argv
    only_static = "--only-static" in argv
    no_ui = "--no-ui" in argv
    all_ui = "--all-ui" in argv
    ci_mode = "--ci" in argv
    ui_only = "--ui-only" in argv
    ui_set = next((a.split("=", 1)[1] for a in argv if a.startswith("--ui-set=")), "")
    if ui_set and ui_set not in UI_SETS:
        print(f"--ui-set 只接受 {sorted(UI_SETS)}，收到 {ui_set!r}", file=sys.stderr)
        return 2
    if only_guard:
        problems = check_ui_manifest(ROOT)
        for p in problems:
            # GitHub Actions 注释：守卫失败要能直接在 PR 上看见
            print(f"::error::{p}")
        if problems:
            print(f"UI 归类守卫失败：{len(problems)} 项")
            return 1
        print(f"UI 归类守卫通过（硬门禁 {len(HARD)} / 观察期 {len(SOFT)} / 取证 {len(TOOLS)}）")
        print(f"  CI 上跑：{len(ci_hard()) + len(ci_soft())} 件；"
              f"本机专属：{len(local_only())} 件（依据见 tests/_ui_manifest.py 顶部）")
        return 0
    if "--list-ui" in argv:
        list_ui()
        return 0
    if "--emit-env" in argv:
        # CI 用它把名单写进 $GITHUB_ENV —— **只有环境变量行**，别掺任何日志
        # （混一行提示进去就是一条非法环境变量，第五轮踩过）
        print(f"HARD_UI={' '.join(ci_hard())}")
        print(f"SOFT_UI={' '.join(ci_soft())}")
        print(f"UI_LOCAL_ONLY={' '.join(local_only())}")
        return 0
    if only_plan:
        plan(all_ui, ci_mode, ui_set, ui_only, no_ui, only_static)
        return 0

    print(f"解释器：{PY}")
    # 发版 / CI 环境：发布包卫生的「环境缺件」也算失败（fail-closed）
    strict_env = bool(ci_mode or os.environ.get("CI", "").lower() in ("1", "true"))
    total_fail: list[str] = [] if ui_only else static_checks(strict_env=strict_env)
    if not only_static:
        groups = []
        labels = []
        if not ui_only:
            groups.append(("test_*.py", test_plan(), TIMEOUT_TEST, None))
            labels.append("② 回归 / 单元测试")
        if not no_ui:
            ui_names = ui_plan(all_ui, ci_mode, ui_set)
            groups.append(("ui", [f"{n}.py" for n in ui_names], TIMEOUT_UI, None))
            if all_ui:
                labels.append("③ WebUI 全部用例（含观察期 / 取证；本机复现用）")
            elif ui_set == "ci-soft":
                labels.append("③ WebUI 渲染 / 度量类（观察期，只跑不拦）")
            elif ui_set == "ci-hard" or ci_mode:
                labels.append("③ WebUI 契约类（CI 可跑子集，硬门禁）")
            else:
                labels.append("③ WebUI 契约类（本机全跑，硬门禁）")
        for (pattern, files, timeout, env_extra), label in zip(groups, labels):
            print("=" * 68)
            print(f"{label}（{len(files)} 个文件，单文件超时 {timeout:g}s）")
            print("=" * 68)
            for name in files:
                ok, cost, out, timed_out = run_file(f"tests/{name}", timeout, env_extra)
                flag = "TIMEOUT" if timed_out else ("PASS" if ok else "FAIL")
                print(f"[{flag}] tests/{name}  ({cost:.1f}s)")
                if not ok:
                    total_fail.append(name)
                    for line in out.strip().splitlines()[-12:]:
                        print("    " + line)
            print(f"→ {label}：{len(files) - len([n for n in total_fail if n in files])}"
                  f"/{len(files)} 通过\n")

    print("=" * 68)
    if total_fail:
        print("失败项：", ", ".join(total_fail))
        return 1
    print("全部通过（ALL PASS）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
