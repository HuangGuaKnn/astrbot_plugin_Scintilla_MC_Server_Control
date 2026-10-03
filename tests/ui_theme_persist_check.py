"""验证 v0.23.4 主题持久化修复（真实环境复刻）。

用户报的现象：AstrBot 里从 WebUI 进插件页，切成深色、出去再进来又变浅色。

页面自己写明了环境特征（index.html 内注释）：
  「iframe 内（sandbox 无 allow-same-origin，直连必失败）：必须走 bridge」
  「iframe sandbox 无 allow-modals → 原生 confirm/alert 被静默拒绝」
即：AstrBot 的插件页跑在 **sandbox iframe（不给 allow-same-origin）** 里 ——
localStorage 抛 SecurityError、fetch 发不出去，**只有 bridge 能通**。

旧代码把主题存 localStorage 且 catch 静默吞掉 → 每次回退「跟随系统偏好」→ 浅色。
修法：本地存储降级为「首帧加速」，权威值走后端 /ui/theme（经 bridge）。

v0.23.5 追加：iframe 内本地无存档时**不再**回退系统偏好，改为保持深色初值，
并加 theme-pending 遮挡（纯 CSS 500ms 兜底）覆盖裁定窗口，彻底消除进出时的闪烁。

本测试如实复刻：sandbox iframe + mock /api/plugin/page/bridge-sdk.js。
由于不透明源下父页读不到子页 DOM，子页用 postMessage 上报。
"""
from __future__ import annotations

import http.server
import json
import pathlib
import socketserver
import threading

FAILS: list[str] = []


def _patch_pw_driver() -> None:
    import playwright._impl._transport as t
    orig = t.compute_driver_executable

    def patched():
        node, cli = orig()
        return (node.replace("\\\\?\\", ""), cli.replace("\\\\?\\", ""))

    t.compute_driver_executable = patched


_patch_pw_driver()

from playwright.sync_api import sync_playwright  # noqa: E402
from _paths import UI_LAUNCH_KWARGS  # noqa: E402  # 浏览器通道见 _paths（本机 Edge / CI bundled）

PLUGIN = pathlib.Path(r"%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control")


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(PLUGIN), **k)

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"
PAGE = f"{BASE}/pages/mc_control/index.html"

# ---- 假 bridge SDK：注入到 iframe 内（替代 /api/plugin/page/bridge-sdk.js）----
# 它做两件事：① 扮演 AstrBot 的 bridge；② 用 postMessage 把状态上报给父页
# （不透明源下父页读不到子页 DOM，只能这样取证）
FAKE_SDK = r"""
(function(){
  var SENT = [];
  function report(){
    try{
      parent.postMessage({
        __mc: true,
        theme: document.documentElement.dataset.theme,
        txt: (document.getElementById('theme_txt')||{}).textContent || '',
        calls: SENT
      }, '*');
    }catch(e){}
  }
  window.__backendTheme = window.__backendTheme || null;
  window.AstrBotPluginPage = {
    ready: function(){ return Promise.resolve(); },
    apiGet: function(url){
      SENT.push(['GET', String(url)]);
      if(/ui\/theme$/.test(url)) return Promise.resolve({ok:true, theme: window.__backendTheme});
      return Promise.resolve({ok:true});
    },
    apiPost: function(url, body){
      SENT.push(['POST', String(url), body]);
      if(/ui\/theme\/save/.test(url)){ window.__backendTheme = body && body.theme; }
      return Promise.resolve({ok:true, theme: body && body.theme});
    }
  };
  setInterval(report, 150);
})();
"""

HOST = ('<!doctype html><meta charset=utf-8><style>html,body{margin:0}'
        'iframe{width:100%;height:700px;border:0}</style>'
        '<iframe sandbox="allow-scripts" src="{url}"></iframe>')

RECV = "window.__received = window.__received || []; window.addEventListener('message', e => window.__received.push(e.data));"


def latest(pg):
    """父页收到的最后一条子页上报。"""
    msgs = pg.evaluate("() => (window.__received || []).filter(m => m && m.__mc)")
    return msgs[-1] if msgs else {}


def wait_report(pg, cond, timeout=6000):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout / 1000:
        d = latest(pg)
        if d and cond(d):
            return d
        pg.wait_for_timeout(200)
    return latest(pg)


def open_case(ctx, backend_theme):
    host = PLUGIN / "_t_host.html"
    host.write_text(HOST.replace("{url}", PAGE), encoding="utf-8")
    pg = ctx.new_page()
    pg.add_init_script(RECV)
    pg.add_init_script(f"window.__backendTheme = {json.dumps(backend_theme)};")
    pg.route("**/bridge-sdk.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript", body=FAKE_SDK))
    pg.goto(f"{BASE}/_t_host.html")
    return pg, host


with sync_playwright() as pw:
    b = pw.chromium.launch(**UI_LAUNCH_KWARGS, headless=True)
    ctx = b.new_context(color_scheme="light")   # 系统偏好钉死浅色

    # ============ 场景 1：真环境 + 后端有存档 dark ============
    print("【场景 1】sandbox iframe（localStorage/fetch 皆不可用）+ 后端存档 dark")
    pg, host = open_case(ctx, "dark")
    pg.wait_for_timeout(3500)
    d = wait_report(pg, lambda x: x.get("theme") == "dark")
    check(bool(d), "子页有上报（bridge 活着）", f"{list(d.keys()) if d else '无'}")
    check(d.get("theme") == "dark", "★★ 主题从后端恢复为 dark（原来会掉回浅色）", f"拿到 {d.get('theme')}")
    gets = [c for c in d.get("calls", []) if c[0] == "GET"]
    check(any("ui/theme" in c[1] for c in gets), "★ 确实经 bridge 向 /ui/theme 要过存档", f"{[c[1] for c in gets]}")
    check(d.get("txt") == "浅色", "按钮文案同步（写「点一下会变成什么」）", f"拿到 {d.get('txt')!r}")

    # ============ 场景 2：点切换 → 回写后端 ============
    print("\n【场景 2】点切换按钮 → 必须回写后端")
    pg.frames[1].evaluate("() => window.toggleTheme()")
    d2 = wait_report(pg, lambda x: x.get("theme") == "light")
    check(d2.get("theme") == "light", "★ 切换到 light", f"拿到 {d2.get('theme')}")
    posts = [c for c in d2.get("calls", []) if c[0] == "POST"]
    check(any("ui/theme/save" in c[1] for c in posts), "★★ 确实经 bridge POST 了 ui/theme/save", f"{[c[1] for c in posts]}")
    hit = [c for c in posts if "ui/theme/save" in c[1]]
    if hit:
        check(hit[-1][2].get("theme") == "light", "载荷 theme 正确", f"{hit[-1][2]}")

    # ============ 场景 3：模拟「出去再进来」 ============
    print("\n【场景 3】★ 重进页面（模拟用户「出去再回来」）→ 必须是上次选的浅色")
    pg.close()
    pg2, _ = open_case(ctx, "light")   # 场景 2 已把后端改成 light
    pg2.wait_for_timeout(3500)
    d3 = wait_report(pg2, lambda x: x.get("theme") == "light")
    check(d3.get("theme") == "light", "★★★ 重进后仍是上次选的 light（Bug 修复的直接判据）", f"拿到 {d3.get('theme')}")

    # ============ 场景 4：后端和本地都没存档 ============
    # v0.23.5 契约变更：iframe 内不再回退系统偏好，而是保持 <html> 的深色初值。
    # 原因：本页是深色工作台设计，回退系统偏好在深色用户（系统浅色）身上会先闪
    # 一下浅色、再被后端纠正回深色，实测 2 次闪烁；保持初值则由紧随业务脚本的
    # earlyThemeSync 在遮罩期内完成裁定，用户看不到切换。
    print("\n【场景 4】后端与本地都没存档 → 保持深色初值（不闪）且不崩")
    pg3, _ = open_case(ctx, None)
    pg3.wait_for_timeout(3500)
    d4 = wait_report(pg3, lambda x: bool(x))
    check(d4.get("theme") == "dark", "无存档时保持深色初值（v0.23.5 防闪契约）",
          f"拿到 {d4.get('theme')}")

    # ============ 场景 5：独立窗口（localStorage 可用）以本地为准 ============
    print("\n【场景 5】独立窗口（非 iframe）→ 以本地为准，不打扰后端")
    pg4 = ctx.new_page()
    pg4.add_init_script(RECV)
    got = {"n": 0}

    def sdk_route(r):
        got["n"] += 1
        r.fulfill(status=200, content_type="application/javascript", body=FAKE_SDK)

    pg4.route("**/bridge-sdk.js", sdk_route)
    pg4.goto(PAGE)
    pg4.evaluate("() => localStorage.setItem('mcctrl_theme','light')")
    pg4.reload()
    pg4.wait_for_timeout(2500)
    t5 = pg4.evaluate("() => document.documentElement.dataset.theme")
    check(t5 == "light", "独立窗口以本地存档为准", f"拿到 {t5}")

    # ============ 场景 6：独立窗口 + 无存档 → 跟随系统偏好 ============
    # v0.23.5 新增兜底：只有独立窗口（拿不到 bridge）才用系统偏好，
    # iframe 内一律保持初值交给 earlyThemeSync，以此消除进出闪烁。
    print("\n【场景 6】独立窗口 + 无本地存档 → 跟随系统偏好（浅色）")
    ctx6 = b.new_context(color_scheme="light")     # 系统偏好=浅色
    pg5 = ctx6.new_page()
    pg5.add_init_script(RECV)
    pg5.route("**/bridge-sdk.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript", body=FAKE_SDK))
    pg5.goto(PAGE)
    pg5.evaluate("() => { try{ localStorage.removeItem('mcctrl_theme'); }catch(e){} }")
    pg5.reload()
    pg5.wait_for_timeout(2500)
    d6 = wait_report(pg5, lambda x: bool(x))
    check(d6.get("theme") == "light", "独立窗口无存档时跟随系统偏好", f"拿到 {d6.get('theme')}")
    # 揭幕保险：theme-pending 必须已被摘掉（否则页面被遮挡）
    pending = pg5.evaluate(
        "() => document.documentElement.classList.contains('theme-pending')")
    check(pending is False, "独立窗口下遮罩已揭（页面可见）", f"theme-pending={pending}")
    ctx6.close()

    b.close()

host.unlink(missing_ok=True)
srv.shutdown()
print("\n" + ("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} 项失败: {FAILS} ==="))
