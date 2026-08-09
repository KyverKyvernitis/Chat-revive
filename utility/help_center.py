from __future__ import annotations

from dataclasses import dataclass
import difflib
import re
import unicodedata
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


CATEGORIES: tuple[HelpCategory, ...] = (
    HelpCategory(
        key="voice",
        label="Voz e TTS",
        emoji="🔊",
        description="Fale na call e ajuste sua voz.",
        accent=discord.Color.green(),
        search_terms=("tts", "voz", "falar", "fala", "call", "audio"),
    ),
    HelpCategory(
        key="music",
        label="Música",
        emoji="🎵",
        description="Toque músicas e controle a fila.",
        accent=discord.Color.blurple(),
        search_terms=("musica", "player", "fila", "som"),
    ),
    HelpCategory(
        key="games",
        label="Jogos e fichas",
        emoji="🎮",
        description="Jogue, ganhe e use suas fichas.",
        accent=discord.Color.orange(),
        search_terms=("jogos", "jogo", "fichas", "ficha", "economia", "saldo"),
    ),
    HelpCategory(
        key="chatbot",
        label="Chatbot e imagens",
        emoji="🤖",
        description="Converse com o bot ou crie imagens.",
        accent=discord.Color.fuchsia(),
        search_terms=("chatbot", "chat", "ia", "imagem", "imagens", "profile", "perfil"),
    ),
    HelpCategory(
        key="server",
        label="Servidor",
        emoji="⚙️",
        description="Configure recursos deste servidor.",
        accent=discord.Color.gold(),
        search_terms=("servidor", "server", "staff", "admin", "config", "configurar"),
    ),
)


ENTRIES: tuple[HelpEntry, ...] = (
    # Voz e TTS
    HelpEntry("gtts", "voice", "Falar", "Fala usando gTTS.", "{gtts_prefix}texto", ("google tts", "gtts")),
    HelpEntry("edge", "voice", "Falar", "Fala usando Edge TTS.", "{edge_prefix}texto", ("edge", "edge tts")),
    HelpEntry(
        "tts_menu", "voice", "Configurar", "Configura sua voz.", "/tts menu",
        ("menu tts", "painel tts", "configurar voz"), slash_root="tts", slash_path="tts menu",
    ),
    HelpEntry(
        "tts_status", "voice", "Configurar", "Mostra seu TTS atual.", "/tts status",
        ("status tts", "meu tts"), slash_root="tts", slash_path="tts status",
    ),
    HelpEntry(
        "tts_toggle", "voice", "Staff", "Liga ou desliga recursos do TTS.", "/toggle_menu",
        ("toggle", "toggles", "toggle menu", "tts toggle"), permission="kick", slash_root="toggle_menu", slash_path="toggle_menu",
        detail_note="Requer Expulsar membros.",
    ),
    HelpEntry(
        "panel", "voice", "Configurar", "Abre seu painel de TTS.", "{bot_prefix}panel",
        ("painel", "p", "painel usuario", "painel usuário"), aliases=("painel", "p"),
    ),
    HelpEntry(
        "set_lang", "voice", "Configurar", "Troca seu idioma do gTTS.", "{bot_prefix}set lang <idioma>",
        ("idioma tts", "idioma gtts", "set lang", "lingua tts"),
    ),
    HelpEntry("join", "voice", "Call", "Entra na sua call.", "{bot_prefix}join", ("entrar call", "conectar")),
    HelpEntry("leave", "voice", "Call", "Sai da call.", "{bot_prefix}leave", ("sair call", "desconectar")),
    HelpEntry("clear", "voice", "Call", "Limpa a fila de fala.", "{bot_prefix}clear", ("limpar tts", "limpar fila")),
    HelpEntry(
        "panel_other", "voice", "Staff", "Abre o painel de TTS de outro usuário.", "{bot_prefix}panel @usuário",
        ("tts usuario", "tts usuário", "painel outro", "editar tts usuario"), permission="kick",
        detail_note="Requer Expulsar membros.",
    ),
    HelpEntry(
        "tts_server", "voice", "Staff", "Configura o TTS do servidor.", "/tts server menu",
        ("tts servidor", "server tts", "painel servidor tts"), permission="kick",
        slash_root="tts", slash_path="tts server menu", detail_note="Requer Expulsar membros.",
    ),
    HelpEntry(
        "panel_server", "voice", "Staff", "Abre o painel de TTS do servidor.", "{bot_prefix}panel_server",
        ("painel servidor", "server panel", "sp"), aliases=("sp",), permission="kick",
        detail_note="Requer Expulsar membros.",
    ),
    HelpEntry(
        "panel_toggle", "voice", "Staff", "Abre o painel de toggles do TTS.", "{bot_prefix}toggle_panel",
        ("painel toggle", "painel toggles", "tp"), aliases=("tp",), permission="kick",
        detail_note="Requer Expulsar membros.",
    ),
    HelpEntry(
        "reset_tts", "voice", "Staff", "Reseta o TTS de outro usuário.", "{bot_prefix}reset @usuário",
        ("reset tts", "reset usuario tts", "reset usuário tts"), permission="kick",
        detail_note="Requer Expulsar membros.",
    ),

    # Música
    HelpEntry("play", "music", "Player", "Toca uma música ou playlist.", "{bot_prefix}play <nome ou link>", ("tocar", "music", "musica"), ("tocar", "music", "musica")),
    HelpEntry("pause", "music", "Player", "Pausa a reprodução.", "{bot_prefix}pause", ("pausar",), ("pausar", "pa")),
    HelpEntry("resume", "music", "Player", "Continua a reprodução.", "{bot_prefix}resume", ("retomar", "continuar"), ("retomar", "continuar", "r")),
    HelpEntry("skip", "music", "Player", "Pula a música atual.", "{bot_prefix}skip", ("pular",), ("pular", "s")),
    HelpEntry("back", "music", "Player", "Volta uma faixa.", "{bot_prefix}back", ("voltar musica", "anterior"), ("b", "previous", "voltar", "anterior")),
    HelpEntry("stop", "music", "Player", "Encerra o player.", "{bot_prefix}stop", ("parar musica",), ("st", "pararmusica", "musicstop")),
    HelpEntry("queue", "music", "Fila", "Mostra a fila.", "{bot_prefix}queue", ("fila",), ("fila", "q")),
    HelpEntry("shuffle", "music", "Fila", "Embaralha a fila.", "{bot_prefix}shuffle", ("embaralhar",), ("sh", "embaralhar")),
    HelpEntry("loop", "music", "Fila", "Repete a reprodução.", "{bot_prefix}loop", ("repetir",), ("l", "repeat", "repetir")),
    HelpEntry("remove", "music", "Fila", "Remove uma faixa da fila.", "{bot_prefix}remove <posição>", ("remover musica",), ("rm", "remover")),
    HelpEntry("move", "music", "Fila", "Move uma faixa na fila.", "{bot_prefix}move <de> <para>", ("mover musica",), ("mv", "mover")),
    HelpEntry("skipto", "music", "Fila", "Pula até uma posição da fila.", "{bot_prefix}skipto <posição>", ("pular ate", "ir para musica"), ("goto", "jump", "jumpto", "tocarfila")),
    HelpEntry("clearqueue", "music", "Fila", "Limpa a fila de músicas.", "{bot_prefix}clearqueue", ("limpar fila musica",), ("cq", "limparfila", "limparqueue", "clearq")),
    HelpEntry("np", "music", "Outros", "Mostra o que está tocando.", "{bot_prefix}np", ("tocando", "now playing"), ("now", "nowplaying", "tocando")),
    HelpEntry("volume", "music", "Outros", "Ajusta o volume.", "{bot_prefix}volume <0-100>", ("volume musica",), ("v", "vol")),
    HelpEntry("history", "music", "Outros", "Mostra o histórico recente.", "{bot_prefix}history", ("historico musica", "histórico música"), ("h", "historico", "played")),
    HelpEntry("readd", "music", "Outros", "Readiciona uma música do histórico.", "{bot_prefix}readd <posição>", ("readicionar",), ("ra", "readicionar", "historicofila", "historicoqueue")),

    # Jogos e fichas
    HelpEntry("ficha", "games", "Fichas", "Mostra seu saldo.", "ficha", ("saldo", "fichas"), ("fichas",)),
    HelpEntry("extrato", "games", "Fichas", "Mostra movimentações recentes.", "extrato", ("movimentacoes", "movimentações")),
    HelpEntry("daily", "games", "Fichas", "Resgata o prêmio diário.", "daily", ("diario", "diário", "bonus", "login"), ("bonus", "login")),
    HelpEntry("recarga", "games", "Fichas", "Recupera um saldo muito baixo.", "recarga", ("recarregar fichas",), ("recarrega",)),
    HelpEntry("rank", "games", "Fichas", "Mostra o ranking de fichas.", "rank", ("ranking", "leaderboard"), ("leaderboard",)),
    HelpEntry("pay", "games", "Fichas", "Envia fichas para alguém.", "pay @usuário <valor>", ("pagar", "enviar fichas")),
    HelpEntry("mendigar", "games", "Fichas", "Pede fichas.", "mendigar <valor>", ("pedir fichas",)),
    HelpEntry("race", "games", "Fichas", "Abre a seleção de raça.", "race", ("raca", "raça"), ("raça",)),
    HelpEntry("roleta", "games", "Jogos", "Joga roleta.", "roleta", ("roulette",)),
    HelpEntry("carta", "games", "Jogos", "Joga uma rodada de cartas.", "carta", ("cartas",), ("cartas",)),
    HelpEntry("buckshot", "games", "Jogos", "Abre uma partida de sobrevivência.", "buckshot", ("buckshot roulette",)),
    HelpEntry("alvo", "games", "Jogos", "Abre uma disputa de mira.", "alvo", ("mira",)),
    HelpEntry("corrida", "games", "Jogos", "Abre uma corrida de cavalos.", "corrida", ("cavalos", "corrida cavalo")),
    HelpEntry("poker", "games", "Jogos", "Abre uma mesa de poker.", "poker", ("pôquer",)),
    HelpEntry("truco", "games", "Jogos", "Desafia alguém para truco.", "truco @usuário", ("truco 1v1",)),
    HelpEntry("roubar", "games", "Jogos", "Tenta roubar fichas de alguém.", "roubar @usuário", ("rob", "roubo"), ("rob",)),
    HelpEntry(
        "economia", "games", "Staff", "Configura jogos, fichas e raças.", "/economia",
        ("config economia", "administrar economia"), permission="economy_staff",
        slash_root="economia", slash_path="economia", detail_note="Disponível apenas para a staff configurada.",
    ),

    # Chatbot e imagens
    HelpEntry("chat", "chatbot", "Conversar", "Conversa com o chatbot.", "@bot mensagem", ("conversar", "mencionar bot", "responder bot")),
    HelpEntry(
        "imagem", "chatbot", "Criar", "Gera uma imagem pelo profile ativo.", "/imagem <prompt>",
        ("gerar imagem", "imagem ia"), slash_root="imagem", slash_path="imagem", suffix=" `prompt`",
    ),
    HelpEntry(
        "reset", "chatbot", "Memória", "Limpa sua memória pessoal do chatbot.", "/reset",
        ("reset memoria", "limpar memoria", "esquecer"), slash_root="reset", slash_path="reset",
    ),
    HelpEntry(
        "chatbot_profile", "chatbot", "Staff", "Gerencia profiles do chatbot.", "/chatbot profile",
        ("profile", "profiles", "perfil chatbot"), permission="manage_guild",
        slash_root="chatbot", slash_path="chatbot profile", detail_note="Requer Gerenciar servidor.",
    ),
    HelpEntry(
        "chatbot_persona", "chatbot", "Staff", "Configura uma persona baseada em usuário.", "/chatbot persona",
        ("persona",), permission="manage_guild", slash_root="chatbot", slash_path="chatbot persona",
        detail_note="Requer Gerenciar servidor.",
    ),
    HelpEntry(
        "chatbot_extrovert", "chatbot", "Staff", "Ajusta respostas espontâneas do chatbot.", "/chatbot extrovert",
        ("extrovert", "extrovertido", "resposta espontanea"), permission="manage_guild",
        slash_root="chatbot", slash_path="chatbot extrovert", detail_note="Requer Gerenciar servidor.",
    ),
    HelpEntry(
        "chatbot_memoria", "chatbot", "Staff", "Reseta a memória do chatbot no servidor.", "/chatbot memoria",
        ("memoria servidor", "reset servidor chatbot"), permission="manage_guild",
        slash_root="chatbot", slash_path="chatbot memoria", detail_note="Requer Gerenciar servidor.",
    ),

    # Servidor
    HelpEntry("welcome", "server", "Painéis", "Configura boas-vindas.", "{bot_prefix}welcome", ("boas vindas", "boas-vindas", "bv"), ("boasvindas", "boas-vindas", "boas", "bv"), "manage_guild"),
    HelpEntry("birthday", "server", "Painéis", "Configura aniversários.", "{bot_prefix}birthday", ("aniversario", "aniversário", "aniversarios"), permission="manage_guild"),
    HelpEntry("ticket", "server", "Painéis", "Publica o painel de tickets.", "{bot_prefix}ticket", ("tickets", "publicar ticket"), permission="ticket_staff"),
    HelpEntry("ticketedit", "server", "Painéis", "Abre o editor de tickets.", "{bot_prefix}ticketedit", ("editar ticket", "editor ticket"), permission="ticket_staff"),
    HelpEntry("color", "server", "Personalização", "Publica o painel de cores.", "{bot_prefix}color", ("cores", "painel cores"), permission="administrator", detail_note="Requer Administrador."),
    HelpEntry("coloredit", "server", "Personalização", "Edita as cores do painel.", "{bot_prefix}coloredit", ("editar cores",), permission="administrator", detail_note="Requer Administrador."),
    HelpEntry("roleicon", "server", "Personalização", "Gerencia ícones conectados a cargos.", "{bot_prefix}roleicon", ("icone cargo", "ícone cargo", "role icon"), permission="manage_roles", detail_note="Requer Gerenciar cargos."),
    HelpEntry("form", "server", "Formulário", "Publica ou atualiza o formulário.", "form", ("formulario", "formulário"), permission="kick_or_manage_guild"),
    HelpEntry("form_config", "server", "Formulário", "Abre a configuração do formulário.", "c", ("config formulario", "configurar formulario", "customizar formulario"), permission="kick_or_manage_guild"),
    HelpEntry(
        "say", "server", "Outros", "Envia uma mensagem pelo bot.", "/say",
        ("falar pelo bot", "mensagem webhook"), permission="manage_messages", slash_root="say", slash_path="say",
        detail_note="Requer Gerenciar mensagens.",
    ),
    HelpEntry(
        "feedback", "server", "Outros", "Envia feedback para o dono do bot.", "/feedback",
        ("sugestao", "sugestão", "bug", "relato"), permission="feedback_staff", slash_root="feedback", slash_path="feedback",
        detail_note="Disponível para a staff.",
    ),

    # Utilidades diretas, sem ocupar uma categoria própria.
    HelpEntry(
        "ping", None, "", "Mostra latência e estado do bot.", "/ping",
        ("latencia", "latência", "status bot"), slash_root="ping", slash_path="ping",
    ),
    HelpEntry(
        "help", None, "", "Abre esta central de ajuda.", "/help",
        ("ajuda", "comandos"), slash_root="help", slash_path="help",
    ),
)



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
        for token in _category_tokens(category):
            token_map.setdefault(_normalize(token), category)
    for entry in ENTRIES:
        if entry.category in hidden_categories:
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
    bot_prefix: str,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
    limit: int = 25,
) -> list[app_commands.Choice[str]]:
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
        name = f"{entry.key} — {_without_terminal_punctuation(entry.description)}"[:100]
        scored.append((score, name, f"entry:{entry.key}"))

    scored.sort(key=lambda item: (item[0], item[1].casefold()))
    return [app_commands.Choice(name=name, value=value) for _, name, value in scored[:limit]]


def _format_usage(entry: HelpEntry, prefixes: dict[str, str], root_ids: dict[str, int]) -> str:
    if entry.slash_root and entry.slash_path:
        return slash_mention(root_ids, root=entry.slash_root, path=entry.slash_path) + entry.suffix

    try:
        rendered = entry.usage.format(**prefixes)
    except Exception:
        rendered = entry.usage
    return f"`{rendered}`"


def _aliases_line(entry: HelpEntry, prefixes: dict[str, str]) -> str:
    if not entry.aliases:
        return ""
    bot_prefix = prefixes.get("bot_prefix", "_")
    values: list[str] = []
    prefixed_aliases = "{bot_prefix}" in entry.usage
    for alias in entry.aliases:
        if prefixed_aliases:
            values.append(f"`{bot_prefix}{alias}`")
        else:
            values.append(f"`{alias}`")
    return " · ".join(values)


def _render_home(prefixes: dict[str, str], root_ids: dict[str, int]) -> tuple[str, discord.Color]:
    help_mention = slash_mention(root_ids, root="help", path="help")
    ping_mention = slash_mention(root_ids, root="ping", path="ping")
    bot_prefix = prefixes.get("bot_prefix", "_")
    body = (
        "# 📚 Ajuda\n"
        "Encontre comandos e recursos do bot\n\n"
        f"Use o menu, {help_mention} `assunto` ou `{bot_prefix}help assunto`\n"
        f"-# {ping_mention} mostra latência e estado do bot"
    )
    return body, discord.Color.blurple()


def _render_category(
    category: HelpCategory,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    lines = [f"# {category.emoji} {category.label}", _without_terminal_punctuation(category.description)]
    entries = [
        entry
        for entry in ENTRIES
        if entry.category == category.key
        and _permission_allowed(user, entry.permission, extra_permissions)
    ]

    last_group = None
    for entry in entries:
        if entry.group != last_group:
            lines.extend(["", f"**{entry.group}**"])
            last_group = entry.group
        lines.append(f"{_format_usage(entry, prefixes, root_ids)} — {_without_terminal_punctuation(entry.description)}")

    if category.key == "games":
        lines.extend(["", "-# Jogos podem usar trigger ou prefixo, conforme a configuração do servidor"])
    elif category.key == "server" and not entries:
        lines.extend(["", "Nenhuma configuração disponível para suas permissões"])

    return "\n".join(lines), category.accent


def _entry_display_title(entry: HelpEntry, prefixes: dict[str, str]) -> str:
    if entry.slash_path:
        return f"/{entry.slash_path}"
    try:
        plain = entry.usage.format(**prefixes)
    except Exception:
        plain = entry.usage
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
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    usage = _format_usage(entry, prefixes, root_ids)
    category = _CATEGORY_BY_KEY.get(entry.category or "")
    title_emoji = category.emoji if category else "🔎"
    title = _entry_display_title(entry, prefixes)
    lines = [f"# {title_emoji} `{title}`", _without_terminal_punctuation(entry.description), "", f"**Uso:** {usage}"]

    aliases = _aliases_line(entry, prefixes)
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
    prefixes: dict[str, str],
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, discord.Color]:
    suggestions = suggest_help_targets(
        subject,
        bot_prefix=prefixes.get("bot_prefix", "_"),
        hidden_categories=hidden_categories,
    )
    lines = ["# 🔎 Nada encontrado", f"Não achei `{str(subject)[:80]}`"]
    if suggestions:
        labels: list[str] = []
        for target in suggestions:
            if isinstance(target, HelpCategory):
                labels.append(target.label)
            else:
                labels.append(target.key)
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
            placeholder="Ir para…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = str(self.values[0]) if self.values else "home"
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
        extra_permissions: set[str] | frozenset[str] = frozenset(),
        hidden_categories: set[str] | frozenset[str] = frozenset(),
        subject: str | None = None,
        timeout: float = HELP_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=max(1.0, float(timeout)))
        self.owner = owner
        self.owner_id = int(owner.id)
        self.prefixes = dict(prefixes)
        self.root_ids = dict(root_ids)
        self.extra_permissions = frozenset(extra_permissions)
        self.hidden_categories = frozenset(hidden_categories)
        self.message: discord.Message | None = None
        self.target_kind, self.target_key = resolve_help_target(
            subject,
            bot_prefix=self.prefixes.get("bot_prefix", "_"),
            hidden_categories=self.hidden_categories,
        )
        self.rebuild()

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
                extra_permissions=self.extra_permissions,
            )
        if self.target_kind == "entry" and self.target_key in _ENTRY_BY_KEY:
            return _render_entry(
                _ENTRY_BY_KEY[self.target_key],
                user=self.owner,
                prefixes=self.prefixes,
                root_ids=self.root_ids,
                extra_permissions=self.extra_permissions,
            )
        return _render_not_found(
            str(self.target_key or ""),
            prefixes=self.prefixes,
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
