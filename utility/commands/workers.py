from __future__ import annotations

import asyncio
import base64
import hashlib
import contextlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from utility.apk_identity import assert_expected_apk_identity, inspect_apk_identity
from utility.commands.workers_registry import get_core_workers_registry
from utility.interaction_safety import (
    safe_defer_interaction,
    safe_edit_original_or_message,
)


logger = logging.getLogger(__name__)

CORE_WORKER_APK_REPLACES_TERMUX = str(os.getenv("CORE_WORKER_APK_REPLACES_TERMUX", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}

WORKERS_COMMAND_GUILD_ID = 927002914449424404
WORKERS_COMMAND_GUILD = discord.Object(id=WORKERS_COMMAND_GUILD_ID)
WORKERS_PANEL_TIMEOUT_SECONDS = 180.0
WORKERS_DEFAULT_ROLES = (
    "vps",
    "diagnostics",
    "log-summary",
    "maintenance-plan",
    "zip-validate",
    "ffmpeg",
    "ffprobe",
    "tts-convert",
)
CORE_WORKER_AUTO_WAKE_DEFAULT_INTERVAL_SECONDS = 180.0
CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS = 300
CORE_WORKER_AUTO_WAKE_APK_MIN_VERSION_CODE = 74
CORE_WORKER_AUTO_WAKE_APK_SKIP_LOG_INTERVAL_SECONDS = 900.0
CORE_WORKER_IMPORTANT_WAKE_ROLES = {
    "diagnostics",
    "log-summary",
    "maintenance-plan",
    "zip-validate",
    "ffmpeg",
    "ffprobe",
    "tts-convert",
    "apk-builder",
    "vps-assist",
    "cache-worker",
    "service-control",
    "boot-repair",
    "bedrock",
}
CORE_WORKER_IMPORTANT_WAKE_TASKS = {
    "worker_self_check",
    "worker_logs",
    "log_digest",
    "zip_audit",
    "zip_validate",
    "maintenance_plan",
    "apk_build_debug",
    "apk_publish_last",
    "vps_assist_probe",
    "endpoint_probe",
    "service_status",
    "service_start",
    "service_stop",
    "service_restart",
    "boot_status",
    "boot_repair",
}

WORKER_ROLE_PROFILES: dict[str, tuple[str, ...]] = {
    "leve": ("apk-worker", "diagnostics", "log-summary"),
    "midia": ("apk-worker", "diagnostics", "log-summary", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"),
    "completo": ("apk-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert"),
    # Bootstrap: o primeiro APK self-builder ainda é compilado pelo phone-worker/Termux.
    "builder": ("phone-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "apk-builder", "vps-assist", "cache-worker"),
    # Após o toolchain interno ficar pronto, o próprio APK anuncia apk-builder dinamicamente.
    "turbo": ("apk-worker", "diagnostics", "log-summary", "maintenance-plan", "zip-validate", "ffmpeg", "ffprobe", "tts-convert", "vps-assist", "cache-worker"),
    "bedrock": ("apk-worker", "diagnostics", "log-summary", "bedrock", "bedrock-logs", "bedrock-backup"),
}

WORKER_ROLE_LABELS: dict[str, str] = {
    "apk-worker": "Core Worker APK",
    "phone-worker": "Termux bootstrap",
    "diagnostics": "Diagnósticos",
    "log-summary": "Resumo de logs",
    "maintenance-plan": "Plano de manutenção",
    "zip-validate": "Validar ZIP",
    "ffmpeg": "FFmpeg",
    "ffprobe": "FFprobe",
    "tts-convert": "Converter/cache TTS",
    "bedrock": "Minecraft Bedrock",
    "bedrock-logs": "Logs Bedrock",
    "bedrock-backup": "Backup Bedrock",
    "apk-builder": "Compilar APK",
    "vps-assist": "Ajudar VPS",
    "cache-worker": "Cache auxiliar",
    "hash-worker": "Hashes",
    "endpoint-probe": "Teste de endpoint",
    "media-probe": "Análise de mídia",
    "audio-convert": "Converter áudio",
    "worker-logs": "Logs do worker",
    "network-probe": "Teste de rede",
    "tailscale-status": "Status Tailscale",
    "service-control": "Controle de serviços",
    "boot-repair": "Reparo de boot",
}

WORKER_ROLE_PROFILE_DESCRIPTIONS: dict[str, str] = {
    "leve": "economia, diagnósticos e logs",
    "midia": "perfil normal recomendado",
    "completo": "tarefas extras e manutenção segura",
    "builder": "bootstrap no Termux até o APK assumir o autobuild",
    "turbo": "máximo desempenho para acelerar a VPS",
    "bedrock": "Minecraft Bedrock futuro",
}

WORKER_EDITABLE_FEATURES: tuple[dict[str, Any], ...] = (
    {"value": "apk-worker", "label": "Base APK", "description": "Runtime direto sem Termux", "emoji": "📱", "roles": ("apk-worker",), "capabilities": ("apk-worker",), "tasks": ("ping", "status")},
    {"value": "diagnostics", "label": "Diagnóstico", "description": "Saúde e checks básicos", "emoji": "🩺", "roles": ("diagnostics",), "capabilities": ("diagnostics",), "tasks": ("diagnostic_basic", "worker_self_check")},
    {"value": "log-summary", "label": "Logs", "description": "Resumo e leitura de logs", "emoji": "🧾", "roles": ("log-summary",), "capabilities": ("log-summary", "worker-logs"), "tasks": ("log_summary", "log_digest", "worker_logs", "text_stats")},
    {"value": "maintenance-plan", "label": "Manutenção", "description": "Plano seguro de limpeza/cache", "emoji": "🧹", "roles": ("maintenance-plan",), "capabilities": ("maintenance-plan", "cache-worker"), "tasks": ("maintenance_plan",)},
    {"value": "zip-validate", "label": "ZIP / patch", "description": "Validar e auditar ZIP", "emoji": "🧪", "roles": ("zip-validate",), "capabilities": ("zip-validate",), "tasks": ("zip_validate", "zip_audit")},
    {"value": "apk-builder", "label": "APK builder", "description": "Termux bootstrap ou autobuild interno", "emoji": "🏗️", "roles": ("apk-builder",), "capabilities": ("apk-builder",), "tasks": ("apk_build_debug", "apk_publish_last")},
    {"value": "vps-assist", "label": "Ajudar VPS", "description": "Offload seguro quando disponível", "emoji": "🧠", "roles": ("vps-assist",), "capabilities": ("vps-assist", "hash-worker", "endpoint-probe"), "tasks": ("vps_assist_probe", "hash_batch", "endpoint_probe")},
    {"value": "ffmpeg", "label": "FFmpeg", "description": "Conversão de áudio curta", "emoji": "🎚️", "roles": ("ffmpeg",), "capabilities": ("ffmpeg", "audio-convert"), "tasks": ("ffmpeg_check", "audio_convert")},
    {"value": "ffprobe", "label": "FFprobe", "description": "Analisar mídia", "emoji": "🔎", "roles": ("ffprobe",), "capabilities": ("ffprobe", "media-probe"), "tasks": ("ffprobe_check", "media_probe")},
    {"value": "tts-convert", "label": "TTS/cache", "description": "Preparar áudio para TTS", "emoji": "🔊", "roles": ("tts-convert",), "capabilities": ("tts-convert", "audio-convert"), "tasks": ("audio_convert",)},
    {"value": "network-probe", "label": "Rede", "description": "Teste de rede/endpoint", "emoji": "📡", "roles": (), "capabilities": ("network-probe", "endpoint-probe"), "tasks": ("network_probe", "endpoint_probe")},
    {"value": "tailscale-status", "label": "Tailscale", "description": "Status da rede privada", "emoji": "🌐", "roles": (), "capabilities": ("tailscale-status",), "tasks": ("tailscale_status",)},
    {"value": "bedrock", "label": "Bedrock", "description": "Minecraft Bedrock futuro", "emoji": "🧱", "roles": ("bedrock", "bedrock-logs", "bedrock-backup"), "capabilities": ("bedrock", "bedrock-logs", "bedrock-backup"), "tasks": ()},
)

_FEATURE_BY_VALUE: dict[str, dict[str, Any]] = {str(item["value"]): item for item in WORKER_EDITABLE_FEATURES}
LEGACY_WORKER_ID = "__legacy_phone_worker__"
AUTO_WORKER_ID = "__auto_core_worker__"

WORKER_ACTION_SPECS: tuple[dict[str, Any], ...] = (
    {"label": "Testar worker", "value": "ping", "job_type": "ping", "payload": {}, "summary": "teste manual pelo painel workers", "description": "Testa comunicação", "emoji": "🧪", "category": "quick"},
    {"label": "Saúde", "value": "worker_self_check", "job_type": "worker_self_check", "payload": {}, "summary": "saúde completa pelo painel workers", "description": "Bateria, rede e sistema", "emoji": "🩺", "category": "quick"},
    {"label": "Atualizar agent", "value": "worker_update_quick", "job_type": "worker_update", "payload": {}, "summary": "atualizar arquivos do phone-worker", "description": "Corrige agent antigo", "emoji": "⬆️", "category": "quick"},
    {"label": "Buildar APK", "value": "apk_build_debug_quick", "job_type": "apk_build_debug", "payload": {"source_zip_url": "auto", "publish": True}, "summary": "compilar APK Core Worker em worker builder", "description": "Retry manual do APK", "emoji": "🏗️", "category": "quick"},
    {"label": "Republicar APK", "value": "apk_publish_last_quick", "job_type": "apk_publish_last", "payload": {}, "summary": "republicar último APK Core Worker", "description": "Sem rebuild", "emoji": "📤", "category": "quick"},
    {"label": "Atualizar agent", "value": "worker_update", "job_type": "worker_update", "payload": {}, "summary": "atualizar arquivos do phone-worker", "description": "Atualiza e reinicia", "emoji": "⬆️", "requires_declared": True, "category": "maintenance"},
    {"label": "Reparar scripts", "value": "worker_repair_scripts", "job_type": "worker_update", "payload": {"scripts_only": True}, "summary": "reinstalar scripts auxiliares do worker", "description": "Reinstala scripts", "emoji": "🛠️", "requires_declared": True, "category": "maintenance"},
    {"label": "Buildar APK", "value": "apk_build_debug", "job_type": "apk_build_debug", "payload": {"source_zip_url": "auto", "publish": True}, "summary": "compilar APK Core Worker em worker builder", "description": "APK fora da VPS", "emoji": "🏗️", "requires_declared": True, "category": "maintenance"},
    {"label": "Republicar APK", "value": "apk_publish_last", "job_type": "apk_publish_last", "payload": {}, "summary": "republicar último APK gerado", "description": "Só publicação", "emoji": "📤", "requires_declared": True, "category": "maintenance"},
    {"label": "Teste auxiliar", "value": "vps_assist_probe", "job_type": "vps_assist_probe", "payload": {}, "summary": "medir se este worker pode ajudar a VPS", "description": "Pronto para ajudar", "emoji": "🧠", "requires_declared": True, "category": "assist"},
    {"label": "Resumir logs VPS", "value": "vps_log_digest", "job_type": "log_digest", "payload": {"source": "vps_logs_auto"}, "summary": "resumir logs recentes da VPS em worker", "description": "Logs fora da VPS", "emoji": "🧾", "requires_declared": True, "category": "assist"},
    {"label": "Auditar patch ZIP", "value": "vps_zip_audit", "job_type": "zip_audit", "payload": {"source": "latest_update_zip"}, "summary": "auditar ZIP recente usando worker", "description": "Valida ZIP", "emoji": "🧪", "requires_declared": True, "category": "assist"},
    {"label": "Plano de limpeza", "value": "vps_maintenance_plan", "job_type": "maintenance_plan", "payload": {"source": "vps_scan_auto"}, "summary": "planejar limpeza/caches com worker", "description": "Sugestão segura", "emoji": "🧹", "requires_declared": True, "category": "assist"},
    {"label": "Testar VPS pelo celular", "value": "endpoint_probe", "job_type": "endpoint_probe", "payload": {"targets": ["auto"]}, "summary": "testar endpoints da VPS a partir do worker", "description": "Ping HTTP real", "emoji": "📡", "requires_declared": True, "category": "assist"},
    {"label": "Reparar boot automático", "value": "boot_repair", "job_type": "boot_repair", "payload": {}, "summary": "reparar inicialização automática no Termux:Boot", "description": "Auto-start pós-reboot", "emoji": "🚀", "requires_declared": True, "category": "maintenance"},
    {"label": "Status boot", "value": "boot_status", "job_type": "boot_status", "payload": {}, "summary": "verificar inicialização automática", "description": "BootReceiver do APK", "emoji": "🔎", "requires_declared": True, "category": "monitor"},
    {"label": "Logs", "value": "worker_logs", "job_type": "worker_logs", "payload": {"lines": 140}, "summary": "logs recentes do Core Worker APK", "description": "Mostra logs recentes", "emoji": "📜", "category": "quick"},
    {"label": "Tailscale", "value": "tailscale_status", "job_type": "tailscale_status", "payload": {}, "summary": "status Tailscale e alcance da VPS", "description": "Rede privada/VPS", "emoji": "🌐", "category": "monitor"},
    {"label": "Status serviços", "value": "service_status", "job_type": "service_status", "payload": {"service": "core-worker-apk"}, "summary": "status do serviço do APK", "description": "Serviços permitidos", "emoji": "🧰", "category": "monitor"},
    {"label": "Iniciar watchdog", "value": "service_start_watch", "job_type": "service_start", "payload": {"service": "phone-worker-watch"}, "summary": "iniciar watchdog do phone-worker", "description": "Inicia watchdog", "emoji": "▶️", "category": "maintenance"},
    {"label": "Parar watchdog", "value": "service_stop_watch", "job_type": "service_stop", "payload": {"service": "phone-worker-watch"}, "summary": "parar watchdog do phone-worker", "description": "Para watchdog", "emoji": "⏹️", "category": "maintenance"},
    {"label": "Reiniciar worker", "value": "service_restart_worker", "job_type": "service_restart", "payload": {"service": "phone-worker"}, "summary": "reiniciar phone-worker no celular", "description": "Reinicia agent", "emoji": "🔁", "category": "maintenance"},
    {"label": "Parar worker", "value": "service_stop_worker", "job_type": "service_stop", "payload": {"service": "phone-worker"}, "summary": "parar phone-worker no celular", "description": "Para agent", "emoji": "🛑", "category": "maintenance"},
 )


WORKER_ACTION_CATEGORIES: tuple[dict[str, str], ...] = (
    {"label": "Ações rápidas", "value": "quick", "description": "Teste, saúde, logs", "emoji": "⚡"},
    {"label": "Monitoramento", "value": "monitor", "description": "Rede, Tailscale, serviços", "emoji": "📊"},
    {"label": "Manutenção", "value": "maintenance", "description": "Reparar, reiniciar e boot", "emoji": "🛠️"},
    {"label": "Ajudar VPS", "value": "assist", "description": "Tarefas auxiliares seguras", "emoji": "🧠"},
    {"label": "APK interno", "value": "apk", "description": "Jobs seguros sem shell", "emoji": "📲"},
    {"label": "Organizar", "value": "organize", "description": "Nome, funções, pausar/remover", "emoji": "🧩"},
    {"label": "Adicionar celular", "value": "add", "description": "Pareamento e guia simples", "emoji": "📲"},
)


# A interface principal dos workers fica propositalmente enxuta.
# Ações internas/diagnósticas continuam existindo no backend, mas não aparecem
# no select normal para não lotar o painel com tarefas que raramente são usadas.
WORKER_PANEL_ACTION_ORDER: tuple[str, ...] = (
    "_worker_details",
    "ping",
    "worker_self_check",
    "apk_build_debug_quick",
    "apk_publish_last_quick",
    "_apk_internal_test",
    "worker_logs",
    "endpoint_probe",
    "_show_last_result",
    "_rename_worker",
    "_pause_worker",
    "_resume_worker",
    "_delete_worker",
)
WORKER_PANEL_ACTION_ORDER_INDEX: dict[str, int] = {value: index for index, value in enumerate(WORKER_PANEL_ACTION_ORDER)}
WORKER_PANEL_ACTION_VALUES: set[str] = set(WORKER_PANEL_ACTION_ORDER)


_SECRET_PATTERNS = (
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"(PHONE_WORKER_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(CORE_WORKER_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(X-Phone-Worker-Token:\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(X-Core-Worker-Token:\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(\"(?:apkSigningKeystoreB64|apkSigningStorePassword|apkSigningKeyPassword)\"\s*:\s*\")[^\"]+", re.IGNORECASE),
    re.compile(r"(apkSigning(?:KeystoreB64|StorePassword|KeyPassword)\s*[:=]\s*)[^,}\s]+", re.IGNORECASE),
    re.compile(r"(CORE_WORKER_SIGNING_(?:STORE_PASSWORD|KEY_PASSWORD)\s*[:=]\s*)[^,}\s]+", re.IGNORECASE),
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _shorten(text: object, *, limit: int = 80) -> str:
    value = str(text or "").replace("\n", " ").strip()
    value = re.sub(r"\s+", " ", value)
    if len(value) > limit:
        return value[: max(1, limit - 1)].rstrip() + "…"
    return value


def _redact(text: object) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(r"\1[redacted]", value)
    return value


def _format_seconds(value: object) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except Exception:
        return "desconhecido"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts[:3])


def _format_age(value: object) -> str:
    if value is None:
        return "nunca"
    try:
        seconds = max(0.0, float(value))
    except Exception:
        return "desconhecido"
    if seconds < 2:
        return "agora"
    return f"há {_format_seconds(seconds)}"


def _format_bytes(value: object) -> str:
    try:
        size = max(0, float(value or 0))
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"




def _expected_phone_worker_version() -> str:
    path = _repo_root() / "deploy" / "termux" / "phone-worker" / "phone_worker.py"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else ""


def _expected_apk_version() -> tuple[str, int]:
    path = _repo_root() / "android" / "core-worker-app" / "app" / "build.gradle"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "", 0
    name = re.search(r'versionName\s+["\']([^"\']+)["\']', text)
    code = re.search(r"versionCode\s+(\d+)", text)
    return (name.group(1) if name else "", int(code.group(1)) if code else 0)


def _version_tuple(value: object) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) if parts else (0,)




def _active_workers_need_agent_version(workers: list[dict[str, Any]], target_version: object) -> bool:
    target = str(target_version or "").strip()
    if not target:
        return False
    for worker in workers or []:
        if not isinstance(worker, dict) or not worker.get("online") or worker.get("enabled") is False:
            continue
        tokens = set(str(item).replace("-", "_") for item in (worker.get("supported_tasks") or []))
        caps = {str(item) for item in (worker.get("capabilities") or [])} | {str(item) for item in (worker.get("roles") or [])}
        if "phone-worker" not in caps:
            continue
        if tokens and "worker_update" not in tokens:
            continue
        current = str(worker.get("version") or "")
        if not current or _version_tuple(current) < _version_tuple(target):
            return True
    return False

def _agent_version_label(current: object) -> str:
    version = _shorten(current or "sem versão", limit=24)
    expected = _expected_phone_worker_version()
    if expected and current and _version_tuple(current) < _version_tuple(expected):
        return f"{version} → {expected} pendente"
    if expected and not current:
        return f"sem versão → {expected} pendente"
    return version


def _parse_roles(raw: str | None, *, status: dict[str, Any] | None = None) -> list[str]:
    roles: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        clean = item.strip().lower().replace("_", "-")
        if clean and clean not in roles:
            roles.append(clean)
    if not roles:
        roles.extend(WORKERS_DEFAULT_ROLES)
    status = status or {}
    if status.get("ffmpeg") and "ffmpeg" not in roles:
        roles.append("ffmpeg")
    if status.get("ffprobe") and "ffprobe" not in roles:
        roles.append("ffprobe")
    return roles[:16]


def _split_role_list(raw: object, *, limit: int = 24) -> list[str]:
    roles: list[str] = []
    for item in re.split(r"[,;\s]+", str(raw or "")):
        clean = item.strip().lower().replace("_", "-")
        clean = re.sub(r"[^a-z0-9_.:-]+", "-", clean).strip("-._:")
        if clean and clean not in roles:
            roles.append(clean[:32])
        if len(roles) >= limit:
            break
    return roles


def _role_label(role: object) -> str:
    key = str(role or "").strip().lower().replace("_", "-")
    return WORKER_ROLE_LABELS.get(key, key or "função")


def _normalize_worker_profile(value: object, *, default: str = "midia") -> str:
    clean = str(value or "").strip().lower().replace("í", "i").replace("_", "-")
    aliases = {
        "media": "midia",
        "mídia": "midia",
        "midia": "midia",
        "leve": "leve",
        "lite": "leve",
        "completo": "completo",
        "complete": "completo",
        "full": "completo",
                "turbo": "turbo",
        "max": "turbo",
        "rapido": "turbo",
        "rápido": "turbo",
        "bedrock": "bedrock",
    }
    clean = aliases.get(clean, clean)
    if clean not in WORKER_ROLE_PROFILES:
        return default if default in WORKER_ROLE_PROFILES else "midia"
    return clean



def _unique_ordered(items: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        clean = str(item or "").strip().lower().replace("_", "-")
        clean = re.sub(r"[^a-z0-9_.:-]+", "-", clean).strip("-._:")
        if clean and clean not in result:
            result.append(clean[:40])
    return result


def _worker_profile_label(worker: dict[str, Any] | None) -> str:
    if not isinstance(worker, dict):
        return "Normal"
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    raw = worker.get("profile") or status.get("profile") or worker.get("profile_label") or status.get("profile_label") or "midia"
    profile = _normalize_worker_profile(raw)
    labels = {
        "leve": "Leve",
        "midia": "Normal",
        "completo": "Completo",
        "turbo": "Turbo",
        "bedrock": "Bedrock",
    }
    return labels.get(profile, "Normal")



def _worker_runtime_label(worker: dict[str, Any] | None) -> str:
    if not isinstance(worker, dict):
        return "desconhecido"
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else worker.get("runtime") if isinstance(worker.get("runtime"), dict) else {}
    raw = str(worker.get("runtime_mode") or status.get("runtime_mode") or runtime.get("mode") or "").strip().lower()
    source = str(worker.get("source") or "").strip().lower()
    internal = str(runtime.get("internal_runtime") or status.get("internal_runtime_state") or worker.get("internal_runtime_state") or "").strip().lower()
    if not raw and "termux" in source:
        raw = "termux"
    if raw in {"termux", "termux-phone-worker"}:
        if internal and internal not in {"", "none", "unknown"}:
            return "Termux atual · interno em preparação"
        return "Termux atual"
    if raw in {"internal", "core-runtime", "internal-runtime"}:
        return "Runtime interno"
    if raw in {"internal_preview", "preview"}:
        return "Runtime interno preview"
    return raw or "desconhecido"

def _core_worker_app_runtime_record(worker_id: str) -> dict[str, Any] | None:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return None

    # Preferir o snapshot compacto criado pelo webserver. Ele evita carregar o
    # heartbeat grande em caminhos frequentes do painel/auto-wake.
    snapshot_path = _repo_root() / "data" / "core_worker_app_runtime_snapshot.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    except Exception:
        snapshot = {}
    latest_snapshot = snapshot.get("latest") if isinstance(snapshot, dict) else None
    if isinstance(latest_snapshot, dict) and str(latest_snapshot.get("workerId") or "").strip() == worker_id:
        return latest_snapshot

    path = _repo_root() / "data" / "core_worker_app_heartbeats.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    latest = data.get("latestByWorkerId") if isinstance(data, dict) and isinstance(data.get("latestByWorkerId"), dict) else {}
    record = latest.get(worker_id) if isinstance(latest, dict) else None
    return record if isinstance(record, dict) else None


def _core_worker_record_age_seconds(record: dict[str, Any], *, now: float | None = None) -> float | None:
    if not isinstance(record, dict):
        return None
    if now is None:
        now = time.time()
    candidates = (
        record.get("receivedAt"),
        record.get("updatedAt"),
        record.get("lastSeenAt"),
    )
    for candidate in candidates:
        try:
            seen = float(candidate or 0)
        except Exception:
            seen = 0
        if seen > 0:
            return max(0.0, now - seen)
    return None


def _core_worker_apk_runtime_record_is_online(
    record: dict[str, Any],
    *,
    now: float | None = None,
    max_age_seconds: int | float | None = None,
    min_version_code: int | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    if now is None:
        now = time.time()
    if max_age_seconds is None:
        max_age_seconds = int(_env_float("CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS", CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS))
    if min_version_code is None:
        min_version_code = int(_env_float("CORE_WORKER_AUTO_WAKE_APK_MIN_VERSION_CODE", CORE_WORKER_AUTO_WAKE_APK_MIN_VERSION_CODE))

    age = _core_worker_record_age_seconds(record, now=now)
    if age is None or age > float(max_age_seconds):
        return False

    source = str(record.get("source") or "").strip().lower()
    internal_runtime = str(record.get("internalRuntime") or record.get("internal_runtime") or "").strip().lower()
    runtime_mode = str(record.get("runtimeMode") or record.get("runtime_mode") or "").strip().lower()
    looks_like_apk = (
        source.startswith("core-worker-apk")
        or internal_runtime.startswith("apk-")
        or "apk-native" in runtime_mode
    )
    if not looks_like_apk:
        return False

    try:
        version_code = int(record.get("appVersionCode") or 0)
    except Exception:
        version_code = 0
    if version_code < int(min_version_code):
        return False

    capabilities = {str(item) for item in (record.get("capabilities") or [])}
    supported = {str(item) for item in (record.get("supported_tasks") or record.get("supportedTasks") or record.get("appJobs") or [])}
    core_linux = record.get("coreLinux") if isinstance(record.get("coreLinux"), dict) else {}
    prepared = bool(record.get("coreLinuxPrepared") or core_linux.get("prepared"))
    state = str(record.get("coreLinuxState") or core_linux.get("state") or "").strip().lower()
    summary = str(record.get("coreLinuxSummary") or core_linux.get("summary") or "").strip().lower()

    has_runtime_v1 = "core-linux-runtime-v1" in capabilities
    has_smoke_job = "apk_core_linux_runtime_smoke_test" in supported
    state_ready = state in {"runtime_v1_ready", "ready", "ok", "rootfs_ready", "executor_ready", "rootfs_real_validated"}
    summary_ready = "runtime v1" in summary and "pronto" in summary

    return bool(prepared and (has_runtime_v1 or has_smoke_job or state_ready or summary_ready))


def _core_worker_iter_apk_runtime_records() -> list[dict[str, Any]]:
    """Lê registros compactos do APK sem depender do payload grande de heartbeat.

    O snapshot compacto é a fonte preferida. O heartbeat fica como fallback para
    bases antigas/primeiro ciclo depois de restart.
    """
    root = _repo_root()
    records: list[dict[str, Any]] = []

    snapshot_path = root / "data" / "core_worker_app_runtime_snapshot.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    except Exception:
        snapshot = {}
    if isinstance(snapshot, dict):
        latest = snapshot.get("latest")
        if isinstance(latest, dict):
            if not latest.get("updatedAt") and snapshot.get("updatedAt"):
                latest = dict(latest)
                latest.setdefault("updatedAt", snapshot.get("updatedAt"))
            records.append(latest)

    heartbeat_path = root / "data" / "core_worker_app_heartbeats.json"
    try:
        data = json.loads(heartbeat_path.read_text(encoding="utf-8")) if heartbeat_path.exists() else {}
    except Exception:
        data = {}
    if isinstance(data, dict):
        for bucket_name in ("latestByInstallId", "latestByWorkerId"):
            bucket = data.get(bucket_name)
            if isinstance(bucket, dict):
                records.extend(item for item in bucket.values() if isinstance(item, dict))

    # Dedupe simples preservando prioridade snapshot -> latest heartbeat.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        key = (
            str(record.get("installId") or ""),
            str(record.get("workerId") or ""),
            str(record.get("receivedAt") or record.get("updatedAt") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _core_worker_any_apk_runtime_online(max_age_seconds: int | float | None = None) -> bool:
    """Retorna True quando o APK/Core Linux interno atual está online.

    O auto-wake legado serve para acordar Termux/phone-worker. Quando o Core
    Worker APK interno já está online e pronto, tentar acordar Termux só gera
    SSH/timeout/log e lag na VPS pequena.
    """
    if max_age_seconds is None:
        max_age_seconds = int(_env_float("CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS", CORE_WORKER_AUTO_WAKE_APK_ONLINE_MAX_AGE_SECONDS))
    now = time.time()
    for record in _core_worker_iter_apk_runtime_records():
        if _core_worker_apk_runtime_record_is_online(record, now=now, max_age_seconds=max_age_seconds):
            return True
    return False


CORE_WORKER_APP_JOB_ALIASES = {
    "apk_clear_app_cache": "apk_cache_cleanup",
    "apk_cleanup_runtime_cache": "apk_cache_cleanup",
    "apk_report_logs": "apk_upload_app_logs",
    "apk_status_refresh": "apk_sync_runtime_state",
    "apk_trim_runtime_cache": "apk_trim_cache",
    "apk_cleanup_update_storage": "apk_update_storage_cleanup",
    "apk_clear_update_downloads": "apk_update_storage_cleanup",
    "apk_refresh_status": "apk_refresh_runtime",
}
CORE_WORKER_APP_AUTO_JOB_TYPES = {
    "apk_ping",
    "apk_diagnostic",
    "apk_check_update",
    "apk_upload_app_logs",
    "apk_runtime_diagnostic",
    "apk_worker_bridge_status",
    "apk_device_diagnostic",
    "apk_push_diagnostic",
    "apk_update_diagnostic",
    "apk_job_history",
    "apk_native_worker_status",
    "apk_native_boot_status",
    "apk_local_shell_probe",
    "apk_core_linux_native_executor_probe",
    "apk_core_linux_native_executor_test",
    "apk_core_linux_native_runtime_status",
    "apk_core_linux_rootfs_status",
    "apk_core_linux_rootfs_validate",
    "apk_core_linux_runtime_smoke_test",
}

CORE_WORKER_APP_MANUAL_JOB_TYPES = {
    "apk_download_small",
    "apk_verify_file",
    "apk_upload_report",
    "apk_test_vps_connection",
    "apk_sync_profile",
    "apk_sync_runtime_state",
    "apk_refresh_runtime",
    "apk_force_status_bundle",
    "apk_test_notification",
    "apk_repair_local_state",
    "apk_reset_job_history",
    "apk_trim_cache",
    "apk_update_storage_cleanup",
    "apk_sync_profile_now",
    "apk_verify_update_state",
    "apk_native_worker_status",
    "apk_native_boot_status",
    "apk_local_shell_probe",
    "apk_python_runtime_probe",
    "apk_python_health_check",
    "apk_python_runtime_info",
    "apk_python_status_bundle",
    "apk_python_storage_check",
    "apk_python_log_summary",
    "apk_python_network_diagnostic",
    "apk_python_runtime_files_check",
    "apk_linux_runtime_probe",
    "apk_linux_rootfs_probe",
    "apk_core_linux_rootfs_status",
    "apk_core_linux_rootfs_preflight",
    "apk_core_linux_rootfs_prepare",
    "apk_core_linux_rootfs_validate",
    "apk_core_linux_rootfs_repair",
    "apk_core_linux_rootfs_clean_staging",
    "apk_core_linux_rootfs_import_status",
    "apk_core_linux_rootfs_import_validate",
    "apk_core_linux_rootfs_import_abort",
    "apk_core_linux_rootfs_real_status",
    "apk_core_linux_runner_status",
    "apk_core_linux_runner_preflight",
    "apk_core_linux_runner_requirements",
    "apk_linux_box64_probe",
    "apk_linux_provisioner_probe",
    "apk_linux_prepare_directories",
    "apk_linux_generate_setup_plan",
    "apk_core_linux_internal_probe",
    "apk_core_linux_internal_bootstrap",
    "apk_core_linux_executor_probe",
    "apk_core_linux_rootfs_manifest",
    "apk_core_linux_box64_manifest",
    "apk_core_linux_bedrock_preflight",
    "apk_core_linux_native_executor_probe",
    "apk_core_linux_native_executor_test",
    "apk_core_linux_native_runtime_status",
    "apk_core_linux_internal_repair",
    "apk_minecraft_bedrock_probe",
    "apk_minecraft_bedrock_status",
    "apk_minecraft_bedrock_requirements",
    "apk_minecraft_bedrock_install_plan",
    "apk_minecraft_bedrock_properties_template",
    "apk_runtime_foreground_probe",
    "apk_runtime_foreground_start",
    "apk_runtime_foreground_stop",
    "apk_linux_strategy_plan",
    "apk_linux_manifest_plan",
    "apk_minecraft_bedrock_assisted_install_plan",
    "apk_minecraft_bedrock_prepare_files",
    "apk_minecraft_bedrock_start_plan",
    "apk_minecraft_bedrock_stop_plan",
    "apk_minecraft_bedrock_logs_status",
    "apk_minecraft_bedrock_installer_status",
    "apk_minecraft_bedrock_validate_device",
    "apk_minecraft_bedrock_choose_strategy_plan",
    "apk_minecraft_bedrock_prepare_environment_plan",
    "apk_minecraft_bedrock_download_manifest",
    "apk_minecraft_bedrock_final_preflight",
    "apk_minecraft_bedrock_runtime_status",
    "apk_minecraft_bedrock_runtime_start",
    "apk_minecraft_bedrock_runtime_stop",
    "apk_minecraft_bedrock_runtime_logs",
    "apk_minecraft_bedrock_runner_status",
    "apk_minecraft_bedrock_runner_preflight",
    "apk_minecraft_bedrock_runner_start",
    "apk_minecraft_bedrock_runner_stop",
    "apk_minecraft_bedrock_console_tail",
    "apk_minecraft_bedrock_console_command",
    "apk_minecraft_bedrock_runtime_repair",
}
CORE_WORKER_APP_JOB_LABELS = {
    "apk_ping": "ping interno",
    "apk_diagnostic": "diagnóstico geral",
    "apk_check_update": "checagem de atualização",
    "apk_upload_app_logs": "logs internos",
    "apk_runtime_diagnostic": "runtime",
    "apk_worker_bridge_status": "ponte APK/Termux",
    "apk_storage_diagnostic": "armazenamento",
    "apk_collect_status_bundle": "pacote completo",
    "apk_device_diagnostic": "aparelho",
    "apk_network_diagnostic": "rede",
    "apk_push_diagnostic": "push",
    "apk_update_diagnostic": "update",
    "apk_job_history": "histórico",
    "apk_cache_cleanup": "limpeza de cache",
    "apk_refresh_runtime": "atualizar runtime",
    "apk_force_status_bundle": "pacote de status",
    "apk_test_notification": "teste de notificação",
    "apk_repair_local_state": "reparar estado local",
    "apk_reset_job_history": "limpar histórico",
    "apk_trim_cache": "limpar cache",
    "apk_update_storage_cleanup": "limpar updates",
    "apk_sync_profile_now": "sincronizar perfil agora",
    "apk_verify_update_state": "verificar atualização",
    "apk_native_worker_status": "worker nativo",
    "apk_native_boot_status": "boot nativo",
    "apk_local_shell_probe": "shell controlado",
    "apk_python_runtime_probe": "python interno",
    "apk_python_health_check": "Python health check",
    "apk_python_runtime_info": "Python runtime info",
    "apk_python_status_bundle": "Python status bundle",
    "apk_python_storage_check": "Python storage check",
    "apk_python_log_summary": "Python resumo de logs",
    "apk_python_network_diagnostic": "Python diagnóstico de rede",
    "apk_python_runtime_files_check": "Python arquivos runtime",
    "apk_linux_runtime_probe": "Core Linux runtime",
    "apk_linux_rootfs_probe": "Linux rootfs",
    "apk_core_linux_rootfs_status": "rootfs status",
    "apk_core_linux_rootfs_preflight": "rootfs preflight",
    "apk_core_linux_rootfs_prepare": "preparar rootfs",
    "apk_core_linux_rootfs_validate": "validar rootfs",
    "apk_core_linux_rootfs_repair": "reparar rootfs",
    "apk_core_linux_rootfs_clean_staging": "limpar staging rootfs",
    "apk_core_linux_rootfs_import_status": "status import rootfs",
    "apk_core_linux_rootfs_import_validate": "validar rootfs real",
    "apk_core_linux_rootfs_import_abort": "cancelar import rootfs",
    "apk_core_linux_rootfs_real_status": "rootfs real",
    "apk_core_linux_runner_status": "runner Core Linux status",
    "apk_core_linux_runner_preflight": "runner Core Linux preflight",
    "apk_core_linux_runner_requirements": "runner Core Linux requisitos",
    "apk_linux_box64_probe": "Box64",
    "apk_linux_provisioner_probe": "Linux provisioner",
    "apk_linux_prepare_directories": "preparar diretórios Linux",
    "apk_linux_generate_setup_plan": "plano setup Linux",
    "apk_core_linux_internal_probe": "Core Linux interno",
    "apk_core_linux_internal_bootstrap": "bootstrap interno",
    "apk_core_linux_executor_probe": "executor interno",
    "apk_core_linux_rootfs_manifest": "manifesto rootfs interno",
    "apk_core_linux_box64_manifest": "manifesto Box64 interno",
    "apk_core_linux_bedrock_preflight": "preflight Bedrock interno",
    "apk_core_linux_native_executor_probe": "executor nativo interno",
    "apk_core_linux_native_executor_test": "teste executor nativo",
    "apk_core_linux_native_runtime_status": "runtime nativo interno",
    "apk_core_linux_runtime_smoke_test": "smoke test Core Linux",
    "apk_core_linux_internal_repair": "reparar Core Linux interno",
    "apk_minecraft_bedrock_probe": "Bedrock diagnóstico",
    "apk_minecraft_bedrock_status": "Bedrock status",
    "apk_minecraft_bedrock_requirements": "Bedrock requisitos",
    "apk_minecraft_bedrock_install_plan": "Bedrock plano instalação",
    "apk_minecraft_bedrock_properties_template": "Bedrock template propriedades",
    "apk_runtime_foreground_probe": "runtime persistente",
    "apk_runtime_foreground_start": "iniciar runtime persistente",
    "apk_runtime_foreground_stop": "parar runtime persistente",
    "apk_linux_strategy_plan": "estratégia Linux",
    "apk_linux_manifest_plan": "manifesto Linux",
    "apk_minecraft_bedrock_assisted_install_plan": "Bedrock assistido",
    "apk_minecraft_bedrock_prepare_files": "Bedrock preparar arquivos",
    "apk_minecraft_bedrock_start_plan": "Bedrock plano start",
    "apk_minecraft_bedrock_stop_plan": "Bedrock plano stop",
    "apk_minecraft_bedrock_logs_status": "Bedrock logs status",
    "apk_minecraft_bedrock_installer_status": "Bedrock instalador status",
    "apk_minecraft_bedrock_validate_device": "Bedrock validar aparelho",
    "apk_minecraft_bedrock_choose_strategy_plan": "Bedrock escolher estratégia",
    "apk_minecraft_bedrock_prepare_environment_plan": "Bedrock preparar ambiente",
    "apk_minecraft_bedrock_download_manifest": "Bedrock manifesto downloads",
    "apk_minecraft_bedrock_final_preflight": "Bedrock preflight final",
    "apk_minecraft_bedrock_runtime_status": "Bedrock runtime status",
    "apk_minecraft_bedrock_runtime_start": "Bedrock runtime start",
    "apk_minecraft_bedrock_runtime_stop": "Bedrock runtime stop",
    "apk_minecraft_bedrock_runtime_logs": "Bedrock runtime logs",
    "apk_minecraft_bedrock_runner_status": "Bedrock runner status",
    "apk_minecraft_bedrock_runner_preflight": "Bedrock runner preflight",
    "apk_minecraft_bedrock_runner_start": "Bedrock runner start",
    "apk_minecraft_bedrock_runner_stop": "Bedrock runner stop",
    "apk_minecraft_bedrock_console_tail": "Bedrock console tail",
    "apk_minecraft_bedrock_console_command": "Bedrock console comando",
    "apk_minecraft_bedrock_runtime_repair": "Bedrock runtime reparar",
}


def _core_worker_app_normalize_job_type(value: Any) -> str:
    raw = str(value or "").strip()
    return CORE_WORKER_APP_JOB_ALIASES.get(raw, raw)


def _core_worker_scrub_eula_public_text(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if "eula" in text.lower():
        return ""
    return text


def _core_worker_app_job_key_from_record(record: dict[str, Any]) -> str:
    return str(record.get("installId") or record.get("workerId") or "unknown")


def _core_worker_app_jobs_summary_for_worker(worker_id: str) -> dict[str, Any]:
    worker_id = str(worker_id or "").strip()
    path = _repo_root() / "data" / "core_worker_app_jobs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {}
    hb = _core_worker_app_runtime_record(worker_id) or {}
    install_id = str(hb.get("installId") or "")
    summaries = data.get("summaryByInstallId") if isinstance(data.get("summaryByInstallId"), dict) else {}
    for key in (install_id, worker_id, "unknown"):
        summary = summaries.get(key) if key else None
        if isinstance(summary, dict):
            return _core_worker_app_refine_jobs_summary(summary)
    # Compatibilidade se o arquivo ainda não foi regravado pelo Patch 65.
    latest_by_type: dict[str, dict[str, Any]] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        if worker_id or install_id:
            item_worker = str(item.get("workerId") or "")
            item_install = str(item.get("installId") or "")
            if not ((worker_id and item_worker == worker_id) or (install_id and item_install == install_id)):
                continue
        typ = _core_worker_app_normalize_job_type(item.get("type"))
        previous = latest_by_type.get(typ)
        if previous is None or float(item.get("receivedAt") or 0) >= float(previous.get("receivedAt") or 0):
            latest_by_type[typ] = item
    auto_ok = sum(1 for typ in CORE_WORKER_APP_AUTO_JOB_TYPES if bool((latest_by_type.get(typ) or {}).get("ok")))
    auto_failed = sum(1 for typ in CORE_WORKER_APP_AUTO_JOB_TYPES if isinstance(latest_by_type.get(typ), dict) and not bool((latest_by_type.get(typ) or {}).get("ok")))
    auto_missing = [typ for typ in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES) if typ not in latest_by_type]
    return _core_worker_app_refine_jobs_summary({"autoTotal": len(CORE_WORKER_APP_AUTO_JOB_TYPES), "autoOk": auto_ok, "autoFailed": auto_failed, "autoMissing": auto_missing, "manualTotal": len(CORE_WORKER_APP_MANUAL_JOB_TYPES), "pending": len(data.get("pending") or []), "running": len(data.get("runningByJobId") or {}), "latestByType": {k: {"ok": bool(v.get("ok")), "message": _core_worker_scrub_eula_public_text(v.get("message") or v.get("error") or ""), "receivedAt": int(v.get("receivedAt") or 0)} for k, v in latest_by_type.items()}})




def _core_worker_app_job_issue_is_expected(job_type: str, item: dict[str, Any]) -> bool:
    """Classifica estados esperados para não virarem falha no painel principal."""
    if not isinstance(item, dict):
        return False
    joined_parts: list[str] = []
    for key in ("state", "status", "message", "error", "summary"):
        value = item.get(key)
        if value:
            joined_parts.append(str(value))
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    diagnostic = item.get("diagnostic") if isinstance(item.get("diagnostic"), dict) else {}
    for source in (result, diagnostic):
        for key in ("state", "status", "message", "error", "summary", "native_worker_state", "bedrock_state"):
            value = source.get(key)
            if value:
                joined_parts.append(str(value))
        missing = source.get("missing")
        if isinstance(missing, (list, tuple)):
            joined_parts.extend(str(x) for x in missing[:8])
    text = " ".join(joined_parts).lower()
    typ = _core_worker_app_normalize_job_type(job_type).lower()
    expected_needles = (
        "not-configured",
        "não configurado",
        "nao configurado",
        "bedrockserver",
        "serverproperties",
        "worldsdir",
        "logsdir",
        "token nativo ausente",
        "native token missing",
        "ponte aguardando",
        "aguardando",
        "blocked",
        "isolado",
        "isolated",
        "manual-only",
        "manual only",
    )
    if any(needle in text for needle in expected_needles):
        return True
    if typ.startswith("apk_minecraft_bedrock_") and ("missing" in text or "falt" in text):
        return True
    return False


def _core_worker_app_job_ok(job_type: str, item: Any) -> bool | None:
    if not isinstance(item, dict):
        return None
    if _core_worker_app_job_issue_is_expected(job_type, item):
        return True
    for key in ("ok", "success", "succeeded"):
        if key in item:
            return bool(item.get(key))
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    if "ok" in result:
        return bool(result.get("ok")) or _core_worker_app_job_issue_is_expected(job_type, result)
    state = str(item.get("state") or item.get("status") or "").strip().lower()
    if state in {"ok", "success", "succeeded", "not-configured", "não configurado", "nao configurado"}:
        return True
    if state in {"failed", "error", "erro"}:
        return False
    return None


def _core_worker_app_refine_jobs_summary(summary: dict[str, Any]) -> dict[str, Any]:
    refined = dict(summary or {})
    latest = refined.get("latestByType") if isinstance(refined.get("latestByType"), dict) else {}
    if latest:
        total = int(refined.get("autoTotal") or len(CORE_WORKER_APP_AUTO_JOB_TYPES))
        ok_count = 0
        failed_count = 0
        missing: list[str] = []
        expected_pending: list[str] = []
        failed_details: list[dict[str, Any]] = []
        for typ in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
            item = latest.get(typ)
            state = _core_worker_app_job_ok(typ, item)
            if state is True:
                ok_count += 1
                if isinstance(item, dict) and _core_worker_app_job_issue_is_expected(typ, item):
                    expected_pending.append(typ)
            elif state is False:
                failed_count += 1
                message = ""
                if isinstance(item, dict):
                    result = item.get("result") if isinstance(item.get("result"), dict) else {}
                    message = str(item.get("message") or item.get("error") or result.get("summary") or result.get("error") or "").strip()
                failed_details.append({
                    "type": typ,
                    "label": CORE_WORKER_APP_JOB_LABELS.get(typ, typ),
                    "message": _shorten(message, limit=160),
                    "receivedAt": int((item or {}).get("receivedAt") or 0) if isinstance(item, dict) else 0,
                })
            else:
                missing.append(typ)
        refined["autoTotal"] = total
        refined["autoOk"] = ok_count
        refined["autoFailed"] = failed_count
        refined["autoMissing"] = missing
        refined["autoExpectedPending"] = expected_pending
        refined["autoFailedDetails"] = failed_details
    return refined

def _queue_core_worker_app_internal_runtime_test(worker_id: str) -> dict[str, Any]:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return {"ok": False, "error": "worker não selecionado"}
    registry = get_core_workers_registry()
    created: list[str] = []
    errors: list[str] = []
    for typ in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
        try:
            result = registry.create_job(
                job_type=typ,
                payload={"reason": "manual-runtime-test", "source": "workers-panel"},
                target_worker_id=worker_id,
                ttl_seconds=1800,
                lease_seconds=180,
                max_attempts=2,
                summary=CORE_WORKER_APP_JOB_LABELS.get(typ, typ),
            )
            job = result.get("job") if isinstance(result, dict) else {}
            if isinstance(job, dict) and job.get("job_id"):
                created.append(typ)
        except Exception as exc:
            errors.append(f"{typ}: {type(exc).__name__}: {exc}")
    return {
        "ok": bool(created) and not errors,
        "created": len(created),
        "createdTypes": created,
        "errors": errors[:5],
        "workerId": worker_id,
        "protocol": "core-worker-registry-v1",
    }


def _queue_core_worker_app_manual_job(worker_id: str, job_type: str, *, payload: dict[str, Any] | None = None, reason: str = "manual-apk-command") -> dict[str, Any]:
    worker_id = str(worker_id or "").strip()
    job_type = _core_worker_app_normalize_job_type(job_type)
    if not worker_id:
        return {"ok": False, "error": "worker não selecionado"}
    if job_type not in CORE_WORKER_APP_MANUAL_JOB_TYPES and job_type not in CORE_WORKER_APP_AUTO_JOB_TYPES:
        return {"ok": False, "error": f"job APK não permitido: {job_type}"}
    try:
        result = get_core_workers_registry().create_job(
            job_type=job_type,
            payload=dict(payload or {}),
            target_worker_id=worker_id,
            ttl_seconds=1800,
            lease_seconds=180,
            max_attempts=2,
            summary=CORE_WORKER_APP_JOB_LABELS.get(job_type, job_type),
        )
        job = result.get("job") if isinstance(result, dict) else {}
        return {
            "ok": True,
            "created": 1,
            "type": job_type,
            "workerId": worker_id,
            "jobId": str((job or {}).get("job_id") or ""),
            "protocol": "core-worker-registry-v1",
            "reason": reason,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "type": job_type, "workerId": worker_id}


def _core_worker_app_jobs_text(worker_id: str) -> str:
    worker_id = str(worker_id or "").strip()
    summary = _core_worker_app_jobs_summary_for_worker(worker_id)
    if not summary:
        return "jobs internos: aguardando"
    total = int(summary.get("autoTotal") or len(CORE_WORKER_APP_AUTO_JOB_TYPES))
    ok = int(summary.get("autoOk") or 0)
    failed = int(summary.get("autoFailed") or 0)
    pending = int(summary.get("pending") or 0)
    running = int(summary.get("running") or 0)
    missing = summary.get("autoMissing") if isinstance(summary.get("autoMissing"), list) else []
    failed_details = summary.get("autoFailedDetails") if isinstance(summary.get("autoFailedDetails"), list) else []
    expected_pending = summary.get("autoExpectedPending") if isinstance(summary.get("autoExpectedPending"), list) else []
    if failed:
        label = f"jobs internos: {ok}/{total} ok · {failed} falha(s)"
    elif ok >= total and total:
        label = f"jobs internos: {ok}/{total} ok"
    elif ok:
        label = f"jobs internos: {ok}/{total} ok · aquecendo"
    else:
        label = "jobs internos: aguardando cobertura"
    extra: list[str] = []
    if running:
        extra.append(f"{running} rodando")
    if pending:
        extra.append(f"{pending} pend")
    if missing and ok < total:
        extra.append(f"faltam {len(missing)}")
    if expected_pending and not failed:
        extra.append(f"{len(expected_pending)} pendência(s) esperada(s)")
    if failed_details:
        first = failed_details[0] if isinstance(failed_details[0], dict) else {}
        detail = _shorten(first.get("label") or first.get("type") or "job", limit=34)
        message = _shorten(first.get("message") or "", limit=52)
        extra.append(f"última falha: {detail}" + (f" ({message})" if message else ""))
    manual_total = int(summary.get("manualTotal") or len(CORE_WORKER_APP_MANUAL_JOB_TYPES))
    if manual_total:
        extra.append(f"{manual_total} manuais")
    return " · ".join([label] + extra)


def _core_worker_app_queue_text_for_runtime(worker_id: str, record: dict[str, Any]) -> str:
    summary = _core_worker_app_jobs_summary_for_worker(worker_id)
    try:
        running = int(summary.get("running") or 0) if isinstance(summary, dict) else 0
    except Exception:
        running = 0
    try:
        pending = int(summary.get("pending") or 0) if isinstance(summary, dict) else 0
    except Exception:
        pending = 0
    if running or pending:
        parts: list[str] = []
        if running:
            parts.append(f"{running} rodando")
        if pending:
            parts.append(f"{pending} pendentes")
        return " · ".join(parts)
    raw = _shorten(record.get("internalJobsQueue") or "", limit=40) if isinstance(record, dict) else ""
    # Patch 68 podia manter no heartbeat um texto transitório como "6 rodando · 0 pendentes"
    # mesmo depois de a VPS já ter recebido todos os resultados. Quando o resumo oficial
    # por instalação diz 0/0, tratamos a fila como vazia.
    if raw and "rodando" in raw.lower():
        return "fila vazia"
    return raw or "fila vazia"


def _core_worker_app_runtime_detail_text(worker_id: str) -> str:
    record = _core_worker_app_runtime_record(str(worker_id or ""))
    if not isinstance(record, dict):
        return "APK interno: sem heartbeat direto ainda"
    diag = _shorten(record.get("diagnosticsSummary") or "diagnóstico aguardando", limit=90)
    storage = _shorten(record.get("storageSummary") or "armazenamento aguardando", limit=80)
    bridge = _shorten(record.get("bridgeSummary") or "ponte aguardando", limit=80)
    core_linux = record.get("coreLinux") if isinstance(record.get("coreLinux"), dict) else {}
    linux = _shorten(core_linux.get("runnerPhaseSummary") or record.get("coreLinuxSummary") or "Linux runtime aguardando", limit=90)
    bedrock = _shorten(record.get("bedrockSummary") or "Bedrock aguardando", limit=80)
    perm = _shorten(record.get("notificationPermission") or "notificação ?", limit=32)
    jobs = _core_worker_app_jobs_text(worker_id)
    supported = record.get("supported_tasks") or record.get("supportedTasks") or record.get("appJobs") or []
    supported_count = len(supported) if isinstance(supported, list) else 0
    support_text = f" · ações {supported_count}" if supported_count else ""
    return f"APK interno: diagnóstico {diag} · {storage} · {bridge} · {linux} · {bedrock} · notif {perm}{support_text} · {jobs}"


def _core_worker_app_runtime_text(worker_id: str) -> str:
    """Resumo curto para o card principal do Discord.

    O painel é snapshot manual; detalhes completos ficam no app e na ação
    "Detalhes do celular". Aqui evitamos despejar estado técnico/truncado.
    """
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return "aguardando vínculo"
    record = _core_worker_app_runtime_record(worker_id)
    if not isinstance(record, dict):
        return "sem heartbeat direto"
    try:
        seen = float(record.get("receivedAt") or 0)
    except Exception:
        seen = 0.0
    age = max(0.0, time.time() - seen) if seen else None
    online = age is not None and age <= 180
    app_version = _shorten(record.get("appVersion") or "APK", limit=16)
    status = "online" if online else "visto " + _format_age(age)
    parts = [status, app_version]

    # Estados principais, em português natural.
    python_ok = False
    jobs_runtime = str(record.get("jobsRuntime") or record.get("runtimeMode") or "").lower()
    if "python" in jobs_runtime or "chaquopy" in str(record.get("diagnosticsSummary") or "").lower():
        python_ok = True
    parts.append("Python ok" if python_ok else "Python preparando")

    fg = str(record.get("foregroundRuntimeSummary") or "").strip().lower()
    fg_active = bool(record.get("foregroundRuntimeActive"))
    if fg_active or "ativo" in fg:
        parts.append("runtime persistente ativo")
    elif fg:
        parts.append("runtime persistente parado")

    core_linux = record.get("coreLinux") if isinstance(record.get("coreLinux"), dict) else {}
    linux_state = str(record.get("coreLinuxState") or core_linux.get("state") or "").lower()
    linux_summary = str(record.get("coreLinuxSummary") or core_linux.get("summary") or "").lower()
    rootfs_state = str(core_linux.get("rootfsState") or core_linux.get("rootfsImportState") or "").lower()
    rootfs_level = str(core_linux.get("rootfsValidationLevel") or "").lower()
    if "rootfs_real_validated" in linux_state or "rootfs_real_validated" in rootfs_state or rootfs_level == "real":
        parts.append("Rootfs real validado")
        if core_linux.get("termuxReductionReady") or core_linux.get("runnerBaseRequirementsReady"):
            parts.append("base sem Termux pronta")
            parts.append("smoke test pendente")
        elif core_linux.get("baseToolsReady"):
            parts.append("base tools pronta")
            parts.append("runner bloqueado")
        else:
            parts.append("runner bloqueado")
    elif record.get("coreLinuxPrepared") or "pronto" in linux_summary or "plan" in linux_state:
        parts.append("Linux pronto")
    elif linux_summary:
        parts.append("Linux preparando")

    bedrock_summary = str(record.get("bedrockSummary") or "")
    bedrock_installer = str(record.get("bedrockInstallerSummary") or "")
    bedrock_runtime = str(record.get("bedrockRuntimeSummary") or "")
    bedrock_joined = (bedrock_summary + " " + bedrock_installer + " " + bedrock_runtime).lower()
    if record.get("bedrockRuntimeServiceActive") or "serviço bedrock ativo" in bedrock_joined:
        parts.append("Bedrock runtime ativo")
    elif record.get("bedrockReady") or "pronto para iniciar" in bedrock_joined:
        parts.append("Bedrock pronto")
    elif "servidor não instalado" in bedrock_joined or "não instalado" in bedrock_joined:
        parts.append("Bedrock não instalado")
    elif "instalador" in bedrock_joined or "bedrock" in bedrock_joined:
        parts.append("Bedrock assistido")

    q = _core_worker_app_queue_text_for_runtime(worker_id, record)
    if q and q != "fila vazia":
        parts.append(f"fila {q}")
    jobs_text = _core_worker_app_jobs_text(worker_id)
    if jobs_text and "aguardando" not in jobs_text:
        # Compacta "jobs internos: 19/19 ok · 47 manuais" para caber melhor.
        jobs_text = jobs_text.replace("jobs internos: ", "jobs ")
        parts.append(jobs_text)

    return " · ".join(str(x) for x in parts if x)

def _profile_feature_values(profile: str) -> set[str]:
    values: set[str] = {"apk-worker"}
    roles = set(WORKER_ROLE_PROFILES.get(profile, WORKER_ROLE_PROFILES.get("midia", ())))
    for value, feature in _FEATURE_BY_VALUE.items():
        feature_roles = set(str(x) for x in feature.get("roles") or ())
        feature_caps = set(str(x) for x in feature.get("capabilities") or ())
        if value == "apk-worker" or value in roles or roles.intersection(feature_roles | feature_caps):
            values.add(value)
    if profile == "turbo":
        values.update(str(item["value"]) for item in WORKER_EDITABLE_FEATURES if str(item.get("value")) != "bedrock")
    return values


def _feature_values_from_worker(worker: dict[str, Any]) -> set[str]:
    raw_roles = [str(x) for x in (worker.get("roles") or []) if x]
    raw_caps = [str(x) for x in (worker.get("capabilities") or []) if x]
    all_tokens = set(_unique_ordered(raw_roles + raw_caps))
    values: set[str] = {"apk-worker"}
    for value, feature in _FEATURE_BY_VALUE.items():
        tokens = {value}
        tokens.update(str(x) for x in feature.get("roles") or ())
        tokens.update(str(x) for x in feature.get("capabilities") or ())
        normalized = set(_unique_ordered(list(tokens)))
        if all_tokens.intersection(normalized):
            values.add(value)
    return values


def _roles_caps_tasks_for_features(values: set[str], *, profile: str = "manter") -> tuple[list[str], list[str], list[str]]:
    selected = set(values or set())
    selected.add("apk-worker")
    roles: list[str] = []
    caps: list[str] = []
    tasks: list[str] = []
    # Começa pelo perfil quando ele é explícito, depois aplica as marcações.
    if profile in WORKER_ROLE_PROFILES:
        roles.extend(WORKER_ROLE_PROFILES.get(profile, ()))
    for value in selected:
        feature = _FEATURE_BY_VALUE.get(value)
        if not feature:
            continue
        roles.extend(str(x) for x in feature.get("roles") or ())
        caps.extend(str(x) for x in feature.get("capabilities") or ())
        tasks.extend(str(x) for x in feature.get("tasks") or ())
    caps.extend(roles)
    roles = _unique_ordered(roles)[:16]
    caps = _unique_ordered(caps)[:24]
    tasks_normalized: list[str] = []
    for item in tasks:
        clean = _task_name(item)
        if clean and clean not in tasks_normalized:
            tasks_normalized.append(clean[:48])
    return roles, caps, tasks_normalized[:96]

def _host_label(host: str) -> str:
    host = str(host or "").strip()
    if not host:
        return "não configurado"
    # Tailscale costuma usar 100.x.y.z. Não é segredo, mas mascarar reduz
    # vazamento acidental em print do painel.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        parts = host.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    if len(host) > 28:
        return host[:14] + "…" + host[-8:]
    return host


def _repo_root() -> Path:
    # utility/commands/workers.py -> repo root
    return Path(__file__).resolve().parents[2]


def _verified_latest_apk_manifest() -> dict[str, Any]:
    """Lê latest.json, mas substitui a versão declarada pela identidade do APK."""
    release_dir = _repo_root() / "android" / "core-worker-app" / "releases"
    latest_path = release_dir / "latest.json"
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if not isinstance(latest, dict):
            return {}
        filename = str(latest.get("filename") or "").strip()
        if not filename:
            apk_url = str(latest.get("apkUrl") or latest.get("downloadUrl") or "").split("?", 1)[0].rstrip("/")
            filename = apk_url.rsplit("/", 1)[-1]
        if not filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".apk"):
            return {}
        apk_path = (release_dir / filename).resolve()
        if apk_path.parent != release_dir.resolve() or not apk_path.is_file():
            return {}
        identity = inspect_apk_identity(apk_path)
        assert_expected_apk_identity(identity, expected_package="dev.core.worker")
        digest = hashlib.sha256()
        with apk_path.open("rb") as fh:
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        actual_sha = digest.hexdigest()
        declared_sha = str(latest.get("sha256") or "").strip().lower()
        if declared_sha and declared_sha != actual_sha:
            return {}
        latest["filename"] = filename
        latest["packageName"] = str(identity["packageName"])
        latest["versionName"] = str(identity["versionName"])
        latest["versionCode"] = int(identity["versionCode"])
        latest["sha256"] = actual_sha
        latest["compiledIdentityVerified"] = True
        return latest
    except Exception:
        return {}


def _public_base_url() -> str:
    """URL pública/privada que novos workers devem usar para parear.

    Preferência:
    1. CORE_WORKER_PUBLIC_BASE_URL/VPS_PUBLIC_BASE_URL explícito;
    2. Tailscale da própria VPS + PORT;
    3. placeholder curto para não mostrar comando errado.
    """
    explicit = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("VPS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit and "IP_TAILSCALE_DA_VPS" not in explicit:
        return explicit
    port = str(os.getenv("CORE_WORKER_PUBLIC_PORT") or os.getenv("PORT") or "10000").strip() or "10000"
    host = str(os.getenv("CORE_WORKER_PUBLIC_HOST") or os.getenv("VPS_TAILSCALE_HOST") or "").strip()
    if not host:
        try:
            proc = subprocess.run(
                ["tailscale", "ip", "-4"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                candidate = line.strip()
                if re.fullmatch(r"100(?:\.\d{1,3}){3}", candidate):
                    host = candidate
                    break
            if not host:
                host = next((line.strip() for line in (proc.stdout or "").splitlines() if line.strip()), "")
        except Exception:
            host = ""
    if host:
        return f"http://{host}:{port}"
    return f"http://IP_TAILSCALE_DA_VPS:{port}"


def _termux_command_block(command: str) -> str:
    command = re.sub(r"\s+", " ", str(command or "")).strip()
    return f"```bash\n{command}\n```"


def _battery_text(worker: dict[str, Any]) -> str:
    battery = worker.get("battery") if isinstance(worker.get("battery"), dict) else {}
    if not battery:
        return "bat n/a"
    level = battery.get("level") or battery.get("percent") or battery.get("percentage")
    charging = battery.get("charging")
    parts: list[str] = []
    try:
        if level is not None:
            parts.append(f"{int(float(level))}%")
    except Exception:
        pass
    if isinstance(charging, bool):
        parts.append("⚡" if charging else "🔋")
    elif charging is not None:
        parts.append(_shorten(charging, limit=12))
    try:
        temp = battery.get("temperature_c") or battery.get("temperature")
        if temp is not None:
            temp_f = float(temp)
            if 0 < temp_f < 90:
                parts.append(f"{temp_f:.0f}°C")
    except Exception:
        pass
    return " ".join(parts) if parts else "bat n/a"



def _ping_text(network: dict[str, Any]) -> str:
    for key in ("vps_ping_ms", "ping_ms", "latency_ms", "vps_latency_ms"):
        value = network.get(key)
        if value is None:
            continue
        try:
            return f"ping {float(value):.0f}ms"
        except Exception:
            continue
    if network.get("vps_reachable") is False or network.get("vps_ping_error"):
        return "ping n/a"
    return ""

def _network_text(worker: dict[str, Any]) -> str:
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    if not network:
        return "rede n/a"
    kind = network.get("type") or network.get("kind") or network.get("transport") or network.get("name")
    tailscale = network.get("tailscale")
    tailscale_state = str(network.get("tailscale_state") or "").strip().lower()
    tailscale_cli = bool(network.get("tailscale_cli"))
    via_vps = bool(network.get("tailscale_via_vps_url"))
    parts: list[str] = []
    if kind and str(kind).lower() not in {"unknown", ""}:
        label = "rede ok" if str(kind).lower() == "connected" else _shorten(kind, limit=16)
        parts.append(label)
    if isinstance(tailscale, bool):
        if tailscale:
            if tailscale_cli:
                label = "ts cli ok"
            elif via_vps or tailscale_state in {"app/vpn", "vpn", "app"}:
                label = "ts app ok"
            else:
                label = "ts ok"
        else:
            label = "ts n/a" if not tailscale_cli else "ts off"
        if tailscale_state and tailscale_state not in {"unknown", "no-cli", "app/vpn"}:
            label += f"/{_shorten(tailscale_state, limit=12)}"
        parts.append(label)
    elif tailscale_state and tailscale_state not in {"unknown", ""}:
        parts.append(f"ts {_shorten(tailscale_state, limit=14)}")
    ping_label = _ping_text(network)
    if ping_label:
        parts.append(ping_label)
    if network.get("tailscale_ip_masked"):
        parts.append(str(network.get("tailscale_ip_masked")))
    return " · ".join(parts) if parts else "rede n/a"




def _simple_network_text(worker: dict[str, Any]) -> str:
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    if not network:
        return "rede não informada"
    parts: list[str] = []
    kind = str(network.get("type") or network.get("kind") or network.get("transport") or "").strip().lower()
    if kind and kind not in {"unknown", "connected"}:
        parts.append(_shorten(kind, limit=14))
    elif kind == "connected":
        parts.append("rede ok")
    tailscale = network.get("tailscale")
    tailscale_state = str(network.get("tailscale_state") or "").strip().lower()
    if tailscale is True or tailscale_state in {"app/vpn", "vpn", "app", "running"}:
        parts.append("rede privada ok")
    elif tailscale is False and network.get("tailscale_cli"):
        parts.append("rede privada off")
    ping_label = _ping_text(network)
    if ping_label:
        parts.append(ping_label)
    return " · ".join(parts[:3]) if parts else "rede ok"

def _worker_ping_numeric(worker: dict[str, Any]) -> float | None:
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    for key in ("vps_ping_ms", "ping_ms", "latency_ms", "vps_latency_ms"):
        value = network.get(key)
        if value is None:
            continue
        try:
            ms = float(value)
            if 0 <= ms < 60000:
                return ms
        except Exception:
            continue
    return None


def _worker_battery_numeric(worker: dict[str, Any]) -> float | None:
    battery = worker.get("battery") if isinstance(worker.get("battery"), dict) else {}
    for key in ("level", "percent", "percentage"):
        value = battery.get(key)
        if value is None:
            continue
        try:
            pct = float(value)
            if 0 <= pct <= 100:
                return pct
        except Exception:
            continue
    return None


def _worker_score_key(worker: dict[str, Any]) -> tuple[Any, ...]:
    ping = _worker_ping_numeric(worker)
    battery = _worker_battery_numeric(worker)
    return (
        0 if worker.get("online") else 1,
        ping if ping is not None else 999999.0,
        -(battery if battery is not None else -1.0),
        str(worker.get("name") or worker.get("worker_id") or "").casefold(),
    )

def _script_health_label(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    scripts = status.get("scripts") if isinstance(status.get("scripts"), dict) else {}
    if not scripts and isinstance(health.get("scripts"), dict):
        scripts = health.get("scripts")
    if not scripts:
        scripts_ok = health.get("scripts_ok")
        if scripts_ok is True:
            return "scripts ok"
        if scripts_ok is False:
            return "scripts incompletos"
        return "scripts n/a"
    complete = scripts.get("complete")
    mirrored = scripts.get("mirrored")
    if complete and mirrored:
        return "scripts ok"
    if complete:
        return "scripts ok parcial"
    return "scripts incompletos"



def _runtime_health_label(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    if not supervisor:
        sup_ok = health.get("supervisor_ok")
        if sup_ok is True:
            return "runtime ok"
        if sup_ok is False:
            return "runtime atenção"
        return "runtime n/a"
    duplicates = supervisor.get("duplicates")
    pid = supervisor.get("pid_file_pid") or supervisor.get("current_pid")
    if supervisor.get("supervisor_ok") is False or (isinstance(duplicates, int) and duplicates > 0):
        return f"runtime atenção{f' · dup {duplicates}' if isinstance(duplicates, int) and duplicates > 0 else ''}"
    if pid:
        return f"runtime ok · pid {pid}"
    return "runtime ok"


def _wake_channel_text(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    sshd = status.get("sshd") if isinstance(status.get("sshd"), dict) else {}
    supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    shell_autostart = status.get("shell_autostart") if isinstance(status.get("shell_autostart"), dict) else {}
    pieces: list[str] = []
    if supervisor:
        if supervisor.get("watchdog_ok") is True:
            pieces.append("watchdog ok")
        elif supervisor.get("watchdog_ok") is False:
            pieces.append("watchdog off")
    if shell_autostart:
        if shell_autostart.get("ok") is True:
            pieces.append("shell auto ok")
        elif shell_autostart.get("ok") is False:
            pieces.append("shell auto off")
    if sshd:
        if sshd.get("ok"):
            port = str(sshd.get("port") or "8022")
            pieces.append(f"SSHD ok:{port}")
        elif sshd.get("installed") is False:
            pieces.append("SSHD ausente")
        elif sshd.get("running") is False:
            pieces.append("SSHD parado")
        elif sshd.get("listening") is False:
            pieces.append("SSHD sem porta")
    return " · ".join(pieces[:4])


def _boot_health_label(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    boot = status.get("boot") if isinstance(status.get("boot"), dict) else {}
    if not boot and isinstance(health.get("boot"), dict):
        boot = health.get("boot")
    if not boot:
        boot_ok = health.get("boot_ok")
        if boot_ok is True:
            return "boot ok"
        if boot_ok is False:
            return "boot faltando"
        return "boot n/a"
    if boot.get("ok"):
        package = boot.get("package") if isinstance(boot.get("package"), dict) else {}
        mode = str(boot.get("mode") or "").strip()
        if package.get("available") is False:
            return "boot script ok · Termux:Boot?"
        return "boot ok" + (f" · {mode}" if mode and mode != "watchdog" else "")
    if boot.get("exists"):
        mode = str(boot.get("mode") or "").strip()
        return "boot incompleto" + (f" · {mode}" if mode else "")
    return "boot faltando"




def _queue_status_text(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    queue = status.get("core_worker_jobs") if isinstance(status.get("core_worker_jobs"), dict) else {}
    if not queue:
        return ""

    state = str(queue.get("last_poll_state") or "").strip().lower()
    if state == "no_compatible_job":
        reason = _shorten(queue.get("last_poll_reason"), limit=60)
        return f"sem job compatível{f' · {reason}' if reason else ''}"
    # Estados normais como "poll agora · sem job" poluem o card principal.
    # Detalhes completos ficam em "Ver último resultado"/logs.
    return ""

def _worker_stale_note(worker: dict[str, Any]) -> str:
    age = worker.get("last_seen_age_seconds")
    try:
        age_f = float(age)
    except Exception:
        age_f = 999999.0
    if bool(worker.get("online")) and age_f <= 120:
        return ""
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    net_runtime = status.get("core_worker_network") if isinstance(status.get("core_worker_network"), dict) else {}
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    issue = str(net_runtime.get("last_error_kind") or "").replace("_", " ")
    if not issue and network.get("vps_ping_error"):
        issue = "rota/ping VPS falhou"
    prefix = f"offline { _format_age(age) }" if age is not None else "offline"
    if issue:
        return f"{prefix} · último erro: {issue}"
    return f"{prefix} · dados abaixo são último estado conhecido"


def _role_text(roles: list[str], *, limit: int = 8) -> str:
    selected = [str(role) for role in roles[:limit] if role]
    if not selected:
        return "`nenhuma`"
    return " ".join(f"`{_shorten(role, limit=24)}`" for role in selected)


def _task_name(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower().replace("-", "_")).strip("_")


def _task_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    else:
        raw_items = []
    result: set[str] = set()
    for item in raw_items:
        clean = _task_name(item)
        if clean:
            result.add(clean)
    return result


def _compact_failure(exc: BaseException) -> str:
    text = _redact(str(exc) or type(exc).__name__)
    match = re.search(r"HTTP\s+(\d+)\s*:\s*(\{.*\})", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        code = match.group(1)
        try:
            data = json.loads(match.group(2))
        except Exception:
            data = {}
        error = str((data or {}).get("error") or "").strip()
        if error:
            lowered = error.lower()
            if "task não suportada" in lowered or "task nao suportada" in lowered:
                return f"worker desatualizado/sem suporte para essa ação (HTTP {code})"
            return f"HTTP {code}: {_shorten(error, limit=90)}"
    lowered = text.lower()
    if "task não suportada" in lowered or "task nao suportada" in lowered:
        return "worker desatualizado/sem suporte para essa ação"
    return _shorten(text, limit=110)




def _core_worker_notification_status_text() -> str:
    root = _repo_root()
    notif_path = root / "data" / "core_worker_app_notifications.json"
    expected_name, expected_code = _expected_apk_version()
    latest = _verified_latest_apk_manifest()
    if not isinstance(latest, dict) or not latest:
        return f"APK: aguardando publicação ({expected_name or '?'})" if expected_name else ""
    version = str(latest.get("versionName") or expected_name or "?")
    try:
        latest_code = int(latest.get("versionCode") or 0)
    except Exception:
        latest_code = 0
    if expected_code and latest_code and latest_code < expected_code:
        return f"APK: build pendente ({expected_name or expected_code}; VPS ainda em {version})"
    if not latest.get("notificationRequested"):
        return "APK: publicado"
    notification_id = str(latest.get("notificationId") or "").strip()
    published_at = latest.get("publishedAt")
    try:
        data = json.loads(notif_path.read_text(encoding="utf-8")) if notif_path.exists() else {}
    except Exception:
        data = {}
    latest_by_id = data.get("latestById") if isinstance(data, dict) and isinstance(data.get("latestById"), dict) else {}
    record = latest_by_id.get(notification_id) if notification_id else None
    if isinstance(record, dict):
        state = str(record.get("state") or "recebida")
        delivered = bool(record.get("delivered")) or state in {"displayed", "background_displayed", "fcm_received", "fcm_displayed", "duplicate", "background_duplicate", "already_displayed", "download_started", "download_verified", "install_intent_opened", "app_opened"}
        app_version = str(record.get("appVersion") or "").strip()
        try:
            app_code = int(record.get("appVersionCode") or 0)
        except Exception:
            app_code = 0
        installed_latest = bool(latest_code and app_code >= latest_code)
        if installed_latest or (state == "app_opened" and app_version == version):
            return f"APK: instalado {app_version or version}"
        if delivered:
            labels = {
                "displayed": "instalação pendente",
                "background_displayed": "instalação pendente",
                "fcm_received": "push recebido · instalação pendente",
                "fcm_displayed": "push exibido · instalação pendente",
                "duplicate": "instalação pendente",
                "background_duplicate": "instalação pendente",
                "download_started": "download iniciado",
                "download_verified": "baixado · instalação pendente",
                "install_intent_opened": "instalador aberto · instalação pendente",
            }
            return f"APK: {labels.get(state, 'instalação pendente')} ({version})"
        if state in {"permission_missing", "background_permission_missing"}:
            return "APK: sem permissão de notificação"
        if state == "fcm_sent":
            return f"APK: push enviado ({version})"
        if state == "fcm_failed":
            return "APK: push falhou"
        if state == "manifest_seen":
            return f"APK: aviso visto ({version})"
        if state == "install_permission_missing":
            return "APK: sem permissão de instalar"
        if state in {"download_failed", "install_intent_failed", "background_failed"}:
            return "APK: falha no update"
        return f"APK: {state}"
    if published_at:
        try:
            age = _format_age(max(0, time.time() - float(published_at)))
            return f"APK: aguardando app ver versão {version} · {age}"
        except Exception:
            pass
    return f"APK: aguardando app ver versão {version}"

def _core_worker_fcm_status_summary(worker_id: str = "") -> dict[str, Any]:
    path = _repo_root() / "data" / "core_worker_app_fcm_tokens.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    tokens = data.get("tokens") if isinstance(data, dict) and isinstance(data.get("tokens"), dict) else {}
    now = time.time()
    runtime = _core_worker_app_runtime_record(worker_id) if worker_id else None
    runtime_code = 0
    try:
        runtime_code = int((runtime or {}).get("appVersionCode") or 0)
    except Exception:
        runtime_code = 0
    runtime_seen = 0.0
    try:
        runtime_seen = float((runtime or {}).get("receivedAt") or 0)
    except Exception:
        runtime_seen = 0.0
    runtime_install_id = str((runtime or {}).get("installId") or "")
    records: list[dict[str, Any]] = []
    invalidated: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for record in tokens.values():
        if not isinstance(record, dict):
            continue
        if worker_id and str(record.get("workerId") or "") != str(worker_id):
            if not runtime_install_id or str(record.get("installId") or "") != runtime_install_id:
                continue
        token = str(record.get("token") or "").strip()
        if len(token) < 20:
            incomplete.append(record)
            continue
        if str(record.get("lastErrorCode") or "").upper() == "UNREGISTERED" or record.get("invalidatedAt"):
            invalidated.append(record)
        if not record.get("active"):
            continue
        try:
            seen = float(record.get("lastSeenAt") or record.get("registeredAt") or 0)
        except Exception:
            seen = 0.0
        if seen and now - seen > 120 * 86400:
            continue
        try:
            record_code = int(record.get("appVersionCode") or 0)
        except Exception:
            record_code = 0
        if runtime_code > 0 and record_code > 0 and record_code < runtime_code:
            stale.append(record)
            continue
        if runtime_code > 0 and record_code <= 0 and runtime_seen and seen and seen < runtime_seen - 300:
            stale.append(record)
            continue
        records.append(record)
    last = None
    for group in (records, invalidated, stale, incomplete):
        for record in group:
            candidate_seen = float(record.get("lastSeenAt") or record.get("invalidatedAt") or record.get("registeredAt") or 0)
            last_seen = float((last or {}).get("lastSeenAt") or (last or {}).get("invalidatedAt") or (last or {}).get("registeredAt") or 0)
            if last is None or candidate_seen > last_seen:
                last = record
    if not records and not invalidated and not stale and not incomplete:
        return {"active": 0}
    return {
        "active": len(records),
        "needsRefresh": bool(not records and invalidated),
        "invalidated": len(invalidated),
        "stale": len(stale),
        "incomplete": len(incomplete),
        "lastSeenAt": (last or {}).get("lastSeenAt"),
        "lastPushAt": (last or {}).get("lastPushAt"),
        "lastPushStatus": str((last or {}).get("lastPushStatus") or ""),
        "lastError": str((last or {}).get("lastError") or ""),
        "lastErrorCode": str((last or {}).get("lastErrorCode") or ""),
        "lastAppVersion": str((last or {}).get("appVersion") or ""),
        "lastAppVersionCode": int((last or {}).get("appVersionCode") or 0),
        "permission": str((last or {}).get("permission") or ""),
    }



def _core_worker_push_status_text(worker_id: str = "") -> str:
    summary = _core_worker_fcm_status_summary(worker_id)
    active = int(summary.get("active") or 0)
    if active <= 0:
        if summary.get("needsRefresh"):
            return "Push: token expirado · aguardando renovação pelo APK"
        if int(summary.get("stale") or 0):
            return "Push: token antigo · aguardando renovação pelo APK"
        if int(summary.get("incomplete") or 0):
            return "Push: token incompleto · aguardando confirmação do APK"
        return "Push: aguardando APK registrar FCM"
    permission = str(summary.get("permission") or "").lower()
    suffix = ""
    last_push = summary.get("lastPushAt")
    if last_push:
        try:
            suffix = f" · último push {_format_age(max(0, time.time() - float(last_push)))}"
        except Exception:
            suffix = ""
    status = str(summary.get("lastPushStatus") or "")
    if status == "unregistered" or str(summary.get("lastErrorCode") or "").upper() == "UNREGISTERED":
        return "Push: token expirado · aguardando renovação"
    if status == "failed":
        detail = str(summary.get("lastError") or "").strip()
        return "Push: falhou no último envio" + (f" · {_shorten(detail, limit=80)}" if detail else " · detalhes no celular")
    if permission == "missing":
        return f"Push: token ativo · sem permissão visível{suffix}"
    if status == "sent":
        return f"Push: ativo · enviado{suffix}"
    return f"Push: ativo ({active}){suffix}"

def _apk_build_effectively_published(apk: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(apk, dict) or not apk:
        return False, ""
    latest = _verified_latest_apk_manifest()
    if not isinstance(latest, dict) or not latest:
        return False, ""
    want_version = str(apk.get("versionName") or apk.get("version_name") or "").strip()
    try:
        want_code = int(apk.get("versionCode") or apk.get("version_code") or 0)
    except Exception:
        want_code = 0
    try:
        got_code = int(latest.get("versionCode") or 0)
    except Exception:
        got_code = 0
    got_version = str(latest.get("versionName") or "").strip()
    if want_code and got_code and got_code < want_code:
        return False, ""
    if want_version and got_version and got_version != want_version and not (want_code and got_code >= want_code):
        return False, ""
    source_obj = apk.get("source") if isinstance(apk.get("source"), dict) else {}
    expected_fp = str(
        apk.get("sourceFingerprint")
        or apk.get("source_fingerprint")
        or apk.get("sourceSha256")
        or apk.get("source_sha256")
        or source_obj.get("sha256")
        or ""
    ).strip().lower()
    if expected_fp:
        values = {
            str(latest.get("sourceFingerprint") or "").strip().lower(),
            str(latest.get("source_fingerprint") or "").strip().lower(),
            str(latest.get("sourceSha256") or "").strip().lower(),
            str(latest.get("source_sha256") or "").strip().lower(),
        }
        short = expected_fp[:12]
        if expected_fp not in values and short and not any(short in value for value in values if value):
            return False, ""
    label = got_version or want_version or str(got_code or want_code or "")
    return True, label


def _automation_failure_hint(item: dict[str, Any]) -> str:
    detail = _shorten(_redact(item.get("last_failure_detail") or item.get("error") or ""), limit=72)
    if not detail:
        return ""
    detail = re.sub(r"^(RuntimeError|ValueError|FileNotFoundError|Exception):\s*", "", detail, flags=re.IGNORECASE)
    return detail


def _dedupe_automation_parts(parts: list[str]) -> list[str]:
    """Mantém um único estado por pipeline e evita o APK aparecer duas vezes."""
    selected: dict[str, tuple[int, int, str]] = {}
    order: list[str] = []
    for index, raw in enumerate(parts):
        value = re.sub(r"\s+", " ", str(raw or "")).strip(" ·")
        if not value:
            continue
        prefix = value.split(":", 1)[0].strip().lower() if ":" in value else value.lower()
        key = prefix if prefix in {"apk", "worker", "push"} else value.lower()
        lowered = value.lower()
        priority = 0
        if any(token in lowered for token in ("falhou", "erro", "correção necessária")):
            priority = 4
        elif any(token in lowered for token in ("em andamento", "na fila", "publicação pendente")):
            priority = 3
        elif "pendente" in lowered:
            priority = 2
        elif any(token in lowered for token in ("publicado", "em dia", "ativo")):
            priority = 1
        previous = selected.get(key)
        if previous is None:
            selected[key] = (priority, index, value)
            order.append(key)
        elif priority > previous[0] or (priority == previous[0] and len(value) > len(previous[2])):
            selected[key] = (priority, previous[1], value)
    return [selected[key][2] for key in sorted(order, key=lambda item: selected[item][1])]


def _automation_phase_label(kind: str, item: dict[str, Any], *, target: object = "") -> str:
    """Traduz o estado persistido sem apagar a causa real do pipeline."""
    phase = str(item.get("phase") or item.get("state") or "").strip().lower()
    version = _shorten(item.get("versionName") or target or "?", limit=24)
    blocked_key = _shorten(item.get("blocked_key") or item.get("blockedKey") or item.get("block_reason") or "", limit=36)
    hint = _automation_failure_hint(item)
    if kind == "agent":
        labels = {
            "target_published": f"Worker: alvo {version} publicado",
            "waiting_device": f"Worker: aguardando celular ({version})",
            "waiting_worker": f"Worker: aguardando celular ({version})",
            "job_queued": f"Worker: atualização na fila ({version})",
            "downloading": f"Worker: baixando {version}",
            "validating": f"Worker: validando {version}",
            "installing": f"Worker: instalando {version}",
            "restart_pending": f"Worker: reinício pendente ({version})",
            "verifying_runtime": f"Worker: verificando runtime {version}",
            "succeeded": f"Worker: {version} em dia",
            "rolled_back": f"Worker: rollback de {version}",
            "port_conflict_control_plane_alive": "Worker: conflito de porta · control plane ativo",
            "failed": f"Worker: atualização falhou ({version})",
        }
        if phase == "blocked_by_config":
            return f"Worker: bloqueado pela configuração{f' · {blocked_key}' if blocked_key else ''}"
        value = labels.get(phase)
        if value and phase in {"rolled_back", "failed"} and hint:
            value += f" · {hint}"
        return value or ""
    labels = {
        "waiting_agent": f"APK: aguardando agent ({version})",
        "waiting_toolchain": f"APK: aguardando toolchain ({version})",
        "toolchain_downloading": f"APK: baixando toolchain ({version})",
        "preflight_blocked": f"APK: preflight bloqueado ({version})",
        "queued": f"APK: na fila ({version})",
        "job_queued": f"APK: na fila ({version})",
        "building": f"APK: build em andamento ({version})",
        "publish_pending": f"APK: publicação pendente ({version})",
        "published": f"APK: publicado {version}",
        "succeeded": f"APK: publicado {version}",
        "failed_transient": f"APK: falha transitória ({version})",
        "failed_deterministic": f"APK: falha determinística ({version})",
        "failed": f"APK: falhou ({version})",
    }
    value = labels.get(phase) or ""
    if value and (phase.startswith("failed") or phase == "preflight_blocked") and hint:
        value += f" · {hint}"
    return value


def _automation_status_text(snapshot_workers: list[dict[str, Any]] | None = None) -> str:
    """Resumo curto e humano do pipeline agent/APK para o painel.

    Importante: esta função roda durante a renderização do painel. Ela não pode
    chamar o registry nem esperar locks, porque isso bloqueia o event loop do
    Discord quando o registry está ocupado com heartbeat/jobs. Use sempre o
    snapshot já coletado fora do layout.
    """
    root = _repo_root()
    pending_path = root / "data" / "core_worker_automation_pending.json"
    status_path = root / "data" / "core_worker_automation_status.json"
    parts: list[str] = []
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
    except Exception:
        pending = {}
    snapshot_workers = [w for w in (snapshot_workers or []) if isinstance(w, dict)]
    if isinstance(pending, dict):
        agent = pending.get("agent_update") if isinstance(pending.get("agent_update"), dict) else {}
        apk = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        if agent:
            target = agent.get('target_version') or _expected_phone_worker_version() or '?'
            phase_text = _automation_phase_label("agent", agent, target=target)
            if phase_text:
                parts.append(phase_text)
            elif _active_workers_need_agent_version(snapshot_workers, target):
                parts.append(f"Worker: atualização pendente ({target})")
        if apk:
            phase_text = _automation_phase_label("apk", apk)
            published, label = _apk_build_effectively_published(apk)
            if phase_text:
                parts.append(phase_text)
            elif published:
                parts.append(f"APK: publicado {label or apk.get('versionName') or '?'}")
            elif apk.get("blocked_by_recent_failure") or (apk.get("ok") is False and not apk.get("pending")):
                retry = apk.get("retry_after_seconds")
                if apk.get("permanent_failure"):
                    extra = " · correção necessária"
                else:
                    extra = f" · retry em {int(retry)}s" if retry else ""
                hint = _automation_failure_hint(apk)
                parts.append(f"APK: {apk.get('versionName') or '?'} falhou{extra}{f' · {hint}' if hint else ''}")
            else:
                parts.append(f"APK: build pendente ({apk.get('versionName') or '?'})")
    if not parts:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        except Exception:
            status = {}
        if isinstance(status, dict):
            root_status = status.get("process_pending") if isinstance(status.get("process_pending"), dict) else status
            agent = root_status.get("agent_update") if isinstance(root_status.get("agent_update"), dict) else {}
            apk = root_status.get("apk_build") if isinstance(root_status.get("apk_build"), dict) else {}
            if agent:
                queued = agent.get("queued") or []
                target = agent.get("target_version") or _expected_phone_worker_version() or "?"
                phase_text = _automation_phase_label("agent", agent, target=target)
                if phase_text:
                    parts.append(phase_text)
                elif queued:
                    parts.append(f"Worker: {len(queued)} atualização(ões) na fila")
                elif agent.get("pending") and _active_workers_need_agent_version(snapshot_workers, target):
                    parts.append(f"Worker: atualização pendente ({target})")
            if apk:
                phase_text = _automation_phase_label("apk", apk)
                published, label = _apk_build_effectively_published(apk)
                if phase_text:
                    parts.append(phase_text)
                elif apk.get("already_published") or published:
                    parts.append(f"APK: publicado {label or apk.get('versionName') or '?'}")
                elif apk.get("blocked_by_recent_failure") or (apk.get("ok") is False and not apk.get("pending")):
                    suffix = " · correção necessária" if apk.get("permanent_failure") else ""
                    hint = _automation_failure_hint(apk)
                    parts.append(f"APK: {apk.get('versionName') or '?'} falhou{suffix}{f' · {hint}' if hint else ''}")
                elif apk.get("job"):
                    parts.append(f"APK: build em andamento ({apk.get('versionName') or '?'})")
                else:
                    parts.append(f"APK: build pendente ({apk.get('versionName') or '?'})")
    try:
        notif = _core_worker_notification_status_text()
    except Exception:
        notif = ""
    if notif:
        parts.append(notif)
    try:
        push = _core_worker_push_status_text()
    except Exception:
        # O painel não pode cair inteiro por causa de um resumo auxiliar.
        push = ""
    if push and "aguardando APK" not in push:
        parts.append(push)
    parts = _dedupe_automation_parts(parts)
    return " · ".join(parts[:3]) if parts else "tudo em dia"

def _worker_wake_tokens(worker: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("roles", "capabilities", "supported_tasks", "supportedTasks", "appJobs"):
        raw = worker.get(key) if isinstance(worker, dict) else None
        if isinstance(raw, (list, tuple, set)):
            items = raw
        else:
            items = re.split(r"[,;\s]+", str(raw or ""))
        for item in items:
            clean = str(item or "").strip().lower().replace("_", "-")
            clean = re.sub(r"[^a-z0-9_.:-]+", "-", clean).strip("-._:")
            if clean:
                tokens.add(clean)
                tokens.add(clean.replace("-", "_"))
    return tokens


def _worker_has_important_responsibility(worker: dict[str, Any]) -> bool:
    tokens = _worker_wake_tokens(worker)
    if tokens.intersection(CORE_WORKER_IMPORTANT_WAKE_ROLES):
        return True
    return bool(tokens.intersection(CORE_WORKER_IMPORTANT_WAKE_TASKS))


def _offline_important_worker_labels(snapshot: "WorkerSnapshot") -> list[str]:
    registry = snapshot.registry_snapshot if isinstance(snapshot.registry_snapshot, dict) else {}
    workers = registry.get("workers") if isinstance(registry.get("workers"), list) else []
    labels: list[str] = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        if worker.get("online") or not bool(worker.get("enabled", True)):
            continue
        if not _worker_has_important_responsibility(worker):
            continue
        name = worker.get("name") or worker.get("worker_id") or "worker"
        labels.append(_shorten(name, limit=36))
    return labels


def _snapshot_has_online_worker(snapshot: "WorkerSnapshot") -> bool:
    summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
    try:
        if int((summary or {}).get("online") or 0) > 0:
            return True
    except Exception:
        pass
    return bool(snapshot.online)


def _watch_output_note(output: object) -> str:
    text = _redact(str(output or "")).strip()
    lowered = text.lower()
    if not text:
        return "watchdog sem saída"
    if "host ou token não configurados" in lowered:
        return "host/token do phone-worker ausente no .env"
    if "phone_worker_enabled=false" in lowered:
        return "PHONE_WORKER_ENABLED=false"
    if "cooldown ativo" in lowered:
        return "cooldown ativo; tentativa não enviada"
    if "phone_worker_ssh_user vazio" in lowered:
        return "SSH do celular não configurado; aguardando watchdog local"
    if "ssh não encontrado" in lowered or "ssh nao encontrado" in lowered:
        return "SSH não encontrado na VPS"
    if "probe ssh:" in lowered and "connection-refused" in lowered:
        return "porta SSH fechada/sshd parado no celular"
    if "probe ssh:" in lowered and "timeout" in lowered:
        return "SSH/Tailscale não respondeu dentro do timeout"
    if "probe ssh:" in lowered and "no-route" in lowered:
        return "sem rota da VPS até o celular"
    if "health: http 401" in lowered or "health: http 403" in lowered:
        return "phone-worker respondeu, mas token/config da VPS não bate"
    if "probe worker-http:" in lowered and "connection-refused" in lowered:
        return "porta do phone-worker fechada no celular"
    if "probe worker-http:" in lowered and "timeout" in lowered:
        return "porta do phone-worker não respondeu"
    if "não consegui acionar" in lowered or "nao consegui acionar" in lowered:
        if "auth-failed" in lowered:
            return "SSH respondeu, mas autenticação/chave falhou"
        if "connection-refused" in lowered:
            return "SSH recusado; sshd provavelmente parado"
        if "no-route" in lowered:
            return "sem rota da VPS até o celular"
        if "timeout" in lowered:
            return "SSH/Tailscale sem resposta"
        return "SSH falhou ou celular não respondeu"
    if "celular respondeu ao ssh" in lowered:
        return "SSH respondeu, mas o agent ainda não confirmou heartbeat/token/porta"
    if "worker voltou" in lowered:
        return "script informou worker voltou"
    if "worker online" in lowered:
        return "script informou worker online"
    return _shorten(text, limit=120)


def _wake_attempt_note(*, before: "WorkerSnapshot" | None, after: "WorkerSnapshot", attempt: dict[str, Any], reason: str = "manual") -> str:
    elapsed = attempt.get("elapsed_seconds")
    elapsed_text = f" em {float(elapsed):.1f}s" if isinstance(elapsed, (int, float)) else ""
    reason_label = "auto-wake" if reason == "auto" else "wake manual"
    if before is not None and _snapshot_has_online_worker(before):
        return f"✅ {reason_label}: worker já estava online"
    if _snapshot_has_online_worker(after):
        return f"✅ {reason_label}: worker voltou{elapsed_text}"
    if attempt.get("timeout"):
        return f"⚠️ {reason_label}: tentativa excedeu timeout; worker ainda offline"
    detail = _watch_output_note(attempt.get("output") or attempt.get("error") or "")
    return f"⚠️ {reason_label}: worker ainda offline · {detail}"


def _job_result_note(job_type: object, job: dict[str, Any] | None) -> str:
    kind = _task_name(job_type)
    if not isinstance(job, dict) or not job:
        return f"`{kind}` enviado; aguardando resultado"
    status = str(job.get("status") or "queued").strip().lower()
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if status == "succeeded" and isinstance(result, dict) and result.get("ok") is False:
        status = "failed"
    raw_summary = result.get("summary") or job.get("summary") or job.get("error") or status
    summary = _shorten(raw_summary, limit=72)
    if summary.strip().lower() in {"succeeded", "success", "ok", "true"}:
        summary = ""
    suffix = f": {summary}" if summary else ""
    if status == "succeeded":
        return f"✅ `{kind}` concluído{suffix}"
    if status == "failed":
        return f"❌ `{kind}` falhou{suffix}"
    if status in {"superseded", "resolved", "resolved_by_manual_sync"}:
        return f"✅ `{kind}` superado{suffix}"
    if status == "running":
        return f"⏳ `{kind}` em execução"
    if status == "queued":
        return f"⏳ `{kind}` aguardando worker"
    return f"`{kind}`{suffix or f': {status}'}"


def _status_badge(status: object) -> tuple[str, str]:
    raw = str(status or "desconhecido").strip().lower()
    if raw == "succeeded":
        return "✅", "concluído"
    if raw == "failed":
        return "❌", "falhou"
    if raw == "running":
        return "⏳", "em execução"
    if raw == "queued":
        return "🕒", "aguardando"
    if raw == "expired":
        return "⌛", "expirado"
    if raw in {"superseded", "resolved", "resolved_by_manual_sync"}:
        return "✅", "superado"
    return "📄", raw or "desconhecido"


def _check_text(value: object, *, ok_label: str = "ok", fail_label: str = "atenção") -> str:
    if value is True:
        return f"✅ {ok_label}"
    if value is False:
        return f"⚠️ {fail_label}"
    return "—"


def _job_elapsed_seconds(job: dict[str, Any], result: dict[str, Any]) -> float | None:
    for key in ("duration_seconds", "elapsed_seconds"):
        if result.get(key) is not None:
            with contextlib.suppress(Exception):
                return max(0.0, float(result.get(key)))
        if job.get(key) is not None:
            with contextlib.suppress(Exception):
                return max(0.0, float(job.get(key)))
    started = job.get("started_at") or result.get("started_at")
    finished = job.get("finished_at") or result.get("finished_at") or job.get("updated_at")
    if started and finished:
        with contextlib.suppress(Exception):
            return max(0.0, float(finished) - float(started))
    return None


def _apk_result_url(publish: dict[str, Any], apk: dict[str, Any]) -> str:
    latest = publish.get("latest") if isinstance(publish.get("latest"), dict) else {}
    for value in (publish.get("url"), publish.get("apk_url"), latest.get("apkUrl"), latest.get("apk_url")):
        text = str(value or "").strip()
        if text:
            return text
    rel = str(apk.get("relative_url") or apk.get("path") or "").strip()
    if rel.startswith("/core-worker/"):
        base = _public_base_url()
        if base:
            return base.rstrip("/") + rel
        return rel
    return ""


def _append_job_technical_lines(lines: list[str], *, job: dict[str, Any], result: dict[str, Any]) -> None:
    lines.append("")
    lines.append("### Detalhes técnicos")
    compact_job = {
        key: job.get(key)
        for key in ("job_id", "type", "status", "created_at", "updated_at", "started_at", "finished_at", "worker_id", "target_worker_id", "attempts", "lease_until")
        if job.get(key) not in (None, "", [], {})
    }
    if compact_job:
        lines.append("`job`: " + _shorten(_redact(json.dumps(compact_job, ensure_ascii=False, separators=(",", ":"))), limit=520))
    interesting_keys = [
        "version", "target_version", "current_version", "apk", "publish", "builder_environment",
        "artifact_meta", "artifact_cleanup", "gradle_log_path", "gradle_error_summary", "publish_error",
        "source", "scripts", "battery", "network", "ping", "tailscale", "services",
        "ffmpeg", "ffprobe", "lines", "error_lines", "path", "work_dir",
    ]
    shown = 0
    for key in interesting_keys:
        if key not in result:
            continue
        value = result.get(key)
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"`{key}`: {_shorten(_redact(text), limit=520)}")
        shown += 1
        if shown >= 6:
            break
    for tail_key, label in (("stderr_tail", "stderr"), ("stdout_tail", "stdout"), ("tail", "log")):
        tail = result.get(tail_key)
        if isinstance(tail, str) and tail.strip():
            tail = _redact(tail.strip())
            nl = chr(10)
            lines.append(f"```txt{nl}{label}:{nl}" + tail[-900:] + f"{nl}```")
            break


def _job_detail_text(job: dict[str, Any] | None, *, include_technical: bool = False) -> str:
    if not isinstance(job, dict) or not job:
        return "Nenhum resultado recente encontrado para este worker."
    raw_status = str(job.get("status") or "desconhecido").strip().lower()
    kind = _task_name(job.get("type") or "job")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if raw_status == "succeeded" and isinstance(result, dict) and result.get("ok") is False:
        raw_status = "failed"
    icon, status_label = _status_badge(raw_status)
    lines = [
        f"## {icon} Resultado do worker",
        f"**Ação:** `{_shorten(kind, limit=48)}`",
        f"**Status:** `{_shorten(status_label, limit=32)}`",
    ]
    worker_id = job.get("worker_id") or job.get("target_worker_id")
    if worker_id:
        lines.append(f"**Worker:** `{_shorten(worker_id, limit=72)}`")
    duration = _job_elapsed_seconds(job, result)
    if duration is not None:
        lines.append(f"**Duração:** `{duration:.1f}s`")
    summary = result.get("summary") or job.get("summary") or job.get("error")
    if summary:
        lines.append(f"**Resumo:** {_shorten(_redact(summary), limit=300)}")
    if job.get("error") and job.get("error") != summary:
        lines.append(f"**Erro:** `{_shorten(_redact(job.get('error')), limit=260)}`")

    if kind == "apk_build_debug" and result:
        apk = result.get("apk") if isinstance(result.get("apk"), dict) else {}
        publish = result.get("publish") if isinstance(result.get("publish"), dict) else {}
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        latest = publish.get("latest") if isinstance(publish.get("latest"), dict) else {}
        version = apk.get("versionName") or apk.get("version_name") or result.get("versionName") or latest.get("versionName")
        version_code = apk.get("versionCode") or apk.get("version_code") or result.get("versionCode") or latest.get("versionCode")
        filename = apk.get("filename") or apk.get("name") or latest.get("apk")
        size = apk.get("bytes") or apk.get("size_bytes") or latest.get("bytes")
        url = _apk_result_url(publish, apk)
        lines.append("")
        lines.append("### APK")
        if version or version_code:
            label = f"v{version}" if version else "versão desconhecida"
            if version_code:
                label += f" · code {version_code}"
            lines.append(f"**Versão:** `{_shorten(label, limit=90)}`")
        if filename:
            lines.append(f"**Arquivo:** `{_shorten(filename, limit=120)}`")
        if size is not None:
            lines.append(f"**Tamanho:** `{_format_bytes(size)}`")
        if url:
            lines.append(f"**URL:** `{_shorten(url, limit=180)}`")
        checks = [
            ("Build", result.get("ok") if result.get("ok") is not None else raw_status == "succeeded"),
            ("Publicação", publish.get("ok") if publish else None),
            ("Assinatura", apk.get("signed") if apk else None),
            ("Validação", validation.get("ok") if validation else result.get("validated")),
            ("Source ZIP", result.get("source_zip_ok") if result.get("source_zip_ok") is not None else result.get("source_ok")),
        ]
        useful_checks = [f"{name}: {_check_text(value)}" for name, value in checks if value is not None]
        if useful_checks:
            lines.append("**Validações:** " + " · ".join(useful_checks))
        if publish and publish.get("ok") is False:
            detail = publish.get("detail") or publish.get("error") or publish.get("hint")
            if detail:
                lines.append(f"**Publicação:** `{_shorten(_redact(detail), limit=260)}`")
    elif kind == "maintenance_plan" and result:
        if result.get("scanned") is not None:
            lines.append(f"**Arquivos analisados:** `{result.get('scanned')}`")
        if result.get("estimated_reclaimable") is not None:
            lines.append(f"**Recuperável estimado:** `{_format_bytes(result.get('estimated_reclaimable'))}`")
        recommendations = result.get("recommendations") if isinstance(result.get("recommendations"), list) else []
        if recommendations:
            lines.append("")
            lines.append("### Sugestões seguras")
            for item in recommendations[:6]:
                lines.append(f"- {_shorten(_redact(item), limit=180)}")
        largest = result.get("largest") if isinstance(result.get("largest"), list) else []
        if largest:
            lines.append("")
            lines.append("### Maiores arquivos vistos")
            for item in largest[:4]:
                if isinstance(item, dict):
                    lines.append(f"- `{_shorten(item.get('path'), limit=80)}` · {_format_bytes(item.get('size'))}")
    elif kind == "boot_status" and result:
        checks = [
            ("Script", result.get("exists")),
            ("Executável", result.get("executable")),
            ("Conteúdo", result.get("content_ok")),
            ("Termux:Boot", result.get("package_available")),
        ]
        lines.append("")
        lines.append("### Boot")
        lines.append(" · ".join(f"{name}: {_check_text(ok)}" for name, ok in checks))
        if result.get("path"):
            lines.append(f"**Caminho:** `{_shorten(result.get('path'), limit=120)}`")
        if result.get("warning"):
            lines.append(f"**Aviso:** {_shorten(result.get('warning'), limit=220)}")
    elif kind == "endpoint_probe" and result:
        probes = result.get("results") if isinstance(result.get("results"), list) else []
        if probes:
            lines.append("")
            lines.append("### Endpoints")
            for item in probes[:6]:
                if not isinstance(item, dict):
                    continue
                ok = "✅" if item.get("ok") else "❌"
                status_code = item.get("status") or item.get("error") or "sem status"
                latency = item.get("latency_ms")
                suffix = f" · {latency}ms" if latency is not None else ""
                lines.append(f"- {ok} `{_shorten(item.get('url'), limit=92)}` · {status_code}{suffix}")
    elif kind in {"log_summary", "log_digest"} and result:
        counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
        if counts:
            top_counts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:8] if v)
            if top_counts:
                lines.append(f"**Contagens:** `{_shorten(top_counts, limit=240)}`")
        top_messages = result.get("top_messages") if isinstance(result.get("top_messages"), list) else []
        if top_messages:
            lines.append("")
            lines.append("### Mais repetidos")
            for item in top_messages[:5]:
                if isinstance(item, dict):
                    lines.append(f"- `{item.get('count')}`× {_shorten(_redact(item.get('message')), limit=160)}")
        recent = result.get("recent") if isinstance(result.get("recent"), list) else []
        if recent:
            lines.append("")
            lines.append("### Recentes importantes")
            for item in recent[-5:]:
                lines.append(f"- {_shorten(_redact(item), limit=170)}")
    elif kind in {"zip_validate", "zip_audit"} and result:
        lines.append(f"**ZIP:** `{_shorten(result.get('filename') or 'arquivo', limit=80)}` · `{result.get('files', 0)}` arquivo(s) · risco `{result.get('risk', 'n/a')}`")
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        if errors:
            lines.append("**Erros:** " + "; ".join(_shorten(_redact(e), limit=120) for e in errors[:5]))
        if warnings:
            lines.append("**Avisos:** " + "; ".join(_shorten(_redact(w), limit=120) for w in warnings[:5]))

    if include_technical:
        _append_job_technical_lines(lines, job=job, result=result)
    elif result:
        lines.append("")
        lines.append("-# Detalhes técnicos ocultos. Use o botão abaixo se precisar depurar.")
    return chr(10).join(lines)[:3800 if include_technical else 1900]


def _worker_detail_text(worker: dict[str, Any] | None) -> str:
    if not isinstance(worker, dict) or not worker:
        return "Selecione um celular registrado para ver os detalhes."
    name = _shorten(worker.get("name") or worker.get("worker_id") or "Core Worker", limit=64)
    worker_id = _shorten(worker.get("worker_id") or "", limit=72)
    online = "online" if worker.get("online") else "offline"
    seen = _format_age(worker.get("last_seen_age_seconds"))
    version = _agent_version_label(worker.get("version"))
    roles = [str(role) for role in (worker.get("roles") or []) if role]
    caps = [str(role) for role in (worker.get("capabilities") or []) if role]
    supported = [str(task) for task in (worker.get("supported_tasks") or worker.get("supportedTasks") or worker.get("appJobs") or []) if task]
    lines = [
        f"## 📱 {name}",
        f"**Estado:** {online} · visto {seen}",
        f"**Versão:** `{version}`",
        f"**Perfil:** {_worker_profile_label(worker)}",
        f"**Modo:** {_worker_runtime_label(worker)}",
        f"**Push:** {_core_worker_push_status_text(worker_id)}",
        f"**Bateria:** {_battery_text(worker)}",
        f"**Rede:** {_network_text(worker)}",
        f"**Scripts:** {_script_health_label(worker)}",
        f"**Boot automático:** {_boot_health_label(worker)}",
        f"**Canal wake:** {_wake_channel_text(worker) or 'não reportado'}",
        f"**Runtime:** {_runtime_health_label(worker)}",
        f"**APK interno:** {_core_worker_app_runtime_detail_text(worker_id)}",
        "",
        "### Funções",
        _role_text(roles, limit=16),
    ]
    if caps and caps != roles:
        lines.extend(["", "### Capacidades", _role_text(caps, limit=16)])
    if supported:
        compact_tasks = ", ".join(f"`{_shorten(task, limit=28)}`" for task in supported[:18])
        if len(supported) > 18:
            compact_tasks += f" +{len(supported) - 18}"
        lines.extend(["", "### Ações suportadas", compact_tasks])
    lines.extend(["", "### Técnico", f"ID: `{worker_id}`"])
    endpoint = worker.get("endpoint")
    if endpoint:
        lines.append(f"Endpoint: `{_shorten(endpoint, limit=120)}`")
    return "\n".join(lines)[:1900]


def _profile_help_text() -> str:
    lines = ["Perfis disponíveis:"]
    for key, roles in WORKER_ROLE_PROFILES.items():
        label = WORKER_ROLE_PROFILE_DESCRIPTIONS.get(key, "")
        lines.append(f"`{key}` — {label}: " + ", ".join(_role_label(role) for role in roles[:8]))
    return "\n".join(lines)


def _has_online_registry_worker(snapshot: "WorkerSnapshot") -> bool:
    summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
    try:
        return int((summary or {}).get("online") or 0) > 0
    except Exception:
        return False


@dataclass(slots=True)
class WorkerSnapshot:
    enabled: bool
    configured: bool
    online: bool
    host: str = ""
    port: str = "8766"
    scheme: str = "http"
    name: str = "Core Phone Worker"
    roles: list[str] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    checked_at: float = field(default_factory=time.time)
    action_note: str = ""
    watch_output: str = ""
    registry_snapshot: dict[str, Any] = field(default_factory=dict)
    registry_error: str = ""

    @property
    def base_url_label(self) -> str:
        if not self.configured:
            return "não configurado"
        return f"{self.scheme}://{_host_label(self.host)}:{self.port}"

    @property
    def state_label(self) -> str:
        registered_online = int(((self.registry_snapshot or {}).get("summary") or {}).get("online") or 0)
        if registered_online > 0:
            noun = "celular" if registered_online == 1 else "celulares"
            return f"🟢 **Operacional** · {registered_online} {noun} online"
        if self.online:
            return "🟢 **Operacional** · runtime direto online"
        if not self.enabled:
            return "⚫ **Desativado**"
        if not self.configured:
            return "🟠 **Configuração incompleta**"
        return "🔴 **Offline** · nenhum celular disponível"

    @property
    def accent(self) -> discord.Color:
        summary = (self.registry_snapshot or {}).get("summary") if isinstance(self.registry_snapshot, dict) else {}
        if int((summary or {}).get("online") or 0) > 0 or self.online:
            return discord.Color.green()
        if self.configured and self.enabled:
            return discord.Color.orange()
        return discord.Color.dark_grey()



class WorkerRolesEditorView(discord.ui.LayoutView):
    def __init__(self, parent: "WorkersPanelView", *, worker: dict[str, Any]):
        super().__init__(timeout=180.0)
        self.parent = parent
        self.cog = parent.cog
        self.owner_id = parent.owner_id
        self.worker_id = str(worker.get("worker_id") or "")
        self.worker_name = _shorten(worker.get("name") or self.worker_id or "Core Worker", limit=48)
        self.profile = "manter"
        self.selected_features: set[str] = _feature_values_from_worker(worker)
        if not self.selected_features:
            self.selected_features = _profile_feature_values("midia")
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("Só quem abriu o painel pode editar este worker.", ephemeral=True)
        else:
            await interaction.response.send_message("Só quem abriu o painel pode editar este worker.", ephemeral=True)
        return False

    def _clear_items(self) -> None:
        for item in list(self.children):
            self.remove_item(item)

    def _new_select(self, **kwargs: Any):
        select_cls = getattr(discord.ui, "StringSelect", None) or getattr(discord.ui, "Select")
        return select_cls(**kwargs)

    def _profile_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(label="Manter seleção atual", value="manter", description="Não troca o perfil base sozinho", emoji="📌", default=self.profile == "manter"),
            discord.SelectOption(label="Leve", value="leve", description="Reserva, diagnóstico e logs", emoji="🍃", default=self.profile == "leve"),
            discord.SelectOption(label="Normal", value="midia", description="Perfil recomendado", emoji="🎧", default=self.profile == "midia"),
            discord.SelectOption(label="Completo", value="completo", description="Mídia + manutenção", emoji="🧰", default=self.profile == "completo"),
            discord.SelectOption(label="Turbo", value="turbo", description="Ajuda máxima para acelerar a VPS", emoji="⚡", default=self.profile == "turbo"),
            discord.SelectOption(label="Bedrock", value="bedrock", description="Minecraft Bedrock futuro", emoji="🧱", default=self.profile == "bedrock"),
        ]

    def _feature_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        selected = set(self.selected_features)
        for feature in WORKER_EDITABLE_FEATURES:
            value = str(feature.get("value") or "")
            if not value:
                continue
            options.append(discord.SelectOption(
                label=str(feature.get("label") or value)[:100],
                value=value[:100],
                description=_shorten(feature.get("description") or "", limit=100),
                emoji=str(feature.get("emoji") or "🧩"),
                default=value in selected,
            ))
        return options[:25]

    def _preview_text(self) -> str:
        roles, caps, tasks = _roles_caps_tasks_for_features(self.selected_features, profile=(self.profile if self.profile != "manter" else ""))
        role_preview = ", ".join(_role_label(role) for role in roles[:10]) or "Base do worker"
        task_preview = ", ".join(f"`{task}`" for task in tasks[:12]) or "`ping`/`status`"
        if len(tasks) > 12:
            task_preview += f" +{len(tasks) - 12}"
        return (
            f"## 🧩 Editar funções do celular\n"
            f"**Worker:** {_shorten(self.worker_name, limit=64)}\n"
            "Escolha por opções. Nada de digitar lista de funções na mão.\n\n"
            f"**Perfil base:** `{self.profile}`\n"
            f"**Funções finais:** {role_preview}\n"
            f"**Jobs que serão declarados:** {task_preview}\n"
            "-# Isso salva um override manual no registry. O agent ainda deve estar atualizado para executar os jobs."
        )

    def _rebuild(self, *, done: str = "") -> None:
        self._clear_items()
        if done:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay(done[:1900]), accent_color=discord.Color.green()))
            return
        profile_select = self._new_select(
            placeholder="Perfil base",
            min_values=1,
            max_values=1,
            options=self._profile_options(),
        )
        profile_select.callback = self._select_profile
        feature_options = self._feature_options()
        features_select = self._new_select(
            placeholder="Funções deste celular",
            min_values=1,
            max_values=min(25, len(feature_options)),
            options=feature_options,
        )
        features_select.callback = self._select_features
        apply_button = discord.ui.Button(label="Aplicar funções", emoji="✅", style=discord.ButtonStyle.primary)
        apply_button.callback = self._apply
        cancel_button = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(self._preview_text()),
            discord.ui.Separator(),
            discord.ui.ActionRow(profile_select),
            discord.ui.ActionRow(features_select),
            discord.ui.ActionRow(apply_button, cancel_button),
            accent_color=discord.Color.blurple(),
        ))

    async def _select_profile(self, interaction: discord.Interaction) -> None:
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        profile = _normalize_worker_profile(str((values or ["manter"])[0] or "manter"), default="midia")
        if str((values or [""])[0]) == "manter":
            profile = "manter"
        self.profile = profile
        if profile != "manter":
            self.selected_features = _profile_feature_values(profile)
        self._rebuild()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _select_features(self, interaction: discord.Interaction) -> None:
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        self.selected_features = {str(value) for value in values if str(value) in _FEATURE_BY_VALUE}
        self.selected_features.add("apk-worker")
        self._rebuild()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _apply(self, interaction: discord.Interaction) -> None:
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.roles_editor")
        roles, caps, tasks = _roles_caps_tasks_for_features(self.selected_features, profile=(self.profile if self.profile != "manter" else ""))
        try:
            await self.cog._update_core_worker_roles(self.worker_id, ", ".join(roles), ", ".join(caps), ", ".join(tasks))
            self.parent.snapshot = await self.cog._collect_workers_snapshot(action_note=f"funções atualizadas por seleção · {_shorten(self.worker_name, limit=32)}")
            self.parent._ensure_selected_worker()
            self.parent._rebuild_layout()
            if self.parent.message is not None:
                await self.parent.message.edit(view=self.parent, allowed_mentions=discord.AllowedMentions.none())
            text = "✅ Funções atualizadas.\n\n" + _role_text(roles, limit=16) + "\n\nJobs: " + ", ".join(f"`{task}`" for task in tasks[:20])
            self._rebuild(done=text[:1900])
            await safe_edit_original_or_message(
                interaction,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
                log=logger,
                label="workers.roles_editor",
            )
        except Exception as exc:
            text = f"Não consegui atualizar as funções: {_compact_failure(exc)}"
            self._rebuild(done=text)
            await safe_edit_original_or_message(
                interaction,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
                log=logger,
                label="workers.roles_editor",
            )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self._rebuild(done="Edição fechada. Nada foi alterado.")
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())


class LastJobResultView(discord.ui.LayoutView):
    def __init__(self, *, owner_id: int, job: dict[str, Any] | None):
        super().__init__(timeout=120.0)
        self.owner_id = int(owner_id or 0)
        self.job = job if isinstance(job, dict) else None
        self.show_technical = False
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("Só quem abriu o painel pode ver esses detalhes.", ephemeral=True)
        else:
            await interaction.response.send_message("Só quem abriu o painel pode ver esses detalhes.", ephemeral=True)
        return False

    def _clear_items(self) -> None:
        for item in list(self.children):
            self.remove_item(item)

    def _rebuild(self) -> None:
        self._clear_items()
        if not self.job:
            container = discord.ui.Container()
            container.add_item(discord.ui.TextDisplay("Nenhum resultado recente encontrado para este worker."))
            self.add_item(container)
            return
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(_job_detail_text(self.job, include_technical=self.show_technical)))
        row = discord.ui.ActionRow()
        label = "Ocultar detalhes técnicos" if self.show_technical else "Mostrar detalhes técnicos"
        button = discord.ui.Button(label=label, emoji="🧾", style=discord.ButtonStyle.secondary)
        button.callback = self._toggle_technical
        row.add_item(button)
        container.add_item(row)
        self.add_item(container)

    async def _toggle_technical(self, interaction: discord.Interaction) -> None:
        self.show_technical = not self.show_technical
        self._rebuild()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

class WorkersPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "WorkersCommandMixin",
        *,
        owner_id: int,
        snapshot: WorkerSnapshot,
        selected_worker_id: str = "",
        selected_action_category: str = "quick",
    ):
        super().__init__(timeout=WORKERS_PANEL_TIMEOUT_SECONDS)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.snapshot = snapshot
        self.selected_worker_id = str(selected_worker_id or "")
        # O painel antigo tinha categorias demais. Mantemos o atributo só para
        # compatibilidade com mensagens já abertas, mas o select visível agora
        # mostra apenas as ações realmente úteis.
        self.selected_action_category = "main"
        self.message: discord.Message | None = None
        self._ensure_selected_worker()
        self._rebuild_layout()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        message = "Só quem abriu o painel de workers pode usar esses controles."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        self._rebuild_layout(expired=True)
        with contextlib.suppress(Exception):
            if self.message is not None:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    def _clear_items(self) -> None:
        for item in list(self.children):
            self.remove_item(item)

    def _registry_workers(self) -> list[dict[str, Any]]:
        registry = self.snapshot.registry_snapshot or {}
        workers = registry.get("workers") if isinstance(registry.get("workers"), list) else []
        return [worker for worker in workers if isinstance(worker, dict)]

    @staticmethod
    def _physical_worker_id(worker: dict[str, Any]) -> str:
        return str(
            worker.get("physical_worker_id")
            or worker.get("parent_worker_id")
            or worker.get("worker_id")
            or ""
        ).strip()

    def _physical_groups(self) -> list[list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for worker in self._registry_workers():
            physical_id = self._physical_worker_id(worker)
            if not physical_id:
                continue
            groups.setdefault(physical_id, []).append(worker)
        return list(groups.values())

    @staticmethod
    def _preferred_runtime(members: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not members:
            return None

        def score(worker: dict[str, Any]) -> tuple[Any, ...]:
            runtime_kind = str(worker.get("runtime_kind") or "").lower()
            source = str(worker.get("source") or "").lower()
            is_apk = runtime_kind == "apk" or source.startswith("core-worker-apk")
            status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
            builder = status.get("apk_self_builder") if isinstance(status.get("apk_self_builder"), dict) else {}
            caps = {str(item) for item in worker.get("capabilities") or []}
            builder_ready = bool(builder.get("ready")) or "apk-builder" in caps
            return (
                1 if worker.get("online") else 0,
                1 if worker.get("enabled") is not False else 0,
                1 if (is_apk and builder_ready) else 0,
                1 if is_apk else 0,
                float(worker.get("last_seen") or 0),
            )

        return max(members, key=score)

    def _visible_registry_workers(self) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        for members in self._physical_groups():
            preferred = self._preferred_runtime(members)
            if preferred is not None:
                visible.append(preferred)
        visible.sort(key=_worker_score_key)
        return visible

    def _physical_group_for_worker_id(self, worker_id: str) -> list[dict[str, Any]]:
        wanted = str(worker_id or "").strip()
        if not wanted:
            return []
        for members in self._physical_groups():
            if any(str(item.get("worker_id") or "") == wanted for item in members):
                return members
        return []

    @staticmethod
    def _group_parent(members: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not members:
            return None
        physical_id = str(members[0].get("physical_worker_id") or members[0].get("parent_worker_id") or members[0].get("worker_id") or "").strip()
        return next((item for item in members if str(item.get("worker_id") or "") == physical_id), None)

    @staticmethod
    def _group_apk(members: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in members:
            runtime_kind = str(item.get("runtime_kind") or "").lower()
            source = str(item.get("source") or "").lower()
            if runtime_kind == "apk" or source.startswith("core-worker-apk"):
                return item
        return None

    def _group_display_name(self, members: list[dict[str, Any]]) -> str:
        parent = self._group_parent(members)
        preferred = self._preferred_runtime(members) or {}
        physical_id = self._physical_worker_id(preferred)
        return str(
            (parent or {}).get("name")
            or preferred.get("name")
            or physical_id
            or "Core Worker"
        ).strip()

    def _online_registry_workers(self) -> list[dict[str, Any]]:
        workers = [worker for worker in self._visible_registry_workers() if worker.get("online")]
        workers.sort(key=_worker_score_key)
        return workers

    def _has_legacy_worker(self) -> bool:
        if CORE_WORKER_APK_REPLACES_TERMUX:
            return False
        return bool(self.snapshot.configured and self.snapshot.online and not _has_online_registry_worker(self.snapshot))

    def _worker_choices_exist(self) -> bool:
        return bool(self._visible_registry_workers() or self._has_legacy_worker())

    def _ensure_selected_worker(self) -> None:
        workers = self._visible_registry_workers()
        online_workers = self._online_registry_workers()
        worker_ids = [str(worker.get("worker_id") or "") for worker in workers if worker.get("worker_id")]
        if self.selected_worker_id == AUTO_WORKER_ID and len(online_workers) >= 2:
            return
        if self.selected_worker_id:
            if self.selected_worker_id in worker_ids or (self.selected_worker_id == LEGACY_WORKER_ID and self._has_legacy_worker()):
                return
            members = self._physical_group_for_worker_id(self.selected_worker_id)
            preferred = self._preferred_runtime(members)
            if preferred and preferred.get("worker_id"):
                self.selected_worker_id = str(preferred.get("worker_id"))
                return
        if len(online_workers) >= 2:
            self.selected_worker_id = AUTO_WORKER_ID
            return
        online = online_workers[0] if online_workers else None
        if isinstance(online, dict):
            self.selected_worker_id = str(online.get("worker_id") or "")
            return
        first = next((worker for worker in workers if worker.get("worker_id")), None)
        if isinstance(first, dict):
            self.selected_worker_id = str(first.get("worker_id") or "")
            return
        self.selected_worker_id = LEGACY_WORKER_ID if self._has_legacy_worker() else ""

    def _selected_worker(self) -> dict[str, Any] | None:
        wanted = str(self.selected_worker_id or "")
        for worker in self._registry_workers():
            if str(worker.get("worker_id") or "") == wanted:
                return worker
        return None

    def _selected_is_legacy(self) -> bool:
        return self.selected_worker_id == LEGACY_WORKER_ID and self._has_legacy_worker()

    def _selected_is_auto(self) -> bool:
        return self.selected_worker_id == AUTO_WORKER_ID and len(self._online_registry_workers()) >= 2

    def _job_target_worker_id(self) -> str:
        if self._selected_is_legacy() or self._selected_is_auto():
            return ""
        worker = self._selected_worker()
        if not worker:
            return ""
        return str(worker.get("worker_id") or "")

    def _worker_select_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        online_workers = self._online_registry_workers()
        if len(online_workers) >= 2:
            best = online_workers[0]
            best_group = self._physical_group_for_worker_id(str(best.get("worker_id") or ""))
            best_name = _shorten(self._group_display_name(best_group), limit=28)
            options.append(discord.SelectOption(
                label="Melhor celular disponível",
                value=AUTO_WORKER_ID,
                description=_shorten(f"failover automático · melhor agora: {best_name}", limit=100),
                emoji="⚙️",
                default=(self.selected_worker_id == AUTO_WORKER_ID),
            ))
        for members in self._physical_groups()[:24]:
            worker = self._preferred_runtime(members)
            if not worker:
                continue
            worker_id = str(worker.get("worker_id") or "").strip()
            if not worker_id:
                continue
            name = _shorten(self._group_display_name(members), limit=80)
            apk = self._group_apk(members)
            parent = self._group_parent(members)
            details: list[str] = []
            if apk is not None:
                apk_caps = {str(item) for item in apk.get("capabilities") or []}
                details.append("APK pronto" if apk.get("online") and "apk-builder" in apk_caps else ("APK online" if apk.get("online") else "APK offline"))
            if parent is not None:
                details.append("Termux fallback" if parent.get("online") else "Termux offline")
            model = str((apk or {}).get("name") or "").strip()
            if model and model.lower() != name.lower():
                details.insert(0, model)
            online = any(bool(item.get("online")) for item in members)
            options.append(discord.SelectOption(
                label=name[:100],
                value=worker_id[:100],
                description=_shorten(" · ".join(details) or ("online" if online else "offline"), limit=100),
                emoji="🟢" if online else "🔴",
                default=(worker_id == self.selected_worker_id),
            ))
        if self._has_legacy_worker():
            roles = ", ".join(self.snapshot.roles[:3]) or "phone-worker"
            options.append(discord.SelectOption(
                label=_shorten(self.snapshot.name or "phone-worker direto", limit=100),
                value=LEGACY_WORKER_ID,
                description=_shorten(f"direto online · {roles}", limit=100),
                emoji="🟢",
                default=(self.selected_worker_id == LEGACY_WORKER_ID),
            ))
        return options

    def _selected_supported_tasks(self) -> set[str] | None:
        if self._selected_is_auto():
            union: set[str] = set()
            for worker in self._online_registry_workers():
                supported = _task_set(worker.get("supported_tasks"))
                if not supported:
                    return None
                union.update(supported)
            return union
        if self._selected_is_legacy():
            status = self.snapshot.status if isinstance(self.snapshot.status, dict) else {}
            supported = _task_set(status.get("supported_tasks"))
            # O phone-worker direto continua sendo o builder/agent mesmo sem celular
            # pareado pelo APK. Manter ações essenciais visíveis evita regressão
            # quando o registry ainda mostra "nenhum celular pareado".
            legacy_direct = {
                "ping", "status", "worker_self_check", "diagnostic_basic",
                "worker_logs", "worker_update", "apk_build_debug", "apk_publish_last",
                "boot_status", "boot_repair", "service_status", "service_start",
                "service_stop", "service_restart", "network_probe", "tailscale_status",
            }
            return supported | legacy_direct
        worker = self._selected_worker()
        if not worker:
            return set()
        supported = _task_set(worker.get("supported_tasks")) or _task_set(worker.get("supportedTasks")) or _task_set(worker.get("appJobs"))
        if not supported:
            health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
            status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
            supported = (
                _task_set(health.get("supported_tasks"))
                or _task_set(health.get("supportedTasks"))
                or _task_set(status.get("supported_tasks"))
                or _task_set(status.get("supportedTasks"))
                or _task_set(status.get("appJobs"))
            )
        # None = worker registrado antigo/externo sem declarar suporte; manter ações visíveis.
        return supported or None

    def _panel_action_specs_for_selected(self) -> list[dict[str, Any]]:
        selected_worker = self._selected_worker()
        specs: list[dict[str, Any]] = [
            {"label": "Recovery de pareamento", "value": "_pairing_modal", "description": "Código manual excepcional", "emoji": "🔐", "panel_action": "pair", "category": "organize"},
            {"label": "Como adicionar", "value": "_onboarding_guide", "description": "Guia simples", "emoji": "📲", "panel_action": "onboarding", "category": "add"},
            {"label": "Testar troca automática", "value": "_failover_test", "description": "Precisa de 2+ workers", "emoji": "🧪", "panel_action": "failover", "category": "add"},
            {"label": "Limpar jobs", "value": "_cleanup_jobs", "description": "Remove travados/antigos", "emoji": "🧹", "panel_action": "cleanup", "category": "organize"},
        ]
        if selected_worker is not None or self._selected_is_legacy() or self._selected_is_auto():
            specs.append({"label": "Ver último resultado", "value": "_show_last_result", "description": "Detalhes do último job", "emoji": "📄", "panel_action": "last_result", "category": "quick"})

        if selected_worker is not None:
            specs.extend([
                {"label": "Detalhes do celular", "value": "_worker_details", "description": "Resumo completo", "emoji": "📱", "panel_action": "details", "category": "quick"},
                {"label": "Testar runtime APK", "value": "_apk_internal_test", "description": "Agenda jobs internos seguros", "emoji": "🧪", "panel_action": "apk_internal_test", "category": "quick"},
                {"label": "Atualizar runtime", "value": "_apk_refresh_runtime", "description": "Heartbeat/status agora", "emoji": "🔄", "panel_action": "apk_refresh_runtime", "category": "apk", "apk_job_type": "apk_refresh_runtime"},
                {"label": "Pacote de status", "value": "_apk_force_status_bundle", "description": "Bundle completo do APK", "emoji": "📦", "panel_action": "apk_force_status_bundle", "category": "apk", "apk_job_type": "apk_force_status_bundle"},
                {"label": "Testar notificação", "value": "_apk_test_notification", "description": "Notificação local segura", "emoji": "🔔", "panel_action": "apk_test_notification", "category": "apk", "apk_job_type": "apk_test_notification"},
                {"label": "Reparar estado local", "value": "_apk_repair_local_state", "description": "Limpa erro transitório", "emoji": "🛠️", "panel_action": "apk_repair_local_state", "category": "apk", "apk_job_type": "apk_repair_local_state"},
                {"label": "Limpar histórico", "value": "_apk_reset_job_history", "description": "Histórico local de jobs", "emoji": "🧾", "panel_action": "apk_reset_job_history", "category": "apk", "apk_job_type": "apk_reset_job_history"},
                {"label": "Limpar cache APK", "value": "_apk_trim_cache", "description": "Cache interno pequeno", "emoji": "🧹", "panel_action": "apk_trim_cache", "category": "apk", "apk_job_type": "apk_trim_cache"},
                {"label": "Limpar updates APK", "value": "_apk_update_storage_cleanup", "description": "Instaladores/staging antigos", "emoji": "🗑️", "panel_action": "apk_update_storage_cleanup", "category": "apk", "apk_job_type": "apk_update_storage_cleanup"},
                {"label": "Sincronizar perfil", "value": "_apk_sync_profile_now", "description": "Perfil do APK", "emoji": "👤", "panel_action": "apk_sync_profile_now", "category": "apk", "apk_job_type": "apk_sync_profile_now"},
                {"label": "Verificar update", "value": "_apk_verify_update_state", "description": "Manifesto/latest.json", "emoji": "⬆️", "panel_action": "apk_verify_update_state", "category": "apk", "apk_job_type": "apk_verify_update_state"},
                {"label": "Worker nativo", "value": "_apk_native_worker_status", "description": "Heartbeat direto do APK", "emoji": "📡", "panel_action": "apk_native_worker_status", "category": "apk", "apk_job_type": "apk_native_worker_status"},
                {"label": "Boot nativo", "value": "_apk_native_boot_status", "description": "Sem Termux:Boot", "emoji": "🚀", "panel_action": "apk_native_boot_status", "category": "apk", "apk_job_type": "apk_native_boot_status"},
                {"label": "Shell APK", "value": "_apk_local_shell_probe", "description": "Allowlist no sandbox", "emoji": "🧰", "panel_action": "apk_local_shell_probe", "category": "apk", "apk_job_type": "apk_local_shell_probe"},
                {"label": "Python APK", "value": "_apk_python_runtime_probe", "description": "Health check Python", "emoji": "🐍", "panel_action": "apk_python_runtime_probe", "category": "apk", "apk_job_type": "apk_python_runtime_probe"},
                {"label": "Python runtime", "value": "_apk_python_runtime_info", "description": "Versão e módulos internos", "emoji": "🐍", "panel_action": "apk_python_runtime_info", "category": "apk", "apk_job_type": "apk_python_runtime_info"},
                {"label": "Python status", "value": "_apk_python_status_bundle", "description": "Bundle gerado pelo Python", "emoji": "📦", "panel_action": "apk_python_status_bundle", "category": "apk", "apk_job_type": "apk_python_status_bundle"},
                {"label": "Python storage", "value": "_apk_python_storage_check", "description": "Armazenamento via Python", "emoji": "💾", "panel_action": "apk_python_storage_check", "category": "apk", "apk_job_type": "apk_python_storage_check"},
                {"label": "Python logs", "value": "_apk_python_log_summary", "description": "Resumo local de jobs", "emoji": "🧾", "panel_action": "apk_python_log_summary", "category": "apk", "apk_job_type": "apk_python_log_summary"},
                {"label": "Python rede", "value": "_apk_python_network_diagnostic", "description": "Diagnóstico de rede Python", "emoji": "🌐", "panel_action": "apk_python_network_diagnostic", "category": "apk", "apk_job_type": "apk_python_network_diagnostic"},
                {"label": "Python arquivos", "value": "_apk_python_runtime_files_check", "description": "Runtime/sandbox internos", "emoji": "📁", "panel_action": "apk_python_runtime_files_check", "category": "apk", "apk_job_type": "apk_python_runtime_files_check"},
                {"label": "Linux runtime", "value": "_apk_linux_runtime_probe", "description": "Base Linux interna", "emoji": "🐧", "panel_action": "apk_linux_runtime_probe", "category": "apk", "apk_job_type": "apk_linux_runtime_probe"},
                {"label": "Linux rootfs", "value": "_apk_linux_rootfs_probe", "description": "Rootfs experimental", "emoji": "📦", "panel_action": "apk_linux_rootfs_probe", "category": "apk", "apk_job_type": "apk_linux_rootfs_probe"},
                {"label": "Rootfs status", "value": "_apk_core_linux_rootfs_status", "description": "Estado rootfs interno", "emoji": "📦", "panel_action": "apk_core_linux_rootfs_status", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_status"},
                {"label": "Preparar rootfs", "value": "_apk_core_linux_rootfs_prepare", "description": "Scaffold validável", "emoji": "🧱", "panel_action": "apk_core_linux_rootfs_prepare", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_prepare"},
                {"label": "Validar rootfs", "value": "_apk_core_linux_rootfs_validate", "description": "Manifesto e layout", "emoji": "✅", "panel_action": "apk_core_linux_rootfs_validate", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_validate"},
                {"label": "Reparar rootfs", "value": "_apk_core_linux_rootfs_repair", "description": "Recria scaffold seguro", "emoji": "🩹", "panel_action": "apk_core_linux_rootfs_repair", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_repair"},
                {"label": "Limpar staging rootfs", "value": "_apk_core_linux_rootfs_clean_staging", "description": "Preserva rootfs ativa", "emoji": "🧹", "panel_action": "apk_core_linux_rootfs_clean_staging", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_clean_staging"},
                {"label": "Import rootfs", "value": "_apk_core_linux_rootfs_import_status", "description": "Estado da importação real", "emoji": "📥", "panel_action": "apk_core_linux_rootfs_import_status", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_import_status"},
                {"label": "Validar rootfs real", "value": "_apk_core_linux_rootfs_import_validate", "description": "Valida rootfs importado", "emoji": "✅", "panel_action": "apk_core_linux_rootfs_import_validate", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_import_validate"},
                {"label": "Runner preflight", "value": "_apk_core_linux_runner_preflight", "description": "Requisitos sem iniciar", "emoji": "🧭", "panel_action": "apk_core_linux_runner_preflight", "category": "apk", "apk_job_type": "apk_core_linux_runner_preflight"},
                {"label": "Cancelar import rootfs", "value": "_apk_core_linux_rootfs_import_abort", "description": "Limpa staging da importação", "emoji": "🧹", "panel_action": "apk_core_linux_rootfs_import_abort", "category": "apk", "apk_job_type": "apk_core_linux_rootfs_import_abort"},
                {"label": "Box64", "value": "_apk_linux_box64_probe", "description": "Camada x86_64 futura", "emoji": "🧱", "panel_action": "apk_linux_box64_probe", "category": "apk", "apk_job_type": "apk_linux_box64_probe"},
                {"label": "Linux preparar", "value": "_apk_linux_prepare_directories", "description": "Diretórios e planos", "emoji": "🗂️", "panel_action": "apk_linux_prepare_directories", "category": "apk", "apk_job_type": "apk_linux_prepare_directories"},
                {"label": "Linux plano", "value": "_apk_linux_generate_setup_plan", "description": "Setup sem download", "emoji": "🧭", "panel_action": "apk_linux_generate_setup_plan", "category": "apk", "apk_job_type": "apk_linux_generate_setup_plan"},
                {"label": "Provisioner", "value": "_apk_linux_provisioner_probe", "description": "Plano rootfs/Box64", "emoji": "🐧", "panel_action": "apk_linux_provisioner_probe", "category": "apk", "apk_job_type": "apk_linux_provisioner_probe"},
                {"label": "Executor nativo", "value": "_apk_core_linux_native_executor_probe", "description": "Binário embutido do app", "emoji": "🧬", "panel_action": "apk_core_linux_native_executor_probe", "category": "apk", "apk_job_type": "apk_core_linux_native_executor_probe"},
                {"label": "Testar executor", "value": "_apk_core_linux_native_executor_test", "description": "Teste allowlist do executor", "emoji": "🧪", "panel_action": "apk_core_linux_native_executor_test", "category": "apk", "apk_job_type": "apk_core_linux_native_executor_test"},
                {"label": "Reparar Linux", "value": "_apk_core_linux_internal_repair", "description": "Recria estado interno", "emoji": "🩹", "panel_action": "apk_core_linux_internal_repair", "category": "apk", "apk_job_type": "apk_core_linux_internal_repair"},
                {"label": "Bedrock reqs", "value": "_apk_minecraft_bedrock_requirements", "description": "RAM/storage/Ubuntu", "emoji": "⛏️", "panel_action": "apk_minecraft_bedrock_requirements", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_requirements"},
                {"label": "Bedrock plano", "value": "_apk_minecraft_bedrock_install_plan", "description": "Instalação assistida", "emoji": "🧾", "panel_action": "apk_minecraft_bedrock_install_plan", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_install_plan"},
                {"label": "Bedrock props", "value": "_apk_minecraft_bedrock_properties_template", "description": "Template server.properties", "emoji": "⚙️", "panel_action": "apk_minecraft_bedrock_properties_template", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_properties_template"},
                {"label": "Bedrock status", "value": "_apk_minecraft_bedrock_status", "description": "Arquivos do servidor", "emoji": "🟫", "panel_action": "apk_minecraft_bedrock_status", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_status"},
                {"label": "Bedrock preparar", "value": "_apk_minecraft_bedrock_prepare_files", "description": "server.properties e planos", "emoji": "🧱", "panel_action": "apk_minecraft_bedrock_prepare_files", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_prepare_files"},
                {"label": "Bedrock start", "value": "_apk_minecraft_bedrock_start_plan", "description": "Plano de início seguro", "emoji": "▶️", "panel_action": "apk_minecraft_bedrock_start_plan", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_start_plan"},
                {"label": "Bedrock stop", "value": "_apk_minecraft_bedrock_stop_plan", "description": "Plano de parada segura", "emoji": "⏹️", "panel_action": "apk_minecraft_bedrock_stop_plan", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_stop_plan"},
                {"label": "Bedrock logs", "value": "_apk_minecraft_bedrock_logs_status", "description": "Estado dos logs", "emoji": "📄", "panel_action": "apk_minecraft_bedrock_logs_status", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_logs_status"},
                {"label": "Bedrock validar", "value": "_apk_minecraft_bedrock_validate_device", "description": "RAM/storage antes de instalar", "emoji": "✅", "panel_action": "apk_minecraft_bedrock_validate_device", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_validate_device"},
                {"label": "Bedrock instalador", "value": "_apk_minecraft_bedrock_installer_status", "description": "Etapas assistidas", "emoji": "🧭", "panel_action": "apk_minecraft_bedrock_installer_status", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_installer_status"},
                {"label": "Bedrock ambiente", "value": "_apk_minecraft_bedrock_prepare_environment_plan", "description": "Prepara plano seguro", "emoji": "🗂️", "panel_action": "apk_minecraft_bedrock_prepare_environment_plan", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_prepare_environment_plan"},
                {"label": "Bedrock manifesto", "value": "_apk_minecraft_bedrock_download_manifest", "description": "Downloads sem baixar", "emoji": "📜", "panel_action": "apk_minecraft_bedrock_download_manifest", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_download_manifest"},
                {"label": "Bedrock preflight", "value": "_apk_minecraft_bedrock_final_preflight", "description": "Últimos bloqueios", "emoji": "🛫", "panel_action": "apk_minecraft_bedrock_final_preflight", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_final_preflight"},
                {"label": "Bedrock runtime", "value": "_apk_minecraft_bedrock_runtime_status", "description": "Runner assistido", "emoji": "🟢", "panel_action": "apk_minecraft_bedrock_runtime_status", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runtime_status"},
                {"label": "Iniciar Bedrock", "value": "_apk_minecraft_bedrock_runtime_start", "description": "Ativa serviço assistido", "emoji": "▶️", "panel_action": "apk_minecraft_bedrock_runtime_start", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runtime_start"},
                {"label": "Parar Bedrock", "value": "_apk_minecraft_bedrock_runtime_stop", "description": "Para runtime assistido", "emoji": "⏹️", "panel_action": "apk_minecraft_bedrock_runtime_stop", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runtime_stop"},
                {"label": "Logs runtime", "value": "_apk_minecraft_bedrock_runtime_logs", "description": "Logs do runner", "emoji": "📄", "panel_action": "apk_minecraft_bedrock_runtime_logs", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runtime_logs"},
                {"label": "Runner status", "value": "_apk_minecraft_bedrock_runner_status", "description": "Estado real do processo", "emoji": "🟢", "panel_action": "apk_minecraft_bedrock_runner_status", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runner_status"},
                {"label": "Runner preflight", "value": "_apk_minecraft_bedrock_runner_preflight", "description": "Bloqueios para iniciar", "emoji": "🛫", "panel_action": "apk_minecraft_bedrock_runner_preflight", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runner_preflight"},
                {"label": "Console tail", "value": "_apk_minecraft_bedrock_console_tail", "description": "Últimas linhas do console", "emoji": "💬", "panel_action": "apk_minecraft_bedrock_console_tail", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_console_tail"},
                {"label": "Reparar runtime", "value": "_apk_minecraft_bedrock_runtime_repair", "description": "Recria fila/logs sem apagar mundos", "emoji": "🩹", "panel_action": "apk_minecraft_bedrock_runtime_repair", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_runtime_repair"},
                {"label": "Runtime persist.", "value": "_apk_runtime_foreground_probe", "description": "Serviço com notificação", "emoji": "🟢", "panel_action": "apk_runtime_foreground_probe", "category": "apk", "apk_job_type": "apk_runtime_foreground_probe"},
                {"label": "Ativar persist.", "value": "_apk_runtime_foreground_start", "description": "Inicia foreground runtime", "emoji": "▶️", "panel_action": "apk_runtime_foreground_start", "category": "apk", "apk_job_type": "apk_runtime_foreground_start"},
                {"label": "Parar persist.", "value": "_apk_runtime_foreground_stop", "description": "Para foreground runtime", "emoji": "⏹️", "panel_action": "apk_runtime_foreground_stop", "category": "apk", "apk_job_type": "apk_runtime_foreground_stop"},
                {"label": "Linux estratégia", "value": "_apk_linux_strategy_plan", "description": "Core Linux vs fallback", "emoji": "🧭", "panel_action": "apk_linux_strategy_plan", "category": "apk", "apk_job_type": "apk_linux_strategy_plan"},
                {"label": "Linux manifesto", "value": "_apk_linux_manifest_plan", "description": "Plano de downloads", "emoji": "📜", "panel_action": "apk_linux_manifest_plan", "category": "apk", "apk_job_type": "apk_linux_manifest_plan"},
                {"label": "Bedrock assist.", "value": "_apk_minecraft_bedrock_assisted_install_plan", "description": "Plano assistido seguro", "emoji": "🧱", "panel_action": "apk_minecraft_bedrock_assisted_install_plan", "category": "apk", "apk_job_type": "apk_minecraft_bedrock_assisted_install_plan"},
                {"label": "Renomear celular", "value": "_rename_worker", "description": "Troca o nome exibido", "emoji": "✏️", "panel_action": "rename", "category": "organize"},
                {"label": "Editar funções", "value": "_edit_roles", "description": "Perfil + extras/remoções", "emoji": "🧩", "panel_action": "roles", "category": "organize"},
            ])
            if selected_worker.get("enabled", True):
                specs.append({"label": "Pausar celular", "value": "_pause_worker", "description": "Não recebe jobs", "emoji": "⏸️", "panel_action": "pause", "category": "organize"})
            else:
                specs.append({"label": "Ativar celular", "value": "_resume_worker", "description": "Volta a receber jobs", "emoji": "▶️", "panel_action": "resume", "category": "organize"})
            if not selected_worker.get("online"):
                specs.append({"label": "Remover offline", "value": "_delete_worker", "description": "Remove do registry", "emoji": "🗑️", "panel_action": "delete", "category": "organize"})
        return specs

    def _action_specs_for_selected(self, *, category: str | None = None) -> list[dict[str, Any]]:
        supported = self._selected_supported_tasks()
        specs_by_value: dict[str, dict[str, Any]] = {}

        def add_if_visible(spec: dict[str, Any]) -> None:
            value = str(spec.get("value") or "")
            if value not in WORKER_PANEL_ACTION_VALUES:
                return
            apk_job_type = _core_worker_app_normalize_job_type(spec.get("apk_job_type")) if spec.get("apk_job_type") else ""
            if apk_job_type and supported is not None and apk_job_type not in supported:
                return
            specs_by_value.setdefault(value, dict(spec))

        for spec in self._panel_action_specs_for_selected():
            add_if_visible(spec)

        for spec in WORKER_ACTION_SPECS:
            value = str(spec.get("value") or "")
            if value not in WORKER_PANEL_ACTION_VALUES:
                continue
            job_type = _task_name(spec.get("job_type"))
            requires_declared = bool(spec.get("requires_declared"))
            if requires_declared and (supported is None or job_type not in supported):
                continue
            if supported is not None and job_type not in supported:
                continue
            add_if_visible(spec)

        return sorted(
            specs_by_value.values(),
            key=lambda item: WORKER_PANEL_ACTION_ORDER_INDEX.get(str(item.get("value") or ""), 999),
        )

    def _category_select_options(self) -> list[discord.SelectOption]:
        return []

    def _action_select_options(self) -> list[discord.SelectOption]:
        specs = self._action_specs_for_selected()
        if not specs:
            return [discord.SelectOption(
                label="Sem ações disponíveis",
                value="_unsupported",
                description="Atualize o worker ou escolha outro celular",
                emoji="⚠️",
            )]
        return [
            discord.SelectOption(
                label=str(spec["label"])[:100],
                value=str(spec["value"])[:100],
                description=_shorten(spec.get("description"), limit=100),
                emoji=str(spec.get("emoji") or "⚙️"),
            )
            for spec in specs[:25]
        ]

    def _new_select(self, **kwargs: Any):
        select_cls = getattr(discord.ui, "StringSelect", None) or getattr(discord.ui, "Select")
        return select_cls(**kwargs)

    def _rebuild_layout(self, *, expired: bool = False) -> None:
        self._clear_items()
        snapshot = self.snapshot
        if expired:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 📱 Core Workers\n"
                    "Painel expirado. Use `workers`, `worker` ou `w` para abrir novamente."
                ),
                accent_color=discord.Color.dark_grey(),
            ))
            return

        self._ensure_selected_worker()
        summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
        queued = int((summary or {}).get('jobs_queued') or 0)
        running = int((summary or {}).get('jobs_running') or 0)
        registered = int((summary or {}).get('registered') or 0)
        online = int((summary or {}).get('online') or 0)
        pairings = int((summary or {}).get('pairings_active') or 0)
        queue_label = "livre"
        if queued or running:
            queue_label = f"{queued} na fila · {running} em execução"
            if queued and not online and not self._has_legacy_worker():
                queue_label += " · sem celular online"
        registry_workers = []
        if isinstance(snapshot.registry_snapshot, dict):
            registry_workers = [w for w in (snapshot.registry_snapshot.get("workers") or []) if isinstance(w, dict)]
        automation_label = _shorten(_automation_status_text(registry_workers), limit=190)
        if snapshot.registry_error:
            automation_label = _shorten(f"{automation_label} · registry: {snapshot.registry_error}", limit=190)
        pairing_note = f" · {pairings} código{'s' if pairings != 1 else ''} de pareamento ativo{'s' if pairings != 1 else ''}" if pairings else ""
        header = discord.ui.TextDisplay(
            "# 📱 Core Workers\n"
            f"{snapshot.state_label}{pairing_note}\n"
            f"**Fila:** {queue_label}\n"
            f"**Atualização:** {automation_label}"
        )


        worker_options = self._worker_select_options()
        worker_select = None
        action_select = None
        if worker_options:
            worker_select = self._new_select(
                placeholder="Selecionar celular",
                min_values=1,
                max_values=1,
                options=worker_options,
                disabled=False,
            )
            worker_select.callback = self._select_worker

            action_options = self._action_select_options()
            action_disabled = bool(action_options and str(action_options[0].value) == "_unsupported")
            action_placeholder = "Escolher ação" if not action_disabled else "Sem ações disponíveis"
            action_select = self._new_select(
                placeholder=action_placeholder,
                min_values=1,
                max_values=1,
                options=action_options,
                disabled=action_disabled,
            )
            action_select.callback = self._select_action

        refresh = discord.ui.Button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.primary)
        refresh.callback = self._refresh_panel

        wake = discord.ui.Button(
            label="Acordar phone-worker",
            emoji="📡",
            style=discord.ButtonStyle.secondary,
            disabled=not snapshot.configured,
        )
        wake.callback = self._wake_worker

        components: list[Any] = [
            header,
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._selected_worker_lines(snapshot))),
        ]
        if worker_select is not None and action_select is not None:
            components.extend([
                discord.ui.Separator(),
                discord.ui.ActionRow(worker_select),
                discord.ui.ActionRow(action_select),
            ])
            components.append(discord.ui.ActionRow(refresh))
        else:
            components.append(discord.ui.ActionRow(refresh))
        if not CORE_WORKER_APK_REPLACES_TERMUX and not _snapshot_has_online_worker(snapshot):
            components.append(discord.ui.ActionRow(wake))
        container = discord.ui.Container(*components, accent_color=snapshot.accent)
        self.add_item(container)

    def _selected_worker_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        if snapshot.registry_error:
            return [
                "## Worker",
                f"Registry indisponível: `{_shorten(_redact(snapshot.registry_error), limit=120)}`",
            ]

        workers = self._registry_workers()
        worker = self._selected_worker()
        lines = ["## Worker"]
        if self._selected_is_auto():
            online_workers = self._online_registry_workers()
            best = online_workers[0] if online_workers else {}
            best_name = _shorten(best.get("name") or best.get("worker_id") or "worker", limit=36) if isinstance(best, dict) else "worker"
            lines.append("⚙️ **Melhor celular disponível** · `failover`")
            lines.append(f"-# {len(online_workers)} online · melhor agora: {best_name} · jobs sem alvo podem migrar se um worker cair")
            roles_union: list[str] = []
            for item in online_workers:
                for role in item.get("roles") or []:
                    role_s = str(role)
                    if role_s and role_s not in roles_union:
                        roles_union.append(role_s)
            if roles_union:
                lines.append(f"-# {len(roles_union)} função(ões) técnicas disponíveis nos celulares online")
        elif worker:
            members = self._physical_group_for_worker_id(str(worker.get("worker_id") or "")) or [worker]
            preferred = self._preferred_runtime(members) or worker
            parent = self._group_parent(members)
            apk = self._group_apk(members)
            online = any(bool(item.get("online")) for item in members)
            icon = "🟢" if online else "🔴"
            name = _shorten(self._group_display_name(members), limit=36)
            lines[0] = f"## {icon} {name}"

            seen_values = [float(item.get("last_seen_age_seconds") or 10**9) for item in members if item.get("last_seen_age_seconds") is not None]
            seen = _format_age(min(seen_values)) if seen_values else "n/a"
            lines.append(f"**Status:** {('online' if online else 'offline')} · visto {seen}")

            runtime_parts: list[str] = []
            if apk is not None:
                apk_caps = {str(item) for item in apk.get("capabilities") or []}
                apk_version = _shorten(apk.get("version") or "sem versão", limit=24)
                apk_state = "principal" if apk.get("online") and "apk-builder" in apk_caps else ("online" if apk.get("online") else "offline")
                runtime_parts.append(f"APK `{apk_version}` · {apk_state}")
            if parent is not None:
                parent_version = _agent_version_label(parent.get("version"))
                parent_state = "fallback" if parent.get("online") else "offline"
                runtime_parts.append(f"Termux `{parent_version}` · {parent_state}")
            if runtime_parts:
                lines.append("**Runtimes:** " + " · ".join(runtime_parts))

            lines.append(f"**Aparelho:** {_battery_text(preferred)} · {_simple_network_text(preferred)}")
            if len(members) <= 1:
                queue_text = _queue_status_text(preferred)
                if queue_text:
                    lines.append(f"**Fila:** {queue_text}")
            model = str((apk or {}).get("name") or "").strip()
            if model and model.lower() != name.lower():
                lines.append(f"-# Modelo: {model}")
        elif self._selected_is_legacy():
            version = _shorten((snapshot.status or {}).get("version") or "sem versão", limit=24)
            lines[0] = f"## 🟢 {_shorten(snapshot.name or 'phone-worker direto', limit=36)}"
            status = snapshot.status if isinstance(snapshot.status, dict) else {}
            lines.append("**Status:** online direto")
            lines.append(f"**Runtime:** `{version}` · {_script_health_label({'status': status})}")
            lines.append("-# Diagnóstico completo em **Detalhes do celular**.")
        elif workers:
            lines.append("Selecione um worker.")
        elif snapshot.configured:
            legacy_state = "🟢 direto online" if snapshot.online else "🔴 direto offline"
            lines.append(f"{legacy_state}. Nenhum worker pareado ainda.")
        else:
            lines.append("Nenhum worker configurado ou pareado.")

        supported = self._selected_supported_tasks()
        if self._selected_is_legacy() and supported is not None and "service_status" not in supported:
            lines.append("-# Ações avançadas ocultas: phone-worker legado/desatualizado.")
        elif supported == set():
            lines.append("-# Nenhuma ação compatível declarada por este worker.")

        if snapshot.action_note:
            note = _shorten(_redact(snapshot.action_note), limit=80)
            label = "Última falha de wake" if _snapshot_has_online_worker(snapshot) and "worker ainda offline" in note.lower() else "Última ação"
            lines.append(f"-# {label}: {note}")
        # Saídas completas de watchdog/sync ficam fora do painel principal para manter o card compacto.
        return lines

    async def _select_worker(self, interaction: discord.Interaction):
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        if values:
            self.selected_worker_id = str(values[0] or "")
        # Selecionar worker é navegação visual, não uma ação/job real.
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _select_action_category(self, interaction: discord.Interaction):
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        category = str((values or [""])[0] or "")
        if category not in {str(item.get("value")) for item in WORKER_ACTION_CATEGORIES}:
            category = "quick"
        self.selected_action_category = category
        # Abrir categoria é só navegação; não altera a última ação útil do painel.
        self._rebuild_layout()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _select_action(self, interaction: discord.Interaction):
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        action = str((values or [""])[0] or "")
        if action == "_unsupported":
            await interaction.response.send_message("Esse worker está desatualizado ou não declarou suporte para ações seguras.", ephemeral=True)
            return
        if action == "_create_pairing":
            await self._create_pairing(interaction)
            return
        if action == "_pairing_modal":
            await self._open_pairing_modal(interaction)
            return
        if action == "_cleanup_jobs":
            await self._cleanup_jobs(interaction)
            return
        if action == "_onboarding_guide":
            await self._show_onboarding_guide(interaction)
            return
        if action == "_failover_test":
            await self._run_failover_test(interaction)
            return
        if action == "_worker_details":
            await self._show_worker_details(interaction)
            return
        if action == "_apk_internal_test":
            await self._queue_apk_internal_runtime_test(interaction)
            return
        if action == "_show_last_result":
            await self._show_last_result(interaction)
            return
        apk_spec = next((spec for spec in self._panel_action_specs_for_selected() if str(spec.get("value")) == action and spec.get("apk_job_type")), None)
        if apk_spec is not None:
            await self._queue_apk_manual_job(interaction, str(apk_spec.get("apk_job_type") or ""), label=str(apk_spec.get("label") or "job APK"))
            return
        if action == "_rename_worker":
            await self._open_rename_modal(interaction)
            return
        if action == "_edit_roles":
            await self._open_roles_modal(interaction)
            return
        if action == "_pause_worker":
            await self._set_selected_worker_enabled(interaction, enabled=False)
            return
        if action == "_resume_worker":
            await self._set_selected_worker_enabled(interaction, enabled=True)
            return
        if action == "_delete_worker":
            await self._delete_selected_worker(interaction)
            return
        specs = {str(spec.get("value")): spec for spec in self._action_specs_for_selected()}
        spec = specs.get(action)
        if spec is None:
            await interaction.response.send_message("Ação indisponível para esse worker.", ephemeral=True)
            return
        await self._queue_named_job(
            interaction,
            job_type=str(spec.get("job_type") or action),
            payload=dict(spec.get("payload") or {}),
            summary=str(spec.get("summary") or spec.get("label") or action),
        )

    async def _queue_apk_internal_runtime_test(self, interaction: discord.Interaction) -> None:
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.apk_job")
        worker = self._selected_worker()
        worker_id = str((worker or {}).get("worker_id") or "").strip()
        result = await asyncio.to_thread(_queue_core_worker_app_internal_runtime_test, worker_id)
        if result.get("ok"):
            created = int(result.get("created") or 0)
            note = f"🧪 Teste do runtime APK agendado: {created} job(s) interno(s). Abra/aguarde o app sincronizar."
        else:
            note = f"⚠️ Não consegui agendar teste do runtime APK: {_shorten(result.get('error'), limit=120)}"
        self.snapshot = await self.cog._collect_workers_snapshot(action_note=note)
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, note)

    async def _queue_apk_manual_job(self, interaction: discord.Interaction, job_type: str, *, label: str = "job APK") -> None:
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.apk_job")
        worker = self._selected_worker()
        worker_id = str((worker or {}).get("worker_id") or "").strip()
        payload: dict[str, Any] = {}
        if job_type in {"apk_sync_profile_now", "apk_sync_profile"} and isinstance(worker, dict):
            payload["profile"] = _normalize_worker_profile((worker.get("profile") or (worker.get("status") if isinstance(worker.get("status"), dict) else {}).get("profile") or "midia"), default="midia")
        result = await asyncio.to_thread(_queue_core_worker_app_manual_job, worker_id, job_type, payload=payload, reason="workers-panel-apk-command")
        if result.get("ok"):
            if result.get("alreadyPending"):
                note = f"📲 {label}: já estava pendente no APK. Abra/aguarde o app sincronizar."
            elif result.get("alreadyRunning"):
                note = f"📲 {label}: já está rodando no APK. Aguarde o resultado."
            else:
                note = f"📲 {label} agendado no APK. Abra/aguarde o app sincronizar."
        else:
            note = f"⚠️ Não consegui agendar {label}: {_shorten(result.get('error'), limit=120)}"
        self.snapshot = await self.cog._collect_workers_snapshot(action_note=note)
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, note)

    async def _edit_panel_after_response(self, interaction: discord.Interaction) -> None:
        await safe_edit_original_or_message(
            interaction,
            message=self.message,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
            log=logger,
            label="workers.panel_edit",
        )

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        with contextlib.suppress(Exception):
            await interaction.followup.send(message[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _open_flow_message(self, interaction: discord.Interaction, message: str):
        try:
            return await interaction.followup.send(
                message[:1900],
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            return None

    async def _edit_flow_message(self, flow_message: Any, message: str) -> None:
        if flow_message is None:
            return
        with contextlib.suppress(Exception):
            await flow_message.edit(content=message[:1900], allowed_mentions=discord.AllowedMentions.none())

    async def _refresh_panel(self, interaction: discord.Interaction):
        # Botão de refresh precisa reconhecer a interação imediatamente. Se a VPS
        # estiver engasgada e Discord já tiver invalidado o token, ainda tentamos
        # atualizar a mensagem salva sem transformar isso em traceback na view.
        acked = await safe_defer_interaction(
            interaction,
            thinking=False,
            log=logger,
            label="workers.refresh",
        )
        previous_note = self.snapshot.action_note
        self.snapshot = await self.cog._collect_workers_snapshot(action_note=previous_note)
        self._ensure_selected_worker()
        self._rebuild_layout()
        if acked:
            await safe_edit_original_or_message(
                interaction,
                message=self.message,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
                log=logger,
                label="workers.refresh",
            )
        elif self.message is not None:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _wake_worker(self, interaction: discord.Interaction):
        # Responde imediatamente para o Discord não mostrar "Esta interação falhou".
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.wake")
        flow = await self._open_flow_message(
            interaction,
            "## 📡 Acordando phone-worker\n"
            "Status: tentativa manual enviada ao watchdog da VPS.\n"
            "-# O painel só marca sucesso se o worker voltar a responder heartbeat/health.",
        )
        snapshot = await self.cog._wake_phone_worker(force=True, reason="manual")
        self.snapshot = snapshot
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._edit_flow_message(
            flow,
            "## 📡 Acordar phone-worker\n"
            f"Resultado: {_shorten(snapshot.action_note or snapshot.error or 'verifique o painel', limit=260)}\n"
            "-# Saída técnica fica fora do painel principal para não vazar detalhes nem poluir a mensagem.",
        )

    async def _sync_worker(self, interaction: discord.Interaction):
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.sync")
        snapshot = await self.cog._sync_phone_worker()
        self.snapshot = snapshot
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, f"🔁 Sync phone-worker: {_shorten(snapshot.action_note or snapshot.error or 'verifique o painel', limit=120)}")

    async def _open_pairing_modal(self, interaction: discord.Interaction):
        view = self

        class PairWorkerModal(discord.ui.Modal, title="Adicionar celular"):
            worker_name = discord.ui.TextInput(
                label="Nome do celular",
                placeholder="Ex.: Xiaomi principal",
                default="Core Worker 2",
                min_length=2,
                max_length=48,
                required=True,
            )
            profile = discord.ui.TextInput(
                label="Perfil",
                placeholder="leve, midia, completo, turbo ou bedrock",
                default="midia",
                min_length=3,
                max_length=16,
                required=True,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                await view._create_pairing(
                    modal_interaction,
                    worker_name=str(self.worker_name.value or "Core Worker 2"),
                    profile=str(self.profile.value or "midia"),
                )

        await interaction.response.send_modal(PairWorkerModal())

    async def _create_pairing(self, interaction: discord.Interaction, *, worker_name: str = "Core Worker 2", profile: str = "midia"):
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.pairing")
        try:
            pairing = await self.cog._create_core_worker_pairing(interaction.user)
            code = str(pairing.get("code") or "")
            expires = _format_seconds(max(0, float(pairing.get("expires_at") or 0) - time.time()))
            base_url = _public_base_url()
            default_name = _shorten(worker_name or "Core Worker 2", limit=48)
            default_profile = _normalize_worker_profile(profile)
            profile_roles = ", ".join(_role_label(role) for role in WORKER_ROLE_PROFILES.get(default_profile, ()))
            msg = (
                "## 🔐 Recovery manual de pareamento\n"
                f"**Código:** `{code}` · expira em `{expires}`\n"
                f"**Nome:** `{default_name}` · **perfil:** `{default_profile}`\n\n"
                "O fluxo normal do APK 0.8.4+ é automático e não usa código. Use este recovery apenas se o enrollment parent → child não conseguir concluir.\n\n"
                "**No APK Core Worker:** abra a área de recovery manual, informe o código e confirme a VPS.\n\n"
                f"Funções desse perfil: `{_shorten(profile_roles, limit=220)}`\n"
                "-# O token do Termux não é compartilhado com o APK no fluxo automático."
            )
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"código de pareamento gerado: {code}")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await self._edit_panel_after_response(interaction)
            await interaction.followup.send(msg[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha ao gerar pareamento: {_compact_failure(exc)}")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await self._edit_panel_after_response(interaction)
            await interaction.followup.send("Não consegui gerar o pareamento agora.", ephemeral=True)

    async def _show_onboarding_guide(self, interaction: discord.Interaction):
        base_url = _public_base_url()
        msg = (
            "## 📲 Como adicionar um celular\n"
            "O Core Worker 0.8.2+ pareia automaticamente com o Termux já autenticado do mesmo aparelho.\n\n"
            "**Fluxo normal:**\n"
            "1. Mantenha o phone-worker Termux online e atualizado.\n"
            "2. Instale/abra o APK privado publicado por esse worker.\n"
            "3. O APK abre o enrollment local em 8767 e o Termux cria automaticamente `<worker>-apk`.\n"
            "4. A credencial filha é entregue pelo loopback e o toolchain começa a sincronizar sozinho.\n\n"
            f"VPS detectada: `{base_url}`\n"
            "O código manual existe somente em **Recovery de pareamento**.\n"
            "-# Termux continua como fallback e nunca entrega o próprio token ao APK."
        )
        await interaction.response.send_message(msg[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _run_failover_test(self, interaction: discord.Interaction):
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.failover")
        online_workers = self._online_registry_workers()
        if len(online_workers) < 2:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note="teste failover precisa de 2 workers online")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await self._edit_panel_after_response(interaction)
            await interaction.followup.send("O teste de failover real precisa de pelo menos 2 workers online. Com 1 worker, use `Testar worker`.", ephemeral=True)
            return
        try:
            result = await self.cog._queue_core_worker_job(
                interaction.user,
                job_type="ping",
                payload={"source": "workers_panel_failover_test"},
                summary="teste failover multi-worker",
                target_worker_id="",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            job_id = str((job or {}).get("job_id") or "")
            self.snapshot = await self.cog._collect_workers_snapshot(action_note="⏳ teste failover enviado; aguardando melhor worker")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await self._edit_panel_after_response(interaction)
            final_job = await self.cog._wait_core_worker_job(job_id, timeout=_env_float("WORKERS_PANEL_ACTION_WAIT_SECONDS", 12.0)) if job_id else None
            worker_id = str((final_job or {}).get("worker_id") or "")
            worker_name = self._worker_name_by_id(worker_id) if worker_id else "worker desconhecido"
            if isinstance(final_job, dict) and str(final_job.get("status") or "").lower() == "succeeded":
                note = f"✅ failover ok: `{_shorten(worker_name, limit=32)}` executou"
            else:
                note = _job_result_note("failover", final_job)
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=note)
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha no teste failover: {_compact_failure(exc)}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)

    def _worker_name_by_id(self, worker_id: str) -> str:
        for worker in self._registry_workers():
            if str(worker.get("worker_id") or "") == str(worker_id or ""):
                return str(worker.get("name") or worker_id)
        return str(worker_id or "")


    def _read_vps_logs_payload_sync(self, *, max_bytes: int = 220_000) -> dict[str, Any]:
        root = _repo_root()
        candidates = [root / "logs" / "updater.log", root / "logs" / "bot.log"]
        parts: list[str] = []
        remaining = max(16_000, max_bytes)
        for path in candidates:
            if remaining <= 0:
                break
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()[-remaining:]
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:
                text = f"[falha lendo {path.name}: {type(exc).__name__}: {exc}]"
            parts.append(f"===== {path.name} =====\n{text}")
            remaining -= len(text.encode("utf-8", errors="ignore"))
        if not parts:
            parts.append("sem logs locais encontrados em logs/updater.log ou logs/bot.log")
        return {"text": "\n\n".join(parts)[-max_bytes:], "max_recent": 18, "max_top": 14, "source": "vps_logs_auto"}

    def _latest_update_zip_payload_sync(self) -> dict[str, Any]:
        root = _repo_root()
        candidates: list[Path] = []
        for base in (root / "data", root / "logs", root):
            if not base.exists():
                continue
            try:
                candidates.extend([p for p in base.rglob("*.zip") if p.is_file() and "core-worker-app/releases" not in str(p)])
            except Exception:
                continue
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        if not candidates:
            raise RuntimeError("nenhum ZIP recente encontrado para auditar")
        path = candidates[0]
        raw = path.read_bytes()
        if len(raw) > 24 * 1024 * 1024:
            raise RuntimeError(f"ZIP recente grande demais para payload seguro: {path.name}")
        return {"filename": path.name, "data_b64": base64.b64encode(raw).decode("ascii"), "max_entries": 1200, "max_preview": 50, "source": "latest_update_zip"}

    def _maintenance_plan_payload_sync(self) -> dict[str, Any]:
        root = _repo_root()
        now = time.time()
        entries: list[dict[str, Any]] = []
        scan_roots = [(root / "tmp_audio", "tmp_audio"), (root / "logs", "log"), (root / "android" / "core-worker-app" / "app" / "build", "build"), (root / "android" / "core-worker-app" / "releases", "release")]
        for base, kind in scan_roots:
            if not base.exists():
                continue
            try:
                iterator = base.rglob("*")
                for item in iterator:
                    if not item.is_file():
                        continue
                    try:
                        st = item.stat()
                    except Exception:
                        continue
                    entries.append({"path": str(item.relative_to(root))[:260], "kind": kind, "size": int(st.st_size), "mtime": float(st.st_mtime)})
                    if len(entries) >= 4000:
                        break
            except Exception:
                continue
        return {"entries": entries, "now": now, "max_entries": 4000, "source": "vps_scan_auto"}

    async def _build_assist_payload(self, job_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        task = _task_name(job_type)
        source = str(payload.get("source") or "")
        if task in {"log_summary", "log_digest"} and source == "vps_logs_auto":
            return await asyncio.to_thread(self._read_vps_logs_payload_sync)
        if task in {"zip_validate", "zip_audit"} and source == "latest_update_zip":
            return await asyncio.to_thread(self._latest_update_zip_payload_sync)
        if task == "maintenance_plan" and source == "vps_scan_auto":
            return await asyncio.to_thread(self._maintenance_plan_payload_sync)
        if task == "endpoint_probe":
            targets = payload.get("targets")
            if targets == ["auto"] or targets == "auto" or not targets:
                base = _public_base_url().rstrip("/")
                payload["targets"] = [base + "/health", base + "/core-worker/app/latest.json"]
                payload.setdefault("timeout_seconds", 3)
            return payload
        return payload

    async def _queue_named_job(
        self,
        interaction: discord.Interaction,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
    ):
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.queue_job")
        target_worker_id = self._job_target_worker_id()
        task = _task_name(job_type)
        readable = summary or task
        target_label = _shorten(target_worker_id or "melhor worker compatível", limit=40)
        flow = await self._open_flow_message(
            interaction,
            f"## ⏳ {_shorten(readable, limit=80)}\n"
            f"Status: preparando payload\n"
            f"Worker: `{target_label}`\n"
            "-# Esta mensagem será editada durante o fluxo. O painel também será atualizado.",
        )
        final_note = ""
        try:
            if task == "worker_update":
                payload = await self.cog._build_worker_update_payload(scripts_only=bool((payload or {}).get("scripts_only")))
            elif task == "apk_build_debug":
                payload = await self.cog._build_apk_builder_payload(payload or {})
            elif task == "apk_publish_last":
                payload = await self._build_assist_payload(job_type, payload or {})
            else:
                payload = await self._build_assist_payload(job_type, payload or {})

            await self._edit_flow_message(
                flow,
                f"## 📨 {_shorten(readable, limit=80)}\n"
                f"Status: enviando job `{task}`\n"
                f"Worker: `{target_label}`",
            )

            if self._selected_is_legacy():
                result = await self.cog._run_legacy_worker_action(job_type=job_type, payload=payload or {})
                result_ok = bool((result or {}).get("ok", True))
                note = _shorten((result or {}).get("summary") or ("ação direta concluída" if result_ok else "ação direta falhou"), limit=160)
                icon = "✅" if result_ok else "❌"
                final_note = f"{icon} `{task}` direto {'concluído' if result_ok else 'falhou'}: {note}"
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=final_note)
                pseudo_job = {
                    "type": task,
                    "status": "succeeded" if result_ok else "failed",
                    "worker_id": str((result or {}).get("worker_id") or "phone-worker-direto"),
                    "summary": note,
                    "result": result or {},
                    "error": "" if result_ok else note,
                    "finished_at": time.time(),
                }
                await self._edit_flow_message(flow, _job_detail_text(pseudo_job) + "\n\n-# Resultado direto salvo no estado do phone-worker.")
            else:
                result = await self.cog._queue_core_worker_job(
                    interaction.user,
                    job_type=job_type,
                    payload=payload or {},
                    summary=summary or job_type,
                    target_worker_id=target_worker_id,
                )
                job = result.get("job") if isinstance(result, dict) else {}
                job_id = str((job or {}).get("job_id") or "")
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"⏳ `{task}` enviado para {target_label}; aguardando resultado")
                self._ensure_selected_worker()
                self._rebuild_layout()
                await self._edit_panel_after_response(interaction)
                await self._edit_flow_message(
                    flow,
                    f"## ⚙️ {_shorten(readable, limit=80)}\n"
                    f"Status: job criado e aguardando execução\n"
                    f"Job: `{job_id or 'sem id'}`\n"
                    f"Worker: `{target_label}`",
                )
                final_job = await self.cog._wait_core_worker_job(job_id, timeout=_env_float("WORKERS_PANEL_ACTION_WAIT_SECONDS", 12.0)) if job_id else None
                final_note = _job_result_note(job_type, final_job)
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=final_note)
                detail = _job_detail_text(final_job) if isinstance(final_job, dict) else final_note
                await self._edit_flow_message(
                    flow,
                    f"{detail}\n\n-# Resultado salvo em Ver último resultado."
                )
        except Exception as exc:
            final_note = f"❌ falha em `{task}`: {_compact_failure(exc)}"
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=final_note)
            await self._edit_flow_message(
                flow,
                f"## ❌ {_shorten(readable, limit=80)}\n"
                f"Status: falhou\nMotivo: `{_shorten(_compact_failure(exc), limit=260)}`",
            )
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)


    async def _show_worker_details(self, interaction: discord.Interaction):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um celular registrado para ver detalhes.", ephemeral=True)
            return
        await interaction.response.send_message(_worker_detail_text(worker), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _show_last_result(self, interaction: discord.Interaction):
        try:
            if self._selected_is_legacy():
                job = await self.cog._latest_legacy_worker_result()
            else:
                worker_id = self._job_target_worker_id()
                # Modo automático/multi-worker: sem alvo fixo, mostrar o job mais recente
                # do registry. Isso evita exigir um worker específico quando a VPS escolhe
                # o melhor builder compatível.
                job = await self.cog._latest_core_worker_job(worker_id)
            view = LastJobResultView(owner_id=self.owner_id, job=job)
            await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await interaction.response.send_message(f"Não consegui buscar o resultado: {_compact_failure(exc)}", ephemeral=True)

    async def _open_rename_modal(self, interaction: discord.Interaction):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um worker registrado para renomear.", ephemeral=True)
            return
        view = self
        current_name = _shorten(worker.get("name") or worker.get("worker_id") or "Core Worker", limit=64)
        worker_id = str(worker.get("worker_id") or "")

        class RenameWorkerModal(discord.ui.Modal, title="Renomear Core Worker"):
            new_name = discord.ui.TextInput(
                label="Novo nome",
                placeholder="Ex.: Redmi Worker",
                default=current_name[:100],
                min_length=2,
                max_length=64,
                required=True,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                await safe_defer_interaction(modal_interaction, thinking=False, log=logger, label="workers.rename_modal")
                try:
                    await view.cog._rename_core_worker(worker_id, str(self.new_name.value or ""))
                    view.snapshot = await view.cog._collect_workers_snapshot(action_note=f"worker renomeado para {_shorten(self.new_name.value, limit=40)}")
                except Exception as exc:
                    view.snapshot = await view.cog._collect_workers_snapshot(action_note=f"falha ao renomear: {_compact_failure(exc)}")
                view._ensure_selected_worker()
                view._rebuild_layout()
                if view.message is not None:
                    await view.message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                with contextlib.suppress(Exception):
                    await modal_interaction.followup.send("Nome do worker atualizado.", ephemeral=True)

        await interaction.response.send_modal(RenameWorkerModal())

    async def _open_roles_modal(self, interaction: discord.Interaction):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um celular registrado para editar funções.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=WorkerRolesEditorView(self, worker=worker),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_selected_worker_enabled(self, interaction: discord.Interaction, *, enabled: bool):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um worker registrado.", ephemeral=True)
            return
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.set_enabled")
        worker_id = str(worker.get("worker_id") or "")
        try:
            await self.cog._set_core_worker_enabled(worker_id, enabled=enabled)
            note = "worker ativado" if enabled else "worker pausado"
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=note)
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha alterando worker: {_compact_failure(exc)}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, self.snapshot.action_note or "Estado do worker atualizado.")

    async def _delete_selected_worker(self, interaction: discord.Interaction):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um worker offline para remover.", ephemeral=True)
            return
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.delete")
        worker_id = str(worker.get("worker_id") or "")
        try:
            await self.cog._delete_core_worker(worker_id)
            self.selected_worker_id = ""
            self.snapshot = await self.cog._collect_workers_snapshot(action_note="worker offline removido")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha removendo worker: {_compact_failure(exc)}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, self.snapshot.action_note or "Worker removido/atualizado.")

    async def _cleanup_jobs(self, interaction: discord.Interaction):
        await safe_defer_interaction(interaction, thinking=False, log=logger, label="workers.cleanup")
        try:
            result = await self.cog._cleanup_core_worker_jobs()
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"limpeza de jobs: {result}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha limpando jobs: {_compact_failure(exc)}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await self._edit_panel_after_response(interaction)
        await self._send_ephemeral(interaction, self.snapshot.action_note or "Jobs limpos/atualizados.")


class WorkersCommandMixin:
    """Comando prefixado `workers`/`worker`/`w` da cog Utility.

    Painel único em Components V2 para o Core Worker. O comando é privado do
    dono do bot e limitado à guild configurada; não é mais slash command.
    """

    async def _can_use_workers_author(self, user: discord.abc.User) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(user))
        return False

    def _worker_base_config(self) -> tuple[bool, bool, str, str, str, str]:
        enabled = _env_bool("PHONE_WORKER_ENABLED", False)
        host = str(os.getenv("PHONE_WORKER_HOST") or "").strip()
        port = str(os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
        scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
        name = str(os.getenv("PHONE_WORKER_NAME") or os.getenv("CORE_WORKER_NAME") or "Core Phone Worker").strip() or "Core Phone Worker"
        configured = bool(host)
        return enabled, configured, host, port, scheme, name

    def _request_worker_status_sync(self, *, timeout: float) -> dict[str, Any]:
        enabled, configured, host, port, scheme, _name = self._worker_base_config()
        if not enabled:
            raise RuntimeError("PHONE_WORKER_ENABLED=false")
        if not configured:
            raise RuntimeError("PHONE_WORKER_HOST não configurado")
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{scheme}://{host}:{port}/status", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:240]
            raise RuntimeError(f"HTTP {exc.code}: {_redact(body)}") from exc
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise RuntimeError("resposta não é JSON object")
        return parsed

    def _request_worker_task_sync(self, task: str, payload: dict[str, Any] | None = None, *, timeout: float) -> dict[str, Any]:
        enabled, configured, host, port, scheme, _name = self._worker_base_config()
        if not enabled:
            raise RuntimeError("PHONE_WORKER_ENABLED=false")
        if not configured:
            raise RuntimeError("PHONE_WORKER_HOST não configurado")
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        body = dict(payload or {})
        body["task"] = str(task or "").strip()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{scheme}://{host}:{port}/task", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:1024]
            try:
                parsed_error = json.loads(body_text or "{}")
            except Exception:
                parsed_error = {}
            error_text = str((parsed_error or {}).get("error") or body_text or "erro HTTP").strip()
            if "task não suportada" in error_text.lower() or "task nao suportada" in error_text.lower():
                raise RuntimeError(f"HTTP {exc.code}: worker legado/desatualizado não suporta `{task}`") from exc
            raise RuntimeError(f"HTTP {exc.code}: {_shorten(_redact(error_text), limit=120)}") from exc
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise RuntimeError("resposta não é JSON object")
        if parsed.get("ok") is False:
            parsed.setdefault("summary", _shorten(_redact(parsed.get("error") or "worker retornou erro"), limit=160))
        return parsed

    def _build_worker_update_payload_sync(self, *, scripts_only: bool = False) -> dict[str, Any]:
        src_dir = _repo_root() / "deploy" / "termux" / "phone-worker"
        files: list[dict[str, Any]] = []
        all_targets = (
            ("phone_worker.py", 0o755),
            ("apk_identity.py", 0o644),
            ("music_agent.py", 0o755),
            ("start-phone-worker.sh", 0o755),
            ("start-phone-music-agent.sh", 0o755),
            ("watch-phone-worker.sh", 0o755),
            ("pair-phone-worker.sh", 0o755),
            ("bootstrap-phone-worker.sh", 0o755),
            ("install.sh", 0o755),
            ("README.md", 0o644),
            ("phone-worker.env.example", 0o600),
        )
        script_targets = (
            ("start-phone-worker.sh", 0o755),
            ("start-phone-music-agent.sh", 0o755),
            ("watch-phone-worker.sh", 0o755),
            ("pair-phone-worker.sh", 0o755),
            ("bootstrap-phone-worker.sh", 0o755),
            ("install.sh", 0o755),
        )
        targets = script_targets if scripts_only else all_targets
        version = ""
        version_re = re.compile(r'^PHONE_WORKER_VERSION\s*=\s*["\\\']([^"\\\']+)["\\\']', re.MULTILINE)
        for name, mode in targets:
            path = src_dir / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if name == "phone_worker.py":
                with contextlib.suppress(Exception):
                    match = version_re.search(data.decode("utf-8", errors="ignore"))
                    if match:
                        version = match.group(1)
            files.append({
                "target": name,
                "mode": mode,
                "sha256": hashlib.sha256(data).hexdigest(),
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
        if not files:
            raise RuntimeError("arquivos do phone-worker não encontrados em deploy/termux/phone-worker")
        return {"version": version or "desconhecida", "restart": not scripts_only, "scripts_only": scripts_only, "files": files}

    async def _build_worker_update_payload(self, *, scripts_only: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(self._build_worker_update_payload_sync, scripts_only=scripts_only)

    async def _run_legacy_worker_action(self, *, job_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = str(job_type or "").strip().lower().replace("-", "_")
        if kind in {"ping", "status", "diagnostic_basic", "worker_self_check"}:
            timeout = max(1.0, _env_float("WORKERS_PANEL_STATUS_TIMEOUT_SECONDS", 3.0))
            result = await asyncio.to_thread(self._request_worker_status_sync, timeout=timeout)
            result.setdefault("summary", "status direto coletado")
            return result
        timeout = max(3.0, _env_float("WORKERS_PANEL_DIRECT_TASK_TIMEOUT_SECONDS", 18.0))
        if kind == "apk_build_debug":
            timeout = max(timeout, _env_float("WORKERS_PANEL_DIRECT_APK_BUILD_TIMEOUT_SECONDS", 7200.0))
        elif kind == "worker_update":
            timeout = max(timeout, _env_float("WORKERS_PANEL_DIRECT_UPDATE_TIMEOUT_SECONDS", 60.0))
        direct_tasks = {
            "network_probe",
            "tailscale_status",
            "worker_logs",
            "worker_update",
            "apk_build_debug",
            "apk_publish_last",
            "boot_status",
            "boot_repair",
            "service_status",
            "service_start",
            "service_stop",
            "service_restart",
            "ffmpeg_check",
            "ffprobe_check",
        }
        if kind not in direct_tasks:
            raise RuntimeError("ação direta não suportada pelo phone-worker legado")
        return await asyncio.to_thread(self._request_worker_task_sync, kind, payload or {}, timeout=timeout)

    async def _collect_registry_snapshot(self) -> tuple[dict[str, Any], str]:
        try:
            registry = get_core_workers_registry()
            lock_timeout = max(0.05, _env_float("WORKERS_REGISTRY_SNAPSHOT_LOCK_TIMEOUT_SECONDS", 0.35))
            total_timeout = max(0.2, _env_float("WORKERS_REGISTRY_SNAPSHOT_TOTAL_TIMEOUT_SECONDS", 1.2))
            snapshot = await asyncio.wait_for(
                asyncio.to_thread(registry.snapshot, lock_timeout_seconds=lock_timeout),
                timeout=total_timeout,
            )
            error = str(snapshot.get("error") or "") if isinstance(snapshot, dict) else ""
            return snapshot if isinstance(snapshot, dict) else {}, error
        except asyncio.TimeoutError:
            return {}, "registry_snapshot_timeout"
        except Exception as exc:
            return {}, f"{type(exc).__name__}: {exc}"

    async def _collect_workers_snapshot(self, *, action_note: str = "", watch_output: str = "") -> WorkerSnapshot:
        enabled, configured, host, port, scheme, name = self._worker_base_config()
        timeout = max(0.8, _env_float("WORKERS_PANEL_STATUS_TIMEOUT_SECONDS", _env_float("PHONE_WORKER_QUICK_STATUS_TIMEOUT_SECONDS", 1.6)))
        status: dict[str, Any] = {}
        error = ""
        online = False
        if enabled and configured:
            try:
                status = await asyncio.to_thread(self._request_worker_status_sync, timeout=timeout)
                online = bool(status.get("ok", True))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        registry_snapshot, registry_error = await self._collect_registry_snapshot()
        roles = _parse_roles(os.getenv("PHONE_WORKER_ROLES") or os.getenv("CORE_WORKER_ROLES"), status=status)
        return WorkerSnapshot(
            enabled=enabled,
            configured=configured,
            online=online,
            host=host,
            port=port,
            scheme=scheme,
            name=name,
            roles=roles,
            status=status,
            error=error,
            action_note=action_note,
            watch_output=watch_output,
            registry_snapshot=registry_snapshot,
            registry_error=registry_error,
        )

    async def _create_core_worker_pairing(self, owner: discord.abc.User) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(
            registry.create_pairing,
            created_by_id=int(getattr(owner, "id", 0) or 0),
            created_by_name=str(getattr(owner, "display_name", None) or getattr(owner, "name", "") or ""),
        )

    def _read_core_worker_app_version(self) -> tuple[str, int]:
        build_gradle = _repo_root() / "android" / "core-worker-app" / "app" / "build.gradle"
        text = build_gradle.read_text(encoding="utf-8", errors="ignore") if build_gradle.exists() else ""
        name_match = re.search(r"versionName\s+[\"']([^\"']+)[\"']", text)
        code_match = re.search(r"versionCode\s+(\d+)", text)
        return (name_match.group(1) if name_match else "0.0.0", int(code_match.group(1)) if code_match else 0)


    def _load_google_services_payload_for_apk_build(self) -> dict[str, Any]:
        """Carrega google-services.json local sem colocá-lo no Git ou no ZIP público.

        O source ZIP fica público em /core-worker/app/*.zip para o worker baixar.
        Por isso o Firebase Android config é enviado apenas dentro do payload do
        job, que passa pelo canal autenticado do registry para o phone worker.
        """
        root = _repo_root()
        candidates: list[Path] = []
        for raw in (
            os.getenv("CORE_WORKER_GOOGLE_SERVICES_JSON"),
            os.getenv("CORE_WORKER_FIREBASE_ANDROID_CONFIG"),
            os.getenv("GOOGLE_SERVICES_JSON"),
        ):
            if raw:
                candidates.append(Path(str(raw)).expanduser())
        candidates.append(root / "android" / "core-worker-app" / "app" / "google-services.json")

        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise FileNotFoundError(
                "google-services.json local não encontrado. Coloque em "
                "android/core-worker-app/app/google-services.json na VPS/build env ou defina CORE_WORKER_GOOGLE_SERVICES_JSON."
            )
        raw = path.read_bytes()
        if len(raw) > 512 * 1024:
            raise ValueError("google-services.json grande demais")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"google-services.json inválido: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("google-services.json inválido: raiz não é objeto JSON")
        project_info = data.get("project_info") if isinstance(data.get("project_info"), dict) else {}
        project_id = str(project_info.get("project_id") or "").strip()
        clients = data.get("client") if isinstance(data.get("client"), list) else []
        matched_client: dict[str, Any] | None = None
        for client in clients:
            if not isinstance(client, dict):
                continue
            info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
            android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
            package_name = str(android.get("package_name") or "").strip()
            if package_name == "dev.core.worker":
                matched_client = client
                break
        if not project_id or matched_client is None:
            raise ValueError("google-services.json precisa conter project_id e client Android package_name dev.core.worker")
        info = matched_client.get("client_info") if isinstance(matched_client.get("client_info"), dict) else {}
        mobile_app_id = str(info.get("mobilesdk_app_id") or "").strip()
        api_keys = matched_client.get("api_key") if isinstance(matched_client.get("api_key"), list) else []
        api_key = ""
        for entry in api_keys:
            if isinstance(entry, dict) and str(entry.get("current_key") or "").strip():
                api_key = str(entry.get("current_key") or "").strip()
                break
        if not mobile_app_id or not api_key:
            raise ValueError("google-services.json precisa conter mobilesdk_app_id e api_key para dev.core.worker")
        sha = hashlib.sha256(raw).hexdigest()
        return {
            "googleServicesJsonB64": base64.b64encode(raw).decode("ascii"),
            "googleServicesSha256": sha,
            "googleServicesPackage": "dev.core.worker",
            "googleServicesProjectId": project_id[:80],
            "googleServicesSource": "local-vps-payload",
        }


    def _load_apk_signing_payload_for_worker_build(self) -> dict[str, Any]:
        """Carrega a keystore compatível para o payload autenticado do job.

        Não entra no Git, no ZIP público nem no painel. O phone worker usa essa
        chave só no workspace temporário para permitir update sem desinstalar.
        """
        root = _repo_root()
        candidates: list[Path] = []
        for raw in (
            os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE"),
            os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE"),
            os.getenv("CORE_WORKER_APK_SIGNING_KEYSTORE"),
            os.getenv("CORE_WORKER_APK_KEYSTORE"),
        ):
            if raw:
                candidates.append(Path(str(raw)).expanduser())
        candidates.extend([
            Path("/home/ubuntu/secrets/core-worker-upload.keystore"),
            Path.home() / ".android" / "debug.keystore",
            root / "secrets" / "core-worker-upload.keystore",
        ])
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise FileNotFoundError(
                "keystore compatível não encontrada. Copie a chave antiga para "
                "/home/ubuntu/secrets/core-worker-upload.keystore."
            )
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("keystore compatível grande demais")
        alias = (
            os.getenv("CORE_WORKER_APK_COMPAT_KEY_ALIAS")
            or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_ALIAS")
            or os.getenv("CORE_WORKER_APK_KEY_ALIAS")
            or "androiddebugkey"
        ).strip()
        storepass = (
            os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE_PASSWORD")
            or os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE_PASSWORD")
            or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD")
            or "android"
        ).strip()
        keypass = (
            os.getenv("CORE_WORKER_APK_COMPAT_KEY_PASSWORD")
            or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_PASSWORD")
            or os.getenv("CORE_WORKER_APK_KEY_PASSWORD")
            or storepass
            or "android"
        ).strip()
        if not alias or not storepass:
            raise ValueError("alias/senha da keystore compatível ausentes")
        sha = hashlib.sha256(raw).hexdigest()
        return {
            "apkSigningMode": "compat-vps-debug-keystore",
            "apkSigningKeystoreB64": base64.b64encode(raw).decode("ascii"),
            "apkSigningKeystoreSha256": sha,
            "apkSigningKeyAlias": alias,
            "apkSigningStorePassword": storepass,
            "apkSigningKeyPassword": keypass,
            "apkSigningSource": "local-vps-secret",
        }

    def _prepare_core_worker_source_zip_sync(self) -> dict[str, Any]:
        root = _repo_root()
        project = root / "android" / "core-worker-app"
        if not project.is_dir():
            raise FileNotFoundError(str(project))
        release_dir = project / "releases"
        release_dir.mkdir(parents=True, exist_ok=True)
        zip_path = release_dir / "source-core-worker-app.zip"
        excluded_dirs = {"build", ".gradle", "releases", ".idea"}
        excluded_names = {
            ".env",
            "local.properties",
            "private.properties",
            "vps.properties",
            "google-services.json",
            "firebase-service-account.json",
        }
        excluded_suffixes = (".jks", ".keystore", ".p12", ".pem", ".key")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(project.rglob("*")):
                rel_project = path.relative_to(project)
                if any(part in excluded_dirs for part in rel_project.parts):
                    continue
                if path.is_dir():
                    continue
                name = path.name.lower()
                rel_text = rel_project.as_posix().lower()
                if name in excluded_names or "service-account" in name or rel_text.endswith("/google-services.json"):
                    continue
                if any(name.endswith(suffix) for suffix in excluded_suffixes):
                    continue
                arcname = Path("android/core-worker-app") / rel_project
                already_compressed = path.suffix.lower() in {".zip", ".jar", ".apk", ".so", ".gz", ".xz", ".zst", ".7z"}
                zf.write(
                    path,
                    arcname.as_posix(),
                    compress_type=zipfile.ZIP_STORED if already_compressed else zipfile.ZIP_DEFLATED,
                    compresslevel=None if already_compressed else 6,
                )
        digest = hashlib.sha256()
        source_bytes = 0
        with zip_path.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                source_bytes += len(chunk)
                digest.update(chunk)
        return {
            "path": str(zip_path),
            "filename": zip_path.name,
            "bytes": source_bytes,
            "sha256": digest.hexdigest(),
            "url": f"{_public_base_url()}/core-worker/app/{zip_path.name}",
            "firebase_config_delivery": "job_payload",
        }

    async def _build_apk_builder_payload(self, base_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(base_payload or {})
        source = await asyncio.to_thread(self._prepare_core_worker_source_zip_sync)
        version_name, version_code = self._read_core_worker_app_version()
        firebase_config = self._load_google_services_payload_for_apk_build()
        signing_config = self._load_apk_signing_payload_for_worker_build()
        payload.update({
            "source_zip_url": source["url"],
            "source_sha256": source["sha256"],
            "source_bytes": source["bytes"],
            "firebase_config_delivery": source.get("firebase_config_delivery") or "job_payload",
            **firebase_config,
            **signing_config,
            "project_subdir": "android/core-worker-app",
            "selfBuilderRequired": True,
            "publish": True,
            "versionName": version_name,
            "versionCode": version_code,
            "filename": f"CoreWorker-v{version_name}-debug.apk",
            "coreWorkerVpsUrl": _public_base_url(),
            "coreWorkerVpsLabel": f"VPS privada · {_host_label(_public_base_url().replace('http://', '').replace('https://', '').split(':')[0])}:" + str(os.getenv("CORE_WORKER_PUBLIC_PORT") or os.getenv("PORT") or "10000"),
            "changelog": ["APK bootstrap compilado no Termux ou atualização compilada pelo próprio APK", "VPS apenas publicou o APK pronto", "Toolchain interno validado para os próximos autobuilds"],
        })
        return payload

    async def _queue_core_worker_job(
        self,
        owner: discord.abc.User,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
        target_worker_id: str = "",
    ) -> dict[str, Any]:
        registry = get_core_workers_registry()
        task = _task_name(job_type)
        is_apk_build = task in {"apk_build_debug", "apk_publish_last"}
        assist_tasks = {"vps_assist_probe", "hash_batch", "endpoint_probe", "media_probe", "audio_convert", "log_digest", "zip_audit", "maintenance_plan"}
        # Não use uma capacidade genérica como trava para todos os assist jobs.
        # O registry já valida supported_tasks/target. Isso evita oferecer uma ação
        # compatível no painel e falhar depois só porque o worker não tinha
        # `vps-assist` escrito nas capabilities antigas.
        required_caps = ["apk-builder"] if is_apk_build else []
        ttl = 7200 if is_apk_build else (1200 if task in assist_tasks or task in {"log_summary", "maintenance_plan", "zip_validate"} else 900)
        lease = 7200 if is_apk_build else (240 if task in assist_tasks or task in {"log_summary", "maintenance_plan", "zip_validate"} else 120)
        return await asyncio.to_thread(
            registry.create_job,
            job_type=job_type,
            payload=payload or {},
            created_by_id=int(getattr(owner, "id", 0) or 0),
            created_by_name=str(getattr(owner, "display_name", None) or getattr(owner, "name", "") or ""),
            target_worker_id=target_worker_id,
            required_capabilities=required_caps,
            ttl_seconds=ttl,
            lease_seconds=lease,
            max_attempts=1 if is_apk_build else 2,
            summary=summary or job_type,
        )

    async def _get_core_worker_job(self, job_id: str) -> dict[str, Any] | None:
        if not job_id:
            return None
        registry = get_core_workers_registry()
        try:
            result = await asyncio.to_thread(registry.get_job, job_id)
        except Exception:
            return None
        job = result.get("job") if isinstance(result, dict) else None
        return job if isinstance(job, dict) else None

    async def _wait_core_worker_job(self, job_id: str, *, timeout: float = 7.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.5, float(timeout or 0))
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            job = await self._get_core_worker_job(job_id)
            if isinstance(job, dict):
                last = job
                if str(job.get("status") or "").lower() in {"succeeded", "failed", "expired"}:
                    return job
            await asyncio.sleep(0.8)
        return last

    async def _latest_core_worker_job(self, worker_id: str) -> dict[str, Any] | None:
        registry = get_core_workers_registry()
        result = await asyncio.to_thread(registry.latest_job_for_worker, worker_id)
        job = result.get("job") if isinstance(result, dict) else None
        return job if isinstance(job, dict) else None

    async def _latest_legacy_worker_result(self) -> dict[str, Any] | None:
        status = await asyncio.to_thread(
            self._request_worker_status_sync,
            timeout=max(1.0, _env_float("WORKERS_PANEL_STATUS_TIMEOUT_SECONDS", 3.0)),
        )
        queue = status.get("core_worker_jobs") if isinstance(status.get("core_worker_jobs"), dict) else {}
        job_id = str(queue.get("last_result_job_id") or "")
        kind = str(queue.get("last_result_type") or "phone_worker_direct")
        raw_status = str(queue.get("last_result_status") or "succeeded").lower()
        summary = str(queue.get("last_result_summary") or status.get("summary") or "resultado direto do phone-worker")
        if not job_id and not summary:
            return None
        return {
            "job_id": job_id or "direct-last-result",
            "type": kind,
            "status": raw_status if raw_status in {"succeeded", "failed"} else "succeeded",
            "worker_id": str(status.get("worker_id") or status.get("id") or "phone-worker-direto"),
            "summary": summary,
            "finished_at": queue.get("last_result_at") or time.time(),
            "result": {
                "ok": raw_status != "failed",
                "summary": summary,
                "sent_ok": bool(queue.get("last_result_sent")),
                "pending_results": queue.get("pending_results"),
                "source": "phone-worker-direct-status",
            },
        }

    async def _rename_core_worker(self, worker_id: str, name: str) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.rename_worker, worker_id, name)

    async def _update_core_worker_roles(self, worker_id: str, roles: str, capabilities: str = "", supported_tasks: str = "") -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.update_worker_roles, worker_id, roles, capabilities or None, supported_tasks or None)

    async def _set_core_worker_enabled(self, worker_id: str, *, enabled: bool) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.set_worker_enabled, worker_id, enabled)

    async def _delete_core_worker(self, worker_id: str) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.delete_worker, worker_id, only_offline=True)

    async def _cleanup_core_worker_jobs(self) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.cleanup_jobs, clear_active=True)

    def _get_core_worker_wake_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_core_worker_wake_lock", None)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            setattr(self, "_core_worker_wake_lock", lock)
        return lock

    async def _run_phone_worker_watch_script(self, *, force: bool = False, reason: str = "manual") -> dict[str, Any]:
        script = _repo_root() / "scripts" / "phone-worker-watch.sh"
        if not script.exists():
            return {"ok": False, "missing": True, "output": "watchdog não encontrado em scripts/phone-worker-watch.sh"}

        timeout = max(5.0, _env_float("WORKERS_PANEL_WAKE_TIMEOUT_SECONDS", 28.0))
        started = time.perf_counter()
        proc: asyncio.subprocess.Process | None = None
        env = dict(os.environ)
        env["PHONE_WORKER_WATCH_REASON"] = str(reason or "manual")[:32]
        if force:
            env["PHONE_WORKER_FORCE_WAKE"] = "1"
            # Botão manual não pode ficar preso no cooldown do timer.
            env["PHONE_WORKER_KICK_COOLDOWN_SECONDS"] = "0"
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(script),
                cwd=str(_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            elapsed = time.perf_counter() - started
            output_text = (stdout or b"").decode("utf-8", errors="replace").strip()
            error_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            merged = " | ".join(part for part in [output_text, error_text] if part)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "elapsed_seconds": elapsed,
                "output": merged or "sem saída",
                "timeout": False,
            }
        except asyncio.TimeoutError:
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.communicate()
            return {"ok": False, "timeout": True, "elapsed_seconds": time.perf_counter() - started, "output": f"watchdog excedeu {timeout:.0f}s"}
        except Exception as exc:
            return {"ok": False, "elapsed_seconds": time.perf_counter() - started, "output": f"watchdog falhou: {_compact_failure(exc)}"}

    async def _wake_phone_worker(self, *, force: bool = False, reason: str = "manual") -> WorkerSnapshot:
        lock = self._get_core_worker_wake_lock()
        if lock.locked() and not force:
            return await self._collect_workers_snapshot(action_note="auto-wake ignorado: já existe tentativa de wake em andamento")
        async with lock:
            before = await self._collect_workers_snapshot()
            if _snapshot_has_online_worker(before):
                return await self._collect_workers_snapshot(action_note=_wake_attempt_note(before=before, after=before, attempt={}, reason=reason))
            attempt = await self._run_phone_worker_watch_script(force=force, reason=reason)
            confirm_deadline = time.monotonic() + max(0.0, _env_float("CORE_WORKER_WAKE_CONFIRM_SECONDS", 8.0))
            after = await self._collect_workers_snapshot(watch_output=str(attempt.get("output") or ""))
            while not _snapshot_has_online_worker(after) and time.monotonic() < confirm_deadline:
                await asyncio.sleep(1.5)
                after = await self._collect_workers_snapshot(watch_output=str(attempt.get("output") or ""))
            note = _wake_attempt_note(before=before, after=after, attempt=attempt, reason=reason)
            return await self._collect_workers_snapshot(action_note=note, watch_output=str(attempt.get("output") or ""))

    async def _sync_phone_worker(self) -> WorkerSnapshot:
        script = _repo_root() / "scripts" / "sync-phone-worker.sh"
        if not script.exists():
            return await self._collect_workers_snapshot(action_note="sync não encontrado em scripts/sync-phone-worker.sh")

        timeout = max(15.0, _env_float("WORKERS_PANEL_SYNC_TIMEOUT_SECONDS", 75.0))
        started = time.perf_counter()
        note = "sync executado"
        output = ""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(script),
                cwd=str(_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            elapsed = time.perf_counter() - started
            out = (stdout or b"").decode("utf-8", errors="replace").strip()
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            merged = " | ".join(part for part in [out, err] if part)
            output = merged or "sem saída"
            if proc.returncode == 0:
                note = f"phone-worker atualizado em {elapsed:.1f}s"
            else:
                note = f"sync retornou código {proc.returncode} em {elapsed:.1f}s"
        except asyncio.TimeoutError:
            note = f"sync excedeu {timeout:.0f}s"
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
        except Exception as exc:
            note = f"sync falhou: {_compact_failure(exc)}"
        return await self._collect_workers_snapshot(action_note=note, watch_output=output)

    def _snapshot_needs_auto_wake(self, snapshot: WorkerSnapshot) -> bool:
        if not _env_bool("CORE_WORKER_AUTO_WAKE_ENABLED", True):
            return False
        if not snapshot.enabled or not snapshot.configured:
            return False
        if _env_bool("CORE_WORKER_AUTO_WAKE_SKIP_WHEN_APK_ONLINE", True) and _core_worker_any_apk_runtime_online():
            return False
        if _snapshot_has_online_worker(snapshot):
            return False
        offline_important = _offline_important_worker_labels(snapshot)
        if offline_important:
            return True
        registry = snapshot.registry_snapshot if isinstance(snapshot.registry_snapshot, dict) else {}
        workers = registry.get("workers") if isinstance(registry.get("workers"), list) else []
        # Sem registry pareado ainda, o worker legado configurado também merece tentativa.
        return not workers

    async def _core_worker_auto_wake_loop(self) -> None:
        with contextlib.suppress(Exception):
            await self.bot.wait_until_ready()
        last_apk_skip_log = 0.0
        while not getattr(self.bot, "is_closed", lambda: False)():
            interval = max(30.0, _env_float("CORE_WORKER_AUTO_WAKE_INTERVAL_SECONDS", CORE_WORKER_AUTO_WAKE_DEFAULT_INTERVAL_SECONDS))
            try:
                # Caminho rápido: se o APK/Core Linux interno já está online, não
                # coleta snapshot pesado e não tenta acordar o Termux legado.
                if _env_bool("CORE_WORKER_AUTO_WAKE_SKIP_WHEN_APK_ONLINE", True) and _core_worker_any_apk_runtime_online():
                    now = time.monotonic()
                    log_interval = max(60.0, _env_float("CORE_WORKER_AUTO_WAKE_APK_SKIP_LOG_INTERVAL_SECONDS", CORE_WORKER_AUTO_WAKE_APK_SKIP_LOG_INTERVAL_SECONDS))
                    if now - last_apk_skip_log >= log_interval:
                        print("[core-worker-auto-wake] ignorado: APK/Core Linux interno online", flush=True)
                        last_apk_skip_log = now
                    await asyncio.sleep(interval)
                    continue

                snapshot = await self._collect_workers_snapshot()
                if self._snapshot_needs_auto_wake(snapshot):
                    labels = _offline_important_worker_labels(snapshot)
                    label = ", ".join(labels[:3]) if labels else "phone-worker configurado"
                    print(f"[core-worker-auto-wake] tentando acordar: {label}", flush=True)
                    await self._wake_phone_worker(force=False, reason="auto")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[core-worker-auto-wake] falha: {_compact_failure(exc)}", flush=True)
            await asyncio.sleep(interval)

    def _start_core_worker_auto_wake_task(self) -> None:
        if not _env_bool("CORE_WORKER_AUTO_WAKE_ENABLED", True):
            return
        task = getattr(self, "_core_worker_auto_wake_task", None)
        if task is not None and not task.done():
            return
        self._core_worker_wake_lock = asyncio.Lock()
        try:
            self._core_worker_auto_wake_task = asyncio.create_task(self._core_worker_auto_wake_loop())
        except RuntimeError:
            # Em testes/imports sem loop rodando, não derruba a cog.
            # Ao carregar pelo discord.py normalmente já existe loop ativo.
            self._core_worker_auto_wake_task = None

    def _stop_core_worker_auto_wake_task(self) -> None:
        task = getattr(self, "_core_worker_auto_wake_task", None)
        if task is not None:
            task.cancel()
        self._core_worker_auto_wake_task = None

    async def _send_workers_panel_from_context(self, ctx: commands.Context) -> None:
        if ctx.guild is None or int(getattr(ctx.guild, "id", 0) or 0) != WORKERS_COMMAND_GUILD_ID:
            with contextlib.suppress(Exception):
                await ctx.message.delete()
            return
        if not await self._can_use_workers_author(ctx.author):
            with contextlib.suppress(Exception):
                await ctx.message.delete()
            return

        with contextlib.suppress(Exception):
            await ctx.message.delete()

        snapshot = await self._collect_workers_snapshot()
        view = WorkersPanelView(self, owner_id=int(ctx.author.id), snapshot=snapshot)
        message = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        view.message = message

    @commands.command(name="workers", aliases=["worker", "w"], hidden=True)
    async def workers(self, ctx: commands.Context):
        await self._send_workers_panel_from_context(ctx)
