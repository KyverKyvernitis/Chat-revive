"""Builder autocontido do Core Worker APK.

Executa somente jobs allowlist de build/publicação. O primeiro APK é compilado
no Termux sem toolchain gigante nos assets. Depois da instalação, o APK baixa e
valida um toolchain Bionic externo, retém o último slot saudável e executa o
Gradle diretamente no armazenamento privado. A VPS entrega fonte/artefatos e
recebe o APK pronto; nunca executa Gradle, JDK ou Android SDK.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from coreworker.apk_identity import assert_expected_apk_identity, inspect_apk_identity

SCHEMA = "core-worker-apk-self-builder-v1"
TOOLCHAIN_SCHEMA_V1 = "core-worker-android-builder-v1"
TOOLCHAIN_SCHEMA_V2 = "core-worker-android-builder-v2"
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_ENTRIES = 16000
MAX_SOURCE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_APK_BYTES = 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 3 * 60 * 60


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _safe_json_load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _safe_rel(raw: Any, fallback: str = "") -> str:
    value = str(raw or fallback).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("caminho relativo inválido")
    return "/".join(parts)


def _safe_filename(raw: Any, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or fallback)).strip("-._")
    return (value or fallback)[:160]


def _same_origin(url: str, server_url: str) -> bool:
    left = urllib.parse.urlsplit(url)
    right = urllib.parse.urlsplit(server_url)
    if left.scheme not in {"http", "https"} or right.scheme not in {"http", "https"}:
        return False
    left_port = left.port or (443 if left.scheme == "https" else 80)
    right_port = right.port or (443 if right.scheme == "https" else 80)
    return left.scheme == right.scheme and (left.hostname or "").lower() == (right.hostname or "").lower() and left_port == right_port


def _resolve_toolchain(toolchain_dir: Path) -> dict[str, Any]:
    manifest_path = toolchain_dir / "manifest.json"
    manifest = _safe_json_load(manifest_path)
    schema = str(manifest.get("schema") or "").strip()
    arch = str(manifest.get("arch") or "").strip().lower()
    try:
        manifest_version = int(manifest.get("version") or 0)
    except Exception:
        manifest_version = 0
    runtime_libraries = manifest.get("runtimeLibraries") if isinstance(manifest.get("runtimeLibraries"), dict) else {}
    gradle_launcher = manifest.get("gradleLauncher") if isinstance(manifest.get("gradleLauncher"), dict) else {}
    bootstrap_smoke = manifest.get("bootstrapSmoke") if isinstance(manifest.get("bootstrapSmoke"), dict) else {}
    validation = manifest.get("validation") if isinstance(manifest.get("validation"), dict) else {}
    versions = manifest.get("versions") if isinstance(manifest.get("versions"), dict) else {}
    raw_executables = manifest.get("executablePaths") if isinstance(manifest.get("executablePaths"), list) else []

    paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    jdk_rel = _safe_rel(paths.get("jdk") or "jdk")
    gradle_rel = _safe_rel(paths.get("gradle") or "gradle/bin/gradle")
    sdk_rel = _safe_rel(paths.get("androidSdk") or paths.get("android_sdk") or "android-sdk")
    aapt2_rel = _safe_rel(paths.get("aapt2") or "bin/aapt2")

    jdk = toolchain_dir / jdk_rel
    java = jdk / "bin/java"
    javac = jdk / "bin/javac"
    jar = jdk / "bin/jar"
    gradle = toolchain_dir / gradle_rel
    sdk = toolchain_dir / sdk_rel
    aapt2 = toolchain_dir / aapt2_rel
    android_jar = sdk / "platforms/android-34/android.jar"
    try:
        executable_paths = {_safe_rel(item) for item in raw_executables}
    except Exception:
        executable_paths = set()
    mandatory_executables = {
        f"{jdk_rel}/bin/java",
        f"{jdk_rel}/bin/javac",
        f"{jdk_rel}/bin/jar",
        gradle_rel,
        aapt2_rel,
    }
    jspawn_rel = f"{jdk_rel}/lib/jspawnhelper"
    declared_executables_valid = (
        bool(raw_executables)
        and len(raw_executables) == len(executable_paths)
        and mandatory_executables.issubset(executable_paths)
        and all((toolchain_dir / item).is_file() for item in executable_paths)
        and (not (toolchain_dir / jspawn_rel).is_file() or jspawn_rel in executable_paths)
    )
    smoke_checks = bootstrap_smoke.get("checks") if isinstance(bootstrap_smoke.get("checks"), list) else []
    smoke_by_name = {
        str(item.get("name") or ""): item
        for item in smoke_checks
        if isinstance(item, dict) and item.get("name")
    }
    bootstrap_smoke_valid = bootstrap_smoke.get("ok") is True
    for name in ("java", "javac", "jar", "gradle", "aapt2"):
        check = smoke_by_name.get(name) or {}
        try:
            returncode_ok = check.get("returncode") is not None and int(check.get("returncode")) == 0
        except (TypeError, ValueError):
            returncode_ok = False
        bootstrap_smoke_valid = bootstrap_smoke_valid and check.get("ok") is True and returncode_ok

    legacy_v1 = schema == TOOLCHAIN_SCHEMA_V1
    external_v2 = schema == TOOLCHAIN_SCHEMA_V2
    required_smoke = set(validation.get("requiredSmokeChecks") or [])
    exact_v2_versions = (
        int(versions.get("jdkMajor") or 0) == 17
        and str(versions.get("gradle") or "") == "8.9"
        and str(versions.get("agp") or "") == "8.7.3"
        and int(versions.get("compileSdk") or 0) == 34
        and str(versions.get("buildTools") or "") == "34.0.0"
        and str(versions.get("chaquopy") or "") == "17.0.0"
    )
    checks = {
        "manifest": manifest_path.is_file(),
        "schema": legacy_v1 or external_v2,
        "manifestVersion": (legacy_v1 and manifest_version >= 7) or (external_v2 and manifest_version >= 2),
        "versions": (not external_v2) or exact_v2_versions,
        "executablePaths": declared_executables_valid,
        "runtimeLibraries": runtime_libraries.get("strategy") == "dt-needed-transitive-v1",
        "gradleLauncher": (not legacy_v1) or gradle_launcher.get("strategy") == "android-sh-resolved-app-home-jvm-opts-v2",
        "validation": validation.get("strategy") == "required-executable-smoke-v2"
            and ((not external_v2) or required_smoke == {"java", "javac", "jar", "gradle", "aapt2"}),
        "bootstrapSmoke": bootstrap_smoke_valid,
        "arch": arch in {"aarch64", "arm64", "arm64-v8a"},
        "java": java.is_file() and java.stat().st_size > 0 and os.access(java, os.X_OK),
        "javac": javac.is_file() and javac.stat().st_size > 0 and os.access(javac, os.X_OK),
        "jar": jar.is_file() and jar.stat().st_size > 0 and os.access(jar, os.X_OK),
        "gradle": gradle.is_file() and gradle.stat().st_size > 0 and os.access(gradle, os.X_OK),
        "androidSdk": sdk.is_dir(),
        "androidJar34": android_jar.is_file() and android_jar.stat().st_size > 1024 * 1024,
        "aapt2": aapt2.is_file() and aapt2.stat().st_size > 0 and os.access(aapt2, os.X_OK),
        "jspawnhelper": not (toolchain_dir / jspawn_rel).is_file() or os.access(toolchain_dir / jspawn_rel, os.X_OK),
    }
    missing = [key for key, ok in checks.items() if not ok]
    return {
        "ok": not missing,
        "schema": schema,
        "arch": arch,
        "manifest": str(manifest_path),
        "checks": checks,
        "missing": missing,
        "paths": {
            "toolchain": str(toolchain_dir),
            "jdk": str(jdk),
            "java": str(java),
            "javac": str(javac),
            "jar": str(jar),
            "gradle": str(gradle),
            "androidSdk": str(sdk),
            "aapt2": str(aapt2),
            "androidJar34": str(android_jar),
        },
        "manifestData": manifest,
    }


def _toolchain_fingerprint(tool: dict[str, Any]) -> str:
    """Fingerprint leve para invalidar smoke antigo sem reler o bundle inteiro."""
    candidates = [
        Path(str(tool.get("manifest") or "")),
        Path(str((tool.get("paths") or {}).get("java") or "")),
        Path(str((tool.get("paths") or {}).get("javac") or "")),
        Path(str((tool.get("paths") or {}).get("jar") or "")),
        Path(str((tool.get("paths") or {}).get("gradle") or "")),
        Path(str((tool.get("paths") or {}).get("androidJar34") or "")),
        Path(str((tool.get("paths") or {}).get("aapt2") or "")),
    ]
    digest = hashlib.sha256()
    for path in candidates:
        try:
            stat = path.stat()
            digest.update(str(path).encode("utf-8", errors="replace"))
            digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("ascii"))
            if path.name == "manifest.json" and stat.st_size <= 1024 * 1024:
                digest.update(_sha256_file(path).encode("ascii"))
        except Exception:
            digest.update((str(path) + "\0missing\n").encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _toolchain_environment(
    tool: dict[str, Any],
    *,
    home: Path,
    temp: Path,
    gradle_home: Path,
    clean: bool,
) -> dict[str, str]:
    paths = tool["paths"]
    jdk = Path(paths["jdk"])
    sdk = Path(paths["androidSdk"])
    toolchain = Path(paths["toolchain"])
    runtime_libs = toolchain / "runtime-libs"
    library_paths = [
        runtime_libs,
        jdk / "lib",
        jdk / "lib/server",
        jdk / "lib/jli",
    ]
    env = {} if clean else os.environ.copy()
    existing_library_path = "" if clean else str(env.get("LD_LIBRARY_PATH") or "").strip()
    resolved_library_paths = [str(path) for path in library_paths if path.is_dir()]
    if existing_library_path:
        resolved_library_paths.append(existing_library_path)
    env.update({
        "HOME": str(home),
        "TMPDIR": str(temp),
        "GRADLE_USER_HOME": str(gradle_home),
        "JAVA_HOME": str(jdk),
        "ANDROID_HOME": str(sdk),
        "ANDROID_SDK_ROOT": str(sdk),
        "PATH": os.pathsep.join((
            str(jdk / "bin"),
            str(sdk / "platform-tools"),
            str(sdk / "cmdline-tools/latest/bin"),
            "/system/bin",
            "/system/xbin",
        )),
        "LD_LIBRARY_PATH": os.pathsep.join(resolved_library_paths),
        "LANG": "C",
        "LC_ALL": "C",
    })
    return env


def _run_smoke_command(name: str, command: list[str], env: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = _short(completed.stdout, 6000)
        return {
            "name": name,
            "ok": completed.returncode == 0,
            "returncode": int(completed.returncode),
            "durationMs": int((time.time() - started) * 1000),
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        return {
            "name": name,
            "ok": False,
            "returncode": 124,
            "durationMs": int((time.time() - started) * 1000),
            "output": _short(output, 6000),
            "error": f"timeout após {timeout}s",
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "returncode": -1,
            "durationMs": int((time.time() - started) * 1000),
            "output": "",
            "error": f"{type(exc).__name__}: {_short(exc, 600)}",
        }


def _toolchain_smoke(files: Path, tool: dict[str, Any], *, force: bool) -> dict[str, Any]:
    builder = files / "apk-self-builder"
    state_path = builder / "toolchain-smoke.json"
    fingerprint = _toolchain_fingerprint(tool)
    cached = _safe_json_load(state_path)
    if (
        not force
        and cached.get("fingerprint") == fingerprint
        and cached.get("schema") == "core-worker-apk-self-builder-smoke-v3"
    ):
        return cached

    runtime = builder / "runtime/smoke"
    home = runtime / "home"
    temp = runtime / "tmp"
    gradle_home = runtime / "gradle-home"
    shutil.rmtree(runtime, ignore_errors=True)
    for path in (home, temp, gradle_home):
        path.mkdir(parents=True, exist_ok=True)
    env = _toolchain_environment(tool, home=home, temp=temp, gradle_home=gradle_home, clean=True)
    paths = tool["paths"]
    commands = [
        ("java", [paths["java"], "-version"], 45),
        ("javac", [paths["javac"], "-version"], 45),
        ("jar", [paths["jar"], "--version"], 45),
        ("gradle", ["/system/bin/sh", paths["gradle"], "--version", "--no-daemon"], 90),
        ("aapt2", [paths["aapt2"], "version"], 45),
    ]
    checks: list[dict[str, Any]] = []
    try:
        for name, command, timeout in commands:
            result = _run_smoke_command(name, command, env, timeout)
            checks.append(result)
            if not result.get("ok"):
                break
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    ok = len(checks) == len(commands) and all(bool(item.get("ok")) for item in checks)
    result = {
        "schema": "core-worker-apk-self-builder-smoke-v3",
        "ok": ok,
        "state": "toolchain_smoke_ok" if ok else "toolchain_smoke_failed",
        "summary": "Java, Javac, Jar, Gradle e aapt2 executaram no APK" if ok else "toolchain não executou no ambiente privado do APK",
        "fingerprint": fingerprint,
        "checks": checks,
        "updatedAt": _now_ms(),
    }
    _atomic_json(state_path, result)
    return result


def preflight(files_dir: str, native_dir: str, run_smoke: bool = False) -> str:
    del native_dir  # assinatura mantida para compatibilidade Java; builder não depende do rootfs/PRoot.
    files = Path(files_dir)
    builder = files / "apk-self-builder"
    toolchain = builder / "toolchain"
    tool = _resolve_toolchain(toolchain)
    checks = {
        "toolchain": bool(tool.get("ok")),
        "systemShell": Path("/system/bin/sh").is_file(),
    }
    basic_missing = [key for key, ok in checks.items() if not ok]
    smoke = {
        "ok": False,
        "state": "toolchain_smoke_blocked" if basic_missing else "toolchain_smoke_pending",
        "summary": "smoke bloqueado por preflight básico" if basic_missing else "smoke real ainda não executado",
        "checks": [],
    }
    if not basic_missing:
        smoke = _toolchain_smoke(files, tool, force=bool(run_smoke))
    checks["toolchainSmoke"] = bool(smoke.get("ok"))
    missing = list(basic_missing)
    if not smoke.get("ok") and "toolchainSmoke" not in missing:
        missing.append("toolchainSmoke")

    latest = _safe_json_load(builder / "artifacts/latest-artifact.json")
    latest_path = Path(str(latest.get("artifact_path") or "")) if latest else Path()
    publish_ready = bool(
        latest
        and latest_path.is_file()
        and _is_inside(latest_path, builder)
        and latest_path.stat().st_size > 1024 * 1024
    )
    ready = not missing
    out = {
        "ok": ready,
        "ready": ready,
        "publishReady": publish_ready,
        "schema": SCHEMA,
        "runtime": "android-private-toolchain-direct",
        "state": "apk_self_builder_ready" if ready else "apk_self_builder_blocked",
        "summary": "Autobuild do APK pronto e executável" if ready else "Autobuild do APK aguardando: " + ", ".join(missing),
        "checks": checks,
        "missing": missing,
        "toolchain": tool,
        "smoke": smoke,
        "paths": {
            "builder": str(builder),
            "toolchain": str(toolchain),
        },
        "latestArtifact": {
            "available": publish_ready,
            "filename": latest.get("filename", "") if latest else "",
            "versionName": latest.get("versionName", "") if latest else "",
            "versionCode": latest.get("versionCode", 0) if latest else 0,
        },
        "updatedAt": _now_ms(),
    }
    _atomic_json(builder / "state.json", out)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _download_source(url: str, target: Path, expected_sha: str, expected_bytes: int, server_url: str) -> dict[str, Any]:
    if not _same_origin(url, server_url):
        raise ValueError("source_zip_url precisa apontar para a mesma origem autenticada da VPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "CoreWorkerApkSelfBuilder/1"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_SOURCE_BYTES:
                raise ValueError("source zip excede o limite do autobuilder")
            digest.update(block)
            output.write(block)
    actual = digest.hexdigest()
    if expected_sha and actual.lower() != expected_sha.lower():
        raise ValueError("sha256 do source zip divergente")
    if expected_bytes > 0 and total != expected_bytes:
        raise ValueError(f"tamanho do source zip divergente: esperado {expected_bytes}, recebido {total}")
    return {"url": url, "bytes": total, "sha256": actual}


def _safe_extract_zip(source: Path, target: Path) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    count = 0
    expanded = 0
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            count += 1
            if count > MAX_SOURCE_ENTRIES:
                raise ValueError("source zip contém arquivos demais")
            name = str(info.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("source zip contém caminho inseguro")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("source zip contém link simbólico")
            expanded += max(0, int(info.file_size or 0))
            if expanded > MAX_SOURCE_EXPANDED_BYTES:
                raise ValueError("source zip excede limite expandido")
            destination = (root / name).resolve()
            if not _is_inside(destination, root):
                raise ValueError("source zip tenta sair do workspace")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    return {"files": count, "expandedBytes": expanded}


def _find_project(source_root: Path, project_subdir: str) -> Path:
    rel = _safe_rel(project_subdir or "android/core-worker-app")
    direct = source_root / rel
    if (direct / "app/build.gradle").is_file():
        return direct
    children = [item for item in source_root.iterdir() if item.is_dir()]
    for child in children[:20]:
        nested = child / rel
        if (nested / "app/build.gradle").is_file():
            return nested
    candidates = list(source_root.glob("**/app/build.gradle"))
    candidates = [path.parent.parent for path in candidates if len(path.relative_to(source_root).parts) <= 8]
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError("projeto android/core-worker-app não encontrado no source zip")


def _decode_b64(payload: dict[str, Any], names: tuple[str, ...], max_bytes: int, label: str) -> bytes:
    raw = next((str(payload.get(name) or "").strip() for name in names if str(payload.get(name) or "").strip()), "")
    if not raw:
        raise FileNotFoundError(f"{label} ausente no payload autenticado")
    try:
        data = base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"{label} base64 inválido: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise ValueError(f"{label} excede o limite")
    return data


def _inject_private_files(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    google = _decode_b64(payload, ("googleServicesJsonB64", "google_services_json_b64"), 512 * 1024, "google-services.json")
    expected_google = str(payload.get("googleServicesSha256") or payload.get("google_services_sha256") or "").lower().strip()
    google_sha = hashlib.sha256(google).hexdigest()
    if expected_google and expected_google != google_sha:
        raise ValueError("sha256 do google-services.json divergente")
    parsed = json.loads(google.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("google-services.json inválido")
    package = str(payload.get("googleServicesPackage") or "dev.core.worker")
    clients = parsed.get("client") if isinstance(parsed.get("client"), list) else []
    matching = []
    for client in clients:
        if not isinstance(client, dict):
            continue
        info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
        if str(android.get("package_name") or "") == package:
            matching.append(client)
    if not matching:
        raise ValueError("google-services.json não contém o package do Core Worker")
    google_path = project / "app/google-services.json"
    google_path.parent.mkdir(parents=True, exist_ok=True)
    google_path.write_bytes(google)

    keystore = _decode_b64(payload, ("apkSigningKeystoreB64", "apk_signing_keystore_b64"), 1024 * 1024, "keystore compatível")
    expected_key = str(payload.get("apkSigningKeystoreSha256") or payload.get("apk_signing_keystore_sha256") or "").lower().strip()
    key_sha = hashlib.sha256(keystore).hexdigest()
    if expected_key and expected_key != key_sha:
        raise ValueError("sha256 da keystore divergente")
    alias = str(payload.get("apkSigningKeyAlias") or payload.get("apk_signing_key_alias") or "androiddebugkey").strip()
    store_password = str(payload.get("apkSigningStorePassword") or payload.get("apk_signing_store_password") or "").strip()
    key_password = str(payload.get("apkSigningKeyPassword") or payload.get("apk_signing_key_password") or store_password).strip()
    if not alias or not store_password:
        raise ValueError("alias/senha da assinatura compatível ausentes")
    key_path = project / "app/core-worker-upload.keystore"
    props_path = project / "app/core-worker-signing.properties"
    key_path.write_bytes(keystore)
    os.chmod(key_path, 0o600)
    props_path.write_text(
        "\n".join((
            "CORE_WORKER_SIGNING_KEYSTORE=core-worker-upload.keystore",
            f"CORE_WORKER_SIGNING_KEY_ALIAS={alias}",
            f"CORE_WORKER_SIGNING_STORE_PASSWORD={store_password}",
            f"CORE_WORKER_SIGNING_KEY_PASSWORD={key_password or store_password}",
            "",
        )),
        encoding="utf-8",
    )
    os.chmod(props_path, 0o600)
    return {
        "googleServicesSha256": google_sha,
        "signingKeystoreSha256": key_sha,
        "signingMode": str(payload.get("apkSigningMode") or "compat-vps-debug-keystore")[:80],
    }



def _read_meminfo_bytes() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            match = re.search(r"(\d+)", rest)
            if match:
                result[key] = int(match.group(1)) * 1024
    except Exception:
        pass
    return result


def _tree_bytes(root: Path, *, entry_limit: int = 80_000) -> int:
    total = 0
    count = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            count += 1
            if count > entry_limit:
                break
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
    except Exception:
        pass
    return total


def _active_heavy_build_processes() -> list[dict[str, Any]]:
    current = os.getpid()
    found: list[dict[str, Any]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    needles = ("org.gradle.launcher", "gradledaemon", "gradleworker", "aapt2")
    for item in proc.iterdir():
        if not item.name.isdigit() or int(item.name) == current:
            continue
        try:
            raw = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        lower = text.lower()
        if text and any(needle in lower for needle in needles):
            found.append({"pid": int(item.name), "cmd": _short(text, 220)})
            if len(found) >= 8:
                break
    return found


def _effective_gradle_heap_mb(available_bytes: int) -> int:
    available_mb = max(0, int(available_bytes // (1024 * 1024)))
    # Reserva memória para Android/Chaquopy/aapt2/filesystem. Sem configuração
    # global: cada build calcula o heap a partir do estado atual do aparelho.
    if available_mb <= 0:
        return 512
    usable = max(256, available_mb - max(384, int(available_mb * 0.18)))
    return max(384, min(1280, int(usable * 0.55)))


def _resource_preflight(
    files: Path,
    project: Path | None,
    payload: dict[str, Any],
    tool: dict[str, Any],
) -> dict[str, Any]:
    mem = _read_meminfo_bytes()
    total = int(mem.get("MemTotal") or 0)
    available = int(mem.get("MemAvailable") or mem.get("MemFree") or 0)
    xmx_mb = _effective_gradle_heap_mb(available)
    metaspace_mb = max(192, min(384, xmx_mb // 3))
    builder = files / "apk-self-builder"
    free = int(shutil.disk_usage(builder if builder.exists() else files).free)
    source_compressed = int(payload.get("source_bytes") or payload.get("sourceBytes") or 0)
    project_bytes = _tree_bytes(project) if project is not None and project.is_dir() else 0
    toolchain = Path(str((tool.get("paths") or {}).get("toolchain") or ""))
    toolchain_bytes = _tree_bytes(toolchain, entry_limit=60_000) if toolchain.is_dir() else 0
    # Antes do download usamos o tamanho compactado declarado. Depois da
    # extração, o tamanho real da árvore substitui a estimativa.
    source_estimate = project_bytes or min(MAX_SOURCE_EXPANDED_BYTES, max(source_compressed * 4, source_compressed))
    required_temp = max(1024 * 1024 * 1024, source_compressed + source_estimate * 2 + 512 * 1024 * 1024)

    supplied = payload.get("builderResources") if isinstance(payload.get("builderResources"), dict) else {}
    battery_percent = int(supplied.get("batteryPercent", -1) or -1)
    charging = bool(supplied.get("charging", False))
    try:
        temperature_c = float(supplied.get("temperatureC", -1.0))
    except Exception:
        temperature_c = -1.0
    try:
        thermal_status = int(supplied.get("thermalStatus", -1))
    except Exception:
        thermal_status = -1
    heavy = _active_heavy_build_processes()

    blockers: list[str] = []
    if available and available < (xmx_mb + 256) * 1024 * 1024:
        blockers.append("memory_low")
    if free < required_temp:
        blockers.append("storage_low")
    if battery_percent >= 0 and battery_percent < 25 and not charging:
        blockers.append("battery_low")
    if temperature_c >= 45.0:
        blockers.append("temperature_high")
    # Android Q+: SEVERE=3, CRITICAL=4, EMERGENCY=5, SHUTDOWN=6.
    if thermal_status >= 3:
        blockers.append("thermal_severe")
    if heavy:
        blockers.append("builder_busy")

    return {
        "ok": not blockers,
        "state": "ready" if not blockers else "preflight_blocked",
        "blockers": blockers,
        "memoryTotalBytes": total,
        "memoryAvailableBytes": available,
        "storageFreeBytes": free,
        "sourceCompressedBytes": source_compressed,
        "sourceEstimatedExpandedBytes": source_estimate,
        "projectTreeBytes": project_bytes,
        "toolchainBytes": toolchain_bytes,
        "estimatedRequiredTempBytes": required_temp,
        "batteryPercent": battery_percent,
        "charging": charging,
        "temperatureC": temperature_c,
        "thermalStatus": thermal_status,
        "concurrentBuildProcesses": heavy,
        "xmxMb": xmx_mb,
        "maxMetaspaceMb": metaspace_mb,
    }


_TRANSIENT_FAILURE_RE = re.compile(
    r"outofmemoryerror|java heap space|gc overhead|killed|signal 9|cannot allocate memory|"
    r"no space left on device|enospc|timed? ?out|timeout|connection reset|network is unreachable|"
    r"temporary failure|preflight_blocked|battery_low|temperature_high|thermal_severe|builder_busy",
    re.IGNORECASE,
)
_DETERMINISTIC_FAILURE_RE = re.compile(
    r"cannot find symbol|compilation failed|manifest merger failed|resource .* not found|"
    r"aapt2? .*error:|google-services|signing|keystore|package .* does not exist|"
    r"source zip contém caminho inseguro|sha256 do source zip divergente|toolchain .* inválido|"
    r"versionname .* divergente|versioncode .* divergente",
    re.IGNORECASE,
)


def _classify_failure(detail: Any) -> str:
    text = str(detail or "")
    if _TRANSIENT_FAILURE_RE.search(text):
        return "transient"
    if _DETERMINISTIC_FAILURE_RE.search(text):
        return "deterministic"
    return "unknown"


def _acquire_build_lock(builder: Path) -> tuple[bool, Path, dict[str, Any]]:
    lock = builder / ".apk-build-active"
    owner = lock / "owner.json"
    for _attempt in range(2):
        try:
            lock.mkdir(parents=False, exist_ok=False)
            _atomic_json(owner, {"pid": os.getpid(), "startedAt": time.time(), "schema": SCHEMA})
            return True, lock, {"pid": os.getpid(), "path": str(lock)}
        except FileExistsError:
            data = _safe_json_load(owner)
            pid = int(data.get("pid") or 0)
            started = float(data.get("startedAt") or 0.0)
            alive = pid > 0 and Path(f"/proc/{pid}").exists()
            fresh = started > 0 and time.time() - started < 4 * 60 * 60
            if alive and fresh:
                return False, lock, {"pid": pid, "startedAt": started, "path": str(lock)}
            shutil.rmtree(lock, ignore_errors=True)
        except Exception as exc:
            return False, lock, {"error": f"{type(exc).__name__}: {_short(exc, 300)}", "path": str(lock)}
    return False, lock, {"error": "lock ativo", "path": str(lock)}


def _release_build_lock(lock: Path) -> None:
    try:
        data = _safe_json_load(lock / "owner.json")
        if int(data.get("pid") or 0) not in {0, os.getpid()}:
            return
    except Exception:
        return
    shutil.rmtree(lock, ignore_errors=True)


def _hydrate_runtime_assets(project: Path, native_dir: Path, repro_assets: Path) -> dict[str, Any]:
    copied: list[str] = []
    jni = project / "app/src/main/jniLibs/arm64-v8a"
    jni.mkdir(parents=True, exist_ok=True)
    allowed_native = {
        "libcoreworker_executor.so", "libcoreworker_runner.so", "libcoreworker_proot.so",
        "libcoreworker_proot_loader.so", "libcoreworker_proot_loader32.so",
        "libcoreworker_busybox.so", "libbusybox.so", "libandroid-selinux.so",
        "libpcre2-8.so", "libtalloc.so",
    }
    if native_dir.is_dir():
        for source in native_dir.iterdir():
            if source.name not in allowed_native or not source.is_file():
                continue
            target = jni / source.name
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(project)))
    if repro_assets.is_dir():
        for source in repro_assets.rglob("*"):
            if not source.is_file():
                continue
            rel = source.relative_to(repro_assets)
            target = project / "app/src/main/assets" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(project)))
    # Compatibilidade .cwpart é apenas de LEITURA no APK já instalado. Nunca
    # propagamos o toolchain legado para o APK seguinte, mesmo se repro-assets
    # antigos ainda estiverem no armazenamento privado.
    toolchain_assets = project / "app/src/main/assets/core-linux/android-builder"
    removed_forbidden: list[str] = []
    if toolchain_assets.is_dir():
        forbidden = [
            toolchain_assets / "android-builder-toolchain.zip",
            toolchain_assets / "android-builder-toolchain.parts.json",
            *toolchain_assets.glob("*.cwpart"),
        ]
        for stale in forbidden:
            if stale.is_file():
                removed_forbidden.append(str(stale.relative_to(project)))
                stale.unlink()
    return {
        "copied": copied,
        "count": len(copied),
        "removedForbiddenToolchainAssets": removed_forbidden,
        "externalToolchainOnly": True,
    }


def _tail(path: Path, limit: int = 16000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as fh:
        size = path.stat().st_size
        fh.seek(max(0, size - limit * 2))
        raw = fh.read(limit * 2)
    return raw.decode("utf-8", errors="replace")[-limit:]


def _run_gradle(files: Path, native: Path, project: Path, payload: dict[str, Any], work: Path, log_path: Path, resources: dict[str, Any]) -> dict[str, Any]:
    del native
    # O _build já executou o smoke forçado; aqui reutilizamos o fingerprint salvo.
    pre = json.loads(preflight(str(files), "", False))
    if not pre.get("ready"):
        raise RuntimeError(pre.get("summary") or "autobuilder não está pronto")
    tool = pre["toolchain"]
    paths = tool["paths"]
    builder = files / "apk-self-builder"
    persistent = builder / "persistent"
    gradle_home = persistent / "gradle-home"
    home = persistent / "home"
    temp = work / "tmp"
    for path in (gradle_home, home, temp):
        path.mkdir(parents=True, exist_ok=True)

    xmx_mb = int(resources.get("xmxMb") or 512)
    metaspace_mb = int(resources.get("maxMetaspaceMb") or 256)
    gradle_props = gradle_home / "gradle.properties"
    gradle_props.write_text(
        "\n".join((
            f"android.aapt2FromMavenOverride={paths['aapt2']}",
            "org.gradle.daemon=false",
            "org.gradle.workers.max=1",
            "org.gradle.parallel=false",
            "org.gradle.vfs.watch=false",
            f"org.gradle.jvmargs=-Xmx{xmx_mb}m -Xms64m -XX:MaxMetaspaceSize={metaspace_mb}m -Dfile.encoding=UTF-8 -Djdk.lang.Process.launchMechanism=FORK",
            "",
        )), encoding="utf-8"
    )

    env = _toolchain_environment(tool, home=home, temp=temp, gradle_home=gradle_home, clean=False)
    vps_url = str(payload.get("coreWorkerVpsUrl") or payload.get("core_worker_vps_url") or "").strip()
    vps_label = str(payload.get("coreWorkerVpsLabel") or payload.get("core_worker_vps_label") or "VPS privada").strip()
    env.update({
        "CORE_WORKER_VPS_URL": vps_url,
        "CORE_WORKER_VPS_LABEL": vps_label,
        "CORE_WORKER_REQUIRE_COMPAT_SIGNING": "true",
        "CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN": "true",
        # Mantém o launcher e o daemon de uso único com os mesmos argumentos e
        # evita depender de jspawnhelper ao iniciar aapt2/Java no Android.
        "GRADLE_OPTS": f"-Xmx{xmx_mb}m -Xms64m -XX:MaxMetaspaceSize={metaspace_mb}m -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false -Dorg.gradle.vfs.watch=false -Djdk.lang.Process.launchMechanism=FORK",
        "JAVA_TOOL_OPTIONS": "-Djdk.lang.Process.launchMechanism=FORK",
    })
    command = [
        "/system/bin/sh", paths["gradle"], "assembleDebug",
        "--no-daemon", "--max-workers=1", "--stacktrace", "--console=plain",
    ]

    timeout = int(payload.get("timeout_seconds") or payload.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
    timeout = max(600, min(4 * 60 * 60, timeout))
    started = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("===== Core Worker APK self-build =====\n")
        log.write(f"schema={SCHEMA}\nstarted_at={int(started)}\nproject={project}\n")
        log.write("runtime=android-private-toolchain-direct\n")
        log.write(f"worker_version={payload.get('requiredAgentVersion') or ''}\n")
        log.write(f"agent_source_hash={payload.get('requiredAgentSourceHash') or ''}\n")
        log.write(f"source_fingerprint={payload.get('sourceFingerprint') or payload.get('source_sha256') or ''}\n")
        log.write(f"apk_target={payload.get('versionName') or ''} code={payload.get('versionCode') or 0}\n")
        log.write("jdk=17 gradle=8.9 agp=8.7.3 compileSdk=34 buildTools=34.0.0 chaquopy=17.0.0\n")
        log.write(f"aapt2={paths['aapt2']}\n")
        log.write(f"xmx_mb={xmx_mb} memory_available_bytes={resources.get('memoryAvailableBytes', 0)} storage_free_bytes={resources.get('storageFreeBytes', 0)}\n")
        log.write(f"toolchain_fingerprint={payload.get('toolchainFingerprint') or _toolchain_fingerprint(tool)}\n")
        log.write(f"toolchain_bytes={resources.get('toolchainBytes', 0)} project_bytes={resources.get('projectTreeBytes', 0)}\n")
        log.write("===== Gradle output =====\n")
        log.flush()
        process = subprocess.Popen(command, cwd=str(project), env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            return_code = 124
            log.write(f"\n===== TIMEOUT {timeout}s =====\n")
    return {
        "returncode": int(return_code),
        "timeoutSeconds": timeout,
        "durationSeconds": round(time.time() - started, 3),
        "log": str(log_path),
        "logTail": _tail(log_path, 16000),
    }


def _validate_apk(
    path: Path,
    *,
    expected_version_name: str = "",
    expected_version_code: int = 0,
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        raise FileNotFoundError("APK gerado não encontrado ou pequeno demais")
    if path.stat().st_size > MAX_APK_BYTES:
        raise ValueError("APK gerado excede o limite")
    identity = inspect_apk_identity(path)
    assert_expected_apk_identity(
        identity,
        expected_package="dev.core.worker",
        expected_version_name=expected_version_name,
        expected_version_code=expected_version_code,
    )
    return {
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        **identity,
    }


def _multipart_publish(apk_path: Path, fields: dict[str, Any], publish_url: str, token: str, worker_id: str, worker_version: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(publish_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL de publicação inválida")
    boundary = "----CoreWorkerApk" + hashlib.sha256(f"{time.time()}:{os.getpid()}".encode()).hexdigest()[:24]

    parts: list[bytes] = []
    for name, value in fields.items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ).encode("utf-8"))
    filename = _safe_filename(fields.get("filename"), "CoreWorker-debug.apk")
    file_header = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"apk\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/vnd.android.package-archive\r\n\r\n"
    ).encode("utf-8")
    ending = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = sum(len(item) for item in parts) + len(file_header) + apk_path.stat().st_size + len(ending)
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=180)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection.putrequest("POST", path)
    connection.putheader("Authorization", f"Bearer {token}")
    connection.putheader("X-Core-Worker-ID", worker_id)
    connection.putheader("X-Core-Worker-Version", worker_version)
    connection.putheader("X-Phone-Worker-Token", token)
    connection.putheader("User-Agent", f"CoreWorkerApkSelfBuilder/{worker_version}")
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    connection.putheader("Content-Length", str(content_length))
    connection.endheaders()
    for item in parts:
        connection.send(item)
    connection.send(file_header)
    with apk_path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            connection.send(block)
    connection.send(ending)
    response = connection.getresponse()
    raw = response.read(1024 * 1024)
    connection.close()
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text or "{}")
    except Exception:
        body = {"ok": False, "error": _short(text, 500)}
    if response.status < 200 or response.status >= 300:
        return {"ok": False, "status": response.status, "error": _short(body.get("error") if isinstance(body, dict) else text, 500)}
    return body if isinstance(body, dict) else {"ok": False, "error": "resposta inválida da VPS"}


def _publish_latest(files: Path, payload: dict[str, Any], server_url: str, worker_id: str, token: str, worker_version: str) -> dict[str, Any]:
    builder = files / "apk-self-builder"
    metadata_path = builder / "artifacts/latest-artifact.json"
    meta = _safe_json_load(metadata_path)
    apk = Path(str(meta.get("artifact_path") or ""))
    if not apk.is_file() or not _is_inside(apk, builder):
        raise FileNotFoundError("nenhum APK autoconstrído persistido para republicar")
    validated = _validate_apk(apk)
    if meta.get("sha256") and str(meta.get("sha256")) != validated["sha256"]:
        raise ValueError("sha256 do último artifact divergente")
    publish_url = str(payload.get("publish_url") or payload.get("publishUrl") or server_url.rstrip("/") + "/core-worker/app/publish")
    if not _same_origin(publish_url, server_url):
        raise ValueError("publish_url precisa apontar para a mesma VPS")
    fields = {
        "worker_id": worker_id,
        "workerName": "Core Worker APK self-builder",
        "filename": f"CoreWorker-v{validated['versionName']}-debug.apk",
        "versionName": validated["versionName"],
        "versionCode": int(validated["versionCode"]),
        "sha256": validated["sha256"],
        "requiredAgentVersion": worker_version,
        "notifyUsers": "true",
        "notificationRequested": "true",
        "sourceSha256": meta.get("sourceSha256") or "",
        "sourceFingerprint": meta.get("sourceFingerprint") or meta.get("sourceSha256") or "",
        "notificationId": meta.get("notificationId") or "",
        "apkSigningMode": meta.get("apkSigningMode") or "compat-vps-debug-keystore",
        "apkSigningKeystoreSha256": str(meta.get("apkSigningKeystoreSha256") or "")[:64],
        "changelog": payload.get("changelog") or meta.get("changelog") or ["APK compilado pelo próprio Core Worker APK"],
    }
    published = _multipart_publish(apk, fields, publish_url, token, worker_id, worker_version)
    return {
        "ok": bool(published.get("ok")),
        "summary": "APK republicado pelo próprio APK" if published.get("ok") else "falha publicando APK autoconstrído",
        "apk": {"filename": fields["filename"], **validated},
        "publish": published,
        "artifact": meta,
    }


def _build(payload: dict[str, Any], files: Path, cache: Path, native: Path, server_url: str, worker_id: str, token: str, worker_version: str) -> dict[str, Any]:
    # O manager Java já executou um smoke forçado antes de despachar o job.
    # Aqui reutilizamos o fingerprint persistido para não rodar Java/Gradle duas vezes.
    pre = json.loads(preflight(str(files), str(native), False))
    if not pre.get("ready"):
        return {"ok": False, "summary": pre.get("summary"), "error": pre.get("summary"), "preflight": pre, "retryable": True}

    source_url = str(payload.get("source_zip_url") or payload.get("sourceZipUrl") or "").strip()
    if not source_url:
        raise ValueError("source_zip_url ausente")
    expected_sha = str(payload.get("source_sha256") or payload.get("sourceSha256") or "").strip().lower()
    expected_bytes = int(payload.get("source_bytes") or payload.get("sourceBytes") or 0)
    source_fingerprint = str(payload.get("sourceFingerprint") or expected_sha).strip()
    version_name = str(payload.get("versionName") or payload.get("version_name") or "").strip()
    version_code = int(payload.get("versionCode") or payload.get("version_code") or 0)
    notification_id = str(payload.get("notificationId") or f"apk-{version_code}-{source_fingerprint[:12]}").strip()

    early_resources = _resource_preflight(files, None, payload, pre["toolchain"])
    if not early_resources.get("ok"):
        detail = "preflight_blocked: " + ", ".join(early_resources.get("blockers") or [])
        return {
            "ok": False,
            "summary": detail,
            "error": detail,
            "preflight": pre,
            "resource_preflight": early_resources,
            "failure_category": "transient",
            "retryable": True,
        }

    builder = files / "apk-self-builder"
    work_root = builder / "work"
    artifacts = builder / "artifacts"
    logs = builder / "logs"
    repro_assets = builder / "repro-assets"
    for path in (work_root, artifacts, logs):
        path.mkdir(parents=True, exist_ok=True)
    lock_ok, build_lock, lock_info = _acquire_build_lock(builder)
    if not lock_ok:
        return {
            "ok": False,
            "summary": "preflight_blocked: builder_busy",
            "error": "builder_busy: outro build ainda possui o lock",
            "builder_lock": lock_info,
            "failure_category": "transient",
            "retryable": True,
        }
    job_slug = _safe_filename(notification_id or f"build-{int(time.time())}", "apk-build")
    work = work_root / (job_slug + "-" + hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:8])
    source_zip = work / "source.zip"
    source_root = work / "source"
    log_path = logs / (job_slug + "-gradle.log")
    started = time.time()
    try:
        work.mkdir(parents=True, exist_ok=False)
        download = _download_source(source_url, source_zip, expected_sha, expected_bytes, server_url)
        extracted = _safe_extract_zip(source_zip, source_root)
        project = _find_project(source_root, str(payload.get("project_subdir") or "android/core-worker-app"))
        private = _inject_private_files(project, payload)
        hydrated = _hydrate_runtime_assets(project, native, repro_assets)
        resources = _resource_preflight(files, project, payload, pre["toolchain"])
        if not resources.get("ok"):
            detail = "preflight_blocked: " + ", ".join(resources.get("blockers") or [])
            return {
                "ok": False,
                "summary": detail,
                "error": detail,
                "resource_preflight": resources,
                "builder_environment": {"preflight": pre, "hydrated": hydrated, "resources": resources},
                "failure_category": "transient",
                "retryable": True,
            }
        output_dir = project / "app/build/outputs/apk/debug"
        shutil.rmtree(output_dir, ignore_errors=True)
        build = _run_gradle(files, native, project, payload, work, log_path, resources)
        if build["returncode"] != 0:
            detail = (build.get("logTail") or "") + "\nGradle retornou código " + str(build["returncode"])
            category = _classify_failure(detail)
            return {
                "ok": False,
                "summary": "autobuild do APK falhou; consulte gradle_log_tail",
                "error": "Gradle retornou código " + str(build["returncode"]),
                "returncode": build["returncode"],
                "gradle_log_tail": build["logTail"],
                "duration_seconds": round(time.time() - started, 3),
                "builder_environment": {"preflight": pre, "hydrated": hydrated, "resources": resources},
                "failure_category": category,
                "retryable": category != "deterministic",
            }
        candidates = sorted((project / "app/build/outputs/apk/debug").glob("*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            raise FileNotFoundError("Gradle terminou sem gerar app-debug.apk")
        built_apk = candidates[0]
        validated = _validate_apk(
            built_apk,
            expected_version_name=version_name,
            expected_version_code=version_code,
        )
        actual_version_name = str(validated["versionName"])
        actual_version_code = int(validated["versionCode"])
        filename = _safe_filename(payload.get("filename"), f"CoreWorker-v{actual_version_name}-debug.apk")
        if not filename.lower().endswith(".apk"):
            filename += ".apk"
        artifact_path = artifacts / filename
        if artifact_path.exists() and _sha256_file(artifact_path) != validated["sha256"]:
            artifact_path = artifacts / (artifact_path.stem + "-" + notification_id[:16] + ".apk")
        shutil.copy2(built_apk, artifact_path)
        meta = {
            "schema": SCHEMA,
            "filename": artifact_path.name,
            "versionName": actual_version_name,
            "versionCode": actual_version_code,
            "sha256": validated["sha256"],
            "bytes": validated["bytes"],
            "artifact_path": str(artifact_path),
            "sourceFingerprint": source_fingerprint,
            "sourceSha256": download["sha256"],
            "notificationId": notification_id,
            "apkSigningMode": private["signingMode"],
            "apkSigningKeystoreSha256": private["signingKeystoreSha256"],
            "changelog": payload.get("changelog") or ["APK compilado pelo próprio Core Worker APK"],
            "created_at": time.time(),
            "builderRuntime": "android-private-toolchain-direct",
            "workerVersion": worker_version,
        }
        _atomic_json(artifact_path.with_suffix(artifact_path.suffix + ".json"), meta)
        _atomic_json(artifacts / "latest-artifact.json", meta)
        result: dict[str, Any] = {
            "ok": True,
            "summary": f"APK {actual_version_name} compilado pelo próprio APK",
            "build_gradle_ok": True,
            "artifact_found": True,
            "apk": {"filename": artifact_path.name, "signed": True, **validated},
            "versionName": actual_version_name,
            "versionCode": actual_version_code,
            "artifact_meta": meta,
            "source": {**download, **extracted},
            "builder_environment": {"preflight": pre, "hydrated": hydrated},
            "duration_seconds": round(time.time() - started, 3),
        }
        if bool(payload.get("publish", True)):
            publish = _publish_latest(files, payload, server_url, worker_id, token, worker_version)
            result["publish"] = publish.get("publish", publish)
            result["published"] = bool(publish.get("ok"))
            if not publish.get("ok"):
                result["ok"] = False
                result["summary"] = "APK compilado e persistido, mas a publicação falhou"
                result["error"] = _short((publish.get("publish") or {}).get("error") if isinstance(publish.get("publish"), dict) else publish.get("summary"), 500)
        return result
    finally:
        # O workspace contém keystore e senhas temporárias; nunca é preservado.
        shutil.rmtree(work, ignore_errors=True)
        _release_build_lock(build_lock)


def run(task: str, payload_json: str, files_dir: str, cache_dir: str, native_dir: str, server_url: str, worker_id: str, token: str, worker_version: str) -> str:
    payload = json.loads(payload_json or "{}")
    if not isinstance(payload, dict):
        payload = {}
    files = Path(files_dir)
    cache = Path(cache_dir)
    native = Path(native_dir)
    result: dict[str, Any]
    try:
        if task == "apk_build_debug":
            result = _build(payload, files, cache, native, server_url, worker_id, token, worker_version)
        elif task == "apk_publish_last":
            result = _publish_latest(files, payload, server_url, worker_id, token, worker_version)
        elif task == "apk_builder_status":
            result = json.loads(preflight(files_dir, native_dir))
        else:
            result = {"ok": False, "error": "task de autobuild não permitida", "task": task}
    except Exception as exc:
        detail = f"{type(exc).__name__}: {_short(exc, 800)}"
        category = _classify_failure(detail)
        result = {
            "ok": False,
            "task": task,
            "summary": "falha no autobuilder do APK",
            "error": detail,
            "failure_category": category,
            "retryable": category != "deterministic",
        }
    result.setdefault("task", task)
    result.setdefault("type", task)
    result.setdefault("executedBy", "core-worker-apk-self-builder")
    result.setdefault("schema", SCHEMA)
    result.setdefault("updatedAt", _now_ms())
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
