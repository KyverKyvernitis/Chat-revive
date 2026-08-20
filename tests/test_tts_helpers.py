from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_runtime_stubs() -> None:
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class Object:
            def __init__(self, id: int):
                self.id = id

        class Embed:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        discord.Object = Object
        discord.Embed = Embed
        discord.Member = type("Member", (), {})
        discord.Role = type("Role", (), {})
        discord.Guild = type("Guild", (), {})
        discord.Message = type("Message", (), {})
        discord.Interaction = type("Interaction", (), {})
        discord.InteractionResponseType = type("InteractionResponseType", (), {})
        discord.NotFound = type("NotFound", (Exception,), {})
        discord.SelectOption = type("SelectOption", (), {})
        discord.ButtonStyle = type("ButtonStyle", (), {"secondary": 2, "success": 3, "danger": 4, "primary": 1})
        discord.Color = type("Color", (), {"green": staticmethod(lambda: 0), "red": staticmethod(lambda: 0)})
        discord.VoiceChannel = type("VoiceChannel", (), {})
        discord.StageChannel = type("StageChannel", (), {})
        discord.TextChannel = type("TextChannel", (), {})
        discord.VoiceClient = type("VoiceClient", (), {})
        discord.VoiceState = type("VoiceState", (), {})
        discord.FFmpegPCMAudio = type("FFmpegPCMAudio", (), {})
        discord.PCMVolumeTransformer = type("PCMVolumeTransformer", (), {})
        discord.utils = types.SimpleNamespace(get=lambda *args, **kwargs: None)
        discord.abc = types.SimpleNamespace(GuildChannel=type("GuildChannel", (), {}))
        discord.ui = types.SimpleNamespace(View=type("View", (), {}), Button=type("Button", (), {}), Select=type("Select", (), {}))

        app_commands = types.ModuleType("discord.app_commands")

        def guilds(*_objs):
            def decorator(func):
                return func
            return decorator

        app_commands.guilds = guilds
        discord.app_commands = app_commands

        ext = types.ModuleType("discord.ext")
        commands = types.ModuleType("discord.ext.commands")
        ext.commands = commands
        discord.ext = ext

        sys.modules["discord"] = discord
        sys.modules["discord.app_commands"] = app_commands
        sys.modules["discord.ext"] = ext
        sys.modules["discord.ext.commands"] = commands

    discord = sys.modules["discord"]
    if "discord.errors" not in sys.modules:
        discord_errors = types.ModuleType("discord.errors")

        class ConnectionClosed(Exception):
            def __init__(self, code: int, message: str = "closed"):
                super().__init__(message)
                self.code = code

        discord_errors.ConnectionClosed = ConnectionClosed
        sys.modules["discord.errors"] = discord_errors
    discord.errors = sys.modules["discord.errors"]

    if "edge_tts" not in sys.modules:
        edge_tts = types.ModuleType("edge_tts")
        edge_tts.Communicate = object
        sys.modules["edge_tts"] = edge_tts

    if "gtts" not in sys.modules:
        gtts = types.ModuleType("gtts")

        class FakeGTTS:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def write_to_fp(self, fp):
                fp.write(b"")

        gtts.gTTS = FakeGTTS
        sys.modules["gtts"] = gtts
    else:
        gtts = sys.modules["gtts"]

    if "gtts.tts" not in sys.modules:
        gtts_tts = types.ModuleType("gtts.tts")

        class gTTSError(Exception):
            pass

        gtts_tts.gTTSError = gTTSError
        sys.modules["gtts.tts"] = gtts_tts

    if "gtts.lang" not in sys.modules:
        gtts_lang = types.ModuleType("gtts.lang")
        gtts_lang.tts_langs = lambda: {"pt": "Portuguese", "en": "English"}
        sys.modules["gtts.lang"] = gtts_lang

    if "google" not in sys.modules:
        google = types.ModuleType("google")
        sys.modules["google"] = google
    if "google.cloud" not in sys.modules:
        google_cloud = types.ModuleType("google.cloud")
        sys.modules["google.cloud"] = google_cloud
    if "google.cloud.texttospeech_v1" not in sys.modules:
        tts = types.ModuleType("google.cloud.texttospeech_v1")
        sys.modules["google.cloud.texttospeech_v1"] = tts

    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = type("ClientSession", (), {})
        aiohttp.ClientTimeout = type("ClientTimeout", (), {})
        aiohttp.TCPConnector = type("TCPConnector", (), {})
        sys.modules["aiohttp"] = aiohttp


_install_runtime_stubs()

import cogs.tts.audio as tts_audio
from cogs.tts.audio import GuildTTSState, QueueItem, TTSAudioMixin, _has_speakable_tts_text
from cogs.chatbot import VoiceConnectionFilter
from cogs.tts.utils.message_dispatch import dispatch_message_tts
from cogs.tts.utils.message_gate import analyze_message_for_tts
from cogs.tts.utils.message_payload import MessageTTSPayload, build_message_tts_payload
from cogs.tts.utils.message_render import render_message_tts_text
from cogs.tts.common import (
    _normalize_spaces,
    _speech_name,
    _looks_pronounceable_for_tts,
    _extract_primary_domain,
    DISCORD_CHANNEL_URL_PATTERN,
    _ATTACHMENT_IMAGE_EXTENSIONS,
    _ATTACHMENT_VIDEO_EXTENSIONS,
)
from cogs.tts.utils.text import (
    tts_attachment_descriptions,
    tts_channel_reference,
    tts_link_reference,
    tts_role_reference,
    tts_user_reference,
)


class FakeDB:
    def __init__(self, *, guild_defaults=None, resolved=None):
        self.guild_defaults = guild_defaults or {}
        self.resolved = resolved or {}

    async def get_guild_tts_defaults(self, guild_id: int):
        return dict(self.guild_defaults)

    async def resolve_tts(self, guild_id: int, user_id: int):
        return dict(self.resolved)


class FakeGuild:
    def __init__(self, guild_id: int = 1, *, members=None, roles=None, channels=None):
        self.id = guild_id
        self._members = {m.id: m for m in (members or [])}
        self._roles = {r.id: r for r in (roles or [])}
        self._channels = {c.id: c for c in (channels or [])}

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


class FakeCog:
    def __init__(self, *, db=None):
        self._db = db
        self._state = SimpleNamespace(last_text_channel_id=None)
        self.enqueue_calls = []

    def _get_db(self):
        return self._db

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _render_tts_text(self, message, text: str) -> str:
        return text.strip().replace("dupe", "rendered")

    def _apply_author_prefix_if_needed(self, guild_id, author, text: str, *, enabled: bool):
        return f"{author.display_name} disse {text}" if enabled else text

    def _guild_announce_author_enabled(self, guild_defaults):
        return bool((guild_defaults or {}).get("announce_author"))

    def _get_state(self, guild_id: int):
        return self._state

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem):
        self.enqueue_calls.append((guild_id, item))
        return True, 0, False


class MessageGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_reason_when_tts_disabled(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content=", oi")

        with patch("cogs.tts.utils.message_gate.config.TTS_ENABLED", False):
            decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertEqual(decision.reason, "tts_disabled")

    async def test_detects_prefix_command(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"bot_prefix": "_"}))
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content="_join")

        decision = await analyze_message_for_tts(cog, message)

        self.assertTrue(decision.should_dispatch_prefix_command)
        self.assertEqual(decision.prefix_command.kind, "join")
        self.assertEqual(decision.reason, "prefix_command")

    async def test_detects_tts_prefix(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"tts_prefix": "."}))
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content=".olá")

        decision = await analyze_message_for_tts(cog, message)

        self.assertTrue(decision.should_process_tts)
        self.assertEqual(decision.forced_engine, "gtts")
        self.assertEqual(decision.active_prefix, ".")
        self.assertEqual(decision.reason, "tts_prefix_matched")

    async def test_returns_reason_when_no_matching_prefix(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content="olá mundo")

        decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertEqual(decision.reason, "no_engine_prefix")


class MessageRenderTests(unittest.TestCase):
    def _user_reference(self, member, *, guild_id=None):
        return tts_user_reference(member, resolver=lambda m, guild_id=None: (getattr(m, "display_name", "usuário"), None), guild_id=guild_id)

    def _role_reference(self, role):
        return tts_role_reference(role, normalize_spaces=_normalize_spaces, looks_pronounceable_for_tts=_looks_pronounceable_for_tts, speech_name=_speech_name)

    def _channel_reference(self, channel):
        return tts_channel_reference(channel, normalize_spaces=_normalize_spaces, looks_pronounceable_for_tts=_looks_pronounceable_for_tts, speech_name=_speech_name)

    def _link_reference(self, url, *, guild=None):
        return tts_link_reference(
            url,
            guild=guild,
            discord_channel_url_pattern=DISCORD_CHANNEL_URL_PATTERN,
            channel_reference=self._channel_reference,
            extract_primary_domain=_extract_primary_domain,
            looks_pronounceable_for_tts=_looks_pronounceable_for_tts,
            speech_name=_speech_name,
        )

    def test_simple_text_uses_fast_path_and_attachment_suffix(self):
        attachment = SimpleNamespace(filename="foto.png", content_type="image/png")
        message = SimpleNamespace(attachments=[attachment], guild=None, mentions=[], role_mentions=[], channel_mentions=[])

        result = render_message_tts_text(
            message,
            "vc mandou",
            guild_id=None,
            user_reference=self._user_reference,
            role_reference=self._role_reference,
            channel_reference=self._channel_reference,
            link_reference=self._link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

        self.assertEqual(result, "você mandou. Anexo de imagem")

    def test_replaces_mentions_links_and_channels(self):
        member = SimpleNamespace(id=10, display_name="Lucas")
        role = SimpleNamespace(id=20, name="Staff")
        channel = SimpleNamespace(id=30, name="geral")
        guild = FakeGuild(members=[member], roles=[role], channels=[channel])
        message = SimpleNamespace(
            guild=guild,
            attachments=[],
            mentions=[member],
            role_mentions=[role],
            channel_mentions=[channel],
        )

        result = render_message_tts_text(
            message,
            "oi <@10> veja <@&20> no <#30> https://example.com/test",
            guild_id=1,
            user_reference=self._user_reference,
            role_reference=self._role_reference,
            channel_reference=self._channel_reference,
            link_reference=self._link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

        self.assertIn("Lucas", result)
        self.assertIn("cargo Staff", result)
        self.assertIn("canal geral", result)
        self.assertIn("link do example", result)


class SpeakableTextTests(unittest.TestCase):
    def test_rejects_inputs_that_cannot_produce_speech(self):
        for text in ("", "   ", "!!!", "... ???", "😀🔥"):
            with self.subTest(text=text):
                self.assertFalse(_has_speakable_tts_text(text))

    def test_accepts_unicode_letters_and_numbers(self):
        for text in ("olá", "東京", "123", "oi 😀"):
            with self.subTest(text=text):
                self.assertTrue(_has_speakable_tts_text(text))


class VoiceConnectionLogFilterTests(unittest.TestCase):
    def test_recoverable_close_is_kept_as_compact_info_record(self):
        connection_closed = sys.modules["discord.errors"].ConnectionClosed
        try:
            exc = connection_closed(SimpleNamespace(close_code=1006), shard_id=None, code=1006)
        except TypeError:
            # O stub leve usado sem discord.py mantém a assinatura antiga.
            exc = connection_closed(1006, "abnormal closure")
        record = logging.LogRecord(
            name="discord.voice_client",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="voice websocket closed",
            args=(),
            exc_info=(type(exc), exc, None),
        )

        allowed = VoiceConnectionFilter().filter(record)

        self.assertTrue(allowed)
        self.assertEqual(record.levelno, logging.INFO)
        self.assertIsNone(record.exc_info)
        self.assertIn("code=1006", record.getMessage())
        self.assertIn("controlador do bot", record.getMessage())


class MessagePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_payload_and_queue_item_with_author_prefix(self):
        db = FakeDB(resolved={"engine": "edge", "voice": "", "rate": "", "pitch": ""})
        cog = FakeCog(db=db)
        author = SimpleNamespace(id=22, display_name="Dilma", voice=SimpleNamespace(channel=SimpleNamespace(id=555)))
        message = SimpleNamespace(guild=SimpleNamespace(id=111), author=author, content=",olá")

        payload = await build_message_tts_payload(
            cog,
            message,
            guild_defaults={"announce_author": True},
            active_prefix=",",
            forced_engine="edge",
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload.queue_item.guild_id, 111)
        self.assertEqual(payload.queue_item.channel_id, 555)
        self.assertEqual(payload.queue_item.engine, "edge")
        self.assertEqual(payload.queue_item.voice, "pt-BR-FranciscaNeural")
        self.assertEqual(payload.queue_item.rate, "+0%")
        self.assertEqual(payload.text, "Dilma disse olá")

    async def test_returns_none_without_voice_channel(self):
        db = FakeDB(resolved={"engine": "gtts", "language": "pt-br"})
        cog = FakeCog(db=db)
        author = SimpleNamespace(id=22, display_name="Dilma", voice=None)
        message = SimpleNamespace(guild=SimpleNamespace(id=111), author=author, content=".olá")

        payload = await build_message_tts_payload(
            cog,
            message,
            guild_defaults={},
            active_prefix=".",
            forced_engine="gtts",
        )

        self.assertIsNone(payload)

    async def test_returns_none_for_punctuation_only_text(self):
        db = FakeDB(resolved={"engine": "gtts", "language": "pt-br"})
        cog = FakeCog(db=db)
        author = SimpleNamespace(id=22, display_name="Dilma", voice=SimpleNamespace(channel=SimpleNamespace(id=555)))
        message = SimpleNamespace(guild=SimpleNamespace(id=111), author=author, content=".!!!")

        payload = await build_message_tts_payload(
            cog,
            message,
            guild_defaults={},
            active_prefix=".",
            forced_engine="gtts",
        )

        self.assertIsNone(payload)


class PlaybackRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_playback_uses_single_controller_without_hard_reset(self):
        recovered_voice_client = object()

        class Probe(TTSAudioMixin):
            def __init__(self):
                self.guild_states = {}
                self.play_calls = 0
                self.ensure_calls = 0
                self.reset_calls = 0

            async def _play_file(self, vc, path, *, item=None):
                self.play_calls += 1
                if self.play_calls == 1:
                    raise RuntimeError("Not connected to voice")
                self.asserted_voice_client = vc
                return {"playback_ms": 1.0}

            async def _ensure_connected_fast(self, guild, item):
                self.ensure_calls += 1
                return recovered_voice_client

            async def _reset_voice_client(self, guild, *, reason="unknown"):
                self.reset_calls += 1

        probe = Probe()
        guild = SimpleNamespace(id=77)
        item = QueueItem(77, 55, 66, "teste", "gtts", "", "pt", "+0%", "+0Hz")

        result = await probe._play_file_with_recovery(guild, item, object(), "/tmp/audio.mp3")

        self.assertEqual(result["playback_ms"], 1.0)
        self.assertEqual(probe.play_calls, 2)
        self.assertEqual(probe.ensure_calls, 1)
        self.assertEqual(probe.reset_calls, 0)
        self.assertIs(probe.asserted_voice_client, recovered_voice_client)


class EdgeStreamingFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        runtime = os.path.join(root, "runtime")
        cache = os.path.join(root, "cache")
        self.paths_patch = patch.multiple(
            tts_audio,
            TTS_TEMP_DIR=root,
            _RUNTIME_DIR=runtime,
            _CACHE_DIR=cache,
            _TTS_REQUIRED_DIRS=(root, runtime, cache),
        )
        self.paths_patch.start()
        tts_audio._ensure_tts_temp_dirs()

    async def asyncTearDown(self):
        self.paths_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _item() -> QueueItem:
        return QueueItem(
            77,
            55,
            66,
            "teste progressivo",
            "edge",
            "pt-BR-FranciscaNeural",
            "pt-br",
            "+18%",
            "-7Hz",
        )

    @staticmethod
    def _probe():
        class Probe(TTSAudioMixin):
            def __init__(self):
                self.guild_states = {}
                self.edge_voice_names = {"pt-BR-FranciscaNeural"}
                self.bot = SimpleNamespace(audio_router=None)

            async def _record_persistent_synt_success(self, guild_id, engine):
                return None

            def _schedule_worker_turbo_cache_store(self, item, path):
                return None

        return Probe()

    async def test_returns_after_prebuffer_and_streams_remaining_audio(self):
        release_tail = asyncio.Event()
        captured = {}

        class FakeCommunicate:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def stream(self):
                async def generate():
                    yield {"type": "audio", "data": b"A" * 2048}
                    await release_tail.wait()
                    yield {"type": "audio", "data": b"B" * 2048}

                return generate()

        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        item = self._item()

        with patch.object(tts_audio.edge_tts, "Communicate", FakeCommunicate):
            handle = await asyncio.wait_for(
                probe._prepare_edge_stream(state, item, store_in_cache=True),
                timeout=1.0,
            )
            self.assertFalse(handle.producer_task.done())
            self.assertTrue(os.path.exists(handle.fifo_path))
            self.assertEqual(handle.queue.maxsize, tts_audio.TTS_EDGE_STREAM_QUEUE_MAX_CHUNKS)

            def read_fifo():
                with open(handle.fifo_path, "rb") as source:
                    return source.read()

            reader_task = asyncio.create_task(asyncio.to_thread(read_fifo))
            await probe._activate_edge_stream(handle)
            probe._close_edge_stream_reader_anchor(handle)
            release_tail.set()
            streamed = await asyncio.wait_for(reader_task, timeout=2.0)
            await asyncio.wait_for(handle.producer_task, timeout=2.0)
            await asyncio.wait_for(handle.writer_task, timeout=2.0)
            if handle.cache_task is not None:
                await asyncio.wait_for(handle.cache_task, timeout=2.0)

        self.assertEqual(streamed, (b"A" * 2048) + (b"B" * 2048))
        self.assertEqual(captured["voice"], item.voice)
        self.assertEqual(captured["rate"], item.rate)
        self.assertEqual(captured["pitch"], item.pitch)
        self.assertEqual(
            captured["connect_timeout"],
            tts_audio.TTS_EDGE_CONNECT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            captured["receive_timeout"],
            tts_audio.TTS_EDGE_RECEIVE_TIMEOUT_SECONDS,
        )
        self.assertTrue(handle.cache_path)
        with open(handle.cache_path, "rb") as cached:
            self.assertEqual(cached.read(), streamed)

        await probe._finalize_edge_stream(handle)
        self.assertFalse(os.path.exists(handle.fifo_path))
        self.assertEqual(probe._get_edge_stream_handles(), {})

    async def test_failure_before_first_audio_uses_gtts_without_retrying_edge(self):
        class FailingCommunicate:
            def __init__(self, **kwargs):
                pass

            def stream(self):
                async def generate():
                    raise RuntimeError("edge offline")
                    yield  # pragma: no cover

                return generate()

        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        item = self._item()

        async def fake_gtts(text, language):
            path = probe._make_runtime_temp_file(suffix=".mp3")
            with open(path, "wb") as output:
                output.write(b"gtts-fallback")
            return path

        with (
            patch.object(tts_audio.edge_tts, "Communicate", FailingCommunicate),
            patch.object(probe, "_generate_gtts_file", side_effect=fake_gtts) as gtts_mock,
        ):
            path, should_cleanup = await probe._resolve_audio_path(
                state,
                item,
                allow_edge_stream=True,
            )

        self.assertTrue(should_cleanup)
        self.assertEqual(gtts_mock.await_count, 1)
        with open(path, "rb") as fallback:
            self.assertEqual(fallback.read(), b"gtts-fallback")
        self.assertEqual(probe._get_metrics_store()["edge_stream_fallbacks"], 1)
        await probe._discard_edge_stream_path(path)

    async def test_cancel_before_playback_releases_slot_and_removes_pipe(self):
        never_release = asyncio.Event()

        class SlowCommunicate:
            def __init__(self, **kwargs):
                pass

            def stream(self):
                async def generate():
                    yield {"type": "audio", "data": b"A" * 2048}
                    await never_release.wait()

                return generate()

        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        semaphore = probe._get_synth_semaphore()
        initial_slots = semaphore._value

        with patch.object(tts_audio.edge_tts, "Communicate", SlowCommunicate):
            handle = await probe._prepare_edge_stream(state, self._item(), store_in_cache=False)
            self.assertEqual(semaphore._value, initial_slots - 1)
            await probe._finalize_edge_stream(handle, cancel=True)

        self.assertEqual(semaphore._value, initial_slots)
        self.assertFalse(os.path.exists(handle.fifo_path))
        self.assertEqual(handle.part_path, "")
        self.assertFalse(os.path.exists(handle.part_path))

    async def test_current_speech_overtakes_queued_prefetch(self):
        semaphore = tts_audio._PrioritySemaphore(1)
        await semaphore.acquire()
        order = []

        async def waiter(label, *, foreground):
            await semaphore.acquire(foreground=foreground)
            order.append(label)
            await asyncio.sleep(0)
            semaphore.release()

        background = asyncio.create_task(waiter("prefetch", foreground=False))
        await asyncio.sleep(0)
        foreground = asyncio.create_task(waiter("current", foreground=True))
        await asyncio.sleep(0)
        semaphore.release()
        await asyncio.gather(background, foreground)

        self.assertEqual(order, ["current", "prefetch"])
        self.assertEqual(semaphore._value, 1)

    async def test_edge_circuit_bypasses_repeated_network_failure(self):
        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        engine_metrics = probe._get_engine_metrics("edge")
        engine_metrics["consecutive_failures"] = tts_audio.TTS_EDGE_CIRCUIT_BREAKER_FAILURES
        engine_metrics["last_error_at"] = tts_audio.time.time()

        async def fake_gtts(text, language):
            path = probe._make_runtime_temp_file(suffix=".mp3")
            with open(path, "wb") as output:
                output.write(b"circuit-fallback")
            return path

        with (
            patch.object(probe, "_generate_gtts_file", side_effect=fake_gtts),
            patch.object(
                probe,
                "_prepare_edge_stream",
                side_effect=AssertionError("Edge não deve ser chamado durante cooldown"),
            ) as edge_mock,
        ):
            path, should_cleanup = await probe._resolve_audio_path(
                state,
                self._item(),
                allow_edge_stream=True,
            )

        self.assertTrue(should_cleanup)
        self.assertEqual(edge_mock.await_count, 0)
        self.assertEqual(probe._get_metrics_store()["edge_circuit_bypasses"], 1)
        with open(path, "rb") as fallback:
            self.assertEqual(fallback.read(), b"circuit-fallback")
        os.remove(path)
        probe._shutdown_tts_runtime()

    async def test_adaptive_prebuffer_reacts_slowly_and_backs_off_on_stall(self):
        probe = self._probe()
        item = self._item()
        with patch.object(tts_audio, "TTS_EDGE_ADAPTIVE_PREBUFFER_STABLE_STREAMS", 2):
            initial_ms, profile_key = probe._edge_prebuffer_ms(item)
            handle = SimpleNamespace(
                engine="edge",
                prebuffer_profile_key=profile_key,
                max_source_read_ms=4.0,
                source_read_stalls=0,
                error=None,
                pipe_error=None,
            )
            probe._observe_edge_stream_playback(handle, playback_ok=True)
            probe._observe_edge_stream_playback(handle, playback_ok=True)
            lowered_ms, _ = probe._edge_prebuffer_ms(item)

            handle.source_read_stalls = 1
            probe._observe_edge_stream_playback(handle, playback_ok=True)
            raised_ms, _ = probe._edge_prebuffer_ms(item)

        self.assertEqual(initial_ms, tts_audio.TTS_EDGE_STREAM_PREBUFFER_MS)
        self.assertEqual(lowered_ms, max(tts_audio.TTS_EDGE_ADAPTIVE_PREBUFFER_MIN_MS, initial_ms - 20))
        self.assertEqual(raised_ms, min(tts_audio.TTS_EDGE_ADAPTIVE_PREBUFFER_MAX_MS, lowered_ms + 40))
        self.assertEqual(probe._get_metrics_store()["edge_prebuffer_lowered"], 1)
        self.assertEqual(probe._get_metrics_store()["edge_prebuffer_raised"], 1)

    async def test_mp3_input_hint_is_scoped_to_edge_fifo(self):
        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        fifo_path = probe._make_edge_stream_fifo()
        handle = tts_audio.EdgeStreamHandle(
            fifo_path=fifo_path,
            part_path="",
            cache_key="hint-test",
            state=state,
            item=self._item(),
            queue=asyncio.Queue(maxsize=8),
            store_in_cache=False,
            started_at=asyncio.get_running_loop().time(),
            first_audio_ms=1.0,
        )
        probe._get_edge_stream_handles()[os.path.abspath(fifo_path)] = handle
        calls = []

        class FakePCM:
            def __init__(self, path, **kwargs):
                calls.append((path, kwargs))

        with (
            patch.object(tts_audio.discord, "FFmpegPCMAudio", FakePCM),
            patch.object(tts_audio, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin"),
        ):
            _, source_kind = probe._make_discord_tts_source(fifo_path)
            handle.engine = "gtts"
            probe._make_discord_tts_source(fifo_path)

        self.assertEqual(source_kind, "ffmpeg_pcm")
        self.assertIn("-f mp3", calls[0][1]["before_options"])
        self.assertNotIn("-f mp3", calls[1][1]["before_options"])
        await probe._finalize_edge_stream(handle, cancel=True)

    async def test_direct_worker_handoff_is_skipped_for_local_edge_fast_path(self):
        probe = self._probe()
        probe._tts_agent_route_available = lambda: True
        guild = SimpleNamespace(id=77)

        with patch.multiple(
            tts_audio,
            WORKER_VOICE_AGENT_ENABLED=True,
            WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED=True,
            WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED=True,
        ):
            allowed, reason = probe._worker_voice_direct_tts_available_for(guild, self._item())
            with patch.object(tts_audio, "TTS_EDGE_STREAMING_ENABLED", False):
                rollback_allowed, rollback_reason = probe._worker_voice_direct_tts_available_for(guild, self._item())

        self.assertFalse(allowed)
        self.assertEqual(reason, "edge_vps_stream_fastpath")
        self.assertTrue(rollback_allowed)
        self.assertEqual(rollback_reason, "allowed")

    async def test_prefetch_limit_reserves_one_edge_slot_for_current_speech(self):
        never_release = asyncio.Event()

        class SlowCommunicate:
            def __init__(self, **kwargs):
                pass

            def stream(self):
                async def generate():
                    yield {"type": "audio", "data": b"A" * 2048}
                    await never_release.wait()

                return generate()

        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        prefetch_handles = []

        with (
            patch.object(tts_audio.edge_tts, "Communicate", SlowCommunicate),
            patch.object(tts_audio, "TTS_SYNTH_CONCURRENCY", 3),
            patch.object(tts_audio, "TTS_EDGE_PREFETCH_CONCURRENCY", 2),
        ):
            for _ in range(2):
                item = self._item()
                setattr(item, "_tts_prefetch", True)
                prefetch_handles.append(
                    await asyncio.wait_for(
                        probe._prepare_edge_stream(state, item, store_in_cache=False),
                        timeout=0.5,
                    )
                )

            blocked_item = self._item()
            setattr(blocked_item, "_tts_prefetch", True)
            blocked_prefetch = asyncio.create_task(
                probe._prepare_edge_stream(state, blocked_item, store_in_cache=False)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(blocked_prefetch.done())

            current_handle = await asyncio.wait_for(
                probe._prepare_edge_stream(state, self._item(), store_in_cache=False),
                timeout=0.5,
            )

            blocked_prefetch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await blocked_prefetch
            await probe._finalize_edge_stream(current_handle, cancel=True)
            for handle in prefetch_handles:
                await probe._finalize_edge_stream(handle, cancel=True)

    async def test_end_marker_wakes_fifo_writer_without_polling_tail(self):
        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        fifo_path = probe._make_edge_stream_fifo()
        part_path = probe._make_runtime_temp_file(suffix=".edge-stream.tmp")
        handle = tts_audio.EdgeStreamHandle(
            fifo_path=fifo_path,
            part_path=part_path,
            cache_key="test",
            state=state,
            item=self._item(),
            queue=asyncio.Queue(maxsize=8),
            store_in_cache=False,
            started_at=asyncio.get_running_loop().time(),
            first_audio_ms=1.0,
        )
        probe._get_edge_stream_handles()[os.path.abspath(fifo_path)] = handle

        def read_fifo():
            with open(fifo_path, "rb") as source:
                return source.read()

        reader = asyncio.create_task(asyncio.to_thread(read_fifo))
        await probe._activate_edge_stream(handle)
        await handle.queue.put(b"audio")
        await handle.queue.join()
        await asyncio.sleep(0.01)
        started = asyncio.get_running_loop().time()
        await probe._signal_stream_end(handle)
        probe._close_edge_stream_reader_anchor(handle)
        self.assertEqual(await asyncio.wait_for(reader, timeout=0.5), b"audio")
        self.assertLess(asyncio.get_running_loop().time() - started, 0.15)
        await probe._finalize_edge_stream(handle)

    async def test_short_stream_stays_open_until_late_ffmpeg_reader_arrives(self):
        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        fifo_path = probe._make_edge_stream_fifo()
        handle = tts_audio.EdgeStreamHandle(
            fifo_path=fifo_path,
            part_path="",
            cache_key="short-stream",
            state=state,
            item=self._item(),
            queue=asyncio.Queue(maxsize=8),
            store_in_cache=False,
            started_at=asyncio.get_running_loop().time(),
            first_audio_ms=1.0,
        )
        probe._get_edge_stream_handles()[os.path.abspath(fifo_path)] = handle
        await handle.queue.put(b"audio")
        await probe._signal_stream_end(handle)
        await probe._activate_edge_stream(handle)
        await asyncio.wait_for(handle.writer_task, timeout=0.5)

        def late_read():
            with open(fifo_path, "rb", buffering=0) as source:
                return source.read(5)

        self.assertEqual(
            await asyncio.wait_for(asyncio.to_thread(late_read), timeout=0.5),
            b"audio",
        )
        probe._close_edge_stream_reader_anchor(handle)
        await probe._finalize_edge_stream(handle)


class GTTSLatencyFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = self.temp_dir.name
        runtime = os.path.join(root, "runtime")
        cache = os.path.join(root, "cache")
        self.paths_patch = patch.multiple(
            tts_audio,
            TTS_TEMP_DIR=root,
            _RUNTIME_DIR=runtime,
            _CACHE_DIR=cache,
            _TTS_REQUIRED_DIRS=(root, runtime, cache),
        )
        self.paths_patch.start()
        tts_audio._ensure_tts_temp_dirs()

    async def asyncTearDown(self):
        self.paths_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _item() -> QueueItem:
        return QueueItem(
            77,
            55,
            66,
            "Esta mensagem tem mais de cem caracteres para que o gTTS possa entregar a primeira parte enquanto prepara a segunda parte do áudio.",
            "gtts",
            "",
            "pt-br",
            "+0%",
            "+0Hz",
        )

    @staticmethod
    def _probe():
        class Probe(TTSAudioMixin):
            def __init__(self):
                self.guild_states = {}
                self.bot = SimpleNamespace(audio_router=None, settings_db=None)

            def _schedule_worker_turbo_cache_store(self, item, path):
                return None

        return Probe()

    async def test_progressive_gtts_returns_on_first_part_and_preserves_full_cache(self):
        release_tail = threading.Event()
        captured = {}

        class FakeGTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def stream(self):
                yield b"A" * 2048
                release_tail.wait(timeout=2.0)
                yield b"B" * 2048

        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())

        with patch.object(tts_audio, "gTTS", FakeGTTS):
            handle = await asyncio.wait_for(
                probe._prepare_gtts_stream(state, self._item(), store_in_cache=True),
                timeout=1.0,
            )
            self.assertFalse(handle.producer_task.done())
            self.assertEqual(handle.engine, "gtts")

            def read_fifo():
                with open(handle.fifo_path, "rb") as source:
                    return source.read()

            reader_task = asyncio.create_task(asyncio.to_thread(read_fifo))
            await probe._activate_edge_stream(handle)
            probe._close_edge_stream_reader_anchor(handle)
            release_tail.set()
            streamed = await asyncio.wait_for(reader_task, timeout=2.0)
            await asyncio.wait_for(handle.producer_task, timeout=2.0)
            if handle.cache_task is not None:
                await asyncio.wait_for(handle.cache_task, timeout=2.0)

        self.assertEqual(streamed, (b"A" * 2048) + (b"B" * 2048))
        self.assertEqual(captured["lang"], "pt")
        self.assertEqual(
            captured["timeout"],
            (tts_audio.TTS_GTTS_CONNECT_TIMEOUT_SECONDS, tts_audio.TTS_GTTS_READ_TIMEOUT_SECONDS),
        )
        self.assertTrue(handle.cache_path)
        with open(handle.cache_path, "rb") as cached:
            self.assertEqual(cached.read(), streamed)

        await probe._finalize_edge_stream(handle)
        probe._shutdown_tts_runtime()

    async def test_full_gtts_passes_native_request_timeout(self):
        captured = {}

        class FakeGTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def stream(self):
                yield b"complete-mp3"

        probe = self._probe()
        with patch.object(tts_audio, "gTTS", FakeGTTS):
            path = await probe._generate_gtts_file("texto curto", "pt-br")

        with open(path, "rb") as audio:
            self.assertEqual(audio.read(), b"complete-mp3")
        self.assertEqual(
            captured["timeout"],
            (tts_audio.TTS_GTTS_CONNECT_TIMEOUT_SECONDS, tts_audio.TTS_GTTS_READ_TIMEOUT_SECONDS),
        )
        os.remove(path)
        probe._shutdown_tts_runtime()

    async def test_timed_out_gtts_keeps_physical_concurrency_slot_until_thread_stops(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingGTTS:
            def __init__(self, **kwargs):
                pass

            def stream(self):
                entered.set()
                release.wait(timeout=2.0)
                yield b"late-audio"

        probe = self._probe()
        with (
            patch.object(tts_audio, "gTTS", BlockingGTTS),
            patch.object(tts_audio, "TTS_GTTS_TIMEOUT_SECONDS", 0.05),
        ):
            with self.assertRaisesRegex(RuntimeError, "gTTS timeout"):
                await probe._generate_gtts_file("texto", "pt")

            self.assertTrue(entered.is_set())
            semaphore = probe._get_gtts_semaphore()
            self.assertEqual(semaphore._value, 0)
            release.set()
            deadline = asyncio.get_running_loop().time() + 1.0
            while semaphore._value == 0 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(semaphore._value, 1)

        probe._shutdown_tts_runtime()

    async def test_prefetch_enables_progressive_resolution_for_local_engines(self):
        calls = []

        class Probe(TTSAudioMixin):
            def __init__(self):
                self.guild_states = {}

            async def _resolve_audio_path(self, state, item, *, allow_edge_stream=False):
                calls.append((item, allow_edge_stream, bool(getattr(item, "_tts_prefetch", False))))
                return "/tmp/fake.mp3", False

        probe = Probe()
        state = GuildTTSState(queue=asyncio.Queue())
        item = self._item()
        await state.queue.put(item)

        prefetched_item, audio_task = await probe._maybe_prefetch_next(state)
        await audio_task

        self.assertIs(prefetched_item, item)
        self.assertEqual(calls, [(item, True, True)])
        state.queue.task_done()

    async def test_direct_voice_handoff_is_disabled_for_gtts_by_default(self):
        probe = self._probe()
        probe._tts_agent_route_available = lambda: True
        guild = SimpleNamespace(id=77)

        with patch.multiple(
            tts_audio,
            WORKER_VOICE_AGENT_ENABLED=True,
            WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED=True,
            WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED=True,
            WORKER_VOICE_AGENT_DIRECT_GTTS_ENABLED=False,
        ):
            allowed, reason = probe._worker_voice_direct_tts_available_for(guild, self._item())

        self.assertFalse(allowed)
        self.assertEqual(reason, "gtts_direct_handoff_disabled")

    async def test_persistent_counter_is_not_on_generation_critical_path(self):
        release_db = asyncio.Event()

        class DB:
            async def increment_tts_synt_count(self, guild_id, engine, amount):
                await release_db.wait()

        probe = self._probe()
        probe.bot.settings_db = DB()

        async def generate():
            return "/tmp/audio.mp3"

        result = await asyncio.wait_for(
            probe._run_timed_generation("gtts", generate, guild_id=77),
            timeout=0.2,
        )
        self.assertEqual(result, "/tmp/audio.mp3")
        self.assertTrue(probe._get_tts_background_tasks())
        release_db.set()
        await asyncio.gather(*list(probe._get_tts_background_tasks()))
        probe._shutdown_tts_runtime()

    async def test_cache_maintenance_is_deferred_until_after_critical_path(self):
        probe = self._probe()
        state = GuildTTSState(queue=asyncio.Queue())
        calls = []
        probe._purge_cache = lambda active_state, protected_paths=None: calls.append(
            (active_state, set(protected_paths or set()))
        )

        with patch.object(tts_audio, "TTS_CACHE_MAINTENANCE_DELAY_SECONDS", 0.01):
            probe._schedule_cache_maintenance(state, protected_paths={"/tmp/protected.mp3"})
            self.assertEqual(calls, [])
            await asyncio.sleep(0.03)

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], state)
        self.assertIn("/tmp/protected.mp3", calls[0][1])
        probe._shutdown_tts_runtime()


class FirstFrameMetricsTests(unittest.TestCase):
    def test_audio_source_proxy_reports_only_the_first_nonempty_frame(self):
        frames = []
        stream_reads = []

        class Source:
            def __init__(self):
                self.parts = [b"", b"frame-1", b"frame-2"]
                self.cleaned = False

            def read(self):
                return self.parts.pop(0)

            def is_opus(self):
                return True

            def cleanup(self):
                self.cleaned = True

        source = Source()
        proxy = tts_audio._FirstFrameAudioSource(
            source,
            lambda frame_at, read_ms: frames.append((frame_at, read_ms)),
            lambda read_ms: stream_reads.append(read_ms),
        )

        self.assertEqual(proxy.read(), b"")
        self.assertEqual(proxy.read(), b"frame-1")
        self.assertEqual(proxy.read(), b"frame-2")
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(stream_reads), 1)
        self.assertTrue(proxy.is_opus())
        proxy.cleanup()
        self.assertTrue(source.cleaned)


class MessageDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_aborts_when_payload_is_none(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2))

        async def fake_build(*args, **kwargs):
            return None

        with patch("cogs.tts.utils.message_dispatch.build_message_tts_payload", fake_build):
            result = await dispatch_message_tts(cog, message, guild_defaults={}, active_prefix=".", forced_engine="gtts")

        self.assertFalse(result.enqueued)
        self.assertIsNone(result.payload)
        self.assertEqual(cog.enqueue_calls, [])

    async def test_dispatch_enqueues_payload_and_updates_state(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(guild=SimpleNamespace(id=77), channel=SimpleNamespace(id=99))
        item = QueueItem(77, 55, 66, "teste", "gtts", "", "pt", "+0%", "+0Hz")
        payload = MessageTTSPayload(text="teste", resolved={"engine": "gtts"}, queue_item=item, forced_gtts=False)

        async def fake_build(*args, **kwargs):
            return payload

        with patch("cogs.tts.utils.message_dispatch.build_message_tts_payload", fake_build):
            result = await dispatch_message_tts(cog, message, guild_defaults={}, active_prefix=".", forced_engine="gtts")

        self.assertTrue(result.enqueued)
        self.assertEqual(cog._state.last_text_channel_id, 99)
        self.assertEqual(len(cog.enqueue_calls), 1)
        self.assertEqual(cog.enqueue_calls[0][1].text, "teste")


if __name__ == "__main__":
    unittest.main()
