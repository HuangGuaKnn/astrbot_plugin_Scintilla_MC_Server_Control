"""v0.23.5 回归：输入与日志读取都必须有上限（外部审查 P1-⑤）。

两件事，来自同一句话「输入 / 资源缺统一上限」：

  A. 日志监听：`core/log_watcher.py` 原先 `data = f.read()` **无上限** ——
     整合包把日志刷屏或甩出崩溃堆栈时，一次轮询就把整段积压读进内存。
     **而且**：改成带上限之后会冒出一个新风险 —— 若「一次读满上限却一个换行
     都没有」，原来的 `if cut == -1: return` 会让 `_pos` **原地不动 = 永久卡死**。
     这里的测试把这两面都钉住。

  B. LLM 工具入参：`mc_execute_command.command` / `mc_broadcast.message` 此前
     没有任何长度限制，LLM 偶发把整段文本塞进来就会原样拼进 RCON 报文。

运行：
  python tests\\test_v0235_input_and_log_limits.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.log_watcher import (  # noqa: E402
    MAX_LAG_BYTES, MAX_READ_BYTES, LogWatcher,
)

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool) -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}")


def make_watcher(tmp: Path) -> tuple[LogWatcher, list]:
    """造一个已 start 过、但 _pos 归零的 watcher（等价于「从文件头开始追」）。"""
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    events: list = []

    async def on_event(etype, player, detail):
        events.append((etype, player, detail))

    w = LogWatcher(str(tmp), on_event, poll_interval=0.01)
    w._enc = "utf-8"
    w._pos = 0
    return w, events


def main() -> int:
    print("================ [A] 日志读取上限 ================")
    check("MAX_READ_BYTES 已定义且为正", isinstance(MAX_READ_BYTES, int) and MAX_READ_BYTES > 0)
    check("MAX_LAG_BYTES 已定义且大于单次上限", MAX_LAG_BYTES > MAX_READ_BYTES)
    check("单次上限没有大到失去意义（≤ 4 MiB）", MAX_READ_BYTES <= 4 * 1024 * 1024)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, events = make_watcher(tmp)
        p = tmp / "logs" / "latest.log"
        # 造 1 MiB + 5000 字节的规整日志行
        line = "[12:00:00] [Server thread/INFO]: <Steve> " + "x" * 30 + "\n"
        n = (MAX_READ_BYTES + 5000) // len(line) + 1
        p.write_bytes((line * n).encode("utf-8"))
        size = p.stat().st_size
        check(f"样本文件 {size} 字节 > 单次上限", size > MAX_READ_BYTES)

        asyncio.run(w._poll())
        check("一次 _poll 不会把整个文件读完（_pos 未到尾部）", w._pos < size)
        check("一次 _poll 最多前进 MAX_READ_BYTES", w._pos <= MAX_READ_BYTES)
        check("确实消费了内容（_pos > 0）", w._pos > 0)
        first_pos = w._pos
        check("消费的是**完整行**（正好停在换行处）", p.read_bytes()[: w._pos].endswith(b"\n"))
        check("事件被正常产出（不是静默丢弃）", len(events) > 0)

        # 继续 poll 应能读完（无积压）
        for _ in range(40):
            asyncio.run(w._poll())
            if w._pos >= size:
                break
        check("继续轮询可以追平文件尾部", w._pos == size)
        check("第二次之后又前进了", w._pos > first_pos)

    print("\n================ [A2] 滞后保护：积压过大时跳到尾部 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, events = make_watcher(tmp)
        p = tmp / "logs" / "latest.log"
        line = "[12:00:00] [Server thread/INFO]: <Steve> yyyyyyyyyyyyyyyyyyyy\n"
        n = (MAX_LAG_BYTES + 100000) // len(line) + 1
        p.write_bytes((line * n).encode("utf-8"))
        size = p.stat().st_size
        check(f"积压样本 {size} 字节 > MAX_LAG_BYTES", size > MAX_LAG_BYTES)

        asyncio.run(w._poll())
        check("积压超限 → 直接追到文件尾部（不逐块慢慢爬）", w._pos == size)

    print("\n================ [A3] 超长无换行片段：绝不能原地卡死 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, events = make_watcher(tmp)
        p = tmp / "logs" / "latest.log"
        # 整整 MAX_READ_BYTES 一个换行都没有（某些模组把堆栈挤成一行）
        p.write_bytes(b"z" * MAX_READ_BYTES)
        asyncio.run(w._poll())
        check("读满上限的无换行片段 → _pos 前进（不卡死）", w._pos == MAX_READ_BYTES)
        check("没有产出事件（这本来就不是完整行）", len(events) == 0)

        # 再 poll 一次：这次读到的是空（文件已到尾部区间），_pos 不应倒退
        asyncio.run(w._poll())
        check("_pos 单调不减", w._pos >= MAX_READ_BYTES)

        # 半行（未读满上限）：必须保持等待，不能跳过
        p2 = tmp / "logs" / "latest.log"
        p2.write_bytes(b"[12:00:00] [Server thread/INFO]: <Alex> ")   # 半行
        w2, ev2 = make_watcher(tmp)
        asyncio.run(w2._poll())
        check("未读满上限的半行 → 原地等待（_pos 不动）", w2._pos == 0)
        check("半行不产出事件", len(ev2) == 0)
        p2.write_bytes(b"[12:00:00] [Server thread/INFO]: <Alex> hello\n")
        asyncio.run(w2._poll())
        check("行写完后立刻被消费", w2._pos == p2.stat().st_size)
        check("产出 1 条聊天事件", len(ev2) == 1 and ev2[0][0] == "chat")

    print("\n================ [A4] 轮转（日志被重建）仍按原逻辑处理 ================")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, events = make_watcher(tmp)
        p = tmp / "logs" / "latest.log"
        p.write_bytes(b"[12:00:00] [Server thread/INFO]: <Steve> a\n")
        asyncio.run(w._poll())
        old_pos = w._pos
        check("先消费一轮", old_pos > 0)
        p.write_bytes(b"[12:00:01] [Server thread/INFO]: <Steve> b\n")   # 轮转，文件变小
        asyncio.run(w._poll())
        check("文件变小 → 从头重读（size < _pos 分支仍生效）", w._pos == p.stat().st_size)

    print("\n================ [B] LLM 工具入参限长 ================")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("命令上限常量已定义", "MAX_COMMAND_CHARS = 8000" in src)
    check("消息上限常量已定义", "MAX_MESSAGE_CHARS = 500" in src)
    check("统一 helper 已定义", "def _too_long(" in src)
    check("helper 说明「只做长度粗筛、不改既有语义」", "只做长度粗筛" in src)
    check("mc_execute_command 检查 command",
          'self._too_long(command, self.MAX_COMMAND_CHARS, "命令")' in src)
    check("mc_execute_command 也检查 feedback（它会 tellraw 进游戏）",
          'self._too_long(feedback, self.MAX_MESSAGE_CHARS, "反馈文案")' in src)
    check("mc_broadcast 检查 message",
          'self._too_long(message, self.MAX_MESSAGE_CHARS, "广播内容")' in src)
    check("限长发生在权限闸门之前（省一次无谓往返）",
          src.index('self._too_long(command, self.MAX_COMMAND_CHARS, "命令")')
          < src.index('denied = await self._safe_command(event, command, tool="mc_execute_command")'))
    check("超长文案会指路 mc_workflow", "mc_workflow" in src)

    # helper 行为（直接摘出实现比对，避免为此拉起整个插件类）
    def _too_long(value, limit, label):
        n = len(value or "")
        if n <= limit:
            return None
        return (f"{label}过长（{n} 字符，上限 {limit}）。请缩短后重试；"
                "需要复杂 NBT 或批量操作用 mc_workflow。")

    check("刚好等于上限 → 放行", _too_long("a" * 500, 500, "广播内容") is None)
    check("超出 1 字符 → 拒绝", _too_long("a" * 501, 500, "广播内容") is not None)
    check("None / 空串 → 放行", _too_long(None, 500, "x") is None and _too_long("", 500, "x") is None)
    msg = _too_long("a" * 501, 500, "广播内容")
    check("拒绝文案含实际长度与上限", "501" in msg and "500" in msg)

    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
