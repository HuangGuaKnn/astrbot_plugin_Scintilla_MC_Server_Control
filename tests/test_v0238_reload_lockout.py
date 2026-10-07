# -*- coding: utf-8 -*-
"""v0.23.8 · 热重载自锁时必须如实回报并给出可执行出路。

背景（2026-10-07 深夜排查）
==========================
插件的热重载要靠**后台任务登记册**排一个 hot_reload 任务；登记册一旦「收口」
（本实例走过 terminate：重载竞态、被停用等），此后再 spawn 一律丢弃。

于是形成死锁：**插件自己再也排不出重载**。更糟的是旧文案对这种情况照样说
「已经有一次重载在排队了…（热重载大约 1 秒完成）」—— 一句等不到的承诺。

本用例钉死三件事
================
1. 收口态 → 文案**必须**指出出路（AstrBot 插件管理页 / 重启），且**不得**再出现
   「大约 1 秒完成」这类等得到的暗示；
2. 未收口、只是同名任务在跑 → 保留原「已在排队」文案（不能把正常等待说成故障）；
3. `_bg_registry_closed()` 读登记册真身 `_closing`；`_bg` 缺失时不抛异常（失败关闭）。

运行：python tests/test_v0238_reload_lockout.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

import astrbot_plugin_Scintilla_MC_Server_Control.main as main  # noqa: E402

FAIL: list[str] = []


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Bg:
    """登记册替身：只暴露收口标志（真身属性名 _closing）。"""

    def __init__(self, closing: bool) -> None:
        self._closing = closing


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {extra}" if extra and not ok else ""))
    if not ok:
        FAIL.append(desc)


def make_stub(closing: bool):
    """真身插件实例（绕过 __init__），只喂 _schedule_hot_reload 用到的那几样。"""
    m = main.McControlPlugin.__new__(main.McControlPlugin)
    m._bg = _Bg(closing)
    m.logger = _Logger()
    m._plugin_manager = lambda: object()          # 非 None 即可

    def _spawn_bg(name, coro, **kw):
        coro.close()                              # 别留未 await 的协程
        return None                               # 模拟「登记册不收」→ 本次排不上

    m._spawn_bg = _spawn_bg
    return m


async def _schedule(m) -> str:
    # 函数要求有 running loop；target=None 会跳过目录校验，专测回报口径
    return m._schedule_hot_reload(None)


def run() -> int:
    stub = make_stub(True)
    msg = asyncio.run(_schedule(stub))
    check("收口态文案指出出路（插件管理页）", "插件管理页" in msg, msg)
    check("收口态不再承诺「大约 1 秒完成」", "大约 1 秒完成" not in msg, msg)
    check("收口态说明原因是登记册收口", "收口" in msg, msg)

    stub2 = make_stub(False)
    msg2 = asyncio.run(_schedule(stub2))
    check("未收口时仍是「已在排队」的正常口径", "已经有一次重载在排队了" in msg2, msg2)

    check("_bg_registry_closed 读 _closing=True", stub._bg_registry_closed() is True)
    check("_bg_registry_closed 读 _closing=False", stub2._bg_registry_closed() is False)
    bare = main.McControlPlugin.__new__(main.McControlPlugin)
    check("缺 _bg 时不抛异常（失败关闭）", bare._bg_registry_closed() is False)

    # 4）替身 / 降级装配：连这个方法都没有时，重载入口照样要活着（不许 AttributeError）
    class _NoMethod:
        pass

    nom = _NoMethod()
    # 替身只缺「收口探测」这一个方法，调度器本身用真身（这是本用例的考点）
    nom._schedule_hot_reload = types.MethodType(main.McControlPlugin._schedule_hot_reload, nom)
    nom._bg = _Bg(True)
    nom.logger = _Logger()
    nom._plugin_manager = lambda: object()
    nom._spawn_bg = lambda name, coro, **kw: (coro.close(), None)[1]
    msg3 = asyncio.run(_schedule(nom))
    check("替身连 _bg_registry_closed 都没有时不抛异常", isinstance(msg3, str), repr(msg3))
    check("此时落回「已在排队」的正常口径", "已经有一次重载在排队了" in msg3, msg3)

    print()
    if FAIL:
        print("失败 %d 项：%s" % (len(FAIL), " / ".join(FAIL)))
        return 1
    print("通过 9 项，失败 0 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
