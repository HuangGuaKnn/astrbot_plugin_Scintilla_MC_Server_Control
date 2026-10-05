# -*- coding: utf-8 -*-
"""v0.23.7 · N2：空服广播回执 —— 「没有接收者」不是失败。

背景（2026-10-05 全版本矩阵巡礼实测）
======================================
同一条 ``tellraw @a "hi"`` 打到**空服**，各世代表述不同：
- ≤1.13.2：**静默成功**（RCON 不回文本）→ 现有逻辑判 success（静默集合）；
- ≥1.16.5：回 ``No player was found`` → 落进 `_PHRASE_FAILURE_MARKERS` →
  回执被判 **failed**：「广播到底成没成」随服务端版本号时好时坏。

修复：把「消息投递类命令 + **通配选择器**目标 + 无匹配对象回执」单独识别为
``success`` + ``no_recipient=True``（回执文案改为「消息已发出，暂无接收者」）。

本测试钉死：
1. 正例：tellraw / title / 带参选择器 / 命名空间前缀 → success + no_recipient；
2. 世代等价：≤1.13.2 的静默空响应**不**被硬塞 no_recipient（不臆造信息）；
3. 反面（防止温和化变成「什么都算成功」的口子）：
   - 点名目标（give Steve / tellraw Steve）→ 仍 failed；
   - 非消息类命令 → 仍 failed；
   - 语法错误 / 命令不存在 → 硬错误优先，不被吞；
   - ``say`` 的第一个参数不是选择器 → 仍 failed（通配门是硬门）；
   - ``execute as @a run tellraw`` → 不在集合内（边界，如实记录）；
4. 链路：`_exec_text_cmds` 遇 no_recipient **继续**发后续段（渐变两段式）；
   真失败仍然立即中断；
5. 回执文案：带「没有玩家在线」且**不含**「失败」；普通成功文案不夹带提示；
6. 源码登记（常量 / 字段 / N2 来历注释在案）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

_fail: list[str] = []
_pass = 0
_skip: list[str] = []

EMPTY_SERVER = "No player was found"


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


def skip(name: str, why: str) -> None:
    _skip.append(name)
    print(f"[SKIP] {name}  <- {why}")


def main() -> None:
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control.core import command_result as cr
    except Exception as e:
        skip("全部（识别器行为）", f"缺 AstrBot 运行时：{type(e).__name__}: {e}")
        _source_scan()
        _summary()
        return

    def cls(cmd: str, out: str, **kw):
        return cr.classify_command_output(
            cmd, out, boundary_confirmed=True, response_received=True, **kw
        )

    print("---- 一、空服广播：正例（1.16.5+ 回执） ----")
    for cmd in (
        'tellraw @a {"text":"[Server] hi"}',
        'tellraw @a hello',
        'minecraft:tellraw @a hello',
        'title @a title {"text":"hi"}',
        'title @a actionbar {"text":"hi"}',
        'tellraw @a[team=red] hello',
        'tellraw @e hello',
    ):
        r = cls(cmd, EMPTY_SERVER)
        check(f"★{cmd[:46]:<46} → success/no_recipient",
              r.status == "success" and r.confidence == "no_recipient" and r.no_recipient
              and r.ok is True and r.accepted is True,
              f"{r.status}/{r.confidence}/no_recipient={r.no_recipient}")

    print("---- 二、世代等价：静默世代不臆造 ----")
    r = cls('tellraw @a hi', "")
    check("★≤1.13.2 空响应（静默成功）→ success 但 no_recipient=False",
          r.status == "success" and r.no_recipient is False and r.confidence == "empty",
          f"{r.status}/{r.confidence}/no_recipient={r.no_recipient}")
    r = cls('title @a title {"text":"hi"}', "")
    check("title 静默空响应同理（不冒充「无接收者」）",
          r.status == "success" and r.no_recipient is False, f"{r.status}")

    print("---- 三、反面：温和化不许溢出 ----")
    cases = [
        ("give Steve diamond 1", EMPTY_SERVER, "点名目标 give → 仍 failed（真没送到）"),
        ("tellraw Steve hi", EMPTY_SERVER, "点名目标 tellraw → 仍 failed（打错名要报错）"),
        ('tellraw @s {"text":"hi"}', EMPTY_SERVER, "@s（自己）不是通配目标 → 仍 failed"),
        ("say hello", EMPTY_SERVER, "say 的第一参数是正文、不是选择器 → 仍 failed"),
        ("tellraw", EMPTY_SERVER, "缺目标参数 → 仍 failed"),
        ("time set day", EMPTY_SERVER, "非消息投递类命令 → 仍 failed"),
        ("execute as @a run tellraw @a hi", EMPTY_SERVER, "execute 包装 → 仍 failed（边界，如实记录）"),
    ]
    for cmd, out, desc in cases:
        r = cls(cmd, out)
        check(f"★{desc}", r.status == "failed" and r.no_recipient is False,
              f"{r.status}/{r.no_recipient}/{r.reason}")

    print("---- 四、硬错误优先：不被温和化吞掉 ----")
    r = cls("tellraw @a hi", "Unknown or incomplete command, see below for error")
    check("★语法错误优先 → syntax_error（可安全重写重试）",
          r.status == "syntax_error", f"{r.status}/{r.reason}")
    r = cls("tellraw @a hi", 'Unknown command. Type "/help" for help.')
    check("★命令不存在优先 → failed（模组/版本不支持）",
          r.status == "failed" and "不存在" in r.reason, f"{r.status}/{r.reason}")
    r = cls("tellraw @a hi", "No player was found")
    check("对照：同一命令同一回执走温和化（前一例不是被新逻辑改坏的）",
          r.status == "success" and r.no_recipient, r.status)

    print("---- 五、链路：广播两段式不中断 ----")
    _link_checks(cr)

    _source_scan()
    _summary()


def _link_checks(cr) -> None:
    try:
        import astrbot_plugin_Scintilla_MC_Server_Control.main as m
    except Exception as e:
        skip("链路（_exec_text_cmds / _render_result）",
             f"缺 AstrBot 运行时：{type(e).__name__}: {e}")
        return

    class Stub:
        """只接住 `_exec_checked`：把假回执喂回真识别器。"""

        def __init__(self, replies):
            self.replies = replies
            self.sent: list[str] = []

        async def _exec_checked(self, rcon, command):
            self.sent.append(command)
            return cr.classify_command_output(
                command, self.replies.get(command, ""),
                boundary_confirmed=True, response_received=True,
            )

    async def run():
        seg_a, seg_b = 'tellraw @a {"text":"[Server] hi"}' * 1, 'tellraw @a {"text":"more"}'
        st = Stub({seg_a: EMPTY_SERVER, seg_b: EMPTY_SERVER})
        r = await m.McControlPlugin._exec_text_cmds(st, None, [seg_a, seg_b])
        check("★空服两段式：仍是 success/no_recipient，且**后续段照发**",
              r.status == "success" and r.no_recipient is True and len(st.sent) == 2,
              f"{r.status}/no_recipient={r.no_recipient}/sent={len(st.sent)}")

        st2 = Stub({seg_a: "Unknown or incomplete command, see below for error",
                    seg_b: EMPTY_SERVER})
        r2 = await m.McControlPlugin._exec_text_cmds(st2, None, [seg_a, seg_b])
        check("真失败仍立即中断（不继续发后续段）",
              r2.status == "syntax_error" and len(st2.sent) == 1,
              f"{r2.status}/sent={len(st2.sent)}")

        st3 = Stub({seg_a: EMPTY_SERVER})
        r3 = await m.McControlPlugin._exec_text_cmds(st3, None, [seg_a])
        text = m.McControlPlugin._render_result(
            st3, r3, "已在服务器内广播：hi", "广播失败")
        check("★回执文案：说「没有玩家在线」、且**不含**「失败」",
              "没有玩家在线" in text and "失败" not in text, text)
        check("回执文案仍保留 ok_text（消息确实发出去了）",
              "已在服务器内广播：hi" in text, text)

        ok = cr.classify_command_output(seg_a, "", boundary_confirmed=True, response_received=True)
        text_ok = m.McControlPlugin._render_result(st3, ok, "已在服务器内广播：hi", "广播失败")
        check("静默世代（无人也能收）不夹带空服提示",
              "没有玩家在线" not in text_ok, text_ok)

        bad = cr.classify_command_output("give Steve diamond 1", EMPTY_SERVER,
                                         boundary_confirmed=True, response_received=True)
        text_bad = m.McControlPlugin._render_result(st3, bad, "已发放", "发放失败")
        check("点名命令的失败回执照旧报失败（提示没被误加）",
              "失败" in text_bad and "没有玩家在线" not in text_bad, text_bad)

    asyncio.run(run())


def _source_scan() -> None:
    print("---- 六、源码登记 ----")
    src = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")
    check("常量 MESSAGE_DELIVERY_COMMANDS 已登记", "MESSAGE_DELIVERY_COMMANDS" in src)
    check("回执标记 _NO_RECIPIENT_MARKERS 已登记", "_NO_RECIPIENT_MARKERS" in src)
    check("通配选择器门 _SELECTOR_WILDCARD_RE 已登记", "_SELECTOR_WILDCARD_RE" in src)
    check("CommandResult 新字段 no_recipient 已登记", "no_recipient: bool = False" in src)
    check("v0.23.7 · N2 来历注释在案", "v0.23.7 · N2" in src)
    main_src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("main.py 回执渲染已接 no_recipient",
          'getattr(r, "no_recipient", False)' in main_src
          and "当前服务器内没有玩家在线" in main_src)


def _summary() -> None:
    print()
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项，跳过 {len(_skip)} 项")
    if _fail:
        print("失败清单：")
        for f in _fail:
            print("  -", f)
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
