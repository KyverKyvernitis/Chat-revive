from __future__ import annotations

import logging
import os
from typing import Any

from discord.ext import commands, tasks

log = logging.getLogger(__name__)

_FALSE_VALUES = {"0", "false", "no", "off", "disabled", "disable"}


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSE_VALUES


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


class DashboardSync(commands.Cog):
    """Sincroniza o cache local do bot quando o Dashboard salva configs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = _env_enabled("DASHBOARD_SYNC_ENABLED", True)
        self.interval_seconds = max(5.0, _env_float("DASHBOARD_SYNC_INTERVAL_SECONDS", 10.0))
        self._last_seen_revision: dict[int, int] = {}
        self._prime_seen_revisions()

        if self.enabled:
            self.dashboard_sync_loop.change_interval(seconds=self.interval_seconds)
            self.dashboard_sync_loop.start()
            log.info("[dashboard_sync] ativo interval=%.1fs", self.interval_seconds)
        else:
            log.info("[dashboard_sync] desativado por DASHBOARD_SYNC_ENABLED")

    def cog_unload(self):
        self.dashboard_sync_loop.cancel()

    def _db(self):
        return getattr(self.bot, "settings_db", None)

    def _prime_seen_revisions(self):
        db = self._db()
        cache = getattr(db, "guild_cache", {}) or {}
        for guild_id, doc in list(cache.items()):
            gid = _to_int(guild_id)
            if gid <= 0 or not isinstance(doc, dict):
                continue
            self._last_seen_revision[gid] = _to_int(doc.get("dashboard_revision"), 0)

    @tasks.loop(seconds=10.0)
    async def dashboard_sync_loop(self):
        db = self._db()
        coll = getattr(db, "coll", None)
        if db is None or coll is None:
            return

        cursor = coll.find(
            {"type": "guild", "dashboard_revision": {"$exists": True}},
            {"_id": 0, "guild_id": 1, "dashboard_revision": 1, "dashboard_changed_sections": 1},
        )
        async for doc in cursor:
            guild_id = _to_int(doc.get("guild_id"), 0)
            revision = _to_int(doc.get("dashboard_revision"), 0)
            if guild_id <= 0 or revision <= 0:
                continue

            last_revision = self._last_seen_revision.get(guild_id)
            if last_revision is None:
                # Guild nova ou não cacheada ainda. Recarrega só ela para evitar
                # efeitos retroativos durante a inicialização do sincronizador.
                await self._reload_guild(db, guild_id, revision, doc.get("dashboard_changed_sections"), apply_changes=False)
                continue
            if revision > last_revision:
                await self._reload_guild(db, guild_id, revision, doc.get("dashboard_changed_sections"), apply_changes=True)

    async def _reload_guild(self, db: Any, guild_id: int, revision: int, sections: Any, *, apply_changes: bool):
        try:
            reload_one = getattr(db, "reload_guild_cache", None)
            if callable(reload_one):
                await reload_one(guild_id)
            else:
                await db.load_cache()
            self._last_seen_revision[guild_id] = revision
            changed_sections = [str(item) for item in sections] if isinstance(sections, list) else []
            if apply_changes and changed_sections:
                await self._apply_section_changes(guild_id, changed_sections)
            log.info(
                "[dashboard_sync] guild=%s revision=%s sections=%s cache recarregado",
                guild_id,
                revision,
                changed_sections,
            )
        except Exception:
            log.exception("[dashboard_sync] falha ao recarregar cache guild=%s revision=%s", guild_id, revision)


    async def _apply_section_changes(self, guild_id: int, sections: list[str]):
        """Aplica efeitos seguros em mensagens persistentes após recarregar o cache."""
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return

        async def run(cog_name: str, method_name: str, *args: Any):
            cog = self.bot.get_cog(cog_name)
            method = getattr(cog, method_name, None) if cog is not None else None
            if not callable(method):
                return
            try:
                await method(*args)
            except Exception:
                log.exception(
                    "[dashboard_sync] falha ao aplicar seção guild=%s cog=%s método=%s",
                    guild_id,
                    cog_name,
                    method_name,
                )

        changed = set(sections)
        if "birthday" in changed:
            await run("BirthdayCog", "_sync_public_calendar", guild)
        if "forms" in changed:
            await run("FormsCog", "_refresh_published_form", int(guild_id))
        if "tickets" in changed:
            await run("TicketsCog", "_refresh_public_panel", int(guild_id))
        if "color_roles" in changed:
            await run("ColorRolesCog", "_refresh_public_panel_messages", int(guild_id))
        if "tts" in changed:
            await run("TTSVoice", "_apply_dashboard_enabled_state", guild)

    @dashboard_sync_loop.before_loop
    async def before_dashboard_sync_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardSync(bot))
