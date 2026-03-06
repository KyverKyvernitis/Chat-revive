import os
import re
import asyncio
import tempfile
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands

import edge_tts

from config import TTS_ENABLED, BLOCK_VOICE_BOT_ID

RATE_RE = re.compile(r"^[+-]?\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]?\d{1,4}Hz$")


class TtsVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}

        # cache de vozes
        self._voices_cache: Optional[List[dict]] = None
        self._voices_cache_lock = asyncio.Lock()

        # 🔴 NOVO: evita responder duas vezes
        self._seen_messages: set[int] = set()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    # 🔴 NOVO: sistema anti-duplicação
    async def _mark_seen(self, message_id: int, ttl: int = 10) -> bool:
        if message_id in self._seen_messages:
            return True

        self._seen_messages.add(message_id)

        async def cleanup():
            await asyncio.sleep(ttl)
            self._seen_messages.discard(message_id)

        asyncio.create_task(cleanup())
        return False

    async def _ensure_voices_cache(self):
        if self._voices_cache is not None:
            return

        async with self._voices_cache_lock:
            if self._voices_cache is None:
                self._voices_cache = await edge_tts.list_voices()

    async def _reply_temp_error(self, message: discord.Message, content: str, delay: int = 7):
        try:
            bot_msg = await message.reply(content)
        except Exception:
            return

        async def cleanup():
            await asyncio.sleep(delay)

            try:
                await bot_msg.delete()
            except Exception:
                pass

            try:
                await message.delete()
            except Exception:
                pass

        asyncio.create_task(cleanup())

    async def _speak_from_message(self, message: discord.Message, text: str):

        if not TTS_ENABLED:
            await self._reply_temp_error(message, "❌ O TTS está desativado no momento.")
            return

        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)

        if not vs or not vs.channel or not isinstance(vs.channel, discord.VoiceChannel):
            await self._reply_temp_error(message, "⚠️ Você precisa estar em um canal de voz para eu falar.")
            return

        channel: discord.VoiceChannel = vs.channel

        if BLOCK_VOICE_BOT_ID and any(m.id == BLOCK_VOICE_BOT_ID for m in channel.members):
            await self._reply_temp_error(message, "❌ Já existe um bot de voz nesta call")
            return

        me = message.guild.me or message.guild.get_member(self.bot.user.id)

        perms = channel.permissions_for(me)

        if not perms.connect:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Conectar** nesse canal de voz.")
            return

        if not perms.speak:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Falar** nesse canal de voz.")
            return

        vc = message.guild.voice_client

        try:

            if vc is None:
                vc = await channel.connect()

            elif vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)

        except Exception as e:

            await self._reply_temp_error(
                message,
                f"❌ Não consegui entrar na call. Erro: `{type(e).__name__}` — `{e}`"
            )
            return

        text = (text or "").strip()

        if not text:
            await self._reply_temp_error(message, "⚠️ Escreva algo depois da vírgula. Ex: `,olá`")
            return

        if len(text) > 250:
            text = text[:250]

        cfg = self.bot.settings_db.resolve_tts(message.guild.id, message.author.id)

        voice = cfg["voice"]
        rate = cfg["rate"]
        pitch = cfg["pitch"]

        lock = self._lock(message.guild.id)

        async with lock:

            if vc.is_playing():
                vc.stop()

            tmp = None

            try:

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                    tmp = fp.name

                communicate = edge_tts.Communicate(
                    text=text,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                    volume="+0%",
                )

                await communicate.save(tmp)

                vc.play(discord.FFmpegPCMAudio(tmp))

                while vc.is_playing():
                    await asyncio.sleep(0.2)

            except Exception as e:

                await self._reply_temp_error(
                    message,
                    f"❌ Falha no TTS: `{type(e).__name__}` — `{e}`"
                )

            finally:

                if tmp:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        if message.author.bot or not message.guild:
            return

        # 🔴 NOVO: evita duplicação
        if await self._mark_seen(message.id):
            return

        if not message.content.startswith(","):
            return

        text = message.content[1:]

        await self._speak_from_message(message, text)

    # auto leave

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):

        vc = member.guild.voice_client

        if vc is None or vc.channel is None:
            return

        humans = [m for m in vc.channel.members if not m.bot]

        if len(humans) == 0:

            try:

                if vc.is_playing():
                    vc.stop()

                await vc.disconnect()

            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TtsVoiceCog(bot))
