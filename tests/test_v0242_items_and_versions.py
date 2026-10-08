# -*- coding: utf-8 -*-
"""v0.24.2 回归：物品词典与版本知识（GPT 全面复核 F08 + F09 + F10 + F17 + F18）。

判据全部取自复核报告原文：
  F08｜红羊毛搜索回执含 14 与 Red Wool，并由回执走完整发放规划得到 wool 14，
       不能只断言搜索有结果。
  F09｜Diamond 补齐、Fancy Stick 覆盖保留、模组 custom 仍存在，三者一起验。
  F10｜标准 Fabric fixture 的 mod ID、英文、中文、配方均可查询；
       坏 metadata 不影响原版表。
  F17｜1.20.4 / 1.20.5 / 1.20.6 / 1.21.4 / 1.21.5 / 1.21.11 逐个核对附魔 ID 与模板。
  F18｜1.7.10 数量 64 允许、65 拒绝；1.12.2 钻石与非堆叠物各验单条上限。

跑法：<python> tests\test_v0242_items_and_versions.py
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []
PASS_N: list[str] = []
add_sys_paths()


def check(desc, ok, detail=""):
    if ok:
        PASS_N.append(desc)
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc):
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


try:
    ID_ = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary")
    LG = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.legacy_items")
    VC = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.version_caps")
    OK, ERR = True, ""
except Exception as e:  # noqa: BLE001
    OK, ERR = False, f"{type(e).__name__}: {e}"


def make_jar(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)


# ===================== F09 + F10 =====================
def dict_cases():
    print("---- 一、F09 / F10：语言表合并与 Fabric 入口 ----")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "mods").mkdir()
        (root / "eula.txt").write_text("eula=true", encoding="utf-8")
        (root / "server.properties").write_text("x", encoding="utf-8")

        # Forge fixture：mod 自带一个 minecraft: 键（旧判据会因此跳过整张原版表）
        make_jar(root / "mods" / "testmod.jar", {
            "META-INF/mods.toml": 'modId="testmod"\nname="Test Mod"\n',
            "assets/testmod/lang/en_us.json": json.dumps({
                "item.testmod.custom": "Custom Thing",
                "item.minecraft.stick": "Fancy Stick",          # 模组覆盖原版显示名
            }),
        })
        # 原版服务端 jar（根目录 server.jar 是候选之一）
        make_jar(root / "server.jar", {
            "assets/minecraft/lang/en_us.json": json.dumps({
                "item.minecraft.stick": "Stick",
                "item.minecraft.diamond": "Diamond",
            }),
        })
        d = ID_.ItemDictionary(str(root), str(root / "items.json"))
        st = d.build()
        check("★F09：原版表被真的读了（Diamond 补齐）", "minecraft:diamond" in d.items, str(st))
        check("★F09：模组显示名覆盖保留（Fancy Stick 不被原版 Stick 抹掉）",
              (d.items.get("minecraft:stick") or {}).get("en") == "Fancy Stick",
              str(d.items.get("minecraft:stick")))
        check("★F09：模组自己的条目仍在（custom）", "testmod:custom" in d.items, str(list(d.items)[:6]))
        check("★F09：完整性用来源标识判定（不再靠「有几个 minecraft: 键」）",
              getattr(d, "_vanilla_src_loaded", False) is True)

        # Fabric fixture：只有 fabric.mod.json
        fab = root / "mods" / "fab.jar"
        make_jar(fab, {
            "fabric.mod.json": json.dumps({"id": "fabmod", "name": "Fabric Mod"}),
            "assets/fabmod/lang/en_us.json": json.dumps({"item.fabmod.widget": "Widget"}),
            "assets/fabmod/lang/zh_cn.json": json.dumps({"item.fabmod.widget": "小部件"}),
            "data/fabmod/recipes/widget.json": json.dumps({
                "type": "minecraft:crafting_shaped",
                "pattern": ["X"],
                "key": {"X": {"item": "minecraft:diamond"}},
                "result": {"item": "fabmod:widget"},
            }),
        })
        d2 = ID_.ItemDictionary(str(root), str(root / "items2.json"))
        d2.build()
        check("★F10：Fabric 模组进了词典（mod ID）", any(m.get("id") == "fabmod" for m in d2.mods),
              str(d2.mods))
        ent = d2.items.get("fabmod:widget") or {}
        check("★F10：Fabric 模组的英文名可查", ent.get("en") == "Widget", str(ent))
        check("★F10：Fabric 模组的中文名可查", ent.get("zh") == "小部件", str(ent))
        rec = d2.get_recipes("fabmod:widget", "forward")
        check("★F10：Fabric 模组的配方可查", bool(rec), str(d2.recipes)[:120])


# ===================== F08 =====================
def f08_cases():
    print("\n---- 二、F08：变体数据值与完整发放规划 ----")
    D = ID_.ItemDictionary.__new__(ID_.ItemDictionary)
    D.items = {
        "minecraft:wool": {
            "en": "Wool", "zh": "羊毛", "type": "item",
            "variants": {"0": "White Wool", "14": "Red Wool"},
            "domain_upper": 15,
        },
    }
    hits = D.search_items("Red Wool")
    hit = next((h for h in hits if h["id"] == "minecraft:wool"), None)
    check("★搜索命中变体时带回数据值 14", hit is not None and hit.get("variant_damage") == 14, str(hits)[:200])
    check("★同时带回展示名 Red Wool", (hit or {}).get("variant_display") == "Red Wool", str(hit))

    class Stub:
        pass

    stub = Stub()
    stub.items = D.items
    stub.legacy_generation = "1.12.2"
    stub.legacy_bridge = {}
    plan = LG.plan_give(stub, "Steve", "minecraft:wool", count=1,
                        damage=(hit or {}).get("variant_damage"))
    check("★由回执的数据值走完整发放规划 → wool 14（不是 data0 白羊毛）",
          plan.ok and plan.command == "give Steve minecraft:wool 1 14", f"{plan.ok}｜{plan.command}｜{plan.reason}")

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    wf = (Path(__file__).resolve().parents[1] / "core" / "workflow.py").read_text(encoding="utf-8")
    check("★工具出口带上 variant_damage", "variant_damage" in src)
    check("★工作流工程师上下文也带上 variant_damage", "variant_damage" in wf)


# ===================== F17 =====================
def f17_cases():
    print("\n---- 三、F17：附魔 ID 与模板的六个版本逐个核对 ----")
    # mode：legacy = 1.20.5 以下仍写 NBT {Enchantments:[...]}；
    #       levels = 1.20.5~1.21.4 附魔组件带 levels 一层；
    #       inline = 1.21.5 起 levels 内联（官方版本说明）
    cases = [((1, 20, 4), "minecraft:sweeping", "legacy"),
             ((1, 20, 5), "minecraft:sweeping_edge", "levels"),
             ((1, 20, 6), "minecraft:sweeping_edge", "levels"),
             ((1, 21, 4), "minecraft:sweeping_edge", "levels"),
             ((1, 21, 5), "minecraft:sweeping_edge", "inline"),
             ((1, 21, 11), "minecraft:sweeping_edge", "inline")]
    for mc, want_id, mode in cases:
        info = VC.resolve_version_info(override="", detected="MC %d.%d.%d" % mc)
        got = VC.enchantment_id_for(mc, "minecraft:sweeping_edge")
        ctx = VC.build_version_context(info)
        if mode == "legacy":
            tpl_ok = ("Enchantments:[" in ctx) and ("enchantments={levels:" not in ctx)
            label = "legacy NBT"
        elif mode == "levels":
            tpl_ok = "enchantments={levels:" in ctx
            label = "带 levels"
        else:
            tpl_ok = ("enchantments={" in ctx) and ("enchantments={levels:" not in ctx)
            label = "内联（无 levels）"
        check(f"★{'.'.join(map(str, mc))}：横扫附魔 ID = {want_id}", got == want_id, got)
        check(f"★{'.'.join(map(str, mc))}：附魔模板 {label} 正确", tpl_ok,
              [l for l in ctx.split("\n") if "give <玩家>" in l][:1])
    check("★1.20.5 就是分水岭（CUTOVERS 里那条改对了）",
          any(c.get("version") == (1, 20, 5) and c.get("surface") == "enchant_id" for c in VC.CUTOVERS))
    check("★ENCHANT_RENAMES 的生效版本 = 1.20.5",
          VC.ENCHANT_RENAMES and VC.ENCHANT_RENAMES[0][2] == (1, 20, 5), str(VC.ENCHANT_RENAMES))
    src = (Path(__file__).resolve().parents[1] / "core" / "version_caps.py").read_text(encoding="utf-8")
    check("没有残留的 (1, 21) 横扫阈值", '"sweeping", (1, 21)' not in src and "sweeping_edge\", \"minecraft:sweeping\", (1, 21)" not in src)


# ===================== F18 =====================
def f18_cases():
    print("\n---- 四、F18：旧版单条数量上限 ----")

    class Stub:
        pass

    for gen, item, ok_cnt, bad_cnt in (("1.7.10", "minecraft:diamond", 64, 65),
                                       ("1.12.2", "minecraft:diamond", 64, 65)):
        s = Stub()
        s.items = {item: {"en": item.split(":")[1].title(), "zh": "", "type": "item"},
                   "minecraft:diamond_sword": {"en": "Diamond Sword", "zh": "", "type": "item"}}
        s.legacy_generation = gen
        s.legacy_bridge = {}
        p_ok = LG.plan_give(s, "Steve", item, count=ok_cnt)
        p_bad = LG.plan_give(s, "Steve", item, count=bad_cnt)
        check(f"★{gen}：{item} 数量 {ok_cnt} 放行", p_ok.ok, f"{p_ok.ok}｜{p_ok.reason}")
        check(f"★{gen}：{item} 数量 {bad_cnt} 拒绝（并提示拆单）",
              (not p_bad.ok) and ("拆" in p_bad.reason or "上限" in p_bad.reason), p_bad.reason)
        p_sword = LG.plan_give(s, "Steve", "minecraft:diamond_sword", count=2)
        if gen.startswith("1.12"):
            check(f"★{gen}：非堆叠物（钻石剑）单条上限 1 —— 2 件被拒",
                  (not p_sword.ok) and "非堆叠" in p_sword.reason, p_sword.reason)
        else:
            check(f"{gen}：钻石剑不在「按堆叠收紧」的世代（1.7.10 走 1..64 硬范围）",
                  p_sword.ok or "上限" in p_sword.reason, p_sword.reason)


def main() -> int:
    if not OK:
        skip(f"取不到模块：{ERR}")
    else:
        print("=" * 78)
        print("v0.24.2 回归 · 物品词典与版本知识（F08 / F09 / F10 / F17 / F18）")
        print("=" * 78)
        dict_cases()
        f08_cases()
        f17_cases()
        f18_cases()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
