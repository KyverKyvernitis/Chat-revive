from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from utility.commands.workers_registry import CoreWorkerRegistryError, CoreWorkersRegistry, _hash_secret


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"
SELF_BUILDER = ANDROID / "app/src/main/python/coreworker/apk_self_builder.py"
AUTOMATION = ROOT / "scripts/core-worker-automation.py"
WORKERS = ROOT / "utility/commands/workers.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_self_builder():
    python_root = ANDROID / "app/src/main/python"
    sys.path.insert(0, str(python_root))
    try:
        return _load("self_builder_086_tests", SELF_BUILDER)
    finally:
        sys.path.remove(str(python_root))


def _automation():
    return _load(f"automation_086_{time.time_ns()}", AUTOMATION)


def _termux(worker_id: str = "phone-test", token: str = "termux-token") -> dict[str, Any]:
    now = time.time()
    return {
        "worker_id": worker_id,
        "name": "telefone",
        "enabled": True,
        "token_hash": _hash_secret(token),
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "last_seen": now,
        "last_seen_age_seconds": 0,
        "online": True,
        "runtime_kind": "termux",
        "physical_worker_id": worker_id,
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "version": "1.11.5",
        "source_hash": "a" * 64,
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["worker_update", "apk_build_debug", "apk_publish_last"],
        "status": {},
    }


def _apk(
    worker_id: str = "phone-test-apk",
    token: str = "apk-token",
    *,
    online: bool = True,
    ready: bool = True,
    age: float = 0.0,
    lease_tokens: bool = False,
) -> dict[str, Any]:
    now = time.time()
    seen = now - age if online or age else now - age
    caps = ["apk-native", "apk-durable-jobs-v1"]
    roles = ["apk-worker"]
    tasks = ["apk_builder_status"]
    if ready:
        caps += ["apk-builder", "apk-self-builder", "apk-publisher"]
        roles += ["apk-builder"]
        tasks += ["apk_build_debug", "apk_publish_last"]
    if lease_tokens:
        caps.append("apk-job-lease-token-v1")
    return {
        "worker_id": worker_id,
        "name": "telefone · APK",
        "enabled": True,
        "token_hash": _hash_secret(token),
        "registered_at": now - 100,
        "updated_at": seen,
        "last_heartbeat_at": seen,
        "last_seen_age_seconds": age,
        "online": online,
        "runtime_kind": "apk",
        "parent_worker_id": "phone-test",
        "physical_worker_id": "phone-test",
        "source": "core-worker-apk-foreground-service",
        "platform": "android",
        "version": "0.8.5",
        "versionCode": 132,
        "roles": roles,
        "capabilities": caps,
        "supported_tasks": tasks,
        "apk_builder_last_ready_at": now if ready else 0,
        "status": {"apk_self_builder": {
            "ready": ready,
            "ok": ready,
            "publishReady": ready,
            "state": "apk_self_builder_ready" if ready else "apk_self_builder_refreshing",
            "checkedAt": int(now * 1000),
            "smoke": {"ok": ready, "fingerprint": "b" * 64},
        }, "core_worker_jobs": {"active_job_id": "", "pending_result_count": 0}},
    }


def _registry(tmp_path: Path, *, lease_tokens: bool = False) -> tuple[CoreWorkersRegistry, str, str]:
    termux_token = "termux-token"
    apk_token = "apk-token"
    data = {
        "version": 1,
        "pairings": {},
        "workers": {
            "phone-test": _termux(token=termux_token),
            "phone-test-apk": _apk(token=apk_token, lease_tokens=lease_tokens),
        },
        "jobs": {},
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return CoreWorkersRegistry(path), termux_token, apk_token


def _create_build(registry: CoreWorkersRegistry, *, max_attempts: int = 2) -> str:
    result = registry.create_job(
        job_type="apk_build_debug",
        payload={"versionName": "0.8.6", "versionCode": 133, "sourceFingerprint": "c" * 64},
        target_worker_id="phone-test-apk",
        required_capabilities=["apk-builder"],
        ttl_seconds=7200,
        lease_seconds=240,
        max_attempts=max_attempts,
        summary="build automático APK 0.8.6",
    )
    return result["job"]["job_id"]


def _telemetry_functions() -> dict[str, Any]:
    tree = ast.parse(WORKERS.read_text(encoding="utf-8"))
    wanted = {
        "_env_float", "_shorten", "_telemetry_epoch",
        "_physical_telemetry_snapshot", "_physical_telemetry_text",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"Any": Any, "os": os, "re": re, "time": time}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(WORKERS), "exec"), namespace)
    return namespace


def test_01_ready_apk_beats_termux() -> None:
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), _apk()]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"
    assert selected["runtime_kind"] == "apk"


def test_vps_received_readiness_survives_android_clock_skew(tmp_path: Path) -> None:
    future = int((time.time() + 24 * 60 * 60) * 1000)
    apk = _apk()
    apk["status"]["apk_self_builder"]["checkedAt"] = future
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), apk]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"

    registry, _termux_token, apk_token = _registry(tmp_path)
    heartbeat = _apk()
    heartbeat["status"]["apk_self_builder"]["checkedAt"] = future
    registry.heartbeat(heartbeat, token=apk_token)
    job_id = _create_build(registry)
    assert registry.get_job(job_id)["job"]["target_worker_id"] == "phone-test-apk"


def test_02_recent_apk_restart_grace_does_not_fall_to_termux() -> None:
    apk = _apk(ready=False)
    apk["apk_builder_last_ready_at"] = time.time() - 10
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), apk]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test-apk"
    assert selected["wait_for_readiness"] is True


def test_03_apk_offline_after_grace_allows_termux() -> None:
    apk = _apk(online=False, ready=True, age=600)
    apk["apk_builder_last_ready_at"] = time.time() - 600
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), apk]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test"
    assert selected["runtime_kind"] == "termux"


def test_online_apk_without_prior_ready_proof_does_not_block_fallback() -> None:
    apk = _apk(ready=False)
    apk["apk_builder_last_ready_at"] = 0
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), apk]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test"
    assert selected["runtime_kind"] == "termux"


def test_fresh_deterministic_readiness_failure_uses_termux() -> None:
    apk = _apk(ready=False)
    apk["apk_builder_last_ready_at"] = time.time() - 10
    apk["status"]["apk_self_builder"].update({
        "state": "apk_self_builder_blocked",
        "summary": "JDK 17 ausente",
        "missing": ["java"],
    })
    selected = _automation()._select_apk_builder(
        {"workers": [_termux(), apk]},
        target_agent_version="1.11.5",
        target_agent_source_hash="a" * 64,
    )
    assert selected["worker_id"] == "phone-test"


def test_04_worker_update_is_never_targeted_to_apk(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    result = registry.create_job(
        job_type="worker_update",
        payload={"version": "1.11.6", "source_hash": "d" * 64},
        target_worker_id="phone-test-apk",
        required_capabilities=["phone-worker"],
    )
    assert result["job"]["target_worker_id"] == "phone-test"


def test_05_running_runtime_does_not_receive_a_second_job(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path)
    first = _create_build(registry)
    second = _create_build(registry)
    assert registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]["job_id"] == first
    busy = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)
    assert busy["job"] is None and busy["busy_job_id"] == first
    assert registry.get_job(second)["job"]["status"] == "queued"


def test_06_active_job_is_required_before_execution() -> None:
    source = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    persist = source.index('if (!persistActiveJob(job, "claimed"')
    execute = source.index("CoreWorkerApkBuildManager.execute(")
    assert persist < execute
    assert "active_job não pôde ser persistido antes da execução" in source


def test_07_active_job_clears_only_after_result_ack() -> None:
    source = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    final = source[source.index("boolean sent = postResultEnvelope"):]
    assert "if (sent && resourcesReleased)" in final
    assert final.index("if (sent && resourcesReleased)") < final.index("clearActiveJob(jobId)")
    assert "resultado final não pôde ser persistido na outbox" in source


def test_08_restart_reconciles_active_job_without_silent_clear() -> None:
    source = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    recovery = source[source.index("private boolean recoverInterruptedActiveJob"):source.index("private Thread startJobLeaseKeeper")]
    assert "reconcileInterruptedBuild" in recovery
    assert '"abandon"' in recovery
    assert "outbox != null && outbox.isFile()" in recovery
    assert 'persistActiveJob(active, "result_pending"' in recovery
    assert recovery.index("outbox != null && outbox.isFile()") < recovery.index("reconcileInterruptedBuild")


def test_09_running_without_apk_ownership_is_requeued_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_WORKER_APK_EXECUTOR_STALE_GRACE_SECONDS", "30")
    monkeypatch.setenv("CORE_WORKER_APK_EXECUTOR_MISMATCH_CONFIRM_SECONDS", "15")
    registry, _, apk_token = _registry(tmp_path)
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["updated_at"] = time.time() - 100
    data["jobs"][job_id]["executor_missing_since"] = time.time() - 40
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    registry.heartbeat({
        "worker_id": "phone-test-apk", "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "status": {"core_worker_jobs": {"active_job_id": "", "pending_result_count": 0}},
    }, token=apk_token)
    job = registry.get_job(job_id)["job"]
    assert job["status"] == "queued"
    assert job["progress_stage"] == "requeued_after_executor_loss"


def test_10_readiness_loading_is_transient() -> None:
    manager = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    assert 'state.contains("loading")' in manager
    assert 'state.contains("preflight")' in manager
    assert '.put("retryable", transientBuildGate(gate))' in manager


def test_11_delayed_toolchain_smoke_waits_with_lease_keeper() -> None:
    manager = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    assert "TimeUnit.MINUTES.toMillis(8)" in manager
    assert '"toolchainSmoke".equals(item)' in manager
    assert service.index("startJobLeaseKeeper") < service.index("CoreWorkerApkBuildManager.execute(")


def test_preflight_freshness_starts_after_smoke_finishes() -> None:
    manager = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    finalize_at = manager.index("value = finalizeToolchainPreflight(context, value);")
    completed_at = manager.index("long completedAt = System.currentTimeMillis();", finalize_at)
    checked_at = manager.index('value.put("checkedAt", completedAt);', completed_at)
    assert finalize_at < completed_at < checked_at


class _Response:
    def __init__(self, body: bytes, url: str, content_length: int):
        self._stream = io.BytesIO(body)
        self._url = url
        self.headers = {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url


def test_12_truncated_source_zip_is_never_promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder()
    url = "https://vps.example/core-worker/app/source.zip"
    body = b"truncated"
    calls = 0

    def open_truncated(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(body, url, len(body) + 50)

    monkeypatch.setattr(module.urllib.request, "urlopen", open_truncated)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    target = tmp_path / "source.zip"
    with pytest.raises(module.SourceDownloadTransientError, match="retry_exhausted"):
        module._download_source(url, target, "", len(body) + 50, "https://vps.example")
    assert calls == module.SOURCE_DOWNLOAD_ATTEMPTS
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_13_hash_mismatch_removes_partial_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder()
    url = "https://vps.example/core-worker/app/source.zip"
    body = b"complete zip bytes"
    calls = 0

    def open_wrong_hash(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(body, url, len(body))

    monkeypatch.setattr(module.urllib.request, "urlopen", open_wrong_hash)
    target = tmp_path / "source.zip"
    with pytest.raises(module.SourceHashMismatchError, match="source_hash_mismatch"):
        module._download_source(url, target, "0" * 64, len(body), "https://vps.example")
    assert calls == 1
    assert not target.exists() and not list(tmp_path.glob("*.part"))


def test_14_source_retry_is_explicitly_limited() -> None:
    module = _load_self_builder()
    assert module.SOURCE_DOWNLOAD_ATTEMPTS == 3
    source = SELF_BUILDER.read_text(encoding="utf-8")
    assert "for attempt in range(1, SOURCE_DOWNLOAD_ATTEMPTS + 1)" in source


def test_15_deterministic_build_failure_is_classified_failed() -> None:
    module = _load_self_builder()
    assert module._classify_failure("cannot find symbol in MainActivity") == "deterministic"
    assert module._classify_failure("source_hash_mismatch") == "deterministic"


def test_source_job_requires_full_hash_and_fingerprint_contract() -> None:
    module = _load_self_builder()
    valid = {
        "source_sha256": "a" * 64,
        "source_bytes": 1234,
        "sourceFingerprint": "b" * 64,
    }
    assert module._validated_source_identifiers(valid) == ("a" * 64, 1234, "b" * 64)
    with pytest.raises(ValueError, match="source_sha256"):
        module._validated_source_identifiers({"sourceFingerprint": "b" * 64})
    with pytest.raises(ValueError, match="source_fingerprint"):
        module._validated_source_identifiers({"source_sha256": "a" * 64})
    assert module._classify_failure("source_sha256 ausente ou inválido") == "deterministic"


def test_16_transient_build_failure_can_retry() -> None:
    module = _load_self_builder()
    assert module._classify_failure("network_truncation") == "transient"
    assert module._classify_failure("preflight_blocked: builder_busy") == "transient"


def test_17_lease_token_and_progress_are_renewed(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=True)
    job_id = _create_build(registry)
    delivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    assert delivered["lease_token"]
    before = float(delivered["lease_until"])
    renewed = registry.renew_job_lease({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": delivered["lease_token"], "stage": "building", "progress": 42,
    }, token=apk_token)
    assert renewed["accepted"] is True
    assert renewed["job"]["lease_until"] >= before
    assert renewed["job"]["progress_stage"] == "building"
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    assert "PROGRESS_OWNERSHIP_LOST" in service
    assert "CoreWorkerApkBuildManager.requestCancellation" in service


def test_expired_lease_cannot_be_renewed_or_finish_successfully(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=True)
    job_id = _create_build(registry, max_attempts=2)
    first = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["lease_until"] = time.time() - 1
    registry.path.write_text(json.dumps(data), encoding="utf-8")

    late_renewal = registry.renew_job_lease({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": first["lease_token"], "stage": "gradle_running",
    }, token=apk_token)
    assert late_renewal["accepted"] is False
    assert late_renewal["ownership_lost"] is True
    assert registry.get_job(job_id)["job"]["status"] == "queued"

    second = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["lease_until"] = time.time() - 1
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    late_result = registry.submit_job_result({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": second["lease_token"], "status": "succeeded",
        "result": {"ok": True},
    }, token=apk_token)
    assert late_result["accepted"] is False
    assert late_result["ownership_lost"] is True
    assert registry.get_job(job_id)["job"]["status"] == "failed"


def test_apk_cancels_executor_before_local_lease_deadline() -> None:
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    assert "LOCAL_LEASE_SAFETY_MS" in service
    assert "activeLeaseDeadlineMillis" in service
    assert "rememberRenewedLease" in service
    lease_keeper = service[service.index("private Thread startJobLeaseKeeper"):service.index("private int postJobProgress")]
    assert "remaining <= LOCAL_LEASE_SAFETY_MS" in lease_keeper
    assert "CoreWorkerApkBuildManager.requestCancellation" in lease_keeper


def test_18_stale_lease_does_not_remain_running_for_hours(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path)
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["lease_until"] = time.time() - 1
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    assert registry.snapshot()["jobs"][0]["status"] == "queued"


def test_19_parent_and_child_count_as_one_phone(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    snapshot = registry.snapshot()
    assert snapshot["summary"]["registered"] == 1
    assert snapshot["summary"]["runtime_registered"] == 2


def test_20_battery_uses_freshest_runtime_measurement() -> None:
    funcs = _telemetry_functions()
    now = time.time()
    members = [
        {"worker_id": "parent", "runtime_kind": "termux", "battery": {"level": 96, "_level_observed_at": now - 2}},
        {"worker_id": "child", "runtime_kind": "apk", "battery": {"level": 46, "_level_observed_at": now - 100}},
    ]
    result = funcs["_physical_telemetry_snapshot"](members, now=now)
    assert result["battery"]["value"] == 96
    assert result["battery"]["source_worker_id"] == "parent"


def test_21_temperature_uses_freshest_independent_source() -> None:
    funcs = _telemetry_functions()
    now = time.time()
    members = [
        {"worker_id": "parent", "battery": {"temperature_c": 37, "_temperature_observed_at": now - 30}},
        {"worker_id": "child", "battery": {"temperature_c": 39, "_temperature_observed_at": now - 3}},
    ]
    result = funcs["_physical_telemetry_snapshot"](members, now=now)
    assert result["temperature"]["value"] == 39
    assert result["temperature"]["source_worker_id"] == "child"


def test_network_and_ping_choose_their_own_freshest_sources() -> None:
    funcs = _telemetry_functions()
    now = time.time()
    members = [
        {"worker_id": "parent", "runtime_kind": "termux", "network": {
            "type": "wifi", "vps_ping_ms": 18,
            "_network_observed_at": now - 40, "_ping_observed_at": now - 2,
        }},
        {"worker_id": "child", "runtime_kind": "apk", "network": {
            "type": "celular", "vps_ping_ms": 70,
            "_network_observed_at": now - 3, "_ping_observed_at": now - 60,
        }},
    ]
    result = funcs["_physical_telemetry_snapshot"](members, now=now)
    assert result["network"]["value"] == "celular"
    assert result["network"]["source_worker_id"] == "child"
    assert result["ping"]["value"] == 18
    assert result["ping"]["source_worker_id"] == "parent"


def test_22_stale_telemetry_is_rendered_unavailable() -> None:
    funcs = _telemetry_functions()
    now = time.time()
    members = [{
        "worker_id": "child",
        "battery": {"level": 46, "temperature_c": 38, "_level_observed_at": now - 600, "_temperature_observed_at": now - 600},
        "network": {"type": "wifi", "ping_ms": 24, "_network_observed_at": now - 600, "_ping_observed_at": now - 600},
    }]
    text = funcs["_physical_telemetry_text"](members, now=now)
    assert "46%" not in text and "38°C" not in text
    assert text.count("indisponível") == 4


def test_partial_telemetry_heartbeat_preserves_independent_fresh_metrics(tmp_path: Path) -> None:
    registry, termux_token, _ = _registry(tmp_path)
    first_at = time.time() - 20
    registry.heartbeat({
        "worker_id": "phone-test",
        "runtime_kind": "termux",
        "battery": {"level": 96, "temperature_c": 36, "measured_at": first_at},
        "network": {"type": "wifi", "vps_ping_ms": 24, "measured_at": first_at},
    }, token=termux_token)
    second_at = time.time()
    registry.heartbeat({
        "worker_id": "phone-test",
        "runtime_kind": "termux",
        "battery": {"temperature_c": 37, "measured_at": second_at},
        "network": {"vps_ping_ms": 18, "measured_at": second_at},
    }, token=termux_token)

    worker = next(item for item in registry.snapshot()["workers"] if item["worker_id"] == "phone-test")
    assert worker["battery"]["level"] == 96
    assert worker["battery"]["temperature_c"] == 37
    assert worker["battery"]["_level_observed_at"] == pytest.approx(first_at, abs=1)
    assert worker["battery"]["_temperature_observed_at"] == pytest.approx(second_at, abs=1)
    assert worker["network"]["type"] == "wifi"
    assert worker["network"]["vps_ping_ms"] == 18
    assert worker["network"]["_network_observed_at"] == pytest.approx(first_at, abs=1)
    assert worker["network"]["_ping_observed_at"] == pytest.approx(second_at, abs=1)


def test_23_phone_worker_version_is_not_used_as_apk_target_in_card() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    panel = source[source.index("def _selected_worker_lines"):source.index("async def _select_worker", source.index("def _selected_worker_lines"))]
    assert 'apk.get("version")' in panel
    assert "_agent_version_label(parent.get(\"version\"))" in panel
    assert "target_version" not in panel and "sourceFingerprint" not in panel


def test_24_internal_worker_update_details_are_absent_from_physical_card() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    panel = source[source.index("def _selected_worker_lines"):source.index("async def _select_worker", source.index("def _selected_worker_lines"))]
    assert "worker_update" not in panel
    assert "job_id" not in panel
    assert "routing_reason" not in panel


def test_25_compiling_label_requires_running_job() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    method = source[source.index("def _physical_build_line"):source.index("def _selected_worker_lines")]
    assert 'if status == "running" and owner_is_apk' in method
    assert '"gradle"' in method and 'activity = "compilando"' in method
    assert 'activity = "preparando ambiente"' in method


def test_26_queue_label_requires_queued_or_running_filter() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    method = source[source.index("def _physical_build_line"):source.index("def _selected_worker_lines")]
    assert 'not in {"queued", "running"}' in method
    assert 'return f"**Build:** APK `{version}` · na fila"' in method


def test_27_core_ui_keeps_rootfs_and_runner_out_of_daily_card() -> None:
    source = (JAVA / "MainActivity.java").read_text(encoding="utf-8")
    assert 'prepareCard.addView(rootfsHeroText)' not in source
    assert 'prepareCard.addView(runnerStatusText)' not in source
    assert 'technicalCard.addView(sectionTitle("Avançado"))' in source


def test_28_normal_restart_reuses_fresh_persisted_autobuild_readiness() -> None:
    source = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    assert "readPersistedPreflight(context, now)" in source
    assert "PERSISTED_READY_MAX_MS" in source
    assert 'prefs.getInt("apk_self_builder_checked_version_code", 0) != BuildConfig.VERSION_CODE' in source
    assert "refreshAsync(context)" in source


def test_physical_card_does_not_call_stale_builder_ready() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    panel = source[source.index("def _selected_worker_lines"):source.index("async def _select_worker", source.index("def _selected_worker_lines"))]
    assert "readiness_fresh" in panel
    assert "CORE_WORKER_APK_BUILDER_READINESS_MAX_AGE_SECONDS" in panel


def test_29_no_toolchain_binary_is_embedded_in_apk_assets() -> None:
    assets = ANDROID / "app/src/main/assets"
    forbidden = []
    for path in assets.rglob("*") if assets.is_dir() else []:
        name = path.name.lower()
        if path.is_file() and (name == "android-builder-toolchain.zip" or name.endswith(".cwpart")):
            forbidden.append(path)
    assert forbidden == []


def test_30_cleanup_preserves_latest_current_and_toolchain(tmp_path: Path) -> None:
    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    artifacts = builder / "artifacts"
    toolchain = builder / "toolchain"
    artifacts.mkdir(parents=True)
    toolchain.mkdir(parents=True)
    (toolchain / "manifest.json").write_text("{}", encoding="utf-8")
    current = artifacts / "CoreWorker-current.apk"
    current.write_bytes(b"current")
    for index in range(5):
        old = artifacts / f"CoreWorker-old-{index}.apk"
        old.write_bytes(bytes([index]) * (index + 1))
        os.utime(old, (100 + index, 100 + index))
    os.utime(current, (1, 1))
    module._atomic_json(artifacts / "latest-artifact.json", {"artifact_path": str(current)})
    module._cleanup_private_builder_storage(builder)
    assert current.is_file()
    assert (artifacts / "latest-artifact.json").is_file()
    assert (toolchain / "manifest.json").is_file()


def test_worker_update_is_deduplicated_and_superseded_atomically(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    first = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.6", "source_hash": "d" * 64},
        target_worker_id="phone-test", required_capabilities=["phone-worker"],
    )
    duplicate = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.6", "source_hash": "d" * 64},
        target_worker_id="phone-test", required_capabilities=["phone-worker"],
    )
    assert duplicate["deduplicated"] is True
    assert duplicate["job"]["job_id"] == first["job"]["job_id"]
    newer = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.7", "source_hash": "e" * 64},
        target_worker_id="phone-test", required_capabilities=["phone-worker"],
    )
    assert registry.get_job(first["job"]["job_id"])["job"]["status"] == "superseded"
    assert newer["job"]["status"] == "queued"


def test_untargeted_worker_update_uses_preferred_runtime_for_dedup(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)
    first = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.6", "source_hash": "d" * 64},
        required_capabilities=["phone-worker"],
    )
    assert first["job"]["target_worker_id"] == ""
    assert first["job"]["preferred_worker_id"] == "phone-test"
    duplicate = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.6", "source_hash": "d" * 64},
        required_capabilities=["phone-worker"],
    )
    assert duplicate["deduplicated"] is True
    assert duplicate["job"]["job_id"] == first["job"]["job_id"]


def test_untargeted_worker_update_is_closed_by_matching_heartbeat(tmp_path: Path) -> None:
    registry, termux_token, _ = _registry(tmp_path)
    created = registry.create_job(
        job_type="worker_update", payload={"version": "1.11.6", "source_hash": "d" * 64},
        required_capabilities=["phone-worker"],
    )
    registry.heartbeat({
        "worker_id": "phone-test",
        "runtime_kind": "termux",
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "version": "1.11.6",
        "source_hash": "d" * 64,
    }, token=termux_token)
    job = registry.get_job(created["job"]["job_id"])["job"]
    assert job["status"] == "succeeded"
    assert job["result"]["confirmed_by_heartbeat"] is True


def test_builder_change_supersedes_queued_job_but_never_running_job(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path)
    queued_id = _create_build(registry)
    changed = registry.supersede_queued_apk_jobs_for_builder("c" * 64, "phone-test")
    assert changed["superseded"] == 1
    assert registry.get_job(queued_id)["job"]["status"] == "superseded"

    running_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)
    changed = registry.supersede_queued_apk_jobs_for_builder("c" * 64, "phone-test")
    assert changed["superseded"] == 0
    assert changed["running_other_builder"] == 1
    assert registry.get_job(running_id)["job"]["status"] == "running"


def test_builder_supersede_rejects_missing_or_invalid_target(tmp_path: Path) -> None:
    registry, _, _ = _registry(tmp_path)

    with pytest.raises(CoreWorkerRegistryError):
        registry.supersede_queued_apk_jobs_for_builder("c" * 64, "")
    with pytest.raises(CoreWorkerRegistryError):
        registry.supersede_queued_apk_jobs_for_builder("c" * 64, "../apk")


def test_same_source_preselection_preserves_desired_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _automation()
    desired = tmp_path / "desired-source.json"
    desired.write_text(json.dumps({
        "schema": "core-worker-apk-desired-source-v1",
        "versionName": "0.8.6",
        "versionCode": 133,
        "sourceFingerprint": "c" * 64,
        "sourceSha256": "c" * 64,
        "publicationPolicy": "selected-builder-v1",
        "selectedBuilderWorkerId": "phone-test-apk",
        "selectedBuilderRuntimeKind": "apk",
        "requiredAgentSourceHash": "",
        "toolchainFingerprint": "b" * 64,
    }), encoding="utf-8")

    builder_supersede_calls: list[tuple[Any, ...]] = []

    class Registry:
        def supersede_queued_apk_jobs_for_builder(self, *_args, **_kwargs):
            builder_supersede_calls.append(_args)
            return {"ok": True, "superseded": 0, "running_other_builder": 0}

    monkeypatch.setattr(module, "_desired_apk_source_path", lambda: desired)
    monkeypatch.setattr(module, "_desired_apk_source_lock", contextlib.nullcontext)
    monkeypatch.setattr(module, "get_core_workers_registry", lambda: Registry())
    result = module._publish_desired_apk_source(
        version_name="0.8.6", version_code=133,
        source_fingerprint="c" * 64, source_sha256="c" * 64,
    )
    assert result["record"]["selectedBuilderWorkerId"] == "phone-test-apk"
    assert result["record"]["toolchainFingerprint"] == "b" * 64
    assert builder_supersede_calls == []


def test_running_apk_blocks_termux_build_on_same_physical_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _automation()
    now = time.time()
    raw = {
        "workers": {
            "phone-test": {"worker_id": "phone-test", "physical_worker_id": "phone-test"},
            "phone-test-apk": {
                "worker_id": "phone-test-apk",
                "parent_worker_id": "phone-test",
                "physical_worker_id": "phone-test",
            },
        },
        "jobs": {"job-running": {
            "job_id": "job-running",
            "type": "apk_build_debug",
            "status": "running",
            "worker_id": "phone-test-apk",
            "lease_until": now + 120,
            "expires_at": now + 600,
        }},
    }

    class Registry:
        def snapshot(self, **_kwargs):
            return {"ok": True}

    monkeypatch.setattr(module, "get_core_workers_registry", lambda: Registry())
    monkeypatch.setattr(module, "_registry_raw", lambda: raw)
    assert module._active_apk_build_exists_for_physical("phone-test") is True
    assert module._active_apk_build_exists_for_physical("other-phone") is False


def test_stale_result_cannot_finish_a_new_lease_and_terminal_retry_is_idempotent(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=True)
    job_id = _create_build(registry)
    first = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    registry.renew_job_lease({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": first["lease_token"], "action": "abandon",
    }, token=apk_token)
    second = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    stale = registry.submit_job_result({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": first["lease_token"], "status": "succeeded", "result": {"ok": True},
    }, token=apk_token)
    assert stale["accepted"] is False and stale["ownership_lost"] is True
    assert registry.get_job(job_id)["job"]["status"] == "running"
    accepted = registry.submit_job_result({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": second["lease_token"], "status": "succeeded", "result": {"ok": True},
    }, token=apk_token)
    assert accepted["accepted"] is True
    repeated = registry.submit_job_result({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": second["lease_token"], "status": "succeeded", "result": {"ok": True},
    }, token=apk_token)
    assert repeated["idempotent"] is True


def test_legacy_claim_survives_runtime_upgrade_to_lease_token_capability(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=False)
    job_id = _create_build(registry)
    delivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    assert delivered["lease_token"]
    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    assert raw["jobs"][job_id]["lease_token_required"] is False

    # O update do app altera a capability, mas não o contrato do claim antigo.
    worker = raw["workers"]["phone-test-apk"]
    worker["capabilities"].append("apk-job-lease-token-v1")
    registry.path.write_text(json.dumps(raw), encoding="utf-8")
    renewed = registry.renew_job_lease({
        "worker_id": "phone-test-apk", "job_id": job_id, "stage": "result_pending",
    }, token=apk_token)
    assert renewed["accepted"] is True
    result = registry.submit_job_result({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "status": "succeeded", "result": {"ok": True},
    }, token=apk_token)
    assert result["accepted"] is True


def test_source_archive_is_content_addressed_and_integral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _automation()
    monkeypatch.setattr(module, "_core_worker_release_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://vps.example")
    monkeypatch.setattr(module, "_registry_raw", lambda: {"jobs": {}})
    source = module._prepare_apk_source_zip()
    path = Path(source["path"])
    assert source["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert source["source_fingerprint"] == source["sha256"]
    assert source["sha256"] in path.name and path.name != "source-core-worker-app.zip"
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
    assert not any(name.endswith("google-services.json") for name in names)
    assert not any(name.endswith((".jks", ".keystore", ".p12", ".pem", ".key")) for name in names)


def test_discord_manual_build_uses_atomic_content_addressed_source_and_short_lease() -> None:
    source = WORKERS.read_text(encoding="utf-8")
    start = source.index("def _prepare_core_worker_source_zip_sync")
    end = source.index("async def _build_apk_builder_payload", start)
    packager = source[start:end]
    assert 'source-core-worker-app-{source_sha256}.zip' in packager
    assert "os.replace(temp, zip_path)" in packager
    assert "os.fsync(source_file.fileno())" in packager
    assert 'release_dir / "source-core-worker-app.zip"' not in packager
    payload = source[source.index("async def _build_apk_builder_payload"):source.index("async def _queue_core_worker_job")]
    assert '"sourceFingerprint": source["source_fingerprint"]' in payload
    enqueue = source[source.index("async def _queue_core_worker_job"):source.index("async def _get_core_worker_job")]
    assert "lease = 240 if is_apk_build" in enqueue


def test_apk_release_is_086_and_phone_worker_stays_1115() -> None:
    gradle = (ANDROID / "app/build.gradle").read_text(encoding="utf-8")
    phone = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    assert 'versionName "0.8.6"' in gradle and "versionCode 133" in gradle
    assert 'PHONE_WORKER_VERSION = "1.11.5"' in phone


def test_revoked_claim_is_stopped_before_executor_entrypoint() -> None:
    source = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    claim = source.index("int claimAcknowledged = postJobProgress")
    revoked = source.index("claimAcknowledged == PROGRESS_OWNERSHIP_LOST", claim)
    execute = source.index("CoreWorkerApkBuildManager.execute(", claim)
    assert claim < revoked < execute
    assert "clearActiveJob(jobId)" in source[revoked:execute]


def test_result_outbox_wins_over_stale_active_stage_on_restart() -> None:
    source = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    recovery = source[source.index("private boolean recoverInterruptedActiveJob"):source.index("private Thread startJobLeaseKeeper")]
    outbox = recovery.index("outbox != null && outbox.isFile()")
    abandon = recovery.index('"abandon"')
    assert outbox < abandon
    assert "resultado local preservado" in recovery[outbox:abandon]
    assert 'postJobProgress(serverUrl, token, jobId, "result_pending"' in recovery[outbox:abandon]


def test_lease_loss_cancellation_covers_preflight_gradle_and_publish() -> None:
    manager = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    builder = SELF_BUILDER.read_text(encoding="utf-8")
    assert '"apk-self-builder/cancellations"' in manager
    assert 'effectivePayload.put("registryAttempt"' in manager
    assert 'effectivePayload.put("registryCancellationPath"' in manager
    assert "registry_cancellation" in builder
    assert "_raise_if_publish_cancelled(cancellation_marker, connection)" in builder
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    assert "requestActiveBuildCancellation();" in service


def test_build_stages_are_written_for_lease_progress() -> None:
    builder = SELF_BUILDER.read_text(encoding="utf-8")
    for stage in ("source_downloading", "source_preparing", "gradle_running", "publishing"):
        assert f'"{stage}"' in builder
    assert "_update_active_job_stage" in builder


def test_result_is_fsynced_before_build_resources_are_released(tmp_path: Path) -> None:
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    durable_at = service.index("File stored = persistOutbox(jobId, envelope);")
    release_at = service.index("boolean resourcesReleased = finalizeEnvelopeBuildResources(envelope);", durable_at)
    assert durable_at < release_at

    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    work = builder / "work/job-result"
    toolchain = builder / "toolchain/current"
    work.mkdir(parents=True)
    toolchain.mkdir(parents=True)
    (work / "private.properties").write_text("secret", encoding="utf-8")
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "pid": os.getpid(),
        "processStartTicks": module._proc_start_ticks(os.getpid()),
        "jobId": "job-result",
        "attempt": 1,
        "work": str(work),
        "project": str(work / "source/android/core-worker-app"),
    })
    result = json.loads(module.finalize_build_attempt(str(tmp_path), "job-result", 1))
    assert result["released"] is True
    assert not lock.exists() and not work.exists()
    assert toolchain.is_dir()


def test_restart_handoff_does_not_wait_forever_on_live_process_lock(tmp_path: Path) -> None:
    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    work = builder / "work/job-handoff"
    work.mkdir(parents=True)
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "pid": os.getpid(),
        "processStartTicks": module._proc_start_ticks(os.getpid()),
        "pythonThreadNativeId": 999_999_999,
        "pythonFinishedAt": time.time(),
        "jobId": "job-handoff",
        "attempt": 1,
        "work": str(work),
        "project": str(work / "source/android/core-worker-app"),
    })
    result = json.loads(module.reconcile_interrupted_build(str(tmp_path), "job-handoff", 1))
    assert result["safeToRequeue"] is True
    assert result["state"] == "executor_stopped"
    assert not lock.exists() and not work.exists()


def test_reused_owner_pid_does_not_invent_a_live_gradle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder()
    lock = tmp_path / "apk-self-builder/.apk-build-active"
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "pid": os.getpid(),
        "processStartTicks": 1,
        "jobId": "job-pid-reused",
        "attempt": 1,
    })
    monkeypatch.setattr(module, "_proc_start_ticks", lambda _pid: 2)
    _owner, gradle_pid, safe = module._safe_gradle_owner(lock, "job-pid-reused", 1)
    assert gradle_pid == 0
    assert safe is True


def test_restart_rechecks_durable_outbox_before_abandoning() -> None:
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    reconcile_at = service.index("boolean executorStopped =")
    recheck_at = service.index("outbox = outboxFile(jobId);", reconcile_at)
    abandon_at = service.index('"executor_restarted"', recheck_at)
    assert reconcile_at < recheck_at < abandon_at
    tail = service[abandon_at:]
    assert "return true;" in tail[:1200]


def test_builder_busy_result_does_not_release_or_wait_on_foreign_lock(tmp_path: Path) -> None:
    module = _load_self_builder()
    lock = tmp_path / "apk-self-builder/.apk-build-active"
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "jobId": "job-owner", "attempt": 1, "pid": os.getpid(),
        "processStartTicks": module._proc_start_ticks(os.getpid()),
    })
    result = json.loads(module.finalize_build_attempt(str(tmp_path), "job-busy", 1))
    assert result["released"] is True
    assert result["state"] == "foreign_build_lock_preserved"
    assert lock.is_dir()


def test_attempt_scoped_cancellation_rejects_only_matching_attempt(tmp_path: Path) -> None:
    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    marker = builder / "cancellations/job-attempt-1.request"
    payload = {"registryCancellationPath": str(marker)}
    assert module._payload_cancellation_marker(builder, payload) == marker
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(module.SourceDownloadTransientError, match="ownership_lost"):
        module._raise_if_build_cancelled(builder / ".apk-build-active", marker)
    next_marker = builder / "cancellations/job-attempt-2.request"
    module._raise_if_build_cancelled(builder / ".apk-build-active", next_marker)


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="validação de PID exige /proc")
def test_owned_process_group_is_fully_stopped() -> None:
    module = _load_self_builder()
    process = subprocess.Popen(
        ["/bin/bash", "-c", "/bin/sleep 30 & wait"],
        start_new_session=True,
    )
    try:
        module._stop_owned_process(process, grace_seconds=1.0)
        assert process.poll() is not None
        assert module._process_group_alive(process.pid) is False
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="validação de PID exige /proc")
def test_exact_gradle_process_group_is_stopped_before_requeue(tmp_path: Path) -> None:
    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    work = builder / "work/job-attempt-1"
    project = work / "source/android/core-worker-app"
    project.mkdir(parents=True)
    lock.mkdir(parents=True)
    process = subprocess.Popen(
        ["/bin/bash", "-c", "exec -a gradle /bin/sleep 30"],
        cwd=project,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2.0
        cmdline = ""
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "gradle" in cmdline:
                break
            time.sleep(0.02)
        if "gradle" not in cmdline:
            pytest.skip("namespace de PID não expõe o subprocesso em /proc")
        module._atomic_json(lock / "owner.json", {
            "pid": 999999999,
            "processStartTicks": 1,
            "jobId": "job-exact",
            "attempt": 1,
            "gradlePid": process.pid,
            "gradlePgid": process.pid,
            "gradleStartTicks": module._proc_start_ticks(process.pid),
            "project": str(project),
            "work": str(work),
        })
        # Em produção o processo órfão é colhido pelo init do Android. Neste
        # teste ele ainda é nosso filho, então uma thread faz o reap equivalente.
        threading.Thread(target=process.wait, daemon=True).start()
        result = json.loads(module.reconcile_interrupted_build(str(tmp_path), "job-exact", 1))
        assert result["safeToRequeue"] is True, result
        assert result["identityValidated"] is True
        assert process.poll() is not None
        assert module._process_group_alive(process.pid) is False
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="validação de PID exige /proc")
def test_live_orphaned_gradle_group_keeps_build_lock_busy(tmp_path: Path) -> None:
    module = _load_self_builder()
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    lock.mkdir(parents=True)
    process = subprocess.Popen(["/bin/bash", "-c", "sleep 30"], start_new_session=True)
    try:
        module._atomic_json(lock / "owner.json", {
            "pid": 999999999,
            "processStartTicks": 1,
            "jobId": "job-old",
            "attempt": 1,
            "startedAt": time.time() - 600,
            "gradlePid": process.pid,
            "gradlePgid": process.pid,
            "gradleStartTicks": module._proc_start_ticks(process.pid),
        })
        acquired, returned_lock, owner = module._acquire_build_lock(builder, "job-new", 2)
        assert acquired is False
        assert returned_lock == lock
        assert owner["executorLive"] is True
        assert lock.is_dir()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def test_orphaned_process_group_requires_live_member_identity() -> None:
    module = _load_self_builder()
    assert module._validated_orphaned_gradle_group(999999999, Path("/tmp/project"), Path("/tmp/work")) is False


def test_stale_heartbeat_result_cannot_close_requeued_attempt(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=True)
    job_id = _create_build(registry)
    first = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    registry.renew_job_lease({
        "worker_id": "phone-test-apk", "job_id": job_id,
        "lease_token": first["lease_token"], "action": "abandon",
    }, token=apk_token)
    second = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    claim_at = float(data["jobs"][job_id]["lease_token_issued_at"])
    queue = data["workers"]["phone-test-apk"]["status"].setdefault("core_worker_jobs", {})
    queue.update({
        "last_result_job_id": job_id,
        "last_result_status": "failed",
        "last_result_at": claim_at - 30,
    })
    registry.path.write_text(json.dumps(data), encoding="utf-8")
    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "status": {"core_worker_jobs": {
            "active_job_id": job_id,
            "active_job_lease_token_hash": _hash_secret(second["lease_token"]),
            "pending_result_count": 0,
        }},
    }, token=apk_token)
    assert registry.get_job(job_id)["job"]["status"] == "running"


def test_fresh_heartbeat_result_can_recover_lost_result_post(tmp_path: Path) -> None:
    registry, _, apk_token = _registry(tmp_path, lease_tokens=True)
    job_id = _create_build(registry)
    delivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=apk_token)["job"]
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    claim_at = float(data["jobs"][job_id]["lease_token_issued_at"])
    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "status": {"core_worker_jobs": {
            "active_job_id": job_id,
            "active_job_lease_token_hash": _hash_secret(delivered["lease_token"]),
            "last_result_job_id": job_id,
            "last_result_status": "succeeded",
            "last_result_at": claim_at + 1,
            "last_result_summary": "resultado recuperado",
            "pending_result_count": 1,
        }},
    }, token=apk_token)
    job = registry.get_job(job_id)["job"]
    assert job["status"] == "succeeded"
    assert job["result"]["recovered_from_worker_status"] is True


def test_registry_transactions_are_locked_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    first = CoreWorkersRegistry(path)
    second = CoreWorkersRegistry(path)
    assert first._lock.acquire() is True
    try:
        snapshot = second.snapshot(lock_timeout_seconds=0.02)
        assert snapshot["stale"] is True
        assert snapshot["error"] == "registry_lock_timeout"
    finally:
        first._lock.release()
