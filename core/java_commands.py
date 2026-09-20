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

#: execute 嵌套解析的深度上限（超过即拒绝：fail-closed，绝不因为「解析不动了」而放行）。
MAX_EXECUTE_DEPTH = 8


class CommandParseError(ValueError):
    """命令结构无法安全解析（execute 子命令边界判不出来 / 嵌套过深）。

    闸门一律按**拒绝**处理：解析失败绝不能变成「按普通命令放行」。
    """

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

    `/execute as @a run give ...` 这类包装会被**按语法**解包到 `run` 之后的真实
    子命令，否则「前缀写着 execute」就能绕开危险命令判定（如 execute run stop）。

    v0.22.3：解包失败（子命令边界判不出 / 嵌套过深）时返回
    ``("execute", 2, True)``；第三个元素为 True 表示「无法安全判定」，
    闸门 :func:`_check_line` 会据此**直接拒绝**（fail-closed）。
    """
    line = normalize_command(command)
    if not line:
        return "", 0, False
    try:
        inner = unwrap_command(line)
    except CommandParseError:
        return "execute", COMMAND_LEVELS["execute"], True
    name = base_name(inner)
    if name == "execute":
        return "execute", COMMAND_LEVELS["execute"], False
    if name in COMMAND_LEVELS:
        return name, COMMAND_LEVELS[name], False
    return name, DEFAULT_LEVEL, True


def strip_namespace(token: str) -> str:
    """去掉命名空间前缀：``minecraft:gamemode`` → ``gamemode``。"""
    return token.split(":", 1)[1] if ":" in token else token


def tokenize_command(text: str) -> list[str]:
    """按 Minecraft 命令的词法切分：空白分隔，引号 / 方括号 / 花括号**内部不切**。

    ``@e[name="a b"]``、``{"text":"a b"}``、``[1, 2, 3]`` 都算**一个** token。
    这正是「找第一个 run」那种字符串匹配翻车的根源：token 边界得先站稳，
    才能判断某个 ``run`` 到底是执行分界、还是目标实体名 / 参数的一部分。
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in str(text or "").strip():
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch.isspace() and depth == 0:
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth = max(0, depth - 1)
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


#: execute 子命令里参数个数固定的那些（``as <targets>`` / ``at <targets>`` …）。
_EXEC_SIMPLE_ARITY: dict[str, int] = {
    "as": 1, "at": 1, "align": 1, "anchored": 1, "in": 1,
}

#: ``if`` / ``unless`` 条件里「边界可以可靠判定」的关键字 → 参数个数。
#: 刻意**只收录能数得清的**：数不清的（data/items/function 的可变长写法等）
#: 一律让解析失败 → 交给闸门拒绝，而不是猜一个边界继续放行。
_CONDITION_ARITY: dict[str, int] = {
    "block": 4,      # if block <pos:3> <block>
    "blocks": 10,    # if blocks <start:3> <end:3> <destination:3> <mode>
    "biome": 4,      # if biome <pos:3> <biome>
    "loaded": 3,     # if loaded <pos:3>
    "dimension": 1,  # if dimension <dimension>
    "entity": 1,     # if entity <targets>
    "predicate": 1,  # if predicate <predicate>
    "data": 3,       # if data <block|entity|storage> <target> <path>
}

_SCORE_OPS: tuple[str, ...] = ("<", "<=", "=", ">=", ">")


def _peek(tokens: list[str], idx: int) -> str:
    """取 tokens[idx] 的小写、去命名空间形式（越界返回空串）。"""
    return strip_namespace(tokens[idx]).lower() if idx < len(tokens) else ""


def _need(tokens: list[str], idx: int, count: int) -> int:
    """要求从 idx 起还有 count 个 token，返回消费后的下标；不够就判为无法安全解析。"""
    if idx + count > len(tokens):
        raise CommandParseError("execute 子命令参数不完整，无法安全判定边界")
    return idx + count


def _skip_condition(tokens: list[str], idx: int) -> int:
    """跳过一条 ``if`` / ``unless`` 条件，返回下一个子命令的下标。"""
    kw = _peek(tokens, idx)
    if not kw:
        raise CommandParseError("execute 的 if / unless 缺少条件")
    if kw == "score":
        # if score <target> <objective> matches <range>
        # if score <target> <objective> <op> <source> <sourceObjective>
        base = _need(tokens, idx, 3)
        nxt = _peek(tokens, base)
        if nxt == "matches":
            return _need(tokens, base, 2)
        if nxt in _SCORE_OPS:
            return _need(tokens, base, 3)
        raise CommandParseError("execute if score 的条件写法无法安全判定")
    need = _CONDITION_ARITY.get(kw)
    if need is None:
        raise CommandParseError(f"execute 条件「{tokens[idx]}」的边界无法安全判定")
    return _need(tokens, idx, 1 + need)


def _skip_store(tokens: list[str], idx: int) -> int:
    """跳过一条 ``store`` 子命令，返回下一个子命令的下标。"""
    if _peek(tokens, idx) not in ("result", "success"):
        raise CommandParseError("execute store 缺少 result / success 关键字")
    idx += 1
    kind = _peek(tokens, idx)
    if kind == "block":                       # store <…> block <pos:3> <path>
        return _need(tokens, idx, 5)
    if kind == "entity":                      # store <…> entity <targets> <path>
        return _need(tokens, idx, 3)
    if kind in ("score", "storage", "bossbar"):  # <targets|id> <objective|path|value|max>
        return _need(tokens, idx, 3)
    raise CommandParseError("execute store 的目标类型无法安全判定")


def _skip_execute_clauses(tokens: list[str], idx: int) -> str | None:
    """从 tokens[idx] 开始按**语法**消费 execute 子命令，返回 ``run`` 之后的命令文本。

    返回 ``None`` = 一路读到末尾也没有 ``run``（这条 execute 没有内层命令，本身干不了事）。
    任何判不准的写法都抛 :class:`CommandParseError`：猜不出来就拒绝，绝不放行。
    """
    while idx < len(tokens):
        kw = _peek(tokens, idx)
        if kw == "run":
            inner = tokens[idx + 1:]
            if not inner:
                raise CommandParseError("execute run 之后没有可执行的命令")
            return " ".join(inner)
        if kw in ("if", "unless"):
            idx = _skip_condition(tokens, idx + 1)
            continue
        if kw == "store":
            idx = _skip_store(tokens, idx + 1)
            continue
        if kw in _EXEC_SIMPLE_ARITY:
            idx = _need(tokens, idx, 1 + _EXEC_SIMPLE_ARITY[kw])
            continue
        if kw == "positioned":
            # positioned as <targets> <pos:3> ／ positioned <pos:3>
            idx = _need(tokens, idx, 6 if _peek(tokens, idx + 1) == "as" else 4)
            continue
        if kw == "rotated":
            # rotated as <targets> <rot:2> ／ rotated <rot:2>
            idx = _need(tokens, idx, 5 if _peek(tokens, idx + 1) == "as" else 3)
            continue
        if kw == "facing":
            # facing entity <targets> <anchor> ／ facing <pos:3>
            idx = _need(tokens, idx, 4)
            continue
        raise CommandParseError(f"无法识别的 execute 子命令「{tokens[idx]}」")
    return None


def unwrap_command(line: str) -> str:
    """剥掉 ``execute … run`` 包装，返回最内层真实命令文本。

    v0.22.3：**不再「找第一个 run」**，而是按 execute 语法逐个消费子命令 ——
    只有出现在合法分界位置的 ``run`` 才算执行边界。于是
    ``execute as run run stop`` 里的第一个 ``run``（目标实体名）不会被误当成边界，
    真正的 ``stop`` 会老老实实被解包出来受检。

    解析失败（子命令边界判不出 / 参数不完整）或嵌套超过
    :data:`MAX_EXECUTE_DEPTH` 时抛 :class:`CommandParseError`，
    调用方必须**拒绝**：绝不能退回「按普通命令放行」。

    没有 ``run`` 的写法（如 ``execute as @a``）返回 ``"execute"``：
    它没有内层命令，本身执行不了任何东西。
    """
    text = normalize_command(line)
    depth = 0
    while text:
        tokens = tokenize_command(text)
        if not tokens or _peek(tokens, 0) != "execute":
            return text
        depth += 1
        if depth > MAX_EXECUTE_DEPTH:
            raise CommandParseError(
                f"execute 嵌套超过安全上限 {MAX_EXECUTE_DEPTH} 层，无法安全判定"
            )
        inner = _skip_execute_clauses(tokens, 1)
        if inner is None:
            return "execute"
        text = inner
    return text


def canonical_text(line: str) -> str:
    """把命令归一化成「可做策略匹配」的文本。

    流程：按语法剥 execute 包装 → **只对命令名 token** 去命名空间 → 压缩空白 → 转小写 → 去尾分号。

    v0.22.3：不再对**所有** token 无条件去冒号 —— 那会改写 ``mod:item``、
    ``ns:function``、``minecraft:overworld`` 这类资源标识符的参数含义。
    现在只有「命令名」那一个 token 去命名空间，参数保持原样。

    结构解析不出来时抛 :class:`CommandParseError`（由闸门拒绝，不放行）。
    """
    text = unwrap_command(line)
    tokens = tokenize_command(text)
    if not tokens:
        return ""
    tokens[0] = strip_namespace(tokens[0])
    return " ".join(tokens).lower().rstrip(";")


def match_danger_rule(line: str) -> str | None:
    """返回命中的危险规则原文（未命中返回 ``None``）。

    规则按「命令名 + 参数」**整词**匹配，因此 ``gamemode spectator`` 这类带参数的
    规则也能拦住 ``execute run gamemode spectator @a``。
    """
    text = canonical_text(line)
    if not text:
        return None
    tokens = text.split(" ")
    for prefix in DANGER_PREFIXES:
        need = prefix.lower().split(" ")
        if tokens[: len(need)] == need:
            return prefix
    return None


def is_danger_command(command: str) -> bool:
    """黑名单策略的黑名单判定：规范化后的命令名 / 前缀命中内置危险命令。

    先按语法解包（execute 包装）与命名空间处理，再做匹配，因此
    ``minecraft:gamemode spectator @a``、``execute run gamemode spectator @a``
    与 ``gamemode spectator @a`` 判定完全一致。

    fail-closed：命令结构无法安全解析时**返回 True**（按危险处理）。
    解析失败绝不能变成放行。
    """
    try:
        return match_danger_rule(command) is not None
    except CommandParseError:
        return True


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
    """黑名单策略下的逐行把关（白名单在 check_tool_access 就已整体拦下）。

    fail-closed 三段式：
      ① 结构解析不出来（子命令边界判不出 / 嵌套过深）→ 拒绝；
      ② 解包后命中危险清单 → 拒绝；
      ③ 解包后的命令权限等级 ≥ 3（服务器管理类）→ 拒绝。
    绝不允许出现「解析失败 → 按普通命令放行」这种 fail-open。
    """
    name, level, _unknown = effective_level(line)
    if not name:
        return "命令为空。"
    if is_whitelist_policy(policy):
        return check_tool_access(policy, is_admin=False)
    try:
        unwrap_command(line)
    except CommandParseError as e:
        return (
            f"命令「{name}」的结构无法安全解析（{e}），"
            "为安全起见仅管理员可执行，已拒绝。"
        )
    if is_danger_command(line):
        return f"命令「{name}」属于危险操作，仅管理员可执行，已拒绝。"
    if level >= MIN_ADMIN_LEVEL:
        return (
            f"命令「{name}」需要权限等级 {level}（服务器管理类），"
            f"仅管理员可执行，已拒绝。"
        )
    return None
