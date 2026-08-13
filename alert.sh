#!/usr/bin/env bash
set -u

ENV_FILE="/home/ubuntu/bot/.env"
HOSTNAME="$(hostname)"
NOW="$(date '+%d/%m/%Y %H:%M:%S')"
NOW_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

TYPE="${1:-info}"
TITLE="${2:-Sem título}"
BODY="${3:-}"
ATTACH_FILE="${4:-}"
ATTACH_NAME="${5:-}"

if [ -z "${ALERT_WEBHOOK_URL:-}" ] && [ "${ALERT_DRY_RUN:-0}" != "1" ]; then
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  exit 1
fi

if [ "${ALERT_DRY_RUN:-0}" != "1" ] && ! command -v curl >/dev/null 2>&1; then
  exit 1
fi

PAYLOAD_JSON="$({
TYPE="$TYPE" \
TITLE="$TITLE" \
BODY="$BODY" \
HOSTNAME="$HOSTNAME" \
NOW="$NOW" \
NOW_ISO="$NOW_ISO" \
python3 - <<'PY'
import json
import os
import re
from collections import OrderedDict

TYPE = os.environ.get("TYPE", "info").strip().lower()
TITLE = os.environ.get("TITLE", "Sem título").strip()
BODY = os.environ.get("BODY", "")
HOSTNAME = os.environ.get("HOSTNAME", "unknown")
NOW = os.environ.get("NOW", "")
NOW_ISO = os.environ.get("NOW_ISO", "")

COMPONENTS_V2_FLAG = 1 << 15
TEXT_DISPLAY = 10
SEPARATOR = 14
CONTAINER = 17

COLOR_MAP = {
    "error": 0xED4245,
    "warn": 0xF5A524,
    "success": 0x57F287,
    "update": 0x5865F2,
    "info": 0x3BA55D,
}

EMOJI_MAP = {
    "error": "❌",
    "warn": "⚠️",
    "success": "✅",
    "update": "🔄",
    "info": "ℹ️",
}

LABEL_MAP = {
    "resumo": "Resumo",
    "host": "Host",
    "branch": "Branch",
    "commit": "Commit",
    "mudança": "Mudança",
    "mudanca": "Mudança",
    "arquivos": "Arquivos",
    "arquivos alterados": "Arquivos",
    "bot": "Bot health",
    "bot health": "Bot health",
    "bot healthcheck": "Bot health",
    "frontend": "Frontend",
    "backend": "Backend",
    "activity": "Activity",
    "rollback": "Rollback",
    "duração": "Duração",
    "duracao": "Duração",
    "hora": "Hora",
    "motivo": "Motivo",
    "url": "URL",
    "etapa": "Etapa",
    "comando": "Comando",
    "serviço": "Serviço",
    "servico": "Serviço",
    "activestate": "Estado ativo",
    "substate": "Subestado",
    "result": "Resultado",
    "execmaincode": "Código",
    "execmainstatus": "Status",
    "stderr": "Stderr",
    "saída do erro": "Stderr",
    "saida do erro": "Stderr",
    "commit anterior": "Commit anterior",
    "commit alvo": "Commit alvo",
    "commit sujo": "Commit sujo",
    "serviço afetado": "Serviço afetado",
    "servico afetado": "Serviço afetado",
    "update": "Update",
    "zip": "ZIP",
    "health": "Health",
    "aplicação": "Aplicação",
    "aplicacao": "Aplicação",
    "últimas linhas": "Últimas linhas",
    "ultimas linhas": "Últimas linhas",
    "últimas linhas do erro": "Últimas linhas",
    "ultimas linhas do erro": "Últimas linhas",
    "ação sugerida": "Ação sugerida",
    "acao sugerida": "Ação sugerida",
    "arquivos locais": "Arquivos locais",
    "status git": "Status git",
    "diagnóstico": "Diagnóstico",
    "diagnostico": "Diagnóstico",
    "log": "Log",
    "log completo": "Log completo",
    "serviços de áudio": "Serviços de áudio",
    "servicos de audio": "Serviços de áudio",
    "limpeza de áudio": "Limpeza de áudio",
    "limpeza de audio": "Limpeza de áudio",
    "watcher lavalink celular": "Watcher Lavalink celular",
    "phone worker": "Phone-worker",
    "phone-worker": "Phone-worker",
    "phone-worker sync": "Phone-worker sync",
    "análise phone-worker": "Análise phone-worker",
    "analise phone-worker": "Análise phone-worker",
    "callkeeper": "CallKeeper",
    "validações": "Validações",
    "validacoes": "Validações",
    "cogs": "Cogs",
    "avisos": "Avisos",
    "serviços": "Serviços",
    "servicos": "Serviços",
    "core worker": "Core Worker",
    "processos alterados": "Processos alterados",
    "processo alterado": "Processos alterados",
    "identificador": "Identificador",
    "id da atualização": "Identificador",
    "id da atualizacao": "Identificador",
    "verificações": "Verificações",
    "verificacoes": "Verificações",
    "tempos": "Tempos",
    "tentativa": "Tentativa",
}

HEADER_FIELDS = {
    "Host",
    "Branch",
    "Commit",
    "Commit anterior",
    "Commit alvo",
    "Mudança",
    "Rollback",
    "Commit sujo",
    "Duração",
    "Serviço",
    "Serviço afetado",
    "Update",
    "ZIP",
    "Aplicação",
    "Processos alterados",
    "Resultado",
    "Código",
    "Status",
    "Estado ativo",
    "Subestado",
    "Identificador",
    "Tentativa",
}
STATUS_FIELDS = {"Bot health", "Health", "Serviços de áudio", "Limpeza de áudio", "Watcher Lavalink celular", "Phone-worker", "Phone-worker sync", "CallKeeper", "Frontend", "Backend", "Activity", "Cogs", "Avisos"}
DETAIL_FIELDS = {"Etapa", "Motivo", "URL", "Diagnóstico", "Análise phone-worker", "Validações", "Verificações", "Tempos", "Serviços", "Core Worker"}
BULLET_FIELDS = {"Arquivos"}
CHECK_FIELDS = {"Verificações", "Validações"}
CODE_FIELDS = {"Últimas linhas", "Comando", "Stderr", "Status git", "Arquivos locais", "Log", "Log completo"}
MAX_TEXT = 1800
MAX_TOTAL_TEXT = 3800
MAX_CODE = 1200


def trunc(value: str, limit: int) -> str:
    value = (value or "").strip()
    if not value:
        return "—"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def normalize_lines(body: str):
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def strip_outer_code_fences(value: str) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while True:
        match = re.match(r"^```[A-Za-z0-9_-]*\n?(.*?)\n?```$", text, flags=re.DOTALL)
        if not match:
            return text
        inner = match.group(1).strip("\n").strip()
        if inner == text:
            return text
        text = inner


def strip_lonely_fence_lines(value: str) -> str:
    cleaned = []
    for raw in (value or "").splitlines():
        stripped = raw.strip()
        if stripped in {"`", "``", "```"}:
            continue
        if re.fullmatch(r"```[A-Za-z0-9_-]+", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()


def clean_field_text(value: str) -> str:
    text = (value or "").replace("\t", "  ")
    text = strip_outer_code_fences(text)
    text = strip_lonely_fence_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_multiline_bullets(value: str) -> str:
    raw = clean_field_text(value)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return "—"
    normalized = []
    for line in lines:
        line = re.sub(r"^[•\-]\s*", "", line).strip()
        if line.startswith("`") and line.endswith("`") and len(line) >= 2:
            line = line[1:-1].strip()
        line = line.strip("`").strip()
        if not line or line in {"—", "`", "``", "```"}:
            continue
        normalized.append(f"- `{line}`")
    return trunc("\n".join(normalized) if normalized else "—", 1500)


def format_code_block(value: str) -> str:
    raw = clean_field_text(value) or "—"
    raw = raw.replace("```", "ʼʼʼ")
    raw = trunc(raw, MAX_CODE)
    return f"```text\n{raw}\n```"


def format_field_value(name: str, value: str) -> str:
    value = clean_field_text(value) or "—"
    if name in BULLET_FIELDS:
        return format_multiline_bullets(value)
    if name in CHECK_FIELDS:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return trunc("\n".join(lines) if lines else "—", 1500)
    if name in CODE_FIELDS:
        return format_code_block(value)
    if "\n" in value and len([line for line in value.splitlines() if line.strip()]) >= 3:
        return format_code_block(value)
    return trunc(value, 1200)


def parse_body(body: str):
    lines = normalize_lines(body)
    fields = []
    description = ""
    footer = NOW
    current_idx = None

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue

        match = re.match(r"^([^:]{1,48}):\s*(.*)$", stripped)
        if match:
            raw_label = match.group(1).strip()
            value = match.group(2).rstrip()
            key = raw_label.lower()
            label = LABEL_MAP.get(key, raw_label[:48])

            if key == "resumo":
                description = trunc(value, 1200)
                current_idx = None
                continue

            if key == "hora":
                footer = value or footer
                current_idx = None
                continue

            if key == "host" and not value:
                value = HOSTNAME

            initial_value = format_field_value(label, value) if value else ""
            fields.append({"name": trunc(label, 80), "value": initial_value})
            current_idx = len(fields) - 1
            continue

        if current_idx is not None:
            prev = fields[current_idx]["value"]
            if prev.startswith("```text\n") and prev.endswith("\n```"):
                prev_plain = prev[len("```text\n"):-len("\n```")]
            else:
                prev_plain = "" if prev == "—" else prev
            joined = stripped if not prev_plain else f"{prev_plain}\n{stripped}"
            fields[current_idx]["value"] = format_field_value(fields[current_idx]["name"], joined)
        elif description:
            description = trunc(f"{description}\n{stripped}", 1200)
        else:
            description = trunc(stripped, 1200)

    cleaned = []
    for field in fields:
        value = field["value"].strip()
        if field["name"].strip().lower() == "arquivos" and value in {"", "—"}:
            continue
        if not value:
            field["value"] = "—"
        cleaned.append(field)
    return description or "Notificação automática.", cleaned[:25], footer or NOW


def split_text(text: str, limit: int = MAX_TEXT):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 3:
            split_at = limit
        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return pieces


def make_text(content: str):
    return {"type": TEXT_DISPLAY, "content": content}


def make_separator():
    return {"type": SEPARATOR, "divider": True, "spacing": 1}


def render_field_block(title: str, pairs):
    if not pairs:
        return []
    lines = [f"## {title}"]
    for name, value in pairs:
        value = (value or "—").strip() or "—"
        if value.startswith("```") or "\n" in value:
            lines.extend(["", f"### {name}", value])
        else:
            lines.append(f"- **{name}:** {value}")
    joined = "\n".join(lines)
    return [make_text(chunk) for chunk in split_text(joined)]


def render_code_block(name: str, value: str):
    raw = value.strip()
    block = raw if raw.startswith("```") else format_code_block(raw)
    return [make_text(chunk) for chunk in split_text(f"## {name}\n{block}")]


def append_container(components, color, children):
    normalized = []
    for child in children:
        if not child:
            continue
        if child["type"] == TEXT_DISPLAY:
            content = child.get("content", "").strip()
            if not content:
                continue
            normalized.append({"type": TEXT_DISPLAY, "content": content})
        else:
            normalized.append(child)
    if normalized:
        components.append({"type": CONTAINER, "accent_color": color, "components": normalized[:10]})


def is_update_notification(title: str, fields) -> bool:
    lowered = (title or "").casefold()
    names = {field.get("name") for field in fields}
    return ("update" in lowered or "atualiza" in lowered) and bool(names & {"Update", "Aplicação", "Identificador", "Commit"})


def build_update_containers(title: str, summary: str, fields, footer: str, color: int):
    field_map = OrderedDict((field["name"], field["value"]) for field in fields)
    identifier = field_map.pop("Identificador", "")
    branch = field_map.pop("Branch", "")
    commit = field_map.pop("Commit", "")
    field_map.pop("Host", None)
    update_value = field_map.pop("Update", "")
    application = field_map.pop("Aplicação", "")
    processes = field_map.pop("Processos alterados", "")
    duration = field_map.pop("Duração", "")
    checks = field_map.pop("Verificações", "") or field_map.pop("Validações", "")
    timings = field_map.pop("Tempos", "")
    files = field_map.pop("Arquivos", "")
    warnings = field_map.pop("Avisos", "")

    meta = []
    if identifier and identifier != "—":
        meta.append(f"`{identifier.strip('`')}`")
    if branch and branch != "—":
        meta.append(f"branch `{branch.strip('`')}`")
    if commit and commit != "—":
        meta.append(f"commit `{commit.strip('`')}`")
    meta.append(footer)

    result_pairs = []
    for name, value in (("Alterações", update_value), ("Aplicação", application), ("Processos", processes), ("Duração", duration)):
        if value and value != "—":
            result_pairs.append((name, value))

    primary = [make_text(f"# {title}"), make_text("-# " + " • ".join(part for part in meta if part))]
    if summary and summary != "—":
        primary.append(make_separator())
        primary.extend(make_text(chunk) for chunk in split_text(summary, 1000))
    if result_pairs:
        primary.append(make_separator())
        primary.extend(render_field_block("Resultado", result_pairs))
    if checks and checks != "—":
        primary.append(make_separator())
        primary.extend(make_text(chunk) for chunk in split_text(f"## Verificações\n{checks}"))

    components = []
    append_container(components, color, primary)

    details = []
    if warnings and warnings != "—" and warnings.casefold() not in {"sem avisos", "nenhum"}:
        details.extend(make_text(chunk) for chunk in split_text(f"## Avisos\n{warnings}"))
    status_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in STATUS_FIELDS]
    if status_pairs:
        if details:
            details.append(make_separator())
        details.extend(render_field_block("Estado final", status_pairs))
    remaining = [(name, value) for name, value in field_map.items() if value and value != "—"]
    if remaining:
        if details:
            details.append(make_separator())
        details.extend(render_field_block("Detalhes", remaining))
    append_container(components, color, details)

    technical = []
    if files and files != "—":
        technical.extend(make_text(chunk) for chunk in split_text(f"## Arquivos alterados\n{files}"))
    if timings and timings != "—":
        if technical:
            technical.append(make_separator())
        technical.extend(make_text(chunk) for chunk in split_text(f"## Tempos\n{timings}"))
    append_container(components, color, technical)

    total_chars = 0
    for container in components:
        for child in container["components"]:
            if child["type"] != TEXT_DISPLAY:
                continue
            remaining_chars = max(400, MAX_TOTAL_TEXT - total_chars)
            child["content"] = trunc(child["content"], remaining_chars)
            total_chars += len(child["content"])
    return components[:4]


def build_containers(title: str, summary: str, fields, footer: str, color: int):
    field_map = OrderedDict((field["name"], field["value"]) for field in fields)

    header_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in HEADER_FIELDS]
    status_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in STATUS_FIELDS]
    detail_pairs = [(name, field_map.pop(name)) for name in list(field_map) if name in DETAIL_FIELDS]
    file_value = field_map.pop("Arquivos", "")
    action_value = field_map.pop("Ação sugerida", "")
    command_value = field_map.pop("Comando", "")
    logs_value = field_map.pop("Últimas linhas", "")
    stderr_value = field_map.pop("Stderr", "")
    local_files_value = field_map.pop("Arquivos locais", "")
    git_status_value = field_map.pop("Status git", "")
    log_value = field_map.pop("Log", "") or field_map.pop("Log completo", "")
    other_pairs = list(field_map.items())

    header_meta = []
    host = next((value for name, value in header_pairs if name == "Host"), None)
    branch = next((value for name, value in header_pairs if name == "Branch"), None)
    commit = next((value for name, value in header_pairs if name == "Commit"), None)
    if host and host != "—":
        header_meta.append(f"host `{host}`")
    if branch and branch != "—":
        header_meta.append(f"branch `{branch}`")
    if commit and commit != "—":
        header_meta.append(f"commit `{commit}`")
    header_meta.append(footer)
    header_pairs = [(name, value) for name, value in header_pairs if name not in {"Host", "Branch", "Commit"}]

    components = []
    primary_children = [make_text(f"# {title}"), make_text("-# " + " • ".join(part for part in header_meta if part))]
    if summary and summary != "—":
        primary_children.append(make_separator())
        primary_children.extend(make_text(chunk) for chunk in split_text(summary, 1000))
    if header_pairs:
        primary_children.append(make_separator())
        primary_children.extend(render_field_block("Resumo rápido", header_pairs))
    if status_pairs:
        primary_children.append(make_separator())
        primary_children.extend(render_field_block("Status dos serviços", status_pairs))
    append_container(components, color, primary_children)

    secondary_children = []
    if detail_pairs:
        secondary_children.extend(render_field_block("Detalhes", detail_pairs))
    if other_pairs:
        if secondary_children:
            secondary_children.append(make_separator())
        secondary_children.extend(render_field_block("Informações extras", other_pairs))
    if file_value and file_value != "—":
        if secondary_children:
            secondary_children.append(make_separator())
        secondary_children.extend(make_text(chunk) for chunk in split_text(f"## Arquivos alterados\n{file_value}"))
    if action_value and action_value != "—":
        if secondary_children:
            secondary_children.append(make_separator())
        secondary_children.extend(make_text(chunk) for chunk in split_text(f"## Ação sugerida\n{action_value}"))
    append_container(components, color, secondary_children)

    log_children = []
    if command_value and command_value != "—":
        log_children.extend(render_code_block("Comando", command_value))
    for name, value in (("Status git", git_status_value), ("Arquivos locais", local_files_value), ("Stderr", stderr_value), ("Últimas linhas", logs_value), ("Log completo", log_value)):
        if value and value != "—":
            if log_children:
                log_children.append(make_separator())
            log_children.extend(render_code_block(name, value))
    append_container(components, color, log_children)

    total_chars = 0
    for container in components:
        for child in container["components"]:
            if child["type"] != TEXT_DISPLAY:
                continue
            remaining = max(400, MAX_TOTAL_TEXT - total_chars)
            child["content"] = trunc(child["content"], remaining)
            total_chars += len(child["content"])

    return components[:4]


description, fields, footer = parse_body(BODY)
emoji = EMOJI_MAP.get(TYPE, "ℹ️")
color = COLOR_MAP.get(TYPE, COLOR_MAP["info"])
full_title = TITLE if TITLE.startswith(("❌", "⚠️", "✅", "🔄", "ℹ️")) else f"{emoji} {TITLE}"

renderer = build_update_containers if is_update_notification(full_title, fields) else build_containers
payload = {
    "allowed_mentions": {"parse": []},
    "flags": COMPONENTS_V2_FLAG,
    "components": renderer(trunc(full_title, 120), description, fields, trunc(footer, 200), color),
}

print(json.dumps(payload, ensure_ascii=False))
PY
})" || exit 1

if [ "${ALERT_DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "$PAYLOAD_JSON"
  exit 0
fi

WEBHOOK_URL="$ALERT_WEBHOOK_URL"
case "$WEBHOOK_URL" in
  *with_components=*) ;;
  *\?*) WEBHOOK_URL="${WEBHOOK_URL}&with_components=true" ;;
  *) WEBHOOK_URL="${WEBHOOK_URL}?with_components=true" ;;
esac
case "$WEBHOOK_URL" in
  *wait=*) ;;
  *\?*) WEBHOOK_URL="${WEBHOOK_URL}&wait=true" ;;
  *) WEBHOOK_URL="${WEBHOOK_URL}?wait=true" ;;
esac

TMP_RESP="$(mktemp)"

SEND_ARGS=(
  -sS
  --connect-timeout 8
  --max-time 35
  -o "$TMP_RESP"
  -w '%{http_code}'
)

if [ -n "$ATTACH_FILE" ] && [ -f "$ATTACH_FILE" ] && [ -s "$ATTACH_FILE" ]; then
  if [ -z "$ATTACH_NAME" ]; then
    ATTACH_NAME="$(basename "$ATTACH_FILE")"
  fi
  # Envio multipart permite anexar o log completo sem estourar o limite visual
  # dos Components V2. O payload continua indo como JSON seguro.
  HTTP_CODE="$(curl "${SEND_ARGS[@]}" \
    --form-string "payload_json=$PAYLOAD_JSON" \
    -F "files[0]=@$ATTACH_FILE;filename=$ATTACH_NAME;type=text/plain" \
    "$WEBHOOK_URL")" || {
    rm -f "$TMP_RESP"
    exit 1
  }
else
  HTTP_CODE="$(curl "${SEND_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD_JSON" \
    "$WEBHOOK_URL")" || {
    rm -f "$TMP_RESP"
    exit 1
  }
fi

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "204" ]; then
  cat "$TMP_RESP" >&2
  rm -f "$TMP_RESP"
  exit 1
fi

rm -f "$TMP_RESP"
exit 0
