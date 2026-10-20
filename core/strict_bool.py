"""严格布尔解析 —— 全工程唯一实现（v0.24.8）。

背景（GPT 第三轮复核 P4）：修 v0.24.7 时把「严格解析」只贴在了 WebUI 写接口边界，
读配置的几条路仍然各自裸用 ``bool()``，于是同一个配置文件能读出互相矛盾的结论，
而且方向清一色 fail-open（把「关」读成「开」）：

  * ``core/workflow.py::enabled``      —— ``"false"`` → True
  * ``core/knowledge_base.py::load()`` —— 文件里 ``"enabled": "false"`` → True，**关不掉**
  * ``core/web_api.py`` 状态读取多处（审计时计 18 处）—— WebUI 显示的开关与实际生效相反。
    整改后该文件只余 7 处**运行态** ``bool()``（对象属性 / health / 密码非空），
    配置读取一律走本模块；判据同时扫 ``bool(self._cfg(`` 与 ``bool(self.plugin._cfg(``。

本模块只提供两种语义，调用方必须显式选一个：

  * :func:`parse_bool`  —— 严格：无法判定就抛 ``ValueError``。用于**外部输入入口**
    （WebUI 写接口、工具开关这类 fail-closed 场景）。
  * :func:`strict_bool` —— 严格 + 回落：无法判定时取调用方给的 ``default``，并留一条
    WARNING。用于**读配置**场景（热路径不能抛 500）。

注意：``None`` 在 :func:`strict_bool` 里直接取 ``default``（键不存在与「显式写了 null」
都按缺省处理）；空串仍按 :func:`parse_bool` 的语义视为 False（保守关闭）。
"""
from __future__ import annotations

from astrbot.api import logger

_log = logger

_TRUE_TOKENS = frozenset({"true", "1", "yes", "on", "y", "t"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off", "n", "f", ""})


def parse_bool(value) -> bool:
    """严格解析布尔值；无法确定时抛 ``ValueError``（语义与 v0.23.5 完全一致）。

    只认三类输入，其余一律报错、绝不猜：

      * JSON 布尔 ``true`` / ``false``
      * 数字 ``0`` / ``1``（含 ``0.0`` / ``1.0``）
      * 字符串词表 ``true``/``false``、``yes``/``no``、``on``/``off``、``1``/``0``
        （忽略大小写与首尾空白）

    ``None`` 与空串按 False 处理（方向保守 = 关闭）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):          # bool 已在上面拦掉
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"数字只接受 0 或 1，收到 {value!r}")
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ValueError(f"无法解析为布尔值：{value!r}")
    raise ValueError(f"不支持的类型 {type(value).__name__}：{value!r}")


def strict_bool(value, default: bool, *, key: str = "", logger=None) -> bool:
    """严格解析；读不出结论时回落 ``default`` 并留 WARNING（读配置专用）。"""
    if value is None:
        return bool(default)
    try:
        return parse_bool(value)
    except ValueError:
        (_log if logger is None else logger).warning(
            "配置项 %s 的值无法解析为布尔（%r），已按默认值 %s 处理",
            key or "(未命名)", value, bool(default),
        )
        return bool(default)
