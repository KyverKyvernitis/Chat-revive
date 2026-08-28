from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = ROOT / "cogs" / "games" / "services" / "base.py"
ADMIN_PATH = ROOT / "cogs" / "games" / "commands" / "admin.py"


class GamesInputModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = BASE_PATH.read_text(encoding="utf-8")
        cls.admin = ADMIN_PATH.read_text(encoding="utf-8")
        ast.parse(cls.base)
        ast.parse(cls.admin)

    @staticmethod
    def _function_source(source: str, name: str) -> str:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                segment = ast.get_source_segment(source, node)
                if segment is None:
                    break
                return str(segment)
        raise AssertionError(f"função não encontrada: {name}")

    def test_prefix_commands_are_allowed_in_both_input_modes(self) -> None:
        ensure = self._function_source(self.base, "_ensure_games_command_entry")
        self.assertNotIn('_gincana_input_mode(guild.id) != "commands"', ensure)
        self.assertNotIn("Modo por triggers", ensure)
        self.assertIn("_gincana_channel_matches", ensure)

    def test_bare_triggers_remain_exclusive_to_trigger_mode(self) -> None:
        allowed = self._function_source(self.base, "_games_trigger_entry_allowed")
        self.assertIn('_gincana_input_mode(guild.id) != "triggers"', allowed)
        self.assertIn("_gincana_channel_matches", allowed)

    def test_admin_copy_describes_asymmetric_modes(self) -> None:
        self.assertIn("Comandos permanecem disponíveis nos dois modos", self.admin)
        self.assertIn('mode_text = "Somente comandos" if mode == "commands" else "Triggers + comandos"', self.admin)
        self.assertIn("Triggers ativados; comandos continuam disponíveis", self.admin)
        self.assertNotIn("Apenas uma forma fica ativa por vez", self.admin)


if __name__ == "__main__":
    unittest.main()
