from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

import discord
import edge_tts
from gtts import gTTS

from tts_helpers import GTTS_LANG, validate_pitch, validate_rate, validate_voice


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


class GuildTTSState:
    def __init__(self):
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self.last_text_channel_id: Optional[int] = None


class TTSAudioMixin:
    guild_states: dict[int, GuildTTSState]
    edge_voice_names: set[str]
    bot: discord.Client

    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState()
            self.guild_states[guild_id] = state
        return state

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        voice = validate_voice(voice, self.edge_voice_names)
        rate = validate_rate(rate)
        pitch = validate_pitch(pitch)

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

    def _ensure_worker(self, guild_id: int):
        state = self._get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self._worker_loop(guild_id))

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
