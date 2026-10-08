# -*- coding: utf-8 -*-
"""v0.24.2 回归：持久化与目录判定（GPT 全面复核 F11 + F12 + F13 + F14）。

F11｜纠错契约字段错位 → 纠错整段白跑
  · 提示词给的 schema 是 ``new_content``，消费方读的是 ``content`` —— 「修正正文永远
    为空」，写库与回传全被静默跳过，任务照样报成功，错误模板下次继续进流水线。
  · 修法：两个键都认（迁移期兼容）；「有 topic 没正文」留痕；把**完整修正正文**一并
    交回实现器（只给 topic 名等于让实现器再猜一遍）；消费逻辑抽成
    ``_apply_corrections()`` 以便直接按判据验（而不是靠源文本断言）。

F12｜绑定落盘失败仍报成功
  · ``_save()`` 把异常吞成 ``pass``，``bind()`` 无条件回「已绑定」—— 内存改了、磁盘没改，
    重启后用户看到旧绑定，而当时回执说成功了。
  · 修法：``_save()`` 返回 ``(ok, err)``；失败**回滚内存**并如实报错（bind / unbind 都是）。

F13｜多预设共用一个落盘部件名
  · 所有预设都记在 ``preset_file`` 一个键上：预设 A 写失败、B 写成功就把 A 的红覆盖成
    「一切正常」，而 A 那个文件仍然带回退版知识。
  · 修法：``_preset_part(pid)`` 按**文件**记账（含指纹回写）；transfer 的目标 / 源两笔
    各记各的；某个文件的红只有它自己下次写成功才会清掉。

F14｜客户端 logs/libraries 被当成服务端强特征
  · ``.minecraft`` 里天然有 logs/、libraries/ —— 一个只有 logs/ saves/ options.txt 的
    客户端目录能通过「是服务端根目录」硬校验。
  · 修法：强特征只留服务端专属证据（server.properties / eula.txt / 启动脚本 / server.jar）；
    客户端标记 + 无强特征 → 拒。干净原版（第一次启动前）仍要放行。

跑法：<python> tests\test_v0242_persistence.py
"""
from __future__ import annotations

import importlib
import logging
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

FAIL: list[str] = []
SKIP: list[str] = []
PASS_N: list[str] = []
add_sys_paths()


def check(desc, ok, detail=""):
    if ok:
        PASS_N.append(desc)
    else:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {detail}" if detail and not ok else ""))


def skip(desc):
    SKIP.append(desc)
    print(f"[SKIP] {desc}")


try:
    WF = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.workflow")
    PB = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.player_bindings")
    KB = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base")
    SD = importlib.import_module("astrbot_plugin_Scintilla_MC_Server_Control.core.server_dir_check")
    IMP_OK, IMP_ERR = True, ""
except Exception as e:  # noqa: BLE001
    IMP_OK, IMP_ERR = False, f"{type(e).__name__}: {e}"


def cls_of(mod, attr):
    for v in vars(mod).values():
        if isinstance(v, type) and hasattr(v, attr):
            return v
    return None


# ===================== F11 =====================
def f11_cases():
    print("---- 一、F11：纠错契约（new_content 必须被消费）----")
    MW = cls_of(WF, "_apply_corrections")
    if MW is None:
        skip("找不到 _apply_corrections")
        return

    class KbStub:
        def __init__(self, ok=True):
            self.ok = ok
            self.saved: list = []
            self.last_save_ok = ok
            self.last_save_error = "" if ok else "写盘失败（替身）"

        def save_entry(self, topic, content, source=""):
            self.saved.append((topic, content, source))

    wf = MW.__new__(MW)
    wf.logger = logging.getLogger("test.v0242.f11")

    kb = KbStub()
    fixed = wf._apply_corrections({"corrections": [{"topic": "t1", "new_content": "正文1"}]}, kb)
    check("★new_content 被消费：写库 + 回传正文",
          kb.saved == [("t1", "正文1", "agent_corrector")] and fixed == [("t1", "正文1")],
          f"{kb.saved}｜{fixed}")

    kb2 = KbStub()
    fixed2 = wf._apply_corrections({"corrections": [{"topic": "t2", "content": "正文2"}]}, kb2)
    check("旧字段 content 仍兼容（迁移期）", fixed2 == [("t2", "正文2")], str(fixed2))

    kb3 = KbStub()
    fixed3 = wf._apply_corrections({"corrections": [{"topic": "t3"}]}, kb3)
    check("★只有 topic 没有正文 → 不算已修正，且不写库", fixed3 == [] and kb3.saved == [], f"{fixed3}｜{kb3.saved}")

    kb4 = KbStub(ok=False)
    fixed4 = wf._apply_corrections({"corrections": [{"topic": "t4", "new_content": "正文4"}]}, kb4)
    check("★没落盘的纠正不算「已修正」（重启后错误条目会复活）", fixed4 == [], str(fixed4))


# ===================== F12 =====================
def f12_cases():
    print("\n---- 二、F12：绑定落盘失败不得报成功 ----")
    B = cls_of(PB, "bind")
    if B is None:
        skip("找不到 PlayerBindings.bind")
        return
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "blocker"
        blocker.write_text("我是文件，不是目录", encoding="utf-8")
        bad = blocker / "bindings.json"          # 父路径是文件 → 写盘必失败

        def make():
            o = B.__new__(B)
            o._path = Path(bad)
            o._data = {}
            o._lock = threading.RLock()
            return o

        b = make()
        ok, msg = b.bind("u1", "Steve")
        check("★写盘失败 → bind 返回 False（不谎报成功）", ok is False, msg)
        check("★写盘失败 → 内存已回滚（重启前后一致）", b.get("u1") == "", b.get("u1"))
        check("错误里说明「未生效 + 保持原样」", "未生效" in msg and "写盘失败" in msg, msg)

        b2 = make()
        b2._data = {"u2": {"player": "Alex", "ts": 1.0}}
        ok2, msg2 = b2.unbind("u2")
        check("★解绑写盘失败 → 返回 False 且绑定仍在", ok2 is False and b2.get("u2") == "Alex", f"{ok2}｜{msg2}")


# ===================== F13 =====================
def f13_cases():
    print("\n---- 三、F13：落盘健康度按文件记账 ----")
    M = getattr(KB, "KnowledgePresetManager", None)
    if M is None:
        skip("找不到 KnowledgePresetManager")
        return
    with tempfile.TemporaryDirectory() as td:
        m = M(td, "fp-test", "")
        a = m.create(name="A")
        b = m.create(name="B")
        pid_a, pid_b = a["id"], b["id"]
        check("两个预设的记账键互不相同",
              m._preset_part(pid_a) != m._preset_part(pid_b),
              f"{m._preset_part(pid_a)}｜{m._preset_part(pid_b)}")

        # 把 A 的预设文件位置换成一个目录 → A 的写入必失败
        pa = Path(m.preset_path(pid_a))
        pa.unlink()
        pa.mkdir()
        m.bind(pid_a, "fp-a")                     # 指纹回写 → 写 A 失败
        check("A 写失败后账本是红的", m.last_save_ok is False, m.last_save_error)
        check("★错误里能看出是**哪个文件**（不再是笼统一句）",
              pid_a in m.last_save_error, m.last_save_error)

        # 紧接着 B 写成功 —— 不许把 A 的红覆盖掉
        c = m.create(name="C")
        health = m.save_health()
        check("★B/C 写成功**不会**覆盖 A 的失败（按文件记账的核心）",
              m.last_save_ok is False and pid_a in m.last_save_error,
              f"last_save_ok={m.last_save_ok} err={m.last_save_error}")
        check("save_health 聚合值同样为 False", bool(health.get("save_ok")) is False, str(health))
        check("B/C 自己的账是清的", m._save_parts.get(m._preset_part(c["id"]), (True,))[0] is True,
              str(m._save_parts.get(m._preset_part(c["id"]))))


# ===================== F14 =====================
def f14_cases():
    print("\n---- 四、F14：客户端目录不得被当成服务端 ----")
    chk = getattr(SD, "inspect_server_dir", None)
    if chk is None:
        skip("找不到 inspect_server_dir")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        client = root / "dot_minecraft"
        (client / "logs").mkdir(parents=True)
        (client / "saves").mkdir()
        (client / "logs" / "latest.log").write_text("x", encoding="utf-8")
        (client / "options.txt").write_text("x", encoding="utf-8")
        (client / "servers.dat").write_text("x", encoding="utf-8")
        r = chk(str(client))
        check("★客户端 .minecraft（只有 logs/saves/options.txt）被拒绝", r.get("ok") is False, str(r)[:200])
        check("错误里点明「像客户端目录」",
              any("客户端" in e for e in (r.get("errors") or [])), str(r.get("errors")))

        vanilla = root / "clean_vanilla"                 # 第一次启动前的干净原版
        vanilla.mkdir()
        (vanilla / "server.jar").write_bytes(b"PK\x03\x04fake")
        (vanilla / "eula.txt").write_text("eula=false", encoding="utf-8")
        r2 = chk(str(vanilla))
        check("★干净原版（第一次启动前：server.jar + eula.txt）仍放行", r2.get("ok") is True, str(r2)[:200])

        launched = root / "launched"
        (launched / "logs").mkdir(parents=True)
        (launched / "config").mkdir()
        (launched / "server.properties").write_text("x", encoding="utf-8")
        (launched / "eula.txt").write_text("eula=true", encoding="utf-8")
        r3 = chk(str(launched))
        check("启动过的服务端（logs + server.properties）放行", r3.get("ok") is True, str(r3)[:200])


def main() -> int:
    if not IMP_OK:
        skip(f"取不到插件模块：{IMP_ERR}")
    else:
        print("=" * 78)
        print("v0.24.2 回归 · 持久化与目录判定（F11 / F12 / F13 / F14）")
        print("=" * 78)
        f11_cases()
        f12_cases()
        f13_cases()
        f14_cases()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS_N), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
