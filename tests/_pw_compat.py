# -*- coding: utf-8 -*-
"""playwright 兼容层 —— 全仓**唯一**一份 `patch_pw_driver()`。

为什么要有这个文件（v0.24.11 的事故）
=====================================
原先 10 个 `ui_*.py` **各自抄了一份** `_patch_pw_driver()`：它去 import
`playwright._impl._transport` 来剥掉 Windows 路径的 `\\\\?\\` 前缀。
playwright 1.65 把这个内部模块移走之后，**10 份同时炸在 import 上** ——
CI 四个 UI 用例 0.0s 全灭，连页面都没打开（本机因为默认走系统 Edge + 旧版 playwright，
所以一直绿，谁也想不到）。

现在的口径：
* 只在 **Windows** 上做事（Linux / CI 根本不需要这层）；
* 取不到内部模块就**静默跳过**（判据本身不依赖它）；
* 全仓只此一处 —— 将来 playwright 再动内部结构，改一个文件就行。

用法（各 UI 用例里）：
    from _pw_compat import patch_pw_driver as _patch_pw_driver
    _patch_pw_driver()
"""

from __future__ import annotations

import os


def patch_pw_driver() -> None:
    """Windows 专用：把 node 驱动入口里的 `\\\\?\\` 前缀剥掉（拿不到内部模块就跳过）。"""
    if os.name != "nt":
        return
    try:
        import playwright._impl._transport as t  # noqa: WPS433
    except ModuleNotFoundError:
        return
    orig = getattr(t, "compute_driver_executable", None)
    if orig is None:
        return

    def patched():
        node, cli = orig()
        return (node.replace("\\\\?\\", ""), cli.replace("\\\\?\\", ""))

    t.compute_driver_executable = patched


# 兼容旧名字（有些用例里是 `_patch_pw_driver()` 直接调用）
_patch_pw_driver = patch_pw_driver

__all__ = ["patch_pw_driver", "_patch_pw_driver"]
