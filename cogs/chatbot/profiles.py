"""Profiles e configuração efetiva do chatbot.

O campo legado ``profile.active`` continua sincronizado para permitir rollback,
mas a fonte de verdade V2 é um único documento ``chatbot_guild_config``. Isso
torna ativação/desativação atômica para os leitores e impede que menção nominal,
reply ou modo extrovert contornem ``/chatbot desativar``.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from . import constants as C
from .lru_cache import LRUCacheTTL


class ProfileLimitReached(ValueError):
    """O servidor já atingiu o limite de profiles."""


@dataclass(frozen=True)
class GuildChatbotConfig:
    guild_id: int
    enabled: bool = False
    active_profile_id: str = ""
    schema_version: int = C.CHATBOT_SCHEMA_VERSION
    updated_at: float = 0.0

    @classmethod
    def from_doc(cls, doc: dict) -> "GuildChatbotConfig":
        return cls(
            guild_id=int(doc.get("guild_id") or 0),
            enabled=bool(doc.get("enabled", False)),
            active_profile_id=str(doc.get("active_profile_id") or ""),
            schema_version=int(doc.get("schema_version") or C.CHATBOT_SCHEMA_VERSION),
            updated_at=float(doc.get("updated_at") or 0.0),
        )


@dataclass
class ChatbotProfile:
    """Representação estável de um profile persistido."""

    guild_id: int
    profile_id: str
    name: str
    revision: str = ""
    avatar_url: str = ""
    system_prompt: str = ""
    temperature: float = C.DEFAULT_TEMPERATURE
    history_size: int = C.DEFAULT_HISTORY_SIZE
    active: bool = False
    tts_chance: float = 0.0
    profile_kind: str = C.PROFILE_KIND_NORMAL
    source_user_id: int = 0
    source_channel_id: int = 0
    dynamic_identity: bool = False
    fallback_name: str = ""
    fallback_avatar_url: str = ""
    persona_sample_count: int = 0
    persona_generated_at: float = 0.0
    created_by: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_doc(cls, doc: dict) -> "ChatbotProfile":
        temperature = doc.get("temperature")
        history_size = doc.get("history_size")
        profile_id = str(doc.get("profile_id") or "")
        revision = str(doc.get("revision") or f"legacy:{profile_id}")
        return cls(
            guild_id=int(doc.get("guild_id") or 0),
            profile_id=profile_id,
            name=str(doc.get("name") or ""),
            revision=revision,
            avatar_url=str(doc.get("avatar_url") or ""),
            system_prompt=str(doc.get("system_prompt") or ""),
            temperature=float(C.DEFAULT_TEMPERATURE if temperature is None else temperature),
            history_size=int(C.DEFAULT_HISTORY_SIZE if history_size is None else history_size),
            active=bool(doc.get("active", False)),
            tts_chance=float(doc.get("tts_chance") or 0.0),
            profile_kind=str(doc.get("profile_kind") or C.PROFILE_KIND_NORMAL),
            source_user_id=int(doc.get("source_user_id") or 0),
            source_channel_id=int(doc.get("source_channel_id") or 0),
            dynamic_identity=bool(doc.get("dynamic_identity", False)),
            fallback_name=str(doc.get("fallback_name") or ""),
            fallback_avatar_url=str(doc.get("fallback_avatar_url") or ""),
            persona_sample_count=int(doc.get("persona_sample_count") or 0),
            persona_generated_at=float(doc.get("persona_generated_at") or 0.0),
            created_by=int(doc.get("created_by") or 0),
            created_at=float(doc.get("created_at") or time.time()),
            updated_at=float(doc.get("updated_at") or time.time()),
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_PROFILE,
            "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "guild_id": self.guild_id,
            "profile_id": self.profile_id,
            "revision": self.revision or f"legacy:{self.profile_id}",
            "name": self.name,
            "avatar_url": self.avatar_url,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "history_size": self.history_size,
            "active": self.active,
            "tts_chance": self.tts_chance,
            "profile_kind": self.profile_kind,
            "source_user_id": self.source_user_id,
            "source_channel_id": self.source_channel_id,
            "dynamic_identity": self.dynamic_identity,
            "fallback_name": self.fallback_name,
            "fallback_avatar_url": self.fallback_avatar_url,
            "persona_sample_count": self.persona_sample_count,
            "persona_generated_at": self.persona_generated_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _slugify(name: str) -> str:
    stripped = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return stripped[:40] if stripped else "profile"


def _new_profile_id(name: str) -> str:
    return f"{_slugify(name)}-{secrets.token_hex(3)}"


def _new_revision() -> str:
    return secrets.token_hex(8)


class ProfileStore:
    """Persistência com fonte de verdade atômica e quota serializada."""

    _MAX_LOCAL_LOCKS = 256

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._guild_locks: "OrderedDict[int, asyncio.Lock]" = OrderedDict()
        self._config_cache: LRUCacheTTL[int, GuildChatbotConfig] = LRUCacheTTL(
            max_entries=C.PROFILE_CACHE_MAX_ENTRIES,
            ttl_seconds=C.PROFILE_CACHE_TTL_SECONDS,
        )
        self._profile_docs_cache: LRUCacheTTL[int, tuple[dict, ...]] = LRUCacheTTL(
            max_entries=C.PROFILE_CACHE_MAX_ENTRIES,
            ttl_seconds=C.PROFILE_CACHE_TTL_SECONDS,
        )

    def _invalidate_guild(self, guild_id: int) -> None:
        gid = int(guild_id)
        self._config_cache.pop(gid)
        self._profile_docs_cache.pop(gid)

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        gid = int(guild_id)
        lock = self._guild_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[gid] = lock
        else:
            self._guild_locks.move_to_end(gid)
        if len(self._guild_locks) > self._MAX_LOCAL_LOCKS:
            for key, candidate in list(self._guild_locks.items()):
                if key != gid and not candidate.locked():
                    self._guild_locks.pop(key, None)
                    break
        return lock

    async def get_guild_config(self, guild_id: int) -> GuildChatbotConfig:
        gid = int(guild_id)
        cached = self._config_cache.get(gid)
        if cached is not None:
            return cached
        query = {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid}
        doc = await self._coll.find_one(query)
        if doc:
            config = GuildChatbotConfig.from_doc(doc)
            self._config_cache.set(gid, config)
            return config
        legacy = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": gid,
            "active": True,
        })
        now = time.time()
        initial = {
            "type": C.DOC_TYPE_GUILD_CONFIG,
            "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "guild_id": gid,
            "enabled": bool(legacy),
            "active_profile_id": str((legacy or {}).get("profile_id") or ""),
            "created_at": now,
            "updated_at": now,
        }
        await self._coll.update_one(query, {"$setOnInsert": initial}, upsert=True)
        doc = await self._coll.find_one(query)
        config = GuildChatbotConfig.from_doc(doc or initial)
        self._config_cache.set(gid, config)
        return config

    async def is_enabled(self, guild_id: int) -> bool:
        return (await self.get_guild_config(guild_id)).enabled

    async def _collect_profiles(self, guild_id: int) -> list[dict]:
        gid = int(guild_id)
        cached = self._profile_docs_cache.get(gid)
        if cached is not None:
            return [dict(doc) for doc in cached]
        cursor = self._coll.find({"type": C.DOC_TYPE_PROFILE, "guild_id": gid})
        out: list[dict] = []
        async for doc in cursor:
            out.append(doc)
        self._profile_docs_cache.set(gid, tuple(dict(doc) for doc in out))
        return out

    async def list_profiles(self, guild_id: int) -> list[ChatbotProfile]:
        gid = int(guild_id)
        config, docs = await asyncio.gather(
            self.get_guild_config(gid), self._collect_profiles(gid)
        )
        active_id = config.active_profile_id if config.enabled else ""
        profiles = [
            replace(ChatbotProfile.from_doc(doc), active=(str(doc.get("profile_id")) == active_id))
            for doc in docs
        ]
        profiles.sort(key=lambda profile: profile.created_at)
        return profiles

    async def get_profile(self, guild_id: int, profile_id: str) -> Optional[ChatbotProfile]:
        gid, pid = int(guild_id), str(profile_id)
        cached = self._profile_docs_cache.get(gid)
        if cached is not None:
            doc = next((item for item in cached if str(item.get("profile_id")) == pid), None)
            return ChatbotProfile.from_doc(doc) if doc else None
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": gid,
            "profile_id": pid,
        })
        return ChatbotProfile.from_doc(doc) if doc else None

    async def get_active_profile(self, guild_id: int) -> Optional[ChatbotProfile]:
        config = await self.get_guild_config(guild_id)
        if not config.enabled or not config.active_profile_id:
            return None
        profile = await self.get_profile(guild_id, config.active_profile_id)
        return replace(profile, active=True) if profile else None

    async def count_profiles(self, guild_id: int) -> int:
        return await self._coll.count_documents({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
        })

    async def get_user_style_profile(
        self, guild_id: int, source_user_id: int
    ) -> Optional[ChatbotProfile]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_kind": C.PROFILE_KIND_USER_STYLE,
            "source_user_id": int(source_user_id),
        })
        return ChatbotProfile.from_doc(doc) if doc else None

    async def create_profile(
        self,
        *,
        guild_id: int,
        name: str,
        created_by: int,
        system_prompt: str = "",
        avatar_url: str = "",
        temperature: float = C.DEFAULT_TEMPERATURE,
        history_size: int = C.DEFAULT_HISTORY_SIZE,
    ) -> ChatbotProfile:
        gid = int(guild_id)
        async with self._lock_for(gid):
            if await self.count_profiles(gid) >= C.MAX_PROFILES_PER_GUILD:
                raise ProfileLimitReached("limite de profiles atingido")
            now = time.time()
            profile = ChatbotProfile(
                guild_id=gid,
                profile_id=_new_profile_id(name),
                revision=_new_revision(),
                name=name.strip()[:C.MAX_NAME_LENGTH],
                avatar_url=avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
                system_prompt=system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH],
                temperature=max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, float(temperature))),
                history_size=max(1, min(C.MAX_HISTORY_SIZE, int(history_size))),
                created_by=int(created_by),
                created_at=now,
                updated_at=now,
            )
            await self._coll.insert_one(profile.to_doc())
            self._invalidate_guild(gid)
            return profile

    async def update_profile(
        self,
        guild_id: int,
        profile_id: str,
        *,
        name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        history_size: Optional[int] = None,
    ) -> Optional[ChatbotProfile]:
        current_doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
        })
        if current_doc is None:
            return None
        updates: dict[str, Any] = {
            "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "updated_at": time.time(),
        }
        if name is not None:
            updates["name"] = name.strip()[:C.MAX_NAME_LENGTH]
        if avatar_url is not None:
            updates["avatar_url"] = avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH]
        if system_prompt is not None:
            normalized_prompt = system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH]
            updates["system_prompt"] = normalized_prompt
            if normalized_prompt != str(current_doc.get("system_prompt") or ""):
                # Uma personalidade nova não herda conversas construídas sob
                # instruções antigas. Os documentos ficam para rollback, mas a
                # nova revisão usa outra chave de memória.
                updates["revision"] = _new_revision()
        if temperature is not None:
            updates["temperature"] = max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, float(temperature)))
        if history_size is not None:
            updates["history_size"] = max(1, min(C.MAX_HISTORY_SIZE, int(history_size)))
        result = await self._coll.find_one_and_update(
            {"_id": current_doc["_id"], "type": C.DOC_TYPE_PROFILE},
            {"$set": updates}, return_document=True,
        )
        self._invalidate_guild(guild_id)
        return ChatbotProfile.from_doc(result) if result else None

    async def delete_profile(self, guild_id: int, profile_id: str) -> bool:
        gid, pid = int(guild_id), str(profile_id)
        async with self._lock_for(gid):
            result = await self._coll.delete_one({
                "type": C.DOC_TYPE_PROFILE, "guild_id": gid, "profile_id": pid,
            })
            if result.deleted_count:
                await self._coll.update_one(
                    {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid, "active_profile_id": pid},
                    {"$set": {"enabled": False, "active_profile_id": "", "updated_at": time.time()}},
                )
                self._invalidate_guild(gid)
            return result.deleted_count > 0

    async def upsert_user_style_profile(
        self,
        *,
        guild_id: int,
        source_user_id: int,
        source_channel_id: int,
        created_by: int,
        fallback_name: str,
        fallback_avatar_url: str,
        system_prompt: str,
        sample_count: int,
        activate: bool = False,
    ) -> tuple[ChatbotProfile, bool]:
        now = time.time()
        gid, uid = int(guild_id), int(source_user_id)
        profile_id = f"persona-{uid}"
        async with self._lock_for(gid):
            existing = await self.get_user_style_profile(gid, uid)
            created = existing is None
            if created and await self.count_profiles(gid) >= C.MAX_PROFILES_PER_GUILD:
                raise ProfileLimitReached("limite de profiles atingido")
            updates: dict[str, Any] = {
                "type": C.DOC_TYPE_PROFILE,
                "schema_version": C.CHATBOT_SCHEMA_VERSION,
                "guild_id": gid,
                "profile_id": profile_id,
                "revision": _new_revision(),
                "name": fallback_name.strip()[:C.MAX_NAME_LENGTH] or "Persona",
                "avatar_url": fallback_avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
                "system_prompt": system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH],
                "temperature": C.DEFAULT_TEMPERATURE,
                "history_size": C.DEFAULT_HISTORY_SIZE,
                "profile_kind": C.PROFILE_KIND_USER_STYLE,
                "source_user_id": uid,
                "source_channel_id": int(source_channel_id),
                "dynamic_identity": True,
                "fallback_name": fallback_name.strip()[:C.MAX_NAME_LENGTH] or "Persona",
                "fallback_avatar_url": fallback_avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
                "persona_sample_count": int(sample_count),
                "persona_generated_at": now,
                "created_by": int(created_by if created else existing.created_by),
                "created_at": now if created else float(existing.created_at),
                "active": False if created else bool(existing.active),
                "tts_chance": 0.0 if created else float(existing.tts_chance),
                "updated_at": now,
            }
            await self._coll.update_one(
                {"type": C.DOC_TYPE_PROFILE, "guild_id": gid, "profile_id": profile_id},
                {"$set": updates}, upsert=True,
            )
            self._invalidate_guild(gid)
        if activate:
            activated = await self.set_active_profile(gid, profile_id)
            if activated:
                return activated, created
        profile = await self.get_profile(gid, profile_id)
        return (profile or ChatbotProfile.from_doc(updates)), created

    async def delete_user_style_profile(self, guild_id: int, source_user_id: int) -> bool:
        profile = await self.get_user_style_profile(guild_id, source_user_id)
        return False if profile is None else await self.delete_profile(guild_id, profile.profile_id)

    async def set_active_profile(self, guild_id: int, profile_id: str) -> Optional[ChatbotProfile]:
        gid, pid = int(guild_id), str(profile_id)
        async with self._lock_for(gid):
            # Mutations must not make authorization/state decisions from the
            # short read cache: another bot process may have deleted or changed
            # the profile during the cache TTL.
            now = time.time()
            target_doc = await self._coll.find_one_and_update(
                {"type": C.DOC_TYPE_PROFILE, "guild_id": gid, "profile_id": pid},
                {"$set": {"active": True, "updated_at": now}},
                return_document=True,
            )
            if target_doc is None:
                return None
            # Keep the atomic config document as the last write. Readers either
            # observe the previous valid selection or the complete new one.
            await self._coll.update_many(
                {
                    "type": C.DOC_TYPE_PROFILE,
                    "guild_id": gid,
                    "profile_id": {"$ne": pid},
                    "active": True,
                },
                {"$set": {"active": False, "updated_at": now}},
            )
            await self._coll.update_one(
                {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid},
                {
                    "$set": {
                        "schema_version": C.CHATBOT_SCHEMA_VERSION,
                        "enabled": True,
                        "active_profile_id": pid,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "type": C.DOC_TYPE_GUILD_CONFIG,
                        "guild_id": gid,
                        "created_at": now,
                    },
                },
                upsert=True,
            )
            self._invalidate_guild(gid)
            return replace(ChatbotProfile.from_doc(target_doc), active=True)

    async def deactivate_all(self, guild_id: int) -> int:
        gid = int(guild_id)
        async with self._lock_for(gid):
            previous = await self.get_guild_config(gid)
            now = time.time()
            await self._coll.update_one(
                {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid},
                {
                    "$set": {
                        "schema_version": C.CHATBOT_SCHEMA_VERSION,
                        "enabled": False,
                        "active_profile_id": "",
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "type": C.DOC_TYPE_GUILD_CONFIG,
                        "guild_id": gid,
                        "created_at": now,
                    },
                }, upsert=True,
            )
            result = await self._coll.update_many(
                {"type": C.DOC_TYPE_PROFILE, "guild_id": gid, "active": True},
                {"$set": {"active": False, "updated_at": now}},
            )
            self._invalidate_guild(gid)
            return max(int(result.modified_count), int(previous.enabled))
