from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


ACHIEVEMENT_NOTICE_MERGE_WINDOW_SECONDS = 15.0
ACHIEVEMENT_NOTICE_MAX_BURST_SECONDS = 45.0


def merge_achievement_keys(existing: Iterable[str], incoming: Iterable[str]) -> tuple[str, ...]:
    """Combina chaves preservando a primeira ocorrência de cada conquista."""
    merged: list[str] = []
    seen: set[str] = set()
    for source in (existing, incoming):
        for raw_key in source:
            key = str(raw_key or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(key)
    return tuple(merged)


@dataclass(slots=True)
class AchievementNoticeBurst:
    achievement_keys: tuple[str, ...]
    started_at: float
    last_at: float
    message: Any

    def expires_at(self) -> float:
        return min(
            float(self.last_at) + ACHIEVEMENT_NOTICE_MERGE_WINDOW_SECONDS,
            float(self.started_at) + ACHIEVEMENT_NOTICE_MAX_BURST_SECONDS,
        )

    def can_merge(self, now: float) -> bool:
        return float(now) <= self.expires_at()

    def is_expired(self, now: float) -> bool:
        return not self.can_merge(now)
