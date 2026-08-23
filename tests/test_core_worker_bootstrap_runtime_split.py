from __future__ import annotations

import json
import time
from pathlib import Path

from utility.commands.workers_registry import CoreWorkersRegistry, _hash_secret


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _record(worker_id: str, token: str, *, source: str, platform: str, roles: list[str], tasks: list[str]) -> dict:
    now = time.time()
    return {
        "worker_id": worker_id,
        "name": "teste",
        "enabled": True,
        "token_hash": _hash_secret(token),
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "source": source,
        "platform": platform,
        "roles": roles,
        "capabilities": roles,
        "supported_tasks": tasks,
        "status": {},
    }


def test_legacy_apk_collision_is_split_without_counting_two_phones(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            "phone-localhost-test": _record(
                "phone-localhost-test",
                token,
                source="core-worker-apk-agent-service-v2",
                platform="android",
                roles=["apk-worker", "diagnostics"],
                tasks=["apk_ping"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")

    registry = CoreWorkersRegistry(registry_path)
    heartbeat = registry.heartbeat({
        "worker_id": "phone-localhost-test",
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "runtime_kind": "apk",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)

    assert heartbeat["worker_id"] == "phone-localhost-test-apk"
    snapshot = registry.snapshot()
    workers = {item["worker_id"]: item for item in snapshot["workers"]}
    assert set(workers) == {"phone-localhost-test", "phone-localhost-test-apk"}
    assert workers["phone-localhost-test"]["runtime_kind"] == "termux"
    assert workers["phone-localhost-test"]["online"] is False
    assert workers["phone-localhost-test-apk"]["runtime_kind"] == "apk"
    assert workers["phone-localhost-test-apk"]["parent_worker_id"] == "phone-localhost-test"
    assert workers["phone-localhost-test-apk"]["online"] is True
    assert snapshot["summary"]["registered"] == 1
    assert snapshot["summary"]["runtime_registered"] == 2

    restored = registry.heartbeat({
        "worker_id": "phone-localhost-test",
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "physical_worker_id": "phone-localhost-test",
        "version": "1.10.34",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["worker_update", "apk_build_debug", "apk_publish_last"],
    }, token=token)
    assert restored["worker_id"] == "phone-localhost-test"
    assert restored["worker"]["source"] == "termux-phone-worker"


def test_old_apk_can_poll_child_and_submit_result_with_physical_id(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    worker_id = "phone-localhost-result"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    registry.heartbeat({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)
    child_id = f"{worker_id}-apk"
    created = registry.create_job(
        job_type="apk_ping",
        target_worker_id=child_id,
        ttl_seconds=300,
        lease_seconds=120,
        summary="legacy result bridge",
    )
    job_id = created["job"]["job_id"]

    polled = registry.poll_job({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping"],
    }, token=token)
    assert polled["worker_id"] == child_id
    assert polled["job"]["job_id"] == job_id

    submitted = registry.submit_job_result({
        "worker_id": worker_id,
        "job_id": job_id,
        "status": "succeeded",
        "summary": "ok",
        "result": {"ok": True, "type": "apk_ping"},
    }, token=token)
    assert submitted["ok"] is True
    assert submitted["worker_id"] == child_id


def test_registry_preserves_only_authenticated_opaque_job_payloads_above_text_limit(tmp_path: Path) -> None:
    token = "worker-update-bootstrap-token"
    worker_id = "phone-large-update"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker"],
                tasks=["worker_update"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    encoded = "A" * 700_000
    ordinary_text = "x" * 300_000
    target_hash = "c" * 64
    bootstrap_hash = "d" * 64
    created = registry.create_job(
        job_type="worker_update",
        target_worker_id=worker_id,
        required_capabilities=["phone-worker"],
        payload={
            "version": "1.10.39",
            "bootstrap_stage": True,
            "bootstrap_complete": False,
            "bootstrap_source_hash": bootstrap_hash,
            "target_source_hash": target_hash,
            "note": ordinary_text,
            "files": [{"target": "phone_worker.py", "data_b64": encoded}],
        },
    )

    polled = registry.poll_job({"worker_id": worker_id}, token=token)

    assert polled["job"]["payload"]["files"][0]["data_b64"] == encoded
    assert polled["job"]["payload"]["note"].endswith("…[truncated]")
    assert len(polled["job"]["payload"]["note"]) < len(ordinary_text)
    registry.submit_job_result({
        "worker_id": worker_id,
        "job_id": created["job"]["job_id"],
        "status": "succeeded",
        "result": {
            "ok": True,
            "applied_file_version": "1.10.39",
        },
    }, token=token)
    current = next(item for item in registry.snapshot()["workers"] if item["worker_id"] == worker_id)
    assert current["version"] == "1.10.39"
    assert current["source_hash"] == bootstrap_hash


def test_android_runtime_uses_child_identity_and_bootstrap_port() -> None:
    identity = _read(JAVA / "CoreWorkerRuntimeIdentity.java")
    service = _read(JAVA / "CoreWorkerRuntimeService.java")
    direct = _read(JAVA / "CoreWorkerDirectHttpServer.java")
    activity = _read(JAVA / "MainActivity.java")

    assert "APK_BOOTSTRAP_PORT = 8767" in identity
    assert 'return safe + "-apk"' in identity
    assert 'putString("parent_worker_id", canonical)' in identity
    assert "CoreWorkerRuntimeIdentity.migrate(getApplicationContext())" in service
    assert "CoreWorkerRuntimeIdentity.putRuntimeFields(getApplicationContext(), payload)" in service
    assert "CoreWorkerRuntimeIdentity.directHttpPort(context)" in direct
    assert "CoreWorkerRuntimeIdentity.markDedicatedApkPair(prefs, workerId)" in activity
    assert "CoreWorkerRuntimeIdentity.clear(editor)" in activity


def test_automation_routes_apk_trigger_to_termux_bootstrap() -> None:
    automation = _read(ROOT / "scripts/core-worker-automation.py")
    phone_worker = _read(ROOT / "deploy/termux/phone-worker/phone_worker.py")
    webserver = _read(ROOT / "webserver.py")
    assert "def _bootstrap_worker_id_for_runtime" in automation
    assert "runtime APK não recebe worker_update" in automation
    assert "worker_update continua reservado ao Termux bootstrap" in automation
    assert 'PHONE_WORKER_VERSION = "1.10.43"' in phone_worker
    assert '"runtime_kind": "termux"' in phone_worker
    assert '"platform": "android-termux"' in phone_worker
    assert "def _core_worker_automation_transition_is_urgent" in webserver
    assert 'CORE_WORKER_PENDING_AUTOMATION_TRANSITION_COOLDOWN_SECONDS", 8.0' in webserver


def test_bootstrap_build_stays_on_termux_until_apk_self_builder_is_ready(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    worker_id = "phone-localhost-builder"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    # O APK legado ainda usa o ID físico, mas deve virar runtime filho sem
    # sobrescrever as capacidades do Termux bootstrap.
    registry.heartbeat({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping", "apk_builder_status"],
        "status": {"apk_self_builder": {"ready": False, "publishReady": False}},
    }, token=token)
    child_id = f"{worker_id}-apk"

    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
        summary="bootstrap 0.7.2",
    )
    assert created["job"]["preferred_worker_id"] == worker_id

    child_poll = registry.poll_job({
        "worker_id": worker_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "roles": ["apk-worker", "diagnostics"],
        "capabilities": ["apk-worker", "diagnostics"],
        "supported_tasks": ["apk_ping", "apk_builder_status"],
    }, token=token)
    assert child_poll["worker_id"] == child_id
    assert child_poll["job"] is None

    termux_poll = registry.poll_job({
        "worker_id": worker_id,
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["worker_update", "apk_build_debug", "apk_publish_last"],
    }, token=token)
    assert termux_poll["worker_id"] == worker_id
    assert termux_poll["job"]["type"] == "apk_build_debug"


def test_automation_maps_apk_child_to_physical_termux_runtime() -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_runtime_split_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    snapshot = {
        "workers": [
            {
                "worker_id": "phone-test",
                "source": "termux-phone-worker",
                "runtime_kind": "termux",
                "roles": ["phone-worker", "apk-builder"],
            },
            {
                "worker_id": "phone-test-apk",
                "source": "core-worker-apk-agent-service-v2",
                "runtime_kind": "apk",
                "parent_worker_id": "phone-test",
                "physical_worker_id": "phone-test",
                "roles": ["apk-worker"],
            },
        ]
    }
    assert module._bootstrap_worker_id_for_runtime(snapshot, "phone-test-apk") == "phone-test"


def test_failed_direct_update_keeps_bootstrap_pending(monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_pending_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    snapshot = {
        "workers": [
            {
                "worker_id": "phone-test",
                "name": "teste",
                "enabled": True,
                "online": False,
                "source": "termux-bootstrap-awaiting-heartbeat",
                "runtime_kind": "termux",
                "roles": ["phone-worker", "apk-builder"],
                "capabilities": ["phone-worker", "apk-builder"],
                "supported_tasks": ["worker_update", "apk_build_debug"],
            },
            {
                "worker_id": "phone-test-apk",
                "name": "teste · APK",
                "enabled": True,
                "online": True,
                "source": "core-worker-apk-agent-service-v2",
                "runtime_kind": "apk",
                "parent_worker_id": "phone-test",
                "physical_worker_id": "phone-test",
                "roles": ["apk-worker"],
            },
        ]
    }
    pending = {}
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_build_worker_update_payload", lambda: {"version": "1.10.34", "files": []})
    monkeypatch.setattr(module, "_direct_phone_worker_update_if_needed", lambda *a, **k: {
        "ok": False,
        "skipped": True,
        "port_conflict": True,
        "summary": "porta direta responde ao APK",
    })
    monkeypatch.setattr(module, "_load_pending", lambda: dict(pending))
    monkeypatch.setattr(module, "_save_pending", lambda value: (pending.clear(), pending.update(value)))

    result = module.queue_agent_updates(only_worker_id="phone-test-apk")
    assert result["pending"] is True
    assert "agent_update" in pending
    assert result["direct_update"]["port_conflict"] is True


def test_manual_jobs_selected_on_apk_child_route_to_termux_bootstrap(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    parent_id = "phone-manual-route"
    child_id = f"{parent_id}-apk"
    now = time.time()
    parent = _record(
        parent_id,
        token,
        source="termux-phone-worker",
        platform="android-termux",
        roles=["phone-worker", "apk-builder"],
        tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
    )
    child = _record(
        child_id,
        token,
        source="core-worker-apk-agent-service-v2",
        platform="android",
        roles=["apk-worker", "diagnostics"],
        tasks=["apk_builder_status"],
    )
    child.update({
        "runtime_kind": "apk",
        "parent_worker_id": parent_id,
        "physical_worker_id": parent_id,
        "status": {"apk_self_builder": {"ready": False, "publishReady": False}},
        "updated_at": now,
        "last_heartbeat_at": now,
    })
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {parent_id: parent, child_id: child},
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)

    update = registry.create_job(
        job_type="worker_update",
        target_worker_id=child_id,
        required_capabilities=["phone-worker"],
        ttl_seconds=300,
        lease_seconds=120,
    )
    assert update["job"]["target_worker_id"] == parent_id

    build = registry.create_job(
        job_type="apk_build_debug",
        target_worker_id=child_id,
        required_capabilities=["apk-builder"],
        payload={"selfBuilderRequired": True},
        ttl_seconds=300,
        lease_seconds=300,
    )
    assert build["job"]["target_worker_id"] == parent_id


def test_shared_runtime_switches_future_builds_to_apk_after_real_preflight(tmp_path: Path) -> None:
    token = "shared-bootstrap-token"
    parent_id = "phone-ready-switch"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            parent_id: _record(
                parent_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker", "apk-builder"],
                tasks=["worker_update", "apk_build_debug", "apk_publish_last"],
            ),
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    child_id = f"{parent_id}-apk"

    heartbeat = registry.heartbeat({
        "worker_id": child_id,
        "parent_worker_id": parent_id,
        "physical_worker_id": parent_id,
        "source": "core-worker-apk-agent-service-v2",
        "platform": "android",
        "runtime_kind": "apk",
        "roles": ["apk-worker", "apk-builder", "apk-self-builder"],
        "capabilities": ["apk-worker", "apk-builder", "apk-self-builder", "apk-publisher"],
        "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"],
        "status": {"apk_self_builder": {"ready": True, "publishReady": True}},
    }, token=token)
    assert heartbeat["worker_id"] == child_id

    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
    )
    assert created["job"]["preferred_worker_id"] == child_id


def test_apk_build_waits_for_worker_update_instead_of_recording_false_failure(monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_update_gate_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pending = {
        "agent_update": {"target_version": "1.10.37", "pending": True},
        "apk_build": {"versionName": "0.7.4", "versionCode": 122, "pending": True},
    }
    snapshot = {
        "workers": [{
            "worker_id": "phone-test",
            "online": True,
            "version": "1.10.35",
            "source": "termux-phone-worker",
            "runtime_kind": "termux",
            "roles": ["phone-worker"],
            "capabilities": ["phone-worker"],
            "supported_tasks": ["worker_update"],
        }]
    }

    monkeypatch.setattr(module, "_load_pending", lambda: dict(pending))
    monkeypatch.setattr(module, "_save_pending", lambda value: (pending.clear(), pending.update(value)))
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_read_phone_worker_version", lambda: "1.10.37")
    monkeypatch.setattr(module, "_read_android_version", lambda: ("0.7.4", 122))
    monkeypatch.setattr(module, "_workers_need_agent_version", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "queue_agent_updates", lambda **_kwargs: {
        "ok": True,
        "pending": True,
        "target_version": "1.10.37",
        "queued": ["teste:job-update"],
    })
    monkeypatch.setattr(module, "queue_apk_build", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("APK não deve ser enfileirado antes do upgrade")))
    monkeypatch.setattr(module, "write_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_env_bool", lambda *_args, **_kwargs: False)

    result = module.process_pending(worker_id="phone-test")

    assert result["apk_build"]["phase"] == "waiting_worker_update"
    assert result["apk_build"]["pending"] is True
    assert result["apk_build"]["ok"] is True
    assert pending["apk_build"]["targetWorkerVersion"] == "1.10.37"
    assert not pending["apk_build"].get("error")


def test_same_version_source_hash_difference_still_requires_agent_update() -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_hash_gate_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = "a" * 64
    worker = {
        "worker_id": "phone-hash-test",
        "online": True,
        "enabled": True,
        "version": "1.10.39",
        "source_hash": "b" * 64,
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "roles": ["phone-worker"],
        "capabilities": ["phone-worker"],
        "supported_tasks": ["worker_update"],
    }

    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected) is True
    worker["source_hash"] = expected
    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected) is False
    worker.pop("source_hash")
    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected) is True
    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected, force=True) is True
    assert module._worker_supports_update_archive(worker) is False
    worker["status"] = {"worker_update": {"transports": ["inline-b64-v1", "zip-v1"]}}
    assert module._worker_supports_update_archive(worker) is True
    worker["version"] = "1.11.0"
    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected, force=True) is False
    worker["version"] = "1.10.38"
    worker["online"] = False
    assert module._workers_need_agent_version({"workers": [worker]}, "1.10.39", expected) is False
    assert module._registered_workers_need_agent_version({"workers": [worker]}, "1.10.39", expected) is True


def test_agent_update_migrates_legacy_core_then_queues_verified_zip(tmp_path: Path, monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_two_stage_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = "two-stage-update-token"
    worker_id = "phone-two-stage"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "workers": {
            worker_id: _record(
                worker_id,
                token,
                source="termux-phone-worker",
                platform="android-termux",
                roles=["phone-worker"],
                tasks=["worker_update"],
            ) | {"version": "1.10.25"},
        },
        "jobs": {},
    }), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    monkeypatch.setattr(module, "get_core_workers_registry", lambda: registry)
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: registry.snapshot())
    monkeypatch.setattr(module, "_direct_phone_worker_update_if_needed", lambda *_args, **_kwargs: {
        "ok": False,
        "skipped": True,
        "summary": "rota direta desativada no teste",
    })
    monkeypatch.setattr(module, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(module, "_core_worker_release_dir", lambda: tmp_path / "releases")
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://vps.invalid")

    first = module.queue_agent_updates(force=True)
    first_job = next(job for job in json.loads(registry_path.read_text(encoding="utf-8"))["jobs"].values() if job["status"] == "queued")
    assert first["pending"] is True
    assert first_job["payload"]["bootstrap_stage"] is True
    assert [item["target"] for item in first_job["payload"]["files"]] == ["phone_worker.py"]

    delivered = registry.poll_job({"worker_id": worker_id}, token=token)["job"]
    registry.submit_job_result({
        "worker_id": worker_id,
        "job_id": delivered["job_id"],
        "status": "succeeded",
        "result": {"ok": True, "applied_file_version": "1.10.43"},
    }, token=token)
    registry.heartbeat({
        "worker_id": worker_id,
        "version": "1.10.43",
        "source_hash": "",
        "source": "termux-phone-worker",
        "platform": "android-termux",
        "runtime_kind": "termux",
        "roles": ["phone-worker"],
        "capabilities": ["phone-worker"],
        "supported_tasks": ["worker_update"],
        "status": {"worker_update": {"transports": ["inline-b64-v1", "zip-v1"]}},
    }, token=token)

    second = module.queue_agent_updates(force=True)
    queued = [job for job in json.loads(registry_path.read_text(encoding="utf-8"))["jobs"].values() if job["status"] == "queued"]
    assert second["pending"] is True
    assert len(queued) == 1
    assert queued[0]["payload"]["update_transport"] == "zip-v1"
    assert "files" not in queued[0]["payload"]
    archive = next((tmp_path / "releases").glob("phone-worker-update-*.zip"))
    assert queued[0]["payload"]["update_zip_sha256"] == module._sha256_file(archive)


def test_apk_retry_after_agent_update_bypasses_old_failure_once(monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_retry_after_agent_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pending = {"apk_build": {"pending": True, "retry_after_agent_update": True}}
    calls: list[bool] = []
    monkeypatch.setattr(module, "_load_pending", lambda: dict(pending))
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: {"workers": []})
    monkeypatch.setattr(module, "_read_phone_worker_version", lambda: "1.10.43")
    monkeypatch.setattr(module, "_hash_phone_worker_files", lambda *_args: "a" * 64)
    monkeypatch.setattr(module, "_read_android_version", lambda: ("0.7.4", 122))
    monkeypatch.setattr(module, "_workers_need_agent_version", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "queue_apk_build", lambda *, manual=False: calls.append(manual) or {"ok": True, "pending": True})
    monkeypatch.setattr(module, "write_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_env_bool", lambda *_args, **_kwargs: False)

    result = module.process_pending(worker_id="phone-test")

    assert calls == [True]
    assert result["apk_retry_reason"] == "agent_updated"


def test_no_compatible_builder_is_transient_pending_not_build_failure(monkeypatch) -> None:
    import importlib.util

    path = ROOT / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_builder_wait_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pending: dict = {}

    class Registry:
        def create_job(self, **_kwargs):
            raise module.CoreWorkerRegistryError("nenhum worker online compatível para este job", status=409)

    monkeypatch.setattr(module, "get_core_workers_registry", lambda: Registry())
    monkeypatch.setattr(module, "_read_android_version", lambda: ("0.7.4", 122))
    monkeypatch.setattr(module, "_prepare_apk_source_zip", lambda: {
        "url": "https://example.invalid/source.zip",
        "sha256": "a" * 64,
        "bytes": 123,
        "firebase_config_delivery": "job_payload",
    })
    monkeypatch.setattr(module, "_current_fingerprints", lambda: {"apk_source_hash": "b" * 64})
    monkeypatch.setattr(module, "_load_google_services_payload_for_apk_build", lambda: {})
    monkeypatch.setattr(module, "_load_apk_signing_payload_for_worker_build", lambda: {})
    monkeypatch.setattr(module, "_load_pending", lambda: dict(pending))
    monkeypatch.setattr(module, "_save_pending", lambda value: (pending.clear(), pending.update(value)))
    monkeypatch.setattr(module, "_reconcile_apk_build_pending_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_apk_needs_build", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_recent_built_unpublished_apk", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_stale_running_apk_build_for_source", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_pending_apk_build_recently_queued", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_recent_failed_apk_build", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_active_job_exists", lambda **_kwargs: False)
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://example.invalid")

    result = module.queue_apk_build()

    assert result["ok"] is True
    assert result["pending"] is True
    assert result["phase"] == "waiting_builder"
    assert result["transient"] is True
    assert result["error"] == ""
    assert "nenhum worker online compatível" in result["last_enqueue_error"]


def test_turbo_profile_contract_cannot_lose_apk_builder_to_stale_env() -> None:
    phone_worker = _read(ROOT / "deploy/termux/phone-worker/phone_worker.py")
    assert "def _merge_profile_contract" in phone_worker
    assert "valores do env continuam aceitos como extensões" in phone_worker
    assert "roles, capabilities = _current_core_worker_roles_and_capabilities()" in phone_worker
    assert 'PHONE_WORKER_VERSION = "1.10.43"' in phone_worker
