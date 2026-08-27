from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import discord

from ..chip_profile_renderer import (
    ChipProfileData,
    PreparedProfileAssets,
    build_profile_accessible_description,
    prepare_profile_assets,
    render_chip_profile,
)


log = logging.getLogger(__name__)

PROFILE_FILENAME = "perfil-fichas.png"
MAX_PROFILE_IMAGES = 48
MAX_IDENTITY_ASSETS = 48
IDENTITY_TTL_SECONDS = 6 * 60 * 60
NO_BANNER_TTL_SECONDS = 30 * 60
ASSET_FAILURE_TTL_SECONDS = 5 * 60
ASSET_TIMEOUT_SECONDS = 4.0
PROFILE_FETCH_TIMEOUT_SECONDS = 3.5
MAX_ASSET_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ChipProfileResponse:
    image_bytes: bytes
    accessible_description: str


@dataclass(frozen=True, slots=True)
class _IdentityEntry:
    avatar_key: str
    name_key: str
    banner_key: str
    assets: PreparedProfileAssets
    generation: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _ImageEntry:
    data_signature: ChipProfileData
    identity_generation: int
    token_signature: tuple[object, ...]
    response: ChipProfileResponse


class ChipProfileCache:
    """Cache pequeno do perfil; rede e Pillow nunca bloqueiam o event loop."""

    def __init__(self, bot: discord.Client, rank_cache: object):
        self.bot = bot
        self.rank_cache = rank_cache
        self._identities: OrderedDict[int, _IdentityEntry] = OrderedDict()
        self._images: OrderedDict[tuple[int, int], _ImageEntry] = OrderedDict()
        self._identity_locks: dict[int, asyncio.Lock] = {}
        self._image_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._prepare_semaphore = asyncio.Semaphore(2)
        self._render_semaphore = asyncio.Semaphore(1)
        self._generation = 0
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        self._identities.clear()
        self._images.clear()
        self._identity_locks.clear()
        self._image_locks.clear()

    @staticmethod
    def _profile_avatar_asset(user: object) -> object | None:
        return getattr(user, "avatar", None) or getattr(user, "default_avatar", None)

    @staticmethod
    def _asset_key(asset: object | None) -> str:
        return str(getattr(asset, "url", "") or "")

    @staticmethod
    def _name_key(user: object) -> str:
        global_name = str(getattr(user, "global_name", "") or "").strip()
        username = str(getattr(user, "name", "") or "Usuário").strip() or "Usuário"
        return global_name or username

    async def get_profile(
        self,
        member: discord.Member,
        data: ChipProfileData,
    ) -> ChipProfileResponse:
        if self._closed:
            raise RuntimeError("cache de perfil encerrado")

        guild_id = int(member.guild.id)
        user_id = int(member.id)
        key = (guild_id, user_id)
        identity = await self._get_identity(member)
        token_icons = self._token_icons()
        token_signature = self._token_signature(token_icons)

        cached = self._images.get(key)
        if self._image_matches(cached, data, identity.generation, token_signature):
            self._images.move_to_end(key)
            return cached.response

        lock = self._image_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._images.get(key)
            if self._image_matches(cached, data, identity.generation, token_signature):
                self._images.move_to_end(key)
                return cached.response

            async with self._render_semaphore:
                payload = await asyncio.to_thread(
                    render_chip_profile,
                    data,
                    identity.assets,
                    normal_icon_png=token_icons.get("normal"),
                    bonus_icon_png=token_icons.get("bonus"),
                    debt_icon_png=token_icons.get("debt"),
                )
            response = ChipProfileResponse(
                image_bytes=payload,
                accessible_description=build_profile_accessible_description(data),
            )
            self._images[key] = _ImageEntry(
                data_signature=data,
                identity_generation=identity.generation,
                token_signature=token_signature,
                response=response,
            )
            self._images.move_to_end(key)
            while len(self._images) > MAX_PROFILE_IMAGES:
                evicted_key, _entry = self._images.popitem(last=False)
                evicted_lock = self._image_locks.get(evicted_key)
                if evicted_lock is None or not evicted_lock.locked():
                    self._image_locks.pop(evicted_key, None)
            return response

    @staticmethod
    def _image_matches(
        cached: _ImageEntry | None,
        data: ChipProfileData,
        identity_generation: int,
        token_signature: tuple[object, ...],
    ) -> bool:
        return bool(
            cached is not None
            and cached.data_signature == data
            and cached.identity_generation == int(identity_generation)
            and cached.token_signature == token_signature
        )

    def _token_icons(self) -> dict[str, bytes]:
        getter = getattr(self.rank_cache, "get_cached_token_icons", None)
        if not callable(getter):
            return {}
        try:
            return {
                str(name): bytes(payload)
                for name, payload in dict(getter()).items()
                if payload
            }
        except Exception:
            return {}

    @staticmethod
    def _token_signature(icons: dict[str, bytes]) -> tuple[object, ...]:
        return tuple(
            (name, len(payload), payload[:16])
            for name, payload in sorted(icons.items())
        )

    async def _get_identity(self, member: discord.Member) -> _IdentityEntry:
        user_id = int(member.id)
        avatar_key = self._asset_key(self._profile_avatar_asset(member))
        name_key = self._name_key(member)
        now = time.monotonic()
        cached = self._identities.get(user_id)
        if (
            cached is not None
            and cached.avatar_key == avatar_key
            and cached.name_key == name_key
            and cached.expires_at > now
        ):
            self._identities.move_to_end(user_id)
            return cached

        lock = self._identity_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._identities.get(user_id)
            if (
                cached is not None
                and cached.avatar_key == avatar_key
                and cached.name_key == name_key
                and cached.expires_at > now
            ):
                self._identities.move_to_end(user_id)
                return cached

            avatar_asset = self._profile_avatar_asset(member)
            avatar_task = asyncio.create_task(
                self._read_asset(avatar_asset, size=256, static=True)
            )
            profile_task = asyncio.create_task(self._fetch_global_user(user_id))
            avatar_source, fetched_user = await asyncio.gather(avatar_task, profile_task)

            banner_asset = getattr(fetched_user, "banner", None) if fetched_user is not None else None
            banner_key = self._asset_key(banner_asset)
            banner_source = await self._read_asset(banner_asset, size=1024, static=False)

            async with self._prepare_semaphore:
                assets = await asyncio.to_thread(
                    prepare_profile_assets,
                    avatar_source,
                    banner_source,
                    name_key,
                )

            self._generation += 1
            asset_failed = bool(
                fetched_user is None
                or (avatar_asset is not None and avatar_source is None)
                or (banner_asset is not None and banner_source is None)
            )
            if asset_failed:
                ttl = ASSET_FAILURE_TTL_SECONDS
            else:
                ttl = IDENTITY_TTL_SECONDS if banner_source else NO_BANNER_TTL_SECONDS
            entry = _IdentityEntry(
                avatar_key=avatar_key,
                name_key=name_key,
                banner_key=banner_key,
                assets=assets,
                generation=self._generation,
                expires_at=time.monotonic() + ttl,
            )
            self._identities[user_id] = entry
            self._identities.move_to_end(user_id)
            while len(self._identities) > MAX_IDENTITY_ASSETS:
                evicted_user_id, _old = self._identities.popitem(last=False)
                evicted_lock = self._identity_locks.get(evicted_user_id)
                if evicted_lock is None or not evicted_lock.locked():
                    self._identity_locks.pop(evicted_user_id, None)
            return entry

    async def _fetch_global_user(self, user_id: int) -> discord.User | None:
        fetcher = getattr(self.bot, "fetch_user", None)
        if not callable(fetcher):
            return None
        try:
            return await asyncio.wait_for(fetcher(int(user_id)), timeout=PROFILE_FETCH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, discord.HTTPException, OSError):
            return None
        except Exception:
            log.debug("games-profile: perfil global indisponível user=%s", user_id, exc_info=True)
            return None

    async def _read_asset(
        self,
        asset: object | None,
        *,
        size: int,
        static: bool,
    ) -> bytes | None:
        if asset is None or not callable(getattr(asset, "read", None)):
            return None
        prepared_asset = asset
        try:
            replace = getattr(asset, "replace", None)
            if callable(replace):
                if static:
                    prepared_asset = replace(size=int(size), static_format="png")
                else:
                    prepared_asset = replace(size=int(size))
        except (TypeError, ValueError):
            prepared_asset = asset
        try:
            payload = await asyncio.wait_for(
                prepared_asset.read(),
                timeout=ASSET_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, discord.HTTPException, OSError):
            return None
        except Exception:
            return None
        if not payload or len(payload) > MAX_ASSET_BYTES:
            return None
        return bytes(payload)

    def invalidate_member(self, member: discord.Member) -> None:
        self._purge_user(int(member.id))

    def user_changed(self, before: discord.User, after: discord.User) -> None:
        before_avatar = self._asset_key(self._profile_avatar_asset(before))
        after_avatar = self._asset_key(self._profile_avatar_asset(after))
        before_banner = self._asset_key(getattr(before, "banner", None))
        after_banner = self._asset_key(getattr(after, "banner", None))
        if (
            self._name_key(before) == self._name_key(after)
            and before_avatar == after_avatar
            and before_banner == after_banner
        ):
            return
        self._purge_user(int(after.id))

    def drop_guild(self, guild_id: int) -> None:
        gid = int(guild_id)
        for key in [key for key in self._images if key[0] == gid]:
            self._images.pop(key, None)
            lock = self._image_locks.get(key)
            if lock is None or not lock.locked():
                self._image_locks.pop(key, None)

    def _purge_user(self, user_id: int) -> None:
        uid = int(user_id)
        self._identities.pop(uid, None)
        for key in [key for key in self._images if key[1] == uid]:
            self._images.pop(key, None)
            lock = self._image_locks.get(key)
            if lock is None or not lock.locked():
                self._image_locks.pop(key, None)
