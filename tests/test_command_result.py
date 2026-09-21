# -*- coding: utf-8 -*-
"""v0.22.6 统一命令结果判定 —— 矩阵测试。

运行：
  <python> tests/test_command_result.py

不依赖 AstrBot 运行时（``core/command_result.py`` 只用标准库），
因此本文件在任何环境下都必须跑得起来、不许 SKIP。

改代码前先读这三条设计要点
==========================
1. ``status`` / ``response_received`` / ``boundary_confirmed`` 是**三个独立维度**，
   任何一个都不能代替另外两个。
   ``response_received=True`` + ``boundary_confirmed=False`` 是**合法组合**
   （idle 降级模式），此时消息类命令应报 ``dispatched_unconfirmed``，
   既不是「失败」、也不是「结果未知」。
2. 「命令不存在」与「语法错误」只差几个词，语义**相反**：
   前者重试徒劳，后者可安全重写重试。
3. 错误用黑名单、成功用「非空且无错误标记」——
   严格白名单会与既有熔断叠加，把多命令任务的后续命令整批吞掉。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

FAIL: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAIL.append(desc)
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}"
          + (f"  <- {detail}" if detail and not ok else ""))


def _ensure_core_package():
    """把 ``core/`` 注册成一个真正的包（``core/__init__.py`` 是空的，安全）。"""
    if "core" in sys.modules:
        return sys.modules["core"]
    spec = importlib.util.spec_from_file_location(
        "core",
        PLUGIN_DIR / "core/__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR / "core")],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["core"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load(name: str, rel: str):
    """加载插件模块；``core/`` 下的按**包内模块**加载。

    v0.22.7：core 内部开始有相对导入（``command_result`` → ``java_commands``），
    而 ``spec_from_file_location`` 的单文件加载没有父包，``from .x import y``
    会直接 ``ImportError: attempted relative import with no known parent package``。
    ``core/__init__.py`` 是空的，因此可以安全地把 core 注册成包再按 ``core.<模块名>``
    加载 —— 模块名必须与包路径一致，相对导入才解析得到。
    """
    path = PLUGIN_DIR / rel
    if rel.startswith("core/"):
        _ensure_core_package()
        mod_name = "core." + Path(rel).stem
    else:
        mod_name = name
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules 再 exec：@dataclass 会通过
    # sys.modules[cls.__module__] 反查命名空间，不注册就拿到 None →
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cr = _load("cr", "core/command_result.py")
classify = cr.classify_command_output

#: 真实事故原文（2026-09-21 19:18，服务端 MC 1.20.1 · Forge 47.4.23 实测）
REAL_SYNTAX_ERR = "Expected whitespace to end one argument, but found trailing data"
REAL_OK = "Gave 1 [Netherite Sword] to HuangGuaKnn"
REAL_BAD_CMD = ('give HuangGuaKnn netherite_sword[enchantments={levels:'
                '{"minecraft:sharpness":5,"minecraft:sweeping_edge":3}}] 1')
REAL_GOOD_CMD = ('give HuangGuaKnn netherite_sword{Enchantments:'
                 '[{id:"minecraft:sharpness",lvl:5}]} 1')


# ===================== 一、结果矩阵 =====================

def matrix_cases() -> None:
    print("---- 一、结果矩阵 ----")

    # ---- 空响应：三个维度一起看（静默命令 tellraw）----
    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=True, response_received=True)
    check("tellraw 空 + 边界已确认 + 收到响应 → success",
          r.status == "success", r.status)

    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=False, response_received=True)
    check("tellraw 空 + 边界未确认 + 收到响应 → dispatched_unconfirmed（不是失败、不是未知）",
          r.status == "dispatched_unconfirmed", r.status)

    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=False, response_received=False)
    check("tellraw 空 + 未收到响应 → unknown", r.status == "unknown", r.status)

    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=None, response_received=False)
    check("tellraw 空 + 尚未执行过命令(None) → unknown", r.status == "unknown", r.status)

    # dispatched_unconfirmed 不算成功，但算「已被接受」（不该熔断后续命令）
    r2 = classify('tellraw @a {"text":"hi"}', "",
                  boundary_confirmed=False, response_received=True)
    check("dispatched_unconfirmed：不得宣称成功（ok=False）", r2.ok is False)
    check("dispatched_unconfirmed：视为已送达（accepted=True，不熔断后续命令）",
          r2.accepted is True, str(r2.accepted))

    # ---- 幂等但**非静默**（time 在 vanilla 里是有回显的）----
    r = classify("time set day", "", boundary_confirmed=True, response_received=True)
    check("time 空 + 边界已确认 → success（幂等集合，不是静默集合）",
          r.status == "success", r.status)

    # ---- 非幂等命令：空响应**永远不能**当成功 ----
    r = classify("give Steve diamond 1", "",
                 boundary_confirmed=True, response_received=True)
    check("give 空 + 边界已确认 → unknown（副作用是否落定无法确认）",
          r.status == "unknown", r.status)

    r = classify("summon minecraft:zombie", "",
                 boundary_confirmed=True, response_received=True)
    check("summon 空 → unknown", r.status == "unknown", r.status)

    # ---- 「命令不存在」vs「语法错误」：只差几个词，语义相反 ----
    r = classify("frobnicate", 'Unknown command. Type "/help" for help.')
    check("Unknown command → failed 且不可重试（重试徒劳）",
          r.status == "failed" and r.retryable is False,
          f"{r.status}/{r.retryable}")

    r = classify("give", "Unknown or incomplete command, see below for error")
    check("Unknown or incomplete command → syntax_error 且可重试",
          r.status == "syntax_error" and r.retryable is True,
          f"{r.status}/{r.retryable}")

    # ---- 真实事故原文（本次修复的靶心）----
    r = classify(REAL_BAD_CMD, REAL_SYNTAX_ERR)
    check("★真实事故：Expected whitespace… → syntax_error（绝不再是成功）",
          r.status == "syntax_error", r.status)

    r = classify(REAL_GOOD_CMD, REAL_OK)
    check("★真实对照：Gave 1 [Netherite Sword]… → success（explicit）",
          r.status == "success" and r.confidence == "explicit",
          f"{r.status}/{r.confidence}")

    # ---- 其它明确失败 ----
    r = classify("kick Nobody", "Player Nobody not found")
    check("Player not found → failed", r.status == "failed", r.status)

    # ---- 模组返回的任意文本：不得谎报成功，也不得误吞后续命令 ----
    # v0.22.7：模组命令的反馈文本不可枚举，不能逼它命中成功模式表，
    # 但「没发现错误」也**不是**成功的证据 —— 因此落 inferred_success：
    # ok=False（绝不对外宣称成功）、accepted=True（不熔断后续命令）。
    r = classify("tac:reload", "Reloaded 42 gun definitions")
    check("模组未知文本 → inferred_success（不是 success，也不熔断）",
          r.status == "inferred_success" and r.confidence == "inferred"
          and r.ok is False and r.accepted is True,
          f"{r.status}/{r.confidence}/ok={r.ok}")

    # ---- 非幂等 + 未知文本：不确认副作用 → unknown（熔断，宁停不重）----
    r = classify("give Steve diamond 1", "Weird modded feedback xyz")
    check("非幂等 + 未知文本 → unknown（不确认副作用，宁可熔断）",
          r.status == "unknown" and r.ok is False and r.accepted is False,
          r.status)

    # ---- 成功识别必须早于 unknown 兜底 ----
    r = classify("give Steve diamond 1", "Gave 1 [Diamond] to Steve")
    check("成功识别早于 unknown 兜底（旧顺序会把它吞成 unknown）",
          r.status == "success", r.status)


# ===================== 二、复杂任务路由 =====================

def routing_cases() -> None:
    print("---- 二、复杂任务路由守门 ----")

    # 选择器语法（裸 [）**不得**触发复杂路由
    for cmd in ("kill @e[type=item]", "tp @p[tag=foo]",
                "gamemode creative @a[team=red]"):
        check(f"选择器语法不触发复杂路由：{cmd}",
              not cr.looks_like_complex_task("清理一下", [cmd]), cmd)

    # 真实事故命令（1.21 组件语法）必须触发
    check("★真实事故命令（1.21 组件语法）必须触发复杂路由",
          cr.looks_like_complex_task("发把剑", [REAL_BAD_CMD]), REAL_BAD_CMD)

    # legacy NBT 也必须触发
    check("legacy NBT（{Enchantments:…}）必须触发复杂路由",
          cr.looks_like_complex_task("发把剑", [REAL_GOOD_CMD]), REAL_GOOD_CMD)

    check("普通 give 仍可走 simple",
          not cr.looks_like_complex_task("给我点钻石", ["give Steve diamond 64"]))

    check("带模组命名空间的普通物品命令不误判",
          not cr.looks_like_complex_task("给点铜锭",
                                         ["give Steve create:copper_ingot 8"]))

    check("tellraw（含 {} 但不是物品命令）不触发复杂路由",
          not cr.looks_like_complex_task("广播一句", ['tellraw @a {"text":"hi"}']))

    check("★用户请求「附魔」命中第一层信号（哪怕命令本身是简单的）",
          cr.looks_like_complex_task("帮我发一把下界合金剑，附魔为锋利5",
                                     ["give Steve diamond_sword 1"]))

    check("execute 包装后的复杂物品命令也要拦",
          cr.looks_like_complex_task("发把剑",
                                     ["execute as @a run " + REAL_GOOD_CMD]))


# ===================== 三、命令名解析 =====================

def name_cases() -> None:
    print("---- 三、命令名解析（不得用前缀匹配）----")
    check("minecraft:tellraw 也算 tellraw（去命名空间）",
          cr.base_name("minecraft:tellraw @a x") == "tellraw")
    check("前导斜杠被剥掉", cr.base_name("/tellraw @a x") == "tellraw")
    check("execute 包装被穿透",
          cr.base_name("execute as @a run give Steve diamond 1") == "give")
    check("裸 execute（无 run）判为 execute",
          cr.base_name("execute as @a") == "execute")
    check("tellrawx **不是** tellraw（前缀匹配会误判）",
          cr.base_name("tellrawx @a x") == "tellrawx")
    check("tellrawx 空响应不得当成功",
          classify("tellrawx @a x", "", boundary_confirmed=True,
                   response_received=True).status == "unknown")


# ===================== 四、反向对照（回归护栏） =====================

def reverse_control_cases() -> None:
    """这些断言的作用：**删掉关键逻辑后，上面的正向用例必须变红**。

    · 删掉 ``_SYNTAX_MARKERS`` 里的 ``expected`` → 真实事故用例变红；
    · 把 ``time`` 挪进 ``SILENT_EMPTY_SUCCESS_COMMANDS`` → 静默/幂等语义用例变红；
    · 把 ``give`` 移出 ``NON_IDEMPOTENT_COMMANDS`` → 空响应用例变红；
    · 把裸 ``[`` 加回全局特征 → 选择器用例变红；
    · 打开 ``STRICT_SUCCESS_MATCHING`` → 模组文本用例变红。
    """
    print("---- 四、反向对照 ----")
    check("语法黑名单非空且含 'expected'（真实事故靠它兜住）",
          any("expected" in m for m in cr._SYNTAX_MARKERS))
    check("'unknown command' 与 'unknown or incomplete' 分属不同集合",
          any("unknown command" in m for m in cr._UNKNOWN_COMMAND_MARKERS)
          and any("unknown or incomplete" in m for m in cr._SYNTAX_MARKERS))
    check("非幂等集合含 give/summon/effect",
          {"give", "summon", "effect"} <= set(cr.NON_IDEMPOTENT_COMMANDS))
    check("静默集合与幂等集合**不混用**（time 不在静默集合里）",
          "time" not in cr.SILENT_EMPTY_SUCCESS_COMMANDS
          and "time" in cr.IDEMPOTENT_COMMANDS)
    check("v0.22.7：失败词已拆成「短语 / 英文词 / 中文词」三组（不再裸子串扫英文）",
          hasattr(cr, "_PHRASE_FAILURE_MARKERS")
          and hasattr(cr, "_WORD_FAILURE_MARKERS")
          and hasattr(cr, "_CJK_FAILURE_MARKERS")
          and not hasattr(cr, "_FAILED_MARKERS"))
    check("v0.22.7：英文失败词走词边界（`Error404` / `MrError` 这类名字不再误伤）",
          cr._FAILURE_WORD_RE.search("operation error occurred") is not None
          and cr._FAILURE_WORD_RE.search("gave 1 [diamond] to Error404") is None
          and cr._FAILURE_WORD_RE.search("gave 1 [diamond] to MrError") is None)
    check("v0.22.7：★玩家名就叫 Error —— 真正兜住它的是**锚定成功模式**而非词边界",
          cr.classify_command_output(
              "give Error diamond 1", "Gave 1 [Diamond] to Error"
          ).status == "success")
    check("v0.22.7：Operation aborted → failed（旧口径会判 success）",
          cr.classify_command_output(
              "give Steve diamond 1", "Operation aborted"
          ).status == "failed")
    check("v0.22.7：原版命令有锚定成功模式表，模组命令不在表里",
          "give" in cr.ANCHORED_SUCCESS_PATTERNS
          and "tacz" not in cr.ANCHORED_SUCCESS_PATTERNS)
    check("v0.22.7：inferred_success 已进入状态机且**不等于** success",
          "inferred_success" in cr.CommandStatus.__args__
          and cr.CommandResult(command="x", status="inferred_success").ok is False
          and cr.CommandResult(command="x", status="inferred_success").accepted is True)
    check("v0.22.7：INFERRED_SUCCESS_ENABLED 默认开（关掉则全落 unknown）",
          cr.INFERRED_SUCCESS_ENABLED is True)


def main() -> int:
    matrix_cases()
    routing_cases()
    name_cases()
    reverse_control_cases()

    print("==========================================")
    if FAIL:
        print(f"FAILED {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过：三态结果判定 / 命令不存在与语法错误拆分 / 复杂 NBT 路由守门")
    return 0


if __name__ == "__main__":
    sys.exit(main())
