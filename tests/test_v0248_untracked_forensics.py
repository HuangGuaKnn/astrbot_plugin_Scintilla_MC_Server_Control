# -*- coding: utf-8 -*-
"""v0.24.8 回归（第六轮复核 B9）：未跟踪的取证产物必须让门禁变红。

为什么要有这一条
================
取证日志 / 探针脚本是本地一次性产物，含本机绝对路径（`<用户目录>\…`），而仓库对
「内部件绝不入库」有明文红线。`.gitignore` 只在**写对名字**时才挡得住：

  * `/*.log` 挡得住 `_r4_verify.log`，挡不住 `_r4_verify.txt` / `_r4_probe.json`；
  * `tests/_r5_probe.py` 这种连规则都没有；
  * 第四轮、第五轮各漏过一次，每次都是「规则补上了、名字又换了」。

靠规则的前提是「我记得起名字」，而这恰恰是它漏的原因。所以本支判据不问规则、直接问 git：
**哪些文件已经在工作区里、却不在版本控制之下**，其中形状像取证产物的，一律红。

同时钉住**边界**（写宽了会把正常开发流整个拦住，那是另一种坏）：
`__pycache__` / `docs/_internal/` / `tests/_shots/` 这些**已被忽略**的目录不在这条判据的
视野里（`--exclude-standard` 不算它们），`tests/_paths.py` / `tests/_ui_manifest.py`
这类正经辅助模块也不该被误判。

运行：
  <AstrBot python> tests\\test_v0248_untracked_forensics.py
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
# 门禁脚本本身就在插件根目录下（`add_sys_paths()` 只加了「插件根的上级」）——单独补一条，
# 这样判据可以直接 import 它、调它真正的实现（不是抄一份逻辑进用例）。
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
import run_release_verify as RV  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


def _git(repo: Path, *args: str):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


# ============================================================ 一、形状判定
#: 阳性：像取证产物的（根目录 / tests 一层）
POSITIVE = ("_r7_probe.py", "_r4_verify.log", "verify_1234.txt", "notes.json", "x.patch",
            "tests/_r5_probe.py", "tests/_probe.log", "tests/_out.txt", "tests/x.json")
#: 阴性：正经文件不许误判
NEGATIVE = ("core/knowledge_base.py", "tests/test_v0248_review_round4.py",
            "tests/_paths.py", "tests/_ui_manifest.py", "README.md",
            "docs/VERIFY_v0.24.8.md", "data/legacy_items/1.7.10.json",
            "core/deep/thing.py", "tests/fixtures/x.json", "run_release_verify.py")


def part_one() -> None:
    print("---- 一、形状判定（阳性必须命中、阴性不许误判）----")
    missed = [p for p in POSITIVE if not RV.is_forensic_artifact(p)]
    wrong = [p for p in NEGATIVE if RV.is_forensic_artifact(p)]
    check(f"★{len(POSITIVE)} 个取证形状全部命中", not missed, "漏判：" + str(missed))
    check(f"★{len(NEGATIVE)} 个正经文件全部放行（含 tests/_paths.py 这类辅助模块）",
          not wrong, "误判：" + str(wrong))
    check("形状表本身写在模块级（外部复核脚本可以直接读）",
          isinstance(RV.FORENSIC_SUFFIXES, tuple) and "_paths.py" in RV.FORENSIC_ALLOW)


# ============================================================ 二、临时仓库实测
def part_two() -> None:
    print("\n---- 二、临时仓库：未跟踪 / intent-to-add / 已暂存，三种状态都要抓到 ----")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init")
        (repo / "tests").mkdir()
        (repo / "_r7_probe.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "notes.txt").write_text("some notes\n", encoding="utf-8")
        (repo / "tests" / "_r5_probe.py").write_text("y = 2\n", encoding="utf-8")
        (repo / "tests" / "_paths.py").write_text("z = 3\n", encoding="utf-8")
        (repo / "tests" / "test_new_thing.py").write_text("w = 4\n", encoding="utf-8")

        got = RV.check_untracked_forensics(repo)
        check("★未跟踪阶段：3 个取证产物被抓、辅助模块与正经用例放行",
              got == ["_r7_probe.py", "notes.txt", "tests/_r5_probe.py"], str(got))

        _git(repo, "add", "-N", "notes.txt")
        st = _git(repo, "status", "--porcelain").stdout
        check("前置：`git add -N` 的条目在 porcelain 里是 ` A`（不是 `??`）",
              " A notes.txt" in st.replace("\r", ""), repr(st[:120]))
        check("★intent-to-add 的取证文件照样被抓（只认 `??` 的判据会漏掉它）",
              "notes.txt" in RV.check_untracked_forensics(repo),
              str(RV.check_untracked_forensics(repo)))

        _git(repo, "add", "_r7_probe.py")
        check("★真 `git add`（已暂存未提交）的取证文件照样被抓",
              "_r7_probe.py" in RV.check_untracked_forensics(repo),
              str(RV.check_untracked_forensics(repo)))

        # 被忽略的目录不算（这是边界：`__pycache__` 之类不该把它判红）
        (repo / ".gitignore").write_text("__pycache__/\n*.tmp\n", encoding="utf-8")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "x.pyc").write_text("junk\n", encoding="utf-8")
        check("★被 .gitignore 忽略的文件不进这份名单（--exclude-standard）",
              not RV.check_untracked_forensics(repo) or
              all("__pycache__" not in n for n in RV.check_untracked_forensics(repo)),
              str(RV.check_untracked_forensics(repo)))

        plain = Path(td) / "plain"
        plain.mkdir()
        (plain / "_x.log").write_text("nope\n", encoding="utf-8")
        check("★不是 git 仓库 → 这一项不适用（不判红，也不假装通过）",
              RV.check_untracked_forensics(plain) == [], str(RV.check_untracked_forensics(plain)))


# ============================================================ 三、门禁接线
def part_three() -> None:
    print("\n---- 三、门禁接线：发现取证产物必须真的把这一轮判失败 ----")
    real = RV.check_untracked_forensics
    try:
        RV.check_untracked_forensics = lambda root=None: ["_r7_probe.py"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fails = RV.static_checks(strict_env=False)
    finally:
        RV.check_untracked_forensics = real
    out = buf.getvalue()
    check("★发现取证产物时，static_checks 返回的失败清单里有这一项",
          any("取证" in f for f in fails), str(fails))
    check("★并且打印了一行可读的 FAIL（说明是谁、往哪放）",
          "[FAIL] 工作区里有未跟踪的取证产物" in out, out[-400:])
    print("       已恢复：真实检查结果 =",
          real() or "（无未跟踪取证产物）")


# ============================================================ 四、真实仓库现状
def part_four() -> None:
    print("\n---- 四、真实仓库现状（这一条现在就得是绿的）----")
    cur = RV.check_untracked_forensics()
    check("★当前工作区没有未跟踪的取证产物", cur == [], str(cur))
    st = _git(PLUGIN_DIR, "status", "--porcelain").stdout
    untracked = [ln[3:] for ln in st.replace("\r", "").splitlines() if ln.startswith("??")]
    check("前置：工作区确实有「未跟踪 / 未提交」的中间状态可判（不是空转）",
          bool(st.strip()), "git status 是空的 —— 这条判据这次没被测到")
    print(f"       git status：{len(st.strip().splitlines())} 条；其中未跟踪 {len(untracked)} 个"
          f"{'：' + str(untracked[:4]) if untracked else ''}")


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 第六轮复核 B9：未跟踪的取证产物必须让门禁变红")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
