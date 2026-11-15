# -*- coding: utf-8 -*-
"""模型下拉回归。用真实浏览器验开关联动、可见提示、焦点和菜单状态。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ui_theme_check as F  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_LAUNCH_KWARGS  # noqa: E402

HTML = F.PLUGIN / "pages" / "mc_control" / "index.html"
PAGE_URL = "http://mc-control.test/index.html"
PAIRS = (
    ("cfg_kb_semantic", "cfg_kb_embed_p"),
    ("cfg_kb_rerank", "cfg_kb_rerank_p"),
)
MASTER = "cfg_kb_master"
WATCHED = [MASTER, *(sw for sw, _ in PAIRS)]

# 仅在测试页面上暴露闭包函数，允许不触发 document click，直接锁定展开中的菜单。
TEST_HOOKS = """
window.__kbTest = {lock: lockAnimatedSelect, handler: syncKbProviderLocks, loads: 0};
const originalLoadSettings = loadSettings;
loadSettings = async function(){
  await originalLoadSettings();
  ++window.__kbTest.loads;
};
"""

WATCH_CHANGE_JS = """
(ids => {
  const watched = new Set(ids);
  const adds = window.__kbChangeAdds = Object.fromEntries(ids.map(id => [id, 0]));
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function(type, ...args){
    if(type === "change" && watched.has(this.id)) ++adds[this.id];
    return add.call(this, type, ...args);
  };
})
"""

STATE_JS = """
(select => {
  const box = select.closest(".kb-prov");
  const pop = select.closest(".select-pop");
  const trigger = pop.querySelector(".select-trigger");
  let opacity = 1;
  for(let el = trigger; el && box.contains(el); el = el.parentElement){
    opacity *= Number(getComputedStyle(el).opacity);
  }
  return {
    nativeDisabled: select.disabled,
    boxOff: box.classList.contains("is-off"),
    locked: pop.classList.contains("is-locked"),
    popAria: pop.getAttribute("aria-disabled"),
    triggerOpacity: opacity,
    triggerCursor: getComputedStyle(trigger).cursor,
    triggerTabIndex: trigger.tabIndex,
    isOpen: pop.classList.contains("is-open"),
    expanded: trigger.getAttribute("aria-expanded"),
    optionTabStops: Array.from(pop.querySelectorAll(".select-option"), opt => opt.tabIndex),
    value: select.value,
  };
})
"""

FAILED: list[str] = []


def check(desc: str, cond: bool, extra="") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def _route(route) -> None:
    if route.request.url.endswith("/kb/models"):
        def channel(kind):
            return {"active": f"{kind}-1", "choices": [{"id": f"{kind}-1"}, {"id": f"{kind}-2"}]}

        route.fulfill(json={"ok": True, "embedding": channel("embed"), "rerank": channel("rerank")})
    else:
        F.route(route)


def _set_switch(pg, sw_id: str, on: bool) -> None:
    sw = pg.locator(f"#{sw_id}")
    if sw.is_checked() != on:
        # 隐藏的 input 由可见 label 接收鼠标操作。
        sw.locator("xpath=ancestor::label[1]").click()


def _trigger(pg, sel_id: str):
    return pg.locator(f"#{sel_id}").locator("xpath=ancestor::div[contains(@class,'select-pop')]").locator(".select-trigger")


def _click_trigger(pg, sel_id: str) -> None:
    trigger = _trigger(pg, sel_id)
    trigger.evaluate("el => el.scrollIntoView({block:'center'})")
    # 真实鼠标点击不被 aria-disabled 拦住，force 只跳过 Playwright 的 enabled 检查。
    trigger.click(force=True)


def _state(pg, sel_id: str) -> dict:
    return pg.locator(f"#{sel_id}").evaluate(STATE_JS)


def _focus_state(pg, sel_id: str) -> dict:
    trigger = _trigger(pg, sel_id)
    pg.keyboard.press("Tab")
    return trigger.evaluate("""async el => {
        el.focus({preventScroll:true});
        getComputedStyle(el).boxShadow;
        await Promise.all(el.getAnimations().map(animation => animation.finished.catch(() => {})));
        return {
          focused: document.activeElement === el && el.matches(":focus-visible"),
          boxShadow: getComputedStyle(el).boxShadow,
          outline: getComputedStyle(el).outlineStyle,
        };
    }""")


def _lock_hint(pg, sel_id: str) -> dict:
    return _trigger(pg, sel_id).evaluate("""el => {
        el.scrollIntoView({block:"center"});
        const hitTest = node => {
          const rect = node.getBoundingClientRect();
          return node.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
        };
        const hint = document.getElementById(el.getAttribute("aria-describedby"));
        return {
          triggerHit: hitTest(el),
          hintHit: !!hint && hitTest(hint),
          text: hint ? hint.textContent.trim() : "",
        };
    }""")


def _load_settings_tab(pg) -> None:
    previous = pg.evaluate("window.__kbTest.loads")
    pg.locator('button.tab[data-page="settings"]').click()
    pg.wait_for_function("previous => window.__kbTest.loads > previous", arg=previous)


def _bindings(pg) -> dict:
    return pg.evaluate("""ids => Object.fromEntries(ids.map(id => {
        const propertyHandler = document.getElementById(id).onchange === window.__kbTest.handler;
        return [id, {propertyHandler, addCalls: window.__kbChangeAdds[id]}];
    }))""", WATCHED)


def main() -> int:
    src = HTML.read_text(encoding="utf-8")
    entry = '"use strict";\nconst bridge ='
    if src.count(entry) != 1:
        raise AssertionError("找不到设置页脚本入口，无法安装浏览器测试探针")
    src = src.replace(entry, '"use strict";\n' + TEST_HOOKS + "\nconst bridge =")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 通道起不来（{e}），退回自带 Chromium")
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, reduced_motion="reduce")
        ctx.add_init_script(f"{WATCH_CHANGE_JS}({WATCHED!r})")
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route(PAGE_URL, lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=src))
        pg.route("**/bridge-sdk.js", lambda route: route.fulfill(content_type="text/javascript", body=""))
        pg.route(F.API_GLOB, _route)
        pg.goto(PAGE_URL)
        pg.wait_for_function("window.__kbTest.loads >= 1")
        _load_settings_tab(pg)
        pg.wait_for_function("document.getElementById('cfg_kb_rerank_p').options.length >= 3")

        print("[1] 单项开关关闭时，下拉不可用且有可见说明")
        _set_switch(pg, MASTER, True)
        for sw_id, sel_id in PAIRS:
            _set_switch(pg, sw_id, False)
            st = _state(pg, sel_id)
            check(f"{sel_id}：锁定同步到原生控件、wrapper 和容器",
                  st["nativeDisabled"] and st["locked"] and st["popAria"] == "true" and st["boxOff"], st)
            check(f"{sel_id}：锁定透明度只淡化一次",
                  0.35 < st["triggerOpacity"] < 0.55, st["triggerOpacity"])
            check(f"{sel_id}：锁定 trigger 移出 Tab 顺序", st["triggerTabIndex"] == -1, st["triggerTabIndex"])
            check(f"{sel_id}：鼠标样式 not-allowed", st["triggerCursor"] == "not-allowed", st["triggerCursor"])
            hint = _lock_hint(pg, sel_id)
            label = "语义增强检索" if sw_id == "cfg_kb_semantic" else "重排序精排"
            check(f"{sel_id}：命中可见触发器和解锁提示",
                  hint["triggerHit"] and hint["hintHit"] and f"打开「{label}」开关后可选择模型" in hint["text"], hint)
            _click_trigger(pg, sel_id)
            check(f"{sel_id}：锁定后鼠标打不开菜单", not _state(pg, sel_id)["isOpen"])
            focus = _focus_state(pg, sel_id)
            check(f"{sel_id}：锁定后无焦点光晕",
                  focus["focused"] and focus["boxShadow"] == "none" and focus["outline"] == "none", focus)
            for key in ("ArrowDown", "ArrowDown", "Enter", "Enter"):
                pg.keyboard.press(key)
            after = _state(pg, sel_id)
            check(f"{sel_id}：方向键和回车打不开菜单、也改不了值",
                  not after["isOpen"] and after["value"] == st["value"], after)

        print("\n[2] 解锁恢复交互、可读性和键盘反馈")
        for sw_id, sel_id in PAIRS:
            _set_switch(pg, sw_id, True)
            st = _state(pg, sel_id)
            focus = _focus_state(pg, sel_id)
            check(f"{sel_id}：解锁恢复透明度、Tab 顺序和焦点光晕",
                  not st["locked"] and not st["boxOff"] and st["triggerOpacity"] == 1
                  and st["triggerTabIndex"] == 0 and focus["focused"] and focus["boxShadow"] != "none"
                  and "当前生效" in _lock_hint(pg, sel_id)["text"],
                  {"state": st, "focus": focus})
            _click_trigger(pg, sel_id)
            check(f"{sel_id}：解锁后鼠标可打开菜单", _state(pg, sel_id)["isOpen"])
            _click_trigger(pg, sel_id)

        print("\n[3] 总开关优先，单项选择保留")
        _set_switch(pg, MASTER, False)
        for sw_id, sel_id in PAIRS:
            st = _state(pg, sel_id)
            check(f"{sel_id}：总开关关闭，单项保持开启且下拉仍锁定",
                  pg.locator(f"#{sw_id}").is_checked() and st["locked"] and st["nativeDisabled"], st)
            _click_trigger(pg, sel_id)
            check(f"{sel_id}：总开关关闭后鼠标打不开菜单", not _state(pg, sel_id)["isOpen"])
        _set_switch(pg, MASTER, True)
        for _, sel_id in PAIRS:
            _click_trigger(pg, sel_id)
            check(f"{sel_id}：总开关恢复后菜单能打开", _state(pg, sel_id)["isOpen"])
            _click_trigger(pg, sel_id)

        print("\n[4] 直接锁定展开菜单后复位所有选项的 Tab 顺序")
        for _, sel_id in PAIRS:
            _click_trigger(pg, sel_id)
            before = _state(pg, sel_id)
            check(f"{sel_id}：展开时选项均在 Tab 顺序",
                  before["isOpen"] and len(before["optionTabStops"]) >= 3 and all(
                      index == 0 for index in before["optionTabStops"]), before)
            pg.locator(f"#{sel_id}").evaluate("sel => window.__kbTest.lock(sel, true)")
            after = _state(pg, sel_id)
            check(f"{sel_id}：直接锁定后关闭菜单并移除全部选项的 Tab 停靠点",
                  not after["isOpen"] and after["expanded"] == "false"
                  and after["optionTabStops"] == [-1] * len(before["optionTabStops"]), after)
            pg.locator(f"#{sel_id}").evaluate("sel => window.__kbTest.lock(sel, false)")

        print("\n[5] 往返三个页签周期，监听保持一个且仍有效")
        before_bind = _bindings(pg)
        for _ in range(3):
            pg.locator('button.tab[data-page="overview"]').click()
            _load_settings_tab(pg)
        after_bind = _bindings(pg)
        expected = {sw: {"propertyHandler": True, "addCalls": 0} for sw in WATCHED}
        check("往返三次仍只有一个 onchange 槽，重复 addEventListener 注册次数为 0",
              before_bind == after_bind == expected, f"{before_bind} → {after_bind}")
        _set_switch(pg, MASTER, True)
        for sw_id, _ in PAIRS:
            _set_switch(pg, sw_id, True)
        for sw_id, sel_id in PAIRS:
            _set_switch(pg, sw_id, False)
            other = next(s for _, s in PAIRS if s != sel_id)
            states = [_state(pg, s) for s in (sel_id, other)]
            check(f"{sel_id}：切页后关单项立即锁定，另一项仍可用",
                  states[0]["locked"] and not states[1]["locked"], states)
            _set_switch(pg, sw_id, True)
        _set_switch(pg, MASTER, False)
        check("切页后关总开关仍立即锁定两个模型",
              all(_state(pg, sel_id)["locked"] for _, sel_id in PAIRS))

        print("\n[6] 页面无 JS 异常")
        check("pageerror 为空", not errs, errs[:2])
        ctx.close()
        browser.close()

    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
