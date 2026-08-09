from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("discord")

from cogs.application_presence_admin import _parse_duration
from utility.application_presence import ApplicationPresenceService


class _Guild:
    def __init__(self, members: int):
        self.member_count = members
        self.members = []


class _Bot:
    def __init__(self):
        self.guilds = [_Guild(10), _Guild(20)]
        self.latency = 0.042
        self.started_at = None
        self.calls: list[tuple[object, object]] = []
        self._closed = False

    async def change_presence(self, *, status, activity):
        self.calls.append((status, activity))

    def is_closed(self) -> bool:
        return self._closed


def _service(tmp_path: Path) -> ApplicationPresenceService:
    return ApplicationPresenceService(
        _Bot(),
        tmp_path / "runtime-state.json",
        tmp_path / "data" / "application_presence.json",
    )


def test_presence_config_migrates_defaults_and_persists_edits(tmp_path: Path) -> None:
    service = _service(tmp_path)
    config_path = tmp_path / "data" / "application_presence.json"
    assert config_path.is_file()
    assert len(service.get_snapshot()["statuses"]) == 2

    async def scenario() -> None:
        status_id = await service.add_status("Ping {n:ping}", 90)
        await service.update_status(status_id, text="Teste", duration_seconds=120, position=1)
        assert service.get_snapshot()["statuses"][0]["id"] == status_id
        await service.remove_status(status_id)

    asyncio.run(scenario())

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert len(payload["statuses"]) == 2

    reloaded = _service(tmp_path)
    assert len(reloaded.get_snapshot()["statuses"]) == 2
    assert reloaded._render_template("{n:m} / {n:sv}") == "30 / 2"


def test_presence_duration_parser_is_human_and_guarded() -> None:
    assert _parse_duration("30s") == 30
    assert _parse_duration("1m") == 60
    assert _parse_duration("2m 30s") == 150
    assert _parse_duration("1h") == 3600
    assert _parse_duration("90") == 90
    with pytest.raises(ValueError):
        _parse_duration("10s")
    with pytest.raises(ValueError):
        _parse_duration("amanhã")


def test_single_status_has_no_rotation_timer(tmp_path: Path) -> None:
    config_path = tmp_path / "data" / "application_presence.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "enabled": True,
                "statuses": [{"id": "only", "text": "Só um", "duration_seconds": 60}],
            }
        ),
        encoding="utf-8",
    )
    service = ApplicationPresenceService(_Bot(), tmp_path / "runtime-state.json", config_path)
    now = time.monotonic()
    service._reconcile_runtime(now)
    assert service.get_snapshot()["current_id"] == "only"
    assert service._current_deadline is None
    assert service._next_wake_timeout(now) is None


def test_removing_current_status_selects_the_next_item(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.monotonic()
    service._reconcile_runtime(now)
    first_id = service.get_snapshot()["current_id"]
    assert first_id

    async def scenario() -> None:
        await service.remove_status(str(first_id))

    asyncio.run(scenario())
    service._reconcile_runtime(time.monotonic())
    snapshot = service.get_snapshot()
    assert len(snapshot["statuses"]) == 1
    assert snapshot["current_id"] == snapshot["statuses"][0]["id"]


def test_guild_refresh_only_wakes_dynamic_status(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._reconcile_runtime(time.monotonic())
    service._wake_event.clear()
    service.schedule_refresh(immediate=True)
    assert service._wake_event.is_set()

    async def make_static() -> None:
        current_id = str(service.get_snapshot()["current_id"])
        await service.update_status(current_id, text="Use _help", duration_seconds=60, position=1)

    asyncio.run(make_static())
    service._wake_event.clear()
    service.schedule_refresh(immediate=True)
    assert not service._wake_event.is_set()
