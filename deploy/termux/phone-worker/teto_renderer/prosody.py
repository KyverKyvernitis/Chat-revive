from __future__ import annotations

from dataclasses import dataclass

from .phonemizer import Mora


@dataclass(frozen=True, slots=True)
class RenderNote:
    candidates: tuple[str, ...]
    pitch: str
    duration_ms: int
    pause_after_ms: int


def build_notes(moras: list[Mora], *, base_pitch: str = "C4") -> list[RenderNote]:
    if not moras:
        return []
    notes: list[RenderNote] = []
    for index, mora in enumerate(moras):
        pitch = base_pitch
        # A pequena elevação periódica evita uma leitura completamente plana sem
        # transformar a fala em uma melodia aleatória.
        if index and index % 7 == 4:
            pitch = "C#4"
        notes.append(RenderNote(
            candidates=mora.candidates,
            pitch=pitch,
            duration_ms=max(70, min(500, int(mora.duration_ms))),
            pause_after_ms=max(0, min(1000, int(mora.pause_after_ms))),
        ))
    return notes
