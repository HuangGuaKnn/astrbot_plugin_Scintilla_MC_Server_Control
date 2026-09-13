"""v0.21.11 UI 实跑：知识条目「查看详情 / 编辑」全屏子窗口。

真浏览器（Edge + 假后端）验这几件事：
  1) 卡片预览确实看不全（72px 截断 → scrollHeight 远大于 clientHeight），
     同时卡片上有「查看详情」入口；
  2) 点「查看详情」→ 与指纹弹窗同款的全屏子窗口打开，里面是**完整正文**＋元信息；
  3) 弹窗里改正文 / 改主题 → 「保存修改」把 {topic, old_topic, content, status} 交给后端，
     保存后关窗 + 刷新列表；status 原样带上（不在前端瞎改验证状态）；
  4) Ctrl+Enter 保存、Esc 关闭、点遮罩空白处关闭、直接点卡片内容区也能打开；
  5) 后端拒绝（改名撞已有主题）→ 弹窗不关、原地显示错误、不静默覆盖。

跑法：python tests\\ui_kb_detail_check.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 ASTRBOT_APP_DIR 指路）
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

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
PAGE_URL = os.environ.get("KBDET_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()
API_GLOB = "**/api/v1/plugins/extensions/astrbot_plugin_Scintilla_MC_Server_Control/page/**"

FP = "e38d99c65853"
TOPIC = "tac 满改 mp5 方案"
LONG = "【满改方案】\n" + "\n".join(
    f"第{i}行：配件槽位与 NBT 键名（Scope / Under_Barrel / Muzzle / Magazine）需大写驼峰" for i in range(1, 41)
)

entries = [{
    "topic": TOPIC, "content": LONG, "status": "verified", "enabled": True,
    "source": "webui", "created_at": "2026-09-11 10:00:00", "updated_at": "2026-09-11 10:00:00",
    "mod": "tac", "kind": "instance",
}]

state = {"calls": [], "posts": [], "fail_rename": False}


def kb_state() -> dict:
    return {
        "ok": True,
        "state": {"enabled": True, "learning": True, "auto_apply": True},
        "stats": {"total": len(entries), "verified": sum(1 for e in entries if e["status"] == "verified"),
                  "pending": 0, "corrected": 0, "untested": 0},
        "entries": [dict(e) for e in entries],
        "pending": [],
        "server_id": FP,
    }


def overview() -> dict:
    return {
        "ok": True, "version": "v0.21.11",
        "rcon": {"ok": True, "host": "127.0.0.1", "port": 25575},
        "dictionary": {"enabled": True, "mods": 185, "items": 11894},
        "knowledge": {
            "enabled": True, "server_id": FP, "state": {"enabled": True},
            "stats": {"total": len(entries)}, "preset_name": "默认知识库", "fingerprint": FP,
            "match": True, "notice_show": False, "fingerprint_mismatch": False,
        },
        "workflow": {"enabled": True, "recent": []}, "listener": {"running": True},
    }


def kb_presets() -> dict:
    # 指纹一致 → 不弹指纹弹窗，免得遮住知识库页的点击
    return {
        "ok": True, "active": "p_default", "server_fingerprint": FP,
        "notice": {"show": False, "changed": False, "mismatch": False, "key": "p_default|" + FP,
                   "server_fp": FP, "preset_id": "p_default", "preset_name": "默认知识库",
                   "preset_fp": FP, "server_fp_weak": False, "server_dir": ""},
        "presets": [{"id": "p_default", "name": "默认知识库", "fingerprint": FP, "bound": True,
                     "active": True, "entries": len(entries), "match": True}],
    }


def handle_post(path: str, body: dict) -> dict:
    state["posts"].append((path, body))
    if path == "kb/entry":
        topic = (body or {}).get("topic", "")
        old = (body or {}).get("old_topic", topic)
        if old != topic and (state["fail_rename"] or any(e["topic"] == topic for e in entries)):
            return {"ok": False, "error": f"主题「{topic}」已存在，换个名字或先删除它"}
        hit = next((e for e in entries if e["topic"] == old), None)
        if hit is None:
            return {"ok": False, "error": "原条目已不存在（可能刚被改名或删除）"}
        hit["topic"] = topic
        hit["content"] = (body or {}).get("content", "")
        hit["status"] = (body or {}).get("status", hit["status"])
        hit["updated_at"] = "2026-09-12 11:30:00"
        return {"ok": True, "entry": dict(hit), "renamed_from": old if old != topic else ""}
    if path == "kb/presets/notice":
        return {"ok": True, **kb_presets(), "notice_text": ""}
    return {"ok": False, "error": "mock: 未实现 " + path}


def route(route_obj):
    url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
    state["calls"].append(route_obj.request.method + " " + url)
    if route_obj.request.method == "GET":
        payload = {"overview": overview, "state": kb_state, "kb/presets": kb_presets,
                   "events": lambda: {"ok": True, "events": []}}.get(url, lambda: {"ok": False, "error": "mock: 未实现 " + url})()
    else:
        try:
            body = json.loads(route_obj.request.post_data or "{}")
        except Exception:
            body = {}
        payload = handle_post(url, body)
    route_obj.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))


failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def det_visible(page) -> bool:
    return page.eval_on_selector("#kbdet_modal", "el => getComputedStyle(el).display !== 'none'")


def open_kb_tab(page) -> None:
    page.click('button.tab[data-page="knowledge"]')      # .page 用 .active 切换，别用文本选
    page.wait_for_selector("#page_knowledge.active", timeout=5000)
    page.wait_for_selector('#kb_list button[data-act="view"]', state="visible", timeout=8000)


def main() -> int:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)
        page.wait_for_timeout(600)
        open_kb_tab(page)

        print("[1] 卡片：内容确实被截断（基线），但「查看详情」入口在")
        clipped = page.eval_on_selector('.entry .content',
                                        "el => ({c: el.clientHeight, s: el.scrollHeight})")
        check("预览被 72px 截断（看不全 → 才有这个需求）", clipped["s"] > clipped["c"] + 8, str(clipped))
        check("卡片有「查看详情」按钮",
              page.inner_text('#kb_list button[data-act="view"]').strip() == "查看详情")
        check("卡片有「点内容也能看全文」提示",
              "查看完整内容" in page.inner_text("#kb_list .entry .more"))

        print("[2] 点「查看详情」→ 同款全屏子窗口，里面是完整正文")
        page.click('#kb_list button[data-act="view"]')
        page.wait_for_selector("#kbdet_modal", state="visible", timeout=4000)
        check("弹窗打开", det_visible(page))
        check("弹窗标题 = 主题", page.inner_text("#kbdet_title").strip() == TOPIC)
        ta = page.input_value("#kbdet_content")
        check("textarea 是完整正文（含最后一行）", ta == LONG and "第40行" in ta, f"len={len(ta)}/{len(LONG)}")
        sub = page.inner_text("#kbdet_sub")
        check("副标题带类型与状态徽章", "实例" in sub and "已验证" in sub, sub)
        kv = page.inner_text("#kbdet_kv")
        check("元信息：模组/来源/创建/更新/长度", all(k in kv for k in ("tac", "webui", "2026-09-11", "字")), kv)
        box = page.eval_on_selector("#kbdet_modal .modal-box",
                                    "el => ({w: el.clientWidth, h: el.clientHeight, vw: innerWidth, vh: innerHeight})")
        check("子窗口接近全屏（宽 ≥ 640 且不超过视口）",
              640 <= box["w"] <= box["vw"] and box["h"] > box["vh"] * 0.4, str(box))
        check("正文区放大了（远高于卡片的 72px）",
              page.eval_on_selector("#kbdet_content", "el => el.clientHeight") > 200)

        print("[3] 改正文 + 改主题 → 保存把 {topic, old_topic, content, status} 交出去")
        page.fill("#kbdet_content", "改过的正文：弹匣用 30 发扩容，其余按原方案")
        page.fill("#kbdet_topic", "tac 满改 mp5 方案 v2")
        page.click("#kbdet_save")
        page.wait_for_timeout(700)
        path, body = state["posts"][-1]
        check("打到 kb/entry", path == "kb/entry", path)
        check("topic = 新主题", body["topic"] == "tac 满改 mp5 方案 v2", str(body))
        check("old_topic = 原主题（改名靠它）", body["old_topic"] == TOPIC, str(body))
        check("content = 新正文", body["content"].startswith("改过的正文"), str(body)[:120])
        check("status 原样带上（不在前端改验证状态）", body["status"] == "verified", str(body))
        check("保存后关窗", not det_visible(page))
        check("页面上给了成功提示", "已保存" in page.inner_text("#kb_notice"), page.inner_text("#kb_notice"))
        page.wait_for_timeout(400)
        check("列表已刷新成新主题", "tac 满改 mp5 方案 v2" in page.inner_text("#kb_list"))

        print("[4] Ctrl+Enter 保存 / Esc 关闭 / 点空白关闭 / 点卡片内容也能开")
        page.click('#kb_list button[data-act="view"]')
        page.wait_for_selector("#kbdet_modal", state="visible", timeout=4000)
        page.fill("#kbdet_content", "Ctrl+Enter 保存的正文")
        page.keyboard.press("Control+Enter")
        page.wait_for_timeout(700)
        check("Ctrl+Enter 也走保存", state["posts"][-1][1]["content"] == "Ctrl+Enter 保存的正文",
              str(state["posts"][-1][1])[:120])
        check("Ctrl+Enter 后关窗", not det_visible(page))

        page.click(".entry .content")
        page.wait_for_selector("#kbdet_modal", state="visible", timeout=4000)
        check("点卡片内容区也能打开详情", det_visible(page))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        check("Esc 关闭", not det_visible(page))

        page.click('#kb_list button[data-act="view"]')
        page.wait_for_selector("#kbdet_modal", state="visible", timeout=4000)
        page.mouse.click(4, 4)                       # 遮罩左上角空白
        page.wait_for_timeout(300)
        check("点窗口外空白关闭", not det_visible(page))

        print("[5] 后端拒绝改名（撞已有主题）→ 弹窗不关、原地报错、不静默覆盖")
        before = dict(entries[0])
        page.click('#kb_list button[data-act="view"]')
        page.wait_for_selector("#kbdet_modal", state="visible", timeout=4000)
        page.fill("#kbdet_topic", "")                 # 顺便验空值校验
        page.click("#kbdet_save")
        page.wait_for_timeout(300)
        check("空主题 → 原地报错且不关窗",
              det_visible(page) and "不能为空" in page.inner_text("#kbdet_state"),
              page.inner_text("#kbdet_state"))
        page.fill("#kbdet_topic", "占位主题")
        page.fill("#kbdet_content", "想覆盖别人的内容")
        state["fail_rename"] = True
        page.click("#kbdet_save")
        page.wait_for_timeout(600)
        check("后端拒绝 → 弹窗仍开着", det_visible(page))
        check("错误原样显示给主人", "已存在" in page.inner_text("#kbdet_state"), page.inner_text("#kbdet_state"))
        check("本地条目没被改动", entries[0] == before, str(entries[0])[:120])
        check("保存按钮恢复可点（没卡死）",
              page.eval_on_selector("#kbdet_save", "el => !el.disabled"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("[6] 旧卡片没坏：禁用/删除按钮还在，弹窗是新增而非替换")
        acts = page.eval_on_selector_all('#kb_list .entry [data-act]', "els => els.map(e => e.dataset.act)")
        check("卡片动作含 view/toggle/del", {"view", "toggle", "del"} <= set(acts), str(acts))

        browser.close()

    print()
    if failed:
        print(f"✗ {len(failed)} 项失败：" + "；".join(failed))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
