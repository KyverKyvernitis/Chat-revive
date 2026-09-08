#!/usr/bin/env python3
"""Core Music Agent for the phone worker.

Same-bot music plane: the VPS remains the UI/status plane while
this process owns Discord voice/Lavalink/yt-dlp on the phone worker.

The agent intentionally does not register Discord commands and does not handle
message events. It exposes a small localhost HTTP API that phone_worker.py can
proxy through its authenticated /task endpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import importlib
import base64
import hashlib
import io
import queue
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import signal
import subprocess
import secrets
import tempfile
import threading
import time
from array import array
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from aiohttp import web
except Exception as exc:  # pragma: no cover - startup dependency error
    raise SystemExit(f"aiohttp ausente no Music Agent: {exc}")

try:
    import discord
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"discord.py ausente no Music Agent: {exc}")

try:
    import wavelink
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"wavelink ausente no Music Agent: {exc}")

class _TimedTTSSource(discord.AudioSource):
    def __init__(self, source, *, started, reader=None, buffered=False):
        self.source, self.started, self.reader = source, started, reader
        self.first_frame_ms = None
        self.closed = False
        self.error = None
        self.frames = queue.Queue(maxsize=25) if buffered else None
        self.ready = threading.Event()
        if buffered:
            threading.Thread(target=self._fill, name='tts-pcm-reader', daemon=True).start()

    def _fill(self):
        try:
            while not self.closed:
                frame = self.source.read()
                while not self.closed:
                    try:
                        self.frames.put(frame, timeout=.1)
                        self.ready.set()
                        break
                    except queue.Full:
                        continue
                if not frame:
                    break
        except Exception as error:
            self.error = error
            self.ready.set()
        finally:
            self.ready.set()

    def read(self):
        if self.closed:
            return b''
        if self.frames is None:
            frame = self.source.read()
        else:
            try:
                frame = self.frames.get_nowait()
            except queue.Empty:
                if self.error:
                    raise self.error
                return bytes(3840)  # keep the music clock running during provider stalls
        if not frame and self.reader is not None and self.reader.error is not None:
            raise RuntimeError('síntese progressiva incompleta') from self.reader.error
        if frame and self.first_frame_ms is None:
            self.first_frame_ms = (time.monotonic() - self.started) * 1000
        return frame

    def is_opus(self):
        return self.source.is_opus()

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self.reader is not None:
            self.reader.close()
        self.source.cleanup()


_TTS_MAINTENANCE_LOCK = threading.Lock()
_TTS_MAINTENANCE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='music-cache-prune')
_TTS_MAINTENANCE_PENDING = None
_TTS_MAINTENANCE_RUNNING = False
_TTS_CACHE_TOUCHES = {}


def _schedule_tts_prune(callback, path):
    global _TTS_MAINTENANCE_PENDING, _TTS_MAINTENANCE_RUNNING
    with _TTS_MAINTENANCE_LOCK:
        _TTS_MAINTENANCE_PENDING = callback, path
        if _TTS_MAINTENANCE_RUNNING:
            return
        _TTS_MAINTENANCE_RUNNING = True
    def run():
        global _TTS_MAINTENANCE_PENDING, _TTS_MAINTENANCE_RUNNING
        while True:
            with _TTS_MAINTENANCE_LOCK:
                current = _TTS_MAINTENANCE_PENDING
                _TTS_MAINTENANCE_PENDING = None
                if current is None:
                    _TTS_MAINTENANCE_RUNNING = False
                    return
            with contextlib.suppress(Exception):
                current[0](protected=current[1])
    try:
        _TTS_MAINTENANCE_POOL.submit(run)
    except RuntimeError:
        with _TTS_MAINTENANCE_LOCK:
            _TTS_MAINTENANCE_RUNNING = False
            _TTS_MAINTENANCE_PENDING = None


AGENT_VERSION = "0.3.28"
STARTED_AT = time.time()


def load_env_file(path: Path, *, override: bool = False) -> None:
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if override or key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def bootstrap_env() -> None:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    load_env_file(Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), override=False)
    env_file = Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()
    load_env_file(env_file, override=False)
    if not str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip():
        token = secrets.token_urlsafe(32)
        os.environ["MUSIC_AGENT_TOKEN"] = token
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            old = env_file.read_text("utf-8", errors="replace") if env_file.exists() else ""
            lines: list[str] = []
            replaced = False
            for line in old.splitlines():
                if re.match(r"^\s*MUSIC_AGENT_TOKEN\s*=", line):
                    if not replaced:
                        lines.append("MUSIC_AGENT_TOKEN=" + token)
                        replaced = True
                    continue
                lines.append(line)
            if not replaced:
                lines.append("MUSIC_AGENT_TOKEN=" + token)
            env_file.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
            with contextlib.suppress(Exception):
                os.chmod(env_file, 0o600)
        except Exception:
            pass


bootstrap_env()


_LOCAL_SEARCH_PREFIXES = ("ytsearch", "ytmsearch")
_LAVALINK_PREFIXES = ("scsearch:", "spsearch:", "amsearch:", "dzsearch:")


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return text in {"1", "true", "yes", "y", "on", "sim"}


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def short_text(value: object, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def safe_id(value: object) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def read_lavalink_password_from_yaml() -> str:
    path = Path(os.getenv("MUSIC_AGENT_LAVALINK_CONFIG") or Path.home() / "lavalink" / "application.yml").expanduser()
    if not path.exists():
        return ""
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if line.strip().lower().startswith("password:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except Exception:
        return ""
    return ""


def _looks_like_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value) != "" else None
    except Exception:
        return None


def _metadata_text(value: Any, *, limit: int = 180) -> str:
    text = short_text(value, limit)
    lower = text.lower()
    if lower in {"youtube", "link", "música", "musica", "worker-agent", "music-agent-ytdlp", "worker-ytdlp", "desconhecida", "unknown"}:
        return ""
    if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
        return ""
    return text


def _duration_from_ytdlp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return None
    try:
        return float(text)
    except Exception:
        pass
    parts = text.split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(float(part))
        return float(total)
    except Exception:
        return None


PCM_FRAME_BYTES = 3840


class AgentMixedAudioSource(discord.AudioSource):
    """PCM mixer used by the worker-owned voice session.

    It keeps music and TTS inside the same Discord voice connection, so TTS does
    not make the VPS bot steal/disconnect the Music Agent session.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, duck_factor: float = 0.08) -> None:
        self.loop = loop
        self.music_source = music_source
        self.normal_music_volume = max(0.0, min(2.0, float(music_volume)))
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self._overlays: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._closed = False
        self._music_ended = False

    def is_opus(self) -> bool:
        return False

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def add_tts(self, source: discord.AudioSource, *, volume: float = 1.0) -> asyncio.Future:
        future = self.loop.create_future()
        with self._lock:
            self._overlays.append({"source": source, "volume": max(0.0, min(2.0, float(volume))), "future": future, "ended": False})
        return future

    def _future_result(self, future: asyncio.Future, value: object = None) -> None:
        def _set() -> None:
            if not future.done():
                future.set_result(value)
        self.loop.call_soon_threadsafe(_set)

    def _future_exception(self, future: asyncio.Future, error: Exception) -> None:
        def _set() -> None:
            if not future.done():
                future.set_exception(error)
        self.loop.call_soon_threadsafe(_set)

    def cancel_tts(self, future: asyncio.Future) -> None:
        target = None
        with self._lock:
            for overlay in self._overlays:
                if overlay["future"] is future:
                    target = overlay
                    self._overlays.remove(overlay)
                    break
        if target is not None:
            with contextlib.suppress(Exception):
                target["source"].cleanup()
        if not future.done():
            future.cancel()

    def _limit(self, value: int) -> int:
        return max(-32768, min(32767, int(value)))

    def _samples(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(volume - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit(int(sample * volume))
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._samples(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            base[i] = self._limit(int(base[i]) + int(sample))

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            overlays = list(self._overlays)
        music_frame = b""
        if not self._music_ended:
            music_frame = self.music_source.read()
            if not music_frame:
                self._music_ended = True
                with contextlib.suppress(Exception):
                    self.music_source.cleanup()
        if not music_frame and not overlays:
            self.cleanup()
            return b""
        music_volume = self.normal_music_volume * (self.duck_factor if overlays else 1.0)
        if music_frame:
            base = self._samples(music_frame, music_volume)
        else:
            base = array("h", [0] * (PCM_FRAME_BYTES // 2))
        ended: list[dict[str, Any]] = []
        for overlay in overlays:
            source = overlay.get("source")
            future = overlay.get("future")
            try:
                frame = source.read() if source is not None else b""
            except Exception as exc:
                if isinstance(future, asyncio.Future):
                    self._future_exception(future, exc)
                ended.append(overlay)
                continue
            if frame:
                self._mix_into(base, frame, float(overlay.get("volume") or 1.0))
            else:
                with contextlib.suppress(Exception):
                    source.cleanup()
                if isinstance(future, asyncio.Future):
                    self._future_result(future, None)
                ended.append(overlay)
        if ended:
            with self._lock:
                self._overlays = [ov for ov in self._overlays if ov not in ended]
        return base.tobytes()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.music_source.cleanup()
        with self._lock:
            overlays = list(self._overlays)
            self._overlays.clear()
        for overlay in overlays:
            with contextlib.suppress(Exception):
                overlay.get("source").cleanup()
            future = overlay.get("future")
            if isinstance(future, asyncio.Future):
                self._future_result(future, None)


def _format_audio_info(fmt: dict[str, Any], *, url: str = "") -> dict[str, Any]:
    info = {"stream_url": url or str(fmt.get("url") or "").strip()}
    info["audio_format_id"] = short_text(fmt.get("format_id"), 40)
    info["audio_ext"] = short_text(fmt.get("ext"), 20).lower()
    info["audio_codec"] = short_text(fmt.get("acodec") or fmt.get("codec"), 40).lower()
    with contextlib.suppress(Exception):
        info["audio_abr"] = int(float(fmt.get("abr") or fmt.get("tbr") or 0))
    return info


def _select_stream_info(entry: dict[str, Any]) -> dict[str, Any]:
    for item in entry.get("requested_downloads") or []:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return _format_audio_info(item, url=url)
    url = str(entry.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
        return _format_audio_info(entry, url=url)
    best: dict[str, Any] = {}
    best_score = -1.0
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        candidate = str(fmt.get("url") or "").strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        acodec = str(fmt.get("acodec") or "").lower()
        if acodec in {"", "none"}:
            continue
        score = float(fmt.get("abr") or fmt.get("tbr") or 0)
        if str(fmt.get("vcodec") or "").lower() in {"", "none"}:
            score += 10000
        if score > best_score:
            best = _format_audio_info(fmt, url=candidate)
            best_score = score
    return best


def _select_stream_url(entry: dict[str, Any]) -> str:
    return str(_select_stream_info(entry).get("stream_url") or "")


@dataclass
class AgentTrack:
    title: str = "Música"
    requester_id: int = 0
    requester_name: str = ""
    query: str = ""
    webpage_url: str = ""
    stream_url: str = ""
    duration: float | None = None
    uploader: str = ""
    thumbnail: str = ""
    source: str = "worker-agent"
    transport_hint: str = ""
    audio_format_id: str = ""
    audio_ext: str = ""
    audio_codec: str = ""
    audio_abr: int = 0
    start_offset_seconds: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "requester_id": self.requester_id,
            "requester_name": self.requester_name,
            "query": self.query,
            "webpage_url": self.webpage_url,
            "duration": self.duration,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "source": self.source,
            "transport_hint": self.transport_hint,
            "audio_format_id": self.audio_format_id,
            "audio_ext": self.audio_ext,
            "audio_codec": self.audio_codec,
            "audio_abr": self.audio_abr,
            "resolved_audio_format_id": self.audio_format_id,
            "resolved_audio_ext": self.audio_ext,
            "resolved_audio_codec": self.audio_codec,
            "resolved_audio_abr": self.audio_abr,
            "resolved_audio_max_abr": self.audio_abr,
            "start_offset_seconds": self.start_offset_seconds,
        }


@dataclass
class GuildMusicState:
    guild_id: int
    voice_channel_id: int = 0
    text_channel_id: int = 0
    current: AgentTrack | None = None
    queue: list[AgentTrack] = field(default_factory=list)
    history: list[AgentTrack] = field(default_factory=list)
    status: str = "idle"
    paused: bool = False
    last_error: str = ""
    last_action: str = ""
    last_event: str = ""
    transport: str = ""
    preparing_since: float = 0.0
    playing_since: float = 0.0
    started_monotonic: float = 0.0
    updated_at: float = field(default_factory=time.time)
    player: Any = None
    volume_percent: int = 55
    normal_volume_percent: int = 55
    ducked: bool = False
    playback_token: int = 0
    shuffle: bool = False
    loop_mode: str = "off"

    def public(self) -> dict[str, Any]:
        player = self.player
        voice_connected = False
        playing = False
        position_ms = 0
        if player is not None:
            with contextlib.suppress(Exception):
                connected_attr = getattr(player, "connected", None)
                voice_connected = bool(connected_attr) if connected_attr is not None else voice_connected
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_connected", None)
                if callable(checker):
                    voice_connected = bool(checker())
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_playing", None)
                if callable(checker):
                    playing = bool(checker())
            with contextlib.suppress(Exception):
                playing = bool(playing or getattr(player, "playing", False))
            with contextlib.suppress(Exception):
                position_ms = int(float(getattr(player, "position", 0) or 0))
        if position_ms <= 0 and self.current is not None and self.status in {"playing", "paused"} and self.started_monotonic:
            with contextlib.suppress(Exception):
                base = max(0.0, float(getattr(self.current, "start_offset_seconds", 0.0) or 0.0))
                position_ms = int(max(0.0, base + (time.monotonic() - float(self.started_monotonic))) * 1000)
        status_age = max(0.0, time.time() - float(self.updated_at or time.time()))
        return {
            "guild_id": self.guild_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "status": self.status,
            "paused": self.paused,
            "last_error": self.last_error,
            "last_action": self.last_action,
            "last_event": self.last_event,
            "transport": self.transport,
            "preparing_since": self.preparing_since,
            "playing_since": self.playing_since,
            "status_age_seconds": round(status_age, 2),
            "voice_connected": voice_connected,
            "player_present": player is not None,
            "player_playing": playing,
            "position_ms": position_ms,
            "confirmed_playing": bool(
                self.status == "playing"
                and player is not None
                and voice_connected
                and (playing or position_ms > 0)
            ),
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": len(self.queue),
            "history_size": len(self.history),
            "previous_available": bool(self.history),
            "volume_percent": self.volume_percent,
            "normal_volume_percent": self.normal_volume_percent,
            "ducked": self.ducked,
            "shuffle": False,
            "loop_mode": self.loop_mode,
            "repeat": self.loop_mode,
            "queue": [item.public() for item in self.queue[:10]],
        }


class MusicAgent:
    def __init__(self) -> None:
        self.host = os.getenv("MUSIC_AGENT_HOST", "127.0.0.1")
        self.port = env_int("MUSIC_AGENT_PORT", 8780)
        self.token = os.getenv("MUSIC_AGENT_TOKEN") or os.getenv("PHONE_WORKER_TOKEN") or ""
        self.discord_token = os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        self.lavalink_uri = os.getenv("MUSIC_AGENT_LAVALINK_URI") or os.getenv("LAVALINK_URI") or "http://127.0.0.1:2333"
        self.lavalink_password = os.getenv("MUSIC_AGENT_LAVALINK_PASSWORD") or os.getenv("LAVALINK_PASSWORD") or read_lavalink_password_from_yaml()
        self.lavalink_node_name = os.getenv("MUSIC_AGENT_LAVALINK_NODE_NAME", "phone-agent")
        self.ytdlp_format = os.getenv("MUSIC_AGENT_YTDLP_FORMAT") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or "bestaudio[acodec=opus]/bestaudio/best"
        self.ytdlp_timeout = env_int("MUSIC_AGENT_YTDLP_TIMEOUT_SECONDS", 35)
        self.cookies_file = os.getenv("MUSIC_AGENT_YTDLP_COOKIES_FILE") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE") or str(Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt")
        self.js_runtimes = os.getenv("MUSIC_AGENT_YTDLP_JS_RUNTIMES") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES") or "node"
        self.default_search = os.getenv("MUSIC_AGENT_YTDLP_DEFAULT_SEARCH") or "ytsearch5"
        self.direct_audio_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_AUDIO_ENABLED"), True)
        self.direct_youtube_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_YOUTUBE_ENABLED"), True)
        self.lavalink_for_direct_streams = truthy(os.getenv("MUSIC_AGENT_LAVALINK_FOR_DIRECT_STREAMS"), False)
        self.ffmpeg_executable = os.getenv("MUSIC_AGENT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
        self.ffmpeg_before_options = os.getenv(
            "MUSIC_AGENT_FFMPEG_BEFORE_OPTIONS",
            "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 15000000",
        )
        self.ffmpeg_options = os.getenv("MUSIC_AGENT_FFMPEG_OPTIONS", "-vn -sn -dn -loglevel warning")
        self.ffmpeg_bitrate = env_int("MUSIC_AGENT_FFMPEG_OPUS_BITRATE_KBPS", 128)
        self.default_volume_percent = max(0, min(150, env_int("MUSIC_AGENT_DEFAULT_VOLUME_PERCENT", 55)))
        self.duck_volume_percent = max(0, min(100, env_int("MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", 8)))
        # PCMVolumeTransformer lets the worker-owned direct voice path duck TTS and restore volume.
        self.direct_pcm_volume_enabled = truthy(os.getenv("MUSIC_AGENT_DIRECT_PCM_VOLUME_ENABLED"), True)
        self.prepare_timeout = env_float("MUSIC_AGENT_PREPARING_TIMEOUT_SECONDS", 30.0)
        normal_idle = env_float("MUSIC_IDLE_DISCONNECT_SECONDS", 120.0)
        min_idle = max(15.0, env_float("MUSIC_AGENT_MIN_IDLE_DISCONNECT_SECONDS", normal_idle))
        self.idle_disconnect_seconds = max(min_idle, env_float("MUSIC_AGENT_IDLE_DISCONNECT_SECONDS", normal_idle))
        # Cache separado: metadata pode viver muito mais que URL tocável. URLs
        # diretas do YouTube/googlevideo expiram, então o cache de stream é curto
        # e é invalidado em erro de playback.
        legacy_cache_ttl = env_float("MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS", 300.0)
        self.metadata_cache_ttl = max(0.0, env_float("MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS", 21600.0))
        self.stream_cache_ttl = max(0.0, env_float("MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS", min(max(legacy_cache_ttl, 1.0), 300.0)))
        self.resolve_cache_ttl = self.stream_cache_ttl
        self.prefetch_enabled = truthy(os.getenv("MUSIC_AGENT_PREFETCH_ENABLED"), True)
        self.prefetch_timeout = max(3.0, env_float("MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS", 18.0))
        self._idle_disconnect_tasks: dict[int, asyncio.Task] = {}
        self._tts_direct_locks: dict[int, asyncio.Lock] = {}
        self._metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._resolve_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._resolve_locks: dict[str, asyncio.Lock] = {}
        self._prefetch_tasks: dict[str, asyncio.Task] = {}
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.states: dict[int, GuildMusicState] = {}
        self._pool_connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = web.Application()
        self._app.add_routes([
            web.get("/health", self.handle_health),
            web.post("/command", self.handle_command),
        ])
        self._wire_discord_events()

    def log(self, event: str, *, guild_id: int = 0, **fields: Any) -> None:
        details = " ".join(f"{key}={short_text(value, 220)!r}" for key, value in fields.items() if value is not None and value != "")
        gid = f" guild={guild_id}" if guild_id else ""
        print(f"[music-agent] {event}{gid}{(' ' + details) if details else ''}", flush=True)

    def _wire_discord_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:  # type: ignore[no-untyped-def]
            self.log("discord_ready", user=str(self.client.user), version=AGENT_VERSION)
            with contextlib.suppress(Exception):
                await self.ensure_lavalink_pool()

        @self.client.event
        async def on_wavelink_track_start(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                if st.transport == "lavalink":
                    self._set_status(st, "playing", event="lavalink_track_start")
                    self.log("play_started", guild_id=guild_id, transport="lavalink", title=getattr(st.current, "title", ""))
                    self._schedule_next_queue_prefetch(guild_id, reason="lavalink_playing")

        @self.client.event
        async def on_wavelink_track_end(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                self.log("play_ended", guild_id=guild_id, transport="lavalink")
                await self._finish_current(guild_id, error=None, event="lavalink_track_end")

        @self.client.event
        async def on_wavelink_track_exception(payload: Any) -> None:  # type: ignore[no-untyped-def]
            player = getattr(payload, "player", None)
            guild_id = safe_id(getattr(getattr(player, "guild", None), "id", 0))
            if guild_id:
                err = short_text(getattr(payload, "exception", "erro no Lavalink"), 260)
                st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                self._set_status(st, "failed", event="lavalink_track_exception", error=err)
                self.log("play_failed", guild_id=guild_id, transport="lavalink", error=err)
                if st.queue:
                    await self._play_next(guild_id)

    def _resolve_cache_key(self, query: str, track_meta: dict[str, Any] | None = None) -> str:
        meta = track_meta or {}
        raw = str(meta.get("webpage_url") or meta.get("original_url") or meta.get("stream_url") or query or "").strip().lower()
        raw = re.sub(r"[?&](utm_[^=&]+|feature|si)=[^&]+", "", raw)
        return raw or str(query or "").strip().lower()

    def _cache_prune_one(self, cache: dict[str, tuple[float, dict[str, Any]]]) -> None:
        if not cache:
            return
        oldest = min(cache.items(), key=lambda item: item[1][0])[0]
        cache.pop(oldest, None)

    def _metadata_cache_get(self, key: str) -> dict[str, Any] | None:
        if self.metadata_cache_ttl <= 0 or not key:
            return None
        item = self._metadata_cache.get(key)
        if not item:
            return None
        created, data = item
        if time.monotonic() - created > self.metadata_cache_ttl:
            self._metadata_cache.pop(key, None)
            return None
        return dict(data)

    def _metadata_cache_put(self, key: str, data: dict[str, Any]) -> None:
        if self.metadata_cache_ttl <= 0 or not key or not data:
            return
        stable = dict(data)
        for volatile_key in ("stream_url", "url", "direct_url", "http_headers"):
            stable.pop(volatile_key, None)
        if len(self._metadata_cache) >= 512:
            self._cache_prune_one(self._metadata_cache)
        self._metadata_cache[key] = (time.monotonic(), stable)

    def _resolve_cache_get(self, key: str) -> dict[str, Any] | None:
        if self.stream_cache_ttl <= 0 or not key:
            return None
        item = self._resolve_cache.get(key)
        if not item:
            return None
        created, data = item
        if time.monotonic() - created > self.stream_cache_ttl:
            # Preserve metadata even when the playable URL expired.
            self._metadata_cache_put(key, data)
            self._resolve_cache.pop(key, None)
            return None
        return dict(data)

    def _resolve_cache_put(self, key: str, data: dict[str, Any]) -> None:
        if not key or not data:
            return
        self._metadata_cache_put(key, data)
        if self.stream_cache_ttl <= 0 or not data.get("stream_url"):
            return
        if len(self._resolve_cache) >= 128:
            self._cache_prune_one(self._resolve_cache)
        self._resolve_cache[key] = (time.monotonic(), dict(data))

    def _invalidate_stream_cache(self, key: str) -> None:
        if key:
            self._resolve_cache.pop(key, None)

    def _invalidate_track_stream_cache(self, track: AgentTrack | None) -> None:
        if track is None:
            return
        meta = track.public()
        key = self._resolve_cache_key(track.query or track.webpage_url or track.title, meta)
        self._invalidate_stream_cache(key)

    def _agent_track_from_resolved(self, resolved: dict[str, Any], *, query: str, track_meta: dict[str, Any], body: dict[str, Any], cached: bool = False) -> AgentTrack:
        title_hint = _metadata_text(track_meta.get("title") or body.get("title"), limit=160)
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        meta_uploader = _metadata_text(track_meta.get("uploader"), limit=120)
        meta_duration = _float_or_none(track_meta.get("duration"))
        source = short_text(track_meta.get("source") or body.get("source") or "worker-agent", 80)
        webpage_url = str(track_meta.get("webpage_url") or track_meta.get("original_url") or resolved.get("webpage_url") or query).strip()
        resolved_title = _metadata_text(resolved.get("title"), limit=160)
        resolved_uploader = _metadata_text(resolved.get("uploader"), limit=120)
        track = AgentTrack(
            title=title_hint or resolved_title or short_text(query, 160) or "Música",
            requester_id=requester_id,
            requester_name=requester_name,
            query=query,
            webpage_url=webpage_url,
            stream_url=str(resolved.get("stream_url") or ""),
            duration=meta_duration if meta_duration is not None else _float_or_none(resolved.get("duration")),
            uploader=meta_uploader or resolved_uploader,
            thumbnail=short_text(track_meta.get("thumbnail") or resolved.get("thumbnail"), 500),
            source=source if source and source != "worker-agent" else "music-agent-ytdlp",
            transport_hint="direct-cache" if cached else "direct",
            audio_format_id=short_text(resolved.get("audio_format_id") or resolved.get("format_id"), 40),
            audio_ext=short_text(resolved.get("audio_ext") or resolved.get("ext"), 20).lower(),
            audio_codec=short_text(resolved.get("audio_codec") or resolved.get("codec"), 40).lower(),
            audio_abr=int(float(resolved.get("audio_abr") or resolved.get("abr") or 0) or 0),
            start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
        )
        return track

    def _metadata_source_kind(self, track_meta: dict[str, Any]) -> str:
        raw = " ".join(str(track_meta.get(key) or "") for key in (
            "source", "display_source", "extractor", "extractor_key", "ie_key", "webpage_url", "original_url", "query"
        )).lower()
        if "spotify" in raw:
            return "spotify"
        if "deezer" in raw:
            return "deezer"
        if "apple" in raw or "music.apple" in raw:
            return "apple"
        if "youtube" in raw or "youtu.be" in raw:
            return "youtube"
        if "soundcloud" in raw:
            return "soundcloud"
        return ""

    def _query_from_track_meta(self, track_meta: dict[str, Any] | None, *, fallback_query: Any = "") -> str:
        meta = track_meta or {}
        source_kind = self._metadata_source_kind(meta)
        raw_query = str(meta.get("query") or fallback_query or "").strip()
        direct = str(meta.get("stream_url") or meta.get("direct_url") or "").strip()
        if direct.startswith(("http://", "https://")):
            return direct
        original_url = str(meta.get("webpage_url") or meta.get("original_url") or "").strip()
        url_is_metadata_only = source_kind in {"spotify", "deezer", "apple"}
        if original_url.startswith(("http://", "https://")) and not url_is_metadata_only:
            return original_url
        if raw_query.startswith(("http://", "https://")) and not any(marker in raw_query.lower() for marker in ("spotify.com", "deezer.com", "music.apple.com")):
            return raw_query
        title = _metadata_text(meta.get("display_title") or meta.get("title") or meta.get("track") or fallback_query, limit=160)
        artist = _metadata_text(meta.get("display_uploader") or meta.get("uploader") or meta.get("artist") or meta.get("creator") or meta.get("channel"), limit=120)
        if artist and title and artist.lower() not in title.lower():
            text = f"{artist} - {title}"
        else:
            text = title or artist or raw_query
        if source_kind in {"spotify", "deezer", "apple"} and text and "official" not in text.lower():
            text = f"{text} official audio"
        return text.strip()

    def _agent_track_from_metadata(self, track_meta: dict[str, Any], *, body: dict[str, Any], fallback_query: Any = "") -> AgentTrack:
        query = self._query_from_track_meta(track_meta, fallback_query=fallback_query)
        title = _metadata_text(track_meta.get("display_title") or track_meta.get("title") or query, limit=160) or "Música"
        uploader = _metadata_text(track_meta.get("display_uploader") or track_meta.get("uploader") or track_meta.get("artist") or track_meta.get("channel"), limit=120)
        source = short_text(track_meta.get("display_source") or track_meta.get("source") or body.get("source") or "worker-ytdlp", 80)
        return AgentTrack(
            title=title,
            requester_id=safe_id(body.get("requester_id") or track_meta.get("requester_id")),
            requester_name=short_text(body.get("requester_name") or track_meta.get("requester_name"), 80),
            query=query,
            webpage_url=str(track_meta.get("webpage_url") or track_meta.get("original_url") or "").strip(),
            stream_url=str(track_meta.get("stream_url") or "").strip(),
            duration=_float_or_none(track_meta.get("duration")),
            uploader=uploader,
            thumbnail=short_text(track_meta.get("thumbnail"), 500),
            source=source,
            transport_hint="metadata-lazy" if not str(track_meta.get("stream_url") or "").strip() else "direct",
            audio_format_id=short_text(track_meta.get("resolved_audio_format_id") or track_meta.get("audio_format_id"), 40),
            audio_ext=short_text(track_meta.get("resolved_audio_ext") or track_meta.get("audio_ext"), 20).lower(),
            audio_codec=short_text(track_meta.get("resolved_audio_codec") or track_meta.get("audio_codec"), 40).lower(),
            audio_abr=int(float(track_meta.get("resolved_audio_abr") or track_meta.get("audio_abr") or 0) or 0),
            start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
        )

    async def _prefetch_track(self, body: dict[str, Any], track_meta: dict[str, Any], query: str, cache_key: str) -> None:
        try:
            started = time.time()
            await asyncio.wait_for(self.resolve_track(query, track_meta=track_meta, body=body), timeout=self.prefetch_timeout)
            self.log("prefetch_ok", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), elapsed_ms=round((time.time() - started) * 1000.0, 1))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.log("prefetch_failed", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), error=short_text(exc, 180))
        finally:
            self._prefetch_tasks.pop(cache_key, None)
            self._prefetch_tasks.pop(self._guild_prefetch_key(safe_id(body.get("guild_id")), cache_key), None)

    def _auth_ok(self, request: web.Request) -> bool:
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}" or request.headers.get("X-Music-Agent-Token") == self.token

    def _cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self._idle_disconnect_tasks.pop(int(guild_id or 0), None)
        if task and not task.done():
            task.cancel()

    def _schedule_idle_disconnect(self, guild_id: int) -> None:
        guild_id = int(guild_id or 0)
        if not guild_id:
            return
        self._cancel_idle_disconnect(guild_id)
        delay = max(15.0, float(self.idle_disconnect_seconds or 120.0))
        self._idle_disconnect_tasks[guild_id] = asyncio.create_task(self._idle_disconnect_later(guild_id, delay))

    async def _idle_disconnect_later(self, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
            if st.current is not None or st.queue or st.status not in {"idle", "stopped"}:
                return
            player = st.player
            if player is None:
                return
            st.player = None
            st.transport = ""
            st.paused = False
            self._set_status(st, "idle", event="idle_timeout_disconnect")
            with contextlib.suppress(Exception):
                if isinstance(player, getattr(wavelink, "Player", ())):
                    await player.disconnect()
                else:
                    if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                        player.stop()
                    await player.disconnect(force=True)
            self.log("idle_timeout_disconnect", guild_id=guild_id, delay=round(delay, 1))
        except asyncio.CancelledError:
            return
        finally:
            self._idle_disconnect_tasks.pop(guild_id, None)

    def _set_status(self, st: GuildMusicState, status: str, *, event: str = "", error: str = "") -> None:
        now = time.time()
        st.status = status
        st.updated_at = now
        if event:
            st.last_event = event
        if error:
            st.last_error = short_text(error, 320)
        elif status not in {"failed", "error"}:
            st.last_error = ""
        if status in {"preparing", "starting"}:
            st.preparing_since = now
            st.playing_since = 0.0
        elif status == "playing":
            if not st.playing_since:
                st.playing_since = now
            if not st.started_monotonic:
                st.started_monotonic = time.monotonic()
            st.preparing_since = 0.0
        elif status in {"idle", "failed", "error"}:
            st.preparing_since = 0.0
            if status != "playing":
                st.playing_since = 0.0
                st.started_monotonic = 0.0

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response(self.status_payload())

    async def handle_command(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            result = await self.dispatch(body)
            return web.json_response(result)
        except Exception as exc:
            self.log("command_error", action=body.get("action"), error=f"{type(exc).__name__}: {exc}")
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {short_text(exc, 300)}", "status": self.status_payload()}, status=400)

    def voice_dependencies_payload(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        modules = {
            "discord.py": "discord",
            "PyNaCl": "nacl",
            "davey": "davey",
            "yt-dlp": "yt_dlp",
            "wavelink": "wavelink",
            "aiohttp": "aiohttp",
            "gTTS": "gtts",
            "edge-tts": "edge_tts",
        }
        optional_modules: set[str] = set()
        for label, module in modules.items():
            try:
                importlib.import_module(module)
                checks[label] = {"ok": True, "optional": label in optional_modules}
            except Exception as exc:
                checks[label] = {"ok": False, "optional": label in optional_modules, "error": f"{type(exc).__name__}: {short_text(exc, 120)}"}
        for binary in ("ffmpeg", "ffprobe"):
            path = shutil.which(binary)
            checks[binary] = {"ok": bool(path), "path": path or ""}
        missing = [name for name, info in checks.items() if not bool(info.get("ok"))]
        missing_critical = [name for name in missing if not bool(checks.get(name, {}).get("optional"))]
        optional_missing = [name for name in missing if bool(checks.get(name, {}).get("optional"))]
        return {"ok": not missing_critical, "missing": missing_critical, "optional_missing": optional_missing, "checks": checks}

    def status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "available": bool(self.client.is_ready()),
            "version": AGENT_VERSION,
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "discord_ready": bool(self.client.is_ready()),
            "user": str(self.client.user) if self.client.user else "",
            "lavalink_uri": self.lavalink_uri,
            "lavalink_node": self.lavalink_node_name,
            "pool_connected": self._pool_connected,
            "direct_audio_enabled": self.direct_audio_enabled,
            "idle_disconnect_seconds": self.idle_disconnect_seconds,
            "cache": {
                "metadata_entries": len(self._metadata_cache),
                "stream_entries": len(self._resolve_cache),
                "metadata_ttl_seconds": self.metadata_cache_ttl,
                "stream_ttl_seconds": self.stream_cache_ttl,
            },
            "voice_dependencies": self.voice_dependencies_payload(),
            "guilds": {str(gid): state.public() for gid, state in self.states.items()},
        }

    async def ensure_lavalink_pool(self) -> None:
        if self._pool_connected:
            return
        if not self.lavalink_uri or not self.lavalink_password:
            raise RuntimeError("Lavalink do worker não configurado para o Music Agent")
        self.log("lavalink_pool_connecting", uri=self.lavalink_uri, node=self.lavalink_node_name)
        node = wavelink.Node(uri=self.lavalink_uri, password=self.lavalink_password, identifier=self.lavalink_node_name)
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.client, cache_capacity=100)
        except TypeError:
            await wavelink.Pool.connect(nodes=[node], client=self.client)
        except Exception:
            self._pool_connected = False
            raise
        self._pool_connected = True
        self.log("lavalink_pool_ready", node=self.lavalink_node_name)

    async def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_")
        if action in {"status", "get_state"}:
            return self.status_payload()
        if action in {"play", "enqueue", "play_direct", "enqueue_many", "queue_many", "add_many", "playlist"}:
            body = dict(body)
            body["_agent_action"] = action
            return await self.cmd_play(body)
        if action == "pause":
            return await self.cmd_pause(body)
        if action in {"resume", "unpause"}:
            return await self.cmd_resume(body)
        if action in {"stop", "disconnect"}:
            return await self.cmd_stop(body)
        if action in {"skip", "next"}:
            return await self.cmd_skip(body)
        if action in {"previous", "back", "prev", "anterior", "voltar"}:
            return await self.cmd_previous(body)
        if action == "volume":
            return await self.cmd_volume(body)
        if action in {"shuffle", "shuffle_queue", "mix_queue"}:
            return await self.cmd_shuffle(body)
        if action in {"loop", "repeat", "cycle_loop", "repeat_mode"}:
            return await self.cmd_loop(body)
        if action in {"seek", "set_position", "select_moment"}:
            return await self.cmd_seek(body)
        if action in {"duck", "duck_volume", "tts_duck"}:
            return await self.cmd_duck(body)
        if action in {"unduck", "restore_volume", "tts_restore"}:
            return await self.cmd_unduck(body)
        if action in {"voice_tts", "voice_tts_direct", "direct_tts", "tts_direct"}:
            return await self._run_tts_request(body, direct=True)
        if action in {"tts", "tts_play", "speak"}:
            return await self._run_tts_request(body, direct=False)
        if action == "cancel_tts":
            return await self.cmd_cancel_tts(body)
        if action in {"prefetch", "prepare", "preload"}:
            return await self.cmd_prefetch(body)
        raise ValueError("ação do Music Agent não suportada")

    def _cancel_prefetch_tasks(self, guild_id: int | None = None) -> int:
        cancelled = 0
        prefix = f"{int(guild_id)}:" if guild_id else ""
        for key, task in list(self._prefetch_tasks.items()):
            if prefix and not str(key).startswith(prefix):
                continue
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
            self._prefetch_tasks.pop(key, None)
        if cancelled:
            self.log("prefetch_cancelled", guild_id=int(guild_id or 0), count=cancelled)
        return cancelled

    def _bump_playback_generation(self, st: GuildMusicState, *, reason: str = "change") -> int:
        st.playback_token += 1
        st.updated_at = time.time()
        self._cancel_prefetch_tasks(st.guild_id)
        self.log("playback_generation_bumped", guild_id=st.guild_id, reason=reason, token=st.playback_token)
        return st.playback_token

    def _guild_prefetch_key(self, guild_id: int, cache_key: str) -> str:
        return f"{int(guild_id or 0)}:{cache_key}"

    def _schedule_next_queue_prefetch(self, guild_id: int, *, reason: str = "playing") -> None:
        if not self.prefetch_enabled:
            return
        st = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
        if not st.queue:
            return
        meta = st.queue[0].public()
        query = self._query_from_track_meta(meta, fallback_query=st.queue[0].query or st.queue[0].webpage_url or st.queue[0].title)
        if not query:
            return
        cache_key = self._resolve_cache_key(query, meta)
        task_key = self._guild_prefetch_key(guild_id, cache_key)
        if self._resolve_cache_get(cache_key):
            return
        current_task = self._prefetch_tasks.get(task_key)
        if current_task is not None and not current_task.done():
            return
        token = int(getattr(st, "playback_token", 0) or 0)
        delay = 3.0
        current = st.current
        try:
            if current is not None and current.duration and st.started_monotonic:
                elapsed = max(0.0, time.monotonic() - float(st.started_monotonic))
                remaining = max(0.0, float(current.duration) - elapsed)
                delay = max(2.0, remaining - env_float("MUSIC_AGENT_PREFETCH_BEFORE_END_SECONDS", 45.0))
        except Exception:
            delay = 3.0

        async def _runner() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                latest = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
                if int(getattr(latest, "playback_token", 0) or 0) != token or not latest.queue:
                    return
                current_first = latest.queue[0]
                current_key = self._resolve_cache_key(
                    self._query_from_track_meta(current_first.public(), fallback_query=current_first.query or current_first.webpage_url or current_first.title),
                    current_first.public(),
                )
                if current_key != cache_key:
                    return
                started = time.time()
                body = {
                    "guild_id": guild_id,
                    "voice_channel_id": latest.voice_channel_id,
                    "text_channel_id": latest.text_channel_id,
                    "requester_id": current_first.requester_id,
                    "requester_name": current_first.requester_name,
                    "query": query,
                    "track": current_first.public(),
                }
                resolved = await asyncio.wait_for(self.resolve_track(query, track_meta=current_first.public(), body=body), timeout=self.prefetch_timeout)
                latest2 = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
                if int(getattr(latest2, "playback_token", 0) or 0) == token and latest2.queue:
                    check_key = self._resolve_cache_key(
                        self._query_from_track_meta(latest2.queue[0].public(), fallback_query=latest2.queue[0].query or latest2.queue[0].webpage_url or latest2.queue[0].title),
                        latest2.queue[0].public(),
                    )
                    if check_key == cache_key:
                        latest2.queue[0] = resolved
                self.log("next_prefetch_ready", guild_id=guild_id, reason=reason, elapsed_ms=round((time.time() - started) * 1000.0, 1), title=getattr(resolved, "title", ""))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.log("next_prefetch_failed", guild_id=guild_id, reason=reason, error=short_text(exc, 180))
            finally:
                self._prefetch_tasks.pop(task_key, None)

        self._prefetch_tasks[task_key] = asyncio.create_task(_runner())
        self.log("next_prefetch_scheduled", guild_id=guild_id, delay=round(delay, 2), title=st.queue[0].title, reason=reason)

    async def cmd_prefetch(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.prefetch_enabled:
            return {"ok": True, "accepted": 0, "disabled": True}
        tracks = body.get("tracks") if isinstance(body.get("tracks"), list) else []
        if not tracks:
            single = body.get("track") if isinstance(body.get("track"), dict) else {}
            if single:
                tracks = [single]
        try:
            limit = max(0, min(3, int(float(body.get("limit") or len(tracks) or 0))))
        except Exception:
            limit = min(2, len(tracks))
        accepted = 0
        for meta in tracks[:limit]:
            if not isinstance(meta, dict):
                continue
            query = self._query_from_track_meta(meta, fallback_query=body.get("query") or "")
            if not query:
                continue
            cache_key = self._resolve_cache_key(query, meta)
            task_key = self._guild_prefetch_key(safe_id(body.get("guild_id")), cache_key)
            if self._resolve_cache_get(cache_key):
                continue
            if task_key in self._prefetch_tasks and not self._prefetch_tasks[task_key].done():
                continue
            child = dict(body)
            child["track"] = dict(meta)
            task = asyncio.create_task(self._prefetch_track(child, dict(meta), query, cache_key))
            self._prefetch_tasks[task_key] = task
            accepted += 1
        return {"ok": True, "accepted": accepted, "cache_size": len(self._resolve_cache), "metadata_cache_size": len(self._metadata_cache)}

    async def cmd_play(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        voice_channel_id = safe_id(body.get("voice_channel_id"))
        text_channel_id = safe_id(body.get("text_channel_id"))
        if not guild_id or not voice_channel_id:
            raise ValueError("guild_id e voice_channel_id são obrigatórios")
        action = str(body.get("_agent_action") or body.get("action") or body.get("command") or "play").strip().lower().replace("-", "_")
        query = str(body.get("query") or body.get("url") or body.get("webpage_url") or body.get("original_url") or "").strip()
        track_meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        tracks_payload = body.get("tracks") if isinstance(body.get("tracks"), list) else []
        if not query and tracks_payload and isinstance(tracks_payload[0], dict):
            query = self._query_from_track_meta(tracks_payload[0], fallback_query="")
        if not query:
            query = self._query_from_track_meta(track_meta, fallback_query="")
        if not query and not tracks_payload:
            raise ValueError("query/url vazia")
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.voice_channel_id = voice_channel_id
        st.text_channel_id = text_channel_id
        if not st.normal_volume_percent:
            st.normal_volume_percent = self.default_volume_percent
        if not st.volume_percent:
            st.volume_percent = st.normal_volume_percent
        st.last_action = "play"
        self._cancel_idle_disconnect(guild_id)
        command_generation = int(getattr(st, "playback_token", 0) or 0)
        self.log("play_received", guild_id=guild_id, voice=voice_channel_id, query=query, action=action, tracks=len(tracks_payload), generation=command_generation)

        if tracks_payload:
            tracks: list[AgentTrack] = []
            for item in tracks_payload:
                if not isinstance(item, dict):
                    continue
                track = self._agent_track_from_metadata(item, body=body, fallback_query=query)
                if track.query or track.stream_url or track.webpage_url:
                    tracks.append(track)
            if not tracks:
                raise ValueError("playlist/fila sem faixas válidas")
            active = bool(st.current and st.status in {"playing", "starting", "preparing", "paused"})
            st.queue.extend(tracks)
            st.updated_at = time.time()
            self.log("queued_many", guild_id=guild_id, added=len(tracks), queue_size=len(st.queue), active=active)
            if not active:
                await self._play_next(guild_id)
                if st.status in {"failed", "error"}:
                    return {"ok": False, "queued": False, "added": len(tracks), "error": st.last_error or "falha ao iniciar playback", "state": st.public()}
                return {"ok": True, "queued": False, "added": len(tracks), "state": st.public()}
            self._schedule_next_queue_prefetch(guild_id, reason="enqueue_many")
            return {"ok": True, "queued": True, "added": len(tracks), "state": st.public()}

        query = self._query_from_track_meta(track_meta, fallback_query=query) or query
        track = await self.resolve_track(query, track_meta=track_meta, body=body)
        if int(getattr(st, "playback_token", 0) or 0) != command_generation or str(getattr(st, "last_action", "") or "").lower() == "stop":
            self.log("play_resolve_ignored", guild_id=guild_id, reason="stale_or_stopped", generation=command_generation, current_generation=getattr(st, "playback_token", 0), title=getattr(track, "title", ""))
            return {"ok": False, "cancelled": True, "queued": False, "error": "operação cancelada", "state": st.public()}
        if st.current and st.status in {"playing", "starting", "preparing", "paused"}:
            st.queue.append(track)
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="enqueue")
            self.log("queued", guild_id=guild_id, title=track.title, queue_size=len(st.queue))
            return {"ok": True, "queued": True, "track": track.public(), "state": st.public()}
        st.queue.append(track)
        await self._play_next(guild_id)
        if st.status in {"failed", "error"}:
            return {"ok": False, "queued": False, "error": st.last_error or "falha ao iniciar playback", "state": st.public()}
        return {"ok": True, "queued": False, "state": st.public()}

    async def cmd_pause(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            if isinstance(player, getattr(wavelink, "Player", ())):
                await player.pause(True)
            elif hasattr(player, "pause"):
                player.pause()
            st.paused = True
            self._set_status(st, "paused", event="pause")
        return {"ok": True, "state": st.public()}

    async def cmd_resume(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.states.setdefault(safe_id(body.get("guild_id")), GuildMusicState(guild_id=safe_id(body.get("guild_id"))))
        player = st.player
        if player:
            if isinstance(player, getattr(wavelink, "Player", ())):
                await player.pause(False)
            elif hasattr(player, "resume"):
                player.resume()
            st.paused = False
            self._set_status(st, "playing", event="resume")
        return {"ok": True, "state": st.public()}

    def _track_key(self, track: AgentTrack | None) -> str:
        if track is None:
            return ""
        for value in (track.webpage_url, track.query, track.stream_url, track.title):
            raw = str(value or "").strip().lower()
            if raw:
                return raw[:300]
        return ""

    def _clone_track(self, track: AgentTrack | None) -> AgentTrack | None:
        if track is None:
            return None
        with contextlib.suppress(Exception):
            clone = replace(track)
            clone.start_offset_seconds = 0.0
            return clone
        return None

    def _push_history(self, st: GuildMusicState, track: AgentTrack | None) -> None:
        clone = self._clone_track(track)
        if clone is None:
            return
        key = self._track_key(clone)
        if key and st.history and self._track_key(st.history[-1]) == key:
            return
        st.history.append(clone)
        max_history = max(1, env_int("MUSIC_AGENT_HISTORY_MAXSIZE", 25))
        if len(st.history) > max_history:
            del st.history[:-max_history]

    def _track_from_command_payload(self, body: dict[str, Any], *, fallback_query: str = "") -> AgentTrack | None:
        meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        if not meta:
            return None
        track = self._agent_track_from_metadata(meta, body=body, fallback_query=fallback_query or self._query_from_track_meta(meta, fallback_query=""))
        track.start_offset_seconds = 0.0
        return track

    async def _stop_player_instance(self, player: Any, *, disconnect: bool = False) -> None:
        if not player:
            return
        with contextlib.suppress(Exception):
            if isinstance(player, getattr(wavelink, "Player", ())):
                await player.stop()
                if disconnect:
                    await player.disconnect()
            else:
                if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                    player.stop()
                if disconnect:
                    await player.disconnect(force=True)

    async def _stop_current_player_for_transition(self, st: GuildMusicState, *, disconnect: bool = False) -> None:
        player = st.player
        if disconnect:
            st.player = None
        await self._stop_player_instance(player, disconnect=disconnect)

    async def cmd_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "stop"
        self._cancel_prefetch_tasks(guild_id)
        self._cancel_idle_disconnect(guild_id)
        st.queue.clear()
        st.history.clear()
        player = st.player
        st.current = None
        self._set_status(st, "idle", event="stop")
        st.paused = False
        self._bump_playback_generation(st, reason="stop")
        await self._stop_player_instance(player, disconnect=True)
        return {"ok": True, "state": st.public()}

    async def cmd_skip(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "skip"
        self._cancel_prefetch_tasks(guild_id)
        player = st.player
        self._push_history(st, st.current)
        self._bump_playback_generation(st, reason="skip")
        await self._stop_player_instance(player, disconnect=False)
        st.current = None
        await self._play_next(guild_id)
        return {"ok": True, "state": st.public()}

    async def cmd_previous(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "previous"
        explicit = self._track_from_command_payload(body)
        previous = explicit
        if previous is None and st.history:
            previous = st.history.pop()
        elif explicit is not None:
            explicit_key = self._track_key(explicit)
            if explicit_key:
                # Remova a mesma faixa do topo do histórico remoto se ela estiver lá.
                for idx in range(len(st.history) - 1, -1, -1):
                    if self._track_key(st.history[idx]) == explicit_key:
                        del st.history[idx]
                        break
        if previous is None:
            return {"ok": False, "error": "sem música anterior no histórico", "state": st.public()}

        current = self._clone_track(st.current)
        if current is not None:
            st.queue.insert(0, current)
        previous.start_offset_seconds = 0.0
        st.queue.insert(0, previous)
        self._cancel_prefetch_tasks(guild_id)
        self._cancel_idle_disconnect(guild_id)
        player = st.player
        self._bump_playback_generation(st, reason="previous")
        await self._stop_player_instance(player, disconnect=False)
        st.current = None
        st.paused = False
        await self._play_next(guild_id, preserve_current_to_history=False)
        self.log("previous_started", guild_id=guild_id, title=getattr(previous, "title", ""), queue_size=len(st.queue), history_size=len(st.history))
        return {"ok": True, "previous": previous.public(), "state": st.public()}

    async def cmd_shuffle(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "shuffle"
        # Shuffle é uma ação única para embaralhar a fila atual, não um modo
        # persistente ligado/desligado. Não altere playback_token aqui: ele é
        # usado pelo callback do áudio atual; mudar esse token faria a faixa
        # atual terminar sem avançar a queue. Cancele apenas o prefetch antigo.
        self._cancel_prefetch_tasks(guild_id)
        if len(st.queue) > 1:
            import random as _random
            _random.shuffle(st.queue)
            st.shuffle = False
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="shuffle")
            self.log("queue_shuffled", guild_id=guild_id, queue_size=len(st.queue))
            return {"ok": True, "shuffled": True, "enabled": False, "queue_size": len(st.queue), "state": st.public()}
        st.shuffle = False
        st.updated_at = time.time()
        return {"ok": True, "shuffled": False, "enabled": False, "queue_size": len(st.queue), "state": st.public()}

    async def cmd_loop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested = str(body.get("mode") or body.get("loop_mode") or "").strip().lower()
        modes = ("off", "one", "all")
        if requested in modes:
            st.loop_mode = requested
        else:
            current = str(getattr(st, "loop_mode", "off") or "off").strip().lower()
            if current == "off":
                st.loop_mode = "one"
            elif current == "one":
                st.loop_mode = "all"
            else:
                st.loop_mode = "off"
        st.last_action = "loop"
        st.updated_at = time.time()
        self.log("loop_mode_changed", guild_id=guild_id, mode=st.loop_mode)
        return {"ok": True, "mode": st.loop_mode, "loop_mode": st.loop_mode, "state": st.public()}

    async def cmd_seek(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        raw = body.get("position_seconds")
        if raw is None:
            raw = body.get("seconds")
        if raw is None and body.get("position_ms") is not None:
            raw = float(body.get("position_ms") or 0) / 1000.0
        try:
            target = max(0.0, float(raw or 0.0))
        except Exception:
            return {"ok": False, "error": "tempo inválido", "state": self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id)).public()}
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        track = st.current
        if track is None:
            return {"ok": False, "error": "não há música tocando agora", "state": st.public()}
        if track.duration is not None:
            try:
                duration = float(track.duration)
                if duration > 0 and target > duration:
                    return {"ok": False, "error": f"momento passa da duração da música ({int(duration)}s)", "state": st.public()}
            except Exception:
                pass
        st.last_action = "seek"
        player = st.player
        if player is not None and isinstance(player, getattr(wavelink, "Player", ())) :
            position_ms = max(0, int(target * 1000))
            seeker = getattr(player, "seek", None)
            if callable(seeker):
                maybe = seeker(position_ms)
                if asyncio.iscoroutine(maybe):
                    await maybe
                st.started_monotonic = time.monotonic() - target
                self._set_status(st, "playing", event="seek")
                return {"ok": True, "position_seconds": target, "state": st.public()}
            return {"ok": False, "error": "backend atual não aceitou seek", "state": st.public()}
        if not track.stream_url:
            try:
                track = await self.resolve_track(track.webpage_url or track.query or track.title, track_meta=track.public(), body=body)
                st.current = track
            except Exception as exc:
                return {"ok": False, "error": f"não consegui preparar seek: {short_text(exc, 180)}", "state": st.public()}
        track.start_offset_seconds = target
        st.current = track
        st.paused = False
        st.playback_token += 1
        if player is not None:
            with contextlib.suppress(Exception):
                if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                    player.stop()
        try:
            await self._play_direct_voice(guild_id, track)
        except Exception as exc:
            self._set_status(st, "failed", event="seek_failed", error=short_text(exc, 260))
            return {"ok": False, "error": st.last_error or "seek falhou", "state": st.public()}
        self._set_status(st, "playing", event="seek")
        return {"ok": True, "position_seconds": target, "state": st.public()}

    async def _apply_player_volume(self, st: GuildMusicState, volume: int) -> bool:
        volume = max(0, min(1000, int(volume)))
        player = st.player
        if player is None:
            st.volume_percent = volume
            return False
        applied = False
        setter = getattr(player, "set_volume", None)
        if callable(setter):
            maybe = setter(volume)
            if asyncio.iscoroutine(maybe):
                await maybe
            applied = True
        # discord.VoiceClient exposes the active source; when it is a
        # PCMVolumeTransformer, changing source.volume is immediate.
        source = getattr(player, "source", None)
        if source is not None and hasattr(source, "set_music_volume"):
            with contextlib.suppress(Exception):
                source.set_music_volume(max(0.0, min(10.0, volume / 100.0)))
                applied = True
        elif source is not None and hasattr(source, "volume"):
            with contextlib.suppress(Exception):
                source.volume = max(0.0, min(10.0, volume / 100.0))
                applied = True
        st.volume_percent = volume
        st.updated_at = time.time()
        return applied

    async def cmd_volume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        volume = max(0, min(1000, int(float(body.get("volume") or body.get("volume_percent") or self.default_volume_percent))))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.normal_volume_percent = volume
        if not st.ducked:
            await self._apply_player_volume(st, volume)
        return {"ok": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "ducked": st.ducked, "state": st.public()}

    async def cmd_duck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested = body.get("volume") if body.get("volume") is not None else body.get("volume_percent")
        if requested is None:
            requested = self.duck_volume_percent
        duck_volume = max(0, min(100, int(float(requested))))
        if not st.normal_volume_percent:
            st.normal_volume_percent = max(0, min(150, int(st.volume_percent or self.default_volume_percent)))
        st.ducked = True
        await self._apply_player_volume(st, duck_volume)
        self.log("tts_duck", guild_id=guild_id, volume=duck_volume, normal=st.normal_volume_percent)
        return {"ok": True, "ducked": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    async def cmd_unduck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        restore = max(0, min(150, int(float(body.get("volume") or body.get("volume_percent") or st.normal_volume_percent or self.default_volume_percent))))
        st.ducked = False
        st.normal_volume_percent = restore
        await self._apply_player_volume(st, restore)
        self.log("tts_restore", guild_id=guild_id, volume=restore)
        return {"ok": True, "ducked": False, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    def _normalize_tts_language(self, value: Any) -> str:
        raw = str(value or "pt-br").strip().lower().replace("_", "-")
        if raw in {"pt", "ptbr", "pt-br", "br"}:
            return "pt-br"
        if raw in {"en", "en-us", "us"}:
            return "en"
        return raw or "pt-br"

    def _normalize_edge_rate(self, value: Any) -> str:
        raw = str(value or "+0%").strip() or "+0%"
        if re.match(r"^[+-]?\d+%$", raw):
            return raw if raw.startswith(("+", "-")) else "+" + raw
        return "+0%"

    def _normalize_edge_pitch(self, value: Any) -> str:
        raw = str(value or "+0Hz").strip() or "+0Hz"
        if re.match(r"^[+-]?\d+Hz$", raw, re.I):
            return raw if raw.startswith(("+", "-")) else "+" + raw
        return "+0Hz"



    def _tts_cache_enabled(self) -> bool:
        return truthy(os.getenv("MUSIC_AGENT_TTS_CACHE_ENABLED"), truthy(os.getenv("PHONE_WORKER_TTS_AGENT_CACHE_ENABLED"), True))

    def _tts_cache_root(self) -> Path:
        configured = str(os.getenv("MUSIC_AGENT_TTS_CACHE_DIR") or os.getenv("PHONE_WORKER_TTS_CACHE_DIR") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "tts"

    def _tts_cache_limits(self) -> tuple[int, int]:
        max_mb = max(16, min(32768, env_int("MUSIC_AGENT_TTS_CACHE_MAX_MB", env_int("PHONE_WORKER_TTS_CACHE_MAX_MB", 4096))))
        max_files = max(64, min(100000, env_int("MUSIC_AGENT_TTS_CACHE_MAX_FILES", env_int("PHONE_WORKER_TTS_CACHE_MAX_FILES", 20000))))
        return max_mb * 1024 * 1024, max_files

    def _sanitize_tts_cache_key(self, raw: Any) -> str:
        key = re.sub(r"[^a-z0-9_\-]", "", str(raw or "").strip().lower())
        if len(key) < 16:
            raise ValueError("cache_key curta")
        return key[:96]

    def _tts_cache_path(self, key: str, audio_format: str) -> Path:
        fmt = str(audio_format or "mp3").strip().lower().replace(".", "")
        if fmt in {"wave", "wav"}:
            fmt = "wav"
        elif fmt in {"ogg", "opus"}:
            fmt = "ogg"
        else:
            fmt = "mp3"
        return self._tts_cache_root() / f"{key}.{fmt}"

    def _find_tts_cache_file(self, key: str) -> tuple[Path | None, str]:
        root = self._tts_cache_root()
        for fmt in ("mp3", "wav", "ogg"):
            path = root / f"{key}.{fmt}"
            try:
                if path.exists() and path.stat().st_size > 0:
                    return path, fmt
            except Exception:
                continue
        return None, ""

    def _touch_tts_cache_file(self, path: Path) -> None:
        now = time.monotonic()
        key = str(path)
        with _TTS_MAINTENANCE_LOCK:
            if now - _TTS_CACHE_TOUCHES.get(key, -60) < 30:
                return
            if len(_TTS_CACHE_TOUCHES) >= 4096:
                _TTS_CACHE_TOUCHES.pop(next(iter(_TTS_CACHE_TOUCHES)))
            _TTS_CACHE_TOUCHES[key] = now
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def _prune_tts_cache(self, *, protected: Path | None = None) -> None:
        import fcntl
        root = self._tts_cache_root()
        max_bytes, max_files = self._tts_cache_limits()
        stats = []
        with contextlib.suppress(OSError):
            with os.scandir(root) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {'.mp3', '.wav', '.ogg'} or not entry.is_file(follow_symlinks=False):
                        continue
                    with contextlib.suppress(OSError):
                        st = entry.stat(follow_symlinks=False)
                        stats.append((st.st_mtime, st.st_size, entry.path))
        count, total = len(stats), sum(row[1] for row in stats)
        if count <= max_files and total <= max_bytes:
            return
        fresh = time.time() - 180
        for mtime, size, path in sorted(stats):
            if count <= max_files and total <= max_bytes:
                break
            if mtime > fresh or protected is not None and path == str(protected):
                continue
            try:
                with open(path, 'rb') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    before, current = os.fstat(handle.fileno()), os.stat(path)
                    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                        continue
                    os.unlink(path)
                    count -= 1
                    total -= size
            except OSError:
                continue

    def _tts_cache_key_for_body(self, body: dict[str, Any], *, engine: str, text: str) -> str:
        requested_engine = str(body.get("engine") or engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        aliases = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "edge_tts": "edge"}
        requested_engine = aliases.get(requested_engine, requested_engine)
        normalized_engine = aliases.get(str(engine or requested_engine).strip().lower().replace("-", "_"), str(engine or requested_engine).strip().lower().replace("-", "_"))
        provided = str(body.get("cache_key") or "").strip()
        if provided and requested_engine == normalized_engine:
            with contextlib.suppress(Exception):
                return self._sanitize_tts_cache_key(provided)
        normalized_text = str(text or "").strip()
        if engine == "edge":
            voice = str(body.get("voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
            payload = f"edge|{voice}|{self._normalize_edge_rate(body.get('rate'))}|{self._normalize_edge_pitch(body.get('pitch'))}|{normalized_text}"
        else:
            language = self._normalize_tts_language(body.get("language"))
            payload = f"gtts|{language}|{body.get('tld') or 'com'}|{normalized_text}"
        return hashlib.sha256(("tts-v2|" + payload).encode("utf-8")).hexdigest()

    def _tts_cache_mode_allows_read(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "refresh"}

    def _tts_cache_mode_allows_store(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "no_store"}

    def _try_read_tts_cache_to_target(self, *, key: str, target: Path, body: dict[str, Any]) -> bool:
        path, audio_format = self._find_tts_cache_file(key)
        if path is None:
            return False
        data = path.read_bytes()
        if not data:
            return False
        max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
        if len(data) > max_bytes:
            return False
        target.write_bytes(data)
        body["audio_format"] = audio_format
        body["tts_cache_hit"] = True
        self._touch_tts_cache_file(path)
        self.log("tts_cache_hit", engine=str(body.get("engine") or ""), file=path.name, bytes=len(data))
        return True

    def _store_tts_cache_bytes(self, *, key: str, data: bytes, audio_format: str, engine: str) -> None:
        if not key or not data:
            return
        path = self._tts_cache_path(key, audio_format)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._touch_tts_cache_file(path)
            _schedule_tts_prune(self._prune_tts_cache, path)
            self.log("tts_cache_store", engine=engine, file=path.name, bytes=len(data))
        except Exception as exc:
            self.log("tts_cache_store_failed", engine=engine, error=f"{type(exc).__name__}: {short_text(exc, 120)}")
        finally:
            with contextlib.suppress(Exception):
                tmp.unlink()

    async def _synthesize_tts_file(self, body: dict[str, Any], target: Path) -> str:
        text = short_text(body.get("text") or body.get("content") or "", 1600)
        if not text:
            raise ValueError("texto TTS vazio")
        engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_")
        if engine in {"google", "google_tts", "googlecloud", "google_cloud", "gcloud"}:
            engine = "gtts"
        cache_key = ""
        if self._tts_cache_enabled():
            with contextlib.suppress(Exception):
                cache_key = self._tts_cache_key_for_body(body, engine=engine, text=text)
            if cache_key and self._tts_cache_mode_allows_read(body) and self._try_read_tts_cache_to_target(key=cache_key, target=target, body=body):
                return f"{engine}-cache"
        audio_format = "mp3"
        try:
            transport = importlib.import_module('tts_transport')
        except ImportError:
            transport = None
        if transport is not None:
            stream = transport.AudioStream(engine=engine, text=text,
                voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
                language=self._normalize_tts_language(body.get('language')),
                rate=self._normalize_edge_rate(body.get('rate')),
                pitch=self._normalize_edge_pitch(body.get('pitch')),
                tld=str(body.get('tld') or 'com'), timeout=float(body.get('timeout_seconds') or 30))
            try:
                data = await asyncio.to_thread(lambda: b''.join(stream))
            finally:
                stream.close()
        elif engine == 'edge':
            import edge_tts
            communicate = edge_tts.Communicate(text=text, voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
                rate=self._normalize_edge_rate(body.get('rate')), pitch=self._normalize_edge_pitch(body.get('pitch')),
                connect_timeout=4, receive_timeout=15)
            chunks = []
            async for chunk in communicate.stream():
                if chunk.get('type') == 'audio' and chunk.get('data'):
                    chunks.append(chunk['data'])
            data = b''.join(chunks)
        else:
            from gtts import gTTS
            def synthesize():
                buffer = io.BytesIO()
                gTTS(text=text, lang=self._normalize_tts_language(body.get('language')), timeout=(3.5, 8)).write_to_fp(buffer)
                return buffer.getvalue()
            data = await asyncio.to_thread(synthesize)
        if not data:
            raise RuntimeError("TTS não gerou áudio")
        target.write_bytes(data)
        body.setdefault("audio_format", audio_format)
        if cache_key and self._tts_cache_enabled() and self._tts_cache_mode_allows_store(body):
            self._store_tts_cache_bytes(key=cache_key, data=data, audio_format=str(body.get("audio_format") or audio_format), engine=engine)
        return engine

    def _tts_requests(self):
        if not hasattr(self, '_active_tts_requests'):
            self._active_tts_requests = {}
            self._cancelled_tts_requests = {}
        return self._active_tts_requests

    async def _run_tts_request(self, body, *, direct):
        active = self._tts_requests()
        now = time.monotonic()
        self._cancelled_tts_requests = {key: when for key, when in self._cancelled_tts_requests.items() if now - when < 60}
        request = str(body.get('tts_request_id') or '')[:80]
        key = (safe_id(body.get('guild_id')), request)
        if request and key in self._cancelled_tts_requests:
            return {'ok': False, 'cancelled': True, 'error': 'TTS cancelado antes da admissão'}
        if request and key in active:
            return {'ok': False, 'error': 'pedido TTS já está ativo'}
        owner = asyncio.current_task()
        if request:
            active[key] = owner
        try:
            return await (self.cmd_voice_tts(body) if direct else self.cmd_tts(body))
        finally:
            if active.get(key) is owner:
                active.pop(key, None)

    async def cmd_cancel_tts(self, body):
        active = self._tts_requests()
        request = str(body.get('tts_request_id') or '')[:80]
        if not request:
            return {'ok': False, 'error': 'tts_request_id obrigatório'}
        key = (safe_id(body.get('guild_id')), request)
        if len(self._cancelled_tts_requests) >= 256:
            self._cancelled_tts_requests.pop(next(iter(self._cancelled_tts_requests)))
        self._cancelled_tts_requests[key] = time.monotonic()
        task = active.get(key)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {'ok': True, 'cancelled': True, 'tts_request_id': request}

    async def _prepare_tts_source(self, body, target, *, started):
        text = str(body.get('text') or body.get('content') or '').strip()
        if not text or len(text) > 1600:
            raise ValueError('tamanho de texto TTS inválido')
        engine = str(body.get('engine') or 'gtts').lower().replace('-', '_')
        if engine in {'google', 'google_tts', 'googlecloud', 'google_cloud', 'gcloud'}:
            engine = 'gtts'
        key = self._tts_cache_key_for_body(body, engine=engine, text=text) if self._tts_cache_enabled() else ''
        cache_hit = key and self._tts_cache_mode_allows_read(body) and await asyncio.to_thread(
            self._try_read_tts_cache_to_target, key=key, target=target, body=body)
        if cache_hit:
            source = discord.FFmpegPCMAudio(str(target), executable=self.ffmpeg_executable,
                before_options='-nostdin', options='-vn -sn -dn -loglevel warning')
            return _TimedTTSSource(source, started=started), f'{engine}-cache'
        try:
            transport = importlib.import_module('tts_transport')
        except ImportError:
            await self._synthesize_tts_file(body, target)
            source = discord.FFmpegPCMAudio(str(target), executable=self.ffmpeg_executable,
                before_options='-nostdin', options='-vn -sn -dn -loglevel warning')
            return _TimedTTSSource(source, started=started), engine
        stream = transport.AudioStream(engine=engine, text=text,
            voice=str(body.get('voice') or 'pt-BR-FranciscaNeural'),
            language=self._normalize_tts_language(body.get('language')),
            rate=self._normalize_edge_rate(body.get('rate')), pitch=self._normalize_edge_pitch(body.get('pitch')),
            tld=str(body.get('tld') or 'com'), timeout=float(body.get('timeout_seconds') or 30))
        publish = None
        if key and self._tts_cache_mode_allows_store(body):
            publish = lambda data: self._store_tts_cache_bytes(key=key, data=data, audio_format='mp3', engine=engine)
        reader = transport.AudioReader(stream, on_complete=publish)
        timed = None
        try:
            source = discord.FFmpegPCMAudio(reader, pipe=True, executable=self.ffmpeg_executable,
                before_options='-nostdin -f mp3 -probesize 32768 -analyzeduration 0',
                options='-vn -sn -dn -loglevel warning')
            timed = _TimedTTSSource(source, started=started, reader=reader, buffered=True)
            ready = await asyncio.to_thread(timed.ready.wait, min(20, float(body.get('timeout_seconds') or 30)))
            if not ready or timed.error:
                raise RuntimeError('TTS não preparou o primeiro PCM') from timed.error
            return timed, engine
        except BaseException:
            reader.close()
            if timed is not None:
                timed.cleanup()
            raise

    async def cmd_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        player = st.player
        if not guild_id or player is None or st.current is None:
            return {"ok": False, "error": "sem sessão musical ativa no worker", "state": st.public()}
        source = getattr(player, "source", None)
        if not isinstance(source, AgentMixedAudioSource):
            return {"ok": False, "error": "sessão atual não suporta TTS no worker sem interromper música", "state": st.public()}
        timeout = max(1.0, min(90.0, float(body.get("timeout_seconds") or 30.0)))
        started = time.monotonic()
        engine = "worker"
        tts_source = None
        future = None
        st.ducked = True
        st.updated_at = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="music-agent-tts-") as tmp:
                path = Path(tmp) / "tts.mp3"
                audio_url = str(body.get("audio_url") or body.get("url") or "").strip()
                audio_b64 = str(body.get("audio_b64") or body.get("audioBase64") or "").strip()
                tts_input = ""
                if audio_url.startswith(("http://", "https://", "file://")):
                    tts_input = audio_url
                    engine = str(body.get("engine") or "prebuilt-url").strip() or "prebuilt-url"
                elif audio_b64:
                    try:
                        raw = base64.b64decode(audio_b64.encode("ascii"), validate=True)
                    except Exception as exc:
                        raise ValueError(f"audio_b64 inválido: {type(exc).__name__}") from exc
                    max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
                    if len(raw) > max_bytes:
                        raise ValueError("áudio TTS grande demais para o Music Agent")
                    path.write_bytes(raw)
                    tts_input = str(path)
                    engine = str(body.get("engine") or "prebuilt-b64").strip() or "prebuilt-b64"
                else:
                    tts_source, engine = await self._prepare_tts_source(body, path, started=started)
                if tts_source is None:
                    tts_source = _TimedTTSSource(discord.FFmpegPCMAudio(tts_input, executable=self.ffmpeg_executable,
                        before_options="-nostdin", options="-vn -sn -dn -loglevel warning"), started=started)
                future = source.add_tts(tts_source, volume=max(0.0, min(2.0, env_float("MUSIC_AGENT_TTS_VOLUME", 1.0))))
                self.log("tts_overlay_start", guild_id=guild_id, engine=engine, chars=len(str(body.get("text") or "")), prebuilt=bool(audio_url or audio_b64))
                await asyncio.wait_for(future, timeout=timeout)
        finally:
            if future is not None:
                source.cancel_tts(future)
            if tts_source is not None:
                tts_source.cleanup()
            st.ducked = False
            st.updated_at = time.time()
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        self.log("tts_overlay_done", guild_id=guild_id, elapsed_ms=round(elapsed_ms, 1))
        return {"ok": True, "engine": engine, "playback_ms": round(elapsed_ms, 1), "first_frame_observed": tts_source.first_frame_ms is not None, "first_frame_ms": tts_source.first_frame_ms, "state": st.public()}

    def _get_tts_direct_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._tts_direct_locks.get(int(guild_id or 0))
        if lock is None:
            lock = asyncio.Lock()
            self._tts_direct_locks[int(guild_id or 0)] = lock
        return lock

    async def cmd_voice_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        """Play a short TTS directly from the worker-owned Discord voice plane.

        This is intentionally only a voice/audio-plane command. The VPS still owns
        commands, permissions, panels and DB state. The Music Agent uses its
        existing Discord voice client solely to connect/play audio in the target
        voice channel.
        """
        guild_id = safe_id(body.get("guild_id"))
        voice_channel_id = safe_id(body.get("voice_channel_id") or body.get("channel_id"))
        if guild_id <= 0 or voice_channel_id <= 0:
            raise ValueError("guild_id/voice_channel_id obrigatórios para TTS direto")
        timeout = max(3.0, min(90.0, float(body.get("timeout_seconds") or 30.0)))
        started = time.monotonic()
        lock = self._get_tts_direct_lock(guild_id)
        async with lock:
            st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
            st.voice_channel_id = voice_channel_id
            st.text_channel_id = safe_id(body.get("text_channel_id") or st.text_channel_id)
            # If worker music is currently playing through the mixed source, reuse
            # the overlay path so TTS and music do not fight over the same voice connection.
            player = st.player
            source = getattr(player, "source", None)
            if st.current is not None and isinstance(source, AgentMixedAudioSource):
                return await self.cmd_tts(body)
            if st.current is not None:
                return {"ok": False, "error": "música ativa sem mixer TTS direto; evitando interromper player", "state": st.public()}

            guild, channel = await self._resolve_guild_and_channel(guild_id, voice_channel_id)
            existing = guild.voice_client
            player_cls = getattr(wavelink, "Player", None)
            if existing is not None and player_cls is not None and isinstance(existing, player_cls):
                # Wavelink idle/old player should not block a standalone short TTS.
                with contextlib.suppress(Exception):
                    await existing.disconnect(force=True)
                existing = None
            if existing is None or not getattr(existing, "is_connected", lambda: False)():
                self.log("voice_direct_tts_connecting", guild_id=guild_id, channel=voice_channel_id)
                voice_client = await channel.connect(self_deaf=True)
            else:
                voice_client = existing
                if getattr(getattr(voice_client, "channel", None), "id", None) != voice_channel_id:
                    await voice_client.move_to(channel)
            st.player = voice_client
            st.transport = "worker_voice_direct_tts"
            st.status = "tts_direct"
            st.updated_at = time.time()

            if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
                with contextlib.suppress(Exception):
                    voice_client.stop()
                await asyncio.sleep(0.15)

            loop = asyncio.get_running_loop()
            finished = loop.create_future()
            def _after(error: Exception | None) -> None:
                def complete():
                    if not finished.done():
                        finished.set_exception(error) if error else finished.set_result(None)
                loop.call_soon_threadsafe(complete)

            engine = "worker"
            with tempfile.TemporaryDirectory(prefix="music-agent-direct-tts-") as tmp:
                def _audio_suffix_from_format(value: Any) -> str:
                    fmt = str(value or "").strip().lower().replace(".", "").replace("-", "_")
                    if fmt in {"ogg", "opus", "ogg_opus"}:
                        return ".ogg"
                    if fmt in {"wav", "wave", "linear16", "pcm"}:
                        return ".wav"
                    if fmt == "m4a":
                        return ".m4a"
                    return ".mp3"

                def _build_tts_audio_source(tts_input_path: str, *, audio_format: str = "") -> Any:
                    suffix = _audio_suffix_from_format(audio_format or Path(str(tts_input_path)).suffix)
                    opus_cls = getattr(discord, "FFmpegOpusAudio", None)
                    if suffix == ".ogg" and opus_cls is not None:
                        try:
                            return opus_cls(
                                tts_input_path,
                                executable=self.ffmpeg_executable,
                                before_options="-nostdin",
                                options="-vn -sn -dn -loglevel warning",
                                codec="copy",
                            )
                        except TypeError:
                            pass
                        except Exception as exc:
                            self.log("voice_direct_tts_opus_copy_fallback", error=short_text(exc, 180))
                        try:
                            return opus_cls(
                                tts_input_path,
                                executable=self.ffmpeg_executable,
                                before_options="-nostdin",
                                options="-vn -sn -dn -loglevel warning",
                            )
                        except Exception as exc:
                            self.log("voice_direct_tts_opus_source_fallback", error=short_text(exc, 180))
                    return discord.FFmpegPCMAudio(tts_input_path, executable=self.ffmpeg_executable, before_options="-nostdin", options="-vn -sn -dn -loglevel warning")

                audio_format = str(body.get("audio_format") or body.get("format") or "").strip().lower()
                path = Path(tmp) / f"tts{_audio_suffix_from_format(audio_format)}"
                audio_url = str(body.get("audio_url") or body.get("url") or "").strip()
                audio_b64 = str(body.get("audio_b64") or body.get("audioBase64") or "").strip()
                tts_input = ""
                audio_source = None
                if audio_url.startswith(("http://", "https://", "file://")):
                    tts_input = audio_url
                    engine = str(body.get("engine") or "prebuilt-url").strip() or "prebuilt-url"
                elif audio_b64:
                    raw = base64.b64decode(audio_b64.encode("ascii"), validate=True)
                    max_bytes = max(1024, int(env_float("MUSIC_AGENT_TTS_MAX_B64_BYTES", 8 * 1024 * 1024)))
                    if len(raw) > max_bytes:
                        raise ValueError("áudio TTS grande demais para o Music Agent")
                    path.write_bytes(raw)
                    tts_input = str(path)
                    engine = str(body.get("engine") or "prebuilt-b64").strip() or "prebuilt-b64"
                else:
                    path = Path(tmp) / "tts.mp3"
                    audio_source, engine = await self._prepare_tts_source(body, path, started=started)
                    audio_format = "mp3"
                if audio_source is None:
                    audio_source = _TimedTTSSource(_build_tts_audio_source(tts_input, audio_format=audio_format), started=started)
                play_started = time.monotonic()
                try:
                    voice_client.play(audio_source, after=_after)
                    self.log("voice_direct_tts_start", guild_id=guild_id, engine=engine, channel=voice_channel_id, chars=len(str(body.get("text") or "")))
                    await asyncio.wait_for(finished, timeout=timeout)
                except BaseException:
                    with contextlib.suppress(Exception):
                        if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
                            voice_client.stop()
                    raise
                finally:
                    with contextlib.suppress(Exception):
                        audio_source.cleanup()
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            playback_ms = max(0.0, (time.monotonic() - play_started) * 1000.0)
            st.status = "idle"
            st.updated_at = time.time()
            self._schedule_idle_disconnect(guild_id)
            self.log("voice_direct_tts_done", guild_id=guild_id, engine=engine, elapsed_ms=round(elapsed_ms, 1))
            return {
                "ok": True,
                "engine": engine,
                "audio_format": audio_format or "mp3",
                "direct_tts": True,
                "voice_connected": bool(getattr(voice_client, "is_connected", lambda: False)()),
                "playback_ms": round(playback_ms, 1),
                "elapsed_ms": round(elapsed_ms, 1),
                "first_frame_observed": audio_source.first_frame_ms is not None,
                "first_frame_ms": audio_source.first_frame_ms,
                "state": st.public(),
            }

    async def _play_next(self, guild_id: int, *, preserve_current_to_history: bool = True) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        self._cancel_idle_disconnect(guild_id)
        if not st.queue:
            if st.current is not None:
                self._push_history(st, st.current)
            st.current = None
            st.paused = False
            self._set_status(st, "idle", event="queue_empty")
            self._schedule_idle_disconnect(guild_id)
            return
        next_track = st.queue.pop(0)
        if preserve_current_to_history and st.current is not None:
            # A faixa atual precisa virar histórico sempre que deixa de ser a
            # faixa ativa por troca direta, fallback, playlist ou transição
            # remota. O comando previous trata esse caso separadamente porque
            # recoloca a faixa atual na frente da fila.
            if self._track_key(st.current) != self._track_key(next_track):
                self._push_history(st, st.current)
        st.current = next_track
        request_token = int(getattr(st, "playback_token", 0) or 0)
        current_ref = st.current
        st.paused = False
        st.transport = ""
        st.ducked = False
        st.normal_volume_percent = max(0, min(150, int(st.normal_volume_percent or self.default_volume_percent)))
        st.volume_percent = st.normal_volume_percent
        self._set_status(st, "preparing", event="play_preparing")
        self.log("track_loading", guild_id=guild_id, title=getattr(st.current, "title", ""), source=getattr(st.current, "source", ""), lazy=not bool(getattr(st.current, "stream_url", "")))
        try:
            if st.current and not st.current.stream_url:
                started = time.time()
                meta = st.current.public()
                query = self._query_from_track_meta(meta, fallback_query=st.current.query or st.current.title)
                body = {
                    "guild_id": guild_id,
                    "voice_channel_id": st.voice_channel_id,
                    "text_channel_id": st.text_channel_id,
                    "requester_id": st.current.requester_id,
                    "requester_name": st.current.requester_name,
                    "query": query,
                    "track": meta,
                }
                resolved_current = await self.resolve_track(query, track_meta=meta, body=body)
                if int(getattr(st, "playback_token", 0) or 0) != request_token or st.current is not current_ref:
                    self.log("lazy_resolve_ignored", guild_id=guild_id, title=getattr(resolved_current, "title", ""), reason="stale_generation")
                    return
                st.current = resolved_current
                current_ref = st.current
                self.log("lazy_resolve_done", guild_id=guild_id, elapsed_ms=round((time.time() - started) * 1000.0, 1), title=getattr(st.current, "title", ""))
            if int(getattr(st, "playback_token", 0) or 0) != request_token or st.current is not current_ref:
                self.log("play_start_ignored", guild_id=guild_id, reason="stale_generation")
                return
            use_direct = self._should_use_direct_voice(st.current)
            if use_direct:
                await asyncio.wait_for(self._play_direct_voice(guild_id, st.current), timeout=max(5.0, self.prepare_timeout))
            else:
                await asyncio.wait_for(self._play_lavalink(guild_id, st.current), timeout=max(5.0, self.prepare_timeout))
        except Exception as exc:
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="play_failed", error=f"{type(exc).__name__}: {short_text(exc, 260)}")
            self.log("play_failed", guild_id=guild_id, transport=st.transport or "unknown", error=st.last_error)

    def _should_use_direct_voice(self, track: AgentTrack) -> bool:
        if not self.direct_audio_enabled or self.lavalink_for_direct_streams:
            return False
        stream_raw = " ".join([track.stream_url, track.transport_hint, track.source, track.query]).lower()
        if track.stream_url and self.direct_youtube_enabled:
            if track.transport_hint.startswith("direct") or any(marker in stream_raw for marker in ("googlevideo", "yt-dlp", "ytdlp", "music-agent-ytdlp", "worker-ytdlp", "youtube", "youtu.be", "soundcloud", "sndcdn", "cdn")):
                return True
        raw = " ".join([track.source, track.query, track.webpage_url, track.stream_url, track.transport_hint]).lower()
        if raw.startswith(_LAVALINK_PREFIXES) or any(prefix in raw for prefix in ("spotify.com", "scsearch:", "spsearch:")):
            return False
        return False

    async def _resolve_guild_and_channel(self, guild_id: int, voice_channel_id: int) -> tuple[Any, Any]:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"guild {guild_id} não encontrada no player remoto")
        channel = guild.get_channel(voice_channel_id) or self.client.get_channel(voice_channel_id)
        if channel is None:
            raise RuntimeError(f"canal de voz {voice_channel_id} não encontrado")
        return guild, channel

    async def _play_direct_voice(self, guild_id: int, track: AgentTrack) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if not track.stream_url:
            raise RuntimeError("track sem stream_url direto")
        guild, channel = await self._resolve_guild_and_channel(guild_id, st.voice_channel_id)
        self.log("voice_connecting", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
        existing = guild.voice_client
        if existing is not None and isinstance(existing, getattr(wavelink, "Player", ())):
            with contextlib.suppress(Exception):
                await existing.disconnect()
            existing = None
        if existing is None or not getattr(existing, "is_connected", lambda: False)():
            voice_client = await channel.connect(self_deaf=True)
        else:
            voice_client = existing
            if getattr(getattr(voice_client, "channel", None), "id", None) != st.voice_channel_id:
                await voice_client.move_to(channel)
        if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
            voice_client.stop()
        source = self._build_ffmpeg_source(track.stream_url, volume_percent=st.volume_percent, start_offset_seconds=getattr(track, "start_offset_seconds", 0.0))
        st.player = voice_client
        st.transport = "direct"
        st.playback_token += 1
        playback_token = st.playback_token
        self._set_status(st, "starting", event="direct_player_starting")
        self.log("player_play_called", guild_id=guild_id, transport="direct", title=track.title, offset=round(float(getattr(track, "start_offset_seconds", 0.0) or 0.0), 2))

        def after(error: Exception | None) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(self._direct_after(guild_id, error, playback_token), loop)

        voice_client.play(source, after=after)
        # Confirmação curta: evita segurar a UI/reação depois que discord.py já
        # iniciou o FFmpeg, mas ainda confirma que a voz realmente ficou tocando.
        confirm_delay = max(0.25, min(1.2, env_float("MUSIC_AGENT_DIRECT_CONFIRM_SECONDS", 0.35)))
        await asyncio.sleep(confirm_delay)
        if not getattr(voice_client, "is_connected", lambda: False)():
            raise RuntimeError("conectei no canal, mas a voz caiu antes do áudio")
        if not getattr(voice_client, "is_playing", lambda: False)() and not getattr(voice_client, "is_paused", lambda: False)():
            raise RuntimeError("ffmpeg iniciou, mas o áudio não ficou tocando")
        st.started_monotonic = time.monotonic() - max(0.0, float(getattr(track, "start_offset_seconds", 0.0) or 0.0))
        self._set_status(st, "playing", event="direct_track_start_confirmed")
        self.log("play_started", guild_id=guild_id, transport="direct", title=track.title, confirm_delay=confirm_delay)
        self._schedule_next_queue_prefetch(guild_id, reason="direct_playing")

    def _ffmpeg_before_options_for_offset(self, start_offset_seconds: float = 0.0) -> str:
        try:
            offset = max(0.0, float(start_offset_seconds or 0.0))
        except Exception:
            offset = 0.0
        if offset <= 0.05:
            return self.ffmpeg_before_options
        # -ss antes do input torna o seek rápido para URLs remotas.
        return f"-ss {offset:.3f} {self.ffmpeg_before_options}".strip()

    def _build_ffmpeg_source(self, stream_url: str, *, volume_percent: int | None = None, start_offset_seconds: float = 0.0) -> Any:
        volume = max(0.0, min(10.0, float(volume_percent if volume_percent is not None else self.default_volume_percent) / 100.0))
        before_options = self._ffmpeg_before_options_for_offset(start_offset_seconds)
        if self.direct_pcm_volume_enabled:
            pcm = discord.FFmpegPCMAudio(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=before_options,
                options=self.ffmpeg_options,
            )
            loop = self._loop or asyncio.get_running_loop()
            return AgentMixedAudioSource(loop=loop, music_source=pcm, music_volume=volume, duck_factor=max(0.0, min(1.0, self.duck_volume_percent / 100.0)))
        opus_cls = getattr(discord, "FFmpegOpusAudio", None)
        if opus_cls is not None:
            return opus_cls(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=before_options,
                options=self.ffmpeg_options,
                bitrate=self.ffmpeg_bitrate,
            )
        pcm = discord.FFmpegPCMAudio(
            stream_url,
            executable=self.ffmpeg_executable,
            before_options=before_options,
            options=self.ffmpeg_options,
        )
        return discord.PCMVolumeTransformer(pcm, volume=volume)

    async def _direct_after(self, guild_id: int, error: Exception | None, playback_token: int = 0) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if playback_token and playback_token != int(getattr(st, "playback_token", 0) or 0):
            return
        if st.current is None and not st.queue:
            return
        played_for = time.monotonic() - float(st.started_monotonic or 0.0) if st.started_monotonic else 0.0
        min_ok = max(0.5, env_float("MUSIC_AGENT_EARLY_END_SECONDS", 2.5))
        if error:
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_error", error=f"{type(error).__name__}: {short_text(error, 260)}")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error)
            if st.queue:
                await self._play_next(guild_id)
            return
        if played_for < min_ok and st.current is not None:
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_early_end", error=f"áudio encerrou cedo demais ({played_for:.1f}s)")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error, title=getattr(st.current, "title", ""))
            if st.queue:
                await self._play_next(guild_id)
            return
        self.log("play_ended", guild_id=guild_id, transport="direct", title=getattr(st.current, "title", ""))
        await self._finish_current(guild_id, error=None, event="direct_track_end")

    async def _play_lavalink(self, guild_id: int, track: AgentTrack) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        await self.ensure_lavalink_pool()
        guild, channel = await self._resolve_guild_and_channel(guild_id, st.voice_channel_id)
        player_cls = getattr(wavelink, "Player", None)
        if player_cls is None:
            raise RuntimeError("Wavelink não expõe Player")
        existing = guild.voice_client
        if existing is not None and not isinstance(existing, player_cls):
            with contextlib.suppress(Exception):
                if getattr(existing, "is_playing", lambda: False)() or getattr(existing, "is_paused", lambda: False)():
                    existing.stop()
                await existing.disconnect(force=True)
            existing = None
        self.log("voice_connecting", guild_id=guild_id, channel=st.voice_channel_id, transport="lavalink")
        player = existing
        if player is None or not isinstance(player, player_cls) or not getattr(player, "connected", False):
            player = await channel.connect(cls=player_cls, self_deaf=True)
        elif getattr(getattr(player, "channel", None), "id", None) != st.voice_channel_id:
            await player.move_to(channel)
        st.player = player
        st.transport = "lavalink"
        with contextlib.suppress(Exception):
            maybe = player.set_volume(max(0, min(150, int(st.volume_percent or self.default_volume_percent))))
            if asyncio.iscoroutine(maybe):
                await maybe
        playable = await self._playable_for_track(track)
        self.log("player_play_called", guild_id=guild_id, transport="lavalink", title=track.title)
        self._set_status(st, "starting", event="lavalink_player_play_called")
        await player.play(playable)
        await asyncio.sleep(0.25)
        # Não marque como tocando só porque o comando foi despachado.
        # O estado definitivo vem do evento TrackStart do Wavelink/Lavalink.
        self._set_status(st, "starting", event="lavalink_play_dispatched")
        self.log("play_dispatched", guild_id=guild_id, transport="lavalink", title=track.title)

    async def _finish_current(self, guild_id: int, *, error: str | None, event: str) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if error:
            self._set_status(st, "failed", event=event, error=error)
            return
        finished = st.current
        st.current = None
        st.paused = False
        loop_mode = str(getattr(st, "loop_mode", "off") or "off").strip().lower()
        if finished is not None and loop_mode != "one":
            self._push_history(st, finished)

        if finished is not None and loop_mode == "one":
            finished.start_offset_seconds = 0.0
            st.queue.insert(0, finished)
            self.log("loop_one_requeue", guild_id=guild_id, title=getattr(finished, "title", ""))
            await self._play_next(guild_id)
            return

        if finished is not None and loop_mode == "all":
            finished.start_offset_seconds = 0.0
            st.queue.append(finished)
            self.log("loop_all_requeue", guild_id=guild_id, title=getattr(finished, "title", ""), queue_size=len(st.queue))

        if st.queue:
            await self._play_next(guild_id)
            return
        self._set_status(st, "idle", event=event)
        # Fim normal de fila não é desconexão externa: mantenha a sessão de voz
        # viva e deixe o mesmo timeout AFK/idle decidir quando sair da call.
        self._schedule_idle_disconnect(guild_id)

    async def _playable_for_track(self, track: AgentTrack) -> Any:
        identifier = track.stream_url or track.webpage_url or track.query
        if not identifier:
            raise RuntimeError("track sem URL tocável")
        self.log("track_loading", title=track.title, transport="lavalink", identifier=identifier[:80])
        search = await wavelink.Playable.search(identifier)
        if isinstance(search, list):
            if not search:
                raise RuntimeError("Lavalink não retornou playable")
            return search[0]
        tracks = getattr(search, "tracks", None)
        if tracks:
            return tracks[0]
        if hasattr(search, "__iter__"):
            items = list(search)
            if items:
                return items[0]
        raise RuntimeError("Lavalink não retornou playable")

    async def resolve_track(self, query: str, *, track_meta: dict[str, Any], body: dict[str, Any]) -> AgentTrack:
        direct = str(track_meta.get("stream_url") or body.get("stream_url") or "").strip()
        title_hint = _metadata_text(track_meta.get("title") or body.get("title"), limit=160)
        requester_id = safe_id(body.get("requester_id") or track_meta.get("requester_id"))
        requester_name = short_text(body.get("requester_name") or track_meta.get("requester_name"), 80)
        source = short_text(track_meta.get("source") or body.get("source") or "worker-agent", 80)
        webpage_url = str(track_meta.get("webpage_url") or track_meta.get("original_url") or query).strip()
        if direct.startswith(("http://", "https://")):
            return AgentTrack(
                title=title_hint or short_text(query, 160) or "Música",
                requester_id=requester_id,
                requester_name=requester_name,
                query=query,
                webpage_url=webpage_url,
                stream_url=direct,
                duration=_float_or_none(track_meta.get("duration")),
                uploader=_metadata_text(track_meta.get("uploader"), limit=120),
                thumbnail=short_text(track_meta.get("thumbnail"), 500),
                source=source or "worker-ytdlp",
                transport_hint="direct",
                audio_format_id=short_text(track_meta.get("resolved_audio_format_id") or track_meta.get("audio_format_id"), 40),
                audio_ext=short_text(track_meta.get("resolved_audio_ext") or track_meta.get("audio_ext"), 20).lower(),
                audio_codec=short_text(track_meta.get("resolved_audio_codec") or track_meta.get("audio_codec"), 40).lower(),
                audio_abr=int(float(track_meta.get("resolved_audio_abr") or track_meta.get("audio_abr") or 0) or 0),
                start_offset_seconds=max(0.0, float(track_meta.get("start_offset_seconds") or track_meta.get("start") or body.get("position_seconds") or 0.0)),
            )
        cache_key = self._resolve_cache_key(query, track_meta)
        cached = self._resolve_cache_get(cache_key)
        if cached:
            self.log("resolve_stream_cache_hit", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
            return self._agent_track_from_resolved(cached, query=query, track_meta=track_meta, body=body, cached=True)
        cached_meta = self._metadata_cache_get(cache_key)
        if cached_meta:
            # Metadata stable can fill missing title/artist/duration while yt-dlp
            # refreshes the short-lived playable URL.
            merged_meta = dict(cached_meta)
            merged_meta.update({k: v for k, v in track_meta.items() if v not in (None, "", [], {})})
            track_meta = merged_meta
            self.log("resolve_metadata_cache_hit", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
        lock = self._resolve_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._resolve_cache_get(cache_key)
            if cached:
                self.log("resolve_stream_cache_hit_after_wait", guild_id=safe_id(body.get("guild_id")), title=track_meta.get("title"), query=query[:90])
                return self._agent_track_from_resolved(cached, query=query, track_meta=track_meta, body=body, cached=True)
            started = time.time()
            resolved = await asyncio.to_thread(self._resolve_with_ytdlp, query)
            self._resolve_cache_put(cache_key, resolved)
            self.log("resolve_ytdlp_done", guild_id=safe_id(body.get("guild_id")), elapsed_ms=round((time.time() - started) * 1000.0, 1), title=resolved.get("title"))
            return self._agent_track_from_resolved(resolved, query=query, track_meta=track_meta, body=body, cached=False)

    def _resolve_with_ytdlp(self, query: str) -> dict[str, Any]:
        target = query
        lowered = query.lower().strip()
        if not _looks_like_url(query) and not lowered.startswith(_LOCAL_SEARCH_PREFIXES):
            target = f"{self.default_search.rstrip(':')}:{query}"
        base_cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
        cookies = Path(self.cookies_file).expanduser()
        if cookies.exists() and cookies.stat().st_size > 0:
            base_cmd += ["--cookies", str(cookies)]
        if self.js_runtimes:
            base_cmd += ["--js-runtimes", self.js_runtimes]
        base_cmd += ["--no-playlist", "--no-warnings", "--socket-timeout", "12"]
        self.log("yt_dlp_resolve", query=query, target=target, js=self.js_runtimes)
        if _looks_like_url(query):
            fast_cmd = base_cmd + [
                "-f", self.ytdlp_format,
                "--print", "__title__:%(title)s",
                "--print", "__uploader__:%(uploader,channel,creator)s",
                "--print", "__duration__:%(duration)s",
                "--print", "__thumbnail__:%(thumbnail)s",
                "--print", "__webpage_url__:%(webpage_url,original_url)s",
                "-g", target,
            ]
            fast_started = time.time()
            fast = subprocess.run(fast_cmd, cwd=str(Path.home() / "phone-worker"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(5, min(self.ytdlp_timeout, 18)))
            lines = [line.strip() for line in (fast.stdout or "").splitlines() if line.strip()]
            urls = [line for line in lines if line.startswith(("http://", "https://")) and not line.startswith(("https://i.ytimg.com", "http://i.ytimg.com"))]
            def marker(name: str) -> str:
                prefix = f"__{name}__:"
                return next((line.split(":", 1)[1].strip() for line in lines if line.startswith(prefix)), "")
            title_hint = marker("title")
            uploader_hint = marker("uploader")
            duration_hint = marker("duration")
            thumbnail_hint = marker("thumbnail")
            webpage_hint = marker("webpage_url")
            if fast.returncode == 0 and urls:
                self.log(
                    "yt_dlp_fast_url_ok",
                    elapsed_ms=round((time.time() - fast_started) * 1000.0, 1),
                    titled=bool(title_hint),
                    uploader=bool(uploader_hint),
                    duration=bool(duration_hint),
                )
                return {
                    "title": title_hint or query,
                    "uploader": uploader_hint,
                    "duration": _duration_from_ytdlp(duration_hint),
                    "thumbnail": thumbnail_hint,
                    "webpage_url": webpage_hint or query,
                    "stream_url": urls[0],
                    "audio_format_id": "yt-dlp-fast",
                    "audio_ext": "",
                    "audio_codec": "",
                    "audio_abr": 0,
                }
            self.log("yt_dlp_fast_url_fallback", rc=fast.returncode, error=short_text(fast.stderr, 160))
        cmd = base_cmd + ["-f", self.ytdlp_format, "-J", target]
        proc = subprocess.run(cmd, cwd=str(Path.home() / "phone-worker"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.ytdlp_timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(short_text(proc.stderr or f"yt-dlp rc={proc.returncode}", 300))
        data = json.loads(proc.stdout or "{}")
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            data = next((item for item in data.get("entries") or [] if isinstance(item, dict)), {})
        if not isinstance(data, dict) or not data:
            raise RuntimeError("yt-dlp não retornou mídia")
        stream_info = _select_stream_info(data)
        stream_url = str(stream_info.get("stream_url") or "")
        if not stream_url:
            raise RuntimeError("yt-dlp não retornou stream_url")
        return {
            "title": data.get("title") or data.get("fulltitle") or query,
            "uploader": data.get("uploader") or data.get("channel") or data.get("creator") or "",
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "webpage_url": data.get("webpage_url") or data.get("original_url") or query,
            **stream_info,
        }

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self.log("api_ready", url=f"http://{self.host}:{self.port}", token="sim" if self.token else "não")
        if not self.discord_token:
            raise RuntimeError("defina MUSIC_AGENT_BOT_TOKEN, DISCORD_TOKEN ou BOT_TOKEN no worker")
        await self.client.start(self.discord_token)


async def amain() -> None:
    agent = MusicAgent()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(agent.run())
    done, pending = await asyncio.wait({task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        await agent.client.close()
    for item in pending:
        item.cancel()
    for item in done:
        exc = item.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(amain())
