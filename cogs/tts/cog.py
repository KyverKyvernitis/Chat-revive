import inspect
import contextlib
import asyncio
import logging
import time
import os
import re
import weakref
import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


import config

logger = logging.getLogger(__name__)
from .audio import GuildTTSState, QueueItem, TTSAudioMixin, TTS_BOOT_WARMUP_ENABLED
from .common import (
    _guild_scoped,
    _shorten,
    _replace_custom_emojis_for_tts,
    _normalize_spaces,
    _speech_name,
    _looks_pronounceable_for_tts,
    _extract_primary_domain,
    _expand_abbreviations_for_tts,
    USER_MENTION_PATTERN,
    ROLE_MENTION_PATTERN,
    CHANNEL_MENTION_PATTERN,
    URL_PATTERN,
    DISCORD_CHANNEL_URL_PATTERN,
    _ATTACHMENT_IMAGE_EXTENSIONS,
    _ATTACHMENT_VIDEO_EXTENSIONS,
    get_gtts_languages,
    build_gtts_language_aliases,
    validate_mode,
)
from .utils.embed import (
    make_embed,
    build_expired_panel_embed,
    build_toggle_embed,
    build_status_embed,
    build_settings_embed,
    human_voice_name,
    human_rate,
    human_pitch,
    status_voice_channel_text,
    spoken_name_status_text,
)
from .prefix import dispatch_prefix_control_command
from .utils.message_render import render_message_tts_text, append_tts_descriptions
from .utils.message_gate import analyze_message_for_tts
from .utils.message_dispatch import dispatch_message_tts
from .utils.resolution import (
    normalize_rate_value,
    normalize_pitch_value,
    normalize_language_query,
    resolve_gtts_language_input,
)
from .ui import (
    _BaseTTSView,
    _SimpleSelectView,
    ModeSelect,
    LanguageSelect,
    SpeedSelect,
    PitchSelect,
    VoiceRegionSelect,
    VoiceSelect,
    ToggleSelect,
    LanguageCodeModal,
    LanguageHelpView,
    BotPrefixModal,
    GTTSPrefixModal,
    EdgePrefixModal,
    SpokenNameModal,
    IgnoreRoleConfigView,
    TTSPublicLauncherView,
    TTSMainPanelView,
    TTSStatusView,
    TTSTogglePanelView,
)

from .utils.panel_apply import (
    _apply_server_prefix_from_modal as apply_server_prefix_from_modal,
    _apply_mode_from_panel as apply_mode_from_panel,
    _apply_voice_from_panel as apply_voice_from_panel,
    _apply_language_from_panel as apply_language_from_panel,
    _apply_speed_from_panel as apply_speed_from_panel,
    _apply_pitch_from_panel as apply_pitch_from_panel,
    _apply_spoken_name_from_modal as apply_spoken_name_from_modal,
    _apply_announce_author_from_panel as apply_announce_author_from_panel,
    _apply_auto_leave_from_panel as apply_auto_leave_from_panel,
)

USER_CONFIG_ACTION_CHOICES = [
    app_commands.Choice(name="Abrir painel pessoal do usuário", value="panel"),
    app_commands.Choice(name="Alterar apelido falado do usuário", value="spoken_name"),
    app_commands.Choice(name="Resetar configurações do usuário para as do servidor", value="reset"),
]

STATUS_ACTION_CHOICES = [
    app_commands.Choice(name="Ver o meu status", value="self"),
    app_commands.Choice(name="Mostrar o status de outro usuário no chat", value="show_other"),
    app_commands.Choice(name="Copiar as configurações de outro usuário", value="copy_other"),
]

# Alertas de voz são deliberadamente conservadores: falhas transitórias ficam
# apenas nos logs. Quando algo realmente vira incidente, a DM do dono passa a
# funcionar como uma mensagem viva: agrega novas ocorrências, mostra recuperação
# e pode ser reaberta sem criar spam.
_VOICE_FAILURE_EVENT_RETENTION_SECONDS = 15 * 60
_VOICE_INCIDENT_EDIT_MIN_INTERVAL_SECONDS = 12.0
_VOICE_INCIDENT_REOPEN_WINDOW_SECONDS = 10 * 60
_VOICE_INCIDENT_STATE_RETENTION_SECONDS = 60 * 60


class TTSVoice(TTSAudioMixin, commands.GroupCog, group_name="tts", group_description="Comandos de texto para fala"):
    server = app_commands.Group(name="server", description="Configurações padrão do servidor")
    voices = app_commands.Group(name="voices", description="Listas de vozes e idiomas")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_cache: list[str] = []
        self.edge_voice_names: set[str] = set()
        self.gtts_languages: dict[str, str] = get_gtts_languages()
        self.gtts_language_aliases: dict[str, str] = build_gtts_language_aliases(self.gtts_languages)
        self._recent_tts_message_ids: dict[int, float] = {}
        self._voice_connect_locks: dict[int, asyncio.Lock] = {}
        self._prefix_panel_cooldowns: dict[tuple[int, int, str], float] = {}
        self._active_prefix_panels: dict[tuple[int, int, str], tuple[discord.Message, discord.ui.View]] = {}
        self._public_panel_states: dict[int, dict] = {}
        self._status_views_by_target: dict[tuple[int, int], weakref.WeakSet[TTSStatusView]] = {}
        self._status_refresh_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_announced_author_by_guild: dict[int, int] = {}
        self._app_command_id_cache: dict[object, tuple[float, dict[str, int]]] = {}
        self._voice_restore_task: asyncio.Task | None = None
        self._runtime_voice_restore_tasks: dict[int, asyncio.Task] = {}
        self._runtime_voice_restore_failures: dict[int, int] = {}
        self._runtime_voice_restore_next_allowed_at: dict[int, float] = {}
        self._runtime_voice_restore_suppressed_until: dict[int, float] = {}
        self._expected_voice_channel_ids: dict[int, int] = {}
        self._manual_voice_disconnect_until: dict[int, float] = {}
        self._voice_failure_events_by_guild: dict[tuple[int, str], list[float]] = {}
        self._voice_failure_events_global: dict[str, list[tuple[float, int]]] = {}
        self._voice_incidents: dict[tuple[str, int, str], dict[str, object]] = {}
        self._voice_incident_recovery_tasks: dict[int, asyncio.Task] = {}
        self._voice_incident_report_tasks: set[asyncio.Task] = set()
        self._voice_failure_dm_target_id: int | None = None
        self._voice_failure_dm_target_verified: bool = False
        self._voice_auto_restore_enabled: bool = bool(getattr(config, "TTS_VOICE_AUTO_RESTORE_ENABLED", True))

    async def cog_load(self):
        self._prime_tts_runtime()
        await self._load_edge_voices()
        self._ensure_tts_agent_health_task()
        if TTS_BOOT_WARMUP_ENABLED:
            asyncio.create_task(self._boot_warmup())
        if self._voice_auto_restore_enabled:
            self._voice_restore_task = asyncio.create_task(self._restore_voice_sessions_after_ready())
        else:
            print("[tts_voice] restore automático de call desativado por TTS_VOICE_AUTO_RESTORE_ENABLED=false")

    def cog_unload(self):
        self._cancel_tts_agent_health_task()
        self._shutdown_tts_runtime()
        close_phone_worker_session = getattr(self, "_close_phone_worker_http_session", None)
        if callable(close_phone_worker_session):
            close_phone_worker_session()
        task = getattr(self, "_voice_restore_task", None)
        if task is not None and not task.done():
            task.cancel()
        for incident in list(getattr(self, "_voice_incidents", {}).values()):
            refresh_task = incident.get("_refresh_task") if isinstance(incident, dict) else None
            if isinstance(refresh_task, asyncio.Task) and not refresh_task.done():
                refresh_task.cancel()
        for recovery_task in list(getattr(self, "_voice_incident_recovery_tasks", {}).values()):
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
        for report_task in list(getattr(self, "_voice_incident_report_tasks", set())):
            if report_task is not None and not report_task.done():
                report_task.cancel()

    async def _get_root_command_ids_cached(self, guild: discord.Guild | None = None, *, ttl_seconds: float = 600.0) -> dict[str, int]:
        return await fetch_root_command_ids_cached(
            self.bot,
            self._app_command_id_cache,
            guild,
            ttl_seconds=ttl_seconds,
            include_global_fallback=False,
        )

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    def _voice_channel_name(self, channel) -> str:
        if channel is None:
            return "canal desconhecido"
        name = getattr(channel, "name", None) or "canal desconhecido"
        channel_id = getattr(channel, "id", None)
        if channel_id:
            return f"{name} (`{channel_id}`)"
        return str(name)

    def _voice_permissions_report(self, guild: discord.Guild, voice_channel) -> str:
        member = getattr(guild, "me", None)
        if member is None and getattr(self.bot, "user", None) is not None:
            try:
                member = guild.get_member(self.bot.user.id)
            except Exception:
                member = None
        if member is None or voice_channel is None:
            return "não consegui ler o membro/canal do bot"
        try:
            perms = voice_channel.permissions_for(member)
        except Exception as e:
            return f"não consegui ler permissões: {type(e).__name__}: {e}"

        parts = [
            f"Ver canal: {'sim' if getattr(perms, 'view_channel', False) else 'não'}",
            f"Conectar: {'sim' if getattr(perms, 'connect', False) else 'não'}",
            f"Falar: {'sim' if getattr(perms, 'speak', False) else 'não'}",
            f"Mover membros: {'sim' if getattr(perms, 'move_members', False) else 'não'}",
        ]
        return " • ".join(parts)

    def _diagnose_voice_connect_precheck(self, guild: discord.Guild, voice_channel) -> str | None:
        if guild is None:
            return "guild ausente antes da conexão"
        if voice_channel is None:
            return "canal de voz ausente antes da conexão"
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return f"canal inválido para voz: {type(voice_channel).__name__}"

        member = getattr(guild, "me", None)
        if member is None and getattr(self.bot, "user", None) is not None:
            try:
                member = guild.get_member(self.bot.user.id)
            except Exception:
                member = None
        if member is None:
            # O cache de membros não é requisito para o handshake de voz. Bloquear
            # aqui cria falso negativo: voice_channel.connect() consegue conectar
            # mesmo quando guild.me/get_member ainda não foi hidratado. Deixe o
            # Discord ser a fonte de verdade e trate uma eventual exceção real.
            print(
                f"[tts_voice] pré-checagem sem membro do bot no cache; conexão seguirá | "
                f"guild={getattr(guild, 'id', None)} channel={getattr(voice_channel, 'id', None)}"
            )
            return None

        try:
            perms = voice_channel.permissions_for(member)
        except Exception as e:
            # Falha ao calcular permissões localmente também não prova que o bot
            # não pode conectar. O handshake real fornece Forbidden/HTTPException
            # quando houver um bloqueio efetivo.
            print(
                f"[tts_voice] não consegui checar permissões localmente; conexão seguirá | "
                f"guild={getattr(guild, 'id', None)} channel={getattr(voice_channel, 'id', None)} "
                f"error={type(e).__name__}: {e}"
            )
            return None

        missing: list[str] = []
        if not getattr(perms, "view_channel", False):
            missing.append("Ver Canal")
        if not getattr(perms, "connect", False):
            missing.append("Conectar")
        if not getattr(perms, "speak", False):
            missing.append("Falar")
        if missing:
            return "faltam permissões no canal de voz: " + ", ".join(missing)

        try:
            current_vc = self._get_voice_client_for_guild(guild)
            current_channel = getattr(current_vc, "channel", None) if current_vc is not None else None
            already_in_target = bool(
                current_vc is not None
                and self._voice_client_is_connected(current_vc)
                and getattr(current_channel, "id", None) == getattr(voice_channel, "id", None)
            )
            user_limit = int(getattr(voice_channel, "user_limit", 0) or 0)
            if not already_in_target and user_limit > 0 and len(getattr(voice_channel, "members", []) or []) >= user_limit and not getattr(perms, "move_members", False):
                return f"canal cheio ({len(getattr(voice_channel, 'members', []) or [])}/{user_limit}) e o bot não tem Mover Membros para furar o limite"
        except Exception:
            pass

        return None

    def _classify_voice_connect_exception(self, exc: BaseException) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "timeout no handshake de voz com o Discord"
        if isinstance(exc, discord.Forbidden):
            return "Discord recusou por falta de permissão"
        if isinstance(exc, discord.NotFound):
            return "canal/servidor não encontrado pelo Discord"
        if isinstance(exc, discord.HTTPException):
            return "Discord retornou erro HTTP durante a conexão de voz"
        if isinstance(exc, discord.ClientException):
            msg = str(exc).lower()
            if "already connected" in msg:
                return "o discord.py acha que já existe uma conexão de voz nesse servidor"
            return "discord.py bloqueou a conexão por estado de cliente inválido"

        exc_name = type(exc).__name__
        msg = str(exc).lower()
        if exc_name == "OpusNotLoaded" or "opus" in msg:
            return "Opus/lib de áudio não carregou corretamente"
        if "closing transport" in msg:
            return "transporte de voz estava fechando durante a conexão"
        if "not connected to voice" in msg:
            return "estado interno dizia que o bot não estava conectado à voz"
        return f"erro inesperado ao conectar na call ({exc_name})"

    def _format_voice_exception(self, exc: BaseException | None) -> str:
        if exc is None:
            return "sem exceção técnica; falhou em pré-checagem ou retornou None"
        return _shorten(f"{type(exc).__name__}: {exc}", 900)

    def _voice_failure_policy(self, reason: str, exc: BaseException | None) -> dict[str, object]:
        reason_text = str(reason or "falha de voz não classificada").strip()
        lowered = reason_text.lower()
        exc_name = type(exc).__name__ if exc is not None else ""
        exc_text = str(exc or "").lower()

        # Situações esperadas que dependem do estado momentâneo do servidor não
        # são incidentes do bot e nunca devem acordar o dono por DM.
        if "canal cheio" in lowered:
            return {"category": "channel_full", "label": "Canal de voz cheio", "suppress": True}
        if "canal de voz ausente" in lowered or "guild ausente" in lowered:
            return {"category": "stale_target", "label": "Destino de voz ficou indisponível", "suppress": True}

        # guild.me pode ficar temporariamente indisponível durante sincronização
        # de cache. Uma guild isolada é ruído; várias guilds ao mesmo tempo
        # sugerem problema real de cache/intents e passam a ser acionáveis.
        if "não consegui encontrar o membro do bot" in lowered:
            return {
                "category": "bot_member_unavailable",
                "label": "Estado do bot indisponível durante conexões de voz",
                "threshold": 6,
                "window": 600.0,
                "global_threshold": 3,
                "global_window": 300.0,
                "severity": "high",
            }

        if "não consegui checar permissões" in lowered or "não consegui ler permissões" in lowered:
            return {
                "category": "permission_precheck",
                "label": "Leitura de permissões de voz está falhando repetidamente",
                "threshold": 5,
                "window": 600.0,
                "global_threshold": 3,
                "global_window": 300.0,
                "severity": "high",
            }

        if isinstance(exc, discord.Forbidden) or "faltam permissões" in lowered or "falta de permissão" in lowered:
            return {
                "category": "permissions",
                "label": "Permissões de voz impedindo o TTS",
                "threshold": 3,
                "window": 600.0,
                "global_threshold": 0,
                "severity": "high",
            }

        if exc_name == "OpusNotLoaded" or "opus" in lowered or "opus" in exc_text:
            return {
                "category": "audio_runtime",
                "label": "Runtime local de áudio indisponível",
                "threshold": 1,
                "window": 300.0,
                "global_threshold": 0,
                "severity": "critical",
                "immediate": True,
            }

        if isinstance(exc, asyncio.TimeoutError) or "timeout" in lowered:
            return {
                "category": "voice_timeout",
                "label": "Timeouts persistentes na conexão de voz",
                "threshold": 3,
                "window": 300.0,
                "global_threshold": 3,
                "global_window": 300.0,
                "severity": "high",
            }

        if isinstance(exc, discord.NotFound) or "não encontrado pelo discord" in lowered:
            return {
                "category": "discord_voice_not_found",
                "label": "Destino de voz repetidamente não encontrado",
                "threshold": 3,
                "window": 300.0,
                "global_threshold": 0,
                "severity": "high",
            }

        if isinstance(exc, discord.HTTPException) or "erro http" in lowered:
            return {
                "category": "discord_voice_http",
                "label": "Falhas persistentes do Discord Voice",
                "threshold": 3,
                "window": 300.0,
                "global_threshold": 3,
                "global_window": 300.0,
                "severity": "high",
            }

        if (
            "closing transport" in lowered
            or "closing transport" in exc_text
            or "transporte de voz estava fechando" in lowered
            or "not connected to voice" in lowered
            or "not connected to voice" in exc_text
            or "estado interno dizia que o bot não estava conectado" in lowered
            or "já existe uma conexão de voz" in lowered
            or "estado de cliente inválido" in lowered
        ):
            return {
                "category": "voice_state",
                "label": "Estado de conexão de voz não está se recuperando",
                "threshold": 4,
                "window": 300.0,
                "global_threshold": 3,
                "global_window": 300.0,
                "severity": "high",
            }

        # Exceções não reconhecidas são as mais úteis para detectar regressões de
        # código. Ainda exigimos repetição para não transformar um evento isolado
        # em incidente, exceto quando o runtime local já foi classificado acima.
        return {
            "category": "unexpected",
            "label": "Falha inesperada e recorrente no subsistema de voz",
            "threshold": 2,
            "window": 300.0,
            "global_threshold": 2,
            "global_window": 300.0,
            "severity": "critical",
        }

    def _prune_voice_incident_tracking(self, now: float) -> None:
        retention_floor = now - _VOICE_FAILURE_EVENT_RETENTION_SECONDS
        for key, values in list(self._voice_failure_events_by_guild.items()):
            kept = [stamp for stamp in values if stamp >= retention_floor]
            if kept:
                self._voice_failure_events_by_guild[key] = kept
            else:
                self._voice_failure_events_by_guild.pop(key, None)
        for category, values in list(self._voice_failure_events_global.items()):
            kept = [(stamp, gid) for stamp, gid in values if stamp >= retention_floor]
            if kept:
                self._voice_failure_events_global[category] = kept
            else:
                self._voice_failure_events_global.pop(category, None)

        closed_floor = now - _VOICE_INCIDENT_STATE_RETENTION_SECONDS
        for key, incident in list(self._voice_incidents.items()):
            status = str(incident.get("status") or "active")
            closed_at = float(incident.get("closed_mono") or 0.0)
            if status not in {"recovered", "merged"} or closed_at <= 0.0 or closed_at >= closed_floor:
                continue
            refresh_task = incident.get("_refresh_task")
            if isinstance(refresh_task, asyncio.Task) and not refresh_task.done():
                refresh_task.cancel()
            self._voice_incidents.pop(key, None)

    def _voice_incident_key(self, scope: str, guild_id: int, category: str) -> tuple[str, int, str]:
        return (scope, int(guild_id) if scope == "guild" else 0, str(category))

    def _new_voice_incident_state(
        self,
        *,
        key: tuple[str, int, str],
        scope: str,
        category: str,
        policy: dict[str, object],
        now: float,
        now_epoch: float,
        first_event_mono: float,
        event_count: int,
        guild_counts: dict[int, int],
    ) -> dict[str, object]:
        started_mono = min(now, max(0.0, float(first_event_mono or now)))
        started_epoch = max(0.0, now_epoch - max(0.0, now - started_mono))
        affected = {int(gid) for gid in guild_counts if int(gid) > 0}
        return {
            "key": key,
            "scope": scope,
            "category": category,
            "policy": dict(policy),
            "status": "active",
            "incident_id": f"VOICE-{int(now_epoch)}-{category.replace('_', '')[:4].upper()}",
            "opened_mono": started_mono,
            "opened_epoch": started_epoch,
            "cycle_started_mono": started_mono,
            "cycle_started_epoch": started_epoch,
            "last_failure_mono": now,
            "last_failure_epoch": now_epoch,
            "event_count": max(1, int(event_count)),
            "cycle_event_count": max(1, int(event_count)),
            "guild_event_counts": dict(guild_counts),
            "affected_guild_ids": affected,
            "recovered_guild_ids": set(),
            "peak_guilds": max(1, len(affected)),
            "recurrences": 0,
            "message": None,
            "last_edit_mono": 0.0,
            "closed_mono": 0.0,
            "closed_epoch": 0.0,
            "last_reason": "",
            "last_context": "",
            "last_exception": "",
            "last_permissions": "",
            "last_guild_id": 0,
            "last_guild_name": "",
            "last_channel_name": "",
            "recovery_context": "",
            "recovery_guild_name": "",
            "recovery_channel_name": "",
            "merged_into": "",
            "_refresh_task": None,
            "_io_lock": asyncio.Lock(),
        }

    def _reopen_voice_incident_state(
        self,
        incident: dict[str, object],
        *,
        now: float,
        now_epoch: float,
        first_event_mono: float,
        event_count: int,
        guild_counts: dict[int, int],
        policy: dict[str, object],
    ) -> None:
        started_mono = min(now, max(0.0, float(first_event_mono or now)))
        started_epoch = max(0.0, now_epoch - max(0.0, now - started_mono))
        affected = {int(gid) for gid in guild_counts if int(gid) > 0}
        incident["policy"] = dict(policy)
        incident["status"] = "reopened"
        incident["cycle_started_mono"] = started_mono
        incident["cycle_started_epoch"] = started_epoch
        incident["last_failure_mono"] = now
        incident["last_failure_epoch"] = now_epoch
        incident["event_count"] = max(0, int(incident.get("event_count") or 0)) + max(1, int(event_count))
        incident["cycle_event_count"] = max(1, int(event_count))
        incident["guild_event_counts"] = dict(guild_counts)
        incident["affected_guild_ids"] = affected
        incident["recovered_guild_ids"] = set()
        incident["peak_guilds"] = max(int(incident.get("peak_guilds") or 1), len(affected), 1)
        incident["recurrences"] = max(0, int(incident.get("recurrences") or 0)) + 1
        incident["closed_mono"] = 0.0
        incident["closed_epoch"] = 0.0
        incident["recovery_context"] = ""
        incident["recovery_guild_name"] = ""
        incident["recovery_channel_name"] = ""
        incident["merged_into"] = ""

    def _apply_voice_incident_event(
        self,
        incident: dict[str, object],
        *,
        guild_id: int,
        now: float,
        now_epoch: float,
    ) -> None:
        counts = incident.get("guild_event_counts")
        if not isinstance(counts, dict):
            counts = {}
            incident["guild_event_counts"] = counts
        gid = int(guild_id)
        counts[gid] = max(0, int(counts.get(gid, 0) or 0)) + 1

        affected = incident.get("affected_guild_ids")
        if not isinstance(affected, set):
            affected = set()
            incident["affected_guild_ids"] = affected
        if gid > 0:
            affected.add(gid)

        recovered = incident.get("recovered_guild_ids")
        if isinstance(recovered, set):
            recovered.discard(gid)

        incident["last_failure_mono"] = now
        incident["last_failure_epoch"] = now_epoch
        incident["event_count"] = max(0, int(incident.get("event_count") or 0)) + 1
        incident["cycle_event_count"] = max(0, int(incident.get("cycle_event_count") or 0)) + 1
        incident["peak_guilds"] = max(int(incident.get("peak_guilds") or 1), len(affected), 1)

    def _reserve_voice_failure_alert(
        self,
        guild_id: int,
        *,
        reason: str,
        exc: BaseException | None,
    ) -> dict[str, object] | None:
        policy = self._voice_failure_policy(reason, exc)
        if bool(policy.get("suppress")):
            return None

        now = time.monotonic()
        now_epoch = time.time()
        self._prune_voice_incident_tracking(now)

        category = str(policy.get("category") or "unexpected")
        guild_id = int(guild_id)
        guild_key = (guild_id, category)
        guild_events = self._voice_failure_events_by_guild.setdefault(guild_key, [])
        guild_events.append(now)
        global_events = self._voice_failure_events_global.setdefault(category, [])
        global_events.append((now, guild_id))

        window = max(1.0, float(policy.get("window") or 300.0))
        recent_guild = [stamp for stamp in guild_events if stamp >= now - window]
        direct_count = len(recent_guild)
        threshold = max(0, int(policy.get("threshold") or 0))

        global_window = max(1.0, float(policy.get("global_window") or window))
        recent_global = [(stamp, gid) for stamp, gid in global_events if stamp >= now - global_window]
        distinct_guilds = {gid for _, gid in recent_global if gid > 0}
        global_threshold = max(0, int(policy.get("global_threshold") or 0))
        global_event_threshold = (
            max(global_threshold, int(policy.get("global_event_threshold") or (global_threshold * 2)))
            if global_threshold > 0
            else 0
        )

        trigger_scope = ""
        if bool(policy.get("immediate")):
            trigger_scope = "guild"
        elif (
            global_threshold > 0
            and len(distinct_guilds) >= global_threshold
            and len(recent_global) >= global_event_threshold
        ):
            # Quando a mesma falha já aparece em várias guilds, o incidente global
            # é mais útil do que uma sequência de alertas locais.
            trigger_scope = "global"
        elif threshold > 0 and direct_count >= threshold:
            trigger_scope = "guild"

        global_incident_key = self._voice_incident_key("global", 0, category)
        local_incident_key = self._voice_incident_key("guild", guild_id, category)
        global_incident = self._voice_incidents.get(global_incident_key)
        local_incident = self._voice_incidents.get(local_incident_key)

        if isinstance(global_incident, dict) and str(global_incident.get("status")) in {"active", "reopened"}:
            self._apply_voice_incident_event(global_incident, guild_id=guild_id, now=now, now_epoch=now_epoch)
            return {"action": "update", "state": global_incident}

        if trigger_scope == "global":
            guild_counts: dict[int, int] = {}
            for _, gid in recent_global:
                if gid > 0:
                    guild_counts[gid] = guild_counts.get(gid, 0) + 1
            first_event = min((stamp for stamp, _ in recent_global), default=now)

            # Se o mesmo incidente global acabou de se recuperar, reabra a DM
            # global anterior antes de considerar promover uma mensagem local.
            # Isso preserva um único histórico visual para a mesma reincidência.
            if isinstance(global_incident, dict):
                closed_mono = float(global_incident.get("closed_mono") or 0.0)
                if (
                    str(global_incident.get("status") or "") == "recovered"
                    and closed_mono > 0.0
                    and now - closed_mono <= _VOICE_INCIDENT_REOPEN_WINDOW_SECONDS
                ):
                    self._reopen_voice_incident_state(
                        global_incident,
                        now=now,
                        now_epoch=now_epoch,
                        first_event_mono=first_event,
                        event_count=len(recent_global),
                        guild_counts=guild_counts,
                        policy=policy,
                    )
                    for other in list(self._voice_incidents.values()):
                        if other is global_incident or not isinstance(other, dict):
                            continue
                        if (
                            str(other.get("category") or "") == category
                            and str(other.get("scope") or "") == "guild"
                            and str(other.get("status") or "") in {"active", "reopened"}
                        ):
                            other["status"] = "merged"
                            other["merged_into"] = str(global_incident.get("incident_id") or "incidente global")
                            other["closed_mono"] = now
                            other["closed_epoch"] = now_epoch
                            self._schedule_owner_voice_incident_refresh(other, force=True)
                    return {"action": "reopen", "state": global_incident}

            # Se uma guild já abriu este mesmo problema, promova a própria DM para
            # escopo global em vez de mandar outra mensagem ao dono.
            promotable = local_incident if isinstance(local_incident, dict) and str(local_incident.get("status")) in {"active", "reopened"} else None
            if promotable is None:
                for candidate in self._voice_incidents.values():
                    if (
                        isinstance(candidate, dict)
                        and str(candidate.get("category") or "") == category
                        and str(candidate.get("scope") or "") == "guild"
                        and str(candidate.get("status") or "") in {"active", "reopened"}
                    ):
                        promotable = candidate
                        break

            if promotable is not None:
                old_key = promotable.get("key")
                if isinstance(old_key, tuple):
                    self._voice_incidents.pop(old_key, None)
                promotable["key"] = global_incident_key
                promotable["scope"] = "global"
                promotable["policy"] = dict(policy)
                promotable["last_failure_mono"] = now
                promotable["last_failure_epoch"] = now_epoch
                promotable["cycle_event_count"] = max(int(promotable.get("cycle_event_count") or 0), len(recent_global))
                promotable["event_count"] = max(int(promotable.get("event_count") or 0), len(recent_global))
                counts = promotable.get("guild_event_counts")
                if not isinstance(counts, dict):
                    counts = {}
                for gid, count in guild_counts.items():
                    counts[gid] = max(int(counts.get(gid, 0) or 0), int(count))
                promotable["guild_event_counts"] = counts
                affected = promotable.get("affected_guild_ids")
                if not isinstance(affected, set):
                    affected = set()
                affected.update(guild_counts)
                promotable["affected_guild_ids"] = affected
                promotable["peak_guilds"] = max(int(promotable.get("peak_guilds") or 1), len(affected), 1)
                self._voice_incidents[global_incident_key] = promotable

                # Outras DMs locais do mesmo fingerprint viram referência histórica
                # e deixam claro que foram consolidadas, sem gerar nova mensagem.
                for other_key, other in list(self._voice_incidents.items()):
                    if other is promotable or not isinstance(other, dict):
                        continue
                    if (
                        str(other.get("category") or "") == category
                        and str(other.get("scope") or "") == "guild"
                        and str(other.get("status") or "") in {"active", "reopened"}
                    ):
                        other["status"] = "merged"
                        other["merged_into"] = str(promotable.get("incident_id") or "incidente global")
                        other["closed_mono"] = now
                        other["closed_epoch"] = now_epoch
                        self._schedule_owner_voice_incident_refresh(other, force=True)
                return {"action": "promote", "state": promotable}

            state = self._new_voice_incident_state(
                key=global_incident_key,
                scope="global",
                category=category,
                policy=policy,
                now=now,
                now_epoch=now_epoch,
                first_event_mono=first_event,
                event_count=len(recent_global),
                guild_counts=guild_counts,
            )
            self._voice_incidents[global_incident_key] = state
            return {"action": "open", "state": state}

        if isinstance(local_incident, dict) and str(local_incident.get("status")) in {"active", "reopened"}:
            self._apply_voice_incident_event(local_incident, guild_id=guild_id, now=now, now_epoch=now_epoch)
            return {"action": "update", "state": local_incident}

        if trigger_scope != "guild":
            return None

        first_event = min(recent_guild, default=now)
        guild_counts = {guild_id: max(1, direct_count)}
        if isinstance(local_incident, dict):
            closed_mono = float(local_incident.get("closed_mono") or 0.0)
            if (
                str(local_incident.get("status") or "") == "recovered"
                and closed_mono > 0.0
                and now - closed_mono <= _VOICE_INCIDENT_REOPEN_WINDOW_SECONDS
            ):
                self._reopen_voice_incident_state(
                    local_incident,
                    now=now,
                    now_epoch=now_epoch,
                    first_event_mono=first_event,
                    event_count=direct_count,
                    guild_counts=guild_counts,
                    policy=policy,
                )
                return {"action": "reopen", "state": local_incident}

        state = self._new_voice_incident_state(
            key=local_incident_key,
            scope="guild",
            category=category,
            policy=policy,
            now=now,
            now_epoch=now_epoch,
            first_event_mono=first_event,
            event_count=direct_count,
            guild_counts=guild_counts,
        )
        self._voice_incidents[local_incident_key] = state
        return {"action": "open", "state": state}

    def _update_voice_incident_context(
        self,
        incident: dict[str, object],
        guild: discord.Guild,
        voice_channel,
        *,
        reason: str,
        exc: BaseException | None,
        context: str,
    ) -> None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        incident["last_guild_id"] = guild_id
        incident["last_guild_name"] = str(getattr(guild, "name", None) or "servidor desconhecido")
        incident["last_channel_name"] = self._voice_channel_name(voice_channel)
        incident["last_reason"] = _shorten(str(reason or "falha de voz não classificada"), 1000)
        incident["last_context"] = _shorten(str(context or "entrada na call"), 500)
        incident["last_exception"] = _shorten(self._format_voice_exception(exc), 900)
        category = str(incident.get("category") or "")
        incident["last_permissions"] = (
            _shorten(self._voice_permissions_report(guild, voice_channel), 900)
            if category == "permissions"
            else ""
        )

    async def _resolve_voice_failure_dm_target(self) -> discord.User | None:
        # Depois que o owner foi validado neste boot, reaproveitamos o ID para não
        # chamar application_info em toda atualização da mesma DM.
        if self._voice_failure_dm_target_verified and self._voice_failure_dm_target_id:
            try:
                user = self.bot.get_user(self._voice_failure_dm_target_id) or await self.bot.fetch_user(self._voice_failure_dm_target_id)
                if user is not None:
                    return user
            except Exception:
                self._voice_failure_dm_target_verified = False

        configured_owner_id = 0
        bot_owner_id = 0
        try:
            configured_owner_id = int(getattr(config, "TTS_VOICE_FAILURE_DM_USER_ID", 0) or 0)
        except Exception:
            configured_owner_id = 0
        try:
            bot_owner_id = int(getattr(self.bot, "owner_id", 0) or 0)
        except Exception:
            bot_owner_id = 0

        app_owner = None
        official_owner_id = 0
        try:
            app = await self.bot.application_info()
            app_owner = getattr(app, "owner", None)
            official_owner_id = int(getattr(app_owner, "id", 0) or 0)
        except Exception as e:
            print(f"[tts_voice] não consegui obter application_info para DM de incidente: {e}")

        candidate_ids: list[int] = []

        def add_candidate(raw: int) -> None:
            try:
                parsed = int(raw or 0)
            except Exception:
                parsed = 0
            if parsed > 0 and parsed not in candidate_ids:
                candidate_ids.append(parsed)

        if official_owner_id > 0:
            # Regra forte: app.owner é a fonte de verdade. Team members e IDs administrativos
            # adicionais nunca entram no fan-out deste canal privado.
            add_candidate(official_owner_id)
        else:
            # Fallback explícito para incidentes em que a própria consulta à API do
            # Discord falhou. TTS_VOICE_FAILURE_DM_USER_ID deve ser o ID do dono.
            add_candidate(configured_owner_id)
            add_candidate(bot_owner_id)

        for user_id in candidate_ids:
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if user is not None:
                    self._voice_failure_dm_target_id = user_id
                    self._voice_failure_dm_target_verified = True
                    return user
            except Exception:
                continue

        if official_owner_id > 0 and app_owner is not None:
            self._voice_failure_dm_target_id = official_owner_id
            self._voice_failure_dm_target_verified = True
            return app_owner
        return None

    def _voice_incident_human_duration(self, seconds: float) -> str:
        total = max(0, int(round(seconds)))
        if total < 60:
            return f"{total} s"
        minutes, secs = divmod(total, 60)
        if minutes < 60:
            return f"{minutes} min" if secs == 0 else f"{minutes} min {secs} s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"

    def _voice_incident_effective_severity(self, incident: dict[str, object]) -> str:
        policy = dict(incident.get("policy") or {})
        if str(policy.get("severity") or "high") == "critical":
            return "critical"
        affected = incident.get("affected_guild_ids")
        affected_count = len(affected) if isinstance(affected, set) else 1
        duration = max(0.0, time.monotonic() - float(incident.get("cycle_started_mono") or time.monotonic()))
        cycle_events = max(0, int(incident.get("cycle_event_count") or 0))
        if affected_count >= 5 or (duration >= 15 * 60 and cycle_events >= 10):
            return "critical"
        return "high"

    def _voice_incident_recommendation(self, incident: dict[str, object]) -> str:
        category = str(incident.get("category") or "unexpected")
        recommendations = {
            "bot_member_unavailable": "Sem ação imediata. O bot continua observando cache e estado de voz; só mantenho este incidente aberto enquanto houver recorrência real.",
            "permission_precheck": "Sem ação imediata. O bot continua tentando ler o estado do canal; só escalo quando a leitura falha de forma persistente ou em vários servidores.",
            "permissions": "Revise Ver canal, Conectar e Falar no servidor afetado. O alerta fecha sozinho quando uma conexão saudável for confirmada.",
            "audio_runtime": "Verifique Opus/lib de áudio no runtime. Esta categoria é crítica porque pode impedir o TTS local em qualquer servidor.",
            "voice_timeout": "O bot continua tentando se recuperar. Se o alcance crescer entre servidores, trate como possível instabilidade do Discord Voice ou da rede da VPS.",
            "discord_voice_not_found": "Confirme se o canal ainda existe e se o estado lembrado da call continua válido. Alvos transitórios ausentes não geram este alerta.",
            "discord_voice_http": "O bot continua tentando se recuperar. Crescimento entre servidores indica provável problema externo ou de conectividade.",
            "voice_state": "O controlador de voz continua limpando estados órfãos e tentando reconectar sem ativar o loop paralelo do discord.py.",
            "unexpected": "Consulte `tts_voice` e o traceback correspondente. Repetição de erro inesperado pode indicar regressão de código.",
        }
        return recommendations.get(category, recommendations["unexpected"])

    def _build_owner_voice_incident_view(self, incident: dict[str, object]) -> discord.ui.LayoutView:
        status = str(incident.get("status") or "active")
        policy = dict(incident.get("policy") or {})
        label = str(policy.get("label") or "Falha persistente de voz")
        scope = str(incident.get("scope") or "guild")
        severity = self._voice_incident_effective_severity(incident)
        event_count = max(1, int(incident.get("event_count") or 1))
        cycle_events = max(1, int(incident.get("cycle_event_count") or 1))
        recurrences = max(0, int(incident.get("recurrences") or 0))
        incident_id = str(incident.get("incident_id") or "VOICE")
        opened_epoch = max(1, int(float(incident.get("cycle_started_epoch") or time.time())))
        last_epoch = max(1, int(float(incident.get("last_failure_epoch") or time.time())))

        affected = incident.get("affected_guild_ids")
        affected_ids = sorted(affected) if isinstance(affected, set) else []
        recovered = incident.get("recovered_guild_ids")
        recovered_ids = recovered if isinstance(recovered, set) else set()
        counts = incident.get("guild_event_counts")
        guild_counts = counts if isinstance(counts, dict) else {}

        if status == "merged":
            heading = "# ↗️ Incidente consolidado"
            accent = discord.Color.dark_grey()
            merged_into = str(incident.get("merged_into") or "incidente global")
            summary = (
                f"`{incident_id}` foi absorvido por **{merged_into}** porque a mesma causa passou a aparecer em vários servidores.\n"
                "Nenhuma nova DM foi criada para a consolidação."
            )
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(heading),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(summary),
                    accent_color=accent,
                )
            )
            return view

        if status == "recovered":
            heading = "# ✅ Voz recuperada"
            accent = discord.Color.green()
            badge = "`RECUPERADO`"
        elif status == "reopened":
            heading = "# 🔁 Incidente de voz reaberto"
            accent = discord.Color.red() if severity == "critical" else discord.Color.orange()
            badge = "`CRÍTICO` `REABERTO`" if severity == "critical" else "`ALTO` `REABERTO`"
        elif severity == "critical":
            heading = "# 🚨 Incidente crítico de voz"
            accent = discord.Color.red()
            badge = "`CRÍTICO` `ATIVO`"
        else:
            heading = "# ⚠️ Voz degradada"
            accent = discord.Color.orange()
            badge = "`ALTO` `ATIVO`"

        header = f"{heading}\n{badge}\n**{label}**\n-# {incident_id}"

        peak = max(1, int(incident.get("peak_guilds") or len(affected_ids) or 1))
        if status == "recovered":
            closed_mono = float(incident.get("closed_mono") or time.monotonic())
            cycle_started = float(incident.get("cycle_started_mono") or closed_mono)
            duration = self._voice_incident_human_duration(max(0.0, closed_mono - cycle_started))
            impact = (
                f"**Duração**  {duration}\n"
                f"**Ocorrências agrupadas**  `{cycle_events}` neste ciclo · `{event_count}` no histórico da mensagem\n"
                f"**Pico de alcance**  `{peak}` servidor{'es' if peak != 1 else ''}\n"
                f"**Recorrências**  `{recurrences}`"
            )
        else:
            impact = (
                f"**Ocorrências**  `{cycle_events}` neste ciclo · `{event_count}` no histórico da mensagem\n"
                f"**Alcance atual**  `{max(1, len(affected_ids))}` servidor{'es' if len(affected_ids) != 1 else ''}\n"
                f"**Aberto**  <t:{opened_epoch}:R>\n"
                f"**Última falha**  <t:{last_epoch}:R>"
            )
            if recurrences:
                impact += f"\n**Recorrências**  `{recurrences}`"

        scope_lines: list[str] = []
        if scope == "global":
            ranked_ids = sorted(affected_ids, key=lambda gid: (-int(guild_counts.get(gid, 0) or 0), gid))
            for gid in ranked_ids[:5]:
                guild_obj = self.bot.get_guild(int(gid))
                guild_name = str(getattr(guild_obj, "name", None) or f"Servidor {gid}")
                scope_lines.append(f"• **{_shorten(guild_name, 80)}** · `{int(guild_counts.get(gid, 0) or 0)}` falhas · `{gid}`")
            if len(ranked_ids) > 5:
                scope_lines.append(f"-# +{len(ranked_ids) - 5} servidores neste incidente")
            if recovered_ids and status != "recovered":
                scope_lines.append(f"-# Recuperação confirmada em {len(recovered_ids)}/{max(1, len(affected_ids))} servidores afetados")
        else:
            guild_name = str(incident.get("last_guild_name") or "servidor desconhecido")
            guild_id = int(incident.get("last_guild_id") or (affected_ids[0] if affected_ids else 0))
            channel_name = str(incident.get("last_channel_name") or "canal desconhecido")
            scope_lines = [
                f"**Servidor**  {_shorten(guild_name, 100)} · `{guild_id}`",
                f"**Canal**  {_shorten(channel_name, 180)}",
            ]
        scope_text = "\n".join(scope_lines) or "Escopo indisponível"

        reason = _shorten(str(incident.get("last_reason") or label), 950)
        context = _shorten(str(incident.get("last_context") or "entrada na call"), 450)
        diagnosis = f"**Sinal detectado**\n{reason}\n\n**Origem**\n{context}"

        technical = str(incident.get("last_exception") or "").strip()
        if technical and technical != "sem exceção técnica; falhou em pré-checagem ou retornou None":
            safe_technical = _shorten(technical.replace("```", "~~~"), 800)
            diagnosis += f"\n\n**Erro técnico**\n```py\n{safe_technical}\n```"

        permissions = str(incident.get("last_permissions") or "").strip()
        if permissions:
            diagnosis += f"\n**Permissões relevantes**\n{permissions}"

        if status == "recovered":
            recovery_guild = str(incident.get("recovery_guild_name") or incident.get("last_guild_name") or "servidor")
            recovery_channel = str(incident.get("recovery_channel_name") or incident.get("last_channel_name") or "canal")
            recovery_context = str(incident.get("recovery_context") or "conexão saudável confirmada")
            action = (
                f"**Normalização confirmada**\n{_shorten(recovery_guild, 100)} · {_shorten(recovery_channel, 180)}\n"
                f"-# {_shorten(recovery_context, 350)}\n\n"
                "Se a mesma causa voltar dentro de 10 minutos e atingir o limiar novamente, esta própria mensagem será reaberta."
            )
        else:
            action = (
                "**Recuperação automática**\nContinua ativa enquanto houver um alvo de voz válido\n\n"
                f"**Leitura recomendada**\n{self._voice_incident_recommendation(incident)}"
            )

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(header),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## Impacto\n{impact}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## Escopo\n{scope_text}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## Diagnóstico\n{diagnosis}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## Estado\n{action}"),
                accent_color=accent,
            )
        )
        return view

    async def _sync_owner_voice_incident_message(self, incident: dict[str, object], *, force: bool = False) -> None:
        io_lock = incident.get("_io_lock")
        if not isinstance(io_lock, asyncio.Lock):
            io_lock = asyncio.Lock()
            incident["_io_lock"] = io_lock

        async with io_lock:
            now = time.monotonic()
            last_edit = float(incident.get("last_edit_mono") or 0.0)
            if not force and last_edit > 0.0 and now - last_edit < _VOICE_INCIDENT_EDIT_MIN_INTERVAL_SECONDS:
                self._schedule_owner_voice_incident_refresh(incident)
                return

            target = await self._resolve_voice_failure_dm_target()
            if target is None:
                print(f"[tts_voice] incidente sem DM: dono do bot não encontrado | id={incident.get('incident_id')}")
                return

            view = self._build_owner_voice_incident_view(incident)
            message = incident.get("message")
            if message is not None:
                try:
                    await message.edit(view=view)
                    incident["last_edit_mono"] = time.monotonic()
                    return
                except discord.NotFound:
                    incident["message"] = None
                except Exception as e:
                    print(f"[tts_voice] falha ao atualizar DM de incidente | id={incident.get('incident_id')} error={e}")
                    return

            try:
                sent = await target.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                incident["message"] = sent
                incident["last_edit_mono"] = time.monotonic()
                print(
                    f"[tts_voice] DM de incidente criada para o dono | id={incident.get('incident_id')} "
                    f"scope={incident.get('scope')} category={incident.get('category')}"
                )
            except Exception as e:
                print(
                    f"[tts_voice] não consegui enviar DM de incidente de voz | id={incident.get('incident_id')} "
                    f"owner={getattr(target, 'id', None)} error={e}"
                )

    def _schedule_owner_voice_incident_refresh(self, incident: dict[str, object], *, force: bool = False) -> None:
        current_task = incident.get("_refresh_task")
        if isinstance(current_task, asyncio.Task) and not current_task.done():
            if force:
                current_task.cancel()
            else:
                return

        last_edit = float(incident.get("last_edit_mono") or 0.0)
        delay = 0.0 if force or last_edit <= 0.0 else max(
            0.0,
            _VOICE_INCIDENT_EDIT_MIN_INTERVAL_SECONDS - (time.monotonic() - last_edit),
        )

        async def _runner() -> None:
            try:
                if delay > 0.0:
                    await asyncio.sleep(delay)
                await self._sync_owner_voice_incident_message(incident, force=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[tts_voice] falha inesperada ao sincronizar incidente | id={incident.get('incident_id')} error={e}")
            finally:
                if incident.get("_refresh_task") is asyncio.current_task():
                    incident["_refresh_task"] = None

        incident["_refresh_task"] = asyncio.create_task(_runner())

    def _schedule_voice_failure_report(
        self,
        guild: discord.Guild,
        voice_channel,
        *,
        reason: str,
        exc: BaseException | None = None,
        context: str = "entrada na call",
    ) -> None:
        """Agenda telemetria/DM sem jamais bloquear o caminho de conexão de voz."""
        guild_id = int(getattr(guild, "id", 0) or 0)

        async def _runner() -> None:
            try:
                await self._maybe_notify_owner_voice_incident(
                    guild,
                    voice_channel,
                    reason=reason,
                    exc=exc,
                    context=context,
                )
            except asyncio.CancelledError:
                raise
            except Exception as report_error:
                print(
                    f"[tts_voice] falha isolada no pipeline de incidente; conexão não foi afetada | "
                    f"guild={guild_id} error={type(report_error).__name__}: {report_error}"
                )

        task = asyncio.create_task(_runner(), name=f"tts-voice-incident-report:{guild_id}")
        self._voice_incident_report_tasks.add(task)
        task.add_done_callback(self._voice_incident_report_tasks.discard)

    async def _maybe_notify_owner_voice_incident(
        self,
        guild: discord.Guild,
        voice_channel,
        *,
        reason: str,
        exc: BaseException | None = None,
        context: str = "entrada na call",
    ) -> None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id <= 0:
            return

        decision = self._reserve_voice_failure_alert(guild_id, reason=reason, exc=exc)
        if decision is None:
            return
        incident = decision.get("state")
        if not isinstance(incident, dict):
            return

        self._update_voice_incident_context(
            incident,
            guild,
            voice_channel,
            reason=reason,
            exc=exc,
            context=context,
        )
        action = str(decision.get("action") or "update")
        if action in {"open", "reopen", "promote"}:
            # O primeiro alerta e mudanças importantes aparecem imediatamente.
            await self._sync_owner_voice_incident_message(incident, force=True)
        else:
            # Falhas adicionais só atualizam a mesma DM, com debounce para não
            # consumir rate-limit nem criar carga perceptível no bot.
            self._schedule_owner_voice_incident_refresh(incident)

    def _schedule_voice_incident_recovery(self, guild: discord.Guild, voice_channel, *, context: str) -> None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id <= 0:
            return

        relevant = False
        for incident in self._voice_incidents.values():
            if not isinstance(incident, dict) or str(incident.get("status") or "") not in {"active", "reopened"}:
                continue
            if str(incident.get("scope") or "") == "guild":
                key = incident.get("key")
                if isinstance(key, tuple) and len(key) >= 2 and int(key[1] or 0) == guild_id:
                    relevant = True
                    break
            else:
                affected = incident.get("affected_guild_ids")
                if isinstance(affected, set) and guild_id in affected:
                    relevant = True
                    break
        if not relevant:
            return

        existing = self._voice_incident_recovery_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return

        async def _runner() -> None:
            try:
                # Um pequeno atraso evita marcar recuperação no meio do handshake
                # disparado por voice_state_update. Não afeta o caminho do TTS.
                await asyncio.sleep(0.8)
                await self._mark_voice_incidents_recovered(guild, voice_channel, context=context)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[tts_voice] falha ao confirmar recuperação de incidente | guild={guild_id} error={e}")
            finally:
                if self._voice_incident_recovery_tasks.get(guild_id) is asyncio.current_task():
                    self._voice_incident_recovery_tasks.pop(guild_id, None)

        self._voice_incident_recovery_tasks[guild_id] = asyncio.create_task(_runner())

    async def _mark_voice_incidents_recovered(self, guild: discord.Guild, voice_channel, *, context: str) -> None:
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id <= 0:
            return

        vc = self._get_voice_client_for_guild(guild)
        healthy_channel = self._voice_client_channel(vc) if vc is not None else None
        if vc is None or not self._voice_client_is_connected(vc) or healthy_channel is None:
            return
        expected_channel_id = int(getattr(voice_channel, "id", 0) or 0)
        healthy_channel_id = int(getattr(healthy_channel, "id", 0) or 0)
        if expected_channel_id > 0 and healthy_channel_id != expected_channel_id:
            return

        now = time.monotonic()
        now_epoch = time.time()
        changed: list[dict[str, object]] = []
        recovered_states: list[dict[str, object]] = []

        for incident in list(self._voice_incidents.values()):
            if not isinstance(incident, dict) or str(incident.get("status") or "") not in {"active", "reopened"}:
                continue
            category = str(incident.get("category") or "")
            scope = str(incident.get("scope") or "guild")
            affected = incident.get("affected_guild_ids")
            if not isinstance(affected, set):
                affected = set()
                incident["affected_guild_ids"] = affected

            if scope == "guild":
                key = incident.get("key")
                if not isinstance(key, tuple) or len(key) < 2 or int(key[1] or 0) != guild_id:
                    continue
                should_close = True
            else:
                if guild_id not in affected:
                    continue
                recovered = incident.get("recovered_guild_ids")
                if not isinstance(recovered, set):
                    recovered = set()
                    incident["recovered_guild_ids"] = recovered
                recovered.add(guild_id)
                should_close = bool(affected) and recovered.issuperset(affected)

            # Um sucesso real zera o ruído antigo daquela guild para que uma única
            # falha após recuperação não reabra o incidente por contagem herdada.
            self._voice_failure_events_by_guild.pop((guild_id, category), None)
            global_values = self._voice_failure_events_global.get(category, [])
            kept = [(stamp, gid) for stamp, gid in global_values if gid != guild_id]
            if kept:
                self._voice_failure_events_global[category] = kept
            else:
                self._voice_failure_events_global.pop(category, None)

            if should_close:
                incident["status"] = "recovered"
                incident["closed_mono"] = now
                incident["closed_epoch"] = now_epoch
                incident["recovery_context"] = _shorten(str(context or "conexão saudável confirmada"), 350)
                incident["recovery_guild_name"] = str(getattr(guild, "name", None) or "servidor")
                incident["recovery_channel_name"] = self._voice_channel_name(healthy_channel)
                recovered_states.append(incident)
            changed.append(incident)

        # Um incidente global encerrado não precisa carregar contagens antigas de
        # outras guilds; a próxima reincidência começa com uma janela limpa.
        for incident in recovered_states:
            if str(incident.get("scope") or "") == "global":
                category = str(incident.get("category") or "")
                self._voice_failure_events_global.pop(category, None)
                affected = incident.get("affected_guild_ids")
                if isinstance(affected, set):
                    for gid in affected:
                        self._voice_failure_events_by_guild.pop((int(gid), category), None)

        for incident in changed:
            self._schedule_owner_voice_incident_refresh(incident, force=str(incident.get("status")) == "recovered")

    async def _set_remembered_voice_channel(self, guild_id: int, channel_id: int | None) -> None:
        db = self._get_db()
        if db is None or not hasattr(db, "set_tts_voice_channel_id"):
            return
        try:
            await self._maybe_await(db.set_tts_voice_channel_id(guild_id, channel_id))
        except Exception as e:
            print(f"[tts_voice] erro ao salvar canal de voz lembrado da guild {guild_id}: {e}")

    async def _clear_remembered_voice_channel(self, guild_id: int) -> None:
        await self._set_remembered_voice_channel(guild_id, None)

    async def _get_remembered_voice_channel_id(self, guild_id: int) -> int:
        db = self._get_db()
        if db is None or not hasattr(db, "get_tts_voice_channel_id"):
            return 0
        try:
            value = db.get_tts_voice_channel_id(guild_id)
            value = await self._maybe_await(value)
            return max(0, int(value or 0))
        except Exception as e:
            print(f"[tts_voice] erro ao ler canal de voz lembrado da guild {guild_id}: {e}")
            return 0

    def _get_bot_voice_state_channel(self, guild: discord.Guild | None):
        if guild is None:
            return None
        me = getattr(guild, "me", None)
        me_voice = getattr(me, "voice", None)
        return getattr(me_voice, "channel", None)

    def _is_lavalink_voice_client(self, vc) -> bool:
        if vc is None:
            return False
        module = str(getattr(type(vc), "__module__", "") or "")
        qualname = str(getattr(type(vc), "__qualname__", "") or getattr(type(vc), "__name__", "") or "")
        return module.startswith("wavelink") or (qualname == "Player" and hasattr(vc, "node") and hasattr(vc, "play"))

    def _voice_client_is_connected(self, vc) -> bool:
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
            return bool(getattr(vc, "channel", None) is not None or getattr(vc, "guild", None) is not None)
        checker = getattr(vc, "is_connected", None)
        try:
            return bool(checker() if callable(checker) else getattr(vc, "connected", False))
        except Exception:
            return False

    def _voice_client_channel(self, vc):
        return getattr(vc, "channel", None) if vc is not None else None

    def _voice_client_is_playing(self, vc) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            return bool(getattr(vc, "playing", False))
        checker = getattr(vc, "is_playing", None)
        try:
            return bool(checker() if callable(checker) else getattr(vc, "playing", False))
        except Exception:
            return False

    def _voice_client_is_paused(self, vc) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            return bool(getattr(vc, "paused", False))
        checker = getattr(vc, "is_paused", None)
        try:
            return bool(checker() if callable(checker) else getattr(vc, "paused", False))
        except Exception:
            return False

    def _voice_client_is_playing_or_paused(self, vc) -> bool:
        return self._voice_client_is_playing(vc) or self._voice_client_is_paused(vc)

    def _is_voice_client_stale(self, guild: discord.Guild, vc: discord.VoiceClient | None) -> bool:
        actual_channel = self._get_bot_voice_state_channel(guild)
        if vc is None:
            return actual_channel is not None

        try:
            connected = self._voice_client_is_connected(vc)
        except Exception:
            connected = False

        vc_channel = getattr(vc, "channel", None)
        if not connected:
            # Um VoiceClient ainda registrado, mas desconectado, sempre é
            # obsoleto. Mesmo com ambos os canais em None ele pode continuar em
            # bot.voice_clients por alguns instantes e bloquear a conexão nova
            # com "Already connected".
            return True

        if actual_channel is not None and vc_channel is not None:
            return getattr(vc_channel, "id", None) != getattr(actual_channel, "id", None)

        return False

    async def _clear_ghost_voice_state(self, guild: discord.Guild, *, reason: str) -> None:
        actual_channel = self._get_bot_voice_state_channel(guild)
        if actual_channel is None:
            return
        try:
            await guild.change_voice_state(channel=None)
            await asyncio.sleep(0.5)
            print(f"[tts_voice] estado fantasma limpo | guild={guild.id} reason={reason} channel={getattr(actual_channel, 'id', None)}")
        except Exception as e:
            print(f"[tts_voice] falha ao limpar estado fantasma | guild={guild.id} reason={reason} error={e}")

    async def _recover_stale_voice_client(self, guild: discord.Guild, *, reason: str) -> None:
        vc = self._get_voice_client_for_guild(guild)
        stale = self._is_voice_client_stale(guild, vc)
        if not stale:
            return
        if self._is_lavalink_voice_client(vc):
            print(f"[tts_voice] recuperação de voice state ignorada | player Lavalink ativo | guild={guild.id} reason={reason}")
            return
        try:
            if vc is not None:
                try:
                    if self._voice_client_is_playing_or_paused(vc):
                        vc.stop()
                except Exception:
                    pass
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        finally:
            await self._clear_ghost_voice_state(guild, reason=reason)

    async def _restore_voice_sessions_after_ready(self) -> None:
        if not self._voice_auto_restore_enabled:
            return
        await self.bot.wait_until_ready()
        await asyncio.sleep(6.0)
        db = self._get_db()
        if db is None or not hasattr(db, "iter_tts_voice_channel_ids"):
            return

        try:
            remembered = db.iter_tts_voice_channel_ids()
            remembered = await self._maybe_await(remembered)
        except Exception as e:
            print(f"[tts_voice] erro ao listar canais de voz lembrados: {e}")
            return

        pending = {int(gid): int(cid) for gid, cid in dict(remembered or {}).items() if int(cid or 0) > 0}
        if not pending:
            print("[tts_voice] restore pós-boot sem canais lembrados")
            return

        for guild_id, channel_id in pending.items():
            self._remember_expected_voice_channel(guild_id, channel_id)
        print(f"[tts_voice] restore pós-boot iniciado | guilds={len(pending)}")

        for attempt in range(4):
            if not pending or self.bot.is_closed():
                break

            remaining: dict[int, int] = {}
            for guild_id, channel_id in list(pending.items()):
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue

                channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    await self._clear_remembered_voice_channel(guild_id)
                    continue

                auto_leave_enabled = True
                try:
                    auto_leave_enabled = bool(await self._get_guild_toggle_value(
                        guild_id,
                        public_key="auto_leave",
                        raw_key="auto_leave_enabled",
                        default=True,
                    ))
                except Exception:
                    auto_leave_enabled = True

                humans_present = any(not getattr(member, "bot", False) for member in getattr(channel, "members", []))
                if auto_leave_enabled and not humans_present:
                    remaining[guild_id] = channel_id
                    continue

                current_vc = self._get_voice_client_for_guild(guild)
                if current_vc is not None and not self._is_voice_client_stale(guild, current_vc):
                    current_channel = getattr(current_vc, "channel", None)
                    if getattr(current_channel, "id", None) == channel_id:
                        self._remember_expected_voice_channel(guild_id, channel_id)
                        continue

                try:
                    await self._recover_stale_voice_client(guild, reason="startup_restore")
                    vc = await self._ensure_connected(
                        guild,
                        channel,
                        report_failure=True,
                        failure_context=f"restore automático após reinício · tentativa {attempt + 1}/4",
                    )
                    if vc is not None and self._voice_client_is_connected(vc):
                        self._remember_expected_voice_channel(guild_id, channel_id)
                        self._runtime_voice_restore_failures[guild_id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild_id] = 0.0
                        print(f"[tts_voice] call restaurada após boot | guild={guild_id} channel={channel_id}")
                        continue
                except Exception as e:
                    print(f"[tts_voice] falha ao restaurar call após boot | guild={guild_id} channel={channel_id} error={e}")
                    self._schedule_voice_failure_report(
                        guild,
                        channel,
                        reason=self._classify_voice_connect_exception(e),
                        exc=e,
                        context=f"restore automático após reinício · tentativa {attempt + 1}/4",
                    )

                remaining[guild_id] = channel_id

            pending = remaining
            if pending:
                await asyncio.sleep(8.0 + (attempt * 4.0))

    async def _get_voice_moderation_settings(self, guild_id: int) -> dict:
        return {}

    async def _voice_should_self_deaf(self, guild_id: int) -> bool:
        return True

    async def _should_use_receive_voice_client(self, guild_id: int) -> bool:
        return False

    async def _notify_voice_moderation_ready(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        return

    async def _notify_voice_moderation_playback_start(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        return

    async def _notify_voice_moderation_playback_end(self, guild: discord.Guild, vc: discord.VoiceClient | None = None) -> None:
        return


    async def _set_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None, **kwargs):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.set_user_tts(guild_id, user_id, **kwargs))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    async def _reset_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.reset_user_tts(guild_id, user_id))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    def _register_status_view(self, view: TTSStatusView) -> None:
        if view.message is None:
            return
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if views is None:
            views = weakref.WeakSet()
            self._status_views_by_target[key] = views
        views.add(view)

    def _unregister_status_view(self, view: TTSStatusView) -> None:
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if not views:
            return
        views.discard(view)
        if not list(views):
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)

    async def _notify_status_views_changed(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        views = self._status_views_by_target.get(key)
        if not views:
            return
        active_views = [view for view in list(views) if getattr(view, "message", None) is not None and not view.is_finished()]
        if not active_views:
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)
            return
        lock = self._status_refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            for view in list(active_views):
                await view.refresh_from_config_change()

    def _cancel_runtime_voice_restore(self, guild_id: int) -> None:
        task = self._runtime_voice_restore_tasks.pop(int(guild_id), None)
        if task is not None and not task.done():
            task.cancel()

    def _suppress_runtime_voice_restore(self, guild_id: int, *, seconds: float = 15.0) -> None:
        self._runtime_voice_restore_suppressed_until[int(guild_id)] = time.monotonic() + max(0.0, float(seconds))

    def _runtime_voice_restore_is_suppressed(self, guild_id: int) -> bool:
        until = float(self._runtime_voice_restore_suppressed_until.get(int(guild_id), 0.0) or 0.0)
        return until > time.monotonic()

    async def _runtime_should_restore_voice(self, guild_id: int) -> bool:
        if not self._voice_auto_restore_enabled:
            return False
        try:
            auto_leave_enabled = bool(await self._get_guild_toggle_value(
                guild_id,
                public_key="auto_leave",
                raw_key="auto_leave_enabled",
                default=True,
            ))
        except Exception:
            auto_leave_enabled = True
        return not auto_leave_enabled

    def _remember_expected_voice_channel(self, guild_id: int, channel_id: int | None) -> None:
        try:
            parsed = max(0, int(channel_id or 0))
        except Exception:
            parsed = 0
        if parsed > 0:
            self._expected_voice_channel_ids[int(guild_id)] = parsed
        else:
            self._expected_voice_channel_ids.pop(int(guild_id), None)

    def _mark_manual_voice_disconnect(self, guild_id: int, *, seconds: float = 45.0) -> None:
        self._manual_voice_disconnect_until[int(guild_id)] = time.monotonic() + max(5.0, float(seconds))

    def _clear_manual_voice_disconnect(self, guild_id: int) -> None:
        self._manual_voice_disconnect_until.pop(int(guild_id), None)

    def _is_manual_voice_disconnect_recent(self, guild_id: int) -> bool:
        until = float(self._manual_voice_disconnect_until.get(int(guild_id), 0.0) or 0.0)
        if until <= time.monotonic():
            self._manual_voice_disconnect_until.pop(int(guild_id), None)
            return False
        return True

    async def suppress_runtime_voice_restore(self, guild_id: int, *, seconds: float = 20.0, expected_channel_id: int | None = None) -> None:
        guild_id = int(guild_id)
        self._suppress_runtime_voice_restore(guild_id, seconds=seconds)
        self._cancel_runtime_voice_restore(guild_id)
        if expected_channel_id is not None:
            self._remember_expected_voice_channel(guild_id, expected_channel_id)
            with contextlib.suppress(Exception):
                await self._set_remembered_voice_channel(guild_id, expected_channel_id)


    async def _schedule_runtime_voice_restore(
        self,
        guild: discord.Guild,
        *,
        channel_id: int | None = None,
        reason: str,
        initial_delay: float = 4.0,
    ) -> None:
        guild_id = int(guild.id)
        if self.bot.is_closed() or self._runtime_voice_restore_is_suppressed(guild_id):
            return
        if not await self._runtime_should_restore_voice(guild_id):
            return

        target_channel_id = 0
        try:
            target_channel_id = max(0, int(channel_id or 0))
        except Exception:
            target_channel_id = 0
        if target_channel_id <= 0:
            target_channel_id = int(self._expected_voice_channel_ids.get(guild_id, 0) or 0)
        if target_channel_id <= 0:
            target_channel_id = await self._get_remembered_voice_channel_id(guild_id)
        if target_channel_id <= 0:
            return

        self._expected_voice_channel_ids[guild_id] = target_channel_id
        current_task = self._runtime_voice_restore_tasks.get(guild_id)
        if current_task is not None and not current_task.done():
            return

        async def _runner() -> None:
            delay = max(1.5, float(initial_delay))
            attempt = 0
            try:
                while not self.bot.is_closed():
                    if self._runtime_voice_restore_is_suppressed(guild_id):
                        return

                    now = time.monotonic()
                    next_allowed_at = float(self._runtime_voice_restore_next_allowed_at.get(guild_id, 0.0) or 0.0)
                    wait_for = max(delay, max(0.0, next_allowed_at - now))
                    if wait_for > 0.0:
                        await asyncio.sleep(wait_for)

                    if self._runtime_voice_restore_is_suppressed(guild_id):
                        return
                    if not await self._runtime_should_restore_voice(guild_id):
                        return

                    current_guild = self.bot.get_guild(guild_id) or guild
                    if current_guild is None:
                        return

                    desired_channel_id = int(self._expected_voice_channel_ids.get(guild_id, 0) or 0)
                    if desired_channel_id <= 0:
                        desired_channel_id = await self._get_remembered_voice_channel_id(guild_id)
                    if desired_channel_id <= 0:
                        return

                    current_vc = self._get_voice_client_for_guild(current_guild)
                    if current_vc is not None and not self._is_voice_client_stale(current_guild, current_vc):
                        current_channel = getattr(current_vc, "channel", None)
                        if getattr(current_channel, "id", None) == desired_channel_id:
                            self._runtime_voice_restore_failures[guild_id] = 0
                            self._runtime_voice_restore_next_allowed_at[guild_id] = 0.0
                            return

                    desired_channel = current_guild.get_channel(desired_channel_id) or self.bot.get_channel(desired_channel_id)
                    if not isinstance(desired_channel, (discord.VoiceChannel, discord.StageChannel)):
                        await self._clear_remembered_voice_channel(guild_id)
                        self._expected_voice_channel_ids.pop(guild_id, None)
                        return

                    try:
                        await self._recover_stale_voice_client(current_guild, reason=f"runtime_restore:{reason}:{attempt}")
                        vc = await self._ensure_connected(
                            current_guild,
                            desired_channel,
                            report_failure=True,
                            failure_context=f"restore em runtime ({reason}) · tentativa {attempt + 1}",
                        )
                        if vc is not None and self._voice_client_is_connected(vc) and getattr(getattr(vc, "channel", None), "id", None) == desired_channel_id:
                            self._runtime_voice_restore_failures[guild_id] = 0
                            self._runtime_voice_restore_next_allowed_at[guild_id] = time.monotonic() + 10.0
                            print(f"[tts_voice] call restaurada em runtime | guild={guild_id} channel={desired_channel_id} reason={reason}")
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[tts_voice] falha ao restaurar call em runtime | guild={guild_id} channel={desired_channel_id} reason={reason} error={e}")
                        self._schedule_voice_failure_report(
                            current_guild,
                            desired_channel,
                            reason=self._classify_voice_connect_exception(e),
                            exc=e,
                            context=f"restore em runtime ({reason}) · tentativa {attempt + 1}",
                        )

                    attempt += 1
                    self._runtime_voice_restore_failures[guild_id] = attempt
                    delay = min(45.0, 4.0 + (attempt * 5.0))
                    self._runtime_voice_restore_next_allowed_at[guild_id] = time.monotonic() + delay
            finally:
                task = self._runtime_voice_restore_tasks.get(guild_id)
                if task is not None and task.done():
                    self._runtime_voice_restore_tasks.pop(guild_id, None)

        self._runtime_voice_restore_tasks[guild_id] = asyncio.create_task(_runner())

    def _cleanup_guild_runtime_state(self, guild_id: int) -> None:
        self._last_announced_author_by_guild.pop(int(guild_id), None)
        self._cancel_runtime_voice_restore(guild_id)
        self._runtime_voice_restore_failures.pop(int(guild_id), None)
        self._runtime_voice_restore_next_allowed_at.pop(int(guild_id), None)
        self._runtime_voice_restore_suppressed_until.pop(int(guild_id), None)
        self._expected_voice_channel_ids.pop(int(guild_id), None)
        self._manual_voice_disconnect_until.pop(int(guild_id), None)
        for key in [key for key in self._voice_failure_events_by_guild if key[0] == int(guild_id)]:
            self._voice_failure_events_by_guild.pop(key, None)
        recovery_task = self._voice_incident_recovery_tasks.pop(int(guild_id), None)
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
        for category, values in list(self._voice_failure_events_global.items()):
            kept = [(stamp, gid) for stamp, gid in values if gid != int(guild_id)]
            if kept:
                self._voice_failure_events_global[category] = kept
            else:
                self._voice_failure_events_global.pop(category, None)

    def _guild_announce_author_enabled(self, guild_defaults: dict | None) -> bool:
        return bool((guild_defaults or {}).get("announce_author", False))

    def _get_ignored_tts_role_id(self, guild_id: int, *, guild_defaults: dict | None = None) -> int:
        if guild_defaults is not None:
            try:
                return max(0, int((guild_defaults or {}).get("ignored_tts_role_id", 0) or 0))
            except Exception:
                return 0

        db = self._get_db()
        if db is not None and hasattr(db, "get_ignored_tts_role_id"):
            try:
                value = db.get_ignored_tts_role_id(guild_id)
                return max(0, int(value or 0))
            except Exception:
                pass
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                defaults = db.get_guild_tts_defaults(guild_id)
                return max(0, int((defaults or {}).get("ignored_tts_role_id", 0) or 0))
            except Exception:
                pass
        return 0

    def _ignored_tts_role_enabled(self, guild_id: int, *, guild_defaults: dict | None = None) -> bool:
        if guild_defaults is not None:
            # Migração suave: se não existir flag explícita, cargo antigo salvo continua ativo.
            if "ignored_tts_role_enabled" in (guild_defaults or {}):
                return bool((guild_defaults or {}).get("ignored_tts_role_enabled", False))
            return bool(self._get_ignored_tts_role_id(guild_id, guild_defaults=guild_defaults))

        db = self._get_db()
        if db is not None and hasattr(db, "get_ignored_tts_role_enabled"):
            try:
                return bool(db.get_ignored_tts_role_enabled(guild_id))
            except Exception:
                pass
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                defaults = db.get_guild_tts_defaults(guild_id) or {}
                if "ignored_tts_role_enabled" in defaults:
                    return bool(defaults.get("ignored_tts_role_enabled", False))
                return bool(self._get_ignored_tts_role_id(guild_id, guild_defaults=defaults))
            except Exception:
                pass
        return bool(self._get_ignored_tts_role_id(guild_id))

    def _get_ignored_tts_role(self, guild: discord.Guild | None, *, guild_defaults: dict | None = None) -> discord.Role | None:
        if guild is None:
            return None
        role_id = self._get_ignored_tts_role_id(guild.id, guild_defaults=guild_defaults)
        if role_id <= 0:
            return None
        return guild.get_role(role_id)

    def _ignored_tts_role_text(self, guild_id: int, *, guild_defaults: dict | None = None) -> str:
        role_id = self._get_ignored_tts_role_id(guild_id, guild_defaults=guild_defaults)
        enabled = self._ignored_tts_role_enabled(guild_id, guild_defaults=guild_defaults)
        if role_id <= 0:
            return "desligado"
        guild = self.bot.get_guild(guild_id)
        role = guild.get_role(role_id) if guild is not None else None
        mention = role.mention if role is not None else f"<@&{role_id}>"
        if enabled:
            return f"{mention} · ligado"
        return f"desligado · {mention} salvo"

    def _member_has_ignored_tts_role(self, member: discord.Member | None, *, guild_defaults: dict | None = None) -> bool:
        if member is None or member.guild is None:
            return False
        if not self._ignored_tts_role_enabled(member.guild.id, guild_defaults=guild_defaults):
            return False
        ignored_role_id = self._get_ignored_tts_role_id(member.guild.id, guild_defaults=guild_defaults)
        if ignored_role_id <= 0:
            return False
        return any(int(getattr(role, "id", 0) or 0) == ignored_role_id for role in getattr(member, "roles", []))

    def _spoken_name_suffix(self, member: discord.Member | None, *, guild_defaults: dict | None = None) -> str:
        if member is None:
            return ""

        is_muted = False
        voice_state = getattr(member, "voice", None)
        if voice_state is not None:
            try:
                is_muted = bool(getattr(voice_state, "mute", False))
            except Exception:
                is_muted = False

        ignores_tts = self._member_has_ignored_tts_role(member, guild_defaults=guild_defaults)

        if is_muted and ignores_tts:
            return " [ultra-censurado]"
        if is_muted:
            return " [censurado]"
        if ignores_tts:
            return " [bot ignora]"
        return ""

    def _apply_author_prefix_if_needed(self, guild_id: int, author: discord.abc.User | None, text: str, *, enabled: bool) -> str:
        text = str(text or "").strip()
        if not enabled or not text:
            return text
        author_id = int(getattr(author, "id", 0) or 0)
        if not author_id:
            return text
        last_author_id = int(self._last_announced_author_by_guild.get(int(guild_id), 0) or 0)
        self._last_announced_author_by_guild[int(guild_id)] = author_id
        if last_author_id == author_id:
            return text
        speaker = self._tts_user_reference(author, guild_id=guild_id)
        return f"{speaker} disse, {text}" if speaker else text


    def _panel_actor_name(self, interaction: discord.Interaction) -> str:
        member = getattr(interaction, "user", None)
        return self._member_actor_name(member)

    def _member_actor_name(self, member) -> str:
        if member is None:
            return "@usuário"

        name = (
            getattr(member, "display_name", None)
            or getattr(member, "global_name", None)
            or getattr(member, "name", None)
            or "usuário"
        )
        if not str(name).startswith("@"):
            return f"@{name}"
        return str(name)

    def _encode_public_owner_history(self, owner_id: int, actor_name: str, action_text: str) -> str:
        safe_actor = str(actor_name or "@usuário").replace("|", "/")
        safe_action = str(action_text or "").replace("|", "/")
        return f"__PUBLIC_OWNER_SELF__|{int(owner_id)}|{safe_actor}|{safe_action}"

    def _decode_public_owner_history(self, entry: str) -> tuple[int, str, str] | None:
        raw = str(entry or "")
        prefix = "__PUBLIC_OWNER_SELF__|"
        if not raw.startswith(prefix):
            return None
        try:
            _, owner_id, actor_name, action_text = raw.split("|", 3)
            return int(owner_id), actor_name, action_text
        except (TypeError, ValueError):
            return None

    def _render_history_entry(self, entry: str, *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        decoded = self._decode_public_owner_history(entry)
        if not decoded:
            return str(entry or "")

        owner_id, actor_name, action_text = decoded
        state = self._public_panel_states.get(message_id or 0, {}) if message_id else {}
        is_public_user_panel = bool(state and state.get("panel_kind") == "user")
        public_panel_owner_id = int(state.get("owner_id", 0) or 0) if state else 0

        if viewer_user_id == owner_id:
            if is_public_user_panel:
                if public_panel_owner_id == owner_id:
                    return f"Você ({actor_name}) {action_text}"
                return f"{actor_name} {action_text}"
            return f"Você {action_text}"

        return f"{actor_name} {action_text}"

    def _quote_value(self, value: str) -> str:
        return f'"{value}"'

    def _history_actor_label(self, entry: str, rendered: str) -> str:
        decoded = self._decode_public_owner_history(entry)
        if decoded:
            return decoded[1]

        text = str(rendered or entry or "").strip().replace("`", "")
        match = re.match(r"^(@.+?)\s+(?:alterou|ativou|desativou|removeu|resetou)\b", text, flags=re.I)
        if match:
            return match.group(1).strip()
        match = re.match(r"^Você\s+\((@[^)]+)\)", text, flags=re.I)
        if match:
            return match.group(1).strip()
        return ""

    def _compact_history_text(self, rendered: str) -> str:
        text = str(rendered or "").strip().replace("`", "")
        if not text:
            return ""

        lowered = text.lower()
        value = ""
        if " para " in text:
            value = text.rsplit(" para ", 1)[-1].strip().strip('"')

        if "prefixo" in lowered:
            if "teto" in lowered:
                label = "Prefixo Teto"
            elif "atts" in lowered:
                label = "Prefixo ATTS"
            elif "edge" in lowered:
                label = "Prefixo Edge"
            elif "gtts" in lowered:
                label = "Prefixo gTTS"
            elif "comandos" in lowered or "bot" in lowered:
                label = "Prefixo do bot"
            else:
                label = "Prefixos"
            return f"{label}: {value}" if value else f"{label} atualizado"
        if "gtts" in lowered and "idioma" in lowered:
            return f"gTTS: idioma {value}" if value else "gTTS: idioma atualizado"
        if "edge" in lowered and "voz" in lowered:
            return f"Edge: voz {human_voice_name(value)}" if value else "Edge: voz atualizada"
        if "edge" in lowered and "tom" in lowered:
            return f"Edge: tom {human_pitch(value)}" if value else "Edge: tom atualizado"
        if "edge" in lowered and "velocidade" in lowered:
            return f"Edge: velocidade {human_rate(value)}" if value else "Edge: velocidade atualizada"
        if "edge" in lowered:
            return f"Edge: {value}" if value else "Edge atualizado"
        if "voz" in lowered:
            return f"Edge: voz {human_voice_name(value)}" if value else "Edge: voz atualizada"
        if "tom" in lowered:
            return f"Edge: tom {human_pitch(value)}" if value else "Edge: tom atualizado"
        if "velocidade" in lowered:
            return f"Edge: velocidade {human_rate(value)}" if value else "Edge: velocidade atualizada"
        if "apelido" in lowered or "nome falado" in lowered:
            return "Apelido atualizado" if value else "Apelido limpo"
        if "autor" in lowered:
            return "Autor antes da frase: ligado" if any(x in lowered for x in ("ativ", "lig", "on", "true")) else "Autor antes da frase: desligado"
        if "cargo ignorado" in lowered:
            clean_value = re.sub(r"^cargo ignorado\s+", "", value, flags=re.I).strip() if value else ""
            return f"Cargo ignorado: {clean_value}" if clean_value else "Cargo ignorado atualizado"
        if "regras" in lowered:
            return "Regras atualizadas"
        if "modo" in lowered:
            return f"Modo de TTS: {value}" if value else "Modo de TTS atualizado"

        # Limpa as frases antigas, sem inventar informação.
        text = re.sub(r"^Você(?: \([^)]*\))?\s+alterou\s+", "", text, flags=re.I)
        text = re.sub(r"^.+?\s+alterou\s+", "", text, flags=re.I)
        return text[:90]

    def _format_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        for entry in entries[-3:]:
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=message_id)
            safe = self._compact_history_text(rendered)
            actor = self._history_actor_label(entry, rendered)
            if safe:
                if actor:
                    if not actor.startswith("@"):
                        actor = f"@{actor}"
                    safe = f"{safe} ({actor})"
                lines.append(f"• {safe}")
        return "\n".join(lines)

    def _format_status_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        recent_entries = entries[-2:]
        for idx, entry in enumerate(recent_entries):
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=None)
            safe = rendered.replace("`", "'")
            line = f"• {safe}"
            if idx == len(recent_entries) - 1:
                line = f"**{line}**"
            lines.append(line)
        return "\n".join(lines)

    def _get_public_panel_history(self, message_id: int | None) -> list[str]:
        if not message_id:
            return []
        state = self._public_panel_states.get(message_id, {}) or {}
        return [str(x) for x in (state.get("history", []) or []) if str(x or "").strip()]

    def _merge_history_entries(self, *groups: list[str] | tuple[str, ...]) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for entry in (group or []):
                clean = str(entry or "").strip()
                if not clean:
                    continue
                if merged and merged[-1] == clean:
                    continue
                merged.append(clean)
        return merged[-3:]

    def _append_public_panel_history(self, message_id: int | None, text: str):
        if not message_id:
            return
        state = self._public_panel_states.get(message_id)
        if state is None:
            state = {"history": []}
            self._public_panel_states[message_id] = state
        history = self._merge_history_entries(state.get("history", []) or [], [text] if text else [])
        state["history"] = history

    def _resolve_last_changes(self, *, stored_changes: list[str] | None = None, message_id: int | None = None) -> list[str]:
        stored = [str(x) for x in (stored_changes or []) if str(x or "").strip()]
        if not message_id or message_id not in self._public_panel_states:
            return stored
        public_history = self._get_public_panel_history(message_id)
        return self._merge_history_entries(stored, public_history)

    def _resolve_public_panel_message(self, interaction: discord.Interaction, source_panel_message: discord.Message | None = None) -> tuple[discord.Message | None, int | None]:
        direct_message = getattr(interaction, "message", None)
        direct_id = getattr(direct_message, "id", None)
        if direct_id in self._public_panel_states:
            return direct_message, direct_id

        source_id = getattr(source_panel_message, "id", None)
        if source_id in self._public_panel_states:
            return source_panel_message, source_id

        if source_panel_message is not None:
            return source_panel_message, source_id

        return direct_message, direct_id

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._voice_connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._voice_connect_locks[guild_id] = lock
        return lock


    def _prefix_panel_key(self, guild_id: int, user_id: int, panel_kind: str) -> tuple[int, int, str]:
        return (guild_id, user_id, panel_kind)

    async def _delete_prefix_panel(self, guild_id: int, user_id: int, panel_kind: str):
        key = self._prefix_panel_key(guild_id, user_id, panel_kind)
        message = self._active_prefix_panels.pop(key, None)
        if not message:
            return
        self._public_panel_states.pop(getattr(message, "id", None), None)
        try:
            await message.delete()
        except Exception:
            pass

    async def _check_prefix_panel_cooldown(self, message: discord.Message, panel_kind: str) -> bool:
        if not message.guild:
            return False

        now = time.monotonic()
        key = self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)
        expires_at = self._prefix_panel_cooldowns.get(key, 0.0)

        if expires_at > now:
            remaining = max(1, int(expires_at - now + 0.999))
            embed = discord.Embed(
                title="Calma aí",
                description=f"Você precisa esperar **{remaining}s** para usar esse comando de painel novamente",
                color=discord.Color.red(),
            )
            await message.channel.send(embed=embed)
            return True

        self._prefix_panel_cooldowns[key] = now + 5.0

        stale = [k for k, ts in self._prefix_panel_cooldowns.items() if ts < now - 60.0]
        for stale_key in stale:
            self._prefix_panel_cooldowns.pop(stale_key, None)

        return False

    async def _send_prefix_panel(
        self,
        message: discord.Message,
        *,
        panel_kind: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ):
        if not message.guild:
            return

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        content, send_embed, send_view = self._prepare_panel_payload(embed=embed, view=view)
        sent = await message.channel.send(
            content=content,
            embed=send_embed,
            view=send_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = sent
        db = self._get_db()
        initial_history: list[str] = []
        if db and hasattr(db, "get_panel_history"):
            panel_history = await self._maybe_await(db.get_panel_history(message.guild.id, message.author.id))
            if panel_kind == "server":
                initial_history = list((panel_history or {}).get("server_last_changes", []) or [])
            elif panel_kind == "toggle":
                initial_history = list((panel_history or {}).get("toggle_last_changes", []) or [])
            else:
                initial_history = list((panel_history or {}).get("user_last_changes", []) or [])
        self._public_panel_states[sent.id] = {"panel_kind": panel_kind, "history": self._merge_history_entries(initial_history), "owner_id": message.author.id}
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent

    def _mark_tts_message_seen(self, message_id: int) -> None:
        now = time.monotonic()
        self._recent_tts_message_ids[message_id] = now
        cutoff = now - 30.0
        stale = [mid for mid, ts in self._recent_tts_message_ids.items() if ts < cutoff]
        for mid in stale:
            self._recent_tts_message_ids.pop(mid, None)

    def _was_tts_message_seen(self, message_id: int) -> bool:
        ts = self._recent_tts_message_ids.get(message_id)
        if ts is None:
            return False
        if time.monotonic() - ts > 30.0:
            self._recent_tts_message_ids.pop(message_id, None)
            return False
        return True

    def _record_tts_message_gate(self, message: discord.Message, reason: str, *, matched: bool = False) -> None:
        try:
            metrics = self._get_metrics_store()
            metrics["message_gate_seen"] = int(metrics.get("message_gate_seen", 0) or 0) + 1
            if matched:
                metrics["message_gate_matched"] = int(metrics.get("message_gate_matched", 0) or 0) + 1
            else:
                metrics["message_gate_ignored"] = int(metrics.get("message_gate_ignored", 0) or 0) + 1
            metrics["last_message_gate_reason"] = str(reason or "")[:80]
            metrics["last_message_gate_guild_id"] = int(getattr(getattr(message, "guild", None), "id", 0) or 0)
            metrics["last_message_gate_channel_id"] = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
            metrics["last_message_gate_author_id"] = int(getattr(getattr(message, "author", None), "id", 0) or 0)
            metrics["last_message_gate_seen_at"] = time.time()
        except Exception:
            pass

    async def handle_message_from_bot_on_message(self, message: discord.Message) -> None:
        # Ponte explícita usada por bot.py. Mesmo que o dispatcher de listeners do
        # discord.py mude/atrase, o TTS continua recebendo mensagens. O cache
        # `_recent_tts_message_ids` evita enqueue duplicado quando o listener do
        # Cog também roda normalmente.
        await self.on_message(message)

    async def _load_edge_voices(self):
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")
            self.edge_voice_cache = []
            self.edge_voice_names = set()

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return make_embed(title, description, ok=ok)

    def _is_components_v2_panel_view(self, view: discord.ui.View | None) -> bool:
        checker = getattr(view, "is_components_v2_panel", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _prepare_panel_payload(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> tuple[str | None, discord.Embed | None, discord.ui.View | None]:
        if self._is_components_v2_panel_view(view):
            setter = getattr(view, "set_panel_embed", None)
            if callable(setter):
                try:
                    setter(embed)
                except Exception as e:
                    print(f"[tts_panel] falha ao preparar payload v2: {e!r}")
                    return content, embed, view
            # Components V2 não deve ser enviado junto de embed/content tradicional.
            return None, None, view
        return content, embed, view

    async def _edit_panel_message_payload(
        self,
        message: discord.Message,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        content: str | None = None,
    ):
        content, embed, view = self._prepare_panel_payload(content=content, embed=embed, view=view)
        return await message.edit(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ):
        content, embed, view = self._prepare_panel_payload(content=content, embed=embed, view=view)
        if interaction.response.is_done():
            response_type = getattr(interaction.response, "type", None)
            if response_type == discord.InteractionResponseType.deferred_channel_message:
                await interaction.edit_original_response(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    return await interaction.original_response()
                except Exception:
                    return None

            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            return await interaction.original_response()
        except Exception:
            return None

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _require_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Comando indisponível", "Esse comando só pode ser usado dentro de um servidor.", ok=False), ephemeral=True)
        return False

    async def _require_manage_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.", ok=False), ephemeral=True)
        return False

    async def _require_kick_members(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.kick_members:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    async def _require_staff_or_kick_members(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and (perms.kick_members or perms.manage_guild or perms.administrator):
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa ser staff ou ter a permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    def _format_metric_ms(self, value) -> str:
        try:
            return f"{float(value):.2f} ms"
        except Exception:
            return "n/a"

    def _format_bytes_human(self, value: int | float) -> str:
        try:
            size = float(value)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.2f} {units[idx]}"

    def _build_tts_perf_embeds(self, guild: discord.Guild | None) -> list[discord.Embed]:
        snapshot = {}
        get_snapshot = getattr(self.bot, "get_health_snapshot", None)
        if callable(get_snapshot):
            try:
                snapshot = get_snapshot() or {}
            except Exception:
                snapshot = {}

        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        engine_metrics = dict(tts_metrics.get("engines") or {})

        total_tmp_bytes = 0
        runtime_count = 0
        cache_count = 0
        tmp_files = []
        try:
            tmp_files = self._list_tmp_audio_files()
        except Exception:
            tmp_files = []
        for _, _, size, path in tmp_files:
            total_tmp_bytes += int(size or 0)
            parent_name = os.path.basename(os.path.dirname(path)).lower()
            if parent_name == "runtime":
                runtime_count += 1
            elif parent_name == "cache":
                cache_count += 1

        uptime_seconds = snapshot.get("uptime_seconds")
        try:
            uptime_seconds = int(float(uptime_seconds or 0))
        except Exception:
            uptime_seconds = 0
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_parts = []
        if days:
            uptime_parts.append(f"{days}d")
        if hours:
            uptime_parts.append(f"{hours}h")
        if minutes:
            uptime_parts.append(f"{minutes}m")
        if seconds or not uptime_parts:
            uptime_parts.append(f"{seconds}s")
        uptime_text = " ".join(uptime_parts)

        cache_hits = int(tts_metrics.get("cache_hits", 0) or 0)
        cache_misses = int(tts_metrics.get("cache_misses", 0) or 0)
        total_cache_lookups = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_cache_lookups * 100.0) if total_cache_lookups else 0.0

        embed = discord.Embed(
            title="🛠️ Status técnico do TTS",
            description="Métricas internas do TTS e saúde atual do bot para diagnóstico rápido.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Saúde geral",
            value=(
                f"**Status:** `{snapshot.get('status', 'unknown')}`\n"
                f"**Healthy:** `{snapshot.get('healthy', False)}`\n"
                f"**Discord pronto:** `{snapshot.get('discord_ready', False)}`\n"
                f"**Mongo:** `{snapshot.get('mongo_ok', False)}`\n"
                f"**Uptime:** `{uptime_text}`\n"
                f"**Latência:** `{snapshot.get('latency_ms', 'n/a')} ms`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Fila e despacho",
            value=(
                f"**Na fila agora:** `{tts_metrics.get('queued_items_current', 0)}`\n"
                f"**Guild states:** `{tts_metrics.get('guild_states_current', 0)}`\n"
                f"**Enfileiradas:** `{tts_metrics.get('queue_enqueued', 0)}`\n"
                f"**Deduplicadas:** `{tts_metrics.get('queue_deduplicated', 0)}`\n"
                f"**Descartadas:** `{tts_metrics.get('queue_dropped', 0)}`\n"
                f"**Espera média:** `{self._format_metric_ms(tts_metrics.get('avg_queue_wait_ms'))}`\n"
                f"**Dispatch médio:** `{self._format_metric_ms(tts_metrics.get('avg_dispatch_ms'))}`\n"
                f"**Source setup:** `{self._format_metric_ms(tts_metrics.get('avg_source_setup_ms'))}`\n"
                f"**Play call:** `{self._format_metric_ms(tts_metrics.get('avg_play_call_ms'))}`\n"
                f"**Total até tocar:** `{self._format_metric_ms(tts_metrics.get('avg_total_to_playback_ms'))}`\n"
                f"**Playback médio:** `{self._format_metric_ms(tts_metrics.get('avg_playback_ms'))}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Cache e armazenamento",
            value=(
                f"**Cache hits:** `{cache_hits}`\n"
                f"**Cache misses:** `{cache_misses}`\n"
                f"**Hit rate:** `{cache_hit_rate:.1f}%`\n"
                f"**Cache stores:** `{tts_metrics.get('cache_stores', 0)}`\n"
                f"**tmp_audio:** `{self._format_bytes_human(total_tmp_bytes)}`\n"
                f"**Arquivos runtime:** `{runtime_count}`\n"
                f"**Arquivos cache:** `{cache_count}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Warmup",
            value=(
                f"**Warmups no boot:** `{tts_metrics.get('boot_warmups', 0)}`\n"
                f"**Último warmup:** `{self._format_metric_ms(tts_metrics.get('last_warmup_duration_ms'))}`"
            ),
            inline=False,
        )
        if guild is not None:
            try:
                state = self.guild_states.get(guild.id)
                guild_queue = state.queue.qsize() if state else 0
            except Exception:
                guild_queue = 0
            embed.add_field(
                name="Servidor atual",
                value=(
                    f"**Servidor:** `{guild.name}`\n"
                    f"**Fila deste servidor:** `{guild_queue}`"
                ),
                inline=False,
            )
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Visão técnica para staff/admin")

        engine_embed = discord.Embed(
            title="⚙️ Engines do TTS",
            description="Contadores e médias por engine desde o boot atual do bot.",
            color=discord.Color.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        if engine_metrics:
            for engine_name, data in sorted(engine_metrics.items()):
                engine_embed.add_field(
                    name=f"{engine_name}",
                    value=(
                        f"**Synths:** `{int(data.get('synth_count', 0) or 0)}`\n"
                        f"**Falhas:** `{int(data.get('synth_failures', 0) or 0)}`\n"
                        f"**Falhas consecutivas:** `{int(data.get('consecutive_failures', 0) or 0)}`\n"
                        f"**Hits cache:** `{int(data.get('cache_hits', 0) or 0)}`\n"
                        f"**Misses cache:** `{int(data.get('cache_misses', 0) or 0)}`\n"
                        f"**Média synth:** `{self._format_metric_ms(data.get('avg_synth_ms'))}`\n"
                        f"**Última synth:** `{self._format_metric_ms(data.get('last_synth_ms'))}`\n"
                        f"**Slow alerts:** `{int(data.get('slow_alerts', 0) or 0)}`\n"
                        f"**Último erro:** `{str(data.get('last_error') or 'nenhum')[:120]}`"
                    ),
                    inline=False,
                )
        else:
            engine_embed.description = "Ainda não há métricas de engine suficientes para mostrar aqui."
        if self.bot.user and self.bot.user.display_avatar:
            engine_embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        engine_embed.set_footer(text="Use isto para diagnosticar lentidão, cache e falhas por engine")

        return [embed, engine_embed]

    async def _require_toggle_allowed_guild(self, interaction: discord.Interaction) -> bool:
        guild_ids = getattr(config, "GUILD_IDS", []) or []
        if not guild_ids:
            return True
        guild = getattr(interaction, "guild", None)
        if guild and guild.id in guild_ids:
            return True
        await self._respond(interaction, embed=self._make_embed("Indisponível aqui", "Esse comando só está habilitado nos servidores definidos na env.", ok=False), ephemeral=True)
        return False

    def _normalize_rate_value(self, raw: str) -> str | None:
        return normalize_rate_value(raw)

    def _normalize_pitch_value(self, raw: str) -> str | None:
        return normalize_pitch_value(raw)

    def _coerce_setting_bool(self, value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "on", "ativado", "ativo", "sim"}:
            return True
        if text in {"false", "0", "no", "n", "off", "desativado", "inativo", "nao", "não"}:
            return False
        return default

    async def _get_guild_toggle_value(self, guild_id: int, *, public_key: str, raw_key: str, default: bool) -> bool:
        db = self._get_db()
        if db is None:
            return default
        try:
            raw_doc = getattr(db, "guild_cache", {}).get(guild_id, {}) or {}
            if raw_key in raw_doc:
                return self._coerce_setting_bool(raw_doc.get(raw_key), default)
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return self._coerce_setting_bool((data or {}).get(public_key), default)
        except Exception as e:
            print(f"[tts_voice] Erro ao ler {public_key} da guild {guild_id}: {e}")
            return default

    def _get_voice_client_for_guild(self, guild: discord.Guild | None) -> Optional[discord.VoiceClient]:
        if guild is None:
            return None

        for vc in self.bot.voice_clients:
            try:
                if vc.guild and vc.guild.id == guild.id:
                    return vc
            except Exception:
                continue

        return guild.voice_client

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel) -> bool:
        # Legacy toggle removed: nunca mais bloquear o TTS por presença de outro bot na call.
        return False

    def _music_player_is_active(self, guild_id: int) -> bool:
        router = getattr(getattr(self, "bot", None), "audio_router", None)
        is_music_active = getattr(router, "is_music_active", None)
        if not callable(is_music_active):
            return False
        try:
            return bool(is_music_active(int(guild_id)))
        except Exception:
            return False

    def _lavalink_music_should_own_voice(self, guild: discord.Guild | None) -> bool:
        """Evita que o TTS local roube a conexão enquanto o player de música via Wavelink está ativo.

        Quando uma música está resolvendo/iniciando pelo Wavelink, ainda pode não
        existir ``guild.voice_client``. Se o TTS conectar localmente nessa janela,
        o playback real cai em ``Already connected to a voice channel`` ou precisa
        derrubar o TTS. Preferimos ignorar/adiar esse TTS local e deixar o node
        ser o único dono da conexão.
        """
        if guild is None:
            return False
        router = getattr(getattr(self, "bot", None), "audio_router", None)
        if router is None:
            return False
        should_block_local = getattr(router, "should_block_tts_local_voice", None)
        if callable(should_block_local):
            try:
                return bool(should_block_local(guild.id))
            except Exception:
                pass
        lavalink_active = getattr(router, "is_lavalink_active_for_guild", None)
        if callable(lavalink_active):
            try:
                return bool(lavalink_active(guild.id))
            except Exception:
                pass
        return False

    async def _disconnect_and_clear(self, guild: discord.Guild):
        self._mark_manual_voice_disconnect(guild.id, seconds=60.0)
        self._suppress_runtime_voice_restore(guild.id, seconds=60.0)
        self._cancel_runtime_voice_restore(guild.id)
        self._remember_expected_voice_channel(guild.id, None)
        state = self._get_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()
        except Exception:
            pass
        self._last_announced_author_by_guild.pop(int(guild.id), None)
        if self._music_player_is_active(guild.id):
            print(f"[tts_voice] desconexão do TTS ignorada | player de música ativo | guild={guild.id}")
            return
        vc = self._get_voice_client_for_guild(guild)
        disconnected = False
        if vc and self._voice_client_is_connected(vc):
            try:
                if self._voice_client_is_playing(vc):
                    stopper = getattr(vc, "stop", None)
                    if callable(stopper):
                        result = stopper()
                        if asyncio.iscoroutine(result):
                            await result
            except Exception:
                pass
            try:
                await vc.disconnect(force=False)
                disconnected = True
            except Exception as e:
                print(f"[tts_voice] erro ao desconectar guild {guild.id}: {e}")
        if not disconnected and self._get_bot_voice_state_channel(guild) is not None:
            await self._clear_ghost_voice_state(guild, reason="disconnect_and_clear")
        await self._clear_remembered_voice_channel(guild.id)

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        await self._disconnect_and_clear(guild)

    def _voice_channel_has_only_bots_or_is_empty(self, voice_channel) -> bool:
        if voice_channel is None:
            return True
        members = list(getattr(voice_channel, "members", []))
        return not any(not m.bot for m in members)

    async def _disconnect_if_alone_or_only_bots(self, guild: discord.Guild):
        auto_leave_enabled = await self._get_guild_toggle_value(
            guild.id,
            public_key="auto_leave",
            raw_key="auto_leave_enabled",
            default=True,
        )
        if not auto_leave_enabled:
            return

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not self._voice_client_is_connected(vc) or self._voice_client_channel(vc) is None:
            return

        channel = self._voice_client_channel(vc)
        only_bots_or_empty = self._voice_channel_has_only_bots_or_is_empty(channel)

        should_defer = getattr(router, "should_defer_tts_auto_leave", None)
        if callable(should_defer):
            try:
                if should_defer(guild.id):
                    occupancy_update = getattr(router, "handle_music_voice_occupancy_update", None)
                    if callable(occupancy_update):
                        await occupancy_update(guild.id, auto_leave_enabled=True)
                    elif only_bots_or_empty:
                        schedule_idle = getattr(router, "schedule_music_idle_disconnect", None)
                        if callable(schedule_idle):
                            await schedule_idle(guild.id)
                    if only_bots_or_empty:
                        print(f"[tts_voice] auto-leave adiado | sessão de música em contagem AFK | guild={guild.id}")
                    return
            except Exception:
                pass

        if self._music_player_is_active(guild.id):
            if only_bots_or_empty:
                schedule_idle = getattr(router, "schedule_music_idle_disconnect", None)
                if callable(schedule_idle):
                    await schedule_idle(guild.id)
                print(f"[tts_voice] auto-leave adiado | player de música ativo em contagem AFK | guild={guild.id}")
            return

        if only_bots_or_empty:
            # TTS puro: não sai mais imediatamente. Dá 2s para estabilizar voice
            # state/reconexões rápidas e só então confirma se ainda está sozinho.
            await asyncio.sleep(2.0)
            auto_leave_enabled = await self._get_guild_toggle_value(
                guild.id,
                public_key="auto_leave",
                raw_key="auto_leave_enabled",
                default=True,
            )
            if not auto_leave_enabled:
                return
            vc = self._get_voice_client_for_guild(guild)
            if vc is None or not self._voice_client_is_connected(vc) or self._voice_client_channel(vc) is None:
                return
            if self._music_player_is_active(guild.id):
                return
            if not self._voice_channel_has_only_bots_or_is_empty(self._voice_client_channel(vc)):
                return
            print(f"[tts_voice] saindo da call | sozinho ou só com bots | guild={guild.id} channel={getattr(self._voice_client_channel(vc), 'id', None)}")
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(
        self,
        guild: discord.Guild,
        voice_channel,
        *,
        report_failure: bool = False,
        failure_context: str = "entrada na call",
    ) -> Optional[discord.VoiceClient]:
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected recebeu canal None | guild={guild.id}")
            if report_failure:
                self._schedule_voice_failure_report(
                    guild,
                    voice_channel,
                    reason="canal de voz ausente antes da conexão",
                    context=failure_context,
                )
            return None

        precheck_reason = self._diagnose_voice_connect_precheck(guild, voice_channel)
        if precheck_reason:
            print(f"[tts_voice] pré-checagem bloqueou conexão | guild={guild.id} channel={getattr(voice_channel, 'id', None)} reason={precheck_reason}")
            if report_failure:
                self._schedule_voice_failure_report(
                    guild,
                    voice_channel,
                    reason=precheck_reason,
                    context=failure_context,
                )
            return None

        if self._lavalink_music_should_own_voice(guild):
            print(f"[tts_voice] conexão local do TTS ignorada | player de música via Wavelink controlando voz | guild={guild.id}")
            return None

        async def _desired_self_deaf() -> bool:
            try:
                return bool(await self._voice_should_self_deaf(guild.id))
            except Exception:
                return True

        async def _ensure_expected_voice_state() -> None:
            should_self_deaf = await _desired_self_deaf()
            last_error = None
            for _ in range(3):
                try:
                    me = getattr(guild, "me", None)
                    me_voice = getattr(me, "voice", None)
                    target_channel = getattr(me_voice, "channel", None) or voice_channel
                    current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                    if me_voice and current_self_deaf == should_self_deaf:
                        return
                    await guild.change_voice_state(channel=target_channel, self_deaf=should_self_deaf)
                    await asyncio.sleep(0.35)
                    me = getattr(guild, "me", None)
                    me_voice = getattr(me, "voice", None)
                    current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                    if me_voice and current_self_deaf == should_self_deaf:
                        return
                except Exception as e:
                    last_error = e
                    await asyncio.sleep(0.35)
            if last_error is not None:
                print(
                    f"[tts_voice] falha ao aplicar estado de voz | guild={guild.id} channel={getattr(voice_channel, 'id', None)} self_deaf={should_self_deaf} error={last_error}"
                )

        async def _build_connect_kwargs() -> dict:
            should_self_deaf = await _desired_self_deaf()
            # Existe apenas um dono de reconexão por guild: este cog, protegido
            # por _voice_connect_locks. Se o loop interno do discord.py também
            # ficar ativo, uma tentativa antiga pode encerrar uma sessão nova
            # cerca de 30 s depois e iniciar um ciclo de desconexões forçadas.
            return {"self_deaf": should_self_deaf, "reconnect": False}

        lock = self._get_voice_connect_lock(guild.id)
        async with lock:
            vc = self._get_voice_client_for_guild(guild)
            if self._is_voice_client_stale(guild, vc):
                await self._recover_stale_voice_client(guild, reason="ensure_connected")
                vc = self._get_voice_client_for_guild(guild)
            if self._is_lavalink_voice_client(vc):
                if self._lavalink_music_should_own_voice(guild):
                    print(f"[tts_voice] conexão TTS ignorada | player Lavalink ativo | guild={guild.id}")
                    return None
                # Wavelink parado/órfão: force a limpeza para o TTS local não ficar
                # bloqueado depois de uma falha de música/TTS.
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
                vc = None
            if self._lavalink_music_should_own_voice(guild):
                print(f"[tts_voice] conexão local do TTS ignorada | música Lavalink aguardando conexão | guild={guild.id}")
                return None

            is_receive_client = bool(vc and hasattr(vc, "listen") and hasattr(vc, "is_listening"))

            if vc and self._voice_client_is_connected(vc) and self._voice_client_channel(vc) and self._voice_client_channel(vc).id == voice_channel.id:
                if not is_receive_client:
                    await _ensure_expected_voice_state()
                    await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                    self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                    self._runtime_voice_restore_failures[guild.id] = 0
                    self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                    self._cancel_runtime_voice_restore(guild.id)
                    self._clear_manual_voice_disconnect(guild.id)
                    self._schedule_voice_incident_recovery(guild, voice_channel, context="conexão de voz já estava saudável")
                    return vc
                try:
                    if self._voice_client_is_playing_or_paused(vc):
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._schedule_voice_incident_recovery(guild, voice_channel, context="cliente de voz ativo confirmou sessão saudável")
                        return vc
                except Exception:
                    pass
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None

            async def _fresh_connect() -> Optional[discord.VoiceClient]:
                connect_kwargs = await _build_connect_kwargs()
                new_vc = await voice_channel.connect(**connect_kwargs)
                await _ensure_expected_voice_state()
                await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                self._runtime_voice_restore_failures[guild.id] = 0
                self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                self._cancel_runtime_voice_restore(guild.id)
                self._clear_manual_voice_disconnect(guild.id)
                print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
                self._schedule_voice_incident_recovery(guild, voice_channel, context="nova conexão de voz concluída")
                return new_vc

            try:
                if vc and self._voice_client_is_connected(vc):
                    if is_receive_client:
                        try:
                            await vc.disconnect(force=True)
                        except Exception:
                            pass
                        return await _fresh_connect()
                    try:
                        await vc.move_to(voice_channel)
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        self._schedule_voice_incident_recovery(guild, voice_channel, context="movimentação de voz concluída")
                        return vc
                    except Exception as move_err:
                        msg = str(move_err).lower()
                        if "closing transport" in msg or "not connected to voice" in msg:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            return await _fresh_connect()
                        raise

                return await _fresh_connect()

            except Exception as e:
                msg = str(e).lower()
                current_vc = self._get_voice_client_for_guild(guild)
                if self._is_lavalink_voice_client(current_vc):
                    print(f"[tts_voice] conexão TTS ignorada após already-connected | player Lavalink ativo | guild={guild.id}")
                    return None

                if "already connected" in msg and current_vc and self._voice_client_is_connected(current_vc):
                    if self._voice_client_channel(current_vc) and self._voice_client_channel(current_vc).id == voice_channel.id:
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        self._schedule_voice_incident_recovery(guild, voice_channel, context="estado already-connected confirmado como saudável")
                        return current_vc
                    try:
                        await current_vc.move_to(voice_channel)
                        await _ensure_expected_voice_state()
                        await self._set_remembered_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._remember_expected_voice_channel(guild.id, getattr(voice_channel, "id", None))
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._cancel_runtime_voice_restore(guild.id)
                        self._clear_manual_voice_disconnect(guild.id)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        self._schedule_voice_incident_recovery(guild, voice_channel, context="recuperação por move_to concluída")
                        return current_vc
                    except Exception:
                        pass

                if "closing transport" in msg or "not connected to voice" in msg:
                    try:
                        if current_vc:
                            await current_vc.disconnect(force=True)
                    except Exception:
                        pass
                    try:
                        return await _fresh_connect()
                    except Exception as retry_err:
                        print(f"[tts_voice] Erro ao reconectar na guild {guild.id}: {retry_err}")
                        if report_failure:
                            self._schedule_voice_failure_report(
                                guild,
                                voice_channel,
                                reason=self._classify_voice_connect_exception(retry_err),
                                exc=retry_err,
                                context=failure_context,
                            )
                        return None

                print(f"[tts_voice] Erro ao conectar na guild {guild.id}: {e}")
                if report_failure:
                    self._schedule_voice_failure_report(
                        guild,
                        voice_channel,
                        reason=self._classify_voice_connect_exception(e),
                        exc=e,
                        context=failure_context,
                    )
                return None

    def _chunk_lines(self, lines: list[str], max_chars: int = 3500) -> list[str]:
        chunks, current, size = [], [], 0
        for line in lines:
            extra = len(line) + 1
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current, size = [line], extra
            else:
                current.append(line)
                size += extra
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _send_list_embeds(self, interaction: discord.Interaction, *, title: str, lines: list[str], footer: str):
        chunks = self._chunk_lines(lines)
        if not chunks:
            await self._respond(interaction, embed=self._make_embed(title, "Nenhum item encontrado.", ok=False), ephemeral=True)
            return
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})", description=f"```{chunk}```", color=discord.Color.blurple())
            embed.set_footer(text=footer)
            await self._respond(interaction, embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        gate = await analyze_message_for_tts(self, message)

        if gate.should_dispatch_prefix_command:
            self._record_tts_message_gate(message, gate.reason or "prefix_command", matched=True)
            if self._was_tts_message_seen(message.id):
                return
            self._mark_tts_message_seen(message.id)
            if await dispatch_prefix_control_command(self, message, gate.prefix_command):
                return

        if not gate.should_process_tts:
            # Mensagem comum continua silenciosa, mas o health passa a mostrar
            # o último motivo de gate. Isso evita outro caso de queue_enqueued=0
            # sem pista nenhuma.
            self._record_tts_message_gate(message, gate.reason or "ignored", matched=False)
            return

        guild_defaults = gate.guild_defaults
        forced_engine = str(gate.forced_engine or "")
        active_prefix = str(gate.active_prefix or "")
        self._record_tts_message_gate(message, gate.reason or "tts_prefix_matched", matched=True)

        if isinstance(message.author, discord.Member) and self._member_has_ignored_tts_role(message.author, guild_defaults=guild_defaults):
            self._record_tts_message_gate(message, "ignored_role", matched=False)
            logger.info(
                "[tts_voice] mensagem ignorada por cargo sem TTS | guild=%s channel=%s user=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
            )
            return

        if self._was_tts_message_seen(message.id):
            return
        self._mark_tts_message_seen(message.id)

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if voice_channel is None:
            self._record_tts_message_gate(message, "author_not_in_voice", matched=False)
            logger.info(
                "[tts_voice] mensagem TTS ignorada: autor não está em call | guild=%s channel=%s user=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
            )
            return

        logger.info(
            "[tts_voice] mensagem TTS recebida | guild=%s text_channel=%s voice_channel=%s user=%s prefix=%r forced_engine=%s reason=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            getattr(voice_channel, "id", None),
            getattr(message.author, "id", None),
            active_prefix,
            forced_engine or "default",
            gate.reason,
        )

        dispatch_result = await dispatch_message_tts(
            self,
            message,
            guild_defaults=guild_defaults,
            active_prefix=active_prefix,
            forced_engine=forced_engine,
        )
        payload = dispatch_result.payload
        if payload is None:
            self._record_tts_message_gate(message, "payload_empty", matched=False)
            logger.info(
                "[tts_voice] mensagem TTS não virou payload | guild=%s channel=%s user=%s reason=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                gate.reason,
            )
            return

        if dispatch_result.enqueued:
            logger.info(
                "[tts_voice] TTS enfileirado | guild=%s voice_channel=%s user=%s engine=%s dropped=%s deduplicated=%s dispatch_ms=%.1f",
                getattr(message.guild, "id", None),
                getattr(payload.queue_item, "channel_id", None),
                getattr(message.author, "id", None),
                getattr(payload.queue_item, "engine", None),
                dispatch_result.dropped_count,
                dispatch_result.deduplicated,
                dispatch_result.dispatch_ms,
            )
            try:
                self._schedule_tts_turbo_benchmark_if_needed(
                    message,
                    active_prefix,
                    payload.queue_item,
                    payload.resolved,
                )
            except Exception:
                logger.exception("[tts_benchmark] falha ao agendar benchmark turbo")
        else:
            logger.info(
                "[tts_voice] payload criado mas nada foi enfileirado | guild=%s channel=%s user=%s deduplicated=%s dropped=%s",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.author, "id", None),
                dispatch_result.deduplicated,
                dispatch_result.dropped_count,
            )

        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        me = getattr(guild, "me", None)
        vc = self._get_voice_client_for_guild(guild)

        if me and member.id == me.id:
            if after.channel is not None:
                self._remember_expected_voice_channel(guild.id, getattr(after.channel, "id", None))
                self._runtime_voice_restore_failures[guild.id] = 0
                self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                self._cancel_runtime_voice_restore(guild.id)
                self._clear_manual_voice_disconnect(guild.id)
                await self._set_remembered_voice_channel(guild.id, getattr(after.channel, "id", None))
                self._schedule_voice_incident_recovery(
                    guild,
                    after.channel,
                    context="voice_state confirmou conexão saudável",
                )
                desired_self_deaf = True
                try:
                    desired_self_deaf = bool(await self._voice_should_self_deaf(guild.id))
                except Exception:
                    desired_self_deaf = True
                current_self_deaf = bool(getattr(after, "self_deaf", False))
                if current_self_deaf != desired_self_deaf:
                    try:
                        await guild.change_voice_state(channel=after.channel, self_deaf=desired_self_deaf)
                        print(
                            f"[tts_voice] estado de voz corrigido após mudança de canal | guild={guild.id} channel={after.channel.id} self_deaf={desired_self_deaf}"
                        )
                    except Exception as e:
                        print(
                            f"[tts_voice] falha ao corrigir estado de voz no voice_state_update | guild={guild.id} channel={getattr(after.channel, 'id', None)} self_deaf={desired_self_deaf} error={e}"
                        )
            elif before.channel is not None:
                print(f"[tts_voice] bot saiu da call | guild={guild.id} channel={getattr(before.channel, 'id', None)}")
                expected_channel_id = int(self._expected_voice_channel_ids.get(guild.id, 0) or 0)
                remembered_channel_id = await self._get_remembered_voice_channel_id(guild.id)
                before_channel_id = int(getattr(before.channel, "id", 0) or 0)
                has_restore_target = expected_channel_id > 0 or remembered_channel_id > 0 or before_channel_id > 0
                manual_or_intentional = (
                    self._is_manual_voice_disconnect_recent(guild.id)
                    or self._runtime_voice_restore_is_suppressed(guild.id)
                    or not has_restore_target
                )
                if manual_or_intentional:
                    self._cancel_runtime_voice_restore(guild.id)
                    self._runtime_voice_restore_failures[guild.id] = 0
                    self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                    self._remember_expected_voice_channel(guild.id, None)
                    await self._clear_remembered_voice_channel(guild.id)
                    print(f"[tts_voice] saída intencional/guardada; restore ignorado | guild={guild.id}")
                else:
                    target_channel_id = before_channel_id or expected_channel_id or remembered_channel_id
                    if self._voice_auto_restore_enabled:
                        self._remember_expected_voice_channel(guild.id, target_channel_id)
                        await self._set_remembered_voice_channel(guild.id, target_channel_id)
                        if await self._runtime_should_restore_voice(guild.id):
                            await self._schedule_runtime_voice_restore(
                                guild,
                                channel_id=target_channel_id,
                                reason="voice_state_disconnect",
                                initial_delay=4.0,
                            )
                        else:
                            print(f"[tts_voice] restore em runtime não agendado porque auto-leave está ativo | guild={guild.id} channel={target_channel_id}")
                    else:
                        self._cancel_runtime_voice_restore(guild.id)
                        self._runtime_voice_restore_failures[guild.id] = 0
                        self._runtime_voice_restore_next_allowed_at[guild.id] = 0.0
                        self._remember_expected_voice_channel(guild.id, None)
                        await self._clear_remembered_voice_channel(guild.id)
                        print(f"[tts_voice] saída detectada; restore automático desativado | guild={guild.id}")

        if vc is None or not self._voice_client_is_connected(vc) or self._voice_client_channel(vc) is None:
            return

        await self._disconnect_if_alone_or_only_bots(guild)


    async def voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()
        voices = self.edge_voice_cache or sorted(self.edge_voice_names)
        voices = [voice for voice in voices if voice.lower().startswith("pt-")]

        results: list[app_commands.Choice[str]] = []
        for voice in voices:
            if current and current not in voice.lower():
                continue
            results.append(app_commands.Choice(name=voice[:100], value=voice))
            if len(results) >= 25:
                break
        return results

    async def language_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()

        results: list[app_commands.Choice[str]] = []
        for code, name in sorted(self.gtts_languages.items()):
            label = f"{code} — {name}"
            haystack = f"{code} {name}".lower()
            if current and current not in haystack:
                continue
            results.append(app_commands.Choice(name=label[:100], value=code))
            if len(results) >= 25:
                break
        return results


    async def _set_mode_common(self, interaction: discord.Interaction, *, mode: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = validate_mode(mode)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            title, desc = "Modo padrão atualizado", f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, engine=value)
            title, desc = "Modo atualizado", f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_voice_common(self, interaction: discord.Interaction, *, voice: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge. Use `/tts voices edge` para ver as opções.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            title, desc = "Voz padrão atualizada", f"A voz padrão do servidor agora é `{voice}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, voice=voice)
            title, desc = "Voz atualizada", f"A sua voz do Edge agora é `{voice}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_language_common(self, interaction: discord.Interaction, *, language: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = str(language or "").strip().lower()
        if value not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse código não foi encontrado na lista do gTTS. Toque em **Ver lista de idiomas** ou tente um destes exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=value))
            title, desc = "Idioma padrão atualizado", f"O idioma padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, language=value)
            title, desc = "Idioma atualizado", f"O seu idioma do gTTS agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_speed_common(self, interaction: discord.Interaction, *, speed: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_rate_value(speed)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title, desc = "Velocidade padrão atualizada", f"A velocidade padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, rate=value)
            title, desc = "Velocidade atualizada", f"A sua velocidade do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title, desc = "Tom padrão atualizado", f"O tom padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, pitch=value)
            title, desc = "Tom atualizado", f"O seu tom do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)


    @app_commands.command(name="menu", description="Abre um painel guiado para configurar o seu TTS")
    async def menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=False)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg



    async def _get_panel_command_mention(self, guild_id: int, panel_kind: str) -> str:
        command_path = {
            "user": "tts menu",
            "server": "tts server menu",
            "toggle": "toggle_menu",
        }.get(panel_kind, "tts menu")

        command_ids = await self._get_root_command_ids_cached()
        cmd_id = command_ids.get("tts")
        if cmd_id:
            return f"</{command_path}:{cmd_id}>"
        return f"`/{command_path}`"

    async def _get_panel_prefix_hint(self, guild_id: int, panel_kind: str) -> str:
        prefix_command = {
            "user": "panel",
            "server": "panel_server",
            "toggle": "panel_toggles",
        }.get(panel_kind, "panel")
        bot_prefix = getattr(config, "BOT_PREFIX", "_")

        db = self._get_db()
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id))
                bot_prefix = str((guild_defaults or {}).get("bot_prefix") or bot_prefix)
            except Exception:
                pass

        return f"`{bot_prefix}{prefix_command}`"

    async def _build_expired_panel_embed(self, guild_id: int, panel_kind: str) -> discord.Embed:
        slash_mention = await self._get_panel_command_mention(guild_id, panel_kind)
        prefix_hint = await self._get_panel_prefix_hint(guild_id, panel_kind)
        return build_expired_panel_embed(slash_mention=slash_mention, prefix_hint=prefix_hint)

    async def _build_expired_panel_message(self, guild_id: int, panel_kind: str) -> str:
        slash_mention = await self._get_panel_command_mention(guild_id, panel_kind)
        prefix_hint = await self._get_panel_prefix_hint(guild_id, panel_kind)
        return (
            "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
            f"Para abrir um novo painel, tente usar {slash_mention} novamente"
            f" — ou, se preferir, {prefix_hint}."
        )

    def _build_panel_view(self, owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None) -> discord.ui.View:
        return TTSMainPanelView(self, owner_id, guild_id, server=server, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    def _build_public_tts_launcher_view(self, guild_id: int, *, timeout: float = 300, history_text: str = "") -> discord.ui.View:
        return TTSPublicLauncherView(self, 0, guild_id, timeout=timeout, history_text=history_text)

    def _member_panel_name(self, member: discord.abc.User | None) -> str:
        if member is None:
            return "@usuário"
        name = (
            getattr(member, "display_name", None)
            or getattr(member, "global_name", None)
            or getattr(member, "name", None)
            or str(member)
        )
        return name if str(name).startswith("@") else f"@{name}"

    async def _resolve_member_from_text(self, guild: discord.Guild, raw: str) -> discord.Member | None:
        query = str(raw or "").strip()
        if not query:
            return None

        mention_match = re.fullmatch(r"<@!?(\d+)>", query)
        if mention_match:
            member_id = int(mention_match.group(1))
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        if query.isdigit():
            member_id = int(query)
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        lowered = query.lower()
        exact_matches: list[discord.Member] = []
        fuzzy_matches: list[discord.Member] = []
        for member in guild.members:
            candidates = [
                str(member),
                getattr(member, "display_name", "") or "",
                getattr(member, "global_name", "") or "",
                getattr(member, "name", "") or "",
            ]
            candidate_values = [c.strip() for c in candidates if str(c).strip()]
            if any(c.lower() == lowered for c in candidate_values):
                exact_matches.append(member)
                continue
            if any(lowered in c.lower() for c in candidate_values):
                fuzzy_matches.append(member)

        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        return None

    def _normalize_language_query(self, value: str) -> str:
        return normalize_language_query(value)

    def _resolve_gtts_language_input(self, raw_language: str) -> tuple[str | None, str | None]:
        return resolve_gtts_language_input(raw_language, self.gtts_languages, self.gtts_language_aliases)

    async def _prefix_set_lang(self, message: discord.Message, raw_language: str):
        if message.guild is None:
            return

        value = str(raw_language or "").strip()
        if not value:
            await message.channel.send(embed=self._make_embed("Idioma obrigatório", f"Use esse comando assim: `_set lang português` ou `_set lang pt-br`.", ok=False))
            return

        code, language_name = self._resolve_gtts_language_input(value)
        if code is None:
            await message.channel.send(embed=self._make_embed("Idioma inválido", "Não reconheci esse idioma do gTTS. Use um código como `pt-br`, `pt`, `en`, `es` ou um nome em português como `português` e `espanhol`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "set_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para alterar o idioma do gTTS.", ok=False))
            return

        history_entry = f"Você alterou o próprio idioma para {code}"
        await self._set_user_tts_and_refresh(message.guild.id, message.author.id, language=code, history_entry=history_entry)

        pretty_name = language_name or code
        await message.channel.send(embed=self._make_embed("Idioma atualizado", f"Seu idioma pessoal do gTTS agora é `{code}` ({pretty_name}).", ok=True))

    async def _prefix_reset_user(self, message: discord.Message, raw_target: str):
        if message.guild is None:
            return
        if not getattr(message.author.guild_permissions, "kick_members", False):
            await message.channel.send(embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para resetar as configurações de TTS de outro usuário.", ok=False))
            return

        target_text = str(raw_target or "").strip()
        if not target_text:
            await message.channel.send(embed=self._make_embed("Usuário obrigatório", "Use esse comando assim: `reset @usuário`, `reset ID` ou `reset tag`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as configurações.", ok=False))
            return

        member = await self._resolve_member_from_text(message.guild, target_text)
        if member is None:
            await message.channel.send(embed=self._make_embed("Usuário não encontrado", "Não consegui encontrar esse usuário. Use menção, ID ou tag exata do usuário no servidor.", ok=False))
            return

        history_entry = f"{self._member_panel_name(message.author)} resetou as configurações de TTS de {self._member_panel_name(member)} para os padrões do servidor"
        await self._reset_user_tts_and_refresh(message.guild.id, member.id, history_entry=history_entry)

        await message.channel.send(embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {self._member_panel_name(member)} agora seguem os padrões do servidor.", ok=True))

    def _resolve_target_user(self, interaction: discord.Interaction, target_user_id: int | None = None, target_user_name: str | None = None) -> tuple[int, str]:
        resolved_id = int(target_user_id or getattr(getattr(interaction, "user", None), "id", 0) or 0)
        resolved_name = str(target_user_name or self._member_panel_name(getattr(interaction, "user", None)))
        return resolved_id, resolved_name

    def _resolve_panel_target_user(
        self,
        interaction: discord.Interaction,
        *,
        server: bool,
        message_id: int | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ) -> tuple[int, str, bool]:
        resolved_id, resolved_name = self._resolve_target_user(interaction, target_user_id, target_user_name)

        if server or not message_id or message_id not in self._public_panel_states:
            return resolved_id, resolved_name, False

        state = self._public_panel_states.get(message_id, {}) or {}
        if state.get("panel_kind") != "user":
            return resolved_id, resolved_name, False

        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        explicit_target = target_user_id is not None or bool(str(target_user_name or "").strip())

        if explicit_target and resolved_id != actor_id:
            return resolved_id, resolved_name, False

        return actor_id, self._member_panel_name(getattr(interaction, "user", None)), True

    def _build_toggle_view(self, owner_id: int, guild_id: int, *, timeout: float = 180) -> discord.ui.View:
        return TTSTogglePanelView(self, owner_id, guild_id, timeout=timeout)


    async def _announce_panel_change(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        target_message: discord.Message | None = None,
    ):
        channel = interaction.channel
        if channel is None:
            return

        try:
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
            )
            if interaction.user and getattr(interaction.user, "display_avatar", None):
                embed.set_author(
                    name=str(interaction.user),
                    icon_url=interaction.user.display_avatar.url,
                )
            embed.set_footer(text="Alteração feita pelo painel de TTS")
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            print(f"[tts_voice] Falha ao anunciar alteração do painel: {e}")


    def _get_saved_spoken_name(self, guild_id: int | None, user_id: int | None) -> str:
        if not guild_id or not user_id:
            return ""
        db = self._get_db()
        if db is None or not hasattr(db, "get_user_tts"):
            return ""
        try:
            data = db.get_user_tts(int(guild_id), int(user_id)) or {}
        except Exception:
            return ""
        return _normalize_spaces(str((data or {}).get("speaker_name", "") or ""))


    def _validate_spoken_name_input(self, raw_value: str) -> tuple[str | None, str | None]:
        value = _normalize_spaces(str(raw_value or ""))
        if not value:
            return "", None
        if not _looks_pronounceable_for_tts(value):
            return None, "Esse apelido tem caracteres que o TTS não consegue pronunciar bem. Use letras, números, espaço, ponto, traço ou underline."
        spoken = _speech_name(value)
        if not spoken or not _looks_pronounceable_for_tts(spoken):
            return None, "Esse apelido não ficou pronunciável depois da normalização do TTS."
        return spoken[:32], None

    def _resolve_spoken_name(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> tuple[str, str]:
        if member is None:
            return "usuário", "padrão"

        guild_defaults = None
        if isinstance(member, discord.Member):
            try:
                guild_defaults = self.db.get_guild_tts_defaults(member.guild.id)
            except Exception:
                guild_defaults = None


        saved_spoken_name = self._get_saved_spoken_name(guild_id, getattr(member, "id", None))
        if saved_spoken_name:
            spoken = _speech_name(saved_spoken_name)
            if spoken and _looks_pronounceable_for_tts(spoken):
                return f"{spoken}", "personalizado"

        display_name = _normalize_spaces(getattr(member, "display_name", None) or "")
        username = _normalize_spaces(getattr(member, "name", None) or "")

        if _looks_pronounceable_for_tts(display_name):
            spoken = _speech_name(display_name)
            if spoken:
                return f"{spoken}", "apelido do servidor"

        if _looks_pronounceable_for_tts(username):
            spoken = _speech_name(username)
            if spoken:
                return f"{spoken}", "nome de usuário"

        return "usuário", "padrão"

    def _tts_user_reference(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> str:
        spoken, _ = self._resolve_spoken_name(member, guild_id=guild_id)
        return spoken

    def _tts_role_reference(self, role: discord.Role | None) -> str:
        name = _normalize_spaces(getattr(role, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"cargo {spoken}"
        return "cargo do discord"

    def _tts_channel_reference(self, channel) -> str:
        name = _normalize_spaces(getattr(channel, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"canal {spoken}"
        return "canal do discord"

    def _tts_link_reference(self, url: str, *, guild: discord.Guild | None = None) -> str:
        cleaned_url = str(url or "").strip().rstrip(".,!?)]}")
        match = DISCORD_CHANNEL_URL_PATTERN.fullmatch(cleaned_url)
        if match and guild is not None:
            channel_id = int(match.group(2))
            channel = guild.get_channel(channel_id)
            return self._tts_channel_reference(channel)

        try:
            parsed = urlparse(cleaned_url)
        except Exception:
            return "link"

        domain = _extract_primary_domain(parsed.hostname or "")
        if _looks_pronounceable_for_tts(domain):
            spoken = _speech_name(domain)
            if spoken:
                return f"link do {spoken}"
        return "link"

    def _tts_attachment_descriptions(self, attachments) -> list[str]:
        descriptions: list[str] = []
        for attachment in attachments or []:
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type == "image/gif" or filename.endswith(".gif"):
                descriptions.append("Anexo em GIF")
            elif content_type.startswith("image/") or filename.endswith(_ATTACHMENT_IMAGE_EXTENSIONS):
                descriptions.append("Anexo de imagem")
            elif content_type.startswith("video/") or filename.endswith(_ATTACHMENT_VIDEO_EXTENSIONS):
                descriptions.append("Anexo de vídeo")
        return descriptions

    def _append_tts_descriptions(self, text: str, descriptions: list[str]) -> str:
        return append_tts_descriptions(text, descriptions, normalize_spaces=_normalize_spaces)

    def _render_tts_text(self, message: discord.Message, raw_text: str) -> str:
        return render_message_tts_text(
            message,
            raw_text,
            guild_id=getattr(message.guild, "id", None),
            user_reference=self._tts_user_reference,
            role_reference=self._tts_role_reference,
            channel_reference=self._tts_channel_reference,
            link_reference=self._tts_link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

    def _user_history_text(self, interaction: discord.Interaction, what: str, value: str, *, message_id: int | None = None, target_user_id: int | None = None, target_user_name: str | None = None) -> str:
        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        target_id = int(target_user_id or actor_id or 0)
        target_name = str(target_user_name or self._panel_actor_name(interaction))
        action_text = f"alterou {what} para {value}"
        if target_id == actor_id:
            return self._encode_public_owner_history(actor_id, self._panel_actor_name(interaction), action_text)
        return f"{self._panel_actor_name(interaction)} alterou {what} de {target_name} para {value}"

    def _server_history_text(self, interaction: discord.Interaction, what: str, value: str) -> str:
        return f"{self._panel_actor_name(interaction)} alterou {what} para {value}"

    def _toggle_history_text(self, interaction: discord.Interaction, text: str) -> str:
        return f"{self._panel_actor_name(interaction)} {text}"


    async def _build_toggle_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        guild_defaults = guild_defaults or {}
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        return build_toggle_embed(
            auto_leave_enabled=bool(guild_defaults.get("auto_leave", True)),
            history_text=history_text,
        )

    def _setting_origin_label(self, user_settings: dict, key: str) -> str:
        return "Usuário" if str((user_settings or {}).get(key, "") or "").strip() else "Servidor"

    def _status_bool(self, value: bool) -> str:
        return "Ativado" if bool(value) else "Desativado"

    def _status_badge(self, value: bool, *, on: str = "Ativo", off: str = "Inativo") -> str:
        return status_badge(value, on=on, off=off)

    def _status_source_badge(self, source: str) -> str:
        from .utils.embed import status_source_badge as _status_source_badge
        return _status_source_badge(source)

    def _status_engine_label(self, engine: str) -> str:
        from .utils.embed import status_engine_label as _status_engine_label
        return _status_engine_label(engine)

    def _status_voice_channel_text(self, guild: discord.Guild | None, target_user_id: int) -> str:
        return status_voice_channel_text(guild, target_user_id)

    def _spoken_name_status_text(self, guild_id: int, member: discord.abc.User | None, *, resolved: dict | None = None) -> tuple[str, str]:
        active_name, active_source = self._resolve_spoken_name(member, guild_id=guild_id)
        custom_name = _normalize_spaces(str((resolved or {}).get("speaker_name", "") or ""))
        return spoken_name_status_text(active_name=active_name, active_source=active_source, custom_name=custom_name)

    async def _build_status_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        viewer_user_id: int | None = None,
        target_user_name: str | None = None,
        public: bool = False,
    ) -> discord.Embed:
        db = self._get_db()
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        user_settings = user_settings or {}
        resolved = resolved or {}

        guild = self.bot.get_guild(guild_id)
        vc = self._get_voice_client_for_guild(guild)
        state = self.guild_states.get(guild_id)
        queue_size = int(getattr(getattr(state, "queue", None), "qsize", lambda: 0)() if state else 0)
        is_connected = bool(vc and self._voice_client_is_connected(vc))
        is_playing = bool(vc and self._voice_client_is_playing_or_paused(vc))
        vc_channel = self._voice_client_channel(vc)
        bot_channel = getattr(vc_channel, "mention", None) or (f"`{getattr(vc_channel, 'name', 'Desconhecido')}`" if vc_channel is not None else "Desconectado")
        user_channel = self._status_voice_channel_text(guild, user_id)
        member = guild.get_member(user_id) if guild else None
        target_name = str(target_user_name or self._member_panel_name(member))
        spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("user_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        history_text = self._format_status_history_entries(stored_last_changes or [], viewer_user_id=viewer_user_id or user_id)
        return build_status_embed(
            member=member,
            target_name=target_name,
            user_id=user_id,
            viewer_user_id=int(viewer_user_id or user_id or 0),
            public=public,
            is_connected=is_connected,
            is_playing=is_playing,
            queue_size=queue_size,
            resolved=resolved,
            user_settings=user_settings,
            user_channel=user_channel,
            bot_channel=bot_channel,
            spoken_name_text=spoken_name_text,
            history_text=history_text,
        )

    def _build_status_view(self, owner_id: int, guild_id: int, *, target_user_id: int | None = None, target_user_name: str | None = None, timeout: float = 180) -> discord.ui.View:
        return TTSStatusView(self, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _build_settings_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        server: bool = False,
        panel_kind: str = "user",
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}

        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes: list[str] = []
        if panel_kind == "server":
            stored_last_changes = list((panel_history or {}).get("server_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("server_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        elif panel_kind == "toggle":
            stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        else:
            stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("user_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []

        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)

        if server:
            title = "TTS do servidor"
            description = "Padrões usados por quem ainda não configurou o próprio TTS."
        elif target_user_name and int(user_id or 0) != int(viewer_user_id or user_id or 0):
            title = f"TTS de {target_user_name}"
            description = "Ajustes salvos para esse usuário."
        else:
            title = "TTS"
            description = "Ajustes salvos só para você."
        member = self.bot.get_guild(guild_id).get_member(user_id) if (not server and self.bot.get_guild(guild_id)) else None
        spoken_name_text = None
        if not server and self._guild_announce_author_enabled(guild_defaults):
            spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        return build_settings_embed(
            title=title,
            description=description,
            resolved=resolved,
            guild_defaults=guild_defaults,
            history_text=history_text,
            server=server,
            panel_kind=panel_kind,
            spoken_name_text=spoken_name_text,
            ignored_tts_role_text=self._ignored_tts_role_text(guild_id, guild_defaults=guild_defaults) if server else None,
        )


    async def _apply_ignored_tts_role_from_panel(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        *,
        source_panel_message: discord.Message | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o cargo ignorado do servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        await self._maybe_await(
            db.set_guild_tts_defaults(
                interaction.guild.id,
                ignored_tts_role_id=int(role.id),
                ignored_tts_role_enabled=True,
            )
        )
        history_entry = self._server_history_text(interaction, "o cargo ignorado", role.mention)
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        if source_panel_message is not None:
            self._append_public_panel_history(getattr(source_panel_message, "id", None), history_entry)

        panel_message = source_panel_message
        if panel_message is not None:
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
            embed = await self._build_settings_embed(
                interaction.guild.id,
                interaction.user.id,
                server=True,
                panel_kind="server",
                last_changes=last_changes,
                message_id=getattr(panel_message, "id", None),
                viewer_user_id=interaction.user.id,
            )
            view = self._build_panel_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
            view.message = panel_message
            try:
                await self._edit_panel_message_payload(panel_message, embed=embed, view=view)
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel: {e!r}")

        title = "Cargo ignorado atualizado"
        description = f"Cargo ignorado: {role.mention} · ligado."
        await interaction.response.send_message(
            embed=self._make_embed(title, description, ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if panel_message is not None:
            await self._announce_panel_change(interaction, title=title, description=description, target_message=panel_message)

    async def _remove_ignored_tts_role_from_panel(
        self,
        interaction: discord.Interaction,
        *,
        source_panel_message: discord.Message | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para remover o cargo ignorado do servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        current_role_id = self._get_ignored_tts_role_id(interaction.guild.id)
        if current_role_id <= 0:
            await interaction.response.send_message(
                embed=self._make_embed("Nenhum cargo configurado", "Não existe cargo ignorado configurado no TTS deste servidor.", ok=False),
                ephemeral=True,
            )
            return

        current_role = interaction.guild.get_role(current_role_id)
        current_role_text = current_role.mention if current_role is not None else f"<@&{current_role_id}>"
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, ignored_tts_role_enabled=False))
        history_entry = self._server_history_text(interaction, "o cargo ignorado", "desligado")
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        if source_panel_message is not None:
            self._append_public_panel_history(getattr(source_panel_message, "id", None), history_entry)

        panel_message = source_panel_message
        if panel_message is not None:
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
            embed = await self._build_settings_embed(
                interaction.guild.id,
                interaction.user.id,
                server=True,
                panel_kind="server",
                last_changes=last_changes,
                message_id=getattr(panel_message, "id", None),
                viewer_user_id=interaction.user.id,
            )
            view = self._build_panel_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
            view.message = panel_message
            try:
                await self._edit_panel_message_payload(panel_message, embed=embed, view=view)
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel: {e!r}")

        title = "Cargo ignorado desativado"
        description = f"Cargo ignorado: desligado. O cargo {current_role_text} continua salvo."
        await interaction.response.send_message(
            embed=self._make_embed(title, description, ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if panel_message is not None:
            await self._announce_panel_change(interaction, title=title, description=description, target_message=panel_message)

    async def _apply_server_prefix_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        prefix_kind: str,
        prefix: str,
        panel_message: discord.Message,
    ):
        return await apply_server_prefix_from_modal(self, interaction, prefix_kind=prefix_kind, prefix=prefix, panel_message=panel_message)

    async def _panel_update_after_change(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        title: str,
        description: str,
        target_message: discord.Message | None = None,
    ):
        edited = False
        message_to_edit = target_message or getattr(interaction, "message", None)
        current_interaction_message = getattr(interaction, "message", None)

        target_id = getattr(message_to_edit, "id", None)
        state = self._public_panel_states.get(target_id or 0, {}) if target_id else {}
        if message_to_edit is not None and state.get("panel_kind") == "launcher":
            try:
                history_text = self._format_history_entries(
                    self._get_public_panel_history(target_id),
                    viewer_user_id=None,
                    message_id=target_id,
                ) or "• Nada recente."
                launcher_view = self._build_public_tts_launcher_view(
                    getattr(getattr(message_to_edit, "guild", None), "id", getattr(interaction.guild, "id", 0)),
                    timeout=300,
                    history_text=history_text,
                )
                launcher_view.message = message_to_edit
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=False)
                await self._edit_panel_message_payload(
                    message_to_edit,
                    embed=self._make_embed("TTS", "Cada prefixo escolhe um modo de voz.", ok=True),
                    view=launcher_view,
                )
                await interaction.followup.send(
                    embed=self._make_embed(title, description or "Salvo.", ok=True),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except Exception as e:
                print(f"[tts_panel] falha ao atualizar launcher público: {e!r}")

        if message_to_edit is not None and hasattr(view, "message"):
            view.message = message_to_edit

        try:
            if (
                message_to_edit is not None
                and current_interaction_message is not None
                and getattr(current_interaction_message, "id", None) == getattr(message_to_edit, "id", None)
                and not interaction.response.is_done()
            ):
                content, edit_embed, edit_view = self._prepare_panel_payload(embed=embed, view=view)
                await interaction.response.edit_message(content=content, embed=edit_embed, view=edit_view)
                edited = True
        except discord.NotFound as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")
        except Exception as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=False)
                await self._edit_panel_message_payload(message_to_edit, embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via message.edit: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via message.edit: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                content, edit_embed, edit_view = self._prepare_panel_payload(embed=embed, view=view)
                await interaction.followup.edit_message(
                    message_id=message_to_edit.id,
                    content=content,
                    embed=edit_embed,
                    view=edit_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via followup.edit_message: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via followup.edit_message: {e!r}")

        if not edited and current_interaction_message is not None:
            try:
                if hasattr(view, "message"):
                    view.message = current_interaction_message
                if not interaction.response.is_done():
                    content, edit_embed, edit_view = self._prepare_panel_payload(embed=embed, view=view)
                    await interaction.response.edit_message(
                        content=content,
                        embed=edit_embed,
                        view=edit_view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    content, edit_embed, edit_view = self._prepare_panel_payload(embed=embed, view=view)
                    await interaction.followup.edit_message(
                        message_id=current_interaction_message.id,
                        content=content,
                        embed=edit_embed,
                        view=edit_view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")

        if not edited:
            try:
                if not interaction.response.is_done():
                    content, send_embed, send_view = self._prepare_panel_payload(embed=embed, view=view)
                    await interaction.response.send_message(
                        content=content,
                        embed=send_embed,
                        view=send_view,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    content, send_embed, send_view = self._prepare_panel_payload(embed=embed, view=view)
                    await interaction.followup.send(
                        content=content,
                        embed=send_embed,
                        view=send_view,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception as e:
                print(f"[tts_panel] falha ao responder followup: {e!r}")



    async def _apply_mode_from_panel(self, interaction: discord.Interaction, mode: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_mode_from_panel(self, interaction, mode, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_voice_from_panel(self, interaction: discord.Interaction, voice: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_voice_from_panel(self, interaction, voice, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_language_from_panel(self, interaction, language, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_speed_from_panel(self, interaction, speed, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)


    async def _apply_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        return await apply_pitch_from_panel(self, interaction, pitch, server=server, source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name)








    async def _apply_spoken_name_from_modal(
        self,
        interaction: discord.Interaction,
        spoken_name: str,
        *,
        panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        return await apply_spoken_name_from_modal(self, interaction, spoken_name, panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_announce_author_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        return await apply_announce_author_from_panel(self, interaction, enabled, source_panel_message)


    async def _apply_auto_leave_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        return await apply_auto_leave_from_panel(self, interaction, enabled, source_panel_message)


    async def _join_from_panel(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        vc = await self._ensure_connected(
            interaction.guild,
            user_voice.channel,
            report_failure=True,
            failure_context=f"entrada manual pelo painel de TTS por {interaction.user} ({interaction.user.id})",
        )
        if vc is None or not self._voice_client_is_connected(vc):
            await interaction.response.send_message(
                embed=self._make_embed("Falha ao conectar", "Não consegui entrar na call agora.", ok=False),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._make_embed("Bot conectado", f"Entrei na call `{user_voice.channel.name}`.", ok=True),
            ephemeral=True,
        )



    async def _clear_queue_only(self, guild: discord.Guild | None, *, stop_playback: bool = True) -> int:
        if guild is None:
            return 0

        state = self._get_state(guild.id)
        cleared = 0

        while True:
            try:
                state.queue.get_nowait()
                state.queue.task_done()
                cleared += 1
            except Exception:
                break

        vc = self._get_voice_client_for_guild(guild)
        if stop_playback and vc and self._voice_client_is_connected(vc):
            try:
                if self._voice_client_is_playing_or_paused(vc):
                    vc.stop()
            except Exception:
                pass

        task = getattr(state, "worker_task", None)
        if task and not task.done():
            task.cancel()
            state.worker_task = None

        return cleared

    async def _apply_dashboard_enabled_state(self, guild: discord.Guild | None) -> int:
        """Descarta apenas falas pendentes quando o dashboard desativa o TTS.

        A reprodução atual não é interrompida e nenhum novo estado é criado.
        """
        if guild is None:
            return 0
        db = self._get_db()
        defaults = await self._maybe_await(db.get_guild_tts_defaults(guild.id)) if db else {}
        state = self.guild_states.get(int(guild.id))
        enabled = bool((defaults or {}).get("enabled", True))
        if state is not None:
            state.dashboard_enabled = enabled
        if enabled:
            return 0
        if state is None:
            return 0
        cleared = 0
        while True:
            try:
                item = state.queue.get_nowait()
                try:
                    self._decrement_pending_signature(state, item)
                finally:
                    state.queue.task_done()
                    cleared += 1
            except asyncio.QueueEmpty:
                break
        return cleared

    async def _prefix_leave(self, message: discord.Message):
        if not message.guild:
            return

        await self._disconnect_and_clear(message.guild)

        embed = discord.Embed(
            title="Saindo da call",
            description="Saí da call e limpei a fila do TTS",
            color=discord.Color.red(),
        )
        await message.channel.send(embed=embed)

    async def _prefix_clear(self, message: discord.Message):
        if not message.guild:
            return

        await self._clear_queue_only(message.guild, stop_playback=True)

        try:
            await message.add_reaction("<:r_dot:1480307087522140331>")
        except Exception:
            try:
                await message.add_reaction("🟥")
            except Exception:
                pass

    async def _prefix_join(self, message: discord.Message):
        if not message.guild:
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            embed = self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse comando", ok=False)
            await message.channel.send(embed=embed)
            return

        self._suppress_runtime_voice_restore(message.guild.id, seconds=12.0)
        self._cancel_runtime_voice_restore(message.guild.id)
        self._remember_expected_voice_channel(message.guild.id, getattr(author_voice.channel, "id", None))
        await self._set_remembered_voice_channel(message.guild.id, getattr(author_voice.channel, "id", None))
        self._clear_manual_voice_disconnect(message.guild.id)

        vc = await self._ensure_connected(
            message.guild,
            author_voice.channel,
            report_failure=True,
            failure_context=f"entrada manual por comando de prefixo por {message.author} ({message.author.id})",
        )
        if vc is None or not self._voice_client_is_connected(vc):
            embed = self._make_embed("Falha ao conectar", "Não consegui entrar na call agora", ok=False)
            await message.channel.send(embed=embed)
            return

        embed = self._make_embed("Entrei na call com sucesso", f"Entrei na call `{author_voice.channel.name}`", ok=True)
        await message.channel.send(embed=embed)

    async def _send_prefix_panel(
        self,
        message: discord.Message,
        *,
        panel_type: str,
        target_query: str = "",
        invoked_alias: str = "",
    ) -> bool:
        if not message.guild:
            return False

        panel_kind = "user"
        initial_history: list[str] = []
        db = self._get_db()
        target_member: discord.Member | None = None
        target_query = str(target_query or "").strip()
        invoked_alias_text = str(invoked_alias or "").strip().lower()
        short_panel_alias = bool(invoked_alias_text) and invoked_alias_text.endswith("p") and not invoked_alias_text.endswith("panel") and not invoked_alias_text.endswith("painel")

        if panel_type == "user" and target_query:
            # `_p` também pode ser alias do player de música. Só consumimos
            # `_p <algo>` quando o autor é staff e o alvo resolve claramente.
            if not getattr(message.author.guild_permissions, "kick_members", False):
                if short_panel_alias:
                    return False
                await message.channel.send(
                    embed=self._make_embed(
                        "Sem permissão",
                        "Você precisa da permissão `Expulsar Membros` para editar o TTS de outro usuário.",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True

            target_member = await self._resolve_member_from_text(message.guild, target_query)
            if target_member is None:
                if short_panel_alias:
                    return False
                await message.channel.send(
                    embed=self._make_embed(
                        "Usuário não encontrado",
                        "Use menção, ID ou nome exato do usuário.",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True

        history_user_id = int(getattr(target_member, "id", message.author.id) or message.author.id)
        if db and hasattr(db, "get_panel_history"):
            panel_history = await self._maybe_await(db.get_panel_history(message.guild.id, history_user_id))
            if panel_type == "server":
                initial_history = list((panel_history or {}).get("server_last_changes", []) or [])
            elif panel_type == "toggle":
                initial_history = list((panel_history or {}).get("toggle_last_changes", []) or [])
            else:
                # Painel novo começa com o histórico pessoal do usuário editado.
                # Se outro usuário interagir com a mesma mensagem depois, a ação
                # também entra no histórico temporário daquele painel.
                initial_history = list((panel_history or {}).get("user_last_changes", []) or [])

        if panel_type == "server":
            panel_kind = "server"
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor",
                    ok=False,
                )
                await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return True
            embed = await self._build_settings_embed(
                message.guild.id,
                message.author.id,
                server=True,
                panel_kind="server",
                last_changes=initial_history,
            )
            view = self._build_panel_view(0, message.guild.id, server=True, timeout=300)
        elif panel_type == "toggle":
            panel_kind = "toggle"
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel de toggles",
                    ok=False,
                )
                await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return True
            guild_ids = getattr(config, "GUILD_IDS", []) or []
            if guild_ids and message.guild.id not in guild_ids:
                return True
            embed = await self._build_toggle_embed(message.guild.id, message.author.id)
            view = self._build_toggle_view(0, message.guild.id, timeout=300)
        elif target_member is not None:
            panel_kind = "user_target"
            target_name = self._member_panel_name(target_member)
            embed = await self._build_settings_embed(
                message.guild.id,
                target_member.id,
                server=False,
                panel_kind="user",
                last_changes=initial_history,
                target_user_name=target_name,
                viewer_user_id=message.author.id,
            )
            view = self._build_panel_view(
                message.author.id,
                message.guild.id,
                server=False,
                timeout=300,
                target_user_id=target_member.id,
                target_user_name=target_name,
            )
        else:
            panel_kind = "launcher"
            history_text = self._format_history_entries(initial_history, viewer_user_id=None, message_id=None)
            embed = self._make_embed(
                "TTS",
                "Cada prefixo escolhe um modo de voz. Use os botões para abrir seus ajustes.",
                ok=True,
            )
            view = self._build_public_tts_launcher_view(message.guild.id, timeout=300, history_text=history_text)

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return True

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        content, send_embed, send_view = self._prepare_panel_payload(embed=embed, view=view)
        sent = await message.channel.send(
            content=content,
            embed=send_embed,
            view=send_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = sent
        self._public_panel_states[sent.id] = {
            "panel_kind": "user" if panel_kind == "user_target" else panel_kind,
            "history": self._merge_history_entries(initial_history),
            "owner_id": message.author.id,
            "target_user_id": int(getattr(target_member, "id", 0) or 0) if target_member is not None else None,
        }
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent
        return True

    async def _leave_from_panel(self, interaction: discord.Interaction):
        vc = self._get_voice_client_for_guild(interaction.guild)
        actual_channel = self._get_bot_voice_state_channel(interaction.guild)
        active_channel = getattr(vc, "channel", None) if vc and self._voice_client_is_connected(vc) else actual_channel
        if active_channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz agora.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        if active_channel and user_voice.channel.id != active_channel.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False),
                ephemeral=True,
            )
            return

        await self._disconnect_and_clear(interaction.guild)
        await interaction.response.send_message(
            embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True),
            ephemeral=True,
        )



    @app_commands.command(name="status", description="Mostra o status atual do TTS ou copia a configuração de outro usuário")
    @app_commands.describe(acao="Escolha se quer ver o seu status, mostrar o de outro usuário ou copiar a configuração dele", usuario="Usuário alvo quando a ação envolver outro usuário")
    @app_commands.choices(acao=STATUS_ACTION_CHOICES)
    async def status(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str] | None = None,
        usuario: discord.Member | None = None,
    ):
        if not await self._require_guild(interaction):
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        action_value = str(getattr(acao, "value", "self") or "self")
        if action_value == "self":
            await self._defer_ephemeral(interaction)
        elif not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if action_value == "show_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para mostrar o status público dele no chat.", ok=False), ephemeral=True)
                return
            embed = await self._build_status_embed(
                interaction.guild.id,
                usuario.id,
                viewer_user_id=interaction.user.id,
                target_user_name=self._member_panel_name(usuario),
                public=True,
            )
            embed.description = f"{self._member_panel_name(interaction.user)} mostrou no chat o status de TTS de {self._member_panel_name(usuario)}."
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        if action_value == "copy_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para copiar as configurações de TTS dele.", ok=False), ephemeral=True)
                return
            if usuario.id == interaction.user.id:
                await self._respond(interaction, embed=self._make_embed("Escolha outro usuário", "Você já está usando as suas próprias configurações. Escolha outro usuário para copiar as configurações dele.", ok=False), ephemeral=True)
                return

            resolved = await self._maybe_await(db.resolve_tts(interaction.guild.id, usuario.id))
            resolved = resolved or {}
            copied_engine = str(resolved.get('engine', 'gtts') or 'gtts').lower().replace('-', '_')
            if copied_engine in {'gcloud', 'google', 'google_cloud', 'googlecloud', 'google_tts'}:
                copied_engine = 'gtts'
            history_entry = f"{self._panel_actor_name(interaction)} copiou as configurações de TTS de {self._member_panel_name(usuario)}"
            await self._set_user_tts_and_refresh(
                interaction.guild.id,
                interaction.user.id,
                engine=copied_engine,
                voice=str(resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural')) or 'pt-BR-FranciscaNeural'),
                language=str(resolved.get('gtts_language', resolved.get('language', 'pt-br')) or 'pt-br'),
                rate=str(resolved.get('edge_rate', resolved.get('rate', '+0%')) or '+0%'),
                pitch=str(resolved.get('edge_pitch', resolved.get('pitch', '+0Hz')) or '+0Hz'),
                history_entry=history_entry,
            )

            embed = self._make_embed(
                "Configurações copiadas",
                f"{self._member_panel_name(interaction.user)} copiou as configurações de TTS de {self._member_panel_name(usuario)}.",
                ok=True,
            )
            embed.add_field(name="Engine", value=f"`{copied_engine}`", inline=True)
            embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural'))}`", inline=True)
            embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'pt-br'))}`", inline=True)
            embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
            embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        embed = await self._build_status_embed(
            interaction.guild.id,
            interaction.user.id,
            viewer_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
            public=False,
        )
        view = self._build_status_view(
            interaction.user.id,
            interaction.guild.id,
            target_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg

    # O antigo /tts usuario foi removido. Staff deve usar o comando prefixado
    # `_panel <usuário>`/`_p <usuário>` para abrir o painel de outro membro.
    async def _legacy_usuario_slash_removed(self, interaction: discord.Interaction, usuario: discord.Member, acao: app_commands.Choice[str]):
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        target_name = self._member_panel_name(usuario)
        action_value = str(getattr(acao, "value", "") or "")

        if action_value == "spoken_name":
            current_value = self._get_saved_spoken_name(interaction.guild.id, usuario.id)
            await interaction.response.send_modal(
                SpokenNameModal(
                    self,
                    None,
                    target_user_id=usuario.id,
                    target_user_name=target_name,
                    current_value=current_value,
                )
            )
            return

        await self._defer_ephemeral(interaction)

        if action_value == "reset":
            if not hasattr(db, "reset_user_tts"):
                await self._respond(interaction, embed=self._make_embed("Função indisponível", "Esse banco ainda não suporta resetar as configurações do usuário.", ok=False), ephemeral=True)
                return
            history_entry = f"{self._panel_actor_name(interaction)} resetou as configurações de TTS de {target_name} para os padrões do servidor"
            await self._reset_user_tts_and_refresh(interaction.guild.id, usuario.id, history_entry=history_entry)
            embed = await self._build_settings_embed(
                interaction.guild.id,
                usuario.id,
                server=False,
                panel_kind="user",
                target_user_name=target_name,
                viewer_user_id=interaction.user.id,
            )
            await self._respond(interaction, embed=embed, ephemeral=True)
            await interaction.followup.send(
                embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {target_name} agora seguem os padrões do servidor.", ok=True),
                ephemeral=True,
            )
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            usuario.id,
            server=False,
            panel_kind="user",
            target_user_name=target_name,
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=usuario.id,
            target_user_name=target_name,
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


    @server.command(name="menu", description="Abre um painel guiado para configurar o TTS do servidor")
    async def server_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        msg = await self._respond(
            interaction,
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.message = msg

    async def toggle_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_toggle_allowed_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id)
        view = self._build_toggle_view(interaction.user.id, interaction.guild.id)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
