"""生成 README / docs 用的界面截图（截到内容高度，设置页按分组切图）。

跑法：python tests\\make_docs_images.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe）

输出到 docs/images/{页签}-{主题}.png 与 docs/images/settings-{分组}-{主题}.png。
依赖 playwright（+ 系统 Edge/Chromium）与 Pillow；UI 数据来自 ui_theme_check 的假后端夹具。
界面有改动后重跑本脚本即可刷新文档配图。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import ui_theme_check as F  # noqa: E402  复用假后端夹具与 Playwright 驱动补丁

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = F.PLUGIN / "docs" / "images"
# 设置页各分组在 DOM 里的顺序标识（与 .set-sec 出现顺序一一对应）
SECTIONS = ["conn", "notify", "cmd", "tools", "look", "workflow"]
VIEWPORT = {"width": 1360, "height": 950}


def content_height(pg, tab: str) -> int:
    """当前页签的内容高度（页面本体 + 子元素底边，避免 min-height:100vh 留白）。"""
    return pg.evaluate(
        """(tab) => {
            const el = document.getElementById('page_' + tab);
            const r = el.getBoundingClientRect();
            const kids = [...el.children].map(c => c.getBoundingClientRect().bottom + scrollY);
            return Math.ceil(Math.max(r.bottom + scrollY, ...kids) + 18);
        }""",
        tab,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcshots_"))
    made = 0
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            browser = pw.chromium.launch()
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport=VIEWPORT, color_scheme=theme)
            pg = ctx.new_page()
            pg.route(F.API_GLOB, F.route)
            pg.goto(F.PAGE_URL)
            pg.wait_for_timeout(800)
            pg.evaluate(f"localStorage.setItem('mcctrl_theme','{theme}')")
            pg.reload()
            pg.wait_for_timeout(900)

            for tab in F.TABS:
                pg.click(f'button.tab[data-page="{tab}"]')
                pg.wait_for_timeout(400)
                raw = tmp / f"{tab}-{theme}.png"
                pg.screenshot(path=str(raw), full_page=True)
                bottom = content_height(pg, tab)
                if tab == "settings":
                    tops = pg.evaluate(
                        "() => [...document.querySelectorAll('#page_settings .set-sec')]"
                        ".map(el => Math.round(el.getBoundingClientRect().top + scrollY))")
                    tops.append(bottom)
                    for i, name in enumerate(SECTIONS):
                        top, bot = tops[i] - 10, tops[i + 1] - 6
                        im = Image.open(raw).crop((0, top, VIEWPORT["width"], bot))
                        im.save(OUT / f"settings-{name}-{theme}.png", optimize=True)
                        made += 1
                    continue
                src = Image.open(raw)
                src.crop((0, 0, VIEWPORT["width"], min(bottom, src.size[1]))).save(
                    OUT / f"{tab}-{theme}.png", optimize=True)
                made += 1
            ctx.close()
        browser.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"已生成 {made} 张 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
