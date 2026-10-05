# -*- coding: utf-8 -*-
"""v0.23.7 · 广播署名与任务反馈署名分家（主人点名）。

背景（2026-10-05）
==================
WebUI 广播控制台的「聊天栏」格此前与任务反馈共用 `feedback_name` ——
主人配的是「皮莉卡」，于是拿广播跟服务器里的玩家闲聊时，公屏上是
「[皮莉卡] 你好」，看着像 Bot 本人在发言。现在：

  · 「正常输出」（对全服公开说话：工具广播 mc_broadcast 的 chat 模式、
    WebUI 广播控制台的「聊天栏」格）→ 固定署名 [Server]；
  · 「任务反馈」（含 WebUI「模拟任务输出」预览）→ 仍用 feedback_name；
  · 「全屏标题」/ 动作栏 → 本来就不带前缀。

本用例钉死四件事
================
1. 常量：McControlPlugin.BROADCAST_NAME == "Server"（换名字只改这一处）；
2. 行为：真插件实例跑 mc_broadcast —— chat 带 [Server]；title / actionbar 一个前缀都不带；
3. 行为：_send_feedback 仍走 feedback_name，且两条线真的解耦（改了署名不影响广播）；
4. 源码契约：core/web_api.py 的 broadcast 里「聊天栏」用 plugin.BROADCAST_NAME、
   「模拟任务输出」用 feedback_name，且 main.py 的 mc_broadcast 已不再读 feedback_name 配置。

运行：python tests/test_v0237_broadcast_alias.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

import astrbot_plugin_Scintilla_MC_Server_Control.main as main  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.version_caps import (  # noqa: E402
    VersionInfo,
)

FAIL: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {extra}" if extra and not ok else ""))
    if not ok:
        FAIL.append(desc)


class FakeRcon:
    """只把收到的命令记下来（不真的连服务器）。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        return "ok"


def make_plugin(fb_name: str = "皮莉卡"):
    """真身插件实例（绕过 __init__，只喂被调用到的那些依赖）。"""
    m = main.McControlPlugin.__new__(main.McControlPlugin)
    m.config = {"appearance": {
        "gradient_enabled": False,
        "gradient_format": "json",
        "plain_color_format": "hex",
        "color_say": "white",
        "color_title": "gold",
        "color_feedback": "gold",
        "gradient_colors_say": "#FFFFFF,#55FFFF",
        "gradient_colors_title": "#FFAA00,#FF00FF",
        "gradient_colors_feedback": "#FFAA00,#55FF55",
        "feedback_name": fb_name,
        "feedback_tellraw": True,
    }}
    m._cfg_index = {k: "appearance" for k in m.config["appearance"]}
    m.logger = logging.getLogger("t0237alias")
    m._resolve_version_info = lambda: VersionInfo(mc=(1, 21, 11), raw="", source="detected")
    # —— 工具链上的旁支全部短路，只让 _build_text_cmds / _send_feedback 走真身 ——
    m._tool_enabled = lambda name: True
    m._too_long = lambda text, limit, label="": None
    m._is_admin = lambda event: True
    m._guard_command_for_version = lambda cmd, source="": None
    m._render_result = lambda r, ok_text, bad_text: ok_text
    m._fake_rcon = FakeRcon()

    async def _no_deny(event, cmd, tool=""):
        return None

    async def _exec(rcon, cmds):
        rcon.sent.extend(cmds)
        return object()

    async def _rcon():
        return m._fake_rcon

    m._safe_command = _no_deny
    m._exec_text_cmds = _exec
    m._get_rcon = _rcon
    return m


def text_of(cmd: str, prefix: str) -> str:
    """从 tellraw / title 命令里取回玩家实际看到的那段文本。"""
    assert cmd.startswith(prefix), cmd[:80]
    return json.loads(cmd[len(prefix):]).get("text") or ""


def broadcast(m, **kw):
    return asyncio.run(main.McControlPlugin.mc_broadcast(m, None, **kw))


# ===================== 一、常量 =====================

print("=========== 一、署名常量 ===========")
check('McControlPlugin.BROADCAST_NAME == "Server"（广播署名收敛在一处）',
      main.McControlPlugin.BROADCAST_NAME == "Server",
      repr(main.McControlPlugin.BROADCAST_NAME))

# ===================== 二、行为：mc_broadcast =====================

print("")
print("=========== 二、行为：工具广播 mc_broadcast ===========")
m = make_plugin(fb_name="皮莉卡")
broadcast(m, message="大家好", mode="chat")
check("chat 模式发出一条 tellraw", len(m._fake_rcon.sent) == 1, str(m._fake_rcon.sent))
check("chat 模式署名固定 [Server]（不再写 feedback_name）",
      text_of(m._fake_rcon.sent[0], "tellraw @a ") == "[Server] 大家好",
      str(m._fake_rcon.sent[:1]))
check("chat 命令里不出现反馈署名", "皮莉卡" not in m._fake_rcon.sent[0])

m3 = make_plugin(fb_name="皮莉卡")
broadcast(m3, message="嗨")
check("不传 mode（默认 chat）也带 [Server]",
      text_of(m3._fake_rcon.sent[0], "tellraw @a ") == "[Server] 嗨",
      str(m3._fake_rcon.sent[:1]))

m2 = make_plugin(fb_name="皮莉卡")
broadcast(m2, message="集合啦", mode="title")
check("title 模式不带任何前缀",
      text_of(m2._fake_rcon.sent[0], "title @a title ") == "集合啦",
      str(m2._fake_rcon.sent[:1]))

m4 = make_plugin(fb_name="皮莉卡")
broadcast(m4, message="集合啦", mode="actionbar")
check("actionbar 模式不带任何前缀",
      text_of(m4._fake_rcon.sent[0], "title @a actionbar ") == "集合啦",
      str(m4._fake_rcon.sent[:1]))

# ===================== 三、行为：任务反馈仍用 feedback_name =====================

print("")
print("=========== 三、行为：任务反馈 _send_feedback ===========")
m5 = make_plugin(fb_name="皮莉卡")
asyncio.run(m5._send_feedback(m5._fake_rcon, "已给 Steve 发了一把锋利5的下界合金剑"))
fb_text = text_of(m5._fake_rcon.sent[0], "tellraw @a ")
check("任务反馈仍用 feedback_name", fb_text.startswith("[皮莉卡] "), fb_text)
check("任务反馈没有被广播署名污染", not fb_text.startswith("[Server] "), fb_text)

m6 = make_plugin(fb_name="小助手")
asyncio.run(m6._send_feedback(m6._fake_rcon, "完成"))
check("反馈署名改成别的要跟着变（证明是活的配置，不是写死）",
      text_of(m6._fake_rcon.sent[0], "tellraw @a ") == "[小助手] 完成",
      str(m6._fake_rcon.sent[:1]))

m7 = make_plugin(fb_name="小助手")
broadcast(m7, message="喂", mode="chat")
check("反馈署名改了也不影响广播（两条线真的解耦）",
      text_of(m7._fake_rcon.sent[0], "tellraw @a ") == "[Server] 喂",
      str(m7._fake_rcon.sent[:1]))

# ===================== 四、源码契约 =====================

print("")
print("=========== 四、源码契约（两个落点必须同源） ===========")
src_main = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
src_web = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")

check("main.py：chat 分支用 self.BROADCAST_NAME 拼前缀",
      'f"[{self.BROADCAST_NAME}] {message}", "tellraw @a ",' in src_main)
check("web_api.py：聊天栏分支用 say_name（取自 plugin.BROADCAST_NAME）",
      'f"[{say_name}] {message}", "tellraw @a ",' in src_web)
check("web_api.py：模拟任务输出分支仍用 feedback_name",
      'f"[{fb_name}] {message}", "tellraw @a ",' in src_web)
check("web_api.py：署名取插件常量，且带 Server 兜底",
      'getattr(plugin, "BROADCAST_NAME", "") or "Server"' in src_web)

seg = src_main[src_main.index("async def mc_broadcast"):]
seg = seg[:seg.index("async def mc_give_item")]
check('main.py：mc_broadcast 不再读 feedback_name 配置',
      'self._cfg("feedback_name"' not in seg)

# ===================== 汇总 =====================

print("")
print("================ 汇总 ================")
print(f"失败 {len(FAIL)} 项" + ("" if not FAIL else "：" + " / ".join(FAIL)))
sys.exit(1 if FAIL else 0)
