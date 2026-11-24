# -*- coding: utf-8 -*-
"""给子进程戴「不弹黑框」的帽子（Windows）。

背景（2026-11-24，主人反馈）：流水线跑起来时 python 黑框像潮水一样往外弹 ——
回归 110 件 + UI 18 件 + 变异 165 条里每条一次子进程，**每一个都是一次 CreateProcess**。
弹不弹取决于**父进程有没有控制台**：

* 父进程带着控制台（哪怕窗口不可见）→ 子进程继承它，不弹；
* 父进程没有控制台（AstrBot 后端的计划任务、Launcher 拉起的进程、CI runner 都是这样）
  → 每个子进程都会**新建一个可见控制台窗口**。

所以「本机手动跑没事、计划任务一跑就满屏框」不是玄学。修法只有一条：
**每个子进程都显式带上 `CREATE_NO_WINDOW`**，不指望继承。

用法::

    from _no_window import no_window_kw
    subprocess.run([...], **no_window_kw())
    subprocess.Popen([...], **no_window_kw())

判据在 ``tests/test_v0390_verify_pipeline.py``：既扫源码（每一处 spawn 都得带帽子，
用 AST 看，不受字符串/注释干扰），也起真进程量窗口（不带帽子必须能量到窗口，
否则说明量尺本身是坏的 —— 恒真的判据不如没有）。
"""
from __future__ import annotations

import os
import subprocess

#: Win32 ``CREATE_NO_WINDOW``（0x08000000）：给控制台程序一个**没有窗口**的控制台。
#: `subprocess.CREATE_NO_WINDOW` 只在 Windows 上存在，这里留一个字面量兜底。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

#: 非 Windows 上 0 = 不加任何 creationflags（POSIX 传非 0 会 ValueError）。
NO_WINDOW_FLAGS = CREATE_NO_WINDOW if os.name == "nt" else 0


def no_window_kw() -> dict:
    """直接展开进 ``subprocess.run/Popen/call/check_output`` 的 kwargs。

    非 Windows 返回空字典 —— 调用点写法一致，跨平台也不会炸。
    """
    return {"creationflags": NO_WINDOW_FLAGS} if NO_WINDOW_FLAGS else {}
