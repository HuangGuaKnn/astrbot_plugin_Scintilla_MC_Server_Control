# -*- coding: utf-8 -*-
"""UI 用例的归类清单 —— **唯一权威来源**（v0.23.5 第五轮）。

为什么单独一个文件：分类此前只写在 `.github/workflows/tests.yml` 的 env 里，
发版流程（release.yml）想复用这份判据就只能**再抄一遍**清单 —— 两份清单迟早分叉，
而分叉的方向永远是「新用例漂进没人管的软门禁，甚至哪儿都没进」。
现在清单住在这里：
  · `tests.yml` 用 `run_release_verify.py --emit-env` 把自己的 env 现算出来；
  · `release.yml` 的发布前验证（`run_release_verify.py`）读同一份；
  · 本机一键复现（`run_v0230_all.py`）也读同一份。
判据只允许存在一处 —— 这是前几轮反复踩出来的教训。

三档的含义：
  · HARD：契约类（DOM 结构 / 状态 / 顺序 / 折叠边界），与字体、滚动条宽度、
    亚像素取整无关 —— 绿了就是真的没回归，红就该挡住（**含发版**）。
  · SOFT：渲染 / 度量类（像素阈值、配色），Linux + 自带 Chromium 与本机 Edge
    的渲染细节不完全一致 —— 观察期只跑不拦。
  · TOOLS：取证 / 诊断脚本，本身不产出「通过 / 失败」结论，只负责把现场打出来。

注意清单覆盖的是 `ui_*.py` **全部文件**（不只是 `*_check.py`）：
`ui_theme_flash_trace.py` 当年就是因为不叫 `_check` 而绕过了守卫。
"""
from __future__ import annotations

from pathlib import Path

#: 契约类：硬门禁（tests.yml 的 ui-contract + 发版前验证都跑它）
HARD = [
    "ui_broadcast_order_check",
    "ui_fp_notice_check",
    "ui_kb_detail_check",
    "ui_rcon_adv_fold_check",
    "ui_remote_blur_check",
    "ui_settings_structure_check",
    "ui_theme_check",
    "ui_theme_persist_check",
    "ui_version_override_check",
]

#: 渲染 / 度量类：观察期（只跑不拦）
SOFT = [
    "ui_card_layout_check",
    "ui_kb_fold_check",
    "ui_select_option_check",
    "ui_sw_wrap_check",
]

#: 取证 / 诊断脚本：无通过失败判定
TOOLS = [
    "ui_theme_flash_trace",
]


def ui_stems(root: Path) -> list[str]:
    """`tests/` 下所有 `ui_*.py` 的文件名主干（不含扩展名）。"""
    return sorted(p.stem for p in (root / "tests").glob("ui_*.py"))


def unclassified(root: Path) -> list[str]:
    """没有被归类的 `ui_*.py` —— 新写的用例若漂在这里，门禁就是漏的。"""
    known = set(HARD) | set(SOFT) | set(TOOLS)
    return [s for s in ui_stems(root) if s not in known]


def missing_files(root: Path) -> list[str]:
    """清单里有、文件却不在了的条目（改名忘同步，守卫要抓的另一种错）。"""
    have = set(ui_stems(root))
    return sorted(s for s in (set(HARD) | set(SOFT) | set(TOOLS)) if s not in have)


def check(root: Path) -> list[str]:
    """归类守卫：返回问题列表（空 = 全部有归属、且清单里没有幽灵条目）。"""
    problems: list[str] = []
    for s in unclassified(root):
        problems.append(f"未归类的 UI 用例：{s}（硬门禁 / 观察期 / 取证脚本？"
                        f"写进 tests/_ui_manifest.py）")
    for s in missing_files(root):
        problems.append(f"清单里有、tests/ 下没有的用例：{s}（改名了？同步清单）")
    return problems
