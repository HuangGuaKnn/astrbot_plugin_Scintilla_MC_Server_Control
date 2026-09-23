# -*- coding: utf-8 -*-
"""版本能力上下文（v0.22.6 批次 2）。

背景（v0.22.5 线上事故）
========================
用户请求「附魔锋利5、亡灵杀手5…的下界合金剑」，分类器生成了：

    give HuangGuaKnn netherite_sword[enchantments={levels:{...}}] 1

两处错，且都是**版本错**：

1. ``[enchantments={levels:{...}}]`` 是 **1.20.5+ 的物品组件语法**，而服务端是 1.20.1；
2. ``minecraft:sweeping_edge`` 是 **1.21+ 的附魔 ID**，1.20.1 叫 ``minecraft:sweeping``。

服务端回 ``Expected whitespace to end one argument, but found trailing data``。

根因不是 LLM 笨：``main.py::detect_server_version()`` **早已存在**，却只喂给 WebUI 的
``/server/status``，**从未进入任何 Agent 提示词** —— 等于让 LLM 凭记忆猜版本。

本模块把「版本 → 语法世代 → 模板 / ID 对照」收口成**代码侧唯一实现**，
由提示词注入层调用。方向是实施单 Q1 的裁决：**不让 LLM 自由决定语法世代**。

约束（实施单 §四）
==================
* **不猜**：版本未知时显式降级并拒绝生成带数据的命令，绝不默认某个版本。
* **手动声明 > 文件探测 > 未知**（补丁 3）：异地 RCON 模式读不到服务端文件，
  若无手动声明出口，所有附魔 / NBT 请求会被一律拒绝且**无法解除**——那是功能倒退。
  手动声明是**用户显式提供的事实**，不是猜。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ===================== 语法世代 =====================

#: 1.13 以下（1.8~1.12.2）：**预扁平化**世代 —— 数字物品 ID + data 值、
#: 旧 ``execute <实体> <x y z>`` 语法、``ench`` 数字附魔 ID。
#: v0.23.2（GPT 续单裁决 Q1/Q4）：本世代**不自动生成任何命令**，仅作能力标注。
#: 真机验证前不实现（路线见 docs/CONSULT_gpt_cross_version_2.md §5 方案 B / 阶段 2）。
ITEM_SYNTAX_PREFLATTEN = "legacy_preflatten"

#: 已知版本 < 1.13 时**生效**的世代：明确不支持自动生成（fail-closed）。
#: 与 ``ITEM_SYNTAX_UNKNOWN``（版本未知）区分：这是「知道版本、知道不支持」。
ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN = "unsupported_preflatten"

#: 1.20.5 以下：物品数据写在 ``{}`` NBT 里
ITEM_SYNTAX_LEGACY = "legacy_nbt"
#: 1.20.5 及以上：物品数据写在 ``[]`` 物品组件里
ITEM_SYNTAX_COMPONENTS = "components"
#: 版本未知且未手动声明 —— 不得生成任何带数据的命令
ITEM_SYNTAX_UNKNOWN = "unknown"

#: 预扁平化分水岭（v0.23.2）：1.13 重写了命令图（give/execute/effect/difficulty/data）
#: 并做资源 ID 扁平化。低于此版本 = 本插件不自动生成命令。
ITEM_PREFLATTEN_CUTOVER = (1, 13)

#: NBT → 物品组件的分水岭（实施单 Q1c 点名的核心分水岭）
ITEM_COMPONENTS_CUTOVER = (1, 20, 5)

#: ``item_syntax_override`` 配置项的合法取值
ITEM_SYNTAX_CHOICES = ("auto", ITEM_SYNTAX_LEGACY, ITEM_SYNTAX_COMPONENTS)

# ===================== 版本解析 =====================

_MC_TAGGED_RE = re.compile(r"\bMC\s+(\d+)\.(\d+)(?:\.(\d+))?")
_BARE_VER_RE = re.compile(r"(?:^|[^\d.])(\d+)\.(\d+)(?:\.(\d+))?")


def parse_mc_version(text: str) -> tuple[int, ...] | None:
    """从任意版本文本里解析出 ``(major, minor, patch)``，解析不出返回 ``None``。

    同时吃这两种形态：
    * 探测输出 ``"MC 1.20.1 · Forge 47.4.23"``（优先取带 ``MC`` 标记的）
    * 用户手填 ``"1.20.1"`` / ``"paper 1.20.1"`` / ``"1.21"``

    注意：``"MC 1.20.1 · Forge 47.4.23"`` 里有两个版本号，
    因此**必须先试带 ``MC`` 标记的**，否则会误取 Forge 的 ``47.4.23``。
    """
    s = str(text or "").strip()
    if not s:
        return None
    for rx in (_MC_TAGGED_RE, _BARE_VER_RE):
        m = rx.search(s)
        if not m:
            continue
        parts = [int(m.group(1)), int(m.group(2))]
        if m.group(3) is not None:
            parts.append(int(m.group(3)))
        # 合理的 MC 版本必然是 1.x（未来 2.x 也放行）；挡住 "47.4.23" 这类
        if parts[0] > 9:
            continue
        return tuple(parts)
    return None


def version_text(mc: tuple[int, ...] | None) -> str:
    return ".".join(str(x) for x in mc) if mc else ""


# ===================== 版本能力 =====================


@dataclass(frozen=True)
class VersionInfo:
    """服务端版本事实。

    ``source`` 是**诚实标注**，必须透传到提示词与 UI：

    * ``override`` —— 用户在设置里手填（最高优先级，是事实不是猜）
    * ``detected`` —— 从服务端文件探测到
    * ``unknown``  —— 探测失败且未手填（此时**不得**生成带数据的命令）
    """

    mc: tuple[int, ...] | None = None
    raw: str = ""
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return self.mc is not None

    @property
    def source_label(self) -> str:
        return {
            "override": "主人手动声明",
            "detected": "服务端文件探测",
            "unknown": "未知（探测失败且未声明）",
        }.get(self.source, self.source)

    def at_least(self, other: tuple[int, ...]) -> bool:
        """版本是否 ≥ ``other``。版本未知时返回 ``False``（保守）。"""
        if self.mc is None:
            return False
        return tuple(self.mc) >= tuple(other)


def resolve_version_info(override: str = "", detected: str = "") -> VersionInfo:
    """按 **手动声明 > 文件探测 > 未知** 的优先级解析版本（补丁 3）。

    注意：手填了但解析不出（如 ``"最新版"``）时**不能**静默回退到探测结果 ——
    否则用户以为声明生效了、实际用的是别的版本。此时按「未知」处理并如实上报。
    """
    ov = str(override or "").strip()
    if ov:
        mc = parse_mc_version(ov)
        if mc:
            return VersionInfo(mc=mc, raw=ov, source="override")
        # 声明了但解析不了：不猜，显式降级（原始文本保留，供 UI 展示）
        return VersionInfo(mc=None, raw=ov, source="unknown")
    de = str(detected or "").strip()
    if de:
        mc = parse_mc_version(de)
        if mc:
            return VersionInfo(mc=mc, raw=de, source="detected")
    return VersionInfo(mc=None, raw="", source="unknown")


def item_syntax_for(mc: tuple[int, ...] | None) -> str:
    """版本 → 物品语法世代。

    三态之外多一态（v0.23.2）：**已知版本 < 1.13** 返回
    ``unsupported_preflatten`` —— 不再让它落进 ``legacy_nbt``。
    因为 ``legacy_nbt`` 实际表示的是 **1.13~1.20.4** 的写法，
    拿它去覆盖 1.12 只会生成语法上不成立的命令（静默错命令）。
    """
    if not mc:
        return ITEM_SYNTAX_UNKNOWN
    if tuple(mc) < ITEM_PREFLATTEN_CUTOVER:
        return ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN
    return (
        ITEM_SYNTAX_COMPONENTS
        if tuple(mc) >= ITEM_COMPONENTS_CUTOVER
        else ITEM_SYNTAX_LEGACY
    )


def resolve_item_syntax(info: VersionInfo, override: str = "auto") -> tuple[str, str]:
    """解析最终生效的物品语法世代，返回 ``(世代, 来源)``。

    来源 ``override`` 时，即使版本未知也**允许**继续 —— 这是补丁 3 的逃生出口：
    异地模式用户手填语法世代后，复杂 NBT 请求不再被一律拒绝。
    """
    # v0.23.2（裁决 Q1）：**已知**版本 < 1.13 时，手动指定世代也不放行 ——
    # ``legacy_nbt`` 是 1.13~1.20.4 的写法，用它去「放行」1.12 只会生成错命令。
    # 注意与「版本未知」的区别：未知时手填世代是合法逃生出口（异地模式），
    # 而「知道是 1.12」的情况下，用户真正该做的是手动执行适配该版本的命令。
    if info.mc is not None and tuple(info.mc) < ITEM_PREFLATTEN_CUTOVER:
        return ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN, "unsupported"
    ov = str(override or "auto").strip().lower()
    if ov in (ITEM_SYNTAX_LEGACY, ITEM_SYNTAX_COMPONENTS):
        return ov, "override"
    return item_syntax_for(info.mc), info.source


# ===================== 分水岭清单（实施单 Q1c） =====================
#
# 只收「会让同一条命令在某个版本上解析失败」的硬分水岭。
# 每条都写明**判据**（怎么确认），供日后核对与扩充。

#: 分水岭清单。**v0.23.2 起结构化**（GPT 续单裁决 Q6：强烈采纳）——
#: 每条至少回答四件事：哪条边界、影响哪类命令、两侧各是什么形态、遇到不支持的一侧要做什么。
#: 字段约定：
#:   ``version``       分水岭版本（元组）
#:   ``surface``       差异层面：command_syntax / function_system / item_format /
#:                     item_nbt_field / enchant_id / attribute_id
#:   ``affects``       受影响的命令族（元组）
#:   ``before`` / ``after``  两侧的形态名
#:   ``action_before`` / ``action_after``  两侧的动作：supported / reject_generation / unsupported
#:   ``tested``        该结论是否经过真机验证（当前真机矩阵只有 1.20.1 · Forge 47.4.23）
CUTOVERS: tuple[dict, ...] = (
    {
        "version": (1, 13),
        "surface": "command_syntax",
        "title": "命令图与资源 ID 扁平化（1.13 大断层）",
        "affects": ("give", "execute", "effect", "difficulty", "data", "entitydata", "blockdata"),
        "before": "preflatten",
        "after": "flattened",
        "action_before": "unsupported",
        "action_after": "supported",
        "tested": False,
        "detail": (
            "1.13 重写命令图：`give` 取消 data 位置参数、NBT 改贴物品 ID 之后、数量后置"
            "（旧写法 `give <玩家> <物品> <数量> <数据值> {NBT}`）；"
            "旧 `execute @p ~ ~ ~ <命令>` 废弃，改为 `execute as/at/positioned/... run`；"
            "`effect` / `difficulty` 语法变化，`entitydata` → `data`；"
            "同时做资源 ID 扁平化（数字 ID 移除、大量方块/物品/实体 ID 改名）。"
            "**本插件对 <1.13 不自动生成命令**（见 ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN）。"
        ),
    },
    {
        # v0.23.2 第二版（GPT 核验 Q6）：建议现在加入能力清单、暂不实现生成。
        # 理由：这是真实能力差异，即使当前不生成 `/function`，能力表也应完整；
        # 将来实现 P1 时有明确落点；且不会扩大当前行为范围。
        "version": (1, 13),
        "surface": "function_system",
        "title": "function 从旧函数体系迁移到数据包体系",
        "affects": ("function",),
        "before": "legacy_function",
        "after": "datapack_function",
        "action_before": "unsupported",
        "action_after": "reject_generation",
        "tested": False,
        "detail": (
            "1.12 也有 `/function`，但它读的是存档内 `functions/` 目录下的 `.mcfunction`；"
            "1.13 起改为数据包 `data/<命名空间>/functions/`，且函数体内命令语法同步跟随命令图变化。"
            "本插件当前**不生成** `/function`，此处仅作能力登记，为 P1 的旧版函数体系留出落点。"
        ),
    },
    {
        "version": (1, 14),
        "surface": "command_syntax",
        "title": "execute 条件子命令（if / unless data 等）",
        "affects": ("execute",),
        "before": "no_execute_conditions",
        "after": "has_execute_conditions",
        "action_before": "reject_generation",
        "action_after": "supported",
        "tested": False,
        "detail": (
            "`execute if/unless data` 等条件子命令自 1.14 起才有；"
            "在 1.13.x 上生成这类命令会被判为语法错误。"
        ),
    },
    {
        "version": (1, 20, 2),
        "surface": "item_nbt_field",
        "title": "部分药水 / 特殊物品 NBT 字段改名",
        "affects": ("give", "item", "replaceitem"),
        "before": "CustomPotionEffects",
        "after": "custom_potion_effects",
        "action_before": "reject_generation",
        "action_after": "supported",
        "tested": False,
        "detail": (
            "药水类物品的部分 NBT 字段名在 1.20.2 前后不同"
            "（来源：外部核验方裁决 Q3；**本插件未真机验证**）。"
            "这类差异应绑定到具体物品类型，不做全局字符串替换。"
        ),
    },
    {
        "version": ITEM_COMPONENTS_CUTOVER,
        "surface": "item_format",
        "title": "物品 NBT → 物品组件（本模块的核心）",
        "affects": ("give", "item", "replaceitem"),
        "before": "nbt_braces",
        "after": "components_brackets",
        "action_before": "supported",
        "action_after": "supported",
        "tested": False,
        "detail": (
            "1.20.4 及以下：`netherite_sword{Enchantments:[{id:\"minecraft:sharpness\",lvl:5}]}`；"
            "1.20.5 及以上：`netherite_sword[enchantments={levels:{\"minecraft:sharpness\":5}}]`。"
            "同批改名：`CustomModelData`→`custom_model_data`、"
            "`AttributeModifiers`→`attribute_modifiers`、`display.Name`→`custom_name`。"
        ),
    },
    {
        "version": (1, 21),
        "surface": "enchant_id",
        "title": "附魔 ID 改名 sweeping → sweeping_edge",
        "affects": ("give", "enchant", "item"),
        "before": "minecraft:sweeping",
        "after": "minecraft:sweeping_edge",
        "action_before": "supported",
        "action_after": "supported",
        "tested": True,
        "detail": (
            "横扫之刃在 1.20.6 及以下叫 `minecraft:sweeping`，"
            "1.21 起叫 `minecraft:sweeping_edge`。写错的那个版本会报 unknown enchantment。"
        ),
    },
    {
        "version": (1, 21, 2),
        "surface": "attribute_id",
        "title": "属性 ID 去掉分组前缀",
        "affects": ("give", "attribute", "item"),
        "before": "minecraft:generic.movement_speed",
        "after": "minecraft:movement_speed",
        "action_before": "supported",
        "action_after": "supported",
        "tested": False,
        "detail": (
            "`minecraft:generic.movement_speed` → `minecraft:movement_speed`；"
            "`horse.jump_strength` / `player.block_break_speed` 等前缀一并去掉。"
        ),
    },
)

#: 预扁平化服务端（<1.13）上仍可**安全自动生成**的简单命令（白名单）。
#: 判据：命令本身不含资源 ID、且 1.13 命令图改动未触及它的参数结构。
#: 这是保守白名单 —— 1.12 用户至少还能喊话、管人、改时间天气；物品/NBT 类一律拒绝。
#:
#: ⚠️ **本集合只表示「语法可生成」，不表示「允许执行」。**
#: 权限与副作用由 ``_safe_command()``（黑名单/白名单/游客闸门）与
#: ``_admin_gate()``（stop / op / ban 等管理类）**独立**把关 ——
#: 进入本集合**不会**绕过它们。维护时不要把「加进白名单」当成「放开权限」。
PREFLATTEN_SAFE_COMMANDS: frozenset[str] = frozenset({
    "time", "weather", "say", "me", "tell", "msg", "w", "list", "gamemode",
    "kill", "seed", "save-all", "save-off", "save-on", "stop", "kick",
    "ban", "ban-ip", "pardon", "pardon-ip", "op", "deop", "whitelist",
    "help", "spawnpoint", "setworldspawn",
    # v0.23.2 第二版（GPT 核验 P0-3）：插件自身的播报 / 标题入口所用命令。
    # `tellraw`（1.7+）与 `title`（1.8+）在 1.8~1.12.2 与 1.13+ 语法一致，
    # 插件内部反馈 `_send_feedback()` 与 `mcs_say` / `mcs_title_cmd` / `mc_broadcast`
    # 都依赖它们；若不列入，1.12 用户连「播报」都用不了（功能性倒退）。
    "tellraw", "title",
    # `banlist`：`mcs_banlist_cmd` 依赖它；1.8~1.21 语法未变。
    # 该入口目前走直连（不守门），列入白名单是**预防性**的 ——
    # 将来若给内部只读命令也加守门，不会误伤。
    "banlist",
})


#: v0.23.2 第二版（GPT 核验 P0-2）：**物品类命令一律拒绝**，不再有「不带数据就放行」的例外。
#:
#: 原设计曾放行不带 NBT 的 ``give P item N``（依据：1.12 允许省略数据值/NBT，
#: 参数位置与 1.13+ 一致）。GPT 核验指出该推理**不足以证明语义正确** ——
#: 因为 ``item`` 本身可能就是扁平化之后的现代 ID（1.12 里没有 ``red_wool``，
#: 只有 ``wool`` + data 14），而本函数无从校验「这个 ID 在旧版是否存在 / 要不要 data 值」。
#: 故在实现旧版物品映射（数字 ID + data，见 P1 路线图）之前，物品类命令一并拦掉。
PREFLATTEN_ITEM_COMMANDS: frozenset[str] = frozenset({"give", "clear", "item", "replaceitem"})


def preflatten_block_reason(command: str) -> str:
    """预扁平化服务端上，这条命令能否**自动生成 / 执行**？可放行返回空串，否则返回拒绝原因。

    判据（保守白名单）：
    1. 明确不受 1.13 命令图改动影响的简单命令 → 放行（``PREFLATTEN_SAFE_COMMANDS``）；
    2. 物品类（``give`` / ``clear`` / ``item`` / ``replaceitem``）→ **一律拒绝**
       （物品 ID 扁平化风险，见 ``PREFLATTEN_ITEM_COMMANDS``）；
    3. 其余（execute / effect / difficulty / data / summon / setblock …）→ 一律拒绝。

    适用范围（v0.23.2 第二版起扩大）：**所有执行入口** ——
    ``mc_workflow`` / ``mc_execute_command`` / ``mc_give_item`` / ``mc_broadcast`` /
    ``mcs_say`` / ``mcs_title_cmd`` / ``mcs_kick_cmd`` / ``mcs_ban_cmd`` /
    ``mcs_unban_cmd`` / ``mc_kick`` / ``mc_ban``。

    ⚠️ GPT 核验 P0-1 的修正：``mc_execute_command`` **不是**「用户手写命令」——
    它是 ``@filter.llm_tool``，命令字符串由 LLM 生成，同样可能把 1.13+ 语法
    发给 1.12 服务端，因此必须一并守门。真正的人工原始命令通道留到 P1。
    """
    cmd = str(command or "").strip().lstrip("/")
    name = cmd.split(" ", 1)[0].lower()
    if not name:
        return "命令为空"
    if name in PREFLATTEN_SAFE_COMMANDS:
        return ""
    if name in PREFLATTEN_ITEM_COMMANDS:
        return (
            f"`{name}` 属于物品类命令，而 1.13 以下的物品 ID 与当前版本不同"
            "（扁平化前是「数字 ID + data 值」），本插件尚未完成旧版物品 ID/data 映射；"
            "请手动执行适配该版本的命令，或把服务端升级到 1.13 及以上"
        )
    return (
        f"命令 `{name}` 属于 1.13 重写 / 扁平化影响的命令族，"
        "本插件对 1.13 以下版本不自动生成命令"
    )

# ===================== 附魔 ID 版本化别名 =====================

#: ``(新 ID, 旧 ID, 改名生效版本)`` —— 生效版本**之前**用旧 ID，之后用新 ID。
ENCHANT_RENAMES: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    ("minecraft:sweeping_edge", "minecraft:sweeping", (1, 21)),
)

#: 各版本通用、从未改名的附魔（中文名 → ID），供提示词直接抄，减少 LLM 记忆负担。
ENCHANT_COMMON: tuple[tuple[str, str], ...] = (
    ("锋利", "minecraft:sharpness"),
    ("亡灵杀手", "minecraft:smite"),
    ("节肢杀手", "minecraft:bane_of_arthropods"),
    ("击退", "minecraft:knockback"),
    ("火焰附加", "minecraft:fire_aspect"),
    ("抢夺", "minecraft:looting"),
    ("耐久", "minecraft:unbreaking"),
    ("经验修补", "minecraft:mending"),
    ("保护", "minecraft:protection"),
    ("效率", "minecraft:efficiency"),
    ("时运", "minecraft:fortune"),
    ("精准采集", "minecraft:silk_touch"),
    ("力量", "minecraft:power"),
    ("无限", "minecraft:infinity"),
)


def enchantment_id_for(mc: tuple[int, ...] | None, new_id: str) -> str:
    """把某条改名附魔的**新 ID** 折算成 ``mc`` 版本上真实存在的 ID。"""
    for new, old, since in ENCHANT_RENAMES:
        if new == new_id:
            if mc is not None and tuple(mc) >= tuple(since):
                return new
            return old
    return new_id


def enchantment_renames_for(mc: tuple[int, ...] | None) -> list[tuple[str, str]]:
    """返回 ``[(会被误用的写法, 本版本正确的写法)]``，供提示词展示。

    例：1.20.1 → ``[("minecraft:sweeping_edge", "minecraft:sweeping")]``；
        1.21   → ``[("minecraft:sweeping", "minecraft:sweeping_edge")]``。
    """
    if mc is None:
        return []
    out: list[tuple[str, str]] = []
    for new, old, since in ENCHANT_RENAMES:
        if tuple(mc) >= tuple(since):
            out.append((old, new))
        else:
            out.append((new, old))
    return out


# ===================== 提示词注入片段 =====================

_LEGACY_TEMPLATE = (
    'give <玩家> <物品ID>{Enchantments:[{id:"minecraft:<附魔ID>",lvl:<等级>}, ...]} <数量>'
)
_COMPONENTS_TEMPLATE = (
    "give <玩家> <物品ID>[enchantments={levels:{\"minecraft:<附魔ID>\":<等级>, ...}}] <数量>"
)


def build_version_context(
    info: VersionInfo, item_syntax_override: str = "auto"
) -> str:
    """产出注入 Agent 提示词的「版本约束片段」。

    这是**代码决定语法世代**的落点（实施单 Q1）：片段由版本算出，
    LLM 只负责填 ID 与数值，不负责挑语法。
    """
    syntax, syntax_source = resolve_item_syntax(info, item_syntax_override)
    lines: list[str] = ["【服务端版本上下文 · 由代码注入，不是猜测】"]

    if info.known:
        lines.append(f"- 服务端：{info.raw or version_text(info.mc)}（来源：{info.source_label}）")
    else:
        raw = f"（声明文本：{info.raw}，无法解析）" if info.raw else ""
        lines.append(f"- 服务端版本：**未知**{raw}")

    # ---- 版本未知且未声明语法世代：显式降级，拒绝生成带数据的命令 ----
    if syntax == ITEM_SYNTAX_UNKNOWN:
        lines.append(
            "- ⚠️ **本次任务禁止构造任何带 NBT / 组件 / 附魔 / 属性 / 自定义名称的物品命令。**"
        )
        lines.append(
            "  需要这类命令时，直接输出 `success=false`，并在 reasoning 里写明："
            "「服务端版本未知，无法确定物品语法世代；请先在插件设置 → 连接 里填写"
            " `server_version_override`（如 1.20.1），或手动指定 `item_syntax_override`」。"
        )
        lines.append("  不要猜版本、不要凭记忆挑语法 —— 猜错会生成解析失败的命令。")
        return "\n".join(lines)

    # ---- 已知版本 < 1.13：本插件不自动生成命令（v0.23.2 · GPT 续单裁决 Q1/Q4）----
    if syntax == ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN:
        lines.append(
            f"- ⚠️ **该版本低于 1.13，本插件暂不支持自动生成命令。**"
            f"（分水岭：{version_text(ITEM_PREFLATTEN_CUTOVER)}）"
        )
        lines.append(
            "  原因：1.13 重写了命令图（`give` 取消数据值参数并把 NBT 移到物品 ID 之后、"
            "旧 `execute` 废弃、`effect` / `difficulty` 变化、`entitydata`→`data`），"
            "并做了资源 ID 扁平化。1.8~1.12.2 需要另一套预扁平化写法"
            "（数字物品 ID + data 值、`ench` 数字附魔 ID、旧 `execute`），"
            "本插件尚未实现，**也没有真机验证条件**。"
        )
        lines.append(
            "  因此：**本次任务不要构造任何命令**，直接输出 `success=false`，"
            "reasoning 写明：「服务端版本 "
            f"{version_text(info.mc)} 低于 1.13，本插件暂不支持该版本的自动命令生成；"
            "请手动执行适配该版本的命令，或把服务端升级到 1.13 及以上」。"
        )
        safe = "、".join(f"`{c}`" for c in sorted(PREFLATTEN_SAFE_COMMANDS))
        lines.append(
            f"  例外：以下不受 1.13 命令图改动影响的简单命令**可以**照常生成 —— {safe}。"
        )
        return "\n".join(lines)

    if syntax == ITEM_SYNTAX_LEGACY:
        lines.append(
            f"- 物品语法世代：**legacy_nbt**（1.20.5 以下，物品数据写在 `{{}}` 里，来源：{syntax_source}）"
        )
        lines.append(
            "- ⚠️ **禁止**使用物品组件写法 `<物品ID>[...]` —— 那是 1.20.5+ 才支持的语法，"
            "本版本会回 `Expected whitespace to end one argument, but found trailing data`。"
        )
        lines.append(f"- 本版本正确的物品写法（照抄结构，只换 ID 与数值）：\n  {_LEGACY_TEMPLATE}")
    else:
        lines.append(
            f"- 物品语法世代：**components**（1.20.5 及以上，物品数据写在 `[]` 里，来源：{syntax_source}）"
        )
        lines.append(
            "- ⚠️ **禁止**使用旧 NBT 写法 `<物品ID>{...}` —— 1.20.5+ 已改为物品组件。"
        )
        lines.append(f"- 本版本正确的物品写法（照抄结构，只换 ID 与数值）：\n  {_COMPONENTS_TEMPLATE}")

    # ---- 附魔 ID 版本化别名（本次事故的第二处根因）----
    renames = enchantment_renames_for(info.mc)
    if renames:
        lines.append("- 本版本附魔 ID 改名对照（**必须用右列**）：")
        for wrong, right in renames:
            lines.append(f"  · `{wrong}` → 本版本应为 `{right}`")

    common = "、".join(f"{cn}=`{eid}`" for cn, eid in ENCHANT_COMMON)
    lines.append(f"- 各版本通用附魔 ID：{common}")

    # ---- 其它硬分水岭 ----
    applicable = [c for c in CUTOVERS if info.at_least(c["version"]) and c["version"] != ITEM_COMPONENTS_CUTOVER]
    if applicable:
        lines.append("- 本版本已生效的其它语法分水岭（构造命令时留意）：")
        for c in applicable:
            lines.append(f"  · {version_text(c['version'])} {c['title']}")
    return "\n".join(lines)


def describe_capabilities(
    info: VersionInfo, item_syntax_override: str = "auto"
) -> dict:
    """给 WebUI / 概览用的版本能力快照（不含提示词正文）。"""
    syntax, syntax_source = resolve_item_syntax(info, item_syntax_override)
    supported = syntax not in (ITEM_SYNTAX_UNKNOWN, ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN)
    if syntax == ITEM_SYNTAX_UNKNOWN:
        support_note = "版本未知：带 NBT / 附魔 / 物品组件的请求不会被自动生成"
    elif syntax == ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN:
        support_note = (
            f"服务端低于 {version_text(ITEM_PREFLATTEN_CUTOVER)}："
            "本插件暂不支持该版本的自动命令生成"
        )
    else:
        support_note = "支持自动命令生成"
    return {
        "known": info.known,
        "version": version_text(info.mc),
        "raw": info.raw,
        "source": info.source,
        "source_label": info.source_label,
        "item_syntax": syntax,
        "item_syntax_source": syntax_source,
        "supported": supported,
        "support_note": support_note,
        "cutover": version_text(ITEM_COMPONENTS_CUTOVER),
        "preflatten_cutover": version_text(ITEM_PREFLATTEN_CUTOVER),
        # 真机验证矩阵（诚实标注：唯一实测过的服务端形态）
        "verified_on": "1.20.1 · Forge 47.4.23",
        "enchant_renames": [
            {"wrong": w, "right": r} for w, r in enchantment_renames_for(info.mc)
        ],
        "cutovers": [
            {
                "version": version_text(c["version"]),
                "title": c["title"],
                "surface": c["surface"],
                "affects": list(c["affects"]),
                "tested": c["tested"],
            }
            for c in CUTOVERS
        ],
    }
