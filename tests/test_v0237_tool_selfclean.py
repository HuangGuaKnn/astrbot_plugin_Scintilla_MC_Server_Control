# -*- coding: utf-8 -*-
"""v0.23.7 · B1：LLM 工具自清（重载幽灵的工具栈除积）。

运行：
  <python> tests/test_v0237_tool_selfclean.py

背景（2026-10-05 §十一 第二回合 · 幽灵工具事故）
=================================================
- 每轮重载日志「Added llm tool ×17」；风暴配方（配置变更＋页存＋官方存＋停用启用
  ＋连环重载）下工具栈线性膨胀（实测 17×3 条同名），陈旧条目的 handler 绑着
  旧实例/旧配置 —— 工具链死连已废弃端口的「幽灵」由此而来。
- 核心 ``add_func`` 是「删一个（首个同名）再追加」：一旦某名字积到 ≥3 条，
  单删一加就永远收敛不回 1 条；而 ``get_func`` 的取用口径是「取最后一个」。
- 插件侧自清（本单）：加载时把本插件名下条目**每名字只留最后一次注册**，
  其余全部移除；只动本插件名下条目，不碰核心与其它插件。

本测试钉死：
1. 判属逻辑（模块路径 / 原始函数 / partial / 绑定实例 四种形态）；
2. 伪造幽灵（3 条同名 + 别的插件的双份）→ 自清后本插件每名字恰 1 条、
   保留最后一条、其它插件条目纹丝不动、返回移除数正确；
3. 真环境端到端：import 后 17 条 → reload 模拟重载（允许 17/34 两态）→ 自清归一
   → 风暴残渣（翻倍至 34）→ 自清回 17（移除 17）→ 幂等（再清 0 条）；
4. 源码扫描：initialize() 确实调用自清且包了 try/except（失败不阻加载）。
"""
from __future__ import annotations

import functools
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

_fail: list[str] = []
_pass = 0
_skip: list[str] = []


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


class _NS:
    """轻量替身：dedupe 只读 name / handler / handler_module_path 三个属性。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fn_with_module(modname: str):
    def f():
        pass

    f.__module__ = modname
    return f


def part_one(pm) -> None:
    print("---- 一、判属逻辑（四种形态） ----")
    C = pm.McControlPlugin
    pkg = pm.__package__
    ours_mod = pkg + ".main"
    other_mod = "data.plugins.some_other_plugin.main"

    check("模块路径形态 → 属于本插件",
          C._llm_tool_entry_is_ours(
              _NS(name="mc_x", handler_module_path=ours_mod, handler=None)))
    check("原始函数 __module__ 形态 → 属于本插件",
          C._llm_tool_entry_is_ours(
              _NS(name="mc_x", handler_module_path=None,
                  handler=_fn_with_module(ours_mod))))
    check("partial(raw, cls) 形态 → 属于本插件",
          C._llm_tool_entry_is_ours(
              _NS(name="mc_x", handler_module_path=None,
                  handler=functools.partial(_fn_with_module(ours_mod), object))))
    owner = type("OwnerCls", (), {})()
    type(owner).__module__ = ours_mod
    h = types.SimpleNamespace(__self__=owner)
    check("绑定实例形态 → 属于本插件",
          C._llm_tool_entry_is_ours(
              _NS(name="mc_x", handler_module_path=None, handler=h)))
    check("其它插件的条目 → 不属于（绝不误伤）",
          not C._llm_tool_entry_is_ours(
              _NS(name="mc_x", handler_module_path=other_mod,
                  handler=_fn_with_module(other_mod))))


def part_two(pm) -> None:
    print("---- 二、伪造幽灵 → 自清收口 ----")
    from astrbot.core.provider.register import llm_tools

    C = pm.McControlPlugin
    pkg = pm.__package__
    ours_mod = pkg + ".main"
    other_mod = "data.plugins.some_other_plugin.main"

    a1 = _NS(name="mc_alpha", handler_module_path=ours_mod, handler=None)
    a2 = _NS(name="mc_alpha", handler_module_path=ours_mod, handler=None)
    a3 = _NS(name="mc_alpha", handler_module_path=ours_mod, handler=None)
    b1 = _NS(name="mc_beta", handler_module_path=ours_mod, handler=None)
    g1 = _NS(name="mc_gamma", handler_module_path=other_mod, handler=None)
    g2 = _NS(name="mc_gamma", handler_module_path=other_mod, handler=None)
    fake = [a1, a2, a3, b1, g1, g2]

    stub = C.__new__(C)
    stub.logger = logging.getLogger("t0237-b1")

    saved = llm_tools.func_list
    llm_tools.func_list = fake
    try:
        removed = stub._dedupe_own_llm_tools()
        names = [f.name for f in fake]
        kept_alpha = [f for f in fake if f.name == "mc_alpha"]
    finally:
        llm_tools.func_list = saved

    check("移除数 = 2（alpha 的两条幽灵）", removed == 2, str(removed))
    check("mc_alpha 收敛为 1 条", names.count("mc_alpha") == 1, str(names))
    check("mc_beta 原样保留（1 条）", names.count("mc_beta") == 1)
    check("★其它插件 mc_gamma 双份纹丝不动", names.count("mc_gamma") == 2, str(names))
    check("保留的是最后一条（alpha 剩 a3）",
          kept_alpha and kept_alpha[0] is a3, str(len(kept_alpha)))


def part_three(pm) -> None:
    print("---- 三、真环境端到端（import → 重载模拟 → 风暴残渣 → 自清） ----")
    import importlib

    from astrbot.core.provider.register import llm_tools

    C = pm.McControlPlugin

    def ours():
        return [f for f in llm_tools.func_list
                if C._llm_tool_entry_is_ours(f)]

    n0 = len(ours())
    check("首次 import 后本插件登记 17 条", n0 == 17, str(n0))
    if n0 == 0:
        return

    # 真实重载模拟：reload(main) → 装饰器再执行一遍（核心行为：删一加一 或 追加）
    importlib.reload(pm)
    C2 = pm.McControlPlugin
    nR = len([f for f in llm_tools.func_list if C2._llm_tool_entry_is_ours(f)])
    check("重载模拟：条目数 17 或 34（两种核心行为都接受）", nR in (17, 34), str(nR))
    stub = C2.__new__(C2)
    stub.logger = logging.getLogger("t0237-b1")
    stub._dedupe_own_llm_tools()
    nR2 = len([f for f in llm_tools.func_list if C2._llm_tool_entry_is_ours(f)])
    check("重载后自清归一：17 条", nR2 == 17, str(nR2))

    # 风暴残渣：把现有条目整份再挂一遍（同名翻倍，模拟连环重载的积垢）
    llm_tools.func_list.extend(
        [f for f in llm_tools.func_list if C2._llm_tool_entry_is_ours(f)]
    )
    n1 = len([f for f in llm_tools.func_list if C2._llm_tool_entry_is_ours(f)])
    check("风暴残渣：翻倍至 34 条", n1 == 34, str(n1))

    removed = stub._dedupe_own_llm_tools()
    n2 = len([f for f in llm_tools.func_list if C2._llm_tool_entry_is_ours(f)])
    check("★自清：移除 17 条幽灵", removed == 17, str(removed))
    check("★自清后每名字恰 1 条（共 17）", n2 == 17, str(n2))

    removed2 = stub._dedupe_own_llm_tools()
    check("幂等：再清 0 条", removed2 == 0, str(removed2))

    names = sorted({f.name for f in llm_tools.func_list
                    if C2._llm_tool_entry_is_ours(f)})
    check("17 个工具名齐全", len(names) == 17, str(names))
    check("mc_ 前缀名单抽查", {"mc_workflow", "mc_broadcast", "mc_give_item"} <= set(names))


def part_four() -> None:
    print("---- 四、源码登记（initialize 自清 + try/except） ----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("_dedupe_own_llm_tools 已定义", "def _dedupe_own_llm_tools(self)" in src)
    check("判属函数已定义", "def _llm_tool_entry_is_ours(entry)" in src)
    check("★initialize() 顶部调用自清", "self._dedupe_own_llm_tools()" in src)
    check("★自清失败不阻加载（try/except + 警告文案）",
          "LLM 工具自清失败（不影响其它功能）" in src)
    check("判属覆盖 partial 形态（绑定生命周期的真实形态）",
          "functools.partial(raw, star_cls)" in src or "functools.partial" in src)
    check("只动本插件条目（不碰核心/其它插件的注释契约在案）",
          "不碰核心与其它插件" in src)


def main() -> None:
    pm = None
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control import main as pm  # noqa: F811
    except Exception as e:
        skip("插件层（一、二、三）", f"缺 AstrBot 运行时：{type(e).__name__}: {e}")

    if pm is not None:
        part_one(pm)
        part_two(pm)
        part_three(pm)

    part_four()

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
