# -*- coding: utf-8 -*-
"""前端 `parseCfgBool` 与后端 `strict_bool` 的**逐格同源**判据（v0.24.8 · 第六轮复核 B6 / B10）。

为什么要有这一支
================
第四轮把设置页的布尔渲染从 `!!value` 改成 `parseCfgBool(value, default)`，判据写的是：

    check(…, "parseCfgBool(v, dft)" in html)
    same_line = [ln for ln in html.splitlines() if ".checked" in ln and "!!v" in ln]
    check(…, not same_line)

第六轮复核把三条变异喂进去，**三条全存活**（实测）：

| 变异 | 断言①（字符串） | 断言②（同行 `!!v`） |
|---|---|---|
| 调用点改回 `!!e.enabled` | 绿 | 绿 |
| 函数体整段换成 `return !!v;` | 绿 | 绿 |
| 删掉数字分支 `if(typeof v === "number") …` | 绿 | 绿 |

第二条尤其致命：它的效果就是**把 P4 要修的那个 bug 原样装回去**（字符串 `"false"` 又被画成
「开」），而两条断言一条都没红 —— 因为 `function parseCfgBool(v, dft)` 这串字**是函数签名
本身**，函数体怎么换它都在。**读文本的判据证明不了行为。**

本支怎么判
==========
照抄仓库里已有的现成范式（`tests/test_v0248_collect_settings.py`）：

* 从 `pages/mc_control/index.html` 里**抽取真实的 `parseCfgBool` 与两份词表常量原文**
  （不是复制一份到用例里 —— 复制会和实现漂移），在 node 里真的跑；
* **输入表从后端词表生成**（`core/strict_bool.py` 的 `_TRUE_TOKENS` / `_FALSE_TOKENS`），
  不是人手列 —— 第六轮实测的 12 格分叉里，`y`/`t`/`n`/`f` 那一族就是「人列表」漏掉的：
  后端词表里有、前端词表里没有，人列必漏格；
* **每一格跑两档默认值**：空串族只在「默认值 = True」的字段上露头、非 0/1 数字族只在
  「默认值 = False」的字段上露头 —— 两族的露头条件正好相反，**只跑一档会漏掉一半**
  （这正是「看起来对账了、其实只对了一半」的那种假绿）；
* 逐格与 Python 侧 `strict_bool(value, default)` 比对，**0 格分叉**才算过；
* 外加一条**调用点哨兵**：两个条目级渲染点必须走 `parseCfgBool`，设置页任何把配置值
  画到开关上的行都不许出现 `!!`；
* **第三层（v0.24.8 第八轮 · GPT 第七轮复核 B10）**：把设置页的**渲染循环原文**抽出来，
  配最小 DOM 桩在 node 里**真跑一遍**，看每个格里开关**最终画成什么**。
  起因是第七轮复核实测：把 `if(ty==="b") el.checked = …parseCfgBool(v, dft);` 整句换成
  `el.checked = !!v;`，函数本体一个字没动、文本哨兵照样命中（`parseCfgBool(v, dft)` 这串字
  在别处还在），整页回退成 `!!v` 而判据全绿。**函数对 ≠ 它被用上了。**

判据自己有没有牙
================
`audit()` 同时用于真实页面与**内存里变异过的页面**：五种变异各自必须让 `audit()` 报红，
一条等价改写（阳性对照）必须照样绿 —— 否则「有牙」这句话本身就是没牙的。

依赖：node（GitHub Actions 的 ubuntu-latest 自带；本机亦已装）。
node 缺席时**显式失败**，不静默跳过 —— 「跑不动」不能伪装成「通过了」。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
# 抽取函数的工具**复用**现成实现（`tests/test_v0248_collect_settings.py` 里那一支），
# 不再抄第二份 —— 抄一份就多一处会和实现漂移的地方。
from test_v0248_collect_settings import _extract_function  # noqa: E402

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core import strict_bool as sb  # noqa: E402

PAGE = PLUGIN_DIR / "pages" / "mc_control" / "index.html"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


class _Quiet:
    """静音桩：逐格比对会故意喂一堆读不懂的值，WARNING 由后端实现去留，
    判据这里不需要它们刷屏（真实页面的行为不受影响）。"""

    def warning(self, *a, **k) -> None:      # noqa: D102
        pass

    def info(self, *a, **k) -> None:         # noqa: D102
        pass


_QUIET = _Quiet()


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        _FAILED.append(name)
        print(f"[FAIL] {name}" + (f"  →  {detail}" if detail else ""))


# ============================ 抽取 ============================

def extract_tokens(html: str) -> tuple[set, set]:
    """从 index.html 里读出 `CFG_TRUE_TOKENS` / `CFG_FALSE_TOKENS` 的字面量。

    抽不出来直接抛错（宁可红，也不许拿空集合去判绿 —— 空集合对空集合最容易假绿）。
    """
    out = []
    for name in ("CFG_TRUE_TOKENS", "CFG_FALSE_TOKENS"):
        m = re.search(r"const\s+" + name + r"\s*=\s*new Set\(\[(.*?)\]\)", html, re.S)
        if not m:
            raise AssertionError(f"index.html 里找不到 `const {name} = new Set([...])`")
        out.append(set(json.loads("[" + m.group(1) + "]")))
    return out[0], out[1]


def extract_bool_source(html: str) -> str:
    """词表 + `parseCfgBool` 本体（node 侧要一起 eval）。"""
    start = html.find("const CFG_TRUE_TOKENS")
    end = html.find("function parseCfgBool(")
    if start < 0 or end < 0 or end < start:
        raise AssertionError("index.html 里找不到 CFG_TRUE_TOKENS / parseCfgBool 的原文")
    return html[start:end] + _extract_function(html, "parseCfgBool")


#: 设置页把配置值写进控件的**渲染循环头**（第八轮 B10 的盲区就在这个循环里的那一行）。
_RENDER_ANCHOR = "for(const [id,key,ty,dft] of CFG_FIELDS){"


def extract_render_loop(html: str) -> str:
    """抽取渲染循环的**原文**（含 `ty==="b"` 那一行），供 node 真跑。

    锚点是循环头原文 —— 循环被改名 / 被换写法就抽不出来，抽不出来**判红**。
    这是故意的保守方向：宁可要人来更新锚点，也不许「抽不到就当通过」。
    """
    start = html.find(_RENDER_ANCHOR)
    if start < 0:
        raise AssertionError(f"index.html 里找不到渲染循环 `{_RENDER_ANCHOR}`")
    brace = html.find("{", start)
    depth = 0
    for i in range(brace, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError("渲染循环的大括号没有闭合")


#: 输入表：**从后端词表生成** + 边界值。每格都带两档默认值（见模块说明）。
def build_cells() -> list[dict]:
    cells: list[dict] = []

    def add(kind: str, value, label: str) -> None:
        for dft in (True, False):
            cells.append({"kind": kind, "v": value, "dft": dft, "label": label})

    add("null", None, "None / null")
    add("undefined", None, "undefined（键不存在）")
    add("value", True, "JSON 布尔 true")
    add("value", False, "JSON 布尔 false")
    for n in (0, 1, 2, 3.5, -1, 0.0, 1.0, 100):
        add("value", n, f"数字 {n}")
    tokens = sorted(sb._TRUE_TOKENS | sb._FALSE_TOKENS)          # ← 词表来自后端，不是人列
    for t in tokens:
        add("value", t, f"词表原样 {t!r}")
    for t in sorted(sb._TRUE_TOKENS | sb._FALSE_TOKENS):
        if t:
            add("value", t.upper(), f"大写 {t.upper()!r}")
            add("value", f" {t} ", f"带首尾空白 {t!r}")
    for s in ("", "   ", "maybe", "2", "-1", "null", "yesno", "ON "):
        add("value", s, f"字符串 {s!r}")
    add("value", [], "空数组")
    add("value", {}, "空对象")
    add("value", [1], "非空数组")
    add("value", {"a": 1}, "非空对象")
    return cells


_HARNESS = r"""
const fs = require("fs");
const boolSrc = fs.readFileSync(process.argv[2], "utf8");      // 词表 + parseCfgBool 原文
const cells = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
eval(boolSrc + "\n;globalThis.__pcb = parseCfgBool;");
const arg = (c) => (c.kind === "undefined" ? undefined : c.v);
const out = cells.map((c) => globalThis.__pcb(arg(c), c.dft));
process.stdout.write(JSON.stringify(out));
"""


def run_node(bool_src: str, cells: list[dict], node: str) -> list:
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "harness.js"
        src = Path(td) / "bool.js"
        data = Path(td) / "cells.json"
        js.write_text(_HARNESS, encoding="utf-8")
        src.write_text(bool_src, encoding="utf-8")
        data.write_text(json.dumps(cells, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([node, str(js), str(src), str(data)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    if r.returncode != 0:
        raise AssertionError("node 跑 parseCfgBool 失败：" + (r.stderr or r.stdout or "")[-400:])
    return json.loads(r.stdout)


# ============================ 判据本体（真实页面与变异页面共用） ============================

_RENDER_HARNESS = r"""
const fs = require("fs");
const boolSrc = fs.readFileSync(process.argv[2], "utf8");   // 词表 + parseCfgBool 原文
const loopSrc = fs.readFileSync(process.argv[3], "utf8");   // 渲染循环原文
const cells = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
eval(boolSrc + "\n;globalThis.__pcb = parseCfgBool;");
// 注意：这里不能再 `const parseCfgBool = …` —— 直接 eval 已经把 parseCfgBool 声明在
// 本模块作用域里了，重名会直接 SyntaxError（第一次跑就是这么挂的：判据本身写错 ≠ 页面有问题）。

// 最小 DOM 桩：只实现这段循环**真的会碰**的属性 / 方法（不是模拟浏览器）
function makeEl(id){
  return { id, checked: null, value: null, innerHTML: "", options: [] };
}
const els = {};
const $ = (id) => els[id] || (els[id] = makeEl(id));
const ensureProviderOption = () => {};
const syncPwdPlaceholder = () => {};
const S_DEFAULTS = {};
const render = new Function("cfg", "CFG_FIELDS", "$", "S_DEFAULTS",
  "ensureProviderOption", "syncPwdPlaceholder", "parseCfgBool", loopSrc);

// 每格：只放一个布尔字段，默认值就是这一格要测的那一档，读回它**最终**画成的 checked
const out = cells.map((c) => {
  const el = makeEl("cfg_probe");
  els["cfg_probe"] = el;
  const cfg = { probe: (c.kind === "undefined" ? undefined : c.v) };
  render(cfg, [["cfg_probe", "probe", "b", c.dft]], $, S_DEFAULTS,
         ensureProviderOption, syncPwdPlaceholder, globalThis.__pcb);
  return el.checked;
});
process.stdout.write(JSON.stringify(out));
"""


def run_render(html: str, cells: list[dict], node: str) -> list:
    """把设置页的渲染循环**真跑一遍**，返回每个格里开关最终是开还是关。"""
    loop_src = extract_render_loop(html)
    bool_src = extract_bool_source(html)
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "render.js"
        b = Path(td) / "bool.js"
        lp = Path(td) / "loop.js"
        data = Path(td) / "cells.json"
        js.write_text(_RENDER_HARNESS, encoding="utf-8")
        b.write_text(bool_src, encoding="utf-8")
        lp.write_text(loop_src, encoding="utf-8")
        data.write_text(json.dumps(cells, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([node, str(js), str(b), str(lp), str(data)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    if r.returncode != 0:
        raise AssertionError("node 跑渲染循环失败：" + (r.stderr or r.stdout or "")[-400:])
    return json.loads(r.stdout)


_CALLSITE_RE = re.compile(r"parseCfgBool\(\s*e\.enabled\s*,\s*true\s*\)")
#: 设置页布尔渲染的调用点（`= / :` 之后的那次调用；**函数签名不算**）
_SITE_BOOL_RE = re.compile(r"[=:]\s*parseCfgBool\(\s*v\s*,\s*dft\s*\)")


def audit_text(html: str) -> list[str]:
    """① 文本层（**最弱**的一层：第六轮实测过它可以全绿而 bug 原样在）。
    留着是因为它便宜、且能抓到「明显的写法退化」；真正算数的是下面两层行为判据。"""
    problems: list[str] = []
    # 词表逐字同源（顺序无关：Set 语义）
    try:
        f_true, f_false = extract_tokens(html)
    except AssertionError as exc:
        return [str(exc)]
    if f_true != set(sb._TRUE_TOKENS):
        problems.append(f"真值词表不同源：前端多 {sorted(f_true - set(sb._TRUE_TOKENS))} / "
                        f"少 {sorted(set(sb._TRUE_TOKENS) - f_true)}")
    if f_false != set(sb._FALSE_TOKENS):
        problems.append(f"假值词表不同源：前端多 {sorted(f_false - set(sb._FALSE_TOKENS))} / "
                        f"少 {sorted(set(sb._FALSE_TOKENS) - f_false)}")
    # 调用点哨兵：设置页必须**真的调用** parseCfgBool（`!!e.enabled` 是变异靶子）
    if len(_CALLSITE_RE.findall(html)) < 2:
        problems.append("条目级开关的渲染点没走 parseCfgBool（`parseCfgBool(e.enabled, true)` 少于 2 处）")
    # 第八轮：这条原来写的是 `"parseCfgBool(v, dft)" in html` —— 而 `function parseCfgBool(v, dft){`
    # 这串字**本身就是函数签名**，函数体怎么换它都在（第六轮踩过的同一个坑，这里又踩了一次）。
    # 现在要求「赋值 / 冒号之后出现的那次调用」，签名不再能满足它。
    if not _SITE_BOOL_RE.search(html):
        problems.append("设置页的布尔渲染没走 parseCfgBool（找不到 `: parseCfgBool(v, dft)` 这样的调用）")
    bad = [ln.strip() for ln in html.splitlines()
           if ".checked" in ln and "!!" in ln
           and re.search(r"(cfg\[|e\.enabled|g\.enabled)", ln)]
    if bad:
        problems.append(f"仍有对配置值直接取两次非的行：{bad[:2]}")
    return problems


def audit_parse(html: str, node: str) -> list[str]:
    """② `parseCfgBool` 本体的逐格行为（把函数原文抽出来真跑）。"""
    try:
        bool_src = extract_bool_source(html)
    except AssertionError as exc:
        return [str(exc)]
    cells = build_cells()
    got = run_node(bool_src, cells, node)
    if len(got) != len(cells):
        return [f"node 返回 {len(got)} 格，输入表是 {len(cells)} 格（形状不对）"]
    diff = []
    for c, g in zip(cells, got):
        want = sb.strict_bool(c["v"], c["dft"], logger=_QUIET)
        if bool(g) is not want or (g is not True and g is not False):
            diff.append(f"默认值={c['dft']} / {c['label']}：前端 {g!r} vs 后端 {want!r}")
    if not diff:
        return []
    return [f"逐格比对 {len(diff)}/{len(cells)} 格分叉：" + "；".join(diff[:4])]


def audit_render(html: str, node: str) -> list[str]:
    """③ 设置页**渲染入口**的逐格行为（GPT 第七轮复核 B10）：把循环原文抽出来真跑，
    看开关最终画成什么。

    为什么单列这一层：`parseCfgBool` 本身对，不代表**它被用上了**。
    把 `if(ty==="b") el.checked = … parseCfgBool(v, dft);` 换成 `el.checked = !!v;`，
    函数本体一个字没动、文本哨兵也照样命中（`parseCfgBool(v, dft)` 这串字在别处还在），
    于是整页回退成 `!!v` 而判据全绿 —— 这正是第七轮复核实测出来的盲区。
    """
    cells = build_cells()
    try:
        got = run_render(html, cells, node)
    except AssertionError as exc:
        return [str(exc)]
    if len(got) != len(cells):
        return [f"渲染循环返回 {len(got)} 格，输入表是 {len(cells)} 格（形状不对）"]
    diff = []
    for c, g in zip(cells, got):
        want = sb.strict_bool(c["v"], c["dft"], logger=_QUIET)
        if bool(g) is not want or (g is not True and g is not False):
            diff.append(f"默认值={c['dft']} / {c['label']}：开关画成 {g!r}，应是 {want!r}")
    if not diff:
        return []
    return [f"渲染入口逐格 {len(diff)}/{len(cells)} 格画错：" + "；".join(diff[:4])]


def audit(html: str, node: str) -> list[str]:
    """返回问题清单（空清单 = 干净）。真实页面与病灶样本共用同一套判定。

    三层由弱到强：文本 → 函数本体行为 → **渲染入口行为**。任一层发现问题就算红。
    """
    return audit_text(html) + audit_parse(html, node) + audit_render(html, node)


# ============================ 一、真实页面 ============================

def part_one(node: str) -> None:
    print("=" * 74)
    print("① 真实页面：抽取 parseCfgBool 原文，在 node 里与后端 strict_bool 逐格比对")
    print("=" * 74)
    html = PAGE.read_text(encoding="utf-8")
    cells = build_cells()
    print(f"       输入表 {len(cells)} 格（{len(cells) // 2} 种输入 × 两档默认值；"
          f"词表来自后端 {sorted(sb._TRUE_TOKENS | sb._FALSE_TOKENS)}）")
    try:
        f_true, f_false = extract_tokens(html)
    except AssertionError as exc:
        check("能从 index.html 抽出两份词表", False, str(exc))
        return
    check("能从 index.html 抽出两份词表", True)
    check("★真值词表与后端 _TRUE_TOKENS 逐字同源", f_true == set(sb._TRUE_TOKENS),
          f"前端 {sorted(f_true)} / 后端 {sorted(sb._TRUE_TOKENS)}")
    check("★假值词表与后端 _FALSE_TOKENS 逐字同源", f_false == set(sb._FALSE_TOKENS),
          f"前端 {sorted(f_false)} / 后端 {sorted(sb._FALSE_TOKENS)}")
    try:
        bool_src = extract_bool_source(html)
    except AssertionError as exc:
        check("能抽出 parseCfgBool 原文", False, str(exc))
        return
    check("能抽出 parseCfgBool 原文（含词表常量）",
          "function parseCfgBool(" in bool_src and "typeof v === \"number\"" in bool_src,
          bool_src[:120])

    problems = audit(html, node)
    check("★真实页面零问题（词表同源 + 调用点 + 逐格行为全对）", not problems,
          "；".join(problems[:3]))
    # 单列一条「逐格」的正面证据：两族各挑一格，把数字摆出来
    got = run_node(bool_src, cells, node)
    pairs = {(c["label"], c["dft"]): bool(g) for c, g in zip(cells, got)}
    for label, dft, want in (("字符串 ''", True, False), ("字符串 ''", False, False),
                             ("数字 2", True, True), ("数字 2", False, False),
                             ("词表原样 'y'", True, True), ("带首尾空白 'false'", True, False)):
        check(f"  逐格：{label}（默认值={dft}）→ {want}",
              pairs.get((label, dft)) is want, f"实际 {pairs.get((label, dft))!r}")
    print(f"       共比对 {len(cells)} 格，0 格分叉")
    # ---- 渲染入口（GPT 第七轮复核 B10）：开关**最终画成什么** ----
    try:
        rgot = run_render(html, cells, node)
    except AssertionError as exc:
        check("★能把设置页渲染循环抽出来真跑", False, str(exc))
        return
    rpairs = {(c["label"], c["dft"]): bool(g) for c, g in zip(cells, rgot)}
    for label, dft, want in (("词表原样 'false'", True, False), ("词表原样 'n'", True, False),
                             ("字符串 ''", True, False), ("数字 2", False, False),
                             ("数字 1", False, True), ("字符串 'maybe'", True, True)):
        check(f"  渲染入口：{label}（默认值={dft}）→ 开关 {want}",
              rpairs.get((label, dft)) is want, f"实际 {rpairs.get((label, dft))!r}")
    print(f"       渲染入口逐格 {len(cells)} 格，0 格画错（这三格是 `!!v` 那套画法必错的）")


# ============================ 二、判据有牙 ============================

#: 变异 → （改法, 说明）。改法作用在**页面原文**上，改完再走同一套 audit()。
MUTATIONS = [
    ("调用点改回 `!!e.enabled`",
     lambda h: h.replace("const on = parseCfgBool(e.enabled, true);",
                         "const on = !!e.enabled;"),
     "条目列表的开关又按真值画 → 哨兵必须红"),
    ("函数体整段换成 `return !!v;`",
     lambda h: re.sub(r"function parseCfgBool\(v, dft\)\{.*?\n\}",
                      "function parseCfgBool(v, dft){\n  return !!v;\n}", h, count=1, flags=re.S),
     "P4 要修的 bug 原样装回来 → 逐格必须红"),
    ("删掉数字分支",
     lambda h: h.replace('  if(typeof v === "number") return (v === 0 || v === 1) ? !!v : !!dft;'
                         '   // 数字只认 0 / 1（含 0.0 / 1.0）\n', ""),
     "非 0/1 数字一律 true → 逐格必须红"),
    ("前端词表删掉单字母 `y` / `t`",
     lambda h: h.replace('const CFG_TRUE_TOKENS = new Set(["true", "1", "yes", "on", "y", "t"]);',
                         'const CFG_TRUE_TOKENS = new Set(["true", "1", "yes", "on"]);'),
     "词表靠人列就会漏格 → 对账 + 逐格必须红"),
    ("前端词表删掉空串",
     lambda h: h.replace('const CFG_FALSE_TOKENS = new Set(["false", "0", "no", "off", "n", "f", ""]);',
                         'const CFG_FALSE_TOKENS = new Set(["false", "0", "no", "off", "n", "f"]);'),
     "空串不再按「关」→ 默认值=True 那一档必须红"),
    # v0.24.8 第八轮（GPT 第七轮复核 B10）：**函数本体一个字没动**、文本哨兵照样命中，
    # 整页却回退成 `!!v` —— 只有「真跑渲染循环」那一层判据能抓到。
    ("设置页渲染入口整句换回 `!!v`（B10 盲区）",
     lambda h: h.replace(
         'if(ty==="b") el.checked = (v===undefined||v===null) ? !!dft : parseCfgBool(v, dft);',
         'if(ty==="b") el.checked = !!v;'),
     "函数本体没动、文本哨兵照样命中 → 渲染入口的行为判据必须红"),
    # 这一条才是「行为层」为什么非有不可的证据：**调用还在**、文本层与函数体层都放过，
    # 只有真跑一遍才知道画错了。
    ("渲染入口忽略默认值（null / undefined 一律画「关」）",
     lambda h: h.replace(
         'if(ty==="b") el.checked = (v===undefined||v===null) ? !!dft : parseCfgBool(v, dft);',
         'if(ty==="b") el.checked = (v===undefined||v===null) ? false : parseCfgBool(v, dft);'),
     "调用点还在（文本层放过）、函数体没动（逐格层放过）→ 只有渲染入口的行为判据能红"),
]

#: 渲染入口那一行的原文（变异与阳性对照共用；改实现时这里跟着改，改不动就会红）
_RENDER_LINE = 'if(ty==="b") el.checked = (v===undefined||v===null) ? !!dft : parseCfgBool(v, dft);'


def part_two(node: str) -> None:
    print("\n" + "=" * 74)
    print("② 判据有牙：七种变异必须各自报红，等价改写必须照样绿（阳性对照）")
    print("=" * 74)
    html = PAGE.read_text(encoding="utf-8")
    for name, mutate, why in MUTATIONS:
        mutated = mutate(html)
        check(f"变异真的改到了原文：{name}", mutated != html, "锚点没命中（用例本身要维护）")
        problems = audit(mutated, node)
        check(f"★{name} → 判据报红", bool(problems), f"（{why}）但 audit() 说没问题")
        if problems:
            print(f"       └ 命中：{problems[0][:110]}")
    # 阳性对照：等价改写（`!!dft` → `dft ? true : false`）不许被误判
    same = html.replace("  if(v === null || v === undefined) return !!dft;",
                        "  if(v === null || v === undefined) return dft ? true : false;")
    check("阳性对照：等价改写（`!!dft` → 三元）照样绿", same != html and audit(same, node) == [],
          str(audit(same, node)))
    # 阳性对照 2（第八轮 B10）：渲染入口的等价改写（加括号 / 换 Boolean(dft)）不许被误判 ——
    # 证明这一层判的是**行为**，不是那一行的字面。
    same2 = html.replace(
        _RENDER_LINE,
        'if(ty==="b"){ el.checked = (v === undefined || v === null) ? Boolean(dft) : parseCfgBool(v, dft); }')
    check("阳性对照：渲染入口等价改写（括号 / Boolean(dft)）照样绿",
          same2 != html and audit(same2, node) == [], str(audit(same2, node)))


def main() -> int:
    node = shutil.which("node")
    check("node 可用（缺席即失败，不静默跳过）", bool(node), "PATH 里找不到 node")
    if not node:
        print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
        return 1
    print(f"       node = {node}")
    part_one(node)
    part_two(node)
    print("\n" + "=" * 74)
    print(f"通过 {_PASS} 项，失败 {_FAIL} 项")
    if _FAILED:
        print("失败项：")
        for n in _FAILED:
            print("  -", n)
        return 1
    print("全部通过 ✅ （前端 parseCfgBool 与后端 strict_bool 逐格同源，且判据有牙）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
