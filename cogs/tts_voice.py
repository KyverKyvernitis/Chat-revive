import os
import re
import asyncio
import tempfile
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands

import edge_tts
from gtts import gTTS

from config import TTS_ENABLED, BLOCK_VOICE_BOT_ID

RATE_RE = re.compile(r"^[+-]?\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]?\d{1,4}Hz$")


class TtsVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}

        self._voices_cache: Optional[List[dict]] = None
        self._voices_cache_lock = asyncio.Lock()

        self._seen_messages: set[int] = set()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

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

    async def _synthesize_edge(self, text: str, out_path: str, *, voice: str, rate: str, pitch: str):
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume="+0%",
        )
        await communicate.save(out_path)

    async def _synthesize_gtts(self, text: str, out_path: str):
        tts = gTTS(text=text, lang="pt", tld="com.br")
        tts.save(out_path)

    async def _speak_from_message(self, message: discord.Message, text: str):
        if not TTS_ENABLED:
            await self._reply_temp_error(message, "❌ O TTS está desativado no momento.")
            return

        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)
        if not vs or not vs.channel or
