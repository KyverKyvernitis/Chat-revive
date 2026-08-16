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


if __name__ == "__main__":
    unittest.main()
