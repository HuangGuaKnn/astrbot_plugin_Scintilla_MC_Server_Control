# -*- coding: utf-8 -*-
"""v0.22.6 批次 2 · 复杂任务路由守门（实施单 §五）。

为什么不能只改提示词
====================
``agent_prompts.py`` 里「原版物品 = simple」与「复杂 NBT 结构（附魔…）= complex」
在「附魔原版剑」上**规则重叠**，实测 LLM 选了 simple ——
而 simple 是两条路径里**唯一没有输出校验、没有纠错、没有到账核验**的一条。

所以修法是两层：
* 改规则（治本，已做：改为「**裸**原版物品」+ 显式优先级）；
* 加**确定性**守门（兜底，本文件测的就是它）——分类之后、执行之前强制转 complex。

⚠️ 守门**只做升级**（simple → complex），绝不反向降级。
"""
import asyncio
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.command_result import (  # noqa: E402
    looks_like_complex_item_command,
    looks_like_complex_request,
    looks_like_complex_task,
)

# 端到端那组要 import core.workflow，而它依赖 AstrBot 运行时（astrbot.api.*）。
# 找不到运行时则**只跳过端到端组**，纯函数组照跑（不整体判失败）。
WORKFLOW_OK = True
IMPORT_ERR = ""
try:
    from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow
except Exception as e:  # noqa: BLE001
    WORKFLOW_OK = False
    IMPORT_ERR = f"{type(e).__name__}: {e}"
    MCWorkflow = None  # type: ignore[assignment]

_fail: list[str] = []
_skip: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


# ===================== 一、请求特征 =====================

def request_marker_cases() -> None:
    print("---- 一、请求文本特征（第一层路由信号）----")
    for text in ["发一把附魔锋利5的下界合金剑", "给我满配的 M4A1", "要带 NBT 的",
                 "自定义名称改成宝剑", "加个属性修饰符", "装上配件"]:
        check(f"复杂请求命中：{text}", looks_like_complex_request(text) is True)
    for text in ["把时间设成白天", "给我发一把钻石剑", "现在几点", "广播大家好"]:
        check(f"简单请求不误伤：{text}", looks_like_complex_request(text) is False)


# ===================== 二、命令上下文感知（补丁 7） =====================

def command_marker_cases() -> None:
    print("---- 二、命令特征必须**上下文感知**（补丁 7：裸 [ 太吵）----")
    bad = 'give HuangGuaKnn netherite_sword[enchantments={levels:{"minecraft:sharpness":5}}] 1'
    check("★事故命令命中复杂物品命令", looks_like_complex_item_command(bad) is True, "")

    # 这些全是**正常简单命令**，含裸 [ 但是选择器语法 —— 一旦按 [ 触发就白跑整套流水线
    for cmd in ["kill @e[type=item]", "tp @p[tag=foo]",
                "gamemode creative @a[team=red]", "clear @a[distance=..5]"]:
        check(f"★选择器语法不得误判：{cmd}",
              looks_like_complex_item_command(cmd) is False, "被误判成复杂命令")
    check("★裸原版 give 不得误判",
          looks_like_complex_item_command("give Steve diamond 1") is False, "")
    check("非物品命令即使带 { 也不管（如 tellraw 的 JSON）",
          looks_like_complex_item_command('tellraw @a {"text":"hi"}') is False, "")
    check("execute 包装要能穿透",
          looks_like_complex_item_command(
              "execute as @a run give @s diamond_sword{Enchantments:[{id:\"minecraft:sharpness\",lvl:5}]} 1"
          ) is True, "")


def combined_cases() -> None:
    print("---- 三、请求 + 命令：任一命中即判复杂 ----")
    check("请求命中 → 复杂", looks_like_complex_task("附魔剑") is True)
    check("命令命中 → 复杂",
          looks_like_complex_task("发个东西", [{"command": "give A x{Enchantments:[]} 1"}]) is True)
    check("★都未命中 → 不升级（不得把简单任务误升级，白烧 token）",
          looks_like_complex_task("把时间设成白天", [{"command": "time set day"}]) is False)
    check("守门**只升级不降级**：请求复杂时命令再简单也判复杂",
          looks_like_complex_task("满配 M4A1", [{"command": "say hi"}]) is True)


# ===================== 四、端到端：dispatch 的强制转轨 =====================

class FakeEvent:
    unified_msg_origin = "test:umo"


class FakeLogger:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass


class FakePlugin:
    """只提供 dispatch 需要的几个钩子。"""
    logger = FakeLogger()

    def __init__(self):
        self.cfg = {}

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _version_context_text(self):
        return "【服务端版本上下文 · 测试桩】"


class FakeAgent:
    def __init__(self, cls):
        self._cls = cls
        self.version_context = ""

    async def classify(self, request, umo=""):
        return dict(self._cls)

    def set_version_context(self, text):
        self.version_context = text


def build_workflow(cls: dict):
    """不走 __init__（避免真实 AgentLLM / AstrBot Context 依赖）。"""
    wf = MCWorkflow.__new__(MCWorkflow)
    wf.plugin = FakePlugin()
    wf.logger = FakeLogger()
    wf.agent = FakeAgent(cls)
    wf.logs = deque(maxlen=30)
    wf._tasks = set()
    wf.called = []
    return wf


async def dispatch_cases() -> None:
    if not WORKFLOW_OK:
        print("---- 四、端到端（SKIP：找不到 AstrBot 运行时）----")
        _skip.append(f"端到端转轨用例（{IMPORT_ERR}）")
        print(f"[SKIP] 端到端转轨用例（{IMPORT_ERR}）")
        return
    print("---- 四、端到端：分类器判 simple，但必须被强制转 complex ----")
    complex_req = "给我发一把附魔锋利5、耐久3的下界合金剑"

    # ① 分类器「判错」成 simple，且它给出的命令就是那条 1.21 组件语法命令
    cls = {
        "type": "simple", "requires_player": True, "target": "named",
        "player_name": "HuangGuaKnn", "reason": "原版物品",
        "commands": [{"command": 'give HuangGuaKnn netherite_sword[enchantments={levels:{}}] 1',
                      "feedback": "已发放"}],
    }
    wf = build_workflow(cls)

    async def fake_decide(event, request, player, c):
        return {"abort": False, "player": "HuangGuaKnn", "reason": ""}

    async def fake_simple(c, request, player, event):
        wf.called.append("simple")
        return "不该走到这里", "done"

    async def fake_complex_async(request, player, event, umo):
        wf.called.append("complex")

    wf._decide_player = fake_decide
    wf._run_simple = fake_simple
    wf._run_complex_async = fake_complex_async

    out = await wf.dispatch(FakeEvent(), complex_req)
    # 复杂路径是 asyncio.create_task 调度的（后台流水线），
    # 让出一次事件循环，被调度的协程才会真正跑起来。
    await asyncio.sleep(0)

    check("★分类器判 simple 但命中复杂特征 → **不走** simple 路径",
          "simple" not in wf.called, str(wf.called))
    check("★被强制转进 complex 路径", "complex" in wf.called, str(wf.called))
    check("回执写明已进入后台流水线", "已开始" in out, out[:120])
    check("★路由日志如实记录「已强制转 complex」（可追溯）",
          any("强制转 complex" in str(e.get("detail", "")) for e in wf.logs),
          str(list(wf.logs))[:300])

    # ② 对照组：真正的简单请求必须**留在** simple 路径（不得误升级）
    wf2 = build_workflow({
        "type": "simple", "requires_player": False, "target": "none",
        "player_name": "", "reason": "时间命令",
        "commands": [{"command": "time set day"}],
    })
    wf2._decide_player = fake_decide

    async def fake_simple2(c, request, player, event):
        wf2.called.append("simple")
        return "已把时间设为白天", "done"

    async def fake_complex_async2(request, player, event, umo):
        wf2.called.append("complex")

    wf2._run_simple = fake_simple2
    wf2._run_complex_async = fake_complex_async2

    out2 = await wf2.dispatch(FakeEvent(), "把时间设成白天")
    check("★对照组：真简单请求留在 simple（不误升级、不白烧 token）",
          wf2.called == ["simple"], str(wf2.called))
    check("对照组：回执是 simple 的即时结果", "完成" in out2, out2[:120])

    # ③ 版本上下文必须在**分类器之前**注入（事故里命令就是分类器写的）
    wf3 = build_workflow({
        "type": "simple", "requires_player": False, "target": "none",
        "player_name": "", "commands": [{"command": "time set day"}],
    })
    wf3._decide_player = fake_decide
    wf3._run_simple = fake_simple2
    wf3._run_complex_async = fake_complex_async2
    await wf3.dispatch(FakeEvent(), "把时间设成白天")
    check("★版本上下文已注入 Agent（否则分类器只能凭记忆猜版本）",
          "测试桩" in wf3.agent.version_context, repr(wf3.agent.version_context))

    # ④ 版本上下文注入失败不得炸掉主流程
    class BoomPlugin(FakePlugin):
        def _version_context_text(self):
            raise RuntimeError("探测炸了")

    wf4 = build_workflow({"type": "simple", "requires_player": False, "target": "none",
                          "player_name": "", "commands": [{"command": "time set day"}]})
    wf4.plugin = BoomPlugin()
    wf4._decide_player = fake_decide
    wf4._run_simple = fake_simple2
    wf4._run_complex_async = fake_complex_async2
    try:
        await wf4.dispatch(FakeEvent(), "把时间设成白天")
        check("★版本上下文注入失败 → 主流程照常（不得因探测失败炸掉任务）", True)
    except Exception as e:  # noqa: BLE001
        check("★版本上下文注入失败 → 主流程照常（不得因探测失败炸掉任务）", False, repr(e))


def main() -> int:
    request_marker_cases()
    command_marker_cases()
    combined_cases()
    asyncio.run(dispatch_cases())
    print("==========================================")
    if _fail:
        print(f"FAILED {len(_fail)} 项：")
        for f in _fail:
            print(f"  - {f}")
        return 1
    tail = "（含端到端转轨）" if WORKFLOW_OK else "（端到端组已跳过）"
    print(f"全部通过（{_pass} 项{tail}）：分类失误不再致命 —— "
          f"带数据的请求必被拦进有校验的复杂路径，且版本上下文先于分类器注入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
