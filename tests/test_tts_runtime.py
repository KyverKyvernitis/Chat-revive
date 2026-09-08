from __future__ import annotations

import asyncio
import contextlib
import html
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_tts_helpers import tts_audio, QueueItem, GuildTTSState
from cogs.tts.runtime import MemoryBudget, PathLeases, ReplayBuffer, split_text, unlink_if_unlocked


class Probe(tts_audio.TTSAudioMixin):
    def __init__(self):
        self.guild_states = {}
        self.edge_voice_names = {'pt-BR-FranciscaNeural', 'pt-BR-AntonioNeural'}
        self.bot = SimpleNamespace(audio_router=None, get_guild=lambda _: None)

    def _get_db(self):
        return None

    def _schedule_worker_turbo_cache_store(self, *args):
        return None

    def _schedule_cache_maintenance(self, *args, **kwargs):
        return None


def item(text='olá!', guild=1):
    return QueueItem(guild_id=guild, channel_id=10 + guild, author_id=1,
                     text=text, engine='edge', voice='pt-BR-FranciscaNeural',
                     language='pt', rate='+0%', pitch='+0Hz')


class RuntimePrimitiveTests(unittest.IsolatedAsyncioTestCase):
    def test_text_and_encoded_limits_include_truncation_marker(self):
        for edge, text, limit in ((False, 'a' * 4000, 420),
                (True, ('á<&你. ' * 2000), 3000)):
            parts = split_text(text, escaped_utf8=edge, limit=limit, max_parts=8)
            self.assertLessEqual(len(parts), 8)
            for part in parts:
                size = len(html.escape(part, quote=False).encode()) if edge else len(part)
                self.assertLessEqual(size, limit)
        complete = ' '.join(['a' * 419 + '.'] * 8)
        parts = split_text(complete, escaped_utf8=False, limit=420, max_parts=8)
        self.assertEqual(' '.join(parts), complete)
        self.assertFalse(parts[-1].endswith('…'))

    async def test_replay_spills_and_readers_keep_independent_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = MemoryBudget(64)
            buffer = ReplayBuffer(directory, budget, memory_limit=64, max_bytes=4096)
            await buffer.append(b'A' * 40)
            first = buffer.chunks(chunk_size=20)
            self.assertEqual(await anext(first), b'A' * 20)
            await buffer.append(b'B' * 200)
            self.assertLessEqual(budget.used, 64)
            await buffer.finish()
            rest = b''.join([chunk async for chunk in first])
            second = b''.join([chunk async for chunk in buffer.chunks(chunk_size=31)])
            self.assertEqual(rest, b'A' * 20 + b'B' * 200)
            self.assertEqual(second, b'A' * 40 + b'B' * 200)
            buffer.close()
            self.assertEqual(budget.used, 0)

    def test_old_lease_cannot_delete_replacement_cache_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'entry.mp3'
            path.write_bytes(b'old')
            leases = PathLeases()
            leases.acquire(str(path), 'old-reader')
            self.assertFalse(unlink_if_unlocked(str(path)))
            leases.remove(str(path))
            replacement = Path(directory) / 'new.mp3'
            replacement.write_bytes(b'new')
            os.replace(replacement, path)
            leases.acquire(str(path), 'new-reader')
            leases.release('old-reader')
            self.assertEqual(path.read_bytes(), b'new')
            self.assertFalse(unlink_if_unlocked(str(path)))
            leases.release('new-reader')
            self.assertTrue(unlink_if_unlocked(str(path)))

    async def test_repeated_cancellation_waits_for_physical_spool_write(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = MemoryBudget(8)
            buffer = ReplayBuffer(directory, budget, memory_limit=8, max_bytes=1024)
            await buffer.append(b'first')
            started = asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()
            original = buffer._write
            def blocked_write(data):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError('test write was not released')
                original(data)
            buffer._write = blocked_write
            writing = asyncio.create_task(buffer.append(b'second'))
            try:
                await asyncio.wait_for(started.wait(), 1)
                writing.cancel()
                await asyncio.sleep(0)
                writing.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(writing.done())
                self.assertFalse(buffer._file.closed)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(writing, 1)
                buffer.close()
            self.assertEqual(budget.used, 0)

    async def test_duplicate_admission_does_not_evict_pending_speech(self):
        probe = Probe()
        state = GuildTTSState(queue=asyncio.Queue(maxsize=2))
        probe.guild_states[1] = state
        await probe._enqueue_tts_item(1, item('A'))
        await probe._enqueue_tts_item(1, item('B'))
        accepted, dropped, duplicate = await probe._enqueue_tts_item(1, item('B'))
        self.assertEqual((accepted, dropped, duplicate), (False, 0, True))
        self.assertEqual([entry.text for entry in state.queue._queue], ['A', 'B'])

    async def test_group_admission_is_all_or_nothing(self):
        probe = Probe()
        state = GuildTTSState(queue=asyncio.Queue(maxsize=2))
        probe.guild_states[1] = state
        await probe._enqueue_tts_item(1, item('existing'))
        accepted, _, _ = await probe._enqueue_tts_items(1, [item('A'), item('B'), item('C')])
        self.assertFalse(accepted)
        self.assertEqual([entry.text for entry in state.queue._queue], ['existing'])

    def test_cache_key_keeps_text_voice_and_tld_distinct(self):
        probe = Probe()
        self.assertNotEqual(probe._cache_key(item('Olá!')), probe._cache_key(item('olá!')))
        self.assertNotEqual(probe._cache_key(item('Olá!!')), probe._cache_key(item('Olá!')))
        first, second = item(), item()
        second.voice = 'pt-BR-AntonioNeural'
        self.assertNotEqual(probe._cache_key(first), probe._cache_key(second))
        first.engine = second.engine = 'gtts'
        first._cache_key_value = second._cache_key_value = None
        second.tld = 'com.br'
        self.assertNotEqual(probe._cache_key(first), probe._cache_key(second))

    def test_cancelled_gtts_does_not_start_a_second_http_attempt(self):
        stop = threading.Event()
        calls = []
        class Session:
            def send(self, **kwargs):
                calls.append(kwargs)
                stop.set()
                raise tts_audio.requests.exceptions.ConnectionError('simulated')
        probe = Probe()
        probe._get_gtts_thread_session = lambda: (Session(), {})
        tts = SimpleNamespace(stream=lambda: iter(()), _prepare_requests=lambda: ['request'],
                              _tts_stop_requested=stop, timeout=(3.5, 8))
        with self.assertRaises(TimeoutError):
            list(probe._iter_gtts_audio_chunks(tts))
        self.assertEqual(len(calls), 1)


class SharedSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(tts_audio, "TTS_FFMPEG_PRIME_ENABLED", False))
        self.patches.enter_context(patch.object(tts_audio, 'TTS_TEMP_DIR', str(directory)))
        self.patches.enter_context(patch.object(tts_audio, '_RUNTIME_DIR', str(directory / 'runtime')))
        self.patches.enter_context(patch.object(tts_audio, '_CACHE_DIR', str(directory / 'cache')))
        self.patches.enter_context(patch.object(tts_audio, '_TTS_REQUIRED_DIRS',
                                               (str(directory), str(directory / 'runtime'), str(directory / 'cache'))))
        tts_audio._ensure_tts_temp_dirs(force=True)
        self.probe = Probe()
        self.tail = asyncio.Event()
        self.calls = []
        outer = self
        class Engine:
            def __init__(self, **kwargs):
                outer.calls.append(kwargs)
            async def stream(self):
                yield {'type': 'audio', 'data': b'A' * 2048}
                await outer.tail.wait()
                yield {'type': 'audio', 'data': b'B' * 2048}
        self.patches.enter_context(patch.object(tts_audio.edge_tts, 'Communicate', Engine))

    async def asyncTearDown(self):
        self.tail.set()
        for handle in list(self.probe._get_edge_stream_handles().values()):
            await self.probe._finalize_edge_stream(handle, cancel=True)
        for job in list(self.probe._shared_synthesis_jobs().values()):
            if job.task:
                await asyncio.gather(job.task, return_exceptions=True)
        self.probe._audio_leases().close()
        self.probe._shutdown_tts_runtime()
        self.patches.close()
        self.temp.cleanup()

    def worker_response(self, *, status=200, error=None):
        class Content:
            async def read(self, count): return b'worker unavailable'[:count]
            async def iter_chunked(self, size):
                yield b'worker-first'
                if error is not None:
                    raise error
                yield b'worker-last'
        class Response:
            headers = {'X-Core-Worker-Stream-Protocol': '2',
                       'X-Core-Worker-Engine': 'edge',
                       'X-Core-Worker-Audio-Format': 'mp3'}
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
        response = Response()
        response.status = status
        class Session:
            def post(self, *args, **kwargs): return response
        async def session(): return Session()
        self.probe._get_phone_worker_http_session = session
        self.probe._phone_worker_tts_base_url = lambda: 'http://worker.test'
        self.probe._worker_stream_available_for = lambda _: True
        self.patches.enter_context(patch.object(tts_audio.aiohttp, 'ClientTimeout',
                                               lambda **kwargs: SimpleNamespace(**kwargs)))

    async def test_worker_missing_protocol_falls_back_only_before_audio(self):
        self.worker_response(status=404)
        self.tail.set()
        utterance = item()
        path, temporary = await self.probe._shared_job_file(self.probe._get_state(1),
                                                          utterance, store_in_cache=False)
        try:
            self.assertEqual(Path(path).read_bytes(), b'A' * 2048 + b'B' * 2048)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(utterance._tts_audio_route, 'local_fallback')
            self.assertEqual(self.probe._tts_agent_route_state()['stream_protocol'], 0)
            self.assertTrue(temporary)
        finally:
            self.probe._release_item_audio(utterance)

    async def test_partial_worker_audio_is_neither_replayed_locally_nor_cached(self):
        self.worker_response(error=RuntimeError('incomplete HTTP response'))
        job = self.probe._acquire_shared_job(self.probe._get_state(1), item(), store_in_cache=True)
        try:
            await asyncio.wait_for(job.task, 1)
            self.assertEqual(job.buffer.size, len(b'worker-first'))
            self.assertIsInstance(job.buffer.error, RuntimeError)
            self.assertEqual(self.calls, [])
            self.assertIsNone(job.cache_task)
            self.assertEqual(list((Path(self.temp.name) / 'cache').glob('*')), [])
        finally:
            self.probe._release_shared_job(job)

    async def test_two_streams_share_synthesis_but_not_fifo_or_cancellation(self):
        first = await self.probe._prepare_edge_stream(self.probe._get_state(1), item(guild=1), store_in_cache=False)
        second = await self.probe._prepare_edge_stream(self.probe._get_state(2), item(guild=2), store_in_cache=False)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(first.fifo_path, second.fifo_path)
        shared = second.shared_job
        await self.probe._finalize_edge_stream(first, cancel=True)
        self.assertFalse(shared.stop.is_set())
        def read():
            with open(second.fifo_path, 'rb') as source:
                return source.read()
        reading = asyncio.create_task(asyncio.to_thread(read))
        await self.probe._activate_edge_stream(second)
        self.probe._close_edge_stream_reader_anchor(second)
        self.tail.set()
        data = await asyncio.wait_for(reading, 2)
        self.assertEqual(data, b'A' * 2048 + b'B' * 2048)
        await self.probe._finalize_edge_stream(second)
        self.assertEqual(self.probe._tts_audio_memory_budget.used, 0)

    async def test_cancelled_file_waiter_keeps_other_waiter_alive(self):
        first_item, second_item = item(guild=1), item(guild=2)
        first = asyncio.create_task(self.probe._shared_job_file(self.probe._get_state(1), first_item, store_in_cache=False))
        second = asyncio.create_task(self.probe._shared_job_file(self.probe._get_state(2), second_item, store_in_cache=False))
        for _ in range(10):
            await asyncio.sleep(0)
            if self.calls:
                break
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.tail.set()
        path, temporary = await asyncio.wait_for(second, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(temporary)
        self.assertEqual(Path(path).read_bytes(), b'A' * 2048 + b'B' * 2048)
        self.probe._audio_leases().remove(path)
        self.probe._release_item_audio(second_item)
        self.assertFalse(Path(path).exists())


class RealDecoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ffmpeg_starts_before_first_network_bytes_and_decodes_before_eof(self):
        import shlex
        import shutil
        import subprocess
        if shutil.which('ffmpeg') is None:
            self.skipTest('ffmpeg required')
        mp3 = subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi',
            '-i','sine=frequency=440:duration=4','-b:a','64k','-f','mp3','pipe:1'], check=True, stdout=subprocess.PIPE).stdout
        provider = asyncio.Event()
        tail = asyncio.Event()
        decoder_started = asyncio.Event()
        sources = []
        class RealSource:
            def __init__(self, path, *, before_options='', options=''):
                self.process = subprocess.Popen(['ffmpeg', *shlex.split(before_options), '-i',path,
                    *shlex.split(options), '-f','s16le','-ar','48000','-ac','2','pipe:1'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                sources.append(self)
                decoder_started.set()
            def read(self): return self.process.stdout.read(3840)
            def is_opus(self): return False
            def cleanup(self):
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=2)
                self.process.stdout.close()
        class Edge:
            def __init__(self, **kwargs): pass
            async def stream(self):
                await provider.wait()
                yield {'type':'audio', 'data':mp3[:16384]}
                await tail.wait()
                yield {'type':'audio', 'data':mp3[16384:]}
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            for name,value in {'TTS_TEMP_DIR':tmp, '_RUNTIME_DIR':tmp+'/runtime', '_CACHE_DIR':tmp+'/cache',
                '_TTS_REQUIRED_DIRS':(tmp,tmp+'/runtime',tmp+'/cache'), 'TTS_FFMPEG_PRIME_ENABLED':True}.items():
                stack.enter_context(patch.object(tts_audio,name,value))
            stack.enter_context(patch.object(tts_audio.config,'TTS_FFMPEG_OVERLAP_ENABLED',True,create=True))
            stack.enter_context(patch.object(tts_audio.discord,'FFmpegPCMAudio',RealSource))
            stack.enter_context(patch.object(tts_audio.edge_tts,'Communicate',Edge))
            tts_audio._ensure_tts_temp_dirs(force=True)
            probe = Probe()
            utterance = item('Teste de decodificação antecipada')
            prepare = asyncio.create_task(probe._prepare_edge_stream(probe._get_state(1),utterance,store_in_cache=False))
            handle = None
            prepared = None
            try:
                await asyncio.wait_for(decoder_started.wait(), timeout=1)
                self.assertFalse(prepare.done())
                provider.set()
                handle = await asyncio.wait_for(prepare, timeout=1)
                audio = asyncio.get_running_loop().create_future()
                audio.set_result((handle.fifo_path, True))
                path, cleanup, prepared = await asyncio.wait_for(probe._resolve_and_prime_audio(audio,utterance), timeout=2)
                self.assertIsNotNone(prepared)
                self.assertFalse(tail.is_set())
                self.assertEqual(len(prepared.source.read()),3840)
                self.assertEqual(len(sources),1)
                self.assertEqual(probe._tts_early_decoders,0)
            finally:
                provider.set()
                tail.set()
                if prepared:
                    prepared.cleanup()
                if handle:
                    await probe._finalize_edge_stream(handle,cancel=True)
                if not prepare.done():
                    prepare.cancel()
                await asyncio.gather(prepare,return_exceptions=True)
                probe._shutdown_tts_runtime()
                for source in sources:
                    if source.process.poll() is None:
                        source.cleanup()
            self.assertTrue(all(source.process.poll() is not None for source in sources))


class ComparableRoutingTests(unittest.TestCase):
    def test_expired_different_sized_cached_and_total_samples_are_never_mixed(self):
        from cogs.tts.routing import RouteMeasurements
        history = RouteMeasurements(ttl=10)
        for _ in range(3):
            history.record('edge','curto','worker','total',1,now=100)
            history.record('edge','muito longo '*100,'worker','first_audio',1,now=100)
            history.record('edge','curto','worker','first_audio',1,cached=True,now=100)
            history.record('edge','curto','local','first_audio',500,now=100)
        self.assertIsNone(history.estimate('edge','curto','worker',now=101))
        self.assertEqual(history.estimate('edge','curto','local',now=101),500)
        self.assertIsNone(history.estimate('edge','curto','local',now=111))

    def test_prepared_opus_cache_is_bounded_and_each_playback_has_its_own_cursor(self):
        from cogs.tts.prepared import PreparedOpusCache
        cache = PreparedOpusCache(max_bytes=12,max_entries=2)
        cache.put('a',[b'aaa',b'bbb'])
        one,two=cache.get('a'),cache.get('a')
        self.assertEqual(one.read(),b'aaa')
        self.assertEqual(one.read(),b'bbb')
        self.assertEqual(two.read(),b'aaa')
        cache.put('b',[b'c'*6])
        cache.put('c',[b'd'*6])
        self.assertIsNone(cache.get('a'))
        self.assertEqual(two.read(),b'bbb')
        self.assertLessEqual(cache.total,12)


class ClearLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_finishes_worker_cancellation_before_accepting_new_generation(self):
        import ast
        import types
        source = (Path(__file__).resolve().parents[1] / 'cogs/tts/cog.py').read_text()
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_clear_queue_only')
        namespace = {'asyncio':asyncio,'contextlib':contextlib}
        exec('from __future__ import annotations\n' + ast.unparse(node),namespace)
        probe = Probe()
        probe._clear_queue_only = types.MethodType(namespace['_clear_queue_only'], probe)
        probe._music_player_is_active = lambda _:True
        class Voice:
            def stop(self): raise AssertionError('music must not be stopped')
        probe._get_voice_client_for_guild = lambda _:Voice()
        guild = SimpleNamespace(id=1)
        await probe._enqueue_tts_items(1, [item('um'), item('dois')])
        state = probe._get_state(1)
        entered, exited = asyncio.Event(), asyncio.Event()
        async def worker():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                exited.set()
        state.worker_task = asyncio.create_task(worker())
        await entered.wait()
        before = state.generation
        cleared = await probe._clear_queue_only(guild)
        self.assertEqual(cleared,2)
        self.assertTrue(exited.is_set())
        self.assertIsNone(state.worker_task)
        self.assertTrue(state.accepting)
        self.assertEqual(state.generation,before+1)
        self.assertEqual(state.pending_signatures,{})
        self.assertEqual(state.queue._unfinished_tasks,0)
        new = item('novo')
        await probe._enqueue_tts_item(1,new)
        self.assertEqual(new.generation,state.generation)
