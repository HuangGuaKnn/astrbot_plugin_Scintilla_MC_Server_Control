"""v0.21.14 UI 实跑：设置页开关行「说明文字被行内 <b> 切成块」的回归。

真浏览器（Edge + 假后端）验这几件事：
  1) 「拦截后会话闩锁」说明里的行内 <b>直接短路</b>，计算样式必须是 inline、
     字号必须跟说明正文一致（11px）—— 真凶就是 .sw-t b{display:block} 把它带成了 13px 块；
  2) 该说明按「真实文本行盒」数（Range 逐字测量），必须落回 1～2 行，
     不再是「一行文字 + 一个独立块 + 逗号单占一行」的三行；
  3) 全页扫描：任何 .sw-t 的说明里都不允许出现块级子元素（防同类复制粘贴再犯）；
  4) 深 / 浅两套主题各截一张开关行特写（人眼复核）。

跑法：python tests\\ui_sw_wrap_check.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 ASTRBOT_APP_DIR 指路）
"""
from __future__ import annotations

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ui_theme_check import (  # noqa: E402  （复用同一套假后端 + 驱动补丁）
    API_GLOB, PAGE_URL, SHOTS, overview, route,
)

from playwright.sync_api import sync_playwright  # noqa: E402

ROW = '#cfg_perm_latch'

MEASURE_JS = r"""
() => {
  // 真实文本行盒：按 Range 逐文本节点取 rect，去重 top —— 不受 line-height 写法影响
  const lineCount = el => {
    const r = document.createRange(), tops = new Set();
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walk.nextNode())) {
      r.selectNodeContents(n);
      for (const rect of r.getClientRects()) if (rect.height > 0) tops.add(Math.round(rect.top));
    }
    return tops.size;
  };
  const snap = el => {
    const cs = getComputedStyle(el);
    return { display: cs.display, size: parseFloat(cs.fontSize),
             weight: cs.fontWeight, color: cs.color, lines: lineCount(el) };
  };

  const row = document.querySelector('#cfg_perm_latch').closest('.sw-row');
  const title = row.querySelector('.sw-t > b');
  const desc = row.querySelector('.sw-t > span');
  const inner = desc.querySelector('b');

  // 全页扫描：说明里的子元素不许是块级（本次真凶的通用形态）
  const offenders = [];
  for (const d of document.querySelectorAll('.sw-t > span')) {
    for (const el of d.querySelectorAll('*')) {
      const disp = getComputedStyle(el).display;
      if (!['inline', 'inline-block', 'inline-flex', 'none'].includes(disp)) {
        offenders.push({ tag: el.tagName.toLowerCase(), display: disp,
                         text: d.textContent.trim().slice(0, 18) });
      }
    }
  }
  // 说明文字行数分布（顺手看看有没有别的行被切碎）
  const lineStats = {};
  for (const d of document.querySelectorAll('.sw-t > span')) {
    const k = 'lines=' + lineCount(d);
    lineStats[k] = (lineStats[k] || 0) + 1;
  }
  const rowCount = document.querySelectorAll('.sw-t > span').length;

  return { title: snap(title), desc: snap(desc), inner: inner ? snap(inner) : null,
           innerText: desc.textContent.trim(), offenders, lineStats, rowCount };
}
"""

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            browser = pw.chromium.launch()

        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="dark")
        page = ctx.new_page()
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        page.wait_for_timeout(900)
        page.click('button.tab[data-page="settings"]')
        page.wait_for_timeout(600)

        print("[1] 说明里的行内 <b> 必须是 inline（v0.21.14 的真凶）")
        m = page.evaluate(MEASURE_JS)
        check("行内 <b> 存在", m["inner"] is not None)
        check("行内 <b> display 是 inline", m["inner"] and m["inner"]["display"] == "inline",
              str(m["inner"] and m["inner"]["display"]))
        check("行内 <b> 字号跟说明一致（11px）", m["inner"] and abs(m["inner"]["size"] - 11) < .01,
              str(m["inner"] and m["inner"]["size"]))
        check("行内 <b> 行盒数与整段一致（同一行内）",
              bool(m["inner"]) and m["inner"]["lines"] <= 1,
              f"b 独占 {m['inner']['lines'] if m['inner'] else '-'} 行")

        print("[2] 标题仍是 13px 块级、说明是 11px 灰字（修完不许影响原样式）")
        check("标题 display:block / 13px",
              m["title"]["display"] == "block" and abs(m["title"]["size"] - 13) < .01,
              f"{m['title']['display']} / {m['title']['size']}px")
        check("说明 11px", abs(m["desc"]["size"] - 11) < .01, f"{m['desc']['size']}px")
        check("说明行数正常（1～2 行）", 1 <= m["desc"]["lines"] <= 2, f"{m['desc']['lines']} 行")
        check("说明文案完整", "直接短路" in m["innerText"] and m["innerText"].endswith("立刻停手"),
              m["innerText"][:40])

        print("[3] 全页扫描：说明里不许有块级子元素")
        for o in m["offenders"]:
            print(f"      ✗ {o['tag']} display:{o['display']} «{o['text']}»")
        check("无块级子元素", not m["offenders"], f"{len(m['offenders'])} 处")
        print(f"      说明行数分布 {' '.join(f'{k}={v}' for k, v in sorted(m['lineStats'].items()))}"
              f"（共 {m['rowCount']} 条说明）")

        print("[4] 截图（深色）")
        row = page.locator(f'{ROW}').locator('xpath=ancestor::div[contains(@class,"sw-row")]')
        row.screenshot(path=str(SHOTS / "sw_wrap_dark.png"))
        page.screenshot(path=str(SHOTS / "settings_dark.png"))
        print(f"      {SHOTS / 'sw_wrap_dark.png'}")

        print("[5] 切浅色再验一遍 + 截图")
        page.click("#themebtn")
        page.wait_for_timeout(400)
        m2 = page.evaluate(MEASURE_JS)
        check("浅色下行内 <b> 仍是 inline 11px",
              m2["inner"] and m2["inner"]["display"] == "inline" and abs(m2["inner"]["size"] - 11) < .01,
              str(m2["inner"]))
        check("浅色下说明行数仍正常", 1 <= m2["desc"]["lines"] <= 2, f"{m2['desc']['lines']} 行")
        row.screenshot(path=str(SHOTS / "sw_wrap_light.png"))
        print(f"      {SHOTS / 'sw_wrap_light.png'}")

        print("[6] 复现：把旧 CSS（.sw-t b）注回去，必须重新裂成块（证明确实是它）")
        page.add_style_tag(content=".sw-t b{font-size:13px;display:block}")
        page.wait_for_timeout(250)
        m3 = page.evaluate(MEASURE_JS)
        print(f"      旧 CSS 下行内 <b> = {m3['inner']['display']} / {m3['inner']['size']}px，"
              f"说明变成 {m3['desc']['lines']} 行")
        check("旧 CSS 下确实复现（block + 13px + 行数变多）",
              m3["inner"]["display"] == "block" and abs(m3["inner"]["size"] - 13) < .01
              and m3["desc"]["lines"] >= 3,
              f"{m3['inner']} lines={m3['desc']['lines']}")

        print("[7] 版本号显示：后端带 / 不带 v 前缀都不许叠成 vv0.21.14")

        def make_route(v: str):
            # 注意：必须保持「单参数」签名 —— Playwright 见到两参数的 handler 会把
            # request 当第二参数塞进来（本插件第一版就踩了这个坑）
            def handler(route_obj):
                url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
                if url != "overview":
                    return route(route_obj)
                payload = overview()
                payload["plugin"]["version"] = v
                route_obj.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(payload, ensure_ascii=False))
            return handler

        for backend_v, want_header, want_badge in (("0.21.13", "0.21.13", "v0.21.13"),
                                                   ("v0.21.14", "v0.21.14", "v0.21.14")):
            c2 = browser.new_context(viewport={"width": 1360, "height": 950})
            p2 = c2.new_page()
            p2.route(API_GLOB, make_route(backend_v))
            p2.goto(PAGE_URL)
            p2.wait_for_timeout(900)
            got_header = p2.inner_text("#ver").strip()
            got_badge = p2.locator("#ov_plugin .badge.dim").first.inner_text().strip()
            check(f"后端 {backend_v} → 顶栏 {want_header}", got_header == want_header, got_header)
            check(f"后端 {backend_v} → 徽标 {want_badge}", got_badge == want_badge, got_badge)
            c2.close()

        ctx.close()
        browser.close()

    print()
    if failed:
        print(f"✗ 失败 {len(failed)} 项：" + "、".join(failed))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
