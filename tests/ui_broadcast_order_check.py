"""无头浏览器实测：广播控制台与「文本颜色」的叫法 / 顺序是否对得齐。

背景（v0.21.45）：广播控制台的三个模式是「聊天栏 → 全屏标题 → 模拟任务输出」，
下面「文本颜色」卡片却是「全屏喊话 → 普通喊话 → 任务输出」——顺序反着、叫法也不一样，
用户得自己在脑子里翻译一遍。现在两处统一成同一套名字、同一个顺序。

判定：
  1) 广播控制台 tab 名称 = 聊天栏 / 全屏标题 / 模拟任务输出；
  2) 设置页可视化配色三格的中文标题，**按视觉从左到右的顺序**与 tab 完全一致；
  3) 设置页只有一张文本颜色卡，不残留手动配置副本；
  4) 每格仍然握着自己的输入框（标着聊天栏的那格必须装 clr_say，别串台）；
  5) 控制台配色快捷入口打开设置页，高亮可视化配色卡。

跑法：python tests\\ui_broadcast_order_check.py
"""
from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _pw_compat import patch_pw_driver as _patch_pw_driver  # noqa: E402
from _ui_page import API_GLOB, PAGE_URL, make_router  # noqa: E402

_patch_pw_driver()

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_CHANNEL, UI_LAUNCH_KWARGS  # noqa: E402

WANT = ["聊天栏", "全屏标题", "模拟任务输出"]

MEASURE_JS = r"""
() => {
  // 可视化配色三格按视觉位置（左→右、上→下）排序。
  const card = document.getElementById('srv_color_card');
  const boxes = [...card.querySelectorAll('div[style*="border-radius:8px"]')].map(b => {
    const r = b.getBoundingClientRect();
    return { label: b.querySelector('div[style*="margin-bottom:6px"]').textContent.trim().split(/\s+/)[0],
             ids: [...b.querySelectorAll('input[type=text],input[type=color]')].map(i => i.id),
             stops: (b.querySelector('[id^=stops_]') || {}).id || '',
             add: (b.querySelector('button[data-add]') || {}).dataset?.add || '',
             y: +r.top.toFixed(1), x: +r.left.toFixed(1) };
  }).sort((a, b) => (Math.abs(a.y - b.y) > 4 ? a.y - b.y : a.x - b.x));
  const colorCards = [...document.querySelectorAll('#page_settings .card')]
    .filter(el => /^文本颜色/.test(el.querySelector('h3')?.textContent.trim() || ''));
  return { boxes, colorCardIds: colorCards.map(el => el.id),
           legacyControls: document.querySelectorAll('#cfg_plain_fmt, #jump_text_color').length };
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
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001 —— 兜底必须说出原因，否则真错误会被静默吞掉
            print(f"⚠ 通道 {UI_CHANNEL!r} 起不来（{e}），退回 playwright 自带 Chromium")
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(API_GLOB, make_router({}))
        page.goto(PAGE_URL)
        page.click('button.tab[data-page="server"]')
        page.click('button[role="tab"][data-server-tab="tools"]')
        broadcast_card = page.locator(".card").filter(has=page.locator("#bc_modes"))
        tabs = [label.strip() for label in broadcast_card.locator(".bc-mode").all_text_contents()]
        hint = " ".join(broadcast_card.locator(".hint").all_text_contents())
        page.click("#bc_text_color")
        page.locator("#page_settings.active #srv_color_card.jump-highlight").wait_for(timeout=6000)
        m = page.evaluate(MEASURE_JS)
        browser.close()

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

    print("控制台配色说明：", hint.strip())
    for name in WANT:
        if name not in hint:
            fails.append(f"控制台配色说明里没提到「{name}」")

    print("设置页颜色卡：", m["colorCardIds"])
    if m["colorCardIds"] != ["srv_color_card"] or m["legacyControls"]:
        fails.append(f"设置页应仅保留可视化颜色卡：{m['colorCardIds']}")

    if fails:
        print("\nFAIL")
        for f in fails:
            print("  ✗", f)
        return 1
    print("\nPASS：控制台与唯一『文本颜色』卡的顺序、叫法完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
