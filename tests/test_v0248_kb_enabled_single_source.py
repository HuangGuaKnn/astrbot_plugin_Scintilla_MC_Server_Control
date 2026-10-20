# -*- coding: utf-8 -*-
"""v0.24.8 回归（第六轮复核 B7 / B8）：条目级 `enabled` 的**唯一口径** + 加载期只落一次盘。

B7：为什么「一处实现」比「每处都判对」更重要
============================================
条目级 `enabled` 来自**持久化文件**（手工编辑 / 旧版插件写坏 / 脚本写入）。第四轮把
`_searchable()` 改成严格解析之后，同一个默认值 `True` 仍然散在 7 处读点里，方向还能相反：

    load() 的收敛点          读不懂 → 按 default（当时是 True）
    _searchable()            读不懂 → True
    list_entries() / stats() / _entry_view() / _public_entry()   各写各的
    save_entry(rename_from=) 把旧值**原样**搬给新条目

于是同一个文件能读出互相矛盾的结论：界面列表显示「已禁用」、检索照样命中；
统计里 `disabled` 是 0，而检索把它排除在外。**每处各自判对**这件事，人做不到 ——
所以判据不是「7 处都写对了吗」，而是「7 处是不是同一处」。

两条规则必须分开，因为来路不同（本文件逐格钉住）：

  * 字段**不存在** → `True`（历史兼容）：老文件里没有这个字段，按「未禁用」处理才不会
    一夜之间把整个库禁掉；
  * 字段**存在但读不懂**（`"maybe"` / 数字 2 / 列表…）→ `False`（保守关闭）：
    与写路径 `set_enabled()` 的方向一致 —— 宁可少开一个条目，也不让手改坏的值照常生效。

B8：收敛与迁移的落盘合并成一次
==============================
`_normalize_enabled_flags()` 此前**只改内存、不落盘**（`_migrate()` 只在缺 mod/kind 时
才 `save()`）—— 于是「文件里写坏布尔」这种情况每次启动都会重新收敛一遍、每次都留一条
WARNING，而文件里那个坏值一直没被修掉。反过来，若让两处各自 `save()`，同一批内容会触发
两次 `on_write`（= 插件的向量补算排两次单）。本文件两条都钉：**该写的时候写且只写一次**、
**不该写的时候一次都不写**。

运行：
  <AstrBot python> tests\\test_v0248_kb_enabled_single_source.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # tests/ → _paths
from _paths import add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as kbmod  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


class _Quiet:
    """静音桩：判据会故意喂一堆读不懂的值，不需要它们刷日志。"""

    def __init__(self) -> None:
        self.msgs: list[str] = []

    def warning(self, fmt: str, *a, **k) -> None:
        self.msgs.append(fmt % a if a else fmt)

    def info(self, *a, **k) -> None:
        pass

    def error(self, *a, **k) -> None:
        pass


def make_kb(data_dir: Path, preset_id: str = "p1") -> "kbmod.ModKnowledgeBase":
    return kbmod.ModKnowledgeBase(str(data_dir), server_id="fp", preset_id=preset_id,
                                  preset_name="测试库", fingerprint="fp")


#: 产品口径表（**写死**，不跟实现跑）：值 → 期望结论。
#: 「读不懂 → False」与「缺失 → True」这两条是产品的决定，不是实现的副产品。
VERDICTS = (
    (True, True), (False, False),
    (1, True), (0, False), (2, False), (100, False), (-1, False), (1.0, True), (0.0, False),
    ("true", True), ("TRUE", True), (" yes ", True), ("on", True), ("y", True), ("t", True),
    ("1", True),
    ("false", False), (" FALSE ", False), ("no", False), ("off", False), ("n", False),
    ("f", False), ("0", False), ("", False), ("   ", False),
    ("maybe", False), ("2", False), ("yesno", False), ("null", False),
    ([], False), ({}, False), ([1], False),
)


def audit_verdicts(resolve) -> list[str]:
    """口径表逐格比对；`resolve` = 「值 → 结论」的解析器（真实实现 / 病灶样本共用）。"""
    bad = []
    for value, want in VERDICTS:
        try:
            got = resolve(value)
        except Exception as e:                                   # noqa: BLE001
            bad.append(f"{value!r}：抛了 {type(e).__name__}")
            continue
        if got is not want:
            bad.append(f"{value!r}：{got!r} ≠ {want!r}")
    return bad


# ============================================================ 一、口径表
def part_one() -> None:
    print("---- 一、条目级口径表（唯一实现 `entry_enabled`）----")
    bad = audit_verdicts(lambda v: kbmod.entry_enabled({"enabled": v}))
    check(f"★口径表 {len(VERDICTS)} 格逐格正确（读不懂 → False，词表按后端词表）",
          not bad, "；".join(bad[:4]))
    check("★字段**缺失** → True（历史兼容：老文件不许被一夜禁掉）",
          kbmod.entry_enabled({"content": "没有 enabled 字段"}) is True)
    check("★结构不是 dict（文件被外力弄坏）→ False（保守）",
          kbmod.entry_enabled("坏结构") is False)
    check("`flag_enabled` 与 `entry_enabled` 同一实现（同一张表同样过）",
          not audit_verdicts(lambda v: kbmod.flag_enabled({"enabled": v}, "enabled", "t")))

    # 判据有牙：把「读不懂」改成 fail-open，这张表必须立刻报红
    def fail_open(v):
        try:
            return kbmod.entry_enabled({"enabled": v})
        except Exception:                                        # noqa: BLE001
            return True

    def bad_resolver(v):
        if isinstance(v, (list, dict)) or v == "maybe":
            return True                                          # 病灶：读不懂就放行
        return kbmod.entry_enabled({"enabled": v})

    check("阳性对照：口径表在真实实现上零违规", audit_verdicts(fail_open) == [],
          str(audit_verdicts(fail_open)))
    check("★判据有牙：把「读不懂」改成 fail-open，口径表立刻报红",
          bool(audit_verdicts(bad_resolver)), "口径表没拦住 fail-open")


# ============================================================ 二、三条路径同表
def part_two() -> None:
    print("\n---- 二、三条路径同表同结论：读点 / 字段口径 / 写入口 ----")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td) / "kb")
        topic = "tac 弹匣"
        kb.save_entry(topic, "扩容弹匣 30 发", source="webui", status="verified", manual=True)
        bad = []
        for value, want in VERDICTS:
            read = kbmod.entry_enabled({"enabled": value})
            field = kbmod.flag_enabled({"enabled": value}, "enabled", "t")
            out = kb.set_enabled(topic, value) or {}
            written = out.get("enabled")
            on_disk = json.loads(kb.file_path.read_text(encoding="utf-8"))
            disk = on_disk["entries"][kb._topic_key(topic)]["enabled"]
            if not (read is want and field is want and written is want and disk is want):
                bad.append(f"{value!r}：读点 {read!r} / 字段 {field!r} / 写入口 {written!r} / "
                           f"落盘 {disk!r}（期望 {want!r}）")
        check(f"★三条路径 + 落盘，{len(VERDICTS)} 格全部一致（0 格分叉）", not bad,
              "；".join(bad[:3]))
        # 写入口落盘的永远是 JSON 真布尔，不是字符串
        kb.set_enabled(topic, "false")
        on_disk = json.loads(kb.file_path.read_text(encoding="utf-8"))
        check("★写入口落盘的是 JSON 真布尔（文件里不会长回 \"false\"）",
              on_disk["entries"][kb._topic_key(topic)]["enabled"] is False,
              str(on_disk["entries"][kb._topic_key(topic)]))


# ============================================================ 三、读点一致（行为级）
def part_three() -> None:
    print("\n---- 三、绕过 load 直接改内存：7 处读点必须同口径（行为级，不数源码）----")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td) / "kb")
        good, dirty = "正常条目", "手改坏的条目"
        kb.save_entry(good, "扩容弹匣 30 发", source="webui", status="verified", manual=True)
        kb.save_entry(dirty, "另一条知识", source="webui", status="verified", manual=True)
        key = kb._topic_key(dirty)
        kb._data["entries"][key]["enabled"] = "maybe"            # 绕过 load 与 set_enabled

        check("★检索池排除（读不懂 → 关）", dirty not in kb._searchable(),
              str(list(kb._searchable())))
        check("★详情视图显示禁用", (kb.get_entry(dirty) or {}).get("enabled") is False,
              str(kb.get_entry(dirty)))
        rows = {r["topic"]: r for r in kb.list_entries()}
        check("★列表视图显示禁用", rows.get(dirty, {}).get("enabled") is False,
              str(rows.get(dirty)))
        check("★include_disabled=False 时被过滤掉",
              dirty not in {r["topic"] for r in kb.list_entries(include_disabled=False)})
        st = kb.stats()
        check("★统计里 disabled 计数把它算进去（与列表 / 检索同一口径）",
              st.get("disabled") == 1 and st.get("total") == 2, str(st))
        h_dirty = kbmod.SemanticIndex.content_hash(dirty, kb._data["entries"][key])
        h_false = kbmod.SemanticIndex.content_hash(dirty, {"content": "另一条知识",
                                                           "enabled": False})
        h_true = kbmod.SemanticIndex.content_hash(dirty, {"content": "另一条知识",
                                                          "enabled": True})
        check("★内容指纹也按同一口径（坏值 → 与显式 False 同哈希、与 True 不同）",
              h_dirty == h_false and h_dirty != h_true, f"{h_dirty[:8]}/{h_false[:8]}/{h_true[:8]}")
        check("正常条目不受影响（仍在检索池里）", good in kb._searchable())


# ============================================================ 四、继承路径
def part_four() -> None:
    print("\n---- 四、改名继承：搬的是**收敛后的结论**，不是上一版的原始值 ----")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td) / "kb")
        old = "旧主题"
        kb.save_entry(old, "扩容弹匣 30 发", source="webui", status="verified", manual=True)
        kb._data["entries"][kb._topic_key(old)]["enabled"] = "maybe"   # 内存里的坏值
        kb.save_entry("新主题", "扩容弹匣 30 发", source="webui", status="verified",
                      manual=True, rename_from=old)
        got = (kb.get_entry("新主题") or {}).get("enabled")
        check("★继承的是 False（真布尔），不是 \"maybe\" 这个原始值", got is False, repr(got))
        on_disk = json.loads(kb.file_path.read_text(encoding="utf-8"))
        check("落盘里也是真布尔",
              on_disk["entries"][kb._topic_key("新主题")]["enabled"] is False,
              str(on_disk["entries"][kb._topic_key("新主题")]))


# ============================================================ 五、加载期落盘（B8）
def counting_kb(data_dir: Path) -> tuple:
    """造一个 KB 并挂上计数桩（save / on_write 各计一次）。"""
    kb = make_kb(data_dir)
    calls = {"save": 0, "write": 0}
    real_save = kb.save

    def counting_save():
        calls["save"] += 1
        return real_save()

    kb.save = counting_save
    kb.on_write = lambda: calls.__setitem__("write", calls["write"] + 1)
    return kb, calls


def part_five() -> None:
    print("\n---- 五、加载期的收敛 / 迁移：该写的时候写，且只写一次 ----")
    dirty_bool = {"content": "坏布尔但字段齐全", "status": "verified", "enabled": "false",
                  "mod": "general", "kind": "instance"}
    both = {"content": "坏布尔且缺 mod/kind", "status": "verified", "enabled": "maybe"}
    clean = {"content": "干净条目", "status": "verified", "enabled": True,
             "mod": "general", "kind": "instance"}

    # ① 只有坏布尔（mod/kind 齐全）：旧实现只改内存、不落盘 → 文件里还是 "false"
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "kb"
        kb, calls = counting_kb(d)
        kb.file_path.write_text(json.dumps({"preset_id": "p1", "entries": {"A": dirty_bool}},
                                           ensure_ascii=False), encoding="utf-8")
        quiet = _Quiet()
        real_log, kbmod._log = kbmod._log, quiet
        try:
            kb.load()
        finally:
            kbmod._log = real_log
        on_disk = json.loads(kb.file_path.read_text(encoding="utf-8"))
        check("★坏布尔被**写回文件**（不再每次启动都收敛一遍）",
              on_disk["entries"]["A"]["enabled"] is False,
              repr(on_disk["entries"]["A"]["enabled"]))
        check("★save 恰好 1 次（不是 0 次「没落盘」）", calls["save"] == 1, str(calls))
        check("★on_write 恰好 1 次（向量补算只排一次单）", calls["write"] == 1, str(calls))
        n_warn1 = len([m for m in quiet.msgs if "不是布尔" in m])
        check("收敛时留一条 WARNING（有痕）", n_warn1 == 1, f"{n_warn1} 条：{quiet.msgs[:2]}")

        # ② 文件已收敛：再 load 不该再写盘、也不该再警告
        calls["save"] = calls["write"] = 0
        quiet.msgs.clear()
        real_log, kbmod._log = kbmod._log, quiet
        try:
            kb.load()
        finally:
            kbmod._log = real_log
        check("★干净文件再 load → save 0 次（不是「一律写盘」）", calls["save"] == 0, str(calls))
        check("★干净文件再 load → 0 条 WARNING（不再刷屏）",
              not [m for m in quiet.msgs if "不是布尔" in m], str(quiet.msgs[:2]))

    # ③ 坏布尔 + 缺 mod/kind：两条改动合并成**一次**落盘（两次 save = 两次向量补算）
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "kb"
        kb, calls = counting_kb(d)
        kb.file_path.write_text(json.dumps({"preset_id": "p1", "entries": {"A": both}},
                                           ensure_ascii=False), encoding="utf-8")
        quiet = _Quiet()
        real_log, kbmod._log = kbmod._log, quiet
        try:
            kb.load()
        finally:
            kbmod._log = real_log
        on_disk = json.loads(kb.file_path.read_text(encoding="utf-8"))
        check("★两条改动都落了盘（布尔收敛 + mod/kind 补齐）",
              on_disk["entries"]["A"]["enabled"] is False
              and "mod" in on_disk["entries"]["A"] and "kind" in on_disk["entries"]["A"],
              str(on_disk["entries"]["A"]))
        check("★合并成一次落盘（save == 1 / on_write == 1，不是 2）",
              calls["save"] == 1 and calls["write"] == 1, str(calls))

    # ④ 阳性对照：本来就干净 → 一次都不写
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "kb"
        kb, calls = counting_kb(d)
        kb.file_path.write_text(json.dumps({"preset_id": "p1", "entries": {"A": clean}},
                                           ensure_ascii=False), encoding="utf-8")
        kb.load()
        check("★阳性对照：文件本来就干净 → save 0 次 / on_write 0 次",
              calls["save"] == 0 and calls["write"] == 0, str(calls))


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 第六轮复核 B7 / B8：条目级 enabled 唯一口径 + 加载期只落一次盘")
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
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
