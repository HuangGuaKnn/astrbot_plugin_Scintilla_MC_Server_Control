"""无头浏览器实测：广播控制台与「文本颜色」的叫法 / 顺序是否对得齐。

背景（v0.21.45）：广播控制台的三个模式是「聊天栏 → 全屏标题 → 模拟任务输出」，
下面「文本颜色」卡片却是「全屏喊话 → 普通喊话 → 任务输出」——顺序反着、叫法也不一样，
用户得自己在脑子里翻译一遍。现在两处统一成同一套名字、同一个顺序。

判定：
  1) 广播控制台 tab 名称 = 聊天栏 / 全屏标题 / 模拟任务输出；
  2) 服务器页『文本颜色』三格的中文标题，**按视觉从左到右的顺序**与 tab 完全一致；
  3) 设置页『文本颜色』里三段「单色 / 渐变锚点」标签也是同一顺序、同一叫法；
  4) 每格仍然握着自己的输入框（标着聊天栏的那格必须装 clr_say，别串台）；
  5) 控制台配色说明写的是「下方」——颜色卡确实在它下面。

跑法：python tests\\ui_broadcast_order_check.py
"""
from __future__ import annotations

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
PAGE_URL = os.environ.get("BC_ORDER_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()

WANT = ["聊天栏", "全屏标题", "模拟任务输出"]

MEASURE_JS = r"""
() => {
  document.querySelectorAll('.page').forEach(p => p.classList.add('active'));
  const tabs = [...document.querySelectorAll('#bc_modes .bc-mode')]
                 .map(b => b.textContent.trim());
  // 服务器页『文本颜色』三格：按视觉位置（左→右、上→下）排序，免得再被 HTML 顺序骗到
  const card = document.getElementById('srv_color_card');
  const boxes = [...card.querySelectorAll('div[style*="border-radius:8px"]')].map(b => {
    const r = b.getBoundingClientRect();
    return { label: b.querySelector('div[style*="margin-bottom:6px"]').textContent.trim().split(/\s+/)[0],
             ids: [...b.querySelectorAll('input[type=text],input[type=color]')].map(i => i.id),
             stops: (b.querySelector('[id^=stops_]') || {}).id || '',
             add: (b.querySelector('button[data-add]') || {}).dataset?.add || '',
             y: +r.top.toFixed(1), x: +r.left.toFixed(1) };
  }).sort((a, b) => (Math.abs(a.y - b.y) > 4 ? a.y - b.y : a.x - b.x));
  const hint = (card.previousElementSibling.querySelector('.hint') || {}).textContent || '';
  // 设置页那张卡的三段标签
  const setCard = [...document.querySelectorAll('#page_settings .card')].find(c => {
    const h = c.querySelector('h3');
    return h && h.textContent.trim().replace(/\s+/g, '').startsWith('文本颜色');
  });
  const setLabels = setCard ? [...setCard.querySelectorAll('label.f')]
                    .map(l => l.textContent.trim()).filter(t => /渐变锚点/.test(t)) : [];
  return { tabs, boxes, hint, setLabels };
}
"""

# 每格该装的输入框（key = 中文标题）
WANT_IDS = {
    "聊天栏": {"clr_say", "clr_say_txt", "stops_say", "say"},
    "全屏标题": {"clr_title", "clr_title_txt", "stops_title", "title"},
    "模拟任务输出": {"clr_fb", "clr_fb_txt", "stops_fb", "fb"},
}


def main() -> int:
    fails: list[str] = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(PAGE_URL)
        page.wait_for_timeout(1500)
        m = page.evaluate(MEASURE_JS)
        browser.close()

    tabs = m["tabs"]
    print("广播控制台 tab ：", tabs)
    if tabs != WANT:
        fails.append(f"控制台 tab 顺序/叫法不是 {WANT}，实际 {tabs}")

    got = [b["label"] for b in m["boxes"]]
    print("文本颜色三格（视觉序）：", got)
    if got != WANT:
        fails.append(f"『文本颜色』三格顺序/叫法与控制台不一致：{got} ≠ {WANT}")

    for b in m["boxes"]:
        own = set(b["ids"]) | {b["add"], b["stops"]}
        want = WANT_IDS.get(b["label"], set())
        missing = {i for i in want if i not in own}
        print(f"  {b['label']}: 输入框 {b['ids']} stops={b['stops']} add={b['add']}")
        if missing:
            fails.append(f"「{b['label']}」那格少了 {sorted(missing)} —— 改顺序时把格子串台了")

    print("控制台配色说明：", m["hint"].strip())
    if "下方" not in m["hint"] or "上方" in m["hint"]:
        fails.append("控制台配色说明里的「上方/下方」不对：颜色卡就在它下面，应写「下方」")
    for name in WANT:
        if name not in m["hint"]:
            fails.append(f"控制台配色说明里没提到「{name}」")

    print("设置页标签：", m["setLabels"])
    for name, lbl in zip(WANT, m["setLabels"]):
        if not lbl.startswith(name):
            fails.append(f"设置页『文本颜色』标签顺序/叫法不一致：期望以「{name}」开头，实际 {m['setLabels']}")
            break

    if fails:
        print("\nFAIL")
        for f in fails:
            print("  ✗", f)
        return 1
    print("\nPASS：控制台与两张『文本颜色』卡的顺序、叫法完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
