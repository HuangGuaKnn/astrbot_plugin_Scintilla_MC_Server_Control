"""v0.24.5 回归：LLM 工具开关遇到字符串配置时必须 fail-closed。

旧配置或手工编辑的 JSON 可能把布尔值写成 ``"false"``。Python 的
``bool("false")`` 是 True，曾导致用户明确关闭的工具仍然暴露给模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.main import McControlPlugin


class _FakePlugin:
    def __init__(self, value):
        self.value = value

    def _cfg_bool(self, key, default=False):
        """替身版严格布尔解析（v0.24.6：生产侧把布尔配置收口到 _cfg_bool）。

        v0.24.9（外部复核 P2）：总开关两个键**固定回 True**。
        否则本用例会退化成空断言 —— 替身对任何键都回 `self.value`，`enable_all_tools` 先一步
        把工具挡掉，8 条断言里 7 条就不再检验「单项开关的 fail-closed 语义」了（实测如此）。
        """
        if key in ("enable_all_tools", "enable_all_knowledge"):
            return True
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "y", "t")
        return bool(value)

    def _cfg(self, key, default=None):
        return self.value


def main() -> int:
    for value in (False, "false", "0", "off", "no", None, "maybe", 2):
        assert McControlPlugin._tool_enabled(_FakePlugin(value), "mc_give_item") is False, value
    for value in (True, "true", "1", "on", "yes"):
        assert McControlPlugin._tool_enabled(_FakePlugin(value), "mc_give_item") is True, value
    print("[ok] 工具开关对字符串 false/非法值保持 fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
