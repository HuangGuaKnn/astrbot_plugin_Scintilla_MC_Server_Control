# -*- coding: utf-8 -*-
"""v0.25.0 实时服务器卡：四字段 · 五态 · 采样来源（离线判据 + 假 RCON 端到端）。

要解决的问题
============
WebUI 的「服务器实时状态」原先只贴三行裸字符串（版本 / 游戏时间 / 在线人数），
既没有采样来源，也分不清「没拿到」的四种完全不同的原因。用户看到空值只能猜。
本用例钉住新口径：

* **四个字段**：在线人数 / 游戏内时间 / TPS / 版本与世代，顺序固定，每项都带采样来源；
* **五态必须分开说**（合并任意两个 = 红）：

  ==============  ==========================================================
  ``ok``          拿到回包并解析成功
  ``empty``       ⚪ 空服（0 人在线）—— 正常状态，**不是**错误
  ``unsupported`` 🟡 该世代不支持（命令不存在 / 回了用法文本 / 语法被拒）
  ``unrecognized``🟠 回执认不出 —— 连上了、也回话了，但解析不了，**必须带原文**
  ``unreachable`` 🔴 连不上（RCON 断 / 超时 / 命令抛异常）
  ``no_source``   版本字段专用：既没探测到也没手动声明（**不是**「认不出」）
  ==============  ==========================================================

「能红」证据（每条都实跑过改坏 → 变红 → 字节级还原 → 回绿）
==========================================================
1. 把 ``judge_reply`` 里 ``is_unknown_command_output`` 那一步删掉 →
   `权限不足 ≠ 该世代不支持` 与 `Unknown command → unsupported` 两条同时红；
2. 把 ``_field`` 里 ``if state != STATE_OK and raw`` 改成 ``if raw`` 之外的写法
   （即丢掉原文）→ `认不出必须带原文` 红；
3. 把 ``overall_state`` 里 ``online == STATE_EMPTY`` 那一步删掉 →
   `空服不被 TPS 不支持污染头条` 红；
4. 把 ``parse_list_reply`` 的 ``_LIST_LEGACY_RE`` 删掉 →
   1.7.10 / 1.12.2 的 ``There are 0/20 players online:`` 从 `empty` 变 `unrecognized` → 红；
5. 把 ``_LIST_MODERN_RE`` 里可选的 ``(?:of\\s+)?`` 删掉 →
   1.13 – 1.15 的 ``There are 0 of a max 20 players online:`` 红
   （这条形态是 2026-11-17 八台真机验收抓出来的：1.13.2 / 1.14.4 的实际回包，
   旧正则写死 ``max of``，这两台的在线人数一直静默显示 0 / 0）；
6. 界面不显示原文 / 界面不渲染卡片 → ``tests/ui_server_card_check.py`` 红。

运行：<AstrBot python> tests\\test_v0250_server_card.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core import server_card as SC  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


# ===================== 一、`/list` 的三种形态 =====================

#: 真机 / 原版语言文件里的原样（不是编的）
LIST_CASES = [
    # (回包, online, max, players, 出处)
    ("There are 2 of a max of 20 players online: Steve, Alex", 2, 20, ["Steve", "Alex"], "1.16+ 原版"),
    ("There are 2 of a maximum of 20 players online: Steve", 2, 20, ["Steve"], "1.16 早期措辞"),
    ("There are 0 of a max 20 players online:", 0, 20, [],
     "1.13 – 1.15 原版（真机 1.13.2/1.14.4：max 20，没有第二个 of）"),
    ("There are 3 of a max 30 players online: a, b, c", 3, 30, ["a", "b", "c"],
     "1.13 – 1.15 原版（有人在线）"),
    ("There are 0/20 players online:", 0, 20, [], "1.7.10 ~ 1.15 / Bukkit"),
    ("There are 3/30 players online: a, b, c", 3, 30, ["a", "b", "c"], "1.12.2"),
    ("有 2 个玩家在线，最多 20", 2, 20, [], "中文服务端"),
    ("§aThere are 1 of a max of 8 players online: Notch", 1, 8, ["Notch"], "带颜色码"),
]

LIST_UNPARSABLE = [
    "Players online (2): Steve",        # 模组改了措辞
    "Hello",                            # 完全不相干
    "There are many players",           # 数字都没了
    "",                                 # 空回包
    "   ",                              # 全空白
]


def part_one() -> None:
    print("\n---- 一、`/list`：三种世代形态都要认出来 ----")
    for raw, online, maxp, players, where in LIST_CASES:
        got = SC.parse_list_reply(raw)
        check(f"{where}：{raw[:46]!r} → {online}/{maxp}",
              got == {"online": online, "max": maxp, "players": players}, repr(got))
    for raw in LIST_UNPARSABLE:
        check(f"认不出（交给 unrecognized + 原文）：{raw[:40]!r}", SC.parse_list_reply(raw) is None,
              repr(SC.parse_list_reply(raw)))
    # 中文形态里「最多 20」绝不能被当成一个叫 20 的玩家
    zh = SC.parse_list_reply("有 2 个玩家在线，最多 20") or {}
    check("中文形态不把「最多 20」当玩家名", "20" not in zh.get("players", []), repr(zh))


# ===================== 二、`/tps` =====================

TPS_CASES = [
    # (回包, 期望 dict, 出处)
    ("§6TPS from last 1m, 5m, 15m: §a20.0§r, §a20.0§r, §a20.0", {"1m": 20.0, "5m": 20.0, "15m": 20.0},
     "paper-1.21.11 真机原样（带颜色码）"),
    ("TPS from last 1m, 5m, 15m: 19.98, 20.0, 20.0", {"1m": 19.98, "5m": 20.0, "15m": 20.0}, "Paper 纯文本"),
    ("Server TPS: 18.5", {"1m": 18.5, "5m": None, "15m": None}, "模组探针（单值）"),
    ("TPS: 20.0 20.0 20.0", {"1m": 20.0, "5m": 20.0, "15m": 20.0}, "模组探针（空格分隔）"),
]

TPS_UNPARSABLE = [
    'Unknown command. Type "/help" for help.',   # 原版 / Fabric：没有 /tps
    "Unknown commandtps<--[HERE]",               # 1.14.4 真机：brigadier 位置标记
    "TPS 报告还没生成",                            # 回了话但没数字
    "",
]


def part_two() -> None:
    print("\n---- 二、`/tps`：有探针才有，没有就不许编 ----")
    for raw, want, where in TPS_CASES:
        got = SC.parse_tps_reply(raw)
        check(f"{where}：{raw[:44]!r}", got == want, repr(got))
    for raw in TPS_UNPARSABLE:
        check(f"解析不出（交状态机判）：{raw[:40]!r}", SC.parse_tps_reply(raw) is None,
              repr(SC.parse_tps_reply(raw)))
    # 只扫含 tps 的那一行：别的行里的数字不许被当成 TPS
    check("不扫无关行里的数字",
          SC.parse_tps_reply("Saved the game\nTPS: 17.5") == {"1m": 17.5, "5m": None, "15m": None},
          repr(SC.parse_tps_reply("Saved the game\nTPS: 17.5")))


# ===================== 三、状态判定（五态分界） =====================

JUDGE_CASES = [
    # (命令, 回包, parsed, error, 期望状态, 说明)
    ("list", "There are 2/20 players online: Steve", True, "", SC.STATE_OK, "解析成功即 ok"),
    ("list", "", False, "TimeoutError: RCON 超时", SC.STATE_UNREACHABLE, "异常 = 一个字节都没拿到"),
    ("tps", 'Unknown command. Type "/help" for help.', False, "", SC.STATE_UNSUPPORTED,
     "命令不存在 = 该世代不支持"),
    ("time query daytime", "/time <set|add> <value>", False, "", SC.STATE_UNSUPPORTED,
     "1.7.10 用法文本 = 子命令不存在"),
    ("tps", "Expected whitespace to end one argument, but found trailing data", False, "",
     SC.STATE_UNSUPPORTED, "语法被拒 = 该世代写法不同"),
    ("tps", "TPS 报告还没生成", False, "", SC.STATE_UNRECOGNIZED, "回了话但认不出"),
    ("list", "Players online (2): Steve", False, "", SC.STATE_UNRECOGNIZED, "措辞没见过"),
    ("list", "", False, "", SC.STATE_UNRECOGNIZED, "空回包：不是「不支持」"),
    ("tps", "You do not have permission to use this command", False, "", SC.STATE_UNRECOGNIZED,
     "权限不足 ≠ 该世代不支持"),
]


def part_three() -> None:
    print("\n---- 三、状态判定：五态分界（合并任意两个 = 红）----")
    for cmd, raw, parsed, error, want, why in JUDGE_CASES:
        state, note = SC.judge_reply(cmd, raw, parsed=parsed, error=error)
        check(f"{why} → {want}", state == want, f"{state} / {note}")
        if want != SC.STATE_OK:
            check(f"  ↳ 非正常态必须说清为什么：{why}", bool(note.strip()), repr(note))
    # 状态集合必须与标签表一一对应（新增状态忘登记 → 界面显示英文 key）
    check("STATE_LABEL 覆盖所有状态常量",
          all(st in SC.STATE_LABEL for st in (SC.STATE_OK, SC.STATE_EMPTY, SC.STATE_UNSUPPORTED,
                                              SC.STATE_UNRECOGNIZED, SC.STATE_UNREACHABLE,
                                              SC.STATE_NO_SOURCE)))
    check("STATE_LEGEND 覆盖所有状态常量",
          {st for st, _ in SC.STATE_LEGEND} == set(SC.STATE_LABEL))


# ===================== 四、卡片级结论 =====================


def _fields(**states) -> list[dict]:
    out = []
    for key, _label, _src in SC.FIELD_SPECS:
        out.append({"key": key, "state": states.get(key, SC.STATE_OK)})
    return out


def part_four() -> None:
    print("\n---- 四、卡片级状态：头条归在线人数，别被别的字段带偏 ----")
    check("四项全正常 → ok", SC.overall_state(_fields()) == SC.STATE_OK)
    check("★空服 + TPS 不支持 → 头条仍是「空服」（不是错误）",
          SC.overall_state(_fields(online=SC.STATE_EMPTY, tps=SC.STATE_UNSUPPORTED)) == SC.STATE_EMPTY)
    check("★RCON 断 → 头条「连不上」",
          SC.overall_state(_fields(online=SC.STATE_UNREACHABLE)) == SC.STATE_UNREACHABLE)
    check("★回执认不出 → 头条「认不出」（这张卡的数据不可信）",
          SC.overall_state(_fields(online=SC.STATE_OK, tps=SC.STATE_UNRECOGNIZED)) == SC.STATE_UNRECOGNIZED)
    check("版本无来源不污染头条",
          SC.overall_state(_fields(version=SC.STATE_NO_SOURCE)) == SC.STATE_OK)
    check("摘要能说清「几项正常 · 几项有问题」",
          SC.summary_line(_fields(tps=SC.STATE_UNSUPPORTED)) == "3 项正常 · 该世代不支持 1 项",
          SC.summary_line(_fields(tps=SC.STATE_UNSUPPORTED)))
    check("全正常时摘要不啰嗦",
          SC.summary_line(_fields()) == "4 项全部正常", SC.summary_line(_fields()))


# ===================== 五、端到端：假 RCON 跑六个场景 =====================


class FakeRcon:
    def __init__(self, replies: dict, errors: dict | None = None) -> None:
        self.replies = replies
        self.errors = errors or {}
        self.calls: list[str] = []

    async def command(self, cmd: str) -> str:
        self.calls.append(cmd)
        if cmd in self.errors:
            raise self.errors[cmd]
        return self.replies.get(cmd, "")


CAPS_KNOWN = {
    "known": True, "version": "1.20.6", "raw": "MC 1.20.6 · Forge 50.1.0",
    "source": "detected", "source_label": "服务端文件探测",
    "item_syntax": "components", "item_syntax_source": "detected",
    "supported": True, "support_note": "支持自动命令生成",
}
CAPS_UNKNOWN = {
    "known": False, "version": "", "raw": "", "source": "unknown",
    "source_label": "未知（探测失败且未声明）", "item_syntax": "unknown",
    "support_note": "版本未知：带 NBT / 附魔 / 物品组件的请求不会被自动生成",
}


class FakePlugin:
    def __init__(self, rcon=None, caps=None, connect_error: Exception | None = None) -> None:
        self.rcon = rcon
        self.caps = caps or CAPS_KNOWN
        self.connect_error = connect_error

    async def _get_rcon(self):
        if self.connect_error is not None:
            raise self.connect_error
        return self.rcon

    def _version_capabilities(self):
        return dict(self.caps)


def _run(plugin) -> dict:
    return asyncio.run(SC.sample(plugin))


def _by_key(card: dict) -> dict:
    return {f["key"]: f for f in card["fields"]}


def part_five() -> None:
    print("\n---- 五、端到端：假 RCON 跑六个场景 ----")

    # A) 现代服 + Paper 探针，2 人在线
    a = _run(FakePlugin(FakeRcon({
        "list": "There are 2 of a max of 20 players online: Steve, Alex",
        "time query daytime": "The time is 593",
        "tps": "§6TPS from last 1m, 5m, 15m: §a20.0§r, §a19.9§r, §a19.8",
    })))
    fa = _by_key(a)
    check("A 全正常 → overall ok", a["overall"] == SC.STATE_OK, a["overall"])
    check("A 在线人数 2 / 20", fa["online"]["value"] == "2 / 20", fa["online"]["value"])
    check("A 游戏内时间给的是墙上时间 06:35",
          fa["daytime"]["value"] == "06:35", fa["daytime"]["value"])
    check("A TPS 三个值都在", fa["tps"]["value"] == "20.0 / 19.9 / 19.8", fa["tps"]["value"])
    check("A 版本字段带世代人话",
          "物品组件世代" in fa["version"]["note"], fa["version"]["note"])
    check("A 每项都标了采样来源",
          all(f["source"] for f in a["fields"]), [f["source"] for f in a["fields"]])
    check("A 字段顺序 = 在线 / 时间 / TPS / 版本",
          [f["key"] for f in a["fields"]] == ["online", "daytime", "tps", "version"],
          [f["key"] for f in a["fields"]])

    # B) 1.7.10 空服：list 旧形态 0 人 + time query 回用法文本 + 没有 /tps
    b = _run(FakePlugin(FakeRcon({
        "list": "There are 0/20 players online:",
        "time query daytime": "/time <set|add> <value>",
        "tps": "Unknown command. Try /help for a list of commands",
    }), caps={**CAPS_KNOWN, "version": "1.7.10", "item_syntax": "legacy_preflatten",
              "raw": "MC 1.7.10 · Forge 10.13.4.1614"}))
    fb = _by_key(b)
    check("★B 空服 → online empty（不是错误）", fb["online"]["state"] == SC.STATE_EMPTY,
          fb["online"]["state"])
    check("★B 空服说明写清「正常状态」", "正常状态" in fb["online"]["note"], fb["online"]["note"])
    check("★B 1.7.10 的 time query → 该世代不支持",
          fb["daytime"]["state"] == SC.STATE_UNSUPPORTED, fb["daytime"]["state"])
    check("★B 没装探针 → TPS 该世代不支持", fb["tps"]["state"] == SC.STATE_UNSUPPORTED,
          fb["tps"]["state"])
    check("★B 头条仍是「空服」", b["overall"] == SC.STATE_EMPTY, b["overall"])
    check("B 不支持时也带原文（用户要能自查）",
          fb["daytime"].get("raw") == "/time <set|add> <value>", fb["daytime"].get("raw"))

    # C) 回执认不出：连上了、回话了，但话没见过
    c = _run(FakePlugin(FakeRcon({
        "list": "Players online (2): Steve, Alex",
        "time query daytime": "现在是白天",
        "tps": "TPS 报告还没生成",
    })))
    fc = _by_key(c)
    for key, raw in (("online", "Players online (2): Steve, Alex"),
                     ("daytime", "现在是白天"), ("tps", "TPS 报告还没生成")):
        check(f"★C {key} → 回执认不出", fc[key]["state"] == SC.STATE_UNRECOGNIZED,
              fc[key]["state"])
        check(f"★C {key} 认不出时**必须带原文**", fc[key].get("raw") == raw, fc[key].get("raw"))
        check(f"★C {key} 不许说「不支持」", "不支持" not in fc[key]["note"], fc[key]["note"])
    check("★C 头条升级为「回执认不出」", c["overall"] == SC.STATE_UNRECOGNIZED, c["overall"])

    # D) 连不上：RCON 建连就抛
    d = _run(FakePlugin(None, connect_error=ConnectionRefusedError("拒绝连接")))
    fd = _by_key(d)
    check("★D 连不上 → 三个 RCON 字段全 unreachable",
          all(fd[k]["state"] == SC.STATE_UNREACHABLE for k in ("online", "daytime", "tps")),
          {k: fd[k]["state"] for k in ("online", "daytime", "tps")})
    check("★D 连不上时版本字段照旧可用（它与 RCON 无关）",
          fd["version"]["state"] == SC.STATE_OK, fd["version"]["state"])
    check("D 卡片如实标 rcon_connected=False", d["rcon_connected"] is False)
    check("D 头条 = 连不上", d["overall"] == SC.STATE_UNREACHABLE, d["overall"])
    check("D 连不上的原因写进说明", "拒绝连接" in fd["online"]["note"], fd["online"]["note"])

    # E) 版本既没探测到也没声明 → no_source（不是「认不出」）
    e = _run(FakePlugin(FakeRcon({"list": "There are 1/20 players online: Steve",
                                  "time query daytime": "Time is 1000",
                                  "tps": "TPS: 20.0"}), caps=CAPS_UNKNOWN))
    fe = _by_key(e)
    check("★E 版本无来源 → no_source", fe["version"]["state"] == SC.STATE_NO_SOURCE,
          fe["version"]["state"])
    check("E 版本字段的世代写「世代未知」", fe["version"]["generation"] == "世代未知",
          fe["version"].get("generation"))
    check("E 版本无来源不污染头条", e["overall"] == SC.STATE_OK, e["overall"])

    # F) 中途一条探针抛异常：只有它 unreachable，其余照旧
    f = _run(FakePlugin(FakeRcon(
        {"list": "There are 1 of a max of 20 players online: Steve", "tps": "TPS: 20.0"},
        errors={"time query daytime": TimeoutError("RCON 超时")},
    )))
    ff = _by_key(f)
    check("★F 单条探针超时 → 只有 daytime unreachable",
          ff["daytime"]["state"] == SC.STATE_UNREACHABLE and ff["online"]["state"] == SC.STATE_OK,
          {k: ff[k]["state"] for k in ("online", "daytime")})
    check("F 单条超时不算「认不出」", ff["daytime"]["state"] != SC.STATE_UNRECOGNIZED)

    # 全场景不变量：凡是 unrecognized / unsupported，都必须带原文
    for tag, card in (("A", a), ("B", b), ("C", c), ("D", d), ("E", e), ("F", f)):
        bad = [f["key"] for f in card["fields"]
               if f["state"] in (SC.STATE_UNRECOGNIZED, SC.STATE_UNSUPPORTED) and not f.get("raw")]
        check(f"★不变量 {tag}：认不出 / 不支持都必须带原文", not bad, bad)
        check(f"不变量 {tag}：每项都有 state_label",
              all(f.get("state_label") == SC.STATE_LABEL[f["state"]] for f in card["fields"]))


# ===================== 六、接线：接口与界面都必须用这一份 =====================


def part_six() -> None:
    print("\n---- 六、接线：接口 / 界面都走这一份判据 ----")
    api_src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    card_src = (PLUGIN_DIR / "core" / "server_card.py").read_text(encoding="utf-8")
    check("接口注册了独立的 /server/card", '/server/card' in api_src)
    check("★/server/status 也带 card（一次采样喂两个消费者）", '"card": card' in api_src
          or 'info["card"] = card' in api_src)
    # 只在 get_server_status 这一段里查：`test_rcon` 那条接口本来就该自己发 list
    seg = api_src[api_src.find("async def get_server_status"):]
    seg = seg[:seg.find("async def ", 10)] if "async def " in seg[10:] else seg
    check("★get_server_status 不再自己发 list 探针（避免两次采样看到不同时刻）",
          'rcon.command("list")' not in seg and "sample_with(" in seg, seg[:200])
    check("★界面渲染卡片", "renderServerCard" in html)
    # 判据：前端必须读后端给的 state_label，且**不许**自己写一份状态中文措辞
    # （两份措辞一旦分叉，同一个状态在接口和页面上就叫两个名字，验收无从谈起）
    check("★界面只渲染后端给的 state_label，不另写一份措辞",
          "f.state_label" in html and "该世代不支持" not in html and "回执认不出" not in html,
          "state_label=%s / 硬编码=%s" % ("f.state_label" in html,
                                        [s for s in ("该世代不支持", "回执认不出") if s in html]))
    check("★界面必须把原文显示出来（认不出时）", "sc-raw" in html)
    check("时间换算只有一处实现（daytime_clock）",
          "daytime_clock(" in card_src and "// 1000) + 6" not in card_src)


def main() -> int:
    print("=" * 78)
    print("v0.25.0 实时服务器卡 · 四字段 / 五态 / 采样来源")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    part_five()
    part_six()
    print("\n================ 汇总 ================")
    print(f"通过 {PASSED[0]} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✔（四态分开说、认不出必带原文、空服不当错误）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
