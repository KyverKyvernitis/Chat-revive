"""Protocol and resource regressions, without contacting speech providers."""
from __future__ import annotations
import ast
import asyncio
import concurrent.futures
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

worker = load('tts_protocol_test_worker', 'deploy/termux/phone-worker/phone_worker.py')
transport = worker._tts_transport_module()

class HTTPStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {'CORE_WORKER_PROFILE': 'turbo',
            'CORE_WORKER_ROLES': 'cache-worker', 'CORE_WORKER_CAPABILITIES': 'tts-synth,cache-worker',
            'PHONE_WORKER_TTS_CACHE_DIR': self.tmp.name, 'PHONE_WORKER_TTS_AGENT_CONCURRENCY': '2'}))
        self.stack.enter_context(patch.object(worker, '_current_core_worker_profile', return_value='turbo'))
        self.stack.enter_context(patch.object(worker, '_tts_agent_available_engines', return_value=['edge','gtts']))
        self.stack.enter_context(patch.object(worker, '_turbo_dependency_snapshot', return_value={}))
        self.tail = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.fail_tail = False
        outer = self
        class FakeStream:
            def __init__(self, **kwargs):
                outer.calls.append(kwargs)
                self.stop = False
            def __iter__(self):
                yield b'first-audio'
                outer.tail.wait(3)
                if outer.fail_tail:
                    raise RuntimeError('provider disconnected')
                yield b'last-audio'
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
            def close(self):
                self.stop = True
                outer.finished.set()
        self.stack.enter_context(patch.object(transport, 'AudioStream', FakeStream))
        worker._TTS_AGENT_ACTIVE = 0
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), worker.WorkerHandler)
        self.server.worker_token = 'test-token'
        self.server.job_timeout = 5
        self.server.max_body_bytes = 65536
        self.server.max_output_bytes = 65536
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.connections = []

    def tearDown(self):
        self.tail.set()
        for conn in self.connections:
            conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.stack.close()
        self.tmp.cleanup()

    def request(self, path, payload, *, headers=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.connections.append(conn)
        request_headers = {'Authorization': 'Bearer test-token'} if auth else {}
        request_headers.update(headers or {})
        data = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        conn.request('POST', path, body=data, headers=request_headers)
        return conn.getresponse()

    def test_first_bytes_arrive_before_provider_finishes(self):
        response = self.request('/tts-agent/synthesize.stream', {'text':'Olá', 'engine':'edge'})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('X-Core-Worker-Stream-Protocol'), '2')
        self.assertEqual(response.read(len(b'first-audio')), b'first-audio')
        self.assertFalse(self.finished.is_set())
        self.tail.set()
        self.assertEqual(response.read(), b'last-audio')
        self.assertTrue(self.finished.wait(1))
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_stream_never_has_a_successful_eof_or_cache(self):
        self.fail_tail = True
        response = self.request('/tts-agent/synthesize.stream', {'text':'Falha', 'engine':'edge'})
        self.assertEqual(response.read(11), b'first-audio')
        self.tail.set()
        with self.assertRaises(http.client.IncompleteRead):
            response.read()
        self.assertTrue(self.finished.wait(1))
        self.assertEqual(list(Path(self.tmp.name).glob('*.mp3')), [])
        self.assertEqual(list(Path(self.tmp.name).glob('*.part')), [])

    def test_stream_and_binary_cache_require_authentication(self):
        for route in ['/tts-agent/synthesize.stream','/tts-agent/cache.raw']:
            response = self.request(route, {'text':'test'}, auth=False)
            self.assertEqual(response.status, 403)
            response.read()
        self.assertEqual(self.calls, [])

    def test_binary_cache_checks_digest_and_publishes_atomically(self):
        data = b'audio-content' * 64
        key = 'b' * 64
        headers = {'X-Core-Cache-Key':key, 'X-Core-Audio-Format':'mp3',
                   'X-Core-Sha256':hashlib.sha256(data).hexdigest()}
        response = self.request('/tts-agent/cache.raw', data, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())['cache_stored'])
        target = Path(self.tmp.name) / (key + '.mp3')
        self.assertEqual(target.read_bytes(), data)
        headers['X-Core-Sha256'] = '0' * 64
        response = self.request('/tts-agent/cache.raw', b'corrupted', headers=headers)
        self.assertEqual(response.status, 502)
        response.read()
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(list(Path(self.tmp.name).glob('*.part')))

    def test_cached_stream_never_invokes_provider(self):
        key = 'c' * 64
        (Path(self.tmp.name) / (key + '.mp3')).write_bytes(b'cached')
        response = self.request('/tts-agent/synthesize.stream', {'engine':'gtts','text':'cache','cache_key':key})
        self.assertEqual(response.getheader('X-Core-Worker-Cache-Hit'), 'true')
        self.assertEqual(response.read(), b'cached')
        self.assertEqual(self.calls, [])

    def test_legacy_raw_endpoint_still_returns_content_length(self):
        self.tail.set()
        response = self.request('/tts-agent/synthesize.raw', {'engine':'edge','text':'legacy'})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Length'), str(len(b'first-audiolast-audio')))
        self.assertEqual(response.read(), b'first-audiolast-audio')

class ResourceTests(unittest.TestCase):
    def test_atomic_worker_admission_under_simultaneous_requests(self):
        barrier = threading.Barrier(16)
        worker._TTS_AGENT_ACTIVE = 0
        def try_enter():
            barrier.wait()
            try:
                worker._tts_agent_record_start()
                return True
            except RuntimeError:
                return False
        with patch.object(worker, '_tts_agent_queue_limit', return_value=3), concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: try_enter(), range(16)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(worker._TTS_AGENT_ACTIVE, 3)
        worker._TTS_AGENT_ACTIVE = 0

    def test_provider_slots_stay_owned_until_gtts_thread_stops(self):
        entered = threading.Event()
        release = threading.Event()
        class FakeTTS:
            def __init__(self, **kwargs): pass
            def stream(self):
                entered.set()
                release.wait(2)
                yield b'audio'
        slots = threading.BoundedSemaphore(1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(transport, '_resources', return_value=(slots, None, pool)), patch.dict(sys.modules, {'gtts':types.SimpleNamespace(gTTS=FakeTTS)}):
                stream = transport.AudioStream(engine='gtts', text='one')
                self.assertTrue(entered.wait(1))
                stream.close()
                with self.assertRaises(RuntimeError):
                    transport.AudioStream(engine='gtts', text='two')
                release.set()
                self.assertTrue(stream.done.wait(1))
                self.assertTrue(slots.acquire(blocking=False))
                slots.release()

    def test_completed_stream_has_immediate_eof_and_independent_reader_sizes(self):
        class Stream:
            def __iter__(self): return iter((b'abcdef', b'ghi'))
            def __next__(self): return next(self.iterator)
            def close(self): pass
        stream = Stream()
        stream.iterator = iter(stream)
        published = []
        reader = transport.AudioReader(stream, on_complete=published.append)
        self.assertEqual(reader.read(2), b'ab')
        self.assertEqual(reader.read(3), b'cde')
        self.assertEqual(reader.read(9), b'f')
        self.assertEqual(reader.read(9), b'ghi')
        self.assertEqual(reader.read(9), b'')
        self.assertEqual(published, [b'abcdefghi'])
        reader.close()

    def test_cleaner_preserves_lease_credentials_and_piper_quota(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'tmp_audio'
            cache = root / 'cache'
            credentials = root / 'credentials'
            cache.mkdir(parents=True)
            credentials.mkdir()
            private = credentials / 'token.json'
            private.write_text('credential-test-fixture')
            paths = [cache / 'active.mp3', cache / 'old.mp3', cache / 'piper_old.wav', cache / 'fresh.mp3']
            for path in paths:
                path.write_bytes(b'x' * 128)
            for path in paths[:3]:
                os.utime(path, (time.time() - 600,)*2)
            with paths[0].open('rb') as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                subprocess.run(['bash', str(ROOT / 'cleanup-audio-temp.sh')], env={**os.environ,
                    'BOT_DIR':tmp, 'MAX_BYTES':'0', 'CACHE_MAX_BYTES':'0', 'TTS_PIPER_VPS_CACHE_MAX_BYTES':'1024'}, check=True)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertTrue(paths[2].exists())
            self.assertTrue(paths[3].exists())
            self.assertTrue(private.exists())
            self.assertTrue((root / 'runtime').is_dir())

    def test_worker_cache_identity_keeps_text_case_punctuation_and_tld(self):
        handler = object.__new__(worker.WorkerHandler)
        keys = [handler._tts_agent_standard_cache_key(body, engine='gtts') for body in
                ({'text':'Olá!'}, {'text':'olá!'}, {'text':'Olá!!'}, {'text':'Olá!', 'tld':'com.br'})]
        self.assertEqual(len(set(keys)), len(keys))

    def test_mixer_cancellation_removes_only_its_own_overlay(self):
        # Load only the real mixer class; this test does not need Discord login.
        text = (ROOT / 'deploy/termux/phone-worker/music_agent.py').read_text()
        node = next(n for n in ast.parse(text).body if isinstance(n,ast.ClassDef) and n.name=='AgentMixedAudioSource')
        from array import array
        namespace = {'discord':types.SimpleNamespace(AudioSource=object), 'asyncio':asyncio,
                     'contextlib':contextlib,'threading':threading,'array':array,'PCM_FRAME_BYTES':3840,'Any':object}
        exec(compile(ast.Module(body=[node], type_ignores=[]),'<mixer>', 'exec'),namespace)
        class Source:
            cleaned = False
            def cleanup(self): self.cleaned = True
        loop = asyncio.new_event_loop()
        try:
            music, first, second = Source(), Source(), Source()
            mixer = namespace['AgentMixedAudioSource'](loop=loop,music_source=music,music_volume=1)
            one, two = mixer.add_tts(first), mixer.add_tts(second)
            mixer.cancel_tts(one)
            self.assertTrue(first.cleaned)
            self.assertFalse(second.cleaned)
            self.assertFalse(music.cleaned)
            self.assertTrue(one.cancelled())
            self.assertFalse(two.done())
        finally:
            loop.close()


class RemoteCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = (ROOT / 'deploy/termux/phone-worker/music_agent.py').read_text()
        names = {'_tts_requests','_run_tts_request','cmd_cancel_tts'}
        methods = [n for n in ast.walk(ast.parse(source)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        namespace = {'asyncio':asyncio, 'time':time, 'safe_id':int}
        for method in methods:
            exec(compile(ast.Module(body=[method],type_ignores=[]),'<request-cancellation>','exec'),namespace)
        Agent = type('Agent', (), {name:namespace[name] for name in names})
        self.agent = Agent()
        self.entered = asyncio.Event()
        self.stopped = []
        async def synthesize(body):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.append(body['tts_request_id'])
        self.agent.cmd_tts = self.agent.cmd_voice_tts = synthesize

    async def test_cancellation_only_targets_matching_request_and_guild(self):
        one = asyncio.create_task(self.agent._run_tts_request({'guild_id':1,'tts_request_id':'one'}, direct=True))
        await self.entered.wait()
        self.entered.clear()
        two = asyncio.create_task(self.agent._run_tts_request({'guild_id':1,'tts_request_id':'two'}, direct=False))
        await self.entered.wait()
        try:
            await self.agent.cmd_cancel_tts({'guild_id':2,'tts_request_id':'one'})
            self.assertFalse(one.done())
            await self.agent.cmd_cancel_tts({'guild_id':1,'tts_request_id':'one'})
            self.assertTrue(one.cancelled())
            self.assertFalse(two.done())
            self.assertEqual(self.stopped,['one'])
        finally:
            two.cancel()
            await asyncio.gather(one,two,return_exceptions=True)

    async def test_cancel_arriving_before_synthesis_prevents_late_playback(self):
        await self.agent.cmd_cancel_tts({'guild_id':1,'tts_request_id':'late'})
        response = await self.agent._run_tts_request({'guild_id':1,'tts_request_id':'late'},direct=True)
        self.assertTrue(response['cancelled'])
        self.assertFalse(self.entered.is_set())

class DistributionTests(unittest.TestCase):
    def test_panel_release_installs_transport_and_preserves_worker_source_hash(self):
        source = (ROOT / 'utility/commands/workers.py').read_text()
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)
                    and n.name == '_build_worker_update_payload_sync')
        automation = load('tts_release_automation', 'scripts/core-worker-automation.py')
        bootstrap = load('tts_release_bootstrap', 'deploy/termux/phone-worker/phone_worker_bootstrap.py')
        namespace = {'Any': object, '_load_worker_update_automation': lambda: automation}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker-update>', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as tmp, patch.object(automation, 'AGENT_RELEASE_ROOT', Path(tmp) / 'release'):
            payload = namespace[node.name](object())
            self.assertEqual(payload['update_transport'], 'bootstrap-manifest-v2')
            self.assertTrue(payload['bootstrap_manifest'])
            self.assertEqual(payload['version'], worker.PHONE_WORKER_VERSION)
            self.assertNotIn('files', payload)
            release = Path(tmp) / 'release'
            outer = json.loads((release / 'latest.json').read_text())
            archive = release / 'releases' / (payload['source_hash'] + '.zip')
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), payload['release_sha256'])
            staging = Path(tmp) / 'staging'
            bootstrap._extract_and_validate(archive, staging, outer)
            self.assertEqual((staging / 'tts_transport.py').read_bytes(),
                             (ROOT / 'deploy/termux/phone-worker/tts_transport.py').read_bytes())
            with patch.dict(os.environ, {'PHONE_WORKER_RELEASE_DIR': str(staging)}):
                self.assertEqual(worker._phone_worker_source_hash(), payload['source_hash'])
            repair = namespace[node.name](object(), scripts_only=True)
            self.assertEqual(repair['update_transport'], 'inline-b64-v1')
            self.assertFalse(repair['restart'])
            self.assertTrue(all(entry['target'].endswith('.sh') for entry in repair['files']))
