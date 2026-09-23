# -*- coding: utf-8 -*-
"""v0.22.8 回归（真浏览器）：版本设置的完整链路。

补的正是 v0.22.7 自己记下的缺口：
  「WebUI 版本控件缺真实交互验证 —— server_version_override / item_syntax_override
    目前只有静态契约测试与后端单测，尚未在真实浏览器验证『填写 1.20.1 → 保存 →
    item_syntax 切到 legacy_nbt』的完整链路（计划补 Playwright 用例）。」

真 Edge + 假后端（复用 ui_theme_check 的驱动补丁与夹具），验四件事：
  1) 控件存在、下拉三档齐全、输入框的值**来自后端**（不是硬编码）；
  2) 只改草稿、不点保存 → 能力行**不变**（草稿不生效）；
  3) 点「保存全部设置」→ 提交载荷含 server_version_override=1.20.1 →
     保存后自动重刷 → 能力行显示 legacy_nbt（来源：主人手动声明）；
  4) 清空版本再保存 → 能力行回到「无法确定服务端版本」，并告诉主人去哪填。

关键：能力结论由**插件自己的 core/version_caps.py 现算**（不是手写假数据），
所以这条链路测的是真逻辑：前端 → 接口载荷 → 后端能力计算 → 页面显示。

跑法：<python> tests\\ui_version_override_check.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402

add_sys_paths()
import ui_theme_check as F  # noqa: E402  复用：驱动补丁 + 假后端夹具（导入即打补丁）

SHOTS = F.PLUGIN / "tests" / "_shots"

from astrbot_plugin_Scintilla_MC_Server_Control.core import version_caps as VC  # noqa: E402

failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


#: 假后端的「已保存状态」—— 只有 settings/save 能改它（草稿改不动）
STATE = {"ov": "", "syn": "auto", "saves": [], "status_calls": 0}


def status_payload() -> dict:
    """用真实 version_caps 现算能力快照 —— 与后端 web_api 走同一套函数。"""
    info = VC.resolve_version_info(STATE["ov"], "")
    caps = VC.describe_capabilities(info, STATE["syn"])
    ver = (f"{info.raw}（手动声明）" if info.source == "override" and info.raw
           else (info.raw or "未知"))
    return {"ok": True, "version": ver, "daytime": "白天", "mspt": 10.0, "tps": 20.0,
            "version_caps": caps, "list": {"online": 0, "max": 20, "players": []}}


def route(r) -> None:
    url = r.request.url.split("/page/", 1)[-1].split("?")[0]
    method = r.request.method

    def send(obj: dict) -> None:
        r.fulfill(status=200, content_type="application/json",
                  body=json.dumps(obj, ensure_ascii=False))

    if url == "settings" and method == "GET":
        s = F.settings()
        s["settings"]["server_version_override"] = STATE["ov"]
        s["settings"]["item_syntax_override"] = STATE["syn"]
        return send(s)
    if url == "settings/save" and method == "POST":
        payload = json.loads(r.request.post_data or "{}")
        got = payload.get("settings") or {}
        STATE["saves"].append(got)
        if "server_version_override" in got:
            STATE["ov"] = str(got.get("server_version_override") or "").strip()
        if "item_syntax_override" in got:
            STATE["syn"] = str(got.get("item_syntax_override") or "auto").strip()
        return send({"ok": True, "applied": got, "notice": "设置已保存"})
    if url == "server/status":
        STATE["status_calls"] += 1
        return send(status_payload())
    fn = F.GETS.get(url, lambda: {"ok": False, "error": "mock: 未实现 " + url})
    return send(fn())


def save_and_wait(page) -> None:
    page.click("#btn_save_cfg_top")
    page.wait_for_timeout(900)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with F.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="light")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route(F.API_GLOB, route)
        page.goto(F.PAGE_URL)
        page.wait_for_timeout(1200)

        print("[1] 打开设置页：控件、下拉、初值")
        page.click('button.tab[data-page="settings"]')
        page.wait_for_timeout(700)
        check("设置页有版本输入框 #cfg_server_version",
              page.locator("#cfg_server_version").count() == 1)
        check("设置页有语法世代下拉 #cfg_item_syntax",
              page.locator("#cfg_item_syntax").count() == 1)
        opts = page.eval_on_selector_all("#cfg_item_syntax option", "els => els.map(e => e.value)")
        check("下拉三档齐全（auto / legacy_nbt / components）",
              opts == ["auto", "legacy_nbt", "components"], str(opts))
        check("★输入框初值来自后端（不是前端硬编码）",
              page.input_value("#cfg_server_version") == "",
              repr(page.input_value("#cfg_server_version")))
        caps0 = page.inner_text("#ver_caps_line")
        check("★版本未知时能力行明说「无法确定」", "无法确定" in caps0, caps0[:140])
        check("★未知时给出逃生出口（告诉主人填 1.20.1）",
              "1.20.1" in caps0, caps0[:160])
        # v0.22.11：能力行**不在折叠区里** —— 「版本未知 → 带数据的请求会被拒绝」
        # 属于安全警示，收起折叠区也必须看得见（这是折叠改造的硬边界）。
        check("★能力警示行不在折叠区内（收起也看得见）",
              page.evaluate("() => !document.getElementById('ver_caps_line')"
                            ".closest('details.adv-fold')") is True)
        page.screenshot(path=str(SHOTS / "ver_override_1_unknown.png"))

        # v0.22.11：版本 / 语法收进了「进阶设置」折叠区（默认收起）——
        # 后续要改值得先展开；顺带把「默认收起」这条契约钉住。
        check("★版本 / 语法默认收在折叠区里（普通用户不必调）",
              page.evaluate("() => { const d = document.getElementById('adv_rcon');"
                            " return !!d && d.open === false; }") is True)
        page.click("#adv_rcon > summary")
        page.wait_for_timeout(300)
        check("展开后版本输入框可见（可改）", page.is_visible("#cfg_server_version"))
        page.screenshot(path=str(SHOTS / "ver_override_1b_expanded.png"))

        print("[2] 只改草稿、不保存 → 后端状态与能力行都不该动")
        calls_before = STATE["status_calls"]
        page.fill("#cfg_server_version", "1.20.1")
        page.wait_for_timeout(500)
        caps_draft = page.inner_text("#ver_caps_line")
        check("★草稿不影响能力行（未保存就不生效）",
              "无法确定" in caps_draft, caps_draft[:140])
        check("草稿阶段没有偷偷提交", len(STATE["saves"]) == 0, str(len(STATE["saves"])))

        print("[3] 点「保存全部设置」→ 载荷 → 保存后自动重刷")
        page.select_option("#cfg_item_syntax", "auto")
        save_and_wait(page)
        check("★保存请求真的发出去了", len(STATE["saves"]) == 1, str(len(STATE["saves"])))
        got = STATE["saves"][-1] if STATE["saves"] else {}
        check("★载荷带上了 server_version_override=1.20.1",
              got.get("server_version_override") == "1.20.1",
              repr(got.get("server_version_override")))
        check("★载荷带上了 item_syntax_override",
              "item_syntax_override" in got, repr(got.get("item_syntax_override")))
        check("★保存后自动重刷能力状态（不需要重启插件）",
              STATE["status_calls"] > calls_before,
              f"{calls_before} → {STATE['status_calls']}")
        caps_after = page.inner_text("#ver_caps_line")
        check("★★填写 1.20.1 并保存 → 物品语法切到 legacy_nbt",
              "legacy_nbt" in caps_after, caps_after[:160])
        check("★来源如实标注「主人手动声明」（不许含糊）",
              "主人手动声明" in caps_after, caps_after[:160])
        check("★显示版本号 1.20.1", "1.20.1" in caps_after, caps_after[:160])
        check("保存提示出现在吸顶条", "已保存" in page.inner_text("#cfg_notice_top"),
              page.inner_text("#cfg_notice_top")[:80])
        page.screenshot(path=str(SHOTS / "ver_override_2_saved.png"))

        print("[4] 清空版本再保存 → 回到「无法确定」")
        page.fill("#cfg_server_version", "")
        save_and_wait(page)
        check("清空后确实提交了空值",
              STATE["saves"] and STATE["saves"][-1].get("server_version_override") == "",
              repr(STATE["saves"][-1].get("server_version_override") if STATE["saves"] else None))
        caps_clear = page.inner_text("#ver_caps_line")
        check("★清空后能力行回到「无法确定服务端版本」",
              "无法确定" in caps_clear, caps_clear[:140])
        check("★不再残留上一次的 legacy_nbt 结论",
              "legacy_nbt" not in caps_clear, caps_clear[:140])
        page.screenshot(path=str(SHOTS / "ver_override_3_cleared.png"))

        print("[5] 填 1.12.2 → 保存 → 能力行明确说不支持自动生成（v0.23.2 裁决 Q1/Q4）")
        page.fill("#cfg_server_version", "1.12.2")
        save_and_wait(page)
        check("1.12.2 确实提交了", STATE["ov"] == "1.12.2", STATE["ov"])
        caps12 = page.inner_text("#ver_caps_line")
        check("★★1.12.2 → 能力行明说「暂不支持该版本的自动命令生成」",
              "暂不支持" in caps12, caps12[:200])
        check("★1.12.2 → 点名 1.13 分水岭（不是含糊说「可能不兼容」）",
              "1.13" in caps12, caps12[:200])
        check("★1.12.2 → 不谎报支持（不得出现 legacy_nbt 能力结论）",
              "legacy_nbt" not in caps12, caps12[:200])
        check("★1.12.2 → 给出可执行出路（手动执行 / 升级服务端）",
              "手动" in caps12 or "升级" in caps12, caps12[:200])
        check("★1.12.2 → 与「版本未知」区分（不再说「无法确定」）",
              "无法确定" not in caps12, caps12[:200])
        page.screenshot(path=str(SHOTS / "ver_override_4_preflatten.png"))

        print("[6] 1.12.2 下手填 legacy_nbt → 仍不得放行")
        page.select_option("#cfg_item_syntax", "legacy_nbt")
        save_and_wait(page)
        caps12b = page.inner_text("#ver_caps_line")
        check("★★已知 1.12.2 时手填 legacy_nbt 不放行"
              "（legacy_nbt 是 1.13~1.20.4 的写法，拿它覆盖只会生成错命令）",
              "legacy_nbt" not in caps12b and "暂不支持" in caps12b, caps12b[:200])
        page.screenshot(path=str(SHOTS / "ver_override_5_preflatten_override.png"))

        check("全程无 JS 异常（pageerror）", not errors, "；".join(errors[:3]))
        print(f"  截图目录：{SHOTS}")
        ctx.close()
        browser.close()

    print()
    if failed:
        print(f"✗ 失败 {len(failed)} 项：" + "；".join(failed))
        return 1
    print("✓ 全部通过：填写版本 → 保存 → item_syntax 切换（含 1.12.2 预扁平化拒绝）的完整链路已验通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
