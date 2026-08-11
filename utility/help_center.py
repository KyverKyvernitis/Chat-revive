from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import re
import unicodedata
from collections.abc import Awaitable, Callable
import discord
from discord import app_commands

from cogs.tts.utils.app_commands import slash_mention


HELP_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class HelpCategory:
    key: str
    label: str
    emoji: str
    description: str
    accent: discord.Color
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelpEntry:
    key: str
    category: str | None
    group: str
    description: str
    usage: str
    search_terms: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    permission: str = "user"
    slash_root: str | None = None
    slash_path: str | None = None
    suffix: str = ""
    detail_note: str = ""
    show_in_category: bool = True


_CATALOG_PATH = Path(__file__).resolve().parents[1] / "shared" / "help_catalog.json"
_ACCENT_FACTORIES: dict[str, Callable[[], discord.Color]] = {
    "green": discord.Color.green,
    "blurple": discord.Color.blurple,
    "orange": discord.Color.orange,
    "fuchsia": discord.Color.fuchsia,
    "gold": discord.Color.gold,
}


def _catalog_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _load_help_catalog() -> tuple[tuple[HelpCategory, ...], tuple[HelpEntry, ...]]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or int(raw.get("version") or 0) != 1:
        raise RuntimeError("catálogo de ajuda incompatível")

    categories: list[HelpCategory] = []
    for item in raw.get("categories") or []:
        if not isinstance(item, dict):
            continue
        accent_name = str(item.get("accent") or "blurple").strip().lower()
        accent_factory = _ACCENT_FACTORIES.get(accent_name, discord.Color.blurple)
        categories.append(HelpCategory(
            key=str(item.get("key") or "").strip(),
            label=str(item.get("label") or "").strip(),
            emoji=str(item.get("emoji") or "").strip(),
            description=str(item.get("description") or "").strip(),
            accent=accent_factory(),
            search_terms=_catalog_strings(item.get("search_terms")),
        ))

    entries: list[HelpEntry] = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        category_raw = item.get("category")
        entries.append(HelpEntry(
            key=str(item.get("key") or "").strip(),
            category=str(category_raw).strip() if category_raw is not None else None,
            group=str(item.get("group") or "").strip(),
            description=str(item.get("description") or "").strip(),
            usage=str(item.get("usage") or "").strip(),
            search_terms=_catalog_strings(item.get("search_terms")),
            aliases=_catalog_strings(item.get("aliases")),
            permission=str(item.get("permission") or "user").strip(),
            slash_root=str(item.get("slash_root") or "").strip() or None,
            slash_path=str(item.get("slash_path") or "").strip() or None,
            suffix=str(item.get("suffix") or ""),
            detail_note=str(item.get("detail_note") or "").strip(),
            show_in_category=bool(item.get("show_in_category", True)),
        ))

    category_keys = {category.key for category in categories}
    entry_keys = {entry.key for entry in entries}
    if not categories or not entries or len(category_keys) != len(categories) or len(entry_keys) != len(entries):
        raise RuntimeError("catálogo de ajuda inválido")
    if any(entry.category is not None and entry.category not in category_keys for entry in entries):
        raise RuntimeError("catálogo de ajuda possui categoria desconhecida")
    return tuple(categories), tuple(entries)


CATEGORIES, ENTRIES = _load_help_catalog()


_CATEGORY_DIRECT_TERMS: dict[str, tuple[str, ...]] = {
    "voice": ("voice", "voz", "tts", "falar"),
    "music": ("music", "musica", "música", "player"),
    "games": ("games", "jogos", "jogo", "economia"),
    "chatbot": ("chatbot", "chat", "ia"),
    "server": ("server", "servidor", "staff", "admin"),
}

_CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
_ENTRY_BY_KEY = {entry.key: entry for entry in ENTRIES}


def _normalize(value: str) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _without_terminal_punctuation(value: str) -> str:
    return re.sub(r"[.!?…]+$", "", str(value or "").rstrip())


def _strip_command_prefix(query: str, bot_prefix: str) -> str:
    raw = str(query or "").strip()
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    prefix = str(bot_prefix or "")
    if prefix and raw.startswith(prefix):
        raw = raw[len(prefix):].lstrip()
    return raw


def _permission_allowed(
    user: discord.abc.User,
    permission: str,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> bool:
    if permission == "user" or permission in extra_permissions:
        return True
    if not isinstance(user, discord.Member):
        return False
    perms = getattr(user, "guild_permissions", None)
    if perms is None:
        return False

    is_owner = user.id == user.guild.owner_id
    is_admin = bool(getattr(perms, "administrator", False))
    if permission == "administrator":
        return bool(is_owner or is_admin)
    if permission == "manage_guild":
        return bool(is_owner or is_admin or getattr(perms, "manage_guild", False))
    if permission == "manage_messages":
        return bool(is_owner or is_admin or getattr(perms, "manage_messages", False))
    if permission == "say_staff":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
            or getattr(perms, "manage_webhooks", False)
        )
    if permission == "manage_roles":
        return bool(is_owner or is_admin or getattr(perms, "manage_roles", False))
    if permission == "kick":
        # Os handlers de TTS checam `kick_members` diretamente, então o help
        # reproduz exatamente essa regra em vez de assumir bypass por admin.
        return bool(is_owner or getattr(perms, "kick_members", False))
    if permission == "kick_or_manage_guild":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "kick_members", False)
            or getattr(perms, "manage_guild", False)
        )
    if permission == "feedback_staff":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "kick_members", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
        )
    if permission in {"economy_staff", "ticket_staff"}:
        return False
    return False


def _category_visible(
    category: HelpCategory,
    user: discord.abc.User,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> bool:
    if category.key in hidden_categories:
        return False
    if category.key != "server":
        return True
    return any(
        entry.category == category.key
        and _permission_allowed(user, entry.permission, extra_permissions)
        for entry in ENTRIES
    )


def _entry_tokens(entry: HelpEntry) -> tuple[str, ...]:
    tokens = [entry.key, *entry.search_terms, *entry.aliases]
    if entry.slash_path:
        tokens.append(entry.slash_path)
    return tuple(token for token in tokens if token)


def _category_tokens(category: HelpCategory) -> tuple[str, ...]:
    return (category.key, category.label, *category.search_terms)


def _prefix_entry_tokens(entry: HelpEntry) -> tuple[str, ...]:
    if not entry.usage.startswith("{bot_prefix}"):
        return ()

    raw = entry.usage[len("{bot_prefix}"):].strip()
    command_parts: list[str] = []
    for part in raw.split():
        if part.startswith("<") or part.startswith("@") or part.startswith("["):
            break
        command_parts.append(part)
    command = " ".join(command_parts).strip()
    return tuple(token for token in (command, *entry.aliases) if token)


def resolve_help_target(
    subject: str | None,
    *,
    bot_prefix: str,
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    if subject is None or not str(subject).strip():
        return ("home", None)

    raw = str(subject).strip()
    normalized_special = _normalize(raw)
    if normalized_special == "home" or normalized_special == "inicio":
        return ("home", None)
    if normalized_special.startswith("category:"):
        key = normalized_special.split(":", 1)[1]
        if key in _CATEGORY_BY_KEY and key not in hidden_categories:
            return ("category", key)
    if normalized_special.startswith("entry:"):
        key = normalized_special.split(":", 1)[1]
        entry = _ENTRY_BY_KEY.get(key)
        if entry is not None and entry.category not in hidden_categories:
            return ("entry", key)

    is_slash_command = raw.startswith("/")
    is_prefix_command = bool(bot_prefix and raw.startswith(bot_prefix))
    normalized = _normalize(_strip_command_prefix(raw, bot_prefix))

    # Quando o usuário inclui o prefixo, respeita o namespace real do comando.
    # Isso diferencia, por exemplo, `_reset` (TTS) de `/reset` (chatbot).
    if is_slash_command:
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if entry.slash_path and normalized == _normalize(entry.slash_path):
                return ("entry", entry.key)
    elif is_prefix_command:
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if any(normalized == _normalize(token) for token in _prefix_entry_tokens(entry)):
                return ("entry", entry.key)

    # Em texto natural, termos realmente genéricos abrem a categoria.
    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        direct_terms = _CATEGORY_DIRECT_TERMS.get(category.key, ())
        if any(normalized == _normalize(token) for token in direct_terms):
            return ("category", category.key)

    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if any(normalized == _normalize(token) for token in _entry_tokens(entry)):
            return ("entry", entry.key)

    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        if any(normalized == _normalize(token) for token in _category_tokens(category)):
            return ("category", category.key)

    # Prefix matching só entra para textos com pelo menos 3 caracteres. Evita
    # que aliases curtos como `p`, `q` e `s` levem a resultados arbitrários.
    if len(normalized) >= 3:
        entry_matches: list[HelpEntry] = []
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if any(_normalize(token).startswith(normalized) for token in _entry_tokens(entry)):
                entry_matches.append(entry)
        if len(entry_matches) == 1:
            return ("entry", entry_matches[0].key)

    return ("notfound", raw)


def suggest_help_targets(
    subject: str,
    *,
    bot_prefix: str,
    user: discord.abc.User | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
    limit: int = 3,
) -> list[HelpEntry | HelpCategory]:
    normalized = _normalize(_strip_command_prefix(subject, bot_prefix))
    if not normalized:
        return []

    token_map: dict[str, HelpEntry | HelpCategory] = {}
    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        if user is not None and not _category_visible(category, user, extra_permissions, hidden_categories):
            continue
        for token in _category_tokens(category):
            token_map.setdefault(_normalize(token), category)
    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if user is not None and not _permission_allowed(user, entry.permission, extra_permissions):
            continue
        for token in _entry_tokens(entry):
            token_map.setdefault(_normalize(token), entry)

    matched_tokens = difflib.get_close_matches(normalized, list(token_map), n=max(limit * 3, 6), cutoff=0.78)
    results: list[HelpEntry | HelpCategory] = []
    seen: set[tuple[str, str]] = set()
    for token in matched_tokens:
        target = token_map[token]
        kind = "category" if isinstance(target, HelpCategory) else "entry"
        key = target.key
        marker = (kind, key)
        if marker in seen:
            continue
        seen.add(marker)
        results.append(target)
        if len(results) >= limit:
            break
    return results


def help_autocomplete_choices(
    current: str,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
    limit: int = 25,
) -> list[app_commands.Choice[str]]:
    bot_prefix = prefixes.get("bot_prefix", "_")
    query = _normalize(_strip_command_prefix(current, bot_prefix))
    scored: list[tuple[int, str, str]] = []

    for category in CATEGORIES:
        if not _category_visible(category, user, extra_permissions, hidden_categories):
            continue
        tokens = tuple(_normalize(token) for token in _category_tokens(category))
        if not query:
            score = 0
        elif any(token.startswith(query) for token in tokens):
            score = 0
        elif any(query in token for token in tokens):
            score = 1
        else:
            continue
        scored.append((score, f"{category.emoji} {category.label}", f"category:{category.key}"))

    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if not _permission_allowed(user, entry.permission, extra_permissions):
            continue
        tokens = tuple(_normalize(token) for token in _entry_tokens(entry))
        if not query:
            # Sem texto, evita despejar dezenas de comandos: mostra só as categorias.
            continue
        if any(token == query for token in tokens):
            score = 0
        elif any(token.startswith(query) for token in tokens):
            score = 1
        elif any(query in token for token in tokens):
            score = 2
        else:
            continue
        if not entry.show_in_category and not any(token == query for token in tokens):
            continue
        public_name = _plain_usage(entry, prefixes, games_context)
        name = f"{public_name} — {_without_terminal_punctuation(entry.description)}"[:100]
        scored.append((score, name, f"entry:{entry.key}"))

    scored.sort(key=lambda item: (item[0], item[1].casefold()))
    return [app_commands.Choice(name=name, value=value) for _, name, value in scored[:limit]]


def _plain_usage(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    try:
        rendered = entry.usage.format(**prefixes)
    except Exception:
        rendered = entry.usage

    if (
        entry.category == "games"
        and entry.slash_path is None
        and (games_context or {}).get("mode") == "commands"
    ):
        bot_prefix = prefixes.get("bot_prefix", "_")
        if bot_prefix and not rendered.startswith(bot_prefix):
            rendered = f"{bot_prefix}{rendered}"
    return rendered


def _format_usage(
    entry: HelpEntry,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str] | None = None,
) -> str:
    if entry.slash_root and entry.slash_path:
        return slash_mention(root_ids, root=entry.slash_root, path=entry.slash_path) + entry.suffix
    return f"`{_plain_usage(entry, prefixes, games_context)}`"


def _aliases_line(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    if not entry.aliases:
        return ""
    bot_prefix = prefixes.get("bot_prefix", "_")
    values: list[str] = []
    prefixed_aliases = "{bot_prefix}" in entry.usage or (
        entry.category == "games" and (games_context or {}).get("mode") == "commands"
    )
    for alias in entry.aliases:
        if prefixed_aliases:
            values.append(f"`{bot_prefix}{alias}`")
        else:
            values.append(f"`{alias}`")
    return " · ".join(values)


def _render_home(prefixes: dict[str, str], root_ids: dict[str, int]) -> tuple[str, discord.Color]:
    help_mention = slash_mention(root_ids, root="help", path="help")
    bot_prefix = prefixes.get("bot_prefix", "_")
    body = (
        "# 📚 Ajuda\n"
        "Encontre comandos e recursos do bot\n\n"
        f"-# Busca direta: {help_mention} `assunto` · `{bot_prefix}help assunto`"
    )
    return body, discord.Color.blurple()


def _render_category(
    category: HelpCategory,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str] | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    lines = [f"# {category.emoji} {category.label}", _without_terminal_punctuation(category.description)]
    entries = [
        entry
        for entry in ENTRIES
        if entry.category == category.key
        and entry.show_in_category
        and _permission_allowed(user, entry.permission, extra_permissions)
    ]

    last_group = None
    for entry in entries:
        if entry.group != last_group:
            lines.extend(["", f"**{entry.group}**"])
            last_group = entry.group
        lines.append(
            f"{_format_usage(entry, prefixes, root_ids, games_context)} — "
            f"{_without_terminal_punctuation(entry.description)}"
        )

    if category.key == "games":
        mode = (games_context or {}).get("mode")
        if mode == "commands":
            lines.extend(["", f"-# Neste servidor, jogos usam o prefixo `{prefixes.get('bot_prefix', '_')}`"])
        elif mode == "triggers":
            lines.extend(["", "-# Neste servidor, jogos são usados sem prefixo"])
        channel_mention = str((games_context or {}).get("channel_mention") or "").strip()
        if channel_mention:
            lines.append(f"-# Canal de jogos: {channel_mention}")
    elif category.key == "server" and not entries:
        lines.extend(["", "Nenhuma configuração disponível para suas permissões"])

    return "\n".join(lines), category.accent


def _entry_display_title(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    if entry.slash_path:
        return f"/{entry.slash_path}"
    plain = _plain_usage(entry, prefixes, games_context)
    # Mantém prefixos de voz inteiros, mas corta argumentos de comandos.
    if entry.key in {"gtts", "edge"}:
        return plain
    return plain.split(" ", 1)[0]


def _render_entry(
    entry: HelpEntry,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str] | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    usage = _format_usage(entry, prefixes, root_ids, games_context)
    category = _CATEGORY_BY_KEY.get(entry.category or "")
    title_emoji = category.emoji if category else "🔎"
    title = _entry_display_title(entry, prefixes, games_context)
    lines = [f"# {title_emoji} `{title}`", _without_terminal_punctuation(entry.description), "", f"**Uso:** {usage}"]

    aliases = _aliases_line(entry, prefixes, games_context)
    if aliases:
        lines.append(f"**Aliases:** {aliases}")

    if entry.detail_note:
        lines.extend(["", f"-# {_without_terminal_punctuation(entry.detail_note)}"])
    elif not _permission_allowed(user, entry.permission, extra_permissions):
        lines.extend(["", "-# Você não tem a permissão necessária para usar este recurso"])

    if category is not None:
        lines.append(f"-# {category.emoji} {category.label}")
        accent = category.accent
    else:
        accent = discord.Color.teal()
    return "\n".join(lines), accent


def _render_not_found(
    subject: str,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    suggestions = suggest_help_targets(
        subject,
        bot_prefix=prefixes.get("bot_prefix", "_"),
        user=user,
        extra_permissions=extra_permissions,
        hidden_categories=hidden_categories,
    )
    lines = ["# 🔎 Nada encontrado", f"Não achei `{str(subject)[:80]}`"]
    if suggestions:
        labels: list[str] = []
        for target in suggestions:
            if isinstance(target, HelpCategory):
                labels.append(target.label)
            else:
                labels.append(_plain_usage(target, prefixes, games_context))
        lines.extend(["", "Talvez: " + " · ".join(f"`{label}`" for label in labels)])
    return "\n".join(lines), discord.Color.dark_grey()


class _HelpNavigationSelect(discord.ui.Select):
    def __init__(self, owner_view: "HelpCenterView"):
        self.owner_view = owner_view
        options: list[discord.SelectOption] = [
            discord.SelectOption(label="Início", value="home", emoji="📚", description="Volta para a central de ajuda")
        ]
        for category in CATEGORIES:
            if not _category_visible(category, owner_view.owner, owner_view.extra_permissions, owner_view.hidden_categories):
                continue
            options.append(
                discord.SelectOption(
                    label=category.label,
                    value=f"category:{category.key}",
                    emoji=category.emoji,
                    description=_without_terminal_punctuation(category.description)[:100],
                )
            )
        super().__init__(
            placeholder="Escolha uma área…" if owner_view.target_kind == "home" else "Trocar área…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = str(self.values[0]) if self.values else "home"
        await self.owner_view.refresh_dynamic_state()
        if selected.startswith("category:"):
            category_key = selected.split(":", 1)[1]
            if category_key in self.owner_view.hidden_categories:
                selected = "home"
        self.owner_view.set_target(selected)
        self.owner_view.rebuild()
        await interaction.response.edit_message(view=self.owner_view)


class HelpCenterView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        owner: discord.abc.User,
        prefixes: dict[str, str],
        root_ids: dict[str, int],
        games_context: dict[str, str] | None = None,
        extra_permissions: set[str] | frozenset[str] = frozenset(),
        hidden_categories: set[str] | frozenset[str] = frozenset(),
        hidden_categories_provider: Callable[[], Awaitable[frozenset[str]]] | None = None,
        subject: str | None = None,
        timeout: float = HELP_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=max(1.0, float(timeout)))
        self.owner = owner
        self.owner_id = int(owner.id)
        self.prefixes = dict(prefixes)
        self.root_ids = dict(root_ids)
        self.games_context = dict(games_context or {})
        self.extra_permissions = frozenset(extra_permissions)
        self.hidden_categories = frozenset(hidden_categories)
        self.hidden_categories_provider = hidden_categories_provider
        self.message: discord.Message | None = None
        self.target_kind, self.target_key = resolve_help_target(
            subject,
            bot_prefix=self.prefixes.get("bot_prefix", "_"),
            hidden_categories=self.hidden_categories,
        )
        self.rebuild()

    async def refresh_dynamic_state(self) -> None:
        provider = self.hidden_categories_provider
        if provider is None:
            return
        try:
            hidden = frozenset(await provider())
        except Exception:
            return
        self.hidden_categories = hidden
        if self.target_kind == "category" and self.target_key in hidden:
            self.target_kind, self.target_key = ("home", None)
        elif self.target_kind == "entry":
            entry = _ENTRY_BY_KEY.get(str(self.target_key or ""))
            if entry is not None and entry.category in hidden:
                self.target_kind, self.target_key = ("home", None)

    def set_target(self, target: str) -> None:
        self.target_kind, self.target_key = resolve_help_target(
            target,
            bot_prefix=self.prefixes.get("bot_prefix", "_"),
            hidden_categories=self.hidden_categories,
        )

    def _render_current(self) -> tuple[str, discord.Color]:
        if self.target_kind == "home":
            return _render_home(self.prefixes, self.root_ids)
        if self.target_kind == "category" and self.target_key in _CATEGORY_BY_KEY:
            return _render_category(
                _CATEGORY_BY_KEY[self.target_key],
                user=self.owner,
                prefixes=self.prefixes,
                root_ids=self.root_ids,
                games_context=self.games_context,
                extra_permissions=self.extra_permissions,
            )
        if self.target_kind == "entry" and self.target_key in _ENTRY_BY_KEY:
            return _render_entry(
                _ENTRY_BY_KEY[self.target_key],
                user=self.owner,
                prefixes=self.prefixes,
                root_ids=self.root_ids,
                games_context=self.games_context,
                extra_permissions=self.extra_permissions,
            )
        return _render_not_found(
            str(self.target_key or ""),
            user=self.owner,
            prefixes=self.prefixes,
            games_context=self.games_context,
            extra_permissions=self.extra_permissions,
            hidden_categories=self.hidden_categories,
        )

    def rebuild(self) -> None:
        for child in list(self.children):
            self.remove_item(child)
        body, accent = self._render_current()
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(body),
                discord.ui.Separator(),
                discord.ui.ActionRow(_HelpNavigationSelect(self)),
                accent_color=accent,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        try:
            await interaction.response.send_message(
                "Abra sua própria central com `/help`",
                ephemeral=True,
            )
        except Exception:
            pass
        return False

    async def on_timeout(self) -> None:
        for child in list(self.children):
            self.remove_item(child)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 📚 Ajuda\nEsta central expirou\nUse `/help` novamente"
                ),
                accent_color=discord.Color.dark_grey(),
            )
        )
        if self.message is None:
            return
        try:
            await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
        except TypeError:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        except Exception:
            pass
