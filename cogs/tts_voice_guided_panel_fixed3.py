import inspect
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin

from typing import Callable


def _shorten(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _BaseTTSView(discord.ui.View):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True


class _SimpleSelectView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, title: str, description: str, select: discord.ui.Select):
        super().__init__(cog, owner_id, guild_id)
        self.title = title
        self.description = description
        self.add_item(select)

    async def send(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(self.title, self.description, ok=True),
            view=self,
            ephemeral=True,
        )


class ModeSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="gtts", description="Mais simples e compatível", value="gtts", emoji="🗣️"),
            discord.SelectOption(label="edge", description="Voz natural com voice, speed e pitch", value="edge", emoji="✨"),
        ]
        super().__init__(
            placeholder="Escolha o modo de TTS",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog._apply_mode_from_panel(interaction, self.values[0], server=self.server)


class LanguageSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = []
        for code, name in list(sorted(cog.gtts_languages.items()))[:25]:
            options.append(
                discord.SelectOption(
                    label=_shorten(f"{code} — {name}"),
                    description="Idioma do modo gtts",
                    value=code,
                )
            )
        super().__init__(
            placeholder="Escolha um idioma do gtts",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog._apply_language_from_panel(interaction, self.values[0], server=self.server)


class SpeedSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-50%", description="Bem mais devagar", value="-50%"),
            discord.SelectOption(label="-25%", description="Mais devagar", value="-25%"),
            discord.SelectOption(label="+0%", description="Velocidade normal", value="+0%"),
            discord.SelectOption(label="+25%", description="Mais rápido", value="+25%"),
            discord.SelectOption(label="+50%", description="Bem mais rápido", value="+50%"),
        ]
        super().__init__(placeholder="Escolha uma velocidade", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.cog._apply_speed_from_panel(interaction, self.values[0], server=self.server)


class PitchSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-50Hz", description="Mais grave", value="-50Hz"),
            discord.SelectOption(label="-25Hz", description="Levemente grave", value="-25Hz"),
            discord.SelectOption(label="+0Hz", description="Tom normal", value="+0Hz"),
            discord.SelectOption(label="+25Hz", description="Levemente agudo", value="+25Hz"),
            discord.SelectOption(label="+50Hz", description="Mais agudo", value="+50Hz"),
        ]
        super().__init__(placeholder="Escolha um tom", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.cog._apply_pitch_from_panel(interaction, self.values[0], server=self.server)


class VoiceRegionSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        regions = sorted({voice.rsplit("-", 1)[0] for voice in (cog.edge_voice_cache or [])})
        if not regions:
            regions = ["pt-BR"]
        options = [
            discord.SelectOption(
                label=_shorten(region),
                description="Abre a lista de vozes dessa região",
                value=region,
            )
            for region in regions[:25]
        ]
        super().__init__(placeholder="Escolha a região da voz", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        voices = [v for v in (self.cog.edge_voice_cache or []) if v.startswith(region + "-")]
        if not voices:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Nenhuma voz encontrada", "Não encontrei vozes para essa região.", ok=False),
                ephemeral=True,
            )
            return
        view = _SimpleSelectView(
            self.cog,
            interaction.user.id,
            self.guild_id if hasattr(self, "guild_id") else interaction.guild.id,
            "Escolha a voz",
            f"Região selecionada: `{region}`",
            VoiceSelect(self.cog, server=self.server, voices=voices),
        )
        await interaction.response.send_message(
            embed=self.cog._make_embed("Escolha a voz", f"Região selecionada: `{region}`", ok=True),
            view=view,
            ephemeral=True,
        )


class VoiceSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool, voices: list[str]):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(
                label=_shorten(voice),
                description="Voz do modo edge",
                value=voice,
            )
            for voice in voices[:25]
        ]
        super().__init__(placeholder="Escolha uma voz do edge", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.cog._apply_voice_from_panel(interaction, self.values[0], server=self.server)


class ToggleSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", toggle_name: str):
        self.cog = cog
        self.toggle_name = toggle_name
        desc = "Ativar" if toggle_name == "only_target_user" else "Ativar"
        options = [
            discord.SelectOption(label="Ativar", description="Liga essa função", value="true", emoji="✅"),
            discord.SelectOption(label="Desativar", description="Desliga essa função", value="false", emoji="⛔"),
        ]
        super().__init__(placeholder="Escolha se quer ativar ou desativar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        enabled = self.values[0] == "true"
        if self.toggle_name == "only_target_user":
            await self.cog._apply_only_target_from_panel(interaction, enabled)
        else:
            await self.cog._apply_block_voice_bot_from_panel(interaction, enabled)


class TTSMainPanelView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False):
        super().__init__(cog, owner_id, guild_id)
        self.server = server

    @discord.ui.button(label="Modo", style=discord.ButtonStyle.secondary, emoji="🎛️", row=0)
    async def mode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Escolha o modo", "Selecione como a fala vai funcionar.", ModeSelect(self.cog, server=self.server)).send(interaction)

    @discord.ui.button(label="Voz", style=discord.ButtonStyle.secondary, emoji="🎙️", row=0)
    async def voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Escolha a região da voz", "Primeiro escolha a região, depois selecione a voz.", VoiceRegionSelect(self.cog, server=self.server))
        await view.send(interaction)

    @discord.ui.button(label="Idioma", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Escolha o idioma", "Selecione o idioma usado no modo gtts.", LanguageSelect(self.cog, server=self.server)).send(interaction)

    @discord.ui.button(label="Velocidade", style=discord.ButtonStyle.secondary, emoji="⏩", row=1)
    async def speed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Escolha a velocidade", "Selecione uma velocidade pronta para o modo edge.", SpeedSelect(self.cog, server=self.server)).send(interaction)

    @discord.ui.button(label="Tom", style=discord.ButtonStyle.secondary, emoji="🎚️", row=1)
    async def pitch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Escolha o tom", "Selecione um tom pronto para o modo edge.", PitchSelect(self.cog, server=self.server)).send(interaction)

    @discord.ui.button(label="Atualizar painel", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await self.cog._build_settings_embed(interaction.guild.id, interaction.user.id, server=self.server)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Sair da call", style=discord.ButtonStyle.secondary, emoji="📤", row=2)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._leave_from_panel(interaction)


class TTSTogglePanelView(_BaseTTSView):
    @discord.ui.button(label="Bloqueio por outro bot", style=discord.ButtonStyle.secondary, emoji="🤖", row=0)
    async def block_voice_bot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Bloqueio por outro bot", "Escolha se o bot deve sair ou bloquear quando o outro bot de voz entrar na call.", ToggleSelect(self.cog, "block_voice_bot")).send(interaction)

    @discord.ui.button(label="Modo Cuca", style=discord.ButtonStyle.secondary, emoji="👑", row=0)
    async def only_target_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self.owner_id, self.guild_id, "Modo Cuca", "Quando ativado, a Cuca continua normal e os outros usuários são forçados para gtts.", ToggleSelect(self.cog, "only_target_user")).send(interaction)

    @discord.ui.button(label="Atualizar painel", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await self.cog._build_settings_embed(interaction.guild.id, interaction.user.id, server=False)
        await interaction.response.edit_message(embed=embed, view=self)


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


def validate_mode(mode: str) -> str:
    return "edge" if str(mode or "").strip().lower() == "edge" else "gtts"


MODE_CHOICES = [
    app_commands.Choice(name="gtts — mais simples e compatível", value="gtts"),
    app_commands.Choice(name="edge — voz natural com voice, speed e pitch", value="edge"),
]

SPEED_CHOICES = [
    app_commands.Choice(name="-50% — bem mais devagar", value="-50%"),
    app_commands.Choice(name="-25% — mais devagar", value="-25%"),
    app_commands.Choice(name="+0% — normal", value="+0%"),
    app_commands.Choice(name="+25% — mais rápido", value="+25%"),
    app_commands.Choice(name="+50% — bem mais rápido", value="+50%"),
]

PITCH_CHOICES = [
    app_commands.Choice(name="-50Hz — mais grave", value="-50Hz"),
    app_commands.Choice(name="-25Hz — levemente grave", value="-25Hz"),
    app_commands.Choice(name="+0Hz — normal", value="+0Hz"),
    app_commands.Choice(name="+25Hz — levemente agudo", value="+25Hz"),
    app_commands.Choice(name="+50Hz — mais agudo", value="+50Hz"),
]


class TTSVoice(TTSAudioMixin, commands.GroupCog, group_name="tts", group_description="Comandos de texto para fala"):
    server = app_commands.Group(name="server", description="Configurações padrão do servidor")
    voices = app_commands.Group(name="voices", description="Listas de vozes e idiomas")
    toggle = app_commands.Group(name="toggle", description="Atalhos e modos especiais")

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
        return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ):
        if interaction.response.is_done():
            await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
            )
        else:
            await interaction.response.send_message(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
            )

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _require_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Comando indisponível", "Esse comando só pode ser usado dentro de um servidor.", ok=False), ephemeral=True)
        return False

    async def _require_manage_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.", ok=False), ephemeral=True)
        return False

    async def _require_kick_members(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.kick_members:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

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

    async def _only_target_user_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return bool((data or {}).get("only_target_user", False))
        except Exception as e:
            print(f"[tts_voice] Erro ao ler only_target_user da guild {guild_id}: {e}")
            return False

    async def _block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return bool((data or {}).get("block_voice_bot", False))
        except Exception as e:
            print(f"[tts_voice] Erro ao ler block_voice_bot da guild {guild_id}: {e}")
            return False

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

    def _chunk_lines(self, lines: list[str], max_chars: int = 3500) -> list[str]:
        chunks, current, size = [], [], 0
        for line in lines:
            extra = len(line) + 1
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current, size = [line], extra
            else:
                current.append(line)
                size += extra
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _send_list_embeds(self, interaction: discord.Interaction, *, title: str, lines: list[str], footer: str):
        chunks = self._chunk_lines(lines)
        if not chunks:
            await self._respond(interaction, embed=self._make_embed(title, "Nenhum item encontrado.", ok=False), ephemeral=True)
            return
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})", description=f"```{chunk}```", color=discord.Color.blurple())
            embed.set_footer(text=footer)
            await self._respond(interaction, embed=embed, ephemeral=True)

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

        only_target_enabled = await self._only_target_user_enabled(message.guild.id)
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        forced_gtts = False
        if only_target_enabled and target_user_id and message.author.id != target_user_id:
            resolved["engine"] = "gtts"
            resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
            resolved["voice"] = ""
            resolved["rate"] = "+0%"
            resolved["pitch"] = "+0Hz"
            forced_gtts = True

        text = message.content[1:].strip()
        if not text:
            print("[tts_voice] ignorado | texto vazio após prefixo")
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)
        await state.queue.put(QueueItem(guild_id=message.guild.id, channel_id=voice_channel.id, author_id=message.author.id, text=text, engine=resolved["engine"], voice=resolved["voice"], language=resolved["language"], rate=resolved["rate"], pitch=resolved["pitch"]))
        print(f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} user={message.author.id} msg_channel={getattr(message.channel, 'id', None)} canal_voz={voice_channel.id} engine={resolved['engine']} forced_gtts={forced_gtts} texto={text!r}")
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if await self._block_voice_bot_enabled(guild.id) and self._target_voice_bot_in_channel(vc.channel):
            print(f"[tts_voice] Bot de voz alvo detectado na call | guild={guild.id} channel={vc.channel.id} target_bot_id={self._target_voice_bot_id()}")
            await self._disconnect_and_clear(guild)
            return
        await self._disconnect_if_alone_or_only_bots(guild)


    async def voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()
        voices = self.edge_voice_cache or sorted(self.edge_voice_names)

        results: list[app_commands.Choice[str]] = []
        for voice in voices:
            if current and current not in voice.lower():
                continue
            results.append(app_commands.Choice(name=voice[:100], value=voice))
            if len(results) >= 25:
                break
        return results

    async def language_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()

        results: list[app_commands.Choice[str]] = []
        for code, name in sorted(self.gtts_languages.items()):
            label = f"{code} — {name}"
            haystack = f"{code} {name}".lower()
            if current and current not in haystack:
                continue
            results.append(app_commands.Choice(name=label[:100], value=code))
            if len(results) >= 25:
                break
        return results


    async def _set_mode_common(self, interaction: discord.Interaction, *, mode: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = validate_mode(mode)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            title, desc = "Modo padrão atualizado", f"O modo padrão do servidor agora é `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, engine=value))
            title, desc = "Modo atualizado", f"O seu modo de TTS agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_voice_common(self, interaction: discord.Interaction, *, voice: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge. Use `/tts voices edge` para ver as opções.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            title, desc = "Voz padrão atualizada", f"A voz padrão do servidor agora é `{voice}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice))
            title, desc = "Voz atualizada", f"A sua voz do Edge agora é `{voice}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_language_common(self, interaction: discord.Interaction, *, language: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = str(language or "").strip().lower()
        if value not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse idioma não foi encontrado na lista do gTTS. Use `/tts voices gtts` para ver as opções.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=value))
            title, desc = "Idioma padrão atualizado", f"O idioma padrão do servidor agora é `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=value))
            title, desc = "Idioma atualizado", f"O seu idioma do gTTS agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_speed_common(self, interaction: discord.Interaction, *, speed: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(interaction.guild.id))
        user_settings = await self._maybe_await(db.get_user_tts(interaction.guild.id, interaction.user.id))
        current_mode = (user_settings.get("engine") or guild_defaults.get("engine") or "gtts")
        if current_mode != "edge":
            await self._respond(interaction, embed=self._make_embed("Modo incompatível", "Esse ajuste só funciona no modo `edge`. Use `/tts mode` para trocar o seu modo.", ok=False), ephemeral=True)
            return
        value = self._normalize_rate_value(speed)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title, desc = "Velocidade padrão atualizada", f"A velocidade padrão do servidor agora é `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value))
            title, desc = "Velocidade atualizada", f"A sua velocidade agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(interaction.guild.id))
        user_settings = await self._maybe_await(db.get_user_tts(interaction.guild.id, interaction.user.id))
        current_mode = (user_settings.get("engine") or guild_defaults.get("engine") or "gtts")
        if current_mode != "edge":
            await self._respond(interaction, embed=self._make_embed("Modo incompatível", "Esse ajuste só funciona no modo `edge`. Use `/tts mode` para trocar o seu modo.", ok=False), ephemeral=True)
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title, desc = "Tom padrão atualizado", f"O tom padrão do servidor agora é `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value))
            title, desc = "Tom atualizado", f"O seu tom agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)


    @app_commands.command(name="menu", description="Abre um painel guiado para configurar o seu TTS")
    async def menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        embed = await self._build_settings_embed(interaction.guild.id, interaction.user.id, server=False)
        await self._respond(interaction, embed=embed, view=TTSMainPanelView(self, interaction.user.id, interaction.guild.id, server=False), ephemeral=True)


    async def _build_settings_embed(self, guild_id: int, user_id: int, *, server: bool = False) -> discord.Embed:
        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}

        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        only_target_user = bool(guild_defaults.get("only_target_user", False))
        forced_note = "Não"
        if only_target_user and target_user_id and user_id != target_user_id:
            forced_note = "Sim, quando mandar `,`"

        title = "Painel de TTS do servidor" if server else "Painel de TTS"
        description = (
            "Use os botões abaixo para alterar os padrões do servidor."
            if server else
            "Use os botões abaixo para alterar as suas configurações."
        )

        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="Modo ativo", value=f"`{resolved.get('engine', 'gtts')}`", inline=True)
        embed.add_field(name="Voz Edge ativa", value=f"`{resolved.get('voice', 'Não definido')}`", inline=True)
        embed.add_field(name="Idioma gTTS ativo", value=f"`{resolved.get('language', 'Não definido')}`", inline=True)
        embed.add_field(name="Velocidade ativa", value=f"`{resolved.get('rate', '+0%')}`", inline=True)
        embed.add_field(name="Tom ativo", value=f"`{resolved.get('pitch', '+0Hz')}`", inline=True)
        embed.add_field(
            name="Bloqueio por outro bot",
            value="`Ativado`" if bool(guild_defaults.get("block_voice_bot", False)) else "`Desativado`",
            inline=True,
        )
        embed.add_field(
            name="Modo Cuca",
            value="`Ativado`" if only_target_user else "`Desativado`",
            inline=True,
        )
        embed.add_field(
            name="Usuário alvo",
            value=f"`{target_user_id}`" if target_user_id else "`Não configurado`",
            inline=True,
        )
        embed.add_field(
            name="Forçado para gtts?",
            value=f"`{forced_note}`",
            inline=True,
        )
        embed.set_footer(text="As alterações feitas por menus também ficam salvas no banco.")
        return embed

    async def _apply_mode_from_panel(self, interaction: discord.Interaction, mode: str, *, server: bool):
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        value = validate_mode(mode)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            desc = f"O modo padrão do servidor agora é `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, engine=value))
            desc = f"O seu modo de TTS agora é `{value}`."

        await interaction.response.send_message(
            embed=self._make_embed("Modo atualizado", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_voice_from_panel(self, interaction: discord.Interaction, voice: str, *, server: bool):
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if voice not in self.edge_voice_names and voice not in self.edge_voice_cache:
            await interaction.response.send_message(
                embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            desc = f"A voz padrão do servidor agora é `{voice}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice))
            desc = f"A sua voz do Edge agora é `{voice}`."

        await interaction.response.send_message(
            embed=self._make_embed("Voz atualizada", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool):
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
            desc = f"O idioma padrão do servidor agora é `{language}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=language))
            desc = f"O seu idioma do gtts agora é `{language}`."

        await interaction.response.send_message(
            embed=self._make_embed("Idioma atualizado", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool):
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=speed))
            desc = f"A velocidade padrão do servidor agora é `{speed}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, rate=speed))
            desc = f"A sua velocidade agora é `{speed}`."

        await interaction.response.send_message(
            embed=self._make_embed("Velocidade atualizada", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool):
        if server and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch))
            desc = f"O tom padrão do servidor agora é `{pitch}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=pitch))
            desc = f"O seu tom agora é `{pitch}`."

        await interaction.response.send_message(
            embed=self._make_embed("Tom atualizado", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_only_target_from_panel(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, only_target_user=bool(enabled)))
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)

        if enabled:
            desc = "Só a Cuca pode falar nesse caralho.\n\n" + f"Todo mundo que não for o ID `{target_user_id}` será forçado para `gtts`."
        else:
            desc = "Agora os betinhas podem usar também.\n\nTodo mundo voltou a usar as próprias configurações."

        await interaction.response.send_message(
            embed=self._make_embed("Modo Cuca atualizado", desc, ok=True),
            ephemeral=True,
        )

    async def _apply_block_voice_bot_from_panel(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
        await interaction.response.send_message(
            embed=self._make_embed("Bloqueio atualizado", f"O bloqueio por outro bot de voz agora está em `{enabled}`.", ok=True),
            ephemeral=True,
        )

        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

    async def _leave_from_panel(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz agora.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False),
                ephemeral=True,
            )
            return

        await self._disconnect_and_clear(interaction.guild)
        await interaction.response.send_message(
            embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True),
            ephemeral=True,
        )


    @app_commands.command(name="settings", description="Mostra as suas configurações atuais de TTS")
    async def settings(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        try:
            guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(interaction.guild.id))
            user_settings = await self._maybe_await(db.get_user_tts(interaction.guild.id, interaction.user.id))
            resolved = await self._maybe_await(db.resolve_tts(interaction.guild.id, interaction.user.id))
        except Exception as e:
            await self._respond(interaction, embed=self._make_embed("Erro ao carregar configurações", f"Não consegui ler as configurações de TTS agora.\n\nErro: `{e}`", ok=False), ephemeral=True)
            return
        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}
        only_target_user = bool(guild_defaults.get("only_target_user", False))
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        forced_note = "Não"
        if only_target_user and target_user_id and interaction.user.id != target_user_id:
            forced_note = "Sim, quando você mandar `,`"
        embed = discord.Embed(title="Configurações de TTS", description="Resumo das suas configurações atuais neste servidor.", color=discord.Color.blurple())
        embed.add_field(name="Modo ativo", value=f"`{resolved.get('engine', 'gtts')}`", inline=True)
        embed.add_field(name="Voz Edge ativa", value=f"`{resolved.get('voice', 'Não definido')}`", inline=True)
        embed.add_field(name="Idioma gTTS ativo", value=f"`{resolved.get('language', 'Não definido')}`", inline=True)
        embed.add_field(name="Velocidade ativa", value=f"`{resolved.get('rate', '+0%')}`", inline=True)
        embed.add_field(name="Tom ativo", value=f"`{resolved.get('pitch', '+0Hz')}`", inline=True)
        embed.add_field(name="Bloqueio por outro bot", value="`Ativado`" if bool(guild_defaults.get("block_voice_bot", False)) else "`Desativado`", inline=True)
        embed.add_field(name="Modo Cuca", value="`Ativado`" if only_target_user else "`Desativado`", inline=True)
        embed.add_field(name="Usuário alvo", value=f"`{target_user_id}`" if target_user_id else "`Não configurado`", inline=True)
        embed.add_field(name="Você será forçado para gTTS?", value=f"`{forced_note}`", inline=True)
        embed.add_field(name="Suas configurações salvas", value=(f"Modo: `{user_settings.get('engine', '—')}`\n" f"Voz: `{user_settings.get('voice', '—')}`\n" f"Idioma: `{user_settings.get('language', '—')}`\n" f"Velocidade: `{user_settings.get('rate', '—')}`\n" f"Tom: `{user_settings.get('pitch', '—')}`"), inline=False)
        embed.add_field(name="Padrões do servidor", value=(f"Modo: `{guild_defaults.get('engine', '—')}`\n" f"Voz: `{guild_defaults.get('voice', '—')}`\n" f"Idioma: `{guild_defaults.get('language', '—')}`\n" f"Velocidade: `{guild_defaults.get('rate', '—')}`\n" f"Tom: `{guild_defaults.get('pitch', '—')}`"), inline=False)
        await self._respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="mode", description="Define como a sua fala vai funcionar")
    @app_commands.describe(mode="gtts: simples e compatível | edge: voz natural com voice, speed e pitch")
    @app_commands.choices(mode=MODE_CHOICES)
    async def mode(self, interaction: discord.Interaction, mode: str):
        await self._set_mode_common(interaction, mode=mode, server=False)

    @app_commands.command(name="voice", description="Escolhe a sua voz do modo edge")
    @app_commands.describe(voice="Escolha uma voz do Edge pela lista")
    @app_commands.autocomplete(voice=voice_autocomplete)
    async def voice(self, interaction: discord.Interaction, voice: str):
        await self._set_voice_common(interaction, voice=voice, server=False)

    @app_commands.command(name="language", description="Escolhe o seu idioma do modo gtts")
    @app_commands.describe(language="Escolha um idioma do gTTS pela lista")
    @app_commands.autocomplete(language=language_autocomplete)
    async def language(self, interaction: discord.Interaction, language: str):
        await self._set_language_common(interaction, language=language, server=False)

    @app_commands.command(name="speed", description="Define a velocidade da sua voz no modo edge")
    @app_commands.describe(speed="Escolha uma velocidade pronta")
    @app_commands.choices(speed=SPEED_CHOICES)
    async def speed(self, interaction: discord.Interaction, speed: str):
        await self._set_speed_common(interaction, speed=speed, server=False)

    @app_commands.command(name="pitch", description="Define o tom da sua voz no modo edge")
    @app_commands.describe(pitch="Escolha um tom pronto")
    @app_commands.choices(pitch=PITCH_CHOICES)
    async def pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(name="leave", description="Faz o bot sair da call e limpa a fila")
    async def leave(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._respond(interaction, embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz agora.", ok=False), ephemeral=True)
            return
        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await self._respond(interaction, embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse comando.", ok=False), ephemeral=True)
            return
        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False), ephemeral=True)
            return
        await self._disconnect_and_clear(interaction.guild)
        await self._respond(interaction, embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True), ephemeral=True)

    
    @server.command(name="menu", description="Abre um painel guiado para configurar o TTS do servidor")
    async def server_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return

        embed = await self._build_settings_embed(interaction.guild.id, interaction.user.id, server=True)
        await self._respond(
            interaction,
            embed=embed,
            view=TTSMainPanelView(self, interaction.user.id, interaction.guild.id, server=True),
            ephemeral=True,
        )

@server.command(name="mode", description="Define o modo padrão de fala do servidor")
    @app_commands.describe(mode="gtts: simples e compatível | edge: voz natural com voice, speed e pitch")
    @app_commands.choices(mode=MODE_CHOICES)
    async def server_mode(self, interaction: discord.Interaction, mode: str):
        await self._set_mode_common(interaction, mode=mode, server=True)

    @server.command(name="voice", description="Escolhe a voz padrão do modo edge no servidor")
    @app_commands.describe(voice="Escolha uma voz do Edge pela lista")
    @app_commands.autocomplete(voice=voice_autocomplete)
    async def server_voice(self, interaction: discord.Interaction, voice: str):
        await self._set_voice_common(interaction, voice=voice, server=True)

    @server.command(name="language", description="Escolhe o idioma padrão do modo gtts no servidor")
    @app_commands.describe(language="Escolha um idioma do gTTS pela lista")
    @app_commands.autocomplete(language=language_autocomplete)
    async def server_language(self, interaction: discord.Interaction, language: str):
        await self._set_language_common(interaction, language=language, server=True)

    @server.command(name="speed", description="Define a velocidade padrão da voz no modo edge")
    @app_commands.describe(speed="Escolha uma velocidade pronta")
    @app_commands.choices(speed=SPEED_CHOICES)
    async def server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_speed_common(interaction, speed=speed, server=True)

    @server.command(name="pitch", description="Define o tom padrão da voz no modo edge")
    @app_commands.describe(pitch="Escolha um tom pronto")
    @app_commands.choices(pitch=PITCH_CHOICES)
    async def server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    @voices.command(name="edge", description="Mostra a lista de vozes disponíveis do modo edge")
    async def voices_edge(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        lines = self.edge_voice_cache or ["Nenhuma voz Edge carregada."]
        await self._send_list_embeds(interaction, title="Vozes do Edge", lines=lines, footer="Use `/tts voice` ou `/tts server voice` para escolher uma voz.")

    @voices.command(name="gtts", description="Mostra a lista de idiomas disponíveis do modo gtts")
    async def voices_gtts(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        lines = [f"{code} - {name}" for code, name in sorted(self.gtts_languages.items())]
        await self._send_list_embeds(interaction, title="Idiomas do gTTS", lines=lines, footer="Use `/tts language` ou `/tts server language` para escolher um idioma.")


    @toggle.command(name="menu", description="Abre um painel guiado para os toggles de TTS")
    async def toggle_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        embed = await self._build_settings_embed(interaction.guild.id, interaction.user.id, server=False)
        await self._respond(interaction, embed=embed, view=TTSTogglePanelView(self, interaction.user.id, interaction.guild.id), ephemeral=True)

    @toggle.command(name="block_voice_bot", description="Liga ou desliga o bloqueio quando o outro bot de voz entrar na call")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def toggle_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
        await self._respond(interaction, embed=self._make_embed("Bloqueio atualizado", f"O bloqueio por outro bot de voz agora está em `{enabled}`.", ok=True), ephemeral=True)
        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

    @toggle.command(name="only_target_user", description="Liga ou desliga o modo Cuca para forçar gtts nos outros usuários")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def toggle_only_target_user(self, interaction: discord.Interaction, enabled: bool):
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, only_target_user=bool(enabled)))
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        if enabled:
            desc = "Só a Cuca pode falar nesse caralho.\n\n" + f"Todo mundo que não for o ID `{target_user_id}` será forçado para `gtts`."
        else:
            desc = "Agora os betinhas podem usar também.\n\nTodo mundo voltou a usar as próprias configurações."
        await self._respond(interaction, embed=self._make_embed("Modo Cuca atualizado", desc, ok=True), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
