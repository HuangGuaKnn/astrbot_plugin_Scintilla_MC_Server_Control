# -*- coding: utf-8 -*-
"""UI 用例共用的「页面从哪来 / 接口怎么拦」—— 全仓**唯一**一份。

背景（v0.24.11 的两次翻车）
==========================
1. **页面地址**：原先各用例自己写 `(PLUGIN / "pages" / ... / "index.html").as_uri()`，
   即用 `file://` 打开。页面里的 `fetch("/api/...")` 在 file:// 下会变成
   `file:///<盘符>/api/...`，Chromium 直接拒绝（"URL scheme file is not supported"），
   playwright 的 `page.route` 也拦不到 —— 于是表现为「点了保存却没有请求」。
   现在统一走 `_paths.serve_plugin_root()` 起的**本地 HTTP**：同源 fetch 正常、route 能拦、
   localStorage / cookie 行为也与真实环境一致。
2. **接口匹配串**：`API_GLOB` 原先在 4 个文件里各写一份（其中一份是折行写法），
   改一处漏三处。现在只此一份。

用法
====
    from _ui_page import PAGE_URL, API_GLOB, VIEWPORT, make_router, fulfill
"""

from __future__ import annotations

import json
import os

from _paths import serve_plugin_root

# 页面地址：默认本地 HTTP（见模块 docstring）；仍可用环境变量覆盖（排查旧页面时有用）
PAGE_URL = os.environ.get("THEME_TEST_PAGE") or (
    serve_plugin_root() + "/pages/mc_control/index.html"
)

# 本插件 WebUI 的全部后端接口都在这条前缀下
API_GLOB = "**/api/v1/plugins/extensions/astrbot_plugin_Scintilla_MC_Server_Control/page/**"

# 统一的视口（各用例都用它，避免「我这里宽一点所以没换行」这类环境差异）
VIEWPORT = {"width": 1360, "height": 950}


def split_url(route_obj) -> str:
    """把请求 URL 折成「page/ 之后的相对路径」（去掉查询串）。"""
    return route_obj.request.url.split("/page/", 1)[-1].split("?")[0]


def fulfill(route_obj, payload) -> None:
    """回一个 200 + JSON（中文不转义，便于失败时肉眼读）。"""
    route_obj.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))


def make_router(gets: dict, post=None, on_call=None, default_get=None):
    """造一个 `route(route_obj)` 处理函数。

    gets: {相对路径: () -> payload}；未命中用 default_get（默认回 `mock: 未实现`）。
    post: (url, body) -> payload；不给就一律回 `mock: 未实现 POST`。
    on_call: (method, url) 回调（用例用它记录调用序列）。
    default_get: 未知 GET 的回包工厂 —— 有的用例约定「未知路径一律 {"ok": True}」，
                 把这条差异显式交出来，而不是让每个用例自己写一遍分发。
    """
    if default_get is None:
        default_get = lambda url: {"ok": False, "error": "mock: 未实现 " + url}  # noqa: E731

    def route(route_obj):
        url = split_url(route_obj)
        if on_call is not None:
            on_call(route_obj.request.method, url)
        if route_obj.request.method == "GET":
            fn = gets.get(url)
            return fulfill(route_obj, fn() if fn is not None else default_get(url))
        try:
            body = json.loads(route_obj.request.post_data or "{}")
        except Exception:  # noqa: BLE001 —— 请求体坏了就当空对象，别让用例炸在 mock 上
            body = {}
        if post is None:
            return fulfill(route_obj, {"ok": False, "error": "mock: 未实现 POST " + url})
        return fulfill(route_obj, post(url, body))

    return route


__all__ = ["PAGE_URL", "API_GLOB", "VIEWPORT", "split_url", "fulfill", "make_router"]
