from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

from ..rank_renderer import (
    RankRenderRow,
    assign_competition_positions,
    format_number,
    format_weekly_delta,
    prepare_avatar_thumbnail,
    render_rank_image,
)


log = logging.getLogger(__name__)

RANK_FILENAME = "rank-fichas.png"
MAX_GUILD_IMAGES = 48
MAX_AVATAR_THUMBNAILS = 160
TOP_IMAGE_ROWS = 10
PRELOAD_CANDIDATES = 12
DEBOUNCE_SECONDS = 0.18
NORMAL_CHIP_EMOJI = "<:emoji_63:1485041721573249135>"
LOSS_CHIP_EMOJI = "<:emoji_65:1485043671077228786>"

TOKEN_URLS = {
    "normal": "https://cdn.discordapp.com/emojis/1485041721573249135.png?size=64&quality=lossless",
    "bonus": "https://cdn.discordapp.com/emojis/1487076933819830443.png?size=64&quality=lossless",
    "debt": "https://cdn.discordapp.com/emojis/1485043671077228786.png?size=64&quality=lossless",
}


def format_weekly_chip_summary(value: int) -> str:
    """Descreve a variação semanal com a ficha correspondente."""
    amount = int(value)
    if amount == 0:
        return ""
    if amount > 0:
        emoji = NORMAL_CHIP_EMOJI
        movement = "ganhas"
    else:
        emoji = LOSS_CHIP_EMOJI
        movement = "perdidas"
    return f"**{format_weekly_delta(amount)}** {emoji} {movement} nessa semana"


@dataclass(frozen=True, slots=True)
class ChipRankRow:
    position: int
    user_id: int
    display_name: str
    chips: int
    bonus_chips: int
    weekly_delta: int
    avatar_key: str
    member: discord.Member


@dataclass(frozen=True, slots=True)
class ChipRankResponse:
    image_bytes: bytes
    top_rows: tuple[ChipRankRow, ...]
    requester_line: str


@dataclass(frozen=True, slots=True)
class _GuildRankEntry:
    image_bytes: bytes
    top_rows: tuple[ChipRankRow, ...]
    positions: dict[int, int]
    week_key: str
    data_signature: tuple[object, ...]
    asset_signature: tuple[object, ...]
    generated_at: float


class ChipRankCache:
    """Cache compartilhado do Top 10, com render e avatares fora do comando."""

    def __init__(self, bot: discord.Client, db: Any):
        self.bot = bot
        self.db = db
        self._entries: OrderedDict[int, _GuildRankEntry] = OrderedDict()
        self._avatar_cache: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self._token_icons: dict[str, bytes] = {}
        self._token_generation = 0
        self._dirty: set[int] = set()
        self._asset_warm_needed: set[int] = set()
        self._revisions: dict[int, int] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._refresh_tasks: dict[int, asyncio.Task] = {}
        self._render_semaphore = asyncio.Semaphore(1)
        self._avatar_prepare_semaphore = asyncio.Semaphore(2)
        self._refresh_semaphore = asyncio.Semaphore(3)
        self._week_task: asyncio.Task | None = None
        self._token_task: asyncio.Task | None = None
        self._started = False
        self._closed = False

        register = getattr(self.db, "add_chip_change_listener", None)
        if callable(register):
            register(self._on_chip_change)

    def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._token_task = asyncio.create_task(self._warm_token_icons(), name="games-rank-token-icons")
        self._week_task = asyncio.create_task(self._watch_week_rollover(), name="games-rank-week-rollover")
        for index, guild in enumerate(list(getattr(self.bot, "guilds", ()) or ())):
            self.invalidate(int(guild.id), delay=min(1.2, index * 0.08))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        unregister = getattr(self.db, "remove_chip_change_listener", None)
        if callable(unregister):
            unregister(self._on_chip_change)

        tasks = [task for task in self._refresh_tasks.values() if not task.done()]
        if self._week_task is not None and not self._week_task.done():
            tasks.append(self._week_task)
        if self._token_task is not None and not self._token_task.done():
            tasks.append(self._token_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()

    def _on_chip_change(self, guild_id: int, user_id: int) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self.bot, "loop", None)
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self.invalidate, int(guild_id), int(user_id))

    def invalidate(self, guild_id: int, user_id: int | None = None, *, delay: float = DEBOUNCE_SECONDS) -> None:
        if self._closed:
            return
        gid = int(guild_id)
        self._dirty.add(gid)
        self._revisions[gid] = int(self._revisions.get(gid, 0)) + 1
        if self._started:
            self._schedule_refresh(gid, delay=delay)

    def invalidate_member(self, member: discord.Member) -> None:
        self._purge_member_avatars(int(member.id))
        self.invalidate(int(member.guild.id), int(member.id), delay=0.05)

    @staticmethod
    def _profile_avatar_asset(user: object) -> object | None:
        """Retorna apenas o avatar global, sem priorizar o avatar da guild."""
        return getattr(user, "avatar", None) or getattr(user, "default_avatar", None)

    @staticmethod
    def _username_tag(user: object) -> str:
        username = " ".join(str(getattr(user, "name", "") or "usuario").split()) or "usuario"
        return f"@{username.lstrip('@')}"

    def user_changed(self, before: discord.User, after: discord.User) -> None:
        before_avatar = str(getattr(self._profile_avatar_asset(before), "url", "") or "")
        after_avatar = str(getattr(self._profile_avatar_asset(after), "url", "") or "")
        if str(getattr(before, "name", "")) == str(getattr(after, "name", "")) and before_avatar == after_avatar:
            return
        self._purge_member_avatars(int(after.id))
        for guild in list(getattr(self.bot, "guilds", ()) or ()):
            if guild.get_member(int(after.id)) is not None:
                self.invalidate(int(guild.id), int(after.id), delay=0.05)

    def drop_guild(self, guild_id: int) -> None:
        gid = int(guild_id)
        task = self._refresh_tasks.pop(gid, None)
        if task is not None and not task.done():
            task.cancel()
        self._entries.pop(gid, None)
        self._dirty.discard(gid)
        self._asset_warm_needed.discard(gid)
        self._revisions.pop(gid, None)
        lock = self._guild_locks.get(gid)
        if lock is None or not lock.locked():
            self._guild_locks.pop(gid, None)

    def get_cached_position(self, guild_id: int, user_id: int) -> int | None:
        entry = self._entries.get(int(guild_id))
        if entry is None or entry.week_key != self._current_week_key():
            return None
        return entry.positions.get(int(user_id))

    async def get_rank(self, guild: discord.Guild, requester: discord.Member | None) -> ChipRankResponse:
        gid = int(guild.id)
        entry = self._entries.get(gid)
        current_week = self._current_week_key()
        lock = self._guild_locks.setdefault(gid, asyncio.Lock())

        needs_refresh = entry is None or gid in self._dirty or entry.week_key != current_week
        if needs_refresh:
            # Nunca deixe o comando preso atrás de uma leitura de CDN. Se já houver
            # uma imagem enquanto o aquecimento trabalha, ela é enviada de imediato.
            if entry is None or not lock.locked():
                try:
                    entry = await self.refresh_guild(guild, allow_download=False)
                except Exception:
                    log.exception("games-rank: falha no render local da guild %s", gid)
                    if entry is None:
                        raise

        if entry is None:
            entry = await self.refresh_guild(guild, allow_download=False)
        self._entries.move_to_end(gid)
        return self._make_response(entry, requester)

    async def refresh_guild(self, guild: discord.Guild, *, allow_download: bool) -> _GuildRankEntry:
        gid = int(guild.id)
        lock = self._guild_locks.setdefault(gid, asyncio.Lock())
        async with lock:
            async with self._refresh_semaphore:
                revision = int(self._revisions.get(gid, 0))
                week_key = self._current_week_key()
                ranked_rows = self._build_ranked_rows(guild)
                top_rows = tuple(ranked_rows[:TOP_IMAGE_ROWS])
                positions = {row.user_id: row.position for row in ranked_rows}
                data_signature = self._data_signature(week_key, top_rows)

                preload_rows = ranked_rows[:PRELOAD_CANDIDATES]
                avatar_results = await asyncio.gather(
                    *(self._prepared_avatar(row, allow_download=allow_download) for row in preload_rows),
                    return_exceptions=True,
                )
                prepared_by_user: dict[int, bytes] = {}
                asset_markers: list[tuple[int, str]] = []
                missing_real_avatar = False
                top_user_ids = {row.user_id for row in top_rows}
                for row, result in zip(preload_rows, avatar_results):
                    if isinstance(result, Exception):
                        result = None
                    if result is None:
                        missing_real_avatar = True
                        result = await self._fallback_avatar(row)
                        marker = f"fallback:{row.display_name}"
                    else:
                        marker = row.avatar_key
                    if row.user_id in top_user_ids:
                        prepared_by_user[row.user_id] = result
                        asset_markers.append((row.user_id, marker))

                if missing_real_avatar and not allow_download:
                    self._asset_warm_needed.add(gid)
                    if self._started:
                        self._schedule_refresh(gid, delay=0.0)

                asset_signature: tuple[object, ...] = (
                    self._token_generation,
                    *asset_markers,
                )
                previous = self._entries.get(gid)
                if (
                    previous is not None
                    and previous.data_signature == data_signature
                    and previous.asset_signature == asset_signature
                ):
                    entry = _GuildRankEntry(
                        image_bytes=previous.image_bytes,
                        top_rows=top_rows,
                        positions=positions,
                        week_key=week_key,
                        data_signature=data_signature,
                        asset_signature=asset_signature,
                        generated_at=previous.generated_at,
                    )
                else:
                    render_rows = [
                        RankRenderRow(
                            position=row.position,
                            user_id=row.user_id,
                            display_name=row.display_name,
                            chips=row.chips,
                            bonus_chips=row.bonus_chips,
                            weekly_delta=row.weekly_delta,
                            avatar_png=prepared_by_user.get(row.user_id),
                        )
                        for row in top_rows
                    ]
                    async with self._render_semaphore:
                        image_bytes = await asyncio.to_thread(
                            render_rank_image,
                            render_rows,
                            normal_icon_png=self._token_icons.get("normal"),
                            bonus_icon_png=self._token_icons.get("bonus"),
                            debt_icon_png=self._token_icons.get("debt"),
                        )
                    entry = _GuildRankEntry(
                        image_bytes=image_bytes,
                        top_rows=top_rows,
                        positions=positions,
                        week_key=week_key,
                        data_signature=data_signature,
                        asset_signature=asset_signature,
                        generated_at=time.monotonic(),
                    )

                self._store_entry(gid, entry)
                if int(self._revisions.get(gid, 0)) == revision:
                    self._dirty.discard(gid)
                if allow_download:
                    self._asset_warm_needed.discard(gid)
                return entry

    def _build_ranked_rows(self, guild: discord.Guild) -> list[ChipRankRow]:
        snapshot_getter = getattr(self.db, "get_chip_rank_snapshot", None)
        raw_rows = list(snapshot_getter(guild.id) if callable(snapshot_getter) else ())
        candidates: list[dict[str, object]] = []
        for raw in raw_rows:
            try:
                user_id = int(raw.get("user_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            member = guild.get_member(user_id)
            if member is None or bool(getattr(member, "bot", False)):
                continue
            asset = self._profile_avatar_asset(member)
            candidates.append(
                {
                    **raw,
                    "display_name": self._username_tag(member),
                    "avatar_key": str(getattr(asset, "url", "") or ""),
                    "member": member,
                }
            )

        ranked = assign_competition_positions(candidates)
        return [
            ChipRankRow(
                position=int(row["position"]),
                user_id=int(row["user_id"]),
                display_name=str(row["display_name"]),
                chips=int(row["chips"]),
                bonus_chips=int(row["bonus_chips"]),
                weekly_delta=int(row["weekly_delta"]),
                avatar_key=str(row["avatar_key"]),
                member=row["member"],
            )
            for row in ranked
        ]

    async def _prepared_avatar(self, row: ChipRankRow, *, allow_download: bool) -> bytes | None:
        cache_key = (row.user_id, row.avatar_key)
        cached = self._avatar_cache_get(cache_key)
        if cached is not None:
            return cached
        if not allow_download:
            return None

        asset = self._profile_avatar_asset(row.member)
        if asset is None:
            return None
        try:
            try:
                asset = asset.replace(size=128, static_format="png")
            except (AttributeError, TypeError, ValueError):
                pass
            source = await asyncio.wait_for(asset.read(), timeout=4.0)
            if not source or len(source) > 8 * 1024 * 1024:
                return None
            async with self._avatar_prepare_semaphore:
                prepared = await asyncio.to_thread(prepare_avatar_thumbnail, source, row.display_name)
        except (asyncio.TimeoutError, OSError, discord.HTTPException, ValueError):
            return None
        except Exception:
            log.debug("games-rank: avatar indisponível user=%s", row.user_id, exc_info=True)
            return None
        self._avatar_cache_put(cache_key, prepared)
        return prepared

    async def _fallback_avatar(self, row: ChipRankRow) -> bytes:
        cache_key = (row.user_id, f"fallback:{row.display_name}")
        cached = self._avatar_cache_get(cache_key)
        if cached is not None:
            return cached
        async with self._avatar_prepare_semaphore:
            prepared = await asyncio.to_thread(prepare_avatar_thumbnail, None, row.display_name)
        self._avatar_cache_put(cache_key, prepared)
        return prepared

    def _avatar_cache_get(self, key: tuple[int, str]) -> bytes | None:
        value = self._avatar_cache.get(key)
        if value is not None:
            self._avatar_cache.move_to_end(key)
        return value

    def _avatar_cache_put(self, key: tuple[int, str], value: bytes) -> None:
        self._avatar_cache[key] = value
        self._avatar_cache.move_to_end(key)
        while len(self._avatar_cache) > MAX_AVATAR_THUMBNAILS:
            self._avatar_cache.popitem(last=False)

    def _purge_member_avatars(self, user_id: int) -> None:
        uid = int(user_id)
        for key in [key for key in self._avatar_cache if key[0] == uid]:
            self._avatar_cache.pop(key, None)

    def _store_entry(self, guild_id: int, entry: _GuildRankEntry) -> None:
        gid = int(guild_id)
        self._entries[gid] = entry
        self._entries.move_to_end(gid)
        while len(self._entries) > MAX_GUILD_IMAGES:
            evicted_gid, _entry = self._entries.popitem(last=False)
            self._dirty.discard(evicted_gid)
            self._asset_warm_needed.discard(evicted_gid)

    def _data_signature(self, week_key: str, rows: tuple[ChipRankRow, ...]) -> tuple[object, ...]:
        return (
            week_key,
            *(
                (
                    row.position,
                    row.user_id,
                    row.display_name,
                    row.avatar_key,
                    row.chips,
                    row.bonus_chips,
                    row.weekly_delta,
                )
                for row in rows
            ),
        )

    def _make_response(self, entry: _GuildRankEntry, requester: discord.Member | None) -> ChipRankResponse:
        if requester is None:
            requester_line = ""
        else:
            user_id = int(requester.id)
            position = entry.positions.get(user_id)
            chips = self._safe_int(self.db.get_user_chips(requester.guild.id, user_id, default=100), 100)
            weekly_getter = getattr(self.db, "get_user_chip_week_delta", None)
            weekly = self._safe_int(weekly_getter(requester.guild.id, user_id) if callable(weekly_getter) else 0, 0)
            if position is None:
                requester_line = f"-# Você ainda não entrou no rank • **{format_number(chips)} fichas**"
            else:
                requester_line = f"-# Você: **#{position}** • **{format_number(chips)} fichas**"
            weekly_summary = format_weekly_chip_summary(weekly)
            if weekly_summary:
                requester_line += f" • {weekly_summary}"

        return ChipRankResponse(
            image_bytes=entry.image_bytes,
            top_rows=entry.top_rows,
            requester_line=requester_line,
        )

    def _schedule_refresh(self, guild_id: int, *, delay: float) -> None:
        if self._closed:
            return
        gid = int(guild_id)
        existing = self._refresh_tasks.get(gid)
        if existing is not None and not existing.done():
            return
        try:
            task = asyncio.create_task(self._debounced_refresh(gid, max(0.0, float(delay))), name=f"games-rank-refresh-{gid}")
        except RuntimeError:
            return
        self._refresh_tasks[gid] = task
        task.add_done_callback(lambda completed, guild_id=gid: self._finish_refresh_task(guild_id, completed))

    async def _debounced_refresh(self, guild_id: int, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        while not self._closed and (guild_id in self._dirty or guild_id in self._asset_warm_needed):
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                self._dirty.discard(guild_id)
                self._asset_warm_needed.discard(guild_id)
                return
            try:
                # Primeiro publica um snapshot local correto. Só a passagem
                # seguinte acessa a CDN para trocar fallbacks por avatares reais.
                await self.refresh_guild(guild, allow_download=(guild_id not in self._dirty))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("games-rank: falha ao atualizar cache da guild %s", guild_id)
                self._dirty.discard(guild_id)
                self._asset_warm_needed.discard(guild_id)
                return
            if guild_id in self._dirty:
                await asyncio.sleep(0.05)

    def _finish_refresh_task(self, guild_id: int, task: asyncio.Task) -> None:
        if self._refresh_tasks.get(guild_id) is task:
            self._refresh_tasks.pop(guild_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("games-rank: tarefa de cache encerrou com erro guild=%s", guild_id)
        if not self._closed and (guild_id in self._dirty or guild_id in self._asset_warm_needed):
            self._schedule_refresh(guild_id, delay=0.05)

    async def _warm_token_icons(self) -> None:
        async def fetch(name: str, url: str) -> tuple[str, bytes | None]:
            try:
                payload = await asyncio.to_thread(self._download_small_file, url)
                return name, payload
            except Exception:
                log.debug("games-rank: emoji %s indisponível; usando desenho local", name, exc_info=True)
                return name, None

        results = await asyncio.gather(*(fetch(name, url) for name, url in TOKEN_URLS.items()))
        changed = False
        for name, payload in results:
            if payload:
                self._token_icons[name] = payload
                changed = True
        if changed:
            self._token_generation += 1
            for guild in list(getattr(self.bot, "guilds", ()) or ()):
                self.invalidate(int(guild.id), delay=0.0)

    @staticmethod
    def _download_small_file(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "GamesRankCache/1.0"})
        with urllib.request.urlopen(request, timeout=4.0) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if not payload or len(payload) > 2 * 1024 * 1024:
            raise ValueError("arquivo de emoji vazio ou grande demais")
        return payload

    async def _watch_week_rollover(self) -> None:
        last_key = self._current_week_key()
        while not self._closed:
            await asyncio.sleep(self._seconds_until_next_week())
            current_key = self._current_week_key()
            if current_key == last_key:
                continue
            last_key = current_key
            guild_ids = {int(guild.id) for guild in list(getattr(self.bot, "guilds", ()) or ())}
            guild_ids.update(self._entries)
            for guild_id in guild_ids:
                self.invalidate(guild_id, delay=0.0)

    def _current_week_key(self) -> str:
        getter = getattr(self.db, "_current_week_key", None)
        if callable(getter):
            return str(getter())
        now = self._sao_paulo_now()
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _seconds_until_next_week(self) -> float:
        now = self._sao_paulo_now()
        days_ahead = 7 - now.weekday()
        next_monday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=1, microsecond=0)
        return max(1.0, (next_monday - now).total_seconds())

    @staticmethod
    def _sao_paulo_now() -> datetime:
        try:
            return datetime.now(ZoneInfo("America/Sao_Paulo"))
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)
