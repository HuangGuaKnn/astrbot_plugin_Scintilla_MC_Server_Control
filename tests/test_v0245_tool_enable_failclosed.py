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
