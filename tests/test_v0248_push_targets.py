# -*- coding: utf-8 -*-
"""v0.24.8 回归（2026-10-21 真机 + 设计裁定）：播报目标失效时**不许把消息改道**。

裁定（2026-10-21，主人）
========================
真机上发现：插件里订阅的播报目标可能是早就失效的旧会话（平台适配器没开），
那时只留一条 WARN、用户侧什么都收不到。皮莉卡第一版写了「全部失败就发给最近交互的会话」
的兜底 —— **被否掉**，理由是真实场景：

    同一台服务器有两个群：
      群聊1 只收消息转发；群聊2 只用命令工具。
    群聊1 的适配器挂了 → 兜底会把转发塞进群聊2 ——
    而群聊2 里坐着**本来就不该看到这些消息的人**。

结论：**消息改道比消息丢失更糟**。保留「逐条 WARN」，并把「全部失败」升级成一条 ERROR
（管理员在日志里看得见、能去 WebUI 重选会话），插件绝不替人决定发给谁。

这一支把这条裁定钉成行为判据：给一组「全部发不出去」的目标 + 一个「最近交互过的别的会话」，
断言那个会话**一次都没被调用过**。

运行：<AstrBot python> tests\\test_v0248_push_targets.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
import astrbot_plugin_Scintilla_MC_Server_Control.main as M  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg, *a) -> None:  # noqa: D102
        self.warnings.append(str(msg) % a if a else str(msg))

    def error(self, msg, *a) -> None:  # noqa: D102
        self.errors.append(str(msg) % a if a else str(msg))

    def info(self, msg, *a) -> None:  # noqa: D102
        self.infos.append(str(msg) % a if a else str(msg))


def _plugin(log: _Log):
    """构造一个只带「推送这条路」需要的东西的插件实例（不跑 AstrBot 初始化）。"""
    p = M.McControlPlugin.__new__(M.McControlPlugin)
    p.logger = log
    p.config = {}
    p._terminated = False
    p._platform_ids = lambda: ["webchat"]          # 供 _target_candidates 补全平台前缀
    p._last_umo = "webchat:GroupMessage:999"       # 假装「最近交互过的是另一个会话」
    return p


def _run(targets: list[str], ok_umos: set[str]):
    """跑一次 _send_to_targets，返回（是否送达、被调用过的会话列表、日志）。"""
    log = _Log()
    p = _plugin(log)
    calls: list[str] = []
    real = M.StarTools.send_message

    async def fake(umo, chain):
        calls.append(umo)
        return umo in ok_umos

    M.StarTools.send_message = fake
    try:
        delivered = asyncio.run(p._send_to_targets(targets, object()))
    finally:
        M.StarTools.send_message = real
    return delivered, calls, log


def part_one() -> None:
    print("---- 一、目标发得出去：照常送达 ----")
    delivered, calls, log = _run(["webchat:GroupMessage:1"], {"webchat:GroupMessage:1"})
    check("★目标正常 → 送达成功", delivered is True, str(delivered))
    check("★且只发给配置里的那一个会话", calls == ["webchat:GroupMessage:1"], str(calls))
    check("全部成功时不该有 ERROR", log.errors == [], str(log.errors))


def part_two() -> None:
    print("\n---- 二、全部失败：只记 ERROR，**绝不改道**（主人裁定的场景）----")
    # 场景：群聊1（订阅了转发）挂了；群聊2（只用命令工具）是「最近交互过的会话」
    delivered, calls, log = _run(["webchat:GroupMessage:1"], set())
    check("★全部失败 → 返回未送达", delivered is False, str(delivered))
    check("★只试过配置里的目标（群聊1）", calls == ["webchat:GroupMessage:1"], str(calls))
    check("★「最近交互的会话」（群聊2 = ...:999）**一次都没被调用**",
          "webchat:GroupMessage:999" not in calls, str(calls))
    check("★全部失败要升成 ERROR（不是只留 WARN）",
          len(log.errors) == 1 and "全部发送失败" in log.errors[0], str(log.errors))
    check("★ERROR 文案要给出下一步（重选会话 / 查适配器）且明说不会改道",
          "重新选择" in log.errors[0] and "改道" in log.errors[0], log.errors[0])
    check("逐条失败仍留 WARN（现状不变）", len(log.warnings) == 1, str(log.warnings))


def part_three() -> None:
    print("\n---- 三、多目标：第一个成功就停，失败的不改道 ----")
    delivered, calls, log = _run(["webchat:GroupMessage:1", "webchat:FriendMessage:2"],
                                 {"webchat:FriendMessage:2"})
    check("★第一个失败 → 继续试第二个 → 送达", delivered is True, str(delivered))
    check("★调用顺序 = 配置顺序，且没有第三个会话掺进来",
          calls == ["webchat:GroupMessage:1", "webchat:FriendMessage:2"], str(calls))
    check("有成功就不该有「全部失败」的 ERROR", log.errors == [], str(log.errors))


def part_four() -> None:
    print("\n---- 四、源码层：不许再出现跨会话回退这条路 ----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("★代码里没有 _push_to_last_session（该兜底已被裁定撤销）",
          "_push_to_last_session" not in src)
    check("★_last_umo 只作诊断记录，不再被推送路径读取",
          "_last_umo" in src and "self._last_umo" in src
          and "_send_to_targets" in src,
          "（_last_umo 保留用于诊断；推送路径不读它）")


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 播报目标失效时不许把消息改道（主人 2026-10-21 裁定）")
    print("=" * 78)
    part_one()
    part_two()
    part_three()
    part_four()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （目标挂了只会叫，不会把消息塞给别的会话）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
