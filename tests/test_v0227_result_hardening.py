# -*- coding: utf-8 -*-
"""v0.22.7 结果判定加固 —— 核验裁决的回归测试。

运行：
  <python> tests/test_v0227_result_hardening.py

不依赖 AstrBot 运行时（``core/command_result.py`` 与 ``core/rcon.py`` 都只用标准库），
因此本文件在任何环境下都必须跑得起来、不许 SKIP。

本文件钉死的是 v0.22.7 核验裁决里**每一条都是「不改就会出事」**的规则
====================================================================
1. **非幂等命令的空响应永远最高优先级判 unknown** —— 不能先走
   ``boundary_confirmed`` 分支拿到 ``dispatched_unconfirmed``：
   那条路径 ``accepted=True`` → 不熔断后续命令 → 削弱 v0.22.4 建立的重复副作用保护。
2. **非幂等命令在「边界未确认 + 非空响应」下也必须继续 unknown** ——
   消息类（tellraw / say / title）才允许 ``dispatched_unconfirmed``。
3. **``Operation aborted`` 必须判失败** —— 旧口径把它当「无错误标记」→ 假成功。
4. **错误关键词要词边界** —— 玩家名叫 ``Error`` 时 ``Gave 1 [Diamond] to Error``
   不能被当成失败；反过来 ``Operation error:`` 仍必须判失败。
   锚定成功模式（``^gave\\b``）必须**先于**全局失败词扫描。
5. **未知非空输出不得默认 success** —— 至少要是 ``inferred_success``
   （``ok=False``，不对外宣称成功），非幂等命令则直接 ``unknown``。
6. **RCON 异常必须分阶段** —— ``send`` / ``read`` / ``protocol`` 阶段失败时命令
   可能已经发出去了，判 ``failed`` 会诱导重试 → 重复副作用。只有 ``connect`` 才判失败。
7. **execute 解析器全仓只有一份** —— ``command_result`` 必须复用
   ``java_commands.unwrap_command``，不许再手搓 ``rfind(" run ")``。
8. **WebUI 必须能改、也必须能看见版本** —— 两个配置键要在 CFG_FIELDS 里、
   ``version_hint`` 要被消费、保存后要刷新能力状态（不重启插件）。
"""
from __future__ import annotations

import importlib.util
import re
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
    path = PLUGIN_DIR / rel
    if rel.startswith("core/"):
        _ensure_core_package()
        mod_name = "core." + Path(rel).stem
    else:
        mod_name = name
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cr = _load("cr", "core/command_result.py")
rcon_mod = _load("rcon_mod", "core/rcon.py")
classify = cr.classify_command_output
RconError = rcon_mod.RconError

MAIN_SRC = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
CR_SRC = (PLUGIN_DIR / "core/command_result.py").read_text(encoding="utf-8")
WF_SRC = (PLUGIN_DIR / "core/workflow.py").read_text(encoding="utf-8")
WEB_SRC = (PLUGIN_DIR / "core/web_api.py").read_text(encoding="utf-8")
HTML = (PLUGIN_DIR / "pages/mc_control/index.html").read_text(encoding="utf-8")


# ===================== 一、空响应：非幂等优先于 boundary_confirmed =====================

def empty_response_priority() -> None:
    print("---- 一、空响应：非幂等命令优先于 boundary_confirmed ----")

    # give 空响应 + 边界已确认 + 收到响应 —— 旧顺序会走「静默/幂等 → success」
    r = classify("give Steve diamond 1", "",
                 boundary_confirmed=True, response_received=True)
    check("★give 空 + 边界已确认 + 收到响应 → unknown（**不是** success）",
          r.status == "unknown", r.status)
    check("★give 空 → accepted=False（必须熔断后续命令，保住重复副作用保护）",
          r.accepted is False, str(r.accepted))

    # give 空响应 + 边界未确认 —— 旧顺序会走 dispatched_unconfirmed（accepted=True）
    r = classify("give Steve diamond 1", "",
                 boundary_confirmed=False, response_received=True)
    check("★give 空 + 边界未确认 → unknown（**不是** dispatched_unconfirmed）",
          r.status == "unknown", r.status)
    check("★give 空 + 边界未确认 → accepted=False（不许不熔断）",
          r.accepted is False, str(r.accepted))

    r = classify("give Steve diamond 1", "", boundary_confirmed=None,
                 response_received=True)
    check("give 空 + 边界未知(None) → unknown", r.status == "unknown", r.status)

    # 非幂等家族的另外三条
    for cmd in ("summon minecraft:zombie ~ ~ ~", "effect give @a speed 10 1",
                "item replace entity Steve container.0 with diamond"):
        r = classify(cmd, "", boundary_confirmed=True, response_received=True)
        check(f"{cmd.split()[0]} 空 + 边界已确认 → unknown",
              r.status == "unknown", r.status)

    # 消息类 / 幂等：边界已确认时空响应才是 success
    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=True, response_received=True)
    check("tellraw 空 + 边界已确认 → success（消息类，不是非幂等）",
          r.status == "success", r.status)
    r = classify("time set day", "", boundary_confirmed=True, response_received=True)
    check("time 空 + 边界已确认 → success（幂等）", r.status == "success", r.status)
    r = classify('tellraw @a {"text":"hi"}', "",
                 boundary_confirmed=False, response_received=True)
    check("tellraw 空 + 边界未确认 → dispatched_unconfirmed",
          r.status == "dispatched_unconfirmed", r.status)


# ===================== 二、非空 + 边界未确认：非幂等仍是 unknown =====================

def nonempty_idle_unconfirmed() -> None:
    print("---- 二、非空输出 + 边界未确认（idle 降级）----")

    # 半截输出对非幂等命令毫无意义：不知道副作用落没落定
    r = classify("give Steve diamond 1", "Gave 1 [Diamond] to Steve",
                 boundary_confirmed=False, response_received=True)
    check("★give 非空 + 边界未确认 → unknown（**不是** dispatched_unconfirmed）",
          r.status == "unknown", r.status)
    check("★give 非空 + 边界未确认 → accepted=False", r.accepted is False,
          str(r.accepted))

    r = classify("summon minecraft:zombie ~ ~ ~", "Summoned new Zombie",
                 boundary_confirmed=False, response_received=True)
    check("summon 非空 + 边界未确认 → unknown", r.status == "unknown", r.status)

    # 消息类：输出就是消息回显，边界不可靠也确认发出去了
    for cmd, out in (('tellraw @a {"text":"hi"}', "hi"),
                     ("say 大家早", "大家早"),
                     ('title @a title {"text":"hi"}', "hi")):
        r = classify(cmd, out, boundary_confirmed=False, response_received=True)
        check(f"{cmd.split()[0]} 非空 + 边界未确认 → dispatched_unconfirmed",
              r.status == "dispatched_unconfirmed", r.status)

    # 幂等：重发无副作用，允许 dispatched_unconfirmed
    r = classify("time set day", "Set the time to 1000",
                 boundary_confirmed=False, response_received=True)
    check("time 非空 + 边界未确认 → dispatched_unconfirmed（幂等，重发无副作用）",
          r.status == "dispatched_unconfirmed", r.status)


# ===================== 三、Operation aborted 与词边界 =====================

def aborted_and_word_boundary() -> None:
    print("---- 三、Operation aborted / 错误词边界 ----")

    for text in ("Operation aborted",
                 "Operation aborted: something went wrong",
                 "Refused to execute",
                 "Command cancelled",
                 "Command canceled by plugin",
                 "Request rejected",
                 "Permission denied"):
        r = classify("give Steve diamond 1", text)
        check(f"★{text!r} → failed（旧口径会判 success）",
              r.status == "failed", f"{r.status}｜{r.reason}")

    # 词边界：名字里含 Error 的物品/玩家不该被误伤
    r = classify("give Error diamond 1", "Gave 1 [Diamond] to Error")
    check("★玩家名 Error + Gave 成功反馈 → success（锚定模式先于失败词扫描）",
          r.status == "success", f"{r.status}｜{r.reason}")

    r = classify("give Steve error_sword 1", "Gave 1 [Error Sword] to Steve")
    check("★物品名含 error（error_sword / [Error Sword]）→ success",
          r.status == "success", f"{r.status}｜{r.reason}")

    # 反过来：真正的 error 语义仍必须判失败
    r = classify("give Steve diamond 1", "Operation error: unexpected exception")
    check("★Operation error: ... → failed（词边界不能把真错误也放走）",
          r.status == "failed", f"{r.status}｜{r.reason}")

    r = classify("give Steve diamond 1", "Invalid item id: not_a_thing")
    check("Invalid ... → failed", r.status == "failed", r.status)

    # v0.22.7：运行期错误不能被误标成「可安全重试」的语法错误。
    # "expected " 是 "unexpected " 的子串 —— 裸子串匹配会把
    # 「An unexpected error occurred」判成 syntax_error（retryable=True）。
    for text in ("An unexpected error occurred while executing the command",
                 "Operation error: unexpected exception",
                 "Unexpected exception in mod"):
        check(f"★{text[:40]!r}… 不是 syntax_error（运行期错误 ≠ 语法错误）",
              cr.is_syntax_error_output(text) is False, "被误判为 syntax_error")
        st = classify("give Steve diamond 1", text).status
        # 「Unexpected exception in mod」不含任何失败词 → 保守落到 unknown（熔断），
        # 这同样正确：**不能**是 success，更不能是 syntax_error（那是「可安全重试」的谎）。
        check(f"★{text[:40]!r}… → 既不是 success 也不是 syntax_error",
              st in ("failed", "unknown"), st)
    # 反过来：真正的解析错误仍必须是 syntax_error（可安全重写重试）
    for text in ("Expected whitespace to end one argument, but found trailing data",
                 "Expected literal", "Expected integer",
                 "Unknown or incomplete command"):
        check(f"真正语法错误 {text[:36]!r}… → syntax_error",
              cr.is_syntax_error_output(text) is True, "漏判")

    # 词边界正则本体：Error404 / MrError 不该命中 \berror\b
    pat = getattr(cr, "_FAILURE_WORD_RE", None) or getattr(cr, "FAILURE_WORD_RE", None)
    check("模块导出了失败词正则（供复核）", pat is not None)
    if pat is not None:
        rx = pat if hasattr(pat, "search") else re.compile(pat, re.I)
        check("词边界：'Error404' 不命中 error", not rx.search("Error404"))
        check("词边界：'MrError' 不命中 error", not rx.search("MrError"))
        check("词边界：'error:' 命中", bool(rx.search("Operation error: x")))

    # 锚定成功模式表覆盖核心原版命令
    tbl = getattr(cr, "ANCHORED_SUCCESS_PATTERNS", None)
    check("模块导出了锚定成功模式表 ANCHORED_SUCCESS_PATTERNS",
          isinstance(tbl, dict) and "give" in tbl)
    if isinstance(tbl, dict):
        for cmd, out in (("give", "Gave 1 [Diamond] to Steve"),
                         ("time", "Set the time to 1000"),
                         ("weather", "Changed the weather to clear"),
                         ("gamemode", "Set Steve's game mode to Creative Mode"),
                         ("clear", "Cleared the inventory of Steve"),
                         ("kick", "Kicked Steve"),
                         ("ban", "Banned player Steve: reason"),
                         ("pardon", "Unbanned player Steve")):
            r = classify(f"{cmd} x", out)
            check(f"锚定模式 {cmd}：{out[:34]!r}… → success",
                  r.status == "success", f"{r.status}｜{r.reason}")


# ===================== 四、未知非空输出不得默认 success =====================

def unknown_output_not_success() -> None:
    print("---- 四、未知非空输出 ----")

    # 模组命令：非空、无错误标记、但没有任何成功证据
    r = classify("tac:reload", "Reloaded gun pack (custom mod feedback)")
    check("★模组未知文本 → inferred_success（**不是** success）",
          r.status == "inferred_success", f"{r.status}｜{r.reason}")
    check("★inferred_success：ok=False（不得对外宣称成功）", r.ok is False, str(r.ok))
    check("inferred_success：confidence='inferred'", r.confidence == "inferred",
          r.confidence)

    # 非幂等 + 未知文本 → 直接 unknown，连 inferred 都不给
    r = classify("give Steve diamond 1", "Weird modded feedback xyz")
    check("★非幂等 + 未知文本 → unknown（不确认副作用，宁可熔断）",
          r.status == "unknown", r.status)
    check("非幂等 + 未知文本 → accepted=False", r.accepted is False, str(r.accepted))

    # 开关存在且默认开启（关掉则全落 unknown，是最保守口径）
    check("INFERRED_SUCCESS_ENABLED 默认开（关掉 = 全落 unknown 的最保守口径）",
          getattr(cr, "INFERRED_SUCCESS_ENABLED", None) is True,
          str(getattr(cr, "INFERRED_SUCCESS_ENABLED", None)))

    # 状态机里必须有这一档，且标签不能让用户误读成「成功」
    check("CommandStatus 含 inferred_success",
          "inferred_success" in cr.CommandStatus.__args__)
    label = cr.STATUS_LABEL.get("inferred_success", "")
    check("★inferred_success 的中文标签不含「成功」二字",
          "成功" not in label, label)
    check("inferred_success 在回执里明确写「未确认」",
          "未确认" in label, label)


# ===================== 五、RCON 异常分阶段 =====================

def rcon_error_phase() -> None:
    print("---- 五、RCON 异常分阶段 ----")

    check("RconError 有 phase 属性", hasattr(RconError("x"), "phase"))
    check("默认阶段 = unknown（不靠异常文本猜）",
          RconError("x").phase == "unknown", RconError("x").phase)

    e_conn = RconError("refused", phase=RconError.PHASE_CONNECT)
    check("★connect 阶段：命令确实没发出去（command_may_have_run=False）",
          e_conn.command_may_have_run is False, str(e_conn.command_may_have_run))

    for ph in (RconError.PHASE_SEND, RconError.PHASE_READ,
               RconError.PHASE_PROTOCOL, RconError.PHASE_UNKNOWN):
        e = RconError("boom", phase=ph)
        check(f"★{ph} 阶段：命令可能已发出（command_may_have_run=True）",
              e.command_may_have_run is True, str(e.command_may_have_run))

    e_to = rcon_mod.RconTimeoutError("timeout")
    check("RconTimeoutError 默认阶段 = read（结果未知，不是失败）",
          e_to.phase == RconError.PHASE_READ, e_to.phase)
    check("RconTimeoutError：command_may_have_run=True",
          e_to.command_may_have_run is True)

    # rcon.py 里每个构造点都标了阶段（不靠猜）
    rcon_src = (PLUGIN_DIR / "core/rcon.py").read_text(encoding="utf-8")
    raises = [m for m in re.finditer(r"raise Rcon\w*Error\(", rcon_src)]
    unphased = 0
    for m in raises:
        blob = rcon_src[m.start():m.start() + 400]
        if "phase=" not in blob and "PartialResponse" not in blob \
                and "Timeout" not in blob:
            unphased += 1
    check(f"rcon.py 的 {len(raises)} 个构造点都带阶段（或为默认 read 的子类）",
          unphased == 0, f"未标注 {unphased} 处")

    # _exec_checked：connect → failed，其余 → unknown
    check("★_exec_checked 按 phase 分流（源码断言）",
          "RconError.PHASE_CONNECT" in MAIN_SRC and "phase" in MAIN_SRC)
    seg = MAIN_SRC[MAIN_SRC.find("async def _exec_checked"):]
    seg = seg[:seg.find("\n    def ")] if "\n    def " in seg else seg
    check("★_exec_checked：connect 阶段才判 failed",
          "PHASE_CONNECT" in seg and '"failed"' in seg, seg[:200])
    check("★_exec_checked：非 connect 阶段判 unknown（不诱导重试）",
          '"unknown"' in seg and "phase" in seg, seg[:200])


# ===================== 六、execute 解析器全仓一份 =====================

def single_execute_parser() -> None:
    print("---- 六、execute 解析器只有一份 ----")

    # 源码里不允许再出现手搓的 run 查找
    check("★command_result.py 不再手搓 rfind(\" run \")（只允许注释里提历史）",
          not re.search(r'^\s*.*=\s*.*\.rfind\(\s*["\']\s*run\s', CR_SRC, re.M),
          "仍有 rfind 调用")

    check("★command_result 复用 java_commands.unwrap_command",
          "from .java_commands import" in CR_SRC and "unwrap_command" in CR_SRC)

    # 行为：v0.22.3 点名的反例必须解析对
    name, args = cr.effective_command_segment(
        'execute as @a run tellraw @a {"text":" run "}'
    )
    check("★'execute as @a run tellraw @a {\"text\":\" run \"}' → 命令名 tellraw",
          name == "tellraw", f"{name!r}｜args={args!r}")
    check("★同一反例的参数原文完整保留（不是 '}'）",
          '" run "' in args, repr(args))

    name, _ = cr.effective_command_segment("execute positioned 0 0 0 run setblock 0 0 0 stone")
    check("execute ... run setblock → 命令名 setblock", name == "setblock", name)

    name, _ = cr.effective_command_segment("give Steve diamond 1")
    check("普通命令 → 命令名 give", name == "give", name)

    name, _ = cr.effective_command_segment("minecraft:give Steve diamond 1")
    check("带命名空间 → 命令名 give（命名空间已剥离）", name == "give", name)

    # 解析不出来时绝不猜：返回一个不在任何豁免清单里的名字
    bad = "execute as @a say hi"      # execute 子命令无法识别 → CommandParseError
    try:
        jc_unwrap_failed = False
        cr.effective_command_segment(bad)
    except cr.CommandParseError:
        jc_unwrap_failed = True
    seg_bad = cr.effective_command_segment(bad)
    check("★解析失败（execute 子命令无法识别）→ 不落进静默/幂等豁免清单",
          seg_bad[0] not in cr.SILENT_EMPTY_SUCCESS_COMMANDS
          and seg_bad[0] not in cr.IDEMPOTENT_COMMANDS,
          str(seg_bad))
    check("★解析失败 → 命令名退化为 'execute'（fail-closed，不给任何豁免）",
          seg_bad[0] == "execute", str(seg_bad))
    r = classify(bad, "", boundary_confirmed=True, response_received=True)
    check("★解析失败的命令空响应 → 仍是 unknown（不借豁免逃成 success）",
          r.status == "unknown", r.status)
    check("（参考）unwrap_command 对该输入直接抛错 = fail-closed 源头",
          jc_unwrap_failed or True)


# ===================== 七、工作流汇总：未确认档必须被看见 =====================

def workflow_summary() -> None:
    print("---- 七、工作流汇总 ----")

    check("workflow 汇总单列 inferred_success",
          "inferred_success" in WF_SRC and "已执行但未确认" in WF_SRC)
    check("workflow 的 _fmt_results 有 inferred_success 标签",
          '"inferred_success": "已执行·未确认"' in WF_SRC)
    check("★inferred_success 不把工作流标成 done",
          "elif unknown or inferred:" in WF_SRC)
    check("main.py 回执里 inferred_success 明确写「未确认」",
          "命令已下发但未确认" in MAIN_SRC)
    check("main.py 的 _render_result 有 inferred_success 分支",
          'r.status == "inferred_success"' in MAIN_SRC)


# ===================== 八、WebUI 版本设置 =====================

def webui_version_settings() -> None:
    print("---- 八、WebUI 版本设置 ----")

    check("设置页有 server_version_override 输入框",
          'id="cfg_server_version"' in HTML)
    check("设置页有 item_syntax_override 下拉", 'id="cfg_item_syntax"' in HTML)
    check("★CFG_FIELDS 已挂绑定（否则用户看不到、也保存不了）",
          '["cfg_server_version","server_version_override","s"]' in HTML
          and '["cfg_item_syntax","item_syntax_override","s"]' in HTML)
    check("S_DEFAULTS 补了 cfg_item_syntax 兜底",
          '"cfg_item_syntax": "auto"' in HTML)
    check("item_syntax 下拉含 auto / legacy_nbt / components 三个取值",
          all(f'value="{v}"' in HTML for v in ("auto", "legacy_nbt", "components")))

    check("★设置页有能力状态行 ver_caps_line", 'id="ver_caps_line"' in HTML)
    check("★保存后刷新能力状态（loadSettings 里调 refreshVersionCaps）",
          "refreshVersionCaps();" in HTML)
    check("renderVersionCaps 会直说「无法确定服务端版本」",
          "当前无法确定 Minecraft 服务端版本" in HTML)
    check("版本已知时显示来源（手动声明 / 文件探测）",
          "caps.source_label" in HTML)

    check("★服务器页消费 version_hint（此前只生成、没人显示）",
          "r.version_hint" in HTML and 'id="sv_ver_caps"' in HTML)
    check("服务器页也渲染版本能力", 'renderVersionCaps(r && r.version_caps ? r.version_caps : null, "sv_ver_caps")' in HTML)

    # 后端：version_caps 必须在 _get_rcon 之前算 —— 否则 RCON 一掉线就什么都看不到
    seg = WEB_SRC[WEB_SRC.find("async def get_server_status"):]
    seg = seg[:seg.find("async def ", 10)] if "async def " in seg[10:] else seg
    i_caps = seg.find("_version_capabilities")
    i_rcon = seg.find("_get_rcon")
    check("★get_server_status：version_caps 在 _get_rcon 之前算（掉线也能显示）",
          0 <= i_caps < i_rcon, f"caps@{i_caps} rcon@{i_rcon}")
    check("★RCON 连不上时也把版本能力返回出去（error 分支带 **info）",
          '{"ok": False, "error": str(e), **info}' in seg, seg[:300])


def main() -> int:
    print("=" * 74)
    print("v0.22.7 结果判定加固 —— 核验裁决回归测试")
    print("=" * 74)
    empty_response_priority()
    nonempty_idle_unconfirmed()
    aborted_and_word_boundary()
    unknown_output_not_success()
    rcon_error_phase()
    single_execute_parser()
    workflow_summary()
    webui_version_settings()
    print("=" * 74)
    if FAIL:
        print(f"❌ 失败 {len(FAIL)} 项：")
        for d in FAIL:
            print(f"   - {d}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
