from __future__ import annotations

import asyncio
import logging
import random
import time

import discord

from config import OFF_COLOR


ROLETA2_COST = 15
ROLETA2_SPIN_LIMIT = 5
ROLETA2_WINDOW_SECONDS = 6 * 60 * 60
ROLETA2_DAILY_EXTRA_SPINS = 2
ROLETA2_DAILY_EXTRA_CAP = 2
ROLETA2_PROBABILITY_SCALE = 10_000
ROLETA2_DEJA_VU_CHAIN_LIMIT = 20
ROLETA2_EFFECT_CHAIN_LIMIT = 60
ROLETA2_DEJA_VU_BASE_PAYOUT = 10
ROLETA2_DEJA_VU_TO_ESCORREDIO_CHANCE = 0.25
ROLETA2_ESCORREDIO_TO_DEJA_VU_CHANCE = 0.35
ROLETA2_RESPIN_START_DELAYS = (0.66, 0.67, 0.67)
ROLETA2_EFFECT_READ_DELAY = 0.70

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
SLOT_SAFE_SYMBOL_WEIGHTS = (32, 29, 26, 13)

# A identidade visual acompanha o evento que causou o resultado. Resultados
# sem tema próprio continuam usando o saldo líquido para decidir a cor.
ROLETA2_THEME_COLORS = {
    SLOT_BANANA: 0xF2C94C,
    SLOT_FRAMBOESA: 0xC43D5C,
    SLOT_CEREJA: 0xE5484D,
    SLOT_BAR: 0x9B59B6,
    SLOT_SEVEN: 0x3498DB,
    "deja_vu": 0x4FA3B5,
}
ROLETA2_NEUTRAL_COLOR = 0x747F8D

ROLETA2_KIND_TITLE_SYMBOLS = {
    "sete_pecados": SLOT_SEVEN,
    "jackpot": SLOT_SEVEN,
    "bar_triplo": SLOT_BAR,
    "bar_abriu_as_7": SLOT_BAR,
    "banana_split": SLOT_BANANA,
    "escorredio": SLOT_BANANA,
    "setes_espalhados": SLOT_SEVEN,
    "faltou_um_sete": SLOT_SEVEN,
    "sete_solitario": SLOT_SEVEN,
}

# Pontos-base deixam todas as probabilidades auditáveis. O resultado principal
# é sorteado antes da grade, então a aparência nunca redefine sua raridade.
ROLETA2_OUTCOME_WEIGHTS = (
    ("sete_pecados", 10),
    ("jackpot", 190),
    ("bar_triplo", 150),
    ("bar_abriu_as_7", 300),
    ("deja_vu", 1_000),
    ("banana_split", 750),
    ("escorredio", 600),
    ("colheita", 900),
    ("setes_espalhados", 500),
    ("faltou_um_sete", 1_200),
    ("loss", 4_400),
)

# Variações internas também têm chances próprias. Elas são sorteadas uma única
# vez por giro e preservadas caso uma grade inválida precise ser refeita.
ROLETA2_LINE_TYPE_WEIGHTS = (
    ("horizontal", 7_500),
    ("diagonal", 2_500),
)
# Banana split preserva a proporção 75/25 já usada para orientação, mas usa
# linhas/colunas para que a descrição sempre corresponda a uma posição visível.
ROLETA2_BANANA_SPLIT_AXIS_WEIGHTS = (
    ("horizontal", 7_500),
    ("vertical", 2_500),
)
ROLETA2_COLHEITA_FRUIT_WEIGHTS = (
    (SLOT_BANANA, 4_500),
    (SLOT_FRAMBOESA, 3_500),
    (SLOT_CEREJA, 2_000),
)
ROLETA2_SCATTERED_SEVEN_WEIGHTS = (
    (3, 7_500),
    (4, 2_000),
    (5, 500),
)
ROLETA2_LOSS_SEVEN_WEIGHTS = (
    (0, 8_500),
    (1, 1_500),
)

ROLETA2_LOSS_SUMMARIES = {
    "Não veio nada": "Nenhum resultado foi formado",
    "Nenhuma combinação": "Os símbolos pararam sem formar uma combinação",
    "Você ganhou... nada!": "Uau parece que não veio nada, incrível",
    "Foi quase hein": "Dois símbolos combinaram",
    "7 solitário": "Veio apenas um 7",
}

ROLETA2_PAYOUTS = {
    "sete_pecados": (200, 0),
    "jackpot": (100, 0),
    "bar_triplo": (80, 0),
    "bar_abriu_as_7": (40, 0),
    "deja_vu": (0, 10),
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
_SLOT_COLUMNS = (
    ((0, 0), (1, 0), (2, 0)),
    ((0, 1), (1, 1), (2, 1)),
    ((0, 2), (1, 2), (2, 2)),
)
_SLOT_BANANA_SPLIT_LINES = (*_SLOT_LINES[:3], *_SLOT_COLUMNS)


def _slots_validate_probability_table(name: str, table) -> None:
    total = sum(int(weight) for _value, weight in table)
    if total != ROLETA2_PROBABILITY_SCALE:
        raise RuntimeError(
            f"tabela de probabilidades inválida em {name}: "
            f"{total}/{ROLETA2_PROBABILITY_SCALE}"
        )
    if any(int(weight) <= 0 for _value, weight in table):
        raise RuntimeError(f"peso não positivo na tabela {name}")


for _probability_name, _probability_table in (
    ("resultados", ROLETA2_OUTCOME_WEIGHTS),
    ("tipos de linha", ROLETA2_LINE_TYPE_WEIGHTS),
    ("eixos do banana split", ROLETA2_BANANA_SPLIT_AXIS_WEIGHTS),
    ("frutas da colheita", ROLETA2_COLHEITA_FRUIT_WEIGHTS),
    ("setes espalhados", ROLETA2_SCATTERED_SEVEN_WEIGHTS),
    ("setes em perdas", ROLETA2_LOSS_SEVEN_WEIGHTS),
):
    _slots_validate_probability_table(_probability_name, _probability_table)


def _slot_weighted_choice(rng: random.Random, table):
    values, weights = zip(*table)
    return rng.choices(values, weights=weights, k=1)[0]


def _slot_pick_result_line(rng: random.Random):
    line_type = str(_slot_weighted_choice(rng, ROLETA2_LINE_TYPE_WEIGHTS))
    return rng.choice(_SLOT_LINES[:3] if line_type == "horizontal" else _SLOT_DIAGONALS)


def _slot_pick_banana_split_line(rng: random.Random):
    axis = str(_slot_weighted_choice(rng, ROLETA2_BANANA_SPLIT_AXIS_WEIGHTS))
    return rng.choice(_SLOT_LINES[:3] if axis == "horizontal" else _SLOT_COLUMNS)


def _slot_line_values(grid: list[list[str]], line) -> list[str]:
    return [grid[row][column] for row, column in line]


def _slot_set_line(grid: list[list[str]], line, values: list[str] | tuple[str, ...]) -> None:
    for (row, column), value in zip(line, values):
        grid[row][column] = str(value)


def _slot_random_symbol(rng: random.Random, *, include_seven: bool = True) -> str:
    symbols = SLOT_SYMBOLS if include_seven else SLOT_SYMBOLS[:-1]
    weights = SLOT_SYMBOL_WEIGHTS if include_seven else SLOT_SAFE_SYMBOL_WEIGHTS
    return str(rng.choices(symbols, weights=weights, k=1)[0])


def _slot_random_grid(rng: random.Random, *, include_seven: bool = True) -> list[list[str]]:
    return [
        [_slot_random_symbol(rng, include_seven=include_seven) for _ in range(3)]
        for _ in range(3)
    ]


def _slots_has_escorredio(grid: list[list[str]]) -> bool:
    return any(_slot_line_values(grid, line) == [SLOT_BANANA] * 3 for line in _SLOT_DIAGONALS)


def _slots_is_sete_solitario(grid: list[list[str]]) -> bool:
    flat = [cell for row in grid for cell in row]
    return (
        flat.count(SLOT_SEVEN) == 1
        and not _slots_matching_kinds(grid)
        and not _slots_has_escorredio(grid)
    )


def _slots_sete_solitario_column(grid: list[list[str]]) -> int | None:
    if not _slots_is_sete_solitario(grid):
        return None
    for row in range(3):
        for column in range(3):
            if grid[row][column] == SLOT_SEVEN:
                return column
    return None


def _slots_generate_sete_solitario_respin(
    rng: random.Random,
    grid: list[list[str]],
) -> tuple[list[list[str]], int]:
    locked_column = _slots_sete_solitario_column(grid)
    if locked_column is None:
        raise ValueError("tabuleiro sem 7 solitário")
    final = [list(row) for row in grid]
    for row in range(3):
        for column in range(3):
            if column == locked_column:
                continue
            final[row][column] = _slot_random_symbol(rng, include_seven=True)
    return final, int(locked_column)


def _slots_banana_split_location(grid: list[list[str]]) -> tuple[str, int] | None:
    target = [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA]
    for index, line in enumerate(_SLOT_LINES[:3]):
        if _slot_line_values(grid, line) == target:
            return "linha", index
    for index, line in enumerate(_SLOT_COLUMNS):
        if _slot_line_values(grid, line) == target:
            return "coluna", index
    return None


def _slots_banana_split_summary(grid: list[list[str]]) -> str:
    location = _slots_banana_split_location(grid)
    if location is None:
        return "Duas bananas ao redor da cereja"
    axis, index = location
    return f"Duas bananas ao redor da cereja na {axis} {index + 1}"


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
    if _slots_banana_split_location(grid) is not None:
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


def _slots_fallback_grid(kind: str, variant: dict[str, object] | None = None) -> list[list[str]]:
    selected_variant = dict(variant or {})
    fallback_rng = random.Random(f"roleta2:{kind}:{selected_variant!r}")
    for _ in range(4_096):
        candidate = _slots_build_candidate(kind, fallback_rng, selected_variant)
        if _slots_grid_matches_kind(kind, candidate, selected_variant):
            return candidate

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


def _slots_select_variant(kind: str, rng: random.Random) -> dict[str, object]:
    if kind in {"jackpot", "bar_triplo"}:
        return {"line": _slot_pick_result_line(rng)}
    if kind == "banana_split":
        return {"line": _slot_pick_banana_split_line(rng)}
    if kind == "colheita":
        return {
            "line": rng.choice(_SLOT_LINES[:3]),
            "fruit": str(_slot_weighted_choice(rng, ROLETA2_COLHEITA_FRUIT_WEIGHTS)),
        }
    if kind == "setes_espalhados":
        return {"seven_count": int(_slot_weighted_choice(rng, ROLETA2_SCATTERED_SEVEN_WEIGHTS))}
    if kind == "loss":
        return {"seven_count": int(_slot_weighted_choice(rng, ROLETA2_LOSS_SEVEN_WEIGHTS))}
    if kind == "sete_pecados":
        return {"fruit": str(_slot_weighted_choice(rng, ROLETA2_COLHEITA_FRUIT_WEIGHTS))}
    return {}


def _slots_build_candidate(
    kind: str,
    rng: random.Random,
    variant: dict[str, object] | None = None,
) -> list[list[str]]:
    selected = variant if variant is not None else _slots_select_variant(kind, rng)
    if kind == "sete_pecados":
        fruit = str(selected.get("fruit") or _slot_weighted_choice(rng, ROLETA2_COLHEITA_FRUIT_WEIGHTS))
        flat = [SLOT_SEVEN] * 7 + [fruit, SLOT_BAR]
        rng.shuffle(flat)
        return [flat[index:index + 3] for index in range(0, 9, 3)]

    if kind in {"jackpot", "bar_triplo", "banana_split", "colheita"}:
        grid = _slot_random_grid(rng, include_seven=False)
        line = selected.get("line")
        allowed_lines = _SLOT_BANANA_SPLIT_LINES if kind == "banana_split" else _SLOT_LINES
        if line not in allowed_lines:
            if kind == "colheita":
                line = rng.choice(_SLOT_LINES[:3])
            elif kind == "banana_split":
                line = _slot_pick_banana_split_line(rng)
            else:
                line = _slot_pick_result_line(rng)
        if kind == "jackpot":
            values = [SLOT_SEVEN] * 3
        elif kind == "bar_triplo":
            values = [SLOT_BAR] * 3
        elif kind == "banana_split":
            values = [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA]
        else:
            fruit = str(selected.get("fruit") or _slot_weighted_choice(rng, ROLETA2_COLHEITA_FRUIT_WEIGHTS))
            values = [fruit] * 3
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

    if kind in {"faltou_um_sete", "setes_espalhados", "loss"}:
        if kind == "faltou_um_sete":
            count = 2
        elif kind == "setes_espalhados":
            count = int(selected.get("seven_count", 3) or 3)
        else:
            count = int(selected.get("seven_count", 0) or 0)
        count = max(0, min(9, count))
        flat = [_slot_random_symbol(rng, include_seven=False) for _ in range(9)]
        for position in rng.sample(range(9), count):
            flat[position] = SLOT_SEVEN
        return [flat[index:index + 3] for index in range(0, 9, 3)]

    return _slot_random_grid(rng, include_seven=False)


def _slots_grid_matches_kind(
    kind: str,
    grid: list[list[str]],
    variant: dict[str, object] | None = None,
) -> bool:
    selected = variant or {}
    matches = _slots_matching_kinds(grid)
    if kind == "deja_vu":
        # Déjà vu pode acumular com resultados de OUTROS estágios da mesma
        # rodada, mas o tabuleiro que o dispara é exclusivo: nenhuma outra
        # combinação paga simultaneamente nesse mesmo grid.
        if matches != {"deja_vu"}:
            return False
    else:
        expected_matches = set() if kind == "loss" else {kind}
        if matches != expected_matches:
            return False
    if kind in {"loss", "deja_vu"} and _slots_has_escorredio(grid):
        return False

    flat = [cell for row in grid for cell in row]
    if kind in {"loss", "setes_espalhados"} and "seven_count" in selected:
        if flat.count(SLOT_SEVEN) != int(selected["seven_count"]):
            return False

    line = selected.get("line")
    allowed_variant_lines = _SLOT_BANANA_SPLIT_LINES if kind == "banana_split" else _SLOT_LINES
    if line in allowed_variant_lines:
        values = _slot_line_values(grid, line)
        expected_values = {
            "jackpot": [SLOT_SEVEN] * 3,
            "bar_triplo": [SLOT_BAR] * 3,
            "banana_split": [SLOT_BANANA, SLOT_CEREJA, SLOT_BANANA],
            "colheita": [str(selected.get("fruit") or SLOT_BANANA)] * 3,
        }.get(kind)
        if expected_values is not None and values != expected_values:
            return False
    return True


def _slots_generate_grid(
    kind: str,
    rng: random.Random,
    variant: dict[str, object] | None = None,
) -> list[list[str]]:
    selected_variant = dict(variant) if variant is not None else _slots_select_variant(kind, rng)
    for _ in range(512):
        grid = _slots_build_candidate(kind, rng, selected_variant)
        if _slots_grid_matches_kind(kind, grid, selected_variant):
            return grid
    return _slots_fallback_grid(kind, selected_variant)


def _slots_apply_escorredio_preview(
    rng: random.Random,
    preview_grid: list[list[str]],
    *,
    force_deja_vu: bool = False,
) -> tuple[list[list[str]], list[list[str]], int]:
    preview = [list(row) for row in preview_grid]
    if not _slots_has_escorredio(preview):
        raise ValueError("preview sem Escorredio")

    candidate_columns = (0, 2) if force_deja_vu else (0, 1, 2)
    for _ in range(512):
        slip_column = int(rng.choice(candidate_columns))
        final = [list(row) for row in preview]
        if force_deja_vu:
            mirror_column = 2 if slip_column == 0 else 0
            for row in range(3):
                final[row][slip_column] = final[row][mirror_column]
            # A transição forçada é exclusiva: o novo tabuleiro é resolvido
            # como Déjà vu, mesmo se os mesmos símbolos também desenharem outra
            # combinação visual rara. O Escorredio já pertence ao estágio anterior.
            if "deja_vu" in _slots_matching_kinds(final):
                return preview, final, slip_column
            continue

        for row in range(3):
            final[row][slip_column] = _slot_random_symbol(rng, include_seven=True)
        # O Escorredio já foi consumido neste estágio e não se auto-repete.
        if _slots_has_escorredio(final):
            continue
        # Os 35% são a chance total da transição Escorredio -> Déjà vu.
        if "deja_vu" in _slots_matching_kinds(final):
            continue
        return preview, final, slip_column

    slip_column = int(candidate_columns[0])
    final = [list(row) for row in preview]
    if force_deja_vu:
        mirror_column = 2 if slip_column == 0 else 0
        for row in range(3):
            final[row][slip_column] = final[row][mirror_column]
    else:
        fallback_cycle = (SLOT_CEREJA, SLOT_BAR, SLOT_FRAMBOESA)
        for row in range(3):
            final[row][slip_column] = fallback_cycle[row]
    return preview, final, slip_column


def _slots_generate_escorredio(
    rng: random.Random,
    *,
    force_deja_vu: bool = False,
) -> tuple[list[list[str]], list[list[str]], int]:
    diagonal = rng.choice(_SLOT_DIAGONALS)
    # Para o Escorredio realmente *causar* um Déjà vu, a coluna que escorrega
    # precisa ser uma das externas; só elas conseguem transformar um preview
    # que ainda não é Déjà vu em duas colunas externas idênticas.
    slip_column = rng.choice((0, 2)) if force_deja_vu else rng.randrange(3)
    for _ in range(512):
        preview = _slot_random_grid(rng, include_seven=False)
        _slot_set_line(preview, diagonal, [SLOT_BANANA] * 3)
        if _slots_detect_kind(preview) == "loss" and _slots_has_escorredio(preview):
            for _reroll in range(512):
                final = [list(row) for row in preview]
                if force_deja_vu:
                    mirror_column = 2 if slip_column == 0 else 0
                    for row in range(3):
                        final[row][slip_column] = final[row][mirror_column]
                else:
                    for row in range(3):
                        final[row][slip_column] = _slot_random_symbol(rng, include_seven=True)
                # Escorredio é o gatilho da coluna extra, não deve se auto-repetir
                # só porque o novo símbolo manteve a diagonal de bananas.
                if _slots_has_escorredio(final):
                    continue
                final_matches = _slots_matching_kinds(final)
                if force_deja_vu:
                    if final_matches != {"deja_vu"}:
                        continue
                elif "deja_vu" in final_matches:
                    continue
                return preview, final, slip_column
    if diagonal == _SLOT_DIAGONALS[0]:
        preview = [
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
            [SLOT_CEREJA, SLOT_BANANA, SLOT_FRAMBOESA],
            [SLOT_FRAMBOESA, SLOT_CEREJA, SLOT_BANANA],
        ]
    else:
        preview = [
            [SLOT_FRAMBOESA, SLOT_CEREJA, SLOT_BANANA],
            [SLOT_CEREJA, SLOT_BANANA, SLOT_FRAMBOESA],
            [SLOT_BANANA, SLOT_FRAMBOESA, SLOT_CEREJA],
        ]
    final = [list(row) for row in preview]
    if force_deja_vu:
        mirror_column = 2 if slip_column == 0 else 0
        for row in range(3):
            final[row][slip_column] = final[row][mirror_column]
        # O fallback acima pode coincidir com outra combinação em um caso
        # extremo. Este grid é deliberadamente simples e exclusivo para manter
        # a invariável "Déjà vu sozinho no tabuleiro".
        if _slots_matching_kinds(final) != {"deja_vu"} or _slots_has_escorredio(final):
            if slip_column == 0:
                preview = [
                    [SLOT_BANANA, SLOT_BAR, SLOT_CEREJA],
                    [SLOT_CEREJA, SLOT_BANANA, SLOT_FRAMBOESA],
                    [SLOT_FRAMBOESA, SLOT_BAR, SLOT_BANANA],
                ]
            else:
                preview = [
                    [SLOT_BANANA, SLOT_BAR, SLOT_FRAMBOESA],
                    [SLOT_FRAMBOESA, SLOT_BANANA, SLOT_CEREJA],
                    [SLOT_CEREJA, SLOT_BAR, SLOT_BANANA],
                ]
            final = [list(row) for row in preview]
            mirror_column = 2 if slip_column == 0 else 0
            for row in range(3):
                final[row][slip_column] = final[row][mirror_column]
    else:
        fallback_cycle = (SLOT_CEREJA, SLOT_BAR, SLOT_FRAMBOESA)
        for row in range(3):
            final[row][slip_column] = fallback_cycle[row]
        if "deja_vu" in _slots_matching_kinds(final):
            final[0][slip_column] = SLOT_BAR
    return preview, final, slip_column


def _slots_matching_pair(grid: list[list[str]]) -> tuple[str, int] | None:
    for row_index, values in enumerate(grid):
        for symbol in SLOT_SYMBOLS:
            if values.count(symbol) == 2:
                return str(symbol), row_index
    return None


def _slots_matching_pair_symbol(grid: list[list[str]]) -> str | None:
    match = _slots_matching_pair(grid)
    return str(match[0]) if match is not None else None


def _slots_bar_abriu_as_7_summary(grid: list[list[str]]) -> str:
    for line in _SLOT_LINES:
        values = _slot_line_values(grid, line)
        if values.count(SLOT_BAR) == 2 and values.count(SLOT_SEVEN) == 1:
            return f"Veio dois {SLOT_EMOJIS[SLOT_BAR]} e um {SLOT_EMOJIS[SLOT_SEVEN]}"
    return f"Veio dois {SLOT_EMOJIS[SLOT_BAR]} e um {SLOT_EMOJIS[SLOT_SEVEN]}"


def _slots_jackpot_summary(grid: list[list[str]]) -> str:
    target = [SLOT_SEVEN] * 3
    for index, line in enumerate(_SLOT_LINES[:3]):
        if _slot_line_values(grid, line) == target:
            ordinal = ("1ª", "2ª", "3ª")[index]
            return f"Três {SLOT_EMOJIS[SLOT_SEVEN]} combinam na {ordinal} linha"
    if _slot_line_values(grid, _SLOT_DIAGONALS[0]) == target:
        return f"Três {SLOT_EMOJIS[SLOT_SEVEN]} combinam na diagonal principal"
    if _slot_line_values(grid, _SLOT_DIAGONALS[1]) == target:
        return f"Três {SLOT_EMOJIS[SLOT_SEVEN]} combinam na diagonal secundária"
    return f"Três {SLOT_EMOJIS[SLOT_SEVEN]} combinaram"


def _slots_colheita_fruit(grid: list[list[str]]) -> str:
    for row in grid:
        if len(set(row)) == 1 and row[0] in SLOT_FRUITS:
            return str(row[0])
    return SLOT_BANANA


def _slots_deja_vu_payout(chain_index: int) -> int:
    return ROLETA2_DEJA_VU_BASE_PAYOUT * max(1, int(chain_index or 1))


def _slots_effect_triggers_followup(
    source_kind: str,
    rng: random.Random,
    *,
    allow_deja_vu: bool = True,
) -> bool:
    if source_kind == "deja_vu":
        return float(rng.random()) < ROLETA2_DEJA_VU_TO_ESCORREDIO_CHANCE
    if source_kind == "escorredio" and allow_deja_vu:
        return float(rng.random()) < ROLETA2_ESCORREDIO_TO_DEJA_VU_CHANCE
    return False


def _slots_deja_vu_followup_policy(rng: random.Random) -> tuple[str | None, tuple[str, ...]]:
    # Os 25% são a chance TOTAL de o próximo estágio ser Escorredio. Nos 75%
    # restantes ele é removido do sorteio normal, evitando 25% + chance-base.
    if _slots_effect_triggers_followup("deja_vu", rng):
        return "escorredio", ()
    return None, ("escorredio",)


def _slots_respin_column_order(deja_vu_index: int) -> tuple[int, int, int]:
    # O primeiro Déjà vu rebate da direita para a esquerda; cada novo Déjà vu
    # inverte o sentido inteiro do respin (entrada dos emojis + parada).
    return (2, 1, 0) if max(1, int(deja_vu_index or 1)) % 2 == 1 else (0, 1, 2)


def _slots_final_presentation(
    outcomes: list[dict[str, object]],
) -> tuple[dict[str, object], str | None]:
    if not outcomes:
        return {}, None

    # Título/descrição/emoji sempre pertencem ao ÚLTIMO tabuleiro mostrado.
    # A cor pode continuar herdando o último evento especial anterior quando o
    # terminal é uma perda comum, preservando a identidade visual da cadeia sem
    # mentir sobre a grade final.
    terminal = outcomes[-1]
    terminal_kind = str(terminal.get("primary_kind") or terminal.get("kind") or "loss")
    color_theme = str(terminal.get("color_theme")) if terminal.get("color_theme") else None
    if terminal_kind == "loss":
        for previous in reversed(outcomes[:-1]):
            previous_kind = str(previous.get("primary_kind") or previous.get("kind") or "loss")
            if previous_kind == "loss" or not previous.get("color_theme"):
                continue
            color_theme = str(previous.get("color_theme"))
            break
    return terminal, color_theme


def _slots_respin_start_grid(
    current_grid: list[list[str]],
    *,
    spinning_columns: set[int],
) -> list[list[str]]:
    return [
        [
            SLOT_SPINNING if column in spinning_columns else str(current_grid[row][column])
            for column in range(3)
        ]
        for row in range(3)
    ]


def _slots_result_presentation(
    kind: str,
    grid: list[list[str]],
    title: str,
) -> tuple[str, str | None]:
    symbol: str | None = None
    if kind == "colheita":
        symbol = _slots_colheita_fruit(grid)
    elif kind == "loss":
        if title == "7 solitário":
            symbol = SLOT_SEVEN
        elif title == "Foi quase hein":
            symbol = _slots_matching_pair_symbol(grid)
    else:
        symbol = ROLETA2_KIND_TITLE_SYMBOLS.get(kind)

    if symbol in SLOT_EMOJIS:
        return SLOT_EMOJIS[symbol], symbol
    if kind == "deja_vu":
        return "🔁", "deja_vu"
    return "🎰", None


class GincanaSlotsMixin:
    def _pick_roleta2_loss_copy(self, grid: list[list[str]]) -> tuple[str, str]:
        self._ensure_game_animation_runtime()
        flat = [cell for row in grid for cell in row]
        pair_match = _slots_matching_pair(grid)
        contextual_title = ""
        if _slots_is_sete_solitario(grid):
            # 7 solitário agora é uma mecânica de respin, então nunca pode ser
            # substituído por uma copy genérica só porque repetiu em sequência.
            self._last_game_loss_titles["roleta2"] = "7 solitário"
            return "7 solitário", ROLETA2_LOSS_SUMMARIES["7 solitário"]
        if pair_match is not None:
            contextual_title = "Foi quase hein"

        generic_titles = (
            "Não veio nada",
            "Nenhuma combinação",
            "Você ganhou... nada!",
        )
        last_title = self._last_game_loss_titles.get("roleta2")
        if contextual_title and contextual_title != last_title:
            title = contextual_title
        else:
            available = [candidate for candidate in generic_titles if candidate != last_title]
            title = random.choice(available or list(generic_titles))
        self._last_game_loss_titles["roleta2"] = title
        if title == "Foi quase hein" and pair_match is not None:
            pair_symbol = str(pair_match[0])
            line_number = ("1ª", "2ª", "3ª")[int(pair_match[1])]
            pair_emoji = SLOT_EMOJIS.get(pair_symbol, "🎰")
            return title, f"Duas {pair_emoji} combinaram na {line_number} linha"
        return title, ROLETA2_LOSS_SUMMARIES[title]

    def _roll_roleta2_outcome(
        self,
        *,
        deja_vu_index: int = 1,
        allow_deja_vu: bool = True,
        forced_kind: str | None = None,
        excluded_kinds: tuple[str, ...] = (),
        grid_override: list[list[str]] | None = None,
    ) -> dict[str, object]:
        rng = random
        preview_grid: list[list[str]] | None = None
        slip_column: int | None = None

        if grid_override is not None:
            candidate_grid = [list(row) for row in grid_override]
            candidate_matches = _slots_matching_kinds(candidate_grid)
            # Um tabuleiro que desenha Déjà vu é resolvido exclusivamente como
            # Déjà vu. Outras combinações podem existir em outros estágios da
            # mesma rodada, nunca simultaneamente neste grid.
            if allow_deja_vu and "deja_vu" in candidate_matches:
                kind = "deja_vu"
                grid = candidate_grid
            elif _slots_has_escorredio(candidate_grid):
                kind = "escorredio"
                trigger_deja_vu = _slots_effect_triggers_followup(
                    "escorredio",
                    rng,
                    allow_deja_vu=allow_deja_vu,
                )
                preview_grid, grid, slip_column = _slots_apply_escorredio_preview(
                    rng,
                    candidate_grid,
                    force_deja_vu=trigger_deja_vu,
                )
            else:
                kind = _slots_detect_kind(candidate_grid)
                grid = candidate_grid
        else:
            excluded = set(str(item) for item in excluded_kinds)
            if not allow_deja_vu:
                excluded.add("deja_vu")
            outcome_table = tuple(
                item for item in ROLETA2_OUTCOME_WEIGHTS if item[0] not in excluded
            )
            selected_forced_kind = str(forced_kind or "")
            if selected_forced_kind and selected_forced_kind not in excluded:
                kind = selected_forced_kind
            else:
                kinds, weights = zip(*outcome_table)
                kind = str(random.choices(kinds, weights=weights, k=1)[0])
            if kind == "escorredio":
                trigger_deja_vu = _slots_effect_triggers_followup(
                    "escorredio",
                    rng,
                    allow_deja_vu=allow_deja_vu,
                )
                preview_grid, grid, slip_column = _slots_generate_escorredio(
                    rng,
                    force_deja_vu=trigger_deja_vu,
                )
            else:
                grid = _slots_generate_grid(kind, rng)

        def result_details(component_kind: str) -> dict[str, object]:
            normal_payout, bonus_payout = ROLETA2_PAYOUTS.get(component_kind, (0, 0))
            free_spins = 0
            if component_kind == "colheita":
                title = "Colheita"
                fruit = _slots_colheita_fruit(grid)
                fruit_emoji = SLOT_EMOJIS.get(fruit, "🎰")
                if fruit == SLOT_FRAMBOESA:
                    normal_payout, bonus_payout = 0, 30
                    summary = f"3 {fruit_emoji} renderam +30 {self._CHIP_BONUS_EMOJI}"
                elif fruit == SLOT_CEREJA:
                    normal_payout, bonus_payout = 20, 0
                    summary = "As cerejas duplicaram o valor base da Colheita"
                else:
                    normal_payout, bonus_payout = 15, 0
                    free_spins = 1
                    summary = f"3 {fruit_emoji} fecharam uma Colheita e devolveram a entrada"
            else:
                summaries = {
                    "sete_pecados": "Sete dos nove espaços vieram com 7",
                    "jackpot": _slots_jackpot_summary(grid),
                    "bar_triplo": "Três BAR fecharam uma linha",
                    "bar_abriu_as_7": _slots_bar_abriu_as_7_summary(grid),
                    "deja_vu": "A primeira e a terceira coluna repetiram o mesmo resultado",
                    "banana_split": _slots_banana_split_summary(grid),
                    "escorredio": "As bananas fizeram uma coluna escorregar e girar outra vez",
                    "setes_espalhados": "Os 7 vieram espalhados pelo resultado",
                    "faltou_um_sete": "Se tivesse mais 1 hein",
                }
                titles = {
                    "sete_pecados": "Sete pecados",
                    "jackpot": "Jackpot!",
                    "bar_triplo": "BAR triplo",
                    "bar_abriu_as_7": "Bar abriu às 7",
                    "deja_vu": "Déjà vu",
                    "banana_split": "Banana split",
                    "escorredio": "Escorredio",
                    "setes_espalhados": "Setes espalhados",
                    "faltou_um_sete": "Faltou um sete",
                }
                title = titles.get(component_kind, "Roleta 2")
                summary = summaries.get(component_kind, "Resultado da roleta")
                if component_kind == "deja_vu":
                    normal_payout, bonus_payout = 0, _slots_deja_vu_payout(deja_vu_index)
            title_emoji, color_theme = _slots_result_presentation(component_kind, grid, title)
            return {
                "kind": component_kind,
                "title": title,
                "title_emoji": title_emoji,
                "color_theme": color_theme,
                "summary": summary,
                "normal_payout": int(normal_payout),
                "bonus_payout": int(bonus_payout),
                "free_spins": int(free_spins),
                "premium": component_kind in {
                    "sete_pecados", "jackpot", "bar_triplo", "bar_abriu_as_7"
                },
                "partial": component_kind in {"deja_vu", "faltou_um_sete"},
                "jackpot": component_kind in {"sete_pecados", "jackpot"},
            }

        if kind == "loss":
            title, summary = self._pick_roleta2_loss_copy(grid)
            title_emoji, color_theme = _slots_result_presentation(kind, grid, title)
            components = [{
                "kind": "loss",
                "title": title,
                "title_emoji": title_emoji,
                "color_theme": color_theme,
                "summary": summary,
                "normal_payout": 0,
                "bonus_payout": 0,
                "free_spins": 0,
                "premium": False,
                "partial": False,
                "jackpot": False,
            }]
        elif kind == "deja_vu":
            components = [result_details("deja_vu")]
        elif kind == "escorredio":
            matches = _slots_matching_kinds(grid)
            ordered_combinations = (
                "sete_pecados",
                "jackpot",
                "bar_triplo",
                "bar_abriu_as_7",
                "colheita",
                "banana_split",
                "setes_espalhados",
                "faltou_um_sete",
            )
            if "deja_vu" in matches:
                # Escorredio pertence ao preview; o grid final é exclusivamente
                # Déjà vu e só ele pode pagar nesse segundo estágio.
                components = [result_details("escorredio"), result_details("deja_vu")]
            else:
                components = [
                    result_details(component_kind)
                    for component_kind in ordered_combinations
                    if component_kind in matches
                ]
                components.append(result_details("escorredio"))
        else:
            components = [result_details(kind)]

        primary = next(
            (component for component in components if component["kind"] not in {"deja_vu", "loss"}),
            components[0],
        )
        normal_payout = sum(int(component["normal_payout"]) for component in components)
        bonus_payout = sum(int(component["bonus_payout"]) for component in components)
        free_spins = sum(int(component.get("free_spins", 0) or 0) for component in components)
        non_deja_summaries = [
            str(component["summary"])
            for component in components
            if component["kind"] != "deja_vu" and str(component["summary"]).strip()
        ]
        if non_deja_summaries:
            summary = "\n".join(dict.fromkeys(non_deja_summaries))
        else:
            summary = str(primary["summary"])

        component_kinds = tuple(str(component["kind"]) for component in components)
        has_deja_vu = "deja_vu" in component_kinds
        has_escorredio = "escorredio" in component_kinds
        has_sete_solitario = (
            not has_deja_vu
            and _slots_is_sete_solitario(grid)
        )
        sete_solitario_column = (
            _slots_sete_solitario_column(grid) if has_sete_solitario else None
        )
        escorredio_normal_payout = sum(
            int(component["normal_payout"])
            for component in components
            if component["kind"] == "escorredio"
        )
        escorredio_bonus_payout = sum(
            int(component["bonus_payout"])
            for component in components
            if component["kind"] == "escorredio"
        )
        return {
            "kind": kind,
            "primary_kind": str(primary["kind"]),
            "component_kinds": component_kinds,
            "components": tuple(dict(component) for component in components),
            "has_deja_vu": has_deja_vu,
            "has_escorredio": has_escorredio,
            "has_sete_solitario": has_sete_solitario,
            "sete_solitario_column": sete_solitario_column,
            "sete_solitario_summary": ("Veio apenas um 7" if has_sete_solitario else ""),
            "deja_vu_index": int(deja_vu_index) if has_deja_vu else 0,
            "deja_vu_payout": (
                _slots_deja_vu_payout(deja_vu_index) if has_deja_vu else 0
            ),
            "deja_vu_summary": (
                "A primeira e a terceira coluna repetiram o mesmo resultado"
                if has_deja_vu else ""
            ),
            "escorredio_summary": (
                "As bananas fizeram uma coluna escorregar e girar outra vez"
                if has_escorredio else ""
            ),
            "escorredio_normal_payout": int(escorredio_normal_payout),
            "escorredio_bonus_payout": int(escorredio_bonus_payout),
            "title": str(primary["title"]),
            "title_emoji": str(primary["title_emoji"]),
            "color_theme": primary["color_theme"],
            "summary": summary,
            "grid": grid,
            "preview_grid": preview_grid,
            "slip_column": slip_column,
            "normal_payout": int(normal_payout),
            "bonus_payout": int(bonus_payout),
            "free_spins": int(free_spins),
            "success": kind != "loss",
            "premium": any(bool(component["premium"]) for component in components),
            "partial": all(bool(component["partial"]) for component in components),
            "jackpot": any(bool(component["jackpot"]) for component in components),
        }

    def _render_roleta2_board(self, grid: list[list[str]]) -> str:
        return "\n".join(
            "# " + " ".join(
                SLOT_EMOJIS.get(str(cell), SLOT_EMOJIS[SLOT_SPINNING])
                for cell in row
            )
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

    def _roleta2_window_total(self, bonus_spins: int = 0, reward_spins: int = 0) -> int:
        bonus = max(0, min(ROLETA2_DAILY_EXTRA_CAP, int(bonus_spins or 0)))
        rewards = max(0, int(reward_spins or 0))
        return ROLETA2_SPIN_LIMIT + bonus + rewards

    async def _sync_roleta2_spin_window(self, guild_id: int, user_id: int) -> dict[str, float | int]:
        now = time.time()
        doc = self.db._get_user_doc(guild_id, user_id)
        try:
            started_at = float(doc.get("roleta2_window_started_at", 0) or 0.0)
        except Exception:
            started_at = 0.0
        try:
            used = max(0, int(doc.get("roleta2_spins_used", 0) or 0))
        except Exception:
            used = 0
        try:
            bonus = max(
                0,
                min(ROLETA2_DAILY_EXTRA_CAP, int(doc.get("roleta2_bonus_spins", 0) or 0)),
            )
        except Exception:
            bonus = 0
        try:
            rewards = max(0, int(doc.get("roleta2_reward_spins", 0) or 0))
        except Exception:
            rewards = 0

        changed = False
        if started_at <= 0 or (started_at + ROLETA2_WINDOW_SECONDS) <= now:
            started_at = now
            used = 0
            bonus = 0
            doc["roleta2_window_started_at"] = float(started_at)
            doc["roleta2_spins_used"] = 0
            doc["roleta2_bonus_spins"] = 0
            doc["roleta2_reward_spins"] = 0
            rewards = 0
            changed = True

        total = self._roleta2_window_total(bonus, rewards)
        available = max(0, total - used)
        reset_in = max(0.0, (started_at + ROLETA2_WINDOW_SECONDS) - now)
        if changed:
            await self.db._save_user_doc(guild_id, user_id, doc)
        return {
            "started_at": float(started_at),
            "used": int(used),
            "bonus": int(bonus),
            "rewards": int(rewards),
            "total": int(total),
            "available": int(available),
            "reset_in": float(reset_in),
        }

    async def _consume_roleta2_spin(self, guild_id: int, user_id: int) -> dict[str, float | int]:
        state = await self._sync_roleta2_spin_window(guild_id, user_id)
        if int(state["available"]) <= 0:
            return state
        doc = self.db._get_user_doc(guild_id, user_id)
        used = int(state["used"]) + 1
        doc["roleta2_window_started_at"] = float(state["started_at"])
        doc["roleta2_spins_used"] = used
        doc["roleta2_bonus_spins"] = int(state["bonus"])
        doc["roleta2_reward_spins"] = int(state.get("rewards", 0) or 0)
        await self.db._save_user_doc(guild_id, user_id, doc)
        total = int(state["total"])
        return {
            "started_at": float(state["started_at"]),
            "used": used,
            "bonus": int(state["bonus"]),
            "rewards": int(state.get("rewards", 0) or 0),
            "total": total,
            "available": max(0, total - used),
            "reset_in": float(
                max(0.0, (float(state["started_at"]) + ROLETA2_WINDOW_SECONDS) - time.time())
            ),
        }

    async def _grant_daily_roleta2_spins(
        self,
        guild_id: int,
        user_id: int,
    ) -> tuple[int, dict[str, float | int]]:
        state = await self._sync_roleta2_spin_window(guild_id, user_id)
        current_bonus = int(state["bonus"])
        granted = min(
            ROLETA2_DAILY_EXTRA_SPINS,
            max(0, ROLETA2_DAILY_EXTRA_CAP - current_bonus),
        )
        if granted <= 0:
            return 0, state
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["roleta2_window_started_at"] = float(state["started_at"])
        doc["roleta2_spins_used"] = int(state["used"])
        doc["roleta2_bonus_spins"] = current_bonus + granted
        doc["roleta2_reward_spins"] = int(state.get("rewards", 0) or 0)
        await self.db._save_user_doc(guild_id, user_id, doc)
        return granted, await self._sync_roleta2_spin_window(guild_id, user_id)

    async def _grant_roleta2_reward_spins(
        self,
        guild_id: int,
        user_id: int,
        count: int = 1,
    ) -> tuple[int, dict[str, float | int]]:
        amount = max(0, int(count or 0))
        state = await self._sync_roleta2_spin_window(guild_id, user_id)
        if amount <= 0:
            return 0, state
        doc = self.db._get_user_doc(guild_id, user_id)
        rewards = int(state.get("rewards", 0) or 0) + amount
        doc["roleta2_window_started_at"] = float(state["started_at"])
        doc["roleta2_spins_used"] = int(state["used"])
        doc["roleta2_bonus_spins"] = int(state["bonus"])
        doc["roleta2_reward_spins"] = rewards
        await self.db._save_user_doc(guild_id, user_id, doc)
        return amount, await self._sync_roleta2_spin_window(guild_id, user_id)

    async def _reserve_roleta2_spin_state(
        self,
        guild_id: int,
        user_id: int,
        *,
        is_staff: bool,
    ) -> tuple[bool, dict[str, float | int]]:
        state = await self._sync_roleta2_spin_window(guild_id, user_id)
        if int(state.get("available", 0) or 0) <= 0:
            return bool(is_staff), state
        return True, await self._consume_roleta2_spin(guild_id, user_id)

    def _roleta2_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
        available = max(0, int(state.get("available", 0) or 0))
        if is_staff and available <= 0:
            return "Seus giros da roleta2 acabaram, mas como você é staff ainda pode girar"
        giro_text = "giro da roleta2" if available == 1 else "giros da roleta2"
        verb = "Resta" if available == 1 else "Restam"
        reset = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
        return f"{verb} {available} {giro_text} • Reset em {reset}"

    def _roleta2_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
        total = max(ROLETA2_SPIN_LIMIT, int(state.get("total", ROLETA2_SPIN_LIMIT) or ROLETA2_SPIN_LIMIT))
        wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
        return (
            "🎰 Sem giros por agora",
            f"Seus {total} giros da roleta2 acabaram\nReset em **{wait_text}**",
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
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=discord.Color.from_rgb(255, 190, 46),
        )

    def _make_roleta2_respin_view(
        self,
        board: str,
        *,
        balance_text: str,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
    ) -> discord.ui.LayoutView:
        return self._make_game_layout_view(
            "🔁 Déjà vu...",
            details=[
                f"**Resultado:** {self._format_game_result_breakdown(normal_delta, bonus_delta)}",
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=discord.Color(ROLETA2_THEME_COLORS["deja_vu"]),
        )

    def _make_roleta2_effect_view(
        self,
        *,
        title: str,
        title_emoji: str,
        color_theme: str,
        summary: str,
        board: str,
        balance_text: str,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
    ) -> discord.ui.LayoutView:
        return self._make_game_layout_view(
            f"{title_emoji} {title}",
            summary=summary,
            details=[
                f"**Resultado:** {self._format_game_result_breakdown(normal_delta, bonus_delta)}",
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=discord.Color(ROLETA2_THEME_COLORS[color_theme]),
        )

    def _format_roleta2_result_value(
        self,
        normal_delta: int,
        bonus_delta: int,
        free_spins: int = 0,
    ) -> str:
        parts: list[str] = []
        if int(normal_delta) != 0 or int(bonus_delta) != 0:
            parts.append(self._format_game_result_breakdown(normal_delta, bonus_delta))
        free_count = max(0, int(free_spins or 0))
        if free_count > 0:
            spin_label = "giro grátis" if free_count == 1 else "giros grátis"
            parts.append(f"+{free_count} {spin_label}")
        if parts:
            return " · ".join(parts)
        return self._format_game_result_breakdown(normal_delta, bonus_delta)

    def _make_roleta2_result_view(
        self,
        *,
        title: str,
        title_emoji: str,
        color_theme: str | None,
        summary: str,
        board: str,
        balance_text: str,
        success: bool,
        premium: bool,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
        free_spins: int = 0,
    ) -> discord.ui.LayoutView:
        # success/premium continuam na assinatura para preservar compatibilidade
        # com o fluxo atual; a cor do resultado agora segue tema + saldo líquido.
        _ = success, premium
        themed_color = ROLETA2_THEME_COLORS.get(str(color_theme or ""))
        if themed_color is not None:
            color = discord.Color(int(themed_color))
        else:
            net_delta = int(normal_delta) + int(bonus_delta)
            if net_delta > 0:
                color = discord.Color.green()
            elif net_delta < 0:
                color = discord.Color(OFF_COLOR)
            else:
                color = discord.Color(ROLETA2_NEUTRAL_COLOR)
        return self._make_game_layout_view(
            f"{title_emoji or '🎰'} {title}",
            summary=summary,
            details=[
                f"**Resultado:** {self._format_roleta2_result_value(normal_delta, bonus_delta, free_spins)}",
                f"**Saldo atual:** {balance_text}",
            ],
            board=board,
            footer_text=footer_text,
            color=color,
        )

    def _roleta2_historical_modifier_lines(
        self,
        outcomes: list[dict[str, object]],
    ) -> list[str]:
        # Resultados pagos em tabuleiros anteriores da cadeia continuam valendo,
        # mas não podem assumir o título/descrição do tabuleiro final. Exibi-los
        # como modificadores compactos mantém o valor auditável sem mentir sobre
        # a grade atualmente mostrada.
        aggregated: dict[tuple[str, str, str], dict[str, int]] = {}
        order: list[tuple[str, str, str]] = []
        for outcome in outcomes[:-1]:
            raw_components = outcome.get("components")
            if not isinstance(raw_components, (tuple, list)):
                continue
            for component in raw_components:
                if not isinstance(component, dict):
                    continue
                kind = str(component.get("kind") or "")
                if kind in {"", "loss", "deja_vu"}:
                    continue
                normal = max(0, int(component.get("normal_payout", 0) or 0))
                bonus = max(0, int(component.get("bonus_payout", 0) or 0))
                free_spins = max(0, int(component.get("free_spins", 0) or 0))
                if normal <= 0 and bonus <= 0 and free_spins <= 0:
                    continue
                key = (
                    kind,
                    str(component.get("title") or "Resultado"),
                    str(component.get("title_emoji") or "🎰"),
                )
                if key not in aggregated:
                    aggregated[key] = {"count": 0, "normal": 0, "bonus": 0, "free_spins": 0}
                    order.append(key)
                aggregated[key]["count"] += 1
                aggregated[key]["normal"] += normal
                aggregated[key]["bonus"] += bonus
                aggregated[key]["free_spins"] += free_spins

        lines: list[str] = []
        for key in order:
            _kind, title, emoji = key
            values = aggregated[key]
            count = int(values["count"])
            count_text = f" ×{count}" if count > 1 else ""
            rewards: list[str] = []
            normal = int(values["normal"])
            bonus = int(values["bonus"])
            free_spins = int(values.get("free_spins", 0) or 0)
            if normal > 0 and free_spins <= 0:
                rewards.append(f"+{normal} {self._CHIP_GAIN_EMOJI}")
            if bonus > 0:
                rewards.append(f"+{bonus} {self._CHIP_BONUS_EMOJI}")
            if free_spins > 0:
                spin_label = "giro grátis" if free_spins == 1 else "giros grátis"
                rewards.append(f"+{free_spins} {spin_label}")
            if rewards:
                lines.append(f"-# {emoji} {title}{count_text} · " + " · ".join(rewards))
        return lines

    async def _animate_roleta2_spin(
        self,
        source_message: discord.Message,
        *,
        outcome: dict[str, object],
        balance_text: str,
        footer_text: str,
        paid_entry: int,
        entry_normal: int,
        entry_bonus: int,
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
            escorredio_normal_delta = (
                int(outcome.get("escorredio_normal_payout", 0) or 0) - int(entry_normal)
            )
            escorredio_bonus_delta = (
                int(outcome.get("escorredio_bonus_payout", 0) or 0) - int(entry_bonus)
            )
            preview_view = self._make_roleta2_effect_view(
                title="Escorredio",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(target_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY):
                return spin_message, final_grid
            slipping = self._roleta2_display_grid(
                target_grid,
                stopped_columns=stopped,
                spinning_column=max(0, min(2, int(slip_column))),
            )
            slipping_view = self._make_roleta2_effect_view(
                title="Escorredio...",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(slipping),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, 0.76):
                return spin_message, final_grid

        if bool(outcome.get("has_deja_vu")):
            deja_view = self._make_roleta2_effect_view(
                title="Déjà vu",
                title_emoji="🔁",
                color_theme="deja_vu",
                summary=str(outcome.get("deja_vu_summary") or ""),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=(
                    int(outcome.get("normal_payout", 0) or 0) - int(entry_normal)
                ),
                bonus_delta=(
                    int(outcome.get("bonus_payout", 0) or 0) - int(entry_bonus)
                ),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=deja_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

        if bool(outcome.get("has_sete_solitario")):
            sete_view = self._make_roleta2_effect_view(
                title="7 solitário",
                title_emoji=SLOT_EMOJIS[SLOT_SEVEN],
                color_theme=SLOT_SEVEN,
                summary=str(outcome.get("sete_solitario_summary") or "Veio apenas um 7"),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=(
                    int(outcome.get("normal_payout", 0) or 0) - int(entry_normal)
                ),
                bonus_delta=(
                    int(outcome.get("bonus_payout", 0) or 0) - int(entry_bonus)
                ),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=sete_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

        return spin_message, final_grid

    async def _animate_roleta2_respin(
        self,
        source_message: discord.Message,
        *,
        current_grid: list[list[str]],
        outcome: dict[str, object],
        balance_text: str,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
        respin_index: int,
        spin_message: discord.Message | None = None,
        skip_event: asyncio.Event | None = None,
    ) -> tuple[discord.Message | None, list[list[str]]]:
        final_grid = [list(row) for row in outcome.get("grid", [])]
        preview_raw = outcome.get("preview_grid")
        target_grid = [list(row) for row in preview_raw] if isinstance(preview_raw, list) else final_grid
        if skip_event is not None and skip_event.is_set():
            return spin_message, final_grid

        column_order = _slots_respin_column_order(respin_index)
        spinning_columns: set[int] = set()
        for index, column in enumerate(column_order):
            spinning_columns.add(column)
            frame = _slots_respin_start_grid(
                current_grid,
                spinning_columns=spinning_columns,
            )
            view = self._make_roleta2_respin_view(
                self._render_roleta2_board(frame),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta,
                bonus_delta=bonus_delta,
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            if not await self._wait_game_animation_delay(
                skip_event,
                ROLETA2_RESPIN_START_DELAYS[index],
            ):
                return spin_message, final_grid

        stopped: set[int] = set()
        for column, delay in zip(column_order, ROLETA2_COLUMN_DELAYS):
            if not await self._wait_game_animation_delay(skip_event, delay):
                return spin_message, final_grid
            stopped.add(column)
            frame = self._roleta2_display_grid(target_grid, stopped_columns=stopped)
            view = self._make_roleta2_respin_view(
                self._render_roleta2_board(frame),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta,
                bonus_delta=bonus_delta,
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            if skip_event is not None and skip_event.is_set():
                return spin_message, final_grid

        slip_column = outcome.get("slip_column")
        if isinstance(preview_raw, list) and isinstance(slip_column, int):
            escorredio_normal_delta = normal_delta + int(
                outcome.get("escorredio_normal_payout", 0) or 0
            )
            escorredio_bonus_delta = bonus_delta + int(
                outcome.get("escorredio_bonus_payout", 0) or 0
            )
            preview_view = self._make_roleta2_effect_view(
                title="Escorredio",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(target_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY):
                return spin_message, final_grid
            slipping = self._roleta2_display_grid(
                target_grid,
                stopped_columns={0, 1, 2},
                spinning_column=max(0, min(2, int(slip_column))),
            )
            slipping_view = self._make_roleta2_effect_view(
                title="Escorredio...",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(slipping),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, 0.76):
                return spin_message, final_grid

        if bool(outcome.get("has_deja_vu")):
            deja_view = self._make_roleta2_effect_view(
                title="Déjà vu",
                title_emoji="🔁",
                color_theme="deja_vu",
                summary=str(outcome.get("deja_vu_summary") or ""),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta + int(outcome.get("normal_payout", 0) or 0),
                bonus_delta=bonus_delta + int(outcome.get("bonus_payout", 0) or 0),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=deja_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

        if bool(outcome.get("has_sete_solitario")):
            sete_view = self._make_roleta2_effect_view(
                title="7 solitário",
                title_emoji=SLOT_EMOJIS[SLOT_SEVEN],
                color_theme=SLOT_SEVEN,
                summary=str(outcome.get("sete_solitario_summary") or "Veio apenas um 7"),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta + int(outcome.get("normal_payout", 0) or 0),
                bonus_delta=bonus_delta + int(outcome.get("bonus_payout", 0) or 0),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=sete_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

        return spin_message, final_grid

    async def _animate_roleta2_sete_solitario_respin(
        self,
        source_message: discord.Message,
        *,
        current_grid: list[list[str]],
        outcome: dict[str, object],
        locked_column: int,
        balance_text: str,
        footer_text: str,
        normal_delta: int,
        bonus_delta: int,
        spin_message: discord.Message | None = None,
        skip_event: asyncio.Event | None = None,
    ) -> tuple[discord.Message | None, list[list[str]]]:
        final_grid = [list(row) for row in outcome.get("grid", [])]
        preview_raw = outcome.get("preview_grid")
        target_grid = [list(row) for row in preview_raw] if isinstance(preview_raw, list) else final_grid
        if skip_event is not None and skip_event.is_set():
            return spin_message, final_grid

        locked = max(0, min(2, int(locked_column)))
        reroll_columns = tuple(column for column in range(3) if column != locked)
        stopped: set[int] = {locked}

        # O 7 preserva sua coluna inteira. As outras duas voltam a girar juntas,
        # então o jogador vê imediatamente qual parte do tabuleiro ficou travada.
        frame = self._roleta2_display_grid(target_grid, stopped_columns=stopped)
        spinning_view = self._make_roleta2_effect_view(
            title="7 solitário...",
            title_emoji=SLOT_EMOJIS[SLOT_SEVEN],
            color_theme=SLOT_SEVEN,
            summary="Veio apenas um 7",
            board=self._render_roleta2_board(frame),
            balance_text=balance_text,
            footer_text=footer_text,
            normal_delta=normal_delta,
            bonus_delta=bonus_delta,
        )
        rendered = await self._render_or_replace_game_message(
            source_message,
            spin_message,
            view=spinning_view,
            final=False,
            cancel_event=skip_event,
        )
        if rendered is not None:
            spin_message = rendered

        for column in reroll_columns:
            if not await self._wait_game_animation_delay(
                skip_event,
                ROLETA2_COLUMN_DELAYS[column],
            ):
                return spin_message, final_grid
            stopped.add(column)
            frame = self._roleta2_display_grid(target_grid, stopped_columns=stopped)
            stopping_view = self._make_roleta2_effect_view(
                title="7 solitário...",
                title_emoji=SLOT_EMOJIS[SLOT_SEVEN],
                color_theme=SLOT_SEVEN,
                summary="Veio apenas um 7",
                board=self._render_roleta2_board(frame),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta,
                bonus_delta=bonus_delta,
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=stopping_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            if skip_event is not None and skip_event.is_set():
                return spin_message, final_grid

        slip_column = outcome.get("slip_column")
        if isinstance(preview_raw, list) and isinstance(slip_column, int):
            escorredio_normal_delta = normal_delta + int(
                outcome.get("escorredio_normal_payout", 0) or 0
            )
            escorredio_bonus_delta = bonus_delta + int(
                outcome.get("escorredio_bonus_payout", 0) or 0
            )
            preview_view = self._make_roleta2_effect_view(
                title="Escorredio",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(target_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY):
                return spin_message, final_grid
            slipping = self._roleta2_display_grid(
                target_grid,
                stopped_columns={0, 1, 2},
                spinning_column=max(0, min(2, int(slip_column))),
            )
            slipping_view = self._make_roleta2_effect_view(
                title="Escorredio...",
                title_emoji=SLOT_EMOJIS[SLOT_BANANA],
                color_theme=SLOT_BANANA,
                summary=str(outcome.get("escorredio_summary") or ""),
                board=self._render_roleta2_board(slipping),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=escorredio_normal_delta,
                bonus_delta=escorredio_bonus_delta,
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
            if not await self._wait_game_animation_delay(skip_event, 0.76):
                return spin_message, final_grid

        if bool(outcome.get("has_deja_vu")):
            deja_view = self._make_roleta2_effect_view(
                title="Déjà vu",
                title_emoji="🔁",
                color_theme="deja_vu",
                summary=str(outcome.get("deja_vu_summary") or ""),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta + int(outcome.get("normal_payout", 0) or 0),
                bonus_delta=bonus_delta + int(outcome.get("bonus_payout", 0) or 0),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=deja_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

        if bool(outcome.get("has_sete_solitario")):
            sete_view = self._make_roleta2_effect_view(
                title="7 solitário",
                title_emoji=SLOT_EMOJIS[SLOT_SEVEN],
                color_theme=SLOT_SEVEN,
                summary=str(outcome.get("sete_solitario_summary") or "Veio apenas um 7"),
                board=self._render_roleta2_board(final_grid),
                balance_text=balance_text,
                footer_text=footer_text,
                normal_delta=normal_delta + int(outcome.get("normal_payout", 0) or 0),
                bonus_delta=bonus_delta + int(outcome.get("bonus_payout", 0) or 0),
            )
            rendered = await self._render_or_replace_game_message(
                source_message,
                spin_message,
                view=sete_view,
                final=False,
                cancel_event=skip_event,
            )
            if rendered is not None:
                spin_message = rendered
            await self._wait_game_animation_delay(skip_event, ROLETA2_EFFECT_READ_DELAY)

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
        outcomes: list[dict[str, object]] = []
        deja_vu_count = 0
        forced_kind: str | None = None
        excluded_kinds: tuple[str, ...] = ()
        followup_mode: str | None = None
        effect_steps = 0
        while True:
            allow_deja_vu = deja_vu_count < ROLETA2_DEJA_VU_CHAIN_LIMIT
            grid_override: list[list[str]] | None = None
            locked_column: int | None = None
            if followup_mode == "sete_solitario" and outcomes:
                source_grid = [list(row) for row in outcomes[-1].get("grid", [])]
                for _ in range(64):
                    candidate_grid, candidate_locked = _slots_generate_sete_solitario_respin(
                        random,
                        source_grid,
                    )
                    if allow_deja_vu or "deja_vu" not in _slots_matching_kinds(candidate_grid):
                        grid_override = candidate_grid
                        locked_column = candidate_locked
                        break
                if grid_override is None:
                    grid_override, locked_column = _slots_generate_sete_solitario_respin(
                        random,
                        source_grid,
                    )
                forced_kind = None
                excluded_kinds = ()

            outcome = self._roll_roleta2_outcome(
                deja_vu_index=deja_vu_count + 1,
                allow_deja_vu=allow_deja_vu,
                forced_kind=forced_kind,
                excluded_kinds=excluded_kinds,
                grid_override=grid_override,
            )
            if followup_mode is not None:
                outcome["respin_mode"] = followup_mode
            if locked_column is not None:
                outcome["locked_column"] = int(locked_column)
            outcomes.append(outcome)
            effect_steps += 1

            has_deja_vu = bool(outcome.get("has_deja_vu"))
            has_sete_solitario = bool(outcome.get("has_sete_solitario"))
            if not has_deja_vu and not has_sete_solitario:
                break
            if effect_steps >= ROLETA2_EFFECT_CHAIN_LIMIT:
                # Failsafe técnico compartilhado: fecha a rodada com um estágio
                # terminal de verdade, sem deixar um Déjà vu/7 solitário visual
                # sem a animação que ele prometeu.
                terminal_outcome: dict[str, object] | None = None
                if has_sete_solitario:
                    source_grid = [list(row) for row in outcome.get("grid", [])]
                    for _ in range(512):
                        terminal_grid, terminal_locked = _slots_generate_sete_solitario_respin(
                            random,
                            source_grid,
                        )
                        if "deja_vu" in _slots_matching_kinds(terminal_grid):
                            continue
                        if _slots_has_escorredio(terminal_grid):
                            continue
                        if _slots_is_sete_solitario(terminal_grid):
                            continue
                        terminal_outcome = self._roll_roleta2_outcome(
                            deja_vu_index=deja_vu_count + 1,
                            allow_deja_vu=False,
                            grid_override=terminal_grid,
                        )
                        terminal_outcome["respin_mode"] = "sete_solitario"
                        terminal_outcome["locked_column"] = int(terminal_locked)
                        break
                else:
                    for _ in range(64):
                        candidate_terminal = self._roll_roleta2_outcome(
                            deja_vu_index=deja_vu_count + 1,
                            allow_deja_vu=False,
                            forced_kind="loss",
                            excluded_kinds=("deja_vu", "escorredio"),
                        )
                        if bool(candidate_terminal.get("has_sete_solitario")):
                            continue
                        terminal_outcome = candidate_terminal
                        terminal_outcome["respin_mode"] = "deja_vu"
                        break
                if terminal_outcome is not None:
                    outcomes.append(terminal_outcome)
                break

            if has_deja_vu:
                deja_vu_count += 1
                followup_mode = "deja_vu"
                if deja_vu_count >= ROLETA2_DEJA_VU_CHAIN_LIMIT:
                    forced_kind = None
                    excluded_kinds = ("deja_vu",)
                else:
                    forced_kind, excluded_kinds = _slots_deja_vu_followup_policy(random)
                continue

            followup_mode = "sete_solitario"
            forced_kind = None
            excluded_kinds = ()

        paid_entry = self._entry_paid_amount(entry_spend, entry_cost)
        if isinstance(entry_spend, dict):
            entry_normal = max(0, int(entry_spend.get("chips", 0) or 0))
            entry_bonus = max(0, int(entry_spend.get("bonus", 0) or 0))
        else:
            entry_normal = paid_entry
            entry_bonus = 0

        balance_text = self._format_compact_chip_balance(guild.id, actor.id)
        running_normal_payout = 0
        running_bonus_payout = 0
        final_grid = [list(row) for row in outcomes[-1].get("grid", [])]
        try:
            first_outcome = outcomes[0]
            spin_message, final_grid = await self._animate_roleta2_spin(
                source_message,
                outcome=first_outcome,
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
                entry_normal=entry_normal,
                entry_bonus=entry_bonus,
                spin_message=spin_message,
                skip_event=skip_event,
            )
            running_normal_payout += max(0, int(first_outcome.get("normal_payout", 0) or 0))
            running_bonus_payout += max(0, int(first_outcome.get("bonus_payout", 0) or 0))

            for outcome_index, next_outcome in enumerate(outcomes[1:], start=1):
                previous_outcome = outcomes[outcome_index - 1]
                if bool(previous_outcome.get("has_sete_solitario")):
                    locked_column = previous_outcome.get("sete_solitario_column")
                    if not isinstance(locked_column, int):
                        locked_column = _slots_sete_solitario_column(final_grid)
                    if not isinstance(locked_column, int):
                        locked_column = 1
                    spin_message, final_grid = await self._animate_roleta2_sete_solitario_respin(
                        source_message,
                        current_grid=final_grid,
                        outcome=next_outcome,
                        locked_column=locked_column,
                        balance_text=balance_text,
                        footer_text=footer_text,
                        normal_delta=running_normal_payout - entry_normal,
                        bonus_delta=running_bonus_payout - entry_bonus,
                        spin_message=spin_message,
                        skip_event=skip_event,
                    )
                else:
                    deja_respin_index = max(
                        1,
                        int(previous_outcome.get("deja_vu_index", 0) or 1),
                    )
                    spin_message, final_grid = await self._animate_roleta2_respin(
                        source_message,
                        current_grid=final_grid,
                        outcome=next_outcome,
                        balance_text=balance_text,
                        footer_text=footer_text,
                        normal_delta=running_normal_payout - entry_normal,
                        bonus_delta=running_bonus_payout - entry_bonus,
                        respin_index=deja_respin_index,
                        spin_message=spin_message,
                        skip_event=skip_event,
                    )
                running_normal_payout += max(
                    0, int(next_outcome.get("normal_payout", 0) or 0)
                )
                running_bonus_payout += max(
                    0, int(next_outcome.get("bonus_payout", 0) or 0)
                )
        except Exception:
            logging.getLogger("gincana.roleta2").exception(
                "falha visual na animação/respin da roleta2 | guild=%s user=%s",
                guild.id,
                actor.id,
            )
            final_grid = [list(row) for row in outcomes[-1].get("grid", [])]
        await self._release_game_animation_session(guild.id, session_id)

        if round_sequence is not None:
            await self._wait_for_game_round_commit_turn(guild.id, actor.id, round_sequence)
        async with self._game_user_state_lock(guild.id, actor.id):
            commit_start_normal, commit_start_bonus = self._current_game_chip_balances(guild.id, actor.id)
            successful_outcomes = [item for item in outcomes if bool(item.get("success"))]
            display_outcome, display_color_theme = _slots_final_presentation(outcomes)
            normal_payout = sum(
                max(0, int(item.get("normal_payout", 0) or 0)) for item in outcomes
            )
            bonus_payout = sum(
                max(0, int(item.get("bonus_payout", 0) or 0)) for item in outcomes
            )
            free_spins_awarded = sum(
                max(0, int(item.get("free_spins", 0) or 0)) for item in outcomes
            )
            gross_payout = normal_payout + bonus_payout
            round_success = bool(successful_outcomes)
            round_jackpot = any(bool(item.get("jackpot")) for item in outcomes)
            round_premium = any(bool(item.get("premium")) for item in outcomes)
            round_partial = round_success and not any(
                bool(item.get("success")) and not bool(item.get("partial"))
                for item in outcomes
            )
            summary_lines: list[str] = [str(display_outcome.get("summary") or "").strip()]
            summary_lines.extend(self._roleta2_historical_modifier_lines(outcomes))
            deja_vu_total = sum(int(item.get("deja_vu_payout", 0) or 0) for item in outcomes)
            if deja_vu_count > 0:
                summary_lines.append(
                    f"-# 🔁 Déjà vu ×{deja_vu_count} · +{deja_vu_total} {self._CHIP_BONUS_EMOJI}"
                )

            await self.db.add_user_game_stat(guild.id, actor.id, "roleta2_spins", 1)
            weekly_points = 2
            if round_success:
                if round_jackpot:
                    weekly_points = 12
                elif round_premium:
                    weekly_points = 8
                elif round_partial:
                    weekly_points = 3
                else:
                    weekly_points = 4
            await self._record_game_played(guild.id, actor.id, weekly_points=weekly_points)

            if normal_payout > 0:
                await self._change_user_chips(
                    guild.id,
                    actor.id,
                    normal_payout,
                    reason=f"Roleta2 · {display_outcome.get('title')}",
                )
            if bonus_payout > 0:
                await self._change_user_bonus_chips(
                    guild.id,
                    actor.id,
                    bonus_payout,
                    reason=f"Roleta2 · {display_outcome.get('title')}",
                )

            reward_spin_state: dict[str, float | int] | None = None
            if free_spins_awarded > 0:
                _granted_spins, reward_spin_state = await self._grant_roleta2_reward_spins(
                    guild.id,
                    actor.id,
                    free_spins_awarded,
                )

            if round_jackpot:
                await self.db.add_user_game_stat(guild.id, actor.id, "roleta2_jackpots", 1)
                await self._grant_weekly_points(guild.id, actor.id, 18)
            elif round_premium:
                await self._grant_weekly_points(guild.id, actor.id, 6)

            race_won: bool | None = round_success
            race_payout = gross_payout
            if not round_success:
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
                valid=not round_partial,
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
            if reward_spin_state is not None:
                is_staff = isinstance(actor, discord.Member) and self._is_staff_member(actor)
                footer_text = self._roleta2_footer_text(
                    state=reward_spin_state,
                    is_staff=is_staff,
                )
            result_view = self._make_roleta2_result_view(
                title=str(display_outcome.get("title") or "Roleta 2"),
                title_emoji=str(display_outcome.get("title_emoji") or "🎰"),
                color_theme=display_color_theme,
                summary="\n".join(line for line in summary_lines if line),
                board=self._render_roleta2_board(final_grid),
                balance_text=self._format_game_balance_values(display_normal, display_bonus),
                success=round_success,
                premium=round_premium,
                footer_text=footer_text,
                normal_delta=normal_result_delta,
                bonus_delta=bonus_result_delta,
                free_spins=free_spins_awarded,
            )
            first_game_unlocked = await self._unlock_achievement(guild.id, actor.id, "first_game")
            roulette_achievements = await self._record_roulette_achievement_result(
                guild.id,
                actor.id,
                jackpot=round_jackpot,
                lost=not round_success,
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
                spin_state = await self._sync_roleta2_spin_window(guild.id, message.author.id)
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
                        can_spin, spin_state = await self._reserve_roleta2_spin_state(
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
