from __future__ import annotations

import ast
import asyncio
import random
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SLOTS_PATH = ROOT / "cogs" / "games" / "games" / "slots.py"
COG_PATH = ROOT / "cogs" / "games" / "cog.py"
GAMES_PATH = ROOT / "cogs" / "games" / "__init__.py"
ROUTER_PATH = ROOT / "cogs" / "games" / "handlers" / "message_router.py"
BASE_PATH = ROOT / "cogs" / "games" / "services" / "base.py"
DB_PATH = ROOT / "db.py"


class GamesRoleta2SlotsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SLOTS_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.namespace: dict[str, object] = {"random": random, "time": time}
        for node in cls.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef)):
                module = ast.Module(body=[node], type_ignores=[])
                ast.fix_missing_locations(module)
                exec(compile(module, str(SLOTS_PATH), "exec"), cls.namespace)

    def _async_function_source(self, name: str) -> str:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                segment = ast.get_source_segment(self.source, node)
                self.assertIsNotNone(segment)
                return str(segment)
        self.fail(f"função não encontrada: {name}")

    def _class_method(self, name: str):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                namespace = dict(self.namespace)
                module = ast.Module(body=[node], type_ignores=[])
                ast.fix_missing_locations(module)
                exec(compile(module, str(SLOTS_PATH), "exec"), namespace)
                return namespace[name]
        self.fail(f"método não encontrado: {name}")

    def test_uses_the_exact_custom_emojis(self) -> None:
        emojis = self.namespace["SLOT_EMOJIS"]
        self.assertEqual(emojis["banana"], "<:slot_banana:1543757649911353404>")
        self.assertEqual(emojis["framboesa"], "<:slot_framboesa:1543757628520271964>")
        self.assertEqual(emojis["cereja"], "<:slot_cereja:1543757610925162516>")
        self.assertEqual(emojis["bar"], "<:slot_bar:1543757593078403072>")
        self.assertEqual(emojis["seven"], "<:slot_7:1543757577496821800>")
        self.assertEqual(emojis["spinning"], "<a:slot_girando:1543757558374994021>")

    def test_outcome_weights_are_total_and_deja_vu_is_ten_percent(self) -> None:
        weights = dict(self.namespace["ROLETA2_OUTCOME_WEIGHTS"])
        self.assertEqual(sum(weights.values()), 10_000)
        self.assertEqual(weights["deja_vu"], 1_000)
        self.assertEqual(weights["loss"], 4_400)

    def test_internal_probabilities_are_fixed_and_total(self) -> None:
        expected = {
            "ROLETA2_LINE_TYPE_WEIGHTS": {"horizontal": 7_500, "diagonal": 2_500},
            "ROLETA2_BANANA_SPLIT_AXIS_WEIGHTS": {"horizontal": 7_500, "vertical": 2_500},
            "ROLETA2_COLHEITA_FRUIT_WEIGHTS": {
                "banana": 4_500,
                "framboesa": 3_500,
                "cereja": 2_000,
            },
            "ROLETA2_SCATTERED_SEVEN_WEIGHTS": {3: 7_500, 4: 2_000, 5: 500},
            "ROLETA2_LOSS_SEVEN_WEIGHTS": {0: 8_500, 1: 1_500},
        }
        for name, values in expected.items():
            configured = dict(self.namespace[name])
            self.assertEqual(configured, values)
            self.assertEqual(sum(configured.values()), 10_000)

    def test_special_effect_transition_chances_are_exact(self) -> None:
        self.assertEqual(self.namespace["ROLETA2_DEJA_VU_TO_ESCORREDIO_CHANCE"], 0.25)
        self.assertEqual(self.namespace["ROLETA2_ESCORREDIO_TO_DEJA_VU_CHANCE"], 0.35)
        triggers = self.namespace["_slots_effect_triggers_followup"]
        policy = self.namespace["_slots_deja_vu_followup_policy"]

        class FixedRng:
            def __init__(self, value: float):
                self.value = value

            def random(self) -> float:
                return self.value

        self.assertTrue(triggers("deja_vu", FixedRng(0.249999)))
        self.assertFalse(triggers("deja_vu", FixedRng(0.25)))
        self.assertTrue(triggers("escorredio", FixedRng(0.349999)))
        self.assertFalse(triggers("escorredio", FixedRng(0.35)))
        self.assertFalse(
            triggers("escorredio", FixedRng(0.0), allow_deja_vu=False)
        )
        self.assertEqual(policy(FixedRng(0.10)), ("escorredio", ()))
        # Escorredio sai do sorteio dos 75% restantes; assim a transição total
        # Déjà vu -> Escorredio continua sendo exatamente 25%.
        self.assertEqual(policy(FixedRng(0.90)), (None, ("escorredio",)))

    def test_fixed_probabilities_keep_the_base_return_in_the_intended_band(self) -> None:
        outcome_weights = dict(self.namespace["ROLETA2_OUTCOME_WEIGHTS"])
        payouts = dict(self.namespace["ROLETA2_PAYOUTS"])
        scale = int(self.namespace["ROLETA2_PROBABILITY_SCALE"])
        expected_gross = 0.0
        for kind, weight in outcome_weights.items():
            if kind == "colheita":
                continue
            normal, bonus = payouts.get(kind, (0, 0))
            expected_gross += (weight / scale) * (normal + bonus)
        colheita_values = {"banana": 15, "framboesa": 30, "cereja": 20}
        colheita_average = sum(
            (weight / scale) * colheita_values[fruit]
            for fruit, weight in self.namespace["ROLETA2_COLHEITA_FRUIT_WEIGHTS"]
        )
        expected_gross += (outcome_weights["colheita"] / scale) * colheita_average
        return_rate = expected_gross / int(self.namespace["ROLETA2_COST"])
        self.assertAlmostEqual(expected_gross, 11.34, places=2)
        self.assertGreaterEqual(return_rate, 0.75)
        self.assertLessEqual(return_rate, 0.76)

    def test_every_generated_grid_matches_its_selected_result(self) -> None:
        generate = self.namespace["_slots_generate_grid"]
        detect = self.namespace["_slots_detect_kind"]
        has_escorredio = self.namespace["_slots_has_escorredio"]
        rng = random.Random(20260830)
        kinds = (
            "sete_pecados",
            "jackpot",
            "bar_triplo",
            "bar_abriu_as_7",
            "deja_vu",
            "banana_split",
            "colheita",
            "setes_espalhados",
            "faltou_um_sete",
            "loss",
        )
        matching = self.namespace["_slots_matching_kinds"]
        for kind in kinds:
            for _ in range(100):
                grid = generate(kind, rng)
                if kind == "deja_vu":
                    self.assertEqual(matching(grid), {"deja_vu"}, grid)
                else:
                    self.assertEqual(detect(grid), kind, (kind, grid))
                if kind == "loss":
                    self.assertFalse(has_escorredio(grid), grid)
                    self.assertIn(sum(cell == "seven" for row in grid for cell in row), {0, 1})
                if kind == "setes_espalhados":
                    self.assertIn(sum(cell == "seven" for row in grid for cell in row), {3, 4, 5})

    def test_forced_internal_variants_survive_grid_validation(self) -> None:
        generate = self.namespace["_slots_generate_grid"]
        lines = self.namespace["_SLOT_LINES"]
        rng = random.Random(31082026)

        for kind, values in (
            ("jackpot", ["seven"] * 3),
            ("bar_triplo", ["bar"] * 3),
        ):
            for line in lines:
                grid = generate(kind, rng, {"line": line})
                actual = [grid[row][column] for row, column in line]
                self.assertEqual(actual, values, (kind, line, grid))

        banana_split_lines = self.namespace["_SLOT_BANANA_SPLIT_LINES"]
        for line in banana_split_lines:
            grid = generate("banana_split", rng, {"line": line})
            actual = [grid[row][column] for row, column in line]
            self.assertEqual(actual, ["banana", "cereja", "banana"], (line, grid))

        for fruit in ("banana", "framboesa", "cereja"):
            line = lines[1]
            grid = generate("colheita", rng, {"line": line, "fruit": fruit})
            self.assertEqual([grid[row][column] for row, column in line], [fruit] * 3)

        for count in (3, 4, 5):
            grid = generate("setes_espalhados", rng, {"seven_count": count})
            self.assertEqual(sum(cell == "seven" for row in grid for cell in row), count)
        for count in (0, 1):
            grid = generate("loss", rng, {"seven_count": count})
            self.assertEqual(sum(cell == "seven" for row in grid for cell in row), count)

    def test_escorredio_rerolls_only_one_column_and_deja_vu_is_an_explicit_followup(self) -> None:
        generate = self.namespace["_slots_generate_escorredio"]
        has_escorredio = self.namespace["_slots_has_escorredio"]
        matching = self.namespace["_slots_matching_kinds"]
        rng = random.Random(1543757558374994021)
        combined_results: set[str] = set()
        for _ in range(100):
            preview, final, column = generate(rng)
            self.assertTrue(has_escorredio(preview), preview)
            self.assertFalse(has_escorredio(final), final)
            self.assertIn(column, {0, 1, 2})
            for other_column in {0, 1, 2} - {column}:
                self.assertEqual(
                    [preview[row][other_column] for row in range(3)],
                    [final[row][other_column] for row in range(3)],
                )
            combined_results.update(matching(final))
            self.assertNotIn("deja_vu", matching(final))
        self.assertTrue(combined_results & {"colheita", "banana_split", "bar_triplo"})

        forced_deja_rng = random.Random(20260831)
        for _ in range(100):
            preview, final, column = generate(forced_deja_rng, force_deja_vu=True)
            self.assertTrue(has_escorredio(preview), preview)
            self.assertFalse(has_escorredio(final), final)
            self.assertIn(column, {0, 2})
            self.assertEqual(matching(final), {"deja_vu"}, final)
            for other_column in {0, 1, 2} - {column}:
                self.assertEqual(
                    [preview[row][other_column] for row in range(3)],
                    [final[row][other_column] for row in range(3)],
                )

    def test_command_and_bare_trigger_are_both_wired(self) -> None:
        games = GAMES_PATH.read_text(encoding="utf-8")
        router = ROUTER_PATH.read_text(encoding="utf-8")
        cog = COG_PATH.read_text(encoding="utf-8")
        self.assertIn('@dcommands.command(name="roleta2")', games)
        self.assertIn('handler_name="_handle_roleta2_trigger"', games)
        self.assertIn('content="roleta2"', games)
        self.assertIn('"_handle_roleta2_trigger"', router)
        self.assertIn("GincanaSlotsMixin", cog)

    def test_roleta2_has_independent_limits_daily_bonus_and_safe_spam_flow(self) -> None:
        trigger = self._async_function_source("_run_roleta2_trigger_locked")
        animation = self._async_function_source("_animate_roleta2_spin")
        execution = self._async_function_source("_execute_roleta2_round")
        daily_grant = self._async_function_source("_grant_daily_roleta2_spins")
        base = BASE_PATH.read_text(encoding="utf-8")
        self.assertEqual(self.namespace["ROLETA2_SPIN_LIMIT"], 5)
        self.assertEqual(self.namespace["ROLETA2_WINDOW_SECONDS"], 6 * 60 * 60)
        self.assertEqual(self.namespace["ROLETA2_DAILY_EXTRA_SPINS"], 2)
        self.assertEqual(self.namespace["ROLETA2_DAILY_EXTRA_CAP"], 2)
        self.assertIn("_sync_roleta2_spin_window", trigger)
        self.assertIn("_reserve_roleta2_spin_state", trigger)
        self.assertNotIn("_sync_carta_spin_window", trigger)
        self.assertNotIn("_reserve_carta_spin_state", trigger)
        self.assertIn('doc["roleta2_bonus_spins"]', daily_grant)
        self.assertIn("_grant_daily_roleta2_spins", base)
        self.assertIn("<:slot_cereja:1543757610925162516>", base)
        self.assertIn("roleta2_spins_granted", base)
        self.assertIn("_confirm_game_negative_from_message", trigger)
        self.assertIn("_issue_game_round_sequence", trigger)
        self.assertIn("_activate_game_animation_session", trigger)
        self.assertIn("skip_event", animation)
        self.assertIn("_wait_for_game_round_commit_turn", execution)
        self.assertIn("_deliver_game_result", execution)
        self.assertIn("_complete_game_round_delivery_sequence", execution)

    def test_independent_window_grants_two_daily_spins_and_resets_to_five(self) -> None:
        method_names = (
            "_roleta2_window_total",
            "_sync_roleta2_spin_window",
            "_consume_roleta2_spin",
            "_grant_daily_roleta2_spins",
            "_grant_roleta2_reward_spins",
            "_reserve_roleta2_spin_state",
        )
        harness_type = type(
            "Roleta2WindowHarness",
            (),
            {name: self._class_method(name) for name in method_names},
        )

        class FakeDB:
            def __init__(self) -> None:
                self.doc: dict[str, object] = {}

            def _get_user_doc(self, _guild_id: int, _user_id: int) -> dict[str, object]:
                return dict(self.doc)

            async def _save_user_doc(
                self,
                _guild_id: int,
                _user_id: int,
                doc: dict[str, object],
            ) -> None:
                self.doc = dict(doc)

        async def scenario() -> None:
            harness = harness_type()
            harness.db = FakeDB()
            initial = await harness._sync_roleta2_spin_window(1, 2)
            self.assertEqual((initial["total"], initial["available"]), (5, 5))

            granted, boosted = await harness._grant_daily_roleta2_spins(1, 2)
            self.assertEqual(granted, 2)
            self.assertEqual((boosted["total"], boosted["available"]), (7, 7))

            allowed, consumed = await harness._reserve_roleta2_spin_state(1, 2, is_staff=False)
            self.assertTrue(allowed)
            self.assertEqual((consumed["used"], consumed["available"]), (1, 6))

            free_granted, rewarded = await harness._grant_roleta2_reward_spins(1, 2, 1)
            self.assertEqual(free_granted, 1)
            self.assertEqual((rewarded["rewards"], rewarded["total"], rewarded["available"]), (1, 8, 7))

            duplicate_grant, unchanged = await harness._grant_daily_roleta2_spins(1, 2)
            self.assertEqual((duplicate_grant, unchanged["bonus"]), (0, 2))

            harness.db.doc["roleta2_window_started_at"] = (
                time.time() - int(self.namespace["ROLETA2_WINDOW_SECONDS"]) - 1
            )
            reset = await harness._sync_roleta2_spin_window(1, 2)
            self.assertEqual(
                (reset["used"], reset["bonus"], reset["rewards"], reset["total"], reset["available"]),
                (0, 0, 0, 5, 5),
            )

        asyncio.run(scenario())

    def test_loss_copy_contains_only_the_five_approved_titles(self) -> None:
        self.assertEqual(
            set(self.namespace["ROLETA2_LOSS_SUMMARIES"]),
            {
                "Você ganhou... nada!",
                "Foi quase hein",
                "Nenhuma combinação",
                "7 solitário",
                "Não veio nada",
            },
        )
        self.assertEqual(self.namespace["ROLETA2_LOSS_SUMMARIES"]["7 solitário"], "Veio apenas um 7")
        self.assertEqual(
            self.namespace["ROLETA2_LOSS_SUMMARIES"]["Você ganhou... nada!"],
            "Uau parece que não veio nada, incrível",
        )
        self.assertNotIn("máquina", self.namespace["ROLETA2_LOSS_SUMMARIES"]["Você ganhou... nada!"])
        picker = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_pick_roleta2_loss_copy"
        )
        picker_source = str(ast.get_source_segment(self.source, picker))
        self.assertIn('_last_game_loss_titles["roleta2"]', picker_source)
        self.assertIn('f"Duas {pair_emoji} combinaram na {line_number} linha"', picker_source)

    def test_almost_copy_uses_the_actual_horizontal_line(self) -> None:
        matching_pair = self.namespace["_slots_matching_pair"]
        grids = (
            (["cereja", "cereja", "banana"], 0),
            (["banana", "bar", "bar"], 1),
            (["framboesa", "banana", "banana"], 2),
        )
        base = [
            ["banana", "framboesa", "bar"],
            ["cereja", "banana", "framboesa"],
            ["bar", "cereja", "framboesa"],
        ]
        for row_values, expected_row in grids:
            grid = [list(row) for row in base]
            grid[expected_row] = list(row_values)
            match = matching_pair(grid)
            self.assertIsNotNone(match)
            self.assertEqual(match[1], expected_row)

    def test_almost_copy_names_the_actual_matching_symbol(self) -> None:
        picker = self._class_method("_pick_roleta2_loss_copy")
        harness = type(
            "Roleta2LossHarness",
            (),
            {
                "_pick_roleta2_loss_copy": picker,
                "_ensure_game_animation_runtime": lambda self: None,
            },
        )()
        harness._last_game_loss_titles = {}
        grid = [
            ["cereja", "cereja", "banana"],
            ["framboesa", "bar", "banana"],
            ["banana", "framboesa", "bar"],
        ]
        title, summary = harness._pick_roleta2_loss_copy(grid)
        self.assertEqual(title, "Foi quase hein")
        self.assertEqual(
            summary,
            "Duas <:slot_cereja:1543757610925162516> combinaram na 1ª linha",
        )

    def test_jackpot_copy_has_compact_title_and_actual_position(self) -> None:
        summary = self.namespace["_slots_jackpot_summary"]
        row_grid = [
            ["banana", "framboesa", "cereja"],
            ["seven", "seven", "seven"],
            ["cereja", "bar", "banana"],
        ]
        self.assertEqual(
            summary(row_grid),
            "Três <:slot_7:1543757577496821800> combinam na 2ª linha",
        )
        diagonal_grid = [
            ["seven", "banana", "framboesa"],
            ["cereja", "seven", "bar"],
            ["banana", "framboesa", "seven"],
        ]
        self.assertEqual(
            summary(diagonal_grid),
            "Três <:slot_7:1543757577496821800> combinam na diagonal principal",
        )
        roll_source = self._async_function_source("_execute_roleta2_round")
        _ = roll_source
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        source = str(ast.get_source_segment(self.source, roll))
        self.assertIn('"jackpot": "Jackpot!"', source)
        self.assertIn('"jackpot": _slots_jackpot_summary(grid)', source)

    def test_roleta2_copy_never_refers_to_a_machine(self) -> None:
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        roll_source = str(ast.get_source_segment(self.source, roll)).lower()
        loss_copy = repr(self.namespace["ROLETA2_LOSS_SUMMARIES"]).lower()
        self.assertNotIn("máquina", roll_source)
        self.assertNotIn("máquina", loss_copy)

    def test_board_uses_heading_rows_to_render_larger_slot_emojis(self) -> None:
        render = self._class_method("_render_roleta2_board")
        harness = type("Roleta2BoardHarness", (), {"_render_roleta2_board": render})()
        grid = [
            ["bar", "framboesa", "banana"],
            ["banana", "framboesa", "cereja"],
            ["cereja", "banana", "framboesa"],
        ]
        board = harness._render_roleta2_board(grid)
        rows = board.splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.startswith("# ") for row in rows))
        self.assertIn("<:slot_bar:1543757593078403072>", rows[0])

    def test_framboesa_colheita_pays_thirty_bonus_with_compact_copy(self) -> None:
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        roll_source = str(ast.get_source_segment(self.source, roll))
        self.assertIn("normal_payout, bonus_payout = 0, 30", roll_source)
        self.assertIn('summary = f"3 {fruit_emoji} renderam +30 {self._CHIP_BONUS_EMOJI}"', roll_source)
        self.assertIn('free_spins = 1', roll_source)
        self.assertIn(
            'summary = f"3 {fruit_emoji} fecharam uma Colheita e devolveram a entrada"',
            roll_source,
        )

    def test_bar_abriu_as_7_copy_uses_the_actual_slot_emojis(self) -> None:
        summary = self.namespace["_slots_bar_abriu_as_7_summary"]
        grid = [
            ["banana", "framboesa", "cereja"],
            ["bar", "seven", "bar"],
            ["cereja", "banana", "framboesa"],
        ]
        self.assertEqual(
            summary(grid),
            "Veio dois <:slot_bar:1543757593078403072> e um <:slot_7:1543757577496821800>",
        )

    def test_banana_split_copy_reports_the_actual_row_or_column(self) -> None:
        summary = self.namespace["_slots_banana_split_summary"]
        location = self.namespace["_slots_banana_split_location"]
        row_grid = [
            ["framboesa", "bar", "cereja"],
            ["banana", "cereja", "banana"],
            ["cereja", "framboesa", "bar"],
        ]
        self.assertEqual(location(row_grid), ("linha", 1))
        self.assertEqual(summary(row_grid), "Duas bananas ao redor da cereja na linha 2")

        column_grid = [
            ["framboesa", "banana", "bar"],
            ["cereja", "cereja", "framboesa"],
            ["bar", "banana", "cereja"],
        ]
        self.assertEqual(location(column_grid), ("coluna", 1))
        self.assertEqual(summary(column_grid), "Duas bananas ao redor da cereja na coluna 2")

    def test_faltou_um_sete_uses_the_new_compact_copy(self) -> None:
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        roll_source = str(ast.get_source_segment(self.source, roll))
        self.assertIn('"faltou_um_sete": "Se tivesse mais 1 hein"', roll_source)

    def test_result_title_emoji_and_theme_follow_the_causing_symbol(self) -> None:
        presentation = self.namespace["_slots_result_presentation"]
        raspberry_grid = [
            ["framboesa", "framboesa", "framboesa"],
            ["banana", "bar", "cereja"],
            ["cereja", "banana", "bar"],
        ]
        emoji, theme = presentation("colheita", raspberry_grid, "Colheita")
        self.assertEqual(emoji, "<:slot_framboesa:1543757628520271964>")
        self.assertEqual(theme, "framboesa")

        bar_grid = [["bar"] * 3, ["banana", "cereja", "framboesa"], ["cereja", "banana", "framboesa"]]
        emoji, theme = presentation("bar_triplo", bar_grid, "BAR triplo")
        self.assertEqual(emoji, "<:slot_bar:1543757593078403072>")
        self.assertEqual(theme, "bar")

        almost_grid = [
            ["cereja", "cereja", "banana"],
            ["banana", "bar", "framboesa"],
            ["framboesa", "banana", "bar"],
        ]
        emoji, theme = presentation("loss", almost_grid, "Foi quase hein")
        self.assertEqual(emoji, "<:slot_cereja:1543757610925162516>")
        self.assertEqual(theme, "cereja")

        emoji, theme = presentation("deja_vu", raspberry_grid, "Déjà vu")
        self.assertEqual((emoji, theme), ("🔁", "deja_vu"))

    def test_banana_colheita_grants_and_displays_one_free_spin(self) -> None:
        formatter = self._class_method("_format_roleta2_result_value")
        harness = type(
            "Roleta2ResultHarness",
            (),
            {
                "_format_roleta2_result_value": formatter,
                "_format_game_result_breakdown": lambda self, normal, bonus: f"money:{normal}:{bonus}",
            },
        )()
        self.assertEqual(harness._format_roleta2_result_value(0, 0, 1), "+1 giro grátis")
        self.assertEqual(
            harness._format_roleta2_result_value(0, 10, 1),
            "money:0:10 · +1 giro grátis",
        )
        execution = self._async_function_source("_execute_roleta2_round")
        self.assertIn('free_spins_awarded = sum(', execution)
        self.assertIn('_grant_roleta2_reward_spins(', execution)
        self.assertIn('free_spins=free_spins_awarded', execution)
        self.assertIn('_roleta2_footer_text(', execution)

    def test_result_color_prioritizes_special_theme_then_net_delta(self) -> None:
        result_view = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_make_roleta2_result_view"
        )
        source = str(ast.get_source_segment(self.source, result_view))
        self.assertIn("ROLETA2_THEME_COLORS.get", source)
        self.assertIn("net_delta = int(normal_delta) + int(bonus_delta)", source)
        self.assertLess(source.index("ROLETA2_THEME_COLORS.get"), source.index("net_delta ="))
        self.assertIn("discord.Color.green()", source)
        self.assertIn("discord.Color(OFF_COLOR)", source)
        self.assertIn("ROLETA2_NEUTRAL_COLOR", source)
        self.assertIn("title_emoji or '🎰'", source)

    def test_deja_vu_is_exclusive_per_board_but_scales_ten_twenty_thirty(self) -> None:
        matching = self.namespace["_slots_matching_kinds"]
        grid_matches = self.namespace["_slots_grid_matches_kind"]
        payout = self.namespace["_slots_deja_vu_payout"]
        combined_grid = [
            ["framboesa", "framboesa", "framboesa"],
            ["banana", "cereja", "banana"],
            ["bar", "bar", "bar"],
        ]
        matches = matching(combined_grid)
        self.assertIn("deja_vu", matches)
        self.assertIn("colheita", matches)
        self.assertIn("banana_split", matches)
        self.assertIn("bar_triplo", matches)
        self.assertFalse(grid_matches("deja_vu", combined_grid))
        exclusive_grid = self.namespace["_slots_generate_grid"](
            "deja_vu", random.Random(20260831)
        )
        self.assertEqual(matching(exclusive_grid), {"deja_vu"})
        self.assertTrue(grid_matches("deja_vu", exclusive_grid))
        self.assertEqual(payout(1), 10)
        self.assertEqual(payout(2), 20)
        self.assertEqual(payout(3), 30)
        self.assertEqual(payout(1) + payout(2), 30)
        self.assertEqual(self.namespace["ROLETA2_PAYOUTS"]["deja_vu"], (0, 10))
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        roll_source = str(ast.get_source_segment(self.source, roll))
        self.assertIn(
            "normal_payout, bonus_payout = 0, _slots_deja_vu_payout(deja_vu_index)",
            roll_source,
        )

    def test_deja_vu_respin_runs_inside_the_same_round_with_live_result(self) -> None:
        execution = self._async_function_source("_execute_roleta2_round")
        initial_spin_view = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_make_roleta2_spin_view"
        )
        respin = self._async_function_source("_animate_roleta2_respin")
        respin_view = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_make_roleta2_respin_view"
        )
        respin_view_source = str(ast.get_source_segment(self.source, respin_view))
        initial_spin_view_source = str(ast.get_source_segment(self.source, initial_spin_view))

        self.assertEqual(self.namespace["ROLETA2_DEJA_VU_CHAIN_LIMIT"], 20)
        self.assertEqual(tuple(self.namespace["ROLETA2_RESPIN_START_DELAYS"]), (0.66, 0.67, 0.67))
        self.assertAlmostEqual(self.namespace["ROLETA2_EFFECT_READ_DELAY"], 0.70, places=2)
        self.assertIn("while True:", execution)
        self.assertIn("_roll_roleta2_outcome", execution)
        self.assertIn("forced_kind=forced_kind", execution)
        self.assertIn("excluded_kinds=excluded_kinds", execution)
        self.assertIn("_slots_deja_vu_followup_policy(random)", execution)
        self.assertIn("_animate_roleta2_respin", execution)
        self.assertNotIn("_reserve_roleta2_spin_state", respin)
        self.assertNotIn("_try_consume_chips", respin)
        self.assertIn("column_order = _slots_respin_column_order(respin_index)", respin)
        self.assertIn("for index, column in enumerate(column_order)", respin)
        self.assertIn("for column, delay in zip(column_order, ROLETA2_COLUMN_DELAYS)", respin)
        self.assertIn("ROLETA2_RESPIN_START_DELAYS[index]", respin)
        self.assertIn("for respin_index, next_outcome in enumerate(outcomes[1:], start=1)", execution)
        self.assertIn("respin_index=respin_index", execution)
        self.assertIn('"🔁 Déjà vu..."', respin_view_source)
        self.assertIn('f"**Resultado:**', respin_view_source)
        self.assertNotIn('f"**Entrada:**', respin_view_source)
        self.assertNotIn("Prêmio máximo", initial_spin_view_source)
        self.assertNotIn("Prêmio máximo", respin_view_source)
        self.assertIn('ROLETA2_THEME_COLORS["deja_vu"]', respin_view_source)
        self.assertIn("ROLETA2_EFFECT_READ_DELAY", respin)


    def test_deja_vu_respin_direction_alternates_every_chain_step(self) -> None:
        order = self.namespace["_slots_respin_column_order"]
        self.assertEqual(order(1), (2, 1, 0))
        self.assertEqual(order(2), (0, 1, 2))
        self.assertEqual(order(3), (2, 1, 0))
        self.assertEqual(order(4), (0, 1, 2))

    def test_final_presentation_always_matches_the_terminal_board(self) -> None:
        select = self.namespace["_slots_final_presentation"]
        previous_colheita = {
            "kind": "deja_vu",
            "primary_kind": "colheita",
            "title": "Colheita",
            "summary": "3 <:slot_framboesa:1543757628520271964> renderam +30 <bonus>",
            "color_theme": "framboesa",
        }
        terminal_loss = {
            "kind": "loss",
            "primary_kind": "loss",
            "title": "Foi quase hein",
            "summary": "Duas <:slot_cereja:1543757610925162516> combinaram na 3ª linha",
            "color_theme": "cereja",
        }
        display, color_theme = select([previous_colheita, terminal_loss])
        self.assertIs(display, terminal_loss)
        self.assertEqual(display["title"], "Foi quase hein")
        self.assertEqual(display["summary"], "Duas <:slot_cereja:1543757610925162516> combinaram na 3ª linha")
        # A identidade temática acumulada pode permanecer sem trocar o texto
        # do tabuleiro terminal por um resultado antigo.
        self.assertEqual(color_theme, "framboesa")

    def test_final_deja_vu_modifier_is_compact_and_uses_bonus_emoji(self) -> None:
        execution = self._async_function_source("_execute_roleta2_round")
        self.assertIn('f"-# 🔁 Déjà vu ×{deja_vu_count} · +{deja_vu_total} {self._CHIP_BONUS_EMOJI}"', execution)
        self.assertNotIn("acumulados", execution)
        self.assertIn("display_outcome, display_color_theme = _slots_final_presentation(outcomes)", execution)
        self.assertNotIn("visible_outcomes[-1]", execution)

    def test_deja_vu_roll_never_pays_another_result_on_the_same_board(self) -> None:
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        source = str(ast.get_source_segment(self.source, roll))
        self.assertIn('elif kind == "deja_vu":', source)
        deja_branch = source[source.index('elif kind == "deja_vu":'):source.index('elif kind == "escorredio":')]
        self.assertIn('components = [result_details("deja_vu")]', deja_branch)
        self.assertNotIn('matches = _slots_matching_kinds(grid)', deja_branch)
        self.assertIn('normal_payout = sum(', source)
        self.assertIn('bonus_payout = sum(', source)
        self.assertIn('"has_deja_vu": has_deja_vu', source)

    def test_escorredio_is_a_combinable_modifier_with_a_read_pause(self) -> None:
        roll = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_roll_roleta2_outcome"
        )
        roll_source = str(ast.get_source_segment(self.source, roll))
        initial_animation = self._async_function_source("_animate_roleta2_spin")
        respin_animation = self._async_function_source("_animate_roleta2_respin")

        self.assertIn('elif kind == "escorredio":', roll_source)
        self.assertIn('components.append(result_details("escorredio"))', roll_source)
        self.assertIn('if "deja_vu" in matches:', roll_source)
        self.assertIn('components = [result_details("escorredio")]', roll_source)
        self.assertIn('force_deja_vu=trigger_deja_vu', roll_source)
        self.assertIn('"escorredio_normal_payout"', roll_source)
        self.assertIn('title="Escorredio"', initial_animation)
        self.assertIn("ROLETA2_EFFECT_READ_DELAY", initial_animation)
        self.assertIn('title="Escorredio"', respin_animation)
        self.assertIn("ROLETA2_EFFECT_READ_DELAY", respin_animation)

    def test_previous_respin_prizes_are_shown_as_compact_modifiers(self) -> None:
        formatter = self._class_method("_roleta2_historical_modifier_lines")
        harness = type(
            "Roleta2ModifierHarness",
            (),
            {
                "_roleta2_historical_modifier_lines": formatter,
                "_CHIP_GAIN_EMOJI": "<gain>",
                "_CHIP_BONUS_EMOJI": "<bonus>",
            },
        )()
        prior = {
            "components": (
                {
                    "kind": "banana_split",
                    "title": "Banana split",
                    "title_emoji": "🍌",
                    "normal_payout": 10,
                    "bonus_payout": 10,
                },
                {
                    "kind": "deja_vu",
                    "title": "Déjà vu",
                    "title_emoji": "🔁",
                    "normal_payout": 0,
                    "bonus_payout": 10,
                },
            )
        }
        terminal = {"components": ({"kind": "loss", "normal_payout": 0, "bonus_payout": 0},)}
        self.assertEqual(
            harness._roleta2_historical_modifier_lines([prior, terminal]),
            ["-# 🍌 Banana split · +10 <gain> · +10 <bonus>"],
        )

    def test_roleta2_statistics_are_separate_but_visible_in_profile_totals(self) -> None:
        base = BASE_PATH.read_text(encoding="utf-8")
        database = DB_PATH.read_text(encoding="utf-8")
        execution = self._async_function_source("_execute_roleta2_round")
        self.assertIn('"roleta2_spins"', execution)
        self.assertIn('"roleta2_jackpots"', execution)
        self.assertGreaterEqual(database.count('"roleta2_spins": _int("roleta2_spins")'), 2)
        self.assertGreaterEqual(database.count('"roleta2_jackpots": _int("roleta2_jackpots")'), 2)
        self.assertIn("stats.get('roleta2_spins'", base)
        self.assertIn("stats.get('roleta2_jackpots'", base)


if __name__ == "__main__":
    unittest.main()
