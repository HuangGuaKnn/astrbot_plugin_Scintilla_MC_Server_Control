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

**第四份名单：CI_OK（能不能在 CI 上跑）** —— 第五轮第一次把 UI 用例真搬上 CI 之后的教训。
上面三档回答的是「这条用例的判据有多硬」，**不回答「它在别人的机器上跑不跑得动」**。
第五轮发版流程与 CI 共用 `run_release_verify.py` 之后，9 件契约类第一次在
`ubuntu-latest + playwright 自带 Chromium` 上跑（run 37144699301），结果 **5 件红**：

    · ui_fp_notice_check      —— 等弹窗 8s 超时（本机 Edge 上正常）
    · ui_kb_detail_check      —— 等知识库卡片 8s 超时
    · ui_remote_blur_check    —— 点「去设置」30s 超时
    · ui_theme_persist_check  —— 文件里**硬编码了本机路径** `%USERPROFILE%/…`（真 bug，已修）
    · ui_version_override_check —— 18 项断言全读到「版本能力：暂时读不到」

结论不是「这些用例错了」，而是**它们的判据里含本机环境**（本机宿主页 / Edge 通道 /
绝对路径），换个 OS 就不成立。所以 `CI_OK` 是一份**白名单**：
  · 在里面 = 已在 CI 上实测跑通，CI 与发版都跑它；
  · 不在里面 = 只在**本机**跑（`run_v0230_all.py` 全跑）。
默认**不在里面**是刻意的方向：一条新用例若默认进 CI，就会像第五轮这样把主分支点红；
要进 CI，得先本机跑通、再补一次 CI 观测记录（把文件加进本名单并写清依据）。

注意清单覆盖的是 `ui_*.py` **全部文件**（不只是 `*_check.py`）：
`ui_theme_flash_trace.py` 当年就是因为不叫 `_check` 而绕过了守卫。
"""
from __future__ import annotations

from pathlib import Path

#: 契约类：硬门禁（本机全跑；其中在 `CI_OK` 里的那几件，CI 与发版也挡住）
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

#: 已在 CI（ubuntu-latest + playwright 自带 Chromium）上**实测跑通**的白名单。
#: 依据 = run 37144699301（第五轮发版流程改造后的第一次 UI 上云）：
#:   契约类 9 件里这 4 件绿；渲染类 4 件里这 3 件绿。
#: 剩下的（硬 5 / 观察期 1）判据含本机环境，见本文件顶部说明 —— 待办：逐件做成可移植，
#: 每做好一件就把它加进来，并在此处补一行实测依据（改了就得重测，别凭感觉放行）。
CI_OK = [
    # 契约类
    "ui_broadcast_order_check",     # 广播控制台顺序 / 叫法（纯 DOM 契约）
    "ui_rcon_adv_fold_check",       # RCON 卡折叠边界（纯 DOM 结构）
    "ui_settings_structure_check",  # 设置页 .set-sec 数量 / 不下坠 / 下拉渲染
    "ui_theme_check",               # 主题跟随系统 + 对比度审计（阈值取得够宽）
    # 渲染 / 度量类（观察期，只跑不拦）
    "ui_card_layout_check",         # 同排卡片等高（Δ 实测 0.0px）
    "ui_kb_fold_check",             # 折叠区内容高度差 ≤ 120px
    "ui_select_option_check",       # 原生下拉配色
]


def ui_stems(root: Path) -> list[str]:
    """`tests/` 下所有 `ui_*.py` 的文件名主干（不含扩展名）。"""
    return sorted(p.stem for p in (root / "tests").glob("ui_*.py"))


def ci_hard() -> list[str]:
    """CI 上跑的**硬门禁**子集（契约类 ∩ CI_OK）—— tests.yml 与发版流程共用。"""
    return [s for s in HARD if s in CI_OK]


def ci_soft() -> list[str]:
    """CI 上跑的**观察期**子集（渲染类 ∩ CI_OK，只跑不拦）。"""
    return [s for s in SOFT if s in CI_OK]


def local_only() -> list[str]:
    """只在**本机**跑的 UI 用例（CI 上实测不过 / 或者压根没上过 CI）。"""
    known = set(HARD) | set(SOFT) | set(TOOLS)
    return sorted(s for s in known if s not in CI_OK)


def unclassified(root: Path) -> list[str]:
    """没有被归类的 `ui_*.py` —— 新写的用例若漂在这里，门禁就是漏的。"""
    known = set(HARD) | set(SOFT) | set(TOOLS)
    return [s for s in ui_stems(root) if s not in known]


def missing_files(root: Path) -> list[str]:
    """清单里有、文件却不在了的条目（改名忘同步，守卫要抓的另一种错）。"""
    have = set(ui_stems(root))
    return sorted(s for s in (set(HARD) | set(SOFT) | set(TOOLS)) if s not in have)


def check(root: Path) -> list[str]:
    """归类守卫：返回问题列表（空 = 全部有归属、且清单里没有幽灵条目）。

    第五轮补的两条：`CI_OK` 里的名字必须真的是一个 UI 用例、且必须先在 HARD / SOFT 里
    有档 —— 否则「白名单」会变成第三份平行清单，正是这一轮在治的病。
    """
    problems: list[str] = []
    for s in unclassified(root):
        problems.append(f"未归类的 UI 用例：{s}（硬门禁 / 观察期 / 取证脚本？"
                        f"写进 tests/_ui_manifest.py）")
    for s in missing_files(root):
        problems.append(f"清单里有、tests/ 下没有的用例：{s}（改名了？同步清单）")
    graded = set(HARD) | set(SOFT)
    for s in CI_OK:
        if s not in graded:
            problems.append(f"CI_OK 里的 {s} 没有判定档（取证脚本不该进 CI；"
                            f"先在 HARD / SOFT 里给它一个档）")
        elif s not in set(ui_stems(root)):
            problems.append(f"CI_OK 里的 {s} 在 tests/ 下不存在（改名了？同步清单）")
    return problems
