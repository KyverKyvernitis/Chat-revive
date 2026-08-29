from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GAMES_INIT = (ROOT / "cogs" / "games" / "__init__.py").read_text(encoding="utf-8")
BASE = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
DB = (ROOT / "db.py").read_text(encoding="utf-8")
ROLETA = (ROOT / "cogs" / "games" / "games" / "roleta.py").read_text(encoding="utf-8")
BUCKSHOT = (ROOT / "cogs" / "games" / "games" / "buckshot.py").read_text(encoding="utf-8")
TRUCO = (ROOT / "cogs" / "games" / "games" / "truco.py").read_text(encoding="utf-8")
POKER = (ROOT / "cogs" / "games" / "games" / "poker.py").read_text(encoding="utf-8")


def node_source(source: str, name: str, node_type: type[ast.AST]) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, node_type) and getattr(node, "name", None) == name:
            segment = ast.get_source_segment(source, node)
            if segment:
                return segment
    raise AssertionError(f"nó ausente: {name}")


class RaceSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for source in (GAMES_INIT, BASE, DB, ROLETA, BUCKSHOT, TRUCO, POKER):
            ast.parse(source)
        json.loads((ROOT / "shared" / "help_catalog.json").read_text(encoding="utf-8"))

    def test_only_final_command_names_are_registered(self) -> None:
        for command in ("coinflip", "0to1", "reborn", "changefate", "forcerob", "joker"):
            self.assertIn(f'@dcommands.command(name="{command}")', GAMES_INIT)
        for removed in ("error", "1to0", "lucky", "blackout"):
            self.assertNotIn(f'@dcommands.command(name="{removed}")', GAMES_INIT)

    def test_reborn_confirmation_is_components_v2_and_continue_only(self) -> None:
        view = node_source(GAMES_INIT, "_RebornConfirmView", ast.ClassDef)
        self.assertIn("discord.ui.LayoutView", view)
        self.assertIn("discord.ui.Container", view)
        self.assertIn("discord.ui.TextDisplay", view)
        self.assertIn("discord.ui.ActionRow", view)
        self.assertEqual(view.count("discord.ui.Button("), 1)
        self.assertIn('label="Continuar"', view)
        self.assertNotIn('label="Cancelar"', view)

    def test_temporary_effects_clear_but_cooldowns_persist_on_reroll(self) -> None:
        runtime_fields = node_source(BASE, "GincanaBase", ast.ClassDef).split("_ACHIEVEMENT_THUMBNAIL_FILENAME", 1)[0]
        for field in (
            "race_skill_coinflip_temp_bonus",
            "race_skill_coinflip_jackpot_bonus",
            "race_skill_changefate_golden_until",
            "race_skill_joker_until",
        ):
            self.assertIn(field, runtime_fields)
        for persistent in (
            "race_skill_daily_last_use",
            "race_skill_reborn_used_at",
            "race_skill_0to1_cutoff_ts",
        ):
            self.assertNotIn(persistent, runtime_fields)

    def test_coinflip_uses_separate_temporary_pool_and_jackpot_hook(self) -> None:
        activation = node_source(BASE, "_activate_coinflip_skill", ast.AsyncFunctionDef)
        consume = node_source(BASE, "_try_consume_chips", ast.AsyncFunctionDef)
        jackpot = node_source(BASE, "_claim_coinflip_jackpot_bonus", ast.AsyncFunctionDef)
        self.assertIn("RACE_SKILL_COINFLIP_SECONDS", activation)
        self.assertIn('doc["race_skill_coinflip_temp_bonus"]', activation)
        self.assertIn("use_temporary", consume)
        self.assertNotIn('doc["bonus_chips"] = RACE_SKILL_COINFLIP_BONUS', activation)
        self.assertIn("skill_eligible=False", jackpot)
        self.assertIn("_claim_coinflip_jackpot_bonus", ROLETA)

    def test_0to1_is_capped_single_use_and_excludes_forced_robbery(self) -> None:
        selector = node_source(BASE, "_race_skill_0to1_entry", ast.FunctionDef)
        execute = node_source(BASE, "_execute_0to1_skill", ast.AsyncFunctionDef)
        self.assertIn('"forced_robbery"', selector)
        self.assertIn("resolved_robberies", selector)
        self.assertIn("RACE_SKILL_0TO1_LIMIT", execute)
        self.assertIn('user_doc["race_skill_0to1_cutoff_ts"]', execute)
        self.assertIn('updated_robbery["resolved_by"] = "0to1"', execute)

    def test_forcerob_prioritizes_persistent_bonus_and_is_not_ordinary(self) -> None:
        execute = node_source(BASE, "_execute_forcerob_skill", ast.AsyncFunctionDef)
        self.assertLess(execute.index("bonus_taken = min"), execute.index("normal_taken = amount - bonus_taken"))
        self.assertIn('"event_type": "forced_robbery"', execute)
        self.assertIn('"skill_eligible": False', execute)
        self.assertNotIn("_consume_limited_action", execute)
        self.assertIn("random.randint(5, min(20, available))", execute)

    def test_changefate_consumes_only_valid_golden_rounds(self) -> None:
        self.assertIn("_reserve_changefate_golden", BUCKSHOT)
        self.assertIn("changefate_token", BUCKSHOT)
        self.assertIn("len(eligible) < 2", BUCKSHOT)
        self.assertLess(BUCKSHOT.index("len(eligible) < 2"), BUCKSHOT.index("_consume_changefate_golden"))
        self.assertIn("game.changefate_forced", TRUCO)
        self.assertLess(TRUCO.index("consumed_entries.append"), TRUCO.index("_consume_changefate_golden"))
        changefate = node_source(BASE, "_execute_changefate_skill", ast.AsyncFunctionDef)
        self.assertIn("race_ordinary_robberies", changefate)
        self.assertIn("- amount - 10", changefate)

    def test_joker_replaces_passive_and_reaches_all_paid_game_families(self) -> None:
        refund = node_source(BASE, "_maybe_apply_coringa_loss_refund", ast.AsyncFunctionDef)
        self.assertLess(refund.index("active_until > time.time()"), refund.index("random.random()"))
        self.assertIn("RACE_SKILL_JOKER_REFUND_CAP", refund)
        self.assertIn('kind="bonus"', refund)
        for source in (ROLETA, BUCKSHOT, TRUCO, POKER):
            self.assertIn("_maybe_apply_coringa_loss_refund", source)

    def test_history_has_event_identity_and_skill_eligibility(self) -> None:
        append = node_source(DB, "_settingsdb_append_chip_history", ast.AsyncFunctionDef)
        for field in ("entry_id", "event_type", "event_id", "other_user_id", "skill_eligible"):
            self.assertIn(field, append)
        ordinary = node_source(BASE, "_execute_ordinary_robbery_transfer", ast.AsyncFunctionDef)
        self.assertIn('"event_type": "ordinary_robbery"', ordinary)
        self.assertIn("race_ordinary_robberies", ordinary)


if __name__ == "__main__":
    unittest.main()
