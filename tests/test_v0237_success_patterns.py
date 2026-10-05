# -*- coding: utf-8 -*-
"""v0.23.7 · B3：成功识别器补 pattern（data get / attribute get 只读回执）。

背景（2026-10-05 全版本矩阵巡礼实测）
======================================
- 1.13.2：`data get entity …` 成功（RCON 回 `… has the following entity data: …`），
  但锚定 pattern 表里没有这条 → 输出落到「已执行·未确认」（inferred_success），
  回执文案误导（其实命令已成功）。
- 1.21.1：`attribute … get` 同理（`Value of attribute … for entity … is …`）。

修复：把两类**只读回执**补进 ANCHORED_SUCCESS_PATTERNS（按命令名锚定——
命中 = 正面证据 → success/explicit；且命中后跳过失败词扫描，
NBT 载荷里的普通单词不会把真成功翻盘）。

本测试钉死：
1. data：实体 / 方块 / 储物箱 / 命名空间前缀 四种回执 → success/explicit；
2. attribute：value / base value 两种回执 → success/explicit；
3. 反面：真失败仍是 failed、无关输出不因新 pattern 变成功、失败文案不误判成功；
4. 锚定语义：NBT 载荷里出现失败词（自定义名 Error）不翻盘；
5. 既有锚定回归：give 的 `Gave … to Error` 仍 success（v0.22.7 语义不破坏）；
6. 源码登记。
"""
from __future__ import annotations

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


def main() -> None:
    try:
        from astrbot_plugin_Scintilla_MC_Server_Control.core import command_result as cr
    except Exception as e:
        skip("全部（识别器行为）", f"缺 AstrBot 运行时：{type(e).__name__}: {e}")
        _source_scan()
        _summary()
        return

    R = cr.classify_command_output

    def cls(cmd: str, out: str):
        return R(cmd, out, boundary_confirmed=True, response_received=True)

    print("---- 一、data get 回执 ----")
    r = cls("data get entity @p Health", "Steve has the following entity data: 20.0f")
    check("★实体回执 → success/explicit",
          r.status == "success" and r.confidence == "explicit",
          f"{r.status}/{r.confidence}/{r.reason}")
    r = cls("data get block 12 64 -30",
            'The block at 12, 64, -30 has the following block data: {id: "minecraft:stone"}')
    check("★方块回执 → success/explicit",
          r.status == "success" and r.confidence == "explicit",
          f"{r.status}/{r.confidence}")
    r = cls("data get storage minecraft:demo foo",
            "The storage minecraft:demo has the following storage data: 42")
    check("储物箱回执 → success", r.status == "success", f"{r.status}")
    r = cls("minecraft:data get entity @p Health",
            "Steve has the following entity data: 20.0f")
    check("命名空间前缀命令同样命中", r.status == "success", f"{r.status}")

    print("---- 二、attribute get 回执 ----")
    r = cls("attribute Steve minecraft:generic.max_health get",
            "Value of attribute minecraft:generic.max_health for entity Steve is 20.0")
    check("★value 回执 → success/explicit",
          r.status == "success" and r.confidence == "explicit",
          f"{r.status}/{r.confidence}")
    r = cls("attribute Steve minecraft:generic.max_health base get",
            "Base value of attribute minecraft:generic.max_health for entity Steve is 20.0")
    check("base value 回执 → success", r.status == "success", f"{r.status}")

    print("---- 三、反面（不许误判成功） ----")
    r = cls("data get entity @p Health", "No entity was found")
    check("data 真失败 → failed", r.status == "failed", f"{r.status}/{r.reason}")
    r = cls("data get entity @p Health", "{}")
    check("无证据输出 → 不得是 success", r.status != "success", f"{r.status}")
    r = cls("attribute Steve minecraft:generic.none get",
            "No such attribute minecraft:generic.none")
    check("attribute 失败 → 不得是 success", r.status != "success", f"{r.status}")

    print("---- 四、锚定语义（正面证据跳过失败词扫描） ----")
    r = cls("data get entity @p",
            "Steve has the following entity data: {CustomName: 'Error'}")
    check("★NBT 载荷含 Error → 仍 success（不翻盘）",
          r.status == "success", f"{r.status}/{r.reason}")

    print("---- 五、既有锚定回归 ----")
    r = cls("give Steve diamond 1", "Gave 1 [Diamond] to Error")
    check("give：Gave … to Error → 仍 success（v0.22.7 语义）",
          r.status == "success", f"{r.status}")
    r = cls("time set day", "Set the time to 1000")
    check("time 锚定不受影响", r.status == "success", f"{r.status}")

    _source_scan()
    _summary()


def _source_scan() -> None:
    print("---- 六、源码登记 ----")
    src = (PLUGIN_DIR / "core" / "command_result.py").read_text(encoding="utf-8")
    check("data 锚定 pattern 已登记",
          "has the following (?:entity|block|storage) data" in src)
    check("attribute 锚定 pattern 已登记",
          "value of attribute .+ is" in src)
    check("v0.23.7（B3）来历注释在案", "v0.23.7（B3）" in src)


def _summary() -> None:
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
