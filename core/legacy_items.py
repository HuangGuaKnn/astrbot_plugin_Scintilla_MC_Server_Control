"""B5 · 预扁平化（1.13 以下）物品命令生成与校验。

证据基座（2026-10-07 两代真机实测，见 ``docs/_internal/DESIGN_B5_legacy_item_mapping.md``）：

1. ``give`` 官方写法在 1.7.10 与 1.12.2 **一字不差**：
   ``give <player> <item> [amount] [data] [dataTag]``（数据值仍在位置参数上）。
2. 变体物品**必须**写「家族名 + 数据值」：``minecraft:wool`` + ``14``。
   1.13+ 的扁平名（``red_wool`` / ``oak_planks`` / ``grass_block``）在本世代**不存在**，
   服务端回 ``There is no such item with name …``。
3. ⚠️ **域外数据值服务端不报错，却会静默回落**，而且可能让**客户端渲染崩溃**
   （B13 血账：``double_plant`` + 11 → ``ArrayIndexOutOfBoundsException`` → 一进服必崩）。
   因此域校验必须在本层硬做：**只允许域表里确有展示名的数据值**。
4. 附魔写在物品 ID 之后的 ``{}`` 里、用**数字附魔 ID**：``{ench:[{id:16,lvl:5}]}``（锋利 = 16）。
5. 数字物品 ID 已废弃（1.12.2 起服务端把它当名字解析并直接报错）→ 一律用注册名。

本模块只做**纯计算**（不碰 RCON / 不碰文件），便于单测；调用方拿到命令后再走
``_guard_command_for_version(validated_item=True)`` 放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 1.13 以前玩家名上限 16 字符；同世代的合法字符集为 ``[A-Za-z0-9_]``。
#: （名字里带空格 / 引号 / 命令分隔符一律拒绝 —— 这层是**注入防线**。）
import re as _re

PLAYER_NAME_RE = _re.compile(r"^[A-Za-z0-9_]{1,16}$")

#: ``give`` 单次数量上限（保守：原版上限 64×36 = 2304；这里给足余量但仍设硬顶）。
MAX_COUNT = 6400
#: v0.24.2（GPT 全面复核 F18）：通过生成链**不等于** give 语法接受 —— 任务总量可以很大，
#: 但**每条命令**必须落进该代解析范围。实证（真实字节码，GPT 复核材料）：
#:   · 1.7.10  give 写死 ``at.a(ac, args[2], 1, 64)`` —— 数量范围 1..64，65 直接用法错误；
#:   · 1.12.2  ``cm.a(args[2], 1, item.j())`` —— 上界是**物品自己的最大堆叠**
#:             （非堆叠物为 1），钻石一类仍是 64。
#: 注：1.12.2 的权威值是 ``getMaxStackSize``，插件侧拿不到物品注册表里的那个字段，
#: 于是对「确定非堆叠」的家族（工具/护甲/弓/钓竿/打火石/剪刀/盾）保守收到 1 ——
#: 方向是 fail-closed（宁可让用户拆单，不放行必然违规的条数）。
MAX_COUNT_BY_GEN = {"1.7.10": 64, "1.12.2": 64}   # 堆叠物按 64（= 该代最大堆叠）
_NON_STACKABLE_RE = _re.compile(
    r"_(sword|pickaxe|axe|shovel|hoe|helmet|chestplate|leggings|boots)$"
    r"|(^|:)(bow|flint_and_steel|shears|fishing_rod|shield)$"
)

#: NBT 串长度硬顶（防病态输入）。
MAX_NBT_LEN = 1024

#: 数字附魔 ID 的合法区间（pre-1.13 是数字表，0~255 足够宽松）。
_ENCH_ID_RANGE = (0, 255)


@dataclass
class LegacyGivePlan:
    """一次预扁平化 give 的规划结果。``ok=False`` 时 ``reason`` 是给人看的拒绝原因。"""

    ok: bool
    command: str = ""
    reason: str = ""
    item_id: str = ""
    damage: "int | None" = None
    display: str = ""
    notes: list = field(default_factory=list)


def _fail(reason: str) -> LegacyGivePlan:
    return LegacyGivePlan(ok=False, reason=reason)


def is_preflatten(dictionary) -> bool:
    """词典是否已装载某个预扁平化世代（B5 通道是否可用）。"""
    return bool(getattr(dictionary, "legacy_generation", ""))


def generation_of(dictionary) -> str:
    return str(getattr(dictionary, "legacy_generation", "") or "")


def resolve_entry(dictionary, item: str):
    """把「用户怎么说」解析成词典条目。

    返回 ``(entry_id, entry, hit_damage, hit_display)``；解析不到返回 ``(None, None, None, None)``。
    顺序：直接 ID（``minecraft:x`` 或裸 ``x``）→ 词典模糊搜索（含变体展示名）。
    """
    raw = str(item or "").strip()
    if not raw:
        return (None, None, None, None)
    items = getattr(dictionary, "items", {}) or {}
    # ① 直接命中 ID（带或不带命名空间）
    for cand in (raw.lower(), "minecraft:" + raw.lower().lstrip("minecraft:")):
        if cand in items:
            return (cand, items[cand], None, None)
    # ② 词典搜索（B5 已支持变体展示名并带回数据值）
    try:
        rows = dictionary.search_items(raw, limit=1)
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return (None, None, None, None)
    row = rows[0]
    rid = row.get("id")
    return (rid, items.get(rid) or row, row.get("variant_damage"), row.get("variant_display"))


def validate_nbt(nbt: str) -> "str | None":
    """校验手写 NBT 串；合法返回空串，否则返回原因。"""
    s = str(nbt or "").strip()
    if not s:
        return ""
    if len(s) > MAX_NBT_LEN:
        return f"NBT 过长（> {MAX_NBT_LEN} 字符）"
    if not (s.startswith("{") and s.endswith("}")):
        return "NBT 必须写成 `{...}` 复合标签"
    if s.count("{") != s.count("}"):
        return "NBT 花括号不配对"
    if "\n" in s or "\r" in s:
        return "NBT 不得含换行"
    return ""


def build_enchant_nbt(pairs) -> str:
    """``[(附魔数字ID, 等级), …]`` → ``{ench:[{id:16,lvl:5}]}``。

    任一项不合法抛 ``ValueError`` —— 不合法就别生成（呼应 B13：宁拦不发错）。
    """
    out = []
    for eid, lvl in pairs:
        e, l = int(eid), int(lvl)
        if not (_ENCH_ID_RANGE[0] <= e <= _ENCH_ID_RANGE[1]):
            raise ValueError(f"附魔数字 ID 越界：{e}（合法 {_ENCH_ID_RANGE[0]}~{_ENCH_ID_RANGE[1]}）")
        if not (1 <= l <= 255):
            raise ValueError(f"附魔等级越界：{l}（合法 1~255）")
        out.append("{{id:{},lvl:{}}}".format(e, l))
    if not out:
        raise ValueError("附魔列表为空")
    return "{ench:[" + ",".join(out) + "]}"


def plan_give(dictionary, player: str, item: str, count: int = 1,
              damage: "int | None" = None, nbt: str = "") -> LegacyGivePlan:
    """规划一条 1.13 以下的 ``give`` 命令；**任一步缺证据就 fail-closed**。

    判据链（全部有真机实证）：
    1. 玩家名合语法（注入防线）；
    2. 数量 1~MAX_COUNT；
    3. 物品能在词典里解析成**注册名**（不是 1.13+ 扁平名）；
    4. 变体族：数据值必须**在域表里确有展示名**（``variants`` 命中）——
       域外值服务端不报错但会静默回落 / 崩客户端，因此一律拒绝；
    5. 非变体物品：只接受数据值 0（或省略）；
    6. NBT：结构合法（花括号配对、长度上限）。
    """
    if not PLAYER_NAME_RE.match(str(player or "").strip()):
        return _fail("玩家名不合法（1.13 以前只允许 1~16 位字母/数字/下划线）")
    try:
        n = int(count)
    except Exception:  # noqa: BLE001
        return _fail("数量必须是整数")
    if not (1 <= n <= MAX_COUNT):
        return _fail(f"数量越界（1~{MAX_COUNT}）")

    rid, entry, hit_dmg, hit_disp = resolve_entry(dictionary, item)
    if not rid or entry is None:
        gen = generation_of(dictionary) or "未知"
        return _fail(f"词典里没有该物品（世代 {gen}）；请先 mc_search_item 确认它在本版本存在")

    # v0.24.2（F18）：**单条命令**还要过该代解析范围（见 MAX_COUNT_BY_GEN 注释）
    gen = generation_of(dictionary) or ""
    for g, cap in MAX_COUNT_BY_GEN.items():
        if gen.startswith(g):
            if n > cap:
                return _fail(
                    f"{gen} 的 give 单条数量上限是 {cap}（服务端解析范围实证），"
                    f"一次给 {n} 会被拒；请拆成多条，每条不超过 {cap}"
                )
            break
    if gen.startswith("1.12") and _NON_STACKABLE_RE.search(rid or "") and n > 1:
        return _fail(
            f"{gen} 里 {rid} 是非堆叠物品（最大堆叠 1），单条数量只能为 1；"
            f"需要 {n} 件请拆成 {n} 条命令（脚本侧会逐条记账，不会重复发放）"
        )

    variants = dict(entry.get("variants") or {})
    notes: list = []
    if variants:
        # ---- 变体族：必须落到域表里有展示名的数据值上 ----
        if damage is None:
            if hit_dmg is not None:
                damage = int(hit_dmg)
            elif "0" in variants:
                damage = 0
            else:
                damage = min(int(k) for k in variants)
        try:
            damage = int(damage)
        except Exception:  # noqa: BLE001
            return _fail("数据值必须是整数")
        if str(damage) not in variants:
            up = entry.get("domain_upper")
            if isinstance(up, int) and up == 0:
                top = "，本族合法数据值仅 0"
            elif isinstance(up, int) and up > 0:
                top = f"，本族合法数据值 0~{up}"
            else:
                top = ""
            return _fail(
                f"数据值 {damage} 不在 {rid} 的合法域内{top}。"
                "域外值服务端不报错却会静默回落，且可能让客户端渲染崩溃 —— 已拒绝生成。"
            )
        display = variants.get(str(damage)) or hit_disp or rid
        notes.append(f"变体族：本世代没有扁平名，必须用「家族名 + 数据值」（{rid} + {damage}）")
    else:
        if damage not in (None, 0, "0"):
            return _fail(f"{rid} 不是变体族，不接受数据值 {damage}")
        damage = None
        display = entry.get("en") or rid

    bad_nbt = validate_nbt(nbt)
    if bad_nbt:
        return _fail(bad_nbt)

    parts = ["give", str(player).strip(), rid, str(n)]
    if damage is not None:
        parts.append(str(damage))
    if str(nbt or "").strip():
        if damage is None:
            parts.append("0")          # NBT 在数据值之后，缺位要补 0
        parts.append(str(nbt).strip())
    return LegacyGivePlan(
        ok=True, command=" ".join(parts), item_id=rid, damage=damage,
        display=display, notes=notes,
    )


def render_preview(dictionary, player: str, item: str, count: int = 1,
                   damage: "int | None" = None, nbt: str = "") -> str:
    """给人看的单行预览（成功显示命令与展示名，失败显示拒绝原因）。"""
    plan = plan_give(dictionary, player, item, count=count, damage=damage, nbt=nbt)
    if plan.ok:
        tail = f"（{plan.display}）" if plan.display else ""
        return f"{plan.command}{tail}"
    return f"[拒绝] {plan.reason}"
