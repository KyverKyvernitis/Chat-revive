"""Views da cog de formulários."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from .constants import (
    BUTTON_LABEL_MAX,
    CID_CUST_APPROVAL_EDIT_BTN,
    CID_CUST_COLORS_BTN,
    CID_CUST_DELETE_BTN,
    CID_CUST_MODAL_BTN,
    CID_CUST_OPTIONS_BTN,
    CID_CUST_PANEL_BTN,
    CID_CUST_RESPONSE_BTN,
    CID_FIELDS_ADD_BTN,
    CID_FIELDS_EDIT_BTN,
    CID_FIELDS_MOVE_DOWN_BTN,
    CID_FIELDS_MOVE_UP_BTN,
    CID_FIELDS_REMOVE_BTN,
    CID_FIELDS_REMOVE_CANCEL_BTN,
    CID_FIELDS_REMOVE_CONFIRM_BTN,
    CID_FIELDS_SELECT,
    CID_REVIEW_APPROVE_PREFIX,
    CID_REVIEW_REJECT_PREFIX,
    CID_SETUP_CONFIRM_BTN,
    CID_SETUP_FORM_SELECT,
    CID_SETUP_RESP_SELECT,
    CID_SUBMIT_PREFIX,
    CUSTOMIZATION_VIEW_TIMEOUT,
    DEFAULT_APPROVAL,
    DEFAULT_PANEL,
    DEFAULT_RESPONSE,
    MODAL_FIELD_LIMIT,
    SETUP_VIEW_TIMEOUT,
)
from .fields import get_field_value, normalize_form_fields

if TYPE_CHECKING:
    from .cog import FormsCog


def _truncate(text, limit: int) -> str:
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


def _clean_emoji(text: str | None) -> str | None:
    text = str(text or "").strip()
    return text or None


def _media_url(text: str | None) -> str:
    text = str(text or "").strip()
    if text.startswith(("https://", "http://", "attachment://")):
        return text
    return ""


def _style_from_name(name: str | None, default: discord.ButtonStyle = discord.ButtonStyle.primary) -> discord.ButtonStyle:
    name = str(name or "").strip().lower()
    mapping = {
        "primary": discord.ButtonStyle.primary,
        "blurple": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "gray": discord.ButtonStyle.secondary,
        "grey": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "green": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "red": discord.ButtonStyle.danger,
    }
    return mapping.get(name, default)


def _format_field_block(label: str, value: str) -> str:
    label = str(label or "Campo").strip() or "Campo"
    value = str(value or "—").strip() or "—"
    return f"**{label}**\n{value}"


def _format_config_field_lines(fields: list[dict]) -> str:
    normalized = normalize_form_fields(fields)
    if not normalized:
        return "_nenhum campo configurado_"

    lines: list[str] = []
    for idx, field in enumerate(normalized, start=1):
        label = _truncate(field.get("label") or f"Campo {idx}", 70)
        required = "obrigatório" if field.get("required", True) else "opcional"
        size = "longa" if field.get("long", False) else "curta"
        lines.append(f"`{idx}.` **{label}** — {required} • {size}")
    return "\n".join(lines)


def _style_label(name: str | None) -> str:
    name = str(name or "").strip().lower()
    mapping = {
        "primary": "🟦 Azul/Roxo",
        "blurple": "🟦 Azul/Roxo",
        "secondary": "⬛ Cinza",
        "gray": "⬛ Cinza",
        "grey": "⬛ Cinza",
        "success": "🟩 Verde",
        "green": "🟩 Verde",
        "danger": "🟥 Vermelho",
        "red": "🟥 Vermelho",
    }
    return mapping.get(name, "🟦 Azul/Roxo")

_ACCENT_COLOR_PRESET_LABELS = {
    "#5865F2": "🟦 Azul/Roxo",
    "#747F8D": "⬛ Cinza",
    "#57F287": "🟩 Verde",
    "#ED4245": "🟥 Vermelho",
    "#FEE75C": "🟨 Amarelo",
    "#EB459E": "🩷 Rosa",
    "#9B59B6": "🟪 Roxo",
}


def _normalize_accent_hex(value: str | int | None, default: str = "#5865F2") -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = str(default or "#5865F2").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    elif raw.lower().startswith("0x"):
        raw = raw[2:]
    if len(raw) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in raw):
        return f"#{raw.upper()}"
    return _normalize_accent_hex(default, "#5865F2") if raw != str(default or "").strip() else "#5865F2"


def _accent_color_from_config(value: str | int | None, default: str = "#5865F2") -> discord.Color:
    hex_value = _normalize_accent_hex(value, default)
    return discord.Color(int(hex_value[1:], 16))


def _accent_color_label(value: str | int | None, default: str = "#5865F2") -> str:
    hex_value = _normalize_accent_hex(value, default)
    preset = _ACCENT_COLOR_PRESET_LABELS.get(hex_value)
    return f"{preset} `{hex_value}`" if preset else f"🎨 Personalizada `{hex_value}`"


class FormView(discord.ui.LayoutView):
    """View persistente do form público."""

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        feature_enabled = cog._feature_active(cfg)
        title = str(panel.get("title") or DEFAULT_PANEL["title"])
        description = str(panel.get("description") or DEFAULT_PANEL["description"])
        button_label = _truncate(panel.get("button_label") or DEFAULT_PANEL["button_label"], BUTTON_LABEL_MAX)
        button_emoji = _clean_emoji(panel.get("button_emoji") or DEFAULT_PANEL.get("button_emoji"))
        media_url = _media_url(panel.get("media_url"))

        button = discord.ui.Button(
            label=button_label,
            emoji=button_emoji,
            style=_style_from_name(panel.get("button_style"), discord.ButtonStyle.primary),
            custom_id=f"{CID_SUBMIT_PREFIX}:{self.guild_id}",
            disabled=not feature_enabled,
        )
        button.callback = self._on_submit_click

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"# {title}"),
            discord.ui.TextDisplay(description),
        ]
        if not feature_enabled:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay("_Esta função está desativada no momento._")])
        if media_url:
            children.extend([
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media_url)
                ),
            ])
        children.extend([discord.ui.Separator(), discord.ui.ActionRow(button)])

        self.add_item(discord.ui.Container(
            *children,
            accent_color=_accent_color_from_config(panel.get("accent_color"), DEFAULT_PANEL.get("accent_color")),
        ))

    async def _on_submit_click(self, interaction: discord.Interaction):
        await self.cog._handle_submit_click(interaction, self.guild_id)


class ResponseReviewView(discord.ui.LayoutView):
    """Mensagem de submissão enviada ao canal de respostas."""

    def __init__(
        self,
        cog: "FormsCog",
        guild_id: int,
        applicant_id: int,
        field_values: dict[str, str],
        *,
        status: str = "",
        reviewer_mention: str = "",
        force_buttons: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.applicant_id = int(applicant_id)
        self.field_values = dict(field_values or {})
        self.status = str(status or "")
        self.reviewer_mention = str(reviewer_mention or "")
        self.force_buttons = bool(force_buttons)
        self._build()

    def _build(self):
        cfg = self.cog._get_config(self.guild_id)
        response = cfg.get("response") or {}
        approval = cfg.get("approval") or {}
        fields = normalize_form_fields(cfg.get("modal") or {})

        title = str(response.get("title") or DEFAULT_RESPONSE["title"])
        intro = str(response.get("intro") or "").strip()
        footer_tpl = str(response.get("footer") or DEFAULT_RESPONSE["footer"])
        media_url = _media_url(response.get("media_url"))

        ctx = self.cog._build_template_ctx(self.guild_id, self.applicant_id, self.field_values)
        footer = self.cog._safe_format(footer_tpl, ctx)

        children: list[discord.ui.Item] = [discord.ui.TextDisplay(f"# {title}")]
        if intro:
            children.append(discord.ui.TextDisplay(self.cog._safe_format(intro, ctx)))

        children.append(discord.ui.Separator())
        visible_count = 0
        for index, field in enumerate(fields):
            if not field.get("enabled", True) or not field.get("show_in_response", True):
                continue
            value = get_field_value(self.field_values, field, index)
            children.append(discord.ui.TextDisplay(_format_field_block(field.get("response_label") or field.get("label") or f"Campo {index + 1}", value)))
            visible_count += 1
        if not visible_count:
            children.append(discord.ui.TextDisplay("_Nenhum campo está marcado para aparecer na resposta da staff._"))

        if media_url:
            children.extend([
                discord.ui.Separator(),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media_url)
                ),
            ])
        if footer:
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(footer)])

        if self.status:
            label = "Aprovado" if self.status == "approved" else "Rejeitado"
            emoji = "✅" if self.status == "approved" else "❌"
            by = f" por {self.reviewer_mention}" if self.reviewer_mention else ""
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(f"## {emoji} {label}{by}")])
        elif bool(approval.get("enabled", False)) or self.force_buttons:
            approve_label = _truncate(approval.get("approve_label") or DEFAULT_APPROVAL["approve_label"], BUTTON_LABEL_MAX)
            reject_label = _truncate(approval.get("reject_label") or DEFAULT_APPROVAL["reject_label"], BUTTON_LABEL_MAX)
            approve_btn = discord.ui.Button(
                label=approve_label,
                emoji=_clean_emoji(approval.get("approve_emoji") or DEFAULT_APPROVAL["approve_emoji"]),
                style=_style_from_name(approval.get("approve_style"), discord.ButtonStyle.success),
                custom_id=f"{CID_REVIEW_APPROVE_PREFIX}:{self.guild_id}:{self.applicant_id}",
            )
            reject_btn = discord.ui.Button(
                label=reject_label,
                emoji=_clean_emoji(approval.get("reject_emoji") or DEFAULT_APPROVAL["reject_emoji"]),
                style=_style_from_name(approval.get("reject_style"), discord.ButtonStyle.danger),
                custom_id=f"{CID_REVIEW_REJECT_PREFIX}:{self.guild_id}:{self.applicant_id}",
            )
            approve_btn.callback = self._on_approve
            reject_btn.callback = self._on_reject
            children.extend([discord.ui.Separator(), discord.ui.ActionRow(approve_btn, reject_btn)])

        if self.status == "approved":
            accent_color = discord.Color.green()
        elif self.status == "rejected":
            accent_color = discord.Color.red()
        else:
            accent_color = _accent_color_from_config(response.get("accent_color"), DEFAULT_RESPONSE.get("accent_color"))

        self.add_item(discord.ui.Container(
            *children,
            accent_color=accent_color,
        ))

    async def _on_approve(self, interaction: discord.Interaction):
        await self.cog._handle_review_action(interaction, self, approved=True)

    async def _on_reject(self, interaction: discord.Interaction):
        await self.cog._handle_review_action(interaction, self, approved=False)


class FieldManagerView(discord.ui.LayoutView):
    """Painel ephemeral para adicionar, editar, remover e mover campos."""

    def __init__(
        self,
        cog: "FormsCog",
        *,
        guild_id: int,
        staff_id: int,
        selected_index: int = 0,
        confirm_remove: bool = False,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.selected_index = int(selected_index or 0)
        self.confirm_remove = bool(confirm_remove)
        self._build()

    def _build(self):
        cfg = self.cog._get_config(self.guild_id)
        modal = cfg.get("modal") or {}
        fields = normalize_form_fields(modal)
        if not fields:
            self.selected_index = 0
        else:
            self.selected_index = max(0, min(self.selected_index, len(fields) - 1))

        lines = [
            f"**Título do modal:** {modal.get('title') or 'Nova verificação'}",
            f"**Campos:** {len(fields)}/{MODAL_FIELD_LIMIT}",
            "",
        ]
        for idx, field in enumerate(fields, start=1):
            required = "obrigatório" if field.get("required", True) else "opcional"
            long = " • longo" if field.get("long", False) else ""
            hidden = " • oculto na resposta" if not field.get("show_in_response", True) else ""
            marker = "➡️ " if idx - 1 == self.selected_index else ""
            lines.append(f"{marker}`{idx}.` **{field.get('label') or f'Campo {idx}'}** — {required}{long}{hidden}")

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("# 📋 Campos do formulário"),
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
        ]

        if fields:
            options = []
            for idx, field in enumerate(fields):
                required = "obrigatório" if field.get("required", True) else "opcional"
                desc = required
                if field.get("long", False):
                    desc += " • longo"
                if not field.get("show_in_response", True):
                    desc += " • oculto"
                options.append(discord.SelectOption(
                    label=_truncate(f"{idx + 1}. {field.get('label') or f'Campo {idx + 1}'}", 100),
                    description=_truncate(desc, 100),
                    value=str(idx),
                    default=(idx == self.selected_index),
                ))
            select = discord.ui.Select(
                placeholder="Escolha qual campo editar/mover/remover",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=CID_FIELDS_SELECT,
            )
            select.callback = self._on_select
            self.field_select = select
            children.append(discord.ui.ActionRow(select))

        add_btn = discord.ui.Button(
            label="Adicionar campo",
            emoji="➕",
            style=discord.ButtonStyle.success,
            custom_id=CID_FIELDS_ADD_BTN,
            disabled=len(fields) >= MODAL_FIELD_LIMIT,
        )
        edit_btn = discord.ui.Button(
            label="Editar campo",
            emoji="✏️",
            style=discord.ButtonStyle.primary,
            custom_id=CID_FIELDS_EDIT_BTN,
            disabled=not bool(fields),
        )
        remove_btn = discord.ui.Button(
            label="Remover campo",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=CID_FIELDS_REMOVE_BTN,
            disabled=len(fields) <= 1,
        )
        up_btn = discord.ui.Button(
            label="Subir",
            emoji="⬆️",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_FIELDS_MOVE_UP_BTN,
            disabled=self.selected_index <= 0,
        )
        down_btn = discord.ui.Button(
            label="Descer",
            emoji="⬇️",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_FIELDS_MOVE_DOWN_BTN,
            disabled=self.selected_index >= len(fields) - 1,
        )
        add_btn.callback = self._on_add
        edit_btn.callback = self._on_edit
        remove_btn.callback = self._on_remove
        up_btn.callback = self._on_move_up
        down_btn.callback = self._on_move_down

        children.append(discord.ui.ActionRow(add_btn, edit_btn, remove_btn))
        children.append(discord.ui.ActionRow(up_btn, down_btn))

        if self.confirm_remove and fields:
            confirm_btn = discord.ui.Button(
                label="Confirmar remoção",
                emoji="⚠️",
                style=discord.ButtonStyle.danger,
                custom_id=CID_FIELDS_REMOVE_CONFIRM_BTN,
            )
            cancel_btn = discord.ui.Button(
                label="Cancelar",
                emoji="↩️",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_FIELDS_REMOVE_CANCEL_BTN,
            )
            confirm_btn.callback = self._on_confirm_remove
            cancel_btn.callback = self._on_cancel_remove
            field = fields[self.selected_index]
            children.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"⚠️ Confirmar remoção de **{field.get('label') or 'este campo'}**?"),
                discord.ui.ActionRow(confirm_btn, cancel_btn),
            ])

        self.add_item(discord.ui.Container(*children, accent_color=discord.Color.blurple()))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message("Só quem abriu esse painel pode usar.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        select = getattr(self, "field_select", None)
        try:
            value = int(select.values[0]) if select is not None and select.values else 0
        except (TypeError, ValueError):
            value = 0
        await interaction.response.edit_message(view=FieldManagerView(
            self.cog,
            guild_id=self.guild_id,
            staff_id=self.staff_id,
            selected_index=value,
        ))

    async def _on_add(self, interaction: discord.Interaction):
        from .modals import FieldEditModal
        try:
            await interaction.response.send_modal(FieldEditModal(self.cog, self.guild_id, field_index=None, staff_id=self.staff_id))
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def _on_edit(self, interaction: discord.Interaction):
        from .modals import FieldEditModal
        try:
            await interaction.response.send_modal(FieldEditModal(self.cog, self.guild_id, field_index=self.selected_index, staff_id=self.staff_id))
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def _on_remove(self, interaction: discord.Interaction):
        fields = self.cog._get_form_fields(self.guild_id)
        if len(fields) <= 1:
            await interaction.response.send_message("Não dá para remover todos os campos. Mantenha pelo menos 1.", ephemeral=True)
            return
        await interaction.response.edit_message(view=FieldManagerView(
            self.cog,
            guild_id=self.guild_id,
            staff_id=self.staff_id,
            selected_index=self.selected_index,
            confirm_remove=True,
        ))

    async def _on_confirm_remove(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        new_index = await self.cog._remove_form_field(
            interaction,
            index=self.selected_index,
            staff_id=self.staff_id,
        )
        try:
            await interaction.edit_original_response(view=FieldManagerView(
                self.cog,
                guild_id=self.guild_id,
                staff_id=self.staff_id,
                selected_index=new_index,
            ))
        except discord.HTTPException:
            pass

    async def _on_cancel_remove(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=FieldManagerView(
            self.cog,
            guild_id=self.guild_id,
            staff_id=self.staff_id,
            selected_index=self.selected_index,
        ))

    async def _on_move_up(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        new_index = await self.cog._move_form_field(
            interaction,
            index=self.selected_index,
            direction=-1,
            staff_id=self.staff_id,
        )
        try:
            await interaction.edit_original_response(view=FieldManagerView(
                self.cog,
                guild_id=self.guild_id,
                staff_id=self.staff_id,
                selected_index=new_index,
            ))
        except discord.HTTPException:
            pass

    async def _on_move_down(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        new_index = await self.cog._move_form_field(
            interaction,
            index=self.selected_index,
            direction=1,
            staff_id=self.staff_id,
        )
        try:
            await interaction.edit_original_response(view=FieldManagerView(
                self.cog,
                guild_id=self.guild_id,
                staff_id=self.staff_id,
                selected_index=new_index,
            ))
        except discord.HTTPException:
            pass


class CustomizationPanelView(discord.ui.LayoutView):
    """Painel `c` compacto."""

    def __init__(self, cog: "FormsCog", *, guild_id: int, staff_id: int):
        super().__init__(timeout=float(CUSTOMIZATION_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None
        self._build()

    def _build(self):
        cfg = self.cog._get_config(self.guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)
        approval = cfg.get("approval") or {}
        fields = normalize_form_fields(cfg.get("modal") or {})
        approval_enabled = bool(approval.get("enabled", False))
        approval_state = "ativada" if approval_enabled else "desativada"
        fields_text = _format_config_field_lines(fields)

        panel_btn = discord.ui.Button(label="Editar painel", emoji="📝", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_PANEL_BTN)
        modal_btn = discord.ui.Button(label="Editar campos", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_MODAL_BTN)
        response_btn = discord.ui.Button(label="Editar resposta", emoji="📨", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_RESPONSE_BTN)
        options_btn = discord.ui.Button(label="Editar opções", emoji="☑️", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_OPTIONS_BTN)
        colors_btn = discord.ui.Button(label="Cores", emoji="🎨", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_COLORS_BTN)
        approval_texts_btn = discord.ui.Button(label="Textos da aprovação", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_APPROVAL_EDIT_BTN)
        delete_btn = discord.ui.Button(emoji="⛔", style=discord.ButtonStyle.secondary, custom_id=CID_CUST_DELETE_BTN)
        panel_btn.callback = self._on_panel
        modal_btn.callback = self._on_modal
        response_btn.callback = self._on_response
        options_btn.callback = self._on_options
        colors_btn.callback = self._on_colors
        approval_texts_btn.callback = self._on_approval_edit
        delete_btn.callback = self._on_delete

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# ⚙️ Formulário"),
            discord.ui.TextDisplay(
                f"**Canal do formulário:** {f'<#{form_ch_id}>' if form_ch_id else '_não configurado_'}\n"
                f"**Canal das respostas:** {f'<#{resp_ch_id}>' if resp_ch_id else '_não configurado_'}\n"
                f"**Aprovação:** {approval_state}\n\n"
                f"**Campos:** {len(fields)}/{MODAL_FIELD_LIMIT}\n"
                f"{fields_text}"
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(panel_btn, modal_btn, response_btn),
            discord.ui.ActionRow(options_btn, colors_btn, approval_texts_btn, delete_btn),
            accent_color=discord.Color.gold(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message("Só quem abriu esse painel pode usar.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self):
        return

    async def _on_panel(self, interaction: discord.Interaction):
        from .modals import PanelEditModal
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild_id))

    async def _on_modal(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=FieldManagerView(
                self.cog,
                guild_id=self.guild_id,
                staff_id=int(getattr(interaction.user, "id", 0) or self.staff_id),
            ),
            ephemeral=True,
        )

    async def _on_response(self, interaction: discord.Interaction):
        from .modals import ResponseEditModal
        await interaction.response.send_modal(ResponseEditModal(self.cog, self.guild_id))

    async def _on_approval_edit(self, interaction: discord.Interaction):
        from .modals import ApprovalEditModal
        await interaction.response.send_modal(ApprovalEditModal(self.cog, self.guild_id))

    async def _on_options(self, interaction: discord.Interaction):
        from .modals import ApprovalOptionsModal
        try:
            await interaction.response.send_modal(ApprovalOptionsModal(self.cog, self.guild_id))
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def _on_colors(self, interaction: discord.Interaction):
        from .modals import AccentColorsModal
        try:
            await interaction.response.send_modal(AccentColorsModal(self.cog, self.guild_id))
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def _on_delete(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self.cog._purge_previous_c_session(self.guild_id)
        self.stop()


class SetupView(discord.ui.LayoutView):
    """Wizard inicial: 2 ChannelSelect + Confirmar."""

    def __init__(self, cog: "FormsCog", *, guild_id: int, staff_id: int):
        super().__init__(timeout=float(SETUP_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None
        self.selected_form_channel_id: int = 0
        self.selected_resp_channel_id: int = 0

        self.form_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de formulário",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_FORM_SELECT,
        )
        self.form_select.callback = self._on_form_select

        self.resp_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de respostas",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_RESP_SELECT,
        )
        self.resp_select.callback = self._on_resp_select

        self.confirm_btn = discord.ui.Button(
            label="Confirmar",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=CID_SETUP_CONFIRM_BTN,
            disabled=True,
        )
        self.confirm_btn.callback = self._on_confirm

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# ⚙️ Configuração do formulário"),
            discord.ui.TextDisplay(
                "Escolha o **canal de formulário** (onde o botão fica visível pra todos) e o **canal de respostas** (pra onde as submissões vão). Depois clique em Confirmar."
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(self.form_select),
            discord.ui.ActionRow(self.resp_select),
            discord.ui.ActionRow(self.confirm_btn),
            accent_color=discord.Color.blue(),
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message("Só quem abriu essa configuração pode usar.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    def _maybe_enable_confirm(self):
        self.confirm_btn.disabled = not (self.selected_form_channel_id and self.selected_resp_channel_id)

    async def _on_form_select(self, interaction: discord.Interaction):
        if not self.form_select.values:
            return
        self.selected_form_channel_id = int(self.form_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_resp_select(self, interaction: discord.Interaction):
        if not self.resp_select.values:
            return
        self.selected_resp_channel_id = int(self.resp_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_confirm(self, interaction: discord.Interaction):
        if not (self.selected_form_channel_id and self.selected_resp_channel_id):
            try:
                await interaction.response.send_message("Selecione os dois canais antes de confirmar.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        await self.cog._finalize_setup(
            interaction,
            setup_view=self,
            form_channel_id=self.selected_form_channel_id,
            resp_channel_id=self.selected_resp_channel_id,
        )
        self.stop()
