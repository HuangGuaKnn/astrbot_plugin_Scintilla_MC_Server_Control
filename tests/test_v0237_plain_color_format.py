# -*- coding: utf-8 -*-
"""v0.23.7 · B2④ 单色颜色格式下拉框（hex 默认 / named 固定格式化色）。

运行：
  <python> tests/test_v0237_plain_color_format.py

由来（2026-10-05 · 用户第四回合设计拍板）
========================================
用户接手 B2③ 的自动门控后给出更可控的方案：

  1. 「MC 格式化符号（§0-§f）种类是少点，但能**清清楚楚保留自定义颜色**」
     → 顺手把 ``§6`` / ``&6`` 做成**可接受的输入简写**（解析成 16 色名，全世代通用、零近似）；
  2. 「不开渐变时：默认 hex，回退用固定格式化颜色；并且像渐变格式那样给个下拉框，
     把新版 / 旧版（含版本号）标清楚」
     → 新增 ``plain_color_format``：

        ``hex``（默认）  精确 ``#RRGGBB``｜客户端 1.16+；已知旧版**自动回退**固定格式化色
        ``named``        固定格式化色·16 色名（§0-§f 同款）｜全世代通用；填 hex 自动近似

本测试钉死五件事
================
1. ``§``/``&`` 色码解析：16 码全对、大小写/空白容错、非法一律 None；
2. 裸数字绝不被 § 码抢走（十进制 RGB 不许回归）；
3. 单色裁决四象限：hex×新版=精确 / hex×旧版=回退 / named×新版=强制近似 / named×命名色=原样；
4. 旧版链不回归：1.8.9 洗白 + named 模式仍同时成立；
5. 注册链路（源码扫描）：后端枚举 / GET / save / schema / 两张前端页面。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

_fail: list[str] = []
_pass = 0
_skip: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


def skip(name: str, why: str) -> None:
    _skip.append(name)
    print(f"[SKIP] {name}  <- {why}")


def _load_tob():
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control.core import text_outbound as tob
    except Exception:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "text_outbound_standalone", PLUGIN_DIR / "core" / "text_outbound.py")
        tob = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tob)  # type: ignore[union-attr]
    return tob


tob = _load_tob()


def part_one() -> None:
    print("---- 一、§/& 格式化符号色码 ----")
    expect = {"0": "black", "1": "dark_blue", "2": "dark_green", "3": "dark_aqua",
              "4": "dark_red", "5": "dark_purple", "6": "gold", "7": "gray",
              "8": "dark_gray", "9": "blue", "a": "green", "b": "aqua",
              "c": "red", "d": "light_purple", "e": "yellow", "f": "white"}
    bad = []
    for code, name in expect.items():
        for form in (f"§{code}", f"&{code}", f"§{code.upper()}", f" &{code.upper()} "):
            got = tob.parse_section_color(form)
            if got != name:
                bad.append((form, got, name))
    check(f"16 码 × §与& × 大小写/空白 共 {16 * 4} 态全部解析正确", not bad, str(bad[:3]))
    check("色码表就是 16 颗", len(tob.SECTION_COLOR_CODES) == 16)

    print("---- 二、非法输入与「裸数字不许抢」 ----")
    for v in ["", None, "§", "&", "§g", "§66", "6", "66", "#FF0000", "gold", "红"]:
        check(f"非法/非色码 {v!r} → None", tob.parse_section_color(v) is None)
    check("裸 '6' 不是色码（十进制 RGB 安全）", tob.parse_section_color("6") is None)


def make_plugin(pm, plain="hex", mc=(1, 13, 2), say="#FFD700", gradient=False):
    m = pm.McControlPlugin.__new__(pm.McControlPlugin)
    m.config = {"appearance": {
        "gradient_enabled": gradient,
        "gradient_format": "json",
        "gradient_colors_say": "#FF0000,#00FF00",
        "color_say": say,
        "plain_color_format": plain,
    }}
    m._cfg_index = {k: "appearance" for k in (
        "gradient_enabled", "gradient_format", "gradient_colors_say",
        "color_say", "plain_color_format")}
    m.logger = logging.getLogger("t0237plain")
    from astrbot_plugin_Scintilla_MC_Server_Control.core.version_caps import VersionInfo
    m._resolve_version_info = lambda: VersionInfo(mc=mc, raw="", source="detected")
    return m


def color_of(m, text="你好"):
    return json.loads(
        m._colored_payload(text, "color_say", "gradient_colors_say", "white")
    ).get("color")


def part_three(pm) -> None:
    print("---- 三、单色裁决四象限 + § 简写 ----")
    check("PLAIN_FORMATS 就是 hex / named",
          set(pm.McControlPlugin.PLAIN_FORMATS) == {"hex", "named"},
          str(list(pm.McControlPlugin.PLAIN_FORMATS)))
    check("格式标签带版本号（1.16+ / 全世代）",
          "1.16+" in pm.McControlPlugin.PLAIN_FORMATS["hex"]
          and "全世代" in pm.McControlPlugin.PLAIN_FORMATS["named"])

    # hex（默认）
    check("hex × 1.13.2 → 回退固定格式化色 gold",
          color_of(make_plugin(pm, "hex", (1, 13, 2))) == "gold")
    check("hex × 1.16.5 → 精确 hex",
          color_of(make_plugin(pm, "hex", (1, 16, 5))) == "#FFD700")
    check("hex × 1.21.11 → 精确 hex",
          color_of(make_plugin(pm, "hex", (1, 21, 11))) == "#FFD700")
    check("hex × 版本未知 → 原样（不猜）",
          color_of(make_plugin(pm, "hex", None)) == "#FFD700")
    # named
    check("named × 1.21.11 → 强制近似（不外泄 hex）",
          color_of(make_plugin(pm, "named", (1, 21, 11))) == "gold")
    check("named × 1.13.2 → 近似 dark_blue",
          color_of(make_plugin(pm, "named", (1, 13, 2), say="#123456")) == "dark_blue")
    check("named × 命名色 → 原样",
          color_of(make_plugin(pm, "named", (1, 13, 2), say="aqua")) == "aqua")
    # § / & 简写：解析成命名色 → 两种格式、任意版本一律原样
    for plain in ("hex", "named"):
        for mc in ((1, 13, 2), (1, 21, 11)):
            check(f"{plain} × {mc} × §6 → gold",
                  color_of(make_plugin(pm, plain, mc, say="§6")) == "gold")
    check("&a → green", color_of(make_plugin(pm, "hex", (1, 13, 2), say="&a")) == "green")

    print("---- 四、_parse_color 全格式回归（别被 § 码抢） ----")
    P = pm.McControlPlugin._parse_color
    check("hex", P("#FFD700") == "#FFD700")
    check("hex 无井号", P("ffd700") == "#FFD700")
    check("RGB 三元组", P("255,215,0") == "#FFD700")
    check("rgb() 写法", P("rgb(255,215,0)") == "#FFD700")
    check("十进制 RGB（裸数字没被抢）", P("16711680") == "#FF0000" and P("6") == "#000006")
    check("命名色", P("gold") == "gold")
    check("§ 码", P("§6") == "gold" and P("§F") == "white")
    check("& 码", P("&a") == "green")
    check("非法 → 回退", P("乱码", "white") == "white")

    print("---- 五、旧版链不回归（1.8.9 洗白 + named 同时成立） ----")
    m8 = make_plugin(pm, "named", (1, 8, 9), say="#00AABB")
    cmd = m8._build_text_cmds("甲（乙", "tellraw @a ", "color_say",
                              "gradient_colors_say", "white")[0]
    obj = json.loads(cmd[len("tellraw @a "):])
    check("1.8.9：括号已洗白", "甲(乙" in obj.get("text", "") and "（" not in cmd)
    check("1.8.9 + named：颜色已近似 dark_aqua", obj.get("color") == "dark_aqua")


def part_six() -> None:
    print("---- 六、注册链路（源码扫描） ----")
    wapi = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    mainpy = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    page = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")

    check("后端枚举白名单登记 plain_color_format",
          '"plain_color_format": ("hex", "named"),' in wapi)
    check("GET /colors 增加 plain 块",
          '"plain": {' in wapi and 'self._cfg("plain_color_format", "hex")' in wapi)
    check("save_colors 校验 + 回显 plain",
          'if "plain_color_format" in data:' in wapi and "plain_now" in wapi
          and "未知的单色格式" in wapi)
    check("颜色解析报错文案提到格式化符号",
          "格式化符号（§6 或 &6）" in wapi)
    check("main.py 定义 PLAIN_FORMATS 且裁决读取配置",
          "PLAIN_FORMATS = {" in mainpy
          and 'self._cfg("plain_color_format", "hex")' in mainpy
          and "[颜色格式] 单色格式=固定格式化色" in mainpy)
    check("main.py import 了 parse_section_color", "parse_section_color," in mainpy)

    schema = json.load(open(PLUGIN_DIR / "_conf_schema.json", encoding="utf-8-sig"))
    entry = None

    def walk(o):
        nonlocal entry
        if isinstance(o, dict):
            if "plain_color_format" in o:
                entry = o["plain_color_format"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(schema)
    check("schema 登记（string / options=hex,named / 默认 hex）",
          bool(entry) and entry.get("type") == "string"
          and entry.get("options") == ["hex", "named"]
          and entry.get("default") == "hex", str(entry)[:110])
    check("schema 三项颜色 hint 提到 §6 / &6",
          (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig")
          .count("§6 或 &6") >= 1)
    check("前端颜色卡有单色格式下拉 + 版本提示",
          'id="clr_plain_fmt"' in page and "hex 需客户端 1.16+ · 旧版自动回退" in page)
    check("前端设置页镜像下拉 + CFG_FIELDS/S_DEFAULTS 登记",
          'id="cfg_plain_fmt"' in page
          and '["cfg_plain_fmt","plain_color_format","s"]' in page
          and '"cfg_plain_fmt": "hex",' in page)
    check("前端 PLAIN_LABELS 带版本号 + 载荷/回显/重置齐全",
          "const PLAIN_LABELS" in page and "1.16+" in page
          and "payload.plain_color_format = $(\"clr_plain_fmt\").value;" in page
          and '$("clr_plain_fmt").value = "hex";' in page
          and '$("clr_plain_fmt").value = (r.plain||{}).format || "hex";' in page)
    check("渐变格式标签也补了版本号",
          '"json": "Vanilla（JSON 文本组件）｜客户端 1.16+"' in mainpy
          and "客户端 1.16+" in page)


def main() -> int:
    part_one()
    pm = None
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control import main as pm  # noqa: F811
    except Exception as e:
        skip("插件层（三/四/五）", f"缺 AstrBot 运行时：{type(e).__name__}: {e}")
    if pm is not None:
        part_three(pm)
    part_six()

    print("\n==========================================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项，跳过 {len(_skip)} 项")
    if _fail:
        for f in _fail:
            print("  [FAIL]", f)
        return 1
    print("全部通过 ✅ （单色格式下拉 + §/& 色码简写：默认 hex，旧版自动回退固定格式化色）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
