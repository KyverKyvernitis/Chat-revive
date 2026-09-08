from __future__ import annotations

import asyncio
import contextlib
import base64
import json
import logging
import os
import random
import time
import threading
import shutil
from datetime import datetime, timezone
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import quote, urlsplit, urlunsplit
import urllib.request
import urllib.error

import discord
import aiohttp

import config
from .api_providers import compact_key
from .extractor import MusicExtractor
from .errors import MusicExtractionError, MusicPlaybackError
from .models import LoopMode, MusicTrack
from .providers import describe_url
from .backends import MusicBackendManager
from .worker_node import (
    MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE,
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerEngineUnavailable,
    MusicWorkerUnavailable,
    ensure_music_worker_available as _ensure_music_worker_available,
    music_worker_only_enabled as _music_worker_only_enabled,
    require_music_worker_available as _require_music_worker_available,
    require_music_worker_available_async as _require_music_worker_available_async,
    resolve_music_tracks_on_worker as _resolve_music_tracks_on_worker,
    music_agent_command as _music_agent_command,
    music_agent_status as _music_agent_status,
)

logger = logging.getLogger(__name__)

MUSIC_DEFAULT_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_DEFAULT_VOLUME", 0.55))))
TTS_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_TTS_VOLUME", 1.0))))
MUSIC_TTS_LOCAL_DUCK_FACTOR = max(0.0, min(1.0, float(getattr(config, "MUSIC_TTS_LOCAL_DUCK_PERCENT", 5.0)) / 100.0))
MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT = max(0, min(100, int(getattr(config, "MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", 8))))
MUSIC_AGENT_TTS_ROUTE_ENABLED = bool(getattr(config, "MUSIC_AGENT_TTS_ROUTE_ENABLED", True))
MUSIC_AGENT_TTS_TIMEOUT_SECONDS = max(3.0, float(getattr(config, "MUSIC_AGENT_TTS_TIMEOUT_SECONDS", 30.0)))
MUSIC_LAVALINK_TTS_PAUSE_ENABLED = bool(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_ENABLED", True))
MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS = max(0.2, float(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS", 0.35)))
MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL = bool(getattr(config, "MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL", True))
MUSIC_IDLE_DISCONNECT_SECONDS = max(15.0, float(getattr(config, "MUSIC_IDLE_DISCONNECT_SECONDS", 120)))
MUSIC_AFK_DISCONNECT_GRACE_SECONDS = max(0.0, float(getattr(config, "MUSIC_AFK_DISCONNECT_GRACE_SECONDS", 2.0)))
MUSIC_QUEUE_MAXSIZE = min(100, max(1, int(getattr(config, "MUSIC_QUEUE_MAXSIZE", 100))))
MUSIC_MAX_PLAYLIST_ITEMS = min(100, max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 100))))
MUSIC_SEARCH_RESULTS = max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5))))
MUSIC_YTDLP_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "MUSIC_YTDLP_TIMEOUT_SECONDS", 20.0)))
MUSIC_RECONNECT_BEFORE_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_BEFORE_OPTIONS", "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin") or "-nostdin")
MUSIC_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")
MUSIC_TTS_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")
MUSIC_PLAYBACK_START_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "MUSIC_PLAYBACK_START_TIMEOUT_SECONDS", 8.0)))
MUSIC_HISTORY_MAXSIZE = max(5, int(getattr(config, "MUSIC_HISTORY_MAXSIZE", 25)))
MUSIC_CONTROL_VOTE_SECONDS = max(10.0, float(getattr(config, "MUSIC_CONTROL_VOTE_SECONDS", 45)))
MUSIC_PREFETCH_NEXT = bool(getattr(config, "MUSIC_PREFETCH_NEXT", True))
MUSIC_LIMITER_ENABLED = bool(getattr(config, "MUSIC_LIMITER_ENABLED", True))
MUSIC_MAX_GLOBAL_PREFETCH = max(0, int(getattr(config, "MUSIC_MAX_GLOBAL_PREFETCH", 1)))
MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS = max(0, int(getattr(config, "MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS", 2)))
MUSIC_PREFETCH_MIN_DELAY_SECONDS = max(0.0, float(getattr(config, "MUSIC_PREFETCH_MIN_DELAY_SECONDS", 18.0)))
MUSIC_PREFETCH_BEFORE_END_SECONDS = max(5.0, float(getattr(config, "MUSIC_PREFETCH_BEFORE_END_SECONDS", 45.0)))
MUSIC_PANEL_UPDATE_THROTTLE_SECONDS = max(0.05, float(getattr(config, "MUSIC_PANEL_UPDATE_THROTTLE_SECONDS", 2.0)))
MUSIC_AUDIO_MODE = str(getattr(config, "MUSIC_AUDIO_MODE", "auto") or "auto").strip().lower()
MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS = max(1, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS", 1)))
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ABR", 256)))  # compat/env antigo
MUSIC_MAX_AUDIO_BITRATE_STABLE = max(64, int(getattr(config, "MUSIC_MAX_AUDIO_BITRATE_STABLE", 160)))
MUSIC_HEAVY_LOAD_MAX_ABR = max(64, int(getattr(config, "MUSIC_HEAVY_LOAD_MAX_ABR", 128)))
MUSIC_AUTO_BITRATE_ENABLED = bool(getattr(config, "MUSIC_AUTO_BITRATE_ENABLED", True))
MUSIC_AUTO_BITRATE_MAX = max(8000, int(getattr(config, "MUSIC_AUTO_BITRATE_MAX", 384000)))
MUSIC_AUTO_BITRATE_MIN_GAIN = max(0, int(getattr(config, "MUSIC_AUTO_BITRATE_MIN_GAIN", 16000)))
MUSIC_STREAM_START_RETRIES = max(0, int(getattr(config, "MUSIC_STREAM_START_RETRIES", 1)))
MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS = max(5.0, float(getattr(config, "MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS", 45.0)))
MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS = max(5.0, float(getattr(config, "MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS", 35.0)))
MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES = max(0, int(getattr(config, "MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES", 1)))
MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS", 4.0)))
MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS = max(0.0, float(getattr(config, "MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS", 18.0)))
MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS = max(0.2, float(getattr(config, "MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS", 1.5)))
MUSIC_TTS_INTERNAL_BASE_URL = str(getattr(config, "MUSIC_TTS_INTERNAL_BASE_URL", "") or "").strip().rstrip("/")
MUSIC_LAVALINK_TTS_INTERNAL_FIRST = bool(getattr(config, "MUSIC_LAVALINK_TTS_INTERNAL_FIRST", True))
MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS = max(0.25, float(getattr(config, "MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS", 1.75)))
MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK = bool(getattr(config, "MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK", True))
MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS", 45.0) or 45.0))
MUSIC_TTS_AUDIO_FORMAT = str(getattr(config, "MUSIC_TTS_AUDIO_FORMAT", "opus") or "opus").strip().lower()
MUSIC_TTS_AUDIO_FALLBACK_FORMAT = str(getattr(config, "MUSIC_TTS_AUDIO_FALLBACK_FORMAT", "mp3") or "mp3").strip().lower()
MUSIC_TTS_OPUS_BITRATE = str(getattr(config, "MUSIC_TTS_OPUS_BITRATE", "48k") or "48k").strip()
MUSIC_TTS_OPUS_SAMPLE_RATE = max(8000, int(getattr(config, "MUSIC_TTS_OPUS_SAMPLE_RATE", 48000) or 48000))
MUSIC_TTS_OPUS_CHANNELS = min(2, max(1, int(getattr(config, "MUSIC_TTS_OPUS_CHANNELS", 1) or 1)))
MUSIC_TTS_CONVERT_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "MUSIC_TTS_CONVERT_TIMEOUT_SECONDS", 8.0) or 8.0))
MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED = bool(getattr(config, "MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED", True))
MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS = max(0.8, float(getattr(config, "MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS", 3.5) or 3.5))
MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB = max(1, int(getattr(config, "MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB", 8) or 8))
MUSIC_TTS_PREROLL_SILENCE_MS = max(0, int(getattr(config, "MUSIC_TTS_PREROLL_SILENCE_MS", 140) or 0))
MUSIC_TTS_POSTROLL_SILENCE_MS = max(0, int(getattr(config, "MUSIC_TTS_POSTROLL_SILENCE_MS", 180) or 0))
MUSIC_TTS_FADE_IN_MS = max(0, int(getattr(config, "MUSIC_TTS_FADE_IN_MS", 45) or 0))
MUSIC_TTS_FADE_OUT_MS = max(0, int(getattr(config, "MUSIC_TTS_FADE_OUT_MS", 70) or 0))
MUSIC_TTS_MP3_BITRATE = str(getattr(config, "MUSIC_TTS_MP3_BITRATE", "96k") or "96k").strip()
MUSIC_RESOLVING_STALE_SECONDS = max(5.0, float(getattr(config, "MUSIC_RESOLVING_STALE_SECONDS", 45.0)))
MUSIC_VOICE_STATUS_ENABLED = bool(getattr(config, "MUSIC_VOICE_STATUS_ENABLED", True))
MUSIC_VOICE_STATUS_TEMPLATE = str(getattr(config, "MUSIC_VOICE_STATUS_TEMPLATE", "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})") or "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})").strip()
MUSIC_VOICE_STATUS_IDLE = str(getattr(config, "MUSIC_VOICE_STATUS_IDLE", "") or "").strip()
MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS = max(15.0, float(getattr(config, "MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS", 60.0)))
MUSIC_SOURCE_EMOJIS = {
    "youtube": "<:YouTube:1502490543891021827>",
    "spotify": "<:Spotify:1502490573205016676>",
    "deezer": "<:Deezer:1502490958997094420>",
    "soundcloud": "<:SoundCloud:1502491211485675631>",
}
MUSIC_SOURCE_EMOJI_FALLBACK = "🎵"

PHONE_WORKER_ENABLED = bool(getattr(config, "PHONE_WORKER_ENABLED", False))
PHONE_WORKER_HOST = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
PHONE_WORKER_SCHEME = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip() or "http"
PHONE_WORKER_TOKEN = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()

PCM_FRAME_BYTES = 3840  # 20ms, 48kHz, stereo, signed 16-bit little endian
PCM_FRAME_MS = 20.0
PCM_FRAMES_PER_SECOND = int(1000 / PCM_FRAME_MS)
PCM_SILENCE_FRAME = b"\x00" * PCM_FRAME_BYTES
PCM_LIMITER_THRESHOLD = 30000
MUSIC_WORKER_STREAM_PREBUFFER_SECONDS = max(6.0, float(getattr(config, "MUSIC_WORKER_STREAM_PREBUFFER_SECONDS", 8.0) or 8.0))
MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS = max(30.0, float(getattr(config, "MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS", 90.0) or 90.0))
MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS = max(20.0, float(getattr(config, "MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS", 30.0) or 30.0))
MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY = max(1, int(getattr(config, "MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY", 25) or 25))


def _ffmpeg_options_with_base_volume(options: str, volume: float) -> tuple[str, float]:
    """Aplica volume base no FFmpeg para reduzir trabalho Python por frame.

    Sem TTS por cima, o áudio pode sair quase em passthrough. Quando há TTS
    o mixer Python aplica apenas o fator restante em cima do volume base já
    feito em C pelo FFmpeg.
    """
    raw_options = str(options or "").strip()
    try:
        base_volume = max(0.0, min(2.0, float(volume)))
    except Exception:
        base_volume = 1.0
    # Não injeta filtro se já há filtro customizado, se volume zero impediria
    # aumentar a música atual depois, ou se o volume acima de 100% precisaria
    # do limiter Python para evitar distorção.
    normalized = f" {raw_options} "
    has_filter = " -af " in normalized or " -filter:a " in normalized or " -filter_complex " in normalized
    if has_filter or base_volume <= 0.001 or base_volume > 1.0 or abs(base_volume - 1.0) < 0.001:
        return raw_options, 1.0
    return f"{raw_options} -filter:a volume={base_volume:.4f}".strip(), base_volume


def _consume_expected_music_exception(done: asyncio.Future) -> None:
    """Consome cancelamentos esperados para não gerar "exception was never retrieved"."""
    try:
        done.result()
    except asyncio.CancelledError:
        return
    except MusicPlaybackError as exc:
        message = str(exc)
        if message in {"Música pulada antes de iniciar o áudio.", "Playback cancelado."}:
            return
        logger.warning("[music] task/future terminou com erro de playback: %s", exc)
    except MusicExtractionError:
        # Tasks de resolução também são aguardadas pelo worker; deixar o worker
        # enviar a mensagem amigável evita traceback duplicado e ruído no log.
        return
    except Exception as exc:
        logger.warning(
            "[music] task/future terminou com exceção inesperada",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


@dataclass(slots=True, eq=False)
class TTSOverlay:
    source: discord.AudioSource
    volume: float
    future: asyncio.Future
    started_at: float = field(default_factory=time.monotonic)
    ended: bool = False


@dataclass(slots=True)
class ControlVote:
    action: str
    voters: set[int] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > MUSIC_CONTROL_VOTE_SECONDS


class MixedAudioSource(discord.AudioSource):
    """Mistura música + overlays de TTS em PCM sem numpy.

    O Discord chama read() em uma thread de áudio. Por isso, qualquer Future é
    resolvida com call_soon_threadsafe no loop principal.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, source_base_volume: float = 1.0, duck_factor: float = MUSIC_TTS_LOCAL_DUCK_FACTOR) -> None:
        self.loop = loop
        self.music_source = music_source
        self.music_volume = float(music_volume)
        self.normal_music_volume = float(music_volume)
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self.source_base_volume = max(0.001, min(2.0, float(source_base_volume or 1.0)))
        self._current_music_volume = float(music_volume)
        self._overlays: list[TTSOverlay] = []
        self._overlay_lock = threading.RLock()
        self._closed = False
        self._music_ended = False
        self._music_started = False
        self.music_started_future = loop.create_future()

    def is_opus(self) -> bool:
        return False

    @property
    def has_overlays(self) -> bool:
        with self._overlay_lock:
            return bool(self._overlays)

    def _future_set_result_threadsafe(self, future: asyncio.Future, result: object = None) -> None:
        def _set_once() -> None:
            if not future.done():
                future.set_result(result)

        self.loop.call_soon_threadsafe(_set_once)

    def _future_set_exception_threadsafe(self, future: asyncio.Future, error: Exception) -> None:
        def _set_once() -> None:
            if not future.done():
                future.set_exception(error)

        self.loop.call_soon_threadsafe(_set_once)

    def _future_cancel_threadsafe(self, future: asyncio.Future) -> None:
        def _cancel_once() -> None:
            if not future.done():
                future.cancel()

        self.loop.call_soon_threadsafe(_cancel_once)

    def _mark_music_started(self) -> None:
        if self._music_started:
            return
        self._music_started = True
        if not self.music_started_future.done():
            self._future_set_result_threadsafe(self.music_started_future, None)

    def _mark_music_failed_before_start(self, message: str) -> None:
        if self._music_started or self.music_started_future.done():
            return
        self._future_set_exception_threadsafe(self.music_started_future, MusicPlaybackError(message))

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def _step_music_volume(self, target: float) -> float:
        target = max(0.0, min(2.0, float(target)))
        self._current_music_volume = target
        return target

    def _limit_sample(self, value: int) -> int:
        if not MUSIC_LIMITER_ENABLED:
            return max(-32768, min(32767, int(value)))
        value = int(value)
        if value > PCM_LIMITER_THRESHOLD:
            value = int(PCM_LIMITER_THRESHOLD + (value - PCM_LIMITER_THRESHOLD) * 0.35)
        elif value < -PCM_LIMITER_THRESHOLD:
            value = int(-PCM_LIMITER_THRESHOLD + (value + PCM_LIMITER_THRESHOLD) * 0.35)
        return max(-32768, min(32767, value))

    def _limit_samples(self, samples: array) -> None:
        if not MUSIC_LIMITER_ENABLED:
            return
        for i, sample in enumerate(samples):
            samples[i] = self._limit_sample(int(sample))

    def add_tts(self, source: discord.AudioSource, *, volume: float) -> asyncio.Future:
        future = self.loop.create_future()
        overlay = TTSOverlay(source=source, volume=max(0.0, min(2.0, float(volume))), future=future)
        with self._overlay_lock:
            self._overlays.append(overlay)
        return future


    def cancel_tts(self, future: asyncio.Future) -> None:
        target = None
        with self._overlay_lock:
            for overlay in self._overlays:
                if overlay.future is future:
                    target = overlay
                    break
            if target is not None:
                self._overlays = [ov for ov in self._overlays if ov is not target]
        if target is not None:
            with contextlib.suppress(Exception):
                target.source.cleanup()
        if not future.done():
            self._future_cancel_threadsafe(future)

    def _finish_overlay(self, overlay: TTSOverlay, error: Exception | None = None) -> None:
        if overlay.ended:
            return
        overlay.ended = True
        with contextlib.suppress(Exception):
            overlay.source.cleanup()
        if not overlay.future.done():
            if error is None:
                self._future_set_result_threadsafe(overlay.future, None)
            else:
                self._future_set_exception_threadsafe(overlay.future, error)

    def _effective_music_scale(self, target_volume: float) -> float:
        # music_source pode já vir com volume base aplicado pelo FFmpeg.
        return max(0.0, min(4.0, float(target_volume) / self.source_base_volume))

    def _apply_volume(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(float(volume) - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit_sample(int(sample * volume))
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._apply_volume(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            base[i] = self._limit_sample(int(base[i]) + int(sample))

    def read(self) -> bytes:
        if self._closed:
            return b""

        music_frame = b""
        if not self._music_ended:
            music_frame = self.music_source.read()
            if music_frame:
                self._mark_music_started()
            else:
                self._music_ended = True
                self._mark_music_failed_before_start("FFmpeg encerrou antes de entregar áudio.")
                with contextlib.suppress(Exception):
                    self.music_source.cleanup()

        with self._overlay_lock:
            active_overlays = list(self._overlays)

        if not music_frame and not active_overlays:
            self.cleanup()
            return b""

        if music_frame:
            # Ducking local/yt-dlp: quando há TTS sobreposto, reduza a música
            # para uma fração pequena do volume normal, mas sem alterar o volume
            # persistido do servidor. Quando o último TTS termina, o read() volta
            # automaticamente ao volume normal.
            target_music_volume = self.normal_music_volume * (self.duck_factor if active_overlays else 1.0)
            stepped_volume = self._step_music_volume(target_music_volume)
            scale = self._effective_music_scale(stepped_volume)
            if not active_overlays and abs(scale - 1.0) <= 0.001:
                # Caminho crítico: sem TTS e sem ajuste dinâmico, não processa
                # 96k samples/s em Python. Isso reduz cortes quando yt-dlp/FFmpeg
                # prepara a próxima música em VPS fraca.
                return music_frame
            base = self._apply_volume(music_frame, scale)
        else:
            # Música acabou no mesmo instante em que havia TTS por cima. Mantém silêncio
            # como base para não cortar o TTS no meio da frase.
            base = array("h", [0] * (PCM_FRAME_BYTES // 2))

        if active_overlays:
            ended: list[TTSOverlay] = []
            for overlay in active_overlays:
                try:
                    frame = overlay.source.read()
                    if frame:
                        self._mix_into(base, frame, overlay.volume)
                    else:
                        ended.append(overlay)
                        self._finish_overlay(overlay)
                except Exception as exc:
                    ended.append(overlay)
                    self._finish_overlay(overlay, exc)
            if ended:
                with self._overlay_lock:
                    self._overlays = [ov for ov in self._overlays if ov not in ended]

        self._limit_samples(base)
        return base.tobytes()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._mark_music_failed_before_start("Playback foi limpo antes do áudio iniciar.")
        with contextlib.suppress(Exception):
            self.music_source.cleanup()
        with self._overlay_lock:
            overlays = list(self._overlays)
            self._overlays.clear()
        for overlay in overlays:
            self._finish_overlay(overlay)


class WorkerPCMHttpAudioSource(discord.AudioSource):
    """PCM 48k stereo produzido pelo phone worker com jitter buffer.

    O Discord chama read() em uma thread de áudio a cada ~20ms. Por isso,
    read() não pode bloquear em rede/Tailscale. Um leitor em background puxa o
    HTTP do worker, acumula frames PCM e read() só consome frames prontos.
    """

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        timeout: float = 20.0,
        prebuffer_seconds: float = MUSIC_WORKER_STREAM_PREBUFFER_SECONDS,
        max_buffer_seconds: float = MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS,
    ) -> None:
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.timeout = max(30.0, float(timeout or 90.0))
        self.prebuffer_seconds = max(6.0, float(prebuffer_seconds or MUSIC_WORKER_STREAM_PREBUFFER_SECONDS))
        self.max_buffer_seconds = max(self.prebuffer_seconds + 3.0, 20.0, float(max_buffer_seconds or MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS))
        self._prebuffer_frames = max(1, int(self.prebuffer_seconds * PCM_FRAMES_PER_SECOND))
        self._max_buffer_frames = max(self._prebuffer_frames + 1, int(self.max_buffer_seconds * PCM_FRAMES_PER_SECOND))
        self._response: Any = None
        self._closed = False
        self._reader_started = False
        self._reader_done = False
        self._last_error: Exception | None = None
        self._frames: deque[bytes] = deque()
        self._buffer = b""
        self._underruns = 0
        self._frames_read = 0
        self._frames_buffered = 0
        self._started_at = time.monotonic()
        self._ready_event = threading.Event()
        self._condition = threading.Condition(threading.RLock())
        self._reader_thread: threading.Thread | None = None

    def is_opus(self) -> bool:
        return False

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def _open_response(self) -> Any:
        headers = {"User-Agent": "CoreMusicWorkerPCM/1.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        return urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310 - URL do phone worker configurado

    def _ensure_reader_started(self) -> None:
        with self._condition:
            if self._reader_started or self._closed:
                return
            self._reader_started = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="music-worker-pcm-reader",
                daemon=True,
            )
            self._reader_thread.start()

    def _append_frame_locked(self, frame: bytes) -> None:
        if len(frame) < PCM_FRAME_BYTES:
            frame = frame + (b"\x00" * (PCM_FRAME_BYTES - len(frame)))
        elif len(frame) > PCM_FRAME_BYTES:
            frame = frame[:PCM_FRAME_BYTES]
        while not self._closed and len(self._frames) >= self._max_buffer_frames:
            self._condition.wait(timeout=0.05)
        if self._closed:
            return
        self._frames.append(frame)
        self._frames_buffered += 1
        if len(self._frames) >= self._prebuffer_frames:
            self._ready_event.set()
        self._condition.notify_all()

    def _reader_loop(self) -> None:
        chunk_buffer = b""
        try:
            self._response = self._open_response()
            while True:
                with self._condition:
                    if self._closed:
                        break
                chunk = self._response.read(PCM_FRAME_BYTES * 8)
                if not chunk:
                    break
                chunk_buffer += chunk
                while len(chunk_buffer) >= PCM_FRAME_BYTES:
                    frame = chunk_buffer[:PCM_FRAME_BYTES]
                    chunk_buffer = chunk_buffer[PCM_FRAME_BYTES:]
                    with self._condition:
                        self._append_frame_locked(frame)
            if chunk_buffer:
                with self._condition:
                    self._append_frame_locked(chunk_buffer)
        except Exception as exc:
            self._last_error = exc
            logger.warning("[music/worker-local] leitor PCM falhou: %s", exc)
        finally:
            with self._condition:
                self._reader_done = True
                self._ready_event.set()
                self._condition.notify_all()
            elapsed = max(0.001, time.monotonic() - self._started_at)
            logger.info(
                "[music/worker-local] leitor PCM encerrado | frames=%s read=%s underruns=%s buffer=%s elapsed=%.1fs error=%s",
                self._frames_buffered,
                self._frames_read,
                self._underruns,
                len(self._frames),
                elapsed,
                type(self._last_error).__name__ if self._last_error else "",
            )
            response = self._response
            self._response = None
            with contextlib.suppress(Exception):
                if response is not None:
                    response.close()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Abre o HTTP em background e espera pré-buffer antes do vc.play()."""
        self._ensure_reader_started()
        wait_timeout = max(0.5, float(timeout if timeout is not None else MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS))
        ok = self._ready_event.wait(timeout=wait_timeout)
        with self._condition:
            ready = bool(self._frames)
            done = bool(self._reader_done)
            error = self._last_error
        if ready:
            logger.info(
                "[music/worker-local] pré-buffer pronto | frames=%s alvo=%s timeout=%.1fs",
                len(self._frames),
                self._prebuffer_frames,
                wait_timeout,
            )
            return True
        if error:
            logger.warning("[music/worker-local] pré-buffer falhou: %s", error)
        elif done:
            logger.warning("[music/worker-local] worker encerrou antes do pré-buffer")
        elif not ok:
            logger.warning("[music/worker-local] pré-buffer timeout | frames=%s alvo=%s", len(self._frames), self._prebuffer_frames)
        return False

    def read(self) -> bytes:
        self._ensure_reader_started()
        with self._condition:
            if self._closed:
                return b""
            if self._frames:
                frame = self._frames.popleft()
                self._frames_read += 1
                self._condition.notify_all()
                return frame
            if self._reader_done:
                return b""
            self._underruns += 1
            if self._underruns == 1 or self._underruns % MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY == 0:
                logger.warning(
                    "[music/worker-local] underrun de buffer | count=%s read=%s buffered=%s",
                    self._underruns,
                    self._frames_read,
                    self._frames_buffered,
                )
            return PCM_SILENCE_FRAME

    def cleanup(self) -> None:
        response = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            response = self._response
            self._response = None
            self._frames.clear()
            self._buffer = b""
            self._ready_event.set()
            self._condition.notify_all()
        with contextlib.suppress(Exception):
            if response is not None:
                response.close()


@dataclass
class MusicGuildState:
    queue: asyncio.Queue[MusicTrack] = field(default_factory=lambda: asyncio.Queue(maxsize=MUSIC_QUEUE_MAXSIZE))
    worker_task: Optional[asyncio.Task] = None
    current: Optional[MusicTrack] = None
    last_text_channel_id: Optional[int] = None
    last_voice_channel_id: Optional[int] = None
    volume: float = MUSIC_DEFAULT_VOLUME
    loop_mode: LoopMode = LoopMode.OFF
    shuffle: bool = False
    stop_requested: bool = False
    paused: bool = False
    current_source: Optional[MixedAudioSource] = None
    current_backend: str = "local"
    current_lavalink_player: Any = None
    current_lavalink_playable: Any = None
    current_lavalink_node_label: str = ""
    current_lavalink_node_name: str = ""
    current_resolve_task: Optional[asyncio.Task] = None
    next_resolve_task: Optional[asyncio.Task] = None
    next_resolve_key: str = ""
    next_resolve_active_key: str = ""
    current_status: str = "idle"
    current_status_changed_at: float = field(default_factory=time.monotonic)
    skip_requested: bool = False
    skip_transition_active: bool = False
    skip_history_suppressed_once: bool = False
    now_message: Optional[discord.Message] = None
    panel_track_key: Optional[str] = None
    history: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_HISTORY_MAXSIZE))
    forward_queue: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_HISTORY_MAXSIZE))
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    music_owns_voice: bool = False
    tts_voice_touched: bool = False
    last_tts_activity_at: float = 0.0
    lavalink_tts_until: float = 0.0
    lavalink_resume_grace_until: float = 0.0
    tts_session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tts_session_active_until: float = 0.0
    tts_session_last_error: str = ""
    tts_session_last_cleanup_at: float = 0.0
    tts_lavalink_failures: int = 0
    tts_lavalink_local_fallback_until: float = 0.0
    music_session_active: bool = False
    music_idle_disconnect_task: Optional[asyncio.Task] = None
    music_afk_expired: bool = False
    music_operation_generation: int = 0
    control_votes: dict[str, ControlVote] = field(default_factory=dict)
    control_vote_cleanup_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    volume_loaded: bool = False
    idle_reason: str = "idle"
    idle_actor_id: Optional[int] = None
    idle_actor_name: str = ""
    idle_channel_name: str = ""
    internal_voice_disconnect_until: float = 0.0
    lavalink_transition_until: float = 0.0
    last_lavalink_error: str = ""
    panel_update_task: Optional[asyncio.Task] = None
    panel_update_create: bool = True
    panel_update_requested_at: float = 0.0
    panel_controls_invalid_at: float = 0.0
    panel_controls_invalidation_task: Optional[asyncio.Task] = None
    current_started_at_monotonic: float = 0.0
    current_start_offset_seconds: float = 0.0
    next_local_start_offset_seconds: float = 0.0
    auto_bitrate_channel_id: Optional[int] = None
    auto_bitrate_original: Optional[int] = None
    auto_bitrate_boosted: Optional[int] = None
    current_quality_label: str = "Alta"
    current_quality_kbps: int = MUSIC_HIGH_QUALITY_MAX_ABR
    voice_status_channel_id: Optional[int] = None
    voice_status_had_original: bool = False
    voice_status_original: str = ""
    voice_status_last_bot: str = ""
    voice_status_update_task: Optional[asyncio.Task] = None
    voice_status_last_update_at: float = 0.0
    voice_status_last_track_key: str = ""
    agent_started_track_key: str = ""
    panel_last_repost_key: str = ""
    panel_last_repost_at: float = 0.0
    agent_last_idle_event: str = ""
    agent_remote_queue_size: int = 0
    agent_remote_history_size: int = 0
    voice_status_last_applied_key: str = ""
    voice_status_last_sync_request_key: str = ""
    voice_status_last_sync_request_at: float = 0.0
    voice_status_last_restore_key: str = ""
    voice_status_last_restore_at: float = 0.0
    voice_status_force_task: Optional[asyncio.Task] = None
    voice_status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent_monitor_task: Optional[asyncio.Task] = None
    agent_started_track_key: str = ""
    agent_side_effect_task: Optional[asyncio.Task] = None
    panel_last_repost_key: str = ""
    panel_last_repost_at: float = 0.0

    def queue_size(self) -> int:
        local_count = self.queue.qsize() + len(self.forward_queue)
        try:
            remote_count = int(getattr(self, "agent_remote_queue_size", 0) or 0)
        except Exception:
            remote_count = 0
        if str(getattr(self, "current_backend", "") or "").lower() == "agent":
            return max(local_count, remote_count)
        return local_count


class AudioRouter:
    """Ponto único de áudio do bot.

    Música usa MixedAudioSource. TTS usa overlay quando existe música ativa; caso contrário,
    cai no playback direto para manter latência baixa.
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        self.extractor = MusicExtractor(
            max_playlist_items=MUSIC_MAX_PLAYLIST_ITEMS,
            search_results=MUSIC_SEARCH_RESULTS,
            timeout_seconds=MUSIC_YTDLP_TIMEOUT_SECONDS,
        )
        self._states: dict[int, MusicGuildState] = {}
        self._global_prefetch_active = 0
        self.backends = MusicBackendManager(bot, self.extractor)
        self._lavalink_shadow_tasks: dict[int, asyncio.Task] = {}
        self._phone_worker_tts_convert_disabled_until: float = 0.0
        self._phone_worker_tts_convert_last_log_at: float = 0.0

    def get_state(self, guild_id: int) -> MusicGuildState:
        state = self._states.get(int(guild_id))
        if state is None:
            state = MusicGuildState()
            self._states[int(guild_id)] = state
        self._load_persisted_volume(int(guild_id), state)
        return state

    def current_music_operation_generation(self, guild_id: int) -> int:
        return int(getattr(self.get_state(guild_id), "music_operation_generation", 0) or 0)

    def cancel_pending_music_operations(self, guild_id: int, *, reason: str = "control") -> int:
        """Invalidate pending play/search/playlist work for this guild.

        This is intentionally broader than stopping the currently audible source:
        stop/skip/shuffle can happen while a playlist extraction, yt-dlp resolve,
        or next-track prefetch is still running. Bumping this generation lets the
        caller ignore stale results that finish after the user changed the queue.
        """
        state = self.get_state(guild_id)
        state.music_operation_generation = int(getattr(state, "music_operation_generation", 0) or 0) + 1
        self._cancel_next_prefetch(state)
        task = getattr(state, "current_resolve_task", None)
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                task.cancel()
        state.current_resolve_task = None
        logger.info("[music] operações pendentes invalidadas | guild=%s reason=%s generation=%s", guild_id, reason, state.music_operation_generation)
        return state.music_operation_generation

    def _load_persisted_volume(self, guild_id: int, state: MusicGuildState) -> None:
        if state.volume_loaded:
            return
        state.volume_loaded = True
        settings_db = getattr(self.bot, "settings_db", None)
        try:
            guild_doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) if settings_db is not None else {}
            raw_volume = guild_doc.get("music_volume")
            if raw_volume is not None:
                state.volume = max(0.0, min(1.5, float(raw_volume)))
        except Exception:
            logger.debug("[music] falha ao carregar volume persistido", exc_info=True)

    def _set_current_status(self, state: MusicGuildState, status: str) -> None:
        status = str(status or "idle").strip().lower() or "idle"
        if getattr(state, "current_status", "") != status:
            state.current_status = status
            state.current_status_changed_at = time.monotonic()
        else:
            state.current_status = status

    def music_worker_only_enabled(self) -> bool:
        return bool(_music_worker_only_enabled())

    @property
    def music_worker_unavailable_message(self) -> str:
        return MUSIC_WORKER_UNAVAILABLE_MESSAGE

    async def ensure_music_worker_available(self):
        return await _ensure_music_worker_available()

    def require_music_worker_available(self):
        return _require_music_worker_available()

    async def require_music_worker_available_async(self):
        return await _require_music_worker_available_async()

    def _track_uses_worker_local_stream(self, track: MusicTrack | None) -> bool:
        if track is None:
            return False
        extractor = str(getattr(track, "extractor", "") or "").strip().lower()
        stream_url = str(getattr(track, "stream_url", "") or "").strip().lower()
        return bool(extractor == "worker-ytdlp" and "/music/stream/" in stream_url)

    def _track_is_worker_ytdlp_candidate(self, track: MusicTrack | None) -> bool:
        """Faixa resolvida pelo phone worker via yt-dlp.

        A busca leve retorna metadados. Ao tocar, o worker resolve a URL/stream
        direto; Lavalink/NodeLink não fazem mais parte do fluxo de worker.
        """
        if track is None:
            return False
        extractor = str(getattr(track, "extractor", "") or "").strip().lower()
        source = str(getattr(track, "source", "") or "").strip().lower()
        return bool(extractor == "worker-ytdlp" or source.startswith("worker-ytdlp"))

    def _make_worker_local_source(self, track: MusicTrack) -> WorkerPCMHttpAudioSource:
        return WorkerPCMHttpAudioSource(
            str(getattr(track, "stream_url", "") or ""),
            token=PHONE_WORKER_TOKEN,
            timeout=max(30.0, float(getattr(config, "MUSIC_WORKER_STREAM_CONNECT_TIMEOUT_SECONDS", 90.0) or 90.0)),
            prebuffer_seconds=MUSIC_WORKER_STREAM_PREBUFFER_SECONDS,
            max_buffer_seconds=MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS,
        )

    def _reset_stale_resolving_state(self, guild_id: int, state: MusicGuildState, *, reason: str = "stale") -> bool:
        """Limpa estado local preso em resolving sem task/source ativa.

        Esse estado impede ducking/TTS local e faz o painel/stop acharem que há
        playback pendente quando FFmpeg ou fallback local já falhou antes de iniciar.
        """
        if str(getattr(state, "current_status", "") or "").lower() != "resolving":
            return False
        task = getattr(state, "current_resolve_task", None)
        task_active = bool(task is not None and not task.done())
        if task_active or state.current_source is not None:
            return False
        age = time.monotonic() - float(getattr(state, "current_status_changed_at", 0.0) or 0.0)
        if age < MUSIC_RESOLVING_STALE_SECONDS:
            return False
        logger.warning(
            "[music] resolving preso limpo | guild=%s track=%r age=%.1fs reason=%s",
            guild_id,
            getattr(getattr(state, "current", None), "title", ""),
            age,
            reason,
        )
        state.current = None
        state.current_source = None
        state.current_resolve_task = None
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.current_backend = "local"
        state.music_session_active = False
        state.music_afk_expired = False
        state.paused = False
        state.skip_transition_active = False
        state.skip_history_suppressed_once = False
        self._set_current_status(state, "idle" if not self._has_pending_track(state) else "queued")
        return True

    def _is_lavalink_voice_client(self, vc: Any) -> bool:
        if vc is None:
            return False
        module = str(getattr(type(vc), "__module__", "") or "")
        qualname = str(getattr(type(vc), "__qualname__", "") or getattr(type(vc), "__name__", "") or "")
        return module.startswith("wavelink") or (qualname == "Player" and hasattr(vc, "node") and hasattr(vc, "play"))

    def _vc_is_connected(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            for attr in ("connected", "is_connected"):
                value = getattr(vc, attr, None)
                try:
                    if callable(value):
                        value = value()
                    if value is not None:
                        return bool(value)
                except Exception:
                    continue
            # Wavelink Player não expõe is_connected em todas as versões. Se ele
            # ainda tem canal/guild, trate como ativo para não disparar falso
            # external_disconnect durante troca de faixa/eventos internos.
            return bool(getattr(vc, "channel", None) is not None or getattr(vc, "guild", None) is not None)
        checker = getattr(vc, "is_connected", None)
        return bool(checker() if callable(checker) else getattr(vc, "connected", False))

    def _vc_is_playing(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            # Wavelink 3 usa propriedades ``playing``/``current`` em vez dos
            # métodos do discord.VoiceClient. Alguns nodes públicos demoram um
            # pouco para atualizar ``playing``; se há ``current`` carregado e o
            # player segue conectado, ainda há uma faixa ativa. Sem isso, o
            # worker achava que a faixa acabou logo após player.play() e pulava
            # várias músicas/status em sequência.
            for attr in ("playing", "is_playing"):
                value = getattr(vc, attr, None)
                try:
                    if callable(value):
                        value = value()
                    if bool(value):
                        return True
                except Exception:
                    continue
            current = getattr(vc, "current", None)
            return bool(current is not None and self._vc_is_connected(vc))
        checker = getattr(vc, "is_playing", None)
        return bool(checker() if callable(checker) else getattr(vc, "playing", False))

    def _vc_is_paused(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            value = getattr(vc, "paused", False)
            try:
                if callable(value):
                    value = value()
            except Exception:
                return False
            return bool(value)
        checker = getattr(vc, "is_paused", None)
        return bool(checker() if callable(checker) else getattr(vc, "paused", False))

    def _vc_is_playing_or_paused(self, vc: Any) -> bool:
        return self._vc_is_playing(vc) or self._vc_is_paused(vc)

    def _vc_lavalink_explicit_playing_or_paused(self, vc: Any) -> bool:
        if vc is None or not self._is_lavalink_voice_client(vc):
            return False
        for attr in ("playing", "is_playing", "paused", "is_paused"):
            value = getattr(vc, attr, None)
            try:
                if callable(value):
                    value = value()
                if bool(value):
                    return True
            except Exception:
                continue
        # Em Wavelink/Lavalink 4.2, alguns players seguem com áudio real
        # enquanto ``playing`` oscila falso. Se há faixa atual e a conexão de voz
        # existe, ainda trate como ativo para não limpar o painel/queue cedo.
        current = getattr(vc, "current", None)
        return bool(current is not None and self._vc_is_connected(vc))

    def _lavalink_tts_active(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_tts_until", 0.0) or 0.0)

    def _lavalink_resume_grace_active(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_resume_grace_until", 0.0) or 0.0)

    async def _vc_stop_audio(self, vc: Any) -> None:
        if vc is None:
            return
        if self._is_lavalink_voice_client(vc):
            skipper = getattr(vc, "skip", None)
            if callable(skipper):
                with contextlib.suppress(TypeError):
                    result = skipper(force=True)
                    if asyncio.iscoroutine(result):
                        await result
                    return
                result = skipper()
                if asyncio.iscoroutine(result):
                    await result
                return
            stopper = getattr(vc, "stop", None)
            if callable(stopper):
                result = stopper()
                if asyncio.iscoroutine(result):
                    await result
            return
        stopper = getattr(vc, "stop", None)
        if callable(stopper):
            stopper()

    async def _persist_volume(self, guild_id: int, volume: float) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc["music_volume"] = max(0.0, min(1.5, float(volume)))
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao salvar volume persistido", exc_info=True)

    def _auto_bitrate_record_from_doc(self, guild_id: int) -> dict | None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return None
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) or {}
            raw = doc.get("music_auto_bitrate")
            if isinstance(raw, dict):
                return dict(raw)
        except Exception:
            logger.debug("[music] falha ao ler auto bitrate persistido", exc_info=True)
        return None

    async def _save_auto_bitrate_record(self, guild_id: int, record: dict) -> bool:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return False
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return False
            doc = get_doc(int(guild_id))
            doc["music_auto_bitrate"] = dict(record)
            await save_doc(int(guild_id), doc)
            return True
        except Exception:
            logger.debug("[music] falha ao salvar auto bitrate", exc_info=True)
            return False

    async def _clear_auto_bitrate_record(self, guild_id: int) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc.pop("music_auto_bitrate", None)
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao limpar auto bitrate", exc_info=True)

    def _load_auto_bitrate_record_into_state(self, guild_id: int, state: MusicGuildState) -> dict | None:
        record = self._auto_bitrate_record_from_doc(guild_id)
        if not record:
            return None
        try:
            state.auto_bitrate_channel_id = int(record.get("channel_id") or 0) or None
            state.auto_bitrate_original = int(record.get("original_bitrate") or 0) or None
            state.auto_bitrate_boosted = int(record.get("boosted_bitrate") or 0) or None
        except Exception:
            state.auto_bitrate_channel_id = None
            state.auto_bitrate_original = None
            state.auto_bitrate_boosted = None
            return None
        return record

    def _is_normal_voice_channel(self, channel) -> bool:
        if channel is None:
            return False
        stage_type = getattr(discord, "StageChannel", None)
        if stage_type is not None and isinstance(channel, stage_type):
            return False
        return bool(hasattr(channel, "edit") and hasattr(channel, "bitrate") and hasattr(channel, "permissions_for"))

    def _bot_can_manage_voice_channel(self, guild: discord.Guild, channel) -> bool:
        member = getattr(guild, "me", None) or getattr(guild, "self_role", None)
        if member is None:
            return False
        try:
            perms = channel.permissions_for(member)
            return bool(getattr(perms, "manage_channels", False))
        except Exception:
            return False

    def _target_auto_bitrate(self, guild: discord.Guild, channel) -> int:
        current = int(getattr(channel, "bitrate", 0) or 0)
        guild_limit = int(getattr(guild, "bitrate_limit", 0) or 0)
        if guild_limit <= 0:
            guild_limit = max(current, 96000)

        # Quando só uma guild está tocando, usa o máximo permitido pelo servidor.
        # Com várias guilds tocando, mantém bitrate alto o bastante para música,
        # mas evita forçar todos os canais no teto ao mesmo tempo.
        active = max(1, self._active_player_count())
        hard_limit = min(int(MUSIC_AUTO_BITRATE_MAX), guild_limit)
        if active <= MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS:
            target = hard_limit
        elif active >= 3:
            target = min(hard_limit, max(64000, int(MUSIC_HEAVY_LOAD_MAX_ABR) * 1000))
        else:
            target = min(hard_limit, max(64000, int(MUSIC_MAX_AUDIO_BITRATE_STABLE) * 1000))
        return max(8000, int(target))

    async def _boost_auto_bitrate_for_music(self, guild: discord.Guild, channel, state: MusicGuildState) -> None:
        if not MUSIC_AUTO_BITRATE_ENABLED or guild is None or channel is None:
            return
        if not self._is_normal_voice_channel(channel):
            return
        if not self._bot_can_manage_voice_channel(guild, channel):
            return

        # Se a sessão anterior marcou outro canal, restaura antes de mexer no novo.
        if state.auto_bitrate_channel_id and int(state.auto_bitrate_channel_id) != int(getattr(channel, "id", 0) or 0):
            await self._restore_auto_bitrate_for_state(guild, state, reason="channel_change")

        current = int(getattr(channel, "bitrate", 0) or 0)
        target = self._target_auto_bitrate(guild, channel)
        if target <= current + MUSIC_AUTO_BITRATE_MIN_GAIN:
            return

        if state.auto_bitrate_channel_id == int(getattr(channel, "id", 0) or 0) and state.auto_bitrate_boosted == target:
            return

        record = {
            "channel_id": int(getattr(channel, "id", 0) or 0),
            "original_bitrate": current,
            "boosted_bitrate": target,
            "started_at": time.time(),
            "reason": "music_player",
            "active_players": self._active_player_count(),
        }
        # Sem persistência, não altera para evitar canal preso após restart/crash.
        if not await self._save_auto_bitrate_record(guild.id, record):
            return

        try:
            await channel.edit(bitrate=target, reason="Aumentar bitrate temporariamente enquanto o player de música toca")
        except (discord.Forbidden, discord.HTTPException):
            await self._clear_auto_bitrate_record(guild.id)
            logger.debug("[music] auto bitrate não pôde editar canal | guild=%s channel=%s", guild.id, getattr(channel, "id", None), exc_info=True)
            return
        except Exception:
            await self._clear_auto_bitrate_record(guild.id)
            logger.debug("[music] auto bitrate falhou", exc_info=True)
            return

        state.auto_bitrate_channel_id = record["channel_id"]
        state.auto_bitrate_original = record["original_bitrate"]
        state.auto_bitrate_boosted = record["boosted_bitrate"]

    async def _restore_auto_bitrate_for_state(
        self,
        guild: discord.Guild | None,
        state: MusicGuildState,
        *,
        reason: str = "music_finished",
        channel_hint=None,
    ) -> None:
        if not MUSIC_AUTO_BITRATE_ENABLED:
            return
        if guild is None:
            return

        record = None
        if state.auto_bitrate_channel_id and state.auto_bitrate_original and state.auto_bitrate_boosted:
            record = {
                "channel_id": int(state.auto_bitrate_channel_id),
                "original_bitrate": int(state.auto_bitrate_original),
                "boosted_bitrate": int(state.auto_bitrate_boosted),
            }
        else:
            record = self._load_auto_bitrate_record_into_state(guild.id, state)
        if not record:
            return

        try:
            channel_id = int(record.get("channel_id") or 0)
            original = int(record.get("original_bitrate") or 0)
            boosted = int(record.get("boosted_bitrate") or 0)
        except Exception:
            await self._clear_auto_bitrate_record(guild.id)
            return
        if channel_id <= 0 or original <= 0 or boosted <= 0:
            await self._clear_auto_bitrate_record(guild.id)
            return

        channel = channel_hint if channel_hint is not None and int(getattr(channel_hint, "id", 0) or 0) == channel_id else None
        channel = channel or guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None or not self._is_normal_voice_channel(channel):
            await self._clear_auto_bitrate_record(guild.id)
        else:
            current = int(getattr(channel, "bitrate", 0) or 0)
            # Idempotência: só restaura se o canal ainda está exatamente no
            # bitrate que o bot marcou como temporário. Se staff já mexeu ou o
            # canal já está normal, limpa a marcação e não edita nada.
            if current == boosted and original != current:
                if not self._bot_can_manage_voice_channel(guild, channel):
                    await self._clear_auto_bitrate_record(guild.id)
                    return
                try:
                    await channel.edit(bitrate=original, reason=f"Restaurar bitrate após música ({reason})")
                except (discord.Forbidden, discord.HTTPException):
                    await self._clear_auto_bitrate_record(guild.id)
                    logger.debug("[music] auto bitrate skip sem permissão | guild=%s channel=%s", guild.id, channel_id, exc_info=True)
                    return
                except Exception:
                    logger.debug("[music] restauração de bitrate falhou", exc_info=True)
                    return
            await self._clear_auto_bitrate_record(guild.id)

        state.auto_bitrate_channel_id = None
        state.auto_bitrate_original = None
        state.auto_bitrate_boosted = None

    async def reconcile_auto_bitrate_records(self) -> None:
        """Restaura apenas bitrates temporários realmente pendentes.

        Startup não deve editar canal só porque existe config antiga. Cada item
        passa por validações idempotentes em _restore_auto_bitrate_for_state().
        """
        if not MUSIC_AUTO_BITRATE_ENABLED:
            return
        settings_db = getattr(self.bot, "settings_db", None)
        docs = getattr(settings_db, "guild_cache", {}) if settings_db is not None else {}
        pending = [
            (int(guild_id), doc)
            for guild_id, doc in list(docs.items())
            if isinstance(doc, dict) and isinstance(doc.get("music_auto_bitrate"), dict)
        ]
        if not pending:
            return
        restored = 0
        cleared = 0
        for guild_id, doc in pending:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self._clear_auto_bitrate_record(int(guild_id))
                cleared += 1
                continue
            state = self.get_state(int(guild_id))
            self._load_auto_bitrate_record_into_state(int(guild_id), state)
            before = self._auto_bitrate_record_from_doc(int(guild_id))
            await self._restore_auto_bitrate_for_state(guild, state, reason="restart")
            after = self._auto_bitrate_record_from_doc(int(guild_id))
            if before and not after:
                restored += 1
            await asyncio.sleep(max(0.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_GUILD_DELAY_SECONDS", 0.35) or 0.35)))
        if restored or cleared:
            logger.info("[music] restore bitrate startup concluído | pendentes=%s resolvidos=%s limpos=%s", len(pending), restored, cleared)

    def _voice_status_record_from_doc(self, guild_id: int) -> dict | None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return None
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) or {}
            raw = doc.get("music_voice_status_restore")
            if isinstance(raw, dict):
                return dict(raw)
        except Exception:
            logger.debug("[music] falha ao ler status de voz persistido", exc_info=True)
        return None

    async def _save_voice_status_record(self, guild_id: int, record: dict) -> bool:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return False
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return False
            doc = get_doc(int(guild_id))
            doc["music_voice_status_restore"] = dict(record)
            await save_doc(int(guild_id), doc)
            return True
        except Exception:
            logger.debug("[music] falha ao salvar status de voz", exc_info=True)
            return False

    async def _clear_voice_status_record(self, guild_id: int) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc.pop("music_voice_status_restore", None)
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao limpar status de voz", exc_info=True)

    def _load_voice_status_record_into_state(self, guild_id: int, state: MusicGuildState) -> dict | None:
        record = self._voice_status_record_from_doc(guild_id)
        if not record:
            return None
        try:
            state.voice_status_channel_id = int(record.get("channel_id") or 0) or None
            state.voice_status_had_original = bool(record.get("had_original_status"))
            state.voice_status_original = str(record.get("original_status") or "")
            state.voice_status_last_bot = str(record.get("last_bot_status") or "")
            state.voice_status_last_track_key = str(record.get("last_track_key") or "")
        except Exception:
            state.voice_status_channel_id = None
            state.voice_status_had_original = False
            state.voice_status_original = ""
            state.voice_status_last_bot = ""
            state.voice_status_last_track_key = ""
            return None
        return record

    def _voice_status_settings_from_doc(self, guild_id: int) -> dict:
        settings_db = getattr(self.bot, "settings_db", None)
        doc = {}
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) if settings_db is not None else {}
        except Exception:
            doc = {}
        raw = doc.get("music_voice_status") if isinstance(doc, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        enabled = bool(raw.get("enabled", MUSIC_VOICE_STATUS_ENABLED))
        template = str(raw.get("template") or MUSIC_VOICE_STATUS_TEMPLATE).strip() or MUSIC_VOICE_STATUS_TEMPLATE
        idle = str(raw.get("idle") if raw.get("idle") is not None else MUSIC_VOICE_STATUS_IDLE).strip()
        return {"enabled": enabled, "template": template, "idle": idle}

    async def _save_voice_status_settings(self, guild_id: int, settings: dict) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc["music_voice_status"] = {
                "enabled": bool(settings.get("enabled", MUSIC_VOICE_STATUS_ENABLED)),
                "template": str(settings.get("template") or MUSIC_VOICE_STATUS_TEMPLATE).strip() or MUSIC_VOICE_STATUS_TEMPLATE,
                "idle": str(settings.get("idle") if settings.get("idle") is not None else "").strip(),
            }
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao salvar configuração de status de voz", exc_info=True)

    def get_voice_status_settings(self, guild_id: int) -> dict:
        return self._voice_status_settings_from_doc(guild_id)

    async def set_voice_status_enabled(self, guild_id: int, enabled: bool) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["enabled"] = bool(enabled)
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if not enabled:
            await self._restore_voice_status_for_state(guild, state, reason="config_disabled")
        elif guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    async def set_voice_status_template(self, guild_id: int, template: str) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["template"] = self._sanitize_voice_status_template(template)
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    async def set_voice_status_idle(self, guild_id: int, idle: str) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["idle"] = self._trim_voice_status(str(idle or ""))
        await self._save_voice_status_settings(guild_id, settings)
        return settings

    async def reset_voice_status_settings(self, guild_id: int) -> dict:
        settings = {"enabled": MUSIC_VOICE_STATUS_ENABLED, "template": MUSIC_VOICE_STATUS_TEMPLATE, "idle": MUSIC_VOICE_STATUS_IDLE}
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    def _sanitize_voice_status_template(self, template: str) -> str:
        template = str(template or "").strip()
        if not template:
            template = MUSIC_VOICE_STATUS_TEMPLATE
        return template[:500]

    def _trim_voice_status(self, status: str) -> str:
        status = " ".join(str(status or "").replace("\n", " ").split())
        return status[:500]

    def _source_key_for_track(self, track: MusicTrack | None) -> str:
        if track is None:
            return ""
        fields = []
        for attr in ("source", "extractor", "original_url", "webpage_url", "display_url", "stream_url"):
            with contextlib.suppress(Exception):
                value = str(getattr(track, attr, "") or "").strip().lower()
                if value:
                    fields.append(value)
        text = " ".join(fields)
        # Prioridade nos links/fontes originais: quando um link Spotify/Deezer
        # cai em fallback tocável do YouTube, o status ainda deve mostrar o
        # emoji da plataforma que o usuário pediu, não um ícone genérico.
        if "spotify" in text:
            return "spotify"
        if "soundcloud" in text or "sound cloud" in text:
            return "soundcloud"
        if "deezer" in text:
            return "deezer"
        if "youtube" in text or "youtu.be" in text or "ytmusic" in text or "yt-dlp" in text:
            return "youtube"
        return ""

    def _source_emoji_for_track(self, track: MusicTrack | None) -> str:
        source_key = self._source_key_for_track(track)
        return MUSIC_SOURCE_EMOJIS.get(source_key) or MUSIC_SOURCE_EMOJI_FALLBACK

    def _voice_status_track_key(self, track: MusicTrack | None) -> str:
        if track is None:
            return ""
        parts: list[str] = []
        for attr in ("webpage_url", "original_url", "display_url", "title", "uploader", "duration"):
            with contextlib.suppress(Exception):
                value = str(getattr(track, attr, "") or "").strip()
                if value:
                    parts.append(value)
        if not parts:
            return str(id(track))
        return "|".join(parts)

    def _voice_status_track_is_current(self, state: MusicGuildState, track: MusicTrack | None, track_key: str) -> bool:
        current = state.current
        if current is None or track is None:
            return False
        if current is track:
            return True
        return bool(track_key and self._voice_status_track_key(current) == track_key)

    def _quality_label_for_cap(self, cap: int | None) -> str:
        if cap is None:
            return "Alta"
        try:
            cap_int = int(cap or 0)
        except Exception:
            cap_int = 0
        if cap_int <= MUSIC_HEAVY_LOAD_MAX_ABR:
            return "Baixa"
        return "Média"

    def _refresh_quality_state(self, state: MusicGuildState, track: MusicTrack | None = None, *, cap: int | None = None) -> None:
        track = track or state.current
        if cap is None:
            cap = self._audio_max_abr_for_load()
        state.current_quality_label = self._quality_label_for_cap(cap)
        kbps = 0
        if track is not None:
            with contextlib.suppress(Exception):
                kbps = int(getattr(track, "resolved_audio_abr", 0) or 0)
            if not kbps:
                with contextlib.suppress(Exception):
                    kbps = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        if not kbps:
            kbps = int(cap or MUSIC_HIGH_QUALITY_MAX_ABR)
        state.current_quality_kbps = max(1, int(kbps))

    def render_voice_status(self, guild_id: int, track: MusicTrack | None = None, *, template: str | None = None) -> str:
        state = self.get_state(guild_id)
        track = track or state.current
        self._refresh_quality_state(state, track)
        requester = ""
        if track is not None:
            requester = str(getattr(track, "requester_name", "") or f"<@{getattr(track, 'requester_id', 0)}>")
        title = str(getattr(track, "title", "") or "Música sem título")
        author = str(getattr(track, "uploader", "") or getattr(track, "source", "") or getattr(track, "extractor", "") or "fonte desconhecida")
        duration = getattr(track, "duration_label", "desconhecida") if track is not None else "desconhecida"
        elapsed = "0:00"
        remaining = duration
        if track is not None and not getattr(track, "is_live", False):
            started = float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0)
            offset = max(0.0, float(getattr(state, "current_start_offset_seconds", 0.0) or 0.0))
            elapsed_seconds = max(0, int(offset + (time.monotonic() - started))) if started else int(offset)
            elapsed = self._format_seconds(elapsed_seconds)
            if getattr(track, "duration", None) is not None:
                remaining = self._format_seconds(max(0, int(float(track.duration) - elapsed_seconds)))
        values = {
            "source_emoji": self._source_emoji_for_track(track),
            "title": title,
            "artist": author,
            "author": author,
            "duration": duration,
            "elapsed": elapsed,
            "remaining": remaining,
            "requester": requester or "alguém",
            "queue": str(state.queue_size()),
            "quality": str(getattr(state, "current_quality_label", "Alta") or "Alta"),
            "kbps": str(int(getattr(state, "current_quality_kbps", MUSIC_HIGH_QUALITY_MAX_ABR) or MUSIC_HIGH_QUALITY_MAX_ABR)),
        }
        raw_template = self._sanitize_voice_status_template(template or self._voice_status_settings_from_doc(guild_id).get("template") or MUSIC_VOICE_STATUS_TEMPLATE)
        for key, value in values.items():
            raw_template = raw_template.replace("{" + key + "}", str(value))
        return self._trim_voice_status(raw_template)

    def preview_voice_status(self, guild_id: int) -> str:
        state = self.get_state(guild_id)
        if state.current is not None:
            return self.render_voice_status(guild_id, state.current)
        sample = MusicTrack(
            title="Compass [Limbus Company]",
            webpage_url="https://www.youtube.com/watch?v=preview",
            requester_id=0,
            requester_name="C.◉.R.E",
            uploader="Mili",
            source="YouTube",
            extractor="Youtube",
            duration=169,
        )
        sample.resolved_audio_abr = int(getattr(state, "current_quality_kbps", MUSIC_HIGH_QUALITY_MAX_ABR) or MUSIC_HIGH_QUALITY_MAX_ABR)
        return self.render_voice_status(guild_id, sample)

    def _format_seconds(self, total: int | float) -> str:
        total = max(0, int(total or 0))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _bot_can_set_voice_status(self, guild: discord.Guild, channel) -> bool:
        """Best-effort permission check for Discord's voice-status endpoint.

        Older discord.py builds may not expose the newer
        ``set_voice_channel_status`` permission flag yet. In that case we do
        not block the feature here; the raw REST endpoint will be the source of
        truth and will return Forbidden if the bot really lacks permission.
        """
        member = getattr(guild, "me", None)
        if member is None or channel is None:
            return False
        try:
            perms = channel.permissions_for(member)
            if bool(getattr(perms, "manage_channels", False)):
                return True
            voice_status_perm = getattr(perms, "set_voice_channel_status", None)
            if voice_status_perm is None:
                return True
            return bool(voice_status_perm)
        except Exception:
            # If the library cannot evaluate the new permission, still try the
            # endpoint and let Discord answer with 403 when needed.
            return True

    async def _fetch_voice_channel_status(self, channel) -> tuple[bool, str]:
        if channel is None:
            return False, ""
        for attr in ("status", "voice_status"):
            if hasattr(channel, attr):
                value = getattr(channel, attr, None)
                if value:
                    return True, str(value or "")
                # Em versões atuais do discord.py, esses atributos podem existir
                # mas vir vazios/desatualizados mesmo quando o endpoint de
                # voice-status está funcionando. Tratar vazio como "conhecido"
                # fazia o bot achar que staff mudou manualmente e impedia a
                # troca de status quando a próxima música começava.
        try:
            http = getattr(self.bot, "http", None)
            data = None
            get_channel = getattr(http, "get_channel", None)
            if callable(get_channel):
                data = await get_channel(int(getattr(channel, "id", 0)))
            else:
                from discord.http import Route
                request = getattr(http, "request", None)
                if callable(request):
                    data = await request(Route("GET", "/channels/{channel_id}", channel_id=int(getattr(channel, "id", 0))))
            if isinstance(data, dict):
                if data.get("status"):
                    return True, str(data.get("status") or "")
                if data.get("voice_status"):
                    return True, str(data.get("voice_status") or "")
        except (discord.Forbidden, discord.HTTPException):
            return False, ""
        except Exception:
            logger.debug("[music] falha ao ler status atual do canal", exc_info=True)
        return False, ""

    async def _set_voice_channel_status(self, channel, status: str, *, reason: str = "") -> bool:
        try:
            from discord.http import Route
            http = getattr(self.bot, "http", None)
            request = getattr(http, "request", None)
            if not callable(request):
                logger.warning("[music] não consegui alterar status do canal: cliente HTTP indisponível")
                return False
            channel_id = int(getattr(channel, "id", 0) or 0)
            if channel_id <= 0:
                logger.warning("[music] não consegui alterar status do canal: canal inválido")
                return False
            payload_status = self._trim_voice_status(status)
            if payload_status:
                await request(
                    Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                    json={"status": payload_status},
                    reason=reason or None,
                )
            else:
                # O endpoint de status de voz usa PUT. Enviar string vazia é
                # aceito pelo cliente oficial para limpar o status; caso o
                # Discord mude o contrato, tentamos null como fallback.
                try:
                    await request(
                        Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                        json={"status": ""},
                        reason=reason or None,
                    )
                except discord.HTTPException:
                    await request(
                        Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                        json={"status": None},
                        reason=reason or None,
                    )
            return True
        except discord.Forbidden:
            logger.warning("[music] não consegui alterar status do canal: permissão ausente")
            return False
        except discord.HTTPException as exc:
            logger.warning("[music] não consegui alterar status do canal: HTTP %s", getattr(exc, "status", "?"))
            return False
        except Exception:
            logger.warning("[music] falha inesperada ao alterar status do canal", exc_info=True)
            return False

    async def _apply_voice_status_for_music(self, guild: discord.Guild, channel, state: MusicGuildState, track: MusicTrack, *, force: bool = False) -> None:
        if guild is None or channel is None or track is None:
            return
        settings = self._voice_status_settings_from_doc(guild.id)
        if not bool(settings.get("enabled", True)):
            return
        if not self._bot_can_set_voice_status(guild, channel):
            logger.warning("[music] não consegui alterar status do canal: permissão ausente")
            return
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id <= 0:
            return

        track_key = self._voice_status_track_key(track)
        desired = self.render_voice_status(guild.id, track, template=settings.get("template"))
        if not desired:
            return
        desired_key = f"{channel_id}:{track_key}:{desired}"
        # Dedup barato antes de chamar REST: se o status renderizado já é o
        # último aplicado para essa faixa/canal, não faça nova chamada nem log.
        if (
            not force
            and int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
            and str(getattr(state, "voice_status_last_track_key", "") or "") == track_key
            and str(getattr(state, "voice_status_last_bot", "") or "") == desired
        ):
            return
        # Troca de faixa é uma atualização forçada e não deve ser bloqueada
        # pela checagem anti-staff. Porém, se a task antiga terminar depois de
        # a música já ter mudado, ela não pode sobrescrever o status novo.
        if force and not self._voice_status_track_is_current(state, track, track_key):
            return

        async with state.voice_status_lock:
            if force and not self._voice_status_track_is_current(state, track, track_key):
                return
            if state.voice_status_channel_id and int(state.voice_status_channel_id) != channel_id:
                await self._restore_voice_status_for_state(guild, state, reason="channel_change")

            known, current_status = await self._fetch_voice_channel_status(channel)
            if not known:
                # O status atual do canal nem sempre vem no objeto/REST comum do
                # discord.py. Ainda assim aplicamos o status da música; só marcamos
                # que não havia status original conhecido para não inventar restauração.
                current_status = ""
            if state.voice_status_channel_id == channel_id and state.voice_status_last_bot:
                if known and current_status != state.voice_status_last_bot and not force:
                    # Staff mudou manualmente; não briga com a alteração em
                    # atualizações periódicas. Trocas reais de faixa usam force=True.
                    await self._clear_voice_status_record(guild.id)
                    state.voice_status_channel_id = None
                    state.voice_status_had_original = False
                    state.voice_status_original = ""
                    state.voice_status_last_bot = ""
                    state.voice_status_last_track_key = ""
                    return
            else:
                record = {
                    "channel_id": channel_id,
                    "had_original_status": bool(current_status),
                    "original_status": current_status,
                    "last_bot_status": "",
                    "last_track_key": "",
                    "started_at": time.time(),
                    "reason": "music_player",
                }
                if not await self._save_voice_status_record(guild.id, record):
                    return
                state.voice_status_channel_id = channel_id
                state.voice_status_had_original = bool(current_status)
                state.voice_status_original = current_status
                state.voice_status_last_bot = ""
                state.voice_status_last_track_key = ""

            if not force and desired == state.voice_status_last_bot and track_key == state.voice_status_last_track_key:
                return
            if not force and desired_key == str(getattr(state, "voice_status_last_applied_key", "") or ""):
                return
            if force and not self._voice_status_track_is_current(state, track, track_key):
                return
            logger.info("[music] aplicando status do canal | guild=%s channel=%s track=%r", guild.id, channel_id, getattr(track, "title", ""))
            if not await self._set_voice_channel_status(channel, desired, reason="Atualizar status do canal enquanto a música toca"):
                await self._clear_voice_status_record(guild.id)
                state.voice_status_channel_id = None
                state.voice_status_had_original = False
                state.voice_status_original = ""
                state.voice_status_last_bot = ""
                state.voice_status_last_track_key = ""
                return
            logger.info("[music] status do canal aplicado | guild=%s channel=%s", guild.id, channel_id)
            state.voice_status_last_bot = desired
            state.voice_status_last_track_key = track_key
            state.voice_status_last_applied_key = desired_key
            state.voice_status_last_update_at = time.monotonic()
            await self._save_voice_status_record(
                guild.id,
                {
                    "channel_id": channel_id,
                    "had_original_status": bool(state.voice_status_had_original),
                    "original_status": state.voice_status_original,
                    "last_bot_status": desired,
                    "last_track_key": track_key,
                    "started_at": time.time(),
                    "reason": "music_player",
                },
            )
            self._schedule_voice_status_refresh(guild.id, state)

    def _schedule_voice_status_track_sync(self, guild_id: int, *, repeat_after: float = 2.0, reason: str = "track_change") -> None:
        """Sincroniza status do canal para a faixa atual sem bloquear o player.

        Troca real de faixa não usa o mesmo cooldown do refresh periódico.
        O status só deve ser aplicado depois que o playback começou de verdade;
        retries atrasados só devem acontecer quando o chamador pedir explicitamente.
        """
        state = self.get_state(guild_id)
        current_track = getattr(state, "current", None)
        current_key = self._voice_status_track_key(current_track) if current_track is not None else ""
        now = time.monotonic()
        sync_key = f"{current_key}:{reason}"
        # Trocas reais de faixa precisam passar imediatamente, inclusive quando
        # o usuário alterna rápido A → B → A. O cooldown antigo podia bloquear
        # a segunda atualização legítima e deixar o status preso na música
        # anterior até o refresh tardio.
        high_priority_reason = reason in {"playback_started", "lavalink_track_started", "agent_playback_started", "music_agent_playback_started", "skip", "previous", "seek"}
        if (
            not high_priority_reason
            and sync_key
            and sync_key == str(getattr(state, "voice_status_last_sync_request_key", "") or "")
            and now - float(getattr(state, "voice_status_last_sync_request_at", 0.0) or 0.0) < 1.75
        ):
            return
        state.voice_status_last_sync_request_key = sync_key
        state.voice_status_last_sync_request_at = now
        old_task = state.voice_status_force_task
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _runner() -> None:
            try:
                guild = self.bot.get_guild(int(guild_id))
                if guild is None or state.current is None or not state.last_voice_channel_id:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                track_key = self._voice_status_track_key(state.current)
                logger.info(
                    "[music] atualizando status do canal por %s | guild=%s track=%r",
                    reason,
                    guild_id,
                    getattr(state.current, "title", ""),
                )
                await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
                if repeat_after <= 0:
                    return
                await asyncio.sleep(max(0.0, float(repeat_after)))
                if state.current is None or self._voice_status_track_key(state.current) != track_key:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] sincronização forçada de status de voz falhou", exc_info=True)
            finally:
                if state.voice_status_force_task is asyncio.current_task():
                    state.voice_status_force_task = None

        state.voice_status_force_task = asyncio.create_task(_runner())
        state.voice_status_force_task.add_done_callback(_consume_expected_music_exception)

    def _schedule_voice_status_refresh(self, guild_id: int, state: MusicGuildState) -> None:
        settings = self._voice_status_settings_from_doc(guild_id)
        template = str(settings.get("template") or "")
        if "{elapsed}" not in template and "{remaining}" not in template:
            return
        task = state.voice_status_update_task
        if task is not None and not task.done():
            return

        async def _runner() -> None:
            try:
                while state.current is not None and state.current_status in {"playing", "paused"}:
                    await asyncio.sleep(MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS)
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is None or state.current is None or not state.last_voice_channel_id:
                        return
                    channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                    await self._apply_voice_status_for_music(guild, channel, state, state.current, force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] atualização periódica de status de voz falhou", exc_info=True)
            finally:
                if state.voice_status_update_task is asyncio.current_task():
                    state.voice_status_update_task = None

        state.voice_status_update_task = asyncio.create_task(_runner())
        state.voice_status_update_task.add_done_callback(_consume_expected_music_exception)

    def _cancel_voice_status_refresh(self, state: MusicGuildState) -> None:
        task = state.voice_status_update_task
        if task is not None and not task.done():
            task.cancel()
        state.voice_status_update_task = None
        force_task = state.voice_status_force_task
        current_task = asyncio.current_task()
        if force_task is not None and not force_task.done() and force_task is not current_task:
            force_task.cancel()
        if force_task is not current_task:
            state.voice_status_force_task = None

    def _mark_voice_status_track_change(self, state: MusicGuildState) -> None:
        """Libera dedupe antes de uma troca real de faixa/posição.

        Mantém ``voice_status_last_bot`` para ainda reconhecer o status anterior
        como status do próprio bot, mas força o próximo PUT. Isso evita que
        skip rápido, A → B → A, seek ou faixas repetidas fiquem presas no
        status visual antigo por causa da chave deduplicada.
        """
        state.voice_status_last_track_key = ""
        state.voice_status_last_applied_key = ""
        state.voice_status_last_sync_request_key = ""
        state.voice_status_last_sync_request_at = 0.0

    async def _restore_voice_status_for_state(
        self,
        guild: discord.Guild | None,
        state: MusicGuildState,
        *,
        reason: str = "music_finished",
        channel_hint=None,
    ) -> None:
        self._cancel_voice_status_refresh(state)
        if guild is None:
            return
        record = None
        if state.voice_status_channel_id:
            record = {
                "channel_id": int(state.voice_status_channel_id),
                "had_original_status": bool(state.voice_status_had_original),
                "original_status": state.voice_status_original,
                "last_bot_status": state.voice_status_last_bot,
            }
        else:
            record = self._load_voice_status_record_into_state(guild.id, state)
        if not record:
            return
        try:
            channel_id = int(record.get("channel_id") or 0)
            original_status = str(record.get("original_status") or "")
            had_original = bool(record.get("had_original_status"))
            last_bot_status = str(record.get("last_bot_status") or "")
        except Exception:
            await self._clear_voice_status_record(guild.id)
            return
        if channel_id <= 0:
            await self._clear_voice_status_record(guild.id)
            return
        channel = channel_hint if channel_hint is not None and int(getattr(channel_hint, "id", 0) or 0) == channel_id else None
        channel = channel or guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            await self._clear_voice_status_record(guild.id)
        else:
            known, current_status = await self._fetch_voice_channel_status(channel)
            target_status = original_status if had_original else str(self._voice_status_settings_from_doc(guild.id).get("idle") or "")
            target_status = self._trim_voice_status(target_status)
            current_status = self._trim_voice_status(current_status)
            last_bot_status = self._trim_voice_status(last_bot_status)

            if known and last_bot_status and current_status != last_bot_status:
                # Staff mudou manualmente; respeita a alteração e só limpa a marcação.
                await self._clear_voice_status_record(guild.id)
            elif known and current_status == target_status:
                # Já está restaurado. Não faz PUT no startup só para confirmar.
                await self._clear_voice_status_record(guild.id)
            elif not known and not target_status:
                # Não sabemos ler o status atual, mas o alvo é vazio/idle vazio;
                # não vale chamar endpoint em restart só para limpar algo invisível.
                await self._clear_voice_status_record(guild.id)
            else:
                if not self._bot_can_set_voice_status(guild, channel):
                    await self._clear_voice_status_record(guild.id)
                    logger.debug("[music] restore status skip sem permissão | guild=%s channel=%s", guild.id, channel_id)
                    return
                restore_key = f"{channel_id}:{target_status}:{reason}"
                now = time.monotonic()
                if (
                    restore_key == str(getattr(state, "voice_status_last_restore_key", "") or "")
                    and now - float(getattr(state, "voice_status_last_restore_at", 0.0) or 0.0) < 4.0
                ):
                    return
                state.voice_status_last_restore_key = restore_key
                state.voice_status_last_restore_at = now
                ok = await self._set_voice_channel_status(channel, target_status, reason=f"Restaurar status do canal após música ({reason})")
                if not ok:
                    await self._clear_voice_status_record(guild.id)
                    return
                logger.info("[music] status do canal restaurado | guild=%s channel=%s reason=%s", guild.id, channel_id, reason)
                await self._clear_voice_status_record(guild.id)
        state.voice_status_channel_id = None
        state.voice_status_had_original = False
        state.voice_status_original = ""
        state.voice_status_last_bot = ""
        state.voice_status_last_track_key = ""
        state.voice_status_last_applied_key = ""
        state.voice_status_last_sync_request_key = ""
        state.voice_status_last_sync_request_at = 0.0
        state.voice_status_last_update_at = 0.0

    async def reconcile_voice_status_records(self) -> None:
        """Restaura apenas status de canal temporário realmente pendente."""
        settings_db = getattr(self.bot, "settings_db", None)
        docs = getattr(settings_db, "guild_cache", {}) if settings_db is not None else {}
        pending = [
            (int(guild_id), doc)
            for guild_id, doc in list(docs.items())
            if isinstance(doc, dict) and isinstance(doc.get("music_voice_status_restore"), dict)
        ]
        if not pending:
            return
        restored = 0
        cleared = 0
        for guild_id, doc in pending:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self._clear_voice_status_record(int(guild_id))
                cleared += 1
                continue
            state = self.get_state(int(guild_id))
            self._load_voice_status_record_into_state(int(guild_id), state)
            before = self._voice_status_record_from_doc(int(guild_id))
            await self._restore_voice_status_for_state(guild, state, reason="restart")
            after = self._voice_status_record_from_doc(int(guild_id))
            if before and not after:
                restored += 1
            await asyncio.sleep(max(0.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_GUILD_DELAY_SECONDS", 0.35) or 0.35)))
        if restored or cleared:
            logger.info("[music] restore status startup concluído | pendentes=%s resolvidos=%s limpos=%s", len(pending), restored, cleared)

    def _clear_idle_reason(self, state: MusicGuildState) -> None:
        state.idle_reason = "idle"
        state.idle_actor_id = None
        state.idle_actor_name = ""
        state.idle_channel_name = ""
        state.panel_controls_invalid_at = 0.0
        task = getattr(state, "panel_controls_invalidation_task", None)
        if task is not None and not task.done():
            task.cancel()
        state.panel_controls_invalidation_task = None

    def _set_panel_controls_invalidation(self, guild_id: int, *, delay: float = 60.0) -> None:
        state = self.get_state(guild_id)
        delay = max(0.0, float(delay))
        invalid_at = time.monotonic() + delay
        current_invalid_at = float(getattr(state, "panel_controls_invalid_at", 0.0) or 0.0)
        if current_invalid_at > time.monotonic() and current_invalid_at <= invalid_at + 0.05:
            return
        state.panel_controls_invalid_at = invalid_at
        old_task = getattr(state, "panel_controls_invalidation_task", None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay)
                st = self.get_state(guild_id)
                if float(getattr(st, "panel_controls_invalid_at", 0.0) or 0.0) <= time.monotonic():
                    await self.update_panel(guild_id, create=bool(st.now_message))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] falha ao invalidar controles do painel", exc_info=True)
            finally:
                st = self.get_state(guild_id)
                if getattr(st, "panel_controls_invalidation_task", None) is task:
                    st.panel_controls_invalidation_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
            state.panel_controls_invalidation_task = task
        except RuntimeError:
            state.panel_controls_invalidation_task = None

    def _invalidate_panel_controls_now(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        state.panel_controls_invalid_at = time.monotonic()
        task = getattr(state, "panel_controls_invalidation_task", None)
        if task is not None and not task.done():
            task.cancel()
        state.panel_controls_invalidation_task = None

    def _reactivate_panel_controls_now(self, guild_id: int) -> None:
        """Reativa controles quando uma nova sessão/faixa começa.

        O painel antigo pode ter sido renderizado como encerrado com componentes
        invalidados. Ao iniciar uma nova faixa, principalmente pelo Music Agent,
        os botões precisam voltar com uma View fresca.
        """
        state = self.get_state(guild_id)
        state.panel_controls_invalid_at = 0.0
        task = getattr(state, "panel_controls_invalidation_task", None)
        if task is not None and not task.done():
            task.cancel()
        state.panel_controls_invalidation_task = None

    def _set_idle_reason(
        self,
        state: MusicGuildState,
        reason: str,
        *,
        actor: discord.abc.User | discord.Member | None = None,
        actor_name: str = "",
        channel_name: str = "",
    ) -> None:
        state.idle_reason = (reason or "idle").strip() or "idle"
        if actor is not None:
            state.idle_actor_id = int(getattr(actor, "id", 0) or 0) or None
            state.idle_actor_name = getattr(actor, "display_name", None) or getattr(actor, "name", None) or str(actor)
        else:
            state.idle_actor_id = None
            state.idle_actor_name = actor_name or ""
        state.idle_channel_name = channel_name or ""

    def _mark_internal_voice_disconnect(self, guild_id: int, *, seconds: float = 8.0) -> None:
        state = self.get_state(guild_id)
        state.internal_voice_disconnect_until = max(state.internal_voice_disconnect_until, time.monotonic() + max(0.0, float(seconds)))

    def _is_internal_voice_disconnect_recent(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "internal_voice_disconnect_until", 0.0) or 0.0)

    def _mark_lavalink_transition(self, state: MusicGuildState, *, seconds: float = 12.0) -> None:
        state.lavalink_transition_until = max(
            float(getattr(state, "lavalink_transition_until", 0.0) or 0.0),
            time.monotonic() + max(0.0, float(seconds)),
        )

    def _is_lavalink_transition_recent(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_transition_until", 0.0) or 0.0)

    def _is_lavalink_strict_mode(self, guild_id: int | None) -> bool:
        mode_getter = getattr(self.backends, "lavalink_mode_for_guild", None)
        if not callable(mode_getter):
            return False
        try:
            return str(mode_getter(guild_id) or "").lower() == "lavalink"
        except Exception:
            return False

    def is_lavalink_active_for_guild(self, guild_id: int | None) -> bool:
        try:
            state = self.get_state(int(guild_id or 0))
        except Exception:
            return False
        return bool(
            state.current_backend == "lavalink"
            or state.current_lavalink_player is not None
            or self._is_lavalink_transition_recent(state)
        )

    def should_block_tts_local_voice(self, guild_id: int | None) -> bool:
        """Retorna se o TTS deve evitar conectar via VoiceClient local.

        O bloqueio existe apenas quando uma sessão Lavalink real está ativa ou em
        transição. Se a música atual é local/yt-dlp, o TTS deve entrar pelo
        overlay/ducking local; bloquear aqui faz o TTS simplesmente sumir.
        """
        try:
            state = self.get_state(int(guild_id or 0))
        except Exception:
            return False
        self._reset_stale_resolving_state(int(guild_id or 0), state, reason="tts_guard")
        if time.monotonic() < float(getattr(state, "tts_lavalink_local_fallback_until", 0.0) or 0.0):
            # Depois de falha real de voice state no Lavalink, permita que o cog
            # de TTS assuma um VoiceClient local por um curto período. Sem isso,
            # o próximo TTS fica bloqueado pelo Wavelink fantasma e some.
            return False
        if state.current_backend == "local":
            # Música/fallback local precisa permitir o TTS local para o ducking de 5%.
            # Wavelink antigo em guild.voice_client sem player ativo é tratado como
            # fantasma pelo cog de TTS e pode ser desconectado.
            if self._lavalink_tts_active(state) or self._lavalink_resume_grace_active(state):
                return True
            return False
        if state.current_backend == "agent" and self.should_route_tts_to_music_agent(guild_id, getattr(state, "last_voice_channel_id", 0)):
            return True
        if self._lavalink_tts_active(state) or self._lavalink_resume_grace_active(state):
            return True
        return bool(state.current_backend == "lavalink" and (state.current_lavalink_player is not None or state.current is not None))

    def is_music_staff(self, member) -> bool:
        if member is None or getattr(member, "bot", False):
            return False
        guild = getattr(member, "guild", None)
        if guild is not None and getattr(guild, "owner_id", None) == getattr(member, "id", None):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(
            getattr(perms, "administrator", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_channels", False)
            or getattr(perms, "move_members", False)
        )

    def _is_current_requester(self, state: MusicGuildState, member) -> bool:
        current = state.current
        return bool(current is not None and member is not None and int(getattr(current, "requester_id", 0) or 0) == int(getattr(member, "id", 0) or 0))

    def _prune_control_votes(self, state: MusicGuildState) -> None:
        stale = [key for key, vote in state.control_votes.items() if vote.expired()]
        for key in stale:
            state.control_votes.pop(key, None)
            task = state.control_vote_cleanup_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()

    def pending_vote_summary(self, guild_id: int) -> list[tuple[str, int, int]]:
        state = self.get_state(guild_id)
        self._prune_control_votes(state)
        labels = {"skip": "Pular", "stop": "Parar", "shuffle": "Shuffle", "loop": "Loop"}
        return [(labels.get(action, action), len(vote.voters), 2) for action, vote in state.control_votes.items() if vote.voters]

    def _schedule_vote_cleanup(self, guild_id: int, action: str) -> None:
        state = self.get_state(guild_id)
        old = state.control_vote_cleanup_tasks.get(action)
        if old is not None and not old.done():
            old.cancel()

        async def _cleanup() -> None:
            try:
                await asyncio.sleep(MUSIC_CONTROL_VOTE_SECONDS + 0.25)
                st = self.get_state(guild_id)
                vote = st.control_votes.get(action)
                if vote is not None and vote.expired():
                    st.control_votes.pop(action, None)
                    await self.update_panel(guild_id, create=bool(st.now_message))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] falha ao limpar votação", exc_info=True)

        try:
            state.control_vote_cleanup_tasks[action] = asyncio.create_task(_cleanup())
        except RuntimeError:
            pass

    async def _control_or_vote(self, guild_id: int, member, action: str) -> tuple[bool, str, bool]:
        state = self.get_state(guild_id)
        self._prune_control_votes(state)
        if member is None or getattr(member, "bot", False):
            return False, "Bots não podem controlar essa ação.", False
        if self.is_music_staff(member) or self._is_current_requester(state, member):
            state.control_votes.pop(action, None)
            task = state.control_vote_cleanup_tasks.pop(action, None)
            if task is not None and not task.done():
                task.cancel()
            return True, "", False
        vote = state.control_votes.get(action)
        if vote is None or vote.expired():
            vote = ControlVote(action=action)
            state.control_votes[action] = vote
        vote.voters.add(int(member.id))
        if len(vote.voters) >= 2:
            state.control_votes.pop(action, None)
            task = state.control_vote_cleanup_tasks.pop(action, None)
            if task is not None and not task.done():
                task.cancel()
            return True, "", True
        self._schedule_vote_cleanup(guild_id, action)
        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        label = {"skip": "pular", "stop": "parar", "shuffle": "shuffle", "loop": "repetição"}.get(action, action)
        return False, f"`🗳️` Voto registrado para **{label}**: `1/2`.", False

    def is_music_active(self, guild_id: int) -> bool:
        state = self._states.get(int(guild_id))
        if state is not None:
            self._reset_stale_resolving_state(int(guild_id), state, reason="is_music_active")
        return bool(
            state
            and (
                state.current
                or self._has_pending_track(state)
                or state.current_lavalink_player is not None
                or self._is_lavalink_transition_recent(state)
                or (state.worker_task and not state.worker_task.done())
            )
        )

    def should_defer_tts_auto_leave(self, guild_id: int) -> bool:
        state = self._states.get(int(guild_id))
        if state is None:
            return False
        self._reset_stale_resolving_state(int(guild_id), state, reason="tts_auto_leave")
        return bool(
            state.music_session_active
            or state.music_afk_expired
            or state.current
            or self._has_pending_track(state)
            or state.current_resolve_task
            or state.current_source
            or state.current_lavalink_player is not None
            or self._is_lavalink_transition_recent(state)
        )

    def _should_use_music_agent_queue_controls(self, state: MusicGuildState) -> bool:
        """Retorna se controles de fila devem ir para o Music Agent.

        O painel pode estar espelhando uma fila remota grande mesmo quando a fila
        local da VPS está vazia ou contém só o preview. Shuffle/repetição/queue
        controls precisam usar o estado remoto nesse caso, senão aparecem erros
        falsos como "não há músicas suficientes" em playlists.
        """
        if not (bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and self.music_worker_only_enabled()):
            return False
        backend = str(getattr(state, "current_backend", "") or "").lower()
        if backend == "agent":
            return True
        try:
            if int(getattr(state, "agent_remote_queue_size", 0) or 0) > 0:
                return True
        except Exception:
            pass
        return bool(getattr(state, "last_voice_channel_id", 0) and getattr(state, "current", None) is not None)

    def _agent_guild_state_from_status(self, payload: dict[str, Any] | None, guild_id: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        guilds = payload.get("guilds")
        if not isinstance(guilds, dict):
            return {}
        state = guilds.get(str(int(guild_id))) or guilds.get(int(guild_id))
        return state if isinstance(state, dict) else {}

    async def _refresh_music_agent_control_state(self, guild_id: int, *, create_panel: bool = False) -> dict[str, Any]:
        """Busca um snapshot remoto curto e espelha no painel/estado local."""
        try:
            payload = await _music_agent_status(timeout_seconds=min(2.5, float(getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0) or 5.0)))
        except Exception:
            return {}
        remote = self._agent_guild_state_from_status(payload, guild_id)
        if remote:
            state = self.get_state(guild_id)
            await self.sync_music_agent_state(
                int(guild_id),
                state.current,
                remote,
                voice_channel_id=int(remote.get("voice_channel_id") or getattr(state, "last_voice_channel_id", 0) or 0) or None,
                text_channel_id=int(remote.get("text_channel_id") or getattr(state, "last_text_channel_id", 0) or 0) or None,
                queued=False,
                create_panel=create_panel,
            )
        return remote

    async def _music_auto_leave_enabled(self, guild_id: int) -> bool:
        tts_cog = None
        getter = getattr(self.bot, "get_cog", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                tts_cog = getter("TTSVoice")
        toggle_getter = getattr(tts_cog, "_get_guild_toggle_value", None) if tts_cog is not None else None
        if callable(toggle_getter):
            try:
                result = toggle_getter(
                    int(guild_id),
                    public_key="auto_leave",
                    raw_key="auto_leave_enabled",
                    default=True,
                )
                if asyncio.iscoroutine(result):
                    result = await result
                return bool(result)
            except Exception:
                return True
        return True

    def _voice_channel_has_humans(self, channel: Any) -> bool:
        if channel is None:
            return False
        try:
            return any(not getattr(member, "bot", False) for member in getattr(channel, "members", []) or [])
        except Exception:
            return False

    async def handle_music_voice_occupancy_update(self, guild_id: int, *, auto_leave_enabled: bool = True) -> None:
        state = self.get_state(guild_id)
        if not auto_leave_enabled:
            self._cancel_music_idle_disconnect(state)
            return
        if not self.should_defer_tts_auto_leave(guild_id):
            return
        guild = self.bot.get_guild(int(guild_id))
        vc = getattr(guild, "voice_client", None) if guild is not None else None
        channel = getattr(vc, "channel", None)
        has_humans = self._voice_channel_has_humans(channel)
        if has_humans:
            # Antes dos 2 minutos, a volta de alguém cancela a contagem. Depois que
            # o estado AFK expirou, a saída final não é cancelada por presença tardia.
            if not state.music_afk_expired:
                self._cancel_music_idle_disconnect(state)
            return
        await self.schedule_music_idle_disconnect(guild_id)

    async def schedule_music_idle_disconnect(self, guild_id: int, *, delay: float | None = None) -> None:
        state = self.get_state(guild_id)
        if state.music_idle_disconnect_task is not None and not state.music_idle_disconnect_task.done():
            return
        delay_seconds = MUSIC_IDLE_DISCONNECT_SECONDS if delay is None else max(0.0, float(delay))
        state.music_afk_expired = False

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                st = self.get_state(guild_id)
                if not await self._music_auto_leave_enabled(guild_id):
                    st.music_afk_expired = False
                    return
                st.music_afk_expired = True
                # Estado AFK: a sessão musical expirou. A presença de alguém depois
                # daqui não reinicia nem cancela a saída final.
                await asyncio.sleep(MUSIC_AFK_DISCONNECT_GRACE_SECONDS)
                if not await self._music_auto_leave_enabled(guild_id):
                    st.music_afk_expired = False
                    return
                guild = self.bot.get_guild(int(guild_id))
                if guild is not None:
                    await self._disconnect_music_afk(guild, self.get_state(guild_id))
            finally:
                st = self.get_state(guild_id)
                if st.music_idle_disconnect_task is task:
                    st.music_idle_disconnect_task = None

        task = asyncio.create_task(_runner())
        task.add_done_callback(_consume_expected_music_exception)
        state.music_idle_disconnect_task = task

    def _cancel_music_idle_disconnect(self, state: MusicGuildState) -> None:
        task = state.music_idle_disconnect_task
        current_task = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        if task is not None and not task.done() and task is not current_task:
            task.cancel()
        state.music_idle_disconnect_task = None
        state.music_afk_expired = False

    async def close(self) -> None:
        for task in list(getattr(self, "_lavalink_shadow_tasks", {}).values()):
            if task is not None and not task.done():
                task.cancel()
        self._lavalink_shadow_tasks.clear()
        for guild_id in list(self._states):
            with contextlib.suppress(Exception):
                await self.stop(guild_id, disconnect=False)
        with contextlib.suppress(Exception):
            await self.backends.close()

    async def backend_status(self, guild_id: int | None = None):
        return await self.backends.status(guild_id=guild_id)

    async def test_lavalink_backend(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
    ):
        return await self.backends.test_lavalink(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=guild_id,
        )

    async def update_lavalink_node_config(
        self,
        *,
        node_name: str,
        host: str,
        port: int,
        password: str | None,
        secure: bool,
        guild_id: int | None = None,
    ):
        return await self.backends.update_lavalink_node(
            node_name=node_name,
            host=host,
            port=port,
            password=password,
            secure=secure,
            guild_id=guild_id,
        )

    async def set_lavalink_mode(self, mode: str, *, guild_id: int | None = None):
        return await self.backends.set_lavalink_mode(mode, guild_id=guild_id)

    async def clear_lavalink_config(self, *, guild_id: int | None = None):
        return await self.backends.clear_lavalink_config(guild_id=guild_id)

    async def update_lavalink_panel_options(self, **options):
        return await self.backends.update_lavalink_panel_options(**options)

    def lavalink_config_summary(self, guild_id: int | None = None) -> dict:
        return self.backends.lavalink_config_summary(guild_id=guild_id)

    def backend_runtime_summary(self, guild_id: int | None = None) -> dict:
        return self.backends.compact_runtime_summary(guild_id=guild_id)

    def last_lavalink_shadow_result(self, guild_id: int | None = None):
        return self.backends.last_lavalink_shadow_result(guild_id=guild_id)

    def should_run_lavalink_shadow(self, guild_id: int | None = None) -> bool:
        return self.backends.should_shadow_lavalink(guild_id=guild_id)

    def schedule_lavalink_shadow_search(
        self,
        guild_id: int,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        reason: str = "play",
    ) -> bool:
        query = str(query or "").strip()
        if not query or not self.should_run_lavalink_shadow(guild_id):
            return False
        guild_key = int(guild_id)
        current = self._lavalink_shadow_tasks.get(guild_key)
        if current is not None and not current.done():
            logger.debug("[music/lavalink-shadow] já existe teste ativo | guild=%s", guild_id)
            return False

        async def _runner() -> None:
            try:
                await self.backends.shadow_lavalink_search(
                    query,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    guild_id=guild_key,
                    reason=reason,
                )
            finally:
                if self._lavalink_shadow_tasks.get(guild_key) is task:
                    self._lavalink_shadow_tasks.pop(guild_key, None)

        task = asyncio.create_task(_runner())
        task.add_done_callback(_consume_expected_music_exception)
        self._lavalink_shadow_tasks[guild_key] = task
        logger.debug("[music/lavalink-shadow] teste paralelo agendado | guild=%s query=%r", guild_id, query)
        return True

    async def enqueue(self, guild: discord.Guild, voice_channel: discord.abc.Connectable, text_channel: discord.abc.Messageable, tracks: list[MusicTrack]) -> tuple[int, int]:
        if not tracks:
            return 0, 0
        state = self.get_state(guild.id)
        state.last_text_channel_id = getattr(text_channel, "id", None)
        state.last_voice_channel_id = getattr(voice_channel, "id", None)
        state.music_session_active = True
        self._clear_idle_reason(state)
        self._cancel_music_idle_disconnect(state)

        added = 0
        dropped = 0
        seen = self._current_track_keys(state)
        for track in tracks:
            keys = self._track_keys(track)
            if keys and any(key in seen for key in keys):
                dropped += 1
                continue
            if state.queue.full():
                dropped += 1
                continue
            await state.queue.put(track)
            seen.update(keys)
            added += 1
        if added:
            state.stop_requested = False
            self.ensure_music_worker(guild.id)
            if state.current is not None or state.current_source is not None:
                self._start_prefetch_next(guild.id, state)
            self._schedule_panel_update(guild.id, create=True)
        return added, dropped

    def _track_keys(self, track: MusicTrack) -> set[str]:
        keys: set[str] = set()
        url = (track.webpage_url or track.original_url or "").strip().lower()
        if url:
            keys.add("url:" + url)
        title_key = compact_key(track.title)
        if title_key:
            duration_bucket = ""
            if track.duration is not None:
                duration_bucket = str(int(max(0.0, float(track.duration)) // 8))
            keys.add("title:" + title_key + ":" + duration_bucket)
        return keys

    def _current_track_keys(self, state: MusicGuildState) -> set[str]:
        keys: set[str] = set()
        if state.current is not None:
            keys.update(self._track_keys(state.current))
        for item in list(getattr(state, "forward_queue", []) or []):
            keys.update(self._track_keys(item))
        for item in list(getattr(state.queue, "_queue", [])):
            keys.update(self._track_keys(item))
        return keys

    def _pending_items(self, state: MusicGuildState) -> list[MusicTrack]:
        """Retorna próximas músicas na ordem real de avanço.

        ``forward_queue`` guarda músicas que ficaram à frente quando o usuário
        voltou no histórico. Ela precisa ter prioridade sobre o queue normal para
        que o fluxo A → B → voltar A → avançar B funcione mesmo com só 2 músicas.
        """
        items: list[MusicTrack] = []
        with contextlib.suppress(Exception):
            items.extend(list(getattr(state, "forward_queue", []) or []))
        with contextlib.suppress(Exception):
            items.extend(list(getattr(state.queue, "_queue", [])))
        return items

    def _has_pending_track(self, state: MusicGuildState) -> bool:
        return bool(self._pending_items(state))

    async def _get_next_worker_track(self, state: MusicGuildState, *, timeout: float) -> tuple[MusicTrack, bool]:
        """Obtém a próxima música e informa se veio do asyncio.Queue.

        Músicas do ``forward_queue`` não podem chamar ``queue.task_done()``.

        O ``forward_queue`` não acorda tasks bloqueadas em ``asyncio.Queue.get``.
        Isso quebrava o botão ⏮️ quando o player já estava em "Nada tocando":
        a música anterior era colocada na frente, o painel mostrava "Queue
        pronto", mas o worker seguia dormindo no queue normal. Por isso fazemos
        pequenas esperas periódicas, preservando o timeout total de idle.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        poll_interval = 0.35
        while True:
            if getattr(state, "forward_queue", None):
                try:
                    return state.forward_queue.popleft(), False
                except IndexError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                return await asyncio.wait_for(state.queue.get(), timeout=min(poll_interval, remaining)), True
            except asyncio.TimeoutError:
                continue

    def _panel_key_for_track(self, track: MusicTrack | None) -> str | None:
        if track is None:
            return None
        url = (track.display_url or track.webpage_url or track.original_url or "").strip().lower()
        if url:
            return "url:" + url
        title_key = compact_key(track.title)
        duration = ""
        if track.duration is not None:
            with contextlib.suppress(Exception):
                duration = str(int(max(0.0, float(track.duration))))
        return f"title:{title_key}:{duration}" if title_key else None

    def _track_resolve_key(self, track: MusicTrack | None, audio_max_abr: int | None = None) -> str:
        if track is None:
            return ""
        url = (track.webpage_url or track.original_url or track.stream_url or "").strip().lower()
        base = "url:" + url if url else ""
        if not base:
            title_key = compact_key(track.title)
            duration = ""
            if track.duration is not None:
                with contextlib.suppress(Exception):
                    duration = str(int(max(0.0, float(track.duration))))
            base = f"title:{title_key}:{duration}" if title_key else ""
        if not base:
            return ""
        try:
            max_abr = int(audio_max_abr or 0)
        except Exception:
            max_abr = 0
        return f"{base}|abr:{max_abr}"

    def _cancel_next_prefetch(self, state: MusicGuildState) -> None:
        task = state.next_resolve_task
        if task is not None and not task.done():
            task.cancel()
        state.next_resolve_task = None
        state.next_resolve_key = ""
        state.next_resolve_active_key = ""

    def _active_player_count(self) -> int:
        total = 0
        for st in self._states.values():
            if st.current or st.current_source or st.current_status in {"resolving", "starting", "playing", "paused"}:
                total += 1
        return total

    def _audio_max_abr_for_load(self) -> int | None:
        """Escolhe qualidade por carga sem aumentar RAM.

        Com um único servidor ativo, retorna None para usar o melhor áudio-only
        disponível, sem teto de abr. Quando há mais guilds tocando, limita
        bitrate para reduzir trabalho do yt-dlp/FFmpeg/rede.
        """
        mode = MUSIC_AUDIO_MODE
        active = max(1, self._active_player_count())
        if mode in {"high", "alta", "quality"}:
            return None
        if mode in {"low", "economy", "economico", "econômico", "stable"}:
            return MUSIC_MAX_AUDIO_BITRATE_STABLE
        if active <= MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS:
            return None
        if active >= 3:
            return MUSIC_HEAVY_LOAD_MAX_ABR
        return MUSIC_MAX_AUDIO_BITRATE_STABLE

    def _track_stream_matches_quality(self, track: MusicTrack | None, audio_max_abr: int | None) -> bool:
        if track is None or not track.stream_url:
            return False
        try:
            requested_abr = int(audio_max_abr or 0)
        except Exception:
            requested_abr = 0
        resolved_abr = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        is_direct_track = str(getattr(track, "extractor", "") or "").lower() == "direct"
        return bool(is_direct_track or (not requested_abr and not resolved_abr) or (requested_abr and resolved_abr == requested_abr))

    def _prefetch_delay_for_current(self, state: MusicGuildState) -> float:
        delay = float(MUSIC_PREFETCH_MIN_DELAY_SECONDS)
        current = state.current
        if current is None or current.is_live or current.duration is None:
            return delay
        started_at = float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0)
        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        remaining = max(0.0, float(current.duration) - elapsed)
        if remaining <= MUSIC_PREFETCH_BEFORE_END_SECONDS:
            return delay
        return max(delay, remaining - MUSIC_PREFETCH_BEFORE_END_SECONDS)

    def _start_prefetch_next(self, guild_id: int, state: MusicGuildState) -> None:
        if self.music_worker_only_enabled():
            return
        with contextlib.suppress(Exception):
            if self.backends.should_use_lavalink_real(guild_id):
                # Em modo node de áudio real, a próxima faixa deve ser resolvida
                # pelo node no momento do playback. Prefetch local chamaria yt-dlp e
                # poderia bloquear o event loop/voice heartbeat.
                return
        if not MUSIC_PREFETCH_NEXT or not self._has_pending_track(state):
            return
        if MUSIC_MAX_GLOBAL_PREFETCH <= 0:
            return
        if MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS and self._active_player_count() > MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS:
            return
        if self._global_prefetch_active >= MUSIC_MAX_GLOBAL_PREFETCH:
            return
        try:
            next_track = self._pending_items(state)[0]
        except Exception:
            return
        target_abr = self._audio_max_abr_for_load()
        if next_track is None or self._track_stream_matches_quality(next_track, target_abr):
            return
        key = self._track_resolve_key(next_track, target_abr)
        task = state.next_resolve_task
        if task is not None and not task.done() and state.next_resolve_key == key:
            return
        if task is not None and not task.done():
            task.cancel()

        async def _prefetch() -> None:
            counted_active = False
            try:
                # Baixa prioridade: dá tempo ao player atual e evita resolver a próxima
                # música logo no início da faixa, que era quando mais causava cortes.
                await asyncio.sleep(self._prefetch_delay_for_current(state))
                if state.next_resolve_key != key:
                    return
                if MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS and self._active_player_count() > MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS:
                    return
                if self._global_prefetch_active >= MUSIC_MAX_GLOBAL_PREFETCH:
                    return
                self._global_prefetch_active += 1
                counted_active = True
                state.next_resolve_active_key = key
                await self.extractor.resolve_stream(next_track, force=False, audio_max_abr=target_abr)
                logger.debug("[music] próxima música pré-resolvida | guild=%s track=%r", guild_id, next_track.title)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] pré-resolução da próxima música falhou | guild=%s track=%r", guild_id, getattr(next_track, "title", ""), exc_info=True)
            finally:
                if state.next_resolve_active_key == key:
                    state.next_resolve_active_key = ""
                if counted_active:
                    self._global_prefetch_active = max(0, self._global_prefetch_active - 1)

        state.next_resolve_key = key
        state.next_resolve_task = asyncio.create_task(_prefetch())
        state.next_resolve_task.add_done_callback(_consume_expected_music_exception)

    async def _resolve_current_track(self, state: MusicGuildState, track: MusicTrack) -> None:
        if self._track_uses_worker_local_stream(track):
            # O stream já é um endpoint PCM do phone worker. Não chame o extractor
            # local da VPS, que acionaria yt-dlp/FFmpeg local e quebraria worker-only.
            return
        if self.music_worker_only_enabled() and str(getattr(track, "extractor", "") or "").lower() == "worker-ytdlp" and not str(getattr(track, "stream_url", "") or "").strip():
            # A busca textual em worker-only agora é leve: ela traz apenas
            # metadados para montar 5 opções rapidamente. Só ao tocar a faixa
            # escolhida resolvemos stream real, endpoint PCM e FFmpeg no celular.
            source = str(getattr(track, "webpage_url", "") or getattr(track, "original_url", "") or getattr(track, "title", "") or "").strip()
            if not source:
                raise MusicExtractionError("A música não tem origem para resolver no worker.")
            batch = await _resolve_music_tracks_on_worker(
                source,
                requester_id=int(getattr(track, "requester_id", 0) or 0),
                requester_name=str(getattr(track, "requester_name", "") or ""),
                limit=1,
                metadata_only=False,
            )
            if not batch.tracks:
                raise MusicExtractionError("O worker não retornou stream para a música escolhida.")
            resolved = batch.tracks[0]
            for attr in (
                "stream_url",
                "lavalink_query",
                "lavalink_resolved",
                "source",
                "extractor",
                "duration",
                "uploader",
                "thumbnail",
                "display_source",
                "is_live",
            ):
                value = getattr(resolved, attr, None)
                if value not in (None, ""):
                    setattr(track, attr, value)
            if getattr(resolved, "webpage_url", ""):
                track.webpage_url = resolved.webpage_url
            if getattr(resolved, "original_url", ""):
                track.original_url = resolved.original_url
            if not track.stream_url:
                raise MusicExtractionError("O worker não retornou URL de áudio local.")
            return
        target_abr = self._audio_max_abr_for_load()
        key = self._track_resolve_key(track, target_abr)
        task = state.next_resolve_task if state.next_resolve_key == key else None
        if task is not None:
            state.next_resolve_task = None
            state.next_resolve_key = ""
            if task.done():
                # Propaga exceção caso a pré-resolução tenha falhado; o fallback abaixo tenta de novo.
                with contextlib.suppress(Exception):
                    task.result()
                if track.stream_url:
                    self._refresh_quality_state(state, track, cap=target_abr)
                    return
            else:
                if state.next_resolve_active_key != key:
                    # A pré-resolução ainda estava apenas aguardando o momento certo.
                    # Quando a música virou atual por fim/skip, cancela o atraso e
                    # resolve imediatamente para não criar pausa gigante entre faixas.
                    task.cancel()
                    state.next_resolve_key = ""
                else:
                    state.current_resolve_task = task
                    try:
                        await task
                        if track.stream_url:
                            self._refresh_quality_state(state, track, cap=target_abr)
                            return
                    finally:
                        if state.current_resolve_task is task:
                            state.current_resolve_task = None
        resolve_task = asyncio.create_task(self.extractor.resolve_stream(track, force=False, audio_max_abr=target_abr))
        resolve_task.add_done_callback(_consume_expected_music_exception)
        state.current_resolve_task = resolve_task
        try:
            await resolve_task
            self._refresh_quality_state(state, track, cap=target_abr)
        finally:
            if state.current_resolve_task is resolve_task:
                state.current_resolve_task = None

    def ensure_music_worker(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.stop_requested = False
            state.worker_task = asyncio.create_task(self._music_worker_loop(int(guild_id)))
            state.worker_task.add_done_callback(_consume_expected_music_exception)

    async def _music_worker_loop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        try:
            while not state.stop_requested:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return

                track_from_queue = False
                try:
                    track, track_from_queue = await self._get_next_worker_track(
                        state,
                        timeout=MUSIC_IDLE_DISCONNECT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._maybe_disconnect_idle(guild, state)
                    return

                # Assim que o worker retira a próxima música do queue, ela já vira
                # a música atual do estado. Isso evita uma janela visual durante skip
                # onde o painel via queue vazio + current vazio e mostrava
                # "Nada tocando agora" antes da próxima faixa começar.
                state.current = track
                self._set_current_status(state, "resolving")
                state.current_backend = "local"
                state.skip_transition_active = False

                played_ok = False
                playback_failed = False
                playback_failed_before_start = False
                playback_failure_label = ""
                state.skip_requested = False
                state.control_votes.clear()
                for task in list(state.control_vote_cleanup_tasks.values()):
                    if task is not None and not task.done():
                        task.cancel()
                state.control_vote_cleanup_tasks.clear()
                try:
                    played_ok = await self._play_track(guild, state, track)
                except MusicWorkerUnavailable as exc:
                    playback_failed = True
                    playback_failed_before_start = True
                    playback_failure_label = self._exc_label(exc)
                    if not state.skip_requested and not state.stop_requested:
                        logger.warning("[music/worker] worker indisponível durante playback | guild=%s track=%r erro=%s", guild_id, track.title, self._exc_label(exc))
                        await self._send_text(guild, state, MUSIC_WORKER_UNAVAILABLE_MESSAGE)
                except MusicWorkerEngineUnavailable as exc:
                    playback_failed = True
                    playback_failed_before_start = True
                    playback_failure_label = MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
                    if not state.skip_requested and not state.stop_requested:
                        logger.warning("[music/worker] engine musical indisponível | guild=%s track=%r erro=%s", guild_id, track.title, self._exc_label(exc))
                        await self._send_text(guild, state, MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE)
                except Exception as exc:
                    playback_failed = True
                    playback_failed_before_start = not bool(float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0))
                    playback_failure_label = self._public_playback_failure_label(exc)
                    if not state.skip_requested and not state.stop_requested:
                        logger.warning("[music] Falha ao tocar | guild=%s track=%r erro=%s", guild_id, track.title, self._exc_label(exc))
                        await self._send_text(guild, state, f"⚠️ Não consegui iniciar **{track.short_title}**. Se houver outra música no queue, vou tentar a próxima.")
                finally:
                    state.current_source = None
                    state.current_resolve_task = None
                    started_before_finish = bool(float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0) > 0.0)
                    should_history_natural = bool(played_ok and not state.stop_requested and not state.skip_requested)
                    should_history_skip = bool(
                        state.skip_requested
                        and not state.stop_requested
                        and started_before_finish
                        and not state.skip_history_suppressed_once
                    )
                    if should_history_natural or should_history_skip:
                        self._push_history(state, track)
                    if played_ok and state.loop_mode is LoopMode.ONE and not state.stop_requested and not state.skip_requested:
                        with contextlib.suppress(Exception):
                            # Repetir a atual antes de qualquer item já presente na fila.
                            state.queue._queue.appendleft(track)
                    elif played_ok and state.loop_mode is LoopMode.ALL and not state.stop_requested and not state.skip_requested:
                        with contextlib.suppress(Exception):
                            await state.queue.put(track)
                    skip_to_next = bool(state.skip_requested and not state.stop_requested and self._has_pending_track(state))
                    state.current = None
                    state.current_started_at_monotonic = 0.0
                    state.current_start_offset_seconds = 0.0
                    state.current_backend = "local"
                    state.current_lavalink_player = None
                    state.current_lavalink_playable = None
                    state.paused = False
                    state.skip_history_suppressed_once = False
                    state.skip_transition_active = skip_to_next
                    self._set_current_status(state, "skipping" if skip_to_next else ("idle" if not self._has_pending_track(state) else "queued"))
                    if track_from_queue:
                        with contextlib.suppress(Exception):
                            state.queue.task_done()
                    # Durante um skip com próxima música no queue, não renderiza um
                    # estado intermediário vazio/queued. A próxima iteração já vai
                    # definir current e redesenhar o painel como preparando/tocando.
                    if not skip_to_next:
                        if not state.stop_requested and not self._has_pending_track(state):
                            if playback_failed and playback_failed_before_start:
                                self._set_idle_reason(
                                    state,
                                    "track_failed",
                                    actor_name=getattr(track, "short_title", None) or getattr(track, "title", "") or "essa música",
                                )
                                state.idle_channel_name = playback_failure_label[:160]
                            else:
                                self._set_idle_reason(state, "queue_finished")
                            if not state.history:
                                self._set_panel_controls_invalidation(guild_id, delay=60.0)
                            await self.schedule_music_idle_disconnect(guild_id)
                        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        finally:
            state.worker_task = None
            if state.stop_requested or not self._has_pending_track(state):
                if not state.stop_requested and not self._has_pending_track(state):
                    if str(getattr(state, "idle_reason", "") or "") != "track_failed":
                        self._set_idle_reason(state, "queue_finished")
                    if not state.history:
                        self._set_panel_controls_invalidation(guild_id, delay=60.0)
                state.current = None
                state.current_started_at_monotonic = 0.0
                state.current_start_offset_seconds = 0.0
                state.current_backend = "local"
                state.current_lavalink_player = None
                state.current_lavalink_playable = None
                state.skip_transition_active = False
                state.skip_history_suppressed_once = False
                self._set_current_status(state, "idle")
                guild = self.bot.get_guild(int(guild_id))
                restore_reason = "queue_finished" if not self._has_pending_track(state) else "stop_requested"
                if state.stop_requested and str(getattr(state, "idle_reason", "") or "") == "manual_stop":
                    restore_reason = "manual_stop"
                elif state.stop_requested:
                    restore_reason = "stop_requested"
                await self._restore_auto_bitrate_for_state(guild, state, reason=restore_reason)
                await self._restore_voice_status_for_state(guild, state, reason=restore_reason)
                self._schedule_panel_update(guild_id, create=False)

    def _track_is_direct_youtube_request(self, track: MusicTrack) -> bool:
        """Link direto do YouTube sempre toca pelo yt-dlp local.

        Não tenta Lavalink, não tenta scsearch/mirror e não faz fallback que
        atrase. Pesquisa textual/seleção continua podendo usar o fluxo normal;
        aqui só bloqueamos quando a entrada original do usuário foi um link
        real do YouTube/YouTube Music/Shorts.
        """
        try:
            original_raw = str(getattr(track, "original_url", "") or "").strip()
            if original_raw and describe_url(original_raw).is_youtube:
                return True
        except Exception:
            pass
        return False

    def _track_is_youtube_selection(self, track: MusicTrack) -> bool:
        """Resultado de pesquisa do YouTube: tenta LavaSrc, mas pode cair local."""
        if self._track_is_direct_youtube_request(track):
            return False
        values = (
            str(getattr(track, "source", "") or "").lower(),
            str(getattr(track, "extractor", "") or "").lower(),
        )
        if any(value in {"youtube", "yt", "ytsearch", "ytmsearch"} for value in values):
            return True
        with contextlib.suppress(Exception):
            if describe_url(str(getattr(track, "webpage_url", "") or "")).is_youtube:
                return True
        return False

    def _exc_label(self, exc: BaseException | None) -> str:
        if exc is None:
            return ""
        text = str(exc or "").strip()
        detail = str(getattr(exc, "detail", "") or "").strip()
        if detail and detail not in text:
            compact_detail = " ".join(detail.split())[:180]
            text = f"{text} — {compact_detail}" if text else compact_detail
        name = exc.__class__.__name__
        return f"{name}: {text}" if text else name

    def _track_should_preserve_official_display(self, track: MusicTrack, meta: dict | None = None) -> bool:
        values = " ".join(
            str(value or "").lower()
            for value in (
                getattr(track, "display_source", ""),
                getattr(track, "source", ""),
                getattr(track, "extractor", ""),
                getattr(track, "original_url", ""),
                getattr(track, "webpage_url", ""),
                (meta or {}).get("query", "") if isinstance(meta, dict) else "",
            )
        )
        return bool(
            getattr(track, "display_title", "")
            or "spotify" in values
            or "deezer" in values
            or "apple" in values
            or "metadata" in values
        )

    def _lavalink_position_ms(self, player: Any) -> int:
        for attr in ("position", "last_position"):
            value = getattr(player, attr, None)
            try:
                if callable(value):
                    value = value()
                if value is not None:
                    return max(0, int(float(value)))
            except Exception:
                continue
        return 0

    def _lavalink_premature_end_reason(self, track: MusicTrack, *, played_seconds: float, position_ms: int) -> str:
        if getattr(track, "is_live", False):
            return ""
        duration = getattr(track, "duration", None)
        try:
            duration_seconds = float(duration or 0.0)
        except Exception:
            duration_seconds = 0.0
        observed = max(float(played_seconds or 0.0), float(position_ms or 0) / 1000.0)
        if duration_seconds <= 0.0:
            return ""
        remaining = max(0.0, duration_seconds - observed)
        if observed < MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS and remaining > MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS:
            return f"tocou só {observed:.1f}s de {duration_seconds:.1f}s"
        if duration_seconds >= 90.0 and observed / max(duration_seconds, 1.0) < 0.70 and remaining > MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS:
            return f"tocou {observed / max(duration_seconds, 1.0):.0%} da faixa ({observed:.1f}s/{duration_seconds:.1f}s)"
        return ""

    def _prepare_track_for_local_after_lavalink_failure(self, track: MusicTrack, exc: Exception | None = None) -> bool:
        """Converte mirrors quebrados do LavaSrc em metadata para fallback local.

        SoundCloud/Spotify podem resolver metadata no Lavalink e quebrar só na hora
        do stream real. Nesses casos não adianta entregar a mesma URL ao yt-dlp: ele
        costuma repetir 404 ou erro de extractor. O fallback local deve usar título,
        artista e duração para buscar uma fonte equivalente pelo player local.
        """
        values = " ".join(
            str(value or "").lower()
            for value in (
                getattr(track, "source", ""),
                getattr(track, "extractor", ""),
                getattr(track, "webpage_url", ""),
                getattr(track, "original_url", ""),
            )
        )
        # A mensagem de erro do LavaSrc pode citar SoundCloud mesmo quando o track
        # original era um resultado do YouTube. Só converte para metadata se a
        # própria faixa veio de SoundCloud/Spotify; caso contrário, o fallback
        # local deve tocar a URL original do YouTube selecionada pelo usuário.
        if not any(token in values for token in ("soundcloud", "spotify", "spsearch", "scsearch", "lavasrc")):
            return False
        title = str(getattr(track, "title", "") or "").strip()
        generic_titles = {"", "link", "soundcloud link", "spotify link", "música sem título", "musica sem titulo", "unknown title"}
        if title.lower() in generic_titles:
            return False
        track.stream_url = ""
        track.webpage_url = ""
        track.extractor = "metadata"
        track.lavalink_playable = None
        track.lavalink_encoded = ""
        track.lavalink_resolved = False
        track.resolved_at_monotonic = 0.0
        track.resolved_audio_max_abr = 0
        track.resolved_audio_abr = 0
        track.resolved_audio_ext = ""
        track.resolved_audio_codec = ""
        track.resolved_audio_format_id = ""
        if "spotify" in values:
            track.fallback_reason = "Spotify"
            if "fallback local" not in str(track.source or "").lower():
                track.source = "Spotify → fallback local"
        elif "soundcloud" in values or "scsearch" in values:
            track.fallback_reason = "SoundCloud"
            if "fallback local" not in str(track.source or "").lower():
                track.source = "SoundCloud → fallback local"
        else:
            track.fallback_reason = "Lavalink"
        return True

    def _start_youtube_direct_metadata_refresh(self, guild_id: int, state: MusicGuildState, track: MusicTrack) -> None:
        """Hidrata metadata de link direto do YouTube em background.

        A chamada é fire-and-forget de propósito: YouTube API, oEmbed ou qualquer
        lookup de metadata nunca pode participar do caminho crítico de playback.
        Ela pode rodar enquanto o stream local ainda está resolvendo; assim o
        painel não fica preso em ``YouTube <id>``/``desconhecida``.
        """
        if not self._track_is_direct_youtube_request(track):
            return

        async def _runner() -> None:
            try:
                # Dá preferência ao caminho de áudio, mas não espera o start do
                # FFmpeg: metadata visual deve atualizar o painel mesmo se o
                # stream ainda estiver em resolução ou vier a falhar.
                await asyncio.sleep(0.05)
                if state.current is not track or state.stop_requested:
                    return
                changed = await self.extractor.hydrate_youtube_direct_metadata(track)
                if not changed:
                    return
                if state.current is not track or state.stop_requested:
                    return
                logger.info(
                    "[music] metadata YouTube direta aplicada em background | guild=%s track=%r",
                    guild_id,
                    getattr(track, "title", ""),
                )
                self._mark_voice_status_track_change(state)
                self._schedule_voice_status_track_sync(guild_id, repeat_after=0.5, reason="youtube_direct_metadata")
                await self.update_panel(guild_id, create=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] metadata YouTube direta em background falhou | guild=%s", guild_id, exc_info=True)

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
        except RuntimeError:
            return

    async def _disconnect_lavalink_before_local_fallback(self, guild: discord.Guild, state: MusicGuildState, *, reason: str = "fallback") -> None:
        current_vc = guild.voice_client
        if not self._is_lavalink_voice_client(current_vc):
            return
        self._mark_lavalink_transition(state, seconds=10.0)
        self._mark_internal_voice_disconnect(guild.id, seconds=10.0)
        with contextlib.suppress(Exception):
            await self._vc_stop_audio(current_vc)
        with contextlib.suppress(Exception):
            stopper = getattr(current_vc, "stop", None)
            if callable(stopper):
                result = stopper()
                if asyncio.iscoroutine(result):
                    await result
        with contextlib.suppress(Exception):
            await current_vc.disconnect(force=True)
        # Dá um pequeno tempo para o gateway liberar a sessão de voz antes do
        # VoiceClient local tentar entrar, evitando Already connected/timeout.
        await asyncio.sleep(0.8)

    async def _play_track_lavalink(
        self,
        guild: discord.Guild,
        state: MusicGuildState,
        track: MusicTrack,
        channel: discord.abc.Connectable,
    ) -> bool:
        """Toca uma faixa pelo Lavalink real.

        A queue, painel e status continuam no AudioRouter. Em modo Lavalink o
        erro é tratado sem cair para voice local; em modo Auto, o caller ainda
        pode usar fallback local.
        """
        state.current_backend = "lavalink"
        state.current_source = None
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.last_lavalink_error = ""
        self._set_current_status(state, "starting")
        self._mark_lavalink_transition(state, seconds=18.0)
        self._mark_internal_voice_disconnect(guild.id, seconds=18.0)
        await self._boost_auto_bitrate_for_music(guild, channel, state)
        await self.update_panel(guild.id, create=True)

        recoveries = max(0, int(getattr(track, "lavalink_recoveries", 0) or 0))
        while True:
            play_coro = self.backends.play_lavalink_track(
                guild,
                channel,
                track,
                volume=state.volume,
            )
            if self._track_is_youtube_selection(track):
                # Resultado escolhido no YouTube só tenta LavaSrc por um tempo
                # curto. Se o espelho não bater/abrir rápido, cai para yt-dlp
                # local em vez de deixar a reprodução parecer travada.
                player, playable, meta = await asyncio.wait_for(
                    play_coro,
                    timeout=MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS,
                )
            else:
                player, playable, meta = await play_coro
            state.current_lavalink_player = player
            state.current_lavalink_playable = playable
            if isinstance(meta, dict):
                state.current_lavalink_node_label = str(meta.get("node_label") or "")
                state.current_lavalink_node_name = str(meta.get("node_name") or "")
            else:
                state.current_lavalink_node_label = ""
                state.current_lavalink_node_name = ""
            self._mark_lavalink_transition(state, seconds=8.0)
            self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
            # Usa metadados reais do node quando eles representam a fonte escolhida.
            # Para Spotify/Deezer/Apple resolvidos por mirror LavaSrc/SoundCloud,
            # preserve a metadata oficial no painel e use o mirror só como áudio.
            if meta:
                preserve_display = self._track_should_preserve_official_display(track, meta)
                raw_duration = meta.get("duration")
                with contextlib.suppress(Exception):
                    numeric_duration = float(raw_duration)
                    if numeric_duration > 0:
                        track.duration = numeric_duration / 1000.0 if numeric_duration >= 10000 else numeric_duration
                if preserve_display:
                    track.title = str(getattr(track, "display_title", "") or track.title or meta.get("title") or "Música sem título")
                    track.uploader = str(getattr(track, "display_uploader", "") or track.uploader or "")
                    official_thumb = str(getattr(track, "display_thumbnail", "") or "")
                    if official_thumb:
                        track.thumbnail = official_thumb
                    if not str(track.source or "").strip():
                        track.source = str(getattr(track, "display_source", "") or "metadata")
                else:
                    track.title = str(meta.get("title") or track.title or "Música sem título")
                    track.uploader = str(meta.get("author") or track.uploader or "")
                    track.source = str(meta.get("source") or track.source or "lavalink")
                    artwork = str(meta.get("artwork") or "")
                    if artwork:
                        track.thumbnail = artwork
            self._set_current_status(state, "playing")
            state.current_started_at_monotonic = time.monotonic()
            state.current_quality_label = "Alta"
            state.current_quality_kbps = MUSIC_HIGH_QUALITY_MAX_ABR
            state.current_start_offset_seconds = 0.0
            self._mark_voice_status_track_change(state)
            self._schedule_voice_status_track_sync(guild.id, repeat_after=1.0, reason="lavalink_track_started")
            self._schedule_panel_update(guild.id, create=True)

            started_at = time.monotonic()
            last_explicit_active_at = started_at
            last_position_ms = 0
            while not state.stop_requested and not state.skip_requested:
                if not self._vc_is_connected(player):
                    raise MusicPlaybackError("Player Lavalink desconectou durante a música.")
                now = time.monotonic()
                explicit_active = self._vc_lavalink_explicit_playing_or_paused(player)
                has_current = getattr(player, "current", None) is not None
                with contextlib.suppress(Exception):
                    last_position_ms = max(last_position_ms, self._lavalink_position_ms(player))
                if explicit_active:
                    last_explicit_active_at = now
                    await asyncio.sleep(0.35)
                    continue
                if self._lavalink_tts_active(state) or self._lavalink_resume_grace_active(state):
                    await asyncio.sleep(0.20)
                    continue
                if has_current and (now - started_at < 4.0 or now - last_explicit_active_at < 3.0):
                    await asyncio.sleep(0.20)
                    continue
                break

            if state.stop_requested or state.skip_requested:
                return False

            played_seconds = max(0.0, time.monotonic() - started_at)
            with contextlib.suppress(Exception):
                last_position_ms = max(last_position_ms, self._lavalink_position_ms(player))
            track.lavalink_last_position_ms = int(last_position_ms)
            track.lavalink_last_played_seconds = float(played_seconds)
            premature_reason = self._lavalink_premature_end_reason(track, played_seconds=played_seconds, position_ms=last_position_ms)
            if premature_reason:
                if recoveries < MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES:
                    recoveries += 1
                    track.lavalink_recoveries = recoveries
                    logger.warning(
                        "[music/lavalink] lavalink_premature_end | guild=%s track=%r reason=%s retry=%s/%s",
                        guild.id,
                        getattr(track, "title", ""),
                        premature_reason,
                        recoveries,
                        MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES,
                    )
                    self._mark_lavalink_transition(state, seconds=6.0)
                    self._mark_internal_voice_disconnect(guild.id, seconds=6.0)
                    await asyncio.sleep(0.35)
                    continue
                logger.warning(
                    "[music/lavalink] lavalink_fallback_local_same_track | guild=%s track=%r reason=%s",
                    guild.id,
                    getattr(track, "title", ""),
                    premature_reason,
                )
                if str(getattr(state, "current_lavalink_node_label", "") or "").lower() == "auxiliar":
                    with contextlib.suppress(Exception):
                        self.backends.mark_aux_lavalink_failure(
                            guild.id,
                            RuntimeError(f"node auxiliar terminou/falhou cedo demais: {premature_reason}"),
                        )
                raise MusicPlaybackError(f"Lavalink terminou/falhou cedo demais: {premature_reason}")
            return True

    def _lavalink_failure_is_track_source_error(self, exc: BaseException | None) -> bool:
        text = self._exc_label(exc).lower()
        source_markers = (
            "trackexception",
            "invalid status code",
            "stream: 404",
            "http 404",
            "404",
            "perdeu o track",
            "nenhuma fonte candidata",
            "não conseguiu tocar nenhuma fonte candidata",
            "failed to load tracks",
            "loadexception",
            "no tracks",
        )
        if any(marker in text for marker in source_markers):
            return True
        return False

    def _public_playback_failure_label(self, exc: BaseException | None) -> str:
        text = self._exc_label(exc)
        lower = text.lower()
        if self._lavalink_failure_is_track_source_error(exc):
            if "soundcloud" in lower or "scsearch" in lower:
                return "A fonte encontrada pelo worker não entregou um stream tocável."
            return "A fonte de áudio falhou no worker."
        if "cannot connect" in lower or "connection refused" in lower or "cooldown" in lower or "no nodes" in lower:
            return MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE if self.music_worker_only_enabled() else "Node musical indisponível."
        return text[:160]

    async def _play_track(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> bool:
        if not state.last_voice_channel_id:
            raise RuntimeError("Canal de voz não definido.")
        channel = guild.get_channel(state.last_voice_channel_id) or self.bot.get_channel(state.last_voice_channel_id)
        if channel is None or not hasattr(channel, "connect"):
            raise RuntimeError("Canal de voz não encontrado.")
        if self.music_worker_only_enabled():
            await self.require_music_worker_available_async()

        state.current = track
        state.music_session_active = True
        self._clear_idle_reason(state)
        self._cancel_music_idle_disconnect(state)
        self._set_current_status(state, "resolving")
        state.paused = False
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None

        direct_youtube_request = False if self.music_worker_only_enabled() else self._track_is_direct_youtube_request(track)
        worker_local_stream = self._track_uses_worker_local_stream(track)
        worker_ytdlp_candidate = bool(self._track_is_worker_ytdlp_candidate(track))
        worker_lavalink_transport = False
        # Lavalink/NodeLink foram removidos do worker. Faixas worker-ytdlp seguem
        # pelo caminho direto do worker/yt-dlp/FFmpeg, sem node Java intermediário.
        worker_local_candidate = bool(worker_local_stream or (self.music_worker_only_enabled() and worker_ytdlp_candidate))
        # Link direto do YouTube prioriza áudio. Não bloqueie a resolução do
        # stream local aguardando painel/metadata bonitos; o painel é criado
        # depois, quando o stream já estiver pronto/iniciando.
        if not direct_youtube_request:
            # Cada música nova ganha um painel novo no fim do chat. Alterações da
            # mesma música continuam editando esse painel.
            await self.update_panel(guild.id, create=True, repost=True)

        use_lavalink_real = bool(self.backends.should_use_music_lavalink(guild.id) if self.music_worker_only_enabled() else self.backends.should_use_lavalink_real(guild.id)) and not direct_youtube_request and not worker_local_candidate
        if worker_lavalink_transport and use_lavalink_real:
            # Compatibilidade defensiva: o manager atual desativa esse caminho.
            # Se algum estado antigo tentar forçar node musical, interrompa antes
            # de iniciar serviços removidos.
            raise MusicWorkerEngineUnavailable("Lavalink/NodeLink foi removido do worker; use o fluxo direto do worker.")
        if self.music_worker_only_enabled() and not use_lavalink_real and not worker_local_candidate:
            # Worker-only não pode cair para yt-dlp/FFmpeg local na VPS. Se o
            # worker está online mas o engine musical dele não está pronto, falhe
            # com mensagem natural e sem expor Wavelink/Lavalink/cooldown no painel.
            worker_error_getter = getattr(self.backends, "music_worker_engine_error_message", None)
            message = worker_error_getter(guild.id) if callable(worker_error_getter) else MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
            if message == MUSIC_WORKER_UNAVAILABLE_MESSAGE:
                raise MusicWorkerUnavailable(message)
            raise MusicWorkerEngineUnavailable(message)
        if direct_youtube_request:
            logger.info(
                "[music] YouTube direto usando reprodução local/yt-dlp sem mirror LavaSrc | guild=%s track=%r",
                guild.id,
                getattr(track, "title", ""),
            )
            # Não aguarda metadata, mas já dispara atualização visual e painel.
            # O resolver de áudio continua em primeiro plano; a API só embeleza
            # título/autor/duração/thumb quando responder.
            self._start_youtube_direct_metadata_refresh(guild.id, state, track)
            self._schedule_panel_update(guild.id, create=True)

        if use_lavalink_real:
            try:
                return await self._play_track_lavalink(guild, state, track, channel)
            except Exception as exc:
                allow_local_fallback = False if self.music_worker_only_enabled() else bool(self._track_is_youtube_selection(track))
                fallback_getter = getattr(self.backends, "should_lavalink_fallback_to_local", None)
                if callable(fallback_getter) and not self.music_worker_only_enabled():
                    with contextlib.suppress(Exception):
                        allow_local_fallback = bool(allow_local_fallback or fallback_getter(guild.id))
                state.last_lavalink_error = f"{exc.__class__.__name__}: {exc}"
                if not allow_local_fallback:
                    # Modo Lavalink é lavalink-only. Não misture com voice client local
                    # no mesmo fluxo, porque isso causa handshake/local fallback e painel
                    # falso de external_disconnect.
                    logger.warning(
                        "[music/lavalink] playback real falhou em modo lavalink-only | guild=%s track=%r erro=%s",
                        guild.id,
                        getattr(track, "title", ""),
                        exc,
                    )
                    self._mark_lavalink_transition(state, seconds=10.0)
                    self._mark_internal_voice_disconnect(guild.id, seconds=10.0)
                    self._set_current_status(state, "queued" if self._has_pending_track(state) else "idle")
                    if self.music_worker_only_enabled():
                        if self._lavalink_failure_is_track_source_error(exc):
                            raise MusicPlaybackError(self._public_playback_failure_label(exc)) from exc
                        raise MusicWorkerEngineUnavailable(MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE) from exc
                    await self._send_text(
                        guild,
                        state,
                        "⚠️ Lavalink não conseguiu iniciar essa música agora. O node pode estar reconectando/sobrecarregado; tente novamente ou troque o node em `_musicnode`.",
                    )
                    raise MusicPlaybackError(f"Lavalink indisponível em modo lavalink-only: {exc}") from exc

                # Modo auto: fallback local permitido, mas limpo e sem marcar como
                # desconexão externa.
                logger.warning(
                    "[music/lavalink] playback real falhou antes/durante teste | guild=%s track=%r erro=%s; fallback local(auto)",
                    guild.id,
                    getattr(track, "title", ""),
                    self._exc_label(exc),
                )
                state.current_backend = "local"
                state.current_lavalink_player = None
                state.current_lavalink_playable = None
                self._set_current_status(state, "resolving")
                converted = False if self._track_is_youtube_selection(track) else self._prepare_track_for_local_after_lavalink_failure(track, exc)
                if self._track_is_youtube_selection(track):
                    # Fallback do YouTube é detalhe interno: no painel público a
                    # fonte deve continuar simples como reprodução local.
                    track.fallback_reason = ""
                    track.source = "YouTube"
                    track.extractor = track.extractor or "youtube"
                    logger.info(
                        "[music/lavalink] fallback local para resultado YouTube | guild=%s track=%r reason=%s",
                        guild.id,
                        getattr(track, "title", ""),
                        self._exc_label(exc),
                    )
                elif converted:
                    logger.info(
                        "[music/lavalink] fallback local por metadata | guild=%s track=%r source=%r",
                        guild.id,
                        getattr(track, "title", ""),
                        getattr(track, "source", ""),
                    )
                await self._disconnect_lavalink_before_local_fallback(guild, state, reason="lavalink_failed")

        playback_start_offset = 0.0
        with contextlib.suppress(Exception):
            playback_start_offset = max(0.0, float(getattr(state, "next_local_start_offset_seconds", 0.0) or 0.0))
        state.next_local_start_offset_seconds = 0.0
        if getattr(track, "duration", None) is not None and playback_start_offset > 0:
            with contextlib.suppress(Exception):
                playback_start_offset = min(playback_start_offset, max(0.0, float(track.duration) - 0.25))

        try:
            local_resolve_started = time.monotonic()
            await self._resolve_current_track(state, track)
            logger.info(
                "[music.perf] local_track_ready guild=%s track=%r elapsed=%.2fs",
                guild.id,
                getattr(track, "title", ""),
                time.monotonic() - local_resolve_started,
            )
        except asyncio.CancelledError as exc:
            raise MusicPlaybackError("Música pulada antes de iniciar o áudio.") from exc

        if state.skip_requested or state.stop_requested:
            raise MusicPlaybackError("Playback cancelado.")
        if not track.stream_url:
            raise MusicExtractionError("A música não retornou URL de stream.")
        worker_local_stream = self._track_uses_worker_local_stream(track)
        worker_ytdlp_candidate = bool(self._track_is_worker_ytdlp_candidate(track))
        worker_lavalink_transport = False
        worker_local_candidate = bool(worker_local_stream or (self.music_worker_only_enabled() and worker_ytdlp_candidate))

        # Só entra/move para a call depois que existe stream válido. Antes disso
        # ficaríamos conectando/desconectando enquanto o yt-dlp falha por anti-bot,
        # e a UI tratava a falha como fim normal de fila.
        vc = await self._ensure_voice(guild, channel, state=state)
        if vc is None:
            raise RuntimeError("Não consegui conectar ao canal de voz.")
        await self._boost_auto_bitrate_for_music(guild, channel, state)

        self._set_current_status(state, "starting")
        if direct_youtube_request:
            self._schedule_panel_update(guild.id, create=True)
        else:
            await self.update_panel(guild.id, create=True)

        # Se um TTS direto ainda estiver tocando, espera acabar antes da música entrar.
        for _ in range(60):
            if not self._vc_is_playing_or_paused(vc):
                break
            if state.skip_requested or state.stop_requested:
                raise MusicPlaybackError("Playback cancelado.")
            await asyncio.sleep(0.1)

        loop = asyncio.get_running_loop()
        finished: asyncio.Future | None = None
        mixed_source: MixedAudioSource | None = None
        last_start_error: Exception | None = None
        max_attempts = 1 + max(0, MUSIC_STREAM_START_RETRIES)

        for attempt in range(max_attempts):
            if attempt > 0:
                if state.skip_requested or state.stop_requested:
                    raise MusicPlaybackError("Playback cancelado.")
                if worker_local_stream:
                    raise last_start_error or MusicPlaybackError("Stream local do worker não iniciou.")
                # URL de stream pode expirar/403 antes do FFmpeg produzir áudio.
                # Força uma nova resolução uma única vez, sem loop infinito.
                track.stream_url = ""
                track.resolved_at_monotonic = 0.0
                track.resolved_audio_max_abr = 0
                track.resolved_audio_abr = 0
                track.resolved_audio_ext = ""
                track.resolved_audio_codec = ""
                track.resolved_audio_format_id = ""
                retry_cap = self._audio_max_abr_for_load()
                await self.extractor.resolve_stream(track, force=True, audio_max_abr=retry_cap)
                self._refresh_quality_state(state, track, cap=retry_cap)
                if not track.stream_url:
                    raise MusicExtractionError("A música não retornou URL de stream.")

            finished = loop.create_future()
            ffmpeg_options, source_base_volume = _ffmpeg_options_with_base_volume(MUSIC_FFMPEG_OPTIONS, state.volume)
            ffmpeg_before_options = MUSIC_RECONNECT_BEFORE_OPTIONS
            if playback_start_offset > 0:
                ffmpeg_before_options = f"{ffmpeg_before_options} -ss {playback_start_offset:.3f}".strip()
            if worker_local_stream:
                ffmpeg_source = self._make_worker_local_source(track)
                source_base_volume = 1.0
                state.current_status_detail = "Preenchendo buffer do worker..."
                self._schedule_panel_update(guild.id, create=True)
                ready = await asyncio.to_thread(
                    ffmpeg_source.wait_until_ready,
                    MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS,
                )
                if not ready:
                    err = ffmpeg_source.last_error
                    with contextlib.suppress(Exception):
                        ffmpeg_source.cleanup()
                    raise MusicPlaybackError(
                        f"Stream local do worker não preparou áudio a tempo: {type(err).__name__ if err else 'prebuffer_timeout'}"
                    )
            else:
                ffmpeg_source = discord.FFmpegPCMAudio(
                    track.stream_url,
                    before_options=ffmpeg_before_options,
                    options=ffmpeg_options,
                )
            mixed_source = MixedAudioSource(
                loop=loop,
                music_source=ffmpeg_source,
                music_volume=state.volume,
                source_base_volume=source_base_volume,
                duck_factor=MUSIC_TTS_LOCAL_DUCK_FACTOR,
            )
            state.current_source = mixed_source
            mixed_source.music_started_future.add_done_callback(_consume_expected_music_exception)

            def _after(error: Exception | None, finished_ref: asyncio.Future = finished) -> None:
                if error:
                    logger.warning("[music] after playback error | guild=%s erro=%s", guild.id, error)

                def _finish_once() -> None:
                    if not finished_ref.done():
                        finished_ref.set_result(None)

                loop.call_soon_threadsafe(_finish_once)

            async with state.voice_lock:
                if self._vc_is_playing_or_paused(vc):
                    with contextlib.suppress(Exception):
                        vc.stop()
                vc.play(mixed_source, after=_after)

            done, _pending = await asyncio.wait(
                {mixed_source.music_started_future, finished},
                timeout=MUSIC_PLAYBACK_START_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if mixed_source.music_started_future in done:
                try:
                    mixed_source.music_started_future.result()
                    last_start_error = None
                    break
                except Exception as exc:
                    last_start_error = exc
            elif finished in done:
                last_start_error = MusicPlaybackError("FFmpeg finalizou antes de iniciar o áudio. A URL recebida provavelmente não era um stream tocável.")
            else:
                last_start_error = MusicPlaybackError("FFmpeg demorou demais para iniciar o áudio.")

            with contextlib.suppress(Exception):
                if self._vc_is_playing_or_paused(vc):
                    vc.stop()
                mixed_source.cleanup()
            state.current_source = None
            if attempt + 1 >= max_attempts:
                raise last_start_error

        if mixed_source is None or finished is None:
            raise last_start_error or MusicPlaybackError("Não consegui iniciar o áudio.")

        self._set_current_status(state, "playing")
        state.current_started_at_monotonic = time.monotonic()
        state.current_start_offset_seconds = playback_start_offset
        self._refresh_quality_state(state, track)
        self._mark_voice_status_track_change(state)
        self._schedule_voice_status_track_sync(guild.id, repeat_after=1.0, reason="playback_started")
        self._schedule_panel_update(guild.id, create=True)
        self._start_youtube_direct_metadata_refresh(guild.id, state, track)
        self._start_prefetch_next(guild.id, state)
        await finished
        return not state.skip_requested and not state.stop_requested

    async def _ensure_voice(self, guild: discord.Guild, channel: discord.abc.Connectable, *, state: MusicGuildState | None = None) -> Optional[discord.VoiceClient]:
        vc = guild.voice_client
        if vc and self._is_lavalink_voice_client(vc):
            # O backend local não consegue usar o VoiceProtocol do Wavelink.
            # Desconecta apenas quando não está tocando para evitar leave/join em loop.
            if self._vc_is_playing_or_paused(vc):
                raise RuntimeError("Player Lavalink ainda está ativo; não vou roubar a conexão local.")
            with contextlib.suppress(Exception):
                self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                await vc.disconnect(force=False)
            vc = None
        if vc and self._vc_is_connected(vc):
            if getattr(getattr(vc, "channel", None), "id", None) != getattr(channel, "id", None):
                try:
                    self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                    await vc.move_to(channel)
                except Exception:
                    self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                    await vc.disconnect(force=True)
                    vc = None
            if vc and self._vc_is_connected(vc):
                with contextlib.suppress(Exception):
                    await guild.change_voice_state(channel=channel, self_deaf=True)
                if state is not None:
                    state.music_owns_voice = False
                return vc
        try:
            connected = await channel.connect(self_deaf=True)
            if state is not None:
                state.music_owns_voice = True
            return connected
        except Exception as exc:
            logger.warning("[music] falha ao conectar | guild=%s channel=%s erro=%s", guild.id, getattr(channel, "id", None), exc)
            return guild.voice_client if guild.voice_client and self._vc_is_connected(guild.voice_client) else None

    async def _disconnect_music_afk(self, guild: discord.Guild, state: MusicGuildState) -> None:
        if guild is None:
            return
        if not await self._music_auto_leave_enabled(guild.id):
            state.music_afk_expired = False
            return
        logger.info("[music] sessão musical AFK expirada; saindo da call | guild=%s", guild.id)
        state.stop_requested = True
        state.skip_requested = True
        self._cancel_next_prefetch(state)
        self._set_idle_reason(state, "music_afk")
        self._invalidate_panel_controls_now(guild.id)
        for _vote_action in ("skip", "stop"):
            state.control_votes.pop(_vote_action, None)
            _vote_task = state.control_vote_cleanup_tasks.pop(_vote_action, None)
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
        if state.current_source is not None:
            with contextlib.suppress(Exception):
                state.current_source.cleanup()
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()

        vc = getattr(guild, "voice_client", None)
        if vc is not None and self._vc_is_connected(vc):
            with contextlib.suppress(Exception):
                if self._vc_is_playing_or_paused(vc):
                    await self._vc_stop_audio(vc)
            with contextlib.suppress(Exception):
                self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                await vc.disconnect(force=False)

        state.current = None
        state.current_started_at_monotonic = 0.0
        state.current_start_offset_seconds = 0.0
        state.next_local_start_offset_seconds = 0.0
        state.current_source = None
        state.current_resolve_task = None
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.lavalink_tts_until = 0.0
        state.lavalink_resume_grace_until = 0.0
        state.tts_session_active_until = 0.0
        state.tts_session_last_error = ""
        state.skip_transition_active = False
        state.skip_history_suppressed_once = False
        state.paused = False
        state.music_session_active = False
        state.music_owns_voice = False
        state.music_afk_expired = False
        self._set_current_status(state, "idle")
        state.control_votes.clear()
        for _vote_task in list(state.control_vote_cleanup_tasks.values()):
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        state.control_vote_cleanup_tasks.clear()
        await self._restore_auto_bitrate_for_state(guild, state, reason="music_afk")
        await self._restore_voice_status_for_state(guild, state, reason="music_afk")
        await self.update_panel(guild.id, create=bool(state.now_message))

    async def _maybe_disconnect_idle(self, guild: discord.Guild, state: MusicGuildState) -> None:
        # Sem modo 24/7 para música: quando a música/fila acabam, a sessão entra
        # em contagem AFK de música. Depois que a contagem expira, a saída final
        # acontece após a graça curta de TTS e não é cancelada por presença tardia.
        self._set_current_status(state, "idle")
        if not state.current and not self._has_pending_track(state) and state.idle_reason == "idle":
            self._set_idle_reason(state, "queue_finished")
            self._set_panel_controls_invalidation(guild.id, delay=60.0)
        vc = guild.voice_client
        if not vc or not self._vc_is_connected(vc) or getattr(vc, "channel", None) is None:
            state.music_session_active = False
            state.music_owns_voice = False
            state.music_afk_expired = False
            await self.update_panel(guild.id, create=False)
            return
        if state.current or self._has_pending_track(state) or state.current_resolve_task or state.current_source:
            await self.schedule_music_idle_disconnect(guild.id)
            return
        # Se algum áudio direto do TTS ainda estiver tocando, tenta novamente logo.
        if self._vc_is_playing_or_paused(vc):
            await self.schedule_music_idle_disconnect(guild.id, delay=15.0)
            return
        try:
            if self._voice_channel_has_humans(getattr(vc, "channel", None)) and not state.music_afk_expired:
                await self.update_panel(guild.id, create=False)
                return
            await self.schedule_music_idle_disconnect(guild.id, delay=0.0)
            await self.update_panel(guild.id, create=False)
        except Exception:
            logger.debug("[music] idle disconnect falhou", exc_info=True)

    def _voice_channel_has_music_state(self, state: MusicGuildState) -> bool:
        return bool(state.music_session_active or state.current or self._has_pending_track(state) or state.current_source or state.current_resolve_task or state.current_lavalink_player)

    async def _find_recent_voice_audit_actor(
        self,
        guild: discord.Guild,
        *,
        disconnected: bool,
        before_channel_id: int | None = None,
        after_channel_id: int | None = None,
    ) -> discord.User | discord.Member | None:
        if guild is None:
            return None
        actions: list[discord.AuditLogAction] = []
        with contextlib.suppress(Exception):
            if disconnected and hasattr(discord.AuditLogAction, "member_disconnect"):
                actions.append(discord.AuditLogAction.member_disconnect)
            if hasattr(discord.AuditLogAction, "member_move"):
                actions.append(discord.AuditLogAction.member_move)
        if not actions:
            return None
        now = datetime.now(timezone.utc)
        for action in actions:
            try:
                async for entry in guild.audit_logs(limit=8, action=action):
                    created_at = getattr(entry, "created_at", None)
                    if created_at is not None:
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if abs((now - created_at).total_seconds()) > 12:
                            continue
                    extra = getattr(entry, "extra", None)
                    channel = getattr(extra, "channel", None)
                    channel_id = int(getattr(channel, "id", 0) or 0)
                    if before_channel_id and channel_id and channel_id != int(before_channel_id):
                        # Em member_move, alguns clients registram o canal de origem/destino de forma diferente.
                        if not after_channel_id or channel_id != int(after_channel_id):
                            continue
                    return getattr(entry, "user", None)
            except (discord.Forbidden, discord.HTTPException):
                return None
            except Exception:
                logger.debug("[music] falha ao consultar audit log de voz", exc_info=True)
                return None
        return None

    async def handle_bot_voice_disconnect(
        self,
        guild: discord.Guild,
        before_channel: discord.abc.GuildChannel | None,
        after_channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        if guild is None:
            return
        state = self.get_state(guild.id)
        if self._is_internal_voice_disconnect_recent(state):
            return
        if not self._voice_channel_has_music_state(state):
            return
        # Wavelink pode emitir transições de voice state enquanto o Player ainda
        # está vivo/tocando. Não trate isso como desconexão externa, senão o
        # painel limpa o queue/status mesmo com a música seguindo.
        current_vc = getattr(guild, "voice_client", None)
        lavalink_player = state.current_lavalink_player or current_vc
        if self._is_lavalink_voice_client(lavalink_player) and self._vc_is_connected(lavalink_player):
            logger.debug(
                "[music/lavalink] voice_state disconnect ignorado porque o Player Wavelink ainda está ativo | guild=%s",
                guild.id,
            )
            return
        if (
            self._is_lavalink_strict_mode(guild.id)
            and (state.current_backend == "lavalink" or state.current is not None or self._has_pending_track(state))
            and self._is_lavalink_transition_recent(state)
        ):
            logger.info(
                "[music/lavalink] voice_state disconnect ignorado durante transição Lavalink | guild=%s current=%r pending=%s",
                guild.id,
                getattr(getattr(state, "current", None), "title", None),
                self._has_pending_track(state),
            )
            return

        if str(getattr(state, "current_backend", "") or "").lower() == "agent":
            status = str(getattr(state, "current_status", "") or "").lower()
            reason = str(getattr(state, "idle_reason", "") or "").lower()
            last_event = str(getattr(state, "agent_last_idle_event", "") or "").lower()
            internal_idle_events = {
                "queue_empty",
                "direct_track_end",
                "lavalink_track_end",
                "agent_idle",
                "stop",
                "skip",
                "idle",
                "stopped",
                "idle_timeout_disconnect",
            }
            if (
                status == "idle"
                and state.current is None
                and not self._has_pending_track(state)
                and (reason in {"queue_finished", "manual_stop", "music_afk"} or last_event in internal_idle_events)
            ):
                logger.info(
                    "[music/agent] voice_state disconnect tratado como encerramento interno | guild=%s reason=%s event=%s",
                    guild.id,
                    reason or "-",
                    last_event or "-",
                )
                return
            # O evento de voice_state pode chegar antes do monitor da VPS ver
            # que o Music Agent entrou em idle/fila vazia. Consulte o worker com
            # timeout curto antes de acusar desconexão externa.
            with contextlib.suppress(Exception):
                payload = await _music_agent_status(timeout_seconds=min(2.0, float(getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0) or 5.0)))
                remote = self._music_agent_state_from_payload(payload, int(guild.id))
                if remote:
                    remote_status = str(remote.get("status") or "").strip().lower()
                    remote_event = str(remote.get("last_event") or "").strip().lower()
                    remote_current = remote.get("current") if isinstance(remote.get("current"), dict) else {}
                    remote_queue_size = int(remote.get("queue_size") or 0)
                    if remote_status in {"idle", "stopped"} and not remote_current and remote_queue_size <= 0 and remote_event in internal_idle_events:
                        await self.sync_music_agent_state(
                            int(guild.id),
                            None,
                            remote,
                            voice_channel_id=int(getattr(before_channel, "id", 0) or 0) or None,
                            text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0) or None,
                            queued=False,
                            create_panel=True,
                        )
                        logger.info(
                            "[music/agent] voice_state disconnect confirmado como idle remoto | guild=%s event=%s",
                            guild.id,
                            remote_event or "-",
                        )
                        return

        if str(getattr(state, "current_backend", "") or "").lower() == "agent" and state.current is not None:
            # Em alguns eventos do worker-owned voice, o Discord envia o voice_state
            # antes da VPS receber o idle/queue_empty do Music Agent. Se a faixa já
            # chegou ao fim esperado, não transforme isso em "desconectado por alguém".
            with contextlib.suppress(Exception):
                duration = float(getattr(state.current, "duration", 0) or 0)
                elapsed = (time.monotonic() - float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0)) + float(getattr(state, "current_start_offset_seconds", 0.0) or 0.0)
                if duration > 0 and elapsed >= max(0.0, duration - 8.0):
                    self._push_history(state, state.current)
                    state.current = None
                    state.paused = False
                    state.music_session_active = False
                    state.agent_last_idle_event = "natural_end_grace"
                    self._set_idle_reason(state, "queue_finished")
                    self._set_current_status(state, "idle")
                    self._mark_internal_voice_disconnect(guild.id, seconds=12.0)
                    await self.update_panel(guild.id, create=True)
                    logger.info(
                        "[music/agent] voice_state disconnect tratado como fim natural por tempo da faixa | guild=%s elapsed=%.1f duration=%.1f",
                        guild.id,
                        elapsed,
                        duration,
                    )
                    return

        actor = await self._find_recent_voice_audit_actor(
            guild,
            disconnected=True,
            before_channel_id=int(getattr(before_channel, "id", 0) or 0) or None,
            after_channel_id=int(getattr(after_channel, "id", 0) or 0) or None,
        )
        if state.current_source is not None:
            with contextlib.suppress(Exception):
                state.current_source.cleanup()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
        if state.current_source is not None:
            with contextlib.suppress(Exception):
                state.current_source.cleanup()
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        state.agent_remote_queue_size = 0
        state.current = None
        state.current_started_at_monotonic = 0.0
        state.current_start_offset_seconds = 0.0
        state.next_local_start_offset_seconds = 0.0
        state.current_source = None
        state.current_resolve_task = None
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.paused = False
        state.stop_requested = True
        state.skip_requested = True
        self._set_current_status(state, "idle")
        state.music_session_active = False
        state.music_owns_voice = False
        await self._restore_auto_bitrate_for_state(guild, state, reason="external_disconnect", channel_hint=before_channel)
        await self._restore_voice_status_for_state(guild, state, reason="external_disconnect", channel_hint=before_channel)
        state.control_votes.clear()
        self._cancel_music_idle_disconnect(state)
        self._set_idle_reason(
            state,
            "external_disconnect",
            actor=actor,
            channel_name=getattr(before_channel, "name", "") or "",
        )
        self._invalidate_panel_controls_now(guild.id)
        await self.update_panel(guild.id, create=True)

    async def handle_bot_voice_move(
        self,
        guild: discord.Guild,
        before_channel: discord.abc.GuildChannel | None,
        after_channel: discord.abc.GuildChannel | None,
    ) -> None:
        if guild is None or before_channel is None or after_channel is None:
            return
        state = self.get_state(guild.id)
        if self._is_internal_voice_disconnect_recent(state):
            return
        if not self._voice_channel_has_music_state(state):
            return
        current_vc = getattr(guild, "voice_client", None)
        lavalink_player = state.current_lavalink_player or current_vc
        if self._is_lavalink_voice_client(lavalink_player) and self._vc_is_connected(lavalink_player):
            # Se o Wavelink moveu/atualizou voice state internamente, sincroniza o
            # canal conhecido sem marcar external_move nem restaurar status.
            state.last_voice_channel_id = int(getattr(after_channel, "id", 0) or 0) or state.last_voice_channel_id
            logger.debug(
                "[music/lavalink] voice_state move tratado como interno | guild=%s channel=%s",
                guild.id,
                getattr(after_channel, "id", None),
            )
            return
        actor = await self._find_recent_voice_audit_actor(
            guild,
            disconnected=False,
            before_channel_id=int(getattr(before_channel, "id", 0) or 0) or None,
            after_channel_id=int(getattr(after_channel, "id", 0) or 0) or None,
        )
        await self._restore_auto_bitrate_for_state(guild, state, reason="external_move", channel_hint=before_channel)
        await self._restore_voice_status_for_state(guild, state, reason="external_move", channel_hint=before_channel)
        state.last_voice_channel_id = int(getattr(after_channel, "id", 0) or 0) or state.last_voice_channel_id
        await self._boost_auto_bitrate_for_music(guild, after_channel, state)
        if state.current is not None:
            await self._apply_voice_status_for_music(guild, after_channel, state, state.current, force=True)
        self._set_idle_reason(
            state,
            "external_move",
            actor=actor,
            channel_name=getattr(after_channel, "name", "") or "",
        )
        await self._send_text(
            guild,
            state,
            f"`🔀` O player foi movido para **{discord.utils.escape_markdown(getattr(after_channel, 'name', 'outro canal'))}**"
            + (f" por <@{getattr(actor, 'id', 0)}>" if actor else "")
            + ".",
        )
        await self.update_panel(guild.id, create=bool(state.now_message))

    async def _send_text(self, guild: discord.Guild, state: MusicGuildState, content: str) -> None:
        if not state.last_text_channel_id:
            return
        channel = guild.get_channel(state.last_text_channel_id) or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        with contextlib.suppress(Exception):
            await channel.send(content, silent=True)

    def _schedule_panel_update(self, guild_id: int, *, create: bool = True) -> None:
        state = self.get_state(guild_id)
        state.panel_update_create = bool(state.panel_update_create or create)
        if state.panel_update_task is not None and not state.panel_update_task.done():
            return

        async def _runner() -> None:
            try:
                await asyncio.sleep(MUSIC_PANEL_UPDATE_THROTTLE_SECONDS)
                st = self.get_state(guild_id)
                create_flag = bool(st.panel_update_create)
                st.panel_update_create = False
                await self.update_panel(guild_id, create=create_flag)
            finally:
                st = self.get_state(guild_id)
                if st.panel_update_task is task:
                    st.panel_update_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
            state.panel_update_task = task
        except RuntimeError:
            pass


    def _track_from_agent_payload(self, payload: dict, fallback: MusicTrack | None = None) -> MusicTrack | None:
        if not isinstance(payload, dict):
            return fallback

        def first_useful(*values: object, generic: set[str] | None = None) -> str:
            blocked = generic or {
                "youtube", "link", "música", "musica", "desconhecida", "unknown",
                "worker-agent", "music-agent", "music-agent-ytdlp", "worker-ytdlp",
            }
            for value in values:
                text = str(value or "").strip()
                if not text:
                    continue
                lower = text.lower()
                if lower in blocked:
                    continue
                if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
                    continue
                return text
            return ""

        fallback_title = getattr(fallback, "title", "") if fallback is not None else ""
        fallback_uploader = getattr(fallback, "uploader", "") if fallback is not None else ""
        title = first_useful(
            payload.get("display_title"), payload.get("title"), payload.get("fulltitle"),
            payload.get("name"), payload.get("track_title"), fallback_title,
        ) or "Música"
        uploader = first_useful(
            payload.get("display_uploader"), payload.get("uploader"), payload.get("author"),
            payload.get("channel"), payload.get("creator"), payload.get("artist"), fallback_uploader,
            generic={"youtube", "desconhecida", "unknown", "worker-agent", "music-agent", "worker-ytdlp"},
        )
        webpage_url = str(
            payload.get("webpage_url")
            or payload.get("url")
            or payload.get("display_url")
            or (getattr(fallback, "webpage_url", "") if fallback is not None else "")
            or payload.get("query")
            or ""
        ).strip()
        requester_id = int(payload.get("requester_id") or (getattr(fallback, "requester_id", 0) if fallback is not None else 0) or 0)
        requester_name = str(payload.get("requester_name") or (getattr(fallback, "requester_name", "") if fallback is not None else "") or "").strip()
        source = str(payload.get("display_source") or payload.get("source") or (getattr(fallback, "source", "") if fallback is not None else "") or "YouTube")
        thumbnail = str(
            payload.get("display_thumbnail")
            or payload.get("thumbnail")
            or payload.get("thumb")
            or (getattr(fallback, "thumbnail", "") if fallback is not None else "")
            or ""
        )
        stream_url = str(payload.get("stream_url") or (getattr(fallback, "stream_url", "") if fallback is not None else "") or "")
        track = MusicTrack(
            title=title,
            webpage_url=webpage_url,
            original_url=str(payload.get("original_url") or payload.get("query") or (getattr(fallback, "original_url", "") if fallback is not None else "") or webpage_url),
            stream_url=stream_url,
            requester_id=requester_id,
            requester_name=requester_name,
            duration=(payload.get("duration") if payload.get("duration") is not None else (getattr(fallback, "duration", None) if fallback is not None else None)),
            uploader=uploader,
            thumbnail=thumbnail,
            source=source,
            extractor=str(payload.get("extractor") or "worker-ytdlp"),
            is_live=bool(payload.get("is_live") or (getattr(fallback, "is_live", False) if fallback is not None else False)),
        )
        track.display_source = "YouTube" if "youtube" in track.source.lower() or "ytdlp" in track.source.lower() else track.source
        track.display_title = title
        track.display_uploader = uploader
        track.display_thumbnail = thumbnail
        # O Music Agent é dono do áudio, mas o painel da VPS ainda precisa
        # mostrar a qualidade/fonte como no player antigo. Carregue campos
        # opcionais enviados pelo worker sem depender de detalhes internos.
        with contextlib.suppress(Exception):
            track.resolved_audio_abr = int(float(payload.get("resolved_audio_abr") or payload.get("audio_abr") or payload.get("abr") or 0))
        with contextlib.suppress(Exception):
            track.resolved_audio_max_abr = int(float(payload.get("resolved_audio_max_abr") or payload.get("audio_max_abr") or payload.get("max_abr") or track.resolved_audio_abr or 0))
        track.resolved_audio_ext = str(payload.get("resolved_audio_ext") or payload.get("audio_ext") or payload.get("ext") or "").strip()
        track.resolved_audio_codec = str(payload.get("resolved_audio_codec") or payload.get("audio_codec") or payload.get("codec") or "").strip()
        track.resolved_audio_format_id = str(payload.get("resolved_audio_format_id") or payload.get("audio_format_id") or payload.get("format_id") or "").strip()
        return track

    def _music_agent_state_from_payload(self, payload: dict, guild_id: int) -> dict:
        guilds = payload.get("guilds") if isinstance(payload, dict) else {}
        if not isinstance(guilds, dict):
            return {}
        state = guilds.get(str(guild_id)) or guilds.get(guild_id)
        return state if isinstance(state, dict) else {}

    def start_music_agent_monitor(self, guild_id: int, *, voice_channel_id: int | None = None, text_channel_id: int | None = None) -> None:
        state = self.get_state(int(guild_id))
        task = getattr(state, "agent_monitor_task", None)
        if task is not None and not task.done():
            return

        async def _runner() -> None:
            idle_seen = 0
            try:
                while True:
                    await asyncio.sleep(max(1.0, min(4.0, float(getattr(config, "MUSIC_AGENT_PANEL_POLL_SECONDS", 2.0) or 2.0))))
                    try:
                        payload = await _music_agent_status(timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0))
                    except Exception:
                        logger.debug("[music/agent] monitor não conseguiu consultar status | guild=%s", guild_id, exc_info=True)
                        continue
                    remote = self._music_agent_state_from_payload(payload, int(guild_id))
                    if not remote:
                        idle_seen += 1
                        if idle_seen >= 4:
                            return
                        continue
                    await self.sync_music_agent_state(
                        int(guild_id),
                        None,
                        remote,
                        voice_channel_id=voice_channel_id,
                        text_channel_id=text_channel_id,
                        queued=False,
                        create_panel=True,
                    )
                    status = str(remote.get("status") or "").lower()
                    has_current = isinstance(remote.get("current"), dict) and bool(remote.get("current"))
                    if status in {"idle", "stopped", "failed", "error"} and not has_current:
                        idle_seen += 1
                    else:
                        idle_seen = 0
                    if idle_seen >= 3:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music/agent] monitor encerrado por falha", exc_info=True)
            finally:
                st = self.get_state(int(guild_id))
                if getattr(st, "agent_monitor_task", None) is asyncio.current_task():
                    st.agent_monitor_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
            state.agent_monitor_task = task
        except RuntimeError:
            state.agent_monitor_task = None

    def _schedule_agent_playback_started_effects(self, guild_id: int, track_key: str) -> None:
        state = self.get_state(int(guild_id))
        old = getattr(state, "agent_side_effect_task", None)
        if old is not None and not old.done():
            old.cancel()

        async def _runner() -> None:
            try:
                guild = self.bot.get_guild(int(guild_id))
                if guild is None:
                    return
                st = self.get_state(int(guild_id))
                track = st.current
                if track is None or self._panel_key_for_track(track) != track_key:
                    return
                channel = None
                if st.last_voice_channel_id:
                    channel = guild.get_channel(int(st.last_voice_channel_id)) or self.bot.get_channel(int(st.last_voice_channel_id))
                if channel is None:
                    return
                await self._boost_auto_bitrate_for_music(guild, channel, st)
                await self._apply_voice_status_for_music(guild, channel, st, track, force=True)
                self._schedule_voice_status_track_sync(int(guild_id), repeat_after=2.0, reason="agent_playback_started")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music/agent] efeitos de início da faixa falharam | guild=%s", guild_id, exc_info=True)
            finally:
                st = self.get_state(int(guild_id))
                if getattr(st, "agent_side_effect_task", None) is asyncio.current_task():
                    st.agent_side_effect_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
            state.agent_side_effect_task = task
        except RuntimeError:
            state.agent_side_effect_task = None

    def _schedule_agent_session_finished_effects(self, guild_id: int, reason: str) -> None:
        async def _runner() -> None:
            try:
                guild = self.bot.get_guild(int(guild_id))
                if guild is None:
                    return
                st = self.get_state(int(guild_id))
                channel = None
                if st.last_voice_channel_id:
                    channel = guild.get_channel(int(st.last_voice_channel_id)) or self.bot.get_channel(int(st.last_voice_channel_id))
                await self._restore_auto_bitrate_for_state(guild, st, reason=reason, channel_hint=channel)
                await self._restore_voice_status_for_state(guild, st, reason=reason, channel_hint=channel)
            except Exception:
                logger.debug("[music/agent] efeitos de fim da sessão falharam | guild=%s", guild_id, exc_info=True)

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
        except RuntimeError:
            pass

    def _sync_agent_remote_queue(self, state: MusicGuildState, remote: dict) -> None:
        """Espelha a fila do Music Agent só para painel/controles.

        A fila real continua no worker. A VPS usa essa cópia para mostrar
        queue/próxima música e manter botões coerentes sem tentar tocar localmente.
        """
        if not isinstance(remote, dict):
            return
        remote_queue = remote.get("queue")
        try:
            state.agent_remote_queue_size = max(0, int(remote.get("queue_size") or 0))
        except Exception:
            state.agent_remote_queue_size = 0
        if remote_queue is None and remote.get("queue_size") in (0, "0"):
            remote_queue = []
        if not isinstance(remote_queue, list):
            return
        if not state.agent_remote_queue_size:
            state.agent_remote_queue_size = len(remote_queue)
        else:
            state.agent_remote_queue_size = max(state.agent_remote_queue_size, len(remote_queue))
        mirrored: deque[MusicTrack] = deque(maxlen=MUSIC_HISTORY_MAXSIZE)
        for item in remote_queue[:MUSIC_QUEUE_MAXSIZE]:
            if isinstance(item, dict):
                track = self._track_from_agent_payload(item, None)
                if track is not None:
                    mirrored.append(track)
        state.forward_queue.clear()
        state.forward_queue.extend(mirrored)
        # Evita sobras da fila local poluírem o painel quando o backend remoto
        # assumiu a sessão.
        with contextlib.suppress(Exception):
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()

    async def sync_music_agent_state(
        self,
        guild_id: int,
        track: MusicTrack | None = None,
        agent_state: dict | None = None,
        *,
        voice_channel_id: int | None = None,
        text_channel_id: int | None = None,
        queued: bool = False,
        create_panel: bool = True,
    ) -> MusicGuildState:
        """Espelha o estado do backend de música do worker no painel da VPS.

        O backend remoto toca a música; a VPS continua dona da UI. Este método
        nunca conecta a VPS em voz e nunca expõe nomes internos de backend ao
        usuário. Ele só mantém ``MusicGuildState`` renderizável para o painel.
        """
        state = self.get_state(int(guild_id))
        remote = agent_state if isinstance(agent_state, dict) else {}
        previous_panel_key = getattr(state, "panel_track_key", None)
        previous_status = str(getattr(state, "current_status", "") or "")
        previous_current = getattr(state, "current", None)
        previous_current_key = self._panel_key_for_track(previous_current) if previous_current is not None else ""

        try:
            if text_channel_id:
                state.last_text_channel_id = int(text_channel_id)
            elif remote.get("text_channel_id"):
                state.last_text_channel_id = int(remote.get("text_channel_id"))
        except Exception:
            pass
        try:
            if voice_channel_id:
                state.last_voice_channel_id = int(voice_channel_id)
            elif remote.get("voice_channel_id"):
                state.last_voice_channel_id = int(remote.get("voice_channel_id"))
        except Exception:
            pass

        remote_status_original = str(remote.get("status") or "").strip().lower()
        raw_status = remote_status_original
        current_payload = remote.get("current") if isinstance(remote.get("current"), dict) else {}
        if current_payload:
            track = self._track_from_agent_payload(current_payload, track)
            incoming_key = self._panel_key_for_track(track) if track is not None else ""
            if (
                previous_current is not None
                and incoming_key
                and previous_current_key
                and incoming_key != previous_current_key
                and previous_status not in {"stopped"}
            ):
                # Quando o Music Agent troca a faixa autoritativa, a VPS também
                # guarda uma cópia local como fallback. Isso cobre a janela em
                # que o estado remoto ainda não reportou history_size ou quando
                # um painel antigo aciona o botão voltar logo após a transição.
                self._push_history(state, previous_current)
        last_error = str(remote.get("last_error") or "").strip()
        self._sync_agent_remote_queue(state, remote)
        with contextlib.suppress(Exception):
            state.agent_remote_history_size = max(0, int(remote.get("history_size") or 0))
        had_active_agent_session = bool(
            str(getattr(state, "current_backend", "") or "").lower() == "agent"
            and (state.current is not None or previous_status in {"resolving", "starting", "playing", "paused", "queued"})
        )
        confirmed_playing = bool(remote.get("confirmed_playing"))
        if raw_status == "playing" and not confirmed_playing:
            if "voice_connected" in remote or "player_present" in remote:
                confirmed_playing = bool(remote.get("voice_connected")) and bool(remote.get("player_present"))
        if raw_status == "playing" and not confirmed_playing:
            raw_status = "starting"
        if queued:
            # Música foi aceita para fila remota; mantenha a faixa atual do painel.
            if state.current is None and track is not None:
                state.current = track
                self._set_current_status(state, "queued")
        else:
            if last_error and raw_status in {"", "idle", "stopped"}:
                raw_status = "failed"
            if raw_status in {"failed", "error"}:
                if track is not None:
                    state.current = track
                state.idle_reason = "track_failed"
                state.current_status_detail = last_error[:300]
                self._set_current_status(state, "error")
            elif raw_status in {"idle", "stopped"} and not current_payload and not last_error:
                if had_active_agent_session:
                    last_action = str(remote.get("last_action") or "").strip().lower()
                    last_event = str(remote.get("last_event") or "").strip().lower()
                    if state.current is not None:
                        self._push_history(state, state.current)
                    state.current = None
                    state.paused = False
                    state.music_session_active = False
                    state.agent_started_track_key = ""
                    state.agent_last_idle_event = last_event or raw_status
                    if last_action == "stop" or last_event == "stop":
                        self._set_idle_reason(state, "manual_stop")
                        self._invalidate_panel_controls_now(int(guild_id))
                    elif last_event in {"external_disconnect", "voice_disconnected", "kicked"}:
                        self._set_idle_reason(state, "external_disconnect")
                        self._invalidate_panel_controls_now(int(guild_id))
                    else:
                        # Skip da última faixa, fim natural e fila remota vazia não
                        # são desconexão externa. O painel deve mostrar fila vazia
                        # e manter o botão anterior se houver histórico.
                        self._set_idle_reason(state, "queue_finished")
                        if not state.history:
                            self._set_panel_controls_invalidation(int(guild_id), delay=60.0)
                    self._set_current_status(state, "idle")
                    self._mark_internal_voice_disconnect(int(guild_id), seconds=8.0)
                    self._schedule_agent_session_finished_effects(int(guild_id), "agent_idle")
                else:
                    # O worker pode responder antes do primeiro evento de áudio. Não
                    # apague o painel imediatamente; mostre preparação e deixe o watch
                    # confirmar playing/failed/idle depois.
                    if track is not None:
                        state.current = track
                        self._set_current_status(state, "starting")
                    else:
                        state.current = None
                        self._set_current_status(state, "idle")
            else:
                if track is not None:
                    state.current = track
                mapped = {
                    "preparing": "resolving",
                    "starting": "starting",
                    "playing": "playing",
                    "paused": "paused",
                    "queued": "queued",
                }.get(raw_status or "starting", "starting")
                self._set_current_status(state, mapped)

        state.current_backend = "agent"
        remote_loop_mode = str(remote.get("loop_mode") or remote.get("repeat") or "").strip().lower()
        if remote_loop_mode in {"off", "one", "all"}:
            with contextlib.suppress(Exception):
                state.loop_mode = LoopMode(remote_loop_mode)
        # Shuffle é uma ação pontual no worker, não um estado permanente/toggle.
        state.shuffle = False
        if state.current is not None:
            with contextlib.suppress(Exception):
                kbps = int(float(getattr(state.current, "resolved_audio_abr", 0) or getattr(state.current, "resolved_audio_max_abr", 0) or 0))
                if kbps > 0:
                    state.current_quality_kbps = kbps
            ext = str(getattr(state.current, "resolved_audio_ext", "") or "").strip()
            codec = str(getattr(state.current, "resolved_audio_codec", "") or "").strip()
            if ext or codec:
                state.current_quality_label = "Worker"
        state.current_lavalink_player = None
        state.current_source = None
        state.paused = raw_status == "paused"
        state.music_session_active = bool(state.current or raw_status in {"preparing", "starting", "playing", "paused", "queued"})
        if raw_status and raw_status not in {"failed", "error"}:
            state.current_status_detail = raw_status
        active_statuses = {"resolving", "starting", "playing", "paused", "queued"}
        new_panel_key = self._panel_key_for_track(state.current)
        active_started_signal = bool(remote_status_original == "playing" and state.current is not None)
        active_confirmed = bool(raw_status == "playing" and confirmed_playing and state.current is not None)
        if state.current is not None or self._has_pending_track(state) or state.current_status in active_statuses:
            self._reactivate_panel_controls_now(int(guild_id))
        previous_started_key = str(getattr(state, "agent_started_track_key", "") or "")
        just_started_agent_track = bool((active_confirmed or active_started_signal) and new_panel_key and previous_started_key != new_panel_key)
        if just_started_agent_track:
            state.agent_started_track_key = new_panel_key
            state.current_started_at_monotonic = time.monotonic()
            state.current_start_offset_seconds = 0.0
            self._schedule_agent_playback_started_effects(int(guild_id), new_panel_key)

        # O Music Agent envia confirmações repetidas de "playing" enquanto a faixa
        # continua ativa. Repostar o painel em cada confirmação cria spam de
        # painéis idênticos. Repost só no primeiro start confirmado de uma faixa;
        # refresh de metadata/status apenas edita o painel atual.
        track_changed_for_panel = bool(new_panel_key and previous_panel_key != new_panel_key)
        repost_key = f"{guild_id}:{new_panel_key}" if new_panel_key else ""
        already_reposted = bool(repost_key and repost_key == str(getattr(state, "panel_last_repost_key", "") or ""))
        should_repost_panel = bool(
            create_panel
            and state.now_message is not None
            and new_panel_key
            and just_started_agent_track
            and track_changed_for_panel
            and not already_reposted
            and bool(getattr(config, "MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", True))
        )
        if create_panel:
            await self.update_panel(int(guild_id), create=True, repost=should_repost_panel)
        if state.current_backend == "agent" and state.current_status in active_statuses:
            self.start_music_agent_monitor(int(guild_id), voice_channel_id=voice_channel_id, text_channel_id=text_channel_id)
        return state

    async def update_panel(self, guild_id: int, *, create: bool = True, repost: bool = False) -> None:
        state = self.get_state(guild_id)
        if not state.last_text_channel_id:
            return
        guild = self.bot.get_guild(int(guild_id))
        channel = None
        if guild is not None:
            channel = guild.get_channel(state.last_text_channel_id)
        channel = channel or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        try:
            from .ui import build_player_embeds, MusicPlayerView

            has_player_content = bool(state.current or self._has_pending_track(state))
            if has_player_content:
                self._reactivate_panel_controls_now(int(guild_id))
            state.panel_vote_summary = self.pending_vote_summary(guild_id)
            embeds = build_player_embeds(state)
            # O painel mantém a mesma estrutura de controles mesmo quando a música acaba,
            # é parada ou o bot é desconectado. Os botões ficam visíveis e a view decide
            # quais ações ainda fazem sentido.
            view = MusicPlayerView(self, guild_id)
            current_panel_key = self._panel_key_for_track(state.current)

            async with state.panel_lock:
                track_changed = bool(
                    has_player_content
                    and state.now_message is not None
                    and current_panel_key
                    and (not state.panel_track_key or current_panel_key != state.panel_track_key)
                    and bool(getattr(config, "MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", True))
                )
                repost_key = f"{guild_id}:{current_panel_key}" if current_panel_key else ""
                already_reposted_key = bool(repost_key and repost_key == str(getattr(state, "panel_last_repost_key", "") or ""))
                if repost and already_reposted_key:
                    repost = False
                allow_implicit_repost = str(getattr(state, "current_backend", "") or "").lower() != "agent"
                implicit_repost = bool(create and track_changed and not already_reposted_key and allow_implicit_repost)
                should_repost = bool(has_player_content and (repost or implicit_repost))
                if should_repost and repost_key:
                    state.panel_last_repost_key = repost_key
                    state.panel_last_repost_at = time.monotonic()
                if should_repost and state.now_message is not None:
                    old_message = state.now_message
                    state.now_message = None
                    # O painel antigo não deve continuar vivo/visível com controles
                    # ativos quando uma nova faixa gera painel fresco. Tente deletar;
                    # se o Discord negar, remova a view como fallback seguro.
                    try:
                        await old_message.delete()
                    except Exception:
                        with contextlib.suppress(Exception):
                            await old_message.edit(view=None)
                    logger.info("[music] repostando painel do player | guild=%s track_key=%s", guild_id, current_panel_key)

                if state.now_message is not None and not should_repost:
                    try:
                        await state.now_message.edit(content=None, embeds=embeds, view=view)
                        state.panel_track_key = current_panel_key
                        return
                    except Exception:
                        state.now_message = None
                        state.panel_track_key = None

                if create:
                    state.now_message = await channel.send(embeds=embeds, view=view, silent=True)
                    state.panel_track_key = current_panel_key
        except Exception:
            logger.debug("[music] falha ao atualizar painel", exc_info=True)

    async def _announce_now_playing(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> None:
        await self.update_panel(guild.id, create=True)


    def _swap_url_base_preserving_path(self, url: str, new_base: str) -> str:
        """Troca apenas scheme/host/porta, mantendo path/query exatamente."""
        raw_url = str(url or "").strip()
        raw_base = str(new_base or "").strip().rstrip("/")
        if not raw_url or not raw_base:
            return ""
        try:
            source = urlsplit(raw_url)
            base = urlsplit(raw_base)
            if not source.scheme or not source.netloc or not base.scheme or not base.netloc:
                return ""
            return urlunsplit((base.scheme, base.netloc, source.path, source.query, ""))
        except Exception:
            return ""

    def should_route_tts_to_music_agent(self, guild_id: int | None, channel_id: int | None = None) -> bool:
        """Retorna se o TTS deve seguir pelo worker dono da sessão musical.

        Quando o Music Agent está tocando, a VPS não deve entrar na call pelo
        caminho normal de TTS, porque isso interrompe o player remoto.
        """
        if not MUSIC_AGENT_TTS_ROUTE_ENABLED:
            return False
        try:
            state = self.get_state(int(guild_id or 0))
        except Exception:
            return False
        if str(getattr(state, "current_backend", "") or "").lower() != "agent":
            return False
        active = bool(
            getattr(state, "music_session_active", False)
            or getattr(state, "current", None) is not None
            or str(getattr(state, "current_status", "") or "") in {"resolving", "starting", "playing", "paused", "queued"}
        )
        if not active:
            return False
        try:
            if channel_id and getattr(state, "last_voice_channel_id", 0):
                return int(channel_id) == int(getattr(state, "last_voice_channel_id", 0) or 0)
        except Exception:
            return active
        return active

    _supports_tts_cached_audio = True

    async def play_tts_via_music_agent(
        self,
        *,
        guild_id: int,
        channel_id: int,
        text: str,
        engine: str = "gtts",
        voice: str = "",
        language: str = "pt-br",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        timeout: float | None = None,
        cache_key: str = "",
        audio_b64: str = "",
        audio_format: str = "",
        tld: str = "com",
        tts_request_id: str = "",
    ) -> dict[str, Any]:
        if not self.should_route_tts_to_music_agent(guild_id, channel_id):
            return {"ok": False, "tts_agent_route": False, "reason": "agent_not_owner"}
        started = time.monotonic()
        result = await _music_agent_command(
            "tts",
            guild_id=int(guild_id),
            voice_channel_id=int(channel_id or 0),
            text=str(text or ""),
            cache_key=cache_key, audio_b64=audio_b64, audio_format=audio_format, tld=tld, tts_request_id=tts_request_id,
            engine=str(engine or "gtts"),
            voice=str(voice or ""),
            language=str(language or "pt-br"),
            rate=str(rate or "+0%"),
            pitch=str(pitch or "+0Hz"),
            timeout_seconds=max(3.0, float(timeout or MUSIC_AGENT_TTS_TIMEOUT_SECONDS)),
        )
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        state_payload = result.get("state") if isinstance(result, dict) else None
        if isinstance(state_payload, dict):
            state = self.get_state(int(guild_id))
            await self.sync_music_agent_state(
                int(guild_id),
                state.current,
                state_payload,
                voice_channel_id=int(channel_id or getattr(state, "last_voice_channel_id", 0) or 0),
                text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                create_panel=True,
            )
        return {
            "ok": bool(isinstance(result, dict) and result.get("ok", True)),
            "tts_agent_route": True,
            "source_setup_ms": 0.0,
            "play_call_ms": 0.0,
            "playback_ms": float((result or {}).get("playback_ms") or 0.0) if isinstance(result, dict) else 0.0,
            "playback_started_at": started,
            "first_frame_observed": False,
            "worker_first_frame_ms": result.get('first_frame_ms') if isinstance(result, dict) else None,
            "worker_first_frame_observed": bool(result.get('first_frame_observed')) if isinstance(result, dict) else False,
            "agent_elapsed_ms": elapsed_ms,
            "worker_result": result,
        }

    def _tts_format_suffix(self, fmt: str) -> str:
        fmt = str(fmt or "").strip().lower()
        if fmt in {"ogg", "opus", "ogg_opus", "ogg/opus"}:
            return ".ogg"
        if fmt in {"m4a", "aac"}:
            return ".m4a"
        if fmt in {"wav", "wave"}:
            return ".wav"
        return ".mp3"

    async def _probe_audio_duration_seconds(self, source_path: str) -> float | None:
        try:
            ffprobe = shutil.which("ffprobe") or "ffprobe"
            proc = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode != 0:
                return None
            text = (stdout or b"").decode("utf-8", "replace").strip()
            if not text:
                return None
            value = float(text)
            if value <= 0:
                return None
            return value
        except Exception:
            return None

    def _tts_transition_filter(self, *, duration_s: float | None) -> str:
        """Filtro FFmpeg pequeno para remover clicks/flicker em troca de stream.

        O fade-in começa depois do pré-silêncio para não comer o começo da fala;
        o fade-out usa ffprobe quando disponível e acontece antes do pós-silêncio.
        """
        filters: list[str] = []
        pre_s = max(0.0, MUSIC_TTS_PREROLL_SILENCE_MS / 1000.0)
        post_s = max(0.0, MUSIC_TTS_POSTROLL_SILENCE_MS / 1000.0)
        fade_in_s = max(0.0, MUSIC_TTS_FADE_IN_MS / 1000.0)
        fade_out_s = max(0.0, MUSIC_TTS_FADE_OUT_MS / 1000.0)
        if pre_s > 0:
            filters.append(f"adelay={int(round(pre_s * 1000))}:all=1")
        if fade_in_s > 0:
            filters.append(f"afade=t=in:st={pre_s:.3f}:d={fade_in_s:.3f}")
        if fade_out_s > 0 and duration_s and duration_s > fade_out_s + 0.05:
            fade_start = max(pre_s, pre_s + duration_s - fade_out_s)
            filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out_s:.3f}")
        if post_s > 0:
            filters.append(f"apad=pad_dur={post_s:.3f}")
        return ",".join(filters)

    def _phone_worker_base_url(self) -> str:
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return ""
        scheme = PHONE_WORKER_SCHEME if PHONE_WORKER_SCHEME in {"http", "https"} else "http"
        return f"{scheme}://{PHONE_WORKER_HOST}:{PHONE_WORKER_PORT}"

    def _phone_worker_available_for_tts_convert(self) -> bool:
        if not MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED:
            return False
        if time.monotonic() < float(getattr(self, "_phone_worker_tts_convert_disabled_until", 0.0) or 0.0):
            return False
        return bool(self._phone_worker_base_url())

    def _mark_phone_worker_tts_convert_failure(self, exc: Exception | str, *, cooldown: float = 45.0) -> None:
        self._phone_worker_tts_convert_disabled_until = time.monotonic() + max(5.0, float(cooldown))
        now = time.monotonic()
        if now - float(getattr(self, "_phone_worker_tts_convert_last_log_at", 0.0) or 0.0) >= 60.0:
            self._phone_worker_tts_convert_last_log_at = now
            logger.warning("[music/phone-worker] TTS convert indisponível; fallback local por %.0fs | erro=%s", max(5.0, float(cooldown)), exc)
        else:
            logger.debug("[music/phone-worker] TTS convert falhou; fallback local", exc_info=isinstance(exc, Exception))

    async def _phone_worker_convert_tts_audio(
        self,
        source_path: str,
        target_path: str,
        *,
        input_ext: str,
        output_ext: str,
        ffmpeg_args: list[str],
    ) -> bool:
        """Tenta converter/normalizar TTS no celular via phone-worker.

        É opcional e pré-playback: se falhar, a VPS faz a conversão local normal.
        """
        if not self._phone_worker_available_for_tts_convert():
            return False
        try:
            source_path = os.path.abspath(str(source_path or ""))
            target_path = os.path.abspath(str(target_path or ""))
            if not os.path.isfile(source_path) or os.path.getsize(source_path) <= 0:
                return False
            max_input_bytes = MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB * 1024 * 1024
            source_size = os.path.getsize(source_path)
            if source_size > max_input_bytes:
                logger.debug(
                    "[music/phone-worker] TTS convert ignorado: arquivo grande demais | size=%s max=%s",
                    source_size,
                    max_input_bytes,
                )
                return False

            def _read_file(path: str) -> bytes:
                with open(path, "rb") as handle:
                    return handle.read()

            raw = await asyncio.to_thread(_read_file, source_path)
            payload = {
                "task": "ffmpeg_convert",
                "data_b64": base64.b64encode(raw).decode("ascii"),
                "input_ext": str(input_ext or "mp3").strip(". /\\") or "mp3",
                "output_ext": str(output_ext or "ogg").strip(". /\\") or "ogg",
                "ffmpeg_args": [str(part) for part in ffmpeg_args],
                "timeout_seconds": max(3, int(MUSIC_TTS_CONVERT_TIMEOUT_SECONDS + 1)),
            }
            started = time.monotonic()
            base = self._phone_worker_base_url()
            timeout = aiohttp.ClientTimeout(total=MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS)
            headers = {
                "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                    text = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(f"HTTP {response.status}: {text[:240]}")
                    data = json.loads(text or "{}")
            out_b64 = str(data.get("data_b64") or "")
            if not out_b64:
                raise RuntimeError("phone-worker não retornou data_b64")
            out = base64.b64decode(out_b64.encode("ascii"), validate=True)
            if not out:
                raise RuntimeError("phone-worker retornou áudio vazio")

            def _write_file(path: str, content: bytes) -> None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.phone-worker.tmp"
                with open(tmp, "wb") as handle:
                    handle.write(content)
                os.replace(tmp, path)

            await asyncio.to_thread(_write_file, target_path, out)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            logger.info(
                "[music/phone-worker] tts_convert_ok | source=%s target=%s format=%s input=%s output=%s elapsed_ms=%.1f",
                source_path,
                target_path,
                output_ext,
                source_size,
                len(out),
                elapsed_ms,
            )
            return True
        except asyncio.TimeoutError as exc:
            self._mark_phone_worker_tts_convert_failure(f"timeout após {MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS:.1f}s", cooldown=30.0)
            return False
        except Exception as exc:
            self._mark_phone_worker_tts_convert_failure(exc, cooldown=45.0)
            return False

    async def _convert_tts_audio_for_lavalink(self, source_path: str, fmt: str) -> str | None:
        """Prepara o TTS para o Lavalink em formato leve e com transição suave.

        A preparação acontece antes de pausar a música, então falhas aqui só fazem
        cair para o próximo formato/candidato. O arquivo original continua como
        último fallback quando aplicável.
        """
        fmt = str(fmt or "").strip().lower()
        try:
            source_path = os.path.abspath(str(source_path or ""))
            if not os.path.isfile(source_path) or os.path.getsize(source_path) <= 0:
                return None
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            duration_s = await self._probe_audio_duration_seconds(source_path)
            transition_filter = self._tts_transition_filter(duration_s=duration_s)
            pre = MUSIC_TTS_PREROLL_SILENCE_MS
            post = MUSIC_TTS_POSTROLL_SILENCE_MS
            fin = MUSIC_TTS_FADE_IN_MS
            fout = MUSIC_TTS_FADE_OUT_MS
            transform_enabled = bool(transition_filter)

            if fmt in {"", "mp3", "mpeg"}:
                # Se não há suavização para aplicar e o original já é MP3, preservar.
                if not transform_enabled and source_path.lower().endswith(".mp3"):
                    return source_path
                base, _ext = os.path.splitext(source_path)
                target = f"{base}.tts_smooth_p{pre}_q{post}_fi{fin}_fo{fout}.mp3"
                codec_args = ["-c:a", "libmp3lame", "-b:a", MUSIC_TTS_MP3_BITRATE]
            elif fmt in {"opus", "ogg", "ogg_opus", "ogg/opus"}:
                # Mesmo quando a entrada já é OGG/Opus, reprocessar se houver
                # pré/pós-silêncio ou fade; isso evita reusar um arquivo sem
                # suavização gerado por patch antigo.
                if not transform_enabled and source_path.lower().endswith((".ogg", ".opus")):
                    return source_path
                base, _ext = os.path.splitext(source_path)
                target = f"{base}.tts_opus_p{pre}_q{post}_fi{fin}_fo{fout}.ogg"
                codec_args = [
                    "-c:a",
                    "libopus",
                    "-b:a",
                    MUSIC_TTS_OPUS_BITRATE,
                    "-ar",
                    str(MUSIC_TTS_OPUS_SAMPLE_RATE),
                    "-ac",
                    str(MUSIC_TTS_OPUS_CHANNELS),
                ]
            else:
                return None

            if (
                os.path.isfile(target)
                and os.path.getsize(target) > 0
                and os.path.getmtime(target) >= os.path.getmtime(source_path)
            ):
                return target

            worker_args = ["-vn"]
            if transition_filter:
                worker_args.extend(["-af", transition_filter])
            worker_args.extend(codec_args)
            output_ext = "ogg" if fmt in {"opus", "ogg", "ogg_opus", "ogg/opus"} else "mp3"
            input_ext = os.path.splitext(source_path)[1].strip(".").lower() or "mp3"
            if await self._phone_worker_convert_tts_audio(
                source_path,
                target,
                input_ext=input_ext,
                output_ext=output_ext,
                ffmpeg_args=worker_args,
            ):
                return target

            cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", source_path, *worker_args, target]

            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=MUSIC_TTS_CONVERT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.communicate()
                with contextlib.suppress(Exception):
                    os.remove(target)
                logger.warning(
                    "[music/lavalink] tts_convert_timeout | source=%s fmt=%s timeout=%.1fs",
                    source_path,
                    fmt or "mp3",
                    MUSIC_TTS_CONVERT_TIMEOUT_SECONDS,
                )
                return None
            if proc.returncode != 0 or not os.path.isfile(target) or os.path.getsize(target) <= 0:
                with contextlib.suppress(Exception):
                    os.remove(target)
                logger.warning(
                    "[music/lavalink] tts_convert_failed | source=%s fmt=%s rc=%s filter=%r stderr=%s",
                    source_path,
                    fmt or "mp3",
                    proc.returncode,
                    transition_filter,
                    (stderr or stdout or b"").decode("utf-8", "replace")[:400],
                )
                return None
            logger.info(
                "[music/lavalink] tts_convert_ok | source=%s target=%s format=%s size=%s elapsed_ms=%.1f filter=%r duration_s=%s",
                source_path,
                target,
                "opus" if target.lower().endswith(".ogg") else "mp3",
                os.path.getsize(target),
                (time.monotonic() - started) * 1000.0,
                transition_filter,
                round(duration_s, 3) if duration_s else None,
            )
            return target
        except Exception:
            logger.debug("[music/lavalink] falha ao preparar TTS para Lavalink", exc_info=True)
            return None

    async def _lavalink_tts_source_paths_for_playback(self, path: str) -> list[str]:
        """Retorna caminhos em ordem: formato preferido leve, depois fallback."""
        paths: list[str] = []
        original = os.path.abspath(str(path or ""))
        if not os.path.isfile(original) or os.path.getsize(original) <= 0:
            return paths
        preferred = MUSIC_TTS_AUDIO_FORMAT
        fallback = MUSIC_TTS_AUDIO_FALLBACK_FORMAT
        if preferred in {"opus", "ogg", "ogg_opus", "ogg/opus"}:
            converted = await self._convert_tts_audio_for_lavalink(original, preferred)
            if converted:
                paths.append(converted)
        elif preferred in {"mp3", "mpeg"}:
            converted = await self._convert_tts_audio_for_lavalink(original, "mp3")
            if converted:
                paths.append(converted)
        # MP3 fallback preserva compatibilidade e evita travar quando o Lavalink não
        # aceitar o container/codec preferido.
        if fallback in {"mp3", "mpeg", ""}:
            converted = await self._convert_tts_audio_for_lavalink(original, "mp3")
            if converted and converted not in paths:
                paths.append(converted)
        elif fallback in {"opus", "ogg", "ogg_opus", "ogg/opus"}:
            converted = await self._convert_tts_audio_for_lavalink(original, fallback)
            if converted and converted not in paths:
                paths.append(converted)
        # Último fallback absoluto: arquivo original. Só será usado se os
        # candidatos convertidos falharem no load/probe.
        if original not in paths:
            paths.append(original)
        return paths

    async def _probe_tts_url(self, url: str, *, timeout: float | None = None) -> bool:
        url = str(url or "").strip()
        if not url:
            return False
        probe_timeout = max(0.25, float(timeout or MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS))
        try:
            client_timeout = aiohttp.ClientTimeout(total=probe_timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                for method in ("HEAD", "GET"):
                    try:
                        async with session.request(method, url) as response:
                            if 200 <= int(response.status) < 300:
                                return True
                            if method == "HEAD" and int(response.status) in {405, 403}:
                                continue
                            return False
                    except Exception:
                        if method == "HEAD":
                            continue
                        raise
        except Exception:
            logger.debug("[music/lavalink] probe da URL TTS falhou | url=%s", url, exc_info=True)
        return False

    async def _filter_lavalink_tts_candidates(self, candidates: list[str]) -> list[str]:
        """Valida URLs HTTP antes de pausar/substituir a música no Lavalink.

        Mantém todos os candidatos HTTP que responderam OK, em ordem. Isso permite
        tentar OGG/Opus primeiro e MP3 depois, sem pausar a música antes de saber
        que pelo menos uma URL está acessível.
        """
        if not candidates:
            return []
        ready: list[str] = []
        unprobed: list[str] = []
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            if candidate.startswith("http://") or candidate.startswith("https://"):
                if await self._probe_tts_url(candidate):
                    logger.info("[music/lavalink] tts_url_probe_ok | url=%s", candidate)
                    ready.append(candidate)
                    continue
                logger.warning("[music/lavalink] tts_url_probe_failed | url=%s", candidate)
            else:
                unprobed.append(candidate)
        return ready + unprobed

    def _lavalink_tts_candidates_for_path(self, path: str, *, timeout: float = 120.0) -> list[str]:
        candidates: list[str] = []
        public_base = str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        internal_base = MUSIC_TTS_INTERNAL_BASE_URL
        abs_path = os.path.abspath(str(path or ""))
        file_ok = bool(abs_path and os.path.isfile(abs_path) and os.path.getsize(abs_path) > 0)
        public_url = ""
        internal_url = ""
        if (public_base or internal_base) and file_ok:
            try:
                from webserver import register_tts_audio_file

                token = register_tts_audio_file(
                    abs_path,
                    ttl_seconds=max(float(timeout or 120.0) + 90.0, float(getattr(config, "MUSIC_LAVALINK_TTS_URL_TTL_SECONDS", 240))),
                )
                if token:
                    suffix = os.path.splitext(abs_path)[1].lower() or ".mp3"
                    if suffix not in {".ogg", ".opus", ".m4a", ".aac", ".wav", ".mp3"}:
                        suffix = ".mp3"
                    public_url = f"{public_base}/tts-audio/{token}{suffix}" if public_base else ""
                    if public_url:
                        internal_url = self._swap_url_base_preserving_path(public_url, internal_base)
                    elif internal_base:
                        internal_url = f"{internal_base}/tts-audio/{token}{suffix}"
                    if MUSIC_LAVALINK_TTS_INTERNAL_FIRST and internal_url:
                        candidates.append(internal_url)
                    if public_url:
                        candidates.append(public_url)
                    if (not MUSIC_LAVALINK_TTS_INTERNAL_FIRST) and internal_url:
                        candidates.append(internal_url)
                    logger.info(
                        "[music/lavalink] tts_public_url_registered | format=%s url=%s internal_url=%s size=%s",
                        suffix.lstrip(".") or "unknown",
                        public_url or "não configurada",
                        internal_url or "não configurada",
                        os.path.getsize(abs_path),
                    )
            except Exception:
                logger.debug("[music/lavalink] falha ao registrar URL temporária do TTS", exc_info=True)
        elif (public_base or internal_base) and not file_ok:
            logger.warning("[music/lavalink] TTS via Lavalink sem arquivo válido para publicar | path=%s exists=%s", abs_path, os.path.exists(abs_path))
        else:
            logger.warning("[music/lavalink] TTS via Lavalink sem MUSIC_TTS_PUBLIC_BASE_URL/MUSIC_TTS_INTERNAL_BASE_URL configurado")

        if bool(getattr(config, "MUSIC_LAVALINK_TTS_FILE_FALLBACK", False)) and file_ok:
            with contextlib.suppress(Exception):
                candidates.append("file://" + quote(abs_path))
                candidates.append(abs_path)
        deduped: list[str] = []
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped


    async def prepare_tts_local_fallback_after_lavalink_failure(
        self,
        guild: discord.Guild | None,
        vc: Any | None,
        *,
        reason: str = "",
    ) -> discord.VoiceClient | None:
        """Prepara um VoiceClient local quando o TTS via Lavalink perdeu voz.

        O Lavalink pode receber o arquivo e mesmo assim ficar sem payload de voz
        válido (state=None/voice_keys=[]). Nesse caso, insistir no node só silencia
        o TTS. Este fallback derruba apenas o Player Wavelink preso, libera o
        guarda de TTS local por alguns segundos e conecta o VoiceClient normal.
        A música via Lavalink pode ser restaurada depois pelo fluxo normal/fallback
        do player, mas o TTS curto não fica perdido.
        """
        if not MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK:
            return None
        if guild is None:
            return None
        guild_id = int(getattr(guild, "id", 0) or 0)
        state = self.get_state(guild_id)
        channel = getattr(vc, "channel", None)
        if channel is None and getattr(state, "last_voice_channel_id", None):
            with contextlib.suppress(Exception):
                channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
        if channel is None:
            with contextlib.suppress(Exception):
                channel = getattr(getattr(guild, "me", None), "voice", None).channel
        if channel is None:
            logger.warning(
                "[music/lavalink] fallback local de TTS cancelado: canal de voz desconhecido | guild=%s reason=%s",
                guild_id,
                reason,
            )
            return None

        logger.warning(
            "[music/lavalink] usando fallback local para TTS após falha de voz Lavalink | guild=%s channel=%s reason=%s",
            guild_id,
            getattr(channel, "id", None),
            reason,
        )
        # Libera as travas que fariam o cog de TTS ignorar a conexão local.
        state.lavalink_tts_until = 0.0
        state.lavalink_resume_grace_until = 0.0
        state.tts_session_active_until = 0.0
        state.tts_lavalink_local_fallback_until = time.monotonic() + MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS
        self._mark_lavalink_transition(state, seconds=2.0)
        self._mark_internal_voice_disconnect(guild_id, seconds=2.0)

        current_vc = getattr(guild, "voice_client", None)
        if self._is_lavalink_voice_client(current_vc):
            with contextlib.suppress(Exception):
                await self._vc_stop_audio(current_vc)
            with contextlib.suppress(Exception):
                await current_vc.disconnect(force=True)
            await asyncio.sleep(0.8)

        local_vc = await self._ensure_voice(guild, channel, state=state)
        if local_vc is None or self._is_lavalink_voice_client(local_vc):
            logger.warning(
                "[music/lavalink] fallback local de TTS não obteve VoiceClient local | guild=%s vc=%s",
                guild_id,
                type(local_vc).__name__ if local_vc is not None else "None",
            )
            return None
        return local_vc

    async def _set_music_agent_ducking(self, guild_id: int, enabled: bool, state: MusicGuildState | None = None) -> bool:
        if not guild_id:
            return False
        try:
            action = "duck" if enabled else "unduck"
            kwargs: dict[str, Any] = {"guild_id": int(guild_id), "timeout_seconds": 2.0}
            if enabled:
                kwargs["volume_percent"] = MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT
            elif state is not None:
                kwargs["volume_percent"] = max(0, min(150, int(round(float(getattr(state, "volume", MUSIC_DEFAULT_VOLUME) or MUSIC_DEFAULT_VOLUME) * 100))))
            await _music_agent_command(action, **kwargs)
            logger.info(
                "[music/agent] tts_duck_%s | guild=%s volume=%s",
                "start" if enabled else "restore",
                guild_id,
                kwargs.get("volume_percent", "normal"),
            )
            return True
        except Exception as exc:
            logger.debug("[music/agent] falha ao ajustar ducking remoto | guild=%s enabled=%s erro=%s", guild_id, enabled, exc)
            return False

    async def play_tts(
        self,
        *,
        guild: discord.Guild | None,
        vc: discord.VoiceClient,
        path: str,
        before_options: str = "-nostdin",
        options: str = MUSIC_TTS_FFMPEG_OPTIONS,
        timeout: float = 120.0,
        item=None,
    ) -> dict[str, Any]:
        """Toca TTS integrado ao player de música mantendo o volume normal da música."""
        loop = asyncio.get_running_loop()
        playback_started_at = time.monotonic()
        source_setup_started_at = time.monotonic()
        play_call_ms = 0.0

        guild_id = getattr(guild, "id", None) or getattr(getattr(vc, "guild", None), "id", None)
        state = self.get_state(int(guild_id)) if guild_id is not None else None
        if state is not None:
            state.tts_voice_touched = True
            state.last_tts_activity_at = time.monotonic()
        active_source = state.current_source if state is not None else None

        if state is not None and guild is not None and (state.current_backend == "lavalink" or self._is_lavalink_voice_client(vc)):
            # O arquivo já foi gerado pelo TTS antes de chegar aqui. Antes de
            # pausar/substituir a música, tenta preparar uma versão OGG/Opus
            # menor para Lavalink e mantém MP3 como fallback.
            source_paths = await self._lavalink_tts_source_paths_for_playback(path)
            raw_candidates: list[str] = []
            for source_path in source_paths:
                raw_candidates.extend(self._lavalink_tts_candidates_for_path(source_path, timeout=timeout))
            candidates = await self._filter_lavalink_tts_candidates(raw_candidates)
            source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)
            if not candidates:
                logger.warning(
                    "[music/lavalink] TTS via Lavalink sem URL acessível/configurada | guild=%s source_paths=%s raw_candidates=%s",
                    guild_id,
                    source_paths,
                    raw_candidates,
                )
                return {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": 0.0,
                    "playback_ms": max(0.0, (time.monotonic() - playback_started_at) * 1000.0),
                    "playback_started_at": playback_started_at,
                    "tts_lavalink_failed": True,
                    "tts_lavalink_missing_source": True,
                }
            tts_window = max(12.0, float(timeout or 120.0) + MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS)
            try:
                async with state.voice_lock:
                    state.tts_session_active_until = time.monotonic() + tts_window
                    state.tts_session_last_error = ""
                    state.lavalink_tts_until = max(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), time.monotonic() + tts_window)
                    self._mark_lavalink_transition(state, seconds=tts_window)
                    self._mark_internal_voice_disconnect(int(guild_id), seconds=tts_window)
                    tts_channel = getattr(vc, "channel", None) or getattr(getattr(state, "current_lavalink_player", None), "channel", None)
                    if tts_channel is None and getattr(state, "last_voice_channel_id", None):
                        with contextlib.suppress(Exception):
                            tts_channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                    result = await self.backends.play_lavalink_tts(
                        guild,
                        voice_channel=tts_channel,
                        candidates=candidates,
                        volume=TTS_VOLUME,
                        resume_volume=state.volume,
                        resume_playable=state.current_lavalink_playable,
                        timeout=max(1.0, float(timeout or 120.0)),
                        should_resume=lambda: bool(
                            state.current_backend == "lavalink"
                            and not state.skip_requested
                            and not state.stop_requested
                            and state.current is not None
                        ),
                    )
                result.setdefault("source_setup_ms", source_setup_ms)
                result.setdefault("play_call_ms", 0.0)
                result.setdefault("playback_started_at", playback_started_at)
                result.setdefault("playback_ms", max(0.0, (time.monotonic() - playback_started_at) * 1000.0))
                state.last_tts_activity_at = time.monotonic()
                cleanup_grace = MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS
                state.tts_session_active_until = time.monotonic() + cleanup_grace
                state.tts_session_last_cleanup_at = time.monotonic()
                state.lavalink_resume_grace_until = time.monotonic() + max(1.0, cleanup_grace)
                state.lavalink_tts_until = min(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), state.lavalink_resume_grace_until)
                self._mark_lavalink_transition(state, seconds=max(1.0, cleanup_grace))
                self._mark_internal_voice_disconnect(int(guild_id), seconds=max(1.0, cleanup_grace))
                return result
            except Exception as exc:
                state.tts_lavalink_failures = int(getattr(state, "tts_lavalink_failures", 0) or 0) + 1
                state.tts_session_last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("[music/lavalink] TTS via Lavalink falhou; música deve continuar/resumir | guild=%s erro=%s", guild_id, exc)
                cleanup_grace = MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS
                state.tts_session_active_until = time.monotonic() + cleanup_grace
                state.tts_session_last_cleanup_at = time.monotonic()
                state.lavalink_resume_grace_until = time.monotonic() + max(1.0, cleanup_grace)
                state.lavalink_tts_until = min(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), state.lavalink_resume_grace_until)
                self._mark_lavalink_transition(state, seconds=max(1.0, cleanup_grace))
                self._mark_internal_voice_disconnect(int(guild_id), seconds=max(1.0, cleanup_grace))
                logger.info("[music/lavalink] tts_session_cleanup | guild=%s failures=%s", guild_id, state.tts_lavalink_failures)
                return {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": 0.0,
                    "playback_ms": max(0.0, (time.monotonic() - playback_started_at) * 1000.0),
                    "playback_started_at": playback_started_at,
                    "tts_lavalink_failed": True,
                    "tts_lavalink_error": str(exc),
                    "tts_lavalink_local_fallback_available": MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK,
                }

        if (
            MUSIC_AGENT_TTS_ROUTE_ENABLED
            and state is not None
            and guild is not None
            and guild_id is not None
            and str(getattr(state, "current_backend", "") or "").lower() == "agent"
            and state.current is not None
        ):
            # Preferir síntese no phone worker quando o worker é dono da voz.
            # O fallback por audio_url só fica para chamadas antigas que já chegam
            # aqui com arquivo pronto e sem o QueueItem original.
            if item is not None and str(getattr(item, "text", "") or "").strip():
                started_agent = time.monotonic()
                result = await _music_agent_command(
                    "tts",
                    guild_id=int(guild_id),
                    voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or getattr(getattr(vc, "channel", None), "id", 0) or 0),
                    text=str(getattr(item, "text", "") or ""),
                    engine=str(getattr(item, "engine", "gtts") or "gtts"),
                    voice=str(getattr(item, "voice", "") or ""),
                    language=str(getattr(item, "language", "pt-br") or "pt-br"),
                    rate=str(getattr(item, "rate", "+0%") or "+0%"),
                    pitch=str(getattr(item, "pitch", "+0Hz") or "+0Hz"),
                    timeout_seconds=max(3.0, float(timeout or MUSIC_AGENT_TTS_TIMEOUT_SECONDS)),
                )
                elapsed_ms = max(0.0, (time.monotonic() - started_agent) * 1000.0)
                state_payload = result.get("state") if isinstance(result, dict) else None
                if isinstance(state_payload, dict):
                    await self.sync_music_agent_state(
                        int(guild_id),
                        state.current,
                        state_payload,
                        voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or getattr(getattr(vc, "channel", None), "id", 0) or 0),
                        text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                        create_panel=True,
                    )
                return {
                    "ok": bool(isinstance(result, dict) and result.get("ok", True)),
                    "tts_agent_route": True,
                    "tts_agent_synthesized_on_worker": True,
                    "source_setup_ms": max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0),
                    "play_call_ms": 0.0,
                    "playback_ms": float((result or {}).get("playback_ms") or elapsed_ms) if isinstance(result, dict) else elapsed_ms,
                    "playback_started_at": playback_started_at,
                    "agent_elapsed_ms": elapsed_ms,
                    "worker_result": result,
                }
            source_paths = await self._lavalink_tts_source_paths_for_playback(path)
            raw_candidates: list[str] = []
            for source_path in source_paths:
                raw_candidates.extend(self._lavalink_tts_candidates_for_path(source_path, timeout=timeout))
            candidates = await self._filter_lavalink_tts_candidates(raw_candidates)
            source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)
            if not candidates:
                logger.warning(
                    "[music/agent] TTS roteado para worker sem URL acessível; evitando assumir a voz local e interromper o player | guild=%s source_paths=%s raw_candidates=%s",
                    guild_id,
                    source_paths,
                    raw_candidates,
                )
                return {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": 0.0,
                    "playback_ms": max(0.0, (time.monotonic() - playback_started_at) * 1000.0),
                    "playback_started_at": playback_started_at,
                    "tts_agent_route": True,
                    "tts_agent_failed": True,
                    "tts_agent_missing_source": True,
                }
            started_agent = time.monotonic()
            result = await _music_agent_command(
                "tts",
                guild_id=int(guild_id),
                voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or getattr(getattr(vc, "channel", None), "id", 0) or 0),
                audio_url=str(candidates[0]),
                engine="prebuilt-url",
                timeout_seconds=max(3.0, float(timeout or MUSIC_AGENT_TTS_TIMEOUT_SECONDS)),
            )
            elapsed_ms = max(0.0, (time.monotonic() - started_agent) * 1000.0)
            state_payload = result.get("state") if isinstance(result, dict) else None
            if isinstance(state_payload, dict):
                await self.sync_music_agent_state(
                    int(guild_id),
                    state.current,
                    state_payload,
                    voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or getattr(getattr(vc, "channel", None), "id", 0) or 0),
                    text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                    create_panel=True,
                )
            return {
                "ok": bool(isinstance(result, dict) and result.get("ok", True)),
                "tts_agent_route": True,
                "tts_agent_prebuilt": True,
                "source_setup_ms": source_setup_ms,
                "play_call_ms": 0.0,
                "playback_ms": float((result or {}).get("playback_ms") or elapsed_ms) if isinstance(result, dict) else elapsed_ms,
                "playback_started_at": playback_started_at,
                "agent_elapsed_ms": elapsed_ms,
                "worker_result": result,
            }

        first_frame = {}
        class MeasuredSource(discord.AudioSource):
            def __init__(self, source):
                self.source = source
            def read(self):
                frame = self.source.read()
                if frame and not first_frame:
                    first_frame['first_frame_at'] = time.monotonic()
                    first_frame['first_frame_observed'] = True
                return frame
            def is_opus(self):
                return self.source.is_opus()
            def cleanup(self):
                self.source.cleanup()
        source_factory = getattr(item, '_tts_source_factory', None)
        mixing = active_source is not None and not getattr(active_source, '_closed', True) and self._vc_is_playing_or_paused(vc)
        raw_source = source_factory(path)[0] if callable(source_factory) and not mixing else discord.FFmpegPCMAudio(path, before_options=before_options, options=options)
        source = MeasuredSource(raw_source)
        source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)

        if active_source is not None and not getattr(active_source, "_closed", True) and (self._vc_is_playing_or_paused(vc)):
            with contextlib.suppress(Exception):
                active_source.set_duck_factor(MUSIC_TTS_LOCAL_DUCK_FACTOR)
            logger.info(
                "[music] tts_duck_local_start | guild=%s duck_percent=%.1f base_volume=%.3f",
                guild_id,
                MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
                getattr(active_source, "normal_music_volume", state.volume if state is not None else 0.0),
            )
            future = active_source.add_tts(source, volume=TTS_VOLUME)
            try:
                await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
            except asyncio.CancelledError:
                active_source.cancel_tts(future)
                raise
            except asyncio.TimeoutError:
                active_source.cancel_tts(future)
                playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
                if MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL:
                    logger.warning(
                        "[music] TTS overlay excedeu %.1fs; cancelando apenas este TTS e mantendo a música/call",
                        float(timeout),
                    )
                    logger.info("[music] tts_duck_local_restore | guild=%s cancelled=True", guild_id)
                    return {
                        "source_setup_ms": source_setup_ms,
                        "play_call_ms": play_call_ms,
                        "playback_ms": playback_ms,
                        "playback_started_at": playback_started_at,
                **first_frame,
                        "tts_overlay_cancelled": True,
                        "tts_local_ducked": True,
                        "tts_local_duck_percent": MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
                    }
                raise RuntimeError(f"Playback TTS em overlay excedeu {float(timeout):.1f}s")
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            logger.info("[music] tts_duck_local_restore | guild=%s cancelled=False", guild_id)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
                **first_frame,
                "tts_local_ducked": True,
                "tts_local_duck_percent": MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
            }

        agent_ducked = False
        if state is not None and guild_id is not None and str(getattr(state, "current_backend", "") or "").lower() == "agent" and state.current is not None:
            agent_ducked = await self._set_music_agent_ducking(int(guild_id), True, state)

        finished = loop.create_future()

        def _after(error: Exception | None) -> None:
            with contextlib.suppress(Exception):
                source.cleanup()

            def _finish_once() -> None:
                if finished.done():
                    return
                if error is None:
                    finished.set_result(None)
                else:
                    finished.set_exception(error)

            loop.call_soon_threadsafe(_finish_once)

        try:
            play_call_started_at = time.monotonic()
            vc.play(source, after=_after)
            play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)
            playback_started_at = time.monotonic()
            await asyncio.wait_for(finished, timeout=max(1.0, float(timeout)))
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
                **first_frame,
                "tts_agent_ducked": bool(agent_ducked),
                "tts_agent_duck_percent": MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT if agent_ducked else 0,
            }
        finally:
            if not finished.done() or finished.cancelled():
                if getattr(vc, 'source', None) is source:
                    with contextlib.suppress(Exception):
                        vc.stop()
                with contextlib.suppress(Exception):
                    source.cleanup()
            if agent_ducked and state is not None and guild_id is not None:
                await self._set_music_agent_ducking(int(guild_id), False, state)

    async def pause(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        state = self.get_state(guild_id)
        if not vc or not self._vc_is_connected(vc) or not self._vc_is_playing(vc) or (state.current is None and state.current_source is None):
            return False
        if self._is_lavalink_voice_client(vc):
            await vc.pause(True)
        else:
            vc.pause()
        state.paused = True
        self._set_current_status(state, "paused")
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def resume(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        state = self.get_state(guild_id)
        if not vc or not self._vc_is_connected(vc) or not self._vc_is_paused(vc) or (state.current is None and state.current_source is None):
            return False
        if self._is_lavalink_voice_client(vc):
            await vc.pause(False)
        else:
            vc.resume()
        state.paused = False
        self._set_current_status(state, "playing")
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def skip(self, guild_id: int, *, add_current_to_history: bool = True) -> bool:
        state = self.get_state(guild_id)
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        did_anything = False
        state.skip_requested = True
        self._cancel_next_prefetch(state)
        state.skip_transition_active = True
        state.skip_history_suppressed_once = not bool(add_current_to_history)
        for _vote_action in ("skip", "stop"):
            state.control_votes.pop(_vote_action, None)
            _vote_task = state.control_vote_cleanup_tasks.pop(_vote_action, None)
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
            did_anything = True
        music_audio_active = state.current is not None or state.current_source is not None or state.current_resolve_task is not None or state.current_lavalink_player is not None
        if music_audio_active and vc and self._vc_is_playing_or_paused(vc):
            with contextlib.suppress(Exception):
                await self._vc_stop_audio(vc)
            did_anything = True
        if state.current is not None or state.current_source is not None or state.current_lavalink_player is not None:
            with contextlib.suppress(Exception):
                if state.current_source is not None:
                    state.current_source.cleanup()
            # Não apaga state.current aqui. O worker ainda precisa saber qual faixa
            # foi pulada para salvar histórico e para o painel não piscar como
            # "Nada tocando agora" enquanto a próxima música está sendo preparada.
            state.current_source = None
            state.current_lavalink_playable = None
            state.paused = False
            self._set_current_status(state, "skipping")
            did_anything = True
        else:
            self._set_current_status(state, "queued" if self._has_pending_track(state) else "idle")
            if self._has_pending_track(state):
                # Pode acontecer durante uma transição rápida de anterior/avançar:
                # não há faixa ativa no instante do clique, mas ainda existe uma
                # próxima música pronta. Nesse caso ⏭️ deve avançar/iniciar, não
                # responder "não havia música".
                did_anything = True
            if not did_anything:
                state.skip_transition_active = False
                state.skip_history_suppressed_once = False
        self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        return did_anything

    async def stop(self, guild_id: int, *, disconnect: bool = True) -> bool:
        state = self.get_state(guild_id)
        state.stop_requested = True
        state.skip_requested = True
        self._cancel_next_prefetch(state)
        self._set_idle_reason(state, "manual_stop")
        self._invalidate_panel_controls_now(guild_id)
        for _vote_action in ("skip", "stop"):
            state.control_votes.pop(_vote_action, None)
            _vote_task = state.control_vote_cleanup_tasks.pop(_vote_action, None)
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
        if state.current_source is not None:
            with contextlib.suppress(Exception):
                state.current_source.cleanup()
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if vc:
            # Stop manual precisa limpar também estados presos de TTS/Lavalink. Antes,
            # se o TTS já tinha tocado/encostado na voz, o botão não desconectava e
            # deixava o bot preso na call.
            with contextlib.suppress(Exception):
                if self._vc_is_playing_or_paused(vc):
                    await self._vc_stop_audio(vc)
            if disconnect:
                with contextlib.suppress(Exception):
                    self._mark_internal_voice_disconnect(guild_id, seconds=8.0)
                    await vc.disconnect(force=True)
                state.music_owns_voice = False
        state.current = None
        state.current_started_at_monotonic = 0.0
        state.current_source = None
        state.current_resolve_task = None
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.lavalink_tts_until = 0.0
        state.lavalink_resume_grace_until = 0.0
        state.tts_session_active_until = 0.0
        state.tts_session_last_error = ""
        state.skip_transition_active = False
        state.skip_history_suppressed_once = False
        self._set_current_status(state, "idle")
        state.paused = False
        state.music_session_active = False
        self._cancel_music_idle_disconnect(state)
        state.control_votes.clear()
        for _vote_task in list(state.control_vote_cleanup_tasks.values()):
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        state.control_vote_cleanup_tasks.clear()
        await self._restore_auto_bitrate_for_state(guild, state, reason="manual_stop")
        await self._restore_voice_status_for_state(guild, state, reason="manual_stop")
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def _seek_lavalink_current(self, guild_id: int, state: MusicGuildState, position_seconds: float) -> bool:
        player = state.current_lavalink_player
        if player is None:
            guild = self.bot.get_guild(int(guild_id))
            player = getattr(guild, "voice_client", None) if guild is not None else None
        if player is None or not self._is_lavalink_voice_client(player):
            return False
        position_ms = max(0, int(float(position_seconds) * 1000))
        seeker = getattr(player, "seek", None)
        if callable(seeker):
            try:
                result = seeker(position_ms)
            except TypeError:
                try:
                    result = seeker(position=position_ms)
                except TypeError:
                    result = seeker(milliseconds=position_ms)
            if asyncio.iscoroutine(result):
                await result
            return True

        # Fallback defensivo para versões sem método ``seek`` exposto: reenvia a
        # faixa atual para o node começando no ponto escolhido.
        playable = state.current_lavalink_playable or getattr(player, "current", None)
        if playable is None:
            return False
        kwargs = {"replace": True, "start": position_ms}
        with contextlib.suppress(Exception):
            kwargs["volume"] = max(0, min(150, int(round(float(state.volume or 1.0) * 100))))
        try:
            result = player.play(playable, **kwargs)
        except TypeError:
            kwargs.pop("volume", None)
            try:
                result = player.play(playable, **kwargs)
            except TypeError:
                result = player.play(playable)
        if asyncio.iscoroutine(result):
            await result
        return True

    async def seek_to(self, guild_id: int, position_seconds: int | float) -> tuple[bool, str]:
        state = self.get_state(guild_id)
        track = state.current
        if track is None:
            return False, "Não há música tocando agora."
        if getattr(track, "is_live", False) or getattr(track, "duration", None) is None:
            return False, "Essa música parece ser live/stream ou não tem duração fixa, então não dá para selecionar momento."
        try:
            target = max(0.0, float(position_seconds))
            duration = float(track.duration)
        except Exception:
            return False, "Tempo inválido. Use algo como `129`, `45` ou `1:29`."
        if duration <= 0:
            return False, "Essa música não tem duração fixa, então não dá para selecionar momento."
        if target > duration:
            return False, f"Esse momento passa da duração da música (`{self._format_seconds(duration)}`)."

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Servidor não encontrado."

        target_label = self._format_seconds(target)
        backend = str(getattr(state, "current_backend", "local") or "local").lower()
        if backend == "agent":
            try:
                result = await _music_agent_command(
                    "seek",
                    guild_id=int(guild_id),
                    position_seconds=target,
                    requester_id=int(getattr(track, "requester_id", 0) or 0),
                    requester_name=str(getattr(track, "requester_name", "") or ""),
                )
            except Exception as exc:
                logger.warning("[music/agent] falha ao selecionar momento | guild=%s target=%.2fs erro=%s", guild_id, target, exc)
                return False, "Não consegui selecionar esse momento no player atual."
            remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
            if remote:
                await self.sync_music_agent_state(
                    int(guild_id),
                    track,
                    remote,
                    voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0) or None,
                    text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0) or None,
                    queued=False,
                    create_panel=True,
                )
            if not bool(isinstance(result, dict) and result.get("ok")):
                return False, str((result or {}).get("error") or "O player atual não aceitou selecionar momento.")
            state.current_started_at_monotonic = time.monotonic()
            state.current_start_offset_seconds = target
            state.paused = False
            self._mark_voice_status_track_change(state)
            self._schedule_voice_status_track_sync(guild_id, repeat_after=0.75, reason="seek")
            self._schedule_panel_update(guild_id, create=bool(state.now_message))
            return True, f"`💠` Pulando para `{target_label}`."

        if backend == "lavalink" or state.current_lavalink_player is not None:
            try:
                ok = await self._seek_lavalink_current(guild_id, state, target)
            except Exception as exc:
                logger.warning("[music/lavalink] falha ao selecionar momento | guild=%s target=%.2fs erro=%s", guild_id, target, exc)
                ok = False
            if not ok:
                return False, "O backend Lavalink atual não aceitou seek nessa música."
            state.current_started_at_monotonic = time.monotonic()
            state.current_start_offset_seconds = target
            state.paused = False
            self._mark_voice_status_track_change(state)
            self._schedule_voice_status_track_sync(guild_id, repeat_after=0.75, reason="seek")
            self._schedule_panel_update(guild_id, create=bool(state.now_message))
            return True, f"`💠` Pulando para `{target_label}`."

        vc = getattr(guild, "voice_client", None)
        if vc is None or not self._vc_is_connected(vc) or (state.current_source is None and not self._vc_is_playing_or_paused(vc)):
            return False, "Não há música local tocando agora."

        # Player local/yt-dlp: FFmpeg não seeka uma source já aberta. Para mudar
        # de ponto sem avançar a fila, recolocamos a faixa atual na frente e
        # paramos a source atual com o histórico suprimido nessa transição.
        state.next_local_start_offset_seconds = target
        pending_same_track = False
        with contextlib.suppress(Exception):
            pending_same_track = bool(state.skip_requested and state.forward_queue and state.forward_queue[0] is track)
        if not pending_same_track:
            state.forward_queue.appendleft(track)
        state.skip_requested = True
        state.stop_requested = False
        state.skip_history_suppressed_once = True
        state.skip_transition_active = True
        state.music_session_active = True
        state.paused = False
        self._set_current_status(state, "skipping")
        self._cancel_music_idle_disconnect(state)
        with contextlib.suppress(Exception):
            if state.current_source is not None:
                state.current_source.cleanup()
        with contextlib.suppress(Exception):
            await self._vc_stop_audio(vc)
        self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        return True, f"`💠` Pulando para `{target_label}`."

    async def set_volume(self, guild_id: int, volume_percent: int) -> float:
        state = self.get_state(guild_id)
        volume = max(0, min(150, int(volume_percent))) / 100.0
        state.volume = volume
        await self._persist_volume(guild_id, volume)
        if state.current_source is not None:
            state.current_source.set_music_volume(volume)
        if state.current_backend == "agent":
            with contextlib.suppress(Exception):
                result = await _music_agent_command("volume", guild_id=int(guild_id), volume_percent=int(round(volume * 100)))
                remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
                if remote:
                    await self.sync_music_agent_state(
                        int(guild_id),
                        state.current,
                        remote,
                        voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0) or None,
                        text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0) or None,
                        queued=False,
                        create_panel=False,
                    )
        if state.current_backend == "lavalink":
            await self.backends.set_lavalink_player_volume(guild_id, int(round(volume * 100)))
        self._schedule_panel_update(guild_id, create=False)
        return volume

    async def request_skip(self, guild_id: int, member) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "skip")
        if not allowed:
            return False, pending_message
        self.cancel_pending_music_operations(guild_id, reason="skip")
        ok = await self.skip(guild_id)
        if completed_by_vote:
            return ok, "`⏭️` Votação concluída: pulando música." if ok else "Não havia música para pular."
        return ok, "`⏭️` Pulando música." if ok else "Não havia música para pular."

    async def request_stop(self, guild_id: int, member, *, disconnect: bool = True) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "stop")
        if not allowed:
            return False, pending_message
        self.cancel_pending_music_operations(guild_id, reason="stop")
        ok = await self.stop(guild_id, disconnect=disconnect)
        if completed_by_vote:
            return ok, "`⏹️` Votação concluída: player encerrado e queue limpo."
        return ok, "`⏹️` Player encerrado e queue limpo."

    async def request_shuffle(self, guild_id: int, member) -> tuple[bool, str]:
        state = self.get_state(guild_id)
        state.control_votes.pop("shuffle", None)
        task = state.control_vote_cleanup_tasks.pop("shuffle", None)
        if task is not None and not task.done():
            task.cancel()
        if member is None or getattr(member, "bot", False):
            return False, "Bots não podem embaralhar a queue."

        # Se o painel local perdeu dados da faixa atual, sincronize rapidamente com
        # o Music Agent antes de validar dono/queue. Isso evita usar fila local
        # vazia contra uma playlist remota com dezenas de faixas.
        if self._should_use_music_agent_queue_controls(state):
            remote = await self._refresh_music_agent_control_state(guild_id, create_panel=False)
            if remote:
                state = self.get_state(guild_id)

        current = state.current
        requester_id = int(getattr(current, "requester_id", 0) or 0) if current is not None else 0
        member_id = int(getattr(member, "id", 0) or 0)
        # Shuffle não é votação nem modo ligado/desligado: é uma ação única e
        # exclusiva de quem colocou a música que está tocando agora.
        if not current or not requester_id:
            return False, "Não há música tocando para definir quem pode embaralhar a queue."
        if member_id != requester_id:
            return False, "Só quem colocou a música atual pode embaralhar a queue."

        remote_count = 0
        with contextlib.suppress(Exception):
            remote_count = int(getattr(state, "agent_remote_queue_size", 0) or 0)
        local_count = len(self.snapshot_queue(guild_id))
        if max(remote_count, local_count) <= 1 and not self._should_use_music_agent_queue_controls(state):
            return False, "Não há músicas suficientes no queue para embaralhar."

        self.cancel_pending_music_operations(guild_id, reason="shuffle")
        ok, reason = await self.shuffle_queue_once(guild_id, member=member)
        if ok:
            return True, "`🔀` Queue embaralhado."
        if reason == "not_enough":
            return False, "Não há músicas suficientes no queue para embaralhar."
        return False, "Não consegui embaralhar a queue agora."

    async def request_loop(self, guild_id: int, member) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "loop")
        if not allowed:
            return False, pending_message
        mode = await self.cycle_loop(guild_id, member=member)
        prefix = "Votação concluída: " if completed_by_vote else ""
        return True, f"`🔁` {prefix}Repetição: `{mode.label}`."

    async def toggle_shuffle(self, guild_id: int) -> bool:
        # Compatibilidade com chamadas antigas: hoje shuffle é ação única.
        ok, _reason = await self.shuffle_queue_once(guild_id)
        return ok

    async def shuffle_queue_once(self, guild_id: int, *, member=None) -> tuple[bool, str]:
        state = self.get_state(guild_id)
        items = self.snapshot_queue(guild_id)
        state.control_votes.pop("shuffle", None)
        state.shuffle = False
        self._cancel_next_prefetch(state)
        if self._should_use_music_agent_queue_controls(state):
            try:
                result = await _music_agent_command(
                    "shuffle",
                    guild_id=int(guild_id),
                    requester_id=int(getattr(member, "id", 0) or 0),
                    requester_name=getattr(member, "display_name", str(member)) if member is not None else "",
                )
                remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
                if remote:
                    await self.sync_music_agent_state(
                        int(guild_id),
                        state.current,
                        remote,
                        voice_channel_id=int(remote.get("voice_channel_id") or getattr(state, "last_voice_channel_id", 0) or 0) or None,
                        text_channel_id=int(remote.get("text_channel_id") or getattr(state, "last_text_channel_id", 0) or 0) or None,
                        queued=False,
                        create_panel=True,
                    )
                state.shuffle = False
                self._schedule_panel_update(guild_id, create=False)
                if bool((result or {}).get("shuffled")):
                    return True, "ok"

                # Evite mensagem falsa de "não há músicas suficientes" quando a
                # própria VPS espelha uma fila remota maior que 1. Re-sincronize uma
                # vez e retorne erro genérico se o worker ainda não embaralhou.
                refreshed = await self._refresh_music_agent_control_state(guild_id, create_panel=True)
                queue_size = 0
                with contextlib.suppress(Exception):
                    queue_size = int((refreshed or remote or {}).get("queue_size") or getattr(self.get_state(guild_id), "agent_remote_queue_size", 0) or 0)
                if queue_size <= 1:
                    return False, "not_enough"
                return False, "remote_failed"
            except Exception:
                logger.warning("[music/agent] falha ao embaralhar queue remoto | guild=%s", guild_id, exc_info=True)
                state.shuffle = False
                self._schedule_panel_update(guild_id, create=False)
                return False, "remote_failed"
        if len(items) > 1:
            random.shuffle(items)
            await self.replace_queue(guild_id, items)
            state.shuffle = False
            self._schedule_panel_update(guild_id, create=False)
            return True, "ok"
        self._schedule_panel_update(guild_id, create=False)
        return False, "not_enough"

    async def cycle_loop(self, guild_id: int, *, member=None) -> LoopMode:
        state = self.get_state(guild_id)
        state.control_votes.pop("loop", None)
        if self._should_use_music_agent_queue_controls(state):
            try:
                result = await _music_agent_command(
                    "loop",
                    guild_id=int(guild_id),
                    requester_id=int(getattr(member, "id", 0) or 0),
                    requester_name=getattr(member, "display_name", str(member)) if member is not None else "",
                )
                remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
                mode_value = str((result or {}).get("mode") or (remote or {}).get("loop_mode") or "").strip().lower()
                if mode_value in {"off", "one", "all"}:
                    state.loop_mode = LoopMode(mode_value)
                if remote:
                    await self.sync_music_agent_state(
                        int(guild_id),
                        state.current,
                        remote,
                        voice_channel_id=int(remote.get("voice_channel_id") or getattr(state, "last_voice_channel_id", 0) or 0) or None,
                        text_channel_id=int(remote.get("text_channel_id") or getattr(state, "last_text_channel_id", 0) or 0) or None,
                        queued=False,
                        create_panel=True,
                    )
                self._schedule_panel_update(guild_id, create=False)
                return state.loop_mode
            except Exception:
                logger.warning("[music/agent] falha ao alternar repetição remota | guild=%s", guild_id, exc_info=True)
                # Se o Music Agent falhar, não minta alterando só a VPS enquanto a
                # fila real está no worker. Mantenha o modo atual.
                self._schedule_panel_update(guild_id, create=False)
                return state.loop_mode
        if state.loop_mode is LoopMode.OFF:
            state.loop_mode = LoopMode.ONE
        elif state.loop_mode is LoopMode.ONE:
            state.loop_mode = LoopMode.ALL
        else:
            state.loop_mode = LoopMode.OFF
        self._schedule_panel_update(guild_id, create=False)
        return state.loop_mode

    def snapshot_queue(self, guild_id: int) -> list[MusicTrack]:
        state = self.get_state(guild_id)
        return self._pending_items(state)

    def history_snapshot(self, guild_id: int) -> list[MusicTrack]:
        state = self.get_state(guild_id)
        return list(state.history)

    def _push_history(self, state: MusicGuildState, track: MusicTrack) -> None:
        # Evita duplicar a mesma música em sequência quando skip/loop dispara rápido.
        try:
            if state.history and state.history[-1].display_url == track.display_url and state.history[-1].title == track.title:
                return
            state.history.append(track)
        except Exception:
            logger.debug("[music] falha ao salvar histórico", exc_info=True)

    def _prepend_queue(self, state: MusicGuildState, track: MusicTrack) -> bool:
        try:
            if state.queue.full():
                return False
            state.queue._queue.appendleft(track)
            return True
        except Exception:
            logger.debug("[music] falha ao colocar música no começo da fila", exc_info=True)
            return False

    async def previous(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        is_agent = str(getattr(state, "current_backend", "") or "").lower() == "agent" and self.music_worker_only_enabled()
        previous_track: MusicTrack | None = None

        if is_agent:
            # O Music Agent é o dono do histórico quando ele é dono da voz.
            # Antes de negar o botão, faça uma leitura curta do estado remoto: a
            # VPS pode estar com agent_remote_history_size atrasado entre uma
            # transição de faixa e o clique do usuário.
            with contextlib.suppress(Exception):
                await self._refresh_music_agent_control_state(guild_id, create_panel=False)
                state = self.get_state(guild_id)
            remote_has_history = bool(int(getattr(state, "agent_remote_history_size", 0) or 0) > 0)
            local_fallback = state.history[-1] if state.history else None
            if local_fallback is None and not remote_has_history:
                # Sem histórico local/remoto, o botão de voltar não reinicia a faixa atual.
                return False
            if not int(getattr(state, "last_voice_channel_id", 0) or 0):
                return False
            payload = {
                "guild_id": int(guild_id),
                "voice_channel_id": int(getattr(state, "last_voice_channel_id", 0) or 0),
                "text_channel_id": int(getattr(state, "last_text_channel_id", 0) or 0),
                "timeout_seconds": getattr(config, "MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", 12.0),
            }
            # Só envie faixa explícita quando o worker realmente não tem
            # histórico remoto. Se os dois lados têm histórico, o remoto manda.
            if not remote_has_history and local_fallback is not None:
                payload.update({
                    "query": local_fallback.webpage_url or local_fallback.original_url or local_fallback.stream_url or local_fallback.title,
                    "track": local_fallback,
                })
            try:
                result = await _music_agent_command("previous", **payload)
            except Exception:
                logger.warning("[music/agent] falha ao voltar histórico pelo worker | guild=%s", guild_id, exc_info=True)
                return False
            if not bool(result.get("ok", True)):
                remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
                if remote:
                    with contextlib.suppress(Exception):
                        await self.sync_music_agent_state(
                            int(guild_id),
                            None,
                            remote,
                            voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0),
                            text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                            queued=False,
                            create_panel=False,
                        )
                return False
            # Se o fallback local foi usado com sucesso, remova só agora.
            if not remote_has_history and local_fallback is not None and state.history:
                with contextlib.suppress(Exception):
                    if state.history[-1] is local_fallback:
                        state.history.pop()
            state.stop_requested = False
            state.skip_requested = False
            state.skip_transition_active = True
            self._clear_idle_reason(state)
            self._cancel_music_idle_disconnect(state)
            remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
            previous_payload = result.get("previous") if isinstance(result, dict) and isinstance(result.get("previous"), dict) else {}
            previous_track = self._track_from_agent_payload(previous_payload, local_fallback) if previous_payload else local_fallback
            await self.sync_music_agent_state(
                int(guild_id),
                previous_track,
                remote,
                voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0),
                text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                queued=False,
                create_panel=True,
            )
            return True

        if state.history:
            try:
                previous_track = state.history.pop()
            except IndexError:
                previous_track = None

        if previous_track is None:
            return False

        current = state.current
        # ``forward_queue`` tem prioridade sobre o queue normal. Por isso a
        # música anterior precisa entrar nela ANTES da música atual. O fluxo
        # correto com duas músicas é:
        # A tocando → ⏭️ B → ⏮️ A → ⏭️ B.
        # Quando o player já está em "Nada tocando", current é None; nesse caso
        # o ⏮️ precisa acordar o worker e tocar o histórico imediatamente, não
        # apenas mostrar "Queue pronto" parado.
        try:
            if current is not None:
                state.forward_queue.appendleft(current)
            state.forward_queue.appendleft(previous_track)
        except Exception:
            logger.debug("[music] falha ao preparar histórico/avanço", exc_info=True)
            return False

        skipped = await self.skip(guild_id, add_current_to_history=False) if current is not None else False
        if current is None:
            state.stop_requested = False
            state.skip_requested = False
            state.skip_transition_active = True
            state.skip_history_suppressed_once = False
            self._set_current_status(state, "skipping")
            self._clear_idle_reason(state)
            self._cancel_music_idle_disconnect(state)
            state.music_session_active = True
        self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=True)
        return True if skipped or previous_track else False

    async def readd_history(self, guild_id: int, *, limit: int = 20) -> int:
        state = self.get_state(guild_id)
        items = list(state.history)[-max(1, int(limit)):]
        if not items:
            return 0
        added = 0
        for track in items:
            if state.queue.full():
                break
            await state.queue.put(track)
            added += 1
        if added:
            state.stop_requested = False
            self.ensure_music_worker(guild_id)
            self._schedule_panel_update(guild_id, create=True)
        return added

    async def skip_to(self, guild_id: int, index_1based: int) -> bool:
        state = self.get_state(guild_id)
        items = self.snapshot_queue(guild_id)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            return False
        selected = items.pop(idx)
        items.insert(0, selected)
        await self.replace_queue(guild_id, items)
        state.stop_requested = False
        if state.current is not None or state.current_source is not None or state.current_resolve_task is not None:
            await self.skip(guild_id)
        else:
            self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=True)
        return True

    async def replace_queue(self, guild_id: int, tracks: list[MusicTrack]) -> None:
        state = self.get_state(guild_id)
        self._cancel_next_prefetch(state)
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        for track in tracks[:MUSIC_QUEUE_MAXSIZE]:
            await state.queue.put(track)
        if tracks:
            state.music_session_active = True
            self._cancel_music_idle_disconnect(state)
            if state.current is not None or state.current_source is not None:
                self._start_prefetch_next(guild_id, state)
        self._schedule_panel_update(guild_id, create=bool(state.now_message or state.current or tracks))

    async def remove_at(self, guild_id: int, index_1based: int) -> Optional[MusicTrack]:
        items = self.snapshot_queue(guild_id)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            return None
        removed = items.pop(idx)
        await self.replace_queue(guild_id, items)
        return removed

    async def move(self, guild_id: int, from_pos: int, to_pos: int) -> bool:
        items = self.snapshot_queue(guild_id)
        src = int(from_pos) - 1
        dst = int(to_pos) - 1
        if src < 0 or src >= len(items) or dst < 0 or dst >= len(items):
            return False
        track = items.pop(src)
        items.insert(dst, track)
        await self.replace_queue(guild_id, items)
        return True
