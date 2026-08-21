from __future__ import annotations

import ast
from pathlib import Path

from cogs.antibot.constants import (
    CANCEL_EMOJI,
    COUNTDOWN_SECONDS,
    DELETE_MESSAGE_SECONDS,
    MAX_ENTRIES_PER_BATCH,
    MAX_RENDER_TEXT,
    STATE_BANNED,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_WAITING,
)
from cogs.antibot.state import ChallengeEntry, render_batch, render_entry


ROOT = Path(__file__).resolve().parents[1]


def test_fixed_security_contract() -> None:
    assert COUNTDOWN_SECONDS == 10
    assert DELETE_MESSAGE_SECONDS == 86_400
    assert CANCEL_EMOJI == "<:osaka:1539137127852539944>"


def test_each_user_keeps_an_independent_countdown() -> None:
    first = ChallengeEntry(user_id=111, deadline=110.0)
    second = ChallengeEntry(user_id=222, deadline=115.0)

    assert first.remaining_seconds(104.2) == 6
    assert second.remaining_seconds(104.2) == 11
    assert "Banimento em 6 segundos" in render_entry(first, now=104.2, cancel_emoji=CANCEL_EMOJI)
    assert "Banimento em 11 segundos" in render_entry(second, now=104.2, cancel_emoji=CANCEL_EMOJI)


def test_exact_state_messages_and_user_identity() -> None:
    waiting = ChallengeEntry(user_id=123, deadline=20.0, state=STATE_WAITING)
    cancelled = ChallengeEntry(user_id=124, state=STATE_CANCELLED, terminal_at=1.0)
    banned = ChallengeEntry(user_id=125, state=STATE_BANNED, terminal_at=1.0)
    failed = ChallengeEntry(user_id=126, state=STATE_FAILED, terminal_at=1.0)

    assert render_entry(waiting, now=10.0, cancel_emoji=CANCEL_EMOJI).splitlines() == [
        "<@123>",
        f"Reaja com {CANCEL_EMOJI} para cancelar",
        "Banimento em 10 segundos",
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


def test_ban_is_permanent_but_other_terminal_states_expire() -> None:
    banned = ChallengeEntry(user_id=1, state=STATE_BANNED, terminal_at=0.0)
    cancelled = ChallengeEntry(user_id=2, state=STATE_CANCELLED, terminal_at=0.0)
    failed = ChallengeEntry(user_id=3, state=STATE_FAILED, terminal_at=0.0)

    assert banned.is_permanent
    assert not banned.transient_expired(10_000.0)
    assert cancelled.transient_expired(4.0)
    assert failed.transient_expired(8.0)


def test_full_shared_batch_stays_inside_component_text_limit() -> None:
    entries = [
        ChallengeEntry(user_id=10_000_000_000_000_000_000 + index, deadline=10.0)
        for index in range(MAX_ENTRIES_PER_BATCH)
    ]
    rendered = render_batch(entries, now=0.0, cancel_emoji=CANCEL_EMOJI)

    assert len(rendered) <= MAX_RENDER_TEXT
    assert rendered.count("Reaja com") == MAX_ENTRIES_PER_BATCH
    assert rendered.count("Banimento em 10 segundos") == MAX_ENTRIES_PER_BATCH


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


def test_runtime_uses_one_second_render_and_24_hour_ban_cleanup() -> None:
    source = (ROOT / "cogs" / "antibot" / "cog.py").read_text(encoding="utf-8")
    assert "RENDER_INTERVAL_SECONDS" in source
    assert "delete_message_seconds=DELETE_MESSAGE_SECONDS" in source
    assert "_live_session_by_guild" in source
    assert "clear_reaction(self._cancel_emoji)" in source
    assert "async def cog_unload" in source
    assert "reserved_update_channel" in source
    assert "call_later(" in source
    assert "asyncio.sleep(max(0.0, deadline" not in source


def test_compact_panel_and_trap_warning_copy() -> None:
    source = (ROOT / "cogs" / "antibot" / "views.py").read_text(encoding="utf-8")

    assert '"# Canal armadilha\\n\\n"' in source
    assert "<a:warning:1519862786870743070> AVISO:" in source
    assert "este canal é feito para detectar contas hackeadas" in source
    assert "vai resultar no seu banimento**" in source
    assert 'lines.append(f"Ativo · {current}")' in source
    assert 'lines.append(f"Selecionado · {selected}")' in source
    assert 'lines.append("Banimento em 10 segundos")' in source
    assert 'rendered_notice = f"-# {notice}"' in source
    assert "Canal atual:" not in source
    assert "Canal selecionado:" not in source
    assert "Banimento após 10 segundos" not in source
