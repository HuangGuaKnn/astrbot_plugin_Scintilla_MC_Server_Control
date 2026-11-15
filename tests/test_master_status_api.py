"""概览、工作流页和设置页读取栏级总开关的接口契约。"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.main import McControlPlugin  # noqa: E402


def make_plugin(**settings):
    plugin = McControlPlugin.__new__(McControlPlugin)
    plugin.config = settings
    plugin._cfg_index = {}
    plugin.logger = logging.getLogger(__name__)
    plugin._dictionary = SimpleNamespace(mods=["create"], items=["brass_ingot"], recipes=[])
    plugin._knowledge = SimpleNamespace(
        server_id="test",
        state=SimpleNamespace(as_dict=lambda: {"enabled": True}),
        stats=lambda: {"total": 1},
    )
    plugin.server_content_text = lambda: ""
    plugin._workflow = SimpleNamespace(recent_logs=lambda limit: [{"status": "done"}])
    return plugin


async def run() -> None:
    plugin = make_plugin(
        enable_all_knowledge=False, enable_all_tools=False, enable_mc_workflow=True,
    )
    api = McControlWebApi(plugin)

    overview = json.loads((await api.get_overview()).body)
    dictionary = overview["dictionary"]
    knowledge = overview["knowledge"]
    assert dictionary.get("master_enabled") is False, ("概览词典应返回总开关关闭", dictionary)
    assert knowledge.get("master_enabled") is False, ("概览知识库应返回总开关关闭", knowledge)
    assert dictionary["enabled"] and dictionary["items"] == 1, dictionary
    assert knowledge["enabled"] and knowledge["stats"]["total"] == 1, knowledge

    for status in (overview["workflow"], json.loads((await api.get_workflow_status()).body)):
        assert status.get("master_enabled") is False, ("工作流应返回总开关关闭", status)
        assert status.get("tool_enabled") is False, ("总开关关闭时工作流工具应不可用", status)
        assert status.get("tool_setting_enabled") is True, ("工作流单项仍为开启", status)

    plugin.config.update(enable_all_knowledge=True, enable_all_tools=True, enable_mc_workflow=False)
    overview = json.loads((await api.get_overview()).body)
    assert overview["dictionary"]["master_enabled"] and overview["knowledge"]["master_enabled"], overview
    status = json.loads((await api.get_workflow_status()).body)
    assert status["master_enabled"] and not status["tool_enabled"], status
    assert status["tool_setting_enabled"] is False, status

    plugin.config["enable_mc_workflow"] = "maybe"
    status = json.loads((await api.get_workflow_status()).body)
    assert status["tool_enabled"] is False, ("工作流单项无法解析时应按关闭显示", status)

    plugin.config.update(
        enable_all_knowledge="", enable_all_tools="", enable_mc_workflow=True, dictionary_enabled=False,
    )
    settings = json.loads((await api.get_settings()).body)["settings"]
    assert settings.get("enable_all_tools") is True, ("设置页工具总开关的空串应显示为开启", settings)
    assert settings.get("enable_all_knowledge") is True, ("设置页知识总开关的空串应显示为开启", settings)
    assert settings.get("dictionary_enabled") is False, settings
    assert plugin.config["enable_all_knowledge"] == plugin.config["enable_all_tools"] == ""
    overview = json.loads((await api.get_overview()).body)
    assert overview["workflow"]["tool_enabled"] is True, overview["workflow"]

    plugin._dictionary = plugin._knowledge = None
    plugin.config.update(enable_all_knowledge=False, dictionary_enabled=False, knowledge_enabled=False)
    overview = json.loads((await api.get_overview()).body)
    for key in ("dictionary", "knowledge"):
        assert overview[key]["master_enabled"] is False and overview[key]["enabled"] is False, overview[key]

    with tempfile.TemporaryDirectory() as data_dir:
        config = Path(data_dir) / "config.json"
        config.write_text('{"tools":{"enable_all_tools":false},"knowledge":{"enable_all_knowledge":""}}',
                          encoding="utf-8")
        api._cfg_path = lambda: config
        plugin.config.clear()
        settings = json.loads((await api.get_settings()).body)["settings"]
        assert settings["enable_all_tools"] is False, ("磁盘回退应保留显式关闭的总开关", settings)
        assert settings["enable_all_knowledge"] is True, ("磁盘回退的总开关空串应显示为开启", settings)

    print("[PASS] 概览与工作流区分总开关、实际可用状态和单项配置；设置页按实际值回填总开关")


if __name__ == "__main__":
    asyncio.run(run())
