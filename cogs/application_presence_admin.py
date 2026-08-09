from __future__ import annotations

import contextlib
import re
from typing import Any

import discord
from discord.ext import commands

from utility.application_presence import (
    MAX_STATUS_COUNT,
    MAX_STATUS_DURATION_SECONDS,
    MAX_STATUS_TEXT_LENGTH,
    MIN_STATUS_DURATION_SECONDS,
    ApplicationPresenceService,
)

try:
    import config
except Exception:  # pragma: no cover - fallback para testes isolados
    config = None  # type: ignore[assignment]


ACTION_ADD = "__add__"
ACTION_REMOVE = "__remove__"
ACTION_TOGGLE = "__toggle__"


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _configured_owner_ids(bot: commands.Bot) -> set[int]:
    owner_ids: set[int] = set()
    if config is not None:
        for attr in ("BOT_OWNER_ID", "OWNER_ID"):
            value = _safe_int(getattr(config, attr, 0))
            if value:
                owner_ids.add(value)
    value = _safe_int(getattr(bot, "owner_id", 0))
    if value:
        owner_ids.add(value)
    for raw in getattr(bot, "owner_ids", None) or []:
        value = _safe_int(raw)
        if value:
            owner_ids.add(value)
    return owner_ids


async def _is_owner(bot: commands.Bot, user: discord.abc.User) -> bool:
    user_id = _safe_int(getattr(user, "id", 0))
    configured = _configured_owner_ids(bot)
    if configured and user_id in configured:
        return True
    try:
        return bool(await bot.is_owner(user))
    except Exception:
        return False


def _trim(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _one_line(value: object, limit: int = 96) -> str:
    return _trim(re.sub(r"\s+", " ", str(value or "")).strip(), limit)


def _format_duration(seconds: int | float | None) -> str:
    try:
        total = max(0, int(round(float(seconds or 0))))
    except Exception:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts[:2])


_DURATION_TOKEN_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>dias?|d|horas?|hrs?|h|minutos?|mins?|min|m|segundos?|segs?|sec|s)",
    flags=re.IGNORECASE,
)


def _parse_duration(value: str) -> int:
    raw = str(value or "").strip().lower()
    if not raw:
        raise ValueError("Informe a duração, por exemplo `1m` ou `2m 30s`.")
    if raw.isdigit():
        total = int(raw)
    else:
        total_float = 0.0
        matches = list(_DURATION_TOKEN_RE.finditer(raw))
        if not matches:
            raise ValueError("Use uma duração como `30s`, `1m`, `2m 30s` ou `1h`.")
        leftovers = _DURATION_TOKEN_RE.sub("", raw)
        if leftovers.strip(" ,;+/"):
            raise ValueError("Não entendi a duração. Exemplos: `30s`, `1m`, `2m 30s`, `1h`.")
        for match in matches:
            number = float(match.group("value").replace(",", "."))
            unit = match.group("unit").lower()
            if unit.startswith("d"):
                multiplier = 86400
            elif unit.startswith("h"):
                multiplier = 3600
            elif unit.startswith("m"):
                multiplier = 60
            else:
                multiplier = 1
            total_float += number * multiplier
        total = int(round(total_float))

    if total < MIN_STATUS_DURATION_SECONDS:
        raise ValueError(f"A duração mínima é {_format_duration(MIN_STATUS_DURATION_SECONDS)}.")
    if total > MAX_STATUS_DURATION_SECONDS:
        raise ValueError("A duração máxima é 24 h.")
    return total


async def _respond_error(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


class _OwnedLayoutView(discord.ui.LayoutView):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _safe_int(getattr(interaction.user, "id", 0)) != self.owner_id:
            await _respond_error(interaction, "Esse painel pertence a quem abriu o comando.")
            return False
        return True


class PresencePanelView(_OwnedLayoutView):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int):
        super().__init__(cog, owner_id)
        snapshot = cog.service.get_snapshot()
        statuses = list(snapshot.get("statuses") or [])
        current_id = str(snapshot.get("current_id") or "")
        maintenance = bool(snapshot.get("maintenance_active"))
        rotation_enabled = bool(snapshot.get("enabled"))

        if maintenance:
            state_line = f"🟡 Atualizando · {len(statuses)} status"
        elif not statuses:
            state_line = "⚪ Nenhum status configurado"
        elif len(statuses) == 1:
            state_line = "🟢 1 status · sem rotação"
        elif not rotation_enabled:
            state_line = f"⏸️ {len(statuses)} status · rotação pausada"
        else:
            remaining = snapshot.get("remaining_seconds")
            suffix = f" · próximo em ~{_format_duration(remaining)}" if remaining is not None else ""
            state_line = f"🟢 {len(statuses)} status{suffix}"

        rendered = str(snapshot.get("current_rendered") or "").strip()
        current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else None
        if maintenance:
            now_block = "**Agora**\nAtualizando"
        elif rendered and current:
            now_block = f"**Agora**\n{_trim(rendered, 300)}\n`{_format_duration(current.get('duration_seconds'))}`"
        else:
            now_block = "**Agora**\n_Sem atividade personalizada_"

        options: list[discord.SelectOption] = []
        for index, item in enumerate(statuses, start=1):
            status_id = str(item.get("id") or "")
            text = _one_line(item.get("text") or "Status", 88)
            description = f"{_format_duration(item.get('duration_seconds'))} · posição {index}"
            if status_id == current_id:
                description += " · agora"
            options.append(
                discord.SelectOption(
                    label=text or f"Status {index}",
                    value=f"status:{status_id}",
                    description=_trim(description, 100),
                    emoji="💬",
                )
            )

        if len(statuses) < MAX_STATUS_COUNT:
            options.append(
                discord.SelectOption(
                    label="Adicionar status",
                    value=ACTION_ADD,
                    description="Cria um novo item no fim da rotação",
                    emoji="➕",
                )
            )
        if statuses:
            options.append(
                discord.SelectOption(
                    label="Remover status",
                    value=ACTION_REMOVE,
                    description="Escolha qual item deseja retirar",
                    emoji="🗑️",
                )
            )
        if len(statuses) > 1:
            options.append(
                discord.SelectOption(
                    label="Retomar rotação" if not rotation_enabled else "Pausar rotação",
                    value=ACTION_TOGGLE,
                    description="Mantém o status atual" if rotation_enabled else "Continua do ponto em que parou",
                    emoji="▶️" if not rotation_enabled else "⏸️",
                )
            )

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("# Presença"),
            discord.ui.TextDisplay(f"{state_line}\n\n{now_block}"),
        ]
        if options:
            select = discord.ui.Select(
                placeholder="Escolha um status ou uma ação...",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"application_presence:main:{self.owner_id}",
            )
            select.callback = self._on_select
            self.select = select
            children.extend([discord.ui.Separator(), discord.ui.ActionRow(select)])
        else:
            # Só ocorre se o limite for atingido e não houver status, estado que
            # a normalização não produz; permanece como fallback defensivo.
            children.append(discord.ui.TextDisplay("_Nenhuma ação disponível._"))

        self.add_item(discord.ui.Container(*children, accent_color=discord.Color.blurple()))

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = str((self.select.values or [""])[0])
        if value == ACTION_ADD:
            await interaction.response.send_modal(PresenceAddModal(self.cog, self.owner_id))
            return
        if value == ACTION_REMOVE:
            await interaction.response.edit_message(view=PresenceRemovePickerView(self.cog, self.owner_id))
            return
        if value == ACTION_TOGGLE:
            try:
                await self.cog.service.set_rotation_enabled(not self.cog.service.rotation_enabled)
            except Exception:
                await _respond_error(interaction, "Não consegui alterar a rotação. A configuração anterior foi mantida.")
                return
            await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))
            return
        if value.startswith("status:"):
            status_id = value.split(":", 1)[1]
            item = self.cog.status_by_id(status_id)
            if item is None:
                await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))
                return
            await interaction.response.send_modal(PresenceEditModal(self.cog, self.owner_id, status_id))
            return
        await _respond_error(interaction, "Essa opção não está mais disponível.")


class PresenceRemovePickerView(_OwnedLayoutView):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int):
        super().__init__(cog, owner_id)
        snapshot = cog.service.get_snapshot()
        statuses = list(snapshot.get("statuses") or [])
        options: list[discord.SelectOption] = []
        for index, item in enumerate(statuses, start=1):
            options.append(
                discord.SelectOption(
                    label=_one_line(item.get("text") or f"Status {index}", 90),
                    value=str(item.get("id") or ""),
                    description=_trim(f"{_format_duration(item.get('duration_seconds'))} · posição {index}", 100),
                    emoji="💬",
                )
            )

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("# Remover status"),
            discord.ui.TextDisplay("Escolha o item que você quer retirar da rotação."),
            discord.ui.Separator(),
        ]
        if options:
            select = discord.ui.Select(
                placeholder="Qual status deseja remover?",
                options=options,
                min_values=1,
                max_values=1,
                custom_id=f"application_presence:remove_pick:{self.owner_id}",
            )
            select.callback = self._on_select
            self.select = select
            children.append(discord.ui.ActionRow(select))
        else:
            children.append(discord.ui.TextDisplay("_Não há status para remover._"))

        back = discord.ui.Button(
            label="Voltar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"application_presence:remove_back:{self.owner_id}",
        )
        back.callback = self._on_back
        children.append(discord.ui.ActionRow(back))
        self.add_item(discord.ui.Container(*children, accent_color=discord.Color.blurple()))

    async def _on_select(self, interaction: discord.Interaction) -> None:
        status_id = str((self.select.values or [""])[0])
        item = self.cog.status_by_id(status_id)
        if item is None:
            await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))
            return
        await interaction.response.edit_message(
            view=PresenceRemoveConfirmView(self.cog, self.owner_id, status_id)
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))


class PresenceRemoveConfirmView(_OwnedLayoutView):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int, status_id: str):
        super().__init__(cog, owner_id, timeout=180)
        self.status_id = str(status_id)
        item = cog.status_by_id(self.status_id) or {}
        text = _trim(item.get("text") or "esse status", 350)

        confirm = discord.ui.Button(
            label="Remover",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=f"application_presence:remove_confirm:{self.owner_id}",
        )
        cancel = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"application_presence:remove_cancel:{self.owner_id}",
        )
        confirm.callback = self._on_confirm
        cancel.callback = self._on_cancel
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Remover status"),
                discord.ui.TextDisplay(f"Tem certeza que quer remover?\n\n{text}"),
                discord.ui.Separator(),
                discord.ui.ActionRow(confirm, cancel),
                accent_color=discord.Color.red(),
            )
        )

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        try:
            await self.cog.service.remove_status(self.status_id)
        except ValueError as exc:
            await _respond_error(interaction, str(exc))
            return
        except Exception:
            await _respond_error(interaction, "Não consegui remover o status. Nada foi alterado.")
            return
        await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=PresencePanelView(self.cog, self.owner_id))


class PresenceAddModal(discord.ui.Modal):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int):
        super().__init__(title="Novo status")
        self.cog = cog
        self.owner_id = int(owner_id)
        self.text_input = discord.ui.TextInput(
            label="Texto",
            placeholder="Ex.: 「🌐」_help • {n:sv} servidores",
            max_length=MAX_STATUS_TEXT_LENGTH,
            required=True,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duração",
            placeholder="Ex.: 1m, 2m 30s, 1h",
            default="1m",
            max_length=24,
            required=True,
        )
        self.add_item(self.text_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if _safe_int(getattr(interaction.user, "id", 0)) != self.owner_id:
            await _respond_error(interaction, "Esse painel pertence a quem abriu o comando.")
            return
        try:
            duration = _parse_duration(str(self.duration_input.value or ""))
            await self.cog.service.add_status(str(self.text_input.value or ""), duration)
        except ValueError as exc:
            await _respond_error(interaction, str(exc))
            return
        except Exception:
            await _respond_error(interaction, "Não consegui salvar o status. A configuração anterior foi mantida.")
            return
        await self.cog.refresh_after_modal(interaction, self.owner_id, "Status adicionado")


class PresenceEditModal(discord.ui.Modal):
    def __init__(self, cog: "ApplicationPresenceAdminCog", owner_id: int, status_id: str):
        item = cog.status_by_id(status_id)
        if item is None:
            raise ValueError("Status inexistente")
        snapshot = cog.service.get_snapshot()
        statuses = list(snapshot.get("statuses") or [])
        position = next(
            (index for index, value in enumerate(statuses, start=1) if str(value.get("id") or "") == status_id),
            1,
        )
        super().__init__(title="Editar status")
        self.cog = cog
        self.owner_id = int(owner_id)
        self.status_id = str(status_id)
        self.text_input = discord.ui.TextInput(
            label="Texto",
            default=str(item.get("text") or "")[:MAX_STATUS_TEXT_LENGTH],
            placeholder="Variáveis: {n:m}, {n:sv}, {n:ping}, {n:up}",
            max_length=MAX_STATUS_TEXT_LENGTH,
            required=True,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duração",
            default=_compact_duration(int(item.get("duration_seconds") or 60)),
            placeholder="Ex.: 1m, 2m 30s, 1h",
            max_length=24,
            required=True,
        )
        self.position_input = discord.ui.TextInput(
            label="Posição na rotação",
            default=str(position),
            placeholder=f"1 a {max(1, len(statuses))}",
            max_length=3,
            required=True,
        )
        self.add_item(self.text_input)
        self.add_item(self.duration_input)
        self.add_item(self.position_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if _safe_int(getattr(interaction.user, "id", 0)) != self.owner_id:
            await _respond_error(interaction, "Esse painel pertence a quem abriu o comando.")
            return
        try:
            duration = _parse_duration(str(self.duration_input.value or ""))
            snapshot = self.cog.service.get_snapshot()
            count = len(snapshot.get("statuses") or [])
            position = int(str(self.position_input.value or "").strip())
            if position < 1 or position > max(1, count):
                raise ValueError(f"A posição deve ficar entre 1 e {max(1, count)}.")
            await self.cog.service.update_status(
                self.status_id,
                text=str(self.text_input.value or ""),
                duration_seconds=duration,
                position=position,
            )
        except ValueError as exc:
            await _respond_error(interaction, str(exc))
            return
        except Exception:
            await _respond_error(interaction, "Não consegui salvar a alteração. A configuração anterior foi mantida.")
            return
        await self.cog.refresh_after_modal(interaction, self.owner_id, "Status atualizado")


def _compact_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


class ApplicationPresenceAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        service = getattr(bot, "application_presence", None)
        if not isinstance(service, ApplicationPresenceService):
            raise RuntimeError("ApplicationPresenceService não está disponível no bot")
        self.service: ApplicationPresenceService = service

    def status_by_id(self, status_id: str) -> dict[str, Any] | None:
        snapshot = self.service.get_snapshot()
        for item in snapshot.get("statuses") or []:
            if str(item.get("id") or "") == str(status_id):
                return item
        return None

    async def refresh_after_modal(self, interaction: discord.Interaction, owner_id: int, feedback: str) -> None:
        # Modal de componente pode editar a mensagem de origem diretamente. Se
        # o Discord rejeitar essa forma em algum cliente, cai para um feedback
        # efêmero sem deixar a alteração parcialmente aplicada.
        try:
            await interaction.response.edit_message(view=PresencePanelView(self, owner_id))
            return
        except Exception:
            pass
        with contextlib.suppress(Exception):
            if not interaction.response.is_done():
                await interaction.response.send_message(feedback, ephemeral=True)
            else:
                await interaction.followup.send(feedback, ephemeral=True)

    @commands.command(name="status", aliases=["presenca", "presence"])
    async def presence_command(self, ctx: commands.Context) -> None:
        if not await _is_owner(self.bot, ctx.author):
            return
        await ctx.reply(
            view=PresencePanelView(self, int(ctx.author.id)),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ApplicationPresenceAdminCog(bot))
