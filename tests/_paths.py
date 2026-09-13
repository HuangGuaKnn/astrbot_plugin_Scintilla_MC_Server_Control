"""测试用的路径发现 —— 一律不写死本机路径，换台机器也能跑。

发现顺序（都找不到就返回 None，让调用方自行决定跳过还是报错）：

  · 插件根      = 本文件（tests/_paths.py）的上一级
  · AstrBot app = 环境变量 ASTRBOT_APP_DIR
                  → AstrBot 自带解释器 <AstrBot>/backend/python/python.exe 同级的 app/
                  → import astrbot 反推
  · AstrBot data= 环境变量 ASTRBOT_DATA_DIR → ~/.astrbot/data

用法（测试脚本开头）：
    from _paths import PLUGIN_DIR, add_sys_paths, LIVE_CFG
    add_sys_paths()          # 把「插件根的上级」与 AstrBot app 塞进 sys.path
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_NAME = PLUGIN_DIR.name


def _astrbot_app() -> Path | None:
    env = os.environ.get("ASTRBOT_APP_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    # AstrBot 自带解释器：<AstrBot>/backend/python/python.exe → 同级 backend/app
    cand = Path(sys.executable).resolve().parents[1] / "app"
    if cand.is_dir():
        return cand
    try:
        import astrbot  # noqa: PLC0415
        return Path(astrbot.__file__).resolve().parents[1]
    except Exception:
        return None


def _astrbot_data() -> Path | None:
    env = os.environ.get("ASTRBOT_DATA_DIR")
    if env:
        return Path(env)
    cand = Path.home() / ".astrbot" / "data"
    return cand if cand.is_dir() else None


APP_DIR = _astrbot_app()
DATA_DIR = _astrbot_data()
LIVE_CFG = (DATA_DIR / "config" / f"{PLUGIN_NAME}_config.json") if DATA_DIR else None


def add_sys_paths() -> None:
    """把「插件根的上级」（供 import 包名）与 AstrBot app 目录塞进 sys.path。"""
    for p in (str(PLUGIN_DIR.parent), str(APP_DIR) if APP_DIR else None):
        if p and p not in sys.path:
            sys.path.insert(0, p)


def require_app() -> Path:
    """需要 AstrBot 运行时的用例用：拿不到就抛带说明的错误。"""
    if APP_DIR is None:
        raise RuntimeError(
            "找不到 AstrBot 运行目录：请设置环境变量 ASTRBOT_APP_DIR，"
            "或用 AstrBot 自带解释器（<AstrBot>/backend/python/python.exe）运行本测试。"
        )
    return APP_DIR
