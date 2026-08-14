from __future__ import annotations

import os
from pathlib import Path

import pytest

from utility.storage_maintenance import prune_core_worker_releases


def _release(root: Path, name: str, size: int, mtime: int) -> Path:
    path = root / name
    path.write_bytes(bytes([mtime % 251]) * size)
    os.utime(path, (mtime, mtime))
    return path


def test_release_pruning_preserves_current_two_previous_and_unrelated_files(tmp_path: Path) -> None:
    current = _release(tmp_path, "CoreWorker-v0.7.3-debug.apk", 40, 400)
    previous = _release(tmp_path, "CoreWorker-v0.7.2-debug.apk", 30, 300)
    second_previous = _release(tmp_path, "CoreWorker-v0.7.1-debug.apk", 20, 200)
    obsolete = _release(tmp_path, "CoreWorker-v0.7.0-debug.apk", 10, 100)
    current_idsig = _release(tmp_path, current.name + ".idsig", 4, 400)
    previous_idsig = _release(tmp_path, previous.name + ".idsig", 3, 300)
    obsolete_idsig = _release(tmp_path, obsolete.name + ".idsig", 2, 100)
    orphan_idsig = _release(tmp_path, "CoreWorker-v0.6.9-debug.apk.idsig", 1, 50)
    source_zip = _release(tmp_path, "source-core-worker-app.zip", 5, 10)
    agent_zip = _release(tmp_path, "phone-worker-agent-current.zip", 5, 10)
    latest = _release(tmp_path, "latest.json", 5, 10)

    outside = tmp_path.parent / "outside.apk"
    outside.write_bytes(b"outside")
    symlink = tmp_path / "CoreWorker-v9.9.9-debug.apk"
    symlink.symlink_to(outside)

    result = prune_core_worker_releases(
        tmp_path,
        current.name,
        keep=3,
        max_total_bytes=1024,
    )

    assert result["ok"] is True
    assert result["removed"] == [obsolete.name]
    assert result["removedIdsig"] == sorted([obsolete_idsig.name, orphan_idsig.name])
    assert current.exists()
    assert previous.exists()
    assert second_previous.exists()
    assert current_idsig.exists()
    assert previous_idsig.exists()
    assert not obsolete.exists()
    assert not obsolete_idsig.exists()
    assert not orphan_idsig.exists()
    assert source_zip.exists() and agent_zip.exists() and latest.exists()
    assert symlink.is_symlink() and outside.read_bytes() == b"outside"


def test_release_byte_budget_drops_old_versions_but_never_current(tmp_path: Path) -> None:
    current = _release(tmp_path, "CoreWorker-v0.7.3-debug.apk", 80, 300)
    old_a = _release(tmp_path, "CoreWorker-v0.7.2-debug.apk", 50, 200)
    old_b = _release(tmp_path, "CoreWorker-v0.7.1-debug.apk", 40, 100)

    result = prune_core_worker_releases(
        tmp_path,
        current.name,
        keep=3,
        max_total_bytes=100,
    )

    assert result["retained"] == [current.name]
    assert result["retainedBytes"] == 80
    assert current.exists()
    assert not old_a.exists()
    assert not old_b.exists()


def test_release_pruning_keeps_oversized_current_and_rejects_unsafe_name(tmp_path: Path) -> None:
    current = _release(tmp_path, "CoreWorker-v0.7.3-debug.apk", 120, 100)

    result = prune_core_worker_releases(
        tmp_path,
        current.name,
        keep=1,
        max_total_bytes=100,
    )
    assert result["currentExceedsBudget"] is True
    assert current.exists()

    with pytest.raises(ValueError, match="basename seguro"):
        prune_core_worker_releases(tmp_path, "../CoreWorker-v0.7.3-debug.apk")


def test_release_pruning_requires_real_current_apk(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="arquivo regular"):
        prune_core_worker_releases(tmp_path, "CoreWorker-v0.7.3-debug.apk")
