# -*- coding: utf-8 -*-
"""v0.23.5 · GitHub issue 回归：注入内容块必须是核心认得的对象，不能是裸 dict。

报告（插件 v0.23.3 + AstrBot v4.27.5，异地 RCON）：

  * `mcs` 指令一切正常，但走 LLM 的能力全挂 —— 发一句「mc 服务器状态」就报
    `Error occurred while processing agent: 'dict' object has no attribute
    'model_dump_for_context'`（`agent_sub_stages.internal`）。

根因：AstrBot 4.27.x 的 `ProviderRequest.assemble_context()` 对
`extra_user_content_parts` 里的每个元素**无条件**调用 `part.model_dump_for_context()`；
插件从 v0.21.0 起注入的是裸 dict `{"type": "text", "text": ...}`，dict 没有这个方法，
于是整条 LLM 流水线在这里炸掉 —— **报错位置在核心，很容易被带偏**。
字段文档写着「支持 dict 或 ContentPart 对象」，核心 master 分支后来才补上
`isinstance(part, dict)` 兼容分支：典型「文档先行、代码滞后」。

修法（插件侧，不能要求用户升到哪个版本）：优先构造官方 `TextPart`；
只有取不到这枚零件的老版本才退回 dict（那种核心自带 dict 兼容分支）。

本用例用 AST 摘出 main.py 里的**真源码**（main.py 依赖 astrbot 运行环境，不能直接 import），
并把「4.27.5 的无条件消费逻辑」原样复刻出来当靶子 —— 修复前它会当场变红。

运行：
  <python> tests\\test_v0235_upstream_content_part.py
"""
from __future__ import annotations

import __future__ as _future
import ast
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot.core.agent.message import TextPart as RealTextPart  # noqa: E402

PLUGIN = PLUGIN_DIR
MAIN_SRC = io.open(PLUGIN / "main.py", encoding="utf-8").read()

# `from __future__ import annotations` 的编译标记：main.py 里真正的注解是字符串，
# 摘出来的函数体也要按同样语义编译，否则 `req: ProviderRequest` 这种注解会在
# exec 时求值成 NameError（ProviderRequest 没有 import）。
_FUTURE_ANNOTATIONS = _future.annotations.compiler_flag

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name} {extra}")


def load_funcs(textpart=RealTextPart) -> dict:
    """从 main.py 摘出 `_make_text_part` 与 `McControlPlugin._append_user_hint` 的真源码。"""
    wanted = {"_make_text_part", "_append_user_hint"}
    nodes = [
        n for n in ast.walk(ast.parse(MAIN_SRC))
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    got = {n.name for n in nodes}
    assert got == wanted, f"main.py 里找不到：{wanted - got}"
    ns: dict = {"TextPart": textpart}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<main.py>", "exec", flags=_FUTURE_ANNOTATIONS), ns)
    return ns


class OldCoreRequest:
    """复刻 AstrBot v4.27.5 的消费逻辑：对每个元素**无条件**调 model_dump_for_context。"""

    def __init__(self) -> None:
        self.extra_user_content_parts: list = []

    def assemble_context(self) -> list:
        return [part.model_dump_for_context() for part in self.extra_user_content_parts]


class NewCoreRequest(OldCoreRequest):
    """带 dict 兼容分支的核心（master 之后的版本）。"""

    def assemble_context(self) -> list:
        return [
            part if isinstance(part, dict) else part.model_dump_for_context()
            for part in self.extra_user_content_parts
        ]


class _RecLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __getattr__(self, _name):
        def _rec(*args, **kwargs):
            if args:
                self.lines.append(str(args[0]))
        return _rec


class Host:
    def __init__(self) -> None:
        self.logger = _RecLogger()


# ==================================================================== A. 静态

def part_a() -> None:
    print("================ [A] 静态：注入点不再自己拼 dict ================")
    check("main.py 守卫导入官方 TextPart（老版本没有该模块时置 None）",
          "from astrbot.core.agent.message import TextPart" in MAIN_SRC
          and "TextPart = None" in MAIN_SRC)
    check("新增统一构造器 _make_text_part()",
          "def _make_text_part(" in MAIN_SRC)
    check("注入点改用构造器（不是裸 dict）",
          "parts.append(_make_text_part(block))" in MAIN_SRC)
    check("旧的裸 dict 写法已彻底消失",
          'parts.append({"type": "text", "text": block})' not in MAIN_SRC)


# ==================================================================== B. 复现 issue

def part_b() -> None:
    print("================ [B] 复现 issue：裸 dict 在 4.27.5 上必然炸 ================")
    old = OldCoreRequest()
    old.extra_user_content_parts.append({"type": "text", "text": "权限前置提醒"})
    hit = ""
    try:
        old.assemble_context()
    except AttributeError as e:
        hit = str(e)
    check("对照组：裸 dict → 'dict' object has no attribute 'model_dump_for_context'",
          "model_dump_for_context" in hit and "dict" in hit, f"→ {hit!r}")

    ns = load_funcs()
    part = ns["_make_text_part"]("权限前置提醒")
    check("修复组：_make_text_part 返回官方 TextPart（有 model_dump_for_context）",
          isinstance(part, RealTextPart) and callable(getattr(part, "model_dump_for_context", None)),
          f"→ {type(part).__name__}")
    dump = part.model_dump_for_context()
    check("落成核心要的形状 {type: text, text: ...}",
          dump.get("type") == "text" and dump.get("text") == "权限前置提醒", str(dump))

    fixed = OldCoreRequest()
    fixed.extra_user_content_parts.append(part)
    try:
        out = fixed.assemble_context()
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, out = False, f"{type(e).__name__}: {e}"
    check("修复组：同一段 4.27.5 消费逻辑不再抛异常", ok is True, str(out))
    check("修复组：核心拿到的正是提示文本",
          ok and out[0].get("text") == "权限前置提醒", str(out))


# ==================================================================== C. 真链路

def part_c() -> None:
    print("================ [C] 真链路：_append_user_hint 挂的块能被 4.27.5 消费 ================")
    ns = load_funcs()
    req = OldCoreRequest()
    ns["_append_user_hint"](Host(), req, ["第一块：权限前置提醒", "第二块：能力降级提醒"])
    check("两块都挂上了", len(req.extra_user_content_parts) == 2,
          str(len(req.extra_user_content_parts)))
    check("两块都不是裸 dict",
          all(not isinstance(p, dict) for p in req.extra_user_content_parts),
          str([type(p).__name__ for p in req.extra_user_content_parts]))
    try:
        out = req.assemble_context()
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, out = False, f"{type(e).__name__}: {e}"
    check("旧核心（无兼容分支）走一遍组装：不报错", ok is True, str(out))
    check("文本与顺序都对",
          ok and [d.get("text") for d in out] == ["第一块：权限前置提醒", "第二块：能力降级提醒"],
          str(out))

    # 老版本核心没有该字段 → 宁可不提醒，也不改写 system_prompt（既有契约，别被这次改动破坏）
    class NoField:
        pass

    req2 = NoField()
    ns["_append_user_hint"](Host(), req2, ["随便"])
    check("老版本无 extra_user_content_parts 字段：安静跳过（不新增属性）",
          not hasattr(req2, "extra_user_content_parts"))


# ==================================================================== D. 新核心与回退

def part_d() -> None:
    print("================ [D] 新核心兼容分支 + 更老版本回退 ================")
    ns = load_funcs()
    new = NewCoreRequest()
    new.extra_user_content_parts.append({"type": "text", "text": "dict 也给过"})
    new.extra_user_content_parts.append(ns["_make_text_part"]("对象也给过"))
    try:
        out = new.assemble_context()
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, out = False, f"{type(e).__name__}: {e}"
    check("带 dict 兼容分支的核心：dict 与 TextPart 都能过", ok is True, str(out))
    check("两种元素的文本都在", ok and [d.get("text") for d in out] == ["dict 也给过", "对象也给过"], str(out))

    ns_old = load_funcs(textpart=None)
    fallback = ns_old["_make_text_part"]("回退块")
    check("取不到 TextPart 时退回 dict（那种核心自带兼容分支）",
          isinstance(fallback, dict) and fallback.get("type") == "text"
          and fallback.get("text") == "回退块", str(fallback))


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[FAIL] 失败项:")
        for n in _fail:
            print("  -", n)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
