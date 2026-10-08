# -*- coding: utf-8 -*-
"""v0.24.2 回归：逐世代表审计（GPT 全面复核 F07）。

报告判据原文：
  · 每个 bridge registry 存在于对应服务端物品注册表；
  · 上述名称逐项核对输出；log 4/5 拒绝，log2 0/1 允许；
  · 离线注册表 fixture 应纳入 CI，不能依赖作者机器有 jar。

两层：
  A. 离线层（CI 必跑）：报告点名项逐条核对 + log 族域 + 「两代表不许互相打脸」
     （同一件物品在两代的注册名必须一致，例外只能写进 `_CROSS_GEN_EXCEPTIONS`）
     + 被证伪的写法不得回流 + 离线 fixture 在库。
  B. 靶场层（有 jar 才跑）：用插件自己的词典管线 + 真实 1.12.2 jar，按报告原句验证
     「Acacia Wood → log2 data0 / Dark Oak Wood → log2 data1 / log 4 拒绝 / log2 1 允许」。
     目录由 SCINTILLA_MATRIX_DIR 给出；缺席即 SKIP（不在 CI 上假装跑过）。

跑法：<python> tests\test_v0242_legacy_table_audit.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []
PASS_N: list[str] = []
add_sys_paths()

ROOT = Path(__file__).resolve().parents[1]
TDIR = ROOT / "data" / "legacy_items"
#: 离线审计夹具（v0.24.3 · N05）：从 ``data/legacy_items/`` 迁到 ``tests/fixtures/`` ——
#: ``tests/`` 整目录 export-ignore，夹具不再跟发布包走，也不会被生产选表当成世代表。
FIXDIR = ROOT / "tests" / "fixtures" / "legacy_registry"


#: 允许两代不一致的例外（键 = 展示名）—— 加一条必须写清证据，不许拿来消音。
#: Charcoal：1.7.10 里焦炭是 **coal 的数据值 1**（同一注册名 + 数据值），
#:           1.12.2 已经是独立注册名 charcoal —— 两代都对，属于真实代际差异。
_CROSS_GEN_EXCEPTIONS: dict = {
    "Charcoal": {"1.12.2": "charcoal", "1.7.10": "coal"},
}

_EXPECT = {
    "1.12.2": {
        "Dead Bush": "deadbush",
        "Oak Fence": "fence",
        "Jack o'Lantern": "lit_pumpkin",
        "Stone Bricks": "stonebrick",
        "Totem of Undying": "totem_of_undying",
        "Light Gray Shulker Box": "silver_shulker_box",
    },
    "1.7.10": {
        "Dead Bush": "deadbush",
        "Oak Fence": "fence",
        "Jack o'Lantern": "lit_pumpkin",
        "Stone Bricks": "stonebrick",
    },
}
_FALSIFIED = ("dead_bush", "oak_fence", "jack_o_lantern", "smooth_stone", "light_gray_shulker_box")


def check(desc, ok, detail=""):
    if ok:
        PASS_N.append(desc)
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc):
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


def load(gen):
    return json.loads((TDIR / f"{gen}.json").read_text(encoding="utf-8"))


def reg_by_disp(table):
    out = {}
    for v in (table.get("bridge") or {}).values():
        d = (v.get("display") or "").strip()
        if d:
            out.setdefault(d, v.get("registry"))
    for fam, info in (table.get("variants") or {}).items():
        for _, disp in (info.get("variants") or {}).items():
            if disp:
                out.setdefault(str(disp).strip(), fam)
    return out


def part_a():
    print("---- 一、离线层：表结构与报告点名项 ----")
    t122, t710 = load("1.12.2"), load("1.7.10")

    for gen, table in (("1.12.2", t122), ("1.7.10", t710)):
        by_disp = reg_by_disp(table)
        for disp, want in _EXPECT[gen].items():
            got = by_disp.get(disp)
            if got is None:
                skip(f"{gen}：表里没有「{disp}」（该代可能确实没有此物品）")
                continue
            check(f"★{gen}：「{disp}」的注册名 = {want}", got == want, f"表里写的是 {got}")

    for gen, table in (("1.12.2", t122), ("1.7.10", t710)):
        fam = (table.get("variants") or {})
        lg, l2 = fam.get("log") or {}, fam.get("log2") or {}
        check(f"★{gen}：log 域上限 = 3（金合欢/深色橡木不在这里）",
              lg.get("domain_upper") == 3, str(lg.get("domain_upper")))
        check(f"★{gen}：log 的变体里没有 4/5",
              not any(k in (lg.get("variants") or {}) for k in ("4", "5")),
              str(sorted((lg.get("variants") or {}))))
        check(f"★{gen}：log2 含 0/1（Acacia / Dark Oak）",
              all(k in (l2.get("variants") or {}) for k in ("0", "1")), str(l2.get("variants")))

    a, b = reg_by_disp(t122), reg_by_disp(t710)
    conflicts = [(d, a[d], b[d]) for d in sorted(set(a) & set(b))
                 if a[d] != b[d] and d not in _CROSS_GEN_EXCEPTIONS]
    check("★两代表对同一件物品给出的注册名一致（不一致 = 至少一边错）",
          not conflicts, str(conflicts[:8]))

    for gen, table in (("1.12.2", t122), ("1.7.10", t710)):
        txt = json.dumps(table, ensure_ascii=False)
        for bad in _FALSIFIED:
            check(f"★{gen}：表里不再出现被证伪的 {bad!r}", f'"{bad}"' not in txt)

    for gen in ("1.12.2", "1.7.10"):
        fp = FIXDIR / f"registry_{gen}.json"

        cnt = 0
        if fp.exists():
            cnt = len(json.loads(fp.read_text(encoding="utf-8")).get("registers") or {})
        check(f"{gen}：离线 fixture 在库（{cnt} 个键）", cnt > 100)


def part_b():
    print("\n---- 二、靶场层：真实 jar 下的逐项核对（报告原句）----")
    matrix = os.environ.get("SCINTILLA_MATRIX_DIR", "")
    jar = None
    if matrix:
        cand = Path(matrix) / "1.12.2-forge"
        if cand.is_dir():
            found = sorted(cand.glob("minecraft_server*.jar")) or sorted(cand.glob("server.jar"))
            jar = found[0] if found else None
    if not jar or not jar.exists():
        skip("未提供 SCINTILLA_MATRIX_DIR（或没有 1.12.2 真实 jar）→ 靶场层跳过")
        return
    import importlib
    import shutil
    import tempfile
    ID_ = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary")
    LG = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.legacy_items")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "eula.txt").write_text("eula=true", encoding="utf-8")
        (root / "server.properties").write_text("x", encoding="utf-8")
        shutil.copy2(jar, root / "server.jar")      # 抹掉版本号 —— 这正是 N07 的触发条件

        # ---- N07 靶心：版本提示缺席时**不许猜表** ----
        # 旧实现在这里会挑中 data/legacy_items/ 里的 registry 夹具（0 桥接 + 0 变体），
        # 却照样写 legacy_generation、诊断还报「通道命中」—— 这就是「0 条假成功」。
        bare = ID_.ItemDictionary(str(root), str(root / "items.json"))
        bare.build()
        check("★N07：改名 jar + 无版本提示 → 诚实拒绝（不猜表、不写世代号）",
              not bare.legacy_generation, f"generation={bare.legacy_generation!r}")
        check("★N07：拒绝时给可读诊断，而不是一句「0 条」",
              bool(bare.legacy_diag), repr(bare.legacy_diag))

        # ---- 版本提示在场（产品主路径：main.py 把版本探测结果写进 mc_hint）→ 正常选表 ----
        d = ID_.ItemDictionary(str(root), str(root / "items.json"))
        d.mc_hint = (1, 12, 2)
        st = d.build()
        check("真实 1.12.2 jar（含版本提示）→ 词典落到 1.12.2 世代",
              d.legacy_generation == "1.12.2", str(st))
        for name, want_id, want_dmg in (("Acacia Wood", "minecraft:log2", 0),
                                        ("Dark Oak Wood", "minecraft:log2", 1)):
            hits = d.search_items(name)
            hit = next((h for h in hits if h["id"] == want_id and h.get("variant_damage") == want_dmg), None)
            check(f"★真实词典：{name} → {want_id} data{want_dmg}", hit is not None, str(hits)[:180])
        p = LG.plan_give(d, "Knn", "minecraft:log", 1, damage=4)
        check("★真实词典：log 4 → 拒绝（域上限 3）", not p.ok, p.command or p.reason)
        p = LG.plan_give(d, "Knn", "minecraft:log2", 1, damage=1)
        check("★真实词典：log2 1 → 允许", p.ok and p.command == "give Knn minecraft:log2 1 1",
              p.command or p.reason)


def main() -> int:
    print("=" * 78)
    print("v0.24.2 回归 · 逐世代表审计（F07）")
    print("=" * 78)
    part_a()
    part_b()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
