"""UI 实跑：插件页浅色模式 + 顶栏右上角切换按钮。

真浏览器（Edge + 假后端）验这几件事：
  1) 无存档时跟随系统 prefers-color-scheme（浅色系统 → 浅色，深色系统 → 深色）；
  2) 点右上角按钮 → <html data-theme> 真的换、按钮文案换成「点一下会变成什么」、
     localStorage 记住选择；刷新后仍是选过的那个主题（不闪回深色）；
  3) 两套主题都做对比度审计：遍历六个页签里所有「有自己文字」的元素，
     按 WCAG 宽松线（正文 4.0 / 大字粗体 3.0）找低对比度项 —— 浅色不能比深色更糟；
  4) 六个页签 × 两套主题截图（人眼复核用）。

跑法：python tests\\ui_theme_check.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 ASTRBOT_APP_DIR 指路）
"""
from __future__ import annotations

import json
import os
import pathlib

# 本机 AstrBot 内嵌 Python 的 site-packages 在 sys.path 里带 \\?\ 前缀，
# Playwright 的 node 驱动拿它当入口脚本会解析成 "C:" 而崩 → 先剥掉前缀。
def _patch_pw_driver() -> None:
    import playwright._impl._transport as t
    orig = t.compute_driver_executable

    def patched():
        node, cli = orig()
        return (node.replace("\\\\?\\", ""), cli.replace("\\\\?\\", ""))

    t.compute_driver_executable = patched


_patch_pw_driver()

from playwright.sync_api import sync_playwright  # noqa: E402

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
PAGE_URL = os.environ.get("THEME_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()
API_GLOB = "**/api/v1/plugins/extensions/astrbot_plugin_Scintilla_MC_Server_Control/page/**"
SHOTS = PLUGIN / "tests" / "_shots"

FP = "e38d99c65853"
TABS = ["overview", "server", "knowledge", "workflow", "prompts", "settings"]

ENTRY = {
    "topic": "tac 满改 mp5 方案", "mod": "tac", "kind": "template",
    "content": "【模板】TAC 枪械满改通用规则：槽位键名用大写驼峰（Scope/Under_Barrel/Muzzle/Magazine），\n"
               "配件必须来自枪械工作台；弹药单独给。",
    "status": "verified", "enabled": True, "source": "webui",
    "created_at": "2026-09-11 10:00:00", "updated_at": "2026-09-12 11:00:00",
}
PEND = dict(ENTRY, topic="create 蒸汽机配方", status="untested", kind="instance",
            content="铜板 ×2 + 安山岩合金板 ×1 → 蒸汽机 ×1（自动应用关闭，等待审批）")

EVENTS = [
    {"ts": "15:02:11", "type": "join", "player": "Steve", "detail": "进入了服务器"},
    {"ts": "15:03:40", "type": "chat", "player": "Steve", "detail": "有人吗"},
    {"ts": "15:07:02", "type": "death", "player": "Steve", "detail": "被苦力怕炸死了"},
]


def overview() -> dict:
    return {
        "ok": True, "version": "v0.21.39",
        "plugin": {"name": "astrbot_plugin_Scintilla_MC_Server_Control", "display_name": "Minecraft Server 智控台",
                   "version": "0.21.39", "author": "HuangGuaKnn",
                   "desc": "通过 RCON 用自然语言操控 Minecraft 服务器并具备高度完备的 WebUI：内置多 Agent 工作流处理整合包复杂任务；转发玩家进出/聊天/死亡/成就/对话/指令；平台指令组 mcs 支持绑定、喊话、状态查询与踢人封禁（管理员）。"},
        "rcon": {"ok": True, "host": "127.0.0.1", "port": 25575, "configured": True},
        "dictionary": {"enabled": True, "mods": 185, "items": 11894},
        "knowledge": {"enabled": True, "state": {"enabled": True}, "stats": {"total": 6, "verified": 4},
                      "preset_name": "默认知识库", "fingerprint": FP, "match": True,
                      "notice_show": False, "fingerprint_mismatch": False, "server_id": FP},
        "workflow": {"enabled": True, "recent_total": 15, "recent_done": 13, "recent": []},
        "listener": {"running": True, "enabled": True},
        "events": EVENTS,
        "config": {"rcon_configured": True, "admin_ids": ["Steve", "123456789"]},
    }


def kb_state() -> dict:
    return {
        "ok": True,
        "state": {"enabled": True, "learning": True, "auto_apply": False},
        "stats": {"total": 6, "verified": 4, "pending": 1, "corrected": 0, "untested": 1},
        "entries": [dict(ENTRY)], "pending": [dict(PEND)],
        "server_id": FP, "active_preset": "p_default",
        "presets": [{"id": "p_default", "name": "默认知识库", "fingerprint": FP, "bound": True,
                     "active": True, "entries": 1, "match": True}],
        "notice": None,
    }


def presets() -> dict:
    return {
        "ok": True, "active": "p_default", "server_fingerprint": FP,
        "notice": {"show": False, "changed": False, "mismatch": False, "key": "p_default|" + FP,
                   "server_fp": FP, "preset_id": "p_default", "preset_name": "默认知识库",
                   "preset_fp": FP, "server_fp_weak": False, "server_dir": ""},
        "presets": [{"id": "p_default", "name": "默认知识库", "fingerprint": FP, "bound": True,
                     "active": True, "entries": 1, "match": True}],
    }


def server_status() -> dict:
    return {"ok": True, "version": "1.20.1", "daytime": "白天 13:20", "mspt": 12.4, "tps": 20.0,
            "uptime": "3 天 4 小时", "mem": "2.1 / 4.0 GB", "cpu": "18%",
            "list": {"online": 1, "max": 20, "players": ["Steve"]}}


def prompts() -> dict:
    roles = [("classifier", "分类器", "任务分类 Agent"), ("judge", "模板判断", "模板判断 Agent"),
             ("engineer", "模板工程师", "前瞻建库 Agent"), ("implementer", "实现器", "执行 Agent"),
             ("corrector", "纠错器", "校验 Agent")]
    return {"ok": True, "total": 5, "custom_count": 1,
            "agents": [{"role": r, "short": s, "name": n, "custom": r == "implementer",
                        "content": f"你是{n}。请按流水线要求处理主人的任务。"} for r, s, n in roles]}


#: v0.23.3：内置话题词表（假后端给几个即可，用来验证「恢复内置词表」按钮的填充链路）
HINT_KW_DEFAULT = ["mc", "minecraft", "我的世界", "服务器", "满配", "附魔"]


def settings() -> dict:
    return {
        "ok": True, "wake_prefix": "",
        # v0.23.3：内置话题词表随接口下发（前端「恢复内置词表」按钮的填充源）
        "hint_keywords_default": list(HINT_KW_DEFAULT),
        "platforms": [{"id": "aiocqhttp", "name": "QQ (aiocqhttp)"},
                      {"id": "webchat", "name": "WebChat"}],
        "settings": {"rcon_host": "127.0.0.1", "rcon_port": 25575, "server_dir": r"D:\Minecraft\Servers\MyPack",
                     "admin_ids": ["Steve", "123456789"], "danger_command_policy": "whitelist",
                     # v0.23.3：权限前置提醒的触发时机 + 话题词表（模拟真实配置里已有这两项）
                     "permission_hint_mode": "on_demand",
                     "permission_hint_keywords": list(HINT_KW_DEFAULT),
                     "cmd_enabled": True, "tool_enabled": True, "kb_enabled": True, "kb_learning": True,
                     "kb_auto_apply": False, "wf_enabled": True, "wf_tool_enabled": True,
                     "listener_enabled": True, "bridge_enabled": True, "broadcast_targets": ["aiocqhttp:FriendMessage:123"],
                     "event_types": ["join", "leave", "chat", "death"], "grad_fmt": "legacy"},
    }


def colors() -> dict:
    return {"ok": True,
            "title": {"fmt": "gradient", "stops": ["#FFAA00", "#FF00FF"], "bold": True},
            "say": {"fmt": "gradient", "stops": ["#FFFFFF", "#55FFFF"], "bold": False},
            "fb": {"fmt": "gradient", "stops": ["#FFAA00", "#55FF55"], "bold": False},
            "samples": {}}


def workflow_status() -> dict:
    return {"ok": True, "enabled": True, "tool_enabled": True, "main_provider": "自动",
            "logs": [{"ts": "15:01:20", "kind": "满配枪械", "task": "满配一把 M4A1", "status": "done", "ms": 8200, "agent": "implementer"},
                     {"ts": "15:04:40", "kind": "模组物品", "task": "给 Steve 发黄铜锭", "status": "failed", "ms": 5100, "agent": "classifier"},
                     {"ts": "15:06:10", "kind": "配比计算", "task": "算蒸汽机配方", "status": "running", "ms": 1200, "agent": "engineer"}]}


GETS = {"overview": overview, "state": kb_state, "kb/presets": presets,
        "server/status": server_status, "events": lambda: {"ok": True, "events": EVENTS},
        "prompts": prompts, "settings": settings, "colors": colors,
        "workflow/status": workflow_status}


def route(route_obj):
    url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
    fn = GETS.get(url, lambda: {"ok": False, "error": "mock: 未实现 " + url})
    route_obj.fulfill(status=200, content_type="application/json",
                      body=json.dumps(fn(), ensure_ascii=False))


# ---- 对比度审计：遍历页面上「自己有文字」的元素，算文字色 vs 有效背景色的对比度 ----
AUDIT_JS = r"""
() => {
  const parse = c => { const m = String(c).match(/rgba?\(([^)]+)\)/); if(!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x)); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (f, b) => [0, 1, 2].map(i => f[i] * f[3] + b[i] * (1 - f[3]));
  const lum = rgb => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]); };
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05); };
  const effBg = el => {                      // 逐级向上叠（半透明背景 → 合成到底色）
    const stack = []; let node = el;
    while (node) { const p = parse(getComputedStyle(node).backgroundColor);
      if (p && p[3] > 0) { stack.push(p); if (p[3] >= 1) break; }
      node = node.parentElement; }
    let bg = [255, 255, 255];
    for (let i = stack.length - 1; i >= 0; i--) bg = over(stack[i], bg);
    return bg;
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (parseFloat(cs.opacity) < 0.5) continue;          // 故意做灰的禁用态不参与
    if (!el.getClientRects().length) continue;
    const own = [...el.childNodes].filter(n => n.nodeType === 3)
                  .map(n => n.textContent.trim()).join('');
    if (!own) continue;
    const fg = parse(cs.color); if (!fg || fg[3] < 0.5) continue;
    const bg = effBg(el), r = ratio(over(fg, bg), bg);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight, 10) >= 600;
    const need = (size >= 18.66 || (size >= 14 && bold)) ? 3.0 : 4.0;
    if (r < need) out.push({ cls: String(el.className).slice(0, 34), tag: el.tagName.toLowerCase(),
      txt: own.slice(0, 16), r: Math.round(r * 100) / 100, size: size, need: need });
  }
  return out;
}
"""

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def theme_of(page) -> str:
    return page.evaluate("document.documentElement.dataset.theme")


def audit(page) -> list[dict]:
    rows: list[dict] = []
    for tab in TABS:
        page.click(f'button.tab[data-page="{tab}"]')
        page.wait_for_timeout(220)
        for row in page.evaluate(AUDIT_JS):
            row["tab"] = tab
            rows.append(row)
    return rows


def report(title: str, rows: list[dict]) -> None:
    buckets = {"<2.5": 0, "2.5-3.0": 0, "3.0-3.5": 0, "3.5-4.0": 0}
    for r in rows:
        key = "<2.5" if r["r"] < 2.5 else "2.5-3.0" if r["r"] < 3.0 else "3.0-3.5" if r["r"] < 3.5 else "3.5-4.0"
        buckets[key] += 1
    print(f"    {title}：低于宽松线 {len(rows)} 项 · 分布 " +
          " ".join(f"{k}={v}" for k, v in buckets.items()))
    for row in sorted(rows, key=lambda r: r["r"])[:8]:
        print(f"      {row['r']:>4} (需 {row['need']}) {row['tab']}/{row['tag']}.{row['cls']} "
              f"[{row['size']}px] «{row['txt']}»")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()

        # ---------- 1) 无存档：跟随系统 ----------
        print("[1] 首次打开（没有存档）跟随系统偏好")
        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="light")
        page = ctx.new_page()
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        page.wait_for_timeout(900)
        check("系统浅色 → 页面浅色", theme_of(page) == "light", theme_of(page))
        check("深色主题下 --bg 是浅色", page.evaluate(
            "getComputedStyle(document.body).backgroundColor") == "rgb(244, 245, 247)",
            page.evaluate("getComputedStyle(document.body).backgroundColor"))
        check("按钮文案告诉主人「点一下会变成什么」",
              page.inner_text("#theme_txt").strip() == "深色" and page.inner_text("#theme_ico").strip() == "☾",
              page.inner_text("#themebtn").strip())
        page.screenshot(path=str(SHOTS / "theme_light_auto.png"), full_page=False)
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="dark")
        page = ctx.new_page()
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        page.wait_for_timeout(900)
        check("系统深色 → 页面深色", theme_of(page) == "dark", theme_of(page))
        ctx.close()

        # ---------- 2) 点按钮切换 + 记住 ----------
        print("[2] 右上角按钮：点一下就换，且记得住")
        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="dark")
        page = ctx.new_page()
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        page.wait_for_timeout(900)
        check("按钮在顶栏右上角（top-right 里，且可见）",
              page.eval_on_selector("#themebtn", "el => !!el.closest('.top-right') && el.getClientRects().length > 0"))
        check("按钮在顶栏右侧（右边缘贴着容器右侧）", page.eval_on_selector(
            "#themebtn",
            "el => { const b = el.getBoundingClientRect(), t = el.closest('.top-right').getBoundingClientRect();"
            "        return b.right <= t.right + 1 && b.left > t.left + t.width * 0.3; }"))
        page.click("#themebtn")
        page.wait_for_timeout(250)
        check("点一下 → 浅色", theme_of(page) == "light", theme_of(page))
        check("localStorage 记住 light",
              page.evaluate("localStorage.getItem('mcctrl_theme')") == "light")
        check("卡片底色换成浅色变量（不再是深色面板）",
              page.eval_on_selector("#page_overview.active .card",
                                    "el => getComputedStyle(el).backgroundColor") == "rgb(255, 255, 255)",
              page.eval_on_selector("#page_overview.active .card", "el => getComputedStyle(el).backgroundColor"))
        page.reload()
        page.wait_for_timeout(900)
        check("刷新后仍是浅色（有存档就不跟系统了）", theme_of(page) == "light", theme_of(page))
        page.click("#themebtn")
        page.wait_for_timeout(250)
        check("再点一下 → 回深色", theme_of(page) == "dark", theme_of(page))
        check("localStorage 记住 dark",
              page.evaluate("localStorage.getItem('mcctrl_theme')") == "dark")
        page.reload()
        page.wait_for_timeout(900)
        check("刷新后仍是深色", theme_of(page) == "dark", theme_of(page))

        # ---------- 3) 两套主题的对比度审计 ----------
        print("[3] 对比度审计（六个页签，正文线 4.0 / 大字粗体线 3.0）")
        dark_rows = audit(page)
        report("深色（基线）", dark_rows)
        page.evaluate("localStorage.setItem('mcctrl_theme','light')")
        page.reload()
        page.wait_for_timeout(900)
        light_rows = audit(page)
        report("浅色（本次）", light_rows)
        hard = [r for r in light_rows if r["r"] < 2.5]
        check("浅色没有「几乎看不清」的项（< 2.5 全灭）", not hard,
              "；".join(f"{r['tab']}.{r['cls']}={r['r']}" for r in hard[:5]))
        worst_l = min((r["r"] for r in light_rows), default=99)
        worst_d = min((r["r"] for r in dark_rows), default=99)
        check("浅色最差项不比深色最差项更差", worst_l >= worst_d,
              f"light={worst_l} dark={worst_d}")
        check("浅色「明显偏低」（< 3.0）的项不多于深色",
              sum(1 for r in light_rows if r["r"] < 3.0) <= sum(1 for r in dark_rows if r["r"] < 3.0),
              f"light={sum(1 for r in light_rows if r['r'] < 3.0)} "
              f"dark={sum(1 for r in dark_rows if r['r'] < 3.0)}")

        # ---------- 4) 截图留档 ----------
        print("[4] 六个页签 × 两套主题截图")
        for theme in ("light", "dark"):
            page.evaluate(f"localStorage.setItem('mcctrl_theme','{theme}')")
            page.reload()
            page.wait_for_timeout(900)
            for tab in TABS:
                page.click(f'button.tab[data-page="{tab}"]')
                page.wait_for_timeout(260)
                page.screenshot(path=str(SHOTS / f"theme_{theme}_{tab}.png"))
        print(f"    截图目录：{SHOTS}")
        ctx.close()
        browser.close()

    print()
    if failed:
        print(f"✗ 失败 {len(failed)} 项：" + "；".join(failed))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
