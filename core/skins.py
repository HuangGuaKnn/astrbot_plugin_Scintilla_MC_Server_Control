"""Minecraft 离线玩家 UUID 与经典默认模型。"""
from __future__ import annotations

import hashlib
from uuid import UUID


def offline_uuid(name: str) -> UUID:
    """等同于 Java 的 UUID.nameUUIDFromBytes，保留玩家名原有大小写。"""
    payload = ("OfflinePlayer:" + name).encode("utf-8")
    return UUID(bytes=hashlib.md5(payload, usedforsecurity=False).digest(), version=3)


def _uuid_model(player_uuid: UUID) -> str:
    # Java UUID.hashCode() 异或四段 32 位字。符号位不影响 & 1。
    value = player_uuid.int
    hash_code = value ^ (value >> 32) ^ (value >> 64) ^ (value >> 96)
    return "alex" if hash_code & 1 else "steve"


def default_model(name: str) -> str:
    return _uuid_model(offline_uuid(name))


def avatar_metadata(name: str) -> dict[str, str]:
    player_uuid = offline_uuid(name)
    return {"uuid": str(player_uuid), "default_model": _uuid_model(player_uuid)}
