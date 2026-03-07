import discord
from discord import app_commands
from discord.ext import commands

import config


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value):
        import inspect
        if inspect.isawaitable(value):
            return await value
        return value

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green() if ok else discord.Color.red(),
        )

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = True,
    ):
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    @app_commands.command(
        name="set_only_tts_user",
        description="Ativa ou desativa o modo em que o bot só responde um membro específico",
    )
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def set_only_tts_user(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            await self._respond(
                interaction,
                content="Esse comando só pode ser usado em servidor.",
                ephemeral=False,
            )
            return

        if not interaction.user.guild_permissions.kick_members:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                    ok=False,
                ),
                ephemeral=False,
            )
            return

        db = self._get_db()
        if db is None:
            await self._respond(
                interaction,
                content="Banco de dados indisponível.",
                ephemeral=False,
            )
            return

        await self._maybe_await(
            db.set_guild_tts_defaults(
                interaction.guild.id,
                only_target_user=bool(enabled),
            )
        )

        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)

        if enabled:
            desc = (
                "Só a Cuca pode falar nesse caralho, fodasse os betas.\n\n"
                f"ID alvo da env: `{target_user_id}`"
            )
        else:
            desc = (
                "Agora os betinhas podem usar também.\n\n"
                f"ID alvo da env: `{target_user_id}`"
            )

        await self._respond(
            interaction,
            embed=self._make_embed(
                "Modo de membro específico atualizado",
                desc,
                ok=True,
            ),
            ephemeral=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
