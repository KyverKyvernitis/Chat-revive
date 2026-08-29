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
ALVO = (ROOT / "cogs" / "games" / "games" / "alvo.py").read_text(encoding="utf-8")
CORRIDA = (ROOT / "cogs" / "games" / "games" / "corrida.py").read_text(encoding="utf-8")
HELP = json.loads((ROOT / "shared" / "help_catalog.json").read_text(encoding="utf-8"))


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
        for source in (GAMES_INIT, BASE, DB, ROLETA, BUCKSHOT, TRUCO, POKER, ALVO, CORRIDA):
            ast.parse(source)

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
        self.assertIn('"## 🐦‍🔥 Reborn"', view)
        self.assertIn("command_hint", view)
        self.assertIn("movement='loss'", view)
        self.assertIn("movement='gain'", view)
        self.assertIn("kind='bonus', movement='loss'", view)
        self.assertIn("kind='bonus', movement='gain'", view)

    def test_skill_notices_use_compact_components_v2(self) -> None:
        notice = node_source(BASE, "_make_skill_notice", ast.FunctionDef)
        self.assertIn("discord.ui.LayoutView", notice)
        self.assertIn("discord.ui.Container", notice)
        self.assertIn("discord.ui.TextDisplay", notice)
        self.assertIn('body = [f"## {title}"]', notice)
        self.assertIn('normalized_state == "neutral"', notice)

    def test_skill_chip_values_match_the_extract_color_semantics(self) -> None:
        formatter_source = node_source(BASE, "_skill_chip_value", ast.FunctionDef)
        namespace: dict[str, object] = {}
        exec(formatter_source, namespace)
        formatter = namespace["_skill_chip_value"]

        class Dummy:
            _CHIP_EMOJI = "normal"
            _CHIP_GAIN_EMOJI = "green"
            _CHIP_LOSS_EMOJI = "red"
            _CHIP_BONUS_EMOJI = "orange"

        dummy = Dummy()
        self.assertEqual(formatter(dummy, 5), "normal **5**")
        self.assertEqual(formatter(dummy, 5, movement="gain"), "green **+5**")
        self.assertEqual(formatter(dummy, 5, movement="loss"), "red **-5**")
        self.assertEqual(formatter(dummy, 5, kind="bonus", movement="gain"), "orange **+5**")
        self.assertEqual(formatter(dummy, 5, kind="bonus", movement="loss"), "orange **-5**")

    def test_recharge_accepts_exactly_15_normal_chips_and_ignores_bonus(self) -> None:
        state_source = node_source(BASE, "_chip_recharge_state", ast.FunctionDef)
        namespace: dict[str, object] = {
            "CHIPS_INITIAL": 100,
            "CHIPS_RECHARGE_THRESHOLD": 15,
            "CHIPS_RESET_SECONDS": 12 * 60 * 60,
        }
        exec(state_source, namespace)
        recharge_state = namespace["_chip_recharge_state"]

        class DummyDb:
            user_cache = {(1, 2): {"chip_recharge_manual_initialized": False}}

            def __init__(self, chips: int) -> None:
                self.chips = chips

            def get_user_chips(self, guild_id: int, user_id: int, *, default: int) -> int:
                return self.chips

            def get_user_chip_reset_at(self, guild_id: int, user_id: int) -> float:
                return 0.0

        class Dummy:
            def __init__(self, chips: int, bonus: int) -> None:
                self.db = DummyDb(chips)
                self.bonus = bonus

            def _get_user_bonus_chips(self, guild_id: int, user_id: int) -> int:
                return self.bonus

        exact_limit = recharge_state(Dummy(15, 999), 1, 2)
        self.assertTrue(exact_limit["within_threshold"])
        self.assertTrue(exact_limit["available"])
        self.assertEqual(exact_limit["bonus"], 999)

        above_limit = recharge_state(Dummy(16, 0), 1, 2)
        self.assertFalse(above_limit["within_threshold"])
        self.assertFalse(above_limit["available"])

        recharge_use = node_source(BASE, "_try_use_chip_recharge", ast.AsyncFunctionDef)
        self.assertIn("chips > CHIPS_RECHARGE_THRESHOLD", recharge_use)
        self.assertNotIn("chips + bonus", recharge_use)

    def test_daily_and_card_results_use_extract_emoji_semantics(self) -> None:
        daily = node_source(BASE, "_make_daily_view", ast.FunctionDef)
        self.assertIn("_skill_chip_value(int(bonus), movement='gain')", daily)
        self.assertIn("_skill_chip_value(int(bonus_bonus), kind='bonus', movement='gain')", daily)
        self.assertIn("_skill_chip_value(extra_streak_bonus, movement='gain')", daily)

        namespace: dict[str, object] = {}
        for name in (
            "_format_game_result_value",
            "_format_game_bonus_result_value",
            "_format_game_result_breakdown",
        ):
            exec(node_source(ROLETA, name, ast.FunctionDef), namespace)

        class Dummy:
            _CHIP_GAIN_EMOJI = "green"
            _CHIP_LOSS_EMOJI = "red"
            _CHIP_EMOJI = "normal"
            _CHIP_BONUS_EMOJI = "orange"

        Dummy._format_game_result_value = namespace["_format_game_result_value"]
        Dummy._format_game_bonus_result_value = namespace["_format_game_bonus_result_value"]
        breakdown = namespace["_format_game_result_breakdown"]
        self.assertEqual(breakdown(Dummy(), 10, -15), "**+10 green** • **-15 orange**")

        card_view = node_source(ROLETA, "_make_carta_result_view", ast.FunctionDef)
        card_round = node_source(ROLETA, "_execute_carta_round", ast.AsyncFunctionDef)
        self.assertIn("bonus_result_delta", card_view)
        self.assertIn("_format_game_result_breakdown", card_view)
        self.assertNotIn("_format_game_result_value(effective_result)", card_view)
        self.assertIn("commit_start_normal, commit_start_bonus", card_round)
        self.assertIn("result_delta=normal_result_delta", card_round)
        self.assertIn("bonus_result_delta=bonus_result_delta", card_round)

    def test_apostador_equal_number_pairs_are_half_as_likely(self) -> None:
        resolver_source = node_source(ROLETA, "_halve_apostador_pair_outcome", ast.FunctionDef)
        namespace: dict[str, object] = {
            "ROLETA_APOSTADOR_PAIR_KEEP_CHANCE": 0.5,
        }

        class FakeRandom:
            def __init__(self, value: float) -> None:
                self.value = value
                self.random_calls = 0

            def random(self) -> float:
                self.random_calls += 1
                return self.value

            def sample(self, population, *, k: int):
                return [1, 2, 3]

        fake_random = FakeRandom(0.49)
        namespace["random"] = fake_random
        exec(resolver_source, namespace)
        resolver = namespace["_halve_apostador_pair_outcome"]

        equal_pair = {"target_middle": [4, 5, 4], "forced_kind": None, "forced_amount": None}
        self.assertEqual(resolver(equal_pair), equal_pair)

        fake_random.value = 0.5
        rejected = resolver(equal_pair)
        self.assertEqual(rejected["target_middle"], [1, 2, 3])
        self.assertIsNone(rejected["forced_kind"])
        self.assertIsNone(rejected["forced_amount"])

        calls_before_exclusions = fake_random.random_calls
        jackpot = {"target_middle": [9, 9, 9], "forced_kind": "jackpot", "forced_amount": 100}
        joker_pair = {"target_middle": [4, "🃏", 4], "forced_kind": None, "forced_amount": None}
        distinct = {"target_middle": [4, 5, 6], "forced_kind": None, "forced_amount": None}
        self.assertEqual(resolver(jackpot), jackpot)
        self.assertEqual(resolver(joker_pair), joker_pair)
        self.assertEqual(resolver(distinct), distinct)
        self.assertEqual(fake_random.random_calls, calls_before_exclusions)

        outcome = node_source(ROLETA, "_roleta_outcome_for_user", ast.FunctionDef)
        self.assertIn("return self._halve_apostador_pair_outcome(outcome)", outcome)

        catalog = node_source(BASE, "_race_catalog", ast.FunctionDef)
        for probability in ("0.15", "0.05", "0.25"):
            self.assertIn(f"_format_percent_text({probability})", catalog)

    def test_0to1_preserves_normal_bonus_and_mixed_source_types(self) -> None:
        splitter_source = node_source(BASE, "_race_skill_0to1_source_parts", ast.FunctionDef)
        namespace: dict[str, object] = {}
        exec(splitter_source, namespace)
        splitter = namespace["_race_skill_0to1_source_parts"]

        self.assertEqual(splitter({"delta": -15, "kind": "chips"}, 15), (15, 0))
        self.assertEqual(splitter({"delta": -15, "kind": "bonus"}, 15), (0, 15))
        self.assertEqual(
            splitter(
                {
                    "delta": -15,
                    "kind": "mixed",
                    "normal_delta": -5,
                    "bonus_delta": -10,
                },
                15,
            ),
            (5, 10),
        )
        self.assertEqual(
            splitter(
                {
                    "delta": -40,
                    "kind": "mixed",
                    "normal_delta": -6,
                    "bonus_delta": -34,
                },
                20,
            ),
            (6, 14),
        )

    def test_skill_command_copy_is_short_and_non_redundant(self) -> None:
        coinflip = node_source(GAMES_INIT, "coinflip_command", ast.AsyncFunctionDef)
        zero_to_one = node_source(GAMES_INIT, "zero_to_one_command", ast.AsyncFunctionDef)
        reborn = node_source(GAMES_INIT, "reborn_command", ast.AsyncFunctionDef)
        changefate = node_source(GAMES_INIT, "changefate_command", ast.AsyncFunctionDef)
        forcerob = node_source(GAMES_INIT, "forcerob_command", ast.AsyncFunctionDef)
        joker = node_source(GAMES_INIT, "joker_command", ast.AsyncFunctionDef)

        for command in (coinflip, zero_to_one, reborn, changefate, forcerob, joker):
            self.assertIn("_make_skill_notice", command)
        self.assertIn("**Coroa** ·", coinflip)
        self.assertIn("kind='bonus', movement='gain'", coinflip)
        self.assertIn("Nada para inverter no extrato", zero_to_one)
        self.assertIn("movement='loss'", zero_to_one)
        self.assertIn("movement='gain'", zero_to_one)
        self.assertIn("kind='bonus', movement='loss'", zero_to_one)
        self.assertIn('kind="bonus", movement="loss"', zero_to_one)
        self.assertIn("join(source_parts)", zero_to_one)
        self.assertNotIn("O lançamento original foi preservado", zero_to_one)
        self.assertIn("👁️⃤ 0to1", zero_to_one)
        self.assertNotIn("<a:eyeglitch", zero_to_one)
        self.assertIn("command_prefix=ctx.clean_prefix", reborn)
        self.assertIn("Seu próximo Buckshot ou Truco será **dourado**", changefate)
        self.assertIn("🚨 A polícia pegou o meliante", changefate)
        self.assertIn("thief_loss = amount + penalty", changefate)
        self.assertIn('movement="loss"', changefate)
        self.assertIn("recuperadas", changefate)
        self.assertIn("devolução + **{penalty}** de multa", changefate)
        self.assertNotIn("Recuperado:", changefate)
        self.assertNotIn("Penalidade de", changefate)
        self.assertIn('normal, movement="gain"', changefate)
        self.assertIn('bonus, kind="bonus", movement="gain"', changefate)
        self.assertNotIn("Midas foi preparado", changefate)
        self.assertIn('"🥷🏿 Forcerob"', forcerob)
        self.assertIn('normal, movement="gain"', forcerob)
        self.assertIn('bonus, kind="bonus", movement="gain"', forcerob)
        self.assertNotIn("Total levado", forcerob)
        self.assertIn("Dura **1min** · máximo **50**", joker)

    def test_skill_copy_is_synchronized_in_panel_help_history_and_games(self) -> None:
        catalog = node_source(BASE, "_race_catalog", ast.FunctionDef)
        self.assertIn("roubo garantido de **5–20 fichas**", catalog)
        self.assertIn("cada valor vale uma vez", catalog)
        self.assertIn("Só de dia · cooldown de **6h**", catalog)
        self.assertRegex(catalog, r'"key": "0to1",\s+"emoji": "👁️⃤"')
        self.assertIn('"key": "mao_negra", "emoji": "💲", "title": "Pilantra"', catalog)
        self.assertNotIn("Mão Negra", catalog)
        self.assertNotIn("🖐🏿", catalog)
        self.assertNotIn("movimentação negativa", catalog)

        help_by_key = {
            entry["key"]: entry["description"]
            for entry in HELP["entries"]
            if isinstance(entry, dict)
        }
        self.assertEqual(help_by_key["coinflip"], "Lança a moeda do Apostador")
        self.assertEqual(help_by_key["0to1"], "Inverte a última perda ou bônus")
        self.assertEqual(help_by_key["reborn"], "Alterna fichas normais e bônus")
        self.assertEqual(help_by_key["changefate"], "Recupera um roubo ou garante Midas")
        self.assertEqual(help_by_key["forcerob"], "Roubo garantido de até 20 fichas")
        self.assertEqual(help_by_key["joker"], "Protege a próxima derrota paga")
        self.assertEqual(help_by_key["recarga"], "Recebe bônus com até 15 fichas normais")

        for reason in (
            "Coinflip · jackpot",
            "Reborn · conversão",
            "0to1 · conversão",
            "0to1 · roubo revertido",
            "Change Fate · devolução",
            "Change Fate · polícia",
            "Joker · reembolso",
        ):
            self.assertIn(reason, BASE)
        self.assertIn("_skill_chip_value(coinflip_bonus, kind='bonus', movement='gain')", ROLETA)
        for game in (ROLETA, BUCKSHOT, TRUCO, POKER, ALVO, CORRIDA):
            self.assertIn('_skill_chip_value(refund, kind="bonus", movement="gain")', game)

    def test_temporary_effects_clear_but_cooldowns_persist_on_reroll(self) -> None:
        base_class = node_source(BASE, "GincanaBase", ast.ClassDef)
        runtime_fields = base_class.split("_RACE_RUNTIME_FIELDS = (", 1)[1].split("\n    )", 1)[0]
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
        self.assertIn("_race_skill_0to1_source_parts", execute)
        self.assertIn('"source_normal": source_normal', execute)
        self.assertIn('"source_bonus": source_bonus', execute)
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

    def test_user_and_server_resets_clear_extract_and_linked_skill_state(self) -> None:
        base_class = node_source(BASE, "GincanaBase", ast.ClassDef)
        reset_user = node_source(BASE, "_force_reset_chips", ast.AsyncFunctionDef)
        reset_profile = node_source(BASE, "_force_full_reset_ficha_profile", ast.AsyncFunctionDef)
        active_users = node_source(BASE, "_iter_active_chip_user_ids", ast.FunctionDef)
        reset_guild = node_source(DB, "_settingsdb_reset_guild_chip_economy", ast.AsyncFunctionDef)
        reset_fields = (
            "chip_history",
            "race_skill_0to1_cutoff_ts",
            "race_skill_0to1_last_entry_id",
            "race_ordinary_robberies",
        )

        for field in reset_fields:
            self.assertIn(f'"{field}"', base_class)
            self.assertIn(f'"{field}"', reset_guild)
        for reset in (reset_user, reset_profile):
            self.assertIn("_CHIP_HISTORY_RESET_FIELDS", reset)
            self.assertNotIn("append_chip_history", reset)
        self.assertIn("_CHIP_HISTORY_RESET_FIELDS", active_users)


if __name__ == "__main__":
    unittest.main()
