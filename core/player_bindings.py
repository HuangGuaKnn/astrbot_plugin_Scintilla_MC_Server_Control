"""v0.10.0 玩家绑定存储：AstrBot 用户 → Minecraft 真实游戏名。

用于「决策 AI 玩家守门」：
- 玩家通过聊天平台的唤醒词指令（如 mcs 绑定，前缀跟随 AstrBot 唤醒词设置）
  输入「mcs 绑定 <MC玩家ID>」把自己的 AstrBot 账号与 MC 游戏名绑定
  （mcs 解绑 / mcs 查询 可解绑与查询；绑定/解绑不再交给 LLM 自行判断）。
- 任务未指名玩家时自动回落到绑定名；两者皆无则终止任务并通知。
- 用户指令中明确声明的玩家名优先级永远高于绑定。

数据以 JSON 持久化在插件数据目录下（player_bindings.json）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

# Minecraft 玩家名规则：1~16 位，字母/数字/下划线（宽松校验）
_PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class PlayerBindings:
    """线程安全的玩家绑定存储。"""

    def __init__(self, data_dir: str | Path):
        self._path = Path(data_dir) / "player_bindings.json"
        self._lock = threading.RLock()  # 可重入：bind/unbind 内部会再调用 _save
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # =============== 读写 ===============

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except Exception:
            self._data = {}

    def _save(self) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self._path)
            except Exception:
                pass

    # =============== 接口 ===============

    @staticmethod
    def normalize(name: str) -> str:
        """清洗玩家名：去空格，非法字符返回空串。"""
        n = (name or "").strip()
        return n if _PLAYER_RE.match(n) else ""

    def bind(self, user_id: str, player_name: str) -> tuple[bool, str]:
        """绑定用户 → 玩家。返回 (是否成功, 说明/错误)。"""
        uid = str(user_id or "").strip()
        name = self.normalize(player_name)
        if not uid:
            return False, "用户 ID 为空，无法绑定。"
        if not name:
            return False, (
                f"玩家名「{player_name}」不合法（MC 玩家名应为 1~16 位字母/数字/下划线）。"
            )
        with self._lock:
            self._data[uid] = {"player": name, "ts": time.time()}
            self._save()
        return True, f"已绑定：{uid} → {name}"

    def unbind(self, user_id: str) -> tuple[bool, str]:
        uid = str(user_id or "").strip()
        with self._lock:
            if uid in self._data:
                del self._data[uid]
                self._save()
                return True, f"已解除绑定（{uid}）。"
        return False, f"当前用户（{uid or '未知'}）没有绑定记录。"

    def get(self, user_id: str) -> str:
        """返回绑定玩家名；未绑定返回空串。"""
        uid = str(user_id or "").strip()
        rec = self._data.get(uid) or {}
        return str(rec.get("player", "") or "").strip()

    def info(self, user_id: str) -> dict:
        uid = str(user_id or "").strip()
        rec = self._data.get(uid) or {}
        if not rec:
            return {"bound": False, "user_id": uid, "player": "", "ts": 0}
        return {
            "bound": True,
            "user_id": uid,
            "player": str(rec.get("player", "") or ""),
            "ts": float(rec.get("ts", 0) or 0),
        }

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)
