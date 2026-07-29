import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

import discord

from ..constants import POKER_BUY_IN


Card = tuple[int, str, str]

_HAND_NAMES = {
    8: "Straight Flush",
    7: "Quadra",
    6: "Full House",
    5: "Flush",
    4: "Sequência",
    3: "Trinca",
    2: "Dois Pares",
    1: "Par",
    0: "Carta Alta",
}

_BET_SIZE = 10
_MAX_SWAP = 3
_INVITE_TIMEOUT_TEXT = "60s"


@dataclass
class PokerGame:
    guild_id: int
    channel_id: int
    host_id: int
    opponent_id: int
    status_message: discord.Message | None = None
    hands: dict[int, list[Card]] = field(default_factory=dict)
    original_hands: dict[int, list[Card]] = field(default_factory=dict)
    deck: list[Card] = field(default_factory=list)
    selected: dict[int, set[int]] = field(default_factory=dict)
    confirmed: dict[int, bool] = field(default_factory=dict)
    dm_messages: dict[int, discord.Message] = field(default_factory=dict)
    views: dict[int, "PokerSelectionView"] = field(default_factory=dict)
    finished: bool = False
    accepted: dict[int, bool] = field(default_factory=dict)
    phase: str = "invite"
    turn_id: int | None = None
    stacks: dict[int, int] = field(default_factory=dict)
    stack_normal: dict[int, int] = field(default_factory=dict)
    stack_bonus: dict[int, int] = field(default_factory=dict)
    round_bets: dict[int, int] = field(default_factory=dict)
    entry_spend: dict[int, dict[str, object]] = field(default_factory=dict)
    race_interactions: dict[int, discord.Interaction] = field(default_factory=dict)
    round_acted: set[int] = field(default_factory=set)
    pot: int = 0
    buy_in: int = POKER_BUY_IN
    action_log: list[str] = field(default_factory=list)
    exchange_counts: dict[int, int] = field(default_factory=dict)
    folded_by: int | None = None
    last_activity_at: float = field(default_factory=time.monotonic)
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def players(self) -> tuple[int, int]:
        return (self.host_id, self.opponent_id)

    def other_player(self, player_id: int) -> int:
        return self.opponent_id if player_id == self.host_id else self.host_id


class PokerSelectionView(discord.ui.View):
    def __init__(self, cog: "GincanaPokerMixin", game: PokerGame, player_id: int, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.game = game
        self.player_id = player_id
        self.position_buttons: list[discord.ui.Button] = []
        for idx in range(5):
            button = discord.ui.Button(label=str(idx + 1), style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._make_toggle_callback(idx)
            self.position_buttons.append(button)
            self.add_item(button)

        self.clear_button = discord.ui.Button(label="Limpar", style=discord.ButtonStyle.danger, row=1)
        self.clear_button.callback = self._clear_selection
        self.add_item(self.clear_button)

        self.confirm_button = discord.ui.Button(label="Confirmar troca", style=discord.ButtonStyle.success, row=1)
        self.confirm_button.callback = self._confirm_selection
        self.add_item(self.confirm_button)

        self.accept_button = discord.ui.Button(label="Aceitar duelo", style=discord.ButtonStyle.success, row=2)
        self.accept_button.callback = self._accept_duel
        self.add_item(self.accept_button)

        self.check_call_button = discord.ui.Button(label="Check", style=discord.ButtonStyle.primary, row=3)
        self.check_call_button.callback = self._check_call
        self.add_item(self.check_call_button)

        self.bet_raise_button = discord.ui.Button(label=f"Apostar +{_BET_SIZE}", style=discord.ButtonStyle.secondary, row=3)
        self.bet_raise_button.callback = self._bet_raise
        self.add_item(self.bet_raise_button)

        self.fold_button = discord.ui.Button(label="Desistir", style=discord.ButtonStyle.danger, row=3)
        self.fold_button.callback = self._fold
        self.add_item(self.fold_button)
        self.refresh_buttons()

    def _make_toggle_callback(self, index: int):
        async def _callback(interaction: discord.Interaction):
            self.game.race_interactions[self.player_id] = interaction
            async with self.game.action_lock:
                await self.cog._handle_poker_toggle(interaction, self.game, self.player_id, index)
        return _callback

    async def _clear_selection(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_clear(interaction, self.game, self.player_id)

    async def _confirm_selection(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_confirm(interaction, self.game, self.player_id)

    async def _accept_duel(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_accept(interaction, self.game, self.player_id)

    async def _check_call(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_check_call(interaction, self.game, self.player_id)

    async def _bet_raise(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_bet_raise(interaction, self.game, self.player_id)

    async def _fold(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        async with self.game.action_lock:
            await self.cog._handle_poker_fold(interaction, self.game, self.player_id)

    def refresh_buttons(self):
        selected = self.game.selected.get(self.player_id, set())
        confirmed = self.game.confirmed.get(self.player_id, False)
        phase = self.game.phase
        accepted = self.game.accepted.get(self.player_id, False)
        current_bet = max(self.game.round_bets.values(), default=0)
        own_bet = self.game.round_bets.get(self.player_id, 0)
        turn = self.game.turn_id == self.player_id
        can_bet = self.game.stacks.get(self.player_id, 0) > 0

        for idx, button in enumerate(self.position_buttons):
            button.style = discord.ButtonStyle.primary if idx in selected else discord.ButtonStyle.secondary
            button.disabled = phase != "draw_select" or confirmed
        self.clear_button.disabled = phase != "draw_select" or confirmed or not selected
        self.confirm_button.disabled = phase != "draw_select" or confirmed
        self.confirm_button.label = "Troca confirmada" if confirmed else "Confirmar troca"

        self.accept_button.disabled = phase != "invite" or accepted
        self.accept_button.label = "Duelo aceito" if accepted else "Aceitar duelo"

        betting_phase = phase in {"pre_draw_bet", "post_draw_bet"}
        self.check_call_button.disabled = not betting_phase or not turn
        self.bet_raise_button.disabled = not betting_phase or not turn or not can_bet
        self.fold_button.disabled = not betting_phase or not turn

        if betting_phase:
            if current_bet > own_bet:
                self.check_call_button.label = f"Pagar {current_bet - own_bet}"
                self.bet_raise_button.label = f"Aumentar +{_BET_SIZE}"
            else:
                self.check_call_button.label = "Check"
                self.bet_raise_button.label = f"Apostar {_BET_SIZE}"
        else:
            self.check_call_button.label = "Check"
            self.bet_raise_button.label = f"Apostar {_BET_SIZE}"

    async def on_timeout(self):
        try:
            async with self.game.action_lock:
                if self.game.finished:
                    return
                if self.game.phase == "invite":
                    await self.cog._cancel_poker_game(self.game, reason="timeout", notice="A partida de poker expirou porque o convite não foi aceito a tempo.")
                    return
                await self.cog._award_fold_win(
                    self.game,
                    self.game.other_player(self.player_id),
                    self.player_id,
                    reason="abandono",
                )
        except Exception:
            pass


class GincanaPokerMixin:

    def _touch_poker_game(self, game: PokerGame) -> float:
        now = time.monotonic()
        game.last_activity_at = now
        return now

    def _poker_game_idle_for(self, game: PokerGame) -> float:
        last_activity = float(getattr(game, "last_activity_at", 0.0) or 0.0)
        if last_activity <= 0:
            return 0.0
        return max(0.0, time.monotonic() - last_activity)

    def _cleanup_broken_poker_state(self, guild_id: int):
        game = self._poker_games.get(guild_id)
        if game is None:
            return
        if getattr(game, 'finished', False):
            self._poker_games.pop(guild_id, None)
            return
        if getattr(game, '_stale_cleanup_started', False):
            return
        invite_idle_limit = 90.0
        if str(getattr(game, 'phase', '') or '') == 'invite' and self._poker_game_idle_for(game) > invite_idle_limit:
            game._stale_cleanup_started = True
            asyncio.create_task(self._cancel_poker_game(game, reason='stale_invite', notice='A partida anterior de poker expirou por inatividade e foi encerrada automaticamente.'))

    def _poker_project_stack_after_buy_in(self, guild_id: int, user_id: int, buy_in: int) -> tuple[int, int, int]:
        chips = int(self.db.get_user_chips(guild_id, user_id, default=100) or 0)
        bonus = int(self._get_user_bonus_chips(guild_id, user_id) or 0)
        spend = max(0, int(buy_in))
        use_bonus = min(bonus, spend)
        remaining = spend - use_bonus
        stack_bonus = bonus - use_bonus
        stack_normal = chips - remaining
        return stack_normal, stack_bonus, stack_normal + stack_bonus

    def _poker_stack_text(self, game: PokerGame, player_id: int) -> str:
        normal = int(game.stack_normal.get(player_id, 0) or 0)
        bonus = int(game.stack_bonus.get(player_id, 0) or 0)
        if bonus > 0:
            return f"**{normal}** {self._CHIP_EMOJI} • **{bonus}** {self._CHIP_BONUS_EMOJI}"
        return f"**{normal}** {self._CHIP_EMOJI}"

    def _poker_total_stack(self, game: PokerGame, player_id: int) -> int:
        return int(game.stack_normal.get(player_id, 0) or 0) + int(game.stack_bonus.get(player_id, 0) or 0)

    def _consume_poker_stack(self, game: PokerGame, player_id: int, amount: int) -> bool:
        spend = max(0, int(amount))
        total = self._poker_total_stack(game, player_id)
        if spend > total:
            return False
        bonus = int(game.stack_bonus.get(player_id, 0) or 0)
        normal = int(game.stack_normal.get(player_id, 0) or 0)
        use_bonus = min(bonus, spend)
        remaining = spend - use_bonus
        game.stack_bonus[player_id] = bonus - use_bonus
        game.stack_normal[player_id] = normal - remaining
        spent = dict(game.entry_spend.get(player_id) or {})
        spent["bonus"] = int(spent.get("bonus", 0) or 0) + use_bonus
        spent["chips"] = int(spent.get("chips", 0) or 0) + remaining
        game.entry_spend[player_id] = spent
        game.stacks[player_id] = self._poker_total_stack(game, player_id)
        return True

    def _add_poker_stack_normal(self, game: PokerGame, player_id: int, amount: int):
        game.stack_normal[player_id] = int(game.stack_normal.get(player_id, 0) or 0) + max(0, int(amount))
        game.stacks[player_id] = self._poker_total_stack(game, player_id)

    async def _persist_poker_player_stack(self, guild_id: int, user_id: int, *, normal: int, bonus: int, reason: str = "Poker"):
        new_normal = int(normal)
        new_bonus = int(bonus)
        current_normal = int(self.db.get_user_chips(guild_id, user_id, default=100) or 0)
        current_bonus = int(self._get_user_bonus_chips(guild_id, user_id) or 0)
        delta_normal = new_normal - current_normal
        delta_bonus = new_bonus - current_bonus
        await self.db.set_user_chips(guild_id, user_id, new_normal)
        await self.db.set_user_bonus_chips(guild_id, user_id, new_bonus)
        await self._mark_chip_activity(guild_id, user_id)
        if delta_normal != 0:
            try:
                await self.db.append_chip_history(guild_id, user_id, delta=delta_normal, kind="chips", reason=reason)
            except Exception:
                pass
        if delta_bonus != 0:
            try:
                await self.db.append_chip_history(guild_id, user_id, delta=delta_bonus, kind="bonus", reason=reason)
            except Exception:
                pass

    def _poker_entry_block_note(self, guild_id: int, user_id: int, buy_in: int) -> str | None:
        chips = int(self.db.get_user_chips(guild_id, user_id, default=100) or 0)
        bonus = int(self._get_user_bonus_chips(guild_id, user_id) or 0)
        total = chips + bonus
        if total < int(buy_in):
            return (
                f"O poker precisa de {self._chip_amount(buy_in)} disponíveis sem aumentar dívida. "
                f"Seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}."
            )
        return None
    def _create_poker_deck(self) -> list[Card]:
        suits = ["♠", "♥", "♦", "♣"]
        rank_labels = {11: "J", 12: "Q", 13: "K", 14: "A"}
        deck: list[Card] = []
        for suit in suits:
            for value in range(2, 15):
                deck.append((value, rank_labels.get(value, str(value)), suit))
        random.shuffle(deck)
        return deck

    def _deal_poker_hand(self, deck: list[Card], count: int = 5) -> list[Card]:
        hand: list[Card] = []
        for _ in range(count):
            hand.append(deck.pop())
        return hand

    def _format_card(self, card: Card) -> str:
        return f"{card[1]}{card[2]}"

    def _format_hand(self, hand: list[Card]) -> str:
        return "  ".join(f"{idx + 1}: {self._format_card(card)}" for idx, card in enumerate(hand))

    def _evaluate_poker_hand(self, hand: list[Card]) -> tuple[int, list[int]]:
        values = sorted((card[0] for card in hand), reverse=True)
        suits = [card[2] for card in hand]
        counts: dict[int, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)

        unique_values = sorted(set(values))
        straight = False
        straight_high = max(values)
        if len(unique_values) == 5:
            if max(unique_values) - min(unique_values) == 4:
                straight = True
                straight_high = max(unique_values)
            elif unique_values == [2, 3, 4, 5, 14]:
                straight = True
                straight_high = 5

        flush = len(set(suits)) == 1

        if straight and flush:
            return (8, [straight_high])
        if ordered[0][1] == 4:
            four = ordered[0][0]
            kicker = ordered[1][0]
            return (7, [four, kicker])
        if ordered[0][1] == 3 and ordered[1][1] == 2:
            return (6, [ordered[0][0], ordered[1][0]])
        if flush:
            return (5, values)
        if straight:
            return (4, [straight_high])
        if ordered[0][1] == 3:
            trips = ordered[0][0]
            kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
            return (3, [trips, *kickers])
        if ordered[0][1] == 2 and ordered[1][1] == 2:
            pairs = sorted((value for value, count in counts.items() if count == 2), reverse=True)
            kicker = max(value for value, count in counts.items() if count == 1)
            return (2, [*pairs, kicker])
        if ordered[0][1] == 2:
            pair = ordered[0][0]
            kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
            return (1, [pair, *kickers])
        return (0, values)

    def _poker_hand_name(self, hand: list[Card]) -> str:
        score, _ = self._evaluate_poker_hand(hand)
        return _HAND_NAMES[score]

    def _poker_hand_score(self, hand: list[Card]) -> int:
        score, _ = self._evaluate_poker_hand(hand)
        return score

    def _poker_hand_display(self, hand: list[Card]) -> str:
        score = self._poker_hand_score(hand)
        base = self._poker_hand_name(hand)
        if score >= 6:
            return f"{self._EFFECT_EMOJI} {base}"
        if score >= 4:
            return f"🔥 {base}"
        if score >= 2:
            return f"⭐ {base}"
        return base

    async def _record_poker_round(self, guild_id: int, *player_ids: int | None):
        seen = set()
        for pid in player_ids:
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            await self.db.add_user_game_stat(guild_id, pid, "poker_rounds", 1)
            await self._record_game_played(guild_id, pid, weekly_points=6)

    async def _record_poker_result(self, guild_id: int, winner_id: int | None, loser_id: int | None = None):
        await self._record_poker_round(guild_id, winner_id, loser_id)
        if winner_id is not None:
            await self.db.add_user_game_stat(guild_id, winner_id, "poker_wins", 1)
            await self._grant_weekly_points(guild_id, winner_id, 12)
        if loser_id is not None:
            await self.db.add_user_game_stat(guild_id, loser_id, "poker_losses", 1)

    def _compare_poker_hands(self, hand_a: list[Card], hand_b: list[Card]) -> int:
        eval_a = self._evaluate_poker_hand(hand_a)
        eval_b = self._evaluate_poker_hand(hand_b)
        if eval_a > eval_b:
            return 1
        if eval_b > eval_a:
            return -1
        return 0

    def _make_poker_status_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        embed = self._make_embed(title, description, ok=ok)
        if ok:
            embed.color = discord.Color.blurple()
        return embed

    def _make_poker_dm_embed(self, member: discord.Member, game: PokerGame, selected: set[int], confirmed: bool) -> discord.Embed:
        hand = game.hands[member.id]
        selected_text = "Nenhuma carta marcada." if not selected else "Selecionadas: " + ", ".join(str(i + 1) for i in sorted(selected))
        phase_names = {
            "invite": "Convite",
            "pre_draw_bet": "Apostas antes da troca",
            "draw_select": "Troca de cartas",
            "post_draw_bet": "Apostas finais",
            "finished": "Finalizado",
        }
        phase_text = phase_names.get(game.phase, game.phase)
        current_bet = max(game.round_bets.values(), default=0)
        own_bet = game.round_bets.get(member.id, 0)
        to_call = max(0, current_bet - own_bet)
        turn_text = "É a sua vez." if game.turn_id == member.id else "Aguarde a jogada do adversário."

        if game.phase == "invite":
            status_text = "Aceite o duelo para iniciar a rodada."
        elif game.phase == "draw_select":
            status_text = "Marque até 3 cartas e confirme a troca." if not confirmed else "Troca confirmada. Aguardando o adversário."
        elif game.phase in {"pre_draw_bet", "post_draw_bet"}:
            if to_call > 0:
                status_text = f"Você precisa pagar {self._chip_amount(to_call)}, aumentar ou desistir. {turn_text}"
            else:
                status_text = f"Você pode dar check, apostar {self._chip_amount(_BET_SIZE)} ou desistir. {turn_text}"
        else:
            status_text = "Rodada encerrada."

        embed = discord.Embed(
            title="🃏 Sua mão no Poker",
            description=f"**{member.display_name}**\n{self._format_hand(hand)}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Fase", value=phase_text, inline=True)
        embed.add_field(name="Seu stack", value=self._poker_stack_text(game, member.id), inline=True)
        embed.add_field(name="Pote", value=f"{self._CHIP_EMOJI} **{game.pot}**", inline=True)
        if game.phase in {"pre_draw_bet", "post_draw_bet"}:
            embed.add_field(name="Sua aposta", value=f"{self._CHIP_LOSS_EMOJI} **{own_bet}**", inline=True)
            embed.add_field(name="Maior aposta", value=f"{self._CHIP_EMOJI} **{current_bet}**", inline=True)
            embed.add_field(name="Para pagar", value=f"{self._CHIP_LOSS_EMOJI} **{to_call}**", inline=True)
        if game.phase == "draw_select":
            embed.add_field(name="Seleção", value=selected_text, inline=False)
        embed.add_field(name="Status", value=status_text, inline=False)
        return embed

    def _make_poker_dm_final_embed(self, player: discord.Member, player_hand: list[Card], opponent: discord.Member, opponent_hand: list[Card], result_text: str, game: PokerGame) -> discord.Embed:
        embed = discord.Embed(title="🃏 Resultado do Poker", description=result_text, color=discord.Color.green())
        embed.add_field(
            name=f"{player.display_name} — {self._poker_hand_display(player_hand)}",
            value=self._format_hand(player_hand),
            inline=False,
        )
        embed.add_field(
            name=f"{opponent.display_name} — {self._poker_hand_display(opponent_hand)}",
            value=self._format_hand(opponent_hand),
            inline=False,
        )
        embed.add_field(name="Pote final", value=f"{self._CHIP_EMOJI} **{game.pot}**", inline=True)
        embed.add_field(name="Seu stack", value=self._poker_stack_text(game, player.id), inline=True)
        embed.add_field(name="Stack rival", value=self._poker_stack_text(game, opponent.id), inline=True)
        return embed

    def _make_poker_result_embed(
        self,
        player_a: discord.Member,
        hand_a: list[Card],
        player_b: discord.Member,
        hand_b: list[Card],
        outcome: int,
        game: PokerGame,
        *,
        race_notices=(),
    ) -> discord.Embed:
        if outcome > 0:
            title = "🏆 Vitória no Poker"
            description = f"**Vencedor:** {player_a.mention}\n**Pote:** {self._chip_text(game.pot, kind='gain')}\n**Adversário:** {player_b.mention}"
        elif outcome < 0:
            title = "🏆 Vitória no Poker"
            description = f"**Vencedor:** {player_b.mention}\n**Pote:** {self._chip_text(game.pot, kind='gain')}\n**Adversário:** {player_a.mention}"
        else:
            title = "🤝 Empate no Poker"
            description = f"{player_a.mention} e {player_b.mention} dividiram o pote.\n**Pote total:** {self._chip_amount(game.pot)}"

        embed = self._make_embed(title, description, ok=True)
        embed.add_field(name=f"{player_a.display_name} — {self._poker_hand_display(hand_a)}", value=self._format_hand(hand_a), inline=False)
        embed.add_field(name=f"{player_b.display_name} — {self._poker_hand_display(hand_b)}", value=self._format_hand(hand_b), inline=False)
        embed.add_field(name=f"Stack final — {player_a.display_name}", value=self._poker_stack_text(game, player_a.id), inline=True)
        embed.add_field(name=f"Stack final — {player_b.display_name}", value=self._poker_stack_text(game, player_b.id), inline=True)
        embed.add_field(name="Trocas reveladas", value=f"{player_a.display_name}: **{game.exchange_counts.get(player_a.id, 0)}**\n{player_b.display_name}: **{game.exchange_counts.get(player_b.id, 0)}**", inline=True)
        clean_notices = self._clean_race_notices(race_notices)
        if clean_notices:
            embed.add_field(name="Habilidade", value="\n".join(clean_notices), inline=False)
        return embed

    async def _disable_poker_views(self, game: PokerGame):
        for view in game.views.values():
            for child in view.children:
                child.disabled = True
            try:
                if game.dm_messages.get(view.player_id) is not None:
                    await game.dm_messages[view.player_id].edit(view=view)
            except Exception:
                pass
            try:
                view.stop()
            except Exception:
                pass

    async def _cancel_poker_game(self, game: PokerGame, *, reason: str, notice: str):
        self._touch_poker_game(game)
        if game.finished:
            return
        game.finished = True
        self._poker_games.pop(game.guild_id, None)
        for player_id in game.players:
            normal = int(game.stack_normal.get(player_id, 0) or 0)
            bonus = int(game.stack_bonus.get(player_id, 0) or 0)
            if game.phase == "invite":
                stack_normal, stack_bonus, _total = self._poker_project_stack_after_buy_in(game.guild_id, player_id, game.buy_in)
                normal = stack_normal + game.buy_in
                bonus = stack_bonus
            await self._persist_poker_player_stack(game.guild_id, player_id, normal=normal, bonus=bonus, reason="Devolução do poker")
        await self._disable_poker_views(game)
        if game.status_message is not None:
            try:
                await game.status_message.edit(embed=self._make_poker_status_embed("🃏 Partida cancelada", notice, ok=False), view=None)
            except Exception:
                pass
        for dm_message in list(game.dm_messages.values()):
            try:
                await dm_message.edit(embed=self._make_poker_status_embed("🃏 Partida cancelada", notice, ok=False), view=None)
            except Exception:
                pass

    def _reset_betting_round(self, game: PokerGame, *, turn_id: int):
        game.round_bets = {pid: 0 for pid in game.players}
        game.round_acted = set()
        game.turn_id = turn_id

    async def _update_all_poker_dms(self, game: PokerGame):
        for player_id in game.players:
            await self._update_poker_dm(game, player_id)

    async def _advance_poker_phase(self, game: PokerGame):
        if game.phase == "pre_draw_bet":
            game.phase = "draw_select"
            game.selected = {pid: set() for pid in game.players}
            game.confirmed = {pid: False for pid in game.players}
            game.action_log.append("As apostas iniciais terminaram. Hora de trocar cartas em segredo.")
            await self._update_poker_status(game)
            await self._update_all_poker_dms(game)
            return
        if game.phase == "post_draw_bet":
            await asyncio.sleep(0.8)
            await self._finish_poker_game(game)

    async def _finish_betting_round_if_ready(self, game: PokerGame):
        if game.finished:
            return
        if len(game.round_acted) < 2:
            return
        current_bet = max(game.round_bets.values(), default=0)
        if all(game.round_bets.get(pid, 0) == current_bet for pid in game.players):
            await self._advance_poker_phase(game)
        else:
            await self._update_poker_status(game)
            await self._update_all_poker_dms(game)

    async def _award_fold_win(self, game: PokerGame, winner_id: int, loser_id: int, *, reason: str = "desistência"):
        self._touch_poker_game(game)
        if game.finished:
            return
        game.finished = True
        self._poker_games.pop(game.guild_id, None)
        game.folded_by = loser_id
        self._add_poker_stack_normal(game, winner_id, game.pot)
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        winner = guild.get_member(winner_id)
        loser = guild.get_member(loser_id)
        if winner is None or loser is None:
            return
        await self._persist_poker_player_stack(game.guild_id, winner_id, normal=game.stack_normal.get(winner_id, 0), bonus=game.stack_bonus.get(winner_id, 0), reason="Vitória no poker")
        await self._persist_poker_player_stack(game.guild_id, loser_id, normal=game.stack_normal.get(loser_id, 0), bonus=game.stack_bonus.get(loser_id, 0), reason="Derrota no poker")
        await self._record_poker_result(game.guild_id, winner_id, loser_id)
        first_game_user_ids = await self._unlock_first_game_for_users(game.guild_id, game.players)
        await self._disable_poker_views(game)
        if game.status_message is not None:
            try:
                await game.status_message.edit(
                    embed=self._make_poker_status_embed(
                        "🃏 Vitória por abandono" if reason == "abandono" else "🃏 Vitória por desistência",
                        (f"{loser.mention} ficou inativo e perdeu a rodada. {winner.mention} levou o pote de {self._chip_amount(game.pot)}." if reason == "abandono" else f"{loser.mention} desistiu. {winner.mention} levou o pote de {self._chip_amount(game.pot)}."),
                        ok=True,
                    ),
                    view=None,
                )
            except Exception:
                pass
        for player_id, dm_message in list(game.dm_messages.items()):
            result = ("Você venceu porque o rival abandonou a rodada." if reason == "abandono" else "Você venceu por desistência do rival.") if player_id == winner_id else ("Você ficou inativo e perdeu a rodada." if reason == "abandono" else "Você desistiu da rodada.")
            try:
                await dm_message.edit(
                    embed=self._make_poker_status_embed(
                        "🃏 Rodada encerrada",
                        f"{result}\nPote: {self._chip_amount(game.pot)}\nSeu stack: {self._chip_amount(game.stacks.get(player_id, 0))}",
                        ok=player_id == winner_id,
                    ),
                    view=None,
                )
            except Exception:
                pass
        achievement_channel = getattr(game.status_message, "channel", None) or guild.get_channel(game.channel_id)
        await self._send_first_game_notices(achievement_channel, game.guild_id, first_game_user_ids)

    async def _finish_poker_game(self, game: PokerGame):
        self._touch_poker_game(game)
        if game.finished:
            return
        game.finished = True
        self._poker_games.pop(game.guild_id, None)

        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        player_a = guild.get_member(game.host_id)
        player_b = guild.get_member(game.opponent_id)
        if player_a is None or player_b is None:
            return

        outcome = self._compare_poker_hands(game.hands[player_a.id], game.hands[player_b.id])
        if outcome > 0:
            self._add_poker_stack_normal(game, player_a.id, game.pot)
        elif outcome < 0:
            self._add_poker_stack_normal(game, player_b.id, game.pot)
        else:
            split_left = game.pot // 2
            split_right = game.pot - split_left
            self._add_poker_stack_normal(game, player_a.id, split_left)
            self._add_poker_stack_normal(game, player_b.id, split_right)
        if outcome > 0:
            reason_a = "Vitória no poker"
            reason_b = "Derrota no poker"
        elif outcome < 0:
            reason_a = "Derrota no poker"
            reason_b = "Vitória no poker"
        else:
            reason_a = "Empate no poker"
            reason_b = "Empate no poker"
        await self._persist_poker_player_stack(game.guild_id, player_a.id, normal=game.stack_normal.get(player_a.id, 0), bonus=game.stack_bonus.get(player_a.id, 0), reason=reason_a)
        await self._persist_poker_player_stack(game.guild_id, player_b.id, normal=game.stack_normal.get(player_b.id, 0), bonus=game.stack_bonus.get(player_b.id, 0), reason=reason_b)
        if outcome > 0:
            await self._record_poker_result(game.guild_id, player_a.id, player_b.id)
        elif outcome < 0:
            await self._record_poker_result(game.guild_id, player_b.id, player_a.id)
        else:
            await self._record_poker_round(game.guild_id, player_a.id, player_b.id)
        first_game_user_ids = await self._unlock_first_game_for_users(game.guild_id, game.players)

        payout_map = {
            player_a.id: game.pot if outcome > 0 else (game.pot // 2 if outcome == 0 else 0),
            player_b.id: game.pot if outcome < 0 else (game.pot - game.pot // 2 if outcome == 0 else 0),
        }
        won_map = {
            player_a.id: True if outcome > 0 else (False if outcome < 0 else None),
            player_b.id: True if outcome < 0 else (False if outcome > 0 else None),
        }
        public_race_notices: list[str] = []
        for player_id in game.players:
            opponent_id = game.other_player(player_id)
            notes = await self._apply_new_race_result(
                game.guild_id,
                player_id,
                won=won_map[player_id],
                entry_spend=game.entry_spend.get(player_id) or {"chips": game.buy_in, "bonus": 0},
                payout=int(payout_map[player_id]),
                opponent_ids=[opponent_id],
                valid=True,
                allow_hunt=True,
            )
            await self._route_lobby_race_notices(
                game.race_interactions.get(player_id),
                game.guild_id,
                player_id,
                game.host_id,
                notes,
                public_race_notices,
            )
            game.stack_normal[player_id] = int(self.db.get_user_chips(game.guild_id, player_id, default=100) or 0)
            game.stack_bonus[player_id] = int(self._get_user_bonus_chips(game.guild_id, player_id) or 0)
            game.stacks[player_id] = self._poker_total_stack(game, player_id)
        await self._disable_poker_views(game)

        public_result_delivered = False
        if game.status_message is not None:
            try:
                await game.status_message.edit(
                    embed=self._make_poker_result_embed(
                        player_a,
                        game.hands[player_a.id],
                        player_b,
                        game.hands[player_b.id],
                        outcome,
                        game,
                        race_notices=public_race_notices,
                    ),
                    view=None,
                )
                public_result_delivered = True
            except Exception:
                pass
        if public_race_notices and not public_result_delivered:
            self._queue_private_race_notices(game.guild_id, game.host_id, public_race_notices)

        if outcome > 0:
            text_a = "Você venceu a rodada."
            text_b = "Você perdeu a rodada."
        elif outcome < 0:
            text_a = "Você perdeu a rodada."
            text_b = "Você venceu a rodada."
        else:
            text_a = text_b = "A rodada terminou empatada."

        player_map = {player_a.id: player_a, player_b.id: player_b}
        result_texts = {player_a.id: text_a, player_b.id: text_b}
        for player_id, dm_message in list(game.dm_messages.items()):
            opponent_id = game.other_player(player_id)
            try:
                await dm_message.edit(
                    embed=self._make_poker_dm_final_embed(
                        player_map[player_id],
                        game.hands[player_id],
                        player_map[opponent_id],
                        game.hands[opponent_id],
                        result_texts[player_id],
                        game,
                    ),
                    view=None,
                )
            except Exception:
                pass
        achievement_channel = getattr(game.status_message, "channel", None) or guild.get_channel(game.channel_id)
        await self._send_first_game_notices(achievement_channel, game.guild_id, first_game_user_ids)

    async def _update_poker_dm(self, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        member = guild.get_member(player_id)
        dm_message = game.dm_messages.get(player_id)
        view = game.views.get(player_id)
        if member is None or dm_message is None or view is None:
            return
        view.refresh_buttons()
        try:
            await dm_message.edit(
                content=None,
                embed=self._make_poker_dm_embed(member, game, game.selected.get(player_id, set()), game.confirmed.get(player_id, False)),
                view=view,
            )
        except Exception:
            pass

    async def _update_poker_status(self, game: PokerGame):
        self._touch_poker_game(game)
        if game.status_message is None or game.finished:
            return
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        host = guild.get_member(game.host_id)
        opponent = guild.get_member(game.opponent_id)
        if host is None or opponent is None:
            return

        if game.phase == "invite":
            accepted_host = "✅" if game.accepted.get(game.host_id, False) else "⌛"
            accepted_opponent = "✅" if game.accepted.get(game.opponent_id, False) else "⌛"
            description = (
                f"Duelo entre {host.mention} e {opponent.mention}.\n"
                f"Aceites: {host.display_name} {accepted_host} | {opponent.display_name} {accepted_opponent}\n\n"
                "As DMs foram enviadas. Quando os dois aceitarem, a rodada começa."
            )
        elif game.phase in {"pre_draw_bet", "post_draw_bet"}:
            current = guild.get_member(game.turn_id) if game.turn_id else None
            stage_name = "Apostas antes da troca" if game.phase == "pre_draw_bet" else "Apostas finais"
            pending = current.mention if current else "Ninguém"
            log_text = "\n".join(f"• {entry}" for entry in game.action_log[-5:]) or "Nenhuma ação ainda."
            description = (
                f"{stage_name}\n"
                f"Pote: {self._chip_amount(game.pot)}\n"
                f"{host.display_name}: {self._CHIP_EMOJI} **{game.stacks.get(host.id, 0)}**\n"
                f"{opponent.display_name}: {self._CHIP_EMOJI} **{game.stacks.get(opponent.id, 0)}**\n"
                f"Vez de: {pending}\n\n"
                f"Ações recentes:\n{log_text}"
            )
        elif game.phase == "draw_select":
            host_status = "✅ pronto" if game.confirmed.get(game.host_id, False) else "⌛ escolhendo"
            opp_status = "✅ pronto" if game.confirmed.get(game.opponent_id, False) else "⌛ escolhendo"
            description = (
                f"Troca de cartas em segredo.\n"
                f"Pote: {self._chip_amount(game.pot)}\n"
                f"{host.display_name}: {host_status}\n"
                f"{opponent.display_name}: {opp_status}\n\n"
                "As mãos continuam privadas. A quantidade trocada por cada jogador será mostrada no resultado final."
            )
        else:
            description = "Rodada encerrada."
        try:
            await game.status_message.edit(embed=self._make_poker_status_embed("🃏 Poker em andamento", description, ok=True), view=None)
        except Exception:
            pass

    async def _handle_poker_toggle(self, interaction: discord.Interaction, game: PokerGame, player_id: int, index: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.phase != "draw_select" or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        selected = game.selected.setdefault(player_id, set())
        if index in selected:
            selected.remove(index)
        else:
            if len(selected) >= _MAX_SWAP:
                await interaction.response.send_message(f"Você pode trocar no máximo {_MAX_SWAP} cartas.", ephemeral=True)
                return
            selected.add(index)
        await self._update_poker_dm(game, player_id)
        await interaction.response.defer()

    async def _handle_poker_clear(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.phase != "draw_select" or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        game.selected[player_id] = set()
        await self._update_poker_dm(game, player_id)
        await interaction.response.defer()

    async def _handle_poker_confirm(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.phase != "draw_select" or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        game.confirmed[player_id] = True
        game.exchange_counts[player_id] = len(game.selected.get(player_id, set()))
        await self._update_poker_dm(game, player_id)
        await self._update_poker_status(game)
        await interaction.response.defer()
        if all(game.confirmed.get(pid, False) for pid in game.players):
            for pid in game.players:
                selected_positions = sorted(game.selected.get(pid, set()))
                hand = game.hands[pid]
                game.original_hands[pid] = list(hand)
                for idx in selected_positions:
                    hand[idx] = game.deck.pop()
            game.phase = "post_draw_bet"
            game.action_log.append(
                f"Trocas concluídas. {game.exchange_counts.get(game.host_id, 0)} carta(s) para {self.bot.get_guild(game.guild_id).get_member(game.host_id).display_name if self.bot.get_guild(game.guild_id) and self.bot.get_guild(game.guild_id).get_member(game.host_id) else 'Jogador 1'}, "
                f"{game.exchange_counts.get(game.opponent_id, 0)} para {self.bot.get_guild(game.guild_id).get_member(game.opponent_id).display_name if self.bot.get_guild(game.guild_id) and self.bot.get_guild(game.guild_id).get_member(game.opponent_id) else 'Jogador 2'}."
            )
            self._reset_betting_round(game, turn_id=game.opponent_id)
            await self._update_poker_status(game)
            await self._update_all_poker_dms(game)

    async def _handle_poker_accept(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Esse convite não é seu.", ephemeral=True)
            return
        if game.finished or game.phase != "invite" or game.accepted.get(player_id, False):
            await interaction.response.defer()
            return
        game.accepted[player_id] = True
        game.race_interactions[player_id] = interaction
        await self._update_poker_dm(game, player_id)
        await self._update_poker_status(game)
        await self._send_race_lobby_feedback(interaction, game.guild_id, player_id, "Duelo aceito.")
        if all(game.accepted.get(pid, False) for pid in game.players):
            game.phase = "pre_draw_bet"
            self._reset_betting_round(game, turn_id=game.host_id)
            game.action_log.append(f"Os dois jogadores aceitaram. Pot inicial de {game.pot} {self._CHIP_EMOJI}.")
            await self._update_poker_status(game)
            await self._update_all_poker_dms(game)

    async def _handle_poker_check_call(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa ação não é sua.", ephemeral=True)
            return
        if game.finished or game.phase not in {"pre_draw_bet", "post_draw_bet"}:
            await interaction.response.defer()
            return
        if game.turn_id != player_id:
            await interaction.response.send_message("Ainda não é a sua vez.", ephemeral=True)
            return
        current_bet = max(game.round_bets.values(), default=0)
        own_bet = game.round_bets.get(player_id, 0)
        to_call = max(0, current_bet - own_bet)
        guild = self.bot.get_guild(game.guild_id)
        member = guild.get_member(player_id) if guild else None
        name = member.display_name if member else "Jogador"
        if to_call > self._poker_total_stack(game, player_id):
            await interaction.response.send_message("Você não tem fichas suficientes para pagar.", ephemeral=True)
            return
        if to_call > 0:
            self._consume_poker_stack(game, player_id, to_call)
            game.round_bets[player_id] = current_bet
            game.pot += to_call
            game.action_log.append(f"{name} pagou {to_call} {self._CHIP_EMOJI}.")
        else:
            game.action_log.append(f"{name} deu check.")
        game.round_acted.add(player_id)
        game.turn_id = game.other_player(player_id)
        await self._update_poker_status(game)
        await self._update_all_poker_dms(game)
        await interaction.response.defer()
        await self._finish_betting_round_if_ready(game)

    async def _handle_poker_bet_raise(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa ação não é sua.", ephemeral=True)
            return
        if game.finished or game.phase not in {"pre_draw_bet", "post_draw_bet"}:
            await interaction.response.defer()
            return
        if game.turn_id != player_id:
            await interaction.response.send_message("Ainda não é a sua vez.", ephemeral=True)
            return
        current_bet = max(game.round_bets.values(), default=0)
        own_bet = game.round_bets.get(player_id, 0)
        target_bet = current_bet + _BET_SIZE if current_bet > own_bet else own_bet + _BET_SIZE
        extra = target_bet - own_bet
        if extra > self._poker_total_stack(game, player_id):
            await interaction.response.send_message("Você não tem fichas suficientes para essa aposta.", ephemeral=True)
            return
        self._consume_poker_stack(game, player_id, extra)
        game.round_bets[player_id] = target_bet
        game.pot += extra
        guild = self.bot.get_guild(game.guild_id)
        member = guild.get_member(player_id) if guild else None
        name = member.display_name if member else "Jogador"
        action_name = "apostou" if current_bet == own_bet else "aumentou"
        game.action_log.append(f"{name} {action_name} para {target_bet} {self._CHIP_EMOJI}.")
        game.round_acted = {player_id}
        game.turn_id = game.other_player(player_id)
        await self._update_poker_status(game)
        await self._update_all_poker_dms(game)
        await interaction.response.defer()

    async def _handle_poker_fold(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        self._touch_poker_game(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa ação não é sua.", ephemeral=True)
            return
        if game.finished or game.phase not in {"pre_draw_bet", "post_draw_bet"}:
            await interaction.response.defer()
            return
        if game.turn_id != player_id:
            await interaction.response.send_message("Ainda não é a sua vez.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._award_fold_win(game, game.other_player(player_id), player_id)

    async def _handle_poker_trigger(self, message: discord.Message) -> bool:
        self._cleanup_broken_poker_state(message.guild.id if message.guild else 0)
        content = (message.content or "").strip().lower()
        if content != "poker" and not content.startswith("poker "):
            return False

        guild = message.guild
        if guild is None:
            return False

        opponent = None
        for mentioned in message.mentions:
            if mentioned.id != message.author.id and not mentioned.bot:
                opponent = mentioned
                break

        if opponent is None:
            embed = self._make_poker_status_embed(
                "🃏 Duelo inválido",
                "Use a trigger como **poker @usuário** para iniciar uma partida interativa.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        if guild.id in self._poker_games:
            embed = self._make_poker_status_embed(
                "🃏 Partida em andamento",
                "Já existe uma partida de poker ativa neste servidor. Espere ela terminar para iniciar outra.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        if opponent.id == message.author.id:
            return True

        host_block_note = self._poker_entry_block_note(guild.id, message.author.id, POKER_BUY_IN)
        opp_block_note = self._poker_entry_block_note(guild.id, opponent.id, POKER_BUY_IN)
        if host_block_note:
            try:
                await message.channel.send(embed=self._make_poker_status_embed("🃏 Fichas insuficientes", host_block_note, ok=False))
            except Exception:
                pass
            return True
        if opp_block_note:
            try:
                await message.channel.send(embed=self._make_poker_status_embed("🃏 Rival sem fichas", f"{opponent.mention}: {opp_block_note}", ok=False))
            except Exception:
                pass
            return True
        host_ok, _host_balance, host_note = await self._ensure_action_chips(guild.id, message.author.id, POKER_BUY_IN)
        opp_ok, _opp_balance, opp_note = await self._ensure_action_chips(guild.id, opponent.id, POKER_BUY_IN)
        if not host_ok:
            try:
                await message.channel.send(embed=self._make_poker_status_embed("🃏 Fichas insuficientes", host_note or "Você não tem fichas suficientes para jogar.", ok=False))
            except Exception:
                pass
            return True
        if not opp_ok:
            try:
                await message.channel.send(embed=self._make_poker_status_embed("🃏 Rival sem fichas", opp_note or f"{opponent.mention} não tem fichas suficientes para jogar.", ok=False))
            except Exception:
                pass
            return True
        if self._needs_negative_confirmation(guild.id, message.author.id, POKER_BUY_IN):
            confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, POKER_BUY_IN, title="🃏 Confirmar entrada")
            if not confirmed:
                return True
        if self._needs_negative_confirmation(guild.id, opponent.id, POKER_BUY_IN):
            confirmed = await self._confirm_negative_via_message(message.channel, user_id=opponent.id, title="🃏 Confirmar entrada", note=self._negative_transition_note(guild.id, opponent.id, POKER_BUY_IN) or "")
            if not confirmed:
                try:
                    await message.channel.send(embed=self._make_poker_status_embed("🃏 Convite cancelado", f"{opponent.mention} não confirmou a entrada no poker.", ok=False))
                except Exception:
                    pass
                return True

        game = PokerGame(
            guild_id=guild.id,
            channel_id=message.channel.id,
            host_id=message.author.id,
            opponent_id=opponent.id,
        )
        game.deck = self._create_poker_deck()
        game.hands[message.author.id] = self._deal_poker_hand(game.deck)
        game.hands[opponent.id] = self._deal_poker_hand(game.deck)
        game.original_hands[message.author.id] = list(game.hands[message.author.id])
        game.original_hands[opponent.id] = list(game.hands[opponent.id])
        game.selected = {message.author.id: set(), opponent.id: set()}
        game.confirmed = {message.author.id: False, opponent.id: False}
        game.accepted = {message.author.id: False, opponent.id: False}
        game.entry_spend = {
            message.author.id: self._entry_spend_parts(guild.id, message.author.id, POKER_BUY_IN),
            opponent.id: self._entry_spend_parts(guild.id, opponent.id, POKER_BUY_IN),
        }
        host_stack_normal, host_stack_bonus, host_stack = self._poker_project_stack_after_buy_in(guild.id, message.author.id, POKER_BUY_IN)
        opp_stack_normal, opp_stack_bonus, opp_stack = self._poker_project_stack_after_buy_in(guild.id, opponent.id, POKER_BUY_IN)
        game.stack_normal = {message.author.id: host_stack_normal, opponent.id: opp_stack_normal}
        game.stack_bonus = {message.author.id: host_stack_bonus, opponent.id: opp_stack_bonus}
        game.stacks = {message.author.id: host_stack, opponent.id: opp_stack}
        game.round_bets = {message.author.id: POKER_BUY_IN, opponent.id: POKER_BUY_IN}
        game.pot = POKER_BUY_IN * 2
        game.exchange_counts = {message.author.id: 0, opponent.id: 0}
        self._poker_games[guild.id] = game

        try:
            status_message = await message.channel.send(
                embed=self._make_poker_status_embed(
                    "🃏 Convite para o Poker",
                    (
                        f"**Duelo:** {message.author.mention} × {opponent.mention}\nAs mãos privadas e o convite estão sendo enviados por DM."
                        + (f"\n{host_note}" if host_note else "")
                        + (f"\n{opponent.mention}: {opp_note}" if opp_note else "")
                    ),
                    ok=True,
                )
            )
            game.status_message = status_message
        except Exception:
            self._poker_games.pop(guild.id, None)
            return True

        dm_failures: list[discord.Member] = []
        for player in (message.author, opponent):
            try:
                dm_message = await player.send("Carregando sua mão...")
                game.dm_messages[player.id] = dm_message
                view = PokerSelectionView(self, game, player.id)
                game.views[player.id] = view
                await dm_message.edit(
                    content=None,
                    embed=self._make_poker_dm_embed(player, game, set(), False),
                    view=view,
                )
            except Exception:
                dm_failures.append(player)

        if dm_failures:
            failed_names = ", ".join(member.display_name for member in dm_failures)
            await self._cancel_poker_game(
                game,
                reason="dm_failed",
                notice=f"Não consegui enviar DM para: {failed_names}. Ativem as DMs do servidor e tentem de novo.",
            )
            return True

        await self._update_poker_status(game)
        return True
