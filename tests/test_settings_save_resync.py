"""v0.21.7 回归：WebUI「保存全部设置」改 server_dir 时就地刷新指纹/词典。

覆盖点：save_settings 的热应用分支 + rescan 按钮接口（都不需要真实 HTTP）。
运行：
  python tests\\test_settings_save_resync.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()
from _paths import require_app  # noqa: E402
require_app()                    # AstrBot 运行目录：自动发现（见 tests/_paths.py）

import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.mod_fingerprint import compute_stable_server_id  # noqa: E402


def make_pack(root: Path, mods: dict[str, str]) -> Path:
    mods_dir = root / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for fname, modid in mods.items():
        with zipfile.ZipFile(mods_dir / fname, "w") as zf:
            zf.writestr("META-INF/mods.toml", f'[[mods]]\nmodId="{modid}"\n')
    # v0.21.15：目录结构校验是硬门槛 —— 夹具必须给出「真服务端」的天然特征
    #（eula.txt / server.properties / logs/ / libraries/ / server.jar）。
    # 只造一个 mods/ 空壳目录，正是这次要拦下的高危误配，插件会拒绝初始化。
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


def make_plugin(server_dir: str, data_dir: Path):
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg({
        "server_dir": server_dir,
        "knowledge_enabled": True,
        "dictionary_enabled": True,
        "enable_event_listener": False,
    })
    plug._cfg_index = {}
    plug._kbman = None
    plug._knowledge = None
    plug._dictionary = None
    plug._watcher = None
    plug._rcon = None
    plug._admins = set()
    plug.logger = logging.getLogger("test")
    return plug


def main() -> int:
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir = tmp / "plugin_data"
        data_dir.mkdir()
        m.StarTools.get_data_dir = staticmethod(lambda *a, **k: data_dir)

        pack_a = make_pack(tmp / "PackA", {"a.jar": "mod_a"})
        pack_b = make_pack(tmp / "PackB", {"b.jar": "mod_b"})
        fp_a, _ = compute_stable_server_id(str(pack_a / "mods"))
        fp_b, _ = compute_stable_server_id(str(pack_b / "mods"))

        plug = make_plugin(str(pack_a), data_dir)
        api = wa.McControlWebApi(plug)

        # 先按 pack A 建立上下文（等价于插件启动时的 initialize）
        asyncio.run(plug._sync_server_context(rebuild_dictionary=True))
        if plug._kbman.server_id != fp_a:
            failed.append("初始基准不是 pack A")

        # 1) WebUI「保存全部设置」只改 server_dir → 必须就地重算
        wa.request = FakeRequest({"settings": {"server_dir": str(pack_b)}})
        r = body(asyncio.run(api.save_settings()))
        print("[1] save_settings:", r.get("notice"))
        if not r.get("ok"):
            failed.append(f"保存失败：{r.get('error')}")
        if plug._kbman.server_id != fp_b:
            failed.append(f"保存后指纹未刷新：{plug._kbman.server_id} != {fp_b}")
        if fp_b not in str(r.get("notice")) or fp_a not in str(r.get("notice")):
            failed.append("保存提示未体现指纹变化")
        if {x["id"] for x in plug._dictionary.mods} != {"mod_b"}:
            failed.append("保存后词典未换成 pack B")

        # 2) 保存「与换服无关」的设置 → 不应重算（指纹保持 pack B，不无谓重建词典）
        dict_before = plug._dictionary
        wa.request = FakeRequest({"settings": {"rcon_host": "127.0.0.1"}})
        r = body(asyncio.run(api.save_settings()))
        print("[2] 只改 rcon_host:", r.get("notice"))
        if plug._dictionary is not dict_before:
            failed.append("无关设置变更也不必要地重建了词典")

        # 3) 重建按钮（rescan 接口）→ 刷新指纹 + 重建词典并回报文案
        wa.request = FakeRequest({})
        r = body(asyncio.run(api.rescan()))
        print("[3] rescan:", r.get("notice_text"))
        if not r.get("ok") or "物品词典已重建" not in str(r.get("notice_text")):
            failed.append(f"rescan 未回报重建结果：{r}")
        if r.get("dict_stats", {}).get("mods") != 1:
            failed.append(f"rescan 的 dict_stats 不对：{r.get('dict_stats')}")

        # 4) 词典开关关掉 → 保存后立刻释放实例，且提示里说明
        wa.request = FakeRequest({"settings": {"dictionary_enabled": False}})
        r = body(asyncio.run(api.save_settings()))
        print("[4] 关词典:", r.get("notice"))
        if plug._dictionary is not None:
            failed.append("关闭 dictionary_enabled 后词典实例未释放")
        if "词典已在配置层停用" not in str(r.get("notice")):
            failed.append("关闭词典的提示文案缺失")

    if failed:
        print("\n✗ 失败项:")
        for f in failed:
            print("  -", f)
        return 1
    print("\n✓ 全部通过（4 组接口断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
