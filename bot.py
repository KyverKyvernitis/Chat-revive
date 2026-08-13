import asyncio
import atexit
import base64
from collections import deque
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

# -----------------------------------------------------------------------------
# Logging — precisa vir ANTES de qualquer import do discord para capturar os
# logs de inicialização da biblioteca (gateway/voice/cogs).
# Nível geral INFO; libs barulhentas (discord.gateway / discord.voice_client)
# são rebaixadas para WARNING para não poluir.
# -----------------------------------------------------------------------------
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

class _LowValueNoiseFilter(logging.Filter):
    """Remove ruído conhecido sem esconder erro real do bot.

    O bot roda em VPS pequena. Logs de heartbeat do discord.py podem trazer stack
    inteira e repetir várias vezes durante a mesma travada; isso aumenta I/O no
    journald justamente quando o event loop já está atrasado. Mantemos o primeiro
    aviso e aplicamos cooldown nos repetidos.
    """

    _VOICE_LOGGERS = ("discord.voice", "discord.gateway", "discord.player")
    _EXPECTED_MUSIC_CANCELS = (
        "MusicPlaybackError: Música pulada antes de iniciar o áudio.",
        "MusicPlaybackError: Playback cancelado.",
    )

    def __init__(self) -> None:
        super().__init__()
        self._last_by_key: dict[str, float] = {}
        self._lock = threading.Lock()

    def _allow_every(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_by_key.get(key, 0.0)
            if now - last < seconds:
                return False
            self._last_by_key[key] = now
            return True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            logger_name = str(record.name or "")
            message = record.getMessage()
            lowered = message.lower()

            if logger_name.startswith("discord.gateway") and (
                "heartbeat blocked" in lowered or "loop thread traceback" in lowered
            ):
                cooldown = max(15.0, float(os.getenv("DISCORD_GATEWAY_HEARTBEAT_LOG_COOLDOWN_SECONDS", "60") or 60))
                if not self._allow_every("discord.gateway.heartbeat_blocked", cooldown):
                    return False

            if logger_name.startswith(self._VOICE_LOGGERS):
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                combined = f"{message}\n{exc_text}".lower()
                if "1006" in combined and ("websocket" in combined or "voice" in combined or "closed" in combined):
                    cooldown = max(5.0, float(os.getenv("DISCORD_VOICE_1006_LOG_COOLDOWN_SECONDS", "60") or 60))
                    return self._allow_every("discord.voice.close_1006", cooldown)

            if logger_name == "asyncio" and "exception was never retrieved" in lowered:
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                if any(marker in exc_text for marker in self._EXPECTED_MUSIC_CANCELS):
                    return False
        except Exception:
            return True
        return True


class _FastQueueHandler(logging.handlers.QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler padrão formata exc_info/stack antes de enfileirar. Como o
        # listener roda no mesmo processo, podemos atrasar essa formatação para a
        # thread de logging e manter o event loop leve.
        return record


def _configure_async_logging() -> logging.handlers.QueueListener:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "bot.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = _FastQueueHandler(log_queue)
    queue_handler.addFilter(_LowValueNoiseFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(queue_handler)

    listener = logging.handlers.QueueListener(
        log_queue,
        stream_handler,
        file_handler,
        respect_handler_level=True,
    )
    listener.start()
    atexit.register(listener.stop)
    return listener


_LOG_LISTENER = _configure_async_logging()
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.WARNING)
logging.getLogger("discord.player").setLevel(logging.WARNING)

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver, set_health_provider, set_update_action_provider
from music_system import AudioRouter
from utility.interaction_safety import is_unknown_interaction, safe_send_interaction_message
from utility.update_security import (
    UpdateSecurityError,
    ZipLimits,
    build_file_integrity,
    canonical_path_key,
    inspect_zip_archive,
    is_forbidden_update_path,
    sha256_file,
)
from utility.application_bio import ApplicationBioService
from utility.application_presence import ApplicationPresenceService


BOOT_LOG = logging.getLogger("bot.boot")
COG_LOG = logging.getLogger("bot.cogs")
UPDATE_LOG = logging.getLogger("zip_update")
ASYNCIO_LOG = logging.getLogger("bot.asyncio")

print("BOT.PY INICIOU")



REMOVED_SLASH_COMMANDS = {
    "form_config",
    "form_customizar",
    "form_repostar",
    "form_reset",
    "form_status",
    # O painel técnico de TTS foi movido para /vps > TTS.
    "health",
    # Core Workers agora é comando privado de prefixo: workers/worker/w.
    "workers",
}

TRUTHY_VALUES = {"1", "true", "yes", "y", "on", "sim", "s"}

# Cogs que não devem ser carregadas automaticamente pelo varredor genérico.
# `cogs.tts` é carregado por módulos explícitos mais abaixo; carregar o pacote
# inteiro também criaria ambiguidade/duplicidade.
SKIPPED_COG_FILES = {"voice_moderation"}
SKIPPED_COG_PACKAGES = {"tts", "gincana"}
COG_EXTENSION_ALIASES = {"cogs.gincana": "cogs.games"}
EXPLICIT_COG_EXTENSIONS = (
    "cogs.tts.cog",
    "cogs.tts.toggle",
)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY_VALUES


def _csv_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _normalize_extension_name(value: str) -> str:
    value = str(value or "").strip().replace("/", ".").replace("\\", ".")
    if value.endswith(".py"):
        value = value[:-3]
    value = value.strip(".")
    if not value:
        return ""
    if not value.startswith("cogs."):
        value = f"cogs.{value}"
    return COG_EXTENSION_ALIASES.get(value, value)


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


class BotLocal(commands.Bot):
    ZIP_UPDATE_CHANNEL_ID = 1490093068706386131

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        intents.messages = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(
                getattr(config, "BOT_PREFIX", "_"),
                getattr(config, "PREFIX", "_"),
            ),
            intents=intents,
            help_command=None,
        )

        self.started_at = datetime.now(timezone.utc)
        self.settings_db: SettingsDB | None = None
        self.health_state: dict[str, object] = {
            "status": "starting",
            "healthy": True,
            "starting": True,
            "discord_ready": False,
            "discord_closed": False,
            "guild_count": 0,
            "latency_ms": None,
            "mongo_ok": False,
            "mongo_error": None,
            "last_update": None,
        }
        self._health_task: asyncio.Task | None = None
        self._event_loop_watchdog_task: asyncio.Task | None = None
        self._event_loop_last_lag_ms: float = 0.0
        self._event_loop_max_lag_ms: float = 0.0
        self._event_loop_lag_warnings: int = 0
        self._event_loop_last_warning_at: float = 0.0
        self._event_loop_last_diagnostic_at: float = 0.0
        self._event_loop_last_task_summary: str = ""
        self._event_loop_recent_lags_ms: deque[float] = deque(maxlen=12)
        self._zip_update_lock = asyncio.Lock()
        self._zip_update_reconcile_task: asyncio.Task | None = None
        self._phone_worker_unavailable_until_by_task: dict[str, float] = {}
        self._phone_worker_unavailable_last_log_by_task: dict[str, float] = {}
        self._repo_root = Path(__file__).resolve().parent
        self._update_temp_root = Path("/tmp/discord-auto-update")
        self._update_staging_root = Path(os.getenv("DISCORD_AUTO_UPDATE_STAGING_DIR", str(self._repo_root.parent / "bot-update-staging")))
        self._zip_rollback_state_path = self._update_staging_root / "zip_update_rollback_state.json"
        self._app_command_manifest_path = self._repo_root / "data" / "app_commands_manifest.json"
        self._app_command_sync_status_path = self._repo_root / "data" / "app_commands_sync_status.json"
        self._removed_slash_cleanup_state_path = self._repo_root / "data" / "removed_slash_cleanup_state.json"
        self.audio_router = AudioRouter(self)
        self.application_bio = ApplicationBioService(self, self._repo_root / "data" / "application_bio.json")
        self.application_presence = ApplicationPresenceService(
            self,
            self._update_staging_root / "candidates" / "runtime-state.json",
            self._repo_root / "data" / "application_presence.json",
        )
        self._update_notice_by_user: dict[int, float] = {}
        self._music_bitrate_reconciled = False
        self._music_voice_status_reconciled = False
        self._music_startup_reconcile_task: asyncio.Task | None = None

        self.loaded_extensions: list[str] = []
        self.failed_extensions: dict[str, dict[str, object]] = {}
        self.skipped_extensions: dict[str, str] = {}
        self.critical_extensions: set[str] = self._read_critical_extensions()
        self.cog_loading_finished = False

        set_health_provider(self.get_health_snapshot)
        set_update_action_provider(self.handle_internal_update_action)

    def _read_critical_extensions(self) -> set[str]:
        """Lê a lista de cogs realmente críticas.

        Por padrão, nenhuma feature cog derruba o bot inteiro. Se algum sistema
        base virar indispensável, ele pode ser listado em `CRITICAL_COGS` no
        config.py ou em `BOT_CRITICAL_COGS` no .env/ambiente.
        """
        values: list[str] = []
        values.extend(_csv_values(getattr(config, "CRITICAL_COGS", None)))
        values.extend(_csv_values(getattr(config, "BOT_CRITICAL_COGS", None)))
        values.extend(_csv_values(os.getenv("BOT_CRITICAL_COGS")))
        normalized = {_normalize_extension_name(item) for item in values}
        normalized.discard("")
        return normalized

    def _is_critical_extension(self, extension: str) -> bool:
        return _normalize_extension_name(extension) in self.critical_extensions

    def _extension_error_summary(self, exc: BaseException) -> str:
        root = getattr(exc, "original", None) or getattr(exc, "__cause__", None) or exc
        root_type = type(root).__name__
        root_message = str(root).strip()
        if not root_message:
            root_message = repr(root)
        return f"{root_type}: {root_message}"

    def _record_failed_extension(self, extension: str, exc: BaseException) -> None:
        summary = self._extension_error_summary(exc)
        root = getattr(exc, "original", None) or getattr(exc, "__cause__", None) or exc
        self.failed_extensions[extension] = {
            "summary": summary,
            "exception_type": type(exc).__name__,
            "root_type": type(root).__name__,
            "critical": self._is_critical_extension(extension),
        }

    def _extension_has_setup_entrypoint(self, init_py: Path) -> bool:
        try:
            source = init_py.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            COG_LOG.warning("[cogs] não consegui ler %s: %r", init_py, exc)
            return False
        return bool(re.search(r"^\s*(async\s+def|def)\s+setup\s*\(", source, flags=re.MULTILINE))

    def _discover_cog_extensions(self) -> list[str]:
        cogs_dir = self._repo_root / "cogs"
        discovered: list[str] = []
        seen: set[str] = set()

        def add_extension(extension: str) -> None:
            extension = _normalize_extension_name(extension)
            if not extension or extension in seen:
                return
            seen.add(extension)
            discovered.append(extension)

        if not cogs_dir.is_dir():
            COG_LOG.warning("[cogs] pasta não encontrada: %s", cogs_dir)
            return []

        for entry in sorted(cogs_dir.iterdir(), key=lambda item: item.name.casefold()):
            name = entry.name
            if name.startswith("_"):
                continue

            if entry.is_file() and entry.suffix == ".py":
                module_name = entry.stem
                if module_name in SKIPPED_COG_FILES:
                    self.skipped_extensions[f"cogs.{module_name}"] = "ignorada pelo loader"
                    continue
                add_extension(f"cogs.{module_name}")
                continue

            if entry.is_dir():
                if name in SKIPPED_COG_PACKAGES:
                    self.skipped_extensions[f"cogs.{name}"] = "carregada por módulos explícitos"
                    continue
                init_py = entry / "__init__.py"
                if not init_py.is_file():
                    continue
                if self._extension_has_setup_entrypoint(init_py):
                    add_extension(f"cogs.{name}")

        for extension in EXPLICIT_COG_EXTENSIONS:
            add_extension(extension)

        return discovered

    async def _load_extension_safely(self, extension: str) -> bool:
        extension = _normalize_extension_name(extension)
        try:
            await self.load_extension(extension)
        except commands.ExtensionAlreadyLoaded:
            COG_LOG.info("[cogs] já estava carregada: %s", extension)
            if extension not in self.loaded_extensions:
                self.loaded_extensions.append(extension)
            return True
        except Exception as exc:
            self._record_failed_extension(extension, exc)
            summary = self._extension_error_summary(exc)
            if self._is_critical_extension(extension):
                COG_LOG.exception(
                    "[cogs] cog crítica falhou ao carregar: %s | %s",
                    extension,
                    summary,
                )
                raise
            COG_LOG.error(
                "[cogs] %s falhou ao carregar, mas o bot continuará online. Erro: %s",
                extension,
                summary,
            )
            return False

        self.loaded_extensions.append(extension)
        COG_LOG.info("[cogs] carregada: %s", extension)
        return True

    async def _load_cogs_safely(self) -> None:
        extensions = self._discover_cog_extensions()
        COG_LOG.info("[cogs] preparando %s extensão(ões)", len(extensions))
        for extension in extensions:
            await self._load_extension_safely(extension)
        self.cog_loading_finished = True

        failed = len(self.failed_extensions)
        loaded = len(self.loaded_extensions)
        if failed:
            COG_LOG.warning(
                "[cogs] boot continuou com aviso: %s carregada(s), %s com falha",
                loaded,
                failed,
            )
        else:
            COG_LOG.info("[cogs] todas carregadas: %s", loaded)

    async def _cleanup_removed_slash_commands(self, guild_ids: set[int]) -> None:
        """Remove comandos slash antigos sem tocar em comandos de outras cogs."""
        names = REMOVED_SLASH_COMMANDS

        async def delete_matching(scope: str, *, guild: discord.Object | None = None) -> None:
            try:
                commands_found = await self.tree.fetch_commands(guild=guild)
            except Exception as e:
                print(f"[SYNC][{scope}] não consegui buscar comandos pra limpar slash antigos: {e}")
                return

            for cmd in commands_found:
                name = str(getattr(cmd, "name", "") or "")
                if name not in names:
                    continue
                try:
                    await cmd.delete()
                    print(f"[SYNC][{scope}] removido comando antigo: /{name}")
                except Exception as e:
                    print(f"[SYNC][{scope}] falha ao remover /{name}: {e}")

        await delete_matching("GLOBAL")
        for guild_id in sorted(guild_ids):
            await delete_matching(
                f"GUILD {guild_id}",
                guild=discord.Object(id=int(guild_id)),
            )

    def _json_safe_value(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._json_safe_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe_value(v) for v in value]
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int, float, bool)):
            return enum_value
        enum_name = getattr(value, "name", None)
        if isinstance(enum_name, str):
            return enum_name
        return str(value)

    def _app_command_to_dict(self, command) -> dict[str, object]:
        data = None
        to_dict = getattr(command, "to_dict", None)
        if callable(to_dict):
            for args in ((self.tree,), tuple()):
                try:
                    data = to_dict(*args)
                    break
                except TypeError:
                    continue
                except Exception:
                    data = None
                    break
        if not isinstance(data, dict):
            data = {
                "name": getattr(command, "name", ""),
                "description": getattr(command, "description", ""),
                "type": self._json_safe_value(getattr(command, "type", None)),
            }
            children = list(getattr(command, "commands", []) or [])
            if children:
                data["options"] = [self._app_command_to_dict(child) for child in children]
            params = list(getattr(command, "parameters", []) or [])
            if params:
                data["parameters"] = [
                    {
                        "name": getattr(param, "name", ""),
                        "description": getattr(param, "description", ""),
                        "required": getattr(param, "required", None),
                        "type": self._json_safe_value(getattr(param, "type", None)),
                        "choices": self._json_safe_value(getattr(param, "choices", None)),
                        "autocomplete": getattr(param, "autocomplete", None),
                    }
                    for param in params
                ]
        return self._json_safe_value(data)

    def _app_command_labels_from_dict(self, command_data: dict[str, object], prefix: str = "") -> list[str]:
        name = str(command_data.get("name") or "").strip()
        current = f"{prefix} {name}".strip()
        labels = [f"/{current}"] if current else []
        options = command_data.get("options") or command_data.get("parameters") or []
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                # Discord slash subcommand/subcommand group option types are 1/2.
                option_type = option.get("type")
                if str(option_type) in {"1", "2", "subcommand", "sub_command", "subcommand_group", "sub_command_group"}:
                    labels.extend(self._app_command_labels_from_dict(option, current))
        return labels

    def _build_app_commands_manifest(self, guild_ids: set[int]) -> dict[str, object]:
        scopes: list[dict[str, object]] = []
        all_labels: set[str] = set()

        def add_scope(scope: str, commands_list) -> None:
            entries = [self._app_command_to_dict(cmd) for cmd in commands_list]
            entries.sort(key=lambda item: (str(item.get("type", "")), str(item.get("name", ""))))
            for entry in entries:
                all_labels.update(self._app_command_labels_from_dict(entry))
            scopes.append({"scope": scope, "commands": entries})

        try:
            add_scope("global", list(self.tree.get_commands()))
        except Exception as exc:
            scopes.append({"scope": "global", "error": f"{type(exc).__name__}: {exc}", "commands": []})

        for guild_id in sorted(guild_ids):
            guild_obj = discord.Object(id=int(guild_id))
            try:
                add_scope(f"guild:{guild_id}", list(self.tree.get_commands(guild=guild_obj)))
            except Exception as exc:
                scopes.append({"scope": f"guild:{guild_id}", "error": f"{type(exc).__name__}: {exc}", "commands": []})

        body = {
            "version": 1,
            "scopes": scopes,
            "labels": sorted(all_labels),
        }
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            **body,
            "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_previous_app_commands_manifest(self) -> dict[str, object] | None:
        try:
            return json.loads(self._app_command_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _app_command_manifest_diff(self, previous: dict[str, object] | None, current: dict[str, object]) -> dict[str, object]:
        current_labels = set(current.get("labels") or [])
        if not isinstance(previous, dict):
            # Primeiro boot com o manifest novo: sincroniza e cria baseline, mas
            # não trata todos os comandos existentes como “adicionados” no log.
            return {
                "added": [],
                "removed": [],
                "previous_hash": "",
                "current_hash": str(current.get("hash") or ""),
            }
        previous_labels = set(previous.get("labels") or [])
        return {
            "added": sorted(current_labels - previous_labels),
            "removed": sorted(previous_labels - current_labels),
            "previous_hash": str(previous.get("hash") or ""),
            "current_hash": str(current.get("hash") or ""),
        }

    def _write_app_command_sync_status(self, status: dict[str, object]) -> None:
        try:
            self._app_command_sync_status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._app_command_sync_status_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._app_command_sync_status_path)
        except Exception:
            BOOT_LOG.warning("[SYNC] falha ao salvar status de comandos", exc_info=True)

    def _save_app_commands_manifest(self, manifest: dict[str, object]) -> None:
        self._app_command_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._app_command_manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._app_command_manifest_path)

    def _removed_slash_cleanup_signature(self, guild_ids: set[int]) -> str:
        payload = {
            "names": sorted(REMOVED_SLASH_COMMANDS),
            "guild_ids": sorted(int(gid) for gid in guild_ids),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    async def _cleanup_removed_slash_commands_if_needed(self, guild_ids: set[int]) -> None:
        signature = self._removed_slash_cleanup_signature(guild_ids)
        try:
            state = json.loads(self._removed_slash_cleanup_state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("signature") == signature:
            print("[SYNC] limpeza de slash antigos já conferida; pulando.")
            return
        await self._cleanup_removed_slash_commands(guild_ids)
        try:
            self._removed_slash_cleanup_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._removed_slash_cleanup_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"signature": signature, "updated_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._removed_slash_cleanup_state_path)
        except Exception:
            BOOT_LOG.warning("[SYNC] falha ao salvar marcador de limpeza slash", exc_info=True)

    def _resolve_app_command_sync_guild_ids(self) -> set[int]:
        health_guild_id = 927002914449424404
        guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
        guild_ids.add(health_guild_id)

        return guild_ids

    def _app_command_sync_enabled(self) -> bool:
        if os.getenv("APP_COMMAND_SYNC_ENABLED") is not None:
            return _env_truthy("APP_COMMAND_SYNC_ENABLED")
        return _env_truthy("SYNC_SLASH_COMMANDS")

    def _app_command_global_sync_allowed(self) -> bool:
        scope = str(os.getenv("APP_COMMAND_SYNC_SCOPE", "") or "").strip().lower()
        if scope in {"global", "globals", "public", "production"}:
            return True
        if scope in {"guild", "guilds", "server", "servers"}:
            return False
        return _env_truthy("SYNC_GLOBAL_SLASH_COMMANDS")

    async def _smart_sync_app_commands(self, guild_ids: set[int], *, should_sync: bool, allow_global_sync: bool, clear_globals_allowed: bool, trigger: str = "boot") -> dict[str, object]:
        current_manifest = self._build_app_commands_manifest(guild_ids)
        previous_manifest = self._load_previous_app_commands_manifest()
        diff = self._app_command_manifest_diff(previous_manifest, current_manifest)
        manifest_changed = previous_manifest is None or diff["previous_hash"] != diff["current_hash"]
        status: dict[str, object] = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "manifest_changed": manifest_changed,
            "sync_enabled": bool(should_sync),
            "global_sync_allowed": bool(allow_global_sync),
            "clear_global_allowed": bool(clear_globals_allowed),
            "sync_performed": False,
            "clear_performed": False,
            "added": diff["added"],
            "removed": diff["removed"],
            "previous_hash": diff["previous_hash"],
            "current_hash": diff["current_hash"],
            "reason": "manifest_changed" if manifest_changed else "unchanged",
            "trigger": trigger,
        }

        if not should_sync:
            status["reason"] = "disabled_by_env"
            self._write_app_command_sync_status(status)
            print(f"[SYNC] Pulado ({trigger}): sync de slash commands desativado")
            return status

        if not manifest_changed:
            status["reason"] = "unchanged"
            self._write_app_command_sync_status(status)
            print(f"[SYNC] Manifest de slash commands sem mudanças ({trigger}); sync/clear global pulados.")
            return status

        try:
            if allow_global_sync:
                synced_global = await self.tree.sync()
                status["sync_performed"] = True
                status["global_count"] = len(synced_global)
                print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced_global)}")
                for cmd in synced_global:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GLOBAL] /{name}")
                for guild_id in sorted(guild_ids):
                    guild_obj = discord.Object(id=guild_id)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    print(f"[SYNC] Comandos guild-specific sincronizados na guild {guild_id}: {len(synced_guild)}")
                    for cmd in synced_guild:
                        name = getattr(cmd, "name", None) or str(cmd)
                        print(f"[SYNC][GUILD {guild_id}] /{name}")
            else:
                if clear_globals_allowed and diff["removed"]:
                    try:
                        existing_globals = await self.tree.fetch_commands()
                    except Exception as e:
                        print(f"[SYNC] Não consegui buscar comandos globais: {e}")
                        existing_globals = []
                    removed_names = {label.lstrip('/').split()[0] for label in diff["removed"]}
                    deleted = 0
                    preserved = 0
                    for cmd in existing_globals:
                        cmd_type = getattr(cmd, "type", None)
                        type_value = getattr(cmd_type, "value", cmd_type)
                        if type_value == 4:
                            preserved += 1
                            print(f"[SYNC][GLOBAL] preservado Entry Point: /{cmd.name}")
                            continue
                        if getattr(cmd, "name", "") not in removed_names:
                            continue
                        try:
                            await cmd.delete()
                            deleted += 1
                            status["clear_performed"] = True
                            print(f"[SYNC][GLOBAL] deletado após remoção local: /{cmd.name}")
                        except Exception as e:
                            print(f"[SYNC][GLOBAL] falha ao deletar /{cmd.name}: {e}")
                    print(f"[SYNC] Limpeza global controlada: {deleted} deletados, {preserved} preservados (Entry Point)")
                else:
                    print("[SYNC] Limpeza global pulada; sem remoção que exija clear controlado.")

                for guild_id in sorted(guild_ids):
                    guild_obj = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild_obj)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    status["sync_performed"] = True
                    print(f"[SYNC] Slash commands sincronizados na guild {guild_id}: {len(synced_guild)}")
                    for cmd in synced_guild:
                        name = getattr(cmd, "name", None) or str(cmd)
                        print(f"[SYNC][GUILD {guild_id}] /{name}")
            self._save_app_commands_manifest(current_manifest)
            status["reason"] = "synced"
        except Exception as exc:
            status["reason"] = "failed"
            status["error"] = f"{type(exc).__name__}: {exc}"
            self._write_app_command_sync_status(status)
            raise
        self._write_app_command_sync_status(status)
        return status


    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")
        try:
            from music_system.diagnostics import cleanup_music_diagnostics_temp_artifacts

            print(f"[DIAGNOSTICS] {cleanup_music_diagnostics_temp_artifacts()}")
        except Exception as exc:
            print(f"[DIAGNOSTICS] cleanup temporário falhou: {type(exc).__name__}: {exc}")

        mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
        mongo_db_name = _cfg("MONGODB_DB", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        mongo_collection_name = _cfg("MONGODB_COLLECTION", "MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")

        if not mongo_uri:
            raise RuntimeError("Nenhuma URI do MongoDB encontrada no config.py (MONGODB_URI/MONGO_URI).")

        self.settings_db = SettingsDB(
            mongo_uri,
            mongo_db_name,
            mongo_collection_name,
        )
        await self.settings_db.init()
        self.health_state.update({"mongo_ok": True, "mongo_error": None})

        print("Carregando cogs...")
        await self._load_cogs_safely()

        should_sync = self._app_command_sync_enabled()
        allow_global_sync = self._app_command_global_sync_allowed()
        clear_globals_allowed = _env_truthy("CLEAR_GLOBAL_COMMANDS")
        guild_ids = self._resolve_app_command_sync_guild_ids()

        # Limpa comandos antigos só quando a lista/guilds mudarem. Isso evita
        # fetch/delete remoto em todo boot e mantém a proteção contra slash
        # commands que foram migrados para prefixo.
        await self._cleanup_removed_slash_commands_if_needed(guild_ids)

        # SYNC_SLASH_COMMANDS e CLEAR_GLOBAL_COMMANDS continuam existindo, mas
        # agora são permissões: sync/clear só roda quando o manifest local de
        # comandos realmente muda. Restart comum sem mudança de comando fica
        # muito mais rápido.
        await self._smart_sync_app_commands(
            guild_ids,
            should_sync=should_sync,
            allow_global_sync=allow_global_sync,
            clear_globals_allowed=clear_globals_allowed,
            trigger="boot",
        )

    def get_health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.health_state)
        uptime_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        snapshot["uptime_seconds"] = round(uptime_seconds, 2)

        # Não espere o monitor periódico de 15s para refletir que o bot já
        # voltou. O updater consulta /health logo após restart/reload; usar o
        # estado vivo aqui reduz atraso sem afrouxar a validação real.
        try:
            snapshot["discord_ready"] = self.is_ready()
            snapshot["discord_closed"] = self.is_closed()
            snapshot["guild_count"] = len(self.guilds)
            snapshot["latency_ms"] = round(float(self.latency) * 1000, 2)
        except Exception:
            pass

        ready = bool(snapshot.get("discord_ready"))
        closed = bool(snapshot.get("discord_closed"))
        mongo_ok = bool(snapshot.get("mongo_ok"))
        failed_extensions = dict(getattr(self, "failed_extensions", {}) or {})
        critical_failed = [
            name for name, data in failed_extensions.items()
            if isinstance(data, dict) and bool(data.get("critical"))
        ]

        starting = (not ready) and uptime_seconds < 120
        healthy = (ready and not closed and mongo_ok and not critical_failed) or starting
        warnings = []
        if failed_extensions:
            warnings.append(f"{len(failed_extensions)} cog(s) não carregaram")

        snapshot["starting"] = starting
        snapshot["healthy"] = healthy
        snapshot["status"] = "starting" if starting else ("ok" if healthy else "error")
        snapshot["warnings"] = warnings
        snapshot["loaded_cogs_count"] = len(getattr(self, "loaded_extensions", []) or [])
        snapshot["failed_cogs_count"] = len(failed_extensions)
        snapshot["failed_cogs"] = failed_extensions
        snapshot["critical_failed_cogs"] = critical_failed
        snapshot["cog_loading_finished"] = bool(getattr(self, "cog_loading_finished", False))
        snapshot["event_loop_last_lag_ms"] = round(float(getattr(self, "_event_loop_last_lag_ms", 0.0) or 0.0), 1)
        snapshot["event_loop_max_lag_ms"] = round(float(getattr(self, "_event_loop_max_lag_ms", 0.0) or 0.0), 1)
        snapshot["event_loop_lag_warnings"] = int(getattr(self, "_event_loop_lag_warnings", 0) or 0)
        snapshot["event_loop_recent_lags_ms"] = [round(float(v), 1) for v in list(getattr(self, "_event_loop_recent_lags_ms", []) or [])[-12:]]
        snapshot["event_loop_last_task_summary"] = str(getattr(self, "_event_loop_last_task_summary", "") or "")[:500]

        tts_cog = self.get_cog("TTSVoice")
        if tts_cog is not None and hasattr(tts_cog, "get_tts_metrics_snapshot"):
            try:
                snapshot["tts_metrics"] = tts_cog.get_tts_metrics_snapshot()
            except Exception as e:
                snapshot["tts_metrics_error"] = str(e)
        return snapshot

    async def _health_monitor_loop(self):
        while not self.is_closed():
            mongo_ok = False
            mongo_error = None
            try:
                if self.settings_db is not None:
                    await self.settings_db.client.admin.command("ping")
                    mongo_ok = True
                else:
                    mongo_error = "settings_db not initialized"
            except Exception as e:
                mongo_error = str(e)

            latency_ms = None
            try:
                latency_ms = round(float(self.latency) * 1000, 2)
            except Exception:
                pass

            self.health_state.update({
                "discord_ready": self.is_ready(),
                "discord_closed": self.is_closed(),
                "guild_count": len(self.guilds),
                "latency_ms": latency_ms,
                "mongo_ok": mongo_ok,
                "mongo_error": mongo_error,
                "last_update": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(15)

    def _event_loop_task_summary(self, *, limit: int = 8) -> str:
        try:
            current = asyncio.current_task()
            counts: dict[str, int] = {}
            for task in asyncio.all_tasks():
                if task is current or task.done():
                    continue
                coro = task.get_coro()
                qualname = getattr(coro, "__qualname__", "") or getattr(coro, "__name__", "")
                label = str(qualname or type(coro).__name__).replace("<", "").replace(">", "")
                if "await" in label and hasattr(coro, "cr_code"):
                    label = getattr(coro.cr_code, "co_name", label)
                counts[label[:90]] = counts.get(label[:90], 0) + 1
            if not counts:
                return "sem tasks pendentes do bot"
            rows = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, limit)]
            return ", ".join(f"{name}={count}" for name, count in rows)[:500]
        except Exception as exc:
            return f"indisponível: {type(exc).__name__}"

    async def _event_loop_watchdog_loop(self):
        interval = max(0.5, float(getattr(config, "BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", 1.0) or 1.0))
        warn_after = max(0.25, float(getattr(config, "BOT_EVENT_LOOP_LAG_WARNING_SECONDS", 1.5) or 1.5))
        warning_cooldown = max(10.0, float(getattr(config, "BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS", 30.0) or 30.0))
        severe_after = max(warn_after * 2.0, float(getattr(config, "BOT_EVENT_LOOP_LAG_SEVERE_SECONDS", 8.0) or 8.0))
        diagnostic_cooldown = max(warning_cooldown, float(getattr(config, "BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS", 90.0) or 90.0))
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        while not self.is_closed():
            await asyncio.sleep(interval)
            now = loop.time()
            lag = max(0.0, now - expected)
            expected = now + interval
            lag_ms = lag * 1000.0
            self._event_loop_last_lag_ms = lag_ms
            if lag_ms > self._event_loop_max_lag_ms:
                self._event_loop_max_lag_ms = lag_ms
            if lag >= warn_after:
                self._event_loop_lag_warnings += 1
                self._event_loop_recent_lags_ms.append(round(lag_ms, 1))
                # Evita logar a cada segundo durante uma trava longa; o contador no
                # health snapshot continua registrando todos os atrasos detectados.
                now_mono = time.monotonic()
                last = float(getattr(self, "_event_loop_last_warning_at", 0.0) or 0.0)
                if now_mono - last >= warning_cooldown:
                    self._event_loop_last_warning_at = now_mono
                    task_summary = ""
                    last_diag = float(getattr(self, "_event_loop_last_diagnostic_at", 0.0) or 0.0)
                    if lag >= severe_after and now_mono - last_diag >= diagnostic_cooldown:
                        self._event_loop_last_diagnostic_at = now_mono
                        task_summary = self._event_loop_task_summary()
                        self._event_loop_last_task_summary = task_summary
                    if task_summary:
                        ASYNCIO_LOG.warning(
                            "event loop atrasado %.0f ms; diagnóstico leve: %s",
                            lag_ms,
                            task_summary,
                        )
                    else:
                        ASYNCIO_LOG.warning(
                            "event loop atrasado %.0f ms; possível I/O síncrono ou CPU em callback async",
                            lag_ms,
                        )

    def _make_zip_update_view(
        self,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        title = self._zip_update_normalize_title(title, "")
        text = f"# {title}\n{description.strip()}".strip()
        if len(text) > 3900:
            text = text[:3897].rstrip() + "..."
        children: list[discord.ui.Item] = [discord.ui.TextDisplay(text)]
        if isinstance(control, dict) and control.get("enabled"):
            emoji = str(control.get("emoji") or "").strip() or None
            label = str(control.get("label") or "").strip()[:80] or None
            custom_id = str(control.get("custom_id") or "")[:100]
            disabled = bool(control.get("disabled"))
            style_name = str(control.get("style") or "secondary").strip().lower()
            style = {
                "primary": discord.ButtonStyle.primary,
                "success": discord.ButtonStyle.success,
                "danger": discord.ButtonStyle.danger,
            }.get(style_name, discord.ButtonStyle.secondary)
            if custom_id and (label or emoji):
                button = discord.ui.Button(
                    label=label,
                    emoji=emoji,
                    style=style,
                    custom_id=custom_id,
                    disabled=disabled,
                )
                children.append(discord.ui.Separator())
                children.append(discord.ui.ActionRow(button))
        view.add_item(discord.ui.Container(*children, accent_color=color))
        return view

    def _make_zip_update_confirmation_view(
        self,
        *,
        mode: str,
        token: str,
        current_commit: str,
        target_commit: str,
    ) -> discord.ui.LayoutView:
        action = "reaplicação" if mode == "redo" else "reversão"
        style = discord.ButtonStyle.success if mode == "redo" else discord.ButtonStyle.danger
        current = current_commit[:12] or "desconhecido"
        target = target_commit[:12] or "desconhecido"
        view = discord.ui.LayoutView(timeout=90)
        text = (
            f"# Confirmar {action}\n"
            "A ação criará um novo commit e poderá reiniciar processos.\n\n"
            f"`{current}` → `{target}`\n"
            "-# Nenhuma alteração será feita antes da confirmação."
        )
        confirm = discord.ui.Button(
            label=f"Confirmar {action}",
            style=style,
            custom_id=f"zip_update_confirm:{mode}:{token}"[:100],
        )
        cancel = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"zip_update_confirm:cancel:{token}"[:100],
        )
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(confirm, cancel),
                accent_color=discord.Color.gold(),
            )
        )
        return view

    def _zip_update_state_load(self) -> dict[str, object]:
        try:
            if self._zip_rollback_state_path.is_file():
                data = json.loads(self._zip_rollback_state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger("zip_update").warning("falha ao ler estado de rollback do updater", exc_info=True)
        return {}

    def _zip_update_state_save(self, data: dict[str, object]) -> bool:
        tmp: Path | None = None
        for attempt in range(3):
            try:
                self._zip_rollback_state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._zip_rollback_state_path.with_name(
                    f".{self._zip_rollback_state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self._zip_rollback_state_path)
                return True
            except Exception:
                if tmp is not None:
                    with contextlib.suppress(Exception):
                        tmp.unlink()
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                logging.getLogger("zip_update").warning(
                    "falha ao salvar estado de rollback do updater após 3 tentativas",
                    exc_info=True,
                )
        return False

    def _zip_update_component_text(self, message: discord.Message) -> str:
        """Extrai texto visível dos Components V2 sem depender de uma versão específica do discord.py."""
        parts: list[str] = []
        visited: set[int] = set()

        def visit(value: object, depth: int = 0) -> None:
            if value is None or depth > 8:
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    parts.append(text)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    visit(item, depth + 1)
                return
            if isinstance(value, dict):
                for key in ("content", "text", "label", "description", "components", "children", "items"):
                    if key in value:
                        visit(value.get(key), depth + 1)
                return

            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            for attr in ("content", "text", "label", "description", "components", "children", "items"):
                with contextlib.suppress(Exception):
                    visit(getattr(value, attr), depth + 1)

        visit(getattr(message, "components", None))
        visit(getattr(message, "content", None))
        return "\n".join(dict.fromkeys(parts))

    def _zip_update_recovery_receipt_load(self, candidate_dir: Path) -> dict[str, object]:
        path = candidate_dir / "delivery.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _zip_update_recovery_receipt_save(self, candidate_dir: Path, data: dict[str, object]) -> None:
        try:
            path = candidate_dir / "delivery.json"
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            UPDATE_LOG.warning("falha ao salvar recibo de recuperação do update em %s", candidate_dir, exc_info=True)

    def _zip_update_alert_receipt_save_sync(self, path: Path, event_id: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "event_id": event_id,
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                        "source": "bot_reconcile",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception:
            UPDATE_LOG.warning("falha ao salvar recibo global do log de update em %s", path, exc_info=True)
            return False

    def _zip_update_current_head_sync(self) -> str:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._repo_root),
                env=self._git_env(),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout.strip()
        except Exception:
            UPDATE_LOG.debug("falha ao consultar HEAD durante reconciliação de update", exc_info=True)
        return ""

    def _zip_update_updater_active_sync(self) -> bool:
        """Evita reconciliar enquanto o updater ainda está finalizando o candidato."""
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", "--quiet", "tts-bot-updater.service"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            return completed.returncode == 0
        except Exception:
            UPDATE_LOG.debug("falha ao consultar estado do updater durante reconciliação", exc_info=True)
            return False

    def _zip_update_recent_archives_sync(self) -> list[dict[str, object]]:
        """Localiza candidatos recentes que podem ter sido aplicados sem confirmação visual."""
        root = self._update_staging_root / "candidates"
        try:
            max_age = max(60, int(os.getenv("DISCORD_AUTO_UPDATE_RECONCILE_MAX_AGE_SECONDS", "1800") or 1800))
        except (TypeError, ValueError):
            max_age = 1800
        cutoff = time.time() - max_age
        records: list[dict[str, object]] = []
        for archive_state in ("done", "failed"):
            state_root = root / archive_state
            if not state_root.is_dir():
                continue
            def candidate_mtime(path: Path) -> float:
                try:
                    return path.stat().st_mtime
                except OSError:
                    return 0.0

            try:
                candidates = sorted(
                    (path for path in state_root.iterdir() if path.is_dir()),
                    key=candidate_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            for candidate_dir in candidates[:25]:
                try:
                    if candidate_dir.stat().st_mtime < cutoff:
                        continue
                    manifest = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        continue
                    status_ref = manifest.get("discord_status")
                    if not isinstance(status_ref, dict):
                        continue
                    channel_id = int(str(status_ref.get("channel_id") or "0"))
                    message_id = int(str(status_ref.get("message_id") or "0"))
                    if not channel_id or not message_id:
                        continue
                    state_data: dict[str, object] = {}
                    state_path = candidate_dir / "state.json"
                    if state_path.is_file():
                        loaded = json.loads(state_path.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            state_data = loaded
                    records.append(
                        {
                            "candidate_dir": str(candidate_dir),
                            "archive_state": archive_state,
                            "manifest": manifest,
                            "state": state_data,
                            "channel_id": channel_id,
                            "message_id": message_id,
                        }
                    )
                except Exception:
                    UPDATE_LOG.debug("candidato arquivado inválido ignorado: %s", candidate_dir, exc_info=True)
        return records[:30]

    async def _zip_update_reconcile_archived_messages_once(self) -> None:
        final_markers = (
            "atualização concluída",
            "atualização concluida",
            "nenhuma alteração",
            "nenhuma alteracao",
            "atualização bloqueada",
            "atualizacao bloqueada",
            "atualização rejeitada",
            "atualizacao rejeitada",
            "falha ao aplicar",
            "falha na atualização",
            "update aplicado",
            "update revertido",
            "reversão concluída",
            "reaplicação concluída",
        )
        if await asyncio.to_thread(self._zip_update_updater_active_sync):
            return
        records = await asyncio.to_thread(self._zip_update_recent_archives_sync)
        live_head = await asyncio.to_thread(self._zip_update_current_head_sync)
        for record in records:
            candidate_dir = Path(str(record.get("candidate_dir") or ""))
            receipt = await asyncio.to_thread(self._zip_update_recovery_receipt_load, candidate_dir)
            if bool(receipt.get("status_delivered")) and bool(receipt.get("log_delivered")):
                continue

            manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
            state_data = record.get("state") if isinstance(record.get("state"), dict) else {}
            display_id = str(manifest.get("display_id") or manifest.get("id") or candidate_dir.name)
            branch = str(manifest.get("branch") or "main")
            base_commit = str(manifest.get("base_commit") or "").strip()
            applied_commit = str(state_data.get("commit") or "").strip()
            alert_event_id = f"{display_id}-final-{applied_commit[:7] if applied_commit else 'unknown'}"
            safe_alert_event_id = re.sub(r"[^A-Za-z0-9._-]", "_", alert_event_id)[:120]
            alert_receipt = self._repo_root / "data" / "runtime" / "update-delivery-receipts" / f"{safe_alert_event_id}.alert.done"
            if alert_receipt.is_file():
                receipt["log_delivered"] = True

            channel_id = int(record.get("channel_id") or 0)
            message_id = int(record.get("message_id") or 0)
            message = await self._zip_update_fetch_message(channel_id, message_id)
            if message is None:
                continue
            visible_text = self._zip_update_component_text(message).casefold()
            already_final = any(marker in visible_text for marker in final_markers)
            if already_final:
                receipt.update(
                    {
                        "status_delivered": True,
                        "status_detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                # A mensagem pública final não prova que o webhook/log também
                # chegou. O recibo global é compartilhado com a outbox do shell,
                # evitando que uma recuperação do bot e um replay posterior
                # publiquem o mesmo log duas vezes.
                await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)
                if bool(receipt.get("log_delivered")):
                    continue
            changed_files = [str(item) for item in (manifest.get("changed_files") or []) if str(item).strip()]
            diff_stats = manifest.get("diff_stats") if isinstance(manifest.get("diff_stats"), dict) else {}
            diff_summary = str(diff_stats.get("summary") or "").strip()
            source_author_id = str((manifest.get("discord_status") or {}).get("source_author_id") or "")
            archive_state = str(record.get("archive_state") or "failed")

            if archive_state == "done" and applied_commit:
                commit_line = ""
                if base_commit:
                    commit_line = f"`{base_commit[:7]}` → `{applied_commit[:7]}`\n"
                count_text = "1 arquivo alterado" if len(changed_files) == 1 else f"{len(changed_files)} arquivos alterados"
                current_matches = bool(live_head and applied_commit == live_head)
                if not current_matches:
                    # Um diretório arquivado como done não é prova suficiente de
                    # sucesso se o HEAD já voltou para outro commit. Publicar verde
                    # nesse caso criava o falso positivo visto durante rollback.
                    title = "⚠️ Estado da atualização divergente"
                    description = (
                        "O registro da atualização indica conclusão, mas o commit atualmente instalado é diferente.\n\n"
                        f"Atualização `{display_id}`\n"
                        f"Registrado: `{applied_commit[:7]}`\n"
                        f"Instalado: `{live_head[:7] if live_head else 'desconhecido'}`\n"
                        "Nenhum controle de reversão foi disponibilizado."
                    )
                    control = None
                    status = "warn"
                    log_title = title
                    log_summary = "A reconciliação detectou divergência entre o candidato concluído e o HEAD local."
                else:
                    title = "✅ Atualização concluída"
                    description = (
                        "A atualização foi aplicada; a confirmação visual foi recuperada automaticamente após o reinício.\n\n"
                        f"Atualização `{display_id}`\n"
                        f"{commit_line}{count_text}{f' · {diff_summary}' if diff_summary else ''}\n"
                        "Processo ativo e bot conectado ao Discord."
                    )
                    control = {
                        "enabled": True,
                        "mode": "rollback",
                        "token": hashlib.sha256(f"{display_id}:{applied_commit}".encode()).hexdigest()[:16],
                        "branch": branch,
                        "expected_head": applied_commit,
                        "revert_commit": applied_commit,
                        "head_commit": applied_commit,
                        "update_from": base_commit,
                        "update_to": applied_commit,
                        "source_author_id": source_author_id,
                    }
                    status = "success"
                    log_title = title
                    log_summary = "Confirmação final recuperada após o bot reiniciar durante a atualização."
            elif archive_state == "done":
                title = "ℹ️ Nenhuma alteração necessária"
                description = f"A atualização `{display_id}` já correspondia ao estado atual. Nenhum arquivo foi modificado."
                control = None
                status = "success"
                log_title = title
                log_summary = "Candidato concluído sem diferenças no repositório."
            else:
                error_text = str(state_data.get("last_error") or "A atualização não foi concluída.").strip()
                title = "❌ Falha ao aplicar atualização"
                description = f"A confirmação de falha foi recuperada automaticamente.\n\nAtualização `{display_id}`\n{error_text[:900]}"
                control = None
                status = "error"
                log_title = title
                log_summary = error_text[:1200]

            if not already_final or status == "warn":
                result = await self._edit_zip_status_from_update(
                    {
                        "channel_id": str(channel_id),
                        "message_id": str(message_id),
                        "status": status,
                        "title": title,
                        "description": description,
                        "candidate_id": str(manifest.get("id") or ""),
                        "display_id": display_id,
                        "event_at": datetime.now(timezone.utc).isoformat(),
                        "preserve_existing_control": control is None,
                        **({"control": control} if control else {}),
                    }
                )
                if not result.get("ok"):
                    continue

                receipt.update(
                    {
                        "status_delivered": True,
                        "status_recovered_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)

            if bool(receipt.get("log_delivered")):
                continue
            body_lines = [
                f"Resumo: {log_summary}",
                f"Identificador: {display_id}",
                f"Branch: {branch}",
            ]
            if applied_commit:
                body_lines.append(f"Commit: {base_commit[:7] if base_commit else 'desconhecido'} → {applied_commit[:7]}")
            body_lines.append(f"Update: {'1 arquivo' if len(changed_files) == 1 else f'{len(changed_files)} arquivos'}{f' · {diff_summary}' if diff_summary else ''}")
            body_lines.append("Resultado: confirmação recuperada automaticamente")
            body_lines.append(f"Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            alert_type = "success" if status == "success" else ("warn" if status == "warn" else "error")
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    ["bash", str(self._repo_root / "alert.sh"), alert_type, log_title, "\n".join(body_lines)],
                    cwd=str(self._repo_root),
                    env=self._git_env(),
                    text=True,
                    capture_output=True,
                    timeout=25,
                    check=False,
                )
                if completed.returncode == 0:
                    global_receipt_saved = await asyncio.to_thread(
                        self._zip_update_alert_receipt_save_sync,
                        alert_receipt,
                        alert_event_id,
                    )
                    receipt["log_delivered"] = True
                    receipt["log_recovered_at"] = datetime.now(timezone.utc).isoformat()
                    receipt["global_log_receipt_saved"] = global_receipt_saved
                    await asyncio.to_thread(self._zip_update_recovery_receipt_save, candidate_dir, receipt)
                else:
                    UPDATE_LOG.warning("falha ao recuperar log final de %s: %s", display_id, (completed.stderr or completed.stdout or "")[-500:])
            except Exception:
                UPDATE_LOG.warning("falha ao executar recuperação do log final de %s", display_id, exc_info=True)

    async def _zip_update_reconcile_loop(self) -> None:
        await asyncio.sleep(4)
        while not self.is_closed():
            try:
                await self._zip_update_reconcile_archived_messages_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                UPDATE_LOG.warning("falha na reconciliação de confirmações de update", exc_info=True)
            await asyncio.sleep(60)

    def _zip_update_rollback_request_roots(self) -> list[Path]:
        roots: list[Path] = []
        raw_env = os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIRS") or os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIR") or ""
        for raw in re.split(r"[:;,]", raw_env):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw))
        roots.extend([
            self._update_staging_root / "candidates" / "rollback",
            self._repo_root / "data" / "runtime" / "update-rollback",
            Path(tempfile.gettempdir()) / "tts-bot-update-rollback",
        ])
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                key = str(root.expanduser().resolve(strict=False))
            except Exception:
                key = str(root)
            if key in seen:
                continue
            seen.add(key)
            unique.append(root)
        return unique

    def _zip_update_find_writable_rollback_request_root(self) -> tuple[Path | None, list[str]]:
        details: list[str] = []
        for root in self._zip_update_rollback_request_roots():
            try:
                root.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(Exception):
                    root.chmod(0o775)
                probe = root / f".write-test.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return root, details
            except Exception as exc:
                details.append(f"{root}: {exc.__class__.__name__}: {exc}")
        return None, details

    def _zip_update_has_pending_rollback_request(self) -> bool:
        for root in self._zip_update_rollback_request_roots():
            try:
                if (root / "pending.json").exists() or (root / "active.json").exists():
                    return True
            except Exception:
                continue
        return False

    def _zip_update_control_for_record(self, record: dict[str, object], *, disabled: bool = False) -> dict[str, object] | None:
        token = str(record.get("token") or "").strip()
        mode = str(record.get("mode") or "rollback").strip().lower()
        if not token or mode not in {"rollback", "redo"}:
            return None
        return {
            "enabled": True,
            "emoji": "↪️" if mode == "redo" else "↩️",
            "label": "Reaplicar atualização" if mode == "redo" else "Reverter atualização",
            "style": "success" if mode == "redo" else "danger",
            "custom_id": f"zip_update:{mode}:{token}"[:100],
            "disabled": disabled,
        }

    def _zip_update_cancel_control(self, candidate_id: str, *, disabled: bool = False) -> dict[str, object] | None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id:
            return None
        return {
            "enabled": True,
            "emoji": "✖️",
            "label": "Cancelar atualização",
            "style": "secondary",
            "custom_id": f"zip_update:cancel:{candidate_id}"[:100],
            "disabled": disabled,
        }

    def _zip_update_limits(self) -> ZipLimits:
        defaults = ZipLimits()

        def env_int(name: str, default: int, *, minimum: int = 1) -> int:
            try:
                return max(minimum, int(str(os.getenv(name, default)).strip()))
            except (TypeError, ValueError):
                return default

        def env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
            try:
                return max(minimum, float(str(os.getenv(name, default)).strip()))
            except (TypeError, ValueError):
                return default

        return ZipLimits(
            max_archive_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_ZIP_BYTES", defaults.max_archive_bytes),
            max_uncompressed_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_UNCOMPRESSED_BYTES", defaults.max_uncompressed_bytes),
            max_entries=env_int("DISCORD_AUTO_UPDATE_MAX_ENTRIES", defaults.max_entries),
            max_file_bytes=env_int("DISCORD_AUTO_UPDATE_MAX_FILE_BYTES", defaults.max_file_bytes),
            max_compression_ratio=env_float("DISCORD_AUTO_UPDATE_MAX_COMPRESSION_RATIO", defaults.max_compression_ratio),
        )

    def _zip_update_authorized_user_ids(self, record: dict[str, object] | None = None) -> set[int]:
        ids: set[int] = set()
        raw_values = [
            os.getenv("DISCORD_AUTO_UPDATE_USER_IDS", ""),
            os.getenv("BOT_OWNER_IDS", ""),
            os.getenv("OWNER_IDS", ""),
            os.getenv("BOT_OWNER_ID", ""),
            os.getenv("OWNER_ID", ""),
        ]
        if isinstance(record, dict):
            raw_values.extend([
                str(record.get("source_author_id") or ""),
                str(record.get("requested_by") or ""),
            ])
        for raw in raw_values:
            for piece in re.split(r"[,;\s]+", str(raw or "")):
                piece = piece.strip()
                if piece.isdigit():
                    ids.add(int(piece))
        return ids

    def _zip_update_can_control(self, interaction: discord.Interaction, record: dict[str, object]) -> bool:
        user_id = int(getattr(interaction.user, "id", 0) or 0)
        if user_id in self._zip_update_authorized_user_ids(record):
            return True
        perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            return True
        return False

    def _zip_update_can_submit(self, message: discord.Message) -> bool:
        author = getattr(message, "author", None)
        user_id = int(getattr(author, "id", 0) or 0)
        allowed = self._zip_update_authorized_user_ids()
        allowed.update(int(value) for value in (getattr(self, "owner_ids", None) or set()) if str(value).isdigit())
        owner_id = int(getattr(self, "owner_id", 0) or 0)
        if owner_id:
            allowed.add(owner_id)
        if user_id and user_id in allowed:
            return True
        perms = getattr(author, "guild_permissions", None)
        return bool(perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)))

    async def _send_zip_update_message(
        self,
        message: discord.Message,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control)
        return await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _edit_zip_update_message(
        self,
        source_message: discord.Message,
        status_message: discord.Message | None,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control)
        if status_message is not None:
            try:
                await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return status_message
            except discord.HTTPException:
                logging.getLogger("zip_update").warning(
                    "não consegui editar a mensagem de status do ZIP; enviando resultado final",
                    exc_info=True,
                )
        return await source_message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _zip_update_fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return None
            return await channel.fetch_message(int(message_id))
        except Exception:
            logging.getLogger("zip_update").warning("falha ao buscar mensagem de update", exc_info=True)
            return None

    async def _zip_update_clear_previous_control(self, previous: dict[str, object] | None, *, keep_message_id: int = 0) -> None:
        if not isinstance(previous, dict):
            return
        try:
            channel_id = int(str(previous.get("channel_id") or "0"))
            message_id = int(str(previous.get("message_id") or "0"))
        except Exception:
            return
        if not channel_id or not message_id or message_id == keep_message_id:
            return
        msg = await self._zip_update_fetch_message(channel_id, message_id)
        if msg is None:
            return
        title = str(previous.get("title") or "Update aplicado")
        description = str(previous.get("description") or "")
        status = str(previous.get("status") or "success")
        color = self._zip_update_status_color(status)
        try:
            await msg.edit(view=self._make_zip_update_view(title, description, color), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.getLogger("zip_update").warning("falha ao remover controle de update antigo", exc_info=True)

    def _zip_update_status_color(self, status: str) -> discord.Color:
        status = str(status or "info").lower().strip()
        if status in {"success", "ok", "done"}:
            return discord.Color.green()
        if status in {"warn", "warning", "progress"}:
            return discord.Color.gold()
        if status in {"error", "failed", "failure"}:
            return discord.Color.red()
        if status in {"applying", "pending"}:
            return discord.Color.blurple()
        return discord.Color.blurple()

    def _zip_update_normalize_title(self, title: str, status: str = "") -> str:
        title = str(title or "").strip() or "Atualização"
        normalized = re.sub(r"\s+", " ", title).strip()
        lowered = normalized.casefold()
        status = str(status or "").lower().strip()
        replacements = {
            "update": "Atualização",
            "aplicando update...": "⚙️ Aplicando atualização",
            "revertendo update...": "↩️ Revertendo atualização",
            "reaplicando update...": "↪️ Reaplicando atualização",
            "update aplicado": "✅ Atualização concluída",
            "✅ update aplicado": "✅ Atualização concluída",
        }
        if lowered in replacements:
            return replacements[lowered]
        if status in {"success", "ok", "done"} and lowered in {"atualização", "update"}:
            return "✅ Atualização concluída"
        return normalized

    def _zip_update_latest_message_ref(self, interaction: discord.Interaction, record: dict[str, object]) -> tuple[str, str]:
        channel_id = str(record.get("channel_id") or "").strip()
        message_id = str(record.get("message_id") or "").strip()
        msg = getattr(interaction, "message", None)
        if msg is not None:
            if not channel_id:
                channel_id = str(getattr(getattr(msg, "channel", None), "id", "") or "")
            if not message_id:
                message_id = str(getattr(msg, "id", "") or "")
        return channel_id, message_id

    def _zip_update_restore_record_message_view(self, record: dict[str, object]) -> discord.ui.LayoutView:
        title = self._zip_update_normalize_title(str(record.get("title") or "Update aplicado"), str(record.get("status") or "success"))
        description = str(record.get("description") or "")
        status = str(record.get("status") or "success")
        return self._make_zip_update_view(
            title,
            description,
            self._zip_update_status_color(status),
            control=self._zip_update_control_for_record(record),
        )

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("HOME", "/home/ubuntu")
        key_path = Path("/home/ubuntu/.ssh/id_ed25519")
        if key_path.is_file():
            env.setdefault("GIT_SSH_COMMAND", f"ssh -i {key_path} -o IdentitiesOnly=yes")
        return env

    def _run_cmd(self, args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _normalize_zip_member_parts(self, raw_name: str) -> tuple[str, ...]:
        posix = PurePosixPath(raw_name.replace("\\", "/"))
        return tuple(part for part in posix.parts if part not in ("", "."))

    def _zip_update_should_ignore_generated_file(self, rel_path: Path) -> bool:
        """Ignora lixo de build que não deve virar commit pelo auto updater."""
        parts = tuple(str(part) for part in rel_path.parts)
        name = parts[-1] if parts else ""
        if not parts:
            return True
        if any(part in {"__pycache__", ".gradle", ".idea"} for part in parts):
            return True
        if name.endswith((".pyc", ".pyo", ".tmp")):
            return True
        if name.startswith("build.gradle.bak") or name.endswith(".bak-sdk35"):
            return True
        core_worker_prefix = ("android", "core-worker-app")
        if parts[:2] == core_worker_prefix:
            if len(parts) >= 4 and parts[2] == "app" and parts[3] == "build":
                return True
            if len(parts) >= 3 and parts[2] == "releases":
                return True
        return False

    def _zip_update_is_forbidden_path(self, rel_path: Path | str) -> bool:
        return is_forbidden_update_path(rel_path)

    def _prepare_update_staging_clone(self, origin_url: str, branch_name: str, env: dict[str, str]) -> Path:
        """Mantém um clone staging reutilizável para não clonar o repositório inteiro a cada ZIP."""
        self._update_staging_root.mkdir(parents=True, exist_ok=True)
        clone_dir = self._update_staging_root / "repo"

        def clone_fresh() -> Path:
            shutil.rmtree(clone_dir, ignore_errors=True)
            clone_result = self._run_cmd(["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(clone_dir)], self._update_staging_root, env=env)
            if clone_result.returncode != 0:
                err = (clone_result.stderr or clone_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar staging persistente do update. {err}")
            return clone_dir

        if not (clone_dir / ".git").exists():
            return clone_fresh()

        # Se o staging antigo ficou corrompido, recria do zero. Caso contrário,
        # reset/clean é muito mais rápido do que um clone completo.
        for args in (
            ["git", "remote", "set-url", "origin", origin_url],
            ["git", "fetch", "--prune", "origin", branch_name],
            ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
            ["git", "reset", "--hard", f"origin/{branch_name}"],
            ["git", "clean", "-fdx"],
        ):
            result = self._run_cmd(args, clone_dir, env=env)
            if result.returncode != 0:
                logging.getLogger("zip_update").warning("staging persistente falhou em %s; recriando clone", " ".join(args))
                return clone_fresh()
        return clone_dir

    def _git_diff_numstat_sync(self, repo_dir: Path, env: dict[str, str]) -> dict[str, object]:
        result = self._run_cmd(["git", "diff", "--cached", "--numstat"], repo_dir, env=env)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Falha ao calcular diff de linhas. {err}")

        entries: list[dict[str, object]] = []
        total_added = 0
        total_removed = 0
        binary_files = 0
        for raw in (result.stdout or "").splitlines():
            parts = raw.split("\t")
            if len(parts) < 3:
                continue
            added_raw, removed_raw, path_raw = parts[0], parts[1], parts[-1]
            is_binary = added_raw == "-" or removed_raw == "-"
            added = 0 if is_binary else int(added_raw or 0)
            removed = 0 if is_binary else int(removed_raw or 0)
            if is_binary:
                binary_files += 1
            else:
                total_added += added
                total_removed += removed
            entries.append({"path": path_raw, "added": added, "removed": removed, "binary": is_binary})

        summary = f"+{total_added} -{total_removed}"
        if binary_files:
            summary += f" · {binary_files} binário(s)"
        return {"entries": entries, "summary": summary, "total_added": total_added, "total_removed": total_removed, "binary_files": binary_files}

    def _write_local_update_candidate_sync(
        self,
        *,
        branch_name: str,
        base_commit: str | None,
        changed_files: list[str],
        diff_stats: dict[str, object],
        extracted_files: list[tuple[Path, Path]],
        zip_name: str,
        zip_inspection: dict[str, object],
        patch_diff_text: str = "",
        status_context: dict[str, object] | None = None,
        preparation_steps: list[dict[str, object]] | None = None,
        preparation_started_monotonic: float | None = None,
        candidate_step_started_monotonic: float | None = None,
    ) -> dict[str, object]:
        """Grava um candidato íntegro para validação e aplicação na VPS."""
        candidate_root = self._update_staging_root / "candidates"
        candidate_root.mkdir(parents=True, exist_ok=True)
        unique = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        candidate_id = f"zip-{created_at.strftime('%Y%m%d%H%M%S')}-{unique[:8]}"
        display_id = f"UPD-{unique[:8].upper()}"
        candidate_dir = candidate_root / candidate_id
        files_dir = candidate_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        try:
            written_files: list[str] = []
            expected_changed = set(changed_files)
            for extracted_path, rel_path in extracted_files:
                rel_posix = rel_path.as_posix()
                if rel_posix not in expected_changed:
                    continue
                if self._zip_update_is_forbidden_path(rel_path):
                    raise RuntimeError(f"Caminho protegido não pode ser alterado: {rel_posix}")
                target = files_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted_path, target)
                written_files.append(rel_posix)

            if not written_files:
                raise RuntimeError("O candidato local não trouxe nenhum arquivo alterado.")
            if set(written_files) != expected_changed:
                missing = sorted(expected_changed - set(written_files))
                raise RuntimeError("Candidato incompleto; arquivo(s) ausente(s): " + ", ".join(missing[:5]))

            patch_path = candidate_dir / "patch.diff"
            if patch_diff_text.strip():
                patch_path.write_text(patch_diff_text, encoding="utf-8")

            max_age_seconds = max(300, int(os.getenv("DISCORD_AUTO_UPDATE_CANDIDATE_MAX_AGE_SECONDS", "86400") or 86400))
            expires_at = created_at + timedelta(seconds=max_age_seconds)
            file_integrity = build_file_integrity(files_dir, written_files)

            prepared_at = time.monotonic()
            candidate_prepare_elapsed_ms = max(
                0,
                int((prepared_at - float(candidate_step_started_monotonic or prepared_at)) * 1000),
            )
            preparation_total_ms = max(
                candidate_prepare_elapsed_ms,
                int((prepared_at - float(preparation_started_monotonic or prepared_at)) * 1000),
            )
            handoff_steps: list[dict[str, object]] = []
            seen_handoff_labels: set[str] = set()
            for raw_step in list(preparation_steps or []):
                if not isinstance(raw_step, dict):
                    continue
                label = " ".join(str(raw_step.get("label") or "").split())[:120]
                if not label or label in seen_handoff_labels:
                    continue
                try:
                    elapsed_ms = max(0, int(raw_step.get("elapsed_ms") or 0))
                except (TypeError, ValueError):
                    elapsed_ms = 0
                seen_handoff_labels.add(label)
                handoff_steps.append({"label": label, "elapsed_ms": elapsed_ms})
            final_preparation_label = "Candidato seguro preparado"
            if final_preparation_label not in seen_handoff_labels:
                handoff_steps.append(
                    {
                        "label": final_preparation_label,
                        "elapsed_ms": candidate_prepare_elapsed_ms,
                    }
                )

            manifest: dict[str, object] = {
                "schema_version": 2,
                "id": candidate_id,
                "display_id": display_id,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "branch": branch_name,
                "base_commit": base_commit,
                "source": "discord_zip",
                "zip_name": zip_name,
                "zip_sha256": str(zip_inspection.get("sha256") or ""),
                "zip_stats": zip_inspection,
                "commit_message": f"update: aplicar {display_id}",
                "changed_files": written_files,
                "diff_stats": diff_stats,
                "file_integrity": file_integrity,
                "patch_sha256": sha256_file(patch_path) if patch_path.is_file() else "",
                "patch_size": patch_path.stat().st_size if patch_path.is_file() else 0,
                "progress_handoff": {
                    "schema_version": 1,
                    "completed_steps": handoff_steps,
                    "completed_count": len(handoff_steps),
                    "preparation_total_ms": preparation_total_ms,
                },
            }
            if isinstance(status_context, dict) and status_context.get("message_id") and status_context.get("channel_id"):
                manifest["discord_status"] = {
                    "guild_id": str(status_context.get("guild_id") or ""),
                    "channel_id": str(status_context.get("channel_id") or ""),
                    "message_id": str(status_context.get("message_id") or ""),
                    "source_message_id": str(status_context.get("source_message_id") or ""),
                    "source_author_id": str(status_context.get("source_author_id") or ""),
                }
            manifest_tmp = candidate_dir / ".manifest.json.tmp"
            manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(manifest_tmp, candidate_dir / "manifest.json")

            queue_root = candidate_root / "queue"
            pending_dir = queue_root / "pending"
            active_dir = queue_root / "active"
            pending_dir.mkdir(parents=True, exist_ok=True)
            active_dir.mkdir(parents=True, exist_ok=True)
            for path in (queue_root, pending_dir, active_dir):
                with contextlib.suppress(Exception):
                    path.chmod(0o775)

            active_count = len([item for item in active_dir.glob("*.json") if item.is_file()])
            pending_count = len([item for item in pending_dir.glob("*.json") if item.is_file()])
            if (candidate_root / "pending.json").exists():
                pending_count += 1
            queue_position = active_count + pending_count + 1

            queue_name = f"{created_at.strftime('%Y%m%d%H%M%S%f')}-{candidate_id}.json"
            pending_path = pending_dir / queue_name
            tmp_pending = pending_dir / f".{queue_name}.tmp"
            queue_payload = {
                "candidate_dir": str(candidate_dir),
                "id": candidate_id,
                "display_id": display_id,
                "state": "queued",
                "attempt": 0,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "queue_position_at_enqueue": queue_position,
            }
            tmp_pending.write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_pending, pending_path)
            return {
                "candidate_id": candidate_id,
                "display_id": display_id,
                "candidate_dir": str(candidate_dir),
                "pending_path": str(pending_path),
                "queue_position": queue_position,
                "queue_pending_count": pending_count + 1,
                "candidate_prepare_elapsed_ms": candidate_prepare_elapsed_ms,
                "preparation_total_ms": preparation_total_ms,
            }
        except Exception:
            shutil.rmtree(candidate_dir, ignore_errors=True)
            raise

    def _trigger_updater_service_sync(self) -> tuple[bool, str]:
        service = Path("/etc/systemd/system/tts-bot-updater.service")
        if not service.exists():
            return False, "updater via systemd não encontrado"
        for args in (["sudo", "-n", "systemctl", "start", "--no-block", "tts-bot-updater.service"], ["systemctl", "start", "--no-block", "tts-bot-updater.service"]):
            result = self._run_cmd(args, self._repo_root, env=self._git_env())
            if result.returncode == 0:
                return True, "updater disparado agora"
        detail = (result.stderr or result.stdout or "").strip() if 'result' in locals() else ""
        if detail:
            detail = detail.splitlines()[-1][:180]
            return False, f"commit enviado; timer aplicará depois ({detail})"
        return False, "commit enviado; timer aplicará depois"

    def _guess_repo_name(self, origin_url: str) -> str:
        cleaned = (origin_url or "").strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if "/" in cleaned:
            cleaned = cleaned.rsplit("/", 1)[-1]
        if ":" in cleaned:
            cleaned = cleaned.rsplit(":", 1)[-1]
        return cleaned.strip()

    def _pick_zip_strip_count(self, file_members: list[tuple[str, ...]], repo_name_hint: str, branch_name: str) -> int:
        if not file_members:
            return 0

        repo_root = self._repo_root.resolve()
        repo_top_names = {child.name for child in repo_root.iterdir()}
        max_strip = min(max(len(parts) - 1, 0) for parts in file_members)
        best_strip = 0
        best_score = (-1, -1, 0)

        for strip_count in range(max_strip + 1):
            mapped_members = [parts[strip_count:] for parts in file_members]
            if any(not parts for parts in mapped_members):
                continue

            exact_exists = 0
            top_level_exists = 0
            for mapped_parts in mapped_members:
                rel_path = Path(*mapped_parts)
                if (repo_root / rel_path).exists():
                    exact_exists += 1
                if mapped_parts[0] in repo_top_names:
                    top_level_exists += 1

            score = (exact_exists, top_level_exists, -strip_count)
            if score > best_score:
                best_score = score
                best_strip = strip_count

        if best_score[:2] != (0, 0):
            return best_strip

        common_first = file_members[0][0] if file_members[0] else ""
        if common_first and all(parts and parts[0] == common_first for parts in file_members):
            wrapper_names = {
                self._repo_root.name,
                repo_name_hint,
                f"{repo_name_hint}-main",
                f"{repo_name_hint}-master",
                f"{repo_name_hint}-{branch_name}",
            }
            if common_first in wrapper_names and common_first not in repo_top_names:
                return 1

        return best_strip

    def _phone_worker_base_url(self) -> str | None:
        if not bool(_cfg("PHONE_WORKER_ENABLED", default=False)):
            return None
        host = str(_cfg("PHONE_WORKER_HOST", default="") or "").strip()
        if not host:
            return None
        scheme = str(_cfg("PHONE_WORKER_SCHEME", default="http") or "http").strip() or "http"
        port = int(_cfg("PHONE_WORKER_PORT", default=8766) or 8766)
        return f"{scheme}://{host}:{port}"

    def _phone_worker_request_sync(self, task: str, payload: dict[str, object], *, timeout: float = 5.0) -> dict[str, object] | None:
        base_url = self._phone_worker_base_url()
        if not base_url:
            return None
        task = str(task or "").strip()
        now = time.monotonic()
        cooldown_default = 900.0 if task == "zip_validate" else 120.0
        cooldown = float(_cfg("PHONE_WORKER_UNAVAILABLE_COOLDOWN_SECONDS", default=cooldown_default) or cooldown_default)
        until = float(self._phone_worker_unavailable_until_by_task.get(task) or 0.0)
        if until and now < until:
            last_log = float(self._phone_worker_unavailable_last_log_by_task.get(task) or 0.0)
            if now - last_log >= 300.0:
                self._phone_worker_unavailable_last_log_by_task[task] = now
                logging.getLogger("zip_update").info(
                    "phone-worker indisponível para %s: pulando por cooldown %.0fs",
                    task, max(0.0, until - now),
                )
            return None
        token = str(_cfg("PHONE_WORKER_TOKEN", default="") or "").strip()
        payload = dict(payload)
        payload["task"] = task
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{base_url}/task", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                self._phone_worker_unavailable_until_by_task.pop(task, None)
                return parsed
            return None
        except Exception as exc:
            self._phone_worker_unavailable_until_by_task[task] = now + max(30.0, cooldown)
            last_log = float(self._phone_worker_unavailable_last_log_by_task.get(task) or 0.0)
            if now - last_log >= 300.0:
                self._phone_worker_unavailable_last_log_by_task[task] = now
                logging.getLogger("zip_update").info("phone-worker indisponível para %s: %r", task, exc)
            return None

    def _phone_worker_validate_zip_sync(self, zip_path: Path) -> dict[str, object] | None:
        if not bool(_cfg("PHONE_WORKER_ZIP_VALIDATE_ENABLED", default=True)):
            return None
        try:
            max_mb = int(_cfg("PHONE_WORKER_ZIP_VALIDATE_MAX_MB", default=24) or 24)
            if zip_path.stat().st_size > max_mb * 1024 * 1024:
                logging.getLogger("zip_update").info("zip grande demais para validação phone-worker: %.2f MB", zip_path.stat().st_size / 1048576)
                return None
            now = time.monotonic()
            until = float(self._phone_worker_unavailable_until_by_task.get("zip_validate") or 0.0)
            if until and now < until:
                return None
            timeout = float(_cfg("PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS", default=1.5) or 1.5)
            timeout = max(0.5, min(timeout, 5.0))
            result = self._phone_worker_request_sync(
                "zip_validate",
                {
                    "filename": zip_path.name,
                    "data_b64": base64.b64encode(zip_path.read_bytes()).decode("ascii"),
                    "max_entries": 800,
                    "max_preview": 40,
                },
                timeout=timeout,
            )
            if result:
                logging.getLogger("zip_update").info(
                    "phone-worker validou ZIP: ok=%s risk=%s files=%s size=%s",
                    result.get("ok"), result.get("risk"), result.get("files"), result.get("size"),
                )
            return result
        except Exception as exc:
            logging.getLogger("zip_update").info("falha ao preparar validação phone-worker do ZIP: %r", exc)
            return None

    def _safe_extract_patch(
        self,
        zip_path: Path,
        extract_dir: Path,
        repo_name_hint: str,
        branch_name: str,
    ) -> tuple[list[tuple[Path, Path]], dict[str, object]]:
        limits = self._zip_update_limits()
        inspection = inspect_zip_archive(zip_path, limits)
        accepted: list[tuple[Path, Path]] = []
        mapped_entries: dict[str, str] = {}
        mapped_files: set[str] = set()
        mapped_dirs: set[str] = set()
        extract_root = extract_dir.resolve()

        with zipfile.ZipFile(zip_path) as zf:
            file_members: list[tuple[str, ...]] = []
            prepared_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []

            for info in zf.infolist():
                raw_parts = self._normalize_zip_member_parts(info.filename)
                if not raw_parts:
                    continue
                if raw_parts[0] == "__MACOSX" or raw_parts[-1] == ".DS_Store":
                    continue
                if any(part == ".." for part in raw_parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")
                prepared_infos.append((info, raw_parts))
                if not info.is_dir():
                    file_members.append(raw_parts)

            strip_count = self._pick_zip_strip_count(file_members, repo_name_hint, branch_name)

            for info, raw_parts in prepared_infos:
                normalized_parts = raw_parts[strip_count:]
                if not normalized_parts:
                    continue
                normalized = PurePosixPath(*normalized_parts)
                if normalized.is_absolute() or any(part in ("", ".", "..") for part in normalized.parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                target_rel = Path(*normalized.parts)
                if self._zip_update_should_ignore_generated_file(target_rel):
                    continue
                if self._zip_update_is_forbidden_path(target_rel):
                    raise RuntimeError(f"Caminho protegido não pode ser alterado: {target_rel.as_posix()}")

                key = canonical_path_key(normalized.parts)
                previous = mapped_entries.get(key)
                if previous is not None:
                    raise RuntimeError(
                        f"Caminho duplicado ou ambíguo após normalização: {previous} e {info.filename}"
                    )
                mapped_entries[key] = info.filename

                ancestor_keys = [canonical_path_key(normalized.parts[:idx]) for idx in range(1, len(normalized.parts))]
                if any(ancestor in mapped_files for ancestor in ancestor_keys):
                    raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")

                if info.is_dir():
                    if key in mapped_files:
                        raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")
                    mapped_dirs.add(key)
                    target_dir = (extract_dir / target_rel).resolve()
                    if extract_root not in target_dir.parents and target_dir != extract_root:
                        raise RuntimeError(f"Arquivo fora da extração: {target_rel.as_posix()}")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    continue

                if key in mapped_dirs or any(existing.startswith(key + "/") for existing in mapped_files | mapped_dirs):
                    raise RuntimeError(f"Conflito entre arquivo e diretório no ZIP: {info.filename}")
                mapped_files.add(key)
                extract_path = (extract_dir / target_rel).resolve()
                if extract_root not in extract_path.parents:
                    raise RuntimeError(f"Arquivo fora da extração: {target_rel.as_posix()}")
                extract_path.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info, "r") as src, open(extract_path, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limits.max_file_bytes or written > int(info.file_size):
                            raise RuntimeError(f"Tamanho descompactado inválido: {info.filename}")
                        dst.write(chunk)
                if written != int(info.file_size):
                    raise RuntimeError(f"Tamanho descompactado divergente: {info.filename}")
                accepted.append((extract_path, target_rel))

        if not accepted:
            raise RuntimeError("O ZIP não trouxe nenhum arquivo aplicável.")
        return accepted, inspection.as_dict()

    def _apply_patch_to_clone(self, extracted_files: list[tuple[Path, Path]], clone_dir: Path) -> list[str]:
        changed_files: list[str] = []
        for extracted_path, rel_path in extracted_files:
            destination = (clone_dir / rel_path).resolve()
            clone_root = clone_dir.resolve()
            if clone_root not in destination.parents and destination != clone_root:
                raise RuntimeError(f"Arquivo fora do repositório: {rel_path.as_posix()}")

            destination.parent.mkdir(parents=True, exist_ok=True)
            before = destination.read_bytes() if destination.exists() else None
            data = extracted_path.read_bytes()
            if before == data:
                continue
            destination.write_bytes(data)
            changed_files.append(rel_path.as_posix())
        return changed_files

    def _process_zip_update_sync(
        self,
        zip_path: Path,
        status_context: dict[str, object] | None = None,
        progress_callback=None,
    ) -> dict[str, object]:
        self._update_temp_root.mkdir(parents=True, exist_ok=True)
        env = self._git_env()
        origin_result = self._run_cmd(["git", "remote", "get-url", "origin"], self._repo_root, env=env)
        if origin_result.returncode != 0:
            raise RuntimeError(f"Não foi possível descobrir o origin do git. {origin_result.stderr.strip() or origin_result.stdout.strip()}")
        origin_url = (origin_result.stdout or "").strip()
        if not origin_url:
            raise RuntimeError("O repositório local não tem origin configurado.")

        branch_result = self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._repo_root, env=env)
        branch_name = (branch_result.stdout or "main").strip() or "main"
        if branch_result.returncode != 0 or branch_name == "HEAD":
            branch_name = "main"

        work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-", dir=str(self._update_temp_root)))
        extract_dir = work_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        timings: dict[str, int] = {}
        t0 = time.monotonic()
        last = t0
        preparation_steps: list[dict[str, object]] = []

        def publish_progress(current: str, completed: str = "", elapsed_ms: int | None = None) -> None:
            completed_label = " ".join(str(completed or "").split())[:120]
            safe_elapsed_ms = max(0, int(elapsed_ms or 0))
            if completed_label and not any(
                str(step.get("label") or "") == completed_label for step in preparation_steps
            ):
                preparation_steps.append(
                    {"label": completed_label, "elapsed_ms": safe_elapsed_ms}
                )
            if progress_callback is None:
                return
            try:
                progress_callback(
                    {
                        "current": str(current or "").strip(),
                        "completed": completed_label,
                        "elapsed_ms": safe_elapsed_ms,
                    }
                )
            except Exception:
                logging.getLogger("zip_update").debug("falha ao publicar microetapa de preparação", exc_info=True)

        def mark(label: str) -> int:
            nonlocal last
            now = time.monotonic()
            elapsed_ms = max(0, int((now - last) * 1000))
            timings[label] = elapsed_ms
            last = now
            return elapsed_ms

        try:
            repo_name_hint = self._guess_repo_name(origin_url)
            publish_progress("Inspecionando pacote")
            worker_zip_validation = self._phone_worker_validate_zip_sync(zip_path)
            elapsed = mark("phone_worker_zip_validate_ms")
            publish_progress("Conferindo estrutura do ZIP", "Pacote inspecionado", elapsed)
            if worker_zip_validation and not bool(worker_zip_validation.get("ok", True)):
                errors = worker_zip_validation.get("errors") or []
                if isinstance(errors, list) and errors:
                    raise RuntimeError("ZIP bloqueado pelo phone-worker: " + "; ".join(str(item) for item in errors[:3]))
                raise RuntimeError("ZIP bloqueado pelo phone-worker")

            extracted_files, zip_inspection = self._safe_extract_patch(zip_path, extract_dir, repo_name_hint, branch_name)
            elapsed = mark("extract_ms")
            publish_progress("Preparando base de comparação", "Estrutura do ZIP conferida", elapsed)

            clone_dir = self._prepare_update_staging_clone(origin_url, branch_name, env)
            elapsed = mark("staging_prepare_ms")
            publish_progress("Comparando arquivos", "Base de comparação preparada", elapsed)

            changed_files = self._apply_patch_to_clone(extracted_files, clone_dir)
            elapsed = mark("apply_patch_ms")
            publish_progress("Calculando alterações", "Arquivos comparados", elapsed)
            if not changed_files:
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            self._run_cmd(["git", "config", "user.name", "Discord Auto Update"], clone_dir, env=env)
            self._run_cmd(["git", "config", "user.email", "discord-auto-update@local"], clone_dir, env=env)

            add_result = self._run_cmd(["git", "add", "--", *changed_files], clone_dir, env=env)
            if add_result.returncode != 0:
                err = (add_result.stderr or add_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar arquivos para commit. {err}")

            status_result = self._run_cmd(["git", "status", "--porcelain"], clone_dir, env=env)
            if status_result.returncode != 0:
                err = (status_result.stderr or status_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao verificar alterações do staging persistente. {err}")
            if not (status_result.stdout or "").strip():
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            diff_stats = self._git_diff_numstat_sync(clone_dir, env)
            patch_result = self._run_cmd(["git", "diff", "--cached", "--binary"], clone_dir, env=env)
            if patch_result.returncode != 0:
                err = (patch_result.stderr or patch_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao gerar patch de fila. {err}")
            patch_diff_text = patch_result.stdout or ""
            changed_files = [str(item.get("path")) for item in diff_stats.get("entries", []) if item.get("path")] or changed_files
            elapsed = mark("diff_ms")
            publish_progress("Montando candidato seguro", "Alterações calculadas", elapsed)

            base_result = self._run_cmd(["git", "rev-parse", f"origin/{branch_name}"], clone_dir, env=env)
            base_commit = (base_result.stdout or "").strip() if base_result.returncode == 0 else None

            candidate = self._write_local_update_candidate_sync(
                branch_name=branch_name,
                base_commit=base_commit,
                changed_files=changed_files,
                diff_stats=diff_stats,
                extracted_files=extracted_files,
                zip_name=zip_path.name,
                zip_inspection=zip_inspection,
                patch_diff_text=patch_diff_text,
                status_context=status_context,
                preparation_steps=list(preparation_steps),
                preparation_started_monotonic=t0,
                candidate_step_started_monotonic=last,
            )
            measured_elapsed = mark("candidate_write_ms")
            elapsed = max(0, int(candidate.get("candidate_prepare_elapsed_ms") or measured_elapsed))
            timings["candidate_write_ms"] = elapsed
            publish_progress("Finalizando preparação", "Candidato seguro preparado", elapsed)
            timings["total_ms"] = int((time.monotonic() - t0) * 1000)

            return {
                "changed_files": changed_files,
                "diff_stats": diff_stats,
                "commit_hash": None,
                "candidate_id": candidate.get("candidate_id"),
                "display_id": candidate.get("display_id"),
                "candidate_dir": candidate.get("candidate_dir"),
                "queue_position": candidate.get("queue_position"),
                "queue_pending_count": candidate.get("queue_pending_count"),
                "triggered_update": False,
                "trigger_detail": "aguardando acionamento após publicar a fila",
                "branch": branch_name,
                "phone_worker_zip_validation": worker_zip_validation,
                "zip_inspection": zip_inspection,
                "timings": timings,
                "staging_dir": str(clone_dir),
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


    def handle_internal_update_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "zip_status":
            future = asyncio.run_coroutine_threadsafe(self._edit_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action == "create_zip_status":
            future = asyncio.run_coroutine_threadsafe(self._create_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action != "reload_cogs":
            return {"ok": False, "error": "ação interna desconhecida"}
        modules_raw = payload.get("modules") if isinstance(payload, dict) else None
        if not isinstance(modules_raw, list):
            return {"ok": False, "error": "lista de módulos ausente"}
        modules = [str(item).strip() for item in modules_raw if str(item).strip()]
        if not modules:
            return {"ok": False, "error": "nenhuma cog para recarregar"}
        check_app_commands = bool(payload.get("check_app_commands")) if isinstance(payload, dict) else False
        future = asyncio.run_coroutine_threadsafe(
            self._reload_cogs_for_update(modules, check_app_commands=check_app_commands),
            self.loop,
        )
        return future.result(timeout=90 if check_app_commands else 25)

    async def _create_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        title = str(payload.get("title") or "Atualização").strip() or "Atualização"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        try:
            channel = self.get_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None or not hasattr(channel, "send"):
                return {"ok": False, "error": "canal de update indisponível"}
            view = self._make_zip_update_view(title, description, color)
            msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            return {"ok": True, "channel_id": str(getattr(channel, "id", self.ZIP_UPDATE_CHANNEL_ID)), "message_id": str(msg.id)}
        except Exception as exc:
            logging.getLogger("zip_update").warning("falha ao criar mensagem de update pelo updater", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    async def _edit_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            channel_id = int(str(payload.get("channel_id") or "0"))
            message_id = int(str(payload.get("message_id") or "0"))
        except Exception:
            return {"ok": False, "error": "channel_id/message_id inválido"}
        if not channel_id or not message_id:
            return {"ok": False, "error": "channel_id/message_id ausente"}

        title = str(payload.get("title") or "Atualização").strip() or "Atualização"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        control_raw = payload.get("control") if isinstance(payload, dict) else None
        control: dict[str, object] | None = None
        state_record: dict[str, object] | None = None
        old_state = self._zip_update_state_load()
        previous = old_state.get("latest") if isinstance(old_state.get("latest"), dict) else None

        def parse_event_at(value: object) -> datetime | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        now_event = datetime.now(timezone.utc)
        incoming_event = parse_event_at(payload.get("event_at")) or now_event
        previous_event = parse_event_at(previous.get("event_at") or previous.get("updated_at")) if isinstance(previous, dict) else None
        # Eventos com o mesmo timestamp são retries idempotentes. Eles precisam
        # tentar editar novamente caso a chamada anterior tenha falhado depois de
        # preparar o estado. Somente eventos estritamente mais antigos são stale.
        stale_event = bool(previous_event and incoming_event < previous_event)
        same_message = bool(
            isinstance(previous, dict)
            and str(previous.get("channel_id") or "") == str(channel_id)
            and str(previous.get("message_id") or "") == str(message_id)
        )
        if stale_event and same_message:
            return {"ok": True, "ignored": "estado final antigo"}

        if isinstance(control_raw, dict) and control_raw.get("enabled"):
            mode = str(control_raw.get("mode") or "rollback").lower().strip()
            if mode == "cancel":
                candidate_id = str(control_raw.get("candidate_id") or payload.get("candidate_id") or "").strip()
                control = self._zip_update_cancel_control(candidate_id, disabled=bool(control_raw.get("disabled")))
            else:
                token = str(control_raw.get("token") or uuid.uuid4().hex[:16])[:24]
                if mode not in {"rollback", "redo"}:
                    mode = "rollback"
                state_record = {
                    "token": token,
                    "mode": mode,
                    "channel_id": str(channel_id),
                    "message_id": str(message_id),
                    "title": title,
                    "description": description,
                    "status": status,
                    "branch": str(control_raw.get("branch") or payload.get("branch") or "main"),
                    "expected_head": str(control_raw.get("expected_head") or control_raw.get("head_commit") or ""),
                    "revert_commit": str(control_raw.get("revert_commit") or control_raw.get("head_commit") or ""),
                    "source_author_id": str(control_raw.get("source_author_id") or ""),
                    "event_at": incoming_event.isoformat(),
                    "updated_at": now_event.isoformat(),
                }
                for key in ("update_from", "update_to", "rollback_commit", "redo_commit"):
                    if control_raw.get(key):
                        state_record[key] = str(control_raw.get(key))
                control = self._zip_update_control_for_record(state_record, disabled=stale_event)

        clear_previous_control = bool(payload.get("clear_previous_control"))
        preserve_existing_control = bool(payload.get("preserve_existing_control"))
        final_without_control = bool(
            status in {"success", "ok", "warn", "error", "done", "failed"}
            and state_record is None
            and not stale_event
            and clear_previous_control
            and not preserve_existing_control
        )

        try:
            status_message = await self._zip_update_fetch_message(channel_id, message_id)
            if status_message is None:
                return {"ok": False, "error": "mensagem não encontrada"}
            view = self._make_zip_update_view(title, description, color, control=control)
            await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
            # O estado persistente só pode avançar depois que o Discord confirmou
            # a edição. A ordem antiga salvava antes do await; uma falha tornava o
            # retry "antigo" e o outbox removia a notificação sem nunca publicá-la.
            if state_record is not None and not stale_event:
                await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
                state_record["delivery_state"] = "delivered"
                state_record["delivered_at"] = datetime.now(timezone.utc).isoformat()
                if not self._zip_update_state_save({"latest": state_record}):
                    # Não deixe um botão ativo que o bot não conseguirá resolver
                    # depois de um restart. A confirmação final continua visível,
                    # porém sem oferecer uma ação de rollback inconsistente.
                    await status_message.edit(
                        view=self._make_zip_update_view(title, description, color),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return {
                        "ok": True,
                        "delivered": True,
                        "warning": "controle de rollback indisponível",
                    }
            elif final_without_control:
                # Um estado finalizado sem controle explícito deixa controles
                # antigos inativos, mas somente após a nova mensagem existir.
                await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
                self._zip_update_state_save({})
            return {"ok": True, "delivered": True}
        except Exception:
            logging.getLogger("zip_update").warning("falha ao finalizar mensagem do ZIP pelo updater", exc_info=True)
            return {"ok": False, "error": "falha ao editar mensagem"}

    def _zip_update_find_candidate_sync(self, candidate_id: str) -> dict[str, object]:
        candidate_id = str(candidate_id or "").strip()
        queue_root = self._update_staging_root / "candidates" / "queue"
        for state in ("pending", "active"):
            state_dir = queue_root / state
            for queue_file in sorted(state_dir.glob("*.json")) if state_dir.is_dir() else []:
                try:
                    payload = json.loads(queue_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("id") or "") != candidate_id:
                    continue
                candidate_dir = Path(str(payload.get("candidate_dir") or ""))
                manifest: dict[str, object] = {}
                try:
                    raw = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        manifest = raw
                except Exception:
                    pass
                return {
                    "state": state,
                    "queue_file": str(queue_file),
                    "candidate_dir": str(candidate_dir),
                    "manifest": manifest,
                }
        return {"state": "missing"}

    def _zip_update_cancel_candidate_sync(self, candidate_id: str) -> dict[str, object]:
        found = self._zip_update_find_candidate_sync(candidate_id)
        if found.get("state") != "pending":
            return found
        queue_file = Path(str(found.get("queue_file") or ""))
        candidate_dir = Path(str(found.get("candidate_dir") or ""))
        queue_cancelled = queue_file.parent.parent / "cancelled"
        candidate_cancelled = self._update_staging_root / "candidates" / "cancelled"
        queue_cancelled.mkdir(parents=True, exist_ok=True)
        candidate_cancelled.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d%H%M%S")
        destination = candidate_cancelled / f"{candidate_dir.name}.{stamp}"
        cancelled_queue = queue_cancelled / f"{queue_file.name}.cancelled.{stamp}"

        try:
            queue_payload = json.loads(queue_file.read_text(encoding="utf-8"))
            if not isinstance(queue_payload, dict):
                queue_payload = {}
        except Exception:
            queue_payload = {}
        queue_payload.update(
            {
                "state": "cancelled",
                "cancelled_at": now.isoformat(),
                "archived_candidate_dir": str(destination),
            }
        )
        tmp_queue = queue_file.with_name(f".{queue_file.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_queue.write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_queue, queue_file)
            os.replace(queue_file, cancelled_queue)
        except FileNotFoundError:
            with contextlib.suppress(Exception):
                tmp_queue.unlink(missing_ok=True)
            return self._zip_update_find_candidate_sync(candidate_id)
        except OSError as exc:
            with contextlib.suppress(Exception):
                tmp_queue.unlink(missing_ok=True)
            return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}

        if candidate_dir.is_dir():
            try:
                state_path = candidate_dir / "state.json"
                state_tmp = candidate_dir / f".state.json.{uuid.uuid4().hex}.tmp"
                state_tmp.write_text(
                    json.dumps({"state": "cancelled", "updated_at": now.isoformat()}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(state_tmp, state_path)
                os.replace(candidate_dir, destination)
                found["candidate_dir"] = str(destination)
            except OSError:
                shutil.rmtree(candidate_dir, ignore_errors=True)
        found["state"] = "cancelled"
        found["queue_file"] = str(cancelled_queue)
        return found


    async def _on_zip_update_control_click(self, interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        if not custom_id.startswith("zip_update:"):
            return
        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return
        requested_mode, token = parts[1], parts[2]

        if requested_mode == "cancel":
            found = await asyncio.to_thread(self._zip_update_find_candidate_sync, token)
            manifest = found.get("manifest") if isinstance(found.get("manifest"), dict) else {}
            if found.get("state") == "active":
                await interaction.response.send_message(
                    "Essa atualização já começou e não pode mais ser cancelada.", ephemeral=True
                )
                return
            if found.get("state") != "pending":
                await interaction.response.send_message(
                    "Essa atualização não está mais aguardando na fila.", ephemeral=True
                )
                return
            if not self._zip_update_can_control(interaction, manifest):
                await interaction.response.send_message("Você não pode cancelar esta atualização.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=False)
            cancelled = await asyncio.to_thread(self._zip_update_cancel_candidate_sync, token)
            if cancelled.get("state") != "cancelled":
                await interaction.followup.send(
                    "A atualização já foi assumida pelo updater e não pôde ser cancelada.", ephemeral=True
                )
                return
            display_id = str(manifest.get("display_id") or token)
            try:
                if interaction.message is not None:
                    await interaction.message.edit(
                        view=self._make_zip_update_view(
                            "✖️ Atualização cancelada",
                            f"`{display_id}`\nO pacote foi removido da fila antes de alterar a VPS.",
                            discord.Color.dark_grey(),
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception:
                UPDATE_LOG.warning("falha ao atualizar mensagem de candidato cancelado", exc_info=True)
            await asyncio.to_thread(self._trigger_updater_service_sync)
            await interaction.followup.send("Atualização cancelada.", ephemeral=True)
            return

        if requested_mode not in {"rollback", "redo"}:
            return
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if str(record.get("mode") or "") != requested_mode:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if not self._zip_update_can_control(interaction, record):
            await interaction.response.send_message("Você não pode controlar esta atualização.", ephemeral=True)
            return

        if requested_mode == "redo":
            current_commit = str(record.get("rollback_commit") or record.get("expected_head") or "")
            target_commit = str(record.get("update_to") or record.get("redo_commit") or "")
        else:
            current_commit = str(record.get("expected_head") or record.get("update_to") or "")
            target_commit = str(record.get("update_from") or "")
        await interaction.response.send_message(
            view=self._make_zip_update_confirmation_view(
                mode=requested_mode,
                token=token,
                current_commit=current_commit,
                target_commit=target_commit,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_zip_update_confirmation_click(self, interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        parts = custom_id.split(":", 2)
        if len(parts) != 3 or parts[0] != "zip_update_confirm":
            return
        requested_mode, token = parts[1], parts[2]
        if requested_mode == "cancel":
            await interaction.response.send_message("Ação cancelada.", ephemeral=True)
            return
        if requested_mode not in {"rollback", "redo"}:
            return
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if str(record.get("mode") or "") != requested_mode or not self._zip_update_can_control(interaction, record):
            await interaction.response.send_message("Você não pode executar esta ação.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._start_zip_update_rollback_flow(interaction, record, requested_mode)

    async def _start_zip_update_rollback_flow(self, interaction: discord.Interaction, record: dict[str, object], mode: str) -> None:
        mode = "redo" if str(mode).lower().strip() == "redo" else "rollback"
        branch = str(record.get("branch") or "main")
        channel_id, message_id = self._zip_update_latest_message_ref(interaction, record)
        update_from = str(record.get("update_from") or "").strip()
        update_to = str(record.get("update_to") or "").strip()
        rollback_commit = str(record.get("rollback_commit") or "").strip()
        redo_commit = str(record.get("redo_commit") or "").strip()
        expected_head = str(record.get("expected_head") or "").strip()
        revert_commit = str(record.get("revert_commit") or "").strip()

        # O controle usa `git revert`, então o alvo técnico da ação é o commit
        # que precisa ser revertido, não o commit para onde queremos "voltar".
        # Para ◀️, o HEAD esperado é o update aplicado e o commit revertido é
        # esse próprio update. Para ▶️, o HEAD esperado é o commit de rollback e
        # o commit revertido é o rollback. Mantemos fallback explícito para
        # estados antigos/legados já salvos em disco.
        if mode == "rollback":
            # O commit técnico a reverter é sempre o HEAD atual salvo no controle.
            # `update_to` é mantido como metadado do update original e pode ficar
            # diferente depois de uma reaplicação; por isso ele só serve como
            # fallback para estados antigos que ainda não gravavam expected_head.
            expected_head = expected_head or revert_commit or update_to
            revert_commit = revert_commit or expected_head or update_to
        else:
            # Para refazer, revertemos o commit de rollback. Estados novos salvam
            # `rollback_commit`; em estados legados, `revert_commit`/expected_head
            # apontam para o commit atual que precisa ser revertido.
            expected_head = rollback_commit or expected_head or revert_commit or update_to
            revert_commit = rollback_commit or revert_commit or redo_commit or expected_head

        commit_re = re.compile(r"^[0-9a-fA-F]{7,40}$")
        if expected_head and not commit_re.fullmatch(expected_head):
            UPDATE_LOG.warning("rollback/redo com expected_head inválido: %r", expected_head)
            expected_head = ""
        if revert_commit and not commit_re.fullmatch(revert_commit):
            UPDATE_LOG.warning("rollback/redo com revert_commit inválido: %r", revert_commit)
            revert_commit = ""

        if not expected_head or not revert_commit or not channel_id or not message_id:
            try:
                await interaction.followup.send("Não encontrei os dados necessários para essa ação.", ephemeral=True)
            except Exception:
                pass
            return

        if self._zip_update_has_pending_rollback_request():
            try:
                await interaction.followup.send("Já existe uma ação de update em andamento.", ephemeral=True)
            except Exception:
                pass
            return

        request_root, permission_details = self._zip_update_find_writable_rollback_request_root()
        if request_root is None:
            detail = "; ".join(permission_details[-3:]) or "nenhuma pasta gravável encontrada"
            UPDATE_LOG.warning("falha ao preparar pasta de pedido de rollback/redo: %s", detail)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: pasta de controle sem permissão de escrita.", ephemeral=True)
            except Exception:
                pass
            return

        pending = request_root / "pending.json"
        active = request_root / "active.json"

        request_id = f"{mode}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": request_id,
            "mode": mode,
            "branch": branch,
            "expected_head": expected_head,
            "revert_commit": revert_commit,
            "update_from": update_from,
            "update_to": update_to,
            "rollback_commit": rollback_commit,
            "redo_commit": redo_commit,
            "message": {"channel_id": channel_id, "message_id": message_id},
            "source_author_id": str(record.get("source_author_id") or ""),
            "requested_by": str(getattr(interaction.user, "id", "") or ""),
            "previous_record": record,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        tmp = pending.with_name(f"pending.{request_id}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, pending)
            # Confirma que o pedido existe antes de mexer visualmente na mensagem.
            saved = json.loads(pending.read_text(encoding="utf-8"))
            if str(saved.get("id") or "") != request_id:
                raise RuntimeError("pedido de rollback não foi salvo corretamente")
        except Exception:
            with contextlib.suppress(Exception):
                tmp.unlink()
            UPDATE_LOG.warning("falha ao salvar pedido de rollback/redo em %s", request_root, exc_info=True)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: não foi possível gravar o pedido de controle.", ephemeral=True)
            except Exception:
                pass
            return

        title = "<a:areia:1496606578395189473> Reaplicando update..." if mode == "redo" else "<a:areia:1496606578395189473> Revertendo update..."
        description = "<a:loading:1510065277868445796> **Preparando validação local**"
        processing_record = dict(record)
        processing_record["status"] = "processing"
        processing_record["processing_mode"] = mode
        processing_record["processing_request_id"] = request_id
        processing_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        state = self._zip_update_state_load()
        state["latest"] = processing_record
        state["active_request"] = payload
        self._zip_update_state_save(state)

        try:
            msg = await self._zip_update_fetch_message(int(channel_id), int(message_id))
            if msg is not None:
                view = self._make_zip_update_view(
                    title,
                    description,
                    discord.Color.gold(),
                    control=self._zip_update_control_for_record(record, disabled=True),
                )
                await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            UPDATE_LOG.warning("falha ao colocar update em estado de processamento", exc_info=True)

        triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
        if not triggered:
            UPDATE_LOG.warning("rollback/redo aguardando timer do updater: %s", detail)

        async def _rollback_start_watchdog() -> None:
            await asyncio.sleep(90)
            try:
                # Se o pedido ainda existir igual após um tempo razoável, o updater não consumiu.
                for path in (pending, active):
                    if not path.exists():
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if str(data.get("id") or "") != request_id:
                        continue
                    msg = await self._zip_update_fetch_message(int(channel_id), int(message_id))
                    if msg is not None:
                        fail_title = "Falha ao reaplicar" if mode == "redo" else "Falha ao reverter"
                        fail_desc = "A ação não avançou. O estado atual foi mantido."
                        await msg.edit(
                            view=self._make_zip_update_view(
                                fail_title,
                                fail_desc,
                                discord.Color.red(),
                                control=self._zip_update_control_for_record(record),
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    # Restaura o estado para permitir nova tentativa manual, mas não apaga o arquivo;
                    # o updater ainda poderá arquivar/limpar no próximo ciclo se necessário.
                    state = self._zip_update_state_load()
                    state.pop("active_request", None)
                    state["latest"] = record
                    self._zip_update_state_save(state)
                    return
            except Exception:
                UPDATE_LOG.warning("falha no watchdog de rollback/redo", exc_info=True)

        asyncio.create_task(_rollback_start_watchdog())

    def _schedule_update_interaction_notice(self, interaction: discord.Interaction) -> None:
        presence = getattr(self, "application_presence", None)
        if presence is None or not bool(getattr(presence, "maintenance_active", False)):
            return
        interaction_type = getattr(discord.InteractionType, "application_command", None)
        if interaction_type is None or getattr(interaction, "type", None) != interaction_type:
            return

        user_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        if user_id <= 0:
            return
        try:
            cooldown = max(30.0, min(600.0, float(os.getenv("UPDATE_NOTICE_COOLDOWN_SECONDS", "120") or 120)))
        except (TypeError, ValueError):
            cooldown = 120.0
        now = time.monotonic()
        if now - self._update_notice_by_user.get(user_id, 0.0) < cooldown:
            return
        self._update_notice_by_user[user_id] = now
        asyncio.create_task(
            self._send_update_interaction_notice(interaction, user_id, now),
            name=f"update-notice-{user_id}",
        )

    async def _send_update_interaction_notice(
        self,
        interaction: discord.Interaction,
        user_id: int,
        reservation: float,
    ) -> None:
        try:
            for _ in range(25):
                if interaction.response.is_done():
                    break
                await asyncio.sleep(0.1)
            if not interaction.response.is_done():
                if self._update_notice_by_user.get(user_id) == reservation:
                    self._update_notice_by_user.pop(user_id, None)
                return
            presence = getattr(self, "application_presence", None)
            if presence is None or not bool(getattr(presence, "maintenance_active", False)):
                return
            await interaction.followup.send(
                "⚠️ Atualização em andamento. A resposta pode demorar.",
                ephemeral=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._update_notice_by_user.get(user_id) == reservation:
                self._update_notice_by_user.pop(user_id, None)
            UPDATE_LOG.debug("falha ao enviar aviso curto de atualização", exc_info=True)

    async def on_interaction(self, interaction: discord.Interaction):
        self._schedule_update_interaction_notice(interaction)
        try:
            custom_id = ""
            data = getattr(interaction, "data", None)
            if isinstance(data, dict):
                custom_id = str(data.get("custom_id") or "")
            if custom_id.startswith("zip_update_confirm:"):
                await self._on_zip_update_confirmation_click(interaction)
            elif custom_id.startswith("zip_update:"):
                await self._on_zip_update_control_click(interaction)
        except Exception:
            UPDATE_LOG.exception("falha ao processar controle de update")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Falha ao processar esta ação.", ephemeral=True)
            except Exception:
                pass

    async def _reload_cogs_for_update(self, modules: list[str], *, check_app_commands: bool = False) -> dict[str, object]:
        results: list[dict[str, str]] = []
        for module in modules:
            module = _normalize_extension_name(module)
            try:
                if module in self.extensions:
                    await self.reload_extension(module)
                    results.append({"module": module, "status": "reloaded"})
                else:
                    await self.load_extension(module)
                    results.append({"module": module, "status": "loaded"})
            except Exception as exc:
                logging.getLogger("zip_update").exception("falha ao recarregar cog %s pelo updater", module)
                return {"ok": False, "error": f"{module}: {type(exc).__name__}: {str(exc)[:300]}", "results": results}
        app_commands_status: dict[str, object] | None = None
        if check_app_commands:
            guild_ids = self._resolve_app_command_sync_guild_ids()
            await self._cleanup_removed_slash_commands_if_needed(guild_ids)
            app_commands_status = await self._smart_sync_app_commands(
                guild_ids,
                should_sync=self._app_command_sync_enabled(),
                allow_global_sync=self._app_command_global_sync_allowed(),
                clear_globals_allowed=_env_truthy("CLEAR_GLOBAL_COMMANDS"),
                trigger="reload_cogs",
            )
        response: dict[str, object] = {"ok": True, "results": results}
        if app_commands_status is not None:
            response["app_commands"] = app_commands_status
        return response


    async def _handle_zip_update_message(self, message: discord.Message):
        if not self._zip_update_can_submit(message):
            await self._send_zip_update_message(
                message,
                "❌ Atualização não autorizada",
                "Você não tem permissão para enviar atualizações por este canal.\n-# Nenhum arquivo foi processado.",
                discord.Color.red(),
            )
            return

        zip_attachments = [
            attachment
            for attachment in message.attachments
            if str(getattr(attachment, "filename", "")).lower().endswith(".zip")
        ]
        if not zip_attachments:
            await self._send_zip_update_message(
                message,
                "❌ Arquivo inválido",
                "Envie um arquivo **.zip** neste canal.",
                discord.Color.red(),
            )
            return

        limits = self._zip_update_limits()
        total_zips = len(zip_attachments)
        for index, zip_attachment in enumerate(zip_attachments, start=1):
            prefix = f"ZIP {index}/{total_zips}" if total_zips > 1 else ""
            zip_hint = f"\n-# {prefix}" if prefix else ""
            attachment_size = int(getattr(zip_attachment, "size", 0) or 0)
            if attachment_size > limits.max_archive_bytes:
                await self._send_zip_update_message(
                    message,
                    "❌ Arquivo muito grande",
                    (
                        f"O ZIP excede o limite de {limits.max_archive_bytes // (1024 * 1024)} MB."
                        + zip_hint
                    ),
                    discord.Color.red(),
                )
                continue

            status_message: discord.Message | None = await self._send_zip_update_message(
                message,
                "📦 Atualização recebida",
                (
                    f"**Conferindo o anexo**\n"
                    f"A estrutura e a segurança serão validadas antes de alterar a VPS.{zip_hint}"
                ),
                discord.Color.blurple(),
            )

            async with self._zip_update_lock:
                self._update_temp_root.mkdir(parents=True, exist_ok=True)
                work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-msg-", dir=str(self._update_temp_root)))
                safe_zip_name = Path(str(zip_attachment.filename or "update.zip")).name or "update.zip"
                zip_path = work_dir / safe_zip_name
                try:
                    await zip_attachment.save(zip_path)
                    if zip_path.stat().st_size > limits.max_archive_bytes:
                        raise UpdateSecurityError(
                            f"ZIP excede o limite de {limits.max_archive_bytes // (1024 * 1024)} MB"
                        )
                    status_message = await self._edit_zip_update_message(
                        message,
                        status_message,
                        "🔎 Preparando atualização",
                        (
                            "<a:loading:1510065277868445796> **Validando pacote**\n"
                            "-# A mensagem será atualizada em cada etapa." + zip_hint
                        ),
                        discord.Color.blurple(),
                    )

                    status_context = {
                        "guild_id": getattr(message.guild, "id", None),
                        "channel_id": getattr(status_message.channel, "id", None) if status_message else getattr(message.channel, "id", None),
                        "message_id": getattr(status_message, "id", None),
                        "source_message_id": getattr(message, "id", None),
                        "source_author_id": getattr(message.author, "id", None),
                    }
                    progress_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
                    preparation_history: list[str] = []

                    def format_elapsed_ms(value: object) -> str:
                        try:
                            elapsed_ms = max(0, int(value or 0))
                        except (TypeError, ValueError):
                            elapsed_ms = 0
                        if elapsed_ms <= 0:
                            return "<1ms"
                        if elapsed_ms < 1000:
                            return f"{elapsed_ms}ms"
                        if elapsed_ms < 60000:
                            seconds = elapsed_ms / 1000
                            text = f"{seconds:.1f}".rstrip("0").rstrip(".").replace(".", ",")
                            return f"{text}s"
                        total_seconds = round(elapsed_ms / 1000)
                        minutes, seconds = divmod(total_seconds, 60)
                        return f"{minutes}min {seconds:02d}s"

                    async def consume_preparation_progress() -> None:
                        progress_edits_enabled = True
                        while True:
                            event = await progress_queue.get()
                            if event is None:
                                return
                            completed = str(event.get("completed") or "").strip()
                            current = str(event.get("current") or "Processando pacote").strip()
                            if completed:
                                preparation_history.append(
                                    f"-# ✅ {completed} · {format_elapsed_ms(event.get('elapsed_ms'))}"
                                )
                            visible_history = preparation_history[-8:]
                            hidden_count = max(0, len(preparation_history) - len(visible_history))
                            lines: list[str] = []
                            if hidden_count:
                                noun = "etapa anterior concluída" if hidden_count == 1 else "etapas anteriores concluídas"
                                lines.append(f"-# … {hidden_count} {noun}")
                            lines.extend(visible_history)
                            lines.append(f"<a:loading:1510065277868445796> **{current}**")
                            if prefix:
                                lines.append(f"-# {prefix}")
                            view = self._make_zip_update_view(
                                "🔎 Preparando atualização",
                                "\n".join(lines),
                                discord.Color.blurple(),
                            )
                            try:
                                await asyncio.wait_for(
                                    status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none()),
                                    timeout=15,
                                )
                            except Exception:
                                UPDATE_LOG.warning("falha ao editar microetapa de preparação", exc_info=True)

                    progress_consumer = asyncio.create_task(consume_preparation_progress())

                    def report_preparation_progress(event: dict[str, object]) -> None:
                        self.loop.call_soon_threadsafe(progress_queue.put_nowait, event)

                    try:
                        result = await asyncio.to_thread(
                            self._process_zip_update_sync,
                            zip_path,
                            status_context,
                            report_preparation_progress,
                        )
                    finally:
                        progress_queue.put_nowait(None)
                        await progress_consumer
                    changed_files = list(result.get("changed_files") or [])
                    if not changed_files:
                        await self._edit_zip_update_message(
                            message,
                            status_message,
                            "ℹ️ Nenhuma alteração",
                            f"O pacote é válido, mas não altera o repositório.{zip_hint}",
                            discord.Color.gold(),
                        )
                        continue

                    diff_stats = result.get("diff_stats") if isinstance(result.get("diff_stats"), dict) else {}
                    diff_summary = str(diff_stats.get("summary") or "").strip()
                    queue_position = max(1, int(result.get("queue_position") or 1))
                    candidate_id = str(result.get("candidate_id") or "").strip()
                    display_id = str(result.get("display_id") or candidate_id).strip()

                    file_count = len(changed_files)
                    file_text = "1 arquivo preparado" if file_count == 1 else f"{file_count} arquivos preparados"
                    file_summary = f"{file_text} · {diff_summary}" if diff_summary else file_text

                    if queue_position <= 1:
                        # O primeiro item não está esperando outro update. Mantenha o
                        # painel de microetapas e faça a transição direta para o updater.
                        visible_history = preparation_history[-8:]
                        hidden_count = max(0, len(preparation_history) - len(visible_history))
                        details: list[str] = []
                        if hidden_count:
                            noun = "etapa anterior concluída" if hidden_count == 1 else "etapas anteriores concluídas"
                            details.append(f"-# … {hidden_count} {noun}")
                        details.extend(visible_history)
                        details.append("<a:loading:1510065277868445796> **Iniciando atualização**")
                        details.append(f"-# `{display_id}` · {file_summary}")
                        if prefix:
                            details.append(f"-# {prefix}")
                        status_message = await self._edit_zip_update_message(
                            message,
                            status_message,
                            "🔎 Preparando atualização",
                            "\n".join(details),
                            discord.Color.blurple(),
                            control=self._zip_update_cancel_control(candidate_id),
                        )
                    else:
                        before = queue_position - 1
                        before_text = "1 atualização antes desta" if before == 1 else f"{before} atualizações antes desta"
                        details = [
                            f"Atualização `{display_id}`",
                            f"Posição na fila: **{queue_position}**",
                            f"{before_text}.",
                            f"-# {file_summary}",
                        ]
                        if prefix:
                            details.append(f"-# {prefix}")
                        status_message = await self._edit_zip_update_message(
                            message,
                            status_message,
                            "📦 Atualização na fila",
                            "\n".join(details),
                            discord.Color.blurple(),
                            control=self._zip_update_cancel_control(candidate_id),
                        )
                    await asyncio.to_thread(self._trigger_updater_service_sync)
                except zipfile.BadZipFile:
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ ZIP inválido",
                        f"O arquivo não pôde ser aberto como um ZIP válido.{zip_hint}",
                        discord.Color.red(),
                    )
                except (UpdateSecurityError, RuntimeError) as exc:
                    reason = str(exc).strip() or type(exc).__name__
                    if len(reason) > 600:
                        reason = reason[:597].rstrip() + "..."
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ Atualização rejeitada",
                        f"Nenhuma alteração foi aplicada.\n**Motivo:** {reason}{zip_hint}",
                        discord.Color.red(),
                    )
                except Exception as exc:
                    UPDATE_LOG.exception("Falha no auto-update via ZIP do Discord")
                    reason = str(exc).strip() or type(exc).__name__
                    if len(reason) > 600:
                        reason = reason[:597].rstrip() + "..."
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "❌ Falha ao preparar atualização",
                        f"Nenhuma alteração foi aplicada.\n**Motivo:** {reason}{zip_hint}",
                        discord.Color.red(),
                    )
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

    async def close(self):
        if self._zip_update_reconcile_task is not None:
            self._zip_update_reconcile_task.cancel()
        if self._event_loop_watchdog_task is not None:
            self._event_loop_watchdog_task.cancel()
        if self._health_task is not None:
            self._health_task.cancel()
        if self._music_startup_reconcile_task is not None:
            self._music_startup_reconcile_task.cancel()
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            try:
                await application_presence.close()
            except Exception as e:
                print(f"[bot] falha ao fechar application_presence: {e!r}")
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            try:
                await application_bio.close()
            except Exception as e:
                print(f"[bot] falha ao fechar application_bio: {e!r}")
        router = getattr(self, "audio_router", None)
        if router is not None:
            try:
                await router.close()
            except Exception as e:
                print(f"[bot] falha ao fechar audio_router: {e!r}")
        await super().close()


    def _schedule_music_startup_reconcile(self) -> None:
        """Agenda restaurações de música fora do caminho crítico do on_ready.

        Restore de bitrate/status pode fazer I/O/REST por guild. Rodar isso
        diretamente no on_ready atrasava o heartbeat do Discord em restart
        comum. O reconciler abaixo é idempotente e roda em background.
        """
        task = getattr(self, "_music_startup_reconcile_task", None)
        if task is not None and not task.done():
            return
        if self._music_bitrate_reconciled and self._music_voice_status_reconciled:
            return
        self._music_startup_reconcile_task = asyncio.create_task(self._run_music_startup_reconcile())

    async def _run_music_startup_reconcile(self) -> None:
        try:
            # Dá tempo para o gateway estabilizar e para o bot responder
            # heartbeat antes de qualquer REST/DB extra pós-restart.
            await asyncio.sleep(max(1.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_DELAY_SECONDS", 12.0) or 12.0)))
            router = getattr(self, "audio_router", None)
            if router is None:
                return
            if not self._music_bitrate_reconciled and hasattr(router, "reconcile_auto_bitrate_records"):
                self._music_bitrate_reconciled = True
                try:
                    await router.reconcile_auto_bitrate_records()
                except Exception as e:
                    logging.getLogger("music").debug("reconciliação de bitrate automático falhou: %r", e, exc_info=True)
            # Pequeno intervalo entre categorias para não concentrar chamadas REST
            # num único tick de startup.
            await asyncio.sleep(max(0.0, float(getattr(config, "MUSIC_STARTUP_RESTORE_STEP_DELAY_SECONDS", 0.75) or 0.75)))
            if not self._music_voice_status_reconciled and hasattr(router, "reconcile_voice_status_records"):
                self._music_voice_status_reconciled = True
                try:
                    await router.reconcile_voice_status_records()
                except Exception as e:
                    logging.getLogger("music").debug("reconciliação de status de canal falhou: %r", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("music").debug("reconciliação pós-startup de música falhou", exc_info=True)

    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.start()
        self._schedule_music_startup_reconcile()
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor_loop())
        if self._event_loop_watchdog_task is None or self._event_loop_watchdog_task.done():
            self._event_loop_watchdog_task = asyncio.create_task(self._event_loop_watchdog_loop())
        if self._zip_update_reconcile_task is None or self._zip_update_reconcile_task.done():
            self._zip_update_reconcile_task = asyncio.create_task(self._zip_update_reconcile_loop())
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.start()

    async def on_guild_join(self, guild: discord.Guild):
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.schedule_refresh()
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.schedule_sync("guild_join")

    async def on_guild_remove(self, guild: discord.Guild):
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.schedule_refresh()
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.schedule_sync("guild_remove")

    async def _dispatch_tts_message_bridge(self, message: discord.Message) -> None:
        # Regressão defensiva: o TTS depende de on_message para transformar
        # mensagens prefixadas em fila de síntese. Como bot.py também define
        # on_message para o updater/comandos, chamamos o cog explicitamente aqui
        # para não depender só do fan-out interno de listeners. O cog deduplica
        # pelo ID da mensagem, então se o listener normal também rodar não há
        # enqueue duplicado.
        tts_cog = self.get_cog("TTSVoice")
        handler = getattr(tts_cog, "handle_message_from_bot_on_message", None) if tts_cog is not None else None
        if not callable(handler):
            return
        try:
            await handler(message)
        except Exception as exc:
            logging.getLogger("tts.bridge").exception(
                "[tts_bridge] falha ao encaminhar mensagem para TTS | guild=%s channel=%s user=%s erro=%r",
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "channel", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
                exc,
            )

    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        try:
            if int(getattr(message.channel, "id", 0)) == self.ZIP_UPDATE_CHANNEL_ID:
                await self._handle_zip_update_message(message)
                return
            await self._dispatch_tts_message_bridge(message)
            await self.process_commands(message)
        except Exception as e:
            print(f"[bot] falha ao processar comandos: {e!r}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return

        if hasattr(ctx.command, "on_error"):
            return

        cog = ctx.cog
        if cog is not None:
            overridden = cog._get_overridden_method(cog.cog_command_error)
            if overridden is not None:
                return

        logger = logging.getLogger("discord.ext.commands.bot")
        logger.error("Ignoring exception in command %s", ctx.command, exc_info=error)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        root = getattr(error, "original", None) or getattr(error, "__cause__", None) or error
        if is_unknown_interaction(root):
            logging.getLogger("discord.app_commands.tree").warning(
                "Comando %s expirou antes da primeira resposta: %s",
                getattr(getattr(interaction, "command", None), "name", "?"),
                root,
            )
            return
        print(f"[APP_COMMAND_ERROR] {error!r}")
        ok = await safe_send_interaction_message(
            interaction,
            f"Erro ao executar o comando: {error}",
            ephemeral=True,
            log=logging.getLogger("discord.app_commands.tree"),
            label="bot.app_command_error",
        )
        if not ok:
            print("[APP_COMMAND_ERROR] Falha ao responder ao usuário: token/interaction indisponível")


async def main():
    print("MAIN INICIOU")

    web_thread = threading.Thread(target=run_webserver, daemon=True)
    web_thread.start()

    bot = BotLocal()
    try:
        await bot.start(config.TOKEN)
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        BOOT_LOG.info("Encerrado manualmente")
