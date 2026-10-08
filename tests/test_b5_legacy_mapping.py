# -*- coding: utf-8 -*-
"""B5 · 旧版物品映射（预扁平化世代）自检。

覆盖四条链（全部有 2026-10-07 两代真机实证支撑）：

1. **词典预扁平化通道**：1.7.10 / 1.12.2 真机服务端目录 → 世代键、变体族、桥接、检索带数据值；
2. **fail-closed 边界**：空目录 → 落空；版本不在任何 pre-1.13 世代 range 内 → 落空
   （绝不拿旧表套现代服务端）；
3. **版本门语义变更**：``<1.13`` → ``legacy_preflatten``；手填错世代仍不放行；
   注入给 LLM 的写法片段包含模板 / 域警告 / 数字附魔 ID / 禁扁平名；
4. **生成器判据链**：域内可发、**域外一律拒**、非变体不吃数据值、NBT 结构校验、注入防线。

运行：``python tests/test_b5_legacy_mapping.py``
"""

import os
import sys
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import legacy_items as li          # noqa: E402
from core import version_caps as vc          # noqa: E402
from core.item_dictionary import ItemDictionary  # noqa: E402

#: 真机靶场目录（**不写死本机路径**：可用环境变量覆盖，缺省找 <家>/Desktop/mcs-matrix）。
#: 找不到就让下面「一、词典通道」整段**跳过**，换台机器（如 CI）也能跑通其余部分。
MATRIX = os.environ.get("SCINTILLA_MATRIX_DIR") or os.path.join(
    os.path.expanduser("~"), "Desktop", "mcs-matrix")
#: 靶场两台服务端目录都在，才算「本机能做真机链条」；否则下面相关段落整体跳过。
HAS_MATRIX = (os.path.isdir(os.path.join(MATRIX, "1.7.10-forge"))
              and os.path.isdir(os.path.join(MATRIX, "1.12.2-forge")))
PASS, FAIL = [], []


def check(label, cond, extra=""):
    if cond:
        PASS.append(label)
        print("[PASS] " + label)
    else:
        FAIL.append(label + (" ｜ " + str(extra) if extra else ""))
        print("[FAIL] " + label + (" ｜ " + str(extra) if extra else ""))


def _mk_fake_jar(path, version_name, lang_text=None):
    """造一个最小服务端 jar：可只带 legacy .lang（模拟「无资产」或「现代版本名 + 旧 lang」）。"""
    with zipfile.ZipFile(path, "w") as z:
        if lang_text:
            z.writestr("assets/minecraft/lang/en_us.lang", lang_text)
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")


LANG_SNIPPET = "item.apple.name=Apple\ntile.wool.white.name=White Wool\ntile.wool.red.name=Red Wool\n"

print("=" * 78)
print("B5 自检 · 旧版物品映射（预扁平化世代）")
print("=" * 78)

# ---------------------------------------------------------------- 一、词典通道
print("\n---- 一、词典预扁平化通道（真机目录）----")
if not HAS_MATRIX:
    print("（跳过本段：本机没有靶场目录 %s）" % MATRIX)
    print("  提示：把靶场放好，或设 SCINTILLA_MATRIX_DIR 指向它，即可跑这一段。")
else:
    d710 = ItemDictionary(os.path.join(MATRIX, "1.7.10-forge"))
    s710 = d710.build()
    check("★1.7.10 目录 → 识别为预扁平化世代", d710.legacy_generation == "1.7.10", d710.legacy_generation)
    check("★1.7.10 → 变体族 ≥ 40", len(d710.legacy_variants) >= 40, len(d710.legacy_variants))
    check("1.7.10 → 桥接条数 ≥ 250", len(d710.legacy_bridge) >= 250, len(d710.legacy_bridge))
    r = d710.search_items("diamond sword", 1)
    check("★1.7.10 搜 diamond sword → minecraft:diamond_sword",
          bool(r) and r[0]["id"] == "minecraft:diamond_sword", r[:1])
    r = d710.search_items("red wool", 1)
    check("★1.7.10 搜 red wool → wool + 数据值 14",
          bool(r) and r[0]["id"] == "minecraft:wool" and r[0].get("variant_damage") == 14, r[:1])
    r = d710.search_items("podzol", 1)
    check("1.7.10 搜 podzol → dirt + 数据值 2",
          bool(r) and r[0]["id"] == "minecraft:dirt" and r[0].get("variant_damage") == 2, r[:1])
    check("1.7.10 → 域表带 domain_upper（dirt = 2）",
          isinstance(d710.legacy_variants.get("dirt", {}).get("domain_upper"), int)
          and d710.legacy_variants["dirt"]["domain_upper"] == 2,
          d710.legacy_variants.get("dirt", {}).get("domain_upper"))
    check("★1.7.10 域表内容与实测一致（wool 0 = Wool —— 1.7.10 就叫 Wool）",
          d710.legacy_variants.get("wool", {}).get("variants", {}).get("0") == "Wool",
          d710.legacy_variants.get("wool", {}).get("variants", {}).get("0"))

    d122 = ItemDictionary(os.path.join(MATRIX, "1.12.2-forge"))
    s122 = d122.build()
    check("★1.12.2 目录 → 识别为预扁平化世代", d122.legacy_generation == "1.12.2", d122.legacy_generation)
    check("★1.12.2 → 变体族 ≥ 40", len(d122.legacy_variants) >= 40, len(d122.legacy_variants))
    check("1.12.2 → 桥接条数 ≥ 350", len(d122.legacy_bridge) >= 350, len(d122.legacy_bridge))
    r = d122.search_items("chiseled red sandstone", 1)
    check("★1.12.2 搜 chiseled red sandstone → red_sandstone + 数据值 1",
          bool(r) and r[0]["id"] == "minecraft:red_sandstone" and r[0].get("variant_damage") == 1, r[:1])
    check("★1.12.2 域表含 banner（16 色）",
          d122.legacy_variants.get("banner", {}).get("domain_upper") == 15,
          d122.legacy_variants.get("banner", {}).get("domain_upper"))
    check("★代际展示名差异：1.7.10 Wool ≠ 1.12.2 White Wool",
          d710.legacy_variants.get("wool", {}).get("variants", {}).get("0") !=
          d122.legacy_variants.get("wool", {}).get("variants", {}).get("0"))
    check("★代际差异：1.12.2 展示名 Terracotta ≠ 1.7.10 Stained Clay",
          d122.legacy_variants.get("stained_hardened_clay", {}).get("variants", {}).get("0") == "White Terracotta"
          and d710.legacy_variants.get("stained_hardened_clay", {}).get("variants", {}).get("0") == "White Stained Clay",
          (d122.legacy_variants.get("stained_hardened_clay", {}).get("variants", {}).get("0"),
           d710.legacy_variants.get("stained_hardened_clay", {}).get("variants", {}).get("0")))

    # ------------------------------------------------- 二、fail-closed 边界

print("\n---- 二、fail-closed 边界 ----")
with tempfile.TemporaryDirectory() as td:
    de = ItemDictionary(td)
    de.build()
    check("★空目录 → 不装载任何世代（落空）", de.legacy_generation == "" and not de.items,
          (de.legacy_generation, len(de.items)))
with tempfile.TemporaryDirectory() as td:
    _mk_fake_jar(os.path.join(td, "server-1.21.11.jar"), "1.21.11", LANG_SNIPPET)
    dm = ItemDictionary(td)
    dm.build()
    check("★现代版本名 + 旧 lang → 仍落空（range 不覆盖，绝不拿旧表套现代）",
          dm.legacy_generation == "" and not dm.items, (dm.legacy_generation, len(dm.items)))
with tempfile.TemporaryDirectory() as td:
    _mk_fake_jar(os.path.join(td, "minecraft_server.1.12.2.jar"), "1.12.2", LANG_SNIPPET)
    dy = ItemDictionary(td)
    dy.build()
    check("★1.12.2 版本名 + 旧 lang → 命中世代 1.12.2",
          dy.legacy_generation == "1.12.2", dy.legacy_generation)
    check("★现代扁平名不得被当成注册名（red_sandstone 走族表，不走扁平名）",
          "minecraft:red_wool" not in dy.items, sorted(dy.items)[:5])

# ---------------------------------------------------------------- 三、版本门
print("\n---- 三、版本门（B5 语义变更）----")
check("★1.7.10 → legacy_preflatten", vc.item_syntax_for((1, 7, 10)) == vc.ITEM_SYNTAX_PREFLATTEN)
check("★1.12.2 → legacy_preflatten", vc.item_syntax_for((1, 12, 2)) == vc.ITEM_SYNTAX_PREFLATTEN)
check("★1.8 → legacy_preflatten", vc.item_syntax_for((1, 8)) == vc.ITEM_SYNTAX_PREFLATTEN)
check("★1.13 → legacy_nbt（分水岭不变）", vc.item_syntax_for((1, 13)) == vc.ITEM_SYNTAX_LEGACY)
check("1.20.4 → legacy_nbt", vc.item_syntax_for((1, 20, 4)) == vc.ITEM_SYNTAX_LEGACY)
check("1.20.5 → components", vc.item_syntax_for((1, 20, 5)) == vc.ITEM_SYNTAX_COMPONENTS)
check("版本未知 → unknown", vc.item_syntax_for(None) == vc.ITEM_SYNTAX_UNKNOWN)
konwn12 = vc.resolve_version_info(override="1.12.2", detected="")
check("★已知 1.12.2 + 手填 legacy_nbt → 不放行（世代由版本定）",
      vc.resolve_item_syntax(konwn12, "legacy_nbt")[0] == vc.ITEM_SYNTAX_PREFLATTEN,
      vc.resolve_item_syntax(konwn12, "legacy_nbt"))
check("★已知 1.12.2 + 手填 components → 同样不放行",
      vc.resolve_item_syntax(konwn12, "components")[0] == vc.ITEM_SYNTAX_PREFLATTEN,
      vc.resolve_item_syntax(konwn12, "components"))
check("已知 1.12.2 + auto → preflatten，来源=preflatten",
      vc.resolve_item_syntax(konwn12, "auto")[0] == vc.ITEM_SYNTAX_PREFLATTEN,
      vc.resolve_item_syntax(konwn12, "auto"))
unk = vc.resolve_version_info(override="", detected="")
check("★版本未知 + 手填 legacy_nbt → 仍放行（异地逃生出口保留）",
      vc.resolve_item_syntax(unk, "legacy_nbt") == (vc.ITEM_SYNTAX_LEGACY, "override"),
      vc.resolve_item_syntax(unk, "legacy_nbt"))

frag = vc.build_version_context(konwn12)
check("★片段标注世代 legacy_preflatten", "legacy_preflatten" in frag)
check("★片段给出正确写法模板（数据值位置参数）",
      "give <玩家> <物品ID> <数量> [数据值] [{NBT}]" in frag, frag[:200])
check("★片段点明「家族名 + 数据值」", "家族名 + 数据值" in frag)
check("★片段含域警告（客户端渲染崩溃）",
      ("合法区间" in frag and "客户端" in frag and "崩溃" in frag))
check("★片段明确禁止 1.13+ 扁平名（red_wool）", "red_wool" in frag and "禁止" in frag)
check("★片段给数字附魔 ID 写法（锋利 = 16）", "ench" in frag and "id:16" in frag)
check("★片段不再宣称「暂不支持自动生成命令」", "暂不支持自动生成命令" not in frag)
check("片段保留简单命令白名单（say / time 之类）", "say" in frag or "time" in frag)
cap = vc.describe_capabilities(konwn12)
check("★能力快照 supported=True", cap.get("supported") is True, cap.get("support_note"))
check("★能力快照 item_syntax=legacy_preflatten",
      cap.get("item_syntax") == vc.ITEM_SYNTAX_PREFLATTEN, cap.get("item_syntax"))
modern = vc.resolve_version_info(override="1.20.1", detected="")
frag_m = vc.build_version_context(modern)
check("★现代路径不受影响：1.20.1 片段仍给 {} NBT 模板且不含 preflatten 模板",
      "Enchantments" in frag_m and "legacy_preflatten" not in frag_m)
cap_m = vc.describe_capabilities(modern)
check("现代路径 supported 仍为 True", cap_m.get("supported") is True, cap_m.get("support_note"))

# ---------------------------------------------------------------- 四、生成器
print("\n---- 四、生成器判据链（1.12.2 / 1.7.10）----")
if HAS_MATRIX:
    p = li.plan_give(d122, "Knn", "diamond sword", 1)
    check("★普通物品 → give Knn minecraft:diamond_sword 1",
          p.ok and p.command == "give Knn minecraft:diamond_sword 1", p.command or p.reason)
    p = li.plan_give(d122, "Knn", "red wool", 1)
    check("★变体（按展示名）→ give Knn minecraft:wool 1 14",
          p.ok and p.command == "give Knn minecraft:wool 1 14", p.command or p.reason)
    p = li.plan_give(d122, "Knn", "minecraft:wool", 1, damage=0)
    check("显式数据值 0 也写进命令（位置参数语义）",
          p.ok and p.command == "give Knn minecraft:wool 1 0", p.command or p.reason)
    p = li.plan_give(d122, "Knn", "minecraft:planks", 1, damage=9)
    check("★域外数据值 9（木板域 0~5）→ 一律拒绝", (not p.ok) and "域" in p.reason, p.reason or p.command)
    p = li.plan_give(d122, "Knn", "minecraft:wool", 1, damage=99)
    check("★域外数据值 99 → 一律拒绝", (not p.ok) and "域" in p.reason, p.reason or p.command)
    p = li.plan_give(d122, "Knn", "minecraft:diamond_sword", 1, damage=3)
    check("★非变体物品不接受数据值", (not p.ok) and "变体" in p.reason, p.reason or p.command)
    p = li.plan_give(d122, "Knn", "netherite_sword", 1)
    check("★本世代没有的物品 → 拒绝（不猜）", not p.ok, p.reason or p.command)
    p = li.plan_give(d122, "Knn", "minecraft:double_stone_slab", 1)
    check("★1.12.2 不存在的族（double_stone_slab）→ 拒绝", not p.ok, p.reason or p.command)
    nbt = li.build_enchant_nbt([(16, 5)])
    p = li.plan_give(d122, "Knn", "diamond sword", 1, nbt=nbt)
    check("★附魔 NBT → give Knn minecraft:diamond_sword 1 0 {ench:[{id:16,lvl:5}]}",
          p.ok and p.command == "give Knn minecraft:diamond_sword 1 0 {ench:[{id:16,lvl:5}]}",
          p.command or p.reason)
    p = li.plan_give(d122, "Knn", "minecraft:wool", 2, damage=14, nbt='{display:{Name:"x"}}')
    check("★变体 + NBT → 数据值与 NBT 同时到位",
          p.ok and p.command == 'give Knn minecraft:wool 2 14 {display:{Name:"x"}}', p.command or p.reason)
    p = li.plan_give(d122, "Knn", "minecraft:wool", 1, damage=14, nbt="{ench:[{id:16,lvl:5}]")
    check("★NBT 花括号不配对 → 拒绝", not p.ok, p.reason or p.command)
    try:
        li.build_enchant_nbt([(999, 5)])
        check("★附魔 ID 越界 → ValueError", False, "未抛异常")
    except ValueError:
        check("★附魔 ID 越界 → ValueError", True)
    for bad in ["Knn; stop", "Knn op @a", "a b", "K" * 17, ""]:
        p = li.plan_give(d122, bad, "diamond sword", 1)
        check("★注入防线：玩家名 %r → 拒绝" % bad, not p.ok, p.reason or p.command)
    # v0.24.2（GPT 全面复核 F18）：6400 只是**任务总量**上限，不等于 give 语法允许 6400 ——
    # 单条命令还要过该代解析范围（1.12.2 上界 = 物品最大堆叠；非堆叠物为 1）。
    # 这里用的 `diamond sword` 正是非堆叠物，所以 6400 必须被拒，且拒因要给出拆单出路。
    for bad_n, want in [(0, False), (6401, False), (6400, False), ("x", False)]:
        p = li.plan_give(d122, "Knn", "diamond sword", bad_n)
        check("数量 %r → %s" % (bad_n, "接受" if want else "拒绝"), p.ok is want, p.command or p.reason)
    p = li.plan_give(d122, "Knn", "diamond sword", 6400)
    check("★6400 被拒的原因写明「单条超上限 + 拆单」（不是模糊的越界）",
          ("拆" in p.reason) and ("上限" in p.reason), p.reason)
    p = li.plan_give(d122, "Knn", "minecraft:diamond", 64)
    check("★1.12.2 堆叠物（钻石）64 仍放行（只收紧真正越界的条数）", p.ok is True, p.reason)
    p = li.plan_give(d710, "Knn", "red wool", 1)
    check("★1.7.10 词典同样工作（red wool → wool + 14）",
          p.ok and p.command == "give Knn minecraft:wool 1 14", p.command or p.reason)
    p = li.plan_give(d710, "Knn", "minecraft:red_sandstone", 1)
    check("★代际差：1.7.10 没有 red_sandstone → 拒绝", not p.ok, p.reason or p.command)
    check("★is_preflatten 判据（预扁平化词典 True / 空词典 False）",
          li.is_preflatten(d122) and not li.is_preflatten(ItemDictionary(".")))
    check("预览可用（失败给可读原因）", li.render_preview(d122, "Knn", "minecraft:planks", 1, 9).startswith("[拒绝]"))


# ================= P4 真机验收回填（2026-10-07 · 1.12.2 工地实测） =================
# 实测回执（不是猜的）：`give HuangGuaKnn minecraft:stick 1 0`
#   → "Given [Stick] * 1 to HuangGuaKnn"
# 而锚定成功模式表当时只有 1.16+ 的 `^gave\b`（"Gave 1 [X] to P"）→
# pre-1.13（1.7.10 / 1.12.2）的 give **永远**落 unknown（安全但误导：货已到、回执说未知）。
# 另实测反面文案：不存在的玩家 / 物品 → "… cannot be found" / "There is no such item with name …"

from core import command_result as _cr  # noqa: E402


def _judge(_cmd, _out):
    return _cr.classify_command_output(_cmd, _out, boundary_confirmed=True, response_received=True)


for _out, _tag in (
    ("Given [Stick] * 1 to HuangGuaKnn", "pre-1.13 实测措辞（1.7.10 / 1.12.2）"),
    ("Given [Red Wool] * 16 to HuangGuaKnn", "pre-1.13 · 变体展示名 + 数量"),
    ("Gave 1 [Diamond Sword] to Error", "1.16+ 措辞（人名是 Error 也不被失败词翻盘）"),
):
    _r = _judge("give HuangGuaKnn minecraft:stick 1", _out)
    check(f"P4：give 回执「{_out}」判成功（{_tag}）", _r.status == "success",
          f"实际 status={_r.status} reason={_r.reason}")

for _out, _tag in (
    ("Player '__Probe_Ghost__' cannot be found", "玩家不存在（实测）"),
    ("There is no such item with name minecraft:not_a_real_item", "物品不存在（实测）"),
    ("Givenchy bag dropped", "词边界：Givenchy 不得被 ^given\b 命中"),
):
    _r = _judge("give HuangGuaKnn minecraft:stick 1", _out)
    check(f"P4 反面：{_tag} 不得判成功", _r.status != "success", f"实际 status={_r.status}")

check("P4 登记：give 的锚定模式同时含 1.16+ 与 pre-1.13 两代措辞",
      any("gave" in p for p in _cr.ANCHORED_SUCCESS_PATTERNS.get("give", ()))
      and any("given" in p for p in _cr.ANCHORED_SUCCESS_PATTERNS.get("give", ())),
      str(_cr.ANCHORED_SUCCESS_PATTERNS.get("give")))
# ===================== 统一收尾（2026-10-08 · F15 修复） =====================
# **只此一处收尾**：不管有没有靶场、不管 FAIL 来自哪一节（含上面的 P4 段），都在这里汇总并定退出码。
# 旧写法把 ``sys.exit(1)`` 缩进在 ``if HAS_MATRIX:`` 里面、P4 断言又排在它之后 —— 于是
# 「打印了 [FAIL] 却退出 0」，无靶场的 CI 直接误绿（GPT 全面复核 F15；探针见复核报告）。
print("\n" + "=" * 78)
if FAIL:
    print("FAILED %d 项（共 %d）：" % (len(FAIL), len(PASS) + len(FAIL)))
    for _f in FAIL:
        print("  - " + _f)
    sys.exit(1)
print("全部通过（%d 项）：预扁平化世代「名优先 + 家族名 + 数据值」生成链，"
      "域外值一律拒绝，版本门按世代分派，现代路径不受影响。" % len(PASS))
