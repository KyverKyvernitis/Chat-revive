#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import contextlib
import fcntl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utility.commands.workers_registry import CoreWorkerRegistryError, get_core_workers_registry  # noqa: E402
from utility.apk_identity import assert_expected_apk_identity, inspect_apk_identity  # noqa: E402

PHONE_WORKER_FILES: tuple[tuple[str, int], ...] = (
    ("phone_worker.py", 0o755),
    ("phone_worker_bootstrap.py", 0o755),
    ("apk_identity.py", 0o644),
    ("music_agent.py", 0o755),
    ("start-phone-worker.sh", 0o755),
    ("start-phone-music-agent.sh", 0o755),
    ("watch-phone-worker.sh", 0o755),
    ("pair-phone-worker.sh", 0o755),
    ("repair-phone-worker.sh", 0o755),
    ("accept-core-worker-on-device.sh", 0o755),
    ("bootstrap-phone-worker.sh", 0o755),
    ("install.sh", 0o755),
    ("README.md", 0o644),
    ("phone-worker.env.example", 0o600),
    ("teto_renderer/__init__.py", 0o644),
    ("teto_renderer/errors.py", 0o644),
    ("teto_renderer/cache.py", 0o644),
    ("teto_renderer/voicebank.py", 0o644),
    ("teto_renderer/phonemizer.py", 0o644),
    ("teto_renderer/prosody.py", 0o644),
    ("teto_renderer/renderer.py", 0o644),
    ("scripts/validate-teto-assets.py", 0o755),
)
PHONE_WORKER_UPDATE_ARCHIVE_MIN_VERSION = "1.11.0"
PHONE_WORKER_BOOTSTRAP_MIN_VERSION = "1.0.0"
AGENT_RELEASE_ROOT = ROOT / "data" / "core_worker_agent"
PHONE_WORKER_CANONICAL_ROOT = (ROOT / "deploy" / "termux" / "phone-worker").resolve()
PHONE_WORKER_SOURCE_HASH_EXCLUDED = frozenset({"README.md", "phone-worker.env.example"})
PHONE_WORKER_SOURCE_FILES = tuple(
    item for item in PHONE_WORKER_FILES if item[0] not in PHONE_WORKER_SOURCE_HASH_EXCLUDED
)
# Agents anteriores ao bootstrap persistente não recebem mais o agent principal
# inline. O primeiro resgate é feito uma vez por repair-phone-worker.sh; depois
# disso jobs são apenas um acelerador e o pull do manifesto é autoritativo.

PENDING_PATH = ROOT / "data" / "core_worker_automation_pending.json"
STATUS_PATH = ROOT / "data" / "core_worker_automation_status.json"
STATE_PATH = ROOT / "data" / "core_worker_automation_state.json"
LOCK_DIR = ROOT / "data" / "locks"
APK_BUILD_MIN_BATTERY_PERCENT = 25


def _lock_key(value: str) -> str:
    value = str(value or "").strip() or "all"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value)[:120] or "all"


@contextlib.contextmanager
def _process_pending_lock(worker_id: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"core-worker-automation-process-pending-{_lock_key(worker_id)}.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "worker_id": str(worker_id or ""), "started_at": time.time()}, ensure_ascii=False))
            fh.flush()
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            fh.close()


def _load_repo_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_repo_env()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _changed_files_from_env() -> list[str]:
    raw = os.getenv("CORE_WORKER_CHANGED_FILES") or ""
    items = []
    for line in raw.splitlines():
        clean = line.strip()
        if clean and clean not in items:
            items.append(clean)
    return items


def _has_changed(changed_files: Iterable[str], prefix: str) -> bool:
    return any(str(item).startswith(prefix) for item in changed_files)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root: Path, *, exclude_dirs: set[str] | None = None) -> str:
    exclude_dirs = set(exclude_dirs or set())
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if not path.is_file():
            continue
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _hash_phone_worker_files(root: Path) -> str:
    """Hash estável do runtime instalado; documentação/exemplo não bloqueiam boot."""
    digest = hashlib.sha256()
    for name, _mode in sorted(PHONE_WORKER_SOURCE_FILES):
        path = root / name
        if not path.is_file():
            return ""
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _current_fingerprints() -> dict[str, Any]:
    phone_dir = ROOT / "deploy" / "termux" / "phone-worker"
    android_dir = ROOT / "android" / "core-worker-app"
    version_name, version_code = _read_android_version()
    return {
        "phone_worker_version": _read_phone_worker_version(),
        "phone_worker_hash": _hash_phone_worker_files(phone_dir),
        "apk_versionName": version_name,
        "apk_versionCode": version_code,
        "apk_source_hash": _hash_tree(android_dir, exclude_dirs={"build", ".gradle", "releases"}),
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _read_phone_worker_version() -> str:
    path = ROOT / "deploy" / "termux" / "phone-worker" / "phone_worker.py"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    m = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else "desconhecida"


def _read_android_version() -> tuple[str, int]:
    path = ROOT / "android" / "core-worker-app" / "app" / "build.gradle"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    m_name = re.search(r'versionName\s+["\']([^"\']+)["\']', text)
    m_code = re.search(r"versionCode\s+(\d+)", text)
    return (m_name.group(1) if m_name else "0.0.0", int(m_code.group(1)) if m_code else 0)


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) if parts else (0,)


def _public_base_url() -> str:
    explicit = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("CORE_WORKER_VPS_URL") or os.getenv("VPS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit and "IP_TAILSCALE_DA_VPS" not in explicit:
        return explicit
    port = str(os.getenv("CORE_WORKER_PUBLIC_PORT") or os.getenv("PORT") or "10000").strip() or "10000"
    host = str(os.getenv("CORE_WORKER_PUBLIC_HOST") or os.getenv("VPS_TAILSCALE_HOST") or "").strip()
    if not host:
        try:
            proc = subprocess.run(["tailscale", "ip", "-4"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False)
            for line in (proc.stdout or "").splitlines():
                candidate = line.strip()
                if re.fullmatch(r"100(?:\.\d{1,3}){3}", candidate):
                    host = candidate
                    break
        except Exception:
            host = ""
    if host:
        return f"http://{host}:{port}"
    return f"http://IP_TAILSCALE_DA_VPS:{port}"


def _core_worker_release_dir() -> Path:
    configured = str(os.getenv("CORE_WORKER_APK_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return ROOT / "android" / "core-worker-app" / "releases"

def _desired_apk_source_path() -> Path:
    return _core_worker_release_dir() / "desired-source.json"


def _desired_apk_source_lock_path() -> Path:
    return _core_worker_release_dir() / ".desired-source.lock"


@contextlib.contextmanager
def _desired_apk_source_lock():
    path = _desired_apk_source_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _publish_desired_apk_source(
    *,
    version_name: str,
    version_code: int,
    source_fingerprint: str,
    source_sha256: str,
    selected_builder_worker_id: str = "",
    selected_builder_runtime_kind: str = "",
    required_agent_source_hash: str = "",
    toolchain_fingerprint: str = "",
) -> dict[str, Any]:
    fingerprint = str(source_fingerprint or "").strip().lower()
    archive_sha = str(source_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise RuntimeError("source fingerprint inválido para desired-source")
    if archive_sha and not re.fullmatch(r"[0-9a-f]{64}", archive_sha):
        raise RuntimeError("source sha256 inválido para desired-source")
    record = {
        "schema": "core-worker-apk-desired-source-v1",
        "versionName": str(version_name or ""),
        "versionCode": int(version_code or 0),
        "sourceFingerprint": fingerprint,
        "sourceSha256": archive_sha,
        "publicationPolicy": "selected-builder-v1",
        "selectedBuilderWorkerId": str(selected_builder_worker_id or "").strip(),
        "selectedBuilderRuntimeKind": str(selected_builder_runtime_kind or "").strip().lower(),
        "requiredAgentSourceHash": str(required_agent_source_hash or "").strip().lower(),
        "toolchainFingerprint": str(toolchain_fingerprint or "").strip().lower(),
        "updatedAt": int(time.time()),
    }
    for key in ("requiredAgentSourceHash", "toolchainFingerprint"):
        if record[key] and not re.fullmatch(r"[0-9a-f]{64}", record[key]):
            raise RuntimeError(f"{key} inválido para desired-source")
    path = _desired_apk_source_path()
    previous: dict[str, Any] = {}
    with _desired_apk_source_lock():
        with contextlib.suppress(Exception):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        _atomic_write_json(path, record)
    changed = str(previous.get("sourceFingerprint") or "").strip().lower() != fingerprint
    invalidation: dict[str, Any] = {"ok": True, "superseded": 0, "invalidated_running": 0}
    if changed:
        registry = get_core_workers_registry()
        supersede = getattr(registry, "supersede_apk_jobs_for_new_source", None)
        if callable(supersede):
            invalidation = supersede(
                fingerprint, version_code=int(version_code or 0), reason="job APK superado por fonte mais nova"
            )
        else:
            # Compatibilidade temporária durante a ordem segura de migração: uma
            # VPS ainda com registry antigo continua publicando o desired-source;
            # o endpoint /publish ainda bloqueia artefatos obsoletos por fingerprint.
            invalidation = {"ok": True, "superseded": 0, "invalidated_running": 0, "registry_legacy": True}
    return {"record": record, "previousRecord": previous, "changed": changed, "invalidation": invalidation}


def _canonical_phone_worker_root() -> Path:
    src = (ROOT / "deploy" / "termux" / "phone-worker").resolve()
    if src != PHONE_WORKER_CANONICAL_ROOT:
        raise RuntimeError("raiz não canônica do phone-worker recusada")
    nested = ROOT / "tts-bot-main" / "deploy" / "termux" / "phone-worker"
    # A árvore aninhada histórica pode existir na base por compatibilidade, mas
    # nunca é fonte instalável nem entra em pacote/manifesto do agent.
    if nested.resolve() == src:
        raise RuntimeError("raiz canônica do phone-worker ambígua")
    return src


def _build_worker_update_payload(*, scripts_only: bool = False) -> dict[str, Any]:
    src = _canonical_phone_worker_root()
    targets = PHONE_WORKER_FILES if not scripts_only else tuple(item for item in PHONE_WORKER_FILES if item[0].endswith(".sh"))
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, mode in targets:
        path = src / name
        if not path.is_file():
            missing.append(name)
            continue
        raw = path.read_bytes()
        files.append({
            "target": name,
            "mode": mode,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })
    if missing:
        raise RuntimeError("arquivos obrigatórios do phone-worker ausentes: " + ", ".join(missing[:8]))
    if not files:
        raise RuntimeError("nenhum arquivo do phone-worker encontrado")
    source_hash = _hash_phone_worker_files(src) if not scripts_only else ""
    return {
        "version": _read_phone_worker_version(),
        "source_hash": source_hash,
        "restart": not scripts_only,
        "scripts_only": scripts_only,
        "auto": True,
        "source": "vps-updater",
        "update_transport": "inline-b64-v1",
        "ensure_apk_builder": _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True),
        "files": files,
    }


def _write_deterministic_zip_member(zf: zipfile.ZipFile, name: str, raw: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (int(mode) & 0xFFFF) << 16
    zf.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{time.time_ns()}")
    raw = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _publish_phone_worker_release(inline_payload: dict[str, Any]) -> dict[str, Any]:
    """Publica o target persistente do agent sem guardar o agent no registry.

    O ZIP é imutável por source_hash e `latest.json` é trocado atomicamente.
    A VPS apenas empacota bytes; nunca executa o runtime Android/Gradle.
    """
    files = inline_payload.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("payload do phone-worker sem arquivos")
    source_hash = str(inline_payload.get("source_hash") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise RuntimeError("source_hash inválido ao publicar release do agent")
    version = str(inline_payload.get("version") or "").strip()
    if not version:
        raise RuntimeError("versão do agent ausente")

    decoded: list[tuple[str, int, bytes, str]] = []
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("arquivo inválido no release do agent")
        target = str(item.get("target") or "").strip().replace("\\", "/")
        if target in seen or target.startswith("/") or any(part in {"", ".", ".."} for part in target.split("/")):
            raise ValueError(f"caminho inválido/duplicado no release: {target}")
        seen.add(target)
        raw = base64.b64decode(str(item.get("data_b64") or "").encode("ascii"), validate=True)
        sha = hashlib.sha256(raw).hexdigest()
        if sha != str(item.get("sha256") or "").strip().lower():
            raise ValueError(f"sha256 divergente antes de empacotar {target}")
        mode = int(item.get("mode") or 0o644)
        decoded.append((target, mode, raw, sha))
        members.append({"path": target, "mode": mode, "bytes": len(raw), "sha256": sha})

    release_root = AGENT_RELEASE_ROOT / "releases"
    release_root.mkdir(parents=True, exist_ok=True)
    inner_manifest = {
        "schema": "core-phone-worker-release-v2",
        "version": version,
        "source_hash": source_hash,
        "min_bootstrap_version": PHONE_WORKER_BOOTSTRAP_MIN_VERSION,
        "members": members,
    }
    inner_raw = json.dumps(inner_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = release_root / f".{source_hash}.{os.getpid()}.{time.time_ns()}.tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        _write_deterministic_zip_member(zf, "phone-worker-release.json", inner_raw, 0o644)
        for target, mode, raw, _sha in sorted(decoded):
            _write_deterministic_zip_member(zf, target, raw, mode)
    archive_sha = _sha256_file(tmp)
    final = release_root / f"{source_hash}.zip"
    if final.exists():
        if _sha256_file(final) != archive_sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("release imutável existente diverge do novo pacote")
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, final)

    published_at = int(time.time())
    base = _public_base_url().rstrip("/")
    latest = {
        "schema": "core-phone-worker-release-v2",
        "release_id": f"agent-{version}-{source_hash[:12]}",
        "version": version,
        "source_hash": source_hash,
        "sha256": archive_sha,
        "bytes": final.stat().st_size,
        "members": members,
        "min_bootstrap_version": PHONE_WORKER_BOOTSTRAP_MIN_VERSION,
        "published_at": published_at,
        "url": f"{base}/core-worker/agent/releases/{source_hash}.zip",
    }
    _atomic_write_json(AGENT_RELEASE_ROOT / "latest.json", latest)
    _atomic_write_json(AGENT_RELEASE_ROOT / "releases" / f"{source_hash}.json", latest)
    return latest


def _build_worker_update_artifact_payload(inline_payload: dict[str, Any]) -> dict[str, Any]:
    """Payload pequeno de entrega rápida; o target persistente é autoritativo."""
    latest = _publish_phone_worker_release(inline_payload)
    return {
        "version": latest["version"],
        "source_hash": latest["source_hash"],
        "restart": bool(inline_payload.get("restart", True)),
        "auto": bool(inline_payload.get("auto", True)),
        "source": inline_payload.get("source") or "vps-updater",
        "update_transport": "bootstrap-manifest-v2",
        "bootstrap_manifest": True,
        "manifest_url": f"{_public_base_url().rstrip('/')}/core-worker/agent/latest",
        "release_url": latest["url"],
        "release_sha256": latest["sha256"],
        "release_bytes": latest["bytes"],
        "min_bootstrap_version": latest["min_bootstrap_version"],
    }


def _load_google_services_payload_for_apk_build() -> dict[str, Any]:
    """Envia google-services.json só pelo payload autenticado do job.

    O source ZIP é servido por HTTP para o phone worker; por isso não deve conter
    o google-services.json local. O arquivo continua fora do GitHub e fora do
    ZIP público, mas chega ao worker builder no payload do job.
    """
    candidates: list[Path] = []
    for raw_path in (
        os.getenv("CORE_WORKER_GOOGLE_SERVICES_JSON"),
        os.getenv("CORE_WORKER_FIREBASE_ANDROID_CONFIG"),
        os.getenv("GOOGLE_SERVICES_JSON"),
    ):
        if raw_path:
            candidates.append(Path(str(raw_path)).expanduser())
    candidates.append(ROOT / "android" / "core-worker-app" / "app" / "google-services.json")
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            "google-services.json local não encontrado. Coloque em "
            "android/core-worker-app/app/google-services.json na VPS/build env ou defina CORE_WORKER_GOOGLE_SERVICES_JSON."
        )
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError("google-services.json grande demais")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"google-services.json inválido: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("google-services.json inválido: raiz não é objeto JSON")
    project_info = data.get("project_info") if isinstance(data.get("project_info"), dict) else {}
    project_id = str(project_info.get("project_id") or "").strip()
    clients = data.get("client") if isinstance(data.get("client"), list) else []
    matched_client: dict[str, Any] | None = None
    for client in clients:
        if not isinstance(client, dict):
            continue
        info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
        if str(android.get("package_name") or "").strip() == "dev.core.worker":
            matched_client = client
            break
    if not project_id or matched_client is None:
        raise ValueError("google-services.json precisa conter project_id e client Android package_name dev.core.worker")
    info = matched_client.get("client_info") if isinstance(matched_client.get("client_info"), dict) else {}
    mobile_app_id = str(info.get("mobilesdk_app_id") or "").strip()
    api_keys = matched_client.get("api_key") if isinstance(matched_client.get("api_key"), list) else []
    api_key = ""
    for entry in api_keys:
        if isinstance(entry, dict) and str(entry.get("current_key") or "").strip():
            api_key = str(entry.get("current_key") or "").strip()
            break
    if not mobile_app_id or not api_key:
        raise ValueError("google-services.json precisa conter mobilesdk_app_id e api_key para dev.core.worker")
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "googleServicesJsonB64": base64.b64encode(raw).decode("ascii"),
        "googleServicesSha256": sha,
        "googleServicesPackage": "dev.core.worker",
        "googleServicesProjectId": project_id[:80],
        "googleServicesSource": "local-vps-payload",
    }


def _load_apk_signing_payload_for_worker_build() -> dict[str, Any]:
    """Carrega a keystore compatível sem colocá-la no Git/ZIP público.

    A VPS envia a keystore somente pelo payload autenticado do job para o phone
    worker assinar o APK com a mesma chave da versão instalada. Isso evita o
    erro do Android de conflito de pacote por assinatura diferente.
    """
    candidates: list[Path] = []
    for raw_path in (
        os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_SIGNING_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_KEYSTORE"),
    ):
        if raw_path:
            candidates.append(Path(str(raw_path)).expanduser())
    candidates.extend([
        Path("/home/ubuntu/secrets/core-worker-upload.keystore"),
        Path.home() / ".android" / "debug.keystore",
    ])
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            "keystore compatível do Core Worker não encontrada. Preserve/copie a chave antiga para "
            "/home/ubuntu/secrets/core-worker-upload.keystore."
        )
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("keystore compatível grande demais")
    alias = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEY_ALIAS")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_ALIAS")
        or os.getenv("CORE_WORKER_APK_KEY_ALIAS")
        or "androiddebugkey"
    ).strip()
    storepass = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE_PASSWORD")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE_PASSWORD")
        or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD")
        or "android"
    ).strip()
    keypass = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEY_PASSWORD")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_PASSWORD")
        or os.getenv("CORE_WORKER_APK_KEY_PASSWORD")
        or storepass
        or "android"
    ).strip()
    if not alias or not storepass:
        raise ValueError("alias/senha da keystore compatível ausentes")
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "apkSigningMode": "compat-vps-debug-keystore",
        "apkSigningKeystoreB64": base64.b64encode(raw).decode("ascii"),
        "apkSigningKeystoreSha256": sha,
        "apkSigningKeyAlias": alias,
        "apkSigningStorePassword": storepass,
        "apkSigningKeyPassword": keypass,
        "apkSigningSource": "local-vps-secret",
    }


def _prepare_apk_source_zip() -> dict[str, Any]:
    project = ROOT / "android" / "core-worker-app"
    if not project.is_dir():
        raise FileNotFoundError(str(project))
    release_dir = _core_worker_release_dir()
    release_dir.mkdir(parents=True, exist_ok=True)
    zip_path = release_dir / "source-core-worker-app.zip"
    excluded_dirs = {"build", ".gradle", "releases", ".idea"}
    excluded_names = {
        ".env",
        "local.properties",
        "private.properties",
        "vps.properties",
        "google-services.json",
        "firebase-service-account.json",
    }
    excluded_suffixes = (".jks", ".keystore", ".p12", ".pem", ".key")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(project.rglob("*")):
            rel = path.relative_to(project)
            if any(part in excluded_dirs for part in rel.parts):
                continue
            if path.is_dir():
                continue
            name = path.name.lower()
            rel_text = rel.as_posix().lower()
            if name in excluded_names or "service-account" in name or rel_text.endswith("/google-services.json"):
                continue
            if any(name.endswith(suffix) for suffix in excluded_suffixes):
                continue
            already_compressed = path.suffix.lower() in {".zip", ".jar", ".apk", ".so", ".gz", ".xz", ".zst", ".7z"}
            zf.write(
                path,
                (Path("android/core-worker-app") / rel).as_posix(),
                compress_type=zipfile.ZIP_STORED if already_compressed else zipfile.ZIP_DEFLATED,
                compresslevel=None if already_compressed else 6,
            )
    source_bytes = zip_path.stat().st_size
    source_sha256 = _sha256_file(zip_path)
    return {
        "path": str(zip_path),
        "filename": zip_path.name,
        "bytes": source_bytes,
        "sha256": source_sha256,
        "url": f"{_public_base_url()}/core-worker/app/{zip_path.name}",
        "firebase_config_delivery": "job_payload",
    }


def _load_registry_snapshot() -> dict[str, Any]:
    try:
        timeout = float(os.getenv("CORE_WORKER_AUTOMATION_REGISTRY_LOCK_TIMEOUT_SECONDS", "0.25") or 0.25)
    except Exception:
        timeout = 0.25
    try:
        return get_core_workers_registry().snapshot(lock_timeout_seconds=max(0.0, min(3.0, timeout)))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "workers": [], "jobs": []}


def _load_pending() -> dict[str, Any]:
    if not PENDING_PATH.exists():
        return {}
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data: dict[str, Any]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in (data or {}).items() if v}
    if not clean:
        with contextlib.suppress(Exception):
            PENDING_PATH.unlink()
        return
    tmp = PENDING_PATH.with_suffix(PENDING_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PENDING_PATH)


def _latest_apk_manifest() -> dict[str, Any]:
    release_dir = _core_worker_release_dir()
    manifest = release_dir / "latest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        filename = str(data.get("filename") or "").strip()
        if not filename:
            apk_url = str(data.get("apkUrl") or data.get("downloadUrl") or "").split("?", 1)[0].rstrip("/")
            filename = apk_url.rsplit("/", 1)[-1]
        if not filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".apk"):
            return {}
        apk_path = (release_dir / filename).resolve()
        if apk_path.parent != release_dir.resolve() or not apk_path.is_file():
            return {}
        identity = inspect_apk_identity(apk_path)
        assert_expected_apk_identity(identity, expected_package="dev.core.worker")
        actual_sha = _sha256_file(apk_path)
        declared_sha = str(data.get("sha256") or "").strip().lower()
        if declared_sha and declared_sha != actual_sha:
            return {}
        data["filename"] = filename
        data["packageName"] = str(identity["packageName"])
        data["versionName"] = str(identity["versionName"])
        data["versionCode"] = int(identity["versionCode"])
        data["sha256"] = actual_sha
        data["compiledIdentityVerified"] = True
        return data
    except Exception:
        return {}


def _manifest_version_code() -> int:
    try:
        return int(_latest_apk_manifest().get("versionCode") or 0)
    except Exception:
        return 0


def _manifest_source_sha() -> str:
    data = _latest_apk_manifest()
    return str(data.get("sourceFingerprint") or data.get("source_fingerprint") or data.get("sourceSha256") or data.get("source_sha256") or "").strip().lower()


def _latest_apk_matches(version_code: int, source_fingerprint: str = "") -> bool:
    data = _latest_apk_manifest()
    if not data:
        return False
    try:
        manifest_code = int(data.get("versionCode") or 0)
    except Exception:
        manifest_code = 0
    if int(version_code or 0) and manifest_code < int(version_code or 0):
        return False
    expected = str(source_fingerprint or "").strip().lower()
    if expected:
        current_values = {
            str(data.get("sourceFingerprint") or "").strip().lower(),
            str(data.get("source_fingerprint") or "").strip().lower(),
            str(data.get("sourceSha256") or "").strip().lower(),
            str(data.get("source_sha256") or "").strip().lower(),
        }
        short = expected[:12]
        if expected not in current_values and short and not any(short and short in value for value in current_values if value):
            return False
    return True


def _worker_source_hash(worker: dict[str, Any] | None) -> str:
    if not isinstance(worker, dict):
        return ""
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    for value in (
        worker.get("source_hash"),
        status.get("source_hash"),
        status.get("phone_worker_source_hash"),
        health.get("source_hash"),
    ):
        clean = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", clean):
            return clean
    return ""


def _worker_needs_agent_update(
    worker: dict[str, Any] | None,
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    if not isinstance(worker, dict):
        return False
    expected_hash = str(target_source_hash or "").strip().lower()
    current_hash = _worker_source_hash(worker)
    current_version = str(worker.get("version") or "")
    # Nunca faça downgrade automático: force significa reaplicar a fonte da
    # mesma versão, não substituir um agent que já está adiante do servidor.
    if current_version and _version_tuple(current_version) > _version_tuple(target_version):
        return False
    if not current_version or _version_tuple(current_version) < _version_tuple(target_version):
        return True
    # A partir do target que implementa hash, ausência também significa estado
    # incompleto/desconhecido. Isso mantém o segundo salto do bootstrap pendente
    # mesmo se o resultado antigo já tiver gravado a nova versão no registry.
    if expected_hash:
        return current_hash != expected_hash
    return bool(force)


def _worker_has_recovery_bootstrap(worker: dict[str, Any] | None) -> bool:
    """Confirma que o runtime já possui o bootstrap pull independente.

    Agents legados só aceitavam ``phone_worker.py`` na allowlist. Eles não
    podem receber o novo bootstrap com segurança pelo próprio protocolo antigo;
    a migração inicial usa ``repair-phone-worker.sh`` uma única vez.
    """
    if not isinstance(worker, dict):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    update_status = status.get("worker_update") if isinstance(status.get("worker_update"), dict) else {}
    updater = update_status.get("updater") if isinstance(update_status.get("updater"), dict) else {}
    bootstrap_version = str(updater.get("bootstrap_version") or "").strip()
    declared = (
        worker.get("worker_update_transports")
        or status.get("worker_update_transports")
        or update_status.get("transports")
        or []
    )
    transports = _task_set(declared)
    return bool(
        "bootstrap_manifest_v2" in transports
        and bootstrap_version
        and _version_tuple(bootstrap_version) >= _version_tuple(PHONE_WORKER_BOOTSTRAP_MIN_VERSION)
    )


def _worker_supports_update_archive(worker: dict[str, Any] | None) -> bool:
    if not isinstance(worker, dict):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    update_status = status.get("worker_update") if isinstance(status.get("worker_update"), dict) else {}
    declared = (
        worker.get("worker_update_transports")
        or status.get("worker_update_transports")
        or update_status.get("transports")
        or []
    )
    transports = _task_set(declared)
    return bool(
        "zip_v1" in transports
        and _version_tuple(worker.get("version")) >= _version_tuple(PHONE_WORKER_UPDATE_ARCHIVE_MIN_VERSION)
    )


def _workers_need_agent_version(
    snapshot: dict[str, Any],
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    """Retorna True só para workers ativos/online fora da versão/fonte alvo.

    Workers antigos offline não devem manter o painel preso em "agent pendente".
    Quando um celular voltar online, o heartbeat/process-pending roda de novo e
    cria o update se a versão real ainda estiver antiga.
    """
    if not str(target_version or "").strip():
        return False
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict) or not worker.get("online") or worker.get("enabled") is False:
            continue
        if not _is_termux_bootstrap_worker(worker):
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
            continue
        if _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            return True
    return False


def _registered_workers_need_agent_version(
    snapshot: dict[str, Any],
    target_version: str,
    target_source_hash: str = "",
    *,
    force: bool = False,
) -> bool:
    """Mantém a pendência para um Termux offline que ainda precisa migrar."""
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict) or worker.get("enabled") is False:
            continue
        if not _is_termux_bootstrap_worker(worker):
            continue
        roles = {str(item).strip().lower() for item in worker.get("roles") or []}
        caps = roles | {str(item).strip().lower() for item in worker.get("capabilities") or []}
        tasks = {str(item).replace("-", "_") for item in worker.get("supported_tasks") or []}
        if "phone-worker" not in caps or (tasks and "worker_update" not in tasks):
            continue
        if _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            return True
    return False


def _apk_needs_build(version_code: int, source_sha: str) -> bool:
    if _manifest_version_code() < int(version_code or 0):
        return True
    manifest_source = _manifest_source_sha()
    if source_sha and not manifest_source:
        return True
    return bool(source_sha and manifest_source and manifest_source != source_sha)


def _registry_raw() -> dict[str, Any]:
    try:
        path = get_core_workers_registry().path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _active_job_exists(*, job_type: str, target_worker_id: str = "", summary_contains: str = "") -> bool:
    # snapshot() limpa leases expirados antes da leitura. Se o lock estiver ocupado,
    # caímos no JSON cru, mas ainda ignoramos jobs obviamente velhos. Isso evita
    # build APK ficar travado por horas quando o phone worker caiu depois do Gradle.
    with contextlib.suppress(Exception):
        get_core_workers_registry().snapshot(lock_timeout_seconds=0.4)
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    wanted = str(job_type or "").replace("-", "_")
    target_worker_id = str(target_worker_id or "")
    summary_contains = str(summary_contains or "")
    now = time.time()
    apk_running_grace = max(300, int(os.getenv("CORE_WORKER_APK_BUILD_STALE_RUNNING_SECONDS", "1500") or 1500))
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "queued")
        if status not in {"queued", "running"}:
            continue
        kind = str(job.get("type") or "").replace("-", "_")
        if kind != wanted:
            continue
        expires_at = float(job.get("expires_at") or 0.0)
        lease_until = float(job.get("lease_until") or 0.0)
        updated_at = float(job.get("updated_at") or job.get("started_at") or job.get("created_at") or 0.0)
        if expires_at and expires_at <= now:
            continue
        if status == "running" and lease_until and lease_until <= now:
            continue
        if kind == "apk_build_debug" and status == "running" and updated_at and now - updated_at > apk_running_grace:
            continue
        if target_worker_id and str(job.get("target_worker_id") or job.get("worker_id") or "") not in {target_worker_id, ""}:
            continue
        if summary_contains and summary_contains not in str(job.get("summary") or ""):
            continue
        return True
    return False


def _worker_supports(worker: dict[str, Any], task: str, required_capability: str = "phone-worker") -> bool:
    if not worker.get("online"):
        return False
    roles = {str(item) for item in worker.get("roles") or []}
    caps = {str(item) for item in worker.get("capabilities") or []} | roles
    tasks = {str(item).replace("-", "_") for item in worker.get("supported_tasks") or []}
    if required_capability and required_capability not in caps:
        return False
    return not tasks or task in tasks


def _worker_power_blocked(worker: dict[str, Any] | None, *, minimum_percent: int = APK_BUILD_MIN_BATTERY_PERCENT) -> bool:
    if not isinstance(worker, dict):
        return False
    battery = worker.get("battery") if isinstance(worker.get("battery"), dict) else {}
    if not battery:
        return False
    level = battery.get("level")
    if level is None:
        level = battery.get("percent")
    if level is None:
        level = battery.get("percentage")
    try:
        percent = float(level)
    except (TypeError, ValueError):
        return False
    charging = battery.get("charging")
    if not isinstance(charging, bool):
        status = str(battery.get("status") or "").strip().lower()
        plugged = str(battery.get("plugged") or "").strip().lower()
        if status in {"charging", "full"}:
            charging = True
        elif plugged and plugged not in {"unplugged", "none", "unknown"}:
            charging = True
        elif status or plugged:
            charging = False
        else:
            return False
    return percent < float(minimum_percent) and charging is not True


def _worker_apk_builder_status(worker: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(worker, dict):
        return {}
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    for container in (status, health, worker):
        value = container.get("apk_self_builder") if isinstance(container, dict) else None
        if isinstance(value, dict):
            return value
    return {}


def _worker_toolchain_fingerprint(worker: dict[str, Any] | None) -> str:
    preflight = _worker_apk_builder_status(worker)
    for value in (
        preflight.get("toolchainReleaseFingerprint"),
        preflight.get("toolchainFingerprint"),
        (preflight.get("toolchain") or {}).get("releaseFingerprint") if isinstance(preflight.get("toolchain"), dict) else "",
        (preflight.get("smoke") or {}).get("fingerprint") if isinstance(preflight.get("smoke"), dict) else "",
    ):
        clean = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", clean):
            return clean
    return ""


def _select_apk_builder(
    snapshot: dict[str, Any], *, target_agent_version: str, target_agent_source_hash: str
) -> dict[str, Any]:
    """Escolhe um builder concreto; runtime errado nunca recebe o build por acaso."""
    workers = [item for item in snapshot.get("workers") or [] if isinstance(item, dict)]
    apk_candidates: list[dict[str, Any]] = []
    termux_candidates: list[dict[str, Any]] = []
    for worker in workers:
        if worker.get("enabled") is False or not worker.get("online"):
            continue
        if not _worker_supports(worker, "apk_build_debug", "apk-builder"):
            continue
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            continue
        if _worker_power_blocked(worker):
            continue
        runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
        source = str(worker.get("source") or "").strip().lower()
        is_apk = runtime_kind == "apk" or source.startswith("core-worker-apk")
        if is_apk:
            preflight = _worker_apk_builder_status(worker)
            app_code = int(preflight.get("appVersionCode") or worker.get("appVersionCode") or worker.get("versionCode") or 0)
            fingerprint = _worker_toolchain_fingerprint(worker)
            if not preflight.get("ready") or app_code < 127 or not fingerprint:
                continue
            apk_candidates.append({
                "worker": worker,
                "worker_id": worker_id,
                "runtime_kind": "apk",
                "physical_worker_id": str(worker.get("physical_worker_id") or worker.get("parent_worker_id") or worker_id),
                "toolchain_fingerprint": fingerprint,
                "app_version_code": app_code,
                "rank": (float(worker.get("last_seen") or 0), worker_id),
            })
            continue
        if not _is_termux_bootstrap_worker(worker):
            continue
        if _version_tuple(worker.get("version")) < _version_tuple(target_agent_version):
            continue
        expected_hash = str(target_agent_source_hash or "").strip().lower()
        if expected_hash and _worker_source_hash(worker) != expected_hash:
            continue
        termux_candidates.append({
            "worker": worker,
            "worker_id": worker_id,
            "runtime_kind": "termux",
            "physical_worker_id": worker_id,
            "toolchain_fingerprint": _worker_toolchain_fingerprint(worker),
            "agent_source_hash": _worker_source_hash(worker),
            "rank": (float(worker.get("last_seen") or 0), worker_id),
        })

    # Quando o APK já possui toolchain validado, ele recebe a oportunidade de
    # homologar o self-build. O Termux não é removido: continua como fallback.
    if apk_candidates:
        return sorted(apk_candidates, key=lambda item: item["rank"], reverse=True)[0]
    if termux_candidates:
        return sorted(termux_candidates, key=lambda item: item["rank"], reverse=True)[0]
    return {}


def _is_termux_bootstrap_worker(worker: dict[str, Any]) -> bool:
    source = str(worker.get("source") or "").strip().lower()
    runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
    runtime_mode = str(worker.get("runtime_mode") or "").strip().lower()
    roles = {str(item).strip().lower() for item in worker.get("roles") or []}
    return (
        runtime_kind == "termux"
        or source.startswith("termux-")
        or "termux" in source
        or runtime_mode == "termux"
        or ("phone-worker" in roles and not source.startswith("core-worker-apk"))
    )


def _bootstrap_worker_id_for_runtime(snapshot: dict[str, Any], worker_id: str) -> str:
    """Mapeia o runtime APK filho de volta ao Termux do mesmo celular."""
    wanted = str(worker_id or "").strip()
    if not wanted:
        return ""
    workers = [item for item in snapshot.get("workers") or [] if isinstance(item, dict)]
    selected = next((item for item in workers if str(item.get("worker_id") or "") == wanted), None)
    if isinstance(selected, dict):
        parent = str(selected.get("parent_worker_id") or selected.get("physical_worker_id") or "").strip()
        if parent:
            parent_record = next((item for item in workers if str(item.get("worker_id") or "") == parent), None)
            if isinstance(parent_record, dict) and _is_termux_bootstrap_worker(parent_record):
                return parent
        if _is_termux_bootstrap_worker(selected):
            return wanted
    return wanted






def _task_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace(';', ',').split(',')
    else:
        raw_items = []
    result: set[str] = set()
    for item in raw_items:
        clean = re.sub(r"[^a-z0-9_]+", "_", str(item or "").strip().lower().replace('-', '_')).strip('_')
        if clean:
            result.add(clean)
    return result

def _direct_phone_worker_config() -> dict[str, str]:
    enabled = _env_bool("PHONE_WORKER_ENABLED", True)
    host = str(os.getenv("PHONE_WORKER_HOST") or os.getenv("CORE_WORKER_PHONE_HOST") or "").strip()
    port = str(os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
    scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
    token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
    return {"enabled": "1" if enabled else "0", "host": host, "port": port, "scheme": scheme, "token": token}


def _direct_phone_worker_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    cfg = _direct_phone_worker_config()
    if cfg["enabled"] != "1" or not cfg["host"]:
        return {"ok": False, "skipped": True, "summary": "phone-worker direto não configurado"}
    url = f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}{path}"
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as resp:
            raw = resp.read()
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        return parsed if isinstance(parsed, dict) else {"ok": False, "summary": "resposta direta não é JSON object"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "status": exc.code, "summary": f"HTTP {exc.code}: {_short(body, 160)}"}
    except Exception as exc:
        return {"ok": False, "summary": f"{type(exc).__name__}: {_short(exc, 160)}"}


def _direct_phone_worker_update_if_needed(payload: dict[str, Any], target_version: str, *, force: bool = False) -> dict[str, Any]:
    status = _direct_phone_worker_request("/status", timeout=2.5)
    if not bool(status.get("ok", True)):
        return {"ok": False, "skipped": True, "summary": f"phone-worker direto indisponível: {_short(status.get('summary') or status.get('error'), 160)}"}
    source = str(status.get("source") or "").strip().lower()
    runtime_mode = str(status.get("runtime_mode") or "").strip().lower()
    if source.startswith("core-worker-apk") or runtime_mode.startswith("apk-"):
        return {
            "ok": False,
            "skipped": True,
            "port_conflict": True,
            "summary": "porta direta responde ao APK; worker_update continua reservado ao Termux bootstrap",
        }
    current_version = str(status.get("version") or "")
    worker_id = str(status.get("worker_id") or "").strip()
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    current_source_hash = _worker_source_hash(status)
    supported = _task_set(status.get("supported_tasks"))
    if supported and "worker_update" not in supported:
        return {"ok": False, "skipped": True, "current_version": current_version, "summary": "phone-worker direto não declara worker_update"}
    if not _worker_needs_agent_update(status, target_version, target_source_hash, force=force):
        return {
            "ok": True,
            "skipped": True,
            "current_version": current_version,
            "current_source_hash": current_source_hash,
            "worker_id": worker_id,
            "target_version": target_version,
            "target_source_hash": target_source_hash,
            "summary": f"phone-worker direto já está em {current_version} com a fonte esperada",
        }
    if not _worker_has_recovery_bootstrap(status):
        return {
            "ok": False,
            "skipped": True,
            "bootstrap_required": True,
            "current_version": current_version,
            "worker_id": worker_id,
            "summary": "bootstrap_required: execute repair-phone-worker.sh uma vez",
        }
    body = _build_worker_update_artifact_payload(payload)
    body["task"] = "worker_update"
    body.setdefault("source", "vps-updater-direct")
    result = _direct_phone_worker_request("/task", payload=body, timeout=45.0)
    result.setdefault("current_version", current_version)
    result.setdefault("current_source_hash", current_source_hash)
    result.setdefault("worker_id", worker_id)
    result.setdefault("target_version", target_version)
    result.setdefault("target_source_hash", target_source_hash)
    if result.get("ok") is False:
        result.setdefault("summary", "update direto falhou")
    else:
        result.setdefault("summary", f"update direto enviado para {target_version}")
    return result

def _worker_needs_boot_repair(worker: dict[str, Any]) -> bool:
    if not worker.get("online"):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    boot = status.get("boot") if isinstance(status.get("boot"), dict) else {}
    if not boot and isinstance(health.get("boot"), dict):
        boot = health.get("boot")
    if boot and boot.get("ok") is False:
        return True
    if not boot and health.get("boot_ok") is False:
        return True
    scripts = status.get("scripts") if isinstance(status.get("scripts"), dict) else {}
    installs = scripts.get("installations") if isinstance(scripts.get("installations"), dict) else {}
    if installs.get("has_active_duplicates"):
        return True
    return False


def queue_boot_repairs(*, only_worker_id: str = "") -> dict[str, Any]:
    if not _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True):
        return {"ok": True, "skipped": "termux_bootstrap_builder_disabled", "queued": 0, "workers": []}
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if only_worker_id and worker_id != only_worker_id:
            continue
        if not _worker_needs_boot_repair(worker):
            skipped.append(f"{name}: boot ok")
            continue
        if not _worker_supports(worker, "boot_repair", "phone-worker"):
            skipped.append(f"{name}: sem suporte/offline")
            continue
        if _active_job_exists(job_type="boot_repair", target_worker_id=worker_id):
            skipped.append(f"{name}: boot_repair já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="boot_repair",
                payload={"auto": True, "source": "vps-updater", "reason": "boot incompleto ou duplicata ativa"},
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=900,
                lease_seconds=120,
                max_attempts=2,
                summary="auto-repair boot Core Worker",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    return {"ok": True, "queued": queued, "skipped": skipped[:16], "errors": errors[:10]}


def queue_agent_updates(*, force: bool = False, only_worker_id: str = "") -> dict[str, Any]:
    if not _env_bool("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", True):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "skipped": "termux_bootstrap_builder_disabled", "queued": 0, "workers": []}
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    requested_worker_id = str(only_worker_id or "").strip()
    bootstrap_worker_id = _bootstrap_worker_id_for_runtime(snapshot, requested_worker_id) if requested_worker_id else ""
    payload = _build_worker_update_payload()
    # O target persistente é publicado antes de olhar workers/jobs. Assim um
    # telefone offline durante o deploy sempre encontra a fonte correta ao voltar.
    published_release = _publish_phone_worker_release(payload)
    target_version = str(payload.get("version") or "desconhecida")
    target_source_hash = str(payload.get("source_hash") or "").strip().lower()
    artifact_payload: dict[str, Any] | None = None

    def payload_for(worker: dict[str, Any]) -> dict[str, Any]:
        nonlocal artifact_payload
        if not _worker_has_recovery_bootstrap(worker):
            raise RuntimeError("bootstrap_required: execute repair-phone-worker.sh uma vez")
        if artifact_payload is None:
            artifact_payload = {
                "version": published_release["version"],
                "source_hash": published_release["source_hash"],
                "restart": bool(payload.get("restart", True)),
                "auto": bool(payload.get("auto", True)),
                "source": payload.get("source") or "vps-updater",
                "update_transport": "bootstrap-manifest-v2",
                "bootstrap_manifest": True,
                "manifest_url": f"{_public_base_url().rstrip('/')}/core-worker/agent/latest",
                "release_url": published_release["url"],
                "release_sha256": published_release["sha256"],
                "release_bytes": published_release["bytes"],
                "min_bootstrap_version": published_release["min_bootstrap_version"],
            }
        return artifact_payload

    direct_update = {}
    direct_target = next((w for w in workers if str(w.get("worker_id") or "") == bootstrap_worker_id), None) if bootstrap_worker_id else None
    if not requested_worker_id or (isinstance(direct_target, dict) and _is_termux_bootstrap_worker(direct_target)):
        direct_update = _direct_phone_worker_update_if_needed(payload, target_version, force=force)

    pending = _load_pending()
    pending["agent_update"] = {
        "type": "worker_update",
        "target_version": target_version,
        "target_source_hash": target_source_hash,
        "pending": True,
        "force_same_version": bool(force),
        "created_at": float((pending.get("agent_update") or {}).get("created_at") or time.time()) if isinstance(pending.get("agent_update"), dict) else time.time(),
        "updated_at": time.time(),
        "message": "agent update pendente; será aplicado quando workers compatíveis aparecerem",
    }
    _save_pending(pending)

    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if bootstrap_worker_id and worker_id != bootstrap_worker_id:
            continue
        if not _is_termux_bootstrap_worker(worker):
            skipped.append(f"{name}: runtime APK não recebe worker_update")
            continue
        if not _worker_has_recovery_bootstrap(worker):
            skipped.append(f"{name}: bootstrap_required; execute repair-phone-worker.sh uma vez")
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
            skipped.append(f"{name}: incompatível/offline")
            continue
        current_version = str(worker.get("version") or "")
        if not _worker_needs_agent_update(worker, target_version, target_source_hash, force=force):
            skipped.append(f"{name}: já em {current_version} com a fonte esperada")
            continue
        direct_worker_id = str(direct_update.get("worker_id") or "").strip()
        if (
            bool(direct_update.get("ok"))
            and not bool(direct_update.get("skipped"))
            and direct_worker_id == worker_id
        ):
            skipped.append(f"{name}: update direto enviado")
            continue
        if _active_job_exists(job_type="worker_update", target_worker_id=worker_id, summary_contains=target_version):
            skipped.append(f"{name}: job já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="worker_update",
                payload=payload_for(worker),
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=1800,
                lease_seconds=240,
                max_attempts=2,
                summary=f"auto-update agent {target_version}",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    # Só limpe quando nenhum Termux registrado precisar da fonte. Um celular
    # offline mantém a pendência sem job; ao voltar, o primeiro poll retoma o
    # bootstrap em vez de depender do scan ocioso de 15 minutos.
    refreshed_snapshot = _load_registry_snapshot()
    direct_sent = bool(direct_update.get("ok")) and not bool(direct_update.get("skipped"))
    current_pending = _load_pending()
    pending_item = current_pending.get("agent_update") if isinstance(current_pending.get("agent_update"), dict) else {}
    if pending_item:
        bootstrap_required = bool(direct_update.get("bootstrap_required")) or any("bootstrap_required" in item for item in skipped)
        pending_item["phase"] = (
            "waiting_device"
            if bootstrap_required
            else (
                "job_queued"
                if queued or direct_sent or any("job já pendente" in item for item in skipped)
                else "waiting_worker"
            )
        )
        if bootstrap_required:
            pending_item["requires_manual_bootstrap"] = True
            pending_item["block_reason"] = "initial_bootstrap_required"
            pending_item["message"] = "device requer o resgate inicial por repair-phone-worker.sh; depois o pull automático assume"
        else:
            pending_item.pop("requires_manual_bootstrap", None)
            pending_item.pop("block_reason", None)
        pending_item["updated_at"] = time.time()
        current_pending["agent_update"] = pending_item
        _save_pending(current_pending)
    if (
        not queued
        and not errors
        and not _registered_workers_need_agent_version(
            refreshed_snapshot,
            target_version,
            target_source_hash,
            force=force,
        )
    ):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "target_version": target_version, "target_source_hash": target_source_hash, "queued": [], "skipped": skipped[:16], "errors": [], "pending": False, "direct_update": direct_update, "message": "todos os agents ativos já estão atualizados"}
    return {"ok": True, "target_version": target_version, "target_source_hash": target_source_hash, "queued": queued, "skipped": skipped[:16], "errors": errors[:10], "pending": True, "direct_update": direct_update}

_APK_BUILD_TRANSIENT_ERROR_RE = re.compile(
    r"(outofmemoryerror|java heap space|gc overhead|killed process|signal 9|(?:exit|code) 137|"
    r"cannot allocate memory|resource temporarily unavailable|no space left on device|"
    r"preflight_blocked.*(?:mem|espaço|bateria|temperatura)|thermal|overheat|"
    r"timed? out|timeout|connection (?:reset|refused|aborted)|temporary failure|"
    r"network is unreachable|name or service not known|http 5\d\d|builder.*(?:busy|ocupado)|"
    r"build lock|lease.*(?:expired|perd)|falha publicando|broken pipe)",
    re.IGNORECASE,
)
_APK_BUILD_DETERMINISTIC_ERROR_RE = re.compile(
    r"(cannot find symbol|unclosed string literal|compilation failed|compiledebugjavawithjavac|"
    r"android resource linking failed|resource .+ not found|error: resource|manifest merger failed|"
    r"processdebugmainmanifest|google-services\.json.*(?:ausente|inválid|incompat)|"
    r"assinatura.*(?:incompat|diverg)|keystore.*(?:ausente|inválid|diverg)|"
    r"arquivo obrigatório ausente|source.*(?:fingerprint|sha256).*diverg|"
    r"toolchain.*manifest.*inválid|schema inválido.*toolchain|matriz de versões.*incompat|"
    r"jdk incompatível|jdk 17 completo não encontrado|gradle wrapper.*(?:ausente|não está fixado)|compileSdk 34 ausente|"
    r"build-tools 34\.0\.0 ausente|depend[êe]ncia elf obrigat[oó]ria ausente|"
    r"could not find or load main class.*-xmx|classnotfoundexception:.*-xmx|"
    r"default_jvm_opts.*inválid|launcher gradle.*(?:inesperado|inválid|não portátil)|"
    r"syntax error|cmake error|ninja:.*(?:error|failed)|clang: error)",
    re.IGNORECASE,
)


def _apk_build_job_matches_source(job: dict[str, Any], version_name: str, source_fingerprint: str) -> bool:
    short_fp = str(source_fingerprint or "")[:12]
    haystacks: list[str] = []
    for key in ("summary", "error"):
        value = job.get(key)
        if value:
            haystacks.append(str(value))
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for obj in (job, payload, result):
        for key in ("versionName", "version_name", "versionCode", "version_code", "sourceFingerprint", "source_fingerprint", "sourceSha256", "source_sha256", "notificationId", "notification_id"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if value:
                haystacks.append(str(value))
    joined = " ".join(haystacks)
    has_version = not version_name or version_name in joined
    has_fp = not short_fp or short_fp in joined or str(source_fingerprint or "") in joined
    return bool(has_version and has_fp)


def _apk_build_failure_detail(job: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("summary", "error"):
        value = job.get(key)
        if value:
            pieces.append(str(value))
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for key in ("summary", "error", "stderr_tail", "stdout_tail", "gradle_log_tail", "tail", "message", "gradle_error_detail"):
        value = result.get(key) if isinstance(result, dict) else None
        if value:
            pieces.append(str(value))
    return "\n".join(pieces)


def _apk_build_failure_classification(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    explicit = str(result.get("failure_category") or result.get("failureCategory") or job.get("failure_category") or "").strip().lower()
    detail = _apk_build_failure_detail(job)
    # transient/deterministic explícitos são autoritativos. ``unknown`` não é:
    # versões anteriores do agent podem não conhecer uma falha determinística
    # nova, então a VPS deve poder promovê-la pela mensagem sem criar loop.
    if explicit in {"transient", "deterministic"}:
        category = explicit
    elif detail and _APK_BUILD_TRANSIENT_ERROR_RE.search(detail):
        category = "transient"
    elif detail and _APK_BUILD_DETERMINISTIC_ERROR_RE.search(detail):
        category = "deterministic"
    else:
        category = "unknown"
    return {
        "category": category,
        "retryable": category != "deterministic",
        "permanent": category == "deterministic",
    }


def _apk_build_failure_is_permanent(job: dict[str, Any]) -> bool:
    return bool(_apk_build_failure_classification(job).get("permanent"))


def _job_failure_context(job: dict[str, Any]) -> dict[str, str]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    builder_environment = result.get("builder_environment") if isinstance(result.get("builder_environment"), dict) else {}
    self_builder = builder_environment.get("self_builder_toolchain") if isinstance(builder_environment.get("self_builder_toolchain"), dict) else {}
    return {
        "agent_source_hash": str(
            payload.get("requiredAgentSourceHash") or result.get("phoneWorkerSourceHash") or job.get("requiredAgentSourceHash") or ""
        ).strip().lower(),
        "toolchain_fingerprint": str(
            payload.get("toolchainFingerprint")
            or result.get("toolchainFingerprint")
            or self_builder.get("toolchainFingerprint")
            or job.get("toolchainFingerprint")
            or ""
        ).strip().lower(),
        "builder_worker_id": str(payload.get("selectedBuilderWorkerId") or job.get("selectedBuilderWorkerId") or job.get("target_worker_id") or job.get("worker_id") or "").strip(),
        "builder_runtime_kind": str(payload.get("selectedBuilderRuntimeKind") or job.get("selectedBuilderRuntimeKind") or "").strip().lower(),
    }


def _failure_context_matches(job: dict[str, Any], *, agent_source_hash: str, toolchain_fingerprint: str, builder_worker_id: str) -> bool:
    current = _job_failure_context(job)
    expected_agent = str(agent_source_hash or "").strip().lower()
    expected_toolchain = str(toolchain_fingerprint or "").strip().lower()
    expected_builder = str(builder_worker_id or "").strip()
    # Ausência nos jobs antigos significa contexto desconhecido: não deixe uma
    # falha do builder antigo bloquear a nova matriz de agent/toolchain.
    if expected_agent and current["agent_source_hash"] != expected_agent:
        return False
    if expected_toolchain and current["toolchain_fingerprint"] != expected_toolchain:
        return False
    if expected_builder and current["builder_worker_id"] and current["builder_worker_id"] != expected_builder:
        return False
    return True


def _recent_failed_apk_build(
    version_name: str,
    source_fingerprint: str,
    *,
    agent_source_hash: str = "",
    toolchain_fingerprint: str = "",
    builder_worker_id: str = "",
    cooldown_seconds: int | None = None,
) -> dict[str, Any]:
    cooldown = max(60, int(cooldown_seconds or int(os.getenv("CORE_WORKER_APK_BUILD_FAILURE_COOLDOWN_SECONDS", "1800"))))
    now = time.time()
    try:
        snapshot = _load_registry_snapshot()
        raw_jobs = snapshot.get("jobs")
        if isinstance(raw_jobs, dict):
            jobs = [j for j in raw_jobs.values() if isinstance(j, dict)]
        else:
            jobs = raw_jobs if isinstance(raw_jobs, list) else []
    except Exception:
        jobs = []
    matching: list[tuple[float, dict[str, Any]]] = []
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("type") or "") != "apk_build_debug":
            continue
        if not _apk_build_job_matches_source(job, version_name, source_fingerprint):
            continue
        if not _failure_context_matches(
            job,
            agent_source_hash=agent_source_hash,
            toolchain_fingerprint=toolchain_fingerprint,
            builder_worker_id=builder_worker_id,
        ):
            continue
        updated = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or 0)
        matching.append((updated, job))
    matching.sort(key=lambda item: item[0], reverse=True)
    unknown_failures = 0
    for updated, job in matching:
        status = str(job.get("status") or "").lower()
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if status == "succeeded" and result.get("ok") is not False:
            return {}
        if status != "failed":
            continue
        classification = _apk_build_failure_classification(job)
        category = str(classification.get("category") or "unknown")
        if category == "unknown":
            unknown_failures += 1
        detail = _short(_apk_build_failure_detail(job), 240)
        base = {
            "job": job,
            "category": category,
            "cooldown_seconds": cooldown,
            "detail": detail,
            "context": _job_failure_context(job),
        }
        if category == "deterministic":
            return {**base, "retry_after_seconds": 0, "permanent": True, "requires_intervention": True}
        if category == "unknown" and unknown_failures >= 2:
            return {**base, "retry_after_seconds": 0, "permanent": True, "requires_intervention": True, "unknown_retry_exhausted": True}
        if updated and now - updated < cooldown:
            return {
                **base,
                "retry_after_seconds": max(0, int(cooldown - (now - updated))),
                "permanent": False,
            }
        # Uma falha transient após cooldown não bloqueia. Unknown permite exatamente
        # uma segunda tentativa automática; a segunda falha cai no branch acima.
        if category == "transient":
            return {}
        if category == "unknown" and unknown_failures == 1:
            return {}
    return {}



def _recent_built_unpublished_apk(version_name: str, source_fingerprint: str) -> dict[str, Any]:
    """Retorna build recente que gerou APK mas não conseguiu publicar.

    Usado para preferir `apk_publish_last` em vez de rebuildar, especialmente
    quando a rede caiu depois do Gradle ou quando o processo foi interrompido
    antes de reportar publicação.
    """
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    rows: list[tuple[float, dict[str, Any]]] = []
    now = time.time()
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if str(job.get("type") or "").replace("-", "_") != "apk_build_debug":
            continue
        if not _apk_build_job_matches_source(job, version_name, source_fingerprint):
            continue
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if not result:
            continue
        if not result.get("artifact_found") and not result.get("artifact_path"):
            continue
        if result.get("publish_ok") is True and _latest_apk_matches(_read_android_version()[1], source_fingerprint):
            return {}
        updated = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or 0.0)
        if updated and now - updated > 6 * 3600:
            continue
        rows.append((updated, job))
    rows.sort(key=lambda item: item[0], reverse=True)
    if not rows:
        return {}
    job = rows[0][1]
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    apk = result.get("apk") if isinstance(result.get("apk"), dict) else {}
    context = _job_failure_context(job)
    worker_id = str(job.get("worker_id") or job.get("target_worker_id") or context.get("builder_worker_id") or "")
    return {
        "job": job,
        "result": result,
        "worker_id": worker_id,
        "selected_builder_worker_id": str(context.get("builder_worker_id") or worker_id),
        "selected_builder_runtime_kind": str(context.get("builder_runtime_kind") or ""),
        "required_agent_source_hash": str(context.get("agent_source_hash") or ""),
        "toolchain_fingerprint": str(context.get("toolchain_fingerprint") or ""),
        "artifact_path": str(result.get("artifact_path") or apk.get("artifact_path") or ""),
        "filename": str((apk.get("filename") if isinstance(apk, dict) else "") or result.get("filename") or f"CoreWorker-v{version_name}-debug.apk"),
    }


def _stale_running_apk_build_for_source(version_name: str, source_fingerprint: str) -> dict[str, Any]:
    """Detecta build que provavelmente terminou o Gradle mas não reportou/persistiu.

    Se o processo do phone worker caiu entre `assembleDebug` e a publicação, o
    registry pode ficar só com um job `running`. Nesse caso enfileiramos
    `apk_publish_last`; o worker novo recupera o app-debug.apk direto do workdir.
    """
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    now = time.time()
    grace = max(300, int(os.getenv("CORE_WORKER_APK_BUILD_STALE_RUNNING_SECONDS", "900") or 900))
    rows: list[tuple[float, dict[str, Any]]] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if str(job.get("type") or "").replace("-", "_") != "apk_build_debug":
            continue
        if str(job.get("status") or "").lower() != "running":
            continue
        if not _apk_build_job_matches_source(job, version_name, source_fingerprint):
            continue
        updated = float(job.get("updated_at") or job.get("started_at") or job.get("created_at") or 0.0)
        if updated and now - updated < grace:
            continue
        rows.append((updated, job))
    rows.sort(key=lambda item: item[0], reverse=True)
    if not rows:
        return {}
    job = rows[0][1]
    return {
        "job": job,
        "result": {},
        "worker_id": str(job.get("worker_id") or job.get("target_worker_id") or ""),
        "artifact_path": "",
        "filename": f"CoreWorker-v{version_name}-debug.apk",
        "stale_running_build": True,
    }


def _recent_failed_apk_publish_last(*, worker_id: str, version_name: str, source_fingerprint: str, cooldown_seconds: int | None = None) -> dict[str, Any]:
    cooldown = max(60, int(cooldown_seconds or int(os.getenv("CORE_WORKER_APK_PUBLISH_FAILURE_COOLDOWN_SECONDS", "600"))))
    now = time.time()
    rows: list[tuple[float, dict[str, Any]]] = []
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    for job in jobs.values():
        if not isinstance(job, dict) or str(job.get("type") or "").replace("-", "_") != "apk_publish_last":
            continue
        if str(job.get("status") or "").lower() != "failed":
            continue
        if worker_id and str(job.get("worker_id") or job.get("target_worker_id") or "") != worker_id:
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        fp = str(payload.get("sourceFingerprint") or payload.get("sourceSha256") or "")
        if source_fingerprint and fp and fp != source_fingerprint:
            continue
        if version_name and str(payload.get("versionName") or "") not in {"", version_name}:
            continue
        updated = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or 0.0)
        rows.append((updated, job))
    rows.sort(key=lambda item: item[0], reverse=True)
    if not rows:
        return {}
    updated, job = rows[0]
    age = now - updated if updated else cooldown + 1
    if age >= cooldown:
        return {}
    return {
        "job": job,
        "cooldown_seconds": cooldown,
        "retry_after_seconds": max(0, int(cooldown - age)),
        "detail": _short(job.get("error") or ((job.get("result") or {}).get("summary") if isinstance(job.get("result"), dict) else ""), 240),
    }


def _queue_apk_publish_last_from_build(found: dict[str, Any], *, version_name: str, version_code: int, source_fingerprint: str, source_sha256: str, notification_id: str) -> dict[str, Any]:
    registry = get_core_workers_registry()
    worker_id = str(found.get("selected_builder_worker_id") or found.get("worker_id") or "").strip()
    if not worker_id:
        raise RuntimeError("APK persistente sem identidade do builder original")
    runtime_kind = str(found.get("selected_builder_runtime_kind") or "").strip().lower()
    agent_hash = str(found.get("required_agent_source_hash") or "").strip().lower()
    toolchain_fingerprint = str(found.get("toolchain_fingerprint") or "").strip().lower()

    # O target inicial é publicado sem builder para invalidar sources antigas.
    # Se já existe um APK compilado, esse early-return acontecia antes da seleção
    # normal de builder. Persistimos aqui o builder REAL que produziu o artefato
    # antes de enfileirar a republicação; o endpoint /publish pode então validar
    # worker/runtime/agent/toolchain sem afrouxar a política selected-builder-v1.
    desired = _publish_desired_apk_source(
        version_name=version_name,
        version_code=version_code,
        source_fingerprint=source_fingerprint,
        source_sha256=source_sha256,
        selected_builder_worker_id=worker_id,
        selected_builder_runtime_kind=runtime_kind,
        required_agent_source_hash=(agent_hash if runtime_kind == "termux" else ""),
        toolchain_fingerprint=toolchain_fingerprint,
    )
    previous = desired.get("previousRecord") if isinstance(desired.get("previousRecord"), dict) else {}
    builder_was_already_selected = str(previous.get("selectedBuilderWorkerId") or "").strip() == worker_id

    if _active_job_exists(job_type="apk_publish_last", target_worker_id=worker_id, summary_contains=version_name):
        return {"ok": True, "pending": True, "message": "republicação do APK já está na fila", "versionName": version_name, "versionCode": version_code}
    # Se o target JÁ estava corretamente ligado ao mesmo builder e a publicação
    # acabou de falhar, não gere um job novo a cada execução da automação. Quando
    # acabamos de reparar um target antigo sem builder, permitimos uma tentativa
    # imediata para aproveitar o APK já compilado.
    if builder_was_already_selected:
        failed = _recent_failed_apk_publish_last(
            worker_id=worker_id,
            version_name=version_name,
            source_fingerprint=source_fingerprint,
        )
        if failed:
            return {
                "ok": False,
                "pending": False,
                "phase": "publish_blocked",
                "blocked_by_recent_failure": True,
                "retry_after_seconds": failed.get("retry_after_seconds"),
                "last_failed_job_id": (failed.get("job") or {}).get("job_id"),
                "message": "republicação do APK em cooldown após falha recente",
                "versionName": version_name,
                "versionCode": version_code,
            }
    payload = {
        "artifact_path": found.get("artifact_path") or "",
        "versionName": version_name,
        "versionCode": version_code,
        "filename": found.get("filename") or f"CoreWorker-v{version_name}-debug.apk",
        "sourceFingerprint": source_fingerprint,
        "sourceSha256": source_sha256,
        "notificationId": notification_id,
        "notifyUsers": True,
        "notificationRequested": True,
        "selectedBuilderWorkerId": worker_id,
        "selectedBuilderRuntimeKind": runtime_kind,
        "requiredAgentSourceHash": agent_hash,
        "toolchainFingerprint": toolchain_fingerprint,
        "changelog": [
            "APK já compilado pelo worker builder",
            "Republicação automática sem rebuild",
            "O app mostra Atualizar no topo quando estiver disponível",
        ],
    }
    result = registry.create_job(
        job_type="apk_publish_last",
        payload=payload,
        created_by_id=0,
        created_by_name="VPS updater",
        target_worker_id=worker_id,
        required_capabilities=["apk-builder"],
        ttl_seconds=1800,
        lease_seconds=600,
        max_attempts=2,
        summary=f"republicar APK {version_name} {source_fingerprint[:12]}",
    )
    pending = _load_pending()
    pending["apk_build"] = {
        "ok": True,
        "pending": True,
        "type": "apk_publish_last",
        "versionName": version_name,
        "versionCode": version_code,
        "sourceFingerprint": source_fingerprint,
        "sourceSha256": source_sha256,
        "last_job_id": (result.get("job") or {}).get("job_id") if isinstance(result, dict) else None,
        "updated_at": time.time(),
        "message": "APK já compilado; republicação enfileirada sem rebuild",
    }
    _save_pending(pending)
    return {"ok": True, "pending": True, "versionName": version_name, "versionCode": version_code, "job": result.get("job"), "message": "republicação do APK enfileirada"}

def _registry_job_by_id(job_id: str) -> dict[str, Any]:
    clean = str(job_id or "").strip()
    if not clean:
        return {}
    try:
        raw = _registry_raw()
        jobs = raw.get("jobs") if isinstance(raw.get("jobs"), dict) else {}
        job = jobs.get(clean)
        return job if isinstance(job, dict) else {}
    except Exception:
        return {}


def _reconcile_apk_build_pending_job(
    pending: dict[str, Any],
    *,
    version_name: str,
    version_code: int,
    source_fingerprint: str,
) -> dict[str, Any]:
    """Converte o estado visual pendente no estado real do último job.

    O painel não pode continuar exibindo "aguardando worker" depois que o registry
    já marcou o build como failed/succeeded. A reconciliação ocorre antes dos
    cooldowns para uma falha encerrada nunca parecer job ativo.
    """
    item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
    job_id = str(item.get("last_job_id") or "").strip()
    job = _registry_job_by_id(job_id)
    if not job or not _apk_build_job_matches_source(job, version_name, source_fingerprint):
        return item
    status = str(job.get("status") or "").strip().lower()
    item["job_status"] = status
    item["last_job_id"] = str(job.get("job_id") or job_id)
    item["last_job_updated_at"] = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or 0.0)
    item.pop("blocked_by_recent_queue", None)

    if status in {"queued", "running"}:
        item.update({
            "ok": True,
            "pending": True,
            "phase": "running" if status == "running" else "queued",
            "message": "build do APK em execução no worker builder" if status == "running" else "build do APK na fila do worker builder",
            "updated_at": time.time(),
        })
        item.pop("blocked_by_recent_failure", None)
        item.pop("last_failure_detail", None)
        item.pop("permanent_failure", None)
        item.pop("retry_after_seconds", None)
        item.pop("error", None)
        return item

    if status == "failed":
        detail = _short(_apk_build_failure_detail(job), 360)
        updated = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or time.time())
        cooldown = max(60, int(os.getenv("CORE_WORKER_APK_BUILD_FAILURE_COOLDOWN_SECONDS", "1800") or 1800))
        retry_after = max(0, int(cooldown - max(0.0, time.time() - updated)))
        item.update({
            "ok": False,
            "pending": False,
            "phase": "failed",
            "blocked_by_recent_failure": True,
            "last_failed_job_id": str(job.get("job_id") or job_id),
            "last_failure_detail": detail,
            "error": detail,
            "permanent_failure": _apk_build_failure_is_permanent(job),
            "retry_after_seconds": retry_after,
            "message": "build do APK falhou no worker; veja o detalhe e use retry manual após a correção",
            "updated_at": time.time(),
        })
        return item

    if status == "succeeded":
        if _latest_apk_matches(version_code, source_fingerprint):
            pending.pop("apk_build", None)
            _save_pending(pending)
            return {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        publish_ok = result.get("publish_ok") is True
        item.update({
            "ok": True,
            "pending": not publish_ok,
            "phase": "published" if publish_ok else "publish_pending",
            "message": "APK compilado e publicado" if publish_ok else "APK compilado; publicação na VPS ainda pendente",
            "updated_at": time.time(),
        })
        item.pop("blocked_by_recent_failure", None)
        item.pop("last_failure_detail", None)
        item.pop("permanent_failure", None)
        item.pop("retry_after_seconds", None)
        item.pop("error", None)
        return item

    if status in {"cancelled", "expired"}:
        item.update({
            "ok": False,
            "pending": False,
            "phase": status,
            "error": f"job de build {status}",
            "message": f"build do APK {status}; use retry manual",
            "updated_at": time.time(),
        })
    return item


def _pending_apk_build_recently_queued(pending: dict[str, Any], version_code: int, source_fingerprint: str, *, cooldown_seconds: int | None = None) -> dict[str, Any]:
    item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
    if not item:
        return {}
    cooldown = max(60, int(cooldown_seconds or int(os.getenv("CORE_WORKER_APK_BUILD_QUEUE_COOLDOWN_SECONDS", "600"))))
    last_fp = str(item.get("last_queued_source_fingerprint") or item.get("sourceFingerprint") or "")
    last_code = int(item.get("last_queued_versionCode") or item.get("versionCode") or 0)
    last_at = float(item.get("last_queued_at") or 0)
    if not last_at or last_fp != str(source_fingerprint or "") or last_code != int(version_code or 0):
        return {}
    last_job = _registry_job_by_id(str(item.get("last_job_id") or ""))
    last_status = str(last_job.get("status") or "").strip().lower() if last_job else ""
    if last_status in {"failed", "succeeded", "cancelled", "expired"}:
        return {}
    age = time.time() - last_at
    if age < cooldown:
        return {
            "cooldown_seconds": cooldown,
            "retry_after_seconds": max(0, int(cooldown - age)),
            "last_queued_at": last_at,
        }
    return {}


def queue_apk_build(*, manual: bool = False) -> dict[str, Any]:
    registry = get_core_workers_registry()
    version_name, version_code = _read_android_version()
    source = _prepare_apk_source_zip()
    source_fingerprint = str(_current_fingerprints().get("apk_source_hash") or source["sha256"])
    notification_id = f"apk-{version_code}-{source_fingerprint[:12]}"
    desired_source = _publish_desired_apk_source(
        version_name=version_name,
        version_code=version_code,
        source_fingerprint=source_fingerprint,
        source_sha256=str(source.get("sha256") or ""),
    )
    try:
        firebase_config = _load_google_services_payload_for_apk_build()
        signing_config = _load_apk_signing_payload_for_worker_build()
    except Exception as exc:
        pending = _load_pending()
        message = "arquivo local necessário ausente/inválido; build do APK não foi enfileirado"
        if "google-services" in str(exc).lower():
            message = "google-services.json local ausente/inválido; build do APK não foi enfileirado"
        elif "keystore" in str(exc).lower() or "assinatura" in str(exc).lower():
            message = "keystore compatível ausente/inválida; build do APK não foi enfileirado"
        pending["apk_build"] = {
            "ok": False,
            "pending": False,
            "versionName": version_name,
            "versionCode": version_code,
            "source": source,
            "error": f"{type(exc).__name__}: {_short(exc, 200)}",
            "updated_at": time.time(),
            "message": message,
        }
        _save_pending(pending)
        return pending["apk_build"]
    payload = {
        "source_zip_url": source["url"],
        "source_sha256": source["sha256"],
        "sourceFingerprint": source_fingerprint,
        "source_bytes": source["bytes"],
        "firebase_config_delivery": source.get("firebase_config_delivery") or "job_payload",
        **firebase_config,
        **signing_config,
        "project_subdir": "android/core-worker-app",
        "selfBuilderRequired": True,
        "publish": True,
        "versionName": version_name,
        "versionCode": version_code,
        "filename": f"CoreWorker-v{version_name}-debug.apk",
        "notifyUsers": True,
        "notificationRequested": True,
        "notificationId": notification_id,
        "coreWorkerVpsUrl": _public_base_url(),
        "coreWorkerVpsLabel": os.getenv("CORE_WORKER_VPS_LABEL") or "VPS principal",
        "changelog": [
            "APK bootstrap compilado no Termux ou atualização compilada pelo próprio APK",
            "A VPS só orquestra e publica o APK pronto",
            "O toolchain de autobuild é obrigatório para concluir a transição",
        ],
    }
    pending = _load_pending()
    previous_apk_pending = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
    pending_item = {
        "type": "apk_build_debug",
        "versionName": version_name,
        "versionCode": version_code,
        "payload_redacted": True,
        "firebase_config_delivery": "job_payload",
        "apk_signing_delivery": "job_payload",
        "source": source,
        "created_at": float(previous_apk_pending.get("created_at") or time.time()) if isinstance(previous_apk_pending, dict) else time.time(),
        "updated_at": time.time(),
        "message": "build do APK pendente; será executado quando um worker apk-builder/turbo estiver online",
    }
    if isinstance(previous_apk_pending, dict):
        for key in ("last_queued_at", "last_queued_versionCode", "last_queued_source_fingerprint", "last_job_id", "retry_after_agent_update"):
            if key in previous_apk_pending:
                pending_item[key] = previous_apk_pending[key]
    pending["apk_build"] = pending_item
    reconciled = _reconcile_apk_build_pending_job(
        pending,
        version_name=version_name,
        version_code=version_code,
        source_fingerprint=source_fingerprint,
    )
    if reconciled:
        pending["apk_build"] = reconciled
    _save_pending(pending)

    if not _apk_needs_build(version_code, source_fingerprint):
        pending.pop("apk_build", None)
        _save_pending(pending)
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "already_published": True, "sourceSha256": source.get("sha256"), "sourceFingerprint": source_fingerprint, "message": "latest.json já está publicado nessa versão/source"}
    built_unpublished = _recent_built_unpublished_apk(version_name, source_fingerprint)
    if not built_unpublished:
        built_unpublished = _stale_running_apk_build_for_source(version_name, source_fingerprint)
    if built_unpublished:
        try:
            return _queue_apk_publish_last_from_build(
                built_unpublished,
                version_name=version_name,
                version_code=version_code,
                source_fingerprint=source_fingerprint,
                source_sha256=source.get("sha256") or "",
                notification_id=notification_id,
            )
        except Exception as exc:
            # Se a republicação não pôde ser enfileirada, seguimos para o build normal.
            item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
            item.update({"publish_retry_error": f"{type(exc).__name__}: {_short(exc, 160)}", "updated_at": time.time()})
            pending["apk_build"] = item
            _save_pending(pending)

    target_agent_version = _read_phone_worker_version()
    target_agent_source_hash = _hash_phone_worker_files(ROOT / "deploy" / "termux" / "phone-worker")
    snapshot = _load_registry_snapshot()
    builder = _select_apk_builder(
        snapshot,
        target_agent_version=target_agent_version,
        target_agent_source_hash=target_agent_source_hash,
    )
    if not builder:
        workers = [item for item in snapshot.get("workers") or [] if isinstance(item, dict)]
        waiting_agent = _registered_workers_need_agent_version(
            snapshot, target_agent_version, target_agent_source_hash
        )
        waiting_toolchain = False
        waiting_power = False
        for worker in workers:
            if worker.get("enabled") is False or not worker.get("online"):
                continue
            if _worker_supports(worker, "apk_build_debug", "apk-builder") and _worker_power_blocked(worker):
                waiting_power = True
                continue
            runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
            source_kind = str(worker.get("source") or "").strip().lower()
            is_apk = runtime_kind == "apk" or source_kind.startswith("core-worker-apk")
            if is_apk:
                preflight = _worker_apk_builder_status(worker)
                if not preflight.get("ready"):
                    waiting_toolchain = True
                    break
            elif _is_termux_bootstrap_worker(worker):
                if (
                    _version_tuple(worker.get("version")) >= _version_tuple(target_agent_version)
                    and (not target_agent_source_hash or _worker_source_hash(worker) == target_agent_source_hash)
                    and not _worker_supports(worker, "apk_build_debug", "apk-builder")
                ):
                    waiting_toolchain = True
                    break
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        phase = "waiting_agent" if waiting_agent else ("waiting_power" if waiting_power else ("waiting_toolchain" if waiting_toolchain else "waiting_builder"))
        messages = {
            "waiting_agent": "build do APK aguardando o Termux executar exatamente o agent requerido",
            "waiting_power": "build do APK aguardando carga suficiente ou carregador conectado",
            "waiting_toolchain": "build do APK aguardando toolchain validado e smoke do builder",
            "waiting_builder": "build do APK aguardando um builder compatível ficar online",
        }
        item.update({
            "ok": True,
            "pending": True,
            "transient": True,
            "error": "",
            "phase": phase,
            "requiredAgentVersion": target_agent_version,
            "requiredAgentSourceHash": target_agent_source_hash,
            "desiredSource": (desired_source.get("record") if isinstance(desired_source, dict) else {}),
            "updated_at": time.time(),
            "message": messages[phase],
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item

    selected_worker_id = str(builder.get("worker_id") or "")
    selected_runtime_kind = str(builder.get("runtime_kind") or "")
    selected_toolchain_fingerprint = str(builder.get("toolchain_fingerprint") or "")
    selected_physical_worker_id = str(builder.get("physical_worker_id") or selected_worker_id)
    desired_source = _publish_desired_apk_source(
        version_name=version_name,
        version_code=version_code,
        source_fingerprint=source_fingerprint,
        source_sha256=str(source.get("sha256") or ""),
        selected_builder_worker_id=selected_worker_id,
        selected_builder_runtime_kind=selected_runtime_kind,
        required_agent_source_hash=(target_agent_source_hash if selected_runtime_kind == "termux" else ""),
        toolchain_fingerprint=selected_toolchain_fingerprint,
    )
    payload.update({
        "requiredAgentVersion": target_agent_version,
        "requiredAgentSourceHash": target_agent_source_hash,
        "selectedBuilderWorkerId": selected_worker_id,
        "selectedBuilderRuntimeKind": selected_runtime_kind,
        "toolchainFingerprint": selected_toolchain_fingerprint,
        "physicalWorkerId": selected_physical_worker_id,
        "parentWorkerId": selected_physical_worker_id,
    })
    item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
    item.update({
        "selectedBuilderWorkerId": selected_worker_id,
        "selectedBuilderRuntimeKind": selected_runtime_kind,
        "toolchainFingerprint": selected_toolchain_fingerprint,
        "requiredAgentVersion": target_agent_version,
        "requiredAgentSourceHash": target_agent_source_hash,
        "desiredSource": (desired_source.get("record") if isinstance(desired_source, dict) else {}),
        "phase": "queued",
        "updated_at": time.time(),
    })
    pending["apk_build"] = item
    _save_pending(pending)

    failed_recent = {} if manual else _recent_failed_apk_build(
        version_name,
        source_fingerprint,
        agent_source_hash=target_agent_source_hash,
        toolchain_fingerprint=selected_toolchain_fingerprint,
        builder_worker_id=selected_worker_id,
    )
    if failed_recent:
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        item.update({
            "ok": False,
            "pending": False,
            "phase": "failed_deterministic" if failed_recent.get("permanent") else "failed_transient",
            "blocked_by_recent_failure": True,
            "last_failed_job_id": (failed_recent.get("job") or {}).get("job_id"),
            "retry_after_seconds": failed_recent.get("retry_after_seconds"),
            "updated_at": time.time(),
            "permanent_failure": bool(failed_recent.get("permanent")),
            "last_failure_detail": failed_recent.get("detail"),
            "message": "build do APK falhou recentemente; retry automático bloqueado para evitar loop; use retry manual após corrigir o erro",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item
    recent_queue = {} if manual else _pending_apk_build_recently_queued(pending, version_code, source_fingerprint)
    if recent_queue:
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        item.update({
            "ok": True,
            "pending": True,
            "blocked_by_recent_queue": True,
            "retry_after_seconds": recent_queue.get("retry_after_seconds"),
            "updated_at": time.time(),
            "message": "build do APK já foi enfileirado recentemente; aguardando resultado/cooldown para evitar loop",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item
    if _active_job_exists(job_type="apk_build_debug", target_worker_id=selected_worker_id, summary_contains=version_name):
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        item.pop("retry_after_agent_update", None)
        item.update({
            "ok": True,
            "pending": True,
            "phase": "queued",
            "updated_at": time.time(),
            "message": "build do APK já está na fila ou em execução",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item
    try:
        result = registry.create_job(
            job_type="apk_build_debug",
            payload=payload,
            created_by_id=0,
            created_by_name="VPS updater",
            target_worker_id=selected_worker_id,
            required_capabilities=["apk-builder"],
            ttl_seconds=7200,
            lease_seconds=7200,
            max_attempts=1,
            summary=f"build automático APK {version_name} {source_fingerprint[:12]}",
        )
        pending = _load_pending()
        item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        item.pop("retry_after_agent_update", None)
        item.update({
            "ok": True,
            "pending": True,
            "last_queued_at": time.time(),
            "last_queued_versionCode": version_code,
            "last_queued_source_fingerprint": source_fingerprint,
            "last_job_id": (result.get("job") or {}).get("job_id") if isinstance(result.get("job"), dict) else None,
            "selectedBuilderWorkerId": selected_worker_id,
            "selectedBuilderRuntimeKind": selected_runtime_kind,
            "toolchainFingerprint": selected_toolchain_fingerprint,
            "message": f"build do APK enfileirado no builder {selected_worker_id}",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "source": source, "job": result.get("job"), "pending": True}
    except Exception as exc:
        pending = _load_pending()
        item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        transient_no_builder = (
            isinstance(exc, CoreWorkerRegistryError)
            and int(getattr(exc, "status", 0) or 0) == 409
            and "nenhum worker online compatível" in str(exc).lower()
        )
        if transient_no_builder:
            for stale_key in ("blocked_by_recent_failure", "permanent_failure", "retry_after_seconds", "last_failure_detail", "last_failed_job_id"):
                item.pop(stale_key, None)
            item.update({
                "ok": True,
                "pending": True,
                "phase": "waiting_builder",
                "transient": True,
                "error": "",
                "last_enqueue_error": f"{type(exc).__name__}: {exc}",
                "updated_at": time.time(),
                "message": "build do APK aguardando worker apk-builder online compatível",
            })
        else:
            item.update({
                "ok": False,
                "pending": False,
                "phase": "enqueue_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": time.time(),
                "message": "falha ao enfileirar o build do APK",
            })
        pending["apk_build"] = item
        _save_pending(pending)
        return item


def _automation_time_budget_seconds() -> float:
    try:
        return max(5.0, min(180.0, float(os.getenv("CORE_WORKER_AUTOMATION_TIME_BUDGET_SECONDS", "45") or 45)))
    except Exception:
        return 45.0


def _budget_exceeded(started: float) -> bool:
    return (time.monotonic() - started) >= _automation_time_budget_seconds()


def _mark_apk_waiting_for_agent_update(current: dict[str, Any]) -> dict[str, Any]:
    pending = _load_pending()
    previous = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
    item = dict(previous)
    for stale_key in ("blocked_by_recent_failure", "permanent_failure", "retry_after_seconds", "last_failure_detail", "last_failed_job_id"):
        item.pop(stale_key, None)
    item.update({
        "type": "apk_build_debug",
        "ok": True,
        "pending": True,
        "phase": "waiting_worker_update",
        "transient": True,
        "error": "",
        "versionName": str(current.get("apk_versionName") or ""),
        "versionCode": int(current.get("apk_versionCode") or 0),
        "requiredAgentVersion": str(current.get("phone_worker_version") or ""),
        "requiredAgentSourceHash": str(current.get("phone_worker_hash") or ""),
        "retry_after_agent_update": True,
        "created_at": float(previous.get("created_at") or time.time()),
        "updated_at": time.time(),
        "message": "build do APK aguardando o agent corrigido antes de tentar novamente",
    })
    pending["apk_build"] = item
    _save_pending(pending)
    return item


def process_pending(*, worker_id: str = "") -> dict[str, Any]:
    started = time.monotonic()
    pending = _load_pending()
    result: dict[str, Any] = {"ok": True, "worker_id": worker_id, "processed_at": time.time()}
    snapshot = _load_registry_snapshot()
    # process-pending roda a partir de heartbeat/poll/result e precisa ser barato.
    # Não calcule hash_tree do Android a cada heartbeat: em VPS de 1 GB isso gerou
    # CPU alta, /health caro e subprocessos zumbis. O after-update continua fazendo
    # fingerprint completo quando há commit novo; aqui só lidamos com pendências já
    # gravadas, salvo opt-in explícito para autodetectar APK.
    current: dict[str, Any] = {
        "phone_worker_version": _read_phone_worker_version(),
        "phone_worker_hash": _hash_phone_worker_files(ROOT / "deploy" / "termux" / "phone-worker"),
    }
    target_agent = str(current.get("phone_worker_version") or "")
    target_agent_hash = str(current.get("phone_worker_hash") or "")
    version_name, apk_version_code = _read_android_version()
    current["apk_versionName"] = version_name
    current["apk_versionCode"] = apk_version_code
    apk_source_hash = ""

    if _env_bool("CORE_WORKER_AUTO_BOOT_REPAIR_ON_PENDING", False) and not _budget_exceeded(started):
        result["boot_repair"] = queue_boot_repairs(only_worker_id=worker_id)
    else:
        result["boot_repair"] = {"ok": True, "skipped": "disabled_by_default"}

    agent_pending = pending.get("agent_update") if isinstance(pending.get("agent_update"), dict) else {}
    force_agent = bool(agent_pending.get("force_same_version"))
    agent_needed = False if _budget_exceeded(started) else _workers_need_agent_version(
        snapshot,
        target_agent,
        target_agent_hash,
        force=force_agent,
    )
    if not _budget_exceeded(started) and (pending.get("agent_update") or agent_needed):
        result["agent_update"] = queue_agent_updates(force=force_agent, only_worker_id=worker_id)
        if agent_needed:
            result["agent_update_detected"] = {"target_version": target_agent, "target_source_hash": target_agent_hash, "reason": "worker abaixo da versão/hash esperados"}
    elif pending.get("agent_update") or agent_needed:
        result["agent_update"] = {"ok": True, "skipped": "time_budget_exceeded"}

    apk_needed = False
    auto_detect_apk = _env_bool("CORE_WORKER_AUTOMATION_AUTO_DETECT_APK_CHANGES_ON_POLL", False)
    if not _budget_exceeded(started) and auto_detect_apk:
        apk_source_hash = _hash_tree(ROOT / "android" / "core-worker-app", exclude_dirs={"build", ".gradle", "releases"})
        current["apk_source_hash"] = apk_source_hash
        apk_needed = _apk_needs_build(apk_version_code, apk_source_hash)
    apk_requested = bool(pending.get("apk_build") or apk_needed)
    waiting_for_agent_upgrade = bool(agent_needed)
    if not _budget_exceeded(started) and apk_requested and waiting_for_agent_upgrade:
        current_pending = _load_pending()
        item = current_pending.get("apk_build") if isinstance(current_pending.get("apk_build"), dict) else {}
        for stale_key in ("blocked_by_recent_failure", "permanent_failure", "retry_after_seconds", "last_failure_detail", "last_failed_job_id"):
            item.pop(stale_key, None)
        item.update({
            "ok": True,
            "pending": True,
            "phase": "waiting_worker_update",
            "transient": True,
            "error": "",
            "targetWorkerVersion": target_agent,
            "targetWorkerSourceHash": target_agent_hash,
            "retry_after_agent_update": True,
            "updated_at": time.time(),
            "message": f"build do APK aguardando o worker atualizar para {target_agent}",
        })
        current_pending["apk_build"] = item
        _save_pending(current_pending)
        result["apk_build"] = item
    elif not _budget_exceeded(started) and apk_requested:
        apk_pending = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        retry_after_agent_update = bool(apk_pending.get("retry_after_agent_update"))
        result["apk_build"] = queue_apk_build(manual=retry_after_agent_update)
        if retry_after_agent_update:
            result["apk_retry_reason"] = "agent_updated"
        if apk_needed:
            result["apk_build_detected"] = {"versionCode": apk_version_code, "reason": "latest.json ausente/antigo ou source divergente"}
    elif apk_requested:
        result["apk_build"] = {"ok": True, "skipped": "time_budget_exceeded"}

    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    write_status({"process_pending": result, "pending": _load_pending(), "finished_at": time.time()})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result

def write_status(status: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def after_update(force_agent: bool = False) -> int:
    changed = _changed_files_from_env()
    snapshot = _load_registry_snapshot()
    current = _current_fingerprints()
    previous = _load_state()
    phone_hash_changed = bool(previous.get("phone_worker_hash") and previous.get("phone_worker_hash") != current.get("phone_worker_hash"))
    apk_hash_changed = bool(previous.get("apk_source_hash") and previous.get("apk_source_hash") != current.get("apk_source_hash"))
    phone_source_changed = (
        _has_changed(changed, "deploy/termux/phone-worker/")
        or force_agent
        or phone_hash_changed
    )
    workers_need_agent = _workers_need_agent_version(
        snapshot,
        str(current.get("phone_worker_version") or ""),
        str(current.get("phone_worker_hash") or ""),
    )
    phone_changed = bool(phone_source_changed or workers_need_agent)
    apk_changed = (
        _has_changed(changed, "android/core-worker-app/")
        or apk_hash_changed
        or _apk_needs_build(int(current.get("apk_versionCode") or 0), str(current.get("apk_source_hash") or ""))
    )
    status: dict[str, Any] = {
        "ok": True,
        "changed_files": changed[:80],
        "fingerprints": current,
        "previous_fingerprints_present": bool(previous),
        "phone_worker_hash_changed": phone_hash_changed,
        "apk_source_hash_changed": apk_hash_changed,
        "phone_worker_changed": phone_changed,
        "apk_changed": apk_changed,
        "workers_need_agent": workers_need_agent,
        "apk_needs_build": _apk_needs_build(int(current.get("apk_versionCode") or 0), str(current.get("apk_source_hash") or "")),
        "started_at": time.time(),
        "base_url": _public_base_url(),
    }
    agent_auto_enabled = _env_bool("CORE_WORKER_AUTO_AGENT_UPDATE_ENABLED", True)
    apk_auto_enabled = _env_bool("CORE_WORKER_AUTO_APK_BUILD_ENABLED", True)
    if phone_changed and agent_auto_enabled:
        status["agent_update"] = queue_agent_updates(force=bool(force_agent or phone_source_changed))
    elif phone_changed:
        status["agent_update"] = {"ok": True, "skipped": ["desativado por CORE_WORKER_AUTO_AGENT_UPDATE_ENABLED=false"]}

    if apk_changed and apk_auto_enabled:
        if phone_changed and agent_auto_enabled:
            status["apk_build"] = _mark_apk_waiting_for_agent_update(current)
        else:
            status["apk_build"] = queue_apk_build()
    elif apk_changed:
        status["apk_build"] = {"ok": True, "skipped": ["desativado por CORE_WORKER_AUTO_APK_BUILD_ENABLED=false"]}

    status["boot_repair"] = queue_boot_repairs()

    failed_components = [
        key
        for key in ("agent_update", "apk_build", "boot_repair")
        if isinstance(status.get(key), dict)
        and status[key].get("ok") is False
        and not bool(status[key].get("pending"))
    ]
    status["failed_components"] = failed_components
    status["ok"] = not failed_components
    status["finished_at"] = time.time()
    write_status(status)
    state = dict(current)
    state["updated_at"] = status["finished_at"]
    _save_state(state)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    return 0 if status["ok"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Automação pós-update do Core Worker.")
    sub = parser.add_subparsers(dest="command", required=True)
    after = sub.add_parser("after-update")
    after.add_argument("--force-agent", action="store_true")
    sub.add_parser("queue-agent-update")
    sub.add_parser("queue-apk-build")
    sub.add_parser("queue-boot-repair")
    process = sub.add_parser("process-pending")
    process.add_argument("--worker-id", default="")
    args = parser.parse_args()
    if args.command == "after-update":
        return after_update(force_agent=bool(args.force_agent))
    if args.command == "queue-agent-update":
        result = queue_agent_updates(force=True)
        write_status({"manual": True, "agent_update": result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "queue-apk-build":
        result = queue_apk_build(manual=True)
        write_status({"manual": True, "apk_build": result, "pending": _load_pending(), "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "queue-boot-repair":
        result = queue_boot_repairs()
        write_status({"manual": True, "boot_repair": result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "process-pending":
        worker_id = str(getattr(args, "worker_id", "") or "")
        with _process_pending_lock(worker_id) as acquired:
            if not acquired:
                result = {"ok": True, "skipped": "already_running", "worker_id": worker_id, "processed_at": time.time()}
                write_status({"process_pending": result, "finished_at": time.time()})
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                return 0
            result = process_pending(worker_id=worker_id)
            return 0 if result.get("ok") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
