from __future__ import annotations

import contextlib
import hashlib
import os
import time
from pathlib import Path


class FragmentCache:
    def __init__(self, root: str | Path, *, max_mb: int = 256):
        self.root = Path(root).expanduser()
        self.max_bytes = max(8, int(max_mb)) * 1024 * 1024
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

    def path_for(self, key: str) -> Path:
        safe = "".join(ch for ch in str(key or "").lower() if ch in "0123456789abcdef")[:64]
        if len(safe) < 16:
            safe = self.key(str(key or ""))
        return self.root / safe[:2] / f"{safe}.wav"

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        try:
            if path.is_file() and path.stat().st_size > 44:
                os.utime(path, None)
                return path
        except OSError:
            return None
        return None

    def put(self, key: str, source: Path) -> Path:
        target = self.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        data = source.read_bytes()
        temporary.write_bytes(data)
        temporary.replace(target)
        self.prune()
        return target

    def prune(self) -> None:
        try:
            files = [path for path in self.root.rglob("*.wav") if path.is_file()]
            total = sum(path.stat().st_size for path in files)
        except OSError:
            return
        if total <= self.max_bytes:
            return
        files.sort(key=lambda path: path.stat().st_mtime)
        target = int(self.max_bytes * 0.85)
        for path in files:
            if total <= target:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue
        for directory in sorted(self.root.rglob("*"), reverse=True):
            if directory.is_dir():
                with contextlib.suppress(OSError):
                    directory.rmdir()
