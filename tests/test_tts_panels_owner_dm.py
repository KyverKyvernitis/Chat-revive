from __future__ import annotations

import ast
import unittest
from pathlib import Path

from cogs.tts.prefix import match_prefix_control_command


ROOT = Path(__file__).resolve().parents[1]


class TTSPrefixAliasRegressionTests(unittest.TestCase):
    def test_tts_alias_opens_personal_panel_and_accepts_staff_target(self):
        personal = match_prefix_control_command("_tts", "_")
        self.assertIsNotNone(personal)
        self.assertEqual(personal.kind, "panel_user")
        self.assertEqual(personal.argument, "")
        self.assertEqual(personal.alias, "_tts")

        target = match_prefix_control_command("_tts @Pessoa", "_")
        self.assertIsNotNone(target)
        self.assertEqual(target.kind, "panel_user")
        self.assertEqual(target.argument, "@Pessoa")
        self.assertEqual(target.alias, "_tts")

        custom_prefix = match_prefix_control_command("!tts 123456789", "!")
        self.assertIsNotNone(custom_prefix)
        self.assertEqual(custom_prefix.kind, "panel_user")
        self.assertEqual(custom_prefix.argument, "123456789")
        self.assertEqual(custom_prefix.alias, "!tts")

        self.assertIsNone(match_prefix_control_command("_ttsqualquer", "_"))
        self.assertIsNone(match_prefix_control_command("_tts", "!"))

    def test_legacy_panel_aliases_and_short_p_route_are_preserved(self):
        for content in ("_panel", "_painel", "_p"):
            command = match_prefix_control_command(content, "_")
            self.assertIsNotNone(command)
            self.assertEqual(command.kind, "panel_user")

        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_prefix_panel"
        ]
        self.assertGreaterEqual(len(handlers), 1)
        # A definição efetiva é a última da classe. Ela preserva o escape de `_p`
        # para o player e exige kick_members para editar outra pessoa.
        source = ast.get_source_segment(text, sorted(handlers, key=lambda node: node.lineno)[-1]) or ""
        self.assertIn("short_panel_alias", source)
        self.assertIn('getattr(message.author.guild_permissions, "kick_members", False)', source)
        self.assertIn("await self._resolve_member_from_text(message.guild, target_query)", source)
        self.assertIn("if short_panel_alias:\n                    return False", source)

    def test_panel_target_resolution_keeps_public_message_context(self):
        checked = 0
        for rel in ("cogs/tts/ui.py", "cogs/tts/utils/panel_apply.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                if not isinstance(call.func, ast.Attribute) or call.func.attr != "_resolve_panel_target_user":
                    continue
                keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
                self.assertIn("message_id", keywords, f"{rel}:{call.lineno} perdeu o contexto do painel público")
                checked += 1
        self.assertEqual(checked, 7)

    def test_testdm_is_not_routed_by_tts_prefix_dispatcher(self):
        # `_testdm` agora é um comando real do commands.Bot. Manter uma segunda
        # rota no listener do TTS faria bot.py executar o teste duas vezes.
        self.assertIsNone(match_prefix_control_command("_testdm", "_"))
        self.assertIsNone(match_prefix_control_command("!testdm", "!"))

        aliases = (ROOT / "cogs" / "tts" / "aliases.py").read_text(encoding="utf-8")
        prefix = (ROOT / "cogs" / "tts" / "prefix.py").read_text(encoding="utf-8")
        self.assertNotIn("owner_dm_test", aliases)
        self.assertNotIn("owner_dm_test", prefix)


class TTSOwnerDMRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cog_text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        cls.cog_tree = ast.parse(cls.cog_text)

    def _function_source(self, name: str, *, async_def: bool) -> str:
        cls = ast.AsyncFunctionDef if async_def else ast.FunctionDef
        nodes = [node for node in ast.walk(self.cog_tree) if isinstance(node, cls) and node.name == name]
        self.assertEqual(len(nodes), 1, name)
        return ast.get_source_segment(self.cog_text, nodes[0]) or ""

    def test_owner_dm_test_reuses_canonical_owner_and_rejects_other_users_first(self):
        source = self._function_source("_prefix_test_owner_dm", async_def=True)
        self.assertIn("configured_owner_id > 0 and author_id != configured_owner_id", source)
        self.assertIn("preferred_user=author", source)
        self.assertIn('getattr(target, "id", 0)', source)
        self.assertIn("!= author_id", source)
        self.assertIn("await target.send(", source)
        self.assertIn("allowed_mentions=discord.AllowedMentions.none()", source)

        # Só o dono reconhecido pode chegar ao feedback de falha. Usuário comum
        # retorna antes do resolver e nunca recebe resposta/reação.
        reject_pos = source.index("configured_owner_id > 0 and author_id != configured_owner_id")
        resolve_pos = source.index("_resolve_voice_failure_dm_target")
        self.assertLess(reject_pos, resolve_pos)
        self.assertNotIn("add_reaction", source)

    def test_testdm_is_a_real_hidden_prefix_command_and_works_in_dm(self):
        nodes = [
            node for node in ast.walk(self.cog_tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "test_owner_dm_command"
        ]
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        decorators = [ast.get_source_segment(self.cog_text, dec) or "" for dec in node.decorator_list]
        self.assertTrue(any('commands.command(name="testdm", hidden=True)' in dec for dec in decorators))
        source = ast.get_source_segment(self.cog_text, node) or ""
        self.assertIn("ctx.message", source)
        self.assertNotIn("ctx.guild", source)

        # bot.py encaminha primeiro ao gate do TTS e, em seguida, sempre chama
        # process_commands; como `_testdm` saiu do dispatcher TTS, não há dupla DM.
        bot_source = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn("await self._dispatch_tts_message_bridge(message)", bot_source)
        self.assertIn("await self.process_commands(message)", bot_source)

    def test_owner_dm_resolver_prefers_explicit_owner_and_handles_team_owner(self):
        source = self._function_source("_resolve_voice_failure_dm_target", async_def=True)
        self.assertIn("canonical_id = configured_owner_id or bot_owner_id", source)
        self.assertIn('team_owner = getattr(team, "owner", None)', source)
        self.assertIn("canonical_object = team_owner or app_owner", source)
        self.assertIn("preferred_id == canonical_id", source)
        self.assertNotIn("owner_ids", source)

    def test_owner_dm_test_is_components_v2_without_buttons_or_incident_side_effects(self):
        builder = self._function_source("_build_owner_dm_test_view", async_def=False)
        self.assertIn("discord.ui.LayoutView(timeout=None)", builder)
        self.assertIn("discord.ui.Container(", builder)
        self.assertIn("discord.ui.TextDisplay(", builder)
        self.assertIn("discord.ui.Separator()", builder)
        self.assertNotIn("discord.ui.Button", builder)
        self.assertNotIn("discord.ui.ActionRow", builder)

        handler = self._function_source("_prefix_test_owner_dm", async_def=True)
        failure_builder = self._function_source("_build_owner_dm_test_failure_view", async_def=False)
        self.assertIn("discord.ui.LayoutView(timeout=None)", failure_builder)
        self.assertIn("discord.ui.Container(", failure_builder)
        self.assertNotIn("discord.ui.Button", failure_builder)
        self.assertNotIn("discord.ui.ActionRow", failure_builder)
        for forbidden in (
            "_ensure_connected",
            "_reserve_voice_failure_alert",
            "_maybe_notify_owner_voice_incident",
            "_mark_voice_incidents_recovered",
            "_voice_incidents",
            "guild_states",
        ):
            self.assertNotIn(forbidden, handler)


class TTSPanelHistoryRemovalRegressionTests(unittest.TestCase):
    def test_last_changes_ui_and_runtime_history_code_are_fully_removed(self):
        production_files = [
            ROOT / "cogs" / "tts" / "cog.py",
            ROOT / "cogs" / "tts" / "ui.py",
            ROOT / "cogs" / "tts" / "utils" / "embed.py",
            ROOT / "cogs" / "tts" / "utils" / "panel_apply.py",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in production_files)
        for forbidden in (
            "Últimas alterações",
            "🕘 Últimas alterações",
            "Sincronizado com o histórico",
            "Nada recente",
            "get_panel_history",
            "set_user_panel_last_change",
            "set_guild_panel_last_change",
            "_append_public_panel_history",
            "_format_history_entries",
            "_format_status_history_entries",
            "history_entry",
            "history_text",
            "last_changes",
        ):
            self.assertNotIn(forbidden, combined)

    def test_legacy_history_module_contains_no_executable_code(self):
        path = ROOT / "cogs" / "tts" / "utils" / "history.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        executable = [node for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
        self.assertEqual(executable, [])

    def test_database_cleanup_unsets_panel_history_before_cache_load(self):
        text = (ROOT / "db.py").read_text(encoding="utf-8")
        tree = ast.parse(text)

        legacy_defs = {
            "get_panel_history",
            "set_user_panel_last_change",
            "set_guild_panel_last_change",
            "_settingsdb_get_panel_history",
            "_settingsdb_set_user_panel_last_change",
            "_settingsdb_set_guild_panel_last_change",
            "_settingsdb_history_list",
        }
        present_defs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(legacy_defs.isdisjoint(present_defs))

        cleanup_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cleanup_removed_tts_panel_history"
        ]
        self.assertEqual(len(cleanup_nodes), 1)
        cleanup_source = ast.get_source_segment(text, cleanup_nodes[0]) or ""
        self.assertIn('"panel_history": {"$exists": True}', cleanup_source)
        self.assertIn('{"$unset": {"panel_history": ""}}', cleanup_source)
        self.assertIn("update_many", cleanup_source)

        init_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "init"
        ]
        self.assertEqual(len(init_nodes), 1)
        init_source = ast.get_source_segment(text, init_nodes[0]) or ""
        cleanup_pos = init_source.index("_cleanup_removed_tts_panel_history")
        load_pos = init_source.index("load_cache")
        self.assertLess(cleanup_pos, load_pos)

        # Fora da migração idempotente, o schema antigo não deve reaparecer.
        self.assertNotIn("SettingsDB.get_panel_history", text)
        self.assertNotIn("SettingsDB.set_user_panel_last_change", text)
        self.assertNotIn("SettingsDB.set_guild_panel_last_change", text)


if __name__ == "__main__":
    unittest.main()
