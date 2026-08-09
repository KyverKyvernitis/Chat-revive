from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tts.aliases import extract_prefixed_argument, matches_prefixed_command


class HelpCommandMixin:
    """Entradas slash/prefixadas da central de ajuda."""

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = prefixes["bot_prefix"]
        if not matches_prefixed_command(message.content, bot_prefix, kind="help"):
            return

        subject = extract_prefixed_argument(message.content, bot_prefix, kind="help") or None
        await self._send_help_response(
            guild=message.guild,
            owner=message.author,
            responder=message.channel,
            prefix_command_message=message,
            subject=subject,
        )

    @app_commands.command(name="help", description="Encontra comandos e recursos do bot")
    @app_commands.describe(assunto="Comando ou recurso que você procura")
    async def help_command(self, interaction: discord.Interaction, assunto: str | None = None):
        await self._send_help_response(
            guild=interaction.guild,
            owner=interaction.user,
            responder=interaction.channel,
            interaction=interaction,
            ephemeral=True,
            subject=assunto,
        )

    @help_command.autocomplete("assunto")
    async def help_subject_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._help_autocomplete_choices(interaction, current)
