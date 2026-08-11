from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

from .constants import (
    FLOW_CONFIRM_TICKET,
    FLOW_DIRECT_TICKET,
    FLOW_MODAL_CHANNEL,
    FLOW_MODAL_TICKET,
    KIND_OTHER,
    KIND_PARTNERSHIP,
    KIND_REPORT,
    KIND_SUGGESTION,
    PUBLIC_OPTIONS,
    TICKET_COMMAND_CLEANUP_DELAY,
    TICKET_COMMAND_COOLDOWN,
    default_ticket_config,
)
from .permissions import permission_overwrite_from_scope
from .transcripts import build_transcript_file
from .utils import (
    get_ticket_option,
    is_staff,
    member_display,
    now_iso,
    option_emoji_text,
    sanitize_config,
    slugify_channel_part,
    truncate,
)
from .webhooks import send_with_server_identity
from .views import (
    CloseConfirmView,
    PartnershipConfirmView,
    SimpleNoticeView,
    SuggestionMessageView,
    TicketChannelView,
    TicketEditorView,
    TicketPublicPanelView,
)

log = logging.getLogger(__name__)


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered_panel_views: set[tuple[int, int]] = set()
        self._registered_ticket_views: set[tuple[int, int, int]] = set()
        self._active_edit_messages: dict[tuple[int, int], tuple[int, int]] = {}
        self._publish_cd: dict[int, float] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._restore_views()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_views()

    async def _restore_views(self):
        for guild_id in sorted(self._known_guild_ids()):
            cfg = self._get_config(guild_id)
            panel = cfg.get("panel") or {}
            message_id = int(panel.get("message_id") or 0)
            if message_id:
                key = (guild_id, message_id)
                if key not in self._registered_panel_views:
                    try:
                        self.bot.add_view(TicketPublicPanelView(self, guild_id), message_id=message_id)
                        self._registered_panel_views.add(key)
                    except Exception as exc:
                        log.warning("[tickets] falha ao registrar painel persistente gid=%s mid=%s: %r", guild_id, message_id, exc)

            for ticket in cfg.get("active_tickets") or []:
                channel_id = int(ticket.get("channel_id") or 0)
                control_message_id = int(ticket.get("control_message_id") or 0)
                if not (channel_id and control_message_id):
                    continue
                key = (guild_id, channel_id, control_message_id)
                if key in self._registered_ticket_views:
                    continue
                try:
                    self.bot.add_view(TicketChannelView(self, guild_id, channel_id), message_id=control_message_id)
                    self._registered_ticket_views.add(key)
                except Exception as exc:
                    log.warning("[tickets] falha ao registrar view ticket gid=%s ch=%s mid=%s: %r", guild_id, channel_id, control_message_id, exc)

    def _known_guild_ids(self) -> set[int]:
        ids: set[int] = set()
        db = self.db
        if db is not None and hasattr(db, "guild_cache"):
            ids.update(int(gid) for gid in getattr(db, "guild_cache", {}).keys() if gid)
        ids.update(int(getattr(guild, "id", 0) or 0) for guild in getattr(self.bot, "guilds", []) if getattr(guild, "id", 0))
        return ids

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_tickets_config"):
            return sanitize_config(default_ticket_config())
        try:
            return sanitize_config(db.get_tickets_config(int(guild_id)))
        except Exception as exc:
            log.warning("[tickets] erro ao ler config gid=%s: %r", guild_id, exc)
            return sanitize_config(default_ticket_config())

    @staticmethod
    def _feature_active(cfg: dict[str, Any]) -> bool:
        panel = (cfg or {}).get("panel") or {}
        option_items = (cfg or {}).get("option_items")
        option_items = option_items if isinstance(option_items, dict) else {}
        has_flow = any(bool(item.get("enabled", True)) for item in option_items.values() if isinstance(item, dict))
        return bool((cfg or {}).get("feature_enabled", False) and int(panel.get("channel_id") or 0) and has_flow)

    async def _save_config(self, guild_id: int, cfg: dict[str, Any]):
        cfg = sanitize_config(cfg)
        db = self.db
        if db is None or not hasattr(db, "set_tickets_config"):
            log.warning("[tickets] settings_db sem set_tickets_config — config não foi salva")
            return
        try:
            await db.set_tickets_config(int(guild_id), cfg)
        except Exception:
            log.exception("[tickets] falha ao salvar config gid=%s", guild_id)

    def _find_active_ticket(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        cfg = self._get_config(guild_id)
        for ticket in cfg.get("active_tickets") or []:
            if int(ticket.get("channel_id") or 0) == int(channel_id):
                return dict(ticket)
        return None

    def _find_user_open_ticket(self, cfg: dict[str, Any], user_id: int) -> dict[str, Any] | None:
        if bool((cfg.get("options") or {}).get("allow_multiple_open_tickets", False)):
            return None
        guild = None
        for ticket in cfg.get("active_tickets") or []:
            if int(ticket.get("user_id") or 0) != int(user_id):
                continue
            channel_id = int(ticket.get("channel_id") or 0)
            if guild is None:
                guild = self.bot.get_guild(int(ticket.get("guild_id") or 0))
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                return dict(ticket)
        return None

    async def _delete_message_after(self, message: discord.Message | None, delay: float = TICKET_COMMAND_CLEANUP_DELAY):
        if message is None:
            return
        try:
            await asyncio.sleep(max(0.0, float(delay)))
            await message.delete()
        except Exception:
            pass

    async def _consume_publish_cooldown(self, guild_id: int) -> float:
        now = time.monotonic()
        last = float(self._publish_cd.get(guild_id, 0.0) or 0.0)
        if now - last < TICKET_COMMAND_COOLDOWN:
            return TICKET_COMMAND_COOLDOWN - (now - last)
        self._publish_cd[guild_id] = now
        return 0.0

    def _is_staff(self, member: discord.Member | None, guild_id: int) -> bool:
        return is_staff(member, self._get_config(guild_id))

    async def _delete_existing_panel(self, guild_id: int):
        cfg = self._get_config(guild_id)
        panel = cfg.get("panel") or {}
        channel_id = int(panel.get("channel_id") or 0)
        message_id = int(panel.get("message_id") or 0)
        if not (channel_id and message_id):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except Exception:
            pass

    async def _publish_panel(self, channel: discord.abc.Messageable, guild_id: int) -> discord.Message:
        view = TicketPublicPanelView(self, guild_id, feature_enabled=True)
        cfg = self._get_config(guild_id)
        message = await send_with_server_identity(cfg, channel, view=view, wait=True)
        if message is None:
            raise RuntimeError("não foi possível enviar a mensagem do painel")
        try:
            self.bot.add_view(view, message_id=int(message.id))
        except Exception:
            pass
        self._registered_panel_views.add((int(guild_id), int(message.id)))
        cfg = self._get_config(guild_id)
        cfg["feature_enabled"] = True
        cfg["panel"]["channel_id"] = int(getattr(channel, "id", 0) or 0)
        cfg["panel"]["message_id"] = int(message.id)
        await self._save_config(guild_id, cfg)
        return message

    async def _refresh_public_panel(self, guild_id: int) -> tuple[bool, str]:
        cfg = self._get_config(guild_id)
        panel = cfg.get("panel") or {}
        channel_id = int(panel.get("channel_id") or 0)
        message_id = int(panel.get("message_id") or 0)
        if not (channel_id and message_id):
            return False, "Nenhum painel publicado para atualizar. Use `ticket` no canal desejado."
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False, "Não encontrei o canal do painel salvo."
        try:
            message = await channel.fetch_message(message_id)
            view = TicketPublicPanelView(self, guild_id)
            await message.edit(view=view)
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception:
                pass
            self._registered_panel_views.add((guild_id, message_id))
            return True, "Painel publicado atualizado."
        except Exception as exc:
            log.warning("[tickets] falha ao atualizar painel gid=%s: %r", guild_id, exc)
            return False, "Não consegui atualizar o painel salvo. Talvez ele tenha sido apagado."

    async def _refresh_editor_message(self, guild_id: int, staff_id: int):
        entry = self._active_edit_messages.get((int(guild_id), int(staff_id)))
        if not entry:
            return
        channel_id, message_id = entry
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            view = TicketEditorView(self, guild_id, staff_id)
            view.message = message
            await message.edit(view=view)
        except Exception:
            pass

    async def _after_editor_modal_save(self, interaction: discord.Interaction, guild_id: int, staff_id: int, text: str):
        cfg = self._get_config(guild_id)
        panel = cfg.get("panel") or {}
        panel_is_published = bool(int(panel.get("channel_id") or 0) and int(panel.get("message_id") or 0))
        refresh_note = ""
        if panel_is_published:
            ok, message = await self._refresh_public_panel(guild_id)
            refresh_note = "\nPainel publicado atualizado." if ok else f"\nAviso: {message}"
        await self._refresh_editor_message(guild_id, staff_id)
        try:
            await interaction.response.send_message(f"{text}{refresh_note}", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"{text}{refresh_note}", ephemeral=True)
        except Exception:
            pass

    async def _handle_public_choice(self, interaction: discord.Interaction, value: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Esse painel só funciona dentro de servidor.", ephemeral=True)
            return
        cfg = self._get_config(guild.id)
        if not self._feature_active(cfg):
            await self._reply_interaction(interaction, "A função de tickets está desativada neste servidor.")
            return
        option = get_ticket_option(cfg, value)
        if not option:
            await interaction.response.send_message("Opção inválida ou desatualizada. Peça para a staff atualizar o painel.", ephemeral=True)
            return
        if not bool(option.get("enabled", True)):
            await interaction.response.send_message("Essa opção está desativada no momento.", ephemeral=True)
            return
        flow = str(option.get("flow") or FLOW_MODAL_TICKET)
        if flow == FLOW_CONFIRM_TICKET:
            await interaction.response.send_message(view=PartnershipConfirmView(self, guild.id, int(interaction.user.id), value), ephemeral=True)
            return
        if flow == FLOW_MODAL_CHANNEL:
            from .modals import GenericChannelModal
            await interaction.response.send_modal(GenericChannelModal(self, guild.id, value))
            return
        if flow == FLOW_MODAL_TICKET:
            from .modals import GenericTicketModal, ReportTicketModal
            if value == KIND_REPORT and bool(option.get("use_report_types", True)):
                await interaction.response.send_modal(ReportTicketModal(self, guild.id, value))
            else:
                await interaction.response.send_modal(GenericTicketModal(self, guild.id, value))
            return
        if flow == FLOW_DIRECT_TICKET:
            await self._create_ticket_from_interaction(interaction, kind=value, payload={})
            return
        await interaction.response.send_message("Fluxo dessa opção está inválido. Peça para a staff revisar em `ticketedit`.", ephemeral=True)

    async def _handle_suggestion_submission(self, interaction: discord.Interaction, *, title: str, body: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Servidor não encontrado.", ephemeral=True)
            return
        cfg = self._get_config(guild.id)
        if not self._feature_active(cfg):
            await self._reply_interaction(interaction, "A função de tickets está desativada neste servidor.")
            return
        channel_id = int((cfg.get("channels") or {}).get("suggestions_channel_id") or 0)
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            await interaction.response.send_message("Canal de sugestões não configurado. Peça para a staff configurar em `ticketedit`.", ephemeral=True)
            return
        try:
            message = await send_with_server_identity(
                cfg,
                channel,
                view=SuggestionMessageView(guild_id=guild.id, author_id=int(interaction.user.id), title=title, body=body),
                wait=True,
            )
            if message is None:
                raise RuntimeError("envio retornou vazio")
            await interaction.response.send_message("Sugestão enviada com sucesso.", ephemeral=True)
        except Exception as exc:
            log.warning("[tickets] falha ao enviar sugestão gid=%s: %r", guild.id, exc)
            await interaction.response.send_message("Não consegui enviar a sugestão no canal configurado.", ephemeral=True)


    async def _handle_channel_option_submission(self, interaction: discord.Interaction, *, option_id: str, title: str, body: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Servidor não encontrado.", ephemeral=True)
            return
        cfg = self._get_config(guild.id)
        if not self._feature_active(cfg):
            await self._reply_interaction(interaction, "A função de tickets está desativada neste servidor.")
            return
        option = get_ticket_option(cfg, option_id) or {}
        channel_id = int(option.get("target_channel_id") or 0) or int((cfg.get("channels") or {}).get("suggestions_channel_id") or 0)
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            await interaction.response.send_message("Canal destino não configurado. Configure um canal nessa opção ou em `ticketedit` > Canais.", ephemeral=True)
            return
        try:
            view = SuggestionMessageView(
                guild_id=guild.id,
                author_id=int(interaction.user.id),
                title=f"{option_emoji_text(option.get('emoji'), fallback='📨')} {title}",
                body=f"{option.get('opening_text') or ''}\n\n{body}".strip(),
            )
            message = await send_with_server_identity(cfg, channel, view=view, wait=True)
            if message is None:
                raise RuntimeError("envio retornou vazio")
            await interaction.response.send_message("Mensagem enviada com sucesso.", ephemeral=True)
        except Exception as exc:
            log.warning("[tickets] falha ao enviar opção para canal gid=%s option=%s: %r", guild.id, option_id, exc)
            await interaction.response.send_message("Não consegui enviar no canal configurado.", ephemeral=True)

    def _kind_label(self, kind: str) -> str:
        cfg = None
        try:
            # Chamadas internas normalmente já têm config sanitizada; este fallback
            # mantém compatibilidade com chamadas antigas que só passam o kind.
            for guild in getattr(self.bot, "guilds", []) or []:
                cfg = self._get_config(int(guild.id))
                if get_ticket_option(cfg, kind):
                    break
        except Exception:
            cfg = None
        item = get_ticket_option(cfg or {}, kind)
        return (item or PUBLIC_OPTIONS.get(kind, {})).get("label") or kind

    def _kind_emoji(self, kind: str) -> str:
        cfg = None
        try:
            for guild in getattr(self.bot, "guilds", []) or []:
                cfg = self._get_config(int(guild.id))
                if get_ticket_option(cfg, kind):
                    break
        except Exception:
            cfg = None
        item = get_ticket_option(cfg or {}, kind)
        return (item or PUBLIC_OPTIONS.get(kind, {})).get("emoji") or "🎫"

    def _kind_label_from_config(self, cfg: dict[str, Any], kind: str) -> str:
        item = get_ticket_option(cfg, kind)
        return (item or PUBLIC_OPTIONS.get(kind, {})).get("label") or kind

    def _kind_emoji_from_config(self, cfg: dict[str, Any], kind: str) -> str:
        item = get_ticket_option(cfg, kind)
        return (item or PUBLIC_OPTIONS.get(kind, {})).get("emoji") or "🎫"

    def _staff_role_ids_for_kind(self, cfg: dict[str, Any], kind: str) -> list[int]:
        roles = cfg.get("roles") or {}
        ids = [int(roles.get("staff_role_id") or 0)]
        if kind == KIND_PARTNERSHIP:
            ids.append(int(roles.get("partnership_staff_role_id") or 0))
        elif kind == KIND_REPORT:
            ids.append(int(roles.get("report_staff_role_id") or 0))
        elif kind == KIND_OTHER:
            ids.append(int(roles.get("other_staff_role_id") or 0))
        result: list[int] = []
        for role_id in ids:
            if role_id and role_id not in result:
                result.append(role_id)
        return result

    async def _create_ticket_from_interaction(self, interaction: discord.Interaction, *, kind: str, payload: dict[str, str]):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, (discord.Member, discord.User)):
            await interaction.response.send_message("Não consegui identificar o servidor ou usuário.", ephemeral=True)
            return
        guild_id = int(guild.id)
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            cfg = self._get_config(guild_id)
            if not self._feature_active(cfg):
                await self._reply_interaction(interaction, "A função de tickets está desativada neste servidor.")
                return
            existing = self._find_user_open_ticket(cfg, int(interaction.user.id))
            if existing:
                await self._reply_interaction(interaction, f"Você já tem um atendimento aberto: <#{int(existing.get('channel_id'))}>.", ephemeral=True)
                return

            ticket_number = int(cfg.get("next_ticket_number") or 1)
            cfg["next_ticket_number"] = ticket_number + 1
            label = self._kind_label_from_config(cfg, kind)
            emoji = self._kind_emoji_from_config(cfg, kind)
            user_part = slugify_channel_part(getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", None) or interaction.user.id)
            kind_part = slugify_channel_part(label, fallback="ticket")
            channel_name = truncate(f"ticket-{ticket_number:04d}-{kind_part}-{user_part}", 95, suffix="").strip("-")

            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: permission_overwrite_from_scope(cfg, "everyone"),
            }
            me = getattr(guild, "me", None)
            if me is not None:
                overwrites[me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_messages=True,
                    manage_webhooks=True,
                    attach_files=True,
                    embed_links=True,
                    add_reactions=True,
                )
            if isinstance(interaction.user, discord.Member):
                overwrites[interaction.user] = permission_overwrite_from_scope(cfg, "creator")
            for role_id in self._staff_role_ids_for_kind(cfg, kind):
                role = guild.get_role(role_id)
                if role is not None:
                    overwrites[role] = permission_overwrite_from_scope(cfg, "staff")

            category = None
            category_id = int((cfg.get("channels") or {}).get("category_id") or 0)
            maybe_category = guild.get_channel(category_id) if category_id else None
            if isinstance(maybe_category, discord.CategoryChannel):
                category = maybe_category

            try:
                channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Ticket {label} aberto por {interaction.user} ({interaction.user.id})",
                )
            except discord.Forbidden:
                await self._reply_interaction(interaction, "Não tenho permissão para criar canais de ticket.", ephemeral=True)
                return
            except Exception as exc:
                log.warning("[tickets] falha ao criar canal gid=%s kind=%s: %r", guild_id, kind, exc)
                await self._reply_interaction(interaction, "Não consegui criar o canal do ticket.", ephemeral=True)
                return

            ticket = {
                "ticket_id": ticket_number,
                "channel_id": int(channel.id),
                "control_message_id": 0,
                "user_id": int(interaction.user.id),
                "kind": kind,
                "created_at": now_iso(),
                "label": label,
            }
            intro_view = self._build_ticket_intro_view(guild_id, ticket=ticket, payload=payload, opener=interaction.user)
            try:
                intro_message = await send_with_server_identity(cfg, channel, view=intro_view, wait=True)
                action_view = TicketChannelView(self, guild_id, int(channel.id))
                action_message = await send_with_server_identity(cfg, channel, view=action_view, wait=True)
                if action_message is None:
                    raise RuntimeError("não foi possível enviar ações do ticket")
                ticket["control_message_id"] = int(action_message.id)
                try:
                    self.bot.add_view(action_view, message_id=int(action_message.id))
                    self._registered_ticket_views.add((guild_id, int(channel.id), int(action_message.id)))
                except Exception:
                    pass
            except Exception as exc:
                log.warning("[tickets] falha ao postar mensagem inicial gid=%s ch=%s: %r", guild_id, channel.id, exc)

            cfg["active_tickets"].append(ticket)
            await self._save_config(guild_id, cfg)
            await self._send_ticket_log(guild_id, "created", ticket=ticket, actor=interaction.user, payload=payload)
            await self._reply_interaction(interaction, f"Ticket criado: {channel.mention}", ephemeral=True)

    def _build_ticket_intro_view(self, guild_id: int, *, ticket: dict[str, Any], payload: dict[str, str], opener: discord.abc.User) -> discord.ui.LayoutView:
        cfg = self._get_config(guild_id)
        texts = cfg.get("texts") or {}
        kind = str(ticket.get("kind") or KIND_OTHER)
        label = str(ticket.get("label") or self._kind_label_from_config(cfg, kind))
        emoji = self._kind_emoji_from_config(cfg, kind)
        lines = [
            f"# {emoji} Ticket #{int(ticket.get('ticket_id') or 0):04d}",
            f"**Categoria:** {label}",
            f"**Aberto por:** {member_display(opener)} (`{int(getattr(opener, 'id', 0) or 0)}`)",
        ]
        option = get_ticket_option(cfg, kind) or {}
        opening = str(option.get("opening_text") or "")
        if not opening:
            if kind == KIND_PARTNERSHIP:
                opening = str(texts.get("partnership_opening") or "Envie aqui as informações da parceria.")
            elif kind == KIND_REPORT:
                opening = str(texts.get("report_opening") or "A equipe irá analisar a denúncia.")
            else:
                opening = str(texts.get("other_opening") or "Explique aqui o que você precisa.")
        fields: list[discord.ui.Item] = [discord.ui.TextDisplay("\n".join(lines)), discord.ui.Separator(), discord.ui.TextDisplay(opening)]
        clean_payload = {str(k): str(v or "").strip() for k, v in (payload or {}).items() if str(v or "").strip()}
        if clean_payload:
            fields.append(discord.ui.Separator())
            for key, value in clean_payload.items():
                fields.append(discord.ui.TextDisplay(f"**{truncate(key.capitalize(), 80)}:**\n{truncate(value, 1500)}"))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*fields, accent_color=discord.Color.blurple()))
        return view

    async def _reply_interaction(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(content, ephemeral=ephemeral)
            else:
                await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception:
            pass

    async def _send_ticket_log(self, guild_id: int, event: str, *, ticket: dict[str, Any], actor: discord.abc.User | None = None, payload: dict[str, str] | None = None, file: discord.File | None = None):
        cfg = self._get_config(guild_id)
        channel_id = int((cfg.get("channels") or {}).get("logs_channel_id") or 0)
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        if event == "created":
            title = "🎫 Ticket criado"
            color = discord.Color.green()
        elif event == "closed":
            title = "🔒 Ticket fechado"
            color = discord.Color.red()
        else:
            title = "📄 Ticket"
            color = discord.Color.blurple()
        lines = [
            f"# {title}",
            f"**Ticket:** `#{int(ticket.get('ticket_id') or 0):04d}` <#{int(ticket.get('channel_id') or 0)}>",
            f"**Tipo:** {ticket.get('label') or ticket.get('kind') or 'ticket'}",
            f"**Usuário:** <@{int(ticket.get('user_id') or 0)}> (`{int(ticket.get('user_id') or 0)}`)",
        ]
        if actor is not None:
            lines.append(f"**Ação por:** {member_display(actor)} (`{int(getattr(actor, 'id', 0) or 0)}`)")
        payload = payload or {}
        payload_text = ""
        for key, value in payload.items():
            if str(value or "").strip():
                payload_text += f"\n**{truncate(key.capitalize(), 80)}:** {truncate(value, 500)}"
        view = discord.ui.LayoutView(timeout=None)
        children: list[discord.ui.Item] = [discord.ui.TextDisplay("\n".join(lines))]
        if payload_text:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(payload_text.strip())])
        view.add_item(discord.ui.Container(*children, accent_color=color))
        try:
            await send_with_server_identity(cfg, channel, view=view, file=file, wait=True)
        except Exception:
            pass

    async def _handle_transcript_button(self, interaction: discord.Interaction, channel_id: int):
        await interaction.response.defer(ephemeral=True)
        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Canal do ticket não encontrado.", ephemeral=True)
            return
        ticket = self._find_active_ticket(int(interaction.guild_id or 0), int(channel_id)) or {}
        try:
            file = await build_transcript_file(channel, ticket=ticket)
            await interaction.followup.send("Transcript gerado.", file=file, ephemeral=True)
        except Exception as exc:
            log.warning("[tickets] falha ao gerar transcript ch=%s: %r", channel_id, exc)
            await interaction.followup.send("Não consegui gerar o transcript.", ephemeral=True)

    async def _add_user_to_ticket(self, interaction: discord.Interaction, channel_id: int, user: discord.abc.User | None):
        if user is None:
            await interaction.response.send_message("Usuário inválido.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal do ticket não encontrado.", ephemeral=True)
            return
        try:
            cfg = self._get_config(int(interaction.guild_id or 0))
            await channel.set_permissions(user, overwrite=permission_overwrite_from_scope(cfg, "creator"), reason="Usuário adicionado ao ticket")
            await interaction.response.edit_message(view=SimpleNoticeView(f"{member_display(user)} foi adicionado ao ticket.", color=discord.Color.green()))
        except Exception:
            await interaction.response.send_message("Não consegui adicionar esse usuário ao ticket.", ephemeral=True)

    async def _send_close_confirmation(self, interaction: discord.Interaction, *, guild_id: int, channel_id: int, user_id: int):
        channel = self.bot.get_channel(int(channel_id)) or getattr(interaction, "channel", None)
        if not isinstance(channel, discord.TextChannel):
            await self._reply_interaction(interaction, "Canal do ticket não encontrado.", ephemeral=True)
            return
        view = CloseConfirmView(self, int(guild_id), int(channel_id), int(user_id))
        cfg = self._get_config(int(guild_id))
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        message = await send_with_server_identity(cfg, channel, view=view, wait=True)
        if message is None:
            try:
                await interaction.followup.send("Não consegui enviar a confirmação de fechamento.", ephemeral=True)
            except Exception:
                pass

    async def _close_ticket(self, interaction: discord.Interaction, channel_id: int):
        await interaction.response.defer(ephemeral=True)
        guild_id = int(interaction.guild_id or 0)
        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Canal do ticket não encontrado.", ephemeral=True)
            return
        cfg = self._get_config(guild_id)
        ticket = self._find_active_ticket(guild_id, int(channel_id)) or {"channel_id": int(channel_id), "ticket_id": 0, "user_id": int(getattr(interaction.user, "id", 0) or 0), "kind": "ticket", "label": "Ticket"}
        transcript_file = None
        if bool((cfg.get("options") or {}).get("transcript_on_close", True)):
            try:
                transcript_file = await build_transcript_file(channel, ticket=ticket)
            except Exception as exc:
                log.warning("[tickets] falha ao gerar transcript no fechamento ch=%s: %r", channel_id, exc)
        await self._send_ticket_log(guild_id, "closed", ticket=ticket, actor=interaction.user, payload={}, file=transcript_file)
        cfg["active_tickets"] = [t for t in (cfg.get("active_tickets") or []) if int(t.get("channel_id") or 0) != int(channel_id)]
        await self._save_config(guild_id, cfg)
        texts = cfg.get("texts") or {}
        try:
            await send_with_server_identity(cfg, channel, view=SimpleNoticeView(str(texts.get("close_notice") or "Este ticket será fechado em alguns segundos."), color=discord.Color.dark_gray()), wait=True)
        except Exception:
            pass
        await interaction.followup.send("Ticket fechado.", ephemeral=True)
        await asyncio.sleep(4)
        try:
            await channel.delete(reason=f"Ticket fechado por {interaction.user} ({interaction.user.id})")
        except Exception:
            pass

    @commands.command(name="ticket")
    @commands.guild_only()
    async def ticket_command(self, ctx: commands.Context):
        if not self._is_staff(getattr(ctx, "author", None), ctx.guild.id):
            await ctx.send("Só a staff pode publicar o painel de tickets.")
            return
        remaining = await self._consume_publish_cooldown(ctx.guild.id)
        if remaining > 0:
            await ctx.send(f"Espere {remaining:.0f}s para publicar o painel de tickets de novo.")
            return
        await self._delete_existing_panel(ctx.guild.id)
        try:
            message = await self._publish_panel(ctx.channel, ctx.guild.id)
        except Exception as exc:
            log.warning("[tickets] falha ao publicar painel gid=%s: %r", ctx.guild.id, exc)
            await ctx.send(f"Não consegui publicar o painel: {exc}")
            return
        confirmation = await ctx.send(view=SimpleNoticeView(f"Painel de tickets publicado: {message.jump_url}", color=discord.Color.green()))
        asyncio.create_task(self._delete_message_after(confirmation))
        asyncio.create_task(self._delete_message_after(getattr(ctx, "message", None)))

    @commands.command(name="ticketedit")
    @commands.guild_only()
    async def ticketedit_command(self, ctx: commands.Context):
        if not self._is_staff(getattr(ctx, "author", None), ctx.guild.id):
            await ctx.send("Só a staff pode abrir o editor de tickets.")
            return
        key = (int(ctx.guild.id), int(ctx.author.id))
        old = self._active_edit_messages.get(key)
        if old:
            channel_id, message_id = old
            channel = self.bot.get_channel(channel_id)
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    old_msg = await channel.fetch_message(message_id)
                    await old_msg.delete()
                except Exception:
                    pass
        view = TicketEditorView(self, ctx.guild.id, ctx.author.id)
        try:
            msg = await ctx.channel.send(view=view)
        except Exception as exc:
            await ctx.send(f"Não consegui abrir o editor de tickets: {exc}")
            return
        view.message = msg
        self._active_edit_messages[key] = (int(ctx.channel.id), int(msg.id))


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))
