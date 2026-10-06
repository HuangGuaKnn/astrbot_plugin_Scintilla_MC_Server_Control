# -*- coding: utf-8 -*-
"""v0.23.8 · 设置页「版本原文」不再收下 Paper 的异步占位符。

背景（2026-10-06 载体验实测）
============================
Paper 1.21.11 台点火后，设置页「版本原文」显示的不是版本号，而是
``Checking version, please wait...`` —— 这是 Paper 的 **`version` 命令异步回包**：
命令当场只回这句占位符，真版本号稍后由服务端异步写进控制台，RCON 这条通道拿不到。

而 ``core/web_api.py`` 的探测兜底只排掉了 ``Unknown`` / ``incomplete``（原版 / Fabric
那条「命令不存在」的路），于是整句英文被当成版本原文，贴进服务器状态卡片。

修复：把「这一趟到底拿没拿到版本」抽成 ``is_version_reply_text()``（保守白名单），
占位符与报错特征词一并拦下 → 落回「未检测到（请检查 server_dir 配置）」。

本测试钉死六件事
================
1. **正例**：真版本原文（Paper / CraftBukkit / 裸版本号 / 多行回包）一律放行；
2. **反例**：占位符（含大小写与首尾空白变体）/ 命令不存在 / 半截回包 / 空串 → 一律拦下；
3. **回归**：原口径（``Unknown`` / ``incomplete``）不能被新过滤器吃掉；
4. **链路**：``web_api.py`` 的兜底**改用它**，旧写法 ``"Unknown" not in raw`` 已清零；
5. **回退文案**：拿不到时落回「未检测到（请检查 server_dir 配置）」，不静默变空串；
6. **来历注释在案**（v0.23.8）—— 后来人看得见为什么会有这四个词。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import (  # noqa: E402
    _VERSION_REPLY_PLACEHOLDERS,
    is_version_reply_text,
)

_fail: list[str] = []
_pass = 0


def check(desc: str, ok: bool, extra: str = "") -> None:
    global _pass
    print(f"[{'PASS' if ok else 'FAIL'}] {desc:<64}{extra}")
    if ok:
        _pass += 1
    else:
        _fail.append(desc)


# ============ 一、正例：真实版本原文必须放行 ============

def group_real_replies() -> None:
    print("---- 一、正例：真版本原文放行 ----")
    cases = {
        "Paper 1.21.11（真机回包原文）":
            "This server is running Paper version 1.21.11-132-master@5e5b2f2 (2025-12-09T12:00:00Z) "
            "(Implementing API version 1.21.11-R0.1-SNAPSHOT)\nYou are running the latest version",
        "CraftBukkit 1.13.2":
            "This server is running CraftBukkit version git-Spigot-1c5f5a1-1c5f5a1 (MC: 1.13.2)",
        "裸版本号":
            "1.21.11",
        "带前后空白的版本原文":
            "   This server is running Spigot version 1.20.1   ",
    }
    for label, raw in cases.items():
        check(f"放行：{label}", is_version_reply_text(raw) is True)


# ============ 二、反例：占位符 / 报错一律拦下 ============

def group_placeholders() -> None:
    print("---- 二、反例：占位符与报错一律拦下 ----")
    bad = {
        "Paper 异步占位符（原文）": "Checking version, please wait...",
        "Paper 异步占位符（大小写变体）": "CHECKING VERSION, PLEASE WAIT...",
        "Paper 异步占位符（前后空白）": "\n  checking version, please wait...  \n",
        "半句占位符（只留 please wait）": "Please wait",
        "命令不存在（原版 / Fabric）": 'Unknown command. Type "/help" for help.',
        "半截回包（服务端未就绪）": "Unknown or incomplete command, see below for error",
        "只留 unknown": "unknown",
        "空串": "",
        "纯空白": "   \t ",
        "None": None,
    }
    for label, raw in bad.items():
        check(f"拦下：{label}", is_version_reply_text(raw) is False)


# ============ 三、词表登记（后来人别把口径改窄） ============

def group_tokens() -> None:
    print("---- 三、占位符词表登记 ----")
    for token in ("unknown", "incomplete", "checking version", "please wait"):
        check(f"词表含 `{token}`", token in _VERSION_REPLY_PLACEHOLDERS)
    check("词表全小写（判定前统一 lower）",
          all(t == t.lower() for t in _VERSION_REPLY_PLACEHOLDERS), str(_VERSION_REPLY_PLACEHOLDERS))


# ============ 四、链路：兜底改用它，旧写法清零 ============

def group_wiring() -> None:
    print("---- 四、链路：探测兜底改用它 ----")
    src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    check("兜底调用 is_version_reply_text(raw)", "if is_version_reply_text(raw):" in src)
    check("★旧写法 `\"Unknown\" not in raw` 已清零", '"Unknown" not in raw' not in src)
    check("旧写法 `\"incomplete\" not in raw` 已清零", '"incomplete" not in raw' not in src)
    check("版本回退文案仍在（不是静默空串）",
          '未检测到（请检查 server_dir 配置）' in src)
    check("v0.23.8 来历注释在案", "v0.23.8" in src)


def main() -> int:
    group_real_replies()
    group_placeholders()
    group_tokens()
    group_wiring()
    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
