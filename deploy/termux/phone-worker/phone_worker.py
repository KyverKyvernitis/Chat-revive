#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import colorsys
import hashlib
import io
import importlib
import json
import os
import platform
import re
import shutil
import socket
import shlex
import stat
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from types import SimpleNamespace

_APK_IDENTITY_MODULE: Any = None


def _load_apk_identity_module() -> Any:
    """Carrega sob demanda para o bootstrap de um arquivo conseguir reiniciar.

    Agents antigos só conhecem ``phone_worker.py`` na allowlist. O restante do
    runtime chega por ZIP logo após este núcleo novo subir; nenhuma rotina de APK
    é executada antes disso.
    """
    global _APK_IDENTITY_MODULE
    if _APK_IDENTITY_MODULE is not None:
        return _APK_IDENTITY_MODULE
    try:
        module = importlib.import_module("apk_identity")
    except ModuleNotFoundError:
        path = Path(__file__).with_name("apk_identity.py")
        spec = importlib.util.spec_from_file_location("core_phone_worker_apk_identity", path)
        if not path.is_file() or spec is None or spec.loader is None:
            raise RuntimeError("apk_identity.py ainda não foi instalado; aguarde o segundo estágio do auto-update")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    _APK_IDENTITY_MODULE = module
    return module


def inspect_apk_identity(*args: Any, **kwargs: Any) -> Any:
    return _load_apk_identity_module().inspect_apk_identity(*args, **kwargs)


def assert_expected_apk_identity(*args: Any, **kwargs: Any) -> Any:
    return _load_apk_identity_module().assert_expected_apk_identity(*args, **kwargs)

try:
    from PIL import Image, ImageSequence  # type: ignore
except Exception:
    Image = None
    ImageSequence = None

START_TIME = time.time()
JOBS_STARTED = 0
JOBS_FAILED = 0
_PING_CACHE: dict[str, Any] = {}
_ANDROID_TTS_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}
_CORE_WORKER_NETWORK_STATE: dict[str, Any] = {"last_ok_at": 0.0, "last_error_at": 0.0, "last_error": "", "last_error_kind": ""}
_CORE_JOB_LOCK = threading.RLock()
_CORE_JOB_ACTIVE: dict[str, Any] = {}
_CORE_JOB_LAST_RESULT: dict[str, Any] = {}
_PENDING_CORE_JOB_RESULTS: dict[str, dict[str, Any]] = {}
_APK_BUILD_THREAD_LOCK = threading.Lock()
_HEAVY_RESOURCE_LOCK = threading.Lock()
_TETO_RENDERER_LOCK = threading.RLock()
_TETO_RENDERER: Any = None
_TETO_RENDERER_ERROR = ""
_MUSIC_STREAM_LOCK = threading.RLock()
_MUSIC_STREAMS: dict[str, dict[str, Any]] = {}
PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = int(PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES * (PCM_FRAME_MS / 1000.0))

DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_OUTPUT_MB = 32
DEFAULT_TIMEOUT_SECONDS = 45
PHONE_WORKER_VERSION = "1.10.40"
CORE_WORKER_RUNTIME_MODE = "termux"
CORE_WORKER_INTERNAL_RUNTIME_STATE = "apk-preview-only"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_JOB_POLL_INTERVAL_SECONDS = 10
DEFAULT_CORE_JOB_RESULT_MAX_BYTES = 256 * 1024


def _early_env_truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    if text in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    return default


def _load_env_file_once(path: Path, *, override: bool = False) -> dict[str, str]:
    loaded: dict[str, str] = {}
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return loaded
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def _music_agent_env_file_early() -> Path:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    return Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()


def _ensure_music_agent_token_env(*, persist: bool = True) -> str:
    env_file = _music_agent_env_file_early()
    _load_env_file_once(env_file, override=False)
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    os.environ["MUSIC_AGENT_TOKEN"] = token
    if persist:
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
    return token


def _load_phone_worker_runtime_env() -> None:
    _load_env_file_once(Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), override=False)
    _load_env_file_once(_music_agent_env_file_early(), override=False)
    if _early_env_truthy(os.getenv("MUSIC_AGENT_AUTO_TOKEN"), True):
        _ensure_music_agent_token_env(persist=True)


_load_phone_worker_runtime_env()

SUPPORTED_DIRECT_TASKS = (
    "diagnostic_basic",
    "ffmpeg_check",
    "ffmpeg_convert",
    "ffprobe_check",
    "ffprobe_media",
    "health",
    "log_extract",
    "log_summary",
    "maintenance_plan",
    "music_ytdlp_resolve",
    "music_agent_command",
    "music_agent_status",
    "network_probe",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "sha256",
    "status",
    "tailscale_status",
    "text_stats",
    "tts_cache_lookup",
    "tts_cache_store",
    "tts_synthesize_benchmark",
    "tts_agent_status",
    "tts_agent_synthesize",
    "voice_agent_status",
    "voice_agent_register_session",
    "voice_agent_clear_session",
    "voice_agent_guild_status",
    "voice_agent_register_handoff",
    "voice_agent_clear_handoff",
    "voice_agent_handoff_status",
    "voice_agent_prepare_transfer",
    "voice_agent_begin_transfer",
    "voice_agent_release_transfer",
    "voice_agent_transfer_status",
    "voice_agent_play_tts",
    "voice_agent_probe_connection",
    "voice_agent_connection_status",
    "voice_agent_clear_connection",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "apk_build_debug",
    "apk_publish_last",
    "vps_assist_probe",
    "hash_batch",
    "endpoint_probe",
    "emoji_recolor",
    "media_probe",
    "audio_convert",
    "log_digest",
    "zip_audit",
    "boot_status",
    "boot_repair",
    "zip",
    "zip_validate",
)

SUPPORTED_CORE_WORKER_JOB_TYPES = (
    "diagnostic_basic",
    "ffmpeg_check",
    "ffprobe_check",
    "log_summary",
    "maintenance_plan",
    "music_ytdlp_resolve",
    "music_agent_command",
    "music_agent_status",
    "network_probe",
    "ping",
    "service_restart",
    "service_start",
    "service_status",
    "service_stop",
    "status",
    "tailscale_status",
    "text_stats",
    "tts_cache_lookup",
    "tts_cache_store",
    "tts_synthesize_benchmark",
    "tts_agent_status",
    "tts_agent_synthesize",
    "voice_agent_status",
    "voice_agent_register_session",
    "voice_agent_clear_session",
    "voice_agent_guild_status",
    "voice_agent_register_handoff",
    "voice_agent_clear_handoff",
    "voice_agent_handoff_status",
    "voice_agent_prepare_transfer",
    "voice_agent_begin_transfer",
    "voice_agent_release_transfer",
    "voice_agent_transfer_status",
    "voice_agent_play_tts",
    "voice_agent_probe_connection",
    "voice_agent_connection_status",
    "voice_agent_clear_connection",
    "worker_logs",
    "worker_self_check",
    "worker_update",
    "apk_build_debug",
    "apk_publish_last",
    "vps_assist_probe",
    "hash_batch",
    "endpoint_probe",
    "emoji_recolor",
    "media_probe",
    "audio_convert",
    "log_digest",
    "zip_audit",
    "boot_status",
    "boot_repair",
    "zip_validate",
)

CORE_WORKER_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "leve": {
        "label": "Leve",
        "roles": ["phone-worker", "diagnostics", "log-summary", "vps-assist"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "vps-assist", "hash-worker", "endpoint-probe", "worker-logs", "network-probe", "tailscale-status"],
    },
    "midia": {
        "label": "Mídia",
        "roles": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "worker-logs", "network-probe", "tailscale-status"],
    },
    "completo": {
        "label": "Completo",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "cache-worker", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "worker-logs", "network-probe", "tailscale-status", "service-control"],
    },
    "builder": {
        "label": "Builder",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "apk-builder", "zip-validate", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "apk-builder", "zip-validate", "vps-assist", "cache-worker", "hash-worker", "endpoint-probe", "media-probe", "worker-logs", "network-probe", "tailscale-status", "boot-repair", "service-control"],
    },
    "turbo": {
        "label": "Turbo",
        "roles": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "tts-synth", "tts-benchmark", "tts-agent", "voice-agent", "apk-builder", "vps-assist", "cache-worker"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "tts-synth", "tts-benchmark", "tts-agent", "tts-gtts", "tts-edge", "tts-android-native", "tts-teto", "voice-agent", "worker-voice", "shared-voice-session", "apk-builder", "vps-assist", "cache-worker", "music", "music-node", "music-lavalink", "music-ytdlp", "music-ytdlp-resolve", "hash-worker", "endpoint-probe", "media-probe", "audio-convert", "emoji-recolor", "worker-logs", "network-probe", "tailscale-status", "service-control"],
    },
    "bedrock": {
        "label": "Bedrock",
        "roles": ["phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup"],
        "capabilities": ["phone-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup", "worker-logs", "network-probe", "tailscale-status"],
    },
}



def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _load_env_file(path: str | None = None) -> None:
    """Carrega ~/.phone-worker.env sem sobrescrever variáveis já exportadas.

    Isso deixa o worker funcionar mesmo quando o script de start roda dentro do
    tmux sem exportar todas as variáveis CORE_WORKER_*.
    """
    raw_path = path or os.getenv("PHONE_WORKER_ENV") or str(Path.home() / ".phone-worker.env")
    env_path = Path(raw_path).expanduser()
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return
    for line in lines:
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export "):].strip()
        key, value = clean.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            if value[0] == '"':
                with contextlib.suppress(Exception):
                    value = json.loads(value)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    default = list(default or [])
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    items: list[str] = []
    for item in raw.replace(";", ",").split(","):
        clean = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", item.strip().lower()).strip("-._:")
        if clean and clean not in items:
            items.append(clean[:40])
    return items or default


def _normalize_core_worker_profile(value: Any) -> str:
    profile = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    if profile in CORE_WORKER_PROFILE_PRESETS:
        return profile
    return "midia"


def _core_worker_profile_label(profile: Any) -> str:
    normalized = _normalize_core_worker_profile(profile)
    return str(CORE_WORKER_PROFILE_PRESETS[normalized].get("label") or normalized.title())


def _core_worker_profile_roles(profile: Any) -> list[str]:
    normalized = _normalize_core_worker_profile(profile)
    return list(CORE_WORKER_PROFILE_PRESETS[normalized].get("roles") or CORE_WORKER_PROFILE_PRESETS["midia"]["roles"])


def _core_worker_profile_capabilities(profile: Any) -> list[str]:
    normalized = _normalize_core_worker_profile(profile)
    return list(CORE_WORKER_PROFILE_PRESETS[normalized].get("capabilities") or CORE_WORKER_PROFILE_PRESETS["midia"]["capabilities"])


def _merge_profile_contract(configured: list[str], required: list[str]) -> list[str]:
    """Preserva extensões locais sem deixar o perfil perder capacidades-base.

    Instalações antigas persistiram CORE_WORKER_ROLES/CAPABILITIES no env. Quando
    um preset ganhou uma capacidade nova, esse snapshot antigo continuava
    substituindo o preset inteiro e o painel mostrava Turbo enquanto o registry
    não recebia apk-builder. O perfil escolhido passa a ser o contrato mínimo;
    valores do env continuam aceitos como extensões.
    """
    merged: list[str] = []
    for item in list(required or []) + list(configured or []):
        clean = str(item or "").strip().lower()
        if clean and clean not in merged:
            merged.append(clean)
    return merged


def _current_core_worker_roles_and_capabilities() -> tuple[list[str], list[str]]:
    profile = _current_core_worker_profile()
    preset_roles = _core_worker_profile_roles(profile)
    preset_capabilities = _core_worker_profile_capabilities(profile)
    roles = _merge_profile_contract(_env_list("CORE_WORKER_ROLES", []), preset_roles)
    capabilities = _merge_profile_contract(_env_list("CORE_WORKER_CAPABILITIES", []), preset_capabilities)
    return roles, capabilities


def _supported_core_worker_job_types() -> list[str]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    caps = set(roles + capabilities)
    allowed = list(SUPPORTED_CORE_WORKER_JOB_TYPES)
    # Build Android é pesado e só deve aparecer para celular escolhido como builder.
    if "apk-builder" not in caps:
        allowed = [item for item in allowed if item != "apk_build_debug"]
    # Funções de assistência só aparecem quando o perfil permite ajudar a VPS.
    if "vps-assist" not in caps:
        allowed = [item for item in allowed if item not in {"vps_assist_probe", "hash_batch", "endpoint_probe", "log_digest", "zip_audit"}]
    if "maintenance-plan" not in caps and "cache-worker" not in caps:
        allowed = [item for item in allowed if item != "maintenance_plan"]
    if "service-control" not in caps:
        allowed = [item for item in allowed if item not in {"service_status", "service_start", "service_stop", "service_restart"}]
    if "boot-repair" not in caps:
        allowed = [item for item in allowed if item not in {"boot_status", "boot_repair"}]
    if "ffprobe" not in caps and "media-probe" not in caps:
        allowed = [item for item in allowed if item != "media_probe"]
    if "ffmpeg" not in caps and "audio-convert" not in caps and "tts-convert" not in caps:
        allowed = [item for item in allowed if item != "audio_convert"]
    return allowed


def _current_core_worker_profile() -> str:
    return _normalize_core_worker_profile(os.getenv("CORE_WORKER_PROFILE") or os.getenv("PHONE_WORKER_PROFILE") or "midia")


def _safe_env_key(value: Any) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ValueError(f"chave de env inválida: {key or '<vazia>'}")
    return key


def _format_env_value(value: Any) -> str:
    text = str(value if value is not None else "")
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,;-]*", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _update_env_file(path: str | None, updates: dict[str, Any]) -> Path:
    env_path = Path(path or os.getenv("PHONE_WORKER_ENV") or str(Path.home() / ".phone-worker.env")).expanduser()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    wanted = {_safe_env_key(k): str(v if v is not None else "") for k, v in updates.items()}
    existing = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    assign_re = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)=")
    for line in existing:
        match = assign_re.match(line.strip())
        if match and match.group(1) in wanted:
            key = match.group(1)
            output.append(f"{key}={_format_env_value(wanted[key])}")
            seen.add(key)
        else:
            output.append(line)
    missing = [key for key in wanted if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Core Worker pareado automaticamente. Não envie estes valores ao GitHub.")
        for key in missing:
            output.append(f"{key}={_format_env_value(wanted[key])}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    with contextlib.suppress(Exception):
        env_path.chmod(0o600)
    for key, value in wanted.items():
        os.environ[key] = value
    return env_path


def _android_prop(name: str) -> str:
    if not shutil.which("getprop"):
        return ""
    try:
        proc = subprocess.run(["getprop", name], capture_output=True, text=True, timeout=1.2)
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _default_worker_name() -> str:
    configured = str(os.getenv("CORE_WORKER_NAME") or os.getenv("PHONE_WORKER_NAME") or "").strip()
    if configured and configured.lower() not in {"localhost", "localhost.localdomain", "termux"}:
        return configured
    manufacturer = _android_prop("ro.product.manufacturer")
    model = _android_prop("ro.product.model")
    device = _android_prop("ro.product.device")
    parts = []
    if manufacturer and manufacturer.lower() not in str(model).lower():
        parts.append(manufacturer)
    if model:
        parts.append(model)
    elif device:
        parts.append(device)
    label = " ".join(part.strip() for part in parts if part.strip())
    if label:
        return label[:64]
    node = str(platform.node() or "").strip()
    if node and node.lower() not in {"localhost", "localhost.localdomain"}:
        return node[:64]
    return "Core Phone Worker"


def _default_worker_id() -> str:
    raw = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    if raw:
        return raw
    name = _default_worker_name().strip().lower()
    name = re.sub(r"[^a-z0-9_.:-]+", "-", name).strip("-._:") or "phone-worker"
    seed = f"{platform.node()}|{Path.home()}".encode("utf-8", errors="ignore")
    suffix = hashlib.sha256(seed).hexdigest()[:8]
    return f"phone-{name[:28]}-{suffix}"


def _short_text(value: Any, *, limit: int = 120, default: str = "") -> str:
    text = str(value or default).replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text



def _music_stream_ttl_seconds() -> float:
    return max(300.0, min(21600.0, _env_float("PHONE_WORKER_MUSIC_STREAM_TTL_SECONDS", 7200.0)))


def _cleanup_music_streams_unlocked(now: float | None = None) -> None:
    current = time.time() if now is None else float(now)
    expired = [key for key, item in _MUSIC_STREAMS.items() if float(item.get("expires_at") or 0.0) <= current]
    for key in expired:
        item = _MUSIC_STREAMS.pop(key, None)
        if isinstance(item, dict):
            _cleanup_music_prepared_file(item)


def _register_music_stream(item: dict[str, Any]) -> str:
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url:
        return ""
    seed = f"{time.time()}|{os.urandom(16).hex()}|{stream_url[:96]}".encode("utf-8", errors="ignore")
    stream_id = hashlib.sha256(seed).hexdigest()[:32]
    ttl = _music_stream_ttl_seconds()
    stored = dict(item)
    stored["id"] = stream_id
    stored["created_at"] = time.time()
    stored["expires_at"] = time.time() + ttl
    with _MUSIC_STREAM_LOCK:
        _cleanup_music_streams_unlocked()
        _MUSIC_STREAMS[stream_id] = stored
    return stream_id


def _music_stream_lookup(stream_id: str) -> dict[str, Any] | None:
    stream_id = str(stream_id or "").strip()
    if not stream_id:
        return None
    with _MUSIC_STREAM_LOCK:
        _cleanup_music_streams_unlocked()
        item = _MUSIC_STREAMS.get(stream_id)
        return dict(item) if isinstance(item, dict) else None


def _safe_ffmpeg_header_lines(headers: Any) -> str:
    if not isinstance(headers, dict):
        return ""
    allowed = {"user-agent", "accept", "accept-language", "referer", "origin", "cookie", "range"}
    lines: list[str] = []
    for key, value in headers.items():
        name = str(key or "").strip()
        if not name or name.lower() not in allowed:
            continue
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{name}: {text}\r\n")
    return "".join(lines)



def _music_pcm_cache_dir() -> Path:
    raw = str(os.getenv("PHONE_WORKER_MUSIC_PCM_CACHE_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else (Path.home() / "phone-worker" / "cache" / "music-pcm")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _music_prepared_mode_enabled() -> bool:
    raw = str(os.getenv("PHONE_WORKER_MUSIC_STREAM_MODE") or os.getenv("PHONE_WORKER_MUSIC_PREPARE_MODE") or "prepared").strip().lower()
    return raw not in {"live", "passthrough", "stream", "realtime", "0", "false", "off", "no", "não", "nao"}


def _music_prepare_timeout_seconds(item: dict[str, Any]) -> float:
    configured = _env_float("PHONE_WORKER_MUSIC_PREPARE_TIMEOUT_SECONDS", 0.0)
    if configured > 0:
        return max(20.0, min(1800.0, configured))
    duration = float(item.get("duration") or 0.0)
    if duration > 0:
        return max(45.0, min(1800.0, duration * 2.5 + 45.0))
    return 240.0


def _music_prepare_max_duration_seconds() -> float:
    return max(0.0, _env_float("PHONE_WORKER_MUSIC_PREPARE_MAX_DURATION_SECONDS", 1800.0))


def _music_pcm_cache_max_bytes() -> int:
    mb = max(64.0, min(16384.0, _env_float("PHONE_WORKER_MUSIC_PCM_CACHE_MAX_MB", 2048.0)))
    return int(mb * 1024 * 1024)


def _cleanup_music_prepared_file(item: dict[str, Any]) -> None:
    path = str(item.get("prepared_pcm_path") or "").strip()
    if not path:
        return
    with contextlib.suppress(Exception):
        p = Path(path)
        cache_dir = _music_pcm_cache_dir().resolve()
        resolved = p.resolve()
        if cache_dir in resolved.parents or resolved == cache_dir:
            p.unlink(missing_ok=True)


def _cleanup_music_pcm_cache() -> None:
    try:
        cache_dir = _music_pcm_cache_dir()
        files = [p for p in cache_dir.glob("*.pcm") if p.is_file()]
    except Exception:
        return
    now = time.time()
    max_age = _music_stream_ttl_seconds() + 600.0
    for p in files:
        with contextlib.suppress(Exception):
            if now - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
    try:
        files = [p for p in cache_dir.glob("*.pcm") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
    except Exception:
        return
    max_bytes = _music_pcm_cache_max_bytes()
    if total <= max_bytes:
        return
    for p in sorted(files, key=lambda item: item.stat().st_mtime):
        with contextlib.suppress(Exception):
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            total -= size
        if total <= max_bytes:
            break


def _music_stream_build_ffmpeg_input_cmd(item: dict[str, Any], *, output: str) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg não encontrado no worker")
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url.startswith(("http://", "https://")):
        raise ValueError("stream inválido")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_at_eof",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "403,404,408,429,5xx",
        "-reconnect_delay_max",
        "5",
        "-rw_timeout",
        "10000000",
    ]
    ff_headers = _safe_ffmpeg_header_lines(item.get("http_headers"))
    if ff_headers:
        cmd += ["-headers", ff_headers]
    cmd += [
        "-i",
        stream_url,
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        output,
    ]
    return cmd


def _prepare_music_pcm_file(stream_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Transcodifica a faixa inteira no worker antes de servir para a VPS.

    Isso evita streaming PCM estritamente em tempo real via Tailscale. O worker usa
    CPU/IO local para preparar o áudio mais rápido que tempo real; a VPS só lê um
    arquivo PCM estável com buffer alto.
    """
    prepared_path = str(item.get("prepared_pcm_path") or "").strip()
    if prepared_path:
        p = Path(prepared_path)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return item

    duration = float(item.get("duration") or 0.0)
    max_duration = _music_prepare_max_duration_seconds()
    if max_duration > 0 and duration > max_duration:
        raise TimeoutError(f"faixa muito longa para cache completo no worker ({duration:.0f}s > {max_duration:.0f}s)")

    _cleanup_music_pcm_cache()
    cache_dir = _music_pcm_cache_dir()
    out_path = cache_dir / f"{stream_id}.pcm"
    tmp_path = cache_dir / f"{stream_id}.tmp.pcm"
    with contextlib.suppress(Exception):
        tmp_path.unlink(missing_ok=True)

    timeout = _music_prepare_timeout_seconds(item)
    cmd = _music_stream_build_ffmpeg_input_cmd(item, output=str(tmp_path))
    started = time.time()
    print(
        f"[music-stream] cache_started id={stream_id} title={_short_text(item.get('title'), limit=80)!r} duration={duration:.1f}s timeout={timeout:.1f}s",
        flush=True,
    )
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(Path.home() / "phone-worker"),
        timeout=timeout,
    )
    elapsed = time.time() - started
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, (bytes, bytearray)) else str(proc.stderr or "")
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg cache falhou rc={proc.returncode}: {_short_text(stderr, limit=220)}")
    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg não gerou PCM no worker")
    tmp_path.replace(out_path)
    size = out_path.stat().st_size
    prepared_seconds = size / 192000.0
    updated = dict(item)
    updated["prepared_pcm_path"] = str(out_path)
    updated["prepared_pcm_bytes"] = size
    updated["prepared_pcm_seconds"] = prepared_seconds
    updated["prepared_at"] = time.time()
    updated["stream_mode"] = "prepared_pcm"
    with _MUSIC_STREAM_LOCK:
        current = _MUSIC_STREAMS.get(stream_id)
        if isinstance(current, dict):
            current.update(updated)
    print(
        f"[music-stream] cache_ready id={stream_id} bytes={size} seconds={prepared_seconds:.1f} elapsed={elapsed:.1f}s",
        flush=True,
    )
    _cleanup_music_pcm_cache()
    return updated


def _serve_prepared_music_pcm(handler: BaseHTTPRequestHandler, stream_id: str, item: dict[str, Any]) -> None:
    path = Path(str(item.get("prepared_pcm_path") or ""))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("PCM preparado não encontrado")
    size = path.stat().st_size
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Core-Worker-Stream-Id", stream_id)
    handler.send_header("X-Core-Worker-Stream-Mode", "prepared-pcm")
    handler.send_header("X-Core-Worker-Prepared-Seconds", f"{float(item.get('prepared_pcm_seconds') or 0.0):.3f}")
    handler.end_headers()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(PCM_FRAME_BYTES * 64)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
    with contextlib.suppress(Exception):
        handler.wfile.flush()

def _stream_music_pcm(handler: BaseHTTPRequestHandler, stream_id: str) -> None:
    item = _music_stream_lookup(stream_id)
    if not item:
        _error(handler, HTTPStatus.NOT_FOUND, "stream não encontrado ou expirado")
        return
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url.startswith(("http://", "https://")):
        _error(handler, HTTPStatus.BAD_REQUEST, "stream inválido")
        return

    if _music_prepared_mode_enabled():
        try:
            prepared = _prepare_music_pcm_file(stream_id, item)
            _serve_prepared_music_pcm(handler, stream_id, prepared)
            return
        except Exception as exc:
            fallback_live = str(os.getenv("PHONE_WORKER_MUSIC_PREPARE_LIVE_FALLBACK") or "false").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}
            print(f"[music-stream] cache_failed id={stream_id} erro={type(exc).__name__}: {_short_text(exc, limit=180)}", flush=True)
            if not fallback_live:
                _error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, f"preparo de áudio no worker falhou: {type(exc).__name__}")
                return

    # Fallback legado: PCM ao vivo. Mantido apenas para emergência; por padrão o
    # modo prepared acima é usado para evitar travadas por jitter/rede.
    proc: subprocess.Popen[bytes] | None = None
    try:
        cmd = _music_stream_build_ffmpeg_input_cmd(item, output="pipe:1")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(Path.home() / "phone-worker"))
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Core-Worker-Stream-Id", stream_id)
        handler.send_header("X-Core-Worker-Stream-Mode", "live-pcm")
        handler.end_headers()
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(PCM_FRAME_BYTES * 16)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
        with contextlib.suppress(Exception):
            handler.wfile.flush()
    except Exception as exc:
        try:
            if not getattr(handler, "_headers_buffer", None):
                _error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, f"stream falhou: {type(exc).__name__}")
        except Exception:
            pass
        print(f"[music-stream] falhou id={stream_id} erro={type(exc).__name__}: {_short_text(exc, limit=160)}", flush=True)
    finally:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)

def _format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while abs(size) >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def _run_json_command(command: list[str], *, timeout: float = 2.0) -> dict[str, Any]:
    if not command or not shutil.which(command[0]):
        return {}
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        parsed = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_text_file(path: str | Path, *, limit: int = 4096) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit].strip()
    except Exception:
        return ""


def _empty_battery_snapshot(source: str = "unavailable", error: object = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "source": _short_text(source, limit=48, default="unavailable"),
        "level": None,
        "charging": None,
        "temperature_c": None,
    }
    if error:
        result["error"] = _short_text(error, limit=120)
    return result


def _safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except (PermissionError, OSError):
        return False
    except Exception:
        return False


def _sysfs_battery_snapshot() -> dict[str, Any]:
    # Fallback leve quando Termux:API não está instalado ou sem permissão.
    # Em alguns Androids/Termux, apenas chamar Path.exists() em /sys pode gerar
    # PermissionError. Telemetria é sempre best-effort e nunca pode derrubar
    # heartbeat/jobs.
    base_candidates: list[Path] = []
    primary = Path("/sys/class/power_supply/battery")
    if _safe_path_exists(primary):
        base_candidates.append(primary)
    try:
        for candidate in Path("/sys/class/power_supply").glob("BAT*"):
            if _safe_path_exists(candidate) and candidate not in base_candidates:
                base_candidates.append(candidate)
    except (PermissionError, OSError):
        return _empty_battery_snapshot("sysfs_permission_denied")
    except Exception as exc:
        return _empty_battery_snapshot("sysfs_error", exc)

    for base in base_candidates:
        try:
            result: dict[str, Any] = {"available": True, "source": "sysfs"}
            capacity = _read_text_file(base / "capacity", limit=32)
            status = _read_text_file(base / "status", limit=64).lower()
            plugged = _read_text_file(base / "type", limit=64).lower()
            temp = _read_text_file(base / "temp", limit=32)
            try:
                if capacity:
                    result["level"] = max(0, min(100, int(float(capacity))))
            except Exception:
                pass
            if status:
                result["status"] = status[:32]
                result["charging"] = status in {"charging", "full"}
            if plugged:
                result["plugged"] = plugged[:32]
            try:
                if temp:
                    raw_temp = float(temp)
                    # Android costuma expor décimos de °C.
                    if raw_temp > 1000:
                        raw_temp = raw_temp / 10.0
                    result["temperature_c"] = round(raw_temp, 1)
            except Exception:
                pass
            if any(key in result for key in ("level", "status", "charging", "temperature_c")):
                return result
        except (PermissionError, OSError):
            continue
        except Exception:
            continue
    return _empty_battery_snapshot("sysfs_unavailable")


def _battery_snapshot() -> dict[str, Any]:
    try:
        raw = _run_json_command(["termux-battery-status"], timeout=2.0)
    except Exception as exc:
        raw = {}
        termux_error = exc
    else:
        termux_error = None

    if not raw:
        try:
            return _sysfs_battery_snapshot()
        except (PermissionError, OSError) as exc:
            return _empty_battery_snapshot("battery_permission_denied", exc)
        except Exception as exc:
            return _empty_battery_snapshot("battery_error", exc or termux_error)

    level = raw.get("percentage")
    if level is None:
        level = raw.get("level")
    charging = None
    status = str(raw.get("status") or "").strip().lower()
    plugged = str(raw.get("plugged") or "").strip().lower()
    if status:
        charging = status in {"charging", "full"}
    elif plugged:
        charging = plugged not in {"unplugged", "none", "unknown"}
    result: dict[str, Any] = {"available": True, "source": "termux-api"}
    try:
        if level is not None:
            clean_level = max(0, min(100, int(float(level))))
            result["level"] = clean_level
            result["percentage"] = clean_level
            result["percent"] = clean_level
    except Exception:
        pass
    if charging is not None:
        result["charging"] = bool(charging)
    if status:
        result["status"] = status[:32]
    if plugged:
        result["plugged"] = plugged[:32]
    try:
        temp = raw.get("temperature")
        if temp is not None:
            result["temperature_c"] = round(float(temp), 1)
    except Exception:
        pass
    return result


def _safe_telemetry(name: str, callback, default: Any) -> Any:
    try:
        return callback()
    except Exception as exc:
        print(f"[phone-worker] telemetria {name} indisponível: {type(exc).__name__}: {_short_text(exc, limit=100)}", flush=True)
        if isinstance(default, dict):
            fallback = dict(default)
            fallback.setdefault("ok", False)
            fallback.setdefault("error", f"{type(exc).__name__}: {_short_text(exc, limit=100)}")
            return fallback
        return default

def _run_text_command(command: list[str], *, timeout: float = 3.0, max_bytes: int = 32768) -> tuple[int, str, str]:
    if not command or not shutil.which(command[0]):
        return 127, "", f"{command[0] if command else 'comando'} não encontrado"
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.5, timeout),
        )
        stdout = (proc.stdout or b"")[:max_bytes].decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"")[:max_bytes].decode("utf-8", errors="replace")
        return int(proc.returncode), stdout.strip(), stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {_short_text(exc, limit=120)}"


def _mask_ipv4(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
        parts = text.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    return _short_text(text, limit=64)


def _base_url_host() -> str:
    base_url, _token, _worker_id = _core_worker_auth_parts()
    if not base_url:
        return ""
    try:
        return urllib.parse.urlparse(base_url).hostname or ""
    except Exception:
        return ""


def _looks_like_tailscale_host(host: str) -> bool:
    text = str(host or "").strip()
    if re.fullmatch(r"100\.(?:\d{1,3}\.){2}\d{1,3}", text):
        return True
    # MagicDNS/headscale costumam usar nomes internos. Não marca como conectado,
    # só ajuda a explicar que a rota parece privada.
    return text.endswith(".ts.net") or text.endswith(".tailnet")


def _tailscale_snapshot(*, probe_vps: bool = False) -> dict[str, Any]:
    base_url, _token, _worker_id = _core_worker_auth_parts()
    base_host = _base_url_host()
    base_looks_tailscale = _looks_like_tailscale_host(base_host)
    result: dict[str, Any] = {
        "cli_available": bool(shutil.which("tailscale")),
        "connected": False,
        "state": "unknown",
        "via_vps_url": bool(base_looks_tailscale),
    }
    if base_host:
        result["vps_host_masked"] = _mask_ipv4(base_host)
    ip = ""
    if result["cli_available"]:
        code, stdout, stderr = _run_text_command(["tailscale", "ip", "-4"], timeout=2.5, max_bytes=4096)
        if code == 0 and stdout.strip():
            ip = stdout.strip().splitlines()[0].strip()
            result["connected"] = True
            result["ip_present"] = True
            result["ip_masked"] = _mask_ipv4(ip)
        elif stderr:
            result["ip_error"] = _short_text(stderr, limit=120)

        code, stdout, stderr = _run_text_command(["tailscale", "status", "--json"], timeout=3.5, max_bytes=65536)
        if code == 0 and stdout:
            try:
                parsed = json.loads(stdout)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                state = str(parsed.get("BackendState") or parsed.get("backendState") or "").strip()
                if state:
                    result["state"] = state[:48]
                    result["connected"] = result["connected"] or state.lower() == "running"
                self_info = parsed.get("Self") if isinstance(parsed.get("Self"), dict) else {}
                if self_info:
                    result["hostname"] = _short_text(self_info.get("HostName"), limit=64)
                    result["online"] = bool(self_info.get("Online", result.get("connected")))
                peers = parsed.get("Peer") if isinstance(parsed.get("Peer"), dict) else {}
                result["peers"] = len(peers) if isinstance(peers, dict) else 0
        elif code != 127 and stderr:
            result["status_error"] = _short_text(stderr, limit=160)
    else:
        # No Android é comum usar o app oficial do Tailscale como VPN, sem CLI no Termux.
        # Se a VPS configurada é 100.x.x.x ou MagicDNS, o heartbeat bem-sucedido já prova
        # que o Termux alcança a VPS por uma rota privada/VPN; não mostrar como "off".
        if base_looks_tailscale:
            result["connected"] = True
            result["state"] = "app/vpn"
            result["note"] = "CLI tailscale ausente; conexão inferida pelo endpoint privado da VPS"
        else:
            result["state"] = "no-cli"
            result["note"] = "CLI tailscale não encontrada no Termux; use o app oficial para a VPN"

    if probe_vps and base_url:
        health_url = base_url.rstrip("/") + "/health"
        started = time.time()
        try:
            req = urllib.request.Request(health_url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                raw = resp.read(4096)
                result["vps_reachable"] = True
                result["vps_status"] = int(getattr(resp, "status", 200) or 200)
                result["vps_latency_ms"] = round((time.time() - started) * 1000, 1)
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                    if isinstance(data, dict):
                        result["vps_health_ok"] = bool(data.get("ok", True))
                except Exception:
                    pass
        except Exception as exc:
            result["vps_reachable"] = False
            result["vps_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
    return result



def _vps_tcp_ping_snapshot(*, timeout: float = 2.5, cache_ttl: float = 6.0) -> dict[str, Any]:
    """Mede RTT TCP do worker até a VPS/orquestrador.

    Não usa ICMP/root. Apenas abre uma conexão TCP curta para a URL já
    configurada em CORE_WORKER_VPS_URL. Resultado é cacheado por poucos
    segundos porque o payload também é usado no polling de jobs.
    """
    base_url, _token, _worker_id = _core_worker_auth_parts()
    if not base_url:
        return {"available": False, "reachable": False, "source": "not_configured"}
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as exc:
        return {"available": False, "reachable": False, "source": "invalid_url", "error": _short_text(exc, limit=100)}
    if not host:
        return {"available": False, "reachable": False, "source": "missing_host"}

    cache_key = f"{host}:{port}"
    now = time.monotonic()
    cached = _PING_CACHE.get(cache_key)
    if isinstance(cached, dict) and now - float(cached.get("monotonic_at") or 0.0) <= max(0.5, cache_ttl):
        result = dict(cached.get("result") or {})
        result["cached"] = True
        return result

    started = time.perf_counter()
    result: dict[str, Any] = {
        "available": True,
        "source": "tcp_connect",
        "host_masked": _mask_ipv4(host),
        "port": port,
    }
    try:
        with socket.create_connection((host, port), timeout=max(0.3, timeout)):
            pass
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        result.update({
            "reachable": True,
            "ping_ms": latency_ms,
            "latency_ms": latency_ms,
            "vps_ping_ms": latency_ms,
        })
    except Exception as exc:
        result.update({
            "reachable": False,
            "error": f"{type(exc).__name__}: {_short_text(exc, limit=100)}",
        })
    _PING_CACHE[cache_key] = {"monotonic_at": now, "result": dict(result)}
    return result

def _network_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    wifi = _run_json_command(["termux-wifi-connectioninfo"], timeout=2.0)
    if wifi:
        result["type"] = "wifi"
        result["source"] = "termux-api"
        ssid = str(wifi.get("ssid") or "").strip()
        if ssid and ssid != "<unknown ssid>":
            result["name"] = _short_text(ssid, limit=48)
        try:
            result["rssi"] = int(wifi.get("rssi"))
        except Exception:
            pass
    else:
        # Sem Termux:API, ainda conseguimos dizer que há conectividade se o worker
        # está alcançando a VPS por heartbeat/poll.
        result["type"] = "connected" if _heartbeat_configured() else "unknown"
        result["source"] = "inferred"
    tailscale = _tailscale_snapshot(probe_vps=False)
    result["tailscale"] = bool(tailscale.get("connected"))
    result["tailscale_cli"] = bool(tailscale.get("cli_available"))
    result["tailscale_state"] = _short_text(tailscale.get("state"), limit=48, default="unknown")
    result["tailscale_via_vps_url"] = bool(tailscale.get("via_vps_url"))
    if tailscale.get("ip_masked"):
        result["tailscale_ip_masked"] = tailscale.get("ip_masked")
    elif tailscale.get("vps_host_masked") and tailscale.get("via_vps_url"):
        result["tailscale_ip_masked"] = tailscale.get("vps_host_masked")
    if tailscale.get("note"):
        result["tailscale_note"] = _short_text(tailscale.get("note"), limit=100)
    ping = _safe_telemetry("vps ping", _vps_tcp_ping_snapshot, {"available": False, "reachable": False, "source": "telemetry_failed"})
    if isinstance(ping, dict):
        result["vps_reachable"] = bool(ping.get("reachable"))
        result["vps_ping_available"] = bool(ping.get("available", True))
        if ping.get("ping_ms") is not None:
            result["vps_ping_ms"] = ping.get("ping_ms")
            result["ping_ms"] = ping.get("ping_ms")
        elif ping.get("latency_ms") is not None:
            result["vps_ping_ms"] = ping.get("latency_ms")
            result["ping_ms"] = ping.get("latency_ms")
        if ping.get("host_masked"):
            result["vps_host_masked"] = ping.get("host_masked")
        if ping.get("port"):
            result["vps_port"] = ping.get("port")
        if ping.get("error"):
            result["vps_ping_error"] = _short_text(ping.get("error"), limit=120)
    return result


def _heartbeat_configured() -> bool:
    if not _env_bool("CORE_WORKER_HEARTBEAT_ENABLED", True):
        return False
    return bool(
        str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip()
        and str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
        and str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    )


def _core_worker_jobs_configured() -> bool:
    if not _env_bool("CORE_WORKER_JOBS_ENABLED", True):
        return False
    return _heartbeat_configured()


def _core_worker_auth_parts() -> tuple[str, str, str]:
    base_url = str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip().rstrip("/")
    token = str(os.getenv("CORE_WORKER_TOKEN") or "").strip()
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    return base_url, token, worker_id


def _classify_core_worker_network_error(exc: BaseException | str) -> str:
    text = str(exc or "").lower()
    if "no route to host" in text or "errno 113" in text:
        return "no_route_to_vps"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "network is unreachable" in text or "errno 101" in text:
        return "network_unreachable"
    if "name or service not known" in text or "temporary failure in name resolution" in text:
        return "dns_failed"
    if "connection refused" in text:
        return "connection_refused"
    return "request_failed"


def _remember_core_worker_network_ok() -> None:
    _CORE_WORKER_NETWORK_STATE.update({
        "last_ok_at": time.time(),
        "last_error": "",
        "last_error_kind": "",
    })


def _remember_core_worker_network_error(exc: BaseException | str) -> None:
    _CORE_WORKER_NETWORK_STATE.update({
        "last_error_at": time.time(),
        "last_error": _short_text(exc, limit=160),
        "last_error_kind": _classify_core_worker_network_error(exc),
    })


def _core_worker_network_runtime_snapshot() -> dict[str, Any]:
    now = time.time()
    last_ok = float(_CORE_WORKER_NETWORK_STATE.get("last_ok_at") or 0.0)
    last_error = float(_CORE_WORKER_NETWORK_STATE.get("last_error_at") or 0.0)
    return {
        "last_ok_age_seconds": round(now - last_ok, 3) if last_ok else None,
        "last_error_age_seconds": round(now - last_error, 3) if last_error else None,
        "last_error_kind": _CORE_WORKER_NETWORK_STATE.get("last_error_kind") or "",
        "last_error": _CORE_WORKER_NETWORK_STATE.get("last_error") or "",
    }


def _post_json_url(url: str, payload: dict[str, Any], *, token: str = "", timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    response_limit = max(1024 * 1024, min(8 * 1024 * 1024, _env_int("CORE_WORKER_JSON_RESPONSE_MAX_BYTES", 4 * 1024 * 1024)))
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(response_limit + 1)
            status = int(getattr(resp, "status", 200) or 200)
            _remember_core_worker_network_ok()
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        status = int(exc.code)
        _remember_core_worker_network_ok()
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        raise
    parsed: dict[str, Any]
    if len(raw) > response_limit:
        return status, {"ok": False, "error": "resposta JSON da VPS grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        parsed = data if isinstance(data, dict) else {"ok": False, "error": "resposta não é objeto"}
    except Exception as exc:
        parsed = {"ok": False, "error": f"JSON inválido da VPS: {type(exc).__name__}"}
    return status, parsed


def _get_json_url(url: str, *, timeout: float = 8.0, max_bytes: int = 1024 * 1024) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(max_bytes + 1)
            status = int(getattr(resp, "status", 200) or 200)
            _remember_core_worker_network_ok()
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        status = int(exc.code)
        _remember_core_worker_network_ok()
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        raise
    if len(raw) > max_bytes:
        return status, {"ok": False, "error": "resposta JSON grande demais"}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        parsed = data if isinstance(data, dict) else {"ok": False, "error": "resposta não é objeto"}
    except Exception as exc:
        parsed = {"ok": False, "error": f"JSON inválido: {type(exc).__name__}"}
    return status, parsed


def _download_url_to_file(
    url: str,
    target: Path,
    *,
    timeout: float = 35.0,
    max_bytes: int = 150 * 1024 * 1024,
    accept: str = "application/vnd.android.package-archive,*/*",
    size_label: str = "APK",
) -> dict[str, Any]:
    headers = {
        "Accept": accept,
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    total = 0
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp, tmp.open("wb") as fh:
            status = int(getattr(resp, "status", 200) or 200)
            _remember_core_worker_network_ok()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{size_label} grande demais para este worker")
                digest.update(chunk)
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        _remember_core_worker_network_ok()
        body = exc.read(8 * 1024).decode("utf-8", errors="replace")
        with contextlib.suppress(Exception):
            tmp.unlink()
        return {"ok": False, "status": int(exc.code), "error": _short_text(body or exc, limit=180)}
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        with contextlib.suppress(Exception):
            tmp.unlink()
        raise
    tmp.replace(target)
    return {"ok": True, "status": status, "path": str(target), "bytes": total, "sha256": digest.hexdigest()}


def _post_core_worker_json(path: str, payload: dict[str, Any], *, timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    base_url, token, _worker_id = _core_worker_auth_parts()
    if not base_url or not token:
        return 0, {"ok": False, "error": "Core Worker não configurado"}
    return _post_json_url(f"{base_url}{path}", payload, token=token, timeout=timeout)




def _core_job_runtime_snapshot() -> dict[str, Any]:
    with _CORE_JOB_LOCK:
        active = dict(_CORE_JOB_ACTIVE)
        last = dict(_CORE_JOB_LAST_RESULT)
        pending = len(_PENDING_CORE_JOB_RESULTS)
    return {
        "configured": _core_worker_jobs_configured(),
        "active": bool(active),
        "active_job_id": active.get("job_id") or "",
        "active_type": active.get("type") or "",
        "active_since": active.get("started_at") or 0,
        "last_result_job_id": last.get("job_id") or "",
        "last_result_type": last.get("type") or "",
        "last_result_status": last.get("status") or "",
        "last_result_summary": last.get("summary") or "",
        "last_result_at": last.get("finished_at") or 0,
        "last_result_sent": bool(last.get("sent_ok")),
        "pending_results": pending,
    }


def _set_core_job_active(job: dict[str, Any]) -> None:
    with _CORE_JOB_LOCK:
        _CORE_JOB_ACTIVE.clear()
        _CORE_JOB_ACTIVE.update({
            "job_id": str(job.get("job_id") or ""),
            "type": str(job.get("type") or ""),
            "started_at": time.time(),
        })


def _finish_core_job(job_id: str, kind: str, status: str, *, summary: str = "", sent_ok: bool = False) -> None:
    with _CORE_JOB_LOCK:
        _CORE_JOB_ACTIVE.clear()
        _CORE_JOB_LAST_RESULT.clear()
        _CORE_JOB_LAST_RESULT.update({
            "job_id": str(job_id or ""),
            "type": str(kind or ""),
            "status": str(status or ""),
            "summary": _short_text(summary or status, limit=160),
            "finished_at": time.time(),
            "sent_ok": bool(sent_ok),
        })


def _store_pending_core_job_result(payload: dict[str, Any]) -> None:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        return
    safe_payload = dict(payload)
    safe_payload.setdefault("stored_at", time.time())
    with _CORE_JOB_LOCK:
        _PENDING_CORE_JOB_RESULTS[job_id] = safe_payload
    _persist_pending_core_job_results()


def _post_core_worker_job_result_payload_status(payload: dict[str, Any], *, timeout: float = 8.0) -> tuple[bool, int, dict[str, Any]]:
    code, data = _post_core_worker_json("/core-worker/jobs/result", payload, timeout=timeout)
    ok = bool(200 <= code < 300 and data.get("ok", True))
    if ok:
        return True, int(code), data
    print(f"[core-worker-jobs] falha ao enviar resultado HTTP {code}: {_short_text(data.get('error') or data, limit=180)}", flush=True)
    return False, int(code), data


def _post_core_worker_job_result_payload(payload: dict[str, Any], *, timeout: float = 8.0) -> bool:
    ok, _code, _data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
    return ok


def _flush_pending_core_worker_job_results(*, timeout: float = 8.0) -> int:
    _load_persisted_pending_core_job_results()
    with _CORE_JOB_LOCK:
        pending = list(_PENDING_CORE_JOB_RESULTS.items())[:5]
    sent = 0
    changed = False
    for job_id, payload in pending:
        ok, code, data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
        if ok:
            with _CORE_JOB_LOCK:
                _PENDING_CORE_JOB_RESULTS.pop(job_id, None)
                if _CORE_JOB_LAST_RESULT.get("job_id") == job_id:
                    _CORE_JOB_LAST_RESULT["sent_ok"] = True
            changed = True
            sent += 1
            continue
        if _job_result_rejection_is_permanent(code, data):
            _archive_pending_core_job_result(job_id, payload, reason=f"VPS recusou resultado antigo HTTP {code}", response=data)
            with _CORE_JOB_LOCK:
                _PENDING_CORE_JOB_RESULTS.pop(job_id, None)
            changed = True
            print(f"[core-worker-jobs] resultado pendente antigo arquivado e removido: {job_id}", flush=True)
    if changed:
        _persist_pending_core_job_results()
    return sent

def _core_worker_payload(*, host: str, port: int) -> dict[str, Any]:
    status = _safe_telemetry("system", _system_status, {"ok": False})
    music_node = _safe_telemetry("music_node", _music_node_snapshot, {"ok": False, "online": False, "state": "unknown"})
    music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
    worker_id = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or "").strip()
    name = _default_worker_name()
    endpoint = str(os.getenv("CORE_WORKER_ENDPOINT") or os.getenv("PHONE_WORKER_ENDPOINT") or "").strip()
    if not endpoint and host not in {"", "0.0.0.0", "::"}:
        endpoint = f"http://{host}:{port}"
    profile = _current_core_worker_profile()
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    safe_mode = _phone_worker_safe_mode_enabled()
    if safe_mode:
        blocked_prefixes = ("music",)
        roles = [item for item in roles if not str(item).lower().startswith(blocked_prefixes)]
        capabilities = [item for item in capabilities if not str(item).lower().startswith(blocked_prefixes)]
    if status.get("ffmpeg") and "ffmpeg" not in capabilities:
        capabilities.append("ffmpeg")
    if status.get("ffprobe") and "ffprobe" not in capabilities:
        capabilities.append("ffprobe")
    music_ready = (not safe_mode) and bool(music_agent.get("available") or music_node.get("ok") or music_node.get("online") or profile == "turbo")
    if music_ready:
        for role in ("music", "music-agent", "music-node", "music-lavalink", "music-ytdlp"):
            if role not in roles:
                roles.append(role)
        for capability in ("music", "music-agent", "music-voice", "music-node", "music-lavalink", "music-ytdlp", "music-ytdlp-resolve"):
            if capability not in capabilities:
                capabilities.append(capability)
    return {
        "worker_id": worker_id,
        "physical_worker_id": worker_id,
        "name": _short_text(name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["inline-b64-v1", "zip-v1"],
        "profile": profile,
        "profile_label": _core_worker_profile_label(profile),
        "safe_mode": safe_mode,
        "endpoint": endpoint,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "supported_tasks": _supported_core_worker_job_types(),
        "battery": _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot()),
        "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}),
        "health": {
            "ok": True,
            "pid": status.get("pid"),
            "uptime_seconds": status.get("uptime_seconds"),
            "jobs_started": status.get("jobs_started"),
            "jobs_failed": status.get("jobs_failed"),
            "ffmpeg": status.get("ffmpeg"),
            "ffprobe": status.get("ffprobe"),
            "scripts_ok": ((status.get("scripts") or {}).get("complete") if isinstance(status.get("scripts"), dict) else None),
            "boot_ok": ((status.get("boot") or {}).get("ok") if isinstance(status.get("boot"), dict) else None),
            "supervisor_ok": ((status.get("supervisor") or {}).get("supervisor_ok") if isinstance(status.get("supervisor"), dict) else None),
            "sshd_ok": ((status.get("sshd") or {}).get("ok") if isinstance(status.get("sshd"), dict) else None),
            "runtime_mode": CORE_WORKER_RUNTIME_MODE,
            "internal_runtime_state": CORE_WORKER_INTERNAL_RUNTIME_STATE,
        },
        "status": {
            "worker_update": {"transports": ["inline-b64-v1", "zip-v1"]},
            "core_worker_jobs": _core_job_runtime_snapshot(),
            "core_worker_network": _core_worker_network_runtime_snapshot(),
            "music_node": music_node,
            "music_agent": music_agent,
            "runtime_mode": CORE_WORKER_RUNTIME_MODE,
            "runtime": {
                "mode": CORE_WORKER_RUNTIME_MODE,
                "current_worker": "termux-phone-worker",
                "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
                "migration_stage": "termux-bootstrap-builder",
                "summary": "Termux executa jobs reais; APK prepara runtime interno gradualmente.",
            },
            "profile": profile,
            "profile_label": _core_worker_profile_label(profile),
            "http_host": host,
            "http_port": port,
            "python": status.get("python"),
            "platform": status.get("platform"),
            "disk_home": status.get("disk_home"),
            "loadavg": status.get("loadavg"),
            "scripts": status.get("scripts"),
            "boot": status.get("boot"),
            "shell_autostart": status.get("shell_autostart"),
            "auto_boot_repair": status.get("auto_boot_repair"),
            "supervisor": status.get("supervisor"),
            "sshd": status.get("sshd"),
        },
    }


def _pair_core_worker(
    *,
    code: str,
    vps_url: str,
    host: str,
    port: int,
    worker_id: str = "",
    name: str = "",
    roles: str = "",
    capabilities: str = "",
    env_file: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    normalized_code = str(code or "").strip().upper()
    base_url = str(vps_url or "").strip().rstrip("/")
    if not normalized_code:
        message = "informe o código CORE-XXXX"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "error": message}
    if not base_url:
        message = "informe a URL da VPS/Tailscale com --vps-url"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "error": message}

    selected_worker_id = str(worker_id or _default_worker_id()).strip()
    selected_name = str(name or _default_worker_name()).strip()
    payload = _core_worker_payload(host=host, port=port)
    payload.update({
        "code": normalized_code,
        "worker_id": selected_worker_id,
        "name": _short_text(selected_name, limit=64, default="Core Phone Worker"),
        "source": "termux-phone-worker",
    })
    requested_roles = _env_list("CORE_WORKER_ROLES", []) if not roles else _env_list("CORE_WORKER_ROLES", [])
    if roles:
        os.environ["CORE_WORKER_ROLES"] = roles
        requested_roles = _env_list("CORE_WORKER_ROLES", [])
    requested_capabilities = _env_list("CORE_WORKER_CAPABILITIES", []) if not capabilities else _env_list("CORE_WORKER_CAPABILITIES", [])
    if capabilities:
        os.environ["CORE_WORKER_CAPABILITIES"] = capabilities
        requested_capabilities = _env_list("CORE_WORKER_CAPABILITIES", [])
    if requested_roles:
        payload["roles"] = requested_roles[:16]
    if requested_capabilities:
        payload["capabilities"] = requested_capabilities[:24]

    status, data = _post_json_url(f"{base_url}/core-worker/pair", payload, timeout=timeout)
    if not (200 <= status < 300) or not data.get("ok", False):
        message = _short_text(data.get("error") or data, limit=180)
        print(f"[core-worker-pair] HTTP {status}: {message}", flush=True)
        return {"ok": False, "status": status, "error": message}

    token = str(data.get("token") or "").strip()
    returned_worker_id = str(data.get("worker_id") or selected_worker_id).strip()
    if not token or not returned_worker_id:
        message = "resposta sem worker_id/token"
        print(f"[core-worker-pair] {message}", flush=True)
        return {"ok": False, "status": status, "error": message}

    env_path = _update_env_file(env_file, {
        "CORE_WORKER_HEARTBEAT_ENABLED": "true",
        "CORE_WORKER_JOBS_ENABLED": "true",
        "CORE_WORKER_VPS_URL": base_url,
        "CORE_WORKER_ID": returned_worker_id,
        "CORE_WORKER_TOKEN": token,
        "CORE_WORKER_NAME": payload.get("name") or selected_name,
        "CORE_WORKER_ROLES": ",".join(payload.get("roles") or _env_list("CORE_WORKER_ROLES", [])),
        "CORE_WORKER_CAPABILITIES": ",".join(payload.get("capabilities") or _env_list("CORE_WORKER_CAPABILITIES", [])),
    })
    print(f"[core-worker-pair] pareado como {returned_worker_id}; token salvo em {env_path}", flush=True)
    print("[core-worker-pair] heartbeat/jobs já podem usar o env atualizado; reiniciar ainda é recomendado se o supervisor estiver antigo.", flush=True)
    return {
        "ok": True,
        "status": status,
        "worker_id": returned_worker_id,
        "name": payload.get("name") or selected_name,
        "vps_url": base_url,
        "env_updated": True,
        "env_file": str(env_path),
        "message": "worker local pareado com a VPS",
    }

def _send_core_worker_heartbeat_once(*, host: str, port: int, timeout: float = 6.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not _base_url or not _token or not worker_id:
        return False
    started = time.perf_counter()
    try:
        payload = _core_worker_payload(host=host, port=port)
        status, data = _post_core_worker_json("/core-worker/heartbeat", payload, timeout=timeout)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if 200 <= status < 300 and data.get("ok", True):
            with contextlib.suppress(Exception):
                _flush_pending_core_worker_job_results(timeout=min(5.0, max(1.0, timeout)))
            return True
        print(f"[core-worker-heartbeat] HTTP {status} em {elapsed_ms}ms: {_short_text(data.get('error') or data, limit=180)}", flush=True)
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        print(f"[core-worker-heartbeat] falhou endpoint=/core-worker/heartbeat elapsed_ms={elapsed_ms}: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)
    return False


def _start_core_worker_heartbeat(*, host: str, port: int) -> None:
    if not _heartbeat_configured():
        print("[core-worker-heartbeat] desativado ou incompleto; defina CORE_WORKER_VPS_URL, CORE_WORKER_ID e CORE_WORKER_TOKEN", flush=True)
        return
    interval = max(10.0, min(300.0, _env_float("CORE_WORKER_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)))

    def loop() -> None:
        while True:
            _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(6.0, min(20.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 12.0))))
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="core-worker-heartbeat", daemon=True)
    thread.start()
    print(f"[core-worker-heartbeat] ativo; intervalo={int(interval)}s", flush=True)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _header_ascii(value: Any, *, limit: int = 180) -> str:
    text = _short_text(value, limit=limit, default="")
    return text.encode("ascii", errors="ignore").decode("ascii", errors="ignore")


def _audio_response(handler: BaseHTTPRequestHandler, status: int, data: bytes, meta: dict[str, Any]) -> None:
    fmt = str(meta.get("audio_format") or meta.get("format") or "mp3").strip().lower().replace(".", "")
    content_type = "audio/mpeg"
    if fmt in {"ogg", "opus", "ogg_opus", "oggopus"}:
        fmt = "ogg"
        content_type = "audio/ogg"
    elif fmt in {"wav", "wave"}:
        fmt = "wav"
        content_type = "audio/wav"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Core-Worker-Audio-Format", fmt)
    handler.send_header("X-Core-Worker-Engine", _header_ascii(meta.get("engine")))
    handler.send_header("X-Core-Worker-Selected-Engine", _header_ascii(meta.get("selected_engine") or meta.get("engine")))
    handler.send_header("X-Core-Worker-Cache-Hit", "true" if meta.get("cache_hit") else "false")
    handler.send_header("X-Core-Worker-Sha256", _header_ascii(meta.get("sha256"), limit=80))
    handler.send_header("X-Core-Worker-Id", _header_ascii(meta.get("worker_id"), limit=100))
    handler.send_header("X-Core-Worker-Version", _header_ascii(meta.get("worker_version"), limit=40))
    for key, header in (
        ("worker_total_ms", "X-Core-Worker-Worker-Total-Ms"),
        ("worker_synth_ms", "X-Core-Worker-Worker-Synth-Ms"),
        ("cache_read_ms", "X-Core-Worker-Cache-Read-Ms"),
        ("android_synth_ms", "X-Core-Worker-Android-Synth-Ms"),
    ):
        value = meta.get(key)
        if value not in (None, ""):
            handler.send_header(header, _header_ascii(value, limit=40))
    timing = meta.get("timing_ms") if isinstance(meta.get("timing_ms"), dict) else {}
    if timing:
        if timing.get("worker_total") not in (None, ""):
            handler.send_header("X-Core-Worker-Worker-Total-Ms", _header_ascii(timing.get("worker_total"), limit=40))
        if timing.get("worker_synth") not in (None, ""):
            handler.send_header("X-Core-Worker-Worker-Synth-Ms", _header_ascii(timing.get("worker_synth"), limit=40))
        if timing.get("cache_read") not in (None, ""):
            handler.send_header("X-Core-Worker-Cache-Read-Ms", _header_ascii(timing.get("cache_read"), limit=40))
        if timing.get("android_synth") not in (None, ""):
            handler.send_header("X-Core-Worker-Android-Synth-Ms", _header_ascii(timing.get("android_synth"), limit=40))
        if timing.get("android_roundtrip") not in (None, ""):
            handler.send_header("X-Core-Worker-Android-Roundtrip-Ms", _header_ascii(timing.get("android_roundtrip"), limit=40))
    handler.end_headers()
    handler.wfile.write(data)


def _error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _json_response(handler, status, {"ok": False, "error": message})


def _b64decode(value: str, *, max_bytes: int) -> bytes:
    if not value:
        return b""
    # Base64 inflates data by ~33%; fail early on clearly huge payloads.
    if len(value) > int(max_bytes * 1.45) + 64:
        raise ValueError("payload base64 grande demais")
    data = base64.b64decode(value.encode("ascii"), validate=True)
    if len(data) > max_bytes:
        raise ValueError("payload grande demais")
    return data


def _b64encode(data: bytes, *, max_bytes: int) -> str:
    if len(data) > max_bytes:
        raise ValueError("resultado grande demais")
    return base64.b64encode(data).decode("ascii")


def _with_tts_audio_payload(payload: dict[str, Any], data: bytes, *, max_bytes: int, raw_response: bool) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise ValueError("resultado grande demais")
    if raw_response:
        payload["_raw_audio"] = data
    else:
        payload["data_b64"] = _b64encode(data, max_bytes=max_bytes)
    return payload








def _android_tts_enabled() -> bool:
    return _env_bool("PHONE_WORKER_ANDROID_TTS_ENABLED", True)


def _android_tts_base_url() -> str:
    value = str(os.getenv("PHONE_WORKER_ANDROID_TTS_URL") or "http://127.0.0.1:8877").strip()
    return value.rstrip("/") or "http://127.0.0.1:8877"


def _android_tts_timeout_seconds(default: float = 0.45) -> float:
    return max(0.08, min(5.0, _env_float("PHONE_WORKER_ANDROID_TTS_STATUS_TIMEOUT_SECONDS", default)))


def _android_tts_json_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 1.5) -> dict[str, Any]:
    url = _android_tts_base_url() + path
    data: bytes | None = None
    method = "GET"
    headers = {"Accept": "application/json", "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}"}
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
        raw = response.read(16 * 1024 * 1024)
    parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Android TTS retornou JSON inválido")
    return parsed


def _android_tts_raw_request(path: str, *, payload: dict[str, Any], timeout: float = 1.5, max_audio_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, dict[str, Any]]:
    url = _android_tts_base_url() + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "audio/wav,application/octet-stream,application/json;q=0.4,*/*;q=0.1",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
    }
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        raw = response.read(max(1024, max_audio_bytes) + 1)
        if "application/json" in content_type:
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception as exc:
                raise RuntimeError(f"Android TTS raw retornou JSON inválido: {_short_text(exc, limit=100)}") from exc
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                raise RuntimeError(str(parsed.get("error") or "Android TTS raw retornou ok=false"))
            raise RuntimeError("Android TTS raw retornou JSON sem áudio")
        if len(raw) > max_audio_bytes:
            raise RuntimeError(f"Android TTS raw grande demais: {len(raw)} bytes")
        meta = {
            "content_type": content_type,
            "audio_format": str(response.headers.get("X-Core-Worker-Audio-Format") or "wav").strip().lower() or "wav",
            "android_synth_ms": str(response.headers.get("X-Core-Worker-Android-Synth-Ms") or "").strip(),
            "locale": str(response.headers.get("X-Core-Worker-Locale") or "").strip(),
            "voice": str(response.headers.get("X-Core-Worker-Voice") or "").strip(),
            "sha256": str(response.headers.get("X-Core-Worker-Sha256") or "").strip(),
        }
    if not raw:
        raise RuntimeError("Android TTS raw não retornou áudio")
    return raw, meta


def _android_tts_status(*, use_cache: bool = True) -> dict[str, Any]:
    if not _android_tts_enabled():
        return {"ok": False, "available": False, "ready": False, "enabled": False, "engine": "android_native", "last_error": "PHONE_WORKER_ANDROID_TTS_ENABLED=false"}
    now = time.monotonic()
    max_age = max(0.2, min(60.0, _env_float("PHONE_WORKER_ANDROID_TTS_STATUS_CACHE_SECONDS", 5.0)))
    cached = _ANDROID_TTS_STATUS_CACHE.get("data") if isinstance(_ANDROID_TTS_STATUS_CACHE, dict) else {}
    if use_cache and isinstance(cached, dict) and cached and now - float(_ANDROID_TTS_STATUS_CACHE.get("at") or 0.0) <= max_age:
        return dict(cached)
    try:
        timeout = _android_tts_timeout_seconds()
        data = _android_tts_json_request("/native-tts/status", timeout=timeout)
        ready = bool(data.get("ok") or data.get("ready") or data.get("available"))
        result = dict(data)
        result.update({
            "ok": ready,
            "available": ready,
            "ready": ready,
            "enabled": True,
            "engine": "android_native",
            "url": _android_tts_base_url(),
        })
    except Exception as exc:
        result = {
            "ok": False,
            "available": False,
            "ready": False,
            "enabled": True,
            "engine": "android_native",
            "url": _android_tts_base_url(),
            "last_error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}",
        }
    _ANDROID_TTS_STATUS_CACHE["at"] = now
    _ANDROID_TTS_STATUS_CACHE["data"] = dict(result)
    return result


def _android_tts_ready() -> bool:
    status = _android_tts_status(use_cache=True)
    return bool(status.get("ok") or status.get("ready") or status.get("available"))

def _android_tts_voices(*, locale: str = "", limit: int = 500) -> dict[str, Any]:
    if not _android_tts_enabled():
        return {"ok": False, "engine": "android_native", "voices": [], "error": "PHONE_WORKER_ANDROID_TTS_ENABLED=false"}
    payload = {"locale": str(locale or ""), "limit": max(1, min(600, int(limit or 500)))}
    try:
        data = _android_tts_json_request("/native-tts/voices", payload=payload, timeout=max(0.6, _android_tts_timeout_seconds(1.2)))
        voices = data.get("voices") if isinstance(data, dict) else []
        if not isinstance(voices, list):
            voices = []
        return {
            "ok": bool(data.get("ok", True)),
            "engine": "android_native",
            "locale": data.get("locale") or locale,
            "total": data.get("total", len(voices)),
            "returned": data.get("returned", len(voices)),
            "voices": voices,
        }
    except Exception as exc:
        return {"ok": False, "engine": "android_native", "locale": locale, "voices": [], "error": f"{type(exc).__name__}: {_short_text(exc, limit=140)}"}

def _safe_name(name: Any, fallback: str = "file.bin") -> str:
    text = str(name or fallback).replace("\\", "/").strip().lstrip("/")
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part[:120])
    return "/".join(parts) or fallback


def _available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text("utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                return max(0, int(parts[1]) // 1024)
    except Exception:
        return None
    return None


def _teto_resource_snapshot() -> dict[str, Any]:
    memory_mb = _available_memory_mb()
    min_memory_mb = max(128, _env_int("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", 1200))
    battery = _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot())
    level = battery.get("level") if isinstance(battery, dict) else None
    temperature = battery.get("temperature_c") if isinstance(battery, dict) else None
    status = str((battery or {}).get("status") or "").strip().lower() if isinstance(battery, dict) else ""
    charging = bool((battery or {}).get("charging")) if isinstance(battery, dict) else False
    charging = charging or status in {"charging", "full"}
    min_battery = max(0, min(100, _env_int("PHONE_WORKER_TETO_MIN_BATTERY_PERCENT", 25)))
    max_temperature = max(30.0, _env_float("PHONE_WORKER_TETO_MAX_BATTERY_TEMP_C", 43.0))
    allow_low_when_charging = _env_bool("PHONE_WORKER_TETO_ALLOW_LOW_BATTERY_WHEN_CHARGING", True)
    core_job = _core_job_runtime_snapshot()
    active_type = str(core_job.get("active_type") or "").strip().lower()
    heavy_types = {
        "apk_build_debug", "apk_build", "worker_update", "self_update",
        "boot_repair", "audio_convert", "media_convert", "maintenance_heavy",
    }
    reasons: list[str] = []
    if _APK_BUILD_THREAD_LOCK.locked() or active_type in heavy_types:
        reasons.append(f"tarefa pesada ativa: {active_type or 'apk_build'}")
    if memory_mb is not None and memory_mb < min_memory_mb:
        reasons.append(f"memória disponível baixa: {memory_mb} MB < {min_memory_mb} MB")
    try:
        if temperature is not None and float(temperature) > max_temperature:
            reasons.append(f"bateria aquecida: {float(temperature):.1f} °C > {max_temperature:.1f} °C")
    except (TypeError, ValueError):
        pass
    try:
        if level is not None and float(level) < min_battery and not (charging and allow_low_when_charging):
            reasons.append(f"bateria baixa: {float(level):.0f}% < {min_battery}%")
    except (TypeError, ValueError):
        pass
    return {
        "ok": not reasons,
        "reason": "; ".join(reasons) if reasons else "ok",
        "memory_available_mb": memory_mb,
        "minimum_memory_mb": min_memory_mb,
        "battery_level": level,
        "battery_temperature_c": temperature,
        "charging": charging,
        "active_heavy_job": active_type,
    }


def _get_teto_renderer():
    global _TETO_RENDERER, _TETO_RENDERER_ERROR
    with _TETO_RENDERER_LOCK:
        if _TETO_RENDERER is not None:
            return _TETO_RENDERER
        try:
            from teto_renderer import TetoRenderer
            _TETO_RENDERER = TetoRenderer(resource_guard=_teto_resource_snapshot)
            _TETO_RENDERER_ERROR = ""
            return _TETO_RENDERER
        except Exception as exc:
            _TETO_RENDERER_ERROR = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
            raise RuntimeError(f"renderer Teto indisponível: {_TETO_RENDERER_ERROR}") from exc


def _teto_status(*, force: bool = False) -> dict[str, Any]:
    enabled = _env_bool("PHONE_WORKER_TETO_ENABLED", False)
    result: dict[str, Any] = {
        "ok": False,
        "available": False,
        "ready": False,
        "enabled": enabled,
        "engine": "teto",
    }
    if not enabled:
        result["last_error"] = "PHONE_WORKER_TETO_ENABLED=false"
        result["resources"] = {"ok": False, "reason": "engine desativada"}
        return result
    try:
        result.update(_get_teto_renderer().status(force=force))
    except Exception as exc:
        result["last_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
    result["resources"] = _teto_resource_snapshot()
    return result


def _turbo_dependency_snapshot() -> dict[str, Any]:
    profile = _current_core_worker_profile()
    deps: dict[str, Any] = {
        "profile": profile,
        "turbo": profile == "turbo",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "curl": bool(shutil.which("curl")),
        "wget": bool(shutil.which("wget")),
        "piper_cli": bool(shutil.which("piper")),
        "piper_command": bool(str(os.getenv("PHONE_WORKER_PIPER_COMMAND", "") or "").strip()),
        "piper_model": False,
        "piper_config": False,
        "edge_tts": False,
        "gtts": False,
        "android_native_tts": False,
        "teto_tts": False,
    }
    model = str(os.getenv("PHONE_WORKER_PIPER_MODEL", "") or "").strip()
    config = str(os.getenv("PHONE_WORKER_PIPER_CONFIG", "") or "").strip()
    if model:
        with contextlib.suppress(Exception):
            deps["piper_model"] = Path(model).expanduser().exists() and Path(model).expanduser().stat().st_size > 0
    if config:
        with contextlib.suppress(Exception):
            deps["piper_config"] = Path(config).expanduser().exists() and Path(config).expanduser().stat().st_size > 0
    try:
        import edge_tts  # type: ignore  # noqa: F401
        deps["edge_tts"] = True
    except Exception as exc:
        deps["edge_tts_error"] = _short_text(exc, limit=80)
    try:
        import gtts  # type: ignore  # noqa: F401
        deps["gtts"] = True
    except Exception as exc:
        deps["gtts_error"] = _short_text(exc, limit=80)
    android_status = _android_tts_status(use_cache=True)
    deps["android_native_tts"] = bool(android_status.get("ok") or android_status.get("ready") or android_status.get("available"))
    deps["android_native_enabled"] = bool(android_status.get("enabled", True))
    deps["android_native_url"] = str(android_status.get("url") or _android_tts_base_url())
    if android_status.get("last_error"):
        deps["android_native_error"] = _short_text(android_status.get("last_error"), limit=100)
    teto_status = _teto_status()
    deps["teto"] = teto_status
    teto_resources = teto_status.get("resources") if isinstance(teto_status.get("resources"), dict) else {}
    deps["teto_tts"] = bool(teto_status.get("ready") and teto_resources.get("ok", True))
    deps["teto_enabled"] = bool(teto_status.get("enabled"))
    deps["teto_fingerprint"] = str(teto_status.get("fingerprint") or "")[:64]
    if teto_status.get("last_error"):
        deps["teto_error"] = _short_text(teto_status.get("last_error"), limit=120)
    missing = [key for key in ("ffmpeg", "ffprobe", "edge_tts", "gtts") if not deps.get(key)]
    deps["ok"] = not missing
    deps["missing"] = missing
    return deps



def _tts_agent_available_engines(deps: dict[str, Any] | None = None) -> list[str]:
    deps = deps if isinstance(deps, dict) else _turbo_dependency_snapshot()
    engines: list[str] = []
    if deps.get("teto_tts"):
        engines.append("teto")
    if deps.get("android_native_tts"):
        engines.append("android_native")
    # Piper ficou legado. O fluxo normal anuncia Teto/ATTS/Edge/gTTS.
    if deps.get("edge_tts"):
        engines.append("edge")
    if deps.get("gtts"):
        engines.append("gtts")
    return engines

def _tts_agent_preferred_engine(available: list[str]) -> str:
    requested = str(os.getenv("PHONE_WORKER_TTS_AGENT_ENGINE") or "auto").strip().lower().replace("-", "_") or "auto"
    aliases = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "edge_tts": "edge", "android": "android_native", "android_tts": "android_native", "native": "android_native", "native_android": "android_native", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto"}
    requested = aliases.get(requested, requested)
    if requested != "auto" and requested in available:
        return requested
    for candidate in ("android_native", "edge", "gtts"):
        if candidate in available:
            return candidate
    return ""


def _tts_agent_queue_limit() -> int:
    return max(1, min(8, _env_int("PHONE_WORKER_TTS_AGENT_CONCURRENCY", 1)))


def _tts_agent_snapshot() -> dict[str, Any]:
    profile = _current_core_worker_profile()
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    deps = _turbo_dependency_snapshot()
    available = _tts_agent_available_engines(deps)
    preferred = _tts_agent_preferred_engine(available)
    synth_allowed = profile == "turbo" and "tts-synth" in capabilities
    enabled = _env_bool("PHONE_WORKER_TTS_AGENT_ENABLED", True)
    with _TTS_AGENT_LOCK:
        active = int(_TTS_AGENT_ACTIVE)
        total = int(_TTS_AGENT_TOTAL)
        failed = int(_TTS_AGENT_FAILED)
        total_ms = float(_TTS_AGENT_TOTAL_MS)
        last_error = str(_TTS_AGENT_LAST_ERROR or "")
        last_engine = str(_TTS_AGENT_LAST_ENGINE or "")
        last_ok_at = float(_TTS_AGENT_LAST_OK_AT or 0.0)
    avg_ms = round(total_ms / total, 2) if total else 0.0
    missing = []
    if not enabled:
        missing.append("PHONE_WORKER_TTS_AGENT_ENABLED=false")
    if profile != "turbo":
        missing.append("perfil turbo")
    if "tts-synth" not in capabilities:
        missing.append("capacidade tts-synth")
    if not available:
        missing.append("engine TTS disponível")
    return {
        "ok": bool(enabled and synth_allowed and available),
        "available": bool(enabled and synth_allowed and available),
        "synth_ready": bool(enabled and synth_allowed and available),
        "state": "ready" if (enabled and synth_allowed and available) else "not_ready",
        "reason": "ok" if (enabled and synth_allowed and available) else ", ".join(missing),
        "profile": profile,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "available_engines": available,
        "preferred_engine": preferred,
        "selected_engine": preferred or last_engine,
        "deps": deps,
        "teto": deps.get("teto") if isinstance(deps.get("teto"), dict) else _teto_status(),
        "active": active,
        "concurrency_limit": _tts_agent_queue_limit(),
        "total": total,
        "failed": failed,
        "avg_synth_ms": avg_ms,
        "last_error": _short_text(last_error, limit=180),
        "last_engine": last_engine,
        "last_ok_age_seconds": round(time.time() - last_ok_at, 1) if last_ok_at else None,
    }


def _tts_agent_record_start() -> None:
    global _TTS_AGENT_ACTIVE
    with _TTS_AGENT_LOCK:
        _TTS_AGENT_ACTIVE += 1


def _tts_agent_record_done(*, ok: bool, engine: str, elapsed_ms: float, error: str = "") -> None:
    global _TTS_AGENT_ACTIVE, _TTS_AGENT_TOTAL, _TTS_AGENT_FAILED, _TTS_AGENT_TOTAL_MS, _TTS_AGENT_LAST_ERROR, _TTS_AGENT_LAST_ENGINE, _TTS_AGENT_LAST_OK_AT
    with _TTS_AGENT_LOCK:
        _TTS_AGENT_ACTIVE = max(0, _TTS_AGENT_ACTIVE - 1)
        _TTS_AGENT_TOTAL += 1
        _TTS_AGENT_TOTAL_MS += max(0.0, float(elapsed_ms or 0.0))
        _TTS_AGENT_LAST_ENGINE = str(engine or "")[:40]
        if ok:
            _TTS_AGENT_LAST_OK_AT = time.time()
            _TTS_AGENT_LAST_ERROR = ""
        else:
            _TTS_AGENT_FAILED += 1
            _TTS_AGENT_LAST_ERROR = _short_text(error, limit=220)

def _cache_dir_snapshot(path: Path, *, max_scan_files: int = 20000) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "files": 0,
        "bytes": 0,
        "scanned_files": 0,
    }
    try:
        if not path.exists():
            return result
        entries = []
        for item in path.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() not in {".mp3", ".wav", ".ogg"}:
                continue
            entries.append(item)
            if len(entries) >= max_scan_files:
                break
        total = 0
        newest = 0.0
        oldest = 0.0
        for item in entries:
            with contextlib.suppress(Exception):
                st = item.stat()
                total += int(st.st_size or 0)
                newest = max(newest, float(st.st_mtime or 0.0))
                oldest = float(st.st_mtime or 0.0) if not oldest else min(oldest, float(st.st_mtime or 0.0))
        result.update({
            "files": len(entries),
            "bytes": total,
            "scanned_files": len(entries),
            "oldest_mtime": oldest or None,
            "newest_mtime": newest or None,
        })
    except Exception as exc:
        result["error"] = _short_text(exc, limit=100)
    return result


def _phone_lavalink_env_value(name: str, default: str = "") -> str:
    value = str(os.getenv(name) or "").strip()
    if value:
        return value
    env_file = Path(os.getenv("PHONE_LAVALINK_ENV") or str(Path.home() / ".phone-lavalink.env")).expanduser()
    try:
        if not env_file.exists():
            return default
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, raw_value = raw.split("=", 1)
            if key.strip() != name:
                continue
            return raw_value.strip().strip('"').strip("'")
    except Exception:
        return default
    return default



_LAVALINK_AUTOSTART_LOCK = threading.Lock()
_LAVALINK_AUTOSTART_LAST_AT = 0.0


def _truthy_env(name: str, default: bool = False) -> bool:
    return _env_bool(name, default)


def _phone_lavalink_port() -> int:
    port_raw = _phone_lavalink_env_value("PHONE_LAVALINK_PORT", _phone_lavalink_env_value("MUSIC_WORKER_LAVALINK_PORT", "2333")) or "2333"
    try:
        return max(1, min(65535, int(str(port_raw).strip())))
    except Exception:
        return 2333


def _read_lavalink_application_password() -> str:
    app_path = Path(_phone_lavalink_env_value("PHONE_LAVALINK_APPLICATION_YML", str(Path.home() / "lavalink" / "application.yml"))).expanduser()
    try:
        if not app_path.exists():
            return ""
        text = app_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            if key.strip().lower() != "password":
                continue
            value = value.strip().strip('"').strip("'")
            if value.startswith("${") and value.endswith("}"):
                env_key = value[2:-1].split(":", 1)[0].strip()
                return str(os.getenv(env_key) or "").strip()
            return value
    except Exception:
        return ""
    return ""


def _phone_lavalink_password() -> str:
    return (
        _phone_lavalink_env_value("PHONE_LAVALINK_PASSWORD", "")
        or _phone_lavalink_env_value("AUX_LAVALINK_PASSWORD", "")
        or _phone_lavalink_env_value("MUSIC_WORKER_LAVALINK_PASSWORD", "")
        or _read_lavalink_application_password()
    )


def _probe_local_lavalink_http(*, timeout: float = 2.5) -> tuple[bool, int, str]:
    port = _phone_lavalink_port()
    password = _phone_lavalink_password()
    headers = {"Accept": "text/plain"}
    if password:
        headers["Authorization"] = password
    req = urllib.request.Request(f"http://127.0.0.1:{port}/version", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout))) as response:
            body = response.read(512).decode("utf-8", errors="replace").strip()
            status = int(getattr(response, "status", 0) or 0)
        return (200 <= status < 300), status, body
    except urllib.error.HTTPError as exc:
        # 401 prova que existe Lavalink respondendo, mas a senha do probe não bateu
        # ou não foi enviada. Para autostart isso é suficiente para não duplicar
        # processos; para health completo, _music_node_snapshot usa a senha e marca
        # saudável apenas quando der 2xx.
        return (int(exc.code) == 401 and not password), int(exc.code), _short_text(exc.reason, limit=80)
    except Exception as exc:
        return False, 0, _short_text(f"{type(exc).__name__}: {exc}", limit=120)


def _spawn_builtin_lavalink_proot_start() -> tuple[bool, str]:
    if not shutil.which("tmux"):
        return False, "tmux não encontrado"
    if not shutil.which("proot-distro"):
        return False, "proot-distro não encontrado"
    host_dir = Path(_phone_lavalink_env_value("PHONE_LAVALINK_HOST_DIR", str(Path.home() / "lavalink"))).expanduser()
    jar = host_dir / "Lavalink.jar"
    if not jar.exists():
        return False, f"Lavalink.jar não encontrado em {host_dir}"
    session = _phone_lavalink_env_value("PHONE_LAVALINK_TMUX_SESSION", "lavalink-debian") or "lavalink-debian"
    distro = _phone_lavalink_env_value("PHONE_LAVALINK_PROOT_DISTRO", "debian") or "debian"
    proot_dir = _phone_lavalink_env_value("PHONE_LAVALINK_PROOT_DIR", "/root/lavalink") or "/root/lavalink"
    java_xmx = _phone_lavalink_env_value("PHONE_LAVALINK_JAVA_XMX", "384m") or "768m"
    log_name = _phone_lavalink_env_value("PHONE_LAVALINK_LOG_NAME", "lavalink-proot.log") or "lavalink-proot.log"
    subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
    with contextlib.suppress(Exception):
        subprocess.run(["pkill", "-f", "java.*Lavalink.jar"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
    command = (
        "cd " + shlex.quote(proot_dir) +
        " && mkdir -p /tmp/lavalink" +
        " && exec /usr/bin/java -Djava.io.tmpdir=/tmp/lavalink -Xmx" + shlex.quote(java_xmx) +
        " -jar Lavalink.jar >> " + shlex.quote(log_name) + " 2>&1"
    )
    tmux_cmd = [
        "tmux", "new-session", "-d", "-s", session,
        "proot-distro", "login", distro,
        "--bind", f"{host_dir}:{proot_dir}",
        "--", "bash", "-lc", command,
    ]
    subprocess.Popen(tmux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, f"sessão {session} iniciada"


def _ensure_phone_lavalink_started(reason: str = "health") -> dict[str, Any]:
    global _LAVALINK_AUTOSTART_LAST_AT
    if not _env_autostart_enabled("PHONE_LAVALINK_AUTO_START", "auto"):
        return {"attempted": False, "reason": "auto_start_disabled_or_safe_mode", "safe_mode": _phone_worker_safe_mode_enabled()}
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    caps = {str(x).strip().lower() for x in (roles + capabilities)}
    if not ({"music", "music-node", "music-lavalink"} & caps) and _current_core_worker_profile() != "turbo":
        return {"attempted": False, "reason": "worker_sem_capacidade_music"}
    alive, status, body = _probe_local_lavalink_http(timeout=1.5)
    if alive or status == 401:
        return {"attempted": False, "reason": "already_online", "http_status": status}
    now = time.time()
    cooldown = max(5.0, _env_float("PHONE_LAVALINK_AUTO_START_COOLDOWN_SECONDS", 30.0))
    if now - _LAVALINK_AUTOSTART_LAST_AT < cooldown:
        return {"attempted": False, "reason": "cooldown", "last_error": body}
    with _LAVALINK_AUTOSTART_LOCK:
        now = time.time()
        if now - _LAVALINK_AUTOSTART_LAST_AT < cooldown:
            return {"attempted": False, "reason": "cooldown", "last_error": body}
        _LAVALINK_AUTOSTART_LAST_AT = now
        script = Path(_phone_lavalink_env_value("PHONE_LAVALINK_START_COMMAND", str(Path.home() / "start-phone-lavalink.sh"))).expanduser()
        try:
            if script.exists() and os.access(script, os.X_OK):
                subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"[phone-worker] lavalink auto-start solicitado via {script} ({reason})", flush=True)
                return {"attempted": True, "method": "script", "script": str(script)}
            ok, detail = _spawn_builtin_lavalink_proot_start()
            if ok:
                print(f"[phone-worker] lavalink auto-start solicitado via proot/tmux ({reason})", flush=True)
            else:
                print(f"[phone-worker] lavalink auto-start falhou: {detail}", flush=True)
            return {"attempted": bool(ok), "method": "builtin-proot", "detail": detail}
        except Exception as exc:
            detail = _short_text(f"{type(exc).__name__}: {exc}", limit=160)
            print(f"[phone-worker] lavalink auto-start erro: {detail}", flush=True)
            return {"attempted": False, "error": detail}

def _music_node_snapshot() -> dict[str, Any]:
    autostart = _ensure_phone_lavalink_started(reason="music_node_snapshot")
    port = _phone_lavalink_port()
    password = _phone_lavalink_password()
    bind_host = _phone_lavalink_env_value("PHONE_LAVALINK_BIND_HOST", "127.0.0.1") or "127.0.0.1"
    public_host = (
        _phone_lavalink_env_value("PHONE_LAVALINK_PUBLIC_HOST", "")
        or _phone_lavalink_env_value("MUSIC_WORKER_LAVALINK_HOST", "")
        or _phone_lavalink_env_value("PHONE_LAVALINK_HOST", "")
    )
    public_port_raw = _phone_lavalink_env_value("PHONE_LAVALINK_PUBLIC_PORT", _phone_lavalink_env_value("MUSIC_WORKER_LAVALINK_PORT", ""))
    try:
        public_port = max(1, min(65535, int(str(public_port_raw).strip()))) if str(public_port_raw or "").strip() else port
    except Exception:
        public_port = port
    url = f"http://127.0.0.1:{port}/version"
    headers = {"Accept": "text/plain"}
    if password:
        headers["Authorization"] = password
    result: dict[str, Any] = {
        "kind": "lavalink",
        "mode": "lavalink",
        "host": bind_host,
        "port": port,
        "public_host": public_host,
        "public_port": public_port,
        "connect_host": public_host,
        "connect_port": public_port,
        "ok": False,
        "online": False,
        "state": "offline",
        "autostart": autostart,
    }
    start = time.time()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=2.5) as response:
            body = response.read(512).decode("utf-8", errors="replace").strip()
            status = int(getattr(response, "status", 0) or 0)
        healthy = 200 <= status < 300
        result.update({
            "ok": healthy,
            "online": healthy,
            "state": "healthy" if healthy else f"http_{status}",
            "http_status": status,
            "version": _short_text(body, limit=80),
            "latency_ms": round((time.time() - start) * 1000.0, 1),
            "music_available": healthy,
            "playback_modes": ["lavalink"] if healthy else [],
        })
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        # 401 ainda prova que o Lavalink está vivo; a VPS/bot pode ter a senha
        # correta mesmo quando o worker não conseguiu ler application.yml/env.
        alive = status == 401
        result.update({
            "ok": alive,
            "online": alive,
            "state": "auth_required" if alive else f"http_{status}",
            "http_status": status,
            "error": "authorization_required" if alive else _short_text(exc.reason, limit=100),
            "latency_ms": round((time.time() - start) * 1000.0, 1),
            "music_available": alive,
            "playback_modes": ["lavalink"] if alive else [],
        })
    except Exception as exc:
        result.update({
            "error": _short_text(f"{type(exc).__name__}: {exc}", limit=120),
            "latency_ms": round((time.time() - start) * 1000.0, 1),
            "music_available": False,
            "playback_modes": [],
        })
    return result


def _worker_turbo_cache_snapshot() -> dict[str, Any]:
    tts_dir = Path(os.getenv("PHONE_WORKER_TTS_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "tts")).expanduser()
    piper_dir = Path(os.getenv("PHONE_WORKER_PIPER_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "piper")).expanduser()
    return {
        "tts": _cache_dir_snapshot(tts_dir),
        "piper": _cache_dir_snapshot(piper_dir),
        "tts_limits": {
            "max_mb": int(float(os.getenv("PHONE_WORKER_TTS_CACHE_MAX_MB", "4096") or 4096)),
            "max_files": int(float(os.getenv("PHONE_WORKER_TTS_CACHE_MAX_FILES", "20000") or 20000)),
        },
        "piper_limits": {
            "max_mb": int(float(os.getenv("PHONE_WORKER_PIPER_CACHE_MAX_MB", "2048") or 2048)),
            "max_files": int(float(os.getenv("PHONE_WORKER_PIPER_CACHE_MAX_FILES", "4096") or 4096)),
        },
    }


def _read_music_agent_version_from_path(path: Path | None = None) -> str:
    path = path or (_phone_worker_dir() / "music_agent.py")
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else ""


def _version_tuple_loose(value: Any) -> tuple[int, ...]:
    try:
        parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    except Exception:
        parts = []
    return tuple(parts or [0])


def _version_lt_loose(current: Any, target: Any) -> bool:
    left = _version_tuple_loose(current)
    right = _version_tuple_loose(target)
    size = max(len(left), len(right))
    left = left + (0,) * (size - len(left))
    right = right + (0,) * (size - len(right))
    return left < right


def _music_agent_start_script() -> Path:
    explicit = str(os.getenv("MUSIC_AGENT_START_COMMAND") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return _best_script("start-phone-music-agent.sh")


def _music_agent_pid_file() -> Path:
    return Path(os.getenv("MUSIC_AGENT_PID_FILE") or (_phone_worker_dir() / "music_agent.pid")).expanduser()


def _music_agent_log_file() -> Path:
    return Path(os.getenv("MUSIC_AGENT_LOG_FILE") or (_phone_worker_dir() / "music_agent.log")).expanduser()


_TTS_DEP_AUTOINSTALL_LOCK = threading.Lock()
_TTS_DEP_AUTOINSTALL_LAST_AT = 0.0
_TTS_DEP_AUTOINSTALL_RUNNING = False
_TTS_AGENT_LOCK = threading.Lock()
_TTS_AGENT_ACTIVE = 0
_TTS_AGENT_TOTAL = 0
_TTS_AGENT_FAILED = 0
_TTS_AGENT_TOTAL_MS = 0.0
_TTS_AGENT_LAST_ERROR = ""
_TTS_AGENT_LAST_ENGINE = ""
_TTS_AGENT_LAST_OK_AT = 0.0
_TTS_CACHE_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-cache-maint")
_TTS_CACHE_MAINTENANCE_SLOTS = threading.BoundedSemaphore(2)


def _submit_tts_cache_maintenance(callback, *args, **kwargs) -> bool:
    if not _TTS_CACHE_MAINTENANCE_SLOTS.acquire(blocking=False):
        return False

    def _runner() -> None:
        try:
            callback(*args, **kwargs)
        finally:
            _TTS_CACHE_MAINTENANCE_SLOTS.release()

    try:
        _TTS_CACHE_MAINTENANCE_EXECUTOR.submit(_runner)
        return True
    except Exception:
        _TTS_CACHE_MAINTENANCE_SLOTS.release()
        return False


def _music_voice_dependency_specs() -> dict[str, dict[str, Any]]:
    return {
        "discord.py": {"module": "discord", "pip": "discord.py"},
        "PyNaCl": {"module": "nacl", "pip": "PyNaCl"},
        "davey": {"module": "davey", "pip": "davey"},
        "yt-dlp": {"module": "yt_dlp", "pip": "yt-dlp"},
        "wavelink": {"module": "wavelink", "pip": "wavelink"},
        "aiohttp": {"module": "aiohttp", "pip": "aiohttp"},
        "gTTS": {"module": "gtts", "pip": "gTTS"},
        "edge-tts": {"module": "edge_tts", "pip": "edge-tts"},
    }


def _module_import_ok(module_name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(module_name)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {_short_text(exc, limit=120)}"


def _phone_worker_deps_mode_enabled() -> bool:
    """Whether safe, missing-only dependency auto-install may run.

    Older emergency configs used *_MODE=off to stop heavy grpcio/google-cloud
    compile loops. Keep that value as safe-only instead of globally disabling
    idempotent installs. Use disabled/never/none to block all auto-install.
    """
    for key in ("PHONE_WORKER_TURBO_DEPS_INSTALL_MODE", "PHONE_WORKER_DEPS_INSTALL_MODE", "PHONE_WORKER_TURBO_DEPS_INSTALL", "PHONE_WORKER_TTS_DEPS_INSTALL"):
        raw = str(os.getenv(key) or "").strip().lower().strip('"\'')
        if raw in {"disabled", "disable", "never", "none", "bloqueado"}:
            return False
        if raw:
            return True
    return True


def _phone_worker_heavy_python_deps_enabled() -> bool:
    raw = str(os.getenv("PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL") or "").strip().lower().strip('"\'')
    return raw in {"1", "true", "on", "yes", "sim"}


def _phone_worker_allow_pip_source_builds() -> bool:
    raw = str(os.getenv("PHONE_WORKER_ALLOW_PIP_SOURCE_BUILDS") or "").strip().lower().strip('"\'')
    return raw in {"1", "true", "on", "yes", "sim"}


def _dependency_install_state_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_DEPS_STATE_DIR") or (_phone_worker_dir() / ".dependency-install")).expanduser()


def _dependency_attempt_allowed(key: str, cooldown_seconds: float) -> tuple[bool, str, float]:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120] or "dependency"
    state_dir = _dependency_install_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    marker = state_dir / f"{safe_key}.last"
    now = time.time()
    last = 0.0
    try:
        last = float(marker.read_text(encoding="utf-8", errors="ignore").strip() or "0")
    except Exception:
        last = 0.0
    if last and now - last < cooldown_seconds:
        return False, "cooldown", max(0.0, cooldown_seconds - (now - last))
    try:
        marker.write_text(str(int(now)), encoding="utf-8")
    except Exception:
        pass
    return True, "allowed", 0.0


def _phone_worker_safe_mode_enabled() -> bool:
    for key in ("PHONE_WORKER_SAFE_MODE", "PHONE_WORKER_BASIC_ONLY", "PHONE_WORKER_LIGHT_MODE", "PHONE_WORKER_DISABLE_HEAVY_SERVICES"):
        if _env_bool(key, False):
            return True
    return False


def _env_autostart_enabled(name: str, default: str = "auto") -> bool:
    raw = str(os.getenv(name) if os.getenv(name) is not None else default).strip().lower().strip('"\'')
    if raw in {"1", "true", "on", "yes", "sim"}:
        return True
    if raw in {"0", "false", "off", "no", "nao", "não"}:
        return False
    if _phone_worker_safe_mode_enabled():
        return False
    return True


def _start_tts_dependency_autoinstall(missing: list[str], checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    global _TTS_DEP_AUTOINSTALL_LAST_AT, _TTS_DEP_AUTOINSTALL_RUNNING
    enabled = _env_bool("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", True) and _phone_worker_deps_mode_enabled()
    if not enabled:
        return {"enabled": False, "started": False, "reason": "disabled"}
    if _phone_worker_safe_mode_enabled():
        return {"enabled": False, "started": False, "reason": "safe_mode"}
    specs = _music_voice_dependency_specs()
    heavy_enabled = _phone_worker_heavy_python_deps_enabled()
    install_items: list[dict[str, Any]] = []
    skipped_optional: list[str] = []
    for name in missing:
        spec = specs.get(name)
        if not spec or not spec.get("pip"):
            continue
        optional = bool(spec.get("optional"))
        if optional and not heavy_enabled:
            skipped_optional.append(name)
            continue
        install_items.append({"name": name, "package": str(spec.get("pip")), "optional": optional})
    system_packages: list[str] = []
    if ("ffmpeg" in missing or "ffprobe" in missing) and shutil.which("pkg"):
        system_packages.append("ffmpeg")
    install_items = list({str(item["name"]): item for item in install_items}.values())
    system_packages = [p for p in dict.fromkeys(system_packages) if p]
    if not install_items and not system_packages:
        reason = "optional_only" if skipped_optional else "nothing_installable"
        return {"enabled": True, "started": False, "reason": reason, "skipped_optional": skipped_optional}
    cooldown = max(60.0, float(_env_int("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_COOLDOWN_SECONDS", 900)))
    now = time.time()
    with _TTS_DEP_AUTOINSTALL_LOCK:
        if _TTS_DEP_AUTOINSTALL_RUNNING:
            return {
                "enabled": True,
                "started": False,
                "reason": "already_running",
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        if _TTS_DEP_AUTOINSTALL_LAST_AT and now - _TTS_DEP_AUTOINSTALL_LAST_AT < cooldown:
            return {
                "enabled": True,
                "started": False,
                "reason": "cooldown",
                "remaining_seconds": round(cooldown - (now - _TTS_DEP_AUTOINSTALL_LAST_AT), 1),
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        allowed, reason, remaining = _dependency_attempt_allowed("tts-autoinstall", cooldown)
        if not allowed:
            return {
                "enabled": True,
                "started": False,
                "reason": reason,
                "remaining_seconds": round(remaining, 1),
                "pip": [item["package"] for item in install_items],
                "system": system_packages,
                "skipped_optional": skipped_optional,
            }
        _TTS_DEP_AUTOINSTALL_RUNNING = True
        _TTS_DEP_AUTOINSTALL_LAST_AT = now

    def _runner() -> None:
        global _TTS_DEP_AUTOINSTALL_RUNNING
        try:
            timeout = max(45, _env_int("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS_TIMEOUT_SECONDS", 240))
            allow_source = _phone_worker_allow_pip_source_builds()
            for item in install_items:
                name = str(item.get("name") or "")
                package = str(item.get("package") or "")
                if not package:
                    continue
                # Recheck inside the worker thread so a race does not reinstall an
                # already-present package.
                module = str(specs.get(name, {}).get("module") or "")
                if module:
                    ok, _ = _module_import_ok(module)
                    if ok:
                        continue
                pip_cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--upgrade", "--prefer-binary"]
                if bool(item.get("optional")) or not allow_source:
                    pip_cmd.append("--only-binary=:all:")
                pip_cmd.append(package)
                subprocess.run(pip_cmd, timeout=timeout, check=False)
            if system_packages and shutil.which("pkg"):
                env = dict(os.environ)
                env.setdefault("DEBIAN_FRONTEND", "noninteractive")
                subprocess.run(["pkg", "install", "-y", *system_packages], timeout=timeout, check=False, env=env)
        except Exception:
            pass
        finally:
            with _TTS_DEP_AUTOINSTALL_LOCK:
                _TTS_DEP_AUTOINSTALL_RUNNING = False

    threading.Thread(target=_runner, name="tts-dependency-autoinstall", daemon=True).start()
    return {
        "enabled": True,
        "started": True,
        "pip": [item["package"] for item in install_items],
        "system": system_packages,
        "skipped_optional": skipped_optional,
        "safe_only_binary": not _phone_worker_allow_pip_source_builds(),
    }


def _music_voice_dependencies_snapshot() -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    specs = _music_voice_dependency_specs()
    for label, spec in specs.items():
        ok, error = _module_import_ok(str(spec.get("module") or ""))
        checks[label] = {"ok": ok, "optional": bool(spec.get("optional"))}
        if error:
            checks[label]["error"] = error
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        checks[binary] = {"ok": bool(path), "path": path or ""}
    missing = [name for name, info in checks.items() if not bool(info.get("ok"))]
    missing_critical = [name for name in missing if not bool(checks.get(name, {}).get("optional"))]
    optional_missing = [name for name in missing if bool(checks.get(name, {}).get("optional"))]
    # Tenta preparar o worker automaticamente sem bloquear o heartbeat/status.
    auto_install = _start_tts_dependency_autoinstall(missing, checks) if missing else {"enabled": _env_bool("PHONE_WORKER_AUTO_INSTALL_TTS_DEPS", True), "started": False, "reason": "ok"}
    return {
        "ok": not missing_critical,
        "missing": missing_critical,
        "optional_missing": optional_missing,
        "all_missing": missing,
        "checks": checks,
        "auto_install": auto_install,
    }


def _music_agent_snapshot() -> dict[str, Any]:
    """Small local health probe for the same-bot Music Agent.

    This is intentionally best-effort and never exposes tokens. The VPS uses it
    to know whether the worker can own voice/playback before falling back to any
    legacy music route.
    """
    _load_phone_worker_runtime_env()
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip() or _ensure_music_agent_token_env(persist=True)
    host = str(os.getenv("MUSIC_AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(float(os.getenv("MUSIC_AGENT_PORT") or 8780))
    except Exception:
        port = 8780
    configured = bool(str(os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip())
    safe_mode = _phone_worker_safe_mode_enabled()
    deps = _music_voice_dependencies_snapshot()
    file_version = _read_music_agent_version_from_path()
    url = f"http://{host}:{port}/health"
    headers = {"Accept": "application/json", "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=max(0.5, min(5.0, _env_float("MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 2.5)))) as resp:
            raw = resp.read(512 * 1024).decode("utf-8", "replace")
            status = int(getattr(resp, "status", 200) or 200)
        data = json.loads(raw or "{}") if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        runtime_version = str(data.get("version") or "").strip()
        discord_ready = bool(data.get("discord_ready"))
        # YouTube direto usa voz/ffmpeg do Music Agent e não precisa que o pool
        # Lavalink esteja conectado. Pool conectado é detalhe técnico para
        # playlists/Spotify/SoundCloud, não condição para o worker existir.
        available = bool(data.get("available") or discord_ready)
        needs_restart = bool(runtime_version and file_version and _version_lt_loose(runtime_version, file_version))
        data.update({
            "ok": available and bool(deps.get("ok", True)),
            "available": available and bool(deps.get("ok", True)),
            "configured": configured,
            "safe_mode": safe_mode,
            "auto_start_allowed": _env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version,
            "runtime_version": runtime_version,
            "needs_restart": needs_restart,
            "host": host,
            "port": port,
            "http_status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        })
        return data
    except urllib.error.HTTPError as exc:
        raw = ""
        with contextlib.suppress(Exception):
            raw = exc.read(1024).decode("utf-8", "replace")
        return {
            "ok": False,
            "available": False,
            "configured": configured,
            "safe_mode": safe_mode,
            "auto_start_allowed": _env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version,
            "host": host,
            "port": port,
            "http_status": int(exc.code),
            "error": _short_text(raw or exc.reason, limit=180),
            "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        }
    except Exception as exc:
        return {
            "ok": False,
            "available": False,
            "configured": configured,
            "safe_mode": safe_mode,
            "auto_start_allowed": _env_autostart_enabled("MUSIC_AGENT_ENABLED", "auto"),
            "file_version": file_version,
            "host": host,
            "port": port,
            "error": f"{type(exc).__name__}: {_short_text(exc, limit=160)}",
            "voice_dependencies": deps,
            "dependency_missing": list(deps.get("missing") or []),
            "optional_dependency_missing": list(deps.get("optional_missing") or []),
        }


_VOICE_AGENT_SESSION_LOCK = threading.RLock()
_VOICE_AGENT_SESSION_MEMORY: dict[str, Any] | None = None
_VOICE_AGENT_HANDOFF_MEMORY: dict[str, dict[str, Any]] = {}
_VOICE_AGENT_CONNECTION_MEMORY: dict[str, dict[str, Any]] = {}
_VOICE_AGENT_TRANSFER_MEMORY: dict[str, dict[str, Any]] = {}


def _voice_agent_state_file() -> Path:
    base = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    raw = str(os.getenv("PHONE_WORKER_VOICE_AGENT_STATE_FILE") or "").strip()
    return Path(raw).expanduser() if raw else base / "voice-agent-state.json"


def _voice_agent_now_ms() -> int:
    return int(time.time() * 1000)


def _voice_agent_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _voice_agent_clean_text(value: Any, *, limit: int = 160) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:/@# -]+", "", str(value or "")).strip()[:limit]


def _voice_agent_load_state() -> dict[str, Any]:
    global _VOICE_AGENT_SESSION_MEMORY
    with _VOICE_AGENT_SESSION_LOCK:
        if isinstance(_VOICE_AGENT_SESSION_MEMORY, dict):
            return _VOICE_AGENT_SESSION_MEMORY
        path = _voice_agent_state_file()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        _VOICE_AGENT_SESSION_MEMORY = {
            "version": 1,
            "updated_at_ms": _voice_agent_now_ms(),
            "sessions": {str(k): v for k, v in sessions.items() if isinstance(v, dict)},
        }
        return _VOICE_AGENT_SESSION_MEMORY


def _voice_agent_save_state(state: dict[str, Any]) -> None:
    global _VOICE_AGENT_SESSION_MEMORY
    with _VOICE_AGENT_SESSION_LOCK:
        state["updated_at_ms"] = _voice_agent_now_ms()
        _VOICE_AGENT_SESSION_MEMORY = state
        path = _voice_agent_state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            with contextlib.suppress(Exception):
                os.chmod(path, 0o600)
        except Exception:
            # O estado em memória continua suficiente; persistência é só conveniência.
            pass


def _voice_agent_public_session(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or _voice_agent_now_ms())
    expires_at_ms = _voice_agent_int(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    voice = raw.get("discord_voice") if isinstance(raw.get("discord_voice"), dict) else {}
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "text_channel_id": str(raw.get("text_channel_id") or ""),
        "requester_id": str(raw.get("requester_id") or ""),
        "source": str(raw.get("source") or "")[:40],
        "state": str(raw.get("state") or "registered")[:60],
        "registered_by": str(raw.get("registered_by") or "vps_control_plane")[:60],
        "age_seconds": round(max(0, now_ms - _voice_agent_int(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "session_id_present": bool(voice.get("session_id_present")),
        "endpoint_present": bool(voice.get("endpoint_present")),
        "voice_token_present": bool(voice.get("voice_token_present")),
        "endpoint_host": str(voice.get("endpoint_host") or "")[:120],
        "connected": bool(voice.get("connected")),
        "direct_tts_enabled": bool(raw.get("direct_tts_enabled")),
    }


def _voice_agent_public_handoff(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or _voice_agent_now_ms())
    expires_at_ms = _voice_agent_int(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    endpoint = _voice_agent_clean_text(raw.get("endpoint_host") or raw.get("endpoint"), limit=160)
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "source": str(raw.get("source") or "")[:40],
        "state": str(raw.get("state") or "handoff_registered")[:60],
        "dry_run": bool(raw.get("dry_run", True)),
        "age_seconds": round(max(0, now_ms - _voice_agent_int(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "session_id_present": bool(raw.get("session_id")),
        "endpoint_present": bool(endpoint),
        "voice_token_present": bool(raw.get("voice_token")),
        "endpoint_host": endpoint[:120],
        "voice_owner": str(raw.get("voice_owner") or raw.get("transport_owner") or "vps")[:40],
        "transport_owner": str(raw.get("transport_owner") or raw.get("voice_owner") or "vps")[:40],
        "connection_policy": str(raw.get("connection_policy") or "handoff_only_wait_for_voice_ownership")[:80],
        "allow_connection_probe": bool(raw.get("allow_connection_probe") or raw.get("allow_probe")),
        "complete": bool(raw.get("session_id") and raw.get("voice_token") and endpoint),
    }


def _voice_agent_prune_handoffs() -> dict[str, dict[str, Any]]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        stale = [key for key, data in _VOICE_AGENT_HANDOFF_MEMORY.items() if _voice_agent_int(data.get("expires_at_ms"), 0) and _voice_agent_int(data.get("expires_at_ms"), 0) <= now_ms]
        for key in stale:
            _VOICE_AGENT_HANDOFF_MEMORY.pop(key, None)
        return {str(k): dict(v) for k, v in _VOICE_AGENT_HANDOFF_MEMORY.items() if isinstance(v, dict)}


def _voice_agent_handoff_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    handoffs_dict = _voice_agent_prune_handoffs()
    handoffs = []
    for key, raw in handoffs_dict.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        handoffs.append(_voice_agent_public_handoff(raw, now_ms=now_ms))
    handoffs.sort(key=lambda item: float(item.get("age_seconds", 999999) or 999999))
    complete_count = sum(1 for item in handoffs if item.get("complete"))
    return {
        "handoff_count": len(handoffs),
        "handoff_complete_count": complete_count,
        "handoff_ready": complete_count > 0,
        "handoff_guilds": [str(item.get("guild_id") or "") for item in handoffs[:12] if item.get("guild_id")],
        "handoffs": handoffs[:limit],
        "last_handoff": handoffs[0] if handoffs else {},
    }



def _voice_agent_public_connection(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or _voice_agent_now_ms())
    started_at = _voice_agent_int(raw.get("started_at_ms"), now_ms)
    updated_at = _voice_agent_int(raw.get("updated_at_ms"), started_at)
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "state": str(raw.get("state") or "unknown")[:80],
        "stage": str(raw.get("stage") or "")[:80],
        "dry_run": bool(raw.get("dry_run", True)),
        "connected_once": bool(raw.get("connected_once")),
        "closed_after_probe": bool(raw.get("closed_after_probe")),
        "ws_url_present": bool(raw.get("ws_url_present")),
        "hello_received": bool(raw.get("hello_received")),
        "ready_received": bool(raw.get("ready_received")),
        "udp_probe_attempted": bool(raw.get("udp_probe_attempted")),
        "udp_probe_ok": bool(raw.get("udp_probe_ok")),
        "ssrc_present": bool(raw.get("ssrc_present")),
        "selected_protocol_ready": bool(raw.get("selected_protocol_ready")),
        "endpoint_host": str(raw.get("endpoint_host") or "")[:120],
        "voice_ip": str(raw.get("voice_ip") or "")[:80],
        "voice_port": _voice_agent_int(raw.get("voice_port"), 0),
        "latency_ms": raw.get("latency_ms"),
        "age_seconds": round(max(0, now_ms - started_at) / 1000.0, 1),
        "updated_age_seconds": round(max(0, now_ms - updated_at) / 1000.0, 1),
        "error": str(raw.get("error") or "")[:180],
    }


def _voice_agent_public_transfer(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or _voice_agent_now_ms())
    expires_at_ms = _voice_agent_int(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "state": str(raw.get("state") or "transfer_unknown")[:80],
        "current_owner": str(raw.get("current_owner") or raw.get("voice_owner") or "vps")[:40],
        "voice_owner": str(raw.get("voice_owner") or raw.get("current_owner") or "vps")[:40],
        "requested_owner": str(raw.get("requested_owner") or "worker")[:40],
        "lease_id": str(raw.get("lease_id") or "")[:80],
        "allow_connection_probe": bool(raw.get("allow_connection_probe")),
        "probe_authorized": bool(raw.get("probe_authorized")),
        "age_seconds": round(max(0, now_ms - _voice_agent_int(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "reason": str(raw.get("reason") or "")[:140],
        "error": str(raw.get("error") or "")[:160],
    }


def _voice_agent_prune_transfers() -> dict[str, dict[str, Any]]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        stale = [key for key, data in _VOICE_AGENT_TRANSFER_MEMORY.items() if _voice_agent_int(data.get("expires_at_ms"), 0) and _voice_agent_int(data.get("expires_at_ms"), 0) <= now_ms]
        for key in stale:
            _VOICE_AGENT_TRANSFER_MEMORY.pop(key, None)
        return {str(k): dict(v) for k, v in _VOICE_AGENT_TRANSFER_MEMORY.items() if isinstance(v, dict)}


def _voice_agent_transfer_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    transfers_dict = _voice_agent_prune_transfers()
    transfers = []
    for key, raw in transfers_dict.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        transfers.append(_voice_agent_public_transfer(raw, now_ms=now_ms))
    transfers.sort(key=lambda item: float(item.get("age_seconds", 999999) or 999999))
    ready_count = sum(1 for item in transfers if item.get("voice_owner") == "worker" and item.get("probe_authorized"))
    staged_count = sum(1 for item in transfers if str(item.get("state") or "").startswith("transfer_staged"))
    last = transfers[0] if transfers else {}
    return {
        "transfer_count": len(transfers),
        "transfer_ready_count": ready_count,
        "transfer_staged_count": staged_count,
        "transfer_ready": ready_count > 0,
        "transfer_guilds": [str(item.get("guild_id") or "") for item in transfers[:12] if item.get("guild_id")],
        "transfers": transfers[:limit],
        "last_transfer": last,
        "transfer_state": str(last.get("state") or ""),
        "current_voice_owner": str(last.get("voice_owner") or last.get("current_owner") or "vps") if last else "vps",
        "requested_voice_owner": str(last.get("requested_owner") or "") if last else "",
    }


def _voice_agent_set_transfer(guild_id: int, **updates: Any) -> dict[str, Any]:
    key = str(int(guild_id or 0))
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        current = dict(_VOICE_AGENT_TRANSFER_MEMORY.get(key) or {})
        current.setdefault("guild_id", key)
        current.setdefault("created_at_ms", now_ms)
        current.update(updates)
        current["updated_at_ms"] = now_ms
        _VOICE_AGENT_TRANSFER_MEMORY[key] = current
        return dict(current)


def _voice_agent_prepare_transfer(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", True):
        raise RuntimeError("controle de transferência de voz desativado")
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para transferência")
    ttl_seconds = max(10, min(180, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))))
    now_ms = _voice_agent_now_ms()
    lease_id = f"vta:{guild_id}:{channel_id}:{now_ms}"
    current_owner = str(body.get("current_owner") or body.get("voice_owner") or "vps").strip().lower() or "vps"
    requested_owner = str(body.get("requested_owner") or "worker").strip().lower() or "worker"
    transfer = _voice_agent_set_transfer(
        guild_id,
        channel_id=str(channel_id),
        text_channel_id=str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        requester_id=str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        bot_user_id=str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        source=_voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        state="transfer_staged_waiting_vps_release",
        current_owner=current_owner,
        voice_owner=current_owner,
        requested_owner=requested_owner,
        lease_id=lease_id,
        allow_connection_probe=False,
        probe_authorized=False,
        reason=_voice_agent_clean_text(body.get("reason") or "preparado pela VPS; aguardando liberação explícita", limit=160),
        error="",
        expires_at_ms=now_ms + ttl_seconds * 1000,
    )
    return {"ok": True, "prepared": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5)}


def _voice_agent_begin_transfer(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", True):
        raise RuntimeError("controle de transferência de voz desativado")
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    if guild_id <= 0:
        raise RuntimeError("guild_id obrigatório para iniciar transferência")
    if not bool(body.get("confirm_transfer") or body.get("confirm") or body.get("manual") or body.get("diagnostic")):
        raise RuntimeError("transferência exige confirmação explícita da VPS")
    transfers = _voice_agent_prune_transfers()
    current = dict(transfers.get(str(guild_id)) or {})
    if not current:
        current = _voice_agent_prepare_transfer(body).get("transfer") or {}
    handoffs = _voice_agent_prune_handoffs()
    handoff = dict(handoffs.get(str(guild_id)) or {})
    if not handoff:
        raise RuntimeError("handoff temporário ausente; não é seguro entregar posse")
    ttl_seconds = max(10, min(120, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))))
    now_ms = _voice_agent_now_ms()
    lease_id = str(current.get("lease_id") or f"vta:{guild_id}:{now_ms}")
    current.pop("guild_id", None)
    transfer = _voice_agent_set_transfer(
        guild_id,
        **current,
        state="worker_ownership_granted_waiting_probe",
        current_owner="worker",
        voice_owner="worker",
        requested_owner="worker",
        lease_id=lease_id,
        allow_connection_probe=True,
        probe_authorized=True,
        reason="VPS confirmou transferência controlada de posse da voz",
        error="",
        expires_at_ms=now_ms + ttl_seconds * 1000,
    )
    with _VOICE_AGENT_SESSION_LOCK:
        handoff["voice_owner"] = "worker"
        handoff["transport_owner"] = "worker"
        handoff["allow_connection_probe"] = True
        handoff["connection_policy"] = "worker_ownership_granted_explicit_transfer"
        handoff["updated_at_ms"] = now_ms
        _VOICE_AGENT_HANDOFF_MEMORY[str(guild_id)] = handoff
    return {"ok": True, "started": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5), **_voice_agent_handoff_summary(guild_id=guild_id, limit=5)}


def _voice_agent_release_transfer(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    if guild_id <= 0:
        raise RuntimeError("guild_id obrigatório para liberar transferência")
    reason = _voice_agent_clean_text(body.get("reason") or "released_by_vps", limit=120)
    now_ms = _voice_agent_now_ms()
    transfer = _voice_agent_set_transfer(
        guild_id,
        state="released_to_vps",
        current_owner="vps",
        voice_owner="vps",
        requested_owner="",
        allow_connection_probe=False,
        probe_authorized=False,
        reason=reason,
        expires_at_ms=now_ms + 10_000,
    )
    with _VOICE_AGENT_SESSION_LOCK:
        handoff = dict(_VOICE_AGENT_HANDOFF_MEMORY.get(str(guild_id)) or {})
        if handoff:
            handoff["voice_owner"] = "vps"
            handoff["transport_owner"] = "vps"
            handoff["allow_connection_probe"] = False
            handoff["connection_policy"] = "vps_owner_transfer_released"
            handoff["updated_at_ms"] = now_ms
            _VOICE_AGENT_HANDOFF_MEMORY[str(guild_id)] = handoff
    return {"ok": True, "released": True, "state": transfer.get("state"), "transfer": _voice_agent_public_transfer(transfer), **_voice_agent_transfer_summary(guild_id=guild_id, limit=5)}


def _voice_agent_connection_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        items = {str(k): dict(v) for k, v in _VOICE_AGENT_CONNECTION_MEMORY.items() if isinstance(v, dict)}
    connections = []
    for key, raw in items.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        connections.append(_voice_agent_public_connection(raw, now_ms=now_ms))
    connections.sort(key=lambda item: float(item.get("updated_age_seconds", 999999) or 999999))
    ready_count = sum(1 for item in connections if item.get("state") in {"connected_dry_run", "probe_ok", "voice_ws_ready"} or item.get("connected_once"))
    probing_count = sum(1 for item in connections if item.get("state") in {"probing", "connecting", "voice_ws_connecting"})
    failed_count = sum(1 for item in connections if str(item.get("state") or "").endswith("failed") or item.get("state") == "failed")
    return {
        "connection_count": len(connections),
        "connection_ready_count": ready_count,
        "connection_probing_count": probing_count,
        "connection_failed_count": failed_count,
        "connection_ready": ready_count > 0,
        "connection_guilds": [str(item.get("guild_id") or "") for item in connections[:12] if item.get("guild_id")],
        "connections": connections[:limit],
        "last_connection": connections[0] if connections else {},
    }


def _voice_agent_normalize_endpoint(endpoint: str) -> tuple[str, str]:
    raw = str(endpoint or "").strip()
    raw = raw.replace("wss://", "").replace("ws://", "").replace("https://", "").replace("http://", "")
    raw = raw.split("/", 1)[0].strip()
    host = _voice_agent_clean_text(raw, limit=180)
    if not host:
        raise RuntimeError("endpoint de voz vazio")
    return host, f"wss://{host}/?v=4"


def _voice_agent_set_connection(guild_id: int, **updates: Any) -> dict[str, Any]:
    key = str(int(guild_id or 0))
    now_ms = _voice_agent_now_ms()
    with _VOICE_AGENT_SESSION_LOCK:
        current = dict(_VOICE_AGENT_CONNECTION_MEMORY.get(key) or {})
        current.setdefault("guild_id", key)
        current.setdefault("started_at_ms", now_ms)
        current.update(updates)
        current["updated_at_ms"] = now_ms
        _VOICE_AGENT_CONNECTION_MEMORY[key] = current
        return dict(current)


def _voice_agent_udp_discovery_probe(*, ip: str, port: int, ssrc: int, timeout: float = 0.9) -> dict[str, Any]:
    result = {"attempted": False, "ok": False, "ip": "", "port": 0, "error": ""}
    if not ip or int(port or 0) <= 0 or int(ssrc or 0) <= 0:
        result["error"] = "ready sem ip/port/ssrc para UDP discovery"
        return result
    result["attempted"] = True
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(max(0.2, min(2.0, float(timeout or 0.9))))
        packet = bytearray(70)
        packet[0:4] = int(ssrc).to_bytes(4, "big", signed=False)
        sock.sendto(packet, (str(ip), int(port)))
        data, _addr = sock.recvfrom(70)
        if len(data) >= 70:
            discovered_ip = data[4:68].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            discovered_port = int.from_bytes(data[68:70], "little", signed=False)
            result.update({"ok": bool(discovered_ip and discovered_port), "ip": discovered_ip, "port": discovered_port})
        else:
            result["error"] = f"resposta UDP curta: {len(data)} bytes"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
    return result


async def _voice_agent_probe_connection_async(handoff: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        import aiohttp  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"aiohttp indisponível para voice dry-run: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc

    guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
    bot_user_id = _voice_agent_clean_text(handoff.get("bot_user_id"), limit=80)
    session_id = str(handoff.get("session_id") or "").strip()
    token = str(handoff.get("voice_token") or "").strip()
    endpoint_host, ws_url = _voice_agent_normalize_endpoint(str(handoff.get("endpoint") or handoff.get("endpoint_host") or ""))
    if guild_id <= 0 or not bot_user_id or not session_id or not token:
        raise RuntimeError("handoff incompleto para conexão: guild_id/bot_user_id/session_id/token")

    started = time.perf_counter()
    _voice_agent_set_connection(
        guild_id,
        channel_id=str(handoff.get("channel_id") or ""),
        state="voice_ws_connecting",
        stage="ws_connect",
        dry_run=True,
        ws_url_present=True,
        endpoint_host=endpoint_host,
        error="",
    )
    hello_received = False
    ready_received = False
    ready_payload: dict[str, Any] = {}
    udp_result: dict[str, Any] = {"attempted": False, "ok": False}
    client_timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds or 4.0)))
    async with aiohttp.ClientSession(timeout=client_timeout, headers={"User-Agent": f"CorePhoneWorkerVoiceAgent/{PHONE_WORKER_VERSION}"}) as session:
        async with session.ws_connect(ws_url, timeout=max(1.0, min(8.0, float(timeout_seconds or 4.0))), heartbeat=None) as ws:
            _voice_agent_set_connection(guild_id, state="voice_ws_identifying", stage="identify", endpoint_host=endpoint_host)
            await ws.send_json({
                "op": 0,
                "d": {
                    "server_id": str(guild_id),
                    "user_id": str(bot_user_id),
                    "session_id": session_id,
                    "token": token,
                },
            })
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 4.0))
            while time.monotonic() < deadline:
                remaining = max(0.1, min(1.0, deadline - time.monotonic()))
                msg = await ws.receive(timeout=remaining)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data or "{}")
                    except Exception:
                        continue
                    op = payload.get("op")
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                    if op == 8:
                        hello_received = True
                        interval = int(float(data.get("heartbeat_interval") or 0)) if data else 0
                        _voice_agent_set_connection(guild_id, state="voice_ws_hello", stage="hello", hello_received=True, heartbeat_interval_ms=interval)
                        # O dry-run não mantém sessão viva, mas manda um heartbeat curto para validar a ida/volta básica.
                        with contextlib.suppress(Exception):
                            await ws.send_json({"op": 3, "d": int(time.time() * 1000)})
                    elif op == 2:
                        ready_received = True
                        ready_payload = dict(data)
                        ssrc = _voice_agent_int(data.get("ssrc"), 0)
                        voice_ip = str(data.get("ip") or "").strip()
                        voice_port = _voice_agent_int(data.get("port"), 0)
                        modes = data.get("modes") if isinstance(data.get("modes"), list) else []
                        selected_protocol_ready = bool(voice_ip and voice_port and ssrc and modes)
                        _voice_agent_set_connection(
                            guild_id,
                            state="voice_ws_ready",
                            stage="ready",
                            ready_received=True,
                            ssrc_present=bool(ssrc),
                            selected_protocol_ready=selected_protocol_ready,
                            voice_ip=voice_ip,
                            voice_port=voice_port,
                            modes=[str(item)[:80] for item in modes[:8]],
                        )
                        udp_result = _voice_agent_udp_discovery_probe(ip=voice_ip, port=voice_port, ssrc=ssrc, timeout=max(0.2, min(1.2, float(timeout_seconds or 4.0) / 3.0)))
                        break
                    elif op == 6:
                        _voice_agent_set_connection(guild_id, heartbeat_ack=True)
                    elif op == 9:
                        raise RuntimeError("Voice WS invalid session no dry-run")
                elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                    break
            with contextlib.suppress(Exception):
                await ws.close(code=1000, message=b"Core Worker voice dry-run complete")

    if not ready_received:
        raise RuntimeError("Voice WS não retornou READY no dry-run")
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    final = _voice_agent_set_connection(
        guild_id,
        state="connected_dry_run",
        stage="closed_after_probe",
        hello_received=hello_received,
        ready_received=ready_received,
        connected_once=True,
        closed_after_probe=True,
        udp_probe_attempted=bool(udp_result.get("attempted")),
        udp_probe_ok=bool(udp_result.get("ok")),
        udp_probe_error=str(udp_result.get("error") or "")[:160],
        external_ip=str(udp_result.get("ip") or "")[:80],
        external_port=_voice_agent_int(udp_result.get("port"), 0),
        latency_ms=elapsed_ms,
        error="",
    )
    return _voice_agent_public_connection(final)


def _voice_agent_probe_worker(handoff: dict[str, Any], *, timeout_seconds: float) -> None:
    guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
    try:
        result = asyncio.run(_voice_agent_probe_connection_async(handoff, timeout_seconds=timeout_seconds))
        _voice_agent_set_connection(guild_id, **{k: v for k, v in result.items() if k not in {"guild_id"}})
    except Exception as exc:
        _voice_agent_set_connection(
            guild_id,
            state="connection_failed",
            stage="failed",
            dry_run=True,
            connected_once=False,
            error=f"{type(exc).__name__}: {_short_text(exc, limit=180)}",
        )


def _voice_agent_start_connection_probe(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED", True):
        raise RuntimeError("voice connection dry-run desativado")
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    handoffs = _voice_agent_prune_handoffs()
    handoff = handoffs.get(str(guild_id)) if guild_id > 0 else None
    if not handoff:
        # Se o chamador acabou de mandar um handoff embutido, aceita sem exigir registro prévio.
        if isinstance(body.get("discord_voice_handoff"), dict):
            handoff = _voice_agent_register_handoff_payload(body)
            with _VOICE_AGENT_SESSION_LOCK:
                _VOICE_AGENT_HANDOFF_MEMORY[str(handoff["guild_id"])] = handoff
        else:
            raise RuntimeError("handoff temporário de voz não encontrado para a guild")
    guild_id = _voice_agent_int(handoff.get("guild_id"), 0)
    force = bool(body.get("force") or body.get("manual") or body.get("diagnostic"))
    allow_probe = bool(body.get("allow_probe") or body.get("allow_connection_probe") or handoff.get("allow_connection_probe") or handoff.get("allow_probe"))
    owner = str(handoff.get("voice_owner") or handoff.get("transport_owner") or "vps").strip().lower() or "vps"
    if owner != "worker":
        blocked = _voice_agent_set_connection(
            guild_id,
            channel_id=str(handoff.get("channel_id") or ""),
            state="waiting_for_voice_ownership",
            stage="handoff_received",
            dry_run=True,
            connected_once=False,
            endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
            error="dono atual da voz é a VPS; probe direto não iniciado",
        )
        return {
            "ok": True,
            "started": False,
            "blocked": True,
            "state": "waiting_for_explicit_voice_ownership_transfer",
            "reason": "vps_voice_owner_requires_explicit_begin_transfer",
            "connection": _voice_agent_public_connection(blocked),
            **_voice_agent_connection_summary(guild_id=guild_id, limit=5),
        }
    if not allow_probe:
        blocked = _voice_agent_set_connection(
            guild_id,
            channel_id=str(handoff.get("channel_id") or ""),
            state="connection_probe_blocked",
            stage="probe_not_authorized",
            dry_run=True,
            connected_once=False,
            endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
            error="probe de conexão exige allow_probe/force explícito",
        )
        return {"ok": True, "started": False, "blocked": True, "state": "connection_probe_blocked", "reason": "probe_not_authorized", "connection": _voice_agent_public_connection(blocked), **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}
    timeout_seconds = max(1.0, min(10.0, float(body.get("timeout_seconds") or _env_float("PHONE_WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS", 4.0))))
    existing = _voice_agent_connection_summary(guild_id=guild_id, limit=1).get("last_connection") or {}
    if existing.get("state") in {"probing", "connecting", "voice_ws_connecting", "voice_ws_identifying"} and float(existing.get("updated_age_seconds") or 999.0) < 8.0:
        return {"ok": True, "started": False, "state": "connection_probe_already_running", **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}
    _voice_agent_set_connection(
        guild_id,
        channel_id=str(handoff.get("channel_id") or ""),
        state="probing",
        stage="scheduled",
        dry_run=True,
        connected_once=False,
        endpoint_host=_voice_agent_clean_text(handoff.get("endpoint_host") or handoff.get("endpoint"), limit=160),
        error="",
    )
    thread = threading.Thread(target=_voice_agent_probe_worker, kwargs={"handoff": dict(handoff), "timeout_seconds": timeout_seconds}, name=f"voice-agent-probe-{guild_id}", daemon=True)
    thread.start()
    return {"ok": True, "started": True, "state": "connection_probe_started", **_voice_agent_connection_summary(guild_id=guild_id, limit=5)}


def _voice_agent_clear_connection(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    removed = False
    with _VOICE_AGENT_SESSION_LOCK:
        if guild_id > 0:
            removed = _VOICE_AGENT_CONNECTION_MEMORY.pop(str(guild_id), None) is not None
        elif body.get("all"):
            removed = bool(_VOICE_AGENT_CONNECTION_MEMORY)
            _VOICE_AGENT_CONNECTION_MEMORY.clear()
    return {"ok": True, "cleared": removed, "state": "connection_cleared" if removed else "connection_not_found", **_voice_agent_connection_summary(limit=5)}

def _voice_agent_register_handoff_payload(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("discord_voice_handoff") if isinstance(body.get("discord_voice_handoff"), dict) else body
    guild_id = _voice_agent_int(body.get("guild_id") or raw.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id") or raw.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para handoff de voz")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_HANDOFF_ENABLED", True):
        raise RuntimeError("handoff do Worker Voice Agent desativado")
    ttl_seconds = max(10, min(180, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS", 60))))
    now_ms = _voice_agent_now_ms()
    session_id = _voice_agent_clean_text(raw.get("session_id"), limit=220)
    endpoint = _voice_agent_clean_text(raw.get("endpoint"), limit=220)
    token = str(raw.get("voice_token") or raw.get("token") or "").strip()
    if not (session_id and endpoint and token):
        raise RuntimeError("handoff incompleto: session_id/endpoint/voice_token obrigatórios")
    return {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "text_channel_id": str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        "requester_id": str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        "bot_user_id": str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        "source": _voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        "state": _voice_agent_clean_text(body.get("state") or "voice_handoff_registered_dry_run", limit=60) or "voice_handoff_registered_dry_run",
        "registered_by": _voice_agent_clean_text(body.get("registered_by") or "vps_control_plane", limit=60) or "vps_control_plane",
        "dry_run": bool(body.get("dry_run", True)),
        "voice_owner": str(body.get("voice_owner") or body.get("transport_owner") or "vps")[:40],
        "transport_owner": str(body.get("transport_owner") or body.get("voice_owner") or "vps")[:40],
        "allow_connection_probe": bool(body.get("allow_connection_probe") or body.get("allow_probe")),
        "connection_policy": str(body.get("connection_policy") or "handoff_only_wait_for_voice_ownership")[:80],
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_seconds * 1000,
        # Dados temporários necessários para a futura conexão Voice WS/UDP.
        # Eles ficam só em memória, nunca no arquivo voice-agent-state.json e nunca aparecem no painel.
        "session_id": session_id,
        "endpoint": endpoint,
        "endpoint_host": endpoint,
        "voice_token": token,
    }


def _voice_agent_register_handoff(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    handoff = _voice_agent_register_handoff_payload(body)
    with _VOICE_AGENT_SESSION_LOCK:
        _VOICE_AGENT_HANDOFF_MEMORY[str(handoff["guild_id"])] = handoff
    return {
        "ok": True,
        "registered": True,
        "state": "voice_handoff_registered_dry_run",
        "handoff": _voice_agent_public_handoff(handoff),
        **_voice_agent_handoff_summary(guild_id=_voice_agent_int(handoff.get("guild_id"), 0), limit=5),
    }


def _voice_agent_clear_handoff(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    removed = False
    with _VOICE_AGENT_SESSION_LOCK:
        if guild_id > 0:
            removed = _VOICE_AGENT_HANDOFF_MEMORY.pop(str(guild_id), None) is not None
        elif body.get("all"):
            removed = bool(_VOICE_AGENT_HANDOFF_MEMORY)
            _VOICE_AGENT_HANDOFF_MEMORY.clear()
    return {
        "ok": True,
        "cleared": removed,
        "state": "voice_handoff_cleared" if removed else "voice_handoff_not_found",
        "reason": _voice_agent_clean_text(body.get("reason"), limit=120),
        **_voice_agent_handoff_summary(limit=5),
    }


def _voice_agent_prune_sessions(state: dict[str, Any] | None = None) -> dict[str, Any]:
    now_ms = _voice_agent_now_ms()
    state = state or _voice_agent_load_state()
    sessions = state.setdefault("sessions", {})
    stale = [key for key, data in sessions.items() if _voice_agent_int(data.get("expires_at_ms"), 0) and _voice_agent_int(data.get("expires_at_ms"), 0) <= now_ms]
    for key in stale:
        sessions.pop(key, None)
    if stale:
        _voice_agent_save_state(state)
    return state


def _voice_agent_session_summary(*, guild_id: int | None = None, limit: int = 5) -> dict[str, Any]:
    state = _voice_agent_prune_sessions()
    now_ms = _voice_agent_now_ms()
    sessions_dict = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    sessions = []
    for key, raw in sessions_dict.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        if isinstance(raw, dict):
            sessions.append(_voice_agent_public_session(raw, now_ms=now_ms))
    sessions.sort(key=lambda item: float(item.get("age_seconds", 999999) or 999999))
    return {
        "session_count": len(sessions),
        "active_guilds": [str(item.get("guild_id") or "") for item in sessions[:12] if item.get("guild_id")],
        "sessions": sessions[:limit],
        "last_session": sessions[0] if sessions else {},
        "state_file": str(_voice_agent_state_file()),
    }


def _voice_agent_register_session_payload(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    channel_id = _voice_agent_int(body.get("channel_id"), 0)
    if guild_id <= 0 or channel_id <= 0:
        raise RuntimeError("guild_id/channel_id obrigatórios para registrar sessão de voz")
    ttl_seconds = max(30, min(900, _voice_agent_int(body.get("expires_in_seconds"), _env_int("PHONE_WORKER_VOICE_AGENT_SESSION_TTL_SECONDS", 180))))
    now_ms = _voice_agent_now_ms()
    raw_voice = body.get("discord_voice") if isinstance(body.get("discord_voice"), dict) else {}
    voice = {
        "connected": bool(raw_voice.get("connected")),
        "channel_id": _voice_agent_int(raw_voice.get("channel_id"), channel_id),
        "session_id_present": bool(raw_voice.get("session_id_present")),
        "endpoint_present": bool(raw_voice.get("endpoint_present")),
        "endpoint_host": _voice_agent_clean_text(raw_voice.get("endpoint_host"), limit=160),
        "voice_token_present": bool(raw_voice.get("voice_token_present")),
        "self_deaf": raw_voice.get("self_deaf") if isinstance(raw_voice.get("self_deaf"), bool) else None,
        "self_mute": raw_voice.get("self_mute") if isinstance(raw_voice.get("self_mute"), bool) else None,
    }
    return {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "text_channel_id": str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
        "requester_id": str(_voice_agent_int(body.get("requester_id"), 0) or ""),
        "bot_user_id": str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
        "source": _voice_agent_clean_text(body.get("source") or "tts", limit=40) or "tts",
        "state": _voice_agent_clean_text(body.get("state") or "registered", limit=60) or "registered",
        "registered_by": _voice_agent_clean_text(body.get("registered_by") or "vps_control_plane", limit=60) or "vps_control_plane",
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_seconds * 1000,
        "direct_tts_enabled": bool(body.get("direct_tts_enabled")),
        "discord_voice": voice,
        "note": "sem DISCORD_TOKEN e sem voice token bruto",
    }


def _voice_agent_register_session(body: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
        raise RuntimeError("Worker Voice Agent desativado")
    if not _env_bool("PHONE_WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", True):
        raise RuntimeError("sessão compartilhada do Worker Voice Agent desativada")
    session = _voice_agent_register_session_payload(body)
    state = _voice_agent_prune_sessions()
    sessions = state.setdefault("sessions", {})
    sessions[str(session["guild_id"])] = session
    _voice_agent_save_state(state)
    summary = _voice_agent_session_summary(guild_id=_voice_agent_int(session.get("guild_id"), 0), limit=5)
    return {
        "ok": True,
        "registered": True,
        "state": "session_registered",
        "session": _voice_agent_public_session(session),
        **summary,
    }


def _voice_agent_clear_session(body: dict[str, Any]) -> dict[str, Any]:
    guild_id = _voice_agent_int(body.get("guild_id"), 0)
    state = _voice_agent_prune_sessions()
    sessions = state.setdefault("sessions", {})
    removed = False
    if guild_id > 0:
        removed = sessions.pop(str(guild_id), None) is not None
    elif body.get("all"):
        removed = bool(sessions)
        sessions.clear()
    _voice_agent_save_state(state)
    summary = _voice_agent_session_summary(limit=5)
    return {
        "ok": True,
        "cleared": removed,
        "state": "session_cleared" if removed else "session_not_found",
        "reason": _voice_agent_clean_text(body.get("reason"), limit=120),
        **summary,
    }

def _voice_agent_snapshot(*, music_agent: dict[str, Any] | None = None, tts_agent: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shared worker voice/audio plane readiness.

    This does not make the worker own the whole bot. It reports whether the
    worker is prepared to become the direct voice/audio plane for Music + TTS
    while the VPS remains the control plane/gateway/UI owner.
    """
    profile = _current_core_worker_profile()
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    enabled = _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True)
    shared_session_enabled = _env_bool("PHONE_WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", True)
    direct_tts_enabled = _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", True)
    direct_music_enabled = _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_MUSIC_ENABLED", True)
    safe_mode = _phone_worker_safe_mode_enabled()

    if music_agent is None:
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
    if tts_agent is None:
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})

    music_ready = bool((music_agent or {}).get("ok") or (music_agent or {}).get("available") or (music_agent or {}).get("discord_ready"))
    tts_ready = bool((tts_agent or {}).get("ok") and (tts_agent or {}).get("synth_ready"))
    has_voice_capability = "voice-agent" in capabilities or "worker-voice" in capabilities or ("music" in capabilities and "tts-agent" in capabilities)
    base_ready = bool(enabled and profile == "turbo" and has_voice_capability and not safe_mode)
    session_summary = _voice_agent_session_summary(limit=5)
    handoff_summary = _voice_agent_handoff_summary(limit=5)
    connection_summary = _voice_agent_connection_summary(limit=5)
    transfer_summary = _voice_agent_transfer_summary(limit=5)
    session_count = int(session_summary.get("session_count") or 0)
    handoff_count = int(handoff_summary.get("handoff_count") or 0)
    handoff_ready = bool(handoff_summary.get("handoff_ready"))
    connection_ready = bool(connection_summary.get("connection_ready"))
    transfer_ready = bool(transfer_summary.get("transfer_ready"))
    transfer_count = int(transfer_summary.get("transfer_count") or 0)
    shared_ready = bool(base_ready and shared_session_enabled and (music_ready or direct_music_enabled) and tts_ready)
    shared_session_ready = bool(shared_ready and session_count > 0)
    last_connection = dict(connection_summary.get("last_connection") or {})
    direct_connection_ready = bool(connection_ready and str(last_connection.get("state") or "").startswith("worker_direct_tts"))
    direct_tts_ready = bool(direct_tts_enabled and music_ready and tts_ready and (direct_connection_ready or (shared_session_ready and handoff_ready and connection_ready)))

    missing: list[str] = []
    if not enabled:
        missing.append("PHONE_WORKER_VOICE_AGENT_ENABLED=false")
    if profile != "turbo":
        missing.append("perfil turbo")
    if safe_mode:
        missing.append("safe mode")
    if not has_voice_capability:
        missing.append("capacidade voice-agent/worker-voice")
    if not shared_session_enabled:
        missing.append("sessão compartilhada desativada")
    if not tts_ready:
        missing.append("TTS Agent pronto")
    if not music_ready:
        missing.append("Music Agent/voz pronta")
    if shared_ready and session_count <= 0:
        missing.append("sessão de voz registrada pela VPS")
    if shared_session_ready and not handoff_ready:
        missing.append("handoff temporário de voz")
    if shared_session_ready and handoff_ready and transfer_count <= 0:
        missing.append("preparação de transferência de posse")
    if shared_session_ready and handoff_ready and transfer_count > 0 and not transfer_ready:
        missing.append("transferência explícita de posse da voz")
    if shared_session_ready and handoff_ready and transfer_ready and not connection_ready and not direct_connection_ready:
        missing.append("conexão worker autorizada ainda não testada")

    if direct_tts_ready:
        state = "direct_tts_voice_ready"
    elif shared_session_ready and handoff_ready and connection_ready:
        state = "voice_connection_dry_run_ready"
    elif shared_session_ready and handoff_ready and transfer_ready:
        state = "voice_ownership_granted_waiting_connection"
    elif shared_session_ready and handoff_ready and transfer_count > 0:
        state = "voice_transfer_staged_waiting_vps_release"
    elif shared_session_ready and handoff_ready:
        state = "voice_handoff_received_waiting_transfer"
    elif shared_session_ready:
        state = "shared_voice_session_registered"
    elif shared_ready:
        state = "waiting_shared_voice_session"
    elif base_ready:
        state = "waiting_dependencies"
    elif enabled:
        state = "not_ready"
    else:
        state = "disabled"

    return {
        "ok": bool(shared_ready),
        "available": bool(base_ready),
        "state": state,
        "enabled": bool(enabled),
        "profile": profile,
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "worker_version": PHONE_WORKER_VERSION,
        "control_plane": "vps",
        "audio_plane": "worker",
        "authority": "vps_control_plane_worker_audio_plane",
        "shared_session_enabled": bool(shared_session_enabled),
        "shared_session_ready": bool(shared_session_ready),
        "session_count": session_count,
        "active_guilds": list(session_summary.get("active_guilds") or [])[:12],
        "sessions": list(session_summary.get("sessions") or [])[:5],
        "last_session": dict(session_summary.get("last_session") or {}),
        "handoff_count": handoff_count,
        "handoff_complete_count": int(handoff_summary.get("handoff_complete_count") or 0),
        "handoff_ready": bool(handoff_ready),
        "handoff_guilds": list(handoff_summary.get("handoff_guilds") or [])[:12],
        "handoffs": list(handoff_summary.get("handoffs") or [])[:5],
        "last_handoff": dict(handoff_summary.get("last_handoff") or {}),
        "connection_count": int(connection_summary.get("connection_count") or 0),
        "connection_ready_count": int(connection_summary.get("connection_ready_count") or 0),
        "connection_probing_count": int(connection_summary.get("connection_probing_count") or 0),
        "connection_failed_count": int(connection_summary.get("connection_failed_count") or 0),
        "connection_ready": bool(connection_summary.get("connection_ready")),
        "connection_guilds": list(connection_summary.get("connection_guilds") or [])[:12],
        "connections": list(connection_summary.get("connections") or [])[:5],
        "last_connection": dict(connection_summary.get("last_connection") or {}),
        "transfer_count": transfer_count,
        "transfer_ready_count": int(transfer_summary.get("transfer_ready_count") or 0),
        "transfer_staged_count": int(transfer_summary.get("transfer_staged_count") or 0),
        "transfer_ready": bool(transfer_ready),
        "transfer_state": str(transfer_summary.get("transfer_state") or "")[:80],
        "current_voice_owner": str(transfer_summary.get("current_voice_owner") or "vps")[:40],
        "requested_voice_owner": str(transfer_summary.get("requested_voice_owner") or "")[:40],
        "transfer_guilds": list(transfer_summary.get("transfer_guilds") or [])[:12],
        "transfers": list(transfer_summary.get("transfers") or [])[:5],
        "last_transfer": dict(transfer_summary.get("last_transfer") or {}),
        "direct_tts_enabled": bool(direct_tts_enabled),
        "direct_tts_ready": bool(direct_tts_ready),
        "direct_music_enabled": bool(direct_music_enabled),
        "connection_auto_probe_enabled": bool(_env_bool("PHONE_WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED", False)),
        "music_ready": bool(music_ready),
        "tts_ready": bool(tts_ready),
        "music_state": str((music_agent or {}).get("state") or (music_agent or {}).get("status") or "unknown")[:80],
        "tts_state": str((tts_agent or {}).get("state") or "unknown")[:80],
        "voice_transport": "worker_shared_voice_session" if shared_session_ready else ("music_agent_shared_session" if music_ready else "not_connected"),
        "ducking_ready": bool(shared_session_ready and music_ready and tts_ready),
        "missing": missing[:10],
        "note": "Base do Worker Voice Agent: VPS segue como cérebro; worker vira plano de voz/áudio quando a etapa direta for ativada.",
    }

def _system_status() -> dict[str, Any]:
    auto_boot_repair = _auto_repair_local_boot_if_needed()
    disk = shutil.disk_usage(Path.home())
    load = None
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    music_agent_snapshot = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
    tts_agent_snapshot = _safe_telemetry("tts agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False, "state": "telemetry_failed"})
    voice_agent_snapshot = _safe_telemetry(
        "voice agent",
        lambda: _voice_agent_snapshot(music_agent=music_agent_snapshot, tts_agent=tts_agent_snapshot),
        {"ok": False, "available": False, "state": "telemetry_failed"},
    )
    return {
        "ok": True,
        "worker": "phone-worker",
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "physical_worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "runtime": {
            "mode": CORE_WORKER_RUNTIME_MODE,
            "current_worker": "termux-phone-worker",
            "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
            "migration_stage": "termux-bootstrap-builder",
        },
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["inline-b64-v1", "zip-v1"],
        "profile": _current_core_worker_profile(),
        "profile_label": _core_worker_profile_label(_current_core_worker_profile()),
        "core_worker_heartbeat": _heartbeat_configured(),
        "core_worker_jobs": {"configured": _core_worker_jobs_configured(), **_core_job_runtime_snapshot()},
        "core_worker_network": _core_worker_network_runtime_snapshot(),
        "music_node": _safe_telemetry("music_node", _music_node_snapshot, {"ok": False, "online": False, "state": "unknown"}),
        "music_agent": music_agent_snapshot,
        "voice_agent": voice_agent_snapshot,
        "music_voice_dependencies": _safe_telemetry("music voice dependencies", _music_voice_dependencies_snapshot, {"ok": False, "missing": ["unknown"]}),
        "scripts": _script_inventory(),
        "boot": _safe_telemetry("boot", _termux_boot_status_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "shell_autostart": _safe_telemetry("shell autostart", _termux_shell_autostart_status_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "auto_boot_repair": auto_boot_repair,
        "supervisor": _safe_telemetry("supervisor", _runtime_supervisor_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "sshd": _safe_telemetry("sshd", _sshd_snapshot, {"ok": False, "source": "telemetry_failed"}),
        "supported_tasks": list(SUPPORTED_DIRECT_TASKS),
        "supported_core_worker_jobs": _supported_core_worker_job_types(),
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - START_TIME, 3),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "jobs_started": JOBS_STARTED,
        "jobs_failed": JOBS_FAILED,
        "loadavg": list(load) if load else None,
        "disk_home": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
        },
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "turbo_dependencies": _turbo_dependency_snapshot(),
        "tts_agent": tts_agent_snapshot,
        "turbo_cache": _worker_turbo_cache_snapshot(),
    }


def _local_agent_status_payload(*, host: str, port: int) -> dict[str, Any]:
    profile = _current_core_worker_profile()
    safe_mode = _phone_worker_safe_mode_enabled()
    status = _safe_telemetry("system", _system_status, {"ok": False})
    roles = _env_list("CORE_WORKER_ROLES", _core_worker_profile_roles(profile))
    capabilities = _env_list("CORE_WORKER_CAPABILITIES", _core_worker_profile_capabilities(profile))
    if safe_mode:
        roles = [item for item in roles if not str(item).lower().startswith("music")]
        capabilities = [item for item in capabilities if not str(item).lower().startswith("music")]
    return {
        "ok": True,
        "local_only": True,
        "worker": "phone-worker",
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "physical_worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "runtime_mode": CORE_WORKER_RUNTIME_MODE,
        "runtime": {
            "mode": CORE_WORKER_RUNTIME_MODE,
            "current_worker": "termux-phone-worker",
            "internal_runtime": CORE_WORKER_INTERNAL_RUNTIME_STATE,
            "migration_stage": "termux-bootstrap-builder",
        },
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
        "name": _default_worker_name(),
        "version": PHONE_WORKER_VERSION,
        "source_hash": _phone_worker_source_hash(),
        "worker_update_transports": ["inline-b64-v1", "zip-v1"],
        "profile": profile,
        "profile_label": _core_worker_profile_label(profile),
        "safe_mode": safe_mode,
        "roles": roles[:16],
        "capabilities": capabilities[:24],
        "supported_tasks": _supported_core_worker_job_types(),
        "vps_configured": _heartbeat_configured(),
        "jobs_configured": _core_worker_jobs_configured(),
        "vps_url": str(os.getenv("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_BASE_URL") or "").strip(),
        "pid": os.getpid(),
        "uptime_seconds": status.get("uptime_seconds"),
        "endpoint": f"http://127.0.0.1:{port}",
        "bind_host": host,
        "bind_port": port,
        "ffmpeg": bool(status.get("ffmpeg")),
        "ffprobe": bool(status.get("ffprobe")),
        "tts_agent": status.get("tts_agent") if isinstance(status.get("tts_agent"), dict) else _safe_telemetry("tts agent", _tts_agent_snapshot, {"ok": False}),
        "voice_agent": status.get("voice_agent") if isinstance(status.get("voice_agent"), dict) else _safe_telemetry("voice agent", _voice_agent_snapshot, {"ok": False}),
        "boot_ok": ((status.get("boot") or {}).get("ok") if isinstance(status.get("boot"), dict) else None),
        "supervisor_ok": ((status.get("supervisor") or {}).get("supervisor_ok") if isinstance(status.get("supervisor"), dict) else None),
        "sshd_ok": ((status.get("sshd") or {}).get("ok") if isinstance(status.get("sshd"), dict) else None),
        "sshd_summary": ((status.get("sshd") or {}).get("summary") if isinstance(status.get("sshd"), dict) else None),
        "shell_autostart_ok": ((status.get("shell_autostart") or {}).get("ok") if isinstance(status.get("shell_autostart"), dict) else None),
        "shell_autostart_summary": ((status.get("shell_autostart") or {}).get("summary") if isinstance(status.get("shell_autostart"), dict) else None),
        "note": "Rota local para o APK Core Worker; Termux segue como runtime oficial nesta etapa e não expõe token.",
    }


def _apply_local_core_worker_profile(profile: Any) -> dict[str, Any]:
    normalized = _normalize_core_worker_profile(profile)
    roles = _core_worker_profile_roles(normalized)
    capabilities = _core_worker_profile_capabilities(normalized)
    env_path = _update_env_file(None, {
        "CORE_WORKER_PROFILE": normalized,
        "CORE_WORKER_ROLES": ",".join(roles),
        "CORE_WORKER_CAPABILITIES": ",".join(capabilities),
    })
    return {
        "ok": True,
        "saved": True,
        "profile": normalized,
        "profile_label": _core_worker_profile_label(normalized),
        "roles": roles,
        "capabilities": capabilities,
        "env_updated": True,
        "env_file": str(env_path),
    }


class WorkerHandler(BaseHTTPRequestHandler):
    server_version = "PhoneWorker/1.2"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet default HTTP noise
        if _env_bool("PHONE_WORKER_HTTP_LOGS", False):
            super().log_message(fmt, *args)

    @property
    def token(self) -> str:
        return str(getattr(self.server, "worker_token", "") or "")

    @property
    def max_body_bytes(self) -> int:
        return int(getattr(self.server, "max_body_bytes", DEFAULT_MAX_BODY_MB * 1024 * 1024))

    @property
    def max_output_bytes(self) -> int:
        return int(getattr(self.server, "max_output_bytes", DEFAULT_MAX_OUTPUT_MB * 1024 * 1024))

    @property
    def job_timeout(self) -> int:
        return int(getattr(self.server, "job_timeout", DEFAULT_TIMEOUT_SECONDS))

    def _authorized(self) -> bool:
        expected = self.token
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        custom = self.headers.get("X-Phone-Worker-Token", "")
        return auth == f"Bearer {expected}" or custom == expected

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        _error(self, HTTPStatus.FORBIDDEN, "token inválido")
        return False

    def _is_local_client(self) -> bool:
        host = str((self.client_address or ("", 0))[0] or "").strip().lower()
        return host == "::1" or host == "localhost" or host.startswith("127.")

    def _require_local_client(self) -> bool:
        if self._is_local_client():
            return True
        _error(self, HTTPStatus.FORBIDDEN, "rota local disponível apenas em 127.0.0.1")
        return False

    def _bind_host_port(self) -> tuple[str, int]:
        host = str(getattr(self.server, "phone_worker_host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(self.server, "phone_worker_port", 8766) or 8766)
        return host, port

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            _error(self, HTTPStatus.LENGTH_REQUIRED, "Content-Length inválido")
            return None
        if length <= 0:
            return {}
        if length > self.max_body_bytes:
            _error(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "requisição grande demais")
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            _error(self, HTTPStatus.BAD_REQUEST, f"JSON inválido: {type(exc).__name__}")
            return None
        if not isinstance(parsed, dict):
            _error(self, HTTPStatus.BAD_REQUEST, "JSON precisa ser objeto")
            return None
        return parsed

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/music/stream/"):
            if not self._require_auth():
                return
            stream_id = path.rsplit("/", 1)[-1]
            _stream_music_pcm(self, stream_id)
            return
        if path == "/local/status":
            if not self._require_local_client():
                return
            host, port = self._bind_host_port()
            _json_response(self, HTTPStatus.OK, _local_agent_status_payload(host=host, port=port))
            return
        if path in {"/tts-agent/health", "/tts-agent/status"}:
            if not self._require_auth():
                return
            try:
                agent = self._task_tts_agent_status({})
                payload = {
                    "ok": bool(agent.get("ok")),
                    "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
                    "version": PHONE_WORKER_VERSION,
                    "tts_agent": agent,
                }
                _json_response(self, HTTPStatus.OK, payload)
            except Exception as exc:
                _error(self, HTTPStatus.SERVICE_UNAVAILABLE, f"{type(exc).__name__}: {_short_text(exc, limit=180)}")
            return
        if path not in {"/", "/health", "/status"}:
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        _json_response(self, HTTPStatus.OK, _system_status())

    def do_POST(self) -> None:
        global JOBS_STARTED, JOBS_FAILED
        path = urllib.parse.urlparse(self.path).path
        if path == "/local/pair":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                host, port = self._bind_host_port()
                profile_result = _apply_local_core_worker_profile(body.get("profile"))
                name = _short_text(body.get("name") or body.get("device_name") or _default_worker_name(), limit=64, default="Core Phone Worker")
                pair_result = _pair_core_worker(
                    code=str(body.get("code") or ""),
                    vps_url=str(body.get("vps_url") or body.get("server_url") or ""),
                    host=host,
                    port=port,
                    worker_id=str(os.getenv("CORE_WORKER_ID") or _default_worker_id()),
                    name=name,
                    roles=",".join(profile_result.get("roles") or []),
                    capabilities=",".join(profile_result.get("capabilities") or []),
                    env_file=None,
                    timeout=10.0,
                )
                if not pair_result.get("ok"):
                    _json_response(self, HTTPStatus.BAD_REQUEST, pair_result)
                    return
                heartbeat_ok = _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(6.0, min(20.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 12.0))))
                result = _local_agent_status_payload(host=host, port=port)
                result.update(pair_result)
                result["profile"] = profile_result.get("profile")
                result["profile_label"] = profile_result.get("profile_label")
                result["roles"] = profile_result.get("roles") or result.get("roles")
                result["capabilities"] = profile_result.get("capabilities") or result.get("capabilities")
                result["synced_to_vps"] = bool(heartbeat_ok)
                result["message"] = "worker local pareado; o APK não criou registro separado"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path == "/local/heartbeat":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                host, port = self._bind_host_port()
                result = _local_agent_status_payload(host=host, port=port)
                result["synced_to_vps"] = _send_core_worker_heartbeat_once(host=host, port=port, timeout=max(6.0, min(20.0, _env_float("CORE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 12.0)))) if _heartbeat_configured() else False
                result["message"] = "heartbeat solicitado ao worker local"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path == "/local/profile":
            if not self._require_local_client():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                result = _apply_local_core_worker_profile(body.get("profile"))
                host, port = self._bind_host_port()
                result.update(_local_agent_status_payload(host=host, port=port))
                result["synced_to_vps"] = _send_core_worker_heartbeat_once(host=host, port=port, timeout=5.0) if _heartbeat_configured() else False
                result["message"] = "perfil atualizado no worker local"
                _json_response(self, HTTPStatus.OK, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
            return
        if path == "/tts-agent/synthesize.raw":
            if not self._require_auth():
                return
            body = self._read_json()
            if body is None:
                return
            try:
                result = self._task_tts_agent_synthesize(body, raw_response=True)
                max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
                raw = result.pop("_raw_audio", None)
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    raw = _b64decode(str(result.get("data_b64") or ""), max_bytes=max_audio_bytes)
                if not raw:
                    raise RuntimeError("TTS Agent não retornou áudio")
                raw = bytes(raw)
                result["worker_id"] = str(result.get("worker_id") or os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip()
                result["worker_version"] = str(result.get("worker_version") or PHONE_WORKER_VERSION)
                _audio_response(self, HTTPStatus.OK, raw, result)
            except Exception as exc:
                _error(self, HTTPStatus.BAD_GATEWAY, f"{type(exc).__name__}: {_short_text(exc, limit=220)}")
            return
        if path != "/task":
            _error(self, HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        if not self._require_auth():
            return
        body = self._read_json()
        if body is None:
            return

        task = str(body.get("task") or "").strip().lower().replace("-", "_")
        JOBS_STARTED += 1
        try:
            if task in {"ping", "health", "status"}:
                payload = _system_status()
                payload.setdefault("summary", "status direto coletado")
            elif task in {"diagnostic_basic", "worker_self_check"}:
                payload = _execute_core_worker_job({"type": "worker_self_check", "payload": body}, max_body_bytes=self.max_body_bytes, max_output_bytes=self.max_output_bytes, job_timeout=self.job_timeout)
            elif task in {"network_probe", "tailscale_status", "worker_logs", "worker_update", "apk_build_debug", "vps_assist_probe", "hash_batch", "endpoint_probe", "media_probe", "audio_convert", "log_digest", "zip_audit", "boot_status", "boot_repair", "service_status", "service_start", "service_stop", "service_restart", "ffmpeg_check", "ffprobe_check"}:
                payload = _execute_core_worker_job({"type": task, "payload": body}, max_body_bytes=self.max_body_bytes, max_output_bytes=self.max_output_bytes, job_timeout=self.job_timeout)
            elif task == "sha256":
                payload = self._task_sha256(body)
            elif task == "zip":
                payload = self._task_zip(body)
            elif task == "zip_validate":
                payload = self._task_zip_validate(body)
            elif task == "maintenance_plan":
                payload = self._task_maintenance_plan(body)
            elif task in {"music_agent_status", "music_agent_command"}:
                payload = self._task_music_agent_proxy(body)
            elif task == "music_ytdlp_resolve":
                payload = self._task_music_ytdlp_resolve(body)
            elif task == "text_stats":
                payload = self._task_text_stats(body)
            elif task == "emoji_recolor":
                payload = self._task_emoji_recolor(body)
            elif task == "tts_cache_lookup":
                payload = self._task_tts_cache_lookup(body)
            elif task == "tts_cache_store":
                payload = self._task_tts_cache_store(body)
            elif task == "tts_synthesize_benchmark":
                payload = self._task_tts_synthesize_benchmark(body)
            elif task == "tts_synthesize_piper":
                payload = self._task_tts_synthesize_piper(body)
            elif task == "tts_agent_status":
                payload = self._task_tts_agent_status(body)
            elif task in {"tts_android_voices", "tts_atts_voices", "android_tts_voices"}:
                payload = self._task_tts_android_voices(body)
            elif task == "tts_agent_synthesize":
                payload = self._task_tts_agent_synthesize(body)
            elif task == "voice_agent_status":
                payload = self._task_voice_agent_status(body)
            elif task == "voice_agent_register_session":
                payload = self._task_voice_agent_register_session(body)
            elif task == "voice_agent_clear_session":
                payload = self._task_voice_agent_clear_session(body)
            elif task == "voice_agent_guild_status":
                payload = self._task_voice_agent_guild_status(body)
            elif task == "voice_agent_register_handoff":
                payload = self._task_voice_agent_register_handoff(body)
            elif task == "voice_agent_clear_handoff":
                payload = self._task_voice_agent_clear_handoff(body)
            elif task == "voice_agent_handoff_status":
                payload = self._task_voice_agent_handoff_status(body)
            elif task == "voice_agent_prepare_transfer":
                payload = self._task_voice_agent_prepare_transfer(body)
            elif task == "voice_agent_begin_transfer":
                payload = self._task_voice_agent_begin_transfer(body)
            elif task == "voice_agent_release_transfer":
                payload = self._task_voice_agent_release_transfer(body)
            elif task == "voice_agent_transfer_status":
                payload = self._task_voice_agent_transfer_status(body)
            elif task == "voice_agent_play_tts":
                payload = self._task_voice_agent_play_tts(body)
            elif task == "voice_agent_probe_connection":
                payload = self._task_voice_agent_probe_connection(body)
            elif task == "voice_agent_connection_status":
                payload = self._task_voice_agent_connection_status(body)
            elif task == "voice_agent_clear_connection":
                payload = self._task_voice_agent_clear_connection(body)
            elif task == "log_extract":
                payload = self._task_log_extract(body)
            elif task == "log_summary":
                payload = self._task_log_summary(body)
            elif task == "ffprobe_media":
                payload = self._task_ffprobe_media(body)
            elif task == "ffmpeg_convert":
                payload = self._task_ffmpeg_convert(body)
            else:
                raise ValueError("task não suportada")
            payload.setdefault("ok", True)
            _json_response(self, HTTPStatus.OK, payload)
            _launch_deferred_phone_worker_action(payload)
        except Exception as exc:
            JOBS_FAILED += 1
            _error(self, HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")

    def _normalize_tts_edge_rate(self, raw: Any) -> str:
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

    def _normalize_tts_edge_pitch(self, raw: Any) -> str:
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

    def _normalize_tts_gtts_language(self, raw: Any) -> str:
        language = str(raw or "pt").strip().lower().replace("_", "-") or "pt"
        if language == "pt-br":
            language = "pt"
        return language




    def _ensure_tts_cache_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "cache-worker" not in caps:
            raise RuntimeError("tts_cache é restrito ao perfil turbo com cache-worker")
        return roles, capabilities

    def _tts_cache_root(self) -> Path:
        configured = str(os.getenv("PHONE_WORKER_TTS_CACHE_DIR", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "tts"

    def _tts_cache_limits(self) -> tuple[int, int]:
        def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(float(os.getenv(name, str(default)) or default))
            except Exception:
                value = default
            return max(minimum, min(maximum, value))
        max_mb = _as_int("PHONE_WORKER_TTS_CACHE_MAX_MB", 4096, 64, 32768)
        max_files = _as_int("PHONE_WORKER_TTS_CACHE_MAX_FILES", 20000, 64, 100000)
        return max_mb * 1024 * 1024, max_files

    def _sanitize_tts_cache_key(self, raw: Any) -> str:
        key = str(raw or "").strip().lower()
        key = re.sub(r"[^a-z0-9_\-]", "", key)
        if len(key) < 16:
            raise RuntimeError("cache_key inválida/curta")
        return key[:96]

    def _normalize_tts_cache_format(self, raw: Any) -> str:
        fmt = str(raw or "mp3").strip().lower().replace(".", "")
        if fmt in {"wav", "wave"}:
            return "wav"
        if fmt in {"ogg", "opus"}:
            return "ogg"
        return "mp3"

    def _tts_cache_path(self, key: str, audio_format: str) -> Path:
        return self._tts_cache_root() / f"{key}.{self._normalize_tts_cache_format(audio_format)}"

    def _find_tts_cache_file(self, key: str) -> tuple[Path | None, str]:
        root = self._tts_cache_root()
        for fmt in ("mp3", "wav", "ogg"):
            path = root / f"{key}.{fmt}"
            if path.exists() and path.stat().st_size > 0:
                return path, fmt
        return None, ""

    def _touch_tts_cache_file(self, path: Path) -> None:
        now = time.time()
        with contextlib.suppress(Exception):
            os.utime(path, (now, now))

    def _prune_tts_cache(self, *, protected: Path | None = None) -> None:
        root = self._tts_cache_root()
        max_bytes, max_files = self._tts_cache_limits()
        try:
            entries = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".wav", ".ogg"}]
        except FileNotFoundError:
            return
        stats: list[tuple[float, int, Path]] = []
        total_bytes = 0
        for path in entries:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            size = int(st.st_size or 0)
            total_bytes += size
            stats.append((float(st.st_mtime), size, path))
        if len(stats) <= max_files and total_bytes <= max_bytes:
            return
        protected_path = None
        if protected is not None:
            with contextlib.suppress(Exception):
                protected_path = protected.resolve()
        for _, size, path in sorted(stats, key=lambda item: item[0]):
            if len(stats) <= max_files and total_bytes <= max_bytes:
                break
            if protected_path is not None:
                with contextlib.suppress(Exception):
                    if path.resolve() == protected_path:
                        continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                continue
            total_bytes = max(0, total_bytes - size)
            stats = [item for item in stats if item[2] != path]

    def _schedule_tts_cache_prune(self, *, protected: Path, piper: bool = False) -> bool:
        callback = self._prune_piper_cache if piper else self._prune_tts_cache
        return _submit_tts_cache_maintenance(callback, protected=protected)

    def _task_tts_cache_lookup(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_cache_allowed()
        started = time.monotonic()
        key = self._sanitize_tts_cache_key(body.get("cache_key"))
        max_audio_bytes = max(1, int(body.get("max_audio_bytes") or self.max_output_bytes))
        path, audio_format = self._find_tts_cache_file(key)
        if path is None:
            total_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": str(body.get("engine") or "tts-cache")[:40],
                "cache_hit": False,
                "cache_exists_before": False,
                "cache_key": key[:16],
                "error": "worker turbo cache miss",
                "worker_profile": _current_core_worker_profile(),
                "worker_version": PHONE_WORKER_VERSION,
                "roles": roles[:16],
                "capabilities": capabilities[:24],
                "worker_synth_ms": 0.0,
                "worker_total_ms": round(total_ms, 2),
                "total_ms": round(total_ms, 2),
                "logs": [f"tts cache miss key={key[:16]}"],
            }
        read_started = time.monotonic()
        data = path.read_bytes()
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            raise RuntimeError("cache TTS vazio")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache TTS grande demais: {len(data)} bytes")
        self._touch_tts_cache_file(path)
        digest = hashlib.sha256(data).hexdigest()
        total_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": True,
            "engine": str(body.get("engine") or "tts-cache")[:40],
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "worker_total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": [f"tts cache hit {path.name} {len(data)} B em {read_ms:.1f} ms"],
            "data_b64": _b64encode(data, max_bytes=max_audio_bytes),
        }

    def _task_tts_cache_store(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_cache_allowed()
        started = time.monotonic()
        key = self._sanitize_tts_cache_key(body.get("cache_key"))
        audio_format = self._normalize_tts_cache_format(body.get("audio_format"))
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        if not data:
            raise RuntimeError("data_b64 vazio para cache TTS")
        expected_hash = str(body.get("sha256") or "").strip().lower()
        actual_hash = hashlib.sha256(data).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise RuntimeError("sha256 do cache TTS não confere")
        root = self._tts_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        path = self._tts_cache_path(key, audio_format)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
        self._touch_tts_cache_file(path)
        self._prune_tts_cache(protected=path)
        total_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": True,
            "engine": str(body.get("engine") or "tts-cache")[:40],
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_stored": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": actual_hash,
            "logs": [f"tts cache store {path.name} {len(data)} B"],
        }

    def _ensure_tts_benchmark_turbo_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "tts-synth" not in caps or "tts-benchmark" not in caps:
            raise RuntimeError("tts_synthesize_benchmark é restrito ao perfil turbo com tts-synth/tts-benchmark")
        return roles, capabilities

    def _ensure_tts_piper_turbo_allowed(self) -> tuple[list[str], list[str]]:
        profile = _current_core_worker_profile()
        roles, capabilities = _current_core_worker_roles_and_capabilities()
        caps = set(roles) | set(capabilities)
        if profile != "turbo" or "tts-synth" not in caps:
            raise RuntimeError("tts_synthesize_piper é restrito ao perfil turbo com tts-synth")
        return roles, capabilities




    def _task_voice_agent_status(self, body: dict[str, Any]) -> dict[str, Any]:
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot["music_agent"] = {
            "ok": bool(music_agent.get("ok")),
            "available": bool(music_agent.get("available")),
            "configured": bool(music_agent.get("configured")),
            "runtime_version": str(music_agent.get("runtime_version") or music_agent.get("version") or "")[:40],
            "latency_ms": music_agent.get("latency_ms"),
            "error": str(music_agent.get("error") or "")[:160],
        }
        snapshot["tts_agent"] = {
            "ok": bool(tts_agent.get("ok")),
            "available": bool(tts_agent.get("available")),
            "synth_ready": bool(tts_agent.get("synth_ready")),
            "selected_engine": str(tts_agent.get("selected_engine") or tts_agent.get("preferred_engine") or "")[:40],
            "available_engines": list(tts_agent.get("available_engines") or [])[:8],
            "last_error": str(tts_agent.get("last_error") or "")[:160],
        }
        snapshot.setdefault("logs", []).append("voice agent status coletado; direct_tts_voice ainda depende de etapa futura")
        return snapshot

    def _task_voice_agent_register_session(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_register_session(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("sessão de voz registrada pela VPS; worker ainda não recebeu controle geral do bot")
        return snapshot

    def _task_voice_agent_clear_session(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_session(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("sessão de voz removida pelo controle da VPS")
        return snapshot

    def _task_voice_agent_guild_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_session_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["ok"] = bool(snapshot.get("ok"))
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de sessão por guild coletado")
        return snapshot

    def _task_voice_agent_register_handoff(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_register_handoff(body)
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("handoff temporário de voz recebido em dry-run; raw token guardado só em memória")
        return snapshot

    def _task_voice_agent_clear_handoff(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_handoff(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("handoff temporário de voz limpo")
        return snapshot

    def _task_voice_agent_handoff_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_handoff_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de handoff de voz coletado")
        return snapshot

    def _task_voice_agent_prepare_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_prepare_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("transferência de posse preparada; owner ainda fica na VPS")
        return snapshot

    def _task_voice_agent_begin_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_begin_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("posse de voz concedida ao worker por confirmação explícita da VPS; ainda sem áudio automático")
        return snapshot

    def _task_voice_agent_release_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_release_transfer(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("posse de voz devolvida para a VPS")
        return snapshot

    def _task_voice_agent_transfer_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_transfer_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de transferência de posse coletado")
        return snapshot

    def _task_voice_agent_play_tts(self, body: dict[str, Any]) -> dict[str, Any]:
        if not _env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True):
            raise RuntimeError("Worker Voice Agent desativado")
        if not _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", True):
            raise RuntimeError("TTS direto do Worker Voice Agent desativado")
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        channel_id = _voice_agent_int(body.get("voice_channel_id") or body.get("channel_id"), 0)
        if guild_id <= 0 or channel_id <= 0:
            raise RuntimeError("guild_id/channel_id obrigatórios para TTS direto")
        text = _voice_agent_clean_text(body.get("text") or body.get("content"), limit=_env_int("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS", 600))
        if not text and not (body.get("audio_b64") or body.get("audio_url") or body.get("url")):
            raise RuntimeError("texto/audio obrigatório para TTS direto")
        now_ms = _voice_agent_now_ms()
        transfer: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            transfers = _voice_agent_prune_transfers()
            transfer = dict(transfers.get(str(guild_id)) or {})
        if bool(body.get("confirm_transfer") or body.get("confirm") or body.get("manual")):
            if str(transfer.get("voice_owner") or transfer.get("current_owner") or "").lower() != "worker":
                try:
                    transfer_result = _voice_agent_begin_transfer({**body, "confirm_transfer": True, "requested_owner": "worker"})
                    transfer = dict(transfer_result.get("transfer") or {})
                except Exception:
                    # Direct TTS can still be valid when the VPS had no active voice handoff;
                    # mark an explicit worker-owned lease for the Music Agent gateway path.
                    transfer = _voice_agent_set_transfer(
                        guild_id,
                        channel_id=str(channel_id),
                        text_channel_id=str(_voice_agent_int(body.get("text_channel_id"), 0) or ""),
                        requester_id=str(_voice_agent_int(body.get("requester_id"), 0) or ""),
                        bot_user_id=str(_voice_agent_int(body.get("bot_user_id"), 0) or ""),
                        source="tts_worker_voice_direct",
                        state="worker_ownership_granted_music_agent_gateway",
                        current_owner="worker",
                        voice_owner="worker",
                        requested_owner="worker",
                        lease_id=f"direct-tts:{guild_id}:{channel_id}:{now_ms}",
                        allow_connection_probe=False,
                        probe_authorized=False,
                        reason="TTS direto via Music Agent gateway; sem handoff VPS ativo",
                        error="",
                        expires_at_ms=now_ms + max(10, min(180, _env_int("PHONE_WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", 45))) * 1000,
                    )
        _voice_agent_set_connection(
            guild_id,
            channel_id=str(channel_id),
            state="worker_direct_tts_starting",
            stage="music_agent_voice_tts",
            direct_tts=True,
            dry_run=False,
            connected_once=False,
            error="",
        )
        started = time.perf_counter()
        proxy_body = dict(body)
        proxy_body["task"] = "music_agent_command"
        proxy_body["action"] = "voice_tts"
        proxy_body["voice_channel_id"] = channel_id
        proxy_body["channel_id"] = channel_id
        proxy_body["text"] = text or str(body.get("text") or body.get("content") or "")
        proxy_body["timeout_seconds"] = max(3.0, min(90.0, float(body.get("timeout_seconds") or _env_float("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS", 30.0))))
        if (
            proxy_body.get("text")
            and not (proxy_body.get("audio_b64") or proxy_body.get("audio_url") or proxy_body.get("url"))
            and _env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_WITH_TTS_AGENT", True)
        ):
            try:
                synth_body = dict(proxy_body)
                synth_body["task"] = "tts_agent_synthesize"
                synth_body.setdefault("cache_mode", "prefer")
                synth_body["timeout_seconds"] = max(3, min(int(proxy_body["timeout_seconds"]), _env_int("PHONE_WORKER_TTS_AGENT_DIRECT_PREBUILD_TIMEOUT_SECONDS", 18)))
                synth_body["max_audio_bytes"] = max(1024, min(self.max_output_bytes, _env_int("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_BYTES", 12 * 1024 * 1024)))
                synth_result = self._task_tts_agent_synthesize(synth_body)
                if isinstance(synth_result, dict) and synth_result.get("ok") and synth_result.get("data_b64"):
                    proxy_body["audio_b64"] = str(synth_result.get("data_b64") or "")
                    proxy_body["audio_format"] = str(synth_result.get("audio_format") or "mp3")
                    proxy_body["engine"] = str(synth_result.get("selected_engine") or synth_result.get("engine") or proxy_body.get("engine") or "tts-agent")
                    proxy_body["selected_engine"] = proxy_body["engine"]
                    proxy_body["prebuilt_audio"] = True
                    proxy_body["prebuilt_audio_source"] = "phone_worker_tts_agent"
                    proxy_body["tts_agent_cache_hit"] = bool(synth_result.get("cache_hit"))
                    proxy_body["tts_agent_synth_ms"] = synth_result.get("worker_synth_ms")
                    proxy_body["tts_agent_total_ms"] = synth_result.get("worker_total_ms") or synth_result.get("total_ms")
            except Exception as exc:
                # A falha de prebuild não deve quebrar o TTS direto: o Music Agent ainda
                # pode sintetizar no caminho antigo. Guardamos o motivo só para diagnóstico.
                proxy_body["tts_agent_prebuild_error"] = f"{type(exc).__name__}: {_short_text(exc, limit=140)}"
        result = self._task_music_agent_proxy(proxy_body)
        ok = bool(isinstance(result, dict) and result.get("ok", False))
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        conn = _voice_agent_set_connection(
            guild_id,
            channel_id=str(channel_id),
            state="worker_direct_tts_ok" if ok else "worker_direct_tts_failed",
            stage="music_agent_voice_tts_done" if ok else "music_agent_voice_tts_failed",
            direct_tts=True,
            dry_run=False,
            connected_once=ok,
            ready_received=ok,
            latency_ms=elapsed_ms,
            playback_ms=float(result.get("playback_ms") or result.get("elapsed_ms") or 0.0) if isinstance(result, dict) else 0.0,
            engine=str(result.get("engine") or body.get("engine") or "")[:60] if isinstance(result, dict) else str(body.get("engine") or "")[:60],
            error="" if ok else _short_text((result or {}).get("error") if isinstance(result, dict) else "Music Agent retornou resposta inválida", limit=180),
        )
        music_agent = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        tts_agent = _safe_telemetry("tts_agent", _tts_agent_snapshot, {"ok": False, "available": False, "synth_ready": False})
        snapshot = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        if not ok:
            snapshot.update({"ok": False, "direct_tts": False, "error": str((result or {}).get("error") if isinstance(result, dict) else "Music Agent falhou")[:220]})
        else:
            snapshot.update({"ok": True, "direct_tts": True, "engine": str(result.get("engine") or body.get("engine") or ""), "playback_ms": result.get("playback_ms"), "elapsed_ms": elapsed_ms})
        snapshot["worker_result"] = result
        snapshot["connection"] = _voice_agent_public_connection(conn)
        snapshot["voice_agent"] = _voice_agent_snapshot(music_agent=music_agent, tts_agent=tts_agent)
        snapshot.setdefault("logs", []).append("TTS direto worker→Discord executado via Music Agent voice plane" if ok else "TTS direto worker→Discord falhou; VPS deve usar fallback")
        return snapshot

    def _task_voice_agent_probe_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_start_connection_probe(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("voice connection dry-run iniciado; sem áudio e sem DISCORD_TOKEN no worker")
        return snapshot

    def _task_voice_agent_connection_status(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = _voice_agent_int(body.get("guild_id"), 0)
        summary = _voice_agent_connection_summary(guild_id=guild_id if guild_id > 0 else None, limit=5)
        snapshot = _voice_agent_snapshot()
        snapshot.update(summary)
        snapshot["guild_id"] = str(guild_id or "")
        snapshot.setdefault("logs", []).append("status de conexão voice dry-run coletado")
        return snapshot

    def _task_voice_agent_clear_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        result = _voice_agent_clear_connection(body)
        snapshot = _voice_agent_snapshot()
        snapshot.update(result)
        snapshot["voice_agent"] = _voice_agent_snapshot()
        snapshot.setdefault("logs", []).append("estado de conexão voice dry-run limpo")
        return snapshot

    def _task_tts_agent_status(self, body: dict[str, Any]) -> dict[str, Any]:
        snapshot = _tts_agent_snapshot()
        snapshot.update({
            "ok": bool(snapshot.get("ok")),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "logs": [f"tts agent {snapshot.get('state')} engines={','.join(snapshot.get('available_engines') or [])}"],
        })
        return snapshot

    def _task_tts_android_voices(self, body: dict[str, Any]) -> dict[str, Any]:
        locale = str(body.get("locale") or body.get("language") or "").strip()
        try:
            limit = int(body.get("limit") or 500)
        except Exception:
            limit = 500
        result = _android_tts_voices(locale=locale, limit=limit)
        result.update({
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "logs": [f"android tts voices locale={locale or 'all'} total={result.get('total', 0)} returned={len(result.get('voices') or [])}"],
        })
        return result

    def _tts_agent_engine_order(self, body: dict[str, Any], available: list[str]) -> list[str]:
        requested = str(body.get("engine") or "gtts").strip().lower().replace("-", "_") or "gtts"
        preferred = str(body.get("preferred_engine") or os.getenv("PHONE_WORKER_TTS_AGENT_ENGINE") or "auto").strip().lower().replace("-", "_") or "auto"
        fallback = str(body.get("fallback_engine") or "gtts").strip().lower().replace("-", "_") or "gtts"
        aliases = {
            "google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts",
            "edge_tts": "edge", "android": "android_native", "android_tts": "android_native", "native": "android_native",
            "native_android": "android_native", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto",
        }
        requested = aliases.get(requested, requested)
        preferred = aliases.get(preferred, preferred)
        fallback = aliases.get(fallback, fallback)
        order: list[str] = []
        if requested == "teto":
            # Prefixo explícito: Teto deve ser tentada antes de qualquer preferência global.
            order.append("teto")
        elif preferred != "auto":
            order.append(preferred)
        order.append(requested)
        if fallback != requested:
            order.append(fallback)
        for candidate in ("android_native", "edge", "gtts"):
            order.append(candidate)
        deduped: list[str] = []
        for engine in order:
            if engine not in {"teto", "android_native", "edge", "gtts"}:
                continue
            if engine not in available:
                continue
            if engine not in deduped:
                deduped.append(engine)
        return deduped

    def _tts_agent_standard_cache_enabled(self, roles: list[str], capabilities: list[str]) -> bool:
        if not _env_bool("PHONE_WORKER_TTS_AGENT_CACHE_ENABLED", True):
            return False
        if not _env_bool("PHONE_WORKER_TTS_AGENT_STANDARD_CACHE_ENABLED", True):
            return False
        profile = _current_core_worker_profile()
        if profile != "turbo" and not _env_bool("PHONE_WORKER_TTS_AGENT_CACHE_ALLOW_NON_TURBO", False):
            return False
        return True

    def _tts_agent_standard_cache_key(self, body: dict[str, Any], *, engine: str) -> str:
        normalized_engine = str(engine or body.get("engine") or "gtts").strip().lower().replace("-", "_") or "gtts"
        aliases = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "edge_tts": "edge", "android": "android_native", "android_tts": "android_native", "native": "android_native", "native_android": "android_native", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto"}
        normalized_engine = aliases.get(normalized_engine, normalized_engine)
        requested_engine = str(body.get("engine") or normalized_engine).strip().lower().replace("-", "_") or normalized_engine
        requested_engine = aliases.get(requested_engine, requested_engine)
        provided = str(body.get("cache_key") or "").strip()
        if provided and requested_engine == normalized_engine and normalized_engine != "teto":
            with contextlib.suppress(Exception):
                return self._sanitize_tts_cache_key(provided)
        text = " ".join(str(body.get("text") or "").strip().split()).lower()
        text = text.replace("!!", "!").replace("??", "?").replace("..", ".")
        if normalized_engine == "teto":
            status = _teto_status()
            fingerprint = str(status.get("fingerprint") or "unavailable")
            voice = str(body.get("voice") or "kasane-teto-standard").strip() or "kasane-teto-standard"
            language = str(body.get("language") or "pt-BR").strip() or "pt-BR"
            base_pitch = str(os.getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4")
            payload = f"teto|{fingerprint}|{voice}|{language}|{base_pitch}|{text}"
        elif normalized_engine == "android_native":
            language = str(body.get("language") or body.get("fallback_language") or "pt-BR").strip().replace("_", "-") or "pt-BR"
            voice = str(body.get("voice") or "auto").strip() or "auto"
            rate = str(body.get("rate") or "1.0").strip() or "1.0"
            pitch = str(body.get("pitch") or "1.0").strip() or "1.0"
            payload = f"android_native|{language}|{voice}|{rate}|{pitch}|{text}"
        elif normalized_engine == "edge":
            voice = str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
            rate = self._normalize_tts_edge_rate(body.get("rate"))
            pitch = self._normalize_tts_edge_pitch(body.get("pitch"))
            payload = f"edge|{voice}|{rate}|{pitch}|{text}"
        else:
            language = self._normalize_tts_gtts_language(body.get("language") or body.get("fallback_language"))
            payload = f"gtts|{language}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _tts_agent_cache_mode_allows_read(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "refresh"}

    def _tts_agent_cache_mode_allows_store(self, body: dict[str, Any]) -> bool:
        mode = str(body.get("cache_mode") or "prefer").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "none", "bypass", "no_store"}

    def _tts_agent_standard_cache_hit(self, *, key: str, engine: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, max_audio_bytes: int, raw_response: bool = False) -> dict[str, Any] | None:
        path, audio_format = self._find_tts_cache_file(key)
        if path is None:
            return None
        read_started = time.monotonic()
        data = path.read_bytes()
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            return None
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache TTS grande demais: {len(data)} bytes")
        self._touch_tts_cache_file(path)
        total_ms = (time.monotonic() - started) * 1000.0
        digest = hashlib.sha256(data).hexdigest()
        logs.append(f"standard-cache hit engine={engine} file={path.name} read={read_ms:.1f}ms")
        timing_ms = {
            "cache_read": round(read_ms, 2),
            "worker_total": round(total_ms, 2),
            "worker_synth": 0.0,
        }
        result = {
            "ok": True,
            "engine": engine,
            "selected_engine": engine,
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "timing_ms": timing_ms,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "available_engines": _tts_agent_available_engines(),
            "worker_synth_ms": 0.0,
            "worker_total_ms": round(total_ms, 2),
            "total_ms": round(total_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _store_tts_agent_standard_cache(self, *, key: str, data: bytes, audio_format: str, logs: list[str]) -> None:
        if not key or not data:
            return
        path = self._tts_cache_path(key, audio_format)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
            self._touch_tts_cache_file(path)
            if not self._schedule_tts_cache_prune(protected=path):
                self._prune_tts_cache(protected=path)
            logs.append(f"standard-cache store {path.name} {len(data)}B")
        except Exception as exc:
            logs.append(f"standard-cache store falhou: {type(exc).__name__}: {_short_text(exc, limit=90)}")
        finally:
            with contextlib.suppress(Exception):
                tmp.unlink()

    def _synthesize_standard_tts_bytes(self, body: dict[str, Any], *, engine: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, max_audio_bytes: int, timeout: int, raw_response: bool = False) -> dict[str, Any]:
        normalized = str(engine or "gtts").strip().lower().replace("-", "_") or "gtts"
        engine = {"google": "gtts", "google_tts": "gtts", "googlecloud": "gtts", "google_cloud": "gtts", "gcloud": "gtts", "kasane_teto": "teto", "teto_utau": "teto", "utau": "teto"}.get(normalized, normalized)
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        stage_ms: dict[str, float] = {}
        cache_key = ""
        if self._tts_agent_standard_cache_enabled(roles, capabilities):
            with contextlib.suppress(Exception):
                cache_key = self._tts_agent_standard_cache_key(body, engine=engine)
            if cache_key and self._tts_agent_cache_mode_allows_read(body):
                hit = self._tts_agent_standard_cache_hit(
                    key=cache_key,
                    engine=engine,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    started=started,
                    max_audio_bytes=max_audio_bytes,
                    raw_response=raw_response,
                )
                if hit is not None:
                    return hit
        audio_format = "mp3"
        data = b""
        response: dict[str, Any] = {}
        teto_meta: dict[str, Any] = {}
        if engine == "teto":
            if not _HEAVY_RESOURCE_LOCK.acquire(blocking=False):
                raise RuntimeError("recurso pesado ocupado por build ou manutenção")
            teto_started = time.monotonic()
            try:
                rendered = _get_teto_renderer().synthesize(
                    text,
                    timeout_seconds=float(timeout),
                    max_audio_bytes=max_audio_bytes,
                )
            finally:
                _HEAVY_RESOURCE_LOCK.release()
            data = bytes(rendered.pop("audio", b"") or b"")
            audio_format = self._normalize_tts_cache_format(rendered.get("audio_format") or "wav")
            teto_meta = dict(rendered)
            stage_ms["teto_render"] = round((time.monotonic() - teto_started) * 1000.0, 2)
            logs.append(
                f"teto voicebank={rendered.get('voicebank') or 'Kasane Teto'} "
                f"rendered={rendered.get('rendered_phonemes') or 0} missing={len(rendered.get('missing_phonemes') or [])}"
            )
        elif engine == "android_native":
            synth_timeout_ms = max(1000, min(timeout * 1000, int(float(body.get("android_timeout_ms") or os.getenv("PHONE_WORKER_ANDROID_TTS_SYNTH_TIMEOUT_MS") or timeout * 1000))))
            android_payload = {
                "text": text,
                "language": str(body.get("language") or body.get("fallback_language") or "pt-BR"),
                "locale": str(body.get("locale") or body.get("language") or body.get("fallback_language") or "pt-BR"),
                "voice": str(body.get("voice") or ""),
                "rate": str(body.get("rate") or "1.0"),
                "pitch": str(body.get("pitch") or "1.0"),
                "timeout_ms": synth_timeout_ms,
                "max_audio_bytes": max_audio_bytes,
            }
            android_payload["prefer_local_voice"] = _env_bool("PHONE_WORKER_ANDROID_TTS_PREFER_LOCAL_VOICE", True)
            android_started = time.monotonic()
            raw_enabled = _env_bool("PHONE_WORKER_ANDROID_TTS_RAW_ENABLED", True)
            raw_error = ""
            response = {}
            if raw_enabled:
                try:
                    data, raw_meta = _android_tts_raw_request(
                        "/native-tts/synthesize.raw",
                        payload=android_payload,
                        timeout=max(1.0, min(timeout + 1.0, (synth_timeout_ms / 1000.0) + 1.0)),
                        max_audio_bytes=max_audio_bytes,
                    )
                    audio_format = self._normalize_tts_cache_format(raw_meta.get("audio_format") or "wav")
                    android_ms = (time.monotonic() - android_started) * 1000.0
                    stage_ms["android_roundtrip"] = round(android_ms, 2)
                    apk_ms = raw_meta.get("android_synth_ms") or ""
                    with contextlib.suppress(Exception):
                        stage_ms["android_synth"] = round(float(apk_ms), 2)
                    voice_used = raw_meta.get("voice") or "auto"
                    if raw_meta.get("sha256"):
                        response["sha256"] = raw_meta.get("sha256")
                    response["android_synth_ms"] = apk_ms
                    response["voice"] = voice_used
                    response["locale"] = raw_meta.get("locale") or android_payload["locale"]
                    logs.append(f"android-native raw locale={response['locale']} voice={voice_used} format={audio_format} apk_ms={apk_ms or 0} roundtrip={android_ms:.1f}ms bytes={len(data)}")
                except Exception as exc:
                    raw_error = f"{type(exc).__name__}: {_short_text(exc, limit=110)}"
                    data = b""
                    logs.append(f"android-native raw indisponível; fallback json: {raw_error}")
            if not data:
                response = _android_tts_json_request(
                    "/native-tts/synthesize",
                    payload=android_payload,
                    timeout=max(1.0, min(timeout + 1.0, (synth_timeout_ms / 1000.0) + 1.0)),
                )
                if response.get("ok") is False:
                    raise RuntimeError(str(response.get("error") or "Android TTS retornou ok=false"))
                data = _b64decode(str(response.get("data_b64") or ""), max_bytes=max_audio_bytes)
                audio_format = self._normalize_tts_cache_format(response.get("audio_format") or "wav")
                android_ms = (time.monotonic() - android_started) * 1000.0
                stage_ms["android_roundtrip"] = round(android_ms, 2)
                with contextlib.suppress(Exception):
                    stage_ms["android_synth"] = round(float(response.get("android_synth_ms") or response.get("worker_synth_ms") or 0), 2)
                logs.append(f"android-native json locale={android_payload['locale']} format={audio_format} apk_ms={response.get('android_synth_ms') or response.get('worker_synth_ms') or 0} roundtrip={android_ms:.1f}ms")
        elif engine == "edge":
            try:
                import edge_tts  # type: ignore
            except Exception as exc:
                raise RuntimeError(f"edge-tts não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
            voice = str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
            rate = self._normalize_tts_edge_rate(body.get("rate"))
            pitch = self._normalize_tts_edge_pitch(body.get("pitch"))

            async def _edge_bytes() -> bytes:
                communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
                buffer = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        buffer.write(chunk["data"])
                return buffer.getvalue()

            try:
                data = asyncio.run(asyncio.wait_for(_edge_bytes(), timeout=timeout))
            except Exception:
                # Compatibilidade com versões antigas do edge-tts que podem não expor stream().
                with tempfile.TemporaryDirectory(prefix="phone-worker-edge-tts-") as tmp:
                    out_path = Path(tmp) / "speech.mp3"

                    async def _save_edge() -> None:
                        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
                        await communicate.save(str(out_path))

                    asyncio.run(asyncio.wait_for(_save_edge(), timeout=timeout))
                    data = out_path.read_bytes() if out_path.exists() else b""
            logs.append(f"edge voice={voice} rate={rate} pitch={pitch}")
        else:
            try:
                from gtts import gTTS  # type: ignore
            except Exception as exc:
                raise RuntimeError(f"gTTS não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
            language = self._normalize_tts_gtts_language(body.get("language") or body.get("fallback_language"))
            buffer = io.BytesIO()
            tts = gTTS(text=text, lang=language)
            tts.write_to_fp(buffer)
            data = buffer.getvalue()
            logs.append(f"gtts language={language}")
        if not data:
            raise RuntimeError("engine não gerou áudio")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        if cache_key and self._tts_agent_cache_mode_allows_store(body):
            store_started = time.monotonic()
            self._store_tts_agent_standard_cache(key=cache_key, data=data, audio_format=audio_format, logs=logs)
            stage_ms["cache_store"] = round((time.monotonic() - store_started) * 1000.0, 2)
        synth_ms = (time.monotonic() - started) * 1000.0
        stage_ms["worker_synth"] = round(synth_ms, 2)
        stage_ms["worker_total"] = round(synth_ms, 2)
        digest = hashlib.sha256(data).hexdigest()
        result = {
            "ok": True,
            "engine": engine,
            "selected_engine": engine,
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_key": cache_key[:16] if cache_key else "",
            "timing_ms": stage_ms,
            "android_synth_ms": response.get("android_synth_ms") if engine == "android_native" and isinstance(response, dict) else None,
            "android_voice": response.get("voice") if engine == "android_native" and isinstance(response, dict) else "",
            "android_locale": response.get("locale") if engine == "android_native" and isinstance(response, dict) else "",
            "teto_voicebank": teto_meta.get("voicebank") if engine == "teto" else "",
            "teto_fingerprint": teto_meta.get("voicebank_fingerprint") if engine == "teto" else "",
            "teto_rendered_phonemes": teto_meta.get("rendered_phonemes") if engine == "teto" else None,
            "teto_missing_phonemes": teto_meta.get("missing_phonemes") if engine == "teto" else [],
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip(),
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "available_engines": _tts_agent_available_engines(),
            "worker_synth_ms": round(synth_ms, 2),
            "worker_total_ms": round(synth_ms, 2),
            "total_ms": round(synth_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _task_tts_agent_synthesize(self, body: dict[str, Any], *, raw_response: bool = False) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_piper_turbo_allowed()
        if not _env_bool("PHONE_WORKER_TTS_AGENT_ENABLED", True):
            raise RuntimeError("PHONE_WORKER_TTS_AGENT_ENABLED=false")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        max_chars = max(64, _env_int("PHONE_WORKER_TTS_AGENT_MAX_TEXT_LENGTH", 1200))
        if len(text) > max_chars:
            raise ValueError(f"texto grande demais para TTS Agent ({len(text)} > {max_chars})")
        requested_engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_") or "gtts"
        if requested_engine in {"kasane_teto", "teto_utau", "utau"}:
            requested_engine = "teto"
        if requested_engine == "teto":
            teto_max_chars = max(16, _env_int("PHONE_WORKER_TETO_MAX_CHARACTERS", 180))
            if len(text) > teto_max_chars:
                raise ValueError(f"texto grande demais para Teto ({len(text)} > {teto_max_chars})")
        limit = _tts_agent_queue_limit()
        with _TTS_AGENT_LOCK:
            if _TTS_AGENT_ACTIVE >= limit:
                raise RuntimeError("TTS Agent ocupado; fila local cheia")
        timeout = max(2, min(self.job_timeout, int(float(body.get("timeout_seconds") or os.getenv("PHONE_WORKER_TTS_AGENT_TIMEOUT_SECONDS") or self.job_timeout))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        deps = _turbo_dependency_snapshot()
        available = _tts_agent_available_engines(deps)
        order = self._tts_agent_engine_order(body, available)
        if not order:
            raise RuntimeError("nenhuma engine TTS pronta no worker")
        base_logs = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"tts-agent chars={len(text)} order={','.join(order)} timeout={timeout}s",
        ]
        errors: list[str] = []
        _tts_agent_record_start()
        started = time.monotonic()
        selected = ""
        try:
            for engine in order:
                selected = engine
                try:
                    if engine == "piper":
                        piper_body = dict(body)
                        piper_body["engine"] = "piper"
                        piper_body.setdefault("cache_mode", "prefer")
                        result = self._synthesize_piper_bytes(piper_body, benchmark=False, raw_response=raw_response)
                        result["engine"] = "piper"
                        result["selected_engine"] = "piper"
                        result["worker_id"] = str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or _default_worker_id()).strip()
                        result["available_engines"] = available
                        result["logs"] = (base_logs + list(result.get("logs") or []))[:10]
                        elapsed_ms = (time.monotonic() - started) * 1000.0
                        result["total_ms"] = round(float(result.get("worker_total_ms") or elapsed_ms), 2)
                        _tts_agent_record_done(ok=True, engine="piper", elapsed_ms=elapsed_ms)
                        return result
                    result = self._synthesize_standard_tts_bytes(
                        body,
                        engine=engine,
                        roles=roles,
                        capabilities=capabilities,
                        logs=list(base_logs),
                        started=started,
                        max_audio_bytes=max_audio_bytes,
                        timeout=timeout,
                        raw_response=raw_response,
                    )
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    _tts_agent_record_done(ok=True, engine=engine, elapsed_ms=elapsed_ms)
                    return result
                except Exception as exc:
                    errors.append(f"{engine}: {type(exc).__name__}: {_short_text(exc, limit=140)}")
                    continue
            raise RuntimeError("; ".join(errors) or "todas as engines falharam")
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            _tts_agent_record_done(ok=False, engine=selected, elapsed_ms=elapsed_ms, error=str(exc))
            raise

    def _resolve_piper_command(self) -> str:
        configured = str(os.getenv("PHONE_WORKER_PIPER_COMMAND", "") or "").strip()
        if configured:
            return configured
        found = shutil.which("piper")
        if found:
            return found
        raise RuntimeError("piper não encontrado; configure PHONE_WORKER_PIPER_COMMAND ou instale o binário no worker turbo")

    def _resolve_piper_model(self, body: dict[str, Any]) -> tuple[str, str]:
        model = str(body.get("model") or body.get("model_path") or os.getenv("PHONE_WORKER_PIPER_MODEL", "") or "").strip()
        config = str(body.get("config") or body.get("config_path") or os.getenv("PHONE_WORKER_PIPER_CONFIG", "") or "").strip()
        model_name = str(body.get("model_name") or os.getenv("PHONE_WORKER_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
        if not model:
            # Atalho opcional para manter vários modelos por nome sem mexer no bot.
            env_key = re.sub(r"[^A-Z0-9]+", "_", model_name.upper()).strip("_")
            if env_key:
                model = str(os.getenv(f"PHONE_WORKER_PIPER_MODEL_{env_key}", "") or "").strip()
                config = config or str(os.getenv(f"PHONE_WORKER_PIPER_CONFIG_{env_key}", "") or "").strip()
        if not model:
            raise RuntimeError("Piper sem modelo; configure PHONE_WORKER_PIPER_MODEL=/caminho/voz.onnx no worker turbo")
        model_path = Path(model).expanduser()
        if not model_path.exists() or model_path.stat().st_size <= 0:
            raise RuntimeError(f"modelo Piper não encontrado ou vazio: {model_path}")
        if not config:
            candidate = Path(str(model_path) + ".json")
            if candidate.exists():
                config = str(candidate)
        if config:
            config_path = Path(config).expanduser()
            if not config_path.exists() or config_path.stat().st_size <= 0:
                raise RuntimeError(f"config Piper não encontrado ou vazio: {config_path}")
            config = str(config_path)
        return str(model_path), config

    def _piper_cache_enabled(self, body: dict[str, Any]) -> bool:
        raw = body.get("cache_enabled", os.getenv("PHONE_WORKER_PIPER_CACHE_ENABLED", "true"))
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    def _piper_cache_dir(self) -> Path:
        configured = str(os.getenv("PHONE_WORKER_PIPER_CACHE_DIR", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "phone-worker" / "cache" / "piper"

    def _piper_cache_limits(self) -> tuple[int, int]:
        def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(float(os.getenv(name, str(default)) or default))
            except Exception:
                value = default
            return max(minimum, min(maximum, value))
        max_mb = _as_int("PHONE_WORKER_PIPER_CACHE_MAX_MB", 2048, 32, 16384)
        max_files = _as_int("PHONE_WORKER_PIPER_CACHE_MAX_FILES", 4096, 32, 50000)
        return max_mb * 1024 * 1024, max_files

    def _normalize_piper_cache_text(self, text: str) -> str:
        return " ".join(str(text or "").strip().split()).lower()

    def _piper_cache_key(self, *, text: str, model_path: str, config_path: str, model_name: str, audio_format_hint: str) -> str:
        model = Path(model_path)
        try:
            stat = model.stat()
            model_sig = f"{model.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
        except Exception:
            model_sig = str(model_path)
        bitrate = str(os.getenv("PHONE_WORKER_PIPER_MP3_BITRATE", "96k") or "96k")
        payload = "|".join([
            "piper-worker-cache-v2",
            PHONE_WORKER_VERSION,
            str(model_name or "turbo-default"),
            model_sig,
            str(config_path or ""),
            str(audio_format_hint or "auto"),
            bitrate,
            self._normalize_piper_cache_text(text),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _find_piper_cache_file(self, key: str) -> tuple[Path | None, str]:
        cache_dir = self._piper_cache_dir()
        for fmt, suffix in (("mp3", ".mp3"), ("wav", ".wav")):
            path = cache_dir / f"{key}{suffix}"
            if path.exists() and path.stat().st_size > 0:
                return path, fmt
        return None, ""

    def _touch_piper_cache_file(self, path: Path) -> None:
        now = time.time()
        with contextlib.suppress(Exception):
            os.utime(path, (now, now))

    def _prune_piper_cache(self, *, protected: Path | None = None) -> None:
        cache_dir = self._piper_cache_dir()
        max_bytes, max_files = self._piper_cache_limits()
        try:
            entries = [p for p in cache_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".wav"}]
        except FileNotFoundError:
            return
        total_bytes = 0
        stats: list[tuple[float, int, Path]] = []
        for path in entries:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            total_bytes += int(st.st_size)
            stats.append((float(st.st_mtime), int(st.st_size), path))
        if len(stats) <= max_files and total_bytes <= max_bytes:
            return
        protected_path = protected.resolve() if protected else None
        for _, size, path in sorted(stats, key=lambda item: item[0]):
            if protected_path is not None:
                with contextlib.suppress(Exception):
                    if path.resolve() == protected_path:
                        continue
            if len(stats) <= max_files and total_bytes <= max_bytes:
                break
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                continue
            total_bytes = max(0, total_bytes - size)
            stats = [item for item in stats if item[2] != path]

    def _piper_cache_hit_result(self, *, path: Path, audio_format: str, key: str, roles: list[str], capabilities: list[str], logs: list[str], max_audio_bytes: int, started: float, cache_mode: str = "prefer", raw_response: bool = False) -> dict[str, Any]:
        read_started = time.monotonic()
        data = path.read_bytes()
        read_ms = (time.monotonic() - read_started) * 1000.0
        if not data:
            raise RuntimeError("cache Piper vazio")
        if len(data) > max_audio_bytes:
            raise RuntimeError(f"cache Piper grande demais: {len(data)} bytes")
        self._touch_piper_cache_file(path)
        digest = hashlib.sha256(data).hexdigest()
        total_ms = (time.monotonic() - started) * 1000.0
        logs.append(f"cache hit Piper {path.name} {len(data)} B em {read_ms:.1f} ms")
        result = {
            "ok": True,
            "engine": "piper",
            "audio_format": audio_format,
            "cache_hit": True,
            "cache_exists_before": True,
            "cache_mode": cache_mode,
            "cache_key": key[:16],
            "cache_file": path.name,
            "cache_read_ms": round(read_ms, 2),
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
            "worker_total_ms": round(total_ms, 2),
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _piper_cache_miss_result(self, *, key: str, roles: list[str], capabilities: list[str], logs: list[str], started: float, cache_mode: str) -> dict[str, Any]:
        total_ms = (time.monotonic() - started) * 1000.0
        logs.append(f"cache only Piper miss key={key[:16]}")
        return {
            "ok": False,
            "engine": "piper",
            "cache_hit": False,
            "cache_exists_before": False,
            "cache_mode": cache_mode,
            "cache_key": key[:16],
            "error": "cache Piper miss",
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": 0.0,
            "size": 0,
            "logs": logs[:10],
            "worker_total_ms": round(total_ms, 2),
        }

    def _synthesize_piper_bytes(self, body: dict[str, Any], *, benchmark: bool, raw_response: bool = False) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_piper_turbo_allowed()
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        if len(text) > 1600:
            raise ValueError("texto grande demais para Piper experimental")
        timeout = max(2, min(self.job_timeout, int(float(body.get("timeout_seconds") or self.job_timeout))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        model_path, config_path = self._resolve_piper_model(body)
        model_name = str(body.get("model_name") or os.getenv("PHONE_WORKER_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
        cache_mode = str(body.get("cache_mode") or "prefer").strip().lower().replace("-", "_")
        cache_enabled = self._piper_cache_enabled(body)
        cache_only_modes = {"only", "cache_only", "hit", "hit_only", "read", "read_only"}
        cache_lookup_modes = cache_only_modes | {"prefer", "preferred", "auto"}
        logs: list[str] = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"engine=piper chars={len(text)} timeout={timeout}s modelo={model_name} cache_mode={cache_mode}",
        ]
        started = time.monotonic()
        cache_key = self._piper_cache_key(text=text, model_path=model_path, config_path=config_path, model_name=model_name, audio_format_hint="auto")
        logs.append(f"cache key Piper {cache_key[:16]}")
        if cache_enabled and cache_mode in cache_lookup_modes:
            cached_path, cached_format = self._find_piper_cache_file(cache_key)
            if cached_path is not None:
                return self._piper_cache_hit_result(
                    path=cached_path,
                    audio_format=cached_format,
                    key=cache_key,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    max_audio_bytes=max_audio_bytes,
                    started=started,
                    cache_mode=cache_mode,
                    raw_response=raw_response,
                )
            logs.append("cache miss Piper")
            if cache_mode in cache_only_modes:
                return self._piper_cache_miss_result(
                    key=cache_key,
                    roles=roles,
                    capabilities=capabilities,
                    logs=logs,
                    started=started,
                    cache_mode=cache_mode,
                )
        elif cache_enabled:
            logs.append(f"cache Piper ignorado por modo={cache_mode}")
        else:
            logs.append("cache Piper desativado")

        piper_cmd = self._resolve_piper_command()
        with tempfile.TemporaryDirectory(prefix="phone-worker-piper-") as tmp:
            tmp_dir = Path(tmp)
            wav_path = tmp_dir / "speech.wav"
            mp3_path = tmp_dir / "speech.mp3"
            cmd = [piper_cmd, "--model", model_path, "--output_file", str(wav_path)]
            if config_path:
                cmd.extend(["--config", config_path])
            extra_args = shlex.split(str(os.getenv("PHONE_WORKER_PIPER_EXTRA_ARGS", "") or ""))
            if extra_args:
                cmd.extend(extra_args)
            piper_started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    input=(text + "\n").encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Piper timeout após {timeout}s") from exc
            piper_ms = (time.monotonic() - piper_started) * 1000.0
            stderr = _short_text((proc.stderr or b"").decode("utf-8", errors="ignore"), limit=220)
            if proc.returncode != 0:
                raise RuntimeError(f"Piper saiu com código {proc.returncode}: {stderr or 'sem stderr'}")
            if not wav_path.exists() or wav_path.stat().st_size <= 0:
                raise RuntimeError(f"Piper não gerou WAV válido: {stderr or 'sem stderr'}")
            logs.append(f"piper wav {wav_path.stat().st_size} B em {piper_ms:.1f} ms")

            output_path = wav_path
            audio_format = "wav"
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                conv_started = time.monotonic()
                conv_cmd = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav_path),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    str(os.getenv("PHONE_WORKER_PIPER_MP3_BITRATE", "96k") or "96k"),
                    str(mp3_path),
                ]
                conv = subprocess.run(conv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(2, min(timeout, 8)), check=False)
                conv_ms = (time.monotonic() - conv_started) * 1000.0
                if conv.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0:
                    output_path = mp3_path
                    audio_format = "mp3"
                    logs.append(f"ffmpeg mp3 {mp3_path.stat().st_size} B em {conv_ms:.1f} ms")
                else:
                    conv_err = _short_text((conv.stderr or b"").decode("utf-8", errors="ignore"), limit=160)
                    logs.append(f"ffmpeg mp3 indisponível; usando wav: {conv_err or 'sem stderr'}")
            else:
                logs.append("ffmpeg não encontrado; retornando wav")

            data = output_path.read_bytes()
            if not data:
                raise RuntimeError("Piper retornou áudio vazio")
            if len(data) > max_audio_bytes:
                raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        synth_ms = (time.monotonic() - started) * 1000.0
        digest = hashlib.sha256(data).hexdigest()
        cache_stored = False
        if cache_enabled and cache_mode not in {"bypass"}:
            try:
                cache_dir = self._piper_cache_dir()
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / f"{cache_key}.{audio_format}"
                tmp_cache_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
                tmp_cache_path.write_bytes(data)
                os.replace(tmp_cache_path, cache_path)
                self._touch_piper_cache_file(cache_path)
                if not self._schedule_tts_cache_prune(protected=cache_path, piper=True):
                    self._prune_piper_cache(protected=cache_path)
                cache_stored = True
                logs.append(f"cache store Piper {cache_path.name} {len(data)} B")
            except Exception as exc:
                logs.append(f"cache store Piper falhou: {_short_text(exc, limit=120)}")
        logs.append(f"worker Piper gerou {len(data)} bytes em {synth_ms:.1f} ms")
        result = {
            "ok": True,
            "engine": "piper",
            "audio_format": audio_format,
            "cache_hit": False,
            "cache_exists_before": False,
            "cache_mode": cache_mode,
            "cache_key": cache_key[:16],
            "cache_file": f"{cache_key}.{audio_format}" if cache_stored else "",
            "cache_stored": cache_stored,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": round(synth_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:10],
            "worker_total_ms": round(synth_ms, 2),
        }
        return _with_tts_audio_payload(result, data, max_bytes=max_audio_bytes, raw_response=raw_response)

    def _task_tts_synthesize_piper(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._synthesize_piper_bytes(body, benchmark=False)

    def _task_tts_synthesize_benchmark(self, body: dict[str, Any]) -> dict[str, Any]:
        roles, capabilities = self._ensure_tts_benchmark_turbo_allowed()
        engine = str(body.get("engine") or "gtts").strip().lower().replace("-", "_")
        if engine not in {"android_native", "edge", "gtts"}:
            raise ValueError("engine inválida para benchmark TTS")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("texto vazio")
        if len(text) > 1200:
            raise ValueError("texto grande demais para benchmark")
        timeout = max(2, min(self.job_timeout, int(float(body.get("timeout_seconds") or self.job_timeout))))
        max_audio_bytes = max(1024, min(self.max_output_bytes, int(body.get("max_audio_bytes") or self.max_output_bytes)))
        logs: list[str] = [
            f"perfil={_current_core_worker_profile()} versão={PHONE_WORKER_VERSION}",
            f"engine={engine} chars={len(text)} timeout={timeout}s",
        ]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="phone-worker-tts-bench-") as tmp:
            tmp_dir = Path(tmp)
            audio_format = "mp3"
            out_path = tmp_dir / "speech.mp3"
            try:
                if engine == "android_native":
                    result = self._synthesize_standard_tts_bytes(
                        body,
                        engine="android_native",
                        roles=roles,
                        capabilities=capabilities,
                        logs=list(logs),
                        started=started,
                        max_audio_bytes=max_audio_bytes,
                        timeout=timeout,
                    )
                    result["engine"] = "android_native"
                    result["logs"] = (logs + list(result.get("logs") or []))[:10]
                    return result
                if engine == "edge":
                    try:
                        import edge_tts  # type: ignore
                    except Exception as exc:
                        raise RuntimeError(f"edge-tts não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
                    voice = str(body.get("voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
                    rate = self._normalize_tts_edge_rate(body.get("rate"))
                    pitch = self._normalize_tts_edge_pitch(body.get("pitch"))

                    async def _save_edge() -> None:
                        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
                        await communicate.save(str(out_path))

                    asyncio.run(asyncio.wait_for(_save_edge(), timeout=timeout))
                    logs.append(f"edge voice={voice} rate={rate} pitch={pitch}")
                elif engine == "piper":
                    result = self._synthesize_piper_bytes(body, benchmark=True)
                    result["engine"] = "piper"
                    result["logs"] = (logs + list(result.get("logs") or []))[:10]
                    return result
                else:
                    try:
                        from gtts import gTTS  # type: ignore
                    except Exception as exc:
                        raise RuntimeError(f"gTTS não instalado no worker: {type(exc).__name__}: {_short_text(exc, limit=120)}") from exc
                    language = self._normalize_tts_gtts_language(body.get("language"))
                    tts = gTTS(text=text, lang=language)
                    with open(out_path, "wb") as handle:
                        tts.write_to_fp(handle)
                    logs.append(f"gtts language={language}")
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"{engine} timeout após {timeout}s no worker") from exc
            synth_ms = (time.monotonic() - started) * 1000.0
            if not out_path.exists() or out_path.stat().st_size <= 0:
                raise RuntimeError("engine não gerou arquivo de áudio")
            data = out_path.read_bytes()
            if len(data) > max_audio_bytes:
                raise RuntimeError(f"áudio grande demais: {len(data)} bytes")
        digest = hashlib.sha256(data).hexdigest()
        logs.append(f"worker gerou {len(data)} bytes em {synth_ms:.1f} ms")
        return {
            "ok": True,
            "engine": engine,
            "audio_format": audio_format,
            "worker_profile": _current_core_worker_profile(),
            "worker_version": PHONE_WORKER_VERSION,
            "roles": roles[:16],
            "capabilities": capabilities[:24],
            "worker_synth_ms": round(synth_ms, 2),
            "size": len(data),
            "sha256": digest,
            "logs": logs[:8],
            "data_b64": _b64encode(data, max_bytes=max_audio_bytes),
        }

    def _task_sha256(self, body: dict[str, Any]) -> dict[str, Any]:
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        return {"ok": True, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def _task_zip(self, body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("files vazio")
        if len(files) > 80:
            raise ValueError("arquivos demais")
        compression = zipfile.ZIP_DEFLATED
        level = max(1, min(9, int(body.get("compresslevel") or 6)))
        total_in = 0
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=compression, compresslevel=level) as zf:
            for index, item in enumerate(files, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"files[{index}] inválido")
                name = _safe_name(item.get("name"), fallback=f"file-{index}.bin")
                data = _b64decode(str(item.get("data_b64") or ""), max_bytes=self.max_body_bytes)
                total_in += len(data)
                if total_in > self.max_body_bytes:
                    raise ValueError("entrada total grande demais")
                zf.writestr(name, data)
        data_out = output.getvalue()
        return {
            "ok": True,
            "filename": _safe_name(body.get("filename"), fallback="phone-worker.zip"),
            "input_size": total_in,
            "size": len(data_out),
            "data_b64": _b64encode(data_out, max_bytes=self.max_output_bytes),
        }

    def _task_zip_validate(self, body: dict[str, Any]) -> dict[str, Any]:
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        filename = _safe_name(body.get("filename"), fallback="update.zip")
        max_entries = max(1, min(2000, int(body.get("max_entries") or 600)))
        max_preview = max(1, min(80, int(body.get("max_preview") or 30)))
        warnings: list[str] = []
        errors: list[str] = []
        extensions: Counter[str] = Counter()
        total_uncompressed = 0
        file_count = 0
        dir_count = 0
        py_files: list[str] = []
        shell_files: list[str] = []
        large_files: list[dict[str, Any]] = []
        assets = 0
        manifests = 0
        service_files = 0
        script_files = 0
        top_level: Counter[str] = Counter()
        preview: list[str] = []

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                bad = zf.testzip()
                if bad:
                    errors.append(f"arquivo corrompido no ZIP: {bad}")
                infos = zf.infolist()
                if len(infos) > max_entries:
                    warnings.append(f"muitos itens no ZIP: {len(infos)}")
                for info in infos:
                    raw_name = str(info.filename or "")
                    normalized = raw_name.replace("\\", "/").lstrip("/")
                    parts = [part for part in normalized.split("/") if part]
                    if not parts:
                        continue
                    top_level[parts[0][:80]] += 1
                    if len(preview) < max_preview:
                        preview.append(normalized[:240])
                    if normalized.startswith("/") or any(part == ".." for part in parts):
                        errors.append(f"caminho inseguro: {raw_name}")
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        errors.append(f"symlink não permitido: {raw_name}")
                    if info.is_dir():
                        dir_count += 1
                        continue
                    file_count += 1
                    total_uncompressed += int(info.file_size or 0)
                    suffix = Path(parts[-1]).suffix.lower() or "<sem_ext>"
                    extensions[suffix] += 1
                    path_lc = normalized.lower()
                    if suffix == ".py":
                        py_files.append(normalized)
                    elif suffix in {".sh", ".bash", ".zsh"}:
                        shell_files.append(normalized)
                    if "/assets/" in f"/{path_lc}" or path_lc.startswith("assets/") or "/public/" in f"/{path_lc}":
                        assets += 1
                    if "manifest" in path_lc or path_lc.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock")):
                        manifests += 1
                    if path_lc.startswith("deploy/systemd/") or path_lc.endswith(".service") or path_lc.endswith(".timer"):
                        service_files += 1
                    if suffix in {".sh", ".py"} or path_lc.startswith("scripts/"):
                        script_files += 1
                    if int(info.file_size or 0) >= 1024 * 1024:
                        large_files.append({"path": normalized[:240], "size": int(info.file_size or 0)})
                    if total_uncompressed > self.max_output_bytes * 6:
                        warnings.append("tamanho descompactado muito alto para validação leve")
                        break
        except zipfile.BadZipFile:
            raise ValueError("ZIP inválido")

        large_files.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        risk = "ok"
        if errors:
            risk = "blocked"
        elif warnings or large_files or service_files:
            risk = "review"
        return {
            "ok": not errors,
            "filename": filename,
            "risk": risk,
            "size": len(data),
            "files": file_count,
            "dirs": dir_count,
            "total_uncompressed": total_uncompressed,
            "extensions": dict(extensions.most_common(20)),
            "top_level": dict(top_level.most_common(12)),
            "python_files": len(py_files),
            "shell_files": len(shell_files),
            "assets": assets,
            "manifests": manifests,
            "service_files": service_files,
            "script_files": script_files,
            "large_files": large_files[:12],
            "preview": preview,
            "warnings": warnings[:20],
            "errors": errors[:20],
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _task_maintenance_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        entries = body.get("entries") or []
        if not isinstance(entries, list):
            raise ValueError("entries precisa ser lista")
        max_entries = max(1, min(5000, int(body.get("max_entries") or 1000)))
        now = float(body.get("now") or time.time())
        scanned = 0
        total_size = 0
        by_kind: dict[str, dict[str, Any]] = {}
        old_temp: list[dict[str, Any]] = []
        old_logs: list[dict[str, Any]] = []
        largest: list[dict[str, Any]] = []
        for item in entries[:max_entries]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")[:260]
            kind = str(item.get("kind") or "other")[:40]
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            try:
                raw_mtime = item.get("mtime")
                mtime = float(now if raw_mtime is None else raw_mtime)
            except Exception:
                mtime = now
            age_seconds = max(0, int(now - mtime))
            scanned += 1
            total_size += max(0, size)
            bucket = by_kind.setdefault(kind, {"count": 0, "size": 0})
            bucket["count"] += 1
            bucket["size"] += max(0, size)
            record = {"path": path, "size": size, "age_seconds": age_seconds, "kind": kind}
            largest.append(record)
            path_lc = path.lower()
            if kind in {"tmp_audio", "cache", "temp"} or "tmp_audio" in path_lc or "/cache/" in path_lc:
                if age_seconds >= 3600:
                    old_temp.append(record)
            if kind == "log" or path_lc.endswith((".log", ".txt")):
                if age_seconds >= 7 * 86400:
                    old_logs.append(record)
        largest.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        old_temp.sort(key=lambda item: (int(item.get("age_seconds") or 0), int(item.get("size") or 0)), reverse=True)
        old_logs.sort(key=lambda item: (int(item.get("age_seconds") or 0), int(item.get("size") or 0)), reverse=True)
        reclaimable_temp = sum(int(item.get("size") or 0) for item in old_temp)
        reclaimable_logs = sum(int(item.get("size") or 0) for item in old_logs)
        reclaimable = reclaimable_temp + reclaimable_logs
        recommendations: list[str] = []
        if old_temp:
            recommendations.append(f"limpar {len(old_temp)} cache(s)/temporário(s) antigos com cerca de {_format_bytes(reclaimable_temp)}")
        if old_logs:
            recommendations.append(f"arquivar ou remover {len(old_logs)} log(s) antigos com cerca de {_format_bytes(reclaimable_logs)}")
        if not recommendations:
            recommendations.append("nenhuma limpeza automática necessária agora")
        summary = f"{scanned} arquivo(s) analisados; {_format_bytes(reclaimable)} recuperável estimado; nada foi apagado"
        return {
            "ok": True,
            "summary": summary,
            "safe": True,
            "note": "Plano apenas sugere limpeza; o worker não remove arquivos automaticamente.",
            "scanned": scanned,
            "total_size": total_size,
            "by_kind": by_kind,
            "largest": largest[:30],
            "old_temp_candidates": old_temp[:80],
            "old_log_candidates": old_logs[:80],
            "estimated_reclaimable": reclaimable,
            "estimated_reclaimable_temp": reclaimable_temp,
            "estimated_reclaimable_logs": reclaimable_logs,
            "recommendations": recommendations[:12],
        }

    def _emoji_cdn_url(self, emoji: dict[str, Any]) -> str:
        emoji_id = str(emoji.get("id") or "")
        ext = "gif" if bool(emoji.get("animated")) else "png"
        return f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"

    def _download_emoji_asset(self, emoji: dict[str, Any], *, limit: int = 900_000) -> bytes:
        req = urllib.request.Request(self._emoji_cdn_url(emoji), headers={"User-Agent": "CorePhoneWorker/emoji-recolor"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("emoji base grande demais")
        return data

    def _rgb_from_hex_worker(self, value: Any) -> tuple[int, int, int]:
        raw = str(value or "#5865F2").strip().upper()
        if not raw.startswith("#"):
            raw = "#" + raw
        if not re.fullmatch(r"#[0-9A-F]{6}", raw):
            raw = "#5865F2"
        return int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16)

    def _mix_rgb_worker(self, first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        amount = max(0.0, min(1.0, float(amount)))
        return (
            max(0, min(255, int(round(first[0] * (1.0 - amount) + second[0] * amount)))),
            max(0, min(255, int(round(first[1] * (1.0 - amount) + second[1] * amount)))),
            max(0, min(255, int(round(first[2] * (1.0 - amount) + second[2] * amount)))),
        )

    def _adjust_rgb_hsv_worker(self, rgb: tuple[int, int, int], *, sat_mul: float = 1.0, val_mul: float = 1.0, hue_shift: float = 0.0) -> tuple[int, int, int]:
        h, sat, val = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        h = (h + hue_shift) % 1.0
        sat = max(0.0, min(1.0, sat * sat_mul))
        val = max(0.0, min(1.0, val * val_mul))
        r, g, b = colorsys.hsv_to_rgb(h, sat, val)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _palette_from_hex_worker(self, values: Any, fallback: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        if isinstance(values, list):
            for item in values[:8]:
                try:
                    result.append(self._rgb_from_hex_worker(item))
                except Exception:
                    continue
        if result:
            return result
        return [
            fallback,
            self._adjust_rgb_hsv_worker(fallback, sat_mul=0.92, val_mul=1.24),
            self._adjust_rgb_hsv_worker(fallback, sat_mul=1.06, val_mul=0.68),
        ]

    def _fit_emoji_canvas_frame(self, frame: Any, *, canvas_size: int = 128) -> Any:
        if Image is None:
            raise RuntimeError("Pillow não instalado no worker")
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        rgba = frame.convert("RGBA")
        if rgba.size == (canvas_size, canvas_size):
            return rgba.copy()
        width, height = rgba.size
        if width <= 0 or height <= 0:
            return Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        scale = min(canvas_size / float(width), canvas_size / float(height))
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        resized = rgba.resize(new_size, resampling)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        canvas.alpha_composite(resized, ((canvas_size - resized.width) // 2, (canvas_size - resized.height) // 2))
        return canvas

    def _recolor_rgba_image(self, img: Any, rgb: tuple[int, int, int], palette: list[tuple[int, int, int]] | None = None) -> Any:
        img = img.convert("RGBA")
        px = img.load()
        base = rgb
        usable_palette = palette or [base]
        light = usable_palette[1] if len(usable_palette) > 1 else self._adjust_rgb_hsv_worker(base, sat_mul=0.92, val_mul=1.22)
        dark = usable_palette[2] if len(usable_palette) > 2 else self._adjust_rgb_hsv_worker(base, sat_mul=1.04, val_mul=0.68)
        accents = usable_palette[3:] or [base]
        width, height = img.size
        for y in range(height):
            for x in range(width):
                r, g, b, a = px[x, y]
                if a < 8:
                    continue
                lum = max(0.0, min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0))
                if lum < 0.50:
                    target = self._mix_rgb_worker(dark, base, lum / 0.50)
                else:
                    target = self._mix_rgb_worker(base, light, (lum - 0.50) / 0.50)
                try:
                    oh, osat, _oval = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if accents and osat > 0.08:
                        accent = accents[(int(oh * 12) + (x // 24) + (y // 24)) % len(accents)]
                        target = self._mix_rgb_worker(target, accent, 0.10)
                except Exception:
                    pass
                px[x, y] = (target[0], target[1], target[2], a)
        return img

    def _save_static_emoji_png(self, img: Any) -> bytes:
        candidate = self._fit_emoji_canvas_frame(img, canvas_size=128)
        out = io.BytesIO()
        candidate.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= 256 * 1024:
            return data
        quantized = candidate.convert("P", palette=Image.Palette.ADAPTIVE, colors=96).convert("RGBA") if Image is not None else candidate
        out = io.BytesIO()
        quantized.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= 256 * 1024:
            return data
        raise RuntimeError("emoji estático ficou maior que 256 KiB")

    def _save_animated_emoji_gif(self, frames: list[Any], durations: list[int]) -> bytes | None:
        if not frames:
            return None
        normalized_frames = [self._fit_emoji_canvas_frame(frame, canvas_size=128) for frame in frames]
        for step in (1, 2, 3, 4, 5, 6, 8, 10):
            selected = [frame for idx, frame in enumerate(normalized_frames) if idx % step == 0]
            selected_durations = [max(20, min(500, int((durations[idx] if idx < len(durations) else 80) * step))) for idx in range(len(normalized_frames)) if idx % step == 0]
            if not selected:
                continue
            out = io.BytesIO()
            selected[0].save(out, format="GIF", save_all=True, append_images=selected[1:], duration=selected_durations, loop=0, optimize=True, disposal=2)
            data = out.getvalue()
            if len(data) <= 256 * 1024:
                return data
        return None

    def _recolor_emoji_bytes(self, raw: bytes, *, animated: bool, color: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        if Image is None:
            raise RuntimeError("Pillow não instalado no worker")
        rgb = self._rgb_from_hex_worker(color)
        subtle_palette = palette or self._palette_from_hex_worker([], rgb)
        with Image.open(io.BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                frames = [self._recolor_rgba_image(self._fit_emoji_canvas_frame(frame), rgb, subtle_palette) for frame in raw_frames]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            fitted = self._fit_emoji_canvas_frame(img.convert("RGBA"))
            out_img = self._recolor_rgba_image(fitted, rgb, subtle_palette)
            return self._save_static_emoji_png(out_img), "png"

    def _task_emoji_recolor(self, body: dict[str, Any]) -> dict[str, Any]:
        emojis = body.get("emojis") or []
        if not isinstance(emojis, list):
            raise ValueError("emojis precisa ser lista")
        color = str(body.get("color") or "#5865F2")
        base_rgb = self._rgb_from_hex_worker(color)
        monochrome = bool(body.get("monochrome", False))
        palette = self._palette_from_hex_worker(body.get("palette"), base_rgb)
        # A VPS já escolhe a cor/paleta efetiva da mensagem. O worker só executa
        # o processamento pesado e não deve trocar essa decisão por outra cor.
        # Quando o avatar é monocromático, a paleta recebida já vem coerente com isso.
        if monochrome and not body.get("palette"):
            palette = [
                base_rgb,
                self._adjust_rgb_hsv_worker(base_rgb, sat_mul=0.70, val_mul=1.18),
                self._adjust_rgb_hsv_worker(base_rgb, sat_mul=0.85, val_mul=0.72),
            ]
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for raw in emojis[:4]:
            if not isinstance(raw, dict):
                continue
            raw_variants = raw.get("raw_variants")
            if not isinstance(raw_variants, list):
                raw_variants = []
            raw_variants = [str(item or "") for item in raw_variants if str(item or "")]
            emoji = {
                "raw": str(raw.get("raw") or ""),
                "raw_variants": raw_variants,
                "key": str(raw.get("key") or ""),
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or "emoji")[:32],
                "animated": bool(raw.get("animated")),
            }
            if emoji["raw"] and emoji["raw"] not in emoji["raw_variants"]:
                emoji["raw_variants"].insert(0, emoji["raw"])
            if not re.fullmatch(r"\d{15,25}", emoji["id"]):
                continue
            try:
                data = self._download_emoji_asset(emoji)
                out, fmt = self._recolor_emoji_bytes(data, animated=bool(emoji.get("animated")), color=color, palette=palette)
                items.append({**emoji, "format": fmt, "size": len(out), "data_b64": _b64encode(out, max_bytes=512 * 1024)})
            except Exception as exc:
                errors.append({"id": emoji["id"], "error": str(exc)[:160]})
                continue
        return {"ok": True, "items": items, "count": len(items), "errors": errors[:4], "monochrome": monochrome, "summary": f"{len(items)} emoji(s) recolorido(s)"}



    def _task_text_stats(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if len(text.encode("utf-8")) > self.max_body_bytes:
            raise ValueError("texto grande demais")
        lines = text.splitlines()
        words = text.split()
        return {
            "ok": True,
            "bytes": len(text.encode("utf-8")),
            "chars": len(text),
            "lines": len(lines),
            "words": len(words),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _task_log_extract(self, body: dict[str, Any]) -> dict[str, Any]:
        import re
        text = str(body.get("text") or "")
        pattern = str(body.get("pattern") or r"error|exception|traceback|falhou|failed|fatal|timeout")
        max_lines = max(1, min(500, int(body.get("max_lines") or 120)))
        flags = re.IGNORECASE
        regex = re.compile(pattern, flags)
        matches = [line for line in text.splitlines() if regex.search(line)]
        trimmed = matches[-max_lines:]
        return {"ok": True, "matches": trimmed, "count": len(matches), "returned": len(trimmed)}


    @staticmethod
    def _normalize_log_message(line: str) -> str:
        text = str(line or "")
        # Remove prefixos comuns de journal/systemd e dados muito voláteis para agrupar melhor.
        text = re.sub(r"^\d{4}-\d{2}-\d{2}[T\s][^\s]+\s+", "", text)
        text = re.sub(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+", "", text)
        text = re.sub(r"^[\w.\-]+\s+", "", text, count=1)
        text = re.sub(r"^[\w@./+\-]+(?:\[\d+\])?:\s*", "", text)
        text = re.sub(r"\bguild=\d+\b", "guild=<id>", text)
        text = re.sub(r"\bchannel=\d+\b", "channel=<id>", text)
        text = re.sub(r"\buser=\d+\b", "user=<id>", text)
        text = re.sub(r"\b\d{15,22}\b", "<snowflake>", text)
        text = re.sub(r"\bpid=\d+\b|\[\d+\]", "[pid]", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:220] or "linha vazia"

    def _task_log_summary(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        if len(text.encode("utf-8")) > self.max_body_bytes:
            raise ValueError("texto grande demais")
        max_recent = max(1, min(80, int(body.get("max_recent") or 12)))
        max_top = max(1, min(40, int(body.get("max_top") or 12)))
        lines = text.splitlines()
        patterns = {
            "critical": r"\bcritical\b|\bcritico\b|\bcrítico\b|\bfatal\b",
            "error": r"\berror\b|\berro\b",
            "warning": r"\bwarning\b|\bwarn\b|\baviso\b",
            "timeout": r"timeout|timed out|tempo esgotado",
            "traceback": r"traceback",
            "exception": r"exception|exce[cç][aã]o",
            "failed": r"failed|falhou|failure|falha",
            "restart": r"restart|restarting|started|stopped|iniciando|parando",
            "syntax": r"syntaxerror|indentationerror|taberror",
            "import": r"importerror|modulenotfounderror|extensionfailed|extensionnotfound",
            "lavalink": r"lavalink|lavasrc|trackexception|loadexception",
            "yt_dlp": r"yt[-_ ]?dlp|youtube|googlevideo",
            "rate_limit": r"rate.?limit|too many requests|429",
            "phone_worker": r"phone-worker|phone_lavalink|phone-lavalink",
        }
        compiled = {key: re.compile(pattern, re.IGNORECASE) for key, pattern in patterns.items()}
        counts = {key: 0 for key in compiled}
        important: list[str] = []
        grouped: Counter[str] = Counter()
        for line in lines:
            hit = False
            for key, regex in compiled.items():
                if regex.search(line or ""):
                    counts[key] += 1
                    hit = True
            if hit:
                important.append(line.strip())
                grouped[self._normalize_log_message(line)] += 1
        top_messages = [
            {"message": message, "count": count}
            for message, count in grouped.most_common(max_top)
        ]
        return {
            "ok": True,
            "bytes": len(text.encode("utf-8")),
            "lines": len(lines),
            "important_count": len(important),
            "counts": counts,
            "recent": important[-max_recent:],
            "top_messages": top_messages,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }


    def _task_hash_batch(self, body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("files vazio")
        if len(files) > 64:
            raise ValueError("arquivos demais para hash_batch")
        total = 0
        results: list[dict[str, Any]] = []
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"files[{index}] inválido")
            name = _safe_name(item.get("name"), fallback=f"file-{index}.bin")
            data = _b64decode(str(item.get("data_b64") or ""), max_bytes=self.max_body_bytes)
            total += len(data)
            if total > self.max_body_bytes:
                raise ValueError("entrada total grande demais")
            results.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"ok": True, "summary": f"{len(results)} hash(es) calculados", "files": results, "total_bytes": total}

    def _task_endpoint_probe(self, body: dict[str, Any]) -> dict[str, Any]:
        raw_targets = body.get("targets") or body.get("urls") or []
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        if not isinstance(raw_targets, list) or not raw_targets:
            base_url, _token, _worker_id = _core_worker_auth_parts()
            raw_targets = [base_url.rstrip("/") + "/health"] if base_url else []
        if not raw_targets:
            raise ValueError("nenhum endpoint informado")
        timeout = max(0.5, min(8.0, float(body.get("timeout_seconds") or 3.0)))
        max_targets = max(1, min(8, int(body.get("max_targets") or 4)))
        results: list[dict[str, Any]] = []
        for raw_url in raw_targets[:max_targets]:
            url = str(raw_url or "").strip()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                results.append({"url": _short_text(url, limit=120), "ok": False, "error": "URL inválida"})
                continue
            started = time.perf_counter()
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json,text/plain,*/*", "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}"}, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    sample = resp.read(512)
                    status = int(getattr(resp, "status", 200) or 200)
                results.append({
                    "url": _short_text(url, limit=160),
                    "ok": 200 <= status < 500,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "bytes_sampled": len(sample),
                })
            except Exception as exc:
                results.append({"url": _short_text(url, limit=160), "ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"})
        return {"ok": any(item.get("ok") for item in results), "summary": "endpoints testados pelo worker", "results": results}

    def _task_music_agent_proxy(self, body: dict[str, Any]) -> dict[str, Any]:
        """Proxy authenticated /task requests to the local same-bot Music Agent."""
        _load_phone_worker_runtime_env()
        token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip() or _ensure_music_agent_token_env(persist=True)
        host = str(os.getenv("MUSIC_AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(float(os.getenv("MUSIC_AGENT_PORT") or 8780))
        except Exception:
            port = 8780
        action = str(body.get("action") or body.get("command") or "status").strip().lower().replace("-", "_") or "status"
        try:
            timeout_seconds = float(body.get("timeout_seconds") or os.getenv("MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS") or 18.0)
        except Exception:
            timeout_seconds = 18.0
        timeout_seconds = max(1.0, min(90.0, timeout_seconds))
        base = f"http://{host}:{port}"
        agent_configured = bool(str(os.getenv("MUSIC_AGENT_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip())

        if action not in {"status", "get_state"} and not agent_configured:
            return {
                "ok": False,
                "available": False,
                "error": "Music Agent sem token do bot no worker",
                "message": "configure MUSIC_AGENT_BOT_TOKEN em ~/phone-worker/secrets/music-agent.env",
                "agent": {"host": host, "port": port, "configured": False},
            }

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _request_agent() -> dict[str, Any]:
            local_headers = dict(headers)
            if action in {"status", "get_state"}:
                req = urllib.request.Request(f"{base}/health", headers=local_headers, method="GET")
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    raw = resp.read(min(self.max_output_bytes, 1024 * 1024)).decode("utf-8", "replace")
                parsed = json.loads(raw or "{}")
            else:
                payload = {k: v for k, v in body.items() if k not in {"task", "timeout_seconds"}}
                payload.setdefault("action", action)
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                local_headers["Content-Type"] = "application/json"
                req = urllib.request.Request(f"{base}/command", data=encoded, headers=local_headers, method="POST")
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    raw = resp.read(min(self.max_output_bytes, 1024 * 1024)).decode("utf-8", "replace")
                parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}

        attempted_prepare: dict[str, Any] | None = None
        try:
            if action not in {"status", "get_state"}:
                snapshot = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": agent_configured})
                runtime_version = str(snapshot.get("version") or snapshot.get("runtime_version") or "").strip()
                file_version = str(snapshot.get("file_version") or "").strip()
                needs_restart = bool(snapshot.get("needs_restart") or (runtime_version and file_version and _version_lt_loose(runtime_version, file_version)))
                if needs_restart:
                    attempted_prepare = _run_service_action("music-agent", "restart")
                elif not bool(snapshot.get("available")):
                    attempted_prepare = _run_service_action("music-agent", "start")
            data = _request_agent()
        except urllib.error.HTTPError as exc:
            raw = ""
            with contextlib.suppress(Exception):
                raw = exc.read(2048).decode("utf-8", "replace")
            return {
                "ok": False,
                "available": False,
                "error": f"Music Agent HTTP {exc.code}: {_short_text(raw or exc.reason, limit=260)}",
                "agent": {"host": host, "port": port, "configured": agent_configured},
                "prepare": attempted_prepare,
            }
        except Exception as exc:
            # Uma queda de conexão no primeiro comando normalmente significa
            # agent parado/desatualizado. Tenta um start uma vez antes de falhar.
            if action not in {"status", "get_state"} and attempted_prepare is None and agent_configured:
                try:
                    attempted_prepare = _run_service_action("music-agent", "start")
                    data = _request_agent()
                except Exception as retry_exc:
                    return {
                        "ok": False,
                        "available": False,
                        "error": f"{type(retry_exc).__name__}: {_short_text(retry_exc, limit=260)}",
                        "first_error": f"{type(exc).__name__}: {_short_text(exc, limit=180)}",
                        "agent": {"host": host, "port": port, "configured": agent_configured},
                        "prepare": attempted_prepare,
                    }
            else:
                return {
                    "ok": False,
                    "available": False,
                    "error": f"{type(exc).__name__}: {_short_text(exc, limit=260)}",
                    "agent": {"host": host, "port": port, "configured": agent_configured},
                    "prepare": attempted_prepare,
                }
        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("available", bool(data.get("discord_ready") or data.get("available") or data.get("ok")))
            data.setdefault("agent", {"host": host, "port": port, "configured": agent_configured})
            if attempted_prepare is not None:
                data.setdefault("prepare", attempted_prepare)
            return data
        return {"ok": False, "available": False, "error": "resposta inválida do Music Agent", "agent": {"host": host, "port": port, "configured": agent_configured}, "prepare": attempted_prepare}

    def _task_music_ytdlp_resolve(self, body: dict[str, Any]) -> dict[str, Any]:
        query = str(body.get("query") or body.get("url") or body.get("q") or "").strip()
        if not query:
            raise ValueError("query vazia")
        limit = max(1, min(10, int(float(body.get("limit") or body.get("max_results") or 5))))
        timeout = max(5, min(self.job_timeout, int(float(body.get("timeout_seconds") or min(self.job_timeout, 30)))))
        fmt = str(body.get("format") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or "bestaudio/best").strip() or "bestaudio/best"
        is_url = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", query) or query.lower().startswith("www."))
        metadata_only = str(body.get("metadata_only") if body.get("metadata_only") is not None else body.get("search_only") or "").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}
        allow_playlist = str(body.get("allow_playlist") or "").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}

        def _default_search_prefix() -> str:
            raw = str(
                body.get("default_search")
                or os.getenv("PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH")
                or os.getenv("MUSIC_WORKER_YTDLP_DEFAULT_SEARCH")
                or "ytsearch"
            ).strip().lower()
            raw = raw.rstrip(":")
            if raw in {"ytsearch", "ytsearchdate", "ytsearchall", "ytmsearch"}:
                raw = f"{raw}{limit}"
            if not raw:
                raw = f"ytsearch{limit}"
            return raw

        default_search = _default_search_prefix()
        if is_url or query.lower().startswith(("ytsearch:", "ytsearch", "ytmsearch:")):
            target = query
        else:
            # A pesquisa textual precisa ser explícita. Sem isso, o YouTube pode
            # interpretar "megalovania" como ID/URL e retornar "Video unavailable".
            target = f"{default_search}:{query}"

        try:
            import yt_dlp  # type: ignore
        except Exception as exc:
            raise RuntimeError("yt-dlp não está instalado no phone worker") from exc

        def _split_csv(value: Any) -> list[str]:
            items: list[str] = []
            for part in re.split(r"[,;\s]+", str(value or "")):
                clean = part.strip()
                if clean:
                    items.append(clean)
            return items

        def _safe_url_for_log(value: str) -> str:
            if not value:
                return ""
            try:
                parsed = urllib.parse.urlsplit(value)
                if parsed.scheme and parsed.netloc:
                    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:80], "", ""))
            except Exception:
                pass
            return _short_text(value, limit=160)

        def select_stream(entry: dict[str, Any]) -> str:
            for item in entry.get("requested_downloads") or []:
                if isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    if url.startswith(("http://", "https://")):
                        return url
            url = str(entry.get("url") or "").strip()
            if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
                return url
            best_url = ""
            best_score = -1.0
            for fmt_item in entry.get("formats") or []:
                if not isinstance(fmt_item, dict):
                    continue
                candidate = str(fmt_item.get("url") or "").strip()
                if not candidate.startswith(("http://", "https://")):
                    continue
                acodec = str(fmt_item.get("acodec") or "").lower()
                vcodec = str(fmt_item.get("vcodec") or "").lower()
                if acodec in {"", "none"}:
                    continue
                score = float(fmt_item.get("abr") or fmt_item.get("tbr") or 0)
                if vcodec in {"", "none"}:
                    score += 10000
                if score > best_score:
                    best_score = score
                    best_url = candidate
            return best_url

        configured_cookies = str(
            os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE")
            or os.getenv("MUSIC_WORKER_YTDLP_COOKIES_FILE")
            or os.getenv("MUSIC_YTDLP_COOKIES_FILE")
            or os.getenv("YTDLP_COOKIES_FILE")
            or ""
        ).strip()
        default_cookies = Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt"
        cookies_path = Path(configured_cookies).expanduser() if configured_cookies else default_cookies
        cookies_ok = bool(cookies_path.exists() and cookies_path.is_file() and cookies_path.stat().st_size > 0)

        js_runtime_raw = str(
            body.get("js_runtimes")
            or body.get("js_runtime")
            or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES")
            or os.getenv("MUSIC_WORKER_YTDLP_JS_RUNTIMES")
            or "node"
        ).strip()
        js_runtimes = _split_csv(js_runtime_raw)
        remote_components = str(
            body.get("remote_components")
            or os.getenv("PHONE_WORKER_MUSIC_YTDLP_REMOTE_COMPONENTS")
            or os.getenv("MUSIC_WORKER_YTDLP_REMOTE_COMPONENTS")
            or ""
        ).strip()

        ydl_opts: dict[str, Any] = {
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not allow_playlist,
            "ignoreerrors": True,
            "socket_timeout": max(3, min(20, timeout - 1)),
            "retries": max(0, int(float(os.getenv("PHONE_WORKER_MUSIC_YTDLP_RETRIES", "1") or 1))),
            "fragment_retries": max(0, int(float(os.getenv("PHONE_WORKER_MUSIC_YTDLP_FRAGMENT_RETRIES", "1") or 1))),
            "cachedir": str(Path(os.getenv("PHONE_WORKER_MUSIC_YTDLP_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "yt-dlp")).expanduser()),
        }
        if cookies_ok:
            ydl_opts["cookiefile"] = str(cookies_path)
        if metadata_only:
            # Busca textual leve: retorna 5 candidatos sem resolver stream_url, sem
            # ffmpeg e sem abrir endpoint de áudio. A resolução pesada acontece
            # apenas depois que o usuário escolhe uma faixa.
            ydl_opts["extract_flat"] = "in_playlist"
            ydl_opts.pop("format", None)

        # O suporte Python para js_runtimes pode variar por versão do yt-dlp.
        # A chamada via API continua rápida quando funcionar; se retornar vazio
        # por challenge/EJS, o fallback CLI abaixo usa exatamente os flags que
        # funcionaram no Termux: --js-runtimes node + ytsearch1:<query>.
        started = time.time()
        info: Any = None
        api_error = ""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            api_error = f"{type(exc).__name__}: {_short_text(exc, limit=220)}"
            info = None

        entries: list[Any]
        if isinstance(info, dict) and isinstance(info.get("entries"), list):
            entries = list(info.get("entries") or [])
            playlist_title = str(info.get("title") or "")
            is_playlist = True
        elif info is not None:
            entries = [info]
            playlist_title = ""
            is_playlist = False
        else:
            entries = []
            playlist_title = ""
            is_playlist = False

        tracks: list[dict[str, Any]] = []

        def entry_webpage_url(entry: dict[str, Any], *, stream_url: str = "") -> str:
            webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or "").strip()
            if webpage_url and webpage_url != stream_url and webpage_url.startswith(("http://", "https://")):
                return webpage_url
            entry_id = str(entry.get("id") or entry.get("display_id") or "").strip()
            source_key = str(entry.get("extractor_key") or entry.get("extractor") or "").lower()
            if entry_id and ("youtube" in source_key or target.lower().startswith(("ytsearch", "ytmsearch"))):
                return f"https://www.youtube.com/watch?v={entry_id}"
            return str(entry.get("webpage_url_basename") or entry.get("display_id") or query).strip() or query

        def append_metadata_track(entry: dict[str, Any], *, source_label: str | None = None) -> bool:
            if not isinstance(entry, dict):
                return False
            webpage_url = entry_webpage_url(entry)
            if not webpage_url:
                return False
            title_value = entry.get("title") or entry.get("fulltitle") or entry.get("alt_title") or ""
            title = _short_text(title_value, limit=160, default=query)
            uploader = _short_text(entry.get("uploader") or entry.get("channel") or entry.get("creator") or entry.get("artist") or "", limit=120)
            source_value = source_label or entry.get("extractor_key") or entry.get("extractor") or "worker-ytdlp-search"
            tracks.append({
                "title": title,
                "uploader": uploader,
                "duration": entry.get("duration"),
                "thumbnail": _short_text(entry.get("thumbnail") or "", limit=500),
                "webpage_url": webpage_url,
                "original_url": webpage_url,
                "original_query": query,
                "source": _short_text(source_value, limit=80, default="worker-ytdlp-search"),
                "extractor": "worker-ytdlp",
                "is_live": bool(entry.get("is_live")),
                "metadata_only": True,
                "search_only": True,
                "is_direct_stream": False,
            })
            return True

        def append_entry_track(entry: dict[str, Any], *, source_label: str | None = None) -> bool:
            if not isinstance(entry, dict):
                return False
            if metadata_only:
                return append_metadata_track(entry, source_label=source_label)
            stream_url = select_stream(entry)
            if not stream_url:
                return False
            webpage_url = entry_webpage_url(entry, stream_url=stream_url)
            title_value = entry.get("title") or entry.get("fulltitle") or entry.get("alt_title") or ""
            title = _short_text(title_value, limit=160, default=(query if not is_url else "Música"))
            uploader = _short_text(entry.get("uploader") or entry.get("channel") or entry.get("creator") or entry.get("artist") or "", limit=120)
            source_value = source_label or entry.get("extractor_key") or entry.get("extractor") or "worker-ytdlp"
            track_payload = {
                "title": title,
                "uploader": uploader,
                "duration": entry.get("duration"),
                "thumbnail": _short_text(entry.get("thumbnail") or "", limit=500),
                "webpage_url": webpage_url,
                "original_url": webpage_url,
                "original_query": query,
                "stream_url": stream_url,
                "direct_url": stream_url,
                "source": _short_text(source_value, limit=80, default="worker-ytdlp"),
                "extractor": "worker-ytdlp",
                "is_live": bool(entry.get("is_live")),
                "ext": _short_text(entry.get("ext") or "", limit=20),
                "format_id": _short_text(entry.get("format_id") or "", limit=80),
                "http_headers": entry.get("http_headers") if isinstance(entry.get("http_headers"), dict) else {},
                "is_direct_stream": True,
            }
            stream_id = _register_music_stream(track_payload)
            if stream_id:
                track_payload["worker_stream_id"] = stream_id
                track_payload["worker_stream_path"] = f"/music/stream/{stream_id}"
                track_payload["worker_stream_transport"] = "pcm_s16le_48k_stereo"
            tracks.append(track_payload)
            return True

        for entry in entries:
            append_entry_track(entry)
            if len(tracks) >= limit:
                break

        cli_stderr = ""
        cli_rc: int | None = None
        if not tracks:
            cmd_json = [shutil.which("python") or "python", "-m", "yt_dlp"]
            if cookies_ok:
                cmd_json += ["--cookies", str(cookies_path)]
            if js_runtimes:
                cmd_json += ["--js-runtimes", ",".join(js_runtimes)]
            if remote_components:
                cmd_json += ["--remote-components", remote_components]
            if metadata_only:
                cmd_json += ["--flat-playlist"]
            cmd_json += [
                "--no-warnings",
                "--socket-timeout",
                str(max(3, min(20, timeout - 1))),
            ]
            if not allow_playlist:
                cmd_json += ["--no-playlist"]
            if not metadata_only:
                cmd_json += ["-f", fmt]
            cmd_json += ["-J", target]
            try:
                proc_json = subprocess.run(
                    cmd_json,
                    cwd=str(Path.home() / "phone-worker"),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
                cli_rc = int(proc_json.returncode)
                cli_stderr = _short_text(proc_json.stderr or "", limit=800)
                parsed: Any = json.loads(proc_json.stdout or "{}") if proc_json.stdout else {}
                json_entries: list[Any]
                if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
                    json_entries = list(parsed.get("entries") or [])
                    playlist_title = playlist_title or str(parsed.get("title") or "")
                    is_playlist = True
                elif isinstance(parsed, dict) and parsed:
                    json_entries = [parsed]
                else:
                    json_entries = []
                for entry in json_entries:
                    if isinstance(entry, dict):
                        append_entry_track(entry, source_label="worker-ytdlp-cli-json")
                    if len(tracks) >= limit:
                        break
            except Exception as exc:
                cli_stderr = f"{type(exc).__name__}: {_short_text(exc, limit=500)}"

        if not tracks and not metadata_only:
            cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
            if cookies_ok:
                cmd += ["--cookies", str(cookies_path)]
            for runtime in js_runtimes:
                # yt-dlp aceita lista separada por vírgula em uma única opção.
                # Mantemos uma opção para todos os runtimes para preservar sintaxe CLI.
                pass
            if js_runtimes:
                cmd += ["--js-runtimes", ",".join(js_runtimes)]
            if remote_components:
                cmd += ["--remote-components", remote_components]
            cmd += [
                "--no-warnings",
                "--socket-timeout",
                str(max(3, min(20, timeout - 1))),
            ]
            if not allow_playlist:
                cmd += ["--no-playlist"]
            cmd += [
                "-f",
                fmt,
                "-g",
                target,
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(Path.home() / "phone-worker"),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
                cli_rc = int(proc.returncode)
                cli_stderr = _short_text(proc.stderr or "", limit=800)
                urls = [
                    line.strip()
                    for line in (proc.stdout or "").splitlines()
                    if line.strip().startswith(("http://", "https://"))
                ]
                for idx, stream_url in enumerate(urls[:limit], start=1):
                    track_payload = {
                        "title": _short_text(query if not is_url else "Música", limit=160, default="Música"),
                        "uploader": "",
                        "duration": None,
                        "thumbnail": "",
                        "webpage_url": query,
                        "original_url": query,
                        "original_query": query,
                        "stream_url": stream_url,
                        "direct_url": stream_url,
                        "source": "worker-ytdlp-cli",
                        "extractor": "worker-ytdlp",
                        "is_live": False,
                        "ext": "",
                        "format_id": "",
                        "http_headers": {},
                        "is_direct_stream": True,
                    }
                    stream_id = _register_music_stream(track_payload)
                    if stream_id:
                        track_payload["worker_stream_id"] = stream_id
                        track_payload["worker_stream_path"] = f"/music/stream/{stream_id}"
                        track_payload["worker_stream_transport"] = "pcm_s16le_48k_stereo"
                    tracks.append(track_payload)
            except Exception as exc:
                cli_stderr = f"{type(exc).__name__}: {_short_text(exc, limit=500)}"

        if not tracks:
            reason = cli_stderr or api_error or "yt-dlp não retornou URL de áudio"
            print(
                "[music-ytdlp] tracks=0 "
                f"target={_short_text(target, limit=120)!r} cookies={'on' if cookies_ok else 'off'} "
                f"js={','.join(js_runtimes) or 'off'} rc={cli_rc} erro={_short_text(reason, limit=240)}",
                flush=True,
            )

        return {
            "ok": True,
            "summary": ("busca leve resolvida pelo yt-dlp no worker" if metadata_only and tracks else "música resolvida pelo yt-dlp no worker" if tracks else "yt-dlp não encontrou áudio tocável no worker"),
            "query": query,
            "target": target,
            "tracks": tracks,
            "tracks_found": len(tracks),
            "is_playlist": is_playlist,
            "playlist_title": playlist_title,
            "truncated": bool(len(entries) > len(tracks)),
            "metadata_only": bool(metadata_only),
            "allow_playlist": bool(allow_playlist),
            "elapsed_ms": round((time.time() - started) * 1000.0, 1),
            "cookies": "on" if cookies_ok else "off",
            "js_runtime": ",".join(js_runtimes) if js_runtimes else "",
            "default_search": default_search,
            "api_error": _short_text(api_error, limit=240),
            "cli_rc": cli_rc,
            "cli_error": _short_text(cli_stderr, limit=240),
        }

    def _task_ffprobe_media(self, body: dict[str, Any]) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe não instalado no celular")
        input_ext = str(body.get("input_ext") or "bin").strip(". /\\")[:12] or "bin"
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        timeout = max(3, min(self.job_timeout, int(body.get("timeout_seconds") or min(self.job_timeout, 20))))
        with tempfile.TemporaryDirectory(prefix="phone-worker-ffprobe-") as tmp:
            src = Path(tmp) / f"input.{input_ext}"
            src.write_bytes(data)
            cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")[-800:]
                raise RuntimeError(f"ffprobe falhou: {err}")
            parsed = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
        streams = []
        for stream in parsed.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            streams.append({
                "index": stream.get("index"),
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
                "duration": stream.get("duration"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "bit_rate": stream.get("bit_rate"),
            })
        fmt = parsed.get("format") if isinstance(parsed.get("format"), dict) else {}
        return {
            "ok": True,
            "input_size": len(data),
            "format": {
                "name": fmt.get("format_name"),
                "duration": fmt.get("duration"),
                "size": fmt.get("size"),
                "bit_rate": fmt.get("bit_rate"),
            },
            "streams": streams,
        }

    def _task_ffmpeg_convert(self, body: dict[str, Any]) -> dict[str, Any]:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg não instalado no celular")
        input_ext = str(body.get("input_ext") or "bin").strip(". /\\")[:12] or "bin"
        output_ext = str(body.get("output_ext") or "ogg").strip(". /\\")[:12] or "ogg"
        args = body.get("ffmpeg_args")
        if not isinstance(args, list) or not args:
            if output_ext in {"ogg", "opus"}:
                args = ["-vn", "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1"]
                output_ext = "ogg"
            elif output_ext == "mp3":
                args = ["-vn", "-c:a", "libmp3lame", "-b:a", "96k"]
            else:
                args = ["-vn"]
        safe_args = [str(part) for part in args if str(part) not in {";", "&&", "||"}]
        data = _b64decode(str(body.get("data_b64") or ""), max_bytes=self.max_body_bytes)
        timeout = max(3, min(self.job_timeout, int(body.get("timeout_seconds") or self.job_timeout)))
        with tempfile.TemporaryDirectory(prefix="phone-worker-ffmpeg-") as tmp:
            src = Path(tmp) / f"input.{input_ext}"
            dst = Path(tmp) / f"output.{output_ext}"
            src.write_bytes(data)
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *safe_args, str(dst)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="ignore")[-800:]
                raise RuntimeError(f"ffmpeg falhou: {err}")
            out = dst.read_bytes()
        return {
            "ok": True,
            "output_ext": output_ext,
            "input_size": len(data),
            "size": len(out),
            "data_b64": _b64encode(out, max_bytes=self.max_output_bytes),
        }



def _command_version(command: str) -> dict[str, Any]:
    if not shutil.which(command):
        return {"ok": False, "available": False, "command": command}
    try:
        proc = subprocess.run([command, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
        output = (proc.stdout or proc.stderr).decode("utf-8", errors="replace").splitlines()
        return {
            "ok": proc.returncode == 0,
            "available": True,
            "command": command,
            "returncode": proc.returncode,
            "version_line": _short_text(output[0] if output else "", limit=180),
        }
    except Exception as exc:
        return {"ok": False, "available": True, "command": command, "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}



def _sanitize_log_text(value: str, *, limit: int = 12000) -> str:
    text = str(value or "")
    for key in ("PHONE_WORKER_TOKEN", "CORE_WORKER_TOKEN"):
        raw = str(os.getenv(key) or "").strip()
        if raw:
            text = text.replace(raw, "[redacted]")
    text = re.sub(r"(Authorization:\s*Bearer\s+)[^\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(X-(?:Phone|Core)-Worker-Token:\s*)[^\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    if len(text) > limit:
        return text[-limit:]
    return text


def _phone_worker_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_DIR") or (Path.home() / "phone-worker")).expanduser()


def _phone_worker_log_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_LOG_FILE") or (_phone_worker_dir() / "phone-worker.log")).expanduser()


def _phone_worker_pid_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PID_FILE") or (_phone_worker_dir() / "phone-worker.pid")).expanduser()


def _phone_worker_status_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_STATUS_FILE") or (_phone_worker_dir() / "phone-worker.status")).expanduser()


def _phone_worker_start_lock_dir() -> Path:
    return Path(os.getenv("PHONE_WORKER_LOCK_DIR") or (_phone_worker_dir() / ".phone-worker-start.lock")).expanduser()


def _phone_worker_watch_log_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_WATCH_LOG_FILE") or (_phone_worker_dir() / "phone-worker-watch.log")).expanduser()


def _phone_worker_watch_pid_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_WATCH_PID_FILE") or (_phone_worker_dir() / "phone-worker-watch.pid")).expanduser()


def _phone_worker_pending_results_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PENDING_RESULTS_FILE") or (_phone_worker_dir() / "phone-worker-pending-results.json")).expanduser()


def _phone_worker_pending_results_archive_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_PENDING_RESULTS_ARCHIVE_FILE") or (_phone_worker_dir() / "phone-worker-pending-results.archive.json")).expanduser()


def _phone_worker_update_status_file() -> Path:
    return Path(os.getenv("PHONE_WORKER_UPDATE_STATUS_FILE") or (_phone_worker_dir() / "phone-worker-update.status.json")).expanduser()


def _write_json_file_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_persisted_pending_core_job_results() -> None:
    data = _read_json_file(_phone_worker_pending_results_file())
    items = data.get("results") if isinstance(data.get("results"), dict) else {}
    if not items:
        return
    with _CORE_JOB_LOCK:
        for job_id, payload in list(items.items())[:20]:
            if isinstance(payload, dict) and str(job_id or "").strip():
                _PENDING_CORE_JOB_RESULTS[str(job_id)] = dict(payload)


def _persist_pending_core_job_results() -> None:
    path = _phone_worker_pending_results_file()
    with _CORE_JOB_LOCK:
        items = {k: v for k, v in _PENDING_CORE_JOB_RESULTS.items() if k and isinstance(v, dict)}
    if not items:
        with contextlib.suppress(Exception):
            path.unlink()
        return
    try:
        _write_json_file_atomic(path, {"updated_at": time.time(), "results": items})
    except Exception as exc:
        print(f"[core-worker-jobs] não consegui persistir resultado pendente: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _archive_pending_core_job_result(job_id: str, payload: dict[str, Any], *, reason: str, response: dict[str, Any] | None = None) -> None:
    """Move um resultado pendente impossível de reenviar para histórico local curto.

    Isso evita loops eternos quando a VPS já limpou o job antigo, mas preserva
    informação suficiente para diagnóstico.
    """
    safe_job_id = _short_text(job_id, limit=80)
    if not safe_job_id:
        return
    path = _phone_worker_pending_results_archive_file()
    archive = _read_json_file(path)
    items = archive.get("items") if isinstance(archive.get("items"), list) else []
    entry = {
        "job_id": safe_job_id,
        "archived_at": time.time(),
        "reason": _short_text(reason, limit=180),
        "summary": _short_text(payload.get("summary") or payload.get("error") or "", limit=180),
        "status": _short_text(payload.get("status") or "", limit=40),
        "type": _short_text((payload.get("result") or {}).get("type") if isinstance(payload.get("result"), dict) else payload.get("type"), limit=60),
        "stored_at": payload.get("stored_at"),
        "response": _short_text(json.dumps(response or {}, ensure_ascii=False, separators=(",", ":")), limit=600),
    }
    items.append(entry)
    items = items[-40:]
    try:
        _write_json_file_atomic(path, {"updated_at": time.time(), "items": items})
    except Exception as exc:
        print(f"[core-worker-jobs] não consegui arquivar resultado pendente antigo: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)


def _job_result_rejection_is_permanent(code: int, data: dict[str, Any]) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    if int(code or 0) == 404 and ("job não encontrado" in text or "job nao encontrado" in text or "job not found" in text):
        return True
    return False


def _read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0]
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _path_size(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except Exception:
        return None


def _runtime_supervisor_snapshot() -> dict[str, Any]:
    pid_file = _phone_worker_pid_file()
    status_file = _phone_worker_status_file()
    log_file = _phone_worker_log_file()
    watch_log = _phone_worker_watch_log_file()
    watch_pid_file = _phone_worker_watch_pid_file()
    pid = _read_pid_file(pid_file)
    watch_pid = _read_pid_file(watch_pid_file)
    processes = _pgrep_count("phone_worker.py") if "_pgrep_count" in globals() else None
    result: dict[str, Any] = {
        "ok": True,
        "current_pid": os.getpid(),
        "pid_file": str(pid_file),
        "pid_file_pid": pid,
        "pid_file_alive": _pid_alive(pid),
        "processes": processes,
        "duplicates": (max(0, int(processes) - 1) if isinstance(processes, int) else None),
        "lock_dir": str(_phone_worker_start_lock_dir()),
        "lock_active": _phone_worker_start_lock_dir().exists(),
        "log_file": str(log_file),
        "log_size_bytes": _path_size(log_file),
        "watch_log_file": str(watch_log),
        "watch_log_size_bytes": _path_size(watch_log),
        "watch_pid_file": str(watch_pid_file),
        "watch_pid": watch_pid,
        "watch_pid_alive": _pid_alive(watch_pid),
        "status_file": str(status_file),
        "status_text": _short_text(_read_text_file(status_file, limit=240), limit=160),
    }
    watch_alive = result.get("watch_pid_alive") is True
    result["watchdog_ok"] = bool(watch_alive)
    result["supervisor_ok"] = bool(
        (result.get("pid_file_alive") in {True, None})
        and (result.get("duplicates") in {0, None})
        and watch_alive
    )
    return result


def _sshd_snapshot() -> dict[str, Any]:
    """Diagnóstico local do canal SSH do Termux usado pela VPS para wake.

    Não inicia/paralisa SSH automaticamente aqui; apenas informa se o caminho
    que a VPS tenta usar parece existir. Isso ajuda o painel a diferenciar
    "Tailscale ativo" de "SSHD/porta indisponível".
    """
    configured_port = str(os.getenv("PHONE_WORKER_SSH_PORT") or os.getenv("PHONE_LAVALINK_SSH_PORT") or "8022").strip() or "8022"
    result: dict[str, Any] = {
        "ok": False,
        "source": "termux-sshd",
        "installed": bool(shutil.which("sshd")),
        "port": configured_port,
        "running": False,
        "processes": 0,
        "listening": False,
        "listening_ports": [],
    }
    try:
        count = _pgrep_count("sshd")
        result["processes"] = count
        result["running"] = count > 0
    except Exception as exc:
        result["process_error"] = _short_text(exc, limit=100)
    output = ""
    if shutil.which("ss"):
        _code, stdout, stderr = _run_text_command(["ss", "-lnt"], timeout=2.0, max_bytes=16384)
        output = stdout or stderr or ""
    elif shutil.which("netstat"):
        _code, stdout, stderr = _run_text_command(["netstat", "-lnt"], timeout=2.0, max_bytes=16384)
        output = stdout or stderr or ""
    ports: list[str] = []
    if output:
        for line in output.splitlines():
            if "LISTEN" not in line.upper() and not re.search(r"[:.]\d+\s", line):
                continue
            for match in re.findall(r"(?::|\.)(\d{2,5})(?:\s|$)", line):
                if match not in ports:
                    ports.append(match)
        result["listening_ports"] = ports[:16]
        result["listening"] = configured_port in ports or "22" in ports
    if not result["installed"]:
        result["summary"] = "sshd não instalado no Termux"
    elif result["listening"]:
        result["summary"] = f"sshd ouvindo porta {configured_port}"
    elif result["running"]:
        result["summary"] = "sshd rodando, mas porta configurada não apareceu ouvindo"
    else:
        result["summary"] = "sshd parado; wake via SSH não funciona"
    result["ok"] = bool(result.get("installed") and (result.get("running") or result.get("listening")))
    return result


def _termux_boot_script_path() -> Path:
    return (Path.home() / ".termux" / "boot" / "10-core-worker").expanduser()


def _termux_boot_script_content() -> str:
    return "\n".join([
        '#!/data/data/com.termux/files/usr/bin/sh',
        '# Auto-start do Core Worker pelo Termux:Boot.',
        '# Criado/reparado pelo phone-worker. Não coloque segredos aqui.',
        'termux-wake-lock 2>/dev/null || true',
        'sleep "${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}"',
        'cd "$HOME/phone-worker" || exit 0',
        'if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then',
        '  nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.boot.log" 2>&1 &',
        '  exit 0',
        'fi',
        'echo \'[core-worker-boot] watch-phone-worker.sh não encontrado\' >> "$HOME/phone-worker.log"',
        ''
    ])




def _termux_shell_autostart_block() -> str:
    """Bloco gerenciado para iniciar o watchdog quando o Termux é aberto."""
    return "\n".join([
        '# >>> core-worker-autostart >>>',
        '# Bloco gerenciado pelo Core Worker. Não coloque segredos aqui.',
        'if [ -z "${CORE_WORKER_SHELL_AUTOSTART_DONE:-}" ]; then',
        '  export CORE_WORKER_SHELL_AUTOSTART_DONE=1',
        '  if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then',
        '    (',
        '      termux-wake-lock >/dev/null 2>&1 || true',
        '      cd "$HOME/phone-worker" >/dev/null 2>&1 || exit 0',
        '      nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.shell.log" 2>&1 &',
        '    ) >/dev/null 2>&1 &',
        '  fi',
        'fi',
        '# <<< core-worker-autostart <<<',
        ''
    ])


def _update_managed_shell_block(path: Path, block: str) -> bool:
    start = '# >>> core-worker-autostart >>>'
    end = '# <<< core-worker-autostart <<<'
    path = path.expanduser()
    try:
        text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
    except Exception:
        text = ''
    pattern = re.compile(re.escape(start) + r'.*?' + re.escape(end) + r'\n?', re.DOTALL)
    clean = pattern.sub('', text).rstrip()
    new_text = (clean + '\n\n' if clean else '') + block
    if text == new_text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding='utf-8')
    return True


def _termux_shell_autostart_status_snapshot() -> dict[str, Any]:
    files = [Path.home() / '.bashrc', Path.home() / '.profile']
    items: list[dict[str, Any]] = []
    ok_any = False
    for path in files:
        exists = path.exists()
        has_block = False
        has_watchdog = False
        try:
            text = path.read_text(encoding='utf-8', errors='ignore') if exists else ''
            has_block = '# >>> core-worker-autostart >>>' in text and '# <<< core-worker-autostart <<<' in text
            has_watchdog = 'watch-phone-worker.sh' in text
        except Exception:
            pass
        ok = bool(exists and has_block and has_watchdog)
        ok_any = ok_any or ok
        items.append({'path': str(path), 'exists': exists, 'content_ok': ok, 'has_block': has_block, 'has_watchdog': has_watchdog})
    return {
        'ok': bool(ok_any),
        'source': 'termux-shell',
        'files': items,
        'summary': 'shell abre watchdog ao abrir Termux' if ok_any else 'shell não dispara watchdog ao abrir Termux',
    }


def _repair_termux_shell_autostart() -> dict[str, Any]:
    block = _termux_shell_autostart_block()
    changed: list[str] = []
    errors: list[str] = []
    for path in (Path.home() / '.bashrc', Path.home() / '.profile'):
        try:
            if _update_managed_shell_block(path, block):
                changed.append(str(path))
        except Exception as exc:
            errors.append(f'{path}: {type(exc).__name__}: {_short_text(exc, limit=100)}')
    status = _termux_shell_autostart_status_snapshot()
    status['changed_files'] = changed
    status['changed'] = bool(changed)
    if errors:
        status['errors'] = errors[:4]
        status['ok'] = False
        status['summary'] = 'falha reparando autostart de shell do Termux'
    return status


def _termux_boot_package_status() -> dict[str, Any]:
    # Best-effort: Android 16 pode negar listagem completa de pacotes para o app,
    # então falha aqui não deve marcar o boot como quebrado.
    if not shutil.which("cmd"):
        return {"available": None, "source": "cmd_missing"}
    for command in (["cmd", "package", "path", "com.termux.boot"], ["pm", "path", "com.termux.boot"]):
        code, stdout, stderr = _run_text_command(command, timeout=2.0, max_bytes=2048)
        text = f"{stdout}\n{stderr}".strip()
        if code == 0 and "package:" in stdout:
            return {"available": True, "source": command[0]}
        if "com.termux.boot" in text.lower() and "permission denial" not in text.lower():
            return {"available": True, "source": command[0], "note": _short_text(text, limit=100)}
        if "permission denial" in text.lower():
            return {"available": None, "source": command[0], "note": "Android negou listagem de pacote"}
    return {"available": False, "source": "pm", "note": "Termux:Boot não detectado"}


def _termux_boot_status_snapshot() -> dict[str, Any]:
    path = _termux_boot_script_path()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "executable": False,
        "content_ok": False,
        "ok": False,
        "source": "termux-boot",
    }
    try:
        exists = path.exists()
        result["exists"] = bool(exists)
        if exists:
            result["executable"] = os.access(path, os.X_OK)
            content = path.read_text(encoding="utf-8", errors="ignore")[:4096]
            has_watch = "watch-phone-worker.sh" in content
            direct_start = "start-phone-worker.sh" in content and not has_watch
            result["mode"] = "watchdog" if has_watch else ("direct-start" if direct_start else "unknown")
            result["content_ok"] = has_watch and "phone-worker" in content and "nohup" in content
            result["uses_watchdog"] = bool(has_watch)
            result["direct_start_only"] = bool(direct_start)
            result["size"] = path.stat().st_size
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=100)}"
    package = _termux_boot_package_status()
    result["package"] = package
    result["package_available"] = package.get("available")
    result["ok"] = bool(result.get("exists") and result.get("executable") and result.get("content_ok"))
    if result["ok"] and package.get("available") is False:
        result["warning"] = "script ok, mas app Termux:Boot não detectado"
    elif not result["ok"]:
        result["warning"] = "script de boot ausente/incompleto ou não aponta para watchdog"
    pieces = [
        "script existe" if result.get("exists") else "script ausente",
        "executável" if result.get("executable") else "sem permissão de execução",
        "watchdog" if result.get("uses_watchdog") else "sem watchdog",
        "Termux:Boot instalado" if package.get("available") else "Termux:Boot não detectado",
    ]
    result["summary"] = ("boot automático ok: " if result.get("ok") else "boot automático precisa atenção: ") + "; ".join(pieces)
    return result


def _repair_termux_boot_script() -> dict[str, Any]:
    path = _termux_boot_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _termux_boot_script_content()
    changed = True
    if path.exists():
        with contextlib.suppress(Exception):
            changed = path.read_text(encoding="utf-8", errors="ignore") != content
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o755)
    shell_status = _repair_termux_shell_autostart()
    status = _termux_boot_status_snapshot()
    status.update({
        "ok": bool(status.get("ok")) and bool(shell_status.get("ok", True)),
        "changed": bool(changed or shell_status.get("changed")),
        "shell_autostart": shell_status,
        "summary": "boot/shell automático reparado" if status.get("ok") else "boot automático criado, verifique Termux:Boot",
    })
    return status


def _home_script(name: str) -> Path:
    return (Path.home() / name).expanduser()


def _script_candidates(name: str) -> list[Path]:
    # Preferir scripts dentro de ~/phone-worker, mas manter compatibilidade com ~/script.sh.
    worker_path = (_phone_worker_dir() / name).expanduser()
    home_path = _home_script(name)
    if worker_path == home_path:
        return [worker_path]
    return [worker_path, home_path]


def _best_script(name: str) -> Path:
    for path in _script_candidates(name):
        if path.exists():
            return path
    return _script_candidates(name)[0]


def _duplicate_installations_snapshot() -> dict[str, Any]:
    home = Path.home().expanduser()
    official = _phone_worker_dir().expanduser()
    candidates: list[Path] = []
    for path in (official, home / "phone-worker-install", home / "phone-worker"):
        try:
            resolved = path.resolve()
            if path.exists() and (path / "phone_worker.py").exists() and all(resolved != existing.resolve() for existing in candidates):
                candidates.append(path)
        except Exception:
            continue

    boot_text = ""
    with contextlib.suppress(Exception):
        boot_text = _termux_boot_script_path().read_text(encoding="utf-8", errors="ignore")[:8192]
    env_dir = str(os.getenv("PHONE_WORKER_DIR") or "").strip()

    duplicate_details: list[dict[str, Any]] = []
    for path in candidates:
        try:
            if path.resolve() == official.resolve():
                continue
        except Exception:
            continue
        path_s = str(path)
        active_reasons: list[str] = []
        if path_s and path_s in boot_text:
            active_reasons.append("referenciada pelo Termux:Boot")
        if env_dir and str(Path(env_dir).expanduser()) == path_s:
            active_reasons.append("PHONE_WORKER_DIR aponta para ela")
        for script_name in ("start-phone-worker.sh", "watch-phone-worker.sh"):
            script_path = path / script_name
            if script_path.exists() and os.access(script_path, os.X_OK):
                # Existir executável em duplicata não é ativo sozinho, mas ajuda no diagnóstico.
                pass
        duplicate_details.append({
            "path": path_s,
            "active": bool(active_reasons),
            "reason": "; ".join(active_reasons) if active_reasons else "duplicata encontrada, mas boot oficial não aponta para ela",
        })

    return {
        "official": str(official),
        "found": [str(path) for path in candidates],
        "duplicates": [item["path"] for item in duplicate_details],
        "details": duplicate_details,
        "has_duplicates": bool(duplicate_details),
        "active_duplicates": [item["path"] for item in duplicate_details if item.get("active")],
        "has_active_duplicates": any(bool(item.get("active")) for item in duplicate_details),
    }


def _script_inventory() -> dict[str, Any]:
    scripts: dict[str, Any] = {}
    complete = True
    mirrored = True
    for name in ("start-phone-worker.sh", "watch-phone-worker.sh", "pair-phone-worker.sh"):
        worker_path = (_phone_worker_dir() / name).expanduser()
        home_path = _home_script(name)
        worker_exists = worker_path.exists()
        home_exists = home_path.exists()
        exists = worker_exists or home_exists
        complete = complete and exists
        mirrored = mirrored and worker_exists and home_exists
        scripts[name] = {
            "ok": exists,
            "worker_dir": worker_exists,
            "home": home_exists,
            "executable": any(path.exists() and os.access(path, os.X_OK) for path in (worker_path, home_path)),
            "preferred": str(_best_script(name)),
        }
    installs = _duplicate_installations_snapshot()
    shell_autostart = _termux_shell_autostart_status_snapshot()
    return {
        "complete": complete,
        "mirrored": mirrored,
        "scripts": scripts,
        "shell_autostart": shell_autostart,
        "installations": installs,
        # Só marcamos como problema quando a duplicata parece ativa/referenciada.
        # Diretórios antigos inativos continuam visíveis no diagnóstico sem poluir o card.
        "duplicate_installations": bool(installs.get("has_active_duplicates")),
        "duplicate_installations_found": bool(installs.get("has_duplicates")),
    }


def _local_boot_needs_repair() -> tuple[bool, str]:
    boot = _termux_boot_status_snapshot()
    scripts = _script_inventory()
    reasons: list[str] = []
    if not boot.get("ok"):
        reasons.append(str(boot.get("warning") or "boot incompleto"))
    installs = scripts.get("installations") if isinstance(scripts.get("installations"), dict) else {}
    if installs.get("has_active_duplicates"):
        reasons.append("duplicata ativa aponta para instalação antiga")
    shell_autostart = _termux_shell_autostart_status_snapshot()
    if not shell_autostart.get("ok"):
        reasons.append("Termux aberto não dispara watchdog")
    return bool(reasons), "; ".join(reasons)


def _auto_repair_local_boot_if_needed() -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_AUTO_BOOT_REPAIR", True):
        return {"enabled": False}
    try:
        needed, reason = _local_boot_needs_repair()
        shell_status = _termux_shell_autostart_status_snapshot()
        shell_needed = not bool(shell_status.get("ok"))
        if not needed and not shell_needed:
            return {"enabled": True, "changed": False, "reason": "ok", "shell_autostart": shell_status}
        repaired = _repair_termux_boot_script() if needed else _termux_boot_status_snapshot()
        shell_repaired = _repair_termux_shell_autostart() if shell_needed else shell_status
        reasons = [reason] if reason else []
        if shell_needed:
            reasons.append("shell do Termux não iniciava watchdog ao abrir app")
        return {
            "enabled": True,
            "changed": bool(needed or shell_needed),
            "reason": "; ".join(reasons) or "reparo aplicado",
            "boot": repaired,
            "shell_autostart": shell_repaired,
        }
    except Exception as exc:
        return {"enabled": True, "changed": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}


def _tmux_session_exists(session: str) -> bool:
    if not session or not shutil.which("tmux"):
        return False
    code, _stdout, _stderr = _run_text_command(["tmux", "has-session", "-t", session], timeout=2.0, max_bytes=1024)
    return code == 0


def _pgrep_count(pattern: str) -> int:
    if not shutil.which("pgrep"):
        return 0
    code, stdout, _stderr = _run_text_command(["pgrep", "-f", pattern], timeout=2.0, max_bytes=4096)
    if code != 0 or not stdout:
        return 0
    current = str(os.getpid())
    return sum(1 for line in stdout.splitlines() if line.strip() and line.strip() != current)


def _allowed_service_name(value: Any) -> str:
    service = str(value or "phone-worker").strip().lower().replace("_", "-")
    service = re.sub(r"[^a-z0-9.-]+", "-", service).strip("-.")
    aliases = {
        "worker": "phone-worker",
        "core-worker": "phone-worker",
        "phone": "phone-worker",
        "watch": "phone-worker-watch",
        "watchdog": "phone-worker-watch",
        "music": "music-agent",
        "music-agent": "music-agent",
        "musicagent": "music-agent",
        "agent-music": "music-agent",
    }
    service = aliases.get(service, service)
    if service not in {"phone-worker", "phone-worker-watch", "music-agent", "tailscale"}:
        raise ValueError("serviço não permitido")
    return service


def _service_status(service: str) -> dict[str, Any]:
    service = _allowed_service_name(service)
    phone_session = str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker").strip() or "phone-worker"
    watch_session = str(os.getenv("PHONE_WORKER_WATCH_TMUX_SESSION") or "phone-worker-watch").strip() or "phone-worker-watch"
    if service == "phone-worker":
        status = _system_status()
        supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": True,
            "current_pid": os.getpid(),
            "tmux_session": phone_session,
            "tmux_running": _tmux_session_exists(phone_session),
            "processes": _pgrep_count("phone_worker.py"),
            "uptime_seconds": status.get("uptime_seconds"),
            "log_file": str(_phone_worker_log_file()),
            "scripts": _script_inventory(),
            "start_script": str(_best_script("start-phone-worker.sh")),
            "supervisor": supervisor,
            "pid_file": supervisor.get("pid_file"),
            "pid_file_pid": supervisor.get("pid_file_pid"),
            "duplicates": supervisor.get("duplicates"),
        }
    if service == "phone-worker-watch":
        watch_pid = _read_pid_file(_phone_worker_watch_pid_file())
        tmux_running = _tmux_session_exists(watch_session)
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": bool(_pid_alive(watch_pid) or tmux_running),
            "tmux_session": watch_session,
            "tmux_running": tmux_running,
            "watch_pid_file": str(_phone_worker_watch_pid_file()),
            "watch_pid": watch_pid,
            "watch_pid_alive": _pid_alive(watch_pid),
            "script": str(_best_script("watch-phone-worker.sh")),
            "scripts": _script_inventory(),
        }
    if service == "music-agent":
        pid = _read_pid_file(_music_agent_pid_file())
        snapshot = _safe_telemetry("music_agent", _music_agent_snapshot, {"ok": False, "available": False, "configured": False})
        running = bool(snapshot.get("available") or _pid_alive(pid) or _pgrep_count("music_agent.py") > 0)
        return {
            "ok": True,
            "service": service,
            "manageable": True,
            "running": running,
            "available": bool(snapshot.get("available")),
            "configured": bool(snapshot.get("configured")),
            "version": snapshot.get("version") or snapshot.get("runtime_version") or "",
            "file_version": snapshot.get("file_version") or "",
            "needs_restart": bool(snapshot.get("needs_restart")),
            "pid_file": str(_music_agent_pid_file()),
            "pid_file_pid": pid,
            "pid_file_alive": _pid_alive(pid),
            "processes": _pgrep_count("music_agent.py"),
            "script": str(_music_agent_start_script()),
            "log_file": str(_music_agent_log_file()),
            "health": snapshot,
        }
    tailscale = _tailscale_snapshot(probe_vps=True)
    return {
        "ok": True,
        "service": service,
        "manageable": False,
        "running": bool(tailscale.get("connected")),
        "tailscale": tailscale,
        "note": "controle start/stop do Tailscale oficial deve ser feito no app Android; o worker só diagnostica",
    }


def _run_service_action(service: str, action: str) -> dict[str, Any]:
    service = _allowed_service_name(service)
    action = str(action or "status").strip().lower().replace("-", "_")
    if action == "status":
        return _service_status(service)
    if action not in {"start", "stop", "restart"}:
        raise ValueError("ação de serviço não permitida")

    phone_session = str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker").strip() or "phone-worker"
    watch_session = str(os.getenv("PHONE_WORKER_WATCH_TMUX_SESSION") or "phone-worker-watch").strip() or "phone-worker-watch"
    start_script = _best_script("start-phone-worker.sh")
    watch_script = _best_script("watch-phone-worker.sh")

    if service == "tailscale":
        raise ValueError("Tailscale oficial no Android não pode ser iniciado/parado pelo Termux com segurança; use o app Tailscale")

    if service == "phone-worker-watch":
        if action in {"stop", "restart"}:
            if shutil.which("tmux"):
                _run_text_command(["tmux", "kill-session", "-t", watch_session], timeout=3.0, max_bytes=4096)
            watch_pid = _read_pid_file(_phone_worker_watch_pid_file())
            if watch_pid:
                with contextlib.suppress(Exception):
                    os.kill(int(watch_pid), 15)
            with contextlib.suppress(Exception):
                _phone_worker_watch_pid_file().unlink()
        if action in {"start", "restart"}:
            if not watch_script.exists():
                raise FileNotFoundError(str(watch_script))
            subprocess.Popen(["bash", str(watch_script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1)
        return _service_status(service) | {"action": action}

    if service == "music-agent":
        start_script = _music_agent_start_script()
        if action in {"stop", "restart"}:
            pid = _read_pid_file(_music_agent_pid_file())
            if pid:
                with contextlib.suppress(Exception):
                    os.kill(int(pid), 15)
            with contextlib.suppress(Exception):
                _music_agent_pid_file().unlink()
            for _ in range(4):
                if _pgrep_count("music_agent.py") <= 0:
                    break
                time.sleep(0.25)
            if _pgrep_count("music_agent.py") > 0:
                with contextlib.suppress(Exception):
                    _run_text_command(["pkill", "-f", "music_agent.py"], timeout=2.0, max_bytes=4096)
                time.sleep(0.5)
        result_extra: dict[str, Any] = {}
        if action in {"start", "restart"}:
            if not start_script.exists():
                raise FileNotFoundError(str(start_script))
            proc = subprocess.run(["bash", str(start_script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90.0)
            result_extra.update({
                "returncode": int(proc.returncode),
                "stdout": _sanitize_log_text(proc.stdout.decode("utf-8", errors="replace"), limit=3000),
                "stderr": _sanitize_log_text(proc.stderr.decode("utf-8", errors="replace"), limit=3000),
            })
        status = _service_status(service) | {"action": action, **result_extra}
        if result_extra.get("returncode") not in (None, 0) and not bool(status.get("available")):
            status["ok"] = False
        return status

    # phone-worker é o próprio processo atual. Parar/reiniciar precisa ser deferido
    # para o resultado do job conseguir voltar para a VPS antes do tmux/pkill.
    if action == "start":
        if not start_script.exists():
            raise FileNotFoundError(str(start_script))
        proc = subprocess.run([str(start_script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20.0)
        status = _service_status(service)
        status.update({
            "action": action,
            "returncode": int(proc.returncode),
            "stdout": _sanitize_log_text(proc.stdout.decode("utf-8", errors="replace"), limit=3000),
            "stderr": _sanitize_log_text(proc.stderr.decode("utf-8", errors="replace"), limit=3000),
        })
        if proc.returncode != 0:
            status["ok"] = False
        return status
    if action == "restart" and not start_script.exists():
        raise FileNotFoundError(str(start_script))
    status = _service_status(service)
    status.update({"action": action, "deferred": True, "message": f"{action} agendado após envio do resultado"})
    status["_deferred_phone_worker_action"] = action
    status["_deferred_phone_worker_session"] = phone_session
    status["_deferred_start_script"] = str(start_script)
    return status





def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_extract_zip_file(zip_path: Path, target_dir: Path, *, max_members: int = 6000) -> int:
    target_root = target_dir.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if count >= max_members:
                raise ValueError("source zip com arquivos demais")
            name = str(member.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"caminho inseguro no zip: {name[:80]}")
            destination = (target_root / name).resolve()
            if destination != target_root and not str(destination).startswith(str(target_root) + os.sep):
                raise ValueError(f"caminho fora da pasta do build: {name[:80]}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=128 * 1024)
            count += 1
    return count



def _is_termux_runtime() -> bool:
    prefix = os.getenv("PREFIX") or ""
    return "com.termux" in prefix or Path("/data/data/com.termux/files/usr").exists()


def _apk_self_builder_bundle_asset(project_dir: Path) -> Path:
    return project_dir / "app/src/main/assets/core-linux/android-builder/android-builder-toolchain.zip"


def _apk_self_builder_bundle_valid(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "path": str(path), "bytes": 0, "sha256": ""}
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        result["error"] = "bundle ausente ou pequeno"
        return result
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"entrada corrompida: {bad}")
            names = set(archive.namelist())
            if "manifest.json" not in names:
                raise ValueError("manifest.json ausente")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema") != "core-worker-android-builder-v1":
                raise ValueError("schema inválido")
            try:
                manifest_version = int(manifest.get("version") or 0)
            except Exception:
                manifest_version = 0
            if manifest_version < 5:
                raise ValueError("bundle antigo: regenere com launcher Gradle compatível com Android v5")
            runtime_libraries = manifest.get("runtimeLibraries") if isinstance(manifest.get("runtimeLibraries"), dict) else {}
            if runtime_libraries.get("strategy") != "dt-needed-transitive-v1":
                raise ValueError("estratégia de bibliotecas do autobuilder inválida")
            gradle_launcher = manifest.get("gradleLauncher") if isinstance(manifest.get("gradleLauncher"), dict) else {}
            if gradle_launcher.get("strategy") != "android-sh-unquoted-default-jvm-opts-v1":
                raise ValueError("launcher Gradle do autobuilder não é compatível com /system/bin/sh")
            bootstrap_smoke = manifest.get("bootstrapSmoke") if isinstance(manifest.get("bootstrapSmoke"), dict) else {}
            if bootstrap_smoke.get("ok") is not True:
                raise ValueError("smoke bootstrap do autobuilder ausente ou reprovado")
            arch = str(manifest.get("arch") or "").strip().lower()
            if arch not in {"aarch64", "arm64", "arm64-v8a"}:
                raise ValueError("arquitetura inválida")
            paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
            jdk = str(paths.get("jdk") or "jdk").strip("/")
            gradle = str(paths.get("gradle") or "gradle/bin/gradle").strip("/")
            sdk = str(paths.get("androidSdk") or paths.get("android_sdk") or "android-sdk").strip("/")
            aapt2 = str(paths.get("aapt2") or "bin/aapt2").strip("/")
            required = {
                f"{jdk}/bin/java": 64 * 1024,
                f"{jdk}/bin/javac": 8 * 1024,
                f"{jdk}/bin/jar": 8 * 1024,
                gradle: 100,
                f"{sdk}/platforms/android-34/android.jar": 1024 * 1024,
                aapt2: 64 * 1024,
            }
            for name, minimum in required.items():
                info = archive.getinfo(name) if name in names else None
                if info is None or info.is_dir() or int(info.file_size or 0) < minimum:
                    raise ValueError(f"arquivo obrigatório ausente/pequeno: {name}")
            executable_paths = manifest.get("executablePaths") if isinstance(manifest.get("executablePaths"), list) else []
            executable_set = {str(item or "").replace("\\", "/").strip("/") for item in executable_paths}
            mandatory_exec = {f"{jdk}/bin/java", f"{jdk}/bin/javac", f"{jdk}/bin/jar", gradle, aapt2}
            missing_exec = sorted(name for name in mandatory_exec if name not in executable_set)
            if missing_exec:
                raise ValueError("bundle sem modos executáveis obrigatórios: " + ", ".join(missing_exec))
            jspawn = f"{jdk}/lib/jspawnhelper"
            if jspawn in names and jspawn not in executable_set:
                raise ValueError("bundle não preserva execução de jdk/lib/jspawnhelper")
            for executable in executable_set:
                if not executable or executable.startswith("/") or ".." in executable.split("/") or executable not in names:
                    raise ValueError(f"caminho executável inseguro/ausente no bundle: {executable}")
            for info in archive.infolist():
                name = str(info.filename or "").replace("\\", "/")
                if not name or name.startswith("/") or ".." in name.split("/"):
                    raise ValueError("caminho inseguro no bundle")
        result.update({
            "ok": True,
            "bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
            "manifest": manifest,
        })
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=240)}"
        return result


def _copy_tree_dereferenced(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        symlinks=False,
        dirs_exist_ok=True,
        ignore_dangling_symlinks=True,
        copy_function=shutil.copy2,
    )


_GRADLE_DEFAULT_JVM_OPTS_RE = re.compile(r"(?m)^(?P<prefix>[ \t]*DEFAULT_JVM_OPTS[ \t]*=)[^\r\n]*$")


def _parse_gradle_default_jvm_opts(raw_assignment: str) -> list[str]:
    """Decodifica as duas camadas de quoting usadas pelo launcher do Gradle."""
    try:
        outer = shlex.split(str(raw_assignment or ""), posix=True)
        values = shlex.split(outer[0], posix=True) if len(outer) == 1 else outer
    except ValueError as exc:
        raise ValueError(f"DEFAULT_JVM_OPTS inválido no launcher Gradle: {exc}") from exc
    if len(values) > 32:
        raise ValueError("DEFAULT_JVM_OPTS contém opções demais")
    forbidden = set(" \t\r\n'\"\\`$;&|<>(){}[]*?!#~")
    for value in values:
        if not value.startswith("-") or len(value) > 512 or any(char in forbidden for char in value):
            raise ValueError(f"opção JVM não portátil no launcher Gradle: {_short_text(value, limit=120)}")
    return values


def _patch_gradle_launcher_for_android(path: Path) -> dict[str, Any]:
    """Remove as aspas aninhadas que o ``/system/bin/sh`` repassa ao Java.

    O launcher Unix distribuído pelo Gradle costuma declarar
    ``DEFAULT_JVM_OPTS='\"-Xmx64m\" \"-Xms64m\"'``. No shell do Android essas
    aspas podem sobreviver ao pipeline ``xargs``/``eval`` do launcher e o Java
    tenta carregar ``\"-Xmx64m\"`` como classe principal. Preservamos todas as
    opções JVM simples declaradas pela distribuição, mas serializamos uma única
    lista sem aspas internas ou metacaracteres de shell.
    """
    if not path.is_file():
        raise FileNotFoundError(f"launcher Gradle ausente: {path}")
    original = path.read_text(encoding="utf-8", errors="strict")
    matches = list(_GRADLE_DEFAULT_JVM_OPTS_RE.finditer(original))
    if len(matches) != 1:
        raise ValueError(
            "launcher Gradle inesperado: esperado exatamente um DEFAULT_JVM_OPTS, "
            f"encontrado {len(matches)}"
        )
    original_assignment = matches[0].group(0).split("=", 1)[1]
    default_jvm_opts = _parse_gradle_default_jvm_opts(original_assignment)
    replacement = matches[0].group("prefix") + "'" + " ".join(default_jvm_opts) + "'"
    patched = _GRADLE_DEFAULT_JVM_OPTS_RE.sub(replacement, original, count=1)
    path.write_text(patched, encoding="utf-8")
    if not os.access(path, os.X_OK):
        raise ValueError("launcher Gradle perdeu permissão de execução após normalização")
    return {
        "strategy": "android-sh-unquoted-default-jvm-opts-v1",
        "defaultJvmOpts": default_jvm_opts,
        "changed": patched != original,
        "sha256": _sha256_path(path),
    }


def _find_termux_java_home(env: dict[str, str]) -> Path:
    candidates: list[Path] = []
    if env.get("JAVA_HOME"):
        candidates.append(Path(env["JAVA_HOME"]).expanduser())
    java_cmd = shutil.which("java", path=env.get("PATH")) or shutil.which("java")
    if java_cmd:
        resolved = Path(java_cmd).resolve()
        if resolved.parent.name == "bin":
            candidates.append(resolved.parent.parent)
    prefix = Path(env.get("PREFIX") or os.getenv("PREFIX") or "/data/data/com.termux/files/usr")
    for root in (prefix / "lib/jvm", prefix / "opt"):
        if root.is_dir():
            candidates.extend(path for path in sorted(root.glob("*"), reverse=True) if path.is_dir())
    for candidate in candidates:
        if all((candidate / f"bin/{name}").is_file() for name in ("java", "javac", "jar")):
            return candidate.resolve()
    raise FileNotFoundError("JDK completo não encontrado no Termux (java/javac/jar)")


def _find_termux_gradle_home(env: dict[str, str]) -> Path:
    candidates: list[Path] = []
    if env.get("GRADLE_HOME"):
        candidates.append(Path(env["GRADLE_HOME"]).expanduser())
    prefix = Path(env.get("PREFIX") or os.getenv("PREFIX") or "/data/data/com.termux/files/usr")
    candidates.extend((prefix / "share/gradle", prefix / "opt/gradle"))
    gradle_cmd = shutil.which("gradle", path=env.get("PATH")) or shutil.which("gradle")
    if gradle_cmd:
        resolved = Path(gradle_cmd).resolve()
        if resolved.parent.name == "bin":
            candidates.append(resolved.parent.parent)
    for candidate in candidates:
        launcher = candidate / "bin/gradle"
        libs = list((candidate / "lib").glob("gradle-launcher-*.jar")) if (candidate / "lib").is_dir() else []
        if launcher.is_file() and libs:
            return candidate.resolve()
    raise FileNotFoundError("distribuição completa do Gradle não encontrada no Termux")


def _find_android_sdk_home(env: dict[str, str]) -> Path:
    candidates = [
        Path(str(env.get("ANDROID_HOME") or "")).expanduser(),
        Path(str(env.get("ANDROID_SDK_ROOT") or "")).expanduser(),
        Path.home() / "android-sdk",
    ]
    for candidate in candidates:
        if str(candidate) in {"", "."}:
            continue
        android_jar = candidate / "platforms/android-34/android.jar"
        if android_jar.is_file() and android_jar.stat().st_size > 1024 * 1024:
            return candidate.resolve()
    raise FileNotFoundError("Android SDK 34 não encontrado no Termux")


def _latest_android_build_tools(sdk: Path) -> Path | None:
    root = sdk / "build-tools"
    if not root.is_dir():
        return None
    candidates = [path for path in root.iterdir() if path.is_dir()]
    candidates.sort(key=lambda path: tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.-]", path.name)), reverse=True)
    return candidates[0] if candidates else None


def _zip_directory_deterministic(source: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    entries = 0
    expanded = 0
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError("caminho inseguro ao gerar toolchain")
            entries += 1
            expanded += path.stat().st_size
            if entries > 50_000:
                raise ValueError("toolchain contém arquivos demais")
            if expanded > 4 * 1024 * 1024 * 1024:
                raise ValueError("toolchain expandido excede 4 GiB")
            already_compressed = path.suffix.lower() in {".zip", ".jar", ".apk", ".so", ".gz", ".xz", ".zst", ".7z", ".jmod"}
            archive.write(
                path,
                rel,
                compress_type=zipfile.ZIP_STORED if already_compressed else zipfile.ZIP_DEFLATED,
                compresslevel=None if already_compressed else 6,
            )
    if target.exists():
        target.unlink()
    temp.replace(target)
    return {"entries": entries, "expandedBytes": expanded, "bytes": target.stat().st_size, "sha256": _sha256_path(target)}



_TERMUX_ANDROID_SYSTEM_LIBRARIES = frozenset({
    "libandroid.so",
    "libbinder_ndk.so",
    "libc.so",
    "libdl.so",
    "libEGL.so",
    "libGLESv2.so",
    "libjnigraphics.so",
    "liblog.so",
    "libm.so",
    "libmediandk.so",
    "libnativewindow.so",
    "libOpenSLES.so",
    "libstdc++.so",
    "libsync.so",
    "libvulkan.so",
    "libz.so",
})
_ELF_NEEDED_RE = re.compile(r"\(NEEDED\).*?\[([^\]]+)\]")
_ELF_SONAME_RE = re.compile(r"\(SONAME\).*?\[([^\]]+)\]")


def _elf_header(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()[:64]
    except Exception:
        return {"ok": False, "aarch64": False}
    if len(raw) < 20 or raw[:4] != b"\x7fELF":
        return {"ok": False, "aarch64": False}
    endian = "little" if raw[5] == 1 else "big"
    try:
        machine = int.from_bytes(raw[18:20], endian)
    except Exception:
        machine = -1
    return {
        "ok": True,
        "class": int(raw[4]),
        "machine": machine,
        "aarch64": int(raw[4]) == 2 and machine == 183,
    }


def _find_elf_inspector(env: dict[str, str]) -> list[str]:
    configured = str(env.get("PHONE_WORKER_ELF_INSPECTOR") or os.getenv("PHONE_WORKER_ELF_INSPECTOR") or "").strip()
    candidates: list[list[str]] = []
    if configured:
        candidates.append(shlex.split(configured))
    path_value = env.get("PATH") or os.getenv("PATH")
    for name in ("readelf", "llvm-readelf"):
        found = shutil.which(name, path=path_value) or shutil.which(name)
        if found:
            candidates.append([found])
    for candidate in candidates:
        try:
            completed = subprocess.run(
                [*candidate, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                return candidate
        except Exception:
            continue
    # O bootstrap não deve depender de um pacote adicional do Termux. Quando
    # readelf não estiver instalado, o parser ELF64 abaixo lê DT_NEEDED/SONAME
    # diretamente com a biblioteca padrão do Python.
    return []


def _read_elf_dynamic_python(path: Path) -> dict[str, Any]:
    import struct

    header = _elf_header(path)
    if not header.get("aarch64"):
        return {"ok": False, "needed": [], "soname": "", "error": "não é ELF64 AArch64"}
    try:
        with path.open("rb") as handle:
            ident = handle.read(16)
            if len(ident) != 16 or ident[:4] != b"\x7fELF" or ident[4] != 2:
                raise ValueError("cabeçalho ELF64 inválido")
            endian = "<" if ident[5] == 1 else ">" if ident[5] == 2 else ""
            if not endian:
                raise ValueError("endianness ELF inválida")
            rest = handle.read(48)
            if len(rest) != 48:
                raise ValueError("cabeçalho ELF truncado")
            values = struct.unpack(endian + "HHIQQQIHHHHHH", rest)
            phoff = int(values[4])
            phentsize = int(values[8])
            phnum = int(values[9])
            if phentsize < 56 or phnum <= 0 or phnum > 4096:
                raise ValueError("tabela de program headers inválida")

            loads: list[tuple[int, int, int, int]] = []
            dynamic: tuple[int, int] | None = None
            for index in range(phnum):
                handle.seek(phoff + index * phentsize)
                raw = handle.read(56)
                if len(raw) != 56:
                    raise ValueError("program header truncado")
                p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, p_memsz, _align = struct.unpack(
                    endian + "IIQQQQQQ", raw
                )
                if p_type == 1:  # PT_LOAD
                    loads.append((int(p_vaddr), int(p_memsz), int(p_offset), int(p_filesz)))
                elif p_type == 2:  # PT_DYNAMIC
                    dynamic = (int(p_offset), int(p_filesz))
            if dynamic is None:
                return {"ok": True, "needed": [], "soname": "", "inspector": "python-elf64-dynamic-v1"}

            dynamic_offset, dynamic_size = dynamic
            if dynamic_size <= 0 or dynamic_size > 64 * 1024 * 1024:
                raise ValueError("seção dinâmica ELF inválida")
            handle.seek(dynamic_offset)
            raw_dynamic = handle.read(dynamic_size)
            if len(raw_dynamic) != dynamic_size:
                raise ValueError("seção dinâmica ELF truncada")

            needed_offsets: list[int] = []
            soname_offset: int | None = None
            string_vaddr: int | None = None
            string_size = 0
            entry_size = 16
            for offset in range(0, len(raw_dynamic) - entry_size + 1, entry_size):
                tag, value = struct.unpack_from(endian + "qQ", raw_dynamic, offset)
                if tag == 0:  # DT_NULL
                    break
                if tag == 1:  # DT_NEEDED
                    needed_offsets.append(int(value))
                elif tag == 5:  # DT_STRTAB
                    string_vaddr = int(value)
                elif tag == 10:  # DT_STRSZ
                    string_size = int(value)
                elif tag == 14:  # DT_SONAME
                    soname_offset = int(value)
            if string_vaddr is None:
                raise ValueError("DT_STRTAB ausente")

            string_file_offset: int | None = None
            for vaddr, memsz, file_offset, filesz in loads:
                if vaddr <= string_vaddr < vaddr + memsz:
                    delta = string_vaddr - vaddr
                    if delta >= filesz:
                        continue
                    string_file_offset = file_offset + delta
                    break
            if string_file_offset is None:
                raise ValueError("DT_STRTAB não pertence a segmento carregável")
            if string_size <= 0:
                string_size = 16 * 1024 * 1024
            string_size = min(string_size, 16 * 1024 * 1024)
            handle.seek(string_file_offset)
            string_table = handle.read(string_size)
            if not string_table:
                raise ValueError("tabela de strings ELF vazia")

            def read_string(offset: int | None) -> str:
                if offset is None or offset < 0 or offset >= len(string_table):
                    return ""
                end = string_table.find(b"\0", offset)
                if end < 0:
                    end = len(string_table)
                return string_table[offset:end].decode("utf-8", errors="replace").strip()

            needed: list[str] = []
            for offset in needed_offsets:
                value = read_string(offset)
                if value and value not in needed:
                    needed.append(value)
            return {
                "ok": True,
                "needed": needed,
                "soname": read_string(soname_offset),
                "inspector": "python-elf64-dynamic-v1",
            }
    except Exception as exc:
        return {
            "ok": False,
            "needed": [],
            "soname": "",
            "error": f"{type(exc).__name__}: {_short_text(exc, limit=220)}",
        }


def _read_elf_dynamic(path: Path, inspector: list[str]) -> dict[str, Any]:
    if not inspector:
        return _read_elf_dynamic_python(path)
    if not _elf_header(path).get("aarch64"):
        return {"ok": False, "needed": [], "soname": "", "error": "não é ELF64 AArch64"}
    last_error = ""
    for flags in (("-dW",), ("-d",)):
        try:
            completed = subprocess.run(
                [*inspector, *flags, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=20,
                check=False,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {_short_text(exc, limit=180)}"
            continue
        output = completed.stdout or ""
        if completed.returncode != 0:
            last_error = _short_text(output, limit=240) or f"readelf rc={completed.returncode}"
            continue
        needed = []
        for value in _ELF_NEEDED_RE.findall(output):
            clean = str(value or "").strip()
            if clean and clean not in needed:
                needed.append(clean)
        soname_match = _ELF_SONAME_RE.search(output)
        return {
            "ok": True,
            "needed": needed,
            "soname": str(soname_match.group(1) if soname_match else "").strip(),
            "inspector": " ".join(inspector),
        }
    fallback = _read_elf_dynamic_python(path)
    if fallback.get("ok"):
        return fallback
    return {
        "ok": False,
        "needed": [],
        "soname": "",
        "error": last_error or fallback.get("error") or "falha lendo seção dinâmica ELF",
    }


def _elf_index(roots: list[Path], *, max_files: int = 30_000) -> dict[str, list[Path]]:
    """Indexa bibliotecas ELF por SONAME esperado sem varrer arquivos irrelevantes.

    O diretório ``$PREFIX/lib`` também contém Python, pkgconfig, fontes e outros
    milhares de arquivos. Priorizar ``*.so*`` evita atingir o limite antes das
    bibliotecas de topo que realmente podem aparecer em ``DT_NEEDED``.
    """
    index: dict[str, list[Path]] = {}
    seen_paths: set[str] = set()
    scanned = 0
    for root in roots:
        if not root.is_dir():
            continue
        candidates: list[Path] = []
        try:
            candidates.extend(sorted(root.glob("*.so*")))
            candidates.extend(sorted(root.rglob("*.so*")))
        except Exception:
            continue
        for path in candidates:
            if scanned >= max_files:
                raise ValueError(f"índice ELF excedeu {max_files} bibliotecas candidatas em {root}")
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            key = str(path.absolute())
            if key in seen_paths:
                continue
            seen_paths.add(key)
            scanned += 1
            if not _elf_header(path).get("aarch64"):
                continue
            index.setdefault(path.name, []).append(path)
    for values in index.values():
        values.sort(key=lambda item: (len(item.parts), len(str(item)), str(item)))
    return index


def _required_jdk_elf_seeds(jdk_home: Path) -> list[Path]:
    """Retorna todos os ELF AArch64 do JDK, com binários essenciais primeiro.

    O JDK carrega algumas bibliotecas por ``dlopen`` somente durante compilação
    (fontes, ZIP, NIO, instrumentação etc.). Inspecionar apenas ``java`` e
    ``javac`` passaria no smoke de versão, mas poderia falhar no primeiro
    ``assembleDebug``. Ainda assim, o escopo permanece mínimo: auditamos apenas
    arquivos nativos dentro do JDK efetivamente empacotado, nunca todo PREFIX/lib.
    """
    required = tuple(jdk_home / f"bin/{name}" for name in ("java", "javac", "jar"))
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"arquivo obrigatório ausente no JDK do self-builder: {path}")

    seeds: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        if not candidate.is_file() or not _elf_header(candidate).get("aarch64"):
            return
        try:
            key = str(candidate.resolve())
        except Exception:
            key = str(candidate.absolute())
        if key in seen:
            return
        seen.add(key)
        seeds.append(candidate)

    for path in required:
        add(path)
    scanned = 0
    for candidate in sorted(jdk_home.rglob("*")):
        if scanned >= 12_000:
            raise ValueError("JDK do self-builder contém arquivos demais para auditoria ELF segura")
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        scanned += 1
        add(candidate)

    if not seeds:
        raise ValueError("JDK encontrado, mas nenhum executável ELF64 AArch64 foi detectado")
    return seeds


def _collect_minimal_termux_runtime_libraries(
    *,
    jdk_home: Path,
    aapt2_path: Path,
    prefix: Path,
    target: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    """Copia apenas dependências DT_NEEDED externas do JDK/aapt2.

    A implementação anterior copiava todo o ``$PREFIX/lib``. Além de incluir LLVM,
    FFmpeg, Python e bibliotecas de outros pacotes, links simbólicos podiam duplicar
    o mesmo arquivo. O self-builder precisa somente das dependências transitivas dos
    executáveis que realmente roda.
    """
    inspector = _find_elf_inspector(env)
    internal_index = _elf_index([jdk_home], max_files=12_000)
    external_roots = [prefix / "lib", prefix / "lib64"]
    external_index = _elf_index(external_roots, max_files=30_000)
    target.mkdir(parents=True, exist_ok=True)

    seeds = _required_jdk_elf_seeds(jdk_home)
    if not aapt2_path.is_file() or not _elf_header(aapt2_path).get("aarch64"):
        raise ValueError("aapt2 do bootstrap não é ELF64 AArch64 válido")
    seeds.append(aapt2_path)

    queue: list[tuple[Path, str]] = [(path, "seed") for path in seeds]
    visited_files: set[str] = set()
    copied_names: dict[str, Path] = {}
    dependency_parents: dict[str, set[str]] = {}
    system_provided: set[str] = set()
    inspect_errors: list[str] = []

    while queue:
        current, origin = queue.pop(0)
        try:
            canonical = current.resolve()
        except Exception:
            canonical = current.absolute()
        visit_key = str(canonical)
        if visit_key in visited_files:
            continue
        visited_files.add(visit_key)
        dynamic = _read_elf_dynamic(current, inspector)
        if not dynamic.get("ok"):
            inspect_errors.append(f"{current.name}: {dynamic.get('error') or 'falha ELF'}")
            continue
        for needed in dynamic.get("needed") or []:
            name = str(needed or "").strip()
            if not name or "/" in name or "\\" in name:
                continue
            dependency_parents.setdefault(name, set()).add(current.name)
            internal_candidates = internal_index.get(name) or []
            if internal_candidates:
                queue.append((internal_candidates[0], "jdk"))
                continue
            external_candidates = external_index.get(name) or []
            if external_candidates:
                source = external_candidates[0]
                if name not in copied_names:
                    resolved = source.resolve()
                    destination = target / name
                    shutil.copy2(resolved, destination)
                    destination.chmod(0o644)
                    copied_names[name] = resolved
                queue.append((source, "termux-lib"))
                continue
            if name in _TERMUX_ANDROID_SYSTEM_LIBRARIES:
                system_provided.add(name)
                continue

    unresolved = sorted(
        name for name in dependency_parents
        if name not in copied_names
        and name not in system_provided
        and not (internal_index.get(name) or [])
    )
    if inspect_errors:
        raise ValueError("não consegui auditar todas as dependências ELF do self-builder: " + "; ".join(inspect_errors[:5]))
    if unresolved:
        detail = ", ".join(
            f"{name} (usada por {', '.join(sorted(dependency_parents.get(name) or []))})"
            for name in unresolved[:12]
        )
        raise FileNotFoundError("bibliotecas necessárias ao self-builder não foram encontradas no Termux: " + detail)

    copied = []
    total_bytes = 0
    for name, source in sorted(copied_names.items()):
        destination = target / name
        size = destination.stat().st_size
        total_bytes += size
        copied.append({"name": name, "bytes": size, "source": str(source)})
    max_bytes = max(64 * 1024 * 1024, _env_int("PHONE_WORKER_APK_SELF_BUILDER_RUNTIME_LIB_MAX_BYTES", 384 * 1024 * 1024))
    if total_bytes > max_bytes:
        largest = ", ".join(f"{item['name']}={item['bytes'] // (1024 * 1024)}MiB" for item in sorted(copied, key=lambda row: row["bytes"], reverse=True)[:8])
        raise ValueError(
            f"dependências mínimas do self-builder excedem {max_bytes // (1024 * 1024)} MiB "
            f"({total_bytes // (1024 * 1024)} MiB); maiores: {largest}"
        )
    return {
        "strategy": "dt-needed-transitive-v1",
        "inspector": " ".join(inspector) or "python-elf64-dynamic-v1",
        "count": len(copied),
        "bytes": total_bytes,
        "names": [item["name"] for item in copied],
        "systemProvided": sorted(system_provided),
        "seeds": [str(path.relative_to(jdk_home)) if _is_inside_path(path, jdk_home) else path.name for path in seeds],
        "largest": [
            {"name": item["name"], "bytes": item["bytes"]}
            for item in sorted(copied, key=lambda row: row["bytes"], reverse=True)[:12]
        ],
        "candidateLibraries": sum(len(values) for values in external_index.values()),
    }


def _is_inside_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _apk_self_builder_executable_paths(bundle_root: Path) -> list[str]:
    paths: list[str] = []
    for candidate in sorted(bundle_root.rglob("*")):
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            rel = candidate.relative_to(bundle_root).as_posix()
        except Exception:
            continue
        if rel and not rel.startswith("/") and ".." not in rel.split("/"):
            paths.append(rel)
    mandatory = ["jdk/bin/java", "jdk/bin/javac", "jdk/bin/jar", "gradle/bin/gradle", "bin/aapt2"]
    missing = [name for name in mandatory if name not in paths]
    if missing:
        raise ValueError("toolchain gerado perdeu modos executáveis: " + ", ".join(missing))
    jspawn = bundle_root / "jdk/lib/jspawnhelper"
    if jspawn.is_file() and "jdk/lib/jspawnhelper" not in paths:
        raise ValueError("toolchain gerado perdeu modo executável de jdk/lib/jspawnhelper")
    return paths


def _smoke_apk_self_builder_bundle(bundle_root: Path) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_APK_SELF_BUILDER_SMOKE", True):
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}
    jdk = bundle_root / "jdk"
    sdk = bundle_root / "android-sdk"
    gradle = bundle_root / "gradle/bin/gradle"
    aapt2 = bundle_root / "bin/aapt2"
    shell = Path("/system/bin/sh") if Path("/system/bin/sh").is_file() else Path("/bin/sh")
    runtime = bundle_root.parent / "smoke-runtime"
    shutil.rmtree(runtime, ignore_errors=True)
    home = runtime / "home"
    temp = runtime / "tmp"
    gradle_home = runtime / "gradle-home"
    for path in (home, temp, gradle_home):
        path.mkdir(parents=True, exist_ok=True)
    library_paths = [
        bundle_root / "runtime-libs",
        jdk / "lib",
        jdk / "lib/server",
        jdk / "lib/jli",
    ]
    clean_env = {
        "HOME": str(home),
        "TMPDIR": str(temp),
        "GRADLE_USER_HOME": str(gradle_home),
        "JAVA_HOME": str(jdk),
        "ANDROID_HOME": str(sdk),
        "ANDROID_SDK_ROOT": str(sdk),
        "PATH": os.pathsep.join((str(jdk / "bin"), str(sdk / "platform-tools"), "/system/bin", "/system/xbin")),
        "LD_LIBRARY_PATH": os.pathsep.join(str(path) for path in library_paths if path.is_dir()),
        "LANG": "C",
        "LC_ALL": "C",
    }
    commands = [
        ("java", [str(jdk / "bin/java"), "-version"], 60),
        ("javac", [str(jdk / "bin/javac"), "-version"], 60),
        ("jar", [str(jdk / "bin/jar"), "--version"], 60),
        ("gradle", [str(shell), str(gradle), "--version", "--no-daemon"], 120),
        ("aapt2", [str(aapt2), "version"], 60),
    ]
    checks: list[dict[str, Any]] = []
    try:
        for name, command, timeout in commands:
            started = time.time()
            completed = subprocess.run(
                command,
                env=clean_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            output = _short_text(completed.stdout, limit=1800)
            check = {
                "name": name,
                "ok": completed.returncode == 0,
                "returncode": int(completed.returncode),
                "durationMs": int((time.time() - started) * 1000),
                "output": output,
            }
            checks.append(check)
            if not check["ok"]:
                raise RuntimeError(f"smoke {name} falhou (rc={completed.returncode}): {output}")
        return {"ok": True, "checks": checks}
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def _prepare_apk_self_builder_toolchain(project_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Gera no Termux o bundle que o APK usará nos builds seguintes.

    A VPS nunca executa essa preparação. O bundle é criado dentro do workspace
    temporário do phone-worker, entra no APK bootstrap e depois é retido pelo app.
    """
    asset = _apk_self_builder_bundle_asset(project_dir)
    existing = _apk_self_builder_bundle_valid(asset)
    rebuild = _env_bool("PHONE_WORKER_APK_SELF_BUILDER_REBUILD", False)
    if existing.get("ok") and not rebuild:
        return {**existing, "generated": False, "source": "project_asset"}
    if not _is_termux_runtime():
        raise RuntimeError("toolchain self-builder ausente e geração bootstrap só é permitida no Termux")

    jdk_home = _find_termux_java_home(env)
    gradle_home = _find_termux_gradle_home(env)
    sdk_home = _find_android_sdk_home(env)
    aapt2_cmd = shutil.which("aapt2", path=env.get("PATH")) or shutil.which("aapt2")
    if not aapt2_cmd or not Path(aapt2_cmd).is_file():
        raise FileNotFoundError("aapt2 compatível com Termux não encontrado")

    stage_root = project_dir / "app/build/core-worker-self-builder-toolchain"
    bundle_root = stage_root / "bundle"
    shutil.rmtree(stage_root, ignore_errors=True)
    bundle_root.mkdir(parents=True, exist_ok=True)
    try:
        _copy_tree_dereferenced(jdk_home, bundle_root / "jdk")
        _copy_tree_dereferenced(gradle_home, bundle_root / "gradle")
        gradle_launcher_report = _patch_gradle_launcher_for_android(bundle_root / "gradle/bin/gradle")

        sdk_target = bundle_root / "android-sdk"
        _copy_tree_dereferenced(sdk_home / "platforms/android-34", sdk_target / "platforms/android-34")
        build_tools = _latest_android_build_tools(sdk_home)
        if build_tools is not None:
            _copy_tree_dereferenced(build_tools, sdk_target / "build-tools" / build_tools.name)
        for optional in ("platform-tools", "licenses"):
            source = sdk_home / optional
            if source.is_dir():
                _copy_tree_dereferenced(source, sdk_target / optional)

        bin_dir = bundle_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(aapt2_cmd).resolve(), bin_dir / "aapt2")

        prefix = Path(env.get("PREFIX") or os.getenv("PREFIX") or "/data/data/com.termux/files/usr")
        runtime_libs = bundle_root / "runtime-libs"
        runtime_library_report = _collect_minimal_termux_runtime_libraries(
            jdk_home=jdk_home,
            aapt2_path=Path(aapt2_cmd),
            prefix=prefix,
            target=runtime_libs,
            env=env,
        )
        smoke = _smoke_apk_self_builder_bundle(bundle_root)
        executable_paths = _apk_self_builder_executable_paths(bundle_root)

        manifest = {
            "schema": "core-worker-android-builder-v1",
            "version": 5,
            "arch": "aarch64",
            "runtime": "termux-bionic-direct",
            "generatedBy": f"phone-worker-{PHONE_WORKER_VERSION}",
            "createdAt": int(time.time()),
            "paths": {
                "jdk": "jdk",
                "gradle": "gradle/bin/gradle",
                "androidSdk": "android-sdk",
                "aapt2": "bin/aapt2",
                "runtimeLibs": "runtime-libs",
            },
            "sdk": {
                "compileSdk": 34,
                "buildTools": build_tools.name if build_tools is not None else "",
            },
            "runtimeLibraries": runtime_library_report,
            "gradleLauncher": gradle_launcher_report,
            "bootstrapSmoke": smoke,
            "executablePaths": executable_paths,
            "safety": {
                "generatedOnTermux": True,
                "vpsBuild": False,
                "noRemoteShell": True,
                "privateApkOnly": True,
                "targetSdkMax": 28,
            },
        }
        (bundle_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        generated = _zip_directory_deterministic(bundle_root, asset)
        validated = _apk_self_builder_bundle_valid(asset)
        if not validated.get("ok"):
            raise RuntimeError("bundle self-builder gerado mas inválido: " + str(validated.get("error") or "erro desconhecido"))
        return {
            **validated,
            **generated,
            "generated": True,
            "source": "termux_bootstrap",
            "jdk": str(jdk_home),
            "gradle": str(gradle_home),
            "androidSdk": str(sdk_home),
            "aapt2": str(aapt2_cmd),
            "runtimeLibraries": runtime_library_report,
            "gradleLauncher": gradle_launcher_report,
            "bootstrapSmoke": smoke,
        }
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _prepare_termux_android_build(project_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Ajustes seguros para build Android dentro do Termux.

    O Android Gradle Plugin tenta baixar um aapt2 Linux comum que não roda no
    Android/Termux. Quando existir aapt2 do Termux, forçamos o override global.
    Também permitimos fallback para SDK 34, que é o caminho que funcionou no
    builder do usuário.
    """
    info: dict[str, Any] = {"termux": _is_termux_runtime()}
    if not info["termux"] or not _env_bool("PHONE_WORKER_APK_BUILD_TERMUX_TWEAKS", True):
        return info

    default_sdk = Path.home() / "android-sdk"
    if not env.get("ANDROID_HOME") and default_sdk.exists():
        env["ANDROID_HOME"] = str(default_sdk)
        env.setdefault("ANDROID_SDK_ROOT", str(default_sdk))
        env["PATH"] = f"{default_sdk}/cmdline-tools/latest/bin:{default_sdk}/platform-tools:" + env.get("PATH", "")
        info["android_home"] = str(default_sdk)

    sdk_fallback = str(os.getenv("PHONE_WORKER_APK_BUILD_TERMUX_SDK") or "34").strip()
    android_home = Path(env.get("ANDROID_HOME") or env.get("ANDROID_SDK_ROOT") or default_sdk).expanduser()
    if sdk_fallback:
        android_jar = android_home / "platforms" / f"android-{sdk_fallback}" / "android.jar"
        info["android_jar"] = str(android_jar)
        info["android_jar_ok"] = bool(android_jar.is_file() and android_jar.stat().st_size > 1024 * 1024)
    build_gradle = project_dir / "app" / "build.gradle"
    if sdk_fallback and build_gradle.exists():
        text = build_gradle.read_text(encoding="utf-8", errors="ignore")
        changed = re.sub(r"(compileSdk\s*=?\s*)\d+", rf"\g<1>{sdk_fallback}", text)
        changed = re.sub(r"(targetSdk\s*=?\s*)\d+", rf"\g<1>{sdk_fallback}", changed)
        if changed != text:
            build_gradle.write_text(changed, encoding="utf-8")
            info["sdk_fallback"] = sdk_fallback

    aapt2_path = shutil.which("aapt2")
    if aapt2_path:
        gradle_dir = Path.home() / ".gradle"
        gradle_dir.mkdir(parents=True, exist_ok=True)
        props = gradle_dir / "gradle.properties"
        line = f"android.aapt2FromMavenOverride={aapt2_path}"
        lines = props.read_text(encoding="utf-8", errors="ignore").splitlines() if props.exists() else []
        replaced = False
        new_lines = []
        for existing in lines:
            if existing.startswith("android.aapt2FromMavenOverride="):
                new_lines.append(line)
                replaced = True
            else:
                new_lines.append(existing)
        if not replaced:
            new_lines.append(line)
        props.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")
        info["aapt2_override"] = aapt2_path
    return info



def _apk_build_safe_slug(value: Any, *, fallback: str = "apk-build") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
    return (clean or fallback)[:96]


def _apk_build_logs_dir(build_root: Path) -> Path:
    path = (build_root / "logs").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tail_text_file(path: Path, *, limit: int = 12000) -> str:
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    if len(raw) > max(1024, limit):
        raw = raw[-max(1024, limit):]
    return _sanitize_log_text(raw.decode("utf-8", errors="replace"), limit=limit)


def _apk_build_lock_path(build_root: Path) -> Path:
    build_root.mkdir(parents=True, exist_ok=True)
    return build_root / ".apk-build.lock"


def _try_acquire_apk_build_file_lock(build_root: Path) -> tuple[Any | None, dict[str, Any]]:
    """Lock cross-process para evitar dois Gradle/NDK ao mesmo tempo no Termux."""
    lock_path = _apk_build_lock_path(build_root)
    info: dict[str, Any] = {"lock_path": str(lock_path)}
    try:
        fh = lock_path.open("a+", encoding="utf-8")
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
        return None, info
    try:
        import fcntl  # Linux/Termux
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            info["holder"] = _short_text(fh.read(), limit=240)
            fh.close()
            return None, info
        except OSError as exc:
            fh.seek(0)
            info["holder"] = _short_text(fh.read(), limit=240)
            info["error"] = f"{type(exc).__name__}: {_short_text(exc, limit=120)}"
            fh.close()
            return None, info
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "started_at": time.time(), "version": PHONE_WORKER_VERSION}, ensure_ascii=False))
        fh.flush()
        return fh, info
    except Exception as exc:
        # Fallback intra-processo quando fcntl não estiver disponível.
        info["warning"] = f"file-lock indisponível: {type(exc).__name__}: {_short_text(exc, limit=120)}"
        return fh, info


def _release_apk_build_file_lock(handle: Any | None) -> None:
    if handle is None:
        return
    with contextlib.suppress(Exception):
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()


def _cleanup_old_apk_build_logs(build_root: Path, *, keep_logs: int | None = None) -> None:
    keep = max(3, int(keep_logs if keep_logs is not None else _env_int("PHONE_WORKER_APK_BUILD_KEEP_LOGS", 20)))
    log_dir = build_root / "logs"
    if log_dir.is_dir():
        logs = sorted(log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        for path in logs[keep:]:
            with contextlib.suppress(Exception):
                path.unlink()
    max_dirs = max(0, _env_int("PHONE_WORKER_APK_BUILD_KEEP_WORKDIRS", 2))
    dirs = sorted([p for p in build_root.glob("build-*") if p.is_dir()], key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for path in dirs[max_dirs:]:
        marker = path / ".build-active"
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < 6 * 3600:
                continue
        with contextlib.suppress(Exception):
            shutil.rmtree(path)


def _cleanup_old_apk_build_artifacts(build_root: Path, *, keep_apks: int | None = None) -> dict[str, Any]:
    """Remove APKs antigos do builder sem tocar no latest nem no build atual.

    O diretório de artifacts cresce rápido no celular. A limpeza mantém os N APKs
    mais recentes e seus JSONs irmãos. O resultado é apenas telemetria; falhas de
    unlink são ignoradas para nunca quebrar um build publicado.
    """
    artifact_dir = build_root / "artifacts"
    keep = max(3, int(keep_apks if keep_apks is not None else _env_int("PHONE_WORKER_APK_BUILD_KEEP_ARTIFACTS", 12)))
    result: dict[str, Any] = {"enabled": True, "keep": keep, "removed": 0, "removedBytes": 0}
    if not artifact_dir.is_dir():
        result["enabled"] = False
        return result
    apks = sorted(artifact_dir.glob("*.apk"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    keep_set = {p.resolve() for p in apks[:keep] if p.exists()}
    latest_meta = artifact_dir / "latest-artifact.json"
    for apk in apks[keep:]:
        if not apk.exists() or apk.resolve() in keep_set:
            continue
        candidates = [apk, apk.with_suffix(apk.suffix + ".json")]
        for item in candidates:
            if item == latest_meta or not item.exists():
                continue
            try:
                size = item.stat().st_size
                item.unlink()
                result["removed"] += 1
                result["removedBytes"] += size
            except Exception:
                pass
    return result


def _summarize_gradle_log(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"summary": "erro de Gradle sem log persistente", "permanent": False, "detail": ""}
    text = _tail_text_file(path, limit=70000)
    lowered = text.lower()
    patterns = [
        r"execution failed for task '[^']+'",
        r"c/c\+\+: .+",
        r"cmake[^\n]+syntax error[^\n]+",
        r"cmake error[^\n]*",
        r"ninja:[^\n]+",
        r"aapt2[^\n]+",
        r"manifest merger failed[^\n]*",
        r"outofmemoryerror[^\n]*",
        r"no space left[^\n]*",
    ]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = _short_text(match.group(0), limit=220)
            if value and value not in hits:
                hits.append(value)
            if len(hits) >= 4:
                break
        if len(hits) >= 4:
            break
    permanent = any(fragment in lowered for fragment in (
        'syntax error: ")" unexpected',
        "cmake",
        "manifest merger failed",
        "google-services.json",
        "assinatura compatível",
    ))
    if 'syntax error: ")" unexpected' in lowered and "cmake" in lowered:
        summary = "CMake do Android SDK incompatível com Termux/Android; use executor prebuilt via jniLibs"
    elif hits:
        summary = "build do APK falhou: " + hits[0]
    else:
        summary = "build do APK falhou; veja gradle_log_tail"
    return {"summary": _short_text(summary, limit=180), "permanent": permanent, "detail": " | ".join(hits[:4])}


def _apk_build_failure_result(
    *,
    summary: str,
    version_name: str,
    version_code: int,
    source_fingerprint: str,
    source_sha256: str,
    notification_id: str,
    work_dir: Path,
    gradle_log: Path | None = None,
    returncode: int | None = None,
    error: str = "",
    builder_environment: dict[str, Any] | None = None,
    native_environment: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "summary": _short_text(summary, limit=180),
        "versionName": version_name,
        "versionCode": int(version_code or 0),
        "sourceFingerprint": source_fingerprint,
        "sourceSha256": source_sha256,
        "notificationId": notification_id,
        "phoneWorkerVersion": PHONE_WORKER_VERSION,
        "phoneWorkerSourceHash": _phone_worker_source_hash(),
        "work_dir": str(work_dir),
    }
    if returncode is not None:
        result["returncode"] = int(returncode)
    if error:
        result["error"] = _short_text(error, limit=400)
    if gradle_log is not None:
        result["gradle_log_path"] = str(gradle_log)
        tail = _tail_text_file(gradle_log, limit=16000)
        if tail:
            result["gradle_log_tail"] = tail[-12000:]
            result["stdout_tail"] = tail[-9000:]
    if builder_environment is not None:
        result["builder_environment"] = builder_environment
    if native_environment is not None:
        result["native_build"] = native_environment
    if extra:
        result.update(extra)
    return result


def _install_google_services_from_payload(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Grava google-services.json recebido pelo canal autenticado do job.

    O arquivo não vem no ZIP público e não deve ir para GitHub. A VPS envia o
    conteúdo em base64 no payload do job; o worker grava somente no workspace
    temporário de build e ele é removido junto com o work_dir ao final.
    """
    target = project_dir / "app" / "google-services.json"
    raw_b64 = str(payload.get("googleServicesJsonB64") or payload.get("google_services_json_b64") or "").strip()
    expected_sha = str(payload.get("googleServicesSha256") or payload.get("google_services_sha256") or "").strip().lower()
    expected_package = str(payload.get("googleServicesPackage") or payload.get("google_services_package") or "dev.core.worker").strip() or "dev.core.worker"

    if raw_b64:
        try:
            raw = base64.b64decode(raw_b64.encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError(f"google-services payload inválido: {type(exc).__name__}: {_short_text(exc, limit=100)}") from exc
        if len(raw) > 512 * 1024:
            raise ValueError("google-services payload grande demais")
        actual_sha = hashlib.sha256(raw).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            raise ValueError("sha256 do google-services.json divergente no payload")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        with contextlib.suppress(Exception):
            target.chmod(0o600)
    elif not target.is_file():
        raise FileNotFoundError(
            "google-services.json ausente no pacote de build. A VPS deve enviar googleServicesJsonB64 no payload do job; "
            "não coloque esse arquivo no GitHub."
        )
    raw = target.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"google-services.json inválido no workspace: {type(exc).__name__}: {_short_text(exc, limit=100)}") from exc
    if not isinstance(data, dict):
        raise ValueError("google-services.json inválido: raiz não é objeto JSON")
    project_info = data.get("project_info") if isinstance(data.get("project_info"), dict) else {}
    project_id = str(project_info.get("project_id") or "").strip()
    clients = data.get("client") if isinstance(data.get("client"), list) else []
    matched = False
    has_app_id = False
    has_api_key = False
    for client in clients:
        if not isinstance(client, dict):
            continue
        info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
        if str(android.get("package_name") or "").strip() != expected_package:
            continue
        matched = True
        has_app_id = bool(str(info.get("mobilesdk_app_id") or "").strip())
        keys = client.get("api_key") if isinstance(client.get("api_key"), list) else []
        has_api_key = any(isinstance(item, dict) and str(item.get("current_key") or "").strip() for item in keys)
        break
    if not project_id or not matched or not has_app_id or not has_api_key:
        raise ValueError(f"google-services.json não contém configuração Firebase completa para {expected_package}")
    return {
        "ok": True,
        "path": "app/google-services.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "package": expected_package,
        "project_id": project_id[:80],
        "source": "job_payload" if raw_b64 else "workspace",
    }


def _install_apk_signing_from_payload(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Instala keystore compatível recebida pelo payload autenticado do job.

    A keystore não vem no ZIP público e não fica no Git. Ela é gravada somente
    no workspace temporário do build para que o APK novo tenha a mesma assinatura
    da versão já instalada e possa atualizar sem desinstalar.
    """
    raw_b64 = str(payload.get("apkSigningKeystoreB64") or payload.get("apk_signing_keystore_b64") or "").strip()
    expected_sha = str(payload.get("apkSigningKeystoreSha256") or payload.get("apk_signing_keystore_sha256") or "").strip().lower()
    alias = str(payload.get("apkSigningKeyAlias") or payload.get("apk_signing_key_alias") or "androiddebugkey").strip() or "androiddebugkey"
    storepass = str(payload.get("apkSigningStorePassword") or payload.get("apk_signing_store_password") or "").strip()
    keypass = str(payload.get("apkSigningKeyPassword") or payload.get("apk_signing_key_password") or storepass).strip()

    if not raw_b64:
        raise FileNotFoundError(
            "keystore de assinatura compatível ausente no payload. A VPS deve enviar apkSigningKeystoreB64; "
            "não use a chave debug aleatória do phone worker para atualizar o Core Worker instalado."
        )
    if not storepass or not alias:
        raise ValueError("configuração de assinatura compatível incompleta no payload")

    try:
        raw = base64.b64decode(raw_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"payload da keystore inválido: {type(exc).__name__}: {_short_text(exc, limit=100)}") from exc
    if len(raw) > 1024 * 1024:
        raise ValueError("keystore de assinatura grande demais")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha and expected_sha != actual_sha:
        raise ValueError("sha256 da keystore de assinatura divergente no payload")

    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    key_path = app_dir / "core-worker-upload.keystore"
    props_path = app_dir / "core-worker-signing.properties"

    key_path.write_bytes(raw)
    with contextlib.suppress(Exception):
        key_path.chmod(0o600)

    # Não registrar senhas em logs/resultados. Este arquivo fica só no workspace temporário.
    props_path.write_text(
        "\n".join([
            "CORE_WORKER_SIGNING_KEYSTORE=core-worker-upload.keystore",
            f"CORE_WORKER_SIGNING_KEY_ALIAS={alias}",
            f"CORE_WORKER_SIGNING_STORE_PASSWORD={storepass}",
            f"CORE_WORKER_SIGNING_KEY_PASSWORD={keypass or storepass}",
            "",
        ]),
        encoding="utf-8",
    )
    with contextlib.suppress(Exception):
        props_path.chmod(0o600)

    return {
        "ok": True,
        "mode": str(payload.get("apkSigningMode") or payload.get("apk_signing_mode") or "compat-vps-debug-keystore")[:80],
        "alias": alias,
        "keystore_sha256": actual_sha,
        "source": str(payload.get("apkSigningSource") or payload.get("apk_signing_source") or "job_payload")[:80],
    }


def _read_android_version(project_dir: Path) -> tuple[str, int]:
    build_gradle = project_dir / "app" / "build.gradle"
    text = build_gradle.read_text(encoding="utf-8", errors="ignore") if build_gradle.exists() else ""
    name_match = re.search(r"versionName\s+[\"']([^\"']+)[\"']", text)
    code_match = re.search(r"versionCode\s+(\d+)", text)
    version_name = name_match.group(1) if name_match else "0.0.0"
    version_code = int(code_match.group(1)) if code_match else 0
    return version_name, version_code


def _inspect_android_native_build_environment(project_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Diagnóstico leve para builds Android com código nativo.

    Patch 84.2 separa dois modos:
    - externalNativeBuild/CMake: exige toolchain nativa executável no host;
    - jniLibs prebuilt: o Gradle apenas empacota .so já pronta e não deve exigir
      CMake/NDK no Termux.

    O CMakeLists em src/main/cpp pode existir como fonte de auditoria sem obrigar
    o phone worker a executar CMake.
    """
    build_gradle = project_dir / "app" / "build.gradle"
    cmake_lists = project_dir / "app" / "src" / "main" / "cpp" / "CMakeLists.txt"
    prebuilt_dir = project_dir / "app" / "src" / "main" / "jniLibs" / "arm64-v8a"
    prebuilt_executor = prebuilt_dir / "libcoreworker_executor.so"
    text = build_gradle.read_text(encoding="utf-8", errors="ignore") if build_gradle.exists() else ""
    # Ignore comentários: o CMakeLists pode existir como auditoria e o build.gradle
    # pode explicar externalNativeBuild sem ativá-lo de fato. Só exigimos toolchain
    # quando há bloco Gradle real.
    gradle_no_line_comments = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    external_required = bool(re.search(r"(?m)^\s*externalNativeBuild\s*\{", gradle_no_line_comments))
    prebuilt_present = bool(prebuilt_executor.is_file() and prebuilt_executor.stat().st_size > 1024)
    required = external_required
    info: dict[str, Any] = {
        "required": required,
        "externalNativeBuild": external_required,
        "jniLibsPrebuilt": prebuilt_present,
        "prebuilt_executor": str(prebuilt_executor),
        "prebuilt_executor_bytes": prebuilt_executor.stat().st_size if prebuilt_executor.is_file() else 0,
        "cmake_lists": str(cmake_lists),
        "cmake_lists_ok": cmake_lists.is_file(),
        "ok": True,
        "missing": [],
    }
    if prebuilt_present and not external_required:
        info["summary"] = "executor nativo prebuilt será empacotado via jniLibs; CMake/NDK não exigidos no Termux"
        return info
    if not required:
        info["summary"] = "sem externalNativeBuild ativo"
        return info

    android_home = Path(env.get("ANDROID_HOME") or env.get("ANDROID_SDK_ROOT") or (Path.home() / "android-sdk")).expanduser()
    info["android_home"] = str(android_home)
    if not cmake_lists.is_file():
        info["ok"] = False
        info["missing"].append("app/src/main/cpp/CMakeLists.txt")

    ndk_candidates: list[Path] = []
    for key in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        value = str(env.get(key) or os.getenv(key) or "").strip()
        if value:
            ndk_candidates.append(Path(value).expanduser())
    ndk_dir = android_home / "ndk"
    if ndk_dir.is_dir():
        ndk_candidates.extend(sorted([p for p in ndk_dir.iterdir() if p.is_dir()], reverse=True))
    ndk_bundle = android_home / "ndk-bundle"
    if ndk_bundle.is_dir():
        ndk_candidates.append(ndk_bundle)
    ndk_found = next((p for p in ndk_candidates if (p / "source.properties").is_file()), None)
    info["ndk"] = str(ndk_found or "")
    info["ndk_ok"] = ndk_found is not None
    if ndk_found is None:
        info["ok"] = False
        info["missing"].append("Android NDK")

    cmake_candidates: list[Path] = []
    cmake_root = android_home / "cmake"
    if cmake_root.is_dir():
        cmake_candidates.extend(sorted([p / "bin" / "cmake" for p in cmake_root.iterdir() if p.is_dir()], reverse=True))
    which_cmake = shutil.which("cmake")
    if which_cmake:
        cmake_candidates.append(Path(which_cmake))
    cmake_found = next((p for p in cmake_candidates if p.is_file()), None)
    info["cmake"] = str(cmake_found or "")
    info["cmake_ok"] = cmake_found is not None
    if cmake_found is None:
        info["ok"] = False
        info["missing"].append("CMake")

    info["summary"] = "toolchain nativa pronta" if info["ok"] else "toolchain nativa incompleta: " + ", ".join(info["missing"])
    return info


def _upload_core_worker_apk(apk_path: Path, *, filename: str, version_name: str, version_code: int, sha256: str, publish_url: str, changelog: list[str] | None = None, source_sha256: str = "", source_fingerprint: str = "", notification_id: str = "", apk_signing_mode: str = "", apk_signing_keystore_sha256: str = "") -> dict[str, Any]:
    identity = inspect_apk_identity(apk_path)
    assert_expected_apk_identity(
        identity,
        expected_package="dev.core.worker",
        expected_version_name=version_name,
        expected_version_code=version_code,
    )
    version_name = str(identity["versionName"])
    version_code = int(identity["versionCode"])
    actual_sha = _sha256_path(apk_path)
    if sha256 and str(sha256).lower() != actual_sha.lower():
        raise ValueError("sha256 do artifact APK divergente antes da publicação")
    sha256 = actual_sha
    base_url, token, worker_id = _core_worker_auth_parts()
    if not token or not worker_id:
        return {"ok": False, "error": "worker não pareado; não posso publicar APK"}
    publish_url = str(publish_url or "").strip() or f"{base_url}/core-worker/app/publish"
    boundary = "----CoreWorkerApkBoundary" + hashlib.sha256(f"{time.time()}:{os.getpid()}".encode()).hexdigest()[:24]

    def field(name: str, value: Any) -> bytes:
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")

    apk_bytes = apk_path.read_bytes()
    parts = [
        field("worker_id", worker_id),
        field("workerName", _default_worker_name()),
        field("filename", filename),
        field("versionName", version_name),
        field("versionCode", int(version_code or 0)),
        field("sha256", sha256),
        field("requiredAgentVersion", PHONE_WORKER_VERSION),
        field("notifyUsers", "true"),
        field("notificationRequested", "true"),
        field("sourceSha256", source_sha256),
        field("sourceFingerprint", source_fingerprint or source_sha256),
        field("notificationId", notification_id),
        field("apkSigningMode", apk_signing_mode),
        field("apkSigningKeystoreSha256", apk_signing_keystore_sha256[:64]),
        field("changelog", json.dumps(changelog or ["APK compilado por worker builder"], ensure_ascii=False)),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"apk\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/vnd.android.package-archive\r\n\r\n"
        ).encode("utf-8"),
        apk_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        publish_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Core-Worker-ID": worker_id,
            "X-Core-Worker-Version": PHONE_WORKER_VERSION,
            "X-Phone-Worker-Token": token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": f"CorePhoneWorker/{PHONE_WORKER_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(5.0, _env_float("PHONE_WORKER_APK_PUBLISH_TIMEOUT_SECONDS", 60.0))) as resp:
            raw = resp.read(256 * 1024)
            _remember_core_worker_network_ok()
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            return data if isinstance(data, dict) else {"ok": False, "error": "resposta inválida da VPS"}
    except urllib.error.HTTPError as exc:
        _remember_core_worker_network_ok()
        raw = exc.read(64 * 1024).decode("utf-8", errors="replace")
        return {"ok": False, "status": int(exc.code), "error": _short_text(raw or exc, limit=240)}
    except Exception as exc:
        _remember_core_worker_network_error(exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=220)}", "exception": type(exc).__name__}



def _apk_build_log_has_success(log_path: Path | None) -> bool:
    if log_path is None or not log_path.is_file():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
    except Exception:
        return False
    lowered = text.lower()
    if "build successful" not in lowered:
        return False
    fatal_markers = ("failure: build failed", "execution failed for task", "compilation failed")
    return not any(marker in lowered for marker in fatal_markers)


def _apk_build_log_value(log_path: Path | None, key: str, default: str = "") -> str:
    if log_path is None or not log_path.is_file():
        return default
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")[:12000]
    except Exception:
        return default
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text)
    if not match:
        return default
    return str(match.group(1) or "").strip()


def _find_gradle_log_for_work_dir(build_root: Path, work_dir: Path) -> Path | None:
    logs_dir = build_root / "logs"
    if not logs_dir.is_dir():
        return None
    wanted = str(work_dir)
    logs = sorted(logs_dir.glob("*.log"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for log_path in logs[:60]:
        try:
            head = log_path.read_text(encoding="utf-8", errors="replace")[:6000]
        except Exception:
            continue
        if wanted in head:
            return log_path
    return None


def _persist_recovered_apk_artifact(
    build_root: Path,
    *,
    apk_path: Path,
    project_dir: Path,
    gradle_log: Path | None,
    version_name: str = "",
    version_code: int = 0,
    source_sha256: str = "",
    source_fingerprint: str = "",
    notification_id: str = "",
    reason: str = "recovered-orphaned-gradle-output",
) -> dict[str, Any]:
    if not apk_path.is_file():
        return {}
    identity = inspect_apk_identity(apk_path)
    assert_expected_apk_identity(
        identity,
        expected_package="dev.core.worker",
        expected_version_name=version_name,
        expected_version_code=version_code,
    )
    version_name = str(identity["versionName"])
    version_code = int(identity["versionCode"])
    if gradle_log is not None:
        source_sha256 = source_sha256 or _apk_build_log_value(gradle_log, "source_sha256")
        source_fingerprint = source_fingerprint or _apk_build_log_value(gradle_log, "source_fingerprint")
    source_fingerprint = str(source_fingerprint or source_sha256 or "").strip()
    notification_id = str(notification_id or f"apk-{version_code}-{source_fingerprint[:12] or int(time.time())}").strip()
    raw = apk_path.read_bytes()
    apk_sha = hashlib.sha256(raw).hexdigest()
    filename = f"CoreWorker-v{version_name}-debug.apk"
    artifact_dir = build_root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / filename
    if artifact_path.exists():
        try:
            existing_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except Exception:
            existing_sha = ""
        if existing_sha != apk_sha:
            artifact_path = artifact_dir / f"{Path(filename).stem}-{notification_id[:16] or int(time.time())}.apk"
    try:
        shutil.copy2(apk_path, artifact_path)
    except Exception:
        artifact_path.write_bytes(raw)
    artifact_meta = {
        "filename": filename,
        "versionName": version_name,
        "versionCode": version_code,
        "sha256": apk_sha,
        "bytes": len(raw),
        "artifact_path": str(artifact_path),
        "sourceFingerprint": source_fingerprint,
        "sourceSha256": str(source_sha256 or ""),
        "notificationId": notification_id,
        "apkSigningMode": "compat-vps-debug-keystore",
        "apkSigningKeystoreSha256": "",
        "gradle_log_path": str(gradle_log) if gradle_log is not None else "",
        "gradle_log_exists": bool(gradle_log and gradle_log.exists()),
        "gradle_log_bytes": gradle_log.stat().st_size if gradle_log is not None and gradle_log.exists() else 0,
        "build_successful": True,
        "build_result": "success",
        "phoneWorkerVersion": PHONE_WORKER_VERSION,
        "phoneWorkerSourceHash": _phone_worker_source_hash(),
        "created_at": time.time(),
        "recovered": True,
        "recoveryReason": reason,
    }
    with contextlib.suppress(Exception):
        _write_json_file_atomic(artifact_path.with_suffix(artifact_path.suffix + ".json"), artifact_meta)
        _write_json_file_atomic(artifact_dir / "latest-artifact.json", artifact_meta)
    return artifact_meta


def _recover_orphaned_apk_build_outputs(
    build_root: Path,
    *,
    version_name: str = "",
    version_code: int = 0,
    source_fingerprint: str = "",
    source_sha256: str = "",
    notification_id: str = "",
) -> dict[str, Any]:
    """Recupera APK gerado quando o processo caiu depois do Gradle.

    Em Android/Termux o processo pode ser morto depois de `assembleDebug` e antes
    da cópia para artifacts/latest. O Gradle já deixou `app-debug.apk` no workdir;
    esse helper promove esse APK para artifacts e permite republicar sem rebuild.
    """
    if not build_root.is_dir():
        return {}
    wanted_fp = str(source_fingerprint or source_sha256 or "").strip()
    workdirs = sorted([p for p in build_root.glob("build-*") if p.is_dir()], key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for work_dir in workdirs[:40]:
        project_dir = work_dir / "src" / "android" / "core-worker-app"
        if not project_dir.is_dir():
            alt = work_dir / "src" / "core-worker-app"
            if alt.is_dir():
                project_dir = alt
        if not project_dir.is_dir():
            continue
        detected_name, detected_code = _read_android_version(project_dir)
        if version_name and detected_name and str(detected_name) != str(version_name):
            continue
        if int(version_code or 0) and int(detected_code or 0) and int(detected_code or 0) != int(version_code or 0):
            continue
        gradle_log = _find_gradle_log_for_work_dir(build_root, work_dir)
        if not _apk_build_log_has_success(gradle_log):
            continue
        log_fp = _apk_build_log_value(gradle_log, "source_fingerprint")
        log_sha = _apk_build_log_value(gradle_log, "source_sha256")
        if wanted_fp:
            hay = {str(log_fp or "").strip(), str(log_sha or "").strip()}
            short = wanted_fp[:12]
            if wanted_fp not in hay and short and not any(short and short in value for value in hay if value):
                continue
        apk_dir = project_dir / "app" / "build" / "outputs" / "apk" / "debug"
        apk_candidates = sorted(apk_dir.glob("*.apk"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True) if apk_dir.is_dir() else []
        for apk_path in apk_candidates:
            if not apk_path.is_file() or apk_path.stat().st_size < 1024 * 1024:
                continue
            return _persist_recovered_apk_artifact(
                build_root,
                apk_path=apk_path,
                project_dir=project_dir,
                gradle_log=gradle_log,
                version_name=version_name or detected_name,
                version_code=int(version_code or detected_code or 0),
                source_sha256=source_sha256 or log_sha,
                source_fingerprint=source_fingerprint or log_fp or source_sha256,
                notification_id=notification_id,
            )
    return {}

def _latest_apk_artifact_metadata(build_root: Path) -> dict[str, Any]:
    artifact_dir = build_root / "artifacts"
    candidates: list[Path] = []
    latest_meta = artifact_dir / "latest-artifact.json"
    if latest_meta.is_file():
        candidates.append(latest_meta)
    if artifact_dir.is_dir():
        candidates.extend(sorted(artifact_dir.glob("*.apk.json"), key=lambda path: path.stat().st_mtime, reverse=True))
    for meta_path in candidates:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8", errors="replace") or "{}")
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        apk_path = Path(str(data.get("artifact_path") or "")).expanduser()
        if apk_path.is_file():
            try:
                identity = inspect_apk_identity(apk_path)
                assert_expected_apk_identity(identity, expected_package="dev.core.worker")
            except Exception:
                continue
            data["filename"] = apk_path.name
            data["versionName"] = str(identity["versionName"])
            data["versionCode"] = int(identity["versionCode"])
            data["sha256"] = _sha256_path(apk_path)
            data["bytes"] = apk_path.stat().st_size
            data["metadata_path"] = str(meta_path)
            return data
    if artifact_dir.is_dir():
        for apk_path in sorted(artifact_dir.glob("*.apk"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                raw = apk_path.read_bytes()
            except Exception:
                continue
            name = apk_path.name
            try:
                identity = inspect_apk_identity(apk_path)
                assert_expected_apk_identity(identity, expected_package="dev.core.worker")
            except Exception:
                continue
            return {
                "filename": name,
                "versionName": identity["versionName"],
                "versionCode": int(identity["versionCode"]),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "artifact_path": str(apk_path),
                "notificationId": f"apk-republish-{int(apk_path.stat().st_mtime)}",
            }
    return {}


def _apply_apk_publish_last(payload: dict[str, Any]) -> dict[str, Any]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    if "apk-builder" not in set(roles + capabilities):
        raise PermissionError("este worker não tem função apk-builder")
    build_root = Path(os.getenv("PHONE_WORKER_APK_BUILD_DIR") or (Path.home() / "core-worker-apk-builds")).expanduser()
    requested = str(payload.get("artifact_path") or payload.get("apk_path") or "").strip()
    meta: dict[str, Any] = {}
    if requested:
        apk_path = Path(requested).expanduser()
        if apk_path.is_file():
            raw = apk_path.read_bytes()
            meta = {
                "filename": apk_path.name,
                "versionName": str(payload.get("versionName") or payload.get("version_name") or ""),
                "versionCode": int(payload.get("versionCode") or payload.get("version_code") or 0),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "artifact_path": str(apk_path),
                "sourceFingerprint": str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or ""),
                "sourceSha256": str(payload.get("sourceSha256") or payload.get("source_sha256") or ""),
                "notificationId": str(payload.get("notificationId") or payload.get("notification_id") or f"apk-republish-{int(time.time())}"),
            }
    if not meta:
        meta = _recover_orphaned_apk_build_outputs(
            build_root,
            version_name=str(payload.get("versionName") or payload.get("version_name") or ""),
            version_code=int(payload.get("versionCode") or payload.get("version_code") or 0),
            source_fingerprint=str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or ""),
            source_sha256=str(payload.get("sourceSha256") or payload.get("source_sha256") or ""),
            notification_id=str(payload.get("notificationId") or payload.get("notification_id") or ""),
        )
    if not meta:
        meta = _latest_apk_artifact_metadata(build_root)
    if not meta:
        return {"ok": False, "summary": "nenhum APK persistente encontrado para republicar", "artifact_dir": str(build_root / "artifacts")}
    apk_path = Path(str(meta.get("artifact_path") or "")).expanduser()
    if not apk_path.is_file():
        return {"ok": False, "summary": "artifact APK não existe mais", "artifact_path": str(apk_path)}
    identity = inspect_apk_identity(apk_path)
    requested_name = str(payload.get("versionName") or payload.get("version_name") or meta.get("versionName") or "")
    try:
        requested_code = int(payload.get("versionCode") or payload.get("version_code") or meta.get("versionCode") or 0)
    except Exception:
        requested_code = 0
    assert_expected_apk_identity(
        identity,
        expected_package="dev.core.worker",
        expected_version_name=requested_name,
        expected_version_code=requested_code,
    )
    version_name = str(identity["versionName"])
    version_code = int(identity["versionCode"])
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload.get("filename") or meta.get("filename") or apk_path.name)).strip("-._")
    publish_url = str(payload.get("publish_url") or "").strip()
    base_url, _token, _worker_id = _core_worker_auth_parts()
    try:
        publish = _upload_core_worker_apk(
            apk_path,
            filename=filename,
            version_name=version_name,
            version_code=version_code,
            sha256=str(meta.get("sha256") or hashlib.sha256(apk_path.read_bytes()).hexdigest()),
            publish_url=publish_url or f"{base_url}/core-worker/app/publish",
            changelog=list(payload.get("changelog") or ["APK republicado por worker builder"]),
            source_sha256=str(payload.get("sourceSha256") or payload.get("source_sha256") or meta.get("sourceSha256") or ""),
            source_fingerprint=str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or meta.get("sourceFingerprint") or ""),
            notification_id=str(payload.get("notificationId") or payload.get("notification_id") or meta.get("notificationId") or f"apk-republish-{int(time.time())}"),
            apk_signing_mode=str(payload.get("apkSigningMode") or meta.get("apkSigningMode") or "compat-vps-debug-keystore"),
            apk_signing_keystore_sha256=str(payload.get("apkSigningKeystoreSha256") or meta.get("apkSigningKeystoreSha256") or ""),
        )
    except Exception as exc:
        publish = {"ok": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=220)}", "exception": type(exc).__name__}
    return {
        "ok": bool(publish.get("ok")),
        "summary": "APK republicado na VPS" if publish.get("ok") else "APK persistente encontrado, mas publicação falhou",
        "publish_ok": bool(publish.get("ok")),
        "publish": publish,
        "artifact_found": True,
        "artifact_path": str(apk_path),
        "versionName": version_name,
        "versionCode": version_code,
        "apk": {"filename": filename, "bytes": apk_path.stat().st_size, "sha256": str(meta.get("sha256") or ""), "artifact_path": str(apk_path)},
    }


def _apply_apk_build_debug(payload: dict[str, Any]) -> dict[str, Any]:
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    if "apk-builder" not in set(roles + capabilities):
        raise PermissionError("este worker não tem função apk-builder")
    if not _env_bool("PHONE_WORKER_APK_BUILD_ENABLED", True):
        raise PermissionError("build de APK desativado neste worker")

    source_url = str(payload.get("source_zip_url") or os.getenv("PHONE_WORKER_APK_BUILD_SOURCE_URL") or "").strip()
    if not source_url:
        raise ValueError("source_zip_url ausente; publique source-core-worker-app.zip na VPS")
    expected_source_sha = str(payload.get("source_sha256") or "").strip().lower()
    source_fingerprint = str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or expected_source_sha or "").strip()
    project_subdir = str(payload.get("project_subdir") or "android/core-worker-app").strip().strip("/")
    if not project_subdir or project_subdir.startswith("/") or ".." in project_subdir.split("/"):
        raise ValueError("project_subdir inválido")

    build_root = Path(os.getenv("PHONE_WORKER_APK_BUILD_DIR") or (Path.home() / "core-worker-apk-builds")).expanduser()
    build_root.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(60, _env_int("PHONE_WORKER_APK_BUILD_TIMEOUT_SECONDS", 3600))
    max_source_bytes = max(1024 * 1024, _env_int("PHONE_WORKER_APK_BUILD_SOURCE_MAX_BYTES", 1024 * 1024 * 1024))
    keep_workdir = _env_bool("PHONE_WORKER_APK_BUILD_KEEP_WORKDIR", False)
    keep_failed_workdir = _env_bool("PHONE_WORKER_APK_BUILD_KEEP_FAILED_WORKDIR", False)
    started = time.time()
    version_name = str(payload.get("versionName") or payload.get("version_name") or "desconhecida")
    try:
        version_code = int(payload.get("versionCode") or payload.get("version_code") or 0)
    except Exception:
        version_code = 0
    notification_id = str(payload.get("notificationId") or payload.get("notification_id") or f"apk-{version_code}-{(source_fingerprint or expected_source_sha)[:12]}").strip()
    job_slug = _apk_build_safe_slug(f"{notification_id or 'apk'}-{int(started)}-{os.getpid()}")
    work_dir = build_root / f"build-{int(started)}-{os.getpid()}"
    source_zip = work_dir / "source.zip"
    gradle_log = _apk_build_logs_dir(build_root) / f"{job_slug}-gradle.log"
    preserve_workdir = False
    lock_handle: Any | None = None
    active_marker = work_dir / ".build-active"

    if not _HEAVY_RESOURCE_LOCK.acquire(blocking=False):
        return _apk_build_failure_result(
            summary="recurso pesado ocupado por renderização ou manutenção",
            version_name=version_name,
            version_code=version_code,
            source_fingerprint=source_fingerprint,
            source_sha256=expected_source_sha,
            notification_id=notification_id,
            work_dir=work_dir,
            gradle_log=gradle_log,
            extra={"busy": True, "retryable": True},
        )

    if not _APK_BUILD_THREAD_LOCK.acquire(blocking=False):
        _HEAVY_RESOURCE_LOCK.release()
        return _apk_build_failure_result(
            summary="build APK já está em execução neste processo do phone worker",
            version_name=version_name,
            version_code=version_code,
            source_fingerprint=source_fingerprint,
            source_sha256=expected_source_sha,
            notification_id=notification_id,
            work_dir=work_dir,
            gradle_log=gradle_log,
            extra={"busy": True, "retryable": True},
        )

    try:
        recovered_meta = _recover_orphaned_apk_build_outputs(
            build_root,
            version_name=version_name,
            version_code=version_code,
            source_fingerprint=source_fingerprint,
            source_sha256=expected_source_sha,
            notification_id=notification_id,
        )
        if recovered_meta and bool(payload.get("publish", True)):
            publish_result = _apply_apk_publish_last({
                **payload,
                "artifact_path": recovered_meta.get("artifact_path"),
                "filename": recovered_meta.get("filename"),
                "versionName": recovered_meta.get("versionName") or version_name,
                "versionCode": recovered_meta.get("versionCode") or version_code,
                "sourceFingerprint": recovered_meta.get("sourceFingerprint") or source_fingerprint,
                "sourceSha256": recovered_meta.get("sourceSha256") or expected_source_sha,
                "notificationId": recovered_meta.get("notificationId") or notification_id,
                "changelog": payload.get("changelog") or ["APK recuperado de build anterior e publicado pelo worker builder"],
            })
            publish_result["recovered_from_orphaned_output"] = True
            publish_result["artifact_meta"] = recovered_meta
            return publish_result
        elif recovered_meta:
            return {
                "ok": True,
                "build_gradle_ok": True,
                "artifact_found": True,
                "recovered_from_orphaned_output": True,
                "summary": f"APK {recovered_meta.get('versionName') or version_name} recuperado de build anterior",
                "versionName": recovered_meta.get("versionName") or version_name,
                "versionCode": recovered_meta.get("versionCode") or version_code,
                "artifact_path": recovered_meta.get("artifact_path"),
                "apk": {"filename": recovered_meta.get("filename"), "bytes": recovered_meta.get("bytes"), "sha256": recovered_meta.get("sha256"), "artifact_path": recovered_meta.get("artifact_path")},
                "artifact_meta": recovered_meta,
            }

        lock_handle, lock_info = _try_acquire_apk_build_file_lock(build_root)
        if lock_handle is None:
            return _apk_build_failure_result(
                summary="build APK já está em execução no phone worker; não iniciei outro Gradle",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=expected_source_sha,
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                extra={"busy": True, "retryable": True, "lock": lock_info},
            )
        _cleanup_old_apk_build_logs(build_root)
        work_dir.mkdir(parents=True, exist_ok=True)
        active_marker.write_text(json.dumps({"pid": os.getpid(), "started_at": started, "job": notification_id}, ensure_ascii=False), encoding="utf-8")

        download = _download_url_to_file(source_url, source_zip, timeout=60.0, max_bytes=max_source_bytes)
        if not download.get("ok"):
            preserve_workdir = keep_failed_workdir
            return _apk_build_failure_result(
                summary="falha baixando fonte do APK",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=expected_source_sha,
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                extra={"download": download},
            )
        if expected_source_sha and expected_source_sha != str(download.get("sha256") or "").lower():
            preserve_workdir = keep_failed_workdir
            return _apk_build_failure_result(
                summary="sha256 do source zip divergente",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=expected_source_sha,
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                extra={"expected": expected_source_sha, "actual": download.get("sha256")},
            )
        if not source_fingerprint:
            source_fingerprint = str(download.get("sha256") or "")
        source_dir = work_dir / "src"
        members = _safe_extract_zip_file(source_zip, source_dir)
        project_dir = (source_dir / project_subdir).resolve()
        if not project_dir.is_dir():
            alt = source_dir / "core-worker-app"
            if alt.is_dir():
                project_dir = alt.resolve()
            else:
                raise FileNotFoundError(f"projeto Android não encontrado: {project_subdir}")
        google_services = _install_google_services_from_payload(project_dir, payload)
        apk_signing = _install_apk_signing_from_payload(project_dir, payload)
        env = os.environ.copy()
        env["CORE_WORKER_REQUIRE_COMPAT_SIGNING"] = "true"
        if bool(payload.get("selfBuilderRequired", True)):
            env["CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN"] = "true"
        base_url, _token, _worker_id = _core_worker_auth_parts()
        injected_vps_url = str(payload.get("coreWorkerVpsUrl") or payload.get("core_worker_vps_url") or payload.get("vps_url") or base_url or "").strip().rstrip("/")
        injected_vps_label = str(payload.get("coreWorkerVpsLabel") or payload.get("core_worker_vps_label") or ("VPS privada configurada" if injected_vps_url else "VPS não configurada no build")).strip()
        if injected_vps_url:
            env["CORE_WORKER_VPS_URL"] = injected_vps_url
            env["CORE_WORKER_VPS_LABEL"] = injected_vps_label
        builder_environment = _prepare_termux_android_build(project_dir, env)
        if bool(payload.get("selfBuilderRequired", True)):
            try:
                builder_environment["self_builder_toolchain"] = _prepare_apk_self_builder_toolchain(project_dir, env)
            except Exception as exc:
                preserve_workdir = keep_failed_workdir
                detail = f"{type(exc).__name__}: {_short_text(exc, limit=360)}"
                builder_environment["self_builder_toolchain"] = {
                    "ok": False,
                    "stage": "self_builder_toolchain_prepare",
                    "error": detail,
                }
                return _apk_build_failure_result(
                    summary="preparação do autobuilder do APK falhou: " + _short_text(exc, limit=180),
                    version_name=version_name,
                    version_code=version_code,
                    source_fingerprint=source_fingerprint,
                    source_sha256=expected_source_sha,
                    notification_id=notification_id,
                    work_dir=work_dir,
                    gradle_log=gradle_log,
                    builder_environment=builder_environment,
                    extra={
                        "stage": "self_builder_toolchain_prepare",
                        "retryable": False,
                        "permanent_failure": True,
                        "self_builder_error": detail,
                    },
                )
        native_environment = _inspect_android_native_build_environment(project_dir, env)
        builder_environment["native_build"] = native_environment
        builder_environment["gradle_log_path"] = str(gradle_log)
        if native_environment.get("required") and not native_environment.get("ok"):
            preserve_workdir = keep_failed_workdir
            return _apk_build_failure_result(
                summary=native_environment.get("summary") or "toolchain nativa Android incompleta",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=expected_source_sha,
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                builder_environment=builder_environment,
                native_environment=native_environment,
                extra={"hint": "instale/prepare Android NDK e CMake no phone worker builder; a VPS não deve buildar APK"},
            )
        builder_environment["google_services"] = {
            "ok": bool(google_services.get("ok")),
            "package": google_services.get("package"),
            "project_id": google_services.get("project_id"),
            "source": google_services.get("source"),
            "sha256": str(google_services.get("sha256") or "")[:12],
        }
        builder_environment["apk_signing"] = {
            "ok": bool(apk_signing.get("ok")),
            "mode": apk_signing.get("mode"),
            "alias": apk_signing.get("alias"),
            "source": apk_signing.get("source"),
            "keystore_sha256": str(apk_signing.get("keystore_sha256") or "")[:12],
        }
        if injected_vps_url:
            builder_environment["injected_vps_url"] = True
        detected_version_name, detected_version_code = _read_android_version(project_dir)
        version_name = str(payload.get("versionName") or payload.get("version_name") or detected_version_name)
        version_code = int(payload.get("versionCode") or payload.get("version_code") or detected_version_code or 0)
        notification_id = str(payload.get("notificationId") or payload.get("notification_id") or f"apk-{version_code}-{(source_fingerprint or expected_source_sha)[:12]}").strip()
        gradlew = project_dir / "gradlew"
        if gradlew.exists():
            gradlew.chmod(0o755)
            cmd = [str(gradlew), "assembleDebug", "--no-daemon", "--max-workers=1", "--stacktrace", "--console=plain"]
        else:
            if not shutil.which("gradle"):
                raise FileNotFoundError("gradle não encontrado no worker builder")
            cmd = ["gradle", "assembleDebug", "--no-daemon", "--max-workers=1", "--stacktrace", "--console=plain"]
        if not env.get("ANDROID_HOME"):
            default_sdk = Path.home() / "android-sdk"
            if default_sdk.exists():
                env["ANDROID_HOME"] = str(default_sdk)
                env.setdefault("ANDROID_SDK_ROOT", str(default_sdk))
                env["PATH"] = f"{default_sdk}/cmdline-tools/latest/bin:{default_sdk}/platform-tools:" + env.get("PATH", "")
        shutil.rmtree(project_dir / "app/build/outputs/apk/debug", ignore_errors=True)
        with gradle_log.open("w", encoding="utf-8", errors="replace") as log_fh:
            log_fh.write("===== Core Worker APK build =====\n")
            log_fh.write(f"phone_worker_version={PHONE_WORKER_VERSION}\n")
            log_fh.write(f"started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started))}\n")
            log_fh.write(f"work_dir={work_dir}\n")
            log_fh.write(f"project_dir={project_dir}\n")
            log_fh.write(f"versionName={version_name}\nversionCode={version_code}\n")
            log_fh.write(f"source_sha256={expected_source_sha or download.get('sha256') or ''}\n")
            log_fh.write(f"source_fingerprint={source_fingerprint}\n")
            log_fh.write("cmd=" + " ".join(shlex.quote(part) for part in cmd) + "\n")
            log_fh.write("===== Gradle output =====\n")
            log_fh.flush()
            try:
                proc = subprocess.run(cmd, cwd=str(project_dir), env=env, stdout=log_fh, stderr=subprocess.STDOUT, text=True, timeout=timeout_seconds)
                returncode = int(proc.returncode)
            except subprocess.TimeoutExpired as exc:
                log_fh.write(f"\n===== TIMEOUT after {timeout_seconds}s =====\n{type(exc).__name__}: {_short_text(exc, limit=300)}\n")
                returncode = 124
        if returncode != 0:
            preserve_workdir = keep_failed_workdir
            gradle_failure = _summarize_gradle_log(gradle_log)
            return _apk_build_failure_result(
                summary=str(gradle_failure.get("summary") or "build do APK falhou; veja gradle_log_tail"),
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=str(download.get("sha256") or expected_source_sha or ""),
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                returncode=returncode,
                builder_environment=builder_environment,
                native_environment=native_environment,
                extra={
                    "retryable": False,
                    "permanent_failure": bool(gradle_failure.get("permanent")),
                    "gradle_error_summary": gradle_failure.get("summary"),
                    "gradle_error_detail": gradle_failure.get("detail"),
                },
            )
        apk_candidates = sorted((project_dir / "app" / "build" / "outputs" / "apk" / "debug").glob("*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not apk_candidates:
            preserve_workdir = keep_failed_workdir
            return _apk_build_failure_result(
                summary="build terminou mas APK não foi encontrado",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=str(download.get("sha256") or expected_source_sha or ""),
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                builder_environment=builder_environment,
                native_environment=native_environment,
            )
        apk_path = apk_candidates[0]
        identity = inspect_apk_identity(apk_path)
        assert_expected_apk_identity(
            identity,
            expected_package="dev.core.worker",
            expected_version_name=version_name,
            expected_version_code=version_code,
        )
        version_name = str(identity["versionName"])
        version_code = int(identity["versionCode"])
        raw = apk_path.read_bytes()
        apk_sha = hashlib.sha256(raw).hexdigest()
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload.get("filename") or f"CoreWorker-v{version_name}-debug.apk")).strip("-._")
        if not filename.lower().endswith(".apk"):
            filename += ".apk"
        artifact_dir = build_root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / filename
        if artifact_path.exists():
            artifact_path = artifact_dir / f"{Path(filename).stem}-{notification_id[:16] or int(started)}.apk"
        try:
            shutil.copy2(apk_path, artifact_path)
        except Exception:
            artifact_path.write_bytes(raw)
        artifact_meta = {
            "filename": filename,
            "versionName": version_name,
            "versionCode": version_code,
            "sha256": apk_sha,
            "bytes": len(raw),
            "artifact_path": str(artifact_path),
            "sourceFingerprint": source_fingerprint,
            "sourceSha256": str(download.get("sha256") or expected_source_sha or ""),
            "notificationId": notification_id,
            "apkSigningMode": str(apk_signing.get("mode") or "compat-vps-debug-keystore"),
            "apkSigningKeystoreSha256": str(apk_signing.get("keystore_sha256") or ""),
            "gradle_log_path": str(gradle_log),
            "gradle_log_exists": bool(gradle_log.exists()),
            "gradle_log_bytes": gradle_log.stat().st_size if gradle_log.exists() else 0,
            "build_successful": True,
            "build_result": "success",
            "phoneWorkerVersion": PHONE_WORKER_VERSION,
            "phoneWorkerSourceHash": _phone_worker_source_hash(),
            "created_at": time.time(),
        }
        # A etapa pós-Gradle é tão importante quanto o build: se o APK foi
        # gerado, ele precisa virar artifact persistente antes de qualquer publish.
        # Não esconda falhas aqui, senão o painel mostra build pendente mesmo com
        # `BUILD SUCCESSFUL` no log.
        if not artifact_path.is_file():
            artifact_path.write_bytes(raw)
        persisted_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if persisted_sha != apk_sha:
            artifact_path.write_bytes(raw)
            persisted_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if persisted_sha != apk_sha:
            preserve_workdir = True
            return _apk_build_failure_result(
                summary="build terminou mas falhou persistindo artifact APK",
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=str(download.get("sha256") or expected_source_sha or ""),
                notification_id=notification_id,
                work_dir=work_dir,
                gradle_log=gradle_log,
                builder_environment=builder_environment,
                native_environment=native_environment,
                extra={"artifact_path": str(artifact_path), "expected_sha256": apk_sha, "actual_sha256": persisted_sha},
            )
        _write_json_file_atomic(artifact_path.with_suffix(artifact_path.suffix + ".json"), artifact_meta)
        _write_json_file_atomic(artifact_dir / "latest-artifact.json", artifact_meta)
        artifact_cleanup = _cleanup_old_apk_build_artifacts(build_root)
        result: dict[str, Any] = {
            "ok": True,
            "build_gradle_ok": True,
            "artifact_found": True,
            "artifact_path": str(artifact_path),
            "summary": f"APK {version_name} compilado pelo worker",
            "versionName": version_name,
            "versionCode": version_code,
            "sourceFingerprint": source_fingerprint,
            "sourceSha256": str(download.get("sha256") or expected_source_sha or ""),
            "notificationId": notification_id,
            "gradle_log_path": str(gradle_log),
            "apk": {
                "filename": filename,
                "bytes": len(raw),
                "sha256": apk_sha,
                "artifact_path": str(artifact_path),
                "signed": True,
                "signed_by": str(apk_signing.get("mode") or "compat-vps-debug-keystore"),
                "signing_keystore_sha256": str(apk_signing.get("keystore_sha256") or "")[:12],
            },
            "source": {"url": source_url, "bytes": download.get("bytes"), "sha256": download.get("sha256"), "files": members},
            "builder_environment": builder_environment,
            "artifact_meta": artifact_meta,
            "artifact_cleanup": artifact_cleanup,
            "duration_seconds": round(time.time() - started, 3),
        }
        if bool(payload.get("publish", True)):
            publish_url = str(payload.get("publish_url") or "").strip()
            base_url, _token, _worker_id = _core_worker_auth_parts()
            try:
                publish = _upload_core_worker_apk(
                    artifact_path,
                    filename=filename,
                    version_name=version_name,
                    version_code=version_code,
                    sha256=apk_sha,
                    publish_url=publish_url or f"{base_url}/core-worker/app/publish",
                    changelog=list(payload.get("changelog") or ["APK compilado por worker builder"]),
                    source_sha256=str(download.get("sha256") or expected_source_sha or ""),
                    source_fingerprint=str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or download.get("sha256") or expected_source_sha or ""),
                    notification_id=notification_id,
                    apk_signing_mode=str(apk_signing.get("mode") or "compat-vps-debug-keystore"),
                    apk_signing_keystore_sha256=str(apk_signing.get("keystore_sha256") or ""),
                )
            except Exception as exc:
                publish = {"ok": False, "error": f"{type(exc).__name__}: {_short_text(exc, limit=220)}", "exception": type(exc).__name__}
            result["publish"] = publish
            result["publish_ok"] = bool(publish.get("ok", False))
            if not bool(publish.get("ok", False)):
                result["ok"] = True
                result["summary"] = "APK compilado; publicação na VPS ficou pendente"
                result["publish_pending"] = True
                if publish.get("error"):
                    result["publish_error"] = str(publish.get("error"))[:240]
        else:
            result["publish_ok"] = False
        return result
    finally:
        with contextlib.suppress(Exception):
            active_marker.unlink()
        _release_apk_build_file_lock(lock_handle)
        _APK_BUILD_THREAD_LOCK.release()
        _HEAVY_RESOURCE_LOCK.release()
        if not keep_workdir and not preserve_workdir:
            with contextlib.suppress(Exception):
                shutil.rmtree(work_dir)

_WORKER_UPDATE_TARGETS: dict[str, tuple[str, str, int]] = {
    "phone_worker.py": ("worker", "phone_worker.py", 0o755),
    "apk_identity.py": ("worker", "apk_identity.py", 0o644),
    "music_agent.py": ("worker", "music_agent.py", 0o755),
    "start-phone-worker.sh": ("worker", "start-phone-worker.sh", 0o755),
    "start-phone-music-agent.sh": ("worker", "start-phone-music-agent.sh", 0o755),
    "watch-phone-worker.sh": ("worker", "watch-phone-worker.sh", 0o755),
    "pair-phone-worker.sh": ("worker", "pair-phone-worker.sh", 0o755),
    "bootstrap-phone-worker.sh": ("worker", "bootstrap-phone-worker.sh", 0o755),
    "install.sh": ("worker", "install.sh", 0o755),
    "README.md": ("worker", "README.md", 0o644),
    "phone-worker.env.example": ("worker", "phone-worker.env.example", 0o600),
    "teto_renderer/__init__.py": ("worker", "teto_renderer/__init__.py", 0o644),
    "teto_renderer/errors.py": ("worker", "teto_renderer/errors.py", 0o644),
    "teto_renderer/cache.py": ("worker", "teto_renderer/cache.py", 0o644),
    "teto_renderer/voicebank.py": ("worker", "teto_renderer/voicebank.py", 0o644),
    "teto_renderer/phonemizer.py": ("worker", "teto_renderer/phonemizer.py", 0o644),
    "teto_renderer/prosody.py": ("worker", "teto_renderer/prosody.py", 0o644),
    "teto_renderer/renderer.py": ("worker", "teto_renderer/renderer.py", 0o644),
    "scripts/validate-teto-assets.py": ("worker", "scripts/validate-teto-assets.py", 0o755),
}
_PHONE_WORKER_SOURCE_HASH_EXCLUDED = frozenset({"README.md", "phone-worker.env.example"})
_PHONE_WORKER_SOURCE_HASH_CACHE: dict[str, Any] = {"signature": None, "value": ""}
_PHONE_WORKER_SOURCE_HASH_LOCK = threading.Lock()


def _phone_worker_source_hash() -> str:
    """Hash do conjunto instalável, ignorando logs, env e arquivos temporários."""
    entries: list[tuple[str, Path, int, int, int]] = []
    for target in sorted(_WORKER_UPDATE_TARGETS):
        if target in _PHONE_WORKER_SOURCE_HASH_EXCLUDED:
            continue
        path, _mode = _safe_update_target_path(target)
        try:
            stat_result = path.stat()
        except OSError:
            return ""
        if not path.is_file():
            return ""
        entries.append((target, path, int(stat_result.st_size), int(stat_result.st_mtime_ns), int(stat_result.st_ino)))
    signature = tuple((target, size, mtime_ns, inode) for target, _path, size, mtime_ns, inode in entries)
    with _PHONE_WORKER_SOURCE_HASH_LOCK:
        if _PHONE_WORKER_SOURCE_HASH_CACHE.get("signature") == signature:
            return str(_PHONE_WORKER_SOURCE_HASH_CACHE.get("value") or "")
        digest = hashlib.sha256()
        for target, path, _size, _mtime_ns, _inode in entries:
            digest.update(target.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_path(path).encode("ascii"))
            digest.update(b"\n")
        value = digest.hexdigest()
        _PHONE_WORKER_SOURCE_HASH_CACHE.update({"signature": signature, "value": value})
        return value


def _normalize_worker_update_target(target: str) -> str:
    raw = str(target or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError(f"arquivo de update não permitido: {raw or '<vazio>'}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"arquivo de update não permitido: {raw}")
    normalized = "/".join(parts)
    if normalized not in _WORKER_UPDATE_TARGETS:
        raise ValueError(f"arquivo de update não permitido: {normalized}")
    return normalized


def _safe_update_target_path(target: str) -> tuple[Path, int]:
    clean = _normalize_worker_update_target(target)
    location, filename, mode = _WORKER_UPDATE_TARGETS[clean]
    base = (_phone_worker_dir() if location == "worker" else Path.home()).expanduser().resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"destino de update escapou da pasta permitida: {clean}") from exc
    return path, mode


def _read_phone_worker_version_from_path(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else ""


def _load_worker_update_files(payload: dict[str, Any], *, max_file_bytes: int, max_total_bytes: int) -> tuple[list[dict[str, Any]], str]:
    archive_url = str(payload.get("update_zip_url") or "").strip()
    if archive_url:
        parsed = urllib.parse.urlparse(archive_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("update_zip_url inválida")
        expected_archive_sha = str(payload.get("update_zip_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_archive_sha):
            raise ValueError("sha256 do pacote de update ausente/inválido")
        max_archive_bytes = max(256 * 1024, _env_int("PHONE_WORKER_UPDATE_ARCHIVE_MAX_BYTES", 8 * 1024 * 1024))
        with tempfile.TemporaryDirectory(prefix="phone-worker-update-") as temp_dir:
            archive_path = Path(temp_dir) / "update.zip"
            download = _download_url_to_file(
                archive_url,
                archive_path,
                timeout=max(10.0, _env_float("PHONE_WORKER_UPDATE_DOWNLOAD_TIMEOUT_SECONDS", 60.0)),
                max_bytes=max_archive_bytes,
                accept="application/zip,*/*",
                size_label="pacote de update",
            )
            if not download.get("ok"):
                raise RuntimeError(f"download do update falhou: {_short_text(download.get('error'), limit=160)}")
            if str(download.get("sha256") or "").lower() != expected_archive_sha:
                raise ValueError("sha256 divergente no pacote de update")
            expected_bytes = int(payload.get("update_zip_bytes") or 0)
            if expected_bytes and int(download.get("bytes") or 0) != expected_bytes:
                raise ValueError("tamanho divergente no pacote de update")
            with zipfile.ZipFile(archive_path) as zf:
                members = [member for member in zf.infolist() if not member.is_dir()]
                names = [str(member.filename or "").replace("\\", "/") for member in members]
                if len(names) != len(set(names)) or len(names) > 25:
                    raise ValueError("pacote de update com membros duplicados/em excesso")
                if "phone-worker-update.json" not in names:
                    raise ValueError("manifesto do pacote de update ausente")
                manifest_info = zf.getinfo("phone-worker-update.json")
                if manifest_info.file_size > 64 * 1024:
                    raise ValueError("manifesto do pacote de update grande demais")
                manifest = json.loads(zf.read(manifest_info).decode("utf-8"))
                if not isinstance(manifest, dict) or manifest.get("schema") != "core-phone-worker-update-v1":
                    raise ValueError("schema inválido no pacote de update")
                manifest_files = manifest.get("files")
                if not isinstance(manifest_files, list) or not manifest_files or len(manifest_files) > 24:
                    raise ValueError("lista de arquivos inválida no pacote de update")
                for key in ("version", "source_hash"):
                    expected = str(payload.get(key) or "").strip()
                    actual = str(manifest.get(key) or "").strip()
                    if expected and actual != expected:
                        raise ValueError(f"{key} divergente no pacote de update")
                prepared: list[dict[str, Any]] = []
                total = 0
                expected_names = {"phone-worker-update.json"}
                seen_targets: set[str] = set()
                for item in manifest_files:
                    if not isinstance(item, dict):
                        raise ValueError("item inválido no manifesto de update")
                    target = _normalize_worker_update_target(str(item.get("target") or ""))
                    _target_path, allowed_mode = _safe_update_target_path(target)
                    declared_mode = int(item.get("mode") or allowed_mode)
                    if declared_mode != allowed_mode:
                        raise ValueError(f"modo não permitido no update: {target}")
                    if target in seen_targets:
                        raise ValueError(f"arquivo duplicado no manifesto: {target}")
                    seen_targets.add(target)
                    expected_names.add(target)
                    info = zf.getinfo(target)
                    if info.flag_bits & 0x1 or info.file_size > max_file_bytes:
                        raise ValueError(f"arquivo inválido/grande demais no update: {target}")
                    raw = zf.read(info)
                    total += len(raw)
                    if total > max_total_bytes:
                        raise ValueError("update grande demais")
                    sha = hashlib.sha256(raw).hexdigest()
                    if sha != str(item.get("sha256") or "").strip().lower() or len(raw) != int(item.get("bytes") or -1):
                        raise ValueError(f"integridade divergente em {target}")
                    prepared.append({"target": target, "mode": allowed_mode, "sha256": sha, "raw": raw})
                if set(names) != expected_names:
                    raise ValueError("pacote de update contém arquivo não declarado")
                return prepared, "zip-v1"

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("payload de update sem arquivos")
    if len(files) > 24:
        raise ValueError("arquivos demais no update")
    prepared = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("item inválido no update")
        target = _normalize_worker_update_target(str(item.get("target") or item.get("name") or ""))
        _target_path, allowed_mode = _safe_update_target_path(target)
        declared_mode = int(item.get("mode") or allowed_mode)
        if declared_mode != allowed_mode:
            raise ValueError(f"modo não permitido no update: {target}")
        raw = _b64decode(str(item.get("data_b64") or ""), max_bytes=max_file_bytes)
        total += len(raw)
        if total > max_total_bytes:
            raise ValueError("update grande demais")
        sha = hashlib.sha256(raw).hexdigest()
        expected = str(item.get("sha256") or "").strip().lower()
        if expected and expected != sha:
            raise ValueError(f"sha256 divergente em {target}")
        prepared.append({"target": target, "mode": allowed_mode, "sha256": sha, "raw": raw})
    return prepared, "inline-b64-v1"


def _apply_worker_update(payload: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("PHONE_WORKER_SELF_UPDATE_ENABLED", True):
        raise PermissionError("self-update do phone-worker desativado por configuração")
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    if target_source_hash and not re.fullmatch(r"[0-9a-f]{64}", target_source_hash):
        raise ValueError("source_hash do update ausente/inválido")
    current_source_hash = _phone_worker_source_hash()
    if target_source_hash and current_source_hash == target_source_hash:
        result: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "summary": "update já aplicado; hash da fonte confere",
            "current_version": PHONE_WORKER_VERSION,
            "source_hash": current_source_hash,
        }
        # Uma entrega pode ser repetida depois de os arquivos terem sido gravados,
        # mas antes de o watchdog reiniciar o processo antigo. Preserve o restart
        # no caminho idempotente para nunca ficar executando código anterior.
        if _env_bool("PHONE_WORKER_UPDATE_RESTART", bool(payload.get("restart", True))):
            result.update({
                "deferred_restart": True,
                "deferred_restart_mode": "watchdog",
                "_deferred_phone_worker_action": "restart",
                "_deferred_phone_worker_session": str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker"),
                "_deferred_start_script": str(_best_script("start-phone-worker.sh")),
                "_deferred_watch_script": str(_best_script("watch-phone-worker.sh")),
            })
        return result
    updated: list[dict[str, Any]] = []
    errors: list[str] = []
    max_file_bytes = max(1024, _env_int("PHONE_WORKER_UPDATE_MAX_FILE_BYTES", 768 * 1024))
    max_total_bytes = max(max_file_bytes, _env_int("PHONE_WORKER_UPDATE_MAX_TOTAL_BYTES", 2 * 1024 * 1024))
    files, update_transport = _load_worker_update_files(payload, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)
    total = sum(len(item.get("raw") or b"") for item in files)

    for item in files:
        target = str(item.get("target") or "").strip()
        try:
            normalized_target = _normalize_worker_update_target(target)
            path, mode = _safe_update_target_path(normalized_target)
            raw = bytes(item.get("raw") or b"")
            expected = str(item.get("sha256") or "").strip().lower()
            actual = hashlib.sha256(raw).hexdigest()
            if expected and expected != actual:
                raise ValueError(f"sha256 divergente em {target}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                backup = path.with_suffix(path.suffix + ".bak")
                with contextlib.suppress(Exception):
                    shutil.copy2(path, backup)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(raw)
            os.chmod(tmp, int(item.get("mode") or mode))
            tmp.replace(path)
            applied_paths = [path]
            if path.name in {"start-phone-worker.sh", "start-phone-music-agent.sh", "watch-phone-worker.sh", "pair-phone-worker.sh", "bootstrap-phone-worker.sh"}:
                # Espelhar scripts em ~/ também para instalações antigas e atalhos existentes.
                home_copy = _home_script(path.name)
                try:
                    if home_copy != path:
                        shutil.copy2(path, home_copy)
                        os.chmod(home_copy, int(item.get("mode") or mode))
                        applied_paths.append(home_copy)
                except Exception as mirror_exc:
                    errors.append(f"{target} mirror: {type(mirror_exc).__name__}: {_short_text(mirror_exc, limit=80)}")
            updated.append({"target": normalized_target, "paths": [str(p) for p in applied_paths], "bytes": len(raw), "sha256": actual[:12]})
        except Exception as exc:
            errors.append(f"{target or '<sem alvo>'}: {type(exc).__name__}: {_short_text(exc, limit=100)}")

    if errors:
        return {"ok": False, "summary": "update parcial/falhou", "updated": updated, "errors": errors[:8], "total_bytes": total}

    applied_source_hash = _phone_worker_source_hash()
    if target_source_hash and applied_source_hash != target_source_hash:
        return {
            "ok": False,
            "summary": "update aplicado, mas hash final da fonte divergiu",
            "updated": updated,
            "expected_source_hash": target_source_hash,
            "applied_source_hash": applied_source_hash,
            "total_bytes": total,
        }

    target_version = _short_text(payload.get("version"), limit=48, default="desconhecida")
    applied_version = _read_phone_worker_version_from_path(_phone_worker_dir() / "phone_worker.py") or target_version
    updated_names = {str(item.get("target") or "") for item in updated}
    applied_music_agent_version = _read_music_agent_version_from_path(_phone_worker_dir() / "music_agent.py")
    music_agent_restart: dict[str, Any] | None = None
    if {"music_agent.py", "start-phone-music-agent.sh"} & updated_names:
        try:
            music_agent_restart = _run_service_action("music-agent", "restart")
        except Exception as exc:
            errors.append(f"music-agent restart: {type(exc).__name__}: {_short_text(exc, limit=100)}")
    boot_status = _repair_termux_boot_script() if any(item.get("target") in {"start-phone-worker.sh", "start-phone-music-agent.sh", "watch-phone-worker.sh", "bootstrap-phone-worker.sh", "install.sh"} for item in updated) else _termux_boot_status_snapshot()
    shell_status = _repair_termux_shell_autostart()
    update_status = {
        "ok": True,
        "updated_at": time.time(),
        "previous_runtime_version": PHONE_WORKER_VERSION,
        "applied_file_version": applied_version,
        "applied_music_agent_version": applied_music_agent_version,
        "target_version": target_version,
        "source_hash": applied_source_hash,
        "update_transport": update_transport,
        "restart_requested": bool(payload.get("restart", True)),
        "files": [item.get("target") for item in updated],
        "boot_ok": bool(boot_status.get("ok")),
        "shell_autostart_ok": bool(shell_status.get("ok")),
    }
    with contextlib.suppress(Exception):
        _write_json_file_atomic(_phone_worker_update_status_file(), update_status)

    result: dict[str, Any] = {
        "ok": True,
        "summary": f"update aplicado: {len(updated)} arquivo(s); reinício pelo watchdog",
        "updated": updated,
        "total_bytes": total,
        "current_version": PHONE_WORKER_VERSION,
        "applied_file_version": applied_version,
        "applied_music_agent_version": applied_music_agent_version,
        "target_version": target_version,
        "source_hash": applied_source_hash,
        "update_transport": update_transport,
        "music_agent_restart": music_agent_restart,
        "boot": {"ok": bool(boot_status.get("ok")), "mode": boot_status.get("mode"), "summary": boot_status.get("summary")},
        "shell_autostart": {"ok": bool(shell_status.get("ok")), "summary": shell_status.get("summary"), "changed": bool(shell_status.get("changed"))},
        "update_status_file": str(_phone_worker_update_status_file()),
    }
    if _env_bool("PHONE_WORKER_UPDATE_RESTART", bool(payload.get("restart", True))):
        result.update({
            "deferred_restart": True,
            "deferred_restart_mode": "watchdog",
            "_deferred_phone_worker_action": "restart",
            "_deferred_phone_worker_session": str(os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker"),
            "_deferred_start_script": str(_best_script("start-phone-worker.sh")),
            "_deferred_watch_script": str(_best_script("watch-phone-worker.sh")),
        })
    return result


def _launch_deferred_phone_worker_action(result: dict[str, Any]) -> None:
    action = str(result.pop("_deferred_phone_worker_action", "") or "").strip().lower()
    if action not in {"stop", "restart"}:
        return
    session = str(result.pop("_deferred_phone_worker_session", "") or os.getenv("PHONE_WORKER_TMUX_SESSION") or "phone-worker")
    start_script = Path(str(result.pop("_deferred_start_script", "") or _best_script("start-phone-worker.sh"))).expanduser()
    watch_script = Path(str(result.pop("_deferred_watch_script", "") or _best_script("watch-phone-worker.sh"))).expanduser()
    worker_dir = _phone_worker_dir()
    script = worker_dir / f".core-worker-deferred-{action}.sh"
    lines = [
        "#!/data/data/com.termux/files/usr/bin/bash",
        "set +e",
        "sleep 1",
        "termux-wake-lock >/dev/null 2>&1 || true",
        f"tmux kill-session -t {shlex.quote(session)} >/dev/null 2>&1 || true",
        "pkill -f 'phone_worker.py' >/dev/null 2>&1 || true",
    ]
    if action == "restart":
        lines.extend([
            "sleep 1",
            f"bash {shlex.quote(str(start_script))} >/dev/null 2>&1 &",
            f"nohup bash {shlex.quote(str(watch_script))} >> {shlex.quote(str(_phone_worker_watch_log_file()))} 2>&1 &",
        ])
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o700)
        subprocess.Popen(["bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        print(f"[core-worker-service] falha ao agendar {action}: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)



def _assist_readiness_snapshot(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    roles, capabilities = _current_core_worker_roles_and_capabilities()
    battery = _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot())
    network = _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"})
    system = _safe_telemetry("system", _system_status, {"ok": False})
    level = None
    charging = False
    try:
        level = float((battery or {}).get("level") or (battery or {}).get("percent"))
        charging = str((battery or {}).get("status") or "").lower() in {"charging", "full"} or bool((battery or {}).get("plugged"))
    except Exception:
        level = None
    heavy_ok = True
    reasons: list[str] = []
    if level is not None and level < float(payload.get("min_battery_for_heavy") or 25) and not charging:
        heavy_ok = False
        reasons.append("bateria baixa para tarefa pesada")
    if not bool((network or {}).get("vps_reachable", True)) and _heartbeat_configured():
        reasons.append("VPS instável vista do worker")
    caps = set(capabilities) | set(roles)
    recommended: list[str] = ["log_summary", "zip_validate", "hash_batch", "endpoint_probe"]
    if "maintenance-plan" in caps or "cache-worker" in caps:
        recommended.append("maintenance_plan")
    if "ffprobe" in caps:
        recommended.append("media_probe")
    if "ffmpeg" in caps or "tts-convert" in caps:
        recommended.append("audio_convert")
    if "apk-builder" in caps and heavy_ok:
        recommended.append("apk_build_debug")
    return {
        "ok": True,
        "summary": "worker auxiliar pronto" if heavy_ok else "worker auxiliar só para tarefas leves",
        "assist_enabled": _env_bool("CORE_WORKER_ASSIST_ENABLED", True),
        "heavy_ok": heavy_ok,
        "reasons": reasons[:6],
        "profile": _current_core_worker_profile(),
        "roles": roles,
        "capabilities": capabilities,
        "recommended_tasks": recommended,
        "battery": battery,
        "network": network,
        "system": {
            "uptime_seconds": system.get("uptime_seconds"),
            "disk_home": system.get("disk_home"),
            "loadavg": system.get("loadavg"),
            "ffmpeg": system.get("ffmpeg"),
            "ffprobe": system.get("ffprobe"),
        },
    }

def _worker_logs_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    lines = max(20, min(400, _env_int("CORE_WORKER_LOG_LINES", int(payload.get("lines") or 120))))
    path = Path(str(payload.get("path") or _phone_worker_log_file())).expanduser()
    if not path.exists() or not path.is_file():
        return {"ok": False, "path": str(path), "error": "log não encontrado"}
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except Exception as exc:
        return {"ok": False, "path": str(path), "error": f"{type(exc).__name__}: {_short_text(exc, limit=120)}"}
    text = _sanitize_log_text("\n".join(data), limit=16000)
    return {
        "ok": True,
        "path": str(path),
        "lines": len(data),
        "tail": text,
        "error_lines": sum(1 for line in data if re.search(r"error|erro|exception|traceback|falha|failed", line, re.IGNORECASE)),
    }

def _task_runner(max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> WorkerHandler:
    runner = WorkerHandler.__new__(WorkerHandler)
    runner.server = SimpleNamespace(
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
        worker_token="",
    )
    return runner


def _sanitize_job_result(result: dict[str, Any]) -> dict[str, Any]:
    max_bytes = max(4096, _env_int("CORE_WORKER_JOB_RESULT_MAX_BYTES", DEFAULT_CORE_JOB_RESULT_MAX_BYTES))
    try:
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        return {"ok": False, "error": "resultado não serializável"}
    if len(raw) <= max_bytes:
        return result
    return {
        "ok": bool(result.get("ok", True)),
        "truncated": True,
        "original_json_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "keys": sorted(str(key)[:80] for key in result.keys())[:40],
        "summary": "resultado grande demais para salvar no registry; use rota direta do phone-worker para payload pesado",
    }


def _execute_core_worker_job(job: dict[str, Any], *, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> dict[str, Any]:
    global JOBS_STARTED, JOBS_FAILED
    kind = str(job.get("type") or "").strip().lower().replace("-", "_")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    JOBS_STARTED += 1
    try:
        if kind in {"ping", "status"}:
            result = _system_status()
        elif kind in {"diagnostic_basic", "worker_self_check"}:
            result = {
                "ok": True,
                "summary": "saúde do worker coletada",
                "system": _safe_telemetry("system", _system_status, {"ok": False}),
                "battery": _safe_telemetry("battery", _battery_snapshot, _empty_battery_snapshot()),
                "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}),
                "ping": _safe_telemetry("vps ping", _vps_tcp_ping_snapshot, {"available": False, "reachable": False, "source": "telemetry_failed"}),
                "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"}),
                "services": {
                    "phone-worker": _safe_telemetry("service phone-worker", lambda: _service_status("phone-worker"), {"ok": False}),
                    "phone-worker-watch": _safe_telemetry("service phone-worker-watch", lambda: _service_status("phone-worker-watch"), {"ok": False}),
                    "tailscale": _safe_telemetry("service tailscale", lambda: _service_status("tailscale"), {"ok": False}),
                    "sshd": _safe_telemetry("sshd", _sshd_snapshot, {"ok": False}),
                },
                "ffmpeg": _command_version("ffmpeg"),
                "ffprobe": _command_version("ffprobe"),
                "roles": _env_list("CORE_WORKER_ROLES", []),
                "capabilities": _env_list("CORE_WORKER_CAPABILITIES", []),
            }
        elif kind == "network_probe":
            result = {"ok": True, "summary": "rede testada", "network": _safe_telemetry("network", _network_snapshot, {"type": "unknown", "source": "telemetry_failed"}), "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "vps_assist_probe":
            result = _assist_readiness_snapshot(payload)
        elif kind == "tailscale_status":
            result = {"ok": True, "summary": "status Tailscale coletado", "tailscale": _safe_telemetry("tailscale", lambda: _tailscale_snapshot(probe_vps=True), {"connected": False, "state": "telemetry_failed"})}
        elif kind == "worker_logs":
            result = _worker_logs_snapshot(payload)
            result.setdefault("summary", "logs do phone-worker coletadas" if result.get("ok") else "falha ao coletar logs")
        elif kind == "worker_update":
            result = _apply_worker_update(payload)
            result.setdefault("summary", "arquivos do phone-worker atualizados")
        elif kind == "apk_build_debug":
            result = _apply_apk_build_debug(payload)
            result.setdefault("summary", "APK compilado/publicado pelo worker")
        elif kind == "apk_publish_last":
            result = _apply_apk_publish_last(payload)
            result.setdefault("summary", "último APK republicado pelo worker")
        elif kind == "boot_status":
            result = _termux_boot_status_snapshot()
            result["shell_autostart"] = _termux_shell_autostart_status_snapshot()
            result.setdefault("summary", "status do boot/shell automático coletado")
        elif kind == "boot_repair":
            result = _repair_termux_boot_script()
            result["shell_autostart"] = _repair_termux_shell_autostart()
            result["scripts"] = _script_inventory()
            result["supervisor"] = _runtime_supervisor_snapshot()
            result.setdefault("summary", "boot/shell automático reparado")
        elif kind in {"service_status", "service_start", "service_stop", "service_restart"}:
            service = payload.get("service") or "phone-worker"
            action = kind.removeprefix("service_")
            result = _run_service_action(str(service), action)
            result.setdefault("summary", f"{action} {result.get('service') or service}")
        elif kind == "ffmpeg_check":
            result = _command_version("ffmpeg")
            result.setdefault("summary", "ffmpeg verificado")
        elif kind == "ffprobe_check":
            result = _command_version("ffprobe")
            result.setdefault("summary", "ffprobe verificado")
        elif kind in {"music_agent_status", "music_agent_command"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_music_agent_proxy(payload)
            result.setdefault("summary", "Music Agent consultado pelo worker")
        elif kind == "music_ytdlp_resolve":
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_music_ytdlp_resolve(payload)
            result.setdefault("summary", "música resolvida pelo worker")
        elif kind == "emoji_recolor":
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_emoji_recolor(payload)
            result.setdefault("summary", "emojis recoloridos pelo worker")
        elif kind in {"media_probe", "ffprobe_media"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_ffprobe_media(payload)
            result.setdefault("summary", "mídia analisada pelo worker")
        elif kind in {"audio_convert", "ffmpeg_convert"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            result = runner._task_ffmpeg_convert(payload)
            result.setdefault("summary", "áudio convertido pelo worker")
        elif kind in {"zip_validate", "zip_audit", "log_summary", "log_digest", "text_stats", "maintenance_plan", "hash_batch", "endpoint_probe"}:
            runner = _task_runner(max_body_bytes, max_output_bytes, job_timeout)
            if kind == "zip_validate":
                result = runner._task_zip_validate(payload)
            elif kind in {"log_summary", "log_digest"}:
                result = runner._task_log_summary(payload)
                result.setdefault("summary", "logs resumidos pelo worker")
            elif kind == "zip_audit":
                result = runner._task_zip_validate(payload)
                result.setdefault("summary", "ZIP auditado pelo worker")
            elif kind == "hash_batch":
                result = runner._task_hash_batch(payload)
            elif kind == "endpoint_probe":
                result = runner._task_endpoint_probe(payload)
            elif kind == "text_stats":
                result = runner._task_text_stats(payload)
            else:
                result = runner._task_maintenance_plan(payload)
            result.setdefault("summary", kind)
        else:
            raise ValueError("job não permitido pelo worker")
        result.setdefault("ok", True)
        deferred = {key: result.pop(key) for key in list(result.keys()) if key.startswith("_deferred_")}
        clean = _sanitize_job_result(result)
        clean.update(deferred)
        return clean
    except Exception as exc:
        JOBS_FAILED += 1
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def _send_core_worker_job_result(*, job_id: str, status: str, result: dict[str, Any] | None = None, error: str = "", timeout: float = 8.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not worker_id:
        return False
    safe_result = dict(result or {})
    payload = {
        "worker_id": worker_id,
        "job_id": job_id,
        "status": status,
        "result": safe_result,
        "error": _short_text(error, limit=240),
        "summary": _short_text(safe_result.get("summary") or error or status, limit=160),
    }
    ok, code, data = _post_core_worker_job_result_payload_status(payload, timeout=timeout)
    if not ok:
        if _job_result_rejection_is_permanent(code, data):
            _archive_pending_core_job_result(job_id, payload, reason=f"VPS não reconhece mais este job HTTP {code}", response=data)
        else:
            _store_pending_core_job_result(payload)
    return ok


def _poll_core_worker_job_once(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int, timeout: float = 8.0) -> bool:
    _base_url, _token, worker_id = _core_worker_auth_parts()
    if not worker_id:
        return False
    _flush_pending_core_worker_job_results(timeout=timeout)
    payload = _core_worker_payload(host=host, port=port)
    code, data = _post_core_worker_json("/core-worker/jobs/poll", payload, timeout=timeout)
    if not (200 <= code < 300):
        print(f"[core-worker-jobs] poll HTTP {code}: {_short_text(data.get('error') or data, limit=180)}", flush=True)
        return False
    job = data.get("job") if isinstance(data.get("job"), dict) else None
    if not job:
        return False
    job_id = str(job.get("job_id") or "").strip()
    kind = str(job.get("type") or "").strip()
    if not job_id:
        return False
    print(f"[core-worker-jobs] executando {job_id} ({kind})", flush=True)
    _set_core_job_active(job)
    try:
        result = _execute_core_worker_job(job, max_body_bytes=max_body_bytes, max_output_bytes=max_output_bytes, job_timeout=job_timeout)
        result_ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        final_status = "succeeded" if result_ok else "failed"
        summary = str(result.get("summary") or ("ok" if result_ok else "ação falhou"))
        ok = _send_core_worker_job_result(job_id=job_id, status=final_status, result=result, error="" if result_ok else summary, timeout=timeout)
        _finish_core_job(job_id, kind, final_status, summary=summary, sent_ok=ok)
        # Self-update/restart must happen even when the result could not be sent
        # because the exact issue we are fixing is route/VPN failure during update.
        # The result is persisted and retried after restart/reconnect.
        if result_ok:
            _launch_deferred_phone_worker_action(result)
    except Exception as exc:
        summary = f"{type(exc).__name__}: {exc}"
        ok = _send_core_worker_job_result(job_id=job_id, status="failed", result={}, error=summary, timeout=timeout)
        _finish_core_job(job_id, kind, "failed", summary=summary, sent_ok=ok)
    return True


def _start_core_worker_jobs(*, host: str, port: int, max_body_bytes: int, max_output_bytes: int, job_timeout: int) -> None:
    if not _core_worker_jobs_configured():
        print("[core-worker-jobs] desativado ou incompleto; habilite CORE_WORKER_HEARTBEAT_ENABLED/JOBS e configure URL, ID e TOKEN", flush=True)
        return
    interval = max(3.0, min(120.0, _env_float("CORE_WORKER_JOB_POLL_INTERVAL_SECONDS", DEFAULT_JOB_POLL_INTERVAL_SECONDS)))

    def loop() -> None:
        while True:
            try:
                ran_job = _poll_core_worker_job_once(
                    host=host,
                    port=port,
                    max_body_bytes=max_body_bytes,
                    max_output_bytes=max_output_bytes,
                    job_timeout=job_timeout,
                    timeout=8.0,
                )
                time.sleep(0.5 if ran_job else interval)
            except Exception as exc:
                print(f"[core-worker-jobs] loop falhou: {type(exc).__name__}: {_short_text(exc, limit=120)}", flush=True)
                time.sleep(interval)

    thread = threading.Thread(target=loop, name="core-worker-jobs", daemon=True)
    thread.start()
    print(f"[core-worker-jobs] polling ativo; intervalo={int(interval)}s", flush=True)

def main() -> int:
    _load_env_file()
    _load_persisted_pending_core_job_results()
    parser = argparse.ArgumentParser(description="Worker auxiliar do celular para tarefas opcionais da VPS.")
    parser.add_argument("--host", default=os.getenv("PHONE_WORKER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("PHONE_WORKER_PORT", 8766))
    parser.add_argument("--token", default=os.getenv("PHONE_WORKER_TOKEN", ""))
    parser.add_argument("--max-body-mb", type=int, default=_env_int("PHONE_WORKER_MAX_BODY_MB", DEFAULT_MAX_BODY_MB))
    parser.add_argument("--max-output-mb", type=int, default=_env_int("PHONE_WORKER_MAX_OUTPUT_MB", DEFAULT_MAX_OUTPUT_MB))
    parser.add_argument("--job-timeout", type=int, default=_env_int("PHONE_WORKER_JOB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    parser.add_argument("--heartbeat-once", action="store_true", help="envia um heartbeat para a VPS e encerra")
    parser.add_argument("--jobs-once", action="store_true", help="faz um poll de job na VPS, executa no máximo um job e encerra")
    parser.add_argument("--pair", "--pair-code", dest="pair_code", default="", help="pareia este phone-worker com o registry usando um código CORE-XXXX")
    parser.add_argument("--vps-url", default=os.getenv("CORE_WORKER_VPS_URL", ""), help="URL base da VPS/Tailscale para pareamento, ex: http://100.x.x.x:10000")
    parser.add_argument("--worker-id", default=os.getenv("CORE_WORKER_ID", ""), help="ID estável deste worker; opcional")
    parser.add_argument("--name", default=os.getenv("CORE_WORKER_NAME", os.getenv("PHONE_WORKER_NAME", "")), help="nome exibido no painel")
    parser.add_argument("--roles", default=os.getenv("CORE_WORKER_ROLES", ""), help="roles do worker separadas por vírgula")
    parser.add_argument("--capabilities", default=os.getenv("CORE_WORKER_CAPABILITIES", ""), help="capacidades do worker separadas por vírgula")
    parser.add_argument("--env-file", default=os.getenv("PHONE_WORKER_ENV", str(Path.home() / ".phone-worker.env")), help="arquivo .env local a atualizar no pareamento")
    args = parser.parse_args()

    max_body_bytes = max(1, args.max_body_mb) * 1024 * 1024
    max_output_bytes = max(1, args.max_output_mb) * 1024 * 1024
    job_timeout = max(3, args.job_timeout)

    if args.pair_code:
        result = _pair_core_worker(
            code=args.pair_code,
            vps_url=args.vps_url,
            host=args.host,
            port=args.port,
            worker_id=args.worker_id,
            name=args.name,
            roles=args.roles,
            capabilities=args.capabilities,
            env_file=args.env_file,
            timeout=10.0,
        )
        return 0 if result.get("ok") else 1
    if args.heartbeat_once:
        ok = _send_core_worker_heartbeat_once(host=args.host, port=args.port, timeout=8.0)
        return 0 if ok else 1
    if args.jobs_once:
        ok = _poll_core_worker_job_once(
            host=args.host,
            port=args.port,
            max_body_bytes=max_body_bytes,
            max_output_bytes=max_output_bytes,
            job_timeout=job_timeout,
            timeout=8.0,
        )
        return 0 if ok else 1

    server = ThreadingHTTPServer((args.host, args.port), WorkerHandler)
    server.worker_token = args.token
    server.phone_worker_host = args.host
    server.phone_worker_port = args.port
    server.max_body_bytes = max_body_bytes
    server.max_output_bytes = max_output_bytes
    server.job_timeout = job_timeout
    print(f"[phone-worker] ouvindo em {args.host}:{args.port}; token={'sim' if args.token else 'não'}; versão={PHONE_WORKER_VERSION}", flush=True)
    _start_core_worker_heartbeat(host=args.host, port=args.port)
    _start_core_worker_jobs(
        host=args.host,
        port=args.port,
        max_body_bytes=max_body_bytes,
        max_output_bytes=max_output_bytes,
        job_timeout=job_timeout,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
