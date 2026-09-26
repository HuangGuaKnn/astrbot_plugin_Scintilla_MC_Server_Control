# -*- coding: utf-8 -*-
"""v0.23.3 · 权限前置提醒的配置契约（触发时机 + 话题词表）。

运行：
  <python> tests/test_v0233_hint_config.py

背景
====
v0.23.3 把「权限前置提醒」从「每轮硬塞」改成「按需触发」，并把「哪些说法算涉及 MC」
交给用户自己定。这两件事都靠**配置项**落地，因此本文件不看行为、只钉契约：

1. ``_conf_schema.json`` 里 ``permission_hint_keywords`` 的默认值
   **必须等于**代码里的内置词表 —— 两份清单一旦漂移，这里立刻红
   （v0.23.0 就吃过「只加 schema、漏加后端白名单」的亏，界面误报「插件未重载」）；
2. 词表的读取语义：**配置优先、留空回退内置、纯文本也能解析、大小写不敏感**；
3. 词表与触发判定联动：命中 → ``topic``；``always`` 档不看词表。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control import main as m  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import tool_guard as tg  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi  # noqa: E402

_fail: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


class Ev:
    """最小事件替身（只需要插件用到的那几个接口）。"""

    def __init__(self, text: str = ""):
        self.message_str = text
        self.unified_msg_origin = "t:FriendMessage:1"
        self._s = "10001"

    def get_sender_id(self) -> str:
        return self._s


def make_plug(permission: dict):
    """真插件类 + 只填必要属性（沿用 test_remote_rcon_mode 的搭法）。"""
    plug = object.__new__(m.McControlPlugin)
    plug.config = {"permission": dict(permission)}
    plug._cfg_index = m.load_cfg_group_index(PLUGIN_DIR / m.CFG_SCHEMA_FNAME)
    plug.logger = logging.getLogger("hint-cfg")
    plug._mc_tool_recent = {}
    plug._admins = set()
    return plug


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    schema = json.loads((PLUGIN_DIR / m.CFG_SCHEMA_FNAME).read_text(encoding="utf-8-sig"))
    items = schema["permission"]["items"]

    # ============================================================
    print("=========== A. schema 默认值 ↔ 内置词表 一致性 ===========")
    kw = items.get("permission_hint_keywords")
    check("schema 里有 permission_hint_keywords", bool(kw))
    check("类型为 list（前端才会渲染成可增删的列表）", (kw or {}).get("type") == "list")
    default_kw = tuple((kw or {}).get("default") or ())
    check("默认值非空（否则「留空回退内置」就没了意义）", bool(default_kw))
    check("默认值 == 内置词表（防两份清单漂移）",
          default_kw == tuple(tg.HINT_TOPIC_KEYWORDS),
          f"→ schema {len(default_kw)} 个 / 内置 {len(tg.HINT_TOPIC_KEYWORDS)} 个")
    check("触发时机默认 on_demand（不是旧行为 always）",
          (items.get("permission_hint_mode") or {}).get("default") == "on_demand")
    check("触发时机是枚举三档",
          (items.get("permission_hint_mode") or {}).get("options") == list(tg.HINT_MODES))
    check("后端把词表收进了 LIST_SETTING_KEYS（前端才提交得进去）",
          "permission_hint_keywords" in getattr(McControlWebApi, "LIST_SETTING_KEYS", ()),
          f"→ {getattr(McControlWebApi, 'LIST_SETTING_KEYS', None)}")
    check("后端把触发时机收进了 ENUM_SETTING_KEYS",
          "permission_hint_mode" in getattr(McControlWebApi, "ENUM_SETTING_KEYS", {}),
          f"→ {list(getattr(McControlWebApi, 'ENUM_SETTING_KEYS', {}))}")

    # ============================================================
    print("\n=========== B. 词表读取语义 ===========")
    base = {"permission_hint_mode": "on_demand"}
    plug_empty = make_plug({**base, "permission_hint_keywords": []})
    check("留空 → 回退内置词表", plug_empty._hint_keywords() == tg.HINT_TOPIC_KEYWORDS)
    check("留空时内置词照常命中", plug_empty._hint_topic_hit(Ev("帮我看看服务器")) is True)

    plug_none = make_plug(base)  # 键完全不存在（老配置）
    check("键缺失 → 同样回退内置词表", plug_none._hint_keywords() == tg.HINT_TOPIC_KEYWORDS)

    plug_c = make_plug({**base, "permission_hint_keywords": ["开黑", "刷怪塔"]})
    check("配置优先：自定义词表原样生效", plug_c._hint_keywords() == ("开黑", "刷怪塔"))
    check("自定义词命中 → topic", plug_c._hint_topic_hit(Ev("今晚开黑吗")) is True)
    check("生效方式是「替换」不是「追加」（默认值已是全量词表）",
          plug_c._hint_topic_hit(Ev("给我发一把钻石剑")) is False)

    plug_s = make_plug({**base, "permission_hint_keywords": "开黑, 刷怪塔\n腐竹"})
    check("纯文本（逗号 / 中文逗号 / 换行）也能解析",
          plug_s._hint_keywords() == ("开黑", "刷怪塔", "腐竹"))

    plug_u = make_plug({**base, "permission_hint_keywords": ["AK-47", "  ", ""]})
    check("大小写不敏感（配置里大写、消息里小写也认）",
          plug_u._hint_topic_hit(Ev("给我满配一把 ak-47")) is True)
    check("忽略空白项", plug_u._hint_keywords() == ("ak-47",))

    plug_bad = make_plug({**base, "permission_hint_keywords": 12345})
    check("非法类型（非 list / str）→ 回退内置，不炸",
          plug_bad._hint_keywords() == tg.HINT_TOPIC_KEYWORDS)

    # ============================================================
    print("\n=========== C. 与触发判定联动 ===========")
    plug_ok = make_plug({**base, "permission_hint_keywords": ["开黑"]})
    check("命中自定义词 → trigger = topic", plug_ok._hint_trigger(Ev("开黑")) == "topic")
    check("闲聊 → trigger 为空（不注入）", plug_ok._hint_trigger(Ev("今天天气真好")) == "")
    check("本会话调过 MC 工具 → trigger = recent_tool",
          (lambda p, e: (p._mark_mc_tool_used(e), p._hint_trigger(e))[1])(
              plug_ok, Ev("嗯嗯")) == "recent_tool")
    plug_always = make_plug({"permission_hint_mode": "always",
                             "permission_hint_keywords": ["开黑"]})
    check("always 档不看词表（闲聊也注入）",
          plug_always._hint_trigger(Ev("今天天气真好")) == "always")
    plug_off = make_plug({"permission_hint_mode": "off",
                          "permission_hint_keywords": ["开黑"]})
    check("off 档一律不注入", plug_off._hint_trigger(Ev("开黑")) == "")

    # ============================================================
    print(f"\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n✗ 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
