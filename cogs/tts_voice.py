import os
import re
import asyncio
import tempfile
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands

import edge_tts
from gtts import gTTS

from config import TTS_ENABLED, BLOCK_VOICE_BOT_ID

RATE_RE = re.compile(r"^[+-]?\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]?\d{1,4}Hz$")


class TtsVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}

        self._voices_cache: Optional[List[dict]] = None
        self._voices_cache_lock = asyncio.Lock()

        self._seen_messages: set[int] = set()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def _mark_seen(self, message_id: int, ttl: int = 10) -> bool:
        if message_id in self._seen_messages:
            return True

        self._seen_messages.add(message_id)

        async def cleanup():
            await asyncio.sleep(ttl)
            self._seen_messages.discard(message_id)

        asyncio.create_task(cleanup())
        return False

    async def _ensure_voices_cache(self):
        if self._voices_cache is not None:
            return
        async with self._voices_cache_lock:
            if self._voices_cache is None:
                self._voices_cache = await edge_tts.list_voices()

    async def _reply_temp_error(self, message: discord.Message, content: str, delay: int = 7):
        try:
            bot_msg = await message.reply(content)
        except Exception:
            return

        async def cleanup():
            await asyncio.sleep(delay)
            try:
                await bot_msg.delete()
            except Exception:
                pass
            try:
                await message.delete()
            except Exception:
                pass

        asyncio.create_task(cleanup())

    async def _synthesize_edge(self, text: str, out_path: str, *, voice: str, rate: str, pitch: str):
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume="+0%",
        )
        await communicate.save(out_path)

    async def _synthesize_gtts(self, text: str, out_path: str):
        tts = gTTS(text=text, lang="pt", tld="com.br")
        tts.save(out_path)

    async def _speak_from_message(self, message: discord.Message, text: str):
        if not TTS_ENABLED:
            await self._reply_temp_error(message, "❌ O TTS está desativado no momento.")
            return

        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)
        if not vs or not vs.channel or not isinstance(vs.channel, discord.VoiceChannel):
            await self._reply_temp_error(message, "⚠️ Você precisa estar em um canal de voz para eu falar.")
            return

        channel: discord.VoiceChannel = vs.channel

        if BLOCK_VOICE_BOT_ID and any(m.id == BLOCK_VOICE_BOT_ID for m in channel.members):
            await self._reply_temp_error(message, "❌ Já existe um bot de voz nesta call")
            return

        me = message.guild.me or message.guild.get_member(self.bot.user.id)
        perms = channel.permissions_for(me)
        if not perms.connect:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Conectar** nesse canal de voz.")
            return
        if not perms.speak:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Falar** nesse canal de voz.")
            return

        vc = message.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect()
            elif vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
        except Exception as e:
            await self._reply_temp_error(message, f"❌ Não consegui entrar na call. Erro: `{type(e).__name__}` — `{e}`")
            return

        text = (text or "").strip()
        if not text:
            await self._reply_temp_error(message, "⚠️ Escreva algo depois da vírgula. Ex: `,olá`")
            return
        if len(text) > 250:
            text = text[:250]

        cfg = self.bot.settings_db.resolve_tts(message.guild.id, message.author.id)
        engine = cfg["engine"]
        voice = cfg["voice"]
        rate = cfg["rate"]
        pitch = cfg["pitch"]

        lock = self._lock(message.guild.id)
        async with lock:
            if vc.is_playing():
                vc.stop()

            tmp = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                    tmp = fp.name

                if engine == "gtts":
                    await self._synthesize_gtts(text, tmp)
                else:
                    await self._synthesize_edge(text, tmp, voice=voice, rate=rate, pitch=pitch)

                vc.play(discord.FFmpegPCMAudio(tmp))
                while vc.is_playing():
                    await asyncio.sleep(0.2)

            except Exception as e:
                await self._reply_temp_error(message, f"❌ Falha no TTS ({engine}): `{type(e).__name__}` — `{e}`")
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if await self._mark_seen(message.id):
            return

        if not message.content.startswith(","):
            return

        text = message.content[1:]
        await self._speak_from_message(message, text)

    async def _reply(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    def _kick_check(self, interaction: discord.Interaction) -> bool:
        m = interaction.user
        return isinstance(m, discord.Member) and bool(m.guild_permissions.kick_members)

    async def voice_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            await self._ensure_voices_cache()
        except Exception:
            return []

        q = (current or "").lower().strip()
        voices = self._voices_cache or []
        names = sorted({
            v.get("ShortName")
            for v in voices
            if v.get("ShortName") and (v.get("Locale", "") or "").lower() in ("pt-br", "pt-pt")
        })

        if q:
            names = [n for n in names if q in n.lower()]

        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    @app_commands.command(name="voices", description="Lista vozes do edge-tts em Português (PT-BR/PT-PT).")
    @app_commands.describe(locale="Filtrar por: br (pt-BR), pt (pt-PT) ou all")
    async def voices(self, interaction: discord.Interaction, locale: Optional[str] = None):
        try:
            await self._ensure_voices_cache()
        except Exception as e:
            await self._reply(interaction, f"❌ Não consegui listar vozes agora. `{type(e).__name__}`", ephemeral=True)
            return

        loc = (locale or "all").lower().strip()
        voices = self._voices_cache or []

        if loc == "br":
            voices = [v for v in voices if (v.get("Locale", "") or "").lower() == "pt-br"]
            title = "Vozes disponíveis do edge-tts (pt-BR)"
        elif loc == "pt":
            voices = [v for v in voices if (v.get("Locale", "") or "").lower() == "pt-pt"]
            title = "Vozes disponíveis do edge-tts (pt-PT)"
        else:
            voices = [v for v in voices if (v.get("Locale", "") or "").lower() in ("pt-br", "pt-pt")]
            title = "Vozes disponíveis do edge-tts (Português)"

        names = sorted({v.get("ShortName") for v in voices if v.get("ShortName")})
        if not names:
            await self._reply(interaction, "⚠️ Não encontrei vozes PT-BR/PT-PT no edge-tts.", ephemeral=True)
            return

        shown = names[:40]
        extra = f"\n… e mais **{len(names) - len(shown)}**." if len(names) > len(shown) else ""

        msg = (
            f"**{title}**\n```"
            + "\n".join(shown)
            + "```"
            + extra
            + "\n\nUse `/set_voice` ou `/set_server_voice` para escolher uma voz do edge-tts."
            + "\nUse `/set_tts_engine` para alternar entre **gtts** e **edge-tts**."
        )
        await self._reply(interaction, msg, ephemeral=True)

    @app_commands.command(name="set_tts_engine", description="Define seu motor de TTS: gtts ou edge-tts.")
    @app_commands.describe(engine="Escolha entre gtts e edge")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)

        engine = (engine or "").lower().strip()
        if engine not in ("edge", "gtts"):
            return await self._reply(interaction, "⚠️ Use `edge` ou `gtts`.", ephemeral=True)

        await self.bot.settings_db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)

        if engine == "gtts":
            await self._reply(
                interaction,
                "✅ Seu motor foi alterado para **gtts**.\n`voice`, `speed` e `voice_tone` não têm efeito real nele.",
                ephemeral=True,
            )
        else:
            await self._reply(interaction, "✅ Seu motor foi alterado para **edge-tts**.", ephemeral=True)

    @app_commands.command(name="set_server_tts_engine", description="Define o motor padrão do servidor: gtts ou edge-tts.")
    @app_commands.describe(engine="Escolha entre gtts e edge")
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        if not self._kick_check(interaction):
            return await self._reply(interaction, "❌ Você não tem permissão (precisa de **Expulsar membros**).", ephemeral=True)

        engine = (engine or "").lower().strip()
        if engine not in ("edge", "gtts"):
            return await self._reply(interaction, "⚠️ Use `edge` ou `gtts`.", ephemeral=True)

        await self.bot.settings_db.set_guild_tts_defaults(interaction.guild.id, engine=engine)

        if engine == "gtts":
            await self._reply(
                interaction,
                "✅ O motor padrão do servidor foi alterado para **gtts**.\n`voice`, `speed` e `voice_tone` não têm efeito real nele.",
                ephemeral=True,
            )
        else:
            await self._reply(interaction, "✅ O motor padrão do servidor foi alterado para **edge-tts**.", ephemeral=True)

    @app_commands.command(name="set_voice", description="Define sua voz do edge-tts.")
    @app_commands.describe(voice="Nome da voz do edge-tts (use /voices)")
    @app_commands.autocomplete(voice=voice_autocomplete)
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        await self.bot.settings_db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice.strip())
        await self._reply(interaction, "✅ Sua voz do edge-tts foi atualizada.", ephemeral=True)

    @app_commands.command(name="set_speed", description="Define sua velocidade de fala (funciona só no edge-tts).")
    @app_commands.describe(speed="Ex: +10%, -10%, +0%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        speed = speed.strip()
        if not RATE_RE.match(speed):
            return await self._reply(interaction, "⚠️ Formato inválido. Use `+10%`, `-10%`, `+0%`.", ephemeral=True)
        await self.bot.settings_db.set_user_tts(interaction.guild.id, interaction.user.id, rate=speed)
        await self._reply(interaction, "✅ Sua velocidade foi atualizada. (só funciona no edge-tts)", ephemeral=True)

    @app_commands.command(name="set_voice_tone", description="Define seu tom de voz (funciona só no edge-tts).")
    @app_commands.describe(tone="Ex: +50Hz, -50Hz, +0Hz")
    async def set_voice_tone(self, interaction: discord.Interaction, tone: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        tone = tone.strip()
        if not PITCH_RE.match(tone):
            return await self._reply(interaction, "⚠️ Formato inválido. Use `+50Hz`, `-50Hz`, `+0Hz`.", ephemeral=True)
        await self.bot.settings_db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=tone)
        await self._reply(interaction, "✅ Seu tom foi atualizado. (só funciona no edge-tts)", ephemeral=True)

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do servidor no edge-tts.")
    @app_commands.describe(voice="Nome da voz do edge-tts (use /voices)")
    @app_commands.autocomplete(voice=voice_autocomplete)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        if not self._kick_check(interaction):
            return await self._reply(interaction, "❌ Você não tem permissão (precisa de **Expulsar membros**).", ephemeral=True)
        await self.bot.settings_db.set_guild_tts_defaults(interaction.guild.id, voice=voice.strip())
        await self._reply(interaction, "✅ A voz padrão do servidor (edge-tts) foi atualizada.", ephemeral=True)

    @app_commands.command(name="set_server_speed", description="Define a velocidade padrão do servidor (funciona só no edge-tts).")
    @app_commands.describe(speed="Ex: +10%, -10%, +0%")
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        if not self._kick_check(interaction):
            return await self._reply(interaction, "❌ Você não tem permissão (precisa de **Expulsar membros**).", ephemeral=True)

        speed = speed.strip()
        if not RATE_RE.match(speed):
            return await self._reply(interaction, "⚠️ Formato inválido. Use `+10%`, `-10%`, `+0%`.", ephemeral=True)

        await self.bot.settings_db.set_guild_tts_defaults(interaction.guild.id, rate=speed)
        await self._reply(interaction, "✅ A velocidade padrão do servidor foi atualizada. (só funciona no edge-tts)", ephemeral=True)

    @app_commands.command(name="set_server_voice_tone", description="Define o tom padrão do servidor (funciona só no edge-tts).")
    @app_commands.describe(tone="Ex: +50Hz, -50Hz, +0Hz")
    async def set_server_voice_tone(self, interaction: discord.Interaction, tone: str):
        if not interaction.guild:
            return await self._reply(interaction, "❌ Use em um servidor.", ephemeral=True)
        if not self._kick_check(interaction):
            return await self._reply(interaction, "❌ Você não tem permissão (precisa de **Expulsar membros**).", ephemeral=True)

        tone = tone.strip()
        if not PITCH_RE.match(tone):
            return await self._reply(interaction, "⚠️ Formato inválido. Use `+50Hz`, `-50Hz`, `+0Hz`.", ephemeral=True)

        await self.bot.settings_db.set_guild_tts_defaults(interaction.guild.id, pitch=tone)
        await self._reply(interaction, "✅ O tom padrão do servidor foi atualizado. (só funciona no edge-tts)", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) == 0:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TtsVoiceCog(bot))
