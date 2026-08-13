from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord

from .settings import CallKeeperSettings
from .store import CallKeeperStateStore
from .rescue_cmd import CallKeeperRescueCommandService

log = logging.getLogger(__name__)


class AuxVoiceClient(discord.Client):
    """Client mínimo dos bots auxiliares.

    Ele não carrega cogs nem cache amplo. Além de guilds + voice_states,
    lê mensagens apenas para o modo rescue `_cmd` quando o bot principal cai.
    """

    def __init__(self, slot: "AuxSlot", runtime: "CallKeeperRuntime"):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(intents=intents, heartbeat_timeout=60.0)
        self.slot = slot
        self.runtime = runtime
        self.ready_event = asyncio.Event()
        self._presence_task: Optional[asyncio.Task] = None

    async def _hide_status_dot(self) -> None:
        """Força presença offline somente nos bots auxiliares.

        O discord.py converte `discord.Status.offline` para `invisible`. Aqui
        usamos a string crua `offline` de propósito para testar o visual sem
        indicador que alguns bots exibem no perfil/mobile. Se a lib/API rejeitar
        em algum ambiente, caímos para `invisible` sem derrubar o serviço.
        O estado de voz continua separado e pode seguir aparecendo como
        "Em voz" quando o Discord exibir esse detalhe.
        """
        try:
            await self.change_presence(status="offline", activity=None)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.slot.last_error = f"presence_offline: {type(exc).__name__}: {exc}"
            log.warning(
                "[callkeeper] falha ocultando presença offline do aux %s: %s",
                self.slot.index,
                self.slot.last_error,
            )
            try:
                await self.change_presence(status=discord.Status.invisible, activity=None)
            except asyncio.CancelledError:
                raise
            except Exception as fallback_exc:
                self.slot.last_error = f"presence_invisible: {type(fallback_exc).__name__}: {fallback_exc}"
                log.warning(
                    "[callkeeper] falha ocultando presença invisível do aux %s: %s",
                    self.slot.index,
                    self.slot.last_error,
                )

    def _schedule_hide_status_dot(self, *, delay: float = 2.0) -> None:
        if self.is_closed():
            return
        if self._presence_task is not None and not self._presence_task.done():
            self._presence_task.cancel()
        self._presence_task = asyncio.create_task(
            self._delayed_hide_status_dot(delay),
            name=f"callkeeper-aux-{self.slot.index}-presence",
        )

    async def _delayed_hide_status_dot(self, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._hide_status_dot()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[callkeeper] erro reaplicando presença offline do aux %s", self.slot.index)

    async def on_ready(self):
        self.ready_event.set()
        self.slot.last_error = None
        await self._hide_status_dot()
        self._schedule_hide_status_dot(delay=2.0)
        user = self.user
        log.info(
            "[callkeeper] aux %s pronto: %s (%s)",
            self.slot.index,
            getattr(user, "name", "desconhecido"),
            getattr(user, "id", "?"),
        )
        self.runtime.schedule_reconcile("aux_ready", delay=0.05)

    async def on_resumed(self):
        await self._hide_status_dot()
        self._schedule_hide_status_dot(delay=2.0)
        self.runtime.schedule_reconcile("aux_resumed", delay=0.05)

    async def on_disconnect(self):
        self.runtime.schedule_reconcile("aux_gateway_disconnect", delay=0.25)


    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        await self.runtime.rescue_cmd.handle_message(message)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        user = self.user
        if user is not None and int(member.id) == int(user.id):
            await self.runtime.on_aux_voice_state(self.slot, before, after)
            return

        guild = getattr(member, "guild", None)
        if guild is None or int(guild.id) != int(self.runtime.settings.guild_id):
            return

        before_id = int(getattr(getattr(before, "channel", None), "id", 0) or 0)
        after_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
        target_id = self.runtime.get_target_channel_id()
        if target_id and (before_id == target_id or after_id == target_id):
            self.runtime.schedule_reconcile("aux_seen_voice_update", delay=self.runtime.settings.event_debounce)


@dataclass(eq=False)
class AuxSlot:
    index: int
    token: str
    runtime: "CallKeeperRuntime"
    client: Optional[AuxVoiceClient] = None
    task: Optional[asyncio.Task] = None
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    blocked_until: float = 0.0
    intentional_until: float = 0.0
    last_error: Optional[str] = None

    @property
    def label(self) -> str:
        return f"Aux {self.index}"

    def is_ready(self) -> bool:
        return bool(self.client and not self.client.is_closed() and self.client.ready_event.is_set())

    def is_running(self) -> bool:
        return bool(self.client and not self.client.is_closed() and self.task and not self.task.done())

    def user_id(self) -> int:
        user = getattr(self.client, "user", None)
        return int(getattr(user, "id", 0) or 0)

    def voice_client(self) -> Optional[discord.VoiceClient]:
        client = self.client
        if client is None:
            return None
        for voice_client in list(getattr(client, "voice_clients", []) or []):
            if isinstance(voice_client, discord.VoiceClient):
                return voice_client
        return None

    def voice_channel_id(self) -> int:
        """Canal de voz atual do auxiliar.

        Após restart, o processo novo pode não ter VoiceClient local ainda, mas
        o Discord ainda mostra o bot na call. Nessa situação reidratamos pelo
        voice_state real (`guild.me.voice`) e NÃO fazemos leave/join artificial.
        """
        voice_client = self.voice_client()
        if voice_client is not None and voice_client.is_connected():
            channel = getattr(voice_client, "channel", None)
            channel_id = int(getattr(channel, "id", 0) or 0)
            if channel_id > 0:
                return channel_id

        client = self.client
        if client is None or not self.is_ready():
            return 0
        guild = client.get_guild(self.runtime.settings.guild_id)
        if guild is None:
            return 0
        me = getattr(guild, "me", None)
        voice_state = getattr(me, "voice", None)
        channel = getattr(voice_state, "channel", None)
        return int(getattr(channel, "id", 0) or 0)

    async def ensure_started(self) -> None:
        if not self.token:
            self.last_error = "token ausente"
            return
        if self.is_running():
            return

        if self.client and not self.client.is_closed():
            with contextlib.suppress(Exception):
                await self.client.close()

        self.client = AuxVoiceClient(self, self.runtime)
        self.task = asyncio.create_task(self._runner(), name=f"callkeeper-aux-{self.index}")

    async def _runner(self) -> None:
        assert self.client is not None
        try:
            await self.client.start(self.token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("[callkeeper] aux %s parou com erro", self.index)
        finally:
            self.runtime.schedule_reconcile("aux_runner_finished", delay=1.0)

    async def close(self) -> None:
        self.intentional_until = time.monotonic() + 10.0
        if self.client is not None:
            if getattr(self.client, "_presence_task", None) is not None:
                self.client._presence_task.cancel()
            with contextlib.suppress(Exception):
                for voice_client in list(self.client.voice_clients):
                    await voice_client.disconnect(force=True)
            with contextlib.suppress(Exception):
                await self.client.close()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(Exception):
                await self.task
        self.task = None
        self.client = None


class CallKeeperRuntime:
    """Runtime standalone que realmente controla os 3 bots auxiliares."""

    def __init__(self, *, settings: CallKeeperSettings, store: CallKeeperStateStore):
        self.settings = settings
        self.store = store
        self.rescue_cmd = CallKeeperRescueCommandService(repo_root=Path(__file__).resolve().parents[1])
        self.slots: list[AuxSlot] = [
            AuxSlot(index=i + 1, token=token, runtime=self)
            for i, token in enumerate(settings.bot_tokens[:3])
        ]
        self._reconcile_lock = asyncio.Lock()
        self._scheduled_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._last_seen_target_channel_id = 0
        self._last_logged_state_key: tuple[bool, int, int] | None = None
        self._last_reconcile_log_key: tuple[str, int, int, int] | None = None
        self._last_reconcile_log_at: float = 0.0

    async def start(self) -> None:
        if self.settings.guild_id <= 0:
            raise RuntimeError("CALLKEEPER_GUILD_ID não está configurado.")
        if len(self.slots) < 3:
            raise RuntimeError("Configure CALLKEEPER_BOT_1_TOKEN, CALLKEEPER_BOT_2_TOKEN e CALLKEEPER_BOT_3_TOKEN.")

        await self.ensure_aux_clients_started()
        self.start_watchdog()
        self.schedule_reconcile("service_start", delay=0.25)
        log.info("[callkeeper] serviço iniciado na guild %s", self.settings.guild_id)

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._scheduled_task, self._watchdog_task):
            if task and not task.done():
                task.cancel()
        for slot in list(self.slots):
            await slot.close()
        log.info("[callkeeper] serviço encerrado")

    async def run_forever(self) -> None:
        await self.start()
        await self._stopping.wait()

    def is_enabled(self) -> bool:
        return self.store.is_enabled(self.settings.guild_id)

    def get_target_channel_id(self) -> int:
        return self.store.get_channel_id(self.settings.guild_id)

    async def ensure_aux_clients_started(self) -> None:
        for slot in self.slots:
            await slot.ensure_started()

    def start_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="callkeeper-watchdog")

    async def _watchdog_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.reconcile("watchdog")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[callkeeper] erro no watchdog")
            await asyncio.sleep(self.settings.watchdog_interval)

    def schedule_reconcile(self, reason: str, *, delay: float | None = None) -> None:
        if self._stopping.is_set():
            return
        if self._scheduled_task and not self._scheduled_task.done():
            return
        self._scheduled_task = asyncio.create_task(
            self._delayed_reconcile(reason, self.settings.event_debounce if delay is None else delay),
            name="callkeeper-reconcile-event",
        )

    async def _delayed_reconcile(self, reason: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.reconcile(reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[callkeeper] erro no reconcile agendado")
        finally:
            self._scheduled_task = None

    async def on_aux_voice_state(
        self,
        slot: AuxSlot,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        before_id = int(getattr(getattr(before, "channel", None), "id", 0) or 0)
        after_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
        target_id = self.get_target_channel_id()
        if target_id and before_id == target_id and after_id != target_id:
            now = time.monotonic()
            if now > slot.intentional_until:
                slot.blocked_until = now + self.settings.disconnect_cooldown
                log.warning("[callkeeper] %s saiu da call alvo; procurando substituto", slot.label)
        if target_id and (before_id == target_id or after_id == target_id):
            self.schedule_reconcile("aux_voice_state", delay=0.05)

    def _required_bots_for_member_count(self, member_count: int) -> int:
        member_count = max(0, int(member_count or 0))
        if member_count <= 0:
            return 3
        return max(0, 3 - member_count)

    def _is_voice_target(self, channel: object) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))

    def _aux_user_ids(self) -> set[int]:
        return {slot.user_id() for slot in self.slots if slot.user_id()}

    def _ready_client(self) -> Optional[AuxVoiceClient]:
        for slot in self.slots:
            if slot.client is not None and slot.is_ready():
                return slot.client
        return None

    async def _target_channel(self):
        channel_id = self.get_target_channel_id()
        if channel_id <= 0:
            return None
        client = self._ready_client()
        if client is None:
            return None
        channel = client.get_channel(channel_id)
        if self._is_voice_target(channel):
            return channel
        guild = client.get_guild(self.settings.guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if self._is_voice_target(channel):
                return channel
        try:
            fetched = await client.fetch_channel(channel_id)
        except Exception:
            return None
        if self._is_voice_target(fetched):
            return fetched
        return None

    async def _member_count_without_callkeeper_bots(self) -> int:
        channel = await self._target_channel()
        if channel is None:
            return 0
        aux_ids = self._aux_user_ids()
        count = 0
        for member in list(getattr(channel, "members", []) or []):
            # Ignora somente os 3 bots auxiliares do CallKeeper. Outros bots
            # normais contam como membros para a regra.
            if int(member.id) in aux_ids:
                continue
            count += 1
        return count

    async def _aux_channel_for_slot(self, slot: AuxSlot, channel_id: int):
        client = slot.client
        if client is None or not slot.is_ready():
            return None
        channel = client.get_channel(channel_id)
        if self._is_voice_target(channel):
            return channel
        guild = client.get_guild(self.settings.guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if self._is_voice_target(channel):
                return channel
        try:
            fetched = await client.fetch_channel(channel_id)
        except Exception as exc:
            slot.last_error = f"fetch_channel: {type(exc).__name__}: {exc}"
            return None
        if self._is_voice_target(fetched):
            return fetched
        return None

    async def _connect_slot(self, slot: AuxSlot, channel_id: int) -> bool:
        async with slot.connect_lock:
            await slot.ensure_started()
            if slot.client is None:
                return False
            try:
                await asyncio.wait_for(slot.client.ready_event.wait(), timeout=12.0)
            except asyncio.TimeoutError:
                slot.last_error = "gateway ainda não ficou ready"
                return False

            current_state_channel_id = slot.voice_channel_id()
            if current_state_channel_id == int(channel_id):
                await self._enforce_self_mute_deaf(slot)
                slot.blocked_until = 0.0
                slot.last_error = None
                return True

            current_vc = slot.voice_client()

            channel = await self._aux_channel_for_slot(slot, channel_id)
            if channel is None:
                return False

            if current_vc is not None and current_vc.is_connected():
                current_channel_id = int(getattr(getattr(current_vc, "channel", None), "id", 0) or 0)
                if current_channel_id == int(channel_id):
                    await self._enforce_self_mute_deaf(slot)
                    return True
                slot.intentional_until = time.monotonic() + 3.0
                try:
                    await current_vc.move_to(channel, timeout=10.0)
                    await self._enforce_self_mute_deaf(slot)
                    slot.blocked_until = 0.0
                    slot.last_error = None
                    log.info("[callkeeper] %s movido para %s", slot.label, channel_id)
                    return True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    slot.last_error = f"move_to: {type(exc).__name__}: {exc}"
                    log.warning("[callkeeper] falha movendo %s: %s", slot.label, slot.last_error)
                    slot.intentional_until = time.monotonic() + 3.0
                    with contextlib.suppress(Exception):
                        await current_vc.disconnect(force=True)

            # Se o auxiliar já está em alguma call mas este processo não tem um
            # VoiceClient local, move o voice_state existente em vez de fazer
            # um ciclo visual de sair/entrar.
            if current_state_channel_id > 0 and current_vc is None:
                try:
                    guild = slot.client.get_guild(self.settings.guild_id) if slot.client else None
                    if guild is not None:
                        await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)
                        slot.blocked_until = 0.0
                        slot.last_error = None
                        log.info("[callkeeper] %s movido para %s sem reconnect", slot.label, channel_id)
                        return True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    slot.last_error = f"move_state: {type(exc).__name__}: {exc}"
                    log.warning("[callkeeper] falha movendo %s: %s", slot.label, slot.last_error)

            try:
                await channel.connect(timeout=10.0, reconnect=True, self_mute=True, self_deaf=True)
                slot.blocked_until = 0.0
                slot.last_error = None
                await self._enforce_self_mute_deaf(slot)
                log.info("[callkeeper] %s conectado em %s", slot.label, channel_id)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                slot.last_error = f"connect: {type(exc).__name__}: {exc}"
                log.warning("[callkeeper] falha conectando %s: %s", slot.label, slot.last_error)
                return False

    async def _disconnect_slot(self, slot: AuxSlot, *, intentional_seconds: float = 3.0) -> None:
        slot.intentional_until = time.monotonic() + intentional_seconds
        voice_client = slot.voice_client()
        if voice_client is not None and voice_client.is_connected():
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)
            return

        client = slot.client
        if client is None or not slot.is_ready():
            return
        guild = client.get_guild(self.settings.guild_id)
        if guild is None:
            return
        me = getattr(guild, "me", None)
        voice_state = getattr(me, "voice", None)
        if getattr(voice_state, "channel", None) is None:
            return
        with contextlib.suppress(Exception):
            await guild.change_voice_state(channel=None)

    async def _enforce_self_mute_deaf(self, slot: AuxSlot) -> None:
        client = slot.client
        if client is None or not slot.is_ready():
            return
        guild = client.get_guild(self.settings.guild_id)
        if guild is None:
            return
        me = guild.me
        voice_state = getattr(me, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if channel is None:
            return
        if bool(getattr(voice_state, "self_mute", False)) and bool(getattr(voice_state, "self_deaf", False)):
            return
        try:
            await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)
        except Exception as exc:
            slot.last_error = f"self_mute_deaf: {type(exc).__name__}: {exc}"

    async def _disconnect_all(self) -> None:
        for slot in list(self.slots):
            try:
                await self._disconnect_slot(slot, intentional_seconds=10.0)
            except Exception:
                log.exception("[callkeeper] falha desconectando %s", slot.label)

    async def reconcile(self, reason: str) -> None:
        async with self._reconcile_lock:
            await self.ensure_aux_clients_started()
            await self.store.refresh_guild_state(self.settings.guild_id)

            enabled = self.is_enabled()
            channel_id = self.get_target_channel_id()
            revision = self.store.get_revision(self.settings.guild_id)
            state_key = (enabled, channel_id, revision)
            if state_key != self._last_logged_state_key:
                log.info(
                    "[callkeeper] estado DB atualizado | enabled=%s channel_id=%s revision=%s reason=%s",
                    enabled,
                    channel_id or 0,
                    revision,
                    reason,
                )
                self._last_logged_state_key = state_key

            if not enabled:
                await self._disconnect_all()
                self._last_seen_target_channel_id = 0
                return

            if channel_id <= 0:
                log.warning("[callkeeper] ativo, mas sem call alvo configurada")
                self._last_seen_target_channel_id = 0
                return
            if self._last_seen_target_channel_id and self._last_seen_target_channel_id != channel_id:
                log.info("[callkeeper] foco alterado: %s -> %s", self._last_seen_target_channel_id, channel_id)
            self._last_seen_target_channel_id = channel_id

            member_count = await self._member_count_without_callkeeper_bots()
            required = min(3, self._required_bots_for_member_count(member_count))
            now_log = time.monotonic()
            log_key = (reason, member_count, required, channel_id)
            important_reason = reason not in {"watchdog", "aux_seen_voice_update", "aux_voice_state"}
            if important_reason or log_key != self._last_reconcile_log_key or (now_log - self._last_reconcile_log_at) >= 60.0:
                log.info(
                    "[callkeeper] reconcile %s | membros_nao_callkeeper=%s required=%s target=%s",
                    reason,
                    member_count,
                    required,
                    channel_id,
                )
                self._last_reconcile_log_key = log_key
                self._last_reconcile_log_at = now_log

            target_connected: list[AuxSlot] = []
            connected_elsewhere: list[AuxSlot] = []
            for slot in self.slots:
                vc_channel_id = slot.voice_channel_id()
                if vc_channel_id == channel_id:
                    target_connected.append(slot)
                elif vc_channel_id:
                    connected_elsewhere.append(slot)

            for slot in connected_elsewhere:
                await self._disconnect_slot(slot, intentional_seconds=3.0)

            for slot in list(target_connected):
                await self._enforce_self_mute_deaf(slot)

            if len(target_connected) > required:
                extras = sorted(target_connected, key=lambda s: s.index, reverse=True)[: len(target_connected) - required]
                for slot in extras:
                    await self._disconnect_slot(slot, intentional_seconds=6.0)
                target_connected = [slot for slot in target_connected if slot not in extras]

            deficit = required - len(target_connected)
            if deficit <= 0:
                return

            now = time.monotonic()
            candidates = [
                slot
                for slot in self.slots
                if slot not in target_connected and slot.voice_channel_id() != channel_id
            ]
            ready_candidates = [slot for slot in candidates if slot.is_ready()]
            cold_candidates = [slot for slot in candidates if not slot.is_ready()]
            ready_candidates.sort(key=lambda slot: (slot.blocked_until > now, slot.index))
            ordered = ready_candidates + cold_candidates

            connected_now = 0
            for slot in ordered:
                if connected_now >= deficit:
                    break
                ok = await self._connect_slot(slot, channel_id)
                if ok:
                    connected_now += 1

            log.debug(
                "[callkeeper] reconcile %s | membros_nao_callkeeper=%s required=%s connected=%s deficit=%s",
                reason,
                member_count,
                required,
                len(target_connected) + connected_now,
                max(0, deficit - connected_now),
            )
