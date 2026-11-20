"""R1–R4 的路由权限、刷新档位与页面字面 ID 契约。"""
from __future__ import annotations

import json
import re
import unittest
from collections import Counter
from html.parser import HTMLParser

from _paths import PLUGIN_DIR
from test_v0260_player_admin import (
    ADMIN, FakePlugin, McControlWebApi, PAGE_PREFIX, PluginRequest, Request,
    bind_request_context,
)

from astrbot_plugin_Scintilla_MC_Server_Control.core import web_console


class ElementIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = Counter()

    def handle_starttag(self, _tag, attrs):
        self.ids.update(value for key, value in attrs if key == "id" and value)


class SettingsPlugin(FakePlugin):
    def __init__(self):
        super().__init__()
        self.saved = {}
        self._last_cfg_save_ok = True
        self._recent_events = [{"type": "chat", "player": "Alex", "detail": "聊天文本", "ts": "09:04:00"}]

    def _cfg(self, key, default=None):
        return self.saved.get(key, super()._cfg(key, default))

    def _set_cfg_batch(self, values):
        self.saved.update(values)
        return list(values)


async def request(plugin, method, path, *, body=None, username=ADMIN, site="same-origin"):
    McControlWebApi(plugin).register()
    endpoint = f"{PAGE_PREFIX}/{path}"

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode("utf-8")}

    host = Request({
        "type": "http", "method": method, "path": f"/api/plug/{endpoint.lstrip('/')}",
        "headers": [(b"content-type", b"application/json"), (b"sec-fetch-site", site.encode("ascii"))],
        "query_string": b"",
    }, receive)
    with bind_request_context(PluginRequest(host, username=username)):
        response = await plugin.routes[method, endpoint]()
    return response.status_code, response.headers, json.loads(response.body)


class RcContracts(unittest.IsolatedAsyncioTestCase):
    async def test_r1_chat_route_uses_admin_and_recent_events(self):
        plugin = SettingsPlugin()
        status, headers, payload = await request(plugin, "GET", "events/chat")
        self.assertEqual((status, payload["events"]), (200, plugin._recent_events))
        self.assertEqual(headers["cache-control"], "no-store")
        for user, site in (("api_key:test", "same-origin"), (ADMIN, "cross-site")):
            status, _, payload = await request(plugin, "GET", "events/chat", username=user, site=site)
            self.assertEqual((status, payload.get("authorized")), (403, False))
            self.assertNotIn("events", payload)

    def test_r3_page_ids_resolve(self):
        html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
        parser = ElementIds()
        parser.feed(html)
        self.assertFalse([key for key, count in parser.ids.items() if count > 1])
        refs = set(re.findall(r"""\$\(\s*["']([\w:-]+)["']\s*\)""", html))
        # 两个历史 ID 在预设的条件模板里生成，引用点有存在性保护。
        dynamic = {"pre_notice_on", "rn_in"}
        self.assertTrue(all(re.search(rf'id=["\']{element}["\']', html) for element in dynamic))
        self.assertEqual(refs - parser.ids.keys() - dynamic, set(), "R3 新增了悬空的字面 DOM 引用")

    def test_monitor_poll_schema_matches_settings_validation(self):
        key = "server_monitor_poll_seconds"
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        config = schema["connection"]["items"][key]
        api = McControlWebApi(SettingsPlugin())
        self.assertEqual(config["default"], "10")
        self.assertEqual(config["options"], ["5", "10", "30", "60"])
        for interval in config["options"]:
            self.assertEqual(api._validate_settings({key: interval}), ({key: interval}, None))
        for interval in ("0", "2", "1.5", None):
            parsed, error = api._validate_settings({key: interval})
            self.assertIsNone(parsed)
            self.assertIn(key, error)

    async def test_r4_schema_and_persistence_routes(self):
        path = "settings/console-log"
        key = web_console.LOG_POLL_KEY
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        item = schema["connection"]["items"][key]
        self.assertEqual(item["options"], list(web_console.LOG_POLL_OPTIONS))
        self.assertEqual(item["default"], "1000")
        self.assertEqual(len(schema), 10)
        self.assertEqual(sum(len(group["items"]) for group in schema.values()), 103)
        plugin = SettingsPlugin()
        status, _, payload = await request(plugin, "GET", path)
        self.assertEqual((status, payload["interval"]), (200, "1000"))
        for value in item["options"]:
            status, _, payload = await request(plugin, "POST", path + "/save", body={"interval": value})
            self.assertEqual((status, payload), (200, {"ok": True, "interval": value}))
            self.assertEqual(plugin.saved, {key: value})
        for value in ("300", None, ["1000"]):
            _, _, payload = await request(plugin, "POST", path + "/save", body={"interval": value})
            self.assertFalse(payload["ok"])
            self.assertEqual(plugin.saved, {key: "3000"})
        status, _, _ = await request(plugin, "POST", path + "/save", body={"interval": "1000"}, site="same-site")
        self.assertEqual(status, 403)
        self.assertEqual(plugin.saved, {key: "3000"})
        plugin._last_cfg_save_ok = False
        _, _, payload = await request(plugin, "POST", path + "/save", body={"interval": "1000"})
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
