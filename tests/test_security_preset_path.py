"""Regression checks for preset-id path confinement.

The WebUI exposes ``copy_from`` as an input.  It must resolve through the
registry and never be allowed to make the preset filename escape ``data_dir``.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (
    ModKnowledgeBase,
    KnowledgePresetManager,
)


def test_preset_path_rejects_traversal_components():
    with tempfile.TemporaryDirectory() as td:
        manager = KnowledgePresetManager(data_dir=td, server_id="test")
        for value in ("../secret", "..\\secret", "a/b", "a\\b", "\x00"):
            try:
                manager.preset_path(value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe preset id was accepted: {value!r}")


def test_create_copy_from_only_reads_registered_preset():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        manager = KnowledgePresetManager(data_dir=str(data_dir), server_id="test")
        source = manager.create("source")
        source_path = manager.preset_path(source["id"])
        source_path.write_text(
            json.dumps({"entries": {"known": {"content": "ok"}}}),
            encoding="utf-8",
        )

        # A traversal-like id must be treated as an unknown source, not opened.
        outside = data_dir.parent / "secret.json"
        outside.write_text(
            json.dumps({"entries": {"secret": {"content": "do not read"}}}),
            encoding="utf-8",
        )
        copied = manager.create("copy", copy_from="../../../secret")
        copied_data = json.loads(manager.preset_path(copied["id"]).read_text(encoding="utf-8"))
        assert copied_data.get("entries") == {}, copied_data


def test_malicious_registry_id_is_filtered_before_kb_load():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "kb_presets.json").write_text(
            json.dumps({
                "active": "../../escape",
                "presets": [{"id": "../../escape", "name": "bad"}],
            }),
            encoding="utf-8",
        )
        manager = KnowledgePresetManager(data_dir=str(data_dir), server_id="test")
        active = manager.active_preset() or {}
        assert active.get("id") != "../../escape"
        assert not (data_dir.parent / "escape.json").exists()


def test_direct_kb_construction_falls_back_from_unsafe_id():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        kb = ModKnowledgeBase(str(data_dir), preset_id="../../escape")
        assert kb.file_path.parent.resolve() == data_dir.resolve()
        assert kb.preset_id == "legacy_default"


def main() -> int:
    test_preset_path_rejects_traversal_components()
    test_create_copy_from_only_reads_registered_preset()
    test_malicious_registry_id_is_filtered_before_kb_load()
    print("[ok] 预设路径、复制来源与恶意注册表 ID 均保持在数据目录内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
