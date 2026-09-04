from __future__ import annotations

import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / "scripts/core-worker-automation.py"
PHONE = ROOT / "deploy/termux/phone-worker/phone_worker.py"
MAIN = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker/MainActivity.java"
GRADLE = ROOT / "android/core-worker-app/app/build.gradle"
WORKERS = ROOT / "utility/commands/workers.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apk_worker(*, online: bool, age: float = 5.0) -> dict:
    return {
        "worker_id": "phone-test-apk",
        "parent_worker_id": "phone-test",
        "physical_worker_id": "phone-test",
        "enabled": True,
        "online": online,
        "last_seen_age_seconds": age,
        "last_heartbeat_at": time.time() - age,
        "runtime_kind": "apk",
        "source": "core-worker-apk-agent-service",
        "version": "0.8.2",
        "versionCode": 129,
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder"],
        "supported_tasks": ["apk_build_debug", "apk_publish_last"],
        "battery": {"level": 80, "charging": False},
        "status": {"apk_self_builder": {
            "ready": True,
            "ok": True,
            "smoke": {"ok": True, "fingerprint": "9" * 64},
        }},
    }


def termux_worker() -> dict:
    return {
        "worker_id": "phone-test",
        "enabled": True,
        "online": True,
        "last_heartbeat_at": time.time(),
        "runtime_kind": "termux",
        "runtime_mode": "termux",
        "source": "termux-phone-worker",
        "version": "1.11.5",
        "source_hash": "b" * 64,
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["apk_build_debug", "worker_update"],
        "battery": {"level": 80, "charging": False},
    }


def test_release_versions_advance_after_failed_083_source() -> None:
    gradle = GRADLE.read_text(encoding="utf-8")
    phone = PHONE.read_text(encoding="utf-8")
    assert 'versionCode 132' in gradle
    assert 'versionName "0.8.5"' in gradle
    assert 'PHONE_WORKER_VERSION = "1.11.5"' in phone


def test_main_activity_lifecycle_flags_precede_field_initializers() -> None:
    source = MAIN.read_text(encoding="utf-8")
    destroyed = source.index("private volatile boolean activityDestroyed")
    startup = source.index("private volatile boolean fullStartupDone")
    first_initializer = min(
        source.index("private final Runnable bedrockFullTerminalRefreshRunnable"),
        source.index("private final Runnable autoEnrollmentUiRefreshRunnable"),
        source.index("private final SharedPreferences.OnSharedPreferenceChangeListener privateStateListener"),
    )
    assert destroyed < first_initializer
    assert startup < first_initializer
    assert source.count("private volatile boolean activityDestroyed") == 1
    assert source.count("private volatile boolean fullStartupDone") == 1


def test_recent_ready_apk_gets_reconnect_grace_before_termux_fallback() -> None:
    automation = load("hotfix084_grace", AUTOMATION)
    selected = automation._select_apk_builder(
        {"workers": [termux_worker(), apk_worker(online=False, age=30)]},
        target_agent_version="1.11.5",
        target_agent_source_hash="b" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"
    assert selected["wait_for_online"] is True

    selected_after_grace = automation._select_apk_builder(
        {"workers": [termux_worker(), apk_worker(online=False, age=180)]},
        target_agent_version="1.11.5",
        target_agent_source_hash="b" * 64,
    )
    assert selected_after_grace["worker_id"] == "phone-test"
    assert selected_after_grace["runtime_kind"] == "termux"


def test_online_ready_apk_still_wins_immediately() -> None:
    automation = load("hotfix084_online", AUTOMATION)
    selected = automation._select_apk_builder(
        {"workers": [termux_worker(), apk_worker(online=True, age=2)]},
        target_agent_version="1.11.5",
        target_agent_source_hash="b" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"
    assert selected["runtime_kind"] == "apk"
    assert not selected.get("wait_for_online")


def test_worker_does_not_poll_new_job_with_unsent_final_result(monkeypatch) -> None:
    phone = load("hotfix084_phone", PHONE)
    monkeypatch.setattr(phone, "_core_worker_auth_parts", lambda: ("http://vps", "token", "phone-test"))
    monkeypatch.setattr(phone, "_flush_pending_core_worker_job_results", lambda **_kw: 0)
    monkeypatch.setattr(phone, "_pending_core_job_result_count", lambda: 1)

    def should_not_poll(*_a, **_kw):
        raise AssertionError("não deve buscar outro job com resultado final pendente")

    monkeypatch.setattr(phone, "_post_core_worker_json", should_not_poll)
    assert phone._poll_core_worker_job_once(
        host="0.0.0.0", port=8766, max_body_bytes=1024, max_output_bytes=1024, job_timeout=60, timeout=1.0
    ) is False


def test_recent_worker_update_delivery_suppresses_duplicate_job() -> None:
    automation = load("hotfix084_delivery", AUTOMATION)
    worker = {
        "updater_last_delivery_at": time.time() - 20,
        "updater_last_delivery_target_version": "1.11.5",
        "updater_last_delivery_target_hash": "a" * 64,
    }
    assert automation._worker_recent_update_delivery_matches(worker, "1.11.5", "a" * 64)
    assert not automation._worker_recent_update_delivery_matches(worker, "1.11.5", "b" * 64)


def test_grouped_panel_does_not_apply_termux_agent_version_to_apk() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    assert 'apk_version = _agent_version_label(apk.get("version"))' not in source
    assert 'apk_version = _shorten(apk.get("version") or "sem versão"' in source
    assert "APK e Termux são tratados como um único celular" not in source
