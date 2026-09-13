"""v0.21.11 回归：知识条目详情 / 编辑的后端语义。

背景：WebUI 卡片里内容被 72px 硬截断，看不到全文 → 新增「查看详情」全屏子窗口，
在里面看全文并就地改 topic/content。后端要顶住三件事：

  1) 读取：list_entries / get_entry 给的是**完整 content**（截断是前端的事）；
  2) 编辑（manual=True）：主人手动保存 → 保住原 status（不把「已验证」降级成「未验证」）、
     保住 created_at / enabled / mod / kind，只刷新 content 与 updated_at；
  3) 改名（rename_from）：旧主题整条搬走、不留残影，旧主题进墓碑防旧文件复活，
     沿用 created_at/mod/kind；目标主题撞已有条目 → rename_exists 报 True（前端拒绝覆盖）。
     另外 auto_apply 关闭时的「转 pending」老规矩不能被这次改动破掉（manual=False 时照旧）。

不需要 AstrBot 运行时。运行：
  python tests\\test_kb_entry_edit.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 ASTRBOT_APP_DIR 指路）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import ModKnowledgeBase  # noqa: E402

FP = "e38d99c65853"
LONG = "【满改方案】\n" + "\n".join(f"第{i}行：配件槽位与 NBT 键名示例（Scope/Under_Barrel/Muzzle）" for i in range(1, 41))

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def fresh(data_dir: Path, *, auto_apply: bool = True) -> ModKnowledgeBase:
    kb = ModKnowledgeBase(str(data_dir), server_id=FP, preset_id="p1", preset_name="测试库",
                          fingerprint=FP)
    kb.state.auto_apply = auto_apply
    return kb


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "kb"

        print("[1] 读取：条目 content 必须完整（截断交给前端）")
        kb = fresh(data_dir)
        saved = kb.save_entry("tac 满改 mp5 方案", LONG, source="webui", status="verified", manual=True)
        got = kb.get_entry("tac 满改 mp5 方案")
        check("get_entry 取到条目", got is not None)
        check("content 完整无截断", got and got["content"] == LONG, f"len={len(got['content'])} vs {len(LONG)}")
        check("列表里 content 也完整", kb.list_entries()[0]["content"] == LONG)
        check("get_entry 不存在 → None", kb.get_entry("不存在的主题") is None)
        check("save_entry 返回体带 topic", saved.get("topic") == "tac 满改 mp5 方案")

        print("[2] 编辑正文（manual=True）：保住验证状态与元数据")
        before = kb.get_entry("tac 满改 mp5 方案")
        kb.save_entry("tac 满改 mp5 方案", LONG + "\n补充：弹匣用 30 发扩容", source="webui",
                      status=before["status"], manual=True)
        after = kb.get_entry("tac 满改 mp5 方案")
        check("status 仍为已验证（不降级）", after["status"] == "verified", after["status"])
        check("created_at 未变", after["created_at"] == before["created_at"])
        check("updated_at 刷新了", after["updated_at"] >= before["updated_at"])
        check("mod / kind 沿用", (after["mod"], after["kind"]) == (before["mod"], before["kind"]))
        check("正文已更新", after["content"].endswith("补充：弹匣用 30 发扩容"))
        check("还是只有一条", len(kb.list_entries()) == 1)

        print("[3] auto_apply 关闭：老规矩（LLM 学习转 pending）不许被改掉，手动编辑除外")
        kb2 = fresh(Path(tmp) / "kb2", auto_apply=False)
        kb2.save_entry("旧知识", "内容 A", source="llm_learn", status="verified")
        check("manual=False → 强制 pending", kb2.get_entry("旧知识")["status"] == "pending",
              kb2.get_entry("旧知识")["status"])
        kb2.save_entry("旧知识", "内容 B", source="webui", status="verified", manual=True)
        check("manual=True → 保住 verified", kb2.get_entry("旧知识")["status"] == "verified",
              kb2.get_entry("旧知识")["status"])
        kb2.save_entry("旧知识", "内容 B", source="llm_learn", status="verified")
        check("再走学习路径又回 pending", kb2.get_entry("旧知识")["status"] == "pending")

        print("[4] 改名：旧主题搬走 + 立墓碑，元数据沿用")
        kb3 = fresh(Path(tmp) / "kb3")
        kb3.save_entry("mp5 满改", "旧内容", source="webui", status="verified", manual=True)
        old = kb3.get_entry("mp5 满改")
        kb3.save_entry("tac mp5 满改方案 v2", "新内容", source="webui",
                       status=old["status"], rename_from="mp5 满改", manual=True)
        check("新主题在", kb3.get_entry("tac mp5 满改方案 v2") is not None)
        check("旧主题没了（不留残影）", kb3.get_entry("mp5 满改") is None)
        check("旧主题进墓碑", "mp5 满改" in kb3._data.get("deleted", []), str(kb3._data.get("deleted")))
        renamed = kb3.get_entry("tac mp5 满改方案 v2")
        check("created_at 沿用", renamed["created_at"] == old["created_at"])
        check("mod / kind 沿用", (renamed["mod"], renamed["kind"]) == (old["mod"], old["kind"]))
        check("status 沿用", renamed["status"] == "verified")
        check("正文是新写的", renamed["content"] == "新内容")
        check("只留一条", len(kb3.list_entries()) == 1)

        print("[5] 改名撞已有主题 → rename_exists 报 True（WebUI 据此拒绝覆盖）")
        kb4 = fresh(Path(tmp) / "kb4")
        kb4.save_entry("甲主题", "内容甲", source="webui")
        kb4.save_entry("乙主题", "内容乙", source="webui")
        check("撞名 → True", kb4.rename_exists("甲主题", "乙主题") is True)
        check("同名（没改）→ False", kb4.rename_exists("甲主题", "甲主题") is False)
        check("不撞 → False", kb4.rename_exists("甲主题", "丙主题") is False)

        print("[6] 墓碑不至于把改名后的条目也堵住：同名再写能复活")
        kb4.save_entry("甲主题", "又回来了", source="webui", manual=True)
        check("同名重写复活", kb4.get_entry("甲主题")["content"] == "又回来了")

    print()
    if failed:
        print(f"✗ {len(failed)} 项失败：" + "；".join(failed))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
