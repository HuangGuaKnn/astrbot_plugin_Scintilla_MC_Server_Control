# -*- coding: utf-8 -*-
"""v0.24.9 回归：嵌入 / 重排模型下拉「跟随开关失效」（真浏览器实跑）。

主人的要求（2026-10-28）：
  「重排序和嵌入模型开关关闭时，下面对应的选择模型下拉框也随即跟随失效，
    表现差不多就是变灰 + 无法互动。」

为什么值得一条硬门禁用例：
  这是典型的**隐性失效** —— 开关没开时下拉还亮着、还能选，用户选了却发现不生效，
  只会怀疑「是不是配错了」。把「能不能交互」跟「开没开」绑死，界面自己就不会骗人。

一个必须先搞清的事实（第一版用例在这里翻过车）：
  本页的下拉是**自定义卷帘控件**（`.select-pop` 包住原生 `<select>`，可见的是 `.select-trigger`，
  原生 select 被 `.select-native` 置成 `opacity:0; pointer-events:none`）。
  所以「变灰 + 点不开」必须作用在 wrapper / trigger 上；只设 `select.disabled` 用户**完全看不出来**。

判据（全部在真浏览器里量）：
  1) 开关关着 → 原生 select `disabled`、wrapper 带 `.is-locked` + `aria-disabled="true"`、
     容器带 `.is-off`，且 **trigger 计算透明度 < 1、cursor = not-allowed**（看得见的变灰）；
  2) **真的点不开**：点 trigger 之后 `.is-open` 仍然不存在（不是「样子灰了但还能点」）；
  3) 两个开关**各管各的**：只开语义 → 嵌入能点开、重排仍点不开；
  4) 再关回去 → 立刻回到锁死（不是一次性初始化）；
  5) 页面无 JS 异常。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402  复用同一套假后端夹具与驱动补丁

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_LAUNCH_KWARGS  # noqa: E402

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"
SHOTS = F.SHOTS

PAIRS = (
    ("cfg_kb_semantic", "cfg_kb_embed_p"),
    ("cfg_kb_rerank", "cfg_kb_rerank_p"),
)

STATE_JS = r"""
() => {
  // 真身是自定义下拉：.select-pop（wrapper）+ .select-trigger（可见按钮）；原生 <select> 是视觉隐藏的
  const out = {};
  for (const [swId, selId] of [["cfg_kb_semantic","cfg_kb_embed_p"],
                               ["cfg_kb_rerank","cfg_kb_rerank_p"]]) {
    const sw = document.getElementById(swId);
    const sel = document.getElementById(selId);
    if (!sw || !sel) { out[swId] = { err: "missing" }; continue; }
    const box = sel.closest(".kb-prov");
    const pop = sel.closest(".select-pop");
    const trg = pop ? pop.querySelector(".select-trigger") : null;
    const tcs = trg ? getComputedStyle(trg) : null;
    out[swId] = {
      on: !!sw.checked,
      nativeDisabled: sel.disabled,
      nativeAria: sel.getAttribute("aria-disabled"),
      boxOff: !!(box && box.classList.contains("is-off")),
      locked: !!(pop && pop.classList.contains("is-locked")),
      popAria: pop ? pop.getAttribute("aria-disabled") : null,
      triggerOpacity: tcs ? parseFloat(tcs.opacity) : null,
      triggerCursor: tcs ? tcs.cursor : null,
      isOpen: !!(pop && pop.classList.contains("is-open")),
      hasTitle: sel.hasAttribute("title"),
    };
  }
  return out;
}
"""

FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def _set_switch(pg, sw_id: str, on: bool) -> None:
    """把开关点成想要的状态（幂等）。

    真实的 `<input type=checkbox>` 在 `.sw` 里是**视觉隐藏**的（靠 `span.sl` 画开关），
    直接点 input 会被 Playwright 判「not visible」而超时 —— 点它的 `<label>` 才对。
    """
    sw = pg.locator(f"#{sw_id}")
    if bool(sw.is_checked()) != on:
        sw.locator("xpath=ancestor::label[1]").click()
        pg.wait_for_timeout(220)


def _trigger(pg, sel_id: str):
    return pg.locator(f"#{sel_id}").locator("xpath=ancestor::div[contains(@class,'select-pop')]").locator(".select-trigger")


def _click_trigger(pg, sel_id: str) -> None:
    """点可见的卷帘下拉按钮。

    用 `force=True`：锁住时我们给 wrapper 打了 `aria-disabled="true"`（无障碍语义正确），
    Playwright 的可操作性检查会因此判「element is not enabled」而拒绝点击 ——
    但那恰恰是要验的场景：**强行点也不该展开**。真实鼠标点击不受 aria 阻拦，
    拦它的是 `setOpen()` 里的 `is-locked` 判断，所以这里必须把点击真的发出去。
    """
    _trigger(pg, sel_id).click(force=True)
    pg.wait_for_timeout(220)


def _opacity_ok(v) -> bool:
    return isinstance(v, (int, float)) and v < 1


def main() -> int:
    src = HTML.read_text(encoding="utf-8")

    print("[1] 静态：接线、锁与样式都在")
    check("两个容器都带 .kb-prov", src.count('class="kb-prov"') >= 2, src.count('class="kb-prov"'))
    check("容器用 data-kb-switch 指明跟哪个开关",
          'data-kb-switch="cfg_kb_semantic"' in src and 'data-kb-switch="cfg_kb_rerank"' in src)
    check("有同步函数 syncKbProviderLocks", "function syncKbProviderLocks()" in src)
    check("有下拉锁 lockAnimatedSelect（自定义下拉必须走它）", "function lockAnimatedSelect(" in src)
    check("卷帘控件的 setOpen 认 is-locked（点／键盘都打不开）",
          'next = !!open && !wrap.classList.contains("is-locked")' in src)
    check("两个开关都挂了 change 监听",
          '["cfg_kb_semantic","cfg_kb_rerank"]' in src
          and 'addEventListener("change", syncKbProviderLocks)' in src)
    check("包完卷帘控件后会再同步一次（否则锁不上 wrapper）",
          "if(typeof syncKbProviderLocks === \"function\") syncKbProviderLocks();" in src)
    check("样式里有 .kb-prov.is-off 的变灰规则", ".kb-prov.is-off" in src)

    print("\n[2] 动态：真浏览器里验行为")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001 —— 兜底必须说出原因
            print(f"⚠ 通道起不来（{e}），退回自带 Chromium")
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route(F.API_GLOB, F.route)
        pg.goto(F.PAGE_URL)
        pg.wait_for_timeout(700)
        for sel in ('[data-page="settings"]', "#nav_settings", "text=设置"):
            try:
                if pg.locator(sel).count():
                    pg.click(sel, timeout=1500)
                    break
            except Exception:  # noqa: BLE001
                pass
        pg.wait_for_timeout(700)

        print("      [2.1] 两个开关都关 → 两个下拉都该锁死（灰 + 点不开）")
        _set_switch(pg, "cfg_kb_semantic", False)
        _set_switch(pg, "cfg_kb_rerank", False)
        st = pg.evaluate(STATE_JS)
        for sw_id, sel_id in PAIRS:
            s = st.get(sw_id) or {}
            check(f"{sel_id}：原生 select disabled", s.get("nativeDisabled") is True, s)
            check(f"{sel_id}：wrapper 带 .is-locked", s.get("locked") is True, s)
            check(f"{sel_id}：wrapper aria-disabled=true", s.get("popAria") == "true", s)
            check(f"{sel_id}：容器带 .is-off", s.get("boxOff") is True, s)
            check(f"{sel_id}：看得见的变灰（trigger opacity<1）",
                  _opacity_ok(s.get("triggerOpacity")), s)
            check(f"{sel_id}：鼠标样式 not-allowed", s.get("triggerCursor") == "not-allowed", s)
            check(f"{sel_id}：给了 title 提示", s.get("hasTitle") is True, s)

        print("      [2.2] 真的改不了（不是样子灰了还能改）")
        for _, sel_id in PAIRS:
            _click_trigger(pg, sel_id)
            st = pg.evaluate(STATE_JS)
            check(f"{sel_id}：点 trigger 后菜单仍不展开",
                  st["cfg_kb_semantic"]["isOpen"] is False and st["cfg_kb_rerank"]["isOpen"] is False,
                  st)

        print("      [2.2b] 键盘路径也堵住了（外部复核 P1：方向键 → 回车曾能把灰掉的下拉改掉）")
        before = pg.evaluate(
            """() => ({a: document.getElementById('cfg_kb_embed_p').value,
                      b: document.getElementById('cfg_kb_rerank_p').value})""")
        for sel_id in ("cfg_kb_embed_p", "cfg_kb_rerank_p"):
            trg = _trigger(pg, sel_id)
            trg.focus()
            for key in ("ArrowDown", "ArrowDown", "Enter", "Enter"):
                pg.keyboard.press(key)
                pg.wait_for_timeout(120)
            st = pg.evaluate(STATE_JS)
            key_id = "cfg_kb_semantic" if sel_id == "cfg_kb_embed_p" else "cfg_kb_rerank"
            check(f"{sel_id}：方向键+回车后菜单仍未展开", st[key_id]["isOpen"] is False, st[key_id])
        after = pg.evaluate(
            """() => ({a: document.getElementById('cfg_kb_embed_p').value,
                      b: document.getElementById('cfg_kb_rerank_p').value})""")
        check("★键盘操作后两个下拉的值都没被改", before == after, f"{before} → {after}")

        print("      [2.3] 只开语义 → 嵌入能点开、重排仍点不开（各管各的）")
        _set_switch(pg, "cfg_kb_semantic", True)
        st = pg.evaluate(STATE_JS)
        check("嵌入：不再锁定", st["cfg_kb_semantic"]["locked"] is False, st["cfg_kb_semantic"])
        check("嵌入：透明度回到 1",
              (st["cfg_kb_semantic"]["triggerOpacity"] or 0) >= 0.99, st["cfg_kb_semantic"])
        check("重排：仍锁定", st["cfg_kb_rerank"]["locked"] is True, st["cfg_kb_rerank"])
        _click_trigger(pg, "cfg_kb_embed_p")
        st1 = pg.evaluate(STATE_JS)
        check("嵌入：点得开（.is-open 出现）", st1["cfg_kb_semantic"]["isOpen"] is True, st1["cfg_kb_semantic"])
        _click_trigger(pg, "cfg_kb_embed_p")   # 再点一次收起，免得影响后续
        _click_trigger(pg, "cfg_kb_rerank_p")
        st2 = pg.evaluate(STATE_JS)
        check("重排：点不开（.is-open 仍不出现）", st2["cfg_kb_rerank"]["isOpen"] is False, st2["cfg_kb_rerank"])

        print("      [2.4] 再开重排 → 两个都能点开")
        _set_switch(pg, "cfg_kb_rerank", True)
        st = pg.evaluate(STATE_JS)
        check("重排：不再锁定", st["cfg_kb_rerank"]["locked"] is False, st["cfg_kb_rerank"])
        check("两个容器都没有 .is-off",
              not st["cfg_kb_semantic"]["boxOff"] and not st["cfg_kb_rerank"]["boxOff"], st)

        print("      [2.5] 再关回去 → 立刻锁死（不是一次性初始化）")
        _set_switch(pg, "cfg_kb_rerank", False)
        st = pg.evaluate(STATE_JS)
        check("重排：又锁定", st["cfg_kb_rerank"]["locked"] is True, st["cfg_kb_rerank"])
        check("嵌入：没被误伤", st["cfg_kb_semantic"]["locked"] is False, st["cfg_kb_semantic"])

        print("      [2.6] 截图留档（观察用）")
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            box = pg.locator(".kb-card").first
            if box.count():
                box.screenshot(path=str(SHOTS / "kb_dependent_off.png"))
                print(f"      {SHOTS / 'kb_dependent_off.png'}")
        except Exception as e:  # noqa: BLE001
            print(f"      截图失败（不影响判据）：{e}")

        print("      [2.7] 页面无 JS 异常")
        check("pageerror 为空", not errs, str(errs[:2]))

        ctx.close()
        browser.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("✅ 全部通过（开关关 → 下拉变灰且点不开；两个开关各管各的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
