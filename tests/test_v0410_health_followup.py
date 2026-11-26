"""日志孤儿任务与玩家审计尾读的回归判据。"""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _paths import add_sys_paths

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher, player_admin, web_api


class OrphanChecks(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_cancels_oldest(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = log_watcher.LogWatcher(Path(directory) / "latest.log", on_event=lambda _event: None)
            tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(10)]
            await asyncio.sleep(0)
            try:
                with patch.object(log_watcher.logger, "warning") as warning:
                    for task in tasks:
                        watcher._track_orphan(task)
                    await asyncio.sleep(0)
                self.assertEqual(watcher.health()["orphaned_tasks"], 4, "H3 孤儿任务须受硬上限约束")
                self.assertEqual(list(watcher._orphaned), tasks[-4:])
                self.assertEqual([task for task in tasks if task.cancelled()], tasks[:6])
                self.assertEqual(warning.call_count, 6)
                for call in warning.call_args_list:
                    template, reason, count, age = call.args
                    self.assertIn("已等待", template)
                    self.assertEqual((reason, count), ("超过上限", 5))
                    self.assertGreaterEqual(age, 0)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(watcher.health()["orphaned_tasks"], 0)

    async def test_timeout_and_natural_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = log_watcher.LogWatcher(Path(directory) / "latest.log", on_event=lambda _event: None)
            task = asyncio.create_task(asyncio.Event().wait())
            try:
                with patch.object(log_watcher, "ORPHAN_TIMEOUT", 0), patch.object(log_watcher.logger, "warning") as warning:
                    watcher._track_orphan(task)
                    done, _ = await asyncio.wait({task}, timeout=0.2)
                self.assertIn(task, done, "H5 孤儿任务超时后须请求取消")
                self.assertTrue(task.cancelled())
                self.assertEqual(warning.call_count, 1)
                self.assertEqual(warning.call_args.args[1:3], ("超时", 1))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            done_task = asyncio.create_task(asyncio.sleep(0))
            watcher._track_orphan(done_task)
            _, timer = watcher._orphaned[done_task]
            await done_task
            self.assertTrue(timer.cancelled())
            self.assertEqual(watcher.health()["orphaned_tasks"], 0)


def audit_record(index):
    return {
        "ts": "2026-11-25T00:00:00+08:00", "actor": "控制台", "action": "kick",
        "player": "Steve", "command": "kick Steve", "state": "ok",
        "confirmed": True, "raw_excerpt": f"第 {index} 条记录 " + "x" * 80,
    }


class AuditChecks(unittest.IsolatedAsyncioTestCase):
    def test_large_tail_matches_forward_scan_and_reads_few_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            repeated = json.dumps(audit_record(0), ensure_ascii=False).encode() + b"\n"
            with path.open("wb") as stream:
                for _ in range(200):
                    stream.write(repeated * 1000)
                for index in range(1, 24):
                    stream.write(json.dumps(audit_record(index), ensure_ascii=False).encode() + b"\r\n")
                    stream.write(b'\nnot json\n{"mode":"whitelist","accepted":true}\n')
                stream.write(json.dumps(audit_record(24), ensure_ascii=False).encode())
            with path.open(encoding="utf-8") as stream:
                expected = deque(maxlen=20)
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("confirmed"), bool):
                        expected.append(row)

            total_bytes = 0

            @contextmanager
            def open_counted(*args, **kwargs):
                with io.open(*args, **kwargs) as stream:
                    real_read = stream.read

                    def read(size=-1):
                        nonlocal total_bytes
                        data = real_read(size)
                        total_bytes += len(data)
                        return data

                    yield SimpleNamespace(seek=stream.seek, read=read)

            with patch.object(player_admin, "_audit_path", return_value=path), patch.object(
                player_admin, "io", SimpleNamespace(open=open_counted, SEEK_END=io.SEEK_END),
            ):
                actual = player_admin.get_audit(20)
            self.assertEqual(actual, list(reversed(expected)))
            size = path.stat().st_size
            self.assertGreater(size, 45_000_000)
            self.assertTrue(0 < total_bytes < size // 100, "H4 审计读尾字节数须远小于文件")
            print(f"审计尾读 {total_bytes:,} / {size:,} 字节，最近 20 条逐条一致")

    def test_split_utf8_crlf_and_missing_or_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch.object(player_admin, "_audit_path", return_value=path), patch.object(
                player_admin, "_AUDIT_READ_BYTES", 4,
            ):
                self.assertEqual(player_admin.get_audit(), [])
                a, b, c = [json.dumps(audit_record(i), ensure_ascii=False).encode() for i in range(3)]
                path.write_bytes(a + b"\r\n" + b + b"\r" + c)
                self.assertEqual(player_admin.get_audit(), [audit_record(i) for i in (2, 1, 0)])
                self.assertEqual(player_admin.get_audit(0), [audit_record(2)])
                path.write_bytes(b"garbage\n")
                self.assertEqual(player_admin.get_audit(), [])
                path.write_bytes(a + b"\n\xff")
                with self.assertRaises(UnicodeDecodeError):
                    player_admin.get_audit()

    async def test_route_uses_daemon_executor(self):
        api = web_api.McControlWebApi.__new__(web_api.McControlWebApi)
        caller = threading.get_ident()
        seen = []

        def read_audit(limit):
            seen.append((threading.get_ident(), threading.current_thread().daemon, limit))
            return [audit_record(1)]

        with patch.object(api, "_player_admin_username", return_value="admin"), patch.object(
            web_api, "request", SimpleNamespace(query={"limit": "2"}),
        ), patch.object(web_api, "json_response", side_effect=lambda body, **kwargs: (body, kwargs)), patch.object(
            log_watcher, "daemon_executor", wraps=log_watcher.daemon_executor,
        ) as executor, patch.object(player_admin, "get_audit", side_effect=read_audit):
            body, kwargs = await api.player_audit()
        self.assertEqual(executor.call_count, 1, "H6 审计路由须使用 daemon 执行器")
        thread, daemon, limit = seen[0]
        self.assertNotEqual(thread, caller)
        self.assertTrue(daemon)
        self.assertEqual(limit, 2)
        self.assertEqual((body, kwargs), ({"ok": True, "authorized": True, "items": [audit_record(1)]}, {}))


if __name__ == "__main__":
    unittest.main()
