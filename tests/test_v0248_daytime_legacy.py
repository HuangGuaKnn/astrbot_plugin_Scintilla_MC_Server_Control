# -*- coding: utf-8 -*-
"""v0.24.8 回归（2026-10-21 真机首日）：`time query daytime` 在旧版服务端上的回包。

怎么发现的
==========
真机验收第一台就点火了 **1.7.10-forge**（MC 25612 / RCON 25712）。`mc_server_status`
返回的是：

    There are 0/20 players online:
    /time <set|add> <value>

—— 第二行是服务器的**用法提示**，不是时间。根因很单纯：`time query` 是 **1.13+** 才有的
子命令，而 `mc_server_status`（`main.py`）与 WebUI 的「服务器状态」卡（`core/web_api.py`）
都直接把回包拼出去。

真机原始回包（`取证_真机_1.7.10_原始回包_20261021.log`，直连 RCON 抓的）：

====================  ====================================================
命令                  1.7.10 回包
====================  ====================================================
`list`                ``There are 0/20 players online:``
`tellraw @a {...}`    ``''``（空串）
`say ...`             ``''``（空串）
`title @a title ...`  ``Unknown command. Try /help for a list of commands``
`time query daytime`  ``/time <set|add> <value>``
`time set day`        ``Set the time to 1000``
`give 不存在的玩家`    ``That player cannot be found``
====================  ====================================================

判据
====
1. **只有整条回包就是一个 0..23999 的数字**才算 tick；用法文本 / `Unknown command` / 空串一律 None
   （拿不到就说「这个版本没这个能力」，不装懂）；
2. 拿不到时渲染出来的一行里**不许出现 tick 数字**，而且要说明原因；
3. 两个入口（工具与 WebUI）都必须走这一层 —— 由真机复测作为最终证据（本节只钉纯函数）。

运行：<AstrBot python> tests\\test_v0248_daytime_legacy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (  # noqa: E402
    format_daytime_line,
    parse_daytime_reply,
)

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


#: 2026-10-21 1.7.10-forge 真机抓到的原始回包 —— 判据直接吃它们
NOT_A_TIME = (
    "/time <set|add> <value>",                                        # 1.7.10 用法提示
    "Unknown command. Try /help for a list of commands",              # Paper/Spigot 等
    "",
    "   ",
    "1000 ticks",                                                     # 不是「整条就是数字」
    "daytime = 1000",
    "Usage: /time <add|query|set> <value>",
)


def part_one() -> None:
    print("---- 一、拿不到就说拿不到（真机原始回包）----")
    bad = [r for r in NOT_A_TIME if parse_daytime_reply(r) is not None]
    check(f"★{len(NOT_A_TIME)} 条「不是时间」的回包全部返回 None", not bad, str(bad))
    check("★1.7.10 的用法提示被识别为「拿不到」",
          parse_daytime_reply("/time <set|add> <value>") is None)
    check("空串 / None / 空白都返回 None",
          parse_daytime_reply("") is None and parse_daytime_reply(None) is None
          and parse_daytime_reply("  ") is None)


def part_two() -> None:
    print("\n---- 二、真数字要认（0..23999）----")
    for raw, want in (("1000", 1000), (" 23999 ", 23999), ("0", 0), ("21000", 21000)):
        got = parse_daytime_reply(raw)
        check(f"parse_daytime_reply({raw!r}) == {want}", got == want, repr(got))
    out_of_range = [(r, parse_daytime_reply(r)) for r in ("24000", "-1", "999999")]
    check("★越界（24000 / -1 / 999999）一律 None", all(v is None for _, v in out_of_range),
          str(out_of_range))


def part_three() -> None:
    print("\n---- 三、渲染：拿不到时不许出现 tick 数字，而且要说明原因 ----")
    line_none = format_daytime_line(None)
    check("★拿不到时说明「该版本不支持 time query（1.13+）」",
          "不支持" in line_none and "1.13" in line_none, line_none)
    check("★拿不到时**不出现**「/time」用法文本（那正是真机踩到的样子）",
          "/time" not in line_none, line_none)
    line_1000 = format_daytime_line(1000)
    check("tick 1000 → 含 1000 与 07:00（MC 0 tick = 06:00）",
          "1000" in line_1000 and "07:00" in line_1000, line_1000)
    line_0 = format_daytime_line(0)
    check("tick 0 → 06:00", "06:00" in line_0, line_0)


def part_four() -> None:
    print("\n---- 四、接线：两个入口都要走这一层 ----")
    tool_src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    api_src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    check("★mc_server_status 用 parse_daytime_reply/format_daytime_line",
          "parse_daytime_reply(" in tool_src and "format_daytime_line(" in tool_src)
    check("★WebUI 的 get_server_status 用 parse_daytime_reply",
          "parse_daytime_reply(" in api_src)
    check("两个入口都不再把 time query 的原样回包直接拼出去",
          'info["daytime"] = (await rcon.command("time query daytime")).strip()' not in api_src
          and "day = await rcon.command(\"time query daytime\")" not in tool_src)


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 真机：time query 在 1.7.10 上回的是用法提示")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （旧版服务端不会再吐一行「用法提示」冒充时间）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
