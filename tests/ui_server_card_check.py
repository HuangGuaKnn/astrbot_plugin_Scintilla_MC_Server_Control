"""真浏览器核对「实时服务器卡」的四态渲染（v0.25.0 · 硬门禁）。

钉住的是**契约**，不是像素：
  · 四个字段（在线人数 / 游戏内时间 / TPS / 版本与世代）都要在，顺序固定；
  · 每个字段都显示采样来源；
  · 四种状态必须各自可见、措辞来自后端 ``state_label``（前端不许另写一份）；
  · 「回执认不出」必须把服务器原文摊开（``.sc-raw``），不许折叠；
  · 空服不许被说成错误（``#sv_online_tag`` 说「空服」，玩家卡说「当前没有玩家在线」）；
  · 连不上时**卡片仍然要画出来**（版本字段还能看），而不是只弹一句错误；
  · 接口没给 card 的旧形状仍能退回原来的三行 kv（不白屏）。

能红证据：把 index.html 里 ``renderServerCard`` 的 ``f.raw`` 那段删掉 → 本用例红；
把 ``sv_online_tag`` 的空服分支删掉 → 本用例红；把 ``if(r.card)`` 分支删掉 →
四态全红（页面只剩兜底 kv）。

运行：<AstrBot python> tests\\ui_server_card_check.py
"""
from __future__ import annotations

import json

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from _ui_page import VIEWPORT
from playwright.sync_api import expect, sync_playwright

CAPS = {"known": True, "version": "1.20.6", "raw": "MC 1.20.6 · Forge 50.1.0",
        "source": "detected", "source_label": "服务端文件探测",
        "item_syntax": "components", "item_syntax_source": "detected",
        "supported": True, "support_note": "支持自动命令生成",
        "cutover": "1.20.5", "preflatten_cutover": "1.13", "min_supported": "1.7.10"}


def f(key, label, state, st_label, value, source, note="", raw=None) -> dict:
    d = {"key": key, "label": label, "state": state, "state_label": st_label,
         "value": value, "source": source, "note": note}
    if raw is not None:
        d["raw"] = raw
    return d


def card(overall, overall_label, summary, fields, connected=True) -> dict:
    return {"ok": True, "overall": overall, "overall_label": overall_label, "summary": summary,
            "degraded": overall != "ok", "rcon_connected": connected,
            "sampled_at": "01:50:00", "fields": fields, "legend": []}


ONLINE_OK = f("online", "在线人数", "ok", "正常", "2 / 20", "/list")
DAY_OK = f("daytime", "游戏内时间", "ok", "正常", "06:35", "time query daytime", "593 tick（0 tick = 清晨 06:00）")
TPS_OK = f("tps", "TPS", "ok", "正常", "20.0 / 19.9 / 19.8", "/tps（探针）", "1 分钟 / 5 分钟 / 15 分钟均值")
VER_OK = f("version", "版本与世代", "ok", "正常", "MC 1.20.6 · Forge 50.1.0",
           "服务端文件探测 / 手动声明", "服务端文件探测 · 物品组件世代（1.20.5+）")

CARD_OK = card("ok", "正常", "4 项全部正常", [ONLINE_OK, DAY_OK, TPS_OK, VER_OK])

CARD_EMPTY = card(
    "empty", "空服", "1 项正常 · 空服 1 项 · 该世代不支持 2 项",
    [
        f("online", "在线人数", "empty", "空服", "0 / 20", "/list",
          "服务器当前没有玩家在线 —— 空服是正常状态，不是错误"),
        f("daytime", "游戏内时间", "unsupported", "该世代不支持", "—", "time query daytime",
          "服务端回的是用法文本 —— 这条子命令在该世代不存在", raw="/time <set|add> <value>"),
        f("tps", "TPS", "unsupported", "该世代不支持", "—", "/tps（探针）",
          "服务端回了「命令不存在」（该世代没有这条命令）",
          raw="Unknown command. Try /help for a list of commands"),
        VER_OK,
    ],
)

CARD_UNRECOGNIZED = card(
    "unrecognized", "回执认不出", "3 项正常 · 回执认不出 1 项",
    [
        f("online", "在线人数", "unrecognized", "回执认不出", "—", "/list",
          "回包认不出（没有命中任何已知形态）", raw="Players online (2): Steve, Alex"),
        DAY_OK, TPS_OK, VER_OK,
    ],
)

CARD_UNREACHABLE = card(
    "unreachable", "连不上", "1 项正常 · 连不上 3 项",
    [
        f("online", "在线人数", "unreachable", "连不上", "—", "/list",
          "RCON 连接失败：ConnectionRefusedError: 拒绝连接"),
        f("daytime", "游戏内时间", "unreachable", "连不上", "—", "time query daytime",
          "RCON 连接失败：ConnectionRefusedError: 拒绝连接"),
        f("tps", "TPS", "unreachable", "连不上", "—", "/tps（探针）",
          "RCON 连接失败：ConnectionRefusedError: 拒绝连接"),
        VER_OK,
    ],
    connected=False,
)

#: 旧形状（接口没给 card）：只用来验「兜底不白屏」
LEGACY = {"ok": True, "version": "1.20.1", "daytime": "593",
          "list": {"online": 1, "max": 20, "players": ["Steve"]}, "version_caps": CAPS}


def payload_for(card_obj: dict | None, *, ok: bool = True, error: str = "") -> dict:
    """`/server/status` 的真实形状：卡片挂在 ``card`` 键上，不是整个响应本身。"""
    p = {"ok": ok, "version_caps": CAPS}
    if card_obj is not None:
        p["card"] = card_obj
    if error:
        p["error"] = error
    return p


def check_server_tabs(page) -> None:
    content = {
        "monitor": ("#sv_status", "#sv_events"),
        "players": ("#sv_players", "#sv_ban_card"),
        "tools": ("#bc_modes", "#bc_text_color"),
        "audit": ("#sv_audit_card",),
    }
    tabs = page.locator('#page_server button[role="tab"][data-server-tab]')
    assert tabs.evaluate_all("elements => elements.map(tab => tab.dataset.serverTab)") == list(content)
    selected_tab = page.locator('#page_server button[role="tab"][aria-selected="true"]')
    for category, selectors in content.items():
        panel = page.locator(f"#server_panel_{category}")
        for selector in selectors:
            assert panel.locator(selector).count() == 1, f"{selector} 未放在 {category} 分类"
    assert page.locator("#page_settings #srv_color_card").count() == 1, "文本颜色卡应放在设置页"

    def check_selection(selected):
        expect(selected_tab).to_have_count(1)
        expect(selected_tab).to_have_attribute("data-server-tab", selected)
        expect(page.locator('#page_server [role="tabpanel"]:visible')).to_have_count(1)
        expect(page.locator(f"#server_panel_{selected}")).to_be_visible()

    check_selection("monitor")
    tabs.first.press("ArrowRight")
    check_selection("players")
    expect(tabs.nth(1)).to_be_focused()
    tabs.nth(1).press("End")
    check_selection("audit")
    expect(tabs.last).to_be_focused()
    tabs.last.press("ArrowLeft")
    check_selection("tools")
    expect(tabs.nth(2)).to_be_focused()
    tabs.nth(2).press("Home")
    check_selection("monitor")
    expect(tabs.first).to_be_focused()

    tabs.nth(2).click()
    check_selection("tools")
    page.locator('button.tab[data-page="overview"]').click()
    page.locator("#page_overview").get_by_role("button", name="查看全部 →").click()
    check_selection("monitor")
    print("[PASS] 服务器分类默认打开监控，支持键盘切换，概览的最近事件入口返回监控")


def main() -> None:
    state = {"payload": {**LEGACY, "version_caps": CAPS}}

    def route(route_obj):
        endpoint = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
        if endpoint == "server/status":
            route_obj.fulfill(status=200, content_type="application/json",
                              body=json.dumps(state["payload"], ensure_ascii=False))
            return
        F.route(route_obj)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        page = browser.new_page(viewport=VIEWPORT)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route(F.API_GLOB, route)
        page.goto(F.PAGE_URL)
        page.locator('button.tab[data-page="server"]').click()
        check_server_tabs(page)
        monitor_tab = page.locator('[data-server-tab="monitor"]')
        players_tab = page.locator('[data-server-tab="players"]')

        def show(payload: dict, wait_selector: str) -> None:
            state["payload"] = payload
            page.locator("#sv_refresh").click()
            page.locator(wait_selector).wait_for(timeout=8000)

        def row(key: str):
            return page.locator(f'#sv_status .sc-row[data-key="{key}"]')

        def badge(key: str) -> str:
            return row(key).locator(".badge").inner_text().strip()

        # ---- 1) 全正常：四字段齐全、顺序固定、来源可见、无原文块 ----
        show(payload_for(CARD_OK),
             '#sv_status .sc-row[data-key="online"][data-state="ok"]')
        keys = page.locator("#sv_status .sc-row").evaluate_all(
            "els => els.map(e => e.dataset.key)")
        assert keys == ["online", "daytime", "tps", "version"], f"字段顺序不对：{keys}"
        assert page.locator("#sv_status .sc-head").inner_text().strip().startswith("正常"), \
            page.locator("#sv_status .sc-head").inner_text()
        for key in ("online", "daytime", "tps", "version"):
            assert badge(key) == "正常", f"{key} 应为正常，实际 {badge(key)}"
        assert row("tps").locator(".sc-src").inner_text().strip() == "/tps（探针）", \
            "TPS 字段必须标采样来源"
        assert page.locator("#sv_status .sc-raw").count() == 0, "全正常时不该出现原文块"
        players_tab.click()
        expect(page.locator("#sv_online_tag")).not_to_contain_text("空服")
        monitor_tab.click()

        # ---- 2) 空服 + 该世代不支持：两种状态必须分开说，原文必须摊开 ----
        show(payload_for(CARD_EMPTY),
             '#sv_status .sc-row[data-key="online"][data-state="empty"]')
        assert badge("online") == "空服", badge("online")
        assert "空服" in page.locator("#sv_status .sc-head").inner_text()
        assert badge("daytime") == "该世代不支持", badge("daytime")
        assert badge("tps") == "该世代不支持", badge("tps")
        assert badge("version") == "正常"
        assert page.locator("#sv_status .sc-raw").count() == 2, "不支持的两项都要带原文"
        raws = page.locator("#sv_status .sc-raw").all_inner_texts()
        assert any("/time <set|add> <value>" in t for t in raws), raws
        assert any("Unknown command" in t for t in raws), raws
        players_tab.click()
        expect(page.locator("#sv_online_tag")).to_contain_text("空服")
        expect(page.locator("#sv_players")).to_contain_text("当前没有玩家在线")
        monitor_tab.click()

        # ---- 3) 回执认不出：必须带原文，且不许把「认不出」说成「没人」----
        show(payload_for(CARD_UNRECOGNIZED),
             '#sv_status .sc-row[data-key="online"][data-state="unrecognized"]')
        assert badge("online") == "回执认不出", badge("online")
        assert "回执认不出" in page.locator("#sv_status .sc-head").inner_text()
        raw_text = page.locator("#sv_status .sc-raw").first.inner_text()
        assert "Players online (2): Steve, Alex" in raw_text, raw_text
        players_tab.click()
        expect(page.locator("#sv_players")).to_contain_text("拿不到")
        expect(page.locator("#sv_players")).not_to_contain_text("当前没有玩家在线")
        monitor_tab.click()

        # ---- 4) 连不上：卡片照样画，版本字段仍可用 ----
        show(payload_for(CARD_UNREACHABLE, ok=False, error="RCON 连接失败：拒绝连接"),
             '#sv_status .sc-row[data-key="online"][data-state="unreachable"]')
        assert page.locator("#sv_status .sc-row").count() == 4, "连不上也要有四个字段"
        for key in ("online", "daytime", "tps"):
            assert badge(key) == "连不上", f"{key} 应为连不上，实际 {badge(key)}"
        assert badge("version") == "正常", "版本字段与 RCON 无关，连不上也得能看"
        assert "拒绝连接" in page.locator("#sv_status").inner_text()

        # ---- 5) 旧形状兜底：没有 card 就退回三行 kv，且不许报错 ----
        show(dict(LEGACY),
             "#sv_status .kv >> nth=0")
        page.wait_for_function(
            "document.querySelectorAll('#sv_status .kv').length === 3", timeout=8000)
        assert page.locator("#sv_status .sc-row").count() == 0, "旧形状不该冒出卡片行"

        assert not errors, f"页面抛错：{errors}"
        browser.close()

    print("[PASS] 实时服务器卡：四字段齐全、五态分开说、认不出带原文、连不上仍出卡、旧形状可兜底")


if __name__ == "__main__":
    main()
