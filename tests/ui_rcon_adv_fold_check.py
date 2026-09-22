"""v0.22.11 回归：RCON 卡「进阶设置」折叠区（真浏览器实跑）。

为什么要有它：
  RCON 卡原先把「地址 / 端口 / 密码」之后的**全部**调参（超时 / 结束边界 /
  哨兵与静默窗口 / 版本与语法）平铺在卡里，普通用户既用不到、又被撑成一面墙。
  现在这些收进 `<details class="adv-fold">`（默认收起），点标题按需展开。

两条硬边界（本脚本看护的就是它们，防止「顺手全折起来」的回归）：
  A) 基础三件套（地址 / 端口 / 密码）必须在折叠区**外**、默认可见 ——
     它们才是每个人都要填的东西。
  B) 承载**安全警示**的状态行（#ver_caps_line / #rcon_runtime_line）与
     「重建 RCON 连接」按钮必须留在折叠区**外** —— 折叠起来等于把
     「版本未知 → 带数据的请求会被拒绝」「响应边界未确认」「已自动降级」
     这类警示藏掉，与「不削弱安全信息」的既定口径冲突。

覆盖：
  1) 静态：折叠区存在；折叠内 6 个调参控件齐全；状态行与按钮**不在**折叠内；
     基础三件套出现在折叠区之前；
  2) 动态：默认收起；基础三件套可见、折叠内控件不可见；
  3) 动态：点 summary → 展开，折叠内控件可见，卡片高度显著增加；
  4) 动态：收起态摘要徽标随值走（异地模式漏填版本 → 警示色 + 明确文案）；
  5) 动态：异地模式提示里的展开入口（openAdvRcon）真能展开并聚焦版本输入框；
  6) 动态：页面无 JS 异常（pageerror）。
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402  复用同一套假后端夹具与驱动补丁

from playwright.sync_api import sync_playwright  # noqa: E402

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"

FAILED: list[str] = []

# 折叠区内的调参控件
INSIDE = [
    "cfg_rcon_timeout", "cfg_rcon_end_mode", "cfg_rcon_probe_command",
    "cfg_rcon_idle_probe", "cfg_server_version", "cfg_item_syntax",
]
# 必须留在折叠区外的（安全警示 + 运维按钮）
OUTSIDE = ["ver_caps_line", "rcon_runtime_line", "btn_rcon_reset"]
BASIC = ["cfg_rcon_host", "cfg_rcon_port", "cfg_rcon_password"]

# 自定义开关（.sw）把 input 视觉隐藏了，只能改属性 + 派发事件
SET_CHECKED = """(sel) => {
  const el = document.querySelector(sel);
  el.checked = %s;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}"""


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    print("[1] 静态：折叠区的边界")
    m = re.search(r'<details class="adv-fold" id="adv_rcon">(.*?)</details>', src, re.S)
    check("折叠区 <details class='adv-fold' id='adv_rcon'> 存在", m is not None)
    inside = m.group(1) if m else ""
    for el in INSIDE:
        check(f"折叠内：{el}", el in inside)
    for el in OUTSIDE:
        check(f"★折叠**外**（安全警示 / 运维按钮不许被藏）：{el}", el not in inside)
    open_at = src.find('<details class="adv-fold" id="adv_rcon">')
    for el in BASIC:
        pos = src.find(f'id="{el}"')
        check(f"基础三件套 {el} 在折叠区之前（默认可见）", 0 < pos < open_at)
    check("收起态摘要徽标存在", 'id="adv_rcon_sum"' in src)
    check("展开入口挂在异地模式提示里（openAdvRcon）",
          'onclick="openAdvRcon(' in src and "function openAdvRcon" in src)

    print("\n[2] 动态：默认收起 + 可见性")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route(F.API_GLOB, F.route)
        pg.goto(F.PAGE_URL)
        pg.wait_for_timeout(1000)
        pg.click('button.tab[data-page="settings"]')
        pg.wait_for_timeout(600)

        check("默认收起（details.open === false）",
              pg.evaluate("() => document.getElementById('adv_rcon').open") is False)

        card_h_closed = pg.evaluate(
            "() => document.getElementById('adv_rcon').closest('.card').offsetHeight")
        print(f"     收起态卡片高度：{card_h_closed}px")

        for el in BASIC:
            check(f"收起态可见：{el}", pg.is_visible(f"#{el}"))
        for el in INSIDE:
            check(f"收起态不可见：{el}", not pg.is_visible(f"#{el}"))
        # 状态行有内容时才占高度，故按「不在折叠区内」判定（空 = 不显示，是设计）
        for el in ("ver_caps_line", "rcon_runtime_line"):
            check(f"★状态行 {el} 不在折叠区内（有内容时必然可见）",
                  pg.evaluate(f"() => !document.getElementById('{el}').closest('details.adv-fold')"))
        check("收起态仍可见：重建 RCON 连接按钮", pg.is_visible("#btn_rcon_reset"))

        print("\n[3] 动态：点标题展开")
        pg.click("#adv_rcon > summary")
        pg.wait_for_timeout(350)
        check("点 summary → 展开", pg.evaluate("() => document.getElementById('adv_rcon').open") is True)
        for el in INSIDE:
            check(f"展开后可见：{el}", pg.is_visible(f"#{el}"))
        card_h_open = pg.evaluate(
            "() => document.getElementById('adv_rcon').closest('.card').offsetHeight")
        print(f"     展开态卡片高度：{card_h_open}px（差 {card_h_open - card_h_closed}px）")
        check("★展开比收起明显更高（折叠真的省下了高度）",
              card_h_open - card_h_closed > 200, f"Δ={card_h_open - card_h_closed}")

        print("\n[4] 动态：收起态摘要随值走")
        base_sum = pg.evaluate("() => document.getElementById('adv_rcon_sum').textContent")
        check("默认摘要写明当前配置（超时 + 结束边界）",
              "超时" in base_sum and ("sentinel" in base_sum or "idle" in base_sum), base_sum)
        check("默认摘要不带警示色",
              pg.evaluate("() => document.getElementById('adv_rcon_sum').classList.contains('warn')") is False)

        # 异地模式 + 版本留空 = 必须补的事 → 收起态也要看得见（此刻展开着，便于填值）
        pg.evaluate(SET_CHECKED % "true", "#cfg_remote_rcon_mode")
        pg.fill("#cfg_server_version", "")
        pg.wait_for_timeout(300)
        warn_sum = pg.evaluate("() => document.getElementById('adv_rcon_sum').textContent")
        check("★异地模式漏填版本 → 摘要变警示色",
              pg.evaluate("() => document.getElementById('adv_rcon_sum').classList.contains('warn')") is True,
              warn_sum)
        check("★警示文案点名要补什么", "需手填服务端版本" in warn_sum, warn_sum)

        # 补上版本 → 警示解除
        pg.fill("#cfg_server_version", "1.20.1")
        pg.wait_for_timeout(250)
        check("填了版本 → 警示解除",
              pg.evaluate("() => document.getElementById('adv_rcon_sum').classList.contains('warn')") is False)
        pg.evaluate(SET_CHECKED % "false", "#cfg_remote_rcon_mode")
        pg.wait_for_timeout(200)

        # 收起后再看一次：徽标在 summary 里，收起态照样读得到
        pg.evaluate("() => { document.getElementById('adv_rcon').open = false; }")
        pg.wait_for_timeout(250)
        check("★收起态也能读到摘要（警示不被藏进折叠里）",
              pg.is_visible("#adv_rcon_sum")
              and "超时" in pg.evaluate("() => document.getElementById('adv_rcon_sum').textContent"))

        print("\n[5] 动态：异地模式提示里的展开入口")
        pg.evaluate("() => window.openAdvRcon('cfg_server_version')")
        pg.wait_for_timeout(400)
        check("openAdvRcon() 能展开折叠区",
              pg.evaluate("() => document.getElementById('adv_rcon').open") is True)
        check("openAdvRcon() 把焦点送到版本输入框",
              pg.evaluate("() => document.activeElement && document.activeElement.id") == "cfg_server_version")

        print("\n[6] 动态：无 JS 异常")
        check("页面无 pageerror", not errs, "; ".join(errs[:3]))

        ctx.close()
        browser.close()

    print("==========================================")
    if FAILED:
        print(f"FAILED {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("全部通过：RCON 卡收起态只留基础三件套 + 安全警示，调参按需展开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
