from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import discord

from .constants import (
    CANCEL_EMOJI,
    COUNTDOWN_START,
    STATE_BANNED,
    STATE_FAILED,
    STATE_STAFF_JOKE,
    WARNING_EMOJI,
)
from .state import ChallengeEntry, render_batch

if TYPE_CHECKING:
    from .cog import AntibotCog


def notice_view(title: str, message: str, *, ok: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}\n{message}".rstrip()),
            accent_color=discord.Color.green() if ok else discord.Color.red(),
        )
    )
    return view


def warning_view() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(
                "# 🪤 Canal de armadilha\n\n"
                f"**{WARNING_EMOJI} AVISO:** Não envie mensagens aqui, "
                "este canal é feito para detectar contas hackeadas, **se enviar alguma "
                "mensagem aqui vai resultar no seu banimento**"
            ),
            accent_color=discord.Color.red(),
        )
    )
    return view


def batch_view(entries: Iterable[ChallengeEntry], *, now: float) -> discord.ui.LayoutView:
    items = list(entries)
    text = render_batch(items, now=now, cancel_emoji=CANCEL_EMOJI)
    has_waiting = any(entry.is_waiting for entry in items)
    has_failure = any(entry.state == STATE_FAILED for entry in items)
    has_banned = any(entry.state == STATE_BANNED for entry in items)
    has_staff_joke = any(entry.state == STATE_STAFF_JOKE for entry in items)
    if has_failure or has_banned:
        color = discord.Color.red()
    elif has_waiting:
        color = discord.Color.orange()
    elif has_staff_joke:
        color = discord.Color.blurple()
    else:
        color = discord.Color.green()

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(text),
            accent_color=color,
        )
    )
    return view


class _TrapChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "AntibotPanelView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Escolha o canal de armadilha",
            min_values=1,
            max_values=1,
            custom_id=f"antibot:channel:{panel.guild_id}:{panel.owner_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0] if self.values else None
        channel_id = int(getattr(selected, "id", 0) or 0)
        channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                view=notice_view("Canal inválido", "Escolha um canal de texto", ok=False),
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            view=AntibotPanelView(
                self.panel.cog,
                owner_id=self.panel.owner_id,
                guild_id=self.panel.guild_id,
                selected_channel_id=channel_id,
            )
        )


class AntibotPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "AntibotCog",
        *,
        owner_id: int,
        guild_id: int,
        selected_channel_id: int | None = None,
        notice: str = "",
        notice_ok: bool | None = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        config = cog.get_config(self.guild_id)
        current_channel_id = int(config.get("channel_id") or 0)
        self.selected_channel_id = int(
            current_channel_id if selected_channel_id is None else selected_channel_id
        )
        enabled = bool(config.get("enabled") and current_channel_id)
        current = f"<#{current_channel_id}>" if current_channel_id else "Nenhum"
        selected = f"<#{self.selected_channel_id}>" if self.selected_channel_id else "Nenhum"
        lines = ["# Antibot"]
        if enabled:
            lines.append(f"Ativo · {current}")
        else:
            lines.append("Inativo")
        if self.selected_channel_id and self.selected_channel_id != current_channel_id:
            lines.append(f"Selecionado · {selected}")
        lines.append(f"Contagem regressiva · {COUNTDOWN_START} → 1")
        if notice:
            rendered_notice = f"-# {notice}" if notice_ok is True else str(notice)
            lines.extend(["", rendered_notice])

        if notice_ok is False:
            accent = discord.Color.red()
        elif notice_ok is True or enabled:
            accent = discord.Color.green()
        else:
            accent = discord.Color.dark_gray()

        activate = discord.ui.Button(
            label="Atualizar" if enabled else "Ativar",
            style=discord.ButtonStyle.success,
            disabled=self.selected_channel_id <= 0,
            custom_id=f"antibot:activate:{self.guild_id}:{self.owner_id}",
        )
        deactivate = discord.ui.Button(
            label="Desativar",
            style=discord.ButtonStyle.danger,
            disabled=not enabled,
            custom_id=f"antibot:disable:{self.guild_id}:{self.owner_id}",
        )
        activate.callback = self._activate
        deactivate.callback = self._deactivate

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                discord.ui.Separator(),
                discord.ui.ActionRow(_TrapChannelSelect(self)),
                discord.ui.ActionRow(activate, deactivate),
                accent_color=accent,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message(
                view=notice_view("Painel bloqueado", "Esse painel pertence a quem abriu o comando", ok=False),
                ephemeral=True,
            )
            return False
        if not await self.cog.is_staff(interaction.user):
            await interaction.response.send_message(
                view=notice_view("Sem permissão", "Somente a staff pode configurar a armadilha", ok=False),
                ephemeral=True,
            )
            return False
        return True

    async def _replace(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        try:
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            if interaction.message is not None:
                await interaction.message.edit(view=view)

    async def _activate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        ok, message = await self.cog.activate_trap(
            interaction.guild,
            self.selected_channel_id,
            actor_id=self.owner_id,
        )
        view = AntibotPanelView(
            self.cog,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            selected_channel_id=self.selected_channel_id,
            notice=message,
            notice_ok=ok,
        )
        await self._replace(interaction, view)

    async def _deactivate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        ok, message = await self.cog.deactivate_trap(
            interaction.guild,
            actor_id=self.owner_id,
        )
        view = AntibotPanelView(
            self.cog,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            notice=message,
            notice_ok=ok,
        )
        await self._replace(interaction, view)
