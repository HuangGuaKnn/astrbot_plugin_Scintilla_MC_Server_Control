"""用真机回包离线验证 TPS 采样、状态和两个入口。"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from _paths import PLUGIN_DIR, add_sys_paths

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core import server_card as sc
from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import RconPartialResponseError
from astrbot_plugin_Scintilla_MC_Server_Control.core.tps_parse import parse_tps_output
from astrbot_plugin_Scintilla_MC_Server_Control.main import McControlPlugin

REPLIES = json.loads((Path(__file__).with_name("fixtures") / "tps_replies.json").read_text(encoding="utf-8"))
PAPER = REPLIES["1.21.11-paper-spark"]
FABRIC = REPLIES["1.21.1-fabric-spark"]
FORGE = REPLIES["1.16.5-forge-spark"]
VANILLA = REPLIES["1.13.2-vanilla"]
COMMANDS = ("tps", "forge tps", "mspt", "tick query")


class FakeRcon:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def command(self, command):
        self.calls.append(command)
        reply = self.replies[command]
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakePlugin:
    def __init__(self, replies, server_dir="", *, remote=False):
        self.dir = str(server_dir)
        self.remote = remote
        self.rcon = FakeRcon({
            "list": "There are 1/20 players online: Alex",
            "time query daytime": "The time is 1000",
            **replies,
        })

    async def _get_rcon(self):
        return self.rcon

    def _version_capabilities(self):
        return {}

    def _cfg(self, key, default=None):
        return self.dir if key == "server_dir" else default

    def is_remote_mode(self):
        return self.remote


class TpsChecks(unittest.IsolatedAsyncioTestCase):
    def test_same_implementation(self):
        main = PLUGIN_DIR / "main.py"
        paths = [main, *sorted((PLUGIN_DIR / "core").rglob("*.py"))]
        trees = {path.stem: ast.parse(path.read_text(encoding="utf-8-sig")) for path in paths}

        def definitions(names):
            return [
                module for module, tree in trees.items()
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
            ]

        self.assertEqual(definitions({"parse_tps_output", "_parse_tps_output", "parse_tps_reply"}),
                         ["tps_parse"], "TPS1 解析器只能定义一处")
        self.assertEqual(definitions({"probe_tps", "_probe_tps"}), ["server_card"], "TPS1 探针只能定义一处")
        for module, entry, callee in (
            ("main", "mcs_status", "_query_tps_mspt"),
            ("main", "_query_tps_mspt", "probe_tps"),
            ("server_card", "sample_with", "probe_tps"),
            ("server_card", "probe_tps", "parse_tps_output"),
        ):
            body = next(node for node in ast.walk(trees[module])
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == entry)
            self.assertTrue(any(
                isinstance(node, ast.Call)
                and ((isinstance(node.func, ast.Name) and node.func.id == callee)
                     or (isinstance(node.func, ast.Attribute) and node.func.attr == callee))
                for node in ast.walk(body)
            ), f"TPS1 {entry} 必须调用 {callee}")

    def test_forge_overall(self):
        raw = FORGE["forge tps"]
        self.assertEqual(parse_tps_output("forge tps", raw), {"tps_1m": 20.0, "mspt": 7.032},
                         "TPS2 Forge 必须从 Overall 取 TPS 与 MSPT")
        changed = raw.replace("Overall: Mean tick time: 7.032 ms. Mean TPS: 20.000",
                              "Overall: Mean tick time: 7.032 ms. Mean TPS: 19.000")
        self.assertEqual(parse_tps_output("forge tps", changed), {"tps_1m": 19.0, "mspt": 7.032})
        self.assertEqual(parse_tps_output("forge tps", raw.rsplit("\n", 1)[0]), {})
        self.assertEqual(parse_tps_output("forge tps", raw.replace("\n", r"\n")),
                         {"tps_1m": 20.0, "mspt": 7.032})

    def test_paper_and_spark_columns(self):
        self.assertEqual(parse_tps_output("tps", PAPER["tps"]),
                         {"tps_1m": 19.7, "tps_5m": 19.7, "tps_15m": 19.7})
        self.assertEqual(parse_tps_output("tps", PAPER["tps"].replace("§a19.7", "§a18.7", 1))["tps_1m"], 18.7)
        self.assertEqual(parse_tps_output("tps", PAPER["tps"].replace("§a", "", 1))["tps_1m"], 19.7)
        self.assertEqual(parse_tps_output("tps", "Average tick time: 6.894 ms"), {})
        spark = REPLIES["fabric-spark-log"]
        self.assertEqual(parse_tps_output("spark tps", spark),
                         {"tps_1m": 20.0, "tps_5m": 20.0, "tps_15m": 20.0})
        changed = spark.replace("*20.0, *20.0, 20.0", "*20.0, *20.0, 19.0")
        self.assertEqual(parse_tps_output("spark tps", changed)["tps_1m"], 19.0)
        self.assertEqual(parse_tps_output("spark tps", "*: 19.4, 19.5\n*: 4.2/0.1/5.0"),
                         {"tps_1m": 19.4, "mspt": 4.2})

    async def test_only_mspt(self):
        raw = FABRIC["tick query"]
        self.assertEqual(parse_tps_output("tick query", raw), {
            "mspt": 1.7, "behind_ticks": 0, "percentiles": raw[raw.index("Percentiles:"):],
        }, "TPS3 tick query 只能产生 MSPT 与运行信息")
        changed = raw.replace("1.7ms", "1.8ms").replace("running normally", "running 20 ticks behind")
        self.assertEqual(parse_tps_output("tick query", changed)["behind_ticks"], 20)
        self.assertEqual(parse_tps_output("tick query", changed)["mspt"], 1.8)
        paper = PAPER["mspt"]
        self.assertEqual(parse_tps_output("mspt", paper), {"mspt": 1.8}, "TPS3 mspt 只能产生 MSPT")
        self.assertEqual(parse_tps_output("mspt", paper.replace("§a1.8", "§a2.8", 2)), {"mspt": 2.8})
        for source, raw, value in (("mspt", paper, 1.8), ("tick query", changed, 1.8)):
            with self.subTest(source=source):
                plugin = FakePlugin({**VANILLA, source: raw})
                field = (await sc.sample(plugin))["fields"][2]
                self.assertEqual(field["value"], f"MSPT {value} ms")
                self.assertNotIn("tps", field)
                self.assertNotIn("low", field)
                chat = await McControlPlugin._query_tps_mspt(plugin, plugin.rcon)
                self.assertIn("🐢 TPS：暂无读数", chat)
                self.assertTrue(any(f"{value} ms（/{source}）" in line for line in chat))
                if source == "tick query":
                    self.assertIn("游戏循环落后 20 tick", chat)

    async def test_chain_and_sources(self):
        for replies, source, value in (
            (PAPER, "tps", 19.7),
            (FORGE, "forge tps", 20.0),
            ({**VANILLA, "mspt": PAPER["mspt"]}, "mspt", 1.8),
            (FABRIC, "tick query", 1.7),
        ):
            with self.subTest(source=source):
                plugin = FakePlugin(replies)
                result = await sc.probe_tps(plugin)
                self.assertEqual(result.source, source, "TPS4 每个数字必须带来源")
                self.assertEqual(result.mspt if result.tps_1m is None else result.tps_1m, value)
                tried = list(COMMANDS[:COMMANDS.index(source) + 1])
                self.assertEqual(plugin.rcon.calls, tried, "TPS5 按顺序停止，禁用 spark RCON")
                self.assertEqual([a.raw for a in result.attempts], [replies[c] for c in tried],
                                 "TPS6 保留每次尝试的完整原文")
                self.assertEqual([a.state for a in result.attempts], ["unsupported"] * (len(tried) - 1) + ["ok"])

    async def test_forge_both_entries(self):
        plugin = FakePlugin(FORGE)
        field = (await sc.sample(plugin))["fields"][2]
        self.assertEqual((field["value"], field["mspt"], field["source"]), ("20.0", 7.032, "/forge tps"))
        self.assertFalse(field["low"])
        self.assertEqual(field["tps"], {"1m": 20.0, "5m": None, "15m": None})
        self.assertEqual([a["state"] for a in field["attempts"]], ["unsupported", "ok"])
        chat = await McControlPlugin._query_tps_mspt(plugin, plugin.rcon)
        self.assertEqual(chat, ["🐢 TPS：20.0（/forge tps）", "⚡ MSPT：7.032 ms（/forge tps）"])

    async def test_different_failures(self):
        self.assertTrue(all(not case["spark tps"] for case in (PAPER, FABRIC, FORGE)))
        plugin = FakePlugin(VANILLA)
        result = await sc.probe_tps(plugin)
        self.assertEqual(result.state, "unsupported")
        self.assertTrue(all(f"/{cmd}" in result.note for cmd in COMMANDS))
        field = (await sc.sample(plugin))["fields"][2]
        self.assertEqual(field["note"], "该世代不支持 TPS 查询。已试：/tps、/forge tps、/mspt、/tick query",
                         "TPS8 不支持时只说明能力和已试命令")
        # Paper 的换行 Brigadier 回包与旧版 Unknown command 都能认出。
        paper_unknown = {**VANILLA, "forge tps": PAPER["forge tps"]}
        self.assertEqual((await sc.probe_tps(FakePlugin(paper_unknown))).state, "unsupported")
        changed = {**VANILLA, "tps": VANILLA["tps"].replace("Unknown", "Xnknown")}
        self.assertEqual((await sc.probe_tps(FakePlugin(changed))).state, "unrecognized")
        empty = {**VANILLA, "tps": FABRIC["spark tps"]}
        result = await sc.probe_tps(FakePlugin(empty))
        self.assertEqual(result.state, "empty", "TPS7 空回答独立于不支持和认不出")
        self.assertIn("请在服务端控制台", result.note)
        card = await sc.sample(FakePlugin(empty))
        self.assertEqual(card["fields"][2]["state_label"], "答案未返回")
        self.assertIn("答案未返回 1 项", card["summary"])
        bad = {**empty, "tps": "<b>乱码</b>"}
        result = await sc.probe_tps(FakePlugin(bad))
        self.assertEqual((result.state, result.raw), ("unrecognized", "<b>乱码</b>"))
        result = await sc.probe_tps(FakePlugin({**empty, "tps": ConnectionRefusedError("拒绝连接")}))
        self.assertEqual((result.state, len(result.attempts)), ("unreachable", 1))
        partial = RconPartialResponseError("响应不完整", partial="TPS: 20.0")
        result = await sc.probe_tps(FakePlugin({**VANILLA, "tps": partial}))
        self.assertEqual((result.state, result.raw, result.tps_1m), ("unreachable", "TPS: 20.0", None))
        mixed = "TPS: 20.0\n" + VANILLA["tps"]
        result = await sc.probe_tps(FakePlugin({**FORGE, "tps": mixed}))
        self.assertEqual((result.state, result.source), ("ok", "forge tps"))
        self.assertIn(mixed, result.diagnostics)

    async def test_spark_installed_is_evidence_not_a_numeric_source(self):
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            mods.mkdir()
            with ZipFile(mods / "spark-1.10.109-fabric.jar", "w") as jar:
                jar.writestr("fabric.mod.json", '{"id": "spark"}')
            plugin = FakePlugin(VANILLA, directory)
            result = await sc.probe_tps(plugin)
            self.assertEqual(result.state, "spark_unavailable")
            self.assertIn("请在游戏内或 spark 的 Web 页面查看", result.note)
            self.assertIsNone(result.tps_1m)
            self.assertEqual(plugin.rcon.calls, list(COMMANDS))
            result = await sc.probe_tps(FakePlugin(FABRIC, directory))
            self.assertEqual((result.state, result.tps_1m, result.mspt), ("ok", None, 1.7))
            self.assertIn("服务端目录发现 spark", result.note)
            result = await sc.probe_tps(FakePlugin(VANILLA, directory, remote=True))
            self.assertEqual(result.state, "unsupported")
            self.assertNotIn("服务端目录发现 spark", result.note)


if __name__ == "__main__":
    unittest.main()
