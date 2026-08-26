import asyncio
import io
import logging
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands as dcommands

from config import TRIGGER_WORD

from .cog import GamesCore
from .constants import (
    CHIPS_DEFAULT,
    CHIPS_INITIAL,
    CHIPS_MENDIGAR_COOLDOWN_SECONDS,
    CHIPS_MENDIGAR_TIMEOUT_SECONDS,
    CHIPS_PAY_MIN_BALANCE,
    CHIPS_PAY_RECEIVER_MAX_BALANCE,
    RACE_REROLL_COST,
    _guild_scoped,
)


_CALL_COMMAND_NAME = str(TRIGGER_WORD or "pinto").strip().casefold()
if not re.fullmatch(r"[a-z0-9_-]{1,32}", _CALL_COMMAND_NAME) or _CALL_COMMAND_NAME in {
    "ficha", "daily", "recarga", "economia", "rank", "poker", "truco", "roubar",
    "pay", "race", "roleta", "carta", "corrida", "alvo", "buckshot", "mendigar",
    "focus", "pica", "rola", "dj",
}:
    _CALL_COMMAND_NAME = "pinto"


log = logging.getLogger(__name__)


class _CommandMessageProxy:
    """Mensagem real com conteúdo adaptado para reutilizar handlers de trigger."""

    def __init__(self, message: discord.Message, *, content: str, mentions: list[discord.Member] | None = None):
        self._message = message
        self.content = str(content)
        self.mentions = list(mentions or [])
        self.raw_mentions = [int(member.id) for member in self.mentions]

    def __getattr__(self, name):
        return getattr(self._message, name)


class _MendigarRequestView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "GamesCog",
        *,
        guild_id: int,
        author_id: int,
        author_mention: str,
        amount: int,
        target_id: int | None,
        target_mention: str | None,
        timeout: float = CHIPS_MENDIGAR_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=float(timeout))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.author_id = int(author_id)
        self.author_mention = str(author_mention)
        self.amount = int(amount)
        self.target_id = int(target_id) if target_id else None
        self.target_mention = str(target_mention) if target_mention else None
        self.fulfilled = False
        self.message: discord.Message | None = None

        donate_button = discord.ui.Button(
            label=f"Dar {self.amount} fichas",
            emoji="💸",
            style=discord.ButtonStyle.success,
        )
        donate_button.callback = self._handle_donate
        row = discord.ui.ActionRow(donate_button)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._build_header_lines())),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(self._build_info_lines())),
                row,
                accent_color=discord.Color.orange(),
            )
        )

    def _build_header_lines(self) -> list[str]:
        return [
            "# 🥺 Esmola",
            f"Este pobre usuário necessitado está pedindo uma esmola de {self.cog._chip_amount(self.amount)}",
        ]

    def _build_info_lines(self) -> list[str]:
        lines = [f"**Pedinte:** {self.author_mention}"]
        if self.target_mention:
            lines.append(f"**Convocado para ajudar:** {self.target_mention}")
        else:
            lines.append("Qualquer alma bondosa com fichas normais suficientes pode ajudar no botão abaixo")
        marker = self.cog._race_effect_message(self.guild_id, self.author_id, "labia")
        if marker:
            lines.append(marker)
        lines.append(f"A esmola expira em **{int(CHIPS_MENDIGAR_TIMEOUT_SECONDS // 60)} minutos**")
        return lines

    async def _handle_donate(self, interaction: discord.Interaction):
        if self.fulfilled:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola encerrada", ["Esse pedido já foi atendido"], ok=False),
                ephemeral=True,
            )
            return
        if interaction.guild is None or int(interaction.guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Esse pedido só pode ser usado no servidor original"], ok=False),
                ephemeral=True,
            )
            return
        donor = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        recipient = interaction.guild.get_member(self.author_id)
        if donor is None or recipient is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Não consegui localizar todo mundo para concluir essa esmola"], ok=False),
                ephemeral=True,
            )
            return
        if donor.bot:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Bots não podem dar esmola"], ok=False),
                ephemeral=True,
            )
            return
        if donor.id == recipient.id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Você não pode dar esmola para si mesmo"], ok=False),
                ephemeral=True,
            )
            return
        if self.target_id and int(donor.id) != int(self.target_id):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola reservada", ["Essa esmola foi direcionada para outra pessoa decidir"], ok=False),
                ephemeral=True,
            )
            return

        await self.cog._maybe_execute_due_chip_season_reset(self.guild_id)
        donor_doc = self.cog.db._get_user_doc(self.guild_id, donor.id)
        last_donation = float(donor_doc.get("last_esmola_at", 0) or 0)
        remaining = (last_donation + CHIPS_MENDIGAR_COOLDOWN_SECONDS) - time.time()
        if remaining > 0:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    ["Você já ajudou alguém com esmola recentemente", f"Tente novamente em **{self.cog._format_wait_compact(remaining)}**"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        donor_chips = int(self.cog.db.get_user_chips(self.guild_id, donor.id, default=CHIPS_INITIAL) or 0)
        recipient_chips = int(self.cog.db.get_user_chips(self.guild_id, recipient.id, default=CHIPS_INITIAL) or 0)
        if donor_chips < CHIPS_PAY_MIN_BALANCE:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    [f"Você precisa ter pelo menos **{CHIPS_PAY_MIN_BALANCE} fichas normais** para dar esmola"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if not self.cog._user_has_played_any_game(self.guild_id, donor.id):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    ["Você precisa participar de pelo menos **1 jogo** antes de dar esmola"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if donor_chips < self.amount:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    ["Você não tem fichas normais suficientes para cobrir essa esmola", "Fichas bônus não entram nessa conta"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if recipient_chips + self.amount > CHIPS_PAY_RECEIVER_MAX_BALANCE:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    [f"{recipient.mention} passaria de **{CHIPS_PAY_RECEIVER_MAX_BALANCE} fichas normais** com essa esmola"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        request_limit, request_window = self.cog._limited_action_config(self.guild_id, recipient.id, action="mendigar")
        can_request, _request_state = await self.cog._consume_limited_action(
            self.guild_id,
            recipient.id,
            storage_key="race_mendigar",
            limit=request_limit,
            window_seconds=request_window,
            legacy_field="last_mendigar_at",
        )
        if not can_request:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    ["Esse pedido já ficou velho demais para valer uma nova esmola agora"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await self.cog._transfer_user_chips(
            self.guild_id, donor.id, recipient.id,
            total=self.amount, net_amount=self.amount,
            payer_reason=f"Esmola para {recipient.display_name}",
            target_reason=f"Esmola de {donor.display_name}",
        )
        now_ts = float(time.time())
        donor_doc["last_esmola_at"] = now_ts
        await self.cog.db._save_user_doc(self.guild_id, donor.id, donor_doc)
        self.fulfilled = True
        self.stop()
        result = discord.ui.LayoutView(timeout=None)
        result.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "\n".join(
                        [
                            "# 💸 Esmola entregue",
                            f"{donor.mention} deu {self.cog._chip_amount(self.amount)} para {recipient.mention}",
                            f"Saldo de {recipient.mention}: {self.cog._format_compact_chip_balance(self.guild_id, recipient.id)}",
                        ]
                    )
                ),
                accent_color=discord.Color.dark_green(),
            )
        )
        await interaction.response.edit_message(view=result)

    async def on_timeout(self):
        if self.fulfilled or self.message is None:
            return
        try:
            await self.message.edit(
                view=self.cog._make_v2_notice(
                    "💸 Esmola",
                    ["Ninguém ajudou essa pobre alma, o pedido de esmola expirou"],
                    ok=False,
                    accent_color=discord.Color.red(),
                )
            )
        except Exception:
            pass


class _RacePanelView(discord.ui.LayoutView):
    def __init__(self, cog: "GamesCog", *, guild_id: int, user_id: int):
        super().__init__(timeout=600.0)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.message: discord.Message | None = None
        self._build_layout()

    def _body_lines(self) -> list[str]:
        race_key = self.cog._get_user_race_key(self.guild_id, self.user_id)
        info = self.cog._get_race_info_by_key(race_key) or {}
        emoji = str(info.get("emoji") or "🍀")
        race_name = str(info.get("name") or "Sem raça")
        active = self.cog._is_user_race_active(self.guild_id, self.user_id)
        state_text = "Ativa" if active else "Desativada"
        lines = [f"# {emoji} {race_name}", f"**Estado:** {state_text}", "", "## Habilidades"]
        for effect in self.cog._get_race_effects(race_key):
            effect_emoji = str(effect.get("emoji") or emoji).strip()
            lines.extend([
                "",
                f"{effect_emoji} **{effect.get('title')}**",
                str(effect.get("desc") or ""),
            ])
        lines.extend(["", f"**Trocar raça:** {RACE_REROLL_COST} {self.cog._CHIP_EMOJI}"])
        return lines

    def _build_layout(self):
        self.clear_items()
        reroll = discord.ui.Button(label="Trocar raça", emoji="🎲", style=discord.ButtonStyle.secondary)
        reroll.callback = self._reroll
        row_children = [reroll]
        if self.cog._is_user_race_active(self.guild_id, self.user_id):
            toggle = discord.ui.Button(label="Desativar raça", style=discord.ButtonStyle.secondary)
            toggle.callback = self._toggle_race
        else:
            toggle = discord.ui.Button(label="Ativar raça", style=discord.ButtonStyle.success)
            toggle.callback = self._toggle_race
        row_children.append(toggle)
        row = discord.ui.ActionRow(*row_children)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._body_lines())),
                row,
                accent_color=discord.Color.green(),
            )
        )

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or int(interaction.guild.id) != self.guild_id or int(interaction.user.id) != self.user_id:
            await interaction.response.send_message(view=self.cog._make_v2_notice("🍀 Raça", ["Esse painel pertence a outra pessoa"], ok=False), ephemeral=True)
            return False
        return True

    async def _reroll(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        await self.cog._maybe_execute_due_chip_season_reset(self.guild_id)
        current = self.cog._get_user_race_key(self.guild_id, self.user_id)
        normal_chips = int(self.cog.db.get_user_chips(self.guild_id, self.user_id, default=CHIPS_INITIAL) or 0)
        if normal_chips < RACE_REROLL_COST:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "🍀 Saldo insuficiente",
                    [f"Trocar de raça custa **{RACE_REROLL_COST}** {self.cog._CHIP_EMOJI}"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        await self.cog._change_user_chips(self.guild_id, self.user_id, -RACE_REROLL_COST, mark_activity=True, reason="Troca de raça")
        await self.cog._roll_user_race(self.guild_id, self.user_id, exclude_current=bool(current))
        spinner_texts = ("Sorteando sua nova raça", "Sorteando sua nova raça..", "Sorteando sua nova raça...", "Definindo sua nova raça...")
        await interaction.response.edit_message(view=self.cog._make_race_spinner_view(spinner_texts[0]))
        target_message = interaction.message
        for text_line in spinner_texts[1:]:
            await asyncio.sleep(0.35)
            try:
                await target_message.edit(view=self.cog._make_race_spinner_view(text_line))
            except Exception:
                pass
        await asyncio.sleep(0.35)
        self._build_layout()
        self.message = target_message
        self.cog._remember_race_panel_message(self.guild_id, self.user_id, target_message)
        try:
            await target_message.edit(view=self)
        except Exception:
            pass

    async def _toggle_race(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        race_key = self.cog._get_user_race_key(self.guild_id, self.user_id)
        if not race_key:
            await interaction.response.send_message(view=self.cog._make_v2_notice("🍀 Raça", ["Você ainda não tem uma raça definida"], ok=False), ephemeral=True)
            return
        now_active = self.cog._is_user_race_active(self.guild_id, self.user_id)
        await self.cog._set_user_race_active(self.guild_id, self.user_id, not now_active)
        self._build_layout()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        self.stop()
        self.cog._forget_race_panel_message(self.guild_id, self.user_id, message_id=getattr(self.message, "id", None))
        if self.message is None:
            return
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass


class GamesCog(dcommands.Cog, GamesCore):
    def __init__(self, bot: dcommands.Bot, db):
        dcommands.Cog.__init__(self)
        GamesCore.__init__(self, bot, db)

    def _format_wait_compact(self, seconds: float) -> str:
        remaining = max(0, int(seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    def _make_race_reveal_view(self, guild_id: int, user_id: int, race_key: str) -> discord.ui.LayoutView:
        info = self._get_race_info_by_key(race_key) or {}
        emoji = str(info.get("emoji") or "🍀")
        race_name = str(info.get("name") or "Sem raça")
        lines = [f"# {emoji} Nova raça: {race_name}", "Sua raça foi definida", "", "## Habilidades"]
        for effect in self._get_race_effects(race_key):
            effect_emoji = str(effect.get("emoji") or emoji).strip()
            lines.extend([
                "",
                f"{effect_emoji} **{effect.get('title')}**",
                str(effect.get("desc") or ""),
            ])
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), accent_color=discord.Color.green()))
        return view

    def _make_race_spinner_view(self, text_line: str) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(["# 🍀 Raça", str(text_line)])),
                accent_color=discord.Color.green(),
            )
        )
        return view

    async def _start_mendigar_request(self, message: discord.Message, *, amount: int, target: discord.Member | None) -> bool:
        guild = message.guild
        if guild is None:
            return True
        await self._maybe_execute_due_chip_season_reset(guild.id)
        if amount <= 0:
            await message.channel.send(view=self._make_v2_notice("🥺 Esmola", ["Use um valor maior que zero"], ok=False))
            return True
        current_balance = int(self.db.get_user_chips(guild.id, message.author.id, default=CHIPS_INITIAL) or 0)
        if current_balance + amount > CHIPS_PAY_RECEIVER_MAX_BALANCE:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Esmola",
                    [f"Esse pedido deixaria você acima de **{CHIPS_PAY_RECEIVER_MAX_BALANCE} fichas normais**\nAjuste o valor e tente de novo"],
                    ok=False,
                )
            )
            return True

        limit, window_seconds = self._limited_action_config(guild.id, message.author.id, action="mendigar")
        action_state = self._limited_action_state(guild.id, message.author.id, storage_key="race_mendigar", limit=limit, window_seconds=window_seconds)
        remaining = float(action_state.get("remaining", 0.0) or 0.0)
        if int(action_state.get("available", 0) or 0) <= 0 and remaining > 0:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Esmola",
                    ["Você já pediu esmola demais por agora", f"Tente novamente em **{self._format_wait_compact(remaining)}**"],
                    ok=False,
                )
            )
            return True

        if target is not None:
            if target.bot:
                await message.channel.send(view=self._make_v2_notice("🥺 Esmola", ["Bots não entram nesse esquema de esmola"], ok=False))
                return True
            if target.id == message.author.id:
                await message.channel.send(view=self._make_v2_notice("🥺 Esmola", ["Pedir esmola para si mesmo já é sacanagem demais"], ok=False))
                return True

        view = _MendigarRequestView(
            self,
            guild_id=guild.id,
            author_id=message.author.id,
            author_mention=message.author.mention,
            amount=amount,
            target_id=getattr(target, "id", None),
            target_mention=getattr(target, "mention", None),
        )
        sent = await message.channel.send(view=view)
        view.message = sent
        return True

    async def _handle_mendigar_trigger(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip()
        if content.casefold().startswith("_"):
            return False
        if not re.match(r"^\s*mendigar\b", content, re.IGNORECASE):
            return False
        if message.guild is None:
            return True

        match = re.fullmatch(r"\s*mendigar\s+(\d+)(?:\s+<@!?(\d+)>)?\s*", content, re.IGNORECASE)
        if match is None:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Esmola",
                    [
                        "Use `mendigar 40` para pedir uma esmola geral",
                        "Use `mendigar 40 @usuário` para pedir esmola a alguém específico",
                    ],
                    ok=False,
                )
            )
            return True

        amount = int(match.group(1) or 0)
        target_id_raw = match.group(2)
        target: discord.Member | None = None
        if target_id_raw:
            target_id = int(target_id_raw)
            mentioned_members = [member for member in getattr(message, "mentions", []) if isinstance(member, discord.Member)]
            target = next((member for member in mentioned_members if int(member.id) == target_id), None)
            if target is None:
                target = message.guild.get_member(target_id)
            if target is None:
                await message.channel.send(view=self._make_v2_notice("🥺 Esmola", ["Não encontrei essa pessoa no servidor para pedir esmola"], ok=False))
                return True
        elif getattr(message, "mentions", None):
            await message.channel.send(view=self._make_v2_notice("🥺 Esmola", ["Use no formato `mendigar valor @usuário`"], ok=False))
            return True

        return await self._start_mendigar_request(message, amount=amount, target=target)

    async def _handle_race_trigger(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip().casefold()
        if content.startswith("_"):
            return False
        if content not in {"race", "raça"}:
            return False
        if message.guild is None:
            return True
        await self._maybe_execute_due_chip_season_reset(message.guild.id)
        race_key = self._get_user_race_key(message.guild.id, message.author.id)
        if not race_key:
            race_key = await self._roll_user_race(message.guild.id, message.author.id)
            reveal = self._make_race_reveal_view(message.guild.id, message.author.id, race_key)
            spinner = await message.channel.send(view=self._make_race_spinner_view("Sorteando sua raça"))
            for text_line in ("Sorteando sua raça..", "Sorteando sua raça...", "Definindo sua raça..."):
                await asyncio.sleep(0.35)
                try:
                    await spinner.edit(view=self._make_race_spinner_view(text_line))
                except Exception:
                    pass
            await asyncio.sleep(0.35)
            try:
                await spinner.edit(view=reveal)
            except Exception:
                await message.channel.send(view=reveal)
            return True
        await self._delete_previous_race_panel_message(message.guild.id, message.author.id, channel=message.channel)
        view = _RacePanelView(self, guild_id=message.guild.id, user_id=message.author.id)
        sent = await message.channel.send(view=view)
        view.message = sent
        self._remember_race_panel_message(message.guild.id, message.author.id, sent)
        return True

    async def _dispatch_prefix_trigger(
        self,
        ctx: dcommands.Context,
        *,
        handler_name: str,
        content: str,
        trigger_hint: str,
        mentions: list[discord.Member] | None = None,
        failure_title: str = "Games",
    ) -> bool:
        if not await self._ensure_games_command_entry(ctx, trigger_hint=trigger_hint):
            return False
        handler = getattr(self, handler_name, None)
        if not callable(handler):
            await ctx.reply(
                view=self._make_v2_notice(failure_title, ["Esse comando não está disponível agora"], ok=False),
                mention_author=False,
            )
            return False
        proxy = _CommandMessageProxy(ctx.message, content=content, mentions=mentions)
        try:
            handled = bool(await handler(proxy))
        except Exception:
            log.exception(
                "games: falha em comando prefixado handler=%s guild=%s user=%s",
                handler_name,
                getattr(ctx.guild, "id", 0),
                getattr(ctx.author, "id", 0),
            )
            await ctx.reply(
                view=self._make_v2_notice(failure_title, ["O comando falhou antes de concluir"], ok=False),
                mention_author=False,
            )
            return False
        if not handled:
            await ctx.reply(
                view=self._make_v2_notice(failure_title, ["Não foi possível executar agora"], ok=False),
                mention_author=False,
            )
        return handled

    @_guild_scoped()
    @app_commands.command(name="economia", description="Gerencia jogos, fichas e raças neste servidor")
    async def economia(self, interaction: discord.Interaction):
        await self._run_gincana_command(interaction)

    @economia.error
    async def economia_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_gincana_error(interaction, error)

    @dcommands.command(name="economia")
    async def economia_prefix(self, ctx: dcommands.Context):
        await self._run_gincana_prefix_command(ctx)

    @dcommands.command(name="ficha", aliases=["fichas"])
    async def ficha(self, ctx: dcommands.Context):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="ficha"):
            return
        await ctx.reply(view=self._make_chip_balance_view(ctx.author), mention_author=False)

    @dcommands.command(name="extrato")
    async def extrato_command(self, ctx: dcommands.Context):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="extrato"):
            return
        await ctx.reply(view=self._make_chip_history_view(ctx.author, limit=10), mention_author=False)

    @dcommands.command(name="daily", aliases=["bonus", "login"])
    async def daily(self, ctx: dcommands.Context):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="daily"):
            return
        await ctx.reply(
            view=await self._claim_daily_view(ctx.guild.id, ctx.author.id),
            mention_author=False,
        )

    @dcommands.command(name="recarga", aliases=["recarrega"])
    async def recarga(self, ctx: dcommands.Context):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="recarga"):
            return
        used, new_balance, note = await self._try_use_chip_recharge(ctx.guild.id, ctx.author.id)
        await ctx.reply(
            view=self._make_chip_recharge_view(ctx.guild.id, ctx.author.id, used, new_balance, note),
            mention_author=False,
        )

    @dcommands.command(name="rank", aliases=["leaderboard"])
    async def rank(self, ctx: dcommands.Context):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="rank"):
            return
        try:
            response = await self._chip_rank_cache.get_rank(ctx.guild, ctx.author)
            image = discord.File(io.BytesIO(response.image_bytes), filename="rank-fichas.png")
            await ctx.reply(
                file=image,
                view=self._make_chip_rank_view(response),
                mention_author=False,
            )
        except Exception:
            log.exception(
                "games-rank: falha ao montar imagem guild=%s user=%s",
                getattr(ctx.guild, "id", 0),
                getattr(ctx.author, "id", 0),
            )
            await ctx.reply(
                view=self._make_chip_rank_fallback_view(ctx.guild, ctx.author),
                mention_author=False,
            )

    @dcommands.command(name="race", aliases=["raça"])
    async def race_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_race_trigger",
            content="race",
            trigger_hint="race",
            failure_title="🍀 Raça",
        )

    @dcommands.command(name="pay")
    async def pay_command(
        self,
        ctx: dcommands.Context,
        target: discord.Member | None = None,
        amount: int | None = None,
    ):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="pay @usuário valor"):
            return
        if target is None or amount is None:
            await ctx.reply(
                view=self._make_v2_notice("💸 Pagamento", [f"Use `{ctx.clean_prefix}pay @usuário valor`"], ok=False),
                mention_author=False,
            )
            return
        proxy = _CommandMessageProxy(
            ctx.message,
            content=f"pay {target.mention} {int(amount)}",
            mentions=[target],
        )
        await self._handle_payment_message(proxy)

    @dcommands.command(name="roleta")
    async def roleta_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_roleta_trigger",
            content="roleta",
            trigger_hint="roleta",
            failure_title="🎰 Roleta",
        )

    @dcommands.command(name="carta", aliases=["cartas"])
    async def carta_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_carta_trigger",
            content="cartas",
            trigger_hint="cartas",
            failure_title="🎴 Cartas",
        )

    @dcommands.command(name="corrida")
    async def corrida_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_corrida_trigger",
            content="corrida",
            trigger_hint="corrida",
            failure_title="🏁 Corrida",
        )

    @dcommands.command(name="alvo")
    async def alvo_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_target_trigger",
            content="alvo",
            trigger_hint="alvo",
            failure_title="🎯 Alvo",
        )

    @dcommands.command(name="buckshot")
    async def buckshot_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_buckshot_trigger",
            content="buckshot",
            trigger_hint="buckshot",
            failure_title="🔫 Buckshot",
        )

    @dcommands.command(name="poker")
    async def poker_command(self, ctx: dcommands.Context, opponent: discord.Member | None = None):
        if opponent is None:
            if not await self._ensure_games_command_entry(ctx, trigger_hint="poker @usuário"):
                return
            await ctx.reply(
                view=self._make_v2_notice("🃏 Poker", [f"Use `{ctx.clean_prefix}poker @usuário`"], ok=False),
                mention_author=False,
            )
            return
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_poker_trigger",
            content=f"poker {opponent.mention}",
            mentions=[opponent],
            trigger_hint="poker @usuário",
            failure_title="🃏 Poker",
        )

    @dcommands.command(name="truco")
    async def truco_command(self, ctx: dcommands.Context, opponent: discord.Member | None = None):
        if opponent is None:
            if not await self._ensure_games_command_entry(ctx, trigger_hint="truco @usuário"):
                return
            await ctx.reply(
                view=self._make_v2_notice("🃏 Truco", [f"Use `{ctx.clean_prefix}truco @usuário`"], ok=False),
                mention_author=False,
            )
            return
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_truco_trigger",
            content=f"truco {opponent.mention}",
            mentions=[opponent],
            trigger_hint="truco @usuário",
            failure_title="🃏 Truco",
        )

    @dcommands.command(name="mendigar")
    async def mendigar_command(
        self,
        ctx: dcommands.Context,
        amount: int | None = None,
        target: discord.Member | None = None,
    ):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="mendigar valor"):
            return
        if amount is None:
            await ctx.reply(
                view=self._make_v2_notice(
                    "🥺 Esmola",
                    [
                        f"Use `{ctx.clean_prefix}mendigar 40`",
                        f"Ou `{ctx.clean_prefix}mendigar 40 @usuário`",
                    ],
                    ok=False,
                ),
                mention_author=False,
            )
            return
        proxy = _CommandMessageProxy(
            ctx.message,
            content=f"mendigar {int(amount)}" + (f" {target.mention}" if target else ""),
            mentions=([target] if target else []),
        )
        await self._start_mendigar_request(proxy, amount=int(amount), target=target)

    @dcommands.command(name="focus")
    async def focus_command(self, ctx: dcommands.Context, *, arguments: str = ""):
        content = "focus" + (f" {arguments.strip()}" if arguments.strip() else "")
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_focus_trigger",
            content=content,
            mentions=[m for m in ctx.message.mentions if isinstance(m, discord.Member)],
            trigger_hint="focus",
            failure_title="🎯 Focus",
        )

    @dcommands.command(name="pica")
    async def pica_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_role_toggle_trigger",
            content="pica",
            trigger_hint="pica",
            failure_title="Pica",
        )

    @dcommands.command(name="rola")
    async def rola_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_rola_toggle_trigger",
            content="rola",
            trigger_hint="rola",
            failure_title="Rola",
        )

    @dcommands.command(name="dj")
    async def dj_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_dj_toggle_trigger",
            content="dj",
            trigger_hint="dj",
            failure_title="DJ",
        )

    @dcommands.command(name=_CALL_COMMAND_NAME)
    async def call_trigger_command(self, ctx: dcommands.Context):
        await self._dispatch_prefix_trigger(
            ctx,
            handler_name="_handle_call_control_trigger",
            content=str(TRIGGER_WORD or _CALL_COMMAND_NAME),
            trigger_hint=str(TRIGGER_WORD or _CALL_COMMAND_NAME),
            failure_title="Call",
        )

    async def _run_robbery(self, channel: discord.abc.Messageable, guild: discord.Guild, author: discord.Member, target: discord.Member):
        if target.bot:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você tentou roubar um bot\nIsso foi longe demais"], ok=False))
            return True
        if target.id == author.id:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Tentar roubar a si mesmo já é demais"], ok=False))
            return True
        if int(self.db.get_user_chips(guild.id, author.id, default=CHIPS_INITIAL) or 0) < 0:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você já está devendo (coitado)\nQuite a dívida antes de tentar roubar alguém"], ok=False))
            return True
        adoc = self.db._get_user_doc(guild.id, author.id)
        now = time.time()
        rob_limit, rob_window = self._limited_action_config(guild.id, author.id, action="robbery")
        rob_state = self._limited_action_state(guild.id, author.id, storage_key="race_robbery", limit=rob_limit, window_seconds=rob_window)
        remaining = max(0, int(float(rob_state.get("remaining", 0.0) or 0.0)))
        if int(rob_state.get("available", 0) or 0) <= 0 and remaining > 0:
            wait = self._format_wait_compact(remaining)
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você já aprontou demais por agora", f"Tente de novo em **{wait}**"], ok=False))
            return True
        target_chips = int(self.db.get_user_chips(guild.id, target.id, default=CHIPS_INITIAL) or 0)
        target_bonus = int(self.db.get_user_bonus_chips(guild.id, target.id) or 0)
        if target_chips < 0:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário já está devendo (coitado)"], ok=False))
            return True
        if target_chips < 20:
            if target_bonus > 0 and target_chips <= 0:
                await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário só tem fichas bônus"], ok=False))
            else:
                await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário é muito **pobre** pra ser roubado"], ok=False))
            return True
        consumed, consumed_state = await self._consume_limited_action(
            guild.id,
            author.id,
            storage_key="race_robbery",
            limit=rob_limit,
            window_seconds=rob_window,
            legacy_field="last_robbery_at",
        )
        if not consumed:
            wait = self._format_wait_compact(max(1.0, float(rob_state.get("remaining", 0.0) or 1.0)))
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você já aprontou demais por agora", f"Tente de novo em **{wait}**"], ok=False))
            return True
        success = random.random() < 0.40
        if success:
            max_rob = 40 if self._race_is(guild.id, author.id, "preto") else 30
            amount = random.randint(5, min(max_rob, max(5, target_chips)))
            await self._change_user_chips(guild.id, target.id, -amount, mark_activity=True, reason=f"Roubado por {author.display_name}")
            await self._change_user_chips(guild.id, author.id, amount, mark_activity=True, reason=f"Roubo bem-sucedido em {target.display_name}")
            flavor = random.choice([
                f"Você roubou {self._chip_text(amount, kind='gain')} de {target.mention}",
                f"O golpe encaixou\nVocê levou {self._chip_text(amount, kind='gain')} de {target.mention}",
                f"Você passou a mão em {self._chip_text(amount, kind='gain')} de {target.mention}"
            ])
            effect_lines = []
            robbery_used_count = int(consumed_state.get("used", 0) or 0)
            if self._race_is(guild.id, author.id, "preto") and robbery_used_count > 1:
                marker = self._race_effect_message(guild.id, author.id, "mao_negra")
                if marker:
                    effect_lines.append(marker)
            if amount > 30:
                marker = self._race_effect_message(guild.id, author.id, "mao_grande")
                if marker:
                    effect_lines.append(marker)
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", [flavor, *effect_lines], ok=True, accent_color=discord.Color.dark_green()))
            return True
        penalty = 5 if self._race_is(guild.id, author.id, "preto") else 10
        robbery_used_count = int(consumed_state.get("used", 0) or 0)
        if self._coringa_avoids_robbery_penalty(guild.id, author.id):
            penalty = 0
        if penalty > 0:
            await self._change_user_chips(guild.id, author.id, -penalty, mark_activity=True, reason="Multa por roubo falho")
        lines = [
            f"Você tentou roubar {target.mention}, mas foi pego no flagra",
            (f"Você perdeu {self._chip_text(penalty, kind='loss')}" if penalty > 0 else "Você não perdeu fichas")
        ]
        marker = self._race_effect_message(guild.id, author.id, "mao_negra")
        if self._race_is(guild.id, author.id, "preto") and robbery_used_count > 1 and marker:
            lines.append(marker)
        marker = self._race_effect_message(guild.id, author.id, "trapaceiro")
        if penalty == 0 and marker:
            lines.append(marker)
        await channel.send(view=self._make_v2_notice("🕵️ Deu ruim", lines, ok=False, accent_color=discord.Color.red()))
        return True

    @dcommands.command(name="roubar", aliases=["rob"])
    async def roubar_command(self, ctx: dcommands.Context, target: discord.Member | None = None):
        if not await self._ensure_games_command_entry(ctx, trigger_hint="roubar @usuário"):
            return
        if target is None:
            await ctx.reply(view=self._make_v2_notice("🕵️ Roubo", [f"Use `{ctx.clean_prefix}roubar @usuário` para tentar a sorte"], ok=False), mention_author=False)
            return
        await self._run_robbery(ctx.channel, ctx.guild, ctx.author, target)

    @dcommands.Cog.listener()
    async def on_ready(self):
        try:
            self._chip_rank_cache.start()
        except Exception as e:
            print(f"[games] erro ao aquecer rank de fichas: {e!r}")
        if self._gincana_timed_effects_rehydrated:
            return
        self._gincana_timed_effects_rehydrated = True
        try:
            await self._rehydrate_gincana_timed_effects()
        except Exception as e:
            print(f"[games] erro ao restaurar efeitos temporários: {e!r}")

    @dcommands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            await self._handle_gincana_voice_state_update(member, before, after)
        except Exception as e:
            print(f"[games] erro no on_voice_state_update: {e!r}")


    @dcommands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        try:
            self._chip_rank_cache.member_changed(before, after)
        except Exception as e:
            print(f"[games] erro ao atualizar avatar/nome do rank: {e!r}")
        try:
            await self._handle_gincana_member_update(before, after)
        except Exception as e:
            print(f"[games] erro no on_member_update: {e!r}")

    @dcommands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self._chip_rank_cache.invalidate_member(member)

    @dcommands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        self._chip_rank_cache.invalidate_member(member)

    @dcommands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        self._chip_rank_cache.user_changed(before, after)

    @dcommands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        self._chip_rank_cache.guild_changed(before, after)

    @dcommands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        self._chip_rank_cache.invalidate(guild.id, delay=0.0)

    @dcommands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._chip_rank_cache.drop_guild(guild.id)

    async def cog_unload(self):
        await self._chip_rank_cache.close()

    @dcommands.Cog.listener()
    async def on_message(self, message: discord.Message):
        antibot_guard = getattr(self.bot, "antibot_should_block_message", None)
        if callable(antibot_guard) and bool(antibot_guard(message)):
            return
        try:
            await self._handle_gincana_message(message)
        except Exception as e:
            print(f"[games] erro no on_message: {e!r}")

    @dcommands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        try:
            await self._handle_payment_reaction_event(payload, added=True)
        except Exception as e:
            print(f"[games] erro no on_raw_reaction_add: {e!r}")

    @dcommands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        try:
            await self._handle_payment_reaction_event(payload, added=False)
        except Exception as e:
            print(f"[games] erro no on_raw_reaction_remove: {e!r}")


async def setup(bot: dcommands.Bot):
    await bot.add_cog(GamesCog(bot, bot.settings_db))
