from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

from .constants import (
    CANCEL_EMOJI,
    CANCEL_EMOJI_ID,
    COUNTDOWN_SECONDS,
    DELETE_MESSAGE_SECONDS,
    MAX_ENTRIES_PER_BATCH,
    RENDER_INTERVAL_SECONDS,
    STATE_BANNED,
    STATE_BANNING,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_WAITING,
)
from .state import ChallengeEntry
from .views import AntibotPanelView, batch_view, notice_view, warning_view


log = logging.getLogger("bot.antibot")


@dataclass(slots=True)
class TrapSession:
    guild_id: int
    channel_id: int
    entries: dict[int, ChallengeEntry] = field(default_factory=dict)
    message: discord.Message | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    render_task: asyncio.Task | None = None
    closed: bool = False
    last_render_at: float = 0.0


class AntibotCog(commands.Cog):
    """Canal armadilha para contas comprometidas.

    O caminho comum é somente uma consulta a dicionário. Toda operação de
    Discord e todo timer existem apenas depois de uma mensagem no canal ativo.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_channel_to_guild: dict[int, int] = {}
        self._pending_by_member: dict[tuple[int, int], tuple[TrapSession, ChallengeEntry]] = {}
        self._live_session_by_guild: dict[int, TrapSession] = {}
        self._sessions_by_message: dict[int, TrapSession] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._closing = False
        self._cancel_emoji = discord.PartialEmoji.from_str(CANCEL_EMOJI)

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self) -> None:
        self._rebuild_active_cache()
        setattr(self.bot, "antibot_should_block_message", self.should_block_message_fast)

    async def cog_unload(self) -> None:
        self._closing = True
        guard = getattr(self.bot, "antibot_should_block_message", None)
        if getattr(guard, "__self__", None) is self:
            with contextlib.suppress(Exception):
                delattr(self.bot, "antibot_should_block_message")

        sessions: list[TrapSession] = []
        seen: set[int] = set()
        for session in [
            *self._sessions_by_message.values(),
            *self._live_session_by_guild.values(),
        ]:
            marker = id(session)
            if marker in seen:
                continue
            seen.add(marker)
            sessions.append(session)

        tasks: list[asyncio.Task] = []
        for session in sessions:
            session.closed = True
            if session.render_task is not None:
                session.render_task.cancel()
                tasks.append(session.render_task)
            for entry in session.entries.values():
                if entry.deadline_handle is not None:
                    entry.deadline_handle.cancel()
                if entry.ban_task is not None:
                    entry.ban_task.cancel()
                    tasks.append(entry.ban_task)

        current_task = asyncio.current_task()
        await asyncio.gather(
            *(task for task in tasks if task is not current_task),
            return_exceptions=True,
        )

        # Uma recarga nunca pode deixar na tela uma contagem que já não existe.
        # Registros de banimento continuam permanentes; sessões só temporárias
        # são removidas.
        for session in sessions:
            banned = [entry for entry in session.entries.values() if entry.state == STATE_BANNED]
            if banned and session.message is not None:
                with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await session.message.edit(
                        view=batch_view(banned, now=time.monotonic()),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await session.message.clear_reaction(self._cancel_emoji)
            else:
                await self._delete_quietly(session.message)

        self._sessions_by_message.clear()
        self._live_session_by_guild.clear()
        self._pending_by_member.clear()
        self._guild_locks.clear()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    def get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_antibot_config"):
            return {"enabled": False, "channel_id": 0, "warning_message_id": 0, "revision": 0}
        try:
            return dict(db.get_antibot_config(int(guild_id)) or {})
        except Exception:
            log.exception("falha ao ler configuração guild=%s", guild_id)
            return {"enabled": False, "channel_id": 0, "warning_message_id": 0, "revision": 0}

    def _rebuild_active_cache(self) -> None:
        self._active_channel_to_guild.clear()
        db = self.db
        if db is None or not hasattr(db, "iter_antibot_configs"):
            return
        try:
            configs = db.iter_antibot_configs()
        except Exception:
            log.exception("falha ao reconstruir cache de canais")
            return
        for guild_id, config in dict(configs or {}).items():
            channel_id = int((config or {}).get("channel_id") or 0)
            if bool((config or {}).get("enabled")) and channel_id > 0:
                self._active_channel_to_guild[channel_id] = int(guild_id)

    def should_block_message_fast(self, message: Any) -> bool:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if guild is None or author is None or channel is None:
            return False
        guild_id = int(getattr(guild, "id", 0) or 0)
        user_id = int(getattr(author, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        if guild_id <= 0 or user_id <= 0:
            return False
        if (guild_id, user_id) in self._pending_by_member:
            return True
        return self._active_channel_to_guild.get(channel_id) == guild_id

    async def is_staff(self, user: discord.abc.User | None) -> bool:
        if not isinstance(user, discord.Member):
            return False
        if int(user.id) == int(user.guild.owner_id):
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and any(
            bool(getattr(perms, name, False))
            for name in (
                "administrator",
                "manage_guild",
                "manage_messages",
                "moderate_members",
                "kick_members",
                "ban_members",
            )
        ):
            return True

        games = self.bot.get_cog("GamesCog")
        games_check = getattr(games, "_is_staff_member", None)
        if callable(games_check):
            with contextlib.suppress(Exception):
                if bool(games_check(user)):
                    return True

        tickets = self.bot.get_cog("TicketsCog")
        tickets_check = getattr(tickets, "_is_staff", None)
        if callable(tickets_check):
            with contextlib.suppress(Exception):
                if bool(tickets_check(user, user.guild.id)):
                    return True

        with contextlib.suppress(Exception):
            if await self.bot.is_owner(user):
                return True
        return False

    async def _delete_quietly(self, message: discord.Message | None) -> None:
        if message is None:
            return
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.delete()

    async def handle_message_from_bot_on_message(self, message: discord.Message) -> bool:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if guild is None or author is None or channel is None:
            return False

        guild_id = int(getattr(guild, "id", 0) or 0)
        user_id = int(getattr(author, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        pending = self._pending_by_member.get((guild_id, user_id))
        if pending is not None:
            await self._delete_quietly(message)
            session, entry = pending
            await self._finish_ban(session, entry, repeated_message=True)
            return True

        if self._active_channel_to_guild.get(channel_id) != guild_id:
            return False

        bot_user_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        if user_id == bot_user_id:
            return True
        is_system = getattr(message, "is_system", None)
        if callable(is_system) and is_system():
            return True

        await self._delete_quietly(message)
        if await self.is_staff(author):
            log.info("mensagem de staff ignorada guild=%s channel=%s user=%s", guild_id, channel_id, user_id)
            return True

        await self._start_challenge(message)
        return True

    async def _start_challenge(self, trigger: discord.Message) -> None:
        guild = trigger.guild
        guild_id = int(guild.id)
        user_id = int(trigger.author.id)
        channel_id = int(trigger.channel.id)
        async with self._guild_lock(guild_id):
            current = self._pending_by_member.get((guild_id, user_id))
            if current is not None:
                session, entry = current
                await self._finish_ban(session, entry, repeated_message=True)
                return

            session = self._live_session_by_guild.get(guild_id)
            if (
                session is None
                or session.closed
                or session.channel_id != channel_id
                or len(session.entries) >= MAX_ENTRIES_PER_BATCH
            ):
                session = TrapSession(guild_id=guild_id, channel_id=channel_id)
                self._live_session_by_guild[guild_id] = session

            entry = ChallengeEntry(
                user_id=user_id,
                trigger_message_id=int(getattr(trigger, "id", 0) or 0),
            )
            session.entries[user_id] = entry
            self._pending_by_member[(guild_id, user_id)] = (session, entry)

            if session.message is None:
                try:
                    session.message = await trigger.channel.send(
                        view=batch_view(session.entries.values(), now=time.monotonic()),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    # Registra antes da reação do bot: se o usuário reagir no
                    # instante em que a mensagem aparece, o evento já encontra
                    # a sessão e o cancelamento não se perde.
                    self._sessions_by_message[int(session.message.id)] = session
                    await session.message.add_reaction(self._cancel_emoji)
                except Exception:
                    self._pending_by_member.pop((guild_id, user_id), None)
                    session.entries.pop(user_id, None)
                    if session.message is not None:
                        self._sessions_by_message.pop(int(session.message.id), None)
                    await self._delete_quietly(session.message)
                    session.closed = True
                    if self._live_session_by_guild.get(guild_id) is session:
                        self._live_session_by_guild.pop(guild_id, None)
                    log.exception("não foi possível abrir contagem guild=%s user=%s", guild_id, user_id)
                    return

                session.last_render_at = time.monotonic()
                self._arm_entry(session, entry, session.last_render_at)
                session.render_task = asyncio.create_task(
                    self._render_loop(session),
                    name=f"antibot-render:{guild_id}:{session.message.id}",
                )
            else:
                session.wake_event.set()

    def _arm_entry(self, session: TrapSession, entry: ChallengeEntry, now: float) -> None:
        if entry.state != STATE_WAITING or entry.deadline is not None:
            return
        entry.deadline = float(now) + float(COUNTDOWN_SECONDS)
        delay = max(0.0, entry.deadline - time.monotonic())
        entry.deadline_handle = asyncio.get_running_loop().call_later(
            delay,
            self._dispatch_due_ban,
            session,
            entry,
        )

    def _dispatch_due_ban(self, session: TrapSession, entry: ChallengeEntry) -> None:
        entry.deadline_handle = None
        if self._closing or session.closed or entry.state != STATE_WAITING:
            return
        entry.ban_task = asyncio.create_task(
            self._finish_ban(session, entry),
            name=f"antibot-ban:{session.guild_id}:{entry.user_id}",
        )

    async def _render_loop(self, session: TrapSession) -> None:
        try:
            while not session.closed and not self._closing:
                now = time.monotonic()
                wait_for_slot = max(0.0, session.last_render_at + RENDER_INTERVAL_SECONDS - now)
                if wait_for_slot:
                    try:
                        await asyncio.wait_for(session.wake_event.wait(), timeout=wait_for_slot)
                    except asyncio.TimeoutError:
                        pass
                    session.wake_event.clear()
                    remaining = session.last_render_at + RENDER_INTERVAL_SECONDS - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(remaining)

                should_close = await self._render_once(session)
                if should_close:
                    await self._close_session(session)
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("renderizador falhou guild=%s channel=%s", session.guild_id, session.channel_id)
            await self._abort_session(session)

    async def _render_once(self, session: TrapSession) -> bool:
        now = time.monotonic()
        async with session.lock:
            for user_id, entry in list(session.entries.items()):
                if entry.transient_expired(now):
                    session.entries.pop(user_id, None)
            entries = list(session.entries.values())
            if not entries:
                return True
            view = batch_view(entries, now=now)

        if session.message is None:
            return True
        try:
            await session.message.edit(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            await self._abort_session(session)
            return True
        except (discord.Forbidden, discord.HTTPException):
            # Evita retry em loop apertado quando o Discord aplica rate limit
            # ou uma permissão muda no meio da contagem.
            session.last_render_at = time.monotonic()
            log.warning(
                "falha ao editar contagem guild=%s channel=%s message=%s",
                session.guild_id,
                session.channel_id,
                getattr(session.message, "id", 0),
                exc_info=True,
            )
            async with session.lock:
                has_waiting = any(entry.is_waiting for entry in session.entries.values())
                has_transient = any(
                    entry.state in {STATE_CANCELLED, STATE_FAILED}
                    for entry in session.entries.values()
                )
            return not has_waiting and not has_transient

        rendered_at = time.monotonic()
        session.last_render_at = rendered_at
        async with session.lock:
            for entry in session.entries.values():
                self._arm_entry(session, entry, rendered_at)
            has_waiting = any(entry.is_waiting for entry in session.entries.values())
            has_transient = any(
                entry.state in {STATE_CANCELLED, STATE_FAILED}
                for entry in session.entries.values()
            )
        return not has_waiting and not has_transient

    async def _finish_ban(
        self,
        session: TrapSession,
        entry: ChallengeEntry,
        *,
        repeated_message: bool = False,
    ) -> None:
        async with session.lock:
            if entry.state != STATE_WAITING:
                return
            entry.state = STATE_BANNING
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
                entry.deadline_handle = None

        guild = self.bot.get_guild(session.guild_id)
        member = guild.get_member(entry.user_id) if guild is not None else None
        if member is not None and await self.is_staff(member):
            await self._set_terminal(session, entry, STATE_CANCELLED)
            return

        if guild is None:
            await self._set_terminal(session, entry, STATE_FAILED)
            return

        reason_suffix = "nova mensagem durante a contagem" if repeated_message else "sem confirmação em 10 segundos"
        reason = (
            f"Antibot: canal armadilha {session.channel_id}; {reason_suffix}; "
            f"mensagem {entry.trigger_message_id}"
        )[:480]
        try:
            await guild.ban(
                member or discord.Object(id=entry.user_id),
                reason=reason,
                delete_message_seconds=DELETE_MESSAGE_SECONDS,
            )
        except Exception:
            log.exception(
                "banimento falhou guild=%s channel=%s user=%s",
                session.guild_id,
                session.channel_id,
                entry.user_id,
            )
            await self._set_terminal(session, entry, STATE_FAILED)
            return

        log.warning(
            "conta banida guild=%s channel=%s user=%s delete_seconds=%s",
            session.guild_id,
            session.channel_id,
            entry.user_id,
            DELETE_MESSAGE_SECONDS,
        )
        await self._set_terminal(session, entry, STATE_BANNED)

    async def _set_terminal(self, session: TrapSession, entry: ChallengeEntry, state: str) -> None:
        async with session.lock:
            entry.state = state
            entry.terminal_at = time.monotonic()
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
                entry.deadline_handle = None
            task = entry.ban_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
            self._pending_by_member.pop((session.guild_id, entry.user_id), None)
            session.wake_event.set()

    async def _close_session(self, session: TrapSession) -> None:
        if session.closed:
            return
        session.closed = True
        if self._live_session_by_guild.get(session.guild_id) is session:
            self._live_session_by_guild.pop(session.guild_id, None)
        if session.message is not None:
            self._sessions_by_message.pop(int(session.message.id), None)
        permanent = any(entry.state == STATE_BANNED for entry in session.entries.values())
        if permanent:
            if session.message is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                    await session.message.clear_reaction(self._cancel_emoji)
        else:
            await self._delete_quietly(session.message)

    async def _abort_session(self, session: TrapSession) -> None:
        if session.closed:
            return
        session.closed = True
        if self._live_session_by_guild.get(session.guild_id) is session:
            self._live_session_by_guild.pop(session.guild_id, None)
        if session.message is not None:
            self._sessions_by_message.pop(int(session.message.id), None)
        for entry in session.entries.values():
            self._pending_by_member.pop((session.guild_id, entry.user_id), None)
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
                entry.deadline_handle = None
            if entry.ban_task is not None and not entry.ban_task.done():
                entry.ban_task.cancel()
        await self._delete_quietly(session.message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if int(getattr(getattr(payload, "emoji", None), "id", 0) or 0) != CANCEL_EMOJI_ID:
            return
        bot_user_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        user_id = int(getattr(payload, "user_id", 0) or 0)
        if not user_id or user_id == bot_user_id:
            return
        session = self._sessions_by_message.get(int(getattr(payload, "message_id", 0) or 0))
        if session is None or session.closed:
            return

        now = time.monotonic()
        async with session.lock:
            entry = session.entries.get(user_id)
            if entry is None or entry.state != STATE_WAITING:
                return
            if entry.deadline is not None and now >= entry.deadline:
                return
            entry.state = STATE_CANCELLED
            entry.terminal_at = now
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
                entry.deadline_handle = None
            if entry.ban_task is not None and not entry.ban_task.done():
                entry.ban_task.cancel()
            self._pending_by_member.pop((session.guild_id, user_id), None)
            session.wake_event.set()

        user = getattr(payload, "member", None) or discord.Object(id=user_id)
        if session.message is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException, discord.NotFound):
                await session.message.remove_reaction(self._cancel_emoji, user)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        session = self._sessions_by_message.get(int(getattr(payload, "message_id", 0) or 0))
        if session is not None:
            await self._abort_session(session)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        channel_id = int(getattr(channel, "id", 0) or 0)
        guild_id = self._active_channel_to_guild.get(channel_id)
        if guild_id is None:
            return
        guild = getattr(channel, "guild", None)
        await self.deactivate_trap(guild, actor_id=0, delete_warning=False)

    def _missing_permissions(self, channel: discord.TextChannel) -> list[str]:
        guild = channel.guild
        me = guild.me or (guild.get_member(int(self.bot.user.id)) if self.bot.user else None)
        if me is None:
            return ["não consegui localizar meu membro no servidor"]
        guild_perms = getattr(me, "guild_permissions", None)
        missing: list[str] = []
        if not bool(getattr(guild_perms, "ban_members", False)):
            missing.append("Banir membros")
        perms = channel.permissions_for(me)
        for attr, label in (
            ("view_channel", "Ver canal"),
            ("send_messages", "Enviar mensagens"),
            ("read_message_history", "Ver histórico"),
            ("add_reactions", "Adicionar reações"),
            ("manage_messages", "Gerenciar mensagens"),
        ):
            if not bool(getattr(perms, attr, False)):
                missing.append(label)
        return missing

    async def activate_trap(
        self,
        guild: discord.Guild | None,
        channel_id: int,
        *,
        actor_id: int,
    ) -> tuple[bool, str]:
        if guild is None:
            return False, "Servidor inválido"
        async with self._guild_lock(guild.id):
            return await self._activate_trap_locked(
                guild,
                channel_id,
                actor_id=actor_id,
            )

    async def _activate_trap_locked(
        self,
        guild: discord.Guild,
        channel_id: int,
        *,
        actor_id: int,
    ) -> tuple[bool, str]:
        channel = guild.get_channel(int(channel_id or 0))
        if not isinstance(channel, discord.TextChannel):
            return False, "Escolha um canal de texto"
        reserved_update_channel = int(getattr(self.bot, "ZIP_UPDATE_CHANNEL_ID", 0) or 0)
        if reserved_update_channel and int(channel.id) == reserved_update_channel:
            return False, "Esse canal é reservado para atualizações"
        missing = self._missing_permissions(channel)
        if missing:
            return False, "Permissões ausentes: " + ", ".join(missing)

        old_config = self.get_config(guild.id)
        warning: discord.Message | None = None
        try:
            warning = await channel.send(
                view=warning_view(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await warning.add_reaction(self._cancel_emoji)
            if self.bot.user is not None:
                await warning.remove_reaction(self._cancel_emoji, self.bot.user)
            config = {
                "enabled": True,
                "channel_id": int(channel.id),
                "warning_message_id": int(warning.id),
                "updated_by": int(actor_id or 0),
                "revision": int(old_config.get("revision") or 0) + 1,
            }
            if self.db is None or not hasattr(self.db, "set_antibot_config"):
                raise RuntimeError("persistência indisponível")
            await self.db.set_antibot_config(guild.id, config)
        except Exception as exc:
            await self._delete_quietly(warning)
            log.exception("ativação falhou guild=%s channel=%s", guild.id, channel.id)
            return False, f"Não foi possível ativar: {type(exc).__name__}"

        old_channel_id = int(old_config.get("channel_id") or 0)
        self._active_channel_to_guild.pop(old_channel_id, None)
        self._active_channel_to_guild[int(channel.id)] = int(guild.id)
        if old_channel_id and old_channel_id != int(channel.id):
            await self._cancel_guild_sessions(guild.id)
        await self._delete_saved_warning(guild, old_config, except_message_id=int(warning.id))
        return True, "Configuração salva"

    async def _delete_saved_warning(
        self,
        guild: discord.Guild,
        config: dict[str, Any],
        *,
        except_message_id: int = 0,
    ) -> None:
        channel_id = int(config.get("channel_id") or 0)
        message_id = int(config.get("warning_message_id") or 0)
        if not channel_id or not message_id or message_id == int(except_message_id or 0):
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = await channel.fetch_message(message_id)
            await message.delete()

    async def _cancel_guild_sessions(self, guild_id: int) -> None:
        sessions: list[TrapSession] = []
        seen: set[int] = set()
        for session in self._sessions_by_message.values():
            marker = id(session)
            if marker in seen or session.guild_id != int(guild_id) or session.closed:
                continue
            seen.add(marker)
            sessions.append(session)
        for session in sessions:
            async with session.lock:
                now = time.monotonic()
                for entry in session.entries.values():
                    if entry.state != STATE_WAITING:
                        continue
                    entry.state = STATE_CANCELLED
                    entry.terminal_at = now
                    self._pending_by_member.pop((session.guild_id, entry.user_id), None)
                    if entry.deadline_handle is not None:
                        entry.deadline_handle.cancel()
                        entry.deadline_handle = None
                    if entry.ban_task is not None and not entry.ban_task.done():
                        entry.ban_task.cancel()
                session.wake_event.set()

    async def deactivate_trap(
        self,
        guild: discord.Guild | None,
        *,
        actor_id: int,
        delete_warning: bool = True,
    ) -> tuple[bool, str]:
        if guild is None:
            return False, "Servidor inválido"
        async with self._guild_lock(guild.id):
            return await self._deactivate_trap_locked(
                guild,
                actor_id=actor_id,
                delete_warning=delete_warning,
            )

    async def _deactivate_trap_locked(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        delete_warning: bool,
    ) -> tuple[bool, str]:
        old_config = self.get_config(guild.id)
        config = {
            "enabled": False,
            "channel_id": 0,
            "warning_message_id": 0,
            "updated_by": int(actor_id or 0),
            "revision": int(old_config.get("revision") or 0) + 1,
        }
        try:
            if self.db is None or not hasattr(self.db, "set_antibot_config"):
                raise RuntimeError("persistência indisponível")
            await self.db.set_antibot_config(guild.id, config)
        except Exception as exc:
            log.exception("desativação falhou guild=%s", guild.id)
            return False, f"Não foi possível desativar: {type(exc).__name__}"

        self._active_channel_to_guild.pop(int(old_config.get("channel_id") or 0), None)
        await self._cancel_guild_sessions(guild.id)
        if delete_warning:
            await self._delete_saved_warning(guild, old_config)
        return True, "Armadilha desativada"

    @commands.command(name="antibot", aliases=("armadilha",))
    @commands.guild_only()
    async def antibot_command(self, ctx: commands.Context) -> None:
        if not await self.is_staff(getattr(ctx, "author", None)):
            await ctx.reply(
                view=notice_view("Sem permissão", "Somente a staff pode configurar a armadilha", ok=False),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = AntibotPanelView(
            self,
            owner_id=int(ctx.author.id),
            guild_id=int(ctx.guild.id),
        )
        await ctx.reply(
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
