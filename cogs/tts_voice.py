import inspect
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin


def get_gtts_languages() -> dict[str, str]:
    try:
        from gtts.lang import tts_langs
        return tts_langs()
    except Exception:
        return {
            "pt-br": "Portuguese (Brazil)",
            "pt": "Portuguese",
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "ja": "Japanese",
        }


def validate_engine(engine: str) -> str:
    return "edge" if str(engine or "").strip().lower() == "edge" else "gtts"


class TTSVoice(TTSAudioMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_cache: list[str] = []
        self.edge_voice_names: set[str] = set()
        self.gtts_languages: dict[str, str] = get_gtts_languages()

    async def cog_load(self):
        await self._load_edge_voices()

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _load_edge_voices(self):
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")
            self.edge_voice_cache = []
            self.edge_voice_names = set()

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        color = discord.Color.green() if ok else discord.Color.red()
        return discord.Embed(title=title, description=description, color=color)

    def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
        description = f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}"
        return self._make_embed(title, description, ok=True)

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _respond(self, interaction: discord.Interaction, *, content: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = True):
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        if not value[1:].isdigit():
            return None
        return f"{value[0]}{value[1:]}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.lower().endswith("hz"):
            value = value[:-2]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        if not value[1:].isdigit():
            return None
        return f"{value[0]}{value[1:]}Hz"

    def _target_voice_bot_id(self) -> Optional[int]:
        for name in ("VOICE_BOT_ID", "BLOCK_VOICE_BOT_ID"):
            value = getattr(config, name, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass
        return None

    def _target_voice_bot_in_channel(self, voice_channel) -> bool:
        target_bot_id = self._target_voice_bot_id()
        if not target_bot_id or voice_channel is None:
            return False
        return any(member.id == target_bot_id for member in getattr(voice_channel, "members", []))

    async def _block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            data = await self._maybe_await(db.get_guild_tts_defaults(guild_id))
            return bool((data or {}).get("block_voice_bot", False))
        except Exception as e:
            print(f"[tts_voice] Erro ao ler block_voice_bot da guild {guild_id}: {e}")
            return False

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel) -> bool:
        return await self._block_voice_bot_enabled(guild.id) and self._target_voice_bot_in_channel(voice_channel)

    async def _disconnect_and_clear(self, guild: discord.Guild):
        state = self._get_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()
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
                print(f"[tts_voice] erro ao desconectar guild {guild.id}: {e}")

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        await self._disconnect_and_clear(guild)

    def _voice_channel_has_only_bots_or_is_empty(self, voice_channel) -> bool:
        if voice_channel is None:
            return True
        members = list(getattr(voice_channel, "members", []))
        return not any(not m.bot for m in members)

    async def _disconnect_if_alone_or_only_bots(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if self._voice_channel_has_only_bots_or_is_empty(vc.channel):
            print(f"[tts_voice] saindo da call | sozinho ou só com bots | guild={guild.id} channel={vc.channel.id}")
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel) -> Optional[discord.VoiceClient]:
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected recebeu canal None | guild={guild.id}")
            return None
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
        print(f"[tts_voice] on_message recebido | guild={getattr(message.guild, 'id', None)} channel_type={type(message.channel).__name__} user={getattr(message.author, 'id', None)} raw={message.content!r}")
        if not getattr(config, "TTS_ENABLED", True):
            return
        if message.author.bot or not message.guild or not message.content or not message.content.startswith(","):
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            print("[tts_voice] ignorado | autor não está em call")
            return

        voice_channel = author_voice.channel
        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            print(f"[tts_voice] bloqueado | outro bot de voz detectado | guild={message.guild.id} canal_voz={voice_channel.id}")
            await self._disconnect_and_clear(message.guild)
            return

        db = self._get_db()
        if db is None:
            print("[tts_voice] ignorado | settings_db indisponível")
            return

        try:
            resolved = await self._maybe_await(db.resolve_tts(message.guild.id, message.author.id))
        except Exception as e:
            print(f"[tts_voice] erro em resolve_tts | guild={message.guild.id} user={message.author.id} erro={e}")
            return

        text = message.content[1:].strip()
        if not text:
            print("[tts_voice] ignorado | texto vazio após prefixo")
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)
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
        print(f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} user={message.author.id} msg_channel={getattr(message.channel, 'id', None)} canal_voz={voice_channel.id} engine={resolved['engine']} texto={text!r}")
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if await self._block_voice_bot_enabled(guild.id):
            if self._target_voice_bot_in_channel(vc.channel):
                print(f"[tts_voice] Bot de voz alvo detectado na call | guild={guild.id} channel={vc.channel.id} target_bot_id={self._target_voice_bot_id()}")
                await self._disconnect_and_clear(guild)
                return
        await self._disconnect_if_alone_or_only_bots(guild)

    @app_commands.command(name="voices_edge", description="Mostra as vozes disponíveis do Edge TTS")
    async def voices_edge(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not self.edge_voice_cache:
            await self._load_edge_voices()
        voices = [v for v in self.edge_voice_cache if v.startswith("pt-")] or self.edge_voice_cache[:40]
        lines = [f"- `{v}`" for v in voices[:40]]
        await self._respond(interaction, embed=self._format_list_block("Vozes do Edge TTS", lines, "Use `/set_voice` para escolher uma voz do Edge."))

    @app_commands.command(name="voices_gtts", description="Mostra os idiomas disponíveis do gTTS")
    async def voices_gtts(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()
        items = list(self.gtts_languages.items())[:80]
        lines = [f"- `{code}` — {name}" for code, name in items]
        await self._respond(interaction, embed=self._format_list_block("Idiomas do gTTS", lines, "Use `/set_language` para escolher um idioma do gTTS."))

    @app_commands.command(name="set_tts_engine", description="Define qual engine de TTS você quer usar")
    @app_commands.describe(engine="Escolha entre gtts e edge")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        engine = validate_engine(engine)
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine))
        await self._respond(interaction, embed=self._make_embed("Engine atualizada", f"Sua engine de TTS agora é `{engine}`.\n\n• `gtts`: usa idioma com `/set_language`\n• `edge`: permite voz, velocidade e tom", ok=True))

    @app_commands.command(name="set_server_tts_engine", description="Define a engine de TTS padrão do servidor")
    @app_commands.describe(engine="Escolha entre gtts e edge")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        engine = validate_engine(engine)
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=engine))
        await self._respond(interaction, embed=self._make_embed("Engine padrão atualizada", f"A engine padrão do servidor agora é `{engine}`.\n\nEssa configuração será usada por padrão para membros sem configuração própria.", ok=True))

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.edge_voice_cache:
            await self._load_edge_voices()
        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz atualizada", f"Sua voz do Edge foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do Edge TTS no servidor")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.edge_voice_cache:
            await self._load_edge_voices()
        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz padrão atualizada", f"A voz padrão do servidor foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_language", description="Define seu idioma do gTTS")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    async def set_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()
        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=language))
        await self._respond(interaction, embed=self._make_embed("Idioma atualizado", f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True))

    @app_commands.command(name="set_server_language", description="Define o idioma padrão do gTTS no servidor")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()
        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
        await self._respond(interaction, embed=self._make_embed("Idioma padrão atualizado", f"O idioma padrão do servidor foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True))

    @app_commands.command(name="set_rate", description="Define sua velocidade de fala no Edge TTS")
    @app_commands.describe(rate="Exemplo: 10%, +10%, -10%")
    async def set_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=False)

    @app_commands.command(name="set_speed", description="Alias de /set_rate para velocidade de fala")
    @app_commands.describe(speed="Exemplo: 10%, +10%, -10%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=False)

    @app_commands.command(name="set_server_rate", description="Define a velocidade padrão de fala do servidor no Edge TTS")
    @app_commands.describe(rate="Exemplo: 10%, +10%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=True)

    @app_commands.command(name="set_server_speed", description="Alias de /set_server_rate para velocidade padrão")
    @app_commands.describe(speed="Exemplo: 10%, +10%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=True)

    async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(interaction.guild.id))
        user_settings = await self._maybe_await(db.get_user_tts(interaction.guild.id, interaction.user.id))
        current_engine = (user_settings.get("engine") or guild_defaults.get("engine") or "gtts")
        if current_engine != "edge":
            await self._respond(interaction, embed=self._make_embed("Engine incompatível", "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.", ok=False))
            return
        value = self._normalize_rate_value(rate)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False))
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title, desc = "Velocidade padrão atualizada", f"A velocidade padrão do servidor foi definida para `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value))
            title, desc = "Velocidade atualizada", f"Sua velocidade foi definida para `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True))

    @app_commands.command(name="set_pitch", description="Define seu tom de voz no Edge TTS")
    @app_commands.describe(pitch="Exemplo: 10Hz, +10Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(name="set_server_pitch", description="Define o tom de voz padrão do servidor no Edge TTS")
    @app_commands.describe(pitch="Exemplo: 10Hz, +10Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(interaction.guild.id))
        user_settings = await self._maybe_await(db.get_user_tts(interaction.guild.id, interaction.user.id))
        current_engine = (user_settings.get("engine") or guild_defaults.get("engine") or "gtts")
        if current_engine != "edge":
            await self._respond(interaction, embed=self._make_embed("Engine incompatível", "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.", ok=False))
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False))
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title, desc = "Tom padrão atualizado", f"O tom padrão do servidor foi definido para `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value))
            title, desc = "Tom atualizado", f"Seu tom foi definido para `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True))

    @app_commands.command(name="set_block_voice_bot", description="Ativa ou desativa o bloqueio quando o outro bot de voz estiver na call")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def set_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor`.", ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
        await self._respond(interaction, embed=self._make_embed("Bloqueio atualizado", f"O bloqueio por outro bot de voz agora está em `{enabled}`.", ok=True))
        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

    @app_commands.command(name="leave", description="Faz o bot sair da call e limpa a fila de TTS")
    async def leave(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._respond(interaction, embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz.", ok=False))
            return
        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await self._respond(interaction, embed=self._make_embed("Entre em uma call", "Você precisa estar em um canal de voz para usar esse comando.", ok=False))
            return
        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False))
            return
        await self._disconnect_and_clear(interaction.guild)
        await self._respond(interaction, embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True))

    @app_commands.command(name="tts_settings", description="Mostra suas configurações atuais de TTS neste servidor")
    async def tts_settings(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)

        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        try:
            guild_defaults = db.get_guild_tts_defaults(interaction.guild.id)
            guild_defaults = await self._maybe_await(guild_defaults)

            user_settings = db.get_user_tts(interaction.guild.id, interaction.user.id)
            user_settings = await self._maybe_await(user_settings)

            resolved = db.resolve_tts(interaction.guild.id, interaction.user.id)
            resolved = await self._maybe_await(resolved)
        except Exception as e:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Erro ao carregar configurações",
                    f"Não consegui ler suas configurações de TTS.\n\nErro: `{e}`",
                    ok=False,
                ),
            )
            return

        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}

        engine = resolved.get("engine", "gtts")
        voice = resolved.get("voice", "Não definido")
        language = resolved.get("language", "Não definido")
        rate = resolved.get("rate", "+0%")
        pitch = resolved.get("pitch", "+0Hz")
        block_voice_bot = bool(guild_defaults.get("block_voice_bot", False))

        embed = discord.Embed(
            title="Configurações de TTS",
            description="Resumo das suas configurações atuais neste servidor.",
            color=discord.Color.blurple(),
        )

        embed.add_field(name="Engine ativa", value=f"`{engine}`", inline=True)
        embed.add_field(name="Voz Edge ativa", value=f"`{voice}`", inline=True)
        embed.add_field(name="Idioma gTTS ativo", value=f"`{language}`", inline=True)

        embed.add_field(name="Velocidade ativa", value=f"`{rate}`", inline=True)
        embed.add_field(name="Tom ativo", value=f"`{pitch}`", inline=True)
        embed.add_field(
            name="Bloqueio por outro bot",
            value="`Ativado`" if block_voice_bot else "`Desativado`",
            inline=True,
        )

        embed.add_field(
            name="Suas configurações salvas",
            value=(
                f"Engine: `{user_settings.get('engine', '—')}`\n"
                f"Voz: `{user_settings.get('voice', '—')}`\n"
                f"Idioma: `{user_settings.get('language', '—')}`\n"
                f"Velocidade: `{user_settings.get('rate', '—')}`\n"
                f"Tom: `{user_settings.get('pitch', '—')}`"
            ),
            inline=False,
        )

        embed.add_field(
            name="Padrões do servidor",
            value=(
                f"Engine: `{guild_defaults.get('engine', '—')}`\n"
                f"Voz: `{guild_defaults.get('voice', '—')}`\n"
                f"Idioma: `{guild_defaults.get('language', '—')}`\n"
                f"Velocidade: `{guild_defaults.get('rate', '—')}`\n"
                f"Tom: `{guild_defaults.get('pitch', '—')}`"
            ),
            inline=False,
        )

        await self._respond(interaction, embed=embed)



async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
