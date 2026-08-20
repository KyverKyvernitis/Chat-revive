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

    def test_cold_tts_connection_defers_only_post_connect_maintenance(self):
        cog_source = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        audio_source = (ROOT / "cogs" / "tts" / "audio.py").read_text(encoding="utf-8")

        self.assertIn("defer_post_connect: bool = False", cog_source)
        self.assertIn("new_vc = await voice_channel.connect(**connect_kwargs)", cog_source)
        self.assertIn("pending[guild.id] = new_vc", cog_source)
        self.assertIn("task = self._schedule_tts_background(_complete_safely())", cog_source)
        self.assertIn("defer_post_connect=True", audio_source)
        self.assertIn("if not post_connect_is_pending:", audio_source)

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
        self.assertIn("await target.send(view=view, allowed_mentions=discord.AllowedMentions.none())", source)
        self.assertNotIn("Primeira falha de conexão de voz detectada neste boot", source)

    def test_voice_incident_dm_is_live_reused_and_has_no_buttons(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        builders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build_owner_voice_incident_view"
        ]
        self.assertEqual(len(builders), 1)
        builder_source = ast.get_source_segment(text, builders[0]) or ""
        self.assertNotIn("discord.ui.Button", builder_source)
        self.assertNotIn("discord.ui.ActionRow", builder_source)
        self.assertIn("# ✅ Voz recuperada", builder_source)
        self.assertIn("# 🔁 Incidente de voz reaberto", builder_source)
        self.assertIn("# ↗️ Incidente consolidado", builder_source)

        self.assertIn("await message.edit(view=view)", text)
        self.assertIn("_VOICE_INCIDENT_EDIT_MIN_INTERVAL_SECONDS = 12.0", text)
        self.assertIn("_VOICE_INCIDENT_REOPEN_WINDOW_SECONDS = 10 * 60", text)
        self.assertIn('action in {"open", "reopen", "promote"}', text)

    def test_voice_incident_recovers_from_real_healthy_voice_state(self):
        source = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        self.assertIn("def _schedule_voice_incident_recovery", source)
        self.assertIn("async def _mark_voice_incidents_recovered", source)
        self.assertIn('incident["status"] = "recovered"', source)
        self.assertIn("voice_state confirmou conexão saudável", source)
        self.assertIn("nova conexão de voz concluída", source)
        self.assertIn("Um sucesso real zera o ruído antigo daquela guild", source)
        self.assertIn("falha após recuperação não reabra o incidente por contagem herdada", source)

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

    def test_missing_member_cache_is_advisory_and_does_not_block_connect(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        checks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_diagnose_voice_connect_precheck"
        ]
        self.assertEqual(len(checks), 1)
        source = ast.get_source_segment(text, checks[0]) or ""
        self.assertIn("pré-checagem sem membro do bot no cache; conexão seguirá", source)
        self.assertNotIn('return "não consegui encontrar o membro do bot dentro do servidor"', source)
        self.assertIn("não consegui checar permissões localmente; conexão seguirá", source)

    def test_incident_reporting_never_blocks_voice_connection_path(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        ensures = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ensure_connected"
        ]
        self.assertEqual(len(ensures), 1)
        source = ast.get_source_segment(text, ensures[0]) or ""
        self.assertIn("await voice_channel.connect(**connect_kwargs)", source)
        self.assertIn("self._schedule_voice_failure_report(", source)
        self.assertNotIn("await self._maybe_notify_owner_voice_incident(", source)
        # O único await do pipeline de DM fica encapsulado no task em background;
        # restore de boot/runtime e conexão manual não esperam telemetria.
        self.assertEqual(text.count("await self._maybe_notify_owner_voice_incident("), 1)

        schedulers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_schedule_voice_failure_report"
        ]
        self.assertEqual(len(schedulers), 1)
        scheduler_source = ast.get_source_segment(text, schedulers[0]) or ""
        self.assertIn("put_nowait", scheduler_source)
        self.assertIn("asyncio.QueueFull", scheduler_source)
        self.assertFalse(any(isinstance(node, ast.Await) for node in ast.walk(schedulers[0])))
        self.assertNotIn("asyncio.create_task", scheduler_source)
        self.assertIn("conexão não foi afetada", scheduler_source)

        workers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_ensure_voice_incident_report_worker"
        ]
        self.assertEqual(len(workers), 1)
        worker_source = ast.get_source_segment(text, workers[0]) or ""
        self.assertIn("asyncio.create_task", worker_source)
        self.assertIn("tts-voice-incident-worker", worker_source)
        self.assertIn("await asyncio.sleep(0)", worker_source)


    def test_incident_pipeline_has_bounded_memory_and_never_spawns_task_per_failure(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        self.assertIn("_VOICE_INCIDENT_REPORT_QUEUE_MAXSIZE = 96", text)
        self.assertIn("_VOICE_INCIDENT_WORKER_YIELD_EVERY = 16", text)
        self.assertIn("_VOICE_FAILURE_GUILD_EVENT_MAX = 48", text)
        self.assertIn("_VOICE_FAILURE_GLOBAL_EVENT_MAX = 192", text)
        self.assertIn("asyncio.Queue(maxsize=_VOICE_INCIDENT_REPORT_QUEUE_MAXSIZE)", text)
        self.assertIn("del guild_events[:-_VOICE_FAILURE_GUILD_EVENT_MAX]", text)
        self.assertIn("del global_events[:-_VOICE_FAILURE_GLOBAL_EVENT_MAX]", text)
        self.assertNotIn("_voice_incident_report_tasks", text)

    def test_incident_context_distinguishes_user_impact_from_background_restore(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        profiles = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_voice_failure_context_profile"
        ]
        self.assertEqual(len(profiles), 1)
        profile_source = ast.get_source_segment(text, profiles[0]) or ""
        self.assertIn('"entrada automática do tts"', profile_source)
        self.assertIn('"impact": "user_blocking"', profile_source)
        self.assertIn('"restore automático"', profile_source)
        self.assertIn('"impact": "background"', profile_source)
        self.assertIn('"threshold_multiplier": 2', profile_source)

        reserves = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_reserve_voice_failure_alert"
        ]
        self.assertEqual(len(reserves), 1)
        reserve_source = ast.get_source_segment(text, reserves[0]) or ""
        self.assertIn("effective_threshold = threshold * threshold_multiplier", reserve_source)
        self.assertIn("direct_count >= effective_threshold", reserve_source)

    def test_incident_dm_explains_stage_functional_impact_and_load_protection(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        builders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build_owner_voice_incident_view"
        ]
        self.assertEqual(len(builders), 1)
        source = ast.get_source_segment(text, builders[0]) or ""
        self.assertIn("**Etapa**", source)
        self.assertIn("**Impacto funcional**", source)
        self.assertIn("**Prioridade calculada**", source)
        self.assertIn("**Contexto de inicialização**", source)
        self.assertIn("**Proteção de carga**", source)
        self.assertNotIn("discord.ui.Button", source)
        self.assertNotIn("discord.ui.ActionRow", source)

    def test_recovery_requires_stable_local_voice_state_before_closing_incident(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        schedulers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_schedule_voice_incident_recovery"
        ]
        self.assertEqual(len(schedulers), 1)
        source = ast.get_source_segment(text, schedulers[0]) or ""
        self.assertIn("_voice_connection_matches_target", source)
        self.assertIn("_VOICE_INCIDENT_RECOVERY_STABILITY_SECONDS", source)
        self.assertIn("_voice_incident_has_failure_since", source)
        self.assertIn("duas observações locais saudáveis", source)

    def test_failed_owner_dm_attempts_are_debounced_too(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        self.assertIn('"last_sync_attempt_mono": 0.0', text)
        self.assertIn('incident["last_sync_attempt_mono"] = now', text)
        self.assertIn("last_activity = max(last_edit, last_attempt)", text)
        self.assertIn("evitando martelar a API do Discord", text)

    def test_incident_classification_does_not_wait_for_discord_dm_io(self):
        text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_maybe_notify_owner_voice_incident"
        ]
        self.assertEqual(len(handlers), 1)
        source = ast.get_source_segment(text, handlers[0]) or ""
        self.assertNotIn("await self._sync_owner_voice_incident_message", source)
        self.assertIn("self._schedule_owner_voice_incident_refresh(incident, force=True)", source)

    def test_audio_worker_auto_join_uses_current_incident_reporting_keyword(self):
        audio_text = (ROOT / "cogs" / "tts" / "audio.py").read_text(encoding="utf-8")
        self.assertIn("report_failure=True", audio_text)
        self.assertNotIn("notify_owner_on_failure=True", audio_text)

        cog_text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        tree = ast.parse(cog_text)
        ensures = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ensure_connected"
        ]
        self.assertEqual(len(ensures), 1)
        kwonly = {arg.arg for arg in ensures[0].args.kwonlyargs}
        self.assertIn("report_failure", kwonly)
        # Alias intencional para patches/branches antigos não derrubarem o
        # auto-join com TypeError por keyword inesperado.
        self.assertIn("notify_owner_on_failure", kwonly)

    def test_all_tts_ensure_connected_keyword_calls_are_signature_compatible(self):
        cog_text = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
        cog_tree = ast.parse(cog_text)
        ensures = [
            node
            for node in ast.walk(cog_tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ensure_connected"
        ]
        self.assertEqual(len(ensures), 1)
        accepted = {arg.arg for arg in ensures[0].args.kwonlyargs}

        for rel in ("cogs/tts/cog.py", "cogs/tts/audio.py", "cogs/tts/events.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not isinstance(func, ast.Attribute) or func.attr != "_ensure_connected":
                    continue
                unknown = {kw.arg for kw in call.keywords if kw.arg is not None} - accepted
                self.assertFalse(unknown, f"{rel} usa keyword inválido em _ensure_connected: {unknown}")


if __name__ == "__main__":
    unittest.main()
