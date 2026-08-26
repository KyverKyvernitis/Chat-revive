"""Índice leve de mensagens enviadas pelo chatbot.

Resolve replies de forma robusta usando `message_id -> profile_id`. Isso evita
heurística por nome/avatar do webhook, que quebra quando o profile usa identidade
dinâmica, como personas baseadas em membros.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

from . import constants as C
from .lru_cache import LRUCacheTTL


@dataclass(frozen=True)
class MessageProfileRef:
    guild_id: int
    channel_id: int
    message_id: int
    profile_id: str
    created_at: float = 0.0

    @classmethod
    def from_doc(cls, doc: dict) -> "MessageProfileRef":
        return cls(
            guild_id=int(doc.get("guild_id") or 0),
            channel_id=int(doc.get("channel_id") or 0),
            message_id=int(doc.get("message_id") or 0),
            profile_id=str(doc.get("profile_id") or ""),
            created_at=float(doc.get("created_at") or 0.0),
        )


class MessageProfileIndex:
    """Persistência + cache RAM para mapear mensagens do webhook ao profile."""

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._cache: LRUCacheTTL[int, MessageProfileRef] = LRUCacheTTL(
            max_entries=C.MESSAGE_PROFILE_CACHE_MAX_ENTRIES,
            ttl_seconds=C.MESSAGE_PROFILE_CACHE_TTL_SECONDS,
        )

    async def remember(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        profile_id: str,
    ) -> None:
        if not message_id or not profile_id:
            return
        now = time.time()
        ref = MessageProfileRef(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            message_id=int(message_id),
            profile_id=str(profile_id),
            created_at=now,
        )
        self._cache.set(int(message_id), ref)
        await self._coll.update_one(
            {
                "type": C.DOC_TYPE_MESSAGE_MAP,
                "message_id": int(message_id),
            },
            {
                "$set": {
                    "type": C.DOC_TYPE_MESSAGE_MAP,
                    "guild_id": int(guild_id),
                    "channel_id": int(channel_id),
                    "message_id": int(message_id),
                    "profile_id": str(profile_id),
                    "created_at": now,
                    "expires_at": datetime.now(timezone.utc) + timedelta(
                        seconds=C.MESSAGE_PROFILE_CACHE_TTL_SECONDS
                    ),
                }
            },
            upsert=True,
        )

    async def resolve(self, message_id: int) -> Optional[MessageProfileRef]:
        message_id_i = int(message_id or 0)
        if message_id_i <= 0:
            return None
        cached = self._cache.get(message_id_i)
        if cached is not None:
            return cached
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MESSAGE_MAP,
            "message_id": message_id_i,
        })
        if not doc:
            return None
        ref = MessageProfileRef.from_doc(doc)
        if ref.message_id and ref.profile_id:
            self._cache.set(message_id_i, ref)
            return ref
        return None

    async def cleanup_old(self) -> int:
        cutoff = time.time() - C.MESSAGE_PROFILE_CACHE_TTL_SECONDS
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MESSAGE_MAP,
            "created_at": {"$lt": cutoff},
        })
        return int(getattr(result, "deleted_count", 0) or 0)
