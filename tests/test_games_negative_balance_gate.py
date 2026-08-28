from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = ROOT / "cogs" / "games" / "services" / "base.py"
DB_PATH = ROOT / "db.py"
ROLETA_PATH = ROOT / "cogs" / "games" / "games" / "roleta.py"
TRUCO_PATH = ROOT / "cogs" / "games" / "games" / "truco.py"
POKER_PATH = ROOT / "cogs" / "games" / "games" / "poker.py"


class GamesNegativeBalanceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = BASE_PATH.read_text(encoding="utf-8")
        cls.db = DB_PATH.read_text(encoding="utf-8")
        cls.roleta = ROLETA_PATH.read_text(encoding="utf-8")
        cls.truco = TRUCO_PATH.read_text(encoding="utf-8")
        cls.poker = POKER_PATH.read_text(encoding="utf-8")
        ast.parse(cls.base)
        ast.parse(cls.db)
        ast.parse(cls.roleta)
        ast.parse(cls.truco)
        ast.parse(cls.poker)

    def _function_source(self, source: str, name: str) -> str:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                segment = ast.get_source_segment(source, node)
                self.assertIsNotNone(segment)
                return str(segment)
        self.fail(f"função não encontrada: {name}")

    def test_components_v2_copy_has_only_continue(self) -> None:
        block = self.base[
            self.base.index("class _NegativeDebtConfirmView"):
            self.base.index("class _ChipRankPageButton")
        ]
        self.assertIn("discord.ui.LayoutView", block)
        self.assertIn("# <:emoji_65:1485043671077228786> Saldo negativo", block)
        self.assertIn("Este jogo deixará seu saldo em", block)
        self.assertIn("Continue para permitir saldo negativo até recuperá-lo", block)
        self.assertIn("use _recarga para adicionar", block)
        self.assertIn("<:laranja:1487076933819830443>", block)
        self.assertIn('label="Continuar"', block)
        self.assertNotIn('label="Cancelar"', block)

    def test_authorization_is_persistent_and_checked_globally(self) -> None:
        needs = self._function_source(self.base, "_needs_negative_confirmation")
        self.assertIn("_negative_balance_authorized", needs)
        self.assertIn("projected_chips < 0", needs)
        self.assertIn("projected_chips < chips", needs)
        self.assertIn("projected_chips < -self._MAX_CHIP_DEBT", needs)
        self.assertIn("get_negative_balance_authorized", self.db)
        self.assertIn("set_negative_balance_authorized", self.db)
        self.assertIn('doc["negative_balance_authorized"] = bool(value)', self.db)

    def test_authorization_resets_only_on_negative_to_nonnegative_transition(self) -> None:
        overrides = self.db[self.db.index("# ---- bonus chips / debt overrides ----"):]
        self.assertIn("current < 0 <= new_chips", overrides)
        self.assertIn("old_chips < 0 <= new_chips", overrides)
        self.assertGreaterEqual(overrides.count('doc["negative_balance_authorized"] = False'), 2)

    def test_message_spam_is_coalesced_and_old_confirmation_is_replaced(self) -> None:
        confirm = self._function_source(self.base, "_confirm_negative_from_message")
        show = self._function_source(self.base, "_show_negative_message_gate")
        self.assertIn("_negative_debt_message_gates", confirm)
        self.assertIn('state["generation"]', confirm)
        self.assertIn("old_task.cancel()", confirm)
        self.assertIn("if not owner:\n            await self._delete_negative_gate_message(message)", confirm)
        self.assertIn("_delete_negative_gate_message(old_confirmation)", confirm)
        self.assertIn("await asyncio.sleep(0.45)", show)
        self.assertIn("_set_negative_balance_authorized", show)

    def test_roulette_and_cards_use_the_shared_gate(self) -> None:
        helper = self._function_source(self.roleta, "_confirm_game_negative_from_message")
        self.assertIn("_confirm_negative_from_message", helper)
        self.assertNotIn("_GameDebtConfirmView", self.roleta)


    def test_roulette_confirmation_waits_for_earlier_spam_results(self) -> None:
        helper = self._function_source(self.roleta, "_confirm_game_negative_from_message")
        self.assertIn("_wait_for_game_round_delivery_turn", helper)
        self.assertIn("before_show=_wait_prior_results", helper)
        self.assertIn('"delivery_condition": asyncio.Condition()', self.roleta)
        self.assertIn("_complete_game_round_delivery_sequence", self.roleta)

    def test_truco_covers_entry_and_raises(self) -> None:
        trigger = self._function_source(self.truco, "_handle_truco_trigger")
        raise_request = self._function_source(self.truco, "_handle_truco_raise")
        raise_accept = self._function_source(self.truco, "_handle_truco_accept_raise")
        self.assertIn("_confirm_negative_from_message", trigger)
        self.assertIn("_confirm_negative_ephemeral", raise_request)
        self.assertIn("_confirm_negative_ephemeral", raise_accept)

    def test_poker_uses_shared_negative_policy_for_both_players(self) -> None:
        trigger = self._function_source(self.poker, "_handle_poker_trigger")
        check_call = self._function_source(self.poker, "_handle_poker_check_call")
        cancel = self._function_source(self.poker, "_cancel_poker_game")
        self.assertIn("_confirm_negative_from_message", trigger)
        self.assertIn("_confirm_negative_via_message", trigger)
        self.assertIn("guild_id=guild.id", trigger)
        self.assertIn("amount=POKER_BUY_IN", trigger)
        self.assertNotIn("_poker_entry_block_note", self.poker)
        self.assertNotIn("sem aumentar dívida", self.poker)
        self.assertIn("to_call > 0", check_call)
        self.assertIn("max(0, self._poker_total_stack", check_call)
        self.assertIn("_normalize_entry_spend", cancel)
        self.assertIn('spend.get("chips"', cancel)
        self.assertIn('spend.get("bonus"', cancel)


    def test_all_stake_games_route_negative_entries_through_shared_gate(self) -> None:
        alvo = (ROOT / "cogs" / "games" / "games" / "alvo.py").read_text(encoding="utf-8")
        buckshot = (ROOT / "cogs" / "games" / "games" / "buckshot.py").read_text(encoding="utf-8")
        corrida = (ROOT / "cogs" / "games" / "games" / "corrida.py").read_text(encoding="utf-8")
        for source in (alvo, buckshot, corrida):
            self.assertIn("_needs_negative_confirmation", source)
            self.assertTrue(
                "_confirm_negative_from_message" in source or "_confirm_negative_ephemeral" in source
            )

    def test_unconfirmed_negative_charge_has_last_line_of_defense(self) -> None:
        consume = self._function_source(self.base, "_try_consume_chips")
        self.assertIn("_needs_negative_confirmation", consume)
        self.assertIn("Confirme o saldo negativo antes de continuar", consume)

    def test_game_balance_mutations_share_the_same_micro_lock(self) -> None:
        consume = self._function_source(self.base, "_try_consume_chips")
        change_normal = self._function_source(self.base, "_change_user_chips")
        change_bonus = self._function_source(self.base, "_change_user_bonus_chips")
        persist_poker = self._function_source(self.poker, "_persist_poker_player_stack")
        for source in (consume, change_normal, change_bonus, persist_poker):
            self.assertIn("_chip_economy_lock", source)


if __name__ == "__main__":
    unittest.main()
