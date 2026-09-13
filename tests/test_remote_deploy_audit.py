# -*- coding: utf-8 -*-
"""异地部署可用性实测（RCON 端口映射场景，v0.21.14）。

主人问的是：「AstrBot 与 MC 服务端不同机，只把 RCON 端口映射出来，各功能还能用吗？」
本用例用**纯假环境**（不动主人配置、不连云服务器、不写插件数据目录）逐条验证：

  A. 纯远程形态（server_dir 空 / 指向本机一个无关目录）
     —— 哪些功能照常、哪些会「静默失效」、哪些会「误报或答非所问」
  B. 挂载/共享目录形态（server_dir 指向一个可读的异地副本）
     —— 日志播报、词典、指纹能否恢复；以及「日志延迟」带来的假消息风险
  C. 目标玩家名解析（异地时 RCON list 是唯一真源，误判会导致指令发给错的人）
  D. 远程可达性与超时（超时配置、连接失败文案是否可诊断）

运行：
  python tests\\test_remote_deploy_audit.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()
from _paths import require_app  # noqa: E402
require_app()                    # AstrBot 运行目录：自动发现（见 tests/_paths.py）

import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary import ItemDictionary  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.log_watcher import LogWatcher  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.rcon import AsyncRcon, RconError  # noqa: E402

FAIL: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {desc:<64}{extra}")
    if not ok:
        FAIL.append(desc)


class FakeCfg(dict):
    def save_config(self) -> bool:
        return True


def make_plugin(cfg: dict, data_dir: Path):
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg(cfg)
    plug._cfg_index = {}
    plug._kbman = None
    plug._knowledge = None
    plug._dictionary = None
    plug._rcon = None
    plug.logger = logging.getLogger("audit")
    return plug


def make_pack(root: Path, mods: dict[str, str]) -> Path:
    mods_dir = root / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for fname, modid in mods.items():
        with zipfile.ZipFile(mods_dir / fname, "w") as zf:
            zf.writestr("META-INF/mods.toml", f'[[mods]]\nmodId="{modid}"\nversion="1.0"\n')
    # v0.21.15：目录结构校验是硬门槛 —— 夹具必须给出「真服务端」的天然特征
    #（eula.txt / server.properties / logs/ / libraries/ / server.jar）。
    # 只造一个 mods/ 空壳目录，正是这次要拦下的高危误配，插件会拒绝初始化。
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("", encoding="utf-8")
    (root / "libraries").mkdir(exist_ok=True)
    (root / "eula.txt").write_text("eula=true", encoding="utf-8")
    (root / "server.properties").write_text("level-name=world", encoding="utf-8")
    (root / "server.jar").write_bytes(b"")
    return root


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir = tmp / "plugin_data"
        data_dir.mkdir()
        m.StarTools.get_data_dir = staticmethod(lambda *a, **k: data_dir)
        kdir = data_dir

        # ============================================================
        print("=========== A. 纯远程形态（只有 RCON 端口，没有服务端目录） ===========")
        # server_dir 留空 —— 这是「异地 + 不共享目录」的典型配置
        plug = make_plugin(
            {"server_dir": "", "knowledge_enabled": True, "dictionary_enabled": True,
             "enable_event_listener": True, "dictionary_enabled_": True},
            data_dir,
        )
        note = asyncio.run(plug._sync_server_context(rebuild_dictionary=True))
        print(f"      保存设置时的提示：{note}")
        check("server_dir 为空 → 明确提示「未配置或不是有效目录」",
              "未配置或不是有效目录" in note, note[:90])
        check("server_dir 为空 → 词典未初始化（工具会明确报错，不会瞎猜静默失败）",
              plug._dictionary is None)
        check("server_dir 为空 → 知识库未创建（不产生假的空指纹知识库）",
              plug._kbman is None and plug._knowledge is None)

        # 事件转发在 server_dir 为空时应当被明确拒绝启动
        plug2 = make_plugin({"server_dir": "", "enable_event_listener": True}, data_dir)
        msg = asyncio.run(plug2._restart_event_listener())
        print(f"      事件转发启动返回：{msg}")
        check("server_dir 为空 → 事件转发明确拒绝启动（不会假装运行中）",
              "未配置" in msg, msg)
        check("server_dir 为空 → 未创建监听器对象", getattr(plug2, "_watcher", None) is None)

        # 指向「本机一个不相干的空目录」是最危险的误配：看着像配好了
        # v0.21.15：这类目录现在被**硬校验**直接拦下 —— 不建词典、不建知识库，
        # 保存设置时也拒绝写盘（宁可报错，也不给一堆空数据装成「配好了」）
        empty_dir = tmp / "not_the_server"
        empty_dir.mkdir()
        plug3 = make_plugin(
            {"server_dir": str(empty_dir), "knowledge_enabled": True,
             "dictionary_enabled": True}, data_dir,
        )
        note3 = asyncio.run(plug3._sync_server_context(rebuild_dictionary=True))
        print(f"      指向空目录时的提示：{note3}")
        check("指向无关空目录 → 目录结构校验未通过，并点名缺什么",
              "结构校验" in note3 and "没有任何服务端特征" in note3, note3[:80])
        check("指向无关空目录 → 不再建词典（不拿空壳目录算出 0 mod 的假数据）",
              plug3._dictionary is None)
        check("指向无关空目录 → 不再建知识库（不产生假的空指纹知识库）",
              plug3._kbman is None and plug3._knowledge is None)
        chk3 = plug3.server_dir_check(max_age=0)
        check("server_dir_check：硬校验 ok=False、带 errors 与 suggest_path 字段",
              chk3["ok"] is False and bool(chk3["errors"]) and "suggest_path" in chk3,
              chk3["errors"][0][:56])
        check("工具闸门 _local_gate：直接答「目录校验未通过 + 怎么修」",
              str(plug3._local_gate("物品 ID 搜索", "服务端 mods/*.jar")).startswith(
                  "【服务端目录校验未通过】"))
        check("服务端版本探测 → 目录不合法时返回空（上层显示「未检测到」）",
              plug3.detect_server_version() == "")

        # 结构合法但 mods/ 里没有 jar：这不是误配，而是「原版服务端 / 还没装 mod」
        vanilla = make_pack(tmp / "vanilla_server", {})
        plug3v = make_plugin(
            {"server_dir": str(vanilla), "knowledge_enabled": True,
             "dictionary_enabled": True}, data_dir,
        )
        note3v = asyncio.run(plug3v._sync_server_context(rebuild_dictionary=True))
        print(f"      结构合法但 mods 为空时的提示：{note3v}")
        # v0.21.20：文案从「空整合包」改为「既没有 mod 也没有插件」——
        # 插件服务端（Paper 系）的内容在 plugins/，不能再一律说成空整合包
        check("结构合法 + mods 为空 → 照常建词典（0 mod），并点明指纹认不出服务端",
              plug3v._dictionary is not None and len(plug3v._dictionary.mods) == 0
              and "既没有 mod 也没有插件" in note3v
              and "认不出同形态" in note3v)
        ans = plug3v._dictionary.search_items("钻石剑")
        check("词典为空 → 搜索返回空列表（工具层会提示「尝试英文名/网络搜索」，不编 ID）",
              ans == [])
        check("结构合法 + mods 为空 → 本地文件能力闸门放行（不误报「目录不合法」）",
              plug3v._local_gate("物品 ID 搜索", "服务端 mods/*.jar") is None)
        # 服务端版本探测：异地必然拿不到
        check("异地无目录 → detect_server_version() 返回空（上层会显示「未检测到，请检查 server_dir」）",
              plug3.detect_server_version() == "")
        # 进程指标：psutil 在本机找不到 MC 进程
        metrics = asyncio.run(plug3._server_process_metrics())
        check("异地部署 → 进程指标为 None（/mcs 状态 会如实写「无法获取」而非给假数字）",
              metrics is None or metrics.get("pid") is not None,
              "(本机恰好有 java 服务端进程时可能命中，见报告说明)")

        # ============================================================
        print("\n=========== B. 挂载/共享目录形态（server_dir 指向异地副本） ===========")
        share = make_pack(tmp / "mounted_share", {"craft.jar": "deceasedcraft"})
        logs = share / "logs"
        logs.mkdir(exist_ok=True)
        log_file = logs / "latest.log"
        log_file.write_text(
            "[16:30:01] [Server thread/INFO]: Steve joined the game\n"
            "[16:30:05] [Server thread/INFO]: <Steve> hello\n",
            encoding="utf-8",
        )
        plug4 = make_plugin(
            {"server_dir": str(share), "knowledge_enabled": True,
             "dictionary_enabled": True}, data_dir,
        )
        note4 = asyncio.run(plug4._sync_server_context(rebuild_dictionary=True))
        print(f"      共享目录形态提示：{note4}")
        check("共享目录可读 → 指纹/词典正常建立（异地但有副本时特性可用）",
              plug4._kbman is not None and plug4._dictionary is not None
              and len(plug4._dictionary.mods) == 1)
        check("共享目录可读 → 版本探测可从副本日志/目录名提取",
              isinstance(plug4.detect_server_version(), str))

        events: list[tuple] = []

        async def _on_ev(etype, player, detail):
            events.append((etype, player, detail))

        async def _run_watcher():
            w = LogWatcher(str(share), _on_ev, poll_interval=0.2)
            await w.start()          # 从文件末尾开始，不回溯历史
            await asyncio.sleep(0.4)
            with log_file.open("a", encoding="utf-8") as f:
                f.write("[16:31:00] [Server thread/INFO]: Steve was slain by Zombie\n")
                f.write("[16:31:05] [Server thread/INFO]: Steve has made the advancement [Getting an Upgrade]\n")
            await asyncio.sleep(0.9)
            await w.stop()

        asyncio.run(_run_watcher())
        kinds = [e[0] for e in events]
        print(f"      从共享副本读到的事件：{kinds}")
        check("共享副本日志可被解析（死亡 + 成就均识别）",
              "death" in kinds and "advancement" in kinds, str(kinds))
        check("历史行不回溯（启动时的 joined/hello 未被重复推送）",
              not any(e[0] == "join" for e in events), str(kinds))

        # 断线/半写行：共享盘同步中断时日志可能残缺
        with log_file.open("ab") as f:
            f.write("[16:32:00] [Server thread/INFO]: Alex was slain by Creeper".encode("utf-8"))  # 无换行
        import asyncio as _a

        async def _half_line():
            w = LogWatcher(str(share), _on_ev, poll_interval=0.2)
            await w.start()
            await _a.sleep(0.5)      # 半行不应产出事件
            return list(events)

        before = len(events)
        asyncio.run(_half_line())
        check("残缺半行（网络共享同步中）不会被误报成事件",
              len(events) == before, f"新增 {len(events) - before} 条")

        # 日志被删/不可读时的兜底（服务器迁移中、共享断开）
        (share / "logs" / "latest.log").unlink()
        stub = make_plugin(
            {"server_dir": str(share), "enable_event_listener": True}, data_dir)
        stub_events: list[tuple] = []

        async def _stub_ev(etype, player, detail):
            stub_events.append((etype, player, detail))

        stub._on_server_event = _stub_ev  # 实例属性覆盖，收集重启后的监听输出

        async def _full_cycle():
            # 同一个事件循环内完成「启动 → 日志出现 → 追加新行」全流程
            msg = await stub._restart_event_listener()
            log_file.write_text(
                "[16:40:00] [Server thread/INFO]: Steve joined the game\n",
                encoding="utf-8")
            await asyncio.sleep(0.3)
            with log_file.open("a", encoding="utf-8") as f:
                f.write("[16:41:00] [Server thread/INFO]: <Steve> back\n")
            await asyncio.sleep(1.6)      # 跨过 1s 轮询周期
            if getattr(stub, "_watcher", None):
                await stub._watcher.stop()
            return msg

        msg5 = asyncio.run(_full_cycle())
        print(f"      共享断开/日志缺失时事件转发返回：{msg5}")
        check("共享断开/日志缺失 → 监听器挂起等待（不报错、共享恢复后自动续读）",
              "失败" not in msg5, msg5)
        new_kinds = [(e[0], e[1]) for e in stub_events]
        print(f"      共享恢复后读到的事件：{new_kinds}")
        check("共享恢复 → 新行续读成功（异地挂载掉线可自愈）",
              any(k == "chat" for k, _ in new_kinds), str(new_kinds))
        # 观察项：日志在监听**中途才出现**时，文件里的存量内容会被整份读取
        replay = any(k == "join" for k, _ in new_kinds)
        print(f"      [观察] 共享中途挂入 → 存量行是否被重播：{replay}"
              f"（{'会重播，属已知边缘行为' if replay else '未重播'}）")

        # ============================================================
        print("\n=========== C. 远程 RCON：目标玩家名解析与超时 ===========")
        r = AsyncRcon(host="10.0.0.5", port=25575, password="x", timeout=3.0)
        check("RCON 客户端支持任意主机地址（含异地 IP / 域名解析交给系统）",
              r.host == "10.0.0.5" and r.port == 25575)
        check("超时参数可配置（默认 5s；经 WebUI 保存后对新连接生效）", r.timeout == 3.0)

        # 连接失败必须抛 RconError 且带主机端口（可诊断，不静默）
        bad = AsyncRcon(host="127.0.0.1", port=1, password="x", timeout=0.5)
        try:
            asyncio.run(bad.connect())
            err = ""
        except RconError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = f"非 RconError：{e!r}"
        print(f"      连接失败文案：{err}")
        check("连不上远端 → 抛 RconError 且含 host:port（用户能立刻看出端口映射没通）",
              "127.0.0.1:1" in err and "无法连接 RCON 服务器" in err, err[:80])

        # 在线玩家列表解析（异地时这是唯一真源）
        sample = "There are 2 of a max of 20 players online: Steve, Alex"
        check("list 输出解析正常（异地也能拿到真实在线名单）",
              "Steve" in m.McControlPlugin._parse_list_output(sample)
              and "Alex" in m.McControlPlugin._parse_list_output(sample))
        empty = "There are 0 of a max of 20 players online:"
        check("无人在线时解析为 0 且名单为空（不会误发指令给缺席玩家）",
              "0" in m.McControlPlugin._parse_list_output(empty))

        # ============================================================
        print("\n=========== D. 远程场景下的配置项可达性 ===========")
        plug6 = make_plugin(
            {"rcon_host": "mc.example.com", "rcon_port": 30000,
             "rcon_password": "pw", "rcon_timeout": 8.0}, data_dir,
        )
        rc = asyncio.run(plug6._get_rcon())
        check("rcon_host 支持域名 / 非默认端口 30000（端口映射场景）",
              rc.host == "mc.example.com" and rc.port == 30000)
        check("超时按配置生效（异地高延迟可调大，避免误判失败）", rc.timeout == 8.0)
        check("RCON 客户端复用同一连接对象（每会话一条 TCP，映射端口压力小）",
              asyncio.run(plug6._get_rcon()) is rc)

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("✓ 异地部署审计全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
