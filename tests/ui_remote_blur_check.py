"""v0.21.16 UI 实跑：异地 RCON 模式下不可用区块的「亚克力磨砂 + 文字告知」。

真浏览器（Edge + 假后端）验这几件事：
  1) 开关勾上（= 后端已保存）→ 依赖服务端本地文件的区块全部加 .rmt-off（目录区 + 事件播报 / 聊天桥接 /
     物品词典·知识库三张卡），内容真的被模糊（computed filter 含 blur）且停用交互
     （pointer-events:none）；依赖词典的三行 LLM 工具用行级 .rmt-off-row + .rmt-tag；
  2) 亚克力提示牌**不参与模糊**（filter 必须是 none）、文案写明「哪块不可用 + 为什么」、
     且不越出所属卡片边界；
  3) 重复 loadSettings 不叠牌子（数量稳定）；
  4) 服务器页顺带提示「异地模式下版本探测不可用」（RMT.on 复用）；
  5) 整页封禁（v0.21.18）：知识库一页全废 → 整页磨砂压暗 + 一枚固定居中的亚克力公告，
     页签计数改「—」，公告里的「去设置」真能跳；
  6) **开关草稿不许改写封禁（v0.21.19）**：关掉开关但没保存 → 封禁照旧（旧版本这里会解封，
     主人点进知识库才发现整页不可用）；保存（后端真变）后才解封并补拉数据；反向亦然 ——
     本地模式下勾上开关没保存，不许提前封禁；
  7) 深 / 浅两套主题截图（人眼复核亚克力在两种底色下都好看）。

跑法：python tests\\ui_remote_blur_check.py
（用 AstrBot 自带解释器即可，例如 <AstrBot>/backend/python/python.exe；或用 ASTRBOT_APP_DIR 指路）
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ui_theme_check import (  # noqa: E402  （复用同一套假后端 + 驱动补丁）
    API_GLOB, PAGE_URL, SHOTS, overview, route, settings,
)

from playwright.sync_api import sync_playwright  # noqa: E402

BLOCKS = ["sd_box", "cfg_ev_enabled", "cfg_bridge_en", "cfg_dict_en"]
ROWS = ["cfg_tool_search", "cfg_tool_recipes", "cfg_tool_mods"]

STATE_JS = r"""
(ids) => {
  const pick = el => el ? {
    off: el.classList.contains('rmt-off'),
    offRow: el.classList.contains('rmt-off-row'),
    offSoft: el.classList.contains('rmt-off-soft'),
    badge: (el.querySelector(':scope > .rmt-badge') || {}).innerText || '',
    tag: (el.querySelector(':scope > .rmt-tag') || {}).innerText || '',
  } : null;
  // 逐级累乘有效不透明度 / 收集模糊（外层容器的 opacity 会一起作用到文字上）
  const chain = el => {
    let o = 1, f = [], n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      o *= parseFloat(cs.opacity);
      if (cs.filter && cs.filter !== 'none') f.push(cs.filter);
      n = n.parentElement;
    }
    return {op: Math.round(o * 1000) / 1000, filt: f.join(' ')};
  };
  const rgb = s => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  const lum = c => { const v = c / 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
  const contrast = (f, b) => {
    const L = a => .2126 * lum(a[0]) + .7152 * lum(a[1]) + .0722 * lum(a[2]);
    const [hi, lo] = [L(f), L(b)].sort((x, y) => y - x);
    return Math.round(((hi + .05) / (lo + .05)) * 100) / 100;
  };
  const out = {blocks: {}, rows: {}, counts: {}, sample: {}};
  for (const id of ids.blocks) {
    const el = document.getElementById(id);
    const host = (id === 'sd_box') ? el : (el && el.closest('.card'));
    out.blocks[id] = pick(host);
    if (host) {
      const b = host.querySelector(':scope > .rmt-badge');
      const hr = host.getBoundingClientRect();
      if (b && hr.height > 0) {
        const br = b.getBoundingClientRect();
        out.blocks[id].h = Math.round(hr.height);
        out.blocks[id].topPct = Math.round(((br.top + br.bottom) / 2 - hr.top) / hr.height * 100) / 100;
      }
    }
    if (id === 'cfg_ev_enabled' && host) {
      const kid = host.querySelector('.sw-row') || host.querySelector('*');
      const cs = getComputedStyle(kid);
      const b = host.querySelector(':scope > .rmt-badge');
      const br = b && b.getBoundingClientRect(), hr = host.getBoundingClientRect();
      out.sample = {
        childFilter: cs.filter, childPE: cs.pointerEvents, childOpacity: parseFloat(cs.opacity),
        badgeFilter: b ? getComputedStyle(b).filter : null,
        badgeIn: !!(br && hr) && br.left >= hr.left - 2 && br.right <= hr.right + 2
                   && br.top >= hr.top - 2 && br.bottom <= hr.bottom + 2,
      };
    }
    if (id === 'sd_box' && host) {                    // v0.21.17：文字型区块要「看得清」
      const inp = document.getElementById('cfg_server_dir');
      const hint = document.getElementById('sd_hint');
      const tag = host.querySelector(':scope > .rmt-tag');
      const ic = getComputedStyle(inp), tr = tag && tag.getBoundingClientRect(),
            ir = inp.getBoundingClientRect(), hr = host.getBoundingClientRect();
      out.sd = {
        badgeCount: host.querySelectorAll(':scope > .rmt-badge').length,
        tagText: tag ? tag.innerText : '',
        inputChain: chain(inp), hintChain: chain(hint),
        inputFilter: ic.filter, inputDisabled: inp.disabled,
        /* 文字可读性：输入框文字色 vs 输入框底色（亚克力磨砂层若带底色，会二次压低对比度） */
        fg: ic.webkitTextFillColor || ic.color, bg: ic.backgroundColor,
        contrast: contrast(rgb(ic.webkitTextFillColor || ic.color), rgb(ic.backgroundColor)),
        veilBg: getComputedStyle(host, '::after').backgroundColor,
        veilBorder: getComputedStyle(host, '::after').borderTopStyle,
        veilBackdrop: getComputedStyle(host, '::after').backdropFilter,
        tagOverInput: !!(tr && ir) && tr.bottom > ir.top + 1 && tr.top < ir.bottom && tr.right > ir.left && tr.left < ir.right,
        tagIn: !!(tr && hr) && tr.left >= hr.left - 6 && tr.right <= hr.right + 6
                 && tr.top >= hr.top - 30 && tr.bottom <= hr.bottom + 6,
        hintText: hint.innerText, hintColor: getComputedStyle(hint).color,
      };
    }
  }
  for (const id of ids.rows) {
    const el = document.getElementById(id);
    const row = el && el.closest('.sw-row');
    out.rows[id] = pick(row);
    if (id === 'cfg_tool_search' && row) {
      const t = row.querySelector('.sw-t');
      out.sample.rowFilter = getComputedStyle(t).filter;
      out.sample.rowTagZ = row.querySelector(':scope > .rmt-tag')
        ? getComputedStyle(row.querySelector(':scope > .rmt-tag')).zIndex : null;
    }
  }
  out.counts = {
    off: document.querySelectorAll('.rmt-off').length,
    offRow: document.querySelectorAll('.rmt-off-row').length,
    offSoft: document.querySelectorAll('.rmt-off-soft').length,
    seal: document.querySelectorAll('.rmt-seal').length,
    sealCard: document.querySelectorAll('.rmt-seal-card').length,
    badge: document.querySelectorAll('.rmt-badge').length,
    tag: document.querySelectorAll('.rmt-tag').length,
    anyCardOff: document.querySelectorAll('.card.rmt-off').length,
  };
  // v0.21.18：整页封禁（知识库）—— 页面本身模糊压暗 + 一枚固定居中的亚克力公告
  const kp = document.getElementById('page_knowledge');
  if (kp) {
    const wrap = kp.querySelector(':scope > .rmt-seal-card');
    const panel = kp.querySelector('.rmt-seal-panel');
    const kid = Array.from(kp.children).find(n => !n.classList.contains('rmt-seal-card')) || null;
    const kcs = kid ? getComputedStyle(kid) : null;
    const pr = panel ? panel.getBoundingClientRect() : null;
    const vw = window.innerWidth, vh = window.innerHeight;
    // 面板是可读性的唯一入口：把半透明亚克力底与「身后真实底色」合成后再算对比度。
    // 身后可能是页面底色，也可能是被模糊的卡片底色 —— 两种都算，取最差的那个。
    const mAlpha = s => { const m = (s.match(/[\d.]+/g) || []); return m.length > 3 ? parseFloat(m[3]) : 1; };
    const opaqueBase = from => {
      let n = from;
      while (n && n.nodeType === 1) {
        const b = getComputedStyle(n).backgroundColor;
        if (b && b !== 'transparent' && mAlpha(b) >= 1) return rgb(b);
        n = n.parentElement;
      }
      return [0, 0, 0];
    };
    let sz = null;
    if (panel) {
      const px = getComputedStyle(panel);
      const pbg = rgb(px.backgroundColor), pa = mAlpha(px.backgroundColor);
      const base = opaqueBase(kp), cardBase = opaqueBase(kp.querySelector('.card') || kp);
      const over = (f, b) => [0, 1, 2].map(i => f[i] * pa + b[i] * (1 - pa));
      const T = panel.querySelector('b'), W = panel.querySelector('i');
      const tc = rgb(getComputedStyle(T).color), wc = rgb(getComputedStyle(W).color);
      sz = {
        base: base, cardBase: cardBase, panelBg: pbg, alpha: pa,
        title: Math.min(contrast(tc, over(pbg, base)), contrast(tc, over(pbg, cardBase))),
        why: Math.min(contrast(wc, over(pbg, base)), contrast(wc, over(pbg, cardBase))),
      };
      sz.titleFg = getComputedStyle(T).color; sz.whyFg = getComputedStyle(W).color;
    }
    out.seal = {
      sealed: kp.classList.contains('rmt-seal'),
      cardCount: kp.querySelectorAll(':scope > .rmt-seal-card').length,
      badge: (kp.querySelector(':scope > .rmt-badge') || {}).innerText || '',
      title: panel ? (panel.querySelector('b') || {}).textContent || '' : '',
      why: panel ? (panel.querySelector('i') || {}).textContent || '' : '',
      btn: (panel && panel.querySelector('[data-rmt-goto]')) ? panel.querySelector('[data-rmt-goto]').innerText : '',
      childFilter: kcs ? kcs.filter : null,
      childChain: kid ? chain(kid) : null,
      childPE: kcs ? kcs.pointerEvents : null,
      panelChain: panel ? chain(panel) : null,
      panelPE: panel ? getComputedStyle(panel).pointerEvents : null,
      wrapPE: wrap ? getComputedStyle(wrap).pointerEvents : null,
      panelIn: !!pr && pr.top >= -2 && pr.bottom <= vh + 2 && pr.left >= -2 && pr.right <= vw + 2,
      panelCX: pr ? Math.round((pr.left + pr.right) / 2 - vw / 2) : null,
      panelCY: pr ? Math.round((pr.top + pr.bottom) / 2 - vh / 2) : null,
      panelW: pr ? Math.round(pr.width) : null,
      contrasts: sz,
      tabCount: (document.getElementById('tab_kb_cnt') || {}).textContent || '',
      kbList: (document.getElementById('kb_list') || {}).innerText || '',
    };
  }
  out.hintShown = (document.getElementById('remote_mode_hint') || {}).style?.display || '';
  out.hintTag = (document.getElementById('rmt_hint_tag') || {}).textContent || '';
  out.sdDisabled = !!(document.getElementById('cfg_server_dir') || {}).disabled;
  // v0.21.19：「开关改了但没保存」的提示行
  const pe = document.getElementById('remote_mode_pending');
  out.pendShown = pe ? (pe.style.display || '') : '';
  out.pendVisible = !!(pe && pe.offsetParent !== null);
  out.pendText = pe ? pe.innerText : '';
  return out;
}
"""

def toggle_sw(page, sel: str, on: bool) -> None:
    """勾/取消某个开关。开关的 <input> 是视觉隐藏的（opacity:0;width:0），
    只能点它旁边的 .sl 滑块（点 label 内的滑块同样会切换 input）。"""
    if page.is_checked(sel) == on:
        return
    page.click(f"{sel} ~ .sl")
    page.wait_for_timeout(300)


failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" —— {extra}" if extra and not cond else ""))
    if not cond:
        failed.append(name)


def make_route(state: dict):
    """假后端：把 remote_rcon_mode 写进 overview.config 与 settings（前端就读这两处）。

    `state = {"remote": bool}` 是**可变的**「后端已保存状态」：
      · GET  → 按 state 回；
      · POST settings/save → 读请求体里的 remote_rcon_mode 就地翻状态（模拟真正保存）。
    这样才测得出 v0.21.19 的关键区别：开关草稿不算数，保存才算数。
    """
    def handler(route_obj):          # 必须保持单参数签名（两参数会被 Playwright 塞 request 进来）
        url = route_obj.request.url.split("/page/", 1)[-1].split("?")[0]
        remote = bool(state["remote"])
        if url == "overview":
            payload = overview()
            payload["config"]["remote_rcon_mode"] = remote
            # 异地模式：后端本来就不会给出词典 / 监听数据
            payload["dictionary"] = {"enabled": True, "mods": None, "items": None}
            payload["listener"] = {"running": False, "enabled": True}
            route_obj.fulfill(status=200, content_type="application/json",
                              body=json.dumps(payload, ensure_ascii=False))
        elif url == "settings":
            payload = settings()
            payload["settings"]["remote_rcon_mode"] = remote
            route_obj.fulfill(status=200, content_type="application/json",
                              body=json.dumps(payload, ensure_ascii=False))
        elif url == "settings/save":
            try:
                body = json.loads(route_obj.request.post_data or "{}")
                sent = (body.get("settings") or {}).get("remote_rcon_mode")
                if sent is not None:
                    state["remote"] = bool(sent)          # 保存 = 后端状态真的变了
            except Exception:
                pass
            route_obj.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "notice": "设置已保存（mock）",
                                               "applied": {"remote_rcon_mode": state["remote"],
                                                           "notify_target_events": []}},
                                              ensure_ascii=False))
        else:
            return route(route_obj)
    return handler


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    ids = {"blocks": BLOCKS, "rows": ROWS}
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            browser = pw.chromium.launch()

        ctx = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="dark")
        page = ctx.new_page()
        state = {"remote": True}                       # 后端「已保存」状态（可变）
        page.route(API_GLOB, make_route(state))
        page.goto(PAGE_URL)
        page.wait_for_timeout(900)
        page.click('button.tab[data-page="settings"]')
        page.wait_for_timeout(600)

        print("[1] 开关勾上（异地 RCON 模式）→ 不可用区块被标记")
        check("开关读到的确实是「已开启」", page.is_checked("#cfg_remote_rcon_mode"))
        st = page.evaluate(STATE_JS, ids)
        b0 = st["blocks"]["sd_box"] or {}
        check("sd_box 用的是软标记 .rmt-off-soft（不是硬磨砂）", bool(b0.get("offSoft")), str(b0))
        check("sd_box 有亚克力小标签且文案非空", bool(b0.get("tag")), str(b0))
        for i in BLOCKS[1:]:
            b = st["blocks"][i] or {}
            check(f"{i} 加上了 .rmt-off", bool(b.get("off")), str(b))
            check(f"{i} 有亚克力提示牌且文案非空", bool(b.get("badge")), str(b.get("badge")))
        print("      提示牌文案：")
        for i in BLOCKS[1:]:
            print(f"        · {i} → {st['blocks'][i]['badge'].replace(chr(10), ' / ')}")
        print(f"        · sd_box → [{st['sd']['tagText']}] {st['sd']['hintText'][:60]}")
        for i in ROWS:
            r = st["rows"][i] or {}
            check(f"{i} 那一行加上了 .rmt-off-row + 小标签", bool(r.get("offRow") and r.get("tag")), str(r))
        check("硬磨砂 3 张卡片；目录区走软标记 1 处",
              st["counts"]["off"] == 3 and st["counts"]["offSoft"] == 1, str(st["counts"]))
        check("行级 3 行", st["counts"]["offRow"] == 3, str(st["counts"]))
        check("提示牌 3（卡片级）、小标签 4（3 行工具 + 1 目录区）",
              st["counts"]["badge"] == 3 and st["counts"]["tag"] == 4, str(st["counts"]))
        check("禁用说明（remote_mode_hint）已显示", st["hintShown"] == "block", st["hintShown"])
        check("说明行开头是「已开启：」（没有草稿时不加前缀）", st["hintTag"] == "已开启：", st["hintTag"])
        check("服务器目录输入框被 disabled", st["sdDisabled"])

        print("[1b] v0.21.17 回归：目录区文字必须看得清（旧写法叠了三层淡化）")
        sd = st["sd"]
        check("提示牌不再压住输入框", not sd.get("badgeCount"), f"sd_box 里还有 {sd.get('badgeCount')} 块大提示牌")
        check("小标签没盖到输入框上", not sd.get("tagOverInput"), str(sd.get("tagText")))
        check("小标签不越出目录区边界", bool(sd.get("tagIn")), str(sd.get("tagText")))
        check("输入框不再被模糊", sd.get("inputFilter") == "none", str(sd.get("inputFilter")))
        check("输入框有效不透明度 ≥ 0.9（旧版只剩 0.22）",
              (sd.get("inputChain") or {}).get("op", 0) >= 0.9, str(sd.get("inputChain")))
        check("提示行有效不透明度 ≥ 0.9", (sd.get("hintChain") or {}).get("op", 0) >= 0.9, str(sd.get("hintChain")))
        check("没有磨砂底色盖在文字上", sd.get("veilBg") in ("rgba(0, 0, 0, 0)", "transparent"), str(sd.get("veilBg")))
        check("区域用虚线框圈出（标记仍在）", sd.get("veilBorder") == "dashed", str(sd.get("veilBorder")))
        check("输入框文字对比度 ≥ 4.5（WCAG AA）", (sd.get("contrast") or 0) >= 4.5,
              f"{sd.get('fg')} on {sd.get('bg')} = {sd.get('contrast')}")
        check("提示行换成了「为什么不可用」", "异地模式" in (sd.get("hintText") or ""), sd.get("hintText", "")[:60])
        print(f"      目录区实测：输入框文字 {sd.get('fg')} / 底色 {sd.get('bg')} → 对比度 {sd.get('contrast')}"
              f"；有效不透明度 {sd.get('inputChain', {}).get('op')}；模糊 {sd.get('inputFilter')}")

        print("[1c] v0.21.18：知识库整页封禁（异地模式下整页都长在本地文件上）")
        page.click('button.tab[data-page="knowledge"]')
        page.wait_for_timeout(600)
        stk = page.evaluate(STATE_JS, ids)
        sl = stk["seal"] or {}
        check("知识库页加了 .rmt-seal 且只有一张公告层", sl.get("sealed") and sl.get("cardCount") == 1, str(sl.get("cardCount")))
        check("页内内容被模糊", "blur" in (sl.get("childFilter") or ""), str(sl.get("childFilter")))
        check("页内内容被压暗（有效不透明度 ≤ 0.4）", (sl.get("childChain") or {}).get("op", 1) <= 0.4, str(sl.get("childChain")))
        check("页内内容停用交互（pointer-events:none）", sl.get("childPE") == "none", str(sl.get("childPE")))
        check("公告面板自己不参与模糊", (sl.get("panelChain") or {}).get("filt") == "", str(sl.get("panelChain")))
        check("公告面板有效不透明度 = 1", (sl.get("panelChain") or {}).get("op") == 1, str(sl.get("panelChain")))
        check("公告面板可点（pointer-events:auto，容器 none）",
              sl.get("panelPE") == "auto" and sl.get("wrapPE") == "none", f"{sl.get('panelPE')} / {sl.get('wrapPE')}")
        _cx, _cy = sl.get("panelCX"), sl.get("panelCY")
        check("公告固定在视口正中（不受滚动影响）",
              _cx is not None and _cy is not None and abs(_cx) <= 2 and abs(_cy) <= 2,
              f"偏移 x={_cx} y={_cy}")
        check("公告完整落在视口内", bool(sl.get("panelIn")), f"宽 {sl.get('panelW')}px")
        check("公告标题写清「不可用」", "不可用" in (sl.get("title") or ""), sl.get("title"))
        check("公告正文说清原因（mods/ + RCON）",
              "mods/" in (sl.get("why") or "") and "RCON" in (sl.get("why") or ""), (sl.get("why") or "")[:60])
        check("公告给了出口（去设置）", "设置" in (sl.get("btn") or ""), sl.get("btn"))
        cs = sl.get("contrasts") or {}
        check("标题对比度 ≥ 4.5", (cs.get("title") or 0) >= 4.5, f"{cs.get('titleFg')} → {cs.get('title')}")
        check("正文对比度 ≥ 4.5", (cs.get("why") or 0) >= 4.5, f"{cs.get('whyFg')} → {cs.get('why')}")
        check("页签计数改成「—」（整页不可用，别报一个假条目数）", sl.get("tabCount") == "—", sl.get("tabCount"))
        check("未空打知识库请求（模糊底下没有红色「加载失败」）",
              "加载失败" not in (sl.get("kbList") or ""), (sl.get("kbList") or "")[:60])
        print(f"      公告实测：面板底 {cs.get('panelBg')}(α={cs.get('alpha')}) 合成于 页面 {cs.get('base')} / 卡片 {cs.get('cardBase')}"
              f" → 标题 {cs.get('title')}、正文 {cs.get('why')}")
        page.click('#page_knowledge [data-rmt-goto]')
        page.wait_for_timeout(500)
        check("「去设置」按钮真的能跳到设置页",
              "active" in (page.get_attribute('button.tab[data-page="settings"]', "class") or ""),
              page.get_attribute('button.tab[data-page="settings"]', "class"))
        page.click('button.tab[data-page="settings"]')
        page.wait_for_timeout(400)

        print("[2] 卡片 / 行级磨砂真的生效：内容模糊 + 停用交互；提示牌自己必须清晰")
        s = st["sample"]
        check("被封内容 computed filter 含 blur", "blur" in (s.get("childFilter") or ""), str(s.get("childFilter")))
        check("被封内容 pointer-events:none", s.get("childPE") == "none", str(s.get("childPE")))
        check("被封内容透明度压低（≤0.5）", (s.get("childOpacity") or 1) <= 0.5, str(s.get("childOpacity")))
        check("提示牌不参与模糊（filter:none）", s.get("badgeFilter") == "none", str(s.get("badgeFilter")))
        check("提示牌没越出卡片边界", bool(s.get("badgeIn")), "越界了")
        print("      提示牌纵向位置（占卡片高度比例）：")
        for i in BLOCKS[1:]:
            b = st["blocks"][i]
            print(f"        · {i} → 卡片高 {b.get('h')}px，牌子在 {b.get('topPct')} 处")
        for i in ("cfg_ev_enabled", "cfg_bridge_en"):
            q = st["blocks"][i]
            check(f"{i}（高卡片）提示牌靠上、不会被滚出视野", (q.get("topPct") or 1) <= 0.45, str(q))
        check("行级内容也被模糊", "blur" in (s.get("rowFilter") or ""), str(s.get("rowFilter")))
        check("行级小标签浮在磨砂层之上（z-index ≥ 2）",
              s.get("rowTagZ") not in (None, "auto") and int(s["rowTagZ"]) >= 2, str(s.get("rowTagZ")))

        print("[3] 未受影响的区块不许被误伤")
        others = page.evaluate("() => ['card_policy','card_terminal','cfg_tool_exec','cfg_cmd_say']"
                               ".map(id => { const el = document.getElementById(id);"
                               " const host = el.closest('.card') || el;"
                               " return [id, host.classList.contains('rmt-off') || el.parentElement.classList.contains('rmt-off-row')]; })")
        for oid, hit in others:
            check(f"{oid} 仍可用（未被磨砂）", not hit, str(others))

        print("[4] 反复切回设置页（每次都会重跑 loadSettings）不叠牌子")
        for _ in range(2):
            page.click('button.tab[data-page="overview"]')
            page.wait_for_timeout(300)
            page.click('button.tab[data-page="settings"]')
            page.wait_for_timeout(500)
        st2 = page.evaluate(STATE_JS, ids)
        check("牌子数量仍然 3 / 4（3 卡片提示牌 + 4 小标签）",
              st2["counts"]["badge"] == 3 and st2["counts"]["tag"] == 4, str(st2["counts"]))
        check("目录区软标记没有重复叠加", st2["counts"]["offSoft"] == 1, str(st2["counts"]))
        check("知识库整页公告没有重复叠加", st2["counts"]["seal"] == 1 and st2["counts"]["sealCard"] == 1, str(st2["counts"]))
        check("目录区提示行没有被反复改写（还是异地说明）",
              "异地模式" in st2["sd"]["hintText"] and "logs/" not in st2["sd"]["hintText"],
              st2["sd"]["hintText"][:60])

        print("[5] 服务器页：异地模式顺带说明「版本探测不可用」")
        page.click('button.tab[data-page="server"]')
        page.wait_for_timeout(700)
        sv = page.inner_text("#sv_status")
        check("实时状态里点明了版本探测的降级原因",
              "异地 RCON 模式" in sv and "版本探测" in sv, sv.replace("\n", " / ")[:80])

        print("[6] v0.21.19：开关草稿不许改写封禁 —— 关掉开关但没保存 → 必须照旧封禁")
        page.click('button.tab[data-page="settings"]')
        page.wait_for_timeout(500)
        toggle_sw(page, "#cfg_remote_rcon_mode", False)
        page.wait_for_timeout(400)
        check("开关确实已被关掉（草稿状态）", not page.is_checked("#cfg_remote_rcon_mode"))
        st3 = page.evaluate(STATE_JS, ids)
        check("封禁照旧：.rmt-off 仍是 3 张卡", st3["counts"]["off"] == 3, str(st3["counts"]))
        check("封禁照旧：.rmt-off-row 仍是 3 行", st3["counts"]["offRow"] == 3, str(st3["counts"]))
        check("封禁照旧：整页封禁 / 公告层仍在（旧版本这里会解封 → 主人点进去就是坏的）",
              st3["counts"]["seal"] == 1 and st3["counts"]["sealCard"] == 1, str(st3["counts"]))
        s3 = st3["seal"] or {}
        check("知识库内容仍被模糊压暗 + 停用交互",
              "blur" in (s3.get("childFilter") or "") and (s3.get("childChain") or {}).get("op", 1) <= 0.4
              and s3.get("childPE") == "none",
              f"filter={s3.get('childFilter')} op={(s3.get('childChain') or {}).get('op')} pe={s3.get('childPE')}")
        check("页签计数仍是「—」（别报一个假的可用状态）", s3.get("tabCount") == "—", s3.get("tabCount"))
        check("公告正文补了「你已经把开关关掉了，但还没保存」",
              "还没点「保存全部设置」" in (s3.get("why") or ""), (s3.get("why") or "")[-40:])
        check("「未保存」提示行已出现且可见", st3["pendShown"] == "block" and st3["pendVisible"], st3["pendShown"])
        check("提示行写明「还没有保存」+「仍旧不可用」",
              "还没有保存" in st3["pendText"] and "仍旧不可用" in st3["pendText"], st3["pendText"][:80])
        check("说明行开头改成「后端当前已保存为『开启』」（别对着关掉的开关说「已开启」）",
              st3["hintTag"] == "后端当前已保存为『开启』：", st3["hintTag"])
        check("目录区按草稿恢复可编辑（准备改成本地模式）", not st3["sd"]["inputDisabled"], str(st3["sd"].get("inputDisabled")))
        check("保存提示说清「要保存后才生效」",
              "保存全部设置" in page.inner_text("#cfg_notice_top"), page.inner_text("#cfg_notice_top")[:60])
        print(f"      提示行原文：{st3['pendText'][:70]}…")

        print("[6b] 保存后（后端真的变成本地模式）→ 才解封，并补拉知识库数据")
        page.click("#btn_save_cfg_top")   # 草稿是「未勾选」→ 后端翻成 False
        page.wait_for_timeout(1200)
        check("后端状态确实被这次保存改掉了（mock 收到 remote_rcon_mode=false）",
              state["remote"] is False, str(state))
        st4 = page.evaluate(STATE_JS, ids)
        check(".rmt-off 归零", st4["counts"]["off"] == 0, str(st4["counts"]))
        check(".rmt-off-row 归零", st4["counts"]["offRow"] == 0, str(st4["counts"]))
        check(".rmt-off-soft 归零", st4["counts"]["offSoft"] == 0, str(st4["counts"]))
        check("整页封禁撤掉（.rmt-seal / 公告层归零）",
              st4["counts"]["seal"] == 0 and st4["counts"]["sealCard"] == 0, str(st4["counts"]))
        s4 = st4["seal"] or {}
        check("知识库页内容恢复可交互与清晰",
              s4.get("childPE") != "none" and (s4.get("childChain") or {}).get("op") == 1
              and (s4.get("childFilter") or "none") == "none",
              f"pe={s4.get('childPE')} op={(s4.get('childChain') or {}).get('op')} filter={s4.get('childFilter')}")
        check("页签计数还原（不再是 —）", s4.get("tabCount") != "—", s4.get("tabCount"))
        check("提示牌 / 标签摘净", st4["counts"]["badge"] == 0 and st4["counts"]["tag"] == 0,
              str(st4["counts"]))
        check("目录区原提示行还原（重新出现结构校验说明）",
              "结构校验" in (st4["sd"]["hintText"] or ""), (st4["sd"]["hintText"] or "")[:60])
        check("「未保存」提示行已收起", st4["pendShown"] == "none", st4["pendShown"])
        check("内容恢复可交互（pointer-events 不再是 none）",
              st4["sample"].get("childPE") != "none", str(st4["sample"].get("childPE")))
        check("说明行也收了", st4["hintShown"] == "none", st4["hintShown"])

        print("[6c] v0.21.19 反向：已保存=本地模式时把开关勾上（未保存）→ 不许提前封禁")
        ctx2 = browser.new_context(viewport={"width": 1360, "height": 950}, color_scheme="dark")
        page2 = ctx2.new_page()
        state2 = {"remote": False}
        page2.route(API_GLOB, make_route(state2))
        page2.goto(PAGE_URL)
        page2.wait_for_timeout(900)
        page2.click('button.tab[data-page="settings"]')
        page2.wait_for_timeout(600)
        st5 = page2.evaluate(STATE_JS, ids)
        check("本地模式：没有被封的卡片", st5["counts"]["off"] == 0, str(st5["counts"]))
        check("本地模式：知识库页没被封", st5["counts"]["seal"] == 0, str(st5["counts"]))
        toggle_sw(page2, "#cfg_remote_rcon_mode", True)
        page2.wait_for_timeout(400)
        st6 = page2.evaluate(STATE_JS, ids)
        check("勾上但没保存：封禁**不许**提前生效（后端还是本地模式）",
              st6["counts"]["off"] == 0 and st6["counts"]["seal"] == 0, str(st6["counts"]))
        check("勾上但没保存：提示行说明「保存后才会禁用」",
              st6["pendShown"] == "block" and "还没有保存" in st6["pendText"]
              and "才会" in st6["pendText"], st6["pendText"][:80])
        check("勾上但没保存：知识库页签计数照旧是数字", (st6["seal"] or {}).get("tabCount") != "—",
              (st6["seal"] or {}).get("tabCount"))
        page2.click("#btn_save_cfg_top")
        page2.wait_for_timeout(1200)
        check("保存后后端翻成异地模式（mock 收到 remote_rcon_mode=true）", state2["remote"] is True, str(state2))
        st7 = page2.evaluate(STATE_JS, ids)
        check("保存后才封禁：3 张卡 + 3 行 + 整页公告",
              st7["counts"]["off"] == 3 and st7["counts"]["offRow"] == 3
              and st7["counts"]["seal"] == 1, str(st7["counts"]))
        check("保存后提示行收起", st7["pendShown"] == "none", st7["pendShown"])
        ctx2.close()

        print("[7] 截图（深 / 浅两套底色各来一张，人眼复核亚克力）")
        state["remote"] = True                 # 截封禁态：把 mock 的「已保存」状态拨回异地模式
        toggle_sw(page, "#cfg_remote_rcon_mode", True)
        page.wait_for_timeout(400)
        for theme in ("dark", "light"):
            page.evaluate(f"localStorage.setItem('mcctrl_theme','{theme}')")
            page.reload()
            page.wait_for_timeout(1000)
            page.click('button.tab[data-page="settings"]')
            page.wait_for_timeout(600)
            stt = page.evaluate(STATE_JS, ids)
            check(f"[{theme}] 目录区文字对比度 ≥ 4.5（两套底色都要过关）",
                  (stt["sd"].get("contrast") or 0) >= 4.5,
                  f"{stt['sd'].get('fg')} on {stt['sd'].get('bg')} = {stt['sd'].get('contrast')}")
            check(f"[{theme}] 目录区文字未被模糊/淡化",
                  stt["sd"].get("inputFilter") == "none" and stt["sd"]["inputChain"]["op"] >= 0.9,
                  str(stt["sd"]["inputChain"]))
            page.evaluate("document.getElementById('card_policy')?.scrollIntoView()")
            page.wait_for_timeout(200)
            page.screenshot(path=str(SHOTS / f"rmt_blur_{theme}.png"))
            page.locator("#cfg_dict_en").locator("xpath=ancestor::div[contains(@class,'card')]").screenshot(
                path=str(SHOTS / f"rmt_blur_{theme}_card.png"))
            # 目录区特写（本次反馈的现场：文字必须看得清）
            page.locator("#sd_box").locator("xpath=ancestor::div[contains(@class,'card')]").screenshot(
                path=str(SHOTS / f"rmt_soft_{theme}.png"))
            print(f"      {SHOTS / f'rmt_soft_{theme}.png'}")
            print(f"      {SHOTS / f'rmt_blur_{theme}.png'}")
            # v0.21.18：知识库整页封禁（整页磨砂 + 居中公告）
            page.click('button.tab[data-page="knowledge"]')
            page.wait_for_timeout(500)
            stk2 = page.evaluate(STATE_JS, ids)["seal"] or {}
            check(f"[{theme}] 知识库整页封禁生效且公告文字可读",
                  stk2.get("sealed") and (stk2.get("contrasts") or {}).get("why", 0) >= 4.5,
                  f"sealed={stk2.get('sealed')} why={((stk2.get('contrasts') or {}).get('why'))}")
            page.screenshot(path=str(SHOTS / f"rmt_seal_{theme}.png"))
            print(f"      {SHOTS / f'rmt_seal_{theme}.png'}")
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
