from __future__ import annotations

import discord

from ..constants import (
    DEFAULT_TEMPLATES,
    TEMPLATE_DESCRIPTIONS,
    TEMPLATE_LABELS,
)
from ..helpers import _make_notice_view
from .modals import BirthdayMessageModal, BirthdayPreferencesModal, BirthdayTimeModal


class _AdminMainSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="O que você quer configurar?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Cadastro", value="register", emoji="📍", description="Canal onde fica o calendário."),
                discord.SelectOption(label="Avisos", value="announce", emoji="📢", description="Canal e horário dos parabéns."),
                discord.SelectOption(label="Mensagens", value="messages", emoji="💬", description="Textos configuráveis do fluxo."),
                discord.SelectOption(label="Preferências", value="preferences", emoji="⚙️", description="Reação, idade, 29/02 e agrupamento."),
                discord.SelectOption(label="Testes", value="tests", emoji="🧪", description="Prévia sem esperar o dia."),
                discord.SelectOption(label="Aniversariantes", value="entries", emoji="📋", description="Ver e gerenciar aniversariantes."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        target = str(self.values[0])
        if target == "entries":
            self.panel.entries_page = 0
            await self.panel._reload_entries()
        self.panel.go_to(target)
        self.panel.notice = ""
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)



class _RegisterChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal onde o calendário vai ficar",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, selected)
        if channel is None:
            await interaction.response.send_message(
                view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False), ephemeral=True
            )
            return
        missing = self.panel.cog._missing_channel_permissions(channel, for_register=True)
        if missing:
            await interaction.response.send_message(
                view=_make_notice_view("Permissões insuficientes", missing, ok=False), ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.panel.cog._set_register_channel(interaction.guild, channel)
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = f"Cadastro ajustado para {channel.mention}."
        self.panel.screen = "register"
        self.panel._rebuild()
        await interaction.message.edit(view=self.panel)


class _AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal onde os parabéns serão enviados",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, selected)
        if channel is None:
            await interaction.response.send_message(
                view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False), ephemeral=True
            )
            return
        missing = self.panel.cog._missing_channel_permissions(channel, for_register=False)
        if missing:
            await interaction.response.send_message(
                view=_make_notice_view("Permissões insuficientes", missing, ok=False), ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.panel.cog._update_config(interaction.guild.id, {"announce_channel_id": int(channel.id)})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = f"Avisos ajustados para {channel.mention}."
        self.panel.screen = "announce"
        self.panel._rebuild()
        await interaction.message.edit(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _TemplateSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        current = panel.selected_template
        options = []
        for key, label in TEMPLATE_LABELS.items():
            options.append(discord.SelectOption(
                label=label,
                value=key,
                description=TEMPLATE_DESCRIPTIONS.get(key, "")[:100],
                default=(key == current),
            ))
        super().__init__(placeholder="Mensagem para editar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_template = str(self.values[0])
        self.panel.notice = ""
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _TemplateActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Ação para a mensagem selecionada",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Editar mensagem", value="edit", emoji="✏️"),
                discord.SelectOption(label="Ver prévia", value="preview", emoji="👀"),
                discord.SelectOption(label="Restaurar padrão", value="restore", emoji="↩️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.selected_template
        if action == "edit":
            await interaction.response.send_modal(BirthdayMessageModal(self.panel, key))
            return
        if action == "preview":
            await self.panel.cog._send_template_preview(interaction, key)
            return
        if action == "restore":
            self.panel.go_to("confirm_restore")
            self.panel.notice = ""
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return


class _RestoreConfirmSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Escolha como continuar",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️"),
                discord.SelectOption(label="Ver prévia antes", value="preview", emoji="👀"),
                discord.SelectOption(label="Manter como está", value="cancel", emoji="✨"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.selected_template
        if action == "preview":
            await self.panel.cog._send_template_preview(interaction, key, use_default=True)
            return
        if action == "cancel":
            self.panel.return_to("messages")
            self.panel.notice = "Nada foi alterado."
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        await self.panel.cog._set_template(interaction.guild, key, DEFAULT_TEMPLATES.get(key, ""))
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.return_to("messages")
        self.panel.notice = "Mensagem padrão restaurada."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _UnknownTemplateConfirmSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Variáveis desconhecidas encontradas",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Editar de novo", value="edit", emoji="✏️"),
                discord.SelectOption(label="Salvar mesmo assim", value="save", emoji="✅"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        key = self.panel.pending_template_key or self.panel.selected_template
        if action == "edit":
            await interaction.response.send_modal(BirthdayMessageModal(self.panel, key))
            return
        await self.panel.cog._set_template(interaction.guild, key, self.panel.pending_template_value or "")
        self.panel.pending_template_key = ""
        self.panel.pending_template_value = ""
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.return_to("messages")
        self.panel.notice = "Mensagem salva."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class _VariableCategorySelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        labels = {
            "member": ("Membro", "Variáveis do usuário."),
            "birthday": ("Aniversário", "Data, idade e timestamp."),
            "calendar": ("Calendário", "Lista pública e próximos aniversários."),
            "server": ("Servidor", "Nome, canais e contagem."),
            "time": ("Horário", "Timestamp e horário atual."),
            "invalid": ("Mensagem inválida", "Texto enviado e exemplo válido."),
        }
        options = [discord.SelectOption(label=label, value=key, description=desc) for key, (label, desc) in labels.items()]
        super().__init__(placeholder="Categoria de variáveis", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.variable_category = str(self.values[0])
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _PreferencesActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="O que ajustar?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Preferências gerais", value="prefs", emoji="⚙️", description="Reação, 29/02, agrupamento e limpeza."),
                discord.SelectOption(label="Horário dos avisos", value="time", emoji="🕘", description="Hora usada nos avisos automáticos."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "time":
            await interaction.response.send_modal(BirthdayTimeModal(self.panel))
            return
        try:
            await interaction.response.send_modal(BirthdayPreferencesModal(self.panel))
        except RuntimeError as exc:
            await interaction.response.send_message(view=_make_notice_view("Indisponível", str(exc), ok=False), ephemeral=True)


class _TestActionSelect(discord.ui.Select):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Teste que deseja fazer",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Prévia do calendário", value="calendar", emoji="📅"),
                discord.SelectOption(label="Aviso individual", value="single", emoji="🎂"),
                discord.SelectOption(label="Aviso agrupado", value="group", emoji="🎉"),
                discord.SelectOption(label="Enviar teste no canal de avisos", value="send", emoji="📨"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.panel.cog._handle_test_action(interaction, str(self.values[0]))
