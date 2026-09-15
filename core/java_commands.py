"""Java 版命令「权限等级」表 + 白名单 / 黑名单闸门（口径：非管理员 = 普通玩家）。

数据来源：中文 Minecraft Wiki《命令》条目 →「命令列表及其概述 → Java 版」表格
（该表逐条列出 Java 版命令与其需要的权限等级）。
    https://zh.minecraft.wiki/w/命令?variant=zh-cn
仅采用 **Java 版** 数据：基岩版/教育版的权限体系（BE / EDU 列）与本插件所连的
Java 版专用服务器不通用，故一律不采用。抓取日期：2026-09-12。

权限等级（Java 版）：
    0 = 任何玩家（不需要 OP）
    1 = 1 级 OP（可绕过出生点保护等，本插件不涉及）
    2 = 2 级 OP（give / tp / time / say / gamemode … 常用作弊类）
    3 = 3 级 OP（多人游戏管理：ban / kick / op / whitelist …）
    4 = 4 级 OP（服务器管理：stop / save-* / publish / jfr / perf …）

闸门语义（v0.20.0 简化版）：
    * 本模块只管「命令工具」——也就是会**向服务器下发指令**的入口：
        mc_execute_command（执行指令）、mc_give_item（发物品）、mc_broadcast（广播）；
      mcs 喊话 / 全屏喊话 / 踢人 / 封禁 / 状态 / 查询 / 绑定 / 帮助 等属于
      **插件自带功能**，各有自己的开关，不受本策略影响（想管喊话就关 say_command_public）。
    * whitelist（白名单，默认）→ 非管理员**完全不能使用口头命令工具**；
      插件自带功能照旧（喊话、状态、查询、绑定…）。
    * blacklist（黑名单）→ **所有玩家**都能使用命令工具；
      但危险命令（内置危险清单 + 权限等级 ≥ 3 的服务器管理命令）依旧仅管理员可用。
    * 管理员不受策略限制（仅要求命令非空）。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 权限等级表

#: 未知命令（模组命令、整合包自定义命令、比本表更新的版本命令）按 2 级处理。
DEFAULT_LEVEL = 2

#: 达到该等级即视为「服务器管理」，黑名单策略下非管理员一律拒绝。
MIN_ADMIN_LEVEL = 3

#: Java 版命令 → 需要的权限等级（含 /execute 解包后递归判定）。
#: 来源见模块 docstring；特殊条件命令取「专用服务器」下的值（本插件即专用服务器）。
COMMAND_LEVELS: dict[str, int] = {
    # ---- 等级 0：任何玩家 ----
    "help": 0, "list": 0, "me": 0, "msg": 0, "tell": 0, "w": 0,
    "teammsg": 0, "tm": 0, "trigger": 0, "chase": 0,
    # ---- 等级 2：常见的 2 级 OP 命令 ----
    "advancement": 2, "attribute": 2, "bossbar": 2, "clear": 2, "clone": 2,
    "damage": 2, "data": 2, "datapack": 2, "debugmobspawning": 2, "debugpath": 2,
    "defaultgamemode": 2, "dialog": 2, "difficulty": 2, "effect": 2, "enchant": 2,
    "execute": 2, "experience": 2, "fetchprofile": 2, "fill": 2, "fillbiome": 2,
    "forceload": 2, "function": 2, "gamemode": 2, "gamerule": 2, "give": 2,
    "item": 2, "kill": 2, "locate": 2, "loot": 2, "particle": 2, "place": 2,
    "playsound": 2, "random": 2, "recipe": 2, "reload": 2, "return": 2,
    "ride": 2, "rotate": 2, "say": 2, "schedule": 2, "scoreboard": 2, "seed": 2,
    "serverpack": 2, "setblock": 2, "setworldspawn": 2, "spawn_armor_trims": 2,
    "spawnpoint": 2, "spectate": 2, "spreadplayers": 2, "stopsound": 2,
    "stopwatch": 2, "summon": 2, "swing": 2, "tag": 2, "team": 2,
    "teleport": 2, "tellraw": 2, "test": 2, "time": 2, "title": 2, "tp": 2,
    "version": 2, "warden_spawn_tracker": 2, "waypoint": 2, "weather": 2,
    "worldborder": 2, "xp": 2,
    # ---- 等级 3：多人游戏管理 ----
    "ban": 3, "ban-ip": 3, "banlist": 3, "debug": 3, "debugconfig": 3,
    "deop": 3, "kick": 3, "op": 3, "pardon": 3, "pardon-ip": 3, "raid": 3,
    "setidletimeout": 3, "tick": 3, "transfer": 3, "whitelist": 3,
    # 别名（Wiki 表格中归入 pardon）
    "unban": 3,
    # ---- 等级 4：服务器管理 ----
    "jfr": 4, "perf": 4, "publish": 4, "save-all": 4, "save-off": 4,
    "save-on": 4, "stop": 4, "unpublish": 4,
}

#: 黑名单策略的历史内置危险命令（按「命令名 + 前缀」匹配；黑名单只拦这些与管理类）。
DANGER_PREFIXES: tuple[str, ...] = (
    "stop", "op", "deop", "ban-ip", "whitelist", "save-off", "debug", "publish",
    "forceload", "function", "reload", "kick", "ban", "unban", "pardon",
    "setidletimeout", "difficulty", "gamemode spectator",
)

#: 命令别名 → 主命令名（少数常用别名在 Wiki 表中不单独出现）。
ALIASES: dict[str, str] = {
    "unban": "pardon",
    "xp": "experience",
    "tm": "teammsg",
    "w": "msg",
    "tell": "msg",
}

#: 白名单策略下被拦下的「命令工具」清单（仅用于生成提示文案）。
COMMAND_TOOLS: tuple[str, ...] = ("mc_execute_command", "mc_give_item", "mc_broadcast")

#: 插件自带功能举例（提示文案用；这些能力各自有开关，不受本策略影响）。
BUILTIN_FEATURES_HINT = "喊话、全屏喊话、状态、查询、绑定、帮助"


# ---------------------------------------------------------------- 解析

def normalize_command(command: str) -> str:
    """去掉开头多余的斜杠/空白，得到可解析的命令文本。"""
    return str(command or "").strip().lstrip("/").strip()


def split_lines(command: str) -> list[str]:
    """把一条文本拆成多行命令（RCON/批量场景可能一次传多行），逐行受检。"""
    out: list[str] = []
    for raw in str(command or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = normalize_command(raw)
        if line:
            out.append(line)
    return out


def base_name(command: str) -> str:
    """取命令名：小写、去掉命名空间前缀（minecraft:give → give）。"""
    token = normalize_command(command).split(" ")[0] if normalize_command(command) else ""
    if ":" in token:
        token = token.split(":", 1)[1]
    return token.lower()


def effective_level(command: str) -> tuple[str, int, bool]:
    """解析命令的 (命令名, 权限等级, 是否未知命令)。

    `/execute as @a run give ...` 这类包装会被递归解包到 `run` 之后的真实子命令，
    否则「前缀写着 execute」就能绕开危险命令判定（如 execute run stop）。
    """
    line = normalize_command(command)
    if not line:
        return "", 0, False
    name = base_name(line)
    if name == "execute":
        tokens = line.split()
        for idx, tok in enumerate(tokens):
            if tok == "run" and idx + 1 < len(tokens):
                return effective_level(" ".join(tokens[idx + 1:]))
        return "execute", COMMAND_LEVELS["execute"], False
    if name in COMMAND_LEVELS:
        return name, COMMAND_LEVELS[name], False
    return name, DEFAULT_LEVEL, True


def is_danger_command(command: str) -> bool:
    """黑名单策略的黑名单判定：命令名或前缀命中内置危险命令。"""
    line = normalize_command(command).lower()
    name, _level, _unknown = effective_level(line)
    if name in DANGER_PREFIXES:
        return True
    return any(line == p or line.startswith(p + " ") for p in DANGER_PREFIXES)


def is_whitelist_policy(policy: str | None) -> bool:
    """策略名归一化：除显式 blacklist 外，一律按白名单（默认）处理。"""
    return str(policy or "").strip().lower() != "blacklist"


# ---------------------------------------------------------------- 闸门

def check_tool_access(policy: str = "whitelist", is_admin: bool = False) -> str | None:
    """命令工具总闸：返回 None 表示放行，否则返回拒绝原因。

    白名单（默认）：非管理员完全不能用命令工具（执行指令 / 发物品 / 广播）；
    黑名单：所有人都能用，具体危险命令再由 _check_line 逐条把关。
    """
    if is_admin or not is_whitelist_policy(policy):
        return None
    return (
        "当前为白名单策略：命令工具（执行指令 / 发物品 / 广播）默认仅管理员可用。"
        f"{BUILTIN_FEATURES_HINT}这类插件自带功能不受影响；"
        "如需放开请让管理员把策略改成黑名单，或把你的账号加入 admin_ids。"
    )


def check_command(command: str, policy: str = "whitelist", is_admin: bool = False) -> str | None:
    """权限闸门：返回 None 表示放行，否则返回拒绝原因（人类可读）。

    policy: "whitelist"（白名单，默认：非管理员不能用命令工具）
            "blacklist"（黑名单：所有人都能用，但危险命令与权限等级 ≥ 3 的管理命令仅管理员）
    管理员不受策略限制（但仍要求命令非空）。
    """
    lines = split_lines(command)
    if not lines:
        return "命令为空。"
    if is_admin:
        return None
    denied = check_tool_access(policy, is_admin=False)
    if denied:
        return denied
    for line in lines:
        reason = _check_line(line, policy)
        if reason:
            return reason
    return None


def _check_line(line: str, policy: str) -> str | None:
    """黑名单策略下的逐行把关（白名单在 check_tool_access 就已整体拦下）。"""
    name, level, _unknown = effective_level(line)
    if not name:
        return "命令为空。"
    if is_whitelist_policy(policy):
        return check_tool_access(policy, is_admin=False)
    if is_danger_command(line):
        return f"命令「{name}」属于危险操作，仅管理员可执行，已拒绝。"
    if level >= MIN_ADMIN_LEVEL:
        return (
            f"命令「{name}」需要权限等级 {level}（服务器管理类），"
            f"仅管理员可执行，已拒绝。"
        )
    return None
