"""v0.21.40 回归：设置页 DOM 结构完整性（真浏览器实跑）。

为什么要有它：
  给设置页加卡片时**多写/少写一个 `</div>`**，会让 `#page_settings` 容器提前闭合 ——
  后面的分组（「外观」/「自动化」）整段掉到容器外面。危害是**静默**的：
    · 页面不报错、控制台无异常、肉眼看到的卡片也都还在；
    · 只是那两段「不属于设置页」，切页签时不会跟着隐藏，设置页看起来少了两截；
    · 首当其冲的是截图脚本 make_docs_images.py（按 .set-sec 的顺序切图，直接 IndexError 崩掉）。
  手工很难发现，所以这里用真浏览器数一遍 .set-sec，并与 HTML 静态出现次数对齐。

覆盖：
  1) 静态：HTML 里 `class="set-sec"` 的出现次数 = 截图脚本 SECTIONS 的长度（切图不会越界）；
  2) 动态：真浏览器里 `#page_settings .set-sec` 数量 = 静态次数（没有掉出容器）；
  3) 动态：设置页六大分组标题按序齐全（连接 / 播报 / 指令 / 工具 / 知识库 / 外观 / 自动化全都在）；
  4) 动态：新增的「检索引擎」下拉真的渲染出来、且两档选项齐全；
  5) 动态：工具总开关的范围说明与重载插件开关，后者从配置回填且能保存。
  6) 动态：页面无 JS 异常（pageerror）。
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402  复用同一套假后端夹具与驱动补丁

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_CHANNEL, UI_LAUNCH_KWARGS  # noqa: E402  # 浏览器通道见 _paths（本机 Edge / CI bundled）

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"
SHOT_TOOL = pathlib.Path(__file__).resolve().parent / "make_docs_images.py"

FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def _settings_route(route) -> None:
    url = route.request.url.split("?", 1)[0]
    if url.endswith("/settings"):
        payload = F.settings()
        payload["settings"]["enable_mc_reload_plugin"] = True
        route.fulfill(json=payload)
    elif url.endswith("/settings/save"):
        route.fulfill(json={"ok": True, "applied": route.request.post_data_json["settings"]})
    else:
        F.route(route)


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    print("[1] 静态：.set-sec 数量 vs 截图脚本 SECTIONS 长度")
    static_n = len(re.findall(r'class="set-sec"', src))
    tool = SHOT_TOOL.read_text(encoding="utf-8")
    m = re.search(r"SECTIONS\s*=\s*\[(.*?)\]", tool, re.S)
    sections = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    print(f"      HTML 静态 {static_n} 个 / 截图脚本期望 {len(sections)} 个 {sections}")
    check("数量一致（截切图不会越界）", static_n == len(sections),
          f"HTML={static_n} SECTIONS={len(sections)}")

    print("\n[2] 动态：真浏览器里没掉出容器")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001 —— 兜底必须说出原因，否则真错误会被静默吞掉
            print(f"⚠ 通道 {UI_CHANNEL!r} 起不来（{e}），退回 playwright 自带 Chromium")
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route(F.API_GLOB, _settings_route)
        pg.goto(F.PAGE_URL)
        pg.wait_for_timeout(1000)
        pg.click('button.tab[data-page="settings"]')
        pg.wait_for_timeout(600)

        live = pg.evaluate(
            "() => [...document.querySelectorAll('#page_settings .set-sec')].map(e => e.textContent)")
        check(f"浏览器内 .set-sec 数量 = 静态（实际 {len(live)}）", len(live) == static_n)
        check("六大分组标题齐全",
              len(live) == 6 and all(k in " ".join(live) for k in
                                     ("连接与安全", "播报与桥接", "mcs 指令组",
                                      "LLM 工具与知识库", "外观", "多Agent 工作流")),
              str(live))
        check("最后一段是「自动化 · 多Agent 工作流」（旧 bug 会丢它）",
              bool(live) and "多Agent" in live[-1], str(live[-1:]))

        print("\n[3] 新卡片：检索引擎下拉真的渲染出来")
        info = pg.evaluate("""() => {
            const s = document.getElementById('cfg_kb_engine');
            if(!s) return null;
            return {visible: !!(s.offsetWidth||s.offsetHeight),
                    options: [...s.options].map(o => o.value),
                    inSettings: !!s.closest('#page_settings')};
        }""")
        check("下拉存在", info is not None)
        if info:
            check("下拉可见（不是 hidden / display:none）", info["visible"], str(info))
            check(f"两档选项齐全（实际 {info['options']}）", info["options"] == ["bm25", "legacy"])
            check("挂在设置页容器内（没掉出去）", info["inSettings"], str(info))

        print("\n[4] LLM 工具总开关说明范围，重载插件开关参与设置读写")
        tool_card = pg.locator("#cfg_tools_master").locator("xpath=ancestor::div[contains(@class,'card')]")
        caption = tool_card.locator(".master-row .sw-t > span").inner_text()
        check("总开关说明列明 12 个单项、插件重载、工作流和词典重建",
              all(name in caption for name in ("12 个工具", "mc_reload_plugin", "mc_workflow", "mc_rescan_dictionary")),
              caption)
        tool_count = tool_card.locator(".sw-grid input[type=checkbox]").count()
        check("工具栏显示 12 个单项开关", tool_count == 12, str(tool_count))

        toggle = tool_card.locator(".sw-grid #cfg_tool_reload[type=checkbox]")
        check("工具栏有重载插件单项开关 cfg_tool_reload", toggle.count() == 1)
        fields = re.search(r"const CFG_FIELDS\s*=\s*\[(.*?)\n\];", src, re.S)
        check("CFG_FIELDS 以布尔字段关联 enable_mc_reload_plugin",
              bool(fields and re.search(r'\["cfg_tool_reload"\s*,\s*"enable_mc_reload_plugin"\s*,\s*"b"\s*,\s*true\]',
                                        fields.group(1))))
        if toggle.count() == 1:
            check("读取 enable_mc_reload_plugin=True 回填为开启", toggle.is_checked())
            if toggle.is_checked():
                toggle.locator("xpath=ancestor::label[1]").click()
            with pg.expect_request(lambda request: request.method == "POST" and "/settings/save" in request.url) as sent:
                pg.locator("#btn_save_cfg_top").click()
            settings = sent.value.post_data_json["settings"]
            check("关闭重载插件开关后保存发送 enable_mc_reload_plugin=False",
                  settings.get("enable_mc_reload_plugin") is False, str(settings.get("enable_mc_reload_plugin")))

        print("\n[5] 页面无 JS 异常")
        check("pageerror 为空", not errs, str(errs[:2]))

        ctx.close()
        browser.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
