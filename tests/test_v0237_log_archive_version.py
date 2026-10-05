# -*- coding: utf-8 -*-
"""v0.23.7 · N3：版本探测的**归档日志回退**（跨零点轮转不致版本丢失）。

现场（2026-10-06 00:35 真机巡礼抓到的）
=======================================
1.13.2 站点上，含全角「（」的广播**本该豁免洗白**（门控 = ≤1.8.9），
却照样洗了。取证结果：

  · `logs/latest.log` 只剩 **70 字节**（一行 `Rcon connection from: …`）；
  · 启动行 `Starting minecraft server version 1.13.2` 被压进了
    `logs/2026-10-05-6.log.gz`（mtime = 跨零点后的第一条写入时刻）；
  · 插件的 `server/status` 报 `version_caps.known = false`（版本未知）。

机制：原版 / Forge 的 `latest.log` **每日轮转** —— 跨零点后的第一条写入把当天内容
整段归档，启动行随之离开现役日志。于是版本解析成「未知」（= **保守档**：洗白开、
带数据命令不自动生成），**服务端长期不重启的用户每到零点就静默退化一次**，
重启服务端才「神奇恢复」。

修复：`detect_server_version()` 在 latest.log 查不到时**回退扫归档**
（`logs/*.log.gz`，自新到旧、只读压缩包头部、总量封顶）；解析正则抽成
`_match_startup_version()`，两处共用。另：`reset_rcon()` 里把版本快照缓存一并作废
（换站点后不得沿用上一台的版本判门控）。

本测试钉死：
1. 正例：latest.log 无启动行 + 归档有 → 能从归档认出（原版与 Forge 两种措辞）；
2. 优先级：latest.log 有启动行时**不**看归档（现役日志永远优先）；
3. 新胜旧：多个归档取 mtime 最新的那个；
4. 容错：坏 gzip 不抛异常，继续看下一个有效归档；
5. 边界：没有日志 / 目录不存在 / 异地 RCON 模式 → 返回空串（不假装知道）；
6. 缓存：reset_rcon() 作废版本快照；
7. 源码登记 + 真实现场回归（live 配置里的 server_dir）。
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import LIVE_CFG, PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
from _paths import require_app  # noqa: E402

require_app()  # AstrBot 运行目录：自动发现（见 tests/_paths.py）

import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402

_fail: list[str] = []
_pass = 0
_skip: list[str] = []

STARTUP_113 = "[14:32:00] [Server thread/INFO]: Starting minecraft server version 1.13.2\n"
STARTUP_189 = "[16:00:00] [Server thread/INFO]: Starting minecraft server version 1.8.9\n"
FORGE_201 = "[12:00:00] [main/INFO]: Forge mod loading, version 47.4.23, for MC 1.20.1\n"


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


def skip(name: str, why: str) -> None:
    _skip.append(name)
    print(f"[SKIP] {name}  <- {why}")


class FakeCfg(dict):
    def save_config(self) -> bool:
        return True


def make_plugin(cfg: dict):
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg(cfg)
    plug._cfg_index = {}
    plug.logger = logging.getLogger("n3")
    return plug


def make_server(root: Path, latest: str = "", archives=(), markers: bool = True) -> Path:
    """造一个最小「真服务端」形状的目录（只看探测需要的部分）。"""
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "latest.log").write_text(latest, encoding="utf-8")
    base = time.time() - 100000
    for name, text, age in archives:
        ap = logs / name
        with gzip.open(ap, "wt", encoding="utf-8") as f:
            f.write(text)
        os.utime(ap, (base - age, base - age))     # age 越大越旧
    if markers:
        (root / "eula.txt").write_text("eula=true", encoding="utf-8")
        (root / "server.properties").write_text("level-name=world\n", encoding="utf-8")
    return root


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="n3_"))

    print("---- 一、正例：现役日志没有，归档里有 ----")
    d = make_server(tmp / "a", latest="[00:35:01] [RCON Listener #1/INFO]: Rcon connection from: /127.0.0.1\n",
                    archives=[("2026-10-05-6.log.gz", STARTUP_113, 10)])
    p = make_plugin({"server_dir": str(d)})
    check("★latest.log 只有连接行 + 归档含启动行 → 认出 MC 1.13.2",
          p.detect_server_version() == "MC 1.13.2", p.detect_server_version())

    d2 = make_server(tmp / "b", archives=[("2026-10-05-3.log.gz", FORGE_201, 1)])
    check("★Forge 措辞在归档里同样认得出（MC 1.20.1 · Forge 47.4.23）",
          make_plugin({"server_dir": str(d2)}).detect_server_version() == "MC 1.20.1 · Forge 47.4.23",
          make_plugin({"server_dir": str(d2)}).detect_server_version())

    print("---- 二、优先级：现役日志永远优先 ----")
    d3 = make_server(tmp / "c", latest=STARTUP_189,
                     archives=[("2026-10-05-6.log.gz", STARTUP_113, 1)])
    check("★latest.log 有启动行 → 不采信归档（1.8.9 胜 1.13.2）",
          make_plugin({"server_dir": str(d3)}).detect_server_version() == "MC 1.8.9",
          make_plugin({"server_dir": str(d3)}).detect_server_version())

    print("---- 三、新胜旧 + 容错 ----")
    d4 = make_server(tmp / "d", archives=[("2026-10-04-1.log.gz", STARTUP_189, 9999),
                                         ("2026-10-05-6.log.gz", STARTUP_113, 1)])
    check("★多个归档取 mtime 最新（1.13.2 胜 1.8.9）",
          make_plugin({"server_dir": str(d4)}).detect_server_version() == "MC 1.13.2",
          make_plugin({"server_dir": str(d4)}).detect_server_version())

    d5 = make_server(tmp / "e", archives=[("2026-10-05-6.log.gz", "这不是 gzip 正文", 1),
                                         ("2026-10-05-5.log.gz", STARTUP_113, 2)])
    (d5 / "logs" / "2026-10-05-6.log.gz").write_bytes(b"not a gzip at all")
    os.utime(d5 / "logs" / "2026-10-05-6.log.gz", (time.time(), time.time()))
    check("★坏归档不抛异常、继续看下一个有效归档",
          make_plugin({"server_dir": str(d5)}).detect_server_version() == "MC 1.13.2",
          make_plugin({"server_dir": str(d5)}).detect_server_version())

    print("---- 四、边界：不知道就说不知道 ----")
    d6 = make_server(tmp / "f", latest="")
    check("无启动行且无归档 → 空串（不猜版本）",
          make_plugin({"server_dir": str(d6)}).detect_server_version() == "",
          make_plugin({"server_dir": str(d6)}).detect_server_version())
    check("目录不存在 → 空串",
          make_plugin({"server_dir": str(tmp / "no_such_dir")}).detect_server_version() == "")
    check("异地 RCON 模式 → 空串（读不到文件，不假装能探测）",
          make_plugin({"server_dir": str(d), "remote_rcon_mode": True}).detect_server_version() == "")

    print("---- 五、缓存：reset_rcon() 一并作废 ----")
    plug = make_plugin({"rcon_host": "127.0.0.1", "rcon_port": 25703, "rcon_password": "x"})
    plug._rcon_lock = asyncio.Lock()
    plug._rcon = None
    plug._build_rcon = lambda: None
    plug._text_version_cache = (time.monotonic(), "MC 1.8.9")

    async def run_reset():
        await plug.reset_rcon()

    asyncio.run(run_reset())
    check("★换站点/重置连接后版本快照缓存被清空（不再按上一台判门控）",
          plug._text_version_cache is None, str(plug._text_version_cache))

    _source_scan()
    _live_probe()
    _summary()


def _source_scan() -> None:
    print("---- 六、源码登记 ----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("gzip 已导入", "\nimport gzip\n" in src)
    check("解析器 _match_startup_version 已抽出", "def _match_startup_version" in src)
    check("归档回退常量在案",
          "_ARCHIVE_LOG_MAX_FILES" in src and "_ARCHIVE_LOG_BUDGET_CHARS" in src)
    check("现役日志分支改走共用解析器",
          "got = self._match_startup_version(head)" in src)
    check("归档回退分支在案（*.log.gz + mtime 倒序）",
          'glob("*.log.gz")' in src and "reverse=True" in src)
    check("reset_rcon 里作废版本缓存", "self._text_version_cache = None" in src)
    check("v0.23.7 · N3 来历注释在案", "v0.23.7 · N3" in src)


def _live_probe() -> None:
    print("---- 七、真实现场回归 ----")
    try:
        import json

        cfg = json.loads(Path(LIVE_CFG).read_text(encoding="utf-8-sig"))
        sd = (cfg.get("connection") or {}).get("server_dir") or cfg.get("server_dir") or ""
    except Exception as e:  # noqa: BLE001
        skip("现场回归", f"读不到 live 配置：{type(e).__name__}")
        return
    if not sd or not Path(sd).is_dir():
        skip("现场回归", f"server_dir 不在本机：{sd}")
        return
    got = make_plugin({"server_dir": sd}).detect_server_version()
    check(f"现场 {Path(sd).name}：现役/归档合起来仍能认出非空版本（{got or '空'}）",
          bool(got), got)


def _summary() -> None:
    print()
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项，跳过 {len(_skip)} 项")
    if _fail:
        print("失败清单：")
        for f in _fail:
            print("  -", f)
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
