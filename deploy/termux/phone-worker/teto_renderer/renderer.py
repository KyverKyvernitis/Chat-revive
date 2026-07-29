from __future__ import annotations

import array
import contextlib
import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from .cache import FragmentCache
from .errors import TetoConfigurationError, TetoResourceError, TetoSynthesisError
from .phonemizer import phonemize
from .prosody import RenderNote, build_notes
from .voicebank import OtoEntry, VoicebankIndex


class TetoRenderer:
    SAMPLE_RATE = 44100

    def __init__(self, *, resource_guard: Callable[[], dict[str, Any]] | None = None):
        self._resource_guard = resource_guard
        self._lock = threading.Lock()
        self._index: VoicebankIndex | None = None
        self._index_path = ""
        self._last_status_at = 0.0
        self._last_status: dict[str, Any] = {}
        cache_root = os.getenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "teto-fragments")
        self._cache = FragmentCache(cache_root, max_mb=self._env_int("PHONE_WORKER_TETO_FRAGMENT_CACHE_MB", 256))

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        value = str(os.getenv(name, "") or "").strip().lower()
        if value in {"1", "true", "yes", "on", "sim"}:
            return True
        if value in {"0", "false", "no", "off", "nao", "não"}:
            return False
        return default

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(str(os.getenv(name, default)).strip())
        except (TypeError, ValueError):
            return default

    def _voicebank_dir(self) -> str:
        return str(os.getenv("PHONE_WORKER_TETO_VOICEBANK_DIR") or "").strip()

    def _resampler_command(self) -> list[str]:
        raw = str(os.getenv("PHONE_WORKER_TETO_RESAMPLER_COMMAND") or "").strip()
        if not raw:
            raise TetoConfigurationError("PHONE_WORKER_TETO_RESAMPLER_COMMAND não configurado")
        command = shlex.split(raw)
        if not command:
            raise TetoConfigurationError("comando do resampler vazio")
        executable = command[0]
        if os.path.sep in executable:
            if not Path(executable).expanduser().is_file():
                raise TetoConfigurationError(f"resampler não encontrado: {executable}")
            command[0] = str(Path(executable).expanduser())
        elif not shutil.which(executable):
            raise TetoConfigurationError(f"executável do resampler não encontrado: {executable}")
        return command

    def _load_index(self) -> VoicebankIndex:
        configured = self._voicebank_dir()
        if not configured:
            raise TetoConfigurationError("PHONE_WORKER_TETO_VOICEBANK_DIR não configurado")
        resolved = str(Path(configured).expanduser().resolve())
        if self._index is None or self._index_path != resolved:
            self._index = VoicebankIndex.load(resolved, minimum_aliases=self._env_int("PHONE_WORKER_TETO_MIN_ALIASES", 10))
            self._index_path = resolved
        return self._index

    def status(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        ttl = max(1, self._env_int("PHONE_WORKER_TETO_STATUS_CACHE_SECONDS", 15))
        if not force and self._last_status and now - self._last_status_at <= ttl:
            return dict(self._last_status)
        enabled = self._env_bool("PHONE_WORKER_TETO_ENABLED", False)
        result: dict[str, Any] = {
            "ok": False,
            "available": False,
            "ready": False,
            "enabled": enabled,
            "engine": "teto",
            "voice": "kasane-teto-standard",
        }
        if not enabled:
            result["last_error"] = "PHONE_WORKER_TETO_ENABLED=false"
        else:
            try:
                command = self._resampler_command()
                if not shutil.which("ffmpeg"):
                    raise TetoConfigurationError("ffmpeg não encontrado")
                index = self._load_index()
                result.update(index.snapshot())
                result.update({
                    "ok": True,
                    "available": True,
                    "ready": True,
                    "resampler": " ".join(command[:2]),
                    "last_error": "",
                })
            except Exception as exc:
                result["last_error"] = f"{type(exc).__name__}: {exc}"[:220]
        self._last_status_at = now
        self._last_status = dict(result)
        return result

    def fingerprint(self) -> str:
        try:
            return self._load_index().fingerprint
        except Exception:
            return "unavailable"

    def _check_resources(self) -> None:
        if self._resource_guard is None:
            return
        snapshot = self._resource_guard() or {}
        if not snapshot.get("ok", False):
            raise TetoResourceError(str(snapshot.get("reason") or "recursos insuficientes para Teto"))

    @staticmethod
    def _format_number(value: float) -> str:
        return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"

    def _resample_note(self, *, index: VoicebankIndex, entry: OtoEntry, note: RenderNote, workdir: Path, deadline: float) -> Path:
        payload = "|".join((
            index.fingerprint,
            entry.cache_identity(),
            note.pitch,
            str(note.duration_ms),
            str(self._env_int("PHONE_WORKER_TETO_VELOCITY", 100)),
            str(os.getenv("PHONE_WORKER_TETO_FLAGS") or ""),
        ))
        key = self._cache.key(payload)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tempo da renderização Teto esgotado")
        raw_output = workdir / f"{key}.raw.wav"
        normalized_output = workdir / f"{key}.wav"
        command = self._resampler_command() + [
            str(entry.wav_path),
            str(raw_output),
            note.pitch,
            str(max(1, min(200, self._env_int("PHONE_WORKER_TETO_VELOCITY", 100)))),
            str(os.getenv("PHONE_WORKER_TETO_FLAGS") or ""),
            self._format_number(entry.offset_ms),
            str(note.duration_ms),
            self._format_number(entry.consonant_ms),
            self._format_number(entry.cutoff_ms),
            "100",
            "0",
            f"!{max(60, min(240, self._env_int('PHONE_WORKER_TETO_TEMPO', 140)))}",
            "AA",
        ]
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.5, remaining),
            check=False,
        )
        if proc.returncode != 0 or not raw_output.is_file() or raw_output.stat().st_size <= 44:
            error = proc.stderr.decode("utf-8", errors="replace")[-500:]
            raise TetoSynthesisError(f"resampler falhou para {entry.alias!r}: {error or proc.returncode}")

        remaining = deadline - time.monotonic()
        ffmpeg = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(raw_output), "-ac", "1", "-ar", str(self.SAMPLE_RATE),
                "-c:a", "pcm_s16le", str(normalized_output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(0.5, remaining),
            check=False,
        )
        if ffmpeg.returncode != 0 or not normalized_output.is_file() or normalized_output.stat().st_size <= 44:
            error = ffmpeg.stderr.decode("utf-8", errors="replace")[-500:]
            raise TetoSynthesisError(f"ffmpeg não normalizou fragmento: {error or ffmpeg.returncode}")
        return self._cache.put(key, normalized_output)

    def _read_samples(self, path: Path) -> array.array:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != self.SAMPLE_RATE:
                raise TetoSynthesisError(f"fragmento WAV inesperado: {path.name}")
            samples = array.array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
            if os.sys.byteorder != "little":
                samples.byteswap()
            return samples

    @staticmethod
    def _append_crossfade(target: array.array, fragment: array.array, overlap_samples: int) -> None:
        if not target or not fragment or overlap_samples <= 0:
            target.extend(fragment)
            return
        overlap = min(len(target), len(fragment), overlap_samples)
        start = len(target) - overlap
        for index in range(overlap):
            ratio = (index + 1) / (overlap + 1)
            mixed = int(target[start + index] * (1.0 - ratio) + fragment[index] * ratio)
            target[start + index] = max(-32768, min(32767, mixed))
        target.extend(fragment[overlap:])

    def synthesize(self, text: str, *, timeout_seconds: float = 25.0, max_audio_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
        if not self.status().get("ready"):
            raise TetoConfigurationError(str(self.status().get("last_error") or "Teto indisponível"))
        clean_text = " ".join(str(text or "").strip().split())
        max_chars = max(16, self._env_int("PHONE_WORKER_TETO_MAX_CHARACTERS", 180))
        if not clean_text:
            raise ValueError("texto vazio")
        if len(clean_text) > max_chars:
            raise ValueError(f"texto grande demais para Teto ({len(clean_text)} > {max_chars})")
        self._check_resources()
        if not self._lock.acquire(blocking=False):
            raise TetoResourceError("renderer Teto ocupado")

        started = time.monotonic()
        try:
            index = self._load_index()
            max_moras = max(8, self._env_int("PHONE_WORKER_TETO_MAX_PHONEMES", 240))
            notes = build_notes(phonemize(clean_text, max_moras=max_moras), base_pitch=str(os.getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4"))
            if not notes:
                raise TetoSynthesisError("texto não gerou fonemas compatíveis")
            deadline = started + max(2.0, float(timeout_seconds))
            combined = array.array("h")
            missing: list[str] = []
            rendered = 0
            with tempfile.TemporaryDirectory(prefix="phone-worker-teto-") as temp:
                workdir = Path(temp)
                for note in notes:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("renderização Teto excedeu o timeout")
                    entry = index.resolve(note.candidates)
                    if entry is None:
                        missing.append(note.candidates[0] if note.candidates else "?")
                        pause = int(self.SAMPLE_RATE * min(note.duration_ms, 160) / 1000)
                        combined.extend([0] * pause)
                        continue
                    fragment_path = self._resample_note(index=index, entry=entry, note=note, workdir=workdir, deadline=deadline)
                    fragment = self._read_samples(fragment_path)
                    overlap_ms = max(8.0, min(45.0, entry.overlap_ms or 16.0))
                    self._append_crossfade(combined, fragment, int(self.SAMPLE_RATE * overlap_ms / 1000.0))
                    if note.pause_after_ms:
                        combined.extend([0] * int(self.SAMPLE_RATE * note.pause_after_ms / 1000.0))
                    rendered += 1

                if rendered <= 0:
                    raise TetoSynthesisError("nenhum alias da voicebank correspondeu ao texto")
                max_seconds = max(2, self._env_int("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", 20))
                max_samples = self.SAMPLE_RATE * max_seconds
                if len(combined) > max_samples:
                    raise TetoSynthesisError(f"áudio Teto excedeu {max_seconds}s")
                peak = max((abs(sample) for sample in combined), default=0)
                if peak > 0:
                    scale = min(1.8, 30000.0 / peak)
                    if abs(scale - 1.0) > 0.01:
                        for index_sample, sample in enumerate(combined):
                            combined[index_sample] = max(-32768, min(32767, int(sample * scale)))

                output = workdir / "teto.wav"
                with wave.open(str(output), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(self.SAMPLE_RATE)
                    data = array.array("h", combined)
                    if os.sys.byteorder != "little":
                        data.byteswap()
                    wav.writeframes(data.tobytes())
                raw = output.read_bytes()
            if not raw or len(raw) > max_audio_bytes:
                raise TetoSynthesisError(f"áudio Teto inválido ou grande demais ({len(raw)} bytes)")
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return {
                "audio": raw,
                "audio_format": "wav",
                "voicebank": index.name,
                "voicebank_fingerprint": index.fingerprint,
                "aliases": index.alias_count,
                "rendered_phonemes": rendered,
                "missing_phonemes": missing[:12],
                "worker_synth_ms": round(elapsed_ms, 2),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            self._lock.release()
