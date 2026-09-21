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

#: 1.20.5 以下：物品数据写在 ``{}`` NBT 里
ITEM_SYNTAX_LEGACY = "legacy_nbt"
#: 1.20.5 及以上：物品数据写在 ``[]`` 物品组件里
ITEM_SYNTAX_COMPONENTS = "components"
#: 版本未知且未手动声明 —— 不得生成任何带数据的命令
ITEM_SYNTAX_UNKNOWN = "unknown"

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
    """版本 → 物品语法世代。版本未知 → ``unknown``（不是默认 NBT，更不是默认组件）。"""
    if not mc:
        return ITEM_SYNTAX_UNKNOWN
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
    ov = str(override or "auto").strip().lower()
    if ov in (ITEM_SYNTAX_LEGACY, ITEM_SYNTAX_COMPONENTS):
        return ov, "override"
    return item_syntax_for(info.mc), info.source


# ===================== 分水岭清单（实施单 Q1c） =====================
#
# 只收「会让同一条命令在某个版本上解析失败」的硬分水岭。
# 每条都写明**判据**（怎么确认），供日后核对与扩充。

CUTOVERS: tuple[dict, ...] = (
    {
        "version": (1, 13),
        "title": "execute 重写",
        "detail": (
            "旧写法 `execute @p ~ ~ ~ <命令>` 废弃，改为 "
            "`execute as/at/positioned/... run <命令>`。"
            "1.13+ 服务端见到旧写法会解析失败。"
        ),
    },
    {
        "version": ITEM_COMPONENTS_CUTOVER,
        "title": "物品 NBT → 物品组件（本模块的核心）",
        "detail": (
            "1.20.4 及以下：`netherite_sword{Enchantments:[{id:\"minecraft:sharpness\",lvl:5}]}`；"
            "1.20.5 及以上：`netherite_sword[enchantments={levels:{\"minecraft:sharpness\":5}}]`。"
            "同批改名：`CustomModelData`→`custom_model_data`、"
            "`AttributeModifiers`→`attribute_modifiers`、`display.Name`→`custom_name`。"
        ),
    },
    {
        "version": (1, 21),
        "title": "附魔 ID 改名 sweeping → sweeping_edge",
        "detail": (
            "横扫之刃在 1.20.6 及以下叫 `minecraft:sweeping`，"
            "1.21 起叫 `minecraft:sweeping_edge`。写错的那个版本会报 unknown enchantment。"
        ),
    },
    {
        "version": (1, 21, 2),
        "title": "属性 ID 去掉分组前缀",
        "detail": (
            "`minecraft:generic.movement_speed` → `minecraft:movement_speed`；"
            "`horse.jump_strength` / `player.block_break_speed` 等前缀一并去掉。"
        ),
    },
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
    return {
        "known": info.known,
        "version": version_text(info.mc),
        "raw": info.raw,
        "source": info.source,
        "source_label": info.source_label,
        "item_syntax": syntax,
        "item_syntax_source": syntax_source,
        "cutover": version_text(ITEM_COMPONENTS_CUTOVER),
        "enchant_renames": [
            {"wrong": w, "right": r} for w, r in enchantment_renames_for(info.mc)
        ],
        "cutovers": [
            {"version": version_text(c["version"]), "title": c["title"]}
            for c in CUTOVERS
        ],
    }
