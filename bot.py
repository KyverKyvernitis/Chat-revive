import os
import asyncio
import threading
from flask import Flask
import discord
from discord.ext import commands
from discord import app_commands

# ---- ENV ----
TOKEN = os.getenv("DISCORD_TOKEN")

TARGET_ROLE_ID = int(os.getenv("ROLE_ID", "0"))
DISABLE_TIME = int(os.getenv("DISABLE_TIME", "14400"))

# Voice triggers (anti-mzk)
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "").lower().strip()               # desconectar
MUTE_TOGGLE_WORD = os.getenv("MUTE_TOGGLE_WORD", "rola").lower().strip()   # mute/desmute
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))

# Render sets PORT for web services
PORT = int(os.getenv("PORT", "10000"))

# Embed colors (hex)
ON_COLOR = discord.Color(0x57F287)
OFF_COLOR = discord.Color(0xED4245)

# Guilds para sync rápido (IDs que você passou)
GUILD_IDS = [
    1313883930637762560,
    1349910251117350923,
]

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
anti_mzk_enabled = True  # default ON


async def get_target_member(guild: discord.Guild, user_id: int):
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

    # Sync por guild (aparece rápido)
    for gid in GUILD_IDS:
        guild_obj = discord.Object(id=gid)
        try:
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} app commands to guild {gid}.")
        except Exception as e:
            print(f"Failed to sync commands to guild {gid}: {e}")


# -------------------------
# Slash command: /antimzk
# -------------------------
@bot.tree.command(name="antimzk", description="Ativa/desativa a censura anti-mzk (voz).")
@app_commands.checks.has_permissions(move_members=True)
async def antimzk(interaction: discord.Interaction):
    global anti_mzk_enabled
    anti_mzk_enabled = not anti_mzk_enabled

    if anti_mzk_enabled:
        embed = discord.Embed(
            description="✅ Censura anti-mzk ativada",
            color=ON_COLOR,
        )
    else:
        embed = discord.Embed(
            description="❌ Censura anti-mzk desativada",
            color=OFF_COLOR,
        )

    await interaction.response.send_message(embed=embed)


@antimzk.error
async def antimzk_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Você não tem permissão para usar esse comando (precisa de **Mover Membros**).",
            ephemeral=True,
        )
    else:
        try:
            await interaction.response.send_message("Ocorreu um erro ao executar o comando.", ephemeral=True)
        except Exception:
            pass
        print(f"Error in /antimzk: {error}")


@bot.event
async def on_message(message: discord.Message):
    global cooldown_active, anti_mzk_enabled

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
                await role.edit(mentionable=False, reason="Role mentioned; auto-disable mentions")
        except discord.Forbidden:
            print("Missing permissions to edit role (Manage Roles / role hierarchy).")
        except discord.HTTPException as e:
            print(f"Failed to edit role: {e}")

        await asyncio.sleep(DISABLE_TIME)

        role = message.guild.get_role(TARGET_ROLE_ID)
        if role:
            try:
                await role.edit(mentionable=True, reason="Cooldown finished; auto re-enable mentions")
            except Exception as e:
                print(f"Failed to re-enable role mentions: {e}")

        cooldown_active = False

    # -------------------------------------------------------
    # 2) Voice-channel chat triggers (disconnect / mute toggle)
    # -------------------------------------------------------
    if anti_mzk_enabled and TARGET_USER_ID and (TRIGGER_WORD or MUTE_TOGGLE_WORD):
        # Mensagens no próprio VoiceChannel (chat do canal de voz)
        if isinstance(message.channel, discord.VoiceChannel):
            author_voice = getattr(message.author, "voice", None)
            if author_voice and author_voice.channel and author_voice.channel.id == message.channel.id:
                content = (message.content or "").lower()

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
                            currently_muted = bool(target.voice.mute)  # server mute
                            await target.edit(mute=not currently_muted, reason="Toggle mute trigger word detected")
                        except discord.Forbidden:
                            print("Missing permissions to mute members (Mute Members).")
                        except discord.HTTPException as e:
                            print(f"Failed to toggle mute: {e}")

    await bot.process_commands(message)


def main():
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)


if __name__ == "__main__":
    if not TOKEN or TARGET_ROLE_ID == 0:
        raise RuntimeError("Missing env vars: DISCORD_TOKEN and/or ROLE_ID")
    main()
