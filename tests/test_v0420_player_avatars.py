"""Zulu 8 离线 UUID 真值和两个在线名单入口的头像协议。"""
from __future__ import annotations

import json
import re
import unittest

from test_v0260_player_admin import FakePlugin

from astrbot_plugin_Scintilla_MC_Server_Control.core import player_admin, skins
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi
from _paths import PLUGIN_DIR


# Keep the supplied mixed-case input without a private name in source.
MIXED_CASE_SAMPLE = bytes.fromhex("506972696b6145324531").decode("ascii")

VECTORS = {
    "Alex_Craft": ("e915951d-c45c-3f26-9e4e-ebd1d6528a28", "steve"),
    "Alex": ("36532b5e-c442-3dbb-a24c-c7e55d0f979a", "steve"),
    "Steve": ("5627dd98-e6be-3c21-b8a8-e92344183641", "alex"),
    "Notch": ("b50ad385-829d-3141-a216-7e7d7539ba7f", "steve"),
    MIXED_CASE_SAMPLE: ("472d2a6c-1c23-3ae2-a139-ab6c7c521c8f", "alex"),
    "Player": ("a01e3843-e521-3998-958a-f459800e4d11", "alex"),
    MIXED_CASE_SAMPLE.lower(): ("b831a7a4-54a6-3ced-9013-210a837cc6c0", "alex"),
}


class Server(FakePlugin):
    def _version_capabilities(self):
        return {"source": "override", "raw": "1.20.6", "known": True}


class AvatarChecks(unittest.IsolatedAsyncioTestCase):
    def test_java_uuid_and_model(self):
        for name, (player_uuid, model) in VECTORS.items():
            with self.subTest(name=name):
                self.assertEqual(str(skins.offline_uuid(name)), player_uuid, "AV1 UUID 逐条匹配 Java 真值")
                self.assertEqual(skins.default_model(name), model, "AV1 默认模型逐条匹配 Java 真值")

    def test_deterministic(self):
        for name in VECTORS:
            self.assertEqual(skins.avatar_metadata(name), skins.avatar_metadata(name),
                             "AV2 同名头像属性必须稳定")

    def test_no_network(self):
        source = (PLUGIN_DIR / "core" / "skins.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b(?:requests|urllib|http|https|httpx|aiohttp|socket)\b", source),
                          "AV3 服务端头像算法禁止使用网络")

    async def test_online_payloads(self):
        plugin = Server({
            "list": "There are 2 of a max of 20 players online: Steve, Alex",
            "time query daytime": "The time is 1000",
            "tps": "TPS: 20.0",
        })
        expected = {
            name: {"uuid": VECTORS[name][0], "default_model": VECTORS[name][1]}
            for name in ("Steve", "Alex")
        }
        managed = await player_admin.get_online(plugin)
        self.assertEqual(managed["players"], ["Steve", "Alex"])
        self.assertEqual(managed.get("avatars"), expected, "AV4 管理名单须带头像属性")
        self.assertEqual(plugin.events, [("guard", "list", "web_player_admin"), ("send", "list")])

        plugin.events.clear()
        response = await McControlWebApi(plugin).get_server_status()
        public = json.loads(response.body)["list"]
        self.assertEqual(public["players"], managed["players"])
        self.assertEqual(public.get("avatars"), expected, "AV5 只读名单须带头像属性")
        self.assertEqual([item for item in plugin.events if item == ("send", "list")], [("send", "list")])


if __name__ == "__main__":
    unittest.main()
