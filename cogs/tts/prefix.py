"""Roteamento dos prefixos do TTS.

Tem dois tipos de prefixo aqui:
- prefixo do BOT (`_join`, `_leave`, `_help` etc) — comandos de controle
- prefixo de FALA (`%`, `'`, `,`, `.`) — escolhe a engine de TTS pra essa mensagem

Cada guild pode customizar todos os prefixos via painel de servidor; este
módulo só aplica e devolve a decisão pro cog principal executar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aliases import extract_prefixed_argument, get_prefixed_aliases, matches_prefixed_command


@dataclass(frozen=True)
class PrefixControlCommand:
    kind: str
    argument: str = ""
    alias: str = ""


@dataclass(frozen=True)
class PrefixRoutingConfig:
    bot_prefix: str
    atts_prefix: str
    teto_prefix: str
    gtts_prefix: str
    edge_prefix: str

    def speech_prefixes(self) -> tuple[str, ...]:
        return (self.atts_prefix, self.teto_prefix, self.edge_prefix, self.gtts_prefix)


def build_prefix_routing_config(
    guild_defaults: dict[str, Any] | None,
    *,
    bot_prefix_default: str,
    atts_prefix_default: str = "%",
    teto_prefix_default: str = "'",
) -> PrefixRoutingConfig:
    # Cada prefixo tem um fallback hardcoded — usado quando a guild ainda
    # não configurou nada ou o doc tá faltando o campo.
    defaults = guild_defaults or {}
    return PrefixRoutingConfig(
        bot_prefix=str(defaults.get("bot_prefix", bot_prefix_default) or bot_prefix_default),
        atts_prefix=str(defaults.get("atts_prefix", atts_prefix_default) or atts_prefix_default),
        teto_prefix=str(defaults.get("teto_prefix", teto_prefix_default) or teto_prefix_default),
        gtts_prefix=str(defaults.get("gtts_prefix", defaults.get("tts_prefix", ".")) or "."),
        edge_prefix=str(defaults.get("edge_prefix", ",") or ","),
    )



def _match_prefixed_command_with_argument(content: str, bot_prefix: str, *, kind: str) -> tuple[str, str] | None:
    """Retorna o alias usado e o argumento restante.

    Usado pelo painel de usuário porque `_p` pode ser compartilhado com o
    player de música. O dispatcher decide se consome ou deixa passar.
    """
    raw = str(content or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    for alias in get_prefixed_aliases(bot_prefix, kind, display=False):
        alias_lower = alias.lower()
        if lowered == alias_lower:
            return alias, ""
        if lowered.startswith(alias_lower + " "):
            return alias, raw[len(alias):].strip()
    return None

def match_prefix_control_command(content: str, bot_prefix: str) -> PrefixControlCommand | None:
    # Casa o conteúdo contra cada comando de controle conhecido. Os com
    # argumento (`reset`, `set_lang`) extraem o resto da string como argumento.
    raw = str(content or "").strip()
    if not raw:
        return None

    if matches_prefixed_command(raw, bot_prefix, kind="help"):
        return PrefixControlCommand("help")
    if matches_prefixed_command(raw, bot_prefix, kind="clear"):
        return PrefixControlCommand("clear")
    if matches_prefixed_command(raw, bot_prefix, kind="leave"):
        return PrefixControlCommand("leave")
    if matches_prefixed_command(raw, bot_prefix, kind="join"):
        return PrefixControlCommand("join")
    if matches_prefixed_command(raw, bot_prefix, kind="reset"):
        return PrefixControlCommand("reset", extract_prefixed_argument(raw, bot_prefix, kind="reset"))
    if matches_prefixed_command(raw, bot_prefix, kind="set_lang"):
        return PrefixControlCommand("set_lang", extract_prefixed_argument(raw, bot_prefix, kind="set_lang"))
    if matches_prefixed_command(raw, bot_prefix, kind="owner_dm_test"):
        return PrefixControlCommand("owner_dm_test")
    panel_user = _match_prefixed_command_with_argument(raw, bot_prefix, kind="panel_user")
    if panel_user is not None:
        alias, argument = panel_user
        return PrefixControlCommand("panel_user", argument, alias)
    if matches_prefixed_command(raw, bot_prefix, kind="panel_server"):
        return PrefixControlCommand("panel_server")
    if matches_prefixed_command(raw, bot_prefix, kind="panel_toggle"):
        return PrefixControlCommand("panel_toggle")
    return None


def _clean_configurable_prefix(value: object) -> str:
    return str(value or "").strip()[:8]


def validate_prefix_values(*, bot_prefix: object, atts_prefix: object, teto_prefix: object, edge_prefix: object, gtts_prefix: object) -> tuple[bool, str]:
    values = {
        "bot": _clean_configurable_prefix(bot_prefix),
        "ATTS": _clean_configurable_prefix(atts_prefix),
        "Kasane Teto": _clean_configurable_prefix(teto_prefix),
        "Edge": _clean_configurable_prefix(edge_prefix),
        "gTTS": _clean_configurable_prefix(gtts_prefix),
    }
    empty = [label for label, value in values.items() if not value]
    if empty:
        return False, f"O prefixo de {empty[0]} não pode ficar vazio."
    for label, value in values.items():
        if any(char.isspace() for char in value) or any(ord(char) < 33 for char in value):
            return False, f"O prefixo de {label} não pode conter espaços ou caracteres invisíveis."
    seen: dict[str, str] = {}
    for label, value in values.items():
        other = seen.get(value)
        if other:
            return False, f"Os prefixos de {other} e {label} não podem ser iguais (`{value}`)."
        seen[value] = label
    return True, ""


def match_engine_prefix(
    content: str,
    *,
    atts_prefix: str,
    teto_prefix: str = "'",
    edge_prefix: str,
    gtts_prefix: str,
) -> tuple[str | None, str | None]:
    # Prefixos maiores são testados primeiro para evitar que um prefixo curto
    # capture configurações de vários caracteres. A ordem abaixo só desempata
    # tamanhos iguais e preserva o ATTS como primeira engine histórica.
    text = str(content or "")
    candidates = [
        ("android_native", str(atts_prefix or "")),
        ("teto", str(teto_prefix or "")),
        ("edge", str(edge_prefix or "")),
        ("gtts", str(gtts_prefix or "")),
    ]
    candidates.sort(key=lambda item: len(item[1]), reverse=True)
    for engine, prefix in candidates:
        if prefix and text.startswith(prefix):
            return engine, prefix
    return None, None


async def dispatch_prefix_control_command(cog: Any, message: Any, command: PrefixControlCommand) -> bool:
    # Despacho 1-1 do comando casado pra o handler do cog. `help` é tratado
    # à parte (pelo Utility.on_message), aqui só absorve o evento pra não
    # cair no path de TTS.
    kind = command.kind

    if kind == "help":
        return True
    if kind == "clear":
        await cog._prefix_clear(message)
        return True
    if kind == "leave":
        await cog._prefix_leave(message)
        return True
    if kind == "join":
        await cog._prefix_join(message)
        return True
    if kind == "reset":
        await cog._prefix_reset_user(message, command.argument)
        return True
    if kind == "set_lang":
        await cog._prefix_set_lang(message, command.argument)
        return True
    if kind == "owner_dm_test":
        await cog._prefix_test_owner_dm(message)
        return True
    if kind == "panel_user":
        return bool(await cog._send_prefix_panel(
            message,
            panel_type="user",
            target_query=command.argument,
            invoked_alias=command.alias,
        ))
    if kind == "panel_server":
        await cog._send_prefix_panel(message, panel_type="server")
        return True
    if kind == "panel_toggle":
        await cog._send_prefix_panel(message, panel_type="toggle")
        return True
    return False
