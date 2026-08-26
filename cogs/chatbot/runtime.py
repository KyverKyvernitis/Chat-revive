"""Admissão e ciclo de vida das tarefas do chatbot.

O listener do Discord precisa continuar barato, mas ``create_task`` sozinho
não limita trabalho. Este módulo fornece:

* fila limitada por classe de trabalho;
* apenas uma solicitação pesada por usuário;
* semáforos separados para chat, STT, imagem e persona;
* rastreamento/cancelamento no unload do cog.

Não há dependências externas e todo estado é limitado, adequado à VPS de 1 GB.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Coroutine, Optional, TypeVar

from . import constants as C

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class AdmissionSnapshot:
    queued_chat: int
    queued_image: int
    queued_persona: int
    inflight_users: int


class AdmissionLease:
    """Reserva de fila que vira slot ativo ao entrar no context manager."""

    def __init__(
        self,
        controller: "AdmissionController",
        kind: str,
        user_key: tuple[int, int],
    ) -> None:
        self._controller = controller
        self.kind = kind
        self.user_key = user_key
        self._entered = False
        self._released = False

    async def __aenter__(self) -> "AdmissionLease":
        if self._entered:
            raise RuntimeError("lease do chatbot reutilizado")
        if self._released:
            raise RuntimeError("lease do chatbot já liberado")
        self._entered = True
        try:
            await self._controller._semaphores[self.kind].acquire()
        except BaseException:
            # Se a task for cancelada enquanto espera no semaphore, a reserva
            # de fila/usuário também precisa ser desfeita.
            self._released = True
            await self._controller._release(self.kind, self.user_key)
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()

    async def switch_kind(self, new_kind: str) -> bool:
        """Move uma reserva ativa para outra fila sem reabrir o usuário.

        O slot antigo é devolvido antes de esperar o novo. Assim, um pedido de
        imagem detectado dentro do chat não ocupa uma vaga de LLM por até dois
        minutos enquanto aguarda o gerador. Retorna ``False`` se a nova fila já
        estiver cheia, mantendo a lease original intacta.
        """
        if self._released:
            raise RuntimeError("lease do chatbot já liberado")
        if new_kind == self.kind:
            return True
        old_kind = self.kind
        changed = await self._controller._reclassify(
            old_kind, new_kind, self.user_key,
        )
        if not changed:
            return False
        if not self._entered:
            self.kind = new_kind
            return True

        self._controller._semaphores[old_kind].release()
        self.kind = new_kind
        try:
            await self._controller._semaphores[new_kind].acquire()
        except BaseException:
            self._released = True
            await self._controller._release(new_kind, self.user_key)
            raise
        return True

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._entered:
            self._controller._semaphores[self.kind].release()
        await self._controller._release(self.kind, self.user_key)


class AdmissionController:
    """Controla quantidade de coroutines antes de qualquer trabalho caro."""

    def __init__(self) -> None:
        self._semaphores = {
            "chat": asyncio.Semaphore(C.MAX_CONCURRENT_REQUESTS),
            "image": asyncio.Semaphore(C.IMAGE_MAX_CONCURRENT_REQUESTS),
            "persona": asyncio.Semaphore(C.PERSONA_MAX_CONCURRENT_REQUESTS),
        }
        self._resource_semaphores = {
            "stt": asyncio.Semaphore(C.STT_MAX_CONCURRENT_REQUESTS),
            "image": self._semaphores["image"],
        }
        self._queue_limits = {
            "chat": C.MAX_QUEUE_SIZE,
            "image": C.IMAGE_MAX_QUEUE_SIZE,
            "persona": C.PERSONA_MAX_QUEUE_SIZE,
        }
        self._queued = {kind: 0 for kind in self._queue_limits}
        self._inflight_users: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    async def try_admit(
        self,
        kind: str,
        *,
        guild_id: int,
        user_id: int,
    ) -> Optional[AdmissionLease]:
        """Reserva fila sem esperar; retorna ``None`` quando deve rejeitar."""
        if kind not in self._queue_limits:
            raise ValueError(f"classe de admissão inválida: {kind}")
        key = (int(guild_id), int(user_id))
        async with self._lock:
            if key in self._inflight_users:
                return None
            if self._queued[kind] >= self._queue_limits[kind]:
                return None
            self._queued[kind] += 1
            self._inflight_users.add(key)
        return AdmissionLease(self, kind, key)

    async def _release(self, kind: str, user_key: tuple[int, int]) -> None:
        async with self._lock:
            self._queued[kind] = max(0, self._queued[kind] - 1)
            self._inflight_users.discard(user_key)

    async def _reclassify(
        self,
        old_kind: str,
        new_kind: str,
        user_key: tuple[int, int],
    ) -> bool:
        if old_kind not in self._queue_limits or new_kind not in self._queue_limits:
            raise ValueError(f"classe de admissão inválida: {new_kind}")
        async with self._lock:
            if user_key not in self._inflight_users:
                raise RuntimeError("reserva de usuário inexistente")
            if self._queued[new_kind] >= self._queue_limits[new_kind]:
                return False
            self._queued[old_kind] = max(0, self._queued[old_kind] - 1)
            self._queued[new_kind] += 1
            return True

    @asynccontextmanager
    async def resource(self, kind: str):
        """Slot compartilhado para um sub-recurso dentro de um turno de chat."""
        semaphore = self._resource_semaphores.get(kind)
        if semaphore is None:
            raise ValueError(f"recurso inválido: {kind}")
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    def snapshot(self) -> AdmissionSnapshot:
        return AdmissionSnapshot(
            queued_chat=int(self._queued.get("chat", 0)),
            queued_image=int(self._queued.get("image", 0)),
            queued_persona=int(self._queued.get("persona", 0)),
            inflight_users=len(self._inflight_users),
        )


class TaskSupervisor:
    """Mantém referências fortes e observa exceções de tasks fire-and-forget."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    def create(self, coro: Coroutine[object, object, T], *, name: str) -> Optional[asyncio.Task[T]]:
        if self._closing:
            coro.close()
            return None
        task: asyncio.Task[T] = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("chatbot: falha ao observar task %s", done.get_name())
                return
            if error is not None:
                log.error(
                    "chatbot: task %s terminou com erro",
                    done.get_name(),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_done)
        return task

    @property
    def count(self) -> int:
        return len(self._tasks)

    async def shutdown(self, timeout: float = C.TASK_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        self._closing = True
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(0.1, float(timeout)),
            )
        except asyncio.TimeoutError:
            log.warning("chatbot: %s task(s) não encerraram no prazo", len(tasks))
        finally:
            self._tasks.clear()
