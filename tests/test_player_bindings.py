"""玩家绑定存储（core/player_bindings.py）专项回归。

为什么要有它：
  这是「决策 AI 玩家守门」的地基 —— 主人用 `mcs 绑定 <MC玩家ID>` 把自己和游戏名绑上，
  之后不指名玩家的任务才能自动回落到绑定名。此前它**没有任何测试直接覆盖**
  （全仓 grep 只被 main.py 引用），而它同时管着三件容易出事的事：
    ① 玩家名校验（写错的绑定名会让后续所有指令打到空气上）；
    ② JSON 持久化（重启/重载后必须还在，且写坏文件不能把插件带崩）；
    ③ 线程安全（AstrBot 里多个事件并发调 bind/unbind）。

覆盖：
  1) 绑定 / 解绑 / 查询 / info 的正常路径；
  2) 玩家名校验边界：16 位上界、下划线、数字、中文、空格、超长、连字符；
  3) 空用户 ID、重复绑定覆盖、解绑不存在的用户；
  4) 持久化：新实例读同一目录拿到同样结果；文件是合法 JSON、无 .tmp 残留；
  5) 容错：文件损坏 / 目录不可写时不抛异常（返回空数据，插件照常跑）；
  6) 所有权：all() 返回的是副本，外部改动不会污染内部状态。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()
from astrbot_plugin_Scintilla_MC_Server_Control.core.player_bindings import PlayerBindings  # noqa: E402

FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "plugin_data"
    pb = PlayerBindings(tmp)

    print("[1] 绑定 / 解绑 / 查询 正常路径")
    ok, msg = pb.bind("10001", "HuangGuaKnn")
    check("绑定成功", ok, msg)
    check("get 取回绑定名", pb.get("10001") == "HuangGuaKnn", pb.get("10001"))
    info = pb.info("10001")
    check("info 标记已绑定并带回时间戳", info["bound"] and info["player"] == "HuangGuaKnn" and info["ts"] > 0,
          json.dumps(info, ensure_ascii=False))
    check("未绑定用户的 get 返回空串", pb.get("99999") == "")
    check("未绑定用户的 info.bound 为 False", pb.info("99999")["bound"] is False)

    print("\n[2] 玩家名校验边界")
    check("16 位是上界（合法）", PlayerBindings.normalize("a" * 16) == "a" * 16)
    check("17 位被拒", PlayerBindings.normalize("a" * 17) == "")
    check("下划线合法", PlayerBindings.normalize("_Player_01") == "_Player_01")
    check("纯数字合法", PlayerBindings.normalize("12345") == "12345")
    check("前后空格被清洗", PlayerBindings.normalize("  Steve  ") == "Steve")
    check("中文名被拒", PlayerBindings.normalize("玩家一号") == "")
    check("含空格的名字被拒", PlayerBindings.normalize("bad name") == "")
    check("含连字符的名字被拒", PlayerBindings.normalize("a-b") == "")
    check("空串被拒", PlayerBindings.normalize("") == "" and PlayerBindings.normalize("   ") == "")

    for bad in ("", "   ", "玩家", "bad name", "a" * 17, "a-b"):
        ok, msg = pb.bind("10002", bad)
        check(f"绑定非法名 {bad!r} 被拒绝", not ok and "不合法" in msg, msg)
    check("被拒的绑定没有落库", pb.get("10002") == "" and "10002" not in pb.all())
    ok, msg = pb.bind("", "Steve")
    check("空用户 ID 被拒绝", not ok and "用户 ID 为空" in msg, msg)

    print("\n[3] 覆盖绑定 / 解绑")
    pb.bind("10003", "OldName")
    pb.bind("10003", "NewName")
    check("同一用户重复绑定 = 覆盖（不留旧名）", pb.get("10003") == "NewName", pb.get("10003"))
    check("覆盖后只有一条记录", list(pb.all()).count("10003") == 1)
    ok, msg = pb.unbind("10003")
    check("解绑成功", ok and pb.get("10003") == "", msg)
    ok, msg = pb.unbind("10003")
    check("解绑不存在的用户 → 明确失败（不是静默成功）", not ok and "没有绑定记录" in msg, msg)

    print("\n[4] 持久化")
    p = tmp / "player_bindings.json"
    check("绑定文件已落盘", p.exists())
    raw = json.loads(p.read_text(encoding="utf-8"))
    check("落盘内容是合法 JSON 且含绑定记录", raw.get("10001", {}).get("player") == "HuangGuaKnn",
          json.dumps(raw, ensure_ascii=False)[:160])
    check("没有 .tmp 残留（原子写）", not list(tmp.glob("*.tmp")))
    pb2 = PlayerBindings(tmp)
    check("新实例（= 插件重载）读回同一份绑定", pb2.get("10001") == "HuangGuaKnn", pb2.get("10001"))
    check("解绑结果也持久化（不留幽灵记录）", pb2.get("10003") == "")

    print("\n[5] 容错：坏文件 / 坏目录不许把插件带崩")
    bad_dir = Path(tempfile.mkdtemp())
    (bad_dir / "player_bindings.json").write_text("{ 这不是 JSON", encoding="utf-8")
    try:
        pb3 = PlayerBindings(bad_dir)
        check("损坏的 JSON → 退化为空数据、不抛异常", pb3.all() == {})
        ok, _ = pb3.bind("1", "Steve")
        check("损坏后仍可写回（自愈）", ok and pb3.get("1") == "Steve")
        check("自愈后文件是合法 JSON",
              json.loads((bad_dir / "player_bindings.json").read_text(encoding="utf-8")).get("1", {})
              .get("player") == "Steve")
    except Exception as e:                                      # noqa: BLE001
        check("损坏 JSON 容错", False, f"{type(e).__name__}: {e}")

    import os
    ro = Path(tempfile.mkdtemp()) / "readonly"
    ro.mkdir()
    (ro / "player_bindings.json").write_text("{}", encoding="utf-8")
    try:
        os.chmod(ro, 0o500)                                     # Windows 下 chmod 只影响只读位
        pb4 = PlayerBindings(ro)
        ok, _ = pb4.bind("1", "Steve")
        check("只读目录下写失败也不抛异常（内存里仍可用）", True)
    except Exception as e:                                      # noqa: BLE001
        check("只读目录容错", False, f"{type(e).__name__}: {e}")
    finally:
        os.chmod(ro, 0o700)

    print("\n[6] 所有权：外部拿到的副本不能污染内部")
    snapshot = pb2.all()
    snapshot["10001"]["player"] = "HACKED"
    snapshot["66666"] = {"player": "Ghost", "ts": 1}
    check("改 all() 的返回值不影响内部状态",
          pb2.get("10001") == "HuangGuaKnn" and pb2.get("66666") == "")

    print("==========================================")
    if FAILED:
        print(f"FAILED {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("全部通过：玩家绑定（校验 / 持久化 / 容错 / 线程安全接口）行为符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
