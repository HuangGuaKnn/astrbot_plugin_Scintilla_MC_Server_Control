"""Web 控制台的公开接口契约，使用假 RCON 和真实请求上下文。"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
from starlette.requests import Request

from _paths import PLUGIN_DIR
from test_v0260_player_admin import ADMIN, OTHER, SECRET, FakePlugin

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME
from astrbot_plugin_Scintilla_MC_Server_Control.core import server_card, web_console
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi, PAGE_PREFIX


ONLINE = "There are 1 of a max of 20 players online: Alex"
UNKNOWN = 'Unknown command. Type "/help" for help.'
STOP = "Stopping the server"
IPV4 = "192.0.2.9"
IPV6 = "2001:db8::42"
IPV6_PLAIN_PORT = "2001:db8:0:1:2:3:4:5"
LOGIN = (f"Alex[/{IPV4}:52031] logged in; Steve[/[{IPV6}]:52032] logged in; "
         f"Sam[/{IPV6_PLAIN_PORT}:52033] logged in")


class ConsolePlugin(FakePlugin):
    def __init__(self, replies=None, *, whitelist=False, server_dir=""):
        super().__init__(replies, server_dir=server_dir)
        self.whitelist = whitelist

    def _cfg(self, key, default=None):
        if key == "web_console_whitelist_mode":
            return self.whitelist
        return super()._cfg(key, default)


async def call(plugin, endpoint, command=None, *, username=ADMIN, principal=None,
               proof="bearer", token_type=None, query="", fetch_site=None,
               content_type="application/json", settings=None):
    """通过实际注册的路由进入请求上下文，隔离真实 AstrBot 与 RCON。"""
    McControlWebApi(plugin).register()
    method = "GET" if endpoint == "log" else "POST"
    path = endpoint if endpoint == "settings/save" else f"console/{endpoint}"
    route = f"{PAGE_PREFIX}/{path}"
    prefix = "/api/plug/" if proof == "legacy" else "/api/v1/plugins/extensions/"
    headers = [(b"content-type", content_type.encode("ascii")), (b"host", b"test.local")]
    if fetch_site:
        headers.append((b"sec-fetch-site", fetch_site.encode("ascii")))
    if username and proof in {"bearer", "cookie", "forged"}:
        claims = {"username": username, "exp": datetime.now(timezone.utc) + timedelta(minutes=1)}
        if token_type:
            claims["token_type"] = token_type
        key = SECRET if proof != "forged" else "different-console-test-key-with-32-bytes"
        token = jwt.encode(claims, key, algorithm="HS256")
        if proof == "cookie":
            headers.append((b"cookie", f"{DASHBOARD_JWT_COOKIE_NAME}={token}".encode("ascii")))
        else:
            headers.append((b"authorization", f"Bearer {token}".encode("ascii")))

    async def receive():
        body = {"command": command, "actor": ADMIN, "username": ADMIN}
        if settings is not None:
            body = {"settings": settings}
        return {"type": "http.request", "body": json.dumps(body).encode("utf-8")}

    request = Request(
        {"type": "http", "scheme": "http", "method": method, "path": prefix + route,
         "headers": headers, "query_string": query.encode("ascii")},
        receive,
    )
    with bind_request_context(PluginRequest(request, username=principal or username)):
        response = await plugin.routes[method, route]()
    return response.status_code, json.loads(response.body)


class WebConsoleCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name)
        self.audit = self.path / "console_audit.jsonl"
        redirected_audit = patch.object(web_console, "_audit_path", return_value=self.audit)
        redirected_audit.start()
        self.addCleanup(redirected_audit.stop)

    def assert_redacted(self, payload, message):
        rendered = json.dumps(payload, ensure_ascii=False)
        for address in (IPV4, IPV6, IPV6_PLAIN_PORT):
            self.assertNotIn(address, rendered, message)

    async def test_t01_send_stop_and_exact_command(self):
        written = "minecraft:say  矩阵  验证 "
        plugin = ConsolePlugin({"stop": STOP, written: "矩阵  验证"})
        for command, raw in plugin.replies.items():
            with self.subTest(command=command):
                code, payload = await call(plugin, "execute", command)
                self.assertEqual((code, payload["mode"]), (200, "full"))
                result, = payload["results"]
                self.assertEqual(result["command"], command, "T1 保留命令的空格与命名空间")
                self.assertEqual(result["raw"], raw, "T1 服务端回执须保留")
        self.assertEqual(
            [(kind, command) for kind, command, *_ in plugin.events],
            [("guard", "stop"), ("send", "stop"), ("guard", written), ("send", written)],
            "T1 版本守卫后按收到的原命令交给 RCON",
        )

    async def test_t02_indirect_text_in_preview(self):
        examples = (
            ("execute as @a run stop", "stop"),
            ('execute as @e[name="Alice run Bob"] run execute at @s run stop',
             "execute at @s run stop"),
            ("function test:daily", "test:daily"),
            ("schedule function test:daily 1t", "test:daily"),
        )
        plugin = ConsolePlugin()
        for command, nested in examples:
            with self.subTest(command=command):
                code, payload = await call(plugin, "preview", command)
                self.assertEqual(code, 200)
                result, = payload["results"]
                warning = [row["text"] for row in result["warnings"] if row["code"] == "indirect"]
                self.assertTrue(warning and nested in warning[0], "T2 提示须显示完整的嵌套命令或函数名")
        self.assertEqual(plugin.events, [], "T2 预览不会调用 RCON")
        self.assertFalse(self.audit.exists(), "T2 预览不会写执行审计")

    async def test_t03_multiline_preserves_order_and_replies(self):
        plugin = ConsolePlugin({"list": ONLINE, "stop": STOP})
        code, preview = await call(plugin, "preview", "list\n\nstop")
        self.assertEqual(code, 200)
        self.assertEqual([row["command"] for row in preview["results"]], ["list", "stop"],
                         "T3 预览按顺序列出非空行")
        self.assertIn("multiline", {row["code"] for row in preview["warnings"]}, "T3 预览标注多行")
        self.assertEqual(plugin.events, [], "T3 多行预览不得提前执行")
        code, payload = await call(plugin, "execute", "list\r\n\r\nstop")
        self.assertEqual(code, 200)
        self.assertEqual(
            [event[1] for event in plugin.events if event[0] == "send"],
            ["list", "stop"], "T3 多行逐条按输入顺序发送",
        )
        self.assertEqual([(row["command"], row["raw"]) for row in payload["results"]],
                         [("list", ONLINE), ("stop", STOP)], "T3 多行逐条返回回执")

    async def test_t04_selector_scope_strength_and_filter(self):
        samples = (("@e", "selector_e", "high"),
                   ("@e[type=item]", "selector_e", "high"),
                   ("@a", "selector_a", "high"), ("@r", "selector_r", "medium"),
                   ("@p", "selector_p", "medium"), ("@n", "selector_n", "low"))
        for selector, key, level in samples:
            with self.subTest(selector=selector):
                _, payload = await call(ConsolePlugin(), "preview", f"kill {selector}")
                warnings = payload["results"][0]["warnings"]
                hint = next((row for row in warnings if row["code"] == key), {})
                self.assertEqual(hint.get("level"), level, "T4 目标选择器须给出对应提示强度")
                self.assertIn(selector, hint["text"], "T4 提示须保留选择器与参数")

    async def test_t05_literals_do_not_trigger_selector_warning(self):
        for command in ('say "大家好 @a"', 'say "大家好 @a 欢迎"', r'say "欢迎 \" @e"', "say somebody@a",
                        'tellraw Alex {"text":"看这个 @e"}'):
            with self.subTest(command=command):
                _, payload = await call(ConsolePlugin(), "preview", command)
                codes = {row["code"] for row in payload["results"][0]["warnings"]}
                self.assertTrue("broadcast" in codes, "T5 广播预览须保留广播提示")
                self.assertTrue(all(not code.startswith("selector_") for code in codes),
                                "T5 引号内或单词内部的文本不可触发选择器提示")
        _, payload = await call(ConsolePlugin(), "preview", 'tellraw @a {"text":"看这个 @e"}')
        codes = {row["code"] for row in payload["results"][0]["warnings"]}
        self.assertIn("selector_a", codes, "T5 引号外的目标参数仍须提示")
        self.assertNotIn("selector_e", codes, "T5 JSON 字符串的选择器不应提示")

    async def test_t06_response_states(self):
        fixtures = {
            "list": (ONLINE, "ok"),
            "whiteelephant": (UNKNOWN, "unsupported"),
            "give Alex stone": ("", "unrecognized"),
            "seed": (TimeoutError("RCON wait timed out"), "unreachable"),
        }
        plugin = ConsolePlugin({command: raw for command, (raw, _) in fixtures.items()})
        code, payload = await call(plugin, "execute", "\n".join(fixtures))
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["results"]), len(fixtures), "T6 每条回执须有自己的状态")
        for result, (command, (raw, state)) in zip(payload["results"], fixtures.items(), strict=True):
            with self.subTest(command=command):
                self.assertEqual(result["state"], state, "T6 四态须沿用共享判定")
                self.assertEqual(result["state_label"], server_card.STATE_LABEL[state])
                self.assertIn("raw", result, "T6 即使是空回包也须给出原文字段")
                self.assertEqual(result["raw"], raw if isinstance(raw, str) else "")
                if isinstance(raw, TimeoutError):
                    self.assertIn("timed out", result["note"])

    async def test_t07_every_line_is_audited_even_when_rejected(self):
        raw = "x" * 196 + f" {IPV4} {IPV6} " + "y" * 30
        plugin = ConsolePlugin({"list": raw}, whitelist=True)
        plugin.block.add("seed")
        code, payload = await call(plugin, "execute", "list\nstop\nseed")
        self.assertEqual(code, 200)
        self.assertTrue(self.audit.is_file(), "T7 执行请求必须落 JSONL 审计")
        rows = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["command"] for row in rows], ["list", "stop", "seed"],
                         "T7 每条命令包括版本或模式拒绝均留一条审计")
        for row, result in zip(rows, payload["results"], strict=True):
            self.assertTrue(row["ts"])
            self.assertEqual((row["actor"], row["mode"]), (ADMIN, "whitelist"), "T7 审计记录身份和模式")
            self.assertEqual(row["state"], result["state"], "T7 审计状态取自对应命令回执")
            self.assertEqual(row["warnings"],
                             [hint["code"] for hint in payload["warnings"] + result["warnings"]],
                             "T7 审计保存提示命中项")
        self.assertEqual([row["accepted"] for row in rows], [True, False, False],
                         "T7 审计保存每行是否被接收")
        self.assertIn("service", rows[1]["warnings"], "T7 审计需保留被拒命令的命中提示")
        self.assertEqual(rows[0]["raw_excerpt"], payload["results"][0]["raw"][:200],
                         "T7 回执先脱敏、再截取 200 字符")
        self.assertEqual(len(rows[0]["raw_excerpt"]), 200, "T7 审计原文上限 200 字符")

    async def test_t08_log_missing_empty_local_remote_and_rotated(self):
        _, remote = await call(ConsolePlugin(), "log")
        self.assertEqual(remote["state"], "unsupported", "T8 远程 RCON 没有本地日志来源")
        plugin = ConsolePlugin(server_dir=str(self.path))
        _, missing = await call(plugin, "log")
        self.assertEqual(missing["state"], "unreachable", "T8 日志文件缺失须单独报告")
        log = self.path / "logs" / "latest.log"
        log.parent.mkdir()
        log.touch()
        _, empty = await call(plugin, "log")
        self.assertEqual(empty["state"], "unrecognized", "T8 无可显示日志行须单独报告")
        log.write_text("你好\n正在运行\n", encoding="utf-8")
        _, current = await call(plugin, "log")
        self.assertEqual((current["state"], current["lines"]), ("ok", ["你好", "正在运行"]),
                         "T8 本地日志快照保持行序和编码")
        with log.open("a", encoding="utf-8") as stream:
            stream.write("新日志\n")
        _, appended = await call(plugin, "log", query=f"cursor={current['cursor']}")
        self.assertEqual(appended["state"], "ok", "T8 追加日志不应报告轮转")
        self.assertEqual(appended["lines"][-1], "新日志", "T8 轮询回包须反映新增日志行")
        replacement = log.with_name("replacement.log")
        replacement.write_text("新会话\n", encoding="utf-8")
        replacement.replace(log)
        _, rotated = await call(plugin, "log", query=f"cursor={appended['cursor']}")
        self.assertEqual(rotated["state"], "unrecognized", "T8 日志轮转须单独报告")
        _, resumed = await call(plugin, "log", query=f"cursor={rotated['cursor']}")
        self.assertEqual((resumed["state"], resumed["lines"]), ("ok", ["新会话"]),
                         "T8 轮转后的同一文件恢复为正常快照")

    async def test_t09_permission_applies_to_all_routes(self):
        plugin = ConsolePlugin({"list": ONLINE})
        for endpoint in ("preview", "execute", "log"):
            for options in ({"username": None}, {"username": OTHER}, {"username": "api_key:synthetic"},
                            {"proof": "forged"}, {"proof": "missing"},
                            {"username": OTHER, "principal": ADMIN},
                            {"token_type": "plugin_page_asset"}, {"proof": "legacy", "username": OTHER},
                            {"proof": "cookie", "fetch_site": "cross-site"},
                            {"proof": "cookie", "fetch_site": "same-site"}):
                with self.subTest(endpoint=endpoint, options=options):
                    status, result = await call(plugin, endpoint, "list", **options)
                    self.assertEqual(status, 403, "T9 每条控制台路由均拒绝非管理员或无效令牌")
                    self.assertIs(result["authorized"], False)
        self.assertEqual(plugin.events, [], "T9 权限拒绝不得触发 RCON")
        self.assertFalse(self.audit.exists(), "T9 权限拒绝不得把来路不明的输入记成管理员操作")
        for proof in ("bearer", "cookie", "legacy"):
            status, result = await call(plugin, "preview", "list", proof=proof, fetch_site="same-origin")
            self.assertEqual((status, result["authorized"]), (200, True),
                             "T9 Dashboard 管理员身份可经合法入口操作")
        status, _ = await call(plugin, "execute", "list", proof="cookie", content_type="text/plain")
        self.assertEqual(status, 415, "T9 执行命令要求 JSON 请求")
        self.assertEqual(plugin.events, [], "T9 非 JSON 请求不得发送命令")

    async def test_t09_whitelist_settings_write_requires_dashboard_admin(self):
        plugin = ConsolePlugin()
        settings = {"web_console_whitelist_mode": "true"}
        with patch.object(plugin, "_set_cfg_batch", create=True) as save:
            for options in ({"username": OTHER}, {"token_type": "plugin_page_asset"},
                            {"proof": "cookie", "fetch_site": "same-site"}):
                status, _ = await call(plugin, "settings/save", settings=settings, **options)
                self.assertEqual(status, 403, "T9 非管理员不能关闭或开启白名单模式")
            status, _ = await call(plugin, "settings/save", settings=settings, content_type="text/plain")
            self.assertEqual(status, 415, "T9 模式设置要求 JSON")
            save.assert_not_called()
            status, response = await call(plugin, "settings/save", settings=settings, fetch_site="same-origin")
            self.assertEqual((status, response["applied"]), (200, {"web_console_whitelist_mode": True}),
                             "T9 管理员可保存布尔设置")
            save.assert_called_once_with({"web_console_whitelist_mode": True})

    async def test_t10_addresses_removed_from_replies_and_logs(self):
        plugin = ConsolePlugin({"say hello": LOGIN, "seed": TimeoutError(LOGIN)}, server_dir=str(self.path))
        _, reply = await call(plugin, "execute", "say hello\nseed")
        self.assert_redacted(reply, "T10 回执或错误说明包含未脱敏的地址")
        self.assertIn("Alex", reply["results"][0]["raw"], "T10 原文保留地址以外的有效数据")
        self.assertIn("[IP 已隐藏]", reply["results"][0]["raw"], "T10 回执须替换地址")
        self.assert_redacted(self.audit.read_text(encoding="utf-8"), "T10 审计包含未脱敏的地址")
        log = self.path / "logs" / "latest.log"
        log.parent.mkdir()
        log.write_text(f"first\n{LOGIN}\nlast\n", encoding="utf-8")
        _, tail = await call(plugin, "log")
        self.assertEqual(tail["state"], "ok")
        self.assert_redacted(tail, "T10 日志尾包含未脱敏的地址")
        self.assertIn("[IP 已隐藏]", tail["lines"][1], "T10 日志须替换地址")

    async def test_t10_long_non_address_is_left_intact(self):
        raw = "identifier 12ab:" + "9" * 5000
        _, payload = await call(ConsolePlugin({"say hello": raw}), "execute", "say hello")
        self.assertTrue(payload["results"][0]["raw"] == raw,
                        "T10 过长的数字字段不应中断 IP 识别或清理合法原文")

    def test_t12_console_has_no_llm_context_dependency(self):
        """守住新增 Python 代码的 LLM 边界，且包含三个 Web 入口。"""
        core = ast.parse((PLUGIN_DIR / "core" / "web_console.py").read_text(encoding="utf-8"))
        api = ast.parse((PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8-sig"))
        names = {"console_preview", "console_execute", "console_log", "_console_command", "_console_response"}
        methods = [node for node in ast.walk(api)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.assertEqual({node.name for node in methods}, names,
                         "T12 静态检查必须覆盖三条路由及共用处理函数")
        forbidden_names = {"context", "llm", "prompt", "prompts", "provider", "agent", "agents",
                           "workflow", "conversation", "chat", "messages", "completion"}
        nodes = [node for root in [core, *methods] for node in ast.walk(root)]
        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                for module_part in (node.module or "").split("."):
                    self.assertNotIn(module_part, forbidden_names, "T12 控制台不可导入 LLM 上下文接口")
            elif isinstance(node, ast.alias):
                for module_part in node.name.split("."):
                    self.assertNotIn(module_part, forbidden_names, "T12 控制台不可导入 LLM 上下文接口")
            elif isinstance(node, ast.Attribute):
                name_parts = set(node.attr.lower().split("_"))
                self.assertFalse(name_parts & forbidden_names, "T12 控制台不可向 LLM 上下文送入回执")

    async def test_t13_length_limits_and_whitelist(self):
        non_broadcast = "custom " + "x" * 249
        broadcast = "say " + "测" * 196
        nested_broadcast = f"execute as @a run {broadcast}"
        allowed = [non_broadcast, broadcast, nested_broadcast]
        rejected = [command + "x" for command in allowed]
        plugin = ConsolePlugin({command: "test reply" for command in allowed})
        code, result = await call(plugin, "execute", "\n".join(allowed + rejected))
        self.assertEqual(code, 200)
        self.assertEqual([event[1] for event in plugin.events if event[0] == "send"], allowed,
                         "T13 只送出 256 字与 200 字边界内的命令，嵌套广播也受限")
        self.assertEqual([row["accepted"] for row in result["results"]],
                         [True] * len(allowed) + [False] * len(rejected), "T13 超长逐条报告未接受")
        self.assertEqual((await call(plugin, "execute", ["list"]))[0], 400, "T13 非字符串输入须拒绝")
        self.assertEqual((await call(plugin, "execute", "\n" * 30))[0], 400, "T13 空请求须拒绝")
        self.assertEqual((await call(plugin, "execute", "list\n" * 21))[0], 400, "T13 批量条数须有上限")

        plugin = ConsolePlugin({"list": ONLINE, "time query daytime": "The time is 123"}, whitelist=True)
        code, result = await call(plugin, "execute",
                                  "list\ntime query daytime\nwhitelist off\nlist surplus\nexecute run list")
        self.assertEqual((code, result["mode"]), (200, "whitelist"))
        self.assertEqual([event[1] for event in plugin.events if event[0] == "send"],
                         ["list", "time query daytime"], "T13 可选白名单仅允许声明的诊断形态")

    async def test_t13_whitelist_disallows_non_diagnostic_rcon_sentinel(self):
        plugin = ConsolePlugin({"list": ONLINE}, whitelist=True)
        plugin.end_mode = "sentinel"
        plugin.probe_command = "save-all"
        code, payload = await call(plugin, "execute", "list")
        self.assertEqual(code, 200)
        result, = payload["results"]
        self.assertEqual((result["accepted"], result["state"]), (False, "unsupported"),
                         "T13 非诊断哨兵命令不可绕过白名单")
        self.assertIn("探测命令", result["note"])
        self.assertEqual(plugin.events, [], "T13 非诊断哨兵与业务命令均不得发送")
        row = json.loads(self.audit.read_text(encoding="utf-8"))
        self.assertEqual((row["command"], row["accepted"]), ("list", False),
                         "T13 阻止哨兵时必须审计被拒绝的命令")
        for behavior, probe in (("sentinel", ""), ("sentinel", "list"), ("idle", "save-all")):
            with self.subTest(behavior=behavior, probe=probe):
                plugin.end_mode, plugin.probe_command = behavior, probe
                _, payload = await call(plugin, "execute", "list")
                self.assertTrue(payload["results"][0]["accepted"], "合法探测形态仍可正常诊断")

    async def test_t14_soft_hints_never_block_dispatch(self):
        command = "execute as @a at @e[type=item] run stop"
        plugin = ConsolePlugin({command: STOP, "list": ONLINE})
        code, payload = await call(plugin, "execute", f"{command}\nlist")
        self.assertEqual(code, 200)
        result = payload["results"][0]
        codes = {hint["code"] for hint in result["warnings"]}
        self.assertTrue({"indirect", "service", "selector_a", "selector_e"} <= codes,
                        "T14 样本须同时命中命令、间接执行和选择器提示")
        self.assertIn("multiline", {hint["code"] for hint in payload["warnings"]})
        self.assertEqual([event[1] for event in plugin.events if event[0] == "send"],
                         [command, "list"], "T14 多种提示同时命中仍须发出两条命令")
        self.assertIs(result["accepted"], True, "T14 提示不能变成拒绝执行")
        self.assertEqual(result["raw"], STOP, "T14 有提示的命令也须显示原文")


if __name__ == "__main__":
    unittest.main(verbosity=2)
