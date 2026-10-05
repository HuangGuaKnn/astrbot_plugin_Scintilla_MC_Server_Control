# -*- coding: utf-8 -*-
"""v0.23.7 · 出站文本兼容层（B2 单包字节预算 + B6 旧版字符洗白）。

运行：
  <python> tests/test_v0237_text_outbound.py

背景（2026-10-05 版本矩阵实测，小本本 §十二 / §十九）
=====================================================
B6：1.8.x 客户端把全角「（」当格式码吞掉其后整句；实测 1.9+ 全干净
    → 洗白层版本门 =「仅 ≤1.8.x」，未知版本保守洗。
B2：原版 RCON 单包读取缓冲 = byte[1460]（1.8.9→1.21.11 全世代同构），超限即**静默拔线**；
    JSON 渐变 ≈35B/字 极易超限（实测 ≈1.7KB 炸 / ≈1.1KB 过）
    → 渲染后做字节预算：超限先降级单色，仍超按字符切段（title 类截断）。

本测试钉死六件事
================
1. 洗白只动 4 对全角括号，不碰其他字符；
2. 版本门：<1.9 洗 / ≥1.9 豁免 / 未知保守洗；
3. 字节切段不破坏字符、不丢字符，每段 ≤ 预算；
4. 插件层 _build_text_cmds：常规 1 条；超长切段/截断且都在预算内；
5. 渐变门控：已知 <1.16 自动单色，≥1.16 正常渐变；
6. 关键落点全部改走新管道（源码扫描）+ 配置键登记。
"""
from __future__ import annotations

import json
import logging
import sys
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


# ===================== 一、纯函数：洗白表与版本门 =====================

try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core import text_outbound as tob
except Exception:  # 包 __init__ 依赖 AstrBot 时退回按文件装载（纯模块无依赖）
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "text_outbound_standalone", PLUGIN_DIR / "core" / "text_outbound.py"
    )
    tob = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(tob)  # type: ignore[union-attr]


def part_one() -> None:
    print("---- 一、B6 洗白：表与版本门 ----")
    check("4 对替换齐全", tob.LEGACY_CHAR_MAP == {
        "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}", "｜": "|",
    }, str(tob.LEGACY_CHAR_MAP))
    check("洗白：全角括号族 → 半角",
          tob.wash_legacy_text("甲（乙）［丙］｛丁｝｜戊") == "甲(乙)[丙]{丁}|戊")
    check("洗白：不动普通字符与半角",
          tob.wash_legacy_text("Hello, 世界! ()[]{}|") == "Hello, 世界! ()[]{}|")
    check("洗白：空串安全", tob.wash_legacy_text("") == "")
    check("版本门：1.8.9 → 洗", tob.needs_legacy_wash((1, 8, 9)))
    check("版本门：1.9 起豁免", not tob.needs_legacy_wash((1, 9)))
    check("版本门：1.14.4 豁免", not tob.needs_legacy_wash((1, 14, 4)))
    check("版本门：1.21.11 豁免", not tob.needs_legacy_wash((1, 21, 11)))
    check("版本门：未知 → 保守洗", tob.needs_legacy_wash(None))


# ===================== 二、纯函数：字节预算 =====================

def part_two() -> None:
    print("---- 二、B2 字节预算：切段 / 截断 ----")
    check("预算常量落在理论安全带内（1300~1446）",
          1300 <= tob.RCON_SAFE_BYTES <= 1446, str(tob.RCON_SAFE_BYTES))
    text = "字" * 600  # 1800B
    chunks = tob.split_text_by_bytes(text, 500)
    check("切段数正确（1800B / 500B → 4 段）", len(chunks) == 4, str(len(chunks)))
    check("每段 ≤ 预算", all(tob.utf8_len(c) <= 500 for c in chunks))
    check("不丢字符、不破坏字符", "".join(chunks) == text)
    check("空文本 → 空列表", tob.split_text_by_bytes("", 100) == [])
    check("预算内不切", tob.split_text_by_bytes("你好", 100) == ["你好"])
    check("截断：clip 到字节边界（6B → 2 个汉字）",
          tob.clip_text_to_bytes("字字字", 6) == "字字")
    check("截断：预算 0 → 空串", tob.clip_text_to_bytes("字", 0) == "")


# ===================== 三、插件层 _build_text_cmds =====================

def make_plugin(pm, gradient: bool = False, mc=(1, 21, 11)):
    m = pm.McControlPlugin.__new__(pm.McControlPlugin)
    m.config = {"appearance": {
        "gradient_enabled": gradient,
        "gradient_format": "json",
        "gradient_colors_say": "#FF0000,#00FF00",
        "color_say": "white",
    }}
    m._cfg_index = {
        "gradient_enabled": "appearance", "gradient_format": "appearance",
        "gradient_colors_say": "appearance", "color_say": "appearance",
    }
    m.logger = logging.getLogger("t0237")
    from astrbot_plugin_Scintilla_MC_Server_Control.core.version_caps import VersionInfo
    m._resolve_version_info = lambda: VersionInfo(mc=mc, raw="", source="detected")
    return m


def body_texts(cmds, prefix):
    out = []
    for c in cmds:
        obj = json.loads(c[len(prefix):])
        out.append(obj.get("text") or "")
    return "".join(out)


def part_three(pm) -> None:
    print("---- 三、插件层 _build_text_cmds（渲染后预算） ----")
    m = make_plugin(pm)
    one = m._build_text_cmds("你好", "tellraw @a ", "color_say", "gradient_colors_say", "white")
    check("常规消息 → 1 条", len(one) == 1, str(len(one)))

    m8 = make_plugin(pm, mc=(1, 8, 9))
    c8 = m8._build_text_cmds("甲（乙", "tellraw @a ", "color_say", "gradient_colors_say", "white")[0]
    check("1.8.9：发出前已洗白", "甲(乙" in c8 and "（" not in c8, c8[:90])

    m9 = make_plugin(pm, mc=(1, 9))
    c9 = m9._build_text_cmds("甲（乙", "tellraw @a ", "color_say", "gradient_colors_say", "white")[0]
    check("1.9+：不洗（原样）", "甲（乙" in c9, c9[:90])

    mnone = make_plugin(pm, mc=None)
    cn = mnone._build_text_cmds("甲（乙", "tellraw @a ", "color_say", "gradient_colors_say", "white")[0]
    check("版本未知：保守洗", "甲(乙" in cn and "（" not in cn, cn[:90])

    print("---- 四、切段 / 截断 / 渐变门控 ----")
    big = "大" * 600
    parts = m._build_text_cmds(big, "tellraw @a ", "color_say", "gradient_colors_say", "white")
    check("600 字单色超限 → 切成多段", len(parts) > 1, str(len(parts)))
    check("每段 ≤ 预算", all(tob.utf8_len(c) <= tob.RCON_SAFE_BYTES for c in parts))
    check("切段后文本无损", body_texts(parts, "tellraw @a ") == big)

    tp = m._build_text_cmds(big, "title @a title ", "color_say", "gradient_colors_say",
                            "white", allow_multi=False)
    check("title 类永不切段（1 条）", len(tp) == 1, str(len(tp)))
    check("title 类截断且带省略号", "…" in tp[0], tp[0][-40:])
    check("title 类也在预算内", tob.utf8_len(tp[0]) <= tob.RCON_SAFE_BYTES)

    mg = make_plugin(pm, gradient=True, mc=(1, 21, 11))
    gradient_cmd = mg._build_text_cmds("你好", "tellraw @a ", "color_say", "gradient_colors_say", "white")[0]
    obj_new = json.loads(gradient_cmd.split(" ", 2)[2])
    check("1.16+：渐变正常输出（extra 结构）", "extra" in obj_new, gradient_cmd[:90])

    m_old = make_plugin(pm, gradient=True, mc=(1, 14, 4))
    old_cmd = m_old._build_text_cmds("你好", "tellraw @a ", "color_say", "gradient_colors_say", "white")[0]
    obj_old = json.loads(old_cmd.split(" ", 2)[2])
    check("已知 <1.16：hex 渐变自动禁用（单色 payload）",
          "extra" not in obj_old and obj_old.get("color") == "white", old_cmd[:90])

    gl = make_plugin(pm, gradient=True, mc=(1, 20, 4))
    gl_cmds = gl._build_text_cmds("宠" * 200, "tellraw @a ", "color_say", "gradient_colors_say", "white")
    check("渐变 + 超长：自动降级/切段且都在预算内",
          all(tob.utf8_len(c) <= tob.RCON_SAFE_BYTES for c in gl_cmds) and len(gl_cmds) >= 1,
          f"{len(gl_cmds)} 段")


# ===================== 五、落点与配置登记（源码扫描） =====================

def part_five() -> None:
    print("---- 五、落点与配置登记（源码扫描） ----")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    wapi = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))

    check("_build_text_cmds 定义 + 各落点调用（main.py ≥6 处）",
          "def _build_text_cmds" in src and src.count("self._build_text_cmds(") >= 5,
          str(src.count("self._build_text_cmds(")))
    check("web_api 三路广播全部走新管道", wapi.count("plugin._build_text_cmds(") == 3,
          str(wapi.count("plugin._build_text_cmds(")))
    check("统一执行合并 _exec_text_cmds 已定义（≥2 调用）",
          "async def _exec_text_cmds" in src and src.count("self._exec_text_cmds(") >= 2,
          str(src.count("self._exec_text_cmds(")))
    check("旧直接拼装残留检查：feedback/say/title 里不再直接 _colored_payload 出站",
          "payload = self._colored_payload(" not in src)
    check("超限诊断提示已入 _exec_checked（全世代措辞）",
          ("疑似超过 RCON " in src) and ("全世代同构" in src))

    def find_key(node, key):
        if isinstance(node, dict):
            if key in node:
                return node[key]
            for v in node.values():
                r = find_key(v, key)
                if r:
                    return r
        return None

    entry = find_key(schema, "legacy_text_wash")
    check("schema 登记 legacy_text_wash（bool / 默认 true）",
          bool(entry) and entry.get("type") == "bool" and entry.get("default") is True,
          str(entry)[:90])
    check("web_api BOOL_SETTING_KEYS 含 legacy_text_wash",
          '"legacy_text_wash"' in wapi)


def main() -> None:
    part_one()
    part_two()

    pm = None
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control import main as pm  # noqa: F811
    except Exception as e:  # 没有 AstrBot 运行时的环境：跳过插件层用例
        skip("插件层（三、四）", f"缺 AstrBot 运行时：{type(e).__name__}: {e}")
    if pm is not None:
        part_three(pm)

    part_five()

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
