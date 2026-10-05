# -*- coding: utf-8 -*-
"""v0.23.7 · B2④ 单色格式下拉框 UI 回归（真浏览器 / 无头 Edge）。

覆盖主人的设计原话：
  「不开渐变下默认 hex、回退用固定格式化颜色；像渐变格式一样给下拉框选新版/旧版，
    并把版本号标清楚」

断言：
  [1] 服务器页颜色卡：下拉 2 项（hex / named），标签带版本号，值与后端键一致；
  [2] 打开时按 GET /colors 的 plain.format 回显；
  [3] 显隐关系：单色格式行常驻可见（渐变关闭也在）；渐变格式行随开关显隐，开启后两者并存；
  [4] 点保存 → POST colors/save 载荷里带 plain_color_format；
  [5] 设置页镜像：#cfg_plain_fmt 同款 2 项 + 按 settings 载荷回显；
  [6] 重置 → 回默认 hex。
  [7] 两框对调：单色格式行在渐变输出格式行**之前**（服务器页 + 设置页镜像同序）；
  [8] 单色格式＝固定格式化色 → 取色器换成 16 色名选项框（§0-§f 顺序、标签带符号与中文），
      hex 值按下后端的最近色名回显（#FFD700 → gold），且**不静默改写**原始值；
  [9] 选色名 → 保存载荷直接写色名（gold / red）；切回 hex → 取色器与文本框回来；
  [10] 近似算法逐色对照：前端 nearestNamedColor 与后端 nearest_vanilla_color 完全一致。

跑法：python tests\\ui_plain_format_check.py
"""
from __future__ import annotations

import json
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
from _paths import UI_CHANNEL, UI_LAUNCH_KWARGS  # noqa: E402

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
PAGE_URL = (PLUGIN / "pages" / "mc_control" / "index.html").as_uri()
SHOTS = PLUGIN / "tests" / "_shots"
SHOTS.mkdir(exist_ok=True)
API_GLOB = ("**/api/v1/plugins/extensions/"
            "astrbot_plugin_Scintilla_MC_Server_Control/page/**")

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


SETTINGS = {
    "plain_color_format": "named",
    "gradient_enabled": False,
    "gradient_format": "json",
    "color_say": "#FFD700", "color_title": "gold", "color_feedback": "gold",
    "gradient_colors_say": "#FFFFFF,#55FFFF",
    "gradient_colors_title": "#FFAA00,#FF00FF",
    "gradient_colors_feedback": "#FFAA00,#55FF55",
    "feedback_name": "[MC]", "feedback_tellraw": True,
}
COLORS = {
    "ok": True,
    "colors": {"color_title": "gold", "color_say": "#FFD700", "color_feedback": "gold"},
    "gradient": {
        "enabled": False, "format": "json",
        "stops": {"gradient_colors_title": "#FFAA00,#FF00FF",
                  "gradient_colors_say": "#FFFFFF,#55FFFF",
                  "gradient_colors_feedback": "#FFAA00,#55FF55"},
        "formats": {"json": "mock", "compat_section": "mock",
                    "compat_amp": "mock", "legacy_amp": "mock"},
    },
    "plain": {"format": "named", "formats": {"hex": "mock", "named": "mock"}},
}
state: dict = {"saves": []}


def _json(route_obj, payload: dict) -> None:
    route_obj.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))


def route(route_obj) -> None:
    url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
    if route_obj.request.method == "POST":
        try:
            body = json.loads(route_obj.request.post_data or "{}")
        except Exception:
            body = {}
        if url == "colors/save":
            state["saves"].append(body)
            COLORS["plain"]["format"] = str(body.get("plain_color_format") or "hex")
            payload = dict(COLORS)
            payload.update({"notice": "mock：设置已保存并即时生效", "save_warning": ""})
            _json(route_obj, payload)
            return
        _json(route_obj, {"ok": True})
        return
    if url == "colors":
        _json(route_obj, COLORS)
    elif url == "settings":
        _json(route_obj, {"ok": True, "settings": SETTINGS,
                          "rcon_password_configured": True})
    elif url == "overview":
        _json(route_obj, {"ok": True, "config": SETTINGS, "server": {},
                          "counts": {}, "version": "1.13.2"})
    else:
        _json(route_obj, {"ok": True})


def opts(page, sel: str):
    return page.eval_on_selector_all(
        f"{sel} option", "els=>els.map(e=>[e.value, e.textContent])")


def main() -> int:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001
            print(f"通道 {UI_CHANNEL!r} 起不来（{e}），退回自带 Chromium")
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        # 颜色卡在「服务器」页 —— 默认停在概览，先切页再断言（不可见元素也点不动）
        page.click('[data-page="server"]')
        page.wait_for_function(
            "document.querySelector('#clr_plain_fmt')"
            " && document.querySelector('#clr_plain_fmt').options.length===2",
            timeout=10000)

        print("[1] 颜色卡下拉：选项与版本号")
        o = opts(page, "#clr_plain_fmt")
        check("恰好 2 项", len(o) == 2, str(len(o)))
        check("值与后端键 hex / named 对齐",
              [v for v, _ in o] == ["hex", "named"], str([v for v, _ in o]))
        check("hex 项标注 1.16+", "1.16+" in o[0][1], o[0][1])
        check("named 项标注 全世代", "全世代" in o[1][1], o[1][1])
        check("行内提示写了 hex 需 1.16+ / 旧版自动回退",
              "hex 需客户端 1.16+ · 旧版自动回退" in page.inner_text("#plain_fmt_row"))

        print("[2] 按 GET /colors 的 plain.format 回显")
        check("选中 named", page.input_value("#clr_plain_fmt") == "named",
              page.input_value("#clr_plain_fmt"))

        print("[3] 显隐关系（渐变关闭时单色行必须在）")
        check("单色格式行常驻可见", page.is_visible("#plain_fmt_row"))
        check("渐变关闭 → 渐变格式行隐藏", not page.is_visible("#grad_fmt_row"))
        page.eval_on_selector("#clr_grad_en",
                             "el=>{el.checked=true; el.dispatchEvent(new Event('change'));}")
        check("开启渐变 → 两行并存",
              page.is_visible("#grad_fmt_row") and page.is_visible("#plain_fmt_row"))
        page.eval_on_selector("#clr_grad_en",
                             "el=>{el.checked=false; el.dispatchEvent(new Event('change'));}")
        check("关回渐变 → 单色行仍在", page.is_visible("#plain_fmt_row"))

        print("[4] 保存载荷带 plain_color_format")
        page.select_option("#clr_plain_fmt", "hex")
        page.click("#clr_save")
        page.wait_for_timeout(700)
        body = state["saves"][-1] if state["saves"] else {}
        check("POST colors/save 已发出", bool(state["saves"]), str(state["saves"])[:80])
        check("载荷 plain_color_format == hex", body.get("plain_color_format") == "hex",
              str(body.get("plain_color_format")))
        check("原有字段未被挤掉（gradient_format / 三项颜色）",
              body.get("gradient_format") == "json"
              and body.get("color_say") == "#FFD700"
              and "gradient_colors_say" in body)
        check("保存后与后端回显一致（hex）",
              page.input_value("#clr_plain_fmt") == "hex")

        print("[5] 设置页镜像下拉")
        page.click('[data-page="settings"]')
        page.wait_for_function(
            "document.querySelector('#cfg_plain_fmt')"
            " && document.querySelector('#cfg_plain_fmt').options.length===2",
            timeout=8000)
        o2 = opts(page, "#cfg_plain_fmt")
        check("恰好 2 项", len(o2) == 2, str(len(o2)))
        check("选项值与颜色卡一致",
              [v for v, _ in o2] == ["hex", "named"], str([v for v, _ in o2]))
        check("标签带版本号", "1.16+" in o2[0][1] and "全世代" in o2[1][1])
        check("按 settings 载荷回显 named（不是硬编码默认）",
              page.input_value("#cfg_plain_fmt") == "named",
              page.input_value("#cfg_plain_fmt"))

        print("[6] 重置回默认 hex")
        page.click('[data-page="server"]')
        page.click("#clr_reset")
        check("重置后为 hex", page.input_value("#clr_plain_fmt") == "hex")

        print("[7] 两框对调：单色格式行在渐变输出格式行之前")
        check("服务器页：单色格式行在前",
              page.evaluate("""()=>{
                const a=document.querySelector('#plain_fmt_row'),
                      b=document.querySelector('#grad_fmt_row');
                return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
              }"""))
        page.click('[data-page="settings"]')
        page.wait_for_selector("#cfg_plain_fmt")
        check("设置页镜像同序（单色在前）",
              page.evaluate("""()=>{
                const a=document.querySelector('#cfg_plain_fmt'),
                      b=document.querySelector('#cfg_grad_fmt');
                return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
              }"""))
        page.click('[data-page="server"]')
        page.wait_for_selector("#clr_plain_fmt")

        print("[8] 单色格式＝固定格式化色 → 16 色名选项框（替代取色器）")
        # 先走一遍保存往返：mock 会把 color_say 回显成自定义 hex（#FFD700）——
        # 正好用来验「hex 值 → 后端最近色名」这条链路（重置后的默认值是 white，样本不够刁）
        page.click("#clr_save")
        page.wait_for_timeout(700)
        check("保存往返后 color_say = #FFD700（样本就位）",
              page.evaluate("()=>document.querySelector('#clr_say_txt').value") == "#FFD700",
              page.evaluate("()=>document.querySelector('#clr_say_txt').value"))
        page.select_option("#clr_plain_fmt", "named")
        page.wait_for_timeout(150)
        check("三个取色器全部隐藏",
              not any(page.is_visible(s) for s in ("#clr_say", "#clr_title", "#clr_fb")))
        check("三个色名下拉全部可见",
              all(page.is_visible(s) for s in ("#clr_say_sel", "#clr_title_sel", "#clr_fb_sel")))
        check("文本框让位（也隐藏）", not page.is_visible("#clr_say_txt"))
        check("近似色块可见", page.is_visible("#clr_say_sw"))
        o3 = opts(page, "#clr_say_sel")
        check("色名下拉恰 16 项", len(o3) == 16, str(len(o3)))
        check("顺序＝§0-§f（首黑末白）",
              [v for v, _ in o3][:2] == ["black", "dark_blue"]
              and [v for v, _ in o3][-1] == "white", str([v for v, _ in o3]))
        check("标签带格式化符号与中文（§6 · 金 · gold）",
              o3[6][0] == "gold" and "§6" in o3[6][1] and "金" in o3[6][1], o3[6][1])
        check("hex 值按后端最近色名回显：#FFD700 → gold",
              page.input_value("#clr_say_sel") == "gold", page.input_value("#clr_say_sel"))
        check("原始值不被静默改写（文本框仍留 #FFD700）",
              page.evaluate("()=>document.querySelector('#clr_say_txt').value") == "#FFD700",
              page.evaluate("()=>document.querySelector('#clr_say_txt').value"))
        check("近似色块已上色（gold = 255,170,0）",
              "255, 170, 0" in page.evaluate(
                  "()=>getComputedStyle(document.querySelector('#clr_say_sw')).backgroundColor"))
        check("提示文案切换为色名口径",
              "16 色名" in page.inner_text("#clr_color_hint"), page.inner_text("#clr_color_hint"))

        print("[9] 选色名 → 载荷写色名；切回 hex → 取色器回来")
        page.select_option("#clr_say_sel", "aqua")
        check("选色名后文本框同步为色名",
              page.evaluate("()=>document.querySelector('#clr_say_txt').value") == "aqua")
        page.select_option("#clr_title_sel", "red")
        page.click("#clr_save")
        page.wait_for_timeout(700)
        body2 = state["saves"][-1] if state["saves"] else {}
        check("载荷 color_say == aqua", body2.get("color_say") == "aqua", str(body2.get("color_say")))
        check("载荷 color_title == red", body2.get("color_title") == "red", str(body2.get("color_title")))
        check("载荷 plain_color_format == named", body2.get("plain_color_format") == "named")
        page.locator("#srv_color_card").screenshot(path=str(SHOTS / "plain_named_mode.png"))
        page.select_option("#clr_plain_fmt", "hex")
        page.wait_for_timeout(150)
        check("切回 hex → 取色器回来", page.is_visible("#clr_say"))
        check("切回 hex → 文本框回来", page.is_visible("#clr_say_txt"))
        check("切回 hex → 色名下拉隐藏", not page.is_visible("#clr_say_sel"))
        check("切回 hex → 提示文案回到取色器口径",
              "取色器点选" in page.inner_text("#clr_color_hint"))

        print("[10] 近似算法逐色对照（前端 ↔ 后端 nearest_vanilla_color）")
        sample = ["#FFD700", "#FFAA00", "#FFFFFF", "#000000", "#123456", "#2E8B57",
                  "#FF0000", "#00FF00", "#0000FF", "#808080", "#C0C0C0", "#4B0082",
                  "#8B4513", "#00CED1", "#FF69B4", "#7FFF00", "#2F4F4F", "#DC143C",
                  "#1E90FF", "#ADFF2F", "#FF4500", "#A0522D", "#4682B4", "#DDA0DD"]
        # 页面业务脚本在闭包作用域里（evaluate 摸不到内部函数），因此把**页面源码里的
        # MC_HEX 表 + 近似算法原文**抽出来 eval —— 测的是真身，不是复写版。
        html_src = (PLUGIN / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
        mc_hex_line = [ln for ln in html_src.splitlines() if ln.startswith("const MC_HEX = {")][0]
        i0 = html_src.index("const MC_NAME_ORDER = [")
        i1 = html_src.index("function fillNameSelects(){")
        algo_src = mc_hex_line + chr(10) + html_src[i0:i1]
        js_names = page.evaluate(
            "a=>{ eval(a.src); return a.list.map(c=>nearestNamedColor(c)); }",
            {"src": algo_src, "list": sample})
        try:
            sys.path.insert(0, str(PLUGIN))
            from core.text_outbound import nearest_vanilla_color as _nv  # noqa: E402
            py_names = [_nv(c) for c in sample]
            diff = [(c, a, b) for c, a, b in zip(sample, js_names, py_names) if a != b]
            check(f"{len(sample)} 个取样色前后端判定一致", not diff, str(diff))
        except Exception as e:  # noqa: BLE001
            print(f"  跳过前后端对照（后端不可导入：{e}）")
        js_short = page.evaluate(
            "a=>{ eval(a.src); return [nearestNamedColor('§6'), nearestNamedColor('&c'),"
            " nearestNamedColor('6'), nearestNamedColor('gold'), nearestNamedColor('')]; }",
            {"src": algo_src})
        check("符号简写也认：§6 → gold / &c → red",
              js_short[0] == "gold" and js_short[1] == "red", str(js_short[:2]))
        check("裸数字不误判（'6' → null）", js_short[2] is None, str(js_short[2]))
        check("色名原样返回（gold → gold）", js_short[3] == "gold", str(js_short[3]))
        check("空串 → null", js_short[4] is None, str(js_short[4]))

        page.eval_on_selector("#clr_plain_fmt", "el=>{el.size=3;}")
        page.locator("#srv_color_card").screenshot(
            path=str(SHOTS / "plain_format_row.png"))
        print(f"  截图：{SHOTS / 'plain_format_row.png'} / plain_named_mode.png")
        browser.close()

    print("\n" + "=" * 42)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  FAIL", f)
        return 1
    print("全部通过 ✅（单色格式下拉：默认 hex / 旧版回退 / 版本号标注 / 双页面镜像）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
