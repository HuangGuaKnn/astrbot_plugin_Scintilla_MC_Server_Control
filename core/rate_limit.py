"""按 key 的冷却计数器（v0.23.5 · 喊话限频）。

背景
----
``mcs 喊话`` 是**插件自带功能**，不受白 / 黑名单策略影响，默认对全员开放
（这是产品设计：把群聊搬进服务器）。原先它只有两个开关（是否启用、是否仅管理员）
与一条 ``[审计]`` 日志，**没有任何频率限制**：

* 群里的刷屏机器人 / 手快的人连点，每条都会变成一次 RCON 往返；
* 服务器聊天栏被同一批内容淹没，而 RCON 是**共享连接**，刷屏会把别人的
  正常指令（发物品、踢人、查状态）挤在后面排队。

做法
----
极薄的「最近一次放行时间」表。只做冷却，不做队列、不做令牌桶 ——
喊话是软限制，晚几百毫秒放行毫无意义，直接拒绝并告诉对方还要等几秒更实在。

两个刻意的设计
--------------
* **键表有上限**（``max_keys``）：一个群可能有很多人，也可能有人换小号刷；
  表满就按「最近一次放行时间」淘汰最旧的那批，内存绝不无界增长。
* **时钟可注入**（``clock``）：测试用假时钟推进，不必真的 ``sleep``。
"""

from __future__ import annotations

import threading
import time

__all__ = ["Cooldown"]


class Cooldown:
    """同一 key 在 N 秒内只放行一次。

    典型用法::

        cd = Cooldown()
        wait = cd.hit(sender_id, 5.0)   # 0.0 = 放行；> 0 = 还要等这么多秒
        if wait > 0:
            ...

    ``hit()`` **只在放行时记账**：被拒绝的那次不会刷新计时器，
    否则连点的人等于永远卡在冷却窗口里、越点越等不到。
    """

    #: 键表默认上限。够用且不会无界增长：单个群/会话的活跃发言者远小于此。
    DEFAULT_MAX_KEYS = 512

    def __init__(self, max_keys: int = DEFAULT_MAX_KEYS, clock=time.monotonic):
        self._max_keys = max(1, int(max_keys))
        self._clock = clock
        self._last: dict[str, float] = {}
        # 冷却表会被指令协程并发访问（同一事件循环内多个会话同时喊话），
        # 加一把轻量锁：临界区只有几行字典操作，开销可以忽略。
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 查询

    def remaining(self, key: str, seconds: float) -> float:
        """还需要等待的秒数（只读，不记账）：0.0 表示现在就能放行。"""
        if not seconds or seconds <= 0:
            return 0.0
        with self._lock:
            last = self._last.get(key)
        if last is None:
            return 0.0
        elapsed = self._clock() - last
        return max(0.0, float(seconds) - elapsed)

    def hit(self, key: str, seconds: float) -> float:
        """尝试放行：返回 0.0 表示**已放行并记账**，否则返回还要等的秒数。"""
        if not seconds or seconds <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None:
                wait = float(seconds) - (now - last)
                if wait > 0:
                    return wait
            self._last[key] = now
            if len(self._last) > self._max_keys:
                self._prune_locked()
        return 0.0

    # ---------------------------------------------------------------- 维护

    def forget(self, key: str | None = None) -> int:
        """清掉某个 key 的冷却（``None`` = 全清），返回清掉的条数。"""
        with self._lock:
            if key is None:
                n = len(self._last)
                self._last.clear()
                return n
            return 1 if self._last.pop(key, None) is not None else 0

    def _prune_locked(self) -> None:
        """表满时按最近一次放行时间淘汰最旧的，直到回到上限之内。"""
        overflow = len(self._last) - self._max_keys
        if overflow <= 0:
            return
        for k, _ in sorted(self._last.items(), key=lambda kv: kv[1])[:overflow]:
            self._last.pop(k, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._last)
