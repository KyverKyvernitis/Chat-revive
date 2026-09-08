"""Shared synthesis jobs for voice playback, prefetch and file attachments."""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import replace

from .runtime import MemoryBudget, PathLeases, ReplayBuffer, StreamJob, await_physical_completion


class SharedSynthesisMixin:
    def _shared_synthesis_jobs(self) -> dict[str, StreamJob]:
        jobs = getattr(self, '_tts_shared_jobs', None)
        if jobs is None:
            jobs = self._tts_shared_jobs = {}
        return jobs

    def _audio_leases(self) -> PathLeases:
        leases = getattr(self, '_tts_path_leases', None)
        if leases is None:
            leases = self._tts_path_leases = PathLeases()
        return leases

    def _lease_audio_path(self, item, path: str) -> None:
        if not path or self._edge_stream_handle_for_path(path) is not None:
            return
        self._audio_leases().acquire(path, id(item))

    def _release_item_audio(self, item) -> None:
        if item is not None:
            self._audio_leases().release(id(item))

    def _acquire_shared_job(self, state, item, *, store_in_cache: bool) -> StreamJob:
        from . import audio as a
        key = self._cache_key(item)
        jobs = self._shared_synthesis_jobs()
        job = jobs.get(key)
        if job is None or job.stop.is_set() or job.buffer.closed:
            budget = getattr(self, '_tts_audio_memory_budget', None)
            if budget is None:
                limit = max(1024 * 1024, int(getattr(a.config, 'TTS_STREAM_MEMORY_BUDGET_BYTES', 16 * 1024 * 1024)))
                budget = self._tts_audio_memory_budget = MemoryBudget(limit)
            a._ensure_tts_temp_dirs()
            job = StreamJob(key=key, item=item, started_at=time.monotonic(),
                actual_engine=item.engine, store_in_cache=store_in_cache,
                buffer=ReplayBuffer(a._RUNTIME_DIR, budget,
                    memory_limit=a.TTS_EDGE_STREAM_CACHE_MEMORY_MAX_BYTES,
                    max_bytes=a.TTS_WORKER_AGENT_MAX_AUDIO_MB * 1024 * 1024))
            jobs[key] = job
            if not bool(getattr(item, '_tts_prefetch', False)):
                job.foreground.set()
            job.task = asyncio.create_task(self._produce_shared_job(job))
        job.references += 1
        job.store_in_cache = job.store_in_cache or store_in_cache
        if not bool(getattr(item, '_tts_prefetch', False)):
            job.foreground.set()
            self._get_synth_semaphore().promote(job.task)
            self._get_gtts_semaphore().promote(job.task)
        item._tts_shared_job = job
        return job

    def _release_shared_job(self, job: StreamJob) -> None:
        job.references = max(0, job.references - 1)
        if job.references:
            return
        if not job.buffer.ended and job.task is not None and not job.task.done():
            job.stop.set()
            if not job.task.cancelling():
                job.task.cancel()
        self._close_shared_job_when_idle(job)

    def _close_shared_job_when_idle(self, job: StreamJob) -> None:
        if job.references or job.cleanup_scheduled:
            return
        pending = [task for task in (job.task, job.cache_task, *job.pending_io)
                   if task is not None and not task.done()]
        if pending:
            job.cleanup_scheduled = True
            def retry(_):
                job.cleanup_scheduled = False
                self._close_shared_job_when_idle(job)
            pending[-1].add_done_callback(retry)
            return
        if self._shared_synthesis_jobs().get(job.key) is job:
            self._shared_synthesis_jobs().pop(job.key, None)
        job.buffer.close()
        if job.path and not os.path.abspath(job.path).startswith(os.path.abspath(self._tts_cache_directory()) + os.sep):
            self._audio_leases().remove(job.path)

    async def _append_shared_audio(self, job: StreamJob, data: bytes) -> bool:
        if job.stop.is_set() or not data:
            return False
        task = asyncio.current_task()
        job.pending_io.add(task)
        try:
            await job.buffer.append(data)
            if not job.first_audio_ms:
                now = time.monotonic()
                job.first_audio_ms = (now - job.started_at) * 1000.0
                job.network_first_audio_ms = max(0.0, job.first_audio_ms - job.slot_wait_ms)
            return not job.stop.is_set()
        finally:
            job.pending_io.discard(task)

    async def _shared_prefetch_slot(self, job: StreamJob):
        if job.foreground.is_set() or job.item.engine != 'edge':
            return None
        semaphore = self._get_edge_prefetch_semaphore()
        waiter = asyncio.create_task(semaphore.acquire())
        promotion = asyncio.create_task(job.foreground.wait())
        transferred = False
        def return_grant(future):
            if not future.cancelled() and future.exception() is None and future.result():
                semaphore.release()
        try:
            await asyncio.wait({waiter, promotion}, return_when=asyncio.FIRST_COMPLETED)
            if not job.foreground.is_set() and waiter.done() and not waiter.cancelled():
                transferred = bool(waiter.result())
                return semaphore if transferred else None
            return None
        finally:
            promotion.cancel()
            if not transferred:
                if waiter.done():
                    return_grant(waiter)
                else:
                    waiter.add_done_callback(return_grant)
                    waiter.cancel()

    async def _produce_shared_job(self, job: StreamJob) -> None:
        from . import audio as a
        semaphore = self._get_synth_semaphore() if job.item.engine == 'edge' else self._get_gtts_semaphore()
        prefetch = None
        acquired = False
        blocking = None
        engine = job.item.engine
        started = time.monotonic()
        try:
            prefetch = await self._shared_prefetch_slot(job)
            await semaphore.acquire(foreground=job.foreground.is_set())
            acquired = True
            if prefetch is not None and job.foreground.is_set():
                prefetch.release()
                prefetch = None
            job.slot_wait_ms = (time.monotonic() - started) * 1000.0
            job.deadline = time.monotonic() + (a.TTS_EDGE_STREAM_TOTAL_TIMEOUT_SECONDS if engine == 'edge' else a.TTS_GTTS_TIMEOUT_SECONDS)
            job.slot_ready.set()
            self._record_latency_sample(f'{engine}_slot_wait', job.slot_wait_ms)
            # Protocol v2 is selected only when explicitly advertised. The
            # provider remains local when an older worker is connected.
            worker_stream = getattr(self, '_produce_worker_stream_job', None)
            use_worker = callable(worker_stream) and self._worker_stream_available_for(job.item)
            if use_worker:
                try:
                    await worker_stream(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if job.buffer.size:
                        raise
                    job.route = 'local_fallback'
                    use_worker = False
            if not use_worker:
                if engine == 'edge':
                    voice = a.validate_voice(job.item.voice, getattr(self, 'edge_voice_names', set()))
                    communicate = a.edge_tts.Communicate(text=job.item.text, voice=voice,
                        rate=self._normalize_edge_rate(job.item.rate), pitch=self._normalize_edge_pitch(job.item.pitch),
                        connect_timeout=a.TTS_EDGE_CONNECT_TIMEOUT_SECONDS,
                        receive_timeout=a.TTS_EDGE_RECEIVE_TIMEOUT_SECONDS)
                    async with asyncio.timeout(max(.001, job.deadline - time.monotonic())):
                        async for message in communicate.stream():
                            data = self._edge_stream_audio_chunk(message)
                            if data and not await self._append_shared_audio(job, data):
                                raise asyncio.CancelledError()
                else:
                    language = (job.item.language or a.GTTS_DEFAULT_LANGUAGE).lower().replace('_', '-')
                    if language == 'pt-br':
                        language = 'pt'
                    native = a.EdgeStreamHandle(fifo_path='', part_path='', cache_key=job.key,
                        state=self._get_state(job.item.guild_id), item=job.item,
                        queue=asyncio.Queue(), store_in_cache=False, started_at=job.started_at,
                        first_audio_ms=0.0, engine='gtts', shared_job=job,
                        stop_requested=job.stop)
                    loop = asyncio.get_running_loop()
                    blocking = loop.run_in_executor(self._get_gtts_executor(), self._gtts_stream_blocking,
                        native, loop, language, getattr(job.item, 'tld', 'com'))
                    try:
                        await asyncio.wait_for(asyncio.shield(blocking), timeout=max(.001, job.deadline - time.monotonic()))
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        job.stop.set()
                        # Consumers stop immediately; this producer owns its
                        # physical slot until the blocking request really ends.
                        with contextlib.suppress(Exception):
                            await await_physical_completion(blocking)
                        raise
            if job.buffer.size <= 0:
                raise RuntimeError('engine não enviou áudio')
            await job.buffer.finish()
            duration = (time.monotonic() - job.started_at) * 1000.0
            self._record_engine_success(engine, duration)
            self._record_latency_sample(f'synthesis:{engine}:{job.route}', duration - job.slot_wait_ms)
            cached = bool(getattr(job.item, '_tts_worker_cache_hit', False))
            history = self._route_measurements()
            history.record(engine, job.item.text, job.route, 'first_audio', job.first_audio_ms, cached=cached)
            history.record(engine, job.item.text, job.route, 'total', duration, cached=cached)
            self._schedule_persistent_synt_success(job.item.guild_id, engine)
            metrics = self._get_metrics_store()
            metrics[f'{engine}_stream_completed'] = int(metrics.get(f'{engine}_stream_completed', 0)) + 1
            metrics[f'{engine}_stream_audio_bytes'] = int(metrics.get(f'{engine}_stream_audio_bytes', 0)) + job.buffer.size
            if job.store_in_cache:
                job.cache_task = self._schedule_tts_background(self._materialize_shared_job(job, cache=True))
                for handle in self._get_edge_stream_handles().values():
                    if handle.shared_job is job:
                        handle.cache_task = job.cache_task
        except asyncio.CancelledError as error:
            job.stop.set()
            await job.buffer.finish(error)
            raise
        except Exception as error:
            job.stop.set()
            await job.buffer.finish(error)
            self._record_engine_failure(engine, error, duration_ms=(time.monotonic() - started) * 1000)
            metrics = self._get_metrics_store()
            metrics[f'{engine}_stream_failures'] = int(metrics.get(f'{engine}_stream_failures', 0)) + 1
        finally:
            job.slot_ready.set()
            if acquired:
                semaphore.release()
            if prefetch is not None:
                prefetch.release()

    async def _materialize_shared_job(self, job: StreamJob, *, cache: bool) -> str:
        if job.path and os.path.isfile(job.path):
            return job.path
        path = self._make_runtime_temp_file(suffix='.mp3')
        try:
            await job.buffer.copy_to(path)
            if cache:
                item = replace(job.item, engine=job.actual_engine, _cache_key_value=None, _dedup_signature=None)
                path = await self._store_in_cache(self._get_state(item.guild_id), item, path)
                self._schedule_worker_turbo_cache_store(item, path)
            job.path = path
            for handle in self._get_edge_stream_handles().values():
                if handle.shared_job is job:
                    handle.cache_path = path
            return path
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(path)
            raise

    async def _shared_job_file(self, state, item, *, store_in_cache: bool) -> tuple[str, bool]:
        job = self._acquire_shared_job(state, item, store_in_cache=store_in_cache)
        try:
            await asyncio.shield(job.task)
            if job.buffer.error is not None:
                raise job.buffer.error
            if job.cache_task is None:
                job.cache_task = asyncio.create_task(self._materialize_shared_job(job, cache=store_in_cache))
            path = await asyncio.shield(job.cache_task)
            self._lease_audio_path(item, path)
            item._tts_actual_engine = job.actual_engine
            item._tts_audio_origin = 'generated'
            item._tts_audio_route = job.route
            return path, not os.path.abspath(path).startswith(os.path.abspath(self._tts_cache_directory()) + os.sep)
        finally:
            self._release_shared_job(job)

    def _tts_cache_directory(self) -> str:
        from . import audio as a
        return a._CACHE_DIR

    async def _shared_stream_reader(self, handle) -> None:
        job = handle.shared_job
        try:
            async for chunk in job.buffer.chunks():
                if handle.consumer_abandoned:
                    return
                await self._edge_stream_enqueue(handle, chunk)
                handle.audio_bytes += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            handle.error = error
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await self._signal_stream_end(handle)

    async def _prepare_shared_stream(self, state, item, *, store_in_cache: bool):
        from . import audio as a
        consumer_started = time.monotonic()
        job = self._acquire_shared_job(state, item, store_in_cache=store_in_cache)
        handle = None
        try:
            await job.slot_ready.wait()
            consumer_slot_wait = (time.monotonic() - consumer_started) * 1000
            if job.buffer.error is not None:
                raise job.buffer.error
            edge = item.engine == 'edge'
            prebuffer, profile = self._edge_prebuffer_ms(item) if edge else (0, '')
            minimum = max(1024, prebuffer * 6) if edge else 1
            timeout = a.TTS_EDGE_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS if edge else a.TTS_GTTS_STREAM_FIRST_AUDIO_TIMEOUT_SECONDS
            path = self._make_edge_stream_fifo()
            handle = a.EdgeStreamHandle(fifo_path=path, part_path='', cache_key=job.key,
                state=state, item=item, queue=asyncio.Queue(maxsize=a.TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS),
                store_in_cache=False, started_at=consumer_started, first_audio_ms=job.first_audio_ms,
                engine=item.engine, shared_job=job, prebuffer_ms=prebuffer,
                prebuffer_profile_key=profile, synth_slot_wait_ms=consumer_slot_wait,
                network_first_audio_ms=job.network_first_audio_ms,
                cache_task=job.cache_task, cache_path=job.path)
            self._get_edge_stream_handles()[os.path.abspath(path)] = handle
            handle.producer_task = asyncio.create_task(self._shared_stream_reader(handle))
            early_count = int(getattr(self, '_tts_early_decoders', 0))
            if (a.TTS_FFMPEG_PRIME_ENABLED and getattr(a.config, 'TTS_FFMPEG_OVERLAP_ENABLED', True)
                    and not getattr(item, '_tts_prefetch', False)
                    and not self._is_music_active_for_guild(item.guild_id)
                    and early_count < max(1, int(getattr(a.config, 'TTS_FFMPEG_OVERLAP_CONCURRENCY', 2)))):
                self._tts_early_decoders = early_count + 1
                handle._early_counted = True
                handle._early_started = time.monotonic()
                await self._activate_edge_stream(handle)
                handle._early_source, handle._early_kind = self._make_discord_tts_source(path)
                handle._early_read = asyncio.create_task(asyncio.to_thread(handle._early_source.read))
            await asyncio.wait_for(job.buffer.wait_for_bytes(minimum), timeout=timeout)
            handle.first_audio_ms = (time.monotonic() - consumer_started) * 1000
            handle.network_first_audio_ms = max(0, handle.first_audio_ms - consumer_slot_wait)
            handle.first_audio_ready.set()
            item._tts_actual_engine = job.actual_engine
            item._tts_audio_origin = 'progressive'
            item._tts_audio_route = job.route
            metrics = self._get_metrics_store()
            metrics[f'{item.engine}_stream_started'] = int(metrics.get(f'{item.engine}_stream_started', 0)) + 1
            self._record_average_metric(f'{item.engine}_stream_first_audio_total_ms',
                f'{item.engine}_stream_first_audio_samples', handle.first_audio_ms)
            self._record_latency_sample(f'{item.engine}_first_audio', handle.first_audio_ms)
            return handle
        except BaseException:
            if handle is not None:
                await self._finalize_edge_stream(handle, cancel=True)
            else:
                self._release_shared_job(job)
            raise

    def _shutdown_shared_synthesis(self) -> None:
        for job in list(self._shared_synthesis_jobs().values()):
            job.references = 0
            self._release_shared_job(job)


    async def _produce_worker_stream_job(self, job):
        from . import audio as a
        base = self._phone_worker_tts_base_url()
        payload = self._tts_agent_payload_for_item(job.item)
        payload['preferred_engine'] = job.item.engine
        headers = {'Authorization': f'Bearer {a.PHONE_WORKER_TOKEN}', 'Accept': 'audio/mpeg'}
        timeout = a.aiohttp.ClientTimeout(total=min(a.TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS, max(.001, job.deadline - time.monotonic())),
                                         sock_connect=min(4, a.TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS))
        session = await self._get_phone_worker_http_session()
        started = time.monotonic()
        metrics = self._get_metrics_store()
        metrics['tts_agent_synth_attempts'] = int(metrics.get('tts_agent_synth_attempts', 0)) + 1
        job.route = 'worker'
        try:
            async with session.post(f'{base}/tts-agent/synthesize.stream', headers=headers, json=payload, timeout=timeout) as response:
                if response.status != 200:
                    if response.status in {404, 405}:
                        self._tts_agent_route_state()['stream_protocol'] = 0
                    detail = await response.content.read(200)
                    raise RuntimeError(f'worker stream HTTP {response.status}: {detail!r}')
                if response.headers.get('X-Core-Worker-Stream-Protocol') != '2':
                    raise RuntimeError('worker não confirmou protocolo progressivo')
                engine = self._worker_header_value(response.headers, 'X-Core-Worker-Engine', job.item.engine)
                if engine != job.item.engine or self._worker_header_value(response.headers, 'X-Core-Worker-Audio-Format') != 'mp3':
                    raise RuntimeError('metadados incompatíveis no stream do worker')
                job.actual_engine = engine
                job.item._tts_worker_cache_hit = self._worker_header_value(response.headers, 'X-Core-Worker-Cache-Hit') == 'true'
                async for chunk in response.content.iter_chunked(16384):
                    if not await self._append_shared_audio(job, chunk):
                        raise asyncio.CancelledError()
                if not job.buffer.size:
                    raise RuntimeError('worker stream vazio')
                self._record_tts_agent_synth_success(total_ms=(time.monotonic() - started) * 1000,
                    data={'requested_engine': job.item.engine, 'selected_engine': engine,
                          'audio_format': 'mp3', 'audio_bytes_len': job.buffer.size,
                          'cache_hit': job.item._tts_worker_cache_hit,
                          'worker_id': self._worker_header_value(response.headers, 'X-Core-Worker-Id'),
                          'worker_version': self._worker_header_value(response.headers, 'X-Core-Worker-Version')})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._mark_tts_agent_synth_failure(error)
            raise
