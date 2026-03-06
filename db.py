from typing import Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient


class SettingsDB:
    def __init__(self, uri: str, db_name: str, coll_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.coll = self.db[coll_name]

        self.guild_cache: Dict[int, Dict[str, Any]] = {}
        self.user_cache: Dict[tuple[int, int], Dict[str, Any]] = {}

    async def init(self):
        try:
            await self.coll.create_index("type")
            await self.coll.create_index([("guild_id", 1), ("type", 1)], unique=False)
            await self.coll.create_index([("guild_id", 1), ("user_id", 1), ("type", 1)], unique=True)
        except Exception:
            pass

        await self.load_cache()

    async def load_cache(self):
        self.guild_cache.clear()
        self.user_cache.clear()

        cursor = self.coll.find({}, {"_id": 0})
        async for doc in cursor:
            doc_type = doc.get("type")
            gid = int(doc.get("guild_id", 0))

            if doc_type == "guild" and gid:
                self.guild_cache[gid] = doc
            elif doc_type == "user" and gid and doc.get("user_id") is not None:
                uid = int(doc["user_id"])
                self.user_cache[(gid, uid)] = doc

    def anti_mzk_enabled(self, guild_id: int) -> bool:
        g = self.guild_cache.get(guild_id, {})
        return bool(g.get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        doc = self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
        doc["type"] = "guild"
        doc["guild_id"] = guild_id
        doc["anti_mzk_enabled"] = bool(value)
        self.guild_cache[guild_id] = doc

        await self.coll.update_one(
            {"type": "guild", "guild_id": guild_id},
            {"$set": doc},
            upsert=True,
        )

    def block_voice_bot_enabled(self, guild_id: int) -> bool:
        g = self.guild_cache.get(guild_id, {})
        return bool(g.get("block_voice_bot_enabled", True))

    async def set_block_voice_bot_enabled(self, guild_id: int, value: bool):
        doc = self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
        doc["type"] = "guild"
        doc["guild_id"] = guild_id
        doc["block_voice_bot_enabled"] = bool(value)
        self.guild_cache[guild_id] = doc

        await self.coll.update_one(
            {"type": "guild", "guild_id": guild_id},
            {"$set": doc},
            upsert=True,
        )

    def get_guild_tts_defaults(self, guild_id: int) -> Dict[str, str]:
        g = self.guild_cache.get(guild_id, {})
        tts = g.get("tts_defaults", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
        }

    async def set_guild_tts_defaults(
        self,
        guild_id: int,
        *,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ):
        doc = self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
        doc["type"] = "guild"
        doc["guild_id"] = guild_id

        tts = doc.get("tts_defaults", {}) or {}
        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch

        doc["tts_defaults"] = tts
        self.guild_cache[guild_id] = doc

        await self.coll.update_one(
            {"type": "guild", "guild_id": guild_id},
            {"$set": doc},
            upsert=True,
        )

    def get_user_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        u = self.user_cache.get((guild_id, user_id), {})
        tts = u.get("tts", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
        }

    async def set_user_tts(
        self,
        guild_id: int,
        user_id: int,
        *,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
    ):
        key = (guild_id, user_id)
        doc = self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id})
        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id

        tts = doc.get("tts", {}) or {}
        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch

        doc["tts"] = tts
        self.user_cache[key] = doc

        await self.coll.update_one(
            {"type": "user", "guild_id": guild_id, "user_id": user_id},
            {"$set": doc},
            upsert=True,
        )

    def resolve_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        user = self.get_user_tts(guild_id, user_id)
        guild = self.get_guild_tts_defaults(guild_id)

        def pick(k: str, fallback: str) -> str:
            return (user.get(k) or "").strip() or (guild.get(k) or "").strip() or fallback

        engine = pick("engine", "gtts").lower()
        if engine not in ("edge", "gtts"):
            engine = "gtts"

        return {
            "engine": engine,
            "voice": pick("voice", "pt-BR-FranciscaNeural"),
            "rate": pick("rate", "+0%"),
            "pitch": pick("pitch", "+0Hz"),
        }
