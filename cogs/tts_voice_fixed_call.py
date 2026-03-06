from __future__ import annotations

import asyncio
from typing import Optional

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands

import config
from tts_audio import TTSAudioMixin
from tts_helpers import (
    EDGE_DEFAULT_VOICE,
    get_gtts_languages,
    make_embed,
    validate_engine,
)


class TTSVoice(commands.Cog, TTSAudioMixin):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, object] = {}
        self.edge_voice_cache: list[str] = []
        self.edge_voice_names: set[str] = set()
        self.gtts_languages: dict[str, str] = get_gtts_languages()

    async def cog_load(self):
        await self._load_edge_voices()

    async def _load_edge_voices(self):
        try:
            voices = await edge_tts.list_voices()
            names = sorted({str(v.get("ShortName", "")).strip() for v in voices if v.get("ShortName")})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE]
            self.edge_voice_names = {EDGE_DEFAULT_VOICE}

    def _make_embed(self, title: str, description: str, *, ok: bool) -> discord.Embed:
        return make_embed(
            title,
            description,
            ok=ok,
            on_color=config.ON_COLOR,
            off_color=config.OFF_COLOR,
        )

    def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
        description = f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}"
        return self._make_embed(title, description, ok=True)

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    def _resolve_effective_engine(self, guild_id: int, user_id: int) -> str:
        db = self._get_db()
        if db is None:
            return "gtts"
        resolved = db.resolve_tts(guild_id, user_id)
        return str(resolved.get("engine", "gtts") or "gtts").lower()

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip()
        value = value.replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        if value.endswith("%"):
            value = value[:-1]

        if not value:
            return None

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]

        if not number.isdigit():
            return None

        return f"{sign}{number}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip()
        value = value.replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        lower = value.lower()
        if lower.endswith("hz"):
            value = value[:-2]

        if not value:
            return None

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]

        if not number.isdigit():
            return None

        return f"{sign}{number}Hz"

    async def _ensure_connected(
        self,
        guild: discord.Guild,
        voice_channel: discord.abc.Connectable,
    ) -> Optional[discord.VoiceClient]:
        vc = guild.voice_client

        if vc and vc.channel and vc.channel.id == voice_channel.id:
            return vc

        try:
            if vc and vc.is_connected():
                await vc.move_to(voice_channel)
                print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                return vc

            new_vc = await voice_channel.connect(self_deaf=True)
            print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
            return new_vc

        except Exception as e:
            print(f"[tts_voice] Erro ao conectar na guild {guild.id}: {e}")
            return None

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    async def _should_block_for_voice_bot(
        self,
        guild: discord.Guild,
        voice_channel: discord.abc.Connectable,
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        if not db.block_voice_bot_enabled(guild.id):
            return False
        target_id = int(config.BLOCK_VOICE_BOT_ID or 0)
        if target_id <= 0:
            return False
        members = getattr(voice_channel, "members", []) or []
        return not any(member.bot and member.id == target_id for member in members)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not config.TTS_ENABLED:
            return

        if message.author.bot:
            return

        if not message.guild:
            return

        if not message.content:
            return

        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return

        if not message.content.startswith(","):
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            return

        voice_channel = author_voice.channel

        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            await self._disconnect_if_blocked(message.guild)
            return

        db = self._get_db()
        if db is None:
            print("[tts_voice] settings_db indisponível")
            return

        resolved = db.resolve_tts(message.guild.id, message.author.id)

        text = message.content[1:].strip()
        if not text:
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = message.channel.id

        from tts_audio import QueueItem

        await state.queue.put(
            QueueItem(
                guild_id=message.guild.id,
                channel_id=voice_channel.id,
                author_id=message.author.id,
                text=text,
                engine=resolved["engine"],
                voice=resolved["voice"],
                language=resolved["language"],
                rate=resolved["rate"],
                pitch=resolved["pitch"],
            )
        )

        print(
            f"[tts_voice] Mensagem enfileirada | "
            f"guild={message.guild.id} user={message.author.id} "
            f"canal_voz={voice_channel.id} engine={resolved['engine']} texto={text!r}"
        )

        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not member.guild:
            return

        guild = member.guild
        vc = guild.voice_client
        if vc is None or vc.channel is None:
            return

        current_channel = vc.channel
        humans = [m for m in getattr(current_channel, "members", []) if not m.bot]
        if not humans:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            return

        blocked = await self._should_block_for_voice_bot(guild, current_channel)
        if blocked:
            await self._disconnect_if_blocked(guild)

    @app_commands.command(name="voices_edge", description="Mostra as vozes disponíveis do Edge TTS")
    async def voices_edge(self, interaction: discord.Interaction):
        if not self.edge_voice_cache:
            await self._load_edge_voices()
        voices = [v for v in self.edge_voice_cache if v.startswith("pt-")] or self.edge_voice_cache[:40]
        lines = [f"- `{v}`" for v in voices[:40]]
        embed = self._format_list_block(
            "Vozes do Edge TTS",
            lines,
            "Use `/set_voice` para escolher uma voz do Edge.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="voices_gtts", description="Mostra os idiomas disponíveis do gTTS")
    async def voices_gtts(self, interaction: discord.Interaction):
        items = list(self.gtts_languages.items())[:80]
        lines = [f"- `{code}` — {name}" for code, name in items]
        embed = self._format_list_block(
            "Idiomas do gTTS",
            lines,
            "Use `/set_language` para escolher um idioma do gTTS.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_tts_engine", description="Define qual engine de TTS você quer usar")
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        engine = validate_engine(engine)
        await db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)
        extra = "• `gtts`: usa idioma com `/set_language`\n• `edge`: permite voz, velocidade e tom"
        await interaction.response.send_message(
            embed=self._make_embed("Engine atualizada", f"Sua engine de TTS agora é `{engine}`.\n\n{extra}", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_server_tts_engine", description="Define a engine de TTS padrão do servidor")
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        engine = validate_engine(engine)
        await db.set_guild_tts_defaults(interaction.guild.id, engine=engine)
        await interaction.response.send_message(
            embed=self._make_embed(
                "Engine padrão atualizada",
                f"A engine padrão do servidor agora é `{engine}`.\n\nEssa configuração será usada por padrão para membros sem configuração própria.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        db = self._get_db()
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
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice)
        await interaction.response.send_message(
            embed=self._make_embed("Voz atualizada", f"Sua voz do Edge foi definida para `{voice}`.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do Edge TTS no servidor")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
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
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await db.set_guild_tts_defaults(interaction.guild.id, voice=voice)
        await interaction.response.send_message(
            embed=self._make_embed("Voz padrão atualizada", f"A voz padrão do servidor foi definida para `{voice}`.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_language", description="Define seu idioma do gTTS")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    async def set_language(self, interaction: discord.Interaction, language: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        language = language.strip().lower()
        if language not in self.gtts_languages:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await db.set_user_tts(interaction.guild.id, interaction.user.id, language=language)
        await interaction.response.send_message(
            embed=self._make_embed("Idioma atualizado", f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_server_language", description="Define o idioma padrão do gTTS no servidor")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_language(self, interaction: discord.Interaction, language: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        language = language.strip().lower()
        if language not in self.gtts_languages:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await db.set_guild_tts_defaults(interaction.guild.id, language=language)
        await interaction.response.send_message(
            embed=self._make_embed("Idioma padrão atualizado", f"O idioma padrão do servidor foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_rate", description="Define sua velocidade de fala no Edge TTS")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    async def set_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=False)

    @app_commands.command(name="set_speed", description="Alias de /set_rate para velocidade de fala")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=False)

    @app_commands.command(name="set_server_rate", description="Define a velocidade padrão de fala do servidor no Edge TTS")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=True)

    @app_commands.command(name="set_server_speed", description="Alias de /set_server_rate para velocidade padrão")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=True)

    async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        current_engine = self._resolve_effective_engine(interaction.guild.id, interaction.user.id)
        if current_engine != "edge":
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Engine incompatível",
                    "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        value = self._normalize_rate_value(rate)
        if value is None:
            await interaction.response.send_message(
                embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False),
                ephemeral=True,
            )
            return
        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, rate=value)
            title = "Velocidade padrão atualizada"
            desc = f"A velocidade padrão do servidor foi definida para `{value}`."
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value)
            title = "Velocidade atualizada"
            desc = f"Sua velocidade foi definida para `{value}`."
        await interaction.response.send_message(embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    @app_commands.command(name="set_pitch", description="Define seu tom de voz no Edge TTS")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(name="set_server_pitch", description="Define o tom de voz padrão do servidor no Edge TTS")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        current_engine = self._resolve_effective_engine(interaction.guild.id, interaction.user.id)
        if current_engine != "edge":
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Engine incompatível",
                    "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await interaction.response.send_message(
                embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False),
                ephemeral=True,
            )
            return
        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, pitch=value)
            title = "Tom padrão atualizado"
            desc = f"O tom padrão do servidor foi definido para `{value}`."
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value)
            title = "Tom atualizado"
            desc = f"Seu tom foi definido para `{value}`."
        await interaction.response.send_message(embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    @app_commands.command(name="set_block_voice_bot", description="Ativa ou desativa a trava do outro bot de voz")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    @app_commands.default_permissions(manage_guild=True)
    async def set_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Você precisa da permissão `Gerenciar Servidor`.", ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return
        await db.set_block_voice_bot_enabled(interaction.guild.id, enabled)
        if enabled:
            desc = "A trava foi ativada. O bot só ficará na call quando o outro bot de voz estiver presente."
        else:
            desc = "A trava foi desativada. O bot poderá ficar na call sem depender do outro bot de voz."
        await interaction.response.send_message(
            embed=self._make_embed("Configuração atualizada", desc, ok=True),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
