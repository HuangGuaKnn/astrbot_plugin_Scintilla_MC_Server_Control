"""后台任务登记册（v0.23.x · 生命周期收口）。

背景
----
插件里到处 ``asyncio.create_task(...)`` 丢协程，谁都不持有引用：

* 插件被卸载 / 重载时，这些任务**不会被取消**——事件循环还活着（AstrBot 热重载
  复用同一个 loop），于是任务继续跑，却握着一个已经 ``terminate()`` 过的插件：
  往会话发通知、写知识库向量、跑多 Agent 流水线……全是「幽灵任务」。
* 协程里抛异常没人取，日志里只会冒一句 "Task exception was never retrieved"，
  连是哪个任务都不知道。
* 同一个具名任务可能被并发拉起（典型：知识库向量构建同时被启动流程、WebUI 开关、
  换嵌入模型三处触发）—— ``build_vectors()`` 并发跑同一个 ``SemanticIndex``
  会互相踩（``force`` 直接把 ``self._sem`` 整个换掉，另一个协程还握着旧引用）。

做法
----
一个极薄的登记册，只做四件事：**持有、去重、记异常、收口**。

策略（``policy``）
------------------
* ``"replace"`` —— 同名任务在跑就先取消，再换新的（默认）。适合「后一次请求
  语义上覆盖前一次」：开关切换、换嵌入模型（旧模型算的向量本来就不能用）。
* ``"skip"``    —— 同名任务在跑就**丢弃新协程**，保留旧的。适合「攒批」：
  知识写入后的向量补算，来一百次也只烧一轮嵌入调用。
* ``"parallel"``—— 允许同名并发，全部登记在一个组里，收口时一起取消。
  适合「互不相关的多 Agent 流水线」。

收口豁免（``cancel_on_shutdown=False``）
--------------------------------------
极少数任务**不能**在收口时被取消，典型是本插件的热重载：它此刻正在
``await pm.reload()``，而 AstrBot 的原生 reload 流程本身就会调用本插件的
``terminate()``（terminate → unbind → load）。收口时把它取消，等于把重载从
中间掐断，插件会停在半加载状态 —— 比「幽灵任务」严重得多。

这类任务照常登记（异常照样记、快照照样看得见），但 ``shutdown()`` 既不取消
也不等待它。它必须是自己会结束的一次性短任务，否则就退化成幽灵任务了。

注意：被丢弃的协程必须显式 ``close()``，否则 Python 会报
"coroutine was never awaited"。
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

# 上架规范：插件的日志器必须来自 astrbot.api，不得使用标准库 logging
# （v0.22.1 市场审核驳回项，由 tests/test_review_compliance.py 护栏钉住）。
# 构造参数 logger 会遮蔽这个模块名，故留一份别名供兜底使用。
from astrbot.api import logger as _DEFAULT_LOGGER

#: 收口时等待每个任务退出的超时（秒）。超时的任务放弃等待并记一条警告。
DEFAULT_SHUTDOWN_TIMEOUT = 5.0

POLICY_REPLACE = "replace"
POLICY_SKIP = "skip"
POLICY_PARALLEL = "parallel"
_POLICIES = (POLICY_REPLACE, POLICY_SKIP, POLICY_PARALLEL)

#: 打在 Task 上的「收口豁免」标记属性名（见 spawn 的 cancel_on_shutdown）
_NO_CANCEL_ATTR = "_astrbot_mc_bg_no_cancel"


class BackgroundTasks:
    """插件级后台任务登记册。

    用法::

        bg = BackgroundTasks(logger)
        bg.spawn("kb_semantic", coro(), policy="replace")
        bg.spawn("workflow", coro(), policy="parallel")
        ...
        await bg.shutdown()      # 取消 + 等待，插件 terminate() 里调一次
    """

    def __init__(self, logger=None, *, shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT):
        # 日志器：优先用插件传入的 AstrBot 插件 logger（带 plugin_tag 前缀），
        # 否则退回 astrbot.api 的全局 logger —— 两条路都是 astrbot.api 的日志器。
        self._log = logger or _DEFAULT_LOGGER
        #: 具名槽位：一个名字同时最多一个任务（replace / skip 用）
        self._named: dict[str, asyncio.Task] = {}
        #: 松散任务组：名字 → 任务集合（parallel 用）
        self._loose: dict[str, set[asyncio.Task]] = {}
        self._closing = False
        self._shutdown_timeout = shutdown_timeout
        #: v0.23.5：收口时「超时未退出、已放弃等待」的任务名（册子清空后仍留档）
        self._gave_up: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 查询

    @property
    def closing(self) -> bool:
        """是否已进入收口流程（此后再 spawn 一律丢弃）。"""
        return self._closing

    def get(self, name: str) -> asyncio.Task | None:
        """取具名槽位上的任务（可能已 done，但还没被回调摘掉）。"""
        return self._named.get(name)

    def is_running(self, name: str) -> bool:
        task = self._named.get(name)
        return task is not None and not task.done()

    def names(self) -> tuple[str, ...]:
        return tuple(self._named)

    def snapshot(self) -> dict[str, Any]:
        """给 WebUI / 诊断用的一眼体检结果。"""
        named = {
            n: ("running" if not t.done() else ("cancelled" if t.cancelled() else "done"))
            for n, t in self._named.items()
        }
        loose = {
            n: sorted(
                "running" if not t.done() else ("cancelled" if t.cancelled() else "done")
                for t in tasks
            )
            for n, tasks in self._loose.items()
            if tasks
        }
        return {
            "closing": self._closing,
            "named": named,
            "groups": loose,
            "alive": sum(1 for t in self._all_tasks() if not t.done()),
            # v0.23.5：收口时放弃等待的任务 —— 册子已空，但这些名字仍是「可能还活着」的
            "gave_up_at_shutdown": list(self._gave_up),
        }

    # ------------------------------------------------------------------ 登记

    def spawn(self, name: str, coro: Coroutine[Any, Any, Any], *,
              policy: str = POLICY_REPLACE,
              cancel_on_shutdown: bool = True) -> asyncio.Task | None:
        """把协程登记进册子并拉起。返回任务对象；被丢弃时返回 ``None``。

        ``coro`` 必须是协程对象（不是 ``asyncio.Task``）—— 被丢弃时本方法需要
        ``close()`` 它，传 Task 就关不掉了。

        ``cancel_on_shutdown=False``：收口时**不取消也不等待**这个任务。
        只给「自己会结束的一次性短任务」用，且它做的事恰好会触发 terminate
        （见模块文档「收口豁免」），否则就是自找幽灵任务。
        """
        if policy not in _POLICIES:
            self._log.warning("后台任务 %s 指定了未知策略 %r，按 replace 处理", name, policy)
            policy = POLICY_REPLACE

        if self._closing:
            self._drop(coro, name, "登记册已收口")
            return None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._drop(coro, name, "当前没有运行中的事件循环")
            return None

        if policy == POLICY_PARALLEL:
            task = self._mark(loop.create_task(coro), cancel_on_shutdown)
            self._loose.setdefault(name, set()).add(task)
            task.add_done_callback(
                lambda t, n=name: self._reap_loose(n, t)
            )
            return task

        existing = self._named.get(name)
        if existing is not None and not existing.done():
            if policy == POLICY_SKIP:
                self._drop(coro, name, "同名任务已在运行，按 skip 丢弃")
                return None
            # replace：先取消旧的。不 await —— 取消是异步的，新任务不必等它收尾。
            existing.cancel()

        task = self._mark(loop.create_task(coro), cancel_on_shutdown)
        self._named[name] = task
        task.add_done_callback(lambda t, n=name: self._reap_named(n, t))
        return task

    def adopt(self, name: str, task: asyncio.Task) -> asyncio.Task:
        """把**已经**创建好的任务收编进册子（兼容旧代码路径）。

        调用方自己 ``create_task`` 出来的任务照样能被收口；收编后异常也由本册
        统一记录。注意 ``policy`` 语义在这里不生效 —— 任务已经跑起来了。
        """
        self._loose.setdefault(name, set()).add(task)
        task.add_done_callback(lambda t, n=name: self._reap_loose(n, t))
        return task

    def cancel(self, name: str) -> int:
        """取消某个具名槽位 / 某个松散组的全部任务，返回取消个数。"""
        count = 0
        task = self._named.get(name)
        if task is not None and not task.done():
            task.cancel()
            count += 1
        for t in self._loose.get(name, set()):
            if not t.done():
                t.cancel()
                count += 1
        return count

    # ------------------------------------------------------------------ 收口

    async def shutdown(self) -> int:
        """取消并等待所有仍在跑的任务。可重复调用（幂等）。

        返回被取消的任务数。超时未退出的任务只记警告 —— 收口**绝不能**把
        ``terminate()`` 卡死。
        """
        self._closing = True
        alive = [t for t in self._all_tasks() if not t.done()]
        exempt = [t for t in alive if getattr(t, _NO_CANCEL_ATTR, False)]
        pending = [t for t in alive if t not in exempt]

        for t in exempt:
            # 收口豁免：取消它等于把它正在做的事（如热重载）从中间掐断
            self._log.info(
                "后台任务 %s 收口豁免：它正在做会触发 terminate 的工作，交由它自行结束",
                self._label(t),
            )
        for t in pending:
            t.cancel()

        cancelled = len(pending)
        if pending:
            done, still = await asyncio.wait(pending, timeout=self._shutdown_timeout)
            self._harvest(done)                     # 取异常，避免 never retrieved
            for t in still:
                self._log.warning(
                    "后台任务 %s 在 %.1fs 内未退出，已放弃等待（可能阻塞在 IO）",
                    self._label(t), self._shutdown_timeout,
                )
            # v0.23.5 外部复核：下面两行会把册子清空 —— 于是「超时未退出」的任务从此
            # 在册子上查无此人（snapshot() 会说「没有在跑的」，实际可能还有一个在爬）。
            # 收口阶段做不了更多事，但至少要留下名字，别让诊断信息自相矛盾。
            if still:
                self._gave_up = tuple(self._label(t) for t in still)

        self._named.clear()
        self._loose.clear()
        return cancelled

    @staticmethod
    def _mark(task: asyncio.Task, cancel_on_shutdown: bool) -> asyncio.Task:
        """给任务打上「收口豁免」标记（属性打不上就算了，不影响其它逻辑）。"""
        if not cancel_on_shutdown:
            try:
                setattr(task, _NO_CANCEL_ATTR, True)
            except AttributeError:  # pragma: no cover - Task 不允许设属性时
                pass
        return task

    # -------------------------------------------------------------- 内部工具

    def _all_tasks(self) -> list[asyncio.Task]:
        out = list(self._named.values())
        for tasks in self._loose.values():
            out.extend(tasks)
        return out

    def _drop(self, coro: Coroutine[Any, Any, Any], name: str, why: str) -> None:
        """丢弃一个还没被拉起的协程（必须 close，否则吃 never-awaited 警告）。"""
        try:
            coro.close()
        except Exception:
            pass
        self._log.debug("后台任务 %s 未拉起：%s", name, why)

    def _reap_named(self, name: str, task: asyncio.Task) -> None:
        # 先取异常再摘槽位：反过来的话 _label() 已经查不到名字，
        # 日志里只剩一个 <detached #xxxx>，等于白记。
        self._harvest([task], label=name)
        # 只在「槽位里还是它」时才摘，避免 replace 时旧任务的回调误删新任务
        if self._named.get(name) is task:
            del self._named[name]

    def _reap_loose(self, name: str, task: asyncio.Task) -> None:
        self._harvest([task], label=f"{name}#{id(task) & 0xFFFF:04x}")
        tasks = self._loose.get(name)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._loose.pop(name, None)

    def _harvest(self, tasks, label: str | None = None) -> None:
        """取走任务结果：取消的忽略，出异常的记一条带名字的日志。"""
        for t in tasks:
            if t.cancelled():
                continue
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                continue
            except Exception:                        # 取不到就算了，不能因此炸掉收口
                continue
            if exc is not None:
                self._log.warning("后台任务 %s 异常退出: %s", label or self._label(t), exc)

    def _label(self, task: asyncio.Task) -> str:
        for name, t in self._named.items():
            if t is task:
                return name
        for name, tasks in self._loose.items():
            if task in tasks:
                return f"{name}#{id(task) & 0xFFFF:04x}"
        return f"<detached #{id(task) & 0xFFFF:04x}>"


__all__ = [
    "BackgroundTasks",
    "POLICY_REPLACE",
    "POLICY_SKIP",
    "POLICY_PARALLEL",
    "DEFAULT_SHUTDOWN_TIMEOUT",
]
