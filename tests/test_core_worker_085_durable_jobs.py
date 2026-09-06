from __future__ import annotations

import json
import time
from pathlib import Path

from utility.commands.workers_registry import CoreWorkersRegistry, _hash_secret

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"


def _apk_record(worker_id: str, token: str) -> dict:
    now = time.time()
    return {
        "worker_id": worker_id,
        "name": "APK filho",
        "enabled": True,
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "token_hash": _hash_secret(token),
        "source": "core-worker-apk-foreground-service",
        "platform": "android",
        "runtime_kind": "apk",
        "parent_worker_id": "phone-test",
        "physical_worker_id": "phone-test",
        "roles": ["apk-worker", "apk-builder"],
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder", "apk-durable-jobs-v1"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {
            "apk_self_builder": {"ready": True, "ok": True, "publishReady": False, "checkedAt": int(now * 1000)},
            "core_worker_jobs": {"active_job_id": "", "pending_result_count": 0},
        },
    }


def _registry(tmp_path: Path) -> tuple[CoreWorkersRegistry, str]:
    token = "apk-secret"
    data = {
        "version": 1,
        "pairings": {},
        "workers": {"phone-test-apk": _apk_record("phone-test-apk", token)},
        "jobs": {},
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return CoreWorkersRegistry(path), token


def _create_build(registry: CoreWorkersRegistry) -> str:
    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"versionName": "0.8.6", "versionCode": 133},
        target_worker_id="phone-test-apk",
        required_capabilities=["apk-builder"],
        ttl_seconds=7200,
        lease_seconds=240,
        max_attempts=2,
        summary="build automático APK 0.8.6",
    )
    return created["job"]["job_id"]


def test_release_is_086_without_unnecessary_phone_worker_bump() -> None:
    gradle = (ANDROID / "app/build.gradle").read_text(encoding="utf-8")
    phone = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    assert 'versionCode 133' in gradle
    assert 'versionName "0.8.6"' in gradle
    assert 'PHONE_WORKER_VERSION = "1.11.5"' in phone
    automation = (ROOT / "scripts/core-worker-automation.py").read_text(encoding="utf-8")
    assert "lease_seconds=240,\n            max_attempts=2" in automation


def test_apk_persists_active_job_and_renews_progress() -> None:
    service = (JAVA / "CoreWorkerRuntimeService.java").read_text(encoding="utf-8")
    assert 'core-worker-apk-active-job-v2' in service
    assert 'active-job.json' in service
    assert 'persistActiveJob(job, "claimed"' in service
    assert 'serverUrl + "/core-worker/jobs/progress"' in service
    assert 'JOB_PROGRESS_INTERVAL_MS' in service
    assert 'recoverInterruptedActiveJob' in service
    assert '"abandon"' in service
    catalog = (JAVA / "CoreWorkerJobCatalog.java").read_text(encoding="utf-8")
    assert '"apk-durable-jobs-v1"' in catalog
    assert '"apk-job-lease-token-v1"' in catalog
    assert 'status.put("core_worker_jobs", localCoreWorkerJobsSnapshot())' in service


def test_builder_waits_for_transient_toolchain_smoke_instead_of_failing_immediately() -> None:
    manager = (JAVA / "CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    assert 'awaitBuildPreflight(context)' in manager
    assert 'transientBuildGate' in manager
    assert 'BUILD_PREFLIGHT_WAIT_MS' in manager
    assert '"toolchainSmoke".equals(item)' in manager
    assert 'apk-builder-installed' in manager


def test_registry_does_not_deliver_second_job_while_worker_has_running_lease(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    first = _create_build(registry)
    second = _create_build(registry)

    delivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=token)
    assert delivered["job"]["job_id"] == first

    busy = registry.poll_job({"worker_id": "phone-test-apk"}, token=token)
    assert busy["job"] is None
    assert busy["busy_job_id"] == first

    snapshot = registry.get_job(second)["job"]
    assert snapshot["status"] == "queued"


def test_apk_can_explicitly_abandon_interrupted_job_for_safe_requeue(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    job_id = _create_build(registry)
    delivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=token)
    assert delivered["job"]["job_id"] == job_id

    reconciled = registry.renew_job_lease({
        "worker_id": "phone-test-apk",
        "job_id": job_id,
        "action": "abandon",
        "stage": "executor_restarted",
        "summary": "APK reiniciou durante o build",
    }, token=token)
    assert reconciled["ok"] is True
    assert reconciled["requeued"] is True
    assert reconciled["job"]["status"] == "queued"

    redelivered = registry.poll_job({"worker_id": "phone-test-apk"}, token=token)
    assert redelivered["job"]["job_id"] == job_id
    assert redelivered["job"]["attempts"] == 2


def test_registry_requeues_running_job_when_live_apk_no_longer_recognizes_it(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=token)

    # Simula o caso real: VPS mantém running, APK está vivo mas reporta fila local vazia.
    data = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    now = time.time()
    job = data["jobs"][job_id]
    job["updated_at"] = now - 300
    job["executor_missing_since"] = now - 90
    data["jobs"][job_id] = job
    (tmp_path / "registry.json").write_text(json.dumps(data), encoding="utf-8")

    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder", "apk-durable-jobs-v1"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {
            "apk_self_builder": {"ready": True, "ok": True},
            "core_worker_jobs": {
                "active_job_id": "",
                "active_job_stage": "",
                "pending_result_count": 0,
            },
        },
    }, token=token)

    recovered = registry.get_job(job_id)["job"]
    assert recovered["status"] == "queued"
    assert recovered["worker_id"] == ""
    assert recovered.get("executor_recoveries") == 1
    assert recovered.get("progress_stage") == "requeued_after_executor_loss"


def test_active_job_heartbeat_prevents_false_executor_loss(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=token)

    data = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    data["jobs"][job_id]["updated_at"] = time.time() - 300
    data["jobs"][job_id]["executor_missing_since"] = time.time() - 90
    (tmp_path / "registry.json").write_text(json.dumps(data), encoding="utf-8")

    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder", "apk-durable-jobs-v1"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {
            "apk_self_builder": {"ready": True, "ok": True},
            "core_worker_jobs": {
                "active_job_id": job_id,
                "active_job_stage": "building",
                "pending_result_count": 0,
            },
        },
    }, token=token)

    active = registry.get_job(job_id)["job"]
    assert active["status"] == "running"
    assert active.get("progress_stage") == "building"
    assert not active.get("executor_missing_since")


def test_legacy_apk_is_not_declared_lost_only_because_active_job_field_is_absent(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    data_path = tmp_path / "registry.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["workers"]["phone-test-apk"]["capabilities"] = ["apk-native", "apk-builder", "apk-self-builder"]
    data_path.write_text(json.dumps(data), encoding="utf-8")
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=token)

    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["updated_at"] = time.time() - 600
    data["jobs"][job_id]["executor_missing_since"] = time.time() - 300
    data_path.write_text(json.dumps(data), encoding="utf-8")

    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {"apk_self_builder": {"ready": True, "ok": True}},
    }, token=token)
    assert registry.get_job(job_id)["job"]["status"] == "running"


def test_legacy_ghost_can_be_reconciled_when_registry_proves_executor_moved_on(tmp_path: Path) -> None:
    registry, token = _registry(tmp_path)
    data_path = tmp_path / "registry.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["workers"]["phone-test-apk"]["capabilities"] = ["apk-native", "apk-builder", "apk-self-builder"]
    data_path.write_text(json.dumps(data), encoding="utf-8")
    job_id = _create_build(registry)
    registry.poll_job({"worker_id": "phone-test-apk"}, token=token)

    now = time.time()
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["jobs"][job_id]["updated_at"] = now - 600
    data["jobs"][job_id]["executor_missing_since"] = now - 300
    queue = data["workers"]["phone-test-apk"].setdefault("status", {}).setdefault("core_worker_jobs", {})
    queue["last_job_id"] = "job-newer-proof"
    queue["last_poll_at"] = now
    data_path.write_text(json.dumps(data), encoding="utf-8")

    registry.heartbeat({
        "worker_id": "phone-test-apk",
        "runtime_kind": "apk",
        "source": "core-worker-apk-foreground-service",
        "capabilities": ["apk-native", "apk-builder", "apk-self-builder"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {"apk_self_builder": {"ready": True, "ok": True}},
    }, token=token)
    assert registry.get_job(job_id)["job"]["status"] == "queued"
