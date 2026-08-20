from __future__ import annotations

import json
import re
import time
from pathlib import Path

from utility.commands.workers_registry import (
    CORE_WORKER_JOB_TYPES,
    CoreWorkersRegistry,
    _hash_secret,
)


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"
PYTHON = ANDROID / "app/src/main/python/coreworker"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def java_array(source: str, name: str) -> list[str]:
    match = re.search(rf"{re.escape(name)}\s*=\s*new String\[\]\s*\{{(.*?)\}};", source, re.S)
    assert match, f"array {name} not found"
    return re.findall(r'"([a-z0-9_]+)"', match.group(1))


def all_android_java() -> str:
    return "\n".join(read(path) for path in sorted(JAVA.glob("*.java")))


def _worker_record(*, worker_id: str, token: str, apk: bool, ready: bool) -> dict:
    now = time.time()
    if apk:
        return {
            "worker_id": worker_id,
            "name": "Core Worker APK",
            "enabled": True,
            "registered_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "token_hash": _hash_secret(token),
            "source": "core-worker-apk-agent-service",
            "platform": "android",
            "roles": ["apk-worker", "apk-builder"] if ready else ["apk-worker"],
            "capabilities": ["apk-worker", "apk-builder"] if ready else ["apk-worker"],
            "supported_tasks": ["apk_builder_status", "apk_build_debug", "apk_publish_last"] if ready else ["apk_builder_status"],
            "status": {"apk_self_builder": {"ready": ready, "publishReady": ready}},
        }
    return {
        "worker_id": worker_id,
        "name": "Termux bootstrap",
        "enabled": True,
        "registered_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
        "token_hash": _hash_secret(token),
        "source": "phone-worker-termux",
        "platform": "android-termux",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["apk_build_debug", "apk_publish_last"],
        "status": {},
    }


def test_version_marks_bootstrap_to_self_builder_release() -> None:
    gradle = read(ANDROID / "app/build.gradle")
    assert "versionCode 123" in gradle
    assert 'versionName "0.7.5"' in gradle
    assert "def coreWorkerSelfBuilderTargetSdk = 28" in gradle
    assert "targetSdk coreWorkerSelfBuilderTargetSdk" in gradle
    assert "verifyCoreWorkerSelfBuilderTargetSdk" in gradle
    assert read(ANDROID / "README.md").startswith("# Core Worker 0.7.5 — launcher Gradle compatível com Android")


def test_android_runtime_has_no_legacy_termux_protocol_or_package_dependency() -> None:
    source = all_android_java()
    for forbidden in (
        "/core-worker/app/jobs/fetch",
        "/core-worker/app/jobs/result",
        "/core-worker/app/heartbeat",
        "127.0.0.1:8766/local/",
        'getLaunchIntentForPackage("com.termux")',
        'getPackageInfo("com.termux")',
    ):
        assert forbidden not in source
    manifest = read(ANDROID / "app/src/main/AndroidManifest.xml")
    assert 'android:name="com.termux"' not in manifest
    assert 'android:name="com.termux.api"' not in manifest
    assert 'android:name="com.termux.boot"' not in manifest


def test_direct_http_api_is_authenticated_and_compatible() -> None:
    server = read(JAVA / "CoreWorkerDirectHttpServer.java")
    for route in ("/health", "/status", "/task", "/tts-agent/health", "/tts-agent/synthesize.raw"):
        assert f'"{route}"' in server
    assert 'new InetSocketAddress("0.0.0.0", port)' in server
    assert "constantTimeEquals" in server
    assert 'headers.get("authorization")' in server
    assert 'safeHeader(headers, "x-phone-worker-token")' in server
    assert 'safeHeader(headers, "x-core-worker-token")' in server
    assert 'return json(401' in server
    assert '"/shell"' not in server
    assert '"/command"' not in server


def test_direct_executor_has_allowlist_without_free_shell() -> None:
    executor = read(JAVA / "CoreWorkerDirectTaskExecutor.java")
    assert "static boolean supports(String rawTask)" in executor
    assert "ProcessBuilder" in executor  # somente ferramentas privadas com argv validado
    assert 'new String[] {"/system/bin/sh"' not in executor
    assert "Runtime.getRuntime().exec" not in executor
    assert "isSafeFfmpegArg" in executor
    assert "caminho ZIP suspeito" in executor
    assert "entrada grande demais" in executor


def test_java_python_and_registry_catalogs_include_dynamic_builder() -> None:
    catalog = read(JAVA / "CoreWorkerJobCatalog.java")
    internal = java_array(catalog, "APK_JOBS")
    direct = java_array(catalog, "DIRECT_REGISTRY_TASKS")
    remote = internal + direct
    assert len(internal) == 44
    assert len(direct) == 38
    assert len(remote) == len(set(remote))
    assert set(remote).issubset(CORE_WORKER_JOB_TYPES)
    assert "CoreWorkerApkBuildManager.availableTasks(context)" in catalog
    for task in ("apk_build_debug", "apk_publish_last", "apk_builder_status"):
        assert task in CORE_WORKER_JOB_TYPES
        assert f'"{task}"' in read(JAVA / "CoreWorkerApkBuildManager.java")


def test_runtime_dispatches_builder_before_regular_jobs() -> None:
    service = read(JAVA / "CoreWorkerRuntimeService.java")
    manager_pos = service.index("CoreWorkerApkBuildManager.supports(jobType)")
    internal_pos = service.index("CoreWorkerJobCatalog.supports(jobType)")
    assert manager_pos < internal_pos
    assert 'serverUrl + "/core-worker/jobs/poll"' in service
    assert 'serverUrl + "/core-worker/jobs/result"' in service
    assert 'serverUrl + "/core-worker/heartbeat"' in service
    assert "CoreWorkerJobCatalog.remoteSupportedTasks(getApplicationContext())" in service
    assert 'status.put("apk_self_builder"' in service
    assert "CoreWorkerApkBuildManager.refreshAsync(getApplicationContext())" in service
    manager = read(JAVA / "CoreWorkerApkBuildManager.java")
    assert "preflightRefreshRunning" in manager
    assert "readPersistedPreflight" in manager
    assert "callPythonPreflight(context, true)" in manager


def test_self_builder_has_strict_toolchain_and_no_arbitrary_command() -> None:
    manager = read(JAVA / "CoreWorkerApkBuildManager.java")
    builder = read(PYTHON / "apk_self_builder.py")
    gradle = read(ANDROID / "app/build.gradle")
    assert "core-linux/android-builder/android-builder-toolchain.zip" in manager
    assert "core-worker-android-builder-v1" in builder
    assert 'manifest_version >= 5' in builder
    assert 'android-sh-unquoted-default-jvm-opts-v1' in builder
    assert 'manifest.optInt("version", 0) < 5' in manager
    assert 'launcher Gradle incompatível com /system/bin/sh' in manager
    assert '"runtime": "android-private-toolchain-direct"' in builder
    assert 'del native_dir  # assinatura mantida para compatibilidade Java; builder não depende do rootfs/PRoot.' in builder
    assert '"javac": javac.is_file()' in builder
    assert '"jar": jar.is_file()' in builder
    assert "MAX_SOURCE_BYTES = 1024 * 1024 * 1024" in builder
    assert "MAX_SOURCE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024" in builder
    assert "def _toolchain_smoke(" in builder
    assert '["/system/bin/sh", paths["gradle"], "--version", "--no-daemon"]' in builder
    assert '[paths["aapt2"], "version"]' in builder
    assert '"CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN": "true"' in builder
    assert '"/system/bin/sh", paths["gradle"], "assembleDebug"' in builder
    assert "subprocess.Popen(command" in builder
    assert "payload.get(\"command\")" not in builder
    assert "payload.get(\"shell\")" not in builder
    assert "verifyCoreWorkerSelfBuilderToolchain" in gradle
    assert "CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN" in gradle
    assert "core-worker-android-builder-v1" in gradle
    assert "android-sh-unquoted-default-jvm-opts-v1" in gradle
    assert "pip = false" in gradle
    assert "stdlib = false" in gradle
    assert "targetSdk coreWorkerSelfBuilderTargetSdk" in gradle


def test_termux_bootstrap_requires_self_builder_toolchain() -> None:
    phone_worker = read(ROOT / "deploy/termux/phone-worker/phone_worker.py")
    automation = read(ROOT / "scripts/core-worker-automation.py")
    workers = read(ROOT / "utility/commands/workers.py")
    assert 'PHONE_WORKER_VERSION = "1.10.40"' in phone_worker
    assert 'env["CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN"] = "true"' in phone_worker
    assert '_prepare_apk_self_builder_toolchain(project_dir, env)' in phone_worker
    assert '"runtime": "termux-bionic-direct"' in phone_worker
    assert '"generatedOnTermux": True' in phone_worker
    assert 'toolchain self-builder ausente e geração bootstrap só é permitida no Termux' in phone_worker
    assert '_env_int("PHONE_WORKER_APK_BUILD_SOURCE_MAX_BYTES", 1024 * 1024 * 1024)' in phone_worker
    assert '"selfBuilderRequired": True' in automation
    assert '"selfBuilderRequired": True' in workers
    assert "external_build_required" not in automation
    assert "build/publicação de APK não fazem parte do worker móvel" not in workers
    assert '"builder": ("phone-worker"' in workers


def test_vps_only_orchestrates_and_streams_published_apk() -> None:
    webserver = read(ROOT / "webserver.py")
    automation = read(ROOT / "scripts/core-worker-automation.py")
    assert "assembleDebug" not in webserver
    assert 'subprocess.run(["gradle"' not in automation
    assert 'subprocess.Popen(["gradle"' not in automation
    assert 'CORE_WORKER_APK_UPLOAD_MAX_BYTES", str(1024 * 1024 * 1024)' in webserver
    assert "final_raw = open(target, \"rb\").read()" not in webserver
    assert "final_digest = hashlib.sha256()" in webserver
    assert "source_bytes = zip_path.stat().st_size" in automation
    assert "raw = zip_path.read_bytes()" not in automation
    assert '@app.post("/core-worker/app/publish")' in webserver


def test_registry_prefers_ready_apk_then_termux_fallback(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    apk_token = "apk-secret-token"
    termux_token = "termux-secret-token"
    data = {
        "version": 1,
        "pairings": {},
        "workers": {
            "apk-worker-1": _worker_record(worker_id="apk-worker-1", token=apk_token, apk=True, ready=True),
            "termux-worker-1": _worker_record(worker_id="termux-worker-1", token=termux_token, apk=False, ready=True),
        },
        "jobs": {},
    }
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
        summary="self build",
    )
    assert created["job"]["preferred_worker_id"] == "apk-worker-1"

    # Durante a janela de preferência, o bootstrap não rouba o job.
    polled_termux = registry.poll_job({"worker_id": "termux-worker-1"}, token=termux_token)
    assert polled_termux["job"] is None
    polled_apk = registry.poll_job({"worker_id": "apk-worker-1"}, token=apk_token)
    assert polled_apk["job"]["type"] == "apk_build_debug"


def test_registry_uses_termux_when_apk_self_builder_is_not_ready(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    apk_token = "apk-secret-token"
    termux_token = "termux-secret-token"
    data = {
        "version": 1,
        "pairings": {},
        "workers": {
            "apk-worker-1": _worker_record(worker_id="apk-worker-1", token=apk_token, apk=True, ready=False),
            "termux-worker-1": _worker_record(worker_id="termux-worker-1", token=termux_token, apk=False, ready=True),
        },
        "jobs": {},
    }
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    registry = CoreWorkersRegistry(registry_path)
    created = registry.create_job(
        job_type="apk_build_debug",
        payload={"selfBuilderRequired": True},
        required_capabilities=["apk-builder"],
        ttl_seconds=300,
        lease_seconds=300,
        summary="bootstrap build",
    )
    assert created["job"]["preferred_worker_id"] == "termux-worker-1"
    polled = registry.poll_job({"worker_id": "termux-worker-1"}, token=termux_token)
    assert polled["job"]["type"] == "apk_build_debug"


def test_config_keeps_termux_only_as_bootstrap_builder() -> None:
    config = read(ROOT / "config.py")
    assert 'CORE_WORKER_APK_REPLACES_TERMUX = _parse_bool' in config
    assert 'CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED = _parse_bool' in config
    assert 'os.getenv("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", "true")' in config


def test_rootfs_and_bedrock_use_strict_real_gates() -> None:
    runner = read(JAVA / "CoreLinuxRunnerPreflightManager.java")
    bedrock = read(JAVA / "CoreWorkerBedrockService.java")
    runtime = read(JAVA / "CoreWorkerRuntimeService.java")
    assert 'new File(rootfsDir, "bin/sh").isFile()' in runner
    assert 'new File(rootfsDir, "lib/ld-linux-aarch64.so.1").isFile()' in runner
    assert 'lib/aarch64-linux-gnu/libc.so.6' in runner
    assert "boolean strictFailure" in runner
    assert "boolean bedrockRequirementsReady" in runner
    assert "private static final boolean BEDROCK_RUNTIME_ISOLATED = false" in bedrock
    assert 'if (!p.eulaAccepted) p.blockers.put' in bedrock
    assert 'status.put("bedrock_start_allowed", coreLinux.optBoolean("bedrockStartAllowed", false))' in runtime


def test_direct_http_secret_is_removed_when_connection_is_forgotten() -> None:
    activity = read(JAVA / "MainActivity.java")
    assert '.putString("direct_http_token"' in activity
    assert '.remove("direct_http_token")' in activity
