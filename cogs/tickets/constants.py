from __future__ import annotations

from copy import deepcopy
from typing import Any

TICKET_COMMAND_COOLDOWN = 10.0
TICKET_COMMAND_CLEANUP_DELAY = 6.0
EDITOR_TIMEOUT = 900.0
TRANSCRIPT_FETCH_LIMIT = 1000

KIND_PARTNERSHIP = "partnership"
KIND_REPORT = "report"
KIND_SUGGESTION = "suggestion"
KIND_OTHER = "other"
TICKET_KINDS = (KIND_PARTNERSHIP, KIND_REPORT, KIND_SUGGESTION, KIND_OTHER)

FLOW_CONFIRM_TICKET = "confirm_ticket"
FLOW_MODAL_TICKET = "modal_ticket"
FLOW_MODAL_CHANNEL = "modal_channel"
FLOW_DIRECT_TICKET = "direct_ticket"
OPTION_FLOWS = (FLOW_CONFIRM_TICKET, FLOW_MODAL_TICKET, FLOW_MODAL_CHANNEL, FLOW_DIRECT_TICKET)
CUSTOM_OPTION_PREFIX = "custom_"
MAX_PANEL_OPTIONS = 20
ADD_CUSTOM_OPTION_VALUE = "__add_custom_option__"

PUBLIC_OPTIONS: dict[str, dict[str, str]] = {
    KIND_PARTNERSHIP: {
        "label": "Parceria",
        "emoji": "🤝",
        "description": "Criar um ticket privado de parceria.",
    },
    KIND_REPORT: {
        "label": "Denúncia",
        "emoji": "👾",
        "description": "Enviar uma denúncia e abrir um ticket privado.",
    },
    KIND_SUGGESTION: {
        "label": "Sugestão",
        "emoji": "⚡",
        "description": "Enviar uma sugestão para o canal configurado.",
    },
    KIND_OTHER: {
        "label": "Outros",
        "emoji": "⚙️",
        "description": "Abrir um ticket para outros assuntos.",
    },
}

DEFAULT_REPORT_TYPES = [
    "Spam",
    "Flood",
    "Ofensa",
    "Assédio",
    "Golpe",
    "Divulgação indevida",
    "Conteúdo impróprio",
    "Raid",
    "Fake account",
    "Outro",
]

DEFAULT_TEXTS: dict[str, str] = {
    "partnership_confirm": "Ao confirmar, criaremos um ticket privado para você conversar com a equipe responsável por parcerias.",
    "partnership_opening": "A equipe irá analisar sua solicitação. Envie aqui as informações da parceria.",
    "report_modal_notice": "Ao enviar este formulário, criaremos um ticket privado para você conversar com a equipe. Use esse atendimento apenas para denúncias reais.",
    "report_opening": "A equipe irá analisar a denúncia. Envie provas adicionais aqui, se necessário.",
    "other_opening": "Explique aqui o que você precisa e aguarde a equipe.",
    "suggestion_published": "Nova sugestão enviada para análise.",
    "close_notice": "Este ticket será fechado em alguns segundos.",
}

DEFAULT_OPTION_ITEMS: dict[str, dict[str, Any]] = {
    KIND_PARTNERSHIP: {
        "id": KIND_PARTNERSHIP,
        "builtin": True,
        "enabled": True,
        "label": PUBLIC_OPTIONS[KIND_PARTNERSHIP]["label"],
        "emoji": PUBLIC_OPTIONS[KIND_PARTNERSHIP]["emoji"],
        "description": PUBLIC_OPTIONS[KIND_PARTNERSHIP]["description"],
        "flow": FLOW_CONFIRM_TICKET,
        "confirmation_text": DEFAULT_TEXTS["partnership_confirm"],
        "opening_text": DEFAULT_TEXTS["partnership_opening"],
        "modal_title": "Abrir parceria",
        "modal_notice": "",
        "subject_label": "Assunto",
        "body_label": "Explique o atendimento",
        "target_channel_id": 0,
        "use_report_types": False,
    },
    KIND_REPORT: {
        "id": KIND_REPORT,
        "builtin": True,
        "enabled": True,
        "label": PUBLIC_OPTIONS[KIND_REPORT]["label"],
        "emoji": PUBLIC_OPTIONS[KIND_REPORT]["emoji"],
        "description": PUBLIC_OPTIONS[KIND_REPORT]["description"],
        "flow": FLOW_MODAL_TICKET,
        "confirmation_text": "",
        "opening_text": DEFAULT_TEXTS["report_opening"],
        "modal_title": "Enviar denúncia",
        "modal_notice": DEFAULT_TEXTS["report_modal_notice"],
        "subject_label": "Usuário denunciado, se houver",
        "body_label": "Descrição do ocorrido",
        "target_channel_id": 0,
        "use_report_types": True,
    },
    KIND_SUGGESTION: {
        "id": KIND_SUGGESTION,
        "builtin": True,
        "enabled": True,
        "label": PUBLIC_OPTIONS[KIND_SUGGESTION]["label"],
        "emoji": PUBLIC_OPTIONS[KIND_SUGGESTION]["emoji"],
        "description": PUBLIC_OPTIONS[KIND_SUGGESTION]["description"],
        "flow": FLOW_MODAL_CHANNEL,
        "confirmation_text": "",
        "opening_text": DEFAULT_TEXTS["suggestion_published"],
        "modal_title": "Enviar sugestão",
        "modal_notice": "",
        "subject_label": "Título da sugestão",
        "body_label": "Descrição da sugestão",
        "target_channel_id": 0,
        "use_report_types": False,
    },
    KIND_OTHER: {
        "id": KIND_OTHER,
        "builtin": True,
        "enabled": True,
        "label": PUBLIC_OPTIONS[KIND_OTHER]["label"],
        "emoji": PUBLIC_OPTIONS[KIND_OTHER]["emoji"],
        "description": PUBLIC_OPTIONS[KIND_OTHER]["description"],
        "flow": FLOW_MODAL_TICKET,
        "confirmation_text": "",
        "opening_text": DEFAULT_TEXTS["other_opening"],
        "modal_title": "Abrir ticket",
        "modal_notice": "",
        "subject_label": "Assunto",
        "body_label": "Explique o que você precisa",
        "target_channel_id": 0,
        "use_report_types": False,
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "feature_enabled": False,
    "panel": {
        "channel_id": 0,
        "message_id": 0,
        "title": "🎫 Atendimento",
        "description": "Escolha abaixo o tipo de atendimento.",
        "placeholder": "Escolha uma opção",
        "accent_color": "#5865F2",
        "image_url": "",
        "side_image_url": "",
    },
    "channels": {
        "category_id": 0,
        "logs_channel_id": 0,
        "suggestions_channel_id": 0,
    },
    "roles": {
        "staff_role_id": 0,
        "partnership_staff_role_id": 0,
        "report_staff_role_id": 0,
        "other_staff_role_id": 0,
    },
    "enabled": {
        KIND_PARTNERSHIP: True,
        KIND_REPORT: True,
        KIND_SUGGESTION: True,
        KIND_OTHER: True,
    },
    "options": {
        "allow_multiple_open_tickets": False,
        "transcript_on_close": True,
        "use_server_webhook": False,
    },
    "permissions": {
        "everyone": {
            "view_channel": False,
            "send_messages": False,
            "read_message_history": False,
            "attach_files": False,
            "embed_links": False,
            "add_reactions": False,
        },
        "staff": {
            "view_channel": True,
            "send_messages": True,
            "read_message_history": True,
            "attach_files": True,
            "embed_links": True,
            "add_reactions": True,
            "manage_messages": True,
            "manage_channels": False,
        },
        "creator": {
            "view_channel": True,
            "send_messages": True,
            "read_message_history": True,
            "attach_files": True,
            "embed_links": True,
            "add_reactions": True,
            "mention_everyone": False,
        },
    },
    "texts": dict(DEFAULT_TEXTS),
    "option_items": deepcopy(DEFAULT_OPTION_ITEMS),
    "report_types": list(DEFAULT_REPORT_TYPES),
    "next_ticket_number": 1,
    "next_custom_option_number": 1,
    "active_tickets": [],
}


def default_ticket_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)
