"""玩家进服 IP 历史搜索、边界和管理员入口。"""
from __future__ import annotations

import asyncio
import gzip
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import _paths  # noqa: F401 — 直接运行时启用 UTF-8 控制台兜底
from test_v0260_player_admin import FakePlugin
from test_v0340_contract import request
from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher, player_admin


def login(name: str, ip: str, clock: str = "12:34:56") -> str:
    return f"[{clock}] [Server thread/INFO]: {name}[/{ip}:52031] logged in with entity id 123\n"


class PlayerIpChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.logs = Path(scratch.name) / "logs"
        self.logs.mkdir()
        self.watcher = log_watcher.LogWatcher(scratch.name, AsyncMock())
        self.plugin = FakePlugin({"list": "There are 1 of a max of 20 players online: Steve"},
                                 server_dir=scratch.name)
        self.plugin._watcher = self.watcher

    def write(self, name: str, contents: str, modified: datetime) -> Path:
        path = self.logs / name
        data = contents.encode("utf-8")
        path.write_bytes(gzip.compress(data) if name.endswith(".gz") else data)
        os.utime(path, (modified.timestamp(), modified.timestamp()))
        return path

    async def test_j1_gzip_is_searched_exactly_and_stops_at_first_matching_file(self):
        latest = self.write("latest.log", login("Steve2", "192.0.2.2")
                            + "[12:35:00] [Server thread/INFO]: <Alex> " + login("Steve", "192.0.2.3"),
                            datetime(2026, 11, 21, 10))
        archive = self.write("2026-11-20-1.log.gz", login("Steve", "192.0.2.4") + login("STEVE", "[::1]", "23:50:08"),
                             datetime(2026, 11, 21, 1, 31))
        self.write("old.log", login("Steve", "192.0.2.5"), datetime(2026, 11, 20, 5))
        with patch.object(log_watcher, "_ip_file_matches", wraps=log_watcher._ip_file_matches) as reads:
            result = (await self.watcher.lookup_player_ips(["Steve"]))["Steve"]
            self.assertEqual((result.state, result.ip), ("found", "::1"), "J1 必须检索 gzip 中的精确玩家")
            self.assertEqual([call.args[0] for call in reads.call_args_list], [latest, archive])
        self.assertEqual(result.scanned, 2)
        self.assertEqual(self.watcher.recent_ips(), {})
        self.watcher.on_event.assert_not_called()

    async def test_j2_restricted_ranges_do_not_offer_ban(self):
        for text, expected in (("127.0.0.1", "loopback"), ("127.8.0.2", "loopback"), ("::1", "loopback"),
                               ("::ffff:127.0.0.1", "loopback"), ("10.4.3.2", "private"),
                               ("172.16.0.1", "private"), ("172.31.255.1", "private"),
                               ("192.168.1.5", "private"), ("169.254.1.2", "link_local"), ("fe80::42", "link_local")):
            with self.subTest(address=text):
                details = player_admin._player_ip_details(log_watcher.LoginIpResult("found", text))
                self.assertEqual((details["scope"], details["can_ban"]), (expected, False),
                                 "J2 受保护地址必须标注并禁用封禁")
        for text in ("192.0.2.9", "172.32.1.2", "2001:db8::42"):
            self.assertTrue(player_admin._player_ip_details(log_watcher.LoginIpResult("found", text))["can_ban"])

    async def test_j3_and_j7_online_contains_source_time_and_count(self):
        self.write("2026-11-20-1.log.gz", login("Steve", "127.0.0.1", "23:50:08"),
                   datetime(2026, 11, 21, 1, 31))
        details = (await player_admin.get_online(self.plugin))["ip_details"]["Steve"]
        self.assertEqual((details.get("source"), details.get("joined_at")),
                         ("logs/2026-11-20-1.log.gz", "2026-11-20T23:50:08"), "J3 来源与进服时间须随名单返回")
        self.assertFalse(details["can_ban"])
        missing = (await self.watcher.lookup_player_ips(["Alex"]))["Alex"]
        self.assertEqual((missing.state, missing.scanned), ("not_found", 1), "J7 缺记录须带实际搜索数")

    async def test_j4_negative_cache_coalesces_calls_and_expires(self):
        self.write("latest.log", login("Alex", "192.0.2.10"), datetime(2026, 11, 21, 13))
        with patch.object(log_watcher, "_scan_login_ips", wraps=log_watcher._scan_login_ips) as scan:
            results = await asyncio.gather(player_admin.get_online(self.plugin), player_admin.get_online(self.plugin))
            self.assertTrue(all(r["ip_details"]["Steve"]["state"] == "not_found" for r in results))
            await player_admin.get_online(self.plugin)
            self.assertEqual(scan.call_count, 1, "J4 同一玩家 60 秒内只搜索一次")
            expiry, value = self.watcher._ip_search_cache["steve"]
            self.assertGreater(expiry - time.monotonic(), 58)
            self.watcher._ip_search_cache["steve"] = (time.monotonic() - 1, value)
            await player_admin.get_online(self.plugin)
            self.assertEqual(scan.call_count, 2, "J4 过期后须允许再次搜索")

    async def test_j6_manual_route_bypasses_cached_miss_and_requires_admin(self):
        await player_admin.get_online(self.plugin)
        self.write("latest.log", login("Steve", "192.0.2.9") + login("Alex", "192.0.2.10"), datetime(2026, 11, 21, 13))
        self.assertEqual((await player_admin.get_online(self.plugin))["ip_details"]["Steve"]["state"], "not_found")
        status, _, denied = await request(self.plugin, "POST", "player/ip/search",
                                           username="api_key:test", body={"player": "Steve"})
        self.assertEqual(status, 403)
        self.assertNotIn("details", denied)
        status, headers, reply = await request(self.plugin, "POST", "player/ip/search", body={"player": "Steve"})
        self.assertEqual(status, 200)
        self.assertEqual(reply["details"]["ip"], "192.0.2.9", "J6 手动重搜须越过缓存")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("192.0.2.10", str(reply))
        status, _, _ = await request(self.plugin, "POST", "player/ip/search", body={"player": "Steve/"})
        self.assertEqual(status, 400)

    async def test_live_record_wins_when_it_arrives_during_search(self):
        async def scan(*args, **kwargs):
            self.watcher._note_login_ip(login("Steve", "192.0.2.20"))
            return True, {"steve": log_watcher.LoginIpResult("found", "192.0.2.9",
                                                            "logs/latest.log", "2099-11-20T12:34:56", 1)}

        with patch.object(log_watcher, "run_blocking", side_effect=scan):
            result = (await self.watcher.lookup_player_ips(["Steve"], force=True))["Steve"]
        self.assertEqual(result.ip, "192.0.2.20")
        self.assertEqual(self.watcher.recent_ip("Steve"), "192.0.2.20")
        self.assertEqual((await self.watcher.lookup_player_ips(["Steve"]))["Steve"].ip, "192.0.2.20")

    async def test_remote_and_unreadable_have_distinct_states(self):
        with patch.object(Path, "iterdir", side_effect=PermissionError):
            details = await player_admin.search_player_ip(self.plugin, "Steve")
        self.assertEqual(details["state"], "unreadable")
        self.plugin.server_dir = ""
        with patch.object(log_watcher, "_scan_login_ips") as scan:
            details = await player_admin.search_player_ip(self.plugin, "Steve")
        self.assertEqual(details["state"], "remote")
        scan.assert_not_called()

    async def test_gzip_read_cap_and_corrupt_archive(self):
        self.write("big.log.gz", "x" * 120 + "\n" + login("Steve", "192.0.2.20"), datetime(2026, 11, 21, 15))
        with patch.object(log_watcher, "IP_SEARCH_BYTES", 100):
            details = await player_admin.search_player_ip(self.plugin, "Steve")
        self.assertEqual((details["state"], details["limited"], details["scanned"]), ("not_found", True, 1))
        bad = self.logs / "broken.log.gz"
        bad.write_bytes(b"invalid gzip")
        details = await player_admin.search_player_ip(self.plugin, "Missing")
        self.assertEqual((details["state"], details["scanned"]), ("incomplete", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
