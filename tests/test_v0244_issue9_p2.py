"""Issue #9 P2 级缺陷专项回归测试（v0.24.4）。

覆盖：
1. 日志编码嗅探打分权重修正：UTF-8 中文日志在 GBK 乱码高发区下不被误判为 GBK
2. 1.21+ 配方结构兼容：支持直接平铺与 ingredients_list / pattern 变体
3. 向量多预设模型切换：切换模型时跨预设 .vec.npz 缓存清理生效
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.log_watcher import LogWatcher
from astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary import ItemDictionary

FAIL: list[str] = []

def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[PASS] {desc}")
    else:
        print(f"[FAIL] {desc} -> {detail}")
        FAIL.append(f"{desc}: {detail}")

def test_p2_1_log_encoding_score():
    print("---- 1. P2-1: 日志编码中文评分权重与嗅探 ----")
    # 构造一段包含中文的 UTF-8 日志片段
    text_utf8 = "[12:00:00] [Server thread/INFO]: <Steve> 大家好！欢迎来到服务器，祝大家玩得开心！"
    score_utf8 = LogWatcher._text_score(text_utf8, enc="utf-8")
    
    # 模拟 GBK 尝试解码或 GBK 串
    score_gbk = LogWatcher._text_score(text_utf8, enc="gbk")
    check("UTF-8 权重评分明显优于 GBK", score_utf8 > score_gbk, f"utf8={score_utf8}, gbk={score_gbk}")

    # 乱码序列（例如拉丁扩展字符）
    corrupted = "大家好ÈºÈº"
    score_corrupted = LogWatcher._text_score(corrupted, enc="gbk")
    check("包含拉丁扩展等乱码序列扣分", score_corrupted < score_gbk, f"corrupted={score_corrupted}")

def test_p2_2_recipe_121_compatibility():
    print("---- 2. P2-2: 1.21+ 新配方结构解析 ----")
    dic = ItemDictionary(server_dir="")
    
    # 测试 1.21 新版平铺 pattern / ingredient 配方
    json_121 = '''{
        "type": "minecraft:crafting_shaped",
        "pattern": [
            {"item": "minecraft:iron_ingot"},
            {"item": "minecraft:stick"}
        ],
        "result": {
            "id": "minecraft:iron_sword",
            "count": 1
        }
    }'''
    dic._parse_recipe(json_121, "minecraft", "iron_sword")
    r = dic.recipes.get("minecraft:iron_sword")
    check("1.21+ list pattern 解析成功", bool(r), f"recipes={r}")
    if r:
        inputs = r[0].get("inputs", [])
        check("1.21+ inputs 正确捕获材料", "minecraft:iron_ingot" in inputs and "minecraft:stick" in inputs, f"inputs={inputs}")

def test_p2_3_multi_preset_vector_cache_clean():
    print("---- 3. P2-3: 预设删除联动 .vec.npz 向量缓存清理 ----")
    from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import KnowledgePresetManager
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        man = KnowledgePresetManager(server_dir=td, server_id="test_fp", data_dir=str(tdp / "data"))
        # 创建并获取 pid
        res_a = man.create("preset_a")
        pid_a = res_a.get("id")
        pa_json = man.preset_path(pid_a)
        pa_vec = pa_json.with_suffix(".vec.npz")
        pa_vec.write_bytes(b"mock_vectors")
        check("向量缓存文件预先创建", pa_vec.exists())
        
        # 移除 preset_a
        man.create("preset_b") # 确保有至少两个预设
        res = man.remove(pid_a)
        check("删除预设成功", res.get("ok") is True, f"res={res}")
        check("删除预设联动清理 .vec.npz 缓存文件", not pa_vec.exists(), "vec cache still exists!")

        # 4. P2-3 补强测试：单文件删除异常不阻断其他缓存清理
        res_c = man.create("preset_c")
        pid_c = res_c.get("id")
        pc_vec = man.preset_path(pid_c).with_suffix(".vec.npz")
        pc_vec.write_bytes(b"mock_c")

        res_d = man.create("preset_d")
        pid_d = res_d.get("id")
        pd_vec = man.preset_path(pid_d).with_suffix(".vec.npz")
        pd_vec.write_bytes(b"mock_d")

        # 孤儿缓存
        orphan_vec = (tdp / "data" / "orphan_test.vec.npz")
        orphan_vec.write_bytes(b"mock_orphan")

        # 模拟 pc_vec 无法删除（例如被占用 / unlink 抛异常）
        orig_unlink = Path.unlink
        def mock_unlink(self, missing_ok=False):
            if self.name == pc_vec.name:
                raise PermissionError("Mock locked file")
            orig_unlink(self, missing_ok=missing_ok)

        import unittest.mock as mock
        with mock.patch.object(Path, "unlink", mock_unlink):
            cleared = man.invalidate_all_vector_caches()
            check("单文件锁定报错时仍清理了其他缓存", cleared >= 2, f"cleared={cleared}")
            check("未锁定的 preset_d 缓存已被删除", not pd_vec.exists())
            check("孤儿缓存已被删除", not orphan_vec.exists())
            check("被锁定的 preset_c 缓存仍保留（未被误删）", pc_vec.exists())

def main():
    test_p2_1_log_encoding_score()
    test_p2_2_recipe_121_compatibility()
    test_p2_3_multi_preset_vector_cache_clean()
    if FAIL:
        print(f"\n[SUMMARY] {len(FAIL)} FAILURES:")
        for f in FAIL:
            print(" -", f)
        sys.exit(1)
    else:
        print("\n[ALL PASS] Issue #9 P2 3项全量通过！")

if __name__ == "__main__":
    main()
