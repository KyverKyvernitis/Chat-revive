import asyncio
import inspect
import os
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from .tts.utils.app_commands import fetch_root_command_ids_cached

from utility.commands.help import HelpCommandMixin
from utility.help_center import HELP_TIMEOUT_SECONDS, HelpCenterView, help_autocomplete_choices
from utility.commands.ping import PingCommandMixin
from utility.commands.vps import VpsCommandMixin
from utility.commands.workers import WorkersCommandMixin


class Utility(HelpCommandMixin, PingCommandMixin, VpsCommandMixin, WorkersCommandMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._app_command_id_cache: dict[object, tuple[float, dict[str, int]]] = {}
        self._help_prefix_cache: dict[int, tuple[float, dict[str, str]]] = {}
        self._help_permission_cache: dict[tuple[int, int], tuple[float, frozenset[str]]] = {}
        self._help_music_available_cache: tuple[float, bool] | None = None
        self._core_worker_auto_wake_task: asyncio.Task | None = None
        self._core_worker_wake_lock = asyncio.Lock()
        self._start_core_worker_auto_wake_task()

    def cog_unload(self):
        self._stop_core_worker_auto_wake_task()

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value: Any):
        if inspect.isawaitable(value):
            return await value
        return value


    async def _delete_message_safe(self, message: discord.Message | None) -> bool:
        if message is None:
            return True
        try:
            await message.delete()
            return True
        except Exception:
            return False

    async def _get_prefix_data(self, guild: discord.Guild | None) -> dict[str, str]:
        defaults = {
            "bot_prefix": str(getattr(config, "BOT_PREFIX", getattr(config, "PREFIX", "_")) or "_"),
            "atts_prefix": str(getattr(config, "TTS_ATTS_PREFIX", "%") or "%"),
            "teto_prefix": str(getattr(config, "TTS_TETO_PREFIX", "'") or "'"),
            "gtts_prefix": ".",
            "edge_prefix": ",",
        }
        if guild is None:
            return defaults

        db = self._get_db()
        if db is None or not hasattr(db, "get_guild_tts_defaults"):
            return defaults

        try:
            guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild.id))
        except Exception:
            guild_defaults = {}

        guild_defaults = guild_defaults or {}
        defaults["bot_prefix"] = str(guild_defaults.get("bot_prefix", defaults["bot_prefix"]) or defaults["bot_prefix"])
        defaults["atts_prefix"] = str(guild_defaults.get("atts_prefix", defaults["atts_prefix"]) or defaults["atts_prefix"])
        defaults["teto_prefix"] = str(guild_defaults.get("teto_prefix", defaults["teto_prefix"]) or defaults["teto_prefix"])
        defaults["gtts_prefix"] = str(guild_defaults.get("gtts_prefix", guild_defaults.get("tts_prefix", defaults["gtts_prefix"])) or defaults["gtts_prefix"])
        defaults["edge_prefix"] = str(guild_defaults.get("edge_prefix", defaults["edge_prefix"]) or defaults["edge_prefix"])
        return defaults

    async def _get_help_prefix_data(self, guild: discord.Guild | None) -> dict[str, str]:
        if guild is None:
            return await self._get_prefix_data(None)

        guild_id = int(guild.id)
        now = asyncio.get_running_loop().time()
        cached = self._help_prefix_cache.get(guild_id)
        if cached is not None and now - cached[0] <= 5.0:
            return dict(cached[1])

        data = await self._get_prefix_data(guild)
        self._help_prefix_cache[guild_id] = (now, dict(data))
        return data

    async def _fetch_root_command_ids_cached(self, guild: discord.Guild | None) -> dict[str, int]:
        return await fetch_root_command_ids_cached(
            self.bot,
            self._app_command_id_cache,
            guild,
            ttl_seconds=600.0,
            include_global_fallback=True,
        )

    async def _get_help_extra_permissions(
        self, guild: discord.Guild | None, user: discord.abc.User
    ) -> set[str]:
        permissions: set[str] = set()
        if guild is None or not isinstance(user, discord.Member):
            return permissions

        cache_key = (int(guild.id), int(user.id))
        now = asyncio.get_running_loop().time()
        cached = self._help_permission_cache.get(cache_key)
        if cached is not None and now - cached[0] <= 5.0:
            return set(cached[1])

        games_cog = self.bot.get_cog("GamesCog")
        if games_cog is not None:
            try:
                staff_role = games_cog._get_staff_role(guild)
                if staff_role is not None and staff_role in getattr(user, "roles", []):
                    permissions.add("economy_staff")
            except Exception:
                pass

            if "economy_staff" not in permissions:
                owner_id = getattr(self.bot, "owner_id", None)
                owner_ids = getattr(self.bot, "owner_ids", None) or ()
                if owner_id is not None and int(user.id) == int(owner_id):
                    permissions.add("economy_staff")
                elif int(user.id) in {int(value) for value in owner_ids}:
                    permissions.add("economy_staff")
                elif owner_id is None and not owner_ids:
                    try:
                        if await self.bot.is_owner(user):
                            permissions.add("economy_staff")
                    except Exception:
                        pass

        tickets_cog = self.bot.get_cog("TicketsCog")
        if tickets_cog is not None:
            try:
                if tickets_cog._is_staff(user, guild.id):
                    permissions.add("ticket_staff")
            except Exception:
                pass

        self._help_permission_cache[cache_key] = (now, frozenset(permissions))
        return permissions

    async def _help_music_available(self) -> bool:
        if self.bot.get_cog("Music") is None:
            return False

        now = asyncio.get_running_loop().time()
        cached = self._help_music_available_cache
        if cached is not None and now - cached[0] <= 5.0:
            return bool(cached[1])

        def read_registry() -> bool:
            try:
                from utility.commands.workers_registry import get_core_workers_registry

                snapshot = get_core_workers_registry().snapshot(lock_timeout_seconds=0.03)
                workers = snapshot.get("workers") if isinstance(snapshot, dict) else []
                for worker in workers or []:
                    if not isinstance(worker, dict) or not bool(worker.get("online")):
                        continue
                    runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
                    source = str(worker.get("source") or "").strip().lower()
                    if runtime_kind == "apk" or source.startswith("core-worker-apk"):
                        continue
                    roles = {str(value or "").strip().lower() for value in (worker.get("roles") or [])}
                    capabilities = {str(value or "").strip().lower() for value in (worker.get("capabilities") or [])}
                    roles_caps = roles | capabilities
                    if "phone-worker" in roles_caps and "music" in roles_caps:
                        return True
                return False
            except Exception:
                return False

        available = bool(await asyncio.to_thread(read_registry))
        self._help_music_available_cache = (now, available)
        return available

    def _get_help_games_context(self, guild: discord.Guild | None) -> dict[str, str]:
        if guild is None:
            return {}
        games_cog = self.bot.get_cog("GamesCog")
        if games_cog is None:
            return {}

        context: dict[str, str] = {}
        try:
            context["mode"] = str(games_cog._gincana_input_mode(guild.id) or "triggers")
        except Exception:
            context["mode"] = "triggers"
        try:
            channel = games_cog._get_gincana_channel(guild)
            mention = str(getattr(channel, "mention", "") or "").strip()
            if mention:
                context["channel_mention"] = mention
        except Exception:
            pass
        return context

    async def _help_hidden_categories(self) -> frozenset[str]:
        hidden: set[str] = set()
        if not await self._help_music_available():
            hidden.add("music")
        return frozenset(hidden)

    async def _help_autocomplete_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        prefixes = await self._get_help_prefix_data(interaction.guild)
        extra_permissions = await self._get_help_extra_permissions(interaction.guild, interaction.user)
        hidden_categories = await self._help_hidden_categories()
        games_context = self._get_help_games_context(interaction.guild)
        return help_autocomplete_choices(
            current,
            user=interaction.user,
            prefixes=prefixes,
            games_context=games_context,
            extra_permissions=extra_permissions,
            hidden_categories=hidden_categories,
        )

    async def _send_help_response(
        self,
        *,
        guild: discord.Guild | None,
        owner: discord.abc.User,
        responder: discord.abc.Messageable,
        interaction: discord.Interaction | None = None,
        ephemeral: bool = False,
        prefix_command_message: discord.Message | None = None,
        subject: str | None = None,
    ):
        prefixes = await self._get_help_prefix_data(guild)
        root_ids = await self._fetch_root_command_ids_cached(guild)
        extra_permissions = await self._get_help_extra_permissions(guild, owner)
        hidden_categories = await self._help_hidden_categories()
        games_context = self._get_help_games_context(guild)
        view = HelpCenterView(
            owner=owner,
            prefixes=prefixes,
            root_ids=root_ids,
            games_context=games_context,
            extra_permissions=extra_permissions,
            hidden_categories=hidden_categories,
            hidden_categories_provider=self._help_hidden_categories,
            subject=subject,
            timeout=HELP_TIMEOUT_SECONDS,
        )

        if interaction is not None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=view,
                    ephemeral=ephemeral,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    view.message = await interaction.original_response()
                except Exception:
                    pass
            else:
                view.message = await interaction.followup.send(
                    view=view,
                    ephemeral=ephemeral,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        view.message = await responder.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._delete_message_safe(prefix_command_message)

    def _format_bool_badge(self, value: bool, *, ok_label: str = "OK", bad_label: str = "Falha") -> str:
        return f"🟢 {ok_label}" if bool(value) else f"🔴 {bad_label}"

    def _format_duration(self, total_seconds: float | int | None) -> str:
        try:
            total = int(float(total_seconds or 0))
        except Exception:
            total = 0
        days, rem = divmod(max(0, total), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    def _format_ms(self, value: Any) -> str:
        try:
            return f"{float(value):.2f} ms"
        except Exception:
            return "n/a"

    def _format_bytes_human(self, value: int | float | None) -> str:
        try:
            size = float(value or 0)
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

    def _collect_health_snapshot(self) -> dict[str, Any]:
        """Coleta métricas do bot/TTS reaproveitadas pelo painel `/vps`.

        O nome fica preservado porque o webserver/healthcheck interno ainda usa
        `get_health_snapshot`; aqui não registra nem expõe o antigo comando /health.
        """
        snapshot: dict[str, Any] = {}
        get_snapshot = getattr(self.bot, "get_health_snapshot", None)
        if callable(get_snapshot):
            try:
                snapshot = dict(get_snapshot() or {})
            except Exception:
                snapshot = {}

        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        engine_metrics = dict(tts_metrics.get("engines") or {})

        tmp_root = os.path.join(os.getcwd(), "tmp_audio")
        runtime_dir = os.path.join(tmp_root, "runtime")
        cache_dir = os.path.join(tmp_root, "cache")
        credentials_dir = os.path.join(tmp_root, "credentials")

        def _dir_stats(path: str) -> tuple[int, int]:
            total_bytes = 0
            total_files = 0
            try:
                for entry in os.scandir(path):
                    if not entry.is_file():
                        continue
                    total_files += 1
                    try:
                        total_bytes += int(entry.stat().st_size)
                    except Exception:
                        pass
            except Exception:
                return 0, 0
            return total_files, total_bytes

        runtime_files, runtime_bytes = _dir_stats(runtime_dir)
        cache_files, cache_bytes = _dir_stats(cache_dir)
        cred_files, cred_bytes = _dir_stats(credentials_dir)
        total_tmp_bytes = runtime_bytes + cache_bytes + cred_bytes

        def _guild_sort_key(guild: discord.Guild) -> tuple[int, str]:
            try:
                members = int(getattr(guild, "member_count", 0) or len(getattr(guild, "members", []) or []) or 0)
            except Exception:
                members = 0
            name = str(getattr(guild, "name", "") or "").casefold()
            return (-members, name)

        guilds = sorted(list(getattr(self.bot, "guilds", []) or []), key=_guild_sort_key)
        total_members = 0
        for guild in guilds:
            try:
                count = getattr(guild, "member_count", None)
                if count is None:
                    count = len(getattr(guild, "members", []) or [])
                total_members += int(count or 0)
            except Exception:
                pass

        snapshot.update({
            "tts_metrics": tts_metrics,
            "engine_metrics": engine_metrics,
            "runtime_files": runtime_files,
            "cache_files": cache_files,
            "cred_files": cred_files,
            "total_tmp_bytes": total_tmp_bytes,
            "guilds": guilds,
            "guild_count": len(guilds),
            "total_members": total_members,
        })
        return snapshot





async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
