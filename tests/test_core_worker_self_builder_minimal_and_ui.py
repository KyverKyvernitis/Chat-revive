from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PHONE_WORKER_PATH = ROOT / "deploy/termux/phone-worker/phone_worker.py"
APK_IDENTITY_PATH = PHONE_WORKER_PATH.with_name("apk_identity.py")
AUTOMATION_PATH = ROOT / "scripts/core-worker-automation.py"
WORKERS_PATH = ROOT / "utility/commands/workers.py"
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"
APK_SELF_BUILDER_PATH = ANDROID / "app/src/main/python/coreworker/apk_self_builder.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, size: int, marker: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker * size)
    return path


def test_phone_worker_update_targets_preserve_nested_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("phone_worker_nested_update_target_test", PHONE_WORKER_PATH)
    monkeypatch.setattr(module, "_phone_worker_dir", lambda: tmp_path)

    target = "teto_renderer/renderer.py"
    assert module._normalize_worker_update_target(target) == target
    path, mode = module._safe_update_target_path(target)
    assert path == (tmp_path / target).resolve()
    assert mode == 0o644

    for invalid in ("../renderer.py", "/tmp/renderer.py", "teto_renderer/../renderer.py", "renderer.py"):
        with pytest.raises(ValueError):
            module._normalize_worker_update_target(invalid)


def test_phone_worker_release_is_immutable_and_registry_payload_is_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    automation = _load("core_worker_release_build_test", AUTOMATION_PATH)
    raw = b"print('updated')\n"
    source_hash = "a" * 64
    inline = {
        "version": "1.11.0", "source_hash": source_hash, "restart": True, "auto": True, "source": "test",
        "files": [{"target": "phone_worker.py", "mode": 0o755, "sha256": hashlib.sha256(raw).hexdigest(), "data_b64": base64.b64encode(raw).decode("ascii")}],
    }
    monkeypatch.setattr(automation, "AGENT_RELEASE_ROOT", tmp_path / "agent")
    monkeypatch.setattr(automation, "_public_base_url", lambda: "https://vps.invalid")
    artifact = automation._build_worker_update_artifact_payload(inline)
    artifact_again = automation._build_worker_update_artifact_payload(inline)
    archive = tmp_path / "agent/releases" / f"{source_hash}.zip"

    assert archive.is_file()
    assert artifact["update_transport"] == "bootstrap-manifest-v2"
    assert "files" not in artifact and "data_b64" not in json.dumps(artifact)
    assert artifact_again["release_sha256"] == artifact["release_sha256"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == artifact["release_sha256"]


def test_vps_and_phone_worker_compute_the_same_runtime_source_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    automation = _load("core_worker_source_hash_vps_test", AUTOMATION_PATH)
    phone_worker = _load("phone_worker_source_hash_phone_test", PHONE_WORKER_PATH)
    worker_dir = ROOT / "deploy/termux/phone-worker"
    monkeypatch.setattr(phone_worker, "_phone_worker_dir", lambda: worker_dir)

    assert set(name for name, _mode in automation.PHONE_WORKER_FILES) == set(phone_worker._WORKER_UPDATE_TARGETS)
    assert automation._hash_phone_worker_files(worker_dir) == phone_worker._phone_worker_source_hash()


def test_recovery_bootstrap_is_small_and_legacy_requires_manual_repair() -> None:
    automation = _load("core_worker_bootstrap_budget_test", AUTOMATION_PATH)
    bootstrap = ROOT / "deploy/termux/phone-worker/phone_worker_bootstrap.py"
    repair = ROOT / "deploy/termux/phone-worker/repair-phone-worker.sh"
    assert bootstrap.stat().st_size < 256 * 1024
    assert repair.is_file()
    source = AUTOMATION_PATH.read_text(encoding="utf-8")
    assert "bootstrap_required: execute repair-phone-worker.sh uma vez" in source
    assert "_build_legacy_worker_bootstrap_payload" not in source
    assert automation.PHONE_WORKER_BOOTSTRAP_MIN_VERSION == "1.0.0"


def test_bootstrap_core_starts_before_optional_runtime_files_arrive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_core = tmp_path / "phone_worker.py"
    bootstrap_core.write_bytes(PHONE_WORKER_PATH.read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PHONE_WORKER_ENV", str(tmp_path / "missing.env"))

    module = _load("phone_worker_bootstrap_core_only_test", bootstrap_core)
    original_import_module = module.importlib.import_module

    def import_without_preloaded_identity(name, *args, **kwargs):
        if name == "apk_identity":
            raise ModuleNotFoundError(name)
        return original_import_module(name, *args, **kwargs)

    monkeypatch.setattr(module.importlib, "import_module", import_without_preloaded_identity)

    assert module.PHONE_WORKER_VERSION == "1.11.0"
    assert module._APK_IDENTITY_MODULE is None
    assert bootstrap_core.stat().st_size > 512 * 1024
    assert (ROOT / "deploy/termux/phone-worker/phone_worker_bootstrap.py").stat().st_size < 256 * 1024
    with pytest.raises(RuntimeError, match="segundo estágio"):
        module.inspect_apk_identity(tmp_path / "missing.apk")


def test_apk_builder_respects_shared_heavy_resource_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("phone_worker_shared_heavy_lock_test", PHONE_WORKER_PATH)
    monkeypatch.setattr(module, "_current_core_worker_roles_and_capabilities", lambda: (["apk-builder"], ["apk-builder"]))
    monkeypatch.setenv("PHONE_WORKER_APK_BUILD_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_APK_BUILD_DIR", str(tmp_path / "builds"))

    assert module._HEAVY_RESOURCE_LOCK.acquire(blocking=False)
    try:
        result = module._apply_apk_build_debug({"source_zip_url": "https://example.invalid/source.zip"})
    finally:
        module._HEAVY_RESOURCE_LOCK.release()

    assert result["ok"] is False
    assert result["busy"] is True
    assert result["retryable"] is True
    assert "recurso pesado ocupado" in result["summary"]


def test_teto_request_is_first_but_falls_back_when_unavailable() -> None:
    module = _load("phone_worker_teto_engine_order_test", PHONE_WORKER_PATH)
    handler = object.__new__(module.WorkerHandler)
    payload = {"engine": "teto", "preferred_engine": "edge", "fallback_engine": "edge"}

    assert handler._tts_agent_engine_order(payload, ["teto", "edge", "gtts"]) == ["teto", "edge", "gtts"]
    assert handler._tts_agent_engine_order(payload, ["edge", "gtts"]) == ["edge", "gtts"]


def test_self_builder_collects_only_transitive_runtime_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("phone_worker_minimal_runtime_test", PHONE_WORKER_PATH)
    jdk = tmp_path / "jdk"
    prefix = tmp_path / "prefix"
    target = tmp_path / "bundle/runtime-libs"
    java = _write(jdk / "bin/java", 64)
    aapt2 = _write(prefix / "bin/aapt2", 64)
    libfoo = _write(prefix / "lib/libfoo.so", 101)
    libbar = _write(prefix / "lib/libbar.so", 203)
    libunused = _write(prefix / "lib/libunused-huge.so", 2 * 1024 * 1024)

    monkeypatch.setattr(module, "_find_elf_inspector", lambda env: ["readelf"])
    monkeypatch.setattr(module, "_required_jdk_elf_seeds", lambda home: [java])
    monkeypatch.setattr(module, "_elf_header", lambda path: {"ok": True, "aarch64": True})

    def fake_index(roots, **kwargs):
        if roots == [jdk]:
            return {}
        return {
            "libfoo.so": [libfoo],
            "libbar.so": [libbar],
            "libunused-huge.so": [libunused],
        }

    needed = {
        "java": ["libfoo.so", "libc.so"],
        "aapt2": ["libbar.so", "liblog.so"],
        "libfoo.so": ["libbar.so"],
        "libbar.so": [],
    }
    monkeypatch.setattr(module, "_elf_index", fake_index)
    monkeypatch.setattr(
        module,
        "_read_elf_dynamic",
        lambda path, inspector: {"ok": True, "needed": needed.get(path.name, []), "soname": path.name},
    )

    result = module._collect_minimal_termux_runtime_libraries(
        jdk_home=jdk,
        aapt2_path=aapt2,
        prefix=prefix,
        target=target,
        env={},
    )

    assert result["strategy"] == "dt-needed-transitive-v1"
    assert result["names"] == ["libbar.so", "libfoo.so"]
    assert result["bytes"] == 304
    assert (target / "libfoo.so").read_bytes() == libfoo.read_bytes()
    assert (target / "libbar.so").read_bytes() == libbar.read_bytes()
    assert not (target / "libunused-huge.so").exists()
    assert result["systemProvided"] == ["libc.so", "liblog.so"]


def test_gradle_launcher_is_normalized_for_android_system_shell(tmp_path: Path) -> None:
    module = _load("phone_worker_gradle_launcher_android_test", PHONE_WORKER_PATH)
    launcher = tmp_path / "gradle/bin/gradle"
    launcher.parent.mkdir(parents=True)
    agent = _write(tmp_path / "gradle/lib/agents/gradle-instrumentation-agent-9.6.1.jar", 128)
    launcher.write_text(
        "#!/usr/bin/env sh\n"
        "APP_HOME=$(CDPATH= cd \"${0%/*}/..\" && pwd -P)\n"
        "DEFAULT_JVM_OPTS='-Dfile.encoding=UTF-8 \"-Xmx64m\" \"-Xms64m\" \"-javaagent:$APP_HOME/lib/agents/gradle-instrumentation-agent-9.6.1.jar\"'\n"
        "exec \"$JAVA_HOME/bin/java\" $DEFAULT_JVM_OPTS org.gradle.launcher.GradleMain \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    first = module._patch_gradle_launcher_for_android(launcher)
    second = module._patch_gradle_launcher_for_android(launcher)
    text = launcher.read_text(encoding="utf-8")

    assert 'DEFAULT_JVM_OPTS="-Dfile.encoding=UTF-8 -Xmx64m -Xms64m -javaagent:${APP_HOME}/lib/agents/gradle-instrumentation-agent-9.6.1.jar"' in text
    assert '"-Xmx64m"' not in text
    assert first["strategy"] == "android-sh-resolved-app-home-jvm-opts-v2"
    assert first["defaultJvmOpts"] == [
        "-Dfile.encoding=UTF-8",
        "-Xmx64m",
        "-Xms64m",
        "-javaagent:${APP_HOME}/lib/agents/gradle-instrumentation-agent-9.6.1.jar",
    ]
    assert first["resolvedAppHome"] is True
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["sha256"] == second["sha256"]
    assert launcher.stat().st_mode & 0o111
    assert agent.is_file()

    java = tmp_path / "jdk/bin/java"
    java.parent.mkdir(parents=True)
    java.write_text("#!/usr/bin/env sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    java.chmod(0o755)
    completed = subprocess.run(
        [str(launcher), "--version"],
        env={**os.environ, "JAVA_HOME": str(java.parent.parent)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.splitlines()[:4] == [
        "-Dfile.encoding=UTF-8",
        "-Xmx64m",
        "-Xms64m",
        f"-javaagent:{agent}",
    ]


def test_gradle_launcher_rejects_unknown_shell_expansion(tmp_path: Path) -> None:
    module = _load("phone_worker_gradle_launcher_injection_test", PHONE_WORKER_PATH)
    launcher = tmp_path / "gradle/bin/gradle"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/usr/bin/env sh\n"
        "APP_HOME=/safe/path\n"
        "DEFAULT_JVM_OPTS='\"-Xmx64m\" \"-Duser.home=$HOME\"'\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    with pytest.raises(ValueError, match="não portátil"):
        module._patch_gradle_launcher_for_android(launcher)



def test_python_elf_parser_works_without_termux_readelf(tmp_path: Path) -> None:
    module = _load("phone_worker_python_elf_parser_test", PHONE_WORKER_PATH)
    source = next((Path(candidate) for candidate in ("/bin/ls", "/usr/bin/env", "/bin/sh") if Path(candidate).is_file()), None)
    if source is None:
        pytest.skip("nenhum ELF dinâmico disponível no ambiente de teste")
    target = tmp_path / "aarch64-fixture"
    raw = bytearray(source.read_bytes())
    if raw[:4] != b"\x7fELF" or len(raw) < 64 or raw[4] != 2:
        pytest.skip("fixture não é ELF64")
    endian = "little" if raw[5] == 1 else "big"
    raw[18:20] = int(183).to_bytes(2, endian)
    target.write_bytes(raw)

    result = module._read_elf_dynamic(target, [])

    assert result["ok"] is True
    assert result["inspector"] == "python-elf64-dynamic-v1"
    assert result["needed"]
    assert all("/" not in name for name in result["needed"])

def _builder_bundle(
    path: Path,
    *,
    version: int,
    smoke_names: tuple[str, ...] = ("java", "javac", "jar", "gradle", "aapt2"),
) -> None:
    executable_paths = [
        "jdk/bin/java",
        "jdk/bin/javac",
        "jdk/bin/jar",
        "jdk/lib/jspawnhelper",
        "gradle/bin/gradle",
        "bin/aapt2",
    ]
    manifest = {
        "schema": "core-worker-android-builder-v1",
        "version": version,
        "arch": "aarch64",
        "runtimeLibraries": {"strategy": "dt-needed-transitive-v1"},
        "gradleLauncher": {"strategy": "android-sh-resolved-app-home-jvm-opts-v2"},
        "validation": {"strategy": "required-executable-smoke-v2"},
        "bootstrapSmoke": {
            "ok": True,
            "checks": [{"name": name, "ok": True, "returncode": 0} for name in smoke_names],
        },
        "paths": {
            "jdk": "jdk",
            "gradle": "gradle/bin/gradle",
            "androidSdk": "android-sdk",
            "aapt2": "bin/aapt2",
        },
        "executablePaths": executable_paths,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("jdk/bin/java", b"j")
        archive.writestr("jdk/bin/javac", b"c")
        archive.writestr("jdk/bin/jar", b"r")
        archive.writestr("jdk/lib/jspawnhelper", b"s")
        archive.writestr("gradle/bin/gradle", b"g")
        archive.writestr("android-sdk/platforms/android-34/android.jar", b"a" * (1024 * 1024 + 1))
        archive.writestr("bin/aapt2", b"p")


def test_bundle_validation_forces_regeneration_of_old_full_runtime(tmp_path: Path) -> None:
    module = _load("phone_worker_bundle_version_test", PHONE_WORKER_PATH)
    old = tmp_path / "old.zip"
    current = tmp_path / "current.zip"
    _builder_bundle(old, version=6)
    _builder_bundle(current, version=7)

    rejected = module._apk_self_builder_bundle_valid(old)
    accepted = module._apk_self_builder_bundle_valid(current)

    assert rejected["ok"] is False
    assert "bundle antigo" in rejected["error"]
    assert accepted["ok"] is True
    assert accepted["manifest"]["runtimeLibraries"]["strategy"] == "dt-needed-transitive-v1"


def test_bundle_validation_requires_execution_result_for_every_tool(tmp_path: Path) -> None:
    module = _load("phone_worker_bundle_smoke_test", PHONE_WORKER_PATH)
    incomplete = tmp_path / "incomplete.zip"
    _builder_bundle(incomplete, version=7, smoke_names=("java", "javac", "gradle", "aapt2"))

    result = module._apk_self_builder_bundle_valid(incomplete)

    assert result["ok"] is False
    assert "smoke obrigatório ausente ou reprovado: jar" in result["error"]


def test_apk_accepts_compact_launchers_after_verified_bootstrap_smoke(tmp_path: Path) -> None:
    archive_path = tmp_path / "toolchain.zip"
    toolchain = tmp_path / "toolchain"
    _builder_bundle(archive_path, version=7)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(toolchain)
    manifest = json.loads((toolchain / "manifest.json").read_text(encoding="utf-8"))
    for name in manifest["executablePaths"]:
        (toolchain / name).chmod(0o700)

    python_root = APK_SELF_BUILDER_PATH.parents[1]
    sys.path.insert(0, str(python_root))
    try:
        module = _load("apk_self_builder_compact_launcher_test", APK_SELF_BUILDER_PATH)
    finally:
        sys.path.remove(str(python_root))
    resolved = module._resolve_toolchain(toolchain)

    assert resolved["ok"] is True
    assert resolved["checks"]["bootstrapSmoke"] is True
    assert resolved["checks"]["validation"] is True
    assert (toolchain / "jdk/bin/java").stat().st_size == 1


def test_toolchain_is_split_and_verified_without_one_giant_gradle_asset(tmp_path: Path) -> None:
    archive_path = tmp_path / "android-builder-toolchain.zip"
    project = tmp_path / "project"
    _builder_bundle(archive_path, version=7)
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("padding.bin", b"z" * (5 * 1024 * 1024))
    expected_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    module = _load("apk_identity_chunk_assets_test", APK_IDENTITY_PATH)

    published = module.publish_toolchain_chunk_assets(
        archive_path,
        project,
        chunk_size=4 * 1024 * 1024,
    )

    asset_dir = project / "app/src/main/assets/core-linux/android-builder"
    descriptor = json.loads((asset_dir / "android-builder-toolchain.parts.json").read_text(encoding="utf-8"))
    parts = [asset_dir / item["name"] for item in descriptor["parts"]]
    assert published["ok"] is True
    assert published["transport"] == "chunked-assets-v1"
    assert len(parts) >= 2
    assert all(0 < part.stat().st_size <= 4 * 1024 * 1024 for part in parts)
    assert not (asset_dir / "android-builder-toolchain.zip").exists()
    assert hashlib.sha256(b"".join(part.read_bytes() for part in parts)).hexdigest() == expected_sha

    with parts[0].open("r+b") as handle:
        first = handle.read(1)
        handle.seek(0)
        handle.write(bytes([first[0] ^ 0x01]))
    rejected = module.validate_toolchain_chunk_assets(project)
    assert rejected["ok"] is False
    assert "sha256 divergente" in rejected["error"]


def test_gradle_failure_summary_prefers_root_memory_error(tmp_path: Path) -> None:
    module = _load("phone_worker_gradle_root_cause_test", PHONE_WORKER_PATH)
    log = tmp_path / "gradle.log"
    log.write_text(
        "Execution failed for task ':app:compressDebugAssets'.\n"
        "> A failure occurred while executing CompressAssetsWorkAction\n"
        "Caused by: java.lang.OutOfMemoryError: Java heap space\n",
        encoding="utf-8",
    )

    result = module._summarize_gradle_log(log)

    assert "OutOfMemoryError" in result["summary"]
    assert "compressDebugAssets" in result["detail"]


def test_automation_reconciles_failed_job_instead_of_leaving_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("core_worker_automation_reconcile_test", AUTOMATION_PATH)
    pending = {
        "apk_build": {
            "pending": True,
            "ok": True,
            "versionName": "0.7.3",
            "versionCode": 121,
            "last_job_id": "job-failed",
        }
    }
    failed_job = {
        "job_id": "job-failed",
        "type": "apk_build_debug",
        "status": "failed",
        "updated_at": 1,
        "summary": "preparação do autobuilder falhou",
        "result": {
            "ok": False,
            "stage": "self_builder_toolchain_prepare",
            "retryable": False,
            "permanent_failure": True,
            "error": "dependência ELF obrigatória ausente",
        },
    }
    monkeypatch.setattr(module, "_registry_job_by_id", lambda job_id: failed_job)
    monkeypatch.setattr(module, "_apk_build_job_matches_source", lambda *args: True)

    item = module._reconcile_apk_build_pending_job(
        pending,
        version_name="0.7.3",
        version_code=121,
        source_fingerprint="source",
    )

    assert item["pending"] is False
    assert item["phase"] == "failed"
    assert item["permanent_failure"] is True
    assert item["blocked_by_recent_failure"] is True
    assert "autobuilder" in item["last_failure_detail"]


def test_discord_panel_deduplicates_apk_state_and_prioritizes_failure() -> None:
    tree = ast.parse(WORKERS_PATH.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_dedupe_automation_parts")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(WORKERS_PATH), "exec"), namespace)
    values = namespace["_dedupe_automation_parts"]([
        "APK: build pendente (0.7.3)",
        "APK: build pendente (0.7.3; VPS ainda em 0.7.2)",
        "APK: 0.7.3 falhou · correção necessária · dependência ELF ausente",
        "Push: ativo · enviado",
    ])
    assert values == [
        "APK: 0.7.3 falhou · correção necessária · dependência ELF ausente",
        "Push: ativo · enviado",
    ]


def test_ui_and_versions_expose_builder_state_without_vps_gradle() -> None:
    activity = (JAVA / "MainActivity.java").read_text(encoding="utf-8")
    builder = (ANDROID / "app/src/main/python/coreworker/apk_self_builder.py").read_text(encoding="utf-8")
    gradle = (ANDROID / "app/build.gradle").read_text(encoding="utf-8")
    workers = WORKERS_PATH.read_text(encoding="utf-8")

    assert 'versionCode 127' in gradle
    assert 'versionName "0.8.0"' in gradle
    assert 'builderHeroText = smallText("Autobuild: verificando toolchain local")' in activity
    assert '"✅ Autobuild pronto' in activity
    assert 'sectionTitle("Diagnóstico e manutenção")' in activity
    assert 'bottomNavButton("⚙  Core")' in activity
    assert 'runtime_libraries.get("strategy") == "dt-needed-transitive-v1"' in builder
    assert '"manifestVersion": (legacy_v1 and manifest_version >= 7) or (external_v2 and manifest_version >= 2)' in builder
    assert 'gradle_launcher.get("strategy") == "android-sh-resolved-app-home-jvm-opts-v2"' in builder
    assert 'validation.get("strategy") == "required-executable-smoke-v2"' in builder
    assert 'verifyCoreWorkerNoEmbeddedToolchain' in gradle
    assert 'android-builder-toolchain.zip' in gradle
    assert '".cwpart"' in gradle
    assert 'f"**Atualização:** {automation_label}"' in workers
    assert 'discord.ui.ActionRow(refresh, pairing, cleanup_jobs)' not in workers
