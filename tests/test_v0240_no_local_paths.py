# -*- coding: utf-8 -*-
"""守卫 · 仓库里不许写死本机路径（含 `tests/`）。

为什么单开一条：
  · `tests/` **不进发布包**（release.yml 打包时排除），所以「发布包卫生」扫不到它；
  · 但测试是在 CI（ubuntu）上跑的 —— 写死 `C:\\Users\\…` 的用例在 CI 上必红，
    本机却永远绿（真案：B5 自检写死了靶场目录 `Desktop\\mcs-matrix`）。
  · `tests/_paths.py` 的约定是「一律不写死本机路径，换台机器也能跑」——
    本守卫就是把这条约定变成会响的警报。

判据与样本见文末「守卫有牙」段：把病灶样本喂给扫描器，必须被抓出来。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 本机痕迹判据（与 CHANGELOG 卫生那条同源，但**扫描范围覆盖源码与 tests/**）。
#: 只认**真路径形状**：占位写法（`C:/Users/<账户>/…`）与用法说明（`file:///...`）
#: 属于叙述，不算本机痕迹 —— 判据收得太宽会把讲这条规矩的文档本身也判红。
PATTERNS = (
    r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.-]+",   # C:\Users\<真账户>\…
    r"[A-Za-z]:/Users/[A-Za-z0-9_.-]+",           # C:/Users/<真账户>/…
    r"[A-Za-z]:\\+AstrBotOps\\+[A-Za-z0-9_.-]+",  # 运维台账目录
    r"file:///[A-Za-z]:/",                        # 本机文件 URL（带盘符）
    r"Users\\+10316\\+",                      # 本机账户名
)

#: 自己与「判据声明处」必然含这些串，跳过。
SKIP_FILES = {"test_v0240_no_local_paths.py", "test_v0237_changelog_hygiene.py"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "data", "_archive_2026-10-05"}
SCAN_EXT = {".py", ".json", ".yaml", ".yml", ".md", ".html", ".sql"}

PASS, FAIL = [], []


def check(label: str, cond: bool, extra: object = "") -> None:
    if cond:
        PASS.append(label)
        print("[PASS] " + label)
    else:
        FAIL.append(label + (" ｜ " + str(extra) if extra else ""))
        print("[FAIL] " + label + (" ｜ " + str(extra) if extra else ""))


def scan(text: str) -> list[str]:
    """返回命中的行（供测试与守卫共用）。"""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for pat in PATTERNS:
            if re.search(pat, line):
                hits.append("第 %d 行：%s" % (i, line.strip()[:110]))
                break
    return hits


print("=" * 78)
print("守卫 · 仓库内不得出现本机路径（源码 + 文档 + 配置，含 tests/）")
print("=" * 78)

offenders = []
scanned = 0
for base, dirs, files in os.walk(ROOT):
    # 内审件（docs/_internal/）故意带本机路径，且被 .gitignore 挡在包外 —— 豁免。
    rel = os.path.relpath(base, ROOT).replace("\\", "/")
    if rel.startswith("docs/_internal"):
        dirs[:] = []
        continue
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fn in sorted(files):
        if fn in SKIP_FILES or os.path.splitext(fn)[1].lower() not in SCAN_EXT:
            continue
        fp = Path(base) / fn
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001  非 UTF-8 文本（图片等）直接跳过
            continue
        scanned += 1
        hits = scan(text)
        if hits:
            offenders.append(str(fp.relative_to(ROOT)).replace("\\", "/") + " → " + hits[0])

check("扫到源码 / 文档 / 配置文件（> 30 个）", scanned > 30, scanned)
check("★全库没有写死的本机路径", not offenders, "；".join(offenders[:5]))

# ---------------------------------------------------------------- 守卫有牙
print("\n---- 守卫有牙（喂病灶样本，必须被抓）----")
check("病灶：C:\\Users\\x\\y 被抓",
      bool(scan('P = r"C:\\Users\\x\\y"')))
check("病灶：C:/Users/x/y 被抓", bool(scan('P = "C:/Users/x/y"')))
check("病灶：file:/// 被抓", bool(scan('U = "file:///C:/x"')))
check("病灶：靶场写死被抓", bool(scan('MATRIX = r"C:\\Users\\10316\\Desktop\\mcs-matrix"')))
check("反例：可移植写法不误报",
      not scan('MATRIX = os.path.join(os.path.expanduser("~"), "Desktop", "mcs-matrix")')
      and not scan('D = Path.home() / ".astrbot" / "data"')
      and not scan('HINT = "例如 D:\\Minecraft\\Servers\\MyPack"'))

print("\n================ 汇总 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
