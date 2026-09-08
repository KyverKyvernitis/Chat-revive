#!/usr/bin/env bash
set -Eeuo pipefail
BOT_DIR="${BOT_DIR:-/home/ubuntu/bot}"
export BOT_DIR
exec python3 - <<'PY_CLEANUP'
import contextlib
import fcntl
import os
import stat
import time
from pathlib import Path

bot = Path(os.environ['BOT_DIR'])
root = Path(os.getenv('AUDIO_TMP_DIR', str(bot / 'tmp_audio')))
runtime = Path(os.getenv('RUNTIME_DIR', str(root / 'runtime')))
cache = Path(os.getenv('CACHE_DIR', str(root / 'cache')))
credentials = Path(os.getenv('CREDENTIALS_DIR', str(root / 'credentials')))
log_path = Path(os.getenv('LOG_FILE', str(bot / 'cleanup-audio-temp.log')))
def number(name, default):
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default
for directory in (root, runtime, cache, credentials):
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
now = time.time()
grace = max(180, number('TTS_CLEANUP_ACTIVE_GRACE_SECONDS', 180))
normal_limit = number('MAX_BYTES', number('TTS_TEMP_MAX_BYTES', number('TTS_TEMP_MAX_MB', 256) * 1024 * 1024))
cache_limit = number('CACHE_MAX_BYTES', normal_limit)
piper_limit = number('TTS_PIPER_VPS_CACHE_MAX_BYTES', number('TTS_PIPER_VPS_CACHE_MAX_MB', 2048) * 1024 * 1024)
normal_count = number('TTS_TEMP_MAX_FILES', 256)
piper_count = number('TTS_PIPER_VPS_CACHE_SIZE', 2048)
ages = {runtime: number('RUNTIME_MAX_AGE_MINUTES', 360) * 60,
        cache: number('CACHE_MAX_AGE_MINUTES', 10080) * 60}
rows = []
seen = set()
# Only audio and abandoned partial files. Credentials and voice catalog are not audio.
for directory in (runtime, cache, root):
    with contextlib.suppress(OSError):
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path in seen or not entry.is_file(follow_symlinks=False):
                    continue
                if path.suffix.lower() not in {'.mp3', '.wav', '.ogg', '.opus', '.part', '.tmp'} and '.tmp-' not in path.name:
                    continue
                seen.add(path)
                with contextlib.suppress(OSError):
                    st = entry.stat(follow_symlinks=False)
                    rows.append((st.st_mtime, st.st_size, path, path.name.startswith('piper_'), directory == cache))
rows.sort(key=lambda row: row[0])
deleted = set()
def remove(row):
    mtime, size, path, _, _ = row
    if now - mtime < grace:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            opened, current = os.fstat(handle.fileno()), path.stat()
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                return False
            if now - current.st_mtime < grace:
                return False
            path.unlink()
            deleted.add(path)
            return True
    except OSError:
        return False
for row in rows:
    mtime, _, path, _, _ = row
    if now - mtime > ages.get(path.parent, ages[runtime]):
        remove(row)
for is_piper, limit, count_limit in ((False, normal_limit, normal_count), (True, piper_limit, piper_count)):
    group = [row for row in rows if row[3] == is_piper and row[2] not in deleted]
    total = sum(row[1] for row in group)
    cached = sum(row[1] for row in group if row[4])
    count = len(group)
    for row in group:
        if total <= limit and count <= count_limit and (is_piper or cached <= cache_limit):
            break
        if total <= limit and count <= count_limit and not row[4]:
            continue
        if remove(row):
            total -= row[1]
            cached -= row[1] if row[4] else 0
            count -= 1
if deleted:
    with contextlib.suppress(OSError):
        if log_path.exists() and log_path.stat().st_size > number('LOG_MAX_BYTES', 262144):
            os.replace(log_path, str(log_path) + '.1')
        with log_path.open('a') as log:
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] audio: {len(deleted)} arquivos removidos\n")
PY_CLEANUP
