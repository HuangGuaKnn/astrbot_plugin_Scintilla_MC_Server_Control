"""玩家管理浏览器硬门禁。可传判据号单独跑，供变异脚本核对失败位置。"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import parse_qs, urlsplit

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from _ui_page import API_GLOB, PAGE_URL, VIEWPORT, fulfill, split_url
from playwright.sync_api import sync_playwright

ONLINE_OK = {"state": "ok", "players": ["Alex"], "online": 1, "max": 20}
BAN = {
    "name": "Steve", "reason": "违反服务器守则", "created": "2026-11-17 00:00:00 +0000",
    "source": "Server", "expires": "forever", "detail_available": True,
    # v0.26.1：解封按钮的启用依据从「整卡 state」改成「这一条能不能解」——
    # 回包粘连时名单来自文件，每一条都是确定的，所以逐条可解。
    "removable": True,
}
IPV4 = "192.0.2.42"
IPV6 = "2001:db8::1234"


class Scenario:
    def __init__(self, page):
        self.page = page
        self.authorized = True
        self.online = dict(ONLINE_OK)
        self.bans = {"state": "ok", "items": [dict(BAN)]}
        self.posts = []
        self.audits = []
        self.audit_limits = []
        self.apply_unban = False
        self.reject_write = None
        self.event_detail = "加入游戏"
        self.status_note = "在线人数暂不可用"
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route(API_GLOB, self.route)

    def route(self, route_obj):
        endpoint = split_url(route_obj)
        if endpoint == "server/status":
            # 伪造一份旧快照。管理按钮必须以来自已授权路由的名单为准。
            fulfill(route_obj, {
                "ok": True,
                "list": {"online": 1, "max": 20, "players": ["StaleUser"]},
                "card": {"overall": "unrecognized", "fields": [{
                    "key": "online", "label": "在线人数", "state": "unrecognized",
                    "note": self.status_note, "raw": self.status_note,
                }]},
            })
        elif endpoint == "events":
            fulfill(route_obj, {
                "ok": True, "events": [{
                    "type": "join", "player": "Alex", "ts": "08:30:15", "detail": self.event_detail,
                }],
            })
        elif endpoint.startswith("player/") and not self.authorized:
            route_obj.fulfill(status=403, content_type="application/json",
                              body=json.dumps({"ok": False, "error": "仅管理员可操作"}))
        elif endpoint == "player/bans":
            fulfill(route_obj, {"ok": True, "authorized": True, "online": self.online, **self.bans})
        elif endpoint == "player/audit":
            self.audit_limits.append(parse_qs(urlsplit(route_obj.request.url).query).get("limit"))
            fulfill(route_obj, {"ok": True, "items": self.audits})
        elif endpoint == "player/action":
            body = route_obj.request.post_data_json
            self.posts.append(body)
            action, player = body["action"], body["player"]
            command = f"{'pardon' if action == 'unban' else action} {player}"
            if body.get("preview"):
                fulfill(route_obj, {"ok": True, "action": action, "player": player, "command": command})
                return
            if self.reject_write:
                fulfill(route_obj, self.reject_write)
                return
            if self.apply_unban:
                self.bans = {"state": "empty", "items": []}
            present = any(item["name"] == player for item in self.bans["items"])
            result = {
                # 即使传输层 ok，仍须读 confirmed，名单还在不能涂绿。
                "ok": True, "action": action, "player": player, "command": command,
                "state": "unrecognized" if present else "ok", "confirmed": not present,
                "note": "已发出但未确认" if present else "回读已确认",
                "raw": f"Unbanned {player}",
                "readback": {
                    "command": "banlist players", "state": "ok",
                    "raw": f"There are 1 ban(s): {player}" if present else "There are no bans",
                },
            }
            self.audits = [{
                "ts": "2026-11-17T08:30:15+08:00", "actor": "dashboard-owner", **result,
            }]
            fulfill(route_obj, result)
        else:
            F.route(route_obj)

    def open(self):
        self.page.goto(PAGE_URL)
        self.page.locator('button.tab[data-page="server"]').click()
        self.page.wait_for_function(
            """() => !document.getElementById("sv_refresh").disabled &&
              document.querySelector("#sv_players .ply, #sv_players .player-state")"""
        )

    def refresh(self):
        self.page.locator("#sv_refresh").click()
        self.page.wait_for_function('!document.getElementById("sv_refresh").disabled')

def check_four_states(scenario):
    page = scenario.page
    scenario.open()
    online = page.locator("#sv_players")
    assert "Alex" in online.inner_text() and "StaleUser" not in online.inner_text(), \
        "P1 正常名单须用已守卫的在线结果"
    for state, note, raw, expected in (
        ("empty", "", "", "当前没有玩家在线"),
        ("unrecognized", "格式未知", "list reply in an unfamiliar format", "玩家名单拿不到"),
        ("unreachable", "RCON 连接失败", "", "玩家名单拿不到"),
        ("unsupported", "命令不存在", "Unknown command", "玩家名单拿不到"),
    ):
        scenario.online = {"state": state, "note": note, "raw": raw, "players": []}
        scenario.refresh()
        actual = online.inner_text()
        assert expected in actual and note in actual and raw in actual, f"P1 名单状态和原文缺失：{state}"
        assert "Alex" not in actual and "StaleUser" not in actual, f"P1 名单有过时数据：{state}"
    print("[PASS] P1 名单正常、空服、认不出、连不上和命令不可用分别显示")


def check_permission(scenario):
    scenario.open()
    page = scenario.page
    assert page.locator("#sv_players button[data-action]").count() == 5, "权限用例前置条件：管理员可用五项操作"
    scenario.authorized = False
    scenario.refresh()
    assert page.locator("#sv_players button[data-action], #sv_bans button[data-action]").count() == 0, \
        "P6 非管理员仍有玩家管理按钮"
    assert page.locator("#sv_ban_card").is_hidden() and page.locator("#sv_audit_card").is_hidden(), \
        "P6 非管理员仍看得到封禁名单或审计卡"
    assert page.locator("#sv_bans .player-row, #sv_audit .player-row").count() == 0, \
        "P6 降权后管理数据仍留在 DOM"
    print("[PASS] P6 管理员可操作，接口拒绝后移除管理入口和数据")


def check_disabled(scenario):
    scenario.open()
    page = scenario.page
    for state, extra in (
        ("empty", {"players": [], "online": 0}),
        ("unrecognized", {"raw": "unfamiliar list reply"}),
        ("unreachable", {"note": "RCON 连接失败"}),
        ("ok", {"players": ["Alex"], "online": 2}),
    ):
        scenario.online = {"state": state, **extra}
        scenario.refresh()
        buttons = page.locator("#sv_players button[data-action]").all()
        assert len(buttons) == 5 and all(button.is_disabled() for button in buttons), \
            f"P8 名单不可用时在线玩家操作没有全部 disabled：{state}"
    for state in ("unrecognized", "unreachable"):
        scenario.bans = {"state": state, "note": "封禁名单拿不到", "raw": "unfamiliar ban reply", "items": []}
        scenario.refresh()
        assert page.locator('#sv_bans button[data-action="unban"]').is_disabled(), \
            f"P8 封禁名单不可用时解封按钮没有 disabled：{state}"
        assert "unfamiliar ban reply" in page.locator("#sv_bans").inner_text(), \
            f"P8 封禁名单不可用时未显示原文：{state}"
    assert not scenario.posts, "P8 不可用状态不应发执行请求"
    print("[PASS] P8 空服、回包异常、连不上和名单不完整时操作禁用")


def check_no_ip(scenario):
    scenario.bans["items"][0]["reason"] = f"违规原因 {IPV4} / {IPV6}"
    scenario.event_detail = f"事件文本 {IPV4} / {IPV6}"
    scenario.status_note = f"异常原文 {IPV4} / {IPV6}"
    scenario.open()
    page = scenario.page
    page.locator("#sv_events .ev").wait_for()
    assert "违规原因" in page.locator("#sv_bans").inner_text(), "P9 隐私样本未经过封禁卡渲染"
    # v0.28.0 反转：WebUI 是管理员专属页面，**界面必须显示真实地址** ——
    # 服主要靠它判断 ban-ip 封哪个地址；脱敏只保留在审计文件（_redact 那一份）里。
    dom = page.locator("#page_server").evaluate("el => el.outerHTML")
    assert IPV4 in dom, "P9 封禁原因里的真实 IPv4 应显示在界面"
    assert "地址已隐藏" not in dom, "P9 界面不应再出现脱敏占位符"
    print("[PASS] P9 WebUI 显示真实地址（审计仍脱敏，见后端用例）")


def submit(scenario, button, player, command):
    page = scenario.page
    button.click()
    page.locator("#player_dialog[open]").wait_for()
    page.wait_for_function('command => document.getElementById("player_command").textContent === command',
                           arg=command)
    assert page.locator("#player_target").inner_text() == player, "确认弹窗缺少目标玩家"
    assert scenario.posts[-1].get("preview") is True, "弹窗打开时只能请求预览"
    before = sum(not post.get("preview") for post in scenario.posts)
    page.locator("#player_confirm").click()
    page.locator("#player_dialog").wait_for(state="hidden")
    page.wait_for_function('!document.getElementById("sv_refresh").disabled')
    writes = [post for post in scenario.posts if not post.get("preview")]
    assert len(writes) == before + 1 and writes[-1]["expected_command"] == command, \
        "确认后须执行且只执行一次所展示的命令"
    return writes[-1]


def check_rejection(scenario):
    scenario.open()
    page = scenario.page
    result = page.locator("#sv_player_result")
    for raw, state, color in (
        ("Unknown command", "unsupported", "amb"),
        ("No player was found", "unrecognized", "warn"),
    ):
        scenario.reject_write = {"ok": False, "state": state, "confirmed": False, "raw": raw}
        submit(scenario, page.locator('#sv_players button[data-action="kick"]'), "Alex", "kick Alex")
        assert result.locator(".badge.ok").count() == 0 and result.locator(f".badge.{color}").count() == 1, \
            "P10 拒绝回执显示成成功或没有对应色标"
        assert raw in result.inner_text(), "P10 拒绝回执没有显示原文"
    print("[PASS] P10 Unknown command / No player was found 提示均为非绿并带原文")


def check_ban_rendering(scenario):
    scenario.bans["items"] += [
        {"name": "RemoteUser", "reason": "未知", "created": "未知",
         "source": "未知", "expires": "未知", "detail_available": False},
        {**BAN, "name": "Expired", "expires": "2000-01-01 00:00:00 +0000"},
    ]
    scenario.bans.update(detail_note="未知（server_dir 未配置，读不到 banned-players.json）",
                         mismatch_count=2)
    scenario.open()
    page = scenario.page
    card = page.locator("#sv_ban_card")
    assert card.is_visible() and card.locator("h3").inner_text() == "封禁名单", "P11 缺独立封禁名单卡"
    rows = card.locator(".player-row")
    assert rows.count() == 3 and "永久" in rows.nth(0).inner_text(), "P11 永久封禁行没有正确显示"
    assert rows.nth(1).inner_text().count("未知") == 4, "P11 异地模式详情未逐字段标明未知"
    assert "08:00:00" in rows.nth(2).inner_text() and "已过期" in rows.nth(2).inner_text(), \
        "P11 具体到期时间须转换为本地时间并标已过期"
    assert "server_dir 未配置" in card.inner_text(), "P11 缺详情时没有原因说明"
    assert "与文件记录不一致（2 条）" in card.inner_text(), "P11 未显示文件差异条数"
    scenario.bans = {"state": "empty", "items": []}
    scenario.refresh()
    assert "当前没有封禁记录" in card.inner_text(), "P11 合法空名单文案缺失"
    print("[PASS] P11 永久、本地到期和过期显示准确，缺详情及文件差异有说明")


def check_unban(scenario):
    scenario.open()
    page = scenario.page
    unban = page.locator('#sv_bans button[data-action="unban"][data-player="Steve"]')
    result = page.locator("#sv_ban_result")
    submitted = submit(scenario, unban, "Steve", "pardon Steve")
    assert submitted["action"] == "unban" and submitted["player"] == "Steve", \
        "P12 封禁卡未使用通用解封入口"
    assert result.locator(".badge.ok").count() == 0 and "未确认" in result.inner_text(), \
        "P12 未确认的解封被报为成功"
    assert "There are 1 ban(s): Steve" in result.inner_text() and unban.is_visible(), \
        "P12 回读含目标玩家时须显示原文并保留行"
    scenario.apply_unban = True
    submit(scenario, unban, "Steve", "pardon Steve")
    assert result.locator(".badge.ok").count() == 1 and "已确认" in result.inner_text(), \
        "P12 回读目标消失后应显示已确认"
    assert unban.count() == 0 and "当前没有封禁记录" in page.locator("#sv_bans").inner_text(), \
        "P12 解封确认后没有刷新名单"
    assert "pardon Steve" in page.locator("#sv_audit").inner_text() and scenario.audit_limits[-1] == ["20"], \
        "P12 结果未刷新最近操作或缺少 limit=20"
    print("[PASS] P12 封禁卡解封预览、提交、未确认与已确认、审计和回读刷新")


CASES = {"P1": check_four_states, "P6": check_permission, "P8": check_disabled,
         "P9": check_no_ip, "P10": check_rejection, "P11": check_ban_rendering, "P12": check_unban}


def main(args):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        for key in args or CASES:
            page = browser.new_page(viewport=VIEWPORT, timezone_id="Asia/Shanghai", locale="zh-CN")
            try:
                scenario = Scenario(page)
                CASES[key](scenario)
                assert not scenario.errors, f"{key} 页面异常：{scenario.errors}"
            finally:
                page.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
