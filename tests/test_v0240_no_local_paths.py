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

#: 账户组件（N12）：账户名是**任意非空路径组件**，不是一个 ASCII 单词 ——
#: 中文账户、带空格的账户、点与连字符都要算。旧判据只认 `[A-Za-z0-9_.-]`，
#: 于是 `C:/Users/核验账户/Desktop/private.jar` 整条漏掉（实测 9PASS / rc0）。
#: 仍排除**占位与叙述**：`<账户>` 用尖括号尖出、`…` 是省略号、`*`/`?` 是通配符 ——
#: 这些都不在 `\w` 与 `.`/`-`/空格 之中，所以讲本规距的文档本身不会被判红。
_ACCOUNT = r"[\w.\-][\w.\- ]*"

#: 本机痕迹判据（与 CHANGELOG 卫生那条同源，但**扫描范围覆盖源码与 tests/**）。
#: 只认**真路径形状**：占位写法（`C:/Users/<账户>/…`）与用法说明（`file:///...`）
#: 属于叙述，不算本机痕迹 —— 判据收得太宽会把讲这条规矩的文档本身也判红。
#: 分隔符写成 `[\\/]{1,}`：同一个判据同时管 `C:\Users\x`、`C:/Users/x` 与
#: JSON 里转义后成对的 `C:\\Users\\x`（旧版为这三种各写一条，还漏了混用）。
PATTERNS = (
    r"[A-Za-z]:[\\/]{1,}Users[\\/]{1,}" + _ACCOUNT,
    r"[A-Za-z]:[\\/]{1,}AstrBotOps[\\/]{1,}" + _ACCOUNT,
    r"file:///[A-Za-z]:/",                        # 本机文件 URL（带盘符）
)

#: 自己与「判据声明处」必然含这些串，跳过。
SKIP_FILES = {"test_v0240_no_local_paths.py", "test_v0237_changelog_hygiene.py"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "_archive_2026-10-05"}
#: `data/` 下只有**运行缓存**才豁免；`data/legacy_items/` 与 `data/legacy_registry/` 是**随包发布的运行资产**，
#: 必须一起受本守卫约束（GPT 复核 F16 / N12：此前整目录跳过，等于给发布 JSON 开了后门）。
DATA_SCAN_DIRS = {"legacy_items", "legacy_registry"}
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
    if rel == "data":
        dirs[:] = [d for d in dirs if d in DATA_SCAN_DIRS]      # 只放行随包的运行资产
    else:
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
check("病灶：靶场写死被抓", bool(scan('MATRIX = r"C:\\Users\\example\\Desktop\\mcs-matrix"')))
# N12：账户名是**任意非空路径组件** —— Unicode / 空格 / 混合分隔符 / JSON 转义都要抓。
# 样本一律用通用账户名：账户名不携带含义，而写死真实账户名本身就是个人痕迹。
check("★N12 病灶：Unicode 账户名被抓",
      bool(scan('P = "C:/Users/核验账户/Desktop/private.jar"')))
check("★N12 病灶：带空格的账户名被抓",
      bool(scan('P = "C:/Users/John Smith/Desktop/private.jar"')))
check("★N12 病灶：正反斜杠混用被抓",
      bool(scan('P = "C:\\Users\\核验账户/Desktop/jar"')))
check("★N12 病灶：运维台账目录 + Unicode 账户被抓",
      bool(scan('P = "D:\\AstrBotOps\\核验账户\\ledger"')))
check("病灶：随包发布的 JSON 里写死本机路径被抓（含 Unicode 账户 + 转义反斜杠）",
      bool(scan('{"1.12.2": {"source": "C:\\\\Users\\\\核验账户\\\\Desktop\\\\jar"}}')))
check("反例：占位写法（尖括号）不误报", not scan("见 `C:/Users/<账户>/…`（占位）"))
check("反例：省略号叙述不误报", not scan("如 `C:\\Users\\…` 这类叙述"))
check("反例：数据表里的数据值写法不误报",
      not scan('{"log2": {"domain_upper": 1, "variants": {"0": "Acacia Wood"}}}'))
check("反例：可移植写法不误报",
      not scan('MATRIX = os.path.join(os.path.expanduser("~"), "Desktop", "mcs-matrix")')
      and not scan('D = Path.home() / ".astrbot" / "data"')
      and not scan('HINT = "例如 D:\\Minecraft\\Servers\\MyPack"'))

print("\n================ 汇总 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
