from __future__ import annotations

import ast
import random
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
        cls.namespace: dict[str, object] = {"random": random}
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
        self.assertEqual(sum(weights.values()), 1000)
        self.assertEqual(weights["deja_vu"], 100)
        self.assertEqual(weights["loss"], 440)

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
        for kind in kinds:
            for _ in range(100):
                grid = generate(kind, rng)
                self.assertEqual(detect(grid), kind, (kind, grid))
                if kind == "loss":
                    self.assertFalse(has_escorredio(grid), grid)

    def test_escorredio_has_a_banana_diagonal_then_a_clean_final_grid(self) -> None:
        generate = self.namespace["_slots_generate_escorredio"]
        detect = self.namespace["_slots_detect_kind"]
        has_escorredio = self.namespace["_slots_has_escorredio"]
        rng = random.Random(1543757558374994021)
        for _ in range(100):
            preview, final, column = generate(rng)
            self.assertTrue(has_escorredio(preview), preview)
            self.assertEqual(detect(preview), "loss")
            self.assertEqual(detect(final), "loss")
            self.assertIn(column, {0, 1, 2})

    def test_command_and_bare_trigger_are_both_wired(self) -> None:
        games = GAMES_PATH.read_text(encoding="utf-8")
        router = ROUTER_PATH.read_text(encoding="utf-8")
        cog = COG_PATH.read_text(encoding="utf-8")
        self.assertIn('@dcommands.command(name="roleta2")', games)
        self.assertIn('handler_name="_handle_roleta2_trigger"', games)
        self.assertIn('content="roleta2"', games)
        self.assertIn('"_handle_roleta2_trigger"', router)
        self.assertIn("GincanaSlotsMixin", cog)

    def test_experiment_shares_card_limits_and_safe_spam_flow(self) -> None:
        trigger = self._async_function_source("_run_roleta2_trigger_locked")
        animation = self._async_function_source("_animate_roleta2_spin")
        execution = self._async_function_source("_execute_roleta2_round")
        self.assertIn("_sync_carta_spin_window", trigger)
        self.assertIn("_reserve_carta_spin_state", trigger)
        self.assertIn("_confirm_game_negative_from_message", trigger)
        self.assertIn("_issue_game_round_sequence", trigger)
        self.assertIn("_activate_game_animation_session", trigger)
        self.assertIn("skip_event", animation)
        self.assertIn("_wait_for_game_round_commit_turn", execution)
        self.assertIn("_deliver_game_result", execution)
        self.assertIn("_complete_game_round_delivery_sequence", execution)

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
