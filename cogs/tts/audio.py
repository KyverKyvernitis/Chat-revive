import contextlib
import asyncio
import base64
import concurrent.futures
import errno
import hashlib
import html
import json
import inspect
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import logging
import urllib.request
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
import aiohttp
import edge_tts
import requests
from gtts import gTTS
from gtts.tts import gTTSError


import config
from .helpers import validate_voice

try:
    import fcntl
except ImportError:  # pragma: no cover - FIFO fast path is POSIX-only.
    fcntl = None


GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt")
TTS_IDLE_DISCONNECT_SECONDS = int(getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 240))
TTS_AUDIO_CACHE_SIZE = max(1, int(getattr(config, "TTS_AUDIO_CACHE_SIZE", 128)))
TTS_AUDIO_CACHE_TTL_SECONDS = int(getattr(config, "TTS_AUDIO_CACHE_TTL_SECONDS", 900))
TTS_DEBUG_LOGS = bool(getattr(config, "TTS_DEBUG_LOGS", False))
TTS_WARM_HOLD_SECONDS = float(getattr(config, "TTS_WARM_HOLD_SECONDS", 30))
TTS_QUEUE_MAXSIZE = max(1, int(getattr(config, "TTS_QUEUE_MAXSIZE", 20)))
TTS_SYNTH_CONCURRENCY = max(1, int(getattr(config, "TTS_SYNTH_CONCURRENCY", 3)))
TTS_EDGE_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_EDGE_TIMEOUT_SECONDS", 10)))
TTS_EDGE_CONNECT_TIMEOUT_SECONDS = max(1, int(getattr(config, "TTS_EDGE_CONNECT_TIMEOUT_SECONDS", 4)))
TTS_EDGE_RECEIVE_TIMEOUT_SECONDS = max(1, int(getattr(config, "TTS_EDGE_RECEIVE_TIMEOUT_SECONDS", 15)))
TTS_EDGE_VPS_FAST_PATH_ENABLED = bool(getattr(config, "TTS_EDGE_VPS_FAST_PATH_ENABLED", True))
TTS_EDGE_STREAMING_ENABLED = bool(getattr(config, "TTS_EDGE_STREAMING_ENABLED", True))
TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS", 4.0)))
TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS = max(
    TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS,
    float(getattr(config, "TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS", 30.0)),
)
TTS_EDGE_STREAM_PREBUFFER_MS = min(1200, max(100, int(getattr(config, "TTS_EDGE_STREAM_PREBUFFER_MS", 220))))
TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS = min(128, max(8, int(getattr(config, "TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS", 64))))
TTS_EDGE_STREAM_CHUNK_BYTES = min(64 * 1024, max(4 * 1024, int(getattr(config, "TTS_EDGE_STREAM_CHUNK_BYTES", 16 * 1024))))
TTS_EDGE_STREAM_PIPE_OPEN_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "TTS_EDGE_STREAM_PIPE_OPEN_TIMEOUT_SECONDS", 10.0)))
TTS_EDGE_STREAM_PIPE_BYTES = min(1024 * 1024, max(64 * 1024, int(getattr(config, "TTS_EDGE_STREAM_PIPE_BYTES", 128 * 1024))))
TTS_EDGE_STREAM_CACHE_BUFFER_BYTES = min(1024 * 1024, max(16 * 1024, int(getattr(config, "TTS_EDGE_STREAM_CACHE_BUFFER_BYTES", 128 * 1024))))
TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES = min(
    8 * 1024 * 1024,
    max(256 * 1024, int(getattr(config, "TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES", 2 * 1024 * 1024))),
)
TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED = bool(getattr(config, "TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED", True))
TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS = min(TTS_EDGE_STREAM_PREBUFFER_MS, max(100, int(getattr(config, "TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS", 160))))
TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS = max(TTS_EDGE_STREAM_PREBUFFER_MS, min(1200, int(getattr(config, "TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS", 400))))
TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS = max(4, int(getattr(config, "TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS", 20)))
TTS_EDGE_STREAM_STALL_THRESHOLD_MS = max(20.0, float(getattr(config, "TTS_EDGE_STREAM_STALL_THRESHOLD_MS", 35.0)))
TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED = bool(getattr(config, "TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED", True))
TTS_EDGE_CIRCUIT_BREAKER_ENABLED = bool(getattr(config, "TTS_EDGE_CIRCUIT_BREAKER_ENABLED", True))
TTS_EDGE_CIRCUIT_BREAKER_FAILURES = max(2, int(getattr(config, "TTS_EDGE_CIRCUIT_BREAKER_FAILURES", 3)))
TTS_EDGE_CIRCUIT_BREAKER_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "TTS_EDGE_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 15.0)))
TTS_EDGE_PREFETCH_CONCURRENCY = min(
    TTS_SYNTH_CONCURRENCY,
    max(1, int(getattr(config, "TTS_EDGE_PREFETCH_CONCURRENCY", max(1, TTS_SYNTH_CONCURRENCY - 1)))),
)
TTS_GTTS_CONCURRENCY = max(1, int(getattr(config, "TTS_GTTS_CONCURRENCY", 1)))
TTS_GTTS_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "TTS_GTTS_TIMEOUT_SECONDS", 20.0)))
TTS_GTTS_CONNECT_TIMEOUT_SECONDS = max(0.5, float(getattr(config, "TTS_GTTS_CONNECT_TIMEOUT_SECONDS", 3.5)))
TTS_GTTS_READ_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_GTTS_READ_TIMEOUT_SECONDS", 8.0)))
TTS_GTTS_PERSISTENT_SESSION_ENABLED = bool(getattr(config, "TTS_GTTS_PERSISTENT_SESSION_ENABLED", True))
TTS_GTTS_SESSION_TTL_SECONDS = max(10.0, float(getattr(config, "TTS_GTTS_SESSION_TTL_SECONDS", 90.0)))
TTS_GTTS_SESSION_MAX_REQUESTS = max(4, int(getattr(config, "TTS_GTTS_SESSION_MAX_REQUESTS", 64)))
TTS_GTTS_STREAMING_ENABLED = bool(getattr(config, "TTS_GTTS_STREAMING_ENABLED", True))
TTS_GTTS_STREAM_MIN_CHARS = max(1, int(getattr(config, "TTS_GTTS_STREAM_MIN_CHARS", 101)))
TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS = max(
    1.0,
    float(getattr(config, "TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS", 6.0)),
)
TTS_PERSISTENT_STATS_FLUSH_SECONDS = max(
    0.05,
    float(getattr(config, "TTS_PERSISTENT_STATS_FLUSH_SECONDS", 0.5)),
)
TTS_FFMPEG_PRIME_ENABLED = bool(getattr(config, "TTS_FFMPEG_PRIME_ENABLED", True))
TTS_FFMPEG_PRIME_TIMEOUT_SECONDS = max(
    0.25,
    float(getattr(config, "TTS_FFMPEG_PRIME_TIMEOUT_SECONDS", 1.5)),
)
TTS_CACHE_MAINTENANCE_DELAY_SECONDS = max(
    0.1,
    float(getattr(config, "TTS_CACHE_MAINTENANCE_DELAY_SECONDS", 0.75)),
)
TTS_LATENCY_SAMPLE_WINDOW = min(2048, max(32, int(getattr(config, "TTS_LATENCY_SAMPLE_WINDOW", 256))))
TTS_PLAYBACK_TIMEOUT_BASE_SECONDS = max(5.0, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_BASE_SECONDS", 12.0)))
TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS = max(0.0, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS", 0.08)))
TTS_PLAYBACK_TIMEOUT_MAX_SECONDS = max(TTS_PLAYBACK_TIMEOUT_BASE_SECONDS, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_MAX_SECONDS", 120.0)))
TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS", 25.0)))
TTS_CACHEABLE_TEXT_MAX_LENGTH = max(64, int(getattr(config, "TTS_CACHEABLE_TEXT_MAX_LENGTH", 320)))
TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH = max(TTS_CACHEABLE_TEXT_MAX_LENGTH, int(getattr(config, "TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH", 1200)))
TTS_LONG_TEXT_CACHE_MIN_REPEATS = max(1, int(getattr(config, "TTS_LONG_TEXT_CACHE_MIN_REPEATS", 2)))
TTS_TEMP_PRUNE_INTERVAL_SECONDS = max(5.0, float(getattr(config, "TTS_TEMP_PRUNE_INTERVAL_SECONDS", 20)))
TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS = max(5.0, float(getattr(config, "TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS", 30.0)))
TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES = max(4, int(getattr(config, "TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES", 32) or 32))
TTS_BOOT_WARMUP_ENABLED = bool(getattr(config, "TTS_BOOT_WARMUP_ENABLED", True))
TTS_ENGINE_ALERT_COOLDOWN_SECONDS = max(60.0, float(getattr(config, "TTS_ENGINE_ALERT_COOLDOWN_SECONDS", 900)))
TTS_ENGINE_FAILURE_ALERT_THRESHOLD = max(1, int(getattr(config, "TTS_ENGINE_FAILURE_ALERT_THRESHOLD", 3)))
TTS_ENGINE_SLOW_WARN_SECONDS = max(1.0, float(getattr(config, "TTS_ENGINE_SLOW_WARN_SECONDS", 8.0)))
TTS_OPUS_PLAYBACK_ENABLED = bool(getattr(config, "TTS_OPUS_PLAYBACK_ENABLED", True))
TTS_OPUS_PLAYBACK_COPY_CODEC = bool(getattr(config, "TTS_OPUS_PLAYBACK_COPY_CODEC", True))
WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB = max(1, int(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB", 8) or 8))
TTS_FFMPEG_BEFORE_OPTIONS = getattr(config, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin")
TTS_FFMPEG_OPTIONS = getattr(config, "TTS_FFMPEG_OPTIONS", "-vn -loglevel error")
TTS_TEMP_DIR = os.path.abspath(str(getattr(config, "TTS_TEMP_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio")) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio")).strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio"))
TTS_TEMP_MAX_MB = max(64, int(getattr(config, "TTS_TEMP_MAX_MB", 256)))
TTS_TEMP_MAX_FILES = max(32, int(getattr(config, "TTS_TEMP_MAX_FILES", 256)))
TTS_TEMP_MAX_BYTES = TTS_TEMP_MAX_MB * 1024 * 1024
TTS_TURBO_BENCHMARK_ENABLED = bool(getattr(config, "TTS_TURBO_BENCHMARK_ENABLED", True))
TTS_TURBO_BENCHMARK_GUILD_ID = int(getattr(config, "TTS_TURBO_BENCHMARK_GUILD_ID", 927002914449424404) or 927002914449424404)
TTS_TURBO_BENCHMARK_TRIGGER_TEXT = str(getattr(config, "TTS_TURBO_BENCHMARK_TRIGGER_TEXT", "teste") or "teste").strip().lower() or "teste"
TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS = max(1.5, float(getattr(config, "TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS", 12.0) or 12.0))
TTS_TURBO_BENCHMARK_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_TURBO_BENCHMARK_MAX_AUDIO_MB", 4) or 4))
TTS_PIPER_EXPERIMENT_ENABLED = bool(getattr(config, "TTS_PIPER_EXPERIMENT_ENABLED", True))
TTS_PIPER_EXPERIMENT_GUILD_ID = int(getattr(config, "TTS_PIPER_EXPERIMENT_GUILD_ID", 0) or 0)
TTS_PIPER_EXPERIMENT_PREFIX = str(getattr(config, "TTS_PIPER_EXPERIMENT_PREFIX", "%") or "%").strip() or "%"
TTS_PIPER_EXPERIMENT_ENGINE = str(getattr(config, "TTS_PIPER_EXPERIMENT_ENGINE", "android_native") or "android_native").strip().lower().replace("-", "_") or "android_native"
TTS_PIPER_WORKER_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_PIPER_WORKER_TIMEOUT_SECONDS", 6.0) or 6.0))
TTS_PIPER_MAX_TEXT_LENGTH = max(16, int(getattr(config, "TTS_PIPER_MAX_TEXT_LENGTH", 600) or 600))
TTS_PIPER_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_PIPER_MAX_AUDIO_MB", 8) or 8))
TTS_PIPER_MODEL_NAME = str(getattr(config, "TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
TTS_PIPER_VPS_CACHE_SIZE = max(32, int(getattr(config, "TTS_PIPER_VPS_CACHE_SIZE", 2048) or 2048))
TTS_PIPER_VPS_CACHE_MAX_MB = max(64, int(getattr(config, "TTS_PIPER_VPS_CACHE_MAX_MB", 2048) or 2048))
TTS_PIPER_VPS_CACHE_MAX_BYTES = TTS_PIPER_VPS_CACHE_MAX_MB * 1024 * 1024
TTS_TURBO_WORKER_CACHE_ENABLED = bool(getattr(config, "TTS_TURBO_WORKER_CACHE_ENABLED", True))
TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS = max(0.15, float(getattr(config, "TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS", 0.65) or 0.65))
TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS = max(0.5, float(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS", 2.5) or 2.5))
TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB", 8) or 8))
TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND = bool(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND", True))
TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY = max(1, int(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY", 1) or 1))
TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING = max(TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY, int(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING", 6) or 6))
TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS = max(1.0, float(getattr(config, "TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS", 45.0) or 45.0))
TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS = max(1.0, float(getattr(config, "TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS", 10.0) or 10.0))
TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES = max(128, int(getattr(config, "TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES", 4096) or 4096))
TTS_WORKER_AGENT_ENABLED = bool(getattr(config, "TTS_WORKER_AGENT_ENABLED", True))
TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS = max(5.0, float(getattr(config, "TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS", 20.0) or 20.0))
TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS = max(0.4, float(getattr(config, "TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS", 2.5) or 2.5))
TTS_WORKER_AGENT_STALE_SECONDS = max(TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS + 5.0, float(getattr(config, "TTS_WORKER_AGENT_STALE_SECONDS", 75.0) or 75.0))
TTS_WORKER_AGENT_FAILURE_THRESHOLD = max(1, int(getattr(config, "TTS_WORKER_AGENT_FAILURE_THRESHOLD", 2) or 2))
TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS", 45.0) or 45.0))
TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS", 10.0) or 10.0))
TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS = max(0, int(getattr(config, "TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS", 2) or 2))
TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS = max(0.05, float(getattr(config, "TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS", 0.35) or 0.35))
TTS_WORKER_AGENT_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_WORKER_AGENT_MAX_AUDIO_MB", 8) or 8))
TTS_WORKER_AGENT_MAX_TEXT_LENGTH = max(64, int(getattr(config, "TTS_WORKER_AGENT_MAX_TEXT_LENGTH", 1200) or 1200))
TTS_TETO_MAX_TEXT_LENGTH = max(16, int(getattr(config, "TTS_TETO_MAX_TEXT_LENGTH", 180) or 180))
TTS_TETO_WORKER_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "TTS_TETO_WORKER_TIMEOUT_SECONDS", 25.0) or 25.0))
TTS_TETO_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_TETO_MAX_AUDIO_MB", 8) or 8))
TTS_WORKER_AGENT_PREFERRED_ENGINE = str(getattr(config, "TTS_WORKER_AGENT_PREFERRED_ENGINE", "auto") or "auto").strip().lower().replace("-", "_") or "auto"
TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD = max(1, int(getattr(config, "TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD", 3) or 3))
TTS_WORKER_AGENT_RAW_AUDIO_ENABLED = bool(getattr(config, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True))
TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED = bool(getattr(config, "TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED", True))
TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES = {
    item.strip().lower().replace("-", "_")
    for item in str(getattr(config, "TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES", "android_native,teto") or "android_native,teto").split(",")
    if item.strip()
}
TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS = max(0, int(getattr(config, "TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS", 120) or 120))
TTS_WORKER_AGENT_WORKER_SLOW_MARGIN = max(1.0, float(getattr(config, "TTS_WORKER_AGENT_WORKER_SLOW_MARGIN", 1.15) or 1.15))
TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS = max(0.0, float(getattr(config, "TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS", 120.0) or 120.0))
WORKER_VOICE_AGENT_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_ENABLED", True))
WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", True))
WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED", True))
WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED", False))
WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS = max(16, int(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS", 600) or 600))
WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS = max(3.0, float(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS", 30.0) or 30.0))
WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS", 45.0) or 45.0))
WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", True))
WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED", True))
WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS = max(0.6, float(getattr(config, "WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS", 1.5) or 1.5))
WORKER_VOICE_AGENT_SESSION_TTL_SECONDS = max(30.0, float(getattr(config, "WORKER_VOICE_AGENT_SESSION_TTL_SECONDS", 180.0) or 180.0))
WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS = max(3.0, float(getattr(config, "WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS", 15.0) or 15.0))
WORKER_VOICE_AGENT_HANDOFF_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_HANDOFF_ENABLED", True))
WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS = max(10.0, float(getattr(config, "WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS", 60.0) or 60.0))
WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS = max(0.6, float(getattr(config, "WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS", 1.5) or 1.5))
WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", True))
WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED", True))
WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS = max(0.6, float(getattr(config, "WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS", 1.5) or 1.5))
WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS = max(10.0, float(getattr(config, "WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45.0) or 45.0))
WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED", True))
WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED = bool(getattr(config, "WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED", False))
WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS", 4.0) or 4.0))
WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS = max(0.6, float(getattr(config, "WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS", 1.5) or 1.5))
TTS_LONG_TEXT_CHUNK_ENABLED = bool(getattr(config, "TTS_LONG_TEXT_CHUNK_ENABLED", True))
TTS_LONG_TEXT_CHUNK_MAX_CHARS = max(160, int(getattr(config, "TTS_LONG_TEXT_CHUNK_MAX_CHARS", 420) or 420))
TTS_EDGE_LONG_TEXT_CHUNK_MAX_BYTES = min(
    3800,
    max(512, int(getattr(config, "TTS_EDGE_LONG_TEXT_CHUNK_MAX_BYTES", 3000) or 3000)),
)
TTS_LONG_TEXT_CHUNK_MAX_PARTS = max(1, int(getattr(config, "TTS_LONG_TEXT_CHUNK_MAX_PARTS", 8) or 8))
PHONE_WORKER_ENABLED = bool(getattr(config, "PHONE_WORKER_ENABLED", False))
PHONE_WORKER_HOST = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
PHONE_WORKER_SCHEME = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
PHONE_WORKER_TOKEN = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()

_RUNTIME_DIR = os.path.join(TTS_TEMP_DIR, "runtime")
_CACHE_DIR = os.path.join(TTS_TEMP_DIR, "cache")
_TTS_REQUIRED_DIRS = (TTS_TEMP_DIR, _RUNTIME_DIR, _CACHE_DIR)
_TTS_CACHE_SUFFIXES = (".mp3", ".ogg", ".opus", ".wav", ".m4a", ".mulaw", ".alaw")

logger = logging.getLogger(__name__)

# Marcador dedicado para acordar o escritor no fim da síntese. Sem ele, um
# FIFO vazio podia pagar até 250 ms extras aguardando o próximo polling.
_TTS_STREAM_END = object()
_GTTS_AUDIO_LINE_RE = re.compile(r'jQ1olc","\[\\"(.*)\\"\]')


_DiscordAudioSourceBase = getattr(discord, "AudioSource", object)


class _PrioritySemaphore:
    """Semaphore that lets current speech overtake speculative prefetch."""

    def __init__(self, value: int = 1) -> None:
        if value < 0:
            raise ValueError("Semaphore initial value must be >= 0")
        self._value = int(value)
        self._foreground_waiters: deque[asyncio.Future] = deque()
        self._background_waiters: deque[asyncio.Future] = deque()
        self._task_waiters: dict[asyncio.Task, tuple[asyncio.Future, bool]] = {}

    def locked(self) -> bool:
        return self._value <= 0

    @property
    def foreground_waiters(self) -> int:
        return sum(1 for future in self._foreground_waiters if not future.done())

    def promote(self, task: asyncio.Task | None) -> bool:
        """Move a queued speculative acquisition to foreground priority."""
        if task is None:
            return False
        record = self._task_waiters.get(task)
        if record is None:
            return False
        future, foreground = record
        if foreground or future.done():
            return False
        with contextlib.suppress(ValueError):
            self._background_waiters.remove(future)
        self._foreground_waiters.append(future)
        self._task_waiters[task] = (future, True)
        return True

    def _remove_waiter(self, future: asyncio.Future) -> None:
        for waiters in (self._foreground_waiters, self._background_waiters):
            with contextlib.suppress(ValueError):
                waiters.remove(future)

    async def acquire(self, *, foreground: bool = True) -> bool:
        if (
            self._value > 0
            and not self._foreground_waiters
            and not self._background_waiters
        ):
            self._value -= 1
            return True

        future = asyncio.get_running_loop().create_future()
        waiters = self._foreground_waiters if foreground else self._background_waiters
        waiters.append(future)
        task = asyncio.current_task()
        if task is not None:
            self._task_waiters[task] = (future, foreground)
        try:
            await future
            return True
        except asyncio.CancelledError:
            if future.cancelled():
                self._remove_waiter(future)
            elif not future.done():
                future.cancel()
                self._remove_waiter(future)
            else:
                # release() had already handed this task a physical slot.
                self.release()
            raise
        finally:
            if task is not None:
                current = self._task_waiters.get(task)
                if current is not None and current[0] is future:
                    self._task_waiters.pop(task, None)

    def release(self) -> None:
        for waiters in (self._foreground_waiters, self._background_waiters):
            while waiters:
                future = waiters.popleft()
                if future.done():
                    continue
                future.set_result(True)
                return
        self._value += 1

    async def __aenter__(self):
        await self.acquire(foreground=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class _FirstFrameAudioSource(_DiscordAudioSourceBase):
    """Proxy transparente que mede a primeira leitura real do Discord."""

    def __init__(self, source: Any, on_first_frame, on_stream_read=None) -> None:
        self._source = source
        self._on_first_frame = on_first_frame
        self._on_stream_read = on_stream_read
        self._first_seen = False

    def read(self):
        started_at = time.monotonic()
        data = self._source.read()
        read_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
        if data and not self._first_seen:
            self._first_seen = True
            self._on_first_frame(time.monotonic(), read_ms)
        elif data and self._first_seen and callable(self._on_stream_read):
            self._on_stream_read(read_ms)
        return data

    def is_opus(self) -> bool:
        method = getattr(self._source, "is_opus", None)
        return bool(method()) if callable(method) else False

    def cleanup(self) -> None:
        cleanup = getattr(self._source, "cleanup", None)
        if callable(cleanup):
            cleanup()

    def __getattr__(self, name: str):
        return getattr(self._source, name)


class _PrimedAudioSource(_DiscordAudioSourceBase):
    """Returns one frame decoded in advance, then delegates to FFmpeg."""

    def __init__(self, source: Any, first_frame: bytes) -> None:
        self._source = source
        self._first_frame = bytes(first_frame or b"")
        self._cleaned = False

    def read(self):
        if self._first_frame:
            frame = self._first_frame
            self._first_frame = b""
            return frame
        return self._source.read()

    def is_opus(self) -> bool:
        method = getattr(self._source, "is_opus", None)
        return bool(method()) if callable(method) else False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._first_frame = b""
        cleanup = getattr(self._source, "cleanup", None)
        if callable(cleanup):
            cleanup()

    def __getattr__(self, name: str):
        return getattr(self._source, name)


def _has_speakable_tts_text(text: str) -> bool:
    """Return whether a TTS engine receives at least one speakable character.

    gTTS and Edge can reject inputs made only of punctuation, whitespace or
    emoji. Mentions, links, custom emoji and attachments are converted to words
    before this check, so a Unicode letter/number is a safe minimum.
    """
    return any(character.isalnum() for character in str(text or ""))


def _ensure_tts_temp_dirs() -> dict[str, bool]:
    """Ensure local TTS runtime/cache directories exist.

    The external cleanup timer can legitimately remove empty directories. TTS
    generation uses tempfile.mkstemp(dir=runtime), so a missing runtime
    directory makes every engine fail before synthesis even starts. Keep this
    function tiny and call it before creating/listing temp files, not only at
    module import.
    """
    status: dict[str, bool] = {}
    for directory in _TTS_REQUIRED_DIRS:
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(Exception):
                os.chmod(directory, 0o700)
            status[directory] = os.path.isdir(directory)
        except Exception:
            status[directory] = False
    return status


def _tts_temp_dirs_snapshot() -> dict[str, object]:
    status = _ensure_tts_temp_dirs()
    return {
        "root": TTS_TEMP_DIR,
        "runtime": _RUNTIME_DIR,
        "cache": _CACHE_DIR,
        "ok": all(bool(v) for v in status.values()),
        "exists": {
            "root": bool(status.get(TTS_TEMP_DIR)),
            "runtime": bool(status.get(_RUNTIME_DIR)),
            "cache": bool(status.get(_CACHE_DIR)),
        },
    }


_ensure_tts_temp_dirs()


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
    enqueued_at_monotonic: float = field(default_factory=time.monotonic, repr=False, compare=False)
    _normalized_cache_text: Optional[str] = field(default=None, repr=False, compare=False)
    _cache_key_value: Optional[str] = field(default=None, repr=False, compare=False)
    _dedup_signature: Optional[str] = field(default=None, repr=False, compare=False)
    piper_fallback_engine: str = field(default="gtts", repr=False, compare=False)
    piper_fallback_voice: str = field(default="", repr=False, compare=False)
    piper_fallback_language: str = field(default="", repr=False, compare=False)
    piper_fallback_rate: str = field(default="+0%", repr=False, compare=False)
    piper_fallback_pitch: str = field(default="+0Hz", repr=False, compare=False)
    piper_model: str = field(default="", repr=False, compare=False)


@dataclass
class GuildTTSState:
    queue: asyncio.Queue
    worker_task: Optional[asyncio.Task] = None
    dashboard_enabled: bool = True
    last_text_channel_id: Optional[int] = None
    last_channel_id: Optional[int] = None
    warmed_until: float = 0.0
    cache_order: OrderedDict[str, float] = field(default_factory=OrderedDict)
    pending_signatures: dict[str, int] = field(default_factory=dict)
    last_hard_reset_at: float = 0.0
    lavalink_ignore_logged_until: float = 0.0
    playback_lock: Optional[asyncio.Lock] = field(default=None, repr=False, compare=False)


@dataclass
class EdgeStreamHandle:
    fifo_path: str
    part_path: str
    cache_key: str
    state: GuildTTSState
    item: QueueItem
    queue: asyncio.Queue
    store_in_cache: bool
    started_at: float
    first_audio_ms: float
    engine: str = "edge"
    semaphore: Optional[Any] = field(default=None, repr=False)
    semaphore_released: bool = field(default=False, repr=False)
    semaphore_release_deferred: bool = field(default=False, repr=False)
    prefetch_semaphore: Optional[asyncio.Semaphore] = field(default=None, repr=False)
    prefetch_semaphore_released: bool = field(default=False, repr=False)
    producer_done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    first_audio_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    producer_task: Optional[asyncio.Task] = field(default=None, repr=False)
    writer_task: Optional[asyncio.Task] = field(default=None, repr=False)
    cache_task: Optional[asyncio.Task] = field(default=None, repr=False)
    cache_buffer: Optional[bytearray] = field(default=None, repr=False)
    blocking_future: Optional[asyncio.Future] = field(default=None, repr=False)
    writer_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    reader_anchor_fd: Optional[int] = field(default=None, repr=False)
    reader_anchor_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cache_path: str = ""
    audio_bytes: int = 0
    first_chunk_bytes: int = 0
    network_first_audio_ms: float = 0.0
    prebuffer_fill_ms: float = 0.0
    local_handoff_ms: float = 0.0
    synth_slot_wait_ms: float = 0.0
    prebuffer_ms: int = 0
    prebuffer_profile_key: str = ""
    source_read_stalls: int = 0
    max_source_read_ms: float = 0.0
    error: Optional[BaseException] = field(default=None, repr=False)
    pipe_error: Optional[BaseException] = field(default=None, repr=False)
    cache_error: Optional[BaseException] = field(default=None, repr=False)
    activated: bool = False
    consumer_abandoned: bool = False
    cleaned: bool = False


@dataclass
class _PreparedTTSPlayback:
    path: str
    source: Any
    source_kind: str
    prime_ms: float

    def take_source(self) -> Any | None:
        source = self.source
        self.source = None
        return source

    def cleanup(self) -> None:
        source = self.take_source()
        cleanup = getattr(source, "cleanup", None)
        if callable(cleanup):
            with contextlib.suppress(Exception):
                cleanup()


class TTSAudioMixin:
    def _log_debug(self, text: str) -> None:
        if TTS_DEBUG_LOGS:
            logger.debug(text)

    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState(queue=asyncio.Queue(maxsize=TTS_QUEUE_MAXSIZE))
            self.guild_states[guild_id] = state
        return state

    async def synthesize_chatbot_attachment(
        self,
        *,
        guild_id: int,
        user_id: int,
        text: str,
        voice: str,
        language: str = "pt-br",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        timeout_seconds: float = 18.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> bytes | None:
        """Adapter público e estreito para o chatbot reutilizar cache/singleflight.

        A marca de prefetch mantém a prioridade abaixo da fala normal da call;
        se a mesma fala for enfileirada em seguida, ela reaproveita o cache.
        """
        clean_text = str(text or "").strip()[:800]
        if not _has_speakable_tts_text(clean_text):
            return None
        item = QueueItem(
            guild_id=int(guild_id),
            channel_id=0,
            author_id=int(user_id),
            text=clean_text,
            engine="edge",
            voice=str(voice or "pt-BR-FranciscaNeural"),
            language=str(language or "pt-br"),
            rate=str(rate or "+0%"),
            pitch=str(pitch or "+0Hz"),
        )
        setattr(item, "_tts_prefetch", True)
        state = self._get_state(int(guild_id))
        resolve_task = self._schedule_tts_background(
            self._resolve_or_generate_singleflight_audio(
                state, item, read_cache=True, store_in_cache=True,
            )
        )
        if resolve_task is None:
            return None

        # Se o chatbot perder seu deadline, a síntese compartilhada continua:
        # a fila de voz pode estar aguardando exatamente a mesma chave de cache.
        # O runtime de TTS rastreia/cancela a task no unload.
        def _observe(done: asyncio.Task) -> None:
            if not done.cancelled():
                with contextlib.suppress(Exception):
                    done.exception()

        resolve_task.add_done_callback(_observe)
        try:
            path, _generated = await asyncio.wait_for(
                asyncio.shield(resolve_task),
                timeout=max(1.0, float(timeout_seconds)),
            )
            def _read() -> bytes:
                with open(path, "rb") as handle:
                    return handle.read(max_bytes + 1)
            data = await asyncio.to_thread(_read)
        except (asyncio.TimeoutError, OSError, ValueError):
            logger.warning("[tts_voice] adapter chatbot falhou | guild=%s", guild_id)
            return None
        if not data or len(data) > max_bytes:
            return None
        return data


    def _cleanup_guild_state_if_idle(self, guild_id: int) -> bool:
        state = self.guild_states.get(guild_id)
        if state is None:
            return True

        task = getattr(state, "worker_task", None)
        if task is not None and not task.done():
            return False

        if not state.queue.empty():
            return False

        self.guild_states.pop(guild_id, None)

        cleanup = getattr(self, "_cleanup_guild_runtime_state", None)
        if cleanup is not None:
            try:
                cleanup(guild_id)
            except Exception:
                logger.exception("[tts_voice] Falha ao limpar estado runtime da guild=%s", guild_id)

        return True

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem) -> tuple[bool, int, bool]:
        if not _has_speakable_tts_text(getattr(item, "text", "")):
            logger.info(
                "[tts_voice] item descartado antes da fila | texto sem caracteres faláveis | guild=%s channel=%s user=%s engine=%s",
                guild_id,
                getattr(item, "channel_id", None),
                getattr(item, "author_id", None),
                getattr(item, "engine", None),
            )
            return False, 0, False

        state = self._get_state(guild_id)
        dropped = 0
        deduplicated = False
        signature = self._queue_signature(item)

        while state.queue.full():
            try:
                dropped_item = state.queue.get_nowait()
                self._decrement_pending_signature(state, dropped_item)
                state.queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        if int(state.pending_signatures.get(signature, 0) or 0) > 0:
            self._record_queue_enqueue(deduplicated=True)
            return False, dropped, True

        await state.queue.put(item)
        self._increment_pending_signature(state, item)
        self._record_queue_enqueue(dropped=dropped, deduplicated=False, queue_depth=state.queue.qsize())
        return True, dropped, deduplicated

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
            value = f"{value}" if value.startswith(("+", "-")) else f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0Hz"
        return f"{sign}{number}Hz"


    def _estimate_playback_timeout(self, item: QueueItem | None = None) -> float:
        text_len = len((getattr(item, "text", "") or "").strip()) if item is not None else 0
        timeout = TTS_PLAYBACK_TIMEOUT_BASE_SECONDS + (min(text_len, 1600) * TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS)
        return max(TTS_PLAYBACK_TIMEOUT_BASE_SECONDS, min(TTS_PLAYBACK_TIMEOUT_MAX_SECONDS, timeout))

    def _normalize_cache_text(self, text: str) -> str:
        text = " ".join((text or "").strip().split())
        text = text.lower()
        text = text.replace("!!", "!").replace("??", "?").replace("..", ".")
        return text

    def _get_item_normalized_cache_text(self, item: QueueItem) -> str:
        cached = getattr(item, "_normalized_cache_text", None)
        if cached is None:
            cached = self._normalize_cache_text(item.text)
            item._normalized_cache_text = cached
        return cached

    def _get_synth_semaphore(self) -> _PrioritySemaphore:
        semaphore = getattr(self, "_tts_synth_semaphore", None)
        if semaphore is None:
            semaphore = _PrioritySemaphore(TTS_SYNTH_CONCURRENCY)
            setattr(self, "_tts_synth_semaphore", semaphore)
        return semaphore

    def _get_edge_prefetch_semaphore(self) -> asyncio.Semaphore:
        semaphore = getattr(self, "_tts_edge_prefetch_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(TTS_EDGE_PREFETCH_CONCURRENCY)
            setattr(self, "_tts_edge_prefetch_semaphore", semaphore)
        return semaphore

    def _get_gtts_semaphore(self) -> _PrioritySemaphore:
        semaphore = getattr(self, "_tts_gtts_semaphore", None)
        if semaphore is None:
            semaphore = _PrioritySemaphore(TTS_GTTS_CONCURRENCY)
            setattr(self, "_tts_gtts_semaphore", semaphore)
        return semaphore

    def _get_gtts_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        executor = getattr(self, "_tts_gtts_executor", None)
        if executor is None:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=TTS_GTTS_CONCURRENCY,
                thread_name_prefix="tts-gtts",
            )
            setattr(self, "_tts_gtts_executor", executor)
        return executor

    def _get_gtts_thread_session(self) -> tuple[requests.Session, dict[str, str]]:
        """Return a requests session owned exclusively by the current worker."""
        local = getattr(self, "_tts_gtts_thread_local", None)
        if local is None:
            local = threading.local()
            setattr(self, "_tts_gtts_thread_local", local)

        now = time.monotonic()
        state = getattr(local, "session_state", None)
        rotate = bool(
            not isinstance(state, dict)
            or not isinstance(state.get("session"), requests.Session)
            or now - float(state.get("created_at", 0.0) or 0.0) >= TTS_GTTS_SESSION_TTL_SECONDS
            or int(state.get("uses", 0) or 0) >= TTS_GTTS_SESSION_MAX_REQUESTS
        )
        if rotate:
            if isinstance(state, dict):
                close = getattr(state.get("session"), "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
            state = {
                "session": requests.Session(),
                "created_at": now,
                "uses": 0,
                "proxies": urllib.request.getproxies(),
            }
            local.session_state = state
        state["uses"] = int(state.get("uses", 0) or 0) + 1
        return state["session"], dict(state.get("proxies") or {})

    def _invalidate_gtts_thread_session(self) -> None:
        local = getattr(self, "_tts_gtts_thread_local", None)
        state = getattr(local, "session_state", None) if local is not None else None
        if isinstance(state, dict):
            close = getattr(state.get("session"), "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        if local is not None:
            local.session_state = None

    def _iter_gtts_audio_chunks(self, tts: Any):
        """Use one keep-alive HTTP session per executor thread.

        gTTS 2.5.4 normally creates a requests.Session for every text part.
        The project pins that version, but this fast path still falls back to
        the public stream() method if the private request builder disappears.
        """
        official_stream = getattr(tts, "stream", None)
        prepare_requests = getattr(tts, "_prepare_requests", None)
        if (
            not TTS_GTTS_PERSISTENT_SESSION_ENABLED
            or not callable(official_stream)
            or not callable(prepare_requests)
        ):
            if not callable(official_stream):
                raise RuntimeError("versão instalada do gTTS não oferece stream()")
            yield from official_stream()
            return

        with contextlib.suppress(Exception):
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
        try:
            prepared_requests = prepare_requests()
        except (AttributeError, TypeError, AssertionError):
            yield from official_stream()
            return

        for prepared_request in prepared_requests:
            response = None
            last_request_error: BaseException | None = None
            for attempt in range(2):
                session, proxies = self._get_gtts_thread_session()
                try:
                    response = session.send(
                        request=prepared_request,
                        verify=False,
                        proxies=proxies,
                        timeout=getattr(tts, "timeout", None),
                    )
                    break
                except requests.exceptions.RequestException as exc:
                    last_request_error = exc
                    self._invalidate_gtts_thread_session()
                    if attempt == 0:
                        continue
            if response is None:
                raise gTTSError(tts=tts) from last_request_error

            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                with contextlib.suppress(Exception):
                    response.close()
                raise gTTSError(tts=tts, response=response) from exc

            audio_found = False
            try:
                for line in response.iter_lines(chunk_size=1024):
                    decoded_line = line.decode("utf-8")
                    if "jQ1olc" not in decoded_line:
                        continue
                    match = _GTTS_AUDIO_LINE_RE.search(decoded_line)
                    if match is None:
                        continue
                    audio_found = True
                    yield base64.b64decode(match.group(1).encode("ascii"))
            except requests.exceptions.RequestException as exc:
                self._invalidate_gtts_thread_session()
                raise gTTSError(tts=tts, response=response) from exc
            finally:
                with contextlib.suppress(Exception):
                    response.close()
            if not audio_found:
                raise gTTSError(tts=tts, response=response)

    def _get_gtts_rate_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_tts_gtts_rate_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_tts_gtts_rate_lock", lock)
        return lock

    def _get_edge_stream_handles(self) -> dict[str, EdgeStreamHandle]:
        handles = getattr(self, "_tts_edge_stream_handles", None)
        if not isinstance(handles, dict):
            handles = {}
            setattr(self, "_tts_edge_stream_handles", handles)
        return handles

    def _get_stream_cache_parts(self) -> set[str]:
        paths = getattr(self, "_tts_stream_cache_parts", None)
        if not isinstance(paths, set):
            paths = set()
            setattr(self, "_tts_stream_cache_parts", paths)
        return paths

    def _get_edge_prebuffer_profiles(self) -> OrderedDict[str, dict[str, int]]:
        profiles = getattr(self, "_tts_edge_prebuffer_profiles", None)
        if not isinstance(profiles, OrderedDict):
            profiles = OrderedDict()
            setattr(self, "_tts_edge_prebuffer_profiles", profiles)
        return profiles

    def _edge_prebuffer_profile_key(self, item: QueueItem) -> str:
        voice = str(getattr(item, "voice", "") or "default").strip().lower()
        rate = self._normalize_edge_rate(getattr(item, "rate", "+0%"))
        match = re.fullmatch(r"([+-])(\d+)%", rate)
        value = 0
        if match:
            value = int(match.group(2)) * (-1 if match.group(1) == "-" else 1)
        bucket = "slow" if value <= -15 else "fast" if value >= 15 else "normal"
        return f"{voice}|{bucket}"

    def _edge_prebuffer_ms(self, item: QueueItem) -> tuple[int, str]:
        key = self._edge_prebuffer_profile_key(item)
        if not TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED:
            return TTS_EDGE_STREAM_PREBUFFER_MS, key
        profiles = self._get_edge_prebuffer_profiles()
        profile = profiles.get(key)
        if profile is None:
            profile = {
                "prebuffer_ms": TTS_EDGE_STREAM_PREBUFFER_MS,
                "stable_streams": 0,
            }
            profiles[key] = profile
        profiles.move_to_end(key)
        while len(profiles) > 32:
            profiles.popitem(last=False)
        return int(profile["prebuffer_ms"]), key

    def _observe_edge_stream_playback(self, handle: EdgeStreamHandle, *, playback_ok: bool) -> None:
        if handle.engine != "edge" or not TTS_EDGE_ADAPTIVE_PREBUFFER_ENABLED:
            return
        profiles = self._get_edge_prebuffer_profiles()
        profile = profiles.get(handle.prebuffer_profile_key)
        if profile is None:
            return

        metrics = self._get_metrics_store()
        if handle.max_source_read_ms > 0:
            self._record_latency_sample("edge_source_read_max", handle.max_source_read_ms)
        if handle.source_read_stalls:
            metrics["edge_source_read_stalls"] = int(metrics.get("edge_source_read_stalls", 0) or 0) + int(handle.source_read_stalls)
        stalled = bool(
            handle.source_read_stalls
            or handle.error is not None
            or handle.pipe_error is not None
        )
        if stalled:
            metrics["edge_stream_starvations"] = int(metrics.get("edge_stream_starvations", 0) or 0) + 1
            previous = int(profile["prebuffer_ms"])
            profile["prebuffer_ms"] = min(TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS, previous + 40)
            profile["stable_streams"] = 0
            if int(profile["prebuffer_ms"]) > previous:
                metrics["edge_prebuffer_raised"] = int(metrics.get("edge_prebuffer_raised", 0) or 0) + 1
        elif playback_ok:
            profile["stable_streams"] = int(profile.get("stable_streams", 0) or 0) + 1
            if int(profile["stable_streams"]) >= TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS:
                previous = int(profile["prebuffer_ms"])
                profile["prebuffer_ms"] = max(TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS, previous - 20)
                profile["stable_streams"] = 0
                if int(profile["prebuffer_ms"]) < previous:
                    metrics["edge_prebuffer_lowered"] = int(metrics.get("edge_prebuffer_lowered", 0) or 0) + 1
        profiles.move_to_end(handle.prebuffer_profile_key)

    def _edge_circuit_is_open(self) -> bool:
        if not TTS_EDGE_CIRCUIT_BREAKER_ENABLED:
            return False
        engine_metrics = self._get_engine_metrics("edge")
        failures = int(engine_metrics.get("consecutive_failures", 0) or 0)
        if failures < TTS_EDGE_CIRCUIT_BREAKER_FAILURES:
            return False
        last_error_at = float(engine_metrics.get("last_error_at", 0.0) or 0.0)
        return bool(
            last_error_at > 0.0
            and (time.time() - last_error_at) < TTS_EDGE_CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )

    def _record_edge_circuit_bypass(self) -> None:
        metrics = self._get_metrics_store()
        metrics["edge_circuit_bypasses"] = int(metrics.get("edge_circuit_bypasses", 0) or 0) + 1

    def _edge_stream_handle_for_path(self, path: str | None) -> EdgeStreamHandle | None:
        if not path:
            return None
        return self._get_edge_stream_handles().get(os.path.abspath(str(path)))

    def _edge_streaming_allowed_for(self, item: QueueItem) -> tuple[bool, str]:
        if not (TTS_EDGE_VPS_FAST_PATH_ENABLED and TTS_EDGE_STREAMING_ENABLED):
            return False, "edge_stream_disabled"
        if str(getattr(item, "engine", "") or "").strip().lower() != "edge":
            return False, "not_edge"
        if os.name != "posix" or not callable(getattr(os, "mkfifo", None)):
            return False, "fifo_unsupported"
        guild_id = int(getattr(item, "guild_id", 0) or 0)
        if guild_id and self._is_music_active_for_guild(guild_id):
            return False, "music_active"

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        lavalink_active = getattr(router, "is_lavalink_active_for_guild", None)
        if guild_id and callable(lavalink_active):
            with contextlib.suppress(Exception):
                if bool(lavalink_active(guild_id)):
                    return False, "lavalink_active"

        guild = None
        get_guild = getattr(getattr(self, "bot", None), "get_guild", None)
        if guild_id and callable(get_guild):
            with contextlib.suppress(Exception):
                guild = get_guild(guild_id)
        get_vc = getattr(self, "_get_voice_client_for_guild", None)
        is_lavalink_vc = getattr(self, "_is_lavalink_voice_client", None)
        if guild is not None and callable(get_vc) and callable(is_lavalink_vc):
            with contextlib.suppress(Exception):
                if bool(is_lavalink_vc(get_vc(guild))):
                    return False, "lavalink_voice_client"
        return True, "allowed"

    def _gtts_streaming_allowed_for(self, item: QueueItem) -> tuple[bool, str]:
        if not TTS_GTTS_STREAMING_ENABLED:
            return False, "gtts_stream_disabled"
        if str(getattr(item, "engine", "") or "").strip().lower() != "gtts":
            return False, "not_gtts"
        if len(self._get_item_normalized_cache_text(item)) < TTS_GTTS_STREAM_MIN_CHARS:
            # Até 100 caracteres o gTTS normalmente faz uma única requisição;
            # o stream público só entrega o MP3 quando essa resposta termina.
            return False, "gtts_single_request"
        route_available = getattr(self, "_tts_agent_route_available", None)
        if callable(route_available):
            with contextlib.suppress(Exception):
                if route_available():
                    if not TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED or "gtts" in TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES:
                        return False, "worker_config_preferred"
                    metrics = self._get_metrics_store()
                    first_audio_samples = int(metrics.get("gtts_stream_first_audio_samples", 0) or 0)
                    local_first_audio_ms = (
                        float(metrics.get("gtts_stream_first_audio_total_ms", 0.0) or 0.0)
                        / first_audio_samples
                        if first_audio_samples > 0
                        else 0.0
                    )
                    worker_total_ms = self._engine_average_ms("tts_agent:gtts")
                    if worker_total_ms <= 0:
                        worker_total_ms = float(self._tts_agent_route_state().get("avg_synth_ms") or 0.0)
                    # Primeiro experimenta o stream local. Depois das amostras,
                    # deixa o worker ganhar quando ele entrega o arquivo inteiro
                    # com vantagem clara sobre o primeiro áudio da VPS.
                    if (
                        local_first_audio_ms > 0
                        and worker_total_ms > 0
                        and (worker_total_ms + TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS) < local_first_audio_ms
                    ):
                        return False, "worker_measured_faster"
        if os.name != "posix" or not callable(getattr(os, "mkfifo", None)):
            return False, "fifo_unsupported"
        guild_id = int(getattr(item, "guild_id", 0) or 0)
        if guild_id and self._is_music_active_for_guild(guild_id):
            return False, "music_active"

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        lavalink_active = getattr(router, "is_lavalink_active_for_guild", None)
        if guild_id and callable(lavalink_active):
            with contextlib.suppress(Exception):
                if bool(lavalink_active(guild_id)):
                    return False, "lavalink_active"

        guild = None
        get_guild = getattr(getattr(self, "bot", None), "get_guild", None)
        if guild_id and callable(get_guild):
            with contextlib.suppress(Exception):
                guild = get_guild(guild_id)
        get_vc = getattr(self, "_get_voice_client_for_guild", None)
        is_lavalink_vc = getattr(self, "_is_lavalink_voice_client", None)
        if guild is not None and callable(get_vc) and callable(is_lavalink_vc):
            with contextlib.suppress(Exception):
                if bool(is_lavalink_vc(get_vc(guild))):
                    return False, "lavalink_voice_client"
        return True, "allowed"

    def _cleanup_stale_edge_stream_files(self) -> None:
        _ensure_tts_temp_dirs()
        active_paths: set[str] = set()
        try:
            active_handles = list(self._get_edge_stream_handles().values())
        except RuntimeError:
            # O warmup pode rodar em thread enquanto o loop registra um novo
            # stream. Nesse caso é mais seguro adiar a limpeza inteira.
            return
        for handle in active_handles:
            if handle.fifo_path:
                active_paths.add(os.path.abspath(handle.fifo_path))
            if handle.part_path:
                active_paths.add(os.path.abspath(handle.part_path))
        try:
            active_paths.update(self._get_stream_cache_parts())
        except RuntimeError:
            return
        try:
            with os.scandir(_RUNTIME_DIR) as iterator:
                entries = list(iterator)
        except FileNotFoundError:
            return
        for entry in entries:
            path = os.path.abspath(entry.path)
            if path in active_paths:
                continue
            is_stream_part = entry.name.endswith(".edge-stream.tmp")
            is_stream_fifo = entry.name.endswith(".edge-stream.mp3")
            if not (is_stream_part or is_stream_fifo):
                continue
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if is_stream_part or stat.S_ISFIFO(mode):
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(path)

    def _make_runtime_unique_path(self, *, suffix: str) -> str:
        _ensure_tts_temp_dirs()
        for _ in range(16):
            path = os.path.join(_RUNTIME_DIR, f"tts_{uuid.uuid4().hex}{suffix}")
            if not os.path.lexists(path):
                return path
        raise RuntimeError("não foi possível reservar um nome temporário TTS")

    def _make_edge_stream_fifo(self) -> str:
        _ensure_tts_temp_dirs()
        for _ in range(16):
            path = self._make_runtime_unique_path(suffix=".edge-stream.mp3")
            try:
                os.mkfifo(path, mode=0o600)
                return path
            except FileExistsError:
                continue
            except Exception:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(path)
                raise
        raise RuntimeError("não foi possível criar FIFO temporário TTS")

    @staticmethod
    def _edge_stream_audio_chunk(message: Any) -> bytes:
        if not isinstance(message, dict) or str(message.get("type") or "").lower() != "audio":
            return b""
        data = message.get("data")
        if isinstance(data, bytes):
            return data
        if isinstance(data, (bytearray, memoryview)):
            return bytes(data)
        return b""

    async def _edge_stream_enqueue(self, handle: EdgeStreamHandle, data: bytes) -> None:
        if not data or handle.consumer_abandoned:
            return
        if len(data) <= TTS_EDGE_STREAM_CHUNK_BYTES:
            await handle.queue.put(data)
            return
        data_view = memoryview(data)
        for offset in range(0, len(data_view), TTS_EDGE_STREAM_CHUNK_BYTES):
            if handle.consumer_abandoned:
                return
            await handle.queue.put(data_view[offset:offset + TTS_EDGE_STREAM_CHUNK_BYTES])

    async def _signal_stream_end(self, handle: EdgeStreamHandle) -> None:
        handle.producer_done.set()
        if handle.consumer_abandoned:
            return
        await handle.queue.put(_TTS_STREAM_END)

    @staticmethod
    def _release_edge_stream_slot(handle: EdgeStreamHandle) -> None:
        # Cancelar um asyncio.Future retornado por run_in_executor não encerra a
        # requisição gTTS que já está rodando na thread. Mantenha a vaga física
        # ocupada até essa thread realmente devolver o controle.
        blocking_future = handle.blocking_future
        if (
            handle.engine == "gtts"
            and not handle.semaphore_released
            and blocking_future is not None
            and not blocking_future.done()
        ):
            if not handle.semaphore_release_deferred:
                handle.semaphore_release_deferred = True

                def _release_after_physical_stop(done_future: asyncio.Future) -> None:
                    handle.semaphore_release_deferred = False
                    if not handle.semaphore_released and handle.semaphore is not None:
                        handle.semaphore_released = True
                        handle.semaphore.release()
                    with contextlib.suppress(BaseException):
                        done_future.exception()

                blocking_future.add_done_callback(_release_after_physical_stop)
            return
        if not handle.semaphore_released and handle.semaphore is not None:
            handle.semaphore_released = True
            handle.semaphore.release()
        if not handle.prefetch_semaphore_released and handle.prefetch_semaphore is not None:
            handle.prefetch_semaphore_released = True
            handle.prefetch_semaphore.release()

    async def _edge_stream_pipe_writer(self, handle: EdgeStreamHandle) -> None:
        fd: int | None = None
        try:
            deadline = time.monotonic() + TTS_EDGE_STREAM_PIPE_OPEN_TIMEOUT_SECONDS
            while not handle.consumer_abandoned:
                try:
                    flags = os.O_WRONLY | os.O_NONBLOCK
                    flags |= getattr(os, "O_CLOEXEC", 0)
                    fd = os.open(handle.fifo_path, flags)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.ENXIO, errno.ENOENT} or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.001)

            if fd is None or handle.consumer_abandoned:
                return
            if fcntl is not None and hasattr(fcntl, "F_SETPIPE_SZ"):
                with contextlib.suppress(OSError, ValueError):
                    current_pipe_bytes = (
                        fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
                        if hasattr(fcntl, "F_GETPIPE_SZ")
                        else 0
                    )
                    if current_pipe_bytes < TTS_EDGE_STREAM_PIPE_BYTES:
                        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, TTS_EDGE_STREAM_PIPE_BYTES)
            handle.writer_ready.set()

            while not handle.consumer_abandoned:
                try:
                    chunk = handle.queue.get_nowait()
                except asyncio.QueueEmpty:
                    if handle.producer_done.is_set():
                        break
                    chunk = await handle.queue.get()

                if chunk is _TTS_STREAM_END:
                    handle.queue.task_done()
                    break

                view = memoryview(chunk)
                try:
                    while view and not handle.consumer_abandoned:
                        try:
                            written = os.write(fd, view)
                            view = view[written:]
                        except BlockingIOError:
                            await asyncio.sleep(0.002)
                finally:
                    handle.queue.task_done()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            handle.pipe_error = exc
            handle.consumer_abandoned = True
            producer_task = handle.producer_task
            if producer_task is not None and not producer_task.done():
                producer_task.cancel()
            self._log_debug(
                f"[tts_edge_stream] consumidor encerrou | guild={handle.item.guild_id} erro={type(exc).__name__}: {exc}"
            )
        finally:
            handle.writer_ready.set()
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    @staticmethod
    def _close_edge_stream_reader_anchor(handle: EdgeStreamHandle) -> None:
        with handle.reader_anchor_lock:
            fd = handle.reader_anchor_fd
            handle.reader_anchor_fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    async def _activate_edge_stream(self, handle: EdgeStreamHandle) -> None:
        if handle.activated:
            return
        # O writer abre somente quando o leitor real do FFmpeg existe. Uma
        # âncora O_RDWR parecia antecipar o handoff, mas mantinha artificialmente
        # um writer vivo e podia impedir o EOF/primeiro PCM em áudios curtos.
        handle.activated = True
        handle.writer_task = asyncio.create_task(self._edge_stream_pipe_writer(handle))
        await asyncio.sleep(0)
        if handle.writer_task.done() and handle.pipe_error is not None:
            raise RuntimeError("escritor FIFO do TTS não iniciou") from handle.pipe_error

    async def _finalize_edge_stream(self, handle: EdgeStreamHandle, *, cancel: bool = False) -> None:
        if handle.cleaned:
            return
        if cancel:
            handle.consumer_abandoned = True
            handle.stop_requested.set()
            blocking_future = handle.blocking_future
            if (
                handle.engine != "gtts"
                and blocking_future is not None
                and not blocking_future.done()
            ):
                blocking_future.cancel()
            for task in (handle.writer_task, handle.producer_task):
                if task is not None and not task.done():
                    task.cancel()

        tasks = [task for task in (handle.writer_task, handle.producer_task) if task is not None]
        if tasks:
            with contextlib.suppress(BaseException):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._release_edge_stream_slot(handle)
        self._close_edge_stream_reader_anchor(handle)

        handle.cleaned = True
        if handle.fifo_path:
            self._get_edge_stream_handles().pop(os.path.abspath(handle.fifo_path), None)
            with contextlib.suppress(FileNotFoundError, OSError):
                os.remove(handle.fifo_path)
        if handle.part_path and (handle.cache_task is None or handle.cache_task.done()):
            with contextlib.suppress(FileNotFoundError, OSError):
                os.remove(handle.part_path)

    async def _discard_edge_stream_path(self, path: str | None) -> None:
        handle = self._edge_stream_handle_for_path(path)
        if handle is not None:
            await self._finalize_edge_stream(handle, cancel=True)
            return
        if path:
            with contextlib.suppress(FileNotFoundError, OSError):
                os.remove(path)

    def _cancel_edge_streams(self) -> None:
        handles = list(self._get_edge_stream_handles().values())
        self._get_edge_stream_handles().clear()
        for handle in handles:
            handle.consumer_abandoned = True
            handle.stop_requested.set()
            handle.cleaned = True
            blocking_future = handle.blocking_future
            if (
                handle.engine != "gtts"
                and blocking_future is not None
                and not blocking_future.done()
            ):
                blocking_future.cancel()
            for task in (handle.writer_task, handle.producer_task):
                if task is not None and not task.done():
                    task.cancel()
            self._release_edge_stream_slot(handle)
            self._close_edge_stream_reader_anchor(handle)
            if handle.fifo_path:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(handle.fifo_path)
            if handle.part_path and (handle.cache_task is None or handle.cache_task.done()):
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(handle.part_path)

    async def _get_phone_worker_http_session(self) -> aiohttp.ClientSession:
        session = getattr(self, "_phone_worker_http_session", None)
        if session is None or getattr(session, "closed", True):
            connector = aiohttp.TCPConnector(
                limit=8,
                ttl_dns_cache=300,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None))
            setattr(self, "_phone_worker_http_session", session)
        return session

    def _close_phone_worker_http_session(self) -> None:
        session = getattr(self, "_phone_worker_http_session", None)
        if session is None or getattr(session, "closed", True):
            return
        setattr(self, "_phone_worker_http_session", None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(session.close())

    @staticmethod
    def _parse_header_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "sim", "on"}

    @staticmethod
    def _parse_header_float(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None


    def _tts_agent_base_configured(self) -> bool:
        return bool(TTS_WORKER_AGENT_ENABLED and PHONE_WORKER_ENABLED and PHONE_WORKER_HOST and PHONE_WORKER_TOKEN)

    def _tts_agent_route_state(self) -> dict[str, Any]:
        state = getattr(self, "_tts_agent_route", None)
        if not isinstance(state, dict):
            state = {
                "route": "vps",
                "ok": False,
                "enabled": bool(TTS_WORKER_AGENT_ENABLED),
                "reason": "not_checked",
                "worker_id": "",
                "worker_version": "",
                "engine": "",
                "available_engines": [],
                "last_ok_monotonic": 0.0,
                "last_check_monotonic": 0.0,
                "disabled_until_monotonic": 0.0,
                "failure_count": 0,
                "last_error": "",
                "queue_active": 0,
                "queue_limit": 0,
                "avg_synth_ms": 0.0,
                "last_requested_engine": "",
                "last_selected_engine": "",
                "last_audio_format": "",
                "last_audio_bytes": 0,
                "last_cache_hit": False,
                "last_synth_ms": 0.0,
                "voice_agent": {},
            }
            setattr(self, "_tts_agent_route", state)
        return state

    def _tts_agent_public_snapshot(self) -> dict[str, Any]:
        state = dict(self._tts_agent_route_state())
        now = time.monotonic()
        last_ok = float(state.get("last_ok_monotonic") or 0.0)
        last_check = float(state.get("last_check_monotonic") or 0.0)
        disabled_until = float(state.get("disabled_until_monotonic") or 0.0)
        return {
            "enabled": bool(state.get("enabled")),
            "route": str(state.get("route") or "vps"),
            "ok": bool(state.get("ok")),
            "reason": str(state.get("reason") or ""),
            "worker_id": str(state.get("worker_id") or ""),
            "worker_version": str(state.get("worker_version") or ""),
            "engine": str(state.get("engine") or ""),
            "available_engines": list(state.get("available_engines") or [])[:8],
            "last_ok_age_seconds": round(now - last_ok, 1) if last_ok else None,
            "last_check_age_seconds": round(now - last_check, 1) if last_check else None,
            "cooldown_remaining_seconds": round(max(0.0, disabled_until - now), 1),
            "failure_count": int(state.get("failure_count") or 0),
            "last_error": str(state.get("last_error") or "")[:180],
            "queue_active": int(state.get("queue_active") or 0),
            "queue_limit": int(state.get("queue_limit") or 0),
            "avg_synth_ms": float(state.get("avg_synth_ms") or 0.0),
            "last_requested_engine": str(state.get("last_requested_engine") or ""),
            "last_selected_engine": str(state.get("last_selected_engine") or state.get("engine") or ""),
            "last_audio_format": str(state.get("last_audio_format") or ""),
            "last_audio_bytes": int(state.get("last_audio_bytes") or 0),
            "last_cache_hit": bool(state.get("last_cache_hit")),
            "last_synth_ms": float(state.get("last_synth_ms") or 0.0),
            "voice_agent": dict(state.get("voice_agent") or {}),
        }

    def _tts_agent_set_route(
        self,
        *,
        route: str,
        ok: bool,
        reason: str,
        worker_id: str = "",
        worker_version: str = "",
        engine: str = "",
        available_engines: list[Any] | None = None,
        last_error: str = "",
        queue_active: int | None = None,
        queue_limit: int | None = None,
        avg_synth_ms: float | None = None,
        last_requested_engine: str | None = None,
        last_selected_engine: str | None = None,
        last_audio_format: str | None = None,
        last_audio_bytes: int | None = None,
        last_cache_hit: bool | None = None,
        last_synth_ms: float | None = None,
        reset_failures: bool = False,
    ) -> None:
        state = self._tts_agent_route_state()
        now = time.monotonic()
        state.update({
            "route": route if route in {"worker", "vps"} else "vps",
            "ok": bool(ok),
            "enabled": bool(TTS_WORKER_AGENT_ENABLED),
            "reason": str(reason or "unknown")[:160],
            "worker_id": str(worker_id or state.get("worker_id") or "")[:120],
            "worker_version": str(worker_version or state.get("worker_version") or "")[:80],
            "engine": str(engine or state.get("engine") or "")[:80],
            "available_engines": [str(x)[:40] for x in (available_engines if available_engines is not None else state.get("available_engines") or [])][:8],
            "last_check_monotonic": now,
            "last_error": str(last_error or "")[:220],
        })
        if ok:
            state["last_ok_monotonic"] = now
        if queue_active is not None:
            state["queue_active"] = int(queue_active)
        if queue_limit is not None:
            state["queue_limit"] = int(queue_limit)
        if avg_synth_ms is not None:
            state["avg_synth_ms"] = round(float(avg_synth_ms or 0.0), 2)
        if last_requested_engine is not None:
            state["last_requested_engine"] = str(last_requested_engine or "")[:80]
        if last_selected_engine is not None:
            state["last_selected_engine"] = str(last_selected_engine or "")[:80]
        if last_audio_format is not None:
            state["last_audio_format"] = str(last_audio_format or "")[:24]
        if last_audio_bytes is not None:
            state["last_audio_bytes"] = max(0, int(last_audio_bytes or 0))
        if last_cache_hit is not None:
            state["last_cache_hit"] = bool(last_cache_hit)
        if last_synth_ms is not None:
            state["last_synth_ms"] = round(float(last_synth_ms or 0.0), 2)
        if reset_failures:
            state["failure_count"] = 0
            state["disabled_until_monotonic"] = 0.0

    def _tts_agent_route_available(self) -> bool:
        if not self._tts_agent_base_configured():
            return False
        state = self._tts_agent_route_state()
        now = time.monotonic()
        if now < float(state.get("disabled_until_monotonic") or 0.0):
            return False
        if state.get("route") != "worker" or not bool(state.get("ok")):
            return False
        last_ok = float(state.get("last_ok_monotonic") or 0.0)
        if not last_ok or now - last_ok > TTS_WORKER_AGENT_STALE_SECONDS:
            return False
        return True

    def _record_tts_agent_route_sample(self, worker: bool) -> None:
        metrics = self._get_metrics_store()
        key = "tts_agent_route_worker_samples" if worker else "tts_agent_route_vps_samples"
        metrics[key] = int(metrics.get(key, 0) or 0) + 1

    def _mark_tts_agent_synth_failure(self, exc: Exception | str) -> None:
        state = self._tts_agent_route_state()
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
        state["last_error"] = str(exc)[:220]
        state["reason"] = "synth_failed"
        metrics = self._get_metrics_store()
        metrics["tts_agent_synth_failed"] = int(metrics.get("tts_agent_synth_failed", 0) or 0) + 1
        metrics["tts_agent_last_failure_reason"] = str(exc)[:220]
        if int(state.get("failure_count") or 0) >= TTS_WORKER_AGENT_FAILURE_THRESHOLD:
            state["route"] = "vps"
            state["ok"] = False
            state["disabled_until_monotonic"] = time.monotonic() + TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "[tts_agent] rota worker suspensa temporariamente; failures=%s cooldown=%.1fs erro=%s",
                state.get("failure_count"),
                TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS,
                exc,
            )

    def _record_tts_agent_synth_success(self, *, total_ms: float, data: dict[str, Any]) -> None:
        metrics = self._get_metrics_store()
        metrics["tts_agent_synth_ok"] = int(metrics.get("tts_agent_synth_ok", 0) or 0) + 1
        self._record_average_metric("tts_agent_synth_total_ms", "tts_agent_synth_samples", float(total_ms))
        requested_engine = str(data.get("requested_engine") or data.get("requested") or "").strip().lower()
        selected_engine = str(data.get("selected_engine") or data.get("engine") or "").strip().lower()
        audio_format = str(data.get("audio_format") or "").strip().lower()
        audio_bytes = int(data.get("audio_bytes_len") or 0)
        metrics["tts_agent_last_requested_engine"] = requested_engine
        metrics["tts_agent_last_selected_engine"] = selected_engine
        metrics["tts_agent_last_audio_format"] = audio_format
        metrics["tts_agent_last_audio_bytes"] = audio_bytes
        metrics["tts_agent_last_cache_hit"] = bool(data.get("cache_hit"))
        metrics["tts_agent_last_synth_ms"] = round(float(total_ms or 0.0), 2)
        state = self._tts_agent_route_state()
        state["failure_count"] = 0
        state["disabled_until_monotonic"] = 0.0
        metrics["tts_agent_last_failure_reason"] = ""
        self._tts_agent_set_route(
            route="worker",
            ok=True,
            reason="synth_ok",
            worker_id=str(data.get("worker_id") or state.get("worker_id") or ""),
            worker_version=str(data.get("worker_version") or state.get("worker_version") or ""),
            engine=str(selected_engine or state.get("engine") or ""),
            available_engines=[e for e in list(data.get("available_engines") or state.get("available_engines") or []) if str(e).strip().lower().replace("-", "_") not in {"gcloud", "google", "google_cloud", "googlecloud", "google_tts"}],
            avg_synth_ms=float(total_ms),
            last_requested_engine=requested_engine,
            last_selected_engine=selected_engine,
            last_audio_format=audio_format,
            last_audio_bytes=audio_bytes,
            last_cache_hit=bool(data.get("cache_hit")),
            last_synth_ms=float(total_ms),
            reset_failures=True,
        )

    def _tts_agent_note_transient_health_failure(self, *, reason: str, last_error: str) -> bool:
        """Keep a recently healthy worker route alive through a few probe glitches."""
        state = self._tts_agent_route_state()
        now = time.monotonic()
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
        state["last_check_monotonic"] = now
        state["last_error"] = str(last_error or reason or "health_error")[:220]
        last_ok = float(state.get("last_ok_monotonic") or 0.0)
        recently_ok = bool(last_ok and (now - last_ok) <= TTS_WORKER_AGENT_STALE_SECONDS)
        if bool(state.get("ok")) and state.get("route") == "worker" and recently_ok and int(state.get("failure_count") or 0) < TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD:
            state["reason"] = str(f"{reason}_degraded")[:160]
            return True
        return False

    async def _fetch_tts_agent_light_health(self, *, base: str, headers: dict[str, str]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS)
        session = await self._get_phone_worker_http_session()
        # Endpoint leve novo: não chama _system_status(), então não trava a rota TTS
        # por causa de music/boot/scripts/diagnósticos pesados do worker.
        try:
            async with session.get(f"{base}/tts-agent/health", headers=headers, timeout=timeout) as response:
                text = await response.text()
                if response.status == 404:
                    raise RuntimeError("light_health_404")
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {text[:180]}")
                return json.loads(text or "{}")
        except RuntimeError as exc:
            if "light_health_404" not in str(exc):
                raise
        # Compatibilidade com worker antigo: ainda é mais leve que /health completo.
        data = await self._request_phone_worker_json(
            task="tts_agent_status",
            payload={},
            timeout_seconds=TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS,
            max_audio_mb=1,
            raise_on_worker_error=False,
        )
        return data

    async def _probe_tts_agent_health_once(self) -> None:
        metrics = self._get_metrics_store()
        if not self._tts_agent_base_configured():
            self._tts_agent_set_route(route="vps", ok=False, reason="disabled_or_unconfigured")
            return
        base = self._phone_worker_tts_base_url()
        if not base:
            self._tts_agent_set_route(route="vps", ok=False, reason="worker_base_unavailable")
            return
        headers = {"Authorization": f"Bearer {PHONE_WORKER_TOKEN}", "Accept": "application/json"}
        started = time.monotonic()
        try:
            data = await self._fetch_tts_agent_light_health(base=base, headers=headers)
            agent = data.get("tts_agent") if isinstance(data, dict) else None
            if not isinstance(agent, dict) and isinstance(data, dict):
                # /task tts_agent_status devolve o snapshot direto.
                agent = data
            voice_agent = data.get("voice_agent") if isinstance(data, dict) else None
            if not isinstance(agent, dict):
                agent = {}
            if not isinstance(voice_agent, dict):
                voice_agent = {}
            if voice_agent:
                self._update_worker_voice_agent_snapshot(voice_agent)
            ok = bool(data.get("ok", True) and agent.get("ok") and agent.get("available") and agent.get("synth_ready"))
            if ok:
                metrics["tts_agent_health_ok"] = int(metrics.get("tts_agent_health_ok", 0) or 0) + 1
                self._tts_agent_set_route(
                    route="worker",
                    ok=True,
                    reason=str(agent.get("state") or "health_ok"),
                    worker_id=str(data.get("worker_id") or agent.get("worker_id") or ""),
                    worker_version=str(data.get("version") or data.get("worker_version") or agent.get("worker_version") or ""),
                    engine=str(agent.get("preferred_engine") or agent.get("engine") or ""),
                    available_engines=[e for e in list(agent.get("available_engines") or []) if str(e).strip().lower().replace("-", "_") not in {"gcloud", "google", "google_cloud", "googlecloud", "google_tts"}],
                    queue_active=int(agent.get("active") or 0),
                    queue_limit=int(agent.get("concurrency_limit") or 0),
                    avg_synth_ms=float(agent.get("avg_synth_ms") or 0.0),
                    reset_failures=True,
                )
            else:
                metrics["tts_agent_health_fail"] = int(metrics.get("tts_agent_health_fail", 0) or 0) + 1
                reason = str(agent.get("reason") or agent.get("state") or "tts_agent_not_ready")
                self._tts_agent_set_route(route="vps", ok=False, reason=reason, last_error=reason)
        except Exception as exc:
            metrics["tts_agent_health_fail"] = int(metrics.get("tts_agent_health_fail", 0) or 0) + 1
            error = f"{type(exc).__name__}: {exc}"
            kept = self._tts_agent_note_transient_health_failure(reason="health_error", last_error=error)
            if not kept:
                self._tts_agent_set_route(route="vps", ok=False, reason="health_error", last_error=error)
            self._log_debug(f"[tts_agent] health leve falhou após {(time.monotonic()-started)*1000.0:.1f}ms: {exc}; kept_worker={kept}")

    async def _tts_agent_health_loop(self) -> None:
        try:
            while True:
                await self._probe_tts_agent_health_once()
                await asyncio.sleep(TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[tts_agent] health loop encerrou inesperadamente")

    def _ensure_tts_agent_health_task(self) -> None:
        if not self._tts_agent_base_configured():
            self._tts_agent_set_route(route="vps", ok=False, reason="disabled_or_unconfigured")
            return
        task = getattr(self, "_tts_agent_health_task", None)
        if task is None or task.done():
            self._tts_agent_health_task = asyncio.create_task(self._tts_agent_health_loop())

    def _cancel_tts_agent_health_task(self) -> None:
        task = getattr(self, "_tts_agent_health_task", None)
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _tts_chunk_size(text: str, engine: str) -> int:
        if engine == "edge":
            return len(html.escape(str(text or ""), quote=False).encode("utf-8"))
        return len(str(text or ""))

    def _split_tts_text_chunks(self, text: str, *, engine: str = "") -> list[str]:
        text = " ".join((text or "").strip().split())
        if not text:
            return []
        normalized_engine = str(engine or "").strip().lower().replace("-", "_")
        chunk_limit = TTS_EDGE_LONG_TEXT_CHUNK_MAX_BYTES if normalized_engine == "edge" else TTS_LONG_TEXT_CHUNK_MAX_CHARS
        measure = lambda value: self._tts_chunk_size(value, normalized_engine)
        if not TTS_LONG_TEXT_CHUNK_ENABLED or measure(text) <= chunk_limit:
            return [text]
        chunks: list[str] = []
        current = ""
        parts = []
        # Keep punctuation with the sentence when possible.
        start = 0
        for idx, ch in enumerate(text):
            if ch in ".!?;:" and idx + 1 < len(text) and text[idx + 1].isspace():
                parts.append(text[start:idx + 1].strip())
                start = idx + 1
        tail = text[start:].strip()
        if tail:
            parts.append(tail)
        if not parts:
            parts = text.split(" ")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            while measure(part) > chunk_limit:
                low, high = 1, len(part)
                while low < high:
                    middle = (low + high + 1) // 2
                    if measure(part[:middle]) <= chunk_limit:
                        low = middle
                    else:
                        high = middle - 1
                max_cut = max(1, low)
                cut = part.rfind(" ", 0, max_cut + 1)
                if cut < 120:
                    cut = max_cut
                piece = part[:cut].strip()
                part = part[cut:].strip()
                if piece:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.append(piece)
            if not current:
                current = part
            elif measure(f"{current} {part}") <= chunk_limit:
                current = f"{current} {part}"
            else:
                chunks.append(current)
                current = part
            if len(chunks) >= TTS_LONG_TEXT_CHUNK_MAX_PARTS:
                break
        if current and len(chunks) < TTS_LONG_TEXT_CHUNK_MAX_PARTS:
            chunks.append(current)
        if len(chunks) >= TTS_LONG_TEXT_CHUNK_MAX_PARTS:
            consumed = sum(len(x) for x in chunks)
            if consumed < len(text):
                chunks[-1] = chunks[-1].rstrip() + "…"
        if chunks:
            return chunks
        if normalized_engine != "edge":
            return [text[:TTS_LONG_TEXT_CHUNK_MAX_CHARS]]
        low, high = 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(text[:middle]) <= chunk_limit:
                low = middle
            else:
                high = middle - 1
        return [text[:low]]

    def _expand_tts_queue_item(self, item: QueueItem) -> list[QueueItem]:
        chunks = self._split_tts_text_chunks(item.text, engine=item.engine)
        if len(chunks) <= 1:
            return [item]
        expanded: list[QueueItem] = []
        for index, chunk in enumerate(chunks, start=1):
            clone = QueueItem(
                guild_id=item.guild_id,
                channel_id=item.channel_id,
                author_id=item.author_id,
                text=chunk,
                engine=item.engine,
                voice=item.voice,
                language=item.language,
                rate=item.rate,
                pitch=item.pitch,
                piper_fallback_engine=item.piper_fallback_engine,
                piper_fallback_voice=item.piper_fallback_voice,
                piper_fallback_language=item.piper_fallback_language,
                piper_fallback_rate=item.piper_fallback_rate,
                piper_fallback_pitch=item.piper_fallback_pitch,
                piper_model=item.piper_model,
            )
            setattr(clone, "_chunk_index", index)
            setattr(clone, "_chunk_total", len(chunks))
            expanded.append(clone)
        return expanded

    def _get_global_cache_order(self) -> OrderedDict[str, float]:
        cache_order = getattr(self, "_tts_cache_order", None)
        if cache_order is None:
            cache_order = OrderedDict()
            setattr(self, "_tts_cache_order", cache_order)
        return cache_order

    def _get_global_cache_paths(self) -> dict[str, str]:
        cache_paths = getattr(self, "_tts_cache_paths", None)
        if cache_paths is None:
            cache_paths = {}
            setattr(self, "_tts_cache_paths", cache_paths)
        return cache_paths

    def _get_worker_cache_store_tasks(self) -> set[asyncio.Task]:
        tasks = getattr(self, "_tts_worker_cache_store_tasks", None)
        if tasks is None:
            tasks = set()
            setattr(self, "_tts_worker_cache_store_tasks", tasks)
        return tasks

    def _get_worker_cache_store_semaphore(self) -> asyncio.Semaphore:
        semaphore = getattr(self, "_tts_worker_cache_store_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY)
            setattr(self, "_tts_worker_cache_store_semaphore", semaphore)
        return semaphore

    def _get_long_text_repeat_counts(self) -> dict[str, int]:
        counts = getattr(self, "_tts_long_text_repeat_counts", None)
        if counts is None:
            counts = {}
            setattr(self, "_tts_long_text_repeat_counts", counts)
        return counts

    def _remember_long_text_repeat(self, key: str) -> int:
        counts = self._get_long_text_repeat_counts()
        seen_count = int(counts.get(key, 0) or 0) + 1
        counts[key] = seen_count

        max_entries = max(TTS_AUDIO_CACHE_SIZE * 8, 256)
        if len(counts) > max_entries:
            overflow = len(counts) - max_entries
            for stale_key in list(counts.keys())[:overflow]:
                counts.pop(stale_key, None)

        return seen_count

    def _get_inflight_cache_tasks(self) -> dict[str, asyncio.Task]:
        tasks = getattr(self, "_tts_inflight_cache_tasks", None)
        if tasks is None:
            tasks = {}
            setattr(self, "_tts_inflight_cache_tasks", tasks)
        return tasks
    def _get_cache_frequency_map(self) -> dict[str, int]:
        frequencies = getattr(self, "_tts_cache_frequency", None)
        if frequencies is None:
            frequencies = {}
            setattr(self, "_tts_cache_frequency", frequencies)
        return frequencies

    def _get_metrics_store(self) -> dict[str, object]:
        metrics = getattr(self, "_tts_metrics", None)
        if metrics is None:
            metrics = {
                "queue_enqueued": 0,
                "queue_deduplicated": 0,
                "queue_dropped": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "cache_stores": 0,
                "edge_stream_started": 0,
                "edge_stream_completed": 0,
                "edge_stream_failures": 0,
                "edge_stream_fallbacks": 0,
                "edge_stream_first_audio_total_ms": 0.0,
                "edge_stream_first_audio_samples": 0,
                "edge_stream_audio_bytes": 0,
                "edge_slot_wait_total_ms": 0.0,
                "edge_slot_wait_samples": 0,
                "edge_network_first_audio_total_ms": 0.0,
                "edge_network_first_audio_samples": 0,
                "edge_local_handoff_total_ms": 0.0,
                "edge_local_handoff_samples": 0,
                "edge_prebuffer_fill_total_ms": 0.0,
                "edge_prebuffer_fill_samples": 0,
                "edge_first_chunk_bytes_total": 0,
                "edge_first_chunk_bytes_samples": 0,
                "edge_stream_starvations": 0,
                "edge_source_read_stalls": 0,
                "edge_prebuffer_raised": 0,
                "edge_prebuffer_lowered": 0,
                "edge_circuit_bypasses": 0,
                "gtts_stream_started": 0,
                "gtts_stream_completed": 0,
                "gtts_stream_failures": 0,
                "gtts_stream_fallbacks": 0,
                "gtts_stream_first_audio_total_ms": 0.0,
                "gtts_stream_first_audio_samples": 0,
                "gtts_stream_audio_bytes": 0,
                "queue_wait_total_ms": 0.0,
                "queue_wait_samples": 0,
                "dispatch_total_ms": 0.0,
                "dispatch_samples": 0,
                "source_setup_total_ms": 0.0,
                "source_setup_samples": 0,
                "play_call_total_ms": 0.0,
                "play_call_samples": 0,
                "first_frame_unobserved": 0,
                "playback_total_ms": 0.0,
                "playback_samples": 0,
                "total_to_playback_total_ms": 0.0,
                "total_to_playback_samples": 0,
                "queue_depth_total": 0,
                "queue_depth_samples": 0,
                "queue_depth_max": 0,
                "prefetch_started": 0,
                "prefetch_promoted": 0,
                "prefetch_waiter_promoted": 0,
                "worker_cache_lookup_hits": 0,
                "worker_cache_lookup_misses": 0,
                "worker_cache_lookup_skipped": 0,
                "worker_cache_lookup_errors": 0,
                "worker_cache_store_ok": 0,
                "worker_cache_store_failed": 0,
                "worker_cache_hit_total_ms": 0.0,
                "worker_cache_hit_samples": 0,
                "tts_agent_health_ok": 0,
                "tts_agent_health_fail": 0,
                "tts_agent_synth_attempts": 0,
                "tts_agent_synth_ok": 0,
                "tts_agent_synth_failed": 0,
                "tts_agent_busy_retries": 0,
                "tts_agent_last_failure_reason": "",
                "tts_agent_synth_total_ms": 0.0,
                "tts_agent_synth_samples": 0,
                "tts_agent_route_worker_samples": 0,
                "tts_agent_route_vps_samples": 0,
                "worker_voice_agent": {},
                "worker_voice_session_reports_ok": 0,
                "worker_voice_session_reports_failed": 0,
                "worker_voice_session_skipped": 0,
                "worker_voice_session_handoff_ok": 0,
                "worker_voice_session_handoff_failed": 0,
                "worker_voice_session_handoff_skipped": 0,
                "worker_voice_session_connection_probe_ok": 0,
                "worker_voice_session_connection_probe_failed": 0,
                "worker_voice_session_connection_probe_skipped": 0,
                "worker_voice_session_transfer_prepare_ok": 0,
                "worker_voice_session_transfer_prepare_failed": 0,
                "worker_voice_session_transfer_prepare_skipped": 0,
                "worker_voice_session_clears_ok": 0,
                "worker_voice_session_clears_failed": 0,
                "message_gate_seen": 0,
                "message_gate_matched": 0,
                "message_gate_ignored": 0,
                "last_message_gate_reason": "",
                "last_message_gate_guild_id": 0,
                "last_message_gate_channel_id": 0,
                "last_message_gate_author_id": 0,
                "last_message_gate_seen_at": None,
                "boot_warmups": 0,
                "last_warmup_started_at": None,
                "last_warmup_completed_at": None,
                "last_warmup_duration_ms": None,
                "engines": {},
            }
            setattr(self, "_tts_metrics", metrics)
        return metrics

    def _get_engine_metrics(self, engine: str) -> dict[str, object]:
        engine = (engine or "gtts").strip().lower()
        metrics = self._get_metrics_store()
        engines = metrics.setdefault("engines", {})
        if engine not in engines:
            engines[engine] = {
                "synth_count": 0,
                "synth_failures": 0,
                "slow_alerts": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "synth_total_ms": 0.0,
                "last_synth_ms": None,
                "last_error": None,
                "last_error_at": None,
                "consecutive_failures": 0,
            }
        return engines[engine]

    def _record_average_metric(self, total_key: str, samples_key: str, value_ms: float) -> None:
        metrics = self._get_metrics_store()
        metrics[total_key] = float(metrics.get(total_key, 0.0) or 0.0) + float(value_ms)
        metrics[samples_key] = int(metrics.get(samples_key, 0) or 0) + 1

    def _record_latency_sample(self, name: str, value_ms: float | None) -> None:
        if value_ms is None:
            return
        try:
            value = max(0.0, float(value_ms))
        except (TypeError, ValueError):
            return
        samples = getattr(self, "_tts_latency_samples", None)
        if not isinstance(samples, dict):
            samples = {}
            setattr(self, "_tts_latency_samples", samples)
        bucket = samples.get(name)
        if not isinstance(bucket, deque):
            bucket = deque(maxlen=TTS_LATENCY_SAMPLE_WINDOW)
            samples[name] = bucket
        bucket.append(value)

    @staticmethod
    def _latency_percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(value) for value in values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        fraction = position - lower
        return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)

    def _latency_percentiles_snapshot(self) -> dict[str, dict[str, float | int]]:
        samples = getattr(self, "_tts_latency_samples", None)
        if not isinstance(samples, dict):
            return {}
        result: dict[str, dict[str, float | int]] = {}
        for name, bucket in samples.items():
            values = list(bucket or ())
            if not values:
                continue
            result[str(name)] = {
                "samples": len(values),
                "p50": round(self._latency_percentile(values, 0.50), 2),
                "p95": round(self._latency_percentile(values, 0.95), 2),
                "p99": round(self._latency_percentile(values, 0.99), 2),
            }
        return result

    def _queue_signature(self, item: QueueItem) -> str:
        cached = getattr(item, "_dedup_signature", None)
        if cached is not None:
            return cached
        cached = f"{int(item.channel_id)}|{self._cache_key(item)}"
        item._dedup_signature = cached
        return cached

    def _increment_pending_signature(self, state: GuildTTSState, item: QueueItem) -> None:
        signature = self._queue_signature(item)
        state.pending_signatures[signature] = int(state.pending_signatures.get(signature, 0) or 0) + 1

    def _decrement_pending_signature(self, state: GuildTTSState, item: QueueItem) -> None:
        signature = self._queue_signature(item)
        count = int(state.pending_signatures.get(signature, 0) or 0)
        if count <= 1:
            state.pending_signatures.pop(signature, None)
        else:
            state.pending_signatures[signature] = count - 1

    def _record_queue_enqueue(self, *, dropped: int = 0, deduplicated: bool = False, queue_depth: int | None = None) -> None:
        metrics = self._get_metrics_store()
        if deduplicated:
            metrics["queue_deduplicated"] = int(metrics.get("queue_deduplicated", 0) or 0) + 1
            return
        metrics["queue_enqueued"] = int(metrics.get("queue_enqueued", 0) or 0) + 1
        if dropped:
            metrics["queue_dropped"] = int(metrics.get("queue_dropped", 0) or 0) + int(dropped)
        if queue_depth is not None:
            queue_depth = max(0, int(queue_depth))
            metrics["queue_depth_total"] = int(metrics.get("queue_depth_total", 0) or 0) + queue_depth
            metrics["queue_depth_samples"] = int(metrics.get("queue_depth_samples", 0) or 0) + 1
            metrics["queue_depth_max"] = max(int(metrics.get("queue_depth_max", 0) or 0), queue_depth)

    def _record_prefetch_started(self) -> None:
        metrics = self._get_metrics_store()
        metrics["prefetch_started"] = int(metrics.get("prefetch_started", 0) or 0) + 1

    def _record_cache_hit(self, engine: str) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_hits"] = int(metrics.get("cache_hits", 0) or 0) + 1
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["cache_hits"] = int(engine_metrics.get("cache_hits", 0) or 0) + 1

    def _record_cache_miss(self, engine: str) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_misses"] = int(metrics.get("cache_misses", 0) or 0) + 1
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["cache_misses"] = int(engine_metrics.get("cache_misses", 0) or 0) + 1

    def _record_cache_store(self) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_stores"] = int(metrics.get("cache_stores", 0) or 0) + 1

    def _record_worker_cache_lookup(self, status: str, *, total_ms: float | None = None) -> None:
        metrics = self._get_metrics_store()
        key_map = {
            "hit": "worker_cache_lookup_hits",
            "miss": "worker_cache_lookup_misses",
            "skip": "worker_cache_lookup_skipped",
            "error": "worker_cache_lookup_errors",
        }
        metric_key = key_map.get(str(status or "").strip().lower())
        if metric_key:
            metrics[metric_key] = int(metrics.get(metric_key, 0) or 0) + 1
        if status == "hit" and total_ms is not None:
            self._record_average_metric("worker_cache_hit_total_ms", "worker_cache_hit_samples", float(total_ms))

    def _record_worker_cache_store(self, ok: bool) -> None:
        metrics = self._get_metrics_store()
        key = "worker_cache_store_ok" if ok else "worker_cache_store_failed"
        metrics[key] = int(metrics.get(key, 0) or 0) + 1

    def _get_worker_cache_index(self) -> OrderedDict[str, dict[str, Any]]:
        index = getattr(self, "_tts_worker_cache_index", None)
        if index is None:
            index = OrderedDict()
            setattr(self, "_tts_worker_cache_index", index)
        return index

    def _prune_worker_cache_index(self) -> None:
        index = self._get_worker_cache_index()
        now = time.monotonic()
        for key in list(index.keys()):
            entry = index.get(key) or {}
            expires_at = float(entry.get("expires_at", 0.0) or 0.0)
            if expires_at and expires_at <= now:
                index.pop(key, None)
        while len(index) > TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES:
            index.popitem(last=False)

    def _mark_worker_cache_index(self, key: str, status: str, *, ttl: float | None = None, meta: dict[str, Any] | None = None) -> None:
        clean_key = str(key or "").strip()
        if not clean_key:
            return
        status = str(status or "").strip().lower() or "unknown"
        if ttl is None:
            if status == "miss":
                ttl = TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS
            elif status == "error":
                ttl = TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS
            else:
                ttl = max(float(TTS_AUDIO_CACHE_TTL_SECONDS), 3600.0)
        index = self._get_worker_cache_index()
        index[clean_key] = {
            "status": status,
            "updated_at": time.monotonic(),
            "expires_at": time.monotonic() + max(1.0, float(ttl)),
            **(meta or {}),
        }
        index.move_to_end(clean_key)
        self._prune_worker_cache_index()

    def _worker_cache_recent_negative_status(self, key: str) -> str:
        clean_key = str(key or "").strip()
        if not clean_key:
            return ""
        self._prune_worker_cache_index()
        entry = self._get_worker_cache_index().get(clean_key) or {}
        status = str(entry.get("status") or "").strip().lower()
        if status in {"miss", "error"}:
            return status
        return ""

    def _get_engine_alert_state(self) -> dict[str, float]:
        state = getattr(self, "_tts_engine_alert_last_sent", None)
        if state is None:
            state = {}
            setattr(self, "_tts_engine_alert_last_sent", state)
        return state

    def _schedule_alert_script(self, alert_type: str, title: str, body: str) -> None:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.sh")
        if not os.path.exists(script_path):
            return

        async def _runner() -> None:
            try:
                process = await asyncio.create_subprocess_exec(
                    "bash",
                    script_path,
                    alert_type,
                    title,
                    body,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.communicate()
            except Exception:
                logger.exception("[tts_voice] Falha ao enviar alerta de engine via webhook")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_runner())

    def _maybe_send_engine_alert(self, alert_key: str, alert_type: str, title: str, body: str) -> None:
        state = self._get_engine_alert_state()
        now = time.monotonic()
        last_sent = float(state.get(alert_key, 0.0) or 0.0)
        if (now - last_sent) < TTS_ENGINE_ALERT_COOLDOWN_SECONDS:
            return
        state[alert_key] = now
        self._schedule_alert_script(alert_type, title, body)

    def _record_engine_success(self, engine: str, duration_ms: float) -> None:
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["synth_count"] = int(engine_metrics.get("synth_count", 0) or 0) + 1
        engine_metrics["synth_total_ms"] = float(engine_metrics.get("synth_total_ms", 0.0) or 0.0) + float(duration_ms)
        engine_metrics["last_synth_ms"] = round(float(duration_ms), 2)
        engine_metrics["consecutive_failures"] = 0

        if duration_ms >= TTS_ENGINE_SLOW_WARN_SECONDS * 1000.0:
            engine_metrics["slow_alerts"] = int(engine_metrics.get("slow_alerts", 0) or 0) + 1
            title = f"Engine TTS lenta: {engine}"
            body = (
                f"Engine: {engine}\n"
                f"Duração da síntese: {round(duration_ms, 2)} ms\n"
                f"Limite de alerta: {round(TTS_ENGINE_SLOW_WARN_SECONDS * 1000.0, 2)} ms"
            )
            self._maybe_send_engine_alert(f"slow:{engine}", "warn", title, body)

    async def _record_persistent_synt_success(
        self,
        guild_id: int | None,
        engine: str,
        amount: int = 1,
    ) -> None:
        try:
            gid = int(guild_id or 0)
        except Exception:
            gid = 0
        if gid <= 0:
            return

        db = getattr(getattr(self, "bot", None), "settings_db", None)
        increment = getattr(db, "increment_tts_synt_count", None)
        if not callable(increment):
            return

        try:
            result = increment(gid, engine, max(1, int(amount or 1)))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("[tts_voice] Falha ao persistir synt TTS | guild=%s engine=%s", gid, engine)

    def _get_tts_background_tasks(self) -> set[asyncio.Task]:
        tasks = getattr(self, "_tts_background_tasks", None)
        if not isinstance(tasks, set):
            tasks = set()
            setattr(self, "_tts_background_tasks", tasks)
        return tasks

    def _schedule_tts_background(self, coroutine) -> asyncio.Task | None:
        try:
            task = asyncio.create_task(coroutine)
        except RuntimeError:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            return None
        tasks = self._get_tts_background_tasks()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _flush_persistent_synt_successes(self) -> None:
        await asyncio.sleep(TTS_PERSISTENT_STATS_FLUSH_SECONDS)
        while True:
            pending = getattr(self, "_tts_persistent_synt_pending", None)
            if not isinstance(pending, dict) or not pending:
                return
            batch = dict(pending)
            pending.clear()
            for (guild_id, engine), amount in batch.items():
                await self._record_persistent_synt_success(guild_id, engine, amount)
            if not pending:
                return
            await asyncio.sleep(TTS_PERSISTENT_STATS_FLUSH_SECONDS)

    def _schedule_persistent_synt_success(self, guild_id: int | None, engine: str) -> None:
        try:
            gid = int(guild_id or 0)
        except Exception:
            gid = 0
        if gid <= 0:
            return
        key = (gid, str(engine or "gtts").strip().lower() or "gtts")
        pending = getattr(self, "_tts_persistent_synt_pending", None)
        if not isinstance(pending, dict):
            pending = {}
            setattr(self, "_tts_persistent_synt_pending", pending)
        pending[key] = int(pending.get(key, 0) or 0) + 1

        task = getattr(self, "_tts_persistent_synt_flush_task", None)
        if task is not None and not task.done():
            return
        task = self._schedule_tts_background(self._flush_persistent_synt_successes())
        setattr(self, "_tts_persistent_synt_flush_task", task)

    def _schedule_cache_maintenance(
        self,
        state: GuildTTSState,
        *,
        protected_paths: Optional[set[str]] = None,
    ) -> None:
        protected = getattr(self, "_tts_cache_maintenance_protected", None)
        if not isinstance(protected, set):
            protected = set()
            setattr(self, "_tts_cache_maintenance_protected", protected)
        protected.update(os.path.abspath(path) for path in (protected_paths or set()) if path)

        task = getattr(self, "_tts_cache_maintenance_task", None)
        if task is not None and not task.done():
            return

        async def _runner() -> None:
            try:
                await asyncio.sleep(TTS_CACHE_MAINTENANCE_DELAY_SECONDS)
                while True:
                    active_playbacks = int(getattr(self, "_tts_active_playbacks", 0) or 0)
                    active_streams = len(self._get_edge_stream_handles())
                    queued_items = sum(guild_state.queue.qsize() for guild_state in self.guild_states.values())
                    if active_playbacks <= 0 and active_streams <= 0 and queued_items <= 0:
                        break
                    await asyncio.sleep(TTS_CACHE_MAINTENANCE_DELAY_SECONDS)
                paths = set(getattr(self, "_tts_cache_maintenance_protected", set()) or set())
                getattr(self, "_tts_cache_maintenance_protected", set()).clear()
                # O índice é pequeno e permanece no event loop; a varredura dos
                # diretórios e as remoções, que podem bloquear em disco lento,
                # são executadas em lotes fora dele.
                try:
                    self._purge_cache(state, protected_paths=paths, prune_tmp=False)
                except TypeError as exc:
                    # Preserva subclasses/mocks antigos que sobrescrevem o
                    # helper sem o novo argumento. A implementação normal
                    # nunca percorre o disco neste fallback.
                    if "prune_tmp" not in str(exc):
                        raise
                    self._purge_cache(state, protected_paths=paths)
                await self._prune_tmp_audio_dir_async(protected_paths=paths)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[tts_voice] manutenção adiada do cache falhou")

        task = asyncio.create_task(_runner())
        setattr(self, "_tts_cache_maintenance_task", task)

    def _shutdown_tts_runtime(self) -> None:
        self._cancel_edge_streams()
        maintenance = getattr(self, "_tts_cache_maintenance_task", None)
        if maintenance is not None and not maintenance.done():
            maintenance.cancel()
        for task in list(self._get_tts_background_tasks()):
            if not task.done():
                task.cancel()
        pending_stats = getattr(self, "_tts_persistent_synt_pending", None)
        if isinstance(pending_stats, dict):
            pending_stats.clear()
        executor = getattr(self, "_tts_gtts_executor", None)
        if executor is not None:
            setattr(self, "_tts_gtts_executor", None)
            executor.shutdown(wait=False, cancel_futures=True)

    def _record_engine_failure(self, engine: str, error: Exception, duration_ms: float | None = None) -> None:
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["synth_failures"] = int(engine_metrics.get("synth_failures", 0) or 0) + 1
        engine_metrics["consecutive_failures"] = int(engine_metrics.get("consecutive_failures", 0) or 0) + 1
        engine_metrics["last_error"] = str(error)
        engine_metrics["last_error_at"] = time.time()
        if duration_ms is not None:
            engine_metrics["last_synth_ms"] = round(float(duration_ms), 2)

        if int(engine_metrics.get("consecutive_failures", 0) or 0) >= TTS_ENGINE_FAILURE_ALERT_THRESHOLD:
            title = f"Falhas repetidas na engine TTS: {engine}"
            body = (
                f"Engine: {engine}\n"
                f"Falhas consecutivas: {engine_metrics['consecutive_failures']}\n"
                f"Último erro: {error}"
            )
            if duration_ms is not None:
                body += f"\nDuração até falhar: {round(float(duration_ms), 2)} ms"
            self._maybe_send_engine_alert(f"fail:{engine}", "error", title, body)

    def _record_queue_timing(
        self,
        *,
        queue_wait_ms: float | None = None,
        dispatch_ms: float | None = None,
        source_setup_ms: float | None = None,
        play_call_ms: float | None = None,
        playback_ms: float | None = None,
        total_to_playback_ms: float | None = None,
    ) -> None:
        if queue_wait_ms is not None:
            self._record_average_metric("queue_wait_total_ms", "queue_wait_samples", queue_wait_ms)
            self._record_latency_sample("queue_wait", queue_wait_ms)
        if dispatch_ms is not None:
            self._record_average_metric("dispatch_total_ms", "dispatch_samples", dispatch_ms)
            self._record_latency_sample("dispatch", dispatch_ms)
        if source_setup_ms is not None:
            self._record_average_metric("source_setup_total_ms", "source_setup_samples", source_setup_ms)
        if play_call_ms is not None:
            self._record_average_metric("play_call_total_ms", "play_call_samples", play_call_ms)
        if playback_ms is not None:
            self._record_average_metric("playback_total_ms", "playback_samples", playback_ms)
        if total_to_playback_ms is not None:
            self._record_average_metric("total_to_playback_total_ms", "total_to_playback_samples", total_to_playback_ms)
            self._record_latency_sample("total_to_playback", total_to_playback_ms)

    def _hydrate_cache_index(self) -> None:
        cache_order = self._get_global_cache_order()
        cache_paths = self._get_global_cache_paths()
        cache_frequency = self._get_cache_frequency_map()
        cache_order.clear()
        cache_paths.clear()
        cache_frequency.clear()
        if not os.path.isdir(_CACHE_DIR):
            return
        cache_files: list[tuple[float, str, str]] = []
        try:
            with os.scandir(_CACHE_DIR) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    suffix = os.path.splitext(entry.name)[1].lower()
                    if suffix not in _TTS_CACHE_SUFFIXES:
                        continue
                    try:
                        stat = entry.stat()
                    except FileNotFoundError:
                        continue
                    cache_files.append((stat.st_mtime, os.path.splitext(entry.name)[0], os.path.abspath(entry.path)))
        except FileNotFoundError:
            return

        for modified_ts, key, path in sorted(cache_files, key=lambda item: item[0]):
            cache_order[key] = modified_ts
            cache_order.move_to_end(key)
            cache_paths[key] = path
            cache_frequency[key] = 1

    def _prime_tts_runtime(self) -> None:
        self._get_synth_semaphore()
        self._get_gtts_semaphore()
        self._get_gtts_rate_lock()
        self._get_global_cache_order()
        self._get_global_cache_paths()
        self._get_cache_frequency_map()
        self._get_inflight_cache_tasks()
        self._get_worker_cache_store_tasks()
        self._get_worker_cache_store_semaphore()
        self._get_worker_cache_index()
        self._get_metrics_store()
        self._get_edge_stream_handles()
        self._cleanup_stale_edge_stream_files()
        self._hydrate_cache_index()

    async def _boot_warmup(self) -> None:
        metrics = self._get_metrics_store()
        started_at = time.monotonic()
        metrics["boot_warmups"] = int(metrics.get("boot_warmups", 0) or 0) + 1
        metrics["last_warmup_started_at"] = time.time()

        try:
            await asyncio.to_thread(self._prime_tts_runtime)
            await asyncio.to_thread(self._prune_tmp_audio_dir, force=True)
        finally:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            metrics["last_warmup_completed_at"] = time.time()
            metrics["last_warmup_duration_ms"] = round(duration_ms, 2)

    def get_tts_metrics_snapshot(self) -> dict[str, object]:
        metrics = self._get_metrics_store()
        snapshot = {
            "queue_enqueued": int(metrics.get("queue_enqueued", 0) or 0),
            "queue_deduplicated": int(metrics.get("queue_deduplicated", 0) or 0),
            "queue_dropped": int(metrics.get("queue_dropped", 0) or 0),
            "cache_hits": int(metrics.get("cache_hits", 0) or 0),
            "cache_misses": int(metrics.get("cache_misses", 0) or 0),
            "cache_stores": int(metrics.get("cache_stores", 0) or 0),
            "edge_stream_started": int(metrics.get("edge_stream_started", 0) or 0),
            "edge_stream_completed": int(metrics.get("edge_stream_completed", 0) or 0),
            "edge_stream_failures": int(metrics.get("edge_stream_failures", 0) or 0),
            "edge_stream_fallbacks": int(metrics.get("edge_stream_fallbacks", 0) or 0),
            "avg_edge_stream_first_audio_ms": round(
                float(metrics.get("edge_stream_first_audio_total_ms", 0.0) or 0.0)
                / int(metrics.get("edge_stream_first_audio_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_stream_first_audio_samples", 0) or 0) else 0.0,
            "edge_stream_audio_bytes": int(metrics.get("edge_stream_audio_bytes", 0) or 0),
            "avg_edge_slot_wait_ms": round(
                float(metrics.get("edge_slot_wait_total_ms", 0.0) or 0.0)
                / int(metrics.get("edge_slot_wait_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_slot_wait_samples", 0) or 0) else 0.0,
            "avg_edge_network_first_audio_ms": round(
                float(metrics.get("edge_network_first_audio_total_ms", 0.0) or 0.0)
                / int(metrics.get("edge_network_first_audio_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_network_first_audio_samples", 0) or 0) else 0.0,
            "avg_edge_local_handoff_ms": round(
                float(metrics.get("edge_local_handoff_total_ms", 0.0) or 0.0)
                / int(metrics.get("edge_local_handoff_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_local_handoff_samples", 0) or 0) else 0.0,
            "avg_edge_prebuffer_fill_ms": round(
                float(metrics.get("edge_prebuffer_fill_total_ms", 0.0) or 0.0)
                / int(metrics.get("edge_prebuffer_fill_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_prebuffer_fill_samples", 0) or 0) else 0.0,
            "avg_edge_first_chunk_bytes": round(
                float(metrics.get("edge_first_chunk_bytes_total", 0.0) or 0.0)
                / int(metrics.get("edge_first_chunk_bytes_samples", 0) or 1),
                2,
            ) if int(metrics.get("edge_first_chunk_bytes_samples", 0) or 0) else 0.0,
            "edge_stream_starvations": int(metrics.get("edge_stream_starvations", 0) or 0),
            "edge_source_read_stalls": int(metrics.get("edge_source_read_stalls", 0) or 0),
            "edge_prebuffer_raised": int(metrics.get("edge_prebuffer_raised", 0) or 0),
            "edge_prebuffer_lowered": int(metrics.get("edge_prebuffer_lowered", 0) or 0),
            "edge_prebuffer_profiles": int(len(self._get_edge_prebuffer_profiles())),
            "edge_circuit_bypasses": int(metrics.get("edge_circuit_bypasses", 0) or 0),
            "gtts_stream_started": int(metrics.get("gtts_stream_started", 0) or 0),
            "gtts_stream_completed": int(metrics.get("gtts_stream_completed", 0) or 0),
            "gtts_stream_failures": int(metrics.get("gtts_stream_failures", 0) or 0),
            "gtts_stream_fallbacks": int(metrics.get("gtts_stream_fallbacks", 0) or 0),
            "avg_gtts_stream_first_audio_ms": round(
                float(metrics.get("gtts_stream_first_audio_total_ms", 0.0) or 0.0)
                / int(metrics.get("gtts_stream_first_audio_samples", 0) or 1),
                2,
            ) if int(metrics.get("gtts_stream_first_audio_samples", 0) or 0) else 0.0,
            "gtts_stream_audio_bytes": int(metrics.get("gtts_stream_audio_bytes", 0) or 0),
            "edge_streams_active": int(len(self._get_edge_stream_handles())),
            "avg_queue_wait_ms": round((float(metrics.get("queue_wait_total_ms", 0.0) or 0.0) / int(metrics.get("queue_wait_samples", 0) or 1)), 2) if int(metrics.get("queue_wait_samples", 0) or 0) else 0.0,
            "avg_dispatch_ms": round((float(metrics.get("dispatch_total_ms", 0.0) or 0.0) / int(metrics.get("dispatch_samples", 0) or 1)), 2) if int(metrics.get("dispatch_samples", 0) or 0) else 0.0,
            "avg_source_setup_ms": round((float(metrics.get("source_setup_total_ms", 0.0) or 0.0) / int(metrics.get("source_setup_samples", 0) or 1)), 2) if int(metrics.get("source_setup_samples", 0) or 0) else 0.0,
            "avg_play_call_ms": round((float(metrics.get("play_call_total_ms", 0.0) or 0.0) / int(metrics.get("play_call_samples", 0) or 1)), 2) if int(metrics.get("play_call_samples", 0) or 0) else 0.0,
            "first_frame_unobserved": int(metrics.get("first_frame_unobserved", 0) or 0),
            "avg_playback_ms": round((float(metrics.get("playback_total_ms", 0.0) or 0.0) / int(metrics.get("playback_samples", 0) or 1)), 2) if int(metrics.get("playback_samples", 0) or 0) else 0.0,
            "avg_total_to_playback_ms": round((float(metrics.get("total_to_playback_total_ms", 0.0) or 0.0) / int(metrics.get("total_to_playback_samples", 0) or 1)), 2) if int(metrics.get("total_to_playback_samples", 0) or 0) else 0.0,
            "avg_queue_depth_at_enqueue": round((float(metrics.get("queue_depth_total", 0.0) or 0.0) / int(metrics.get("queue_depth_samples", 0) or 1)), 2) if int(metrics.get("queue_depth_samples", 0) or 0) else 0.0,
            "max_queue_depth_seen": int(metrics.get("queue_depth_max", 0) or 0),
            "prefetch_started": int(metrics.get("prefetch_started", 0) or 0),
            "prefetch_promoted": int(metrics.get("prefetch_promoted", 0) or 0),
            "prefetch_waiter_promoted": int(metrics.get("prefetch_waiter_promoted", 0) or 0),
            "worker_cache_lookup_hits": int(metrics.get("worker_cache_lookup_hits", 0) or 0),
            "worker_cache_lookup_misses": int(metrics.get("worker_cache_lookup_misses", 0) or 0),
            "worker_cache_lookup_skipped": int(metrics.get("worker_cache_lookup_skipped", 0) or 0),
            "worker_cache_lookup_errors": int(metrics.get("worker_cache_lookup_errors", 0) or 0),
            "worker_cache_store_ok": int(metrics.get("worker_cache_store_ok", 0) or 0),
            "worker_cache_store_failed": int(metrics.get("worker_cache_store_failed", 0) or 0),
            "avg_worker_cache_hit_ms": round((float(metrics.get("worker_cache_hit_total_ms", 0.0) or 0.0) / int(metrics.get("worker_cache_hit_samples", 0) or 1)), 2) if int(metrics.get("worker_cache_hit_samples", 0) or 0) else 0.0,
            "worker_cache_index_entries": int(len(self._get_worker_cache_index())),
            "tts_agent": self._tts_agent_public_snapshot(),
            "tts_agent_health_ok": int(metrics.get("tts_agent_health_ok", 0) or 0),
            "tts_agent_health_fail": int(metrics.get("tts_agent_health_fail", 0) or 0),
            "tts_agent_synth_attempts": int(metrics.get("tts_agent_synth_attempts", 0) or 0),
            "tts_agent_synth_ok": int(metrics.get("tts_agent_synth_ok", 0) or 0),
            "tts_agent_synth_failed": int(metrics.get("tts_agent_synth_failed", 0) or 0),
            "tts_agent_busy_retries": int(metrics.get("tts_agent_busy_retries", 0) or 0),
            "tts_agent_last_failure_reason": str(metrics.get("tts_agent_last_failure_reason") or ""),
            "avg_tts_agent_synth_ms": round((float(metrics.get("tts_agent_synth_total_ms", 0.0) or 0.0) / int(metrics.get("tts_agent_synth_samples", 0) or 1)), 2) if int(metrics.get("tts_agent_synth_samples", 0) or 0) else 0.0,
            "tts_agent_last_requested_engine": str(metrics.get("tts_agent_last_requested_engine") or ""),
            "tts_agent_last_selected_engine": str(metrics.get("tts_agent_last_selected_engine") or ""),
            "tts_agent_last_audio_format": str(metrics.get("tts_agent_last_audio_format") or ""),
            "tts_agent_last_audio_bytes": int(metrics.get("tts_agent_last_audio_bytes", 0) or 0),
            "tts_agent_last_cache_hit": bool(metrics.get("tts_agent_last_cache_hit")),
            "tts_agent_last_synth_ms": float(metrics.get("tts_agent_last_synth_ms", 0.0) or 0.0),
            "tts_agent_last_timing_ms": dict(metrics.get("tts_agent_last_timing_ms") or {}),
            "tts_agent_route_worker_samples": int(metrics.get("tts_agent_route_worker_samples", 0) or 0),
            "tts_agent_route_vps_samples": int(metrics.get("tts_agent_route_vps_samples", 0) or 0),
            "worker_voice_agent": dict(metrics.get("worker_voice_agent") or self._tts_agent_route_state().get("voice_agent") or {}),
            "worker_voice_session_reports_ok": int(metrics.get("worker_voice_session_reports_ok", 0) or 0),
            "worker_voice_session_reports_failed": int(metrics.get("worker_voice_session_reports_failed", 0) or 0),
            "worker_voice_session_skipped": int(metrics.get("worker_voice_session_skipped", 0) or 0),
            "worker_voice_session_handoff_ok": int(metrics.get("worker_voice_session_handoff_ok", 0) or 0),
            "worker_voice_session_handoff_failed": int(metrics.get("worker_voice_session_handoff_failed", 0) or 0),
            "worker_voice_session_handoff_skipped": int(metrics.get("worker_voice_session_handoff_skipped", 0) or 0),
            "worker_voice_session_connection_probe_ok": int(metrics.get("worker_voice_session_connection_probe_ok", 0) or 0),
            "worker_voice_session_connection_probe_failed": int(metrics.get("worker_voice_session_connection_probe_failed", 0) or 0),
            "worker_voice_session_connection_probe_skipped": int(metrics.get("worker_voice_session_connection_probe_skipped", 0) or 0),
            "worker_voice_session_transfer_prepare_ok": int(metrics.get("worker_voice_session_transfer_prepare_ok", 0) or 0),
            "worker_voice_session_transfer_prepare_failed": int(metrics.get("worker_voice_session_transfer_prepare_failed", 0) or 0),
            "worker_voice_session_transfer_prepare_skipped": int(metrics.get("worker_voice_session_transfer_prepare_skipped", 0) or 0),
            "worker_voice_session_clears_ok": int(metrics.get("worker_voice_session_clears_ok", 0) or 0),
            "worker_voice_session_clears_failed": int(metrics.get("worker_voice_session_clears_failed", 0) or 0),
            "message_gate_seen": int(metrics.get("message_gate_seen", 0) or 0),
            "message_gate_matched": int(metrics.get("message_gate_matched", 0) or 0),
            "message_gate_ignored": int(metrics.get("message_gate_ignored", 0) or 0),
            "last_message_gate_reason": str(metrics.get("last_message_gate_reason") or ""),
            "last_message_gate_guild_id": int(metrics.get("last_message_gate_guild_id", 0) or 0),
            "last_message_gate_channel_id": int(metrics.get("last_message_gate_channel_id", 0) or 0),
            "last_message_gate_author_id": int(metrics.get("last_message_gate_author_id", 0) or 0),
            "last_message_gate_seen_at": metrics.get("last_message_gate_seen_at"),
            "boot_warmups": int(metrics.get("boot_warmups", 0) or 0),
            "last_warmup_duration_ms": metrics.get("last_warmup_duration_ms"),
            "queued_items_current": int(sum(state.queue.qsize() for state in self.guild_states.values())),
            "guild_states_current": int(len(self.guild_states)),
            "latency_percentiles_ms": self._latency_percentiles_snapshot(),
            "engines": {},
            "temp_dirs": _tts_temp_dirs_snapshot(),
        }
        for engine, engine_metrics in dict(metrics.get("engines", {})).items():
            synth_count = int(engine_metrics.get("synth_count", 0) or 0)
            total_ms = float(engine_metrics.get("synth_total_ms", 0.0) or 0.0)
            snapshot["engines"][engine] = {
                "synth_count": synth_count,
                "synth_failures": int(engine_metrics.get("synth_failures", 0) or 0),
                "slow_alerts": int(engine_metrics.get("slow_alerts", 0) or 0),
                "cache_hits": int(engine_metrics.get("cache_hits", 0) or 0),
                "cache_misses": int(engine_metrics.get("cache_misses", 0) or 0),
                "avg_synth_ms": round(total_ms / synth_count, 2) if synth_count else 0.0,
                "last_synth_ms": engine_metrics.get("last_synth_ms"),
                "last_error": engine_metrics.get("last_error"),
                "consecutive_failures": int(engine_metrics.get("consecutive_failures", 0) or 0),
            }
        return snapshot

    def _should_prune_tmp_audio_dir(self, *, force: bool = False) -> bool:
        if force:
            setattr(self, "_tts_last_prune_ts", time.monotonic())
            return True

        now = time.monotonic()
        last_prune = float(getattr(self, "_tts_last_prune_ts", 0.0) or 0.0)
        if (now - last_prune) < TTS_TEMP_PRUNE_INTERVAL_SECONDS:
            return False

        setattr(self, "_tts_last_prune_ts", now)
        return True

    def _make_runtime_temp_file(self, suffix: str = ".mp3") -> str:
        _ensure_tts_temp_dirs()
        try:
            fd, path = tempfile.mkstemp(prefix="tts_", suffix=suffix, dir=_RUNTIME_DIR)
        except FileNotFoundError:
            # A cleanup job may have deleted an empty runtime dir between the
            # import-time mkdir and this synthesis request. Recreate and retry
            # once so Edge/gTTS do not all fail for the same infra issue.
            _ensure_tts_temp_dirs()
            fd, path = tempfile.mkstemp(prefix="tts_", suffix=suffix, dir=_RUNTIME_DIR)
        os.close(fd)
        return path

    def _list_tmp_audio_files(self) -> list[tuple[int, float, int, str]]:
        _ensure_tts_temp_dirs()
        result: list[tuple[int, float, int, str]] = []
        for directory, priority in ((_RUNTIME_DIR, 0), (_CACHE_DIR, 1)):
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if not entry.name.lower().endswith((".mp3", ".wav", ".ogg", ".tmp")):
                            continue
                        try:
                            stat = entry.stat()
                        except FileNotFoundError:
                            continue
                        result.append((priority, stat.st_mtime, stat.st_size, entry.path))
            except FileNotFoundError:
                continue
        return result

    def _prune_tmp_audio_dir(self, *, protected_paths: Optional[set[str]] = None, force: bool = False) -> None:
        _ensure_tts_temp_dirs()
        if not self._should_prune_tmp_audio_dir(force=force):
            return

        protected = {os.path.abspath(p) for p in (protected_paths or set()) if p}
        try:
            active_handles = list(self._get_edge_stream_handles().values())
        except RuntimeError:
            return
        for handle in active_handles:
            if handle.part_path:
                protected.add(os.path.abspath(handle.part_path))
            if handle.fifo_path:
                protected.add(os.path.abspath(handle.fifo_path))
        try:
            protected.update(self._get_stream_cache_parts())
        except RuntimeError:
            return
        files = self._list_tmp_audio_files()
        total_files = len(files)
        total_bytes = sum(size for _, _, size, _ in files)

        effective_max_files = TTS_TEMP_MAX_FILES + TTS_PIPER_VPS_CACHE_SIZE
        effective_max_bytes = TTS_TEMP_MAX_BYTES + TTS_PIPER_VPS_CACHE_MAX_BYTES

        if total_files <= effective_max_files and total_bytes <= effective_max_bytes:
            return

        cache_order = self._get_global_cache_order()

        for _, _, size, path in sorted(files, key=lambda item: (item[0], item[1])):
            abs_path = os.path.abspath(path)
            if abs_path in protected:
                continue
            if total_files <= effective_max_files and total_bytes <= effective_max_bytes:
                break
            try:
                os.remove(abs_path)
            except FileNotFoundError:
                pass
            except Exception:
                continue
            total_files = max(0, total_files - 1)
            total_bytes = max(0, total_bytes - size)
            if abs_path.startswith(os.path.abspath(_CACHE_DIR) + os.sep):
                cache_key = os.path.splitext(os.path.basename(abs_path))[0]
                self._forget_cache_entry(cache_key, path=abs_path)

    def _tts_foreground_work_pending(self) -> bool:
        return bool(
            int(getattr(self, "_tts_active_playbacks", 0) or 0) > 0
            or self._get_edge_stream_handles()
            or any(state.queue.qsize() > 0 for state in self.guild_states.values())
        )

    async def _prune_tmp_audio_dir_async(self, *, protected_paths: Optional[set[str]] = None) -> None:
        """Scan/remove temporaries without monopolizing the Discord event loop."""
        if not self._should_prune_tmp_audio_dir():
            return

        protected = {os.path.abspath(path) for path in (protected_paths or set()) if path}
        for handle in list(self._get_edge_stream_handles().values()):
            if handle.part_path:
                protected.add(os.path.abspath(handle.part_path))
            if handle.fifo_path:
                protected.add(os.path.abspath(handle.fifo_path))
        protected.update(self._get_stream_cache_parts())

        files = await asyncio.to_thread(self._list_tmp_audio_files)
        total_files = len(files)
        total_bytes = sum(size for _, _, size, _ in files)
        max_files = TTS_TEMP_MAX_FILES + TTS_PIPER_VPS_CACHE_SIZE
        max_bytes = TTS_TEMP_MAX_BYTES + TTS_PIPER_VPS_CACHE_MAX_BYTES
        if total_files <= max_files and total_bytes <= max_bytes:
            return

        candidates = sorted(files, key=lambda entry: (entry[0], entry[1]))
        cursor = 0
        while cursor < len(candidates) and (total_files > max_files or total_bytes > max_bytes):
            if self._tts_foreground_work_pending():
                return

            active = set(protected)
            for handle in list(self._get_edge_stream_handles().values()):
                if handle.part_path:
                    active.add(os.path.abspath(handle.part_path))
                if handle.fifo_path:
                    active.add(os.path.abspath(handle.fifo_path))
            active.update(self._get_stream_cache_parts())

            batch: list[tuple[int, str]] = []
            while cursor < len(candidates) and len(batch) < 16:
                _, _, size, path = candidates[cursor]
                cursor += 1
                abs_path = os.path.abspath(path)
                if abs_path in active:
                    continue
                batch.append((size, abs_path))
                if total_files - len(batch) <= max_files and total_bytes - sum(item[0] for item in batch) <= max_bytes:
                    break
            if not batch:
                continue

            def _remove_batch(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
                removed: list[tuple[int, str]] = []
                for size, path in entries:
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        removed.append((size, path))
                    except OSError:
                        continue
                    else:
                        removed.append((size, path))
                return removed

            removed = await asyncio.to_thread(_remove_batch, batch)
            for size, abs_path in removed:
                total_files = max(0, total_files - 1)
                total_bytes = max(0, total_bytes - size)
                if abs_path.startswith(os.path.abspath(_CACHE_DIR) + os.sep):
                    cache_key = os.path.splitext(os.path.basename(abs_path))[0]
                    self._forget_cache_entry(cache_key, path=abs_path)
            await asyncio.sleep(0)


    def _touch_cache_entry(self, state: GuildTTSState, key: str, *, path: str | None = None) -> None:
        cache_order = self._get_global_cache_order()
        cache_paths = self._get_global_cache_paths()
        cache_frequency = self._get_cache_frequency_map()
        now = time.time()
        cache_order[key] = now
        cache_order.move_to_end(key)
        cache_frequency[key] = int(cache_frequency.get(key, 0) or 0) + 1
        if path:
            cache_paths[key] = os.path.abspath(path)

    def _forget_cache_entry(self, key: str, *, path: str | None = None) -> None:
        cache_paths = self._get_global_cache_paths()
        mapped = str(cache_paths.get(key) or "")
        if path is not None and mapped and os.path.abspath(mapped) != os.path.abspath(path):
            # A cleanup may remove an older duplicate with another extension.
            # Keep the active cache entry and its usage metadata in that case.
            return
        self._get_global_cache_order().pop(key, None)
        self._get_cache_frequency_map().pop(key, None)
        cache_paths.pop(key, None)

    def _is_piper_cache_key(self, key: str) -> bool:
        return str(key or "").startswith("piper_")

    def _cache_quota_overflow(self, cache_order: OrderedDict[str, float]) -> tuple[bool, bool, bool]:
        piper_count = sum(1 for key in cache_order if self._is_piper_cache_key(key))
        normal_count = max(0, len(cache_order) - piper_count)
        piper_over = piper_count > TTS_PIPER_VPS_CACHE_SIZE
        normal_over = normal_count > TTS_AUDIO_CACHE_SIZE
        total_over = len(cache_order) > (TTS_AUDIO_CACHE_SIZE + TTS_PIPER_VPS_CACHE_SIZE)
        return normal_over, piper_over, total_over

    def _cache_path_for_key(self, key: str, *, item: QueueItem | None = None) -> str | None:
        cache_paths = self._get_global_cache_paths()
        mapped = str(cache_paths.get(key) or "")
        if mapped and os.path.isfile(mapped):
            return mapped
        if mapped:
            cache_paths.pop(key, None)

        suffixes = self._cache_suffix_candidates_for_item(item) if item is not None else list(_TTS_CACHE_SUFFIXES)
        for suffix in suffixes:
            candidate = self._cache_path(key, suffix=suffix)
            if os.path.isfile(candidate):
                cache_paths[key] = os.path.abspath(candidate)
                return candidate
        return None

    def _cache_index_sweep_due(self, *, force: bool = False) -> bool:
        if force:
            setattr(self, "_tts_last_cache_index_sweep_monotonic", time.monotonic())
            return True
        now = time.monotonic()
        last = float(getattr(self, "_tts_last_cache_index_sweep_monotonic", 0.0) or 0.0)
        if (now - last) < TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS:
            return False
        setattr(self, "_tts_last_cache_index_sweep_monotonic", now)
        return True

    def _remove_cache_file(self, key: str, path: str | None) -> None:
        self._forget_cache_entry(key, path=path)
        if not path:
            return
        with contextlib.suppress(FileNotFoundError, OSError):
            os.remove(path)

    def _purge_cache(
        self,
        state: GuildTTSState,
        *,
        protected_paths: Optional[set[str]] = None,
        force_tmp_prune: bool = False,
        prune_tmp: bool = True,
    ) -> None:
        cache_order = self._get_global_cache_order()
        cache_frequency = self._get_cache_frequency_map()
        protected = {os.path.abspath(p) for p in (protected_paths or set()) if p}

        if self._cache_index_sweep_due(force=force_tmp_prune):
            now = time.time()
            sweep_entries = list(cache_order.items())
            if not force_tmp_prune:
                sweep_entries = sweep_entries[:TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES]
            for key, last_used_ts in sweep_entries:
                path = self._cache_path_for_key(key)
                abs_path = os.path.abspath(path) if path else ""
                expired = bool(
                    TTS_AUDIO_CACHE_TTL_SECONDS > 0
                    and (now - float(last_used_ts or 0.0)) > TTS_AUDIO_CACHE_TTL_SECONDS
                )
                if not path:
                    self._forget_cache_entry(key)
                elif expired and abs_path not in protected:
                    self._remove_cache_file(key, path)

        while True:
            normal_over, piper_over, total_over = self._cache_quota_overflow(cache_order)
            if not (normal_over or piper_over or total_over):
                break

            candidate_key = None
            candidate_path = None
            candidate_score = None
            for key, last_used_ts in cache_order.items():
                is_piper = self._is_piper_cache_key(key)
                if piper_over and not is_piper:
                    continue
                if normal_over and not piper_over and is_piper:
                    continue
                path = self._cache_path_for_key(key)
                if not path:
                    candidate_key = key
                    candidate_path = None
                    break
                abs_path = os.path.abspath(path)
                if abs_path in protected:
                    continue
                score = (int(cache_frequency.get(key, 0) or 0), float(last_used_ts))
                if candidate_score is None or score < candidate_score:
                    candidate_key = key
                    candidate_path = path
                    candidate_score = score

            if candidate_key is None:
                break
            self._remove_cache_file(candidate_key, candidate_path)

        if prune_tmp:
            self._prune_tmp_audio_dir(protected_paths=protected_paths, force=force_tmp_prune)

    async def _store_in_cache(self, state: GuildTTSState, item: QueueItem, source_path: str) -> str:
        key = self._cache_key(item)
        path = self._cache_path(key, suffix=self._cache_suffix_from_path(source_path))
        existing = self._cache_path_for_key(key, item=item)

        if existing:
            if os.path.abspath(existing) != os.path.abspath(source_path):
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(source_path)
            self._touch_cache_entry(state, key, path=existing)
            self._schedule_cache_maintenance(state, protected_paths={existing})
            return existing

        try:
            await asyncio.to_thread(os.replace, source_path, path)
        except Exception:
            try:
                await asyncio.to_thread(shutil.copyfile, source_path, path)
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(source_path)
            except Exception:
                return source_path

        self._touch_cache_entry(state, key, path=path)
        self._record_cache_store()
        self._schedule_cache_maintenance(state, protected_paths={path})
        return path

    async def _finalize_progressive_stream_cache(self, handle: EdgeStreamHandle) -> None:
        part_path = str(handle.part_path or "")
        part_abs = os.path.abspath(part_path) if part_path else ""
        try:
            cache_buffer = handle.cache_buffer
            if cache_buffer is not None:
                def _write_buffer(target: str, payload: bytearray) -> None:
                    with open(target, "xb", buffering=TTS_EDGE_STREAM_CACHE_BUFFER_BYTES) as output:
                        output.write(payload)

                await asyncio.to_thread(_write_buffer, part_path, cache_buffer)
            elif not await asyncio.to_thread(os.path.isfile, part_path):
                raise RuntimeError("stream progressivo terminou sem arquivo ou buffer de cache")
            cached_path = await self._store_in_cache(handle.state, handle.item, part_path)
            cached_abs = os.path.abspath(cached_path) if cached_path else ""
            if cached_path and os.path.isfile(cached_path) and cached_abs != part_abs:
                handle.cache_path = cached_path
                if not bool(getattr(handle.item, "_tts_agent_inline_cache", False)):
                    self._schedule_worker_turbo_cache_store(handle.item, cached_path)
            else:
                handle.cache_error = RuntimeError("cache progressivo não foi confirmado")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            handle.cache_error = exc
            logger.warning(
                "[tts_stream] áudio concluído, mas cache local falhou | guild=%s engine=%s erro=%s",
                handle.item.guild_id,
                handle.engine,
                exc,
            )
        finally:
            handle.cache_buffer = None
            if part_abs:
                self._get_stream_cache_parts().discard(part_abs)
            if part_path and os.path.isfile(part_path):
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(part_path)

    def _schedule_progressive_stream_cache(self, handle: EdgeStreamHandle) -> None:
        if (
            not handle.store_in_cache
            or not handle.part_path
        ):
            return
        part_abs = os.path.abspath(handle.part_path)
        self._get_stream_cache_parts().add(part_abs)
        task = self._schedule_tts_background(self._finalize_progressive_stream_cache(handle))
        if task is None:
            self._get_stream_cache_parts().discard(part_abs)
            with contextlib.suppress(FileNotFoundError, OSError):
                os.remove(handle.part_path)
            return
        handle.cache_task = task

    def _cache_key(self, item: QueueItem) -> str:
        cached_key = getattr(item, "_cache_key_value", None)
        if cached_key is not None:
            return cached_key

        text = self._get_item_normalized_cache_text(item)
        engine = (item.engine or "gtts").strip().lower()
        if engine == "edge":
            voice = validate_voice(item.voice, getattr(self, "edge_voice_names", set()))
            payload = f"edge|{voice}|{self._normalize_edge_rate(item.rate)}|{self._normalize_edge_pitch(item.pitch)}|{text}"
        elif engine == "piper":
            model = str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME).strip() or TTS_PIPER_MODEL_NAME
            payload = f"piper|worker|{model}|{text}"
        elif engine == "android_native":
            language = (item.language or "pt-BR").strip().lower().replace('_', '-')
            voice = str(item.voice or "auto").strip() or "auto"
            payload = f"android_native|worker|{language}|{voice}|{item.rate or '1.0'}|{item.pitch or '1.0'}|{text}"
        else:
            language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace('_', '-')
            if language == 'pt-br':
                language = 'pt'
            payload = f"gtts|{language}|{text}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cached_key = f"piper_{digest}" if engine == "piper" else f"android_{digest}" if engine == "android_native" else digest
        item._cache_key_value = cached_key
        return cached_key

    def _cache_path(self, key: str, *, suffix: str = ".mp3") -> str:
        clean_suffix = str(suffix or ".mp3").strip().lower()
        if not clean_suffix.startswith("."):
            clean_suffix = f".{clean_suffix}"
        if clean_suffix not in _TTS_CACHE_SUFFIXES:
            clean_suffix = ".mp3"
        return os.path.join(_CACHE_DIR, f"{key}{clean_suffix}")

    def _cache_suffix_from_path(self, path: str) -> str:
        suffix = os.path.splitext(str(path or ""))[1].lower()
        return suffix if suffix in _TTS_CACHE_SUFFIXES else ".mp3"

    def _cache_suffix_candidates_for_item(self, item: QueueItem | None) -> list[str]:
        engine = str(getattr(item, "engine", "") or "gtts").strip().lower() if item is not None else ""
        candidates: list[str] = []
        if engine in {"piper", "android_native"}:
            candidates.extend([".wav", ".ogg", ".mp3"])
        elif engine:
            candidates.extend([".mp3", ".ogg", ".wav"])
        candidates.extend(_TTS_CACHE_SUFFIXES)
        result: list[str] = []
        for suffix in candidates:
            suffix = self._cache_suffix_from_path(f"x{suffix}")
            if suffix not in result:
                result.append(suffix)
        return result

    def _try_get_cached_path(self, state: GuildTTSState, item: QueueItem) -> Optional[str]:
        key = self._cache_key(item)
        path = self._cache_path_for_key(key, item=item)
        if not path:
            return None
        try:
            if os.path.getsize(path) <= 0:
                self._remove_cache_file(key, path)
                return None
        except OSError:
            self._forget_cache_entry(key, path=path)
            return None

        cache_order = self._get_global_cache_order()
        last_used = float(cache_order.get(key, 0.0) or 0.0)
        if TTS_AUDIO_CACHE_TTL_SECONDS > 0 and last_used and (time.time() - last_used) > TTS_AUDIO_CACHE_TTL_SECONDS:
            self._remove_cache_file(key, path)
            return None

        self._touch_cache_entry(state, key, path=path)
        self._record_cache_hit(item.engine)
        self._log_debug(f"[tts_voice] cache hit | guild={item.guild_id} key={key[:10]} path={os.path.basename(path)}")
        return path


    async def _generate_gtts_file(
        self,
        text: str,
        language: str,
        *,
        tld: str = "com",
        foreground: bool = True,
    ) -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace('_', '-')
        if language == 'pt-br':
            language = 'pt'
        tld = str(tld or "com").strip() or "com"
        self._log_debug(f"[tts_voice] gTTS synth | language={language!r} tld={tld!r} text={text[:80]!r}")

        path = self._make_runtime_temp_file(suffix=".mp3")
        stop_requested = threading.Event()
        semaphore = self._get_gtts_semaphore()
        semaphore_acquired = False
        release_deferred = False
        future: asyncio.Future | None = None
        try:
            def _write_gtts_file(target_path: str) -> None:
                # O timeout nativo limita a conexão que continua dentro da
                # thread mesmo se a coroutine externa for cancelada.
                tts = gTTS(
                    text=text,
                    lang=language,
                    tld=tld,
                    timeout=(TTS_GTTS_CONNECT_TIMEOUT_SECONDS, TTS_GTTS_READ_TIMEOUT_SECONDS),
                )
                stream = getattr(tts, "stream", None)
                with open(target_path, "wb") as output:
                    if callable(stream):
                        for chunk in self._iter_gtts_audio_chunks(tts):
                            if stop_requested.is_set():
                                raise TimeoutError("gTTS interrompido")
                            if chunk:
                                output.write(bytes(chunk))
                    else:
                        # Compatibilidade defensiva com versões antigas.
                        tts.write_to_fp(output)

            await semaphore.acquire(foreground=foreground)
            semaphore_acquired = True
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._get_gtts_executor(), _write_gtts_file, path)
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=TTS_GTTS_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                stop_requested.set()
                if not future.done():
                    release_deferred = True
                    def _release_after_physical_stop(done_future: asyncio.Future) -> None:
                        semaphore.release()
                        with contextlib.suppress(BaseException):
                            done_future.exception()
                    future.add_done_callback(_release_after_physical_stop)
                raise
            return path
        except asyncio.TimeoutError as exc:
            logger.warning(
                "[tts_voice] gTTS excedeu o limite | language=%s timeout=%.1fs",
                language,
                TTS_GTTS_TIMEOUT_SECONDS,
            )
            with contextlib.suppress(Exception):
                os.remove(path)
            raise RuntimeError(f"gTTS timeout após {TTS_GTTS_TIMEOUT_SECONDS:.1f}s") from exc
        except BaseException:
            stop_requested.set()
            with contextlib.suppress(Exception):
                os.remove(path)
            raise
        finally:
            if semaphore_acquired and not release_deferred:
                semaphore.release()

    async def _generate_gtts_file_with_priority(
        self,
        text: str,
        language: str,
        *,
        foreground: bool,
    ) -> str:
        """Call the gTTS generator without breaking older mixin overrides.

        The priority keyword is an internal optimization added after the
        original helper became a convenient extension point in tests and
        deployments. A legacy override rejects the keyword before executing,
        so retrying that one precise signature error is side-effect free.
        """
        generator = self._generate_gtts_file
        signature_target = getattr(generator, "side_effect", None)
        if not callable(signature_target):
            signature_target = generator
        try:
            parameters = inspect.signature(signature_target).parameters.values()
            supports_foreground = any(
                parameter.name == "foreground"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_foreground = True

        if not supports_foreground:
            return await generator(text, language)
        try:
            return await generator(
                text,
                language,
                foreground=foreground,
            )
        except TypeError as exc:
            message = str(exc)
            if "foreground" not in message or "unexpected keyword" not in message:
                raise
            return await generator(text, language)

    async def _accept_gtts_stream_chunk(self, handle: EdgeStreamHandle, data: bytes) -> bool:
        if not data or handle.stop_requested.is_set() or handle.consumer_abandoned:
            return False
        await self._edge_stream_enqueue(handle, data)
        if handle.stop_requested.is_set() or handle.consumer_abandoned:
            return False
        handle.audio_bytes += len(data)
        if not handle.first_audio_ready.is_set():
            handle.first_audio_ms = max(0.0, (time.monotonic() - handle.started_at) * 1000.0)
            handle.first_audio_ready.set()
        return True

    def _gtts_stream_blocking(
        self,
        handle: EdgeStreamHandle,
        loop: asyncio.AbstractEventLoop,
        language: str,
        tld: str,
    ) -> None:
        tts = gTTS(
            text=handle.item.text,
            lang=language,
            tld=tld,
            timeout=(TTS_GTTS_CONNECT_TIMEOUT_SECONDS, TTS_GTTS_READ_TIMEOUT_SECONDS),
        )
        if not callable(getattr(tts, "stream", None)):
            raise RuntimeError("versão instalada do gTTS não oferece stream()")

        output = None
        try:
            for raw_chunk in self._iter_gtts_audio_chunks(tts):
                if handle.stop_requested.is_set():
                    return
                chunk = bytes(raw_chunk or b"")
                if not chunk:
                    continue
                bridge = asyncio.run_coroutine_threadsafe(
                    self._accept_gtts_stream_chunk(handle, chunk),
                    loop,
                )
                while True:
                    try:
                        accepted = bool(bridge.result(timeout=0.1))
                        break
                    except concurrent.futures.TimeoutError:
                        if handle.stop_requested.is_set():
                            bridge.cancel()
                            return
                if not accepted:
                    return
                if output is None and handle.part_path:
                    try:
                        output = open(
                            handle.part_path,
                            "xb",
                            buffering=TTS_EDGE_STREAM_CACHE_BUFFER_BYTES,
                        )
                    except OSError as exc:
                        handle.cache_error = exc
                        failed_path = handle.part_path
                        handle.part_path = ""
                        with contextlib.suppress(FileNotFoundError, OSError):
                            os.remove(failed_path)
                if output is not None:
                    try:
                        output.write(chunk)
                    except OSError as exc:
                        handle.cache_error = exc
                        output.close()
                        output = None
                        failed_path = handle.part_path
                        handle.part_path = ""
                        with contextlib.suppress(FileNotFoundError, OSError):
                            os.remove(failed_path)
        finally:
            if output is not None:
                output.close()

    async def _gtts_stream_producer(
        self,
        handle: EdgeStreamHandle,
        language: str,
        tld: str,
    ) -> None:
        stream_ok = False
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._get_gtts_executor(),
                self._gtts_stream_blocking,
                handle,
                loop,
                language,
                tld,
            )
            handle.blocking_future = future
            await asyncio.wait_for(asyncio.shield(future), timeout=TTS_GTTS_TIMEOUT_SECONDS)
            if handle.audio_bytes <= 0:
                raise RuntimeError("gTTS não enviou áudio")
            stream_ok = True
        except asyncio.CancelledError:
            handle.stop_requested.set()
            future = handle.blocking_future
            if future is not None and not future.done():
                future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise
        except Exception as exc:
            handle.stop_requested.set()
            future = handle.blocking_future
            if future is not None:
                if future.done():
                    with contextlib.suppress(BaseException):
                        future.exception()
                else:
                    future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            handle.error = exc
            duration_ms = max(0.0, (time.monotonic() - handle.started_at) * 1000.0)
            self._record_engine_failure("gtts", exc, duration_ms=duration_ms)
            metrics = self._get_metrics_store()
            metrics["gtts_stream_failures"] = int(metrics.get("gtts_stream_failures", 0) or 0) + 1
            logger.warning(
                "[tts_gtts_stream] gTTS encerrou antes do áudio completo | guild=%s bytes=%s erro=%s",
                handle.item.guild_id,
                handle.audio_bytes,
                exc,
            )
        finally:
            self._release_edge_stream_slot(handle)
            if not handle.first_audio_ready.is_set():
                handle.first_audio_ready.set()
            with contextlib.suppress(asyncio.CancelledError):
                await self._signal_stream_end(handle)

        if not stream_ok:
            if handle.part_path:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(handle.part_path)
            return

        duration_ms = max(0.0, (time.monotonic() - handle.started_at) * 1000.0)
        self._record_engine_success("gtts", duration_ms)
        metrics = self._get_metrics_store()
        metrics["gtts_stream_completed"] = int(metrics.get("gtts_stream_completed", 0) or 0) + 1
        metrics["gtts_stream_audio_bytes"] = int(metrics.get("gtts_stream_audio_bytes", 0) or 0) + int(handle.audio_bytes)
        self._schedule_persistent_synt_success(handle.item.guild_id, "gtts")

        self._schedule_progressive_stream_cache(handle)

    async def _prepare_gtts_stream(
        self,
        state: GuildTTSState,
        item: QueueItem,
        *,
        store_in_cache: bool,
        tld: str = "com",
    ) -> EdgeStreamHandle:
        language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace("_", "-")
        if language == "pt-br":
            language = "pt"
        tld = str(tld or "com").strip() or "com"
        started_at = time.monotonic()
        metrics = self._get_metrics_store()
        metrics["gtts_stream_started"] = int(metrics.get("gtts_stream_started", 0) or 0) + 1

        semaphore = self._get_gtts_semaphore()
        is_prefetch = bool(getattr(item, "_tts_prefetch", False))
        await semaphore.acquire(foreground=not is_prefetch)
        part_path = ""
        fifo_path = ""
        handle: EdgeStreamHandle | None = None
        try:
            if store_in_cache:
                part_path = self._make_runtime_unique_path(suffix=".edge-stream.tmp")
            fifo_path = self._make_edge_stream_fifo()
            handle = EdgeStreamHandle(
                fifo_path=fifo_path,
                part_path=part_path,
                cache_key=self._cache_key(item),
                state=state,
                item=item,
                queue=asyncio.Queue(maxsize=TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS),
                store_in_cache=bool(store_in_cache),
                started_at=started_at,
                first_audio_ms=0.0,
                engine="gtts",
                semaphore=semaphore,
            )
            self._get_edge_stream_handles()[os.path.abspath(fifo_path)] = handle
            handle.producer_task = asyncio.create_task(
                self._gtts_stream_producer(handle, language, tld)
            )
            await asyncio.wait_for(
                handle.first_audio_ready.wait(),
                timeout=TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS,
            )
            if handle.audio_bytes <= 0 or handle.error is not None:
                raise handle.error or RuntimeError("gTTS não enviou o primeiro áudio")
            self._record_average_metric(
                "gtts_stream_first_audio_total_ms",
                "gtts_stream_first_audio_samples",
                handle.first_audio_ms,
            )
            self._record_latency_sample("gtts_first_audio", handle.first_audio_ms)
            self._log_debug(
                f"[tts_gtts_stream] pronto para playback | guild={item.guild_id} first_audio_ms={handle.first_audio_ms:.1f}"
            )
            return handle
        except BaseException:
            if handle is not None:
                await self._finalize_edge_stream(handle, cancel=True)
            else:
                semaphore.release()
                for path in (fifo_path, part_path):
                    if path:
                        with contextlib.suppress(FileNotFoundError, OSError):
                            os.remove(path)
            raise

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        voice = validate_voice(voice, getattr(self, "edge_voice_names", set()))
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)

        self._log_debug(
            "[tts_voice] Edge synth | "
            f"voice={voice!r} rate={rate!r} pitch={pitch!r} text={text[:80]!r}"
        )

        path = self._make_runtime_temp_file(suffix=".mp3")
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                pitch=pitch,
                connect_timeout=TTS_EDGE_CONNECT_TIMEOUT_SECONDS,
                receive_timeout=TTS_EDGE_RECEIVE_TIMEOUT_SECONDS,
            )
            async with self._get_synth_semaphore():
                await asyncio.wait_for(communicate.save(path), timeout=TTS_EDGE_TIMEOUT_SECONDS)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _edge_stream_producer(
        self,
        handle: EdgeStreamHandle,
        stream_iterator: Any,
        initial_audio: bytes,
    ) -> None:
        stream_ok = False
        cache_buffer = bytearray(initial_audio) if handle.part_path else None
        try:
            # O cache Edge permanece em memória limitada durante a síntese. Isso
            # evita flush/close de arquivo no event loop que alimenta o FIFO.
            await asyncio.sleep(0)
            async with asyncio.timeout(TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS):
                async for message in stream_iterator:
                    data = self._edge_stream_audio_chunk(message)
                    if not data:
                        continue
                    await self._edge_stream_enqueue(handle, data)
                    handle.audio_bytes += len(data)
                    if cache_buffer is not None:
                        if len(cache_buffer) + len(data) <= TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES:
                            cache_buffer.extend(data)
                        else:
                            handle.cache_error = RuntimeError("áudio Edge excedeu o limite em memória do cache")
                            cache_buffer = None
                            handle.part_path = ""
            stream_ok = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            handle.error = exc
            duration_ms = max(0.0, (time.monotonic() - handle.started_at) * 1000.0)
            self._record_engine_failure("edge", exc, duration_ms=duration_ms)
            metrics = self._get_metrics_store()
            metrics["edge_stream_failures"] = int(metrics.get("edge_stream_failures", 0) or 0) + 1
            logger.warning(
                "[tts_edge_stream] Edge encerrou antes do áudio completo | guild=%s bytes=%s erro=%s",
                handle.item.guild_id,
                handle.audio_bytes,
                exc,
            )
        finally:
            close_stream = getattr(stream_iterator, "aclose", None)
            if callable(close_stream):
                with contextlib.suppress(BaseException):
                    await close_stream()
            self._release_edge_stream_slot(handle)
            with contextlib.suppress(asyncio.CancelledError):
                await self._signal_stream_end(handle)

        if not stream_ok:
            if handle.part_path:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.remove(handle.part_path)
            return

        handle.cache_buffer = cache_buffer

        duration_ms = max(0.0, (time.monotonic() - handle.started_at) * 1000.0)
        self._record_engine_success("edge", duration_ms)
        metrics = self._get_metrics_store()
        metrics["edge_stream_completed"] = int(metrics.get("edge_stream_completed", 0) or 0) + 1
        metrics["edge_stream_audio_bytes"] = int(metrics.get("edge_stream_audio_bytes", 0) or 0) + int(handle.audio_bytes)
        self._schedule_persistent_synt_success(handle.item.guild_id, "edge")

        self._schedule_progressive_stream_cache(handle)

    async def _prepare_edge_stream(
        self,
        state: GuildTTSState,
        item: QueueItem,
        *,
        store_in_cache: bool,
    ) -> EdgeStreamHandle:
        voice = validate_voice(item.voice, getattr(self, "edge_voice_names", set()))
        rate = self._normalize_edge_rate(item.rate)
        pitch = self._normalize_edge_pitch(item.pitch)
        started_at = time.monotonic()
        metrics = self._get_metrics_store()
        metrics["edge_stream_started"] = int(metrics.get("edge_stream_started", 0) or 0) + 1

        semaphore = self._get_synth_semaphore()
        prefetch_semaphore: asyncio.Semaphore | None = None
        semaphore_acquired = False
        prefetch_acquired = False
        stream_iterator = None
        part_path = ""
        fifo_path = ""
        handle: EdgeStreamHandle | None = None
        slot_wait_ms = 0.0
        try:
            is_prefetch = bool(getattr(item, "_tts_prefetch", False))
            if is_prefetch:
                prefetch_semaphore = self._get_edge_prefetch_semaphore()
                promotion_event = getattr(item, "_tts_foreground_event", None)
                if not isinstance(promotion_event, asyncio.Event):
                    promotion_event = asyncio.Event()
                    setattr(item, "_tts_foreground_event", promotion_event)
                prefetch_waiter = asyncio.create_task(prefetch_semaphore.acquire())
                promotion_waiter = asyncio.create_task(promotion_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {prefetch_waiter, promotion_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if prefetch_waiter in done:
                        await prefetch_waiter
                        prefetch_acquired = True
                    else:
                        prefetch_waiter.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await prefetch_waiter
                finally:
                    promotion_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await promotion_waiter

                if promotion_event.is_set():
                    is_prefetch = False
                    if prefetch_acquired:
                        prefetch_semaphore.release()
                        prefetch_acquired = False
                        prefetch_semaphore = None
            slot_wait_started_at = time.monotonic()
            await semaphore.acquire(foreground=not is_prefetch)
            slot_wait_ms = max(0.0, (time.monotonic() - slot_wait_started_at) * 1000.0)
            semaphore_acquired = True
            promotion_event = getattr(item, "_tts_foreground_event", None)
            if isinstance(promotion_event, asyncio.Event) and promotion_event.is_set():
                is_prefetch = False
                if prefetch_acquired and prefetch_semaphore is not None:
                    prefetch_semaphore.release()
                    prefetch_acquired = False
                    prefetch_semaphore = None
            self._record_average_metric(
                "edge_slot_wait_total_ms",
                "edge_slot_wait_samples",
                slot_wait_ms,
            )
            self._record_latency_sample("edge_slot_wait", slot_wait_ms)
            network_started_at = time.monotonic()
            communicate = edge_tts.Communicate(
                text=item.text,
                voice=voice,
                rate=rate,
                pitch=pitch,
                connect_timeout=TTS_EDGE_CONNECT_TIMEOUT_SECONDS,
                receive_timeout=TTS_EDGE_RECEIVE_TIMEOUT_SECONDS,
            )
            stream_iterator = communicate.stream().__aiter__()
            # edge-tts usa MP3 48 kbps: aproximadamente seis bytes por ms.
            prebuffer_ms, prebuffer_profile_key = self._edge_prebuffer_ms(item)
            prebuffer_bytes = max(1024, int(6 * prebuffer_ms))
            initial_parts: list[bytes] = []
            initial_size = 0
            first_chunk_bytes = 0
            network_first_audio_ms = 0.0
            first_chunk_at = 0.0
            deadline = time.monotonic() + TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS
            while initial_size < prebuffer_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                try:
                    message = await asyncio.wait_for(stream_iterator.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                data = self._edge_stream_audio_chunk(message)
                if not data:
                    continue
                if not initial_parts:
                    first_chunk_at = time.monotonic()
                    first_chunk_bytes = len(data)
                    network_first_audio_ms = max(
                        0.0,
                        (first_chunk_at - network_started_at) * 1000.0,
                    )
                initial_parts.append(data)
                initial_size += len(data)

            if initial_size <= 0:
                raise RuntimeError("Edge não enviou áudio no início do stream")

            prebuffer_ready_at = time.monotonic()
            prebuffer_fill_ms = max(
                0.0,
                (prebuffer_ready_at - (first_chunk_at or prebuffer_ready_at)) * 1000.0,
            )
            if store_in_cache:
                part_path = self._make_runtime_unique_path(suffix=".edge-stream.tmp")
            fifo_path = self._make_edge_stream_fifo()

            first_audio_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
            local_handoff_ms = max(0.0, (time.monotonic() - prebuffer_ready_at) * 1000.0)
            initial_audio = initial_parts[0] if len(initial_parts) == 1 else b"".join(initial_parts)
            handle = EdgeStreamHandle(
                fifo_path=fifo_path,
                part_path=part_path,
                cache_key=self._cache_key(item),
                state=state,
                item=item,
                queue=asyncio.Queue(maxsize=TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS),
                store_in_cache=bool(store_in_cache),
                started_at=started_at,
                first_audio_ms=first_audio_ms,
                semaphore=semaphore,
                prefetch_semaphore=prefetch_semaphore,
                audio_bytes=initial_size,
                first_chunk_bytes=first_chunk_bytes,
                network_first_audio_ms=network_first_audio_ms,
                prebuffer_fill_ms=prebuffer_fill_ms,
                local_handoff_ms=local_handoff_ms,
                synth_slot_wait_ms=slot_wait_ms,
                prebuffer_ms=prebuffer_ms,
                prebuffer_profile_key=prebuffer_profile_key,
            )
            handle.first_audio_ready.set()
            # Um único item inicial evita bloquear a preparação mesmo se o SDK
            # entregar um fragmento maior que o tamanho normal dos chunks.
            handle.queue.put_nowait(initial_audio)
            self._get_edge_stream_handles()[os.path.abspath(fifo_path)] = handle
            setattr(item, "_edge_stream_fifo_path", fifo_path)
            self._record_average_metric(
                "edge_stream_first_audio_total_ms",
                "edge_stream_first_audio_samples",
                first_audio_ms,
            )
            self._record_latency_sample("edge_first_audio", first_audio_ms)
            self._record_average_metric(
                "edge_network_first_audio_total_ms",
                "edge_network_first_audio_samples",
                network_first_audio_ms,
            )
            self._record_average_metric(
                "edge_local_handoff_total_ms",
                "edge_local_handoff_samples",
                local_handoff_ms,
            )
            self._record_average_metric(
                "edge_prebuffer_fill_total_ms",
                "edge_prebuffer_fill_samples",
                prebuffer_fill_ms,
            )
            metrics["edge_first_chunk_bytes_total"] = int(metrics.get("edge_first_chunk_bytes_total", 0) or 0) + first_chunk_bytes
            metrics["edge_first_chunk_bytes_samples"] = int(metrics.get("edge_first_chunk_bytes_samples", 0) or 0) + 1
            self._record_latency_sample("edge_network_first_audio", network_first_audio_ms)
            self._record_latency_sample("edge_local_handoff", local_handoff_ms)
            self._record_latency_sample("edge_prebuffer_fill", prebuffer_fill_ms)
            priority_label = "prefetch" if bool(getattr(item, "_tts_prefetch", False)) else "foreground"
            self._record_latency_sample(f"edge_network_first_audio:{priority_label}", network_first_audio_ms)
            self._record_latency_sample("edge_ready", first_audio_ms)
            handle.producer_task = asyncio.create_task(
                self._edge_stream_producer(handle, stream_iterator, initial_audio)
            )
            self._log_debug(
                f"[tts_edge_stream] pronto para playback | guild={item.guild_id} first_audio_ms={first_audio_ms:.1f} prebuffer_ms={prebuffer_ms} prebuffer={initial_size}"
            )
            return handle
        except BaseException as exc:
            if fifo_path:
                self._get_edge_stream_handles().pop(os.path.abspath(fifo_path), None)
            if stream_iterator is not None:
                close_stream = getattr(stream_iterator, "aclose", None)
                if callable(close_stream):
                    with contextlib.suppress(BaseException):
                        await close_stream()
            if semaphore_acquired:
                semaphore.release()
            if prefetch_acquired and prefetch_semaphore is not None:
                prefetch_semaphore.release()
            for path in (fifo_path, part_path):
                if path:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        os.remove(path)
            if isinstance(exc, asyncio.CancelledError):
                raise
            duration_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
            self._record_engine_failure("edge", exc, duration_ms=duration_ms)
            metrics["edge_stream_failures"] = int(metrics.get("edge_stream_failures", 0) or 0) + 1
            raise


    def _phone_worker_tts_benchmark_base_url(self) -> str:
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return ""
        scheme = PHONE_WORKER_SCHEME if PHONE_WORKER_SCHEME in {"http", "https"} else "http"
        return f"{scheme}://{PHONE_WORKER_HOST}:{PHONE_WORKER_PORT}"

    def _phone_worker_tts_base_url(self) -> str:
        return self._phone_worker_tts_benchmark_base_url()

    def _normalize_worker_audio_format(self, value: Any) -> str:
        fmt = str(value or "mp3").strip().lower().replace(".", "")
        if fmt in {"wav", "wave"}:
            return "wav"
        if fmt in {"ogg", "opus", "ogg_opus", "oggopus"}:
            return "ogg"
        return "mp3"

    async def _request_phone_worker_json(self, *, task: str, payload: dict[str, Any], timeout_seconds: float, max_audio_mb: int, raise_on_worker_error: bool = True) -> dict[str, Any]:
        base = self._phone_worker_tts_base_url()
        if not base:
            raise RuntimeError("PHONE_WORKER_ENABLED/HOST/TOKEN não configurado")
        headers = {
            "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        request_payload = dict(payload)
        request_payload["task"] = task
        max_audio_bytes = max(1, int(max_audio_mb)) * 1024 * 1024
        request_payload.setdefault("max_audio_bytes", max_audio_bytes)
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds)))
        session = await self._get_phone_worker_http_session()
        async with session.post(f"{base}/task", headers=headers, json=request_payload, timeout=timeout) as response:
            response_text = await response.text()
            response_ms = (time.monotonic() - started) * 1000.0
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {response_text[:260]}")
            parse_started = time.monotonic()
            data = json.loads(response_text or "{}")
            parse_ms = (time.monotonic() - parse_started) * 1000.0
        data["total_ms"] = round((time.monotonic() - started) * 1000.0, 2)
        data["_vps_worker_request_ms"] = round(response_ms, 2)
        data["_vps_worker_json_parse_ms"] = round(parse_ms, 2)
        data["_vps_worker_response_bytes"] = len(response_text.encode("utf-8", errors="ignore"))
        data["audio_format"] = self._normalize_worker_audio_format(data.get("audio_format"))
        if raise_on_worker_error and data.get("ok") is False:
            raise RuntimeError(str(data.get("error") or "worker retornou ok=false"))
        return data

    def _decode_worker_audio_payload(self, data: dict[str, Any], *, max_audio_mb: int) -> dict[str, Any]:
        max_audio_bytes = max(1, int(max_audio_mb)) * 1024 * 1024
        out_b64 = str(data.get("data_b64") or "")
        if not out_b64:
            raise RuntimeError("worker não retornou data_b64")
        decode_started = time.monotonic()
        raw = base64.b64decode(out_b64.encode("ascii"), validate=True)
        decode_ms = (time.monotonic() - decode_started) * 1000.0
        if not raw:
            raise RuntimeError("worker retornou áudio vazio")
        if len(raw) > max_audio_bytes:
            raise RuntimeError(f"worker retornou áudio grande demais: {len(raw)} bytes")
        expected_hash = str(data.get("sha256") or "")
        actual_hash = hashlib.sha256(raw).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise RuntimeError("sha256 do áudio retornado não confere")
        data["raw_audio"] = raw
        data["sha256"] = actual_hash
        data["audio_format"] = self._normalize_worker_audio_format(data.get("audio_format"))
        data["_vps_audio_decode_ms"] = round(decode_ms, 2)
        return data

    def _worker_header_value(self, headers: Any, name: str, default: str = "") -> str:
        return str(headers.get(name) or headers.get(name.lower()) or default or "").strip()

    async def _request_phone_worker_raw_audio(
        self,
        *,
        payload: dict[str, Any],
        timeout_seconds: float,
        max_audio_mb: int,
        stream_to_file: bool = False,
    ) -> dict[str, Any]:
        base = self._phone_worker_tts_base_url()
        if not base:
            raise RuntimeError("PHONE_WORKER_ENABLED/HOST/TOKEN não configurado")
        max_audio_bytes = max(1, int(max_audio_mb)) * 1024 * 1024
        request_payload = dict(payload)
        request_payload.setdefault("max_audio_bytes", max_audio_bytes)
        headers = {
            "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "audio/ogg,audio/mpeg,audio/wav,application/octet-stream,application/json;q=0.4,*/*;q=0.1",
        }
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds)))
        started = time.monotonic()
        session = await self._get_phone_worker_http_session()
        async with session.post(f"{base}/tts-agent/synthesize.raw", headers=headers, json=request_payload, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if response.status in {404, 405}:
                text = await response.text()
                raise RuntimeError(f"raw_endpoint_unavailable: HTTP {response.status}: {text[:120]}")
            if "application/json" in content_type:
                text = await response.text()
                try:
                    parsed = json.loads(text or "{}")
                except Exception:
                    parsed = {"ok": False, "error": text[:240]}
                if response.status < 200 or response.status >= 300 or parsed.get("ok") is False:
                    raise RuntimeError(str(parsed.get("error") or f"HTTP {response.status}: {text[:180]}"))
                parsed["_vps_worker_request_ms"] = round((time.monotonic() - started) * 1000.0, 2)
                return await asyncio.to_thread(
                    self._decode_worker_audio_payload,
                    parsed,
                    max_audio_mb=max_audio_mb,
                )
            if response.status < 200 or response.status >= 300:
                raw_error = await response.content.read(180)
                raise RuntimeError(f"HTTP {response.status}: {raw_error!r}")

            audio_format = self._normalize_worker_audio_format(
                self._worker_header_value(response.headers, "X-Core-Worker-Audio-Format", "mp3")
            )
            expected_hash = self._worker_header_value(response.headers, "X-Core-Worker-Sha256")
            actual_hash = ""
            raw: bytes | None = None
            audio_path = ""
            audio_size = 0

            if stream_to_file:
                suffix = ".wav" if audio_format == "wav" else ".ogg" if audio_format == "ogg" else ".mp3"
                audio_path = self._make_runtime_temp_file(suffix=suffix)
                hasher = hashlib.sha256()
                try:
                    with open(audio_path, "wb") as handle:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if not chunk:
                                continue
                            audio_size += len(chunk)
                            if audio_size > max_audio_bytes:
                                raise RuntimeError(f"worker raw retornou áudio grande demais: {audio_size} bytes")
                            handle.write(chunk)
                            hasher.update(chunk)
                    if audio_size <= 0:
                        raise RuntimeError("worker raw não retornou áudio")
                    actual_hash = hasher.hexdigest()
                except Exception:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        os.remove(audio_path)
                    raise
            else:
                raw = await response.read()
                audio_size = len(raw)
                if not raw:
                    raise RuntimeError("worker raw não retornou áudio")
                if audio_size > max_audio_bytes:
                    raise RuntimeError(f"worker raw retornou áudio grande demais: {audio_size} bytes")
                actual_hash = hashlib.sha256(raw).hexdigest()

            request_ms = (time.monotonic() - started) * 1000.0
            if expected_hash and expected_hash != actual_hash:
                if audio_path:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        os.remove(audio_path)
                raise RuntimeError("sha256 do áudio raw retornado não confere")

            timing_ms: dict[str, Any] = {}
            for header_name, key in (
                ("X-Core-Worker-Worker-Total-Ms", "worker_total"),
                ("X-Core-Worker-Worker-Synth-Ms", "worker_synth"),
                ("X-Core-Worker-Cache-Read-Ms", "cache_read"),
                ("X-Core-Worker-Android-Synth-Ms", "android_synth"),
                ("X-Core-Worker-Android-Roundtrip-Ms", "android_roundtrip"),
            ):
                value = self._parse_header_float(self._worker_header_value(response.headers, header_name))
                if value is not None:
                    timing_ms[key] = round(value, 2)
            data = {
                "ok": True,
                "audio_format": audio_format,
                "engine": self._worker_header_value(response.headers, "X-Core-Worker-Engine"),
                "selected_engine": self._worker_header_value(response.headers, "X-Core-Worker-Selected-Engine") or self._worker_header_value(response.headers, "X-Core-Worker-Engine"),
                "cache_hit": self._parse_header_bool(self._worker_header_value(response.headers, "X-Core-Worker-Cache-Hit")),
                "sha256": actual_hash,
                "worker_id": self._worker_header_value(response.headers, "X-Core-Worker-Id"),
                "worker_version": self._worker_header_value(response.headers, "X-Core-Worker-Version"),
                "worker_total_ms": timing_ms.get("worker_total", 0.0),
                "worker_synth_ms": timing_ms.get("worker_synth", 0.0),
                "timing_ms": timing_ms,
                "_vps_worker_request_ms": round(request_ms, 2),
                "_vps_worker_json_parse_ms": 0.0,
                "_vps_audio_decode_ms": 0.0,
                "_vps_worker_response_bytes": audio_size,
                "audio_bytes_len": audio_size,
            }
            if audio_path:
                data["audio_path"] = audio_path
            elif raw is not None:
                data["raw_audio"] = raw
                data["audio_bytes"] = raw
            return data

    async def _request_phone_worker_tts_audio(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        max_audio_mb: int,
        stream_to_file: bool = False,
    ) -> dict[str, Any]:
        if TTS_WORKER_AGENT_RAW_AUDIO_ENABLED and task == "tts_agent_synthesize":
            try:
                return await self._request_phone_worker_raw_audio(
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                    max_audio_mb=max_audio_mb,
                    stream_to_file=stream_to_file,
                )
            except Exception as exc:
                if "raw_endpoint_unavailable" not in str(exc):
                    raise
                self._log_debug(f"[tts_agent] raw audio indisponível; tentando JSON/base64: {exc}")
        data = await self._request_phone_worker_json(
            task=task,
            payload=payload,
            timeout_seconds=timeout_seconds,
            max_audio_mb=max_audio_mb,
            raise_on_worker_error=True,
        )
        return await asyncio.to_thread(
            self._decode_worker_audio_payload,
            data,
            max_audio_mb=max_audio_mb,
        )


    def _worker_voice_agent_session_reports(self) -> dict[int, dict[str, Any]]:
        reports = getattr(self, "_worker_voice_agent_session_report_cache", None)
        if not isinstance(reports, dict):
            reports = {}
            setattr(self, "_worker_voice_agent_session_report_cache", reports)
        return reports

    def _record_worker_voice_session_metric(self, key: str) -> None:
        metrics = self._get_metrics_store()
        metric_key = f"worker_voice_session_{key}"
        metrics[metric_key] = int(metrics.get(metric_key, 0) or 0) + 1

    def _worker_voice_agent_reports_enabled(self) -> bool:
        return bool(
            WORKER_VOICE_AGENT_ENABLED
            and WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED
            and WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED
            and self._tts_agent_base_configured()
        )

    def _clean_worker_voice_endpoint(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
        text = text.split("?", 1)[0].strip("/")
        return text[:180]

    def _get_tts_playback_lock(self, guild_id: int) -> asyncio.Lock:
        state = self._get_state(int(guild_id))
        lock = getattr(state, "playback_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            state.playback_lock = lock
        return lock

    def _worker_voice_current_owner(self, vc: Any | None) -> str:
        if vc is not None and self._voice_client_is_connected(vc):
            return "vps"
        return "none"

    def _worker_voice_probe_allowed_for_payload(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if not WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED:
            return False, "connection_dry_run_disabled"
        if not WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED:
            return False, "automatic_probe_disabled"
        if str(payload.get("voice_owner") or payload.get("transport_owner") or "vps").lower() != "worker":
            return False, "waiting_for_voice_ownership"
        if not bool(payload.get("allow_connection_probe") or payload.get("allow_probe")):
            return False, "probe_not_authorized_by_vps"
        return True, "allowed"

    def _voice_client_public_session_payload(self, guild: discord.Guild, item: QueueItem, vc: Any | None, *, source: str) -> dict[str, Any]:
        channel = self._voice_client_channel(vc) if vc is not None else None
        me = getattr(guild, "me", None)
        me_voice = getattr(me, "voice", None)
        session_id = getattr(vc, "session_id", None) or getattr(me_voice, "session_id", None)
        endpoint = getattr(vc, "endpoint", None) or getattr(vc, "_endpoint", None)
        token = getattr(vc, "token", None) or getattr(vc, "_token", None)
        now_ms = int(time.time() * 1000)
        state = self.guild_states.get(int(guild.id))
        text_channel_id = int(getattr(state, "last_text_channel_id", 0) or 0) if state is not None else 0
        return {
            "guild_id": int(guild.id),
            "channel_id": int(getattr(channel, "id", None) or item.channel_id or 0),
            "text_channel_id": text_channel_id,
            "requester_id": int(item.author_id or 0),
            "bot_user_id": int(getattr(me, "id", 0) or 0),
            "source": str(source or "tts").strip().lower()[:40] or "tts",
            "state": "vps_voice_session_observed",
            "registered_by": "vps_control_plane",
            "expires_in_seconds": int(WORKER_VOICE_AGENT_SESSION_TTL_SECONDS),
            "observed_at_ms": now_ms,
            "direct_tts_enabled": False,
            "voice_owner": self._worker_voice_current_owner(vc),
            "transport_owner": self._worker_voice_current_owner(vc),
            "connection_policy": "vps_owned_wait_for_transfer",
            "discord_voice": {
                "connected": bool(vc is not None and self._voice_client_is_connected(vc)),
                "channel_id": int(getattr(channel, "id", None) or item.channel_id or 0),
                "session_id_present": bool(session_id),
                "endpoint_present": bool(endpoint),
                "endpoint_host": self._clean_worker_voice_endpoint(endpoint),
                "voice_token_present": bool(token),
                "self_deaf": bool(getattr(me_voice, "self_deaf", False)) if me_voice is not None else None,
                "self_mute": bool(getattr(me_voice, "self_mute", False)) if me_voice is not None else None,
            },
            "note": "registro seguro; não contém DISCORD_TOKEN nem voice token bruto",
        }

    def _voice_client_handoff_payload(self, guild: discord.Guild, item: QueueItem, vc: Any | None, *, source: str) -> dict[str, Any] | None:
        if not WORKER_VOICE_AGENT_HANDOFF_ENABLED or vc is None:
            return None
        channel = self._voice_client_channel(vc)
        me = getattr(guild, "me", None)
        me_voice = getattr(me, "voice", None)
        session_id = str(getattr(vc, "session_id", None) or getattr(me_voice, "session_id", None) or "").strip()
        endpoint = str(getattr(vc, "endpoint", None) or getattr(vc, "_endpoint", None) or "").strip()
        token = str(getattr(vc, "token", None) or getattr(vc, "_token", None) or "").strip()
        # Sem esses três campos o worker ainda não conseguiria abrir a conexão de voz
        # no futuro. No dry-run o worker só guarda isso em memória, com TTL curto.
        if not (session_id and endpoint and token):
            return None
        now_ms = int(time.time() * 1000)
        return {
            "guild_id": int(guild.id),
            "channel_id": int(getattr(channel, "id", None) or item.channel_id or 0),
            "text_channel_id": int(getattr(self.guild_states.get(int(guild.id)), "last_text_channel_id", 0) or 0) if self.guild_states.get(int(guild.id)) is not None else 0,
            "requester_id": int(item.author_id or 0),
            "bot_user_id": int(getattr(me, "id", 0) or 0),
            "source": str(source or "tts").strip().lower()[:40] or "tts",
            "state": "voice_handoff_observed_dry_run",
            "registered_by": "vps_control_plane",
            "dry_run": True,
            "voice_owner": self._worker_voice_current_owner(vc),
            "transport_owner": self._worker_voice_current_owner(vc),
            "allow_connection_probe": False,
            "connection_policy": "handoff_only_wait_for_voice_ownership",
            "expires_in_seconds": int(WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS),
            "observed_at_ms": now_ms,
            "discord_voice_handoff": {
                "session_id": session_id,
                "endpoint": self._clean_worker_voice_endpoint(endpoint),
                "voice_token": token,
                "channel_id": int(getattr(channel, "id", None) or item.channel_id or 0),
                "guild_id": int(guild.id),
            },
            "note": "handoff temporário; sem DISCORD_TOKEN; worker guarda somente em memória",
        }

    def _voice_client_transfer_prepare_payload(self, guild: discord.Guild, item: QueueItem, vc: Any | None, *, source: str) -> dict[str, Any] | None:
        if not (WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED and WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED):
            return None
        if vc is None:
            return None
        channel = self._voice_client_channel(vc)
        if channel is None:
            return None
        now_ms = int(time.time() * 1000)
        return {
            "guild_id": int(guild.id),
            "channel_id": int(getattr(channel, "id", None) or item.channel_id or 0),
            "text_channel_id": int(getattr(self.guild_states.get(int(guild.id)), "last_text_channel_id", 0) or 0) if self.guild_states.get(int(guild.id)) is not None else 0,
            "requester_id": int(item.author_id or 0),
            "bot_user_id": int(getattr(getattr(guild, "me", None), "id", 0) or 0),
            "source": str(source or "tts").strip().lower()[:40] or "tts",
            "state": "transfer_staged_waiting_vps_release",
            "current_owner": self._worker_voice_current_owner(vc),
            "voice_owner": self._worker_voice_current_owner(vc),
            "requested_owner": "worker",
            "allow_connection_probe": False,
            "connection_policy": "prepare_only_no_connection_until_vps_releases",
            "expires_in_seconds": int(WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS),
            "observed_at_ms": now_ms,
            "reason": "TTS worker route active; preparando transferência controlada sem abrir voice ws",
            "note": "preparação segura; não transfere posse, não abre conexão e não toca áudio",
        }

    def _compact_worker_voice_agent_snapshot(self, voice_agent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(voice_agent, dict) or not voice_agent:
            return {}
        compact = {
            "ok": bool(voice_agent.get("ok")),
            "available": bool(voice_agent.get("available")),
            "state": str(voice_agent.get("state") or "")[:80],
            "direct_tts_enabled": bool(voice_agent.get("direct_tts_enabled")),
            "direct_tts_ready": bool(voice_agent.get("direct_tts_ready")),
            "shared_session_enabled": bool(voice_agent.get("shared_session_enabled")),
            "shared_session_ready": bool(voice_agent.get("shared_session_ready")),
            "music_ready": bool(voice_agent.get("music_ready")),
            "tts_ready": bool(voice_agent.get("tts_ready")),
            "voice_transport": str(voice_agent.get("voice_transport") or "")[:80],
            "ducking_ready": bool(voice_agent.get("ducking_ready")),
            "session_count": int(voice_agent.get("session_count") or 0),
            "handoff_count": int(voice_agent.get("handoff_count") or 0),
            "handoff_ready": bool(voice_agent.get("handoff_ready")),
            "connection_count": int(voice_agent.get("connection_count") or 0),
            "connection_ready_count": int(voice_agent.get("connection_ready_count") or 0),
            "connection_probing_count": int(voice_agent.get("connection_probing_count") or 0),
            "connection_failed_count": int(voice_agent.get("connection_failed_count") or 0),
            "connection_ready": bool(voice_agent.get("connection_ready")),
            "connection_auto_probe_enabled": bool(voice_agent.get("connection_auto_probe_enabled")),
            "active_guilds": [str(item)[:32] for item in list(voice_agent.get("active_guilds") or [])[:8]],
            "handoff_guilds": [str(item)[:32] for item in list(voice_agent.get("handoff_guilds") or [])[:8]],
            "last_session": dict(voice_agent.get("last_session") or {}) if isinstance(voice_agent.get("last_session"), dict) else {},
            "last_handoff": dict(voice_agent.get("last_handoff") or {}) if isinstance(voice_agent.get("last_handoff"), dict) else {},
            "last_connection": dict(voice_agent.get("last_connection") or {}) if isinstance(voice_agent.get("last_connection"), dict) else {},
            "transfer_count": int(voice_agent.get("transfer_count") or 0),
            "transfer_ready": bool(voice_agent.get("transfer_ready")),
            "transfer_state": str(voice_agent.get("transfer_state") or "")[:80],
            "current_voice_owner": str(voice_agent.get("current_voice_owner") or voice_agent.get("voice_owner") or "")[:40],
            "requested_voice_owner": str(voice_agent.get("requested_voice_owner") or "")[:40],
            "last_transfer": dict(voice_agent.get("last_transfer") or {}) if isinstance(voice_agent.get("last_transfer"), dict) else {},
            "missing": [str(item)[:80] for item in list(voice_agent.get("missing") or [])[:6]],
        }
        sessions = voice_agent.get("sessions")
        if isinstance(sessions, list):
            compact["sessions"] = [dict(item) for item in sessions[:5] if isinstance(item, dict)]
        handoffs = voice_agent.get("handoffs")
        if isinstance(handoffs, list):
            compact["handoffs"] = [dict(item) for item in handoffs[:5] if isinstance(item, dict)]
        connections = voice_agent.get("connections")
        if isinstance(connections, list):
            compact["connections"] = [dict(item) for item in connections[:5] if isinstance(item, dict)]
        transfers = voice_agent.get("transfers")
        if isinstance(transfers, list):
            compact["transfers"] = [dict(item) for item in transfers[:5] if isinstance(item, dict)]
        return compact

    def _update_worker_voice_agent_snapshot(self, voice_agent: dict[str, Any] | None) -> None:
        compact = self._compact_worker_voice_agent_snapshot(voice_agent or {})
        if not compact:
            return
        metrics = self._get_metrics_store()
        metrics["worker_voice_agent"] = compact
        self._tts_agent_route_state()["voice_agent"] = compact

    async def _request_worker_voice_agent_json(self, *, task: str, payload: dict[str, Any], timeout_seconds: float | None = None) -> dict[str, Any]:
        data = await self._request_phone_worker_json(
            task=task,
            payload=payload,
            timeout_seconds=timeout_seconds or WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS,
            max_audio_mb=1,
            raise_on_worker_error=False,
        )
        voice_agent = data.get("voice_agent") if isinstance(data.get("voice_agent"), dict) else data
        if isinstance(voice_agent, dict):
            self._update_worker_voice_agent_snapshot(voice_agent)
        return data

    def _should_report_worker_voice_session(self, guild_id: int, channel_id: int, source: str) -> bool:
        if not self._worker_voice_agent_reports_enabled() or not self._tts_agent_route_available():
            self._record_worker_voice_session_metric("skipped")
            return False
        reports = self._worker_voice_agent_session_reports()
        now = time.monotonic()
        previous = reports.get(int(guild_id)) or {}
        key = f"{int(channel_id)}:{str(source or 'tts')}"
        if previous.get("key") == key and (now - float(previous.get("at", 0.0) or 0.0)) < WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS:
            self._record_worker_voice_session_metric("skipped")
            return False
        reports[int(guild_id)] = {"key": key, "at": now, "pending": True}
        return True

    def _schedule_worker_voice_agent_register_session(self, guild: discord.Guild, item: QueueItem, vc: Any | None, *, source: str = "tts") -> None:
        channel_id = int((getattr(self._voice_client_channel(vc), "id", None) if vc is not None else None) or item.channel_id or 0)
        if not self._should_report_worker_voice_session(int(guild.id), channel_id, source):
            return
        payload = self._voice_client_public_session_payload(guild, item, vc, source=source)
        task = asyncio.create_task(self._worker_voice_agent_register_session(payload))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        handoff_payload = self._voice_client_handoff_payload(guild, item, vc, source=source)
        if handoff_payload:
            htask = asyncio.create_task(self._worker_voice_agent_register_handoff(handoff_payload))
            htask.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            transfer_payload = self._voice_client_transfer_prepare_payload(guild, item, vc, source=source)
            if transfer_payload:
                ttask = asyncio.create_task(self._worker_voice_agent_prepare_transfer(transfer_payload))
                ttask.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            elif WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED:
                self._record_worker_voice_session_metric("transfer_prepare_skipped")
        elif WORKER_VOICE_AGENT_HANDOFF_ENABLED:
            self._record_worker_voice_session_metric("handoff_skipped")

    async def _worker_voice_agent_register_session(self, payload: dict[str, Any]) -> None:
        guild_id = int(payload.get("guild_id") or 0)
        try:
            data = await self._request_worker_voice_agent_json(task="voice_agent_register_session", payload=payload)
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("reports_ok")
                logger.debug(
                    "[worker_voice_agent] sessão de voz registrada | guild=%s channel=%s state=%s",
                    guild_id,
                    payload.get("channel_id"),
                    data.get("state"),
                )
            else:
                self._record_worker_voice_session_metric("reports_failed")
                self._worker_voice_agent_session_reports().pop(guild_id, None)
        except Exception as exc:
            self._record_worker_voice_session_metric("reports_failed")
            self._worker_voice_agent_session_reports().pop(guild_id, None)
            logger.debug("[worker_voice_agent] registro de sessão falhou | guild=%s erro=%s", guild_id, exc)

    async def _worker_voice_agent_register_handoff(self, payload: dict[str, Any]) -> None:
        guild_id = int(payload.get("guild_id") or 0)
        try:
            data = await self._request_worker_voice_agent_json(
                task="voice_agent_register_handoff",
                payload=payload,
                timeout_seconds=WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS,
            )
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("handoff_ok")
                logger.debug(
                    "[worker_voice_agent] handoff dry-run registrado | guild=%s channel=%s state=%s ready=%s",
                    guild_id,
                    payload.get("channel_id"),
                    data.get("state"),
                    data.get("handoff_ready"),
                )
                allowed, reason = self._worker_voice_probe_allowed_for_payload(payload)
                if allowed:
                    ctask = asyncio.create_task(self._worker_voice_agent_probe_connection({
                        "guild_id": guild_id,
                        "channel_id": int(payload.get("channel_id") or 0),
                        "source": str(payload.get("source") or "tts")[:40],
                        "timeout_seconds": WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS,
                        "allow_probe": True,
                        "force": False,
                    }))
                    ctask.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                else:
                    self._record_worker_voice_session_metric("connection_probe_skipped")
                    logger.debug(
                        "[worker_voice_agent] conexão voice não iniciada automaticamente | guild=%s reason=%s",
                        guild_id,
                        reason,
                    )
            else:
                self._record_worker_voice_session_metric("handoff_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("handoff_failed")
            logger.debug("[worker_voice_agent] handoff dry-run falhou | guild=%s erro=%s", guild_id, exc)

    async def _worker_voice_agent_prepare_transfer(self, payload: dict[str, Any]) -> None:
        guild_id = int(payload.get("guild_id") or 0)
        if not WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED:
            self._record_worker_voice_session_metric("transfer_prepare_skipped")
            return
        try:
            data = await self._request_worker_voice_agent_json(
                task="voice_agent_prepare_transfer",
                payload=payload,
                timeout_seconds=WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS,
            )
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("transfer_prepare_ok")
                logger.debug(
                    "[worker_voice_agent] transferência preparada | guild=%s owner=%s requested=%s state=%s",
                    guild_id,
                    payload.get("current_owner") or payload.get("voice_owner"),
                    payload.get("requested_owner"),
                    data.get("state"),
                )
            else:
                self._record_worker_voice_session_metric("transfer_prepare_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("transfer_prepare_failed")
            logger.debug("[worker_voice_agent] preparar transferência falhou | guild=%s erro=%s", guild_id, exc)

    async def _worker_voice_agent_probe_connection(self, payload: dict[str, Any]) -> None:
        guild_id = int(payload.get("guild_id") or 0)
        try:
            data = await self._request_worker_voice_agent_json(
                task="voice_agent_probe_connection",
                payload=payload,
                timeout_seconds=WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS,
            )
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("connection_probe_ok")
                logger.debug(
                    "[worker_voice_agent] conexão voice dry-run iniciada | guild=%s state=%s ready=%s",
                    guild_id,
                    data.get("state"),
                    data.get("connection_ready"),
                )
            else:
                self._record_worker_voice_session_metric("connection_probe_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("connection_probe_failed")
            logger.debug("[worker_voice_agent] conexão voice dry-run falhou ao iniciar | guild=%s erro=%s", guild_id, exc)

    def _schedule_worker_voice_agent_clear_session(self, guild_id: int, *, reason: str = "unknown") -> None:
        if not self._worker_voice_agent_reports_enabled():
            return
        self._worker_voice_agent_session_reports().pop(int(guild_id or 0), None)
        payload = {"guild_id": int(guild_id or 0), "reason": str(reason or "unknown")[:120], "source": "vps_control_plane"}
        task = asyncio.create_task(self._worker_voice_agent_clear_session(payload))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        if WORKER_VOICE_AGENT_HANDOFF_ENABLED:
            htask = asyncio.create_task(self._worker_voice_agent_clear_handoff(payload))
            htask.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _worker_voice_agent_clear_session(self, payload: dict[str, Any]) -> None:
        try:
            data = await self._request_worker_voice_agent_json(task="voice_agent_clear_session", payload=payload)
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("clears_ok")
            else:
                self._record_worker_voice_session_metric("clears_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("clears_failed")
            logger.debug("[worker_voice_agent] limpar sessão falhou | guild=%s erro=%s", payload.get("guild_id"), exc)

    async def _worker_voice_agent_clear_handoff(self, payload: dict[str, Any]) -> None:
        try:
            data = await self._request_worker_voice_agent_json(
                task="voice_agent_clear_handoff",
                payload=payload,
                timeout_seconds=WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS,
            )
            if bool(data.get("ok", True)):
                self._record_worker_voice_session_metric("handoff_clears_ok")
            else:
                self._record_worker_voice_session_metric("handoff_clears_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("handoff_clears_failed")
            logger.debug("[worker_voice_agent] limpar handoff falhou | guild=%s erro=%s", payload.get("guild_id"), exc)

    def _worker_voice_direct_tts_disabled_untils(self) -> dict[int, float]:
        # Não use o mesmo nome do método para guardar o dict. A versão anterior
        # fazia setattr(self, "_worker_voice_direct_tts_disabled_untils", data),
        # o que sombreava o método na instância e causava
        # TypeError: 'dict' object is not callable no próximo TTS.
        data = getattr(self, "_worker_voice_direct_tts_disabled_untils_data", None)
        if not isinstance(data, dict):
            legacy = getattr(self, "__dict__", {}).get("_worker_voice_direct_tts_disabled_untils")
            data = legacy if isinstance(legacy, dict) else {}
            with contextlib.suppress(Exception):
                getattr(self, "__dict__", {}).pop("_worker_voice_direct_tts_disabled_untils", None)
            setattr(self, "_worker_voice_direct_tts_disabled_untils_data", data)
        return data

    def _worker_voice_direct_tts_available_for(self, guild: discord.Guild, item: QueueItem) -> tuple[bool, str]:
        if not (WORKER_VOICE_AGENT_ENABLED and WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED and WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED):
            return False, "direct_tts_disabled"
        if not str(item.text or "").strip():
            return False, "empty_text"
        if len(str(item.text or "")) > WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS:
            return False, "text_too_long_for_direct_tts"
        engine = str(getattr(item, "engine", "") or "gtts").strip().lower().replace("-", "_")
        if engine == "gtts" and not WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED:
            # O handoff de voz custa mais que manter a conexão local quente.
            # O worker de síntese adaptativo continua disponível sem transferir
            # a propriedade da call.
            return False, "gtts_direct_handoff_disabled"
        edge_stream_allowed, _ = self._edge_streaming_allowed_for(item)
        if edge_stream_allowed:
            # Handoff + MP3 completo no worker tem maior latência de início.
            # Música continua usando a rota própria antes deste método.
            return False, "edge_vps_stream_fastpath"
        if not self._tts_agent_route_available():
            return False, "worker_route_unavailable"
        if self._is_music_active_for_guild(int(guild.id)):
            # O caminho de música/agent já tem uma rota própria acima no worker_loop.
            return False, "music_active_uses_music_agent_tts_route"
        disabled_until = float(self._worker_voice_direct_tts_disabled_untils().get(int(guild.id), 0.0) or 0.0)
        if disabled_until > time.monotonic():
            return False, "direct_tts_failure_cooldown"
        voice_agent = self._tts_agent_route_state().get("voice_agent")
        if isinstance(voice_agent, dict) and voice_agent:
            if voice_agent.get("available") is False:
                return False, "voice_agent_unavailable"
            if voice_agent.get("music_ready") is False:
                return False, "music_agent_not_ready"
        return True, "allowed"

    def _worker_voice_direct_tts_payload(self, guild: discord.Guild, item: QueueItem) -> dict[str, Any]:
        state = self.guild_states.get(int(guild.id))
        me = getattr(guild, "me", None)
        return {
            "guild_id": int(guild.id),
            "channel_id": int(item.channel_id or 0),
            "voice_channel_id": int(item.channel_id or 0),
            "text_channel_id": int(getattr(state, "last_text_channel_id", 0) or 0) if state is not None else 0,
            "requester_id": int(item.author_id or 0),
            "bot_user_id": int(getattr(me, "id", 0) or 0),
            "source": "tts_worker_voice_direct",
            "text": str(item.text or ""),
            "engine": str(item.engine or "gtts"),
            "voice": str(item.voice or ""),
            "language": str(item.language or "pt-br"),
            "rate": str(item.rate or "+0%"),
            "pitch": str(item.pitch or "+0Hz"),
            "cache_key": self._cache_key(item),
            "cache_mode": "prefer",
            "timeout_seconds": max(3.0, min(WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS, self._estimate_playback_timeout(item))),
            "confirm_transfer": True,
            "direct_tts": True,
            "release_after": False,
        }

    async def _maybe_attach_prebuilt_direct_tts_audio(self, payload: dict[str, Any], item: QueueItem) -> str | None:
        return None

    async def _worker_voice_agent_begin_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload.setdefault("confirm_transfer", True)
        payload.setdefault("confirm", True)
        payload.setdefault("source", "tts_worker_voice_direct")
        data = await self._request_worker_voice_agent_json(
            task="voice_agent_begin_transfer",
            payload=payload,
            timeout_seconds=WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS,
        )
        self._record_worker_voice_session_metric("transfer_begin_ok" if data.get("ok", True) else "transfer_begin_failed")
        return data

    async def _worker_voice_agent_release_transfer(self, payload: dict[str, Any], *, reason: str = "direct_tts_failed") -> None:
        try:
            data = await self._request_worker_voice_agent_json(
                task="voice_agent_release_transfer",
                payload={"guild_id": int(payload.get("guild_id") or 0), "reason": reason, "source": "tts_worker_voice_direct"},
                timeout_seconds=WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS,
            )
            self._record_worker_voice_session_metric("transfer_release_ok" if data.get("ok", True) else "transfer_release_failed")
        except Exception as exc:
            self._record_worker_voice_session_metric("transfer_release_failed")
            logger.debug("[worker_voice_agent] liberar transferência falhou | guild=%s erro=%s", payload.get("guild_id"), exc)

    async def _disconnect_vps_voice_before_worker_direct_tts(self, guild: discord.Guild, item: QueueItem) -> None:
        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not self._voice_client_is_connected(vc):
            return
        if getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc):
            raise RuntimeError("voice client local é Lavalink; não transferindo TTS direto")
        if self._is_music_active_for_guild(int(guild.id)):
            raise RuntimeError("música ativa; TTS direto deve seguir rota do Music Agent")
        try:
            if self._voice_client_is_playing_or_paused(vc):
                vc.stop()
        except Exception:
            pass
        await vc.disconnect(force=True)
        state = self.guild_states.get(int(guild.id))
        if state is not None:
            state.last_channel_id = None
        logger.info(
            "[worker_voice_agent] VPS liberou voice client para TTS direto no worker | guild=%s channel=%s",
            guild.id,
            item.channel_id,
        )

    async def _try_worker_voice_direct_tts(self, guild: discord.Guild, item: QueueItem) -> dict[str, Any] | None:
        try:
            allowed, reason = self._worker_voice_direct_tts_available_for(guild, item)
        except Exception as exc:
            self._record_worker_voice_session_metric("direct_tts_skipped")
            logger.warning(
                "[worker_voice_agent] disponibilidade do TTS direto falhou; seguindo fallback normal | guild=%s erro=%s",
                guild.id,
                exc,
            )
            return None
        if not allowed:
            self._record_worker_voice_session_metric("direct_tts_skipped")
            logger.debug("[worker_voice_agent] TTS direto worker pulado | guild=%s reason=%s", guild.id, reason)
            return None
        payload = self._worker_voice_direct_tts_payload(guild, item)
        started = time.monotonic()
        try:
            prebuilt_path: str | None = None
            with contextlib.suppress(Exception):
                prebuilt_path = await self._maybe_attach_prebuilt_direct_tts_audio(payload, item)
            # Garante que o painel/worker tenham a sessão/handoff mais recente quando a VPS ainda está na call.
            vc = self._get_voice_client_for_guild(guild)
            if vc is not None and self._voice_client_is_connected(vc):
                self._schedule_worker_voice_agent_register_session(guild, item, vc, source="tts_worker_voice_direct_prepare")
                await asyncio.sleep(0.05)
                with contextlib.suppress(Exception):
                    handoff_payload = self._voice_client_handoff_payload(guild, item, vc, source="tts_worker_voice_direct_prepare")
                    if handoff_payload:
                        await self._worker_voice_agent_register_handoff(handoff_payload)
                transfer_result = await self._worker_voice_agent_begin_transfer({**payload, "current_owner": "vps", "requested_owner": "worker"})
                transfer = transfer_result.get("transfer") if isinstance(transfer_result, dict) else {}
                owner = str((transfer or {}).get("voice_owner") or (transfer or {}).get("current_owner") or "").lower()
                if owner != "worker":
                    raise RuntimeError(f"transferência não concedeu posse ao worker: owner={owner or 'desconhecido'}")
                await self._disconnect_vps_voice_before_worker_direct_tts(guild, item)
            else:
                # Sem conexão local ativa, o Music Agent pode assumir a voz direto pelo seu gateway interno.
                with contextlib.suppress(Exception):
                    await self._request_worker_voice_agent_json(
                        task="voice_agent_prepare_transfer",
                        payload={**payload, "current_owner": "none", "requested_owner": "worker", "reason": "sem voice client VPS ativo; worker pode assumir TTS direto"},
                        timeout_seconds=WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS,
                    )
            result = await self._request_worker_voice_agent_json(
                task="voice_agent_play_tts",
                payload=payload,
                timeout_seconds=max(3.0, float(payload.get("timeout_seconds") or WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS) + 4.0),
            )
            if not bool(result.get("ok", True)):
                raise RuntimeError(str(result.get("error") or "worker retornou ok=false no TTS direto"))
            if prebuilt_path:
                with contextlib.suppress(Exception):
                    os.remove(prebuilt_path)
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            self._record_worker_voice_session_metric("direct_tts_ok")
            logger.info(
                "[worker_voice_agent] TTS direto worker→Discord ok | guild=%s channel=%s engine=%s elapsed=%.1fms",
                guild.id,
                item.channel_id,
                result.get("engine") or item.engine,
                elapsed_ms,
            )
            playback_ms = float(result.get("playback_ms") or result.get("worker_result", {}).get("playback_ms") or elapsed_ms)
            return {
                "ok": True,
                "worker_voice_direct_tts": True,
                "source_setup_ms": 0.0,
                "play_call_ms": 0.0,
                "playback_ms": playback_ms,
                "playback_started_at": time.monotonic() - (playback_ms / 1000.0 if playback_ms > 0 else 0.0),
                "worker_result": result,
            }
        except Exception as exc:
            with contextlib.suppress(Exception):
                if 'prebuilt_path' in locals() and prebuilt_path:
                    os.remove(prebuilt_path)
            self._record_worker_voice_session_metric("direct_tts_failed")
            self._worker_voice_direct_tts_disabled_untils()[int(guild.id)] = time.monotonic() + WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS
            await self._worker_voice_agent_release_transfer(payload, reason=f"direct_tts_failed:{type(exc).__name__}")
            logger.warning(
                "[worker_voice_agent] TTS direto worker falhou; fallback VPS normal | guild=%s channel=%s erro=%s",
                guild.id,
                item.channel_id,
                exc,
            )
            return None

    def _worker_tts_cache_payload_base(self, item: QueueItem, key: str) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower() or "gtts"
        payload: dict[str, Any] = {
            "cache_key": key,
            "engine": engine,
            "text_length": len(str(item.text or "")),
            "max_audio_bytes": TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB * 1024 * 1024,
        }
        if engine == "piper":
            payload["model_name"] = str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME)
        return payload

    def _path_audio_format(self, path: str) -> str:
        suffix = os.path.splitext(str(path or ""))[1].lower().replace(".", "")
        if suffix in {"wav", "wave"}:
            return "wav"
        if suffix in {"ogg", "opus"}:
            return "ogg"
        return "mp3"

    def _audio_file_should_use_opus_source(self, path: str) -> bool:
        return bool(TTS_OPUS_PLAYBACK_ENABLED and self._path_audio_format(path) == "ogg" and getattr(discord, "FFmpegOpusAudio", None) is not None)

    def _tts_ffmpeg_before_options(self, path: str) -> str:
        options = str(TTS_FFMPEG_BEFORE_OPTIONS or "").strip()
        handle = self._edge_stream_handle_for_path(path)
        if (
            TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED
            and handle is not None
            and handle.engine == "edge"
            and re.search(r"(?:^|\s)-f\s+mp3(?:\s|$)", options) is None
        ):
            options = f"{options} -f mp3".strip()
        return options

    def _make_discord_tts_source(self, path: str) -> tuple[Any, str]:
        before_options = self._tts_ffmpeg_before_options(path)
        if self._audio_file_should_use_opus_source(path):
            opus_cls = getattr(discord, "FFmpegOpusAudio", None)
            if opus_cls is not None:
                if TTS_OPUS_PLAYBACK_COPY_CODEC:
                    try:
                        return opus_cls(
                            path,
                            before_options=before_options,
                            options=TTS_FFMPEG_OPTIONS,
                            codec="copy",
                        ), "ffmpeg_opus_copy"
                    except TypeError:
                        # Older discord.py builds may not accept codec=. Fall through.
                        pass
                    except Exception as exc:
                        logger.debug("[tts_voice] FFmpegOpusAudio codec=copy indisponível; tentando opus normal | path=%s erro=%s", path, exc)
                try:
                    return opus_cls(
                        path,
                        before_options=before_options,
                        options=TTS_FFMPEG_OPTIONS,
                    ), "ffmpeg_opus"
                except Exception as exc:
                    logger.debug("[tts_voice] FFmpegOpusAudio falhou; usando PCM fallback | path=%s erro=%s", path, exc)
        return discord.FFmpegPCMAudio(
            path,
            before_options=before_options,
            options=TTS_FFMPEG_OPTIONS,
        ), "ffmpeg_pcm"

    async def _try_get_worker_turbo_cache_path(self, item: QueueItem) -> str | None:
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return None
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return None
        # Quando o TTS Agent está ativo e saudável, o cache remoto é consultado
        # dentro do próprio pedido de síntese. Evita uma ida HTTP extra
        # cache_lookup -> miss -> synthesize antes de toda primeira fala.
        if TTS_WORKER_AGENT_ENABLED and self._tts_agent_route_available():
            self._record_worker_cache_lookup("skip")
            self._log_debug(
                f"[tts_worker_cache] lookup separado pulado; TTS Agent fará cache inline | guild={item.guild_id} engine={item.engine}"
            )
            return None
        if TTS_WORKER_AGENT_ENABLED and not self._tts_agent_route_available():
            self._record_worker_cache_lookup("skip")
            return None
        key = self._cache_key(item)
        recent_negative = self._worker_cache_recent_negative_status(key)
        if recent_negative:
            self._record_worker_cache_lookup("skip")
            self._log_debug(
                f"[tts_worker_cache] consulta pulada por índice negativo | guild={item.guild_id} engine={item.engine} key={key[:10]} status={recent_negative}"
            )
            return None
        payload = self._worker_tts_cache_payload_base(item, key)
        try:
            data = await self._request_phone_worker_json(
                task="tts_cache_lookup",
                payload=payload,
                timeout_seconds=TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS,
                max_audio_mb=TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB,
                raise_on_worker_error=False,
            )
            if not bool(data.get("cache_hit")):
                self._mark_worker_cache_index(key, "miss", meta={"engine": item.engine})
                self._record_worker_cache_lookup("miss")
                self._log_debug(
                    f"[tts_worker_cache] miss | guild={item.guild_id} engine={item.engine} key={key[:10]} total={data.get('total_ms')}ms erro={data.get('error')}"
                )
                return None
            data = await asyncio.to_thread(
                self._decode_worker_audio_payload,
                data,
                max_audio_mb=TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB,
            )
            suffix = ".wav" if data.get("audio_format") == "wav" else (".ogg" if data.get("audio_format") == "ogg" else ".mp3")
            path = self._make_runtime_temp_file(suffix=suffix)
            try:
                with open(path, "wb") as handle:
                    handle.write(data["raw_audio"])
                if os.path.getsize(path) <= 0:
                    raise RuntimeError("worker cache retornou áudio vazio")
                self._record_cache_hit(item.engine)
                self._record_worker_cache_lookup("hit", total_ms=float(data.get("total_ms", 0.0) or 0.0))
                self._mark_worker_cache_index(key, "hit", meta={
                    "engine": item.engine,
                    "audio_format": data.get("audio_format"),
                    "size": len(data.get("raw_audio") or b""),
                })
                self._log_debug(
                    f"[tts_worker_cache] hit | guild={item.guild_id} engine={item.engine} key={key[:10]} total={data.get('total_ms')}ms read={data.get('cache_read_ms')}ms"
                )
                return path
            except Exception:
                with contextlib.suppress(Exception):
                    os.remove(path)
                raise
        except Exception as exc:
            self._mark_worker_cache_index(key, "error", ttl=TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS, meta={"engine": item.engine, "error": str(exc)[:160]})
            self._record_worker_cache_lookup("error")
            self._log_debug(f"[tts_worker_cache] miss/indisponível | guild={item.guild_id} engine={item.engine} erro={exc}")
            return None

    async def _store_worker_turbo_cache(self, item: QueueItem, path: str) -> None:
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except Exception:
            return
        max_bytes = TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB * 1024 * 1024
        if size <= 0 or size > max_bytes:
            return
        key = self._cache_key(item)
        try:
            def _build_cache_content(target: str) -> tuple[bytes, str, str]:
                with open(target, "rb") as handle:
                    content = handle.read()
                return (
                    content,
                    hashlib.sha256(content).hexdigest(),
                    base64.b64encode(content).decode("ascii"),
                )

            raw, digest, encoded = await asyncio.to_thread(_build_cache_content, path)
            if not raw or len(raw) > max_bytes:
                return
            payload = self._worker_tts_cache_payload_base(item, key)
            payload.update({
                "audio_format": self._path_audio_format(path),
                "sha256": digest,
                "data_b64": encoded,
            })
            base = self._phone_worker_tts_base_url()
            if not base:
                return
            headers = {
                "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
                "Content-Type": "application/json",
            }
            request_payload = dict(payload)
            request_payload["task"] = "tts_cache_store"
            def _encode_request_body() -> bytes:
                return json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

            request_body = await asyncio.to_thread(_encode_request_body)
            timeout = aiohttp.ClientTimeout(total=TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS)
            session = await self._get_phone_worker_http_session()
            async with session.post(f"{base}/task", headers=headers, data=request_body, timeout=timeout) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
            self._mark_worker_cache_index(key, "hit", meta={"engine": item.engine, "size": size, "source": "store"})
            self._record_worker_cache_store(True)
            self._log_debug(f"[tts_worker_cache] store ok | guild={item.guild_id} engine={item.engine} key={key[:10]} size={size}")
        except Exception as exc:
            self._record_worker_cache_store(False)
            self._log_debug(f"[tts_worker_cache] store falhou | guild={item.guild_id} engine={item.engine} erro={exc}")

    async def _store_worker_turbo_cache_limited(self, item: QueueItem, path: str) -> None:
        async with self._get_worker_cache_store_semaphore():
            await self._store_worker_turbo_cache(item, path)

    def _schedule_worker_turbo_cache_store(self, item: QueueItem, path: str) -> None:
        if not TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND:
            return
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return
        tasks = self._get_worker_cache_store_tasks()
        if len(tasks) >= TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING:
            self._log_debug(
                f"[tts_worker_cache] store descartado por backlog | guild={item.guild_id} engine={item.engine} pending={len(tasks)}"
            )
            return
        task = asyncio.create_task(self._store_worker_turbo_cache_limited(item, path))
        tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            tasks.discard(done_task)
            if not done_task.cancelled():
                with contextlib.suppress(Exception):
                    done_task.exception()

        task.add_done_callback(_done)

    async def _generate_piper_worker_file(self, item: QueueItem) -> str:
        text = str(item.text or "").strip()
        if not text:
            raise RuntimeError("texto vazio para Piper")
        if len(text) > TTS_PIPER_MAX_TEXT_LENGTH:
            raise RuntimeError(f"texto grande demais para Piper experimental ({len(text)}/{TTS_PIPER_MAX_TEXT_LENGTH})")
        payload = {
            "text": text,
            "model_name": str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
            "timeout_seconds": max(1.0, TTS_PIPER_WORKER_TIMEOUT_SECONDS - 0.5),
        }
        data = await self._request_phone_worker_tts_audio(
            task="tts_synthesize_piper",
            payload=payload,
            timeout_seconds=TTS_PIPER_WORKER_TIMEOUT_SECONDS,
            max_audio_mb=TTS_PIPER_MAX_AUDIO_MB,
        )
        suffix = ".wav" if data.get("audio_format") == "wav" else ".mp3"
        path = self._make_runtime_temp_file(suffix=suffix)
        try:
            with open(path, "wb") as handle:
                handle.write(data["raw_audio"])
            if os.path.getsize(path) <= 0:
                raise RuntimeError("Piper retornou áudio vazio")
            logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            if logs:
                self._log_debug("[tts_piper] " + " | ".join(self._short_tts_benchmark_text(x, limit=120) for x in logs[:3]))
            return path
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(path)
            raise

    async def _generate_piper_fallback_file(self, item: QueueItem) -> str:
        fallback_engine = str(getattr(item, "piper_fallback_engine", "gtts") or "gtts").strip().lower()
        if fallback_engine == "edge":
            voice = str(getattr(item, "piper_fallback_voice", "") or item.voice or "pt-BR-FranciscaNeural")
            rate = str(getattr(item, "piper_fallback_rate", "") or item.rate or "+0%")
            pitch = str(getattr(item, "piper_fallback_pitch", "") or item.pitch or "+0Hz")
            return await self._run_timed_generation("edge", lambda: self._generate_edge_file(item.text, voice, rate, pitch), guild_id=item.guild_id)
        language = str(getattr(item, "piper_fallback_language", "") or item.language or GTTS_DEFAULT_LANGUAGE)
        return await self._run_timed_generation("gtts", lambda: self._generate_gtts_file(item.text, language), guild_id=item.guild_id)

    def _short_tts_benchmark_text(self, value: Any, *, limit: int = 180) -> str:
        text = str(value or "").replace("`", "'").replace("\r", " ").replace("\n", " ").strip()
        text = " ".join(text.split())
        if len(text) > limit:
            return text[: max(0, limit - 1)] + "…"
        return text

    def _format_tts_benchmark_ms(self, value: Any) -> str:
        try:
            numeric = float(value)
        except Exception:
            return "—"
        return f"{numeric:.0f} ms" if numeric >= 10 else f"{numeric:.1f} ms"

    def _format_tts_benchmark_delta(self, local_ms: Any, worker_ms: Any) -> str:
        try:
            local = float(local_ms)
            worker = float(worker_ms)
        except Exception:
            return "sem cálculo"
        delta = local - worker
        pct = (delta / local * 100.0) if local > 0 else 0.0
        if delta > 0:
            return f"worker ganhou por {delta:.0f} ms ({pct:.1f}%)"
        if delta < 0:
            return f"VPS ganhou por {abs(delta):.0f} ms ({abs(pct):.1f}%)"
        return "empate técnico"

    def _should_run_tts_turbo_benchmark(self, message: discord.Message, active_prefix: str) -> bool:
        if not TTS_TURBO_BENCHMARK_ENABLED:
            return False
        guild = getattr(message, "guild", None)
        if guild is None or int(getattr(guild, "id", 0) or 0) != TTS_TURBO_BENCHMARK_GUILD_ID:
            return False
        content = str(getattr(message, "content", "") or "")
        prefix = str(active_prefix or "")
        if not prefix or not content.startswith(prefix):
            return False
        spoken = content[len(prefix):].strip().lower()
        return spoken == TTS_TURBO_BENCHMARK_TRIGGER_TEXT

    def _build_tts_benchmark_item(self, base_item: QueueItem, engine: str, resolved: dict[str, Any] | None, *, text: str) -> QueueItem:
        resolved = dict(resolved or {})
        engine = str(engine or "gtts").strip().lower()
        if engine == "edge":
            voice = str(resolved.get("voice") or base_item.voice or "pt-BR-FranciscaNeural")
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = str(resolved.get("rate") or base_item.rate or "+0%")
            pitch = str(resolved.get("pitch") or base_item.pitch or "+0Hz")
        elif engine == "piper":
            voice = ""
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = "+0%"
            pitch = "+0Hz"
        elif engine == "android_native":
            voice = str(resolved.get("android_voice") or "")
            language = str(resolved.get("android_language") or base_item.language or "pt-BR")
            rate = str(resolved.get("android_rate") or base_item.rate or "1.0")
            pitch = str(resolved.get("android_pitch") or base_item.pitch or "1.0")
        else:
            engine = "gtts"
            voice = ""
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = "+0%"
            pitch = "+0Hz"
        return QueueItem(
            guild_id=base_item.guild_id,
            channel_id=base_item.channel_id,
            author_id=base_item.author_id,
            text=text,
            engine=engine,
            voice=voice,
            language=language,
            rate=rate,
            pitch=pitch,
            piper_model=str(resolved.get("piper_model") or getattr(base_item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
        )

    async def _tts_benchmark_local_engine(self, item: QueueItem) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        started = time.monotonic()
        path = ""
        try:
            if engine == "piper":
                raise RuntimeError("Piper experimental roda apenas no phone-worker turbo")
            if engine == "android_native":
                raise RuntimeError("Android TTS nativo roda apenas no APK/phone-worker")
            if engine == "edge":
                path = await self._generate_edge_file(item.text, item.voice, item.rate, item.pitch)
            else:
                engine = "gtts"
                path = await self._generate_gtts_file(item.text, item.language)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
            if size <= 0:
                raise RuntimeError("engine gerou áudio vazio (0 B)")
            sha256 = ""
            if path and os.path.exists(path):
                def _hash_file(target: str) -> str:
                    digest = hashlib.sha256()
                    with open(target, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    return digest.hexdigest()
                sha256 = await asyncio.to_thread(_hash_file, path)
            return {
                "ok": True,
                "engine": engine,
                "elapsed_ms": round(elapsed_ms, 2),
                "size": int(size),
                "sha256": sha256,
                "logs": [f"VPS gerou {size} bytes em {elapsed_ms:.1f} ms"],
            }
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": engine,
                "elapsed_ms": round(elapsed_ms, 2),
                "error": self._short_tts_benchmark_text(f"{type(exc).__name__}: {exc}", limit=220),
                "logs": [f"VPS falhou após {elapsed_ms:.1f} ms"],
            }
        finally:
            if path:
                with contextlib.suppress(Exception):
                    os.remove(path)

    async def _tts_benchmark_worker_engine_once(self, item: QueueItem, *, cache_mode: str | None = None) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        base = self._phone_worker_tts_benchmark_base_url()
        if not base:
            return {
                "ok": False,
                "engine": engine,
                "error": "PHONE_WORKER_ENABLED/HOST/TOKEN não configurado",
                "logs": ["worker indisponível na config da VPS"],
            }
        payload = {
            "task": "tts_synthesize_benchmark",
            "engine": engine,
            "text": item.text,
            "voice": item.voice,
            "language": item.language,
            "rate": item.rate,
            "pitch": item.pitch,
            "model_name": str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
            "timeout_seconds": int(max(2.0, TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS - 1.0)),
            "max_audio_bytes": TTS_TURBO_BENCHMARK_MAX_AUDIO_MB * 1024 * 1024,
        }
        if cache_mode:
            payload["cache_mode"] = cache_mode
        headers = {
            "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                    response_text = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(f"HTTP {response.status}: {response_text[:260]}")
                    data = json.loads(response_text or "{}")
            if isinstance(data, dict) and data.get("ok") is False:
                total_ms = (time.monotonic() - started) * 1000.0
                logs = data.get("logs") if isinstance(data.get("logs"), list) else []
                clean_logs = [self._short_tts_benchmark_text(line, limit=160) for line in logs[:5]]
                clean_logs.append(f"VPS recebeu resposta sem áudio; total real {total_ms:.1f} ms")
                return {
                    "ok": False,
                    "engine": engine,
                    "total_ms": round(total_ms, 2),
                    "worker_total_ms": data.get("worker_total_ms"),
                    "worker_synth_ms": data.get("worker_synth_ms"),
                    "size": int(data.get("size") or 0),
                    "error": self._short_tts_benchmark_text(str(data.get("error") or "worker retornou ok=false"), limit=260),
                    "worker_profile": data.get("worker_profile"),
                    "worker_version": data.get("worker_version"),
                    "audio_format": data.get("audio_format"),
                    "cache_hit": bool(data.get("cache_hit")),
                    "cache_exists_before": bool(data.get("cache_exists_before")),
                    "cache_mode": data.get("cache_mode"),
                    "cache_key": data.get("cache_key"),
                    "cache_file": data.get("cache_file"),
                    "cache_read_ms": data.get("cache_read_ms"),
                    "cache_stored": bool(data.get("cache_stored")),
                    "logs": clean_logs,
                }
            out_b64 = str(data.get("data_b64") or "")
            if not out_b64:
                raise RuntimeError("worker não retornou data_b64")
            raw = base64.b64decode(out_b64.encode("ascii"), validate=True)
            if not raw:
                raise RuntimeError("worker retornou áudio vazio")
            max_audio_bytes = TTS_TURBO_BENCHMARK_MAX_AUDIO_MB * 1024 * 1024
            if len(raw) > max_audio_bytes:
                raise RuntimeError(f"worker retornou áudio grande demais: {len(raw)} bytes")
            expected_hash = str(data.get("sha256") or "")
            actual_hash = hashlib.sha256(raw).hexdigest()
            if expected_hash and expected_hash != actual_hash:
                raise RuntimeError("sha256 do áudio retornado não confere")

            def _write_and_stat(content: bytes) -> int:
                suffix = ".wav" if data.get("audio_format") == "wav" else ".mp3"
                path = self._make_runtime_temp_file(suffix=suffix)
                try:
                    with open(path, "wb") as handle:
                        handle.write(content)
                    return os.path.getsize(path)
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)

            saved_size = await asyncio.to_thread(_write_and_stat, raw)
            if saved_size <= 0:
                raise RuntimeError("worker retornou áudio vazio após salvar temp")
            total_ms = (time.monotonic() - started) * 1000.0
            logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            clean_logs = [self._short_tts_benchmark_text(line, limit=160) for line in logs[:4]]
            clean_logs.append(f"VPS validou/salvou temp {saved_size} bytes; total real {total_ms:.1f} ms")
            return {
                "ok": True,
                "engine": engine,
                "total_ms": round(total_ms, 2),
                "worker_synth_ms": data.get("worker_synth_ms"),
                "size": int(saved_size),
                "sha256": actual_hash,
                "worker_profile": data.get("worker_profile"),
                "worker_version": data.get("worker_version"),
                "audio_format": data.get("audio_format"),
                "cache_hit": bool(data.get("cache_hit")),
                "cache_exists_before": bool(data.get("cache_exists_before")),
                "cache_mode": data.get("cache_mode"),
                "cache_key": data.get("cache_key"),
                "cache_file": data.get("cache_file"),
                "cache_read_ms": data.get("cache_read_ms"),
                "cache_stored": bool(data.get("cache_stored")),
                "logs": clean_logs,
            }
        except Exception as exc:
            total_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": engine,
                "total_ms": round(total_ms, 2),
                "error": self._short_tts_benchmark_text(f"{type(exc).__name__}: {exc}", limit=260),
                "logs": [f"worker falhou após {total_ms:.1f} ms"],
            }

    async def _tts_benchmark_worker_engine(self, item: QueueItem) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        if engine != "piper":
            return await self._tts_benchmark_worker_engine_once(item)

        miss = await self._tts_benchmark_worker_engine_once(item, cache_mode="refresh")
        hit = await self._tts_benchmark_worker_engine_once(item, cache_mode="cache_only")
        if not miss.get("ok"):
            return miss
        hit_is_real = bool(hit.get("ok")) and bool(hit.get("cache_hit"))
        if not hit_is_real:
            combined = dict(miss)
            combined["piper_cache_miss"] = miss
            combined["piper_cache_hit"] = hit
            combined["piper_cache_hit_real"] = False
            reason = hit.get("error") or "segunda chamada não retornou cache_hit=true"
            combined["logs"] = list(miss.get("logs") or [])[:4] + ["cache hit inválido: " + str(reason)] + list(hit.get("logs") or [])[:3]
            return combined
        combined = dict(hit)
        combined["piper_cache_miss"] = miss
        combined["piper_cache_hit"] = hit
        combined["piper_cache_hit_real"] = True
        combined["worker_synth_ms"] = miss.get("worker_synth_ms")
        combined["total_ms"] = hit.get("total_ms")
        logs = []
        logs.extend(list(miss.get("logs") or [])[:3])
        logs.extend(list(hit.get("logs") or [])[:4])
        combined["logs"] = logs[:7]
        return combined

    def _format_tts_benchmark_engine_block(self, engine: str, local: dict[str, Any], worker: dict[str, Any]) -> list[str]:
        local_ok = bool(local.get("ok"))
        worker_ok = bool(worker.get("ok"))
        local_ms = local.get("elapsed_ms")
        worker_total_ms = worker.get("total_ms")
        worker_synth_ms = worker.get("worker_synth_ms")
        if engine == "piper":
            miss = worker.get("piper_cache_miss") if isinstance(worker.get("piper_cache_miss"), dict) else None
            hit = worker.get("piper_cache_hit") if isinstance(worker.get("piper_cache_hit"), dict) else None
            hit_real = bool(worker.get("piper_cache_hit_real")) or (bool(hit and hit.get("ok")) and bool(hit and hit.get("cache_hit")))
            if hit_real:
                title = "Piper funcional · cache hit real"
            elif worker_ok and hit is not None:
                title = "Piper funcional, mas cache hit falhou"
            elif worker_ok:
                title = "Piper funcional, mas cache hit não foi medido"
            else:
                title = "Piper falhou no worker"
            lines = [f"**piper** — {title}"]
            lines.append("VPS: indisponível · Piper experimental roda apenas no phone-worker turbo")
            if worker_ok:
                miss_total = miss.get("total_ms") if miss else worker.get("total_ms")
                miss_synth = miss.get("worker_synth_ms") if miss else worker.get("worker_synth_ms")
                miss_size = int((miss or worker).get("size") or 0)
                lines.append(
                    f"Worker geração/miss: ok · total {self._format_tts_benchmark_ms(miss_total)}"
                    + (f" · synth {self._format_tts_benchmark_ms(miss_synth)}" if miss_synth is not None else "")
                    + f" · {miss_size} B"
                )
                if hit is not None:
                    hit_total = hit.get("total_ms")
                    hit_read = hit.get("cache_read_ms")
                    hit_size = int(hit.get("size") or 0)
                    if hit_real:
                        lines.append(
                            f"Worker cache hit: ok · total {self._format_tts_benchmark_ms(hit_total)}"
                            + (f" · read {self._format_tts_benchmark_ms(hit_read)}" if hit_read is not None else "")
                            + f" · {hit_size} B"
                            + (f" · key `{hit.get('cache_key')}`" if hit.get("cache_key") else "")
                        )
                    else:
                        lines.append(
                            f"Worker cache hit: falhou/ inválido · total {self._format_tts_benchmark_ms(hit_total)}"
                            + f" · {hit.get('error') or 'cache_hit não confirmado'}"
                            + (f" · key `{hit.get('cache_key')}`" if hit.get("cache_key") else "")
                        )
            else:
                lines.append(f"Worker: falhou · total {self._format_tts_benchmark_ms(worker_total_ms)} · {worker.get('error') or 'erro sem detalhe'}")
            logs: list[str] = []
            for source, data in (("Worker", worker),):
                raw_logs = data.get("logs") if isinstance(data.get("logs"), list) else []
                for entry in raw_logs[:6]:
                    logs.append(f"{source}: {self._short_tts_benchmark_text(entry, limit=120)}")
            if logs:
                lines.append("Logs curtas: " + " | ".join(logs[:6]))
            return lines

        if local_ok and worker_ok:
            winner = self._format_tts_benchmark_delta(local_ms, worker_total_ms)
        elif local_ok:
            winner = "só VPS funcionou"
        elif worker_ok:
            winner = "só worker funcionou"
        else:
            winner = "ambos falharam"
        display_engine = "ATTS" if engine == "android_native" else engine
        lines = [f"**{display_engine}** — {winner}"]
        lines.append(
            f"VPS: {'ok' if local_ok else 'falhou'} · {self._format_tts_benchmark_ms(local_ms)}"
            + (f" · {int(local.get('size') or 0)} B" if local_ok else f" · {local.get('error') or 'erro sem detalhe'}")
        )
        lines.append(
            f"Worker: {'ok' if worker_ok else 'falhou'} · total {self._format_tts_benchmark_ms(worker_total_ms)}"
            + (f" · synth {self._format_tts_benchmark_ms(worker_synth_ms)} · {int(worker.get('size') or 0)} B" if worker_ok else f" · {worker.get('error') or 'erro sem detalhe'}")
        )
        logs: list[str] = []
        for source, data in (("VPS", local), ("Worker", worker)):
            raw_logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            for entry in raw_logs[:2]:
                logs.append(f"{source}: {self._short_tts_benchmark_text(entry, limit=120)}")
        if logs:
            lines.append("Logs curtas: " + " | ".join(logs[:4]))
        return lines

    async def _send_tts_turbo_benchmark_report(self, channel: Any, base_item: QueueItem, resolved: dict[str, Any] | None) -> None:
        benchmark_text = TTS_TURBO_BENCHMARK_TRIGGER_TEXT
        engines = ("android_native", "edge", "gtts")
        started = time.monotonic()
        results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        worker_meta: dict[str, Any] = {}
        for engine in engines:
            item = self._build_tts_benchmark_item(base_item, engine, resolved, text=benchmark_text)
            local_task = asyncio.create_task(self._tts_benchmark_local_engine(item))
            worker_task = asyncio.create_task(self._tts_benchmark_worker_engine(item))
            local, worker = await asyncio.gather(local_task, worker_task)
            if worker.get("worker_profile") or worker.get("worker_version"):
                worker_meta = worker
            results.append((engine, local, worker))

        total_ms = (time.monotonic() - started) * 1000.0
        good_comparisons = 0
        worker_wins = 0
        local_wins = 0
        best_saving_ms = 0.0
        for _, local, worker in results:
            if local.get("ok") and worker.get("ok"):
                good_comparisons += 1
                try:
                    delta = float(local.get("elapsed_ms") or 0.0) - float(worker.get("total_ms") or 0.0)
                except Exception:
                    delta = 0.0
                if delta > 0:
                    worker_wins += 1
                    best_saving_ms = max(best_saving_ms, delta)
                elif delta < 0:
                    local_wins += 1
        piper_hit_ms = None
        piper_miss_ms = None
        piper_hit_real = False
        piper_cache_error = ""

        if good_comparisons <= 0:
            verdict = "não deu para comparar com segurança: nenhuma engine teve os dois lados ok."
        elif worker_wins >= 2:
            verdict = f"worker turbo parece promissor ({worker_wins}/{good_comparisons} vitórias; melhor ganho {best_saving_ms:.0f} ms)."
        elif worker_wins == 1:
            verdict = "worker turbo ganhou só em uma engine; ainda não dá para usar em TTS real."
        else:
            verdict = "VPS foi igual ou melhor em Edge/gTTS; ATTS deve ser usado quando o worker/APK estiver pronto."
        if piper_miss_ms is not None:
            if piper_hit_real and piper_hit_ms is not None:
                verdict += f" Piper: miss {self._format_tts_benchmark_ms(piper_miss_ms)}; cache hit real {self._format_tts_benchmark_ms(piper_hit_ms)} — recomendado quando cacheado."
            else:
                detail = f" ({self._short_tts_benchmark_text(piper_cache_error, limit=90)})" if piper_cache_error else ""
                verdict += f" Piper: miss {self._format_tts_benchmark_ms(piper_miss_ms)}; cache hit ainda não validado{detail}."

        header = [
            "🧪 **Benchmark TTS Worker Turbo**",
            f"Servidor autorizado: `{TTS_TURBO_BENCHMARK_GUILD_ID}` · texto: `{benchmark_text}` · total: {self._format_tts_benchmark_ms(total_ms)}",
        ]
        if worker_meta:
            header.append(
                f"Worker: perfil `{worker_meta.get('worker_profile') or '?'}` · versão `{worker_meta.get('worker_version') or '?'}`"
            )
        blocks: list[str] = []
        for engine, local, worker in results:
            blocks.append("\n".join(self._format_tts_benchmark_engine_block(engine, local, worker)))
        footer = f"**Resumo:** {verdict}"
        content = "\n".join(header + ["", *blocks, "", footer])
        if len(content) > 1900:
            content = content[:1880] + "\n… relatório cortado para caber na mensagem."
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[tts_benchmark] falha ao enviar relatório no canal")

    def _schedule_tts_turbo_benchmark_if_needed(self, message: discord.Message, active_prefix: str, item: QueueItem, resolved: dict[str, Any] | None) -> bool:
        if not self._should_run_tts_turbo_benchmark(message, active_prefix):
            return False
        channel = getattr(message, "channel", None)
        if channel is None:
            return False
        task = asyncio.create_task(self._send_tts_turbo_benchmark_report(channel, item, resolved))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return True

    async def _resolve_or_generate_singleflight_audio(self, state: GuildTTSState, item: QueueItem, *, read_cache: bool, store_in_cache: bool) -> tuple[str, bool]:
        key = self._cache_key(item)
        inflight = self._get_inflight_cache_tasks()

        existing = inflight.get(key)
        if existing is not None:
            return await existing

        async def _runner() -> tuple[str, bool]:
            if read_cache:
                cached = self._try_get_cached_path(state, item)
                if cached:
                    return cached, False

                worker_cached = await self._try_get_worker_turbo_cache_path(item)
                if worker_cached:
                    if store_in_cache:
                        cached_path = await self._store_in_cache(state, item, worker_cached)
                        return cached_path, False
                    return worker_cached, False

            generated = await self._generate_audio_file(item)
            if store_in_cache:
                cached_path = await self._store_in_cache(state, item, generated)
                if not bool(getattr(item, "_tts_agent_inline_cache", False)):
                    self._schedule_worker_turbo_cache_store(item, cached_path)
                if cached_path != generated:
                    return cached_path, False
            return generated, False

        task = asyncio.create_task(_runner())
        inflight[key] = task
        try:
            return await task
        finally:
            if inflight.get(key) is task:
                inflight.pop(key, None)

    async def _run_timed_generation(
        self,
        engine: str,
        factory,
        *,
        guild_id: int | None = None,
        persistent_engine: Any = None,
    ) -> str:
        started_at = time.monotonic()
        try:
            result = await factory()
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            self._record_engine_failure(engine, exc, duration_ms=duration_ms)
            raise
        duration_ms = (time.monotonic() - started_at) * 1000.0
        self._record_engine_success(engine, duration_ms)

        engine_to_persist = engine
        if callable(persistent_engine):
            with contextlib.suppress(Exception):
                engine_to_persist = str(persistent_engine() or engine)
        elif persistent_engine not in (None, ""):
            engine_to_persist = str(persistent_engine)
        self._schedule_persistent_synt_success(guild_id, engine_to_persist)
        return result

    def _tts_agent_payload_for_item(self, item: QueueItem) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        if engine in {"gcloud", "google", "google_cloud", "googlecloud", "google_tts"}:
            engine = "gtts"
        is_teto = engine == "teto"
        max_text_length = TTS_TETO_MAX_TEXT_LENGTH if is_teto else TTS_WORKER_AGENT_MAX_TEXT_LENGTH
        timeout_seconds = TTS_TETO_WORKER_TIMEOUT_SECONDS if is_teto else TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS
        max_audio_mb = TTS_TETO_MAX_AUDIO_MB if is_teto else TTS_WORKER_AGENT_MAX_AUDIO_MB
        fallback_engine = str(getattr(item, "piper_fallback_engine", "") or "gtts").strip().lower().replace("-", "_") or "gtts"
        if fallback_engine in {"gcloud", "google", "google_cloud", "googlecloud", "google_tts"}:
            fallback_engine = "gtts"
        return {
            "text": str(item.text or "")[:max_text_length],
            "engine": engine,
            "voice": str(item.voice or ""),
            "language": str(item.language or ""),
            "rate": str(item.rate or "+0%"),
            "pitch": str(item.pitch or "+0Hz"),
            # Uma requisição explícita da Teto não pode ser desviada pela engine
            # global preferida do worker; o fallback continua separado abaixo.
            "preferred_engine": "teto" if is_teto else TTS_WORKER_AGENT_PREFERRED_ENGINE,
            "fallback_engine": fallback_engine,
            "fallback_voice": str(getattr(item, "piper_fallback_voice", "") or item.voice or ""),
            "fallback_language": str(getattr(item, "piper_fallback_language", "") or item.language or GTTS_DEFAULT_LANGUAGE),
            "fallback_rate": str(getattr(item, "piper_fallback_rate", "") or item.rate or "+0%"),
            "fallback_pitch": str(getattr(item, "piper_fallback_pitch", "") or item.pitch or "+0Hz"),
            "model_name": str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
            "cache_key": self._cache_key(item),
            "cache_mode": "prefer",
            "timeout_seconds": timeout_seconds,
            "max_audio_bytes": max_audio_mb * 1024 * 1024,
            "guild_id": int(item.guild_id or 0),
            "channel_id": int(item.channel_id or 0),
            "author_id": int(item.author_id or 0),
        }

    def _is_tts_agent_transient_busy_error(self, exc: Exception | str) -> bool:
        text = str(exc or "").lower()
        return any(token in text for token in (
            "tts agent ocupado",
            "fila local cheia",
            "busy",
            "queue full",
            "http 429",
            "http 503",
            "temporariamente indispon",
            "temporarily unavailable",
        ))

    async def _generate_tts_agent_worker_file(self, item: QueueItem) -> str:
        setattr(item, "_tts_agent_selected_engine", "")
        if not self._tts_agent_route_available():
            raise RuntimeError("TTS Agent indisponível pela rota cacheada")
        text = str(item.text or "").strip()
        if not text:
            raise RuntimeError("texto vazio para TTS Agent")
        engine = str(item.engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        max_text_length = TTS_TETO_MAX_TEXT_LENGTH if engine == "teto" else TTS_WORKER_AGENT_MAX_TEXT_LENGTH
        if len(text) > max_text_length:
            raise RuntimeError(f"texto grande demais para TTS Agent: {len(text)} > {max_text_length}")

        metrics = self._get_metrics_store()
        metrics["tts_agent_synth_attempts"] = int(metrics.get("tts_agent_synth_attempts", 0) or 0) + 1
        started = time.monotonic()
        last_error: Exception | None = None
        max_attempts = max(1, int(TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS or 0) + 1)
        for attempt in range(max_attempts):
            path = ""
            try:
                request_started = time.monotonic()
                data = await self._request_phone_worker_tts_audio(
                    task="tts_agent_synthesize",
                    payload=self._tts_agent_payload_for_item(item),
                    timeout_seconds=TTS_TETO_WORKER_TIMEOUT_SECONDS if engine == "teto" else TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS,
                    max_audio_mb=TTS_TETO_MAX_AUDIO_MB if engine == "teto" else TTS_WORKER_AGENT_MAX_AUDIO_MB,
                    stream_to_file=True,
                )
                request_ms = (time.monotonic() - request_started) * 1000.0
                data["requested_engine"] = str(item.engine or "").strip().lower()
                fmt = self._normalize_worker_audio_format(data.get("audio_format"))
                data["audio_format"] = fmt
                path = str(data.get("audio_path") or "")
                write_ms = 0.0
                raw = data.get("raw_audio") or data.get("audio_bytes")
                if path:
                    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                        raise RuntimeError("TTS Agent retornou arquivo de áudio inválido")
                    data["audio_bytes_len"] = int(data.get("audio_bytes_len") or os.path.getsize(path))
                else:
                    if not isinstance(raw, (bytes, bytearray)) or not raw:
                        raise RuntimeError("TTS Agent não retornou áudio")
                    data["audio_bytes_len"] = len(raw)
                    suffix = ".wav" if fmt == "wav" else ".ogg" if fmt == "ogg" else ".mp3"
                    path = self._make_runtime_temp_file(suffix=suffix)

                    def _write_audio(target: str, content: bytes) -> None:
                        with open(target, "wb") as handle:
                            handle.write(content)

                    write_started = time.monotonic()
                    await asyncio.to_thread(_write_audio, path, bytes(raw))
                    write_ms = (time.monotonic() - write_started) * 1000.0
                total_ms = (time.monotonic() - started) * 1000.0
                self._record_tts_agent_synth_success(total_ms=total_ms, data=data)
                selected_engine = str(data.get("selected_engine") or data.get("engine") or "").strip().lower()
                setattr(item, "_tts_agent_selected_engine", selected_engine or str(item.engine or "gtts"))
                worker_timing = data.get("timing_ms") if isinstance(data.get("timing_ms"), dict) else {}
                timing_bits = [
                    f"worker_http={float(data.get('_vps_worker_request_ms') or request_ms):.1f}ms",
                    f"decode={float(data.get('_vps_audio_decode_ms') or 0.0):.1f}ms",
                    f"write={write_ms:.1f}ms",
                ]
                if worker_timing:
                    for key in ("cache_read", "android_roundtrip", "android_synth", "cache_store", "worker_total"):
                        value = worker_timing.get(key)
                        if value not in (None, ""):
                            with contextlib.suppress(Exception):
                                timing_bits.append(f"{key}={float(value):.1f}ms")
                metrics["tts_agent_last_timing_ms"] = {
                    "worker_http": round(float(data.get("_vps_worker_request_ms") or request_ms), 2),
                    "json_parse": round(float(data.get("_vps_worker_json_parse_ms") or 0.0), 2),
                    "decode": round(float(data.get("_vps_audio_decode_ms") or 0.0), 2),
                    "write": round(write_ms, 2),
                    "total": round(total_ms, 2),
                    **{str(k): v for k, v in worker_timing.items()},
                }
                logger.info(
                    "[tts_agent] synth ok | guild=%s route=worker requested=%s selected=%s format=%s bytes=%s cache_hit=%s total=%.1fms stages=%s",
                    item.guild_id,
                    item.engine,
                    selected_engine or "unknown",
                    fmt,
                    int(data.get("audio_bytes_len") or 0),
                    bool(data.get("cache_hit")),
                    total_ms,
                    " ".join(timing_bits),
                )
                if bool(data.get("cache_hit")) or worker_timing:
                    setattr(item, "_tts_agent_inline_cache", True)
                return path
            except Exception as exc:
                if path:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        os.remove(path)
                last_error = exc
                if attempt < max_attempts - 1 and self._is_tts_agent_transient_busy_error(exc):
                    metrics["tts_agent_busy_retries"] = int(metrics.get("tts_agent_busy_retries", 0) or 0) + 1
                    delay = TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS * (attempt + 1)
                    logger.info(
                        "[tts_agent] worker ocupado; retry curto antes do fallback | guild=%s engine=%s tentativa=%s/%s delay=%.2fs erro=%s",
                        item.guild_id,
                        item.engine,
                        attempt + 1,
                        max_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        final_error = last_error or RuntimeError("falha desconhecida no TTS Agent")
        self._mark_tts_agent_synth_failure(final_error)
        raise final_error

    def _engine_average_ms(self, engine: str) -> float:
        metrics = self._get_engine_metrics(engine)
        count = int(metrics.get("synth_count", 0) or 0)
        total = float(metrics.get("synth_total_ms", 0.0) or 0.0)
        if count <= 0 or total <= 0:
            return 0.0
        return total / count

    def _tts_agent_should_try_worker(self, item: QueueItem) -> tuple[bool, str]:
        if not self._tts_agent_route_available():
            return False, "worker_offline_or_not_ready"
        engine = str(getattr(item, "engine", "") or "gtts").strip().lower().replace("-", "_") or "gtts"
        if not TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED:
            return True, "adaptive_disabled"
        if engine in TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES:
            return True, "always_worker_engine"
        text_len = len(self._get_item_normalized_cache_text(item))
        if engine == "gtts" and text_len < TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS:
            return False, "gtts_short_text_vps_fastpath"
        local_avg = self._engine_average_ms(engine)
        worker_avg = self._engine_average_ms(f"tts_agent:{engine}")
        if worker_avg <= 0:
            worker_avg = float(self._tts_agent_route_state().get("avg_synth_ms") or 0.0)
        if local_avg > 0 and worker_avg > 0:
            slow_limit = max(local_avg * TTS_WORKER_AGENT_WORKER_SLOW_MARGIN, local_avg + TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS)
            if worker_avg > slow_limit:
                return False, f"vps_faster:{local_avg:.0f}ms<{worker_avg:.0f}ms"
        return True, "worker_ready"

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if not _has_speakable_tts_text(getattr(item, "text", "")):
            raise ValueError("texto TTS sem caracteres faláveis")

        agent_available = self._tts_agent_route_available()
        use_agent, agent_decision = self._tts_agent_should_try_worker(item) if agent_available else (False, "worker_offline_or_not_ready")
        self._record_tts_agent_route_sample(use_agent)
        if agent_available and not use_agent:
            self._tts_agent_route_state()["last_error"] = ""
            self._tts_agent_route_state()["reason"] = agent_decision[:160]
            self._log_debug(f"[tts_agent] rota VPS escolhida sem tentativa worker | guild={item.guild_id} engine={item.engine} motivo={agent_decision}")
        if use_agent:
            try:
                return await self._run_timed_generation(
                    f"tts_agent:{item.engine}",
                    lambda: self._generate_tts_agent_worker_file(item),
                    guild_id=item.guild_id,
                    persistent_engine=lambda: getattr(item, "_tts_agent_selected_engine", "") or item.engine,
                )
            except Exception as e:
                logger.warning("[tts_agent] TTS no worker falhou; usando fallback local/VPS | guild=%s engine=%s erro=%s", item.guild_id, item.engine, e)

        if item.engine in {"android_native", "teto"}:
            label = "Kasane Teto" if item.engine == "teto" else "Android TTS nativo"
            logger.warning("[tts_fallback] %s indisponível; usando engine normal do usuário | guild=%s motivo=%s", label, item.guild_id, agent_decision)
            return await self._generate_piper_fallback_file(item)

        if item.engine == "piper":
            try:
                return await self._run_timed_generation(
                    "piper",
                    lambda: self._generate_piper_worker_file(item),
                    guild_id=item.guild_id,
                )
            except Exception as e:
                logger.warning("[tts_piper] Piper experimental falhou, usando fallback local | guild=%s erro=%s", item.guild_id, e)
                return await self._generate_piper_fallback_file(item)

        if item.engine == "edge":
            foreground = not bool(getattr(item, "_tts_prefetch", False))
            if self._edge_circuit_is_open():
                self._record_edge_circuit_bypass()
                logger.info(
                    "[tts_voice] circuito Edge em cooldown; usando gTTS sem aguardar nova falha | guild=%s",
                    item.guild_id,
                )
                return await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file_with_priority(
                        item.text,
                        item.language,
                        foreground=foreground,
                    ),
                    guild_id=item.guild_id,
                )
            try:
                return await self._run_timed_generation(
                    "edge",
                    lambda: self._generate_edge_file(item.text, item.voice, item.rate, item.pitch),
                    guild_id=item.guild_id,
                )
            except Exception as e:
                logger.warning("[tts_voice] Edge falhou, usando gTTS | guild=%s erro=%s", item.guild_id, e)
                return await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file_with_priority(
                        item.text,
                        item.language,
                        foreground=foreground,
                    ),
                    guild_id=item.guild_id,
                )

        foreground = not bool(getattr(item, "_tts_prefetch", False))
        return await self._run_timed_generation(
            "gtts",
            lambda: self._generate_gtts_file_with_priority(
                item.text,
                item.language,
                foreground=foreground,
            ),
            guild_id=item.guild_id,
        )

    def _build_edge_gtts_fallback_item(self, item: QueueItem) -> QueueItem:
        fallback = QueueItem(
            guild_id=item.guild_id,
            channel_id=item.channel_id,
            author_id=item.author_id,
            text=item.text,
            engine="gtts",
            voice="",
            language=item.language,
            rate="+0%",
            pitch="+0Hz",
            enqueued_at_monotonic=item.enqueued_at_monotonic,
        )
        if bool(getattr(item, "_tts_prefetch", False)):
            setattr(fallback, "_tts_prefetch", True)
        promotion_event = getattr(item, "_tts_foreground_event", None)
        if isinstance(promotion_event, asyncio.Event):
            setattr(fallback, "_tts_foreground_event", promotion_event)
        setattr(fallback, "_edge_fallback_source", True)
        return fallback

    async def _resolve_edge_gtts_fallback(
        self,
        state: GuildTTSState,
        item: QueueItem,
        *,
        allow_stream: bool,
    ) -> tuple[str, bool]:
        fallback = self._build_edge_gtts_fallback_item(item)
        cached = self._try_get_cached_path(state, fallback)
        if cached:
            return cached, False

        text_length = len(self._get_item_normalized_cache_text(fallback))
        fallback_key = self._cache_key(fallback)
        if text_length <= TTS_CACHEABLE_TEXT_MAX_LENGTH:
            store_in_cache = True
        else:
            seen_count = self._remember_long_text_repeat(fallback_key)
            store_in_cache = bool(
                text_length <= TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH
                and seen_count >= TTS_LONG_TEXT_CACHE_MIN_REPEATS
            )

        stream_allowed, _ = self._gtts_streaming_allowed_for(fallback)
        if allow_stream and stream_allowed:
            try:
                self._record_cache_miss("gtts")
                handle = await self._prepare_gtts_stream(
                    state,
                    fallback,
                    store_in_cache=store_in_cache,
                )
                return handle.fifo_path, True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[tts_edge_fallback] gTTS progressivo indisponível; usando arquivo completo | guild=%s erro=%s",
                    item.guild_id,
                    exc,
                )

        foreground = not bool(getattr(item, "_tts_prefetch", False))
        source_path = await self._run_timed_generation(
            "gtts",
            lambda: self._generate_gtts_file_with_priority(
                item.text,
                item.language,
                foreground=foreground,
            ),
            guild_id=item.guild_id,
        )
        if not store_in_cache:
            return source_path, True
        cached_path = await self._store_in_cache(state, fallback, source_path)
        return cached_path, os.path.abspath(cached_path) == os.path.abspath(source_path)

    async def _resolve_audio_path(
        self,
        state: GuildTTSState,
        item: QueueItem,
        *,
        allow_edge_stream: bool = False,
    ) -> tuple[str, bool]:
        normalized_text = self._get_item_normalized_cache_text(item)
        text_length = len(normalized_text)

        stream_allowed, stream_reason = self._edge_streaming_allowed_for(item)
        if allow_edge_stream and stream_allowed:
            cached = self._try_get_cached_path(state, item)
            if cached:
                return cached, False

            key = self._cache_key(item)
            existing = self._get_inflight_cache_tasks().get(key)
            if existing is not None:
                return await existing

            if self._edge_circuit_is_open():
                self._record_edge_circuit_bypass()
                return await self._resolve_edge_gtts_fallback(
                    state,
                    item,
                    allow_stream=allow_edge_stream,
                )

            if text_length <= TTS_CACHEABLE_TEXT_MAX_LENGTH:
                store_in_cache = True
            else:
                seen_count = self._remember_long_text_repeat(key)
                store_in_cache = bool(
                    text_length <= TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH
                    and seen_count >= TTS_LONG_TEXT_CACHE_MIN_REPEATS
                )

            self._record_cache_miss(item.engine)
            try:
                handle = await self._prepare_edge_stream(
                    state,
                    item,
                    store_in_cache=store_in_cache,
                )
                return handle.fifo_path, True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics = self._get_metrics_store()
                metrics["edge_stream_fallbacks"] = int(metrics.get("edge_stream_fallbacks", 0) or 0) + 1
                logger.warning(
                    "[tts_edge_stream] falha antes do primeiro áudio; usando gTTS | guild=%s rota=%s erro=%s",
                    item.guild_id,
                    stream_reason,
                    exc,
                )
                return await self._resolve_edge_gtts_fallback(
                    state,
                    item,
                    allow_stream=allow_edge_stream,
                )

        gtts_stream_allowed, gtts_stream_reason = self._gtts_streaming_allowed_for(item)
        if allow_edge_stream and gtts_stream_allowed:
            cached = self._try_get_cached_path(state, item)
            if cached:
                return cached, False

            key = self._cache_key(item)
            existing = self._get_inflight_cache_tasks().get(key)
            if existing is not None:
                return await existing

            if text_length <= TTS_CACHEABLE_TEXT_MAX_LENGTH:
                store_in_cache = True
            else:
                seen_count = self._remember_long_text_repeat(key)
                store_in_cache = bool(
                    text_length <= TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH
                    and seen_count >= TTS_LONG_TEXT_CACHE_MIN_REPEATS
                )

            self._record_cache_miss(item.engine)
            try:
                handle = await self._prepare_gtts_stream(
                    state,
                    item,
                    store_in_cache=store_in_cache,
                )
                return handle.fifo_path, True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics = self._get_metrics_store()
                metrics["gtts_stream_fallbacks"] = int(metrics.get("gtts_stream_fallbacks", 0) or 0) + 1
                logger.warning(
                    "[tts_gtts_stream] falha antes do primeiro áudio; usando arquivo completo | guild=%s rota=%s erro=%s",
                    item.guild_id,
                    gtts_stream_reason,
                    exc,
                )
                fallback_path = await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file_with_priority(
                        item.text,
                        item.language,
                        foreground=not bool(getattr(item, "_tts_prefetch", False)),
                    ),
                    guild_id=item.guild_id,
                )
                return fallback_path, True

        if text_length <= TTS_CACHEABLE_TEXT_MAX_LENGTH:
            return await self._resolve_or_generate_singleflight_audio(
                state,
                item,
                read_cache=True,
                store_in_cache=True,
            )

        cached = self._try_get_cached_path(state, item)
        if cached:
            return cached, False

        key = self._cache_key(item)
        seen_count = self._remember_long_text_repeat(key)

        should_cache_long_text = (
            text_length <= TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH
            and seen_count >= TTS_LONG_TEXT_CACHE_MIN_REPEATS
        )

        self._record_cache_miss(item.engine)
        return await self._resolve_or_generate_singleflight_audio(
            state,
            item,
            read_cache=False,
            store_in_cache=should_cache_long_text,
        )

    async def _wait_until_voice_playable_for_tts(self, vc: discord.VoiceClient, *, item: QueueItem | None = None) -> None:
        guild = getattr(vc, "guild", None)
        guild_id = int(getattr(guild, "id", 0) or getattr(item, "guild_id", 0) or 0)
        if not self._voice_client_is_connected(vc):
            raise RuntimeError("voice client não está conectado")

        if not self._voice_client_is_playing_or_paused(vc):
            return

        if guild_id and self._is_music_active_for_guild(guild_id):
            raise RuntimeError("voice client ocupado com música ativa")

        deadline = time.monotonic() + 1.8
        while time.monotonic() < deadline:
            if not self._voice_client_is_connected(vc):
                raise RuntimeError("voice client desconectou antes do playback")
            if not self._voice_client_is_playing_or_paused(vc):
                return
            await asyncio.sleep(0.09)

        logger.warning(
            "[tts_voice] playback anterior parece preso; parando antes do próximo TTS | guild=%s channel=%s",
            guild_id or None,
            getattr(item, "channel_id", None),
        )
        with contextlib.suppress(Exception):
            vc.stop()
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            if not self._voice_client_is_connected(vc):
                raise RuntimeError("voice client desconectou depois de parar playback preso")
            if not self._voice_client_is_playing_or_paused(vc):
                return
            await asyncio.sleep(0.08)
        raise RuntimeError("voice client continuou tocando após stop de segurança")

    async def _resolve_and_prime_audio(
        self,
        audio_task: asyncio.Future,
        item: QueueItem,
    ) -> tuple[str, bool, _PreparedTTSPlayback | None]:
        path, should_cleanup = await audio_task
        if not TTS_FFMPEG_PRIME_ENABLED or not path:
            return path, should_cleanup, None
        if self._is_music_active_for_guild(item.guild_id):
            return path, should_cleanup, None

        edge_stream = self._edge_stream_handle_for_path(path)
        router = getattr(getattr(self, "bot", None), "audio_router", None)
        if edge_stream is None and callable(getattr(router, "play_tts", None)):
            # Arquivos comuns continuam passando pelo roteador, que também
            # coordena ducking e ownership. O FIFO progressivo já é um fast
            # path local e pode ser preparado sem alterar essa decisão.
            return path, should_cleanup, None
        source = None
        read_task: asyncio.Task | None = None
        started_at = time.monotonic()
        try:
            if edge_stream is not None:
                await self._activate_edge_stream(edge_stream)
            source, source_kind = self._make_discord_tts_source(path)
            read_task = asyncio.create_task(asyncio.to_thread(source.read))
            first_frame = await asyncio.wait_for(
                asyncio.shield(read_task),
                timeout=TTS_FFMPEG_PRIME_TIMEOUT_SECONDS,
            )
            if not first_frame:
                raise RuntimeError("FFmpeg não produziu frame durante a preparação")
            prime_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
            self._record_latency_sample("source_prime", prime_ms)
            return (
                path,
                should_cleanup,
                _PreparedTTSPlayback(
                    path=path,
                    source=_PrimedAudioSource(source, first_frame),
                    source_kind=source_kind,
                    prime_ms=prime_ms,
                ),
            )
        except asyncio.CancelledError:
            if source is not None:
                with contextlib.suppress(Exception):
                    source.cleanup()
            if read_task is not None and not read_task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(read_task, timeout=0.5)
            if edge_stream is not None:
                await self._finalize_edge_stream(edge_stream, cancel=True)
            elif should_cleanup and path:
                await self._discard_edge_stream_path(path)
            raise
        except Exception as exc:
            if source is not None:
                with contextlib.suppress(Exception):
                    source.cleanup()
            if read_task is not None and not read_task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(read_task, timeout=0.5)
            if edge_stream is not None:
                await self._finalize_edge_stream(edge_stream, cancel=True)
                raise RuntimeError("preparação antecipada do stream falhou") from exc
            self._log_debug(
                f"[tts_perf] preparação antecipada ignorada | guild={item.guild_id} erro={type(exc).__name__}: {exc}"
            )
            return path, should_cleanup, None

    async def _abandon_resolved_audio_task(self, task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            path, should_cleanup, prepared = task.result()
            if prepared is not None:
                prepared.cleanup()
            if should_cleanup and path:
                await self._discard_edge_stream_path(path)

    async def _play_file(
        self,
        vc: discord.VoiceClient,
        path: str,
        *,
        item: QueueItem | None = None,
        prepared: _PreparedTTSPlayback | None = None,
    ) -> dict[str, float]:
        guild = getattr(vc, "guild", None)
        guild_id = int(getattr(guild, "id", 0) or getattr(item, "guild_id", 0) or 0)
        lock = self._get_tts_playback_lock(guild_id) if guild_id else asyncio.Lock()
        edge_stream = self._edge_stream_handle_for_path(path)
        stream_playback_ok = False
        playback_counted = False
        source_handed_to_player = False

        async with lock:
            loop = asyncio.get_running_loop()
            finished = loop.create_future()
            first_frame = loop.create_future()

            def _after_playback(error: Optional[Exception]) -> None:
                if error:
                    if not finished.done():
                        loop.call_soon_threadsafe(finished.set_exception, error)
                else:
                    if not finished.done():
                        loop.call_soon_threadsafe(finished.set_result, None)

            def _on_first_frame(frame_at: float, first_read_ms: float) -> None:
                if edge_stream is not None:
                    self._close_edge_stream_reader_anchor(edge_stream)
                def _publish() -> None:
                    if not first_frame.done():
                        first_frame.set_result((frame_at, first_read_ms))
                loop.call_soon_threadsafe(_publish)

            def _on_stream_read(read_ms: float) -> None:
                if edge_stream is None or edge_stream.engine != "edge":
                    return
                edge_stream.max_source_read_ms = max(edge_stream.max_source_read_ms, read_ms)
                if read_ms >= TTS_EDGE_STREAM_STALL_THRESHOLD_MS:
                    edge_stream.source_read_stalls += 1

            source = None
            used_prepared_source = False
            try:
                router = getattr(getattr(self, "bot", None), "audio_router", None)
                play_tts = getattr(router, "play_tts", None)
                if prepared is not None and (
                    os.path.abspath(prepared.path) != os.path.abspath(path)
                    or self._is_music_active_for_guild(guild_id)
                    or bool(getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc))
                ):
                    prepared.cleanup()
                    prepared = None
                # FIFO é um fast path exclusivamente local. Música/Lavalink e
                # agent são filtrados antes da síntese; pular o router aqui
                # impede que uma rota remota tente tratar o pipe como arquivo.
                if edge_stream is None and prepared is None and callable(play_tts) and guild is not None:
                    router_result = await play_tts(
                        guild=guild,
                        vc=vc,
                        path=path,
                        before_options=TTS_FFMPEG_BEFORE_OPTIONS,
                        options=TTS_FFMPEG_OPTIONS,
                        timeout=self._estimate_playback_timeout(item),
                        item=item,
                    )
                    if not (isinstance(router_result, dict) and router_result.get("tts_lavalink_failed")):
                        return router_result

                    fallback = getattr(router, "prepare_tts_local_fallback_after_lavalink_failure", None)
                    if callable(fallback):
                        reason = str(router_result.get("tts_lavalink_error") or router_result.get("error") or "tts_lavalink_failed")
                        fallback_vc = await fallback(guild, vc, reason=reason)
                        if fallback_vc is not None and not getattr(router, "_is_lavalink_voice_client", lambda _vc: False)(fallback_vc):
                            vc = fallback_vc
                            guild = getattr(vc, "guild", guild)
                            logger.warning(
                                "[tts_voice] TTS via Lavalink falhou; usando playback local direto | guild=%s reason=%s",
                                getattr(guild, "id", None),
                                reason,
                            )
                        else:
                            return router_result
                    else:
                        return router_result

                await self._wait_until_voice_playable_for_tts(vc, item=item)

                if edge_stream is not None and prepared is None:
                    await self._activate_edge_stream(edge_stream)

                source_setup_started_at = time.monotonic()
                source_prime_ms = 0.0
                if prepared is not None:
                    source = prepared.take_source()
                    source_kind = prepared.source_kind
                    source_prime_ms = prepared.prime_ms
                    used_prepared_source = True
                else:
                    source, source_kind = self._make_discord_tts_source(path)
                if callable(getattr(source, "read", None)):
                    source = _FirstFrameAudioSource(
                        source,
                        _on_first_frame,
                        _on_stream_read if edge_stream is not None else None,
                    )
                source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)

                play_call_started_at = time.monotonic()
                setattr(self, "_tts_active_playbacks", int(getattr(self, "_tts_active_playbacks", 0) or 0) + 1)
                playback_counted = True
                try:
                    vc.play(source, after=_after_playback)
                    source_handed_to_player = True
                except Exception:
                    raise
                play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)

                playback_started_at = time.monotonic()
                playback_timeout = self._estimate_playback_timeout(item)
                try:
                    await asyncio.wait_for(finished, timeout=playback_timeout)
                except asyncio.TimeoutError as exc:
                    with contextlib.suppress(Exception):
                        if self._voice_client_is_playing_or_paused(vc):
                            vc.stop()
                    raise RuntimeError(f"Playback timeout após {playback_timeout:.1f}s") from exc
                await asyncio.sleep(0)
                playback_duration_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
                first_frame_at = playback_started_at
                first_read_ms = 0.0
                first_frame_observed = bool(first_frame.done() and not first_frame.cancelled())
                if first_frame_observed:
                    with contextlib.suppress(Exception):
                        first_frame_at, first_read_ms = first_frame.result()
                first_frame_ms = max(0.0, (first_frame_at - play_call_started_at) * 1000.0)
                if first_frame_observed:
                    self._record_latency_sample("first_frame", first_frame_ms)
                    self._record_latency_sample("first_source_read", first_read_ms)
                else:
                    metrics = self._get_metrics_store()
                    metrics["first_frame_unobserved"] = int(metrics.get("first_frame_unobserved", 0) or 0) + 1
                result = {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": play_call_ms,
                    "playback_ms": playback_duration_ms,
                    "playback_started_at": playback_started_at,
                    "first_frame_at": first_frame_at,
                    "first_frame_ms": first_frame_ms,
                    "first_read_ms": first_read_ms,
                    "source_prime_ms": source_prime_ms,
                    "source_primed": used_prepared_source,
                    "first_frame_observed": first_frame_observed,
                    "playback_source": source_kind,
                    "audio_format": self._path_audio_format(path),
                }
                if edge_stream is not None:
                    stream_engine = str(edge_stream.engine or "edge")
                    result["progressive_stream"] = True
                    result[f"{stream_engine}_stream"] = True
                    result[f"{stream_engine}_first_audio_ms"] = edge_stream.first_audio_ms
                    result[f"{stream_engine}_stream_bytes"] = edge_stream.audio_bytes
                    if edge_stream.error is not None:
                        result[f"{stream_engine}_stream_incomplete"] = True
                        logger.warning(
                            "[tts_stream] playback terminou com stream incompleto | guild=%s engine=%s erro=%s",
                            guild_id,
                            stream_engine,
                            edge_stream.error,
                        )
                    stream_playback_ok = True
                return result
            finally:
                if prepared is not None:
                    prepared.cleanup()
                if source is not None and not source_handed_to_player:
                    cleanup = getattr(source, "cleanup", None)
                    if callable(cleanup):
                        with contextlib.suppress(Exception):
                            cleanup()
                if playback_counted:
                    setattr(self, "_tts_active_playbacks", max(0, int(getattr(self, "_tts_active_playbacks", 0) or 0) - 1))
                if edge_stream is not None:
                    self._observe_edge_stream_playback(
                        edge_stream,
                        playback_ok=stream_playback_ok,
                    )
                    await self._finalize_edge_stream(edge_stream, cancel=not stream_playback_ok)

    def _is_already_playing_audio_error(self, exc: Exception | str) -> bool:
        return "already playing audio" in str(exc or "").lower()

    def _is_voice_disconnected_error(self, exc: Exception | str) -> bool:
        message = str(exc or "").lower()
        return any(
            marker in message
            for marker in (
                "not connected to voice",
                "voice client desconectou",
                "voice websocket",
                "websocket closed",
                "connection closed",
                "closing transport",
            )
        )

    def _is_music_active_for_guild(self, guild_id: int) -> bool:
        router = getattr(getattr(self, "bot", None), "audio_router", None)
        is_music_active = getattr(router, "is_music_active", None)
        if not callable(is_music_active):
            return False
        with contextlib.suppress(Exception):
            return bool(is_music_active(int(guild_id)))
        return False

    async def _reset_voice_client(self, guild: discord.Guild, *, reason: str = "unknown") -> None:
        lock_getter = getattr(self, "_get_voice_connect_lock", None)
        lock = lock_getter(guild.id) if callable(lock_getter) else None

        async def _do_reset() -> None:
            vc = self._get_voice_client_for_guild(guild)
            if vc is None:
                return
            if getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc):
                logger.info("[tts_voice] reset de voice client ignorado | player Lavalink ativo | guild=%s reason=%s", guild.id, reason)
                return
            try:
                if self._voice_client_is_playing_or_paused(vc):
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
                self._schedule_worker_voice_agent_clear_session(guild.id, reason=f"reset:{reason}")
            except Exception as exc:
                logger.warning(
                    "[tts_voice] Falha ao resetar voice client | guild=%s reason=%s erro_tipo=%s erro=%r",
                    guild.id,
                    reason,
                    type(exc).__name__,
                    exc,
                )
            state = self.guild_states.get(guild.id)
            if state is not None:
                state.last_channel_id = None
                state.last_hard_reset_at = time.monotonic()

        if lock is None:
            await _do_reset()
            return

        async with lock:
            await _do_reset()

    async def _play_file_with_recovery(
        self,
        guild: discord.Guild,
        item: QueueItem,
        vc: discord.VoiceClient,
        path: str,
        *,
        prepared: _PreparedTTSPlayback | None = None,
    ) -> dict[str, float]:
        if self._edge_stream_handle_for_path(path) is not None:
            # FIFO não pode ser reaberto para repetir o mesmo áudio: os bytes já
            # foram consumidos. A recuperação normal continua valendo para
            # arquivos completos e cacheados.
            if prepared is None:
                return await self._play_file(vc, path, item=item)
            return await self._play_file(vc, path, item=item, prepared=prepared)

        current_vc = vc
        last_error: Exception | None = None
        state = self.guild_states.get(guild.id)
        for attempt in range(2):
            try:
                current_prepared = prepared if attempt == 0 else None
                if current_prepared is None:
                    return await self._play_file(current_vc, path, item=item)
                return await self._play_file(
                    current_vc,
                    path,
                    item=item,
                    prepared=current_prepared,
                )
            except Exception as exc:
                last_error = exc
                music_active = self._is_music_active_for_guild(guild.id)
                if music_active:
                    logger.warning(
                        "[tts_voice] Falha no playback do TTS com música ativa; descartando só este TTS sem resetar a call | guild=%s channel=%s erro_tipo=%s erro=%r",
                        guild.id,
                        item.channel_id,
                        type(exc).__name__,
                        exc,
                    )
                    now = time.monotonic()
                    return {
                        "source_setup_ms": 0.0,
                        "play_call_ms": 0.0,
                        "playback_ms": 0.0,
                        "playback_started_at": now,
                        "tts_discarded": True,
                    }

                if self._is_already_playing_audio_error(exc) and attempt == 0:
                    logger.warning(
                        "[tts_voice] voice client já estava tocando; aguardando limpar sem resetar call | guild=%s channel=%s",
                        guild.id,
                        item.channel_id,
                    )
                    with contextlib.suppress(Exception):
                        await self._wait_until_voice_playable_for_tts(current_vc, item=item)
                    await asyncio.sleep(0.15)
                    continue

                logger.warning(
                    "[tts_voice] Falha no playback, tentando recuperar | guild=%s channel=%s tentativa=%s erro_tipo=%s erro=%r",
                    guild.id,
                    item.channel_id,
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
                if attempt >= 1:
                    break

                if self._is_voice_disconnected_error(exc):
                    # _ensure_connected_fast -> _ensure_connected já limpa o
                    # cliente obsoleto sob a trava da guild. Um reset separado
                    # aqui iniciava uma segunda desconexão sobre a mesma sessão.
                    logger.info(
                        "[tts_voice] cliente desconectado; recuperação delegada ao controlador único | guild=%s channel=%s",
                        guild.id,
                        item.channel_id,
                    )
                else:
                    last_hard_reset_at = float(getattr(state, "last_hard_reset_at", 0.0) or 0.0) if state is not None else 0.0
                    time_since_reset = time.monotonic() - last_hard_reset_at if last_hard_reset_at > 0.0 else TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS
                    should_suppress_hard_reset = time_since_reset < TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS

                    if should_suppress_hard_reset:
                        logger.warning(
                            "[tts_voice] Hard reset suprimido para evitar reconexão em loop | guild=%s channel=%s cooldown_restante=%.2fs",
                            guild.id,
                            item.channel_id,
                            max(0.0, TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS - time_since_reset),
                        )
                    else:
                        await self._reset_voice_client(guild, reason=f"playback_failure:{type(exc).__name__}")
                        await asyncio.sleep(0.25)

                current_vc = await self._ensure_connected_fast(guild, item)
                if current_vc is None:
                    break
        if last_error is None:
            raise RuntimeError("Falha desconhecida no playback do TTS")
        raise last_error

    async def _ensure_self_deaf_fast(self, guild: discord.Guild, target_channel=None) -> bool:
        should_self_deaf = True
        try:
            if hasattr(self, "_voice_should_self_deaf"):
                should_self_deaf = bool(await self._maybe_await(self._voice_should_self_deaf(guild.id)))
        except Exception:
            should_self_deaf = True

        last_error = None
        for _ in range(3):
            try:
                me = getattr(guild, "me", None)
                me_voice = getattr(me, "voice", None)
                target = getattr(me_voice, "channel", None) or target_channel
                current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                if me_voice and current_self_deaf == should_self_deaf:
                    return True
                if target is None:
                    return False
                await guild.change_voice_state(channel=target, self_deaf=should_self_deaf)
                await asyncio.sleep(0.35)
                me = getattr(guild, "me", None)
                me_voice = getattr(me, "voice", None)
                current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                if me_voice and current_self_deaf == should_self_deaf:
                    return True
            except Exception as e:
                last_error = e
                await asyncio.sleep(0.35)
        if last_error is not None:
            logger.warning(
                "[tts_voice] Falha ao reaplicar estado de voz | guild=%s channel=%s self_deaf=%s erro=%s",
                guild.id,
                getattr(target_channel, "id", None),
                should_self_deaf,
                last_error,
            )
        return False

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
        if hasattr(self, "_get_guild_toggle_value"):
            try:
                auto_leave_enabled = await self._maybe_await(
                    self._get_guild_toggle_value(
                        guild.id,
                        public_key="auto_leave",
                        raw_key="auto_leave_enabled",
                        default=True,
                    )
                )
            except Exception as e:
                logger.warning("[tts_voice] Falha ao consultar auto_leave no idle timeout | guild=%s erro=%s", guild.id, e)
                auto_leave_enabled = True
            if not auto_leave_enabled:
                self._log_debug(f"[tts_voice] Idle timeout ignorado | auto_leave desativado | guild={guild.id}")
                return False

        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not self._voice_client_is_connected(vc) or self._voice_client_channel(vc) is None:
            return True

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        is_music_active = getattr(router, "is_music_active", None)
        if callable(is_music_active):
            with contextlib.suppress(Exception):
                if is_music_active(guild.id):
                    self._log_debug(f"[tts_voice] Idle timeout ignorado | player de música ativo | guild={guild.id}")
                    return False

        should_defer = getattr(router, "should_defer_tts_auto_leave", None)
        if callable(should_defer):
            with contextlib.suppress(Exception):
                if should_defer(guild.id):
                    schedule_idle = getattr(router, "schedule_music_idle_disconnect", None)
                    if callable(schedule_idle):
                        await schedule_idle(guild.id)
                    self._log_debug(f"[tts_voice] Idle timeout adiado | sessão de música aguardando timeout | guild={guild.id}")
                    return False

        members = list(getattr(self._voice_client_channel(vc), "members", []))
        humans = [m for m in members if not m.bot]
        if humans:
            self._log_debug(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False

        try:
            await vc.disconnect(force=False)
            self._schedule_worker_voice_agent_clear_session(guild.id, reason="idle_disconnect")
            if hasattr(self, "_clear_remembered_voice_channel"):
                with contextlib.suppress(Exception):
                    await self._maybe_await(self._clear_remembered_voice_channel(guild.id))
            logger.info("[tts_voice] Desconectado por inatividade | guild=%s", guild.id)
            return True
        except Exception as e:
            logger.warning("[tts_voice] Erro ao desconectar por inatividade | guild=%s erro=%s", guild.id, e)
            return False

    async def _ensure_connected_fast(self, guild: discord.Guild, item: QueueItem):
        state = self._get_state(guild.id)
        target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
        if target_channel is None:
            return None

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        should_route_agent = getattr(router, "should_route_tts_to_music_agent", None)
        if callable(should_route_agent) and not bool(getattr(item, "_skip_music_agent_tts_route", False)):
            with contextlib.suppress(Exception):
                if should_route_agent(guild.id, item.channel_id):
                    state.last_channel_id = item.channel_id
                    logger.debug("[tts_voice] conexão local ignorada; TTS será roteado pelo worker musical | guild=%s channel=%s", guild.id, item.channel_id)
                    return None

        vc = self._get_voice_client_for_guild(guild)
        if getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc):
            if not getattr(self, "_lavalink_music_should_own_voice", lambda _guild: False)(guild):
                # Wavelink ficou como voice_client fantasma depois de stop/falha,
                # mas o roteador não está usando Lavalink agora. Limpe para o TTS
                # local conseguir conectar/tocar normalmente.
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
                vc = None
            else:
                lavalink_channel = self._voice_client_channel(vc) or getattr(getattr(guild, "me", None), "voice", None) and getattr(getattr(guild, "me", None).voice, "channel", None)
                lavalink_channel_id = getattr(lavalink_channel, "id", None)
                if lavalink_channel_id is not None and lavalink_channel_id != item.channel_id:
                    now = time.monotonic()
                    if now >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                        logger.info(
                            "[tts_voice] TTS ignorado porque o Lavalink está em outro canal | guild=%s lavalink_channel=%s tts_channel=%s",
                            guild.id,
                            lavalink_channel_id,
                            item.channel_id,
                        )
                        state.lavalink_ignore_logged_until = now + 20.0
                    return None
                state.last_channel_id = int(lavalink_channel_id or item.channel_id)
                self._schedule_worker_voice_agent_register_session(guild, item, vc, source="tts_lavalink_shared")
                logger.debug("[tts_voice] TTS encaminhado para reprodução via Lavalink | guild=%s channel=%s", guild.id, state.last_channel_id)
                return vc

        lavalink_voice_guard = getattr(self, "_lavalink_music_should_own_voice", None)
        if callable(lavalink_voice_guard):
            try:
                if lavalink_voice_guard(guild):
                    now = time.monotonic()
                    if now >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                        logger.info(
                            "[tts_voice] TTS local ignorado porque o player de música via Wavelink está assumindo a voz | guild=%s tts_channel=%s",
                            guild.id,
                            item.channel_id,
                        )
                        state.lavalink_ignore_logged_until = now + 20.0
                    return None
            except Exception:
                logger.debug("[tts_voice] falha ao consultar guarda Lavalink antes do TTS local", exc_info=True)

        is_receive_client = bool(vc is not None and hasattr(vc, "listen") and hasattr(vc, "is_listening"))
        if vc is not None and self._voice_client_is_connected(vc):
            if is_receive_client:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
                vc = None
            elif self._voice_client_channel(vc) is not None and self._voice_client_channel(vc).id == item.channel_id:
                await self._ensure_self_deaf_fast(guild, target_channel)
                state.last_channel_id = item.channel_id
                self._schedule_worker_voice_agent_register_session(guild, item, vc, source="tts_local_voice")
                return vc
            else:
                try:
                    await vc.move_to(target_channel)
                    await self._ensure_self_deaf_fast(guild, target_channel)
                    state.last_channel_id = item.channel_id
                    self._schedule_worker_voice_agent_register_session(guild, item, vc, source="tts_local_voice")
                    return vc
                except Exception:
                    pass

        vc = await self._maybe_await(self._ensure_connected(
            guild,
            target_channel,
            report_failure=True,
            failure_context=f"entrada automática do TTS para reproduzir mensagem de {item.author_id}",
            defer_post_connect=True,
        ))
        if vc is None:
            current = self._get_voice_client_for_guild(guild)
            if current is not None and self._voice_client_is_connected(current):
                if self._voice_client_channel(current) is not None and self._voice_client_channel(current).id == item.channel_id:
                    state.last_channel_id = item.channel_id
                    return current
            return None

        if self._voice_client_is_connected(vc):
            pending = getattr(self, "_voice_post_connect_pending", None)
            post_connect_is_pending = bool(
                isinstance(pending, dict) and pending.get(guild.id) is vc
            )
            if not post_connect_is_pending:
                await self._ensure_self_deaf_fast(guild, target_channel)
            state.last_channel_id = item.channel_id
            self._schedule_worker_voice_agent_register_session(guild, item, vc, source="tts_local_voice")
        return vc

    async def _maybe_prefetch_next(self, state: GuildTTSState):
        prefetched_item: Optional[QueueItem] = None
        prefetched_audio_task: Optional[asyncio.Task] = None

        if state.queue.empty():
            return prefetched_item, prefetched_audio_task

        try:
            prefetched_item = state.queue.get_nowait()
            self._decrement_pending_signature(state, prefetched_item)
        except asyncio.QueueEmpty:
            return None, None

        setattr(prefetched_item, "_dequeued_at_monotonic", time.monotonic())
        engine = str(getattr(prefetched_item, "engine", "") or "gtts").strip().lower().replace("-", "_")
        if engine in {"edge", "gtts"}:
            setattr(prefetched_item, "_tts_prefetch", True)
            setattr(prefetched_item, "_tts_foreground_event", asyncio.Event())
            self._record_prefetch_started()
            prefetched_audio_task = asyncio.create_task(
                self._resolve_audio_path(state, prefetched_item, allow_edge_stream=True)
            )
        return prefetched_item, prefetched_audio_task

    async def _wait_and_prefetch_next(self, state: GuildTTSState):
        item = await state.queue.get()
        self._decrement_pending_signature(state, item)
        setattr(item, "_dequeued_at_monotonic", time.monotonic())
        engine = str(getattr(item, "engine", "") or "gtts").strip().lower().replace("-", "_")
        audio_task: asyncio.Task | None = None
        if engine in {"edge", "gtts"}:
            setattr(item, "_tts_prefetch", True)
            setattr(item, "_tts_foreground_event", asyncio.Event())
            self._record_prefetch_started()
            audio_task = asyncio.create_task(
                self._resolve_audio_path(state, item, allow_edge_stream=True)
            )
        return item, audio_task

    def _promote_prefetched_audio(self, item: QueueItem, audio_task: asyncio.Task | None) -> None:
        if not bool(getattr(item, "_tts_prefetch", False)):
            return
        setattr(item, "_tts_prefetch", False)
        setattr(item, "_tts_was_prefetched", True)
        promotion_event = getattr(item, "_tts_foreground_event", None)
        if isinstance(promotion_event, asyncio.Event):
            promotion_event.set()
        promoted = self._get_synth_semaphore().promote(audio_task)
        promoted = self._get_gtts_semaphore().promote(audio_task) or promoted
        metrics = self._get_metrics_store()
        metrics["prefetch_promoted"] = int(metrics.get("prefetch_promoted", 0) or 0) + 1
        if promoted:
            metrics["prefetch_waiter_promoted"] = int(metrics.get("prefetch_waiter_promoted", 0) or 0) + 1

    async def _worker_loop(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        prefetched_item: Optional[QueueItem] = None
        prefetched_audio_task: Optional[asyncio.Task] = None

        try:
            while True:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self._log_debug(f"[tts_voice] Guild não encontrada no worker | guild={guild_id}")
                    return

                fetched_from_queue = False
                arrival_prefetch_task: asyncio.Task | None = None

                if not state.dashboard_enabled:
                    if prefetched_audio_task is not None:
                        if not prefetched_audio_task.done():
                            prefetched_audio_task.cancel()
                            with contextlib.suppress(BaseException):
                                await prefetched_audio_task
                        elif not prefetched_audio_task.cancelled():
                            with contextlib.suppress(Exception):
                                prefetched_path, should_cleanup = prefetched_audio_task.result()
                                if should_cleanup and prefetched_path:
                                    await self._discard_edge_stream_path(prefetched_path)
                    if prefetched_item is not None:
                        state.queue.task_done()
                    return

                if prefetched_item is not None:
                    item = prefetched_item
                    fetched_from_queue = True
                    prefetched_item = None
                    audio_task = prefetched_audio_task
                    prefetched_audio_task = None
                    self._promote_prefetched_audio(item, audio_task)
                else:
                    try:
                        timeout = TTS_IDLE_DISCONNECT_SECONDS
                        if state.warmed_until > time.monotonic():
                            timeout = min(timeout, max(1.0, state.warmed_until - time.monotonic()))
                        item = await asyncio.wait_for(state.queue.get(), timeout=timeout)
                        self._decrement_pending_signature(state, item)
                        setattr(item, "_dequeued_at_monotonic", time.monotonic())
                        fetched_from_queue = True
                    except asyncio.TimeoutError:
                        if state.warmed_until > time.monotonic():
                            continue
                        disconnected = await self._disconnect_idle(guild)
                        if disconnected:
                            return
                        continue
                    audio_task = None

                try:
                    if hasattr(self, "_should_block_for_voice_bot"):
                        target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
                        if target_channel is not None:
                            blocked = await self._maybe_await(self._should_block_for_voice_bot(guild, target_channel))
                            if blocked:
                                logger.info("[tts_voice] Worker bloqueado por outro bot de voz | guild=%s channel=%s", guild_id, item.channel_id)
                                if hasattr(self, "_disconnect_if_blocked"):
                                    await self._maybe_await(self._disconnect_if_blocked(guild))
                                continue

                    router = getattr(getattr(self, "bot", None), "audio_router", None)
                    should_route_agent = getattr(router, "should_route_tts_to_music_agent", None) if router is not None else None
                    play_agent_tts = getattr(router, "play_tts_via_music_agent", None) if router is not None else None
                    if (
                        callable(should_route_agent)
                        and callable(play_agent_tts)
                        and not bool(getattr(item, "_skip_music_agent_tts_route", False))
                        and should_route_agent(guild.id, item.channel_id)
                    ):
                        if audio_task is not None and not audio_task.done():
                            audio_task.cancel()
                            with contextlib.suppress(BaseException):
                                await audio_task
                        elif audio_task is not None and not audio_task.cancelled():
                            with contextlib.suppress(Exception):
                                routed_path, routed_cleanup = audio_task.result()
                                if routed_cleanup and routed_path:
                                    await self._discard_edge_stream_path(routed_path)
                        dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
                        try:
                            playback_result = await play_agent_tts(
                                guild_id=guild.id,
                                channel_id=item.channel_id,
                                text=item.text,
                                engine=item.engine,
                                voice=item.voice,
                                language=item.language,
                                rate=item.rate,
                                pitch=item.pitch,
                                timeout=self._estimate_playback_timeout(item),
                            )
                            playback_started_at = float(playback_result.get("playback_started_at", time.monotonic()) or time.monotonic()) if isinstance(playback_result, dict) else time.monotonic()
                            queue_wait_ms = max(0.0, (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0)
                            dispatch_ms = max(0.0, (playback_started_at - dequeue_started_at) * 1000.0)
                            playback_ms = max(0.0, float((playback_result or {}).get("playback_ms", 0.0) or 0.0)) if isinstance(playback_result, dict) else 0.0
                            self._record_queue_timing(
                                queue_wait_ms=queue_wait_ms,
                                dispatch_ms=dispatch_ms,
                                source_setup_ms=0.0,
                                play_call_ms=0.0,
                                playback_ms=playback_ms,
                                total_to_playback_ms=max(0.0, (playback_started_at - float(getattr(item, "enqueued_at_monotonic", playback_started_at))) * 1000.0),
                            )
                            self._schedule_worker_voice_agent_register_session(guild, item, None, source="tts_music_agent_route")
                            logger.info(
                                "[tts_voice] TTS roteado pelo worker musical | guild=%s channel=%s engine=%s ok=%s",
                                guild_id,
                                item.channel_id,
                                item.engine,
                                bool(isinstance(playback_result, dict) and playback_result.get("ok", True)),
                            )
                        except Exception as exc:
                            exc_text = str(exc or "")
                            exc_lower = exc_text.lower()
                            music_active = bool(self._is_music_active_for_guild(int(guild.id)))
                            safe_to_fallback = (
                                not music_active
                                or "sem sessão musical ativa" in exc_lower
                                or "no active music" in exc_lower
                                or "no music session" in exc_lower
                                or "music session" in exc_lower
                            )
                            if safe_to_fallback:
                                setattr(item, "_skip_music_agent_tts_route", True)
                                logger.warning(
                                    "[tts_voice] TTS do worker musical falhou; seguindo fallback seguro | guild=%s channel=%s erro=%s",
                                    guild_id,
                                    item.channel_id,
                                    exc,
                                )
                            else:
                                logger.warning(
                                    "[tts_voice] TTS do worker musical falhou; mantendo música ativa e descartando TTS para não interromper | guild=%s channel=%s erro=%s",
                                    guild_id,
                                    item.channel_id,
                                    exc,
                                )
                                continue
                        else:
                            continue

                    engine = str(getattr(item, "engine", "") or "gtts").strip().lower().replace("-", "_")
                    if audio_task is None and engine in {"edge", "gtts"}:
                        cached_path = self._try_get_cached_path(state, item)
                        if cached_path:
                            ready_audio = asyncio.get_running_loop().create_future()
                            ready_audio.set_result((cached_path, False))
                            audio_task = ready_audio

                    direct_worker_result = None
                    if audio_task is None:
                        direct_worker_result = await self._try_worker_voice_direct_tts(guild, item)
                    if direct_worker_result is not None:
                        dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
                        playback_started_at = float(direct_worker_result.get("playback_started_at", time.monotonic()) or time.monotonic())
                        queue_wait_ms = max(0.0, (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0)
                        playback_ms = max(0.0, float(direct_worker_result.get("playback_ms", 0.0) or 0.0))
                        dispatch_ms = max(0.0, (playback_started_at - dequeue_started_at) * 1000.0)
                        self._record_queue_timing(
                            queue_wait_ms=queue_wait_ms,
                            dispatch_ms=dispatch_ms,
                            source_setup_ms=0.0,
                            play_call_ms=0.0,
                            playback_ms=playback_ms,
                            total_to_playback_ms=max(0.0, (playback_started_at - float(getattr(item, "enqueued_at_monotonic", playback_started_at))) * 1000.0),
                        )
                        continue

                    existing_vc = self._get_voice_client_for_guild(guild)
                    existing_channel = self._voice_client_channel(existing_vc)
                    setattr(
                        item,
                        "_tts_call_was_hot",
                        bool(
                            existing_vc is not None
                            and self._voice_client_is_connected(existing_vc)
                            and existing_channel is not None
                            and int(getattr(existing_channel, "id", 0) or 0) == int(item.channel_id)
                        ),
                    )
                    connect_task = asyncio.create_task(self._ensure_connected_fast(guild, item))
                    own_audio_task = None
                    if audio_task is None:
                        own_audio_task = asyncio.create_task(
                            self._resolve_audio_path(state, item, allow_edge_stream=True)
                        )
                        active_audio_task = own_audio_task
                    else:
                        active_audio_task = audio_task
                    resolved_audio_task = asyncio.create_task(
                        self._resolve_and_prime_audio(active_audio_task, item)
                    )

                    if prefetched_item is None and not state.queue.empty():
                        prefetched_item, prefetched_audio_task = await self._maybe_prefetch_next(state)
                    elif prefetched_item is None:
                        # Observa a fila durante conexão/síntese/playback. Assim,
                        # uma mensagem que chega enquanto a voz atual toca já
                        # começa a sintetizar, em vez de esperar o áudio acabar.
                        arrival_prefetch_task = asyncio.create_task(
                            self._wait_and_prefetch_next(state)
                        )

                    try:
                        vc = await connect_task
                    except BaseException:
                        await self._abandon_resolved_audio_task(resolved_audio_task)
                        raise
                    if vc is None:
                        await self._abandon_resolved_audio_task(resolved_audio_task)
                        if time.monotonic() >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                            logger.warning("[tts_voice] Worker não conseguiu conectar | guild=%s channel=%s", guild_id, item.channel_id)
                        continue

                    current_path, should_cleanup, prepared_playback = await resolved_audio_task
                    edge_stream = self._edge_stream_handle_for_path(current_path)
                    valid_stream = bool(edge_stream is not None and os.path.exists(current_path))
                    valid_file = bool(
                        current_path
                        and os.path.isfile(current_path)
                        and os.path.getsize(current_path) > 0
                    )
                    if not (valid_stream or valid_file):
                        logger.warning(
                            "[tts_voice] áudio temporário sumiu antes do playback; descartando item sem resetar voice | guild=%s channel=%s path=%s",
                            guild_id,
                            item.channel_id,
                            current_path,
                        )
                        if prepared_playback is not None:
                            prepared_playback.cleanup()
                        if should_cleanup and current_path:
                            await self._discard_edge_stream_path(current_path)
                        continue

                    dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
                    queue_wait_ms = max(0.0, (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0)

                    try:
                        playback_result = await self._play_file_with_recovery(
                            guild,
                            item,
                            vc,
                            current_path,
                            prepared=prepared_playback,
                        )
                        playback_started_at = float(playback_result.get("playback_started_at", time.monotonic()) or time.monotonic())
                        first_frame_at = float(playback_result.get("first_frame_at", playback_started_at) or playback_started_at)
                        source_setup_ms = max(0.0, float(playback_result.get("source_setup_ms", 0.0) or 0.0))
                        play_call_ms = max(0.0, float(playback_result.get("play_call_ms", 0.0) or 0.0))
                        playback_duration_ms = max(0.0, float(playback_result.get("playback_ms", 0.0) or 0.0))
                        dispatch_ms = max(0.0, (first_frame_at - dequeue_started_at) * 1000.0)
                        total_to_playback_ms = max(0.0, (first_frame_at - float(getattr(item, "enqueued_at_monotonic", first_frame_at))) * 1000.0)
                        first_frame_observed = bool(playback_result.get("first_frame_observed", False))
                        if first_frame_observed:
                            self._record_latency_sample("total_to_first_frame", total_to_playback_ms)
                            source_label = "progressive" if edge_stream is not None else ("cache" if not should_cleanup else "file")
                            call_label = "hot" if bool(getattr(item, "_tts_call_was_hot", False)) else "cold"
                            priority_label = "prefetched" if bool(getattr(item, "_tts_was_prefetched", False)) else "foreground"
                            self._record_latency_sample(
                                f"total_to_first_frame:{item.engine}:{source_label}:{call_label}:{priority_label}",
                                total_to_playback_ms,
                            )
                        else:
                            self._record_latency_sample("total_to_play_call", total_to_playback_ms)
                        self._record_queue_timing(
                            queue_wait_ms=queue_wait_ms,
                            dispatch_ms=dispatch_ms,
                            source_setup_ms=source_setup_ms,
                            play_call_ms=play_call_ms,
                            playback_ms=playback_duration_ms,
                            total_to_playback_ms=total_to_playback_ms,
                        )
                        logger.debug(
                            "[tts_perf] pronto para playback | guild=%s engine=%s queue_wait_ms=%.2f dispatch_ms=%.2f source_setup_ms=%.2f play_call_ms=%.2f total_to_playback_ms=%.2f text_len=%s",
                            guild_id,
                            item.engine,
                            queue_wait_ms,
                            dispatch_ms,
                            source_setup_ms,
                            play_call_ms,
                            total_to_playback_ms,
                            len(item.text or ""),
                        )
                    finally:
                        protected_paths: set[str] = set()
                        if should_cleanup and current_path:
                            await self._discard_edge_stream_path(current_path)
                        if prefetched_audio_task is not None and prefetched_audio_task.done() and not prefetched_audio_task.cancelled():
                            with contextlib.suppress(Exception):
                                prefetched_path, _ = prefetched_audio_task.result()
                                if prefetched_path:
                                    protected_paths.add(prefetched_path)
                        self._schedule_cache_maintenance(state, protected_paths=protected_paths)
                        state.warmed_until = time.monotonic() + TTS_WARM_HOLD_SECONDS

                except Exception as e:
                    logger.exception("[tts_voice] Erro no worker da guild %s: %s", guild_id, e)
                finally:
                    if arrival_prefetch_task is not None:
                        if not arrival_prefetch_task.done():
                            arrival_prefetch_task.cancel()
                            with contextlib.suppress(BaseException):
                                await arrival_prefetch_task
                        if arrival_prefetch_task.done() and not arrival_prefetch_task.cancelled():
                            with contextlib.suppress(Exception):
                                arrived_item, arrived_audio_task = arrival_prefetch_task.result()
                                if prefetched_item is None:
                                    prefetched_item = arrived_item
                                    prefetched_audio_task = arrived_audio_task
                                else:
                                    # Só seria possível em uma corrida com uma
                                    # extensão futura de prefetch; devolvemos o
                                    # item sem perder o task_done da fila.
                                    if arrived_audio_task is not None and not arrived_audio_task.done():
                                        arrived_audio_task.cancel()
                                    state.queue.task_done()
                    if fetched_from_queue:
                        state.queue.task_done()
        finally:
            state.worker_task = None
            self._cleanup_guild_state_if_idle(guild_id)
