"""控制台浏览器硬门禁，可按判据号单独运行以验证生产代码变异。"""
from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs, urlsplit

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from _ui_page import API_GLOB, PAGE_URL, VIEWPORT, fulfill, split_url
from playwright.sync_api import expect, sync_playwright


ONLINE = "There are 1 of a max of 20 players online: Alex"
STOP = "Stopping the server"
INDIRECT = "execute as @a at @e[type=item] run stop"
IPV4 = "192.0.2.9"
IPV6 = "2001:db8::42"
LITERAL = f'<span id="console_injected">示例文字 {IPV4} [{IPV6}]</span>'
LABELS = {
    "ok": "正常", "unsupported": "该世代不支持",
    "unrecognized": "回执认不出", "unreachable": "连不上",
}


def warning(code, text, level="high"):
    return {"code": code, "text": text, "level": level}


def preview(command):
    commands = command.splitlines()
    hints = (warning("multiline", f"这会执行 {len(commands)} 条命令", "medium"),) if len(commands) > 1 else ()
    return {
        "authorized": True, "ok": True, "mode": "full", "warnings": hints,
        "results": [{
            "command": line, "accepted": True,
            "warnings": [
                warning("indirect", "嵌套命令：stop", "medium"),
                warning("service", "stop 会关闭服务器"),
                warning("selector_a", "作用范围：@a 所有在线玩家"),
                warning("selector_e", "作用范围：@e[type=item] 批量实体"),
            ] if line == INDIRECT else [],
        } for line in commands],
    }


class Scenario:
    def __init__(self, page):
        self.page = page
        self.authorized = True
        self.previews = []
        self.writes = []
        self.log_requests = []
        self.errors = []
        self.log = {
            "state": "ok", "state_label": LABELS["ok"], "note": "",
            "lines": ["第一条日志"], "cursor": "first",
        }
        self.replies = {
            "list": ("ok", ONLINE),
            "stop": ("ok", STOP),
            INDIRECT: ("ok", STOP),
            "whiteelephant": ("unsupported", "Unknown command"),
            "give Alex stone": ("unrecognized", ""),
            "seed": ("unreachable", ""),
            "say hello": ("ok", LITERAL),
        }
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route(API_GLOB, self.route)

    def route(self, route):
        endpoint = split_url(route)
        if not endpoint.startswith("console/"):
            return F.route(route)
        if endpoint == "console/log":
            cursor = parse_qs(urlsplit(route.request.url).query).get("cursor", [""])[0]
            self.log_requests.append(cursor)
        if not self.authorized:
            return route.fulfill(status=403, content_type="application/json", body=json.dumps({
                "ok": False, "authorized": False, "error": "仅管理员可操作",
            }))
        if endpoint == "console/preview":
            command = route.request.post_data_json["command"]
            self.previews.append(command)
            return fulfill(route, preview(command))
        if endpoint == "console/execute":
            command = route.request.post_data_json["command"]
            self.writes.append(command)
            payload = preview(command)
            for result in payload["results"]:
                state, raw = self.replies[result["command"]]
                result.update(state=state, state_label=LABELS[state], raw=raw, note="")
            return fulfill(route, payload)
        if endpoint == "console/log":
            return fulfill(route, {"ok": True, "authorized": True, "mode": "full", **self.log})
        raise AssertionError(f"未知终端路由：{endpoint}")

    def open(self, *, clock=False):
        if clock:
            self.page.clock.install()
        self.page.goto(PAGE_URL)
        self.page.locator('button.tab[data-page="console"]').click()
        expect(self.page.locator("#console_log_lines")).to_contain_text(self.log["lines"][0])

    def submit(self, value, *, keyboard=True):
        command = self.page.locator("#console_command")
        command.fill(value)
        expect(self.page.locator("#console_execute")).to_be_enabled()
        if keyboard:
            command.press("Enter")
        else:
            self.page.locator("#console_execute").click()


def check_multiline(scenario):
    scenario.open()
    page = scenario.page
    command = page.locator("#console_command")
    command.fill("list")
    command.press("Shift+Enter")
    command.press_sequentially("stop")
    preview_lines = page.locator("#console_warnings ol li code")
    expect(preview_lines, "T3 多行提示须按顺序逐条列出").to_have_text(["list", "stop"])
    assert scenario.writes == [], "T3 多行预览不能提前写入服务器"
    command.press("Enter")
    results = page.locator("#console_results .console-result")
    expect(results.locator("code"), "T3 回执须按顺序分别显示标题").to_have_text(["› list", "› stop"])
    expect(results.locator("pre"), "T3 回执须按顺序显示原文").to_have_text([ONLINE, STOP])
    assert scenario.writes == ["list\nstop"], "T3 Enter 只提交一批且原样保留换行"
    print("[PASS] T3 Shift+Enter 换行，预览与回执按行按序显示")


def check_response_states(scenario):
    scenario.open()
    scenario.submit("list\nwhiteelephant\ngive Alex stone\nseed", keyboard=False)
    results = scenario.page.locator("#console_results .console-result")
    expect(results, "T6 四条回执须分别渲染").to_have_count(4)
    assert [row.get_attribute("data-state") for row in results.all()] == list(LABELS),         "T6 四种后端状态须保持独立"
    expect(results.locator(".badge"), "T6 回执状态使用共享短标签").to_have_text(list(LABELS.values()))
    expect(results.nth(1).locator("pre")).to_have_text("Unknown command")
    expect(results.nth(2).locator("pre")).to_have_text("（空回执）")
    print("[PASS] T6 四态分别呈现，未知命令和空回执可见")


def check_polling(scenario):
    page = scenario.page
    # 在浏览器记录请求发起时刻，避免 Playwright 路由回调还没执行时漏计。
    page.add_init_script('''(() => {
        window.consoleLogRequestCount = 0;
        const fetchOriginal = window.fetch.bind(window);
        window.fetch = (url, options) => {
            if (String(url).includes("/console/log")) ++window.consoleLogRequestCount;
            return fetchOriginal(url, options);
        };
    })()''')
    scenario.open(clock=True)
    toggle = page.locator("#console_log_toggle")
    log_lines = page.locator("#console_log_lines")
    status = page.locator("#console_log_state")

    scenario.log["lines"] = ["轮询更新"]
    page.clock.fast_forward(3200)
    expect(log_lines, "T8 终端可见时须自动轮询更新").to_have_text("轮询更新")
    assert scenario.log_requests[:2] == ["", "first"], "T8 下一次请求带上上次的轮转游标"

    calls = page.evaluate("window.consoleLogRequestCount")
    toggle.click()
    expect(toggle, "T8 暂停状态须明确可见").to_have_attribute("aria-pressed", "true")
    scenario.log["lines"] = ["继续后的更新"]
    page.clock.fast_forward(9500)
    assert page.evaluate("window.consoleLogRequestCount") == calls, "T8 暂停期间不得轮询"
    expect(log_lines, "T8 暂停时日志不应变化").to_have_text("轮询更新")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(log_lines, "T8 继续后须恢复轮询").to_have_text("继续后的更新")

    scenario.log.update(state="unreachable", state_label=LABELS["unreachable"],
                        note="latest.log 缺失", lines=[])
    page.clock.fast_forward(3200)
    expect(status, "T8 文件缺失时显示后端状态").to_have_attribute("data-state", "unreachable")
    expect(page.locator("#console_log_detail")).to_have_text("latest.log 缺失")
    scenario.log.update(state="unrecognized", state_label=LABELS["unrecognized"],
                        note="日志已轮转", lines=["轮转后的内容"], cursor="second")
    page.clock.fast_forward(3200)
    expect(status, "T8 轮转须显示独立状态").to_have_attribute("data-state", "unrecognized")
    expect(log_lines).to_have_text("轮转后的内容")
    expect(page.locator("#console_log_detail")).to_have_text("日志已轮转")

    calls = page.evaluate("window.consoleLogRequestCount")
    page.locator('button.tab[data-page="overview"]').click()
    page.clock.fast_forward(9500)
    assert page.evaluate("window.consoleLogRequestCount") == calls, "T8 切出终端后不得继续轮询"
    print("[PASS] T8 自动轮询、游标、暂停恢复、缺失和轮转、切页停轮询")


def check_permission(scenario):
    scenario.open()
    scenario.submit("list")
    page = scenario.page
    expect(page.locator("#console_results")).to_contain_text(ONLINE)
    scenario.authorized = False
    toggle = page.locator("#console_log_toggle")
    toggle.click()
    toggle.click()
    expect(page.locator("#console_command"), "T9 失去权限后应禁用命令输入").to_be_disabled()
    expect(page.locator("#console_execute"), "T9 失去权限后应禁用提交").to_be_disabled()
    expect(page.locator("#console_results .console-result"), "T9 失去权限后清理当前回执").to_have_count(0)
    expect(page.locator("#console_log_lines"), "T9 失去权限后清理当前日志").not_to_contain_text("第一条日志")
    print("[PASS] T9 403 停用写入并清理回执和日志")


def check_private_literal_text(scenario):
    scenario.log["lines"] = [LITERAL]
    page = scenario.page
    page.goto(PAGE_URL)
    page.locator('button.tab[data-page="console"]').click()
    scenario.submit("say hello")
    for selector in ("#console_results .console-result pre", "#console_log_lines"):
        expect(page.locator(selector), "T10 服务端文本须按字面显示").to_contain_text(
            '<span id="console_injected">示例文字',
        )
    text = page.locator("#page_console").inner_text()
    # v0.28.0 反转：WebUI 是管理员专属页，界面**必须**显示真实地址（服主要靠它封 IP）。
    assert IPV4 in text, "T10 界面应显示真实 IPv4（管理员要靠它判断封哪个地址）"
    assert "[地址已隐藏]" not in text, "T10 界面不应再出现脱敏占位符"
    assert page.locator("#console_injected").count() == 0, "T10 回执和日志不能被当作 HTML"
    print("[PASS] T10 日志与回执均以文本呈现，并遮盖 IPv4 与 IPv6")


def check_notices(scenario):
    scenario.open()
    notice = scenario.page.locator("#console_notice")
    expect(notice, "T11 界面须说明 RCON 一问一答").to_contain_text("这不是实时终端（RCON 一问一答）")
    expect(notice, "T11 界面须说明命令立即执行").to_contain_text("命令会立即在服务端执行")
    print("[PASS] T11 非实时终端与立即执行的告知均可见")


def check_warnings_are_soft(scenario):
    scenario.open()
    page = scenario.page
    command = page.locator("#console_command")
    command.fill(f"{INDIRECT}\nlist")
    preview_area = page.locator("#console_warnings")
    expect(preview_area, "T2 间接执行须显示嵌套命令").to_contain_text("嵌套命令：stop")
    expect(preview_area.locator('[data-level="high"]').filter(has_text="@e[type=item]"),
           "T4 选择器参数及对应提示强度可见").to_have_count(1)
    expect(command, "T14 风险输入应具有警告样式").to_have_class("is-warning")
    expect(page.locator("#console_execute"), "T14 命中提示仍可直接执行").to_be_enabled()
    command.press("Enter")
    expect(page.locator("#console_results .console-result pre"), "T14 有提示的命令也要逐条显示回执").to_have_text(
        [STOP, ONLINE],
    )
    assert scenario.writes == [f"{INDIRECT}\nlist"], "T14 提示不得阻断或重复执行"
    print("[PASS] T2/T4/T14 嵌套命令、带参数选择器和多行提示可见并允许执行")


CASES = {
    "T3": check_multiline, "T6": check_response_states, "T8": check_polling,
    "T9": check_permission, "T10": check_private_literal_text, "T11": check_notices,
    "T14": check_warnings_are_soft,
}


def main(args):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**UI_LAUNCH_KWARGS)
        for key in args or CASES:
            page = browser.new_page(viewport=VIEWPORT, locale="zh-CN")
            try:
                scenario = Scenario(page)
                CASES[key](scenario)
                assert not scenario.errors, f"{key} 页面异常：{scenario.errors}"
            finally:
                page.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
