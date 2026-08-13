from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable

import discord

import config
from .rescue_shell import (
    build_result_attachment,
    chunk_text,
    format_result,
    redact_bytes,
    redact_text,
    run_shell,
)

from .settings import CALLKEEPER_OWNER_USER_ID

log = logging.getLogger(__name__)

BOT_SERVICE = "tts-bot.service"
ALLOWED_ACTIONS = {"start", "restart", "status", "logs"}


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _iter_config_owner_ids() -> Iterable[int]:
    for attr in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID"):
        uid = _safe_int(getattr(config, attr, 0))
        if uid:
            yield uid


def is_rescue_owner(user_id: int) -> bool:
    allowed = {CALLKEEPER_OWNER_USER_ID, *_iter_config_owner_ids()}
    return int(user_id or 0) in {uid for uid in allowed if uid}


class CallKeeperRescueCommandService:
    """`_cmd` mínimo dos CallKeepers para resgatar o bot principal.

    Não executa comando livre. Os auxiliares só controlam `tts-bot.service`,
    para que o dono consiga dar start/restart/status/logs quando o processo
    principal estiver offline.
    """

    def __init__(self, *, repo_root: Path):
        self.repo_root = repo_root

    def _make_file(self, attachment: tuple[str, bytes] | None) -> discord.File | None:
        if not attachment:
            return None
        filename, payload = attachment
        return discord.File(io.BytesIO(redact_bytes(payload)), filename=filename)

    async def send(self, channel: discord.abc.Messageable, content: str, *, attachment: tuple[str, bytes] | None = None) -> None:
        content = redact_text(content)
        chunks = chunk_text(content)
        use_attachment = attachment if len(chunks) > 1 else None
        for index, chunk in enumerate(chunks, start=1):
            file = self._make_file(use_attachment) if index == len(chunks) else None
            try:
                await channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                    file=file,
                )
            finally:
                if file is not None:
                    try:
                        file.close()
                    except Exception:
                        pass

    def resolve(self, raw_command: str) -> tuple[str, str] | tuple[None, str]:
        parts = (raw_command or "").strip().split()
        if len(parts) != 2:
            return None, (
                "Uso rescue: `_cmd start bot`, `_cmd restart bot`, "
                "`_cmd status bot` ou `_cmd logs bot`."
            )
        action = parts[0].lower().strip()
        target = parts[1].lower().strip()
        if target not in {"bot", "tts", "tts-bot", "main", "principal"}:
            return None, "Os CallKeepers só podem resgatar o bot principal."
        if action not in ALLOWED_ACTIONS:
            return None, "Ação rescue permitida: start, restart, status ou logs."
        if action == "status":
            return f"sudo systemctl status {BOT_SERVICE} --no-pager", ""
        if action == "logs":
            return f"sudo journalctl -u {BOT_SERVICE} -n 200 --no-pager", ""
        return f"sudo systemctl {action} {BOT_SERVICE}", ""

    async def handle_message(self, message: discord.Message) -> None:
        content = str(getattr(message, "content", "") or "").strip()
        if not content.startswith("_cmd"):
            return
        author_id = _safe_int(getattr(getattr(message, "author", None), "id", 0))
        if not is_rescue_owner(author_id):
            return

        raw_command = content[4:].strip()
        if not raw_command:
            await self.send(
                message.channel,
                "Uso rescue: `_cmd start bot`, `_cmd restart bot`, `_cmd status bot` ou `_cmd logs bot`.",
            )
            return

        command, error = self.resolve(raw_command)
        if not command:
            await self.send(message.channel, f"⚠️ {error}")
            return

        try:
            result = await run_shell(command, cwd=self.repo_root)
            attachment = build_result_attachment(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                elapsed=result.elapsed,
                timed_out=result.timed_out,
            )
            await self.send(
                message.channel,
                format_result(
                    command=command,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    elapsed=result.elapsed,
                    timed_out=result.timed_out,
                ),
                attachment=attachment,
            )
        except Exception as exc:
            log.exception("[callkeeper] falha no rescue _cmd")
            await self.send(message.channel, f"❌ Falha no rescue: `{type(exc).__name__}: {exc}`")
