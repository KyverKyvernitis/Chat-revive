from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GAMES_INIT_PATH = ROOT / "cogs" / "games" / "__init__.py"
BASE_PATH = ROOT / "cogs" / "games" / "services" / "base.py"
DB_PATH = ROOT / "db.py"
GAME_PATHS = tuple(
    ROOT / "cogs" / "games" / "games" / filename
    for filename in ("alvo.py", "buckshot.py", "corrida.py", "poker.py", "roleta.py", "truco.py")
)


class GamesRaceSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.games_init = GAMES_INIT_PATH.read_text(encoding="utf-8")
        cls.base = BASE_PATH.read_text(encoding="utf-8")
        cls.db = DB_PATH.read_text(encoding="utf-8")
        cls.game_sources = tuple(path.read_text(encoding="utf-8") for path in GAME_PATHS)
        for source in (cls.games_init, cls.base, cls.db, *cls.game_sources):
            ast.parse(source)

    @staticmethod
    def _node_source(source: str, name: str, node_type: type[ast.AST]) -> str:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, node_type) and getattr(node, "name", None) == name:
                segment = ast.get_source_segment(source, node)
                if segment is not None:
                    return str(segment)
        raise AssertionError(f"nó não encontrado: {name}")

    def test_panel_has_requested_order_and_copy(self) -> None:
        panel = self._node_source(self.games_init, "_RacePanelView", ast.ClassDef)
        builder = self._node_source(panel, "_build_layout", ast.FunctionDef)
        self.assertIn('label="Ver habilidades"', builder)
        self.assertIn('label="Reroll"', builder)
        self.assertNotIn('label=f"Reroll {RACE_REROLL_COST}"', builder)
        self.assertIn("emoji=self.cog._CHIP_EMOJI", builder)
        self.assertNotIn("**Estado:**", panel)
        self.assertNotIn("**Trocar raça:**", panel)
        self.assertNotIn('label="Trocar raça"', panel)
        self.assertLess(builder.index('TextDisplay(f"#'), builder.index("ActionRow(show_races)"))
        self.assertLess(builder.index("ActionRow(show_races)"), builder.index("_ability_lines()"))
        self.assertLess(builder.index("_ability_lines()"), builder.index("\n                row,"))

    def test_race_catalog_is_dynamic_private_and_marks_current_race(self) -> None:
        catalog = self._node_source(self.games_init, "_make_race_catalog_view", ast.FunctionDef)
        show = self._node_source(self.games_init, "_show_races", ast.AsyncFunctionDef)
        self.assertIn("self._race_catalog().items()", catalog)
        self.assertIn("self._get_race_effects(race_key)", catalog)
        self.assertIn('current_marker = " · atual"', catalog)
        self.assertIn("_make_race_catalog_view", show)
        self.assertIn("ephemeral=True", show)
        self.assertIn('TextDisplay("# 🧬 Habilidades")', catalog)

    def test_habilidades_alias_and_trigger_keep_legacy_race_commands(self) -> None:
        handler = self._node_source(self.games_init, "_handle_race_trigger", ast.AsyncFunctionDef)
        command = self._node_source(self.games_init, "race_command", ast.AsyncFunctionDef)
        self.assertIn('{"race", "raça", "habilidades"}', handler)
        decorator_area = self.games_init[max(0, self.games_init.index("async def race_command") - 120):self.games_init.index("async def race_command") + 80]
        self.assertIn('aliases=["raça", "habilidades"]', decorator_area)
        self.assertIn('failure_title="🍀 Habilidades"', command)

    def test_preto_no_longer_has_labia_or_extra_begging_use(self) -> None:
        catalog = self._node_source(self.base, "_race_catalog", ast.FunctionDef)
        limited = self._node_source(self.base, "_limited_action_config", ast.FunctionDef)
        self.assertNotIn('"key": "labia"', catalog)
        self.assertNotIn('"title": "Lábia"', catalog)
        mendigar_branch = limited[limited.index('if action == "mendigar":'):]
        self.assertIn('return 1, float(CHIPS_MENDIGAR_COOLDOWN_SECONDS)', mendigar_branch)
        self.assertNotIn('_race_is(guild_id, user_id, "preto")', mendigar_branch.split('return 1, 0.0')[0])

    def test_reroll_confirmation_is_private_components_v2_with_only_continue(self) -> None:
        confirmation = self._node_source(
            self.games_init,
            "_RaceRerollConfirmView",
            ast.ClassDef,
        )
        callback = self._node_source(self.games_init, "_reroll", ast.AsyncFunctionDef)
        self.assertIn("discord.ui.LayoutView", confirmation)
        self.assertIn("discord.ui.Container", confirmation)
        self.assertIn("discord.ui.TextDisplay", confirmation)
        self.assertIn("discord.ui.Separator", confirmation)
        self.assertIn("discord.ui.ActionRow", confirmation)
        self.assertEqual(confirmation.count("discord.ui.Button("), 1)
        self.assertIn('label="Continuar"', confirmation)
        self.assertNotIn('label="Cancelar"', confirmation)
        self.assertIn("RACE_REROLL_COST", confirmation)
        self.assertIn("self.cog._CHIP_EMOJI", confirmation)
        self.assertIn("normal_balance < RACE_REROLL_COST", callback)
        self.assertIn("_RaceRerollConfirmView", callback)
        self.assertIn("ephemeral=True", callback)
        self.assertIn("wait=True", callback)

    def test_vampire_is_removed_from_runtime_and_all_game_calls(self) -> None:
        runtime_sources = (self.base, self.games_init, *self.game_sources)
        for source in runtime_sources:
            lowered = source.casefold()
            self.assertNotIn("vampiro", lowered)
            self.assertNotIn("allow_hunt", source)
            self.assertNotIn("opponent_ids=", source)
        catalog = self._node_source(self.base, "_race_catalog", ast.FunctionDef)
        for race_key in ("preto", "apostador", "sortudo", "coringa", "fenix", "glitch"):
            self.assertIn(f'"{race_key}":', catalog)

    def test_removed_race_cleanup_preserves_economy_and_history(self) -> None:
        cleanup = self._node_source(self.db, "_cleanup_removed_game_races", ast.AsyncFunctionDef)
        self.assertIn('"race_key": "vampiro"', cleanup)
        self.assertIn('"$unset"', cleanup)
        self.assertIn('"race_state"', cleanup)
        self.assertNotIn('"chips"', cleanup)
        self.assertNotIn('"bonus_chips"', cleanup)
        self.assertNotIn('"chip_history"', cleanup)
        init = self._node_source(self.db, "init", ast.AsyncFunctionDef)
        self.assertLess(init.index("_cleanup_removed_game_races"), init.index("load_cache"))

    def test_reroll_uses_normal_chips_once_and_persists_race_with_balance(self) -> None:
        reroll = self._node_source(self.base, "_reroll_user_race", ast.AsyncFunctionDef)
        callback = self._node_source(self.games_init, "_reroll", ast.AsyncFunctionDef)
        execute = self._node_source(
            self.games_init,
            "_execute_confirmed_reroll",
            ast.AsyncFunctionDef,
        )
        self.assertIn("_race_progress_lock", reroll)
        self.assertIn("_chip_economy_lock", reroll)
        self.assertIn('doc["chips"] = normal_chips - reroll_cost', reroll)
        self.assertIn('doc["race_key"] = chosen', reroll)
        self.assertIn("unset_fields=self._RACE_RUNTIME_FIELDS", reroll)
        self.assertNotIn("_change_user_chips", reroll)
        self.assertIn('reason="Reroll de raça"', reroll)
        self.assertNotIn("_reroll_user_race", callback)
        self.assertIn("_race_rerolls_in_progress", execute)
        self.assertIn("_reroll_user_race", execute)
        self.assertIn("cost=RACE_REROLL_COST", execute)
        self.assertIn("await interaction.response.defer(ephemeral=True, thinking=True)", callback)

    def test_confirmation_tokens_block_old_or_duplicate_rerolls(self) -> None:
        create = self._node_source(
            self.base,
            "_new_race_reroll_confirmation",
            ast.FunctionDef,
        )
        current = self._node_source(
            self.base,
            "_race_reroll_confirmation_is_current",
            ast.FunctionDef,
        )
        invalidate = self._node_source(
            self.base,
            "_invalidate_race_reroll_confirmation",
            ast.FunctionDef,
        )
        confirmation = self._node_source(
            self.games_init,
            "_RaceRerollConfirmView",
            ast.ClassDef,
        )
        execute = self._node_source(
            self.games_init,
            "_execute_confirmed_reroll",
            ast.AsyncFunctionDef,
        )
        handler = self._node_source(
            self.games_init,
            "_handle_race_trigger",
            ast.AsyncFunctionDef,
        )
        self.assertIn("+ 1", create)
        self.assertIn("== int(token)", current)
        self.assertIn("current != int(token)", invalidate)
        self.assertIn("token=self.confirmation_token", confirmation)
        self.assertLess(
            execute.index("_race_rerolls_in_progress.add"),
            execute.index("_invalidate_race_reroll_confirmation"),
        )
        self.assertLess(
            execute.index("_invalidate_race_reroll_confirmation"),
            execute.index('"🎲 Confirmando reroll"'),
        )
        self.assertIn("_invalidate_race_reroll_confirmation(guild_id, user_id)", handler)

    def test_first_roll_finishes_in_the_full_management_panel(self) -> None:
        handler = self._node_source(self.games_init, "_handle_race_trigger", ast.AsyncFunctionDef)
        self.assertIn("_race_panel_lock", handler)
        self.assertIn("_race_progress_lock", handler)
        self.assertIn("_RacePanelView", handler)
        self.assertIn("view.message = panel_message", handler)
        self.assertNotIn("_make_race_reveal_view", self.games_init)

    def test_race_state_removals_are_persisted_with_unset(self) -> None:
        save = self._node_source(self.db, "_save_user_doc", ast.AsyncFunctionDef)
        set_race = self._node_source(self.base, "_set_user_race_key", ast.AsyncFunctionDef)
        self.assertIn("unset_fields", save)
        self.assertIn('update["$unset"]', save)
        self.assertIn("unset_fields=tuple(unset_fields)", set_race)


if __name__ == "__main__":
    unittest.main()
