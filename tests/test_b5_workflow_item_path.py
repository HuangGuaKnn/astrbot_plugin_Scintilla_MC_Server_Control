# -*- coding: utf-8 -*-
"""B5 接线 · 工作流物品路径（预扁平化世代的 give 必须过生成器）。

背景（2026-10-07 两代真机实证）：
- 工作流原先只有一道**不带已验证通道**的版本拦：pre-1.13 上的 ``give`` 一律被拒
  —— 安全（fail-closed）但功能缺失，复杂流水线在 1.7.10 / 1.12.2 上发不了物品；
- v0.23.9 起：``give`` 先交 ``core/legacy_items.plan_give()`` 重构，域表校验通过
  才算「已验证」放行；否则 fail-closed 拒绝，**绝不**把手写命令直接下发。
  拒绝面覆盖：扁平名（1.13+）/ 域外数据值（B13 血账）/ 越界数量 / 非法 NBT / 玩家名不合语法。
- 同批修：工作流的世代标记 ``_preflatten`` 改为**每次入口重算** —— 热切服务端
  （设置页保存）不走重载，缓存在 ``__init__`` 的值会残留旧世代的判断（当晚实测的坑）。

运行：``python tests/test_b5_workflow_item_path.py``
"""

import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary import (  # noqa: E402
    ItemDictionary,
)
from astrbot_plugin_Scintilla_MC_Server_Control.core.workflow import MCWorkflow  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.main import McControlPlugin  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, extra=""):
    if cond:
        PASS.append(label)
        print("[PASS] " + label)
    else:
        FAIL.append(label + (" ｜ " + str(extra) if extra else ""))
        print("[FAIL] " + label + (" ｜ " + str(extra) if extra else ""))


#: 假 jar 里的 legacy 语言表（1.7.10 口径：wool dmg0 叫 Wool，不叫 White Wool）
LANG = ("item.apple.name=Apple\n"
        "tile.wool.white.name=Wool\n"
        "tile.wool.red.name=Red Wool\n"
        "tile.planks.oak.name=Oak Wood Planks\n"
        "tile.planks.darkOak.name=Dark Oak Wood Planks\n")


def _mk_fake_jar(path, lang_text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("assets/minecraft/lang/en_us.lang", lang_text)
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")


class _StubPlugin:
    """只提供转交器真正用到的两件东西：词典 + 规划器（取插件真身）。"""

    def __init__(self, dictionary):
        self._dictionary = dictionary

    _legacy_give_plan = McControlPlugin._legacy_give_plan


class _StubAgent:
    def set_version_context(self, text):
        self.ctx = text


class _WF:
    """借用真实实现的替身：只测「转交逻辑」这一层，不起完整 AstrBot 运行时。"""

    plugin = None
    agent = _StubAgent()
    _preflatten = True
    _B5_GIVE_RE = MCWorkflow._B5_GIVE_RE
    _b5_reroute_give = MCWorkflow._b5_reroute_give
    _refresh_version_context = MCWorkflow._refresh_version_context


class _VI:
    def __init__(self, mc):
        self.mc = mc


class _VerPlugin:
    def __init__(self, mc):
        self._mc = mc

    def _version_context_text(self):
        return "（替身）版本上下文"

    def _resolve_version_info(self):
        return _VI(self._mc)


print("=" * 78)
print("B5 接线自检 · 工作流物品路径（预扁平化世代的 give）")
print("=" * 78)

# ---------------------------------------------------------------- 夹具：假 jar
with tempfile.TemporaryDirectory() as _td:
    _mk_fake_jar(os.path.join(_td, "minecraft_server.1.7.10.jar"), LANG)
    D = ItemDictionary(_td)
    D.build()
    check("★夹具：假 jar（带 legacy .lang）→ 识别为 1.7.10 世代",
          getattr(D, "legacy_generation", "") == "1.7.10", getattr(D, "legacy_generation", ""))
    check("夹具：变体族已装载（wool 在内）", "wool" in getattr(D, "legacy_variants", {}),
          list(getattr(D, "legacy_variants", {}))[:6])

    def reroute(cmd, preflatten=True):
        wf = _WF()
        wf.plugin = _StubPlugin(D)
        wf._preflatten = preflatten
        return wf._b5_reroute_give(cmd)

    # ------------------------------------------------------------ 一、世代开关
    print("\n---- 一、世代开关：非预扁平化时语义完全不变 ----")
    c, r, ok = reroute("give Steve red_wool 1", preflatten=False)
    check("现代服务端：原样返回、不算已验证（交回既有守门）",
          c == "give Steve red_wool 1" and r == "" and ok is False, (c, r, ok))
    c, r, ok = reroute("effect give Steve minecraft:speed 10 1")
    check("非 give（effect）→ 原样返回、不算已验证", c == "effect give Steve minecraft:speed 10 1"
          and r == "" and ok is False, (c, r, ok))

    # ------------------------------------------------------------ 二、放行面
    print("\n---- 二、放行面：域内一律重构为「家族名 + 数据值」----")
    c, r, ok = reroute("give Steve minecraft:wool 16 14")
    check("★数量 + 数据值混合放行", ok is True and c == "give Steve minecraft:wool 16 14", (c, r, ok))
    c, r, ok = reroute("give Steve minecraft:wool 1 14")
    check("★家族名 + 域内数据值放行", ok is True and c == "give Steve minecraft:wool 1 14", (c, r, ok))
    c, r, ok = reroute("give Steve diamond_sword 1")
    check("非变体物品放行（生成器统一输出注册名）",
          ok is True and c == "give Steve minecraft:diamond_sword 1", (c, r, ok))
    c, r, ok = reroute("give Steve minecraft:planks 1 5")
    check("木板 dmg5（1.7.10 域内有展示名）放行", ok is True and c == "give Steve minecraft:planks 1 5", (c, r, ok))

    # ------------------------------------------------------------ 三、拒绝面
    print("\n---- 三、拒绝面：全部 fail-closed，且原因可读 ----")
    c, r, ok = reroute("give Steve minecraft:wool 1 99")
    check("★域外数据值 99 拒绝（B13 血账的代码级复现）", c is None and "数据值" in r, (c, r))
    c, r, ok = reroute("give Steve minecraft:planks 1 9")
    check("★1.7.10 木板 dmg9（只有兜底名，已剔除）→ 拒绝", c is None and "数据值" in r, (c, r))
    c, r, ok = reroute("give Steve red_wool 1")
    check("★1.13+ 扁平名 red_wool → 拒绝（预扁平化世代不存在此名）",
          c is None and "词典里没有" in r, (c, r))
    c, r, ok = reroute("give Steve minecraft:wool 0 14")
    check("数量 0 → 拒绝", c is None and "数量" in r, (c, r))
    c, r, ok = reroute("give Steve minecraft:wool 99999999 14")
    check("数量越界 → 拒绝", c is None and "数量" in r, (c, r))
    c, r, ok = reroute("give bad-name! minecraft:wool 1 14")
    check("玩家名含非法字符 → 拒绝（注入防线）", c is None, (c, r))
    c, r, ok = reroute("give Steve minecraft:wool 1 14 {ench:[]}")
    check("结构合法的 NBT 放行（由生成器判定）", ok is True and c.endswith("{ench:[]}"), (c, r, ok))

    # ------------------------------------------------------------ 四、不可解析形状
    print("\n---- 四、解析不了就交回既有版本拦（不擅自放行）----")
    c, r, ok = reroute("give Steve red wool 1")
    check("展示名带空格（命令里无法表达）→ 原样返回、不算已验证",
          c == "give Steve red wool 1" and ok is False, (c, r, ok))

    class _PluginNoPlan:
        pass

    _wf = _WF()
    _wf.plugin = _PluginNoPlan()
    _wf._preflatten = True
    _c, _r, _o = _wf._b5_reroute_give("give Steve minecraft:wool 1 14")
    check("替身环境（没有生成器）→ 不抛异常、原样交回版本拦",
          _c == "give Steve minecraft:wool 1 14" and _o is False, (_c, _r, _o))

    c, r, ok = reroute("give Steve minecraft:wool 1 14 {bad")
    check("括号不配对的尾参 → 原样返回（后续仍会被版本拦挡下）",
          c == "give Steve minecraft:wool 1 14 {bad" and ok is False, (c, r, ok))

# ---------------------------------------------------------------- 五、世代重算
print("\n---- 五、入口重算世代（治热切残留）----")
wf = _WF()
wf._preflatten = False
wf.plugin = _VerPlugin((1, 7, 10))
wf._refresh_version_context()
check("★1.7.10 → _preflatten=True（旧实现只在 __init__ 算一次，热切后会残留）",
      wf._preflatten is True, wf._preflatten)
wf.plugin = _VerPlugin((1, 12, 2))
wf._preflatten = False
wf._refresh_version_context()
check("1.12.2 → _preflatten=True", wf._preflatten is True, wf._preflatten)
wf.plugin = _VerPlugin((1, 20, 4))
wf._preflatten = True
wf._refresh_version_context()
check("1.20.4 → _preflatten=False", wf._preflatten is False, wf._preflatten)

# ---------------------------------------------------------------- 六、源码守卫
print("\n---- 六、源码守卫：防止入口重算被改回去 ----")
_src = open(PLUGIN_DIR / "core" / "workflow.py", encoding="utf-8").read()
_i = _src.find("async def dispatch(")
_dispatch = _src[_i:_src.find("\n    async def ", _i + 10)] if _i >= 0 else ""
check("dispatch() 内确实调用了 _refresh_version_context()",
      "_refresh_version_context()" in _dispatch, _dispatch[:200])
check("转交器只在「预扁平化 + give」时才动手（含 _preflatten 判据）",
      "if not getattr(self, \"_preflatten\", False):" in _src, "")

print("\n" + "=" * 78)
# ---------------------------------------------------------------- 七、世代表体检
print("\n---- 七、世代表体检：域内不得有「无名兜底值」（B13 客户端崩溃的成因）----")
import json  # noqa: E402

_tables = {}
for _gen in ("1.7.10", "1.12.2"):
    with open(PLUGIN_DIR / "data" / "legacy_items" / (_gen + ".json"), encoding="utf-8") as _f:
        _t = json.load(_f)
    _tables[_gen] = _t.get("families") or _t.get("variants") or {}

_dp = _tables["1.7.10"].get("double_plant", {}).get("variants", {})
check("★1.7.10 double_plant 只剩 0~5（6~15 曾崩客户端，必须永久拒绝）",
      sorted(int(k) for k in _dp) == [0, 1, 2, 3, 4, 5], sorted(_dp))

_bad_dup, _bad_up = [], []
for _gen, _fam in _tables.items():
    for _name, _e in _fam.items():
        _vs = {int(k): v for k, v in (_e.get("variants") or {}).items()}
        if len(set(_vs.values())) != len(_vs):
            _bad_dup.append("%s/%s" % (_gen, _name))
        if _vs and _e.get("domain_upper") != max(_vs):
            _bad_up.append("%s/%s" % (_gen, _name))
check("★两代所有族：族内展示名互不重复（兜底填充的指纹）", not _bad_dup, _bad_dup[:6])
check("两代所有族：domain_upper == 域内最大数据值", not _bad_up, _bad_up[:6])

_w = {int(k) for k in _tables["1.7.10"].get("wool", {}).get("variants", {})}
check("真值族不许被误伤：1.7.10 wool 仍是 0~15", _w == set(range(16)), sorted(_w))
_pl = sorted(int(k) for k in _tables["1.7.10"].get("planks", {}).get("variants", {}))
check("1.7.10 planks 收紧到 0~5", _pl == [0, 1, 2, 3, 4, 5], _pl)
_lv = sorted(int(k) for k in _tables["1.7.10"].get("leaves", {}).get("variants", {}))
check("1.7.10 leaves 收紧到 0~3（4/5 的兜底名不可信，宁可拒）", _lv == [0, 1, 2, 3], _lv)
_st = sorted(int(k) for k in _tables["1.7.10"].get("stone", {}).get("variants", {}))
check("1.7.10 stone 收紧到 0（该世代无花岗岩等子类型）", _st == [0], _st)

print("-" * 78)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败明细：")
    for f in FAIL:
        print("   -", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
