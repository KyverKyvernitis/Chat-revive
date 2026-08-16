from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VoiceConnectionSourceRegressionTests(unittest.TestCase):
    def test_tts_connect_disables_discord_py_parallel_reconnect_loop(self):
        tree = ast.parse((ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8"))
        builders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_connect_kwargs"
        ]
        self.assertEqual(len(builders), 1)

        returned_dicts = [
            node.value
            for node in ast.walk(builders[0])
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        ]
        self.assertEqual(len(returned_dicts), 1)
        values = {
            key.value: value
            for key, value in zip(returned_dicts[0].keys, returned_dicts[0].values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        self.assertIn("reconnect", values)
        self.assertIsInstance(values["reconnect"], ast.Constant)
        self.assertIs(values["reconnect"].value, False)

        legacy_events = (ROOT / "cogs" / "tts" / "events.py").read_text(encoding="utf-8")
        self.assertIn("voice_channel.connect(self_deaf=True, reconnect=False)", legacy_events)

    def test_nested_text_inputs_do_not_duplicate_component_v2_labels(self):
        tree = ast.parse((ROOT / "cogs" / "tts" / "ui.py").read_text(encoding="utf-8"))
        expected_targets = {"manual_input", "custom_values", "role_input"}
        checked_targets: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            if not isinstance(node.value.func, ast.Name) or node.value.func.id != "_make_modal_text_input":
                continue
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            relevant = target_names & expected_targets
            if not relevant:
                continue
            label_keyword = next((kw for kw in node.value.keywords if kw.arg == "label"), None)
            self.assertIsNotNone(label_keyword)
            self.assertIsInstance(label_keyword.value, ast.Constant)
            self.assertIsNone(label_keyword.value.value)
            checked_targets.update(relevant)

        self.assertEqual(checked_targets, expected_targets)

    def test_python_stdout_is_unbuffered_for_journald_ordering(self):
        start_script = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn("exec python3 -u bot.py", start_script)

    def test_voice_owner_alerts_are_incident_based_and_components_v2(self):
        source = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        self.assertNotIn("_voice_first_connect_fail_notified", source)
        self.assertNotIn("_notify_owner_voice_connect_failure_once", source)
        self.assertIn("def _voice_failure_policy", source)
        self.assertIn("def _reserve_voice_failure_alert", source)
        self.assertIn("discord.ui.LayoutView(timeout=None)", source)
        self.assertIn("discord.ui.Container(", source)
        self.assertIn("discord.ui.TextDisplay(", source)
        self.assertIn("await target.send(view=view)", source)
        self.assertNotIn("Primeira falha de conexão de voz detectada neste boot", source)

    def test_transient_bot_member_failure_requires_persistence_or_multiple_guilds(self):
        tree = ast.parse((ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8"))
        policies = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_voice_failure_policy"
        ]
        self.assertEqual(len(policies), 1)
        source = ast.get_source_segment(
            (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8"),
            policies[0],
        ) or ""
        self.assertIn('"bot_member_unavailable"', source)
        self.assertIn('"threshold": 6', source)
        self.assertIn('"global_threshold": 3', source)
        self.assertIn('global_threshold * 2', (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8"))
        self.assertIn('"channel_full"', source)
        self.assertIn('"suppress": True', source)

    def test_restore_attempts_feed_incident_tracker_instead_of_alerting_first_failure(self):
        source = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        self.assertNotIn("report_failure=(attempt == 0)", source)
        self.assertNotIn("notify_owner_on_failure=(attempt == 0)", source)
        self.assertIn("report_failure=True", source)
        self.assertIn("restore automático após reinício · tentativa {attempt + 1}/4", source)
        self.assertIn("restore em runtime ({reason}) · tentativa {attempt + 1}", source)

    def test_voice_incident_dm_resolver_does_not_fan_out_to_team_members(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        resolvers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_resolve_voice_failure_dm_target"
        ]
        self.assertEqual(len(resolvers), 1)
        source = ast.get_source_segment(text, resolvers[0]) or ""
        self.assertIn("application_info", source)
        self.assertIn('getattr(app, "owner", None)', source)
        self.assertNotIn("owner_ids", source)
        self.assertNotIn("team.members", source)


if __name__ == "__main__":
    unittest.main()
