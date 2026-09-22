"""v0.23.0 回归：前端提交键 ⊆ 后端接受键（设置白名单一致性）。

为什么要有它：
  v0.22.7 加了「手动声明服务端版本 / 物品语法世代」两个设置项 —— schema、前端控件、
  main.py 读取端全接上了，**唯独漏了 core/web_api.py 里 `_validate_settings` 的白名单**。
  后果很别扭：前端每次保存都会提交这两个键 → 后端认不出 → 回 `ignored` → 界面长期误报
  「⚠ 有 2 项后端未接受…请到 AstrBot「插件管理」重载本插件后重新保存」。
  重载当然没用（代码里就缺），主人白重载了好几回才报过来。

  既有测试为什么全绿？`ui_version_override_check.py` 用的是 **mock 后端** ——
  它只验「前端有没有把键提交上去、回来有没有渲染」，压根不跑真实 `_validate_settings`。
  而 `test_v0227_result_hardening.py` 只做静态字符串检查。两份清单靠手抄同步 → 必漏。

三层守卫（越往后越硬）：
  [1] 静态：index.html 里 DOM 真实存在的 CFG_FIELDS 键 + 运行时补的会话键 ⊆ 后端接受键；
  [2] 真后端：`_validate_settings` 对这两个键的接受与规范化（非法枚举值必须被拒，不许静默吞）；
  [3] 端到端：真浏览器点「保存全部设置」按钮 → 截获那一刻的真实载荷 →
      把它的每个键喂给真后端校验 → `ignored` 必须为空。

用法：
  python tests\\test_settings_whitelist_contract.py
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402
add_sys_paths()
require_app()                    # AstrBot 运行目录：自动发现（见 tests/_paths.py）

try:
    import ui_theme_check as F  # noqa: E402  真浏览器夹具（页面 URL / mock 路由 / 驱动补丁）
    UI_SKIP = ""
except Exception as _e:          # CI（ubuntu-latest）没有 Edge / Playwright —— 第 3 层自动跳过，
    F = None                     # 前两层的后端契约照样跑，不把 CI 弄红。
    UI_SKIP = f"{type(_e).__name__}: {_e}"
import astrbot_plugin_Scintilla_MC_Server_Control.main as m  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core import web_api as wa  # noqa: E402

HTML = PLUGIN_DIR / "pages" / "mc_control" / "index.html"
FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


class FakeCfg(dict):
    def save_config(self) -> bool:      # pragma: no cover - 本脚本不落盘
        return True


def make_plugin():
    plug = object.__new__(m.McControlPlugin)
    plug.config = FakeCfg({})
    plug._cfg_index = {}
    plug.logger = logging.getLogger("test")
    return plug


def backend_accepted_keys() -> set[str]:
    """后端 `_validate_settings` 实际会接受的键（直接读类属性，不手抄清单）。"""
    cls = wa.McControlWebApi
    keys: set[str] = set()
    for attr in ("BOOL_SETTING_KEYS", "LIST_SETTING_KEYS", "DICT_SETTING_KEYS",
                 "SESSION_TARGET_KEYS", "STR_SETTING_KEYS", "COLOR_KEYS"):
        keys |= set(getattr(cls, attr, ()) or ())
    keys |= set(getattr(cls, "GRADIENT_COLOR_KEYS", ()) or ())
    for attr in ("INT_RANGES", "FLOAT_RANGES", "ENUM_SETTING_KEYS"):
        keys |= set(getattr(cls, attr, {}) or {})
    keys |= {"gradient_format"}     # _validate_settings 里单独特判
    return keys


def front_submittable_keys() -> set[str]:
    """静态推导「主人点保存时前端会提交的键」。"""
    src = HTML.read_text(encoding="utf-8")
    blk = re.search(r"const CFG_FIELDS = \[(.*?)\n\];", src, re.S)
    pairs = re.findall(r'\[\s*"([\w]+)"\s*,\s*"([\w]+)"\s*,\s*"(\w)"', blk.group(1)) if blk else []
    dom_ids = set(re.findall(r'id="(cfg_[\w]+)"', src))
    keys = {key for cfg_id, key, _ in pairs if cfg_id in dom_ids}
    # collectSettings 里另外手工塞进去的会话键（TGT_KEYS）+ 每会话事件组
    keys |= {"notify_targets", "chat_bridge_targets", "notify_target_events"}
    return keys


def main() -> int:
    print("[1] 静态：前端提交键 ⊆ 后端接受键")
    front = front_submittable_keys()
    back = backend_accepted_keys()
    print(f"      前端会提交 {len(front)} 个键；后端接受 {len(back)} 个键")
    lost = sorted(front - back)
    check("★没有「提交了但后端不认」的键", not lost,
          "这些键保存时会弹「后端未接受」：" + "、".join(lost))

    print("\n[2] 真后端：_validate_settings 对版本 / 语法两键的行为")
    plug = make_plugin()
    api = wa.McControlWebApi(plug)

    parsed, err = api._validate_settings(
        {"server_version_override": "1.20.1", "item_syntax_override": "legacy_nbt"})
    check("★server_version_override 被接受（v0.22.7 漏项已补）",
          not err and parsed.get("server_version_override") == "1.20.1", repr(err or parsed))
    check("★item_syntax_override 被接受", not err and parsed.get("item_syntax_override") == "legacy_nbt",
          repr(err or parsed))
    check("两键都不会进 ignored",
          not [k for k in ("server_version_override", "item_syntax_override") if k not in parsed])

    parsed, err = api._validate_settings({"server_version_override": "  1.21  "})
    check("版本值会 strip（前后空格不落盘）", parsed.get("server_version_override") == "1.21",
          repr(parsed.get("server_version_override")))
    parsed, err = api._validate_settings({"server_version_override": ""})
    check("版本允许留空（= 自动探测）", not err and parsed.get("server_version_override") == "")

    parsed, err = api._validate_settings({"item_syntax_override": "COMPONENTS"})
    check("语法世代大小写归一化（COMPONENTS → components）",
          parsed.get("item_syntax_override") == "components", repr(parsed))
    parsed, err = api._validate_settings({"item_syntax_override": "nbt"})
    check("★非法语法世代被明确拒绝（不静默吞掉、不写错世代）",
          err is not None and "item_syntax_override" in err, repr(parsed))
    parsed, err = api._validate_settings({"item_syntax_override": "auto"})
    check("auto 合法", not err and parsed.get("item_syntax_override") == "auto")

    if F is None:
        print(f"      SKIP —— 本环境没有 Playwright / 真浏览器（{UI_SKIP}）")
        print("      （CI 即如此；用 AstrBot 自带解释器在本机跑时这一层会真实执行）")
    else:
        saves: list[dict] = []

        def route_capture(req):
            url = req.request.url.split("/page/", 1)[-1].split("?")[0]
            if req.request.method == "POST" and url == "settings/save":
                try:
                    saves.append(json.loads(req.request.post_data or "{}"))
                except Exception:                                     # noqa: BLE001
                    saves.append({})
                req.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"ok": True, "notice": "mock 已保存"}, ensure_ascii=False))
                return
            F.route(req)

        try:
            with F.sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(channel="msedge")
                except Exception:
                    browser = pw.chromium.launch()
                ctx = browser.new_context(viewport={"width": 1360, "height": 950})
                pg = ctx.new_page()
                errs: list[str] = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.route(F.API_GLOB, route_capture)
                pg.goto(F.PAGE_URL)
                pg.wait_for_timeout(1200)
                pg.click('button.tab[data-page="settings"]')
                pg.wait_for_timeout(700)
                pg.click("#btn_save_cfg_top")        # 主人点「保存全部设置」的那个按钮
                pg.wait_for_timeout(900)
                ctx.close()
                browser.close()

            check("点保存真的发出了 settings/save 请求", bool(saves))
            payload = (saves[-1].get("settings") if saves else None) or {}
            print(f"      真实载荷 {len(payload)} 个键")
            parsed, err = api._validate_settings(payload)
            check("真实载荷通过校验（无 error）", err is None, repr(err))
            ignored = sorted(k for k in payload if k not in (parsed or {}))
            check("★真实载荷没有一项会被后端丢弃（ignored 为空）", not ignored, "、".join(ignored))
            for k in ("server_version_override", "item_syntax_override"):
                check(f"真实载荷里的 {k} 真的被接受", k in (parsed or {}))
            check("浏览器无 pageerror", not errs, "; ".join(errs[:3]))
        except Exception as e:                                        # noqa: BLE001
            check(f"浏览器端到端可跑（{type(e).__name__}）", False, str(e)[:200])

    print("==========================================")
    if FAILED:
        print(f"FAILED {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("全部通过：前端提交的每个键后端都认，不会再误报「插件未重载」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
