"""Optional bounded cache of complete 20 ms Opus packets for frequent phrases."""
from collections import OrderedDict
import asyncio
import contextlib
import os
import threading
import time
import discord
from .runtime import await_physical_completion

class OpusFramesSource(getattr(discord, 'AudioSource', object)):
    def __init__(self, frames):
        self.frames = frames
        self.index = 0
    def read(self):
        if self.index >= len(self.frames):
            return b''
        frame = self.frames[self.index]
        self.index += 1
        return frame
    def is_opus(self):
        return True
    def cleanup(self):
        self.frames = ()

class PreparedOpusCache:
    def __init__(self, *, max_bytes=8 * 1024 * 1024, max_entries=128, ttl=600):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self.entries = OrderedDict()
        self.total = 0
        self.hits = OrderedDict()
        self.pending = set()
        self.busy = False

    def key(self, path, options):
        st = os.stat(path)
        return (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, options)

    def get(self, key):
        value = self.entries.get(key)
        if value is None:
            return None
        created, size, frames = value
        if time.monotonic() - created > self.ttl:
            self.entries.pop(key)
            self.total -= size
            return None
        self.entries.move_to_end(key)
        return OpusFramesSource(frames)

    def put(self, key, frames):
        frames = tuple(frames)
        size = sum(len(frame) for frame in frames)
        if not frames or size > min(512 * 1024, self.max_bytes):
            return
        old = self.entries.pop(key, None)
        if old:
            self.total -= old[1]
        while self.entries and (self.total + size > self.max_bytes or len(self.entries) >= self.max_entries):
            _, (_, removed, _) = self.entries.popitem(last=False)
            self.total -= removed
        self.entries[key] = (time.monotonic(), size, frames)
        self.total += size

    def repeated(self, key):
        self.hits[key] = self.hits.get(key, 0) + 1
        self.hits.move_to_end(key)
        while len(self.hits) > 256:
            self.hits.popitem(last=False)
        return self.hits[key] >= 2

    async def prepare(self, key, path, options, *, idle):
        if self.busy or key in self.pending:
            return
        self.pending.add(key)
        self.busy = True
        stop = threading.Event()
        holder = []
        future = None
        try:
            for _ in range(15):
                await asyncio.sleep(.2)
                if idle():
                    break
            else:
                return
            def encode():
                import fcntl
                with open(path, 'rb') as audio_file:
                    fcntl.flock(audio_file, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    if self.key(path, options) != key or stop.is_set():
                        return None
                    source = discord.FFmpegOpusAudio(path, before_options='-nostdin', options=options,
                                                    bitrate=64, codec='libopus')
                    holder.append(source)
                    frames, size = [], 0
                    try:
                        while not stop.is_set():
                            packet = source.read()
                            if not packet:
                                process = getattr(source, '_process', None)
                                if process is not None and process.wait(timeout=.5) != 0:
                                    return None
                                return frames
                            size += len(packet)
                            if size > 512 * 1024 or len(frames) >= 2000:
                                return None
                            frames.append(packet)
                    finally:
                        source.cleanup()
                return None
            future = asyncio.create_task(asyncio.to_thread(encode))
            frames = await asyncio.wait_for(asyncio.shield(future), timeout=3)
            if frames and self.key(path, options) == key:
                self.put(key, frames)
        except (OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            stop.set()
            if holder:
                with contextlib.suppress(Exception):
                    holder[0].cleanup()
            if future is not None and not future.done():
                with contextlib.suppress(BaseException):
                    await await_physical_completion(future)
            self.pending.discard(key)
            self.busy = False
