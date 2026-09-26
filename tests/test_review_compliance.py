# -*- coding: utf-8 -*-
"""AstrBot 上架规范护栏（v0.22.1 整改回归）。

背景：v0.22.0 提交市场被审核驳回（LLM Guard），两条意见在这里钉成可自动跑的护栏：

  1.【必须整改 · 日志记录】
     插件代码里不得出现标准库 logging（`import logging` / `logging.getLogger("astrbot")`），
     日志器必须且只能 `from astrbot.api import logger`。
  2.【建议优化 · 上下文注入】
     不得改写 `req.system_prompt` —— 含请求者 ID 的动态内容会让系统提示词前缀每次都变，
     直接废掉 prompt 前缀缓存（命中率下降 = 更慢更贵）。权限前置提醒改为挂到
     `req.extra_user_content_parts`（AstrBot 官方的插件注入口，拼在用户消息尾部）。

扫描范围刻意只覆盖「会打进发布包」的代码：main.py 与 core/**/*.py。
tests/ 不进 zip、测试脚本自己用 logging.basicConfig 也无害，故不参与扫描。

运行（必须用 AstrBot 自带 python，插件依赖 astrbot 包）：
  python tests\\test_review_compliance.py
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402

add_sys_paths()
require_app()
PLUGIN = PLUGIN_DIR

FAIL = []


def check(desc, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAIL.append(desc)
    print(f"[{tag}] {desc:<62}{extra}")


def shipped_sources() -> list[Path]:
    """发布包里的 Python 源码（与 release 打包口径一致）。"""
    files = [PLUGIN / "main.py", PLUGIN / "__init__.py"]
    files += [p for p in (PLUGIN / "core").rglob("*.py") if "__pycache__" not in p.parts]
    return [p for p in files if p.exists()]


SOURCES = shipped_sources()


# ===================== A. 日志记录：只能用 astrbot.api 的 logger =====================
print("=========== A. 日志记录（必须 from astrbot.api import logger） ===========")
print(f"      扫描 {len(SOURCES)} 个源文件")

BANNED_LOGGING = re.compile(
    r"^\s*import\s+logging\b"          # import logging
    r"|^\s*from\s+logging\s+import\b"  # from logging import ...
    r"|\blogging\.(getLogger|basicConfig|DEBUG|INFO|WARNING|ERROR|CRITICAL)\b"
)

banned_hits = []
import_logging_files = []
for path in SOURCES:
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if BANNED_LOGGING.search(line):
            banned_hits.append(f"{path.relative_to(PLUGIN).as_posix()}:{lineno}")
        if "from astrbot.api import logger" in line:
            import_logging_files.append(path.relative_to(PLUGIN).as_posix())

check("源码里没有标准库 logging 的任何用法", not banned_hits,
      "→ " + ", ".join(banned_hits[:6]) if banned_hits else "")
check("曾经点名的两处日志器用法已消失（core/hot_reload.py 与 core/agent_llm.py 不再出现 GetLogger）",
      not any("hot_reload.py" in h or "agent_llm.py" in h for h in banned_hits))
check("hot_reload 与 agent_llm 都从 astrbot.api 取日志器",
      "core/hot_reload.py" in import_logging_files
      and "core/agent_llm.py" in import_logging_files,
      "→ " + ", ".join(import_logging_files))

# 反过来：任何自己取了 logger 变量的模块，都不能来自标准库（AST 层再确认一次）
for path in SOURCES:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    check(
                        f"{path.relative_to(PLUGIN).as_posix()} 没有 import logging", False
                    )
                    break


# ===================== B. 上下文注入：不得改写 system_prompt =====================
print("\n=========== B. 上下文注入（不得改写 system_prompt） ===========")

ASSIGN_SYSTEM_PROMPT = re.compile(r"\.\s*system_prompt\s*(?<![=!<>])=(?!=)")
assign_hits = []
for path in SOURCES:
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if ASSIGN_SYSTEM_PROMPT.search(line):
            assign_hits.append(
                f"{path.relative_to(PLUGIN).as_posix()}:{lineno}| {line.strip()[:70]}"
            )

check("没有任何一处给 .system_prompt 赋值（含请求者 ID 的动态内容会破坏前缀缓存）",
      not assign_hits, "→ " + " ; ".join(assign_hits[:4]) if assign_hits else "")

main_src = (PLUGIN / "main.py").read_text(encoding="utf-8")
check("提供了统一注入出口 _append_user_hint（拼到用户消息的额外内容块）",
      "def _append_user_hint(" in main_src)
check("注入出口用的是 extra_user_content_parts，且格式是 {type: text, text: ...}",
      'parts.append({"type": "text", "text": block})' in main_src)
check("注入出口收口到 _emit_hint：_append_user_hint 只在内部调用一次（唯一出口）",
      main_src.count("self._append_user_hint(req, blocks)") == 1,
      f"→ 实际 {main_src.count('self._append_user_hint(req, blocks)')} 处")
check("三处注入分支都经 _emit_hint（v0.23.3：管理员 / 无启用工具 / 权限提醒）",
      main_src.count("self._emit_hint(") == 3,
      f"→ 实际 {main_src.count('self._emit_hint(')} 处")
check("老版本 AstrBot 无该字段时宁可不提醒、也不回退写 system_prompt",
      "extra_user_content_parts" in main_src and "跳过权限前置提醒" in main_src)


# ===================== C. 运行期验证（真对象，不是静态扫描） =====================
print("\n=========== C. 运行期验证 ===========")

from astrbot.api import logger as astrbot_logger  # noqa: E402

from astrbot_plugin_Scintilla_MC_Server_Control.core import hot_reload as hr  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.agent_llm import AgentLLM  # noqa: E402

check("core/hot_reload 的模块级日志器就是 astrbot.api 的 logger",
      getattr(hr, "logger", None) is astrbot_logger,
      f"→ {type(getattr(hr, 'logger', None)).__name__}")

# 不传 logger（历史上这里踩过坑：赋值语句落在 return 之后成了死代码，
# 未传 logger 时 self._logger 根本不存在，一 warning 就 AttributeError）
agent = AgentLLM(None, {}, None)
check("AgentLLM 未传 logger 时 self._logger 仍可用（死代码坑已修）",
      getattr(agent, "_logger", None) is astrbot_logger,
      f"→ {type(getattr(agent, '_logger', None)).__name__}")

sentinel = object()
agent2 = AgentLLM(None, {}, None, logger=sentinel)
check("插件显式传入的 logger 优先（保持既有调用方式不变）",
      agent2._logger is sentinel)

# 兜底配置读取器不再兼职设置日志器（副作用已挪进 __init__）
check("_fallback_cfg 只剩「读配置」一件事（无隐藏副作用）",
      "self._logger" not in (PLUGIN / "core" / "agent_llm.py").read_text(encoding="utf-8")
      .split("def _fallback_cfg", 1)[1].split("def _cfg", 1)[0])

print("\n================ 汇总 ================")
print(f"失败 {len(FAIL)} 项" + ("" if not FAIL else "：" + " / ".join(FAIL)))
sys.exit(1 if FAIL else 0)
