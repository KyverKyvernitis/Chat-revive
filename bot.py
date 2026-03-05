import os
import asyncio
import threading
from flask import Flask
import discord
from discord.ext import commands

# ---- ENV ----
TOKEN = os.getenv("DISCORD_TOKEN")

TARGET_ROLE_ID = int(os.getenv("ROLE_ID", "0"))
DISABLE_TIME = int(os.getenv("DISABLE_TIME", "14400"))

# Voice triggers
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "").lower().strip()          # desconectar
MUTE_TOGGLE_WORD = os.getenv("MUTE_TOGGLE_WORD", "rola").lower().strip()  # mute/desmute
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))

# Render sets PORT for web services
PORT = int(os.getenv("PORT", "10000"))

# ---- WEB SERVER ----
app = Flask(__name__)


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return "healthy", 200


def run_web():
    app.run(host="0.0.0.0", port=PORT)


# ---- DISCORD BOT ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

cooldown_active = False


async def get_target_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Try cache first, then fetch from API."""
    target = guild.get_member(user_id)
    if target is not None:
        return target
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    global cooldown_active

    if message.author.bot or not message.guild:
        return

    # -------------------------
    # 1) Role mention cooldown
    # -------------------------
    role = message.guild.get_role(TARGET_ROLE_ID)

    if role and role in message.role_mentions and not cooldown_active:
        cooldown_active = True
        try:
            if role.mentionable:
                await role.edit(
                    mentionable=False,
                    reason="Role mentioned; auto-disable mentions",
                )
        except discord.Forbidden:
            print("Missing permissions to edit role (Manage Roles / role hierarchy).")
        except discord.HTTPException as e:
            print(f"Failed to edit role: {e}")

        await asyncio.sleep(DISABLE_TIME)

        role = message.guild.get_role(TARGET_ROLE_ID)
        if role:
            try:
                await role.edit(
                    mentionable=True,
                    reason="Cooldown finished; auto re-enable mentions",
                )
            except Exception as e:
                print(f"Failed to re-enable role mentions: {e}")

        cooldown_active = False

    # -------------------------------------------------------
    # 2) Voice-channel chat triggers (disconnect / mute toggle)
    # -------------------------------------------------------
    # Só faz sentido se tiver alvo e alguma palavra configurada
    if TARGET_USER_ID and (TRIGGER_WORD or MUTE_TOGGLE_WORD):
        # "Chat do canal de voz": mensagens enviadas no próprio VoiceChannel
        if isinstance(message.channel, discord.VoiceChannel):
            # Autor precisa estar conectado nesse MESMO canal
            author_voice = getattr(message.author, "voice", None)
            if (
                author_voice
                and author_voice.channel
                and author_voice.channel.id == message.channel.id
            ):
                content = (message.content or "").lower()

                # Pega alvo
                target = await get_target_member(message.guild, TARGET_USER_ID)

                # A) Desconectar
                if TRIGGER_WORD and TRIGGER_WORD in content:
                    if target and target.voice and target.voice.channel:
                        try:
                            await target.move_to(None, reason="Trigger word detected (disconnect)")
                        except discord.Forbidden:
                            print("Missing permissions to move members (Move Members).")
                        except discord.HTTPException as e:
                            print(f"Failed to disconnect target user: {e}")

                # B) Mute/desmute (toggle)
                if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
                    if target and target.voice and target.voice.channel:
                        try:
                            # voice.mute = server mute atual (True/False)
                            currently_muted = bool(target.voice.mute)
                            await target.edit(mute=not currently_muted, reason="Toggle mute trigger word detected")
                        except discord.Forbidden:
                            print("Missing permissions to mute members (Mute Members).")
                        except discord.HTTPException as e:
                            print(f"Failed to toggle mute: {e}")

    # Keep commands working
    await bot.process_commands(message)


def main():
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)


if __name__ == "__main__":
    if not TOKEN or TARGET_ROLE_ID == 0:
        raise RuntimeError("Missing env vars: DISCORD_TOKEN and/or ROLE_ID")
    main()
