# -*- coding: utf-8 -*-
"""设置页「留空 = 不提交该键」的行为判据（v0.24.8 · GPT 第三轮复核证据缺口 B6）。

为什么要有这一支
----------------
v0.24.7 的「三键同源」用例里，(e) 段只做了**字符串检查**：
`"cfg_wf_max" not in S_DEFAULTS`。它能拦住「把假默认值塞进 S_DEFAULTS」，
却拦不住把 `collectSettings()` 里的

    else if (ty === "n") { const x = parseFloat(el.value); if (!isNaN(x)) s[key] = x; }

改成

    else if (ty === "n") { const x = parseFloat(el.value); s[key] = isNaN(x) ? 0 : x; }

后者照样让 113 项全绿，而实际行为已经变成「留空提交 0」—— 用户的空输入会
把内置默认值覆盖成 0。GPT 第三轮复核点名的就是这一条：判据读的是文本，不是行为。

本支怎么判
----------
从 `pages/mc_control/index.html` 里**抽取真实的 `collectSettings()` 函数源码**
（不是复制一份到用例里 —— 复制会和实现漂移），套上最小 DOM 桩，在 node 里真的
跑一遍，只看它**返回了什么对象**：

* 数字框留空 / 乱填 → 返回对象里**没有**这个键（不是 0，也不是 null）；
* 数字框填 `0` → 必须有，且等于 0（合法值不许被当成空）；
* 数字框填 `12.5` → 必须有，且等于 12.5；
* 只写字段留空 → 没有这个键；
* 布尔框 → 跟着 `checked` 走；
* 列表字段 → 按中英文逗号切开、去空白、滤空项。

依赖：node（GitHub Actions 的 ubuntu-latest 自带；本机亦已装）。
node 缺席时**显式失败**，不静默跳过 —— 「跑不动」不能伪装成「通过了」。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "mc_control" / "index.html"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        _FAILED.append(name)
        print(f"[FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def _extract_function(html: str, name: str) -> str:
    """按花括号配对抽取 `function <name>(...){...}` 的完整源码。

    只用于 `collectSettings` 这种体内没有「字符串里的花括号 / 正则里的花括号」的函数；
    抽不出来就直接抛错（宁可红，也不许悄悄拿一份空函数去判绿）。
    """
    marker = f"function {name}("
    start = html.find(marker)
    if start < 0:
        raise AssertionError(f"index.html 里找不到 {marker}")
    brace = html.find("{", start)
    if brace < 0:
        raise AssertionError(f"{marker} 后面没有函数体")
    depth = 0
    for i in range(brace, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"{marker} 的花括号没有配对")


#: 判据用的桩（与 index.html 里的真实常量同名，形状最小化）
_HARNESS = r"""
const fs = require("fs");
const page = process.argv[2];
const fnSrc = fs.readFileSync(process.argv[3], "utf8");

// —— 最小 DOM 桩 —
const el = (value, checked) => ({ value: value, checked: !!checked });
const ELS = {
  cfg_num_empty:   el(""),
  cfg_num_blank:   el("   "),
  cfg_num_word:    el("abc"),
  cfg_num_zero:    el("0"),
  cfg_num_float:   el("12.5"),
  cfg_bool_on:     el("", true),
  cfg_bool_off:    el("", false),
  cfg_list:        el(" a,，b ,, c "),
  cfg_text_empty:  el("   "),
  cfg_text_filled: el(" hello "),
  cfg_path_empty:  el("  "),
  cfg_path_filled: el(" /srv/mc "),
};
const $ = (id) => ELS[id] || null;

const CFG_FIELDS = [
  ["cfg_num_empty",   "num_empty",   "n"],
  ["cfg_num_blank",   "num_blank",   "n"],
  ["cfg_num_word",    "num_word",    "n"],
  ["cfg_num_zero",    "num_zero",    "n"],
  ["cfg_num_float",   "num_float",   "n"],
  ["cfg_bool_on",     "bool_on",     "b"],
  ["cfg_bool_off",    "bool_off",    "b"],
  ["cfg_list",        "list_key",    "l"],
  ["cfg_text_empty",  "text_empty",  "t"],
  ["cfg_text_filled", "text_filled", "t"],
  ["cfg_path_empty",  "path_empty",  "p"],
  ["cfg_path_filled", "path_filled", "p"],
];
// S_DEFAULTS 是以 **DOM id** 为键的（collectSettings 里写的是 S_DEFAULTS[id]），
// 桩必须照这个形状来，否则这条判据测的就不是实现了。
const S_DEFAULTS = { cfg_text_empty: "内置默认" };
const TGT_KEYS = {};
const TGT_STATE = {};
const collectTargetEvents = () => [];

eval(fnSrc + "\n;globalThis.__collect = collectSettings;");
const out = globalThis.__collect();
process.stdout.write(JSON.stringify(out));
"""


def main() -> int:
    print("=" * 68)
    print("① 设置页「留空 = 不提交该键」行为判据（抽取真实 collectSettings 跑）")
    print("=" * 68)

    node = shutil.which("node")
    check("node 可用（缺席即失败，不静默跳过）", bool(node), "PATH 里找不到 node")
    if not node:
        print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
        return 1

    html = PAGE.read_text(encoding="utf-8")
    try:
        fn_src = _extract_function(html, "collectSettings")
    except AssertionError as exc:
        check("能从 index.html 抽出 collectSettings() 源码", False, str(exc))
        print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
        return 1
    check("能从 index.html 抽出 collectSettings() 源码", True)
    check("抽取到的是函数本体（含数字分支）", 'parseFloat' in fn_src, fn_src[:120])

    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "harness.js"
        fnsrc = Path(td) / "fn.js"
        js.write_text(_HARNESS, encoding="utf-8")
        fnsrc.write_text(fn_src, encoding="utf-8")
        r = subprocess.run([node, str(js), str(PAGE), str(fnsrc)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)

    if r.returncode != 0:
        check("node 跑通 collectSettings()", False,
              (r.stderr or r.stdout or "")[-400:])
        print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
        return 1
    check("node 跑通 collectSettings()", True)

    out = json.loads(r.stdout)

    # —— 数字：空 / 空白 / 乱填一律「不提交该键」——
    for key, label in (("num_empty", "空字符串"), ("num_blank", "纯空白"),
                       ("num_word", "非数字文本")):
        check(f"★数字框{label} → 返回对象里没有 \"{key}\"（留空不许写成 0）",
              key not in out, f"实际: {out.get(key)!r}")
    check("★数字框填 \"0\" → 必须提交，且等于 0（合法值不许被当成空）",
          out.get("num_zero") == 0 and isinstance(out.get("num_zero"), (int, float)),
          f"实际: {out.get('num_zero')!r}")
    check("数字框填 \"12.5\" → 提交 12.5",
          out.get("num_float") == 12.5, f"实际: {out.get('num_float')!r}")

    # —— 布尔 / 列表 / 文本 / 只写字段 ——
    check("布尔框跟着 checked 走（true）", out.get("bool_on") is True, repr(out.get("bool_on")))
    check("布尔框跟着 checked 走（false）", out.get("bool_off") is False, repr(out.get("bool_off")))
    check("列表字段按中英文逗号切开并去空白 / 滤空项",
          out.get("list_key") == ["a", "b", "c"], repr(out.get("list_key")))
    check("普通文本留空 → 回落到 S_DEFAULTS 的内置默认值",
          out.get("text_empty") == "内置默认", repr(out.get("text_empty")))
    check("普通文本填了 → 去首尾空白后提交",
          out.get("text_filled") == "hello", repr(out.get("text_filled")))
    for key in ("path_empty", "path_filled"):
        expect = key == "path_filled"
        check(f"只写字段{'填了 → 提交' if expect else '留空 → 不提交该键'} "
              f"（\"{key}\"）",
              (key in out) is expect,
              f"实际: {out.get(key)!r}")

    print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        print("失败项：")
        for n in _FAILED:
            print("  -", n)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
