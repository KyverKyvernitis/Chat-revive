from __future__ import annotations

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import edge_tts

from config import BLOCK_VOICE_BOT_ID, OFF_COLOR, ON_COLOR
from tts_audio import GuildTTSState, TTSAudioMixin
from tts_helpers import (
    EDGE_DEFAULT_VOICE,
    PITCH_RE,
    RATE_RE,
    make_embed,
    validate_engine,
)
from tts_voice_events import TTSVoiceEventsMixin


class TTSVoice(TTSAudioMixin, TTSVoiceEventsMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_names: set[str] = set()
        self.edge_voice_cache: list[str] = []

    async def cog_load(self):
        try:
            await self._load_edge_voices()
        except Exception as e:
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE]
            self.edge_voice_names = set(self.edge_voice_cache)
            print(f"[tts_voice] cog_load fallback: {e}")

    async def cog_unload(self):
        for state in self.guild_states.values():
            if state.worker_task and not state.worker_task.done():
                state.worker_task.cancel()

    async def _load_edge_voices(self):
        try:
            voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=15)
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE]
            self.edge_voice_names = set(self.edge_voice_cache)
            print(f"[tts_voice] Falha ao carregar vozes edge, usando fallback: {e}")

    def _make_embed(self, title: str, description: str, ok: bool = True) -> discord.Embed:
        return make_embed(title, description, ok=ok, on_color=ON_COLOR, off_color=OFF_COLOR)

    @app_commands.command(name="tts_status", description="Mostra as configurações atuais de TTS")
    async def tts_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        user_cfg = db.get_user_tts(interaction.guild.id, interaction.user.id)
        guild_cfg = db.get_guild_tts_defaults(interaction.guild.id)
        resolved = db.resolve_tts(interaction.guild.id, interaction.user.id)
        block_enabled = db.block_voice_bot_enabled(interaction.guild.id)

        desc = (
            f"**Resolvido para você agora:**\n"
            f"- Engine: `{resolved['engine']}`\n"
            f"- Voz: `{resolved['voice']}`\n"
            f"- Velocidade: `{resolved['rate']}`\n"
            f"- Tom: `{resolved['pitch']}`\n\n"
            f"**Config do usuário:**\n"
            f"- Engine: `{user_cfg['engine'] or '-'}`\n"
            f"- Voz: `{user_cfg['voice'] or '-'}`\n"
            f"- Velocidade: `{user_cfg['rate'] or '-'}`\n"
            f"- Tom: `{user_cfg['pitch'] or '-'}`\n\n"
            f"**Padrão do servidor:**\n"
            f"- Engine: `{guild_cfg['engine'] or '-'}`\n"
            f"- Voz: `{guild_cfg['voice'] or '-'}`\n"
            f"- Velocidade: `{guild_cfg['rate'] or '-'}`\n"
            f"- Tom: `{guild_cfg['pitch'] or '-'}`\n\n"
            f"**Bloqueio por outro bot de voz:** `{'ativado' if block_enabled else 'desativado'}`"
        )

        await interaction.response.send_message(
            embed=self._make_embed("Status do TTS", desc, ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="voices", description="Lista algumas vozes do Edge TTS")
    async def voices(self, interaction: discord.Interaction):
        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voices = [v for v in self.edge_voice_cache if v.startswith("pt-")]
        if not voices:
            voices = self.edge_voice_cache[:30]

        shown = voices[:40]
        text = "\n".join(f"- `{v}`" for v in shown)

        desc = (
            f"{text}\n\n"
            f"Os comandos de **voz**, **velocidade** e **tom** só têm efeito usando engine `edge`."
        )

        await interaction.response.send_message(
            embed=self._make_embed("Vozes disponíveis", desc, ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_tts_engine", description="Define sua engine de TTS entre gtts e edge")
    @app_commands.describe(engine="gtts ou edge")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        engine = validate_engine(engine)
        await db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)

        await interaction.response.send_message(
            embed=self._make_embed("Engine atualizada", f"Sua engine de TTS agora é `{engine}`.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_server_tts_engine", description="Define a engine padrão do servidor")
    @app_commands.describe(engine="gtts ou edge")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        engine = validate_engine(engine)
        await db.set_guild_tts_defaults(interaction.guild.id, engine=engine)

        await interaction.response.send_message(
            embed=self._make_embed("Engine padrão atualizada", f"A engine padrão do servidor agora é `{engine}`.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS. Use `/voices` para ver opções válidas.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice)

        await interaction.response.send_message(
            embed=self._make_embed(
                "Voz atualizada",
                f"Sua voz foi definida para `{voice}`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do servidor")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS. Use `/voices` para ver opções válidas.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_guild_tts_defaults(interaction.guild.id, voice=voice)

        await interaction.response.send_message(
            embed=self._make_embed(
                "Voz padrão atualizada",
                f"A voz padrão do servidor foi definida para `{voice}`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set_rate", description="Define sua velocidade do Edge TTS")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    async def set_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=False)

    @app_commands.command(name="set_speed", description="Alias de /set_rate")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=False)

    @app_commands.command(name="set_server_rate", description="Define a velocidade padrão do servidor")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=True)

    @app_commands.command(name="set_server_speed", description="Alias de /set_server_rate")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=True)

    async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not RATE_RE.fullmatch(rate.strip()):
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Velocidade inválida",
                    "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, rate=rate.strip())
            title = "Velocidade padrão atualizada"
            desc = f"A velocidade padrão do servidor foi definida para `{rate.strip()}`.\n\nEsse ajuste só funciona com engine `edge`."
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=rate.strip())
            title = "Velocidade atualizada"
            desc = f"Sua velocidade foi definida para `{rate.strip()}`.\n\nEsse ajuste só funciona com engine `edge`."

        await interaction.response.send_message(embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    @app_commands.command(name="set_pitch", description="Define seu tom do Edge TTS")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(name="set_server_pitch", description="Define o tom padrão do servidor")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not PITCH_RE.fullmatch(pitch.strip()):
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Tom inválido",
                    "Use o formato `+0Hz`, `+20Hz` ou `-10Hz`.\n\nEsse ajuste só funciona com engine `edge`.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch.strip())
            title = "Tom padrão atualizado"
            desc = f"O tom padrão do servidor foi definido para `{pitch.strip()}`.\n\nEsse ajuste só funciona com engine `edge`."
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=pitch.strip())
            title = "Tom atualizado"
            desc = f"Seu tom foi definido para `{pitch.strip()}`.\n\nEsse ajuste só funciona com engine `edge`."

        await interaction.response.send_message(embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    @app_commands.command(name="toggle_block_voice_bot", description="Ativa ou desativa o bloqueio se outro bot de voz estiver na call")
    @app_commands.default_permissions(manage_guild=True)
    async def toggle_block_voice_bot(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
            return

        if not BLOCK_VOICE_BOT_ID:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Bot de voz não configurado",
                    "Defina `BLOCK_VOICE_BOT_ID` nas variáveis de ambiente para usar essa função.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        current = db.block_voice_bot_enabled(interaction.guild.id)
        new_value = not current
        await db.set_block_voice_bot_enabled(interaction.guild.id, new_value)

        if new_value:
            await self._disconnect_if_blocked(interaction.guild)

        await interaction.response.send_message(
            embed=self._make_embed(
                "Bloqueio atualizado",
                f"O bloqueio por outro bot de voz foi **{'ativado' if new_value else 'desativado'}**.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="leave", description="Faz o bot sair do canal de voz")
    async def leave(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                embed=self._make_embed("Não conectado", "O bot não está em nenhum canal de voz.", ok=False),
                ephemeral=True,
            )
            return

        await self._disconnect_and_clear(interaction.guild)

        await interaction.response.send_message(
            embed=self._make_embed("Saí da call", "O bot saiu do canal de voz e limpou a fila.", ok=True),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
