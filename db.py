import os
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "discordbot")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
cooldowns = db["role_cooldowns"]

async def ensure_indexes():
    await cooldowns.create_index([("guild_id", 1), ("role_id", 1)], unique=True)
    await cooldowns.create_index("ends_at") 
