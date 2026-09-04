from flask import Flask, jsonify, abort, send_file, request
from waitress import serve
import os
import json
import hashlib
import hmac
import re
import contextlib
import fcntl
import threading
import time
import uuid
import shutil
import subprocess
import tempfile
import zipfile
import urllib.error
import urllib.request
from pathlib import Path

from utility.apk_identity import ApkIdentityError, assert_expected_apk_identity, inspect_apk_identity
from utility.storage_maintenance import prune_core_worker_releases

app = Flask(__name__)

_health_provider = None
_update_action_provider = None
_tts_audio_lock = threading.RLock()
_core_worker_notification_lock = threading.RLock()
_core_worker_fcm_tokens_lock = threading.RLock()
_core_worker_app_heartbeat_lock = threading.RLock()
_core_worker_app_jobs_lock = threading.RLock()
_core_worker_apk_publish_lock = threading.RLock()
_tts_audio_files: dict[str, tuple[str, float]] = {}
_core_worker_pending_automation_lock = threading.RLock()
_core_worker_pending_automation_processes = {}
_core_worker_pending_automation_last_started = {}
_core_worker_pending_automation_last_log = {}

# Cache local para telemetria do APK/Core Worker. A VPS pequena não deve
# reler/regravar JSONs de MB a cada heartbeat/fetch; o cache é invalidado por
# mtime/tamanho e atualizado em toda escrita atômica.
_json_file_cache_lock = threading.RLock()
_json_file_cache: dict[str, dict] = {}
_core_worker_app_fetch_throttle_lock = threading.RLock()
_core_worker_app_fetch_last_served: dict[str, float] = {}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        raw = int(os.getenv(name, str(default)) or default)
    except Exception:
        raw = default
    return max(minimum, min(raw, maximum))


CORE_WORKER_APP_HEARTBEAT_EVENT_LIMIT = _env_int("CORE_WORKER_APP_HEARTBEAT_EVENT_LIMIT", 60, minimum=20, maximum=240)
CORE_WORKER_APP_NOTIFICATION_EVENT_LIMIT = _env_int("CORE_WORKER_APP_NOTIFICATION_EVENT_LIMIT", 60, minimum=20, maximum=200)
CORE_WORKER_APP_PENDING_LIMIT = _env_int("CORE_WORKER_APP_PENDING_LIMIT", 80, minimum=20, maximum=200)
CORE_WORKER_APP_FETCH_THROTTLE_SECONDS = _env_int("CORE_WORKER_APP_FETCH_THROTTLE_SECONDS", 12, minimum=0, maximum=120)
CORE_WORKER_APP_FETCH_THROTTLE_WHEN_IDLE_SECONDS = _env_int("CORE_WORKER_APP_FETCH_THROTTLE_WHEN_IDLE_SECONDS", 25, minimum=0, maximum=180)
CORE_WORKER_APP_HEARTBEAT_STORE_MIN_SECONDS = _env_int("CORE_WORKER_APP_HEARTBEAT_STORE_MIN_SECONDS", 10, minimum=0, maximum=90)


def _core_worker_apk_dir() -> str:
    """Diretório local usado para publicar atualizações privadas do Core Worker APK.

    O caminho pode ser definido por CORE_WORKER_APK_DIR. Por padrão fica dentro do
    repositório para publicar o APK recebido do phone worker builder.
    """
    base = os.getenv("CORE_WORKER_APK_DIR")
    if not base:
        base = os.path.join(os.getcwd(), "android", "core-worker-app", "releases")
    return os.path.abspath(base)


def _core_worker_desired_source_path() -> str:
    return os.path.join(_core_worker_apk_dir(), "desired-source.json")


def _core_worker_desired_source_lock_path() -> str:
    return os.path.join(_core_worker_apk_dir(), ".desired-source.lock")


@contextlib.contextmanager
def _core_worker_desired_source_lock():
    os.makedirs(_core_worker_apk_dir(), exist_ok=True)
    fh = open(_core_worker_desired_source_lock_path(), "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _load_core_worker_desired_source() -> dict:
    path = _core_worker_desired_source_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"_invalid": True, "_error": f"{type(exc).__name__}: {exc}"[:180]}
    if not isinstance(data, dict) or data.get("schema") != "core-worker-apk-desired-source-v1":
        return {"_invalid": True, "_error": "schema inválido"}
    fingerprint = str(data.get("sourceFingerprint") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return {"_invalid": True, "_error": "sourceFingerprint inválido"}
    return data


def _worker_apk_toolchain_fingerprint(worker: dict) -> str:
    for container in (worker.get("status"), worker.get("health"), worker):
        if not isinstance(container, dict):
            continue
        preflight = container.get("apk_self_builder")
        if not isinstance(preflight, dict):
            continue
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


def _desired_source_publish_error(
    desired: dict,
    *,
    worker: dict,
    requested_version_name: str,
    requested_version_code: int,
    requested_source_fingerprint: str,
) -> str:
    if not desired:
        return ""  # compatibilidade durante a primeira implantação do protocolo novo
    if desired.get("_invalid"):
        return "desired-source inválido na VPS"
    expected_fp = str(desired.get("sourceFingerprint") or "").strip().lower()
    actual_fp = str(requested_source_fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", actual_fp):
        return "publicação sem source fingerprint válido"
    if actual_fp != expected_fp:
        return "APK pertence a uma fonte superada"
    expected_code = int(desired.get("versionCode") or 0)
    expected_name = str(desired.get("versionName") or "").strip()
    if expected_code and int(requested_version_code or 0) != expected_code:
        return "versionCode não corresponde ao target desejado"
    if expected_name and requested_version_name and requested_version_name != expected_name:
        return "versionName não corresponde ao target desejado"
    if str(desired.get("publicationPolicy") or "") == "selected-builder-v1":
        expected_worker = str(desired.get("selectedBuilderWorkerId") or "").strip()
        if not expected_worker:
            return "target ainda não possui builder selecionado"
        actual_worker = str(worker.get("worker_id") or "").strip()
        if actual_worker != expected_worker:
            return "APK produzido por builder diferente do selecionado"
        expected_runtime = str(desired.get("selectedBuilderRuntimeKind") or "").strip().lower()
        actual_runtime = str(worker.get("runtime_kind") or "").strip().lower()
        if expected_runtime and actual_runtime and actual_runtime != expected_runtime:
            return "runtime do builder não corresponde ao target"
        required_agent_hash = str(desired.get("requiredAgentSourceHash") or "").strip().lower()
        if required_agent_hash and str(worker.get("source_hash") or "").strip().lower() != required_agent_hash:
            return "Termux builder não está no source hash mínimo requerido"
        expected_toolchain = str(desired.get("toolchainFingerprint") or "").strip().lower()
        if expected_toolchain and _worker_apk_toolchain_fingerprint(worker) != expected_toolchain:
            return "toolchain do builder não corresponde ao target selecionado"
    return ""


def _safe_core_worker_apk_file(filename: str) -> str | None:
    filename = str(filename or "").strip().replace("\\", "/")
    if not filename or filename.startswith("/") or ".." in filename.split("/"):
        return None
    lowered = filename.lower()
    if not lowered.endswith((".apk", ".json", ".txt", ".zip")):
        return None
    base = _core_worker_apk_dir()
    full = os.path.abspath(os.path.join(base, filename))
    if full != base and not full.startswith(base + os.sep):
        return None
    return full




def _safe_release_filename(value: str, *, default: str = "CoreWorker.apk") -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or default).replace("\\", "/").split("/")[-1]).strip("-._")
    if not filename:
        filename = default
    return filename[:120]




def _external_core_worker_url(path: str) -> str:
    path = str(path or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    try:
        return request.url_root.rstrip("/") + path
    except Exception:
        base = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("CORE_WORKER_VPS_URL") or "").strip().rstrip("/")
        return (base + path) if base else path


def _core_worker_apk_url(filename: str) -> str:
    return f"/core-worker/app/{_safe_release_filename(filename, default='CoreWorker.apk')}"


def _json_field(value: str, fallback):
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return fallback


def _repo_data_dir() -> str:
    base = os.getenv("CORE_WORKER_DATA_DIR") or os.path.join(os.getcwd(), "data")
    path = os.path.abspath(os.path.expanduser(base))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_short_text(value, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _safe_string_list(value, *, limit: int = 80, item_limit: int = 96) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _safe_short_text(item, item_limit)
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
        if len(out) >= limit:
            break
    return out


def _first_dict(*values) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_list(*values, limit: int = 80) -> list[str]:
    for value in values:
        items = _safe_string_list(value, limit=limit)
        if items:
            return items
    return []



def _core_worker_scrub_eula_from_public(value):
    """Remove legado de EULA de estruturas que podem ir para painel/status público.

    O histórico bruto antigo do APK 0.5.63 podia conter eula/eulaAccepted em
    runner preflight v1. A etapa atual mantém termos fora das pendências visíveis;
    esta função evita que resultados antigos poluam status e comandos.
    """
    changed = False
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key or "")
            if "eula" in key_text.lower():
                changed = True
                continue
            scrubbed, item_changed = _core_worker_scrub_eula_from_public(item)
            if item_changed:
                changed = True
            out[key] = scrubbed
        state_text = str(out.get("state") or out.get("stage") or out.get("component") or "").lower()
        if "runner_preflight" in state_text or str(out.get("component") or "") == "core_linux_runner_preflight":
            missing = out.get("currentMissing") if isinstance(out.get("currentMissing"), list) else out.get("missing") if isinstance(out.get("missing"), list) else None
            future = out.get("futureMissing") if isinstance(out.get("futureMissing"), list) else []
            if missing is not None:
                count = len(missing)
                summary = str(out.get("summary") or "")
                # Mantém compatibilidade com preflight antigo, mas sem voltar a
                # contar Box64/Bedrock como pendência da fase base.
                if "Runner preflight conclu" in summary and "base" not in summary.lower():
                    out["summary"] = f"Runner preflight concluído · {count} pendência(s) base"
                    changed = True
                if future and "futureMissing" not in out:
                    out["futureMissing"] = future
                    changed = True
        return out, changed
    if isinstance(value, list):
        out = []
        for item in value:
            if "eula" in str(item or "").lower():
                changed = True
                continue
            scrubbed, item_changed = _core_worker_scrub_eula_from_public(item)
            if item_changed:
                changed = True
            out.append(scrubbed)
        return out, changed
    if isinstance(value, str):
        if "eula" in value.lower():
            return "", True
    return value, changed


def _core_worker_app_sanitize_legacy_runner_record(record: dict) -> tuple[dict, bool]:
    if not isinstance(record, dict):
        return record, False
    typ = _core_worker_app_normalize_job_type(record.get("type")) if "_core_worker_app_normalize_job_type" in globals() else str(record.get("type") or "")
    if "runner" not in typ and "runner" not in json.dumps(record, ensure_ascii=False).lower():
        return record, False
    scrubbed, changed = _core_worker_scrub_eula_from_public(record)
    if not isinstance(scrubbed, dict):
        return record, False
    # Corrige mensagem visível de preflight antigo após remover EULA.
    result = scrubbed.get("result") if isinstance(scrubbed.get("result"), dict) else {}
    runner = result.get("coreLinuxRunner") if isinstance(result.get("coreLinuxRunner"), dict) else {}
    missing = runner.get("currentMissing") if isinstance(runner.get("currentMissing"), list) else runner.get("missing") if isinstance(runner.get("missing"), list) else None
    if typ == "apk_core_linux_runner_preflight" and missing is not None:
        ready = bool(runner.get("runnerBaseRequirementsReady") or runner.get("termuxReductionReady"))
        msg = "Core Linux base pronto · smoke test real é o próximo passo" if ready else f"Runner preflight concluído · {len(missing)} pendência(s) base"
        if scrubbed.get("message") != msg:
            scrubbed["message"] = msg
            changed = True
    return scrubbed, changed


def _core_worker_app_sanitize_legacy_runner_results(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    changed = False
    for key in ("results",):
        arr = data.get(key)
        if not isinstance(arr, list):
            continue
        new_arr = []
        for item in arr:
            if isinstance(item, dict):
                item, item_changed = _core_worker_app_sanitize_legacy_runner_record(item)
                changed = changed or item_changed
            new_arr.append(item)
        data[key] = new_arr
    latest = data.get("latestResultByInstallId") if isinstance(data.get("latestResultByInstallId"), dict) else {}
    for key, item in list(latest.items()):
        if isinstance(item, dict):
            clean_item, item_changed = _core_worker_app_sanitize_legacy_runner_record(item)
            if item_changed:
                latest[key] = clean_item
                changed = True
    history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
    for key, items in list(history.items()):
        if not isinstance(items, list):
            continue
        clean_items = []
        for item in items:
            if isinstance(item, dict):
                clean_item, item_changed = _core_worker_app_sanitize_legacy_runner_record(item)
                changed = changed or item_changed
                clean_items.append(clean_item)
            else:
                clean_items.append(item)
        history[key] = clean_items
    return changed




CORE_WORKER_APK_V1_CAPABILITIES = [
    "apk-native",
    "android-status",
    "native-boot",
    "safe-shell-probe",
    "python-embedded",
    "internal-jobs",
    "core-linux-runtime",
    "core-linux-rootfs-manager",
    "core-linux-rootfs-import-v1",
    "core-linux-runner-preflight-v1",
    "core-linux-runner-preflight-v2",
    "core-linux-runner-preflight-v3",
    "core-linux-runner-preflight-v4",
    "core-linux-runner-preflight-v5",
    "core-linux-runner-preflight-v6",
    "core-linux-runner-preflight-v7",
    "core-linux-runner-preflight-v8",
    "core-linux-runner-preflight-v10",
    "core-linux-runner-preflight-v11",
    "core-linux-base-tools-smoke-v12",
    "core-linux-rootfs-proot-smoke-v13",
    "core-linux-rootfs-proot-smoke-v13.1",
    "core-linux-rootfs-proot-smoke-v13.2",
    "core-linux-rootfs-proot-smoke-v13.3",
    "core-linux-box64-intake-preflight-v14.2.1",
    "core-linux-box64-version-smoke-v15",
    "core-linux-box64-version-smoke-v15.1",
    "core-linux-box64-version-smoke-v15.2",
    "core-linux-box64-glibc-preflight-v15.3",
    "core-linux-box64-glibc-preflight-v15.3.1",
    "core-linux-embedded-binaries-intake-v1",
    "core-linux-embedded-binaries-intake-v2",
    "core-linux-embedded-binaries-intake-v3",
    "core-linux-embedded-binaries-intake-v4",
    "core-linux-embedded-binaries-intake-v5",
    "core-linux-embedded-binaries-intake-v6",
    "core-linux-embedded-binaries-intake-v7",
    "core-linux-embedded-binaries-intake-v8",
    "core-linux-embedded-binaries-intake-v10",
    "core-linux-embedded-binaries-intake-v11",
    "core-linux-embedded-binaries-build-pipeline-v1",
    "core-linux-embedded-binaries-build-pipeline-v2",
    "core-linux-embedded-binaries-build-pipeline-v3",
    "core-linux-embedded-binaries-build-pipeline-v4",
    "core-linux-embedded-binaries-build-pipeline-v5",
    "core-linux-embedded-binaries-build-pipeline-v6",
    "core-linux-runtime-v1",
    "minecraft-bedrock-manager-safe-plan",
]

CORE_WORKER_APK_V1_SUPPORTED_TASKS = [
    "apk_ping",
    "apk_status_refresh",
    "apk_upload_app_logs",
    "apk_diagnostic",
    "apk_check_update",
    "apk_test_vps_connection",
    "apk_sync_runtime_state",
    "apk_job_history",
    "apk_device_diagnostic",
    "apk_push_diagnostic",
    "apk_update_diagnostic",
    "apk_runtime_diagnostic",
    "apk_worker_bridge_status",
    "apk_test_notification",
    "apk_repair_local_state",
    "apk_reset_job_history",
    "apk_trim_cache",
    "apk_update_storage_cleanup",
    "apk_sync_profile",
    "apk_sync_profile_now",
    "apk_verify_update_state",
    "apk_native_worker_status",
    "apk_native_boot_status",
    "apk_local_shell_probe",
    "apk_core_linux_native_executor_probe",
    "apk_core_linux_native_executor_test",
    "apk_core_linux_native_runtime_status",
    "apk_core_linux_rootfs_status",
    "apk_core_linux_rootfs_prepare",
    "apk_core_linux_rootfs_validate",
    "apk_core_linux_rootfs_preflight",
    "apk_core_linux_rootfs_clean_staging",
    "apk_core_linux_rootfs_import_status",
    "apk_core_linux_rootfs_import_validate",
    "apk_core_linux_rootfs_import_abort",
    "apk_core_linux_rootfs_real_status",
    "apk_core_linux_rootfs_glibc_preflight",
    "apk_core_linux_runner_status",
    "apk_core_linux_runner_preflight",
    "apk_core_linux_runner_requirements",
    "apk_core_linux_runtime_smoke_test",
    "apk_core_linux_rootfs_smoke_test",
]


def _looks_like_core_worker_apk_v1(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    source = str(record.get("source") or "")
    try:
        version_code = int(record.get("appVersionCode") or record.get("versionCode") or 0)
    except Exception:
        version_code = 0
    return source.startswith("core-worker-apk") or version_code >= 70


def _apk_v1_supported_tasks(record: dict | None = None) -> list[str]:
    return list(CORE_WORKER_APK_V1_SUPPORTED_TASKS) if _looks_like_core_worker_apk_v1(record or {"source": "core-worker-apk"}) else []


def _apk_v1_capabilities(record: dict | None = None) -> list[str]:
    return list(CORE_WORKER_APK_V1_CAPABILITIES) if _looks_like_core_worker_apk_v1(record or {"source": "core-worker-apk"}) else []


def _load_json_cached(path: str, default=None):
    """Carrega JSON com cache por mtime/tamanho para evitar I/O síncrono repetido."""
    if default is None:
        default = {}
    path = os.path.abspath(path)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return default.copy() if isinstance(default, dict) else default
    except Exception:
        return default.copy() if isinstance(default, dict) else default
    cache_key = path
    with _json_file_cache_lock:
        cached = _json_file_cache.get(cache_key)
        if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
            return cached.get("data")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = default.copy() if isinstance(default, dict) else default
    with _json_file_cache_lock:
        _json_file_cache[cache_key] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "data": data, "loadedAt": time.time()}
    return data


def _atomic_write_json(path: str, data: dict, *, mode: int = 0o600) -> None:
    """Grava JSON de forma atômica e barata para a VPS.

    O caminho anterior usava indentação, sort_keys e fsync em toda escrita. Em
    heartbeat/jobs do APK isso bloqueava o processo do bot por segundos quando
    havia rajada. Agora o arquivo é compacto e o fsync fica opt-in via env.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            if str(os.getenv("CORE_WORKER_JSON_FSYNC", "0")).lower() in {"1", "true", "yes", "on"}:
                with contextlib.suppress(Exception):
                    os.fsync(fh.fileno())
        os.replace(tmp, path)
        with contextlib.suppress(Exception):
            os.chmod(path, mode)
        with contextlib.suppress(Exception):
            stat = os.stat(path)
            with _json_file_cache_lock:
                _json_file_cache[os.path.abspath(path)] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "data": data, "loadedAt": time.time()}
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _core_worker_notification_log_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_notifications.json")


def _notification_event_id(*, version_name: str, version_code: int, sha256: str) -> str:
    seed = f"{version_name}:{version_code}:{sha256}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"apk-{int(version_code or 0)}-{digest}"


def _append_core_worker_notification_event(event: dict) -> dict:
    path = _core_worker_notification_log_path()
    now = int(time.time())
    state = _safe_short_text(event.get("state") or event.get("event"), 48)
    delivered = bool(event.get("delivered", False)) or state in {"displayed", "background_displayed", "fcm_received", "fcm_displayed", "duplicate", "background_duplicate", "already_displayed", "download_started", "download_verified", "install_intent_opened", "local_agent_seen", "local_agent_unpaired", "app_opened"}
    clean = {
        "receivedAt": now,
        "notificationId": _safe_short_text(event.get("notificationId"), 96),
        "state": state,
        "delivered": delivered,
        "versionName": _safe_short_text(event.get("versionName"), 48),
        "versionCode": int(event.get("versionCode") or 0),
        "appVersion": _safe_short_text(event.get("appVersion"), 48),
        "appVersionCode": int(event.get("appVersionCode") or 0),
        "workerId": _safe_short_text(event.get("workerId"), 80),
        "installId": _safe_short_text(event.get("installId"), 80),
        "permission": _safe_short_text(event.get("permission"), 40),
        "detail": _safe_short_text(event.get("detail"), 180),
        "remoteAddr": _safe_short_text(getattr(request, "remote_addr", "") or "", 64),
    }
    with _core_worker_notification_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        events = data.get("events") if isinstance(data.get("events"), list) else []
        events.append(clean)
        events = events[-CORE_WORKER_APP_NOTIFICATION_EVENT_LIMIT:]
        latest_by_id = data.get("latestById") if isinstance(data.get("latestById"), dict) else {}
        nid = clean.get("notificationId") or "unknown"
        latest_by_id[nid] = clean
        data = {"ok": True, "updatedAt": now, "events": events, "latestById": latest_by_id}
        _atomic_write_json(path, data, mode=0o600)
    return clean

def _latest_core_worker_notification_summary(notification_id: str) -> dict:
    path = _core_worker_notification_log_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    latest = data.get("latestById") if isinstance(data, dict) and isinstance(data.get("latestById"), dict) else {}
    record = latest.get(str(notification_id or "")) if isinstance(latest, dict) else None
    if isinstance(record, dict):
        state = str(record.get("state") or "")
        delivered = bool(record.get("delivered", False)) or state in {"displayed", "background_displayed", "fcm_received", "fcm_displayed", "duplicate", "background_duplicate", "already_displayed", "download_started", "download_verified", "install_intent_opened", "local_agent_seen", "local_agent_unpaired", "app_opened"}
        return {
            "lastState": record.get("state"),
            "lastDelivered": delivered,
            "lastReceivedAt": record.get("receivedAt"),
            "lastWorkerId": record.get("workerId"),
            "lastAppVersion": record.get("appVersion"),
            "lastDetail": record.get("detail"),
        }
    return {"lastState": "pending", "lastDelivered": False}


def _core_worker_fcm_tokens_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_fcm_tokens.json")


def _fcm_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8", errors="ignore")).hexdigest()


def _fcm_error_is_unregistered(detail: str) -> bool:
    text = str(detail or "").lower()
    return "unregistered" in text or ("http 404" in text and "requested entity was not found" in text)


def _fcm_unregistered_public_error() -> str:
    return "token FCM expirado/invalidado; aguardando novo token do APK"


def _load_core_worker_fcm_tokens() -> dict:
    path = _core_worker_fcm_tokens_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    data["tokens"] = tokens
    data.setdefault("ok", True)
    return data


def _save_core_worker_fcm_tokens(data: dict) -> None:
    path = _core_worker_fcm_tokens_path()
    with _core_worker_fcm_tokens_lock:
        _atomic_write_json(path, data, mode=0o600)

def _register_core_worker_fcm_token(payload: dict) -> dict:
    token = str(payload.get("fcmToken") or payload.get("fcm_token") or payload.get("token") or "").strip()
    if len(token) < 20:
        raise ValueError("fcmToken ausente ou curto demais")
    now = int(time.time())
    token_hash = _fcm_token_hash(token)
    with _core_worker_fcm_tokens_lock:
        data = _load_core_worker_fcm_tokens()
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        record = tokens.get(token_hash) if isinstance(tokens.get(token_hash), dict) else {}
        was_unregistered = str(record.get("lastErrorCode") or "").upper() == "UNREGISTERED" or bool(record.get("invalidatedAt"))
        common = {
            "token": token,
            "tokenHash": token_hash,
            "workerId": _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80),
            "installId": _safe_short_text(payload.get("installId") or payload.get("install_id"), 80),
            "deviceName": _safe_short_text(payload.get("deviceName") or payload.get("device_name"), 80),
            "platform": _safe_short_text(payload.get("platform") or "android", 32),
            "source": _safe_short_text(payload.get("source") or "core-worker-apk", 48),
            "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48),
            "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0),
            "permission": _safe_short_text(payload.get("permission"), 40),
            "lastReason": _safe_short_text(payload.get("reason"), 64),
            "lastRemoteAddr": _safe_short_text(request.remote_addr or "", 64),
            "lastSeenAt": now,
        }
        record.update(common)
        record.setdefault("registeredAt", now)
        if was_unregistered:
            # A documentação FCM recomenda remover tokens UNREGISTERED/404 e obter um novo token no cliente.
            record.update({
                "active": False,
                "refreshRequired": True,
                "lastPushStatus": "unregistered",
                "lastErrorCode": "UNREGISTERED",
                "lastError": _fcm_unregistered_public_error(),
                "lastRejectedRegistrationAt": now,
            })
        else:
            record.update({
                "active": True,
                "refreshRequired": False,
                "lastErrorCode": "",
                "lastError": "",
            })
        # Ao receber um token novo do mesmo APK/instalação, o token antigo fica apenas em histórico local.
        for other_hash, other in list(tokens.items()):
            if other_hash == token_hash or not isinstance(other, dict):
                continue
            same_install = common["installId"] and str(other.get("installId") or "") == common["installId"]
            same_worker = common["workerId"] and str(other.get("workerId") or "") == common["workerId"]
            same_device = common["deviceName"] and str(other.get("deviceName") or "") == common["deviceName"]
            if same_install or same_worker or same_device:
                other["active"] = False
                other["supersededAt"] = now
                other["lastPushStatus"] = "superseded"
                other["lastError"] = "token substituído por registro mais novo do APK"
                tokens[other_hash] = other
        tokens[token_hash] = record
        data["tokens"] = tokens
        data["updatedAt"] = now
        _atomic_write_json(_core_worker_fcm_tokens_path(), data, mode=0o600)
        public = {k: v for k, v in record.items() if k != "token"}
        if was_unregistered:
            public["refreshRequired"] = True
            public["refreshReason"] = _fcm_unregistered_public_error()
        return public


def _active_core_worker_fcm_records(*, max_age_days: int = 45) -> list[dict]:
    data = _load_core_worker_fcm_tokens()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    cutoff = int(time.time()) - max(1, int(max_age_days)) * 86400
    records: list[dict] = []
    runtime_records = _core_worker_app_latest_runtime_records()
    for record in tokens.values():
        if not isinstance(record, dict) or not record.get("active"):
            continue
        token = str(record.get("token") or "").strip()
        if len(token) < 20:
            continue
        try:
            seen = int(record.get("lastSeenAt") or record.get("registeredAt") or 0)
        except Exception:
            seen = 0
        if seen and seen < cutoff:
            continue
        fresh, _reason = _core_worker_fcm_runtime_freshness(record, runtime_records)
        if not fresh:
            continue
        records.append(record)
    return records



def _core_worker_app_latest_runtime_records() -> list[dict]:
    path = _core_worker_app_heartbeats_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return []
    latest = data.get("latestByInstallId") if isinstance(data.get("latestByInstallId"), dict) else {}
    records = [r for r in latest.values() if isinstance(r, dict)]
    return records


def _core_worker_fcm_runtime_freshness(record: dict, runtime_records: list[dict] | None = None) -> tuple[bool, str]:
    if not isinstance(record, dict):
        return False, "registro inválido"
    runtime_records = runtime_records if runtime_records is not None else _core_worker_app_latest_runtime_records()
    install_id = str(record.get("installId") or "").strip()
    worker_id = str(record.get("workerId") or "").strip()
    best = None
    for runtime in runtime_records:
        if not isinstance(runtime, dict):
            continue
        if install_id and str(runtime.get("installId") or "") == install_id:
            best = runtime
            break
        if worker_id and str(runtime.get("workerId") or "") == worker_id:
            best = runtime
            break
    if not best:
        return True, "sem heartbeat comparável"
    try:
        runtime_code = int(best.get("appVersionCode") or 0)
    except Exception:
        runtime_code = 0
    try:
        token_code = int(record.get("appVersionCode") or 0)
    except Exception:
        token_code = 0
    if runtime_code > 0 and token_code > 0 and token_code < runtime_code:
        return False, f"token de APK antigo ({token_code} < {runtime_code})"
    if runtime_code > 0 and token_code <= 0:
        try:
            seen = int(record.get("lastSeenAt") or record.get("registeredAt") or 0)
            hb_seen = int(best.get("receivedAt") or 0)
        except Exception:
            seen = hb_seen = 0
        if hb_seen and seen and seen < hb_seen - 300:
            return False, "token sem versão anterior ao heartbeat atual"
    return True, "compatível com heartbeat atual"


def _core_worker_fcm_public_summary(worker_id: str = "") -> dict:
    worker_id = str(worker_id or "").strip()
    data = _load_core_worker_fcm_tokens()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    runtime_records = _core_worker_app_latest_runtime_records()
    records = _active_core_worker_fcm_records(max_age_days=120)
    if worker_id:
        runtime_install_ids = {str(r.get("installId") or "") for r in runtime_records if isinstance(r, dict) and str(r.get("workerId") or "") == worker_id}
        records = [r for r in records if str(r.get("workerId") or "") == worker_id or (str(r.get("installId") or "") in runtime_install_ids)]
    invalidated = []
    stale = []
    incomplete = []
    for item in tokens.values():
        if not isinstance(item, dict):
            continue
        if worker_id and str(item.get("workerId") or "") != worker_id:
            runtime_install_ids = {str(r.get("installId") or "") for r in runtime_records if isinstance(r, dict) and str(r.get("workerId") or "") == worker_id}
            if str(item.get("installId") or "") not in runtime_install_ids:
                continue
        token = str(item.get("token") or "").strip()
        if len(token) < 20:
            incomplete.append(item)
            continue
        fresh, reason = _core_worker_fcm_runtime_freshness(item, runtime_records)
        if not fresh:
            stale_item = dict(item)
            stale_item["staleReason"] = reason
            stale.append(stale_item)
        if str(item.get("lastErrorCode") or "").upper() == "UNREGISTERED" or item.get("invalidatedAt"):
            invalidated.append(item)
    last = None
    for record in records:
        if last is None or int(record.get("lastSeenAt") or 0) > int(last.get("lastSeenAt") or 0):
            last = record
    if last is None:
        for group in (invalidated, stale, incomplete):
            for record in group:
                if last is None or int(record.get("lastSeenAt") or record.get("invalidatedAt") or record.get("registeredAt") or 0) > int(last.get("lastSeenAt") or last.get("invalidatedAt") or last.get("registeredAt") or 0):
                    last = record
    status = "ok" if records else "missing"
    if not records and invalidated:
        status = "needs_refresh"
    elif not records and stale:
        status = "stale"
    elif not records and incomplete:
        status = "incomplete"
    return {
        "active": len(records),
        "needsRefresh": bool(not records and invalidated),
        "invalidated": len(invalidated),
        "stale": len(stale),
        "incomplete": len(incomplete),
        "status": status,
        "lastSeenAt": int((last or {}).get("lastSeenAt") or 0),
        "lastPushAt": int((last or {}).get("lastPushAt") or 0),
        "lastPushStatus": _safe_short_text((last or {}).get("lastPushStatus"), 40),
        "lastError": _safe_short_text((last or {}).get("lastError") or (last or {}).get("staleReason"), 120),
        "lastErrorCode": _safe_short_text((last or {}).get("lastErrorCode"), 40),
        "lastAppVersion": _safe_short_text((last or {}).get("appVersion"), 48),
        "lastAppVersionCode": int((last or {}).get("appVersionCode") or 0),
        "permission": _safe_short_text((last or {}).get("permission"), 40),
    }


def _core_worker_app_heartbeats_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_heartbeats.json")


def _core_worker_app_runtime_snapshot_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_runtime_snapshot.json")


def _compact_core_worker_public_nested(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    keep = (
        "summary", "state", "prepared", "ok", "rootfsReady", "executorReady",
        "termuxRequired", "bedrockStartAllowed", "readyForBox64Install",
        "readyForBedrockStart", "distributionReady", "validationLevel",
        "rootfsDistributionReady", "rootfsValidationLevel",
        "rootfsSummary", "rootfsState", "rootfsImportState", "rootfsImportSummary",
        "runnerPreflightState", "runnerPreflightSummary", "runnerPreflightVersion",
        "runnerReady", "runnerBlocked", "runnerExecutionAllowed", "runnerRequirementsReady",
        "baseToolsReady", "prootNeedsLibtalloc", "prootDependencyReady",
        "runnerMissing", "lastResultAt", "sourceJobType", "nativeOk",
        "allowlist", "androidSdk", "blockers", "missing", "nextActions",
    )
    out = {}
    for key in keep:
        if key in value:
            raw = value.get(key)
            if isinstance(raw, str):
                out[key] = _safe_short_text(raw, 180)
            elif isinstance(raw, (bool, int, float)) or raw is None:
                out[key] = raw
            elif isinstance(raw, list):
                out[key] = [_safe_short_text(item, 80) for item in raw[:16]]
    return out


def _compact_core_worker_app_heartbeat_record(record: dict) -> dict:
    """Mantém apenas o status público necessário no heartbeat persistido.

    Antes o latestByInstallId/latestByWorkerId guardava runtime/status inteiros
    do APK. Com poucos heartbeats isso crescia para MBs e cada rota passava a
    ler/escrever payload grande dentro do processo do bot. O APK já envia tudo
    de novo periodicamente; persistimos só o snapshot de decisão/painel.
    """
    if not isinstance(record, dict):
        return {}
    keep_keys = (
        "receivedAt", "installId", "workerId", "deviceName", "source", "state",
        "reason", "appVersion", "appVersionCode", "profile", "runtimeMode",
        "capabilities", "supported_tasks", "supportedTasks", "appJobs",
        "internalRuntime", "internalRuntimeState", "termuxWorkerOnline",
        "jobsRuntime", "internalJobsQueue", "internalJobsRunning",
        "internalJobsPending", "fcmState", "batteryPercent",
        "batteryTemperatureC", "batteryCharging", "networkType", "networkVpn",
        "vpsPingMs", "updateState", "updateAvailable", "lastAppError",
        "ready", "diagnosticsSummary", "storageSummary", "bridgeSummary",
        "foregroundRuntimeActive", "foregroundRuntimeSummary",
        "coreLinuxSummary", "coreLinuxState", "coreLinuxPrepared",
        "rootfsValidationLevel", "rootfsDistributionReady", "rootfsSummary",
        "rootfsState", "rootfsImportState", "rootfsImportSummary",
        "runnerPreflightState", "runnerPreflightSummary", "runnerPreflightVersion",
        "runnerReady", "runnerBlocked", "runnerExecutionAllowed", "runnerRequirementsReady",
        "baseToolsReady", "prootNeedsLibtalloc", "prootDependencyReady",
        "bedrockSummary", "bedrockState", "bedrockReady",
        "bedrockRuntimeSummary", "bedrockRuntimeState",
        "bedrockRuntimeServiceActive", "bedrockInstallerSummary",
        "bedrockInstallerState", "bedrockInstallerNextAction",
        "notificationPermission", "remoteAddr",
    )
    out = {key: record.get(key) for key in keep_keys if key in record}
    out["capabilities"] = _safe_string_list(out.get("capabilities"), limit=80)
    supported = _safe_string_list(out.get("supported_tasks") or out.get("supportedTasks") or out.get("appJobs"), limit=120)
    out["supported_tasks"] = supported
    out["supportedTasks"] = list(supported)
    out["appJobs"] = list(supported)
    out["coreLinux"] = _compact_core_worker_public_nested(record.get("coreLinux") if isinstance(record.get("coreLinux"), dict) else {})
    out["nativeRuntime"] = _compact_core_worker_public_nested(record.get("nativeRuntime") if isinstance(record.get("nativeRuntime"), dict) else {})
    return out


def _write_core_worker_app_runtime_snapshot(record: dict) -> None:
    if not isinstance(record, dict) or not (record.get("installId") or record.get("workerId")):
        return
    try:
        snapshot = {
            "ok": True,
            "updatedAt": int(record.get("receivedAt") or time.time()),
            "latest": record,
        }
        _atomic_write_json(_core_worker_app_runtime_snapshot_path(), snapshot, mode=0o600)
    except Exception:
        pass


def _append_core_worker_app_heartbeat(payload: dict) -> dict:
    now = int(time.time())
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    if not install_id and not worker_id:
        raise ValueError("installId ou workerId ausente")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    battery = payload.get("battery") if isinstance(payload.get("battery"), dict) else status.get("battery") if isinstance(status.get("battery"), dict) else {}
    network = payload.get("network") if isinstance(payload.get("network"), dict) else status.get("network") if isinstance(status.get("network"), dict) else {}
    update = payload.get("update") if isinstance(payload.get("update"), dict) else status.get("update") if isinstance(status.get("update"), dict) else {}
    app_status = payload.get("app_status") if isinstance(payload.get("app_status"), dict) else status.get("app_status") if isinstance(status.get("app_status"), dict) else {}
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else status.get("storage") if isinstance(status.get("storage"), dict) else {}
    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else status.get("permissions") if isinstance(status.get("permissions"), dict) else {}
    core_linux = _first_dict(payload.get("coreLinux"), payload.get("core_linux"), runtime.get("coreLinux"), runtime.get("core_linux"), status.get("coreLinux"), status.get("core_linux"))
    native_runtime = _first_dict(payload.get("nativeRuntime"), payload.get("native_runtime"), runtime.get("nativeRuntime"), runtime.get("native_runtime"), status.get("nativeRuntime"), status.get("native_runtime"))
    capabilities = _first_list(payload.get("capabilities"), runtime.get("capabilities"), status.get("capabilities"), limit=80)
    supported_tasks = _first_list(payload.get("supported_tasks"), payload.get("supportedTasks"), payload.get("supportedJobs"), payload.get("app_jobs"), runtime.get("supported_tasks"), runtime.get("supportedTasks"), status.get("supported_tasks"), status.get("supportedTasks"), limit=120)
    record = {
        "receivedAt": now,
        "installId": install_id,
        "workerId": worker_id,
        "deviceName": _safe_short_text(payload.get("deviceName") or payload.get("device_name"), 80),
        "source": _safe_short_text(payload.get("source") or "core-worker-apk-internal-runtime", 64),
        "state": _safe_short_text(payload.get("state") or "internal_heartbeat", 48),
        "reason": _safe_short_text(payload.get("reason"), 64),
        "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48),
        "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0),
        "profile": _safe_short_text(payload.get("profile") or payload.get("profileLabel") or payload.get("profile_label"), 48),
        "runtimeMode": _safe_short_text(payload.get("runtime_mode") or runtime.get("mode") or "hybrid", 40),
        "capabilities": capabilities,
        "supported_tasks": supported_tasks,
        "supportedTasks": supported_tasks,
        "appJobs": _first_list(payload.get("app_jobs"), payload.get("supportedJobs"), supported_tasks, limit=120),
        "runtime": runtime if isinstance(runtime, dict) else {},
        "coreLinux": core_linux,
        "nativeRuntime": native_runtime,
        "internalRuntime": _safe_short_text(payload.get("internal_runtime") or runtime.get("internal_runtime") or "apk-heartbeat", 48),
        "internalRuntimeState": _safe_short_text(payload.get("internal_runtime_state") or runtime.get("internal_runtime_state"), 120),
        "termuxWorkerOnline": bool(payload.get("termuxWorkerOnline") or payload.get("localAgentOnline") or status.get("local_agent_online")),
        "jobsRuntime": _safe_short_text(payload.get("jobsRuntime") or runtime.get("jobs_runtime") or "termux", 40),
        "internalJobsQueue": _safe_short_text(runtime.get("internal_jobs_queue") or status.get("internal_jobs_queue"), 120),
        "internalJobsRunning": int(runtime.get("internal_jobs_running") or status.get("internal_jobs_running") or 0),
        "internalJobsPending": int(runtime.get("internal_jobs_pending") or status.get("internal_jobs_pending") or 0),
        "fcmState": _safe_short_text(status.get("fcm_state") or payload.get("fcm_state"), 80),
        "batteryPercent": int(battery.get("percent") or battery.get("percentage") or -1) if isinstance(battery, dict) else -1,
        "batteryTemperatureC": float(battery.get("temperature_c") or -1) if isinstance(battery, dict) else -1,
        "batteryCharging": bool(battery.get("charging")) if isinstance(battery, dict) else False,
        "networkType": _safe_short_text(network.get("type") if isinstance(network, dict) else "", 32),
        "networkVpn": bool(network.get("vpn")) if isinstance(network, dict) else False,
        "vpsPingMs": int(network.get("vps_ping_ms") or -1) if isinstance(network, dict) else -1,
        "updateState": _safe_short_text(update.get("state") if isinstance(update, dict) else "", 80),
        "updateAvailable": bool(update.get("available")) if isinstance(update, dict) else False,
        "lastAppError": _safe_short_text(app_status.get("last_error") if isinstance(app_status, dict) else "", 160),
        "ready": bool(app_status.get("ready")) if isinstance(app_status, dict) else False,
        "diagnosticsSummary": _safe_short_text(runtime.get("diagnostics_summary") or status.get("diagnostics_summary"), 160),
        "storageSummary": _safe_short_text(runtime.get("storage_summary") or (storage.get("summary") if isinstance(storage, dict) else ""), 120),
        "bridgeSummary": _safe_short_text(runtime.get("bridge_summary"), 120),
        "foregroundRuntimeActive": bool(runtime.get("foreground_runtime_active") or status.get("foreground_runtime_active")),
        "foregroundRuntimeSummary": _safe_short_text(runtime.get("foreground_runtime_summary") or status.get("foreground_runtime_summary"), 120),
        "coreLinuxSummary": _safe_short_text(runtime.get("core_linux_summary") or status.get("core_linux_summary") or core_linux.get("summary"), 160),
        "coreLinuxState": _safe_short_text(runtime.get("core_linux_state") or status.get("core_linux_state") or core_linux.get("state"), 80),
        "coreLinuxPrepared": bool(runtime.get("core_linux_prepared") or status.get("core_linux_prepared") or core_linux.get("prepared")),
        "bedrockSummary": _safe_short_text(runtime.get("bedrock_summary") or status.get("bedrock_summary"), 160),
        "bedrockState": _safe_short_text(runtime.get("bedrock_state") or status.get("bedrock_state"), 80),
        "bedrockReady": bool(runtime.get("bedrock_ready") or status.get("bedrock_ready")),
        "bedrockRuntimeSummary": _safe_short_text(runtime.get("bedrock_runtime_summary") or status.get("bedrock_runtime_summary"), 160),
        "bedrockRuntimeState": _safe_short_text(runtime.get("bedrock_runtime_state") or status.get("bedrock_runtime_state"), 80),
        "bedrockRuntimeServiceActive": bool(runtime.get("bedrock_runtime_service_active") or status.get("bedrock_runtime_service_active")),
        "bedrockInstallerSummary": _safe_short_text(runtime.get("bedrock_installer_summary") or status.get("bedrock_installer_summary"), 160),
        "bedrockInstallerState": _safe_short_text(runtime.get("bedrock_installer_state") or status.get("bedrock_installer_state"), 80),
        "bedrockInstallerNextAction": _safe_short_text(runtime.get("bedrock_installer_next_action") or status.get("bedrock_installer_next_action"), 120),
        "notificationPermission": _safe_short_text((permissions.get("notifications") if isinstance(permissions, dict) else ""), 32),
        "battery": battery if isinstance(battery, dict) else {},
        "network": network if isinstance(network, dict) else {},
        "update": update if isinstance(update, dict) else {},
        "appStatus": app_status if isinstance(app_status, dict) else {},
        "storage": storage if isinstance(storage, dict) else {},
        "permissions": permissions if isinstance(permissions, dict) else {},
        "remoteAddr": _safe_short_text(request.remote_addr or "", 64),
    }
    if _looks_like_core_worker_apk_v1(record):
        if not record.get("supported_tasks"):
            record["supported_tasks"] = _apk_v1_supported_tasks(record)
            record["supportedTasks"] = list(record["supported_tasks"])
            record["appJobs"] = list(record["supported_tasks"])
        if not record.get("capabilities"):
            record["capabilities"] = _apk_v1_capabilities(record)
        needs_core_fallback = (
            not isinstance(record.get("coreLinux"), dict)
            or not record.get("coreLinux")
            or not record.get("coreLinuxSummary")
            or not record.get("coreLinuxState")
            or not record.get("coreLinuxPrepared")
        )
        job_core_linux = _core_worker_app_latest_core_linux_state(worker_id=worker_id, install_id=install_id) if needs_core_fallback else {}
        if (not record.get("coreLinux") or not isinstance(record.get("coreLinux"), dict) or not record.get("coreLinux")) and isinstance(job_core_linux.get("coreLinux"), dict):
            record["coreLinux"] = job_core_linux.get("coreLinux")
        if not record.get("coreLinuxSummary") and job_core_linux.get("coreLinuxSummary"):
            record["coreLinuxSummary"] = job_core_linux.get("coreLinuxSummary")
        if not record.get("coreLinuxState") and job_core_linux.get("coreLinuxState"):
            record["coreLinuxState"] = job_core_linux.get("coreLinuxState")
        if not record.get("coreLinuxPrepared") and job_core_linux.get("coreLinuxPrepared"):
            record["coreLinuxPrepared"] = True
    if isinstance(record.get("coreLinux"), dict):
        promoted_core, promoted_summary, promoted_state, promoted_prepared = _core_worker_promote_rootfs_real_state(
            record.get("coreLinux"),
            _safe_short_text(record.get("coreLinuxSummary"), 160),
            _safe_short_text(record.get("coreLinuxState"), 80),
        )
        record["coreLinux"] = promoted_core
        if promoted_state:
            record["coreLinuxSummary"] = promoted_summary
            record["coreLinuxState"] = promoted_state
            record["coreLinuxPrepared"] = bool(record.get("coreLinuxPrepared") or promoted_prepared)
    path = _core_worker_app_heartbeats_path()
    key = install_id or worker_id or "unknown"
    with _core_worker_app_heartbeat_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        latest_by_install = data.get("latestByInstallId") if isinstance(data.get("latestByInstallId"), dict) else {}
        latest_by_worker = data.get("latestByWorkerId") if isinstance(data.get("latestByWorkerId"), dict) else {}
        events = data.get("events") if isinstance(data.get("events"), list) else []
        previous = latest_by_install.get(install_id) if install_id else None
        if not isinstance(previous, dict) and worker_id:
            previous = latest_by_worker.get(worker_id)
        record = _compact_core_worker_app_heartbeat_record(_merge_core_worker_app_heartbeat_record(record, previous))
        if isinstance(previous, dict) and CORE_WORKER_APP_HEARTBEAT_STORE_MIN_SECONDS > 0:
            prev_seen = int(previous.get("receivedAt") or 0)
            reason_lower = str(record.get("reason") or "").lower()
            source_same = str(previous.get("source") or "") == str(record.get("source") or "")
            manual_or_start = any(token in reason_lower for token in ("manual", "start", "opened", "ready", "install"))
            if source_same and prev_seen and now - prev_seen < CORE_WORKER_APP_HEARTBEAT_STORE_MIN_SECONDS and not manual_or_start:
                return previous
        latest_by_install[key] = record
        if worker_id:
            latest_by_worker[worker_id] = record
        events.append(record)
        events = events[-CORE_WORKER_APP_HEARTBEAT_EVENT_LIMIT:]
        data = {"ok": True, "updatedAt": now, "latestByInstallId": latest_by_install, "latestByWorkerId": latest_by_worker, "events": events}
        _atomic_write_json(path, data, mode=0o600)
        _write_core_worker_app_runtime_snapshot(record)
    return record


def _newest_core_worker_app_heartbeat(data: dict) -> dict | None:
    candidates: list[dict] = []
    for bucket_name in ("latestByWorkerId", "latestByInstallId"):
        bucket = data.get(bucket_name)
        if isinstance(bucket, dict):
            candidates.extend(value for value in bucket.values() if isinstance(value, dict))
    events = data.get("events")
    if isinstance(events, list):
        candidates.extend(value for value in events if isinstance(value, dict))
    if not candidates:
        return None
    candidates.sort(key=lambda item: int(item.get("receivedAt") or item.get("updatedAt") or 0), reverse=True)
    return candidates[0]




def _core_worker_core_linux_is_background_only(value: dict | None) -> bool:
    if not isinstance(value, dict) or not value:
        return True
    state = str(value.get("state") or "").lower()
    summary = str(value.get("summary") or "").lower()
    return "background-safe-runtime" in state or "heartbeat em background" in summary


def _core_worker_core_linux_is_richer(value: dict | None) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return bool(
        value.get("rootfsReady")
        or value.get("rootfsValidationLevel") == "real"
        or value.get("runnerPreflightState")
        or value.get("runnerPreflightSummary")
        or isinstance(value.get("runnerPreflight"), dict)
        or isinstance(value.get("embedded"), dict)
    )


def _core_worker_embedded_asset_public(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "present": bool(value.get("present")),
        "embeddedInApk": bool(value.get("embeddedInApk")),
        "allowedForFutureExecution": bool(value.get("allowedForFutureExecution")),
        "canExecute": bool(value.get("canExecute")),
        "placeholder": bool(value.get("placeholder")),
        "name": _safe_short_text(value.get("name"), 80),
        "kind": _safe_short_text(value.get("kind"), 40),
        "size": int(value.get("size") or 0),
        "sha256": _safe_short_text(value.get("sha256"), 96),
        "detectedBy": _safe_short_text(value.get("detectedBy"), 96),
        "path": _safe_short_text(value.get("path"), 220),
        "sourceApk": _safe_short_text(value.get("sourceApk"), 180),
        "zipEntry": _safe_short_text(value.get("zipEntry"), 120),
        "compressionMethod": _safe_short_text(value.get("compressionMethod"), 32),
    }


def _core_worker_embedded_summary(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for key in ("executor", "runner", "proot", "busybox", "box64"):
        item = value.get(key)
        if isinstance(item, dict):
            out[key] = _core_worker_embedded_asset_public(item)
    for key, item in value.items():
        if key in out or not isinstance(item, dict):
            continue
        out[_safe_short_text(key, 40)] = _core_worker_embedded_asset_public(item)
    return out


def _core_worker_core_linux_detail_score(value: dict | None) -> int:
    if not isinstance(value, dict) or not value:
        return 0
    score = 0
    if value.get("rootfsReady"):
        score += 10
    if str(value.get("rootfsValidationLevel") or "").lower() == "real" or str(value.get("state") or "").lower() == "rootfs_real_validated":
        score += 20
    runner = value.get("runnerPreflight") if isinstance(value.get("runnerPreflight"), dict) else {}
    if runner:
        score += 30
    embedded = value.get("embedded") if isinstance(value.get("embedded"), dict) else runner.get("embedded") if isinstance(runner.get("embedded"), dict) else {}
    if isinstance(embedded, dict):
        for item in embedded.values():
            if isinstance(item, dict) and item.get("present"):
                score += 10
                if item.get("sha256"):
                    score += 5
                if item.get("embeddedInApk"):
                    score += 5
    if isinstance(value.get("runnerMissing"), list) or isinstance(runner.get("missing"), list):
        score += 5
    return score


def _merge_core_worker_core_linux_state(base: dict | None, extra: dict | None) -> dict:
    if not isinstance(base, dict) or not base:
        return dict(extra) if isinstance(extra, dict) else {}
    if not isinstance(extra, dict) or not extra:
        return dict(base)
    merged = dict(base)
    # O heartbeat de background pode ser compacto. Preserve dados vivos dele, mas
    # use o último preflight/job para enriquecer rootfs/runner/assets.
    for key in (
        "runnerPreflight",
        "runnerPreflightState",
        "runnerPreflightSummary",
        "runnerPreflightVersion",
        "runnerReady",
        "runnerBlocked",
        "runnerExecutionAllowed",
        "runnerRequirementsReady",
        "runnerMissing",
        "embedded",
        "rootfs",
        "rootfsImport",
        "rootfsReady",
        "rootfsValidationLevel",
        "rootfsState",
        "rootfsSummary",
        "sourceJobType",
        "lastResultAt",
    ):
        value = extra.get(key)
        if value not in (None, "", [], {}):
            current = merged.get(key)
            if current in (None, "", [], {}) or _core_worker_core_linux_detail_score(extra) >= _core_worker_core_linux_detail_score(merged):
                merged[key] = value
    runner = merged.get("runnerPreflight") if isinstance(merged.get("runnerPreflight"), dict) else {}
    if isinstance(runner.get("embedded"), dict):
        merged["embedded"] = _core_worker_embedded_summary(runner.get("embedded"))
    elif isinstance(extra.get("embedded"), dict) and not isinstance(merged.get("embedded"), dict):
        merged["embedded"] = _core_worker_embedded_summary(extra.get("embedded"))
    embedded = merged.get("embedded") if isinstance(merged.get("embedded"), dict) else {}
    for key in ("executor", "runner", "proot", "busybox", "box64"):
        item = embedded.get(key) if isinstance(embedded.get(key), dict) else {}
        if item:
            public_key = f"{key}Embedded" if key != "runner" else "coreRunnerEmbedded"
            merged[public_key] = bool(item.get("embeddedInApk") or item.get("present"))
            merged[f"{key}Sha256"] = _safe_short_text(item.get("sha256"), 96)
            merged[f"{key}DetectedBy"] = _safe_short_text(item.get("detectedBy"), 96)
    merged["termuxRequired"] = False
    merged["bedrockStartAllowed"] = False
    return merged


def _merge_core_worker_app_heartbeat_record(record: dict, previous: dict | None) -> dict:
    if not isinstance(record, dict):
        return {}
    if not isinstance(previous, dict):
        previous = {}
    merged = dict(record)
    if _looks_like_core_worker_apk_v1(merged):
        if not _safe_string_list(merged.get("supported_tasks") or merged.get("supportedTasks") or merged.get("appJobs"), limit=120):
            fallback_supported = _safe_string_list(previous.get("supported_tasks") or previous.get("supportedTasks") or previous.get("appJobs"), limit=120) or _apk_v1_supported_tasks(merged)
            merged["supported_tasks"] = fallback_supported
            merged["supportedTasks"] = list(fallback_supported)
            merged["appJobs"] = list(fallback_supported)
        if not _safe_string_list(merged.get("capabilities"), limit=80):
            merged["capabilities"] = _safe_string_list(previous.get("capabilities"), limit=80) or _apk_v1_capabilities(merged)
    for key in ("coreLinux", "nativeRuntime"):
        value = merged.get(key)
        if (not isinstance(value, dict) or not value) and isinstance(previous.get(key), dict):
            merged[key] = previous.get(key)
    if _core_worker_core_linux_is_background_only(merged.get("coreLinux")) and _core_worker_core_linux_is_richer(previous.get("coreLinux")):
        merged_core = dict(previous.get("coreLinux") or {})
        merged_core["bedrockStartAllowed"] = False
        merged_core["termuxRequired"] = False
        merged["coreLinux"] = merged_core
        if _safe_short_text(previous.get("coreLinuxSummary"), 200):
            merged["coreLinuxSummary"] = previous.get("coreLinuxSummary")
        if _safe_short_text(previous.get("coreLinuxState"), 200):
            merged["coreLinuxState"] = previous.get("coreLinuxState")
        if previous.get("coreLinuxPrepared"):
            merged["coreLinuxPrepared"] = True
    for key in ("coreLinuxSummary", "coreLinuxState", "internalRuntimeState"):
        if not _safe_short_text(merged.get(key), 200) and _safe_short_text(previous.get(key), 200):
            merged[key] = previous.get(key)
    if not merged.get("coreLinuxPrepared") and previous.get("coreLinuxPrepared"):
        merged["coreLinuxPrepared"] = True
    return merged


def _core_worker_app_latest_core_linux_state(worker_id: str = "", install_id: str = "") -> dict:
    path = _core_worker_app_jobs_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {}
    results = data.get("results") if isinstance(data.get("results"), list) else []
    rows: list[dict] = []
    for item in reversed(results[-240:]):
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        if not ("core_linux" in typ or "rootfs" in typ or "native_executor" in typ or "runner" in typ):
            continue
        if install_id and str(item.get("installId") or "") not in ("", str(install_id)):
            continue
        if worker_id and str(item.get("workerId") or "") not in ("", str(worker_id)):
            continue
        rows.append(item)
    if not rows:
        return {}

    def row_score(row: dict) -> tuple[int, int]:
        typ = str(row.get("type") or "")
        received = int(row.get("receivedAt") or 0)
        if row.get("ok") and typ == "apk_core_linux_runner_preflight":
            return (100, received)
        if row.get("ok") and "rootfs_import" in typ:
            return (90, received)
        if row.get("ok") and typ in {"apk_core_linux_rootfs_validate", "apk_core_linux_rootfs_real_status", "apk_core_linux_rootfs_status"}:
            return (80, received)
        if row.get("ok") and typ == "apk_core_linux_runtime_smoke_test":
            return (70, received)
        if row.get("ok") and "core_linux" in typ:
            return (60, received)
        return (10, received)

    latest_ok = max(rows, key=row_score)
    result = latest_ok.get("result") if isinstance(latest_ok.get("result"), dict) else {}
    runner = result.get("coreLinuxRunner") if isinstance(result.get("coreLinuxRunner"), dict) else {}
    core_payload = result.get("coreLinux") if isinstance(result.get("coreLinux"), dict) else {}
    smoke = result.get("coreLinuxSmokeTest") if isinstance(result.get("coreLinuxSmokeTest"), dict) else {}
    runtime = smoke.get("runtime") if isinstance(smoke.get("runtime"), dict) else result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    rootfs = core_payload.get("rootfs") if isinstance(core_payload.get("rootfs"), dict) else smoke.get("rootfs") if isinstance(smoke.get("rootfs"), dict) else result.get("rootfs") if isinstance(result.get("rootfs"), dict) else runner.get("rootfs") if isinstance(runner.get("rootfs"), dict) else {}
    rootfs_import = core_payload.get("rootfsImport") if isinstance(core_payload.get("rootfsImport"), dict) else {}
    real = False
    for candidate in (core_payload, rootfs, rootfs_import, runner.get("rootfs") if isinstance(runner.get("rootfs"), dict) else {}):
        state_text = str(candidate.get("state") or candidate.get("rootfsState") or candidate.get("rootfsImportState") or "").lower()
        level = str(candidate.get("validationLevel") or candidate.get("rootfsValidationLevel") or "").lower()
        if "rootfs_real_validated" in state_text or level == "real":
            real = True
            break
    prepared = bool(
        real
        or runtime.get("ok")
        or runtime.get("rootfsReady")
        or rootfs.get("rootfsReady")
        or smoke.get("ok")
        or core_payload.get("prepared")
        or core_payload.get("rootfsReady")
    )
    summary = _safe_short_text(
        core_payload.get("summary")
        or rootfs.get("summary")
        or runner.get("rootfsSummary")
        or runtime.get("summary")
        or smoke.get("summary")
        or result.get("message")
        or latest_ok.get("message")
        or latest_ok.get("error"),
        180,
    )
    state = _safe_short_text(
        core_payload.get("state")
        or rootfs.get("state")
        or runtime.get("state")
        or smoke.get("state")
        or ("rootfs_real_validated" if real else "runtime_v1_ready" if prepared else "runtime_v1_pending"),
        80,
    )
    if real:
        state = "rootfs_real_validated"
        summary = summary or "Rootfs real validado · runner real ainda bloqueado"
    core_linux = dict(core_payload) if isinstance(core_payload, dict) else {}
    core_linux.update({
        "summary": summary,
        "state": state,
        "prepared": prepared,
        "rootfsReady": bool(real or runtime.get("rootfsReady") or rootfs.get("rootfsReady") or prepared),
        "executorReady": bool(runtime.get("executorReady") or prepared or runner.get("nativeExecutorReady")),
        "termuxRequired": False,
        "bedrockStartAllowed": False,
        "lastResultAt": int(latest_ok.get("receivedAt") or 0),
        "sourceJobType": _safe_short_text(latest_ok.get("type"), 80),
    })
    if real:
        core_linux["rootfsValidationLevel"] = "real"
        core_linux["rootfsState"] = "rootfs_real_validated"
        core_linux["rootfsSummary"] = _safe_short_text(rootfs.get("summary") or summary, 180)
    if runner:
        core_linux["runnerPreflightState"] = _safe_short_text(runner.get("state"), 80)
        core_linux["runnerPreflightSummary"] = _safe_short_text(runner.get("summary"), 180)
        core_linux["runnerPreflightVersion"] = int(runner.get("preflightVersion") or 1)
        core_linux["runnerReady"] = bool(runner.get("runnerReady"))
        core_linux["runnerBlocked"] = bool(runner.get("runnerBlocked", True))
        core_linux["runnerExecutionAllowed"] = bool(runner.get("runnerExecutionAllowed"))
        core_linux["runnerRequirementsReady"] = bool(runner.get("runnerRequirementsReady"))
        core_linux["runnerBaseRequirementsReady"] = bool(runner.get("runnerBaseRequirementsReady") or runner.get("termuxReductionReady"))
        core_linux["baseToolsReady"] = bool(runner.get("baseToolsReady"))
        core_linux["termuxReductionReady"] = bool(runner.get("termuxReductionReady"))
        core_linux["bedrockRequirementsReady"] = bool(runner.get("bedrockRequirementsReady"))
        if runner.get("phase"):
            core_linux["runnerPhase"] = _safe_short_text(runner.get("phase"), 80)
        if runner.get("phaseSummary"):
            core_linux["runnerPhaseSummary"] = _safe_short_text(runner.get("phaseSummary"), 180)
        current_missing = runner.get("currentMissing") if isinstance(runner.get("currentMissing"), list) else runner.get("missing") if isinstance(runner.get("missing"), list) else []
        future_missing = runner.get("futureMissing") if isinstance(runner.get("futureMissing"), list) else []
        if current_missing:
            core_linux["runnerMissing"] = [_safe_short_text(x, 120) for x in current_missing[:16]]
        if future_missing:
            core_linux["runnerFutureMissing"] = [_safe_short_text(x, 120) for x in future_missing[:16]]
        compact_runner = dict(runner)
        if isinstance(compact_runner.get("embedded"), dict):
            # Mantém um resumo estável dos assets no runtime-summary.
            # Antes isso ficava compacto demais e painéis/comandos liam `null`
            # mesmo quando o preflight real já tinha validado executor/runner.
            compact_runner["embedded"] = _core_worker_embedded_summary(compact_runner.get("embedded"))
        core_linux["runnerPreflight"] = compact_runner
        if isinstance(compact_runner.get("embedded"), dict):
            core_linux["embedded"] = compact_runner.get("embedded")
            for asset_key, asset in compact_runner.get("embedded", {}).items():
                if not isinstance(asset, dict):
                    continue
                if asset_key == "runner":
                    core_linux["coreRunnerEmbedded"] = bool(asset.get("embeddedInApk") or asset.get("present"))
                else:
                    core_linux[f"{asset_key}Embedded"] = bool(asset.get("embeddedInApk") or asset.get("present"))
                if asset.get("sha256"):
                    core_linux[f"{asset_key}Sha256"] = _safe_short_text(asset.get("sha256"), 96)
    return {"coreLinux": core_linux, "coreLinuxSummary": summary, "coreLinuxState": state, "coreLinuxPrepared": prepared}

def _core_worker_promote_rootfs_real_state(core_linux: dict, summary: str = "", state: str = "") -> tuple[dict, str, str, bool]:
    if not isinstance(core_linux, dict):
        core_linux = {}
    nested_rootfs = core_linux.get("rootfs") if isinstance(core_linux.get("rootfs"), dict) else {}
    nested_import = core_linux.get("rootfsImport") if isinstance(core_linux.get("rootfsImport"), dict) else {}
    candidates = [core_linux, nested_rootfs, nested_import]
    real = False
    for item in candidates:
        st = str(item.get("state") or item.get("rootfsState") or item.get("rootfsImportState") or "").lower()
        level = str(item.get("validationLevel") or item.get("rootfsValidationLevel") or "").lower()
        if "rootfs_real_validated" in st or level == "real":
            real = True
            break
    if real:
        root_summary = (
            core_linux.get("rootfsSummary")
            or nested_rootfs.get("summary")
            or core_linux.get("rootfsImportSummary")
            or nested_import.get("summary")
            or core_linux.get("summary")
            or summary
            or "Rootfs real validado · runner real ainda bloqueado"
        )
        core_linux = dict(core_linux)
        core_linux["state"] = "rootfs_real_validated"
        core_linux["summary"] = _safe_short_text(root_summary, 180)
        core_linux["prepared"] = True
        core_linux["rootfsReady"] = True
        core_linux["rootfsState"] = "rootfs_real_validated"
        core_linux["rootfsSummary"] = _safe_short_text(root_summary, 180)
        core_linux["rootfsValidationLevel"] = "real"
        core_linux["termuxRequired"] = False
        core_linux["bedrockStartAllowed"] = False
        return core_linux, _safe_short_text(root_summary, 160), "rootfs_real_validated", True
    return core_linux, summary, state, bool(core_linux.get("prepared"))


def _core_worker_app_runtime_public_summary(worker_id: str = "", install_id: str = "") -> dict:
    path = _core_worker_app_heartbeats_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    record = None
    if worker_id and isinstance(data.get("latestByWorkerId"), dict):
        record = data.get("latestByWorkerId", {}).get(str(worker_id))
    if record is None and install_id and isinstance(data.get("latestByInstallId"), dict):
        record = data.get("latestByInstallId", {}).get(str(install_id))
    if not isinstance(record, dict) and not worker_id and not install_id:
        try:
            snap = _load_json_cached(_core_worker_app_runtime_snapshot_path(), {})
            latest = snap.get("latest") if isinstance(snap, dict) else None
            if isinstance(latest, dict):
                record = latest
        except Exception:
            pass
    if not isinstance(record, dict):
        record = _newest_core_worker_app_heartbeat(data)
    if not isinstance(record, dict):
        return {"online": False, "lastSeenAt": 0, "state": "unknown"}
    seen = int(record.get("receivedAt") or 0)
    online = bool(seen and time.time() - seen <= 180)
    effective_worker_id = _safe_short_text(worker_id or record.get("workerId"), 80)
    effective_install_id = _safe_short_text(install_id or record.get("installId"), 80)
    supported_tasks = _safe_string_list(record.get("supported_tasks") or record.get("supportedTasks") or record.get("appJobs"), limit=120)
    capabilities = _safe_string_list(record.get("capabilities"), limit=80)
    if _looks_like_core_worker_apk_v1(record):
        if not supported_tasks:
            supported_tasks = _apk_v1_supported_tasks(record)
        if not capabilities:
            capabilities = _apk_v1_capabilities(record)
    runtime = record.get("runtime") if isinstance(record.get("runtime"), dict) else {}
    core_linux = _first_dict(record.get("coreLinux"), runtime.get("coreLinux"))
    native_runtime = _first_dict(record.get("nativeRuntime"), runtime.get("nativeRuntime"))
    light_jobs = _core_worker_app_jobs_public_summary(worker_id=effective_worker_id, install_id=effective_install_id)
    job_core_linux = _core_worker_app_latest_core_linux_state(worker_id=effective_worker_id, install_id=effective_install_id)
    if isinstance(job_core_linux.get("coreLinux"), dict):
        core_linux = _merge_core_worker_core_linux_state(core_linux, job_core_linux.get("coreLinux"))
    core_linux_summary = _safe_short_text(record.get("coreLinuxSummary") or core_linux.get("summary") or job_core_linux.get("coreLinuxSummary"), 160)
    core_linux_state = _safe_short_text(record.get("coreLinuxState") or core_linux.get("state") or job_core_linux.get("coreLinuxState"), 80)
    core_linux_prepared = bool(record.get("coreLinuxPrepared") or core_linux.get("prepared") or job_core_linux.get("coreLinuxPrepared"))
    core_linux, promoted_summary, promoted_state, promoted_prepared = _core_worker_promote_rootfs_real_state(core_linux, core_linux_summary, core_linux_state)
    if isinstance(job_core_linux.get("coreLinux"), dict):
        core_linux = _merge_core_worker_core_linux_state(core_linux, job_core_linux.get("coreLinux"))
    if promoted_state:
        core_linux_summary = promoted_summary or core_linux_summary
        core_linux_state = promoted_state or core_linux_state
        core_linux_prepared = core_linux_prepared or promoted_prepared
    if core_linux and not core_linux.get("prepared") and core_linux_prepared:
        core_linux = dict(core_linux)
        core_linux["prepared"] = True
    jobs_runtime = _safe_short_text(record.get("jobsRuntime"), 40)
    if "bedrock-installe" in jobs_runtime:
        jobs_runtime = "apk-native-runtime"
    return {
        "online": online,
        "lastSeenAt": seen,
        "source": _safe_short_text(record.get("source"), 64),
        "ageSeconds": max(0, int(time.time()) - seen) if seen else 0,
        "workerId": effective_worker_id,
        "installId": effective_install_id,
        "state": _safe_short_text(record.get("state"), 48),
        "appVersion": _safe_short_text(record.get("appVersion"), 48),
        "appVersionCode": int(record.get("appVersionCode") or 0),
        "runtimeMode": _safe_short_text(record.get("runtimeMode"), 40),
        "capabilities": capabilities,
        "supported_tasks": supported_tasks,
        "supportedTasks": supported_tasks,
        "internalRuntime": _safe_short_text(record.get("internalRuntime"), 48),
        "internalRuntimeState": _safe_short_text(record.get("internalRuntimeState"), 120),
        "termuxWorkerOnline": bool(record.get("termuxWorkerOnline")),
        "jobsRuntime": jobs_runtime,
        "fcmState": _safe_short_text(record.get("fcmState"), 80),
        "batteryPercent": int(record.get("batteryPercent") or -1),
        "batteryTemperatureC": float(record.get("batteryTemperatureC") or -1),
        "batteryCharging": bool(record.get("batteryCharging")),
        "networkType": _safe_short_text(record.get("networkType"), 32),
        "networkVpn": bool(record.get("networkVpn")),
        "vpsPingMs": int(record.get("vpsPingMs") or -1),
        "updateState": _safe_short_text(record.get("updateState"), 80),
        "updateAvailable": bool(record.get("updateAvailable")),
        "lastAppError": _safe_short_text(record.get("lastAppError"), 160),
        "ready": bool(record.get("ready")),
        "diagnosticsSummary": _safe_short_text(record.get("diagnosticsSummary"), 160),
        "storageSummary": _safe_short_text(record.get("storageSummary"), 120),
        "bridgeSummary": _safe_short_text(record.get("bridgeSummary"), 120),
        "coreLinuxSummary": core_linux_summary,
        "coreLinuxState": core_linux_state,
        "coreLinuxPrepared": core_linux_prepared,
        "coreLinux": core_linux,
        "nativeRuntime": native_runtime,
        "bedrockSummary": _safe_short_text(record.get("bedrockSummary"), 160),
        "bedrockState": _safe_short_text(record.get("bedrockState"), 80),
        "bedrockReady": bool(record.get("bedrockReady")),
        "notificationPermission": _safe_short_text(record.get("notificationPermission"), 32),
        "internalJobsQueue": _safe_short_text(record.get("internalJobsQueue"), 120),
        "internalJobsRunning": int(record.get("internalJobsRunning") or 0),
        "internalJobsPending": int(record.get("internalJobsPending") or 0),
        "lightJobs": light_jobs,
        "summary": _safe_short_text(core_linux_summary or record.get("diagnosticsSummary") or "APK interno aguardando diagnóstico", 180),
    }


def _core_worker_app_jobs_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_jobs.json")


def _core_worker_app_jobs_key(payload: dict) -> str:
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    return install_id or worker_id or "unknown"


CORE_WORKER_APP_JOB_ALIASES = {
    # Nomes antigos aceitos para não quebrar APKs/painéis entre patches.
    "apk_clear_app_cache": "apk_cache_cleanup",
    "apk_cleanup_runtime_cache": "apk_cache_cleanup",
    "apk_report_logs": "apk_upload_app_logs",
    "apk_status_refresh": "apk_sync_runtime_state",
    "apk_trim_runtime_cache": "apk_trim_cache",
    "apk_cleanup_update_storage": "apk_update_storage_cleanup",
    "apk_clear_update_downloads": "apk_update_storage_cleanup",
    "apk_refresh_status": "apk_refresh_runtime",
}

CORE_WORKER_APP_AUTO_JOB_TYPES = {
    "apk_ping",
    "apk_diagnostic",
    "apk_check_update",
    "apk_upload_app_logs",
    "apk_runtime_diagnostic",
    "apk_worker_bridge_status",
    "apk_device_diagnostic",
    "apk_push_diagnostic",
    "apk_update_diagnostic",
    "apk_job_history",
    "apk_native_worker_status",
    "apk_native_boot_status",
    "apk_local_shell_probe",
    "apk_core_linux_native_executor_probe",
    "apk_core_linux_native_executor_test",
    "apk_core_linux_native_runtime_status",
    "apk_core_linux_rootfs_status",
    "apk_core_linux_rootfs_validate",
}

CORE_WORKER_APP_MANUAL_JOB_TYPES = {
    "apk_download_small",
    "apk_verify_file",
    "apk_upload_report",
    "apk_test_vps_connection",
    "apk_sync_profile",
    "apk_sync_runtime_state",
    "apk_refresh_runtime",
    "apk_force_status_bundle",
    "apk_test_notification",
    "apk_repair_local_state",
    "apk_reset_job_history",
    "apk_trim_cache",
    "apk_update_storage_cleanup",
    "apk_sync_profile_now",
    "apk_verify_update_state",
    "apk_native_worker_status",
    "apk_native_boot_status",
    "apk_local_shell_probe",
    "apk_python_runtime_probe",
    "apk_python_health_check",
    "apk_python_runtime_info",
    "apk_python_status_bundle",
    "apk_python_storage_check",
    "apk_python_log_summary",
    "apk_python_network_diagnostic",
    "apk_python_runtime_files_check",
    "apk_linux_runtime_probe",
    "apk_linux_rootfs_probe",
    "apk_core_linux_rootfs_status",
    "apk_core_linux_rootfs_preflight",
    "apk_core_linux_rootfs_prepare",
    "apk_core_linux_rootfs_validate",
    "apk_core_linux_rootfs_repair",
    "apk_core_linux_rootfs_clean_staging",
    "apk_core_linux_rootfs_import_status",
    "apk_core_linux_rootfs_import_validate",
    "apk_core_linux_rootfs_import_abort",
    "apk_core_linux_rootfs_real_status",
    "apk_core_linux_rootfs_glibc_preflight",
    "apk_core_linux_runner_status",
    "apk_core_linux_runner_preflight",
    "apk_core_linux_runner_requirements",
    # V12.1/V13: smokes reais controlados são manuais/seguros, nunca automáticos.
    "apk_core_linux_runtime_smoke_test",
    "apk_core_linux_rootfs_smoke_test",
    "apk_core_linux_box64_preflight",
    "apk_core_linux_box64_smoke_test",
    "apk_linux_box64_probe",
    "apk_linux_provisioner_probe",
    "apk_linux_prepare_directories",
    "apk_linux_generate_setup_plan",
    "apk_core_linux_internal_probe",
    "apk_core_linux_internal_bootstrap",
    "apk_core_linux_executor_probe",
    "apk_core_linux_rootfs_manifest",
    "apk_core_linux_box64_manifest",
    "apk_core_linux_bedrock_preflight",
    "apk_core_linux_native_executor_probe",
    "apk_core_linux_native_executor_test",
    "apk_core_linux_native_runtime_status",
    "apk_core_linux_internal_repair",
    "apk_minecraft_bedrock_probe",
    "apk_minecraft_bedrock_status",
    "apk_minecraft_bedrock_requirements",
    "apk_minecraft_bedrock_install_plan",
    "apk_minecraft_bedrock_properties_template",
    "apk_runtime_foreground_probe",
    "apk_runtime_foreground_start",
    "apk_runtime_foreground_stop",
    "apk_linux_strategy_plan",
    "apk_linux_manifest_plan",
    "apk_minecraft_bedrock_assisted_install_plan",
    "apk_minecraft_bedrock_prepare_files",
    "apk_minecraft_bedrock_start_plan",
    "apk_minecraft_bedrock_stop_plan",
    "apk_minecraft_bedrock_logs_status",
    "apk_minecraft_bedrock_installer_status",
    "apk_minecraft_bedrock_validate_device",
    "apk_minecraft_bedrock_choose_strategy_plan",
    "apk_minecraft_bedrock_prepare_environment_plan",
    "apk_minecraft_bedrock_download_manifest",
    "apk_minecraft_bedrock_final_preflight",
    "apk_minecraft_bedrock_runtime_status",
    "apk_minecraft_bedrock_runtime_start",
    "apk_minecraft_bedrock_runtime_stop",
    "apk_minecraft_bedrock_runtime_logs",
    "apk_minecraft_bedrock_runner_status",
    "apk_minecraft_bedrock_runner_preflight",
    "apk_minecraft_bedrock_runner_start",
    "apk_minecraft_bedrock_runner_stop",
    "apk_minecraft_bedrock_console_tail",
    "apk_minecraft_bedrock_console_command",
    "apk_minecraft_bedrock_runtime_repair",
}

CORE_WORKER_APP_SAFE_JOB_TYPES = (
    CORE_WORKER_APP_AUTO_JOB_TYPES
    | CORE_WORKER_APP_MANUAL_JOB_TYPES
    | set(CORE_WORKER_APP_JOB_ALIASES.keys())
)

CORE_WORKER_APP_JOB_LABELS = {
    "apk_ping": "ping interno",
    "apk_diagnostic": "diagnóstico geral",
    "apk_check_update": "checagem de atualização",
    "apk_upload_app_logs": "logs internos",
    "apk_runtime_diagnostic": "diagnóstico do runtime",
    "apk_worker_bridge_status": "ponte APK/Termux",
    "apk_storage_diagnostic": "armazenamento",
    "apk_collect_status_bundle": "pacote completo",
    "apk_device_diagnostic": "aparelho",
    "apk_network_diagnostic": "rede",
    "apk_push_diagnostic": "push",
    "apk_update_diagnostic": "update",
    "apk_job_history": "histórico",
    "apk_cache_cleanup": "limpeza de cache",
    "apk_download_small": "download pequeno",
    "apk_verify_file": "verificar arquivo",
    "apk_upload_report": "enviar relatório",
    "apk_test_vps_connection": "teste de conexão VPS",
    "apk_sync_profile": "sincronizar perfil",
    "apk_sync_runtime_state": "sincronizar runtime",
    "apk_refresh_runtime": "atualizar runtime",
    "apk_force_status_bundle": "forçar pacote de status",
    "apk_test_notification": "teste de notificação",
    "apk_repair_local_state": "reparar estado local",
    "apk_reset_job_history": "limpar histórico",
    "apk_trim_cache": "limpar cache",
    "apk_update_storage_cleanup": "limpar updates",
    "apk_sync_profile_now": "sincronizar perfil agora",
    "apk_verify_update_state": "verificar atualização",
    "apk_native_worker_status": "worker nativo",
    "apk_native_boot_status": "boot nativo",
    "apk_local_shell_probe": "shell controlado",
    "apk_python_runtime_probe": "python interno",
    "apk_python_health_check": "Python health check",
    "apk_python_runtime_info": "Python runtime info",
    "apk_python_status_bundle": "Python status bundle",
    "apk_python_storage_check": "Python storage check",
    "apk_python_log_summary": "Python resumo de logs",
    "apk_python_network_diagnostic": "Python diagnóstico de rede",
    "apk_python_runtime_files_check": "Python arquivos runtime",
    "apk_linux_runtime_probe": "Core Linux runtime",
    "apk_linux_rootfs_probe": "Linux rootfs",
    "apk_core_linux_rootfs_status": "rootfs status",
    "apk_core_linux_rootfs_preflight": "rootfs preflight",
    "apk_core_linux_rootfs_prepare": "preparar rootfs",
    "apk_core_linux_rootfs_validate": "validar rootfs",
    "apk_core_linux_rootfs_repair": "reparar rootfs",
    "apk_core_linux_rootfs_clean_staging": "limpar staging rootfs",
    "apk_core_linux_rootfs_import_status": "status import rootfs",
    "apk_core_linux_rootfs_import_validate": "validar rootfs real",
    "apk_core_linux_rootfs_import_abort": "cancelar import rootfs",
    "apk_core_linux_rootfs_real_status": "rootfs real",
    "apk_core_linux_rootfs_glibc_preflight": "rootfs glibc preflight",
    "apk_core_linux_runner_status": "runner Core Linux status",
    "apk_core_linux_runner_preflight": "runner Core Linux preflight",
    "apk_core_linux_runner_requirements": "runner Core Linux requisitos",
    "apk_linux_box64_probe": "Box64",
    "apk_linux_provisioner_probe": "Linux provisioner",
    "apk_linux_prepare_directories": "preparar diretórios Linux",
    "apk_linux_generate_setup_plan": "plano setup Linux",
    "apk_core_linux_internal_probe": "Core Linux interno",
    "apk_core_linux_internal_bootstrap": "bootstrap interno",
    "apk_core_linux_executor_probe": "executor interno",
    "apk_core_linux_rootfs_manifest": "manifesto rootfs interno",
    "apk_core_linux_box64_manifest": "manifesto Box64 interno",
    "apk_core_linux_bedrock_preflight": "preflight Bedrock interno",
    "apk_core_linux_native_executor_probe": "executor nativo interno",
    "apk_core_linux_native_executor_test": "teste executor nativo",
    "apk_core_linux_native_runtime_status": "runtime nativo interno",
    "apk_core_linux_runtime_smoke_test": "smoke test Core Linux",
    "apk_core_linux_rootfs_smoke_test": "smoke rootfs PRoot",
    "apk_core_linux_box64_preflight": "Box64 intake/preflight",
    "apk_core_linux_box64_smoke_test": "Box64 smoke version/help",
    "apk_core_linux_internal_repair": "reparar Core Linux interno",
    "apk_minecraft_bedrock_probe": "Bedrock diagnóstico",
    "apk_minecraft_bedrock_status": "Bedrock status",
    "apk_minecraft_bedrock_requirements": "Bedrock requisitos",
    "apk_minecraft_bedrock_install_plan": "Bedrock plano instalação",
    "apk_minecraft_bedrock_properties_template": "Bedrock template propriedades",
    "apk_runtime_foreground_probe": "runtime persistente",
    "apk_runtime_foreground_start": "iniciar runtime persistente",
    "apk_runtime_foreground_stop": "parar runtime persistente",
    "apk_linux_strategy_plan": "estratégia Linux",
    "apk_linux_manifest_plan": "manifesto Linux",
    "apk_minecraft_bedrock_assisted_install_plan": "Bedrock assistido",
    "apk_minecraft_bedrock_prepare_files": "Bedrock preparar arquivos",
    "apk_minecraft_bedrock_start_plan": "Bedrock plano start",
    "apk_minecraft_bedrock_stop_plan": "Bedrock plano stop",
    "apk_minecraft_bedrock_logs_status": "Bedrock logs status",
    "apk_minecraft_bedrock_installer_status": "Bedrock instalador status",
    "apk_minecraft_bedrock_validate_device": "Bedrock validar aparelho",
    "apk_minecraft_bedrock_choose_strategy_plan": "Bedrock escolher estratégia",
    "apk_minecraft_bedrock_prepare_environment_plan": "Bedrock preparar ambiente",
    "apk_minecraft_bedrock_download_manifest": "Bedrock manifesto downloads",
    "apk_minecraft_bedrock_final_preflight": "Bedrock preflight final",
    "apk_minecraft_bedrock_runtime_status": "Bedrock runtime status",
    "apk_minecraft_bedrock_runtime_start": "Bedrock runtime start",
    "apk_minecraft_bedrock_runtime_stop": "Bedrock runtime stop",
    "apk_minecraft_bedrock_runtime_logs": "Bedrock runtime logs",
    "apk_minecraft_bedrock_runner_status": "Bedrock runner status",
    "apk_minecraft_bedrock_runner_preflight": "Bedrock runner preflight",
    "apk_minecraft_bedrock_runner_start": "Bedrock runner start",
    "apk_minecraft_bedrock_runner_stop": "Bedrock runner stop",
    "apk_minecraft_bedrock_console_tail": "Bedrock console tail",
    "apk_minecraft_bedrock_console_command": "Bedrock console comando",
    "apk_minecraft_bedrock_runtime_repair": "Bedrock runtime reparar",
}

def _core_worker_app_normalize_job_type(job_type: object) -> str:
    raw = _safe_short_text(job_type, 48)
    return CORE_WORKER_APP_JOB_ALIASES.get(raw, raw)


def _core_worker_app_job_class(job_type: object) -> str:
    normalized = _core_worker_app_normalize_job_type(job_type)
    if normalized in CORE_WORKER_APP_AUTO_JOB_TYPES:
        return "automatic"
    if normalized in CORE_WORKER_APP_MANUAL_JOB_TYPES:
        return "manual"
    return "unknown"


CORE_WORKER_APP_JOB_MAX_DELIVER = _env_int("CORE_WORKER_APP_JOB_MAX_DELIVER", 2, minimum=1, maximum=6)
CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS = 45
CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES = 1
CORE_WORKER_APP_JOB_RESULT_LIMIT = _env_int("CORE_WORKER_APP_JOB_RESULT_LIMIT", 120, minimum=40, maximum=300)


def _core_worker_app_safe_job_payload(job: dict) -> dict:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = _core_worker_app_normalize_job_type(job.get("type"))
    clean: dict = {}
    if job_type == "apk_download_small":
        raw_url = _safe_short_text(payload.get("url") or payload.get("path") or "/core-worker/app/latest.json", 220)
        # O APK também valida host/protocolo. Aqui só removemos entradas obviamente perigosas.
        if raw_url.startswith("/") or raw_url.startswith("http://") or raw_url.startswith("https://"):
            clean["url"] = raw_url
        try:
            max_bytes = int(payload.get("maxBytes") or payload.get("max_bytes") or 262144)
        except Exception:
            max_bytes = 262144
        clean["maxBytes"] = max(1024, min(max_bytes, 262144))
        expected_sha = _safe_short_text(payload.get("sha256") or payload.get("expectedSha256"), 80).lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            clean["sha256"] = expected_sha
    elif job_type == "apk_verify_file":
        filename = _safe_short_text(payload.get("file") or payload.get("cacheFile") or payload.get("name"), 80)
        if filename and not filename.startswith("/") and ".." not in filename.replace("\\", "/").split("/"):
            clean["file"] = filename
        expected_sha = _safe_short_text(payload.get("sha256") or payload.get("expectedSha256"), 80).lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            clean["sha256"] = expected_sha
    elif job_type == "apk_sync_profile":
        profile = _safe_short_text(payload.get("profile"), 40).lower()
        if profile in {"leve", "midia", "media", "normal", "completo", "builder", "turbo", "bedrock"}:
            clean["profile"] = profile
        detail = _safe_short_text(payload.get("detail") or payload.get("reason"), 80)
        if detail:
            clean["detail"] = detail
    elif job_type == "apk_core_linux_runtime_smoke_test":
        # V12.1: entregar ao APK somente o smoke allowlist fixo de ferramentas base.
        # Nada aqui vira comando livre; o APK também valida a allowlist localmente.
        stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
        if stage == "core-linux-base-tools-smoke-v12":
            clean["stage"] = stage
            clean["smokeStage"] = stage
            clean["allowlistOnly"] = True
            clean["forceFresh"] = True
    elif job_type == "apk_core_linux_rootfs_smoke_test":
        # V13: smoke rootfs via PRoot com comandos fixos no APK. Não aceita
        # comando vindo da VPS; o payload só transporta o estágio esperado.
        stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
        if stage in {"core-linux-rootfs-proot-smoke-v13", "core-linux-rootfs-proot-smoke-v13.1", "core-linux-rootfs-proot-smoke-v13.2", "core-linux-rootfs-proot-smoke-v13.3"}:
            clean["stage"] = stage
            clean["smokeStage"] = stage
            clean["allowlistOnly"] = True
            clean["forceFresh"] = True
    elif job_type == "apk_core_linux_rootfs_glibc_preflight":
        # V16.2: só preflight de rootfs/glibc com telemetria dedicada. Não executa binários, não toca Box64,
        # não abre shell e não aceita comando livre no payload.
        stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
        if stage in {"", CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE}:
            clean["stage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE
            clean["smokeStage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE
            clean["allowlistOnly"] = True
            clean["forceFresh"] = True
            clean["noBedrock"] = True
            clean["noBox64"] = True
    elif job_type in {"apk_core_linux_rootfs_import_status", "apk_core_linux_rootfs_import_validate", "apk_core_linux_rootfs_import_abort", "apk_core_linux_rootfs_real_status"}:
        # V17: jobs de telemetria/validação/aborto do import rootfs. O arquivo rootfs continua sendo escolhido manualmente no APK;
        # estes jobs não carregam arquivos, não executam binários importados, não abrem shell e não iniciam Box64/Bedrock.
        stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
        if stage in {"", CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE}:
            clean["stage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE
            clean["smokeStage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE
            clean["allowlistOnly"] = True
            clean["forceFresh"] = True
            clean["noBedrock"] = True
            clean["noBox64"] = True
            clean["noShell"] = True
            clean["noImportedBinaryExecution"] = True
    elif job_type == "apk_core_linux_box64_smoke_test":
        # V15.3.1: hard guard glibc isolado do Box64. O payload só informa o stage;
        # o APK não chama helpers Box64 nem abre o asset pesado enquanto o runtime glibc estiver ausente.
        stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
        if stage in {"core-linux-box64-version-smoke-v15", "core-linux-box64-version-smoke-v15.1", "core-linux-box64-version-smoke-v15.2", "core-linux-box64-glibc-preflight-v15.3", "core-linux-box64-glibc-preflight-v15.3.1"}:
            clean["stage"] = stage
            clean["smokeStage"] = stage
            clean["allowlistOnly"] = True
            clean["forceFresh"] = True
            clean["noBedrock"] = True
            clean["noUserBinary"] = True
    return clean


def _core_worker_app_job_timeout(job: dict) -> int:
    try:
        raw = int(job.get("timeoutSec") or job.get("timeout_seconds") or CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS)
    except Exception:
        raw = CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS
    return max(10, min(raw, 180))


def _core_worker_app_job_max_retries(job: dict) -> int:
    try:
        raw = int(job.get("maxRetries") or job.get("max_retries") or CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES)
    except Exception:
        raw = CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES
    return max(0, min(raw, 3))


CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB = "apk_core_linux_runtime_smoke_test"
CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_STAGE = "core-linux-base-tools-smoke-v12"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB = "apk_core_linux_rootfs_smoke_test"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_STAGE = "core-linux-rootfs-proot-smoke-v13.3"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_PREVIOUS_STAGE = "core-linux-rootfs-proot-smoke-v13.2"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_PREVIOUS2_STAGE = "core-linux-rootfs-proot-smoke-v13.1"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_LEGACY_STAGE = "core-linux-rootfs-proot-smoke-v13"
CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB = "apk_core_linux_box64_preflight"
CORE_WORKER_APP_CORE_LINUX_BOX64_V14_STAGE = "core-linux-box64-intake-preflight-v14.2.1"
CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB = "apk_core_linux_box64_smoke_test"
CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_STAGE = "core-linux-box64-glibc-preflight-v15.3.1"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB = "apk_core_linux_rootfs_glibc_preflight"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE = "core-linux-rootfs-glibc-intake-preflight-v16.2"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE = "core-linux-rootfs-staging-import-v17"
CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES = {
    "apk_core_linux_rootfs_import_status",
    "apk_core_linux_rootfs_import_validate",
    "apk_core_linux_rootfs_import_abort",
    "apk_core_linux_rootfs_real_status",
}
CORE_WORKER_APP_LOCAL_MANUAL_QUEUE_TYPES = {
    CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB,
    CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB,
    CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB,
    CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB,
    CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB,
    *CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES,
}


def _core_worker_app_payload_version_code(payload: dict) -> int:
    try:
        return int(payload.get("appVersionCode") or payload.get("app_version_code") or 0)
    except Exception:
        return 0


def _core_worker_app_job_fetch_blocker(job: dict, fetch_payload: dict) -> str:
    job_type = _core_worker_app_normalize_job_type(job.get("type"))
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    stage = _safe_short_text(payload.get("stage") or payload.get("smokeStage"), 80)
    version_code = _core_worker_app_payload_version_code(fetch_payload)
    if job_type == CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB:
        if version_code < 97:
            return "smoke V12 exige APK appVersionCode >= 97"
        if stage != CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_STAGE:
            return "smoke V12 exige payload.stage core-linux-base-tools-smoke-v12"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB:
        if version_code < 103:
            return "smoke V13.3 exige APK appVersionCode >= 103"
        if stage not in {CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_STAGE, CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_PREVIOUS_STAGE, CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_PREVIOUS2_STAGE, CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_LEGACY_STAGE}:
            return "smoke V13.3 exige payload.stage core-linux-rootfs-proot-smoke-v13.3"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB:
        if version_code < 107:
            return "Box64 V14.2.1 exige APK appVersionCode >= 107"
        if stage != CORE_WORKER_APP_CORE_LINUX_BOX64_V14_STAGE:
            return "Box64 V14.2.1 exige payload.stage core-linux-box64-intake-preflight-v14.2.1"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB:
        if version_code < 112:
            return "Box64 V15.3.1 exige APK appVersionCode >= 112"
        if stage != CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_STAGE:
            return "Box64 V15.3.1 exige payload.stage core-linux-box64-glibc-preflight-v15.3.1"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB:
        if version_code < 114:
            return "Rootfs glibc V16.2 exige APK appVersionCode >= 114"
        if stage != CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE:
            return "Rootfs glibc V16.2 exige payload.stage core-linux-rootfs-glibc-intake-preflight-v16.2"
    elif job_type in CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES:
        if version_code < 116:
            return "Rootfs import V17 exige APK appVersionCode >= 116"
        if stage != CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE:
            return "Rootfs import V17 exige payload.stage core-linux-rootfs-staging-import-v17"
    return ""


def _core_worker_app_job_matches(job: dict, install_id: str, worker_id: str) -> bool:
    target_install = str(job.get("installId") or job.get("install_id") or "").strip()
    target_worker = str(job.get("workerId") or job.get("worker_id") or "").strip()
    return (target_install and target_install == install_id) or (target_worker and target_worker == worker_id) or (not target_install and not target_worker)


def _core_worker_app_make_timeout_record(job: dict, now: int) -> dict:
    return {
        "receivedAt": now,
        "jobId": _safe_short_text(job.get("id"), 128),
        "type": _safe_short_text(job.get("type"), 48),
        "installId": _safe_short_text(job.get("installId") or job.get("install_id"), 80),
        "workerId": _safe_short_text(job.get("workerId") or job.get("worker_id"), 80),
        "appVersion": "",
        "appVersionCode": 0,
        "ok": False,
        "message": "job interno expirou antes do APK reportar resultado",
        "error": "timeout",
        "result": {"ok": False, "error": "timeout", "attempt": int(job.get("attempt") or 0)},
    }


def _core_worker_app_public_job(job: dict, now: int) -> dict:
    out = {
        "id": _safe_short_text(job.get("id"), 128),
        "type": _safe_short_text(job.get("type"), 48),
        "reason": _safe_short_text(job.get("reason"), 80),
        "issuedAt": int(job.get("issuedAt") or now),
        "title": _safe_short_text(job.get("title"), 80),
    }
    payload = _core_worker_app_safe_job_payload(job)
    if payload:
        out["payload"] = payload
    out["timeoutSec"] = _core_worker_app_job_timeout(job)
    out["attempt"] = int(job.get("attempt") or 0)
    return out


def _core_worker_app_job_catalog() -> dict:
    return {
        "automatic": sorted(CORE_WORKER_APP_AUTO_JOB_TYPES),
        "manual": sorted(CORE_WORKER_APP_MANUAL_JOB_TYPES),
        "aliases": dict(sorted(CORE_WORKER_APP_JOB_ALIASES.items())),
        "labels": {k: CORE_WORKER_APP_JOB_LABELS.get(k, k) for k in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES | CORE_WORKER_APP_MANUAL_JOB_TYPES)},
    }


def _core_worker_app_jobs_build_summaries(data: dict, now: int | None = None) -> dict:
    now = int(now or time.time())
    if not isinstance(data, dict):
        return {}
    results = data.get("results") if isinstance(data.get("results"), list) else []
    pending = data.get("pending") if isinstance(data.get("pending"), list) else []
    running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
    keys: set[str] = set()
    for item in results:
        if isinstance(item, dict):
            key = str(item.get("installId") or item.get("workerId") or "unknown")
            keys.add(key)
    for item in pending:
        if isinstance(item, dict):
            keys.add(str(item.get("installId") or item.get("workerId") or "unknown"))
    for item in running.values():
        if isinstance(item, dict):
            keys.add(str(item.get("installId") or item.get("workerId") or "unknown"))
    summaries: dict[str, dict] = {}
    for key in keys or {"unknown"}:
        latest_by_type: dict[str, dict] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("installId") or item.get("workerId") or "unknown")
            if item_key != key:
                continue
            typ = _core_worker_app_normalize_job_type(item.get("type"))
            prev = latest_by_type.get(typ)
            if prev is None or int(item.get("receivedAt") or 0) >= int(prev.get("receivedAt") or 0):
                latest_by_type[typ] = item
        auto_ok = 0
        auto_failed = 0
        auto_missing: list[str] = []
        failed_types: list[str] = []
        latest_public: dict[str, dict] = {}
        for typ in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
            rec = latest_by_type.get(typ)
            if not isinstance(rec, dict):
                auto_missing.append(typ)
                continue
            ok = bool(rec.get("ok"))
            if ok:
                auto_ok += 1
            else:
                auto_failed += 1
                failed_types.append(typ)
            latest_public[typ] = {
                "ok": ok,
                "message": _safe_short_text(rec.get("message") or rec.get("error"), 120),
                "receivedAt": int(rec.get("receivedAt") or 0),
            }
        pending_for_key = [j for j in pending if isinstance(j, dict) and str(j.get("installId") or j.get("workerId") or "unknown") == key]
        running_for_key = [j for j in running.values() if isinstance(j, dict) and str(j.get("installId") or j.get("workerId") or "unknown") == key]
        status = "ok"
        if auto_failed:
            status = "attention"
        elif auto_missing:
            status = "warming_up"
        summaries[key] = {
            "status": status,
            "autoTotal": len(CORE_WORKER_APP_AUTO_JOB_TYPES),
            "autoOk": auto_ok,
            "autoFailed": auto_failed,
            "autoMissing": auto_missing[:40],
            "manualTotal": len(CORE_WORKER_APP_MANUAL_JOB_TYPES),
            "manualTypes": sorted(CORE_WORKER_APP_MANUAL_JOB_TYPES),
            "failedTypes": failed_types[:20],
            "pending": len(pending_for_key),
            "running": len(running_for_key),
            "latestByType": latest_public,
            "updatedAt": now,
        }
    return summaries


def _core_worker_app_queue_internal_jobs_for_worker(worker_id: str = "", install_id: str = "", *, kinds: list[str] | None = None, reason: str = "manual-runtime-test", allow_manual: bool = False) -> dict:
    now = int(time.time())
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    key = install_id or worker_id or "unknown"
    allowed_types = set(CORE_WORKER_APP_AUTO_JOB_TYPES)
    if allow_manual:
        # V12.1: fila manual local só aceita jobs explicitamente allowlistados.
        allowed_types |= set(CORE_WORKER_APP_LOCAL_MANUAL_QUEUE_TYPES)
    job_types = [_core_worker_app_normalize_job_type(k) for k in (kinds or sorted(CORE_WORKER_APP_AUTO_JOB_TYPES))]
    job_types = [k for k in dict.fromkeys(job_types) if k in allowed_types]
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        legacy_sanitized = _core_worker_app_sanitize_legacy_runner_results(data)
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        existing_types = set()
        for item in pending:
            if isinstance(item, dict) and _core_worker_app_job_matches(item, install_id, worker_id):
                existing_types.add(_core_worker_app_normalize_job_type(item.get("type")))
        for item in running.values():
            if isinstance(item, dict) and _core_worker_app_job_matches(item, install_id, worker_id):
                existing_types.add(_core_worker_app_normalize_job_type(item.get("type")))
        created = []
        manual_short_slugs = {
            CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB: "cl-v12",
            CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB: "cl-rfs-v13",
            CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB: "cl-b64-v14",
            CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB: "cl-b64-v15",
            CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB: "cl-glibc-v16",
            "apk_core_linux_rootfs_import_status": "cl-rfs-status-v17",
            "apk_core_linux_rootfs_import_validate": "cl-rfs-valid-v17",
            "apk_core_linux_rootfs_import_abort": "cl-rfs-abort-v17",
            "apk_core_linux_rootfs_real_status": "cl-rfs-real-v17",
        }
        for typ in job_types:
            if typ in existing_types:
                continue
            # V16.2: IDs antigos começavam com o type longo e eram truncados para 64
            # caracteres no /jobs/fetch. No APK isso fazia retries manuais diferentes
            # virarem o mesmo jobId lembrado e serem ignorados como duplicados.
            # Mantém unicidade no começo do ID e evita colisão mesmo em clientes antigos.
            slug = manual_short_slugs.get(typ, typ.replace("apk_", "").replace("_", "-")[:18])
            job_id = f"m-{now}-{uuid.uuid4().hex[:8]}-{slug}-{len(created)}"
            job = {
                "id": job_id,
                "type": typ,
                "jobClass": _core_worker_app_job_class(typ),
                "reason": reason,
                "issuedAt": now,
                "title": CORE_WORKER_APP_JOB_LABELS.get(typ, typ),
                "status": "pending",
                "timeoutSec": 240 if typ in {CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB, CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB, CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB, CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB, CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB, *CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES} else CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS,
                "maxRetries": 1,
                "installId": install_id,
                "workerId": worker_id,
            }
            if typ == CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_STAGE, "allowlistOnly": True, "forceFresh": True}
            elif typ == CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_STAGE, "allowlistOnly": True, "forceFresh": True}
            elif typ == CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_BOX64_V14_STAGE, "allowlistOnly": True, "forceFresh": True, "noExecute": True}
            elif typ == CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_STAGE, "allowlistOnly": True, "forceFresh": True, "noBedrock": True, "noUserBinary": True}
            elif typ == CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE, "allowlistOnly": True, "forceFresh": True, "noBedrock": True, "noBox64": True}
            elif typ in CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES:
                job["payload"] = {"stage": CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE, "allowlistOnly": True, "forceFresh": True, "noBedrock": True, "noBox64": True, "noShell": True, "noImportedBinaryExecution": True}
            pending.append(job)
            created.append(job)
        data["pending"] = pending[-160:]
        data["runningByJobId"] = running
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        data["updatedAt"] = now
        data["ok"] = True
        _atomic_write_json(path, data, mode=0o600)
    return {"ok": True, "created": len(created), "requested": len(job_types), "workerId": worker_id, "installId": install_id, "types": [j.get("type") for j in created]}


def _core_worker_app_archive_pending_jobs(data: dict, *, job_type: str, install_id: str = "", worker_id: str = "", reason: str = "replaced") -> int:
    if not isinstance(data, dict):
        return 0
    now = int(time.time())
    normalized = _core_worker_app_normalize_job_type(job_type)
    pending = data.get("pending") if isinstance(data.get("pending"), list) else []
    archived = data.get("archived") if isinstance(data.get("archived"), list) else []
    kept = []
    count = 0
    for item in pending:
        if (
            isinstance(item, dict)
            and _core_worker_app_normalize_job_type(item.get("type")) == normalized
            and _core_worker_app_job_matches(item, install_id, worker_id)
        ):
            old = dict(item)
            old["status"] = "archived_stale_pending"
            old["archivedAt"] = now
            old["archiveReason"] = reason
            archived.append(old)
            count += 1
        else:
            kept.append(item)
    data["pending"] = kept[-CORE_WORKER_APP_PENDING_LIMIT:]
    data["archived"] = archived[-200:]
    return count


def _core_worker_app_queue_core_linux_smoke_v12(worker_id: str = "", install_id: str = "", *, reason: str = "manual-v12-base-tools-smoke") -> dict:
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v12-smoke",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_JOB],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_SMOKE_V12_STAGE
    queued["safety"] = "manual allowlist; sem shell livre; sem comando arbitrário; sem Box64/Bedrock"
    return queued




def _core_worker_app_queue_core_linux_rootfs_smoke_v13(worker_id: str = "", install_id: str = "", *, reason: str = "manual-v13-3-rootfs-android-runtime-bind-smoke") -> dict:
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v13-rootfs-smoke",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_STAGE
    queued["safety"] = "manual allowlist; sem shell livre; sem comando arbitrário; sem Box64/Bedrock"
    return queued



def _core_worker_app_queue_core_linux_box64_v14(worker_id: str = "", install_id: str = "", *, reason: str = "manual-v14-2-1-box64-asset-preflight") -> dict:
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v14-2-1-box64-asset-preflight",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_BOX64_V14_STAGE
    queued["safety"] = "manual audit/preflight V14.2.1; Box64 como asset; sem executar Box64; sem shell livre; sem comando arbitrário; sem Bedrock"
    return queued




def _core_worker_app_queue_core_linux_box64_smoke_v15(worker_id: str = "", install_id: str = "", *, reason: str = "manual-v15-box64-version-smoke") -> dict:
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v15-3-1-box64-glibc-hard-guard",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_STAGE
    queued["safety"] = "manual preflight V15.3.1; hard guard no handler; não chama helpers Box64; só File.exists glibc do rootfs; sem Bedrock; sem shell livre; sem comando arbitrário; sem binário x86_64 do usuário"
    return queued



def _core_worker_app_queue_core_linux_rootfs_glibc_v16(worker_id: str = "", install_id: str = "", *, reason: str = "manual-v16-2-rootfs-glibc-telemetry-dedupe") -> dict:
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v16-2-rootfs-glibc-telemetry-dedupe",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_STAGE
    queued["safety"] = "manual preflight V16.2; valida rootfs/glibc com telemetria dedicada e jobId único; sem executar binários importados; sem Box64; sem Bedrock; sem shell livre"
    return queued



def _core_worker_app_queue_core_linux_rootfs_import_v17(job_type: str, worker_id: str = "", install_id: str = "", *, reason: str = "manual-v17-rootfs-import-status") -> dict:
    job_type = _core_worker_app_normalize_job_type(job_type)
    if job_type not in CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES:
        return {"ok": False, "error": "tipo rootfs import V17 inválido", "type": job_type}
    path = _core_worker_app_jobs_path()
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        archived = _core_worker_app_archive_pending_jobs(
            data,
            job_type=job_type,
            install_id=install_id,
            worker_id=worker_id,
            reason="replace-with-single-v17-rootfs-import-job",
        )
        _atomic_write_json(path, data, mode=0o600)
    queued = _core_worker_app_queue_internal_jobs_for_worker(
        worker_id=worker_id,
        install_id=install_id,
        kinds=[job_type],
        reason=reason,
        allow_manual=True,
    )
    queued["archivedPending"] = archived
    queued["stage"] = CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_STAGE
    queued["safety"] = "manual V17 rootfs import/status; arquivo escolhido só no APK; sem executar binários importados; sem Box64; sem Bedrock; sem shell livre"
    return queued

def _core_worker_app_jobs_fetch(payload: dict) -> dict:
    now = int(time.time())
    if not isinstance(payload, dict):
        payload = {}
    key = _core_worker_app_jobs_key(payload)
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    supported = _first_list(payload.get("supportedJobs"), payload.get("supported_tasks"), payload.get("supportedTasks"), payload.get("app_jobs"), limit=160)
    supported_set = {
        _core_worker_app_normalize_job_type(item)
        for item in supported
        if str(item or "").strip()
    } & set(CORE_WORKER_APP_SAFE_JOB_TYPES)
    if not supported_set:
        supported_set = set(CORE_WORKER_APP_SAFE_JOB_TYPES)
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        legacy_sanitized = _core_worker_app_sanitize_legacy_runner_results(data)
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        results = data.get("results") if isinstance(data.get("results"), list) else []
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
        stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
        last_fetch = data.get("lastFetchByInstallId") if isinstance(data.get("lastFetchByInstallId"), dict) else {}
        auto_ping = data.get("lastAutoPingByInstallId") if isinstance(data.get("lastAutoPingByInstallId"), dict) else {}

        reason_lower = str(payload.get("reason") or "").lower()
        force_fetch = bool(payload.get("force")) or "manual" in reason_lower or "smoke" in reason_lower
        matching_pending_exists = any(isinstance(item, dict) and _core_worker_app_job_matches(item, install_id, worker_id) for item in pending)
        throttle_seconds = CORE_WORKER_APP_FETCH_THROTTLE_SECONDS
        if not matching_pending_exists and not running:
            throttle_seconds = max(throttle_seconds, CORE_WORKER_APP_FETCH_THROTTLE_WHEN_IDLE_SECONDS)
        if not force_fetch and throttle_seconds > 0:
            with _core_worker_app_fetch_throttle_lock:
                last_served = float(_core_worker_app_fetch_last_served.get(key) or 0.0)
                elapsed = time.time() - last_served if last_served else 999999.0
                if elapsed < throttle_seconds and not matching_pending_exists:
                    if legacy_sanitized:
                        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
                        _atomic_write_json(path, data, mode=0o600)
                    return {
                        "ok": True,
                        "jobs": [],
                        "count": 0,
                        "throttled": True,
                        "retryAfterSeconds": max(1, int(throttle_seconds - elapsed)),
                        "jobsRuntime": "apk-safe-internal-queue",
                        "queue": {"pending": len(pending), "running": len(running)},
                        "supported": sorted(CORE_WORKER_APP_SAFE_JOB_TYPES),
                    }
                _core_worker_app_fetch_last_served[key] = time.time()

        # Recoloca jobs expirados na fila, com retry limitado. Se estourar, registra timeout claro.
        refreshed_running: dict = {}
        for job_id, job in list(running.items()):
            if not isinstance(job, dict):
                continue
            deadline = int(job.get("deadlineAt") or 0)
            if deadline and deadline < now:
                attempts = int(job.get("attempt") or 0)
                if attempts <= _core_worker_app_job_max_retries(job):
                    job["status"] = "pending"
                    job["requeuedAt"] = now
                    pending.append(job)
                else:
                    results.append(_core_worker_app_make_timeout_record(job, now))
                continue
            refreshed_running[str(job_id)] = job
        running = refreshed_running

        deliver: list[dict] = []
        remaining: list[dict] = []
        delivered_keys: set[str] = set()
        for job in pending:
            if not isinstance(job, dict):
                continue
            job_type = _core_worker_app_normalize_job_type(job.get("type"))
            if job_type not in CORE_WORKER_APP_SAFE_JOB_TYPES or job_type not in supported_set:
                remaining.append(job)
                continue
            if not _core_worker_app_job_matches(job, install_id, worker_id):
                remaining.append(job)
                continue
            blocker = _core_worker_app_job_fetch_blocker(job, payload)
            if blocker:
                blocked_job = dict(job)
                blocked_job["lastFetchBlockReason"] = blocker
                blocked_job["lastFetchBlockAt"] = now
                remaining.append(blocked_job)
                continue
            job_id = _safe_short_text(job.get("id") or f"job-{uuid.uuid4().hex[:12]}", 128)
            if job_id in running or job_id in delivered_keys:
                remaining.append(job)
                continue
            if len(deliver) >= CORE_WORKER_APP_JOB_MAX_DELIVER:
                remaining.append(job)
                continue
            out = dict(job)
            out["id"] = job_id
            out["type"] = job_type
            out["jobClass"] = _core_worker_app_job_class(job_type)
            out["issuedAt"] = int(out.get("issuedAt") or now)
            out["status"] = "running"
            out["claimedAt"] = now
            out["deadlineAt"] = now + _core_worker_app_job_timeout(out)
            out["attempt"] = int(out.get("attempt") or 0) + 1
            if install_id and not out.get("installId"):
                out["installId"] = install_id
            if worker_id and not out.get("workerId"):
                out["workerId"] = worker_id
            running[job_id] = out
            deliver.append(out)
            delivered_keys.add(job_id)

        auto_intervals = {
            "apk_ping": int(os.getenv("CORE_WORKER_APP_AUTO_PING_INTERVAL_SECONDS", "900") or "900"),
            "apk_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_DIAGNOSTIC_INTERVAL_SECONDS", "1800") or "1800"),
            "apk_check_update": int(os.getenv("CORE_WORKER_APP_AUTO_UPDATE_CHECK_INTERVAL_SECONDS", "2700") or "2700"),
            "apk_upload_app_logs": int(os.getenv("CORE_WORKER_APP_AUTO_REPORT_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_runtime_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_RUNTIME_DIAGNOSTIC_INTERVAL_SECONDS", "2100") or "2100"),
            "apk_worker_bridge_status": int(os.getenv("CORE_WORKER_APP_AUTO_BRIDGE_INTERVAL_SECONDS", "2100") or "2100"),
            "apk_storage_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_STORAGE_DIAGNOSTIC_INTERVAL_SECONDS", "5400") or "5400"),
            "apk_collect_status_bundle": int(os.getenv("CORE_WORKER_APP_AUTO_STATUS_BUNDLE_INTERVAL_SECONDS", "7200") or "7200"),
            "apk_device_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_DEVICE_DIAGNOSTIC_INTERVAL_SECONDS", "2400") or "2400"),
            "apk_network_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_NETWORK_DIAGNOSTIC_INTERVAL_SECONDS", "1800") or "1800"),
            "apk_push_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_PUSH_DIAGNOSTIC_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_update_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_UPDATE_DIAGNOSTIC_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_job_history": int(os.getenv("CORE_WORKER_APP_AUTO_JOB_HISTORY_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_cache_cleanup": int(os.getenv("CORE_WORKER_APP_AUTO_CACHE_CLEANUP_INTERVAL_SECONDS", "21600") or "21600"),
            "apk_native_worker_status": int(os.getenv("CORE_WORKER_APP_AUTO_NATIVE_WORKER_STATUS_INTERVAL_SECONDS", "2700") or "2700"),
            "apk_native_boot_status": int(os.getenv("CORE_WORKER_APP_AUTO_NATIVE_BOOT_STATUS_INTERVAL_SECONDS", "7200") or "7200"),
            "apk_local_shell_probe": int(os.getenv("CORE_WORKER_APP_AUTO_LOCAL_SHELL_PROBE_INTERVAL_SECONDS", "7200") or "7200"),
            "apk_python_runtime_probe": int(os.getenv("CORE_WORKER_APP_AUTO_PYTHON_RUNTIME_PROBE_INTERVAL_SECONDS", "7200") or "7200"),
            "apk_linux_runtime_probe": int(os.getenv("CORE_WORKER_APP_AUTO_LINUX_RUNTIME_PROBE_INTERVAL_SECONDS", "10800") or "10800"),
        }

        def _maybe_auto_job(kind: str, interval: int, title: str, reason: str) -> None:
            kind = _core_worker_app_normalize_job_type(kind)
            if len(deliver) >= CORE_WORKER_APP_JOB_MAX_DELIVER or interval <= 0 or kind not in supported_set:
                return
            # Não cria outro job igual se já existe rodando/pendente para esta instalação/worker.
            for existing in list(running.values()) + remaining + deliver:
                if not isinstance(existing, dict):
                    continue
                if _core_worker_app_normalize_job_type(existing.get("type")) == kind and _core_worker_app_job_matches(existing, install_id, worker_id):
                    return
            auto_key = f"{key}:{kind}"
            try:
                last = int(auto_ping.get(auto_key) or 0)
            except Exception:
                last = 0
            if now - last >= interval:
                job_id = f"auto-{kind.replace('_', '-')}-{key[:16]}-{now}"
                job = {"id": job_id, "type": kind, "jobClass": "automatic", "reason": reason, "issuedAt": now, "title": title, "status": "running", "claimedAt": now, "deadlineAt": now + CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS, "attempt": 1, "installId": install_id, "workerId": worker_id}
                running[job_id] = job
                deliver.append(job)
                auto_ping[auto_key] = now

        for kind in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
            interval = int(auto_intervals.get(kind) or 0)
            title = CORE_WORKER_APP_JOB_LABELS.get(kind, kind)
            _maybe_auto_job(kind, interval, title, "auto-" + kind.replace("apk_", "").replace("_", "-"))

        queue_stats = stats.get(key) if isinstance(stats.get(key), dict) else {}
        queue_stats["lastFetchAt"] = now
        queue_stats["jobsReturned"] = len(deliver)
        queue_stats["pending"] = len(remaining)
        queue_stats["running"] = len(running)
        stats[key] = queue_stats
        last_fetch[key] = {"at": now, "installId": install_id, "workerId": worker_id, "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48), "appVersionCode": _core_worker_app_payload_version_code(payload), "jobsReturned": len(deliver), "pending": len(remaining), "running": len(running)}
        data["pending"] = remaining[-CORE_WORKER_APP_PENDING_LIMIT:]
        data["runningByJobId"] = running
        data["results"] = results[-CORE_WORKER_APP_JOB_RESULT_LIMIT:]
        data["historyByInstallId"] = history
        data["statsByInstallId"] = stats
        data["lastFetchByInstallId"] = last_fetch
        data["lastAutoPingByInstallId"] = auto_ping
        data["updatedAt"] = now
        data["ok"] = True
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        _atomic_write_json(path, data, mode=0o600)
    public_jobs = [_core_worker_app_public_job(job, now) for job in deliver]
    return {"ok": True, "jobs": public_jobs, "count": len(public_jobs), "jobsRuntime": "apk-safe-internal-queue", "queue": {"pending": len(remaining), "running": len(running)}, "supported": sorted(CORE_WORKER_APP_SAFE_JOB_TYPES), "catalog": _core_worker_app_job_catalog()}


def _core_worker_app_jobs_result(payload: dict) -> dict:
    now = int(time.time())
    if not isinstance(payload, dict):
        payload = {}
    job_id = _safe_short_text(payload.get("jobId") or payload.get("job_id"), 128)
    job_type = _core_worker_app_normalize_job_type(payload.get("type"))
    if not job_id:
        raise ValueError("jobId ausente")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    record = {"receivedAt": now, "jobId": job_id, "type": job_type, "jobClass": _core_worker_app_job_class(job_type), "installId": _safe_short_text(payload.get("installId") or payload.get("install_id"), 80), "workerId": _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80), "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48), "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0), "ok": bool(result.get("ok")), "message": _safe_short_text(result.get("message") or result.get("summary"), 160), "error": _safe_short_text(result.get("error"), 160), "result": result}
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = _load_json_cached(path, {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        _core_worker_app_sanitize_legacy_runner_results(data)
        results = data.get("results") if isinstance(data.get("results"), list) else []
        latest = data.get("latestResultByInstallId") if isinstance(data.get("latestResultByInstallId"), dict) else {}
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
        stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
        key = record["installId"] or record["workerId"] or "unknown"
        running.pop(job_id, None)
        results.append(record)
        hist = history.get(key) if isinstance(history.get(key), list) else []
        hist.append({"at": now, "jobId": job_id, "type": job_type, "ok": bool(record.get("ok")), "message": _safe_short_text(record.get("message") or record.get("error"), 120)})
        history[key] = hist[-24:]
        queue_stats = stats.get(key) if isinstance(stats.get(key), dict) else {}
        queue_stats["lastResultAt"] = now
        queue_stats["lastType"] = job_type
        queue_stats["lastOk"] = bool(record.get("ok"))
        queue_stats["lastMessage"] = _safe_short_text(record.get("message") or record.get("error"), 120)
        if bool(record.get("ok")):
            queue_stats["okCount"] = int(queue_stats.get("okCount") or 0) + 1
        else:
            queue_stats["failCount"] = int(queue_stats.get("failCount") or 0) + 1
        stats[key] = queue_stats
        data["results"] = results[-CORE_WORKER_APP_JOB_RESULT_LIMIT:]
        latest[key] = record
        data["latestResultByInstallId"] = latest
        data["runningByJobId"] = running
        data["historyByInstallId"] = history
        data["statsByInstallId"] = stats
        data["updatedAt"] = now
        data["ok"] = True
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        _atomic_write_json(path, data, mode=0o600)
    return record


def _core_worker_app_jobs_public_summary(worker_id: str = "", install_id: str = "") -> dict:
    path = _core_worker_app_jobs_path()
    try:
        data = _load_json_cached(path, {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if _core_worker_app_sanitize_legacy_runner_results(data):
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, int(time.time()))
        with contextlib.suppress(Exception):
            _atomic_write_json(path, data, mode=0o600)
    latest = data.get("latestResultByInstallId") if isinstance(data.get("latestResultByInstallId"), dict) else {}
    stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
    history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
    record = None
    if install_id:
        record = latest.get(install_id)
    if not isinstance(record, dict) and isinstance(data.get("results"), list):
        for item in reversed(data.get("results") or []):
            if not isinstance(item, dict):
                continue
            if worker_id and str(item.get("workerId") or "") == worker_id:
                record = item
                break
    pending = data.get("pending") if isinstance(data.get("pending"), list) else []
    running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
    pending_count = len(pending)
    running_count = len(running)
    key = install_id or worker_id or "unknown"
    stat = stats.get(key) if isinstance(stats.get(key), dict) else {}
    hist = history.get(key) if isinstance(history.get(key), list) else []
    return {"pending": pending_count, "running": running_count, "lastResultAt": int((record or {}).get("receivedAt") or 0), "lastType": _safe_short_text((record or {}).get("type"), 48), "lastOk": bool((record or {}).get("ok")) if isinstance(record, dict) else False, "lastMessage": _safe_short_text((record or {}).get("message") or (record or {}).get("error"), 120), "okCount": int((stat or {}).get("okCount") or 0), "failCount": int((stat or {}).get("failCount") or 0), "history": hist[-5:]}


def _firebase_service_account_path() -> str:
    return _expand_path(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "/home/ubuntu/secrets/firebase-service-account.json")


def _firebase_access_token_and_project() -> tuple[str, str]:
    path = _firebase_service_account_path()
    if not os.path.isfile(path):
        raise RuntimeError(f"service account Firebase ausente em {path}")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except Exception as exc:
        raise RuntimeError(f"dependência google-auth indisponível: {type(exc).__name__}") from exc
    scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
    credentials = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    credentials.refresh(GoogleAuthRequest())
    project_id = getattr(credentials, "project_id", "") or ""
    if not project_id:
        with open(path, "r", encoding="utf-8") as fh:
            project_id = str((json.load(fh) or {}).get("project_id") or "")
    if not project_id:
        raise RuntimeError("project_id ausente na service account Firebase")
    return str(credentials.token or ""), project_id


def _fcm_send_v1(token: str, data_payload: dict) -> tuple[bool, str]:
    access_token, project_id = _firebase_access_token_and_project()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    clean_data = {str(k): str(v) for k, v in (data_payload or {}).items() if v is not None}
    body = json.dumps({
        "message": {
            "token": token,
            "android": {"priority": "HIGH"},
            "data": clean_data,
        }
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            response_body = resp.read(8192).decode("utf-8", errors="replace")
            return True, response_body[:500]
    except urllib.error.HTTPError as exc:
        response_body = exc.read(8192).decode("utf-8", errors="replace") if exc.fp else ""
        return False, f"HTTP {exc.code}: {response_body[:500]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"


def _send_core_worker_apk_update_pushes(manifest: dict, *, reason: str = "apk_published") -> dict:
    if not isinstance(manifest, dict):
        return {"ok": False, "error": "manifest inválido"}
    notification_id = _safe_short_text(manifest.get("notificationId") or _notification_event_id(version_name=str(manifest.get("versionName") or ""), version_code=int(manifest.get("versionCode") or 0), sha256=str(manifest.get("sha256") or "")), 96)
    version_name = _safe_short_text(manifest.get("versionName"), 48)
    version_code = int(manifest.get("versionCode") or 0)
    data_payload = {
        "type": "apk_update_available",
        "source": "core-worker-vps",
        "reason": reason,
        "notificationId": notification_id,
        "versionName": version_name,
        "versionCode": str(version_code),
        "apkUrl": str(manifest.get("apkUrl") or ""),
        "downloadUrl": str(manifest.get("downloadUrl") or manifest.get("directApkUrl") or ""),
        "sha256": str(manifest.get("sha256") or ""),
    }
    records = _active_core_worker_fcm_records()
    sent = 0
    failed = 0
    now = int(time.time())
    store = _load_core_worker_fcm_tokens()
    tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
    for record in records:
        token = str(record.get("token") or "").strip()
        token_hash = str(record.get("tokenHash") or _fcm_token_hash(token))
        ok, detail = _fcm_send_v1(token, data_payload)
        current = tokens.get(token_hash) if isinstance(tokens.get(token_hash), dict) else record
        unregistered = (not ok) and _fcm_error_is_unregistered(detail)
        current["lastPushAt"] = now
        current["lastNotificationId"] = notification_id
        if ok:
            current["lastPushStatus"] = "sent"
            current["lastError"] = ""
            current["lastErrorCode"] = ""
            current["refreshRequired"] = False
            current["active"] = True
        elif unregistered:
            current["lastPushStatus"] = "unregistered"
            current["lastErrorCode"] = "UNREGISTERED"
            current["lastError"] = _fcm_unregistered_public_error()
            current["refreshRequired"] = True
            current["active"] = False
            current["invalidatedAt"] = now
        else:
            current["lastPushStatus"] = "failed"
            current["lastError"] = _safe_short_text(detail, 180)
        tokens[token_hash] = current
        event_detail = "push FCM enviado pela VPS" if ok else (_fcm_unregistered_public_error() if unregistered else detail)
        event = {
            "notificationId": notification_id,
            "state": "fcm_sent" if ok else ("fcm_token_unregistered" if unregistered else "fcm_failed"),
            "delivered": False,
            "versionName": version_name,
            "versionCode": version_code,
            "appVersion": _safe_short_text(current.get("appVersion"), 48),
            "appVersionCode": int(current.get("appVersionCode") or 0),
            "workerId": _safe_short_text(current.get("workerId"), 80),
            "installId": _safe_short_text(current.get("installId"), 80),
            "permission": _safe_short_text(current.get("permission"), 40),
            "detail": event_detail,
        }
        with contextlib.suppress(Exception):
            with app.test_request_context(headers={"X-Internal-Core-Worker": "fcm"}):
                # _append_core_worker_notification_event usa request.remote_addr; contexto interno evita depender de uma requisição real.
                _append_core_worker_notification_event(event)
        sent += 1 if ok else 0
        failed += 0 if ok else 1
    store["tokens"] = tokens
    store["updatedAt"] = now
    store["lastBroadcast"] = {"notificationId": notification_id, "sent": sent, "failed": failed, "reason": reason, "at": now}
    _save_core_worker_fcm_tokens(store)
    return {"ok": failed == 0, "tokens": len(records), "sent": sent, "failed": failed, "notificationId": notification_id}


def _kick_core_worker_fcm_push(manifest: dict, *, reason: str = "apk_published") -> None:
    if not isinstance(manifest, dict) or not manifest.get("notificationRequested"):
        return
    def runner() -> None:
        try:
            _send_core_worker_apk_update_pushes(manifest, reason=reason)
        except Exception as exc:
            with contextlib.suppress(Exception):
                with app.test_request_context(headers={"X-Internal-Core-Worker": "fcm"}):
                    _append_core_worker_notification_event({
                        "notificationId": manifest.get("notificationId") or "fcm",
                        "state": "fcm_failed",
                        "delivered": False,
                        "versionName": manifest.get("versionName"),
                        "versionCode": manifest.get("versionCode") or 0,
                        "detail": f"falha geral enviando FCM: {type(exc).__name__}: {str(exc)[:180]}",
                    })
    try:
        threading.Thread(target=runner, name="core-worker-fcm-push", daemon=True).start()
    except Exception:
        pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def _expand_path(value: str | None) -> str:
    return os.path.abspath(os.path.expanduser(str(value or "")))


def _find_android_build_tool(tool: str) -> str | None:
    explicit = os.getenv(f"CORE_WORKER_APK_{tool.upper()}")
    if explicit and os.path.isfile(_expand_path(explicit)):
        return _expand_path(explicit)
    found = shutil.which(tool)
    if found:
        return found
    roots = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.getenv(env_name)
        if root:
            roots.append(_expand_path(root))
    roots.extend([
        _expand_path("~/android-sdk"),
        _expand_path("~/Android/Sdk"),
        "/opt/android-sdk",
        "/usr/lib/android-sdk",
    ])
    candidates: list[str] = []
    for root in dict.fromkeys(roots):
        build_tools = os.path.join(root, "build-tools")
        if not os.path.isdir(build_tools):
            continue
        for version in sorted(os.listdir(build_tools), reverse=True):
            candidates.append(os.path.join(build_tools, version, tool))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _strip_apk_signatures(source: str, target: str) -> None:
    """Recria o APK removendo assinaturas antigas.

    Usado apenas se a VPS for configurada explicitamente para assinatura fixa
    via CORE_WORKER_APK_SIGNING_MODE. No fluxo padrão, a VPS aceita o APK debug
    já assinado pelo phone worker builder e não precisa de Android SDK.
    """
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = str(info.filename or "")
            upper = name.upper()
            if upper.startswith("META-INF/") and (
                upper.endswith(".RSA") or upper.endswith(".DSA") or upper.endswith(".EC") or upper.endswith(".SF") or upper == "META-INF/MANIFEST.MF"
            ):
                continue
            data = zin.read(info.filename)
            info.date_time = info.date_time or (1980, 1, 1, 0, 0, 0)
            zout.writestr(info, data)


def _fixed_apk_signing_config() -> dict[str, str] | None:
    # A VPS Oracle de 1 GB não deve depender de Android SDK/apksigner para o
    # fluxo normal. O phone worker builder gera o APK debug já assinado pelo
    # Gradle; a VPS só valida, publica e notifica. Assinatura fixa na VPS fica
    # opcional para um futuro release build via CORE_WORKER_APK_SIGNING_MODE.
    raw_mode = os.getenv("CORE_WORKER_APK_SIGNING_MODE")
    if _env_bool("CORE_WORKER_APK_SIGNING_DISABLED", False):
        return None
    if raw_mode is None or not str(raw_mode).strip():
        return None
    mode = str(raw_mode).strip().lower()
    if mode in {"off", "none", "disabled", "false", "0", "worker", "uploaded", "phone-worker", "debug", "release"}:
        # Patch 57: mesmo se sobrou CORE_WORKER_APK_SIGNING_MODE=debug no .env,
        # a VPS não deve tentar assinar. O phone worker assina com a keystore
        # compatível recebida pelo payload temporário.
        return None
    if mode == "vps-debug":
        keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "~/.android/debug.keystore")
        return {
            "mode": "vps-debug",
            "keystore": keystore,
            "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "androiddebugkey",
            "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
            "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
        }
    keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "")
    return {
        "mode": mode or "keystore",
        "keystore": keystore,
        "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "",
        "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
        "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
    }


def _validate_core_worker_apk(apk_path: str) -> dict[str, object]:
    """Valida o APK antes de publicar latest.json.

    A identidade vem sempre do AndroidManifest.xml contido no binário. Metadados
    enviados pelo worker nunca podem renomear um APK antigo como versão nova.
    Assinatura e alinhamento continuam sendo verificados quando as ferramentas
    existem, sem transformar a VPS em ambiente de build.
    """
    result: dict[str, object] = {"ok": False, "checks": []}
    path = str(apk_path or "")
    if not path or not os.path.isfile(path):
        result["error"] = "APK não encontrado"
        return result
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad:
                result["error"] = f"ZIP corrompido em {bad}"
                return result
            names = set(zf.namelist())
            missing = [name for name in ("AndroidManifest.xml", "classes.dex") if name not in names]
            if missing:
                result["error"] = "APK sem " + ", ".join(missing)
                return result
            result["checks"].append("zip")
            result["checks"].append("manifest")
            result["entries"] = len(names)
    except Exception as exc:
        result["error"] = f"ZIP inválido: {type(exc).__name__}: {exc}"
        return result

    try:
        identity = inspect_apk_identity(path)
        assert_expected_apk_identity(identity, expected_package="dev.core.worker")
        result["identity"] = identity
        result["checks"].append("compiled-identity")
    except ApkIdentityError as exc:
        result["error"] = f"identidade compilada inválida: {exc}"
        return result

    zipalign = _find_android_build_tool("zipalign")
    if zipalign:
        proc = subprocess.run([zipalign, "-c", "-p", "4", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["zipalign"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "zipalign -c falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:]
            return result
        result["checks"].append("zipalign")

    apksigner = _find_android_build_tool("apksigner")
    if apksigner:
        proc = subprocess.run([apksigner, "verify", "--verbose", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["apksigner"] = proc.returncode == 0
        result["apksigner_tail"] = (proc.stdout or proc.stderr or "")[-800:]
        if proc.returncode != 0:
            result["error"] = "apksigner verify falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("apksigner")

    aapt = _find_android_build_tool("aapt")
    if aapt:
        proc = subprocess.run([aapt, "dump", "badging", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["aapt"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "aapt dump badging falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("aapt")
        first_line = next((line for line in (proc.stdout or "").splitlines() if line.startswith("package:")), "")
        if first_line:
            result["badging"] = first_line[:500]

    result["ok"] = True
    return result


def _current_core_worker_release_identity(base: str) -> dict[str, object] | None:
    """Lê a versão real do APK atualmente apontado por latest.json, sem confiar nele."""
    manifest_path = os.path.join(base, "latest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        if not isinstance(manifest, dict):
            return None
        filename = str(manifest.get("filename") or "").strip()
        if not filename:
            apk_url = str(manifest.get("apkUrl") or manifest.get("downloadUrl") or "").split("?", 1)[0].rstrip("/")
            filename = apk_url.rsplit("/", 1)[-1]
        filename = _safe_release_filename(filename, default="")
        if not filename or not filename.lower().endswith(".apk"):
            return None
        path = os.path.abspath(os.path.join(base, filename))
        if path != base and not path.startswith(base + os.sep):
            return None
        identity = inspect_apk_identity(path)
        return assert_expected_apk_identity(identity, expected_package="dev.core.worker")
    except Exception:
        return None


def _sign_core_worker_apk_with_vps_key(uploaded_apk: str, final_apk: str) -> dict[str, object]:
    cfg = _fixed_apk_signing_config()
    if cfg is None:
        shutil.copyfile(uploaded_apk, final_apk)
        return {"signedByVps": False, "signingMode": "phone-worker-debug"}
    apksigner = _find_android_build_tool("apksigner")
    if not apksigner:
        raise RuntimeError("apksigner não encontrado na VPS; configure ANDROID_HOME ou CORE_WORKER_APK_APKSIGNER")
    zipalign = _find_android_build_tool("zipalign")
    keystore = str(cfg.get("keystore") or "")
    alias = str(cfg.get("alias") or "")
    storepass = str(cfg.get("storepass") or "")
    keypass = str(cfg.get("keypass") or "")
    if not keystore or not os.path.isfile(keystore):
        raise RuntimeError("keystore fixa ausente na VPS; configure CORE_WORKER_APK_KEYSTORE ou preserve ~/.android/debug.keystore")
    if not alias or not storepass:
        raise RuntimeError("configuração de assinatura incompleta na VPS")

    base_dir = os.path.dirname(final_apk) or os.getcwd()
    with tempfile.TemporaryDirectory(prefix="core-worker-sign-", dir=base_dir) as tmpdir:
        stripped = os.path.join(tmpdir, "stripped.apk")
        aligned = os.path.join(tmpdir, "aligned.apk")
        _strip_apk_signatures(uploaded_apk, stripped)
        sign_input = stripped
        if zipalign:
            proc = subprocess.run([zipalign, "-f", "-p", "4", stripped, aligned], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError("zipalign falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:])
            sign_input = aligned
        cmd = [
            apksigner,
            "sign",
            "--ks", keystore,
            "--ks-key-alias", alias,
            "--ks-pass", f"pass:{storepass}",
            "--key-pass", f"pass:{keypass}",
            "--out", final_apk,
            sign_input,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError("apksigner falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:])
        verify = subprocess.run([apksigner, "verify", "--verbose", final_apk], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if verify.returncode != 0:
            raise RuntimeError("verificação da assinatura falhou: " + (verify.stderr or verify.stdout or "sem saída")[-500:])
    return {
        "signedByVps": True,
        "signingMode": str(cfg.get("mode") or "keystore"),
        "zipalign": bool(zipalign),
    }

def set_health_provider(provider):
    global _health_provider
    _health_provider = provider


def set_update_action_provider(provider):
    global _update_action_provider
    _update_action_provider = provider


def _is_local_request() -> bool:
    remote = (request.remote_addr or "").strip()
    return remote in {"127.0.0.1", "::1", "localhost"}


def _purge_expired_tts_audio(now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _tts_audio_lock:
        expired = [token for token, (_path, expires_at) in _tts_audio_files.items() if expires_at <= now]
        for token in expired:
            _tts_audio_files.pop(token, None)


def register_tts_audio_file(path: str, *, ttl_seconds: float = 240.0) -> str | None:
    """Registra um áudio temporário para o Lavalink buscar via HTTP.

    O token é aleatório e expira rápido. O arquivo não é copiado para evitar RAM/IO
    extra; o endpoint apenas faz streaming do caminho já gerado pelo TTS. A URL
    pode usar a extensão real do arquivo (.ogg/.opus/.m4a/.mp3) ou apenas o token.
    """
    try:
        abs_path = os.path.abspath(str(path or ""))
        if not os.path.isfile(abs_path):
            return None
        _purge_expired_tts_audio()
        token = uuid.uuid4().hex
        ttl = max(30.0, min(900.0, float(ttl_seconds or 240.0)))
        with _tts_audio_lock:
            _tts_audio_files[token] = (abs_path, time.time() + ttl)
        return token
    except Exception:
        return None


@app.get("/")
def index():
    return "ok", 200


@app.get("/health")
def health():
    if callable(_health_provider):
        try:
            return jsonify(_health_provider()), 200
        except Exception as e:
            return jsonify({
                "ok": False,
                "healthy": False,
                "error": str(e),
            }), 500
    return jsonify({"ok": True}), 200


def _dispatch_internal_update_action(action: str):
    if not _is_local_request():
        abort(403)
    expected_token = os.getenv("BOT_INTERNAL_UPDATE_TOKEN", "").strip()
    if expected_token and request.headers.get("X-Update-Token", "") != expected_token:
        abort(403)
    if not callable(_update_action_provider):
        return jsonify({"ok": False, "error": "update action provider indisponível"}), 503
    payload = request.get_json(silent=True) or {}
    try:
        result = _update_action_provider(action, payload)
        status = 200 if isinstance(result, dict) and result.get("ok") else 500
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/internal/update/reload-cogs")
def internal_update_reload_cogs():
    return _dispatch_internal_update_action("reload_cogs")


@app.post("/internal/update/zip-status")
def internal_update_zip_status():
    return _dispatch_internal_update_action("zip_status")


@app.post("/internal/update/create-zip-status")
def internal_update_create_zip_status():
    return _dispatch_internal_update_action("create_zip_status")



@app.post("/core-worker/app/publish")
def core_worker_app_publish():
    """Publica somente APK já compilado e com identidade binária comprovada.

    A VPS pode validar e, quando configurado, reassinar o artefato recebido. Ela
    nunca executa Gradle nem aceita versionName/versionCode externos como fonte
    de verdade para latest.json.
    """
    from utility.commands.workers_registry import core_worker_authenticate_http

    form = request.form.to_dict(flat=True)
    auth_payload = {
        "worker_id": form.get("worker_id") or request.headers.get("X-Core-Worker-ID") or "",
        "name": form.get("workerName") or form.get("worker_name") or "Core Phone Worker",
        "version": form.get("requiredAgentVersion") or request.headers.get("X-Core-Worker-Version") or "",
        "source": "apk-publish",
        "roles": ["phone-worker", "apk-builder"],
        "capabilities": ["phone-worker", "apk-builder"],
        "supported_tasks": ["apk_build_debug", "apk_publish_last", "worker_update", "worker_logs", "diagnostic_basic", "network_probe"],
    }
    status, auth_body = core_worker_authenticate_http(request.headers, auth_payload, remote_addr=request.remote_addr or "")
    if status != 200:
        return jsonify(auth_body), status
    worker = auth_body.get("worker") if isinstance(auth_body.get("worker"), dict) else {}
    roles = set(str(item) for item in (worker.get("roles") or []))
    capabilities = set(str(item) for item in (worker.get("capabilities") or [])) | roles
    if "apk-builder" not in capabilities:
        return jsonify({"ok": False, "error": "worker não tem função apk-builder"}), 403

    upload = request.files.get("apk")
    if upload is None:
        return jsonify({"ok": False, "error": "arquivo apk ausente"}), 400

    requested_version_name = str(form.get("versionName") or form.get("version") or "").strip()[:48]
    raw_version_code = str(form.get("versionCode") or "").strip()
    try:
        requested_version_code = int(raw_version_code or 0)
    except Exception:
        return jsonify({"ok": False, "error": "versionCode solicitado é inválido"}), 400
    if requested_version_code < 0:
        return jsonify({"ok": False, "error": "versionCode solicitado é inválido"}), 400

    requested_source_fingerprint = str(
        form.get("sourceFingerprint")
        or form.get("source_fingerprint")
        or form.get("sourceSha256")
        or form.get("source_sha256")
        or ""
    ).strip().lower()
    with _core_worker_desired_source_lock():
        desired_source = _load_core_worker_desired_source()
        desired_error = _desired_source_publish_error(
            desired_source,
            worker=worker,
            requested_version_name=requested_version_name,
            requested_version_code=requested_version_code,
            requested_source_fingerprint=requested_source_fingerprint,
        )
    if desired_error:
        status_code = 503 if desired_source.get("_invalid") else 409
        return jsonify({
            "ok": False,
            "error": desired_error,
            "preservedPreviousRelease": True,
            "desiredSourceFingerprint": str(desired_source.get("sourceFingerprint") or "")[:64],
        }), status_code

    requested_filename = _safe_release_filename(
        form.get("filename") or upload.filename or "CoreWorker.apk",
        default="CoreWorker.apk",
    )
    if not requested_filename.lower().endswith(".apk"):
        return jsonify({"ok": False, "error": "arquivo precisa terminar com .apk"}), 400

    base = _core_worker_apk_dir()
    os.makedirs(base, exist_ok=True)
    expected_sha = str(form.get("sha256") or "").strip().lower()
    max_bytes = int(os.getenv("CORE_WORKER_APK_UPLOAD_MAX_BYTES", str(1024 * 1024 * 1024)))
    upload_total = 0
    upload_sha = ""
    uploaded_stage = ""
    signed_stage = ""
    manifest_stage = ""
    signing_info: dict[str, object] = {"signedByVps": False, "signingMode": "not-run"}
    release_cleanup: dict[str, object] = {"ok": True, "removedCount": 0, "reclaimedBytes": 0}

    try:
        fd, uploaded_stage = tempfile.mkstemp(prefix=".core-worker-upload-", suffix=".apk", dir=base)
        os.close(fd)
        digest = hashlib.sha256()
        with open(uploaded_stage, "wb") as fh:
            while True:
                chunk = upload.stream.read(128 * 1024)
                if not chunk:
                    break
                upload_total += len(chunk)
                if upload_total > max_bytes:
                    raise ValueError("APK grande demais")
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        upload_sha = digest.hexdigest()
        if expected_sha and expected_sha != upload_sha:
            return jsonify({"ok": False, "error": "sha256 divergente", "expected": expected_sha, "actual": upload_sha}), 400

        fd, signed_stage = tempfile.mkstemp(prefix=".core-worker-ready-", suffix=".apk", dir=base)
        os.close(fd)
        os.remove(signed_stage)
        try:
            signing_info = _sign_core_worker_apk_with_vps_key(uploaded_stage, signed_stage)
        except Exception as sign_exc:
            return jsonify({
                "ok": False,
                "error": "falha preparando a assinatura do APK recebido",
                "detail": str(sign_exc)[:500],
                "hint": "a VPS não builda o APK; desative a reassinatura para aceitar o APK já assinado pelo builder no celular",
            }), 500

        validation = _validate_core_worker_apk(signed_stage)
        if not bool(validation.get("ok")):
            return jsonify({
                "ok": False,
                "error": "APK recebido falhou na validação antes da publicação",
                "validation": validation,
                "hint": "latest.json e o APK publicado foram preservados; corrija o build no celular/Termux.",
            }), 422

        identity = validation.get("identity") if isinstance(validation.get("identity"), dict) else {}
        try:
            assert_expected_apk_identity(
                identity,
                expected_package="dev.core.worker",
                expected_version_name=requested_version_name,
                expected_version_code=requested_version_code,
            )
        except ApkIdentityError as exc:
            return jsonify({
                "ok": False,
                "error": "metadados de publicação divergem do APK compilado",
                "detail": str(exc),
                "requested": {
                    "versionName": requested_version_name,
                    "versionCode": requested_version_code,
                },
                "compiled": identity,
                "preservedPreviousRelease": True,
            }), 409

        version_name = str(identity.get("versionName") or "").strip()
        version_code = int(identity.get("versionCode") or 0)
        filename = requested_filename
        if version_name not in filename:
            filename = _safe_release_filename(f"CoreWorker-v{version_name}-debug.apk")
        target = os.path.abspath(os.path.join(base, filename))
        if target != base and not target.startswith(base + os.sep):
            return jsonify({"ok": False, "error": "nome de arquivo inválido"}), 400

        final_digest = hashlib.sha256()
        total = 0
        with open(signed_stage, "rb") as final_fh:
            while True:
                final_chunk = final_fh.read(1024 * 1024)
                if not final_chunk:
                    break
                final_digest.update(final_chunk)
                total += len(final_chunk)
        actual_sha = final_digest.hexdigest()

        changelog = _json_field(form.get("changelog") or "", ["APK compilado por worker builder"])
        if not isinstance(changelog, list):
            changelog = [str(changelog)[:160]]
        required_agent = str(form.get("requiredAgentVersion") or "").strip()[:48]
        source_sha = requested_source_fingerprint[:96]
        notification_id = _safe_short_text(
            form.get("notificationId") or _notification_event_id(version_name=version_name, version_code=version_code, sha256=actual_sha),
            96,
        )
        notification_summary = _latest_core_worker_notification_summary(notification_id)
        apk_url = _core_worker_apk_url(filename)
        manifest = {
            "ok": True,
            "filename": filename,
            "versionName": version_name,
            "versionCode": version_code,
            "packageName": str(identity.get("packageName") or ""),
            "apkUrl": apk_url,
            "downloadUrl": _external_core_worker_url(apk_url),
            "directApkUrl": _external_core_worker_url(apk_url),
            "sha256": actual_sha,
            "uploadedSha256": upload_sha,
            "requiredAgentVersion": required_agent,
            "updateAvailable": True,
            "notifyUsers": True,
            "notificationRequested": True,
            "notificationId": notification_id,
            "notificationStatus": notification_summary,
            "sourceSha256": source_sha,
            "signedByVps": bool(signing_info.get("signedByVps")),
            "signingMode": _safe_short_text(form.get("apkSigningMode") or signing_info.get("signingMode") or "phone-worker-signed", 96),
            "signingKeystoreSha256": _safe_short_text(form.get("apkSigningKeystoreSha256") or "", 64),
            "changelog": [str(item)[:180] for item in changelog[:8]],
            "publishedByWorker": str(worker.get("name") or worker.get("worker_id") or "worker builder")[:80],
            "publishedAt": int(time.time()),
            "publishReason": "worker-builder-auto" if str(form.get("notifyUsers") or form.get("notificationRequested") or "").strip().lower() in {"1", "true", "yes", "on", "sim"} else "worker-builder",
            "bytes": total,
            "uploadedBytes": upload_total,
            "validation": validation,
        }

        manifest_path = os.path.join(base, "latest.json")
        with _core_worker_apk_publish_lock:
            # A comparação final ocorre sob o mesmo lock de arquivo usado pelo
            # publicador de desired-source. Assim um novo target não pode surgir
            # entre a última comparação e o os.replace(latest.json).
            with _core_worker_desired_source_lock():
                desired_source = _load_core_worker_desired_source()
                desired_error = _desired_source_publish_error(
                    desired_source,
                    worker=worker,
                    requested_version_name=version_name,
                    requested_version_code=version_code,
                    requested_source_fingerprint=requested_source_fingerprint,
                )
                if desired_error:
                    status_code = 503 if desired_source.get("_invalid") else 409
                    return jsonify({
                        "ok": False,
                        "error": desired_error,
                        "preservedPreviousRelease": True,
                        "desiredSourceFingerprint": str(desired_source.get("sourceFingerprint") or "")[:64],
                    }), status_code

                current_identity = _current_core_worker_release_identity(base)
                if current_identity:
                    current_code = int(current_identity.get("versionCode") or 0)
                    current_name = str(current_identity.get("versionName") or "")
                    if version_code < current_code or (version_code == current_code and current_name and version_name != current_name):
                        return jsonify({
                            "ok": False,
                            "error": "publicação recusada para impedir downgrade do APK",
                            "compiled": identity,
                            "current": current_identity,
                            "preservedPreviousRelease": True,
                        }), 409

                fd, manifest_stage = tempfile.mkstemp(prefix=".core-worker-latest-", suffix=".json", dir=base)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())

                backup_target = ""
                try:
                    if os.path.isfile(target):
                        backup_target = target + ".rollback-" + uuid.uuid4().hex
                        try:
                            os.link(target, backup_target)
                        except Exception:
                            shutil.copy2(target, backup_target)
                    os.replace(signed_stage, target)
                    signed_stage = ""
                    os.replace(manifest_stage, manifest_path)
                    manifest_stage = ""
                except Exception:
                    with contextlib.suppress(Exception):
                        os.remove(target)
                    if backup_target and os.path.isfile(backup_target):
                        os.replace(backup_target, target)
                        backup_target = ""
                    raise
                finally:
                    if backup_target:
                        with contextlib.suppress(Exception):
                            os.remove(backup_target)

                # A publicação já é íntegra neste ponto. A poda roda sob o mesmo
                # lock, preserva sempre o APK atual e nunca transforma uma falha de
                # housekeeping em falha da atualização entregue ao dispositivo.
                try:
                    release_cleanup = prune_core_worker_releases(
                        base,
                        filename,
                        keep=_env_int("CORE_WORKER_APK_RETAIN_RELEASES", 3, minimum=1, maximum=10),
                        max_total_bytes=_env_int(
                            "CORE_WORKER_APK_RELEASE_MAX_BYTES",
                            256 * 1024 * 1024,
                            minimum=64 * 1024 * 1024,
                            maximum=2 * 1024 * 1024 * 1024,
                        ),
                    )
                except Exception as cleanup_exc:
                    release_cleanup = {
                        "ok": False,
                        "removedCount": 0,
                        "reclaimedBytes": 0,
                        "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"[:240],
                    }
                    app.logger.warning("poda de releases do Core Worker ignorada: %s", cleanup_exc)

        _kick_core_worker_fcm_push(manifest, reason="apk_published")
        _kick_core_worker_pending_automation(str(worker.get("worker_id") or ""))
        return jsonify({
            "ok": True,
            "filename": filename,
            "bytes": total,
            "sha256": actual_sha,
            "signedByVps": bool(signing_info.get("signedByVps")),
            "signingMode": manifest.get("signingMode"),
            "validation": validation,
            "storageCleanup": release_cleanup,
            "latest": manifest,
        }), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:240]}), 400
    except Exception as exc:
        app.logger.exception("falha publicando Core Worker APK")
        return jsonify({
            "ok": False,
            "error": f"falha publicando APK: {type(exc).__name__}",
            "detail": str(exc)[:300],
            "preservedPreviousRelease": True,
        }), 500
    finally:
        for path in (uploaded_stage, signed_stage, manifest_stage):
            if path:
                with contextlib.suppress(Exception):
                    os.remove(path)



@app.post("/core-worker/app/fcm-token")
def core_worker_app_fcm_token():
    """Registra token FCM do APK privado.

    O token é segredo operacional do dispositivo e fica só em data/ local com
    permissão 600. O painel mostra apenas resumo como Push: ativo.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _register_core_worker_fcm_token(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker FCM token ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "push": record}), 200


@app.get("/core-worker/app/fcm-summary")
def core_worker_app_fcm_summary():
    worker_id = str(request.args.get("worker_id") or "").strip()
    return jsonify({"ok": True, "summary": _core_worker_fcm_public_summary(worker_id)}), 200


@app.post("/core-worker/app/heartbeat")
def core_worker_app_heartbeat():
    """Recebe heartbeat direto do APK, sem passar pelo Termux.

    Este endpoint é apenas telemetria de migração: não autentica jobs e não
    concede capacidade de execução. Jobs reais continuam no phone-worker/Termux.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _append_core_worker_app_heartbeat(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker app heartbeat ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "runtime": record}), 200


@app.get("/core-worker/app/runtime-summary")
def core_worker_app_runtime_summary():
    worker_id = str(request.args.get("worker_id") or "").strip()
    install_id = str(request.args.get("install_id") or "").strip()
    return jsonify({"ok": True, "summary": _core_worker_app_runtime_public_summary(worker_id, install_id)}), 200


@app.post("/core-worker/app/jobs/fetch")
def core_worker_app_jobs_fetch():
    """Entrega jobs leves para o runtime interno do APK.

    Apenas jobs sem shell/comando são aceitos pelo APK nesta etapa. Jobs reais
    continuam no Termux. Este endpoint é best-effort e não concede execução arbitrária.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        return jsonify(_core_worker_app_jobs_fetch(payload)), 200
    except Exception as exc:
        app.logger.warning("core-worker app light jobs fetch ignored: %s", exc)
        return jsonify({"ok": False, "jobs": [], "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200


@app.post("/core-worker/app/jobs/enqueue")
def core_worker_app_jobs_enqueue():
    """Enfileira jobs manuais seguros para o runtime interno do APK.

    V12.1 expõe apenas o smoke allowlist de PRoot/BusyBox e restringe a chamada
    ao localhost da VPS para evitar que clientes externos empurrem jobs manuais.
    """
    remote = str(request.remote_addr or "")
    if remote not in {"127.0.0.1", "::1", "localhost"}:
        return jsonify({"ok": False, "error": "endpoint local-only"}), 403
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    job_type = _core_worker_app_normalize_job_type(payload.get("type") or payload.get("jobType") or payload.get("kind"))
    if job_type not in CORE_WORKER_APP_LOCAL_MANUAL_QUEUE_TYPES:
        return jsonify({"ok": False, "error": "job manual não permitido neste endpoint", "allowed": sorted(CORE_WORKER_APP_LOCAL_MANUAL_QUEUE_TYPES)}), 400
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    if not worker_id and not install_id:
        return jsonify({"ok": False, "error": "workerId ou installId obrigatório"}), 400
    default_reason = "manual-v12-base-tools-smoke"
    if job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB:
        default_reason = "manual-v13-3-rootfs-android-runtime-bind-smoke"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB:
        default_reason = "manual-v14-2-1-box64-asset-preflight"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB:
        default_reason = "manual-v15-3-1-box64-glibc-hard-guard"
    elif job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB:
        default_reason = "manual-v16-2-rootfs-glibc-telemetry-dedupe"
    elif job_type in CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES:
        default_reason = "manual-v17-rootfs-import-status"
    reason = _safe_short_text(payload.get("reason") or default_reason, 80)
    if job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_SMOKE_V13_JOB:
        result = _core_worker_app_queue_core_linux_rootfs_smoke_v13(
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_V14_JOB:
        result = _core_worker_app_queue_core_linux_box64_v14(
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    elif job_type == CORE_WORKER_APP_CORE_LINUX_BOX64_SMOKE_V15_JOB:
        result = _core_worker_app_queue_core_linux_box64_smoke_v15(
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    elif job_type == CORE_WORKER_APP_CORE_LINUX_ROOTFS_GLIBC_V16_JOB:
        result = _core_worker_app_queue_core_linux_rootfs_glibc_v16(
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    elif job_type in CORE_WORKER_APP_CORE_LINUX_ROOTFS_IMPORT_V17_TYPES:
        result = _core_worker_app_queue_core_linux_rootfs_import_v17(
            job_type,
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    else:
        result = _core_worker_app_queue_core_linux_smoke_v12(
            worker_id=worker_id,
            install_id=install_id,
            reason=reason,
        )
    return jsonify(result), 200


@app.post("/core-worker/app/jobs/result")
def core_worker_app_jobs_result():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _core_worker_app_jobs_result(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker app light job result ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "result": record}), 200


@app.post("/core-worker/app/notification")
def core_worker_app_notification():
    """Recebe confirmação best-effort do APK privado sobre notificação local.

    O app ainda é privado e fica atrás da rede da VPS/Tailscale. Este endpoint
    armazena apenas telemetria curta; tokens FCM entram em /fcm-token.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    state = _safe_short_text(payload.get("state") or payload.get("event"), 48)
    if not state:
        return jsonify({"ok": False, "error": "state ausente"}), 400
    try:
        event = _append_core_worker_notification_event(payload)
    except Exception as exc:
        app.logger.warning("core-worker app notification ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "event": event}), 200


@app.get("/core-worker/app/latest.json")
@app.get("/core-worker/latest.json")
@app.get("/core-worker/latest")
def core_worker_app_latest():
    """Manifesto privado de atualização do Core Worker APK.

    Publique um arquivo latest.json em CORE_WORKER_APK_DIR contendo versionCode,
    versionName, apkUrl e sha256. Este endpoint não usa segredos e deve ser usado
    preferencialmente apenas pela rede privada/Tailscale.
    """
    base = _core_worker_apk_dir()
    manifest = os.path.join(base, "latest.json")
    if not os.path.isfile(manifest):
        return jsonify({
            "ok": False,
            "error": "Core Worker APK ainda não publicado na VPS.",
            "expected": manifest,
            "hint": "Crie latest.json e coloque o APK no diretório de releases.",
        }), 404
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            identity = _current_core_worker_release_identity(base)
            if not identity:
                return jsonify({
                    "ok": False,
                    "error": "latest.json aponta para um APK ausente ou com identidade inválida",
                    "publicationBlocked": True,
                }), 503
            declared_name = str(data.get("versionName") or "").strip()
            try:
                declared_code = int(data.get("versionCode") or 0)
            except Exception:
                declared_code = 0
            filename = str(data.get("filename") or "").strip()
            if not filename:
                raw_url = str(data.get("apkUrl") or data.get("url") or "").split("?", 1)[0].rstrip("/")
                filename = raw_url.rsplit("/", 1)[-1]
            apk_path = _safe_core_worker_apk_file(filename)
            if not apk_path or not os.path.isfile(apk_path) or not apk_path.lower().endswith(".apk"):
                return jsonify({"ok": False, "error": "APK de latest.json não encontrado", "publicationBlocked": True}), 503
            digest = hashlib.sha256()
            with open(apk_path, "rb") as apk_fh:
                while True:
                    block = apk_fh.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            actual_sha = digest.hexdigest()
            declared_sha = str(data.get("sha256") or "").strip().lower()
            metadata_mismatch = (
                declared_name != str(identity.get("versionName") or "")
                or declared_code != int(identity.get("versionCode") or 0)
                or (bool(declared_sha) and declared_sha != actual_sha)
            )
            data["filename"] = os.path.basename(apk_path)
            data["packageName"] = str(identity.get("packageName") or "")
            data["versionName"] = str(identity.get("versionName") or "")
            data["versionCode"] = int(identity.get("versionCode") or 0)
            data["sha256"] = actual_sha
            data["compiledIdentityVerified"] = True
            if metadata_mismatch:
                data["metadataMismatchDetected"] = True
                data["declaredVersionName"] = declared_name
                data["declaredVersionCode"] = declared_code
            apk_url = str(data.get("apkUrl") or data.get("url") or "").strip()
            if apk_url:
                data["downloadUrl"] = _external_core_worker_url(apk_url)
                data["directApkUrl"] = data["downloadUrl"]
            nid = str(data.get("notificationId") or "")
            if nid:
                data["notificationStatus"] = _latest_core_worker_notification_summary(nid)
            data["pushStatus"] = _core_worker_fcm_public_summary()
            return jsonify(data), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": "latest.json inválido; publicação bloqueada",
            "detail": _safe_short_text(f"{type(exc).__name__}: {exc}", 180),
        }), 503
    return jsonify({"ok": False, "error": "latest.json não contém um objeto válido"}), 503


@app.get("/core-worker/app/<path:filename>")
def core_worker_app_file(filename: str):
    """Serve APKs privados do Core Worker a partir do diretório de releases."""
    full = _safe_core_worker_apk_file(filename)
    if not full or not os.path.isfile(full):
        abort(404)
    lowered = full.lower()
    if lowered.endswith(".apk"):
        return send_file(full, mimetype="application/vnd.android.package-archive", as_attachment=True, download_name=os.path.basename(full), conditional=True, max_age=0)
    if lowered.endswith(".json"):
        return send_file(full, mimetype="application/json", conditional=True, max_age=0)
    if lowered.endswith(".zip"):
        return send_file(full, mimetype="application/zip", conditional=True, max_age=0)
    return send_file(full, mimetype="text/plain", conditional=True, max_age=0)




def _env_bool_web(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_float_web(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except Exception:
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _automation_worker_key(worker_id: str) -> str:
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return "__all__"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", worker_id)[:120] or "__unknown__"


def _is_process_running(process: subprocess.Popen | None) -> bool:
    if process is None:
        return False
    try:
        return process.poll() is None
    except Exception:
        return False


def _reap_pending_automation_process(key: str, process: subprocess.Popen, *, max_runtime: float) -> None:
    """Waits for a spawned process in a daemon thread so it cannot remain defunct.

    Popen objects only release their child process after wait()/poll(). On the 1 GB
    VPS, a few defunct automation children plus repeated health/heartbeat requests
    were enough to make the bot appear frozen. This watcher is intentionally tiny
    and never touches Discord state.
    """
    try:
        try:
            process.wait(timeout=max(5.0, float(max_runtime)))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=5.0)
    finally:
        with _core_worker_pending_automation_lock:
            if _core_worker_pending_automation_processes.get(key) is process:
                _core_worker_pending_automation_processes.pop(key, None)


def _log_pending_automation_skip(key: str, message: str) -> None:
    now = time.time()
    last = float(_core_worker_pending_automation_last_log.get(key) or 0.0)
    if now - last < 300.0:
        return
    _core_worker_pending_automation_last_log[key] = now
    with contextlib.suppress(Exception):
        app.logger.info("core-worker automation skipped | worker=%s reason=%s", key, message)


def _core_worker_automation_status_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_automation_status.json")


def _core_worker_automation_pending_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_automation_pending.json")


def _core_worker_automation_has_explicit_pending() -> bool:
    try:
        data = _load_json_cached(_core_worker_automation_pending_path(), {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return False
    for key in ("agent_update", "apk_build"):
        item = data.get(key)
        if isinstance(item, dict) and (item.get("pending") or item.get("queued") or item.get("job")):
            return True
    return False


def _core_worker_automation_transition_is_urgent() -> bool:
    """Transições agent -> APK precisam reagir no próximo poll, não em 15 min."""
    try:
        data = _load_json_cached(_core_worker_automation_pending_path(), {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return False
    agent = data.get("agent_update") if isinstance(data.get("agent_update"), dict) else {}
    apk = data.get("apk_build") if isinstance(data.get("apk_build"), dict) else {}
    apk_phase = str(apk.get("phase") or "")
    return bool(
        str(agent.get("phase") or "") in {"job_queued", "restart_pending"}
        or apk_phase == "waiting_worker_update"
        or (apk.get("retry_after_agent_update") and apk_phase not in {"waiting_builder", "enqueue_failed", "queued"})
    )


def _core_worker_automation_recently_finished(cooldown: float) -> bool:
    try:
        data = _load_json_cached(_core_worker_automation_status_path(), {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return False
    finished = float(data.get("finished_at") or 0.0)
    return bool(finished and time.time() - finished < max(60.0, float(cooldown)))


def _kick_core_worker_pending_automation(worker_id: str = "") -> None:
    """Agenda processamento leve das pendências de agent/APK após heartbeat.

    O endpoint de heartbeat/poll/result pode ser chamado várias vezes por minuto.
    Sem proteção, cada chamada abre um process-pending novo e a VPS pequena fica
    presa em subprocessos concorrentes. Esta função é fire-and-forget, mas agora
    tem lock/cooldown por worker e mata execuções claramente antigas antes de
    abrir outra. O script também possui lock próprio como segunda linha de defesa.
    """
    if not _env_bool_web("CORE_WORKER_PENDING_AUTOMATION_ENABLED", True):
        return
    worker_id = str(worker_id or "").strip()
    key = _automation_worker_key(worker_id)
    now = time.time()
    explicit_pending = _core_worker_automation_has_explicit_pending()
    idle_cooldown = _env_float_web("CORE_WORKER_PENDING_AUTOMATION_COOLDOWN_SECONDS", 900.0, minimum=300.0, maximum=3600.0)
    if explicit_pending and _core_worker_automation_transition_is_urgent():
        cooldown = _env_float_web("CORE_WORKER_PENDING_AUTOMATION_TRANSITION_COOLDOWN_SECONDS", 8.0, minimum=2.0, maximum=60.0)
    elif explicit_pending:
        cooldown = _env_float_web("CORE_WORKER_PENDING_AUTOMATION_ACTIVE_COOLDOWN_SECONDS", 60.0, minimum=10.0, maximum=300.0)
    else:
        cooldown = idle_cooldown
    max_runtime = _env_float_web("CORE_WORKER_PENDING_AUTOMATION_MAX_RUNTIME_SECONDS", 45.0, minimum=10.0, maximum=180.0)

    script = os.path.join(os.getcwd(), "scripts", "core-worker-automation.py")
    if not os.path.isfile(script):
        return

    # Evita spawn pesado em todo restart/heartbeat quando não há pendência real.
    # Se houver agent_update/apk_build pendente, o processo ainda roda; se o último
    # scan acabou agora e não há pendência explícita, o próximo scan fica para o
    # cooldown persistido em disco.
    if not explicit_pending and _core_worker_automation_recently_finished(idle_cooldown):
        _log_pending_automation_skip(key, f"recent_scan_no_pending cooldown<{cooldown:.0f}s")
        return

    with _core_worker_pending_automation_lock:
        process = _core_worker_pending_automation_processes.get(key)
        last_started = float(_core_worker_pending_automation_last_started.get(key) or 0.0)
        if _is_process_running(process):
            age = now - last_started
            if age < max_runtime:
                _log_pending_automation_skip(key, f"already_running age={age:.1f}s")
                return
            _log_pending_automation_skip(key, f"stale_process_kill age={age:.1f}s")
            with contextlib.suppress(Exception):
                process.terminate()
            time.sleep(0.05)
            if _is_process_running(process):
                with contextlib.suppress(Exception):
                    process.kill()
            _core_worker_pending_automation_processes.pop(key, None)

        if now - last_started < cooldown:
            _log_pending_automation_skip(key, f"cooldown {now - last_started:.1f}s<{cooldown:.1f}s")
            return

        py = os.path.join(os.getcwd(), ".venv", "bin", "python")
        if not os.path.isfile(py):
            py = shutil.which("python3") or "python3"
        cmd = [py, script, "process-pending"]
        if worker_id:
            cmd.extend(["--worker-id", worker_id])
        try:
            process = subprocess.Popen(cmd, cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:
            with contextlib.suppress(Exception):
                app.logger.warning("core-worker automation spawn failed | worker=%s error=%s", key, exc)
            return
        _core_worker_pending_automation_processes[key] = process
        _core_worker_pending_automation_last_started[key] = now
        try:
            threading.Thread(
                target=_reap_pending_automation_process,
                args=(key, process),
                kwargs={"max_runtime": max_runtime},
                name=f"core-worker-auto-reap-{key[:24]}",
                daemon=True,
            ).start()
        except Exception:
            # Próxima chamada ainda fará poll/cleanup; não vale quebrar request HTTP.
            pass
        with contextlib.suppress(Exception):
            app.logger.info("core-worker automation started | worker=%s pid=%s", key, getattr(process, "pid", ""))


def _core_worker_apk_http_token() -> str:
    """Token privado usado apenas pela API direta do APK na porta 8766."""
    return str(
        os.getenv("CORE_WORKER_APK_HTTP_TOKEN")
        or os.getenv("PHONE_WORKER_TOKEN")
        or ""
    ).strip()


def _core_worker_is_apk_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    source = str(payload.get("source") or "").strip().lower()
    platform = str(payload.get("platform") or "").strip().lower()
    return source.startswith("core-worker-apk") or platform == "android"


def _attach_core_worker_apk_http_token(status: int, body: object, payload: object) -> None:
    # Só devolve o segredo em uma resposta já autorizada de pareamento/heartbeat do APK.
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        return
    if not _core_worker_is_apk_payload(payload):
        return
    token = _core_worker_apk_http_token()
    if token:
        body["direct_http_token"] = token


@app.post("/core-worker/enroll/apk-child")
def core_worker_enroll_apk_child():
    from utility.commands.workers_registry import enroll_core_worker_apk_child_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    parent_id = str(payload.get("parent_worker_id") or payload.get("worker_id") or "").strip()
    desired = _load_core_worker_desired_source()
    if desired and not desired.get("_invalid"):
        expected_parent = str(desired.get("selectedBuilderWorkerId") or "").strip()
        allowed_builders = {parent_id, (parent_id + "-apk") if parent_id else ""}
        if expected_parent and expected_parent not in allowed_builders:
            return jsonify({"ok": False, "error": "parent não corresponde ao builder selecionado do APK"}), 409
        expected_fp = str(desired.get("sourceFingerprint") or "").strip().lower()
        actual_fp = str(payload.get("sourceFingerprint") or payload.get("source_fingerprint") or "").strip().lower()
        if expected_fp and actual_fp != expected_fp:
            return jsonify({"ok": False, "error": "APK pertence a uma source diferente do target atual"}), 409
        expected_code = int(desired.get("versionCode") or 0)
        try:
            actual_code = int(payload.get("versionCode") or payload.get("version_code") or 0)
        except Exception:
            actual_code = 0
        if expected_code and actual_code != expected_code:
            return jsonify({"ok": False, "error": "versionCode do APK não corresponde ao target atual"}), 409
    status, body = enroll_core_worker_apk_child_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/pair")
def core_worker_pair():
    from utility.commands.workers_registry import redeem_core_worker_pairing_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = redeem_core_worker_pairing_http(payload, remote_addr=request.remote_addr or "")
    _attach_core_worker_apk_http_token(status, body, payload)
    return jsonify(body), status


@app.post("/core-worker/heartbeat")
def core_worker_heartbeat():
    from utility.commands.workers_registry import core_worker_heartbeat_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_heartbeat_http(request.headers, payload, remote_addr=request.remote_addr or "")
    _attach_core_worker_apk_http_token(status, body, payload)
    if status == 200 and isinstance(body, dict):
        worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
        worker_id = str(worker.get("worker_id") or payload.get("worker_id") or payload.get("id") or "")
        _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status






def _core_worker_agent_root() -> Path:
    configured = str(os.getenv("CORE_WORKER_AGENT_RELEASE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(os.getcwd()) / "data" / "core_worker_agent").resolve()


def _core_worker_request_bearer() -> str:
    auth = str(request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


def _authenticate_core_worker_agent_request(worker_id: str) -> tuple[bool, tuple | None, str]:
    from utility.commands.workers_registry import core_worker_authenticate_http

    token = _core_worker_request_bearer()
    if not token:
        return False, (jsonify({"ok": False, "error": "token ausente"}), 401), ""
    payload = {"worker_id": str(worker_id or "").strip()}
    status, body = core_worker_authenticate_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        return False, (jsonify(body if isinstance(body, dict) else {"ok": False, "error": "não autorizado"}), status), ""
    worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
    runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
    source = str(worker.get("source") or "").strip().lower()
    if runtime_kind == "apk" or source.startswith("core-worker-apk"):
        return False, (jsonify({"ok": False, "error": "manifesto do agent é exclusivo do runtime Termux"}), 403), ""
    return True, None, token


def _core_worker_response_hmac(token: str, timestamp: str, body: bytes) -> str:
    # O timestamp faz parte da mensagem autenticada. Sem isso, um adversário que
    # capturasse body+HMAC poderia trocar apenas o header de tempo e prolongar um
    # replay. O artefato pode ser imutável; a resposta autenticada continua curta.
    mac = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("ascii"))
    mac.update(b"\n")
    mac.update(body)
    return mac.hexdigest()


def _signed_agent_response_headers(body: bytes, token: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = _core_worker_response_hmac(token, timestamp, body)
    return {
        "X-Core-Worker-Timestamp": timestamp,
        "X-Core-Worker-Signature": f"sha256={digest}",
        "X-Core-Worker-Signature-Schema": "ts-body-v1",
        "X-Content-Type-Options": "nosniff",
    }


def _safe_agent_release_hash(value: str) -> str:
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


@app.get("/core-worker/agent/latest")
def core_worker_agent_latest():
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token = _authenticate_core_worker_agent_request(worker_id)
    if not ok:
        return error_response
    path = _core_worker_agent_root() / "latest.json"
    try:
        body = path.read_bytes()
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict) or not _safe_agent_release_hash(parsed.get("source_hash")):
            raise ValueError("manifesto latest inválido")
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "nenhuma release do agent publicada"}), 404
    except Exception as exc:
        app.logger.error("core-worker agent latest inválido: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "manifesto do agent inválido"}), 500
    response = app.response_class(body, status=200, mimetype="application/json")
    response.headers["Cache-Control"] = "private, no-store"
    for key, value in _signed_agent_response_headers(body, token).items():
        response.headers[key] = value
    return response


@app.get("/core-worker/agent/releases/<source_hash>.zip")
def core_worker_agent_release(source_hash: str):
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token = _authenticate_core_worker_agent_request(worker_id)
    if not ok:
        return error_response
    safe_hash = _safe_agent_release_hash(source_hash)
    if not safe_hash:
        return jsonify({"ok": False, "error": "source hash inválido"}), 400
    root = _core_worker_agent_root()
    path = (root / "releases" / f"{safe_hash}.zip").resolve()
    expected_parent = (root / "releases").resolve()
    if path.parent != expected_parent or not path.is_file():
        return jsonify({"ok": False, "error": "release não encontrada"}), 404
    max_bytes = _env_int("CORE_WORKER_AGENT_RELEASE_MAX_BYTES", 16 * 1024 * 1024, minimum=1024, maximum=64 * 1024 * 1024)
    try:
        stat_info = path.stat()
        if stat_info.st_size <= 0 or stat_info.st_size > max_bytes:
            raise ValueError("release fora do limite")
        timestamp = str(int(time.time()))
        mac = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
        mac.update(timestamp.encode("ascii"))
        mac.update(b"\n")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                mac.update(chunk)
    except Exception as exc:
        app.logger.error("core-worker agent release inválida hash=%s error=%s", safe_hash[:12], type(exc).__name__)
        return jsonify({"ok": False, "error": "release do agent inválida"}), 500
    response = send_file(path, mimetype="application/zip", as_attachment=False, conditional=False, etag=safe_hash)
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    response.headers["X-Core-Worker-Timestamp"] = timestamp
    response.headers["X-Core-Worker-Signature"] = f"sha256={mac.hexdigest()}"
    response.headers["X-Core-Worker-Signature-Schema"] = "ts-body-v1"
    response.headers["X-Core-Worker-Source-Hash"] = safe_hash
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response



def _core_worker_toolchain_root() -> Path:
    configured = str(os.getenv("CORE_WORKER_TOOLCHAIN_RELEASE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(os.getcwd()) / "data" / "core_worker_toolchain").resolve()


def _authenticate_core_worker_artifact_request(worker_id: str) -> tuple[bool, tuple | None, str, dict]:
    from utility.commands.workers_registry import core_worker_authenticate_http

    token = _core_worker_request_bearer()
    if not token:
        return False, (jsonify({"ok": False, "error": "token ausente"}), 401), "", {}
    payload = {"worker_id": str(worker_id or "").strip()}
    status, body = core_worker_authenticate_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        return False, (jsonify(body if isinstance(body, dict) else {"ok": False, "error": "não autorizado"}), status), "", {}
    worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
    return True, None, token, worker


def _validate_external_toolchain_archive(path: Path) -> dict:
    max_entries = _env_int("CORE_WORKER_TOOLCHAIN_MAX_ENTRIES", 50000, minimum=10, maximum=100000)
    max_expanded = _env_int("CORE_WORKER_TOOLCHAIN_MAX_EXPANDED_BYTES", 4 * 1024 * 1024 * 1024, minimum=16 * 1024 * 1024, maximum=8 * 1024 * 1024 * 1024)
    entries = 0
    expanded = 0
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        for info in infos:
            name = str(info.filename or "").replace("\\", "/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("caminho inseguro no toolchain")
            entries += 1
            expanded += max(0, int(info.file_size or 0))
            if entries > max_entries or expanded > max_expanded:
                raise ValueError("toolchain excede limites de extração")
        try:
            raw_manifest = archive.read("manifest.json")
        except KeyError as exc:
            raise ValueError("manifest.json ausente no toolchain") from exc
    manifest = json.loads(raw_manifest.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "core-worker-android-builder-v2":
        raise ValueError("schema inválido do toolchain externo")
    if str(manifest.get("arch") or "").lower() not in {"aarch64", "arm64", "arm64-v8a"}:
        raise ValueError("arquitetura do toolchain incompatível")
    versions = manifest.get("versions") if isinstance(manifest.get("versions"), dict) else {}
    if int(versions.get("jdkMajor") or 0) != 17:
        raise ValueError("JDK do toolchain precisa ser major 17")
    if str(versions.get("gradle") or "") != "8.9":
        raise ValueError("Gradle do toolchain precisa ser 8.9")
    if int(versions.get("compileSdk") or 0) != 34 or str(versions.get("buildTools") or "") != "34.0.0":
        raise ValueError("SDK/build-tools do toolchain incompatíveis")
    validation = manifest.get("validation") if isinstance(manifest.get("validation"), dict) else {}
    required = set(validation.get("requiredSmokeChecks") or [])
    if required != {"java", "javac", "jar", "gradle", "aapt2"}:
        raise ValueError("toolchain não declara os cinco smokes obrigatórios")
    return {"manifest": manifest, "entries": entries, "expandedBytes": expanded}


@app.post("/core-worker/toolchain/publish")
def core_worker_toolchain_publish():
    worker_id = str(request.form.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, _token, worker = _authenticate_core_worker_artifact_request(worker_id)
    if not ok:
        return error_response
    runtime_kind = str(worker.get("runtime_kind") or "").lower()
    source = str(worker.get("source") or "").lower()
    if runtime_kind == "apk" or source.startswith("core-worker-apk"):
        return jsonify({"ok": False, "error": "publicação inicial do toolchain é reservada ao Termux"}), 403
    upload = request.files.get("toolchain")
    if upload is None:
        return jsonify({"ok": False, "error": "arquivo toolchain ausente"}), 400
    root = _core_worker_toolchain_root()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".toolchain-upload-", suffix=".zip", dir=str(root))
    os.close(fd)
    temp = Path(temp_name)
    max_bytes = _env_int("CORE_WORKER_TOOLCHAIN_MAX_BYTES", 1024 * 1024 * 1024, minimum=1024 * 1024, maximum=2 * 1024 * 1024 * 1024)
    try:
        upload.save(str(temp))
        size = temp.stat().st_size
        if size <= 0 or size > max_bytes:
            raise ValueError("toolchain fora do limite compactado")
        sha = hashlib.sha256()
        with temp.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha.update(chunk)
        fingerprint = sha.hexdigest()
        declared_sha = str(request.form.get("sha256") or "").strip().lower()
        if declared_sha and declared_sha != fingerprint:
            raise ValueError("sha256 do toolchain divergente")
        validated = _validate_external_toolchain_archive(temp)
        manifest = validated["manifest"]
        physical = str(manifest.get("physicalWorkerId") or request.form.get("physical_worker_id") or worker.get("physical_worker_id") or worker_id).strip()
        target = releases / f"{fingerprint}.zip"
        if target.exists():
            if target.stat().st_size != size:
                raise ValueError("artefato imutável existente diverge")
            temp.unlink(missing_ok=True)
        else:
            os.replace(temp, target)
        record = {
            "schema": "core-worker-toolchain-release-v2",
            "toolchainFingerprint": fingerprint,
            "sha256": fingerprint,
            "bytes": size,
            "expandedBytes": validated["expandedBytes"],
            "entries": validated["entries"],
            "physicalWorkerId": physical,
            "compatibility": manifest.get("compatibility") if isinstance(manifest.get("compatibility"), dict) else {"arch": manifest.get("arch")},
            "versions": manifest.get("versions"),
            "validation": manifest.get("validation"),
            "publishedAt": int(time.time()),
            "url": f"/core-worker/toolchain/releases/{fingerprint}.zip",
        }
        root.mkdir(parents=True, exist_ok=True)
        stage = root / f".latest.{os.getpid()}.tmp"
        stage.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(stage, root / "latest.json")
        (releases / f"{fingerprint}.json").write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return jsonify({"ok": True, **record})
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.error("core-worker toolchain publish falhou: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "falha interna publicando toolchain"}), 500
    finally:
        with contextlib.suppress(Exception):
            temp.unlink()


@app.get("/core-worker/toolchain/latest")
def core_worker_toolchain_latest():
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token, _worker = _authenticate_core_worker_artifact_request(worker_id)
    if not ok:
        return error_response
    path = _core_worker_toolchain_root() / "latest.json"
    if not path.is_file():
        return jsonify({"ok": False, "error": "nenhum toolchain publicado"}), 404
    try:
        body = path.read_bytes()
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(parsed.get("toolchainFingerprint") or "")):
            raise ValueError
    except Exception:
        return jsonify({"ok": False, "error": "manifesto do toolchain inválido"}), 500
    response = app.response_class(body, status=200, mimetype="application/json")
    response.headers["Cache-Control"] = "private, no-store"
    for key, value in _signed_agent_response_headers(body, token).items():
        response.headers[key] = value
    return response


@app.get("/core-worker/toolchain/releases/<fingerprint>.zip")
def core_worker_toolchain_release(fingerprint: str):
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token, _worker = _authenticate_core_worker_artifact_request(worker_id)
    if not ok:
        return error_response
    fingerprint = str(fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return jsonify({"ok": False, "error": "fingerprint inválido"}), 400
    path = (_core_worker_toolchain_root() / "releases" / f"{fingerprint}.zip").resolve()
    if not path.is_file() or path.parent != (_core_worker_toolchain_root() / "releases").resolve():
        return jsonify({"ok": False, "error": "toolchain não encontrado"}), 404
    timestamp = str(int(time.time()))
    mac = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("ascii"))
    mac.update(b"\n")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            mac.update(chunk)
    response = send_file(path, mimetype="application/zip", as_attachment=False, conditional=False, etag=fingerprint)
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    response.headers["X-Core-Worker-Timestamp"] = timestamp
    response.headers["X-Core-Worker-Signature"] = f"sha256={mac.hexdigest()}"
    response.headers["X-Core-Worker-Signature-Schema"] = "ts-body-v1"
    response.headers["X-Core-Worker-Toolchain-Fingerprint"] = fingerprint
    return response


@app.post("/core-worker/jobs/poll")
def core_worker_jobs_poll():
    from utility.commands.workers_registry import core_worker_poll_job_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_poll_job_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker_id = str(body.get("worker_id") or payload.get("worker_id") or payload.get("id") or "")
        if worker_id:
            _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status


@app.post("/core-worker/jobs/progress")
def core_worker_jobs_progress():
    from utility.commands.workers_registry import core_worker_job_progress_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_job_progress_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/jobs/result")
def core_worker_jobs_result():
    from utility.commands.workers_registry import core_worker_job_result_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_job_result_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker_id = str(body.get("worker_id") or payload.get("worker_id") or payload.get("id") or "")
        _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status


@app.get("/tts-audio/<token>")
@app.get("/tts-audio/<token>.<ext>")
def tts_audio(token: str, ext: str | None = None):
    token = str(token or "").strip()
    # Compatibilidade com rotas antigas onde o sufixo vinha incorporado no token.
    for suffix in (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"):
        if token.lower().endswith(suffix):
            token = token[: -len(suffix)]
            break
    if not token:
        abort(404)
    now = time.time()
    with _tts_audio_lock:
        record = _tts_audio_files.get(token)
        if not record:
            abort(404)
        path, expires_at = record
        if expires_at <= now:
            _tts_audio_files.pop(token, None)
            abort(404)
    if not os.path.isfile(path):
        with _tts_audio_lock:
            _tts_audio_files.pop(token, None)
        abort(404)
    lowered = path.lower()
    if lowered.endswith((".ogg", ".opus")):
        mimetype = "audio/ogg"
    elif lowered.endswith((".m4a", ".aac")):
        mimetype = "audio/mp4"
    elif lowered.endswith(".wav"):
        mimetype = "audio/wav"
    else:
        mimetype = "audio/mpeg"
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)


def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print(f"[webserver] usando porta {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)
