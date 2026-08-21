from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    CANCELLED_VISIBLE_SECONDS,
    COUNTDOWN_SECONDS,
    FAILED_VISIBLE_SECONDS,
    STAFF_JOKE_VISIBLE_SECONDS,
    STATE_BANNED,
    STATE_BANNING,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_STAFF_JOKE,
    STATE_WAITING,
    WARNING_EMOJI,
)


@dataclass(slots=True)
class ChallengeEntry:
    user_id: int
    trigger_message_id: int = 0
    deadline: float | None = None
    state: str = STATE_WAITING
    terminal_at: float | None = None
    staff_immune: bool = False
    deadline_handle: Any = field(default=None, repr=False, compare=False)
    ban_task: Any = field(default=None, repr=False, compare=False)

    @property
    def is_waiting(self) -> bool:
        return self.state in {STATE_WAITING, STATE_BANNING}

    @property
    def is_permanent(self) -> bool:
        return self.state == STATE_BANNED

    def remaining_seconds(self, now: float) -> int:
        if self.deadline is None:
            return COUNTDOWN_SECONDS
        # Enquanto a requisição de banimento ainda não terminou, a interface
        # nunca mostra zero nem um número negativo.
        return max(1, int(math.ceil(self.deadline - float(now))))

    def transient_expired(self, now: float) -> bool:
        if self.terminal_at is None:
            return False
        if self.state == STATE_CANCELLED:
            return now - self.terminal_at >= CANCELLED_VISIBLE_SECONDS
        if self.state == STATE_FAILED:
            return now - self.terminal_at >= FAILED_VISIBLE_SECONDS
        if self.state == STATE_STAFF_JOKE:
            return now - self.terminal_at >= STAFF_JOKE_VISIBLE_SECONDS
        return False


def render_entry(entry: ChallengeEntry, *, now: float, cancel_emoji: str) -> str:
    mention = f"<@{int(entry.user_id)}>"
    audit_identity = f"{mention} · `{int(entry.user_id)}`"
    if entry.state in {STATE_WAITING, STATE_BANNING}:
        remaining = entry.remaining_seconds(now)
        unit = "segundo" if remaining == 1 else "segundos"
        return (
            f"{mention}\n"
            f"Reaja com {cancel_emoji} para cancelar\n"
            f"**{WARNING_EMOJI} Você será banido em {remaining} {unit} se não reagir**"
        )
    if entry.state == STATE_CANCELLED:
        return f"{mention}\nBanimento cancelado"
    if entry.state == STATE_BANNED:
        return f"{audit_identity}\nConta banida"
    if entry.state == STATE_STAFF_JOKE:
        return (
            f"{mention}\n"
            "Você foi **banido**\n"
            "-# quer dizer, se você não fosse staff né (ou se você não tivesse um cargo "
            "acima do meu). Agora seja um bom garoto e pare de falar aqui."
        )
    return (
        f"{audit_identity}\n"
        "Banimento falhou\n"
        "Não foi possível banir o usuário"
    )


def render_batch(entries: list[ChallengeEntry], *, now: float, cancel_emoji: str) -> str:
    blocks = [render_entry(entry, now=now, cancel_emoji=cancel_emoji) for entry in entries]
    return "# Antibot\n\n" + "\n\n".join(blocks)
