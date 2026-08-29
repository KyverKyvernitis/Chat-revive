import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

import discord

from ..services.session_registry import GameSessionRegistry, MAX_ACTIVE_GAME_USERS_PER_GUILD


TRUCO_ENTRY = 10
TRUCO_BONUS_REWARD = 10
_TRUCO_INVITE_TIMEOUT = 60.0
_TRUCO_ACTION_TIMEOUT = 180.0

_TRUCO_RANKS = ["4", "5", "6", "7", "Q", "J", "K", "A", "2", "3"]
_TRUCO_SUITS = ["♦", "♠", "♥", "♣"]
_TRUCO_SUIT_STRENGTH = {"♦": 1, "♠": 2, "♥": 3, "♣": 4}
_TRUCO_PUBLIC_SUIT = {"♦": "♦️", "♠": "♠️", "♥": "♥️", "♣": "♣️"}
_TRUCO_LEVELS = [1, 3, 6, 9, 12]
_TRUCO_TARGET_POT_1V1 = {1: 20, 3: 40, 6: 60, 9: 90, 12: 120}
_TRUCO_TARGET_CONTRIB_1V1 = {1: 10, 3: 20, 6: 30, 9: 45, 12: 60}
_TRUCO_RAISE_NAMES = {3: "truco", 6: "seis", 9: "nove", 12: "doze"}
_TRUCO_TRIGGER_RE = re.compile(r"^\s*truco\s+<@!?(\d+)>\s*$", re.IGNORECASE)


@dataclass
class TrucoGame:
    session_id: str
    guild_id: int
    channel_id: int
    owner_id: int
    players_order: list[int]
    teams: list[tuple[int, ...]]
    variant: str = "normal"
    level: int = 1
    status: str = "invite"
    status_text: str = "Esperando a resposta do desafio"
    finish_reason: str | None = None
    winner_team: int | None = None
    turn_id: int | None = None
    hand_starter_id: int | None = None
    round_index: int = 0
    cards_on_table: dict[int, tuple[str, str]] = field(default_factory=dict)
    round_results: list[int | None] = field(default_factory=list)
    hands: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    table_history: list[str] = field(default_factory=list)
    status_message: discord.Message | None = None
    challenge_message: discord.Message | None = None
    vira: tuple[str, str] | None = None
    manilha_rank: str | None = None
    contribution: dict[int, int] = field(default_factory=dict)
    entry_spend: dict[int, dict[str, object]] = field(default_factory=dict)
    pot: int = 0
    accepted: bool = False
    pending_raise_by: int | None = None
    pending_raise_to: int | None = None
    finished: bool = False
    dm_messages: dict[int, discord.Message] = field(default_factory=dict)
    race_interactions: dict[int, discord.Interaction] = field(default_factory=dict)
    changefate_forced: bool = False
    changefate_consumed: bool = False
    changefate_token: str = ""
    match_history: dict[str, int] | None = None
    last_activity_at: float = field(default_factory=time.monotonic)

    @property
    def players(self) -> tuple[int, ...]:
        return tuple(self.players_order)


class TrucoChallengeView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_INVITE_TIMEOUT)
        self.cog = cog
        self.game = game
        self.accept_button = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
        self.decline_button = discord.ui.Button(label="Recusar", style=discord.ButtonStyle.danger)
        self.accept_button.callback = self._accept
        self.decline_button.callback = self._decline
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        lines = self.cog._truco_challenge_lines(self.game)
        row = discord.ui.ActionRow(self.accept_button, self.decline_button)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            row,
            accent_color=self.cog._truco_accent_color(self.game),
        ))

    async def _accept(self, interaction: discord.Interaction):
        await self.cog._handle_truco_accept(interaction, self.game)

    async def _decline(self, interaction: discord.Interaction):
        await self.cog._handle_truco_decline(interaction, self.game)

    async def on_timeout(self):
        try:
            await self.cog._expire_truco_invite(self.game)
        except Exception:
            pass


class TrucoTableView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_ACTION_TIMEOUT)
        self.cog = cog
        self.game = game
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        lines = self.cog._truco_status_lines(self.game)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines["header"])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines["meta"])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines["mesa"])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines["status"])),
            accent_color=self.cog._truco_accent_color(self.game),
        ))

    async def on_timeout(self):
        try:
            lock = self.cog._truco_get_play_lock(self.game)
            async with lock:
                if self.game.finished or not self.cog._truco_is_current_game(self.game):
                    return
                if self.cog._truco_idle_for(self.game) < (_TRUCO_ACTION_TIMEOUT - 1.0):
                    return
                loser = self.game.turn_id or self.game.players_order[0]
                await self.cog._finish_truco_game(
                    self.game,
                    winner_team=self.cog._truco_other_team(self.game, loser),
                    loser_id=loser,
                    reason="tempo esgotado",
                )
        except Exception:
            pass


class TrucoHandView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame, player_id: int):
        super().__init__(timeout=_TRUCO_ACTION_TIMEOUT)
        self.cog = cog
        self.game = game
        self.player_id = int(player_id)
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        hand = self.game.hands.get(self.player_id, [])
        disabled_cards = self.game.finished or self.game.status != "active" or self.game.turn_id != self.player_id
        card_buttons = []
        for index, card in enumerate(hand):
            button = discord.ui.Button(label=self.cog._truco_card_display(card), style=discord.ButtonStyle.secondary, disabled=disabled_cards)
            async def _callback(interaction: discord.Interaction, idx=index):
                self.game.race_interactions[self.player_id] = interaction
                await self.cog._handle_truco_play_card(interaction, self.game, self.player_id, idx)
            button.callback = _callback
            card_buttons.append(button)
        action_buttons = []
        if not self.game.finished:
            if self.game.status == "awaiting_raise_response" and self.cog._truco_can_answer_raise(self.game, self.player_id):
                accept = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
                accept.callback = self._accept_raise
                action_buttons.append(accept)
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.primary)
                    up.callback = self._raise_action
                    action_buttons.append(up)
                leave = discord.ui.Button(label="Correr", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                action_buttons.append(leave)
            elif self.game.status == "active" and self.game.turn_id == self.player_id:
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.primary)
                    up.callback = self._raise_action
                    action_buttons.append(up)
                leave = discord.ui.Button(label="Correr", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                action_buttons.append(leave)
        lines = self.cog._truco_hand_lines(self.game, self.player_id)
        items = []
        for key in ("header", "meta", "mesa", "cards", "status"):
            section = lines.get(key) or []
            if not section:
                continue
            if items:
                items.append(discord.ui.Separator())
            items.append(discord.ui.TextDisplay("\n".join(section)))
        if card_buttons:
            items.append(discord.ui.ActionRow(*card_buttons[:5]))
        if action_buttons:
            items.append(discord.ui.ActionRow(*action_buttons[:5]))
        self.add_item(discord.ui.Container(
            *items,
            accent_color=self.cog._truco_private_accent_color(self.game, self.player_id),
        ))

    async def _raise_action(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        await self.cog._handle_truco_raise(interaction, self.game)

    async def _run_action(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        await self.cog._handle_truco_run(interaction, self.game)

    async def _accept_raise(self, interaction: discord.Interaction):
        self.game.race_interactions[self.player_id] = interaction
        await self.cog._handle_truco_accept_raise(interaction, self.game)


class GincanaTrucoMixin:
    def _ensure_truco_state(self):
        if not hasattr(self, "_game_sessions"):
            self._game_sessions = GameSessionRegistry(
                max_active_users_per_guild=MAX_ACTIVE_GAME_USERS_PER_GUILD
            )
        if not hasattr(self, "_truco_games"):
            self._truco_games = {}
        if not hasattr(self, "_truco_guild_sessions"):
            self._truco_guild_sessions = {}
        if not hasattr(self, "_truco_play_locks"):
            self._truco_play_locks = {}
        if not hasattr(self, "_truco_play_inflight"):
            self._truco_play_inflight = set()

    def _truco_game_runtime_key(self, game: TrucoGame) -> int:
        return id(game)

    def _truco_get_play_lock(self, game: TrucoGame) -> asyncio.Lock:
        self._ensure_truco_state()
        key = self._truco_game_runtime_key(game)
        lock = self._truco_play_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._truco_play_locks[key] = lock
        return lock

    def _truco_clear_runtime_guards(self, game: TrucoGame):
        key = self._truco_game_runtime_key(game)
        self._truco_play_locks.pop(key, None)
        inflight = getattr(self, "_truco_play_inflight", None)
        if inflight is not None:
            for marker in list(inflight):
                if marker and marker[0] == key:
                    inflight.discard(marker)

    def _truco_remove_local_game(self, game: TrucoGame):
        current = self._truco_games.get(game.session_id)
        if current is not game:
            return
        self._truco_games.pop(game.session_id, None)
        guild_sessions = self._truco_guild_sessions.get(game.guild_id)
        if guild_sessions is not None:
            guild_sessions.discard(game.session_id)
            if not guild_sessions:
                self._truco_guild_sessions.pop(game.guild_id, None)
        self._truco_clear_runtime_guards(game)

    async def _truco_release_game(self, game: TrucoGame):
        if game.changefate_forced and not game.changefate_consumed:
            self._release_changefate_golden_reservation(
                game.guild_id,
                int(game.owner_id),
                game.changefate_token,
            )
        self._truco_remove_local_game(game)
        await self._game_sessions.release(game.session_id)

    def _truco_register_game(self, game: TrucoGame):
        self._truco_games[game.session_id] = game
        self._truco_guild_sessions.setdefault(game.guild_id, set()).add(game.session_id)

    def _truco_is_current_game(self, game: TrucoGame) -> bool:
        return self._truco_games.get(game.session_id) is game

    def _truco_touch_runtime(self, game_or_lobby) -> float:
        now = time.monotonic()
        try:
            game_or_lobby.last_activity_at = now
        except Exception:
            pass
        return now

    def _truco_idle_for(self, game_or_lobby) -> float:
        last_activity = float(getattr(game_or_lobby, "last_activity_at", 0.0) or 0.0)
        if last_activity <= 0:
            return 0.0
        return max(0.0, time.monotonic() - last_activity)

    def _truco_entry_refund_parts(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int, int, float]:
        spend = max(0, int(amount))
        temporary = min(self._coinflip_temp_bonus_available(guild_id, user_id), spend)
        current_bonus = int(self._get_user_bonus_chips(guild_id, user_id) or 0)
        use_bonus = min(current_bonus, spend - temporary)
        doc = self.db._get_user_doc(guild_id, user_id)
        expires_at = (
            float(doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
            if temporary > 0
            else 0.0
        )
        return spend - temporary - use_bonus, temporary + use_bonus, temporary, expires_at

    async def _truco_refund_consumed_entries(
        self,
        guild_id: int,
        consumed_entries: list[tuple[int, int, int, int, float]],
    ):
        for user_id, normal_amount, bonus_amount, temporary_amount, expires_at in consumed_entries:
            await self._refund_entry_spend(
                guild_id,
                int(user_id),
                {
                    "chips": int(normal_amount),
                    "bonus": int(bonus_amount),
                    "temporary_bonus": int(temporary_amount),
                    "_temporary_bonus_expires_at": float(expires_at),
                },
                reason="Devolução do truco",
            )

    async def _truco_abort_game_start(self, game: TrucoGame, *, notice: str):
        if getattr(game, "_start_abort_handled", False):
            return False
        game._start_abort_handled = True
        game.finished = True
        refund_entries = list(getattr(game, "entry_refunds", []) or [])
        if refund_entries:
            await self._truco_refund_consumed_entries(game.guild_id, refund_entries)
            game.entry_refunds = []
        if game.changefate_consumed:
            await self._restore_changefate_golden(game.guild_id, int(game.owner_id))
            game.changefate_consumed = False
        await self._truco_release_game(game)
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(discord.ui.TextDisplay(f"# 🃏 Truco\n{notice}"), accent_color=discord.Color.red()))
        target_message = game.status_message or game.challenge_message
        await self._truco_safe_edit(target_message, embed=None, view=closed)
        return False

    async def _truco_cleanup_stale_sessions(self):
        self._ensure_truco_state()
        expired_ids = set(await self._game_sessions.find_expired())
        stale_invites: list[TrucoGame] = []
        stale_games: list[TrucoGame] = []
        known_session_ids = set(self._truco_games)

        for game in list(self._truco_games.values()):
            if game.finished:
                self._truco_remove_local_game(game)
                await self._game_sessions.release(game.session_id)
                continue
            idle = self._truco_idle_for(game)
            expired = game.session_id in expired_ids
            if game.status == "invite" and (expired or idle > (_TRUCO_INVITE_TIMEOUT + 20.0)):
                stale_invites.append(game)
            elif game.status != "invite" and (expired or idle > (_TRUCO_ACTION_TIMEOUT + 45.0)):
                stale_games.append(game)

        for game in stale_invites:
            await self._expire_truco_invite(game)
        for game in stale_games:
            lock = self._truco_get_play_lock(game)
            async with lock:
                if game.finished or not self._truco_is_current_game(game):
                    continue
                if self._truco_idle_for(game) <= (_TRUCO_ACTION_TIMEOUT + 45.0):
                    await self._game_sessions.touch(
                        game.session_id,
                        ttl=_TRUCO_ACTION_TIMEOUT + 90.0,
                    )
                    continue
                loser_id = game.turn_id or game.players_order[0]
                await self._finish_truco_game(
                    game,
                    winner_team=self._truco_other_team(game, loser_id),
                    loser_id=loser_id,
                    reason="tempo esgotado",
                )

        for orphan_session_id in expired_ids - known_session_ids:
            await self._game_sessions.release(orphan_session_id)

    def _truco_variant(self, game_or_lobby) -> str:
        return str(getattr(game_or_lobby, "variant", "normal") or "normal").lower()

    def _truco_is_golden(self, game_or_lobby) -> bool:
        return self._truco_variant(game_or_lobby) == "golden"

    def _truco_title_text(self, game_or_lobby) -> str:
        return f"{self._EFFECT_EMOJI} Truco dourado" if self._truco_is_golden(game_or_lobby) else "🃏 Truco"

    def _truco_accent_color(self, game_or_lobby) -> discord.Color:
        return discord.Color.gold() if self._truco_is_golden(game_or_lobby) else discord.Color.dark_green()

    def _truco_private_accent_color(self, game: TrucoGame, player_id: int) -> discord.Color:
        if game.finished:
            player_team = self._truco_team_index(game, int(player_id))
            return discord.Color.green() if player_team == game.winner_team else discord.Color.red()
        return discord.Color.gold() if self._truco_is_golden(game) else discord.Color.blurple()

    def _truco_bonus_reward_value(self, game_or_lobby) -> int:
        return self._truco_bonus_reward_for_variant(self._truco_variant(game_or_lobby))

    def _roll_truco_variant_for_user(self, guild_id: int, user_id: int) -> str:
        return "golden" if random.random() < self._special_variant_chance_for_user(guild_id, user_id) else "normal"

    def _truco_create_deck(self):
        deck = [(rank, suit) for rank in _TRUCO_RANKS for suit in _TRUCO_SUITS]
        random.shuffle(deck)
        return deck

    def _truco_card_display(self, card):
        return f"{card[0]}{card[1]}" if card else "—"

    def _truco_card_public_display(self, card):
        return f"{card[0]}{_TRUCO_PUBLIC_SUIT.get(card[1], card[1])}" if card else "—"

    def _truco_manilha_rank(self, vira_rank: str) -> str:
        return _TRUCO_RANKS[(_TRUCO_RANKS.index(vira_rank) + 1) % len(_TRUCO_RANKS)]

    def _truco_compare_cards(self, card_a, card_b, manilha_rank: str) -> int:
        ra, sa = card_a
        rb, sb = card_b
        am = ra == manilha_rank
        bm = rb == manilha_rank
        if am and bm:
            return 1 if _TRUCO_SUIT_STRENGTH[sa] > _TRUCO_SUIT_STRENGTH[sb] else -1 if _TRUCO_SUIT_STRENGTH[sa] < _TRUCO_SUIT_STRENGTH[sb] else 0
        if am:
            return 1
        if bm:
            return -1
        ia = _TRUCO_RANKS.index(ra)
        ib = _TRUCO_RANKS.index(rb)
        return 1 if ia > ib else -1 if ia < ib else 0

    def _truco_other_team(self, game: TrucoGame, player_id: int) -> int:
        t = self._truco_team_index(game, player_id)
        return 1 - t if t in (0, 1) else 0

    def _truco_team_index(self, game: TrucoGame, player_id: int) -> int | None:
        pid = int(player_id)
        for idx, team in enumerate(game.teams):
            if pid in team:
                return idx
        return None


    def _truco_target_contrib(self, game: TrucoGame, level: int) -> int:
        return _TRUCO_TARGET_CONTRIB_1V1[level]

    def _truco_target_pot(self, game: TrucoGame, level: int) -> int:
        return _TRUCO_TARGET_POT_1V1[level]

    def _truco_next_raise_level(self, current: int):
        try:
            i = _TRUCO_LEVELS.index(int(current))
        except ValueError:
            return None
        return _TRUCO_LEVELS[i + 1] if i + 1 < len(_TRUCO_LEVELS) else None

    def _truco_next_raise_label(self, game: TrucoGame):
        nxt = self._truco_next_raise_level(game.pending_raise_to or game.level)
        return _TRUCO_RAISE_NAMES.get(nxt) if nxt else None

    def _truco_round_label(self, round_index: int) -> str:
        return {0: "1ª", 1: "2ª", 2: "3ª"}.get(int(round_index), f"{int(round_index)+1}ª")

    def _truco_score_counts(self, game: TrucoGame) -> tuple[int, int]:
        return (
            sum(1 for result in game.round_results if result == 0),
            sum(1 for result in game.round_results if result == 1),
        )

    def _truco_fixed_player_order(self, game: TrucoGame, viewer_id: int | None = None) -> list[int]:
        order = [int(user_id) for user_id in game.players_order]
        if viewer_id is not None and int(viewer_id) in order:
            viewer = int(viewer_id)
            order = [viewer, *[user_id for user_id in order if user_id != viewer]]
        return order

    def _truco_score_text(self, game: TrucoGame, guild: discord.Guild | None, viewer_id: int | None = None) -> str:
        team0, team1 = self._truco_score_counts(game)
        if viewer_id is not None:
            viewer_team = self._truco_team_index(game, int(viewer_id))
            own = team0 if viewer_team == 0 else team1
            other = team1 if viewer_team == 0 else team0
            opponent_id = next(
                (user_id for user_id in game.players if self._truco_team_index(game, user_id) != viewer_team),
                None,
            )
            opponent = self._truco_member_name(guild, opponent_id) if opponent_id is not None else "Adversário"
            return f"Você **{own}** · {opponent} **{other}**"
        player0 = self._truco_member_name(guild, int(game.teams[0][0]))
        player1 = self._truco_member_name(guild, int(game.teams[1][0]))
        return f"{player0} **{team0}** · {player1} **{team1}**"

    def _truco_round_history_lines(self, game: TrucoGame, guild: discord.Guild | None, viewer_id: int | None = None) -> list[str]:
        lines: list[str] = []
        for index, winner_team in enumerate(game.round_results):
            label = self._truco_round_label(index)
            if winner_team is None:
                winner = "Empate"
            else:
                winner_id = int(game.teams[winner_team][0])
                winner = "Você" if viewer_id is not None and winner_id == int(viewer_id) else self._truco_member_name(guild, winner_id)
            lines.append(f"**{label}** · {winner}")
        return lines

    def _truco_match_history_lines(
        self,
        game: TrucoGame,
        guild: discord.Guild | None,
        *,
        viewer_id: int | None = None,
    ) -> list[str]:
        history = game.match_history or {}
        try:
            low_id = int(history.get("player_low_id", 0) or 0)
            high_id = int(history.get("player_high_id", 0) or 0)
            low_wins = max(0, int(history.get("low_wins", 0) or 0))
            high_wins = max(0, int(history.get("high_wins", 0) or 0))
            total_games = max(0, int(history.get("total_games", 0) or 0))
            streak_winner_id = int(history.get("streak_winner_id", 0) or 0)
            streak_count = max(0, int(history.get("streak_count", 0) or 0))
        except Exception:
            return []

        if low_id <= 0 or high_id <= 0 or total_games <= 0:
            return []

        wins_by_user = {low_id: low_wins, high_id: high_wins}
        if viewer_id is not None and int(viewer_id) in wins_by_user:
            first_id = int(viewer_id)
            second_id = high_id if first_id == low_id else low_id
        else:
            winner_id = int(game.teams[game.winner_team or 0][0]) if game.winner_team in (0, 1) else low_id
            first_id = winner_id if winner_id in wins_by_user else low_id
            second_id = high_id if first_id == low_id else low_id

        def _label(user_id: int) -> str:
            if viewer_id is not None and int(viewer_id) == int(user_id):
                return "Você"
            return self._truco_member_name(guild, user_id)

        first_name = _label(first_id)
        second_name = _label(second_id)
        first_wins = wins_by_user[first_id]
        second_wins = wins_by_user[second_id]
        partida = "partida disputada" if total_games == 1 else "partidas disputadas"
        lines = [
            "## Histórico de partidas",
            f"**{first_name}** `{first_wins}` ━ `{second_wins}` **{second_name}**",
        ]

        if first_wins == second_wins:
            lines.append(f"*Duelo empatado após {total_games} {partida}*")
        else:
            lines.append(f"*{total_games} {partida}*")

        if streak_count >= 2 and streak_winner_id in wins_by_user:
            streak_name = _label(streak_winner_id)
            lines.append(f"🔥 **{streak_name}** venceu as últimas **{streak_count} partidas**")
        return lines

    def _truco_member_name(self, guild: discord.Guild | None, user_id: int) -> str:
        member = guild.get_member(int(user_id)) if guild else None
        return member.display_name if member else str(user_id)

    def _truco_member_mention(self, guild: discord.Guild | None, user_id: int | None) -> str:
        if not user_id:
            return "alguém"
        member = guild.get_member(int(user_id)) if guild else None
        return member.mention if member else f"<@{int(user_id)}>"



    def _truco_rotate_order(self, players_order: list[int], starter_id: int) -> list[int]:
        if starter_id not in players_order:
            return list(players_order)
        idx = players_order.index(starter_id)
        return list(players_order[idx:]) + list(players_order[:idx])

    def _truco_make_game(
        self,
        guild_id: int,
        channel_id: int,
        players_order: list[int],
        *,
        variant: str = "normal",
    ) -> TrucoGame:
        players = [int(user_id) for user_id in players_order]
        game = TrucoGame(
            session_id=f"truco:{uuid4().hex}",
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            owner_id=players[0],
            players_order=players,
            teams=[(players[0],), (players[1],)],
            variant=str(variant or "normal"),
            level=1,
            status="invite",
            hand_starter_id=players[0],
            turn_id=players[0],
            contribution={user_id: TRUCO_ENTRY for user_id in players},
        )
        game.pot = self._truco_target_pot(game, 1)
        self._truco_touch_runtime(game)
        return game

    def _truco_challenge_lines(self, game: TrucoGame) -> list[str]:
        guild = self.bot.get_guild(game.guild_id)
        title = self._truco_title_text(game)
        reward_bonus = self._truco_bonus_reward_value(game)
        bonus_label = "Bônus dourado" if self._truco_is_golden(game) else "Bônus do vencedor"
        lines = [
            f"# {title}",
            f"{self._truco_member_mention(guild, game.players_order[0])} desafiou {self._truco_member_mention(guild, game.players_order[1])}",
            "",
            f"**Entrada:** {self._chip_amount(TRUCO_ENTRY)} por jogador",
            f"**Valendo:** {self._chip_amount(game.pot)}",
            f"**{bonus_label}:** **+{reward_bonus}** {self._CHIP_BONUS_EMOJI}",
        ]
        marker = str(getattr(game, "race_effect_marker", "") or "").strip()
        if marker:
            lines.extend(["", marker])
        else:
            lines.extend(["", "Vença duas vazas para ficar com o pote"])
        return lines

    def _truco_status_lines(self, game: TrucoGame, *, title: str = "🃏 Truco") -> dict[str, list[str]]:
        guild = self.bot.get_guild(game.guild_id)
        title = self._truco_title_text(game)
        duel = [
            f"# {title}",
            f"{self._truco_member_mention(guild, int(game.teams[0][0]))} × {self._truco_member_mention(guild, int(game.teams[1][0]))}",
        ]
        meta = [
            f"**Valendo:** {self._chip_amount(game.pot)}",
            f"**Vira:** {self._truco_card_public_display(game.vira)} · **Manilha:** {game.manilha_rank or '—'}",
            f"**Placar:** {self._truco_score_text(game, guild)}",
        ]
        mesa = ["## Mesa"]
        shown = False
        for user_id in self._truco_fixed_player_order(game):
            card = game.cards_on_table.get(user_id)
            mesa.append(f"**{self._truco_member_name(guild, user_id)}** · {self._truco_card_public_display(card) if card else '—'}")
            shown = shown or bool(card)
        if not shown:
            mesa.append("A mesa está limpa")
        history = self._truco_round_history_lines(game, guild)
        if history:
            mesa.extend(["", "## Vazas", *history])
        status = [
            "## Agora",
            game.status_text,
            "Jogue pela mensagem direta enviada pelo bot",
        ]
        return {"header": duel, "meta": meta, "mesa": mesa, "status": status}

    def _truco_hand_lines(self, game: TrucoGame, player_id: int, *, dm_ok: bool = True) -> dict[str, list[str]]:
        guild = self.bot.get_guild(game.guild_id)
        pid = int(player_id)
        cards = game.hands.get(pid, [])
        player_team = self._truco_team_index(game, pid)

        if game.finished:
            won = player_team is not None and player_team == game.winner_team
            reason = game.finish_reason or "jogo encerrado"
            loser_name = self._truco_member_name(guild, getattr(game, "loser_id", 0) or 0)
            winner_id = int(game.teams[game.winner_team or 0][0])
            opponent_name = self._truco_member_name(guild, winner_id)

            if reason == "correu" and getattr(game, "loser_id", None) == pid:
                header = ["# 🏳️ Você correu", "A partida terminou quando você correu"]
            elif reason == "correu" and won:
                header = ["# 🎉 Vitória", f"{loser_name} correu da partida"]
            elif reason == "tempo esgotado" and getattr(game, "loser_id", None) == pid:
                header = ["# ⏱️ Tempo esgotado", "Você não jogou a tempo"]
            elif reason == "tempo esgotado" and won:
                header = ["# 🎉 Vitória", f"{loser_name} não jogou a tempo"]
            elif won:
                header = ["# 🎉 Vitória", "Você venceu a partida"]
            else:
                header = ["# 💥 Derrota", f"{opponent_name} venceu a partida"]

            meta = [
                f"**Valendo:** {self._chip_amount(game.pot)}",
                f"**Vira:** {self._truco_card_public_display(game.vira)} · **Manilha:** {game.manilha_rank or '—'}",
            ]
            history = self._truco_round_history_lines(game, guild, viewer_id=pid)
            rounds = ["## Vazas", *(history or ["Nenhuma vaza foi concluída"])]

            current_chips = int(self.db.get_user_chips(game.guild_id, pid, default=100) or 0)
            current_bonus = int(self._get_user_bonus_chips(game.guild_id, pid) or 0)
            contribution = int(game.contribution.get(pid, TRUCO_ENTRY))
            if won:
                result_lines = [
                    "## Recompensa",
                    f"Você recebeu **{game.pot}** {self._CHIP_GAIN_EMOJI} e **+{self._truco_bonus_reward_value(game)}** {self._CHIP_BONUS_EMOJI}",
                    f"**Saldo:** **{current_chips}** {self._CHIP_EMOJI} · **{current_bonus}** {self._CHIP_BONUS_EMOJI}",
                ]
            else:
                result_lines = [
                    "## Resultado",
                    f"Você perdeu **{contribution}** {self._CHIP_LOSS_EMOJI}",
                    f"**Saldo:** **{current_chips}** {self._CHIP_EMOJI} · **{current_bonus}** {self._CHIP_BONUS_EMOJI}",
                ]
            match_history = self._truco_match_history_lines(game, guild, viewer_id=pid)
            return {"header": header, "meta": meta, "mesa": rounds, "cards": result_lines, "status": match_history}

        if game.status == "awaiting_raise_response" and self._truco_can_answer_raise(game, pid):
            raise_name = _TRUCO_RAISE_NAMES.get(game.pending_raise_to, "aumento")
            status = f"{self._truco_member_name(guild, game.pending_raise_by or 0)} pediu **{raise_name}**\nAceite, aumente ou corra"
        elif game.status == "awaiting_raise_response":
            status = "Aguardando o adversário responder ao aumento"
        elif pid == game.turn_id:
            status = "Escolha uma carta para jogar"
        else:
            status = f"Aguardando **{self._truco_member_name(guild, game.turn_id or 0)}** jogar"

        header = [f"# {self._truco_title_text(game)}", "## Sua partida"]
        meta = [
            f"**Valendo:** {self._chip_amount(game.pot)}",
            f"**Vira:** {self._truco_card_public_display(game.vira)} · **Manilha:** {game.manilha_rank or '—'}",
            f"**Placar:** {self._truco_score_text(game, guild, viewer_id=pid)}",
        ]

        mesa = ["## Mesa"]
        any_card = False
        for user_id in self._truco_fixed_player_order(game, viewer_id=pid):
            label = "Você" if int(user_id) == pid else self._truco_member_name(guild, user_id)
            card = game.cards_on_table.get(user_id)
            mesa.append(f"**{label}** · {self._truco_card_public_display(card) if card else '—'}")
            any_card = any_card or bool(card)
        if not any_card:
            mesa.append("A mesa está limpa")
        history = self._truco_round_history_lines(game, guild, viewer_id=pid)
        if history:
            mesa.extend(["", "## Vazas", *history])

        cards_lines = [
            "## Suas cartas",
            " · ".join(self._truco_card_public_display(card) for card in cards) if cards else "Você já jogou todas as cartas",
        ]
        status_lines = ["## Agora", status]
        if not dm_ok:
            status_lines.append("Não consegui abrir sua DM; por isso, mostrei a mão aqui")
        return {"header": header, "meta": meta, "mesa": mesa, "cards": cards_lines, "status": status_lines}

    def _truco_status_embed(self, game: TrucoGame, *, title: str = "🃏 Truco") -> discord.Embed:
        lines = self._truco_status_lines(game, title=title)
        embed = discord.Embed(title=title, description="\n".join(lines["header"] + [""] + lines["mesa"] + [""] + lines["status"]), color=discord.Color.dark_green())
        return embed

    def _truco_hand_embed(self, game: TrucoGame, player_id: int, *, dm_ok: bool = True) -> discord.Embed:
        lines = self._truco_hand_lines(game, player_id, dm_ok=dm_ok)
        return discord.Embed(title="🃏 Seu jogo", description="\n".join(lines["header"] + [""] + lines["mesa"] + [""] + lines["cards"] + [""] + lines["status"]), color=discord.Color.blurple())

    async def _truco_safe_edit(self, message, *, embed=None, view=None, content=None):
        if message is None:
            return False
        try:
            if view is not None and embed is None and content is None:
                await message.edit(view=view)
            else:
                await message.edit(content=content, embed=embed, view=view)
            return True
        except Exception:
            try:
                if view is not None and embed is None:
                    await message.edit(content=content, view=view)
                    return True
            except Exception:
                pass
        return False


    async def _truco_probe_dm(self, user_id: int) -> bool:
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
        except Exception:
            return False
        try:
            dm = user.dm_channel or await user.create_dm()
            probe = await dm.send("🃏 Truco\nMensagem de teste para confirmar sua DM")
            try:
                await probe.delete()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def _truco_require_dm_for_players(self, player_ids, *, interaction=None, channel=None, guild=None) -> bool:
        missing = []
        for uid in player_ids:
            if not await self._truco_probe_dm(int(uid)):
                missing.append(int(uid))
        if not missing:
            return True
        if len(missing) > 1:
            text = "Não consegui enviar mensagem direta para todos os jogadores\nHabilitem as mensagens diretas do servidor e tentem novamente"
        else:
            mention = None
            if guild is not None:
                m = guild.get_member(missing[0])
                mention = m.mention if m else None
            if interaction is not None:
                text = "Não consegui te enviar mensagem direta\nHabilite as mensagens diretas do servidor e tente novamente"
            else:
                text = f"Não consegui enviar mensagem direta para {mention or 'esse jogador'}\nHabilite as mensagens diretas do servidor e tente novamente"
        if interaction is not None:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        elif channel is not None:
            await channel.send(embed=self._make_embed("🃏 Truco", text, ok=False))
        return False

    async def _truco_update_interaction_message(self, interaction: discord.Interaction, *, view: discord.ui.LayoutView | None = None, content: str | None = None):
        target = getattr(interaction, 'message', None)
        if interaction.response.is_done():
            return await self._truco_safe_edit(target, view=view, content=content, embed=None)
        try:
            await interaction.response.edit_message(content=content, embed=None, view=view)
            return True
        except Exception:
            return await self._truco_safe_edit(target, view=view, content=content, embed=None)

    async def _truco_refresh_private_views(self, game: TrucoGame):
        self._truco_touch_runtime(game)
        for player_id in game.players:
            await self._truco_send_hand_dm(game, player_id, quiet=True)

    async def _truco_show_turn(self, game: TrucoGame):
        self._truco_touch_runtime(game)
        if game.finished:
            return
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"Aguardando {self._truco_member_mention(guild, game.turn_id)} jogar"
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    def _truco_round_winner_team(self, game: TrucoGame) -> int | None:
        if not game.cards_on_table:
            return None
        best_card = None
        winning_player = None
        tie_teams: set[int] = set()
        for pid, card in game.cards_on_table.items():
            if best_card is None:
                best_card = card
                winning_player = pid
                tie_teams = {self._truco_team_index(game, pid)}
                continue
            cmp = self._truco_compare_cards(card, best_card, game.manilha_rank or "")
            if cmp > 0:
                best_card = card
                winning_player = pid
                tie_teams = {self._truco_team_index(game, pid)}
            elif cmp == 0:
                tie_teams.add(self._truco_team_index(game, pid))
        if len(tie_teams) > 1:
            return None
        return self._truco_team_index(game, winning_player)

    def _truco_hand_winner_team(self, game: TrucoGame) -> int | None:
        results = list(game.round_results)
        if not results:
            return None
        if len(results) >= 1 and results[0] is not None and results.count(results[0]) >= 2:
            return results[0]
        if len(results) == 1:
            return None
        first, second = results[0], results[1]
        if first is None and second is not None:
            return second
        if first is not None and second is None:
            return first
        if first is not None and second is not None and first == second:
            return first
        if len(results) < 3:
            return None
        third = results[2]
        if third is not None:
            return third
        if first is not None:
            return first
        starter_team = self._truco_team_index(game, game.hand_starter_id or game.players_order[0])
        return 0 if starter_team is None else starter_team

    async def _truco_resolve_round(self, game: TrucoGame):
        if len(game.cards_on_table) < 2:
            return
        game.status_text = "Conferindo a vaza..."
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        winner_team = self._truco_round_winner_team(game)
        label = self._truco_round_label(game.round_index)
        guild = self.bot.get_guild(game.guild_id)
        if winner_team is None:
            game.status_text = f"A {label.lower()} vaza empatou"
            game.table_history.append(game.status_text)
        else:
            winning_players = [
                user_id for user_id in game.cards_on_table
                if self._truco_team_index(game, user_id) == winner_team
            ]
            winner_id = max(
                winning_players,
                key=lambda user_id: self._truco_card_strength_key(
                    game.cards_on_table[user_id], game.manilha_rank or ""
                ),
            )
            name = self._truco_member_name(guild, winner_id)
            game.status_text = f"{name} venceu a {label.lower()} vaza"
            game.table_history.append(game.status_text)
        game.round_results.append(winner_team)
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)
        await asyncio.sleep(0.8)
        hand_winner = self._truco_hand_winner_team(game)
        if winner_team is not None:
            winning_players = [
                user_id for user_id in game.cards_on_table
                if self._truco_team_index(game, user_id) == winner_team
            ]
            starter = max(
                winning_players,
                key=lambda user_id: self._truco_card_strength_key(
                    game.cards_on_table[user_id], game.manilha_rank or ""
                ),
            )
        else:
            starter = game.hand_starter_id or game.players_order[0]
        game.cards_on_table.clear()
        game.round_index += 1
        if hand_winner is not None or len(game.round_results) >= 3:
            final_winner = hand_winner if hand_winner is not None else (self._truco_team_index(game, starter) or 0)
            loser_id = int(game.teams[1 - final_winner][0])
            await self._finish_truco_game(game, winner_team=final_winner, loser_id=loser_id, reason="jogo encerrado")
            return
        game.hand_starter_id = starter
        game.players_order = self._truco_rotate_order(game.players_order, starter)
        game.turn_id = starter
        await self._truco_show_turn(game)

    def _truco_card_strength_key(self, card, manilha_rank: str):
        rank, suit = card
        if rank == manilha_rank:
            return (1, _TRUCO_SUIT_STRENGTH[suit])
        return (0, _TRUCO_RANKS.index(rank))

    async def _finish_truco_game(self, game: TrucoGame, *, winner_team: int, loser_id: int, reason: str):
        self._truco_touch_runtime(game)
        if game.finished:
            return
        game.finished = True
        game.status = "finishing"
        game.finish_reason = reason
        game.winner_team = winner_team
        game.loser_id = loser_id

        winner_id = int(game.teams[winner_team][0])
        loser_user_id = int(game.teams[1 - winner_team][0])
        owner_id = int(game.owner_id)
        public_race_notices: list[str] = []
        try:
            for user_id in game.players:
                await self.db.add_user_game_stat(game.guild_id, user_id, "truco_games", 1)
            await self.db.add_user_game_stat(game.guild_id, winner_id, "truco_wins", 1)
            await self._record_game_played(game.guild_id, winner_id, weekly_points=6)
            win_reason = "Vitória no truco dourado" if self._truco_is_golden(game) else "Vitória no truco"
            bonus_reason = "Bônus do truco dourado" if self._truco_is_golden(game) else "Bônus do truco"
            await self._change_user_chips(game.guild_id, winner_id, game.pot, mark_activity=True, reason=win_reason)
            await self._change_user_bonus_chips(
                game.guild_id,
                winner_id,
                self._truco_bonus_reward_value(game),
                mark_activity=True,
                reason=bonus_reason,
            )
            await self.db.add_user_game_stat(game.guild_id, loser_user_id, "truco_losses", 1)
            await self._record_game_played(game.guild_id, loser_user_id, weekly_points=2)

            record_history = getattr(self.db, "record_truco_match", None)
            if callable(record_history):
                try:
                    game.match_history = await record_history(
                        game.guild_id,
                        int(game.teams[0][0]),
                        int(game.teams[1][0]),
                        winner_id,
                        match_id=game.session_id,
                    )
                except Exception as exc:
                    print(f"[truco] Falha ao registrar histórico da partida {game.session_id}: {exc}")

            if reason == "jogo encerrado":
                for user_id, did_win in ((winner_id, True), (loser_user_id, False)):
                    entry_spend = game.entry_spend.get(user_id) or {
                        "chips": int(game.contribution.get(user_id, TRUCO_ENTRY) or TRUCO_ENTRY),
                        "bonus": 0,
                    }
                    coringa_note = ""
                    if not did_win:
                        normalized_spend = self._normalize_entry_spend(entry_spend)
                        paid_entry = int(normalized_spend.get("chips", 0) or 0) + int(normalized_spend.get("bonus", 0) or 0)
                        refund, refund_mode = await self._maybe_apply_coringa_loss_refund(
                            game.guild_id,
                            user_id,
                            paid_entry,
                            chance=0.35,
                        )
                        if refund > 0:
                            coringa_note = self._race_effect_message(
                                game.guild_id,
                                user_id,
                                "joker" if refund_mode == "joker" else "as",
                                (
                                    f"**+{refund}** {self._CHIP_BONUS_EMOJI}"
                                    if refund_mode == "joker"
                                    else f"recuperou {self._chip_text(refund, kind='gain')} da entrada"
                                ),
                            )
                    notes = await self._apply_new_race_result(
                        game.guild_id,
                        user_id,
                        won=did_win,
                        entry_spend=entry_spend,
                        payout=(game.pot + self._truco_bonus_reward_value(game)) if did_win else 0,
                        valid=True,
                    )
                    if coringa_note:
                        notes.append(coringa_note)
                    await self._route_lobby_race_notices(
                        game.race_interactions.get(user_id),
                        game.guild_id,
                        user_id,
                        owner_id,
                        notes,
                        public_race_notices,
                    )
        finally:
            game.status = "finished"
            await self._truco_release_game(game)

        first_game_user_ids = await self._unlock_first_game_for_users(game.guild_id, game.players)
        guild = self.bot.get_guild(game.guild_id)
        winner_text = self._truco_member_mention(guild, winner_id)
        loser_text = self._truco_member_mention(guild, loser_id)

        if reason == "correu":
            title = "# 🏳️ Partida encerrada"
            lead = f"{loser_text} correu\nVitória de {winner_text}"
            game.status_text = "Partida encerrada porque um jogador correu"
        elif reason == "tempo esgotado":
            title = "# ⏱️ Tempo esgotado"
            lead = f"{loser_text} não jogou a tempo\nVitória de {winner_text}"
            game.status_text = "Partida encerrada por tempo esgotado"
        else:
            title = (
                f"# {self._EFFECT_EMOJI} Vitória no truco dourado"
                if self._truco_is_golden(game)
                else "# 🏆 Vitória no truco"
            )
            lead = f"{winner_text} venceu a partida contra {loser_text}"
            game.status_text = "Partida encerrada"

        meta = [
            f"**Valendo:** {self._chip_amount(game.pot)}",
            f"**Vira:** {self._truco_card_public_display(game.vira)} · **Manilha:** {game.manilha_rank or '—'}",
        ]
        round_history = self._truco_round_history_lines(game, guild)
        rounds = ["## Vazas", *(round_history or ["Nenhuma vaza foi concluída"])]
        reward = f"{winner_text} recebeu **{game.pot}** {self._CHIP_GAIN_EMOJI} e **+{self._truco_bonus_reward_value(game)}** {self._CHIP_BONUS_EMOJI}"
        rewards = ["## Prêmio", reward]
        match_history = self._truco_match_history_lines(game, guild)

        items = [
            discord.ui.TextDisplay("\n".join([title, lead])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(meta)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(rounds)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(rewards)),
        ]
        if public_race_notices:
            items.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(["## Habilidade", *public_race_notices])),
            ])
        if match_history:
            items.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(match_history)),
            ])
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(
            *items,
            accent_color=self._truco_accent_color(game),
        ))
        delivered = await self._truco_safe_edit(game.status_message, embed=None, view=closed)
        if not delivered and public_race_notices:
            self._queue_private_race_notices(game.guild_id, owner_id, public_race_notices)
        await self._truco_refresh_private_views(game)
        achievement_channel = getattr(game.status_message, "channel", None)
        if achievement_channel is None and guild is not None:
            achievement_channel = guild.get_channel(game.channel_id)
        await self._send_first_game_notices(achievement_channel, game.guild_id, first_game_user_ids)

    async def _expire_truco_invite(self, game: TrucoGame):
        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or game.accepted or game.status != "invite":
                return
            game.finished = True
            await self._truco_release_game(game)
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# Desafio expirado\nNinguém respondeu ao truco a tempo"),
            accent_color=discord.Color.red(),
        ))
        await self._truco_safe_edit(game.challenge_message, embed=None, view=closed)

    async def _handle_truco_trigger(self, message: discord.Message) -> bool:
        self._ensure_truco_state()
        guild = message.guild
        if guild is None:
            return False
        raw = str(message.content or "").strip()
        if not _TRUCO_TRIGGER_RE.match(raw):
            return False

        await self._truco_cleanup_stale_sessions()
        mentions = [member for member in message.mentions if not member.bot]
        if len(mentions) != 1:
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Use `truco @usuário` para desafiar alguém", ok=False))
            return True

        challenger = message.author
        opponent = mentions[0]
        if opponent.id == challenger.id:
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Você precisa desafiar outra pessoa", ok=False))
            return True
        if await self._game_sessions.is_user_busy(challenger.id):
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Você já participa de uma partida ou possui um desafio pendente", ok=False))
            return True
        if await self._game_sessions.is_user_busy(opponent.id):
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Esse usuário já participa de outra partida ou possui um desafio pendente", ok=False))
            return True

        ok, _current, note = await self._ensure_action_chips(guild.id, challenger.id, TRUCO_ENTRY)
        if not ok:
            await message.channel.send(embed=self._make_embed("🃏 Truco", note or "Você não tem saldo suficiente para entrar nesse jogo", ok=False))
            return True
        if self._needs_negative_confirmation(guild.id, challenger.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_from_message(
                message,
                guild.id,
                challenger.id,
                TRUCO_ENTRY,
            )
            if not confirmed:
                return True

        game = self._truco_make_game(
            guild.id,
            message.channel.id,
            [challenger.id, opponent.id],
            variant="normal",
        )
        changefate_forced = await self._reserve_changefate_golden(
            guild.id,
            challenger.id,
            game.session_id,
        )
        game.variant = (
            "golden"
            if changefate_forced
            else self._roll_truco_variant_for_user(guild.id, challenger.id)
        )
        game.changefate_forced = bool(changefate_forced)
        game.changefate_token = game.session_id if changefate_forced else ""
        reservation = await self._game_sessions.create_pending(
            session_id=game.session_id,
            game_type="truco",
            guild_id=guild.id,
            owner_id=challenger.id,
            ttl=_TRUCO_INVITE_TIMEOUT + 30.0,
            required_free_user_ids={opponent.id},
        )
        if not reservation.ok:
            if changefate_forced:
                self._release_changefate_golden_reservation(guild.id, challenger.id, game.session_id)
            if reservation.code == "guild_full":
                text = (
                    f"Este servidor já possui {MAX_ACTIVE_GAME_USERS_PER_GUILD} jogadores "
                    "em partidas de Truco"
                )
            elif opponent.id in reservation.busy_user_ids:
                text = "Esse usuário já participa de outra partida ou possui um desafio pendente"
            else:
                text = "Você já participa de uma partida ou possui um desafio pendente"
            await message.channel.send(embed=self._make_embed("🃏 Truco", text, ok=False))
            return True

        if not await self._truco_require_dm_for_players([challenger.id, opponent.id], channel=message.channel, guild=guild):
            await self._game_sessions.release(game.session_id)
            if changefate_forced:
                self._release_changefate_golden_reservation(guild.id, challenger.id, game.session_id)
            return True

        game.race_effect_marker = (
            self._race_effect_marker(guild.id, challenger.id, "midas")
            if self._truco_is_golden(game) and self._race_is(guild.id, challenger.id, "sortudo")
            else ""
        )
        self._truco_register_game(game)
        view = TrucoChallengeView(self, game)
        try:
            game.challenge_message = await message.channel.send(view=view)
        except Exception:
            game.finished = True
            await self._truco_release_game(game)
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Não consegui publicar o desafio agora\nTente novamente", ok=False))
            return True
        if note:
            try:
                await message.channel.send(note)
            except Exception:
                pass
        return True


    async def _handle_truco_decline(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        if interaction.user.id != game.players_order[1]:
            guild = self.bot.get_guild(game.guild_id)
            target = self._truco_member_mention(guild, game.players_order[1])
            text = (
                f"Você enviou este desafio\nAguarde {target} responder"
                if interaction.user.id == game.players_order[0]
                else f"Somente {target} pode responder a este desafio"
            )
            await interaction.response.send_message(text, ephemeral=True)
            return

        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or game.accepted or game.status != "invite" or not self._truco_is_current_game(game):
                await interaction.response.send_message("Este desafio já foi encerrado", ephemeral=True)
                return
            game.finished = True
            await self._truco_release_game(game)

        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# Truco recusado\n{interaction.user.mention} não aceitou o desafio"),
            accent_color=discord.Color.red(),
        ))
        await self._truco_update_interaction_message(interaction, view=closed)

    async def _handle_truco_accept(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        if interaction.user.id != game.players_order[1]:
            guild = self.bot.get_guild(game.guild_id)
            target = self._truco_member_mention(guild, game.players_order[1])
            text = (
                f"Você enviou este desafio\nAguarde {target} responder"
                if interaction.user.id == game.players_order[0]
                else f"Somente {target} pode responder a este desafio"
            )
            await interaction.response.send_message(text, ephemeral=True)
            return

        ok, _current, note = await self._ensure_action_chips(game.guild_id, interaction.user.id, TRUCO_ENTRY)
        if not ok:
            await interaction.response.send_message(note or "Você não tem saldo suficiente para aceitar", ephemeral=True)
            return
        if self._needs_negative_confirmation(game.guild_id, interaction.user.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_ephemeral(
                interaction,
                game.guild_id,
                interaction.user.id,
                TRUCO_ENTRY,
                title="⚠️ Confirmar entrada",
            )
            if not confirmed:
                return

        guild = interaction.guild
        if guild is not None and not await self._truco_require_dm_for_players(list(game.players), interaction=interaction, guild=guild):
            return

        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or game.accepted or game.status != "invite" or not self._truco_is_current_game(game):
                if interaction.response.is_done():
                    await interaction.followup.send("Este desafio já foi encerrado", ephemeral=True)
                else:
                    await interaction.response.send_message("Este desafio já foi encerrado", ephemeral=True)
                return

            challenger_id = int(game.players_order[0])
            opponent_id = int(game.players_order[1])
            for user_id, message_text in (
                (challenger_id, "O desafiante não possui saldo suficiente para começar"),
                (opponent_id, "Você não possui saldo suficiente para começar"),
            ):
                can_pay, _balance, _note = await self._ensure_action_chips(game.guild_id, user_id, TRUCO_ENTRY)
                if not can_pay:
                    if interaction.response.is_done():
                        await interaction.followup.send(message_text, ephemeral=True)
                    else:
                        await interaction.response.send_message(message_text, ephemeral=True)
                    return

            reservation = await self._game_sessions.activate(
                session_id=game.session_id,
                user_ids=set(game.players),
                ttl=_TRUCO_ACTION_TIMEOUT + 90.0,
            )
            if not reservation.ok:
                if reservation.code == "guild_full":
                    text = (
                        f"Este servidor já possui {MAX_ACTIVE_GAME_USERS_PER_GUILD} jogadores "
                        "em partidas de Truco"
                    )
                    if interaction.response.is_done():
                        await interaction.followup.send(text, ephemeral=True)
                    else:
                        await interaction.response.send_message(text, ephemeral=True)
                    return

                game.finished = True
                await self._truco_release_game(game)
                closed = discord.ui.LayoutView(timeout=None)
                closed.add_item(discord.ui.Container(
                    discord.ui.TextDisplay("# Desafio encerrado\nUm dos jogadores não está mais disponível para esta partida"),
                    accent_color=discord.Color.red(),
                ))
                await self._truco_update_interaction_message(interaction, view=closed)
                return

            consumed_entries: list[tuple[int, int, int, int, float]] = []
            for user_id in (challenger_id, opponent_id):
                normal_part, bonus_part, temporary_part, temporary_expiry = self._truco_entry_refund_parts(
                    game.guild_id,
                    user_id,
                    TRUCO_ENTRY,
                )
                paid, _balance, _payment_note = await self._try_consume_chips(
                    game.guild_id,
                    user_id,
                    TRUCO_ENTRY,
                    reason="Entrada no truco",
                )
                if not paid:
                    if consumed_entries:
                        await self._truco_refund_consumed_entries(game.guild_id, consumed_entries)
                    game.finished = True
                    await self._truco_release_game(game)
                    closed = discord.ui.LayoutView(timeout=None)
                    closed.add_item(discord.ui.Container(
                        discord.ui.TextDisplay("# Desafio encerrado\nNão foi possível cobrar as entradas\nNenhuma ficha foi perdida"),
                        accent_color=discord.Color.red(),
                    ))
                    await self._truco_update_interaction_message(interaction, view=closed)
                    return
                consumed_entries.append((user_id, normal_part, bonus_part, temporary_part, temporary_expiry))

            game.entry_refunds = consumed_entries
            period, period_key = self._race_period_info()
            game.entry_spend = {
                int(user_id): {
                    "chips": int(normal_part),
                    "bonus": int(bonus_part),
                    "temporary_bonus": int(temporary_part),
                    "_temporary_bonus_expires_at": float(temporary_expiry),
                    "_race_period": period,
                    "_race_period_key": period_key,
                }
                for user_id, normal_part, bonus_part, temporary_part, temporary_expiry in consumed_entries
            }
            if game.changefate_forced:
                game.changefate_consumed = await self._consume_changefate_golden(
                    game.guild_id,
                    challenger_id,
                    reservation_token=game.changefate_token,
                )
                if not game.changefate_consumed:
                    game.changefate_forced = False
                    game.variant = "normal"
                    game.race_effect_marker = ""
            game.accepted = True
            game.status = "starting"
            game.race_interactions[interaction.user.id] = interaction

        started = discord.ui.LayoutView(timeout=None)
        started.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {self._truco_title_text(game)}\nDesafio aceito\nDistribuindo as cartas..."),
            accent_color=self._truco_accent_color(game),
        ))
        await self._truco_update_interaction_message(interaction, view=started)
        await self._send_race_lobby_feedback(interaction, game.guild_id, interaction.user.id, "Entrada confirmada")
        await self._start_truco_game(game)

    async def _start_truco_game(self, game: TrucoGame):
        self._truco_touch_runtime(game)
        await self._game_sessions.touch(game.session_id, ttl=_TRUCO_ACTION_TIMEOUT + 90.0)
        try:
            channel = self.bot.get_channel(game.channel_id)
            if channel is None:
                return await self._truco_abort_game_start(game, notice="Não consegui abrir a mesa do truco agora\nAs entradas foram devolvidas")
            deck = self._truco_create_deck()
            game.vira = deck.pop(0)
            game.manilha_rank = self._truco_manilha_rank(game.vira[0])
            for user_id in game.players:
                game.hands[user_id] = [deck.pop(0) for _ in range(3)]
            game.status = "active"
            game.status_text = "Distribuindo as cartas..."
            game.status_message = game.challenge_message
            if game.status_message is not None:
                edited = await self._truco_safe_edit(
                    game.status_message,
                    embed=None,
                    view=TrucoTableView(self, game),
                    content=None,
                )
                if not edited:
                    game.status_message = None
            if game.status_message is None:
                game.status_message = await channel.send(view=TrucoTableView(self, game))
            await asyncio.sleep(0.8)
            game.status_text = "Revelando a vira..."
            await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
            await asyncio.sleep(0.8)
            dm_failed = []
            for user_id in game.players:
                self._truco_touch_runtime(game)
                ok_dm = await self._truco_send_hand_dm(game, user_id)
                if not ok_dm:
                    dm_failed.append(user_id)
            if dm_failed:
                guild = self.bot.get_guild(game.guild_id)
                failed_mentions = ", ".join(self._truco_member_mention(guild, user_id) for user_id in dm_failed)
                return await self._truco_abort_game_start(
                    game,
                    notice=f"Não consegui enviar mensagem direta para {failed_mentions}\nHabilitem as mensagens diretas do servidor e tentem novamente\nAs entradas foram devolvidas",
                )
            await self._truco_show_turn(game)
            game.entry_refunds = []
            return True
        except Exception:
            return await self._truco_abort_game_start(game, notice="Ocorreu uma falha ao iniciar o truco\nAs entradas foram devolvidas automaticamente")

    async def _truco_send_hand_dm(self, game: TrucoGame, player_id: int, quiet: bool = False) -> bool:
        try:
            user = self.bot.get_user(int(player_id)) or await self.bot.fetch_user(int(player_id))
        except Exception:
            return False
        view = TrucoHandView(self, game, int(player_id))
        old = game.dm_messages.get(int(player_id))
        if old is not None:
            ok = await self._truco_safe_edit(old, embed=None, view=view, content=None)
            if ok:
                return True
            game.dm_messages.pop(int(player_id), None)
        try:
            msg = await user.send(view=view)
            game.dm_messages[int(player_id)] = msg
            return True
        except Exception:
            if not quiet:
                game.dm_messages.pop(int(player_id), None)
            return False

    async def _handle_truco_show_hand(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Você não participa desta partida", ephemeral=True)
            return
        sent_dm = await self._truco_send_hand_dm(game, interaction.user.id)
        if sent_dm:
            await interaction.response.send_message("Sua mão foi atualizada na DM", ephemeral=True)
            return
        await interaction.response.send_message("Não consegui te enviar mensagem direta\nHabilite as mensagens diretas do servidor e tente novamente", ephemeral=True)

    async def _handle_truco_play_card(self, interaction: discord.Interaction, game: TrucoGame, player_id: int, card_index: int):
        self._truco_touch_runtime(game)
        if interaction.user.id != player_id:
            await interaction.response.send_message("Esta mão pertence a outro jogador", ephemeral=True)
            return

        game_key = self._truco_game_runtime_key(game)
        inflight_marker = (game_key, int(player_id))
        inflight = getattr(self, "_truco_play_inflight", None)
        if inflight is not None and inflight_marker in inflight:
            await interaction.response.send_message("Sua jogada já está sendo processada", ephemeral=True)
            return

        lock = self._truco_get_play_lock(game)
        if inflight is not None:
            inflight.add(inflight_marker)
        try:
            async with lock:
                if game.finished or game.status != "active" or not self._truco_is_current_game(game):
                    await interaction.response.send_message("Não é possível jogar uma carta neste momento", ephemeral=True)
                    return
                if player_id != game.turn_id:
                    await interaction.response.send_message("Ainda não é a sua vez", ephemeral=True)
                    return
                if player_id in game.cards_on_table:
                    await interaction.response.send_message("Você já jogou sua carta nesta vaza", ephemeral=True)
                    return
                hand = game.hands.get(player_id, [])
                if card_index < 0 or card_index >= len(hand):
                    await interaction.response.send_message("Essa carta não está mais disponível", ephemeral=True)
                    return
                card = hand.pop(card_index)
                guild = self.bot.get_guild(game.guild_id)
                game.status_text = f"{self._truco_member_mention(guild, player_id)} jogou uma carta"
                game.cards_on_table[player_id] = card
                await self._game_sessions.touch(game.session_id, ttl=_TRUCO_ACTION_TIMEOUT + 90.0)
                if not interaction.response.is_done():
                    await interaction.response.defer()
                try:
                    if interaction.message is not None:
                        await self._truco_safe_edit(interaction.message, embed=None, view=TrucoHandView(self, game, player_id), content=None)
                except Exception:
                    pass
                await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
                await self._truco_refresh_private_views(game)
                await asyncio.sleep(0.8)
                if len(game.cards_on_table) < 2:
                    for user_id in game.players_order:
                        if user_id not in game.cards_on_table:
                            game.turn_id = user_id
                            break
                    await self._truco_show_turn(game)
                    return
                await self._truco_resolve_round(game)
        finally:
            if inflight is not None:
                inflight.discard(inflight_marker)

    def _truco_requesting_team(self, game: TrucoGame) -> int | None:
        return self._truco_team_index(game, game.pending_raise_by or 0)

    def _truco_can_answer_raise(self, game: TrucoGame, player_id: int) -> bool:
        if game.status != "awaiting_raise_response" or not game.pending_raise_by:
            return False
        req_team = self._truco_team_index(game, game.pending_raise_by)
        return self._truco_team_index(game, player_id) != req_team

    async def _handle_truco_raise(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Você não participa desta partida", ephemeral=True)
            return
        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or not self._truco_is_current_game(game):
                await interaction.response.send_message("O jogo já terminou", ephemeral=True)
                return
            if game.status == "awaiting_raise_response":
                if not self._truco_can_answer_raise(game, interaction.user.id):
                    await interaction.response.send_message("Aguardando o adversário responder ao aumento atual", ephemeral=True)
                    return
                base = game.pending_raise_to or game.level
            else:
                if game.status != "active" or interaction.user.id != game.turn_id:
                    await interaction.response.send_message("Você só pode pedir aumento na sua vez", ephemeral=True)
                    return
                base = game.level
            next_level = self._truco_next_raise_level(base)
            if not next_level:
                await interaction.response.send_message("Esse jogo já chegou no máximo", ephemeral=True)
                return
            requester_team = self._truco_team_index(game, interaction.user.id)
            target_contribution = self._truco_target_contrib(game, next_level)
            requester_id = int(game.teams[requester_team][0])
            delta = max(0, target_contribution - int(game.contribution.get(requester_id, TRUCO_ENTRY)))
            ok, _current, note = await self._ensure_action_chips(game.guild_id, requester_id, delta)
            if not ok:
                await interaction.response.send_message(note or "Você não pode subir o jogo agora", ephemeral=True)
                return
            if delta > 0 and self._needs_negative_confirmation(game.guild_id, requester_id, delta):
                confirmed = await self._confirm_negative_ephemeral(
                    interaction,
                    game.guild_id,
                    requester_id,
                    delta,
                )
                if not confirmed:
                    return
            guild = self.bot.get_guild(game.guild_id)
            game.status = "awaiting_raise_response"
            game.pending_raise_by = interaction.user.id
            game.pending_raise_to = next_level
            game.status_text = f"{self._truco_member_mention(guild, interaction.user.id)} pediu **{_TRUCO_RAISE_NAMES[next_level]}**"
            await self._game_sessions.touch(game.session_id, ttl=_TRUCO_ACTION_TIMEOUT + 90.0)
        if interaction.response.is_done():
            await interaction.followup.send(f"Pedido de {_TRUCO_RAISE_NAMES[next_level]} enviado", ephemeral=True)
        else:
            await interaction.response.send_message(f"Pedido de {_TRUCO_RAISE_NAMES[next_level]} enviado", ephemeral=True)
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    async def _handle_truco_accept_raise(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or not self._truco_is_current_game(game):
                await interaction.response.send_message("O jogo já terminou", ephemeral=True)
                return
            if game.status != "awaiting_raise_response" or not game.pending_raise_to or not game.pending_raise_by:
                await interaction.response.send_message("Não há aumento pendente agora", ephemeral=True)
                return
            if not self._truco_can_answer_raise(game, interaction.user.id):
                await interaction.response.send_message("A resposta desse aumento não é sua", ephemeral=True)
                return
            target_level = game.pending_raise_to
            target_contribution = self._truco_target_contrib(game, target_level)
            deltas = {
                user_id: max(0, target_contribution - int(game.contribution.get(user_id, TRUCO_ENTRY)))
                for user_id in game.players
            }
            for user_id, delta in deltas.items():
                ok, _current, note = await self._ensure_action_chips(game.guild_id, user_id, delta)
                if not ok:
                    await interaction.response.send_message(note or "Um dos jogadores não consegue bancar o aumento agora", ephemeral=True)
                    return
            responder_delta = int(deltas.get(interaction.user.id, 0) or 0)
            if responder_delta > 0 and self._needs_negative_confirmation(game.guild_id, interaction.user.id, responder_delta):
                confirmed = await self._confirm_negative_ephemeral(
                    interaction,
                    game.guild_id,
                    interaction.user.id,
                    responder_delta,
                )
                if not confirmed:
                    return
            consumed: list[tuple[int, int, int, int, float]] = []
            for user_id, delta in deltas.items():
                if delta <= 0:
                    continue
                normal_part, bonus_part, temporary_part, temporary_expiry = self._truco_entry_refund_parts(
                    game.guild_id,
                    user_id,
                    delta,
                )
                paid, _balance, _note = await self._try_consume_chips(game.guild_id, user_id, delta, reason="Aumento no truco")
                if not paid:
                    if consumed:
                        await self._truco_refund_consumed_entries(game.guild_id, consumed)
                    if interaction.response.is_done():
                        await interaction.followup.send("Não foi possível cobrar o aumento\nNada foi alterado", ephemeral=True)
                    else:
                        await interaction.response.send_message("Não foi possível cobrar o aumento\nNada foi alterado", ephemeral=True)
                    return
                consumed.append((user_id, normal_part, bonus_part, temporary_part, temporary_expiry))
            for user_id, normal_part, bonus_part, temporary_part, temporary_expiry in consumed:
                current_spend = dict(game.entry_spend.get(int(user_id)) or {})
                current_spend["chips"] = int(current_spend.get("chips", 0) or 0) + int(normal_part)
                current_spend["bonus"] = int(current_spend.get("bonus", 0) or 0) + int(bonus_part)
                current_spend["temporary_bonus"] = int(current_spend.get("temporary_bonus", 0) or 0) + int(temporary_part)
                if temporary_part > 0:
                    current_spend["_temporary_bonus_expires_at"] = max(
                        float(current_spend.get("_temporary_bonus_expires_at", 0.0) or 0.0),
                        float(temporary_expiry),
                    )
                game.entry_spend[int(user_id)] = current_spend
            for user_id, delta in deltas.items():
                game.contribution[user_id] = int(game.contribution.get(user_id, TRUCO_ENTRY)) + delta
            game.level = target_level
            game.pot = self._truco_target_pot(game, target_level)
            game.status = "active"
            game.pending_raise_by = None
            game.pending_raise_to = None
            guild = self.bot.get_guild(game.guild_id)
            game.status_text = f"{self._truco_member_mention(guild, interaction.user.id)} aceitou\nO jogo agora vale **{target_level}**"
            await self._game_sessions.touch(game.session_id, ttl=_TRUCO_ACTION_TIMEOUT + 90.0)
        if interaction.response.is_done():
            await interaction.followup.send("Aumento aceito", ephemeral=True)
        else:
            await interaction.response.send_message("Aumento aceito", ephemeral=True)
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    async def _handle_truco_run(self, interaction: discord.Interaction, game: TrucoGame):
        self._truco_touch_runtime(game)
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Você não participa desta partida", ephemeral=True)
            return
        lock = self._truco_get_play_lock(game)
        async with lock:
            if game.finished or not self._truco_is_current_game(game):
                await interaction.response.send_message("O jogo já terminou", ephemeral=True)
                return
            loser_team = self._truco_team_index(game, interaction.user.id)
            winner_team = 1 - loser_team
            if interaction.response.is_done():
                await interaction.followup.send("Você correu da partida", ephemeral=True)
            else:
                await interaction.response.send_message("Você correu da partida", ephemeral=True)
            await self._finish_truco_game(game, winner_team=winner_team, loser_id=interaction.user.id, reason="correu")
