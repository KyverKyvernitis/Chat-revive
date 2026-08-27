from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLETA_PATH = ROOT / "cogs" / "games" / "games" / "roleta.py"


class GamesRouletteSpamPreemptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = ROLETA_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _async_function_source(self, name: str) -> str:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                segment = ast.get_source_segment(self.source, node)
                self.assertIsNotNone(segment)
                return str(segment)
        self.fail(f"função assíncrona não encontrada: {name}")

    def test_trigger_cooldown_and_two_animation_limit_are_removed(self) -> None:
        self.assertNotIn("ROLETA_TRIGGER_COOLDOWN_SECONDS", self.source)
        self.assertNotIn("GAME_ANIMATION_LIMIT_PER_GUILD", self.source)
        self.assertNotIn("_roleta_trigger_cooldown_remaining", self.source)
        self.assertNotIn("_try_acquire_game_animation_slot", self.source)
        self.assertNotIn("Já existem **2** animações", self.source)

    def test_latest_valid_animation_preempts_previous_animation(self) -> None:
        source = self._async_function_source("_activate_game_animation_session")
        self.assertIn('previous = state.get("active")', source)
        self.assertIn("previous_skip.set()", source)
        self.assertIn('state["active"] = {', source)
        self.assertIn('"skip_event": skip_event', source)

    def test_animation_sleeps_and_rate_limit_retries_are_interruptible(self) -> None:
        roulette = self._async_function_source("_animate_roleta_spin")
        cards = self._async_function_source("_animate_carta_spin")
        editor = self._async_function_source("_edit_game_message")
        sender = self._async_function_source("_send_game_message")
        self.assertIn("skip_event: asyncio.Event | None", roulette)
        self.assertIn("_wait_game_animation_delay(skip_event", roulette)
        self.assertIn("skip_event: asyncio.Event | None", cards)
        self.assertIn("_wait_game_animation_delay(skip_event", cards)
        self.assertIn("cancel_event: asyncio.Event | None", editor)
        self.assertIn("_wait_game_animation_delay(cancel_event", editor)
        self.assertIn("cancel_event: asyncio.Event | None", sender)
        self.assertIn("_wait_game_animation_delay(cancel_event", sender)

    def test_same_user_rounds_are_not_rejected_by_a_long_round_lock(self) -> None:
        cards = self._async_function_source("_handle_carta_trigger")
        roulette = self._async_function_source("_handle_roleta_trigger")
        self.assertNotIn("lock.locked()", cards)
        self.assertNotIn("lock.locked()", roulette)
        self.assertNotIn("_game_user_round_lock", self.source)
        self.assertIn("return await self._run_carta_trigger_locked(message)", cards)
        self.assertIn("return await self._run_roleta_trigger_locked(message)", roulette)

    def test_state_mutations_are_serialized_without_covering_animation(self) -> None:
        cards_trigger = self._async_function_source("_run_carta_trigger_locked")
        roulette_trigger = self._async_function_source("_run_roleta_trigger_locked")
        cards_round = self._async_function_source("_execute_carta_round")
        roulette_round = self._async_function_source("_execute_roleta_round")
        for source in (cards_trigger, roulette_trigger, cards_round, roulette_round):
            self.assertIn("_game_user_state_lock", source)
        self.assertLess(
            cards_round.index("_release_game_animation_session"),
            cards_round.index("async with self._game_user_state_lock"),
        )
        self.assertLess(
            roulette_round.index("_release_game_animation_session"),
            roulette_round.index("async with self._game_user_state_lock"),
        )

    def test_result_delivery_is_serialized_per_guild_not_per_round_core(self) -> None:
        delivery = self._async_function_source("_deliver_game_result")
        self.assertIn("_game_result_delivery_lock(guild_id)", delivery)
        self.assertIn("async with delivery_lock", delivery)

    def test_spam_result_balance_uses_round_reservations(self) -> None:
        issue = self.source[self.source.index("        def _issue_game_round_sequence("):self.source.index("        def _game_round_display_balances(")]
        display_start = self.source.index("        def _game_round_display_balances(")
        display_end = self.source.index("        def _format_game_balance_values(", display_start)
        display = self.source[display_start:display_end]
        self.assertIn('"reservations": {}', self.source)
        self.assertIn('entry_spend: dict | None = None', issue)
        self.assertIn('reservations[sequence] = {', issue)
        self.assertIn('if int(reserved_sequence) <= int(sequence):', display)
        self.assertIn('normal += max(0, int(spend.get("chips", 0) or 0))', display)
        self.assertIn('bonus += max(0, int(spend.get("bonus", 0) or 0))', display)

    def test_result_views_use_sequential_balance_snapshot(self) -> None:
        roulette_round = self._async_function_source("_execute_roleta_round")
        cards_round = self._async_function_source("_execute_carta_round")
        for source in (roulette_round, cards_round):
            self.assertIn("_game_round_display_balances(", source)
            self.assertIn("_format_game_balance_values(", source)
        self.assertNotIn(
            "balance_text=self._format_compact_chip_balance(guild.id, actor.id)",
            roulette_round,
        )
        self.assertNotIn(
            "balance_text=self._format_compact_chip_balance(guild.id, actor.id)",
            cards_round,
        )

    def test_round_completion_is_idempotent_and_releases_reservations_in_order(self) -> None:
        complete = self._async_function_source("_complete_game_round_sequence")
        self.assertIn("if completed_sequence < next_commit:", complete)
        self.assertIn("reservations.pop(next_commit, None)", complete)
        self.assertIn("while next_commit in completed:", complete)


if __name__ == "__main__":
    unittest.main()
