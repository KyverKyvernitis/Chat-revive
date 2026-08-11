from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

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
    TICKET_KINDS,
)
from .permissions import permission_summary, reset_permissions
from .utils import (
    accent_color,
    clean_panel_image_url,
    create_custom_ticket_option,
    get_ticket_option,
    is_staff,
    iter_ticket_options,
    option_emoji_for_select,
    option_emoji_text,
    truncate,
)

if TYPE_CHECKING:
    from .cog import TicketsCog




def _safe_select_option(**kwargs: Any) -> discord.SelectOption:
    """Cria SelectOption sem deixar emoji/config quebrada derrubar a interação."""
    attempts: list[dict[str, Any]] = []
    attempts.append(dict(kwargs))
    without_default = dict(kwargs)
    without_default.pop("default", None)
    attempts.append(without_default)
    without_emoji = dict(kwargs)
    without_emoji.pop("emoji", None)
    attempts.append(without_emoji)
    without_both = dict(without_emoji)
    without_both.pop("default", None)
    attempts.append(without_both)

    last_exc: Exception | None = None
    for payload in attempts:
        try:
            return discord.SelectOption(**payload)
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc or RuntimeError("falha ao criar SelectOption")

def _option_label(kind: str, cfg: dict[str, Any] | None = None) -> str:
    item = get_ticket_option(cfg or {}, kind) if cfg else None
    return (item or PUBLIC_OPTIONS.get(kind, {})).get("label") or kind


def _option_emoji(kind: str, cfg: dict[str, Any] | None = None) -> str:
    item = get_ticket_option(cfg or {}, kind) if cfg else None
    return (item or PUBLIC_OPTIONS.get(kind, {})).get("emoji") or "🎫"


def _panel_image_component(url: object) -> discord.ui.Item | None:
    image_url = clean_panel_image_url(url)
    if not image_url:
        return None

    gallery_cls = getattr(discord.ui, "MediaGallery", None)
    item_cls = getattr(discord, "MediaGalleryItem", None) or getattr(discord.ui, "MediaGalleryItem", None)
    if gallery_cls is not None and item_cls is not None:
        attempts = (
            lambda: gallery_cls(item_cls(media=image_url)),
            lambda: gallery_cls(item_cls(url=image_url)),
            lambda: gallery_cls(items=[item_cls(media=image_url)]),
            lambda: gallery_cls(items=[item_cls(url=image_url)]),
            lambda: gallery_cls([item_cls(media=image_url)]),
            lambda: gallery_cls([item_cls(url=image_url)]),
        )
        for make_item in attempts:
            try:
                item = make_item()
                if item is not None:
                    return item
            except Exception:
                continue

    return discord.ui.TextDisplay(f"[Imagem do painel]({image_url})")


def _panel_thumbnail_component(url: object) -> discord.ui.Item | None:
    image_url = clean_panel_image_url(url)
    if not image_url:
        return None

    thumbnail_cls = getattr(discord.ui, "Thumbnail", None) or getattr(discord, "Thumbnail", None)
    if thumbnail_cls is None:
        return None
    attempts = (
        lambda: thumbnail_cls(media=image_url),
        lambda: thumbnail_cls(url=image_url),
    )
    for make_item in attempts:
        try:
            item = make_item()
            if item is not None:
                return item
        except Exception:
            continue
    return None


def _panel_header_components(title: object, description: object, side_image_url: object) -> list[discord.ui.Item]:
    title_item = discord.ui.TextDisplay(f"# {title or '🎫 Atendimento'}")
    desc_item = discord.ui.TextDisplay(str(description or "Escolha abaixo o tipo de atendimento."))
    thumbnail = _panel_thumbnail_component(side_image_url)
    section_cls = getattr(discord.ui, "Section", None)
    if thumbnail is not None and section_cls is not None:
        attempts = (
            lambda: section_cls(title_item, desc_item, accessory=thumbnail),
            lambda: section_cls(accessory=thumbnail, children=[title_item, desc_item]),
            lambda: section_cls([title_item, desc_item], accessory=thumbnail),
        )
        for make_item in attempts:
            try:
                section = make_item()
                if section is not None:
                    return [section]
            except Exception:
                continue
    children = [title_item, desc_item]
    image_url = clean_panel_image_url(side_image_url)
    if image_url:
        children.append(discord.ui.TextDisplay(f"[Imagem lateral]({image_url})"))
    return children


def _select_value(select: discord.ui.Select, interaction: discord.Interaction) -> str:
    try:
        return str((getattr(select, "values", []) or [""])[0])
    except Exception:
        pass
    try:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        return str((data.get("values") or [""])[0])
    except Exception:
        return ""


class TicketPublicPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, *, feature_enabled: bool | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        cfg = cog._get_config(self.guild_id)
        panel = cfg.get("panel") or {}
        feature_enabled = cog._feature_active(cfg) if feature_enabled is None else bool(feature_enabled)
        options: list[discord.SelectOption] = []
        for item in iter_ticket_options(cfg, include_disabled=False) if feature_enabled else []:
            option_id = str(item.get("id") or "")
            if not option_id:
                continue
            options.append(_safe_select_option(
                label=truncate(item.get("label") or option_id, 100, suffix=""),
                value=truncate(option_id, 100, suffix=""),
                description=truncate(item.get("description") or "Abrir atendimento.", 100, suffix=""),
                emoji=option_emoji_for_select(item.get("emoji"), fallback="🎫"),
            ))

        children: list[discord.ui.Item] = _panel_header_components(
            panel.get("title") or "🎫 Atendimento",
            panel.get("description") or "Escolha abaixo o tipo de atendimento.",
            panel.get("side_image_url"),
        )
        image_item = _panel_image_component(panel.get("image_url"))
        if image_item is not None:
            children.extend([discord.ui.Separator(), image_item])
        self.select: discord.ui.Select | None = None
        if options:
            select = discord.ui.Select(
                placeholder=truncate(panel.get("placeholder") or "Escolha uma opção", 100, suffix=""),
                min_values=1,
                max_values=1,
                options=options[:25],
                custom_id=f"tickets:panel:{self.guild_id}",
            )
            select.callback = self._on_select
            self.select = select
            children.extend([discord.ui.Separator(), discord.ui.ActionRow(select)])
        elif feature_enabled:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay("_Nenhuma opção está ativa no momento._")])
        else:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay("_Esta função está desativada no momento._")])

        self.add_item(discord.ui.Container(
            *children,
            accent_color=accent_color(panel.get("accent_color")),
        ))

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str(((self.select.values if self.select is not None else []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""
        await self.cog._handle_public_choice(interaction, value)


class PartnershipConfirmView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, user_id: int, option_id: str = KIND_PARTNERSHIP):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.option_id = str(option_id or KIND_PARTNERSHIP)
        cfg = cog._get_config(self.guild_id)
        option = get_ticket_option(cfg, self.option_id) or get_ticket_option(cfg, KIND_PARTNERSHIP) or {}
        confirm = discord.ui.Button(
            label="Criar ticket",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"tickets:confirm_ticket:{self.guild_id}:{self.user_id}:{self.option_id}",
        )
        confirm.callback = self._on_confirm
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {option_emoji_text(option.get('emoji'), fallback='🎫')} {option.get('label') or 'Atendimento'}"),
            discord.ui.TextDisplay(str(option.get("confirmation_text") or "Ao confirmar, criaremos um ticket privado.")),
            discord.ui.Separator(),
            discord.ui.ActionRow(confirm),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.user_id:
            await interaction.response.send_message("Só quem iniciou essa confirmação pode usar.", ephemeral=True)
            return False
        return True

    async def _on_confirm(self, interaction: discord.Interaction):
        await self.cog._create_ticket_from_interaction(interaction, kind=self.option_id, payload={})

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SimpleNoticeView("🤝 Parceria cancelada.", color=discord.Color.dark_gray()))
        self.stop()


class SimpleNoticeView(discord.ui.LayoutView):
    def __init__(self, text: str, *, color: discord.Color | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(str(text or "")), accent_color=color or discord.Color.blurple()))


class TicketChannelView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)

        close = discord.ui.Button(
            label="Fechar",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:close:{self.guild_id}:{self.channel_id}",
        )
        close.callback = self._on_close
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Ações do ticket"),
            discord.ui.TextDisplay("Use o botão abaixo para encerrar este atendimento."),
            discord.ui.Separator(),
            discord.ui.ActionRow(close),
            accent_color=discord.Color.dark_gray(),
        ))

    async def _is_ticket_staff_or_owner(self, interaction: discord.Interaction) -> bool:
        cfg = self.cog._get_config(self.guild_id)
        ticket = self.cog._find_active_ticket(self.guild_id, self.channel_id)
        uid = int(getattr(interaction.user, "id", 0) or 0)
        if ticket and uid == int(ticket.get("user_id") or 0):
            return True
        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user, cfg)

    async def _staff_only(self, interaction: discord.Interaction) -> bool:
        cfg = self.cog._get_config(self.guild_id)
        if isinstance(interaction.user, discord.Member) and is_staff(interaction.user, cfg):
            return True
        await interaction.response.send_message("Só a equipe pode usar essa ação.", ephemeral=True)
        return False

    async def _on_add_user(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        await interaction.response.send_message(
            view=AddUserView(self.cog, self.guild_id, self.channel_id, int(interaction.user.id)),
            ephemeral=True,
        )

    async def _on_transcript(self, interaction: discord.Interaction):
        if not await self._is_ticket_staff_or_owner(interaction):
            await interaction.response.send_message("Você não pode gerar transcript deste ticket.", ephemeral=True)
            return
        await self.cog._handle_transcript_button(interaction, self.channel_id)

    async def _on_close(self, interaction: discord.Interaction):
        if not await self._is_ticket_staff_or_owner(interaction):
            await interaction.response.send_message("Você não pode fechar este ticket.", ephemeral=True)
            return
        await self.cog._send_close_confirmation(
            interaction,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            user_id=int(interaction.user.id),
        )


class AddUserView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int, staff_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.staff_id = int(staff_id)
        self.select = discord.ui.UserSelect(
            placeholder="Escolha o usuário para adicionar ao ticket",
            min_values=1,
            max_values=1,
            custom_id=f"tickets:add_user_select:{self.guild_id}:{self.channel_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 👤 Adicionar usuário"),
            discord.ui.TextDisplay("Selecione quem também poderá ver e enviar mensagens neste ticket."),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu essa seleção pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        users = getattr(self.select, "values", []) or []
        user = users[0] if users else None
        await self.cog._add_user_to_ticket(interaction, self.channel_id, user)


class CloseConfirmView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, channel_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.user_id = int(user_id)
        confirm = discord.ui.Button(
            label="Confirmar fechamento",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:close_confirm:{self.guild_id}:{self.channel_id}:{self.user_id}",
        )
        cancel = discord.ui.Button(
            label="Cancelar",
            emoji="❌",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:close_cancel:{self.guild_id}:{self.channel_id}:{self.user_id}",
        )
        confirm.callback = self._on_confirm
        cancel.callback = self._on_cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🔒 Fechar ticket"),
            discord.ui.TextDisplay("Confirme para gerar logs/transcript quando configurado e apagar o canal."),
            discord.ui.Separator(),
            discord.ui.ActionRow(confirm, cancel),
            accent_color=discord.Color.red(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.user_id:
            await interaction.response.send_message("Só quem iniciou o fechamento pode confirmar.", ephemeral=True)
            return False
        return True

    async def _on_confirm(self, interaction: discord.Interaction):
        await self.cog._close_ticket(interaction, self.channel_id)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SimpleNoticeView("Fechamento cancelado.", color=discord.Color.dark_gray()))
        self.stop()


class SuggestionMessageView(discord.ui.LayoutView):
    def __init__(self, *, guild_id: int, author_id: int, title: str, body: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# ⚡ Nova sugestão"),
            discord.ui.TextDisplay(f"**Autor:** <@{int(author_id)}> (`{int(author_id)}`)"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"## {truncate(title or 'Sugestão', 180)}"),
            discord.ui.TextDisplay(truncate(body or "_sem descrição_", 1800)),
            accent_color=discord.Color.gold(),
        ))


class TicketEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: discord.Message | None = None
        cfg = cog._get_config(self.guild_id)
        panel = cfg.get("panel") or {}
        channels = cfg.get("channels") or {}
        enabled = cfg.get("enabled") or {}
        options_cfg = cfg.get("options") or {}

        panel_channel_id = int(panel.get("channel_id") or 0)
        panel_message_id = int(panel.get("message_id") or 0)
        if panel_channel_id and panel_message_id:
            panel_ref = f"publicado em <#{panel_channel_id}>"
        else:
            panel_ref = "não publicado"

        category_id = int(channels.get("category_id") or 0)
        logs_channel_id = int(channels.get("logs_channel_id") or 0)
        suggestions_channel_id = int(channels.get("suggestions_channel_id") or 0)
        tickets_ref = f"<#{category_id}>" if category_id else "canal atual / sem categoria"
        logs_ref = f"<#{logs_channel_id}>" if logs_channel_id else "não configurado"
        suggestions_ref = f"<#{suggestions_channel_id}>" if suggestions_channel_id else "não configurado"
        webhook_ref = "servidor" if bool(options_cfg.get("use_server_webhook", False)) else "bot"

        active_names = [
            f"{option_emoji_text(item.get('emoji'), fallback='🎫')} {item.get('label') or item.get('id')}"
            for item in iter_ticket_options(cfg, include_disabled=False)
        ]
        active_text = " · ".join(active_names) if active_names else "nenhuma opção ativa"

        summary = (
            "Configure o sistema de atendimento deste servidor.\n"
            "Use o menu abaixo para escolher o que deseja editar.\n\n"
            f"**Painel:** {panel_ref}\n"
            f"**Tickets:** {tickets_ref}\n"
            f"**Logs:** {logs_ref}\n"
            f"**Sugestões:** {suggestions_ref}\n"
            f"**Envio:** {webhook_ref}\n\n"
            f"**Ativos:** {active_text}"
        )

        self.edit_select = discord.ui.Select(
            placeholder="Escolha o que editar...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Painel público",
                    value="panel",
                    description="Editar título, descrição e placeholder.",
                    emoji="📝",
                ),
                discord.SelectOption(
                    label="Aparência",
                    value="appearance",
                    description="Editar cor, imagem principal e imagem lateral.",
                    emoji="🎨",
                ),
                discord.SelectOption(
                    label="Opções",
                    value="options",
                    description="Usar checkboxes para ligar/desligar opções.",
                    emoji="⚙️",
                ),
                discord.SelectOption(
                    label="Opções do painel",
                    value="panel_options",
                    description="Editar nome, emoji, descrição e fluxo de cada opção.",
                    emoji="🎛️",
                ),
                discord.SelectOption(
                    label="Canais",
                    value="channels",
                    description="Selecionar categoria, logs e sugestões.",
                    emoji="📁",
                ),
                discord.SelectOption(
                    label="Cargos",
                    value="roles",
                    description="Selecionar cargos da equipe por tipo.",
                    emoji="👥",
                ),
                discord.SelectOption(
                    label="Permissões",
                    value="permissions",
                    description="Gerenciar @everyone, staff e autor do ticket.",
                    emoji="🔐",
                ),
                discord.SelectOption(
                    label="Denúncias",
                    value="reports",
                    description="Editar até 10 tipos de denúncia.",
                    emoji="👾",
                ),
                discord.SelectOption(
                    label="Textos",
                    value="texts",
                    description="Editar textos de abertura, confirmação e fechamento.",
                    emoji="💬",
                ),
            ],
            custom_id=f"tickets:editor_select:{guild_id}:{staff_id}",
        )
        self.edit_select.callback = self._on_select

        preview_btn = discord.ui.Button(
            label="Preview",
            emoji="👁️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:edit_preview:{guild_id}:{staff_id}",
        )
        close_btn = discord.ui.Button(
            emoji="⛔",
            style=discord.ButtonStyle.danger,
            custom_id=f"tickets:edit_delete:{guild_id}:{staff_id}",
        )
        preview_btn.callback = self._on_preview
        close_btn.callback = self._on_delete

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🎫 Editor de Atendimento"),
            discord.ui.TextDisplay(summary),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.edit_select),
            discord.ui.ActionRow(preview_btn, close_btn),
            accent_color=discord.Color.gold(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu esse editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str((getattr(self.edit_select, "values", []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""

        if value == "panel":
            await self._on_panel(interaction)
            return
        if value == "appearance":
            await self._on_appearance(interaction)
            return
        if value == "options":
            await self._on_options(interaction)
            return
        if value == "panel_options":
            await self._on_panel_options(interaction)
            return
        if value == "channels":
            await self._on_channels(interaction)
            return
        if value == "roles":
            await self._on_roles(interaction)
            return
        if value == "permissions":
            await self._on_permissions(interaction)
            return
        if value == "reports":
            await self._on_reports(interaction)
            return
        if value == "texts":
            await self._on_texts(interaction)
            return
        await interaction.response.send_message("Escolha inválida ou desatualizada.", ephemeral=True)

    async def _on_panel(self, interaction: discord.Interaction):
        from .modals import PanelEditModal
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_appearance(self, interaction: discord.Interaction):
        from .modals import AppearanceEditModal
        await interaction.response.send_modal(AppearanceEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_options(self, interaction: discord.Interaction):
        from .modals import OptionsEditModal
        await interaction.response.send_modal(OptionsEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_panel_options(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=TicketPanelOptionsEditorView(self.cog, self.guild_id, self.staff_id),
            ephemeral=True,
        )

    async def _on_channels(self, interaction: discord.Interaction):
        from .modals import ChannelsEditModal
        await interaction.response.send_modal(ChannelsEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_roles(self, interaction: discord.Interaction):
        from .modals import RolesEditModal
        await interaction.response.send_modal(RolesEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_permissions(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=TicketPermissionsEditorView(self.cog, self.guild_id, self.staff_id),
            ephemeral=True,
        )

    async def _on_reports(self, interaction: discord.Interaction):
        from .modals import ReportTypesEditModal
        await interaction.response.send_modal(ReportTypesEditModal(self.cog, self.guild_id, self.staff_id))

    async def _on_texts(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=TicketTextsEditorView(self.cog, self.guild_id, self.staff_id),
            ephemeral=True,
        )

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=TicketPublicPanelView(self.cog, self.guild_id), ephemeral=True)

    async def _on_delete(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.delete()
            else:
                await interaction.message.delete()
        except Exception:
            pass
        self.stop()



def _flow_label(flow: object) -> str:
    labels = {
        FLOW_CONFIRM_TICKET: "Confirmar e criar ticket",
        FLOW_MODAL_TICKET: "Modal e criar ticket",
        FLOW_MODAL_CHANNEL: "Modal e enviar para canal",
        FLOW_DIRECT_TICKET: "Criar ticket direto",
    }
    return labels.get(str(flow or ""), "Modal e criar ticket")


class TicketPanelOptionsEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, selected_option_id: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(self.guild_id)
        items = iter_ticket_options(cfg, include_disabled=True)[:25]
        valid_ids = [str(item.get("id") or "") for item in items if str(item.get("id") or "")]
        selected = str(selected_option_id or "")
        if selected not in valid_ids:
            selected = valid_ids[0] if valid_ids else ""
        self.selected_option_id = selected

        options: list[discord.SelectOption] = []
        selected_item: dict[str, Any] | None = None
        for item in items:
            option_id = str(item.get("id") or "")
            if not option_id:
                continue
            if option_id == selected:
                selected_item = item
            status = "ativa" if bool(item.get("enabled", True)) else "desativada"
            flow_label = _flow_label(item.get("flow") or FLOW_MODAL_TICKET)
            kwargs = dict(
                label=truncate(item.get("label") or option_id, 100, suffix=""),
                value=truncate(option_id, 100, suffix=""),
                description=truncate(f"{status} · {flow_label}", 100, suffix=""),
                emoji=option_emoji_for_select(item.get("emoji"), fallback="🎫"),
            )
            kwargs["default"] = option_id == selected
            options.append(_safe_select_option(**kwargs))

        if not options:
            options.append(discord.SelectOption(label="Nenhuma opção", value="__none__", description="Use ➕ para criar uma opção.", emoji="⚠️"))

        self.select = discord.ui.Select(
            placeholder="Escolha uma opção do painel...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"tickets:panel_options_select:{self.guild_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select

        edit_btn = discord.ui.Button(
            emoji="✏️",
            label="Editar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:panel_option_edit:{self.guild_id}:{self.staff_id}",
            disabled=not bool(selected),
        )
        add_btn = discord.ui.Button(
            emoji="➕",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:panel_option_add:{self.guild_id}:{self.staff_id}",
        )
        delete_btn = discord.ui.Button(
            emoji="🗑️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tickets:panel_option_delete:{self.guild_id}:{self.staff_id}",
            disabled=not bool(selected),
        )
        edit_btn.callback = self._on_edit
        add_btn.callback = self._on_add
        delete_btn.callback = self._on_delete_option

        if selected_item:
            enabled = "ativa" if bool(selected_item.get("enabled", True)) else "desativada"
            selected_summary = (
                f"**Selecionada:** {option_emoji_text(selected_item.get('emoji'), fallback='🎫')} {selected_item.get('label') or selected}\n"
                f"**Fluxo:** {_flow_label(selected_item.get('flow') or FLOW_MODAL_TICKET)}\n"
                f"**Status:** {enabled}\n"
                f"**Descrição:** {truncate(selected_item.get('description') or 'Abrir atendimento.', 220)}"
            )
        else:
            selected_summary = "Nenhuma opção criada ainda. Use ➕ para adicionar uma opção personalizada."

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🎛️ Opções do painel"),
            discord.ui.TextDisplay(
                "Selecione uma opção para visualizar. Use **✏️ Editar** para alterar, **➕** para criar e **🗑️** para remover/desativar.\n\n"
                f"{selected_summary}"
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            discord.ui.ActionRow(edit_btn, add_btn, delete_btn),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        option_id = _select_value(self.select, interaction)
        if not option_id or option_id == "__none__":
            await interaction.response.send_message("Nenhuma opção selecionada.", ephemeral=True)
            return
        await interaction.response.edit_message(
            view=TicketPanelOptionsEditorView(self.cog, self.guild_id, self.staff_id, option_id)
        )

    async def _on_edit(self, interaction: discord.Interaction):
        option_id = self.selected_option_id
        if not option_id:
            await interaction.response.send_message("Selecione uma opção primeiro.", ephemeral=True)
            return
        from .modals import TicketOptionEditModal
        await interaction.response.send_modal(TicketOptionEditModal(self.cog, self.guild_id, self.staff_id, option_id))

    async def _on_add(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        created = create_custom_ticket_option(cfg)
        if not created:
            await interaction.response.send_message("Limite de opções atingido; nenhuma opção nova foi criada.", ephemeral=True)
            return
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._refresh_editor_message(self.guild_id, self.staff_id)
        panel = cfg.get("panel") or {}
        if int(panel.get("channel_id") or 0) and int(panel.get("message_id") or 0):
            try:
                await self.cog._refresh_public_panel(self.guild_id)
            except Exception:
                pass
        await interaction.response.edit_message(
            view=TicketPanelOptionsEditorView(self.cog, self.guild_id, self.staff_id, str(created.get("id") or ""))
        )

    async def _on_delete_option(self, interaction: discord.Interaction):
        option_id = self.selected_option_id
        if not option_id:
            await interaction.response.send_message("Selecione uma opção primeiro.", ephemeral=True)
            return
        cfg = self.cog._get_config(self.guild_id)
        item = get_ticket_option(cfg, option_id) or {}
        label = f"{option_emoji_text(item.get('emoji'), fallback='🎫')} {item.get('label') or option_id}"
        await interaction.response.edit_message(
            view=TicketOptionDeleteConfirmView(self.cog, self.guild_id, self.staff_id, option_id, label)
        )


class TicketOptionDeleteConfirmView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, option_id: str, label: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.option_id = str(option_id)
        self.label = str(label or option_id)
        confirm_btn = discord.ui.Button(emoji="🗑️", label="Confirmar", style=discord.ButtonStyle.secondary, custom_id=f"tickets:option_delete_confirm:{self.guild_id}:{self.staff_id}")
        back_btn = discord.ui.Button(emoji="↩️", label="Voltar", style=discord.ButtonStyle.secondary, custom_id=f"tickets:option_delete_back:{self.guild_id}:{self.staff_id}")
        confirm_btn.callback = self._on_confirm
        back_btn.callback = self._on_back
        cfg = cog._get_config(self.guild_id)
        item = get_ticket_option(cfg, self.option_id) or {}
        action = "desativar" if bool(item.get("builtin", False)) else "remover"
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🗑️ Remover opção"),
            discord.ui.TextDisplay(f"Deseja {action} **{self.label}**?"),
            discord.ui.Separator(),
            discord.ui.ActionRow(confirm_btn, back_btn),
            accent_color=discord.Color.dark_gray(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=TicketPanelOptionsEditorView(self.cog, self.guild_id, self.staff_id, self.option_id)
        )

    async def _on_confirm(self, interaction: discord.Interaction):
        cfg = self.cog._get_config(self.guild_id)
        items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
        item = items.get(self.option_id) if isinstance(items, dict) else None
        if not isinstance(item, dict):
            await interaction.response.send_message("Opção não encontrada.", ephemeral=True)
            return
        active_count = sum(1 for opt in items.values() if isinstance(opt, dict) and bool(opt.get("enabled", True)))
        if active_count <= 1 and bool(item.get("enabled", True)):
            await interaction.response.send_message("Não é possível deixar o painel sem nenhuma opção ativa.", ephemeral=True)
            return
        if bool(item.get("builtin", False)):
            item["enabled"] = False
            result = "Opção nativa desativada."
        else:
            items.pop(self.option_id, None)
            result = "Opção personalizada removida."
        cfg["enabled"] = {kind: bool((items.get(kind) or {}).get("enabled", False)) for kind in TICKET_KINDS}
        await self.cog._save_config(self.guild_id, cfg)
        await self.cog._refresh_editor_message(self.guild_id, self.staff_id)
        panel = cfg.get("panel") or {}
        if int(panel.get("channel_id") or 0) and int(panel.get("message_id") or 0):
            try:
                await self.cog._refresh_public_panel(self.guild_id)
            except Exception:
                pass
        await interaction.response.edit_message(
            view=TicketPanelOptionsEditorView(self.cog, self.guild_id, self.staff_id)
        )
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass


class TicketTextsEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(self.guild_id)
        options = []
        for item in iter_ticket_options(cfg, include_disabled=True)[:24]:
            option_id = str(item.get("id") or "")
            if not option_id:
                continue
            options.append(_safe_select_option(
                label=truncate(item.get("label") or option_id, 100, suffix=""),
                value=truncate(option_id, 100, suffix=""),
                description="Editar textos dessa opção.",
                emoji=option_emoji_for_select(item.get("emoji"), fallback="🎫"),
            ))
        options.append(discord.SelectOption(
            label="Fechamento do ticket",
            value="__close_notice__",
            description="Texto enviado antes de apagar o canal.",
            emoji="🔒",
        ))
        self.select = discord.ui.Select(
            placeholder="Escolha qual texto deseja editar...",
            min_values=1,
            max_values=1,
            options=options[:25],
            custom_id=f"tickets:texts_select:{self.guild_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 💬 Editor de textos"),
            discord.ui.TextDisplay("Escolha uma opção. Cada texto abre em um modal separado para caber bem no celular."),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = _select_value(self.select, interaction)
        if value == "__close_notice__":
            from .modals import SingleTicketTextModal
            await interaction.response.send_modal(SingleTicketTextModal(self.cog, self.guild_id, self.staff_id, "__global__", "close_notice"))
            return
        if not value:
            await interaction.response.send_message("Escolha inválida.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=TicketOptionTextsEditorView(self.cog, self.guild_id, self.staff_id, value),
            ephemeral=True,
        )


class TicketOptionTextsEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int, option_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.option_id = str(option_id)
        cfg = cog._get_config(self.guild_id)
        item = get_ticket_option(cfg, self.option_id) or {}
        flow = str(item.get("flow") or FLOW_MODAL_TICKET)
        options: list[discord.SelectOption] = []
        if flow == FLOW_CONFIRM_TICKET:
            options.append(discord.SelectOption(label="Confirmação", value="confirmation_text", description="Texto antes de criar o ticket.", emoji="✅"))
        if flow in {FLOW_CONFIRM_TICKET, FLOW_MODAL_TICKET, FLOW_DIRECT_TICKET}:
            options.append(discord.SelectOption(label="Abertura do ticket", value="opening_text", description="Texto enviado dentro do canal criado.", emoji="🎫"))
        if flow in {FLOW_MODAL_TICKET, FLOW_MODAL_CHANNEL}:
            options.extend([
                discord.SelectOption(label="Título do modal", value="modal_title", description="Título da janela aberta ao usuário.", emoji="📝"),
                discord.SelectOption(label="Aviso do modal", value="modal_notice", description="Texto explicativo no modal, se suportado.", emoji="⚠️"),
                discord.SelectOption(label="Campo assunto/título", value="subject_label", description="Nome do primeiro campo do modal.", emoji="✏️"),
                discord.SelectOption(label="Campo descrição", value="body_label", description="Nome do campo maior do modal.", emoji="💬"),
            ])
        if flow == FLOW_MODAL_CHANNEL:
            options.append(discord.SelectOption(label="Texto da postagem", value="opening_text", description="Texto/base mostrado no canal destino.", emoji="📨"))
        if not options:
            options.append(discord.SelectOption(label="Abertura", value="opening_text", description="Texto principal.", emoji="🎫"))
        # Remove valores duplicados preservando ordem.
        deduped: list[discord.SelectOption] = []
        seen: set[str] = set()
        for opt in options:
            if opt.value in seen:
                continue
            seen.add(opt.value)
            deduped.append(opt)
        self.select = discord.ui.Select(
            placeholder="Escolha o texto dessa opção...",
            min_values=1,
            max_values=1,
            options=deduped[:25],
            custom_id=f"tickets:option_texts:{self.guild_id}:{self.staff_id}:{self.option_id}",
        )
        self.select.callback = self._on_select
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# 💬 {option_emoji_text(item.get('emoji'), fallback='🎫')} {item.get('label') or self.option_id}"),
            discord.ui.TextDisplay("Escolha exatamente qual texto deseja editar."),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        text_key = _select_value(self.select, interaction)
        if not text_key:
            await interaction.response.send_message("Escolha inválida.", ephemeral=True)
            return
        from .modals import SingleTicketTextModal
        await interaction.response.send_modal(SingleTicketTextModal(self.cog, self.guild_id, self.staff_id, self.option_id, text_key))

class TicketPermissionsEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TicketsCog", guild_id: int, staff_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        cfg = cog._get_config(self.guild_id)

        self.select = discord.ui.Select(
            placeholder="Escolha o grupo de permissões...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="@everyone",
                    value="everyone",
                    description="Permissões padrão de todos no canal do ticket.",
                    emoji="🌐",
                ),
                discord.SelectOption(
                    label="Cargos staff",
                    value="staff",
                    description="Permissões dos cargos configurados como equipe.",
                    emoji="👥",
                ),
                discord.SelectOption(
                    label="Autor do ticket",
                    value="creator",
                    description="Permissões do membro que abriu o ticket.",
                    emoji="👤",
                ),
                discord.SelectOption(
                    label="Restaurar padrão seguro",
                    value="reset",
                    description="Voltar @everyone privado e autor/staff liberados.",
                    emoji="♻️",
                ),
            ],
            custom_id=f"tickets:permissions_select:{self.guild_id}:{self.staff_id}",
        )
        self.select.callback = self._on_select

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🔐 Permissões dos Tickets"),
            discord.ui.TextDisplay(
                "Configure quem pode ver, falar e gerenciar os canais criados.\n\n"
                f"**Atual:**\n{permission_summary(cfg)}"
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.select),
            accent_color=discord.Color.blurple(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            await interaction.response.send_message("Só quem abriu o editor pode usar.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        value = ""
        try:
            value = str((getattr(self.select, "values", []) or [""])[0])
        except Exception:
            value = ""
        if not value:
            try:
                data = interaction.data if isinstance(interaction.data, dict) else {}
                value = str((data.get("values") or [""])[0])
            except Exception:
                value = ""

        if value == "reset":
            cfg = self.cog._get_config(self.guild_id)
            reset_permissions(cfg)
            await self.cog._save_config(self.guild_id, cfg)
            await self.cog._after_editor_modal_save(interaction, self.guild_id, self.staff_id, "Permissões restauradas para o padrão seguro.")
            return

        if value in {"everyone", "staff", "creator"}:
            from .modals import PermissionsEditModal
            await interaction.response.send_modal(PermissionsEditModal(self.cog, self.guild_id, self.staff_id, value))
            return

        await interaction.response.send_message("Escolha inválida.", ephemeral=True)
