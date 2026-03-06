import discord
from discord.ext import commands
from discord import app_commands

from config import TRIGGER_WORD, MUTE_TOGGLE_WORD, TARGET_USER_ID, ON_COLOR, OFF_COLOR
from db import SettingsDB


class AntiMzkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db

    @app_commands.command(name="antimzk", description="Ativa/desativa a censura anti-mzk (voz).")
    @app_commands.checks.has_permissions(move_members=True)
    async def antimzk(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Use esse comando em um servidor.", ephemeral=True)
            return

        gid = interaction.guild.id
        new_value = not self.db.anti_mzk_enabled(gid)
        await self.db.set_anti_mzk_enabled(gid, new_value)

        embed = discord.Embed(
            description="✅ Censura anti-mzk ativada" if new_value else "❌ Censura anti-mzk desativada",
            color=discord.Color(ON_COLOR) if new_value else discord.Color(OFF_COLOR),
        )
        await interaction.response.send_message(embed=embed)

    @antimzk.error
    async def antimzk_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Você não tem permissão (precisa de **Mover Membros**).",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Você não tem permissão (precisa de **Mover Membros**).",
                    ephemeral=True,
                )
            return

        print(f"Erro no /antimzk: {repr(error)}")

        if interaction.response.is_done():
            await interaction.followup.send("Ocorreu um erro.", ephemeral=True)
        else:
            await interaction.response.send_message("Ocorreu um erro.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not self.db.anti_mzk_enabled(message.guild.id):
            return

        if not TARGET_USER_ID or (not TRIGGER_WORD and not MUTE_TOGGLE_WORD):
            return

        if not isinstance(message.channel, discord.VoiceChannel):
            return

        author_voice = getattr(message.author, "voice", None)
        if not author_voice or not author_voice.channel or author_voice.channel.id != message.channel.id:
            return

        content = (message.content or "").lower()

        target = message.guild.get_member(TARGET_USER_ID)
        if target is None:
            try:
                target = await message.guild.fetch_member(TARGET_USER_ID)
            except Exception:
                return

        if TRIGGER_WORD and TRIGGER_WORD in content:
            if target.voice and target.voice.channel:
                try:
                    await target.move_to(None, reason="anti-mzk disconnect")
                except Exception:
                    pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            if target.voice and target.voice.channel:
                try:
                    await target.edit(mute=not bool(target.voice.mute), reason="anti-mzk toggle mute")
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
