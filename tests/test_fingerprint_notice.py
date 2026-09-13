"""v0.21.8 回归：指纹不匹配全屏弹窗 —— 「检测到就弹 · 同一轮只弹一次」。

一轮 = (激活预设 × 服务端[指纹+目录])：
  · 不匹配 → show=True（该弹）；
  · 弹窗显示过（mark_notice_shown）→ show=False，刷新/切页都不再重复弹；
  · 服务端下次变动（指纹变了，或指纹撞车但换了服务端目录）→ 轮次翻新 → show 重新 True；
  · 「本轮不再提醒」接口（suppress）保留给旧记账 / 知识库页「重新开启」用，
    但 WebUI 弹窗里的勾选框已在 v0.21.10 撤掉（与「知道了」语义重复）。
  · v0.21.9：空 mods 目录（指纹恒为 d41d8cd98f00）要带 server_fp_weak 标记，
    且此时「换服务端」靠目录识别 —— 否则两个没装 mod 的服务端永远不再弹。

不需要 AstrBot 运行时，直接对 KnowledgePresetManager 做黑盒。运行：
  python tests\\test_fingerprint_notice.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    EMPTY_SERVER_FP,
    KnowledgePresetManager,
)

FP_A = "e38d99c65853"     # 某个真实服务端指纹（示例）
FP_B = "dead1234beef"     # 另一个整合包
FP_C = "cafe0987face"     # 再换一个

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def fresh(data_dir: Path, server_fp: str, server_dir: str = "") -> KnowledgePresetManager:
    return KnowledgePresetManager(str(data_dir), server_fp, server_dir)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "kb"

        print("[1] 未绑定指纹 → 不算不匹配，不弹")
        man = fresh(data_dir, FP_A)
        pid = man.reg["active"]
        check("新预设默认未绑定", not man.get(pid)["fingerprint"])
        check("未绑定不弹窗", man.notice()["show"] is False, str(man.notice()))

        print("[2] 绑定到别的整合包指纹 → 检测到不匹配就弹")
        man.bind(pid, FP_B)
        n = man.notice()
        check("不匹配 → show=True", n["show"] is True, str(n))
        check("mismatch 常驻标记为 True", n["mismatch"] is True)
        check("轮次 key = 预设|服务端指纹", n["key"] == f"{pid}|{FP_A}", n["key"])

        print("[3] 弹窗显示过 → 同一轮只弹一次（不要求点按钮）")
        man.mark_notice_shown()
        check("显示后本轮不再弹", man.notice()["show"] is False, str(man.notice()))
        check("popped 记账", man.notice()["popped"] is True)
        check("mismatch 仍然为 True（概览常驻标记不能跟着消失）", man.notice()["mismatch"] is True)

        print("[4] 重载（等价于刷新页面 / 重启插件）→ 仍然不弹")
        man2 = fresh(data_dir, FP_A)
        check("持久化生效：重载后不弹", man2.notice()["show"] is False, str(man2.notice()))
        check("绑定关系没丢", man2.get(pid)["fingerprint"] == FP_B)

        print("[5] 服务端指纹下次变动 → 重新拥有一次弹窗机会")
        man2.set_server_id(FP_C)
        n = man2.notice()
        check("指纹变动 → show=True", n["show"] is True, str(n))
        check("轮次 key 已更新", n["key"] == f"{pid}|{FP_C}", n["key"])
        man2.mark_notice_shown()
        check("新轮次弹过之后又不弹了", man2.notice()["show"] is False)

        print("[6] 「本轮不再提醒」只对本轮生效")
        man2.ack_notice(suppress=True)
        check("本轮被静音", man2.notice()["show"] is False)
        check("suppressed 标记为 True", man2.notice()["suppressed"] is True)
        man2.set_server_id(FP_A)
        n = man2.notice()
        check("指纹再变动 → 提醒自动恢复", n["show"] is True, str(n))
        check("suppressed 已不适用于新轮次", n["suppressed"] is False)

        print("[7] 知识库页「重新开启」→ 本轮立刻再弹一次")
        man2.ack_notice(suppress=True)
        check("先静音", man2.notice()["show"] is False)
        man2.set_suppress(False)
        check("重新开启 → show=True", man2.notice()["show"] is True, str(man2.notice()))
        man2.mark_notice_shown()   # 弹窗又显示了一次 → 记回「本轮已弹」

        print("[8] 切到另一个预设 → 各自只弹一次，来回切也不重复")
        other = man2.create("另一个整合包的知识库")
        man2.switch(other["id"])
        check("新预设未绑定 → 不弹", man2.notice()["show"] is False)
        man2.bind(other["id"], FP_C)
        check("新预设绑定别的指纹 → 弹", man2.notice()["show"] is True)
        man2.mark_notice_shown()
        check("新预设本轮不重复弹", man2.notice()["show"] is False)
        man2.switch(pid)
        check("切回旧预设：该轮也已弹过 → 不重复弹", man2.notice()["show"] is False)

        print("[9] 旧数据迁移：永久 suppressed / ack 字段不炸、语义变成「本轮」")
        legacy = Path(tmp) / "legacy"
        legacy.mkdir()
        (legacy / "kb_presets.json").write_text(json.dumps({
            "active": "p_old",
            "presets": [{"id": "p_old", "name": "旧库", "fingerprint": FP_B,
                         "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00"}],
            "notice": {"suppressed": True, "ack": f"p_old|{FP_A}"},
        }, ensure_ascii=False), encoding="utf-8")
        (legacy / "kb_preset_p_old.json").write_text(json.dumps(
            {"preset_id": "p_old", "fingerprint": FP_B, "entries": {}, "deleted": []},
            ensure_ascii=False), encoding="utf-8")
        old = fresh(legacy, FP_A)
        check("旧永久关闭 → 迁移为本轮静音（保留主人当前选择）", old.notice()["show"] is False)
        check("迁移保留旧预设", old.notice()["preset_id"] == "p_old")
        old.set_server_id(FP_C)
        check("旧数据的静音同样只对本轮有效 → 指纹变动后重新提示", old.notice()["show"] is True)

        print("[10] v0.21.9：指纹撞车（两个服务端都没装 mod）也算「换了服务端」")
        same = fresh(Path(tmp) / "same", EMPTY_SERVER_FP, r"D:\srv\mcs")
        pid3 = same.reg["active"]
        same.bind(pid3, FP_A)
        n = same.notice()
        check("不匹配 + 空指纹 → 该弹", n["show"] is True, str(n))
        check("空指纹标记为 weak（弹窗要警告 mods 是空的）", n["server_fp_weak"] is True)
        check("notice 带上服务端目录（排障用）", n["server_dir"] == r"D:\srv\mcs", str(n["server_dir"]))
        check("正常指纹不带 weak 标记", fresh(Path(tmp) / "nz", FP_A).notice()["server_fp_weak"] is False)
        same.mark_notice_shown()
        check("弹过之后本轮不弹", same.notice()["show"] is False)
        same.set_server_id(EMPTY_SERVER_FP)     # 指纹、目录都没变
        check("同指纹同目录：保持「本轮已弹」记账", same.notice()["show"] is False)
        same.set_server_id(EMPTY_SERVER_FP, server_dir=r"D:\srv\mcs2")   # 换服务端（指纹撞车）
        check("指纹相同但换了目录 → 重新弹一次", same.notice()["show"] is True, str(same.notice()))
        same.ack_notice(suppress=True)
        check("本轮静音", same.notice()["show"] is False)
        same.set_server_id(EMPTY_SERVER_FP, server_dir=r"D:\srv\mcs")
        check("换回旧目录也是新一轮 → 提醒恢复", same.notice()["show"] is True, str(same.notice()))
        same.mark_notice_shown()
        reopened = fresh(Path(tmp) / "same", EMPTY_SERVER_FP, r"D:\srv\mcs")
        check("记账已落盘：重开同一服务端不重复弹", reopened.notice()["show"] is False)
        reopened.set_server_id(EMPTY_SERVER_FP, server_dir=r"D:\srv\mcs2")
        check("换个目录重开（模拟重载插件后换服）→ 照弹", reopened.notice()["show"] is True)

    if failed:
        print("\n✗ 失败项:")
        for f in failed:
            print("  -", f)
        return 1
    print("\n✓ 全部通过（指纹不匹配弹窗 10 组断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
