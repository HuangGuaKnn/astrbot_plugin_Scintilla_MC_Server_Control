"""B5 · 旧版（1.13 以下）**真实 Item 注册表快照**生成器 / 复验器（GPT v0.24.2 复核 N04）。

为什么要有它
============
``give`` 从服务端的 **Item registry** 取对象。「Block 注册名存在」「.lang 里有这个键」
都**不能**证明某个名字能发放 —— 1.12.2 的 ``water`` / ``fire`` / ``frosted_ice`` /
``pumpkin_stem`` 是 Block 而非 Item；``charcoal`` 只是 ``coal`` 的 metadata 1；
``oak_fence_gate`` 该代注册名其实叫 ``fence_gate``。旧数据表把这些名字当注册名写进
bridge，13 条统统能生成 ``give`` 命令（服务端当场报错），而当时的夹具审计只比对两代
一致性、数条数，**发现不了两代同错**（GPT 复核：把两代 apple 都改成同一个不存在的
ID，用例仍全绿）。

本脚本从**真 jar 的字节码**里把该代的 Item 注册表原样抽出来（名字 + numeric ID +
每物品真实堆叠上限），落成离线快照 ``data/legacy_registry/items_<mc>.json``：
CI 与运行时都据此判断「某个名字到底能不能 give」，不再需要作者机器上有 jar，
也不再靠人脑记忆猜注册名。

抽取方法（两代都按「显式注册 + 方块物品注册」两面取，缺一面都不完整）
==================================================================
* 1.12.2 —— ``ain``（Item 类）：``ain.a(<id>, "<name>", <expr>)`` 是显式注册；
  ``ain.t()`` 段把方块注册成 ItemBlock（``ain.a(aox.<field>, <expr>)`` /
  ``ain.b(aox.<field>)``），方块名从 ``aox`` 的字段→注册名映射取。
* 1.7.10 —— ``adb``（Item 类）：``e.a(<id>, "<name>", <expr>)`` 是显式注册；
  ``adb.l()`` 遍历方块注册表，**循环体内**``hashSet.contains(block)`` 命中且没有
  任何特殊分支的方块不注册物品（源码即 ``if (hashSet.contains(...)) continue;``），
  其余挂通用 ItemBlock、名字沿用方块注册名。
* 注册现场常先落进局部变量（``Item it = new ItemBucket(...).d(16); register(325, "bucket", it)``），
  因此引用会先按变量声明还原成构造表达式。
* 堆叠上限 —— 不是猜的：基类 ``Item`` 自带默认值（1.12.2 ``k = 64``、1.7.10 ``h = 64``），
  子类构造器会写 ``this.k = N`` 或调设置器（1.12.2 ``d(N)``、1.7.10 ``e(N)``），注册现场
  还能再链一次（``.d(16)``）。三者按「基类默认 → 继承链逐层覆盖 → 注册现场最后覆盖」
  定值，并记下这个值从哪儿来（``max_src``）。

范围纪律：特殊分支只在**循环体内**算数
==========================================
``adb.l()`` 的排除集合是 27 个方块字段（空气/床/酿造台/蛋糕/炼药锅/花盆/铁门/两种
红石矿/两种西瓜南瓜茎/地狱疣/两种活塞/四种中继比较器/红石线/甘蔗/头颅/两种告示牌/
绊线/两种红石火把/墙告示牌/小麦/木门），其中**没有**任何一个在循环体内被特殊分支
接走，因此它们整批不注册物品（171 方块 − 27 = 144 枚方块物品）。

★ 特殊分支的比较**必须**只在 ``for (String string : aji.c.b())`` 到循环体结束这段里扫：
一开始按**整个类**扫，别的方法里比过的同名字段会被误判成「有特殊分支」，该排除的方块
就被留进注册表（实测多出 16 枚、总数 331 而非 315）。现在按循环体切分，两代都与复核
材料一致（1.7.10 = 315、1.12.2 = 411）。

水/岩浆/火/传送门在这代**是**物品（不在排除集合里，走的是通用 ItemBlock 那条路），
而 1.12.2 的同一批名字**不是** —— 这正是「拿世代对应的真注册表当判据」的意义。

用法
====
    <python> tests\\make_legacy_registry_snapshot.py            # 生成 / 刷新快照（需靶场 jar）
    <python> tests\\make_legacy_registry_snapshot.py --check     # 只复验：现快照 vs 现 jar（CI / 本机）
    <python> tests\\make_legacy_registry_snapshot.py --canary    # 顺手打印关键对照项

靶场位置由环境变量 ``MC_MATRIX_DIR`` 指定；不给就按 ``~/Desktop/mcs-matrix`` 找
（便携写法，仓库里不写死任何本机路径 —— 见 ``tests/test_v0240_no_local_paths.py`` 的守卫）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_DIR = ROOT / "data" / "legacy_registry"

#: 靶场目录：环境变量优先，缺省按 ``~/Desktop/mcs-matrix``（**不写死本机路径**，换台机器也能跑）。
MATRIX = Path(os.environ.get("MC_MATRIX_DIR") or (Path.home() / "Desktop" / "mcs-matrix"))
CFR = MATRIX / "tools" / "cfr-0.152.jar"

EXTRACTOR = "tests/make_legacy_registry_snapshot.py"
EXTRACTOR_VERSION = 1

#: 每代的抽取参数（类名、调用名、字段名、设置器名、来源 jar 指纹）。
#: ``jar_sha256`` 是**固定值**：靶场 jar 换了版本 / 被打过补丁，快照就不该继续用。
GENERATIONS = {
    "1.12.2": {
        "jar_rel": "1.12.2-forge/minecraft_server.1.12.2.jar",
        "jar_sha256": "fe1f9274e6dad9191bf6e6e8e36ee6ebc737f373603df0946aafcded0d53167e",
        "item_class": "ain",
        "block_registry_class": "aow",
        "block_field_class": "aox",
        "item_callee": "ain.a",
        "block_callee": "aow.a",
        "block_item_callees": ("ain.a", "ain.b"),
        "block_section_marker": "public static void t()",
        "block_section_end": "ain.a(256,",
        "max_field": "k",
        "max_setter": "d",
    },
    "1.7.10": {
        "jar_rel": "1.7.10-forge/minecraft_server.1.7.10.jar",
        "jar_sha256": "c70870f00c4024d829e154f7e5f4e885b02dd87991726a3308d81f513972f3fc",
        "item_class": "adb",
        "block_registry_class": "aji",
        # 1.7.10 没有独立的「方块字段→注册名」类：方块名直接从 aji 的注册调用取。
        "block_field_class": None,
        # 这代的注册调用挂在内嵌持有类上：物品 ``e.a(id, "name", item)``、方块 ``c.a(id, "name")``。
        "item_callee": "e.a",
        "block_callee": "c.a",
        "block_item_callees": (),
        "block_section_marker": None,
        "block_section_end": None,
        "max_field": "h",
        "max_setter": "e",
    },
}

DEFAULT_MAX = 64


# --------------------------------------------------------------------------- #
# 基础工具：读 jar、反编译（带缓存）、按括号深度切参数
# --------------------------------------------------------------------------- #

class _Jar:
    """真 jar 的只读门面：取条目 + 按需反编译（结果缓存，避免重复起 java）。"""

    def __init__(self, path: Path, workdir: Path):
        self.path = path
        self.workdir = workdir
        self._zip = zipfile.ZipFile(path)
        self._cache: dict[str, str] = {}

    def sha256(self) -> str:
        h = hashlib.sha256()
        with io.open(self.path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()

    def has(self, entry: str) -> bool:
        return entry in self._zip.namelist()

    def source(self, cls: str) -> str:
        """反编译 ``cls``（如 ``ain`` / ``ain$a``）并返回 Java 源码；缺失返回空串。"""
        if cls in self._cache:
            return self._cache[cls]
        entry = cls + ".class"
        text = ""
        if self.has(entry):
            cls_file = self.workdir / (self.path.stem + "-" + cls.replace("$", "_") + ".class")
            cls_file.write_bytes(self._zip.read(entry))
            r = subprocess.run(
                ["java", "-jar", str(CFR), str(cls_file), "--silent", "true"],
                capture_output=True, text=True, errors="replace",
            )
            text = r.stdout or ""
        self._cache[cls] = text
        return text


def _split_args(argstr: str) -> list[str]:
    """按**顶层**逗号切参数（忽略嵌套括号 / 字符串里的逗号）。"""
    out, depth, cur, quote = [], 0, [], None
    for ch in argstr:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _scan_calls(src: str, callee: str) -> list[tuple[str, str]]:
    """扫描 ``callee(...)`` 调用，返回 ``[(参数串, 整段语句), ...]``（嵌套括号有保护）。

    左边界用 ``(?<![\\w$.])`` 收口：``e.a(`` **不许**匹配到 ``aje.a(`` 这种长名字的尾巴
    （1.7.10 的注册调用就挂在内嵌持有类 ``e`` 上，少了这个边界会串味）。
    """
    out = []
    for m in re.finditer(r"(?<![\w$.])" + re.escape(callee) + r"\(", src):
        i = m.end() - 1
        depth, j, quote = 0, i, None
        while j < len(src):
            ch = src[j]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue
        args = src[i + 1:j]
        stmt_end = src.find(";", j)
        stmt = src[m.start(): stmt_end if stmt_end > 0 else j]
        out.append((args, stmt))
    return out


#: 「变量 = new <类>(...)」的赋值行（构造现场常先落进变量再注册）。
_VAR_DECL_RE = re.compile(
    r"^[ \t]*(?:final\s+)?[\w$.<>\[\]]+\s+([\w$]+)\s*=\s*(?:\([\w$.<>\[\]]+\))?\s*(new\s+[^;]+);",
    re.M,
)


def _var_map(src: str) -> dict[str, str]:
    """变量 / 字段名 → 它的 ``new ...`` 初始化表达式（用于把引用还原成现场）。"""
    out: dict[str, str] = {}
    for m in _VAR_DECL_RE.finditer(src):
        out.setdefault(m.group(1), m.group(2))
    return out


def _resolve_expr(expr: str, varmap: dict[str, str], hops: int = 2) -> str:
    """把 ``it`` 这类变量引用还原成真正的构造表达式（最多追 ``hops`` 跳）。"""
    cur = expr.strip()
    for _ in range(hops):
        if re.fullmatch(r"[\w$]+", cur) and cur in varmap:
            cur = varmap[cur].strip()
            continue
        break
    return cur


def _class_of(expr: str) -> "str | None":
    m = re.search(r"new\s+([\w$]+)\s*\(", expr)
    return m.group(1) if m else None


def _max_from_expr(expr: str, setter: str) -> "int | None":
    """注册现场链式设置器（``.d(16)``）给出的上限；多个取最后一个（后者覆盖）。"""
    hits = re.findall(r"\.%s\((\d+)\)" % re.escape(setter), expr)
    return int(hits[-1]) if hits else None


def _max_from_ctor(src: str, max_field: str, setter: str) -> "int | None":
    """类里写死的上限：字段赋值（``this.k = N``）与设置器调用（``this.d(N)``）都认。"""
    val = None
    for pattern in (r"this\.%s\s*=\s*(\d+)\s*;" % re.escape(max_field),
                    r"this\.%s\((\d+)\)" % re.escape(setter)):
        hits = re.findall(pattern, src)
        if hits:
            val = int(hits[-1])
    return val


def _super_of(src: str) -> "str | None":
    m = re.search(r"class\s+[\w$]+\s+extends\s+([\w$.]+)", src)
    if not m:
        return None
    name = m.group(1).split(".")[-1]
    return None if name == "Object" else name


def base_default(jar: _Jar, gen: str) -> int:
    """从 Item 基类读出默认堆叠上限（不靠人记）。"""
    cfg = GENERATIONS[gen]
    src = jar.source(cfg["item_class"])
    m = re.search(r"(?:protected|public|private)?\s*int\s+%s\s*=\s*(\d+)\s*;"
                  % re.escape(cfg["max_field"]), src)
    return int(m.group(1)) if m else DEFAULT_MAX


def resolve_max(jar: _Jar, cls: "str | None", site_expr: str, gen: str,
                default: int) -> tuple[int, str]:
    """算出该物品的真实堆叠上限，并说明**这个值是从哪儿来的**。

    优先级（后覆盖前）：基类默认 ``64`` → 继承链构造器 → 注册现场链式调用。
    """
    cfg = GENERATIONS[gen]
    value, src = default, "default"
    seen: set[str] = set()
    cur = cls
    while cur and cur not in seen and len(seen) < 6:
        seen.add(cur)
        text = jar.source(cur)
        if not text:
            break
        hit = _max_from_ctor(text, cfg["max_field"], cfg["max_setter"])
        if hit is not None:
            value, src = hit, f"ctor:{cur}"
            break                      # 近来者胜：子类覆盖了父类
        cur = _super_of(text)
    site = _max_from_expr(site_expr, cfg["max_setter"])
    if site is not None:
        value, src = site, "site"
    return value, src


def _strip(value: str) -> str:
    return value.strip().strip('"')


def extract(gen: str, workdir: Path) -> dict:
    cfg = GENERATIONS[gen]
    jar_path = MATRIX / cfg["jar_rel"]
    if not jar_path.exists():
        raise FileNotFoundError(f"靶场 jar 不在：{jar_path}")
    jar = _Jar(jar_path, workdir)
    sha = jar.sha256()
    if sha != cfg["jar_sha256"]:
        raise RuntimeError(f"{gen} 的 jar 指纹不符：期望 {cfg['jar_sha256']}，实际 {sha}"
                           "（换过版本就请先更新 GENERATIONS 里的期望值并重新复核）")

    item_src = jar.source(cfg["item_class"])
    if not item_src:
        raise RuntimeError(f"{gen}: 抽不到 Item 类 {cfg['item_class']}")
    default = base_default(jar, gen)

    # ---- 方块注册名 ----
    block_field_map: dict[str, str] = {}
    if cfg["block_field_class"]:
        for m in re.finditer(r"([\w$]+)\s*=\s*(?:\([\w$.]+\))?\s*%s\.a\(\"([^\"]+)\"\)"
                             % re.escape(cfg["block_field_class"]), jar.source(cfg["block_field_class"])):
            block_field_map[m.group(1)] = m.group(2)
    block_registry: set[str] = set()
    for args, _stmt in _scan_calls(jar.source(cfg["block_registry_class"]), cfg["block_callee"]):
        parts = _split_args(args)
        if len(parts) >= 2 and parts[0].strip().isdigit():
            block_registry.add(_strip(parts[1]))
    block_names = set(block_field_map.values()) | block_registry

    items: dict[str, dict] = {}

    def _add(name: str, numeric_id: "int | None", expr: str, origin: str) -> None:
        mx, mx_src = resolve_max(jar, _class_of(expr), expr, gen, default)
        row = items.setdefault(name, {"id": numeric_id, "max": mx, "max_src": mx_src, "origin": origin})
        if row.get("id") is None:
            row["id"] = numeric_id

    varmap = _var_map(item_src)

    # ---- 显式物品注册 ----
    explicit = 0
    for args, _stmt in _scan_calls(item_src, cfg["item_callee"]):
        parts = _split_args(args)
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue                      # 方块物品段的调用（第一参数是方块引用）
        name = _strip(parts[1])
        if not re.fullmatch(r"[a-z0-9_]+", name):
            continue
        _add(name, int(parts[0]), _resolve_expr(parts[2], varmap), "explicit")
        explicit += 1

    # ---- 方块物品注册 ----
    block_items = 0
    if cfg["block_section_marker"]:
        seg = item_src.split(cfg["block_section_marker"], 1)
        seg = seg[1].split(cfg["block_section_end"], 1)[0] if len(seg) > 1 else ""
        sub_vars = _var_map(seg)
        for callee in cfg["block_item_callees"]:
            for args, _stmt in _scan_calls(seg, callee):
                parts = _split_args(args)
                if not parts:
                    continue
                m = re.search(r"[\w$]*%s\.([\w$]+)" % re.escape(cfg["block_field_class"]), parts[0])
                if not m:
                    continue
                name = block_field_map.get(m.group(1))
                if not name:
                    continue
                _add(name, None,
                     _resolve_expr(parts[1], sub_vars) if len(parts) > 1 else "", "block")
                block_items += 1
    else:
        # 1.7.10：adb.l() 遍历方块注册表 —— 名字在排除集合里、且**循环体内**没命中任何
        # 特殊分支的方块不注册物品（源码即 ``if (hashSet.contains(block)) continue;``）。
        # ★ 特殊分支只在**循环体内**算数：早先按整个类扫，别的方法里比过的字段会被误当
        #   成「有特殊分支」，该排除的方块（水 / 岩浆 / 火 / 传送门…）就被留下了。
        loop_head = "for (String string : aji.c.b())"
        loop_body = item_src.split(loop_head, 1)[1] if loop_head in item_src else ""
        loop_body = loop_body.split("e.a(aji.b(", 1)[0]
        excl_line = next((row for row in item_src.splitlines() if "HashSet hashSet" in row), "")
        field_map = dict(re.findall(r'\b([\w$]+)\s*=\s*[^;\n]*aji\.c\.a\("([^"]+)"\)',
                                    jar.source("ajn")))
        excl_fields = re.findall(r"ajn\.([\w$]+)", excl_line)
        special_fields = re.findall(r"==\s*ajn\.([\w$]+)", loop_body)
        excluded = {field_map.get(f) for f in excl_fields} - {field_map.get(f) for f in special_fields}
        for name in sorted(block_names - {e for e in excluded if e}):
            _add(name, None, "", "block")
            block_items += 1

    counts = {
        "items": len(items),
        "blocks": len(block_names),
        "explicit": explicit,
        "block_items": block_items,
        "max_stacks": {},
    }
    for row in items.values():
        key = str(row["max"])
        counts["max_stacks"][key] = counts["max_stacks"].get(key, 0) + 1

    return {
        "mc": gen,
        "generation": "legacy_preflatten",
        "counts": counts,
        "source": {
            "jar": Path(cfg["jar_rel"]).name,
            "jar_sha256": sha,
            "item_class": cfg["item_class"] + ".class",
            "block_registry_class": cfg["block_registry_class"] + ".class",
            "cfr": CFR.name,
            "extractor": EXTRACTOR,
            "extractor_version": EXTRACTOR_VERSION,
            "command": f"python tests/make_legacy_registry_snapshot.py --mc {gen}"
                       "   # 需 MC_MATRIX_DIR 指向 mcs-matrix",
            "method": "显式物品注册 + 方块物品注册；堆叠上限 = 基类默认 → 继承链构造器 → 注册现场链式设置器",
        },
        "default_max": default,
        "items": {k: items[k] for k in sorted(items)},
        "blocks_only": sorted(block_names - set(items)),
        "notes": ("本文件是**离线事实快照**：由上面那条命令从指名的真 jar 抽出来，"
                  "运行期与 CI 都据此判断某个名字能不能 give，不需要机器上有 jar。"
                  "改了 GENERATIONS 里的 jar_sha256 就必须重新复核全部条目。"),
    }


def write_snapshot(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def compare_snapshot(old: dict, new: dict) -> list[str]:
    """比对两份快照，返回差异清单（空 = 一致）。"""
    diffs = []
    for field in ("mc", "default_max"):
        if old.get(field) != new.get(field):
            diffs.append(f"{field}: 快照 {old.get(field)!r} ≠ 重算 {new.get(field)!r}")
    oi, ni = old.get("items") or {}, new.get("items") or {}
    for name in sorted(set(oi) ^ set(ni)):
        diffs.append(f"物品集差异：{name}（快照 {'有' if name in oi else '无'} / "
                     f"重算 {'有' if name in ni else '无'}）")
    for name in sorted(set(oi) & set(ni)):
        for key in ("id", "max"):
            if oi[name].get(key) != ni[name].get(key):
                diffs.append(f"{name}.{key}: 快照 {oi[name].get(key)!r} ≠ 重算 {ni[name].get(key)!r}")
    if sorted(old.get("blocks_only") or []) != sorted(new.get("blocks_only") or []):
        diffs.append("blocks_only 集合不一致")
    return diffs


CANARY = ["fence_gate", "wooden_door", "silver_glazed_terracotta", "end_portal_frame",
          "coal", "charcoal", "oak_door", "oak_fence_gate", "diamond", "cake",
          "totem_of_undying", "bucket", "snowball", "iron_shovel", "water", "fire"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="旧版 Item 注册表快照生成 / 复验")
    ap.add_argument("--check", action="store_true", help="只复验：现快照 vs 现 jar")
    ap.add_argument("--canary", action="store_true", help="打印关键对照项")
    ap.add_argument("--mc", default="", help="只处理某一代（1.7.10 / 1.12.2）")
    args = ap.parse_args(argv)

    gens = [args.mc] if args.mc else ["1.7.10", "1.12.2"]
    if not CFR.exists():
        print(f"[FAIL] 反编译器不在：{CFR}", file=sys.stderr)
        return 2
    if shutil.which("java") is None:
        print("[FAIL] PATH 里没有 java（CFR 需要它）", file=sys.stderr)
        return 2

    rc = 0
    with tempfile.TemporaryDirectory(prefix="legacy_registry_") as td:
        workdir = Path(td)
        for gen in gens:
            data = extract(gen, workdir)
            path = OUT_DIR / f"items_{gen}.json"
            dist = {k: v for k, v in sorted(data["counts"]["max_stacks"].items(),
                                            key=lambda kv: -int(kv[0]))}
            print(f"[ok] {gen}：Item {data['counts']['items']} 枚"
                  f"（显式 {data['counts']['explicit']} + 方块物品 {data['counts']['block_items']}）、"
                  f"方块 {data['counts']['blocks']} 枚、堆叠分布 {dist}")
            if args.check:
                if not path.exists():
                    print(f"[FAIL] {gen}：快照缺失 {path}", file=sys.stderr)
                    rc = 1
                    continue
                diffs = compare_snapshot(json.loads(path.read_text(encoding="utf-8")), data)
                if diffs:
                    print(f"[FAIL] {gen}：快照与重算有 {len(diffs)} 处不一致", file=sys.stderr)
                    for d in diffs[:20]:
                        print("        " + d, file=sys.stderr)
                    rc = 1
                else:
                    print(f"[ok] {gen}：快照与真 jar 重算一致（{path.name}）")
            else:
                write_snapshot(data, path)
                print(f"     → 写入 {path}")
            if args.canary:
                print("     canary: " + ", ".join(
                    f"{c}={'有' if c in data['items'] else '无'}"
                    + (f"(max={data['items'][c]['max']})" if c in data["items"] else "")
                    for c in CANARY))
    return rc


if __name__ == "__main__":
    sys.exit(main())
