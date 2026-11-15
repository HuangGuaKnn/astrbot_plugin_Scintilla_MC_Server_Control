"""真浏览器核对概览与工作流页的总开关状态文案。"""
from __future__ import annotations

import json

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from playwright.sync_api import sync_playwright


def check_text(stat, section, expected):
    text = stat.locator(section).inner_text()
    assert text == expected, f"{stat.locator('.l').inner_text()}应显示「{expected}」，实际为「{text}」"


def main() -> None:
    overview = F.overview()
    workflow = F.workflow_status()
    workflow.update(master_enabled=False, tool_enabled=False, tool_setting_enabled=True)
    for key in ("dictionary", "knowledge", "workflow"):
        overview[key]["master_enabled"] = False
    overview["workflow"]["tool_enabled"] = False
    # 数据仍存在时，概览不能把它误报为 AI 可用。知识库单项停用也应先显示总开关。
    overview["knowledge"]["enabled"] = False

    def route(route_obj):
        endpoint = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
        payload = {"overview": overview, "workflow/status": workflow}.get(endpoint)
        if payload is None:
            F.route(route_obj)
            return
        route_obj.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(F.API_GLOB, route)
        page.goto(F.PAGE_URL)

        cards = page.locator("#ov_modules .stat")
        dictionary = cards.filter(has_text="物品词典")
        knowledge = cards.filter(has_text="知识库")
        overview_workflow = cards.filter(has_text="多Agent工作流")
        check_text(dictionary, ".n", "总开关已关闭")
        check_text(knowledge, ".n", "总开关已关闭")
        check_text(overview_workflow, ".n", "总开关已关闭")

        page.locator('button.tab[data-page="workflow"]').click()
        pipeline = page.locator("#wf_stats .stat").filter(has_text="流水线")
        check_text(pipeline, ".s", "总开关已关闭")
        assert page.locator("#sw_wf_tool").is_checked(), "单项工作流开关应保留原先的勾选"
        banner = page.locator("#wf_banner")
        assert "AI 能力" in banner.inner_text(), "工作流横幅应指向栏级总开关"

        def refresh_workflow():
            page.locator("#wf_refresh").click()
            page.locator("#wf_refresh:not([disabled])").wait_for()

        workflow.update(master_enabled=True, tool_enabled=False, tool_setting_enabled=False)
        refresh_workflow()
        check_text(pipeline, ".s", "工具不可用")
        assert not page.locator("#sw_wf_tool").is_checked(), "单项关闭时工作流开关应取消勾选"

        workflow.update(tool_enabled=True, tool_setting_enabled=True)
        refresh_workflow()
        check_text(pipeline, ".s", "工具已开放")
        assert not errors, errors
        browser.close()

    print("[PASS] 概览与工作流显示总开关状态，单项开关保留配置值")


if __name__ == "__main__":
    main()
