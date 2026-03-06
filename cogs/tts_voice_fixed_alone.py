import inspect
import re
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
        return {"pt-br":"Portuguese (Brazil)","pt":"Portuguese","en":"English","es":"Spanish","fr":"French"}

def validate_engine(engine: str) -> str:
    return "edge" if str(engine or "").strip().lower() == "edge" else "gtts"

class TTSVoice(TTSAudioMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_cache = []
        self.edge_voice_names = set()
        self.gtts_languages = get_gtts_languages()

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

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _respond(self, interaction: discord.Interaction, *, content: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = True):
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("％","%").replace("−","-").replace("–","-").replace("—","-").replace(" ","")
        if value.endswith("%"): value = value[:-1]
        if not value: return None
        if value[0] not in "+-": value = f"+{value}"
        if not value[1:].isdigit(): return None
        return f"{value[0]}{value[1:]}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("−","-").replace("–","-").replace("—","-").replace(" ","")
        if value.lower().endswith("hz"): value = value[:-2]
        if not value: return None
        if value[0] not in "+-": value = f"+{value}"
        if not value[1:].isdigit(): return None
        return f"{value[0]}{value[1:]}Hz"

    def _target_voice_bot_id(self) -> Optional[int]:
        for name in ("VOICE_BOT_ID", "BLOCK_VOICE_BOT_ID"):
            value = getattr(config, name, None)
            if value:
                try: return int(value)
                except Exception: pass
        return None

    def _target_voice_bot_in_channel(self, voice_channel) -> bool:
        target_bot_id = self._target_voice_bot_id()
        if not target_bot_id or voice_channel is None:
            return False
        return any(member.id == target_bot_id for member in getattr(voice_channel, "members", []))

    async def _block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None: return False
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
                if vc.is_playing(): vc.stop()
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
        print(f"[tts_voice] on_message recebido | guild={getattr(message.guild,'id',None)} channel_type={type(message.channel).__name__} user={getattr(message.author,'id',None)} raw={message.content!r}")
        if not getattr(config, "TTS_ENABLED", True): return
        if message.author.bot or not message.guild or not message.content or not message.content.startswith(","): return
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
        await state.queue.put(QueueItem(guild_id=message.guild.id, channel_id=voice_channel.id, author_id=message.author.id, text=text, engine=resolved["engine"], voice=resolved["voice"], language=resolved["language"], rate=resolved["rate"], pitch=resolved["pitch"]))
        print(f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} user={message.author.id} msg_channel={getattr(message.channel,'id',None)} canal_voz={voice_channel.id} engine={resolved['engine']} texto={text!r}")
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

    @app_commands.command(name="leave", description="Faz o bot sair da call e limpa a fila de TTS")
    async def leave(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._respond(interaction, embed=self._make_embed("Nada para desconectar","O bot não está conectado em nenhum canal de voz.",ok=False))
            return
        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await self._respond(interaction, embed=self._make_embed("Entre em uma call","Você precisa estar em um canal de voz para usar esse comando.",ok=False))
            return
        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Canal diferente","Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.",ok=False))
            return
        await self._disconnect_and_clear(interaction.guild)
        await self._respond(interaction, embed=self._make_embed("Bot desconectado","Saí da call e limpei a fila de TTS.",ok=True))

    @app_commands.command(name="set_block_voice_bot", description="Ativa ou desativa o bloqueio quando o outro bot de voz estiver na call")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def set_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Sem permissão","Você precisa da permissão `Gerenciar Servidor`.",ok=False))
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
        await self._respond(interaction, embed=self._make_embed("Bloqueio atualizado",f"O bloqueio por outro bot de voz agora está em `{enabled}`.",ok=True))
        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
