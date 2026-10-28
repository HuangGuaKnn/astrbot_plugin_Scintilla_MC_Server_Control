# -*- coding: utf-8 -*-
"""v0.24.9 回归：两栏各自的栏级总开关（含词典类工具的 AND 语义）。

背景（两轮要求 + 一次外部复核）
================================
* 用户第一次说「缺一个总开关」→ 皮莉卡做成覆盖整栏的一个；用户随即纠正「要两栏各一个」。
* 外部复核（2026-10-28）抓到一处**承诺与实现不一致**：关掉「词典与知识库」总开关后，
  `mc_search_item` / `mc_get_recipes` / `mc_list_mods` 照常能用（它们只被「工具栏」总开关管着），
  而 CHANGELOG 与界面都写着「物品词典一起停用」。→ 现已改成**三组工具**：

    PURE_TOOLS  纯 RCON 工具：只受「LLM 工具」总开关管
    DICT_TOOLS  要用物品词典的工具：两个总开关 **都要开**（AND）—— 工具被关或词典被关都不放行
    KB_TOOLS    三个知识工具：只受「词典与知识库」总开关管

判据
====
1. **各管各栏**：关工具栏 → PURE+DICT 停、KB 照常；关知识栏 → DICT+KB 停、PURE 照常。
2. **不改写单项**：总开关只读不写；关掉再打开，用户原来的勾选还在。
3. **回执分得清**：说是「哪一栏的总开关」还是「单项停用」；词典类工具在两条都关时先报工具栏。
4. **fail 方向不同**：单项 fail-closed（读不懂→关）；总开关 fail-open（读不懂→开）。
5. `mc_rescan_dictionary`（唯一没有单项开关的工具）也必须被两个总开关拦住。

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

PURE_TOOLS = (
    "mc_execute_command", "mc_list_players", "mc_server_status", "mc_connection_status",
    "mc_broadcast", "mc_give_item", "mc_kick", "mc_ban", "mc_reload_plugin", "mc_workflow",
)
DICT_TOOLS = ("mc_search_item", "mc_get_recipes", "mc_list_mods", "mc_rescan_dictionary")
KB_TOOLS = ("mc_search_knowledge", "mc_save_knowledge", "mc_correct_knowledge")
ALL_TOOLS = PURE_TOOLS + DICT_TOOLS + KB_TOOLS


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
    cfg = {f"enable_{t}": True for t in ALL_TOOLS}
    cfg.update({"enable_all_tools": True, "enable_all_knowledge": True,
                "dictionary_enabled": True, "knowledge_enabled": True})
    return cfg


def part_one() -> None:
    print("---- 一、schema：两栏各自一个总开关 ----")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    check("没有合并的 master 组", "master" not in schema, str(list(schema)[:3]))
    t_item = (schema.get("tools", {}).get("items") or {}).get("enable_all_tools") or {}
    k_item = (schema.get("knowledge", {}).get("items") or {}).get("enable_all_knowledge") or {}
    check("tools.enable_all_tools = bool / 默认 true",
          t_item.get("type") == "bool" and t_item.get("default") is True, str(t_item)[:60])
    check("knowledge.enable_all_knowledge = bool / 默认 true",
          k_item.get("type") == "bool" and k_item.get("default") is True, str(k_item)[:60])
    check("两项都写清「不改写单项」", "不改写" in (t_item.get("hint") or "")
          and "不改写" in (k_item.get("hint") or ""))


def part_two() -> None:
    print("\n---- 二、三组工具的分栏归属 ----")
    check("★源码有 _DICTIONARY_TOOLS（词典类工具单独一组）", hasattr(M, "_DICTIONARY_TOOLS"))
    check("★_DICTIONARY_TOOLS 内容正确", tuple(getattr(M, "_DICTIONARY_TOOLS", ())) == DICT_TOOLS,
          str(getattr(M, "_DICTIONARY_TOOLS", ())))
    check("★_KNOWLEDGE_TOOLS 仍只有三个知识工具",
          tuple(getattr(M, "_KNOWLEDGE_TOOLS", ())) == KB_TOOLS)

    tools_off = _plug(**{**_all_on(), "enable_all_tools": False})
    bad = [t for t in PURE_TOOLS + DICT_TOOLS if tools_off._tool_enabled(t)]
    check(f"★工具栏关 → 纯工具+词典类（{len(PURE_TOOLS) + len(DICT_TOOLS)} 个）全不可用", not bad, str(bad))
    ok_kb = [t for t in KB_TOOLS if tools_off._tool_enabled(t)]
    check("★同一状态下知识工具照常可用（各管各栏）", len(ok_kb) == len(KB_TOOLS), str(ok_kb))

    kb_off = _plug(**{**_all_on(), "enable_all_knowledge": False})
    bad_kb = [t for t in KB_TOOLS + DICT_TOOLS if kb_off._tool_enabled(t)]
    check(f"★知识栏关 → 知识工具+词典类（{len(KB_TOOLS) + len(DICT_TOOLS)} 个）全不可用 —— "
          f"**这是外部复核抓到的那处承诺不一致**", not bad_kb, str(bad_kb))
    ok_pure = [t for t in PURE_TOOLS if kb_off._tool_enabled(t)]
    check("★同一状态下纯工具照常可用（各管各栏）", len(ok_pure) == len(PURE_TOOLS), str(ok_pure))

    both = _plug(**{**_all_on(), "enable_all_tools": False, "enable_all_knowledge": False})
    check("两栏都关 → 全部不可用", not [t for t in ALL_TOOLS if both._tool_enabled(t)])

    print("\n---- 三、词典类的 AND 语义：任一栏关都不放行 ----")
    for label, cfg in (
        ("工具栏关 / 知识栏开", {"enable_all_tools": False, "enable_all_knowledge": True}),
        ("工具栏开 / 知识栏关", {"enable_all_tools": True, "enable_all_knowledge": False}),
    ):
        p = _plug(**{**_all_on(), **cfg})
        left = [t for t in DICT_TOOLS if p._tool_enabled(t)]
        check(f"{label} → 词典类全停", not left, str(left))


def part_three() -> None:
    print("\n---- 四、回执分得清 ----")
    tools_off = _plug(**{**_all_on(), "enable_all_tools": False})
    msg_t = tools_off._tool_off_reason("mc_give_item")
    check("★工具栏关 → 回执点名「LLM 工具」", "「LLM 工具」总开关已关闭" in msg_t, msg_t)

    kb_off = _plug(**{**_all_on(), "enable_all_knowledge": False})
    msg_k = kb_off._tool_off_reason("mc_search_knowledge")
    check("★知识栏关 → 回执点名「词典与知识库」",
          "「词典与知识库」总开关已关闭" in msg_k, msg_k)

    msg_d = kb_off._tool_off_reason("mc_search_item")
    check("★知识栏关 → 词典类工具的回执点名「词典与知识库」并说明原因",
          "「词典与知识库」总开关已关闭" in msg_d and "物品词典" in msg_d, msg_d)

    both_off = _plug(**{**_all_on(), "enable_all_tools": False, "enable_all_knowledge": False})
    msg_b = both_off._tool_off_reason("mc_search_item")
    check("★两条都关 → 先报「LLM 工具」（顺序稳定，便于用户按顺序开）",
          "「LLM 工具」总开关已关闭" in msg_b, msg_b)

    single = _plug(**{**_all_on(), "enable_mc_give_item": False})
    msg_s = single._tool_off_reason("mc_give_item")
    check("★单项关闭 → 回执不提总开关", "总开关" not in msg_s and "停用" in msg_s, msg_s)
    check("可用工具回执为空串", single._tool_off_reason("mc_ban") == "")

    print("\n---- 五、词典不可用的归因也跟着总开关走（叠加态） ----")
    p = _plug(**{**_all_on(), "enable_all_knowledge": False})
    p._dictionary = None
    check("★总开关关 + 词典缺失 → 报总开关，而不是「请填 server_dir」",
          "「词典与知识库」总开关已关闭" in p._dictionary_unavailable_reason(),
          p._dictionary_unavailable_reason())
    dic_off = _plug(**{**_all_on(), "dictionary_enabled": False})
    dic_off._dictionary = None
    check("单项（词典）关 → 报单项，不甩锅总开关",
          "总开关" not in dic_off._dictionary_unavailable_reason(),
          dic_off._dictionary_unavailable_reason())


def part_four() -> None:
    print("\n---- 六、不改写单项；fail 方向：总开关开、单项关 ----")
    cfg = _all_on()
    cfg.update({"enable_all_tools": False, "enable_mc_kick": True, "enable_mc_ban": False})
    snap = dict(cfg)
    p = _plug(**cfg)
    _ = [p._tool_enabled(t) for t in ALL_TOOLS]
    _ = p._tool_off_reason("mc_kick")
    check("★调用前后配置字典一模一样（只读，不写回）", cfg == snap)

    weird_master = _plug(**{**_all_on(), "enable_all_tools": "maybe"})
    check("工具栏值读不懂 → 按默认（开），单项仍生效",
          weird_master._tool_enabled("mc_ban") is True)
    weird_kb = _plug(**{**_all_on(), "enable_all_knowledge": "maybe"})
    check("知识栏值读不懂 → 按默认（开）",
          weird_kb._tool_enabled("mc_save_knowledge") is True)
    bad_single = _plug(**{**_all_on(), "enable_mc_ban": "maybe"})
    check("单项值读不懂 → fail-closed（关）", bad_single._tool_enabled("mc_ban") is False)


def part_five() -> None:
    print("\n---- 七、页面与源码接线 ----")
    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    check("页面有「LLM 工具总开关」控件", 'id="cfg_tools_master"' in html)
    check("页面有「词典与知识库总开关」控件", 'id="cfg_kb_master"' in html)
    check("旧的合并总开关已移除", "cfg_ai_master" not in html and "AI 能力总开关" not in html)
    check("两个都登记进 CFG_FIELDS",
          '["cfg_tools_master","enable_all_tools","b",true]' in html
          and '["cfg_kb_master","enable_all_knowledge","b",true]' in html)

    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("★`mc_rescan_dictionary` 也过闸门（原来两个总开关都拦不住）",
          '_off = self._tool_off_reason("mc_rescan_dictionary")' in src)
    check("旧的合并实现已清零", "_ai_master_enabled" not in src and '"ai_enabled"' not in src)
    check("15 个工具守卫仍走统一回执", src.count("self._tool_off_reason(") >= 16,
          f"{src.count('self._tool_off_reason(')} 处")
    api = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    check("后端保存白名单两项都登记", '"enable_all_tools"' in api and '"enable_all_knowledge"' in api)


def main() -> int:
    print("=" * 78)
    print("v0.24.9 回归 · 两栏各自的栏级总开关（含词典类 AND 语义）")
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
    print("全部通过 ✅ （两栏各管各的；词典类两个都得开；回执说得清是哪一栏挡的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
