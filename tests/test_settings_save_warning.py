# -*- coding: utf-8 -*-
"""异地 RCON 未填版本 → 保存后软提醒（A 方案）· 后端判定。

现象：异地 RCON 模式读不到服务端文件，版本**必须手填**；但留空也能正常保存，
于是「必须手填」读起来像保存的前置条件，实为功能后果 —— 用户点「保存」看到
「✓ 已保存」就以为功课做完了，等到真用附魔 / NBT 才被拒绝。

方案：保存成功后回包里带结构化 `warnings`（前端吸顶条渲染），**不阻断保存**。

覆盖点（对应方案 §四 的 1~4）
============================
1. 异地 + 版本空 + 语法世代 auto → `warnings` 非空且含关键短语；保存本身仍 `ok: true`；
2. **本地模式 + 版本空 → `warnings` 为空**（本地留空是「自动探测」的正常用法，不误报）；
3. 异地 + 版本空 + 手填语法世代 → **逃生出口不被误伤**：不按「会被拒绝」说，
   只降级成「建议补上版本」；
4. 异地 + 版本已填 → `warnings` 为空。

铁律（v0.23.5 立）：软提醒**绝不能**把「已保存」渲染成失败 —— 所以上列每种情况的
`ok` 都必须是 `True`，本用例逐条钉死。

运行：python tests/test_settings_save_warning.py  （工作目录 = 插件根目录）
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()

from astrbot_plugin_Scintilla_MC_Server_Control.core import version_caps as vc  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402

import test_remote_rcon_mode as harness  # noqa: E402  （复用其 make_plugin / make_server / FakeRequest / body）

FAIL: list[str] = []
N = [0]                       # 真计数器：PASS / FAIL 都算一项


def check(desc: str, ok: bool, extra: str = "") -> None:
    N[0] += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}" + (f"  <- {extra}" if extra and not ok else ""))
    if not ok:
        FAIL.append(desc)


def make_plug(cfg: dict):
    """夹具要求 (cfg, data_dir)：每次给一个干净临时目录，互不串味。"""
    return harness.make_plugin(cfg, Path(tempfile.mkdtemp(prefix="data_")))


def save(plug, payload: dict) -> dict:
    """走真处理器 save_settings，拿到回包（body 由既有夹具解析）。"""
    api = wa.McControlWebApi(plug)
    wa.request = harness.FakeRequest({"settings": payload})
    return harness.body(asyncio.run(api.save_settings()))


def codes(res: dict) -> list:
    return [w.get("code") for w in (res.get("warnings") or [])]


def texts(res: dict) -> str:
    return " ".join(str(w.get("text") or "") for w in (res.get("warnings") or []))


def run() -> int:
    # ---------- 1. 异地 + 版本空 + auto ----------
    p1 = make_plug({"remote_rcon_mode": True, "server_dir": ""})
    r1 = save(p1, {"remote_rcon_mode": True, "server_dir": "",
                   "server_version_override": ""})
    check("① 保存仍成功（ok=True，不阻断）", r1.get("ok") is True, str(r1)[:160])
    check("① 回包带 warnings", bool(r1.get("warnings")), str(r1.get("warnings"))[:160])
    check("① code = remote_version_missing", "remote_version_missing" in codes(r1), str(codes(r1)))
    check("① 文案点明后果（会被拒绝）", "会被拒绝" in texts(r1), texts(r1)[:120])
    check("① 带「去填写」动作标记", all(w.get("action") == "fill_server_version"
                                    for w in (r1.get("warnings") or [])), str(codes(r1)))

    # ---------- 2. 本地模式 + 版本空 → 不误报 ----------
    local_dir = Path(tempfile.mkdtemp(prefix="srv_"))
    harness.make_server(local_dir)
    p2 = make_plug({"remote_rcon_mode": False, "server_dir": ""})
    r2 = save(p2, {"remote_rcon_mode": False, "server_dir": str(local_dir),
                   "server_version_override": ""})
    check("② 本地模式保存成功", r2.get("ok") is True, str(r2)[:160])
    check("② 本地模式 + 版本空 = 不提醒（warnings 为空）", not r2.get("warnings"),
          str(r2.get("warnings"))[:160])

    # ---------- 3. 异地 + 版本空 + 手填语法世代 → 逃生出口不误伤 ----------
    legacy = getattr(vc, "ITEM_SYNTAX_LEGACY", "legacy_nbt")
    p3 = make_plug({"remote_rcon_mode": True, "server_dir": ""})
    r3 = save(p3, {"remote_rcon_mode": True, "server_dir": "",
                   "server_version_override": "", "item_syntax_override": legacy})
    check("③ 保存成功（手填世代是合法逃生出口）", r3.get("ok") is True, str(r3)[:160])
    check("③ code = remote_version_missing_with_syntax",
          "remote_version_missing_with_syntax" in codes(r3), str(codes(r3)))
    check("③ 降级口径：不按「会被拒绝」说", "会被拒绝" not in texts(r3), texts(r3)[:120])
    check("③ 降级口径：只说「建议补上版本」", "建议补上版本" in texts(r3), texts(r3)[:120])

    # ---------- 4. 异地 + 版本已填 → 不提醒 ----------
    p4 = make_plug({"remote_rcon_mode": True, "server_dir": ""})
    r4 = save(p4, {"remote_rcon_mode": True, "server_dir": "",
                   "server_version_override": "1.7.10"})
    check("④ 异地 + 版本已填保存成功", r4.get("ok") is True, str(r4)[:160])
    check("④ 版本已填 = 不提醒（warnings 为空）", not r4.get("warnings"),
          str(r4.get("warnings"))[:160])

    # ---------- 5. 提醒不改变保存语义：ignored / config_saved 仍在 ----------
    check("⑤ 回包仍带 ignored 与 config_saved（同通道，不另立门户）",
          "ignored" in r1 and "config_saved" in r1, str(sorted(r1.keys()))[:160])

    print()
    if FAIL:
        print("失败 %d 项：%s" % (len(FAIL), " / ".join(FAIL)))
        return 1
    print("通过 %d 项，失败 0 项" % (N[0] - len(FAIL)))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
