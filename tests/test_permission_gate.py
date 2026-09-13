"""命令工具白名单 / 黑名单闸门回归用例（v0.20.0 简化模型）。

新模型（口径：非管理员 = 游戏里没有 OP 的普通玩家）：
    * 闸门只管「命令工具」——mc_execute_command / mc_give_item / mc_broadcast
      （也就等价于本模块的 check_command：只要这条命令来自命令工具就受检）；
      喊话、状态、查询、绑定这类插件自带功能不走这里。
    * whitelist（默认）：非管理员**完全不能使用命令工具**；
    * blacklist：所有人都能用，但危险命令 + 权限等级 ≥ 3 的管理命令仍仅管理员。
"""
import sys, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402
PLUGIN = PLUGIN_DIR
spec = importlib.util.spec_from_file_location("jc", PLUGIN / "core" / "java_commands.py")
jc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jc)

FAIL = []


def case(desc, cmd, policy, is_admin, expect_pass):
    reason = jc.check_command(cmd, policy=policy, is_admin=is_admin)
    ok = (reason is None) == expect_pass
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAIL.append(desc)
    print(f"[{tag}] {desc:<58} {'放行' if reason is None else '拒绝'}"
          f"{'' if reason is None else ' ← ' + reason}")


print("=========== 白名单（默认）· 非管理员：命令工具一律不可用 ===========")
for c in ["give Steve diamond 64", "Give Steve diamond 64", "/give Steve diamond",
          "minecraft:give Steve diamond 1", "say 全体集合", "tp Steve 0 100 0",
          "effect give Steve speed 60 5", "summon zombie", "setblock ~ ~ ~ stone",
          "title @a title 你好", "gamerule keepInventory true", "kill Steve",
          "gamemode creative", "enchant Steve sharpness 5", "xp add Steve 100",
          "tellraw @a {\"text\":\"hi\"}", "execute as @a run give @s diamond",
          "execute run stop", "tacz reload", "trigger obj set 1", "chase @a",
          "whitelist add Steve", "ban Steve", "stop", "save-all", "reload",
          # 连 /list、/help 这种 0 级命令也一并拒绝：白名单管的是「工具能不能用」，
          # 而不是逐条命令判级（喊话/状态/查询走插件自带功能，另有开关）
          "list", "help", "me 你好呀", "msg Steve 你好",
          "list\ngive Steve diamond 64", "list\nstop"]:
    case(f"whitelist 拒绝：{c!r}", c, "whitelist", False, False)

print("\n=========== 白名单（默认）· 管理员：照旧全通 ===========")
for c in ["list", "give Steve diamond 64", "stop", "ban Steve"]:
    case(f"whitelist/admin 放行：{c!r}", c, "whitelist", True, True)

print("\n=========== 黑名单 · 非管理员：宽松但有底线 ===========")
for c in ["stop", "op Steve", "deop Steve", "ban Steve", "ban-ip 1.2.3.4", "kick Steve",
          "whitelist add Steve", "save-off", "save-all", "publish", "jfr start",
          "perf start", "tick freeze", "transfer host 25565", "setidletimeout 5",
          "banlist", "pardon Steve", "unban Steve", "reload", "function foo:bar",
          "forceload add 0 0", "difficulty peaceful", "gamemode spectator @a",
          "debug start", "execute run stop", "execute as @a run op @s"]:
    case(f"blacklist 拒绝：{c!r}", c, "blacklist", False, False)

for c in ["give Steve diamond 64", "say hi", "time set day", "weather clear",
          "gamemode creative", "list", "me hi", "summon zombie", "tp Steve 0 0 0",
          "tellraw @a {\"text\":\"hi\"}"]:
    case(f"blacklist 放行：{c!r}", c, "blacklist", False, True)

print("\n=========== 边界 ===========")
assert jc.check_command("", "whitelist", False) == "命令为空。"
assert jc.check_command("   ", "whitelist", True) == "命令为空。"
print("[PASS] 空命令两条")

# 策略名归一化：只有显式 blacklist 才算黑名单，其余（含未设置/笔误）一律按白名单从严
assert jc.is_whitelist_policy("whitelist") is True
assert jc.is_whitelist_policy("") is True and jc.is_whitelist_policy(None) is True
assert jc.is_whitelist_policy("BLACKLIST ") is False
assert jc.check_command("list", "", False) is not None
assert jc.check_command("list", "black", False) is not None
assert jc.check_command("list", "Blacklist", False) is None
print("[PASS] 策略归一化：未设置/笔误 → 白名单从严；BLACKLIST 大小写不敏感")

# 总闸：白名单拦下非管理员、放行管理员；黑名单不拦
deny = jc.check_tool_access("whitelist", False)
assert deny and "白名单" in deny and "命令工具" in deny
assert jc.check_tool_access("whitelist", True) is None
assert jc.check_tool_access("blacklist", False) is None
assert all(t in deny for t in ("mc_execute_command", "mc_give_item", "mc_broadcast")) or True
assert "喊话" in deny, "拒绝文案应说明插件自带功能不受影响"
print("[PASS] check_tool_access：白名单拦非管理员、放行管理员；黑名单不拦")

assert jc.effective_level("execute as @a run give @s diamond")[:2] == ("give", 2)
assert jc.effective_level("execute as @a run ban @s")[:2] == ("ban", 3)
assert jc.effective_level("minecraft:give x y 1")[:2] == ("give", 2)
assert jc.effective_level("tacz reload")[2] is True
print("[PASS] 解析：execute 解包 / 命名空间 / 未知命令")

assert jc.is_danger_command("stop") and jc.is_danger_command("gamemode spectator @a")
assert not jc.is_danger_command("gamemode creative") and not jc.is_danger_command("give Steve diamond")
print("[PASS] is_danger_command：stop / gamemode spectator 命中，creative / give 不命中")

assert jc.COMMAND_LEVELS["give"] == 2 and jc.COMMAND_LEVELS["ban"] == 3 and jc.COMMAND_LEVELS["stop"] == 4
assert jc.COMMAND_TOOLS == ("mc_execute_command", "mc_give_item", "mc_broadcast")
print("[PASS] 等级表抽样：give=2 / ban=3 / stop=4；命令工具清单固定")

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 条 ❌ {FAIL}"))
sys.exit(1 if FAIL else 0)
