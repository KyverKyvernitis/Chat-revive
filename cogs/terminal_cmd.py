from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import discord
from discord.ext import commands

from .terminal_fun import TerminalFunRouter

try:
    import config
except Exception:  # pragma: no cover - fallback defensivo para testes isolados
    config = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 256 * 1024
OUTPUT_PREVIEW_CHARS = 3200
EDITOR_PAGE_LINES = 80
EDITOR_MAX_FILE_BYTES = 1024 * 1024
EDITOR_TIMEOUT_SECONDS = 30 * 60
DISCORD_MESSAGE_MAX_CHARS = 2000
BOT_SERVICE = "tts-bot.service"
CALLKEEPER_TERMS = (
    "callkeeper",
    "call_keeper",
    "call-keeper",
    "call keeper",
    "callkeeper_runtime",
    "callkeeper_service.py",
    "cogs/call_keeper.py",
)
CALLKEEPER_RESCUE_MARKERS = (
    "uso rescue",
    "_cmd start bot",
    "_cmd restart bot",
    "_cmd status bot",
    "_cmd logs bot",
)
INTERACTIVE_EDITORS = {"nano", "vim", "vi", "micro", "edit"}


@dataclass(slots=True)
class ShellResult:
    command: str
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    elapsed: float
    timed_out: bool = False
    truncated: bool = False


@dataclass(slots=True)
class EditorSession:
    owner_id: int
    path: Path
    display_path: str
    original_hash: str | None
    original_mtime_ns: int | None
    draft: str
    page: int = 0
    dirty: bool = False
    created_at: float = 0.0

    def line_count(self) -> int:
        if not self.draft:
            return 1
        return max(1, len(self.draft.splitlines()))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _iter_config_owner_ids() -> Iterable[int]:
    if config is None:
        return []
    values: list[int] = []
    for attr in ("BOT_OWNER_ID", "OWNER_ID"):
        value = _safe_int(getattr(config, attr, 0))
        if value:
            values.append(value)
    return values


def _env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default)) or default))
    except Exception:
        return default


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _collapse_shell_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _touches_callkeeper(value: str | Path) -> bool:
    text = str(value or "").replace("\\", "/")
    lowered = _collapse_shell_text(text)
    compact = lowered.replace(" ", "")
    for term in CALLKEEPER_TERMS:
        normalized = _collapse_shell_text(term)
        if normalized in lowered or normalized.replace(" ", "") in compact:
            return True
    return False


def _looks_like_discord_token(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}", value or ""))


def redact_text(text: str) -> str:
    if not text:
        return ""
    result = str(text)
    result = re.sub(
        r"(?i)(authorization\s*:\s*(?:bot|bearer)\s+)[A-Za-z0-9._\-+/=]+",
        r"\1***redacted***",
        result,
    )
    result = re.sub(
        r"(?i)\b((?:discord_)?(?:bot_)?token|github_token|api[_-]?key|secret|password|passwd|webhook(?:_url)?|database_url)\b\s*([:=])\s*([^\s'\"`]+)",
        r"\1\2***redacted***",
        result,
    )
    result = re.sub(
        r"(?i)(CALLKEEPER_BOT_\d+_TOKEN\s*[:=]\s*)([^\s'\"`]+)",
        r"\1***redacted***",
        result,
    )
    result = re.sub(
        r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}",
        "***redacted***",
        result,
    )
    if _looks_like_discord_token(result):
        result = "***redacted***"
    return result


def redact_bytes(payload: bytes) -> bytes:
    try:
        return redact_text(payload.decode("utf-8", errors="replace")).encode("utf-8")
    except Exception:
        return b"***redacted***"


def _truncate_text(value: str, limit: int, *, suffix: str = "…") -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


def _code_block(text: str, *, language: str = "") -> str:
    clean = str(text or "").replace("```", "`\u200b``")
    return f"```{language}\n{clean}\n```"


def _path_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except Exception:
        return str(path)


def _safe_filename(label: str, fallback: str = "arquivo") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or fallback).strip())
    name = name.strip(".-") or fallback
    return name[:90]


def _split_shell(command: str) -> list[str]:
    try:
        return shlex.split(command or "")
    except ValueError:
        return []


def _editor_request(command: str) -> tuple[bool, str | None, str | None]:
    parts = _split_shell(command)
    if not parts:
        return False, None, None
    first = Path(parts[0]).name.lower()
    if first not in INTERACTIVE_EDITORS:
        return False, None, None
    for arg in parts[1:]:
        if not arg or arg.startswith("-"):
            continue
        return True, arg, None
    return True, None, "Informe o arquivo depois do editor. Exemplo: `_cmd nano bot.py`."


def _is_primary_bot_stop_request(command: str) -> bool:
    parts = _split_shell(command)
    if not parts:
        return False
    if parts and Path(parts[0]).name == "sudo":
        parts = parts[1:]
    if len(parts) != 3:
        return False
    binary = Path(parts[0]).name
    action = parts[1].lower()
    service = parts[2].lower()
    return binary == "systemctl" and action == "stop" and service in {"tts-bot", BOT_SERVICE}


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


class _TerminalNoticeView(discord.ui.LayoutView):
    def __init__(self, title: str, body: str, *, ok: bool = True):
        super().__init__(timeout=None)
        color = discord.Colour.green() if ok else discord.Colour.red()
        text = f"## {title}\n{body}".strip()
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(_truncate_text(text, 3900)), accent_color=color))


class _EditorEditModal(discord.ui.Modal):
    def __init__(self, view: "_TerminalEditorView"):
        super().__init__(title="Editar linhas")
        self.view_ref = view
        session = view.session
        start, end = view.visible_range()
        self.start_input = discord.ui.TextInput(
            label="Linha inicial",
            default=str(start),
            placeholder="1",
            max_length=8,
            required=True,
        )
        self.end_input = discord.ui.TextInput(
            label="Linha final",
            default=str(end),
            placeholder="1",
            max_length=8,
            required=True,
        )
        self.content_input = discord.ui.TextInput(
            label="Novo conteúdo",
            style=discord.TextStyle.paragraph,
            placeholder="Deixe vazio para remover a faixa informada.",
            default="",
            max_length=4000,
            required=False,
        )
        self.add_item(self.start_input)
        self.add_item(self.end_input)
        self.add_item(self.content_input)
        self._session_id = id(session)

    async def on_submit(self, interaction: discord.Interaction):
        if id(self.view_ref.session) != self._session_id:
            await interaction.response.send_message("Sessão expirada.", ephemeral=True)
            return
        if not await self.view_ref.ensure_owner(interaction):
            return
        try:
            start = int(str(self.start_input.value or "").strip())
            end = int(str(self.end_input.value or "").strip())
        except Exception:
            await interaction.response.send_message("Linhas inválidas.", ephemeral=True)
            return
        try:
            self.view_ref.apply_line_edit(start, end, str(self.content_input.value or ""))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.view_ref.rebuild()
        await interaction.response.edit_message(view=self.view_ref)


class _EditorButton(discord.ui.Button):
    def __init__(self, view_ref: "_TerminalEditorView", action: str, *, label: str, style: discord.ButtonStyle = discord.ButtonStyle.secondary, disabled: bool = False):
        super().__init__(label=label, style=style, disabled=disabled)
        self.view_ref = view_ref
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.view_ref.handle_action(interaction, self.action)


class _TerminalEditorView(discord.ui.LayoutView):
    def __init__(self, cog: "TerminalCommandCog", session: EditorSession):
        super().__init__(timeout=EDITOR_TIMEOUT_SECONDS)
        self.cog = cog
        self.session = session
        self.message: discord.Message | None = None
        self.rebuild()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if _safe_int(getattr(interaction.user, "id", 0)) != self.session.owner_id:
            await interaction.response.send_message("Só quem abriu esse editor pode mexer nele.", ephemeral=True)
            return False
        return True

    def visible_range(self) -> tuple[int, int]:
        total = self.session.line_count()
        start = self.session.page * EDITOR_PAGE_LINES + 1
        if start > total:
            start = max(1, ((total - 1) // EDITOR_PAGE_LINES) * EDITOR_PAGE_LINES + 1)
            self.session.page = max(0, (start - 1) // EDITOR_PAGE_LINES)
        end = min(total, start + EDITOR_PAGE_LINES - 1)
        return start, end

    def _preview_text(self) -> str:
        start, end = self.visible_range()
        lines = self.session.draft.splitlines() or [""]
        selected = lines[start - 1:end]
        width = max(2, len(str(end)))
        rendered = "\n".join(f"{idx:>{width}} │ {line}" for idx, line in enumerate(selected, start=start))
        rendered = redact_text(rendered)
        suffix = Path(self.session.path.name).suffix.lower().lstrip(".")
        language = "py" if suffix == "py" else suffix[:16]
        return _code_block(_truncate_text(rendered, 3000), language=language)

    def _status_lines(self) -> list[str]:
        start, end = self.visible_range()
        status = "rascunho alterado" if self.session.dirty else "sem alterações"
        return [
            "# Terminal · editor",
            f"**Arquivo:** `{self.session.display_path}`",
            f"**Linhas:** {start}-{end} de {self.session.line_count()}",
            f"**Estado:** {status}",
        ]

    def rebuild(self) -> None:
        self.clear_items()
        total = self.session.line_count()
        can_prev = self.session.page > 0
        can_next = (self.session.page + 1) * EDITOR_PAGE_LINES < total
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("\n".join(self._status_lines())),
            discord.ui.TextDisplay(self._preview_text()),
            discord.ui.ActionRow(
                _EditorButton(self, "prev", label="◀ Página", disabled=not can_prev),
                _EditorButton(self, "next", label="▶ Página", disabled=not can_next),
                _EditorButton(self, "edit", label="Editar linhas", style=discord.ButtonStyle.primary),
            ),
            discord.ui.ActionRow(
                _EditorButton(self, "save", label="Salvar", style=discord.ButtonStyle.success, disabled=not self.session.dirty),
                _EditorButton(self, "download", label="Baixar rascunho"),
                _EditorButton(self, "cancel", label="Cancelar", style=discord.ButtonStyle.danger),
            ),
        ]
        self.add_item(discord.ui.Container(*children, accent_color=discord.Colour.blurple()))

    def apply_line_edit(self, start: int, end: int, replacement: str) -> None:
        lines = self.session.draft.splitlines()
        if not lines:
            lines = [""]
        total = len(lines)
        if start < 1 or end < start or end > total:
            raise ValueError(f"Faixa inválida. Use linhas entre 1 e {total}.")
        new_lines = str(replacement or "").splitlines()
        lines[start - 1:end] = new_lines
        keep_final_newline = self.session.draft.endswith("\n") or replacement.endswith("\n")
        self.session.draft = "\n".join(lines)
        if keep_final_newline and self.session.draft:
            self.session.draft += "\n"
        self.session.dirty = True
        self.session.page = max(0, (max(1, start) - 1) // EDITOR_PAGE_LINES)

    async def handle_action(self, interaction: discord.Interaction, action: str) -> None:
        if not await self.ensure_owner(interaction):
            return
        if action == "prev":
            self.session.page = max(0, self.session.page - 1)
            self.rebuild()
            await interaction.response.edit_message(view=self)
            return
        if action == "next":
            max_page = max(0, (self.session.line_count() - 1) // EDITOR_PAGE_LINES)
            self.session.page = min(max_page, self.session.page + 1)
            self.rebuild()
            await interaction.response.edit_message(view=self)
            return
        if action == "edit":
            await interaction.response.send_modal(_EditorEditModal(self))
            return
        if action == "save":
            await interaction.response.defer(ephemeral=True, thinking=True)
            ok, text = await self.cog.save_editor_session(self.session)
            if ok:
                self.session.dirty = False
                self.session.original_hash = self.cog.file_hash(self.session.path)
                self.session.original_mtime_ns = self.cog.file_mtime_ns(self.session.path)
                self.rebuild()
                with contextlib.suppress(Exception):
                    if interaction.message is not None:
                        await interaction.message.edit(view=self)
            await interaction.followup.send(view=_TerminalNoticeView("Editor", text, ok=ok), ephemeral=True)
            return
        if action == "download":
            payload = self.session.draft.encode("utf-8", errors="replace")
            filename = _safe_filename(Path(self.session.display_path).name or "rascunho")
            file = discord.File(io.BytesIO(payload), filename=filename)
            try:
                await interaction.response.send_message("Rascunho atual.", file=file, ephemeral=True)
            finally:
                with contextlib.suppress(Exception):
                    file.close()
            return
        if action == "cancel":
            self.stop()
            self.clear_items()
            self.add_item(discord.ui.Container(discord.ui.TextDisplay("## Terminal · editor\nSessão cancelada. Nenhuma alteração pendente foi salva."), accent_color=discord.Colour.dark_grey()))
            await interaction.response.edit_message(view=self)
            return
        await interaction.response.send_message("Ação inválida.", ephemeral=True)

    async def on_timeout(self) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("## Terminal · editor\nSessão expirada. Nenhuma alteração pendente foi salva."), accent_color=discord.Colour.dark_grey()))
        if self.message is not None:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)


class TerminalCommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._run_lock = asyncio.Lock()
        self._fun_router = TerminalFunRouter(self)

    async def _is_owner(self, ctx: commands.Context) -> bool:
        user_id = _safe_int(getattr(ctx.author, "id", 0))
        owner_ids = set(_iter_config_owner_ids())
        for attr in ("owner_id",):
            value = _safe_int(getattr(self.bot, attr, 0))
            if value:
                owner_ids.add(value)
        raw_owner_ids = getattr(self.bot, "owner_ids", None)
        if raw_owner_ids:
            for value in raw_owner_ids:
                value_int = _safe_int(value)
                if value_int:
                    owner_ids.add(value_int)
        if owner_ids and user_id in owner_ids:
            return True
        try:
            return bool(await self.bot.is_owner(ctx.author))
        except Exception:
            return False

    def _make_notice(self, title: str, body: str, *, ok: bool = True) -> _TerminalNoticeView:
        return _TerminalNoticeView(title, body, ok=ok)

    async def _reply_notice(self, ctx: commands.Context, title: str, body: str, *, ok: bool = True, file: discord.File | None = None):
        kwargs = {
            "view": self._make_notice(title, body, ok=ok),
            "mention_author": False,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if file is not None:
            kwargs["file"] = file
        return await ctx.reply(**kwargs)

    def _usage_text(self) -> str:
        return (
            "Use `_cmd help` para ver os subcomandos, "
            "`_cmd <comando de terminal>` para o Bash ou `_cmd nano <arquivo>` para o editor."
        )

    async def _send_direct_message(self, ctx: commands.Context, user_id: int, content: str) -> None:
        if len(content) > DISCORD_MESSAGE_MAX_CHARS:
            await self._reply_notice(
                ctx,
                "Terminal · DM",
                f"A mensagem ultrapassa o limite de {DISCORD_MESSAGE_MAX_CHARS} caracteres do Discord.",
                ok=False,
            )
            return

        try:
            user = self.bot.get_user(user_id)
            if user is None:
                user = await self.bot.fetch_user(user_id)
            sent = await user.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=True,
                    replied_user=False,
                ),
            )
        except discord.NotFound:
            await self._reply_notice(ctx, "Terminal · DM", f"Usuário `{user_id}` não encontrado.", ok=False)
            return
        except discord.Forbidden:
            await self._reply_notice(
                ctx,
                "Terminal · DM",
                "O Discord recusou a mensagem privada. O usuário pode estar com as DMs fechadas ou não compartilhar servidor com o bot.",
                ok=False,
            )
            return
        except discord.HTTPException as exc:
            await self._reply_notice(
                ctx,
                "Terminal · DM",
                f"Falha ao enviar a mensagem: `{type(exc).__name__}: {exc}`",
                ok=False,
            )
            return
        except Exception as exc:
            await self._reply_notice(
                ctx,
                "Terminal · DM",
                f"Falha inesperada: `{type(exc).__name__}: {exc}`",
                ok=False,
            )
            return

        user_label = discord.utils.escape_markdown(str(user))
        await self._reply_notice(
            ctx,
            "Terminal · DM",
            f"Mensagem enviada para **{user_label}** (`{user.id}`).\nID da mensagem: `{sent.id}`.",
            ok=True,
        )

    async def _send_channel_message(self, ctx: commands.Context, channel_id: int, content: str) -> None:
        if len(content) > DISCORD_MESSAGE_MAX_CHARS:
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                f"A mensagem ultrapassa o limite de {DISCORD_MESSAGE_MAX_CHARS} caracteres do Discord.",
                ok=False,
            )
            return

        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            await self._reply_notice(ctx, "Terminal · canal", f"Canal `{channel_id}` não encontrado.", ok=False)
            return
        except discord.Forbidden:
            await self._reply_notice(ctx, "Terminal · canal", "O bot não tem acesso a esse canal.", ok=False)
            return
        except discord.HTTPException as exc:
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                f"Falha ao localizar o canal: `{type(exc).__name__}: {exc}`",
                ok=False,
            )
            return

        if not isinstance(channel, discord.abc.Messageable):
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                f"O destino `{channel_id}` não aceita mensagens.",
                ok=False,
            )
            return

        try:
            sent = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=True,
                    replied_user=False,
                ),
            )
        except discord.Forbidden:
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                "O bot não tem permissão para enviar mensagens nesse canal.",
                ok=False,
            )
            return
        except discord.HTTPException as exc:
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                f"Falha ao enviar a mensagem: `{type(exc).__name__}: {exc}`",
                ok=False,
            )
            return
        except Exception as exc:
            await self._reply_notice(
                ctx,
                "Terminal · canal",
                f"Falha inesperada: `{type(exc).__name__}: {exc}`",
                ok=False,
            )
            return

        channel_mention = getattr(channel, "mention", f"`{channel_id}`")
        jump_url = getattr(sent, "jump_url", "")
        detail = f"Mensagem enviada em {channel_mention}.\nID da mensagem: `{sent.id}`."
        if jump_url:
            detail += f"\n[Abrir mensagem]({jump_url})"
        await self._reply_notice(ctx, "Terminal · canal", detail, ok=True)

    async def _handle_message_subcommand(self, ctx: commands.Context, command: str) -> bool:
        parts = command.split(maxsplit=2)
        if not parts:
            return False

        subcommand = parts[0].casefold()
        if subcommand not in {"dm", "canal", "channel"}:
            return False

        label = "dm" if subcommand == "dm" else "canal"
        if len(parts) < 3:
            usage = (
                "`_cmd dm <id_do_usuário> <mensagem>`"
                if subcommand == "dm"
                else "`_cmd canal <id_do_canal> <mensagem>`"
            )
            await self._reply_notice(ctx, f"Terminal · {label}", f"Use {usage}.", ok=False)
            return True

        target_raw = parts[1].strip()
        target_id = _safe_int(target_raw)
        content = parts[2].strip()
        if target_id <= 0 or not target_raw.isdigit():
            await self._reply_notice(ctx, f"Terminal · {label}", "Informe um ID numérico válido.", ok=False)
            return True
        if not content:
            await self._reply_notice(ctx, f"Terminal · {label}", "A mensagem não pode ficar vazia.", ok=False)
            return True

        if subcommand == "dm":
            await self._send_direct_message(ctx, target_id, content)
        else:
            await self._send_channel_message(ctx, target_id, content)
        return True

    def _is_callkeeper_rescue_hint(self, message: discord.Message) -> bool:
        author = getattr(message, "author", None)
        if not getattr(author, "bot", False):
            return False
        text = _collapse_shell_text(getattr(message, "content", ""))
        if not text:
            return False
        return all(marker in text for marker in CALLKEEPER_RESCUE_MARKERS)

    async def _suppress_callkeeper_rescue_hints(self, ctx: commands.Context) -> None:
        channel = getattr(ctx, "channel", None)
        command_message = getattr(ctx, "message", None)
        if channel is None or command_message is None or not hasattr(channel, "history"):
            return
        # Os CallKeepers também escutam `_cmd` para rescue. Como o updater comum
        # protege callkeeper_runtime, a cog principal limpa apenas os avisos de uso
        # gerados por comandos `_cmd` que pertencem ao terminal do bot principal.
        for _ in range(10):
            with contextlib.suppress(Exception):
                async for message in channel.history(limit=30, after=command_message):
                    if self._is_callkeeper_rescue_hint(message):
                        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                            await message.delete()
            await asyncio.sleep(0.5)

    def resolve_edit_path(self, raw_path: str) -> Path:
        raw = str(raw_path or "").strip()
        if not raw:
            raise ValueError("Arquivo inválido.")
        if "\x00" in raw:
            raise ValueError("Caminho inválido.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path.absolute()
        if _touches_callkeeper(resolved):
            raise PermissionError("Bloqueado: CallKeeper é protegido para recuperação do bot.")
        return resolved

    def file_hash(self, path: Path) -> str | None:
        try:
            return _sha256_bytes(path.read_bytes())
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def file_mtime_ns(self, path: Path) -> int | None:
        try:
            return int(path.stat().st_mtime_ns)
        except Exception:
            return None

    async def open_editor(self, ctx: commands.Context, raw_path: str) -> None:
        try:
            path = self.resolve_edit_path(raw_path)
        except PermissionError as exc:
            await self._reply_notice(ctx, "Terminal", str(exc), ok=False)
            return
        except Exception as exc:
            await self._reply_notice(ctx, "Terminal", f"Caminho inválido: {exc}", ok=False)
            return

        if path.exists() and path.is_dir():
            await self._reply_notice(ctx, "Terminal", "Esse caminho é uma pasta. Informe um arquivo.", ok=False)
            return
        if path.exists():
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            if size > EDITOR_MAX_FILE_BYTES:
                await self._reply_notice(ctx, "Terminal", "Arquivo grande demais para editar pelo painel. Use comandos de shell ou baixe/edite fora do Discord.", ok=False)
                return
            try:
                payload = path.read_bytes()
                if b"\x00" in payload:
                    await self._reply_notice(ctx, "Terminal", "Arquivo parece binário. Editor bloqueado para evitar corrupção.", ok=False)
                    return
                draft = payload.decode("utf-8")
            except UnicodeDecodeError:
                await self._reply_notice(ctx, "Terminal", "Arquivo não está em UTF-8. Editor bloqueado para evitar corrupção.", ok=False)
                return
            except Exception as exc:
                await self._reply_notice(ctx, "Terminal", f"Não consegui ler o arquivo: {type(exc).__name__}: {exc}", ok=False)
                return
            original_hash = _sha256_bytes(payload)
            original_mtime_ns = self.file_mtime_ns(path)
        else:
            draft = ""
            original_hash = None
            original_mtime_ns = None

        session = EditorSession(
            owner_id=_safe_int(getattr(ctx.author, "id", 0)),
            path=path,
            display_path=_path_label(path),
            original_hash=original_hash,
            original_mtime_ns=original_mtime_ns,
            draft=draft,
            created_at=time.monotonic(),
        )
        view = _TerminalEditorView(self, session)
        message = await ctx.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        view.message = message

    async def save_editor_session(self, session: EditorSession) -> tuple[bool, str]:
        if _touches_callkeeper(session.path):
            return False, "Bloqueado: CallKeeper é protegido para recuperação do bot."
        path = session.path
        current_hash = self.file_hash(path)
        if current_hash != session.original_hash:
            return False, "O arquivo mudou no disco desde que o editor foi aberto. Reabra o editor para evitar sobrescrever alterações externas."

        payload = session.draft.encode("utf-8")
        ok, validation = await self._validate_file_payload(path, payload)
        if not ok:
            return False, validation

        backup_dir = Path(tempfile.gettempdir()) / "terminal_cmd_backups"
        with contextlib.suppress(Exception):
            backup_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with contextlib.suppress(Exception):
                backup_name = f"{_safe_filename(_path_label(path))}.{int(time.time())}.bak"
                (backup_dir / backup_name).write_bytes(path.read_bytes())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            original_mode = None
            if path.exists():
                with contextlib.suppress(Exception):
                    original_mode = path.stat().st_mode & 0o777
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.cmd-", suffix=".tmp", dir=str(path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fp:
                    fp.write(payload)
                if original_mode is not None:
                    with contextlib.suppress(Exception):
                        os.chmod(tmp_path, original_mode)
                os.replace(tmp_path, path)
            finally:
                with contextlib.suppress(Exception):
                    if tmp_path.exists():
                        tmp_path.unlink()
        except Exception as exc:
            return False, f"Falha ao salvar `{session.display_path}`: {type(exc).__name__}: {exc}"

        summary = f"Arquivo salvo: `{session.display_path}`."
        if validation:
            summary += f"\n{validation}"
        return True, summary

    async def _validate_file_payload(self, path: Path, payload: bytes) -> tuple[bool, str]:
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                json.loads(payload.decode("utf-8"))
            except Exception as exc:
                return False, f"JSON inválido: {type(exc).__name__}: {exc}"
            return True, "Validação JSON: ok."
        if suffix == ".py":
            tmp_path: Path | None = None
            try:
                fd, tmp_name = tempfile.mkstemp(prefix="terminal-cmd-", suffix=".py")
                tmp_path = Path(tmp_name)
                with os.fdopen(fd, "wb") as fp:
                    fp.write(payload)
                proc = await asyncio.create_subprocess_exec(
                    "python3",
                    "-m",
                    "py_compile",
                    str(tmp_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                if proc.returncode != 0:
                    detail = redact_text((stderr or stdout).decode("utf-8", errors="replace"))
                    return False, "Python inválido:\n" + _code_block(_truncate_text(detail, 1800), language="bash")
                return True, "Validação Python: ok."
            except asyncio.TimeoutError:
                return False, "Validação Python excedeu o tempo limite."
            except Exception as exc:
                return False, f"Falha na validação Python: {type(exc).__name__}: {exc}"
            finally:
                if tmp_path is not None:
                    with contextlib.suppress(Exception):
                        tmp_path.unlink()
        return True, ""


    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(Exception):
            os.killpg(int(proc.pid), signal.SIGKILL)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    def _build_subprocess_env(self, ctx: commands.Context, command: str) -> dict[str, str]:
        env = dict(os.environ)

        def put(name: str, value: object) -> None:
            if value is None:
                return
            text = str(value)
            if text:
                env[name] = text

        guild = getattr(ctx, "guild", None)
        channel = getattr(ctx, "channel", None)
        author = getattr(ctx, "author", None)
        message = getattr(ctx, "message", None)

        put("DISCORD_CMD_GUILD_ID", getattr(guild, "id", ""))
        put("DISCORD_CMD_GUILD_NAME", getattr(guild, "name", ""))
        put("DISCORD_CMD_CHANNEL_ID", getattr(channel, "id", ""))
        put("DISCORD_CMD_CHANNEL_NAME", getattr(channel, "name", ""))
        put("DISCORD_CMD_AUTHOR_ID", getattr(author, "id", ""))
        put("DISCORD_CMD_AUTHOR_NAME", getattr(author, "name", ""))
        put("DISCORD_CMD_AUTHOR_DISPLAY_NAME", getattr(author, "display_name", ""))
        put("DISCORD_CMD_MESSAGE_ID", getattr(message, "id", ""))
        put("DISCORD_CMD_MESSAGE_URL", getattr(message, "jump_url", ""))
        put("DISCORD_CMD_REPO_ROOT", REPO_ROOT)
        put("DISCORD_CMD_BOT_USER_ID", getattr(getattr(self.bot, "user", None), "id", ""))
        put("DISCORD_CMD_RAW", _truncate_text(command, 8192, suffix=""))
        return env

    async def _run_shell(self, ctx: commands.Context, command: str) -> ShellResult:
        timeout = _env_float("TERMINAL_CMD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(REPO_ROOT),
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=self._build_subprocess_env(ctx, command),
        )
        state = {"bytes": 0, "truncated": False}
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def collect(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                remaining = MAX_OUTPUT_BYTES - int(state["bytes"])
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                    state["bytes"] = int(state["bytes"]) + min(len(chunk), remaining)
                if len(chunk) > remaining:
                    state["truncated"] = True
                    await self._kill_process_group(proc)
                    return

        stdout_task = asyncio.create_task(collect(proc.stdout, stdout_chunks))
        stderr_task = asyncio.create_task(collect(proc.stderr, stderr_chunks))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_process_group(proc)
            await proc.wait()
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return ShellResult(
            command=command,
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            exit_code=proc.returncode,
            elapsed=time.monotonic() - start,
            timed_out=timed_out,
            truncated=bool(state["truncated"]),
        )

    def _format_shell_result(self, result: ShellResult) -> tuple[str, discord.File | None]:
        stdout = redact_bytes(result.stdout).decode("utf-8", errors="replace").strip()
        stderr = redact_bytes(result.stderr).decode("utf-8", errors="replace").strip()
        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            if stdout:
                parts.append("\n[stderr]\n" + stderr)
            else:
                parts.append(stderr)
        output = "\n".join(parts).strip() or "(sem saída)"
        status_bits = [
            f"**Comando:** `{_truncate_text(result.command, 600)}`",
            f"**Código:** `{result.exit_code}`",
            f"**Tempo:** `{_format_elapsed(result.elapsed)}`",
        ]
        if result.timed_out:
            status_bits.append("**Status:** tempo limite atingido")
        if result.truncated:
            status_bits.append("**Status:** saída truncada")
        header = "\n".join(status_bits)
        preview = _code_block(_truncate_text(output, OUTPUT_PREVIEW_CHARS), language="bash")
        file: discord.File | None = None
        if len(output) > OUTPUT_PREVIEW_CHARS or result.truncated:
            full = (
                f"$ {result.command}\n"
                f"exit_code={result.exit_code}\n"
                f"elapsed={result.elapsed:.3f}s\n"
                f"timed_out={result.timed_out}\n"
                f"truncated={result.truncated}\n\n"
                f"{output}\n"
            )
            file = discord.File(io.BytesIO(full.encode("utf-8", errors="replace")), filename="cmd-output.txt")
            header += "\nSaída completa enviada em arquivo."
        return f"{header}\n\n{preview}", file

    def _should_suppress_shell_notice(self, result: ShellResult) -> bool:
        if result.exit_code != 0 or result.timed_out or result.truncated:
            return False
        return not (result.stdout or b"").strip() and not (result.stderr or b"").strip()

    async def _stop_primary_bot(self, ctx: commands.Context) -> None:
        await self._reply_notice(
            ctx,
            "Terminal",
            "Bot principal será parado. Para religar, mencione um dos CallKeepers no chat.",
            ok=True,
        )
        await asyncio.create_subprocess_shell(
            f"nohup /bin/bash -lc 'sleep 1; sudo systemctl stop {BOT_SERVICE}' >/dev/null 2>&1 &",
            cwd=str(REPO_ROOT),
            executable="/bin/bash",
        )

    @commands.command(name="cmd")
    @commands.guild_only()
    async def terminal_command(self, ctx: commands.Context, *, command: str | None = None):
        if not await self._is_owner(ctx):
            return
        command = str(command or "").strip()
        if not command:
            await self._reply_notice(ctx, "Terminal", self._usage_text(), ok=False)
            return
        self.bot.loop.create_task(self._suppress_callkeeper_rescue_hints(ctx))

        if await self._handle_message_subcommand(ctx, command):
            return

        if await self._fun_router.handle(ctx, command):
            return

        if _touches_callkeeper(command):
            await self._reply_notice(ctx, "Terminal", "Bloqueado: CallKeeper é protegido para recuperação do bot.", ok=False)
            return

        is_editor, raw_path, editor_error = _editor_request(command)
        if is_editor:
            if editor_error or not raw_path:
                await self._reply_notice(ctx, "Terminal", editor_error or "Arquivo inválido.", ok=False)
                return
            await self.open_editor(ctx, raw_path)
            return

        if _is_primary_bot_stop_request(command):
            await self._stop_primary_bot(ctx)
            return

        if self._run_lock.locked():
            await self._reply_notice(ctx, "Terminal", "Já existe um comando em execução.", ok=False)
            return

        async with self._run_lock:
            try:
                result = await self._run_shell(ctx, command)
            except Exception as exc:
                await self._reply_notice(ctx, "Terminal", f"Falha ao executar: `{type(exc).__name__}: {exc}`", ok=False)
                return
        if self._should_suppress_shell_notice(result):
            return
        body, file = self._format_shell_result(result)
        try:
            await self._reply_notice(ctx, "Terminal", body, ok=(result.exit_code == 0 and not result.timed_out), file=file)
        finally:
            if file is not None:
                with contextlib.suppress(Exception):
                    file.close()

    @terminal_command.error
    async def terminal_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NoPrivateMessage):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            if await self._is_owner(ctx):
                await self._reply_notice(ctx, "Terminal", self._usage_text(), ok=False)
            return
        if isinstance(error, commands.CheckFailure):
            return
        if await self._is_owner(ctx):
            await self._reply_notice(ctx, "Terminal", f"Erro no comando: `{type(error).__name__}`.", ok=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(TerminalCommandCog(bot))
