from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"
AUTOMATION = ROOT / "scripts/core-worker-automation.py"
BOOTSTRAP = PHONE / "phone_worker_bootstrap.py"
PHONE_WORKER = PHONE / "phone_worker.py"
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"
PY_BUILDER = ANDROID / "app/src/main/python/coreworker/apk_self_builder.py"
REGISTRY = ROOT / "utility/commands/workers_registry.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _ApkHealth(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({
            "runtime_kind": "apk",
            "runtime_mode": "shared",
            "source": "core-worker-apk-agent-service-v2",
            "worker_id": "fake-apk",
            "version": "0.8.0",
            "source_hash": "f" * 64,
            "pid": os.getpid(),
            "status_updated_at": time.time(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _apk_server_8766():
    server = HTTPServer(("127.0.0.1", 8766), _ApkHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _start_script_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "PHONE_WORKER_DIR": str(tmp_path / "phone-worker"),
        "PHONE_WORKER_ENV": str(tmp_path / "missing.env"),
        "PHONE_WORKER_STATE_DIR": str(tmp_path / "state"),
        "PHONE_WORKER_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "PHONE_WORKER_PYTHON": sys.executable,
        "PHONE_WORKER_HOST": "127.0.0.1",
        "PHONE_WORKER_PORT": "8766",
        "PHONE_WORKER_START_WAIT_SECONDS": "0.2",
        "PHONE_WORKER_START_KILL_DUPLICATES": "false",
        "PHONE_WORKER_SSHD_AUTO_START": "false",
        "PHONE_WORKER_SAFE_MODE": "true",
        "PHONE_WORKER_DEPS_INSTALL_MODE": "disabled",
        "PHONE_WORKER_START_MUSIC_AGENT": "off",
    })
    return env


def _write_release_zip(path: Path, *, version: str, source_hash: str, members: dict[str, tuple[bytes, int]]) -> dict:
    declared = [
        {"path": name, "mode": mode, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, (raw, mode) in members.items()
    ]
    inner = {
        "schema": "core-phone-worker-release-v2",
        "version": version,
        "source_hash": source_hash,
        "min_bootstrap_version": "1.0.0",
        "members": declared,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("phone-worker-release.json", json.dumps(inner))
        for name, (raw, mode) in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, raw)
    return {
        **inner,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "url": "https://vps.invalid/core-worker/agent/releases/" + source_hash + ".zip",
    }


def _registry_record(reg, worker_id: str, token: str, *, source: str, runtime_kind: str, roles, caps, tasks, parent: str = "", status=None):
    now = time.time()
    return {
        "worker_id": worker_id,
        "name": worker_id,
        "enabled": True,
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "token_hash": reg._hash_secret(token),
        "source": source,
        "platform": "android" if runtime_kind == "apk" else "android-termux",
        "runtime_kind": runtime_kind,
        "runtime_mode": "shared" if runtime_kind == "apk" else "termux",
        "parent_worker_id": parent,
        "physical_worker_id": parent or worker_id,
        "roles": list(roles),
        "capabilities": list(caps),
        "supported_tasks": list(tasks),
        "status": status or {},
    }


def test_01_apk_health_on_8766_is_not_accepted_as_termux(tmp_path: Path):
    server = _apk_server_8766()
    worker_dir = tmp_path / "phone-worker"
    worker_dir.mkdir()
    # Processo vivo e oficialmente nomeado, mas sem runtime-status próprio.
    (worker_dir / "phone_worker.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    try:
        proc = subprocess.run(["bash", str(PHONE / "start-phone-worker.sh")], env=_start_script_env(tmp_path), text=True, capture_output=True, timeout=8)
        assert proc.returncode == 1
        assert "iniciado e validado" not in proc.stdout
        assert "identidade/control-plane não foram confirmados" in proc.stdout
    finally:
        pid_file = worker_dir / "phone-worker.pid"
        if pid_file.exists():
            with pytest.raises(Exception) if False else __import__("contextlib").nullcontext():
                try: os.kill(int(pid_file.read_text().strip()), 9)
                except Exception: pass
        server.shutdown(); server.server_close()


def test_02_dead_child_with_other_http_200_is_failure(tmp_path: Path):
    server = _apk_server_8766()
    worker_dir = tmp_path / "phone-worker"
    worker_dir.mkdir()
    (worker_dir / "phone_worker.py").write_text("import os\nos.execv('/bin/sleep', ['sleep', '5'])\n", encoding="utf-8")
    try:
        env = _start_script_env(tmp_path)
        env["PHONE_WORKER_START_WAIT_SECONDS"] = "1.2"
        proc = subprocess.run(["bash", str(PHONE / "start-phone-worker.sh")], env=env, text=True, capture_output=True, timeout=8)
        assert proc.returncode == 1
        assert ("recém-iniciado morreu" in proc.stdout or "identidade/control-plane não foram confirmados" in proc.stdout)
        assert "iniciado e validado" not in proc.stdout
    finally:
        server.shutdown(); server.server_close()


def test_03_control_plane_starts_before_http_bind(monkeypatch: pytest.MonkeyPatch):
    module = load("phone_worker_control_plane_order_test", PHONE_WORKER)
    order: list[str] = []
    monkeypatch.setattr(module, "_load_env_file", lambda: None)
    monkeypatch.setattr(module, "_load_persisted_pending_core_job_results", lambda: None)
    monkeypatch.setattr(module, "_write_runtime_status", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_start_core_worker_heartbeat", lambda **_kwargs: order.append("heartbeat"))
    monkeypatch.setattr(module, "_start_core_worker_jobs", lambda **_kwargs: order.append("jobs"))
    class Server:
        def serve_forever(self):
            order.append("serve")
            raise RuntimeError("stop")
    monkeypatch.setattr(module, "_bind_phone_worker_http_server", lambda *_a, **_k: order.append("bind") or Server())
    monkeypatch.setattr(sys, "argv", ["phone_worker.py"])
    with pytest.raises(RuntimeError, match="stop"):
        module.main()
    assert order[:3] == ["heartbeat", "jobs", "bind"]


def test_04_control_plane_only_survives_all_port_conflicts(monkeypatch: pytest.MonkeyPatch):
    module = load("phone_worker_control_plane_only_test", PHONE_WORKER)
    def busy(*_a, **_k):
        raise OSError(98, "Address already in use")
    monkeypatch.setattr(module, "ThreadingHTTPServer", busy)
    monkeypatch.setattr(module, "_probe_direct_http_owner", lambda *_a, **_k: "apk")
    server = module._bind_phone_worker_http_server("127.0.0.1", 8766, token="", max_body_bytes=1024, max_output_bytes=1024, job_timeout=3)
    assert server is None
    assert module._EFFECTIVE_HTTP_PORT is None
    assert module._DIRECT_HTTP_STATE == "port_conflict_control_plane_alive"


def test_05_offline_device_target_is_published_persistently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load("automation_offline_target_test", AUTOMATION)
    monkeypatch.setattr(module, "AGENT_RELEASE_ROOT", tmp_path / "agent")
    offline_worker = {
        "worker_id": "phone-offline",
        "enabled": True,
        "source": "termux-phone-worker",
        "runtime_kind": "termux",
        "runtime_mode": "termux",
        "version": "1.10.43",
        "source_hash": "c" * 64,
        "last_heartbeat_at": 0,
        "updated_at": 0,
        "capabilities": ["phone-worker"],
        "supported_tasks": ["worker_update"],
        "status": {},
    }
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: {"workers": [offline_worker]})
    monkeypatch.setattr(module, "_load_pending", lambda: {})
    monkeypatch.setattr(module, "_save_pending", lambda _v: None)
    monkeypatch.setattr(module, "_direct_phone_worker_update_if_needed", lambda *_a, **_k: {"ok": True, "skipped": True})
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://vps.invalid")
    result = module.queue_agent_updates()
    latest = json.loads((tmp_path / "agent/latest.json").read_text(encoding="utf-8"))
    assert result["pending"] is True
    assert latest["version"] == "1.11.5"
    assert (tmp_path / "agent/releases" / f"{latest['source_hash']}.zip").is_file()


def test_06_interrupted_download_never_promotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load("bootstrap_interrupted_download_test", BOOTSTRAP)
    monkeypatch.setattr(module, "_migrate_env", lambda _p: {})
    monkeypatch.setattr(module, "_load_env", lambda _p: {"CORE_WORKER_VPS_URL": "https://vps.invalid", "CORE_WORKER_ID": "w", "CORE_WORKER_TOKEN": "t"})
    monkeypatch.setattr(module, "_config_blocker", lambda _v: "")
    monkeypatch.setattr(module, "_fetch_latest", lambda *_a: {"version": "1.11.0", "source_hash": "a"*64, "sha256": "b"*64, "bytes": 100, "url": "https://vps.invalid/r.zip", "members": [{}]})
    monkeypatch.setattr(module, "_current_identity", lambda: {"version": "1.10.43", "source_hash": "c"*64})
    current = tmp_path / "runtime/releases/current"; current.mkdir(parents=True)
    monkeypatch.setattr(module, "_current_release", lambda: current)
    monkeypatch.setattr(module, "_runtime_root", lambda: tmp_path / "runtime")
    promoted = []
    monkeypatch.setattr(module, "_promote", lambda *_a: promoted.append(True))
    def interrupted(*_a, output_path=None, **_k):
        assert output_path is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"partial")
        raise OSError("connection reset")
    monkeypatch.setattr(module, "_signed_request", interrupted)
    with pytest.raises(OSError):
        module.check_and_apply()
    assert promoted == []


def test_07_corrupt_zip_is_rejected(tmp_path: Path):
    module = load("bootstrap_corrupt_zip_test", BOOTSTRAP)
    archive = tmp_path / "bad.zip"; archive.write_bytes(b"not-a-zip")
    outer = {
        "members": [], "version": "1", "source_hash": "a" * 64,
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    with pytest.raises(zipfile.BadZipFile):
        module._extract_and_validate(archive, tmp_path / "staging", outer)


def test_08_python_syntax_error_rejects_release(tmp_path: Path):
    module = load("bootstrap_bad_python_test", BOOTSTRAP)
    archive = tmp_path / "r.zip"
    outer = _write_release_zip(archive, version="1.11.0", source_hash="a"*64, members={"phone_worker.py": (b"def broken(:\n", 0o755)})
    with pytest.raises(Exception):
        module._extract_and_validate(archive, tmp_path / "stage", outer)


def test_09_invalid_shell_rejects_release(tmp_path: Path):
    module = load("bootstrap_bad_shell_test", BOOTSTRAP)
    archive = tmp_path / "r.zip"
    outer = _write_release_zip(archive, version="1.11.0", source_hash="a"*64, members={"start-phone-worker.sh": (b"#!/bin/bash\nif then\n", 0o755)})
    with pytest.raises(ValueError, match="script shell inválido"):
        module._extract_and_validate(archive, tmp_path / "stage", outer)


def test_10_runtime_hash_mismatch_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load("bootstrap_runtime_mismatch_test", BOOTSTRAP)
    runtime = tmp_path / "runtime"; previous = runtime / "releases/old"; final = runtime / "releases/new"
    previous.mkdir(parents=True); final.mkdir(parents=True)
    (previous / "phone-worker-release.json").write_text(json.dumps({"version": "1.10.43", "source_hash": "c"*64}), encoding="utf-8")
    monkeypatch.setattr(module, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(module, "_migrate_env", lambda _p: {})
    monkeypatch.setattr(module, "_load_env", lambda _p: {"CORE_WORKER_VPS_URL": "https://vps.invalid", "CORE_WORKER_ID": "w", "CORE_WORKER_TOKEN": "t"})
    monkeypatch.setattr(module, "_config_blocker", lambda _v: "")
    manifest = {"version": "1.11.0", "source_hash": "a"*64, "sha256": "b"*64, "bytes": 1, "url": "https://vps.invalid/r.zip", "members": [{}]}
    monkeypatch.setattr(module, "_fetch_latest", lambda *_a: manifest)
    monkeypatch.setattr(module, "_current_identity", lambda: {"version": "1.10.43", "source_hash": "c"*64})
    monkeypatch.setattr(module, "_current_release", lambda: previous)
    def signed(*_a, output_path=None, **_k):
        output_path.parent.mkdir(parents=True, exist_ok=True); output_path.write_bytes(b"x")
        return None, {}, 1, ""
    monkeypatch.setattr(module, "_signed_request", signed)
    monkeypatch.setattr(module, "_extract_and_validate", lambda *_a: {"members": 1})
    monkeypatch.setattr(module, "_promote", lambda *_a: (final, previous))
    monkeypatch.setattr(module, "_terminate_owned_agent", lambda: {})
    monkeypatch.setattr(module, "_start_runtime", lambda: None)
    answers = iter([{"ok": False}, {"ok": True}])
    monkeypatch.setattr(module, "_verify_runtime", lambda *_a, **_k: next(answers))
    result = module.check_and_apply()
    assert result["state"] == "rolled_back"
    assert (runtime / "current").resolve() == previous.resolve()


def test_11_same_version_different_hash_is_an_update():
    module = load("automation_same_version_hash_test", AUTOMATION)
    worker = {"version": "1.11.0", "source_hash": "b"*64}
    assert module._worker_needs_agent_update(worker, "1.11.0", "a"*64)


def test_12_automatic_downgrade_is_refused(monkeypatch: pytest.MonkeyPatch):
    module = load("bootstrap_downgrade_test", BOOTSTRAP)
    monkeypatch.setattr(module, "_migrate_env", lambda _p: {})
    monkeypatch.setattr(module, "_load_env", lambda _p: {"CORE_WORKER_VPS_URL": "https://vps.invalid", "CORE_WORKER_ID": "w", "CORE_WORKER_TOKEN": "t"})
    monkeypatch.setattr(module, "_config_blocker", lambda _v: "")
    monkeypatch.setattr(module, "_fetch_latest", lambda *_a: {"version": "1.10.43", "source_hash": "a"*64})
    monkeypatch.setattr(module, "_current_identity", lambda: {"version": "1.11.0", "source_hash": "b"*64})
    with pytest.raises(PermissionError, match="downgrade"):
        module.check_and_apply()


def test_13_config_migration_preserves_tokens_customization_and_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load("bootstrap_config_migration_test", BOOTSTRAP)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".phone-worker.env"
    env.write_text("PHONE_WORKER_CONFIG_SCHEMA=1\nCORE_WORKER_TOKEN=secret-value\nCORE_WORKER_ROLES=phone-worker,custom\nCORE_WORKER_HEARTBEAT_ENABLED=false\nCUSTOM_SETTING=yes\n", encoding="utf-8")
    module._migrate_env(env)
    data = env.read_text(encoding="utf-8")
    assert "CORE_WORKER_TOKEN=secret-value" in data
    assert "CORE_WORKER_ROLES=phone-worker,custom" in data
    assert "CORE_WORKER_HEARTBEAT_ENABLED=false" in data
    assert "CUSTOM_SETTING=yes" in data
    assert "PHONE_WORKER_CONFIG_SCHEMA=2" in data
    assert module._config_blocker(module._load_env(env)) == "CORE_WORKER_HEARTBEAT_ENABLED"


def test_14_bootstrap_stays_below_size_budget():
    assert BOOTSTRAP.stat().st_size < 256 * 1024


def test_15_registry_update_payload_contains_no_agent_base64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = load("automation_small_update_payload_test", AUTOMATION)
    worker = {
        "worker_id": "w", "name": "w", "online": True, "enabled": True, "version": "1.10.43", "source_hash": "0"*64,
        "source": "termux-phone-worker", "runtime_kind": "termux", "runtime_mode": "termux",
        "roles": ["phone-worker"], "capabilities": ["phone-worker"], "supported_tasks": ["worker_update"],
        "status": {"worker_update": {"transports": ["bootstrap_manifest_v2"], "updater": {"bootstrap_version": "1.0.0"}}},
    }
    captured = []
    class R:
        def create_job(self, **kwargs):
            captured.append(kwargs); return {"job": {"job_id": "j"}}
    monkeypatch.setattr(module, "get_core_workers_registry", lambda: R())
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: {"workers": [worker]})
    monkeypatch.setattr(module, "AGENT_RELEASE_ROOT", tmp_path / "agent")
    monkeypatch.setattr(module, "_public_base_url", lambda: "https://vps.invalid")
    monkeypatch.setattr(module, "_direct_phone_worker_update_if_needed", lambda *_a, **_k: {"ok": False, "skipped": True})
    monkeypatch.setattr(module, "_active_job_exists", lambda **_k: False)
    monkeypatch.setattr(module, "_load_pending", lambda: {})
    monkeypatch.setattr(module, "_save_pending", lambda _v: None)
    module.queue_agent_updates(force=True)
    payload = captured[0]["payload"]
    encoded = json.dumps(payload)
    assert "data_b64" not in encoded and '"files"' not in encoded
    assert len(encoded.encode()) < 16 * 1024


def test_16_only_canonical_agent_root_is_packaged():
    module = load("automation_canonical_root_test", AUTOMATION)
    assert module._canonical_phone_worker_root() == PHONE.resolve()
    payload = module._build_worker_update_payload()
    targets = {item["target"] for item in payload["files"]}
    assert "phone_worker.py" in targets
    assert all("tts-bot-main/" not in target for target in targets)
    for installer in (PHONE / "install.sh", PHONE / "bootstrap-phone-worker.sh", ROOT / "scripts/sync-phone-worker.sh"):
        body = text(installer)
        assert "phone_worker_bootstrap.py" in body
        assert "repair-phone-worker.sh" in body
        assert "accept-core-worker-on-device.sh" in body


def test_17_oom_is_transient():
    module = load("automation_oom_classification_test", AUTOMATION)
    out = module._apk_build_failure_classification({"status": "failed", "result": {"error": "java.lang.OutOfMemoryError: Java heap space"}})
    assert out == {"category": "transient", "retryable": True, "permanent": False}


def test_18_java_compile_error_is_deterministic():
    module = load("automation_compile_classification_test", AUTOMATION)
    out = module._apk_build_failure_classification({"status": "failed", "result": {"error": "compileDebugJavaWithJavac: error: cannot find symbol"}})
    assert out["category"] == "deterministic" and out["permanent"] is True


def test_19_old_failure_context_does_not_block_new_agent_or_toolchain(monkeypatch: pytest.MonkeyPatch):
    module = load("automation_failure_context_test", AUTOMATION)
    fp = "f"*64
    job = {"type": "apk_build_debug", "status": "failed", "updated_at": time.time(), "summary": f"build 0.8.0 {fp[:12]}", "payload": {"versionName": "0.8.0", "sourceFingerprint": fp, "requiredAgentSourceHash": "a"*64, "toolchainFingerprint": "b"*64, "selectedBuilderWorkerId": "old"}, "result": {"error": "OutOfMemoryError"}}
    monkeypatch.setattr(module, "_load_registry_snapshot", lambda: {"jobs": [job]})
    assert module._recent_failed_apk_build("0.8.0", fp, agent_source_hash="c"*64, toolchain_fingerprint="d"*64, builder_worker_id="new") == {}


def test_20_old_jobs_are_superseded_when_source_changes(tmp_path: Path):
    reg = load("registry_supersede_source_test", REGISTRY)
    path = tmp_path / "registry.json"
    old = "a"*64; new = "b"*64
    path.write_text(json.dumps({"version": 1, "pairings": {}, "workers": {}, "jobs": {
        "q": {"job_id": "q", "type": "apk_build_debug", "status": "queued", "payload": {"sourceFingerprint": old}},
        "r": {"job_id": "r", "type": "apk_build_debug", "status": "running", "payload": {"sourceFingerprint": old}},
    }}), encoding="utf-8")
    registry = reg.CoreWorkersRegistry(path)
    result = registry.supersede_apk_jobs_for_new_source(new, version_code=127)
    raw = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert result["superseded"] == 1 and result["invalidated_running"] == 1
    assert raw["q"]["status"] == "superseded"
    assert raw["r"]["obsolete_source"] is True
    assert "_desired_source_publish_error" in text(ROOT / "webserver.py")


def test_21_apk_child_never_receives_worker_update(tmp_path: Path):
    reg = load("registry_apk_child_update_test", REGISTRY)
    token = "shared-token"; parent = "phone"; child = "phone-apk"
    path = tmp_path / "registry.json"
    parent_record = _registry_record(reg, parent, token, source="termux-phone-worker", runtime_kind="termux", roles=["phone-worker"], caps=["phone-worker"], tasks=["worker_update"])
    child_record = _registry_record(reg, child, token, source="core-worker-apk-agent-service-v2", runtime_kind="apk", roles=["apk-worker"], caps=["apk-worker"], tasks=["worker_update"], parent=parent)
    path.write_text(json.dumps({"version": 1, "pairings": {}, "workers": {parent: parent_record, child: child_record}, "jobs": {}}), encoding="utf-8")
    registry = reg.CoreWorkersRegistry(path)
    created = registry.create_job(job_type="worker_update", payload={"version": "1.11.0"}, target_worker_id=child, required_capabilities=["phone-worker"])
    assert created["job"]["target_worker_id"] == parent
    raw = json.loads(path.read_text(encoding="utf-8"))["jobs"][created["job"]["job_id"]]
    assert raw["routed_from_worker_id"] == child


def test_22_apk_builder_capability_requires_ready_preflight():
    manager = text(JAVA / "CoreWorkerApkBuildManager.java")
    assert 'if (preflight.optBoolean("ready", false)) {' in manager
    ready_block = manager[manager.index("static JSONArray dynamicCapabilities"):manager.index("static JSONArray dynamicRoles")]
    assert 'out.put("apk-builder")' in ready_block
    assert ready_block.index('out.put("apk-builder")') > ready_block.index('preflight.optBoolean("ready", false)')


def test_23_new_apk_has_no_embedded_toolchain_assets():
    gradle = text(ANDROID / "app/build.gradle")
    assert "verifyCoreWorkerNoEmbeddedToolchain" in gradle
    assets = ANDROID / "app/src/main/assets"
    forbidden = []
    if assets.exists():
        for path in assets.rglob("*"):
            if not path.is_file(): continue
            low = str(path.relative_to(assets)).lower()
            if path.name == "android-builder-toolchain.zip" or path.name.endswith(".cwpart") or "/jdk/" in f"/{low}/" or "/gradle/" in f"/{low}/" or "/android-sdk/" in f"/{low}/":
                forbidden.append(low)
    assert forbidden == []


def test_24_corrupt_external_toolchain_is_verified_before_promotion():
    manager = text(JAVA / "CoreWorkerApkBuildManager.java")
    download = manager.index("downloadAuthenticated(releaseUrl")
    extract = manager.index("extractZip(archivePart, staging)")
    schema = manager.index("schema interno do toolchain externo inválido")
    promote = manager.index("promoteToolchain(builder, toolchain, staging", schema)
    assert download < extract < schema < promote
    assert "HMAC" in manager or "signature" in manager.lower()


def test_25_valid_toolchain_runs_five_required_smokes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(ANDROID / "app/src/main/python"))
    module = load("apk_builder_five_smokes_test", PY_BUILDER)
    seen = []
    monkeypatch.setattr(module, "_toolchain_fingerprint", lambda _tool: "f"*64)
    monkeypatch.setattr(module, "_toolchain_environment", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "_run_smoke_command", lambda name, command, env, timeout: seen.append(name) or {"name": name, "ok": True, "returncode": 0})
    tool = {"paths": {"java": "java", "javac": "javac", "jar": "jar", "gradle": "gradle", "aapt2": "aapt2"}}
    result = module._toolchain_smoke(tmp_path, tool, force=True)
    assert result["ok"] is True
    assert seen == ["java", "javac", "jar", "gradle", "aapt2"]


def test_26_validated_toolchain_is_retained_between_updates():
    manager = text(JAVA / "CoreWorkerApkBuildManager.java")
    assert 'File previous = new File(builder, "toolchain-previous")' in manager
    assert "apk_self_builder_known_good_toolchain_fingerprint" in manager
    assert "apk_self_builder_previous_toolchain_fingerprint" in manager
    assert "archivePart.delete()" in manager


def test_27_toolchain_loss_removes_apk_builder_and_termux_remains_fallback():
    manager = text(JAVA / "CoreWorkerApkBuildManager.java")
    automation = text(AUTOMATION)
    capabilities = manager[manager.index("static JSONArray dynamicCapabilities"):manager.index("static JSONArray dynamicRoles")]
    assert 'if (preflight.optBoolean("ready", false))' in capabilities
    assert 'out.put("apk-builder")' in capabilities
    assert "if apk_candidates" in automation and "if termux_candidates" in automation
    assert automation.index("if apk_candidates") < automation.index("if termux_candidates")


def test_28_apk_signature_and_identity_are_verified_before_publish():
    automation = text(AUTOMATION)
    web = text(ROOT / "webserver.py")
    assert "assert_expected_apk_identity" in automation
    assert "inspect_apk_identity" in automation
    assert "assert_expected_apk_identity" in web or "inspect_apk_identity" in web
    assert "keystore" in automation.lower()
    assert "sourceFingerprint" in web and "desired_source" in web


def test_hotfix_java_home_prefers_real_jdk17_over_path_java21(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load("phone_worker_hotfix_jdk17_test", PHONE_WORKER)
    prefix = tmp_path / "usr"
    j17 = prefix / "lib/jvm/java-17-openjdk"
    j21 = prefix / "lib/jvm/java-21-openjdk"
    for home, major in ((j17, 17), (j21, 21)):
        bindir = home / "bin"
        bindir.mkdir(parents=True)
        for name in ("javac", "jar"):
            path = bindir / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        java = bindir / "java"
        java.write_text(f"#!/bin/sh\necho 'openjdk version \"{major}.0.1\"' >&2\n", encoding="utf-8")
        java.chmod(0o755)
    monkeypatch.setattr(module.shutil, "which", lambda name, path=None: str(j21 / "bin/java") if name == "java" else None)
    env = {"PREFIX": str(prefix), "PATH": str(j21 / "bin"), "JAVA_HOME": str(j21)}
    assert module._find_termux_java_home(env) == j17.resolve()


def test_hotfix_gradle_xmx_failure_is_deterministic_even_if_old_agent_says_unknown() -> None:
    automation = load("automation_hotfix_gradle_classification_test", AUTOMATION)
    job = {
        "status": "failed",
        "error": 'preparação do autobuilder falhou: Could not find or load main class "-Xmx64m"',
        "result": {
            "failure_category": "unknown",
            "summary": 'smoke gradle falhou: java.lang.ClassNotFoundException: "-Xmx64m"',
        },
    }
    result = automation._apk_build_failure_classification(job)
    assert result == {"category": "deterministic", "retryable": False, "permanent": True}


def test_hotfix_toolchain_normalizes_copied_gradle_launcher_before_smoke() -> None:
    source = text(PHONE_WORKER)
    copy_marker = '_copy_tree_dereferenced(gradle_home, bundle_root / "gradle")'
    patch_marker = '_patch_gradle_launcher_for_android(bundle_root / "gradle/bin/gradle")'
    smoke_marker = 'smoke = _smoke_apk_self_builder_bundle(bundle_root)'
    assert copy_marker in source and patch_marker in source and smoke_marker in source
    assert source.index(copy_marker) < source.index(patch_marker) < source.index(smoke_marker)


def test_hotfix_supervisor_force_restart_is_real() -> None:
    source = text(PHONE / "start-phone-worker.sh")
    assert '--force-restart|--restart' in source
    assert 'FORCE_RESTART=1' in source
    assert 'restart_forced pid=$existing_pid' in source
    assert 'kill_worker_processes' in source


def test_hotfix_registry_snapshot_preserves_only_safe_apk_failure_context() -> None:
    reg = load("registry_hotfix_compact_context_test", REGISTRY)
    record = {
        "job_id": "job-test",
        "type": "apk_build_debug",
        "status": "failed",
        "target_worker_id": "phone-1",
        "worker_id": "phone-1",
        "created_at": time.time() - 5,
        "updated_at": time.time(),
        "payload": {
            "versionName": "0.8.0",
            "versionCode": 127,
            "sourceFingerprint": "a" * 64,
            "requiredAgentSourceHash": "b" * 64,
            "toolchainFingerprint": "",
            "selectedBuilderWorkerId": "phone-1",
            "selectedBuilderRuntimeKind": "termux",
            "google_services_json_b64": "SECRET_FIREBASE",
            "keystore_b64": "SECRET_KEYSTORE",
        },
        "result": {
            "failure_category": "unknown",
            "summary": 'Could not find or load main class "-Xmx64m"',
        },
    }
    compact = reg._compact_job_public(record, include_result=False)
    assert compact["sourceFingerprint"] == "a" * 64
    assert compact["requiredAgentSourceHash"] == "b" * 64
    assert compact["selectedBuilderWorkerId"] == "phone-1"
    assert compact["selectedBuilderRuntimeKind"] == "termux"
    serialized = json.dumps(compact)
    assert "SECRET_FIREBASE" not in serialized
    assert "SECRET_KEYSTORE" not in serialized
    assert "payload" not in compact and "result" not in compact


def test_hotfix_recent_deterministic_failure_blocks_requeue_from_compact_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    automation = load("automation_hotfix_compact_failure_test", AUTOMATION)
    now = time.time()
    compact_job = {
        "job_id": "job-failed",
        "type": "apk_build_debug",
        "status": "failed",
        "created_at": now - 20,
        "updated_at": now - 10,
        "worker_id": "phone-1",
        "target_worker_id": "phone-1",
        "summary": "build automático APK 0.8.0 " + ("a" * 12),
        "error": 'smoke gradle falhou: Could not find or load main class "-Xmx64m"',
        "versionName": "0.8.0",
        "versionCode": 127,
        "sourceFingerprint": "a" * 64,
        "requiredAgentSourceHash": "b" * 64,
        "toolchainFingerprint": "",
        "selectedBuilderWorkerId": "phone-1",
        "selectedBuilderRuntimeKind": "termux",
        "failure_category": "unknown",
    }
    monkeypatch.setattr(automation, "_load_registry_snapshot", lambda: {"jobs": [compact_job], "workers": []})
    failed = automation._recent_failed_apk_build(
        "0.8.0",
        "a" * 64,
        agent_source_hash="b" * 64,
        toolchain_fingerprint="",
        builder_worker_id="phone-1",
        cooldown_seconds=60,
    )
    assert failed["permanent"] is True
    assert failed["category"] == "deterministic"
    assert failed["job"]["job_id"] == "job-failed"


def test_29_publish_last_repairs_desired_source_with_original_builder_before_queue(monkeypatch: pytest.MonkeyPatch):
    module = load("automation_publish_last_builder_repair_test", AUTOMATION)
    calls: list[tuple[str, dict]] = []

    class Registry:
        def create_job(self, **kwargs):
            calls.append(("create_job", kwargs))
            return {"ok": True, "job": {"job_id": "job-publish-fixed"}}

    monkeypatch.setattr(module, "get_core_workers_registry", lambda: Registry())
    monkeypatch.setattr(module, "_active_job_exists", lambda **_kwargs: False)
    monkeypatch.setattr(module, "_load_pending", lambda: {})
    monkeypatch.setattr(module, "_save_pending", lambda _value: None)
    monkeypatch.setattr(module, "_recent_failed_apk_publish_last", lambda **_kwargs: {})

    def publish_desired(**kwargs):
        calls.append(("desired", kwargs))
        return {"record": kwargs, "previousRecord": {"selectedBuilderWorkerId": ""}, "changed": False}

    monkeypatch.setattr(module, "_publish_desired_apk_source", publish_desired)
    found = {
        "worker_id": "phone-localhost-c1111fd9",
        "selected_builder_worker_id": "phone-localhost-c1111fd9",
        "selected_builder_runtime_kind": "termux",
        "required_agent_source_hash": "a" * 64,
        "toolchain_fingerprint": "b" * 64,
        "artifact_path": "/tmp/CoreWorker-v0.8.0-debug.apk",
        "filename": "CoreWorker-v0.8.0-debug.apk",
    }
    result = module._queue_apk_publish_last_from_build(
        found,
        version_name="0.8.0",
        version_code=127,
        source_fingerprint="c" * 64,
        source_sha256="d" * 64,
        notification_id="apk-127-test",
    )
    assert result["pending"] is True
    assert [name for name, _ in calls][:2] == ["desired", "create_job"]
    desired = calls[0][1]
    assert desired["selected_builder_worker_id"] == "phone-localhost-c1111fd9"
    assert desired["selected_builder_runtime_kind"] == "termux"
    assert desired["required_agent_source_hash"] == "a" * 64
    assert desired["toolchain_fingerprint"] == "b" * 64
    payload = calls[1][1]["payload"]
    assert payload["selectedBuilderWorkerId"] == "phone-localhost-c1111fd9"
    assert payload["sourceFingerprint"] == "c" * 64


def test_30_publish_last_failure_does_not_requeue_every_automation_cycle(monkeypatch: pytest.MonkeyPatch):
    module = load("automation_publish_last_cooldown_test", AUTOMATION)

    class Registry:
        def create_job(self, **_kwargs):
            raise AssertionError("não deveria criar outro apk_publish_last durante cooldown")

    monkeypatch.setattr(module, "get_core_workers_registry", lambda: Registry())
    monkeypatch.setattr(module, "_active_job_exists", lambda **_kwargs: False)
    monkeypatch.setattr(module, "_publish_desired_apk_source", lambda **kwargs: {
        "record": kwargs,
        "previousRecord": {"selectedBuilderWorkerId": "phone-localhost-c1111fd9"},
        "changed": False,
    })
    monkeypatch.setattr(module, "_recent_failed_apk_publish_last", lambda **_kwargs: {
        "job": {"job_id": "job-old-failed"},
        "retry_after_seconds": 541,
    })
    result = module._queue_apk_publish_last_from_build(
        {
            "worker_id": "phone-localhost-c1111fd9",
            "selected_builder_worker_id": "phone-localhost-c1111fd9",
            "selected_builder_runtime_kind": "termux",
            "required_agent_source_hash": "a" * 64,
            "toolchain_fingerprint": "b" * 64,
            "artifact_path": "/tmp/CoreWorker-v0.8.0-debug.apk",
        },
        version_name="0.8.0",
        version_code=127,
        source_fingerprint="c" * 64,
        source_sha256="d" * 64,
        notification_id="apk-127-test",
    )
    assert result["phase"] == "publish_blocked"
    assert result["blocked_by_recent_failure"] is True
    assert result["last_failed_job_id"] == "job-old-failed"


def test_31_built_unpublished_apk_carries_original_builder_context(monkeypatch: pytest.MonkeyPatch):
    module = load("automation_built_artifact_context_test", AUTOMATION)
    job = {
        "job_id": "job-built",
        "type": "apk_build_debug",
        "status": "failed",
        "worker_id": "phone-localhost-c1111fd9",
        "updated_at": time.time(),
        "payload": {
            "versionName": "0.8.0",
            "sourceFingerprint": "c" * 64,
            "selectedBuilderWorkerId": "phone-localhost-c1111fd9",
            "selectedBuilderRuntimeKind": "termux",
            "requiredAgentSourceHash": "a" * 64,
            "toolchainFingerprint": "b" * 64,
        },
        "result": {
            "artifact_found": True,
            "artifact_path": "/tmp/CoreWorker-v0.8.0-debug.apk",
            "publish_ok": False,
        },
    }
    monkeypatch.setattr(module, "_registry_raw", lambda: {"jobs": {"job-built": job}})
    found = module._recent_built_unpublished_apk("0.8.0", "c" * 64)
    assert found["selected_builder_worker_id"] == "phone-localhost-c1111fd9"
    assert found["selected_builder_runtime_kind"] == "termux"
    assert found["required_agent_source_hash"] == "a" * 64
    assert found["toolchain_fingerprint"] == "b" * 64
