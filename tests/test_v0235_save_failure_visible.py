"""v0.23.5 回归：持久化失败必须可见 —— 不许静默 pass、不许报假成功。

背景（外部审查指出）：`kb.save()` 吞异常，`_set_cfg_batch` 丢 `_save_config()`
的返回值。核实后确认实际情况是**三层都断**：

  · `core/knowledge_base.py` 三处落盘全是 `except Exception: pass`，且**全文件零日志**
    —— 向量索引 / 开关状态 / 整库 JSON，磁盘满或路径被删时一声不吭
  · `main.py` 的 `_save_config()` 回退分支连日志都没有，返回值被两个调用点丢掉
  · `web_api.py` 的 `save_settings` 因此照样回 `ok:True`；知识库 5 个写端点
    也一律回 `ok:True` —— 界面上"保存成功"，重启后改动凭空消失

本测试钉住四条契约：

  [1] `ModKnowledgeBase.save()` 落盘失败 → 返回 False，`save_health()` 说真话
  [2] 路径恢复正常 → 再存一次就转好，error 清空（状态不是一次性的）
  [3] `KnowledgeState.save()` 同样返回真实结果
  [4] 源码层面：三处静默 pass 已消失；配置侧记录落盘状态；API 层把
      `save_warning` / `config_saved` 回给前端；5 个知识库写端点全部挂上告警

运行：
  python tests\\test_v0235_save_failure_visible.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as kbmod   # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import (                  # noqa: E402
    McControlWebApi,
)

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool) -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}")


class _FakePlugin:
    pass


def break_path_onto_dir(base: Path, name: str) -> Path:
    """造一个「非空目录」，replace 到它上面在 Windows 上必定失败。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "occupied.txt").write_text("occupied", encoding="utf-8")
    return d


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)

        print("================ [1] 知识库整库落盘失败要说话 ================")
        kb = kbmod.ModKnowledgeBase(str(base))
        check("初始 save_health 是健康的", kb.save_health()["ok"] is True)

        good_path = kb.file_path
        kb.file_path = break_path_onto_dir(base, "kb_json")
        ok = kb.save()
        health = kb.save_health()
        check("save() 返回 False（不再无声无息）", ok is False)
        check("save_health().ok 为 False", health["ok"] is False)
        check("save_health().error 带上了原因", bool(health["error"]))
        check("save_health().at 有时间戳", bool(health["at"]))

        print("\n================ [2] 恢复后能自己转好（状态不是一次性的） ================")
        kb.file_path = good_path
        ok2 = kb.save()
        health2 = kb.save_health()
        check("修好路径后 save() 返回 True", ok2 is True)
        check("save_health().ok 回到 True", health2["ok"] is True)
        check("旧的 error 被清空", health2["error"] == "")

        print("\n================ [3] 开关状态落盘同样说真话 ================")
        st = kbmod.KnowledgeState(str(base / "state_ok"))
        check("正常路径 -> True", st.save() is True)
        st.file_path = break_path_onto_dir(base, "state_json")
        check("坏路径 -> False", st.save() is False)
        check("last_save_error 非空", bool(st.last_save_error))

        print("\n================ [4a] API 层的落盘告警文案 ================")
        api = McControlWebApi(_FakePlugin())

        class _KbOk:
            def save_health(self):
                return {"ok": True, "error": "", "at": "t"}

        class _KbBad:
            def save_health(self):
                return {"ok": False, "error": "磁盘已满", "at": "t"}

        class _KbBroken:
            """连 save_health 都没有的老对象 —— 不许把接口炸了。"""

        check("健康 -> 无告警", api._kb_save_warning(_KbOk()) == "")
        w = api._kb_save_warning(_KbBad())
        check("失败 -> 有告警文案", bool(w))
        check("告警里点明「落盘失败」", "落盘失败" in w)
        check("告警里带上了原因", "磁盘已满" in w)
        check("告警里提醒重启会丢", "重启" in w)
        check("没有 save_health 的对象 -> 空串（不抛异常）",
              api._kb_save_warning(_KbBroken()) == "")

        print("\n================ [4b] 源码静态检查 ================")
        kbsrc = (PLUGIN_DIR / "core" / "knowledge_base.py").read_text(encoding="utf-8")
        mainsrc = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        api_src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")

        check("知识库用 astrbot.api 的 logger（项目规范：不用标准库 logging）",
              "from astrbot.api import logger" in kbsrc)
        check("没有引入标准库 logging", "import logging" not in kbsrc)
        check("三处 save 都返回 bool",
              len(re.findall(r"def save\(self.*\) -> bool", kbsrc)) +
              len(re.findall(r"def save\(self, path: Path\) -> bool", kbsrc)) >= 3)
        check("有 save_health() 供上层查询", "def save_health(self) -> dict:" in kbsrc)
        check("整库 save 的 callers 仍会重建索引（注释未丢）",
              "_rebuild_index()" in kbsrc)

        check("main._save_config 记录成功状态",
              "self._last_cfg_save_ok = True" in mainsrc)
        check("main._save_config 记录失败状态与原因",
              "self._last_cfg_save_error = f" in mainsrc)
        check("回退分支失败也会记日志（此前静默）",
              'self.logger.warning("配置回退写盘失败' in mainsrc)

        check("save_settings 读取落盘状态",
              "_last_cfg_save_ok" in api_src and "cfg_save_ok" in api_src)
        check("save_settings 回包带 config_saved",
              '"config_saved": cfg_save_ok' in api_src)
        check("save_settings 的 notice 会显著告警",
              "配置文件落盘失败" in api_src)
        n_kb_hooks = len(re.findall(r'"save_warning": self\._kb_save_warning\(kb\)', api_src))
        check(f"5 个知识库写端点都挂上告警（实际 {n_kb_hooks} 处）", n_kb_hooks == 5)
        check("helper 自身存在", "def _kb_save_warning(self, kb) -> str:" in api_src)

        print("\n================ [5] 前端：告警要真的显示出来 ================")
        html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")

        check("有 saveWarnText 读取 save_warning", "function saveWarnText(r){" in html)
        check("有 showSaveWarn 统一展示", "function showSaveWarn(r, el){" in html)
        check("设置保存按 config_saved 判成败（不再无条件加 ✓）",
              "r.config_saved !== false" in html)
        n_show = len(re.findall(r"showSaveWarn\(", html))
        check(f"知识库写操作已接线（showSaveWarn 出现 {n_show} 处，含定义应 >= 7）", n_show >= 7)
        check("kb/toggle 已捕获响应",
              'callPost("kb/toggle",{topic,enabled:b.dataset.en==="1"}).catch' in html)
        check("kb/delete 已捕获响应", 'callPost("kb/delete",{topic}).catch' in html)
        check("kb/approve 已捕获响应", 'callPost("kb/approve",{topic,approve:true}).catch' in html)
        check("详情弹窗保存也接了告警（与新增条目同款形式）",
              html.count('if(!showSaveWarn(r, $("kb_notice")))') >= 2)
        check("不再有「发完不看响应」的 kb/toggle 写法",
              'await callPost("kb/toggle",{topic,enabled:b.dataset.en==="1"}); kbReload();' not in html)


    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
