"""Bounded streaming transports used by the worker and its music agent.

This module has no provider imports or running threads until the first job.
Each response owns a cancellation token and one bounded compressed-audio queue.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import os
import queue
import re
import threading
import time
import urllib.request

PROTOCOL_VERSION = 2
_AUDIO_LINE = re.compile(r'jQ1olc","\[\\"(.*?)\\"\]')
_LOCAL = threading.local()
_LOCK = threading.Lock()
_LOOP = None
_POOL = None
_SLOTS = {}
_END = object()


def _resources(engine):
    global _LOOP, _POOL
    with _LOCK:
        if engine not in _SLOTS:
            try:
                count = int(os.getenv('PHONE_WORKER_GTTS_CONCURRENCY' if engine == 'gtts' else 'PHONE_WORKER_TTS_AGENT_CONCURRENCY', '2'))
            except ValueError:
                count = 2
            _SLOTS[engine] = threading.BoundedSemaphore(max(1, min(4, count)))
        if engine == 'gtts' and _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='tts-http')
        if engine == 'edge' and _LOOP is None:
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            def run():
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()
            threading.Thread(target=run, daemon=True, name='tts-edge-loop').start()
            ready.wait(timeout=2.0)
            _LOOP = loop
        return _SLOTS[engine], _LOOP, _POOL


def _session():
    import requests
    now = time.monotonic()
    state = getattr(_LOCAL, 'http', None)
    if state is None or now - state['used_at'] >= 90 or now - state['created_at'] >= 600 or state['uses'] >= 256:
        if state is not None:
            state['session'].close()
        state = {'session': requests.Session(), 'created_at': now, 'used_at': now,
                 'uses': 0, 'proxies': urllib.request.getproxies()}
        _LOCAL.http = state
    state['used_at'] = now
    state['uses'] += 1
    return state


def _invalidate_session():
    state = getattr(_LOCAL, 'http', None)
    if state is not None:
        with contextlib.suppress(Exception):
            state['session'].close()
    _LOCAL.http = None


class AudioStream:
    def __init__(self, *, engine, text, voice='pt-BR-FranciscaNeural', language='pt',
                 rate='+0%', pitch='+0Hz', tld='com', timeout=20.0,
                 max_bytes=8 * 1024 * 1024):
        if engine not in {'edge', 'gtts'}:
            raise ValueError('engine não suporta transporte progressivo')
        self.engine = engine
        self.text, self.voice, self.language = text, voice, language
        self.rate, self.pitch, self.tld = rate, pitch, tld
        self.deadline = time.monotonic() + max(.5, float(timeout))
        self.max_bytes = max_bytes
        self.size = 0
        self.stop = threading.Event()
        self.queue = queue.Queue(maxsize=8)
        self.error = None
        self.done = threading.Event()
        self._task = None
        self._loop = None
        slots, loop, pool = _resources(engine)
        if not slots.acquire(blocking=False):
            raise RuntimeError('transporte TTS ocupado')
        self._slots = slots
        self._loop = loop
        try:
            if engine == 'edge':
                self.future = asyncio.run_coroutine_threadsafe(self._edge(), loop)
            else:
                self.future = pool.submit(self._gtts)
        except BaseException:
            slots.release()
            raise
        # A cancelled HTTP consumer does not release a running provider slot.
        self.future.add_done_callback(lambda future: slots.release() if future.cancelled() and engine == "gtts" else None)

    def _check(self):
        if self.stop.is_set():
            raise RuntimeError('TTS cancelado')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('prazo total do TTS esgotado')

    def _put(self, data):
        for offset in range(0, len(data), 16384):
            chunk = data[offset:offset + 16384]
            self._check()
            if self.size + len(chunk) > self.max_bytes:
                raise ValueError('áudio TTS excedeu limite de bytes')
            while True:
                self._check()
                try:
                    self.queue.put(chunk, timeout=.1)
                    self.size += len(chunk)
                    break
                except queue.Full:
                    continue

    async def _edge(self):
        self._task = asyncio.current_task()
        try:
            self._check()
            import edge_tts
            remaining = max(1.0, self.deadline - time.monotonic())
            communicate = edge_tts.Communicate(text=self.text, voice=self.voice, rate=self.rate, pitch=self.pitch,
                                              connect_timeout=min(4, int(remaining)),
                                              receive_timeout=min(15, int(remaining)))
            async with asyncio.timeout(remaining):
                async for message in communicate.stream():
                    self._check()
                    if message.get('type') == 'audio' and message.get('data'):
                        await asyncio.to_thread(self._put, message['data'])
        except BaseException as error:
            self.error = error
        finally:
            await asyncio.to_thread(self._finish)

    def _gtts(self):
        try:
            import requests
            from gtts import gTTS
            self._check()
            language = str(self.language or 'pt').strip().lower().replace('_', '-')
            if language == 'pt-br':
                language = 'pt'
            tts = gTTS(text=self.text, lang=language, tld=self.tld, timeout=(3.5, 8))
            prepare = getattr(tts, '_prepare_requests', None)
            if not callable(prepare):
                for data in tts.stream():
                    self._put(data)
                return
            for request in prepare():
                response = None
                for attempt in range(2):
                    self._check()
                    state = _session()
                    remaining = max(.05, self.deadline - time.monotonic())
                    try:
                        response = state['session'].send(request, proxies=state['proxies'],
                            verify=False, stream=True, timeout=(min(3.5, remaining), min(8.0, remaining)))
                        break
                    except requests.exceptions.RequestException:
                        _invalidate_session()
                        self._check()
                        if attempt:
                            raise
                found = False
                try:
                    response.raise_for_status()
                    for line in response.iter_lines(chunk_size=1024):
                        self._check()
                        if b'jQ1olc' not in line:
                            continue
                        match = _AUDIO_LINE.search(line.decode('utf-8'))
                        if match is not None:
                            data = base64.b64decode(match.group(1))
                            if data:
                                found = True
                                self._put(data)
                finally:
                    response.close()
                if not found:
                    raise RuntimeError('gTTS não retornou áudio')
        except BaseException as error:
            self.error = error
        finally:
            self._finish()

    def _finish(self):
        self.done.set()
        self._slots.release()
        while not self.stop.is_set():
            try:
                self.queue.put(_END, timeout=.1)
                return
            except queue.Full:
                if time.monotonic() >= self.deadline:
                    return

    def __iter__(self):
        return self

    def __next__(self):
        self._check()
        try:
            chunk = self.queue.get(timeout=max(.001, self.deadline - time.monotonic()))
        except queue.Empty:
            self._check()
            raise TimeoutError('TTS não concluiu o transporte')
        if chunk is _END:
            if self.error is not None:
                raise self.error
            raise StopIteration
        return chunk

    def close(self):
        self.stop.set()
        if self.engine == 'edge':
            if self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        else:
            self.future.cancel()
        with contextlib.suppress(queue.Full):
            self.queue.put_nowait(_END)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def synthesize_bytes(**kwargs):
    with AudioStream(**kwargs) as stream:
        return b''.join(stream)



def serve_stream(handler, body, api):
    """Authenticated protocol v2. Never restart a provider after audio headers."""
    import hashlib
    import itertools
    import tempfile
    from pathlib import Path
    roles, capabilities = handler._ensure_tts_piper_turbo_allowed()
    if not api._env_bool('PHONE_WORKER_TTS_AGENT_ENABLED', True):
        raise RuntimeError('TTS Agent desativado')
    text = str(body.get('text') or '').strip()
    if not text or len(text) > max(64, api._env_int('PHONE_WORKER_TTS_AGENT_MAX_TEXT_LENGTH', 1200)):
        raise ValueError('tamanho de texto inválido')
    engine = str(body.get('engine') or 'gtts').strip().lower()
    if engine not in {'edge', 'gtts'}:
        raise ValueError('streaming v2 aceita Edge ou gTTS')
    timeout = max(2, min(handler.job_timeout, float(body.get('timeout_seconds') or handler.job_timeout)))
    limit = max(1024, min(handler.max_output_bytes, int(body.get('max_audio_bytes') or handler.max_output_bytes)))
    started = time.monotonic()
    api._tts_agent_record_start()  # atomic check and reservation
    source = None
    cached = None
    staging = None
    target = None
    headers_sent = False
    ok = False
    error = ''
    size = 0
    try:
        key = ''
        if handler._tts_agent_standard_cache_enabled(roles, capabilities):
            key = handler._tts_agent_standard_cache_key(body, engine=engine)
        if key and handler._tts_agent_cache_mode_allows_read(body):
            path, fmt = handler._find_tts_cache_file(key)
            if path is not None and fmt == 'mp3':
                try:
                    cached = path.open('rb')
                    import fcntl
                    fcntl.flock(cached.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    if not 0 < os.fstat(cached.fileno()).st_size <= limit:
                        raise ValueError('cache fora do limite')
                    handler._touch_tts_cache_file(path)
                except (OSError, ValueError):
                    if cached is not None:
                        cached.close()
                    cached = None
        if cached is not None:
            chunks = iter(lambda: cached.read(16384), b'')
        else:
            source = AudioStream(engine=engine, text=text,
                voice=str(body.get('voice') or body.get('fallback_voice') or 'pt-BR-FranciscaNeural'),
                language=handler._normalize_tts_gtts_language(body.get('language') or body.get('fallback_language')),
                rate=handler._normalize_tts_edge_rate(body.get('rate')),
                pitch=handler._normalize_tts_edge_pitch(body.get('pitch')),
                tld=str(body.get('tld') or 'com'), timeout=timeout, max_bytes=limit)
            chunks = iter(source)
            if key and handler._tts_agent_cache_mode_allows_store(body):
                target = handler._tts_cache_path(key, 'mp3')
                with contextlib.suppress(OSError):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    staging = tempfile.NamedTemporaryFile(prefix=target.name + '.', suffix='.part', dir=target.parent, delete=False)
        first = next(chunks, b'')
        if not first:
            raise RuntimeError('engine não enviou áudio')
        # A chunked terminator is sent ONLY on complete provider success. aiohttp
        # rejects a disconnected partial response, so it can never enter cache.
        handler.connection.settimeout(min(10, timeout))
        handler.protocol_version = 'HTTP/1.1'
        handler.close_connection = True
        handler.send_response(200)
        for name, value in {
            'Content-Type': 'audio/mpeg', 'Transfer-Encoding': 'chunked',
            'Connection': 'close', 'Cache-Control': 'no-store',
            'X-Core-Worker-Stream-Protocol': '2', 'X-Core-Worker-Audio-Format': 'mp3',
            'X-Core-Worker-Engine': engine,
            'X-Core-Worker-Cache-Hit': 'true' if cached else 'false',
            'X-Core-Worker-First-Audio-Ms': str(round((time.monotonic() - started) * 1000, 2)),
            'X-Core-Worker-Id': api._header_ascii(os.getenv('CORE_WORKER_ID') or os.getenv('CORE_WORKER_WORKER_ID') or api._default_worker_id()),
            'X-Core-Worker-Version': api.PHONE_WORKER_VERSION,
        }.items():
            handler.send_header(name, value)
        handler.end_headers()
        headers_sent = True
        for chunk in itertools.chain((first,), chunks):
            if time.monotonic() - started >= timeout or size + len(chunk) > limit:
                raise TimeoutError('limite do stream TTS atingido')
            size += len(chunk)
            handler.wfile.write(('%x\r\n' % len(chunk)).encode('ascii') + chunk + b'\r\n')
            handler.wfile.flush()
            if staging is not None:
                try:
                    staging.write(chunk)
                except OSError:
                    staging.close()
                    Path(staging.name).unlink(missing_ok=True)
                    staging = None
        handler.wfile.write(b'0\r\n\r\n')
        handler.wfile.flush()
        ok = True
        if staging is not None:
            staging.close()
            os.replace(staging.name, target)
            handler._schedule_tts_cache_prune(protected=target)
    except Exception as exc:
        error = str(exc)
        if not headers_sent:
            raise
        handler.close_connection = True
    finally:
        if source is not None:
            source.close()
        if cached is not None:
            cached.close()
        if staging is not None:
            staging.close()
            with contextlib.suppress(OSError):
                Path(staging.name).unlink()
        api._tts_agent_record_done(ok=ok, engine=engine, elapsed_ms=(time.monotonic() - started) * 1000, error=error)


def store_binary_cache(handler, api):
    import hashlib
    import tempfile
    from pathlib import Path
    handler._ensure_tts_cache_allowed()
    key = handler._sanitize_tts_cache_key(handler.headers.get('X-Core-Cache-Key'))
    fmt = handler._normalize_tts_cache_format(handler.headers.get('X-Core-Audio-Format'))
    expected = str(handler.headers.get('X-Core-Sha256') or '').strip().lower()
    length = int(handler.headers.get('Content-Length') or '0')
    if handler.headers.get('Transfer-Encoding') or not 0 < length <= min(handler.max_body_bytes, handler.max_output_bytes):
        raise ValueError('tamanho do cache binário inválido')
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('sha256 obrigatório para cache binário')
    target = handler._tts_cache_path(key, fmt)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler.connection.settimeout(10)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=target.name + '.', suffix='.part', dir=target.parent, delete=False) as output:
            temporary = output.name
            digest = hashlib.sha256()
            remaining = length
            deadline = time.monotonic() + 20
            while remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError('cache upload excedeu prazo')
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    raise ValueError('cache upload incompleto')
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        if digest.hexdigest() != expected:
            raise ValueError('sha256 do cache não confere')
        os.replace(temporary, target)
        handler._schedule_tts_cache_prune(protected=target)
        api._json_response(handler, 200, {'ok': True, 'cache_stored': True, 'size': length})
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                Path(temporary).unlink()


class AudioReader(__import__('io').RawIOBase):
    def __init__(self, stream, on_complete=None):
        super().__init__()
        self.stream = stream
        self.on_complete = on_complete
        self.cache = bytearray() if on_complete is not None else None
        self.pending = b''
        self.error = None
        self.eof = False

    def readable(self):
        return True

    def read(self, size=-1):
        if self.closed or self.eof or size == 0:
            return b''
        try:
            chunk = self.pending or next(self.stream)
            self.pending = b''
        except StopIteration:
            self.eof = True
            if self.cache and self.on_complete is not None:
                with contextlib.suppress(Exception):
                    self.on_complete(bytes(self.cache))
            self.cache = None
            return b''
        except BaseException as error:
            self.error = error
            self.cache = None
            self.eof = True
            return b''
        if size >= 0 and len(chunk) > size:
            chunk, self.pending = chunk[:size], chunk[size:]
        if self.cache is not None:
            if len(self.cache) + len(chunk) <= 2 * 1024 * 1024:
                self.cache.extend(chunk)
            else:
                self.cache = None
        return chunk

    def close(self):
        if not self.closed:
            self.stream.close()
            self.cache = None
            super().close()
