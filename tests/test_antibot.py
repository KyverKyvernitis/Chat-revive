from __future__ import annotations

import ast
import json
from pathlib import Path

from cogs.antibot.constants import (
    CANCEL_EMOJI,
    COUNTDOWN_START,
    DELETE_MESSAGE_SECONDS,
    MAX_ENTRIES_PER_BATCH,
    MAX_RENDER_TEXT,
    RENDER_INTERVAL_SECONDS,
    STAFF_JOKE_VISIBLE_SECONDS,
    STATE_BANNED,
    STATE_BANNING,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_STAFF_JOKE,
    STATE_WAITING,
    WARNING_EMOJI,
)
from cogs.antibot.state import ChallengeEntry, render_batch, render_entry, render_key


ROOT = Path(__file__).resolve().parents[1]


def test_fixed_security_contract() -> None:
    assert COUNTDOWN_START == 5
    assert DELETE_MESSAGE_SECONDS == 86_400
    assert RENDER_INTERVAL_SECONDS == 2.0
    assert COUNTDOWN_START * RENDER_INTERVAL_SECONDS == 10.0
    assert CANCEL_EMOJI == "<:osaka:1539137127852539944>"
    assert WARNING_EMOJI == "<a:warning:1519862786870743070>"
    assert STAFF_JOKE_VISIBLE_SECONDS == 10.0


def test_each_user_keeps_an_independent_countdown() -> None:
    first = ChallengeEntry(user_id=111, countdown_value=4)
    second = ChallengeEntry(user_id=222, countdown_value=3)

    assert first.countdown_number(104.2) == 4
    assert second.countdown_number(104.2) == 3
    assert "Você será banido em 4 segundos" in render_entry(
        first, now=104.2, cancel_emoji=CANCEL_EMOJI
    )
    assert "Você será banido em 3 segundos" in render_entry(
        second, now=104.2, cancel_emoji=CANCEL_EMOJI
    )


def test_visual_countdown_advances_one_number_at_a_time() -> None:
    entry = ChallengeEntry(user_id=111)
    values = [entry.countdown_number(0.0)]

    for _ in range(4):
        assert entry.advance_countdown()
        values.append(entry.countdown_number(999.0))

    assert values == list(range(5, 0, -1))
    assert not entry.advance_countdown()


def test_render_key_skips_only_identical_visible_states() -> None:
    waiting = ChallengeEntry(user_id=111, countdown_value=5)
    next_step = ChallengeEntry(user_id=111, countdown_value=4)
    banning = ChallengeEntry(user_id=111, countdown_value=5, state=STATE_BANNING)
    cancelled = ChallengeEntry(user_id=222, state=STATE_CANCELLED, terminal_at=0.0)

    assert render_key([waiting], now=0.0) != render_key([next_step], now=2.0)
    assert render_key([waiting], now=2.0) == render_key([banning], now=2.0)
    assert render_key([cancelled], now=1.0) == render_key([cancelled], now=3.0)
    assert render_key([cancelled], now=1.0) != render_key(
        [ChallengeEntry(user_id=222, state=STATE_FAILED, terminal_at=0.0)],
        now=1.0,
    )


def test_exact_state_messages_and_user_identity() -> None:
    waiting = ChallengeEntry(user_id=123, state=STATE_WAITING)
    cancelled = ChallengeEntry(user_id=124, state=STATE_CANCELLED, terminal_at=1.0)
    banned = ChallengeEntry(user_id=125, state=STATE_BANNED, terminal_at=1.0)
    failed = ChallengeEntry(user_id=126, state=STATE_FAILED, terminal_at=1.0)
    staff = ChallengeEntry(
        user_id=127,
        state=STATE_STAFF_JOKE,
        terminal_at=1.0,
        staff_immune=True,
    )

    assert render_entry(waiting, now=10.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        f"## {WARNING_EMOJI} Você será banido em 5 segundos se não reagir",
        "<@123>",
        f"Reaja com {CANCEL_EMOJI} para cancelar",
    ]
    assert render_entry(cancelled, now=2.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        "<@124>",
        "Banimento cancelado",
    ]
    assert render_entry(banned, now=2.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        "<@125> · `125`",
        "Conta banida",
    ]
    assert render_entry(failed, now=2.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        "<@126> · `126`",
        "Banimento falhou",
        "Não foi possível banir o usuário",
    ]
    assert render_entry(staff, now=2.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        "<@127>",
        "Você foi **banido**",
        "-# quer dizer, se você não fosse staff né (ou se você não tivesse um cargo acima do meu). Agora seja um bom garoto e pare de falar aqui.",
    ]


def test_ban_is_permanent_but_other_terminal_states_expire() -> None:
    banned = ChallengeEntry(user_id=1, state=STATE_BANNED, terminal_at=0.0)
    cancelled = ChallengeEntry(user_id=2, state=STATE_CANCELLED, terminal_at=0.0)
    failed = ChallengeEntry(user_id=3, state=STATE_FAILED, terminal_at=0.0)
    staff = ChallengeEntry(user_id=4, state=STATE_STAFF_JOKE, terminal_at=1.0)

    assert banned.is_permanent
    assert not banned.transient_expired(10_000.0)
    assert cancelled.transient_expired(4.0)
    assert failed.transient_expired(8.0)
    assert not staff.transient_expired(10.999)
    assert staff.transient_expired(11.0)


def test_full_shared_batch_stays_inside_component_text_limit() -> None:
    entries = [
        ChallengeEntry(user_id=10_000_000_000_000_000_000 + index)
        for index in range(MAX_ENTRIES_PER_BATCH)
    ]
    rendered = render_batch(entries, now=0.0, cancel_emoji=CANCEL_EMOJI)

    assert len(rendered) <= MAX_RENDER_TEXT
    assert not rendered.startswith("# Antibot")
    assert rendered.count("Reaja com") == MAX_ENTRIES_PER_BATCH
    assert rendered.count(
        f"## {WARNING_EMOJI} Você será banido em 5 segundos se não reagir"
    ) == MAX_ENTRIES_PER_BATCH

    staff_entries = [
        ChallengeEntry(
            user_id=10_000_000_000_000_000_000 + index,
            state=STATE_STAFF_JOKE,
            terminal_at=0.0,
            staff_immune=True,
        )
        for index in range(MAX_ENTRIES_PER_BATCH)
    ]
    staff_rendered = render_batch(staff_entries, now=0.0, cancel_emoji=CANCEL_EMOJI)
    assert len(staff_rendered) <= MAX_RENDER_TEXT
    assert staff_rendered.count("Você foi **banido**") == MAX_ENTRIES_PER_BATCH

    overflow = staff_entries + [
        ChallengeEntry(
            user_id=10_000_000_000_000_000_999,
            state=STATE_STAFF_JOKE,
            terminal_at=0.0,
            staff_immune=True,
        )
    ]
    assert len(render_batch(overflow, now=0.0, cancel_emoji=CANCEL_EMOJI)) > MAX_RENDER_TEXT


def test_antibot_runs_before_tts_and_bot_author_shortcut() -> None:
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    on_message = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message"
    )
    body = ast.get_source_segment(source, on_message) or ""

    assert body.index("_dispatch_antibot_message_bridge") < body.index('getattr(message.author, "bot", False)')
    assert body.index("_dispatch_antibot_message_bridge") < body.index("_dispatch_tts_message_bridge")
    assert body.index("antibot_should_block_message") < body.index("_dispatch_antibot_message_bridge")


def test_common_message_path_does_not_await_antibot_bridge() -> None:
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    on_message = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message"
    )
    guarded_calls = [
        node
        for node in ast.walk(on_message)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Attribute)
            and child.attr == "_dispatch_antibot_message_bridge"
            for child in ast.walk(node)
        )
    ]

    assert guarded_calls
    assert any("antibot_guard" in (ast.get_source_segment(source, node.test) or "") for node in guarded_calls)


def test_fast_guard_stays_sync_and_memory_only() -> None:
    source = (ROOT / "cogs" / "antibot" / "cog.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "should_block_message_fast"
    )
    body = ast.get_source_segment(source, guard) or ""

    assert not any(isinstance(node, ast.Await) for node in ast.walk(guard))
    assert "_pending_by_member.get" in body
    assert "_active_channel_to_guild.get" in body
    assert "get_config" not in body
    assert "is_staff" not in body
    assert "self.db" not in body


def test_tts_gate_blocks_antibot_before_database_lookup() -> None:
    source = (ROOT / "cogs" / "tts" / "utils" / "message_gate.py").read_text(encoding="utf-8")
    assert source.index("antibot_should_block_message") < source.index("db = cog._get_db()")
    assert 'reason="antibot_guard"' in source


def test_other_text_message_listeners_use_the_same_fast_guard() -> None:
    listeners = (
        "cogs/chatbot/cog.py",
        "cogs/forms/cog.py",
        "cogs/games/__init__.py",
        "cogs/music.py",
        "cogs/role_cooldown.py",
        "cogs/say.py",
    )
    for relative_path in listeners:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "antibot_should_block_message" in source, relative_path


def test_antibot_config_is_persisted_before_cache_publish() -> None:
    source = (ROOT / "db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    setter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_settingsdb_set_antibot_config"
    )
    body = ast.get_source_segment(source, setter) or ""

    assert body.index("await self.coll.update_one") < body.index("self.guild_cache[guild_id]")
    assert "_save_guild_doc" not in body


def test_runtime_uses_rate_safe_render_and_24_hour_ban_cleanup() -> None:
    source = (ROOT / "cogs" / "antibot" / "cog.py").read_text(encoding="utf-8")
    assert "RENDER_INTERVAL_SECONDS" in source
    assert "entry.advance_countdown()" in source
    assert "if entry.countdown_value > 1:" in source
    assert "sem confirmação ao fim da contagem" in source
    assert "current_render_key == session.last_render_key" in source
    assert "session.last_render_key = current_render_key" in source
    assert "delete_message_seconds=DELETE_MESSAGE_SECONDS" in source
    assert "_live_session_by_guild" in source
    assert "clear_reaction(self._cancel_emoji)" in source
    assert "async def cog_unload" in source
    assert "reserved_update_channel" in source
    assert "call_later(" in source
    assert "asyncio.sleep(max(0.0, entry.next_step_at" not in source
    assert "entry.state in {STATE_CANCELLED, STATE_FAILED, STATE_STAFF_JOKE}" in source


def test_staff_countdown_and_active_channel_toggle_contract() -> None:
    source = (ROOT / "cogs" / "antibot" / "cog.py").read_text(encoding="utf-8")

    assert 'aliases=("armadilha", "trap")' in source
    assert "await get_context(message)" in source
    assert "deactivate_trap_if_channel" in source
    assert 'return True, "Canal de armadilha desativado"' in source
    assert "current_channel_id != int(expected_channel_id)" in source
    assert "delete_after=4.0 if ok else 8.0" in source
    assert "pending[1].staff_immune" in source
    assert "await self._start_challenge(message, staff_immune=is_staff_member)" in source
    assert "await self._set_terminal(session, entry, STATE_STAFF_JOKE)" in source

    state_source = (ROOT / "cogs" / "antibot" / "state.py").read_text(encoding="utf-8")
    assert "staff_immune: bool = False" in state_source


def test_help_catalog_exposes_trap_alias() -> None:
    catalog = json.loads((ROOT / "shared" / "help_catalog.json").read_text(encoding="utf-8"))
    command = next(item for item in catalog["entries"] if item.get("key") == "antibot")

    assert "trap" in command["aliases"]
    assert "trap" in command["search_terms"]


def test_compact_panel_and_trap_warning_copy() -> None:
    source = (ROOT / "cogs" / "antibot" / "views.py").read_text(encoding="utf-8")

    assert '"# 🪤 Canal de armadilha\\n\\n"' in source
    assert 'f"**{WARNING_EMOJI} AVISO:**' in source
    assert "este canal é feito para detectar contas hackeadas" in source
    assert "vai resultar no seu banimento**" in source
    assert 'placeholder="Escolha o canal de armadilha"' in source
    assert 'lines.append(f"Ativo · {current}")' in source
    assert 'lines.append(f"Selecionado · {selected}")' in source
    assert 'lines.append(f"Contagem regressiva · {COUNTDOWN_START} → 1")' in source
    assert 'rendered_notice = f"-# {notice}"' in source
    assert "Canal atual:" not in source
    assert "Canal selecionado:" not in source
    assert "Banimento após 10 segundos" not in source
    assert "Banimento em 10 segundos" not in source
