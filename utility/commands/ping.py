from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tts.aliases import matches_prefixed_command
from utility.interaction_safety import (
    safe_defer_interaction,
    safe_edit_original_or_message,
    safe_send_interaction_message,
)


logger = logging.getLogger(__name__)

try:
    import psutil
except Exception:  # pragma: no cover - o comando continua útil sem a dependência opcional
    psutil = None


_PROCESS = None
if psutil is not None:
    try:
        _PROCESS = psutil.Process(os.getpid())
        # A primeira leitura não bloqueante é apenas uma referência. Prepará-la
        # no import evita exibir 0% artificialmente no primeiro /ping.
        _PROCESS.cpu_percent(interval=None)
    except Exception:
        _PROCESS = None


_SEVERITY_STYLES: dict[int, tuple[str, str, discord.Color]] = {
    0: ("🟢", "Tudo funcionando normalmente", discord.Color.green()),
    1: ("🟡", "Funcionando com pequena variação", discord.Color.gold()),
    2: ("🟠", "Oscilação detectada", discord.Color.orange()),
    3: ("🔴", "Instabilidade detectada", discord.Color.red()),
}


@dataclass(frozen=True, slots=True)
class PingSnapshot:
    websocket_ms: float | None
    response_ms: float
    event_loop_ms: float | None
    database_ok: bool | None
    uptime_text: str
    memory_mb: float | None
    cpu_percent: float | None
    guild_count: int
    shard_text: str | None
    bot_healthy: bool | None


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, number)


def _format_duration(total_seconds: float | int | None) -> str:
    seconds = max(0, int(_safe_float(total_seconds) or 0))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _format_ms(value: float | None) -> str:
    if value is None:
        return "indisponível"
    return f"{round(value)} ms"


def _latency_severity(value: float | None, thresholds: tuple[float, float, float]) -> int:
    if value is None:
        return 1
    if value < thresholds[0]:
        return 0
    if value < thresholds[1]:
        return 1
    if value < thresholds[2]:
        return 2
    return 3


def _metric_icon(severity: int) -> str:
    return _SEVERITY_STYLES[max(0, min(3, int(severity)))][0]


def _database_display(database_ok: bool | None) -> tuple[str, str, int]:
    if database_ok is True:
        return "🟢", "Online", 0
    if database_ok is False:
        return "🔴", "Indisponível", 3
    return "🟡", "Verificando", 1


def _overall_severity(snapshot: PingSnapshot) -> int:
    severities = [
        _latency_severity(snapshot.websocket_ms, (150.0, 300.0, 600.0)),
        _latency_severity(snapshot.response_ms, (350.0, 900.0, 1_800.0)),
        _latency_severity(snapshot.event_loop_ms, (100.0, 350.0, 1_000.0)),
        _database_display(snapshot.database_ok)[2],
    ]
    if snapshot.bot_healthy is False:
        severities.append(3)
    return max(severities)


class PingLoadingView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🏓 Medindo…\n-# Consultando o estado atual do bot"),
                accent_color=discord.Color.blurple(),
            )
        )


class PingPanelView(discord.ui.LayoutView):
    def __init__(self, snapshot: PingSnapshot, *, thumbnail_url: str | None = None):
        super().__init__(timeout=None)

        severity = _overall_severity(snapshot)
        status_icon, status_text, accent = _SEVERITY_STYLES[severity]
        header_text = (
            "# 🏓 Pong!\n"
            f"{status_icon} **{status_text}**\n"
            "-# Status do bot medido em tempo real"
        )
        header: discord.ui.Item[Any] = discord.ui.TextDisplay(header_text)
        if thumbnail_url:
            header = discord.ui.Section(
                discord.ui.TextDisplay(header_text),
                accessory=discord.ui.Thumbnail(
                    thumbnail_url,
                    description="Avatar do bot",
                ),
            )

        websocket_severity = _latency_severity(snapshot.websocket_ms, (150.0, 300.0, 600.0))
        response_severity = _latency_severity(snapshot.response_ms, (350.0, 900.0, 1_800.0))
        loop_severity = _latency_severity(snapshot.event_loop_ms, (100.0, 350.0, 1_000.0))
        database_icon, database_text, _ = _database_display(snapshot.database_ok)
        connection_lines = [
            "## Conexão",
            f"{_metric_icon(websocket_severity)} **WebSocket** `{_format_ms(snapshot.websocket_ms)}`",
            f"{_metric_icon(response_severity)} **Resposta do comando** `{_format_ms(snapshot.response_ms)}`",
            f"{_metric_icon(loop_severity)} **Event loop** `{_format_ms(snapshot.event_loop_ms)}`",
            f"{database_icon} **Banco de dados** `{database_text}`",
        ]

        memory_text = "indisponível" if snapshot.memory_mb is None else f"{snapshot.memory_mb:.1f} MB"
        cpu_text = "indisponível" if snapshot.cpu_percent is None else f"{snapshot.cpu_percent:.1f}%"
        process_lines = [
            "## Processo",
            f"⏱️ **Ativo há** `{snapshot.uptime_text}`",
            f"🧠 **Memória** `{memory_text}` · **CPU** `{cpu_text}`",
            f"🌐 **Servidores** `{snapshot.guild_count}`",
        ]
        if snapshot.shard_text is not None:
            process_lines[-1] += f" · **Shard** `{snapshot.shard_text}`"

        self.add_item(
            discord.ui.Container(
                header,
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay("\n".join(connection_lines)),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay("\n".join(process_lines)),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(
                    "-# WebSocket mede a conexão com o Discord · Event loop mede o atraso interno"
                ),
                accent_color=accent,
            )
        )


class PingCommandMixin:
    """Entradas slash e prefixada do painel leve de status do bot."""

    def _collect_ping_snapshot(
        self,
        *,
        response_ms: float,
        guild: discord.Guild | None,
    ) -> PingSnapshot:
        websocket_seconds = _safe_float(getattr(self.bot, "latency", None))
        websocket_ms = None if websocket_seconds is None else websocket_seconds * 1_000.0

        # O monitor principal já valida o Mongo a cada 15s. Ler o estado em
        # memória evita uma nova consulta e também evita montar o snapshot TTS
        # completo só para responder ao ping.
        raw_health = getattr(self.bot, "health_state", {})
        health: dict[str, Any] = dict(raw_health) if isinstance(raw_health, dict) else {}

        event_loop_ms = _safe_float(
            health.get(
                "event_loop_last_lag_ms",
                getattr(self.bot, "_event_loop_last_lag_ms", None),
            )
        )
        if "mongo_ok" in health:
            database_ok: bool | None = bool(health.get("mongo_ok"))
        elif getattr(self.bot, "settings_db", None) is None:
            database_ok = False
        else:
            database_ok = None

        started_at = getattr(self.bot, "started_at", None)
        uptime_seconds: float | None = None
        if started_at is not None:
            try:
                uptime_seconds = max(0.0, (discord.utils.utcnow() - started_at).total_seconds())
            except Exception:
                uptime_seconds = None

        memory_mb: float | None = None
        cpu_percent: float | None = None
        if _PROCESS is not None:
            try:
                memory_mb = _PROCESS.memory_info().rss / 1_048_576
            except Exception:
                memory_mb = None
            try:
                cpu_percent = _safe_float(_PROCESS.cpu_percent(interval=None))
            except Exception:
                cpu_percent = None

        guild_count = len(getattr(self.bot, "guilds", ()) or ())
        try:
            shard_count = max(1, int(getattr(self.bot, "shard_count", 1) or 1))
        except (TypeError, ValueError):
            shard_count = 1
        shard_text: str | None = None
        if shard_count > 1:
            shard_id = getattr(guild, "shard_id", 0) if guild is not None else 0
            try:
                shard_number = max(0, int(shard_id or 0)) + 1
            except (TypeError, ValueError):
                shard_number = 1
            shard_text = f"{shard_number}/{shard_count}"

        bot_healthy: bool | None = None
        ready_check = getattr(self.bot, "is_ready", None)
        closed_check = getattr(self.bot, "is_closed", None)
        if callable(ready_check) and callable(closed_check) and database_ok is not None:
            try:
                ready = bool(ready_check())
                closed = bool(closed_check())
                failed_extensions = dict(getattr(self.bot, "failed_extensions", {}) or {})
                critical_failed = any(
                    isinstance(data, dict) and bool(data.get("critical"))
                    for data in failed_extensions.values()
                )
                starting = not ready and (uptime_seconds or 0.0) < 120.0
                bot_healthy = (
                    ready and not closed and database_ok is True and not critical_failed
                ) or starting
            except Exception:
                bot_healthy = None
        elif "healthy" in health:
            bot_healthy = bool(health.get("healthy"))

        return PingSnapshot(
            websocket_ms=websocket_ms,
            response_ms=max(0.0, float(response_ms)),
            event_loop_ms=event_loop_ms,
            database_ok=database_ok,
            uptime_text=(
                _format_duration(uptime_seconds)
                if uptime_seconds is not None
                else "indisponível"
            ),
            memory_mb=memory_mb,
            cpu_percent=cpu_percent,
            guild_count=guild_count,
            shard_text=shard_text,
            bot_healthy=bot_healthy,
        )

    def _ping_thumbnail_url(self) -> str | None:
        user = getattr(self.bot, "user", None)
        avatar = getattr(user, "display_avatar", None)
        url = getattr(avatar, "url", None)
        return str(url) if url else None

    async def _send_prefix_ping(self, message: discord.Message) -> None:
        started = time.perf_counter()
        try:
            response = await message.channel.send(
                view=PingLoadingView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("[utility/ping] falha ao reconhecer comando prefixado")
            return

        response_ms = (time.perf_counter() - started) * 1_000.0
        snapshot = self._collect_ping_snapshot(response_ms=response_ms, guild=message.guild)
        view = PingPanelView(snapshot, thumbnail_url=self._ping_thumbnail_url())
        try:
            await response.edit(view=view)
        except Exception:
            logger.exception("[utility/ping] falha ao finalizar painel prefixado")

    @commands.Cog.listener("on_message")
    async def on_ping_message(self, message: discord.Message) -> None:
        raw_content = str(getattr(message, "content", "") or "").strip()
        if getattr(message.author, "bot", False) or not raw_content:
            return
        # Evita até a leitura do prefixo em mensagens comuns. Como o comando
        # não aceita argumentos, qualquer entrada válida termina em `ping`.
        if not raw_content.casefold().endswith("ping"):
            return

        prefixes = await self._get_prefix_data(message.guild)
        bot_prefix = str(prefixes.get("bot_prefix") or "_")
        if not matches_prefixed_command(raw_content, bot_prefix, kind="ping"):
            return
        await self._send_prefix_ping(message)

    @app_commands.command(name="ping", description="Mostra a latência e a saúde atual do bot")
    async def ping(self, interaction: discord.Interaction) -> None:
        started = time.perf_counter()
        acknowledged = await safe_defer_interaction(
            interaction,
            thinking=True,
            ephemeral=True,
            log=logger,
            label="utility/ping",
        )
        if not acknowledged:
            return

        response_ms = (time.perf_counter() - started) * 1_000.0
        snapshot = self._collect_ping_snapshot(
            response_ms=response_ms,
            guild=interaction.guild,
        )
        view = PingPanelView(snapshot, thumbnail_url=self._ping_thumbnail_url())
        edited = await safe_edit_original_or_message(
            interaction,
            content=None,
            embeds=[],
            attachments=[],
            view=view,
            log=logger,
            label="utility/ping",
        )
        if not edited:
            await safe_send_interaction_message(
                interaction,
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                log=logger,
                label="utility/ping-fallback",
            )
