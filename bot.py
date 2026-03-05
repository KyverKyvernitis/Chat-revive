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

# Voice-trigger envs (new)
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "").lower().strip()
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
    # host must be 0.0.0.0 on Render
    app.run(host="0.0.0.0", port=PORT)


# ---- DISCORD BOT ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

cooldown_active = False


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

    # Only trigger once while cooldown is active (no reset/extend)
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

    # ---------------------------------------------
    # 2) Voice-channel chat trigger: disconnect user
    # ---------------------------------------------
    # Only if both env vars are set
    if TRIGGER_WORD and TARGET_USER_ID:
        # "Chat do canal de voz": message.channel é um VoiceChannel
        if isinstance(message.channel, discord.VoiceChannel):
            # Autor precisa estar conectado nesse mesmo canal de voz
            author_voice = getattr(message.author, "voice", None)
            if (
                author_voice
                and author_voice.channel
                and author_voice.channel.id == message.channel.id
            ):
                if TRIGGER_WORD in (message.content or "").lower():
                    guild = message.guild

                    target = guild.get_member(TARGET_USER_ID)
                    if target is None:
                        # fallback via HTTP
                        try:
                            target = await guild.fetch_member(TARGET_USER_ID)
                        except discord.NotFound:
                            target = None
                        except discord.HTTPException:
                            target = None

                    # Desconecta se o alvo estiver em call
                    if target and target.voice and target.voice.channel:
                        try:
                            await target.move_to(None, reason="Voice trigger word detected")
                        except discord.Forbidden:
                            print("Missing permissions to move members (Move Members).")
                        except discord.HTTPException as e:
                            print(f"Failed to disconnect target user: {e}")

    # Keep commands working
    await bot.process_commands(message)


def main():
    # Start Flask in a separate thread so discord.py can run normally
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)


if __name__ == "__main__":
    if not TOKEN or TARGET_ROLE_ID == 0:
        raise RuntimeError("Missing env vars: DISCORD_TOKEN and/or ROLE_ID")
    main()
