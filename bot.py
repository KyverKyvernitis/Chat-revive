from __future__ import annotations

import asyncio
import threading

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import create_app


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.settings_db: SettingsDB | None = None

    async def setup_hook(self):
        if not config.MONGODB_URI:
            raise RuntimeError("Faltou MONGODB_URI")

        self.settings_db = SettingsDB(
            config.MONGODB_URI,
            config.MONGODB_DB,
            config.MONGODB_COLLECTION,
        )
        await self.settings_db.init()

        await self.load_extension("cogs.role_cooldown")
        await self.load_extension("cogs.antimzk")
        await self.load_extension("cogs.tts_voice")

        # Sync por guild quando GUILD_IDS estiver configurado
        if config.GUILD_IDS:
            for gid in config.GUILD_IDS:
                guild_obj = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                print(f"[SYNC] Slash commands sincronizados na guild {gid}")
        else:
            # Sync global se não houver guilds configuradas
            await self.tree.sync()
            print("[SYNC] Slash commands sincronizados globalmente")


bot = MyBot()


def run_web():
    app = create_app()
    app.run(host="0.0.0.0", port=config.PORT)


@bot.event
async def on_ready():
    print(f"Logado como {bot.user} (id: {bot.user.id})")
    print(f"Em {len(bot.guilds)} servidor(es)")


async def main():
    threading.Thread(target=run_web, daemon=True).start()
    await bot.start(config.TOKEN)


if __name__ == "__main__":
    if not config.TOKEN:
        raise RuntimeError("Faltou DISCORD_TOKEN")

    asyncio.run(main())
