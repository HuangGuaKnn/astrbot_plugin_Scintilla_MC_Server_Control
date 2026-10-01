"""复现并定位「深色进入时闪几下」的时序。

预期时序（当前实现）：
  第1帧：HTML 硬编码 <html data-theme="dark">            → dark
  头脚本：localStorage 挂、cookie 挂 → 回退系统偏好 light → light   ← 第 1 次闪
  boot 后：bridge 拿到后端 dark                          → dark    ← 第 2 次闪

本脚本在 sandbox iframe（用户真实环境）里高频采样 data-theme，
记录每次变化的时刻，用于定位闪烁来源。
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

PLUGIN = pathlib.Path(r"%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control")


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

FAKE_SDK = r"""
(function(){
  window.__backendTheme = window.__backendTheme || null;
  window.AstrBotPluginPage = {
    ready: function(){ return new Promise(function(res){ setTimeout(res, window.__readyDelay || 0); }); },
    apiGet: function(url){
      if(/ui\/theme$/.test(url)) return Promise.resolve({ok:true, theme: window.__backendTheme});
      return Promise.resolve({ok:true});
    },
    apiPost: function(url, body){
      if(/ui\/theme\/save/.test(url)){ window.__backendTheme = body && body.theme; }
      return Promise.resolve({ok:true});
    }
  };
})();
"""

# 子页内高频采样（起于文档最初，先于一切业务脚本）
SAMPLER = r"""
window.__trace = [];
(function tick(){
  try{
    window.__trace.push([Math.round(performance.now()), document.documentElement.dataset.theme || '(无)', document.documentElement.classList.contains("theme-pending") ? "遮" : "见"]);
  }catch(e){}
  if(window.__trace.length < 700) setTimeout(tick, 20);
})();
setInterval(function(){
  try{ parent.postMessage({__trace: window.__trace}, '*'); }catch(e){}
}, 300);
"""

HOST = ('<!doctype html><meta charset=utf-8><style>html,body{margin:0}'
        'iframe{width:100%;height:700px;border:0}</style>'
        '<iframe sandbox="allow-scripts" src="{url}"></iframe>')

RECV = ("window.__recv = []; window.addEventListener('message',"
        " e => { if(e.data && e.data.__trace) window.__recv.push(e.data.__trace); });")


def summarize(trace):
    """把采样压成「值变化序列」（带可见性）。"""
    seq = []
    for item in trace:
        tt, v = item[0], item[1]
        vis = item[2] if len(item) > 2 else "见"
        key = (v, vis)
        if not seq or (seq[-1][1], seq[-1][2]) != key:
            seq.append((tt, v, vis))
    return seq


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(color_scheme="light")   # 用户系统是浅色

    for backend in ("dark", "light"):
        print(f"===== 后端存档 = {backend}（系统偏好=浅色）=====")
        hostf = PLUGIN / "_t_host.html"
        hostf.write_text(HOST.replace("{url}", PAGE), encoding="utf-8")
        pg = ctx.new_page()
        pg.add_init_script(RECV)
        pg.add_init_script(f"window.__backendTheme = {json.dumps(backend)}; window.__readyDelay = 600;")
        pg.route("**/bridge-sdk.js", lambda r: r.fulfill(
            status=200, content_type="application/javascript", body=FAKE_SDK))
        pg.route(PAGE, lambda r: r.fulfill(
            status=200, content_type="text/html",
            body=(PLUGIN / "pages/mc_control/index.html").read_text(encoding="utf-8")
                 .replace("<head>", "<head>\n<script>" + SAMPLER + "</script>")))
        pg.goto(f"{BASE}/_t_host.html")
        pg.wait_for_timeout(6000)

        recv = pg.evaluate("() => window.__recv || []")
        # 取最长的那条（最后上报的含全量）
        trace = max(recv, key=len) if recv else []
        seq = summarize(trace)
        print(f"  采样点数: {len(trace)}")
        print("  主题变化序列:")
        for i, (t, v, vis) in enumerate(seq):
            mark = "" if i == len(seq) - 1 else "   ← 变化"
            print(f"    {t:>6}ms  {v:<5} [{vis}] {mark}")
        # ★ 只有「用户看得见」的时候发生主题变化，才算真闪
        visible_seq = []
        for tt, v, vis in seq:
            if vis == "见" and (not visible_seq or visible_seq[-1] != v):
                visible_seq.append(v)
        flash = max(0, len(visible_seq) - 1)
        print(f"  → 可见期间的主题变化次数（=用户能看到的闪）: {flash}")
        print(f"  → 最终主题: {seq[-1][1] if seq else '无'}")
        print()
        hostf.unlink(missing_ok=True)

    b.close()

srv.shutdown()
