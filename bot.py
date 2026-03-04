import os
import time
import asyncio
import discord
from db import cooldowns, ensure_indexes

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN")
ROLE_ID = int(os.getenv("ROLE_ID", "123456789012345678"))
COOLDOWN_SECONDS = 4 * 60 * 60

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

client = discord.Client(intents=intents)
scheduled_tasks: dict[tuple[int, int], asyncio.Task] = {}

async def get_active_cooldown(guild_id: int, role_id: int, now: int):
    return await cooldowns.find_one(
        {"guild_id": guild_id, "role_id": role_id, "ends_at": {"$gt": now}}
    )

async def set_cooldown(guild_id: int, role_id: int, ends_at: int):
    await cooldowns.update_one(
        {"guild_id": guild_id, "role_id": role_id},
        {"$set": {"ends_at": ends_at}},
        upsert=True,
    )

async def clear_cooldown(guild_id: int, role_id: int):
    await cooldowns.delete_one({"guild_id": guild_id, "role_id": role_id})

async def schedule_reenable(guild_id: int, role_id: int):
    key = (guild_id, role_id)
    t = scheduled_tasks.get(key)
    if t and not t.done():
        return

    async def runner():
        try:
            while True:
                doc = await cooldowns.find_one({"guild_id": guild_id, "role_id": role_id})
                if not doc:
                    return
                ends_at = int(doc["ends_at"])
                now = int(time.time())
                remaining = ends_at - now
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 3600))
                    continue

                guild = client.get_guild(guild_id)
                if not guild:
                    await asyncio.sleep(10)
                    continue

                role = guild.get_role(role_id)
                if not role:
                    await clear_cooldown(guild_id, role_id)
                    return

                try:
                    await role.edit(mentionable=True, reason="Role mention cooldown ended")
                finally:
                    await clear_cooldown(guild_id, role_id)
                return
        except asyncio.CancelledError:
            return

    scheduled_tasks[key] = asyncio.create_task(runner())

@client.event
async def on_ready():
    await ensure_indexes()
    now = int(time.time())
    async for doc in cooldowns.find({"ends_at": {"$gt": now}}):
        guild_id = int(doc["guild_id"])
        role_id = int(doc["role_id"])

        guild = client.get_guild(guild_id)
        if guild:
            role = guild.get_role(role_id)
            if role and role.mentionable:
                try:
                    await role.edit(mentionable=False, reason="Restored active cooldown on restart")
                except Exception:
                    pass

        await schedule_reenable(guild_id, role_id)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild = message.guild
    role = guild.get_role(ROLE_ID)
    if not role:
        return

    if role not in message.role_mentions:
        return

    now = int(time.time())
    if await get_active_cooldown(guild.id, role.id, now):
        return

    ends_at = now + COOLDOWN_SECONDS
    await set_cooldown(guild.id, role.id, ends_at)

    if role.mentionable:
        await role.edit(mentionable=False, reason="Role mentioned - starting 4h cooldown")

    await schedule_reenable(guild.id, role.id)

async def start_discord():
    await client.start(TOKEN) 
