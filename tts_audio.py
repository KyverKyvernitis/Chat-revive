import asyncio
import inspect
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

import discord
import edge_tts
from gtts import gTTS

import config
from tts_helpers import validate_voice

GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
TTS_IDLE_DISCONNECT_SECONDS = getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 180)

@dataclass
class QueueItem:
    guild_id: int
    channel_id: int
    author_id: int
    text: str
    engine: str
    voice: str
    language: str
    rate: str
    pitch: str

@dataclass
class GuildTTSState:
    queue: asyncio.Queue
    worker_task: Optional[asyncio.Task] = None
    last_text_channel_id: Optional[int] = None

class TTSAudioMixin:
    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState(queue=asyncio.Queue())
            self.guild_states[guild_id] = state
        return state

    def _ensure_worker(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self._worker_loop(guild_id))

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _normalize_edge_rate(self, raw: str) -> str:
        value = str(raw or "").strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return "+0%"
        if value[0] not in "+-":
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0%"
        return f"{sign}{number}%"

    def _normalize_edge_pitch(self, raw: str) -> str:
        value = str(raw or "").strip().replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.lower().endswith("hz"):
            value = value[:-2]
        if not value:
            return "+0Hz"
        if value[0] not in "+-":
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0Hz"
        return f"{sign}{number}Hz"

    async def _generate_gtts_file(self, text: str, language: str) -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower()
        print(f"[tts_voice] gTTS synth | language={language!r} text={text[:80]!r}")
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            gTTS(text=text, lang=language).save(path)
            return path
        except Exception:
            try: os.remove(path)
            except Exception: pass
            raise

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        original_voice, original_rate, original_pitch = voice, rate, pitch
        voice = validate_voice(voice, self.edge_voice_names)
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)
        print("[tts_voice] Edge synth | "
              f"voice={voice!r} (orig={original_voice!r}) "
              f"rate={rate!r} (orig={original_rate!r}) "
              f"pitch={pitch!r} (orig={original_pitch!r}) "
              f"text={text[:80]!r}")
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            await edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch).save(path)
            return path
        except Exception as e:
            print(f"[tts_voice] Edge synth falhou | voice={voice!r} rate={rate!r} pitch={pitch!r} erro={e}")
            try: os.remove(path)
            except Exception: pass
            raise

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "edge":
            try:
                print(f"[tts_voice] QueueItem edge | voice={item.voice!r} rate={item.rate!r} pitch={item.pitch!r}")
                return await self._generate_edge_file(item.text, item.voice, item.rate, item.pitch)
            except Exception as e:
                print(f"[tts_voice] Edge falhou, usando gTTS. Guild {item.guild_id}: {e}")
                return await self._generate_gtts_file(item.text, item.language or GTTS_DEFAULT_LANGUAGE)
        print(f"[tts_voice] QueueItem gtts | language={item.language!r} text={item.text[:80]!r}")
        return await self._generate_gtts_file(item.text, item.language or GTTS_DEFAULT_LANGUAGE)

    async def _play_file(self, vc: discord.VoiceClient, path: str) -> None:
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        def _after(error: Optional[Exception]) -> None:
            if error:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_result, None)
        vc.play(discord.FFmpegPCMAudio(path), after=_after)
        await finished

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return True
        members = list(getattr(vc.channel, "members", []))
        humans = [m for m in members if not m.bot]
        if humans:
            print(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False
        try:
            await vc.disconnect(force=False)
            print(f"[tts_voice] Desconectado por inatividade | sozinho ou só com bots | guild={guild.id}")
            return True
        except Exception as e:
            print(f"[tts_voice] Erro ao desconectar por inatividade na guild {guild.id}: {e}")
            return False

    async def _worker_loop(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        try:
            while True:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    print(f"[tts_voice] Guild não encontrada no worker | guild={guild_id}")
                    return
                try:
                    item = await asyncio.wait_for(state.queue.get(), timeout=TTS_IDLE_DISCONNECT_SECONDS)
                except asyncio.TimeoutError:
                    disconnected = await self._disconnect_idle(guild)
                    if disconnected:
                        return
                    continue
                try:
                    target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
                    if target_channel is None:
                        print(f"[tts_voice] Canal de voz não encontrado | guild={guild_id} channel={item.channel_id}")
                        continue
                    if hasattr(self, "_should_block_for_voice_bot"):
                        blocked = await self._maybe_await(self._should_block_for_voice_bot(guild, target_channel))
                        if blocked:
                            print(f"[tts_voice] Worker bloqueado por outro bot de voz | guild={guild_id} channel={item.channel_id}")
                            if hasattr(self, "_disconnect_if_blocked"):
                                await self._maybe_await(self._disconnect_if_blocked(guild))
                            continue
                    vc = await self._maybe_await(self._ensure_connected(guild, target_channel))
                    if vc is None:
                        print(f"[tts_voice] Worker não conseguiu conectar | guild={guild_id} channel={item.channel_id}")
                        continue
                    path = await self._generate_audio_file(item)
                    try:
                        await self._play_file(vc, path)
                    finally:
                        try: os.remove(path)
                        except Exception: pass
                except Exception as e:
                    print(f"[tts_voice] Erro no worker da guild {guild_id}: {e}")
                finally:
                    state.queue.task_done()
        finally:
            state.worker_task = None
