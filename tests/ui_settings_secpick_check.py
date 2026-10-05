"""v0.23.7 回归：设置页三项单色的「格式化符号」下拉（真浏览器实跑）。

主人 2026-10-05 点名：设置那一页**最好也能单独选择格式化符号**，
但**取色器不必**（那里已经专门引导用户去服务器页『文本颜色』卡片设置）。

于是设置页三项单色各配一个 `§0`-`§f` 下拉：
  · 三个下拉**各自独立**（改一项绝不动另外两项）；
  · 选中即把「§+码」写进左边单色框（`§6` 与色名 `gold` 在后端 _parse_color 里等价）；
  · 反方向也同步：手打 hex / 色名 / §码 时下拉跟着对位（hex 走与后端同源的最近色名）；
  · 认不出的值（RGB() / 十进制 / 空）→ 下拉回到「（原样保留）」，**绝不改值、绝不清空**；
  · 设置页**不引入取色器**（`input[type=color]` 在设置页必须为 0）。

覆盖：
  1) 静态：三对 id 齐全（txt + sec）／设置页没有取色器／选项文案带 §码 + 中文 + 色名；
  2) 动态：三个下拉真渲染、可见、在 #page_settings 内、各 17 个选项（原样保留 + 16 符号）；
  3) 动态：初值对位（white → §f／gold → §6）；
  4) 动态：选中 §c 后文本框 == "§c"（点保存就会存这个值）；
  5) 动态：反向同步（#FFAA00 → §6；RGB(255,0,0) → 原样保留且值不变）；
  6) 动态：三项互不干扰（改第一项，后两项的值与下拉都不变）；
  7) 动态：端到端 —— 点「保存全部设置」，POST 载荷里的 color_say 真的是 §9；
  8) 动态：页面无 JS 异常（pageerror）。
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402  复用同一套假后端夹具与驱动补丁

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_CHANNEL, UI_LAUNCH_KWARGS  # noqa: E402  # 浏览器通道见 _paths（本机 Edge / CI bundled）

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"
PAIRS = [("cfg_color_say_t", "cfg_color_say_sec"),
         ("cfg_color_title_t", "cfg_color_title_sec"),
         ("cfg_color_fb_t", "cfg_color_fb_sec")]

FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    print("[1] 静态：三对 id 齐全、设置页无取色器、选项文案带 §码")
    for tid, sid in PAIRS:
        check(f"#{tid} 存在（单色文本框）", f'id="{tid}"' in src)
        check(f"#{sid} 存在（格式化符号下拉）", f'id="{sid}"' in src)
    set_block = src.split('id="page_settings"', 1)[1].split("<script", 1)[0]
    check("设置页块内没有 input[type=color]（取色器留在服务器页）",
          "type=\"color\"" not in set_block)
    check("选项文案 = §码 + 中文 + 色名（与服务器页同款）",
          '§${i.toString(16)} ${MC_NAME_CN[n]} ${n}' in src)
    check("三项配对表 SEC_PICKER_PAIRS 覆盖三对 id",
          all(f'"{tid}"' in src and f'"{sid}"' in src for tid, sid in PAIRS)
          and "SEC_PICKER_PAIRS" in src)

    print("\n[2] 动态：三个下拉真渲染、可见、在设置页内、各 17 个选项")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001 —— 兜底必须说出原因，否则真错误会被静默吞掉
            print(f"⚠ 通道 {UI_CHANNEL!r} 起不来（{e}），退回 playwright 自带 Chromium")
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        # 端到端用：抓「保存全部设置」真正发出去的载荷（[7] 里那串 §9 就是从这儿来的）
        sent: list[str] = []
        pg.on("request", lambda r: sent.append(r.post_data or "")
              if (r.method == "POST" and "settings/save" in r.url) else None)
        pg.route(F.API_GLOB, F.route)
        pg.goto(F.PAGE_URL)
        pg.wait_for_timeout(1000)
        pg.click('button.tab[data-page="settings"]')
        pg.wait_for_timeout(600)

        info = pg.evaluate(
            """(pairs) => pairs.map(([tid, sid]) => {
                const s = document.getElementById(sid), t = document.getElementById(tid);
                if(!s || !t) return null;
                return {sid, txt: t.value, val: s.value, n: s.options.length,
                        first: s.options[0] ? s.options[0].textContent : "",
                        visible: !!(s.offsetWidth || s.offsetHeight),
                        inSettings: !!s.closest('#page_settings'),
                        label15: [...s.options].find(o => o.value === 'f') ? 
                                 [...s.options].find(o => o.value === 'f').textContent : ""};
            })""", PAIRS)
        check("三个下拉全部拿到", all(info), str(info))
        for it in (info or []):
            if not it:
                continue
            check(f"#{it['sid']} 可见", it["visible"], str(it))
            check(f"#{it['sid']} 挂在设置页容器内", it["inSettings"], str(it))
            check(f"#{it['sid']} 选项 17 个（原样保留 + 16 符号，实际 {it['n']}）", it["n"] == 17)
            check(f"#{it['sid']} 首项是「（原样保留）」", it["first"] == "（原样保留）", it["first"])
            check(f"#{it['sid']} §f 文案带色名 white", "white" in it["label15"], it["label15"])

        print("\n[3] 初值对位：white → §f，gold → §6")
        want = [("cfg_color_say_sec", "f"), ("cfg_color_title_sec", "6"), ("cfg_color_fb_sec", "6")]
        for sid, code in want:
            got = pg.evaluate(f"() => document.getElementById('{sid}').value")
            check(f"#{sid} 默认选中 §{code}（实际 {got!r}）", got == code)

        print("\n[4] 选中即写入：§c → 文本框值 '§c'")
        pg.select_option("#cfg_color_say_sec", "c")
        pg.wait_for_timeout(120)
        got = pg.evaluate("() => document.getElementById('cfg_color_say_t').value")
        check("文本框被写成 §c（保存即存此值）", got == "§c", repr(got))

        print("\n[5] 反向同步：#FFAA00 → §6；RGB(255,0,0) → 原样保留且值不变")
        pg.fill("#cfg_color_title_t", "#FFAA00")
        pg.wait_for_timeout(120)
        got = pg.evaluate("() => document.getElementById('cfg_color_title_sec').value")
        check("hex 归到 §6（与后端同源的最近色名）", got == "6", repr(got))

        pg.fill("#cfg_color_fb_t", "RGB(255,0,0)")
        pg.wait_for_timeout(120)
        got = pg.evaluate("""() => [document.getElementById('cfg_color_fb_sec').value,
                                    document.getElementById('cfg_color_fb_t').value]""")
        check("认不出 → 回到「原样保留」", got[0] == "", repr(got))
        check("值没有被改写（还是 RGB(255,0,0)）", got[1] == "RGB(255,0,0)", repr(got))

        print("\n[6] 独立：改第一项，后两项的值与下拉都不动")
        before = pg.evaluate("""() => ['cfg_color_title_t','cfg_color_title_sec',
                                       'cfg_color_fb_t','cfg_color_fb_sec']
                                   .map(id => document.getElementById(id).value)""")
        pg.select_option("#cfg_color_say_sec", "9")
        pg.wait_for_timeout(120)
        after = pg.evaluate("""() => ['cfg_color_title_t','cfg_color_title_sec',
                                      'cfg_color_fb_t','cfg_color_fb_sec']
                                  .map(id => document.getElementById(id).value)""")
        said = pg.evaluate("() => document.getElementById('cfg_color_say_t').value")
        check("第一项写成 §9", said == "§9", repr(said))
        check("后两项一字未动", before == after, f"{before} → {after}")

        print("\n[7] 端到端：点「保存全部设置」→ 载荷里真的是 §9")
        pg.click("#btn_save_cfg_top")
        pg.wait_for_timeout(600)
        check("保存请求发出", bool(sent), str(len(sent)))
        check("载荷 color_say = §9（设置页读的就是这个框，不是副本）",
              any('"color_say":"§9"' in b or '"color_say": "§9"' in b for b in sent),
              str(sent[:1])[:200])

        print("\n[8] 页面无 JS 异常")
        check("pageerror 为空", not errs, str(errs[:2]))

        ctx.close()
        browser.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
