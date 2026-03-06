from __future__ import annotations

import discord
from discord.ext import commands

from config import BLOCK_VOICE_BOT_ID, TTS_ENABLED
from tts_audio import QueueItem
from tts_helpers import clean_text, validate_engine, validate_pitch, validate_rate, validate_voice, EDGE_DEFAULT_VOICE


class TTSVoiceEventsMixin:
    bot: commands.Bot
    edge_voice_names: set[str]

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
        enabled = db.block_voice_bot_enabled(guild.id)
        if not enabled:
            return False
        return self._is_voice_bot_blocking(guild, voice_channel)

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        channel = vc.channel
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        blocked = await self._should_block_for_voice_bot(guild, channel)
        if blocked:
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
        if vc and vc.channel and vc.channel.id == voice_channel.id:
            return vc
        if vc and vc.channel and vc.channel.id != voice_channel.id:
            try:
                await vc.move_to(voice_channel)
                return vc
            except Exception:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        try:
            return await voice_channel.connect(self_deaf=True)
        except Exception as e:
            print(f"[tts_voice] Erro ao conectar na call da guild {guild.id}: {e}")
            return None

    async def _enqueue_message(self, message: discord.Message, voice_channel: discord.VoiceChannel | discord.StageChannel):
        if not message.guild:
            return
        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return
        text = clean_text(message.content)
        if not text:
            return
        resolved = db.resolve_tts(message.guild.id, message.author.id)
        item = QueueItem(
            guild_id=message.guild.id,
            channel_id=voice_channel.id,
            author_id=message.author.id,
            text=text,
            engine=validate_engine(resolved.get("engine", "gtts")),
            voice=validate_voice(resolved.get("voice", EDGE_DEFAULT_VOICE), self.edge_voice_names),
            rate=validate_rate(resolved.get("rate", "+0%")),
            pitch=validate_pitch(resolved.get("pitch", "+0Hz")),
        )
        state = self._get_state(message.guild.id)
        state.last_text_channel_id = message.channel.id
        await state.queue.put(item)
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not TTS_ENABLED:
            return
        if message.author.bot or not message.guild:
            return
        if not message.content or "," not in message.content:
            return
        author_voice = getattr(message.author, "voice", None)
        if not author_voice or not author_voice.channel:
            return
        voice_channel = author_voice.channel
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            await self._disconnect_if_blocked(message.guild)
            return
        await self._enqueue_message(message, voice_channel)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not member.guild:
            return
        guild = member.guild
        vc = guild.voice_client
        if vc and vc.channel and isinstance(vc.channel, (discord.VoiceChannel, discord.StageChannel)):
            humans = [m for m in vc.channel.members if not m.bot]
            if len(humans) == 0:
                await self._disconnect_and_clear(guild)
                print(f"[tts_voice] Saindo da call na guild {guild.id} por não haver humanos no canal.")
                return
        if not BLOCK_VOICE_BOT_ID:
            return
        if member.id != BLOCK_VOICE_BOT_ID:
            return
        db = getattr(self.bot, "settings_db", None)
        if db is None or not db.block_voice_bot_enabled(guild.id):
            return
        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        current_channel = vc.channel
        if not isinstance(current_channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        joined_same_channel = after.channel and after.channel.id == current_channel.id
        moved_to_same_channel = before.channel != after.channel and after.channel and after.channel.id == current_channel.id
        if joined_same_channel or moved_to_same_channel:
            await self._disconnect_if_blocked(guild)
