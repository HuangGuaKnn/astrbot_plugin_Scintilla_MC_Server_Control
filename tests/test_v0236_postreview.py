# -*- coding: utf-8 -*-
"""v0.23.6 市场审核跟进回归：主题偏好落点迁移 + 向量缓存去 pickle。

插件市场 LLM Guard 审核（2026-10-05）留下两条轻微建议，本文件逐条配可复现断言：

  A. `core/web_api.py` 把 UI 主题偏好写去了 `data/config/`，不符合持久化审计
     规范。本批迁到 `data/plugin_data/<插件名>/`（StarTools.get_data_dir 同款
     解析），旧位置存量文件在读取时一次性迁移（迁移后清理旧文件）。
  B. `core/knowledge_base.py` 的 `np.load(..., allow_pickle=True)`：旧存档把
     topics/hashes 存成 object 数组，读它必须开 pickle。本批新存档改用
     unicode 定长 dtype（无 pickle），旧缓存由兜底分支一次性读取后立即重写；
     结构校验照旧，不因迁移分支被绕过。

运行：
  <AstrBot python> tests\test_v0236_postreview.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ -> _paths
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import SemanticIndex  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import McControlWebApi  # noqa: E402

try:
    import numpy as np
except Exception:                                    # noqa: BLE001
    np = None

FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


class _FakePrefHost(McControlWebApi):
    """只替换两个路径解析的宿主：不跑真实 __init__，专测读取 / 迁移逻辑。"""

    def __init__(self, newp: Path, oldp) -> None:
        self._newp = Path(newp)
        self._oldp = Path(oldp) if oldp is not None else None

    def _ui_pref_path(self) -> Path:
        return self._newp

    def _legacy_ui_pref_path(self):
        return self._oldp


def part_a() -> None:
    print("========== [A] 向量缓存：去 pickle + 旧格式一次性迁移 ==========")
    if np is None:
        print("  [skip] 环境缺 numpy —— 跳过真实 npz 行为段")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        legacy = td / "legacy.npz"
        np.savez_compressed(legacy, matrix=np.zeros((2, 3), dtype="float32"),
                            topics=np.array(["a", "b"], dtype=object),
                            hashes=np.array(["h1", "h2"], dtype=object))
        sem = SemanticIndex()
        ok = sem.load(legacy)
        check("旧格式（object 数组、需 pickle）照常可加载",
              ok is True and sem.topics == ["a", "b"], f"ok={ok}")

        try:
            with np.load(legacy, allow_pickle=False) as z:
                migrated = [str(x) for x in z["topics"]]
            check("加载即迁移：重写后的文件 allow_pickle=False 直读通畅",
                  migrated == ["a", "b"], str(migrated))
        except Exception as e:                       # noqa: BLE001
            check("加载即迁移：重写后的文件 allow_pickle=False 直读通畅",
                  False, f"{type(e).__name__}: {e}")

        idx = SemanticIndex(dim=2)
        idx.set_vectors(["t1"], {"t1": {"content": "x", "enabled": True}}, [[1.0, 0.0]])
        p_new = td / "new.npz"
        ok_save = idx.save(p_new)
        try:
            with np.load(p_new, allow_pickle=False) as z:
                t = [str(x) for x in z["topics"]]
            check("新存档不依赖 pickle（allow_pickle=False 直读）",
                  ok_save is True and t == ["t1"], f"ok={ok_save} topics={t}")
        except Exception as e:                       # noqa: BLE001
            check("新存档不依赖 pickle（allow_pickle=False 直读）",
                  False, f"{type(e).__name__}: {e}")
        sem2 = SemanticIndex()
        check("新存档自产自销往返可加载",
              sem2.load(p_new) is True and sem2.topics == ["t1"])

        bad = td / "bad.npz"
        np.savez_compressed(bad, matrix=np.zeros((2, 3), dtype="float32"),
                            topics=np.array(["a", "b"], dtype=object),
                            hashes=np.array(["h1"], dtype=object))
        sem3 = SemanticIndex()
        check("结构校验不被迁移分支绕过（行数对不上 → 拒绝）",
              sem3.load(bad) is False and sem3.matrix is None)

        idx4 = SemanticIndex(dim=3)
        idx4.matrix = np.zeros((0, 3), dtype="float32")
        idx4.topics, idx4.hashes = [], []
        p_empty = td / "empty.npz"
        check("空索引可存盘（空 topics/hashes 边界）", idx4.save(p_empty) is True)
        sem4 = SemanticIndex()
        check("空索引往返可加载", sem4.load(p_empty) is True and sem4.topics == [])


def part_b() -> None:
    print("========== [B] 主题偏好：plugin_data 落点 + data/config 一次性迁移 ==========")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        newp = td / "plugin_data" / "astrbot_plugin_Scintilla_MC_Server_Control" / "mc_control_ui.json"
        oldp = td / "config" / "mc_control_ui.json"
        oldp.parent.mkdir(parents=True)
        oldp.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

        host = _FakePrefHost(newp, oldp)
        pref = host._read_ui_pref()
        check("旧位置（data/config）存量偏好可读", pref == {"theme": "dark"}, str(pref))
        check("读取即自动迁移进插件数据目录",
              newp.exists() and json.loads(newp.read_text(encoding="utf-8")) == {"theme": "dark"})
        check("迁移成功后旧文件被清理", not oldp.exists())

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        newp = td / "new" / "ui.json"
        oldp = td / "old" / "ui.json"
        newp.parent.mkdir(parents=True)
        oldp.parent.mkdir(parents=True)
        newp.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
        oldp.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        host = _FakePrefHost(newp, oldp)
        check("新位置优先，旧位置原样不动",
              host._read_ui_pref() == {"theme": "light"}
              and json.loads(oldp.read_text(encoding="utf-8")) == {"theme": "dark"})

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        host = _FakePrefHost(td / "n.json", td / "o.json")
        check("两边都没有 → 返回空（跟随系统）", host._read_ui_pref() == {})

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        newp = td / "n4.json"
        oldp = td / "o4.json"
        oldp.write_text("{ 这不是 json", encoding="utf-8")
        host = _FakePrefHost(newp, oldp)
        check("旧位置损坏 → 静默跳过、不误迁移",
              host._read_ui_pref() == {} and not newp.exists())

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        host = _FakePrefHost(td / "n5.json", None)
        check("旧位置解析不可用（None）→ 照常工作", host._read_ui_pref() == {})

    try:
        from astrbot.api.star import StarTools as _ST
        with tempfile.TemporaryDirectory() as td2:
            orig = _ST.get_data_dir
            _ST.get_data_dir = classmethod(
                lambda cls, name=None: Path(td2) / "plugin_data" / str(name))
            try:
                fake_self = types.SimpleNamespace(UI_PREF_NAME="mc_control_ui.json")
                p = McControlWebApi._ui_pref_path(fake_self)
                ok6 = str(p).replace("\\", "/").endswith(
                    "plugin_data/astrbot_plugin_Scintilla_MC_Server_Control/mc_control_ui.json")
            finally:
                _ST.get_data_dir = orig
        check("真实 _ui_pref_path：落点解析进 data/plugin_data/<插件名>/", ok6, str(p))
    except Exception as e:                           # noqa: BLE001
        check("真实 _ui_pref_path：落点解析进 data/plugin_data/<插件名>/",
              False, f"{type(e).__name__}: {e}")


def main() -> int:
    part_a()
    part_b()
    print()
    if FAILED:
        print(f"✗ 失败 {len(FAILED)} 项：")
        for d in FAILED:
            print("   -", d)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
