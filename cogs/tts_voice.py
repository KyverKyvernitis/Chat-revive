from __future__ import annotations

import asyncio

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands

from config import BLOCK_VOICE_BOT_ID, OFF_COLOR, ON_COLOR
from tts_audio import GuildTTSState, TTSAudioMixin
from tts_helpers import (
    EDGE_DEFAULT_VOICE,
    PITCH_RE,
    RATE_RE,
    get_gtts_languages,
    make_embed,
    validate_engine,
)


class TTSVoice(TTSAudioMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_names: set[str] = set()
        self.edge_voice_cache: list[str] = []
        self.gtts_languages: dict[str, str] = {}

    async def cog_load(self):
        self.gtts_languages = get_gtts_languages()
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

    def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
        description = f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}"
        return self._make_embed(title, description, ok=True)

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

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

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    def _target_voice_bot_in_channel(self, voice_channel: discord.abc.Connectable) -> bool:
        if not BLOCK_VOICE_BOT_ID:
            return False
        for member in getattr(voice_channel, "members", []):
            if member.id == BLOCK_VOICE_BOT_ID:
                return True
        return False

    def _guild_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState()
            self.guild_states[guild_id] = state
        return state

    async def _is_block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            value = db.block_voice_bot_enabled(guild_id)
            if asyncio.iscoroutine(value):
                value = await value
            return bool(value)
        except Exception as e:
            print(f"[tts_voice] Erro ao ler block_voice_bot_enabled da guild {guild_id}: {e}")
            return False

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel: discord.abc.Connectable) -> bool:
        if not await self._is_block_voice_bot_enabled(guild.id):
            return False
        return self._target_voice_bot_in_channel(voice_channel)

    async def _disconnect_and_clear(self, guild: discord.Guild):
        state = self._guild_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
        except Exception:
            pass

        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=False)
            except Exception as e:
                print(f"[tts_voice] Erro ao desconectar e limpar na guild {guild.id}: {e}")

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if await self._should_block_for_voice_bot(guild, vc.channel):
            print(
                f"[tts_voice] Saindo da call por bloqueio de bot de voz | "
                f"guild={guild.id} channel={vc.channel.id} target_bot_id={BLOCK_VOICE_BOT_ID}"
            )
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel: discord.abc.Connectable):
        vc = guild.voice_client

        if vc and vc.channel and vc.channel.id == voice_channel.id and vc.is_connected():
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return

        if not message.content.startswith(','):
            return

        print(
            f"[tts_voice] on_message recebido | guild={message.guild.id} "
            f"channel_type={type(message.channel).__name__} user={message.author.id} raw={message.content!r}"
        )

        author_voice = getattr(message.author, 'voice', None)
        if author_voice is None or author_voice.channel is None:
            print(f"[tts_voice] Ignorado | usuário não está em call | guild={message.guild.id} user={message.author.id}")
            return

        voice_channel = author_voice.channel

        if await self._should_block_for_voice_bot(message.guild, voice_channel):
            await self._disconnect_if_blocked(message.guild)
            print(f"[tts_voice] Ignorado por bloqueio de bot de voz | guild={message.guild.id}")
            return

        db = self._get_db()
        if db is None:
            print('[tts_voice] settings_db indisponível')
            return

        resolved = db.resolve_tts(message.guild.id, message.author.id)
        if asyncio.iscoroutine(resolved):
            resolved = await resolved

        text = message.content[1:].strip()
        if not text:
            return

        state = self._guild_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, 'id', None)

        from tts_audio import QueueItem

        await state.queue.put(
            QueueItem(
                guild_id=message.guild.id,
                channel_id=voice_channel.id,
                author_id=message.author.id,
                text=text,
                engine=resolved['engine'],
                voice=resolved['voice'],
                language=resolved['language'],
                rate=resolved['rate'],
                pitch=resolved['pitch'],
            )
        )

        print(
            f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} user={message.author.id} "
            f"msg_channel={getattr(message.channel, 'id', None)} canal_voz={voice_channel.id} "
            f"engine={resolved['engine']} texto={text!r}"
        )

        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return

        if not await self._is_block_voice_bot_enabled(guild.id):
            return

        if self._target_voice_bot_in_channel(vc.channel):
            print(
                f"[tts_voice] Bot de voz alvo detectado na call | "
                f"guild={guild.id} channel={vc.channel.id} target_bot_id={BLOCK_VOICE_BOT_ID}"
            )
            await self._disconnect_and_clear(guild)

    @app_commands.command(name="tts_status", description="Mostra as configurações atuais de TTS")
    async def tts_status(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        user_cfg = db.get_user_tts(interaction.guild.id, interaction.user.id)
        guild_cfg = db.get_guild_tts_defaults(interaction.guild.id)
        resolved = db.resolve_tts(interaction.guild.id, interaction.user.id)
        block_enabled = db.block_voice_bot_enabled(interaction.guild.id)

        block_bot_text = (
            f"ativado ({BLOCK_VOICE_BOT_ID})" if block_enabled and BLOCK_VOICE_BOT_ID else "desativado"
        )

        desc = (
            "**Configuração usada agora**\n"
            f"- Engine: `{resolved['engine']}`\n"
            f"- Voz Edge: `{resolved['voice']}`\n"
            f"- Idioma gTTS: `{resolved['language']}`\n"
            f"- Velocidade: `{resolved['rate']}`\n"
            f"- Tom: `{resolved['pitch']}`\n\n"
            "**Sua configuração**\n"
            f"- Engine: `{user_cfg['engine'] or '-'}`\n"
            f"- Voz Edge: `{user_cfg['voice'] or '-'}`\n"
            f"- Idioma gTTS: `{user_cfg['language'] or '-'}`\n"
            f"- Velocidade: `{user_cfg['rate'] or '-'}`\n"
            f"- Tom: `{user_cfg['pitch'] or '-'}`\n\n"
            "**Padrão do servidor**\n"
            f"- Engine: `{guild_cfg['engine'] or '-'}`\n"
            f"- Voz Edge: `{guild_cfg['voice'] or '-'}`\n"
            f"- Idioma gTTS: `{guild_cfg['language'] or '-'}`\n"
            f"- Velocidade: `{guild_cfg['rate'] or '-'}`\n"
            f"- Tom: `{guild_cfg['pitch'] or '-'}`\n\n"
            f"**Bloqueio por outro bot de voz:** `{block_bot_text}`"
        )
        await self._respond(interaction,
            embed=self._make_embed("Status do TTS", desc, ok=True),
            ephemeral=True,
        )

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
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        items = list(self.gtts_languages.items())[:80]
        lines = [f"- `{code}` — {name}" for code, name in items]

        embed = self._format_list_block(
            "Idiomas do gTTS",
            lines,
            "Use `/set_language` para escolher um idioma do gTTS.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="set_tts_engine",
        description="Define qual engine de TTS você quer usar",
    )
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        engine = validate_engine(engine)
        await db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)

        extra = (
            "• `gtts`: usa idioma com `/set_language`\n"
            "• `edge`: permite voz, velocidade e tom"
        )

        await self._respond(interaction,
            embed=self._make_embed(
                "Engine atualizada",
                f"Sua engine de TTS agora é `{engine}`.\n\n{extra}",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="set_server_tts_engine",
        description="Define a engine de TTS padrão do servidor",
    )
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        engine = validate_engine(engine)
        await db.set_guild_tts_defaults(interaction.guild.id, engine=engine)

        await self._respond(interaction,
            embed=self._make_embed(
                "Engine padrão atualizada",
                (
                    f"A engine padrão do servidor agora é `{engine}`.\n\n"
                    "Essa configuração será usada por padrão para membros sem configuração própria."
                ),
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(interaction,
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice)
        await self._respond(interaction,
            embed=self._make_embed(
                "Voz atualizada",
                f"Sua voz do Edge foi definida para `{voice}`.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="set_server_voice",
        description="Define a voz padrão do Edge TTS no servidor",
    )
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(interaction,
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_guild_tts_defaults(interaction.guild.id, voice=voice)
        await self._respond(interaction,
            embed=self._make_embed(
                "Voz padrão atualizada",
                f"A voz padrão do servidor foi definida para `{voice}`.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set_language", description="Define seu idioma do gTTS")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    async def set_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(interaction,
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, language=language)
        await self._respond(interaction,
            embed=self._make_embed(
                "Idioma atualizado",
                f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.",
                ok=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="set_server_language",
        description="Define o idioma padrão do gTTS no servidor",
    )
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(interaction,
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await db.set_guild_tts_defaults(interaction.guild.id, language=language)
        await self._respond(interaction,
            embed=self._make_embed(
                "Idioma padrão atualizado",
                f"O idioma padrão do servidor foi definido para `{language}` — {self.gtts_languages[language]}.",
                ok=True,
            ),
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

    @app_commands.command(
        name="set_server_rate",
        description="Define a velocidade padrão de fala do servidor no Edge TTS",
    )
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=True)

    @app_commands.command(
        name="set_server_speed",
        description="Alias de /set_server_rate para velocidade padrão",
    )
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=True)

    async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        value = rate.strip()
        if not RATE_RE.fullmatch(value):
            await self._respond(interaction,
                embed=self._make_embed(
                    "Velocidade inválida",
                    "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona quando a engine estiver em `edge`.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, rate=value)
            title = "Velocidade padrão atualizada"
            desc = (
                f"A velocidade padrão do servidor foi definida para `{value}`.\n\n"
                "Esse ajuste só funciona quando a engine estiver em `edge`."
            )
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value)
            title = "Velocidade atualizada"
            desc = (
                f"Sua velocidade foi definida para `{value}`.\n\n"
                "Esse ajuste só funciona quando a engine estiver em `edge`."
            )

        await self._respond(interaction,
            embed=self._make_embed(title, desc, ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="set_pitch", description="Define seu tom de voz no Edge TTS")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(
        name="set_server_pitch",
        description="Define o tom de voz padrão do servidor no Edge TTS",
    )
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        value = pitch.strip()
        if not PITCH_RE.fullmatch(value):
            await self._respond(interaction,
                embed=self._make_embed(
                    "Tom inválido",
                    "Use o formato `+0Hz`, `+20Hz` ou `-10Hz`.\n\nEsse ajuste só funciona quando a engine estiver em `edge`.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if server:
            await db.set_guild_tts_defaults(interaction.guild.id, pitch=value)
            title = "Tom padrão atualizado"
            desc = (
                f"O tom padrão do servidor foi definido para `{value}`.\n\n"
                "Esse ajuste só funciona quando a engine estiver em `edge`."
            )
        else:
            await db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value)
            title = "Tom atualizado"
            desc = (
                f"Seu tom foi definido para `{value}`.\n\n"
                "Esse ajuste só funciona quando a engine estiver em `edge`."
            )

        await self._respond(interaction,
            embed=self._make_embed(title, desc, ok=True),
            ephemeral=True,
        )

    @app_commands.command(name="leave", description="Faz o bot sair da call e limpa a fila de TTS")
    async def leave(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)

        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return

        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Nada para desconectar",
                    "O bot não está conectado em nenhum canal de voz.",
                    ok=False,
                ),
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Entre em uma call",
                    "Você precisa estar em um canal de voz para usar esse comando.",
                    ok=False,
                ),
            )
            return

        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Canal diferente",
                    "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.",
                    ok=False,
                ),
            )
            return

        await self._disconnect_and_clear(interaction.guild)

        await self._respond(
            interaction,
            embed=self._make_embed(
                "Bot desconectado",
                "Saí da call e limpei a fila de TTS.",
                ok=True,
            ),
        )

    @app_commands.command(
        name="set_block_voice_bot",
        description="Ativa ou desativa o bloqueio quando outro bot de voz estiver na call",
    )
    @app_commands.describe(enabled="Use `true` para ativar ou `false` para desativar")
    @app_commands.default_permissions(manage_guild=True)
    async def set_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction,
                "Esse comando só pode ser usado em servidor.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction,
                "Você precisa da permissão `Gerenciar Servidor`.",
                ephemeral=True,
            )
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        await db.set_block_voice_bot_enabled(interaction.guild.id, enabled)
        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

        bot_info = str(BLOCK_VOICE_BOT_ID) if BLOCK_VOICE_BOT_ID else "não configurado"
        desc = (
            f"O bloqueio por outro bot de voz agora está `{'ativado' if enabled else 'desativado'}`.\n\n"
            f"Bot monitorado: `{bot_info}`\n"
            "Quando ativado, o bot evita entrar e também sai da call se o outro bot entrar no mesmo canal."
        )
        await self._respond(interaction,
            embed=self._make_embed("Bloqueio atualizado", desc, ok=True),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
