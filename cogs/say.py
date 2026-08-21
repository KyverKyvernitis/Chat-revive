from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

LOG = logging.getLogger("bot.say")

SESSION_SECONDS = 30.0
WEBHOOK_NAME = "say-temp"
MESSAGE_CHUNK_LIMIT = 2000


@dataclass(slots=True)
class SayIdentity:
    mode: str
    name: str
    avatar_url: str | None = None
    member_id: int | None = None


@dataclass(slots=True)
class SaySession:
    guild_id: int
    channel_id: int
    author_id: int
    identity: SayIdentity
    webhook: discord.Webhook
    thread: discord.Thread | None
    expires_at: float
    task: asyncio.Task | None = None
    relayed_count: int = 0


class _SayModal(discord.ui.Modal):
    """Modal principal do /say usando componentes reais do Discord UI Kit."""

    GUIDE_TEXT = (
        "Selecione um usuário para usar nome/avatar dele. "
        "Sem usuário, usa o servidor."
    )

    def __init__(self, cog: "SayCog", interaction: discord.Interaction):
        super().__init__(title="Falar por webhook")
        self.cog = cog
        self.source = interaction

        # O TextDisplay deixa o guia visível no topo em clientes que suportam
        # componentes V2 em modais. Se a lib/cliente não aceitar, as descrições
        # dos campos abaixo continuam explicando o fluxo sem quebrar o modal.
        text_display = getattr(discord.ui, "TextDisplay", None)
        if text_display is not None:
            with contextlib.suppress(Exception):
                self.add_item(text_display(self.GUIDE_TEXT))

        self.user_select = discord.ui.UserSelect(
            custom_id="say_user",
            placeholder="Selecionar usuário",
            min_values=0,
            max_values=1,
            required=False,
        )

        self.message_input = discord.ui.TextInput(
            custom_id="say_text",
            placeholder="Texto para enviar agora.",
            required=False,
            max_length=2000,
            style=discord.TextStyle.paragraph,
        )

        self.session_checkbox = discord.ui.Checkbox(custom_id="say_30s", default=False)

        self.add_item(
            discord.ui.Label(
                text="Usuário",
                description="Opcional.",
                component=self.user_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Mensagem",
                description="Deixe vazio para usar só o modo de 30 segundos.",
                component=self.message_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Modo 30 segundos",
                description="Reenvia suas próximas mensagens e apaga as originais.",
                component=self.session_checkbox,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        selected_member = self.user_select.values[0] if self.user_select.values else None
        if isinstance(selected_member, discord.User) and interaction.guild is not None:
            selected_member = interaction.guild.get_member(selected_member.id) or selected_member

        await self.cog._handle_modal_submit(
            interaction,
            selected_member=selected_member,
            initial_text=str(self.message_input.value or ""),
            enable_session=bool(self.session_checkbox.value),
        )


class SayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sessions_by_channel: dict[tuple[int, int], SaySession] = {}
        self._sessions_by_author: dict[tuple[int, int], SaySession] = {}
        self._lock = asyncio.Lock()

    def cog_unload(self) -> None:
        for session in list(self._sessions_by_channel.values()):
            if session.task and not session.task.done():
                session.task.cancel()
            self.bot.loop.create_task(self._delete_webhook_quietly(session.webhook))
        self._sessions_by_channel.clear()
        self._sessions_by_author.clear()

    async def _delete_webhook_quietly(self, webhook: discord.Webhook | None) -> None:
        if webhook is None:
            return
        with contextlib.suppress(Exception):
            await webhook.delete(reason="/say temporário encerrado")

    def _is_staff_member(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return bool(
            member.guild.owner_id == member.id
            or perms.administrator
            or perms.manage_guild
            or perms.manage_messages
            or perms.manage_webhooks
        )

    async def _check_staff(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._send_ephemeral(interaction, "Use esse comando dentro de um servidor.")
            return False
        if not self._is_staff_member(interaction.user):
            await self._send_ephemeral(interaction, "Apenas staff pode usar esse comando.")
            return False
        return True

    def _channel_key(self, guild_id: int, channel_id: int) -> tuple[int, int]:
        return int(guild_id), int(channel_id)

    def _author_key(self, guild_id: int, author_id: int) -> tuple[int, int]:
        return int(guild_id), int(author_id)

    def _target_channel(self, channel: discord.abc.GuildChannel | discord.Thread | None):
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            return parent, channel
        return channel, None

    def _bot_member(self, guild: discord.Guild) -> discord.Member | None:
        user = self.bot.user
        if user is None:
            return None
        return guild.me or guild.get_member(user.id)

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            LOG.exception("falha ao responder ephemeral do /say")

    async def _ack_and_hide(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            with contextlib.suppress(Exception):
                await interaction.delete_original_response()
        except Exception:
            LOG.debug("não consegui ocultar resposta ephemeral do /say", exc_info=True)

    def _identity_for_server(self, guild: discord.Guild) -> SayIdentity:
        avatar = guild.icon.url if guild.icon else None
        return SayIdentity(mode="server", name=guild.name[:80] or "Servidor", avatar_url=avatar)

    def _identity_for_user(self, user: discord.Member | discord.User) -> SayIdentity:
        name = getattr(user, "display_name", None) or getattr(user, "global_name", None) or getattr(user, "name", None) or "Usuário"
        avatar = None
        with contextlib.suppress(Exception):
            avatar = user.display_avatar.url
        return SayIdentity(mode="user", name=str(name)[:80], avatar_url=avatar, member_id=int(user.id))

    async def _create_temp_webhook(self, channel, *, reason: str) -> tuple[discord.Webhook, discord.Thread | None]:
        webhook_channel, thread = self._target_channel(channel)
        if webhook_channel is None or not hasattr(webhook_channel, "create_webhook"):
            raise RuntimeError("Esse canal não aceita webhooks.")
        webhook = await webhook_channel.create_webhook(name=WEBHOOK_NAME, reason=reason)
        return webhook, thread

    def _message_chunks(self, content: str) -> list[str]:
        text = str(content or "")
        if not text:
            return [""]
        return [text[i : i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(text), MESSAGE_CHUNK_LIMIT)]

    async def _send_as_webhook(
        self,
        webhook: discord.Webhook,
        identity: SayIdentity,
        *,
        content: str = "",
        files: list[discord.File] | None = None,
        thread: discord.Thread | None = None,
    ) -> None:
        chunks = self._message_chunks(content)
        avatar_url = identity.avatar_url or None
        first = True
        for chunk in chunks:
            kwargs = {
                "content": chunk,
                "username": identity.name,
                "allowed_mentions": discord.AllowedMentions.all(),
                "wait": False,
            }
            if avatar_url:
                kwargs["avatar_url"] = avatar_url
            if thread is not None:
                kwargs["thread"] = thread
            if first and files:
                kwargs["files"] = files
            await webhook.send(**kwargs)
            first = False

    async def _delete_message_quietly(self, message: discord.Message) -> None:
        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            await message.delete()

    async def _files_from_message(self, message: discord.Message) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in list(message.attachments or [])[:10]:
            try:
                files.append(await attachment.to_file())
            except Exception:
                LOG.warning("/say não conseguiu copiar anexo %s", getattr(attachment, "filename", "?"), exc_info=True)
        return files

    async def _send_initial_text(
        self,
        interaction: discord.Interaction,
        identity: SayIdentity,
        text: str,
        *,
        webhook: discord.Webhook | None = None,
        thread: discord.Thread | None = None,
        cleanup_webhook: bool = True,
    ) -> tuple[discord.Webhook | None, discord.Thread | None]:
        created = False
        if webhook is None:
            webhook, thread = await self._create_temp_webhook(interaction.channel, reason=f"/say usado por {interaction.user} ({interaction.user.id})")
            created = True
        try:
            await self._send_as_webhook(webhook, identity, content=text, thread=thread)
            return webhook, thread
        except Exception:
            if created:
                await self._delete_webhook_quietly(webhook)
            raise
        finally:
            if cleanup_webhook and webhook is not None:
                await self._delete_webhook_quietly(webhook)

    async def _start_session(
        self,
        interaction: discord.Interaction,
        identity: SayIdentity,
        *,
        webhook: discord.Webhook | None = None,
        thread: discord.Thread | None = None,
    ) -> SaySession:
        if interaction.guild is None or interaction.channel is None:
            raise RuntimeError("Servidor ou canal inválido.")
        guild_id = int(interaction.guild.id)
        channel_id = int(interaction.channel.id)
        author_id = int(interaction.user.id)
        channel_key = self._channel_key(guild_id, channel_id)
        author_key = self._author_key(guild_id, author_id)

        async with self._lock:
            existing_channel = self._sessions_by_channel.get(channel_key)
            if existing_channel and existing_channel.expires_at > time.monotonic():
                raise RuntimeError("Já existe uma sessão /say ativa neste canal.")
            existing_author = self._sessions_by_author.get(author_key)
            if existing_author and existing_author.expires_at > time.monotonic():
                raise RuntimeError("Você já tem uma sessão /say ativa.")

            if webhook is None:
                webhook, thread = await self._create_temp_webhook(
                    interaction.channel,
                    reason=f"/say 30s usado por {interaction.user} ({interaction.user.id})",
                )
            session = SaySession(
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                identity=identity,
                webhook=webhook,
                thread=thread,
                expires_at=time.monotonic() + SESSION_SECONDS,
            )
            session.task = asyncio.create_task(self._expire_session_later(session))
            self._sessions_by_channel[channel_key] = session
            self._sessions_by_author[author_key] = session
            return session

    async def _expire_session_later(self, session: SaySession) -> None:
        try:
            await asyncio.sleep(max(0.0, session.expires_at - time.monotonic()))
        finally:
            await self._finish_session(session)

    async def _finish_session(self, session: SaySession) -> None:
        async with self._lock:
            channel_key = self._channel_key(session.guild_id, session.channel_id)
            author_key = self._author_key(session.guild_id, session.author_id)
            if self._sessions_by_channel.get(channel_key) is session:
                self._sessions_by_channel.pop(channel_key, None)
            if self._sessions_by_author.get(author_key) is session:
                self._sessions_by_author.pop(author_key, None)
        await self._delete_webhook_quietly(session.webhook)

    async def _handle_modal_submit(
        self,
        interaction: discord.Interaction,
        *,
        selected_member: discord.Member | discord.User | None,
        initial_text: str,
        enable_session: bool,
    ) -> None:
        if not await self._check_staff(interaction):
            return
        if interaction.guild is None or interaction.channel is None:
            await self._send_ephemeral(interaction, "Servidor ou canal inválido.")
            return

        text = str(initial_text or "").strip()
        identity = self._identity_for_user(selected_member) if selected_member is not None else self._identity_for_server(interaction.guild)

        if not text and not enable_session:
            await self._send_ephemeral(interaction, "Nada para enviar. Escreva uma mensagem ou marque “Modo 30 segundos”.")
            return

        webhook: discord.Webhook | None = None
        thread: discord.Thread | None = None
        try:
            if enable_session:
                webhook, thread = await self._create_temp_webhook(
                    interaction.channel,
                    reason=f"/say usado por {interaction.user} ({interaction.user.id})",
                )
                if text:
                    await self._send_as_webhook(webhook, identity, content=text, thread=thread)
                await self._start_session(interaction, identity, webhook=webhook, thread=thread)
                webhook = None
            else:
                await self._send_initial_text(interaction, identity, text, cleanup_webhook=True)
            await self._ack_and_hide(interaction)
        except discord.Forbidden:
            if webhook is not None:
                await self._delete_webhook_quietly(webhook)
            await self._send_ephemeral(interaction, "Sem permissão para criar webhook ou apagar mensagens neste canal.")
        except Exception as exc:
            if webhook is not None:
                await self._delete_webhook_quietly(webhook)
            LOG.exception("falha ao executar /say")
            await self._send_ephemeral(interaction, f"Não consegui executar /say: {type(exc).__name__}.")

    @app_commands.command(name="say", description="Fala pelo servidor ou por um usuário usando webhook temporário")
    @app_commands.default_permissions(manage_messages=True)
    async def say(self, interaction: discord.Interaction) -> None:
        if not await self._check_staff(interaction):
            return
        if interaction.guild is None:
            await self._send_ephemeral(interaction, "Use esse comando dentro de um servidor.")
            return
        bot_member = self._bot_member(interaction.guild)
        channel = interaction.channel
        if bot_member is not None and hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(bot_member)  # type: ignore[attr-defined]
            if not getattr(perms, "manage_webhooks", False):
                await self._send_ephemeral(interaction, "Preciso de Gerenciar webhooks neste canal.")
                return
            if not getattr(perms, "manage_messages", False):
                await self._send_ephemeral(interaction, "Preciso de Gerenciar mensagens neste canal.")
                return

        try:
            await interaction.response.send_modal(_SayModal(self, interaction))
        except Exception:
            LOG.exception("falha ao abrir modal com componentes reais do /say")
            await self._send_ephemeral(interaction, "Não consegui abrir o modal do /say.")

    @commands.Cog.listener("on_message")
    async def _say_on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        antibot_guard = getattr(self.bot, "antibot_should_block_message", None)
        if callable(antibot_guard) and bool(antibot_guard(message)):
            return
        key = self._channel_key(message.guild.id, message.channel.id)
        session = self._sessions_by_channel.get(key)
        if session is None:
            return
        if session.expires_at <= time.monotonic():
            await self._finish_session(session)
            return
        if int(message.author.id) != int(session.author_id):
            return
        if not str(message.content or "").strip() and not message.attachments:
            await self._delete_message_quietly(message)
            return

        files = await self._files_from_message(message)
        try:
            await self._send_as_webhook(
                session.webhook,
                session.identity,
                content=str(message.content or ""),
                files=files,
                thread=session.thread,
            )
            session.relayed_count += 1
            await self._delete_message_quietly(message)
        except Exception:
            LOG.exception("falha ao reenviar mensagem pelo /say")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SayCog(bot))
