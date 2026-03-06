from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands

from config import DISABLE_TIME, TARGET_ROLE_ID


class RoleCooldownCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await self.restore_cooldowns()

    async def cog_unload(self):
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    async def restore_cooldowns(self):
        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return

        for guild in self.bot.guilds:
            data = db.get_role_cooldown(guild.id)
            if not data.get("active"):
                continue

            ends_at_raw = data.get("ends_at", "")
            role_id = int(data.get("role_id", 0) or 0)

            if not ends_at_raw or not role_id:
                await db.clear_role_cooldown(guild.id)
                continue

            try:
                ends_at = datetime.fromisoformat(ends_at_raw)
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=timezone.utc)
            except Exception:
                await db.clear_role_cooldown(guild.id)
                continue

            remaining = (ends_at - datetime.now(timezone.utc)).total_seconds()

            if remaining <= 0:
                await self.finish_cooldown(guild.id)
                continue

            if guild.id not in self.tasks or self.tasks[guild.id].done():
                self.tasks[guild.id] = asyncio.create_task(
                    self._cooldown_waiter(guild.id, remaining)
                )

    async def start_cooldown(self, guild: discord.Guild, role: discord.Role):
        db = self.bot.settings_db

        if guild.id in self.tasks and not self.tasks[guild.id].done():
            return

        original_mentionable = bool(role.mentionable)

        if role.mentionable:
            await role.edit(
                mentionable=False,
                reason="Cargo mencionado; auto-desativando menções",
            )

        ends_at = datetime.now(timezone.utc).timestamp() + DISABLE_TIME
        ends_iso = datetime.fromtimestamp(ends_at, tz=timezone.utc).isoformat()

        await db.set_role_cooldown(
            guild.id,
            active=True,
            ends_at=ends_iso,
            role_id=role.id,
            role_was_mentionable=original_mentionable,
        )

        self.tasks[guild.id] = asyncio.create_task(
            self._cooldown_waiter(guild.id, DISABLE_TIME)
        )

    async def finish_cooldown(self, guild_id: int):
        db = self.bot.settings_db
        data = db.get_role_cooldown(guild_id)

        role_id = int(data.get("role_id", 0) or 0)
        role_was_mentionable = data.get("role_was_mentionable", None)

        guild = self.bot.get_guild(guild_id)
        if guild and role_id:
            role = guild.get_role(role_id)
            if role and role_was_mentionable is True:
                try:
                    await role.edit(
                        mentionable=True,
                        reason="Cooldown acabou; auto-reativando menções",
                    )
                except Exception:
                    pass

        await db.clear_role_cooldown(guild_id)

        task = self.tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _cooldown_waiter(self, guild_id: int, seconds: float):
        try:
            await asyncio.sleep(max(0, seconds))
            await self.finish_cooldown(guild_id)
        except asyncio.CancelledError:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if TARGET_ROLE_ID == 0:
            return

        role = message.guild.get_role(TARGET_ROLE_ID)
        if not role:
            return

        if role not in message.role_mentions:
            return

        db = self.bot.settings_db
        current = db.get_role_cooldown(message.guild.id)
        if current.get("active"):
            return

        try:
            await self.start_cooldown(message.guild, role)
        except Exception as e:
            print(f"[role_cooldown] Erro ao iniciar cooldown na guild {message.guild.id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleCooldownCog(bot))
