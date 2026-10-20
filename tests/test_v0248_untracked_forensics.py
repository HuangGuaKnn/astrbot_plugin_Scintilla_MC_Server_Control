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

第八轮补的另一半（GPT 第七轮复核）
================================
「未跟踪」只在**还没提交**时有效：取证日志一旦被误提交，就变成**已跟踪**，上面这条判据从此
看不见它 —— 而 `git archive` 会把它原样打进发布包（附件里带着本机绝对路径）。
所以第五节改成对**归档清单**再问一遍同一个形状口径（`check_archive_forensics`），
外加白名单 `ARCHIVE_FORENSIC_ALLOW`（`_conf_schema.json` / `__init__.py` / `requirements.txt`
是合法文件但形状像取证物，必须显式登记）。「先误提交、后发布」这条路，只有这层堵得上。

第九轮再补（GPT 第八轮复核 P2-1）
================================
归档那条一开始也只扫「根 + `tests/` 一层」（与工作区那条同口径）—— 于是
`docs/evidence.log` / `tools/_probe.py` 这种**同样不该进包**的形状漏了。
现在 `is_forensic_artifact(..., deep=True)` 按**任意层级**只看形状，代价用两张显式白名单抵掉：
文件名白名单（`__init__.py` 这类任意层级都合法）+ 目录前缀白名单（`data/`、`docs/images/`）。
工作区那条**仍然**只看一层：那里 `data/` 天天在动，扫深了会把正常开发流整个拦住。

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
    # 「这条判据有没有被测到」是**说明**不是断言：工作区干净（提交之后）本来就没有东西可判，
    # 而那正是我们要的状态 —— 早先把这里写成断言，提交完再跑就红了，属于判据自己给干净状态判红。
    if st.strip():
        print(f"       工作区有中间状态可判：git status {len(st.strip().splitlines())} 条，"
              f"其中未跟踪 {len(untracked)} 个"
              f"{'：' + str(untracked[:4]) if untracked else ''}")
    else:
        print("       工作区完全干净（已提交）—— 本轮没有「未跟踪产物」可判；"
              "「判据非空转」由第二节的临时仓库实测覆盖")


# ============================================================ 五、已跟踪的取证产物
def _archive_files(repo: Path) -> list[str]:
    """`git archive HEAD` → 归档里的文件清单（目录不计；与门禁里的口径一致）。"""
    import io as _io
    import tarfile
    r = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=str(repo),
                       capture_output=True)
    tf = tarfile.open(fileobj=_io.BytesIO(r.stdout), encoding="utf-8", errors="replace")
    dirs = {m.name for m in tf.getmembers() if not m.isfile()}
    return [n for n in tf.getnames() if n not in dirs]


def part_five() -> None:
    print("\n---- 五、已提交进仓库的取证产物：未跟踪那条看不见它，发布包这条必须抓住 ----")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        # 白名单里的三个「合法但形状像取证物」的文件 + 一个真的取证产物 + 一个深层 json
        (repo / "_conf_schema.json").write_text("{}\n", encoding="utf-8")
        (repo / "__init__.py").write_text("", encoding="utf-8")
        (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")
        (repo / "_r7_probe.py").write_text("# 误提交的探针\n", encoding="utf-8")
        (repo / "data").mkdir()
        (repo / "data" / "legacy_items").mkdir()
        (repo / "data" / "legacy_items" / "1.7.10.json").write_text("{}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-q", "-m", "误提交一个取证探针")

        # 事实先行：未跟踪那条判据**确实**看不见已提交的文件（这是 GPT 第七轮复核的观察，
        # 不是我们希望的最终状态 —— 所以下面才要补发布包这一条）。
        check("事实：已提交的取证产物不在「未跟踪」名单里（那条判据的视野边界）",
              RV.check_untracked_forensics(repo) == [], str(RV.check_untracked_forensics(repo)))

        files = _archive_files(repo)
        got = RV.check_archive_forensics(files)
        check("★已提交的取证产物进包时必须被抓（_r7_probe.py）",
              got == ["_r7_probe.py"], f"归档 {sorted(files)} → 抓到 {got}")
        check("★白名单里的合法文件不许误判（_conf_schema.json / __init__.py / requirements.txt）",
              RV.check_archive_forensics(["_conf_schema.json", "__init__.py",
                                          "requirements.txt"]) == [],
              str(RV.check_archive_forensics(["_conf_schema.json", "__init__.py",
                                              "requirements.txt"])))
        check("★深层运行数据表不许误判（data/legacy_items/1.7.10.json）",
              "data/legacy_items/1.7.10.json" not in got, str(got))
        # 去掉那一个，其余应当全清（证明抓的是形状，不是「凡 json 必红」）
        check("★把误提交的那个删掉后，同一份归档零误报",
              RV.check_archive_forensics([n for n in files if n != "_r7_probe.py"]) == [],
              str(RV.check_archive_forensics([n for n in files if n != "_r7_probe.py"])))

    # ---- 第九轮（GPT 第八轮复核 P2-1）：扫描范围放宽到**任意层级** ----
    deep_hits = RV.check_archive_forensics(["docs/evidence.log", "tools/_probe.py",
                                            "core/_debug.py", "pages/_tmp.json"])
    check("★任意层级的取证形状也要抓（docs/evidence.log / tools/_probe.py / core/_debug.py）",
          deep_hits == ["core/_debug.py", "docs/evidence.log", "pages/_tmp.json", "tools/_probe.py"],
          str(deep_hits))
    allow = RV.check_archive_forensics(["_conf_schema.json", "requirements.txt", "core/__init__.py",
                                        "data/legacy_items/1.7.10.json",
                                        "data/legacy_registry/items_1.12.2.json",
                                        "docs/images/logo.png", "docs/configure.md",
                                        "core/knowledge_base.py", "pages/mc_control/index.html"])
    check("★放宽后白名单（文件名 + 目录前缀）不许误判", allow == [], str(allow))
    # ---- 第十轮复核：白名单**收窄**（整片放行 data/ 与 docs/images/ 太宽了）----
    holes = RV.check_archive_forensics(["data/_probe.json", "docs/images/evidence.log",
                                        "docs/images/_shot.png"])
    check("★白名单收窄：data/ 与 docs/images/ 下长成取证形状的仍要抓",
          holes == ["data/_probe.json", "docs/images/_shot.png", "docs/images/evidence.log"],
          str(holes))
    # ---- 第十一轮复核：连**目录内**也不能整段放行（按运行表的命名形状放行）----
    inside = RV.check_archive_forensics(["data/legacy_items/_probe.json",
                                         "data/legacy_registry/_debug.log",
                                         "data/legacy_items/_1.12.2.json"])
    check("★运行数据目录**内部**的取证形状也要抓（_probe.json / _debug.log）",
          inside == ["data/legacy_items/_1.12.2.json", "data/legacy_items/_probe.json",
                     "data/legacy_registry/_debug.log"],
          str(inside))
    future = RV.check_archive_forensics(["data/legacy_items/1.16.5.json",
                                         "data/legacy_registry/items_1.16.5.json",
                                         "data/legacy_items/1.12.2.json"])
    check("★未来新增世代（1.16.5 之类）仍按命名形状放行 —— 不用改白名单",
          future == [], str(future))
    check("★未跟踪那条**仍然**只看根与 tests/ 一层（别把运行资产扫进来）",
          not RV.is_forensic_artifact("data/legacy_items/1.7.10.json")
          and not RV.is_forensic_artifact("core/_debug.py")
          and RV.is_forensic_artifact("core/_debug.py", deep=True),
          "deep 与默认口径必须分得开")

    # 真实仓库：HEAD 的归档里不许有取证产物
    real_files = _archive_files(PLUGIN_DIR)
    real_bad = RV.check_archive_forensics(real_files)
    check(f"★真实仓库 {len(real_files)} 个文件的归档里，取证产物 0 个", real_bad == [], str(real_bad))


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 第六轮复核 B9：未跟踪的取证产物必须让门禁变红")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    part_five()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
