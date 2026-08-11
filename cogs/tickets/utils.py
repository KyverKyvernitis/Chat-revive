from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import discord

from .constants import (
    CUSTOM_OPTION_PREFIX,
    DEFAULT_OPTION_ITEMS,
    DEFAULT_REPORT_TYPES,
    FLOW_CONFIRM_TICKET,
    FLOW_DIRECT_TICKET,
    FLOW_MODAL_CHANNEL,
    FLOW_MODAL_TICKET,
    MAX_PANEL_OPTIONS,
    OPTION_FLOWS,
    PUBLIC_OPTIONS,
    TICKET_KINDS,
    default_ticket_config,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate(text: object, limit: int, *, suffix: str = "…") -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    if limit <= len(suffix):
        return value[:limit]
    return value[: limit - len(suffix)] + suffix


def clean_accent_hex(raw: object, *, fallback: str = "#5865F2") -> str:
    value = str(raw or "").strip()
    if not value:
        value = fallback
    if value.startswith("#"):
        value = value[1:]
    elif value.lower().startswith("0x"):
        value = value[2:]
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return f"#{value.upper()}"
    fallback = str(fallback or "#5865F2").strip()
    if fallback.startswith("#"):
        return fallback.upper()
    return "#5865F2"


def clean_panel_image_url(raw: object) -> str:
    value = truncate(str(raw or "").strip(), 500, suffix="")
    if not value:
        return ""
    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return value
    return ""


def accent_color(raw: object, *, fallback: str = "#5865F2") -> discord.Color:
    hex_value = clean_accent_hex(raw, fallback=fallback)
    return discord.Color(int(hex_value[1:], 16))


def parse_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "sim", "s", "ligado", "ativado"}:
        return True
    if text in {"0", "false", "no", "n", "off", "não", "nao", "desligado", "desativado"}:
        return False
    return default


def id_from_mention_or_text(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r"\d{15,25}", text)
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def normalize_report_types(values: object) -> list[str]:
    if isinstance(values, str):
        raw_items = re.split(r"[\n;,]+", values)
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        label = truncate(str(item or "").strip(), 80, suffix="")
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(label)
        if len(result) >= 10:
            break
    return result or list(DEFAULT_REPORT_TYPES)




CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")
CUSTOM_EMOJI_LIKE_RE = re.compile(r"^<a?:|^<|>$")


def _looks_like_unicode_emoji(value: str) -> bool:
    """Retorna True para emojis unicode comuns sem aceitar texto ASCII puro.

    `discord.SelectOption(emoji=...)` aceita unicode e PartialEmoji, mas valores
    inválidos fazem o Discord rejeitar o componente inteiro. Por isso, se alguém
    salvar `abc`, `<:emoji:123` truncado ou outro texto quebrado, usamos fallback
    em vez de quebrar Preview/Textos/Opções do painel.
    """
    if not value or len(value) > 32:
        return False
    for ch in value:
        code = ord(ch)
        category = unicodedata.category(ch)
        if category in {"So", "Sk"}:
            return True
        if ch in {"\ufe0f", "\u200d", "\u20e3"}:
            return True
        if 0x1F000 <= code <= 0x1FAFF:
            return True
    return False


def _sanitize_option_emoji_value(raw: object, *, fallback: str = "🎫") -> str:
    value = truncate(str(raw or "").strip(), 120, suffix="")
    fallback_value = truncate(str(fallback or "🎫").strip(), 120, suffix="") or "🎫"
    if not value:
        return fallback_value
    if CUSTOM_EMOJI_RE.match(value):
        return value
    # Emoji custom truncado/quebrado: nunca enviar para SelectOption.
    if CUSTOM_EMOJI_LIKE_RE.search(value):
        return fallback_value
    if _looks_like_unicode_emoji(value):
        return value
    return fallback_value


def clean_option_emoji(raw: object, *, fallback: str = "🎫") -> str:
    return _sanitize_option_emoji_value(raw, fallback=fallback)


def option_emoji_for_select(raw: object, *, fallback: str = "🎫") -> object | None:
    value = clean_option_emoji(raw, fallback=fallback)
    if not value:
        return None
    if CUSTOM_EMOJI_RE.match(value):
        try:
            return discord.PartialEmoji.from_str(value)
        except Exception:
            return None
    if _looks_like_unicode_emoji(value):
        return value
    return None


def option_emoji_text(raw: object, *, fallback: str = "🎫") -> str:
    return clean_option_emoji(raw, fallback=fallback)


def normalize_option_id(raw: object, *, fallback: str = "custom") -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_\-]+", "_", text).strip("_-")
    return truncate(text or fallback, 48, suffix="")


def _normalize_option_item(option_id: str, raw: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = dict(defaults or {})
    raw = raw if isinstance(raw, dict) else {}
    fallback_label = defaults.get("label") or "Nova opção"
    fallback_emoji = defaults.get("emoji") or "➕"
    flow = str(raw.get("flow") or defaults.get("flow") or FLOW_MODAL_TICKET)
    if flow not in OPTION_FLOWS:
        flow = FLOW_MODAL_TICKET
    result = {
        "id": option_id,
        "builtin": bool(defaults.get("builtin", False)),
        "enabled": bool(raw.get("enabled", defaults.get("enabled", True))),
        "label": truncate(str(raw.get("label") or fallback_label), 80, suffix=""),
        "emoji": clean_option_emoji(raw.get("emoji") or fallback_emoji, fallback=fallback_emoji),
        "description": truncate(str(raw.get("description") or defaults.get("description") or "Abrir atendimento."), 100, suffix=""),
        "flow": flow,
        "confirmation_text": truncate(str(raw.get("confirmation_text") or defaults.get("confirmation_text") or "Ao confirmar, criaremos um ticket privado para você."), 1800, suffix=""),
        "opening_text": truncate(str(raw.get("opening_text") or defaults.get("opening_text") or "A equipe irá analisar sua solicitação."), 1800, suffix=""),
        "modal_title": truncate(str(raw.get("modal_title") or defaults.get("modal_title") or "Abrir ticket"), 45, suffix=""),
        "modal_notice": truncate(str(raw.get("modal_notice") if raw.get("modal_notice") is not None else defaults.get("modal_notice") or ""), 350, suffix=""),
        "subject_label": truncate(str(raw.get("subject_label") or defaults.get("subject_label") or "Assunto"), 45, suffix=""),
        "body_label": truncate(str(raw.get("body_label") or defaults.get("body_label") or "Explique o que você precisa"), 45, suffix=""),
        "target_channel_id": int(raw.get("target_channel_id") or defaults.get("target_channel_id") or 0),
        "use_report_types": bool(raw.get("use_report_types", defaults.get("use_report_types", False))),
    }
    if not result["label"]:
        result["label"] = "Nova opção"
    if not result["subject_label"]:
        result["subject_label"] = "Assunto"
    if not result["body_label"]:
        result["body_label"] = "Explique o que você precisa"
    return result


def iter_ticket_options(cfg: dict[str, Any], *, include_disabled: bool = True) -> list[dict[str, Any]]:
    raw_items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
    ordered_ids = list(TICKET_KINDS)
    custom_ids = sorted(
        (str(key) for key in raw_items.keys() if str(key) not in set(TICKET_KINDS)),
        key=lambda value: int(re.search(r"(\d+)$", value).group(1)) if re.search(r"(\d+)$", value) else 9999,
    )
    ordered_ids.extend(custom_ids)
    result: list[dict[str, Any]] = []
    for option_id in ordered_ids:
        item = raw_items.get(option_id)
        if not isinstance(item, dict):
            continue
        if include_disabled or bool(item.get("enabled", True)):
            result.append(dict(item))
    return result[:MAX_PANEL_OPTIONS]


def get_ticket_option(cfg: dict[str, Any], option_id: str) -> dict[str, Any] | None:
    option_id = str(option_id or "")
    items = cfg.get("option_items") if isinstance(cfg.get("option_items"), dict) else {}
    item = items.get(option_id)
    if isinstance(item, dict):
        return dict(item)
    if option_id in DEFAULT_OPTION_ITEMS:
        return dict(DEFAULT_OPTION_ITEMS[option_id])
    return None


def create_custom_ticket_option(cfg: dict[str, Any]) -> dict[str, Any] | None:
    cfg.setdefault("option_items", {})
    items = cfg["option_items"] if isinstance(cfg.get("option_items"), dict) else {}
    if len(items) >= MAX_PANEL_OPTIONS:
        return None
    try:
        next_number = max(1, int(cfg.get("next_custom_option_number") or 1))
    except Exception:
        next_number = 1
    existing = set(str(key) for key in items.keys())
    while f"{CUSTOM_OPTION_PREFIX}{next_number}" in existing:
        next_number += 1
    option_id = f"{CUSTOM_OPTION_PREFIX}{next_number}"
    item = _normalize_option_item(
        option_id,
        {
            "enabled": True,
            "label": f"Nova opção {next_number}",
            "emoji": "➕",
            "description": "Abrir atendimento personalizado.",
            "flow": FLOW_MODAL_TICKET,
            "opening_text": "A equipe irá analisar sua solicitação. Explique aqui o que você precisa.",
            "modal_title": "Abrir atendimento",
        },
        {"builtin": False, "enabled": True, "emoji": "➕"},
    )
    item["builtin"] = False
    items[option_id] = item
    cfg["option_items"] = items
    cfg["next_custom_option_number"] = next_number + 1
    return item

def sanitize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    base = default_ticket_config()
    raw = cfg if isinstance(cfg, dict) else {}

    if "feature_enabled" in raw:
        base["feature_enabled"] = bool(raw.get("feature_enabled"))
    else:
        legacy_panel = raw.get("panel") if isinstance(raw.get("panel"), dict) else {}
        base["feature_enabled"] = bool(int(legacy_panel.get("channel_id") or 0) and int(legacy_panel.get("message_id") or 0))

    for section in ("panel", "channels", "roles", "enabled", "options", "texts"):
        payload = raw.get(section) if isinstance(raw.get(section), dict) else {}
        base[section].update(payload)

    raw_permissions = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
    for scope, defaults in list((base.get("permissions") or {}).items()):
        payload = raw_permissions.get(scope) if isinstance(raw_permissions.get(scope), dict) else {}
        if isinstance(defaults, dict):
            defaults.update(payload)
            for key, value in list(defaults.items()):
                defaults[key] = bool(value)

    base["panel"]["channel_id"] = int(base["panel"].get("channel_id") or 0)
    base["panel"]["message_id"] = int(base["panel"].get("message_id") or 0)
    base["panel"]["title"] = truncate(base["panel"].get("title") or "🎫 Atendimento", 200, suffix="")
    base["panel"]["description"] = truncate(base["panel"].get("description") or "Escolha abaixo o tipo de atendimento.", 1800, suffix="")
    base["panel"]["placeholder"] = truncate(base["panel"].get("placeholder") or "Escolha uma opção", 100, suffix="")
    base["panel"]["accent_color"] = clean_accent_hex(base["panel"].get("accent_color"))
    base["panel"]["image_url"] = clean_panel_image_url(base["panel"].get("image_url"))
    base["panel"]["side_image_url"] = clean_panel_image_url(base["panel"].get("side_image_url"))

    for key in ("category_id", "logs_channel_id", "suggestions_channel_id"):
        base["channels"][key] = int(base["channels"].get(key) or 0)
    for key in ("staff_role_id", "partnership_staff_role_id", "report_staff_role_id", "other_staff_role_id"):
        base["roles"][key] = int(base["roles"].get(key) or 0)
    for key in TICKET_KINDS:
        base["enabled"][key] = bool(base["enabled"].get(key, True))

    base["options"]["allow_multiple_open_tickets"] = bool(base["options"].get("allow_multiple_open_tickets", False))
    base["options"]["transcript_on_close"] = bool(base["options"].get("transcript_on_close", True))
    base["options"]["use_server_webhook"] = bool(base["options"].get("use_server_webhook", False))

    for key, value in list(base["texts"].items()):
        base["texts"][key] = truncate(str(value or ""), 1800, suffix="")

    # Migração: os textos antigos continuam valendo como texto padrão das opções nativas.
    default_items = {key: dict(value) for key, value in DEFAULT_OPTION_ITEMS.items()}
    default_items["partnership"]["confirmation_text"] = base["texts"].get("partnership_confirm") or default_items["partnership"].get("confirmation_text")
    default_items["partnership"]["opening_text"] = base["texts"].get("partnership_opening") or default_items["partnership"].get("opening_text")
    default_items["report"]["modal_notice"] = base["texts"].get("report_modal_notice") or default_items["report"].get("modal_notice")
    default_items["report"]["opening_text"] = base["texts"].get("report_opening") or default_items["report"].get("opening_text")
    default_items["other"]["opening_text"] = base["texts"].get("other_opening") or default_items["other"].get("opening_text")
    default_items["suggestion"]["opening_text"] = base["texts"].get("suggestion_published") or default_items["suggestion"].get("opening_text")

    raw_items = raw.get("option_items") if isinstance(raw.get("option_items"), dict) else {}
    normalized_items: dict[str, dict[str, Any]] = {}
    for kind in TICKET_KINDS:
        merged = dict(default_items[kind])
        if isinstance(raw_items.get(kind), dict):
            merged.update(raw_items.get(kind) or {})
        # Migração da chave enabled antiga.
        merged["enabled"] = bool(base.get("enabled", {}).get(kind, merged.get("enabled", True))) if not raw_items else bool(merged.get("enabled", True))
        normalized_items[kind] = _normalize_option_item(kind, merged, default_items[kind])

    for raw_id, raw_item in raw_items.items():
        option_id = normalize_option_id(raw_id)
        if not option_id or option_id in normalized_items or option_id in TICKET_KINDS:
            continue
        if not isinstance(raw_item, dict):
            continue
        item = _normalize_option_item(option_id, raw_item, {"builtin": False, "enabled": True, "emoji": "➕"})
        item["builtin"] = False
        normalized_items[option_id] = item
        if len(normalized_items) >= MAX_PANEL_OPTIONS:
            break

    base["option_items"] = normalized_items
    base["enabled"] = {kind: bool(normalized_items.get(kind, {}).get("enabled", True)) for kind in TICKET_KINDS}

    base["report_types"] = normalize_report_types(raw.get("report_types") or base.get("report_types"))
    try:
        base["next_ticket_number"] = max(1, int(raw.get("next_ticket_number") or 1))
    except Exception:
        base["next_ticket_number"] = 1
    try:
        guessed_next_custom = max(
            [int(match.group(1)) + 1 for key in normalized_items for match in [re.search(r"custom_(\d+)$", str(key))] if match] or [1]
        )
        base["next_custom_option_number"] = max(guessed_next_custom, int(raw.get("next_custom_option_number") or 1))
    except Exception:
        base["next_custom_option_number"] = 1

    active_tickets = raw.get("active_tickets") or []
    normalized_active: list[dict[str, Any]] = []
    valid_kinds = set(normalized_items.keys()) | {"ticket"}
    if isinstance(active_tickets, list):
        seen_channels: set[int] = set()
        for item in active_tickets:
            if not isinstance(item, dict):
                continue
            channel_id = int(item.get("channel_id") or 0)
            user_id = int(item.get("user_id") or 0)
            if not channel_id or not user_id or channel_id in seen_channels:
                continue
            kind = str(item.get("kind") or "other")
            if kind not in valid_kinds:
                kind = "other"
            option = normalized_items.get(kind) or normalized_items.get("other") or {}
            seen_channels.add(channel_id)
            normalized_active.append({
                "ticket_id": int(item.get("ticket_id") or 0),
                "channel_id": channel_id,
                "control_message_id": int(item.get("control_message_id") or 0),
                "user_id": user_id,
                "kind": kind,
                "created_at": str(item.get("created_at") or ""),
                "label": str(item.get("label") or option.get("label") or PUBLIC_OPTIONS.get(kind, {}).get("label") or kind),
            })
    base["active_tickets"] = normalized_active[-300:]
    return base


def slugify_channel_part(value: object, *, fallback: str = "ticket") -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return truncate(ascii_text or fallback, 48, suffix="").strip("-") or fallback


def is_staff(member: discord.Member | None, cfg: dict[str, Any] | None = None) -> bool:
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_guild or perms.manage_channels):
        return True
    cfg = cfg or {}
    roles_cfg = cfg.get("roles") or {}
    allowed = {int(roles_cfg.get(key) or 0) for key in roles_cfg}
    allowed.discard(0)
    if not allowed:
        return False
    return any(int(getattr(role, "id", 0) or 0) in allowed for role in getattr(member, "roles", []) or [])


def member_display(member: discord.abc.User | discord.Member | None) -> str:
    if member is None:
        return "desconhecido"
    mention = getattr(member, "mention", None)
    if mention:
        return str(mention)
    return str(member)
