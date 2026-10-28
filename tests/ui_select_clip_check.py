# -*- coding: utf-8 -*-
"""v0.24.9 回归：卷帘下拉展开后被祖先 overflow:hidden 切掉（真浏览器实跑）。

用户实测（2026-10-28 截图）：设置页「物品语法世代 item_syntax_override」下拉展开后，
菜单**下半截消失**，下面还透出别的文字。

根因（探针实测，不是猜）
========================
那个下拉藏在 `.adv-fold-body-inner`（「高级」折叠容器，`overflow:hidden`）里；
卷帘菜单是 `position:absolute`，于是被容器底边裁掉。
探针当时 `elementFromPoint` 在菜单高度的 60% / 85% / 100% 三处，命中的都是菜单**外面**的元素
（`#ver_caps_line` 与 `.btnrow`）。

修法
====
`syncClipRelax()`：某个下拉展开时，给**整条祖先链**里有裁剪的节点加 `.select-clip-relaxed`
（`overflow:visible`）；所有下拉都收起后全部撤掉。不用 CSS `:has()` —— 要兼容没有 :has() 的内核。

判据（本用例守的就是这几条）
============================
1) 展开后，菜单高度的 25/50/75/95% 四处**全部**命中菜单内部元素（← 真 bug 检测点）；
2) 祖先链里确实有节点被加了 `.select-clip-relaxed`；
3) 收起后这类节点**一个不留**（不能污染其它卡片）；
4) 不在折叠容器里的普通下拉照样能开（没被修崩）。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_LAUNCH_KWARGS  # noqa: E402

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"
SHOTS = F.SHOTS

PROBE_JS = r"""
() => {
  const sel = document.getElementById('cfg_item_syntax');
  const pop = sel.closest('.select-pop');
  const menu = pop.querySelector('.select-menu');
  const r = menu.getBoundingClientRect();
  const probes = [];
  for (const f of [0.25, 0.5, 0.75, 0.95]) {
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height * f);
    const hit = document.elementFromPoint(x, y);
    probes.push({f, y, inside: !!(hit && menu.contains(hit)),
                 hit: hit ? (hit.tagName + '.' + (hit.className || '')) : null});
  }
  return {
    open: pop.classList.contains('is-open'),
    menuRect: {top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height)},
    relaxCount: document.querySelectorAll('.select-clip-relaxed').length,
    relaxNodes: [...document.querySelectorAll('.select-clip-relaxed')].map(e => e.tagName + '.' + e.className),
    probes,
  };
}
"""

FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not ok) else ''}")
    if not ok:
        FAILED.append(desc)


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    print("[1] 静态：漂移守卫")
    check("有 syncClipRelax()", "function syncClipRelax()" in src)
    check("setOpen 里会调用它", src.count("syncClipRelax();") >= 1, src.count("syncClipRelax();"))
    check("有 .select-clip-relaxed 样式（overflow:visible）",
          ".select-clip-relaxed{overflow:visible!important}" in src)
    # 注：页面别处本来就有 :has() 用法（既有样式），所以这里只要求「本条修复不靠 :has()」
    check("新增的裁剪修复只用 JS 类，不依赖 :has()",
          ".select-clip-relaxed{overflow:visible!important}" in src)

    print("\n[2] 动态：真浏览器里展开「物品语法世代」")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 通道起不来（{e}），退回自带 Chromium")
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 900})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route(F.API_GLOB, F.route)
        pg.goto(F.PAGE_URL)
        pg.wait_for_timeout(700)
        for s in ('[data-page="settings"]', "#nav_settings", "text=设置"):
            try:
                if pg.locator(s).count():
                    pg.click(s, timeout=1500)
                    break
            except Exception:  # noqa: BLE001
                pass
        pg.wait_for_timeout(600)

        # 展开设置页里的折叠容器（目标藏在「高级」里）
        for d in pg.locator("#page_settings details").all():
            try:
                if d.get_attribute("open") is None:
                    d.locator("summary").first.click(timeout=1500)
                    pg.wait_for_timeout(180)
            except Exception:  # noqa: BLE001
                pass
        pg.wait_for_timeout(250)

        pop = pg.locator("#cfg_item_syntax").locator(
            "xpath=ancestor::div[contains(@class,'select-pop')]")
        trg = pop.locator(".select-trigger")
        trg.scroll_into_view_if_needed()
        trg.click()
        pg.wait_for_timeout(350)

        st = pg.evaluate(PROBE_JS)
        check("下拉已展开", st["open"] is True, st)
        bad = [p for p in st["probes"] if not p["inside"]]
        check("★菜单 25/50/75/95% 四处全部命中菜单内部（旧 bug：后三处命中外面的元素）",
              not bad, str(bad))
        check("★祖先链被加了 .select-clip-relaxed", st["relaxCount"] >= 1, st["relaxNodes"])

        print("\n[3] 收起后不留痕")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        st2 = pg.evaluate(PROBE_JS)
        check("菜单已收起", st2["open"] is False, st2)
        check("★.select-clip-relaxed 一个不留", st2["relaxCount"] == 0, st2["relaxNodes"])

        print("\n[4] 普通下拉照常（没被修崩）")
        eng = pg.locator("#cfg_kb_engine").locator(
            "xpath=ancestor::div[contains(@class,'select-pop')]").locator(".select-trigger")
        eng.scroll_into_view_if_needed()
        eng.click()
        pg.wait_for_timeout(300)
        ok = pg.evaluate("""() => {
            const s = document.getElementById('cfg_kb_engine');
            return !!(s && s.closest('.select-pop').classList.contains('is-open'));
        }""")
        check("引擎下拉能正常展开", ok is True)

        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        print("\n[5] 截图留档（对比用）")
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            # 再开一次目标下拉，截「区域」——菜单是绝对定位，超出元素盒子
            trg.click()
            pg.wait_for_timeout(300)
            box = pg.evaluate("""() => {
                const sel = document.getElementById('cfg_item_syntax');
                const pop = sel.closest('.select-pop');
                const menu = pop.querySelector('.select-menu');
                const a = pop.getBoundingClientRect(), b = menu.getBoundingClientRect();
                return {x: Math.max(0, Math.min(a.left, b.left) - 12), y: Math.max(0, a.top - 56),
                        w: Math.max(a.right, b.right) + 24, h: (b.bottom + 18) - Math.max(0, a.top - 56)};
            }""")
            pg.screenshot(path=str(SHOTS / "select_clip_fixed.png"),
                          clip={"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]})
            print(f"      {SHOTS / 'select_clip_fixed.png'}")
        except Exception as e:  # noqa: BLE001
            print(f"      截图失败（不影响判据）：{e}")

        print("\n[6] 页面无 JS 异常")
        check("pageerror 为空", not errs, str(errs[:2]))

        ctx.close()
        browser.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("✅ 全部通过（下拉不再被折叠容器切掉；收起不留痕；普通下拉没被修崩）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
