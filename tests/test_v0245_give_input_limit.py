"""v0.24.5 回归：mc_give_item 的命令输入也必须有统一长度上限。

``mc_execute_command`` 和 ``mc_broadcast`` 已有输入限长，但发物品工具此前
可以把超长 ``item`` / ``player`` 直接拼进 RCON 报文。这个用例不依赖 AstrBot
运行时，解析源码并固定两道闸门：参数粗筛在连接前，最终命令在实际发送前复核。

运行：
    python tests\\test_v0245_give_input_limit.py
"""
from __future__ import annotations

# 直接手跑时的控制台编码兜底（见 tests/_paths.py 的 ensure_utf8_console）：
# 不 import 它的话，输出里只要出现一个非 GBK 字符（比如 ✅），Windows 干净 shell 下
# `print` 就会抛 UnicodeEncodeError —— 结果是「全部通过、退出码却是 1」，看着像坏了。
try:
    import _paths  # noqa: F401
except Exception:  # noqa: BLE001 —— 兜底失败也不能拦住用例本身
    pass

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _calls(fn: ast.FunctionDef, name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == name
    ]


def _too_long(value: str | None, limit: int, label: str) -> str | None:
    n = len(value or "")
    if n <= limit:
        return None
    return f"{label}过长（{n} 字符，上限 {limit}）。请缩短后重试"


def main() -> int:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
             and n.name == "mc_give_item"]
    assert len(funcs) == 1, "mc_give_item 应只有一个实现"
    fn = funcs[0]
    too_long_calls = _calls(fn, "_too_long")
    get_rcon_calls = _calls(fn, "_get_rcon")
    assert len(too_long_calls) >= 2, "应有原始参数和最终命令两道限长检查"
    assert get_rcon_calls, "mc_give_item 应获取 RCON 连接"
    # 第一处参数粗筛必须发生在第一次获取连接之前，超长输入不应触碰网络。
    assert min(n.lineno for n in too_long_calls) < min(n.lineno for n in get_rcon_calls)

    # 最终命令复核要位于命令构造之后，且仍早于 _get_rcon。
    final_check = max(n.lineno for n in too_long_calls)
    assert final_check < min(n.lineno for n in get_rcon_calls)

    limit = 8000
    assert _too_long("give Steve diamond 1", limit, "命令") is None
    oversized = _too_long("give Steve " + "x" * limit, limit, "命令")
    assert oversized and "8000" in oversized

    print("[ok] mc_give_item 在 RCON 连接前执行参数与最终命令限长检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
