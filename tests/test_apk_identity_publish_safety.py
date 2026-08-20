from __future__ import annotations

import importlib.util
import io
import json
import struct
import sys
import types
import zipfile
from pathlib import Path

import pytest

from utility.apk_identity import ApkIdentityError, assert_expected_apk_identity, inspect_apk_identity, parse_android_manifest_identity


ANDROID_NS = "http://schemas.android.com/apk/res/android"
NO_INDEX = 0xFFFFFFFF


def _length8(value: int) -> bytes:
    assert 0 <= value < 0x80
    return bytes([value])


def _binary_manifest(*, version_name: str, version_code: int, package_name: str = "dev.core.worker") -> bytes:
    strings = ["manifest", "package", package_name, ANDROID_NS, "versionCode", "versionName", version_name]
    encoded = []
    offsets = []
    cursor = 0
    for value in strings:
        raw = value.encode("utf-8")
        item = _length8(len(value)) + _length8(len(raw)) + raw + b"\0"
        offsets.append(cursor)
        encoded.append(item)
        cursor += len(item)
    string_data = b"".join(encoded)
    while len(string_data) % 4:
        string_data += b"\0"
    strings_start = 28 + 4 * len(strings)
    string_pool_size = strings_start + len(string_data)
    string_pool = (
        struct.pack("<HHI", 0x0001, 28, string_pool_size)
        + struct.pack("<IIIII", len(strings), 0, 0x100, strings_start, 0)
        + b"".join(struct.pack("<I", item) for item in offsets)
        + string_data
    )

    resource_ids = [0, 0, 0, 0, 0x0101021B, 0x0101021C, 0]
    resource_map = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(resource_ids)) + b"".join(
        struct.pack("<I", item) for item in resource_ids
    )

    def attr(namespace: int, name: int, raw: int, data_type: int, value: int) -> bytes:
        return struct.pack("<IIIHBBI", namespace, name, raw, 8, 0, data_type, value)

    attributes = b"".join((
        attr(NO_INDEX, 1, 2, 0x03, 2),
        attr(3, 4, NO_INDEX, 0x10, version_code),
        attr(3, 5, 6, 0x03, 6),
    ))
    start_size = 16 + 20 + len(attributes)
    start_element = (
        struct.pack("<HHI", 0x0102, 16, start_size)
        + struct.pack("<II", 1, NO_INDEX)
        + struct.pack("<IIHHHHHH", NO_INDEX, 0, 20, 20, 3, 0, 0, 0)
        + attributes
    )
    body = string_pool + resource_map + start_element
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def _apk_bytes(*, version_name: str, version_code: int, package_name: str = "dev.core.worker") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "AndroidManifest.xml",
            _binary_manifest(version_name=version_name, version_code=version_code, package_name=package_name),
        )
        archive.writestr("classes.dex", b"dex\n035\0" + b"x" * 64)
    return output.getvalue()


def _write_apk(path: Path, *, version_name: str, version_code: int) -> Path:
    path.write_bytes(_apk_bytes(version_name=version_name, version_code=version_code))
    return path


def _load_webserver_without_flask(monkeypatch: pytest.MonkeyPatch):
    class Logger:
        def exception(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def info(self, *args, **kwargs):
            return None

    class App:
        def __init__(self, *_args, **_kwargs):
            self.logger = Logger()

        def get(self, *_args, **_kwargs):
            return lambda function: function

        def post(self, *_args, **_kwargs):
            return lambda function: function

    flask = types.ModuleType("flask")
    flask.Flask = App
    flask.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
    flask.abort = lambda status: (_ for _ in ()).throw(RuntimeError(f"abort {status}"))
    flask.send_file = lambda *args, **kwargs: None
    flask.request = types.SimpleNamespace()
    waitress = types.ModuleType("waitress")
    waitress.serve = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "flask", flask)
    monkeypatch.setitem(sys.modules, "waitress", waitress)

    path = Path(__file__).resolve().parents[1] / "webserver.py"
    spec = importlib.util.spec_from_file_location("webserver_publish_safety_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Form(dict):
    def to_dict(self, flat: bool = True):
        return dict(self)


def _publish(module, apk: bytes, *, version_name: str = "", version_code: int | None = None, filename: str = "CoreWorker.apk"):
    form: dict[str, str] = {"worker_id": "builder-1", "filename": filename}
    if version_name:
        form["versionName"] = version_name
    if version_code is not None:
        form["versionCode"] = str(version_code)
    upload = types.SimpleNamespace(stream=io.BytesIO(apk), filename=filename)
    module.request = types.SimpleNamespace(
        form=_Form(form),
        headers={},
        files={"apk": upload},
        remote_addr="127.0.0.1",
        url_root="https://worker.example/",
    )
    return module.core_worker_app_publish()


def test_binary_axml_identity_is_source_of_truth(tmp_path: Path) -> None:
    raw = _binary_manifest(version_name="0.7.4", version_code=122)
    assert parse_android_manifest_identity(raw) == {
        "packageName": "dev.core.worker",
        "versionName": "0.7.4",
        "versionCode": 122,
    }
    apk = _write_apk(tmp_path / "CoreWorker.apk", version_name="0.7.4", version_code=122)
    identity = inspect_apk_identity(apk)
    assert identity["versionCode"] == 122
    with pytest.raises(ApkIdentityError, match="version(Name|Code) do APK divergente"):
        assert_expected_apk_identity(identity, expected_version_name="0.7.5", expected_version_code=123)


def test_vps_rejects_old_apk_with_fake_version_and_preserves_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webserver = _load_webserver_without_flask(monkeypatch)
    from utility.commands import workers_registry

    monkeypatch.setenv("CORE_WORKER_APK_DIR", str(tmp_path))
    monkeypatch.setenv("CORE_WORKER_APK_SIGNING_DISABLED", "true")
    monkeypatch.setattr(webserver, "_find_android_build_tool", lambda _name: None)
    monkeypatch.setattr(webserver, "_kick_core_worker_fcm_push", lambda *args, **kwargs: None)
    monkeypatch.setattr(webserver, "_kick_core_worker_pending_automation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workers_registry,
        "core_worker_authenticate_http",
        lambda headers, payload, remote_addr="": (200, {
            "ok": True,
            "worker": {
                "worker_id": "builder-1",
                "name": "Builder no celular",
                "roles": ["phone-worker", "apk-builder"],
                "capabilities": ["phone-worker", "apk-builder"],
            },
        }),
    )

    first = _publish(
        webserver,
        _apk_bytes(version_name="0.7.3", version_code=121),
        version_name="0.7.3",
        version_code=121,
        filename="CoreWorker-v0.7.3-debug.apk",
    )
    first_body, first_status = first
    assert first_status == 200, first_body
    latest_path = tmp_path / "latest.json"
    previous_latest = latest_path.read_bytes()
    previous_apk = (tmp_path / "CoreWorker-v0.7.3-debug.apk").read_bytes()

    fake = _publish(
        webserver,
        _apk_bytes(version_name="0.7.2", version_code=120),
        version_name="0.7.4",
        version_code=122,
        filename="CoreWorker-v0.7.4-debug.apk",
    )
    body, fake_status = fake
    assert fake_status == 409
    assert body["compiled"]["versionName"] == "0.7.2"
    assert body["requested"]["versionName"] == "0.7.4"
    assert body["preservedPreviousRelease"] is True
    assert latest_path.read_bytes() == previous_latest
    assert (tmp_path / "CoreWorker-v0.7.3-debug.apk").read_bytes() == previous_apk
    assert not (tmp_path / "CoreWorker-v0.7.4-debug.apk").exists()

    downgrade = _publish(
        webserver,
        _apk_bytes(version_name="0.7.2", version_code=120),
        filename="CoreWorker-v0.7.2-debug.apk",
    )
    downgrade_body, downgrade_status = downgrade
    assert downgrade_status == 409
    assert "downgrade" in downgrade_body["error"]
    assert latest_path.read_bytes() == previous_latest

    current = _publish(
        webserver,
        _apk_bytes(version_name="0.7.4", version_code=122),
        version_name="0.7.4",
        version_code=122,
        filename="CoreWorker-v0.7.4-debug.apk",
    )
    current_body, current_status = current
    assert current_status == 200, current_body
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    assert latest["versionName"] == "0.7.4"
    assert latest["versionCode"] == 122
    assert latest["validation"]["identity"]["versionCode"] == 122
    assert current_body["storageCleanup"]["ok"] is True

    for version_name, version_code in (("0.7.5", 123), ("0.7.6", 124)):
        published_body, published_status = _publish(
            webserver,
            _apk_bytes(version_name=version_name, version_code=version_code),
            version_name=version_name,
            version_code=version_code,
            filename=f"CoreWorker-v{version_name}-debug.apk",
        )
        assert published_status == 200, published_body

    assert not (tmp_path / "CoreWorker-v0.7.3-debug.apk").exists()
    assert (tmp_path / "CoreWorker-v0.7.4-debug.apk").is_file()
    assert (tmp_path / "CoreWorker-v0.7.5-debug.apk").is_file()
    assert (tmp_path / "CoreWorker-v0.7.6-debug.apk").is_file()
    assert published_body["storageCleanup"]["removed"] == ["CoreWorker-v0.7.3-debug.apk"]


def test_self_builder_restores_jspawnhelper_and_never_builds_on_vps() -> None:
    root = Path(__file__).resolve().parents[1]
    manager = (root / "android/core-worker-app/app/src/main/java/dev/core/worker/CoreWorkerApkBuildManager.java").read_text(encoding="utf-8")
    gradle = (root / "android/core-worker-app/app/build.gradle").read_text(encoding="utf-8")
    web = (root / "webserver.py").read_text(encoding="utf-8")
    assert "restoreExecutablePaths(staging, stagedManifest)" in manager
    assert "restoreExecutablePaths(toolchain, new File(toolchain, \"manifest.json\"))" in manager
    assert "jdk/lib/jspawnhelper" in manager
    assert "launcher Gradle Android v5" in manager
    assert "android-sh-unquoted-default-jvm-opts-v1" in manager
    assert "executablePaths" in gradle
    assert "assembleDebug" not in web


def test_termux_artifact_metadata_is_rebuilt_from_apk_identity(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    worker_path = root / "deploy/termux/phone-worker/phone_worker.py"
    spec = importlib.util.spec_from_file_location("phone_worker_identity_safety_test", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    apk = _write_apk(artifacts / "CoreWorker-v0.7.4-debug.apk", version_name="0.7.4", version_code=122)
    (artifacts / "latest-artifact.json").write_text(json.dumps({
        "artifact_path": str(apk),
        "filename": "CoreWorker-v9.9.9-debug.apk",
        "versionName": "9.9.9",
        "versionCode": 9999,
        "sha256": "0" * 64,
    }), encoding="utf-8")

    metadata = module._latest_apk_artifact_metadata(tmp_path)
    assert metadata["filename"] == apk.name
    assert metadata["versionName"] == "0.7.4"
    assert metadata["versionCode"] == 122
    assert metadata["sha256"] == module._sha256_path(apk)


def test_automation_does_not_treat_fake_latest_version_as_published(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    automation_path = root / "scripts/core-worker-automation.py"
    spec = importlib.util.spec_from_file_location("core_worker_automation_identity_test", automation_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    release_dir = tmp_path / "android/core-worker-app/releases"
    release_dir.mkdir(parents=True)
    apk = _write_apk(release_dir / "CoreWorker-v0.7.4-debug.apk", version_name="0.7.3", version_code=121)
    actual_sha = module._sha256_file(apk)
    (release_dir / "latest.json").write_text(json.dumps({
        "filename": apk.name,
        "apkUrl": f"/core-worker/app/{apk.name}",
        "versionName": "0.7.4",
        "versionCode": 122,
        "sha256": actual_sha,
    }), encoding="utf-8")
    module.ROOT = tmp_path

    latest = module._latest_apk_manifest()
    assert latest["versionName"] == "0.7.3"
    assert latest["versionCode"] == 121
    assert latest["compiledIdentityVerified"] is True
    assert module._apk_needs_build(122, "") is True


def test_latest_endpoint_never_serves_declared_fake_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webserver = _load_webserver_without_flask(monkeypatch)
    monkeypatch.setenv("CORE_WORKER_APK_DIR", str(tmp_path))
    monkeypatch.setattr(webserver, "_core_worker_fcm_public_summary", lambda: {"active": 0})
    apk = _write_apk(tmp_path / "CoreWorker-v0.7.3-debug.apk", version_name="0.7.3", version_code=121)
    digest = webserver.hashlib.sha256(apk.read_bytes()).hexdigest()
    (tmp_path / "latest.json").write_text(json.dumps({
        "ok": True,
        "filename": apk.name,
        "apkUrl": f"/core-worker/app/{apk.name}",
        "versionName": "0.7.4",
        "versionCode": 122,
        "sha256": digest,
    }), encoding="utf-8")
    webserver.request = types.SimpleNamespace(url_root="https://worker.example/")

    body, status = webserver.core_worker_app_latest()
    assert status == 200
    assert body["versionName"] == "0.7.3"
    assert body["versionCode"] == 121
    assert body["metadataMismatchDetected"] is True
    assert body["compiledIdentityVerified"] is True
