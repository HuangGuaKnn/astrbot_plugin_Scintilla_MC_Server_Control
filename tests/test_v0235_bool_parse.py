"""v0.23.5 回归：布尔值必须严格解析，绝不 fail-open。

背景（外部审查指出）：`core/web_api.py` 此前用 `bool(value)` 解析所有布尔设置项
与布尔参数。Python 里 `bool("false")` / `bool("0")` / `bool("no")` **全都是 True**
—— 非前端客户端（脚本、curl、旧页面缓存）提交字符串形式的「关」时，会把开关
**打开**：异地 RCON 模式、多 Agent 工作流与工具位、渐变颜色都可能被意外启用。
方向是「从保守变激进」，属于 fail-open，比报错更危险。

本测试钉住四条契约：

  [1] 取值表：JSON 布尔 / 0·1（含浮点）/ true·false 词表都正确；
      `"false"` `"0"` `"no"` `"off"` 必须得到 **False**（不得沿用 Python 真值语义）
  [2] 无法判定的输入（`"maybe"` / 2 / [] / {}）→ 抛 ValueError，绝不猜
  [3] None 与空串按 False 处理（与旧行为一致，方向保守）
  [4] 四个入口一律走严格解析：save_settings / save_colors / kb toggle / kb approve；
      且源码里不再有「裸 bool() 作用在外部输入上」的写法

其中 [4] 的 save_settings 部分是端到端调用 `_validate_settings`（不需要 AstrBot
运行时，也不触发任何写盘），把「字符串 false 会不会把开关打开」真跑一遍。

运行：
  python tests\\test_v0235_bool_parse.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import (   # noqa: E402
    McControlWebApi,
    parse_bool,
)

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


def raises(fn) -> bool:
    """调用 fn，期望它抛 ValueError。"""
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


class _FakePlugin:
    """`_validate_settings` 只在特定分支用到插件属性；测布尔项用空壳即可。"""


def main() -> int:
    print("================ [1] parse_bool 取值表 ================")

    # ---- 真值 ----
    for v in (True, 1, 1.0, "true", "TRUE", "True", " true ", "1", "yes", "YES",
              "on", "y", "t"):
        check(f"{v!r} -> True", parse_bool(v) is True)

    # ---- 假值（含 Python 真值语义的经典陷阱）----
    for v in (False, 0, 0.0, "false", "FALSE", "False", " false ", "0", "no",
              "NO", "off", "n", "f"):
        check(f"{v!r} -> False", parse_bool(v) is False)

    print("\n================ [2] 无法判定 -> ValueError ================")
    for v in ("maybe", "2", "-1", "null", "nan", 2, -1, [], {}, ["false"], {"a": 1}):
        check(f"{v!r} 抛 ValueError", raises(lambda v=v: parse_bool(v)))

    print("\n================ [3] None / 空串按 False（保守方向） ================")
    check("None -> False", parse_bool(None) is False)
    check('"" -> False', parse_bool("") is False)
    check('"   " -> False（纯空白）', parse_bool("   ") is False)

    print("\n================ [3b] 钉死 Python 的陷阱本身 ================")
    # 如果哪天有人把实现改回 bool(value)，这两条会立刻炸
    check('bool("false") is True（Python 事实，正是危险来源）', bool("false") is True)
    check('parse_bool("false") is False（我们必须与之不同）',
          parse_bool("false") is False)

    print("\n================ [4a] save_settings：字符串 false 不能把开关打开 ================")
    api = McControlWebApi(_FakePlugin())

    parsed, err = api._validate_settings({"remote_rcon_mode": "false"})
    check("未报错", err is None and isinstance(parsed, dict))
    check('"false" 解析为 False（而非 True）', parsed.get("remote_rcon_mode") is False)

    parsed, err = api._validate_settings({"agent_workflow_enabled": "0",
                                          "enable_mc_workflow": "off",
                                          "gradient_enabled": "no"})
    check("未报错", err is None)
    check('"0" -> False', parsed.get("agent_workflow_enabled") is False)
    check('"off" -> False', parsed.get("enable_mc_workflow") is False)
    check('"no" -> False', parsed.get("gradient_enabled") is False)

    parsed, err = api._validate_settings({"remote_rcon_mode": "true"})
    check('"true" -> True', err is None and parsed.get("remote_rcon_mode") is True)

    parsed, err = api._validate_settings({"remote_rcon_mode": True})
    check("JSON 布尔 True 照常", err is None and parsed.get("remote_rcon_mode") is True)

    parsed, err = api._validate_settings({"remote_rcon_mode": False})
    check("JSON 布尔 False 照常", err is None and parsed.get("remote_rcon_mode") is False)

    parsed, err = api._validate_settings({"remote_rcon_mode": "maybe"})
    check("非法值 -> 返回错误而非静默放行", parsed is None and bool(err))
    check("错误信息点名了键名", bool(err) and "remote_rcon_mode" in err)

    print("\n================ [4b] 源码静态检查：不再有裸 bool() 作用在外部输入上 ================")
    src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")

    # 关键：先把 `parse_bool(` 屏蔽掉再查 —— 否则子串会命中自己
    # （"bool(settings[" 是 "parse_bool(settings[" 的后缀，第一版就这么翻过车）
    sanitized = src.replace("parse_bool(", "STRICT_BOOL(")

    forbidden = {
        "bool(settings[": "save_settings 的设置项解析",
        "bool(data[": "save_colors 的入参解析",
        "bool(data.get(": "kb/approve 的入参解析",
        "bool(enabled)": "kb/toggle 的入参解析",
    }
    for pat, label in forbidden.items():
        check(f"源码中不存在 `{pat}`（{label}）", pat not in sanitized)

    for pat in ("parse_bool(settings[", "parse_bool(data[",
                "parse_bool(enabled)", "parse_bool(data.get("):
        check(f"源码中存在 `{pat}`", pat in src)

    # 仍允许 bool() 的地方：读自己配置（值本来就已被校验过）
    allowed = re.findall(r"bool\((self\._cfg|parsed\[[\"']remote_rcon_mode)", src)
    print(f"  （提示）仍保留的 bool(读配置/已校验值) 调用约 {len(allowed)} 处，属预期")

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
