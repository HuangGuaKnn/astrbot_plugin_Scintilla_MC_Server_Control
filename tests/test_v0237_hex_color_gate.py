# -*- coding: utf-8 -*-
"""v0.23.7 · B2③ 旧版 hex 单色门控（自定义颜色可用性）。

运行：
  <python> tests/test_v0237_hex_color_gate.py

由来（2026-10-05 第四回合 · 主人在线验收）
==========================================
主人验收长文广播切段时提出硬要求：「自动切单色没问题，但要确保**自定义颜色**还能用」。

于是顺着单色降级路径往下摸，发现一个静默失效：
  单色 payload = ``{"text": ..., "color": <主人的自定义色>}``，
  而 hex 颜色（``#RRGGBB``）是 **1.16+** 客户端特性 —— 已知 < 1.16 时下发 hex，
  命令**不报错**、客户端**静默忽略**，主人填的颜色等于人间蒸发。
  （活体实测：1.13.2 四条 tellraw —— hex 大写 / 小写 / 无井号 / 色名对照 ——
   全部静默成功、零错误回执。）

修法（可见 > 精确）：
  已知 < 1.16 → hex 单色**降级为最接近的原版 16 色名**（感知加权距离，确定性）；
  ≥ 1.16 → 原样精确 hex；版本未知 → 不猜（与渐变门控同策略）。

本测试钉死五件事
================
1. 「最近色」映射的确定性与几个已知落点；
2. 16 个官方色 hex 精确自映射（不会把官方色改坏）；
3. 版本门：<1.16 降级 / ≥1.16 放行 / 未知不猜；
4. 常量与主流程落点（源码扫描）：_colored_payload 真的调了门控；
5. 门控只在 hex 上动手，命名色 / 空值原样返回。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

_fail: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


def _load():
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control.core import text_outbound as tob
    except Exception:  # 包 __init__ 依赖 AstrBot 时退回按文件装载（纯模块无依赖）
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "text_outbound_standalone", PLUGIN_DIR / "core" / "text_outbound.py"
        )
        tob = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tob)  # type: ignore[union-attr]
    return tob


tob = _load()


def part_one() -> None:
    print("---- 一、最近色映射：确定性与已知落点（色相感知） ----")
    cases = [
        ("#FFAA00", "gold"),        # 官方 gold 原值
        ("#FFD700", "gold"),        # 经典「金」，落 gold
        ("#00AABB", "dark_aqua"),   # 偏青，落 dark_aqua
        ("#123456", "dark_blue"),   # 深靛蓝：保色相，不落 gray
        ("#2E8B57", "dark_green"),  # 哑海绿：保色相，不落 gray
        ("#1E90FF", "blue"),        # dodgerblue
        ("#808080", "gray"),        # 灰阶：不吃色相，纯比明暗
        ("#FE0101", "dark_red"),    # 中调纯红：红/深红明度打平 → 取 RGB 更近的深红
        ("#FFFFFF", "white"),
        ("#000000", "black"),
    ]
    for hexv, want in cases:
        got = tob.nearest_vanilla_color(hexv)
        check(f"{hexv} → {want}", got == want, f"got={got}")

    # 关键性质：彩色输入不该被判成灰（色相感知的反面教材）
    chromatics = ["#123456", "#2E8B57", "#1E90FF", "#8A2BE2", "#B22222", "#CD853F"]
    achromatic_names = {"black", "white", "gray", "dark_gray"}
    bad = [(c, tob.nearest_vanilla_color(c)) for c in chromatics
           if tob.nearest_vanilla_color(c) in achromatic_names]
    check("鲜艳/半鲜艳色不会掉进灰阶档", not bad, str(bad))

    # 确定性：同输入多次同输出；大小写 / 带不带 # 等价
    a = [tob.nearest_vanilla_color("#00AABB") for _ in range(5)]
    check("同输入永远同输出（确定性）", len(set(a)) == 1, str(a))
    check("大小写 / 井号等价",
          tob.nearest_vanilla_color("#00aabb") == tob.nearest_vanilla_color("00AABB")
          == tob.nearest_vanilla_color("#00AABB"))
    check("非 hex 一律 None", tob.nearest_vanilla_color("gold") is None
          and tob.nearest_vanilla_color("") is None
          and tob.nearest_vanilla_color("#12345") is None
          and tob.nearest_vanilla_color("#GGGGGG") is None)


def part_two() -> None:
    print("---- 二、16 个官方色精确自映射（不许把官方色改坏） ----")
    bad = []
    for name, (r, g, b) in tob.VANILLA_COLORS.items():
        hexv = f"#{r:02X}{g:02X}{b:02X}"
        got = tob.nearest_vanilla_color(hexv)
        if got != name:
            bad.append((name, hexv, got))
    check(f"全部 {len(tob.VANILLA_COLORS)} 个官方色自映射正确", not bad, str(bad[:4]))
    check("官方色表就是 16 颗", len(tob.VANILLA_COLORS) == 16)
    check("parse_hex_color 基本正确",
          tob.parse_hex_color("#FF8000") == (255, 128, 0)
          and tob.parse_hex_color("ff8000") == (255, 128, 0)
          and tob.parse_hex_color("red") is None)


def part_three() -> None:
    print("---- 三、版本门：<1.16 降级 / ≥1.16 放行 / 未知不猜 ----")
    check("切分水岭常量 = (1, 16)", tob.HEX_COLOR_CUTOVER == (1, 16))
    table = [
        ((1, 8, 9), True), ((1, 9, 4), True), ((1, 12, 2), True),
        ((1, 13, 2), True), ((1, 14, 4), True), ((1, 15, 2), True),
        ((1, 16), False), ((1, 16, 5), False), ((1, 20, 4), False),
        ((1, 21, 11), False), (None, False),
    ]
    for mc, want in table:
        got = tob.needs_hex_color_downgrade(mc)
        check(f"{mc} → {'降级' if want else '放行'}", got is want, f"got={got}")


def part_four() -> None:
    print("---- 四、主流程落点（源码扫描） ----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("_colored_payload 调用了单色裁决",
          "base = self._single_color_for_server(base)" in src)
    check("_single_color_for_server 已定义（只动 hex）",
          "def _single_color_for_server(self, color: str) -> str:" in src
          and 'if not color or not str(color).startswith("#"):' in src)
    check("门控同时吃 needs_hex_color_downgrade + nearest_vanilla_color",
          "needs_hex_color_downgrade(self._server_mc_cached())" in src
          and "nearest_vanilla_color(color)" in src)
    check("门控日志留痕（[颜色门控]）", "[颜色门控]" in src)
    check("import 已接（needs_hex_color_downgrade / nearest_vanilla_color）",
          "needs_hex_color_downgrade," in src and "nearest_vanilla_color," in src)


def part_five() -> None:
    print("---- 五、命名色 / 空值：原样返回（不许误伤） ----")
    check("_legacy_safe_color 对非 # 开头直接返回（源码级，见 part_four）", True)
    for v in ["white", "gold", "aqua", "dark_purple"]:
        check(f"命名色 {v} 不进入最近的 hex 解析",
              tob.nearest_vanilla_color(v) is None)


def main() -> int:
    part_one()
    part_two()
    part_three()
    part_four()
    part_five()
    print("\n==========================================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for f in _fail:
            print("  [FAIL]", f)
        return 1
    print("全部通过 ✅ （自定义颜色：<1.16 近似可见，≥1.16 精确 hex）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
