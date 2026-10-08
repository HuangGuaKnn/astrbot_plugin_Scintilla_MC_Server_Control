# -*- coding: utf-8 -*-
"""回归 · `return run` 包装不得绕过权限闸门（GPT 全面复核 R01，v0.24.2 修）。

病灶：`unwrap_command` 原本只解 `execute … run`，于是 `return run <命令>` 的**内层命令**
完全没人看 —— 顶层名字 `return` 是 2 级、又不在危险清单里，解包 / 危险词 / 等级三道关
全部擦身而过，内层 `stop`、`op` 就以 RCON 权限跑掉了（报告的字节码证据：`return run`
会 forward 到 dispatcher root 排队执行）。

修的思路不是加一条文本黑名单（治不了 `execute … run return run …` 的混合嵌套），
而是把 `return run` 并进**同一个递归包装解析器**：内层命令照样受等级 / 危险 / 嵌套深度约束。

判据（报告建议）：纯函数对照 —— 直接命令与它的包装形态必须得到**同一个判定**；
再叠混合嵌套与 fail-closed 边界。全程不执行任何真实命令。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import java_commands as jc                                    # noqa: E402

PASS, FAIL = [], []


def check(label, cond, extra=""):
    if cond:
        PASS.append(label)
        print("[PASS] " + label)
    else:
        FAIL.append(label + (" ｜ " + str(extra) if extra else ""))
        print("[FAIL] " + label + (" ｜ " + str(extra) if extra else ""))


def gate(cmd, is_admin=False, policy="blacklist"):
    """黑名单策略下的闸门判定（None = 放行）。默认非管理员视角。"""
    return jc.check_command(cmd, policy=policy, is_admin=is_admin)


print("=" * 78)
print("v0.24.2 回归 · return run 包装的权限判定（R01）")
print("=" * 78)

# ---- 一、直接命令 vs 包装形态：判定必须一致，且管理类必须被拒 ----
# 用 COMMAND_LEVELS / DANGER_PREFIXES 的真实内容挑样本，别写死猜测。
ADMIN_LEVELS = [n for n, lv in jc.COMMAND_LEVELS.items() if lv >= jc.MIN_ADMIN_LEVEL]
DANGER_WORDS = [p.split(" ")[0] for p in jc.DANGER_PREFIXES]
print("---- 一、包装一致性（表内 level ≥ 3 的命令 + 危险命令）----")
for name in ("stop", "op", "deop", "ban", "kick"):
    if name not in jc.COMMAND_LEVELS and name not in DANGER_WORDS:
        continue
    direct = gate(name + " Knn") if name != "stop" else gate("stop")
    wrapped = gate(("return run " + name + " Knn") if name != "stop" else "return run stop")
    check(f"「{name}」直接被拒", direct is not None, direct)
    check(f"「return run {name}」同样被拒", wrapped is not None, wrapped)

check("表里确实存在 level ≥ 3 的命令（样本有效）", bool(ADMIN_LEVELS), ADMIN_LEVELS[:6])
check("危险前缀非空（样本有效）", bool(DANGER_WORDS), DANGER_WORDS[:6])

# ---- 二、混合嵌套：execute 与 return 互相套 ----
print("\n---- 二、execute / return 混合嵌套 ----")
for cmd in (
    "return run execute as @a run stop",
    "execute as @a run return run stop",
    "execute as @a at @s run return run op Knn",
    "execute if entity @a run return run gamemode spectator @a",
    "return run execute positioned 0 0 0 run stop",
):
    check(f"混合嵌套被拒：{cmd}", gate(cmd) is not None, gate(cmd))

# ---- 三、fail-closed 边界 ----
print("\n---- 三、fail-closed ----")
check("`return run` 空壳被拒", gate("return run") is not None, gate("return run"))
check("嵌套超限被拒（9 层 return run）", gate("return run " * 9 + "stop") is not None)
check("嵌套超限被拒（9 层 execute）", gate("execute run " * 9 + "stop") is not None)
check("解包不出来的 execute 条件被拒",
      gate("execute if data entity @a Foo run stop") is not None)

# ---- 四、正向对照：低等级与无内层的写法不受影响 ----
print("\n---- 四、正向对照（不该误伤）----")
check("give（2 级）放行", gate("give Knn minecraft:stone 1") is None)
check("say（2 级）放行", gate("say hi") is None)
check("`return 1` 无内层命令、放行", gate("return 1") is None)
check("`return fail hi` 无内层命令、放行", gate("return fail hi") is None)
check("`return run say hi` 内层是 2 级、放行", gate("return run say hi") is None)

# ---- 五、管理员与白名单策略不受影响 ----
print("\n---- 五、管理员 / 白名单策略 ----")
check("管理员执行 return run stop 仍放行", gate("return run stop", is_admin=True) is None)
check("白名单下非管理员整体不可用命令工具",
      gate("return run stop", policy="whitelist") is not None)

# ---- 六、守卫有牙：把病灶直接喂给解包器 ----
print("\n---- 六、守卫有牙（直接验解包器）----")
check("解包器认得 return run", jc.unwrap_command("return run stop") == "stop",
      jc.unwrap_command("return run stop"))
check("解包器认得混合嵌套",
      jc.unwrap_command("execute as @a run return run stop") == "stop",
      jc.unwrap_command("execute as @a run return run stop"))
def _raises(exc, fn, *a):
    """fn(*a) 必须抛 exc（用来验 fail-closed 的抛错路径）。"""
    try:
        fn(*a)
    except exc:
        return True
    except Exception:                                                   # noqa: BLE001
        return False
    return False


check("解包器对空壳抛错（不是放行）",
      _raises(jc.CommandParseError, jc.unwrap_command, "return run"))
check("解包器对嵌套超限抛错",
      _raises(jc.CommandParseError, jc.unwrap_command, "return run " * 9 + "stop"))

print("\n================ 汇总 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
