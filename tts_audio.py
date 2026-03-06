import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import discord
import edge_tts
from gtts import gTTS

import config
from tts_helpers import validate_voice

TTS_IDLE_DISCONNECT_SECONDS = getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 30)
GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")


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
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker_task: Optional[asyncio.Task] = None
    last_text_channel_id: Optional[int] = None


class TTSAudioMixin:
    def _normalize_edge_rate(self, raw: str) -> str:
        value = str(raw or "").strip()
        value = value.replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        if value.endswith("%"):
            value = value[:-1]

        if not value:
            return "+0%"

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]

        if not number.isdigit():
            return "+0%"

        return f"{sign}{number}%"

    def _normalize_edge_pitch(self, raw: str) -> str:
        value = str(raw or "").strip()
        value = value.replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        lower = value.lower()
        if lower.endswith("hz"):
            value = value[:-2]

        if not value:
            return "+0Hz"

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]

        if not number.isdigit():
            return "+0Hz"

        return f"{sign}{number}Hz"

    async def _generate_gtts_file(self, text: str, language: str) -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower()
        print(f"[tts_voice] gTTS synth | language={language!r} text={text[:80]!r}")

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        try:
            tts = gTTS(text=text, lang=language)
            tts.save(path)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        original_voice = voice
        original_rate = rate
        original_pitch = pitch

        voice = validate_voice(voice, self.edge_voice_names)
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)

        print(
            "[tts_voice] Edge synth | "
            f"voice={voice!r} (orig={original_voice!r}) "
            f"rate={rate!r} (orig={original_rate!r}) "
            f"pitch={pitch!r} (orig={original_pitch!r}) "
            f"text={text[:80]!r}"
        )

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                pitch=pitch,
            )
            await communicate.save(path)
            return path
        except Exception as e:
            print(
                "[tts_voice] Edge synth falhou | "
                f"voice={voice!r} rate={rate!r} pitch={pitch!r} erro={e}"
            )
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "edge":
            try:
                print(
                    "[tts_voice] QueueItem edge | "
                    f"voice={item.voice!r} rate={item.rate!r} pitch={item.pitch!r}"
                )
                return await self._generate_edge_file(
                    item.text,
                    item.voice,
                    item.rate,
                    item.pitch,
                )
            except Exception as e:
                print(f"[tts_voice] Edge falhou, usando gTTS. Guild {item.guild_id}: {e}")
                return await self._generate_gtts_file(
                    item.text,
                    item.language or GTTS_DEFAULT_LANGUAGE,
                )

        print(
            "[tts_voice] QueueItem gtts | "
            f"language={item.language!r} text={item.text[:80]!r}"
        )
        return await self._generate_gtts_file(
            item.text,
            item.language or GTTS_DEFAULT_LANGUAGE,
        )

    def _get_state(self, guild_id: int) -> GuildTTSState:
        if not hasattr(self, "_tts_states"):
            self._tts_states = {}
        state = self._tts_states.get(guild_id)
        if state is None:
            state = GuildTTSState()
            self._tts_states[guild_id] = state
        return state

    def _ensure_worker(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self._worker_loop(guild_id))

    async def _play_file(self, voice_client: discord.VoiceClient, path: str) -> None:
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def _after_play(error: Optional[Exception]):
            if error:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_result, None)

        source = discord.FFmpegPCMAudio(path)
        voice_client.play(source, after=_after_play)
        await finished

    def _voice_channel_has_humans(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return False

        for member in getattr(vc.channel, "members", []):
            if not member.bot:
                return True
        return False

    def _voice_channel_has_only_bots_or_is_empty(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return True

        members = list(getattr(vc.channel, "members", []))
        if not members:
            return True

        return all(member.bot for member in members)

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return True

        if self._voice_channel_has_humans(guild):
            print(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False

        try:
            await vc.disconnect(force=False)
            print(f"[tts_voice] Desconectado por inatividade | sem humanos na call | guild={guild.id}")
            return True
        except Exception as e:
            print(f"[tts_voice] Erro ao desconectar por inatividade na guild {guild.id}: {e}")
            return False

    async def _worker_loop(self, guild_id: int) -> None:
        state = self._get_state(guild_id)

        while True:
            guild = self.bot.get_guild(guild_id)
            try:
                item: QueueItem = await asyncio.wait_for(
                    state.queue.get(),
                    timeout=TTS_IDLE_DISCONNECT_SECONDS,
                )
            except asyncio.TimeoutError:
                if guild is None:
                    state.worker_task = None
                    return

                disconnected = await self._disconnect_idle(guild)
                if disconnected:
                    state.worker_task = None
                    return

                continue

            if guild is None:
                continue

            channel = guild.get_channel(item.channel_id)
            if channel is None:
                print(f"[tts_voice] Canal de voz não encontrado | guild={item.guild_id} channel={item.channel_id}")
                continue

            vc = await self._ensure_connected(guild, channel)
            if vc is None:
                print(f"[tts_voice] Worker não conseguiu conectar | guild={item.guild_id} channel={item.channel_id}")
                continue

            audio_path = None
            try:
                audio_path = await self._generate_audio_file(item)
                await self._play_file(vc, audio_path)
            except Exception as e:
                print(f"[tts_voice] Erro no worker da guild {guild_id}: {e}")
            finally:
                if audio_path:
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass
