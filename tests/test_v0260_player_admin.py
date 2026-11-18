"""v0.26.0 玩家管理契约。使用假 RCON 和真实 Web API 请求上下文。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from starlette.requests import Request

from _paths import PLUGIN_DIR, add_sys_paths, require_app

add_sys_paths()
require_app()

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_Scintilla_MC_Server_Control.core import player_admin
from astrbot_plugin_Scintilla_MC_Server_Control.core.version_caps import resolve_version_info
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi, PAGE_PREFIX

ADMIN = "dashboard-owner"
OTHER = "other-user"
SECRET = "player-admin-test-secret-at-least-32-bytes"
ONLINE = "There are 2 of a max of 20 players online: Steve, Alex"
EMPTY = "There are 0 of a max of 20 players online:"
BANNED = "There are 1 ban(s):Steve was banned by Rcon: matrix-v0260"
NO_BANS = "There are no bans"
WL_ADD = "There are 1 whitelisted player(s): Steve"
WL_EMPTY = "There are no whitelisted players"
UNKNOWN = 'Unknown command. Type "/help" for help.'


class FakePlugin:
    def __init__(self, replies=None, *, server_dir="", connect_error=None):
        self.events = []
        self.replies = replies or {}
        self.server_dir = server_dir
        self.connect_error = connect_error
        self.block = set()
        self.last_boundary_confirmed = True
        self.routes = {}
        self.context = SimpleNamespace(
            get_config=lambda: {
                "dashboard": {"username": ADMIN, "jwt_secret": SECRET},
            },
            register_web_api=self.register,
        )

    def register(self, path, handler, methods, _description):
        for method in methods:
            self.routes[method, path] = handler

    def _cfg(self, key, default=None):
        return {"server_dir": self.server_dir, "remote_rcon_mode": not self.server_dir}.get(
            key, default,
        )

    def _resolve_version_info(self):
        return resolve_version_info("1.20.6")

    async def _get_rcon(self):
        if self.connect_error:
            raise self.connect_error
        return self

    def _guard_command_for_version(self, command, *, source):
        self.events.append(("guard", command, source))
        return "矩阵守卫拒绝该命令" if command in self.block else ""

    async def command(self, command):
        self.events.append(("send", command))
        reply = self.replies[command]
        if isinstance(reply, Exception):
            raise reply
        return reply


async def call(plugin, method, endpoint, *, body=None, username=ADMIN, query="",
               v1=False, token_type=None, cookie=False):
    McControlWebApi(plugin).register()
    route = f"{PAGE_PREFIX}/player/{endpoint}"
    prefix = "/api/v1/plugins/extensions/" if v1 else "/api/plug/"
    headers = [(b"content-type", b"application/json")]
    if v1 and username:
        claims = {"username": username, "exp": datetime.now(timezone.utc) + timedelta(minutes=1)}
        if token_type is not None:
            claims["token_type"] = token_type
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        if cookie:
            from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME

            headers.append((b"cookie", f"{DASHBOARD_JWT_COOKIE_NAME}={token}".encode("ascii")))
        else:
            headers.append((b"authorization", f"Bearer {token}".encode("ascii")))

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode("utf-8")}

    host_req = Request(
        {"type": "http", "method": method, "path": prefix + route,
         "headers": headers, "query_string": query.encode("ascii")},
        receive,
    )
    with bind_request_context(PluginRequest(host_req, username=username)):
        response = await plugin.routes[method, route]()
    return response.status_code, json.loads(response.body)


class PlayerAdminCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name)
        self.audit = self.path / "player_audit.jsonl"
        patcher = patch.object(player_admin, "_audit_path", return_value=self.audit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertGuarded(self, plugin, *commands):
        expected = []
        for command in commands:
            expected.extend((("guard", command, "web_player_admin"), ("send", command)))
        self.assertEqual(plugin.events, expected, "每次发送都必须紧跟同命令的版本守卫")

    async def test_p01_online_states(self):
        cases = (
            ("ok", ONLINE, None, ["Steve", "Alex"], ""),
            ("empty", EMPTY, None, [], ""),
            ("unrecognized", "Players online (2): Steve, Alex", None, [],
             "Players online (2): Steve, Alex"),
            ("unreachable", "", ConnectionError("RCON disconnected"), [], ""),
        )
        for state, reply, failure, players, raw in cases:
            with self.subTest(state=state):
                plugin = FakePlugin({"list": reply}, connect_error=failure)
                result = await player_admin.get_online(plugin)
                self.assertEqual(result["state"], state, f"P1 名单状态 {state}")
                self.assertEqual(result["players"], players, f"P1 {state} 名单")
                self.assertEqual(result.get("raw", ""), raw or reply, f"P1 {state} 原文")
                if failure:
                    self.assertIn("RCON disconnected", result["note"])
                    self.assertEqual(plugin.events, [("guard", "list", "web_player_admin")])
                else:
                    self.assertGuarded(plugin, "list")

        # 共享解析器还支持单有人数的中文回包。缺失名单时不允许推断目标已经下线。
        plugin = FakePlugin({"list": "有 2 个玩家在线，最多 20"})
        result = await player_admin.get_online(plugin)
        self.assertEqual(result["state"], "unrecognized", "P1 有人数但缺名单")

    def test_p02_reason_depends_on_generation(self):
        for version in ("1.7.10", "1.12.2", "1.13.2", "1.21.11"):
            with self.subTest(version=version):
                caps = resolve_version_info(version)
                # 矩阵旧 Forge 使用无原因的 MVP 形态。不存在的玩家两种写法均被拒，
                # 所以不能据此断定旧版踢出语法本身拒收原因。新版封禁原因已进回读和 JSON。
                suffix = "" if version in {"1.7.10", "1.12.2"} else " matrix-v0260"
                for action in ("kick", "ban"):
                    self.assertEqual(
                        player_admin.build_command(action, "Steve", "matrix-v0260", caps),
                        f"{action} Steve{suffix}",
                        f"P2 {version} {action} 原因世代分叉",
                    )
                self.assertEqual(player_admin.build_command("unban", "Steve", "", caps),
                                 "pardon Steve")

    def test_p15_login_ip_index_is_read_only_and_case_insensitive(self):
        """v0.29.0：进服行的 IP 旁路索引。

        这条索引**不产生事件**（`_JOIN_PATTERN` / `_parse_line` 一行没改），
        只是顺带记下「玩家 → 最近出口 IP」，供界面上的「封禁这个 IP」使用。
        查不到时返回空串 —— 界面据此显示「IP 未知」，绝不猜一个地址。
        """
        from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW

        watcher = LW.LogWatcher.__new__(LW.LogWatcher)      # 只测索引，不启动线程
        watcher._recent_ips = {}
        watcher._note_login_ip(
            "[16:30:01] [Server thread/INFO]: Steve[/192.0.2.9:52031] "
            "logged in with entity id 42 at (0.5, 64.0, -12.5)")
        self.assertEqual(watcher.recent_ip("Steve"), "192.0.2.9")
        self.assertEqual(watcher.recent_ip("steve"), "192.0.2.9", "键应大小写不敏感")
        self.assertEqual(watcher.recent_ip("Alex"), "", "没有记录的玩家必须返回空串")
        # 不带 IP 的进服行（离线服 / 老格式）不该覆盖已有记录，也不该抛错
        watcher._note_login_ip("[16:31:02] [Server thread/INFO]: Steve joined the game")
        self.assertEqual(watcher.recent_ip("Steve"), "192.0.2.9")
        # IPv6 也要能捞（方括号里带冒号）
        watcher._note_login_ip(
            "[16:32:00] [Server thread/INFO]: Alex[/2001:db8::42:52031] "
            "logged in with entity id 7 at (1.0, 64.0, 2.0)")
        self.assertEqual(watcher.recent_ip("alex"), "2001:db8::42")

    async def test_p15_online_carries_ips_without_guessing(self):
        """在线名单附带 IP；索引里没有的人留空，界面显示「IP 未知」。"""
        plugin = FakePlugin({"list": "There are 2 of a max of 20 players online: Steve, Alex"})

        class _Watcher:
            def recent_ip(self, name):
                return {"steve": "192.0.2.9"}.get(str(name).casefold(), "")

        plugin._watcher = _Watcher()
        result = await player_admin.get_online(plugin)
        self.assertEqual(result["ips"], {"Steve": "192.0.2.9", "Alex": ""})
        # 没有 watcher（未启用日志监听）时不该多出 ips 字段，也不该炸
        bare = FakePlugin({"list": "There are 1 of a max of 20 players online: Steve"})
        result2 = await player_admin.get_online(bare)
        self.assertNotIn("ips", result2)

    def test_p14_ip_ban_actions(self):
        """v0.29.0：IP 封禁 —— 命令按世代带原因、地址严格校验、回读查 IP 名单。

        `ban-ip` 在 1.13+ 才有 `[原因]` 参数（与 kick/ban 同一条分界线）；
        地址必须是单个 IPv4/IPv6（原版不支持 CIDR 网段，写 /24 会静默失败）。
        """
        for version, suffix in (("1.7.10", ""), ("1.13.2", " scanner")):
            with self.subTest(version=version):
                caps = resolve_version_info(version)
                self.assertEqual(
                    player_admin.build_command("ban_ip", "192.0.2.9", "scanner", caps),
                    f"ban-ip 192.0.2.9{suffix}",
                    f"P14 {version} ban-ip 原因世代分叉",
                )
                self.assertEqual(player_admin.build_command("unban_ip", "192.0.2.9", "", caps),
                                 "pardon-ip 192.0.2.9")
        # IPv6 规范化：用户写法与服务端写法未必一致（`::1` vs `0:0:0:0:0:0:0:1`）
        self.assertEqual(
            player_admin.build_command("ban_ip", "2001:0db8:0:0:0:0:0:1", "",
                                       resolve_version_info("1.13.2")),
            "ban-ip 2001:db8::1",
        )
        # 原版 ban-ip 只接受单个地址：CIDR 段与非法串一律拒绝，别发出去静默失败
        for bad in ("192.0.2.0/24", "300.1.1.1", "not-an-ip", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    player_admin.build_command("ban_ip", bad, "", resolve_version_info("1.13.2"))
        # 回读必须查 IP 名单 —— 查 `banlist players` 会永远报「未确认」
        self.assertEqual(player_admin._READBACK_COMMAND["ban_ip"], "banlist ips")
        self.assertEqual(player_admin._READBACK_COMMAND["unban_ip"], "banlist ips")

    async def test_p03_kick_requires_absence_in_complete_list(self):
        cases = (
            (ONLINE, False, "unrecognized"),
            ("There are 1 of a max of 20 players online: Alex", True, "ok"),
            ("There are 1 of a max of 20 players online:", False, "unrecognized"),
        )
        for readback, confirmed, state in cases:
            with self.subTest(readback=readback):
                plugin = FakePlugin({"kick Steve": "Kicked Steve", "list": readback})
                result = await player_admin.run_action(plugin, "kick", "Steve")
                self.assertIs(result["confirmed"], confirmed, "P3 踢出仅在目标从完整名单消失时确认")
                self.assertEqual(result["state"], state)
                self.assertEqual(result["readback"]["raw"], readback)
                self.assertGuarded(plugin, "kick Steve", "list")

    async def test_p04_ban_and_unban_require_banlist(self):
        cases = (
            ("ban", "ban Steve", "Banned Steve", BANNED, "ok", True),
            ("unban", "pardon Steve", "Unbanned Steve", NO_BANS, "ok", True),
            ("ban", "ban Steve", "Banned Steve", UNKNOWN, "unsupported", False),
            ("unban", "pardon Steve", "Unbanned Steve", "Ban list is loading", "unrecognized", False),
            ("ban", "ban Steve", "Banned Steve", TimeoutError("RCON timeout"), "unreachable", False),
        )
        for action, cmd, reply, readback, state, confirmed in cases:
            with self.subTest(action=action, state=state):
                plugin = FakePlugin({cmd: reply, "banlist players": readback})
                result = await player_admin.run_action(plugin, action, "Steve")
                self.assertIs(result["confirmed"], confirmed, "P4 封禁/解封必须回读 banlist players")
                self.assertEqual(result["state"], state)
                if not confirmed:
                    self.assertIn("未确认", result["note"])
                if isinstance(readback, str):
                    self.assertEqual(result["readback"]["raw"], readback)
                self.assertGuarded(plugin, cmd, "banlist players")

    async def test_p05_whitelist_requires_membership_readback(self):
        cases = (
            ("add", "Added Steve to the whitelist", WL_ADD, True),
            ("add", "Added Steve to the whitelist", WL_EMPTY, False),
            ("remove", "Removed Steve from the whitelist", WL_EMPTY, True),
            ("remove", "Removed Steve from the whitelist", WL_ADD, False),
        )
        for action, reply, readback, confirmed in cases:
            with self.subTest(action=action, confirmed=confirmed):
                cmd = f"whitelist {action} Steve"
                plugin = FakePlugin({cmd: reply, "whitelist list": readback})
                result = await player_admin.run_action(plugin, f"whitelist_{action}", "Steve")
                self.assertIs(result["confirmed"], confirmed, "P5 白名单增删须确认目标成员关系")
                self.assertEqual(result["readback"]["raw"], readback)
                self.assertGuarded(plugin, cmd, "whitelist list")

    async def test_p06_direct_routes_require_dashboard_administrator(self):
        plugin = FakePlugin({
            "kick Steve": "Kicked Steve", "list": EMPTY, "banlist players": NO_BANS,
        })
        body = {"action": "kick", "player": "Steve", "username": ADMIN, "actor": ADMIN}
        for endpoint, method in (("action", "POST"), ("bans", "GET"), ("audit", "GET")):
            for username in (None, OTHER, "api_key:synthetic"):
                with self.subTest(endpoint=endpoint, username=username):
                    code, payload = await call(plugin, method, endpoint, body=body, username=username)
                    self.assertEqual(code, 403, f"P6 {endpoint} 拒绝直接调用者")
                    self.assertIs(payload.get("ok"), False)
        self.assertEqual(plugin.events, [], "P6 权限拒绝不得触发 RCON")
        self.assertFalse(self.audit.exists())
        for cookie in (False, True):
            code, payload = await call(plugin, "GET", "audit", v1=True, cookie=cookie)
            self.assertEqual((code, payload["authorized"]), (200, True))
        code, payload = await call(plugin, "GET", "audit", v1=True, token_type="plugin_page_asset")
        self.assertEqual(code, 403, "P6 资源令牌不得操作玩家管理")
        code, payload = await call(plugin, "POST", "action", body=body)
        self.assertEqual((code, payload["ok"]), (200, True))

    async def test_p07_all_dispatched_writes_are_audited(self):
        raw = "Kicked Steve: " + "长" * 250
        plugin = FakePlugin({"kick Steve": raw, "list": EMPTY})
        await player_admin.run_action(plugin, "kick", "Steve", actor=ADMIN)
        plugin = FakePlugin({"kick Alex": "No player was found", "list": EMPTY})
        await player_admin.run_action(plugin, "kick", "Alex", actor=ADMIN)
        self.assertTrue(self.audit.is_file(), "P7 写操作必须落 JSONL")
        rows = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2, "P7 一次写操作严格一行")
        for row, player, confirmed in zip(rows, ("Steve", "Alex"), (True, False), strict=True):
            self.assertEqual(set(row), {
                "ts", "actor", "action", "player", "command", "state", "confirmed", "raw_excerpt",
            })
            self.assertTrue(row["ts"])
            self.assertEqual((row["actor"], row["action"], row["player"], row["command"]),
                             (ADMIN, "kick", player, f"kick {player}"))
            self.assertIs(row["confirmed"], confirmed)
        self.assertEqual(rows[0]["raw_excerpt"], raw[:200], "P7 审计原文截断到 200 字符")
        code, result = await call(plugin, "GET", "audit", query="limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(result["items"], [rows[1]], "P7 审计最近一行")

    async def test_p10_rejected_write_never_succeeds_even_if_target_absent(self):
        for raw in (UNKNOWN, "No player was found", "Unknown command"):
            with self.subTest(raw=raw):
                # 即使回读已经是空服，也不能把命令被拒误报成踢出成功。
                plugin = FakePlugin({"kick Steve": raw, "list": EMPTY})
                code, result = await call(plugin, "POST", "action",
                                          body={"action": "kick", "player": "Steve"})
                self.assertEqual(code, 200)
                self.assertIs(result["ok"], False, "P10 服务端拒绝不可报成功")
                self.assertIs(result["confirmed"], False, "P10 服务端拒绝不可报已确认")
                self.assertNotEqual(result["state"], "ok")
                self.assertEqual(result["raw"], raw)

    async def test_p11_remote_banlist_preserves_runtime_names_and_unknown_details(self):
        plugin = FakePlugin({"banlist players": BANNED, "list": EMPTY})
        code, result = await call(plugin, "GET", "bans")
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "ok")
        self.assertEqual([row["name"] for row in result["items"]], ["Steve"], "P11 名单来自运行时")
        item = result["items"][0]
        self.assertIs(item["detail_available"], False)
        for key in ("reason", "created", "expires", "source"):
            with self.subTest(detail=key):
                self.assertIn("未知", item.get(key, ""), "P11 未配置目录时详情必须明写未知")
        self.assertIn("server_dir", result["detail_note"], "P11 说明为何读不到详情")
        self.assertIn(("send", "banlist players"), plugin.events)
        self.assertIn(("send", "list"), plugin.events)

    async def test_p11_local_file_enriches_but_cannot_change_membership(self):
        with io.open(self.path / "banned-players.json", "w", encoding="utf-8", newline="\n") as out:
            json.dump([
                {"name": "steve", "created": "2026-11-17 15:23:45 +0800", "expires": "forever",
                 "reason": "file reason", "source": "Console"},
                {"name": "FileOnly", "reason": "stale"},
            ], out)
        plugin = FakePlugin({"banlist players": BANNED, "list": EMPTY}, server_dir=str(self.path))
        code, result = await call(plugin, "GET", "bans")
        self.assertEqual(code, 200)
        self.assertEqual([row["name"] for row in result["items"]], ["Steve"],
                         "P11 文件只补详情，不加入运行时不存在的玩家")
        item = result["items"][0]
        self.assertIs(item["detail_available"], True)
        self.assertEqual((item["reason"], item["source"]), ("file reason", "Console"))
        self.assertEqual(result["mismatch_count"], 1, "P11 明示运行时与文件差异")

    async def test_p12_unban_on_banlist_must_remove_target(self):
        for raw, confirmed in ((BANNED, False), (NO_BANS, True)):
            with self.subTest(confirmed=confirmed):
                plugin = FakePlugin({"pardon Steve": "Unbanned Steve", "banlist players": raw})
                code, result = await call(plugin, "POST", "action",
                                          body={"action": "unban", "player": "Steve"})
                self.assertEqual(code, 200)
                self.assertIs(result["confirmed"], confirmed, "P12 仍在封禁名单不可报已解封")
                self.assertIs(result["ok"], confirmed)
                self.assertGuarded(plugin, "pardon Steve", "banlist players")

    async def test_matrix_multiple_banlist_rows_require_known_boundaries(self):
        examples = (
            ("There are 2 total banned players:steve and alex", "ok", ["steve", "alex"]),
            ("There are 1 ban(s):steve was banned by Rcon: steve was banned by prior vote",
             "ok", ["steve"]),
            ("There are 2 ban(s):\nsteve was banned by Rcon: reason\n"
             "alex was banned by Rcon: second, reason", "ok", ["steve", "alex"]),
            ("There are 2 ban(s):steve was banned by Rcon: reason"
             "alex was banned by Rcon: second, reason", "unrecognized", []),
            # v0.26.0 验收补：**声明数量与实际名字数不符**也必须判认不出。
            # 这类回包真的会出现（被截断、颜色码吃掉一个名字），而它是最容易被
            # 「反正是名字就收下」的实现放过去的那一类 —— 少一条名字就意味着
            # 封禁名单显示不全、解封按钮可能对着错行。
            ("There are 3 total banned players:steve and alex", "unrecognized", []),
            ("There are 2 total banned players:steve", "unrecognized", []),
            ("There are 2 ban(s):\nsteve was banned by Rcon: reason", "unrecognized", []),
        )
        for raw, state, players in examples:
            with self.subTest(state=state, raw=raw):
                plugin = FakePlugin({"banlist players": raw})
                result = await player_admin.get_bans(plugin)
                self.assertEqual(result["state"], state)
                self.assertEqual([item["name"] for item in result["items"]], players)
                self.assertEqual(result["raw"], raw)
                # v0.28.0：get_bans 会顺带读一次 IP 封禁名单（两条读命令，都过守卫）
                self.assertGuarded(plugin, "banlist players", "banlist ips")

    async def test_bans_fall_back_to_file_when_rcon_reply_is_glued(self):
        """v0.26.1（现场）：多条封禁粘连导致回包认不出时，名单改用文件里的记录。

        真机原样（1.13.2，在控制台手动 ban 了 123）：
        `There are 2 bans:demoplayer was banned by Recon: 演示用：看看封禁名单块123 was banned by Server: Banned by an operator.`
        切分点无法确定（理由里可能藏着下一条的玩家名），但 banned-players.json 是确定性数据。
        旧实现让整卡空着，用户只看到一段原文 —— 现在必须列出两条、且逐条可解封，
        同时 state 仍如实保持「回执认不出」，note 里写清名单来源。
        """
        glued = ("There are 2 bans:demoplayer was banned by Recon: 演示用：看看封禁名单块"
                 "123 was banned by Server: Banned by an operator.")
        with io.open(self.path / "banned-players.json", "w", encoding="utf-8", newline="\n") as out:
            json.dump([
                {"name": "demoplayer", "reason": "演示用：看看封禁名单块",
                 "created": "2026-11-17 18:17:26 +0800", "source": "Recon", "expires": "forever"},
                {"name": "123", "reason": "Banned by an operator.",
                 "created": "2026-11-17 18:20:44 +0800", "source": "Server", "expires": "forever"},
            ], out, ensure_ascii=False)
        plugin = FakePlugin({"banlist players": glued}, server_dir=str(self.path))
        result = await player_admin.get_bans(plugin)
        self.assertEqual(result["state"], "unrecognized")           # 不粉饰：回包确实认不出
        self.assertEqual(result["roster_source"], "banned-players.json")
        self.assertIn("无法可靠切分", result["note"])
        self.assertEqual([item["name"] for item in result["items"]], ["demoplayer", "123"])
        self.assertTrue(all(item["removable"] for item in result["items"]))
        self.assertTrue(all(item["detail_available"] for item in result["items"]))
        self.assertEqual(result["items"][1]["reason"], "Banned by an operator.")

    async def test_bans_without_readable_file_still_refuse_to_guess(self):
        """回包认不出、文件也读不到 → 依然不许猜（条目必须空着，只留原文）。"""
        glued = ("There are 2 bans:demoplayer was banned by Recon: reason"
                 "123 was banned by Server: other")
        result = await player_admin.get_bans(FakePlugin({"banlist players": glued}))
        self.assertEqual(result["state"], "unrecognized")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["raw"], glued)

    async def test_preview_does_not_send_and_stale_confirmation_is_rejected(self):
        plugin = FakePlugin({"kick Steve test reason": "Kicked Steve", "list": EMPTY})
        params = {"action": "kick", "player": "Steve", "reason": "test reason"}
        status, preview = await call(plugin, "POST", "action", body={**params, "preview": True})
        self.assertEqual((status, preview["command"]), (200, "kick Steve test reason"))
        self.assertEqual(plugin.events, [])
        self.assertFalse(self.audit.exists())
        status, result = await call(plugin, "POST", "action", body={
            **params, "expected_command": "kick Steve",
        })
        self.assertEqual((status, result["ok"]), (400, False))
        self.assertEqual(plugin.events, [])
        self.assertFalse(self.audit.exists())
        status, result = await call(plugin, "POST", "action", body={
            **params, "expected_command": preview["command"],
        })
        self.assertEqual((status, result["confirmed"]), (200, True))
        self.assertGuarded(plugin, "kick Steve test reason", "list")

    async def test_webui_shows_real_ip_while_audit_stays_redacted(self):
        """v0.28.0 口径反转：WebUI 给真 IP，审计继续脱敏。

        为什么这么分：WebUI 是管理员专属页面（服主本来就有 RCON 权限，藏 IP 只会让
        ban-ip 难用）；而审计是长期留档、可能被导出分享的文件，且聊天工具也不经过这里。
        """
        sentinel = "192.0.2.25"
        address_v6 = "2001:db8::25"
        raw = f"No player was found ({sentinel}, [{address_v6}]:25600)"
        plugin = FakePlugin({"kick Steve": raw, "list": EMPTY})
        result = await player_admin.run_action(plugin, "kick", "Steve")
        self.assertFalse(result["confirmed"])
        # WebUI 拿到的回执里，地址是原样的 —— 管理员要靠它判断封哪个 IP
        self.assertIn(sentinel, result["raw"], "WebUI 回执应显示真实 IPv4")
        self.assertIn(address_v6, result["raw"], "WebUI 回执应显示真实 IPv6")
        # 审计那一份仍然脱敏
        audit = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, audit, "审计文件必须继续脱敏")
        self.assertNotIn(address_v6, audit, "审计文件必须继续脱敏")
        self.assertIn("[IP 已隐藏]", audit)

    async def test_write_and_readback_respect_version_guard_denial(self):
        plugin = FakePlugin({"pardon Steve": "Unbanned Steve", "banlist players": NO_BANS})
        plugin.block.add("pardon Steve")
        result = await player_admin.run_action(plugin, "unban", "Steve")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["state"], "unsupported")
        self.assertEqual(plugin.events, [("guard", "pardon Steve", "web_player_admin")])
        plugin.block = {"banlist players"}
        plugin.events.clear()
        result = await player_admin.run_action(plugin, "unban", "Steve")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["state"], "unsupported")
        self.assertEqual(plugin.events, [
            ("guard", "pardon Steve", "web_player_admin"), ("send", "pardon Steve"),
            ("guard", "banlist players", "web_player_admin"),
        ])

    def test_p13_ip_ban_file_is_supported_but_only_that_one(self):
        """v0.28.0 反转：从「禁止读 banned-ips.json」改成「必须读，且只认这一个」。

        原因：WebUI 是管理员专属页，`ban-ip` 是服主真实会用的功能；把 IP 封禁名单
        藏起来，等于让人去翻文件或凭肉眼认日志。
        仍然守住的部分：**只读这一个 IP 相关文件**，不许顺手去读 ops.json / 玩家数据等。
        """
        source = (PLUGIN_DIR / "core" / "player_admin.py").read_text(encoding="utf-8")
        lowered = source.casefold()
        # 精确到「真的去读这个文件」，而不是「文档里提过这个名字」——
        # 否则把路径改坏、注释里还留着这个词，断言依然绿（第一版就漏了）。
        self.assertIn('Path(server_dir) / "banned-ips.json"', source,
                      "P13 必须真的读取 IP 封禁名单")
        for forbidden in ("ops.json", "usercache.json", "banned-players-raw"):
            self.assertNotIn(forbidden, lowered, f"P13 不应触碰 {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
