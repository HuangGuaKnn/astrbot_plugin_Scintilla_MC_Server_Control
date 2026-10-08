# -*- coding: utf-8 -*-
"""v0.24.3 回归 · N05 + N07：资产类型边界（运行表 vs 测试夹具）。

GPT v0.24.2 复核原句
====================

* **N05**：``run_release_verify.py`` 把 ``data/legacy_items/`` 下全部 JSON 当变体表，
  新增的两张 registry 夹具没有 ``variants`` → 原始 git archive 直接报「0 族」，
  总门禁 FAIL；而同一段代码随后又打印通用 PASS，日志自相矛盾。
* **N07**：``core/item_dictionary.py`` 用 ``sorted(d.glob("*.json"))`` 收候选，
  未知版本时推导式还引用了上一轮循环残留的 ``p`` → 两代完整 jar 改名成 ``server.jar``
  后**都选中 registry 夹具**，items/bridge/variants 全 0，诊断却写「通道命中」。

本用例守四件事
==============

1. **发布目录只放运行表**：``data/legacy_items/*.json`` 逐份过 ``_is_generation_table``；
   离线审计夹具必须住在 ``tests/fixtures/``（``tests/`` 整目录 export-ignore，不进包）。
2. **选表不认非运行表**：候选集里混着夹具时，正确世代仍必须选到真表。
3. **版本未知不猜**（fail-closed）：无 ``mc`` / ``mc_hint`` → 落空，不许随手挑一张表。
4. **空表不算成功**：桥接与变体族都为空的「表」不许写世代号冒充成功。

fail-closed 声明：缺运行依赖（import 失败）判 **FAIL**，不判 SKIP。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

FAIL: list[str] = []

add_sys_paths()
try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary import ItemDictionary
    IMPORT_OK = True
    IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    IMPORT_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"

ROOT = PLUGIN_DIR
TDIR = ROOT / "data" / "legacy_items"          # 随包发布的**运行表**
FIXDIR = ROOT / "tests" / "fixtures" / "legacy_registry"   # 不进包的**离线夹具**


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc: str) -> None:
    print(f"[SKIP] {desc}")


def new_dict(cache_path: Path | None = None) -> ItemDictionary:
    """只要判表能力、不建词典：拿裸对象足够（判据全是类/静态方法）。"""
    d = ItemDictionary.__new__(ItemDictionary)
    d.cache_path = str(cache_path) if cache_path else None
    return d


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ===================== 一、发布目录只放运行表 =====================

def group_dir() -> None:
    print("---- 一、发布目录 / 夹具目录的资产类型 ----")
    d = new_dict()
    tables = sorted(TDIR.glob("*.json"))
    check("data/legacy_items/ 至少两张运行表", len(tables) >= 2, str([p.name for p in tables]))
    for p in tables:
        check(f"★{p.name} 是合格世代运行表", d._is_generation_table(read_json(p)))
    check("★目录内容判据：data/legacy_items/ 算「表在库」", d._dir_has_generation_table(TDIR) is True)

    for gen in ("1.7.10", "1.12.2"):
        fx = FIXDIR / f"registry_{gen}.json"
        check(f"离线夹具已迁到 tests/fixtures/：{fx.name}", fx.exists(), str(fx))
        if fx.exists():
            obj = read_json(fx)
            check(f"★夹具 {fx.name} **不是**运行表（不许被选表逻辑认领）",
                  d._is_generation_table(obj) is False, str(list(obj)[:6]))
            check(f"夹具内容仍是注册表事实（registers ≥100 键）",
                  len(obj.get("registers") or {}) > 100,
                  str(len(obj.get("registers") or {})))
    check("★发布目录里已无 registry 夹具", not list(TDIR.glob("registry_*.json")))

    # 只堆夹具的目录不算「表在库」
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shutil.copy2(FIXDIR / "registry_1.7.10.json", tmp / "registry_1.7.10.json")
        (tmp / "junk.json").write_text("{not json", encoding="utf-8")
        check("★只有夹具 / 坏 JSON 的目录不算「表在库」",
              d._dir_has_generation_table(tmp) is False)
        shutil.copy2(TDIR / "1.12.2.json", tmp / "1.12.2.json")
        check("补一张真表后算「表在库」", d._dir_has_generation_table(tmp) is True)


# ===================== 二、选表：不认夹具、版本未知不猜 =====================

def group_pick() -> None:
    print("\n---- 二、选表边界（N07 靶心）----")
    d = new_dict()
    real = sorted(TDIR.glob("*.json"))
    fixture = sorted(FIXDIR.glob("*.json"))
    mixed = real + fixture                    # 刻意混着放 —— 旧实现在未知版本下会挑中夹具

    # 每张真表都必须能被它自己的版本选中（自洽检查：range 覆盖自己写的 mc）
    for p in real:
        want = read_json(p).get("mc")
        picked = d._pick_legacy_table(want, mixed, require_match=True)
        check(f"★版本 {want} 选中真表 {p.name}（不是夹具）",
              picked is not None and Path(picked).name == p.name,
              f"选到了 {Path(picked).name if picked else None}")

    check("★1.7.10 选到 1.7.10.json",
          Path(d._pick_legacy_table((1, 7, 10), mixed, require_match=True)).name == "1.7.10.json")
    check("★1.12.2 选到 1.12.2.json",
          Path(d._pick_legacy_table((1, 12, 2), mixed, require_match=True)).name == "1.12.2.json")
    check("★版本未知（mc=None）→ 落空，**不猜**（旧实现在这里选到夹具）",
          d._pick_legacy_table(None, mixed, require_match=True) is None,
          str(d._pick_legacy_table(None, mixed, require_match=True)))
    check("★版本未知但只有夹具 → 同样落空",
          d._pick_legacy_table(None, fixture, require_match=True) is None)
    check("★现代版本（1.21.1）不在任何 running 表 range 内 → 落空（不拿旧表套现代服）",
          d._pick_legacy_table((1, 21, 1), mixed, require_match=True) is None)
    check("★候选集全是夹具 → 落空",
          d._pick_legacy_table((1, 12, 2), fixture, require_match=True) is None)
    check("坏 JSON 不进候选（不抛异常，只跳过）",
          d._pick_legacy_table((1, 12, 2), mixed + [Path(__file__)], require_match=True) is not None)
    check("字符串版版本号也能选表（配置里读到的是字符串）",
          Path(d._pick_legacy_table("1.12.2", mixed, require_match=True)).name == "1.12.2.json")

    # 反例固化：这个形状在旧实现下会返回夹具路径（推导式引用上一轮循环残留的 p）
    old_bug = mixed[:1] + fixture             # 第一个文件是 1.12.2.json，但夹具在队尾
    picked = d._pick_legacy_table((1, 12, 2), old_bug, require_match=True)
    check("★反例固化：夹具排在候选队尾也不会被选中",
          picked is not None and Path(picked).name == "1.12.2.json",
          f"选到了 {Path(picked).name if picked else None}")


# ===================== 三、空表不许当成功 =====================

def group_empty() -> None:
    print("\n---- 三、空桥接 / 空变体族不许冒充成功 ----")
    src = (ROOT / "core" / "item_dictionary.py").read_text(encoding="utf-8")
    check("★_parse_legacy_vanilla 里有「空表拒绝」判据",
          "if not self.legacy_bridge and not self.legacy_variants:" in src)
    check("★未知版本走「拒绝猜表」分支（diag 说明原因）",
          "拒绝猜表、不建词典" in src)
    check("★选表前先过运行表判据",
          "if not self._is_generation_table(data):" in src)
    check("★目录判据按内容而非「有没有 *.json」",
          "self._dir_has_generation_table(c)" in src and "any(c.glob(\"*.json\"))" not in src)
    check("残留 p 写法已清零（推导式只解包它自己用到的变量）",
          "for _, _, v in parsed" not in src)


# ===================== 四、发布包卫生口径 =====================

def group_release() -> None:
    print("\n---- 四、发布包卫生：红就是红，别又打 PASS ----")
    src = (ROOT / "run_release_verify.py").read_text(encoding="utf-8")
    check("★PASS 打印已纳入 data_bad（旧版这里会自相矛盾）",
          "if not leaked and not missing and not data_bad:" in src)
    check("★该目录的 JSON 要做「是不是运行表」的形状检查",
          "不像世代运行表" in src)
    check("运行表必须随包（must_keep 仍在）",
          '"data/legacy_items/1.12.2.json"' in src and '"data/legacy_items/1.7.10.json"' in src)
    check("tests/ 仍属「泄漏」判据（夹具因此天然不进包）",
          'n.startswith("tests/")' in src)


# ===================== 五、靶场层（有真实 jar 才跑） =====================

def group_matrix() -> None:
    print("\n---- 五、靶场层：真 jar 改名成 server.jar ----")
    matrix = os.environ.get("SCINTILLA_MATRIX_DIR", "")
    if not matrix:
        skip("未提供 SCINTILLA_MATRIX_DIR → 真 jar 层跳过（不假装跑过）")
        return
    for gen, sub in (("1.7.10", "1.7.10-forge"), ("1.12.2", "1.12.2-forge")):
        cand = Path(matrix) / sub
        if not cand.is_dir():
            skip(f"{sub} 不在矩阵目录里")
            continue
        found = sorted(cand.glob("minecraft_server*.jar")) or sorted(cand.glob("server.jar"))
        if not found:
            skip(f"{sub} 没有服务端 jar")
            continue
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "eula.txt").write_text("eula=true", encoding="utf-8")
            (root / "server.properties").write_text("x", encoding="utf-8")
            shutil.copy2(found[0], root / "server.jar")      # 抹掉版本号 —— N07 的触发条件

            # ① 无版本提示 → 诚实拒绝（旧实现在这里挑中 registry 夹具，造出 0 桥接假成功）
            bare = ItemDictionary(str(root), str(root / "items.json"))
            bare_st = bare.build()
            check(f"★{gen}：改名 jar + 无版本提示 → 诚实拒绝（不写世代号）",
                  not bare.legacy_generation,
                  f"generation={bare.legacy_generation!r} items={bare_st.get('items')}")
            check(f"★{gen}：拒绝时给可读诊断", bool(bare.legacy_diag), repr(bare.legacy_diag))

            # ② 有版本提示 → 必须落到**对应世代**的真表
            inst = ItemDictionary(str(root), str(root / "items.json"))
            inst.mc_hint = tuple(int(x) for x in gen.split("."))
            inst.build()
            check(f"★{gen}：有版本提示 → 落到 {gen} 世代",
                  inst.legacy_generation == gen,
                  f"generation={inst.legacy_generation!r} diag={inst.legacy_diag!r}")
            check(f"★{gen}：选到真表时不是 0 桥接",
                  bool(inst.legacy_bridge) or bool(inst.legacy_variants),
                  f"bridge={len(inst.legacy_bridge)} variants={len(inst.legacy_variants)}")


def main() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · N05 + N07 资产类型边界")
    print("=" * 78)
    if not IMPORT_OK:
        check("插件可导入（缺运行依赖必须判 FAIL 而不是 SKIP）", False, IMPORT_ERR)
        print("\n失败项：" + " / ".join(FAIL))
        return 1
    group_dir()
    group_pick()
    group_empty()
    group_release()
    group_matrix()
    print("\n================ 汇总 ================")
    print("失败 %d 项" % len(FAIL))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
