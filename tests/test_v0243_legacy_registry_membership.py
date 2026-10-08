# -*- coding: utf-8 -*-
"""v0.24.3 回归 · 旧版 Item 注册表成员资格（GPT v0.24.2 复核 N04）。

报告判据原文：
  · 拿 1.12.2 / 1.7.10 真 jar 抽 Item 注册表，逐条核对 bridge/family；
  · 点名目标缺失 / registry 不符必须 **FAIL**（不许 SKIP、不许拿「两代一致」顶替）；
  · 离线注册表 fixture 纳入 CI，不依赖作者机器有 jar。

这条判据要治的病（GPT 复核原文的意思）：``give`` 取的是服务端 **Item registry**。
旧表 13 条名字只是「方块名 / 别名 / 变体名」——``water`` ``lava`` ``fire`` ``portal``
``cocoa`` ``potatoes`` ``frosted_ice`` ``pumpkin_stem`` 是方块只读，``charcoal`` 只是
``coal`` 的 data 1，``oak_fence_gate`` / ``oak_door`` / ``light_gray_glazed_terracotta``
该代真名分别是 ``fence_gate`` / ``wooden_door`` / ``silver_glazed_terracotta``。整批能生成
命令、服务端当场报错；而旧夹具审计只比「两代是否一致」、数条数，**两代同错就全绿**
（把两代 apple 都改成不存在的 ID，用例照样通过）。

判据来源（本用例的 oracle）
=========================
``data/legacy_registry/items_<mc>.json`` —— 由 ``tests/make_legacy_registry_snapshot.py``
从**真 jar 字节码**抽出（显式物品注册 + 方块物品注册；堆叠上限走「基类默认 → 继承链
构造器 → 注册现场链式设置器」）。它是随插件发布的**离线事实快照**：CI 与运行期都不
需要 jar，``--check`` 又能随时拿真 jar 复核（见本用例第四部分）。

跑法：<python> tests\\test_v0243_legacy_registry_membership.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
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

ROOT = Path(__file__).resolve().parents[1]
REG_DIR = ROOT / "data" / "legacy_registry"
TDIR = ROOT / "data" / "legacy_items"
FIXDIR = ROOT / "tests" / "fixtures" / "legacy_registry"
EXTRACTOR = ROOT / "tests" / "make_legacy_registry_snapshot.py"

#: 快照规模（钉死；改了就必须同步更新提取器注释与本行 —— 数值本身是「真 jar 抽出来」的结论）
SNAP_ITEMS = {"1.7.10": 315, "1.12.2": 411}
SNAP_BLOCKS = {"1.7.10": 171, "1.12.2": 255}

#: 该代**真实存在**的注册名（点名目标，必须命中，不许 SKIP）
MUST_EXIST = {
    "1.12.2": ["diamond", "coal", "fence_gate", "wooden_door", "silver_glazed_terracotta",
               "end_portal_frame", "cake", "totem_of_undying", "bucket", "snowball"],
    "1.7.10": ["diamond", "coal", "fence_gate", "wooden_door", "cake", "water", "fire",
               "potato", "bucket", "snowball"],
}

#: 该代**不是** Item 注册名的名字（点名目标，必须缺席）
MUST_ABSENT = {
    "1.12.2": ["charcoal", "oak_door", "oak_fence_gate", "light_gray_glazed_terracotta",
               "water", "lava", "fire", "portal", "cocoa", "potatoes", "frosted_ice",
               "pumpkin_stem"],
    "1.7.10": ["charcoal", "oak_door", "oak_fence_gate", "frosted_ice", "pumpkin_stem",
               "silver_glazed_terracotta", "light_gray_glazed_terracotta"],
}

#: 堆叠上限点名（真值来自 jar：子类构造器 / 注册现场链式设置器）
MAX_PROBE = {
    "1.12.2": {"diamond": 64, "cake": 1, "elytra": 1, "totem_of_undying": 1, "potion": 1,
               "water_bucket": 1, "bucket": 16, "snowball": 16, "egg": 16, "banner": 16,
               "armor_stand": 16, "written_book": 16, "ender_pearl": 16,
               "iron_shovel": 1, "record_13": 1, "minecart": 1, "bed": 1},
}

#: 表里「展示名 → 注册名」的点名映射（键 = 展示名）
DISP_MUST = {
    "1.12.2": {"Charcoal": "coal", "Oak Door": "wooden_door", "Oak Fence Gate": "fence_gate",
               "End Portal Frame": "end_portal_frame", "Cocoa Beans": "dye", "Potato": "potato"},
    # 1.7.10 自己的 lang 叫 Wooden Door / Fence Gate（1.12.2 才叫 Oak Door / Oak Fence Gate）
    "1.7.10": {"Charcoal": "coal", "Wooden Door": "wooden_door", "Fence Gate": "fence_gate",
               "Cocoa Beans": "dye", "Potato": "potato"},
}

#: 被证伪的注册名 —— 只许出现在**展示名 / 键 / 注释**里，绝不许出现在 registry 位
FALSIFIED_AS_REGISTRY = ("charcoal", "oak_door", "oak_fence_gate", "light_gray_glazed_terracotta",
                         "frosted_ice", "pumpkin_stem")


def check(desc, ok, detail=""):
    if ok:
        PASS_N.append(desc)
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc):
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


def load_snap(gen):
    return json.loads((REG_DIR / f"items_{gen}.json").read_text(encoding="utf-8"))


def load_table(gen):
    return json.loads((TDIR / f"{gen}.json").read_text(encoding="utf-8"))


def disp_index(table):
    """展示名 → 注册名（桥接 + 变体族都算；冲突取先出现的，够用于点名）。"""
    out: dict[str, str] = {}
    for v in (table.get("bridge") or {}).values():
        d = (v.get("display") or "").strip()
        if d:
            out.setdefault(d, v.get("registry"))
    for fam, info in (table.get("variants") or {}).items():
        for _, disp in (info.get("variants") or {}).items():
            if disp:
                out.setdefault(str(disp).strip(), fam)
    return out


def membership_violations(table, snapshot):
    """表的每条 registry / 每个族是否都在该代真注册表里。返回违规清单（可测的判据本体）。"""
    items = set((snapshot.get("items") or {}))
    bad = [f"bridge {k}→{v.get('registry')}" for k, v in (table.get("bridge") or {}).items()
           if (v or {}).get("registry") not in items]
    bad += [f"族 {f}" for f in (table.get("variants") or {}) if f not in items]
    return bad


def registry_positions(table):
    """表里所有**当注册名用**的位置（桥接 registry + 变体族名）。"""
    out = {v.get("registry") for v in (table.get("bridge") or {}).values()}
    out |= set((table.get("variants") or {}))
    return {x for x in out if x}


# --------------------------------------------------------------------------- #
# 一、快照事实层：这份 oracle 自己必须先站得住
# --------------------------------------------------------------------------- #

def part_snapshot():
    print("\n---- 一、快照事实层（离线，CI 必跑）----")
    extractor_src = EXTRACTOR.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_mk_snapshot", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for gen in ("1.7.10", "1.12.2"):
        fp = REG_DIR / f"items_{gen}.json"
        if not fp.exists():
            check(f"{gen}：注册表快照在库（{fp.name}）", False, "文件不存在")
            continue
        snap = load_snap(gen)
        items = snap.get("items") or {}
        check(f"{gen}：快照 Item 数 = {SNAP_ITEMS[gen]}", len(items) == SNAP_ITEMS[gen],
              f"实际 {len(items)}")
        check(f"{gen}：快照方块数 = {SNAP_BLOCKS[gen]}", (snap.get("counts") or {}).get("blocks") == SNAP_BLOCKS[gen],
              str((snap.get("counts") or {}).get("blocks")))
        check(f"{gen}：快照自带出处（jar + 指纹 + 命令 + 方法）",
              all((snap.get("source") or {}).get(k) for k in
                  ("jar", "jar_sha256", "extractor", "command", "method")),
              str(snap.get("source")))
        check(f"{gen}：默认堆叠上限 = 64", snap.get("default_max") == 64, str(snap.get("default_max")))
        check(f"{gen}：提取器钉住的 jar 指纹与快照一致",
              (mod.GENERATIONS.get(gen) or {}).get("jar_sha256") == (snap.get("source") or {}).get("jar_sha256"),
              f"提取器 {(mod.GENERATIONS.get(gen) or {}).get('jar_sha256')} / 快照 {(snap.get('source') or {}).get('jar_sha256')}")
        for name in MUST_EXIST[gen]:
            check(f"★{gen}：真注册表里有 {name}", name in items)
        for name in MUST_ABSENT[gen]:
            check(f"★{gen}：真注册表里没有 {name}（不是注册名）", name not in items)

    for name, want in MAX_PROBE["1.12.2"].items():
        row = (load_snap("1.12.2").get("items") or {}).get(name) or {}
        check(f"★1.12.2：{name} 的堆叠上限 = {want}", row.get("max") == want, str(row))

    # 世代差异必须能被这份 oracle 表达出来（否则「一代一表」就是摆设）
    s710, s122 = load_snap("1.7.10").get("items") or {}, load_snap("1.12.2").get("items") or {}
    check("★世代差异：water 在 1.7.10 是物品、在 1.12.2 不是",
          ("water" in s710) and ("water" not in s122))
    check("★世代差异：silver_glazed_terracotta 只在 1.12.2 有",
          ("silver_glazed_terracotta" in s122) and ("silver_glazed_terracotta" not in s710))
    check("提取器里两代的 jar 指纹都是钉死的固定值（换 jar 必须重新复核）",
          all((mod.GENERATIONS[g].get("jar_sha256") or "") for g in ("1.7.10", "1.12.2"))
          and "float" not in extractor_src)


# --------------------------------------------------------------------------- #
# 二、数据表成员资格层（离线，CI 必跑 —— 本用例的靶心）
# --------------------------------------------------------------------------- #

def part_tables():
    print("\n---- 二、数据表 vs 真注册表（点名目标缺失/不符必须 FAIL）----")
    for gen in ("1.7.10", "1.12.2"):
        table, snap = load_table(gen), load_snap(gen)
        bad = membership_violations(table, snap)
        check(f"★{gen}：桥接与变体族**逐条**都在该代真注册表里（{len(table.get('bridge') or {})} 桥接 / "
              f"{len(table.get('variants') or {})} 族）", not bad, str(bad[:8]))
        check(f"{gen}：bridge_count 字段与实际条数一致",
              table.get("bridge_count") == len(table.get("bridge") or {}),
              f"{table.get('bridge_count')} vs {len(table.get('bridge') or {})}")
        by_disp = disp_index(table)
        for disp, want in DISP_MUST[gen].items():
            got = by_disp.get(disp)
            check(f"★{gen}：展示名「{disp}」→ 注册名 {want}", got == want,
                  f"表里给的是 {got!r}；该代真注册表里 {want} {'有' if want in (snap.get('items') or {}) else '没有'}")
        dirty = [k for k, v in (table.get("bridge") or {}).items()
                 if "\r" in str((v or {}).get("display") or "")]
        check(f"★{gen}：展示名里没有 CRLF 残留（1.7.10 旧表整批带 \\r）", not dirty, str(dirty[:6]))
        pos = registry_positions(table)
        for bad_name in FALSIFIED_AS_REGISTRY:
            check(f"★{gen}：被证伪的注册名 {bad_name!r} 没被当注册名用", bad_name not in pos)

    # 跨代一致性（只作**辅助**判据：一致性证明不了正确性，两代同错照样一致）
    a, b = load_table("1.12.2"), load_table("1.7.10")
    da, db = disp_index(a), disp_index(b)
    conflicts = [(d, da[d], db[d]) for d in sorted(set(da) & set(db)) if da[d] != db[d]]
    check("两代表对同名物品给出的注册名一致（辅助判据，不作 oracle）", not conflicts, str(conflicts[:6]))
    check("跨代例外表已清空（charcoal 那条例外是替旧表打掩护的，不是真实代际差异）",
          all(da.get(d) == db.get(d) or d not in da or d not in db for d in ("Charcoal",)))

    # 夹具不是判据：把「两代同错也全绿」这件事钉成可执行的证据
    fx710 = FIXDIR / "registry_1.7.10.json"
    if fx710.exists():
        reg = (json.loads(fx710.read_text(encoding="utf-8")).get("registers") or {})
        s710 = load_snap("1.7.10").get("items") or {}
        check("★夹具（codegen 产物）会把 charcoal 当成 1.7.10 的合法键 —— 真注册表说没有 → "
              "夹具**不能**当成员判据",
              ("charcoal" in reg) and ("charcoal" not in s710))
    else:
        skip("离线夹具 registry_1.7.10.json 不在库，跳过「夹具不是判据」的对照")


# --------------------------------------------------------------------------- #
# 三、运行期层：词典装载快照 + 把发不出去的名字挡在门外
# --------------------------------------------------------------------------- #

def _fake_server(tmp: Path, gen: str) -> Path:
    """造一个**只有 .lang 的假 jar** —— 让预扁平化通道在离线 CI 上也能真跑起来。"""
    root = tmp / ("srv_" + gen)
    (root / "mods").mkdir(parents=True, exist_ok=True)
    jar = root / f"minecraft_server.{gen}.jar"
    lang = [
        "item.apple.name=Apple",
        "tile.stone.name=Stone",
        "item.coal.name=Coal",
        "item.diamond.name=Diamond",
        "tile.wood.oak.name=Oak Wood",
        "item.doorOak.name=Oak Door",
        "tile.fenceGate.name=Oak Fence Gate",
    ]
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("assets/minecraft/lang/en_us.lang", "\n".join(lang) + "\n")
    return root


def part_runtime():
    print("\n---- 三、运行期层：快照驱动（离线假 jar）----")
    import importlib
    ID_ = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary")
    LG = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.legacy_items")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for gen in ("1.12.2", "1.7.10"):
            root = _fake_server(tmp, gen)
            d = ID_.ItemDictionary(str(root), str(root / "items.json"))
            stats = d.build()
            if d.legacy_generation != gen:
                check(f"{gen}：假 jar 能触发预扁平化通道", False, str(stats))
                continue
            check(f"{gen}：词典装载了该代真注册表（Item {SNAP_ITEMS[gen]} 枚）",
                  len(d.legacy_item_registry) == SNAP_ITEMS[gen], str(len(d.legacy_item_registry)))
            check(f"{gen}：诊断里写明快照来源",
                  f"items_{gen}.json" in (d.legacy_registry_diag or ""), d.legacy_registry_diag)
            check(f"★{gen}：数据表已对齐 → 一条都不用剔（剔了就说明表又跑偏了）",
                  d.legacy_registry_dropped == [], str(d.legacy_registry_dropped[:8]))

            # 名字可发放性：该代真名能生成命令，假名必须 fail-closed
            ok_plan = LG.plan_give(d, "Knn", "minecraft:coal", 1)
            check(f"{gen}：真注册名 coal 能过（count=1）", ok_plan.ok, ok_plan.reason or "")
            for bad in [n for n in ("water", "charcoal", "frosted_ice") if n not in (load_snap(gen).get("items") or {})]:
                p = LG.plan_give(d, "Knn", f"minecraft:{bad}", 1)
                check(f"★{gen}：发不出去的 {bad} 被拒（fail-closed）", not p.ok, p.command or "")

            # ID 型输入必须强命中：不许把 minecraft:water 前缀匹配成 waterlily（要水给睡莲）
            if gen == "1.12.2":
                p = LG.plan_give(d, "Knn", "minecraft:water", 1)
                check("★1.12.2：minecraft:water 不被悄悄换成 waterlily（ID 型输入强命中）",
                      (not p.ok) and "waterlily" not in (p.command or ""),
                      p.command or p.reason or "")
            # 人话输入仍走模糊匹配（收严只针对 ID 型输入，别把日常用法打瘸）
            human = "diamond sword" if gen == "1.12.2" else "red wool"
            p = LG.plan_give(d, "Knn", human, 1)
            check(f"{gen}：人话输入「{human}」仍能解析", p.ok, p.reason or "")

            # 木炭：1.12.2 走 coal 变体 data1；1.7.10 同样是 coal 的 data1
            hits = d.search_items("Charcoal")
            hit = next((h for h in hits if h["id"] == "minecraft:coal"
                        and h.get("variant_damage") in (1, "1")), None)
            check(f"★{gen}：木炭经 coal 变体族解析（data 1），不再当独立注册名",
                  hit is not None, str(hits)[:200])

    # 越界条目会被剔掉并留痕（拿伪造条目喂过滤器 —— 「表写错了也得挡得住」）
    with tempfile.TemporaryDirectory() as td:
        root = _fake_server(Path(td), "1.12.2")
        d = ID_.ItemDictionary(str(root), str(root / "items.json"))
        d.build()
        d.legacy_bridge["bogusBridged"] = {"registry": "not_a_real_item", "display": "Bogus"}
        d.legacy_variants["bogus_family"] = {"domain_upper": 1, "variants": {"0": "Bogus"}}
        d._apply_legacy_registry_filter()
        check("★运行期：伪造的越界桥接被剔掉并留痕",
              d.legacy_registry_dropped == ["bogusBridged→not_a_real_item", "族:bogus_family"],
              str(d.legacy_registry_dropped))
        check("★运行期：剔掉后词典里不再有这两个名字",
              all("bogusBridged" not in k for k in d.legacy_bridge)
              and "bogus_family" not in d.legacy_variants)

    # 快照缺席 → 明确降级（写在诊断里），不假装通过
    with tempfile.TemporaryDirectory() as td:
        root = _fake_server(Path(td), "1.12.2")
        d = ID_.ItemDictionary(str(root), str(root / "items.json"))
        d.build()
        d._legacy_registry_dir = lambda: None          # 模拟快照目录整个消失
        d.legacy_registry_dropped = []
        d._apply_legacy_registry_filter()
        check("★运行期：快照缺席时不静默通过（诊断写明本次不做过滤）",
              (not d.legacy_item_registry) and ("不做" in (d.legacy_registry_diag or "")),
              d.legacy_registry_diag)


# --------------------------------------------------------------------------- #
# 四、判据自身的牙口：改坏输入必须 FAIL
# --------------------------------------------------------------------------- #

def part_teeth():
    print("\n---- 四、判据的牙口（改坏输入 → 必须报错）----")
    snap122 = load_snap("1.12.2")
    table122 = load_table("1.12.2")
    check("原始输入下判据干净（成员检查 = 0 违规）",
          membership_violations(table122, snap122) == [],
          str(membership_violations(table122, snap122)[:6]))

    # 1) 快照被改（把真名抹掉）→ 必须报出来
    import copy
    mutated = copy.deepcopy(snap122)
    mutated["items"].pop("fence_gate", None)
    v = membership_violations(table122, mutated)
    check("★牙口：快照里抹掉 fence_gate → 判据立刻报出越界",
          any("fence_gate" in x for x in v), str(v[:6]))

    # 2) 表被改（写回旧错名）→ 必须报出来
    t2 = copy.deepcopy(table122)
    t2["bridge"]["fenceGate"]["registry"] = "oak_fence_gate"
    v2 = membership_violations(t2, snap122)
    check("★牙口：表里写回 oak_fence_gate → 判据立刻报出越界",
          any("oak_fence_gate" in x for x in v2), str(v2[:6]))

    # 3) 两代同错：只比一致性的旧判据会放过，本判据（逐代真表）必须抓住
    t_fake = {"bridge": {"apple": {"registry": "definitely_not_an_item", "display": "Apple"}},
              "variants": {}}
    same122 = membership_violations(t_fake, snap122)
    same710 = membership_violations(t_fake, load_snap("1.7.10"))
    check("★牙口：两代写成同一个不存在的名字（旧判据全绿的那种）→ 逐代判据两代都报",
          bool(same122) and bool(same710), f"{same122} / {same710}")
    ra = disp_index(t_fake)
    check("★牙口：「两代一致」确实看不见这种错（证明辅助判据不能当 oracle）",
          ra.get("Apple") == "definitely_not_an_item")


# --------------------------------------------------------------------------- #
# 五、靶场层：拿真 jar 复核快照（没有 jar / java 就 SKIP，不假装跑过）
# --------------------------------------------------------------------------- #

def part_matrix():
    print("\n---- 五、靶场层：真 jar 复核快照（可选）----")
    matrix = os.environ.get("SCINTILLA_MATRIX_DIR") or os.environ.get("MC_MATRIX_DIR") or ""
    if not matrix:
        skip("未提供 SCINTILLA_MATRIX_DIR / MC_MATRIX_DIR → 靶场层跳过（离线判据已在前四部分跑完）")
        return
    if shutil.which("java") is None:
        skip("PATH 里没有 java（CFR 需要它）→ 靶场层跳过")
        return
    r = subprocess.run([sys.executable, str(EXTRACTOR), "--check"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "MC_MATRIX_DIR": matrix})
    check("★靶场层：快照与真 jar 重算逐条一致（--check）", r.returncode == 0,
          (r.stdout or "")[-400:] + (r.stderr or "")[-400:])
    print((r.stdout or "").strip())


def main() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · 旧版 Item 注册表成员资格（N04）")
    print("=" * 78)
    part_snapshot()
    part_tables()
    part_runtime()
    part_teeth()
    part_matrix()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if SKIP:
        print("跳过项：" + " / ".join(SKIP))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
