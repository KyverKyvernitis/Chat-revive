from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "utility/commands/workers_registry.py"
PHONE_WORKER_PATH = ROOT / "deploy/termux/phone-worker/phone_worker.py"
JAVA = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker"
GRADLE = ROOT / "android/core-worker-app/app/build.gradle"
AUTOMATION = ROOT / "scripts/core-worker-automation.py"
WEBSERVER = ROOT / "webserver.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parent_enrollment_issues_distinct_child_token_and_reuses_child_id(tmp_path: Path):
    mod = load("workers_registry_auto_enroll_test", REGISTRY_PATH)
    path = tmp_path / "registry.json"
    parent_token = "parent-secret-token"
    parent_id = "phone-localhost-c1111fd9"
    path.write_text(json.dumps({
        "version": 1,
        "pairings": {},
        "jobs": {},
        "workers": {
            parent_id: {
                "worker_id": parent_id,
                "name": "teste",
                "enabled": True,
                "token_hash": mod._hash_secret(parent_token),
                "runtime_kind": "termux",
                "source": "termux-phone-worker",
                "roles": ["phone-worker", "apk-builder"],
            }
        },
    }), encoding="utf-8")
    registry = mod.CoreWorkersRegistry(path)
    payload = {
        "parent_worker_id": parent_id,
        "challenge": "x" * 43,
        "install_id": "install-11111111",
        "sourceFingerprint": "a" * 64,
        "versionName": "0.8.2",
        "versionCode": 128,
    }
    first = registry.enroll_apk_child_from_parent(payload, parent_token=parent_token)
    assert first["worker_id"] == parent_id + "-apk"
    assert first["token"] != parent_token
    raw = json.loads(path.read_text(encoding="utf-8"))
    child = raw["workers"][parent_id + "-apk"]
    assert child["token_hash"] == mod._hash_secret(first["token"])
    assert raw["workers"][parent_id]["token_hash"] == mod._hash_secret(parent_token)
    assert child["bootstrap_shared_token"] is False
    assert child["parent_worker_id"] == parent_id
    assert child["physical_worker_id"] == parent_id

    second = registry.enroll_apk_child_from_parent({**payload, "install_id": "install-22222222"}, parent_token=parent_token)
    assert second["worker_id"] == first["worker_id"]
    assert second["token"] != first["token"]
    raw2 = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw2["workers"]) == 2
    assert raw2["workers"][parent_id + "-apk"]["token_hash"] == mod._hash_secret(second["token"])


def test_parent_enrollment_requires_authenticated_termux(tmp_path: Path):
    mod = load("workers_registry_auto_enroll_auth_test", REGISTRY_PATH)
    path = tmp_path / "registry.json"
    token = "secret-parent-token"
    path.write_text(json.dumps({
        "version": 1, "pairings": {}, "jobs": {},
        "workers": {"apk-parent": {
            "worker_id": "apk-parent", "enabled": True,
            "token_hash": mod._hash_secret(token), "runtime_kind": "apk", "source": "core-worker-apk",
        }},
    }), encoding="utf-8")
    registry = mod.CoreWorkersRegistry(path)
    payload = {"parent_worker_id": "apk-parent", "challenge": "c" * 32, "install_id": "install-12345678"}
    with pytest.raises(mod.CoreWorkerRegistryError) as exc:
        registry.enroll_apk_child_from_parent(payload, parent_token=token)
    assert exc.value.status == 409


def test_termux_auto_enrollment_delivers_child_credential_only_over_loopback(monkeypatch: pytest.MonkeyPatch):
    mod = load("phone_worker_auto_enroll_test", PHONE_WORKER_PATH)
    monkeypatch.setattr(mod, "_core_worker_auth_parts", lambda: ("https://vps.invalid", "parent-token", "phone-test"))
    monkeypatch.setattr(mod, "_get_local_json_url", lambda *a, **k: (200, {
        "ok": True,
        "state": "waiting_parent",
        "parent_worker_id": "phone-test",
        "challenge": "z" * 43,
        "install_id": "install-abcdef12",
        "sourceFingerprint": "b" * 64,
        "versionName": "0.8.2",
        "versionCode": 128,
    }))
    seen = {}
    def post_vps(path, payload, timeout=0):
        seen["vps_path"] = path
        seen["vps_payload"] = dict(payload)
        return 200, {"ok": True, "worker_id": "phone-test-apk", "parent_worker_id": "phone-test", "token": "child-token-abcdefghijklmnopqrstuvwxyz", "direct_http_token": "child-direct-abcdefghijklmnopqrstuvwxyz"}
    monkeypatch.setattr(mod, "_post_core_worker_json", post_vps)
    def post_local(url, payload, timeout=0):
        seen["local_url"] = url
        seen["local_payload"] = dict(payload)
        return 200, {"ok": True, "state": "paired", "worker_id": "phone-test-apk"}
    monkeypatch.setattr(mod, "_post_local_json_url", post_local)
    result = mod._try_auto_enroll_local_apk_once()
    assert result["ok"] is True
    assert seen["vps_path"] == "/core-worker/enroll/apk-child"
    assert seen["vps_payload"]["parent_worker_id"] == "phone-test"
    assert "parent-token" not in json.dumps(seen)
    assert seen["local_url"].startswith("http://127.0.0.1:8767/")
    assert seen["local_payload"]["token"].startswith("child-token-")
    assert seen["local_payload"]["server_url"] == "https://vps.invalid"


def test_android_enrollment_endpoint_is_loopback_only_and_pre_auth():
    server = (JAVA / "CoreWorkerDirectHttpServer.java").read_text(encoding="utf-8")
    enroll = (JAVA / "CoreWorkerAutoEnrollment.java").read_text(encoding="utf-8")
    identity = (JAVA / "CoreWorkerRuntimeIdentity.java").read_text(encoding="utf-8")
    assert 'loopback && "GET".equals(request.method) && "/core-worker/enrollment".equals(request.path)' in server
    assert 'loopback && "POST".equals(request.method) && "/core-worker/enrollment/complete".equals(request.path)' in server
    assert server.index('/core-worker/enrollment') < server.index('if (!authorized(request.headers))')
    assert 'putString("worker_token", token)' in enroll
    assert 'markChildApkPair' in enroll
    assert 'putString("parent_worker_id", parent)' in identity
    assert 'putInt("direct_http_port", APK_BOOTSTRAP_PORT)' in identity


def test_build_embeds_only_non_secret_parent_hint_and_source_fingerprint():
    gradle = GRADLE.read_text(encoding="utf-8")
    phone = PHONE_WORKER_PATH.read_text(encoding="utf-8")
    self_builder = (ROOT / "android/core-worker-app/app/src/main/python/coreworker/apk_self_builder.py").read_text(encoding="utf-8")
    assert 'CORE_WORKER_PARENT_WORKER_ID' in gradle
    assert 'CORE_WORKER_SOURCE_FINGERPRINT' in gradle
    assert 'CORE_WORKER_TOKEN' not in "\n".join(line for line in gradle.splitlines() if "buildConfigField" in line)
    assert '-PCORE_WORKER_PARENT_WORKER_ID=' in phone
    assert '-PCORE_WORKER_SOURCE_FINGERPRINT=' in phone
    assert '-PCORE_WORKER_PARENT_WORKER_ID=' in self_builder
    assert '-PCORE_WORKER_SOURCE_FINGERPRINT=' in self_builder


def test_vps_enrollment_is_bound_to_current_selected_builder_and_source():
    web = WEBSERVER.read_text(encoding="utf-8")
    registry = REGISTRY_PATH.read_text(encoding="utf-8")
    assert '@app.post("/core-worker/enroll/apk-child")' in web
    assert 'selectedBuilderWorkerId' in web
    assert 'APK pertence a uma source diferente do target atual' in web
    assert 'enroll_apk_child_from_parent' in registry
    assert 'bootstrap_shared_token": False' in registry
    assert 'secrets.token_urlsafe(32)' in registry


def test_normal_ui_prefers_automatic_enrollment_over_core_code():
    activity = (JAVA / "MainActivity.java").read_text(encoding="utf-8")
    assert 'Vínculo automático ativo. Nenhum código necessário.' in activity
    assert 'Vinculando automaticamente' in activity
    assert 'technicalDetailsContent.addView(pairingForm)' in activity
    assert 'connectCard.addView(pairingForm)' not in activity
    assert 'Recovery de pareamento' in activity


def test_versions_advance_for_auto_enrollment_protocol():
    gradle = GRADLE.read_text(encoding="utf-8")
    phone = PHONE_WORKER_PATH.read_text(encoding="utf-8")
    assert 'versionCode 132' in gradle
    assert 'versionName "0.8.5"' in gradle
    assert 'PHONE_WORKER_VERSION = "1.11.5"' in phone


def test_discord_panel_uses_manual_pairing_only_as_recovery():
    workers = (ROOT / "utility/commands/workers.py").read_text(encoding="utf-8")
    assert 'label="Parear celular"' not in workers
    assert 'Recovery de pareamento' in workers
    assert 'Código manual excepcional' in workers
    assert 'O fluxo normal do APK 0.8.4+ é automático e não usa código.' in workers


def test_auto_enrollment_does_not_require_vps_url_embedded_in_apk():
    enroll = (JAVA / "CoreWorkerAutoEnrollment.java").read_text(encoding="utf-8")
    supported_body = enroll.split("static boolean supported()", 1)[1].split("static String unsupportedReason()", 1)[0]
    assert "CORE_WORKER_PARENT_WORKER_ID" in supported_body
    assert "CORE_WORKER_SOURCE_FINGERPRINT" in supported_body
    assert "CORE_WORKER_VPS_URL" not in supported_body
    assert 'payload.optString("server_url", "")' in enroll
    assert 'serverUrl = safe(BuildConfig.CORE_WORKER_VPS_URL)' in enroll


def test_auto_enrollment_reports_missing_build_hint_instead_of_generic_recovery():
    enroll = (JAVA / "CoreWorkerAutoEnrollment.java").read_text(encoding="utf-8")
    activity = (JAVA / "MainActivity.java").read_text(encoding="utf-8")
    phone = PHONE_WORKER_PATH.read_text(encoding="utf-8")
    assert "parent_hint ausente no APK" in enroll
    assert "sourceFingerprint ausente no APK" in enroll
    assert "CoreWorkerAutoEnrollment.unsupportedReason()" in activity
    assert '"error": _short_text(local.get("error"), limit=120)' in phone
