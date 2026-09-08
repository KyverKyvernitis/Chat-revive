from __future__ import annotations

from typing import Iterable
from functools import lru_cache


_PREFIX_CONTROL_SPECS: dict[str, dict[str, object]] = {
    "ping": {
        "aliases": ("ping",),
        "display": ("ping",),
        "accepts_argument": False,
    },
    "help": {
        "aliases": ("help",),
        "display": ("help",),
        "accepts_argument": True,
    },
    "clear": {
        "aliases": ("clear",),
        "display": ("clear",),
        "accepts_argument": False,
    },
    "leave": {
        "aliases": ("leave",),
        "display": ("leave",),
        "accepts_argument": False,
    },
    "join": {
        "aliases": ("join",),
        "display": ("join",),
        "accepts_argument": False,
    },
    "reset": {
        "aliases": ("reset",),
        "display": ("reset",),
        "accepts_argument": True,
    },
    "set_lang": {
        "aliases": ("set lang",),
        "display": ("set lang",),
        "accepts_argument": True,
    },
    "panel_user": {
        "aliases": ("tts", "panel", "painel", "p"),
        "display": ("tts", "panel", "p"),
        # `_tts @usuário` e aliases legados abrem um painel de edição de usuário
        # para staff. O dispatcher valida o alvo antes de consumir `_p ...`,
        # porque `_p` também pode existir como alias do player de música.
        "accepts_argument": True,
    },
    "panel_server": {
        "aliases": (
            "panel_server", "panel-server", "panelserver",
            "server_panel", "server-panel", "serverpanel",
            "painel_server", "painel-server", "painelserver",
            "servidor_panel", "servidor-panel", "servidorpanel",
            "sp",
        ),
        "display": ("panel_server", "sp"),
        "accepts_argument": False,
    },
    "panel_toggle": {
        "aliases": (
            "panel_toggle", "panel-toggle", "paneltoggle",
            "panel_toggles", "panel-toggles", "paneltoggles",
            "toggle_panel", "toggle-panel", "togglepanel",
            "toggles_panel", "toggles-panel", "togglespanel",
            "tp",
        ),
        "display": ("toggle_panel", "tp"),
        "accepts_argument": False,
    },
}


def iter_prefix_command_kinds() -> tuple[str, ...]:
    return tuple(_PREFIX_CONTROL_SPECS.keys())


def get_prefix_command_spec(kind: str) -> dict[str, object]:
    return dict(_PREFIX_CONTROL_SPECS.get(kind, {}))


@lru_cache(maxsize=1024)
def get_prefixed_aliases(bot_prefix: str, kind: str, *, display: bool = False) -> tuple[str, ...]:
    spec = _PREFIX_CONTROL_SPECS.get(kind, {})
    key = "display" if display else "aliases"
    stems = tuple(spec.get(key, ()) or ())
    return tuple(f"{bot_prefix}{stem}" for stem in stems)


def command_accepts_argument(kind: str) -> bool:
    spec = _PREFIX_CONTROL_SPECS.get(kind, {})
    return bool(spec.get("accepts_argument", False))


def matches_prefixed_command(content: str, bot_prefix: str, *, kind: str) -> bool:
    raw = str(content or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    for alias in get_prefixed_aliases(bot_prefix, kind, display=False):
        alias_lower = alias.lower()
        if command_accepts_argument(kind):
            if lowered == alias_lower or lowered.startswith(alias_lower + " "):
                return True
        elif lowered == alias_lower:
            return True
    return False


def extract_prefixed_argument(content: str, bot_prefix: str, *, kind: str) -> str:
    raw = str(content or "").strip()
    lowered = raw.lower()
    for alias in get_prefixed_aliases(bot_prefix, kind, display=False):
        alias_lower = alias.lower()
        if lowered == alias_lower:
            return ""
        if lowered.startswith(alias_lower + " "):
            return raw[len(alias):].strip()
    return ""


def format_prefixed_aliases(bot_prefix: str, kind: str, *, display: bool = True) -> str:
    aliases = get_prefixed_aliases(bot_prefix, kind, display=display)
    if not aliases:
        return ""
    wrapped = [f"`{alias}`" for alias in aliases]
    if len(wrapped) == 1:
        return wrapped[0]
    if len(wrapped) == 2:
        return f"{wrapped[0]} ou {wrapped[1]}"
    return ", ".join(wrapped[:-1]) + f" ou {wrapped[-1]}"
