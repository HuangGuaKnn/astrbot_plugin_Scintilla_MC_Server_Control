"""无头浏览器实测：设置页「AI 能力 · LLM 工具与知识库」那一排卡片是否还在互相拖高度。

背景（v0.21.44）：知识库卡原先靠 5 段长 hint 堆到 ≈1260px，同排的「LLM 工具开关」卡
被 grid stretch 拉到同样高 → 左卡底部空出 814px，看着像「卡片没写完」。
现在原理与实测数据收进卡片底部的 <details class="kb-fold">（默认收起），
7 个开关收成两列。本脚本把这个契约钉死，防止哪天又把长文案塞回卡片正文。

判定：
  1) 折叠区存在，且**默认收起**（open=false）—— 否则卡片又会变高；
  2) 展开后能读到实测数据（含「实测」等字样），信息没被删掉、只是收起来；
  3) 同排两卡的内容高度差 ≤ 120px（内容高度 = 卡片顶部到最后一个子元素底部）；
  4) **一个开关只占一格**：任何 .sw-row 都不许跨列（曾被写成 `grid-column:1/-1`，看着像没写完）；
  5) **同一行的两格等高**：副标题两行时虚线仍是一条直线（靠 .kb-card 的 align-items:stretch 兜住）；
  6) 「语义增强检索」的副标题保留「仅推荐大型长期运行服务器开启」这句提醒。

跑法：python tests\\ui_kb_fold_check.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 KB_FOLD_TEST_PAGE 指路）
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
PAGE_URL = os.environ.get("KB_FOLD_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()

ROW_MAX_GAP = 120          # 同排两卡的内容高度差上限（px）
KB_CONTENT_MAX = 620       # 知识库卡内容高度上限（px）—— 长文案回流会立刻超标

FIND_ROW = ("[...document.querySelectorAll('#page_settings .grid')]"
            ".find(g => /LLM 工具开关/.test(g.textContent))")

MEASURE_JS = """
(rowExpr) => {
  document.querySelectorAll('.page').forEach(p => p.classList.add('active'));
  const grid = eval(rowExpr);
  if (!grid) return { error: '没找到「AI 能力」那一排卡片' };
  const cards = [...grid.children].filter(e => e.classList.contains('card'));
  const info = cards.map(c => {
    const r = c.getBoundingClientRect();
    let last = 0;
    for (const k of c.children) {
      const kr = k.getBoundingClientRect();
      if (kr.bottom > last) last = kr.bottom;
    }
    return { title: (c.querySelector('h3') || {}).innerText || '',
             cardH: +r.height.toFixed(0),
             contentH: +(last - r.top).toFixed(0) };
  });
  const fold = document.querySelector('details.kb-fold');
  const kb = cards.find(c => /词典与知识库/.test(c.textContent || ''));
  const gw = grid.getBoundingClientRect().width;
  const swrows = kb ? [...kb.querySelectorAll('.sw-row')].map(r => {
    const b = r.getBoundingClientRect();
    return { t: (r.querySelector('b') || {}).innerText || '',
             w: +b.width.toFixed(1), h: +b.height.toFixed(1), top: +b.top.toFixed(1) };
  }) : [];
  const sub = kb ? [...kb.querySelectorAll('.sw-t>b')].find(b => /语义增强检索/.test(b.innerText)) : null;
  const subText = sub ? sub.parentElement.innerText : '';
  return {
    info, gw: +gw.toFixed(1), swrows, subText,
    foldFound: !!fold,
    foldOpen: fold ? fold.hasAttribute('open') : null,
    inlineHints: grid ? [...grid.querySelectorAll('.card.kb-card .hint')].length : 0,
    rowH: +grid.getBoundingClientRect().height.toFixed(0),
  };
}
"""

OPEN_TEXT_JS = """
() => {
  const fold = document.querySelector('details.kb-fold');
  if (!fold) return { ok: false, text: '' };
  fold.open = true;
  const text = fold.textContent || '';
  return { ok: /实测/.test(text) && /代价|前提/.test(text), len: text.length, text: text.slice(0, 60) };
}
"""


def main() -> int:
    fails: list[str] = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(PAGE_URL)
        page.wait_for_timeout(1500)                          # 让页面自己的 fetch 失败回落后稳定
        m = page.evaluate(MEASURE_JS, FIND_ROW)
        if m.get("error"):
            print("FAIL:", m["error"])
            browser.close()
            return 1

        print("卡片内容高度：")
        for it in m["info"]:
            print(f"  {it['title'] or '（无标题）'}: 卡片 {it['cardH']}px / 内容 {it['contentH']}px")

        kb = next((it for it in m["info"] if "词典与知识库" in it["title"]), None)
        if kb is None:
            fails.append('同排没有找到「词典与知识库」卡')
        else:
            if kb["contentH"] > KB_CONTENT_MAX:
                fails.append(f"知识库卡内容高度 {kb['contentH']}px > 上限 {KB_CONTENT_MAX}px"
                             "（长文案又塞回卡片正文了？收进 details.kb-fold）")

        if len(m["info"]) == 2:
            gap = abs(m["info"][0]["contentH"] - m["info"][1]["contentH"])
            print(f"同排内容高度差：{gap}px（上限 {ROW_MAX_GAP}px）")
            if gap > ROW_MAX_GAP:
                fails.append(f"同排内容高度差 {gap}px > {ROW_MAX_GAP}px —— 短的那张会留一大截空档")
        else:
            print(f"提示：这一排当前有 {len(m['info'])} 张卡（多半是窄屏单列堆叠），跳过高度差判定")

        if not m["foldFound"]:
            fails.append("没找到 details.kb-fold（原理说明的折叠区不见了）")
        elif m["foldOpen"]:
            fails.append("details.kb-fold 默认是展开的 —— 卡片高度又被撑起来了")

        if m["foldFound"]:
            opened = page.evaluate(OPEN_TEXT_JS)
            print("展开折叠区：", opened)
            if not opened.get("ok"):
                fails.append("折叠区里读不到「实测 / 代价 / 前提」—— 说明数据被删了，而不是被收起来")

        # 4) 一个开关只占一格：不许跨列
        rows = m["swrows"]
        print(f"开关行宽度（网格半栏 ≈ {m['gw'] / 2:.0f}px）：")
        for r in rows:
            flag = "  ← 跨列了？" if r["w"] > m["gw"] * 0.6 else ""
            print(f"  {r['t'] or '（无标题）'}: {r['w']}px{flag}")
        wide = [r for r in rows if r["w"] > m["gw"] * 0.6]
        if wide:
            fails.append("有开关横跨两列（" + "、".join(r["t"] for r in wide)
                         + "）—— 一个开关应当只占一格，与同列开关左对齐")

        # 5) 同一行的两格等高（虚线不错位）
        groups: dict[float, list] = {}
        for r in rows:
            groups.setdefault(r["top"], []).append(r)
        for top, items in groups.items():
            if len(items) == 2:
                d = abs(items[0]["h"] - items[1]["h"])
                if d > 1.5:
                    fails.append(f'同一行的「{items[0]["t"]}」/「{items[1]["t"]}」不等高'
                                 f'（{items[0]["h"]} vs {items[1]["h"]}px）—— 两格虚线会一高一低')
        print(f"同行等高检查：{len(groups)} 行（两格行 {sum(1 for v in groups.values() if len(v) == 2)}，"
              f"单格行 {sum(1 for v in groups.values() if len(v) == 1)}）")

        # 6) 语义增强检索的提醒文案
        if "仅推荐大型长期运行服务器开启" not in m["subText"]:
            fails.append('「语义增强检索」的副标题里没有「仅推荐大型长期运行服务器开启」：' + repr(m["subText"]))
        else:
            print("语义增强检索副标题：", m["subText"].replace(chr(10), " / "))

        # 展开后卡片应该变高（确认折叠区真的在卡内、能点开）
        after = page.evaluate(MEASURE_JS, FIND_ROW)
        print(f"展开后整排高度：{m['rowH']}px → {after['rowH']}px")
        if after["rowH"] <= m["rowH"]:
            fails.append("展开折叠区后整排没有变高 —— 内容可能没渲染出来")

        browser.close()

    if fails:
        print("\nFAIL")
        for f in fails:
            print("  ✗", f)
        return 1
    print("\nPASS：折叠区默认收起，两卡内容高度差在容差内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
