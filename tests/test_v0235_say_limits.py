"""v0.23.5 回归：喊话必须有长度与频率上限（外部审查 ⑧）。

审查原话：`mcs_say` 默认公开广播，缺长度 / 频率 / 审计。
皮莉卡核实后的实情是 **审计日志与两个开关本来就在**（`enable_say_command`、
`say_command_public`、`[审计] 请求者=… 喊话=…`），真正缺的是这两条：

  A. **长度上限** —— 超长文本原样拼进 `tellraw`，服务器侧截断，玩家根本看不到。
  B. **频率上限** —— 群里连点/刷屏时，每条都变成一次 RCON 往返，而 RCON 是
     **共享连接**，会把别人的正常指令挤在后面排队。

本文件钉三件事：
  1. `core/rate_limit.Cooldown` 本身的行为（含键表上限、假时钟、拒绝不刷新计时器）；
  2. `McControlPlugin._say_gate()` 的两道闸门与边界（0 = 不限、管理员豁免冷却）；
  3. **接线顺序**：闸门必须在 `_get_rcon()` 之前 —— 被拒的喊话不占 RCON 往返；
     以及配置项默认值与代码常量不许漂移。

运行：
  python tests\\test_v0235_say_limits.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control import main as plugin_main  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.rate_limit import Cooldown  # noqa: E402

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


# ---------------------------------------------------------------- 假时钟

class FakeClock:
    """可手动推进的单调时钟（避免测试真的 sleep）。"""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------- 插件替身

class StubPlugin:
    """只借 `_say_gate` 需要的几个东西，不实例化真的 Star（避免拉起 AstrBot）。"""

    _say_gate = plugin_main.McControlPlugin._say_gate
    _as_float = staticmethod(plugin_main.McControlPlugin._as_float)
    _say_cooldown = plugin_main.McControlPlugin._say_cooldown
    SAY_MAX_CHARS = plugin_main.McControlPlugin.SAY_MAX_CHARS
    SAY_COOLDOWN_SECONDS = plugin_main.McControlPlugin.SAY_COOLDOWN_SECONDS

    def __init__(self, cfg: dict | None = None, admin: bool = False, clock=None):
        self.cfg = dict(cfg or {})
        self.admin = admin
        self._say_cd = Cooldown(clock=clock or FakeClock())

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _is_admin(self, event=None):
        return self.admin

    def _say_gate(self, text, sender, is_admin=None):
        """真身签名要求显式传 is_admin（mcs_say 里就是这么传的），替身顺手注入。"""
        return plugin_main.McControlPlugin._say_gate(
            self, text, sender,
            is_admin=self.admin if is_admin is None else is_admin,
        )


def main() -> int:
    print("================ [A] Cooldown 本体 ================")
    clock = FakeClock()
    cd = Cooldown(clock=clock)
    check("首次 hit 立刻放行", cd.hit("u1", 5.0) == 0.0)
    check("冷却窗口内再 hit 被拒且返回剩余秒数", cd.hit("u1", 5.0) == 5.0)
    clock.advance(3.0)
    check("被拒**不刷新**计时器（推进 3 秒后剩 2 秒）", round(cd.remaining("u1", 5.0), 6) == 2.0)
    check("remaining 是只读的（连问两次不变）", round(cd.remaining("u1", 5.0), 6) == 2.0)
    clock.advance(2.0)
    check("窗口走完即放行", cd.hit("u1", 5.0) == 0.0)
    check("换 key 互不影响", cd.hit("u2", 5.0) == 0.0)
    check("seconds=0 → 永不冷却", cd.hit("u3", 0.0) == 0.0 and cd.hit("u3", 0.0) == 0.0)
    check("seconds 为负数按不限处理", cd.hit("u3", -1.0) == 0.0)
    check("forget(key) 只清一个", cd.forget("u2") == 1 and cd.hit("u2", 5.0) == 0.0)
    n_keys = len(cd)
    check("seconds=0 的 key 不进表（本来就不冷却）", n_keys == 2)
    check("forget(None) 全清并返回条数", cd.forget() == n_keys and len(cd) == 0)

    print("\n================ [B] 键表有上限（内存不无界增长） ================")
    small = Cooldown(max_keys=3, clock=clock)
    for i in range(10):
        small.hit(f"user{i}", 60.0)
    check("塞 10 个 key 后长度不超过上限 3", len(small) == 3)
    check("保留的是**最近**放行的（user9 在表内）", small.remaining("user9", 60.0) > 0)
    check("最旧的已被淘汰（user0 已不在表内）", small.remaining("user0", 60.0) == 0.0)
    check("淘汰后仍可正常放行", small.hit("newbie", 1.0) == 0.0)

    print("\n================ [C] 长度闸门 ================")
    p = StubPlugin()
    check("默认上限就是常量" , p.SAY_MAX_CHARS == 200)
    check("刚好等于上限 → 放行", p._say_gate("x" * 200, "u1") is None)
    denied = p._say_gate("x" * 201, "u2")
    check("超一个字 → 拒绝", isinstance(denied, str))
    check("拒绝文案说清实际长度与上限", "201" in denied and "200" in denied)
    check("拒绝文案是给人看的（提到玩家看不到）", "看不到" in denied)
    tight = StubPlugin(cfg={"say_max_chars": 10})
    check("配置可改上限（10 字内放行）", tight._say_gate("x" * 10, "u1") is None)
    check("配置可改上限（11 字拒绝）", isinstance(tight._say_gate("x" * 11, "u1"), str))
    free = StubPlugin(cfg={"say_max_chars": 0})
    check("上限 0 = 不限长度", free._say_gate("x" * 5000, "u1") is None)
    bad = StubPlugin(cfg={"say_max_chars": "not-a-number"})
    check("非法配置回落到默认上限（不因配置写坏而放行）",
          bad._say_gate("x" * 5000, "u1") is not None)

    print("\n================ [D] 频率闸门 ================")
    clock_d = FakeClock()
    q = StubPlugin(clock=clock_d)
    check("第一次喊话放行", q._say_gate("你好", "u1") is None)
    second = q._say_gate("再来一条", "u1")
    check("5 秒内第二条被拒", isinstance(second, str))
    check("拒绝文案报出还要等几秒", "5 秒" in second)
    clock_d.advance(4.9)
    third = q._say_gate("还早", "u1")
    check("4.9 秒仍在冷却（向上取整报 1 秒）", isinstance(third, str) and "1 秒" in third)
    clock_d.advance(0.2)
    check("5.1 秒后放行", q._say_gate("可以了", "u1") is None)
    check("别人不受影响", q._say_gate("另一个人", "u2") is None)

    admin = StubPlugin(admin=True, clock=clock_d)
    check("管理员不受冷却限制（连喊两次都放行）",
          admin._say_gate("一", "boss") is None and admin._say_gate("二", "boss") is None)
    check("管理员**仍受长度限制**", admin._say_gate("x" * 201, "boss") is not None)

    off = StubPlugin(cfg={"say_cooldown_seconds": 0}, clock=FakeClock())
    check("冷却 0 = 不限频率",
          off._say_gate("一", "u1") is None and off._say_gate("二", "u1") is None)
    frac = StubPlugin(cfg={"say_cooldown_seconds": 1.5}, clock=clock_d)
    check("冷却支持小数（1.5 秒）", frac._say_gate("一", "u1") is None
          and isinstance(frac._say_gate("二", "u1"), str))
    check("拒绝的喊话不占冷却（阻塞期间不会顺延）",
          frac._say_gate("三", "u1") is not None)

    print("\n================ [E] 接线与配置（源码级护栏） ================")
    src = Path(plugin_main.__file__).read_text(encoding="utf-8")
    body = src.split("async def mcs_say", 1)[1].split("async def mcs_title_cmd", 1)[0]
    check("mcs_say 确实调用了 _say_gate", "_say_gate(" in body)
    check("闸门在 _get_rcon() **之前**（被拒的喊话不占 RCON 往返）",
          body.index("_say_gate(") < body.index("_get_rcon("))
    check("被拒绝的喊话也写审计日志", "喊话被拒" in body)
    check("喊话仍保留原有审计日志", "[审计]" in body)
    check("喊话仍受 say_command_public 开关约束", "say_command_public" in body)

    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["commands"]["items"]
    check("schema 里有 say_max_chars", "say_max_chars" in items)
    check("schema 里有 say_cooldown_seconds", "say_cooldown_seconds" in items)
    check("schema 默认值与代码常量一致（长度）",
          items["say_max_chars"]["default"] == plugin_main.McControlPlugin.SAY_MAX_CHARS)
    check("schema 默认值与代码常量一致（冷却）",
          float(items["say_cooldown_seconds"]["default"])
          == plugin_main.McControlPlugin.SAY_COOLDOWN_SECONDS)
    check("两个新项都写了 hint（设置页要能看懂）",
          bool(items["say_max_chars"].get("hint")) and bool(items["say_cooldown_seconds"].get("hint")))
    check("hint 里说明了 0 = 不限", "0 = 不限" in items["say_max_chars"]["hint"]
          and "0 = 不限" in items["say_cooldown_seconds"]["hint"])

    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for name in _fail:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
