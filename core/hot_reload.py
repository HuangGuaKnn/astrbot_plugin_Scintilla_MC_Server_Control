"""插件热重载增强 · 「重载即生效」补丁（本插件内置）。

背景
====
AstrBot 的插件重载（WebUI 的「重载」按钮、插件市场里的「更新」）本身已经会清掉
`data.plugins.<插件目录>` 命名空间下的 `sys.modules` 缓存并重新 import，多数情况下
改代码后重载即可生效。但仍有几类漏网之鱼，表现出来就是「改了不生效，必须重启
AstrBot 才行」：

1. **模块名不在插件命名空间下**：部分插件会先 `sys.path.insert(...)` 再
   `import utils`，这类模块的 `__name__` 是顶层名（如 `utils`），而 AstrBot 只按
   `data.plugins.<目录>` 前缀做清理，扫不到它 —— 重载后加载的还是旧代码。
2. **`__pycache__` 陈旧字节码**：CPython 用「源文件 mtime（秒）+ 文件大小」判断
   `.pyc` 是否有效。若新文件与缓存里记录的两项完全一致（zip 解压回填时间戳、
   同一秒内同尺寸覆盖写入等），Python 会直接复用旧字节码。
3. **新放进来的插件目录**：单插件重载只会处理已在册的目录；新目录要等一次
   全量重载（`reload(None)`）才会被发现。

做法
====
本模块在插件被加载时给 `PluginManager.reload` 挂一个**幂等包装**（只在内存里打补丁，
不修改 AstrBot 安装目录下的任何文件，卸载插件即自动失效）：

* 按**文件真实路径**深度清理 `sys.modules`（覆盖第 1 类）；
* 删除目标插件的 `__pycache__` 并 `importlib.invalidate_caches()`（覆盖第 2 类）；
* 名字留空时等价于全量重载，顺带发现新目录（第 3 类交给 AstrBot 原生行为）。

原始流程（terminate → unbind → load）一个字节都没动，只是「擦得更干净」，
因此对所有插件都适用；AstrBot 升级也不会丢补丁 —— 插件每次加载都会重新钉上。

边界（诚实说明）
================
* 编译型扩展模块（`.pyd` / `.so`）无法热重载，真需要重启进程；
* AstrBot 自身的源码更新、Python 解释器重启仍然要重启；
* 补丁只覆盖同一个进程内未来的 reload 调用，历史实例的旧协程不受影响。
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import sys
from typing import Any

__all__ = [
    "deep_clean",
    "install_reload_patch",
    "is_patch_installed",
    "perform_hot_reload",
    "plugin_dirs_for",
    "uninstall_reload_patch",
]

_PATCH_FLAG = "_astrbot_mc_hot_reload_patch"
_ORIG_ATTR = "_astrbot_mc_original_reload"
_IMPL_ATTR = "_astrbot_mc_deep_clean_impl"

_log = logging.getLogger("astrbot")

_IS_WINDOWS = os.name == "nt"


def _norm(path: Any) -> str:
    """规范化路径，用于比较（Windows 下统一大小写与分隔符）。"""
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except Exception:  # pragma: no cover - 极端路径异常
        return os.path.normcase(str(path))


def _path_inside(candidate: Any, dirs: list[str]) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    normed = _norm(candidate)
    for folder in dirs:
        if normed == folder or normed.startswith(folder + os.sep):
            return True
    return False


def plugin_dirs_for(pm, name: str | None = None) -> list[str]:
    """解析目标插件目录。

    Args:
        pm: `PluginManager` 实例。
        name: 插件名（注册名或目录名）。留空表示全部插件。

    Returns:
        存在的插件目录绝对路径列表。
    """
    store = getattr(pm, "plugin_store_path", "") or ""
    reserved_store = getattr(pm, "reserved_plugin_path", "") or ""
    targets: list[str] = []

    if name:
        meta = None
        try:
            for smd in pm.context.get_all_stars():
                if getattr(smd, "name", None) == name or getattr(
                    smd, "root_dir_name", None
                ) == name:
                    meta = smd
                    break
        except Exception:
            meta = None
        if meta is not None and getattr(meta, "root_dir_name", ""):
            base = reserved_store if getattr(meta, "reserved", False) else store
            if base:
                targets.append(os.path.join(base, meta.root_dir_name))
        if not targets:
            for base in (store, reserved_store):
                if base:
                    targets.append(os.path.join(base, str(name)))
    else:
        for base in (store, reserved_store):
            if base and os.path.isdir(base):
                try:
                    for entry in os.scandir(base):
                        if entry.is_dir() and not entry.name.startswith(
                            (".", "__")
                        ):
                            targets.append(entry.path)
                except OSError:
                    continue

    seen: list[str] = []
    for path in targets:
        if path and os.path.isdir(path):
            normed = _norm(path)
            if normed not in [_norm(x) for x in seen]:
                seen.append(os.path.abspath(path))
    return seen


def _module_prefixes_for(pm, dirs: list[str]) -> list[str]:
    """推导插件目录对应的模块前缀（用于兜底清理没有 __file__ 的动态模块）。"""
    store = _norm(getattr(pm, "plugin_store_path", "") or "")
    reserved_store = _norm(getattr(pm, "reserved_plugin_path", "") or "")
    prefixes: list[str] = []
    for folder in dirs:
        root = os.path.basename(folder)
        parent = _norm(os.path.dirname(folder))
        if store and parent == store:
            prefixes.append(f"data.plugins.{root}")
        if reserved_store and parent == reserved_store:
            prefixes.append(f"astrbot.builtin_stars.{root}")
    return prefixes


def deep_clean(pm, name: str | None = None, *, drop_pycache: bool = True) -> dict:
    """深度清理目标插件的模块缓存与字节码缓存。

    Args:
        pm: `PluginManager` 实例。
        name: 插件名（注册名或目录名），留空表示全部插件。
        drop_pycache: 是否顺手删掉 `__pycache__`（默认删，彻底避免旧字节码）。

    Returns:
        统计信息 dict：`dirs` / `modules` / `module_names` / `pycache` / `prefixes`。
    """
    dirs = plugin_dirs_for(pm, name)
    norm_dirs = [_norm(d) for d in dirs]
    prefixes = _module_prefixes_for(pm, dirs)

    removed: list[str] = []
    if norm_dirs or prefixes:
        for key, module in list(sys.modules.items()):
            if module is None:
                continue
            hit = any(
                key == prefix or key.startswith(prefix + ".") for prefix in prefixes
            )
            if not hit:
                # 兜底：模块文件（或包的目录）落在插件目录里，无论它叫什么名字
                hit = _path_inside(getattr(module, "__file__", None), norm_dirs)
            if not hit:
                paths = getattr(module, "__path__", None)
                if paths:
                    try:
                        hit = any(
                            _path_inside(item, norm_dirs) for item in list(paths)
                        )
                    except Exception:
                        hit = False
            if hit:
                sys.modules.pop(key, None)
                removed.append(key)

    pycache_removed = 0
    if drop_pycache and sys.dont_write_bytecode is False:
        for folder in dirs:
            for root, subdirs, _files in os.walk(folder):
                if os.path.basename(root) == "__pycache__":
                    shutil.rmtree(root, ignore_errors=True)
                    pycache_removed += 1
                    subdirs[:] = []

    try:
        importlib.invalidate_caches()
    except Exception:  # pragma: no cover
        pass

    return {
        "dirs": dirs,
        "prefixes": prefixes,
        "modules": len(removed),
        "module_names": removed,
        "pycache": pycache_removed,
    }


def is_patch_installed() -> bool:
    """`PluginManager.reload` 是否已经挂上补丁。"""
    try:
        from astrbot.core.star.star_manager import PluginManager

        return bool(getattr(PluginManager.reload, _PATCH_FLAG, False))
    except Exception:
        return False


def install_reload_patch() -> bool:
    """给 `PluginManager.reload` 挂上「重载即生效」补丁（幂等）。

    Returns:
        True 表示本次真的钉上了补丁；False 表示之前已钉过或环境不支持。
    """
    try:
        from astrbot.core.star.star_manager import PluginManager
    except Exception as exc:
        _log.warning("[热重载] 取不到 PluginManager，补丁未安装：%s", exc)
        return False

    # 每次插件加载都刷新实现，保证补丁调用的是最新版 deep_clean
    setattr(PluginManager, _IMPL_ATTR, staticmethod(deep_clean))

    current = getattr(PluginManager, "reload", None)
    if current is None:
        _log.warning("[热重载] PluginManager.reload 不存在，补丁未安装。")
        return False
    if getattr(current, _PATCH_FLAG, False):
        return False

    setattr(PluginManager, _ORIG_ATTR, current)

    async def reload_with_deep_clean(self, specified_plugin_name=None):
        try:
            impl = getattr(PluginManager, _IMPL_ATTR, None)
            if impl is not None:
                summary = impl(self, specified_plugin_name)
                if summary["modules"] or summary["pycache"]:
                    _log.info(
                        "[热重载] 预清理完成：%d 个模块缓存、%d 个 __pycache__（目标：%s）",
                        summary["modules"],
                        summary["pycache"],
                        specified_plugin_name or "全部插件",
                    )
        except Exception as exc:
            _log.warning("[热重载] 深度清理失败，已回退原生流程：%s", exc)
        original = getattr(PluginManager, _ORIG_ATTR)
        return await original(self, specified_plugin_name)

    setattr(reload_with_deep_clean, _PATCH_FLAG, True)
    setattr(PluginManager, "reload", reload_with_deep_clean)
    _log.info("[热重载] 已给 PluginManager.reload 挂上「重载即生效」补丁。")
    return True


def uninstall_reload_patch() -> bool:
    """还原 `PluginManager.reload`（panic 按钮，一般用不到）。"""
    try:
        from astrbot.core.star.star_manager import PluginManager
    except Exception:
        return False
    current = getattr(PluginManager, "reload", None)
    if current is None or not getattr(current, _PATCH_FLAG, False):
        return False
    original = getattr(PluginManager, _ORIG_ATTR, None)
    if original is None:
        return False
    setattr(PluginManager, "reload", original)
    _log.info("[热重载] 补丁已卸载，恢复原生 reload。")
    return True


async def perform_hot_reload(pm, name: str | None = None) -> tuple[bool, str]:
    """主动做一次「深度清理 + 重载」。

    Args:
        pm: `PluginManager` 实例。
        name: 插件名；留空 = 全量重载（顺带发现新放进来的插件目录）。

    Returns:
        `(ok, message)`：ok 为是否成功，message 为给人看的说明。
    """
    if pm is None:
        return False, "未取到插件管理器（AstrBot 版本差异？），已放弃热重载。"

    dirs = plugin_dirs_for(pm, name)
    if name and not dirs:
        return False, f"未找到插件「{name}」的目录，无法重载。"

    summary = deep_clean(pm, name)
    try:
        success, message = await pm.reload(name or None)
    except Exception as exc:
        return False, f"重载时抛出异常：{exc}"

    cleaned = f"清理 {summary['modules']} 个模块缓存、{summary['pycache']} 个 __pycache__"
    if success:
        target = f"插件「{name}」" if name else "全部插件"
        return True, f"{target}已重载（{cleaned}）。"
    return False, f"重载失败：{message}（已{cleaned}）"


def _self_test() -> None:  # pragma: no cover - 手动调试用
    """离线自检：确认补丁能正常挂载/卸载。"""
    print("installed:", install_reload_patch())
    print("is_patch_installed:", is_patch_installed())
    print("uninstalled:", uninstall_reload_patch())


if __name__ == "__main__":  # pragma: no cover
    _self_test()
