from __future__ import annotations

import asyncio
import time

import discord
from discord.ext import commands

from .audio import QueueItem
from .helpers import (
    EDGE_DEFAULT_VOICE,
    GTTS_DEFAULT_LANGUAGE,
    clean_text,
    validate_engine,
    validate_language,
    validate_pitch,
    validate_rate,
    validate_voice,
)


class TTSVoiceEventsMixin:
    """Mixin legado de compatibilidade. Mantido enxuto para evitar regressões em reaproveitamento futuro."""
    bot: commands.Bot
    edge_voice_names: set[str]
    gtts_languages: dict[str, str]

    def _get_warm_connect_tasks(self) -> dict[int, asyncio.Task]:
        tasks = getattr(self, "_warm_connect_tasks", None)
        if tasks is None:
            tasks = {}
            setattr(self, "_warm_connect_tasks", tasks)
        return tasks

    def _cleanup_warm_connect_task(self, guild_id: int) -> None:
        self._get_warm_connect_tasks().pop(guild_id, None)

    def _schedule_warm_connect(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        tasks = self._get_warm_connect_tasks()
        current = tasks.get(guild.id)
        if current is not None and not current.done():
            return

        async def _runner():
            try:
                await self._ensure_connected(guild, voice_channel)
            finally:
                self._cleanup_warm_connect_task(guild.id)

        tasks[guild.id] = asyncio.create_task(_runner())

    def _get_recent_message_cache(self) -> dict[int, float]:
        cache = getattr(self, "_recent_event_message_ids", None)
        if cache is None:
            cache = {}
            setattr(self, "_recent_event_message_ids", cache)
        return cache

    def _mark_message_seen(self, message_id: int) -> None:
        cache = self._get_recent_message_cache()
        now = time.monotonic()
        cache[message_id] = now

        cutoff = now - 30.0
        stale = [mid for mid, ts in cache.items() if ts < cutoff]
        for mid in stale:
            cache.pop(mid, None)

    def _was_message_seen(self, message_id: int) -> bool:
        cache = self._get_recent_message_cache()
        ts = cache.get(message_id)
        if ts is None:
            return False
        if time.monotonic() - ts > 30.0:
            cache.pop(message_id, None)
            return False
        return True

    def _is_voice_bot_blocking(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> bool:
        if not BLOCK_VOICE_BOT_ID:
            return False
        member = guild.get_member(BLOCK_VOICE_BOT_ID)
        return bool(member and member.voice and member.voice.channel and member.voice.channel.id == voice_channel.id)

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> bool:
        if not BLOCK_VOICE_BOT_ID:
            return False
        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return False
        return db.block_voice_bot_enabled(guild.id) and self._is_voice_bot_blocking(guild, voice_channel)

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        channel = vc.channel
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        if await self._should_block_for_voice_bot(guild, channel):
            await self._disconnect_and_clear(guild)

    async def _disconnect_and_clear(self, guild: discord.Guild):
        vc = guild.voice_client
        state = self._get_state(guild.id)

        while not state.queue.empty():
            try:
                state.queue.get_nowait()
                state.queue.task_done()
            except Exception:
                break

        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    async def _ensure_connected(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient | None:
        vc = guild.voice_client

        if vc and vc.is_connected() and vc.channel and vc.channel.id == voice_channel.id:
            return vc

        if vc and vc.is_connected() and vc.channel and vc.channel.id != voice_channel.id:
            try:
                await vc.move_to(voice_channel)
                print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                return vc
            except Exception:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass

        try:
            # Mantém o mesmo contrato do cog principal: somente o controlador
            # do bot pode reconectar; o loop interno do discord.py fica inativo.
            connected = await voice_channel.connect(self_deaf=True, reconnect=False)
            print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
            return connected
        except Exception as e:
            msg = str(e).lower().strip()
            current = guild.voice_client

            if current and current.is_connected():
                if current.channel and current.channel.id == voice_channel.id:
                    return current
                try:
                    await current.move_to(voice_channel)
                    print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                    return current
                except Exception:
                    pass

            if "already connected" in msg and current and current.is_connected():
                return current

            print(f"[tts_voice] Erro ao conectar na call da guild {guild.id}: {e}")
            return None

    async def _enqueue_message(
        self,
        message: discord.Message,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *,
        raw_text: str,
        resolved: dict,
    ):
        if not message.guild:
            return

        text = clean_text(raw_text)
        if not text:
            return

        engine = validate_engine(resolved.get("engine", "gtts"))
        item = QueueItem(
            guild_id=message.guild.id,
            channel_id=voice_channel.id,
            author_id=message.author.id,
            text=text,
            engine=engine,
            voice=validate_voice(resolved.get("voice", EDGE_DEFAULT_VOICE), self.edge_voice_names),
            language=validate_language(resolved.get("language", GTTS_DEFAULT_LANGUAGE), self.gtts_languages),
            rate=validate_rate(resolved.get("rate", "+0%")),
            pitch=validate_pitch(resolved.get("pitch", "+0Hz")),
        )

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = message.channel.id
        items = self._expand_tts_queue_item(item) if hasattr(self, "_expand_tts_queue_item") else [item]
        ok = False
        dropped = 0
        deduplicated = False
        for queue_item in items:
            item_ok, item_dropped, item_deduplicated = await self._enqueue_tts_item(message.guild.id, queue_item)
            ok = ok or bool(item_ok)
            dropped += int(item_dropped or 0)
            deduplicated = deduplicated or bool(item_deduplicated)
        self._ensure_worker(message.guild.id)

        print(
            f"[tts_voice] trigger TTS | guild={message.guild.id} "
            f"channel_type={type(message.channel).__name__} user={message.author.id} raw={message.content!r}"
        )
        print(
            f"[tts_voice] enfileirada | guild={message.guild.id} user={message.author.id} "
            f"canal_voz={voice_channel.id} engine={item.engine} partes={len(items)} dropped={dropped} deduplicated={deduplicated}"
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not TTS_ENABLED or message.author.bot or not message.guild or not message.content:
            return

        if self._was_message_seen(message.id):
            return
        self._mark_message_seen(message.id)

        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return

        guild_defaults = db.get_guild_tts_defaults(message.guild.id)
        tts_prefix = str((guild_defaults or {}).get("tts_prefix", ",") or ",")

        if not tts_prefix:
            tts_prefix = ","

        # fast reject before doing anything heavier
        if not message.content.startswith(tts_prefix):
            return

        raw_text = message.content[len(tts_prefix):].strip()
        if not raw_text:
            return

        author_voice = getattr(message.author, "voice", None)
        if not author_voice or not author_voice.channel:
            return

        voice_channel = author_voice.channel
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return

        if await self._should_block_for_voice_bot(message.guild, voice_channel):
            await self._disconnect_if_blocked(message.guild)
            return

        resolved = db.resolve_tts(message.guild.id, message.author.id)

        try:
            vc = message.guild.voice_client
            if not (vc and vc.is_connected() and vc.channel and vc.channel.id == voice_channel.id):
                self._schedule_warm_connect(message.guild, voice_channel)
        except Exception:
            pass

        await self._enqueue_message(
            message,
            voice_channel,
            raw_text=raw_text,
            resolved=resolved,
        )
