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

