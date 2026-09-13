# -*- coding: utf-8 -*-
"""v0.21.6 回归：帮助文案 + 「mcs 热重载」下线。

覆盖：
  1. 真实的 mcs_help_cmd 渲染（ast 抽源码 + 假 event），文案断言；
  2. mcs 指令组里再无「热重载」命令（静态扫 @mcs.command 装饰器，含别名）；
  3. _conf_schema.json 里 enable_reload_command 已删、enable_mc_reload_plugin 仍在；
  4. 线上配置无残留 enable_reload_command 键；
  5. _schedule_hot_reload 的失败文案不再引导用户去聊天里敲 mcs 热重载；
  6. 面向用户的文案（字符串字面量 / json / html / yaml）里不再出现「mcs 热重载」。
"""
import ast
import asyncio
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()
PLUGIN = PLUGIN_DIR
MAIN = PLUGIN / "main.py"
SCHEMA = PLUGIN / "_conf_schema.json"
from _paths import LIVE_CFG  # noqa: E402  （本机运行配置；拿不到时下面的用例会自动跳过）
SRC = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SRC)

FAIL = []


def check(desc, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAIL.append(desc)


# ---------- 线上配置拍平，供假 self._cfg 使用 ----------
def flat(d, pre=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, pre + k + "."))
        else:
            out[pre + k] = v
    return out


if LIVE_CFG and Path(LIVE_CFG).is_file():
    LIVE = flat(json.loads(io.open(LIVE_CFG, encoding="utf-8-sig").read()))
else:                                  # 别的机器上没有本机运行配置 → 只跳过「线上配置」用例
    LIVE = None
    print("[SKIP] 未找到本机运行配置（LIVE_CFG），跳过「线上配置残留」检查")


def grab(name, kind=ast.AsyncFunctionDef):
    node = next(n for n in ast.walk(TREE) if isinstance(n, kind) and n.name == name)
    return ast.get_source_segment(SRC, node)


class Ev:
    def __init__(self):
        self.out = []

    def plain_result(self, t):
        self.out.append(t)
        return t


def build(cls_src, globs=None):
    ns = {"AstrMessageEvent": object, "asyncio": asyncio, "plugin_dirs_for": lambda pm, n: ["x"],
          "is_patch_installed": lambda: True, **(globs or {})}
    exec("class Fake:\n" + cls_src + "\n", ns)
    return ns["Fake"], ns


def fake_self():
    f = globals()["FAKE_SELF"]()
    f._cfg = lambda key, default=None: (LIVE or {}).get(key, default)
    return f


print("=========== 1. 真实帮助输出（ast 抽 mcs_help_cmd） ===========")
H, _ = build("    def _wake_prefix(self): return ''\n"
             "    def _is_admin(self, e): return True\n"
             "    def _is_whitelist_policy(self): return True\n"
             "    def _tool_enabled(self, t): return True\n" +
             "\n".join("    " + l for l in grab("mcs_help_cmd").splitlines()) + "\n")
FAKE_SELF = H


async def render():
    ev = Ev()
    async for _ in fake_self().mcs_help_cmd(ev):
        pass
    return "\n".join(ev.out)


help_text = asyncio.run(render())
print(help_text)
print("-" * 72)
check("帮助里不再有「mcs 热重载」", "热重载" not in help_text)
check("管理员区仍以「全屏喊话」收尾", "mcs 全屏喊话 <内容>：向全服发送全屏 title 喊话（仅管理员）" in help_text)
check("结尾吩咐提示仍是 v0.21.5 文案",
      help_text.endswith("提示：也可以用自然语言直接吩咐我干活，例如「给Steve发一把钻石剑」"))
check("仍无多余的前缀/唤醒解释行", "唤醒词" not in help_text and "不是在游戏里输入" not in help_text)

print("\n=========== 2. mcs 指令组登记表（静态扫装饰器） ===========")
cmds = []
for n in ast.walk(TREE):
    if not isinstance(n, ast.AsyncFunctionDef):
        continue
    for dec in n.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "command" and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "mcs"):
            first = dec.args[0].value if dec.args else ""
            alias = []
            for kw in dec.keywords:
                if kw.arg == "alias" and isinstance(kw.value, ast.Set):
                    alias = [e.value for e in kw.value.elts]
            cmds.append((first, alias, n.name))
for first, alias, fn in cmds:
    print(f"  mcs {first:<6} alias={alias or '-'}  →  {fn}()")
check("登记表里没有「热重载」", all(c[0] != "热重载" for c in cmds))
check("也没有 reload 别名", all("reload" not in c[1] for c in cmds))
check("其他指令都还在（%d 条）" % len(cmds), len(cmds) >= 9 and
      {"绑定", "解绑", "查询", "喊话", "状态", "帮助", "踢人", "封禁", "解封", "封禁列表", "全屏喊话"}
      <= {c[0] for c in cmds})
check("mcs_reload_cmd 函数体已删干净", "mcs_reload_cmd" not in SRC)

print("\n=========== 3. 配置 schema ===========")
schema = json.loads(io.open(SCHEMA, encoding="utf-8").read())
groups = schema["config"] if "config" in schema else schema
allkeys = set()


def collect(o):
    """schema 是「分组 → {type: object, items: {...}}」两层结构。"""
    for k, v in o.items():
        if not isinstance(v, dict):
            continue
        if v.get("type") == "object" or "items" in v:
            collect(v.get("items", {}))
        else:
            allkeys.add(k)


collect(groups)
cmd_keys = set((groups["commands"].get("items") or {}).keys())
tool_keys = set((groups["tools"].get("items") or {}).keys())
print("  commands 组 %d 键：" % len(cmd_keys), sorted(cmd_keys))
check("enable_reload_command 已从 schema 删除", "enable_reload_command" not in allkeys)
check("指令组按键数与预期一致（11 → 10，只少「热重载指令」）", cmd_keys == {
    "enable_bind_command", "enable_say_command", "say_command_public", "enable_status_command",
    "enable_kick_command", "enable_ban_command", "enable_unban_command",
    "enable_banlist_command", "enable_help_command", "enable_title_command"}, cmd_keys)
check("enable_mc_reload_plugin 仍在（工具保留）", "enable_mc_reload_plugin" in tool_keys)
check("schema 里再没有任何 reload 开关（tools 里也只留工具键）",
      not [k for k in allkeys if "reload" in k and k != "enable_mc_reload_plugin"])

print("\n=========== 4. 线上配置无残留 ===========")
if LIVE is None:
    print("[SKIP] 本机运行配置不存在，跳过（换机器跑不受影响）")
else:
    check("LIVE 配置里没有 enable_reload_command", "commands.enable_reload_command" not in LIVE)
    check("LIVE 配置里 enable_mc_reload_plugin=True（工具仍开）",
          LIVE.get("tools.enable_mc_reload_plugin") is True)

print("\n=========== 5. _schedule_hot_reload 文案 ===========")
fn_src = grab("_schedule_hot_reload", ast.FunctionDef)
F2, _ = build(
    "    def _plugin_manager(self): return object()\n" +
    "\n".join("    " + l for l in fn_src.splitlines()) + "\n",
    {"plugin_dirs_for": lambda pm, n: [], "perform_hot_reload": lambda *a: None},
)
notfound = F2()._schedule_hot_reload("不存在的插件")
print("  实体缺失分支 →", notfound)

async def full():
    F3, _ = build(
        "    def _plugin_manager(self): return object()\n" +
        "\n".join("    " + l for l in fn_src.splitlines()) + "\n",
        {"plugin_dirs_for": lambda pm, n: ["x"],
         "perform_hot_reload": lambda *a: (True, "ok")},
    )
    f = F3()
    f.logger = type("L", (), {"info": lambda *a: None, "warning": lambda *a: None})()
    f.__class__.logger = f.logger
    return f._schedule_hot_reload(None)


ok_msg = asyncio.run(full())
print("  全量重载分支 →", ok_msg)
check("缺失插件文案不再提 mcs 热重载", "mcs 热重载" not in notfound and "插件名留空" in notfound)
check("全量重载提示仍告知无需重启", "无需重启 AstrBot" in ok_msg)

print("\n=========== 6. 面向用户文案全局扫描 ===========")
targets = [MAIN, PLUGIN / "core" / "web_api.py", SCHEMA,
           PLUGIN / "metadata.yaml", PLUGIN / "pages" / "mc_control" / "index.html"]
bad = []
for t in targets:
    for i, line in enumerate(io.open(t, encoding="utf-8").read().splitlines(), 1):
        if "mcs 热重载" in line and not line.lstrip().startswith("#"):
            bad.append(f"{t.name}:{i}: {line.strip()[:70]}")
check("没有「mcs 热重载」残留文案", not bad, bad)
hint_cases = [
    ("WebUI 提示指向 AstrBot 插件管理页", "重载本插件请到 AstrBot「插件管理」页点「重载」"
     in io.open(PLUGIN / "pages" / "mc_control" / "index.html", encoding="utf-8").read()),
    ("settings 接口提示同样指向 AstrBot",
     "请到 AstrBot「插件管理」重载本插件后重新保存"
     in io.open(PLUGIN / "core" / "web_api.py", encoding="utf-8").read()),
    ("metadata 说明重载回 AstrBot",
     "聊天平台不再提供重载指令" in io.open(PLUGIN / "metadata.yaml", encoding="utf-8").read()),
]
for d, c in hint_cases:
    check(d, c)

print("\n结果：", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 条：{FAIL}")
sys.exit(1 if FAIL else 0)
