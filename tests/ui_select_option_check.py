"""UI 回归：原生下拉列表在深浅两套主题下的配色。

背景（v0.23.3 后用户实测发现）：<select> 的框体吃 CSS，但**展开的选项列表**
由浏览器用系统主题原生渲染，不继承 color/background —— 深色页里会出现
「白底 + 浅色字」，选项几乎看不清。

修法：color-scheme 跟随主题 + option 规则兜底。

本测试把下拉框 size 设为 3 让选项**内联渲染**（原生弹层是 OS 级图层，截不到），
然后断言：选项的字色与底色必须拉开对比、且与自己主题的变量一致。

跑法：python tests\\ui_select_option_check.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def _patch_pw_driver() -> None:
    import playwright._impl._transport as t
    orig = t.compute_driver_executable

    def patched():
        node, cli = orig()
        return (node.replace("\\\\?\\", ""), cli.replace("\\\\?\\", ""))

    t.compute_driver_executable = patched


_patch_pw_driver()

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_LAUNCH_KWARGS  # noqa: E402  # 浏览器通道见 _paths（本机 Edge / CI bundled）

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
PAGE = (PLUGIN / "pages" / "mc_control" / "index.html").as_uri()
SHOTS = PLUGIN / "tests" / "_shots"
SHOTS.mkdir(exist_ok=True)

TARGET = "cfg_perm_hint_mode"  # 用户报 bug 的那个下拉
FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def rgb(s: str) -> tuple[int, int, int]:
    nums = [int(x) for x in s.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")[:3]]
    return tuple(nums)  # type: ignore[return-value]


def lum(c: tuple[int, int, int]) -> float:
    def f(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def probe(pg, theme: str) -> dict:
    pg.goto(PAGE)
    pg.evaluate("(t) => { document.documentElement.dataset.theme = t; }", theme)
    return pg.evaluate(
        """(sel) => {
            const s = document.getElementById(sel);
            s.size = 3;
            const box = document.createElement('div');
            box.style.cssText = 'position:fixed;top:8px;left:8px;z-index:99999;background:var(--panel);padding:10px';
            box.appendChild(s);
            document.body.appendChild(box);
            const opt = s.querySelector('option');
            const a = getComputedStyle(opt), b = getComputedStyle(s), h = getComputedStyle(document.documentElement);
            return {
                theme: document.documentElement.dataset.theme,
                colorScheme: h.colorScheme,
                optColor: a.color, optBg: a.backgroundColor,
                selColor: b.color, selBg: b.backgroundColor,
                n: s.querySelectorAll('option').length,
            };
        }""",
        TARGET,
    )


with sync_playwright() as pw:
    b = pw.chromium.launch(**UI_LAUNCH_KWARGS, headless=True)
    pg = b.new_page(viewport={"width": 900, "height": 420})
    for theme in ("dark", "light"):
        info = probe(pg, theme)
        print(f"\n【{theme}】")
        check(info["theme"] == theme, f"html data-theme = {theme}")
        check(info["colorScheme"] == theme, f"color-scheme 跟随主题（拿到 {info['colorScheme']}）")
        check(info["n"] >= 3, f"选项齐全（{info['n']} 条）")
        oc, ob = rgb(info["optColor"]), rgb(info["optBg"])
        check(oc != ob, "选项字色 ≠ 底色（没被同色吞掉）", f"{info['optColor']} on {info['optBg']}")
        ratio = contrast(oc, ob)
        check(ratio >= 7.0, f"选项对比度 ≥ 7.0", f"实际 {ratio:.2f}")
        # 未修复时是「白底 + 浅色字」，对比度会掉到 1.2 左右
        check(ratio > 2.0, f"★ 未复现「白底白字」（> 2.0）", f"{ratio:.2f}")
        check(rgb(info["selBg"]) == ob, "选项底色与下拉框体一致", f"{info['selBg']} vs {info['optBg']}")
        pg.screenshot(path=str(SHOTS / f"select_option_{theme}.png"))
    b.close()

print("\n" + ("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} 项失败: {FAILS} ==="))
sys.exit(1 if FAILS else 0)
