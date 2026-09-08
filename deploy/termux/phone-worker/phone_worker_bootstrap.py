#!/usr/bin/env python3
"""Bootstrap pequeno e independente do Core Phone Worker.

Este módulo é deliberadamente stdlib-only. Ele não serve TTS, música ou HTTP direto;
seu único trabalho é manter o runtime Termux na release publicada pela VPS.

Layout após a migração:
  ~/.core-worker-runtime/releases/<source_hash>/   releases imutáveis
  ~/.core-worker-runtime/current -> releases/...   ponteiro atômico
  ~/.core-worker-runtime/previous -> releases/...  último rollback
  ~/.local/state/core-worker-phone-worker/          pid/status/updater
  ~/.phone-worker.env                              configuração e segredos

O agent principal pode morrer, ficar sem porta HTTP ou ficar desatualizado: o watchdog
continua chamando este bootstrap por saída HTTP para recuperar o target persistente.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import py_compile
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable

BOOTSTRAP_VERSION = "1.0.0"
MANIFEST_SCHEMA = "core-phone-worker-release-v2"
CONFIG_SCHEMA = 2
DEFAULT_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_EXPANDED_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 64
DEFAULT_RESPONSE_MAX_AGE_SECONDS = 300
DEFAULT_VERIFY_TIMEOUT_SECONDS = 45
DEFAULT_RETAIN_RELEASES = 3

LEGACY_SOURCE_HASH_TARGETS = (
    "phone_worker.py",
    "apk_identity.py",
    "tts_transport.py",
    "music_agent.py",
    "start-phone-worker.sh",
    "start-phone-music-agent.sh",
    "watch-phone-worker.sh",
    "pair-phone-worker.sh",
    "bootstrap-phone-worker.sh",
    "install.sh",
    "teto_renderer/__init__.py",
    "teto_renderer/errors.py",
    "teto_renderer/cache.py",
    "teto_renderer/voicebank.py",
    "teto_renderer/phonemizer.py",
    "teto_renderer/prosody.py",
    "teto_renderer/renderer.py",
    "scripts/validate-teto-assets.py",
)
LEGACY_OPTIONAL_SNAPSHOT_TARGETS = ("README.md", "phone-worker.env.example")

SECRET_KEY_RE = re.compile(r"(?:token|secret|password|keystore|google|credential|authorization)", re.I)
SAFE_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _short(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except Exception:
        value = default
    return max(low, min(high, value))


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(parts or [0])


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not SAFE_ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def _atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _atomic_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    _atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=mode)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL da VPS inválida")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower(), int(port)


def _same_origin(left: str, right: str) -> bool:
    try:
        return _origin(left) == _origin(right)
    except Exception:
        return False


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not _same_origin(self.base_url, newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect cross-origin recusado", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _signed_request(
    url: str,
    *,
    base_url: str,
    token: str,
    worker_id: str,
    accept: str,
    timeout: float,
    max_bytes: int,
    output_path: Path | None = None,
) -> tuple[bytes | None, dict[str, str], int, str]:
    if not _same_origin(base_url, url):
        raise ValueError("download de update recusado: origem diferente da VPS pareada")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Core-Worker-Id": worker_id,
        "Accept": accept,
        "User-Agent": f"CorePhoneWorkerBootstrap/{BOOTSTRAP_VERSION}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_SameOriginRedirect(base_url))
    digest = None
    total = 0
    chunks: list[bytes] = []
    out = None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        out = output_path.open("wb")
    try:
        with opener.open(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            timestamp_text = response_headers.get("x-core-worker-timestamp", "")
            schema = response_headers.get("x-core-worker-signature-schema", "")
            if schema != "ts-body-v1":
                raise ValueError("schema da assinatura HMAC não suportado")
            try:
                timestamp = int(timestamp_text)
            except Exception as exc:
                raise ValueError("timestamp autenticado ausente") from exc
            max_age = _env_int("PHONE_WORKER_BOOTSTRAP_RESPONSE_MAX_AGE_SECONDS", DEFAULT_RESPONSE_MAX_AGE_SECONDS, 30, 3600)
            if abs(int(time.time()) - timestamp) > max_age:
                raise ValueError("resposta autenticada expirada")
            digest = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
            digest.update(timestamp_text.encode("ascii"))
            digest.update(b"\n")
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("resposta excede limite do bootstrap")
                assert digest is not None
                digest.update(chunk)
                if out is not None:
                    out.write(chunk)
                else:
                    chunks.append(chunk)
            if out is not None:
                out.flush()
                os.fsync(out.fileno())
    finally:
        if out is not None:
            out.close()
    signature = response_headers.get("x-core-worker-signature", "")
    if signature.lower().startswith("sha256="):
        signature = signature.split("=", 1)[1].strip().lower()
    if digest is None:
        raise ValueError("resposta autenticada sem corpo")
    expected = digest.hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        if output_path is not None:
            with contextlib.suppress(Exception):
                output_path.unlink()
        raise ValueError("assinatura HMAC da resposta inválida")
    return (b"".join(chunks) if out is None else None), response_headers, total, expected


def _runtime_root() -> Path:
    return Path(os.getenv("PHONE_WORKER_RUNTIME_ROOT") or (Path.home() / ".core-worker-runtime")).expanduser()


def _install_root() -> Path:
    return Path(os.getenv("PHONE_WORKER_DIR") or (Path.home() / "phone-worker")).expanduser()


def _state_root() -> Path:
    return Path(os.getenv("PHONE_WORKER_STATE_DIR") or (Path.home() / ".local/state/core-worker-phone-worker")).expanduser()


def _env_path() -> Path:
    return Path(os.getenv("PHONE_WORKER_ENV") or (Path.home() / ".phone-worker.env")).expanduser()


def _status_path() -> Path:
    return Path(os.getenv("PHONE_WORKER_BOOTSTRAP_STATUS_FILE") or (_state_root() / "updater-status.json")).expanduser()


def _runtime_status_path() -> Path:
    return Path(os.getenv("PHONE_WORKER_RUNTIME_STATUS_FILE") or (_state_root() / "runtime-status.json")).expanduser()


def _pid_path() -> Path:
    return Path(os.getenv("PHONE_WORKER_PID_FILE") or (_state_root() / "phone-worker.pid")).expanduser()


def _write_state(state: str, **extra: Any) -> None:
    data = {
        "schema": "core-phone-worker-bootstrap-state-v1",
        "bootstrap_version": BOOTSTRAP_VERSION,
        "state": state,
        "updated_at": time.time(),
    }
    for key, value in extra.items():
        if SECRET_KEY_RE.search(str(key)):
            continue
        if isinstance(value, str):
            value = _short(value, 500)
        data[key] = value
    with contextlib.suppress(Exception):
        _atomic_json(_status_path(), data)


def _migrate_env(path: Path) -> dict[str, Any]:
    before = path.read_text("utf-8", errors="replace") if path.exists() else ""
    backup = path.with_name(path.name + ".pre-bootstrap.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
        with contextlib.suppress(Exception):
            os.chmod(backup, 0o600)
    lines = before.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        raw = line.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            if SAFE_ENV_KEY_RE.fullmatch(key.strip()):
                values[key.strip()] = value.strip().strip('"').strip("'")
    additions: list[str] = []
    defaults = {
        "PHONE_WORKER_CONFIG_SCHEMA": str(CONFIG_SCHEMA),
        "CORE_WORKER_HEARTBEAT_ENABLED": "true",
        "CORE_WORKER_JOBS_ENABLED": "true",
        "PHONE_WORKER_SELF_UPDATE_ENABLED": "true",
        "PHONE_WORKER_BOOTSTRAP_UPDATE_ENABLED": "true",
        "PHONE_WORKER_RUNTIME_ROOT": str(_runtime_root()),
        "PHONE_WORKER_STATE_DIR": str(_state_root()),
    }
    # Chaves de conectividade nunca são sobrescritas. Se o usuário as desativou,
    # o painel/estado deve reportar blocked_by_config em vez de reativar à força.
    for key, value in defaults.items():
        if key == "PHONE_WORKER_CONFIG_SCHEMA":
            continue
        if key not in values:
            additions.append(f"{key}={value}")
    out: list[str] = []
    schema_written = False
    for line in lines:
        if re.match(r"^\s*PHONE_WORKER_CONFIG_SCHEMA\s*=", line):
            if not schema_written:
                out.append(f"PHONE_WORKER_CONFIG_SCHEMA={CONFIG_SCHEMA}")
                schema_written = True
            continue
        out.append(line)
    if not schema_written:
        out.append(f"PHONE_WORKER_CONFIG_SCHEMA={CONFIG_SCHEMA}")
    if additions:
        out.extend(["", "# Migração automática do Core Worker bootstrap"] + additions)
    text = "\n".join(out).rstrip() + "\n"
    try:
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=" not in raw or not SAFE_ENV_KEY_RE.fullmatch(raw.split("=", 1)[0].strip()):
                raise ValueError("linha inválida após migração")
        _atomic_text(path, text, mode=0o600)
    except Exception:
        if backup.exists():
            shutil.copy2(backup, path)
        raise
    return {"ok": True, "schema": CONFIG_SCHEMA, "added": [x.split("=", 1)[0] for x in additions], "backup": str(backup) if backup.exists() else ""}


def _config_blocker(values: dict[str, str]) -> str:
    checks = (
        ("PHONE_WORKER_BOOTSTRAP_UPDATE_ENABLED", True),
        ("PHONE_WORKER_SELF_UPDATE_ENABLED", True),
        ("CORE_WORKER_HEARTBEAT_ENABLED", True),
        ("CORE_WORKER_JOBS_ENABLED", True),
    )
    for key, default in checks:
        raw = str(values.get(key, os.getenv(key, "")) or "").strip().lower()
        if not raw:
            enabled = default
        else:
            enabled = raw in {"1", "true", "yes", "y", "on", "sim"}
        if not enabled:
            return key
    for key in ("CORE_WORKER_VPS_URL", "CORE_WORKER_ID", "CORE_WORKER_TOKEN"):
        if not str(values.get(key) or os.getenv(key) or "").strip():
            return key
    return ""


def _manifest_url(base_url: str, worker_id: str) -> str:
    explicit = str(os.getenv("PHONE_WORKER_AGENT_MANIFEST_URL") or "").strip()
    if explicit:
        if not _same_origin(base_url, explicit):
            raise ValueError("PHONE_WORKER_AGENT_MANIFEST_URL usa origem diferente da VPS")
        return explicit
    return f"{base_url.rstrip('/')}/core-worker/agent/latest?worker_id={urllib.parse.quote(worker_id)}"


def _validate_manifest(data: dict[str, Any], base_url: str) -> dict[str, Any]:
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("schema do manifesto do agent incompatível")
    source_hash = str(data.get("source_hash") or "").strip().lower()
    archive_sha = str(data.get("sha256") or "").strip().lower()
    version = str(data.get("version") or "").strip()
    release_url = str(data.get("url") or data.get("release_url") or "").strip()
    if not SHA256_RE.fullmatch(source_hash):
        raise ValueError("source_hash inválido no manifesto")
    if not SHA256_RE.fullmatch(archive_sha):
        raise ValueError("sha256 do release inválido")
    if not version:
        raise ValueError("versão ausente no manifesto")
    if not release_url or not _same_origin(base_url, release_url):
        raise ValueError("URL do release fora da origem autorizada")
    size = int(data.get("bytes") or data.get("size") or 0)
    max_archive = _env_int("PHONE_WORKER_BOOTSTRAP_MAX_ARCHIVE_BYTES", DEFAULT_MAX_ARCHIVE_BYTES, 256 * 1024, 64 * 1024 * 1024)
    if size <= 0 or size > max_archive:
        raise ValueError("tamanho do release inválido")
    min_bootstrap = str(data.get("min_bootstrap_version") or "0")
    if _version_tuple(BOOTSTRAP_VERSION) < _version_tuple(min_bootstrap):
        raise RuntimeError(f"bootstrap {BOOTSTRAP_VERSION} abaixo do mínimo {min_bootstrap}")
    members = data.get("members")
    if not isinstance(members, list) or not members or len(members) > DEFAULT_MAX_MEMBERS:
        raise ValueError("lista de membros do release inválida")
    return {
        **data,
        "source_hash": source_hash,
        "sha256": archive_sha,
        "version": version,
        "url": release_url,
        "bytes": size,
    }


def _current_release() -> Path | None:
    current = _runtime_root() / "current"
    try:
        target = current.resolve(strict=True)
    except Exception:
        return None
    releases = (_runtime_root() / "releases").resolve()
    try:
        target.relative_to(releases)
    except Exception:
        return None
    return target if target.is_dir() else None


def _legacy_source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for target in sorted(LEGACY_SOURCE_HASH_TARGETS):
        path = root / target
        if not path.is_file():
            return ""
        digest.update(target.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _seed_legacy_release() -> Path | None:
    """Versiona o runtime pré-bootstrap antes da primeira promoção.

    A instalação inicial não possui ``runtime/current``. Sem esse seed, uma
    release nova que não sobe não teria rollback real. Copiamos somente a
    allowlist conhecida do agent legado; segredos/estado/logs ficam fora.
    """
    current = _current_release()
    if current is not None:
        return current
    source = _install_root()
    source_hash = _legacy_source_hash(source)
    if not SHA256_RE.fullmatch(source_hash):
        raise RuntimeError("runtime legado incompleto; rollback inicial não pode ser garantido")
    version = ""
    text = (source / "phone_worker.py").read_text("utf-8", errors="ignore")
    match = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)', text, re.M)
    version = match.group(1) if match else ""
    if not version:
        raise RuntimeError("versão do runtime legado não pôde ser identificada")
    releases = _runtime_root() / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / source_hash
    if not final.exists():
        staging = _runtime_root() / "staging" / f"legacy-{source_hash}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=False)
        members: list[dict[str, Any]] = []
        for target in (*LEGACY_SOURCE_HASH_TARGETS, *LEGACY_OPTIONAL_SNAPSHOT_TARGETS):
            src = source / target
            if not src.is_file():
                continue
            dst = staging / target
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            mode = stat.S_IMODE(src.stat().st_mode) or (0o755 if target.endswith(".sh") or target.endswith(".py") else 0o644)
            os.chmod(dst, mode)
            members.append({"path": target, "mode": mode, "bytes": dst.stat().st_size, "sha256": _sha256_file(dst)})
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": version,
            "source_hash": source_hash,
            "legacy_seed": True,
            "seeded_at": int(time.time()),
            "members": members,
        }
        _atomic_json(staging / "phone-worker-release.json", manifest, mode=0o644)
        try:
            os.replace(staging, final)
        except OSError:
            if not final.exists():
                raise
            shutil.rmtree(staging, ignore_errors=True)
    _atomic_symlink(_runtime_root() / "current", final)
    _write_state("validating", current_version=version, current_source_hash=source_hash, legacy_seed=True)
    return final


def _current_identity() -> dict[str, Any]:
    release = _current_release()
    if release is None:
        # Compatibilidade pré-migração: identifica o agent diretamente em ~/phone-worker.
        release = _install_root()
    manifest = _read_json(release / "phone-worker-release.json")
    if manifest:
        return {
            "version": str(manifest.get("version") or ""),
            "source_hash": str(manifest.get("source_hash") or "").lower(),
            "path": str(release),
        }
    worker_py = release / "phone_worker.py"
    version = ""
    with contextlib.suppress(Exception):
        text = worker_py.read_text("utf-8", errors="ignore")
        m = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)', text, re.M)
        version = m.group(1) if m else ""
    return {"version": version, "source_hash": "", "path": str(release)}


def _safe_zip_member(name: str) -> str:
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError("nome inseguro no release")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path traversal no release")
    return "/".join(parts)


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (int(info.external_attr) >> 16) & 0xFFFF


def _extract_and_validate(archive: Path, staging: Path, outer: dict[str, Any]) -> dict[str, Any]:
    if archive.stat().st_size != int(outer["bytes"]):
        raise ValueError("tamanho baixado diverge do manifesto")
    if _sha256_file(archive) != outer["sha256"]:
        raise ValueError("SHA-256 do release diverge")
    max_expanded = _env_int("PHONE_WORKER_BOOTSTRAP_MAX_EXPANDED_BYTES", DEFAULT_MAX_EXPANDED_BYTES, 1024 * 1024, 128 * 1024 * 1024)
    expected_members: dict[str, dict[str, Any]] = {}
    for item in outer.get("members") or []:
        if not isinstance(item, dict):
            raise ValueError("membro inválido no manifesto externo")
        name = _safe_zip_member(str(item.get("path") or item.get("target") or ""))
        expected_members[name] = item
    staging.mkdir(parents=True, exist_ok=False)
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = [x for x in zf.infolist() if not x.is_dir()]
        if len(infos) > DEFAULT_MAX_MEMBERS or len({x.filename for x in infos}) != len(infos):
            raise ValueError("release com membros em excesso/duplicados")
        names = {_safe_zip_member(info.filename) for info in infos}
        if "phone-worker-release.json" not in names:
            raise ValueError("manifesto interno ausente")
        for info in infos:
            name = _safe_zip_member(info.filename)
            mode = _zip_mode(info)
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG}:
                raise ValueError(f"link/tipo especial recusado: {name}")
            total += int(info.file_size)
            if total > max_expanded:
                raise ValueError("release expandido excede limite")
            if info.compress_size and info.file_size > max(8 * 1024 * 1024, info.compress_size * 200):
                raise ValueError("possível zip bomb recusado")
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            written = 0
            with zf.open(info) as src, dest.open("wb") as dst:
                while True:
                    chunk = src.read(128 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
            if written != info.file_size:
                raise ValueError(f"extração truncada: {name}")
            if name != "phone-worker-release.json":
                expected = expected_members.get(name)
                if not expected:
                    raise ValueError(f"membro não declarado: {name}")
                if digest.hexdigest() != str(expected.get("sha256") or "").lower():
                    raise ValueError(f"hash divergente: {name}")
                declared_mode = int(expected.get("mode") or 0o644)
                os.chmod(dest, declared_mode)
        inner = json.loads((staging / "phone-worker-release.json").read_text("utf-8"))
        if not isinstance(inner, dict) or inner.get("schema") != MANIFEST_SCHEMA:
            raise ValueError("manifesto interno incompatível")
        for key in ("version", "source_hash", "sha256"):
            if key == "sha256":
                # sha256 externo é do próprio ZIP e não é embutido para evitar autorreferência.
                continue
            if str(inner.get(key) or "") != str(outer.get(key) or ""):
                raise ValueError(f"manifesto interno diverge em {key}")
        if set(expected_members) != {name for name in names if name != "phone-worker-release.json"}:
            raise ValueError("conjunto de membros diverge do manifesto")
    # Valida sintaxe de toda a release antes da promoção.
    for path in sorted(staging.rglob("*.py")):
        py_compile.compile(str(path), doraise=True)
    bash = shutil.which("bash") or "/data/data/com.termux/files/usr/bin/bash"
    for path in sorted(staging.rglob("*.sh")):
        proc = subprocess.run([bash, "-n", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False)
        if proc.returncode != 0:
            raise ValueError(f"script shell inválido: {path.relative_to(staging)}: {_short(proc.stderr, 140)}")
    return {"members": len(expected_members), "expanded_bytes": total}


def _atomic_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + f".tmp.{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(tmp, link)


def _promote(staging: Path, source_hash: str) -> tuple[Path, Path | None]:
    runtime = _runtime_root()
    releases = runtime / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / source_hash
    current_before = _current_release()
    if final.exists():
        # Release pelo hash é imutável. Reutilizar só se o manifesto confere.
        existing = _read_json(final / "phone-worker-release.json")
        if str(existing.get("source_hash") or "") != source_hash:
            raise ValueError("diretório de release existente não corresponde ao hash")
        shutil.rmtree(staging, ignore_errors=True)
    else:
        os.replace(staging, final)
    if current_before is not None and current_before != final:
        _atomic_symlink(runtime / "previous", current_before)
    _atomic_symlink(runtime / "current", final)
    return final, current_before


def _read_pid() -> int:
    try:
        return int(_pid_path().read_text("utf-8").strip().splitlines()[0])
    except Exception:
        return 0


def _pid_cmdline(pid: int) -> str:
    if pid <= 1:
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _owned_agent_pid(pid: int) -> bool:
    cmdline = _pid_cmdline(pid)
    return bool(pid > 1 and "phone_worker.py" in cmdline and ("python" in cmdline.lower() or sys.executable in cmdline))


def _terminate_owned_agent() -> dict[str, Any]:
    pid = _read_pid()
    if not _owned_agent_pid(pid):
        return {"pid": pid or None, "terminated": False, "reason": "pid ausente/não confirmado"}
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 8
    while time.time() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return {"pid": pid, "terminated": True, "forced": False}
        time.sleep(0.2)
    if _owned_agent_pid(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        return {"pid": pid, "terminated": True, "forced": True}
    return {"pid": pid, "terminated": False, "reason": "identidade mudou antes do SIGKILL"}


def _start_runtime() -> subprocess.Popen[Any]:
    root = _install_root()
    active = _current_release()
    candidates = [
        root / "start-phone-worker.sh",
        active / "start-phone-worker.sh" if active else Path("/__missing__"),
    ]
    script = next((p for p in candidates if p.is_file()), None)
    if script is None:
        raise FileNotFoundError("start-phone-worker.sh não encontrado")
    env = os.environ.copy()
    if active:
        env["PHONE_WORKER_RELEASE_DIR"] = str(active)
    env.setdefault("PHONE_WORKER_RUNTIME_ROOT", str(_runtime_root()))
    env.setdefault("PHONE_WORKER_STATE_DIR", str(_state_root()))
    return subprocess.Popen([shutil.which("bash") or "bash", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)


def _verify_runtime(source_hash: str, version: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = _read_json(_runtime_status_path())
        pid = int(last.get("pid") or 0)
        age = max(0.0, time.time() - float(last.get("updated_at") or 0.0)) if last else 999999
        if (
            last.get("runtime_kind") == "termux"
            and last.get("runtime_mode") == "termux"
            and str(last.get("source_hash") or "").lower() == source_hash
            and str(last.get("version") or "") == version
            and age <= 30
            and _owned_agent_pid(pid)
            and bool(last.get("control_plane_alive"))
        ):
            return {"ok": True, "pid": pid, "age_seconds": round(age, 3), "direct_http_state": last.get("direct_http_state")}
        time.sleep(0.5)
    return {"ok": False, "last": {k: v for k, v in last.items() if not SECRET_KEY_RE.search(str(k))}}


def _cleanup_releases(keep_paths: Iterable[Path]) -> list[str]:
    releases = _runtime_root() / "releases"
    keep = {p.resolve() for p in keep_paths if p and p.exists()}
    retain = _env_int("PHONE_WORKER_BOOTSTRAP_RETAIN_RELEASES", DEFAULT_RETAIN_RELEASES, 2, 6)
    candidates = [p for p in releases.iterdir() if p.is_dir() and SHA256_RE.fullmatch(p.name)] if releases.exists() else []
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in candidates:
        try:
            if path.resolve() in keep:
                continue
        except Exception:
            continue
        if len([p for p in candidates if p.exists()]) - len(removed) <= retain:
            break
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


def _fetch_latest(base_url: str, token: str, worker_id: str) -> dict[str, Any]:
    url = _manifest_url(base_url, worker_id)
    raw, _headers, _size, _sig = _signed_request(
        url,
        base_url=base_url,
        token=token,
        worker_id=worker_id,
        accept="application/json",
        timeout=15,
        max_bytes=256 * 1024,
    )
    try:
        data = json.loads((raw or b"").decode("utf-8"))
    except Exception as exc:
        raise ValueError("manifesto do agent não é JSON válido") from exc
    if not isinstance(data, dict):
        raise ValueError("manifesto do agent inválido")
    return _validate_manifest(data, base_url)


def check_and_apply(*, force: bool = False, restart: bool = True) -> dict[str, Any]:
    env_path = _env_path()
    migration = _migrate_env(env_path)
    values = _load_env(env_path)
    blocker = _config_blocker(values)
    if blocker:
        _write_state("blocked_by_config", blocked_key=blocker)
        return {"ok": False, "state": "blocked_by_config", "blocked_key": blocker, "migration": migration}
    base_url = str(values.get("CORE_WORKER_VPS_URL") or os.getenv("CORE_WORKER_VPS_URL") or "").strip().rstrip("/")
    worker_id = str(values.get("CORE_WORKER_ID") or os.getenv("CORE_WORKER_ID") or "").strip()
    token = str(values.get("CORE_WORKER_TOKEN") or os.getenv("CORE_WORKER_TOKEN") or "").strip()
    _origin(base_url)
    _write_state("target_published", worker_id=worker_id)
    manifest = _fetch_latest(base_url, token, worker_id)
    current = _current_identity()
    if not _env_bool("PHONE_WORKER_BOOTSTRAP_ALLOW_DOWNGRADE", False):
        if _version_tuple(manifest["version"]) < _version_tuple(current.get("version")):
            raise PermissionError("downgrade automático recusado")
    same = str(current.get("source_hash") or "").lower() == manifest["source_hash"]
    if same and not force:
        _write_state("succeeded", target_version=manifest["version"], source_hash=manifest["source_hash"], skipped=True)
        return {"ok": True, "state": "succeeded", "skipped": True, "version": manifest["version"], "source_hash": manifest["source_hash"]}

    # Primeira migração: transforme o runtime já instalado em release imutável
    # antes de tocar no ponteiro ativo. Isso garante rollback também no 1º salto.
    if _current_release() is None:
        _seed_legacy_release()
        current = _current_identity()

    runtime = _runtime_root()
    staging_parent = runtime / "staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    _write_state("downloading", target_version=manifest["version"], source_hash=manifest["source_hash"])
    archive = staging_parent / f"{manifest['source_hash']}.download"
    with contextlib.suppress(Exception):
        archive.unlink()
    _raw, _headers, total, _hmac = _signed_request(
        manifest["url"],
        base_url=base_url,
        token=token,
        worker_id=worker_id,
        accept="application/zip,application/octet-stream",
        timeout=max(30, _env_int("PHONE_WORKER_BOOTSTRAP_DOWNLOAD_TIMEOUT_SECONDS", 120, 15, 900)),
        max_bytes=_env_int("PHONE_WORKER_BOOTSTRAP_MAX_ARCHIVE_BYTES", DEFAULT_MAX_ARCHIVE_BYTES, 256 * 1024, 64 * 1024 * 1024),
        output_path=archive,
    )
    if total != int(manifest["bytes"]):
        archive.unlink(missing_ok=True)
        raise ValueError("download do release terminou com tamanho inesperado")
    staging = staging_parent / f"{manifest['source_hash']}.{os.getpid()}.{int(time.time())}"
    _write_state("validating", target_version=manifest["version"], source_hash=manifest["source_hash"])
    try:
        validation = _extract_and_validate(archive, staging, manifest)
    finally:
        with contextlib.suppress(Exception):
            archive.unlink()
    _write_state("installing", target_version=manifest["version"], source_hash=manifest["source_hash"])
    final, previous = _promote(staging, manifest["source_hash"])
    if not restart:
        _write_state("restart_pending", target_version=manifest["version"], source_hash=manifest["source_hash"])
        return {"ok": True, "state": "restart_pending", "release": str(final), "validation": validation}

    _write_state("restart_pending", target_version=manifest["version"], source_hash=manifest["source_hash"])
    terminated = _terminate_owned_agent()
    _start_runtime()
    _write_state("verifying_runtime", target_version=manifest["version"], source_hash=manifest["source_hash"])
    verify = _verify_runtime(manifest["source_hash"], manifest["version"], _env_int("PHONE_WORKER_BOOTSTRAP_VERIFY_TIMEOUT_SECONDS", DEFAULT_VERIFY_TIMEOUT_SECONDS, 10, 180))
    if verify.get("ok"):
        removed = _cleanup_releases([final, previous] if previous else [final])
        _write_state("succeeded", target_version=manifest["version"], source_hash=manifest["source_hash"], release=str(final))
        return {
            "ok": True,
            "state": "succeeded",
            "version": manifest["version"],
            "source_hash": manifest["source_hash"],
            "release": str(final),
            "previous": str(previous) if previous else "",
            "validation": validation,
            "verify": verify,
            "terminated": terminated,
            "removed_releases": removed,
        }

    # Nova release não provou vida: o ponteiro volta antes de qualquer limpeza.
    if previous is not None and previous.exists():
        _atomic_symlink(runtime / "current", previous)
        _terminate_owned_agent()
        _start_runtime()
        rollback_manifest = _read_json(previous / "phone-worker-release.json")
        rollback_hash = str(rollback_manifest.get("source_hash") or "")
        rollback_version = str(rollback_manifest.get("version") or "")
        rollback_verify = _verify_runtime(rollback_hash, rollback_version, 30) if rollback_hash and rollback_version else {"ok": False, "reason": "release anterior sem manifesto"}
    else:
        rollback_verify = {"ok": False, "reason": "release anterior indisponível"}
    _write_state(
        "rolled_back",
        target_version=manifest["version"],
        source_hash=manifest["source_hash"],
        reason="runtime novo não anunciou versão/hash esperados",
        rollback_ok=bool(rollback_verify.get("ok")),
    )
    return {
        "ok": False,
        "state": "rolled_back",
        "reason": "runtime novo não anunciou versão/hash esperados",
        "verify": verify,
        "rollback_verify": rollback_verify,
        "previous": str(previous) if previous else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atualizador bootstrap independente do Core Phone Worker")
    parser.add_argument("--check", action="store_true", help="consulta o target persistente e aplica se necessário")
    parser.add_argument("--force", action="store_true", help="revalida/reaplica mesmo com hash já atual")
    parser.add_argument("--no-restart", action="store_true", help="promove a release, mas deixa o restart pendente")
    parser.add_argument("--status", action="store_true", help="imprime o estado sanitizado do updater")
    args = parser.parse_args(argv)
    _load_env(_env_path())
    if args.status:
        print(json.dumps(_read_json(_status_path()), ensure_ascii=False, sort_keys=True))
        return 0
    try:
        result = check_and_apply(force=args.force, restart=not args.no_restart)
    except Exception as exc:
        _write_state("failed", reason=f"{type(exc).__name__}: {_short(exc, 240)}")
        result = {"ok": False, "state": "failed", "error": f"{type(exc).__name__}: {_short(exc, 240)}"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
