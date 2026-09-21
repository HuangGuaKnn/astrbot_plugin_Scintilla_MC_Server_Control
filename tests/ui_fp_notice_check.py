"""v0.21.8 无头浏览器实测：指纹不匹配全屏弹窗「检测到就弹 · 指纹下次变动前只弹一次」。

用 Playwright 打开 pages/mc_control/index.html（独立窗口 → 直连模式），
把 page/** 接口全部 mock 掉，只验证**前端契约**：
  1) 后端说 show=True → 弹窗出现；
  2) 弹窗显示即回调 seen（前端不要求主人点按钮就记账）；
  3) 刷新页面 / 切页签 → 不再弹；
  4) 服务端指纹下一次变动（后端重新 show=True）→ 又弹一次；
  5) 概览页「指纹不匹配」常驻标记不随弹窗消失；
  6) v0.21.9：设置页改 server_dir 点保存 → 立刻补检（不必切页签/刷新）；
  7) v0.21.9：空 mods 目录（指纹撞车）→ 弹窗额外警告 mods 目录是空的；
  8) v0.21.10：弹窗里**没有**「本轮不再提醒」勾选框，「知道了」是唯一出口（两条路语义合一）。

跑法：python tests\\ui_fp_notice_check.py
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
# 允许用环境变量指向别处的 index.html —— 便于验证「这个用例真的能抓到 bug」
# （把未修复的页面拷出来跑同一套断言，应当失败）。
PAGE_URL = os.environ.get("FP_TEST_PAGE") or (
    PLUGIN / "pages" / "mc_control" / "index.html"
).as_uri()
API_GLOB = "**/api/v1/plugins/extensions/astrbot_plugin_Scintilla_MC_Server_Control/page/**"

PID = "p_default"
PRESET_NAME = "默认知识库"
SAVE_FP = "7788aabbccdd"     # 设置页保存 server_dir 之后「新服务端」的指纹

# ---- mock 后端：指纹不匹配 + 轮次记账（语义与 knowledge_base.py 一致，够用即可）----
state = {
    "server_fp": "e38d99c65853",
    "server_dir": "",
    "preset_fp": "dead1234beef",
    "popped": [],        # 本轮已弹过的预设
    "suppress": [],
    "calls": [],         # 收到的接口调用
    "seen": 0,
    # v0.21.20：指纹是否「认不出服务端」由指纹引擎显式给出（mods/ 与 plugins/ 都空）
    "weak": False,
    "server_content": "mods/ 185 个 jar → 214 个 mod（forge 1.18.2）",
}


LEGACY_EMPTY_FP = "d41d8cd98f00"   # 老算法的空指纹（新引擎已不再产出，仅作回归样本）

def new_round(server_fp: str, *, weak: bool | None = None,
              content: str | None = None) -> None:
    """服务端变动 → 开新的一轮（作废旧记账）。"""
    state["server_fp"] = server_fp
    state["popped"] = []
    state["suppress"] = []
    if weak is not None:
        state["weak"] = weak
    if content is not None:
        state["server_content"] = content


def notice() -> dict:
    key = f"{PID}|{state['server_fp']}"
    mismatch = state["preset_fp"] != state["server_fp"]
    suppressed = key in state["suppress"]
    popped = key in state["popped"]
    return {
        "show": bool(mismatch and not suppressed and not popped),
        "changed": mismatch, "mismatch": mismatch, "suppressed": suppressed,
        "popped": popped, "key": key, "server_fp": state["server_fp"],
        # v0.21.9：指纹认不出服务端 → 前端要额外警告
        # v0.21.20：判据由后端（指纹引擎）给定，不再靠「指纹等于空串哈希」猜
        "server_fp_weak": bool(state["weak"]),
        "server_content": state["server_content"],
        "server_dir": state.get("server_dir", ""),
        "preset_id": PID, "preset_name": PRESET_NAME, "preset_fp": state["preset_fp"],
    }


def kb_presets() -> dict:
    n = notice()
    return {
        "ok": True, "active": PID, "server_fingerprint": state["server_fp"], "notice": n,
        "server_content": state["server_content"],
        "presets": [{"id": PID, "name": PRESET_NAME, "fingerprint": state["preset_fp"],
                     "bound": True, "active": True, "entries": 3,
                     "match": state["preset_fp"] == state["server_fp"]}],
    }


# v0.22.5（复审 P2）：运行态 mock —— 用例可任意切换「配置的方式 / 实际方式 / 最近一次边界」
RT = {"end_mode": "sentinel", "configured": "sentinel", "degraded": False,
      "probe_misses": 0, "connected": True, "boundary_confirmed": None,
      "idle_unconfirmed": 0, "idle_probe": 0.5}


def overview() -> dict:
    n = notice()
    return {
        "ok": True, "version": "v0.21.8",
        # 运行态挂在 config 下（页面读 r.config.rcon_runtime）
        "config": {"rcon_runtime": dict(RT)},
        "rcon": {"ok": True, "host": "127.0.0.1", "port": 25575},
        "dictionary": {"enabled": True, "mods": 185, "items": 11894},
        "knowledge": {
            "enabled": True, "server_id": state["server_fp"],
            "state": {"enabled": True}, "stats": {"total": 6},
            "preset_name": PRESET_NAME, "fingerprint": state["preset_fp"],
            "match": state["preset_fp"] == state["server_fp"],
            "notice_show": n["show"], "fingerprint_mismatch": n["mismatch"],
        },
        "workflow": {"enabled": True, "recent": []}, "listener": {"running": True},
    }


def handle_post(path: str, body: dict) -> dict:
    if path == "settings/save":
        st = (body or {}).get("settings") or {}
        # 模拟后端 v0.21.7 的「换服就地刷新」：改了 server_dir → 指纹就地重算（= 新轮次）
        if "server_dir" in st:
            new_round(SAVE_FP)
            state["server_dir"] = st.get("server_dir") or ""
        return {
            "ok": True,
            "notice": f"已保存 {len(st)} 项并即时生效（当前服务端指纹 {state['server_fp']}）",
            "applied": {"server_dir": st.get("server_dir"), "notify_target_events": []},
            "ignored": [],
        }
    if path == "rcon/reset":
        # v0.22.5：重建后回到配置的方式（sentinel），边界状态回到「暂无结果」
        RT.update({"end_mode": RT["configured"], "degraded": False,
                   "probe_misses": 0, "boundary_confirmed": None})
        return {"ok": True, "message": "RCON 连接已重建（按当前配置：%s）" % RT["configured"],
                "runtime": dict(RT)}
    if path != "kb/presets/notice":
        return {"ok": False, "error": "mock: 未实现 " + path}
    action = (body or {}).get("action", "ack")
    key = notice()["key"]
    if action == "seen":
        state["seen"] += 1
        if key not in state["popped"]:
            state["popped"].append(key)
        return {"ok": True, **kb_presets(), "notice_text": ""}
    if action == "suppress":
        for bucket in ("popped", "suppress"):
            if key not in state[bucket]:
                state[bucket].append(key)
        text = "已关闭本轮指纹不匹配提醒（服务端指纹下次变动时会重新提示）"
    elif action == "enable":
        state["suppress"] = []
        state["popped"] = [k for k in state["popped"] if k != key]
        text = "已重新开启指纹不匹配提醒"
    else:
        if key not in state["popped"]:
            state["popped"].append(key)
        text = "已了解指纹不匹配提醒"
    return {"ok": True, **kb_presets(), "notice_text": text}


def route(route_obj):
    url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
    state["calls"].append(route_obj.request.method + " " + url)
    if route_obj.request.method == "GET":
        if url == "overview":
            payload = overview()
        elif url == "kb/presets":
            payload = kb_presets()
        else:
            payload = {"ok": False, "error": "mock: 未实现 " + url}
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


def modal_visible(page) -> bool:
    return page.eval_on_selector("#fp_modal", "el => getComputedStyle(el).display !== 'none'")


def main() -> int:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")   # 本机无 playwright 自带 chromium → 用系统 Edge
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page()
        page.route(API_GLOB, route)
        page.goto(PAGE_URL)

        print("[1] 检测到指纹不匹配 → 全屏大弹窗自动弹出")
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("弹窗可见", modal_visible(page))
        check("标题说的是服务端指纹不匹配", "服务端指纹不匹配" in page.inner_text("#fp_modal h2"))
        check("弹窗里写着预设名", PRESET_NAME in page.inner_text("#fp_m_preset"))
        check("弹窗里两个指纹都对", "e38d99c65853" in page.inner_text("#fp_m_server")
              and "dead1234beef" in page.inner_text("#fp_m_preset_fp"))

        print("[2] 弹窗「显示过」即记账（不需要主人点按钮）")
        page.wait_for_timeout(600)
        check("前端已回调 seen", state["seen"] >= 1, f"seen={state['seen']}")
        check("后端记账后 show=False", notice()["show"] is False, str(notice()))
        check("但 mismatch 仍然为 True（概览要保持常驻标记）", notice()["mismatch"] is True)

        print("[3] 刷新页面 / 切页签 → 同一轮不再弹第二次")
        page.reload()
        page.wait_for_timeout(1200)
        check("刷新后不弹", not modal_visible(page))
        page.click('text=知识库')
        page.wait_for_timeout(500)
        check("切到知识库页不弹", not modal_visible(page))
        page.click('text=概览')
        page.wait_for_timeout(500)
        check("切回概览不弹", not modal_visible(page))
        check("概览页仍显示「指纹不匹配」常驻标记",
              "指纹不匹配" in page.inner_text("#ov_modules"), page.inner_text("#ov_modules")[:160])

        print("[4] 服务端指纹下一次变动 → 重新弹一次")
        new_round("cafe0987face")
        page.reload()
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("新指纹 → 又弹了", modal_visible(page))
        check("弹窗里服务端指纹已更新", "cafe0987face" in page.inner_text("#fp_m_server"))
        page.wait_for_timeout(500)
        page.click("#fp_ok")            # 知道了
        page.wait_for_timeout(400)
        check("点「知道了」后关闭", not modal_visible(page))

        print("[5] v0.21.10：撤掉「本轮不再提醒」勾选框 → 「知道了」就是唯一出口")
        new_round("1234abcd5678")
        page.reload()
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("弹窗里已无勾选框（#fp_noask 不存在）", page.query_selector("#fp_noask") is None)
        check("文案里也不再出现「本轮不再提醒」",
              "本轮不再提醒" not in page.inner_text("#fp_modal"), page.inner_text("#fp_modal")[-140:])
        check("改用一行说明替代勾选框（同一轮只提示一次）",
              "只提示这一次" in page.inner_text("#fp_modal"))
        page.click("#fp_ok")                       # 知道了（唯一出口）
        page.wait_for_timeout(400)
        check("点「知道了」照常记账（等价于旧 ack，不再走 suppress）",
              notice()["popped"] is True and notice()["suppressed"] is False, str(notice()))
        page.reload()
        page.wait_for_timeout(1000)
        check("本轮刷新不再弹", not modal_visible(page))
        new_round("9999ffff0000")
        page.reload()
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("指纹变动 → 提醒自动恢复", modal_visible(page))
        check("整轮跑下来前端从未调用 suppress（勾选框真撤了）",
              state["suppress"] == [], str(state["suppress"]))

        print("[6] v0.21.9：设置页改 server_dir 后点保存 → 立刻补检（不必切页签/刷新）")
        page.click("#fp_ok")                       # 关掉上一个弹窗
        page.wait_for_timeout(400)
        page.click('text=设置')
        page.wait_for_timeout(600)
        check("刚在本轮弹过的服务端：切到设置页也不重复弹", not modal_visible(page))
        page.fill("#cfg_server_dir", r"D:\srv\mcs2")
        page.click("#btn_save_cfg_top")
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("保存完立刻弹出（此前要等切页签/刷新才检测）", modal_visible(page))
        check("弹窗里的服务端指纹已是新的", SAVE_FP in page.inner_text("#fp_m_server"),
              page.inner_text("#fp_m_server"))
        check("mock 确实收到了 settings/save",
              any("settings/save" in c for c in state["calls"]), str(state["calls"])[-300:])

        print("[7] v0.21.9/20：内容为空（指纹认不出服务端）→ 弹窗额外警告")
        # v0.21.20：weak 由后端显式给出；文案要同时提 mods/ 与 plugins/，
        # 不能再只怪「mods 目录是空的」——Paper 系插件服的内容根本不在 mods/
        new_round("f0d1c2b3a495", weak=True,
                  content="既没有 mod 也没有插件，版本线索也读不到 → 指纹只是占位值")
        page.reload()
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("照常弹出", modal_visible(page))
        weak_visible = page.eval_on_selector("#fp_weak", "el => getComputedStyle(el).display !== 'none'")
        check("警告线显示出来", weak_visible)
        weak_txt = page.inner_text("#fp_weak")
        check("说明两边都没有内容导致指纹撞车",
              "mods" in weak_txt and "plugins" in weak_txt and "f0d1c2b3a495" in weak_txt,
              weak_txt[:160])
        check("指出模组服在 mods/、插件服在 plugins/（别让插件服主人去找 mods）",
              "Paper" in weak_txt or "插件服" in weak_txt, weak_txt[:200])
        check("弹窗里能看到「内容来源」（这枚指纹是怎么算出来的）",
              "内容来源" in page.inner_text("#fp_modal")
              and "插件" in page.inner_text("#fp_m_content"),
              page.inner_text("#fp_m_content"))
        # 正常指纹 + 正常内容 → 警告线要收回
        new_round("e38d99c65853", weak=False,
                  content="mods/ 185 个 jar → 214 个 mod（forge 1.18.2）")
        page.reload()
        page.wait_for_selector("#fp_modal", state="visible", timeout=8000)
        check("内容正常时警告线隐藏",
              not page.eval_on_selector("#fp_weak", "el => getComputedStyle(el).display !== 'none'"))
        check("内容正常时「内容来源」显示 mods/ 摘要",
              "mods/" in page.inner_text("#fp_m_content"), page.inner_text("#fp_m_content"))

        print("[8] v0.22.5 复审 P2：运行态三状态互不替代（绝不能拿「没自动降级」当「响应可靠」）")
        if modal_visible(page):
            page.click("#fp_ok")
            page.wait_for_timeout(300)
        page.click('text=设置')
        page.wait_for_timeout(600)
        line = lambda: page.inner_text("#rcon_runtime_line")

        # ① 用户**亲手**把 rcon_end_mode 配成 idle：degraded=false，但完整性照样不保证
        RT.update({"end_mode": "idle", "configured": "idle", "degraded": False,
                   "boundary_confirmed": False, "idle_unconfirmed": 3, "probe_misses": 0})
        page.reload()
        page.click('text=设置')
        page.wait_for_timeout(900)
        t = line()
        check("显式配置 idle：不得显示「✓ 边界可靠」（复审 P2 核心）",
              "边界可靠" not in t, t[:200])
        check("夹具生效：运行态确实渲染到了设置页（mock 挂在 config.rcon_runtime）",
              t.strip() != "", repr(t[:120]))
        check("显式配置 idle：如实写出「响应完整性未保证」", "完整性未保证" in t, t[:200])
        check("显式配置 idle：标出这是静默窗口模式", "静默窗口" in t, t[:200])
        check("显式配置 idle：同时给出「最近一次响应可能不完整」",
              "最近一次" in t and "不完整" in t, t[:200])
        check("显式配置 idle：累计未确认次数可见", "3" in t and "未确认" in t, t[:200])

        # ② 自动降级（配置 sentinel、跑成 idle）→ 额外一层警示 + 提供重建出口
        RT.update({"end_mode": "idle", "configured": "sentinel", "degraded": True,
                   "probe_misses": 3, "boundary_confirmed": False, "idle_unconfirmed": 1})
        page.reload()
        page.click('text=设置')
        page.wait_for_timeout(900)
        t = line()
        check("自动降级：显示「已自动降级」", "已自动降级" in t, t[:200])
        check("自动降级：当前运行与配置分开显示（idle vs sentinel）",
              "idle" in t and "sentinel" in t, t[:200])
        check("自动降级：给出「重建 RCON 连接」这条恢复路径", "重建" in t, t[:200])

        # ③ 健康态：sentinel 且最近一次确认过 → 只显示哨兵可靠，且不得出现「响应可能不完整」
        RT.update({"end_mode": "sentinel", "configured": "sentinel", "degraded": False,
                   "probe_misses": 0, "boundary_confirmed": True, "idle_unconfirmed": 0})
        page.reload()
        page.click('text=设置')
        page.wait_for_timeout(900)
        t = line()
        check("健康态：显示结束哨兵可靠", "结束哨兵" in t and "边界未确认" not in t, t[:200])
        check("健康态：不出现「响应可能不完整」这种误报", "可能不完整" not in t, t[:200])

        # ④ 还没跑过任何命令（boundary_confirmed=null）→ 说「暂无结果」，不冒充可靠
        RT.update({"end_mode": "sentinel", "configured": "sentinel", "degraded": False,
                   "probe_misses": 0, "boundary_confirmed": None, "idle_unconfirmed": 0})
        page.reload()
        page.click('text=设置')
        page.wait_for_timeout(900)
        t = line()
        check("暂无结果：明说「尚未执行过命令」，不默认成可靠边界",
              "尚未执行过命令" in t and "可靠" not in t.replace("结束哨兵", ""), t[:200])

        # ⑤ 重建按钮：把运行态拉回配置的方式（自动降级场景的恢复动作）
        RT.update({"end_mode": "idle", "configured": "sentinel", "degraded": True,
                   "probe_misses": 3, "boundary_confirmed": False, "idle_unconfirmed": 1})
        page.reload()
        page.click('text=设置')
        page.wait_for_timeout(900)
        page.click("#btn_rcon_reset")
        page.wait_for_timeout(800)
        check("点「重建连接」后前端切回 sentinel 且不再有降级警示",
              "已自动降级" not in line() and "sentinel" in line(), line()[:200])
        check("重建后边界状态回到「尚未执行过命令」（不沿用重建前的 False）",
              "尚未执行过命令" in line(), line()[:200])
        check("mock 收到了 rcon/reset", any("rcon/reset" in c for c in state["calls"]),
              str(state["calls"])[-200:])

        browser.close()

    if failed:
        print("\n✗ 失败项:")
        for f in failed:
            print("  -", f)
        return 1
    print("\n✓ 全部通过（前端弹窗契约 7 组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
