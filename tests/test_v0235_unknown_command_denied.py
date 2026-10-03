"""v0.23.5 回归：黑名单下的「未知命令」必须默认拒绝（fail-closed 第四段）。

背景（外部审查 P0）：`_check_line` 的注释自称「fail-closed 三段式」，
但它 `name, level, _unknown = effective_level(line)` —— **把 unknown 丢掉了**。

于是不在 `COMMAND_LEVELS` 里的命令（模组命令 / 整合包自定义命令 / 比本表
更新的版本命令）一律走 `DEFAULT_LEVEL = 2`，而 `2 < MIN_ADMIN_LEVEL = 3`，
**直接放行**。可偏偏这类命令最不可能被本表覆盖到，`is_danger_command`
也认不出它们 —— 「认不出来就放行」正是 fail-open。

后果：黑名单策略下，模组里任何「改世界、发物品」的命令都对所有玩家敞开，
而这恰恰是整合包服务器上权限等级表永远追不上的部分。

本测试钉住四条：

  [1] 黑名单 + 非管理员 + 未知命令 → **拒绝**，且文案点明原因与解法
  [2] allow_unknown=True → 放行（给需要旧行为的服务器留活路）
  [3] 不误伤：管理员 / 白名单 / 已知命令 / 危险命令 / 高权限命令各走各的路
  [4] 配置链路完整：schema 有键、main 传参、前端有开关、后端白名单已登记

运行：
  python tests\\test_v0235_unknown_command_denied.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import java_commands as jc   # noqa: E402

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool) -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}")


def main() -> int:
    print("================ [1] 未知命令：黑名单下默认拒绝 ================")
    for c in ["tacz reload", "tacz give Steve ak47", "create schematics",
              "ftbquests reload", "jade toggle", "somemod:do_thing"]:
        r = jc.check_command(c, "blacklist", False)
        check(f"拒绝：{c!r}", r is not None)

    r = jc.check_command("tacz reload", "blacklist", False)
    check("文案点明「不在已知命令表里」", "不在已知命令表" in (r or ""))
    check("文案说明「无法判定权限等级」", "无法判定权限等级" in (r or ""))
    check("文案给出解法（允许未知命令 / admin_ids）",
          "允许未知命令" in (r or "") and "admin_ids" in (r or ""))
    check("新文案与 ① 解析失败文案可区分",
          "结构无法安全解析" not in (r or ""))

    print("\n================ [2] allow_unknown=True：恢复旧行为 ================")
    check("tacz reload 放行", jc.check_command("tacz reload", "blacklist", False,
                                             allow_unknown=True) is None)
    check("somemod:do_thing 放行", jc.check_command("somemod:do_thing", "blacklist", False,
                                                  allow_unknown=True) is None)
    check("即便放行未知命令，危险命令仍被拦",
          jc.check_command("stop", "blacklist", False, allow_unknown=True) is not None)
    check("即便放行未知命令，等级≥3 仍被拦",
          jc.check_command("ban Steve", "blacklist", False, allow_unknown=True) is not None)

    print("\n================ [3] 不误伤：各走各的路 ================")
    check("管理员直接放行（不受新闸门影响）",
          jc.check_command("tacz reload", "blacklist", True) is None)
    check("白名单策略下管理员照旧放行",
          jc.check_command("tacz reload", "whitelist", True) is None)
    check("白名单策略下非管理员整体拦下（不是新文案）",
          "白名单" in (jc.check_command("tacz reload", "whitelist", False) or ""))

    for c in ["give Steve diamond 64", "say hi", "time set day", "weather clear",
              "gamemode creative", "summon zombie", "tp Steve 0 0 0"]:
        check(f"已知命令照旧放行：{c!r}", jc.check_command(c, "blacklist", False) is None)

    # ② 段：stop 是纯危险命令
    deny_danger = jc.check_command("stop", "blacklist", False)
    check("危险命令仍走 ② 文案", "属于危险操作" in (deny_danger or ""))

    # ban 既是危险命令（②）又是 3 级命令（③）—— ② 在前，先命中
    deny_ban = jc.check_command("ban Steve", "blacklist", False)
    check("ban 走 ② 危险文案（它同时是危险命令）", "属于危险操作" in (deny_ban or ""))
    check("② 优先于 ④（不是「不在已知命令表」）",
          "不在已知命令表" not in (deny_ban or ""))

    # ③ 段：自适应挑一个「非危险但等级 ≥ 3」的样本，避免写死某个命令的等级
    level_only = [c for c in ("tick freeze", "forceload add 0 0", "difficulty peaceful",
                              "setidletimeout 5", "transfer host 25565", "jfr start",
                              "save-off", "debug start")
                  if not jc.is_danger_command(c)
                  and jc.effective_level(c)[1] >= jc.MIN_ADMIN_LEVEL]
    check("存在「非危险但等级 ≥ 3」的样本（用于验证 ③ 段）", bool(level_only))
    if level_only:
        m = jc.check_command(level_only[0], "blacklist", False)
        check(f"③ 段仍拦下 {level_only[0]!r} 并给出等级文案",
              "需要权限等级" in (m or ""))
        check("③ 段文案不是 ④ 的（未知命令闸门没把它挤掉）",
              "不在已知命令表" not in (m or ""))

    # ① 段：解析失败必须先于 ④ 命中（effective_level 也返回 unknown=True）
    deny_parse = jc.check_command("execute as @a run", "blacklist", False)
    check("解析失败仍走 ① 文案", "结构无法安全解析" in (deny_parse or ""))
    check("① 优先于 ④（不是「不在已知命令表」）",
          "不在已知命令表" not in (deny_parse or ""))

    print("\n================ [4] 配置链路完整 ================")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["permission"]["items"]
    check("schema 里有 allow_unknown_commands", "allow_unknown_commands" in items)
    check("schema 默认 false（fail-closed）",
          items.get("allow_unknown_commands", {}).get("default") is False)
    check("schema 类型是 bool",
          items.get("allow_unknown_commands", {}).get("type") == "bool")

    mainsrc = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("main.py 读取配置并传参",
          'self._cfg("allow_unknown_commands", False)' in mainsrc
          and "allow_unknown=allow_unknown" in mainsrc)

    api = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    check("后端白名单已登记（否则会误报「请重载插件」）",
          '"allow_unknown_commands"' in api)

    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    check("前端 CFG_FIELDS 已绑定",
          '["cfg_allow_unknown","allow_unknown_commands","b",false]' in html)
    check("前端有对应的 DOM 元素", 'id="cfg_allow_unknown"' in html)
    check("白名单策略下该开关会隐藏（只在黑名单下有意义）",
          'id="row_allow_unknown"' in html and "rowAU.style.display" in html)

    jsrc = (PLUGIN_DIR / "core" / "java_commands.py").read_text(encoding="utf-8")
    check("闸门签名带上了 allow_unknown",
          "allow_unknown: bool = False" in jsrc)
    check("注释已从「三段式」更正为「四段式」", "fail-closed 四段式" in jsrc)
    check("unknown 不再被丢弃", "name, level, unknown = effective_level(line)" in jsrc)
    check("DEFAULT_LEVEL 注释已反映新语义（不再说「按 2 级处理」就完事）",
          "默认拒绝" in jsrc)

    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
