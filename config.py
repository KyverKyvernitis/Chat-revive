import os
import re
import asyncio
import tempfile
import discord
from discord.ext import commands

import edge_tts

from config import TTS_ENABLED, BLOCK_VOICE_BOT_ID


RATE_RE = re.compile(r"^[+-]?\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]?\d{1,4}Hz$")


class TtsVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks = {}  # lock por guild

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def _reply_temp(self, message: discord.Message, content: str, delay: int = 7, delete_user_msg: bool = False):
        """Responde e apaga a resposta (e opcionalmente a msg do usuário) depois de Xs."""
        try:
            bot_msg = await message.reply(content)
        except Exception:
            return

        async def _cleanup():
            await asyncio.sleep(delay)
            try:
                await bot_msg.delete()
            except Exception:
                pass
            if delete_user_msg:
                try:
                    await message.delete()
                except Exception:
                    pass

        asyncio.create_task(_cleanup())

    def _has_kick_perm(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return bool(perms.kick_members)

    async def _handle_set_command(self, message: discord.Message, cmd: str, value: str):
        """
        Comandos:
          ,set voice <VOICE>
          ,set speed <+10%>
          ,set voice_tone <+0Hz>

          ,set server_voice <VOICE>        (kick_members)
          ,set server_speed <+10%>         (kick_members)
          ,set server_voice_tone <+0Hz>    (kick_members)
        """
        if not message.guild:
            return

        cmd = cmd.lower().strip()
        value = (value or "").strip()

        # mapeia "speed" -> rate, "voice_tone" -> pitch
        is_server = cmd.startswith("server_")
        key = cmd.replace("server_", "")

        # permissão pros comandos server_*
        if is_server:
            if not isinstance(message.author, discord.Member) or not self._has_kick_perm(message.author):
                await self._reply_temp(
                    message,
                    "❌ Você não tem permissão para isso (precisa de **Expulsar membros**).",
                    delay=7,
                    delete_user_msg=False,
                )
                return

        # validação
        if key == "voice":
            if not value:
                await self._reply_temp(message, "⚠️ Use: `,set voice <NOME_DA_VOZ>`", 7, False)
                return

        elif key == "speed":
            if not RATE_RE.match(value):
                await self._reply_temp(message, "⚠️ Use: `,set speed +10%` (ou `-10%`, `+0%`)", 7, False)
                return

        elif key == "voice_tone":
            if not PITCH_RE.match(value):
                await self._reply_temp(message, "⚠️ Use: `,set voice_tone +0Hz` (ou `-50Hz`, `+50Hz`)", 7, False)
                return
        else:
            await self._reply_temp(message, "⚠️ Comando inválido. Use `,set voice|speed|voice_tone ...`", 7, False)
            return

        # salva
        gid = message.guild.id
        uid = message.author.id

        if is_server:
            if key == "voice":
                await self.bot.settings_db.set_guild_tts_defaults(gid, voice=value)
            elif key == "speed":
                await self.bot.settings_db.set_guild_tts_defaults(gid, rate=value)
            elif key == "voice_tone":
                await self.bot.settings_db.set_guild_tts_defaults(gid, pitch=value)

            await self._reply_temp(message, "✅ Configuração padrão do servidor atualizada.", 7, delete_user_msg=True)
        else:
            if key == "voice":
                await self.bot.settings_db.set_user_tts(gid, uid, voice=value)
            elif key == "speed":
                await self.bot.settings_db.set_user_tts(gid, uid, rate=value)
            elif key == "voice_tone":
                await self.bot.settings_db.set_user_tts(gid, uid, pitch=value)

            await self._reply_temp(message, "✅ Sua configuração de voz foi atualizada.", 7, delete_user_msg=True)

    async def _synthesize_edge(self, text: str, out_path: str, *, voice: str, rate: str, pitch: str):
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume="+0%",
        )
        await communicate.save(out_path)

    async def speak(self, message: discord.Message, text: str):
        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)
        if not vs or not vs.channel:
            await self._reply_temp(message, "⚠️ Você precisa estar em um canal de voz para eu falar.", 7, True)
            return

        channel: discord.VoiceChannel = vs.channel

        # Bloqueio: não entra se o bot de voz informado estiver na call
        if BLOCK_VOICE_BOT_ID and any(m.id == BLOCK_VOICE_BOT_ID for m in channel.members):
            await self._reply_temp(message, "❌ Já existe um bot de voz nesta call", 7, True)
            return

        # Permissões
        me = message.guild.me or message.guild.get_member(self.bot.user.id)
        perms = channel.permissions_for(me)
        if not perms.connect:
            await self._reply_temp(message, "❌ Eu não tenho permissão **Conectar** nesse canal de voz.", 7, True)
            return
        if not perms.speak:
            await self._reply_temp(message, "❌ Eu não tenho permissão **Falar** nesse canal de voz.", 7, True)
            return

        # Conectar/mover
        vc = message.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect()
            elif vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
        except Exception as e:
            await self._reply_temp(message, f"❌ Não consegui entrar na call. Erro: `{type(e).__name__}` — `{e}`", 7, True)
            return

        async with self._lock(message.guild.id):
            if vc.is_playing():
                vc.stop()

            text = (text or "").strip()
            if not text:
                await self._reply_temp(message, "⚠️ Escreva algo depois da vírgula. Ex: `,olá`", 7, True)
                return
            if len(text) > 250:
                text = text[:250]

            # resolve config final (user -> server -> fallback)
            tts_cfg = self.bot.settings_db.resolve_tts(message.guild.id, message.author.id)
            voice = tts_cfg["voice"]
            rate = tts_cfg["rate"]
            pitch = tts_cfg["pitch"]

            tmp = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                    tmp = fp.name

                try:
                    await self._synthesize_edge(text, tmp, voice=voice, rate=rate, pitch=pitch)
                except Exception as e:
                    await self._reply_temp(message, f"❌ Falhei ao gerar a voz (edge-tts). `{type(e).__name__}`", 7, True)
                    return

                try:
                    vc.play(discord.FFmpegPCMAudio(tmp))
                except Exception as e:
                    await self._reply_temp(message, f"❌ Não consegui tocar o áudio. `{type(e).__name__}` — `{e}`", 7, True)
                    return

                while vc.is_playing():
                    await asyncio.sleep(0.2)

            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not TTS_ENABLED:
            return
        if message.author.bot or not message.guild:
            return
        if not message.content.startswith(","):
            return

        body = message.content[1:].strip()
        if not body:
            return

        # comandos ,set ...
        if body.lower().startswith("set "):
            # formato: set <cmd> <value...>
            parts = body.split(maxsplit=2)
            if len(parts) < 3:
                await self._reply_temp(message, "⚠️ Use: `,set voice|speed|voice_tone <valor>`", 7, False)
                return
            _, cmd, value = parts[0], parts[1], parts[2]
            await self._handle_set_command(message, cmd, value)
            return

        # se não for comando, vira TTS normal
        await self.speak(message, body)

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
