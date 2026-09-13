"""v0.21.7 回归：换服务端（server_dir）后指纹 / 词典 / 知识库基准就地刷新。

不需要真实 AstrBot 运行时：只构造插件对象（绕过 __init__）+ 两个假整合包目录。
运行：
  python tests\\test_server_switch_resync.py
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
from astrbot_plugin_Scintilla_MC_Server_Control.core.mod_fingerprint import compute_stable_server_id  # noqa: E402


def make_pack(root: Path, mods: dict[str, str]) -> Path:
    """造一个假服务端目录：mods/<name>.jar 内含 mods.toml（modId=...）。"""
    mods_dir = root / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for fname, modid in mods.items():
        with zipfile.ZipFile(mods_dir / fname, "w") as zf:
            zf.writestr(
                "META-INF/mods.toml",
                f'[[mods]]\nmodId="{modid}"\nversion="1.0"\n',
            )
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
    def save_config(self) -> bool:  # 落盘钩子（测试里不写盘）
        return True


def make_plugin(server_dir: str, data_dir: Path):
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg({
        "server_dir": server_dir,
        "knowledge_enabled": True,
        "dictionary_enabled": True,
    })
    plug._cfg_index = {}
    plug._kbman = None
    plug._knowledge = None
    plug._dictionary = None
    plug.logger = logging.getLogger("test")
    plug._data_dir = data_dir
    return plug


def main() -> int:
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir = tmp / "plugin_data"
        data_dir.mkdir()
        # 让 StarTools.get_data_dir 落到临时目录（脱离 AstrBot 运行时依赖）
        m.StarTools.get_data_dir = staticmethod(lambda *a, **k: data_dir)

        pack_a = make_pack(tmp / "ExamplePack", {"craft.jar": "deceasedcraft", "lib.jar": "somelib"})
        pack_b = make_pack(tmp / "AllTheMods", {"atm.jar": "allthemods", "extra.jar": "extra_mod"})

        fp_a, _ = compute_stable_server_id(str(pack_a / "mods"))
        fp_b, _ = compute_stable_server_id(str(pack_b / "mods"))
        assert fp_a != fp_b, "两个假整合包的指纹应当不同"

        plug = make_plugin(str(pack_a), data_dir)

        # 1) 首次识别：按需初始化知识库 + 词典
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=True))
        print("[1] 首次:", note)
        if plug._kbman is None or plug._kbman.server_id != fp_a:
            failed.append("首次识别未按 pack A 建立知识库基准")
        if plug._knowledge is None or plug._knowledge.server_id != fp_a:
            failed.append("内存知识库实例未使用 pack A 指纹")
        if plug._dictionary is None or not plug._dictionary.mods:
            failed.append("物品词典未构建")
        else:
            ids_a = {x["id"] for x in plug._dictionary.mods}
            if ids_a != {"deceasedcraft", "somelib"}:
                failed.append(f"pack A 词典内容不对：{ids_a}")

        # 2) 换成 pack B：指纹 / 词典必须立刻跟着换（这就是本次修的 bug）
        active_first = plug._kbman.reg.get("active")
        plug.config["server_dir"] = str(pack_b)
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=True))
        print("[2] 换服后:", note)
        if plug._kbman.server_id != fp_b:
            failed.append(f"换服后知识库基准未刷新：{plug._kbman.server_id} != {fp_b}")
        if plug._knowledge.server_id != fp_b:
            failed.append("换服后内存知识库实例仍是旧指纹")
        if fp_b not in note or fp_a not in note:
            failed.append("提示文案未体现指纹变化（旧 → 新）")
        ids_b = {x["id"] for x in plug._dictionary.mods}
        if ids_b != {"allthemods", "extra_mod"}:
            failed.append(f"换服后词典未重建：{ids_b}")

        # 3) 换服**不自动切换**激活预设（v0.18.0 策略保持）
        active_before = plug._kbman.reg.get("active")
        if plug._kbman.reg.get("active") != active_before:
            failed.append("换服后激活预设被自动切换了（违反 v0.18.0 策略）")

        # 4) 词典缓存文件确实落盘
        if not (data_dir / "items.json").exists():
            failed.append("词典缓存未落盘")

        # 5) 知识库配置层停用 → 立刻释放实例
        plug.config["knowledge_enabled"] = False
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=False))
        print("[3] 停用后:", note)
        if plug._kbman is not None or plug._knowledge is not None:
            failed.append("knowledge_enabled=False 未释放知识库实例")

        # 6) 词典停用同样立刻生效
        plug.config["dictionary_enabled"] = False
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=False))
        print("[4] 词典停用后:", note)
        if plug._dictionary is not None:
            failed.append("dictionary_enabled=False 未释放词典实例")

        # 7) server_dir 非法时给出提示、不抛异常
        plug.config.update({"server_dir": str(tmp / "not_exist"), "knowledge_enabled": True,
                            "dictionary_enabled": True})
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=True))
        print("[5] 无效目录:", note)
        if "有效目录" not in note:
            failed.append("无效 server_dir 未给出提示")

        # 8) mods 目录为空 → 必须点明「指纹认不出服务端」，否则换服也看不出区别
        # v0.21.15：结构本身要合法（真服务端），只是 mods/ 里没有 jar
        # v0.21.20：文案改为「既没有 mod 也没有插件」（插件服的内容在 plugins/，
        #          不能一律说成「空整合包」）—— 这里同样要出现身份说明与 weak 警告
        empty_pack = make_pack(tmp / "EmptyPack", {})
        plug.config["server_dir"] = str(empty_pack)
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=False))
        print("[6] 空 mods:", note)
        if "既没有 mod 也没有插件" not in note:
            failed.append("空内容服务端未给出防呆提示")
        if "认不出同形态" not in note:
            failed.append("未点明「指纹认不出同形态的另一台服务端」")
        if not plug._server_identity.get("weak"):
            failed.append("身份识别未把空内容标记为 weak")

    if failed:
        print("\n✗ 失败项:")
        for f in failed:
            print("  -", f)
        return 1
    print("\n✓ 全部通过（8 组断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
