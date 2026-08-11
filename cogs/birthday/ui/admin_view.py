from __future__ import annotations

from typing import Any

import discord

from ..constants import (
    DEFAULT_REACTION,
    DEFAULT_TEMPLATES,
    TEMPLATE_DESCRIPTIONS,
    TEMPLATE_LABELS,
    VARIABLE_CATEGORIES,
    VARIABLE_HELP,
    VARIABLES_BY_TEMPLATE,
)
from ..helpers import (
    _announcement_time_from_config,
    _birthday_date,
    _channel_mention,
    _find_unknown_variables,
    _make_notice_view,
    _trim,
)
from ..models import CalendarEntry
from .buttons import _BackButton, _EntriesPageButton
from .selects import (
    _AdminMainSelect,
    _AnnounceChannelSelect,
    _PreferencesActionSelect,
    _RegisterChannelSelect,
    _RestoreConfirmSelect,
    _TemplateActionSelect,
    _TemplateSelect,
    _TestActionSelect,
    _UnknownTemplateConfirmSelect,
    _VariableCategorySelect,
)


class BirthdayAdminView(discord.ui.LayoutView):
    def __init__(self, cog: "BirthdayCog", *, owner_id: int, guild_id: int, config: dict[str, Any]):
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.config = cog._normalize_config(config)
        self.screen = "home"
        self.screen_history: list[str] = []
        self.notice = ""
        self.selected_template = "calendar"
        self.variable_category = "member"
        self.pending_template_key = ""
        self.pending_template_value = ""
        self.entries_page = 0
        self.entries_per_page = 10
        self.entries_cache: list[CalendarEntry] = []
        self._rebuild()


    def go_to(self, screen: str, *, remember: bool = True):
        if remember and self.screen != screen:
            self.screen_history.append(self.screen)
        self.screen = screen

    def go_back(self):
        previous = self.screen_history.pop() if self.screen_history else "home"
        self.screen = previous
        self.notice = ""

    def return_to(self, screen: str):
        while self.screen_history and self.screen_history[-1] == screen:
            self.screen_history.pop()
        self.screen = screen

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Painel em uso", "Esse painel pertence a quem abriu o comando.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        if not self.cog._can_manage(interaction.user):
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        return True

    def _clear(self):
        for item in list(self.children):
            self.remove_item(item)

    def _base_lines(self) -> list[str]:
        cfg = self.config
        opts = cfg["options"]
        register_channel_id = int(cfg.get("register_channel_id") or 0)
        announce_channel_id = int(cfg.get("announce_channel_id") or 0)
        count = int(cfg.get("birthday_count") or 0)
        hour, minute = _announcement_time_from_config(cfg)
        lines = [
            "# 🎂 Aniversários",
            "",
            f"**Cadastro**\n{_channel_mention(register_channel_id)}",
            "",
            f"**Avisos**\n{_channel_mention(announce_channel_id)} às `{hour:02d}:{minute:02d}`",
            "",
            f"**Calendário**\n{count} aniversariante(s) salvo(s)",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        lines.extend(["", "Escolha abaixo o que deseja ajustar."])
        return lines

    def _rebuild(self):
        self._clear()
        if self.screen == "home":
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._base_lines())),
                discord.ui.Separator(),
                discord.ui.ActionRow(_AdminMainSelect(self)),
                accent_color=discord.Color.blurple(),
            ))
            return
        if self.screen == "register":
            self._build_register()
            return
        if self.screen == "announce":
            self._build_announce()
            return
        if self.screen == "messages":
            self._build_messages()
            return
        if self.screen == "template_vars":
            self._build_template_vars()
            return
        if self.screen == "confirm_restore":
            self._build_restore_confirm()
            return
        if self.screen == "confirm_template":
            self._build_unknown_template_confirm()
            return
        if self.screen == "variables":
            self._build_variables()
            return
        if self.screen == "preferences":
            self._build_preferences()
            return
        if self.screen == "tests":
            self._build_tests()
            return
        if self.screen == "entries":
            self._build_entries()
            return
        self.screen = "home"
        self._rebuild()

    def _build_register(self):
        channel_id = int(self.config.get("register_channel_id") or 0)
        lines = [
            "# 📍 Cadastro",
            "Escolha o canal onde o calendário público vai aparecer.",
            "",
            f"**Canal atual**\n{_channel_mention(channel_id)}",
            "",
            "Os membros vão enviar a data no espaço de aniversários abaixo da mensagem pública.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RegisterChannelSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_announce(self):
        channel_id = int(self.config.get("announce_channel_id") or 0)
        hour, minute = _announcement_time_from_config(self.config)
        lines = [
            "# 📢 Avisos",
            "Escolha onde o bot vai mandar os parabéns.",
            "",
            f"**Canal atual**\n{_channel_mention(channel_id)}",
            f"**Horário**\n`{hour:02d}:{minute:02d}`",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_AnnounceChannelSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_messages(self):
        key = self.selected_template
        template = self.config["templates"].get(key) or DEFAULT_TEMPLATES.get(key, "")
        unknown = _find_unknown_variables(template, VARIABLES_BY_TEMPLATE.get(key, tuple()))
        lines = [
            "# 💬 Mensagens",
            TEMPLATE_DESCRIPTIONS.get(key, "Escolha uma mensagem para editar."),
            "",
            f"**Selecionada:** {TEMPLATE_LABELS.get(key, key)}",
            "",
            "**Prévia do template salvo:**",
            "```",
            _trim(template, 900),
            "```",
        ]
        if unknown:
            lines.extend(["", "⚠️ Variáveis desconhecidas: " + ", ".join(f"${{{v}}}" for v in unknown)])
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TemplateSelect(self)),
            discord.ui.ActionRow(_TemplateActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_template_vars(self):
        key = self.selected_template
        allowed = VARIABLES_BY_TEMPLATE.get(key, tuple())
        lines = [f"# 📌 Variáveis para {TEMPLATE_LABELS.get(key, key)}", ""]
        for var in allowed:
            lines.append(f"`${{{var}}}` — {VARIABLE_HELP.get(var, 'variável disponível')}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TemplateActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_restore_confirm(self):
        key = self.selected_template
        lines = [
            "# Restaurar padrão",
            f"Mensagem: **{TEMPLATE_LABELS.get(key, key)}**",
            "",
            "Escolha abaixo como continuar.",
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RestoreConfirmSelect(self)),
            accent_color=discord.Color.orange(),
        ))

    def _build_unknown_template_confirm(self):
        lines = ["# Variáveis desconhecidas", self.notice or "Essa mensagem usa variáveis que não existem para esse tipo de configuração.", "", "Escolha como continuar."]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_UnknownTemplateConfirmSelect(self)),
            accent_color=discord.Color.orange(),
        ))

    def _build_variables(self):
        category = self.variable_category
        lines = ["# 📌 Variáveis", "Escolha uma categoria para ver o que pode ser usado nas mensagens.", ""]
        for var in VARIABLE_CATEGORIES.get(category, tuple()):
            lines.append(f"`${{{var}}}` — {VARIABLE_HELP.get(var, 'variável disponível')}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_VariableCategorySelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_preferences(self):
        cfg = self.config
        opts = cfg["options"]
        hour, minute = _announcement_time_from_config(cfg)
        lines = [
            "# ⚙️ Preferências",
            f"**Horário dos avisos:** `{hour:02d}:{minute:02d}`",
            "-# O fuso horário é definido em Geral no dashboard",
            f"**Reação em data válida:** {opts.get('valid_reaction') or DEFAULT_REACTION}",
            f"**Idade nos avisos:** {'sim' if opts.get('show_age', True) else 'não'}",
            f"**Avisos agrupados:** {'sim' if opts.get('group_announcements', True) else 'não'}",
            f"**29/02:** {'01/03' if opts.get('leap_day_mode') == 'mar01' else '28/02'}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_PreferencesActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_tests(self):
        lines = [
            "# 🧪 Testes",
            "Veja uma prévia das mensagens sem esperar chegar o dia.",
            "",
            "Os testes usam você como exemplo e não alteram o calendário.",
        ]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_TestActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    def _build_entries(self):
        entries = self.entries_cache
        start = self.entries_page * self.entries_per_page
        page_entries = entries[start:start + self.entries_per_page]
        total_pages = max(1, (len(entries) + self.entries_per_page - 1) // self.entries_per_page)
        lines = ["# 📋 Aniversariantes", f"{len(entries)} aniversariante(s) salvo(s)", f"Página {self.entries_page + 1}/{total_pages}", ""]
        if not page_entries:
            lines.append("Nenhum aniversariante encontrado.")
        else:
            for entry in page_entries:
                lines.append(f"• {entry.display_name} — {_birthday_date(entry.day, entry.month)}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_EntriesPageButton(self, direction="prev"), _EntriesPageButton(self, direction="next"), _BackButton(self)),
            accent_color=discord.Color.blurple(),
        ))

    async def _reload_entries(self):
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            self.entries_cache = []
            return
        entries = await self.cog._calendar_entries(guild, cleanup_missing=False)
        self.entries_cache = entries
