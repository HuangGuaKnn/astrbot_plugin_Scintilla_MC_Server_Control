"""v0.24.5 回归：热重载目标不得越出插件目录。"""
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.hot_reload import plugin_dirs_for


class _Context:
    @staticmethod
    def get_all_stars():
        return []


class _PluginManager:
    context = _Context()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = root / "plugins"
        store.mkdir()
        (store / "safe_plugin").mkdir()
        pm = _PluginManager()
        pm.plugin_store_path = str(store)
        pm.reserved_plugin_path = ""

        assert plugin_dirs_for(pm, "safe_plugin") == [str(store / "safe_plugin")]
        for value in ("../outside", "..\\outside", "safe/../outside", "\x00"):
            assert plugin_dirs_for(pm, value) == [], value

    print("[ok] 热重载插件名路径穿越输入被拒绝")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
