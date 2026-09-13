"""无头浏览器实测：同一网格行里的卡片是否「等高 + 顶边对齐」。

背景：`.card+.card{margin-top:14px}`（纵向堆叠间距）漏进网格布局时，
同排第二张卡会多吃 14px 上边距 → 顶边错位、高度也差 14px，看起来「不等高」。
本脚本把这个契约钉死，防止回归。

用法：
  python tests\\ui_card_layout_check.py
  set CARD_TEST_PAGE=file:///...  && python tests\\ui_card_layout_check.py   # 验证旧页面确实会失败

判定：遍历所有 grid 容器（含 .grid / .pmode / .kb-stats 等），把子卡片按顶边
聚类成「视觉行」（阈值 40px），同一行内 Δ高 > 2px 或 Δ顶边 > 1px 即判失败。
纵向堆叠（block 容器）与提示词页 .pr-wrap（左列表 + 右编辑器，刻意 align-items:start）
不在看护范围内。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys


# 本机 AstrBot 内嵌 Python 的 site-packages 在 sys.path 里带 \\?\ 前缀，
# Playwright 的 node 驱动拿它当入口脚本会解析成 "C:" 而崩 → 先剥掉前缀。
def _patch_pw_driver() -> None:
    import playwright._impl._transport as t

    orig = t.compute_driver_executable

    def patched():
        node, cli = orig()
        return (node.replace("\\\\?\\", ""), cli.replace("\\\\?\\", ""))

    t.compute_driver_executable = patched


_patch_pw_driver()

from playwright.sync_api import sync_playwright  # noqa: E402

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
PAGE_URL = os.environ.get("CARD_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()
API_GLOB = "**/api/v1/plugins/extensions/astrbot_plugin_Scintilla_MC_Server_Control/page/**"

# 刻意不等高的容器（左右分栏 master-detail），不参与判定
EXEMPT = ["pr-wrap"]
ROW_TOLERANCE = 40          # 顶边差 < 40px 视为同一行（14px 错位会被聚进同一行）


MEASURE_JS = r"""
() => {
  // 让所有页签都显示出来，一次量完（不改结构，只加 .active）
  document.querySelectorAll('.page').forEach(p => p.classList.add('active'));
  const out = [];
  for (const p of document.querySelectorAll('*')) {
    const cs = getComputedStyle(p);
    if (cs.display !== 'grid') continue;
    const kids = [...p.children].filter(
      el => el.matches('.card,.stat,.pm') && el.getBoundingClientRect().height > 1);
    if (kids.length < 2) continue;
    out.push({
      page: (p.closest('.page') || {}).id || '?',
      parent: (p.id || p.className || p.tagName) + '[' + cs.display + ']',
      items: kids.map(el => {
        const r = el.getBoundingClientRect();
        return { id: el.id || '', cls: (el.className || '').toString().slice(0, 40),
                 h: +r.height.toFixed(1), top: +r.top.toFixed(1),
                 mt: getComputedStyle(el).marginTop };
      }),
    });
  }
  return out;
}
"""


def rows_of(items: list[dict]) -> list[list[dict]]:
    """按顶边把子元素聚成视觉行。"""
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda x: x["top"]):
        if rows and it["top"] - rows[-1][0]["top"] < ROW_TOLERANCE:
            rows[-1].append(it)
        else:
            rows.append([it])
    return rows


def main() -> int:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(API_GLOB, lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ok": True})))
        page.goto(PAGE_URL)
        page.wait_for_timeout(1200)
        grids = page.evaluate(MEASURE_JS)
        page.wait_for_timeout(300)
        grids = page.evaluate(MEASURE_JS)
        browser.close()

    bad = 0
    for g in grids:
        if any(e in g["parent"] for e in EXEMPT):
            continue
        for row in rows_of(g["items"]):
            if len(row) < 2:
                continue
            dh = max(i["h"] for i in row) - min(i["h"] for i in row)
            dt = max(i["top"] for i in row) - min(i["top"] for i in row)
            ok = dh <= 2 and dt <= 1
            print(f"{'OK  ' if ok else 'FAIL'} [{g['page']}] {g['parent']} "
                  f"Δ高={dh:.1f}px Δ顶边={dt:.1f}px ({len(row)} 张)")
            if not ok:
                bad += 1
                for i in row:
                    print(f"       h={i['h']:>7} top={i['top']:>7} mt={i['mt']:>6}  {i['id'] or i['cls']}")

    total = sum(len(rows_of(g["items"])) for g in grids)
    print(f"\n检查 {len(grids)} 个网格容器，问题行 {bad} 处")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
