"""Bounded primitives shared by file and progressive TTS consumers.

All job and buffer state belongs to the asyncio loop. Blocking file operations
run in a thread; readers have independent offsets and never share a FIFO.
"""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None


_SENTENCE_END = re.compile(r"[.!?;:]\s+")


def split_text(text: str, *, escaped_utf8: bool, limit: int, max_parts: int) -> list[str]:
    """Split once, respecting both the encoded budget and a hard part count."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    sizes = [0]
    escaped = {"&": 5, "<": 4, ">": 4}
    for char in text:
        size = escaped.get(char, len(char.encode("utf-8"))) if escaped_utf8 else 1
        sizes.append(sizes[-1] + size)
    if sizes[-1] <= limit:
        return [text]
    boundaries = [m.start() + 1 for m in _SENTENCE_END.finditer(text)]
    chunks: list[str] = []
    start = 0
    max_parts = max(1, int(max_parts))
    while start < len(text) and len(chunks) < max_parts:
        cut = bisect.bisect_right(sizes, sizes[start] + limit) - 1
        cut = min(len(text), cut)
        truncated = len(chunks) == max_parts - 1 and cut < len(text)
        if truncated:
            reserve = 3 if escaped_utf8 else 1
            cut = bisect.bisect_right(sizes, sizes[start] + limit - reserve) - 1
        elif cut < len(text):
            sentence = bisect.bisect_right(boundaries, cut) - 1
            boundary = boundaries[sentence] if sentence >= 0 else -1
            word = text.rfind(" ", start, cut + 1)
            minimum = start + max(1, (cut - start) // 3)
            if boundary >= minimum:
                cut = boundary
            elif word >= minimum:
                cut = word
        if cut <= start:
            # Configured production budgets are >=160; still reject impossible
            # budgets instead of emitting a character larger than the limit.
            break
        piece = text[start:cut].rstrip()
        if piece:
            chunks.append(piece + ("…" if truncated else ""))
        if truncated:
            break
        start = cut
        while start < len(text) and text[start].isspace():
            start += 1
    return chunks


async def await_physical_completion(task):
    """Preserve ownership of blocking I/O, even if the caller cancels twice."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve a failure without masking cancellation.
        raise


class MemoryBudget:
    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.used = 0

    def reserve(self, size: int) -> bool:
        if self.used + size > self.limit:
            return False
        self.used += size
        return True

    def release(self, size: int) -> None:
        self.used = max(0, self.used - size)


class ReplayBuffer:
    """Append-only compressed audio with bounded RAM and disk spill.

Slow readers retain their own cursor without blocking the network producer.
The owner closes the buffer only after its producer and consumers have ended.
"""
    def __init__(self, directory: str, budget: MemoryBudget, *, memory_limit: int, max_bytes: int):
        self.directory = directory
        self.budget = budget
        self.memory_limit = memory_limit
        self.max_bytes = max_bytes
        self.size = 0
        self._memory = bytearray()
        self._file = None
        self._io_lock = threading.Lock()
        self.condition = asyncio.Condition()
        self.ended = False
        self.error: BaseException | None = None
        self.closed = False

    def _spill(self) -> None:
        with self._io_lock:
            self._file = tempfile.TemporaryFile(prefix="tts-spool-", dir=self.directory)
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_SH)
            self._file.write(self._memory)
            self._file.flush()

    async def _io(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        return await await_physical_completion(task)

    def _write(self, data: bytes) -> None:
        with self._io_lock:
            self._file.seek(0, os.SEEK_END)
            self._file.write(data)
            self._file.flush()

    async def append(self, data: bytes) -> None:
        if not data or self.closed or self.ended:
            return
        if self.size + len(data) > self.max_bytes:
            raise ValueError("áudio TTS excedeu o orçamento de bytes")
        if self._file is None:
            if len(self._memory) + len(data) <= self.memory_limit and self.budget.reserve(len(data)):
                self._memory.extend(data)
            else:
                await self._io(self._spill)
                self.budget.release(len(self._memory))
                self._memory.clear()
                await self._io(self._write, data)
        else:
            await self._io(self._write, data)
        async with self.condition:
            self.size += len(data)
            self.condition.notify_all()

    async def finish(self, error: BaseException | None = None) -> None:
        async with self.condition:
            self.error = error
            self.ended = True
            self.condition.notify_all()

    async def wait_for_bytes(self, minimum: int = 1) -> None:
        async with self.condition:
            await self.condition.wait_for(lambda: self.size >= minimum or self.ended)
        if self.size == 0 or (self.ended and self.error is not None):
            if self.error is not None:
                raise self.error
            raise RuntimeError("engine não enviou áudio")

    def _read(self, offset: int, size: int) -> bytes:
        with self._io_lock:
            self._file.seek(offset)
            return self._file.read(size)

    async def chunks(self, *, chunk_size: int = 16384):
        offset = 0
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: offset < self.size or self.ended)
                available = min(chunk_size, self.size - offset)
                ended, error = self.ended, self.error
            if available:
                if self._file is None:
                    data = bytes(self._memory[offset:offset + available])
                else:
                    data = await self._io(self._read, offset, available)
                if not data:
                    raise RuntimeError("spool TTS incompleto")
                offset += len(data)
                yield data
            elif ended:
                if error is not None:
                    raise error
                return

    async def copy_to(self, path: str) -> None:
        if not self.ended or self.error is not None:
            raise RuntimeError("áudio incompleto não pode entrar no cache")
        def write() -> None:
            with open(path, "wb") as output:
                if self._file is None:
                    output.write(self._memory)
                else:
                    with self._io_lock:
                        self._file.seek(0)
                        shutil.copyfileobj(self._file, output, length=128 * 1024)
        await self._io(write)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.budget.release(len(self._memory))
        self._memory.clear()
        if self._file is not None:
            self._file.close()


@dataclass
class StreamJob:
    key: str
    item: Any
    buffer: ReplayBuffer
    started_at: float
    task: asyncio.Task | None = None
    cache_task: asyncio.Task | None = None
    references: int = 0
    store_in_cache: bool = False
    path: str = ""
    actual_engine: str = ""
    route: str = "local"
    first_audio_ms: float = 0.0
    slot_wait_ms: float = 0.0
    network_first_audio_ms: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    foreground: asyncio.Event = field(default_factory=asyncio.Event)
    slot_ready: asyncio.Event = field(default_factory=asyncio.Event)
    pending_io: set[asyncio.Task] = field(default_factory=set)
    cleanup_scheduled: bool = False


def unlink_if_unlocked(path: str) -> bool:
    """Delete only the inode checked and locked, never its newer replacement."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            return False
        os.remove(path)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class PathLeases:
    """References identify inodes, so replacing a cache entry cannot delete it
    when an older consumer releases the previous file at the same pathname.
    """
    def __init__(self):
        self.entries: dict[tuple[str, int, int], tuple[int, set[object]]] = {}
        self.by_owner: dict[object, set[tuple[str, int, int]]] = {}
        self.pending_delete: set[tuple[str, int, int]] = set()

    @property
    def protected_paths(self) -> set[str]:
        return {key[0] for key in self.entries}

    def acquire(self, path: str, owner: object) -> None:
        path = os.path.abspath(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        try:
            info = os.fstat(fd)
            key = (path, info.st_dev, info.st_ino)
            entry = self.entries.get(key)
            if entry is None:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                entry = (fd, set())
                self.entries[key] = entry
                fd = None
            entry[1].add(owner)
            self.by_owner.setdefault(owner, set()).add(key)
        finally:
            if fd is not None:
                os.close(fd)

    def release(self, owner: object) -> None:
        for key in self.by_owner.pop(owner, ()):
            entry = self.entries.get(key)
            if entry is None:
                continue
            fd, owners = entry
            owners.discard(owner)
            if owners:
                continue
            self.entries.pop(key, None)
            os.close(fd)
            if key in self.pending_delete:
                self.pending_delete.discard(key)
                try:
                    current = os.stat(key[0])
                    if (current.st_dev, current.st_ino) == key[1:]:
                        unlink_if_unlocked(key[0])
                except OSError:
                    pass

    def remove(self, path: str) -> None:
        path = os.path.abspath(path)
        try:
            info = os.stat(path)
        except OSError:
            return
        key = (path, info.st_dev, info.st_ino)
        if key in self.entries:
            self.pending_delete.add(key)
        else:
            unlink_if_unlocked(path)

    def close(self) -> None:
        for owner in list(self.by_owner):
            self.release(owner)


async def wait_writable(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    def wake() -> None:
        if not ready.done():
            ready.set_result(None)
    try:
        loop.add_writer(fd, wake)
    except (AttributeError, NotImplementedError):
        await asyncio.sleep(0.002)
        return
    try:
        await ready
    finally:
        loop.remove_writer(fd)
