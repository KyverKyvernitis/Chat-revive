"""Webhooks gerenciados com verificação de propriedade e envio serializado."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
import discord

from . import constants as C
from .lru_cache import LRUCacheTTL

log = logging.getLogger(__name__)
_MANAGED_WEBHOOK_NAME = "Chatbot"


@dataclass(frozen=True)
class _WebhookRef:
    webhook_id: int
    webhook_token: str


class WebhookManager:
    def __init__(
        self, *, bot: discord.Client, session: aiohttp.ClientSession, coll=None
    ) -> None:
        self._bot = bot
        self._session = session
        self._coll = coll
        self._cache: LRUCacheTTL[int, _WebhookRef] = LRUCacheTTL(
            max_entries=C.WEBHOOK_CACHE_MAX_ENTRIES,
            ttl_seconds=C.WEBHOOK_CACHE_TTL_SECONDS,
        )
        self._channel_locks: "OrderedDict[int, asyncio.Lock]" = OrderedDict()
        self._known_ids: "OrderedDict[int, float]" = OrderedDict()

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        cid = int(channel_id)
        lock = self._channel_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[cid] = lock
        else:
            self._channel_locks.move_to_end(cid)
        if len(self._channel_locks) > C.WEBHOOK_CACHE_MAX_ENTRIES:
            for key, candidate in list(self._channel_locks.items()):
                if key != cid and not candidate.locked():
                    self._channel_locks.pop(key, None)
                    break
        return lock

    def _remember_id(self, webhook_id: int) -> None:
        wid = int(webhook_id)
        self._known_ids[wid] = time.monotonic()
        self._known_ids.move_to_end(wid)
        while len(self._known_ids) > C.WEBHOOK_CACHE_MAX_ENTRIES * 2:
            self._known_ids.popitem(last=False)

    @staticmethod
    def _webhook_host_channel(channel):
        return channel.parent if isinstance(channel, discord.Thread) else channel

    def _owned_by_bot(self, webhook: discord.Webhook) -> bool:
        bot_user = self._bot.user
        owner = getattr(webhook, "user", None)
        return bool(bot_user and owner and int(owner.id) == int(bot_user.id))

    async def _persist_ref(self, host, webhook_id: int) -> None:
        if self._coll is None:
            return
        try:
            await self._coll.update_one(
                {"type": C.DOC_TYPE_WEBHOOK, "channel_id": int(host.id)},
                {"$set": {
                    "type": C.DOC_TYPE_WEBHOOK,
                    "schema_version": C.CHATBOT_SCHEMA_VERSION,
                    "guild_id": int(getattr(host.guild, "id", 0) or 0),
                    "channel_id": int(host.id),
                    "webhook_id": int(webhook_id),
                    "updated_at": time.time(),
                }},
                upsert=True,
            )
        except Exception:
            log.exception("chatbot: falha ao persistir referência de webhook")

    async def _resolve_webhook_unlocked(self, channel) -> Optional[discord.Webhook]:
        host = self._webhook_host_channel(channel)
        if host is None or not hasattr(host, "create_webhook"):
            return None
        cached = self._cache.get(int(host.id))
        if cached is not None:
            self._remember_id(cached.webhook_id)
            return discord.Webhook.partial(
                id=cached.webhook_id, token=cached.webhook_token,
                session=self._session,
            )
        me = host.guild.me if getattr(host, "guild", None) else None
        if me is None or not host.permissions_for(me).manage_webhooks:
            log.warning("chatbot: sem Manage Webhooks | channel=%s", host.id)
            return None

        persisted_id = 0
        if self._coll is not None:
            try:
                persisted = await self._coll.find_one({
                    "type": C.DOC_TYPE_WEBHOOK, "channel_id": int(host.id),
                })
                persisted_id = int((persisted or {}).get("webhook_id") or 0)
            except Exception:
                log.exception("chatbot: falha ao ler referência de webhook")
        try:
            existing = await host.webhooks()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("chatbot: falha ao listar webhooks | channel=%s err=%s", host.id, exc)
            return None

        candidates = [
            webhook for webhook in existing
            if webhook.token is not None
            and self._owned_by_bot(webhook)
            and str(webhook.name or "").strip().casefold() == _MANAGED_WEBHOOK_NAME.casefold()
        ]
        managed = next((w for w in candidates if int(w.id) == persisted_id), None)
        if managed is None:
            managed = candidates[0] if candidates else None
        if managed is None:
            try:
                managed = await host.create_webhook(
                    name=_MANAGED_WEBHOOK_NAME, reason="Chatbot profile bridge",
                )
            except discord.HTTPException as exc:
                log.warning("chatbot: falha ao criar webhook | channel=%s err=%s", host.id, exc)
                return None
        if managed.token is None or not self._owned_by_bot(managed):
            log.warning("chatbot: webhook sem token/propriedade válida | channel=%s", host.id)
            return None
        ref = _WebhookRef(int(managed.id), str(managed.token))
        self._cache.set(int(host.id), ref)
        self._remember_id(ref.webhook_id)
        await self._persist_ref(host, ref.webhook_id)
        return discord.Webhook.partial(
            id=ref.webhook_id, token=ref.webhook_token, session=self._session,
        )

    @staticmethod
    def _rewind_files(files: Optional[list[discord.File]]) -> None:
        for file in files or []:
            try:
                file.fp.seek(0)
            except Exception:
                pass

    @staticmethod
    def _safe_avatar_url(value: str) -> object:
        raw = (value or "").strip()
        if not raw:
            return discord.utils.MISSING
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return discord.utils.MISSING
        return raw[:C.MAX_AVATAR_URL_LENGTH]

    async def send_as_profile(
        self, *, channel, profile_name: str, avatar_url: str,
        content: Optional[str], files: Optional[list[discord.File]] = None,
    ) -> Optional[discord.WebhookMessage]:
        host = self._webhook_host_channel(channel)
        if host is None:
            return None
        async with self._lock_for(int(host.id)):
            webhook = await self._resolve_webhook_unlocked(channel)
            if webhook is None:
                return None
            safe_name = (profile_name or "Chatbot").strip()
            safe_name = re_sub_discord(safe_name)[:80] or "Chatbot"
            safe_content = (content or "").strip()[:2000]
            if not safe_content and not files:
                safe_content = "..."
            send_kwargs: dict = {
                "username": safe_name,
                "avatar_url": self._safe_avatar_url(avatar_url),
                "wait": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if safe_content:
                send_kwargs["content"] = safe_content
            if isinstance(channel, discord.Thread):
                send_kwargs["thread"] = channel
            if files:
                send_kwargs["files"] = files[:10]

            for attempt in range(2):
                try:
                    sent = await webhook.send(**send_kwargs)
                    self._remember_id(int(webhook.id))
                    return sent
                except ValueError:
                    # URL de avatar rejeitada pelo discord.py; o conteúdo ainda
                    # deve ser entregue com o avatar padrão.
                    if send_kwargs.get("avatar_url") is discord.utils.MISSING:
                        return None
                    send_kwargs["avatar_url"] = discord.utils.MISSING
                    self._rewind_files(files)
                except (discord.NotFound, discord.HTTPException) as exc:
                    status = int(getattr(exc, "status", 404 if isinstance(exc, discord.NotFound) else 0) or 0)
                    if attempt or status not in (401, 403, 404):
                        log.warning("chatbot: send webhook falhou | channel=%s err=%s", channel.id, exc)
                        return None
                    self._cache.pop(int(host.id))
                    self._rewind_files(files)
                    webhook = await self._resolve_webhook_unlocked(channel)
                    if webhook is None:
                        return None
            return None

    def is_managed_webhook_id(self, webhook_id: Optional[int]) -> bool:
        return webhook_id is not None and int(webhook_id) in self._known_ids

    async def owns_webhook_id(self, channel, webhook_id: Optional[int]) -> bool:
        """Confirma propriedade sem confiar no nome exibido pelo webhook.

        O caminho persistido recupera replies logo após um restart. Se o banco
        estiver antigo, a listagem da API valida o autor e faz o backfill.
        """
        if webhook_id is None:
            return False
        wid = int(webhook_id)
        if self.is_managed_webhook_id(wid):
            return True
        host = self._webhook_host_channel(channel)
        if host is None:
            return False
        if self._coll is not None:
            try:
                persisted = await self._coll.find_one({
                    "type": C.DOC_TYPE_WEBHOOK,
                    "channel_id": int(host.id),
                    "webhook_id": wid,
                })
                if persisted:
                    self._remember_id(wid)
                    return True
            except Exception:
                log.exception("chatbot: falha ao validar webhook persistido")
        me = getattr(getattr(host, "guild", None), "me", None)
        if me is None or not host.permissions_for(me).manage_webhooks:
            return False
        try:
            webhooks = await host.webhooks()
        except (discord.Forbidden, discord.HTTPException):
            return False
        candidate = next((
            webhook for webhook in webhooks
            if int(webhook.id) == wid
            and self._owned_by_bot(webhook)
            and str(webhook.name or "").strip().casefold() == _MANAGED_WEBHOOK_NAME.casefold()
        ), None)
        if candidate is None:
            return False
        self._remember_id(wid)
        await self._persist_ref(host, wid)
        return True

    def remember_webhook_id(self, webhook_id: int) -> bool:
        wid = int(webhook_id)
        new = wid not in self._known_ids
        self._remember_id(wid)
        return new

    def invalidate_channel(self, channel_id: int) -> None:
        self._cache.pop(int(channel_id))


def re_sub_discord(value: str) -> str:
    """Discord rejeita usernames que contenham a marca, sem diferenciar caixa."""
    import re
    return re.sub("discord", "disc0rd", value, flags=re.IGNORECASE)
