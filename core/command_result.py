# -*- coding: utf-8 -*-
"""统一命令结果判定（v0.22.6）。

背景
====
v0.22.5 线上实测暴露「复杂 NBT 假成功」：分类器生成了 1.21 的物品组件语法
``netherite_sword[enchantments={levels:{...}}]`` 发给 1.20.1 服务端，服务器回：

    Expected whitespace to end one argument, but found trailing data

而 ``core/workflow.py::_run_simple`` 只要 ``rcon.command()`` 不抛异常就 ``ok_count += 1``，
且**成功分支丢弃明细** —— 于是回执写「已执行 1/1 条命令」，游戏里什么都没有。

本模块把「什么算成功」收口成唯一实现，供所有执行型入口复用。

三个维度互相独立
================
任何一个维度都不能代替另外两个：

===============  ================================================
status           这条命令的结果是什么
response_received  有没有收到属于**本命令**的响应（「发出去了」的证据）
boundary_confirmed 响应是否收到**可靠结束边界**（「读完整了」的证据）
===============  ================================================

``response_received=True`` + ``boundary_confirmed=False`` 是**合法组合**：
idle 降级模式下服务端确实答了这一条，但静默窗口到期，不保证内容完整。
此时对消息类命令应报「已发送·边界未确认」，既不是「失败」、也不是「结果未知」。

为什么不再用「非空且无错误标记 = 成功」
======================================
v0.22.6 用「非空 + 没命中错误词 → success」推断成功，v0.22.7 核验实测出两处假成功：

* ``give Steve diamond 1`` 服务端回 ``Operation aborted``（真失败）→ 判成 ``success``
  —— 「没发现错误词」被当成了**成功的证据**，可它只是**没证据**；
* 玩家名叫 ``Error`` 时 ``Gave 1 [Diamond] to Error``（真成功）→ 判成 ``failed``
  —— 裸子串 ``"error"`` 命中了人名。

因此 v0.22.7 改成三档，把「没证据」和「有证据」分开：

* **明确失败**：短语用子串、英文单词用**词边界**（``\berror\b``），不再误伤人名；
* **明确成功**：原版命令有**锚定命令级成功模式**（``^gave\b`` 等，见
  :data:`ANCHORED_SUCCESS_PATTERNS`）；命中锚定模式时**跳过**失败词扫描
  —— 有正面证据就不该被人名里的词翻盘；
* **没证据**：非空、无错误标记、也无成功模式 → ``inferred_success``（**不是** ``success``）
  或 ``unknown``，取决于命令是否非幂等。

``inferred_success`` 与 ``success`` 的区别是硬性的：``ok`` 为 ``False``
（绝不对外宣称成功），但 ``accepted`` 为 ``True``（消息确实发出去了，
不该让后续命令白白熔断）。**调用方不得把它当作 confirmed success 使用，
也不得把它当作「可以重试」的依据**（v0.22.8：重试判据只看「是否生效不确定」）。

若日后要彻底收紧（连 ``inferred_success`` 也不要），把
:data:`INFERRED_SUCCESS_ENABLED` 置 ``False`` 即可 —— 那类输出会全部落 ``unknown``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .java_commands import (
    CommandParseError,
    strip_namespace,
    unwrap_command,
)

#: 无成功证据的非空输出是否允许判 ``inferred_success``（默认允许）。
#:
#: 置 ``False`` = 最保守口径：这类输出一律 ``unknown``。代价是非幂等命令会因此
#: **熔断**后续命令（见 ``_exec_commands``），多命令任务可能半途断掉。
INFERRED_SUCCESS_ENABLED = True

CommandStatus = Literal[
    "success",
    "inferred_success",
    "dispatched_unconfirmed",
    "failed",
    "syntax_error",
    "unknown",
    "skipped",
]

# ===================== 命令集合（三组，禁止混用） =====================
#
# 1) 静默集合：成功时**本来就不回文本**，空响应即成功。
# 2) 幂等集合：成功时**通常有回显**，但重复执行无额外副作用 ——
#    因此空响应也可当成功（兼容某些服务端/插件不回显的情形）。
# 3) 非幂等集合：重复执行会**叠加副作用**，空响应**永远不能**当成功。
#
# 注意：幂等 ≠ 静默。time / weather / gamerule / gamemode / difficulty
# 在 vanilla 里都是有回显的（"Set the time to Day"），它们属于幂等集合。

#: 成功时设计上就不回文本的命令（空响应 = 成功）
SILENT_EMPTY_SUCCESS_COMMANDS = frozenset({
    "tellraw", "say", "title", "subtitle", "actionbar", "tm", "teammsg",
})

#: 幂等命令：重复执行无额外副作用（空响应也可当成功）
IDEMPOTENT_COMMANDS = frozenset({
    "time", "weather", "gamerule", "difficulty", "gamemode",
    "kill", "clear", "deop", "op", "whitelist", "pardon", "banlist",
    "save-all", "save-off", "save-on", "reload", "seed", "list",
    "data", "scoreboard", "tag", "bossbar", "forceload", "difficulty",
})

#: 非幂等命令：重复执行会产生**额外**副作用
NON_IDEMPOTENT_COMMANDS = frozenset({
    "give", "item", "summon", "effect", "xp", "experience", "fill",
    "setblock", "clone", "function", "random", "spreadplayers",
    "enchant", "damage", "loot", "place", "advancement", "recipe",
})

# ===================== 文本特征 =====================

#: 「命令不存在」——**必须**先于其它 unknown/syntax 判定。
#: 与 "Unknown or incomplete command" 只差几个词，语义相反：
#: 前者是命令本身不存在（模组没装 / 版本不支持），重试徒劳；
#: 后者是语法写错（命令存在），可以安全纠错。
_UNKNOWN_COMMAND_MARKERS = (
    'unknown command. type "/help"',
    'unknown command. type \\"/help\\"',
    "unknown command. type",
)

#: 明确的解析/语法错误 —— 命令**从未执行**，因此可安全重写重试。
_SYNTAX_MARKERS = (
    "unknown or incomplete command",
    "expected whitespace",
    "trailing data",
    "incorrect argument",
    "could not parse",
    "failed to parse",
    "invalid argument",
    "unknown item",
    "unknown enchantment",
    "unknown component",
    "unknown recipe",
    "unknown entity",
    "malformed",
    "无法解析",
    "语法错误",
)

#: 明确语法错误 —— **需要词边界**的标记。
#:
#: v0.22.7（与 P0-2 同源）：``"expected "`` 是 ``"unexpected "`` 的子串，
#: 于是 ``An unexpected error occurred`` / ``Operation error: unexpected exception``
#: 这类**运行期**错误会被判成 ``syntax_error``（``retryable=True``）——
#: 等于告诉上层「重写就能安全重试」，而实际上这条命令的失败原因跟语法无关。
#: 语法错误必须是「服务端在解析阶段就拒绝了」，所以这里一律按词边界匹配。
_SYNTAX_WORD_RE = re.compile(
    r"(?<![a-z])expected(?![a-z])", re.IGNORECASE
)

#: 明确失败 —— **多词短语**：按子串匹配（短语足够长，没有边界歧义）。
_PHRASE_FAILURE_MARKERS = (
    "no entity was found",
    "no player was found",
    "player not found",
    "entity not found",
    "permission denied",
    "not permitted",
    "no such",
    "does not exist",
    "not found",
    "you do not have",
    "unable to",
    "failed to",
    "operation aborted",
    "操作被取消",
    "没有权限",
    "拒绝执行",
)

#: 明确失败 —— **英文单词**：必须按**词边界**匹配。
#:
#: v0.22.7（核验 P0-2）：裸子串 ``"error" in text`` 会把玩家名 / 物品名 / NBT
#: 文本里的普通单词误伤成失败 —— 玩家叫 ``Error`` 时
#: ``Gave 1 [Diamond] to Error`` 会被判 ``failed``（实测复现）。
_WORD_FAILURE_MARKERS = (
    "error", "errors", "invalid", "denied", "aborted", "refused",
    "rejected", "cancelled", "canceled", "failure", "failed", "unable",
    "cannot", "can't",
)

#: 明确失败 —— **中文词**：CJK 没有词边界概念（``\b`` 认的是 ASCII/Unicode
#: 词字符与非词字符的交界，``操作失败`` 里 ``作|失`` 之间没有边界），
#: 因此中文仍按子串匹配。
_CJK_FAILURE_MARKERS = (
    "失败", "不存在", "找不到", "无法", "取消", "拒绝", "错误",
)

_FAILURE_WORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _WORD_FAILURE_MARKERS) + r")\b",
    re.IGNORECASE,
)

#: **锚定**命令级成功模式：输出必须以这些文本**开头**才算命中。
#:
#: 原版反馈文本固定且可枚举，因此可以给白名单；模组命令的反馈不可枚举，
#: 不能逼它命中模式表（那会把正常成功的模组命令全判成未知）。
#: 命中锚定模式 = 拿到**正面证据** → 跳过失败词扫描，
#: 于是 ``Gave 1 [Diamond] to Error`` 不会被人名里的 ``Error`` 翻盘。
ANCHORED_SUCCESS_PATTERNS: dict[str, tuple[str, ...]] = {
    "give": (r"^gave\b",),
    "summon": (r"^summoned\b",),
    "time": (r"^set the time\b", r"^the time is\b", r"^time is\b"),
    "weather": (r"^changed the weather\b", r"^set the weather\b"),
    "gamemode": (r"^set .{0,48}game mode\b",),
    "difficulty": (r"^set difficulty\b", r"^the difficulty is\b"),
    "gamerule": (r"^set the gamerule\b", r"^gamerule\b"),
    "clear": (r"^cleared\b",),
    "tp": (r"^teleported\b",),
    "teleport": (r"^teleported\b",),
    "effect": (r"^applied\b",),
    "enchant": (r"^enchanted\b",),
    "xp": (r"^gave\b",),
    "experience": (r"^gave\b",),
    "fill": (r"^successfully filled\b",),
    "setblock": (r"^changed the block\b",),
    "clone": (r"^successfully cloned\b",),
    "kill": (r"^killed\b",),
    "kick": (r"^kicked\b",),
    "ban": (r"^banned\b",),
    "pardon": (r"^unbanned\b",),
    "banlist": (r"^there are\b", r"^banned\b"),
    "whitelist": (r"^added\b", r"^removed\b", r"^there are\b"),
    "op": (r"^made\b", r"^opped\b"),
    "deop": (r"^made\b", r"^de-opped\b"),
    "damage": (r"^applied\b",),
    "item": (r"^replaced\b", r"^modified\b", r"^gave\b"),
    "replaceitem": (r"^replaced\b", r"^modified\b"),
    "loot": (r"^gave\b", r"^dropped\b"),
    "advancement": (r"^granted\b", r"^revoked\b"),
    "recipe": (r"^granted\b", r"^revoked\b"),
    "scoreboard": (r"^set\b", r"^added\b", r"^removed\b", r"^changed\b"),
    "bossbar": (r"^added\b", r"^removed\b", r"^changed\b"),
    "tag": (r"^added\b", r"^removed\b"),
    "forceload": (r"^added\b", r"^removed\b", r"^changed\b"),
    "save-all": (r"^saved the game\b",),
    "seed": (r"^seed\b",),
    "list": (r"^there are\b",),
    "spreadplayers": (r"^spread\b",),
    "place": (r"^placed\b",),
}

_ANCHORED_SUCCESS_RES: dict[str, tuple[re.Pattern, ...]] = {
    cmd: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for cmd, pats in ANCHORED_SUCCESS_PATTERNS.items()
}

#: **非锚定**通用成功标记 —— 只用来把置信度抬到 ``explicit``。
#: 检查顺序在失败词**之后**（只有锚定模式才在之前）。
_SUCCESS_MARKERS = (
    "gave ",
    "set the time",
    "changed the weather",
    "set the weather",
    "set difficulty",
    "set the gamerule",
    "set own game mode",
    "teleported ",
    "summoned ",
    "applied ",
    "cleared ",
    "killed ",
    "granted ",
    "enabled ",
    "disabled ",
    "placed ",
    "added ",
    "已发放",
    "已执行",
    "已完成",
)


@dataclass
class CommandResult:
    """一条命令的执行结果。

    ``status`` / ``response_received`` / ``boundary_confirmed`` 是三个独立维度，
    任何一个都不能代替另外两个。
    """

    command: str
    status: CommandStatus
    output: str = ""
    reason: str = ""
    retryable: bool = False
    boundary_confirmed: bool | None = None
    response_received: bool = False
    attempt: int = 1
    #: ``explicit`` = 命中显式成功标记；``inferred`` = 非空且无错误标记（推断成功）；
    #: ``""`` = 与成功无关。
    confidence: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否可对外宣称「成功」。``dispatched_unconfirmed`` **不算**成功。"""
        return self.status == "success"

    @property
    def accepted(self) -> bool:
        """是否可视为「命令已被服务端接受」（用于决定要不要熔断后续命令）。

        ``dispatched_unconfirmed``（消息确实发出去了，只是边界未确认）与
        ``inferred_success``（无错误标记、但没有成功证据）都不该中止同批次的后续命令。

        注意 ``accepted`` **不等于** ``ok``：前者管「同批次后续命令要不要继续发」，
        后者管「能不能对外宣称成功」——``inferred_success`` 的 ``accepted=True``、
        ``ok=False``。

        v0.22.8（核验 P1）：``accepted`` **不构成自动重试的依据**。工作流要不要重跑本轮，
        由 ``core/workflow.py::_halt_on_uncertain`` 按「是否生效不确定」单独判定 ——
        ``unknown`` / ``inferred_success`` / ``dispatched_unconfirmed`` 一律不自动重试。
        """
        return self.status in ("success", "inferred_success", "dispatched_unconfirmed")

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


# ===================== 复杂任务路由（v0.22.6） =====================
#
# 背景：分类器规则自相矛盾 —— core/agent_prompts.py 里
#   · "simple" ：仅用原版命令即可完成（发原版物品 diamond_sword…）
#   · "complex"：复杂 NBT 结构（附魔、枪械附件 Attachments…）
# 「附魔下界合金剑」**同时命中两条**，实测 LLM 选了 simple，
# 而 simple 是两条路径里唯一没有输出校验、没有纠错、没有游戏内反馈的一条。
#
# 因此不能只靠 prompt 守规矩：分类之后、执行之前要加一道**确定性**守门。

#: 用户请求里的复杂特征（中英文都要查 —— 不能只查中文）
_COMPLEX_REQUEST_MARKERS = (
    "附魔", "附魔书", "nbt", "组件", "满配", "属性", "属性修饰符",
    "自定义名称", "模型数据", "custommodeldata", "枪械", "配件", "附件",
    "attachments", "enchant", "attribute", "modifier",
)

#: 数值计算类特征（v0.22.11 / GPT 核验「额外发现」）：配比、产量、耗材这类问题
#: **只靠分类器提示词拦不住** —— 模型偶尔仍会判 simple，而 simple 是全插件防护
#: 最弱的一条路（无输出校验 / 无纠错 / 无到账核验）。这里补一道**确定性**守门，
#: 与「附魔 / NBT / 组件」等既有特征同源：命中即把 simple 升级为 complex。
_COMPLEX_NUMERIC_MARKERS = (
    "数值计算", "配比", "产量", "耗材", "材料需求", "需要多少",
    "每分钟", "每秒产出", "机器效率",
)

#: 携带物品数据的命令 —— **只对这类命令**检查物品参数。
#: 不做全局字符扫描：裸 ``[`` 是选择器语法，``kill @e[type=item]`` /
#: ``tp @p[tag=foo]`` / ``gamemode creative @a[team=red]`` 全是正常简单命令，
#: 一旦按 ``[`` 触发，就会白跑一整套 Agent 流水线。
_ITEM_COMMANDS = frozenset({"give", "item", "replaceitem", "clear", "loot", "enchant"})

#: 物品参数里的复杂结构特征
_COMPLEX_ITEM_MARKERS = (
    "enchantments", "components", "levels:", "attributemodifiers",
    "custommodeldata", "attachments", "display", "candestroy",
    "canplaceon", "trim", "storedenchantments",
)


def effective_command_segment(command: str) -> tuple[str, str]:
    """把命令拆成 ``(有效命令名, 其后的参数原文)``，并穿透 execute 包装。

    v0.22.7（核验 P2-6）：**解析全仓只有一处实现** —— 直接复用
    :func:`core.java_commands.unwrap_command`（v0.22.3 专为「找第一个 run 会翻车」
    写的按语法消费子命令的实现）。

    此前本模块自己手搓了第二份 ``rfind(" run ")``，实测会把

        execute as @a run tellraw @a {"text":" run "}

    的「命令名」解析成 ``"}`` —— 连 tellraw 都不认识了。同一个解析问题只允许
    有一处实现，否则两份迟早分叉，而分叉的代价是判定与权限用的是两套真相。
    """
    try:
        inner = unwrap_command(command)
    except CommandParseError:
        # 解析不出来（execute 子命令边界判不出 / 嵌套过深）：**绝不猜**。
        # 权限闸门 java_commands.effective_level 已对这类命令 fail-closed 直接拒绝；
        # 这里只需保证**不给它任何静默 / 幂等 / 成功模式豁免** ——
        # 返回一个不在任何清单里的名字即可。
        return "execute", ""
    parts = inner.split(None, 1)
    if not parts or not parts[0]:
        return "", ""
    name = strip_namespace(parts[0].lower())
    return name, (parts[1] if len(parts) > 1 else "")


def looks_like_complex_item_command(command: str) -> bool:
    """命令是否为「携带复杂物品数据」的物品类命令（**命令上下文感知**）。

    只解析物品参数，不做全局字符扫描 —— 否则选择器语法会被大量误判。
    """
    name, args = effective_command_segment(command)
    if name not in _ITEM_COMMANDS:
        return False
    low = args.lower()
    return any(m in low for m in _COMPLEX_ITEM_MARKERS)


def looks_like_numeric_request(request: str) -> bool:
    """请求是否属于「数值计算」类（v0.22.11，**确定性**守门）。

    分类器提示词写的是「数值计算（配比/产量/耗材等）」，但提示词只能让模型
    **倾向**判 complex —— 模型一旦返回 simple，提示词毫无约束力。凡命中本清单
    一律升级为 complex：宁可多走一趟流水线，也不让配比类问题落到 simple 路径
    （那里没有输出校验、没有纠错、没有到账核验）。
    """
    low = (request or "").lower()
    return any(m in low for m in _COMPLEX_NUMERIC_MARKERS)


def looks_like_complex_request(request: str) -> bool:
    """用户请求文本是否命中复杂特征（第一层路由信号）。"""
    low = (request or "").lower()
    return looks_like_numeric_request(low) or any(m in low for m in _COMPLEX_REQUEST_MARKERS)


def looks_like_complex_task(request: str, commands: list | None = None) -> bool:
    """请求与生成命令**都要**检查，任一命中即判复杂。

    只做「升级」判定（simple → complex），不反向降级。
    """
    if looks_like_complex_request(request):
        return True
    for c in commands or []:
        if isinstance(c, dict):
            c = c.get("command") or ""
        if looks_like_complex_item_command(str(c)):
            return True
    return False


# ===================== 命令名解析 =====================


def base_name(command: str) -> str:
    """取命令的**有效命令名**（小写、去命名空间、穿透 execute 包装）。

    必须解析命令名而不是做字符串前缀匹配 —— 否则
    ``minecraft:tellraw`` / ``/tellraw`` / ``execute as @a run tellraw``
    都会被漏掉，而 ``tellrawx`` 这种不存在的命令又会被误当成 tellraw。
    """
    return effective_command_segment(command)[0]


def is_silent_command(command: str) -> bool:
    """成功时设计上就不回文本（空响应 = 成功）。"""
    return base_name(command) in SILENT_EMPTY_SUCCESS_COMMANDS


def is_idempotent_command(command: str) -> bool:
    return base_name(command) in IDEMPOTENT_COMMANDS


def is_non_idempotent_command(command: str) -> bool:
    """重复执行会产生额外副作用 —— 空响应**永远不能**当成功。"""
    return base_name(command) in NON_IDEMPOTENT_COMMANDS


# ===================== 文本分类 =====================


def _low(output: str) -> str:
    return (output or "").strip().lower()


def is_unknown_command_output(output: str) -> bool:
    """``Unknown command. Type "/help" for help.`` —— 命令本身不存在。"""
    low = _low(output)
    if not low:
        return False
    if any(m in low for m in _UNKNOWN_COMMAND_MARKERS):
        return True
    # 兜底形态：以 unknown command 开头且提到 help
    return low.startswith("unknown command") and "help" in low


def is_syntax_error_output(output: str) -> bool:
    """明确的解析/语法错误 —— 命令从未执行，可安全重写重试。"""
    low = _low(output)
    if not low or is_unknown_command_output(low):
        return False
    if any(m in low for m in _SYNTAX_MARKERS):
        return True
    return _SYNTAX_WORD_RE.search(low) is not None


def is_explicit_failure_output(output: str) -> bool:
    """明确失败：英文**单词**按词边界、短语与中文按子串。

    v0.22.7（核验 P0-2）：不再用裸子串扫英文单词 —— 那会误伤玩家名
    （``Gave 1 [Diamond] to Error`` 被判 failed）以及 NBT / 物品名里的普通单词。
    """
    low = _low(output)
    if not low or is_unknown_command_output(low) or is_syntax_error_output(low):
        return False
    if any(m in low for m in _PHRASE_FAILURE_MARKERS):
        return True
    if any(m in low for m in _CJK_FAILURE_MARKERS):
        return True
    return _FAILURE_WORD_RE.search(low) is not None


def is_anchored_success_output(command: str, output: str) -> bool:
    """输出是否以**该命令自己的**锚定成功模式开头（= 拿到正面证据）。

    只对原版命令有效（反馈文本固定可枚举）。模组命令没有模式表，一律返回
    ``False``，由调用方走 ``inferred_success`` / ``unknown`` 兜底 ——
    **不能**因为模组反馈五花八门就默认放宽成成功。
    """
    pats = _ANCHORED_SUCCESS_RES.get(base_name(command))
    if not pats:
        return False
    low = _low(output)
    if not low:
        return False
    return any(p.search(low) for p in pats)


def is_explicit_success_output(output: str) -> bool:
    """命中**非锚定**通用成功标记（``Gave`` 等）。只提升置信度，不作为成功门槛。"""
    low = _low(output)
    if not low:
        return False
    if is_unknown_command_output(low) or is_syntax_error_output(low):
        return False
    if is_explicit_failure_output(low):
        return False
    return any(m in low for m in _SUCCESS_MARKERS)


# ===================== 总入口 =====================


def classify_command_output(
    command: str,
    output: str,
    *,
    boundary_confirmed: bool | None = None,
    response_received: bool = False,
    attempt: int = 1,
) -> CommandResult:
    """把一次 ``rcon.command()`` 的返回判成 ``CommandResult``。

    判定顺序（v0.22.7 定稿，**每一步的位置都有理由**）：

    1. **空响应分支**（最高优先级，且**非幂等先于** ``boundary_confirmed`` 判断）
       —— 非幂等命令的空响应永远是 ``unknown``：``dispatched_unconfirmed`` 会让
       ``accepted=True`` 从而**不熔断**后续命令，那会削弱 v0.22.4 建立的重复副作用
       保护（``give`` 空响应 → 后续命令照发 = 可能重复发物品）。
    2. ``Unknown command`` → ``failed``（命令不存在，重试徒劳）
    3. 明确解析错误 → ``syntax_error``（命令从未执行，可安全重写）
    4. **先取正面证据**：命中本命令的锚定成功模式（``^gave\\b`` 等）时，
       第 5 步跳过失败词扫描 —— 否则玩家名叫 ``Error`` 会把真成功翻成失败
    5. 其它明确失败 → ``failed``
    6. **边界未确认**（``boundary_confirmed is False`` + 已收到响应）：
       非幂等 → ``unknown``（继续熔断）；消息类 / 幂等 → ``dispatched_unconfirmed``。
       **本步必须在「返回成功」之前** —— idle 截断的输出不能冒充完整成功
    7. 明确成功（锚定证据 或 通用成功标记）→ ``success``
    8. 非空、无错误标记、也**没有成功证据** → 消息类命令视为成功（输出即消息回显）；
       非幂等命令 → ``unknown``（不确认副作用，宁可熔断也不谎报）；
       其余 → ``inferred_success``（**不是** ``success``：``ok=False``）
    9. 兜底 → ``unknown``
    """
    out_s = str(output or "").strip()
    res = CommandResult(
        command=command,
        status="unknown",
        output=out_s,
        boundary_confirmed=boundary_confirmed,
        response_received=bool(response_received),
        attempt=attempt,
    )

    # ---- 1) 空响应：三个维度一起看 ----
    if not out_s:
        name = base_name(command)
        if name in NON_IDEMPOTENT_COMMANDS:
            # 非幂等命令收到「合法空响应」也不能当成功：副作用是否落定无法确认
            res.status = "unknown"
            res.reason = "非幂等命令收到空响应，无法确认副作用是否落定"
            res.retryable = False
            return res
        if name in SILENT_EMPTY_SUCCESS_COMMANDS or name in IDEMPOTENT_COMMANDS:
            if boundary_confirmed is True:
                res.status = "success"
                res.confidence = "empty"
                res.reason = "静默/幂等命令，响应边界已确认，空响应即成功"
                return res
            if response_received:
                res.status = "dispatched_unconfirmed"
                res.reason = "已收到本命令响应，但响应边界未确认（可能处于 idle 降级模式）"
                return res
            res.status = "unknown"
            res.reason = "空响应且未收到本命令响应，无法确认命令是否送达"
            return res
        res.status = "unknown"
        res.reason = "未知命令类型的空响应"
        return res

    # ---- 2) 命令不存在（必须先于其它 unknown/syntax） ----
    if is_unknown_command_output(out_s):
        res.status = "failed"
        res.reason = "该命令在此服务端不存在（模组未安装 / 版本不支持 / 命名空间错误）"
        res.retryable = False
        return res

    # ---- 3) 明确解析错误：命令从未执行，可安全重写 ----
    if is_syntax_error_output(out_s):
        res.status = "syntax_error"
        res.reason = "服务端明确拒绝解析（命令未执行，可安全重写后重试）"
        res.retryable = True
        return res

    # ---- 4) 先取正面证据：是否命中**本命令的**锚定成功模式 ----
    anchor_ok = is_anchored_success_output(command, out_s)

    # ---- 5) 其它明确失败 ----
    # 已拿到锚定成功证据时**跳过**失败词扫描：``Gave 1 [Diamond] to Error``
    # 里的 "error" 只是玩家名的一部分（v0.22.7 核验 P0-2）。
    if not anchor_ok and is_explicit_failure_output(out_s):
        res.status = "failed"
        res.reason = "服务端明确拒绝执行"
        res.retryable = False
        return res

    # ---- 6) 边界未确认：不得宣称完整成功（必须早于「返回成功」） ----
    if boundary_confirmed is False and response_received:
        name = base_name(command)
        if name in NON_IDEMPOTENT_COMMANDS:
            # 非幂等命令在 idle 下拿到半截输出：不知道副作用落没落定。
            # 报 dispatched_unconfirmed 会让 accepted=True → 后续命令继续执行，
            # 重复副作用保护就漏了 —— 必须继续 unknown（v0.22.7 核验裁决 #2）。
            res.status = "unknown"
            res.reason = (
                "非幂等命令的响应边界未确认（可能处于 idle 降级模式），"
                "输出可能被截断，无法确认副作用是否落定"
            )
            return res
        if name in SILENT_EMPTY_SUCCESS_COMMANDS or name in IDEMPOTENT_COMMANDS:
            res.status = "dispatched_unconfirmed"
            res.reason = "已收到本命令响应，但响应边界未确认（可能处于 idle 降级模式）"
            return res
        res.status = "unknown"
        res.reason = "响应边界未确认，且命令类型未知，无法确认结果"
        return res

    # ---- 7) 明确成功 ----
    if anchor_ok:
        res.status = "success"
        res.confidence = "explicit"
        res.reason = "命中本命令的锚定成功模式"
        return res
    if is_explicit_success_output(out_s):
        res.status = "success"
        res.confidence = "explicit"
        res.reason = "命中显式成功标记"
        return res

    # ---- 8) 非空、无错误标记，但**没有成功证据** ----
    name = base_name(command)
    if name in SILENT_EMPTY_SUCCESS_COMMANDS:
        # 消息类命令的输出就是消息本身的回显（/say 会把广播内容回给你），
        # 不是状态文本 —— 无错误标记即视为成功。
        res.status = "success"
        res.confidence = "inferred"
        res.reason = "消息类命令：输出为消息回显，无错误标记即视为成功"
        return res
    if name in NON_IDEMPOTENT_COMMANDS:
        # 不确认副作用 → 宁可熔断也不谎报成功（重复发物品比「结果未知」贵得多）
        res.status = "unknown"
        res.reason = "非幂等命令的输出未命中任何成功模式，无法确认副作用是否落定"
        return res
    if INFERRED_SUCCESS_ENABLED:
        res.status = "inferred_success"
        res.confidence = "inferred"
        res.reason = "输出非空且无错误标记，但没有成功证据（未确认，不视为成功）"
        return res

    # ---- 9) 兜底 ----
    res.status = "unknown"
    res.reason = "输出文本无法判定（严格模式：未命中任何成功证据）"
    return res


#: 状态 → 中文短标签（回执/日志统一措辞）
STATUS_LABEL = {
    "success": "成功",
    "inferred_success": "已执行·未确认",
    "dispatched_unconfirmed": "已发送·边界未确认",
    "failed": "失败",
    "syntax_error": "语法错误",
    "unknown": "结果未知",
    "skipped": "未发送",
}


#: 全仓「已知状态」唯一集合 —— 就是上面标签表的键（v0.22.9）。
#:
#: 用途：任何**按状态分派或回推**的地方，都必须先确认状态属于本集合。
#: 不属于本集合的（缺 ``status`` 字段 / 拼写错误 / 将来新加状态忘了登记）
#: 一律按「结果不确定」处理（fail-closed）—— 绝不允许默认成 ``success``：
#: 「默认成功」正是 v0.22.5 假成功事故的同一种坏默认值。
#:
#: 要新增状态请先在这里补标签，不要在别处再抄一份状态表。
KNOWN_STATUSES = frozenset(STATUS_LABEL)


def format_results(results: list[CommandResult]) -> str:
    """把结果列表渲染成可读明细（成功分支**也必须**带上，不允许丢明细）。"""
    lines = []
    for r in results:
        tag = STATUS_LABEL.get(r.status, r.status)
        lines.append(f"[{tag}] {r.command}")
        if r.output:
            lines.append(f"    服务器返回：{r.output}")
            # v0.22.7：未确认 / 未知的状态必须把「为什么未确认」一并带上 ——
            # 否则用户只看到一句服务器原文，会把它当成成功回执。
            if r.status in ("inferred_success", "unknown") and r.reason:
                lines.append(f"    （{r.reason}）")
        elif r.reason:
            lines.append(f"    （{r.reason}）")
    return "\n".join(lines)
