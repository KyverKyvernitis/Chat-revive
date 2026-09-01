from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

from cogs.tts.aliases import matches_prefixed_command
from cogs.tts.prefix import match_prefix_control_command


ROOT = Path(__file__).resolve().parents[1]
PING_PATH = ROOT / "utility" / "commands" / "ping.py"
ALIASES_PATH = ROOT / "cogs" / "tts" / "aliases.py"
TTS_PREFIX_PATH = ROOT / "cogs" / "tts" / "prefix.py"
HELP_CATALOG_PATH = ROOT / "shared" / "help_catalog.json"


class PingCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PING_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        helper_names = {
            "_safe_float",
            "_format_duration",
            "_latency_severity",
            "_database_display",
            "_overall_severity",
            "_status_detail",
        }
        threshold_names = {
            "_WEBSOCKET_THRESHOLDS",
            "_RESPONSE_THRESHOLDS",
            "_EVENT_LOOP_THRESHOLDS",
        }
        helper_tree = ast.Module(
            body=[
                node
                for node in cls.tree.body
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in helper_names
                )
                or (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id in threshold_names
                        for target in node.targets
                    )
                )
            ],
            type_ignores=[],
        )
        cls.helpers: dict[str, object] = {"Any": Any, "PingSnapshot": Any}
        exec(compile(helper_tree, str(PING_PATH), "exec"), cls.helpers)

    def test_ping_module_is_valid_python(self) -> None:
        self.assertIsInstance(self.tree, ast.Module)

    def test_duration_and_latency_thresholds_cover_boundaries(self) -> None:
        format_duration = self.helpers["_format_duration"]
        latency_severity = self.helpers["_latency_severity"]
        self.assertEqual(format_duration(90_061), "1d 1h 1m 1s")
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(latency_severity(149.9, (150.0, 300.0, 600.0)), 0)
        self.assertEqual(latency_severity(150.0, (150.0, 300.0, 600.0)), 1)
        self.assertEqual(latency_severity(600.0, (150.0, 300.0, 600.0)), 3)
        self.assertEqual(latency_severity(None, (150.0, 300.0, 600.0)), 0)

    def test_response_status_ignores_normal_discord_api_variation(self) -> None:
        latency_severity = self.helpers["_latency_severity"]
        overall_severity = self.helpers["_overall_severity"]
        status_detail = self.helpers["_status_detail"]
        response_thresholds = self.helpers["_RESPONSE_THRESHOLDS"]
        self.assertEqual(latency_severity(444.0, response_thresholds), 0)
        self.assertEqual(latency_severity(699.9, response_thresholds), 0)
        self.assertEqual(latency_severity(700.0, response_thresholds), 1)
        self.assertEqual(latency_severity(2_500.0, response_thresholds), 3)

        normal = SimpleNamespace(
            websocket_ms=120.0,
            response_ms=444.0,
            event_loop_ms=1.0,
            database_ok=True,
            bot_healthy=True,
        )
        self.assertEqual(overall_severity(normal), 0)
        self.assertIsNone(status_detail(normal, 0))
        normal.database_ok = False
        self.assertEqual(overall_severity(normal), 3)

    def test_status_copy_identifies_the_metric_causing_lag(self) -> None:
        overall_severity = self.helpers["_overall_severity"]
        status_detail = self.helpers["_status_detail"]

        yellow_response = SimpleNamespace(
            websocket_ms=120.0,
            response_ms=843.0,
            event_loop_ms=1.0,
            database_ok=True,
            bot_healthy=True,
        )
        yellow_severity = overall_severity(yellow_response)
        self.assertEqual(yellow_severity, 1)
        self.assertEqual(
            status_detail(yellow_response, yellow_severity),
            "Tempo de resposta um pouco alto",
        )

        red_response = SimpleNamespace(
            websocket_ms=120.0,
            response_ms=2_896.0,
            event_loop_ms=1.0,
            database_ok=True,
            bot_healthy=True,
        )
        red_severity = overall_severity(red_response)
        self.assertEqual(red_severity, 3)
        self.assertEqual(
            status_detail(red_response, red_severity),
            "Tempo de resposta alto",
        )

        high_ping = SimpleNamespace(
            websocket_ms=400.0,
            response_ms=250.0,
            event_loop_ms=1.0,
            database_ok=True,
            bot_healthy=True,
        )
        ping_severity = overall_severity(high_ping)
        self.assertEqual(status_detail(high_ping, ping_severity), "Ping alto")

    def test_slash_and_dynamic_prefix_entries_share_the_panel(self) -> None:
        self.assertIn('@app_commands.command(name="ping"', self.source)
        self.assertIn('@commands.Cog.listener("on_message")', self.source)
        self.assertIn('matches_prefixed_command(raw_content, bot_prefix, kind="ping")', self.source)
        self.assertGreaterEqual(self.source.count("PingPanelView("), 3)

    def test_panel_uses_components_v2_and_no_legacy_embed(self) -> None:
        for component in (
            "discord.ui.LayoutView",
            "discord.ui.Container",
            "discord.ui.TextDisplay",
            "discord.ui.Section",
            "discord.ui.Thumbnail",
        ):
            self.assertIn(component, self.source)
        self.assertNotIn("discord.Embed", self.source)
        self.assertIn("content=None", self.source)
        self.assertIn("embeds=[]", self.source)
        self.assertIn("attachments=[]", self.source)
        self.assertIn("safe_send_interaction_message", self.source)

    def test_database_uses_monitored_health_instead_of_object_presence(self) -> None:
        database_display = self.helpers["_database_display"]
        self.assertEqual(database_display(True), ("", "on", 0))
        self.assertEqual(database_display(False), (" 🔴", "off", 3))
        self.assertIn('"mongo_ok" in health', self.source)
        self.assertIn('getattr(self.bot, "health_state", {})', self.source)
        self.assertNotIn("client.admin.command", self.source)
        self.assertNotIn("get_health_snapshot()", self.source)

    def test_metrics_collection_is_non_blocking(self) -> None:
        self.assertIn("_PROCESS.cpu_percent(interval=None)", self.source)
        self.assertNotIn("cpu_percent(interval=1", self.source)
        self.assertNotIn("await asyncio.sleep", self.source)

    def test_panel_uses_requested_compact_labels_and_voice_summary(self) -> None:
        self.assertIn('"**Conexão**"', self.source)
        self.assertIn('"**Sistema & TTS**"', self.source)
        self.assertIn('"Tudo normal por aqui"', self.source)
        self.assertIn('"Meio Instável"', self.source)
        self.assertIn('"Instável"', self.source)
        self.assertNotIn("Pequena oscilação", self.source)
        self.assertNotIn("Instabilidade detectada", self.source)
        self.assertIn('f"📡 **Ping**', self.source)
        self.assertIn('f"🔄 **EventLoop**', self.source)
        self.assertIn('f"🗄️ **DB**', self.source)
        self.assertIn("voice_connections", self.source)
        self.assertIn('f"🌐 **Servidores:**', self.source)
        self.assertIn("usando o bot em call", self.source)
        self.assertNotIn("tts_queue_size", self.source)
        self.assertNotIn("Fila TTS", self.source)
        self.assertNotIn('get_cog("TTSVoice")', self.source)
        self.assertNotIn('"## Conexão"', self.source)
        self.assertNotIn('"## Processo"', self.source)
        self.assertNotIn("WebSocket mede a conexão", self.source)

    def test_ping_prefix_is_registered_and_documented(self) -> None:
        aliases = ALIASES_PATH.read_text(encoding="utf-8")
        self.assertIn('"ping": {', aliases)
        self.assertIn('"aliases": ("ping",)', aliases)

        tts_prefix = TTS_PREFIX_PATH.read_text(encoding="utf-8")
        self.assertIn('kind="ping"', tts_prefix)
        self.assertIn('PrefixControlCommand("ping")', tts_prefix)
        self.assertIn('kind in {"help", "ping"}', tts_prefix)

        catalog = json.loads(HELP_CATALOG_PATH.read_text(encoding="utf-8"))
        entry = next(item for item in catalog["entries"] if item.get("key") == "ping")
        self.assertEqual(entry["usage"], "{bot_prefix}ping")
        self.assertEqual(entry["aliases"], ["ping"])
        self.assertEqual(entry["slash_path"], "ping")

    def test_prefix_match_is_exact_and_tts_absorbs_the_control_command(self) -> None:
        self.assertTrue(matches_prefixed_command("_ping", "_", kind="ping"))
        self.assertTrue(matches_prefixed_command("!PING", "!", kind="ping"))
        self.assertFalse(matches_prefixed_command("_ping agora", "_", kind="ping"))
        self.assertFalse(matches_prefixed_command("_pingando", "_", kind="ping"))

        command = match_prefix_control_command("_ping", "_")
        self.assertIsNotNone(command)
        self.assertEqual(command.kind, "ping")


if __name__ == "__main__":
    unittest.main()
