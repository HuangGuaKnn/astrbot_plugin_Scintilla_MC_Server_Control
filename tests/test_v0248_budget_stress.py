# -*- coding: utf-8 -*-
"""发送预算的**压力**判据（v0.24.8 · GPT 第三轮复核建议项 B8）。

GPT 的原话：三条闸门的范围（64 MiB / 10000 条）作为管理员配置可以接受，
但「仍是较高的资源上限，建议补充长度和资源压力测试」。本支只回答两件事：

1. **到顶就停，且停得住**：一万条小命令能在可接受的时间里逐条记账完，
   第 10001 条立刻拒发；64 MiB 的单条命令**不炸内存、不抛异常**，而是
   结构化拒发 —— 并且一旦用尽，后面**每一条**都不再放行（fail-closed 不是
   「只拒这一条」）。
2. **边界是精确的**：恰好等于上限的放行，多一个字节 / 多一条就拒。
   这类判据最容易被 `>=` / `>` 写反，只有边界值能抓住。

刻意不断言「几毫秒」这类机器相关数字 —— 只给一个宽松上限（秒级），
超出即视为退化（例如把 O(1) 记账改成了每条都全量重算字节数）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()   # 用 AstrBot 自带解释器跑；core.workflow 依赖 astrbot 运行时

from core.workflow import SendBudget  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        _FAILED.append(name)
        print(f"[FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def part_ten_thousand() -> None:
    print("\n-- ① 一万条命令（配置允许的最大条数）--")
    b = SendBudget(max_batch=10000, max_commands=10000, max_bytes=64 * 1024 * 1024)
    t0 = time.perf_counter()
    rejects = []
    for i in range(10000):
        why = b.take(f"say {i}")
        if why:
            rejects.append((i, why))
    cost = time.perf_counter() - t0
    check("前 10000 条全部放行（一条不误拒）", not rejects, str(rejects[:1]))
    check(f"10000 条记账耗时在秒级以内（实测 {cost:.3f}s）", cost < 5.0, f"{cost:.3f}s")

    why = b.take("say 10001")
    check("第 10001 条被拒", bool(why), why)
    check("拒发原因点明「累计条数上限」", "上限" in why and "10000" in why, why)
    check("被拒的那一条**不占额度**（用量仍是 10000）",
          b.used_commands == 10000, str(b.used_commands))
    why2 = b.take("say 10002")
    check("★用尽之后后续每一条都拒（fail-closed，不是只拒那一条）",
          bool(why2) and b.used_commands == 10000, why2)


def part_sixty_four_mib() -> None:
    print("\n-- ② 64 MiB 单条命令（配置允许的最大字节数）--")
    cap = 64 * 1024 * 1024
    b = SendBudget(max_batch=10, max_commands=10, max_bytes=cap)
    big = "a" * (cap + 1)
    t0 = time.perf_counter()
    why = b.take(big)
    cost = time.perf_counter() - t0
    check(f"64 MiB + 1 字节的命令被结构化拒发（不抛异常，实测 {cost:.2f}s）", bool(why), why)
    check("拒发原因点明「字节上限」", "字节" in why, why)
    check(f"拒发过程没有把时间拖长（实测 {cost:.2f}s）", cost < 10.0, f"{cost:.2f}s")
    del big

    why2 = b.take("say hi")
    check("★字节用尽后，小命令也不再放行（整任务停发）", bool(why2), why2)

    # 边界：恰好等于上限放行，多 1 字节拒绝
    exact = "b" * (cap - len("say "))
    b2 = SendBudget(max_batch=10, max_commands=10, max_bytes=cap)
    cmd = "say " + exact
    check("恰好等于字节上限的那条放行", b2.take(cmd) == "",
          f"len={len(cmd.encode('utf-8'))}")
    b3 = SendBudget(max_batch=10, max_commands=10, max_bytes=cap)
    check("比上限多 1 字节的那条被拒", bool(b3.take(cmd + "x")))
    del exact, cmd


def part_batch_boundary() -> None:
    print("\n-- ③ 单批边界 --")
    b = SendBudget(max_batch=40, max_commands=120, max_bytes=256 * 1024)
    check("40 条整批放行", b.check_batch([f"say {i}" for i in range(40)]) == "")
    check("41 条整批拒绝（不是截断到 40 条发）",
          bool(b.check_batch([f"say {i}" for i in range(41)])))
    many = b.check_batch([f"say {i}" for i in range(10000)])
    check("★一次 10000 条：整批拒绝，提示里给出实际条数与上限",
          "10000" in many and "40" in many, many[:160])
    check("空白命令不计数（`say` 与 `/say` 都按一条算，空串不算）",
          b.check_batch(["", "   ", "/say a", "say b"]) == "")


def main() -> int:
    print("=" * 68)
    print("发送预算压力判据（v0.24.8 · B8）")
    print("=" * 68)
    part_ten_thousand()
    part_sixty_four_mib()
    part_batch_boundary()
    print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        print("失败项：")
        for n in _FAILED:
            print("  -", n)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
