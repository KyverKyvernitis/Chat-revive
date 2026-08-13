from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.metadata
import io
import logging
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any

import discord

import config


logger = logging.getLogger(__name__)

SENSITIVE_PATTERNS = (
    r"(?i)(password\s*[:=]\s*)[^\s\"']+",
    r"(?i)(token\s*[:=]\s*)[^\s\"']+",
    r"(?i)(secret\s*[:=]\s*)[^\s\"']+",
    r"(?i)(authorization\s*[:=]\s*)[^\s\"']+",
    r"(?i)(clientSecret\s*[:=]\s*)[^\s\"']+",
    r"(?i)(refreshToken\s*[:=]\s*)[^\s\"']+",
    r"(?i)(arl\s*[:=]\s*)[^\s\"']+",
    r"(?i)(DISCORD_TOKEN=).*",
    r"(?i)(MONGODB_URI=).*",
    r"(?i)(MONGO_URI=).*",
    r"(?i)(WEBHOOK[^=]*=).*",
    r"(?i)([A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|COOKIE|CREDENTIAL|WEBHOOK|MONGODB_URI|MONGO_URI|ARL)[A-Z0-9_]*=).*",
)

REPO_ROOT = Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_MUSICNODE_DB = REPO_ROOT / "data" / "musicnode" / "musicnode.db"

VALID_SPOTIFY_TEST_URL = "https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr?si=I69KMQsjTB2g4La8tLAOCw"
VALID_SPOTIFY_TEST_ID = "3BxXcWY0ZYkNBhiOvy6vWr"
VALID_SOUNDCLOUD_TEST_URL = (
    "https://soundcloud.com/yosoyharmless/untitled-1"
    "?si=f0620a0357c74d5badaa77670b9094a5"
    "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
)
VALID_YOUTUBE_TEST_URL = "https://www.youtube.com/watch?v=qU9mHegkTc4"



@dataclass(slots=True)
class DiagnosticsOptions:
    guild_id: int
    guild_name: str
    requester_id: int
    requester_name: str
    include_journalctl: bool = True
    include_local_logs: bool = True


def redact(text: object) -> str:
    out = str(text if text is not None else "")
    for pattern in SENSITIVE_PATTERNS:
        out = re.sub(pattern, lambda m: f"{m.group(1)}***REDACTED***" if m.groups() else "***REDACTED***", out)
    # Máscara tokens comuns do Discord/webhook e valores longos que parecem secrets.
    out = re.sub(r"https://discord(?:app)?\.com/api/webhooks/[\w\-/]+", "https://discord.com/api/webhooks/***REDACTED***", out, flags=re.I)
    out = re.sub(r"\b[A-Za-z0-9_\-]{24}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{20,}\b", "***DISCORD_TOKEN_REDACTED***", out)
    return out


def _mask_value(key: str, value: Any) -> Any:
    lowered = str(key or "").lower()
    if any(marker in lowered for marker in ("pass", "token", "secret", "cookie", "credential", "webhook", "mongo", "arl", "key")):
        return "***REDACTED***" if value not in (None, "") else ""
    return value


def _diagnostics_timezone():
    """Fuso usado em nomes de anexos e timestamps do /vps.

    A VPS costuma estar em UTC, mas o comando é usado pelo dono do bot no Brasil.
    BOT_TIMEZONE permite trocar sem patch; o fallback é America/Sao_Paulo.
    """
    tz_name = (
        os.getenv("BOT_TIMEZONE")
        or os.getenv("VPS_TIMEZONE")
        or os.getenv("TZ")
        or "America/Sao_Paulo"
    )
    try:
        return ZoneInfo(str(tz_name).strip() or "America/Sao_Paulo")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def diagnostics_file_stamp() -> str:
    return datetime.now(_diagnostics_timezone()).strftime("%Y%m%d-%H%M%S")


def _now_stamp() -> str:
    now = datetime.now(_diagnostics_timezone())
    suffix = now.tzname() or str(now.utcoffset() or "")
    return now.strftime(f"%Y-%m-%d %H:%M:%S {suffix}")





def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _phone_worker_base_url() -> str:
    host = (os.getenv("PHONE_WORKER_HOST") or "").strip()
    if not host:
        return ""
    port = (os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
    scheme = (os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
    return f"{scheme}://{host}:{port}"


def _phone_worker_request_json(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    base = _phone_worker_base_url()
    if not base:
        raise RuntimeError("PHONE_WORKER_HOST não configurado")
    token = (os.getenv("PHONE_WORKER_TOKEN") or "").strip()
    data = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
        raw = resp.read()
    parsed = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise RuntimeError("resposta do phone-worker não é objeto JSON")
    return parsed


def _phone_worker_health_summary(*, timeout: float = 3.0) -> str:
    if not _env_bool("PHONE_WORKER_ENABLED", False):
        return "phone-worker desativado."
    try:
        data = _phone_worker_request_json("/health", timeout=timeout)
        if not data.get("ok"):
            return f"phone-worker respondeu erro: {redact(data)}"
        uptime = data.get("uptime_seconds")
        jobs = data.get("jobs_started")
        failed = data.get("jobs_failed")
        ffmpeg = "sim" if data.get("ffmpeg") else "não"
        ffprobe = "sim" if data.get("ffprobe") else "não"
        return f"phone-worker online · uptime={uptime}s · jobs={jobs} · falhas={failed} · ffmpeg={ffmpeg} · ffprobe={ffprobe}"
    except Exception as exc:
        return f"phone-worker indisponível: {type(exc).__name__}: {str(exc)[:220]}"


def _phone_worker_zip_texts(
    files: list[tuple[str, str]],
    *,
    filename: str,
    compresslevel: int = 6,
    timeout: float | None = None,
) -> tuple[bytes | None, str]:
    """Tenta compactar textos no phone-worker; retorna (payload, status).

    É uma aceleração opcional: falha/offline nunca quebra o diagnóstico, porque o
    chamador deve cair para ZIP local na VPS.
    """
    if not _env_bool("PHONE_WORKER_ENABLED", False):
        return None, "phone-worker desativado."
    if not _phone_worker_base_url():
        return None, "PHONE_WORKER_HOST não configurado."
    max_input_mb = max(1, _env_int("PHONE_WORKER_ZIP_MAX_INPUT_MB", 24))
    max_files = max(1, _env_int("PHONE_WORKER_ZIP_MAX_FILES", 80))
    if len(files) > max_files:
        return None, f"arquivos demais para o phone-worker: {len(files)} > {max_files}."

    encoded_files: list[dict[str, str]] = []
    total_in = 0
    for arcname, text in files:
        data = redact(text if text is not None else "").encode("utf-8", "replace")
        total_in += len(data)
        if total_in > max_input_mb * 1024 * 1024:
            return None, f"entrada grande demais para o phone-worker: {total_in} bytes."
        encoded_files.append({
            "name": str(arcname).replace("\\", "/").lstrip("/") or "file.txt",
            "data_b64": base64.b64encode(data).decode("ascii"),
        })

    payload = {
        "task": "zip",
        "filename": filename,
        "compresslevel": max(1, min(9, int(compresslevel or 6))),
        "files": encoded_files,
    }
    timeout = timeout if timeout is not None else max(3.0, _env_float("PHONE_WORKER_ZIP_TIMEOUT_SECONDS", 18.0))
    try:
        result = _phone_worker_request_json("/task", payload=payload, timeout=timeout)
        if not result.get("ok"):
            return None, f"phone-worker respondeu falha: {redact(result)}"
        raw_b64 = str(result.get("data_b64") or "")
        if not raw_b64:
            return None, "phone-worker não retornou data_b64."
        data = base64.b64decode(raw_b64.encode("ascii"), validate=True)
        status = (
            "phone-worker usado para compactação "
            f"(entrada={result.get('input_size', total_in)} bytes; saída={len(data)} bytes)."
        )
        logger.info("[phone-worker] compactação remota concluída | arquivo=%s entrada=%s saída=%s", filename, result.get("input_size", total_in), len(data))
        return data, status
    except Exception as exc:
        status = f"phone-worker falhou: {type(exc).__name__}: {str(exc)[:260]}"
        logger.info("[phone-worker] compactação remota indisponível; usando fallback local | arquivo=%s motivo=%s", filename, status)
        return None, status


def _local_log_extract_text(text: str, *, pattern: str, max_lines: int) -> tuple[list[str], int]:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except Exception:
        regex = re.compile(r"error|exception|traceback|falhou|failed|fatal|timeout", re.IGNORECASE)
    matches = [line for line in str(text or "").splitlines() if regex.search(line or "")]
    max_lines = max(1, min(500, int(max_lines or 120)))
    return matches[-max_lines:], len(matches)


def _phone_worker_log_extract_text(
    text: str,
    *,
    source: str,
    pattern: str = r"error|exception|traceback|falhou|failed|fatal|timeout|TrackException|LoadException|fallback|restart|crash",
    max_lines: int | None = None,
    timeout: float | None = None,
) -> str:
    """Extrai linhas importantes usando o celular quando possível, com fallback local."""
    max_lines = max_lines if max_lines is not None else _env_int("PHONE_WORKER_LOG_MAX_LINES", 160)
    max_input_mb = max(1, _env_int("PHONE_WORKER_LOG_MAX_INPUT_MB", 8))
    timeout = timeout if timeout is not None else max(2.0, _env_float("PHONE_WORKER_LOG_TIMEOUT_SECONDS", 8.0))
    raw = redact(text or "")
    raw_bytes = raw.encode("utf-8", "replace")
    truncated = False
    if len(raw_bytes) > max_input_mb * 1024 * 1024:
        raw_bytes = raw_bytes[-max_input_mb * 1024 * 1024:]
        raw = raw_bytes.decode("utf-8", "replace")
        truncated = True

    if _env_bool("PHONE_WORKER_ENABLED", False) and _phone_worker_base_url():
        try:
            result = _phone_worker_request_json(
                "/task",
                payload={"task": "log_extract", "text": raw, "pattern": pattern, "max_lines": max_lines},
                timeout=timeout,
            )
            if result.get("ok"):
                matches = [str(item) for item in (result.get("matches") or [])]
                count = int(result.get("count") or len(matches))
                logger.info("[phone-worker] extração de logs concluída no celular | fonte=%s total=%s retornadas=%s", source, count, len(matches))
                header = [
                    f"fonte: {source}",
                    "processamento: phone-worker",
                    f"linhas encontradas: {count}",
                    f"linhas retornadas: {len(matches)}",
                ]
                if truncated:
                    header.append(f"entrada enviada ao worker foi cortada para os últimos {max_input_mb} MB")
                return "\n".join(header + ["", *(matches or ["nenhuma linha relevante encontrada"])]) + "\n"
            logger.info("[phone-worker] extração de logs respondeu falha; fallback local | fonte=%s resposta=%s", source, redact(result))
        except Exception as exc:
            logger.info("[phone-worker] extração de logs indisponível; fallback local | fonte=%s motivo=%s: %s", source, type(exc).__name__, str(exc)[:180])

    matches, count = _local_log_extract_text(raw, pattern=pattern, max_lines=max_lines)
    header = [
        f"fonte: {source}",
        "processamento: local na VPS",
        f"linhas encontradas: {count}",
        f"linhas retornadas: {len(matches)}",
    ]
    if truncated:
        header.append(f"entrada local foi cortada para os últimos {max_input_mb} MB")
    return "\n".join(header + ["", *(matches or ["nenhuma linha relevante encontrada"])]) + "\n"


def _phone_worker_text_stats_summary(text: str, *, source: str, timeout: float | None = None) -> str:
    raw = redact(text or "")
    if not raw.strip():
        return f"fonte: {source}\nsem texto para analisar\n"
    max_input_mb = max(1, _env_int("PHONE_WORKER_TEXT_STATS_MAX_INPUT_MB", 8))
    raw_bytes = raw.encode("utf-8", "replace")
    truncated = False
    if len(raw_bytes) > max_input_mb * 1024 * 1024:
        raw_bytes = raw_bytes[-max_input_mb * 1024 * 1024:]
        raw = raw_bytes.decode("utf-8", "replace")
        truncated = True
    timeout = timeout if timeout is not None else max(2.0, _env_float("PHONE_WORKER_TEXT_STATS_TIMEOUT_SECONDS", 6.0))
    if _env_bool("PHONE_WORKER_ENABLED", False) and _phone_worker_base_url():
        try:
            result = _phone_worker_request_json("/task", payload={"task": "text_stats", "text": raw}, timeout=timeout)
            if result.get("ok"):
                logger.info("[phone-worker] estatísticas de texto calculadas no celular | fonte=%s bytes=%s linhas=%s", source, result.get("bytes"), result.get("lines"))
                lines = [
                    f"fonte: {source}",
                    "processamento: phone-worker",
                    f"bytes: {result.get('bytes')}",
                    f"caracteres: {result.get('chars')}",
                    f"linhas: {result.get('lines')}",
                    f"palavras: {result.get('words')}",
                    f"sha256: {result.get('sha256')}",
                ]
                if truncated:
                    lines.append(f"entrada enviada ao worker foi cortada para os últimos {max_input_mb} MB")
                return "\n".join(lines) + "\n"
            logger.info("[phone-worker] text_stats respondeu falha; fallback local | fonte=%s resposta=%s", source, redact(result))
        except Exception as exc:
            logger.info("[phone-worker] text_stats indisponível; fallback local | fonte=%s motivo=%s: %s", source, type(exc).__name__, str(exc)[:180])
    words = raw.split()
    lines = raw.splitlines()
    import hashlib
    out = [
        f"fonte: {source}",
        "processamento: local na VPS",
        f"bytes: {len(raw.encode('utf-8', 'replace'))}",
        f"caracteres: {len(raw)}",
        f"linhas: {len(lines)}",
        f"palavras: {len(words)}",
        f"sha256: {hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()}",
    ]
    if truncated:
        out.append(f"entrada local foi cortada para os últimos {max_input_mb} MB")
    return "\n".join(out) + "\n"



def _local_log_summary_obj(text: str, *, max_recent: int = 12, max_top: int = 12) -> dict[str, Any]:
    raw = str(text or "")
    patterns = {
        "critical": r"\bcritical\b|\bcritico\b|\bcrítico\b|\bfatal\b",
        "error": r"\berror\b|\berro\b",
        "warning": r"\bwarning\b|\bwarn\b|\baviso\b",
        "timeout": r"timeout|timed out|tempo esgotado",
        "traceback": r"traceback",
        "exception": r"exception|exce[cç][aã]o",
        "failed": r"failed|falhou|failure|falha",
        "restart": r"restart|restarting|started|stopped|iniciando|parando",
        "syntax": r"syntaxerror|indentationerror|taberror",
        "import": r"importerror|modulenotfounderror|extensionfailed|extensionnotfound",
        "lavalink": r"lavalink|lavasrc|trackexception|loadexception",
        "yt_dlp": r"yt[-_ ]?dlp|youtube|googlevideo",
        "rate_limit": r"rate.?limit|too many requests|429",
        "phone_worker": r"phone-worker|phone_lavalink|phone-lavalink",
    }
    compiled = {key: re.compile(pattern, re.IGNORECASE) for key, pattern in patterns.items()}
    counts = {key: 0 for key in compiled}
    recent: list[str] = []
    grouped: dict[str, int] = {}
    for line in raw.splitlines():
        hit = False
        for key, regex in compiled.items():
            if regex.search(line or ""):
                counts[key] += 1
                hit = True
        if hit:
            stripped = line.strip()
            recent.append(stripped)
            normalized = re.sub(r"^\d{4}-\d{2}-\d{2}[T\s][^\s]+\s+", "", stripped)
            normalized = re.sub(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+", "", normalized)
            normalized = re.sub(r"^[\w@./+\-]+(?:\[\d+\])?:\s*", "", normalized)
            normalized = re.sub(r"\b(guild|channel|user)=\d+\b", r"\1=<id>", normalized)
            normalized = re.sub(r"\b\d{15,22}\b", "<snowflake>", normalized)
            normalized = re.sub(r"\s+", " ", normalized).strip()[:220] or "linha vazia"
            grouped[normalized] = grouped.get(normalized, 0) + 1
    top_messages = [
        {"message": msg, "count": count}
        for msg, count in sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:max(1, max_top)]
    ]
    return {
        "ok": True,
        "bytes": len(raw.encode("utf-8", "replace")),
        "lines": len(raw.splitlines()),
        "important_count": len(recent),
        "counts": counts,
        "recent": recent[-max(1, max_recent):],
        "top_messages": top_messages,
    }


def _format_log_summary(data: dict[str, Any], *, source: str, processing: str, truncated: bool = False) -> str:
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    recent = [str(item) for item in (data.get("recent") or [])]
    top_messages = data.get("top_messages") if isinstance(data.get("top_messages"), list) else []
    count_order = [
        ("critical", "críticos"),
        ("error", "erros"),
        ("warning", "avisos"),
        ("timeout", "timeouts"),
        ("failed", "falhas"),
        ("exception", "exceptions"),
        ("traceback", "tracebacks"),
        ("syntax", "syntax/import"),
        ("lavalink", "lavalink"),
        ("yt_dlp", "yt/ytdlp"),
        ("rate_limit", "rate limit"),
        ("phone_worker", "phone worker"),
    ]
    lines = [
        f"fonte: {source}",
        f"processamento: {processing}",
        f"linhas analisadas: {data.get('lines')}",
        f"linhas importantes: {data.get('important_count')}",
        "",
        "contagens:",
    ]
    lines.extend(f"- {label}: {counts.get(key, 0)}" for key, label in count_order)
    if truncated:
        lines.append("entrada enviada ao worker foi cortada por limite de tamanho")
    lines.append("")
    lines.append("top mensagens agrupadas:")
    if top_messages:
        for item in top_messages[:12]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('count', 0)}x · {str(item.get('message') or '')[:240]}")
    else:
        lines.append("- nenhuma mensagem relevante agrupada")
    lines.append("")
    lines.append("recentes:")
    lines.extend(f"- {line[:260]}" for line in (recent or ["nenhuma linha relevante encontrada"]))
    return "\n".join(lines).rstrip() + "\n"


def _phone_worker_log_summary_text(
    text: str,
    *,
    source: str,
    max_recent: int | None = None,
    max_top: int | None = None,
    timeout: float | None = None,
) -> str:
    raw = redact(text or "")
    if not raw.strip():
        return f"fonte: {source}\nsem logs para resumir\n"
    max_recent = max_recent if max_recent is not None else _env_int("PHONE_WORKER_LOG_SUMMARY_MAX_RECENT", 12)
    max_top = max_top if max_top is not None else _env_int("PHONE_WORKER_LOG_SUMMARY_MAX_TOP", 12)
    max_input_mb = max(1, _env_int("PHONE_WORKER_LOG_SUMMARY_MAX_INPUT_MB", 8))
    raw_bytes = raw.encode("utf-8", "replace")
    truncated = False
    if len(raw_bytes) > max_input_mb * 1024 * 1024:
        raw_bytes = raw_bytes[-max_input_mb * 1024 * 1024:]
        raw = raw_bytes.decode("utf-8", "replace")
        truncated = True
    timeout = timeout if timeout is not None else max(2.0, _env_float("PHONE_WORKER_LOG_SUMMARY_TIMEOUT_SECONDS", 7.0))
    if _env_bool("PHONE_WORKER_ENABLED", False) and _phone_worker_base_url():
        try:
            result = _phone_worker_request_json(
                "/task",
                payload={"task": "log_summary", "text": raw, "max_recent": max_recent, "max_top": max_top},
                timeout=timeout,
            )
            if result.get("ok"):
                logger.info("[phone-worker] resumo inteligente de logs calculado no celular | fonte=%s importantes=%s", source, result.get("important_count"))
                return _format_log_summary(result, source=source, processing="phone-worker", truncated=truncated)
            logger.info("[phone-worker] log_summary respondeu falha; fallback local | fonte=%s resposta=%s", source, redact(result))
        except Exception as exc:
            logger.info("[phone-worker] log_summary indisponível; fallback local | fonte=%s motivo=%s: %s", source, type(exc).__name__, str(exc)[:180])
    local = _local_log_summary_obj(raw, max_recent=max_recent, max_top=max_top)
    return _format_log_summary(local, source=source, processing="local na VPS", truncated=truncated)

def _zip_texts_local(files: list[tuple[str, str]], *, compresslevel: int = 6) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=max(1, min(9, int(compresslevel or 6)))) as zf:
        for arcname, text in files:
            _write_zip_text(zf, arcname, text)
    return bio.getvalue()

def cleanup_music_diagnostics_temp_artifacts(*, max_age_seconds: float = 12 * 3600) -> str:
    """Remove apenas artefatos temporários de diagnóstico, nunca logs reais.

    O diagnóstico musical deve continuar completo, mas não precisa manter zips/txt
    antigos em pastas temporárias. A função é conservadora e apaga só nomes que
    seguem o padrão do próprio diagnóstico.
    """
    now = time.time()
    roots = [
        Path("/tmp"),
        REPO_ROOT / "data" / "diagnostics" / "tmp",
        REPO_ROOT / "data" / "diagnostics",
    ]
    patterns = (
        "music-diag-*.zip",
        "music-diag-*.txt",
        "music-diag-emergency-*.txt",
        "full-diag-*.txt",
        "status-*.txt",
        "vps-resumo-*.txt",
        # nomes antigos, para limpar sobras de versões anteriores
        "vps-music-diagnostics-*.zip",
        "vps-music-diagnostics-summary-*.txt",
        "vps-music-diagnostics-*.txt",
        "vps-full-diagnostics-*.txt",
    )
    removed = 0
    checked = 0
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            with contextlib.suppress(Exception):
                for path in root.glob(pattern):
                    checked += 1
                    if not path.is_file():
                        continue
                    try:
                        age = now - path.stat().st_mtime
                    except Exception:
                        continue
                    if age >= max_age_seconds:
                        with contextlib.suppress(Exception):
                            path.unlink()
                            removed += 1
    return f"diagnostic-temp-cleanup: checked={checked} removed={removed} max_age_seconds={int(max_age_seconds)}"

def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "não encontrado"


def _run_cmd(args: list[str], *, timeout: float = 12.0, cwd: Path | None = None) -> str:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd or REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        body = cp.stdout or ""
        return f"$ {' '.join(args)}\nexit={cp.returncode}\n{body}"
    except subprocess.TimeoutExpired as exc:
        body = exc.stdout or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return f"$ {' '.join(args)}\nTIMEOUT após {timeout}s\n{body}"
    except Exception as exc:
        return f"$ {' '.join(args)}\nERRO: {type(exc).__name__}: {exc}"


def _read_env_flags() -> dict[str, Any]:
    names = [
        "LAVALINK_ENABLED",
        "LAVALINK_MODE",
        "MUSIC_NODE_PROVIDER",
        "AUDIO_NODE_FAILURE_COOLDOWN_SECONDS",
        "AUDIO_NODE_STARTUP_WAIT_SECONDS",
        "LAVALINK_HOST",
        "LAVALINK_PORT",
        "WAVELINK_HOST",
        "WAVELINK_PORT",
        "MUSIC_LAVASRC_MIRROR_PREFIXES",
        "MUSIC_LAVALINK_ENABLE_ALL_GUILDS",
        "YTDLP_COOKIES_FILE",
        "MUSIC_YTDLP_COOKIES_FILE",
        "YT_DLP_COOKIES_FILE",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_ID",
        "LAVASRC_SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_SECRET",
        "LAVASRC_SPOTIFY_CLIENT_SECRET",
        "DEEZER_API_ENABLED",
        "LAVASRC_DEEZER_ARL",
        "LAVASRC_DEEZER_MASTER_DECRYPTION_KEY",
        "TTS_VOICE_AUTO_RESTORE_ENABLED",
        "MUSIC_TTS_PUBLIC_BASE_URL",
        "MUSIC_TTS_INTERNAL_BASE_URL",
        "MUSIC_LAVALINK_TTS_INTERNAL_FIRST",
        "MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS",
        "MUSIC_TTS_AUDIO_FORMAT",
        "MUSIC_TTS_AUDIO_FALLBACK_FORMAT",
        "MUSIC_TTS_OPUS_BITRATE",
        "MUSIC_TTS_OPUS_SAMPLE_RATE",
        "MUSIC_TTS_OPUS_CHANNELS",
        "MUSIC_TTS_CONVERT_TIMEOUT_SECONDS",
        "MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED",
        "MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS",
        "MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB",
        "MUSIC_TTS_PREROLL_SILENCE_MS",
        "MUSIC_TTS_POSTROLL_SILENCE_MS",
        "MUSIC_TTS_FADE_IN_MS",
        "MUSIC_TTS_FADE_OUT_MS",
        "MUSIC_TTS_RESUME_SEEK_AHEAD_MS",
        "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED",
        "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS",
        "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT",
        "PHONE_WORKER_ENABLED",
        "PHONE_WORKER_HOST",
        "PHONE_WORKER_PORT",
        "PHONE_WORKER_TOKEN",
        "PHONE_WORKER_SSH_USER",
        "PHONE_WORKER_SSH_PORT",
        "PHONE_WORKER_START_COMMAND",
        "PHONE_WORKER_ZIP_TIMEOUT_SECONDS",
        "PHONE_WORKER_ZIP_MAX_INPUT_MB",
        "PHONE_WORKER_ZIP_MAX_FILES",
        "PHONE_WORKER_LOG_TIMEOUT_SECONDS",
        "PHONE_WORKER_LOG_MAX_INPUT_MB",
        "PHONE_WORKER_LOG_MAX_LINES",
        "PHONE_WORKER_TEXT_STATS_TIMEOUT_SECONDS",
        "PHONE_WORKER_TEXT_STATS_MAX_INPUT_MB",
        "PHONE_LAVALINK_WATCH_ENABLED",
        "PHONE_LAVALINK_HOST",
        "PHONE_LAVALINK_PORT",
        "PHONE_LAVALINK_PASSWORD",
    ]
    result: dict[str, Any] = {}
    for name in names:
        value = os.getenv(name)
        if value is None:
            result[name] = "não definido"
            continue
        if any(marker in name.lower() for marker in ("secret", "token", "password", "cookie", "arl")):
            result[name] = f"definido ({len(value)} caracteres)"
        elif name.endswith("_ID") or name in {"SPOTIFY_ID", "LAVASRC_SPOTIFY_CLIENT_ID"}:
            result[name] = f"definido ({len(value)} caracteres)" if value else "vazio"
        else:
            result[name] = value
    return result


def _db_snapshot(db_path: Path = DEFAULT_MUSICNODE_DB) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return "DB do musicnode não encontrado.", data
    try:
        con = sqlite3.connect(str(db_path), timeout=8.0)
        con.row_factory = sqlite3.Row
        try:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            data["tables"] = tables
            rows_by_table: dict[str, list[dict[str, Any]]] = {}
            for table in tables:
                if table not in {"lavalink_nodes", "lavalink_guild_settings", "lavalink_meta"}:
                    continue
                rows: list[dict[str, Any]] = []
                for row in con.execute(f"SELECT * FROM {table} LIMIT 30"):
                    item = {key: _mask_value(key, row[key]) for key in row.keys()}
                    rows.append(item)
                rows_by_table[table] = rows
            data["rows"] = rows_by_table
            return json.dumps(data, ensure_ascii=False, indent=2), data
        finally:
            con.close()
    except Exception as exc:
        data["error"] = f"{type(exc).__name__}: {exc}"
        return json.dumps(data, ensure_ascii=False, indent=2), data


def _lavalink_cfg_from_router(router: Any, guild_id: int) -> dict[str, Any]:
    cfg_data: dict[str, Any] = {}
    try:
        backends = getattr(router, "backends", None)
        store = getattr(backends, "lavalink_store", None)
        if backends is not None and hasattr(backends, "_node_config_for_guild"):
            cfg = backends._node_config_for_guild(guild_id)  # diagnóstico interno: mostra o node efetivo.
        elif store is not None:
            cfg = store.load(guild_id=guild_id)
        else:
            cfg = None
        if cfg is not None:
            cfg_data = {
                "provider": str(getattr(cfg, "provider", "lavalink") or "lavalink"),
                "provider_label": str(getattr(cfg, "provider_label", "Lavalink") or "Lavalink"),
                "enabled": bool(getattr(cfg, "enabled", False)),
                "configured": bool(getattr(cfg, "configured", False)),
                "mode": str(getattr(cfg, "mode", "") or ""),
                "node_name": str(getattr(cfg, "node_name", "") or ""),
                "host": str(getattr(cfg, "host", "") or ""),
                "port": int(getattr(cfg, "port", 0) or 0),
                "secure": bool(getattr(cfg, "secure", False)),
                "password": "***REDACTED***" if getattr(cfg, "password", "") else "",
                "password_defined": bool(getattr(cfg, "password", "")),
                "timeout_seconds": float(getattr(cfg, "timeout_seconds", 0) or 0),
                "base_url": str(getattr(cfg, "base_url", "") or ""),
                "raw_password": str(getattr(cfg, "password", "") or ""),
            }
            with contextlib.suppress(Exception):
                cfg_data["summary"] = store.summary(guild_id=guild_id) if store is not None else {}
            with contextlib.suppress(Exception):
                runtime = backends.compact_runtime_summary(guild_id=guild_id) if backends is not None else {}
                cfg_data["runtime"] = runtime
    except Exception as exc:
        cfg_data = {"error": f"{type(exc).__name__}: {exc}"}
    return cfg_data


def _http_json(url: str, *, password: str = "", timeout: float = 18.0) -> dict[str, Any]:
    headers = {}
    if password:
        headers["Authorization"] = password
    req = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500_000).decode("utf-8", "replace")
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            try:
                data = json.loads(body)
            except Exception:
                data = body[:1000]
            return {"ok": True, "status": resp.status, "latency_ms": elapsed, "data": data}
    except urllib.error.HTTPError as exc:
        body = exc.read(80_000).decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "error": body[:1500]}
    except Exception as exc:
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}


def _summarize_loadtracks(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("ok"):
        return data
    payload = data.get("data")
    if not isinstance(payload, dict):
        return {**data, "summary": "resposta não JSON/dict"}
    load_type = payload.get("loadType")
    raw_tracks = payload.get("data") or []
    tracks: list[Any]
    if isinstance(raw_tracks, dict) and "tracks" in raw_tracks:
        tracks = raw_tracks.get("tracks") or []
    elif isinstance(raw_tracks, dict):
        tracks = [raw_tracks]
    elif isinstance(raw_tracks, list):
        tracks = raw_tracks
    else:
        tracks = []
    brief_tracks = []
    for track in tracks[:5]:
        if not isinstance(track, dict):
            continue
        info = track.get("info") if isinstance(track.get("info"), dict) else {}
        brief_tracks.append({
            "sourceName": info.get("sourceName"),
            "title": info.get("title"),
            "author": info.get("author"),
            "length": info.get("length"),
            "uri": info.get("uri"),
        })
    out = {k: v for k, v in data.items() if k != "data"}
    out["loadType"] = load_type
    out["tracks_found"] = len(tracks)
    out["tracks"] = brief_tracks
    if load_type == "error":
        out["error_data"] = raw_tracks
    return out


def _application_yml_text() -> str:
    path = Path("/opt/lavalink/application.yml")
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _application_lavasrc_analysis() -> dict[str, Any]:
    text = _application_yml_text()
    data: dict[str, Any] = {
        "path": "/opt/lavalink/application.yml",
        "exists": bool(text),
        "providers": [],
        "sources": {},
        "warnings": [],
    }
    if not text:
        data["warnings"].append("application.yml não encontrado ou inacessível.")
        return data

    lines = text.splitlines()
    in_providers = False
    in_sources = False
    for line in lines:
        stripped = line.strip()
        if stripped == "providers:":
            in_providers = True
            in_sources = False
            continue
        if stripped == "sources:":
            in_sources = True
            in_providers = False
            continue
        if in_providers:
            if re.match(r"^\s+-\s+", line):
                data["providers"].append(stripped.lstrip("- ").strip().strip('"'))
                continue
            if stripped and not line.startswith("      "):
                in_providers = False
        if in_sources:
            m = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(true|false)\s*$", line)
            if m:
                data["sources"][m.group(1)] = m.group(2).lower() == "true"
                continue
            if stripped and not line.startswith("      "):
                in_sources = False

    deezer_enabled = bool(data["sources"].get("deezer"))
    has_deezer_master = bool(re.search(r"(?m)^\s*masterDecryptionKey:\s*\S+", text))
    has_deezer_arl = bool(re.search(r"(?m)^\s*arl:\s*\S+", text))
    has_dz_provider = any(str(item).startswith(("dzsearch:", "dzisrc:")) for item in data["providers"])
    data["deezer_master_key_defined"] = has_deezer_master
    data["deezer_arl_defined"] = has_deezer_arl
    if deezer_enabled and not has_deezer_master:
        data["warnings"].append("Deezer está ligado no LavaSrc, mas masterDecryptionKey não foi configurada. Isso derruba o Lavalink com 'Deezer master key must be set'.")
    if has_dz_provider and not deezer_enabled:
        data["warnings"].append("Provider dzsearch/dzisrc está configurado, mas sources.deezer está false; dzsearch tende a falhar/ficar inútil.")
    if has_dz_provider and deezer_enabled and not has_deezer_master:
        data["warnings"].append("Remova dzsearch ou configure Deezer corretamente antes de usar esse provider.")
    return data


def _mirror_prefixes_for_diagnostics() -> tuple[list[str], list[str]]:
    raw = os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "scsearch") or "scsearch"
    prefixes: list[str] = []
    notes: list[str] = []
    analysis = _application_lavasrc_analysis()
    deezer_ok = bool(analysis.get("sources", {}).get("deezer")) and bool(analysis.get("deezer_master_key_defined"))
    for item in re.split(r"[,;\s]+", raw.strip()):
        prefix = item.strip().lower().removesuffix(":")
        if not prefix:
            continue
        if prefix.startswith("dz") and not deezer_ok:
            notes.append(f"{prefix} ignorado no diagnóstico porque Deezer não está totalmente configurado no LavaSrc.")
            continue
        if prefix not in {"scsearch", "spsearch", "dzsearch", "amsearch"}:
            notes.append(f"{prefix} ignorado: prefixo desconhecido para mirror LavaSrc.")
            continue
        if prefix not in prefixes:
            prefixes.append(prefix)
    if not prefixes:
        prefixes = ["scsearch"]
        notes.append("Mirror efetivo do diagnóstico caiu para scsearch.")
    return prefixes, notes



def _spotify_api_fetch_test_track() -> tuple[dict[str, Any] | None, list[str]]:
    """Busca a metadata do track Spotify de teste.

    Retorna também linhas de diagnóstico para que o dry-run consiga explicar
    quando caiu para metadata estática. O dry-run não toca áudio nem entra em call.
    """
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "") or os.getenv("SPOTIFY_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "") or os.getenv("SPOTIFY_SECRET", "")
    lines: list[str] = []
    if not client_id or not client_secret:
        lines.append("Spotify API não consultada no dry-run: credenciais ausentes.")
        return None, lines
    import base64
    try:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            token_payload = json.loads(resp.read().decode("utf-8", "replace"))
        token = token_payload.get("access_token") or ""
        if not token:
            lines.append("Spotify API retornou token vazio no dry-run.")
            return None, lines
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/tracks/{VALID_SPOTIFY_TEST_ID}?market=BR",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            track = json.loads(resp.read().decode("utf-8", "replace"))
        artists = [a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict) and a.get("name")]
        data = {
            "ok": True,
            "metadata_source": "Spotify API",
            "id": track.get("id") or VALID_SPOTIFY_TEST_ID,
            "title": track.get("name") or "Castle Vein",
            "artists": ", ".join(artists) or "Heaven Pierce Her",
            "primary_artist": artists[0] if artists else "Heaven Pierce Her",
            "duration_ms": track.get("duration_ms") or 340480,
            "url": VALID_SPOTIFY_TEST_URL,
        }
        return data, lines
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        lines.append(f"Spotify API indisponível no dry-run: HTTP {exc.code} {redact(body[:600])}")
    except Exception as exc:
        lines.append(f"Spotify API indisponível no dry-run: {type(exc).__name__}: {exc}")
    return None, lines


def _spotify_static_test_track_metadata() -> dict[str, Any]:
    return {
        "ok": False,
        "metadata_source": "fallback estático do diagnóstico",
        "id": VALID_SPOTIFY_TEST_ID,
        "title": "Castle Vein",
        "artists": "Heaven Pierce Her",
        "primary_artist": "Heaven Pierce Her",
        "duration_ms": 340480,
        "url": VALID_SPOTIFY_TEST_URL,
    }


def _spotify_dry_run_mirror_test(cfg: dict[str, Any]) -> str:
    """Simula Spotify -> mirror LavaSrc -> decisão de fallback sem tocar áudio.

    Esse teste existe para validar a query gerada pelo bot e a decisão que seria
    tomada pelo fluxo real, mas sem entrar em call, mexer em fila ou trocar música.
    """
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    password = str(cfg.get("raw_password") or "")
    provider_label = str(cfg.get("provider_label") or "Lavalink")
    lines: list[str] = [
        "Este teste NÃO entra em call e NÃO toca áudio; ele só simula a resolução Spotify -> mirror/fallback.",
        f"Link Spotify de teste: {VALID_SPOTIFY_TEST_URL}",
    ]
    if not base_url or not password:
        lines.append(f"{provider_label} não configurado ou senha ausente; dry-run encerrado.")
        return "\n".join(lines)

    track_meta, api_notes = _spotify_api_fetch_test_track()
    if api_notes:
        lines.append("Notas Spotify API:")
        lines.extend(f"- {note}" for note in api_notes)
    if track_meta is None:
        track_meta = _spotify_static_test_track_metadata()
        lines.append("Usando metadata estática conhecida para ainda validar normalização de query e mirror.")

    title = str(track_meta.get("title") or "Castle Vein").strip()
    primary_artist = str(track_meta.get("primary_artist") or "Heaven Pierce Her").strip()
    duration_ms = int(track_meta.get("duration_ms") or 0)
    spotify_url = str(track_meta.get("url") or VALID_SPOTIFY_TEST_URL).strip()

    try:
        from .backends.lavalink import LavalinkBackend, LavalinkConfig
        from .models import MusicTrack

        backend = LavalinkBackend(LavalinkConfig(
            enabled=True,
            mode=str(cfg.get("mode") or "auto"),
            host=str(cfg.get("host") or "127.0.0.1"),
            port=int(cfg.get("port") or 2333),
            password=password,
            secure=bool(cfg.get("secure") or False),
            node_name=str(cfg.get("node_name") or "lavalink"),
            timeout_seconds=float(cfg.get("timeout_seconds") or 45.0),
            provider=str(cfg.get("provider") or "lavalink"),
        ))
        # Simula o formato que o fluxo local do Spotify costuma entregar:
        # título já pode vir como "Artista - Música" e uploader também como artista.
        # A correção esperada é não gerar "Artista Artista - Música".
        simulated_track = MusicTrack(
            title=f"{primary_artist} - {title}",
            webpage_url=spotify_url,
            requester_id=0,
            requester_name="diagnóstico",
            duration=duration_ms / 1000 if duration_ms else None,
            uploader=primary_artist,
            source="spotify",
            original_url=spotify_url,
            extractor="spotify",
        )
        metadata_query = backend._metadata_search_query(simulated_track, fallback_query=spotify_url)
        candidates = backend._mirror_search_candidates(simulated_track, fallback_query=spotify_url)
    except Exception as exc:
        lines.append(f"ERRO ao gerar query pelo backend: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    prefixes, prefix_notes = _mirror_prefixes_for_diagnostics()
    lines.append("Metadata usada:")
    lines.append(json.dumps(_safe_report_obj({
        "source": track_meta.get("metadata_source"),
        "title": title,
        "artist": primary_artist,
        "duration_ms": duration_ms,
    }), ensure_ascii=False, indent=2))
    lines.append(f"Mirror prefixes efetivos: {', '.join(prefixes)}")
    if prefix_notes:
        lines.append("Notas de prefixo:")
        lines.extend(f"- {note}" for note in prefix_notes)
    lines.append(f"Query metadata normalizada: {metadata_query!r}")
    lines.append("Candidatos gerados:")
    lines.append(json.dumps(candidates[:6], ensure_ascii=False, indent=2))

    decision = "fallback local · Spotify"
    tested: list[dict[str, Any]] = []
    for candidate in candidates[:4]:
        enc = urllib.parse.quote(candidate, safe="")
        result = _http_json(f"{base_url}/v4/loadtracks?identifier={enc}", password=password, timeout=10.0)
        summary = _summarize_loadtracks(result)
        first = (summary.get("tracks") or [None])[0] if isinstance(summary, dict) else None
        item: dict[str, Any] = {
            "candidate": candidate,
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "latency_ms": result.get("latency_ms"),
            "loadType": summary.get("loadType") if isinstance(summary, dict) else None,
            "tracks_found": summary.get("tracks_found") if isinstance(summary, dict) else None,
            "first_track": first,
            "strict_match": False,
        }
        if isinstance(first, dict):
            compare_meta = {
                "title": first.get("title") or "",
                "uploader": first.get("author") or "",
                "duration": (float(first.get("length") or 0) / 1000) if first.get("length") else None,
            }
            try:
                item["strict_match"] = bool(backend._mirror_meta_matches_track(simulated_track, compare_meta, candidate=candidate))
            except Exception as exc:
                item["match_error"] = f"{type(exc).__name__}: {exc}"
        tested.append(item)
        if item.get("strict_match"):
            decision = f"{provider_label} · mirror Spotify aprovado"
            break

    lines.append("Resultados dos mirrors testados:")
    lines.append(json.dumps(_safe_report_obj(tested), ensure_ascii=False, indent=2))
    lines.append(f"Decisão simulada: {decision}")
    if any("  " in str(c) for c in candidates):
        lines.append("AVISO: há espaços duplicados nos candidatos gerados.")
    duplicated_pattern = f"{primary_artist} {primary_artist}".lower()
    if duplicated_pattern in "\n".join(str(c).lower() for c in candidates):
        lines.append("AVISO: artista ainda apareceu duplicado na query gerada.")
    return "\n".join(lines)


def _service_restart_markers() -> str:
    cmds = [
        ["systemctl", "show", "tts-bot.service", "-p", "ActiveEnterTimestamp", "-p", "ExecMainStartTimestamp", "-p", "MainPID", "--no-pager"],
        ["systemctl", "show", "lavalink.service", "-p", "ActiveEnterTimestamp", "-p", "ExecMainStartTimestamp", "-p", "MainPID", "--no-pager"],
    ]
    lines = [
        "Use estes horários para diferenciar logs antigas de logs pós-restart.",
        "Logs locais podem conter histórico antigo; journalctl por unidade é mais confiável para eventos recentes.",
    ]
    for cmd in cmds:
        lines.append(_run_cmd(cmd, timeout=8.0, cwd=REPO_ROOT))
    return "\n\n".join(lines)

def _spotify_api_test() -> str:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "") or os.getenv("SPOTIFY_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "") or os.getenv("SPOTIFY_SECRET", "")
    lines = [f"Link Spotify válido usado no diagnóstico: {VALID_SPOTIFY_TEST_URL}"]
    lines.append(f"client_id: {'definido (' + str(len(client_id)) + ' chars)' if client_id else 'não definido'}")
    lines.append(f"client_secret: {'definido (' + str(len(client_secret)) + ' chars)' if client_secret else 'não definido'}")
    if not client_id or not client_secret:
        lines.append("Spotify API não testada: SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET ausentes.")
        return "\n".join(lines)
    import base64
    try:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            token_payload = json.loads(resp.read().decode("utf-8", "replace"))
        token = token_payload.get("access_token") or ""
        lines.append(f"token_ok: {bool(token)}")
        if not token:
            return "\n".join(lines)
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/tracks/{VALID_SPOTIFY_TEST_ID}?market=BR",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            track = json.loads(resp.read().decode("utf-8", "replace"))
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict))
        summary = {
            "ok": True,
            "id": track.get("id"),
            "title": track.get("name"),
            "artists": artists,
            "duration_ms": track.get("duration_ms"),
            "preview_url_defined": bool(track.get("preview_url")),
        }
        lines.append(json.dumps(summary, ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        lines.append(f"HTTP_ERROR: {exc.code}")
        lines.append(redact(body[:1200]))
    except Exception as exc:
        lines.append(f"ERRO: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


def _lavalink_tests(cfg: dict[str, Any]) -> str:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    password = str(cfg.get("raw_password") or "")
    provider_label = str(cfg.get("provider_label") or "Lavalink")
    if not base_url or not password:
        return f"{provider_label} não configurado ou senha ausente."
    lines: list[str] = []
    lines.append(f"Node efetivo testado: {provider_label} ({base_url})")
    analysis = _application_lavasrc_analysis()
    lines.append("Análise rápida do application.yml/LavaSrc:")
    lines.append(json.dumps(_safe_report_obj(analysis), ensure_ascii=False, indent=2))
    if analysis.get("warnings"):
        lines.append("\nAVISO: há alerta de configuração acima. Se o Lavalink estiver em Connection refused, corrija isso primeiro.")

    info = _http_json(f"{base_url}/v4/info", password=password, timeout=8.0)
    lines.append("\n/v4/info:")
    lines.append(json.dumps(_safe_report_obj(info), ensure_ascii=False, indent=2))

    prefixes, notes = _mirror_prefixes_for_diagnostics()
    if notes:
        lines.append("\nNotas sobre mirrors configurados:")
        lines.extend(f"- {note}" for note in notes)
    mirror_prefix = prefixes[0]

    tests = [
        ("MP3 HTTP direto", "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"),
        ("SoundCloud busca", "scsearch:megalovania"),
        ("SoundCloud link válido", VALID_SOUNDCLOUD_TEST_URL),
        (f"Mirror LavaSrc configurado ({mirror_prefix})", f"{mirror_prefix}:505 arctic monkeys"),
    ]
    for label, identifier in tests:
        enc = urllib.parse.quote(identifier, safe="")
        result = _http_json(f"{base_url}/v4/loadtracks?identifier={enc}", password=password, timeout=12.0)
        lines.append(f"\n{label} -> {identifier}:")
        lines.append(json.dumps(_safe_report_obj(_summarize_loadtracks(result)), ensure_ascii=False, indent=2))
    lines.append(
        "\nSpotify direto via spsearch: omitido. No fluxo atual, Spotify é resolvido pela API do bot "
        "e depois espelhado pelo mirror configurado; se o application.yml estiver com spotify:false, spsearch vazio é esperado."
    )
    lines.append(
        "\nYouTube direto no Lavalink: omitido. No fluxo atual, YouTube fica fora do Lavalink e é validado no teste yt-dlp local com cookies."
    )
    return "\n".join(lines)

def _safe_report_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _safe_report_obj(_mask_value(k, v)) for k, v in obj.items() if k != "raw_password"}
    if isinstance(obj, list):
        return [_safe_report_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def _run_yt_dlp_quick(args: list[str], *, timeout: float = 35.0) -> str:
    """Executa yt-dlp e encerra assim que título+duração aparecerem.

    Alguns builds continuam fazendo trabalho de rede mesmo depois de imprimir as
    duas linhas úteis; para diagnóstico, duas linhas já bastam.
    """
    started = time.monotonic()
    lines: list[str] = [f"$ {' '.join(args)}"]
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        captured: list[str] = []
        assert proc.stdout is not None
        while True:
            if time.monotonic() - started > timeout:
                break
            line = proc.stdout.readline()
            if line:
                captured.append(line.rstrip("\n"))
                useful = [x for x in captured if x.strip() and not x.startswith("[")]
                if len(useful) >= 2:
                    proc.terminate()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=3)
                    lines.append("exit=0 (saída útil coletada; processo encerrado pelo diagnóstico)")
                    lines.extend(captured)
                    return "\n".join(lines)
                continue
            rc = proc.poll()
            if rc is not None:
                lines.append(f"exit={rc}")
                lines.extend(captured)
                return "\n".join(lines)
            time.sleep(0.05)
        if proc and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=3)
        lines.append(f"TIMEOUT após {timeout}s")
        if 'captured' in locals():
            lines.extend(captured)
        return "\n".join(lines)
    except Exception as exc:
        if proc and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
        return f"$ {' '.join(args)}\nERRO: {type(exc).__name__}: {exc}"


def _yt_dlp_test() -> str:
    cookie_candidates = [
        os.getenv("MUSIC_YTDLP_COOKIES_FILE"),
        os.getenv("YTDLP_COOKIES_FILE"),
        os.getenv("YT_DLP_COOKIES_FILE"),
        str(REPO_ROOT / "cookies.txt"),
    ]
    cookie_path = ""
    for candidate in cookie_candidates:
        if candidate and Path(candidate).exists():
            cookie_path = str(Path(candidate))
            break
    lines = []
    if cookie_path:
        path = Path(cookie_path)
        lines.append(f"cookies.txt: existe em {path} ({path.stat().st_size} bytes)")
    else:
        lines.append("cookies.txt: não encontrado pelos caminhos conhecidos")
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--get-title",
        "--get-duration",
        "-f",
        "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
    ]
    if cookie_path:
        args.extend(["--cookies", cookie_path])
    args.append(VALID_YOUTUBE_TEST_URL)
    lines.append(_run_yt_dlp_quick(args, timeout=14.0))
    return "\n".join(lines)

def _local_log_tail() -> str:
    note = "Observação: logs locais podem conter eventos pré-restart; confira a seção Marcos de restart/runtime para contextualizar horários.\n\n"
    log_dir = REPO_ROOT / "logs"
    if not log_dir.exists():
        return note + "Pasta logs/ não existe."
    parts: list[str] = []
    for path in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:4]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-180:])
            parts.append(f"===== {path.relative_to(REPO_ROOT)} =====\n{tail}")
        except Exception as exc:
            parts.append(f"===== {path} =====\nERRO: {type(exc).__name__}: {exc}")
    return note + ("\n\n".join(parts) if parts else "Nenhum .log em logs/.")


def _systemd_units_for_diagnostics() -> list[str]:
    return ["tts-bot.service", "lavalink.service", "callkeeper.service"]


def _node_process_inventory() -> str:
    """Mostra processos Node.js de features independentes, como Sinuca Activity."""
    ss_output = _run_cmd(["ss", "-ltnp"], timeout=8.0)
    lines: list[str] = []
    proc_root = Path("/proc")
    for item in sorted(proc_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not item.name.isdigit():
            continue
        pid = item.name
        try:
            cmdline = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            if not cmdline:
                continue
            exe = os.readlink(item / "exe")
        except Exception:
            continue
        combined = f"{exe} {cmdline}".lower()
        if "node" not in combined and "npm" not in combined:
            continue
        try:
            cwd = os.readlink(item / "cwd")
        except Exception:
            cwd = "?"
        combined_with_cwd = f"{combined} {cwd.lower()}"
        if "sinuca" in combined_with_cwd or "activity/sinuca-server" in combined_with_cwd:
            label = "Sinuca Activity"
        else:
            label = "Node.js (outro; não é backend de música)"
        listen = ""
        for ss_line in ss_output.splitlines():
            if f"pid={pid}," in ss_line or f"pid={pid})" in ss_line:
                listen = ss_line.strip()
                break
        suffix = f" | listen={listen}" if listen else ""
        lines.append(f"pid={pid} | tipo={label} | cwd={cwd} | cmd={cmdline}{suffix}")
    if not lines:
        return "Nenhum processo Node.js encontrado."
    return "Processos Node.js detectados:\n" + "\n".join(lines)



def _tts_runtime_snapshot(router: Any, guild_id: int) -> str:
    try:
        state = router.get_state(int(guild_id)) if router is not None and hasattr(router, "get_state") else None
    except Exception as exc:
        return f"Não consegui ler estado TTS/música: {type(exc).__name__}: {exc}"
    if state is None:
        return "Sem estado de música/TTS para esta guild."
    now = time.monotonic()
    data = {
        "current_backend": getattr(state, "current_backend", ""),
        "current_status": getattr(state, "current_status", ""),
        "current_track": getattr(getattr(state, "current", None), "title", ""),
        "tts_voice_touched": bool(getattr(state, "tts_voice_touched", False)),
        "last_tts_activity_age_s": round(max(0.0, now - float(getattr(state, "last_tts_activity_at", 0.0) or 0.0)), 2) if getattr(state, "last_tts_activity_at", 0.0) else None,
        "lavalink_tts_active_for_s": round(max(0.0, float(getattr(state, "lavalink_tts_until", 0.0) or 0.0) - now), 2),
        "lavalink_resume_grace_for_s": round(max(0.0, float(getattr(state, "lavalink_resume_grace_until", 0.0) or 0.0) - now), 2),
        "tts_session_active_for_s": round(max(0.0, float(getattr(state, "tts_session_active_until", 0.0) or 0.0) - now), 2),
        "tts_lavalink_failures": int(getattr(state, "tts_lavalink_failures", 0) or 0),
        "tts_session_last_error": str(getattr(state, "tts_session_last_error", "") or ""),
        "current_lavalink_player_present": getattr(state, "current_lavalink_player", None) is not None,
        "current_lavalink_playable_present": getattr(state, "current_lavalink_playable", None) is not None,
        "current_source_present": getattr(state, "current_source", None) is not None,
        "current_status_age_s": round(max(0.0, now - float(getattr(state, "current_status_changed_at", 0.0) or 0.0)), 2),
        "current_resolve_task_active": bool(getattr(state, "current_resolve_task", None) is not None and not getattr(state, "current_resolve_task", None).done()),
        "tts_public_base_url_configured": bool(str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip()),
        "tts_public_base_url": redact(str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip()),
        "tts_internal_base_url_configured": bool(str(getattr(config, "MUSIC_TTS_INTERNAL_BASE_URL", "") or "").strip()),
        "tts_internal_base_url": redact(str(getattr(config, "MUSIC_TTS_INTERNAL_BASE_URL", "") or "").strip()),
        "lavalink_tts_internal_first": bool(getattr(config, "MUSIC_LAVALINK_TTS_INTERNAL_FIRST", True)),
        "lavalink_tts_file_fallback": bool(getattr(config, "MUSIC_LAVALINK_TTS_FILE_FALLBACK", False)),
        "tts_audio_format": str(getattr(config, "MUSIC_TTS_AUDIO_FORMAT", "opus") or "opus"),
        "tts_audio_fallback_format": str(getattr(config, "MUSIC_TTS_AUDIO_FALLBACK_FORMAT", "mp3") or "mp3"),
        "tts_opus_bitrate": str(getattr(config, "MUSIC_TTS_OPUS_BITRATE", "48k") or "48k"),
        "tts_preroll_silence_ms": int(getattr(config, "MUSIC_TTS_PREROLL_SILENCE_MS", 140) or 0),
        "tts_postroll_silence_ms": int(getattr(config, "MUSIC_TTS_POSTROLL_SILENCE_MS", 180) or 0),
        "tts_fade_in_ms": int(getattr(config, "MUSIC_TTS_FADE_IN_MS", 45) or 0),
        "tts_fade_out_ms": int(getattr(config, "MUSIC_TTS_FADE_OUT_MS", 70) or 0),
        "tts_resume_seek_ahead_ms": int(getattr(config, "MUSIC_TTS_RESUME_SEEK_AHEAD_MS", 120) or 0),
        "tts_lavalink_volume_ramp_enabled": bool(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", True)),
        "tts_lavalink_volume_ramp_ms": int(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", 180) or 0),
        "tts_lavalink_ramp_floor_percent": int(getattr(config, "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", 5) or 0),
    }
    return json.dumps(_safe_report_obj(data), ensure_ascii=False, indent=2)

def _journalctl_commands(*, full: bool = False) -> list[list[str]]:
    if full:
        spec = [("tts-bot.service", "2 hours ago", "1200"), ("lavalink.service", "2 hours ago", "900"), ("callkeeper.service", "2 hours ago", "500")]
    else:
        spec = [("tts-bot.service", "8 minutes ago", "160"), ("lavalink.service", "8 minutes ago", "160")]
    return [["journalctl", "-u", unit, "--since", since, "-n", limit, "--no-pager", "-o", "cat"] for unit, since, limit in spec]


def _journalctl_tail() -> str:
    parts = []
    for cmd in _journalctl_commands(full=False):
        out = _run_cmd(cmd, timeout=6.0, cwd=REPO_ROOT)
        lines = out.splitlines()
        if len(lines) > 260:
            lines = lines[:3] + ["... (cortado) ..."] + lines[-250:]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _application_yml_head() -> str:
    path = Path("/opt/lavalink/application.yml")
    if not path.exists():
        return "/opt/lavalink/application.yml não existe ou não é acessível."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[:160]
        return "\n".join(f"{idx + 1}: {line}" for idx, line in enumerate(lines))
    except Exception as exc:
        return f"ERRO ao ler application.yml: {type(exc).__name__}: {exc}"



BASE_ARCHIVE_ROOT_NAME = "tts-bot-main"
BASE_ARCHIVE_MAX_BYTES = 23 * 1024 * 1024
# Limite por arquivo da base leve. A base do /vps é para análise/patch,
# não para transportar binários pesados toda vez.
BASE_ARCHIVE_MAX_FILE_BYTES = 1_250_000
BASE_ARCHIVE_SENSITIVE_NAMES = {
    ".env",
    "cookies.txt",
    "cookie.txt",
    "youtube-cookies.txt",
}
BASE_ARCHIVE_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

# A base gerada pelo /vps é enviada para análise de código, não para deploy.
# Mantemos ela leve pulando assets, binários, builds e manifestos gerados.
BASE_ARCHIVE_ASSET_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".flac",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
}
BASE_ARCHIVE_BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".aar",
    ".apk",
    ".apks",
    ".bin",
    ".class",
    ".dat",
    ".deb",
    ".dex",
    ".dll",
    ".dylib",
    ".elf",
    ".exe",
    ".gz",
    ".jar",
    ".lz4",
    ".lzma",
    ".o",
    ".onnx",
    ".pt",
    ".rpm",
    ".so",
    ".tar",
    ".tgz",
    ".wasm",
    ".xz",
    ".zip",
    ".zst",
}
BASE_ARCHIVE_BINARY_DIR_MARKERS = (
    "android/core-worker-app/app/src/main/jnilibs/",
    "android/core-worker-app/app/src/main/assets/core-linux/bin/",
    "android/core-worker-app/app/src/main/assets/core-linux/rootfs/",
    "android/core-worker-app/releases/",
    "android/core-worker-app/app/build/",
    "build/",
    "dist/",
)
BASE_ARCHIVE_ASSET_DIR_NAMES = {
    "assets",
    "audio",
    "fonts",
    "images",
    "media",
    "sounds",
    "sfx",
}
BASE_ARCHIVE_MANIFEST_NAMES = {
    "asset-manifest.json",
    "manifest.json",
    "manifest.webmanifest",
    "site.webmanifest",
}


def _git_cmd(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _is_sensitive_tracked_file(rel: str) -> bool:
    normalized = rel.replace("\\", "/").lstrip("/")
    name = Path(normalized).name.lower()
    lowered = normalized.lower()
    if name in BASE_ARCHIVE_SENSITIVE_NAMES:
        return True

    # .env real e variantes locais são sensíveis.
    # .env.example/.env.sample/.env.template são exemplos rastreados e devem ir no zip.
    allowed_env_examples = {".env.example", ".env.sample", ".env.template"}
    if name.startswith(".env") and name not in allowed_env_examples:
        return True

    if lowered.endswith(BASE_ARCHIVE_SENSITIVE_SUFFIXES):
        return True
    # Banco/log/cookies não deveriam estar rastreados, mas se estiverem, não anexa no Discord.
    if lowered.endswith((".sqlite", ".sqlite3", ".db", ".log")):
        return True
    if "cookies" in lowered and lowered.endswith(".txt"):
        return True
    return False


def _base_archive_skip_reason(rel: str, *, size: int | None = None) -> str | None:
    normalized = rel.replace("\\", "/").lstrip("/")
    lowered = normalized.lower()
    parts = [part.lower() for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    suffixes = [suffix.lower() for suffix in Path(name).suffixes]
    suffix = suffixes[-1] if suffixes else ""

    if name in BASE_ARCHIVE_MANIFEST_NAMES:
        return "manifesto"
    if len(parts) >= 2 and parts[-2] == ".vite" and name == "manifest.json":
        return "manifesto"
    if suffix in BASE_ARCHIVE_ASSET_EXTENSIONS:
        return "asset"
    if suffix in BASE_ARCHIVE_BINARY_EXTENSIONS:
        return "binário"
    if len(suffixes) >= 2 and "".join(suffixes[-2:]) in {".tar.gz", ".tar.xz", ".tar.zst"}:
        return "binário"

    # Diretórios de mídia/binários ficam fora mesmo quando o arquivo não tem extensão.
    # Isso evita mandar Box64, libs nativas, APKs publicados, rootfs e assets pesados
    # toda vez que o dono pede a base Git leve no /vps.
    if any(part in BASE_ARCHIVE_ASSET_DIR_NAMES for part in parts):
        return "asset"
    if any(lowered.startswith(marker) or f"/{marker}" in lowered for marker in BASE_ARCHIVE_BINARY_DIR_MARKERS):
        return "binário"
    if "dist" in parts and any(part in {"assets", "audio", "images", "media"} for part in parts):
        return "asset"
    if "public" in parts and any(part in {"audio", "images", "assets", "media"} for part in parts):
        return "asset"
    if size is not None and size > BASE_ARCHIVE_MAX_FILE_BYTES:
        return f"arquivo grande ({size / (1024 * 1024):.1f} MB)"
    return None


def _is_base_archive_asset_or_manifest(rel: str) -> bool:
    return _base_archive_skip_reason(rel) is not None


def build_git_tracked_base_archive_sync() -> tuple[bytes | None, str, str, str]:
    """Cria um zip com os arquivos rastreados pelo git no estado atual do disco.

    Usa `git ls-files`, então pega apenas arquivos rastreados pelo repositório,
    mas com o conteúdo atual da VPS, inclusive mudanças ainda não commitadas.
    Arquivos sensíveis, assets, binários pesados e manifestos gerados são pulados.
    A base do /vps deve ser leve e voltada para análise de código; por isso
    nenhum manifesto separado é retornado/anexado junto da base.
    """
    stamp = diagnostics_file_stamp()
    filename = f"repo-{stamp}.zip"

    try:
        root_check = _git_cmd(["rev-parse", "--show-toplevel"], timeout=8.0)
    except Exception as exc:
        return None, filename, f"Não consegui executar git: {type(exc).__name__}: {exc}", ""

    if root_check.returncode != 0:
        return None, filename, "Repo não parece ter .git acessível; não foi possível gerar a base rastreada pelo Git.", ""

    ls = _git_cmd(["ls-files", "-z"], timeout=20.0)
    if ls.returncode != 0:
        return None, filename, f"git ls-files falhou: {redact(ls.stderr or ls.stdout)}", ""

    rels = [item for item in ls.stdout.split("\0") if item]
    if not rels:
        return None, filename, "git ls-files não retornou arquivos rastreados.", ""

    status = _git_cmd(["status", "--short"], timeout=12.0)
    commit = _git_cmd(["rev-parse", "HEAD"], timeout=8.0)
    branch = _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], timeout=8.0)

    skipped: list[str] = []
    added = 0
    bio = io.BytesIO()
    try:
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            manifest_lines = [
                "Base gerada pelo /vps",
                f"Gerado em: {_now_stamp()}",
                f"Repo root: {REPO_ROOT}",
                f"Branch: {(branch.stdout or '').strip() if branch.returncode == 0 else 'desconhecida'}",
                f"Commit HEAD: {(commit.stdout or '').strip() if commit.returncode == 0 else 'desconhecido'}",
                "Conteúdo: arquivos retornados por `git ls-files`, usando o conteúdo atual do disco.",
                "Arquivos sensíveis, assets, binários pesados e manifestos gerados são pulados.",
                "",
                "# git status --short",
                (status.stdout or "limpo").rstrip() if status.returncode == 0 else redact(status.stderr or status.stdout),
                "",
                "# arquivos pulados",
            ]

            for rel in rels:
                safe_rel = rel.replace("\\", "/").lstrip("/")
                if not safe_rel or safe_rel.startswith("../") or "/../" in safe_rel:
                    skipped.append(rel)
                    continue
                if _is_sensitive_tracked_file(safe_rel):
                    skipped.append(f"{safe_rel} (sensível)")
                    continue
                src = REPO_ROOT / safe_rel
                if not src.is_file():
                    skipped.append(f"{safe_rel} (não é arquivo regular)")
                    continue
                try:
                    size = src.stat().st_size
                except OSError:
                    size = None
                skip_reason = _base_archive_skip_reason(safe_rel, size=size)
                if skip_reason:
                    skipped.append(f"{safe_rel} ({skip_reason})")
                    continue
                zf.write(src, f"{BASE_ARCHIVE_ROOT_NAME}/{safe_rel}")
                added += 1

            manifest_lines.extend(skipped or ["nenhum"])
            manifest_lines.extend(["", f"# total de arquivos anexados: {added}"])
            manifest_text = redact("\n".join(manifest_lines)) + "\n"
    except Exception as exc:
        return None, filename, f"Falha ao montar zip da base: {type(exc).__name__}: {exc}", ""

    payload = bio.getvalue()
    if len(payload) > BASE_ARCHIVE_MAX_BYTES:
        size_mb = len(payload) / (1024 * 1024)
        return None, filename, f"Base zip ficou grande demais para anexar com segurança no Discord: {size_mb:.1f} MB.", ""

    summary = f"Repositório leve anexado ({len(payload)} bytes)."
    return payload, filename, summary, ""


async def build_git_tracked_base_archive() -> tuple[bytes | None, str, str, str]:
    return await asyncio.to_thread(build_git_tracked_base_archive_sync)


def build_music_diagnostics_report_sync(router: Any, options: DiagnosticsOptions) -> str:
    sections: list[tuple[str, str]] = []
    sections.append((
        "Resumo",
        "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Guild: {options.guild_name} ({options.guild_id})",
            f"Solicitado por: {options.requester_name} ({options.requester_id})",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Sistema: {platform.platform()}",
            f"Repo root: {REPO_ROOT}",
        ]),
    ))
    sections.append((
        "Pacotes",
        "\n".join([
            f"discord.py: {_package_version('discord.py')}",
            f"wavelink: {_package_version('wavelink')}",
            f"yt-dlp: {_package_version('yt-dlp')}",
            f"PyNaCl: {_package_version('PyNaCl')}",
            f"aiohttp: {_package_version('aiohttp')}",
        ]),
    ))
    sections.append(("Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)))
    db_text, _ = _db_snapshot()
    sections.append(("DB musicnode", db_text))
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    sections.append(("Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)))
    sections.append(("Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)))
    sections.append(("Teste Spotify API do bot", _spotify_api_test()))
    sections.append(("Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)))
    sections.append(("Testes Lavalink REST", _lavalink_tests(cfg)))
    sections.append(("Marcos de restart/runtime", _service_restart_markers()))
    sections.append(("Core Worker APK — diagnóstico interno", build_core_worker_apk_diagnostics_report_sync()))
    sections.append(("Teste yt-dlp local com cookies", _yt_dlp_test()))
    sections.append(("application.yml do Lavalink (sanitizado)", _application_yml_head()))
    if options.include_local_logs:
        sections.append(("Logs locais do bot", _local_log_tail()))
    if options.include_journalctl:
        sections.append(("journalctl recente", _journalctl_tail()))

    body_parts: list[str] = []
    for title, body in sections:
        body_parts.append(f"\n\n# {title}\n{redact(body)}")
    report = "".join(body_parts).strip() + "\n"
    # Mantém o anexo menor para não bater limite do Discord.
    max_chars = 1_800_000
    if len(report) > max_chars:
        report = report[:max_chars] + "\n\n[relatório cortado por tamanho]\n"
    return redact(report)


async def build_music_diagnostics_report(router: Any, options: DiagnosticsOptions) -> str:
    return await asyncio.to_thread(build_music_diagnostics_report_sync, router, options)


def _music_diagnostics_sections(router: Any, options: DiagnosticsOptions) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """Monta seções reutilizáveis para relatório texto e pacote modular."""
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    db_text, db_data = _db_snapshot()
    summary = "\n".join([
        f"Gerado em: {_now_stamp()}",
        f"Guild: {options.guild_name} ({options.guild_id})",
        f"Solicitado por: {options.requester_name} ({options.requester_id})",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Sistema: {platform.platform()}",
        f"Repo root: {REPO_ROOT}",
    ])
    package_versions = "\n".join([
        f"discord.py: {_package_version('discord.py')}",
        f"wavelink: {_package_version('wavelink')}",
        f"yt-dlp: {_package_version('yt-dlp')}",
        f"PyNaCl: {_package_version('PyNaCl')}",
        f"aiohttp: {_package_version('aiohttp')}",
    ])
    sections: list[tuple[str, str, str]] = [
        ("00-resumo.txt", "Resumo", summary),
        ("01-pacotes.txt", "Pacotes", package_versions),
        ("02-env-relevante.json", "Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)),
        ("03-db-musicnode.json", "DB musicnode", db_text),
        ("04-lavalink-config-efetiva.json", "Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)),
        ("05-runtime-tts-musica.json", "Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)),
        ("tests/spotify-api.txt", "Teste Spotify API do bot", _spotify_api_test()),
        ("tests/spotify-mirror-dry-run.txt", "Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)),
        ("tests/lavalink-rest.txt", "Testes Lavalink REST", _lavalink_tests(cfg)),
        ("tests/ytdlp-local.txt", "Teste yt-dlp local com cookies", _yt_dlp_test()),
        ("system/restart-markers.txt", "Marcos de restart/runtime", _service_restart_markers()),
        ("lavalink/application-sanitized.yml", "application.yml do Lavalink (sanitizado)", _application_yml_head()),
    ]
    return sections, {"cfg": cfg, "db": db_data}


def _music_diagnostics_summary_text(sections: list[tuple[str, str, str]]) -> str:
    """Resumo textual que continua útil mesmo quando o zip é o anexo principal."""
    wanted = {
        "Resumo",
        "Pacotes",
        "Estado TTS/música em memória",
        "Teste Spotify API do bot",
        "Dry-run Spotify mirror/fallback (sem tocar áudio)",
        "Testes Lavalink REST",
        "Teste yt-dlp local com cookies",
    }
    body_parts: list[str] = []
    for _arc, title, body in sections:
        if title in wanted:
            body_parts.append(f"\n\n# {title}\n{redact(body)}")
    return ("".join(body_parts).strip() + "\n") if body_parts else "Diagnóstico musical gerado em pacote modular.\n"


def _diagnostic_log_commands() -> dict[str, list[str]]:
    return {
        "logs/relevant/music-events.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '90 minutes ago' -n 1200 --no-pager -o cat "
            "| grep -Ei 'music|lavalink|spotify|soundcloud|youtube|yt-dlp|fallback|premature|TrackException|LoadException|tts_|tts |duck|resolve|resolving|FFmpeg|erro|falhou|timeout|exception|traceback' || true",
        ],
        "logs/relevant/tts-events.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '90 minutes ago' -n 1000 --no-pager -o cat "
            "| grep -Ei 'tts|tts_voice|tts-audio|duck|lavalink_tts|public_url|internal_url|voice.*assumindo|timeout|falhou|erro' || true",
        ],
        "logs/relevant/errors-warnings.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service -u lavalink.service --since '90 minutes ago' -n 1200 --no-pager -o cat "
            "| grep -Ei 'warning|error|exception|traceback|falhou|erro|timeout|TrackException|LoadException|stuck|premature|invalid status|404|403|429|5[0-9][0-9]' || true",
        ],
        "logs/relevant/lavalink-events.txt": [
            "bash", "-lc",
            "journalctl -u lavalink.service --since '90 minutes ago' -n 900 --no-pager -o cat "
            "| grep -Ei 'ready|lavasrc|spotify|soundcloud|deezer|youtube|loadtracks|track|exception|failed|error|404|403|429|5[0-9][0-9]' || true",
        ],
        "logs/raw/tts-bot-journal-tail.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '90 minutes ago' -n 1600 --no-pager -o short-iso",
        ],
        "logs/raw/lavalink-journal-tail.txt": [
            "bash", "-lc",
            "journalctl -u lavalink.service --since '90 minutes ago' -n 1200 --no-pager -o short-iso",
        ],
    }


def _local_logs_archive_text() -> dict[str, str]:
    result: dict[str, str] = {}
    log_dir = REPO_ROOT / "logs"
    candidates: list[Path] = []
    if log_dir.exists():
        with contextlib.suppress(Exception):
            candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True))
    for extra in [REPO_ROOT / "bot.log", REPO_ROOT / "logs" / "bot.log", REPO_ROOT / "logs" / "updater.log"]:
        if extra.exists() and extra not in candidates:
            candidates.append(extra)
    for path in candidates[:12]:
        try:
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else Path(path.name)
        except Exception:
            rel = Path(path.name)
        arc = "logs/local/" + str(rel).replace(os.sep, "_")
        result[arc] = _safe_read_file(path, max_chars=900_000)
    if not result:
        result["logs/local/sem-logs.txt"] = "Nenhum arquivo de log local encontrado.\n"
    return result


MUSIC_DIAGNOSTICS_ARCHIVE_MAX_BYTES = 24 * 1024 * 1024


def build_music_diagnostics_emergency_report_sync(router: Any, options: DiagnosticsOptions, *, reason: str = "") -> str:
    """Relatório mínimo para quando o diagnóstico modular estourar timeout/falhar.

    A ideia é nunca deixar o /vps preso em "pensando". Este relatório evita
    testes REST longos e coleta só o essencial para debug imediato.
    """
    sections: list[tuple[str, str]] = []
    sections.append((
        "Resumo emergencial",
        "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Guild: {options.guild_name} ({options.guild_id})",
            f"Solicitado por: {options.requester_name} ({options.requester_id})",
            f"Repo root: {REPO_ROOT}",
            f"Motivo do fallback: {reason or 'diagnóstico principal indisponível'}",
        ]),
    ))
    sections.append(("Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)))
    with contextlib.suppress(Exception):
        sections.append(("Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)))
    sections.append(("Marcos de restart/runtime", _service_restart_markers()))
    sections.append(("Core Worker APK — diagnóstico interno", build_core_worker_apk_diagnostics_report_sync()))
    sections.append((
        "journalctl musical recente",
        _run_cmd([
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '45 minutes ago' -n 900 --no-pager -o cat "
            "| grep -Ei 'music|youtube|yt-dlp|lavalink|spotify|soundcloud|fallback|stream|format|cookie|anti-bot|Sign in|ffmpeg|voice|erro|falhou|exception|traceback|heartbeat|vps|diagn' || true",
        ], timeout=8.0, cwd=REPO_ROOT),
    ))
    sections.append((
        "journalctl lavalink recente",
        _run_cmd([
            "bash", "-lc",
            "journalctl -u lavalink.service --since '45 minutes ago' -n 700 --no-pager -o cat "
            "| grep -Ei 'ready|lavasrc|spotify|soundcloud|youtube|loadtracks|track|exception|failed|error|404|403|429|5[0-9][0-9]' || true",
        ], timeout=8.0, cwd=REPO_ROOT),
    ))
    sections.append(("Processos e recursos", _run_cmd(["bash", "-lc", "date -Is; echo; free -m; echo; ps -eo pid,ppid,%cpu,%mem,etime,cmd --sort=-%cpu | head -35"], timeout=6.0, cwd=REPO_ROOT)))
    body = "".join(f"\n\n# {title}\n{redact(text)}" for title, text in sections).strip() + "\n"
    return redact(body)


async def build_music_diagnostics_emergency_report(router: Any, options: DiagnosticsOptions, *, reason: str = "") -> str:
    return await asyncio.to_thread(build_music_diagnostics_emergency_report_sync, router, options, reason=reason)


def build_music_diagnostics_archive_sync(router: Any, options: DiagnosticsOptions) -> tuple[bytes | None, str, str, str]:
    """Gera diagnóstico musical em zip modular, usando phone-worker quando disponível."""
    stamp = diagnostics_file_stamp()
    filename = f"music-diag-{stamp}.zip"
    summary_text = "Diagnóstico musical gerado em pacote modular.\n"
    files: list[tuple[str, str]] = []
    try:
        cleanup_note = cleanup_music_diagnostics_temp_artifacts()
        sections, _meta = _music_diagnostics_sections(router, options)
        summary_text = _music_diagnostics_summary_text(sections)
        worker_note = _phone_worker_health_summary(timeout=2.5)

        files.extend([
            ("README.txt", "Diagnóstico musical modular. O resumo fica em 00-resumo-curto.txt e summary.txt; logs brutas ficam em logs/raw/.\n"),
            ("00-resumo-curto.txt", summary_text),
            ("summary.txt", summary_text),
            ("system/diagnostic-temp-cleanup.txt", cleanup_note),
            ("system/phone-worker.txt", worker_note + "\n"),
        ])
        for arc, _title, body in sections:
            files.append((arc, body))
        files.append(("bot/env.sanitized.txt", _sanitized_env_text()))
        collected_log_texts: list[str] = []
        for arc, cmd in _diagnostic_log_commands().items():
            timeout = 10.0 if "/raw/" in arc else 8.0
            text = _run_cmd(cmd, timeout=timeout)
            files.append((arc, text))
            collected_log_texts.append(f"# {arc}\n{text}")
        for arc, text in _local_logs_archive_text().items():
            files.append((arc, text))
            collected_log_texts.append(f"# {arc}\n{text}")
        if collected_log_texts:
            combined_logs = "\n\n".join(collected_log_texts)
            files.append(("logs/phone-worker/error-highlights.txt", _phone_worker_log_extract_text(combined_logs, source="diagnóstico musical")))
            files.append(("logs/phone-worker/text-stats.txt", _phone_worker_text_stats_summary(combined_logs, source="diagnóstico musical")))
            files.append(("logs/phone-worker/log-summary.txt", _phone_worker_log_summary_text(combined_logs, source="diagnóstico musical")))
        # Informações úteis para diagnosticar peso/IO sem tornar o relatório síncrono demais.
        files.append(("system/disk-and-process.txt", _run_cmd(["bash", "-lc", "df -h; echo; free -m; echo; ps -eo pid,ppid,%cpu,%mem,etime,cmd --sort=-%cpu | head -40"], timeout=8.0)))

        payload, worker_status = _phone_worker_zip_texts(files, filename=filename, compresslevel=6)
        if payload is None:
            logger.info("[phone-worker] diagnóstico musical compactado localmente | motivo=%s", worker_status)
            files.append(("system/phone-worker-fallback.txt", worker_status + "\nCompactação feita localmente na VPS.\n"))
            payload = _zip_texts_local(files, compresslevel=6)
        else:
            logger.info("[phone-worker] diagnóstico musical compactado no celular")
            files.append(("system/phone-worker-zip.txt", worker_status + "\nCompactação feita no celular.\n"))
    except Exception as exc:
        return None, filename, f"Falha ao montar diagnóstico musical modular: {type(exc).__name__}: {exc}", summary_text

    summary = f"Diagnóstico musical anexado ({len(payload)} bytes)."
    if len(payload) > MUSIC_DIAGNOSTICS_ARCHIVE_MAX_BYTES:
        return None, filename, f"Diagnóstico musical modular ficou grande demais para anexar: {len(payload) / (1024 * 1024):.1f} MB.", summary_text
    # Sucesso modular: o resumo já está dentro do zip. Não retorne fallback_report,
    # para o comando /vps não anexar um segundo arquivo de resumo.
    return payload, filename, summary, ""


async def build_music_diagnostics_archive(router: Any, options: DiagnosticsOptions) -> tuple[bytes | None, str, str, str]:
    return await asyncio.to_thread(build_music_diagnostics_archive_sync, router, options)


def _local_log_tail_full() -> str:
    note = "Observação: logs locais podem conter eventos pré-restart; confira a seção Marcos de restart/runtime para contextualizar horários.\n\n"
    log_dir = REPO_ROOT / "logs"
    parts: list[str] = []

    candidates: list[Path] = []
    if log_dir.exists():
        with contextlib.suppress(Exception):
            candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True))
    for extra in [REPO_ROOT / "bot.log", REPO_ROOT / "logs" / "bot.log", REPO_ROOT / "logs" / "updater.log"]:
        if extra.exists() and extra not in candidates:
            candidates.append(extra)

    if not candidates:
        return note + "Nenhum arquivo de log local encontrado."

    for path in candidates[:12]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-1200:])
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
            parts.append(f"===== {rel} =====\n{tail}")
        except Exception as exc:
            parts.append(f"===== {path} =====\nERRO: {type(exc).__name__}: {exc}")
    return note + "\n\n".join(parts)


def _journalctl_full_tail() -> str:
    parts: list[str] = []
    for cmd in _journalctl_commands(full=True):
        out = _run_cmd(cmd, timeout=18.0, cwd=REPO_ROOT)
        lines = out.splitlines()
        if len(lines) > 900:
            lines = lines[:4] + ["... (cortado) ..."] + lines[-880:]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)



def _run_cmd_body(args: list[str], *, timeout: float = 8.0, cwd: Path | None = None, max_chars: int = 4000) -> str:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd or REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        body = (cp.stdout or "").strip()
        if cp.returncode != 0 and not body:
            body = f"exit={cp.returncode}"
        if len(body) > max_chars:
            body = body[:max_chars] + "\n[cortado por tamanho]"
        return redact(body)
    except subprocess.TimeoutExpired as exc:
        body = exc.stdout or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return redact((body.strip() or f"TIMEOUT após {timeout}s")[:max_chars])
    except Exception as exc:
        return f"ERRO: {type(exc).__name__}: {exc}"


def _system_status_report() -> str:
    units = _systemd_units_for_diagnostics()
    parts = [
        _run_cmd(["date", "-Is"], timeout=5.0),
        _run_cmd(["hostname"], timeout=5.0),
        _run_cmd(["uname", "-a"], timeout=5.0),
        _run_cmd(["df", "-h", "/", "/opt", "/home"], timeout=8.0),
        _run_cmd(["free", "-h"], timeout=8.0),
        _run_cmd(["ss", "-ltnp"], timeout=10.0),
        _node_process_inventory(),
        _run_cmd(["systemctl", "--no-pager", "--full", "status", *units], timeout=8.0),
        _run_cmd(["systemctl", "cat", *units], timeout=8.0),
    ]
    parts.append("Backends de música ativos: Lavalink e fallback local/yt-dlp. Processos Node.js em outras portas são features independentes, como Sinuca Activity; veja o inventário acima.")
    return "\n\n".join(parts)


def _first_nonempty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def _compact_uptime(raw: str) -> str:
    text = _first_nonempty_line(raw).removeprefix("up ").strip()
    if not text:
        return "indisponível"
    replacements = [
        (" days", "d"), (" day", "d"),
        (" hours", "h"), (" hour", "h"),
        (" minutes", "min"), (" minute", "min"),
        (" seconds", "s"), (" second", "s"),
        (",", ""),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text.strip() or "indisponível"


def _pretty_space_size(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    raw = raw.replace("Gi", " GB").replace("Mi", " MB").replace("Ki", " KB")
    raw = re.sub(r"(?<=\d)([KMGTP])$", r" \1B", raw)
    raw = re.sub(r"(?<=\d)([KMGTP])(?=\s|$)", r" \1B", raw)
    return raw.replace("  ", " ")


def _quick_memory_line() -> str:
    raw = _run_cmd_body(["bash", "-lc", "free -m | awk '/^Mem:/ {print $3, $2}'"], timeout=5.0, max_chars=80)
    parts = _first_nonempty_line(raw).split()
    if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
        return f"{int(parts[0])} MB / {int(parts[1])} MB"
    return "indisponível"


def _quick_disk_line() -> str:
    raw = _run_cmd_body(["bash", "-lc", "df -h / | awk 'NR==2 {print $3, $2, $5}'"], timeout=5.0, max_chars=120)
    parts = _first_nonempty_line(raw).split()
    if len(parts) >= 3:
        used, total, pct = parts[:3]
        return f"{_pretty_space_size(used)} / {_pretty_space_size(total)} — {pct} usado"
    return "indisponível"


def _quick_folder_lines() -> list[str]:
    labels = {
        ".": "Bot",
        "activity": "Painel web",
        "logs": "Logs",
        "tmp_audio": "Temp áudio",
        ".venv": ".venv",
    }
    cmd = "for p in . activity logs tmp_audio .venv; do [ -e \"$p\" ] && du -sh \"$p\"; done"
    raw = _run_cmd_body(["bash", "-lc", cmd], timeout=8.0, max_chars=500)
    values: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        size, path = parts[0], parts[1].strip()
        label = labels.get(path)
        if label:
            values[label] = _pretty_space_size(size)

    primary = [f"{label}: {values[label]}" for label in ("Bot", "Painel web", ".venv") if label in values]
    secondary = [f"{label}: {values[label]}" for label in ("Logs", "Temp áudio") if label in values]
    result: list[str] = []
    if primary:
        result.append(" · ".join(primary))
    if secondary:
        result.append(" · ".join(secondary))
    return result or ["sem dados"]

def _quick_service_lines() -> list[str]:
    labels = {
        "tts-bot.service": "Bot",
        "lavalink.service": "Lavalink",
        "callkeeper.service": "CallKeeper",
    }
    names = {
        "active": "ativo",
        "inactive": "parado",
        "failed": "falhou",
        "activating": "iniciando",
        "deactivating": "parando",
        "unknown": "desconhecido",
    }
    states: list[tuple[str, str]] = []
    for unit in _systemd_units_for_diagnostics():
        raw = _run_cmd_body(["systemctl", "is-active", unit], timeout=4.0, max_chars=80)
        state = _first_nonempty_line(raw).lower() or "unknown"
        states.append((labels.get(unit, unit), names.get(state, state)))

    if states and all(state == "ativo" for _, state in states):
        names_only = [name for name, _ in states]
        if len(names_only) >= 3:
            return [", ".join(names_only[:-1]) + f" e {names_only[-1]} ativos."]
        return [" e ".join(names_only) + " ativos."]
    return [" · ".join(f"{name}: {state}" for name, state in states)] or ["sem dados"]

def _quick_git_lines() -> list[str]:
    branch = _first_nonempty_line(_run_cmd_body(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0, max_chars=120)) or "desconhecida"
    commit = _first_nonempty_line(_run_cmd_body(["git", "log", "-1", "--pretty=%h %s"], timeout=5.0, max_chars=220)) or "desconhecido"
    if len(commit) > 72:
        commit = commit[:69].rstrip() + "..."
    return [f"{branch} · {commit}"]


def _format_journal_time(raw_time: str) -> str:
    text = str(raw_time or "").strip()
    if not text:
        return datetime.now(_diagnostics_timezone()).strftime("%d/%m %H:%M")
    # journalctl -o short-iso costuma emitir: 2026-05-14T16:21:03+0000
    normalized = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-5] + normalized[-5:-2] + ":" + normalized[-2:]
    with contextlib.suppress(Exception):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(_diagnostics_timezone()).strftime("%d/%m %H:%M")
    return text[:16]


def _clean_journal_error_message(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    timestamp = ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(.*)$", text)
    if match:
        timestamp = match.group(1)
        text = match.group(2).strip()

    # Remove prefixos comuns do journal: host process[pid]: mensagem
    text = re.sub(r"^[\w.\-]+\s+", "", text, count=1)
    text = re.sub(r"^[\w@./+\-]+(?:\[\d+\])?:\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return timestamp, text or "erro sem mensagem"


def _quick_recent_errors_lines() -> list[str]:
    pattern = re.compile(r"critical|error|exception|traceback|falhou|erro|timeout", re.IGNORECASE)
    raw = _run_cmd_body(
        ["journalctl", "-u", "tts-bot.service", "--since", "2 hours ago", "-n", "260", "--no-pager", "-o", "short-iso"],
        timeout=10.0,
        max_chars=8000,
    )

    # O phone-worker ajuda aqui só como acelerador/analisador curto. Se ele
    # estiver offline, o fallback local abaixo continua instantâneo e seguro.
    if (
        raw.strip()
        and _env_bool("PHONE_WORKER_ENABLED", False)
        and _env_bool("PHONE_WORKER_QUICK_STATUS_ENABLED", True)
        and _phone_worker_base_url()
    ):
        try:
            worker_timeout = max(0.8, _env_float("PHONE_WORKER_QUICK_STATUS_TIMEOUT_SECONDS", 1.2))
            summary = _phone_worker_request_json(
                "/task",
                payload={"task": "log_summary", "text": raw, "max_recent": 3, "max_top": 3},
                timeout=worker_timeout,
            )
            if summary.get("ok"):
                recent = [str(line) for line in (summary.get("recent") or []) if str(line).strip()]
                if recent:
                    out: list[str] = []
                    for line in recent[-3:]:
                        raw_time, message = _clean_journal_error_message(line)
                        when = _format_journal_time(raw_time)
                        out.append(f"{when} · {message[:135].rstrip()}")
                    if out:
                        logger.info("[phone-worker] status rápido analisou erros recentes no celular")
                        return out
        except Exception as exc:
            logger.info("[phone-worker] status rápido usou fallback local para erros recentes | motivo=%s: %s", type(exc).__name__, str(exc)[:120])

    matches: list[str] = []
    for line in raw.splitlines():
        if pattern.search(line or ""):
            matches.append(line.strip())
    if not matches:
        return ["Nenhum erro recente encontrado."]

    result: list[str] = []
    for line in matches[-3:]:
        raw_time, message = _clean_journal_error_message(line)
        when = _format_journal_time(raw_time)
        message = message[:135].rstrip()
        result.append(f"{when} · {message}")
    return result or ["Nenhum erro recente encontrado."]


def _diag_load_json(rel: str) -> dict[str, Any]:
    try:
        path = REPO_ROOT / rel
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _diag_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _diag_float(value: Any, default: float = -1.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _diag_age_from_epoch(epoch: Any) -> str:
    try:
        seconds = max(0, int(time.time() - float(epoch or 0)))
    except Exception:
        return "sem horário"
    if seconds < 60:
        return f"há {seconds}s"
    if seconds < 3600:
        return f"há {seconds // 60}min"
    if seconds < 86400:
        return f"há {seconds // 3600}h"
    return f"há {seconds // 86400}d"


def _diag_short(text: Any, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", str(text or "").replace("\n", " ")).strip()
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"


def _core_worker_expected_apk_version() -> tuple[str, int]:
    path = REPO_ROOT / "android" / "core-worker-app" / "app" / "build.gradle"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        name = re.search(r'versionName\s+"([^"]+)"', text)
        code = re.search(r"versionCode\s+(\d+)", text)
        return (name.group(1) if name else "", int(code.group(1)) if code else 0)
    except Exception:
        return "", 0


def _core_worker_published_apk_version() -> tuple[str, int]:
    path = REPO_ROOT / "android" / "core-worker-app" / "releases" / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(data, dict):
            return str(data.get("versionName") or data.get("version") or ""), int(data.get("versionCode") or 0)
    except Exception:
        pass
    return "", 0


def _core_worker_fcm_diag_summary(latest_records: list[dict[str, Any]]) -> list[str]:
    data = _diag_load_json("data/core_worker_app_fcm_tokens.json")
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    if not tokens:
        return ["FCM: nenhum token registrado ainda."]
    runtime_by_install = {str(r.get("installId") or ""): r for r in latest_records if isinstance(r, dict) and r.get("installId")}
    runtime_by_worker = {str(r.get("workerId") or ""): r for r in latest_records if isinstance(r, dict) and r.get("workerId")}
    active = stale = invalidated = incomplete = 0
    current = 0
    last: dict[str, Any] | None = None
    for record in tokens.values():
        if not isinstance(record, dict):
            continue
        seen = _diag_int(record.get("lastSeenAt") or record.get("registeredAt"), 0)
        if last is None or seen > _diag_int(last.get("lastSeenAt") or last.get("registeredAt"), 0):
            last = record
        token = str(record.get("token") or "").strip()
        if len(token) < 20:
            incomplete += 1
            continue
        if str(record.get("lastErrorCode") or "").upper() == "UNREGISTERED" or record.get("invalidatedAt"):
            invalidated += 1
        if not record.get("active"):
            continue
        active += 1
        runtime = runtime_by_install.get(str(record.get("installId") or "")) or runtime_by_worker.get(str(record.get("workerId") or ""))
        runtime_code = _diag_int((runtime or {}).get("appVersionCode"), 0)
        token_code = _diag_int(record.get("appVersionCode"), 0)
        if runtime_code and token_code and token_code < runtime_code:
            stale += 1
        elif runtime_code and token_code <= 0:
            stale += 1
        else:
            current += 1
    lines = [f"FCM: {current} atual(is) · {active} ativo(s) · {stale} antigo(s) · {invalidated} expirado(s) · {incomplete} incompleto(s)."]
    if last:
        extra = []
        if last.get("appVersion") or last.get("appVersionCode"):
            extra.append(f"app {last.get('appVersion') or '?'} ({last.get('appVersionCode') or 0})")
        if last.get("permission"):
            extra.append(f"permissão {last.get('permission')}")
        if last.get("lastPushStatus"):
            extra.append(f"último push {last.get('lastPushStatus')}")
        if last.get("lastError"):
            extra.append(_diag_short(last.get("lastError"), 90))
        if extra:
            lines.append("FCM último: " + " · ".join(extra))
    return lines


def _core_worker_apk_latest_records() -> list[dict[str, Any]]:
    data = _diag_load_json("data/core_worker_app_heartbeats.json")
    latest = data.get("latestByWorkerId") if isinstance(data.get("latestByWorkerId"), dict) else {}
    records = [r for r in latest.values() if isinstance(r, dict)]
    if not records:
        latest_install = data.get("latestByInstallId") if isinstance(data.get("latestByInstallId"), dict) else {}
        records = [r for r in latest_install.values() if isinstance(r, dict)]
    records.sort(key=lambda item: _diag_int(item.get("receivedAt"), 0), reverse=True)
    return records


def build_core_worker_apk_diagnostics_report_sync() -> str:
    heartbeats = _diag_load_json("data/core_worker_app_heartbeats.json")
    jobs = _diag_load_json("data/core_worker_app_jobs.json")
    notifications = _diag_load_json("data/core_worker_app_notifications.json")
    records = _core_worker_apk_latest_records()
    expected_name, expected_code = _core_worker_expected_apk_version()
    published_name, published_code = _core_worker_published_apk_version()
    lines: list[str] = [
        "# 📲 Diagnóstico Core Worker APK",
        f"Gerado em: {_now_stamp()}",
        "Fonte: arquivos locais da VPS enviados pelo heartbeat direto do APK; sem ler segredos.",
        "",
        "## Versões",
        f"Código atual no repo: {expected_name or '?'} ({expected_code or 0})",
        f"Publicado na VPS: {published_name or 'sem latest.json'} ({published_code or 0})",
    ]
    if not records:
        lines.extend(["", "## Heartbeat", "Nenhum heartbeat direto do APK encontrado."])
    else:
        lines.extend(["", "## Heartbeat/estado atual"])
        for idx, record in enumerate(records[:6], 1):
            age = _diag_age_from_epoch(record.get("receivedAt"))
            online = "online" if _diag_int(record.get("receivedAt"), 0) and time.time() - _diag_int(record.get("receivedAt"), 0) <= 180 else "offline/antigo"
            version = f"{record.get('appVersion') or '?'} ({record.get('appVersionCode') or 0})"
            installed_state = "em dia"
            current_code = _diag_int(record.get("appVersionCode"), 0)
            target_code = published_code or expected_code
            if target_code and current_code and current_code < target_code:
                installed_state = f"atrasado: {current_code} < {target_code}"
            battery = []
            pct = _diag_int(record.get("batteryPercent"), -1)
            temp = _diag_float(record.get("batteryTemperatureC"), -1)
            if pct >= 0:
                battery.append(f"{pct}%")
            if temp >= 0:
                battery.append(f"{temp:.1f}°C")
            if record.get("batteryCharging"):
                battery.append("carregando")
            network = _diag_short(record.get("networkType") or "rede ?", 40)
            if record.get("networkVpn"):
                network += "+VPN"
            ping = _diag_int(record.get("vpsPingMs"), -1)
            if ping >= 0:
                network += f" · ping {ping}ms"
            lines.extend([
                f"### {idx}. {record.get('deviceName') or record.get('workerId') or record.get('installId') or 'APK'}",
                f"Estado: {online} · visto {age} · versão {version} · {installed_state}",
                f"Worker/instalação: {record.get('workerId') or '?'} / {record.get('installId') or '?'}",
                f"Perfil/runtime: {record.get('profile') or '?'} · {record.get('runtimeMode') or '?'} · {record.get('jobsRuntime') or '?'}",
                f"Bateria/rede: {' · '.join(battery) if battery else 'bateria ?'} · {network}",
                f"Push/update: {record.get('fcmState') or 'push ?'} · {record.get('updateState') or 'update ?'} · perm notif {record.get('notificationPermission') or '?'}",
                f"Runtime interno: {_diag_short(record.get('internalRuntimeState') or record.get('diagnosticsSummary'), 160)}",
                f"Cache/ponte: {_diag_short(record.get('storageSummary') or 'cache ?', 90)} · {_diag_short(record.get('bridgeSummary') or 'ponte ?', 90)}",
            ])
            if record.get("lastAppError"):
                lines.append(f"Último erro do app: {_diag_short(record.get('lastAppError'), 180)}")
    lines.extend(["", "## Jobs internos"])
    catalog = jobs.get("jobCatalog") if isinstance(jobs.get("jobCatalog"), dict) else {}
    if catalog:
        lines.append(f"Catálogo: {len(catalog.get('automatic') or [])} automáticos · {len(catalog.get('manual') or [])} manuais · {len(catalog.get('aliases') or {})} aliases.")
    summaries = jobs.get("summaryByInstallId") if isinstance(jobs.get("summaryByInstallId"), dict) else {}
    if not summaries:
        lines.append("Sem resumo de jobs internos ainda.")
    else:
        for key, info in list(summaries.items())[:8]:
            if not isinstance(info, dict):
                continue
            missing = info.get("autoMissing") if isinstance(info.get("autoMissing"), list) else []
            failed = info.get("failedTypes") if isinstance(info.get("failedTypes"), list) else []
            lines.append(f"- {key}: {info.get('status') or '?'} · auto {info.get('autoOk')}/{info.get('autoTotal')} · manuais {info.get('manualTotal')} · fila {info.get('pending')}/{info.get('running')} · faltam {len(missing)} · falhas {len(failed)}")
    latest = jobs.get("latestResultByInstallId") if isinstance(jobs.get("latestResultByInstallId"), dict) else {}
    if latest:
        lines.append("")
        lines.append("Últimos resultados:")
        for key, rec in list(latest.items())[:8]:
            if isinstance(rec, dict):
                lines.append(f"- {key}: {rec.get('type') or rec.get('jobClass') or '?'} · ok={bool(rec.get('ok'))} · app {rec.get('appVersion') or '?'} ({rec.get('appVersionCode') or 0}) · {_diag_short(rec.get('message') or rec.get('error'), 120)}")
    lines.extend(["", "## Push/FCM"])
    lines.extend(_core_worker_fcm_diag_summary(records))
    notif_events = notifications.get("events") if isinstance(notifications.get("events"), list) else []
    if notif_events:
        lines.append("")
        lines.append("Últimos eventos de notificação/update:")
        for event in notif_events[-5:]:
            if isinstance(event, dict):
                lines.append(f"- {event.get('state') or '?'} · v{event.get('versionName') or '?'} ({event.get('versionCode') or 0}) · {_diag_short(event.get('detail'), 120)}")
    return redact("\n".join(lines).strip() + "\n")


async def build_core_worker_apk_diagnostics_report() -> str:
    return await asyncio.to_thread(build_core_worker_apk_diagnostics_report_sync)


def _core_worker_apk_quick_lines() -> list[str]:
    records = _core_worker_apk_latest_records()
    if not records:
        return ["APK interno: sem heartbeat direto ainda."]
    record = records[0]
    age = _diag_age_from_epoch(record.get("receivedAt"))
    version = f"{record.get('appVersion') or '?'} ({record.get('appVersionCode') or 0})"
    battery = []
    pct = _diag_int(record.get("batteryPercent"), -1)
    temp = _diag_float(record.get("batteryTemperatureC"), -1)
    if pct >= 0:
        battery.append(f"{pct}%")
    if temp >= 0:
        battery.append(f"{temp:.0f}°C")
    network = _diag_short(record.get("networkType") or "rede ?", 24)
    if record.get("networkVpn"):
        network += "+VPN"
    ping = _diag_int(record.get("vpsPingMs"), -1)
    if ping >= 0:
        network += f" {ping}ms"
    jobs = _diag_load_json("data/core_worker_app_jobs.json")
    summaries = jobs.get("summaryByInstallId") if isinstance(jobs.get("summaryByInstallId"), dict) else {}
    install_id = str(record.get("installId") or "")
    summary = summaries.get(install_id) if isinstance(summaries.get(install_id), dict) else next((v for v in summaries.values() if isinstance(v, dict)), {})
    job_line = "jobs aguardando"
    if isinstance(summary, dict) and summary:
        job_line = f"jobs {summary.get('autoOk')}/{summary.get('autoTotal')} ok · {summary.get('manualTotal')} manuais · fila {summary.get('pending')}/{summary.get('running')}"
    push_line = _core_worker_fcm_diag_summary(records)[0]
    return [
        f"APK {version} · visto {age} · {record.get('profile') or 'perfil ?'}",
        f"{(' · '.join(battery) if battery else 'bateria ?')} · {network} · {job_line}",
        push_line,
    ]



def _safe_core_worker_app_json_snapshot(rel: str, *, max_chars: int = 500_000) -> str:
    """Lê JSON do APK para anexos de /vps sem expor tokens/secrets."""
    path = REPO_ROOT / rel
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        text = json.dumps(_safe_report_obj(data), ensure_ascii=False, indent=2)
    except Exception as exc:
        if Path(rel).name == "core_worker_app_fcm_tokens.json":
            text = f"ERRO ao sanitizar core_worker_app_fcm_tokens.json: {type(exc).__name__}: {exc}\nConteúdo bruto omitido por segurança.\n"
        else:
            text = _safe_read_file(path, max_chars=max_chars)
    return redact(text)[:max_chars]

def build_quick_vps_status_report_sync() -> str:
    """Resumo curto, legível e pronto para Components V2."""
    services = _quick_service_lines()
    uptime = _compact_uptime(_run_cmd_body(["uptime", "-p"], timeout=5.0, max_chars=200))
    ram = _quick_memory_line()
    disk = _quick_disk_line()
    folders = _quick_folder_lines()
    git_lines = _quick_git_lines()
    errors = _quick_recent_errors_lines()
    apk_lines = _core_worker_apk_quick_lines()

    sections = [
        "## ⚡ Status rápido",
        "",
        "### 🟢 Serviços",
        *services,
        "",
        "### 💾 VPS",
        f"RAM: {ram}",
        f"Disco: {disk}",
        f"Uptime: {uptime}",
        "",
        "### 📁 Pastas",
        *folders,
        "",
        "### 🌿 Git",
        *git_lines,
        "",
        "### 📲 Core Worker APK",
        *apk_lines,
        "",
        "### ⚠️ Erros recentes",
        *errors,
    ]
    report = "\n".join(sections).strip() + "\n"
    if len(report) > 2800:
        report = report[:2800] + "\n[cortado por tamanho]\n"
    return redact(report)


async def build_quick_vps_status_report() -> str:
    return await asyncio.to_thread(build_quick_vps_status_report_sync)


def build_full_vps_diagnostics_report_sync(router: Any, options: DiagnosticsOptions) -> str:
    sections: list[tuple[str, str]] = []
    sections.append((
        "Resumo",
        "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Guild: {options.guild_name} ({options.guild_id})",
            f"Solicitado por: {options.requester_name} ({options.requester_id})",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Sistema: {platform.platform()}",
            f"Repo root: {REPO_ROOT}",
            "Tipo: diagnóstico completo da VPS/bot",
        ]),
    ))
    sections.append((
        "Pacotes",
        "\n".join([
            f"discord.py: {_package_version('discord.py')}",
            f"wavelink: {_package_version('wavelink')}",
            f"yt-dlp: {_package_version('yt-dlp')}",
            f"PyNaCl: {_package_version('PyNaCl')}",
            f"aiohttp: {_package_version('aiohttp')}",
        ]),
    ))
    sections.append(("Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)))
    db_text, _ = _db_snapshot()
    sections.append(("DB musicnode", db_text))
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    sections.append(("Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)))
    sections.append(("Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)))
    sections.append(("Teste Spotify API do bot", _spotify_api_test()))
    sections.append(("Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)))
    sections.append(("Testes Lavalink REST", _lavalink_tests(cfg)))
    sections.append(("Marcos de restart/runtime", _service_restart_markers()))
    sections.append(("Core Worker APK — diagnóstico interno", build_core_worker_apk_diagnostics_report_sync()))
    sections.append(("Teste yt-dlp local com cookies", _yt_dlp_test()))
    sections.append(("application.yml do Lavalink (sanitizado)", _application_yml_head()))
    sections.append(("Status do sistema e services", _system_status_report()))
    local_logs_full = _local_log_tail_full()
    journal_full = _journalctl_full_tail()
    combined_full_logs = "\n\n# Logs locais completas/cortadas\n" + local_logs_full + "\n\n# journalctl completo/cortado\n" + journal_full
    sections.append(("Phone-worker: destaques de erros nas logs completas", _phone_worker_log_extract_text(combined_full_logs, source="diagnóstico completo")))
    sections.append(("Phone-worker: estatísticas das logs completas", _phone_worker_text_stats_summary(combined_full_logs, source="diagnóstico completo")))
    sections.append(("Phone-worker: resumo inteligente das logs completas", _phone_worker_log_summary_text(combined_full_logs, source="diagnóstico completo")))
    sections.append(("Logs locais completas/cortadas", local_logs_full))
    sections.append(("journalctl completo/cortado", journal_full))

    body_parts = [f"\n\n# {title}\n{redact(body)}" for title, body in sections]
    report = "".join(body_parts).strip() + "\n"
    max_chars = 1_900_000
    if len(report) > max_chars:
        report = report[:max_chars] + "\n\n[relatório completo cortado por tamanho]\n"
    return redact(report)


async def build_full_vps_diagnostics_report(router: Any, options: DiagnosticsOptions) -> str:
    return await asyncio.to_thread(build_full_vps_diagnostics_report_sync, router, options)


VPS_SNAPSHOT_MAX_BYTES = 24 * 1024 * 1024


def _write_zip_text(zf: zipfile.ZipFile, arcname: str, text: str) -> None:
    zf.writestr(arcname, redact(text if text is not None else ""))


def _sanitized_env_text() -> str:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return ".env não encontrado\n"
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            lines.append(raw)
            continue
        key, value = raw.split("=", 1)
        lines.append(f"{key}=***REDACTED***" if _mask_value(key, value) == "***REDACTED***" else raw)
    return "\n".join(lines) + "\n"


def _safe_read_file(path: Path, *, max_chars: int = 500_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[arquivo cortado por tamanho]\n"
        return text
    except Exception as exc:
        return f"ERRO ao ler {path}: {type(exc).__name__}: {exc}\n"


def build_vps_snapshot_archive_sync() -> tuple[bytes | None, str, str]:
    stamp = diagnostics_file_stamp()
    filename = f"vps-snapshot-{stamp}.zip"
    files: list[tuple[str, str]] = []

    try:
        files.append(("meta/summary.txt", "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Repo root: {REPO_ROOT}",
            f"Sistema: {platform.platform()}",
            "Snapshot sanitizado da VPS para diagnóstico.",
        ]) + "\n"))
        files.append(("meta/phone-worker.txt", _phone_worker_health_summary(timeout=2.5) + "\n"))
        files.append(("core-worker-apk/diagnostico-apk.txt", build_core_worker_apk_diagnostics_report_sync()))
        for rel in [
            "data/core_worker_app_heartbeats.json",
            "data/core_worker_app_jobs.json",
            "data/core_worker_app_notifications.json",
            "data/core_worker_app_fcm_tokens.json",
        ]:
            path = REPO_ROOT / rel
            if path.exists():
                arc_name = Path(rel).name
                if arc_name == "core_worker_app_fcm_tokens.json":
                    arc_name = "core_worker_app_fcm_tokens.sanitized.json"
                files.append(("core-worker-apk/" + arc_name, _safe_core_worker_app_json_snapshot(rel, max_chars=500_000)))

        files.append(("bot/env.sanitized.txt", _sanitized_env_text()))
        for rel in ["config.py", "requirements.txt", "cogs/music.py", "cogs/utility.py"]:
            path = REPO_ROOT / rel
            if path.exists():
                files.append((f"bot/{rel}", _safe_read_file(path)))
        for folder in ["music_system", "utility"]:
            root = REPO_ROOT / folder
            if root.exists():
                for path in sorted(root.rglob("*.py")):
                    try:
                        arc = f"bot/{path.relative_to(REPO_ROOT)}"
                    except Exception:
                        arc = f"bot/{path.name}"
                    files.append((arc, _safe_read_file(path, max_chars=250_000)))

        app_path = Path("/opt/lavalink/application.yml")
        files.append(("lavalink/application.sanitized.yml", _safe_read_file(app_path, max_chars=500_000)))
        files.append(("lavalink/listing.txt", _run_cmd(["bash", "-lc", "ls -lah /opt/lavalink; echo; ls -lah /opt/lavalink/plugins"], timeout=8.0)))

        files.append(("db/musicnode.snapshot.txt", _db_snapshot()[0]))
        files.append(("systemd/services.txt", _run_cmd(["systemctl", "cat", *_systemd_units_for_diagnostics()], timeout=8.0)))
        files.append(("meta/system.txt", _system_status_report()))
        snapshot_log_blocks: list[str] = []
        log_entries = [
            ("logs/tts-bot.filtered.log", _run_cmd(["bash", "-lc", "journalctl -u tts-bot.service --since '2 hours ago' -n 900 --no-pager -o cat | grep -Ei 'music|lavalink|spotify|soundcloud|youtube|yt-dlp|deezer|fallback|TrackException|LoadException|ChannelTimeout|erro|falhou|exception|traceback|phone-worker|phone-lavalink' || true"], timeout=8.0)),
            ("logs/lavalink.filtered.log", _run_cmd(["bash", "-lc", "journalctl -u lavalink.service --since '2 hours ago' -n 900 --no-pager -o cat | grep -Ei 'ready|lavasrc|spotify|soundcloud|deezer|youtube|loadtracks|master|403|404|error|exception|failed|TrackException' || true"], timeout=8.0)),
            ("logs/phone-worker-watch.log", _run_cmd(["bash", "-lc", "journalctl -u phone-worker-watch.service --since '2 hours ago' -n 220 --no-pager -o cat || true"], timeout=6.0)),
            ("logs/phone-lavalink-watch.log", _run_cmd(["bash", "-lc", "journalctl -u phone-lavalink-watch.service --since '2 hours ago' -n 220 --no-pager -o cat || true"], timeout=6.0)),
            ("logs/local-bot-logs.txt", _local_log_tail()),
        ]
        for arc, text in log_entries:
            files.append((arc, text))
            snapshot_log_blocks.append(f"# {arc}\n{text}")
        if snapshot_log_blocks:
            combined_snapshot_logs = "\n\n".join(snapshot_log_blocks)
            files.append(("logs/phone-worker/error-highlights.txt", _phone_worker_log_extract_text(combined_snapshot_logs, source="snapshot da VPS")))
            files.append(("logs/phone-worker/text-stats.txt", _phone_worker_text_stats_summary(combined_snapshot_logs, source="snapshot da VPS")))
            files.append(("logs/phone-worker/log-summary.txt", _phone_worker_log_summary_text(combined_snapshot_logs, source="snapshot da VPS")))

        payload, worker_status = _phone_worker_zip_texts(files, filename=filename, compresslevel=6)
        if payload is None:
            logger.info("[phone-worker] snapshot compactado localmente | motivo=%s", worker_status)
            files.append(("meta/phone-worker-fallback.txt", worker_status + "\nSnapshot compactado localmente na VPS.\n"))
            payload = _zip_texts_local(files, compresslevel=6)
        else:
            logger.info("[phone-worker] snapshot compactado no celular")
            files.append(("meta/phone-worker-zip.txt", worker_status + "\nSnapshot compactado no celular.\n"))
    except Exception as exc:
        return None, filename, f"Falha ao montar snapshot da VPS: {type(exc).__name__}: {exc}"

    if len(payload) > VPS_SNAPSHOT_MAX_BYTES:
        return None, filename, f"Snapshot ficou grande demais para anexar com segurança: {len(payload) / (1024 * 1024):.1f} MB."
    return payload, filename, f"Snapshot da VPS anexado ({len(payload)} bytes)."


async def build_vps_snapshot_archive() -> tuple[bytes | None, str, str]:
    return await asyncio.to_thread(build_vps_snapshot_archive_sync)
