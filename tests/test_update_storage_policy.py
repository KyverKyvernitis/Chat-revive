from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
INSTALLER = ROOT / "scripts" / "install-vps-systemd-units.sh"
JOURNAL_POLICY = ROOT / "deploy" / "journald" / "60-tts-bot-storage.conf"
TMPFILES_POLICY = ROOT / "deploy" / "tmpfiles.d" / "tts-bot-storage.conf"


def _run_bash(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-Eeuo", "pipefail", "-c", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_updater_runtime_is_systemd_managed_and_uses_unpredictable_names() -> None:
    updater = UPDATER.read_text(encoding="utf-8")
    assert 'mktemp "$UPDATER_RUNTIME_BASE/tts-bot-update.XXXXXX.run"' in updater
    assert 'tts-bot-update.$$.run' not in updater
    assert 'mktemp "$UPDATER_EPHEMERAL_DIR/tts-bot-updater.XXXXXX.log"' in updater
    assert "prune_updater_runtime_orphans" in updater
    assert "guard_updater_disk_space" in updater
    assert updater.index("prune_update_artifacts || true") < updater.index("if ! guard_updater_disk_space; then")

    for unit in (
        ROOT / "deploy/systemd/tts-bot-updater.service",
        ROOT / "deploy/systemd/vps/tts-bot-updater.service",
    ):
        text = unit.read_text(encoding="utf-8")
        assert "RuntimeDirectory=tts-bot-updater" in text
        assert "RuntimeDirectoryPreserve=no" in text
        assert "TTS_BOT_UPDATER_RUNTIME_DIR=/run/tts-bot-updater" in text


def test_native_os_storage_policies_are_bounded_and_conservative() -> None:
    journal = JOURNAL_POLICY.read_text(encoding="utf-8")
    assert "SystemMaxUse=500M" in journal
    assert "SystemKeepFree=2G" in journal
    assert "RuntimeMaxUse=128M" in journal
    assert "MaxRetentionSec=14day" in journal

    tmpfiles = TMPFILES_POLICY.read_text(encoding="utf-8")
    active_rules = [line for line in tmpfiles.splitlines() if line and not line.startswith("#")]
    assert active_rules == ["e /var/lib/snapd/cache - - - 14d"]
    assert "/var/lib/containerd" not in tmpfiles


def test_disk_guard_blocks_only_the_update_when_space_is_critical(tmp_path: Path) -> None:
    harness = f"""
source <(awk '/^human_storage_bytes[(][)]/{{flag=1}} /^prune_update_artifacts[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={tmp_path!s}
LOG_TAG=test-updater
TTS_BOT_UPDATER_DISK_HARD_MIN_BYTES=20
TTS_BOT_UPDATER_DISK_HARD_MIN_PERCENT=5
TTS_BOT_UPDATER_DISK_SOFT_MIN_PERCENT=15
df() {{ printf 'Filesystem 1B-blocks Used Available Use%% Mounted on\\nmock 1000 960 40 96%% /\\n'; }}
logger() {{ :; }}
send_error() {{ printf 'ALERT=%s|%s|%s\\n' "$1" "$2" "$3"; }}
set +e
guard_updater_disk_space
rc=$?
set -e
printf 'RC=%s\\n' "$rc"
"""
    result = _run_bash(harness)

    assert "RC=1" in result.stdout
    assert "ALERT=Update pausado por pouco espaço|" in result.stdout
    assert "|updater-disk-critical-" in result.stdout
    assert "Bot: preservado e sem reinício" in result.stdout


def test_disk_guard_allows_update_above_the_hard_floor(tmp_path: Path) -> None:
    harness = f"""
source <(awk '/^human_storage_bytes[(][)]/{{flag=1}} /^prune_update_artifacts[(][)]/{{flag=0}} flag' {UPDATER!s})
REPO_DIR={tmp_path!s}
LOG_TAG=test-updater
TTS_BOT_UPDATER_DISK_HARD_MIN_BYTES=20
TTS_BOT_UPDATER_DISK_HARD_MIN_PERCENT=5
TTS_BOT_UPDATER_DISK_SOFT_MIN_PERCENT=15
df() {{ printf 'Filesystem 1B-blocks Used Available Use%% Mounted on\\nmock 1000 900 100 90%% /\\n'; }}
logger() {{ :; }}
send_error() {{ printf 'UNEXPECTED_ALERT\\n'; return 1; }}
send_warn() {{ printf 'WARN=%s|%s\\n' "$1" "$3"; }}
guard_updater_disk_space
printf 'RC=%s\\n' "$?"
"""
    result = _run_bash(harness)

    assert "RC=0" in result.stdout
    assert "UNEXPECTED_ALERT" not in result.stdout
    assert "WARN=Espaço da VPS abaixo do nível preventivo|updater-disk-warning-" in result.stdout


def test_runtime_orphan_cleanup_is_aged_and_prefix_scoped(tmp_path: Path) -> None:
    stale_run = tmp_path / "tts-bot-update.stale.run"
    fresh_run = tmp_path / "tts-bot-update.fresh.run"
    unrelated = tmp_path / "unrelated.run"
    stale_candidate = tmp_path / "tts-bot-remote-candidate.stale"
    fresh_candidate = tmp_path / "tts-bot-remote-candidate.fresh"
    for path in (stale_run, fresh_run, unrelated):
        path.write_text("safe", encoding="utf-8")
    stale_candidate.mkdir()
    fresh_candidate.mkdir()
    now = time.time()
    os.utime(stale_run, (now - 7200, now - 7200))
    os.utime(stale_candidate, (now - 25200, now - 25200))

    harness = f"""
source <(awk '/^prune_updater_runtime_orphans[(][)]/{{flag=1}} /^prune_rejected_remote_commits[(][)]/{{flag=0}} flag' {UPDATER!s})
TMPDIR={tmp_path!s}
REPO_DIR={ROOT!s}
sudo() {{ :; }}
prune_updater_runtime_orphans
"""
    _run_bash(harness)

    assert not stale_run.exists()
    assert not stale_candidate.exists()
    assert fresh_run.exists()
    assert fresh_candidate.exists()
    assert unrelated.exists()


def test_candidate_archive_retention_keeps_only_newest_generations(tmp_path: Path) -> None:
    archive = tmp_path / "done"
    archive.mkdir()
    names = ["candidate-a", "candidate-b", "candidate-c", "candidate-d"]
    now = time.time()
    for index, name in enumerate(names):
        path = archive / name
        path.mkdir()
        os.utime(path, (now + index, now + index))
    outside = tmp_path / "outside"
    outside.mkdir()
    link = archive / "candidate-link"
    link.symlink_to(outside, target_is_directory=True)

    harness = f"""
source <(awk '/^prune_archive_root[(][)]/{{flag=1}} /^prune_updater_runtime_orphans[(][)]/{{flag=0}} flag' {UPDATER!s})
prune_archive_root {archive!s} 365 2
"""
    _run_bash(harness)

    assert sorted(path.name for path in archive.iterdir()) == ["candidate-c", "candidate-d", link.name]
    assert link.is_symlink() and outside.is_dir()


def test_rejected_commit_history_has_age_and_count_limits(tmp_path: Path) -> None:
    rejected = tmp_path / "rejected.json"
    now = datetime.now(timezone.utc)
    rejected.write_text(
        json.dumps(
            {
                "commits": {
                    "old": {"rejected_at": (now - timedelta(days=90)).isoformat()},
                    "new-a": {"rejected_at": (now - timedelta(hours=3)).isoformat()},
                    "new-b": {"rejected_at": (now - timedelta(hours=2)).isoformat()},
                    "new-c": {"rejected_at": (now - timedelta(hours=1)).isoformat()},
                }
            }
        ),
        encoding="utf-8",
    )
    harness = f"""
source <(awk '/^prune_rejected_remote_commits[(][)]/{{flag=1}} /^human_storage_bytes[(][)]/{{flag=0}} flag' {UPDATER!s})
REMOTE_REJECTED_FILE={rejected!s}
DISCORD_AUTO_UPDATE_REJECTED_RETENTION_DAYS=30
DISCORD_AUTO_UPDATE_REJECTED_KEEP_COUNT=2
chown() {{ :; }}
prune_rejected_remote_commits
"""
    _run_bash(harness)

    retained = json.loads(rejected.read_text(encoding="utf-8"))["commits"]
    assert set(retained) == {"new-c", "new-b"}
    assert not list(tmp_path.glob(".rejected.json.*.tmp"))


def _installer_file_harness(
    template_dir: Path,
    systemd_dir: Path,
    backup_root: Path,
) -> str:
    return f"""
source <(awk '/^log[(][)]/{{flag=1}} /^install_dir_files[(][)]/{{flag=0}} flag' {INSTALLER!s})
TEMPLATE_DIR={template_dir!s}
SYSTEMD_DIR={systemd_dir!s}
BACKUP_ROOT={backup_root!s}
BACKUP_DIR="$BACKUP_ROOT/20260814-100000"
STATUS_FILE={template_dir.parent / 'status.json'!s}
DRY_RUN=0
CHANGED=0
ACTIONS=()
WARNINGS=()
ensure_paths
install_file tts-bot.service
printf 'CHANGED=%s BACKUP=%s\n' "$CHANGED" "$(test -d "$BACKUP_ROOT" && echo yes || echo no)"
"""


def test_external_storage_policy_is_not_rewritten_without_a_real_change(tmp_path: Path) -> None:
    source = tmp_path / "source.conf"
    live = tmp_path / "etc/policy.conf"
    backup = tmp_path / "backups/20260814-100000"
    source.write_text("limit=true\n", encoding="utf-8")
    live.parent.mkdir()
    live.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(live, 0o644)

    harness = f"""
source <(awk '/^install_storage_policy_file[(][)]/{{flag=1}} /^truthy_env[(][)]/{{flag=0}} flag' {INSTALLER!s})
DRY_RUN=0
BACKUP_DIR={backup!s}
CHANGED=0
POLICY_CHANGED=0
action() {{ printf 'ACTION=%s\\n' "$*"; }}
warn() {{ printf 'WARN=%s\\n' "$*"; }}
install_storage_policy_file {source!s} {live!s} policy.conf test POLICY_CHANGED
printf 'CHANGED=%s POLICY=%s BACKUP=%s\\n' "$CHANGED" "$POLICY_CHANGED" "$(test -d "$BACKUP_DIR" && echo yes || echo no)"
"""
    unchanged = _run_bash(harness)
    assert "CHANGED=0 POLICY=0 BACKUP=no" in unchanged.stdout

    live.write_text("limit=false\n", encoding="utf-8")
    changed = _run_bash(harness)
    assert "CHANGED=1 POLICY=1 BACKUP=yes" in changed.stdout
    assert live.read_text(encoding="utf-8") == "limit=true\n"
    assert (backup / "policy.conf").read_text(encoding="utf-8") == "limit=false\n"


def test_systemd_installer_skips_identical_unit_without_backup(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    systemd = tmp_path / "systemd"
    backups = tmp_path / "backups"
    templates.mkdir()
    systemd.mkdir()
    content = "[Service]\nExecStart=/bin/true\n"
    (templates / "tts-bot.service").write_text(content, encoding="utf-8")
    live = systemd / "tts-bot.service"
    live.write_text(content, encoding="utf-8")
    os.chmod(live, 0o644)

    result = _run_bash(_installer_file_harness(templates, systemd, backups))

    assert "CHANGED=0 BACKUP=no" in result.stdout
    assert "mantido sem regravar" in result.stdout


def test_systemd_installer_backs_up_only_a_real_change(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    systemd = tmp_path / "systemd"
    backups = tmp_path / "backups"
    templates.mkdir()
    systemd.mkdir()
    (templates / "tts-bot.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    live = systemd / "tts-bot.service"
    live.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    os.chmod(live, 0o644)

    result = _run_bash(_installer_file_harness(templates, systemd, backups))

    backup = backups / "20260814-100000/tts-bot.service"
    assert "CHANGED=1 BACKUP=yes" in result.stdout
    assert backup.read_text(encoding="utf-8") == "[Service]\nExecStart=/bin/false\n"
    assert live.read_text(encoding="utf-8") == "[Service]\nExecStart=/bin/true\n"


def test_systemd_backup_retention_keeps_only_newest_generations(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    names = [
        "20260810-100000",
        "20260811-100000",
        "20260812-100000",
        "20260813-100000",
        "20260814-100000",
    ]
    for name in names:
        (backups / name).mkdir()
    unrelated = backups / "manual-do-not-touch"
    unrelated.mkdir()

    harness = f"""
source <(awk '/^prune_systemd_backups[(][)]/{{flag=1}} /^write_status[(][)]/{{flag=0}} flag' {INSTALLER!s})
DRY_RUN=0
BACKUP_ROOT={backups!s}
SYSTEMD_BACKUP_KEEP_COUNT=3
ACTIONS=()
action() {{ ACTIONS+=("$*"); }}
prune_systemd_backups
"""
    _run_bash(harness)

    retained = sorted(path.name for path in backups.iterdir())
    assert retained == [*names[-3:], unrelated.name]
