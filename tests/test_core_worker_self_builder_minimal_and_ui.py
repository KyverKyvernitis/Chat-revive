from __future__ import annotations

import ast
import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PHONE_WORKER_PATH = ROOT / "deploy/termux/phone-worker/phone_worker.py"
AUTOMATION_PATH = ROOT / "scripts/core-worker-automation.py"
WORKERS_PATH = ROOT / "utility/commands/workers.py"
ANDROID = ROOT / "android/core-worker-app"
JAVA = ANDROID / "app/src/main/java/dev/core/worker"


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

def _builder_bundle(path: Path, *, version: int) -> None:
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
        "bootstrapSmoke": {"ok": True},
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
        archive.writestr("jdk/bin/java", b"j" * (64 * 1024 + 1))
        archive.writestr("jdk/bin/javac", b"c" * (8 * 1024 + 1))
        archive.writestr("jdk/bin/jar", b"r" * (8 * 1024 + 1))
        archive.writestr("jdk/lib/jspawnhelper", b"s")
        archive.writestr("gradle/bin/gradle", b"g" * 101)
        archive.writestr("android-sdk/platforms/android-34/android.jar", b"a" * (1024 * 1024 + 1))
        archive.writestr("bin/aapt2", b"p" * (64 * 1024 + 1))


def test_bundle_validation_forces_regeneration_of_old_full_runtime(tmp_path: Path) -> None:
    module = _load("phone_worker_bundle_version_test", PHONE_WORKER_PATH)
    old = tmp_path / "old.zip"
    current = tmp_path / "current.zip"
    _builder_bundle(old, version=3)
    _builder_bundle(current, version=4)

    rejected = module._apk_self_builder_bundle_valid(old)
    accepted = module._apk_self_builder_bundle_valid(current)

    assert rejected["ok"] is False
    assert "bundle antigo" in rejected["error"]
    assert accepted["ok"] is True
    assert accepted["manifest"]["runtimeLibraries"]["strategy"] == "dt-needed-transitive-v1"


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

    assert 'versionCode 122' in gradle
    assert 'versionName "0.7.4"' in gradle
    assert 'builderHeroText = smallText("Autobuild: verificando toolchain local")' in activity
    assert '"✅ Autobuild pronto' in activity
    assert 'sectionTitle("Diagnóstico e manutenção")' in activity
    assert 'bottomNavButton("⚙  Core")' in activity
    assert 'runtime_libraries.get("strategy") == "dt-needed-transitive-v1"' in builder
    assert '"manifestVersion": manifest_version >= 4' in builder
    assert 'f"**Atualização:** {automation_label}"' in workers
    assert 'discord.ui.ActionRow(refresh, pairing, cleanup_jobs)' not in workers
