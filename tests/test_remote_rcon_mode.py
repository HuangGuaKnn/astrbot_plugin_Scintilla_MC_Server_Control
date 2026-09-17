"""v0.21.15 回归：服务端目录硬校验 + 异地 RCON 模式（本地文件能力降级）。

覆盖点：
  A. core.server_dir_check 的判据：真服务端 / 空壳目录 / 客户端 .minecraft / 外壳目录
  B. 插件初始化 _sync_server_context：结构不对不建词典与知识库；异地模式一律不建
  C. 工具闸门 _local_gate：说清「为什么不可用、怎么恢复」
  D. WebUI 保存硬校验：结构不对**拒绝写盘**（配置保持原样）；异地模式可空目录保存
  E. 知识库接口错误体：不再一律「知识库未初始化」，改为说清原因
  F. 事件转发 / 状态文案 / LLM 请求阶段的能力降级注入

运行：python tests/test_remote_rcon_mode.py  （工作目录 = 插件根目录）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()
from _paths import require_app  # noqa: E402
require_app()                    # AstrBot 运行目录：自动发现（见 tests/_paths.py）

import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.server_dir_check import (  # noqa: E402
    format_check,
    inspect_server_dir,
)

FAIL: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {desc:<56}{extra}")
    if not ok:
        FAIL.append(desc)


def make_server(root: Path, mods: int = 0) -> Path:
    """造一个「真服务端」：logs/ + libraries/ + eula.txt + server.properties + server.jar。"""
    (root / "mods").mkdir(parents=True, exist_ok=True)
    for i in range(mods):
        (root / "mods" / f"mod{i}.jar").write_bytes(b"")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("", encoding="utf-8")
    (root / "libraries").mkdir(exist_ok=True)
    (root / "eula.txt").write_text("eula=true", encoding="utf-8")
    (root / "server.properties").write_text("level-name=world", encoding="utf-8")
    (root / "server.jar").write_bytes(b"")
    return root


class FakeCfg(dict):
    def save_config(self) -> bool:
        return True


class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def body(resp) -> dict:
    raw = getattr(resp, "body", b"{}")
    return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)


def make_plugin(cfg: dict, data_dir: Path):
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg(cfg)
    plug._cfg_index = m.load_cfg_group_index(PLUGIN_DIR / m.CFG_SCHEMA_FNAME)
    plug._kbman = None
    plug._knowledge = None
    plug._dictionary = None
    plug._rcon = None
    plug._watcher = None
    plug._dir_check_cache = None
    plug._admins = set()
    plug.logger = logging.getLogger("test")
    return plug


class FakeEvent:
    def __init__(self, sender: str = "10001"):
        self._sender = sender
        self._extra: dict = {}

    def get_sender_id(self) -> str:
        return self._sender

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value


SYSTEM_PROMPT = "你是一个 Minecraft 服务器助手。"


class FakeReq:
    """ProviderRequest 的最小替身（v0.22.1：提示挂 extra_user_content_parts，不碰 system_prompt）。"""

    def __init__(self):
        self.system_prompt = SYSTEM_PROMPT
        self.extra_user_content_parts: list = []

    @property
    def hint_text(self) -> str:
        out = []
        for part in self.extra_user_content_parts:
            out.append(part.get("text", "") if isinstance(part, dict) else getattr(part, "text", ""))
        return "\n\n".join(out)


class HintSelf:
    """_inject_permission_hint 的最小宿主（管理员视角：只看能力降级那一段）。"""

    def __init__(self, plug):
        self.config = plug.config
        self._cfg_index = plug._cfg_index
        self.local_files_degraded_reason = plug.local_files_degraded_reason
        # 真身是 Star 子类，self.logger 由 AstrBot 提供；替身补一个即可
        self.logger = logging.getLogger("HintSelf")

    def _cfg(self, key, default=None):
        box = self.config.get(self._cfg_index.get(key, ""), {})
        if isinstance(box, dict) and key in box:
            return box[key]
        return self.config.get(key, default)

    def _is_admin(self, event) -> bool:
        return True


for _name in ("_inject_permission_hint", "_append_user_hint", "is_remote_mode",
              "server_dir_check", "local_files_degraded_reason", "_local_gate"):
    setattr(HintSelf, _name, getattr(m.McControlPlugin, _name))


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir = tmp / "plugin_data"
        data_dir.mkdir()
        m.StarTools.get_data_dir = staticmethod(lambda *a, **k: data_dir)

        # ============================================================
        print("=========== A. 目录结构判据（core/server_dir_check） ===========")
        real = make_server(tmp / "Server", mods=2)
        r = inspect_server_dir(str(real))
        check("真服务端（logs/libraries/eula/server.jar）→ 通过", r["ok"] is True, r["summary"][:52])
        check("world/ 首次开服前不存在 → 只提示、不当错误",
              r["ok"] is True and any("world" in w for w in r["warnings"]), str(len(r["warnings"])) + " 条提示")

        shell = tmp / "server_dir_空壳"
        shell.mkdir()
        r = inspect_server_dir(str(shell))
        check("空壳目录 → 拒绝，并点名「没有任何服务端特征」",
              r["ok"] is False and "没有任何服务端特征" in r["errors"][0], r["errors"][0][:52])

        only_mods = tmp / "只有mods"
        (only_mods / "mods").mkdir(parents=True)
        r = inspect_server_dir(str(only_mods))
        check("只有 mods/ 的目录 → 仍拒绝（原版与客户端都不带 mods，它不算服务端特征）",
              r["ok"] is False, r["errors"][0][:44])

        client = tmp / "客户端.minecraft"
        client.mkdir()
        (client / "options.txt").write_text("fov:0.5", encoding="utf-8")
        (client / "saves").mkdir()
        r = inspect_server_dir(str(client))
        check("客户端目录（options.txt / saves）→ 单独识别并拒绝",
              r["ok"] is False and "客户端" in r["errors"][0], r["errors"][0][:52])

        outer = tmp / "外壳目录"
        make_server(outer / "ExamplePack_Server_1.20.1", mods=1)
        r = inspect_server_dir(str(outer))
        check("外壳目录 → 给出可直接复制的正确路径",
              r["ok"] is False and r["suggest_path"].endswith("ExamplePack_Server_1.20.1"),
              r["suggest_path"][-34:])

        r = inspect_server_dir(str(tmp / "根本没有这个目录"))
        check("路径不存在 → 明确报「目录不存在」", r["ok"] is False and "不存在" in r["errors"][0])
        r = inspect_server_dir(str(real / "server.jar"))
        check("指到文件 → 明确报「不是目录」", r["ok"] is False and "不是" in r["errors"][0])
        r = inspect_server_dir("")
        check("留空 → 「未填写服务端目录」（不抛异常）", r["ok"] is False and "未填写" in r["errors"][0])

        empty_mods = make_server(tmp / "原版服务端", mods=0)
        r = inspect_server_dir(str(empty_mods))
        # v0.21.20：不再只说「mods 空」，而是点明「内容来源」与指纹退化（插件服同理）
        check("结构合法 + mods 空 → 通过，但提示指纹会退化",
              r["ok"] is True
              and any("退化成「服务端形态」" in w for w in r["warnings"]),
              str(r["warnings"]))
        check("format_check 文案可读（含结论 + 提示）",
              "正常" in format_check(r) and "提示" in format_check(r))

        # ============================================================
        print(chr(10) + "=========== B. 插件初始化：结构不对 / 异地模式 → 不建能力 ===========")
        bad = tmp / "not_the_server"
        bad.mkdir()
        p1 = make_plugin({"server_dir": str(bad), "knowledge_enabled": True,
                          "dictionary_enabled": True}, data_dir)
        note = asyncio.run(p1._sync_server_context(rebuild_dictionary=True))
        print("      目录不合法：", note)
        check("目录不合法 → 词典/知识库都不建（不给空数据装成配好了）",
              p1._dictionary is None and p1._kbman is None, note[:40])
        check("目录不合法 → 提示含「结构校验」", "结构校验" in note)
        check("目录不合法 → 降级原因一行可读",
              "结构校验未通过" in p1.local_files_degraded_reason(),
              p1.local_files_degraded_reason()[:44])

        p2 = make_plugin({"server_dir": str(bad), "remote_rcon_mode": True,
                          "knowledge_enabled": True, "dictionary_enabled": True}, data_dir)
        note2 = asyncio.run(p2._sync_server_context(rebuild_dictionary=True))
        print("      异地模式 + 目录不合法：", note2)
        check("异地模式 → 目录填了也不校验、一律不建",
              p2._dictionary is None and p2._kbman is None and "异地" in note2)
        check("异地模式 → 降级原因点明「按设计禁用」",
              "按设计禁用" in p2.local_files_degraded_reason())

        p3 = make_plugin({"server_dir": "", "knowledge_enabled": True,
                          "dictionary_enabled": True}, data_dir)
        note3 = asyncio.run(p3._sync_server_context(rebuild_dictionary=True))
        check("开关未开 + 目录留空（还没配的起点）→ 照旧放行，文案仍说「未配置」",
              "未配置或不是有效目录" in note3 and p3._dictionary is None, note3[:40])

        p4 = make_plugin({"server_dir": str(real), "knowledge_enabled": True,
                          "dictionary_enabled": True}, data_dir)
        asyncio.run(p4._sync_server_context(rebuild_dictionary=True))
        check("结构合法 → 词典与知识库正常建立",
              p4._dictionary is not None and p4._kbman is not None)

        # ============================================================
        print(chr(10) + "=========== C. 工具闸门 _local_gate（说清原因 + 怎么恢复） ===========")
        g = p2._local_gate("物品 ID 搜索", "服务端 mods/*.jar")
        check("异地模式 → 禁用文案 + 恢复办法",
              str(g).startswith("【异地 RCON 模式·已禁用】") and "关闭「异地 RCON 模式」" in str(g),
              str(g)[:38])
        g = p1._local_gate("物品 ID 搜索", "服务端 mods/*.jar")
        check("目录不合法 → 校验未通过 + 指向设置页",
              str(g).startswith("【服务端目录校验未通过】") and "设置页" in str(g))
        check("结构合法 → 放行（不误报）",
              p4._local_gate("物品 ID 搜索", "服务端 mods/*.jar") is None)
        check("目录留空 → 放行给原有「未初始化」文案（不抢话）",
              p3._local_gate("物品 ID 搜索", "服务端 mods/*.jar") is None)
        rc = asyncio.run(p2._get_rcon())
        check("异地模式不影响 RCON 能力（只禁本地文件类）",
              rc.host == "127.0.0.1" and rc.port == 25575)

        # ============================================================
        print(chr(10) + "=========== D. WebUI 保存硬校验 ===========")
        plug = make_plugin({"server_dir": str(real), "knowledge_enabled": True,
                            "dictionary_enabled": True, "enable_event_listener": False},
                           data_dir)
        api = wa.McControlWebApi(plug)
        before = dict(plug.config)

        wa.request = FakeRequest({"settings": {"server_dir": str(bad)}})
        r = body(asyncio.run(api.save_settings()))
        print("      保存非法目录：", str(r.get("error", ""))[:150].replace(chr(10), " / "))
        check("结构不对 → ok=False 且不写盘", r.get("ok") is False)
        check("错误文案含「校验未通过」「已拒绝保存」「保持原样」",
              all(x in str(r.get("error")) for x in ("校验未通过", "已拒绝保存", "保持原样")),
              str(r.get("error"))[-90:].replace(chr(10), " / "))
        check("错误文案给出两条出路（改目录 / 开异地模式）",
              "异地 RCON 模式" in str(r.get("error")))
        check("配置保持原样（没有静默落盘）", dict(plug.config) == before, str(len(before)) + " 键未变")
        check("回传 server_dir_check 细节供前端展示",
              (r.get("server_dir_check") or {}).get("ok") is False)

        wa.request = FakeRequest({"settings": {"remote_rcon_mode": True, "server_dir": str(bad)}})
        r = body(asyncio.run(api.save_settings()))
        print("      开启异地模式：", r.get("notice"))
        check("异地模式 → 目录不合法也放行保存", r.get("ok") is True)
        check("异地模式 → 提示里明确告知哪些能力被禁用",
              "异地 RCON 模式已开启" in str(r.get("notice")) and "禁用" in str(r.get("notice")))

        wa.request = FakeRequest({"settings": {"server_dir": ""}})
        r = body(asyncio.run(api.save_settings()))
        check("异地模式 → 目录留空保存成功（这是核心诉求）", r.get("ok") is True, str(r.get("notice"))[:40])

        plug2 = make_plugin({"server_dir": "", "knowledge_enabled": True,
                             "dictionary_enabled": True}, data_dir)
        api2 = wa.McControlWebApi(plug2)
        wa.request = FakeRequest({"settings": {"server_dir": ""}})
        r = body(asyncio.run(api2.save_settings()))
        check("普通模式 + 目录留空 → 仍可保存（未配置不是误配）", r.get("ok") is True)

        # ============================================================
        print(chr(10) + "=========== E. 知识库接口错误体 ===========")
        e = wa.McControlWebApi(p2)._kb_uninit()
        check("知识库不可用 → 说清原因（异地模式）：按设计禁用 + 怎么恢复",
              "异地 RCON 模式" in e["error"] and "按设计禁用" in e["error"]
              and "关闭「异地 RCON 模式」" in e["error"], e["error"][:48])
        e = wa.McControlWebApi(p1)._kb_uninit()
        check("知识库不可用 → 说清原因（目录校验未通过）",
              "结构校验未通过" in e["error"] and "修正「服务器目录」" in e["error"])
        e = wa.McControlWebApi(p3)._kb_uninit()
        check("服务端目录没配 → 也说清是「未配置服务器目录」", "未配置服务器目录" in e["error"],
              e["error"][:44])
        p_off = make_plugin({"server_dir": str(real), "knowledge_enabled": False}, data_dir)
        check("其余情况（配置层关掉知识库）→ 保留「知识库未初始化」原文案",
              wa.McControlWebApi(p_off)._kb_uninit()["error"] == "知识库未初始化")

        # ============================================================
        print(chr(10) + "=========== F. 事件转发 / 状态 / LLM 前置注入 ===========")
        p5 = make_plugin({"remote_rcon_mode": True, "enable_event_listener": True,
                          "server_dir": ""}, data_dir)
        msg = asyncio.run(p5._restart_event_listener())
        print("      异地模式事件转发：", msg)
        check("异地模式 → 事件转发明确拒绝启动并说原因",
              "异地 RCON 模式" in msg and "日志读不到" in msg)
        check("异地模式 → 不创建监听器对象", getattr(p5, "_watcher", None) is None)
        check("异地模式 → 版本探测返回空（上层显示未检测到）", p5.detect_server_version() == "")

        hs = HintSelf(p1)
        ev, req = FakeEvent(), FakeReq()
        asyncio.run(m.McControlPlugin._inject_permission_hint(hs, ev, req))
        print("      注入片段：", req.hint_text[:78].replace(chr(10), " "))
        check("LLM 请求阶段先告知「能力降级」", "能力降级提醒" in req.hint_text)
        check("点名不可用的查询工具 + 说明重试无效",
              "mc_search_item" in req.hint_text and "重试与" in req.hint_text)
        check("告知恢复办法", "关闭异地 RCON 模式或修正服务器目录即可恢复" in req.hint_text)
        check("提示走用户消息内容块（不改写 system_prompt）",
              req.extra_user_content_parts and req.system_prompt == SYSTEM_PROMPT)

        hs2 = HintSelf(p4)
        req2 = FakeReq()
        asyncio.run(m.McControlPlugin._inject_permission_hint(hs2, FakeEvent(), req2))
        check("一切正常 → 不注入（不打扰）",
              "能力降级提醒" not in req2.hint_text and not req2.extra_user_content_parts)

    if FAIL:
        print(chr(10) + "✗ 失败项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print(chr(10) + "✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
