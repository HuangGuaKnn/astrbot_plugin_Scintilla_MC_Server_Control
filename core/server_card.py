# -*- coding: utf-8 -*-
"""实时服务器卡（v0.25.0）：四个字段、五种状态，一个都不许含糊。

背景
====
WebUI 的「服务器实时状态」原先只贴三行 kv：版本 / 游戏时间 / 在线人数。
三行都是**裸字符串** —— 谁也不知道那个值是怎么采的，拿不到时又为什么是空的。
于是同一类误读反复出现：

* 1.7.10 的 ``time query daytime`` 回的是用法文本 ``/time <set|add> <value>``，
  旧代码把它当「游戏时间」贴出去，看着像服务器抽风；
* 原版 / Fabric 没有 ``/tps``，回 ``Unknown command`` —— 旧代码把它和「探针还没回话」
  当成同一件事，用户不知道是自己没装探针，还是插件坏了；
* 0 人在线的空服只显示「0 / 20」，看起来像掉线；
* 最要命的一类：服务端**明明回了话**，只是这句话我们没见过 ——
  旧代码一律吞掉当「不支持」，用户于是去查文档、改配置，而真相只是没见过这个回法。

本模块把这件事收口成一张**卡**：

* 四个字段：在线人数 / 游戏内时间 / TPS / 版本与世代；
* 每个字段都标**采样来源**（``/list`` · ``time query daytime`` · ``/tps`` ·
  服务端文件探测或手动声明）；
* 每个字段都落一个**状态**，状态必须分开说、不许合并：

======================  ==============================================================
``ok``                  正常：拿到回包并解析成功
``empty``               ⚪ 空服（0 人在线）—— 这是**正常状态**，不是错误
``unsupported``         🟡 该世代不支持（命令不存在 / 回了用法文本 / 语法被拒）
``unrecognized``        🟠 回执认不出 —— 连上了、也回话了，但解析不了，**必须带原文**
``unreachable``         🔴 连不上（RCON 断 / 超时 / 命令抛异常）
``no_source``           版本字段专用：既没探测到、也没手动声明（**不是**「回执认不出」）
======================  ==============================================================

判据的分界线（最容易被写错的地方）
==================================
「该世代不支持」与「回执认不出」**不许混**：

* 前者只能由**确定性证据**得出，且证据全部来自 :mod:`core.command_result`，
  本模块不另写一套文本判据：
    1. 命令不存在（``Unknown command`` 行，行首锚定）；
    2. 用法文本（``/time <set|add> <value>`` —— 子命令不存在时服务端回的就是它）；
    3. 解析阶段被拒（``syntax_error``：命令存在，但这条写法在该世代不成立）。
* 其余一切「有回包但解析不出」**一律** ``unrecognized``，并把原文一起带出去。
  说错方向的代价是用户去改配置，而真相只是我们没见过这句话。

「连不上」与「空服」同样不许混：前者一个字节都没拿到（RCON 层问题：端口 / 密码 /
超时），后者拿到了、内容就是 0 人（什么都不用做）。

不猜
====
TPS 只有装了探针（Paper / Spigot 的 ``/tps``、部分模组的同名命令）才有。
没有就如实写「该世代不支持」—— 绝不拿「20 - MSPT/50」之类的东西倒推，
也绝不把 MSPT 换算成 TPS 假装有数据。版本同理：未知就写未知，
不按「大概率是 1.20」给世代。
"""
from __future__ import annotations

import re
import time

from .command_result import (
    classify_command_output,
    daytime_clock,
    is_unknown_command_output,
    parse_daytime_reply,
)
from .version_caps import (
    ITEM_SYNTAX_COMPONENTS,
    ITEM_SYNTAX_LEGACY,
    ITEM_SYNTAX_PREFLATTEN,
    ITEM_SYNTAX_UNKNOWN,
    ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN,
)

# ===================== 状态（唯一一份） =====================

STATE_OK = "ok"
STATE_EMPTY = "empty"
STATE_UNSUPPORTED = "unsupported"
STATE_UNRECOGNIZED = "unrecognized"
STATE_UNREACHABLE = "unreachable"
STATE_NO_SOURCE = "no_source"

#: 状态 → 中文短标签。**前端只渲染这个字符串**，不许在前端另写一份措辞 ——
#: 否则同一种状态在页面和接口里叫两个名字，验收时就没人说得清哪边是对的。
STATE_LABEL = {
    STATE_OK: "正常",
    STATE_EMPTY: "空服",
    STATE_UNSUPPORTED: "该世代不支持",
    STATE_UNRECOGNIZED: "回执认不出",
    STATE_UNREACHABLE: "连不上",
    STATE_NO_SOURCE: "无来源",
}

#: 状态图例（给 UI 与文档共用；顺序即阅读顺序）
STATE_LEGEND = (
    (STATE_OK, "正常"),
    (STATE_EMPTY, "空服（0 人，正常）"),
    (STATE_UNSUPPORTED, "该世代不支持"),
    (STATE_UNRECOGNIZED, "回执认不出（附原文）"),
    (STATE_UNREACHABLE, "连不上"),
    (STATE_NO_SOURCE, "无来源"),
)

#: 四个字段的固定顺序与采样来源（顺序即 UI 顺序，别在别处再排一次）
FIELD_SPECS = (
    ("online", "在线人数", "/list"),
    ("daytime", "游戏内时间", "time query daytime"),
    ("tps", "TPS", "/tps（探针）"),
    ("version", "版本与世代", "服务端文件探测 / 手动声明"),
)

#: 物品语法世代 → 人话（UI 与验收单共用）
GENERATION_LABEL = {
    ITEM_SYNTAX_PREFLATTEN: "预扁平化（1.13 以下）",
    ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN: "预扁平化（未确认）",
    ITEM_SYNTAX_LEGACY: "NBT 世代（1.13 – 1.20.4）",
    ITEM_SYNTAX_COMPONENTS: "物品组件世代（1.20.5+）",
    ITEM_SYNTAX_UNKNOWN: "世代未知",
}

# ===================== 回包清洗 =====================

#: MC 的颜色 / 格式码（``§a`` / ``§r`` …）。Paper 的 ``/tps`` 回包整段都带它，
#: 不清掉就解析不出数字 —— 这不是「认不出」，是我们自己的脏活。
_COLOR_CODE_RE = re.compile("\u00a7.", re.UNICODE)


def strip_color_codes(text: str) -> str:
    """去掉 ``§x`` 颜色 / 格式码（不做其它任何改写）。"""
    return _COLOR_CODE_RE.sub("", str(text or ""))


# ===================== `/list` =====================

#: 1.16+ 原版：``There are 2 of a max of 20 players online: Steve, Alex``
#: 1.13 – 1.15 原版：``There are 2 of a max 20 players online:`` —— **没有第二个 of**。
#: 这条是 2026-11-17 八台真机验收抓出来的（1.13.2 / 1.14.4 回包原样），
#: 旧正则写死了 ``max of``，于是这两台的在线人数一直静默显示 0 / 0。
#: 所以第二个 ``of`` 必须**可选** —— 「同一个意思的两种写法」不是两种命令。
_LIST_MODERN_RE = re.compile(
    r"there\s+are\s+(\d+)\s+of\s+a\s+max(?:imum)?\s+(?:of\s+)?(\d+)", re.I
)
#: 1.7.10 ~ 1.15（以及 Bukkit 系列）：``There are 2/20 players online: Steve``
_LIST_LEGACY_RE = re.compile(r"there\s+are\s+(\d+)\s*/\s*(\d+)", re.I)
#: 中文服务端（原版 zh_cn）：``有 2 个玩家在线，最多 20``
_LIST_ZH_RE = re.compile(r"有\s*(\d+)\s*个玩家[^\d]{0,8}(\d+)", re.I)


def parse_list_reply(raw: str) -> dict | None:
    """解析 ``/list`` 回包 → ``{"online", "max", "players"}``；**解析不出返回 None**。

    认三种形态（都是真机 / 原版语言文件里的原样，不是猜的）：

    * ``There are 2 of a max of 20 players online: Steve, Alex`` —— 1.16+；
    * ``There are 2/20 players online: Steve`` —— 1.7.10 ~ 1.15 与 Bukkit 系列；
    * ``有 2 个玩家在线，最多 20`` —— 中文服务端。

    返回 None 的情形（交给 :func:`judge_reply` 判 ``unrecognized`` 并带原文）：
    中文以外的本地化、模组改了措辞、回包被截断、空回包。
    """
    text = strip_color_codes(raw).strip()
    if not text:
        return None
    online = maxp = None
    for rx in (_LIST_MODERN_RE, _LIST_LEGACY_RE, _LIST_ZH_RE):
        m = rx.search(text)
        if m:
            online, maxp = int(m.group(1)), int(m.group(2))
            break
    if online is None:
        return None
    players: list[str] = []
    # 玩家名单只在英文形态里有：`players online:` 之后用逗号分隔。
    # 中文服务端把人数写在句子里、名单另起一句，这里不硬凑 —— 名单空了就是空了，
    # 绝不把「最多 20」当成一个叫「20」的玩家。
    colon = max(text.rfind(":"), text.rfind("："))
    if colon != -1:
        tail = text[colon + 1:].strip()
        if tail:
            players = [p.strip() for p in re.split(r"[,，]", tail) if p.strip()]
    return {"online": online, "max": maxp, "players": players}


# ===================== `/tps` =====================

#: Paper / Spigot 系：``TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0``
_TPS_PAPER_RE = re.compile(r"tps\s+from\s+last\s+1m\s*,\s*5m\s*,\s*15m\s*[:：]\s*(.+)", re.I)
_FLOAT_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_tps_reply(raw: str) -> dict | None:
    """解析 ``/tps`` 回包 → ``{"1m", "5m", "15m"}``（缺的位为 None）；认不出返回 None。

    认两种形态：

    * Paper / Spigot：``TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0``
      （带 ``§`` 颜色码，先清掉再解析）；
    * 其它探针：**含 ``tps`` 字样的那一行**里的浮点数，取前三个。

    第二种是刻意放宽的：模组探针的措辞五花八门（``Server TPS: 20.0`` /
    ``TPS: 20.0 20.0 20.0``），只认 Paper 一种会把「装了探针」误报成「不支持」。
    但**只扫含 ``tps`` 的那一行**，不扫整段 —— 否则任何回包里的数字都能被当成 TPS。

    一个数字都取不到（``Unknown command`` / ``Unknown commandtps<--[HERE]`` /
    空回包）→ None，由 :func:`judge_reply` 按证据判「不支持」还是「认不出」。
    """
    text = strip_color_codes(raw).strip()
    if not text:
        return None
    m = _TPS_PAPER_RE.search(text)
    if m:
        nums = [float(x) for x in _FLOAT_RE.findall(m.group(1))]
    else:
        line = next((l for l in text.splitlines() if "tps" in l.lower()), "")
        nums = [float(x) for x in _FLOAT_RE.findall(line)] if line else []
    nums = [n for n in nums if 0.0 <= n <= 1000.0][:3]
    if not nums:
        return None
    vals = (nums + [None, None, None])[:3]
    return {"1m": vals[0], "5m": vals[1], "15m": vals[2]}


# ===================== 用法文本（「该世代不支持」的确定性证据之一） =====================

_USAGE_PREFIXES = ("usage:", "用法:", "语法:", "用法：", "语法：")


def looks_like_usage_reply(raw: str) -> bool:
    """回包是不是**命令用法提示**（= 这条子命令在该服务端不存在）。

    依据（1.7.10 真机）：``time query daytime`` 在该世代回的是
    ``/time <set|add> <value>`` —— 没有 ``Unknown command`` 那套话，
    只有一行用法。把它归到「认不出」会让用户以为插件不认识这个回包，
    而归到「该世代不支持」才是事实：那个子命令压根没有。
    """
    low = strip_color_codes(raw).strip().lower()
    if not low:
        return False
    if low.startswith(_USAGE_PREFIXES):
        return True
    return low.startswith("/") and ("<" in low or "[" in low)


# ===================== 回包 → 状态（唯一判定入口） =====================


def judge_reply(command: str, raw: str, *, parsed: bool, error: str = "") -> tuple[str, str]:
    """把一次探针结果判成 ``(状态, 说明)`` —— **状态判定的唯一实现**。

    ``parsed`` 表示调用方是否已从 ``raw`` 里解析出数据；``error`` 是命令抛出的异常文本。
    判定顺序（每一步的位置都有理由）：

    1. 有异常 → ``unreachable``（一个字节都没拿到，与「回包认不出」是两件事）；
    2. 解析成功 → ``ok``；
    3. 回包是用法文本 → ``unsupported``（子命令不存在，服务端只回用法）；
    4. ``Unknown command``（命令不存在）→ ``unsupported``；
    5. ``syntax_error``（命令存在、写法被拒）→ ``unsupported``；
    6. 空回包 → ``unrecognized``（服务端一个字都没回，这既不是「不支持」也不是「没数据」）；
    7. 其它明确失败（权限 / 被拒）→ ``unrecognized``：这不是「世代不支持」，
       而是「回了话但拿不到数据」，原文里才有真原因；
    8. 兜底 → ``unrecognized``。

    注意第 4 步用的是 ``is_unknown_command_output()``（行首锚定的「命令不存在」），
    **不是** ``confirmed_not_run`` —— 后者把「权限不足 / 玩家不存在」也算成「没执行」，
    拿它当「命令不存在」的证据会把 ``You do not have permission`` 说成「该世代不支持」。
    状态措辞必须与证据一一对应，否则用户会照着错的话去改错的东西。
    """
    if error:
        return STATE_UNREACHABLE, str(error).strip() or "RCON 命令执行失败"
    if parsed:
        return STATE_OK, ""
    text = strip_color_codes(raw).strip()
    if text and looks_like_usage_reply(text):
        return STATE_UNSUPPORTED, "服务端回的是用法文本 —— 这条子命令在该世代不存在"
    if text and is_unknown_command_output(text):
        return STATE_UNSUPPORTED, "服务端回了「命令不存在」（该世代没有这条命令）"
    verdict = classify_command_output(
        command, raw, boundary_confirmed=True, response_received=True
    )
    if verdict.status == "syntax_error":
        return STATE_UNSUPPORTED, "命令存在，但服务端拒绝这种写法（该世代语法不同）"
    if not text:
        return STATE_UNRECOGNIZED, "服务端没有回任何文本（空回包）"
    if verdict.status == "failed":
        return STATE_UNRECOGNIZED, "服务端明确拒绝执行（原因见原文，可能是权限或配置）"
    return STATE_UNRECOGNIZED, "回包认不出（没有命中任何已知形态）"


# ===================== 字段构造 =====================


def _field(
    key: str,
    state: str,
    value: str,
    *,
    note: str = "",
    raw: str = "",
    extra: dict | None = None,
) -> dict:
    """造一个字段。标签与来源从 :data:`FIELD_SPECS` 取 —— 不许调用方另起一套措辞。"""
    spec = next((s for s in FIELD_SPECS if s[0] == key), None)
    label, source = (spec[1], spec[2]) if spec else (key, "")
    out = {
        "key": key,
        "label": label,
        "state": state,
        "state_label": STATE_LABEL.get(state, state),
        "value": value,
        "source": source,
        "note": note,
    }
    # 原文只在**需要解释**的状态下带出（不支持 / 认不出）：
    # 正常态对用户没用，只会把卡撑长；空服本身已经说清了（0 / 20），原文是噪音；
    # 连不上则连一个字节都没拿到，没有原文可给（原因在 note 里）。
    # 而「不支持 / 认不出」时原文就是唯一线索 —— 这是验收项，不是可选项。
    if state in (STATE_UNSUPPORTED, STATE_UNRECOGNIZED) and raw:
        out["raw"] = strip_color_codes(raw).strip()
    if extra:
        out.update(extra)
    return out


def _unreachable_fields(reason: str) -> list[dict]:
    return [
        _field(key, STATE_UNREACHABLE, "—", note=reason)
        for key, _label, _src in FIELD_SPECS
        if key != "version"
    ]


# ===================== 各字段探针 =====================


async def _probe(rcon, command: str) -> tuple[str, str]:
    """跑一条探针，返回 ``(原文, 异常文本)`` —— 异常不往上抛，交给状态机判。"""
    try:
        return str(await rcon.command(command) or ""), ""
    except Exception as e:  # noqa: BLE001 —— 探针失败是**状态**，不是崩溃
        return "", f"{type(e).__name__}: {e}".strip()


async def _probe_online(rcon) -> dict:
    raw, err = await _probe(rcon, "list")
    parsed = parse_list_reply(raw) if not err else None
    if parsed is not None:
        online, maxp = parsed["online"], parsed["max"]
        value = f"{online} / {maxp}" if maxp else str(online)
        state = STATE_EMPTY if online == 0 else STATE_OK
        note = "服务器当前没有玩家在线 —— 空服是正常状态，不是错误" if state == STATE_EMPTY else ""
        return _field("online", state, value, note=note, raw=raw,
                      extra={"online": online, "max": maxp, "players": parsed["players"]})
    state, note = judge_reply("list", raw, parsed=False, error=err)
    return _field("online", state, "—", note=note, raw=raw)


async def _probe_daytime(rcon) -> dict:
    raw, err = await _probe(rcon, "time query daytime")
    ticks = parse_daytime_reply(raw) if not err else None
    if ticks is not None:
        return _field("daytime", STATE_OK, f"{daytime_clock(ticks)}", note=f"{ticks} tick（0 tick = 清晨 06:00）",
                      extra={"ticks": ticks})
    state, note = judge_reply("time query daytime", raw, parsed=False, error=err)
    return _field("daytime", state, "—", note=note, raw=raw)


async def _probe_tps(rcon) -> dict:
    raw, err = await _probe(rcon, "tps")
    parsed = parse_tps_reply(raw) if not err else None
    if parsed is not None:
        nums = [parsed["1m"], parsed["5m"], parsed["15m"]]
        shown = [f"{n:.1f}" for n in nums if n is not None]
        note = "1 分钟 / 5 分钟 / 15 分钟均值"
        low = any(n is not None and n < 18.0 for n in nums)
        if low:
            note += " · 低于 18，服务端可能吃力"
        return _field("tps", STATE_OK, " / ".join(shown), note=note, extra={"tps": parsed, "low": low})
    state, note = judge_reply("tps", raw, parsed=False, error=err)
    return _field("tps", state, "—", note=note, raw=raw)


def _version_field(plugin) -> dict:
    """版本与世代：**不依赖 RCON**（读的是配置 + 服务端本地文件）。"""
    try:
        caps = plugin._version_capabilities()
    except Exception as e:  # noqa: BLE001
        return _field("version", STATE_NO_SOURCE, "—", note=f"读取版本能力失败：{e}")
    caps = caps or {}
    syntax = str(caps.get("item_syntax") or ITEM_SYNTAX_UNKNOWN)
    generation = GENERATION_LABEL.get(syntax, syntax)
    source = str(caps.get("source") or "unknown")
    source_label = str(caps.get("source_label") or "")
    if caps.get("known"):
        value = str(caps.get("raw") or caps.get("version") or "—")
        note = f"{source_label} · {generation}"
        return _field("version", STATE_OK, value, note=note,
                      extra={"generation": generation, "item_syntax": syntax, "caps_source": source})
    # 探测失败且未手动声明：**不是**「回执认不出」（根本没有回执可认），单列 no_source。
    hint = str(caps.get("support_note") or "").strip()
    note = "探测不到版本、也没有手动声明" + (f"（{hint}）" if hint else "")
    return _field("version", STATE_NO_SOURCE, "—", note=note,
                  extra={"generation": generation, "item_syntax": syntax, "caps_source": source})


# ===================== 卡片级结论 =====================


def overall_state(fields: list[dict]) -> str:
    """卡片级状态：**在线人数**决定头条；只有「数据可信度」类状态能改写它。

    三种状态参与头条，其余一律不进：

    * ``unreachable`` / ``unrecognized`` —— 它们意味着**这张卡本身的数据不可信**，
      必须顶到头条（用户不该拿一份看不懂的回执当结论）；
    * ``empty`` —— 0 人是事实，头条如实说「空服」；
    * 其余（``ok`` / ``unsupported`` / ``no_source``）不进头条。

    为什么 ``unsupported`` 不进头条（2026-11-17 八台真机验收现场改的口径）：
    原版 / Fabric 没有 ``/tps``、1.7.10 没有 ``time query`` —— 这是**这台服务器的
    结构性事实**，不是「有问题」。八台里七台有玩家在线、一切正常，旧口径却把整张卡
    的头条写成「该世代不支持」，第一眼就会被读成故障。能力缺失在**该字段自己那行**
    说清楚就够了，摘要里也照旧统计（「2 项正常 · 该世代不支持 2 项」）。

    版本字段（``version``）同样不参与头条：它既不来自 RCON，也与服务器此刻活得好不好无关。
    """
    live = [f for f in fields if f.get("key") != "version"]
    by_key = {f.get("key"): f.get("state") for f in live}
    online = by_key.get("online")
    if online in (STATE_UNREACHABLE, STATE_UNRECOGNIZED, STATE_EMPTY):
        return online
    # 在线人数拿到了，再看其余字段里有没有「数据不可信」
    for st in (STATE_UNREACHABLE, STATE_UNRECOGNIZED):
        if st in by_key.values():
            return st
    return STATE_OK


def summary_line(fields: list[dict]) -> str:
    """一句话概括（UI 头条 / 回执 / 验收单共用）。"""
    counts: dict[str, int] = {}
    for f in fields:
        counts[str(f.get("state"))] = counts.get(str(f.get("state")), 0) + 1
    total = len(fields)
    if counts.get(STATE_OK, 0) == total:
        return f"{total} 项全部正常"
    parts = [f"{STATE_LABEL.get(st, st)} {n} 项" for st, n in counts.items() if st != STATE_OK]
    ok = counts.get(STATE_OK, 0)
    head = f"{ok} 项正常 · " if ok else ""
    return head + " · ".join(parts)


def build_payload(fields: list[dict], *, rcon_connected: bool) -> dict:
    """把字段列表收成接口载荷（字段顺序固定为 :data:`FIELD_SPECS`）。"""
    order = {key: i for i, (key, _l, _s) in enumerate(FIELD_SPECS)}
    ordered = sorted(fields, key=lambda f: order.get(str(f.get("key")), 99))
    state = overall_state(ordered)
    return {
        "ok": True,
        "overall": state,
        "overall_label": STATE_LABEL.get(state, state),
        "summary": summary_line(ordered),
        # 有任何一项不是 ok 就算「有话说」—— UI 据此决定要不要展开细节
        "degraded": any(f.get("state") != STATE_OK for f in ordered),
        "rcon_connected": bool(rcon_connected),
        "sampled_at": time.strftime("%H:%M:%S"),
        "fields": ordered,
        "legend": [{"state": st, "label": lb} for st, lb in STATE_LEGEND],
    }


def unreachable_payload(plugin, reason: str) -> dict:
    """RCON 层已经确认连不上时，直接造一张「全项连不上」的卡。

    与 :func:`sample` 的异常分支共用 —— 调用方（WebUI 的 status 接口）在
    ``_get_rcon()`` 已经抛过的前提下**不该再连一次**：重连一次要再等一个超时，
    而结论不会变。版本字段照旧给（它本来就不依赖 RCON）。
    """
    return build_payload(_unreachable_fields(reason) + [_version_field(plugin)], rcon_connected=False)


async def sample_with(rcon, plugin) -> dict:
    """用**已有**的 RCON 连接采样四个字段（调用方已持有连接时用这个）。

    单独开这个入口是为了「一次采样喂多个消费者」：``/server/status`` 既要用
    ``list`` / ``daytime`` 这些历史字段、又要给实时服务器卡 —— 采样两遍等于
    对服务端连打 6 条探针，而且两个消费者可能看到**不同时刻**的数据。
    """
    version = _version_field(plugin)
    if rcon is None:
        return build_payload(_unreachable_fields("RCON 未连接") + [version], rcon_connected=False)
    fields = [await _probe_online(rcon), await _probe_daytime(rcon), await _probe_tps(rcon)]
    return build_payload(fields + [version], rcon_connected=True)


async def sample(plugin) -> dict:
    """采样四个字段，返回卡片载荷（**永不抛异常** —— 连不上也是一种结果）。"""
    try:
        rcon = await plugin._get_rcon()
    except Exception as e:  # noqa: BLE001
        return unreachable_payload(plugin, f"RCON 连接失败：{e}")
    return await sample_with(rcon, plugin)
