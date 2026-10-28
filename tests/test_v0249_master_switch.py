# -*- coding: utf-8 -*-
"""v0.24.9 回归：两栏各自的栏级总开关（主人 2026-10-22 / 10-28 两轮要求定稿）。

背景
====
设置页「AI 能力 · LLM 工具与知识库」是左右两栏：**LLM 工具开关**（12 项）与**词典与知识库**（11 项）。
主人第一次说「缺一个总开关」，皮莉卡做成了覆盖整栏的一个；主人随即纠正：
**要的是两栏各一个** —— `tools.enable_all_tools` 与 `knowledge.enable_all_knowledge`。

判据（四条语义，写死在这里）
============================
1. **各管各栏**：工具总开关只管「LLM 工具」那一栏（含 mc_workflow / mc_reload_plugin），
   知识库总开关只管「词典与知识库」那一栏（三个 mc_*_knowledge + 词典本身的可用性）；
   互不误伤 —— 关掉一栏，另一栏照常。
2. **不改写单项**：总开关只读不写；关掉再打开，用户原来的勾选还在。
3. **回执分得清**：说是「哪一栏的总开关」还是「单项停用」。
4. **fail 方向不同**：单项 fail-closed（读不懂→关）；总开关 fail-open（读不懂→开）。

运行：<AstrBot python> tests\\test_v0249_master_switch.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
import logging  # noqa: E402

import astrbot_plugin_Scintilla_MC_Server_Control.main as M  # noqa: E402

PASSED = [0]
FAILED: list[str] = []

TOOLS_COLUMN = (
    "mc_execute_command", "mc_list_players", "mc_server_status", "mc_connection_status",
    "mc_broadcast", "mc_give_item", "mc_kick", "mc_ban", "mc_search_item",
    "mc_get_recipes", "mc_list_mods", "mc_reload_plugin", "mc_workflow",
)
KNOWLEDGE_COLUMN = ("mc_search_knowledge", "mc_save_knowledge", "mc_correct_knowledge")


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


def _plug(**cfg) -> "M.McControlPlugin":
    p = M.McControlPlugin.__new__(M.McControlPlugin)
    p.config = dict(cfg)
    p._cfg_index = {}
    p.logger = logging.getLogger("test-master")
    return p


def _all_on() -> dict:
    cfg = {f"enable_{t}": True for t in TOOLS_COLUMN + KNOWLEDGE_COLUMN}
    cfg.update({"enable_all_tools": True, "enable_all_knowledge": True,
                "dictionary_enabled": True, "knowledge_enabled": True})
    return cfg


def part_one() -> None:
    print("---- 一、schema：两栏各自一个总开关（不再有合并的 AI 总开关）----")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    check("★没有 master 组了（主人要的是两栏各一个）", "master" not in schema, str(list(schema)[:3]))
    t_item = (schema.get("tools", {}).get("items") or {}).get("enable_all_tools") or {}
    k_item = (schema.get("knowledge", {}).get("items") or {}).get("enable_all_knowledge") or {}
    check("★tools.enable_all_tools = bool / 默认 true",
          t_item.get("type") == "bool" and t_item.get("default") is True, str(t_item)[:80])
    check("★knowledge.enable_all_knowledge = bool / 默认 true",
          k_item.get("type") == "bool" and k_item.get("default") is True, str(k_item)[:80])
    check("两项都写清「不改写单项」", "不改写" in (t_item.get("hint") or "")
          and "不改写" in (k_item.get("hint") or ""))


def part_two() -> None:
    print("\n---- 二、各管各栏：关一栏不误伤另一栏 ----")
    tools_off = _plug(**{**_all_on(), "enable_all_tools": False})
    bad = [t for t in TOOLS_COLUMN if tools_off._tool_enabled(t)]
    check(f"★工具总开关关 → {len(TOOLS_COLUMN)} 个工具全部不可用", not bad, str(bad))
    ok_kb = [t for t in KNOWLEDGE_COLUMN if tools_off._tool_enabled(t)]
    check("★同一状态下知识工具**照常可用**（各管各栏）", len(ok_kb) == len(KNOWLEDGE_COLUMN), str(ok_kb))

    kb_off = _plug(**{**_all_on(), "enable_all_knowledge": False})
    bad_kb = [t for t in KNOWLEDGE_COLUMN if kb_off._tool_enabled(t)]
    check("★知识库总开关关 → 三个知识工具全部不可用", not bad_kb, str(bad_kb))
    ok_t = [t for t in TOOLS_COLUMN if kb_off._tool_enabled(t)]
    check("★同一状态下工具**照常可用**（各管各栏）", len(ok_t) == len(TOOLS_COLUMN), str(ok_t))

    both = _plug(**{**_all_on(), "enable_all_tools": False, "enable_all_knowledge": False})
    check("两栏都关 → 全部不可用",
          not [t for t in TOOLS_COLUMN + KNOWLEDGE_COLUMN if both._tool_enabled(t)])


def part_three() -> None:
    print("\n---- 三、回执分得清：哪一栏的总开关 vs 单项 ----")
    tools_off = _plug(**{**_all_on(), "enable_all_tools": False})
    msg_t = tools_off._tool_off_reason("mc_give_item")
    check("★工具总开关关 → 回执点名「LLM 工具」",
          "「LLM 工具」总开关已关闭" in msg_t, msg_t)
    check("回执给出位置（WebUI → 插件设置 → AI 能力）", "WebUI" in msg_t and "AI 能力" in msg_t, msg_t)

    kb_off = _plug(**{**_all_on(), "enable_all_knowledge": False})
    msg_k = kb_off._tool_off_reason("mc_search_knowledge")
    check("★知识库总开关关 → 回执点名「词典与知识库」",
          "「词典与知识库」总开关已关闭" in msg_k, msg_k)

    single = _plug(**{**_all_on(), "enable_mc_give_item": False})
    msg_s = single._tool_off_reason("mc_give_item")
    check("★单项关闭 → 回执**不提**总开关", "总开关" not in msg_s and "停用" in msg_s, msg_s)
    check("可用工具回执为空串", single._tool_off_reason("mc_ban") == "")

    print("\n---- 四、词典不可用时的归因也跟着总开关走 ----")
    reason = kb_off._dictionary_unavailable_reason()
    check("★知识库总开关关 → 词典类工具报「词典与知识库总开关已关闭」",
          "「词典与知识库」总开关已关闭" in reason, reason)
    dic_off = _plug(**{**_all_on(), "dictionary_enabled": False})
    check("单项（词典）关 → 仍报单项，不甩锅总开关",
          "总开关" not in dic_off._dictionary_unavailable_reason(),
          dic_off._dictionary_unavailable_reason())


def part_four() -> None:
    print("\n---- 五、不改写单项；fail 方向：总开关开、单项关 ----")
    cfg = _all_on()
    cfg.update({"enable_all_tools": False, "enable_mc_kick": True, "enable_mc_ban": False})
    snap = dict(cfg)
    p = _plug(**cfg)
    _ = [p._tool_enabled(t) for t in TOOLS_COLUMN]
    _ = p._tool_off_reason("mc_kick")
    check("★调用前后配置字典一模一样（只读，不写回）", cfg == snap)

    weird_master = _plug(**{**_all_on(), "enable_all_tools": "maybe"})
    check("工具总开关值读不懂 → 按默认（开），单项仍生效",
          weird_master._tool_enabled("mc_ban") is True)
    weird_kb = _plug(**{**_all_on(), "enable_all_knowledge": "maybe"})
    check("知识库总开关值读不懂 → 按默认（开）",
          weird_kb._tool_enabled("mc_save_knowledge") is True)
    bad_single = _plug(**{**_all_on(), "enable_mc_ban": "maybe"})
    check("单项值读不懂 → fail-closed（关）", bad_single._tool_enabled("mc_ban") is False)


def part_five() -> None:
    print("\n---- 六、页面与源码接线 ----")
    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    check("★页面有「LLM 工具总开关」控件", 'id="cfg_tools_master"' in html)
    check("★页面有「词典与知识库总开关」控件", 'id="cfg_kb_master"' in html)
    check("旧的合并总开关已移除", "cfg_ai_master" not in html and "AI 能力总开关" not in html)
    check("两个都登记进 CFG_FIELDS",
          '["cfg_tools_master","enable_all_tools","b",true]' in html
          and '["cfg_kb_master","enable_all_knowledge","b",true]' in html)
    check("各自说清「单项选择会原样保留」", html.count("原样保留") >= 2, str(html.count("原样保留")))

    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("★KNOWLEDGE_TOOLS 明确圈出知识类工具", "KNOWLEDGE_TOOLS" in src)
    check("★总开关归属判定走 _tool_master_off_reason", "self._tool_master_off_reason(" in src)
    check("旧的合并实现已清零", "_ai_master_enabled" not in src and '"ai_enabled"' not in src)
    check("15 个工具守卫仍走统一回执", src.count("self._tool_off_reason(") >= 15,
          f"{src.count('self._tool_off_reason(')} 处")
    api = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    check("★后端保存白名单两项都登记（否则前端保存会被判「未接受」）",
          '"enable_all_tools", "enable_all_knowledge"' in api
          or ('"enable_all_tools"' in api and '"enable_all_knowledge"' in api))


def main() -> int:
    print("=" * 78)
    print("v0.24.9 回归 · 两栏各自的栏级总开关")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    part_five()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （两栏各一个总开关，各管各栏，单项不丢，回执分得清）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
