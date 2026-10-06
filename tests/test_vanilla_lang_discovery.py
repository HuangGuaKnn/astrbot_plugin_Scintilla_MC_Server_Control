# -*- coding: utf-8 -*-
"""v0.23.8 原版物品表「找 jar」发现逻辑 —— 载体形态矩阵。

运行：
  <python> tests/test_vanilla_lang_discovery.py

不依赖 AstrBot 运行时（``core/item_dictionary.py`` 只用标准库），
因此本文件在任何环境下都必须跑得起来、不许 SKIP。

背景（2026-10-06 真机实测）
==========================
只扫 ``libraries/net/minecraft/server/`` 与**根目录**时：

  · Paper 1.21.11  —— 根目录是 paperclip 引导 jar（不含 assets）→ 原版表 **0 条**；
  · Fabric 1.21.1  —— 根目录是 181 KB 的 fabric-server-launch.jar → 原版表 **0 条**；
  · 1.18+ 原版     —— 根 server.jar 是 bundler，assets 在内嵌版本 jar 里 → 同上。

本测试用**合成的 jar** 把这几类形态钉死：命中就是命中、落空就是落空，
新增落点不得把既有落点挤掉。
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

FAIL: list[str] = []

LANG_PATH = "assets/minecraft/lang/en_us.json"
LANG_JSON = json.dumps(
    {
        "item.minecraft.diamond_sword": "Diamond Sword",
        "block.minecraft.stone": "Stone",
    },
    ensure_ascii=False,
)

#: 非资产条目：模拟「引导 jar / 启动器 jar」——能被打开，但没有原版语言文件
NOISE_ENTRY = "meta/README.txt"


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(
        f"[{'PASS' if ok else 'FAIL'}] {desc}"
        + (f"  <- {detail}" if detail and not ok else "")
    )


def _ensure_core_package():
    """把 ``core/`` 注册成一个真正的包（``core/__init__.py`` 是空的，安全）。"""
    if "core" in sys.modules:
        return sys.modules["core"]
    spec = importlib.util.spec_from_file_location(
        "core",
        PLUGIN_DIR / "core/__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR / "core")],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["core"] = mod
    spec.loader.exec_module(mod)
    return mod


_LOADED: dict[str, object] = {}


def _load(name: str, rel: str):
    if name in _LOADED:
        return _LOADED[name]
    _ensure_core_package()
    spec = importlib.util.spec_from_file_location(f"core.{name}", PLUGIN_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"core.{name}"] = mod
    spec.loader.exec_module(mod)
    _LOADED[name] = mod
    return mod


# ================= 合成 jar =================
def _jar(path: Path, entries: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return path


def _bootstrap(path: Path) -> Path:
    """只有元数据、没有资产的引导 jar（paperclip / fabric-server-launch 形态）。"""
    return _jar(path, {NOISE_ENTRY: "bootstrap"})


def _bundler(path: Path, inner: str) -> Path:
    """原版 bundler 形态：外层只有 META-INF + 内嵌版本 jar（内含 assets）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(LANG_PATH, LANG_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/versions.list", "1.21.11")
        zf.writestr(inner, buf.getvalue())
    return path


def _build(root: Path):
    ID = _load("item_dictionary", "core/item_dictionary.py")
    d = ID.ItemDictionary(str(root), cache_path=None)
    stats = d.build()
    return d, stats


def _case(desc: str, layout, expect_hit: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        layout(root)
        try:
            d, stats = _build(root)
        except Exception as exc:  # 任何形态都不许把构建打崩
            check(f"{desc}：构建不得抛异常", False, f"{type(exc).__name__}: {exc}")
            return
        hit = "minecraft:diamond_sword" in d.items
        check(
            f"{desc} → {'应命中' if expect_hit else '应落空'}"
            f"（实测 {'命中' if hit else '落空'}，共 {stats['items']} 条）",
            hit == expect_hit,
            "落点没被扫到" if expect_hit and not hit else "",
        )
        # 原版表不进 mods 列表：mods 统计只认 mods/*.jar
        check(f"{desc} → mods 列表应为空", d.mods == [], f"{d.mods}")


# ================= 用例 =================
def baseline_cases() -> None:
    """既有落点不得回归（v0.21.20 就支持的两条路）。"""
    _case(
        "基线 · Forge/原版解包 libraries/net/minecraft/server/",
        lambda r: _jar(
            r / "libraries/net/minecraft/server/1.21.11/server-1.21.11.jar",
            {LANG_PATH: LANG_JSON},
        ),
        True,
    )
    _case(
        "基线 · 根目录老式 paper-x.jar（自带 assets）",
        lambda r: _jar(r / "paper-1.21.11.jar", {LANG_PATH: LANG_JSON}),
        True,
    )


def paper_cases() -> None:
    """Paper 真形态：根是引导 jar，本体在 versions/，原版核在 cache/。"""
    def paper_layout(r: Path) -> None:
        _bootstrap(r / "paper.jar")
        _jar(r / "versions/1.21.11/paper-1.21.11.jar", {LANG_PATH: LANG_JSON})
        _bundler(r / "cache/mojang_1.21.11.jar", "META-INF/versions/1.21.11/server-1.21.11.jar")

    _case("Paper 形态 · 本体在 versions/<版本>/paper-<版本>.jar", paper_layout, True)

    def paper_cache_only(r: Path) -> None:
        _bootstrap(r / "paper.jar")
        _bundler(r / "cache/mojang_1.21.11.jar", "META-INF/versions/1.21.11/server-1.21.11.jar")

    _case("Paper 形态 · 只有 cache/mojang_*.jar（versions 尚未解包）", paper_cache_only, True)


def bundler_cases() -> None:
    """1.18+ 原版 bundler：assets 只在内嵌版本 jar 里。"""
    _case(
        "原版 bundler · 未解包（根 server.jar 内嵌版本 jar）",
        lambda r: _bundler(r / "server.jar", "META-INF/versions/1.21.11/server-1.21.11.jar"),
        True,
    )
    _case(
        "原版 bundler · 已解包（versions/<版本>/server-<版本>.jar）",
        lambda r: (
            _bootstrap(r / "server.jar"),
            _jar(r / "versions/1.21.11/server-1.21.11.jar", {LANG_PATH: LANG_JSON}),
        ),
        True,
    )


def fabric_cases() -> None:
    _case(
        "Fabric 形态 · 原版核在 .fabric/server/",
        lambda r: (
            _bootstrap(r / "fabric-server-launch.jar"),
            _jar(r / ".fabric/server/1.21.1-server.jar", {LANG_PATH: LANG_JSON}),
        ),
        True,
    )


def negative_cases() -> None:
    """落空就是落空：不许为了「找得到」而乱认。"""
    _case("负例 · 空目录", lambda r: None, False)
    _case(
        "负例 · 全是引导 jar（任何一处都没有 assets）",
        lambda r: (
            _bootstrap(r / "paper.jar"),
            _bootstrap(r / "fabric-server-launch.jar"),
            (r / "versions/1.21.11").mkdir(parents=True, exist_ok=True),
        ),
        False,
    )

    def corrupt_layout(r: Path) -> None:
        (r / "paper.jar").write_bytes(b"not a zip at all")
        _jar(r / "versions/1.21.11/paper-1.21.11.jar", {LANG_PATH: LANG_JSON})

    _case("负例 · 坏 jar 在前、好 jar 在后（坏文件不得牵连整体）", corrupt_layout, True)

    def truncated_nested(r: Path) -> None:
        r.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(r / "server.jar", "w") as zf:
            zf.writestr("META-INF/versions/1.21.11/server-1.21.11.jar", b"\x00\x01\x02broken")

    _case("负例 · 内嵌版本 jar 损坏（不得抛出）", truncated_nested, False)


def main() -> int:
    baseline_cases()
    paper_cases()
    bundler_cases()
    fabric_cases()
    negative_cases()

    print("==========================================")
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过：原版表落点发现（libraries / 根目录 / versions / cache / .fabric 与 bundler 内嵌）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
