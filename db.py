from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorClient


class SettingsDB:
    def __init__(self, uri: str, db_name: str, coll_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.coll = self.db[coll_name]
        self.guild_cache: Dict[int, Dict[str, Any]] = {}
        self.user_cache: Dict[tuple[int, int], Dict[str, Any]] = {}

    async def init(self):
        await self._ensure_indexes()
        await self.load_cache()

    async def _ensure_indexes(self):
        try:
            indexes = await self.coll.index_information()
            idx = indexes.get("guild_id_1_type_1")
            if idx:
                try:
                    await self.coll.drop_index("guild_id_1_type_1")
                    print("[db] Índice antigo guild_id_1_type_1 removido.")
                except Exception:
                    pass
            await self.coll.create_index("type")
            await self.coll.create_index([("guild_id", 1), ("type", 1)], unique=True, name="guild_id_1_type_1")
            await self.coll.create_index([("guild_id", 1), ("user_id", 1), ("type", 1)], unique=True, name="guild_id_1_user_id_1_type_1")
        except Exception as e:
            print(f"[db] Erro ao garantir índices: {e}")

    async def load_cache(self):
        self.guild_cache.clear()
        self.user_cache.clear()
        cursor = self.coll.find({}, {"_id": 0})
        async for doc in cursor:
            doc_type = doc.get("type")
            gid = int(doc.get("guild_id", 0) or 0)
            if doc_type == "guild" and gid:
                self.guild_cache[gid] = doc
            elif doc_type == "user" and gid and doc.get("user_id") is not None:
                uid = int(doc["user_id"])
                self.user_cache[(gid, uid)] = doc

    def _get_guild_doc(self, guild_id: int) -> Dict[str, Any]:
        return self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})

    async def _save_guild_doc(self, guild_id: int, doc: Dict[str, Any]):
        doc["type"] = "guild"
        doc["guild_id"] = guild_id
        self.guild_cache[guild_id] = doc
        await self.coll.update_one({"type": "guild", "guild_id": guild_id}, {"$set": doc}, upsert=True)

    def get_guild_tts_defaults(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        tts = g.get("tts_defaults", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
            "block_voice_bot": bool(g.get("block_voice_bot_enabled", True)),
            "only_target_user": bool(g.get("only_target_user_enabled", False)),
        }

    async def set_guild_tts_defaults(self, guild_id: int, *, engine: Optional[str] = None, voice: Optional[str] = None, language: Optional[str] = None, rate: Optional[str] = None, pitch: Optional[str] = None, block_voice_bot: Optional[bool] = None, only_target_user: Optional[bool] = None):
        doc = self._get_guild_doc(guild_id)
        tts = doc.get("tts_defaults", {}) or {}
        if engine is not None: tts["engine"] = engine
        if voice is not None: tts["voice"] = voice
        if language is not None: tts["language"] = language
        if rate is not None: tts["rate"] = rate
        if pitch is not None: tts["pitch"] = pitch
        if block_voice_bot is not None: doc["block_voice_bot_enabled"] = bool(block_voice_bot)
        if only_target_user is not None: doc["only_target_user_enabled"] = bool(only_target_user)
        doc["tts_defaults"] = tts
        await self._save_guild_doc(guild_id, doc)

    def get_user_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        u = self.user_cache.get((guild_id, user_id), {})
        tts = u.get("tts", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
        }

    async def set_user_tts(self, guild_id: int, user_id: int, *, engine: Optional[str] = None, voice: Optional[str] = None, language: Optional[str] = None, rate: Optional[str] = None, pitch: Optional[str] = None):
        key = (guild_id, user_id)
        doc = self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id})
        tts = doc.get("tts", {}) or {}
        if engine is not None: tts["engine"] = engine
        if voice is not None: tts["voice"] = voice
        if language is not None: tts["language"] = language
        if rate is not None: tts["rate"] = rate
        if pitch is not None: tts["pitch"] = pitch
        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id
        doc["tts"] = tts
        self.user_cache[key] = doc
        await self.coll.update_one({"type": "user", "guild_id": guild_id, "user_id": user_id}, {"$set": doc}, upsert=True)

    def resolve_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        user = self.get_user_tts(guild_id, user_id)
        guild = self.get_guild_tts_defaults(guild_id)
        def pick(key: str, fallback: str) -> str:
            return (user.get(key) or "").strip() or (guild.get(key) or "").strip() or fallback
        engine = pick("engine", "gtts").lower()
        if engine not in ("edge", "gtts"):
            engine = "gtts"
        return {
            "engine": engine,
            "voice": pick("voice", "pt-BR-FranciscaNeural"),
            "language": pick("language", "pt-br"),
            "rate": pick("rate", "+0%"),
            "pitch": pick("pitch", "+0Hz"),
        }

    @staticmethod
    def utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
