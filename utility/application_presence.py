from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord


LOG = logging.getLogger("bot.application_presence")

VARIABLE_RE = re.compile(r"\{n:([a-z0-9_.:-]+)\}", flags=re.IGNORECASE)
GUILD_VARIABLES = {"sv", "guilds", "m", "members"}

MIN_STATUS_DURATION_SECONDS = 30
MAX_STATUS_DURATION_SECONDS = 24 * 60 * 60
MAX_STATUS_COUNT = 20
MAX_STATUS_TEXT_LENGTH = 128

DEFAULT_STATUS_TEMPLATES = (
    "「👥 」_𝗵𝗲𝗹𝗽 • {n:m} usuários",
    "「🌐」_𝗵𝗲𝗹𝗽 • {n:sv} servidores",
)


class ApplicationPresenceService:
    """Gerencia a presença do bot com rotação configurável e custo mínimo.

    A configuração é mantida em memória e persistida somente quando o dono do
    bot realmente altera algo. O scheduler usa uma única ``asyncio.Task`` e
    dorme até o próximo prazo ou até uma mudança/evento relevante.
    """

    CONFIG_VERSION = 2

    def __init__(
        self,
        bot: discord.Client,
        update_state_path: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._config_lock = asyncio.Lock()

        self._current_id: str | None = None
        self._current_started_at: float | None = None
        self._current_deadline: float | None = None
        self._next_index_hint = 0
        self._paused_remaining: float | None = None
        self._maintenance_elapsed: float | None = None

        self._last_text: str | None = None
        self._last_presence_status: discord.Status | None = None
        self._last_update_at = 0.0
        self._last_event_schedule_at = 0.0
        self._maintenance_active = False
        self._retry_apply_at: float | None = None

        self._update_state_path = update_state_path or Path(
            os.getenv(
                "DISCORD_AUTO_UPDATE_RUNTIME_STATE_FILE",
                "/home/ubuntu/bot-update-staging/candidates/runtime-state.json",
            )
        )
        self._config_path = config_path or Path(__file__).resolve().parents[1] / "data" / "application_presence.json"
        self._config = self._load_config()

    @property
    def enabled(self) -> bool:
        raw = str(os.getenv("APPLICATION_PRESENCE_ENABLED", "true") or "true").strip().lower()
        return raw not in {"0", "false", "no", "n", "off", "nao", "não"}

    @property
    def maintenance_active(self) -> bool:
        return self._maintenance_active

    @property
    def rotation_enabled(self) -> bool:
        return bool(self._config.get("enabled", True))

    def start(self) -> None:
        if not self.enabled:
            return
        started_new = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="application-presence-service")
            started_new = True
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="application-presence-maintenance",
            )
        if not started_new:
            self._wake_event.set()

    async def close(self) -> None:
        tasks = [task for task in (self._task, self._maintenance_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.debug("falha ao encerrar serviço de presença", exc_info=True)

    def schedule_refresh(self, *, immediate: bool = False) -> None:
        """Atualiza variáveis do status atual sem avançar a rotação.

        Eventos de entrada/saída de servidor só acordam o scheduler quando o
        texto atual realmente depende da contagem de servidores ou membros.
        """
        if not self.enabled or self._maintenance_active:
            return
        current = self._current_status()
        if current is None:
            return
        tokens = self._template_tokens(str(current.get("text") or ""))
        if not (tokens & GUILD_VARIABLES):
            return

        now = time.monotonic()
        if not immediate:
            debounce = self._env_float(
                "APPLICATION_PRESENCE_EVENT_DEBOUNCE_SECONDS",
                60.0,
                minimum=10.0,
                maximum=1800.0,
            )
            if now - self._last_event_schedule_at < debounce:
                return
            self._last_event_schedule_at = now
        self._wake_event.set()

    def get_snapshot(self) -> dict[str, Any]:
        statuses = copy.deepcopy(self._statuses())
        now = time.monotonic()
        current = self._status_by_id(self._current_id)
        current_rendered = self._render_template(str(current.get("text") or "")) if current else ""

        remaining: float | None = None
        if self._maintenance_active and self._maintenance_elapsed is not None and current is not None:
            duration = float(current.get("duration_seconds") or MIN_STATUS_DURATION_SECONDS)
            remaining = max(0.0, duration - self._maintenance_elapsed)
        elif self._current_deadline is not None:
            remaining = max(0.0, self._current_deadline - now)
        elif self._paused_remaining is not None:
            remaining = max(0.0, self._paused_remaining)

        return {
            "enabled": self.rotation_enabled,
            "maintenance_active": self._maintenance_active,
            "statuses": statuses,
            "current_id": self._current_id,
            "current": copy.deepcopy(current) if current else None,
            "current_rendered": current_rendered,
            "remaining_seconds": remaining,
            "config_path": str(self._config_path),
        }

    async def add_status(self, text: str, duration_seconds: int) -> str:
        text = self._validate_text(text)
        duration = self._validate_duration(duration_seconds)
        async with self._config_lock:
            statuses = self._statuses()
            if len(statuses) >= MAX_STATUS_COUNT:
                raise ValueError(f"Você pode ter no máximo {MAX_STATUS_COUNT} status.")
            old_config = copy.deepcopy(self._config)
            status_id = self._new_status_id()
            statuses.append({"id": status_id, "text": text, "duration_seconds": duration})
            try:
                await self._save_config()
            except Exception:
                self._config = old_config
                raise
        self._wake_event.set()
        return status_id

    async def update_status(
        self,
        status_id: str,
        *,
        text: str,
        duration_seconds: int,
        position: int | None = None,
    ) -> None:
        text = self._validate_text(text)
        duration = self._validate_duration(duration_seconds)
        async with self._config_lock:
            statuses = self._statuses()
            index = self._status_index(status_id)
            if index is None:
                raise ValueError("Esse status não existe mais.")
            old_config = copy.deepcopy(self._config)
            item = statuses[index]
            item["text"] = text
            item["duration_seconds"] = duration
            if position is not None and statuses:
                target = max(0, min(len(statuses) - 1, int(position) - 1))
                if target != index:
                    moved = statuses.pop(index)
                    statuses.insert(target, moved)
            try:
                await self._save_config()
            except Exception:
                self._config = old_config
                raise
        self._wake_event.set()

    async def remove_status(self, status_id: str) -> None:
        async with self._config_lock:
            statuses = self._statuses()
            index = self._status_index(status_id)
            if index is None:
                raise ValueError("Esse status não existe mais.")
            old_config = copy.deepcopy(self._config)
            was_current = self._current_id == status_id
            statuses.pop(index)
            try:
                await self._save_config()
            except Exception:
                self._config = old_config
                raise

            if was_current:
                self._current_id = None
                self._current_started_at = None
                self._current_deadline = None
                self._paused_remaining = None
                self._next_index_hint = min(index, max(0, len(statuses) - 1))
        self._wake_event.set()

    async def set_rotation_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.rotation_enabled:
            return

        now = time.monotonic()
        async with self._config_lock:
            old_config = copy.deepcopy(self._config)
            old_paused_remaining = self._paused_remaining
            old_started = self._current_started_at
            old_deadline = self._current_deadline
            self._config["enabled"] = enabled

            if not enabled:
                self._paused_remaining = (
                    max(0.0, self._current_deadline - now) if self._current_deadline is not None else None
                )
                self._current_deadline = None
            else:
                current = self._current_status()
                if current is not None:
                    duration = float(current.get("duration_seconds") or MIN_STATUS_DURATION_SECONDS)
                    remaining = self._paused_remaining
                    if remaining is None:
                        remaining = duration
                    remaining = max(0.0, min(duration, remaining))
                    elapsed = max(0.0, duration - remaining)
                    self._current_started_at = now - elapsed
                    self._current_deadline = now + remaining
                self._paused_remaining = None

            try:
                await self._save_config()
            except Exception:
                self._config = old_config
                self._paused_remaining = old_paused_remaining
                self._current_started_at = old_started
                self._current_deadline = old_deadline
                raise
        self._wake_event.set()

    async def apply_next_status(self, *, reason: str = "manual") -> bool:
        """Compatibilidade: avança uma posição e tenta aplicá-la imediatamente."""
        if not self.enabled or self._maintenance_active:
            return False
        statuses = self._statuses()
        if not statuses:
            return await self._apply_presence(None, discord.Status.online, reason=reason, force=False)

        now = time.monotonic()
        current_index = self._status_index(self._current_id)
        next_index = 0 if current_index is None else (current_index + 1) % len(statuses)
        self._activate_index(next_index, now)
        applied = await self._apply_current(reason=reason)
        self._wake_event.set()
        return applied

    async def _run_loop(self) -> None:
        startup_delay = self._env_float(
            "APPLICATION_PRESENCE_STARTUP_DELAY_SECONDS",
            5.0,
            minimum=0.0,
            maximum=300.0,
        )
        try:
            if startup_delay > 0:
                await asyncio.sleep(startup_delay)
        except asyncio.CancelledError:
            raise

        reason = "boot"
        while not self.bot.is_closed():
            self._wake_event.clear()
            try:
                now = time.monotonic()
                self._reconcile_runtime(now)

                if not self._maintenance_active:
                    await self._apply_current(reason=reason)
                reason = "wake"

                timeout = self._next_wake_timeout(time.monotonic())
                if timeout is None:
                    await self._wake_event.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        reason = "interval"
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("loop de presença falhou", exc_info=True)
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    reason = "recovery"

    def _reconcile_runtime(self, now: float) -> None:
        statuses = self._statuses()
        if not statuses:
            self._current_id = None
            self._current_started_at = None
            self._current_deadline = None
            self._paused_remaining = None
            return

        current_index = self._status_index(self._current_id)
        if current_index is None:
            self._activate_index(min(self._next_index_hint, len(statuses) - 1), now)
            current_index = self._status_index(self._current_id)

        if self._maintenance_active:
            return

        if not self.rotation_enabled or len(statuses) <= 1:
            self._current_deadline = None
            if self._current_started_at is None:
                self._current_started_at = now
            return

        current = statuses[current_index or 0]
        duration = float(current.get("duration_seconds") or MIN_STATUS_DURATION_SECONDS)
        if self._current_started_at is None:
            self._current_started_at = now
        self._current_deadline = self._current_started_at + duration

        if self._current_deadline <= now:
            next_index = ((current_index or 0) + 1) % len(statuses)
            self._activate_index(next_index, now)

    def _activate_index(self, index: int, now: float) -> None:
        statuses = self._statuses()
        if not statuses:
            self._current_id = None
            self._current_started_at = None
            self._current_deadline = None
            return
        index = max(0, min(len(statuses) - 1, int(index)))
        item = statuses[index]
        self._current_id = str(item.get("id") or "") or None
        self._current_started_at = now
        self._paused_remaining = None
        if self.rotation_enabled and len(statuses) > 1:
            duration = float(item.get("duration_seconds") or MIN_STATUS_DURATION_SECONDS)
            self._current_deadline = now + duration
        else:
            self._current_deadline = None

    def _next_wake_timeout(self, now: float) -> float | None:
        if self._maintenance_active:
            return None

        candidates: list[float] = []
        if self._retry_apply_at is not None:
            if self._retry_apply_at <= now:
                self._retry_apply_at = None
            else:
                candidates.append(self._retry_apply_at)
        if self.rotation_enabled and len(self._statuses()) > 1 and self._current_deadline is not None:
            candidates.append(self._current_deadline)
        if not candidates:
            return None
        return max(0.0, min(candidates) - now)

    async def _apply_current(self, *, reason: str) -> bool:
        if self._maintenance_active:
            return False
        current = self._current_status()
        if current is None:
            return await self._apply_presence(None, discord.Status.online, reason=reason, force=False)

        text = self._render_template(str(current.get("text") or "")).strip()
        if not text:
            return False
        return await self._apply_presence(text, discord.Status.online, reason=reason, force=False)

    async def _apply_presence(
        self,
        text: str | None,
        status: discord.Status,
        *,
        reason: str,
        force: bool,
    ) -> bool:
        if text == self._last_text and status == self._last_presence_status:
            self._retry_apply_at = None
            return False

        now = time.monotonic()
        if not force and self._last_update_at > 0:
            min_interval = self._env_float(
                "APPLICATION_PRESENCE_MIN_UPDATE_INTERVAL_SECONDS",
                30.0,
                minimum=5.0,
                maximum=3600.0,
            )
            next_allowed = self._last_update_at + min_interval
            if now < next_allowed:
                self._retry_apply_at = next_allowed
                return False

        activity = self._build_custom_activity(text) if text else None
        try:
            await self.bot.change_presence(status=status, activity=activity)
        except Exception:
            LOG.debug("falha ao alterar custom status", exc_info=True)
            self._retry_apply_at = now + 30.0
            return False

        self._last_text = text
        self._last_presence_status = status
        self._last_update_at = now
        self._retry_apply_at = None
        LOG.info("custom status atualizado (%s): %s", reason, text or "sem atividade")
        return True

    async def _maintenance_loop(self) -> None:
        interval = self._env_float(
            "APPLICATION_PRESENCE_UPDATE_POLL_SECONDS",
            2.0,
            minimum=1.0,
            maximum=30.0,
        )
        while not self.bot.is_closed():
            try:
                active = self._read_update_state()
                if active and not self._maintenance_active:
                    await self._set_maintenance_presence()
                elif not active and self._maintenance_active:
                    await self._restore_regular_presence()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("falha ao acompanhar atualização na presença", exc_info=True)
            await asyncio.sleep(interval)

    def _read_update_state(self) -> bool:
        path = self._update_state_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or not bool(payload.get("active")):
            return False

        stale_after = self._env_float(
            "APPLICATION_PRESENCE_UPDATE_STALE_SECONDS",
            180.0,
            minimum=30.0,
            maximum=1800.0,
        )
        heartbeat = payload.get("heartbeat_epoch")
        try:
            heartbeat_value = float(heartbeat)
        except (TypeError, ValueError):
            heartbeat_text = str(payload.get("heartbeat_at") or "").strip()
            try:
                heartbeat_value = datetime.fromisoformat(heartbeat_text.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return False

        age = time.time() - heartbeat_value
        return -30.0 <= age <= stale_after

    async def _set_maintenance_presence(self) -> None:
        now = time.monotonic()
        current = self._current_status()
        if current is not None and self._current_started_at is not None:
            self._maintenance_elapsed = max(0.0, now - self._current_started_at)
        else:
            self._maintenance_elapsed = None

        self._maintenance_active = True
        self._current_deadline = None
        self._retry_apply_at = None
        self._wake_event.set()
        await self._apply_presence("Atualizando", discord.Status.idle, reason="update", force=True)
        LOG.info("presença de atualização ativada")

    async def _restore_regular_presence(self) -> None:
        now = time.monotonic()
        self._maintenance_active = False
        current = self._current_status()
        if current is not None:
            duration = float(current.get("duration_seconds") or MIN_STATUS_DURATION_SECONDS)
            elapsed = max(0.0, min(duration, float(self._maintenance_elapsed or 0.0)))
            self._current_started_at = now - elapsed
            if self.rotation_enabled and len(self._statuses()) > 1:
                self._current_deadline = self._current_started_at + duration
            else:
                self._current_deadline = None
        self._maintenance_elapsed = None
        self._last_text = None
        self._last_presence_status = None
        self._last_update_at = 0.0
        self._retry_apply_at = None
        self._wake_event.set()
        await self._apply_current(reason="update_done")

    def _load_config(self) -> dict[str, Any]:
        payload: Any = None
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except (OSError, ValueError, TypeError):
            LOG.warning("configuração de presença inválida; usando padrão", exc_info=True)
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("statuses"), list):
            config = self._normalize_config(payload)
        else:
            config = self._legacy_default_config()
            try:
                self._write_config_atomic(config)
            except Exception:
                LOG.debug("não foi possível criar configuração inicial de presença", exc_info=True)
        return config

    def _legacy_default_config(self) -> dict[str, Any]:
        raw = str(os.getenv("APPLICATION_PRESENCE_TEMPLATES", "") or "").strip()
        if raw:
            templates = [item.strip() for item in re.split(r"\s*\|\|\s*", raw) if item.strip()]
        else:
            templates = list(DEFAULT_STATUS_TEMPLATES)
        duration = int(
            round(
                self._env_float(
                    "APPLICATION_PRESENCE_INTERVAL_SECONDS",
                    60.0,
                    minimum=MIN_STATUS_DURATION_SECONDS,
                    maximum=MAX_STATUS_DURATION_SECONDS,
                )
            )
        )
        statuses = []
        for index, template in enumerate(templates[:MAX_STATUS_COUNT]):
            text = str(template or "").strip()[:MAX_STATUS_TEXT_LENGTH]
            if not text:
                continue
            default_id = "members" if index == 0 else "guilds" if index == 1 else self._new_status_id()
            statuses.append({"id": default_id, "text": text, "duration_seconds": duration})
        return {"version": self.CONFIG_VERSION, "enabled": True, "statuses": statuses}

    def _normalize_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        statuses: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        raw_statuses = payload.get("statuses") if isinstance(payload.get("statuses"), list) else []
        for raw in raw_statuses[:MAX_STATUS_COUNT]:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()[:MAX_STATUS_TEXT_LENGTH]
            if not text:
                continue
            try:
                duration = self._validate_duration(int(raw.get("duration_seconds") or 60))
            except Exception:
                duration = 60
            status_id = re.sub(r"[^a-zA-Z0-9_-]+", "", str(raw.get("id") or ""))[:48]
            if not status_id or status_id in seen_ids:
                status_id = self._new_status_id()
            seen_ids.add(status_id)
            statuses.append({"id": status_id, "text": text, "duration_seconds": duration})
        return {
            "version": self.CONFIG_VERSION,
            "enabled": bool(payload.get("enabled", True)),
            "statuses": statuses,
        }

    async def _save_config(self) -> None:
        payload = copy.deepcopy(self._config)
        await asyncio.to_thread(self._write_config_atomic, payload)

    def _write_config_atomic(self, payload: dict[str, Any]) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._config_path.with_name(f".{self._config_path.name}.tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._config_path)

    def _statuses(self) -> list[dict[str, Any]]:
        statuses = self._config.get("statuses")
        if not isinstance(statuses, list):
            statuses = []
            self._config["statuses"] = statuses
        return statuses

    def _status_by_id(self, status_id: str | None) -> dict[str, Any] | None:
        if not status_id:
            return None
        for item in self._statuses():
            if str(item.get("id") or "") == status_id:
                return item
        return None

    def _status_index(self, status_id: str | None) -> int | None:
        if not status_id:
            return None
        for index, item in enumerate(self._statuses()):
            if str(item.get("id") or "") == status_id:
                return index
        return None

    def _current_status(self) -> dict[str, Any] | None:
        return self._status_by_id(self._current_id)

    def _new_status_id(self) -> str:
        existing = {str(item.get("id") or "") for item in self._statuses()} if hasattr(self, "_config") else set()
        while True:
            candidate = uuid.uuid4().hex[:12]
            if candidate not in existing:
                return candidate

    def _validate_text(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            raise ValueError("O texto do status não pode ficar vazio.")
        if len(value) > MAX_STATUS_TEXT_LENGTH:
            raise ValueError(f"O texto pode ter no máximo {MAX_STATUS_TEXT_LENGTH} caracteres.")
        return value

    def _validate_duration(self, duration_seconds: int | float) -> int:
        try:
            value = int(round(float(duration_seconds)))
        except Exception as exc:
            raise ValueError("Duração inválida.") from exc
        if value < MIN_STATUS_DURATION_SECONDS:
            raise ValueError(f"A duração mínima é {MIN_STATUS_DURATION_SECONDS} segundos.")
        if value > MAX_STATUS_DURATION_SECONDS:
            raise ValueError("A duração máxima é 24 horas.")
        return value

    def _build_custom_activity(self, text: str) -> discord.BaseActivity | discord.Activity:
        custom_activity = getattr(discord, "CustomActivity", None)
        if custom_activity is not None:
            try:
                return custom_activity(name=text)
            except TypeError:
                try:
                    return custom_activity(text)
                except Exception:
                    pass
            except Exception:
                pass
        activity_type = getattr(discord.ActivityType, "custom", None)
        if activity_type is not None:
            try:
                return discord.Activity(type=activity_type, name=text, state=text)
            except TypeError:
                return discord.Activity(type=activity_type, name=text)
        return discord.Game(name=text)

    def _template_tokens(self, template: str) -> set[str]:
        return {str(match.group(1) or "").lower() for match in VARIABLE_RE.finditer(str(template or ""))}

    def _render_template(self, template: str) -> str:
        tokens = self._template_tokens(template)
        values = self._collect_values(tokens)

        def repl(match: re.Match[str]) -> str:
            token = str(match.group(1) or "").lower()
            return values.get(token, match.group(0))

        return VARIABLE_RE.sub(repl, str(template or ""))

    def _collect_values(self, tokens: set[str]) -> dict[str, str]:
        values: dict[str, str] = {}
        if tokens & GUILD_VARIABLES:
            guilds = list(getattr(self.bot, "guilds", []) or [])
            guild_count = len(guilds)
            total_members = 0
            if tokens & {"m", "members"}:
                for guild in guilds:
                    member_count = getattr(guild, "member_count", None)
                    if isinstance(member_count, int) and member_count > 0:
                        total_members += member_count
                    else:
                        total_members += len(getattr(guild, "members", []) or [])
            if tokens & {"sv", "guilds"}:
                formatted = self._format_int(guild_count)
                values["sv"] = values["guilds"] = formatted
            if tokens & {"m", "members"}:
                formatted = self._format_int(total_members)
                values["m"] = values["members"] = formatted
        if "ping" in tokens:
            values["ping"] = self._format_ping()
        if tokens & {"up", "uptime"}:
            values["up"] = values["uptime"] = self._format_uptime()
        return values

    def _format_ping(self) -> str:
        try:
            return f"{round(float(getattr(self.bot, 'latency', 0.0) or 0.0) * 1000)}ms"
        except Exception:
            return "0ms"

    def _format_uptime(self) -> str:
        try:
            started_at = getattr(self.bot, "started_at", None)
            if started_at is None:
                return "0m"
            seconds = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
        except Exception:
            return "0m"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _format_int(self, value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", ".")
        except Exception:
            return "0"

    def _env_float(self, name: str, default: float, *, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))
