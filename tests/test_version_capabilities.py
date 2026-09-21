# -*- coding: utf-8 -*-
"""v0.22.6 批次 2 · 版本能力上下文（``core/version_caps.py``）。

改代码前先读这三条设计要点
==========================
1. **代码决定语法世代，LLM 只填 ID 与数值。**
   事故根因不是「提示词写得不够好」，而是**没有任何一处把服务端版本告诉 LLM** ——
   ``detect_server_version()`` 早就存在，却只喂给 WebUI 的 ``/server/status``。
   所以修复方向是「由版本算出约束片段并注入」，而不是继续雕提示词。
2. **不猜。** 版本未知时**必须**显式降级（禁止构造带数据命令 + 告诉用户去哪填），
   绝不能默认某个版本，也不能静默回退到探测结果。
3. **优先级：手动声明 > 文件探测 > 未知**（补丁 3）。
   异地 RCON 模式探测**必然**失败，没有手动声明出口 = 所有附魔请求被拒且无法解除，
   那是功能性倒退。手动声明是用户**显式提供的事实**，不是猜。
"""
import os
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from astrbot_plugin_Scintilla_MC_Server_Control.core import version_caps as vc
from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (
    looks_like_complex_task,
)

_fail: list[str] = []
_skip: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


def parse_cases() -> None:
    print("---- 一、版本解析（不得被 Forge 版本号带偏）----")
    check("★'MC 1.20.1 · Forge 47.4.23' → 1.20.1（不是 Forge 的 47.4.23）",
          vc.parse_mc_version("MC 1.20.1 · Forge 47.4.23") == (1, 20, 1),
          str(vc.parse_mc_version("MC 1.20.1 · Forge 47.4.23")))
    check("手填 '1.20.1' → 1.20.1", vc.parse_mc_version("1.20.1") == (1, 20, 1))
    check("'paper 1.20.1' → 1.20.1（非 Forge 形态同样吃）",
          vc.parse_mc_version("paper 1.20.1") == (1, 20, 1))
    check("'1.21'（无 patch）→ (1, 21)", vc.parse_mc_version("1.21") == (1, 21))
    check("'forge 1.12.2' → 1.12.2", vc.parse_mc_version("forge 1.12.2") == (1, 12, 2))
    check("★'Forge 47.4.23'（只有 Forge 版本）→ None（不猜成 MC 版本）",
          vc.parse_mc_version("Forge 47.4.23") is None,
          str(vc.parse_mc_version("Forge 47.4.23")))
    check("'最新版' → None（解析不了就是解析不了）", vc.parse_mc_version("最新版") is None)
    check("空串 → None", vc.parse_mc_version("") is None)


def syntax_cases() -> None:
    print("---- 二、语法世代（1.20.5 分水岭）----")
    check("★1.20.4 → legacy_nbt（分水岭前一格）",
          vc.item_syntax_for((1, 20, 4)) == vc.ITEM_SYNTAX_LEGACY)
    check("★1.20.5 → components（分水岭本身算新语法）",
          vc.item_syntax_for((1, 20, 5)) == vc.ITEM_SYNTAX_COMPONENTS)
    check("1.20.1 → legacy_nbt（本次事故版本）",
          vc.item_syntax_for((1, 20, 1)) == vc.ITEM_SYNTAX_LEGACY)
    check("1.21 → components", vc.item_syntax_for((1, 21)) == vc.ITEM_SYNTAX_COMPONENTS)
    check("★版本未知 → unknown（**不得**默认成 NBT、更不得默认成组件）",
          vc.item_syntax_for(None) == vc.ITEM_SYNTAX_UNKNOWN)
    check("分水岭常量就是 1.20.5", vc.ITEM_COMPONENTS_CUTOVER == (1, 20, 5))


def enchant_cases() -> None:
    print("---- 三、附魔 ID 版本化（事故第二处根因）----")
    check("★1.20.1：sweeping_edge 折算回 sweeping",
          vc.enchantment_id_for((1, 20, 1), "minecraft:sweeping_edge") == "minecraft:sweeping")
    check("★1.21：sweeping_edge 保持 sweeping_edge",
          vc.enchantment_id_for((1, 21), "minecraft:sweeping_edge") == "minecraft:sweeping_edge")
    check("★1.20.1 对照表 =「sweeping_edge → sweeping」",
          vc.enchantment_renames_for((1, 20, 1))
          == [("minecraft:sweeping_edge", "minecraft:sweeping")],
          str(vc.enchantment_renames_for((1, 20, 1))))
    check("★1.21 对照表方向**必须反过来**（sweeping → sweeping_edge）",
          vc.enchantment_renames_for((1, 21))
          == [("minecraft:sweeping", "minecraft:sweeping_edge")],
          str(vc.enchantment_renames_for((1, 21))))
    check("版本未知 → 对照表为空（不猜）", vc.enchantment_renames_for(None) == [])
    check("未改名的附魔原样返回",
          vc.enchantment_id_for((1, 20, 1), "minecraft:sharpness") == "minecraft:sharpness")


def priority_cases() -> None:
    print("---- 四、优先级：手动声明 > 文件探测 > 未知（补丁 3）----")
    i = vc.resolve_version_info("1.20.1", "MC 1.21.4 · Forge x")
    check("★手动声明压过探测结果",
          i.mc == (1, 20, 1) and i.source == "override", f"{i.mc}/{i.source}")
    i = vc.resolve_version_info("", "MC 1.20.1 · Forge 47.4.23")
    check("无声明时用探测结果",
          i.mc == (1, 20, 1) and i.source == "detected", f"{i.mc}/{i.source}")
    i = vc.resolve_version_info("", "")
    check("★都没有 → unknown（不是默认版本）",
          i.mc is None and i.source == "unknown", f"{i.mc}/{i.source}")
    i = vc.resolve_version_info("最新版", "MC 1.20.1 · Forge 47.4.23")
    check("★声明了但解析不了 → unknown，**不静默回退**到探测结果",
          i.mc is None and i.source == "unknown" and i.raw == "最新版",
          f"{i.mc}/{i.source}/{i.raw}")


def context_cases() -> None:
    print("---- 五、注入片段（Agent 实际看到的东西）----")
    ctx = vc.build_version_context(vc.resolve_version_info("", "MC 1.20.1 · Forge 47.4.23"))
    check("★含正确模板：{} NBT 写法", "Enchantments:[{id:" in ctx, ctx[:200])
    check("★显式禁止 1.20.5+ 的组件写法", "禁止" in ctx and "<物品ID>[...]" in ctx, ctx[:400])
    check("★附魔对照表点名 sweeping_edge 要改",
          "minecraft:sweeping_edge" in ctx and "minecraft:sweeping`" in ctx, "")
    check("★如实标注来源（不假装探测一定成功）", "服务端文件探测" in ctx, "")
    check("含通用附魔 ID 表（减少 LLM 记忆负担）",
          "minecraft:sharpness" in ctx and "经验修补" in ctx, "")

    ctx21 = vc.build_version_context(vc.resolve_version_info("1.21", ""))
    check("★1.21 片段禁止旧 NBT 写法、要求组件写法",
          "components" in ctx21 and "<物品ID>{...}" in ctx21, ctx21[:400])
    check("★1.21 的附魔对照方向与 1.20.1 相反",
          "minecraft:sweeping` → 本版本应为 `minecraft:sweeping_edge" in ctx21, "")

    unknown = vc.build_version_context(vc.resolve_version_info("", ""))
    check("★版本未知 → 明确禁止构造带数据命令", "禁止构造" in unknown, unknown[:300])
    check("★版本未知 → 给出逃生出口（告诉用户去哪填）",
          "server_version_override" in unknown and "item_syntax_override" in unknown, "")
    check("★版本未知 → 片段**不为空**（空串等于让 LLM 凭记忆猜）",
          unknown.strip() != "")


def escape_hatch_cases() -> None:
    print("---- 六、补丁 3 逃生出口：手填语法世代后异地模式放行 ----")
    info = vc.resolve_version_info("", "")   # 版本未知（典型异地模式）
    ctx = vc.build_version_context(info, "auto")
    check("★版本未知 + auto → 仍然拒绝（这是安全默认）", "禁止构造" in ctx, "")
    ctx2 = vc.build_version_context(info, "legacy_nbt")
    check("★版本未知 + 手填 legacy_nbt → **放行**（否则异地用户永远被拒）",
          "禁止构造" not in ctx2 and "legacy_nbt" in ctx2, ctx2[:300])
    check("★放行后给的是 NBT 模板（按手填的世代）", "Enchantments:[{id:" in ctx2, "")
    ctx3 = vc.build_version_context(info, "components")
    check("版本未知 + 手填 components → 放行且给组件模板",
          "禁止构造" not in ctx3 and "enchantments={levels:" in ctx3, "")
    check("描述快照如实反映「未知 → 已拒绝」",
          vc.describe_capabilities(info)["item_syntax"] == "unknown",
          str(vc.describe_capabilities(info)["item_syntax"]))


def incident_regression_cases() -> None:
    print("---- 七、事故回归：1.20.1 上那条命令必须被两头堵住 ----")
    bad = 'give HuangGuaKnn netherite_sword[enchantments={levels:{"minecraft:sharpness":5}}] 1'
    check("★事故命令仍被路由守门判为 complex（不走无校验的 simple）",
          looks_like_complex_task("给我一把附魔剑", [{"command": bad}]) is True, "")
    ctx = vc.build_version_context(vc.resolve_version_info("", "MC 1.20.1 · Forge 47.4.23"))
    check("★1.20.1 片段同时堵住两处错：禁止组件语法 + 点名 sweeping_edge",
          "<物品ID>[...]" in ctx and "minecraft:sweeping_edge" in ctx, "")
    check("分水岭清单含 1.20.5 物品组件（实施单 Q1c 的核心分水岭）",
          any(c["version"] == (1, 20, 5) for c in vc.CUTOVERS), "")


def main() -> int:
    parse_cases()
    syntax_cases()
    enchant_cases()
    priority_cases()
    context_cases()
    escape_hatch_cases()
    incident_regression_cases()
    print("==========================================")
    if _fail:
        print(f"FAILED {len(_fail)} 项：")
        for f in _fail:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{_pass} 项）：版本能力表能算出正确的语法世代与附魔 ID，"
          f"版本未知时如实降级且留有出口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
