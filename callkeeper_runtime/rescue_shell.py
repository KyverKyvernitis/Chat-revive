from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_CHARS = 120_000
CHUNK_SIZE = 1680
MAX_TEXT_ATTACHMENT_BYTES = 24 * 1024 * 1024

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)^(?P<prefix>\s*(?:export\s+)?[A-Z0-9_]*(?:TOKEN|API[_-]?KEY|APIKEY|SECRET|PASSWORD|PASS|MONGO(?:DB)?[_-]?URI|DATABASE[_-]?URL|WEBHOOK[_-]?URL|AUTHORIZATION)[A-Z0-9_]*\s*=\s*)(?P<value>.*)$"
)
DISCORD_WEBHOOK_RE = re.compile(
    r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-\.]+",
    re.IGNORECASE,
)
AUTH_HEADER_RE = re.compile(r"(?i)\b(Authorization\s*[:=]\s*(?:Bearer|Bot)?\s*)[A-Za-z0-9_\-.=:/+]{12,}")
MONGO_URL_PASSWORD_RE = re.compile(r"(?i)\b(mongodb(?:\+srv)?://[^:\s/@]+:)([^@\s/]+)(@[^\s]+)")
GENERIC_URL_PASSWORD_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:\s/@]+:)([^@\s/]+)(@)")
DISCORD_TOKEN_RE = re.compile(
    r"\b(?:mfa\.[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,10}\.[A-Za-z0-9_\-]{20,})\b"
)
COMMON_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9_\-]{20,})\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    elapsed: float
    timed_out: bool = False


async def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(int(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass


async def run_shell(command: str, *, cwd: Path, timeout: float | None = None) -> CommandResult:
    timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
    start = time.perf_counter()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await kill_process_group(proc)
        out_b, err_b = await proc.communicate()
    elapsed = time.perf_counter() - start
    stdout = (out_b or b"").decode("utf-8", errors="replace")
    stderr = (err_b or b"").decode("utf-8", errors="replace")
    return CommandResult(stdout=stdout, stderr=stderr, exit_code=proc.returncode, elapsed=elapsed, timed_out=timed_out)


def redact_text(text: object) -> str:
    if text is None:
        return ""
    value = str(text)
    if not value:
        return ""

    def _assignment_repl(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}[REDACTED]"

    value = SENSITIVE_ASSIGNMENT_RE.sub(_assignment_repl, value)
    value = DISCORD_WEBHOOK_RE.sub("[REDACTED_DISCORD_WEBHOOK]", value)
    value = AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", value)
    value = MONGO_URL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", value)
    value = GENERIC_URL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", value)
    value = DISCORD_TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    value = COMMON_SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", value)
    return value


def redact_bytes(payload: bytes | bytearray | memoryview | None) -> bytes:
    if not payload:
        return b""
    text = bytes(payload).decode("utf-8", errors="replace")
    return redact_text(text).encode("utf-8", errors="replace")


def _escape_code_fences(content: str) -> str:
    return (content or "").replace("```", "`​``")


def chunk_text(content: str) -> list[str]:
    if not content:
        return []
    safe = _escape_code_fences(content)
    if len(safe) <= 1950:
        return [safe]
    parts = [safe[i:i + CHUNK_SIZE] for i in range(0, len(safe), CHUNK_SIZE)]
    total = len(parts)
    return [f"🖥️ Resultado — parte {index}/{total}\n```txt\n{part}\n```" for index, part in enumerate(parts, start=1)]


def build_full_result_text(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> str:
    command = redact_text(command)
    stdout = redact_text(stdout or "")
    stderr = redact_text(stderr or "")
    return (
        f"Comando: {command}\n"
        f"Código: {exit_code if exit_code is not None else 'sem código'}\n"
        f"Tempo: {elapsed:.2f}s\n"
        f"Timeout: {'sim' if timed_out else 'não'}\n"
        "\n"
        "----- STDOUT -----\n"
        f"{stdout or '(sem saída)'}\n"
        "\n"
        "----- STDERR -----\n"
        f"{stderr or '(sem saída)'}\n"
    )


def build_result_attachment(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> tuple[str, bytes] | None:
    content = build_full_result_text(
        command=command,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        exit_code=exit_code,
        elapsed=elapsed,
        timed_out=timed_out,
    )
    payload = content.encode("utf-8", errors="replace")
    if len(payload) > MAX_TEXT_ATTACHMENT_BYTES:
        return None
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"cmd_result_{now}.txt", payload


def format_result(
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    elapsed: float,
    timed_out: bool = False,
) -> str:
    command = redact_text(command)
    stdout = redact_text(stdout or "")
    stderr = redact_text(stderr or "")
    body = (
        "🖥️ Comando executado\n"
        f"Comando: {command}\n"
        f"Código: {exit_code if exit_code is not None else 'sem código'}"
        f" • Tempo: {elapsed:.2f}s"
        f"{' • timeout' if timed_out else ''}\n\n"
        f"--- stdout ---\n{stdout.strip() or '(sem saída)'}\n\n"
        f"--- stderr ---\n{stderr.strip() or '(sem saída)'}"
    )
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n\n[saída cortada na prévia; o .txt anexado contém o resultado completo]"
    return body
