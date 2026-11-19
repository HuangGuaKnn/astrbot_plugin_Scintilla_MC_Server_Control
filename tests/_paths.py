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
import re
import sys
import tempfile
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

# UI 用例（tests/ui_*_check.py）用的浏览器通道。
#   · 本机（Windows）：默认 "msedge" —— 用用户系统里现成的 Edge，不用下载 Chromium；
#   · CI（ubuntu）：没有 Edge → 设 SCINTILLA_UI_CHANNEL=chromium，用 playwright 自带的。
# 其它通道名（chrome / chromium / msedge / msedge-beta…）都按 playwright 原样透传。
UI_CHANNEL = os.environ.get("SCINTILLA_UI_CHANNEL", "msedge")

# 真正传给 playwright 的参数：特殊值 "bundled"（或空串）= 不指定 channel，
# 直接用 playwright 自带的 Chromium —— CI 上最省事，不必赌 channel 名字对不对。
UI_LAUNCH_KWARGS = {} if UI_CHANNEL.strip().lower() in ("", "bundled") else {"channel": UI_CHANNEL}


def _ui_shot_channel(channel: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", channel.strip().lower()) or "bundled"


def ui_shot_dir(channel: str = UI_CHANNEL) -> Path:
    """为本进程的截图创建独立目录。随机后缀让 PID 重用时也能保留旧截图。"""
    parent = PLUGIN_DIR / "tests" / "_shots" / _ui_shot_channel(channel)
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=parent, prefix=f"{os.getpid()}-"))


def check_ui_shots(directory: Path, expected: set[str], channel: str = UI_CHANNEL) -> None:
    root = PLUGIN_DIR / "tests" / "_shots"
    relative = directory.relative_to(root).parts if directory.is_relative_to(root) else ()
    if (len(relative) != 2 or relative[0] != _ui_shot_channel(channel)
            or not relative[1].startswith(f"{os.getpid()}-")):
        raise AssertionError(f"Q4 截图目录必须区分浏览器通道和进程：{directory}")
    actual = {path.name for path in directory.glob("*.png") if path.is_file() and path.stat().st_size}
    if actual != expected:
        raise AssertionError(f"Q4 截图文件不全。缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}")
    print(f"[PASS] Q4 {len(actual)} 张截图：{directory}")


# ── UI 用例的页面地址：走本地 HTTP，而不是 file:// ───────────────────────────
# 为什么必须这样（v0.24.11 连栽三次的坑）：页面用 `fetch("/api/...")` 打后端，
# 在 file:// 下会被解析成 file:///<盘符>/api/... —— Chromium 直接拒绝
# （"Fetch API cannot load ... URL scheme \"file\" is not supported"），
# playwright 的 page.route 也拦不到，于是表现为「点了保存却没有请求」的假红。
# 换成 http://127.0.0.1:<port> 后：同源 fetch 正常、route 能拦、localStorage/cookie
# 也跟真实环境一致（主题持久化那几个用例同样受益）。
_page_server = None


def serve_plugin_root() -> str:
    """起一个只读的本地 HTTP 服务（懒启动、进程内单例），返回插件根 URL。"""
    global _page_server
    if _page_server is None:
        import functools
        import http.server
        import threading

        class _Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):  # 静音：用例输出里不需要访问日志
                pass

        handler = functools.partial(_Quiet, directory=str(PLUGIN_DIR))
        _page_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=_page_server.serve_forever, daemon=True).start()
    host, port = _page_server.server_address[:2]
    return f"http://{host}:{port}"


def add_sys_paths() -> None:
    """把「插件根的上级」（供 import 包名）与 AstrBot app 目录塞进 sys.path。"""
    for p in (str(PLUGIN_DIR.parent), str(APP_DIR) if APP_DIR else None):
        if p and p not in sys.path:
            sys.path.insert(0, p)


def ensure_utf8_console() -> None:
    """直接手跑用例时的编码兜底（第九轮复核 P2-3）。

    干净 shell 里 Windows 的 stdout 常是 **GBK**，而用例末尾会打 `✅` 这类字符 ——
    `print` 抛 `UnicodeEncodeError`，结果是「**34 项全过、退出码却是 1**」，看着像坏了。
    门禁自己设了 `PYTHONIOENCODING=utf-8`，所以它看不见这个坑；人手跑就会撞上。

    只在「当前编码扛不住这些字符」时才动，并且 `errors="replace"` 兜底 ——
    编码问题绝不该让一条**已经通过**的判据变成失败。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = getattr(stream, "encoding", None)
            if not enc:
                continue
            "✅→♡".encode(enc)          # 扛得住就什么都不做
        except (UnicodeEncodeError, LookupError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except Exception:                                            # noqa: BLE001
                pass
        except Exception:                                                # noqa: BLE001
            continue


ensure_utf8_console()


def require_app() -> Path:
    """需要 AstrBot 运行时的用例用：拿不到就抛带说明的错误。

    重要：本函数必须在**模块加载阶段**调用，不能在已经跑起来的 asyncio 里调用。
    AstrBot 的包初始化一旦落在事件循环里就会假死（实测：进程活着、一个字节不输出）。
    这里主动拦住这种情况，避免下次又得到「测试莫名其妙挂住」。
    """
    if APP_DIR is None:
        raise RuntimeError(
            "找不到 AstrBot 运行目录：请设置环境变量 ASTRBOT_APP_DIR，"
            "或用 AstrBot 自带解释器（<AstrBot>/backend/python/python.exe）运行本测试。"
        )
    try:
        import asyncio  # noqa: PLC0415

        asyncio.get_running_loop()
    except RuntimeError:
        return APP_DIR
    raise RuntimeError(
        "require_app()/import astrbot 不能在事件循环内调用（会假死）。"
        "请把 import 提到模块顶部，只在 asyncio.run(...) 之前完成。"
    )
