from __future__ import annotations

import asyncio
import logging
import random

import discord

from config import OFF_COLOR


ROLETA2_COST = 15
ROLETA2_MAX_PRIZE = 200

SLOT_BANANA = "banana"
SLOT_FRAMBOESA = "framboesa"
SLOT_CEREJA = "cereja"
SLOT_BAR = "bar"
SLOT_SEVEN = "seven"
SLOT_SPINNING = "spinning"

SLOT_EMOJIS = {
    SLOT_BANANA: "<:slot_banana:1543757649911353404>",
    SLOT_FRAMBOESA: "<:slot_framboesa:1543757628520271964>",
    SLOT_CEREJA: "<:slot_cereja:1543757610925162516>",
    SLOT_BAR: "<:slot_bar:1543757593078403072>",
    SLOT_SEVEN: "<:slot_7:1543757577496821800>",
    SLOT_SPINNING: "<a:slot_girando:1543757558374994021>",
}

SLOT_SYMBOLS = (SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA, SLOT_BAR, SLOT_SEVEN)
SLOT_FRUITS = (SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA)
SLOT_SYMBOL_WEIGHTS = (30, 27, 25, 12, 6)

# Mil partes deixam as probabilidades auditáveis e evitam que a grade sorteada
# acidentalmente defina a raridade. Déjà vu mantém 10% absolutos por giro.
ROLETA2_OUTCOME_WEIGHTS = (
    ("sete_pecados", 1),
    ("jackpot", 19),
    ("bar_triplo", 15),
    ("bar_abriu_as_7", 30),
    ("deja_vu", 100),
    ("banana_split", 75),
    ("escorredio", 60),
    ("colheita", 90),
    ("setes_espalhados", 50),
    ("faltou_um_sete", 120),
    ("loss", 440),
)

ROLETA2_PAYOUTS = {
    "sete_pecados": (200, 0),
    "jackpot": (100, 0),
    "bar_triplo": (80, 0),
    "bar_abriu_as_7": (40, 0),
    "deja_vu": (10, 0),
    "banana_split": (10, 10),
    "escorredio": (18, 0),
    "setes_espalhados": (15, 0),
    "faltou_um_sete": (5, 0),
    "loss": (0, 0),
}

ROLETA2_COLUMN_DELAYS = (0.72, 0.68, 0.66)

_SLOT_LINES = (
    ((0, 0), (0, 1), (0, 2)),
    ((1, 0), (1, 1), (1, 2)),
    ((2, 0), (2, 1), (2, 2)),
    ((0, 0), (1, 1), (2, 2)),
    ((0, 2), (1, 1), (2, 0)),
)
_SLOT_DIAGONALS = _SLOT_LINES[-2:]


def _slot_line_values(grid: list[list[str]], line) -> list[str]:
    return [grid[row][column] for row, column in line]


def _slot_set_line(grid: list[list[str]], line, values: list[str] | tuple[str, ...]) -> None:
    for (row, column), value in zip(line, values):
        grid[row][column] = str(value)


def _slot_random_symbol(rng: random.Random, *, include_seven: bool = True) -> str:
    symbols = SLOT_SYMBOLS if include_seven else SLOT_SYMBOLS[:-1]
    weights = SLOT_SYMBOL_WEIGHTS if include_seven else SLOT_SYMBOL_WEIGHTS[:-1]
    return str(rng.choices(symbols, weights=weights, k=1)[0])


def _slot_random_grid(rng: random.Random, *, include_seven: bool = True) -> list[list[str]]:
    return [
        [_slot_random_symbol(rng, include_seven=include_seven) for _ in range(3)]
        for _ in range(3)
    ]


def _slots_has_escorredio(grid: list[list[str]]) -> bool:
    return any(_slot_line_values(grid, line) == [SLOT_BANANA] * 3 for line in _SLOT_DIAGONALS)


def _slots_matching_kinds(grid: list[list[str]]) -> set[str]:
    flat = [cell for row in grid for cell in row]
    seven_count = flat.count(SLOT_SEVEN)
    lines = [_slot_line_values(grid, line) for line in _SLOT_LINES]

    if seven_count == 7:
        return {"sete_pecados"}

    matches: set[str] = set()
    has_jackpot = [SLOT_SEVEN] * 3 in lines
    if has_jackpot:
        matches.add("jackpot")
    if [SLOT_BAR] * 3 in lines:
        matches.add("bar_triplo")
    if list(grid[1]) == [SLOT_BAR, SLOT_SEVEN, SLOT_BAR]:
        matches.add("bar_abriu_as_7")
    if all(grid[row][0] == grid[row][2] for row in range(3)):
        matches.add("deja_vu")
    if [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA] in lines:
        matches.add("banana_split")
    if any(len(set(row)) == 1 and row[0] in SLOT_FRUITS for row in grid):
        matches.add("colheita")
    if not has_jackpot and seven_count >= 3:
        matches.add("setes_espalhados")
    if not matches and seven_count == 2:
        matches.add("faltou_um_sete")
    return matches


def _slots_detect_kind(grid: list[list[str]]) -> str:
    matches = _slots_matching_kinds(grid)
    for kind in (
        "sete_pecados",
        "jackpot",
        "bar_triplo",
        "bar_abriu_as_7",
        "deja_vu",
        "banana_split",
        "colheita",
        "setes_espalhados",
        "faltou_um_sete",
    ):
        if kind in matches:
            return kind
    return "loss"


def _slots_fallback_grid(kind: str) -> list[list[str]]:
    fallbacks = {
        "loss": [
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
            [SLOT_CEREJA, SLOT_BAR, SLOT_FRAMBOESA],
            [SLOT_FRAMBOESA, SLOT_CEREJA, SLOT_BANANA],
        ],
        "faltou_um_sete": [
            [SLOT_SEVEN, SLOT_BANANA, SLOT_CEREJA],
            [SLOT_FRAMBOESA, SLOT_BAR, SLOT_BANANA],
            [SLOT_CEREJA, SLOT_FRAMBOESA, SLOT_SEVEN],
        ],
        "setes_espalhados": [
            [SLOT_SEVEN, SLOT_BANANA, SLOT_SEVEN],
            [SLOT_FRAMBOESA, SLOT_SEVEN, SLOT_CEREJA],
            [SLOT_BANANA, SLOT_BAR, SLOT_FRAMBOESA],
        ],
        "colheita": [
            [SLOT_CEREJA, SLOT_CEREJA, SLOT_CEREJA],
            [SLOT_BANANA, SLOT_BAR, SLOT_FRAMBOESA],
            [SLOT_FRAMBOESA, SLOT_BANANA, SLOT_BAR],
        ],
        "banana_split": [
            [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA],
            [SLOT_FRAMBOESA, SLOT_BAR, SLOT_CEREJA],
            [SLOT_CEREJA, SLOT_FRAMBOESA, SLOT_BAR],
        ],
        "deja_vu": [
            [SLOT_BANANA, SLOT_BAR, SLOT_BANANA],
            [SLOT_FRAMBOESA, SLOT_BANANA, SLOT_FRAMBOESA],
            [SLOT_CEREJA, SLOT_FRAMBOESA, SLOT_CEREJA],
        ],
        "bar_abriu_as_7": [
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
            [SLOT_BAR, SLOT_SEVEN, SLOT_BAR],
            [SLOT_CEREJA, SLOT_BANANA, SLOT_FRAMBOESA],
        ],
        "bar_triplo": [
            [SLOT_BAR, SLOT_BAR, SLOT_BAR],
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
            [SLOT_CEREJA, SLOT_BANANA, SLOT_FRAMBOESA],
        ],
        "jackpot": [
            [SLOT_SEVEN, SLOT_SEVEN, SLOT_SEVEN],
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
            [SLOT_CEREJA, SLOT_BAR, SLOT_BANANA],
        ],
        "sete_pecados": [
            [SLOT_SEVEN, SLOT_SEVEN, SLOT_SEVEN],
            [SLOT_SEVEN, SLOT_BAR, SLOT_SEVEN],
            [SLOT_SEVEN, SLOT_BANANA, SLOT_SEVEN],
        ],
    }
    return [list(row) for row in fallbacks.get(kind, fallbacks["loss"])]


def _slots_build_candidate(kind: str, rng: random.Random) -> list[list[str]]:
    if kind == "sete_pecados":
        flat = [SLOT_SEVEN] * 7 + [rng.choice(SLOT_FRUITS), SLOT_BAR]
        rng.shuffle(flat)
        return [flat[index:index + 3] for index in range(0, 9, 3)]

    if kind in {"jackpot", "bar_triplo", "banana_split", "colheita"}:
        grid = _slot_random_grid(rng, include_seven=False)
        line = rng.choice(_SLOT_LINES)
        if kind == "jackpot":
            values = [SLOT_SEVEN] * 3
        elif kind == "bar_triplo":
            values = [SLOT_BAR] * 3
        elif kind == "banana_split":
            values = [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA]
        else:
            values = [rng.choice(SLOT_FRUITS)] * 3
            # Colheita usa linhas horizontais; bananas diagonais pertencem ao
            # resultado Escorredio.
            line = rng.choice(_SLOT_LINES[:3])
        _slot_set_line(grid, line, values)
        return grid

    if kind == "bar_abriu_as_7":
        grid = _slot_random_grid(rng, include_seven=False)
        grid[1] = [SLOT_BAR, SLOT_SEVEN, SLOT_BAR]
        return grid

    if kind == "deja_vu":
        left = [_slot_random_symbol(rng, include_seven=False) for _ in range(3)]
        middle = [_slot_random_symbol(rng, include_seven=False) for _ in range(3)]
        return [[left[row], middle[row], left[row]] for row in range(3)]

    if kind in {"faltou_um_sete", "setes_espalhados"}:
        count = 2 if kind == "faltou_um_sete" else rng.choice((3, 3, 3, 4))
        flat = [_slot_random_symbol(rng, include_seven=False) for _ in range(9)]
        for position in rng.sample(range(9), count):
            flat[position] = SLOT_SEVEN
        return [flat[index:index + 3] for index in range(0, 9, 3)]

    return _slot_random_grid(rng, include_seven=True)


def _slots_generate_grid(kind: str, rng: random.Random) -> list[list[str]]:
    for _ in range(512):
        grid = _slots_build_candidate(kind, rng)
        expected_matches = set() if kind == "loss" else {kind}
        if _slots_matching_kinds(grid) != expected_matches:
            continue
        if kind == "loss" and _slots_has_escorredio(grid):
            continue
        return grid
    return _slots_fallback_grid(kind)


def _slots_generate_escorredio(rng: random.Random) -> tuple[list[list[str]], list[list[str]], int]:
    for _ in range(512):
        preview = _slot_random_grid(rng, include_seven=False)
        _slot_set_line(preview, rng.choice(_SLOT_DIAGONALS), [SLOT_BANANA] * 3)
        if _slots_detect_kind(preview) == "loss" and _slots_has_escorredio(preview):
            return preview, _slots_generate_grid("loss", rng), rng.randrange(3)
    preview = [
        [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
        [SLOT_BAR, SLOT_BANANA, SLOT_FRAMBOESA],
        [SLOT_CEREJA, SLOT_BAR, SLOT_BANANA],
    ]
    return preview, _slots_generate_grid("loss", rng), 2


def _slots_colheita_fruit(grid: list[list[str]]) -> str:
    for row in grid:
        if len(set(row)) == 1 and row[0] in SLOT_FRUITS:
            return str(row[0])
    return SLOT_BANANA


class GincanaSlotsMixin:
    def _roll_roleta2_outcome(self) -> dict[str, object]:
        kinds, weights = zip(*ROLETA2_OUTCOME_WEIGHTS)
        kind = str(random.choices(kinds, weights=weights, k=1)[0])
        rng = random
        preview_grid: list[list[str]] | None = None
        slip_column: int | None = None
        if kind == "escorredio":
            preview_grid, grid, slip_column = _slots_generate_escorredio(rng)
        else:
            grid = _slots_generate_grid(kind, rng)

        normal_payout, bonus_payout = ROLETA2_PAYOUTS.get(kind, (0, 0))
        if kind == "colheita":
            fruit = _slots_colheita_fruit(grid)
            if fruit == SLOT_FRAMBOESA:
                normal_payout, bonus_payout = 0, 15
                summary = "As framboesas transformaram a Colheita em fichas bônus"
            elif fruit == SLOT_CEREJA:
                normal_payout, bonus_payout = 20, 0
                summary = "As cerejas duplicaram o valor base da Colheita"
            else:
                normal_payout, bonus_payout = 15, 0
                summary = "As bananas fecharam uma Colheita"
        else:
            summaries = {
                "sete_pecados": "Sete dos nove espaços vieram com 7",
                "jackpot": "Três 7 fecharam uma linha",
                "bar_triplo": "Três BAR fecharam uma linha",
                "bar_abriu_as_7": "O BAR abriu exatamente às 7",
                "deja_vu": "A primeira e a terceira coluna repetiram o mesmo resultado",
                "banana_split": "As bananas se dividiram ao redor da cereja",
                "escorredio": "As bananas fizeram uma coluna escorregar e girar outra vez",
                "setes_espalhados": "Os 7 apareceram espalhados pela máquina",
                "faltou_um_sete": "Dois 7 apareceram, mas o terceiro não veio",
                "loss": "Nenhum resultado foi formado",
            }
            summary = summaries.get(kind, "Resultado da roleta")

        titles = {
            "sete_pecados": "Sete pecados",
            "jackpot": "Jackpot 777",
            "bar_triplo": "BAR triplo",
            "bar_abriu_as_7": "Bar abriu às 7",
            "deja_vu": "Déjà vu",
            "banana_split": "Banana split",
            "escorredio": "Escorredio",
            "colheita": "Colheita",
            "setes_espalhados": "Setes espalhados",
            "faltou_um_sete": "Faltou um sete",
            "loss": "Não veio nada",
        }
        return {
            "kind": kind,
            "title": titles.get(kind, "Roleta 2"),
            "summary": summary,
            "grid": grid,
            "preview_grid": preview_grid,
            "slip_column": slip_column,
            "normal_payout": int(normal_payout),
            "bonus_payout": int(bonus_payout),
            "success": kind != "loss",
            "premium": kind in {"sete_pecados", "jackpot", "bar_triplo", "bar_abriu_as_7"},
            "partial": kind in {"deja_vu", "faltou_um_sete"},
            "jackpot": kind in {"sete_pecados", "jackpot"},
        }

    def _render_roleta2_board(self, grid: list[list[str]]) -> str:
        return "\n".join(
            " ".join(SLOT_EMOJIS.get(str(cell), SLOT_EMOJIS[SLOT_SPINNING]) for cell in row)
            for row in grid
        )

    def _roleta2_spinning_grid(self) -> list[list[str]]:
        return [[SLOT_SPINNING for _ in range(3)] for _ in range(3)]

    def _roleta2_display_grid(
        self,
        target_grid: list[list[str]],
        *,
        stopped_columns: set[int],
        spinning_column: int | None = None,
    ) -> list[list[str]]:
        return [
            [
                (
                    SLOT_SPINNING
                    if column == spinning_column or column not in stopped_columns
                    else str(target_grid[row][column])
                )
                for column in range(3)
            ]
            for row in range(3)
        ]

    def _roleta2_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
        available = max(0, int(state.get("available", 0) or 0))
        if is_staff and available <= 0:
            return "Os giros compartilhados com cartas acabaram, mas como você é staff ainda pode girar"
        giro_text = "giro" if available == 1 else "giros"
        verb = "Resta" if available == 1 else "Restam"
        reset = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
        return f"{verb} {available} {giro_text} compartilhados com cartas • Reset em {reset}"

    def _roleta2_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
        total = max(1, int(state.get("total", 1) or 1))
        wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
        return (
            "🎰 Sem giros por agora",
            f"Seus {total} giros compartilhados com cartas acabaram\nReset em **{wait_text}**",
        )

    def _make_roleta2_spin_view(
        self,
        board: str,
        *,
        balance_text: str,
        footer_text: str,
        paid_entry: int,
        title: str = "🎰 Girando...",
    ) -> discord.ui.LayoutView:
        return self._make_game_layout_view(
            title,
            details=[
                f"**Entrada:** {self._format_game_entry_value(paid_entry)}",
                f"**Prêmio máximo:** {self._chip_text(ROLETA2_MAX_PRIZE, kind='gain')}",
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=discord.Color.from_rgb(255, 190, 46),
        )

    def _make_roleta2_result_view(
        self,
        *,
        title: str,
        summary: str,
        board: str,
        balance_text: str,
        success: bool,
        premium: bool,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
    ) -> discord.ui.LayoutView:
        color = (
            discord.Color.from_rgb(255, 190, 46)
            if premium
            else (discord.Color.green() if success else discord.Color(OFF_COLOR))
        )
        return self._make_game_layout_view(
            f"🎰 {title}",
            summary=summary,
            details=[
                f"**Resultado:** {self._format_game_result_breakdown(normal_delta, bonus_delta)}",
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=color,
        )

    async def _animate_roleta2_spin(
        self,
        source_message: discord.Message,
        *,
        outcome: dict[str, object],
        balance_text: str,
        footer_text: str,
        paid_entry: int,
        spin_message: discord.Message | None = None,
        skip_event: asyncio.Event | None = None,
    ) -> tuple[discord.Message | None, list[list[str]]]:
        final_grid = [list(row) for row in outcome.get("grid", [])]
        preview_raw = outcome.get("preview_grid")
        target_grid = [list(row) for row in preview_raw] if isinstance(preview_raw, list) else final_grid
        if skip_event is not None and skip_event.is_set():
            return spin_message, final_grid

        opening_view = self._make_roleta2_spin_view(
            self._render_roleta2_board(self._roleta2_spinning_grid()),
            balance_text=balance_text,
            footer_text=footer_text,
            paid_entry=paid_entry,
        )
        spin_message = await self._render_or_replace_game_message(
            source_message,
            spin_message,
            view=opening_view,
            final=False,
            cancel_event=skip_event,
        )
        if spin_message is None or (skip_event is not None and skip_event.is_set()):
            return spin_message, final_grid

        stopped: set[int] = set()
        for column, delay in enumerate(ROLETA2_COLUMN_DELAYS[:2]):
            if not await self._wait_game_animation_delay(skip_event, delay):
                return spin_message, final_grid
            stopped.add(column)
            frame = self._roleta2_display_grid(target_grid, stopped_columns=stopped)
            frame_view = self._make_roleta2_spin_view(
                self._render_roleta2_board(frame),
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=frame_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            if skip_event is not None and skip_event.is_set():
                return spin_message, final_grid

        if not await self._wait_game_animation_delay(skip_event, ROLETA2_COLUMN_DELAYS[2]):
            return spin_message, final_grid

        slip_column = outcome.get("slip_column")
        if isinstance(preview_raw, list) and isinstance(slip_column, int):
            stopped = {0, 1, 2}
            preview_view = self._make_roleta2_spin_view(
                self._render_roleta2_board(target_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
                title="🍌 Escorredio...",
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=preview_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            if not await self._wait_game_animation_delay(skip_event, 0.42):
                return spin_message, final_grid
            slipping = self._roleta2_display_grid(
                target_grid,
                stopped_columns=stopped,
                spinning_column=max(0, min(2, int(slip_column))),
            )
            slipping_view = self._make_roleta2_spin_view(
                self._render_roleta2_board(slipping),
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
                title="🍌 Uma coluna escorregou...",
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=slipping_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, 0.76)

        return spin_message, final_grid

    async def _execute_roleta2_round(
        self,
        *,
        source_message: discord.Message,
        guild: discord.Guild,
        actor: discord.abc.User,
        footer_text: str,
        chip_note: str | None,
        session_id: str,
        skip_event: asyncio.Event | None = None,
        round_sequence: int | None = None,
        spin_message: discord.Message | None = None,
        entry_cost: int = ROLETA2_COST,
        entry_spend: dict | None = None,
    ) -> bool:
        outcome = self._roll_roleta2_outcome()
        paid_entry = self._entry_paid_amount(entry_spend, entry_cost)
        if isinstance(entry_spend, dict):
            entry_normal = max(0, int(entry_spend.get("chips", 0) or 0))
            entry_bonus = max(0, int(entry_spend.get("bonus", 0) or 0))
        else:
            entry_normal = paid_entry
            entry_bonus = 0

        try:
            spin_message, final_grid = await self._animate_roleta2_spin(
                source_message,
                outcome=outcome,
                balance_text=self._format_compact_chip_balance(guild.id, actor.id),
                footer_text=footer_text,
                paid_entry=paid_entry,
                spin_message=spin_message,
                skip_event=skip_event,
            )
        except Exception:
            logging.getLogger("gincana.roleta2").exception(
                "falha visual na animação da roleta2 | guild=%s user=%s",
                guild.id,
                actor.id,
            )
            final_grid = [list(row) for row in outcome.get("grid", [])]
        await self._release_game_animation_session(guild.id, session_id)

        if round_sequence is not None:
            await self._wait_for_game_round_commit_turn(guild.id, actor.id, round_sequence)
        async with self._game_user_state_lock(guild.id, actor.id):
            commit_start_normal, commit_start_bonus = self._current_game_chip_balances(guild.id, actor.id)
            kind = str(outcome.get("kind") or "loss")
            normal_payout = max(0, int(outcome.get("normal_payout", 0) or 0))
            bonus_payout = max(0, int(outcome.get("bonus_payout", 0) or 0))
            gross_payout = normal_payout + bonus_payout
            summary_lines: list[str] = [str(outcome.get("summary") or "").strip()]

            await self.db.add_user_game_stat(guild.id, actor.id, "roleta2_spins", 1)
            weekly_points = 2
            if bool(outcome.get("success")):
                if bool(outcome.get("jackpot")):
                    weekly_points = 12
                elif bool(outcome.get("premium")):
                    weekly_points = 8
                elif bool(outcome.get("partial")):
                    weekly_points = 3
                else:
                    weekly_points = 4
            await self._record_game_played(guild.id, actor.id, weekly_points=weekly_points)

            if normal_payout > 0:
                await self._change_user_chips(
                    guild.id,
                    actor.id,
                    normal_payout,
                    reason=f"Roleta2 · {outcome.get('title')}",
                )
            if bonus_payout > 0:
                await self._change_user_bonus_chips(
                    guild.id,
                    actor.id,
                    bonus_payout,
                    reason=f"Roleta2 · {outcome.get('title')}",
                )

            if bool(outcome.get("jackpot")):
                await self.db.add_user_game_stat(guild.id, actor.id, "roleta2_jackpots", 1)
                await self._grant_weekly_points(guild.id, actor.id, 18)
            elif bool(outcome.get("premium")):
                await self._grant_weekly_points(guild.id, actor.id, 6)

            race_won: bool | None = bool(outcome.get("success"))
            race_payout = gross_payout
            if kind == "loss":
                race_won = False
                race_payout = 0
                refund, refund_mode = await self._maybe_apply_coringa_loss_refund(
                    guild.id,
                    actor.id,
                    entry_cost,
                    chance=0.5,
                )
                if refund > 0:
                    gross_payout += int(refund)
                    if refund_mode == "joker":
                        effect_note = self._race_effect_message(
                            guild.id,
                            actor.id,
                            "joker",
                            self._skill_chip_value(refund, kind="bonus", movement="gain"),
                        )
                        summary_lines.insert(
                            0,
                            effect_note
                            or f"🃏 **Joker** · {self._skill_chip_value(refund, kind='bonus', movement='gain')}",
                        )
                    else:
                        effect_note = self._race_effect_message(
                            guild.id,
                            actor.id,
                            "redencao",
                            f"você recuperou {self._chip_text(refund, kind='gain')} da entrada",
                        )
                        summary_lines.insert(0, effect_note or f"Você recuperou {self._chip_text(refund, kind='gain')}")

            race_notes = await self._apply_new_race_result(
                guild.id,
                actor.id,
                won=race_won,
                entry_spend=entry_spend,
                payout=race_payout,
                valid=not bool(outcome.get("partial")),
                glitch_progress=True,
            )
            if race_notes:
                summary_lines = [*race_notes, *summary_lines]
            if chip_note:
                summary_lines.insert(0, chip_note)

            final_normal, final_bonus = self._current_game_chip_balances(guild.id, actor.id)
            display_normal, display_bonus = self._game_round_display_balances(
                guild.id,
                actor.id,
                round_sequence,
                current_normal=final_normal,
                current_bonus=final_bonus,
            )
            normal_result_delta = (final_normal - commit_start_normal) - entry_normal
            bonus_result_delta = (final_bonus - commit_start_bonus) - entry_bonus
            result_view = self._make_roleta2_result_view(
                title=str(outcome.get("title") or "Roleta 2"),
                summary="\n".join(line for line in summary_lines if line),
                board=self._render_roleta2_board(final_grid),
                balance_text=self._format_game_balance_values(display_normal, display_bonus),
                success=bool(outcome.get("success")),
                premium=bool(outcome.get("premium")),
                footer_text=footer_text,
                normal_delta=normal_result_delta,
                bonus_delta=bonus_result_delta,
            )
            first_game_unlocked = await self._unlock_achievement(guild.id, actor.id, "first_game")
            roulette_achievements = await self._record_roulette_achievement_result(
                guild.id,
                actor.id,
                jackpot=bool(outcome.get("jackpot")),
                lost=kind == "loss",
            )

        await self._complete_game_round_sequence(guild.id, actor.id, round_sequence)
        achievement_keys = (["first_game"] if first_game_unlocked else []) + roulette_achievements
        try:
            await self._deliver_game_result(
                source_message,
                spin_message,
                view=result_view,
                guild_id=guild.id,
                achievement_user_id=actor.id,
                achievement_keys=achievement_keys,
            )
        finally:
            await self._complete_game_round_delivery_sequence(guild.id, actor.id, round_sequence)
        return True

    async def _run_roleta2_trigger_locked(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if not self._matches_exact_trigger(message.content or "", "roleta2"):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
        debt_confirmed = False
        round_sequence: int | None = None
        footer_text = ""
        chip_note: str | None = None
        entry_spend: dict | None = None

        while True:
            needs_confirmation = False
            no_spin_state: dict[str, float | int] | None = None
            payment_error: str | None = None
            async with self._game_user_state_lock(guild.id, message.author.id):
                # A versão experimental compartilha a janela da roleta de cartas
                # para não duplicar giros e injetar fichas extras na economia.
                spin_state = await self._sync_carta_spin_window(guild.id, message.author.id)
                if int(spin_state.get("available", 0) or 0) <= 0 and not is_staff:
                    no_spin_state = spin_state
                else:
                    blessing_state = await self._sync_sortudo_blessings(guild.id, message.author.id)
                    free_spin = (
                        self._race_is(guild.id, message.author.id, "sortudo")
                        and int(blessing_state.get("charges", 0) or 0) > 0
                    )
                    needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(
                        guild.id,
                        message.author.id,
                        ROLETA2_COST,
                    )
                    if needs_negative_confirm and not debt_confirmed:
                        needs_confirmation = True
                    else:
                        can_spin, spin_state = await self._reserve_carta_spin_state(
                            guild.id,
                            message.author.id,
                            is_staff=is_staff,
                        )
                        if not can_spin:
                            no_spin_state = spin_state
                        else:
                            footer_text = self._roleta2_footer_text(state=spin_state, is_staff=is_staff)
                            if free_spin:
                                await self._consume_sortudo_blessing(guild.id, message.author.id)
                                chip_note = self._sortudo_blessing_note(
                                    guild.id,
                                    message.author.id,
                                    kind="roleta",
                                )
                                entry_spend = {"chips": 0, "bonus": 0}
                                paid = True
                            else:
                                entry_spend = self._entry_spend_parts(
                                    guild.id,
                                    message.author.id,
                                    ROLETA2_COST,
                                )
                                paid, _balance, chip_note = await self._try_consume_chips(
                                    guild.id,
                                    message.author.id,
                                    ROLETA2_COST,
                                    reason="Entrada na roleta2",
                                )
                            if needs_negative_confirm:
                                chip_note = None
                            if paid:
                                round_sequence = self._issue_game_round_sequence(
                                    guild.id,
                                    message.author.id,
                                    entry_spend=entry_spend,
                                )
                            else:
                                payment_error = chip_note or "Você não tem saldo suficiente"

            if no_spin_state is not None:
                title, description = self._roleta2_spin_message_text(no_spin_state)
                try:
                    await message.channel.send(
                        view=self._make_game_notice_view(title, description, ok=False),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass
                return True

            if needs_confirmation:
                confirmed = await self._confirm_game_negative_from_message(
                    message,
                    guild.id,
                    message.author.id,
                    ROLETA2_COST,
                    title="🎰 Confirmar aposta",
                )
                if not confirmed:
                    return True
                debt_confirmed = True
                continue

            if payment_error is not None or round_sequence is None:
                try:
                    await message.channel.send(
                        view=self._make_game_notice_view(
                            "🎰 Saldo insuficiente",
                            payment_error or "Você não tem saldo suficiente",
                            ok=False,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass
                return True
            break

        session_id = self._next_game_animation_session_id(
            guild_id=guild.id,
            kind="roleta2",
            owner_id=message.author.id,
        )
        skip_event = await self._activate_game_animation_session(
            guild.id,
            session_id,
            kind="roleta2",
            owner_id=message.author.id,
        )
        try:
            try:
                await self._unlock_and_send_achievement(
                    message.channel,
                    guild.id,
                    message.author.id,
                    "lets_go_gambling",
                )
            except Exception:
                logging.getLogger("gincana.roleta2").exception(
                    "falha ao anunciar conquista inicial da roleta2 | guild=%s user=%s",
                    guild.id,
                    message.author.id,
                )
            await self._execute_roleta2_round(
                source_message=message,
                guild=guild,
                actor=message.author,
                footer_text=footer_text,
                chip_note=chip_note,
                session_id=session_id,
                skip_event=skip_event,
                round_sequence=round_sequence,
                entry_cost=ROLETA2_COST,
                entry_spend=entry_spend,
            )
            return True
        finally:
            await self._release_game_animation_session(guild.id, session_id)
            await self._complete_game_round_sequence(guild.id, message.author.id, round_sequence)
            await self._complete_game_round_delivery_sequence(guild.id, message.author.id, round_sequence)

    async def _handle_roleta2_trigger(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if not self._matches_exact_trigger(message.content or "", "roleta2"):
            return False
        return await self._run_roleta2_trigger_locked(message)
