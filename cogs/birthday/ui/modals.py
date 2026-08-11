from __future__ import annotations

import re

import discord

from ..constants import (
    DEFAULT_REACTION,
    DEFAULT_TEMPLATES,
    TEMPLATE_LABELS,
    VARIABLE_HELP,
    VARIABLES_BY_TEMPLATE,
)
from ..helpers import (
    _announcement_time_from_config,
    _find_unknown_variables,
    _make_notice_view,
    _template_variable_hint,
)


class BirthdayMessageModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView", template_key: str):
        label = TEMPLATE_LABELS.get(template_key, template_key)
        super().__init__(title=label[:45])
        self.panel = view
        self.template_key = template_key
        cfg = view.cog._normalize_config(view.config)
        current = cfg["templates"].get(template_key) or DEFAULT_TEMPLATES.get(template_key, "")
        self.body_input = discord.ui.TextInput(
            label="Mensagem",
            style=discord.TextStyle.paragraph,
            default=str(current)[:4000],
            placeholder=_template_variable_hint(template_key)[:100],
            required=True,
            max_length=4000,
        )
        self.add_item(self.body_input)
        hint = _template_variable_hint(template_key)
        if hint:
            self.variables_input = discord.ui.TextInput(
                label="Variáveis compatíveis",
                style=discord.TextStyle.paragraph,
                default=hint[:4000],
                required=False,
                max_length=4000,
            )
            self.add_item(self.variables_input)

    async def on_submit(self, interaction: discord.Interaction):
        template = str(self.body_input.value or "").strip()
        if not template:
            await interaction.response.send_message(
                view=_make_notice_view("Mensagem vazia", "Escreva a mensagem antes de salvar.", ok=False),
                ephemeral=True,
            )
            return
        allowed = VARIABLES_BY_TEMPLATE.get(self.template_key, tuple(VARIABLE_HELP.keys()))
        unknown = _find_unknown_variables(template, allowed)
        if unknown:
            self.panel.pending_template_key = self.template_key
            self.panel.pending_template_value = template
            self.panel.notice = "Variáveis desconhecidas: " + ", ".join(f"${{{v}}}" for v in unknown)
            self.panel.go_to("confirm_template")
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        await self.panel.cog._set_template(interaction.guild, self.template_key, template)
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Mensagem salva."
        self.panel.return_to("messages")
        self.panel._rebuild()
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)


class BirthdayTimeModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView"):
        super().__init__(title="Horário dos avisos")
        self.panel = view
        cfg = view.cog._normalize_config(view.config)
        hour, minute = _announcement_time_from_config(cfg)
        self.time_input = discord.ui.TextInput(
            label="Horário",
            placeholder="09:00",
            default=f"{hour:02d}:{minute:02d}",
            required=True,
            max_length=5,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_time = str(self.time_input.value or "").strip()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw_time)
        if not m:
            await interaction.response.send_message(
                view=_make_notice_view("Horário inválido", "Use um horário no formato `09:00`.", ok=False),
                ephemeral=True,
            )
            return
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await interaction.response.send_message(
                view=_make_notice_view("Horário inválido", "Use um horário entre `00:00` e `23:59`.", ok=False),
                ephemeral=True,
            )
            return
        await self.panel.cog._update_config(interaction.guild.id, {"announce_hour": hour, "announce_minute": minute})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Horário salvo."
        self.panel.return_to("preferences")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class BirthdayPreferencesModal(discord.ui.Modal):
    def __init__(self, view: "BirthdayAdminView"):
        super().__init__(title="Preferências")
        self.panel = view
        cfg = view.cog._normalize_config(view.config)
        opts = cfg["options"]
        if not all(hasattr(discord.ui, attr) for attr in ("Label", "CheckboxGroup", "RadioGroup")):
            raise RuntimeError("discord.py 2.7+ é necessário para as preferências modernas.")

        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=3)
        for value, label, desc, default in (
            ("show_age", "Mostrar idade nos avisos", "Só funciona quando a pessoa informou o ano.", bool(opts.get("show_age", True))),
            ("group_announcements", "Juntar aniversariantes do dia", "Uma mensagem para todos no mesmo dia.", bool(opts.get("group_announcements", True))),
            ("delete_on_leave", "Remover quando sair do servidor", "Limpa o calendário quando o membro sai.", bool(opts.get("delete_on_leave", True))),
        ):
            self.flags_group.add_option(label=label, value=value, description=desc, default=default)

        self.leap_group = discord.ui.RadioGroup(required=True)
        leap_mode = str(opts.get("leap_day_mode") or "feb28")
        self.leap_group.add_option(label="Avisar em 28/02", value="feb28", description="Para aniversários em 29/02 fora de ano bissexto.", default=leap_mode != "mar01")
        self.leap_group.add_option(label="Avisar em 01/03", value="mar01", description="Para aniversários em 29/02 fora de ano bissexto.", default=leap_mode == "mar01")

        self.reaction_input = discord.ui.TextInput(
            label="Reação em data válida",
            placeholder="✅",
            default=str(opts.get("valid_reaction") or DEFAULT_REACTION)[:20],
            required=True,
            max_length=20,
        )
        self.add_item(discord.ui.Label(
            text="Comportamento",
            description="Marque o que deve ficar ativo.",
            component=self.flags_group,
        ))
        self.add_item(discord.ui.Label(
            text="Aniversários em 29/02",
            component=self.leap_group,
        ))
        self.add_item(self.reaction_input)

    async def on_submit(self, interaction: discord.Interaction):
        selected = set(getattr(self.flags_group, "values", None) or [])
        reaction = str(self.reaction_input.value or DEFAULT_REACTION).strip() or DEFAULT_REACTION
        options = {
            "show_age": "show_age" in selected,
            "group_announcements": "group_announcements" in selected,
            "delete_on_leave": "delete_on_leave" in selected,
            "leap_day_mode": str(getattr(self.leap_group, "value", None) or "feb28"),
            "valid_reaction": reaction,
        }
        await self.panel.cog._update_config(interaction.guild.id, {"options": options})
        self.panel.config = await self.panel.cog._get_config(int(interaction.guild.id))
        self.panel.notice = "Preferências salvas."
        self.panel.return_to("preferences")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
        await self.panel.cog._sync_public_calendar(interaction.guild)
