"""v0.21.40 回归：知识库检索引擎双轨制（bm25 默认 / legacy 可选）。

背景：旧版检索把中文切成**单字**、英文按整段做子串命中，再按「命中 token 数」
排序。后果是连锁误召回——查「满配一把枪」会命中一堆含「配 / 方 / 案」的条目，
实测返回结果精确率仅 13.9%（137 条里 118 条无关），这些垃圾会被塞进 Agent 提示词。

新默认引擎换成「中文二元切分(bigram) + BM25 + 倒排索引 + 最低分门槛」：
bigram 保留字序（「黄铜」≠「铜黄」），idf 自动压低「通用/方案/规则」这类套话，
BM25 长度归一化避免长条目霸榜，门槛再砍掉尾部噪声。

本测试钉住三件事：
  1) 默认就是 bm25；配置写错（非法值）回落 bm25，不能让检索崩掉；
  2) 正常问法下两引擎都能把正确条目排第 1（换引擎不能「改坏了」）；
  3) 新引擎必须比旧版干净：误召回更少、字序敏感、不越词边界、模板补召仍在。
另外钉住索引生命周期：禁用/审批状态、新增/删除条目后，检索结果必须立刻跟上
（索引挂在 load()/save() 上，写操作都走 save()）。

不需要 AstrBot 运行时。运行：
  python tests\\test_kb_search_engine.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    DEFAULT_SEARCH_ENGINE,
    ModKnowledgeBase,
)

FP = "e38d99c65853"

# 语料：真实沉淀风格，外加两个「旧版必然误召回」的探针
SEED = [
    ("tac 满改 mp5 方案", "Scope=... Under_Barrel=... 命令 /give @s tac:hk_mp5a5{Attachments:{...}}",
     "tac", "instance"),
    ("tac 附件 tagkey 大写驼峰规则", "槽位键名必须大写驼峰：Scope / Under_Barrel / Side_Rail",
     "tac", "template"),
    ("create 黄铜锭", "create:brass_ingot 由铜锭 + 锌锭在混合器产出", "create", "instance"),
    ("通用 指令 格式 规则", "命令方块指令的通用书写格式", "general", "template"),
    ("新手 攻略 通用 模板", "给新手的通用玩法模板", "general", "template"),
    ("taconite 铁矿 冶炼", "taconite 铁矿的冶炼流程（别的模组）", "taconite", "instance"),
    ("createaddon 附属 说明", "createaddon 是机械动力的附属扩展", "createaddon", "instance"),
]

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def fresh(data_dir: Path, engine: str | None = None) -> ModKnowledgeBase:
    kwargs = {} if engine is None else {"search_engine": engine}
    kb = ModKnowledgeBase(str(data_dir), server_id=FP, preset_id="p1", preset_name="测试库",
                          fingerprint=FP, **kwargs)
    for topic, content, mod, kind in SEED:
        kb.save_entry(topic, content, source="seed", status="verified", manual=True)
        key = kb._topic_key(topic)          # 主题键会被归一化（strip+lower）
        kb._data["entries"][key]["mod"] = mod
        kb._data["entries"][key]["kind"] = kind
    kb.save()
    return kb


def topics(kb: ModKnowledgeBase, q: str, limit: int = 10) -> list[str]:
    return [r["topic"] for r in kb.search(q, limit=limit)]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        print("[1] 默认引擎 = bm25；非法值回落默认")
        kb = fresh(base / "kb1")
        check(f"默认引擎是 bm25（实际 {kb.search_engine}）", kb.search_engine == DEFAULT_SEARCH_ENGINE)
        kb2 = fresh(base / "kb2", engine="不存在的引擎")
        check("非法引擎名回落 bm25", kb2.search_engine == DEFAULT_SEARCH_ENGINE)
        kb3 = fresh(base / "kb3", engine="legacy")
        check("显式 legacy 生效", kb3.search_engine == "legacy")

        print("\n[2] 正常问法：两引擎都必须把正确条目排第 1（换引擎不改坏）")
        for q, gold in [
            ("满配 mp5", "tac 满改 mp5 方案"),
            ("tac 附件怎么写", "tac 附件 tagkey 大写驼峰规则"),
            ("黄铜锭怎么获得", "create 黄铜锭"),
        ]:
            got_b = topics(fresh(base / "q_b", engine="bm25"), q)
            got_l = topics(fresh(base / "q_l", engine="legacy"), q)
            check(f"bm25 「{q}」首位 = {gold}", got_b and got_b[0] == gold, f"实际 {got_b[:3]}")
            check(f"legacy「{q}」首位 = {gold}", got_l and got_l[0] == gold, f"实际 {got_l[:3]}")

        print("\n[3] 新引擎更干净：误召回条数必须显著少于旧版")
        noisy = "通用 方案 规则 格式 指令 模板"
        n_b = len(topics(fresh(base / "n_b", engine="bm25"), noisy, limit=10))
        n_l = len(topics(fresh(base / "n_l", engine="legacy"), noisy, limit=10))
        check(f"bm25 返回 {n_b} 条 < legacy 返回 {n_l} 条", n_b < n_l)

        print("\n[4] 字序敏感：bigram 让「铜黄」不再等于「黄铜」")
        check("legacy「铜黄」误命中「create 黄铜锭」（单字匹配）",
              "create 黄铜锭" in topics(fresh(base / "o_l", engine="legacy"), "铜黄"))
        check("bm25「铜黄」不再命中「create 黄铜锭」（保留字序）",
              "create 黄铜锭" not in topics(fresh(base / "o_b", engine="bm25"), "铜黄"))

        print("\n[5] 不越词边界：查 tac 不该捞出 taconite，查 create 不该捞出 createaddon")
        check("bm25「tac」不命中 taconite 条目",
              "taconite 铁矿 冶炼" not in topics(fresh(base / "w_b", engine="bm25"), "tac"))
        check("bm25「create」不命中 createaddon 条目",
              "createaddon 附属 说明" not in topics(fresh(base / "w_b2", engine="bm25"), "create"))
        check("（对照）legacy「tac」会误命中 taconite 条目",
              "taconite 铁矿 冶炼" in topics(fresh(base / "w_l", engine="legacy"), "tac"))

        print("\n[6] 索引生命周期：写操作后检索立刻跟上（索引挂 load()/save()）")
        kb = fresh(base / "life", engine="bm25")
        kb.save_entry("ftbquests 任务书 章节 解锁", "依赖关系写在 dependencies 字段",
                      source="test", status="verified", manual=True)
        check("新增条目后立刻可检索到", "ftbquests 任务书 章节 解锁" in topics(kb, "任务书章节解锁"))
        kb.set_enabled("ftbquests 任务书 章节 解锁", False)
        check("禁用后立刻查不到（索引已跟新）", "ftbquests 任务书 章节 解锁" not in topics(kb, "任务书章节解锁"))
        kb.set_enabled("ftbquests 任务书 章节 解锁", True)
        check("重新启用后又能查到", "ftbquests 任务书 章节 解锁" in topics(kb, "任务书章节解锁"))
        kb.delete_entry("ftbquests 任务书 章节 解锁")
        check("删除后查不到", "ftbquests 任务书 章节 解锁" not in topics(kb, "任务书章节解锁"))

        print("\n[7] 待审批条目两引擎都不参与检索")
        kb = fresh(base / "pend", engine="bm25")
        kb.state.auto_apply = False
        kb.save_entry("待审 条目 内容", "x", source="test", status="pending", manual=False)
        check("bm25 不返回 pending 条目", "待审 条目 内容" not in topics(kb, "待审条目内容"))
        kb.state.auto_apply = True
        kb.set_search_engine("legacy")
        check("legacy 也不返回 pending 条目", "待审 条目 内容" not in topics(kb, "待审条目内容"))

        print("\n[8] 模板补召（举一反三）两引擎都保留")
        kb = fresh(base / "tmpl", engine="bm25")
        got = topics(kb, "tac")
        check("tac 查询能带出同模组 template 模板",
              "tac 附件 tagkey 大写驼峰规则" in got, f"实际 {got}")

        print("\n[9] 热切换引擎：不重建实例，立刻换引擎")
        kb = fresh(base / "hot", engine="bm25")
        check("初始 bm25", kb.search_engine == "bm25")
        kb.set_search_engine("legacy")
        check("切到 legacy 生效", kb.search_engine == "legacy")
        check("legacy 下索引已释放（_index is None）", kb._index is None)
        kb.set_search_engine("bm25")
        check("切回 bm25 重建了索引", kb._index is not None)
        kb.set_search_engine("乱写")
        check("非法值回落 bm25", kb.search_engine == "bm25")

        print("\n[10] 空查询 / 无匹配：必须返回空列表，不能抛错")
        kb = fresh(base / "edge", engine="bm25")
        check("空查询 → []", kb.search("") == [])
        check("纯空白查询 → []", kb.search("   ") == [])
        check("完全无关的查询 → []", kb.search("zzz_never_exists_qqq") == [])

        print("\n[11] 配置键贯通：schema / 后端白名单 / 前端绑定 三处必须一致")
        # 这一节防「只改了一半」：加了配置项却忘了进保存白名单 → 保存看着成功、值被静默丢弃；
        # 或前端没挂上 → 页面上根本看不到这个选项。
        plugin = Path(__file__).resolve().parents[1]
        schema = json.loads((plugin / "_conf_schema.json").read_text(encoding="utf-8"))
        item = schema["knowledge"]["items"]["knowledge_search_engine"]
        check("schema 里有 knowledge_search_engine", bool(item))
        check(f"schema 默认值 = bm25（实际 {item.get('default')}）", item.get("default") == "bm25")
        check(f"schema 选项 = 两档（实际 {item.get('options')}）",
              sorted(item.get("options") or []) == ["bm25", "legacy"])

        api_src = (plugin / "core" / "web_api.py").read_text(encoding="utf-8")
        check("后端保存白名单含该键",
              '"knowledge_search_engine": ("bm25", "legacy")' in api_src)
        check("后端保存后触发立即切换（调 set_search_engine）",
              "_kbman.set_search_engine(" in api_src)

        html = (plugin / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
        check("前端有 <select id=cfg_kb_engine>", 'id="cfg_kb_engine"' in html)
        check("前端 CFG_FIELDS 已挂绑定",
              '["cfg_kb_engine","knowledge_search_engine","s"]' in html)
        check("前端 S_DEFAULTS 已补兜底", '"cfg_kb_engine": "bm25"' in html)
        ui_opts = re.findall(r'<select id="cfg_kb_engine">(.*?)</select>', html, re.S)
        ui_vals = re.findall(r'<option value="([^"]+)"', ui_opts[0]) if ui_opts else []
        check(f"前端下拉选项与 schema 一致（实际 {ui_vals}）",
              sorted(ui_vals) == ["bm25", "legacy"])

        main_src = (plugin / "main.py").read_text(encoding="utf-8")
        check("main.py 读配置并下发到 KnowledgePresetManager",
              "search_engine=self._kb_engine()" in main_src)

    print()
    if failed:
        print(f"❌ {len(failed)} 项未通过：" + "、".join(failed))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
