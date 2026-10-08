"""B5 · 旧版（1.13 以下）物品**真实堆叠上限**表。

为什么非要有这张表
==================
``give <player> <item> <count>`` 的单条 count 上界**不是常数**：

* 1.12.2 —— ``CommandGive`` 真实字节码 ``cm.a(args[2], 1, item.j())``：
  上界是**物品自己的** ``getMaxStackSize()``（非堆叠物 = 1）；
* 1.7.10 —— ``at.a(ac, args[2], 1, 64)``：**与物品无关**，固定 1..64。
  所以 1.7.10 里给钻石剑 2 件是「解析层合法」（服务端只在堆叠渲染上限着），
  插件照旧放行 —— 这是**正确行为**，不要顺手把它收紧。

旧实现只有一个正则猜「工具 / 护甲 = 1」，于是两头漏（GPT 全面复核 N08）：

* 真实上限 1、名字却**不带** ``_sword/_helmet`` 之类特征的物品（蛋糕、床、
  附魔书、书与笔、药水、牛奶桶、矿车、鞘翅、不死图腾、马铠…）在 ``count=2``
  时仍被放行；
* 真实上限 16 的物品（空桶、成书、盔甲架、雪球、鸡蛋、末影珍珠、告示牌、
  旗帜）在 ``count=17`` 时仍被放行。

两者都会让服务端**当场报错**（不静默、不吞物，所以要害等级低于 B13 那类静默
回落），但用户白等一次往返、机器侧白吃一条命令。本表就是把这个上界钉死。

证据基座
========
1. 两代 ``CommandGive`` 真实字节码（GPT 复核材料，见上面两条）；
2. 原版堆叠表（Minecraft Wiki「Stacking」+ Java 版 1.12 物品表）：
   非 64 的物品只有三类 —— 上限 1、上限 16、上限 8（8 是历史遗留，1.12 已无）；
3. 家族级判据只用在**整族同值**的大类上（工具 / 护甲 / 矿车 / 船 / 唱片 /
   药水 / 潜影箱 / 马铠）：这类不是「逐名猜测」，是族铁律 ——
   例如原版里**每一个** ``_sword`` 都是 1，模组里继承 ``ItemSword`` 的同类
   也天然是 1，所以这种写法同时覆盖了模组物品里最常见的一批非堆叠物。

未知物品（模组物品）策略 —— fail-closed 的**取舍**，说清代价
==========================================================
插件离线拿不到模组物品的 ``getMaxStackSize``（它甚至可能是动态计算的）。
两种猜法各有代价：

* 猜 1（保守）：每一个模组材料 / 锭 / 矿石 ``count>1`` 都被自己拒掉 ——
  **正常玩法直接废掉**，用户会立刻关掉这个功能；
* 猜 64（原版默认）：真遇到模组非堆叠物时，服务端回一句参数错误 ——
  **一次可逆的失败**，什么都没有发出去。

取 64，但**不装懂**：``max_stack_size()`` 第二个返回值 ``verified`` 会说
「这条上限是确证过的还是按默认值兜的」，调用方据此在计划里留一句
「上限按原版默认 64 处理（模组物品真实堆叠未验证）」，让用户知情。

真机复核清单（B5 真机验收时顺手跑，就能判定本表是否失真）
======================================================
``REALCHECK_PROBES``：堆叠物 / 上限 16 / 上限 1 各挑代表，每条一次 RCON 即可
（给足该物品合法上限 → 应成功；多给 1 → 应被服务端以参数错误拒绝）。
"""

from __future__ import annotations

import re

#: 原版默认堆叠上限（绝大多数物品）。模组物品在拿不到真值时也用它。
MAX_STACK_DEFAULT = 64

#: **家族级铁律 ①**：工具 / 武器 / 护甲 / 弓 / 竿 / 剪刀 / 打火石 / 盾 ——
#: 原版里整族都是 1（带耐久），模组继承同基类的同类也天然是 1。
#: （这条是 v0.24.2 的 F18 判据，保留不动，只是从「唯一判据」降为「判据之一」。）
NON_STACKABLE_FAMILY_RE = re.compile(
    r"_(sword|pickaxe|axe|shovel|hoe|helmet|chestplate|leggings|boots)$"
    r"|(^|:)(bow|flint_and_steel|shears|fishing_rod|shield)$"
)

#: **家族级铁律 ②**（v0.24.3 · N08 新增）：另外几族在 1.12.2 里**整族**是 1 ——
#: 船（含 6 种木）、矿车（含箱子/熔炉/TNT/漏斗/命令方块款）、马铠、潜影箱
#: （含 16 色）、唱片、药水（含喷溅／滞留）。
#: 注意：裸名 ``minecraft:boat`` / ``minecart`` / ``potion`` / ``shulker_box``
#: 不带前导下划线，正则抓不到，另由具名表兜住。
FORCE_ONE_FAMILY_RE = re.compile(
    r"_(boat|minecart|horse_armor|shulker_box)$"
    r"|(^|:)record_[a-z0-9_]+$"
    r"|(^|:)(splash_|lingering_)?potion$"
)

#: 上限 **1** 的具名原版物品（名字不带任何家族特征的散点）。
MAX_STACK_1_NAMES = frozenset({
    # —— 食物 / 容器类 ——
    "bed", "cake", "mushroom_stew", "rabbit_stew", "beetroot_soup",
    # —— 书与特殊装备 ——
    "enchanted_book", "writable_book", "elytra", "saddle", "totem_of_undying",
    # —— 载具 / 药水 / 潜影箱裸名 ——
    "boat", "minecart", "potion", "shulker_box",
    # —— 地图（1.13 起空地图改为 64；1.12.2 仍是 1 → 列入真机复核清单）——
    "map", "filled_map",
    # —— 装液体的桶（空桶 ``bucket`` 是 16，装液体的一律 1）——
    "water_bucket", "lava_bucket", "milk_bucket",
})

#: 上限 **16** 的具名原版物品 —— 1.12.2 全表就这 8 个（GPT 复核 N08 逐条核对过）。
MAX_STACK_16_NAMES = frozenset({
    "bucket",        # 空桶
    "written_book",
    "armor_stand",
    "snowball",
    "egg",
    "ender_pearl",
    "sign",
    "banner",
})

#: 真机复核清单：``(注册名, 试探数量, 期望, 说明)``。
#: 每条一次 give 就能判定本表是否失真（堆叠物 / 16 / 1 三类各覆盖到）。
REALCHECK_PROBES = (
    ("minecraft:diamond", 64, True, "堆叠物：上限 64，给 64 应成功"),
    ("minecraft:diamond", 65, False, "堆叠物：给 65 应被服务端拒（解析上界 64）"),
    ("minecraft:snowball", 16, True, "上限 16：给 16 应成功"),
    ("minecraft:snowball", 17, False, "上限 16：给 17 应被服务端拒"),
    ("minecraft:cake", 1, True, "上限 1：给 1 应成功"),
    ("minecraft:cake", 2, False, "上限 1：给 2 应被服务端拒"),
)


def normalize(name: str) -> str:
    """取裸注册名：去命名空间、转小写、去空白。"""
    s = str(name or "").strip().lower()
    return s.split(":", 1)[1] if ":" in s else s


def max_stack_size(generation: str, registry_name: str) -> "tuple[int, bool]":
    """返回 ``(单条 count 上界, 是否确证)``。

    ``generation`` 是 ``legacy_generation``（如 ``"1.12.2"``）：
    ``1.7.10`` 一类走「与物品无关的固定 64」，判据不同、**不要混**。
    """
    gen = str(generation or "").strip()
    bare = normalize(registry_name)
    # 1.7.10 系的 give 是固定 1..64（与物品无关）→ 上界就是 64，且这是确证事实。
    if gen.startswith("1.7"):
        return (64, True)
    if not gen.startswith("1.12"):
        # 其它世代：没有该代 give 解析范围的实证 → 不装懂（默认值 + verified=False）。
        return (MAX_STACK_DEFAULT, False)
    if bare in MAX_STACK_1_NAMES:
        return (1, True)
    if bare in MAX_STACK_16_NAMES:
        return (16, True)
    if NON_STACKABLE_FAMILY_RE.search(bare) or FORCE_ONE_FAMILY_RE.search(bare):
        return (1, True)
    # 具名表 + 家族铁律都没命中 →
    #   · ``minecraft:``（或裸名）：原版的非 64 物品**只可能是**上面那几类，
    #     所以没命中就说明它确实是 64 —— 这是**确证**；
    #   · 其它命名空间 = 模组物品：本机没有它的 ``getMaxStackSize`` 实证 → 兜底。
    return (MAX_STACK_DEFAULT, _is_vanilla(registry_name))


def _is_vanilla(name: str) -> bool:
    """裸名或 ``minecraft:`` 命名空间都算原版（原版非 64 物品已在上表穷举）。"""
    s = str(name or "").strip().lower()
    return ":" not in s or s.startswith("minecraft:")
