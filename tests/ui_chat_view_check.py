"""R1 玩家对话的过滤、文本渲染和阅读位置。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ui_theme_check as fixtures
from _paths import UI_LAUNCH_KWARGS
from _ui_page import API_GLOB, PAGE_URL, VIEWPORT, fulfill, split_url
from playwright.sync_api import expect, sync_playwright


def event(kind, player, detail, ts="08:30:15"):
    return {"type": kind, "player": player, "detail": detail, "ts": ts}


def check_chat_view(page):
    literal = "<script>这是聊天文本</script>"
    messages = [
        event("chat", "<Alex>", literal + " 192.0.2.9"),
        event("join", "Steve", "加入游戏"),
        event("chat", "Steve", "准备去下界"),
        event("leave", "Riley", "离开游戏"),
        event("chat", "Morgan", "补给在箱子里"),
    ]
    denied = False
    errors = []

    def route(request):
        if split_url(request) != "events/chat":
            return fixtures.route(request)
        if denied:
            return request.fulfill(status=403, json={"authorized": False})
        return fulfill(request, {"ok": True, "events": messages})

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(API_GLOB, route)
    page.goto(PAGE_URL)
    page.locator('button.tab[data-page="server"]').click()
    page.locator("#server_tab_tools").press("Enter")
    host = page.locator("#sv_chat")
    rows = host.locator(".server-chat-row")
    latest = page.locator("#sv_chat_latest")
    expect(rows, "R1 对话只显示玩家发言").to_have_count(3)
    expect(rows.locator("p")).to_have_text(["补给在箱子里", "准备去下界", literal + " 192.0.2.9"])
    expect(rows.last.locator("strong")).to_have_text("<Alex>")
    expect(host.locator("script, alex"), "R1 玩家原文必须以文本渲染").to_have_count(0)

    messages[:0] = [event("chat", "Steve", f"聊天记录 {n}") for n in range(14)]
    messages[:0] = [event("me", "Alex", "挥手"), event("whisper", "Steve", "私聊内容")]
    expect(rows).to_have_count(19)
    expect(latest).to_be_disabled()
    assert host.evaluate("el => el.scrollTop > 0"), "R1 新消息到达时默认跟随底部"
    host.evaluate("el => { el.scrollTop = 44; }")
    expect(latest).to_be_enabled()
    before = host.evaluate("el => el.scrollTop")
    messages.insert(0, event("chat", "Steve", "新到达的消息"))
    expect(rows.last.locator("p")).to_have_text("新到达的消息")
    assert abs(host.evaluate("el => el.scrollTop") - before) <= 1, "R1 上滚后新消息不能改变阅读位置"
    latest.press("Enter")
    expect(latest).to_be_disabled()
    assert host.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop < 8")

    denied = True
    expect(page.locator("#sv_chat_notice")).to_have_text("当前会话无权查看对话与广播")
    expect(rows).to_have_count(0)
    assert not errors, f"R1 页面异常：{errors}"
    print("[PASS] R1 三条聊天、文本转义、私聊和动作、上滚暂停、键盘回到最新及权限撤回")


def check_broadcast_history(page):
    messages = [event("chat", "Alex", "收到")]

    def route(request):
        endpoint = split_url(request)
        if endpoint == "events/chat":
            return fulfill(request, {"ok": True, "events": messages})
        if endpoint != "server/broadcast":
            return fixtures.route(request)
        data = request.request.post_data_json
        mode, text = data["mode"], data["message"]
        if text == "失败样本":
            return fulfill(request, {"ok": False, "error": "服务端拒绝"})
        confirmed = mode != "task"
        sender = "任务助手" if mode == "task" else "Server"
        messages.insert(0, {**event("broadcast", sender, text), "mode": mode, "confirmed": confirmed})
        return fulfill(request, {"ok": True, "mode": mode, "message": text, "confirmed": confirmed})

    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(API_GLOB, route)
    start = datetime(2026, 11, 21, tzinfo=timezone.utc)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(seconds=1))
    page.goto(PAGE_URL)
    page.locator('button.tab[data-page="server"]').click()
    page.locator("#server_tab_tools").click()
    rows = page.locator("#sv_chat .server-chat-row")
    expect(rows).to_have_count(1)
    samples = (
        ("chat", "<这里集合>", "Server · 广播（聊天栏）"),
        ("title", "马上开始", "Server · 广播（全屏标题）"),
        ("task", "已完成", "任务助手 · 广播（模拟任务输出） · 未确认"),
    )
    for mode, text, label in samples:
        page.locator(f'#bc_modes [data-mode="{mode}"]').click()
        page.locator("#bc_msg").fill(text)
        page.locator("#bc_send").click()
        expect(rows.last.locator("p")).to_have_text(text)
        expect(rows.last.locator("strong")).to_have_text(label)

    page.locator("#bc_msg").fill("失败样本")
    page.locator("#bc_send").click()
    expect(page.locator("#bc_out")).to_contain_text("服务端拒绝")
    with page.expect_response("**/page/events/chat"):
        page.clock.run_for(2000)
    expect(rows.locator("p")).to_have_text(["收到", *[sample[1] for sample in samples]])

    page.reload()
    page.locator('button.tab[data-page="server"]').click()
    page.locator("#server_tab_tools").click()
    expect(rows).to_have_count(4)
    assert not errors, errors
    print("[PASS] 三种广播即时进入时间线，未确认有标记，失败不入列，轮询和重载不重复")


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**UI_LAUNCH_KWARGS)
        try:
            check_chat_view(browser.new_page(viewport=VIEWPORT))
            check_broadcast_history(browser.new_page(viewport=VIEWPORT, reduced_motion="reduce"))
        finally:
            browser.close()
