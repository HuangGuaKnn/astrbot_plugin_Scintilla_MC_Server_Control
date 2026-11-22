"""玩家管理浏览器硬门禁。可传判据号单独跑，供变异脚本核对失败位置。"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import parse_qs, urlsplit

import ui_theme_check as F
from _paths import UI_LAUNCH_KWARGS
from _ui_page import API_GLOB, PAGE_URL, VIEWPORT, fulfill, split_url
from playwright.sync_api import expect, sync_playwright

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
        self.ip_searches = []
        self.ip_search_result = {}
        self.audits = []
        self.audit_limits = []
        self.apply_unban = False
        self.reject_write = None
        self.event_detail = "加入游戏"
        self.status_note = "在线人数暂不可用"
        self.color_config = None
        self.color_load_ok = True
        self.color_saves = []
        self.settings_saves = []
        self.workflow_config = None
        self.workflow_logs = []
        self.workflow_reject = False
        self.workflow_hold = False
        self.workflow_held = []
        self.settings_reads = 0
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route(API_GLOB, self.route)

    def route(self, route_obj):
        endpoint = split_url(route_obj)
        if endpoint == "workflow/status" and self.workflow_config is not None:
            payload = F.workflow_status()
            payload.update(enabled=self.workflow_config["agent_workflow_enabled"],
                           tool_enabled=self.workflow_config["enable_mc_workflow"],
                           tool_setting_enabled=self.workflow_config["enable_mc_workflow"],
                           master_enabled=True, logs=self.workflow_logs)
            fulfill(route_obj, payload)
        elif endpoint == "settings" and self.workflow_config is not None:
            self.settings_reads += 1
            payload = F.settings()
            payload["settings"].update(self.workflow_config)
            fulfill(route_obj, payload)
        elif endpoint == "settings/save" and self.workflow_config is not None:
            body = route_obj.request.post_data_json["settings"]
            self.settings_saves.append(body)
            if self.workflow_hold:
                self.workflow_held.append((route_obj, body))
            else:
                self.finish_workflow_save(route_obj, body)
        elif endpoint == "colors" and self.color_config is not None:
            fulfill(route_obj, self.colors_payload() if self.color_load_ok else {"ok": False, "error": "颜色配置暂不可用"})
        elif endpoint == "settings" and self.color_config is not None:
            payload = F.settings()
            payload["settings"].update(self.color_config)
            fulfill(route_obj, payload)
        elif endpoint == "colors/save" and self.color_config is not None:
            body = route_obj.request.post_data_json
            self.color_saves.append(body)
            self.color_config.update(body)
            fulfill(route_obj, self.colors_payload())
        elif endpoint == "settings/save" and self.color_config is not None:
            body = route_obj.request.post_data_json["settings"]
            self.settings_saves.append(body)
            self.color_config.update(body)
            fulfill(route_obj, {"ok": True, "applied": body, "ignored": []})
        elif endpoint == "server/status":
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
        elif endpoint == "player/ip/search":
            self.ip_searches.append(route_obj.request.post_data_json)
            fulfill(route_obj, {"ok": True, "details": self.ip_search_result})
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

    def finish_workflow_save(self, route_obj, body):
        if self.workflow_reject:
            fulfill(route_obj, {"ok": False, "error": "模拟保存失败"})
        else:
            self.workflow_config.update(body)
            fulfill(route_obj, {"ok": True, "applied": body, "ignored": [], "config_saved": True})

    def colors_payload(self):
        cfg = self.color_config
        return {"ok": True,
                "colors": {key: cfg[key] for key in ("color_say", "color_title", "color_feedback")},
                "gradient": {"enabled": cfg["gradient_enabled"], "format": cfg["gradient_format"],
                             "stops": {key: cfg[key] for key in (
                                 "gradient_colors_say", "gradient_colors_title", "gradient_colors_feedback")}},
                "plain": {"format": cfg["plain_color_format"]}}

    def open(self):
        self.page.goto(PAGE_URL)
        self.page.locator('button.tab[data-page="server"]').click()
        self.select("players")
        self.page.wait_for_function(
            """() => !document.getElementById("sv_players_refresh").disabled &&
              document.querySelector("#sv_players .ply, #sv_players .player-state")"""
        )

    def select(self, category):
        self.page.locator(f'[data-server-tab="{category}"]').click()

    def refresh(self):
        refresh = self.page.locator("#sv_players_refresh")
        refresh.click()
        expect(refresh).to_be_enabled()

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
    assert page.locator("#sv_players button[data-action]").count() == 4, "权限用例前置条件：管理员可用四项操作"
    scenario.authorized = False
    scenario.refresh()
    assert page.locator("#sv_players button[data-action], #sv_bans button[data-action]").count() == 0, \
        "P6 非管理员仍有玩家管理按钮"
    expect(page.locator("#sv_ban_card")).to_be_hidden()
    scenario.select("audit")
    expect(page.locator("#sv_audit_card")).to_be_hidden()
    audit_unavailable = page.locator("#sv_audit_unavailable")
    expect(audit_unavailable).to_be_visible()
    expect(audit_unavailable).to_have_text("当前会话无权查看操作记录")
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
        assert len(buttons) == 4 and all(button.is_disabled() for button in buttons), \
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
    scenario.select("monitor")
    page.locator("#sv_events .ev").wait_for()
    scenario.select("players")
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
    expect(page.locator("#sv_players_refresh")).to_be_enabled()
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
    audit = page.locator("#sv_audit")
    expect(audit).to_contain_text("pardon Steve")
    expect(audit.locator(".badge.ok")).to_have_text("已确认")
    assert scenario.audit_limits[-1] == ["20"], "P12 最近操作请求缺少 limit=20"
    scenario.select("audit")
    expect(audit).to_be_visible()
    print("[PASS] P12 封禁卡解封预览、提交、未确认与已确认、审计和回读刷新")


def check_ban_ip_entry(scenario):
    scenario.open()
    page = scenario.page
    """P14（v0.29.0）：手动封禁 IP 的入口。

    校验三件事：目标原样带进确认弹窗、理由框对 IP 封禁也要露出来（`ban-ip <ip> [reason]`）、
    网段写法（`/24`）在本地就被挡下 —— 原版 `ban-ip` 只吃单个地址，发出去只会静默失败。
    """
    scenario.select("players")
    page.locator("#sv_ban_ip").fill("192.0.2.9")
    page.locator("#sv_ban_ip_btn").click()
    page.locator("#player_dialog[open]").wait_for()
    assert page.locator("#player_target").inner_text().strip() == "192.0.2.9", \
        "P14 确认弹窗里的目标应就是这个 IP"
    assert page.locator("#player_reason_wrap").is_visible(), "P14 IP 封禁同样要能填原因"
    page.locator("#player_cancel").click()
    page.locator("#player_dialog").wait_for(state="hidden")
    page.locator("#sv_ban_ip").fill("192.0.2.0/24")
    page.locator("#sv_ban_ip_btn").click()
    assert "不是 IPv4" in page.locator("#sv_ban_ip_hint").inner_text(), \
        "P14 网段写法应在本地被挡下并说明原因"
    assert page.locator("#player_dialog").is_hidden(), "P14 非法输入不该弹出确认框"
    print("[PASS] P14 手动封禁 IP：目标正确、理由可填、网段本地拦下")


def check_player_ip(scenario):
    """P15：玩家行显示「最近进服 IP」，封禁操作使用封禁名单区的入口。

    没有记录的玩家必须明写「IP 未知」且**不给按钮** —— 监听开始前就在线、代理服、
    非 ASCII 名都会走到这条路。猜一个地址比不给更糟。
    """
    scenario.online = {"state": "ok", "players": ["Alex", "Steve"], "online": 2, "max": 20,
                       "ips": {"Alex": "192.0.2.9", "Steve": ""}}
    scenario.open()          # 必须先打开页面（main 里各 case 自己负责）
    scenario.select("players")
    page = scenario.page
    page.locator("#sv_players_refresh").click()
    page.wait_for_timeout(600)
    rows = page.locator("#sv_players .player-row")
    assert rows.count() == 2, f"P15 应渲染两行，实际 {rows.count()}"
    assert "192.0.2.9" in rows.first.inner_text(), "P15 有记录的玩家应显示真实 IP"
    assert rows.first.locator('button[data-action="ban_ip"]').count() == 0, \
        "P15 已知 IP 在线行也不应重复提供 IP 封禁入口"
    assert "IP 未知" in rows.nth(1).inner_text(), "P15 无记录必须明写「IP 未知」"
    assert rows.nth(1).locator('button[data-action="ban_ip"]').count() == 0, \
        "P15 无 IP 时不该出现封禁按钮"
    print("[PASS] P15 玩家行显示最近进服 IP（无记录时明写未知，封禁入口独立）")


def check_online_actions(scenario):
    """P16：所有在线行的动作集合准确，解封和 IP 封禁留在封禁名单区。"""
    scenario.online = {"state": "ok", "players": ["Alex", "Steve"], "online": 2, "max": 20,
                       "ips": {"Steve": IPV4}}
    scenario.open()
    page = scenario.page
    wanted = {"kick", "ban", "whitelist_add", "whitelist_remove"}
    rows = page.locator("#sv_players .player-row")
    assert rows.count() == 2, "P16 前置条件：应有两个在线玩家"
    for row, name in zip(rows.all(), ("Alex", "Steve")):
        buttons = row.locator('button[data-action]')
        actions = buttons.evaluate_all("els => els.map(el => el.dataset.action)")
        assert len(actions) == len(wanted) and set(actions) == wanted, \
            f"P16 在线玩家名动作应恰为四项：{name} 实际 {actions}"
        assert all(button.is_enabled() for button in buttons.all()), "P16 有效在线名单操作应可用"
        assert all(button.get_attribute("data-player") == name for button in buttons.all()), \
            "P16 在线行操作必须指向该行玩家"
    assert rows.first.locator("button[data-action]").count() == 4, \
        "P16 未知 IP 在线行的 data-action 集合应恰为四项"
    assert rows.locator('button[data-action="unban"], button[data-action="unban_ip"]').count() == 0, \
        "P16 在线行不应出现解封操作"
    expect(rows.nth(1).locator(".player-ip")).to_have_text(IPV4)
    expect(page.locator("#sv_ban_ip_btn")).to_be_enabled()
    expect(page.locator('#sv_bans button[data-action="unban"][data-player="Steve"]')).to_be_enabled()
    print("[PASS] P16 已知与未知 IP 在线行动作均恰为四项，解封与 IP 封禁入口独立")


def check_remote_ip_search(scenario):
    scenario.online = {**ONLINE_OK, "ip_details": {"Alex": {"state": "remote", "ip": "", "can_ban": False}}}
    scenario.open()
    row = scenario.page.locator("#sv_players .player-row")
    expect(row.get_by_role("button", name="重新搜索 IP"), "J5 异地模式须禁用重新搜索").to_be_disabled()
    expect(row).to_contain_text("异地模式下读不到服务端日志，无法搜索")
    assert not scenario.ip_searches
    print("[PASS] J5 异地原因可见，重新搜索不可操作")


def check_ip_search_details(scenario):
    scenario.online = {"state": "ok", "players": ["Alex", "Steve", "Morgan"], "online": 3, "max": 20,
                       "ip_details": {
                           "Alex": {"state": "found", "ip": "127.0.0.1", "scope": "loopback", "can_ban": True,
                                    "source": "logs/2026-11-20-1.log.gz", "joined_at": "2026-11-20T12:34:56"},
                           "Steve": {"state": "not_found", "ip": "", "scanned": 7},
                           "Morgan": {"state": "unreadable", "ip": ""},
                       }}
    scenario.ip_search_result = {"state": "found", "ip": IPV4, "scope": "", "can_ban": True,
                                 "source": "logs/latest.log", "joined_at": "2026-11-21T14:07:00"}
    scenario.open()
    rows = scenario.page.locator("#sv_players .player-row")
    note = rows.first.inner_text()
    if not all(text in note for text in (
        "回环地址（127.0.0.1）", "最后一跳", "封禁会连带影响所有经它进来的连接",
    )):
        raise AssertionError("J2 回环地址须提示影响范围")
    loopback_ban = rows.first.get_by_role("button", name="在封禁区使用此 IP")
    expect(loopback_ban).to_be_enabled()
    expect(rows.first).to_contain_text("来自 logs/2026-11-20-1.log.gz · 最后一次进服 2026-11-20 12:34")
    expect(rows.nth(1), "J7 缺记录文案须显示搜索文件数").to_contain_text("最近 7 个日志文件里没有该玩家的进服记录")
    expect(rows.nth(2)).to_contain_text("日志目录不可读（server_dir 配置可能不对）")
    rows.nth(1).get_by_role("button", name="重新搜索 IP").press("Enter")
    expect(rows.nth(1)).to_contain_text(IPV4)
    expect(rows.nth(1)).to_contain_text("来自 logs/latest.log · 最后一次进服 2026-11-21 14:07")
    expect(rows.nth(1).get_by_role("button", name="在封禁区使用此 IP")).to_be_enabled()
    assert scenario.ip_searches == [{"player": "Steve"}]
    loopback_ban.press("Enter")
    expect(scenario.page.locator("#sv_ban_ip")).to_have_value("127.0.0.1")
    assert scenario.posts == [], "J2 填入地址不得自动发送封禁"
    expect(scenario.page.locator("#player_dialog")).to_be_hidden()
    print("[PASS] J2/J3/J6/J7 回环标注和地址填入、来源和时间、手动回显、缺记录计数及目录错误")


def check_console_notice(scenario):
    """P18：常驻说明明确分类表边界，不让用户把未提示误解为安全。"""
    scenario.open()
    page = scenario.page
    page.locator('button.tab[data-page="console"]').click()
    notice = page.locator("#console_notice")
    expect(notice).to_be_visible()
    text = notice.inner_text()
    assert "分级表只覆盖常见命令" in text, "P18 常驻说明缺少分级表覆盖范围"
    assert "插件命令不在表内，同样会立即执行" in text, "P18 常驻说明缺少插件命令立即执行提醒"
    print("[PASS] P18 终端常驻说明展示常见命令分级边界与插件命令立即执行提醒")


def check_single_color_card(scenario):
    """P20：唯一可视化卡从真实回包形状初始化，三类控件与两种保存共用草稿。"""
    scenario.color_config = {
        "color_say": "#123456", "color_title": "#ABCDEF", "color_feedback": "#778899",
        "gradient_enabled": False, "gradient_format": "compat_section", "plain_color_format": "hex",
        "gradient_colors_say": "#112233,#445566", "gradient_colors_title": "#123123,#456456",
        "gradient_colors_feedback": "#ABC123,#DEF456",
    }
    initial = dict(scenario.color_config)
    scenario.open()
    page = scenario.page
    page.locator('button.tab[data-page="settings"]').click()
    card = page.locator("#page_settings #srv_color_card")
    expect(card).to_be_visible()
    color_cards = page.locator("#page_settings .card").filter(
        has=page.locator("h3").filter(has_text=re.compile(r"^文本颜色")))
    assert color_cards.count() == 1, f"P20 文本颜色卡片应恰为 1，实际 {color_cards.count()}"
    assert page.locator("#jump_text_color").count() == 0, "P20 旧颜色卡跳转入口未删除"
    duplicate_ids = page.evaluate("""() => {
        const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
        return [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
    }""")
    assert not duplicate_ids, f"P20 页面存在重复 id：{duplicate_ids}"
    expect(page.locator("#clr_plain_fmt")).to_have_value("hex")
    expect(page.locator("#clr_grad_fmt")).to_have_value("compat_section")
    expect(page.locator("#clr_grad_en")).not_to_be_checked()
    for index, (short, key) in enumerate((
        ("say", "color_say"), ("title", "color_title"), ("fb", "color_feedback")
    )):
        text = card.locator(f"#clr_{short}_txt")
        picker = card.locator(f"#clr_{short}")
        expect(text, "P20 三个颜色控件须使用接口初值").to_have_value(initial[key])
        expect(picker).to_have_value(initial[key].lower())
        changed = ("#334455", "#556677", "#8899AA")[index]
        text.fill(changed)
        expect(picker, "P20 文本编辑须同步取色器").to_have_value(changed.lower())
        picked = ("#aabbcc", "#bbccdd", "#ccddee")[index]
        picker.evaluate("(el, value) => { el.value = value; el.dispatchEvent(new Event('input', {bubbles:true})); }", picked)
        expect(text).to_have_value(picked.upper())
        # 后两格必须仍是各自的接口初值，不能因修改第一格而串台。
        for other in ("say", "title", "fb")[index + 1:]:
            other_key = "color_feedback" if other == "fb" else f"color_{other}"
            expect(card.locator(f"#clr_{other}_txt")).to_have_value(initial[other_key])
    page.locator("label.sw").filter(has=page.locator("#clr_grad_en")).click()
    expect(page.locator("#clr_grad_en")).to_be_checked()
    for short, key in (("say", "say"), ("title", "title"), ("fb", "feedback")):
        stops = card.locator(f"#stops_{short} input[type=text]")
        assert stops.count() == 2, f"P20 {short} 渐变锚点缺失"
        for field, expected in zip(stops.all(), initial[f"gradient_colors_{key}"].split(",")):
            expect(field).to_have_value(expected)
        stops.first.fill("#FEDCBA")
    page.select_option("#clr_plain_fmt", "named")
    for short, name in (("say", "aqua"), ("title", "red"), ("fb", "blue")):
        selector = card.locator(f"#clr_{short}_sel")
        expect(selector).to_be_visible()
        assert selector.locator("option").count() == 16, f"P20 {short} 应有16种格式化色"
        selector.select_option(name)
        expect(card.locator(f"#clr_{short}_txt")).to_have_value(name)
    page.locator("#cfg_feedback_name").fill("颜色测试署名草稿")
    page.locator("#clr_save").click()
    expect(page.locator("#clr_notice")).to_contain_text("✓")
    assert scenario.color_saves, "P20 保存颜色未发请求"
    saved = scenario.color_saves[-1]
    assert all(saved[key] == value for key, value in {
        "color_say": "aqua", "color_title": "red", "color_feedback": "blue",
        "plain_color_format": "named", "gradient_format": "compat_section", "gradient_enabled": True,
    }.items()), f"P20 保存颜色载荷不符：{saved}"
    assert all(saved[f"gradient_colors_{key}"].startswith("#FEDCBA,")
               for key in ("say", "title", "feedback")), "P20 保存颜色遗漏手改渐变"
    expect(page.locator("#cfg_feedback_name")).to_have_value("颜色测试署名草稿")
    page.select_option("#clr_plain_fmt", "hex")
    page.locator("#clr_say_txt").fill("#010203")
    page.locator("label.sw").filter(has=page.locator("#clr_grad_en")).click()
    expect(page.locator("#clr_grad_en")).not_to_be_checked()
    with page.expect_response("**/settings/save"):
        page.locator("#btn_save_cfg_top").click()
    saved_all = scenario.settings_saves[-1]
    assert saved_all.get("color_say") == "#010203" and saved_all.get("plain_color_format") == "hex" \
        and saved_all.get("gradient_enabled") is False \
        and saved_all.get("feedback_name") == "颜色测试署名草稿", \
        f"P20 保存全部设置未提交唯一可视化草稿：{saved_all}"
    expect(page.locator("#clr_say_txt")).to_have_value("#010203")
    page.locator('button.tab[data-page="server"]').click()
    scenario.select("tools")
    page.locator("#bc_text_color").click()
    expect(card).to_be_visible()
    expect(card).to_have_class(re.compile(r"jump-highlight"))
    # 冷启动读取失败时，保存其他设置不能把未加载的颜色默认值写回服务端。
    scenario.color_load_ok = False
    scenario.open()
    page.locator('button.tab[data-page="settings"]').click()
    with page.expect_response("**/settings/save"):
        page.locator("#btn_save_cfg_top").click()
    saved_unloaded = scenario.settings_saves[-1]
    assert not (set(initial) & set(saved_unloaded)), \
        "P20 颜色加载失败时保存全部设置不得提交颜色默认值"
    assert not scenario.errors, f"P20 页面初始化/交互异常：{scenario.errors}"
    print("[PASS] P20 唯一颜色卡、三个非默认初值、独立双向交互、渐变/色名、两种保存与广播跳转")


def check_invalid_ip_bans(scenario):
    """P21：异常 IP 保留原文并转义显示，只有合法地址可解封。"""
    malicious = '<img src=x onerror="window.ipInjected=true">'
    scenario.bans = {"state": "ok", "items": [
        {**BAN, "kind": "ip", "name": "192.0.2.9", "removable": True},
        *[{**BAN, "kind": "ip", "name": name, "removable": False,
           "note": f"格式异常，不能在这里解封；原记录：{json.dumps({'ip': name}, ensure_ascii=False)}"}
          for name in ("192.0.2.0/24", "not-an-ip", malicious)],
    ]}
    scenario.open()
    rows = scenario.page.locator("#sv_bans .player-row")
    assert rows.count() == 4, "P21 合法与异常 IP 记录都必须可见"
    expect(rows.first.locator('button[data-action="unban_ip"]')).to_be_enabled()
    for row, name in zip(rows.all()[1:], ("192.0.2.0/24", "not-an-ip", malicious)):
        assert name in row.inner_text(), f"P21 异常 IP 原文缺失：{name}"
        assert row.locator("button").count() == 0, "P21 异常 IP 行不得渲染解封按钮"
        note = row.locator(".player-ip-invalid.muted")
        expect(note, "P21 异常 IP 行必须显示灰色说明").to_be_visible()
        assert "格式异常" in note.inner_text() and "原记录" in note.inner_text(), \
            "P21 异常 IP 说明必须包含格式异常与原记录"
    assert rows.locator("img, script").count() == 0, "P21 异常 IP 原文未正确转义"
    assert scenario.page.evaluate("() => window.ipInjected === undefined"), "P21 异常 IP 原文执行了脚本"
    print("[PASS] P21 合法 IP 可解封，异常 IP 保留原文、灰色说明、无按钮并转义显示")


def check_long_ban_list(scenario):
    scenario.online = {**ONLINE_OK, "ips": {"Alex": IPV4}}
    address, malformed = "198.51.100.42", "198.51.100.0/24"
    scenario.bans = {"state": "ok", "items": [
        *[{**BAN, "name": f"Player_{n:02d}", "reason": f"违规记录 {n:02d}"} for n in range(28)],
        {**BAN, "kind": "ip", "name": address},
        {**BAN, "kind": "ip", "name": malformed, "removable": False, "note": "格式异常，需查看原记录"},
    ]}
    scenario.open()
    page = scenario.page
    page.evaluate("""() => {
        Object.defineProperty(navigator, "clipboard", {
            configurable:true, value:{writeText: async value => { window.copiedIp = value; }},
        });
    }""")
    rows = page.locator("#sv_bans .player-row")
    expect(rows).to_have_count(30)
    expect(page.locator("#sv_ban_count")).to_have_text("显示 30 / 30 条")
    search = page.locator("#sv_ban_search")
    search.fill("违规记录 27")
    expect(page.locator("#sv_ban_count")).to_have_text("显示 1 / 30 条")
    visible = page.locator("#sv_bans .player-row:visible")
    expect(visible.locator('button[data-action="unban"]')).to_have_attribute("data-player", "Player_27")
    search.fill(malformed)
    expect(visible).to_contain_text("格式异常")
    expect(visible.locator("button")).to_have_count(0)
    search.fill(address)
    expect(visible.locator('button[data-action="unban_ip"]')).to_have_attribute("data-player", address)
    visible.get_by_role("button", name="复制 IP").press("Enter")
    assert page.evaluate("window.copiedIp") == address, "R5 复制必须指向当前行 IP"
    search.fill("匹配不到的记录")
    expect(page.locator("#sv_ban_no_matches")).to_be_visible()

    page.locator("#sv_players").get_by_role("button", name="在封禁区使用此 IP").press("Enter")
    expect(page.locator("#sv_ban_ip")).to_have_value(IPV4)
    expect(page.locator("#sv_ban_ip")).to_be_focused()
    assert scenario.posts == [], "R5 填入地址之前或之后均不得自动发送命令"
    page.locator("#sv_ban_ip").press("Enter")
    expect(page.locator("#player_target")).to_have_text(IPV4)
    expect(page.locator("#player_dialog")).to_be_visible()
    assert scenario.posts[0]["preview"] and scenario.posts[0]["player"] == IPV4
    page.locator("#player_cancel").press("Enter")

    scenario.bans = {"state": "empty", "items": []}
    scenario.refresh()
    expect(page.locator("#sv_bans")).to_contain_text("当前没有封禁记录")
    expect(page.locator("#sv_ban_no_matches")).to_be_hidden()
    scenario.bans = {"state": "unreachable", "items": [], "note": "读取失败，请刷新重试"}
    scenario.refresh()
    expect(page.locator("#sv_bans")).to_contain_text("读取失败，请刷新重试")
    print("[PASS] R5 30 条封禁、搜索和目标、异常只读、复制、选用最近进服 IP、空名单和读失败")


def check_workflow_single_writer(scenario):
    """P23：设置只读并指路，唯一写入口即时保存且不覆盖其他设置草稿。"""
    scenario.workflow_config = {"agent_workflow_enabled": True, "enable_mc_workflow": False,
                                "feedback_name": "原有反馈署名"}
    scenario.open()
    page = scenario.page
    page.locator('button.tab[data-page="settings"]').click()
    expect(page.locator("#cfg_feedback_name")).to_have_value("原有反馈署名")
    for selector in ("#cfg_wf_enabled", "#cfg_wf_tool"):
        expect(page.locator(selector), "P23 设置页两个工作流开关必须只读").to_be_disabled()
    expect(page.locator("#cfg_wf_enabled")).to_be_checked()
    expect(page.locator("#cfg_wf_tool")).not_to_be_checked()
    jump = page.locator("#cfg_wf_jump")
    expect(jump, "P23 设置页必须提供工作流修改入口").to_be_visible()
    expect(jump).to_contain_text("到工作流页修改")
    draft = "不能因工作流开关丢失的署名草稿"
    page.locator("#cfg_feedback_name").fill(draft)
    settings_reads = scenario.settings_reads
    jump.click()
    expect(page.locator("#page_workflow"), "P23 跳转必须打开工作流页").to_be_visible()
    expect(page.locator("#wf_refresh")).to_be_enabled()
    expect(page.locator("#sw_wf_enabled")).to_be_checked()
    expect(page.locator("#sw_wf_tool")).not_to_be_checked()

    for selector, mirror, key, value in (
        ("#sw_wf_enabled", "#cfg_wf_enabled", "agent_workflow_enabled", False),
        ("#sw_wf_tool", "#cfg_wf_tool", "enable_mc_workflow", True),
    ):
        control = page.locator(selector)
        expect(control, "P23 工作流页开关必须可写").to_be_enabled()
        scenario.workflow_hold = True
        with page.expect_request("**/settings/save"):
            page.locator("label.sw").filter(has=control).click()
        for other in ("#sw_wf_enabled", "#sw_wf_tool"):
            expect(page.locator(other), "P23 保存期间两开关必须共同禁用").to_be_disabled()
        assert len(scenario.workflow_held) == 1, "P23 一次切换只能提交一个保存请求"
        with page.expect_response("**/settings/save"):
            scenario.finish_workflow_save(*scenario.workflow_held.pop())
        scenario.workflow_hold = False
        expect(control).to_be_enabled()
        assert scenario.settings_saves[-1] == {key: value}, \
            f"P23 工作流切换必须单独提交对应开关：{scenario.settings_saves[-1]}"
        assert control.is_checked() is value, "P23 工作流开关保存后显示不符"
        assert page.locator(mirror).is_checked() is value, "P23 设置页只读状态未同步"
        assert scenario.settings_reads == settings_reads, "P23 工作流保存不得重载整张设置表"
        expect(page.locator("#cfg_feedback_name"), "P23 工作流保存不得覆盖无关草稿").to_have_value(draft)
    expect(page.locator("#wf_banner")).to_contain_text("agent_workflow_enabled")
    expect(page.locator("#cfg_wf_tip")).to_contain_text("工作流总开关")
    assert "改动未保存" not in page.locator("#cfg_wf_tip").inner_text(), "P23 只读状态不得提示批量保存"

    page.reload()
    page.locator('button.tab[data-page="workflow"]').click()
    expect(page.locator("#wf_refresh")).to_be_enabled()
    expect(page.locator("#sw_wf_enabled"), "P23 总开关重载后必须保留保存值").not_to_be_checked()
    expect(page.locator("#sw_wf_tool"), "P23 工具开关重载后必须保留保存值").to_be_checked()
    page.locator('button.tab[data-page="settings"]').click()
    expect(page.locator("#cfg_feedback_name")).to_have_value("原有反馈署名")
    expect(page.locator("#cfg_wf_enabled")).not_to_be_checked()
    expect(page.locator("#cfg_wf_tool")).to_be_checked()
    # 即使 DOM 中只读展示被改写，批量保存也不得把它当成第二个写入口。
    page.locator("#cfg_wf_enabled").evaluate("el => { el.checked = true; }")
    page.locator("#cfg_wf_tool").evaluate("el => { el.checked = false; }")
    page.locator("#cfg_feedback_name").fill("正常批量设置")
    with page.expect_response("**/settings/save"):
        page.locator("#btn_save_cfg_top").click()
    submitted = scenario.settings_saves[-1]
    assert not ({"agent_workflow_enabled", "enable_mc_workflow"} & set(submitted)), \
        "P23 设置页批量保存不得包含工作流开关"
    assert scenario.workflow_config["agent_workflow_enabled"] is False \
        and scenario.workflow_config["enable_mc_workflow"] is True, "P23 批量保存改写了工作流状态"
    expect(page.locator("#cfg_feedback_name")).to_have_value("正常批量设置")
    scenario.workflow_reject = True
    page.locator("#cfg_wf_jump").click()
    expect(page.locator("#wf_refresh")).to_be_enabled()
    with page.expect_response("**/settings/save"):
        page.locator("label.sw").filter(has=page.locator("#sw_wf_enabled")).click()
    expect(page.locator("#wf_notice")).to_contain_text("模拟保存失败")
    expect(page.locator("#sw_wf_enabled")).not_to_be_checked()
    expect(page.locator("#sw_wf_enabled")).to_be_enabled()
    assert not scenario.errors, f"P23 页面异常：{scenario.errors}"
    print("[PASS] P23 设置只读并跳转，工作流两开关单独保存、重载保留、草稿不丢、批量不覆写及失败回滚")


def check_workflow_history(scenario):
    scenario.workflow_config = {"agent_workflow_enabled": True, "enable_mc_workflow": True}
    scenario.workflow_logs = [
        {"status": "running", "historical": True, "request": "历史任务 · Alex",
         "detail": "复杂任务进入后台流水线", "ts": "2026-11-19 10:00:00"},
        {"status": "running", "request": "当前任务", "ts": "10:00:10"},
    ]
    scenario.open()
    page = scenario.page
    page.locator('button.tab[data-page="workflow"]').click()
    history = page.locator("#wf_logs .wf").filter(has_text="历史任务 · Alex")
    expect(history).to_contain_text("历史 · 未记录结果")
    expect(page.locator("#wf_stats"), "P24 恢复的历史任务不得计为正在执行").to_contain_text("1 执行中")
    print("[PASS] P24 历史摘要回显，历史运行条目不计入当前执行数")


CASES = {"P1": check_four_states, "P6": check_permission, "P8": check_disabled,
         "P9": check_no_ip, "P10": check_rejection, "P11": check_ban_rendering, "P12": check_unban,
         "P14": check_ban_ip_entry, "P15": check_player_ip, "P16": check_online_actions,
         "P18": check_console_notice, "P20": check_single_color_card,
         "P21": check_invalid_ip_bans, "P23": check_workflow_single_writer, "P24": check_workflow_history,
         "R5": check_long_ban_list, "J5": check_remote_ip_search, "J7": check_ip_search_details}


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
