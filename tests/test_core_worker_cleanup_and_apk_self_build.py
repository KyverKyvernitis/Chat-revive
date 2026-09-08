from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"
AUTOMATION = ROOT / "scripts/core-worker-automation.py"
REGISTRY = ROOT / "utility/commands/workers_registry.py"
WEBSERVER = ROOT / "webserver.py"
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"
START = ROOT / "deploy/termux/phone-worker/start-phone-worker.sh"
WORKERS = ROOT / "utility/commands/workers.py"
SELF_BUILDER = ANDROID / "app/src/main/python/coreworker/apk_self_builder.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cleanup_release_versions_are_monotonic() -> None:
    gradle = read(ANDROID / "app/build.gradle")
    phone = read(PHONE)
    assert 'versionCode 133' in gradle
    assert 'versionName "0.8.6"' in gradle
    assert 'PHONE_WORKER_VERSION = "1.11.6"' in phone


def test_core_screen_hides_internal_runtime_noise_and_manual_recovery() -> None:
    activity = read(JAVA / "MainActivity.java")
    assert 'prepareCard.addView(sectionTitle("Neste celular"))' in activity
    assert 'technicalCard.addView(sectionTitle("Avançado"))' in activity
    assert 'technicalDetailsContent.addView(pairingForm)' in activity
    assert 'connectCard.addView(pairingForm)' not in activity
    assert 'prepareCard.addView(rootfsHeroText)' not in activity
    assert 'prepareCard.addView(runnerStatusText)' not in activity
    assert 'secondaryButton("Recovery de pareamento")' in activity
    assert 'connectCard.setVisibility(paired ? View.GONE : View.VISIBLE)' in activity
    assert 'secondaryButton("Sincronizar status")' in activity
    assert 'addProfileRadio("leve"' in activity
    assert 'addProfileRadio("midia"' in activity
    assert 'addProfileRadio("turbo"' in activity
    assert 'addProfileRadio("builder"' not in activity
    assert 'addProfileRadio("completo"' not in activity
    assert 'addProfileRadio("bedrock"' not in activity
    assert '✅ Autobuild pronto\\nAtualizações podem ser compiladas neste celular' in activity


def test_builder_ui_observes_private_state_without_manual_sync() -> None:
    activity = read(JAVA / "MainActivity.java")
    assert 'OnSharedPreferenceChangeListener privateStateListener' in activity
    assert 'registerOnSharedPreferenceChangeListener(privateStateListener)' in activity
    assert 'unregisterOnSharedPreferenceChangeListener(privateStateListener)' in activity
    assert 'key.startsWith("apk_self_builder_")' in activity
    assert 'refreshBuilderHeroStatus()' in activity


def test_discord_groups_parent_and_apk_child_as_one_physical_phone() -> None:
    workers = read(WORKERS)
    for marker in (
        'def _physical_worker_id(',
        'def _physical_groups(',
        'def _preferred_runtime(',
        'def _visible_registry_workers(',
        'def _physical_group_for_worker_id(',
    ):
        assert marker in workers
    assert 'Melhor celular disponível' in workers
    assert 'APK' in workers and 'Termux' in workers and 'fallback' in workers


def test_apk_child_is_preferred_for_next_real_build() -> None:
    automation = load("cleanup_builder_selection", AUTOMATION)
    now = time.time()
    smoke_fp = "9" * 64
    snapshot = {"workers": [
        {
            "worker_id": "phone-test",
            "enabled": True,
            "online": True,
            "last_seen": now,
            "last_heartbeat_at": now,
            "runtime_kind": "termux",
            "runtime_mode": "termux",
            "source": "termux-phone-worker",
            "source_hash": "b" * 64,
            "version": "1.11.4",
            "roles": ["phone-worker", "apk-builder"],
            "capabilities": ["phone-worker", "apk-builder"],
            "supported_tasks": ["apk_build_debug"],
            "battery": {"level": 80, "charging": False},
        },
        {
            "worker_id": "phone-test-apk",
            "parent_worker_id": "phone-test",
            "physical_worker_id": "phone-test",
            "enabled": True,
            "online": True,
            "last_seen": now + 1,
            "last_heartbeat_at": now + 1,
            "runtime_kind": "apk",
            "source": "core-worker-apk-agent-service",
            "version": "0.8.5",
            "versionCode": 132,
            "roles": ["apk-worker", "apk-builder"],
            "capabilities": ["apk-native", "apk-builder", "apk-self-builder", "apk-durable-jobs-v1"],
            "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
            "battery": {"level": 80, "charging": False},
            "status": {"apk_self_builder": {
                "ok": True,
                "ready": True,
                "state": "apk_self_builder_ready",
                "checkedAt": int(now * 1000),
                "smoke": {"ok": True, "fingerprint": smoke_fp},
            }},
        },
    ]}
    selected = automation._select_apk_builder(
        snapshot,
        target_agent_version="1.11.4",
        target_agent_source_hash="b" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"
    assert selected["runtime_kind"] == "apk"
    assert selected["toolchain_fingerprint"] == smoke_fp


def test_low_battery_blocks_new_job_until_power_context_changes() -> None:
    automation = load("cleanup_power_gate", AUTOMATION)
    assert automation._worker_power_blocked({"battery": {"level": 11, "charging": False}}) is True
    assert automation._worker_power_blocked({"battery": {"level": 11, "charging": True}}) is False
    assert automation._worker_power_blocked({"battery": {"level": 25, "charging": False}}) is False


def test_registry_preserves_version_code_for_apk_builder_selection() -> None:
    registry = load("cleanup_registry_version_code", REGISTRY)
    now = time.time()
    public = registry._compact_worker_public({
        "worker_id": "phone-test-apk",
        "enabled": True,
        "last_heartbeat_at": now,
        "versionCode": 129,
        "runtime_kind": "apk",
        "status": {"apk_self_builder": {"ready": True}},
        "capabilities": ["apk-builder"],
    }, now=now)
    assert public["versionCode"] == 129
    malformed = registry._compact_worker_public({
        "worker_id": "bad-apk", "enabled": True, "last_heartbeat_at": now, "versionCode": "oops"
    }, now=now)
    assert malformed["versionCode"] == 0


def test_publish_gate_accepts_smoke_fingerprint_fallback() -> None:
    source = read(WEBSERVER)
    assert '(preflight.get("smoke") or {}).get("fingerprint")' in source
    automation = read(AUTOMATION)
    assert '(preflight.get("smoke") or {}).get("fingerprint")' in automation


def test_supervisor_accepts_historical_release_as_owned_without_broad_kill() -> None:
    start = read(START)
    assert '"$RUNTIME_ROOT"/releases/*' in start
    assert '"$WORKER_DIR"/.releases/*' in start
    assert "pkill -f 'phone_worker.py'" not in start
    assert 'pkill -f "phone_worker.py"' not in start


def test_termux_housekeeping_defaults_are_small() -> None:
    phone = read(PHONE)
    env = read(ROOT / "deploy/termux/phone-worker/phone-worker.env.example")
    assert "DEFAULT_APK_BUILD_KEEP_ARTIFACTS = 3" in phone
    assert '_env_int("PHONE_WORKER_APK_BUILD_KEEP_ARTIFACTS", DEFAULT_APK_BUILD_KEEP_ARTIFACTS)' in phone
    assert "DEFAULT_APK_BUILD_KEEP_LOGS = 12" in phone
    assert "DEFAULT_APK_BUILD_KEEP_WORKDIRS = 1" in phone
    assert 'PHONE_WORKER_APK_BUILD_KEEP_ARTIFACTS=3' in env
    assert 'PHONE_WORKER_APK_BUILD_KEEP_LOGS=12' in env
    assert 'PHONE_WORKER_APK_BUILD_KEEP_WORKDIRS=1' in env
    assert 'latest_payload = _read_json_file(latest_meta)' in phone
    assert 'for sidecar in artifact_dir.glob("*.apk.json")' in phone


def test_apk_private_builder_cleanup_keeps_recent_artifacts(tmp_path: Path) -> None:
    python_root = ANDROID / "app/src/main/python"
    sys.path.insert(0, str(python_root))
    try:
        module = load("cleanup_private_builder_storage", SELF_BUILDER)
    finally:
        try:
            sys.path.remove(str(python_root))
        except ValueError:
            pass
    builder = tmp_path / "apk-self-builder"
    artifacts = builder / "artifacts"
    logs = builder / "logs"
    artifacts.mkdir(parents=True)
    logs.mkdir(parents=True)
    apks = []
    for index in range(6):
        apk = artifacts / f"CoreWorker-{index}.apk"
        apk.write_bytes(b"x" * (index + 1))
        apk.with_suffix(".apk.json").write_text("{}", encoding="utf-8")
        ts = 1000 + index
        os.utime(apk, (ts, ts))
        os.utime(apk.with_suffix(".apk.json"), (ts, ts))
        apks.append(apk)
    for index in range(12):
        log = logs / f"build-{index}.log"
        log.write_text(str(index), encoding="utf-8")
        ts = 2000 + index
        os.utime(log, (ts, ts))

    result = module._cleanup_private_builder_storage(builder, current_apk=apks[-1])
    assert result["removed"] > 0
    assert len(list(artifacts.glob("*.apk"))) == 3
    assert apks[-1].exists()
    assert len(list(logs.glob("*.log"))) == 8
