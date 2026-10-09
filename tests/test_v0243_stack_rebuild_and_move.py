"""v0.24.3 回归：GPT v0.24.2 复核 N06 / N08 / N09 三枚真雷。

N06｜**重扫丢词典** —— `ItemDictionary.build()` 只清 `mods/items/recipes`，
     `_vanilla_src_loaded` 与世代状态全都留着：
       · `mc_rescan_dictionary`（main.py）就是在**同一个** `self._dictionary` 上
         二次 build → 第二次 `_parse_vanilla_lang()` 直接 return，
         原版 `minecraft:` 语言表整体缺席，日志还报「已读过（不重复）」；
       · 构建中途抛错时，本对象只剩半张词典，`_save_cache()` / 检索随时读到它。
     契约：同一对象反复 build 结果**稳定**；构建失败 = 保持上一次的完整结果；
     `items` 容器身份不变（旧引用不会抱着一本旧账）。

N08｜**堆叠上限靠名字猜** —— 1.12.2 give 的单条上界是物品自己的 `getMaxStackSize`，
     旧实现只有一个「工具/护甲 = 1」的正则：
       · 上限 1 却名字不带特征的（蛋糕/床/附魔书/药水/牛奶桶/矿车/鞘翅/图腾…）
         在 count=2 时被放行；
       · 上限 16 的（空桶/成书/盔甲架/雪球/鸡蛋/末影珍珠/告示牌/旗帜）
         在 count=17 时被放行。
     契约：三类各钉死若干代表；1.7.10 仍是「与物品无关的固定 1..64」
     （给钻石剑 2 件放行 = 该代正确行为，不许顺手收紧）；模组物品走成文兜底
     且**把假设说出口**（计划备注里带「未验证」）。

N09｜**移动时源预设的成功没记账** —— 源文件写成功那条路一声不吭，
     于是源预设早先任何一次失败留下的红会永远挂着。契约：写成功 → 该源键转绿。

运行：
  python tests\\test_v0243_stack_rebuild_and_move.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

import importlib  # noqa: E402

PKG = "astrbot_plugin_Scintilla_MC_Server_Control.core"
ID = importlib.import_module(PKG + ".item_dictionary")
LG = importlib.import_module(PKG + ".legacy_items")
LST = importlib.import_module(PKG + ".legacy_stack_table")
KB = importlib.import_module(PKG + ".knowledge_base")

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}" + (f"｜{detail}" if detail else ""))


class Stub:
    """词典替身：只喂 plan_give 需要的字段。"""

    def __init__(self, gen: str, names):
        self.legacy_generation = gen
        self.legacy_bridge = {}
        self.items = {n: {"en": n, "zh": "", "type": "item"} for n in names}


def make_server_dir(base: Path) -> Path:
    """造一个最小「服务端目录」：mods/ 空目录 + 一个带原版语言表的 server.jar。"""
    d = base / "srv"
    (d / "mods").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / "server.jar", "w") as zf:
        # 1.13+ 原版语言表的键格式是 ``item.minecraft.x`` / ``block.minecraft.y``
        zf.writestr("assets/minecraft/lang/en_us.json",
                    json.dumps({"item.minecraft.marker": "Marker",
                                "block.minecraft.wool": "Wool"}))
    return d


# ===================== N06 =====================
def n06_cases(base: Path) -> None:
    print("\n---- N06：同一对象反复 build 不许丢原版表 / 失败不许留半成品 ----")
    srv = make_server_dir(base)
    d = ID.ItemDictionary(str(srv), None)
    s1 = d.build()
    ref = d.items                                   # 记下容器身份
    check("首次构建读到原版语言表", any(k.startswith("minecraft:") for k in d.items),
          str(s1))
    check("首次 legacy_diag 说明来源", "语言表命中" in str(s1.get("legacy_diag")),
          str(s1.get("legacy_diag")))

    s2 = d.build()
    check("★二次构建**仍然**有原版物品（旧实现在这里整批丢）",
          any(k.startswith("minecraft:") for k in d.items), str(s2))
    check("★二次构建的 diag 不再报「已读过（不重复）」",
          "不重复" not in str(s2.get("legacy_diag")), str(s2.get("legacy_diag")))
    check("★两次构建的物品数一致（结果稳定）", s1["items"] == s2["items"],
          f"{s1['items']} vs {s2['items']}")
    check("★items 容器身份不变（就地换入，旧引用看到新账）",
          d.items is ref and any(k.startswith("minecraft:") for k in ref))

    # 构建失败 → 上一次的完整结果必须还在
    before = dict(d.items)
    real = ID.ItemDictionary._parse_vanilla_lang
    ID.ItemDictionary._parse_vanilla_lang = lambda self: (_ for _ in ()).throw(
        RuntimeError("伪构建失败"))
    try:
        try:
            d.build()
            raised = False
        except RuntimeError:
            raised = True
    finally:
        ID.ItemDictionary._parse_vanilla_lang = real
    check("构建中途抛错会如实抛出（不吞）", raised)
    check("★构建失败后本对象仍持有上一次的完整词典", d.items == before,
          f"{len(d.items)} vs {len(before)}")
    s3 = d.build()
    check("恢复后还能正常构建", s3["items"] == before.__len__(), str(s3))
    check("源码里有 _build_once / _adopt 这对结构",
          hasattr(ID.ItemDictionary, "_build_once") and hasattr(ID.ItemDictionary, "_adopt"))


# ===================== N08 =====================
def n08_cases() -> None:
    print("\n---- N08：单条数量上界 = 该代解析范围 ∩ 物品真实堆叠 ----")
    one = ["minecraft:cake", "minecraft:bed", "minecraft:enchanted_book",
           "minecraft:writable_book", "minecraft:elytra", "minecraft:totem_of_undying",
           "minecraft:water_bucket", "minecraft:milk_bucket", "minecraft:potion",
           "minecraft:splash_potion", "minecraft:minecart", "minecraft:chest_minecart",
           "minecraft:boat", "minecraft:acacia_boat", "minecraft:iron_horse_armor",
           "minecraft:white_shulker_box", "minecraft:record_13", "minecraft:saddle",
           "minecraft:diamond_sword", "minecraft:shield",
           # N08 修正项：
           "minecraft:carrot_on_a_stick", "minecraft:knowledge_book"]
    six = ["minecraft:bucket", "minecraft:written_book", "minecraft:armor_stand",
           "minecraft:snowball", "minecraft:egg", "minecraft:ender_pearl",
           "minecraft:sign", "minecraft:banner"]
    s12 = Stub("1.12.2", one + six + ["minecraft:diamond", "minecraft:map", "minecraft:filled_map", "thermalfoundation:ingot", "thermalfoundation:diamond_sword"])
    for n in one:
        check(f"1.12.2 上限 1：{n.split(':')[1]} 给 2 件被拒",
              not LG.plan_give(s12, "Steve", n, count=2).ok)
        check(f"1.12.2 上限 1：{n.split(':')[1]} 给 1 件放行",
              LG.plan_give(s12, "Steve", n, count=1).ok)
    for n in six:
        p_ok = LG.plan_give(s12, "Steve", n, count=16)
        p_bad = LG.plan_give(s12, "Steve", n, count=17)
        check(f"1.12.2 上限 16：{n.split(':')[1]} 给 16 放行 / 17 被拒",
              p_ok.ok and (not p_bad.ok), p_bad.reason)
        check(f"1.12.2 上限 16：{n.split(':')[1]} 的拒绝文案写明 16",
              "16" in p_bad.reason, p_bad.reason)
    p64 = LG.plan_give(s12, "Steve", "minecraft:diamond", count=64)
    p65 = LG.plan_give(s12, "Steve", "minecraft:diamond", count=65)
    check("1.12.2 堆叠物：64 放行 / 65 被拒（并提示拆单）",
          p64.ok and (not p65.ok) and "拆" in p65.reason, p65.reason)
    check("★原版 64 物品**不带**兜底备注（那是确证）",
          not any("未验证" in x for x in p64.notes), str(p64.notes))

    # N08 修正项：空地图与成图在 1.12.2 继承默认 64 堆叠
    pmap64 = LG.plan_give(s12, "Steve", "minecraft:map", count=64)
    pmap65 = LG.plan_give(s12, "Steve", "minecraft:map", count=65)
    check("★1.12.2 map：64 放行 / 65 被拒", pmap64.ok and (not pmap65.ok))
    pfill64 = LG.plan_give(s12, "Steve", "minecraft:filled_map", count=64)
    pfill65 = LG.plan_give(s12, "Steve", "minecraft:filled_map", count=65)
    check("★1.12.2 filled_map：64 放行 / 65 被拒", pfill64.ok and (not pfill65.ok))

    m64 = LG.plan_give(s12, "Steve", "thermalfoundation:ingot", count=64)
    m65 = LG.plan_give(s12, "Steve", "thermalfoundation:ingot", count=65)
    check("模组物品：按原版默认 64 兜（64 放行 / 65 被拒）",
          m64.ok and (not m65.ok), m65.reason)
    check("★模组物品的兜底假设**说出口**（计划备注带「未验证」）",
          any("未验证" in x for x in m64.notes), str(m64.notes))

    s710 = Stub("1.7.10", ["minecraft:diamond", "minecraft:diamond_sword"])
    check("★1.7.10：钻石剑给 2 件**仍放行**（该代解析上界与物品无关，是正确的）",
          LG.plan_give(s710, "Steve", "minecraft:diamond_sword", count=2).ok)

    # N15：bad max 校验与模组同名物品防串借
    s12_bad = Stub("1.12.2", ["minecraft:diamond"])
    s12_bad.legacy_item_registry = {"diamond": {"id": 264, "max": 999}}
    check("★N15：registry.max 越界（999）被拒绝借用，回退至原版确证 64（64放行/65被拒）",
          LG.plan_give(s12_bad, "Steve", "minecraft:diamond", count=64).ok
          and not LG.plan_give(s12_bad, "Steve", "minecraft:diamond", count=65).ok)

    # N15 深度校验：严格类型判断 type is int，拒绝浮点、数值字符串、无穷大溢出
    for bad_max, label in [
        (1.9, "浮点数 1.9"),
        (16.9, "浮点数 16.9"),
        ("1", "字符串 '1'"),
        ("64", "字符串 '64'"),
        (float("inf"), "正无穷 inf"),
        (float("-inf"), "负无穷 -inf"),
        (float("nan"), "非数字 nan"),
        (True, "布尔 True"),
        (False, "布尔 False"),
    ]:
        s12_bad.legacy_item_registry = {"diamond": {"id": 264, "max": bad_max}}
        p_bad = LG.plan_give(s12_bad, "Steve", "minecraft:diamond", count=64)
        check(f"★N15：registry.max 异常类型 [{label}] 严格回退且不崩溃",
              p_bad.ok and not LG.plan_give(s12_bad, "Steve", "minecraft:diamond", count=65).ok)

    s12_mod_cake = Stub("1.12.2", ["thermalfoundation:cake", "thermalfoundation:bucket"])
    s12_mod_cake.legacy_item_registry = {
        "cake": {"id": 354, "max": 1},
        "bucket": {"id": 325, "max": 16},
    }
    p_mcake2 = LG.plan_give(s12_mod_cake, "Steve", "thermalfoundation:cake", count=2)
    check("★N15：模组同名 cake 不借用原版注册表或具名表的 max=1 限制（count=2 放行）",
          p_mcake2.ok, p_mcake2.reason)
    check("★N15：模组同名 cake 包含未验证提示",
          any("未验证" in x for x in p_mcake2.notes), str(p_mcake2.notes))
    p_mbucket17 = LG.plan_give(s12_mod_cake, "Steve", "thermalfoundation:bucket", count=17)
    check("★N15：模组同名 bucket 不借用原版 16 限制（count=17 放行）",
          p_mbucket17.ok, p_mbucket17.reason)

    # 模组堆叠规则规格对齐：具名原版表不继承、武器/工具家族规则按既定语义继承
    p_msword = LG.plan_give(s12, "Steve", "thermalfoundation:diamond_sword", count=2)
    check("★规则对齐：模组武器（_sword）继承武器通用家族限制（count=2 被拒）",
          not p_msword.ok and "单条数量上限 1" in p_msword.reason)
    check("★规则对齐：模组武器单条给 1 放行",
          LG.plan_give(s12, "Steve", "thermalfoundation:diamond_sword", count=1).ok)

    # N16：显式命名空间输入防串命（ID 与人话分支）
    s12_ns = Stub("1.12.2", ["moda:stone"])
    check("★N16：显式 a:stone 绝不借子串匹配命中 moda:stone",
          LG.resolve_entry(s12_ns, "a:stone")[0] is None)
    check("★N16：显式 moda:stone 精确命中自身",
          LG.resolve_entry(s12_ns, "moda:stone")[0] == "moda:stone")

    # N16 深度校验：真实词典下带冒号人话输入（含空格）绝不模糊命中
    real_dict = ID.ItemDictionary("")
    real_dict.items = {"moda:stone": {"en": "a:stone nice", "zh": "", "type": "item"}}
    check("★N16：带冒号人话输入 'a:stone nice' 绝不跨 namespace 命中 moda:stone",
          LG.resolve_entry(real_dict, "a:stone nice")[0] is None)

    # N14 深度校验：真实 ItemDictionary 嵌套快照与搜索结果深拷贝隔离
    d_nest = ID.ItemDictionary("")
    d_nest.items = {"minecraft:wool": {"en": "Wool", "zh": "", "type": "item", "variants": {"0": "White"}}}
    snap = d_nest.get_items_snapshot()
    snap["minecraft:wool"]["variants"]["0"] = "MUTATED_SNAP"
    snap["minecraft:wool"]["variants"]["99"] = "INJECTED"
    check("★N14：get_items_snapshot 深拷贝隔离（修改快照嵌套 variants 不污染原词典）",
          d_nest.items["minecraft:wool"]["variants"] == {"0": "White"})

    s_rows = d_nest.search_items("wool")
    s_rows[0]["variants"]["0"] = "MUTATED_SEARCH"
    s_rows[0]["variants"]["99"] = "INJECTED"
    check("★N14：search_items 深拷贝隔离（修改搜索返回行嵌套 variants 不污染原词典）",
          d_nest.items["minecraft:wool"]["variants"] == {"0": "White"})

    # N14 深度校验：generation bundle 单代自洽
    d_gen = ID.ItemDictionary("")
    d_gen.legacy_generation = "1.12.2"
    d_gen.items = {"minecraft:aa": {"en": "AA Item", "zh": "", "type": "item"}}
    d_gen.legacy_item_registry = {"aa": {"id": 1, "max": 1}}
    b_snap = d_gen.get_generation_bundle()
    # 模拟外界在请求中途发生 _adopt 切换至新代（max=64）
    d_gen.legacy_item_registry = {"aa": {"id": 1, "max": 64}}
    p_bundle = LG.plan_give(b_snap, "Steve", "minecraft:aa", count=2)
    check("★N14：单代 generation bundle 自洽保护（不受外界中途切换代次/registry 影响）",
          not p_bundle.ok and "单条数量上限 1" in p_bundle.reason)

    # N14 延伸与工程改进（GPT v0.24.3 建议收敛）：
    # 1. _get_lock 并发初始化与 __setstate__ 补锁无竞态
    import threading
    bare_dict = object.__new__(ID.ItemDictionary)
    locks_collected = []
    def _fetch_lock():
        locks_collected.append(bare_dict._get_lock())
    threads = [threading.Thread(target=_fetch_lock) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    check("★技术债1：裸对象并发 _get_lock() 线程安全无竞态（20线程取得同个锁）",
          len(set(id(l) for l in locks_collected)) == 1 and hasattr(bare_dict, "_lock"))
    bare_state = object.__new__(ID.ItemDictionary)
    bare_state.__setstate__({"items": {}, "mods": []})
    check("★技术债1：__setstate__ 反序列化入口自动补齐 _lock",
          hasattr(bare_state, "_lock") and hasattr(bare_state._lock, "__enter__"))

    # 2. get_mods_snapshot 深拷贝隔离
    d_mods_test = ID.ItemDictionary("")
    d_mods_test.mods = [{"id": "mod_test", "name": "Test Mod"}]
    m_snap = d_mods_test.get_mods_snapshot()
    m_snap[0]["name"] = "MUTATED"
    m_snap.append({"id": "injected", "name": "Injected"})
    check("★技术债2：get_mods_snapshot 深拷贝隔离（修改返回列表不污染原词典 mods）",
          d_mods_test.mods == [{"id": "mod_test", "name": "Test Mod"}])

    # 3. get_generation_bundle 按代次缓存与换代失效
    d_cache_test = ID.ItemDictionary("")
    d_cache_test.legacy_generation = "1.12.2"
    d_cache_test.items = {f"mod:item_{i}": {"en": f"Item {i}", "zh": "", "type": "item"} for i in range(1000)}
    import time
    t0 = time.perf_counter()
    b_first = d_cache_test.get_generation_bundle()
    t_first = time.perf_counter() - t0
    t0 = time.perf_counter()
    b_second = d_cache_test.get_generation_bundle()
    t_second = time.perf_counter() - t0
    check("★技术债3：同一代次内 get_generation_bundle 命中缓存（开销极低）",
          b_second["generation_id"] == b_first["generation_id"] and len(b_second["items"]) == 1000 and t_second < 0.005)
    # 模拟 _adopt 换代
    d_fresh = ID.ItemDictionary("")
    d_fresh.items = {"mod:item_fresh": {"en": "Fresh", "zh": "", "type": "item"}}
    d_cache_test._adopt(d_fresh)
    b_third = d_cache_test.get_generation_bundle()
    check("★技术债3：_adopt 换代后代次自增且 bundle 缓存自动换新",
          b_third["generation_id"] == b_first["generation_id"] + 1 and "mod:item_fresh" in b_third["items"])
    check("1.7.10：64 放行 / 65 被拒",
          LG.plan_give(s710, "Steve", "minecraft:diamond", count=64).ok
          and not LG.plan_give(s710, "Steve", "minecraft:diamond", count=65).ok)

    check("总量上限（+每条上限）是两套口径：MAX_COUNT 仍在且 ≥ 每条上界",
          LG.MAX_COUNT >= 6400 and LG.MAX_COUNT_BY_GEN["1.12.2"] == 64)
    check("表里 1.7.10 → 固定 64 且标注为确证",
          LST.max_stack_size("1.7.10", "minecraft:diamond_sword") == (64, True))
    check("表里未知世代 → 默认 64 但标注未确证",
          LST.max_stack_size("1.11.2", "minecraft:diamond") == (64, False))
    check("真机复核清单覆盖 堆叠物 / 16 / 1 三类",
          {p[0].split(":")[1] for p in LST.REALCHECK_PROBES}
          >= {"diamond", "snowball", "cake"})


# ===================== N09 =====================
def n09_cases(base: Path) -> None:
    print("\n---- N09：移动时源预设写成功必须清红 ----")
    mgr = KB.KnowledgePresetManager(str(base / "presets"), "srv-n09")
    for nm in ("源A", "目标A", "源B", "目标B"):
        mgr.create(nm)
    ids = {p["name"]: p["id"] for p in mgr.list_presets()}
    src_a, dst_a = ids["源A"], ids["目标A"]

    src_p = mgr.preset_path(src_a)
    src_p.write_text(json.dumps({"preset_id": src_a, "fingerprint": "",
                                 "entries": {"t1": {"c": "内容"}}, "deleted": []},
                                ensure_ascii=False), encoding="utf-8")
    part_src = mgr._preset_part(src_a)
    mgr._note_save(part_src, False, "上次写失败留下的红")
    check("（前置）源键现在是红的", mgr.save_health()["parts"][part_src]["ok"] is False)

    out = mgr.transfer(src_a, dst_a, mode="move")
    check("移动回包 ok=True", out.get("ok") is True, str(out))
    check("★移完源键转绿（旧实现永远挂着红）",
          mgr.save_health()["parts"][part_src]["ok"] is True,
          str(mgr.save_health()["parts"].get(part_src)))
    check("★聚合健康度回到 ok", mgr.save_health()["ok"] is True,
          str(mgr.save_health()["error"]))
    check("源文件确实被清空（动作本身没被打折）",
          mgr._file_entries(src_a) == 0)

    # 反向：源文件写失败 → 必须留红 + 明说源库没清空
    src_b, dst_b = ids["源B"], ids["目标B"]
    src_bp = mgr.preset_path(src_b)
    src_bp.write_text(json.dumps({"preset_id": src_b, "fingerprint": "",
                                  "entries": {"t2": {"c": "内容"}}, "deleted": []},
                                 ensure_ascii=False), encoding="utf-8")
    real = Path.write_text

    def _boom(self, *a, **k):
        if self == src_bp:
            raise OSError("伪磁盘满")
        return real(self, *a, **k)

    Path.write_text = _boom
    try:
        out2 = mgr.transfer(src_b, dst_b, mode="move")
    finally:
        Path.write_text = real
    check("源库清空失败仍报 ok=True（复制确实成了）", out2.get("ok") is True, str(out2))
    check("★源库清空失败要明说（warning 带「源预设清空失败」）",
          "源预设清空失败" in str(out2.get("warning")), str(out2.get("warning")))
    check("★源键留在红上", mgr.save_health()["parts"][mgr._preset_part(src_b)]["ok"] is False)
    check("账本错误带上真实原因", "预设文件写入失败" in mgr.save_health()["error"],
          mgr.save_health()["error"])


def main() -> int:
    print("=" * 78)
    print("v0.24.3 回归 · N06（重扫丢词典）/ N08（堆叠上限）/ N09（源预设清红）")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        n06_cases(base)
        n08_cases()
        n09_cases(base)
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (_pass, len(_fail)))
    if _fail:
        print("失败项：" + " / ".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
