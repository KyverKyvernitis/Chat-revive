from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import edge_tts
from gtts import gTTS

from config import (
    BLOCK_VOICE_BOT_ID,
    OFF_COLOR,
    ON_COLOR,
    TTS_ENABLED,
)


EDGE_DEFAULT_VOICE = "pt-BR-FranciscaNeural"
EDGE_FALLBACK_VOICE = "pt-BR-AntonioNeural"
GTTS_LANG = "pt-br"

RATE_RE = re.compile(r"^[+-]\d+%$")
PITCH_RE = re.compile(r"^[+-]\d+Hz$")


@dataclass
class QueueItem:
    guild_id: int
    channel_id: int
    author_id: int
    text: str
    engine: str
    voice: str
    rate: str
    pitch: str


def _clean_text(text: str) -> str:
    text = re.sub(r"<a?:\w+:\d+>", "", text)
    text = re.sub(r"<@!?\d+>", "usuário", text)
    text = re.sub(r"<@&\d+>", "cargo", text)
    text = re.sub(r"<#\d+>", "canal", text)
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:350]


class GuildTTSState:
    def __init__(self):
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self.last_text_channel_id: Optional[int] = None


class TTSVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_names: set[str] = set()
        self.edge_voice_cache: list[str] = []

    async def cog_load(self):
        await self._load_edge_voices()

    async def cog_unload(self):
        for state in self.guild_states.values():
            if state.worker_task and not state.worker_task.done():
                state.worker_task.cancel()

    async def _load_edge_voices(self):
        try:
            voices = await edge_tts.list_voices()
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE, EDGE_FALLBACK_VOICE]
            self.edge_voice_names = set(self.edge_voice_cache)
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")

    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState()
            self.guild_states[guild_id] = state
        return state

    def _make_embed(self, title: str, description: str, ok: bool = True) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=ON_COLOR if ok else OFF_COLOR,
        )

    def _validate_engine(self, engine: str) -> str:
        engine = engine.strip().lower()
        return engine if engine in ("gtts", "edge") else "gtts"

    def _validate_rate(self, value: str) -> str:
        value = value.strip()
        return value if RATE_RE.fullmatch(value) else "+0%"

    def _validate_pitch(self, value: str) -> str:
        value = value.strip()
        return value if PITCH_RE.fullmatch(value) else "+0Hz"

    def _validate_voice(self, voice: str) -> str:
        voice = voice.strip()
        if not voice:
            return EDGE_DEFAULT_VOICE
        if voice in self.edge_voice_names:
            return voice
        return EDGE_DEFAULT_VOICE

    def _is_voice_bot_blocking(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        if not BLOCK_VOICE_BOT_ID:
            return False

        member = guild.get_member(BLOCK_VOICE_BOT_ID)
        return bool(member and member.voice and member.voice.channel and member.voice.channel.id == voice_channel.id)

    async def _should_block_for_voice_bot(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        if not BLOCK_VOICE_BOT_ID:
            return False

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return False

        enabled = db.block_voice_bot_enabled(guild.id)
        if not enabled:
            return False

        return self._is_voice_bot_blocking(guild, voice_channel)

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.channel:
            return

        channel = vc.channel
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return

        blocked = await self._should_block_for_voice_bot(guild, channel)
        if blocked:
            state = self._get_state(guild.id)
            while not state.queue.empty():
                try:
                    state.queue.get_nowait()
                    state.queue.task_done()
                except Exception:
                    break

            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass

            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    async def _disconnect_and_clear(self, guild: discord.Guild):
        vc = guild.voice_client
        state = self._get_state(guild.id)

        while not state.queue.empty():
            try:
                state.queue.get_nowait()
                state.queue.task_done()
            except Exception:
                break

        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass

            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    async def _ensure_connected(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> discord.VoiceClient | None:
        vc = guild.voice_client

        if vc and vc.channel and vc.channel.id == voice_channel.id:
            return vc

        if vc and vc.channel and vc.channel.id != voice_channel.id:
            try:
                await vc.move_to(voice_channel)
                return vc
            except Exception:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass

        try:
            return await voice_channel.connect(self_deaf=True)
        except Exception as e:
            print(f"[tts_voice] Erro ao conectar na call da guild {guild.id}: {e}")
            return None

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        voice = self._validate_voice(voice)
        rate = self._validate_rate(rate)
        pitch = self._validate_pitch(pitch)

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(path)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_gtts_file(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        def _save():
            tts = gTTS(text=text, lang=GTTS_LANG)
            tts.save(path)

        try:
            await asyncio.to_thread(_save)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "edge":
            try:
                return await self._generate_edge_file(item.text, item.voice, item.rate, item.pitch)
            except Exception as e:
                print(f"[tts_voice] Edge falhou, usando gTTS. Guild {item.guild_id}: {e}")

        return await self._generate_gtts_file(item.text)

    async def _play_file(self, vc: discord.VoiceClient, file_path: str):
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def after_playing(error: Optional[Exception]):
            if error:
                loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                loop.call_soon_threadsafe(finished.set_result, True)

        source = discord.FFmpegPCMAudio(file_path)
        vc.play(source, after=after_playing)
        await finished

    async def _worker_loop(self, guild_id: int):
        state = self._get_state(guild_id)

        while True:
            item = await state.queue.get()
            file_path = None

            try:
                guild = self.bot.get_guild(item.guild_id)
                if guild is None:
                    continue

                voice_channel = guild.get_channel(item.channel_id)
                if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
                    continue

                blocked = await self._should_block_for_voice_bot(guild, voice_channel)
                if blocked:
                    await self._disconnect_if_blocked(guild)
                    continue

                vc = await self._ensure_connected(guild, voice_channel)
                if vc is None:
                    continue

                file_path = await self._generate_audio_file(item)
                await self._play_file(vc, file_path)

            except Exception as e:
                print(f"[tts_voice] Worker error guild {guild_id}: {e}")

            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                state.queue.task_done()

    def _ensure_worker(self, guild_id: int):
        state = self._get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self._worker_loop(guild_id))

    async def _enqueue_message(
        self,
        message: discord.Message,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ):
        if not message.guild:
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return

        text = _clean_text(message.content)
        if not text:
            return

        resolved = db.resolve_tts(message.guild.id, message.author.id)
        item = QueueItem(
            guild_id=message.guild.id,
            channel_id=voice_channel.id,
            author_id=message.author.id,
            text=text,
            engine=self._validate_engine(resolved.get("engine", "gtts")),
            voice=self._validate_voice(resolved.get("voice", EDGE_DEFAULT_VOICE)),
            rate=self._validate_rate(resolved.get("rate", "+0%")),
            pitch=self._validate_pitch(resolved.get("pitch", "+0Hz")),
        )

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = message.channel.id
        await state.queue.put(item)
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not TTS_ENABLED:
            return

        if message.author.bot or not message.guild:
            return

        if not message.content or "," not in message.content:
            return

        author_voice = getattr(message.author, "voice", None)
        if not author_voice or not author_voice.channel:
            return

        voice_channel = author_voice.channel
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return

        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            await self._disconnect_if_blocked(message.guild)
            return

        await self._enqueue_message(message, voice_channel)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not member.guild:
            return

        if not BLOCK_VOICE_BOT_ID:
            return

        if member.id != BLOCK_VOICE_BOT_ID:
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None or not db.block_voice_bot_enabled(member.guild.id):
            return

        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return

        current_channel = vc.channel
        if not isinstance(current_channel, (discord.VoiceChannel, discord.StageChannel)):
            return

        joined_same_channel = after.channel and after.channel.id == current_channel.id
        moved_to_same_channel = (
            before.channel != after.channel
            and after.channel
            and after.channel.id == current_channel.id
        )

        if joined_same_channel or moved_to_same_channel:
            await self._disconnect_if_blocked(member.guild)

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

        embed = self._make_embed("Status do TTS", desc, ok=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        embed = self._make_embed("Vozes disponíveis", desc, ok=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        engine = self._validate_engine(engine)
        await db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)

        embed = self._make_embed(
            "Engine atualizada",
            f"Sua engine de TTS agora é `{engine}`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        engine = self._validate_engine(engine)
        await db.set_guild_tts_defaults(interaction.guild.id, engine=engine)

        embed = self._make_embed(
            "Engine padrão atualizada",
            f"A engine padrão do servidor agora é `{engine}`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        if voice.strip() not in self.edge_voice_names:
            embed = self._make_embed(
                "Voz inválida",
                "Essa voz não existe na lista do Edge TTS. Use `/voices` para ver opções válidas.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice.strip())

        embed = self._make_embed(
            "Voz atualizada",
            f"Sua voz foi definida para `{voice.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        if voice.strip() not in self.edge_voice_names:
            embed = self._make_embed(
                "Voz inválida",
                "Essa voz não existe na lista do Edge TTS. Use `/voices` para ver opções válidas.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_guild_tts_defaults(interaction.guild.id, voice=voice.strip())

        embed = self._make_embed(
            "Voz padrão atualizada",
            f"A voz padrão do servidor foi definida para `{voice.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_rate", description="Define sua velocidade do Edge TTS")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    async def set_rate(self, interaction: discord.Interaction, rate: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not RATE_RE.fullmatch(rate.strip()):
            embed = self._make_embed(
                "Velocidade inválida",
                "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=rate.strip())

        embed = self._make_embed(
            "Velocidade atualizada",
            f"Sua velocidade foi definida para `{rate.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_speed", description="Alias de /set_rate")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not RATE_RE.fullmatch(speed.strip()):
            embed = self._make_embed(
                "Velocidade inválida",
                "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=speed.strip())

        embed = self._make_embed(
            "Velocidade atualizada",
            f"Sua velocidade foi definida para `{speed.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_server_rate", description="Define a velocidade padrão do servidor")
    @app_commands.describe(rate="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
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

        if not RATE_RE.fullmatch(rate.strip()):
            embed = self._make_embed(
                "Velocidade inválida",
                "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_guild_tts_defaults(interaction.guild.id, rate=rate.strip())

        embed = self._make_embed(
            "Velocidade padrão atualizada",
            f"A velocidade padrão do servidor foi definida para `{rate.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_server_speed", description="Alias de /set_server_rate")
    @app_commands.describe(speed="Formato: +0%, +25%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
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

        if not RATE_RE.fullmatch(speed.strip()):
            embed = self._make_embed(
                "Velocidade inválida",
                "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_guild_tts_defaults(interaction.guild.id, rate=speed.strip())

        embed = self._make_embed(
            "Velocidade padrão atualizada",
            f"A velocidade padrão do servidor foi definida para `{speed.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_pitch", description="Define seu tom do Edge TTS")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
            return

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
            return

        if not PITCH_RE.fullmatch(pitch.strip()):
            embed = self._make_embed(
                "Tom inválido",
                "Use o formato `+0Hz`, `+20Hz` ou `-10Hz`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=pitch.strip())

        embed = self._make_embed(
            "Tom atualizado",
            f"Seu tom foi definido para `{pitch.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_server_pitch", description="Define o tom padrão do servidor")
    @app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
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

        if not PITCH_RE.fullmatch(pitch.strip()):
            embed = self._make_embed(
                "Tom inválido",
                "Use o formato `+0Hz`, `+20Hz` ou `-10Hz`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch.strip())

        embed = self._make_embed(
            "Tom padrão atualizado",
            f"O tom padrão do servidor foi definido para `{pitch.strip()}`.\n\nEsse ajuste só funciona com engine `edge`.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        embed = self._make_embed(
            "Bloqueio atualizado",
            f"O bloqueio por outro bot de voz foi **{'ativado' if new_value else 'desativado'}**.",
            ok=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
