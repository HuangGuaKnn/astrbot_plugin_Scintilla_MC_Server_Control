"""监控、页面卸载、孤儿任务与审计尾部读的定向变异。"""
import sys

from mutation_all import Case


def mutation_cases():
    python = (sys.executable, "-B", "-X", "utf8")
    browser = (*python, "tests/ui_server_card_check.py")
    backend = (*python, "tests/test_v0410_health_followup.py")
    html = "pages/mc_control/index.html"
    watcher = "core/log_watcher.py"
    return [
        Case("H1", "离开监控仍渲染状态", html,
             "if(active && revision === monitorEpoch) renderServerStatus(result);",
             "if(active) renderServerStatus(result);",
             browser, "H1 切页后的在飞响应不得重绘隐藏监控", browser),
        Case("H2", "漏掉 pagehide 生命周期处理", html,
             'window.addEventListener("pagehide", handlePageHide);', "// pagehide",
             browser, "H2 pagehide 须清空轮询定时器", browser),
        Case("H3", "取消孤儿登记硬上限", watcher,
             "if len(self._orphaned) > MAX_ORPHANED_TASKS:", "if False:",
             (*backend, "OrphanChecks.test_capacity_cancels_oldest"), "H3 孤儿任务须受硬上限约束", backend),
        Case("H4", "先读完整审计文件再逆序", "core/player_admin.py",
             "    position = stream.seek(0, io.SEEK_END)",
             "    yield from reversed(stream.read().splitlines())\n    return\n    position = stream.seek(0, io.SEEK_END)",
             (*backend, "AuditChecks.test_large_tail_matches_forward_scan_and_reads_few_bytes"),
             "H4 审计读尾字节数须远小于文件", backend),
        Case("H5", "推迟孤儿超时取消", watcher,
             'loop.call_later(ORPHAN_TIMEOUT, self._cancel_orphan, task, "超时")',
             'loop.call_later(3600, self._cancel_orphan, task, "超时")',
             (*backend, "OrphanChecks.test_timeout_and_natural_completion"), "H5 孤儿任务超时后须请求取消", backend),
        Case("H6", "审计路由退回事件循环同步读", "core/web_api.py",
             'ok, items = await run_blocking(player_admin.get_audit, limit, what="读取玩家操作审计")',
             "ok, items = True, player_admin.get_audit(limit)",
             (*backend, "AuditChecks.test_route_uses_daemon_executor"), "H6 审计路由须使用 daemon 执行器", backend),
        Case("H7", "离开监控仍渲染事件", html,
             "if(revision === monitorEpoch) renderServerEvents(response);",
             "renderServerEvents(response);",
             browser, "H1 切页后的在飞响应不得重绘隐藏监控", browser),
        Case("H8", "直连请求漏传取消信号", html,
             'const opt = { method, signal, credentials: "include", headers: {} };',
             'const opt = { method, credentials: "include", headers: {} };',
             browser, "H2 在飞 fetch 须取消且只取消一次", browser),
    ]
