# -*- coding: utf-8 -*-
"""v0.24.8 回归（2026-10-22 真机 + 主人实测）：词典拿不到时，**按真实原因**给话。

怎么发现的
==========
主人在 WebUI 把「知识库」那一栏整段停用（`knowledge_enabled` / `dictionary_enabled` /
三个 mc_*_knowledge 开关全关），然后皮莉卡调 `mc_search_item`，拿到的是：

    物品词典未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。

—— 可 `server_dir` 明明填着（1.13.2），**归因错了**。用户会跑去改一个没问题的配置项，
真正该看的地方（词典开关）反而没人提。

判据
====
`_dictionary_unavailable_reason()` 必须按**真实原因**分岔，且四种原因互不串台：
  ① 词典开关被关 → 说「已在配置中停用」并指出在哪；
  ② 异地 RCON 模式 → 说「读不到服务端目录」；
  ③ server_dir 没填 / 不是目录 → 才说「请先填写 server_dir」；
  ④ 其余（正在重建 / 构建失败）→ 说「尚未就绪，稍后重试」。
四个 `mc_*` 工具（list_mods / search_item / get_recipes / rescan_dictionary）
必须都走这一层 —— 不许再各自抄一份文案。

运行：<AstrBot python> tests\\test_v0248_dictionary_reason.py
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
import astrbot_plugin_Scintilla_MC_Server_Control.main as M  # noqa: E402

PASSED = [0]
FAILED: list[str] = []


def check(desc: str, ok: bool, extra: str = "") -> None:
    if ok:
        PASSED[0] += 1
        print(f"  [ok] {desc}")
    else:
        FAILED.append(desc)
        print(f"  [FAIL] {desc}" + (f"  <- {extra}" if extra else ""))


def _plug(**cfg) -> "M.McControlPlugin":
    p = M.McControlPlugin.__new__(M.McControlPlugin)
    p.config = dict(cfg)
    p._cfg_index = {}          # 真实对象在 __init__ 里建（本用例绕过 __init__）
    import logging
    p.logger = logging.getLogger("test-dict-reason")
    return p


def part_one() -> None:
    print("---- 一、四种原因各说各的话（互不串台）----")
    off = _plug(dictionary_enabled=False, server_dir=str(PLUGIN_DIR))
    msg = off._dictionary_unavailable_reason()
    check("★词典开关被关 → 说「已在配置中停用」", "停用" in msg, msg)
    check("★并且指出该去哪看（WebUI → 插件设置 → 知识库）",
          "WebUI" in msg and "知识库" in msg, msg)
    check("★不再甩锅 server_dir", "填写 server_dir" not in msg, msg)

    remote = _plug(dictionary_enabled=True, remote_rcon_mode=True,
                   server_dir=str(PLUGIN_DIR))
    msg_r = remote._dictionary_unavailable_reason()
    check("异地 RCON 模式 → 说「读不到服务端目录」", "异地" in msg_r, msg_r)

    missing = _plug(dictionary_enabled=True, server_dir="")
    msg_m = missing._dictionary_unavailable_reason()
    check("server_dir 没填 → 才说「请先填写 server_dir」", "填写 server_dir" in msg_m, msg_m)

    notdir = _plug(dictionary_enabled=True, server_dir=str(PLUGIN_DIR / "不存在这个目录"))
    check("server_dir 不是目录 → 同样归到「未配置」一档",
          "填写 server_dir" in notdir._dictionary_unavailable_reason(),
          notdir._dictionary_unavailable_reason())

    ready = _plug(dictionary_enabled=True, server_dir=str(PLUGIN_DIR))
    msg_ready = ready._dictionary_unavailable_reason()
    check("配置都对但词典还没就绪 → 说「尚未就绪，稍后重试」",
          "尚未就绪" in msg_ready and "填写 server_dir" not in msg_ready, msg_ready)


def part_two() -> None:
    print("\n---- 二、四个工具都走同一层（不许各抄一份文案）----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    old = "物品词典未初始化：请先在插件配置中填写 server_dir"
    check("★源码里只剩一处这句话（helper 内部那一次）", src.count(old) == 1,
          f"出现 {src.count(old)} 次")
    check("★helper 被四个工具共用", src.count("self._dictionary_unavailable_reason()") >= 4,
          f"调用 {src.count('self._dictionary_unavailable_reason()')} 次")
    check("helper 自身不会递归（正文里没有自调用）",
          "return self._dictionary_unavailable_reason()\n        return" not in src)


def main() -> int:
    print("=" * 78)
    print("v0.24.8 回归 · 词典不可用时按真实原因给话（主人实测的归因错误）")
    print("=" * 78)
    part_one()
    part_two()
    print("\n================ 汇总 ================")
    print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAILED)))
    if FAILED:
        print("失败项：" + " / ".join(FAILED))
        return 1
    print("全部通过 ✅ （关掉开关就说关掉开关，别再让人去改没问题的 server_dir）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
