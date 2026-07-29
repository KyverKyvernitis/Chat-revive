from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import TetoVoicebankError


@dataclass(frozen=True, slots=True)
class OtoEntry:
    alias: str
    wav_path: Path
    offset_ms: float
    consonant_ms: float
    cutoff_ms: float
    preutterance_ms: float
    overlap_ms: float

    def cache_identity(self) -> str:
        try:
            stat = self.wav_path.stat()
            stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            stamp = "missing"
        return "|".join((
            self.alias,
            str(self.wav_path),
            stamp,
            f"{self.offset_ms:.3f}",
            f"{self.consonant_ms:.3f}",
            f"{self.cutoff_ms:.3f}",
            f"{self.preutterance_ms:.3f}",
            f"{self.overlap_ms:.3f}",
        ))


def _decode_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def _number(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _normalized_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class VoicebankIndex:
    def __init__(self, root: Path, entries: dict[str, OtoEntry], fingerprint: str, name: str):
        self.root = root
        self.entries = entries
        self.fingerprint = fingerprint
        self.name = name

    @classmethod
    def load(cls, root: str | Path, *, minimum_aliases: int = 10) -> "VoicebankIndex":
        base = Path(root).expanduser().resolve()
        if not base.is_dir():
            raise TetoVoicebankError(f"voicebank não encontrada: {base}")

        oto_files = sorted(base.rglob("oto.ini"))
        if not oto_files:
            raise TetoVoicebankError("voicebank sem oto.ini")

        entries: dict[str, OtoEntry] = {}
        digest = hashlib.sha256()
        digest.update(str(base).encode("utf-8", errors="replace"))

        for oto_path in oto_files:
            resolved_oto = oto_path.resolve()
            if not _is_within(resolved_oto, base):
                continue
            try:
                text = _decode_text(oto_path)
                stat = oto_path.stat()
                digest.update(str(oto_path.relative_to(base)).encode("utf-8", errors="replace"))
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
            except OSError as exc:
                raise TetoVoicebankError(f"não foi possível ler {oto_path}: {exc}") from exc

            for line in text.splitlines():
                clean = line.strip().lstrip("\ufeff")
                if not clean or clean.startswith(("#", ";")) or "=" not in clean:
                    continue
                wav_name, payload = clean.split("=", 1)
                values = [part.strip() for part in payload.split(",")]
                while len(values) < 6:
                    values.append("")
                alias = _normalized_alias(values[0] or Path(wav_name).stem)
                if not alias:
                    continue
                wav_path = (oto_path.parent / wav_name.strip()).resolve()
                if not _is_within(wav_path, base) or not wav_path.is_file():
                    continue
                entry = OtoEntry(
                    alias=alias,
                    wav_path=wav_path,
                    offset_ms=max(0.0, _number(values[1])),
                    consonant_ms=max(0.0, _number(values[2])),
                    cutoff_ms=_number(values[3]),
                    preutterance_ms=max(0.0, _number(values[4])),
                    overlap_ms=max(0.0, _number(values[5])),
                )
                entries.setdefault(alias, entry)
                normalized = _normalized_alias(alias).lower()
                entries.setdefault(normalized, entry)
                digest.update(entry.cache_identity().encode("utf-8", errors="replace"))

        unique_entries = {id(entry): entry for entry in entries.values()}
        if len(unique_entries) < max(1, int(minimum_aliases)):
            raise TetoVoicebankError(
                f"voicebank possui poucos aliases válidos ({len(unique_entries)} < {minimum_aliases})"
            )

        name = "Kasane Teto"
        character = base / "character.txt"
        if character.is_file():
            try:
                for line in _decode_text(character).splitlines():
                    if line.lower().startswith("name="):
                        candidate = line.split("=", 1)[1].strip()
                        if candidate:
                            name = candidate[:120]
                        break
            except OSError:
                pass

        return cls(base, entries, digest.hexdigest(), name)

    @property
    def alias_count(self) -> int:
        return len({id(entry) for entry in self.entries.values()})

    def resolve(self, candidates: Iterable[str]) -> OtoEntry | None:
        for candidate in candidates:
            key = _normalized_alias(candidate)
            if not key:
                continue
            entry = self.entries.get(key) or self.entries.get(key.lower())
            if entry is not None:
                return entry
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "root": str(self.root),
            "aliases": self.alias_count,
            "fingerprint": self.fingerprint,
        }
