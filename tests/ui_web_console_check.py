"""控制台浏览器硬门禁，可按判据号单独运行以验证生产代码变异。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from _ui_page import API_GLOB, PAGE_URL, VIEWPORT, fulfill, split_url
from playwright.sync_api import expect, sync_playwright


ONLINE = "There are 1 of a max of 20 players online: Alex"
STOP = "Stopping the server"
INDIRECT = "execute as @a at @e[type=item] run stop"
THREE_WARNINGS = "execute as @a run stop"
WARNING_TEXTS = [
    "嵌套命令：stop",
    "stop 会关闭服务器，所有在线玩家将断线。",
    "@a 作用范围：所有匹配的在线玩家。",
]
NARROW_VIEWPORT = {"width": 390, "height": 844}
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
                warning("indirect", WARNING_TEXTS[0]),
                warning("service", WARNING_TEXTS[1]),
                warning("selector_a", WARNING_TEXTS[2]),
            ] if line == THREE_WARNINGS else [
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
        self.poll_interval = "1000"
        self.poll_saves = []
        self.hold_preview_for = None
        self.held_preview = None
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
        if endpoint == "settings/console-log":
            return fulfill(route, {"ok": True, "interval": self.poll_interval})
        if endpoint == "settings/console-log/save":
            self.poll_interval = route.request.post_data_json["interval"]
            self.poll_saves.append(self.poll_interval)
            return fulfill(route, {"ok": True, "interval": self.poll_interval})
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
            if command == self.hold_preview_for:
                self.held_preview = route
                return
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

    toggle.click()
    expect(toggle, "T8 暂停状态须明确可见").to_have_attribute("aria-pressed", "true")
    # 基线必须在「暂停已生效」之后取：』点击前恰好触发的那一次轮询属于暂停前，
    # 若算进「暂停期间」就会在慢机器上偶发失败（门禁里复现过一次）。
    page.wait_for_timeout(120)
    calls = page.evaluate("window.consoleLogRequestCount")
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


def check_debounced_preview(scenario):
    scenario.open()
    page = scenario.page
    input_box = page.locator("#console_command")
    input_box.evaluate("""input => {
        for (const char of "0123456789") {
            input.value += char;
            input.dispatchEvent(new Event("input", {bubbles:true}));
        }
    }""")
    page.wait_for_timeout(500)
    assert scenario.previews == ["0123456789"], \
        f"R2 连续输入十字符只发一次预览，实际为 {scenario.previews!r}"

    scenario.hold_preview_for = INDIRECT
    input_box.evaluate("""(input, command) => {
        input.value = command;
        input.dispatchEvent(new Event("input", {bubbles:true}));
        input.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true}));
    }""", INDIRECT)
    expect(page.locator("#console_results")).to_contain_text(STOP)
    assert scenario.writes == [INDIRECT], "R2 预览未返回时回车也只提交一次"
    assert scenario.previews == ["0123456789", INDIRECT] and scenario.held_preview is not None, \
        "R2 回车应立即请求当前文本的预览"
    expect(page.locator("#console_warnings")).to_contain_text("stop 会关闭服务器")
    fulfill(scenario.held_preview, preview(INDIRECT))
    print("[PASS] R2 十次输入触发一次预览，回车立即预览且不等待预览回执")


def check_configured_polling(scenario):
    page = scenario.page
    warnings = []
    page.on("console", lambda message: warnings.append(message.text) if message.type == "warning" else None)
    start = datetime(2026, 11, 21, tzinfo=timezone.utc)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(seconds=1))
    page.add_init_script("""(() => {
        window.logFetchTimes = [];
        const fetchOriginal = window.fetch.bind(window);
        window.fetch = (url, options) => {
            if (String(url).includes("/console/log")) window.logFetchTimes.push(Date.now());
            return fetchOriginal(url, options);
        };
    })()""")
    scenario.open()
    page.locator("#console_poll_jump").press("Enter")
    page.clock.run_for(32)
    select = page.locator("#cfg_console_poll")
    expect(select, "R4 终端跳转应聚焦设置页刷新间隔").to_be_focused()

    def check_interval(interval, assertion):
        scenario.log["lines"] = [f"测试间隔 {interval}"]
        page.evaluate("window.logFetchTimes = []")
        page.locator('button.tab[data-page="console"]').click()
        expect(page.locator("#console_log_lines")).to_have_text(f"测试间隔 {interval}")
        page.clock.run_for(interval - 1)
        assert page.evaluate("window.logFetchTimes.length") == 1, assertion
        page.clock.run_for(1)
        page.wait_for_function("window.logFetchTimes.length === 2")
        first, second = page.evaluate("window.logFetchTimes")
        assert second - first == interval, assertion
        expect(page.locator("#console_poll_label")).to_have_text(f"刷新间隔 {interval / 1000:g} 秒")
        print(f"[PASS] R4 实际发出两次日志请求，间隔 {second - first} ms")

    for interval in (1000, 1500, 3000):
        expect(page.locator("#page_settings")).to_be_visible()
        # 产品行为：值没变化时「保存日志间隔」是禁用的（合理）。
        # 第一次循环选的 1000 恰好等于默认值 → 没有 change → 按钮禁用 → 点不动。
        # 所以先切到另一个档位制造真实变化，再选目标值。
        other = "3000" if interval != 3000 else "1500"
        select.select_option(other)
        select.select_option(str(interval))
        expect(page.locator("#cfg_console_poll_save"),
               "R4 改过档位后保存按钮须可用").to_be_enabled()
        page.locator("#cfg_console_poll_save").click()
        expect(page.locator("#cfg_console_poll_notice")).to_have_text("刷新间隔已保存")
        check_interval(interval, f"R4 日志轮询应使用已保存的 {interval} ms 档位")
        page.locator("#console_poll_jump").click()
    assert scenario.poll_saves == ["1000", "1500", "3000"]
    scenario.poll_interval = "300"
    check_interval(1000, "R4 非法的刷新间隔须回退到 1000 ms")
    assert sum("已回退到 1000 ms" in message for message in warnings) == 1, "R4 回退须记录一次警告"
    print("[PASS] R4 300 ms 回退到 1000 ms，记录一次警告")


def check_permission(scenario):
    scenario.open()
    scenario.submit("list")
    page = scenario.page
    expect(page.locator("#console_results")).to_contain_text(ONLINE)
    scenario.authorized = False
    toggle = page.locator("#console_log_toggle")
    # 产品行为：收到 403 后会**禁用**日志开关（index.html 里 `console_log_toggle.disabled = true`）。
    # 原来这里点两次 toggle —— 但按钮可能已经禁用，于是 Chromium 下偶发
    # `element is not enabled`（Edge 慢一点恰好躲过）。改成断言"被禁用"：
    # 既不点禁用控件，也比原来多守一条（开关也停用）。
    expect(toggle, "T9 失去权限后日志开关应禁用").to_be_disabled()
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


def check_beta_tag(scenario):
    """v0.33.1：终端页必须挂「Beta 实验性页面」明示标签。"""
    scenario.open()          # 每个用例自己负责打开页面（与其它 check 一致）
    page = scenario.page
    tag = page.locator("#console_beta_tag")
    assert tag.count() == 1, "终端页应有一个 beta 标签"
    text = tag.inner_text()
    assert "Beta" in text and "实验性" in text, f"标签文案不对：{text!r}"


def check_warning_layout(scenario):
    """按同一判据检查两种尺寸与主题，预览请求由假后端处理。"""
    scenario.open()
    page = scenario.page
    command = page.locator("#console_command")
    execute = page.locator("#console_execute")
    area = page.locator("#console_warnings")
    items = area.locator("[data-level]")
    viewport = page.viewport_size
    theme = page.locator("html").get_attribute("data-theme")
    # 入场动画会改变卡片的视口坐标。等它结束再记录基线。
    page.locator("#page_console").evaluate("""el => Promise.all(el.getAnimations({subtree: true})
        .filter(animation => Number.isFinite(animation.effect.getComputedTiming().endTime))
        .map(animation => animation.finished))""")

    def layout():
        return page.evaluate("""() => {
            const bounds = el => {
                const box = el.getBoundingClientRect();
                return [box.x + scrollX, box.y + scrollY, box.width, box.height];
            };
            const command = document.getElementById("console_command");
            return {
                inputTop: [command.offsetTop],
                execute: bounds(document.getElementById("console_execute")),
                warnings: bounds(document.getElementById("console_warnings")),
                cards: [...document.querySelectorAll("#page_console .card")].flatMap(bounds),
            };
        }""")

    before = layout()
    command.fill(THREE_WARNINGS)
    expect(items, "R9 必须呈现三条完整警告").to_have_text(WARNING_TEXTS)
    warnings = items.all()
    expect(area, "R9 警告区须供读屏播报").to_have_attribute("role", "status")
    assert "console_warnings" in command.get_attribute("aria-describedby").split(), \
        "R9 命令输入须关联警告说明"
    for item in warnings:
        expect(item, "R9 每条警告须可见").to_be_visible()
        assert item.evaluate("el => el.checkVisibility({opacityProperty:true, visibilityProperty:true})"), \
            "R9 每条警告须可见"

    height = area.evaluate("""el => ({
        scroll: el.scrollHeight, client: el.clientHeight, overflow: getComputedStyle(el).overflowY
    })""")
    assert height["overflow"] == "visible" and height["scroll"] <= height["client"] + 1, \
        f"R8 警告区不得内滚或裁剪：{height}"
    assert page.evaluate("scrollY === 0"), "R8 警告须在页面首屏可见"
    box = area.bounding_box()
    assert box and box["x"] >= -1 and box["y"] >= -1 \
        and box["x"] + box["width"] <= viewport["width"] + 1 \
        and box["y"] + box["height"] <= viewport["height"] + 1, \
        f"R8 警告区须在首屏完整可见：{box} / {viewport}"
    for control in (command, execute):
        bounds = control.bounding_box()
        assert bounds and bounds["y"] + bounds["height"] <= box["y"] + 1, \
            "R8 警告区不得盖住输入框或执行按钮"
    for item in warnings:
        bounds = item.bounding_box()
        assert bounds and bounds["x"] >= box["x"] - 1 and bounds["y"] >= box["y"] - 1 \
            and bounds["x"] + bounds["width"] <= box["x"] + box["width"] + 1 \
            and bounds["y"] + bounds["height"] <= box["y"] + box["height"] + 1, \
            f"R8 单条警告须完整落在警告区内：{bounds} / {box}"

    visible = layout()
    command.press("Tab")
    expect(execute, "R9 Tab 须能从输入框进入执行按钮").to_be_focused()
    execute.press("Shift+Tab")
    expect(command, "R9 Shift+Tab 须能回到输入框").to_be_focused()
    command.fill("")
    expect(area).to_be_empty()
    for state in (visible, layout()):
        for part, positions in before.items():
            assert all(abs(a - b) <= 1 for a, b in zip(positions, state[part], strict=True)), \
                f"R7 警告出现或清空时布局移动：{part} {positions} → {state[part]}"
    print(f"[PASS] R7/R8/R9 布局不跳、三条警告首屏完整显示、键盘可达 "
          f"({theme} {viewport['width']}×{viewport['height']}，警告区 {height['scroll']}/{height['client']}px)")
    if viewport == NARROW_VIEWPORT:
        print("[PASS] R10 窄屏通过相同判据")


def check_warning_motion(scenario):
    scenario.open()
    page = scenario.page
    page.emulate_media(reduced_motion="no-preference")
    command = page.locator("#console_command")
    area = page.locator("#console_warnings")
    text = area.locator(".console-warning-text")
    outgoing = page.locator(".console-warning-exit")
    # 暂停短动画，让断言无需抢在动画结束前读取状态。
    area.evaluate("""el => {
        const slot = el.parentElement;
        new MutationObserver(() => {
            for (const animation of slot.getAnimations({subtree:true})) {
                if (animation.playState === "running") animation.pause();
            }
        }).observe(slot, {childList:true, subtree:true});
    }""")

    command.fill(THREE_WARNINGS)
    expect(area.locator("[data-level]")).to_have_text(WARNING_TEXTS)
    assert text.evaluate("""el => {
        const [animation] = el.getAnimations();
        const frames = animation?.effect.getKeyframes();
        return frames && +frames[0].opacity < +frames.at(-1).opacity;
    }"""), "警告出现时应淡入"

    command.fill("")
    expect(area).to_be_empty()
    expect(outgoing).to_have_attribute("aria-hidden", "true")
    assert outgoing.evaluate("""el => {
        const [animation] = el.getAnimations({subtree:true});
        const frames = animation?.effect.getKeyframes();
        return frames && +frames[0].opacity > 0 && +frames.at(-1).opacity === 0;
    }"""), "退出中的文字应淡出"

    command.fill(THREE_WARNINGS)
    expect(area.locator("[data-level]")).to_have_text(WARNING_TEXTS)
    expect(outgoing).to_have_count(0)
    command.fill("")
    outgoing.evaluate("el => el.getAnimations({subtree:true})[0].finish()")
    expect(outgoing).to_have_count(0)

    page.emulate_media(reduced_motion="reduce")
    command.fill(THREE_WARNINGS)
    expect(area.locator("[data-level]")).to_have_text(WARNING_TEXTS)
    assert text.evaluate("el => el.getAnimations().length") == 0, "减少动态时须直接显示警告"
    command.fill("")
    expect(area).to_be_empty()
    expect(outgoing).to_have_count(0)
    print("[PASS] 终端警告淡入淡出，退出文字不重复播报，支持减少动态")


CASES = {
    "T3": check_multiline, "T6": check_response_states, "T8": check_polling,
    "T9": check_permission, "T10": check_private_literal_text, "T11": check_notices,
    "T14": check_warnings_are_soft, "R2": check_debounced_preview, "R4": check_configured_polling,
    "BETA": check_beta_tag, "R7": check_warning_layout, "R10": check_warning_layout,
    "MOTION": check_warning_motion,
}


def main(args):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**UI_LAUNCH_KWARGS)
        for arg in args or CASES:
            key = {"R8": "R7", "R9": "R7"}.get(arg, arg)
            viewport = NARROW_VIEWPORT if key == "R10" else VIEWPORT
            themes = ("dark", "light") if CASES[key] is check_warning_layout else ("dark",)
            for theme in themes:
                page = browser.new_page(viewport=viewport, locale="zh-CN", color_scheme=theme)
                try:
                    scenario = Scenario(page)
                    CASES[key](scenario)
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    assert not scenario.errors, f"{key} 页面异常：{scenario.errors}"
                finally:
                    page.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
