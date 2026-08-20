#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
LAVALINK_SERVICE="lavalink"
LOG_TAG="tts-bot-updater"
DIRTY_MARKER_FILE="$REPO_DIR/.fatal-update-dirty"
LOCAL_CHANGES_MARKER_FILE="$REPO_DIR/.fatal-update-local-changes"
CANDIDATE_ROOT="${DISCORD_AUTO_UPDATE_STAGING_DIR:-$(dirname "$REPO_DIR")/bot-update-staging}/candidates"
# Formato antigo: um único pending.json. Mantemos leitura por compatibilidade,
# mas novos ZIPs entram em queue/pending/*.json para não sobrescrever candidatos.
CANDIDATE_PENDING_FILE="$CANDIDATE_ROOT/pending.json"
CANDIDATE_QUEUE_ROOT="${DISCORD_AUTO_UPDATE_QUEUE_DIR:-$CANDIDATE_ROOT/queue}"
CANDIDATE_QUEUE_PENDING_DIR="$CANDIDATE_QUEUE_ROOT/pending"
CANDIDATE_QUEUE_ACTIVE_DIR="$CANDIDATE_QUEUE_ROOT/active"
CANDIDATE_QUEUE_DONE_DIR="$CANDIDATE_QUEUE_ROOT/done"
CANDIDATE_QUEUE_FAILED_DIR="$CANDIDATE_QUEUE_ROOT/failed"
CANDIDATE_QUEUE_CANCELLED_DIR="$CANDIDATE_QUEUE_ROOT/cancelled"
UPDATE_RUNTIME_STATE_FILE="${DISCORD_AUTO_UPDATE_RUNTIME_STATE_FILE:-$CANDIDATE_ROOT/runtime-state.json}"
UPDATER_LOCK_FILE="${DISCORD_AUTO_UPDATE_LOCK_FILE:-/run/lock/tts-bot-updater.lock}"
REMOTE_REJECTED_FILE="${DISCORD_AUTO_UPDATE_REJECTED_REMOTE_FILE:-$REPO_DIR/data/updater/rejected_remote_commits.json}"
ROLLBACK_REQUEST_DEFAULT_ROOT="$CANDIDATE_ROOT/rollback"
ROLLBACK_REQUEST_DATA_ROOT="${DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIR:-$REPO_DIR/data/runtime/update-rollback}"
ROLLBACK_REQUEST_TMP_ROOT="${TMPDIR:-/tmp}/tts-bot-update-rollback"
ROLLBACK_REQUEST_ROOT="$ROLLBACK_REQUEST_DEFAULT_ROOT"
ROLLBACK_REQUEST_PENDING_FILE="$ROLLBACK_REQUEST_ROOT/pending.json"
ROLLBACK_REQUEST_ACTIVE_FILE="$ROLLBACK_REQUEST_ROOT/active.json"

# O updater pode substituir scripts/tts-bot-update.sh enquanto ele mesmo está
# rodando. Bash lê partes do arquivo sob demanda; se o arquivo for alterado no
# meio da execução, o processo pode continuar lendo uma versão diferente e
# quebrar com variáveis antigas/novas fora de sincronia. Por isso o processo
# real sempre roda a partir de uma cópia temporária estável.
if [[ "${TTS_BOT_UPDATER_RUNNING_COPY:-0}" != "1" ]]; then
  UPDATER_RUNTIME_BASE="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
  if [[ "$UPDATER_RUNTIME_BASE" != /* || ! -d "$UPDATER_RUNTIME_BASE" || -L "$UPDATER_RUNTIME_BASE" || ! -w "$UPDATER_RUNTIME_BASE" ]]; then
    UPDATER_RUNTIME_BASE="/tmp"
  fi
  # mktemp evita colisão/symlink previsível em /tmp. No serviço systemd, a
  # cópia fica em RuntimeDirectory e é removida pelo próprio systemd mesmo em
  # SIGKILL, reboot ou queda antes do trap EXIT.
  UPDATER_RUNTIME_COPY="$(mktemp "$UPDATER_RUNTIME_BASE/tts-bot-update.XXXXXX.run")"
  cp -- "$0" "$UPDATER_RUNTIME_COPY"
  chmod 0700 "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  export TTS_BOT_UPDATER_RUNNING_COPY=1
  export TTS_BOT_UPDATER_RUNTIME_COPY
  exec /usr/bin/env bash "$UPDATER_RUNTIME_COPY" "$@"
fi
UPDATER_RUNTIME_COPY="${TTS_BOT_UPDATER_RUNTIME_COPY:-}"

# Mantém o updater em baixa prioridade para não competir com heartbeat/voz do bot
# na VPS pequena. Filhos como git, python de validação e scripts auxiliares herdam
# essa prioridade sem mudar a lógica do update.
UPDATER_NICE_LEVEL="${TTS_BOT_UPDATER_NICE_LEVEL:-10}"
UPDATER_IONICE_CLASS="${TTS_BOT_UPDATER_IONICE_CLASS:-2}"
UPDATER_IONICE_LEVEL="${TTS_BOT_UPDATER_IONICE_LEVEL:-7}"
renice -n "$UPDATER_NICE_LEVEL" -p "$$" >/dev/null 2>&1 || true
if command -v ionice >/dev/null 2>&1; then
  ionice -c "$UPDATER_IONICE_CLASS" -n "$UPDATER_IONICE_LEVEL" -p "$$" >/dev/null 2>&1 || true
fi

mkdir -p "$(dirname "$UPDATER_LOCK_FILE")" 2>/dev/null || true
exec 9>"$UPDATER_LOCK_FILE"
if ! flock -n 9; then
  logger -t "$LOG_TAG" "updater já está em execução; mantendo fila para o próximo ciclo" 2>/dev/null || true
  exit 0
fi
UPDATE_RUNTIME_RUN_ID="$(date +%Y%m%d%H%M%S)-$$-${RANDOM:-0}"

FRONT_DIR="$REPO_DIR/activity/sinuca"
BACK_DIR="$REPO_DIR/activity/sinuca-server"
FRONT_PUBLISH_DIR="/var/www/sinuca"
BACK_PORT="8787"
BACK_SERVICE="${DASHBOARD_SYSTEMD_SERVICE:-sinuca-activity-server.service}"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"
APP_COMMAND_SYNC_STATUS_FILE="$REPO_DIR/data/app_commands_sync_status.json"

STAGE="inicialização"
FAILED_STAGE=""
CURRENT_COMMIT=""
REMOTE_COMMIT=""
PREVIOUS_COMMIT=""
COMMIT_SUBJECT=""
UPDATE_APPLIED=0
# Limite transacional: depois que o deploy foi validado e, quando aplicável,
# publicado no GitHub, falhas de formatação/Discord/log não podem mais acionar
# rollback do código nem reiniciar o bot.
DEPLOYMENT_COMMITTED=0
DELIVERY_PHASE=0
ROLLBACK_DONE=0
ROLLBACK_IN_PROGRESS=0
BOT_RESTARTS_DEPLOY=0
BOT_RESTARTS_ROLLBACK=0
MANUAL_FAILURE_ALERT_SENT=0
LOCAL_CANDIDATE_MODE=0
LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=0
LOCAL_CANDIDATE_ID=""
LOCAL_CANDIDATE_DISPLAY_ID=""
LOCAL_CANDIDATE_DIR=""
LOCAL_CANDIDATE_BASE_COMMIT=""
LOCAL_CANDIDATE_COMMIT_MESSAGE=""
LOCAL_CANDIDATE_ZIP_NAME=""
LOCAL_CANDIDATE_ZIP_SHA256=""
LOCAL_CANDIDATE_SOURCE_AUTHOR_ID=""
LOCAL_CANDIDATE_ATTEMPT=0
LOCAL_CANDIDATE_VERIFY_ERROR=""
LOCAL_CANDIDATE_PENDING_FILE=""
LOCAL_CANDIDATE_FILES_DIR=""
LOCAL_CANDIDATE_PATCH_FILE=""
LOCAL_CANDIDATE_USE_PATCH=0
LOCAL_CANDIDATE_PUBLISHED=0
REMOTE_CANDIDATE_MODE=0
REMOTE_STATUS_CHANNEL_ID=""
REMOTE_STATUS_MESSAGE_ID=""
REMOTE_WORKTREE_DIR=""
REMOTE_REJECT_REASON=""
ROLLBACK_CONTROL_MODE=0
ROLLBACK_REQUEST_ID=""
ROLLBACK_REQUEST_FILE=""
ROLLBACK_REQUEST_ACTION=""
ROLLBACK_REQUEST_BRANCH=""
ROLLBACK_EXPECTED_HEAD=""
ROLLBACK_REVERT_COMMIT=""
ROLLBACK_MESSAGE_CHANNEL_ID=""
ROLLBACK_MESSAGE_ID=""
ROLLBACK_SOURCE_AUTHOR_ID=""
ROLLBACK_REQUESTED_BY=""
ROLLBACK_PREVIOUS_RECORD_JSON="{}"
ROLLBACK_NEW_COMMIT=""
ROLLBACK_UPDATE_FROM=""
ROLLBACK_UPDATE_TO=""
ROLLBACK_ROLLBACK_COMMIT=""
ROLLBACK_REDO_COMMIT=""

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0
REQUIREMENTS_CHANGED=0
AUDIO_SYSTEMD_CHANGED=0
CLEANUP_CHANGED=0
PHONE_LAVALINK_WATCH_CHANGED=0
PHONE_WORKER_WATCH_CHANGED=0
VPS_SYSTEMD_UNITS_CHANGED=0
ALERT_CHANGED=0
PHONE_WORKER_SYNC_REQUIRED=0
CORE_WORKER_APK_CHANGED=0
CORE_WORKER_AUTOMATION_REQUIRED=0

BOT_HEALTHCHECK_STATUS="não verificado"
BOT_HEALTH_JSON=""
BOT_HEALTH_DETAIL_STATUS="não verificado"
BOT_COGS_STATUS="não verificado"
BOT_WARNINGS_STATUS="sem avisos"
PREFLIGHT_PY_STATUS="não verificado"
PREFLIGHT_BASH_STATUS="não verificado"
PREFLIGHT_COG_IMPORT_STATUS="não verificado"
UPDATE_HAS_WARNINGS=0
AUDIO_SERVICES_STATUS="não alterado"
CLEANUP_STATUS="não alterada"
PHONE_LAVALINK_WATCH_STATUS="não alterado"
PHONE_WORKER_WATCH_STATUS="não alterado"
VPS_SYSTEMD_UNITS_STATUS="não alterado"
PHONE_WORKER_SYNC_STATUS="sem mudanças"
CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
CORE_WORKER_NOTIFY_STATUS="sem mudanças"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
ROLLBACK_STATUS="não foi necessário"
# Variáveis opcionais usadas apenas quando certos scripts/instaladores mudam.
# Com `set -u`, elas precisam existir desde o topo para a etapa final de
# webhook/mensagem nunca derrubar o updater após o commit/push já ter passado.
ALERT_UNIT_STATUS="não alterado"
CRONTAB_HEALTH_STATUS="não alterado"
APP_COMMAND_SYNC_SUMMARY="Comandos sem mudanças"
APP_COMMAND_SYNC_WEBHOOK_BLOCK=""
APP_COMMAND_SYNC_ADDED_COUNT=0
APP_COMMAND_SYNC_REMOVED_COUNT=0
APP_COMMAND_SYNC_CHANGED=0
APP_COMMAND_SYNC_PERFORMED=0
APP_COMMANDS_MAY_HAVE_CHANGED=0
CHANGED_FILES_RAW=""
CHANGED_DIFF_NUMSTAT_RAW=""
DIFF_TOTAL_SUMMARY=""
FAST_RELOAD_STATUS="não usado"
FAST_RELOAD_MODULES=""
UPDATER_UNIT="tts-bot-updater.service"
UPDATER_EPHEMERAL_DIR="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
if [[ "$UPDATER_EPHEMERAL_DIR" != /* || ! -d "$UPDATER_EPHEMERAL_DIR" || -L "$UPDATER_EPHEMERAL_DIR" || ! -w "$UPDATER_EPHEMERAL_DIR" ]]; then
  UPDATER_EPHEMERAL_DIR="/tmp"
fi
RUN_LOG_FILE="$(mktemp "$UPDATER_EPHEMERAL_DIR/tts-bot-updater.XXXXXX.log")"
ZIP_STATUS_CONTROL_JSON=""
UPDATE_TITLE_EMOJI="<a:areia:1496606578395189473>"
UPDATE_STAGE_EMOJI="<a:loading:1510065277868445796>"
ZIP_PROGRESS_HISTORY=""
ZIP_PROGRESS_COMPLETED_COUNT=0
ZIP_PROGRESS_HIDDEN_COUNT=0
ZIP_PROGRESS_MAX_VISIBLE_STEPS=10
ZIP_PROGRESS_STAGE_LABEL=""
ZIP_PROGRESS_STAGE_STARTED_MS=0
ZIP_PROGRESS_STARTED_MS=0
ZIP_PROGRESS_DONE_LABELS=""
ZIP_PROGRESS_HANDOFF_LOADED=0
UPDATER_STEP_LAST=0
UPDATER_TIMINGS=""
UPDATE_STATUS_OUTBOX_DIR="$REPO_DIR/data/runtime/update-status-outbox"
UPDATE_ALERT_OUTBOX_DIR="$REPO_DIR/data/runtime/update-alert-outbox"
UPDATE_DELIVERY_RECEIPTS_DIR="$REPO_DIR/data/runtime/update-delivery-receipts"
chmod 0644 "$RUN_LOG_FILE" 2>/dev/null || true

# Log persistente do updater para diagnóstico pelo /vps.
# O systemd guarda a saída no journalctl, mas este arquivo facilita anexar
# falhas recentes sem depender só do journal.
PERSISTENT_LOG_DIR="$REPO_DIR/logs"
PERSISTENT_LOG_FILE="$PERSISTENT_LOG_DIR/updater.log"
mkdir -p "$PERSISTENT_LOG_DIR" 2>/dev/null || true
# Rotação simples: se passar de 2MB, renomeia .log -> .log.1 e começa do zero.
if [[ -f "$PERSISTENT_LOG_FILE" ]]; then
  log_size=$(stat -c '%s' "$PERSISTENT_LOG_FILE" 2>/dev/null || echo 0)
  if [[ "$log_size" -gt 2097152 ]]; then
    mv -f "$PERSISTENT_LOG_FILE" "$PERSISTENT_LOG_FILE.1" 2>/dev/null || true
  fi
fi
{
  echo ""
  echo "===== $(date -Iseconds) updater started (pid=$$) ====="
} >> "$PERSISTENT_LOG_FILE" 2>/dev/null || true

# Tee pra ambos os arquivos. O persistente vira logs/updater.log;
# o tmpfile continua existindo pra `collect_run_log_excerpt`.
exec > >(tee -a "$RUN_LOG_FILE" "$PERSISTENT_LOG_FILE") 2>&1

write_update_runtime_state() {
  local phase="${1:-Atualizando}"
  if (( LOCAL_CANDIDATE_MODE == 0 && ROLLBACK_CONTROL_MODE == 0 && REMOTE_CANDIDATE_MODE == 0 )); then
    return 0
  fi
  mkdir -p "$(dirname "$UPDATE_RUNTIME_STATE_FILE")" 2>/dev/null || return 0
  UPDATE_PHASE="$phase" \
  UPDATE_RUNTIME_RUN_ID_VALUE="$UPDATE_RUNTIME_RUN_ID" \
  UPDATE_RUNTIME_STATE_FILE_VALUE="$UPDATE_RUNTIME_STATE_FILE" \
  UPDATE_ID_VALUE="${LOCAL_CANDIDATE_DISPLAY_ID:-${ROLLBACK_REQUEST_ID:-${SHORT_TO:-atualização}}}" \
  UPDATE_RESTART_EXPECTED="$BOT_CHANGED" \
  python3 - <<'PYUPDATESTATE' 2>/dev/null || true
import datetime, json, os, pathlib, time
path = pathlib.Path(os.environ['UPDATE_RUNTIME_STATE_FILE_VALUE'])
payload = {
    'active': True,
    'run_id': os.environ.get('UPDATE_RUNTIME_RUN_ID_VALUE') or '',
    'update_id': os.environ.get('UPDATE_ID_VALUE') or 'atualização',
    'phase': (os.environ.get('UPDATE_PHASE') or 'Atualizando')[:200],
    'restart_expected': os.environ.get('UPDATE_RESTART_EXPECTED') == '1',
    'heartbeat_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'heartbeat_epoch': time.time(),
    'pid': os.getppid(),
}
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYUPDATESTATE
  chown ubuntu:ubuntu "$UPDATE_RUNTIME_STATE_FILE" 2>/dev/null || true
  chmod 0644 "$UPDATE_RUNTIME_STATE_FILE" 2>/dev/null || true
}

clear_update_runtime_state() {
  [[ -f "$UPDATE_RUNTIME_STATE_FILE" ]] || return 0
  UPDATE_RUNTIME_RUN_ID_VALUE="$UPDATE_RUNTIME_RUN_ID" \
  UPDATE_RUNTIME_STATE_FILE_VALUE="$UPDATE_RUNTIME_STATE_FILE" \
  python3 - <<'PYCLEARUPDATESTATE' 2>/dev/null || true
import json, os, pathlib
path = pathlib.Path(os.environ['UPDATE_RUNTIME_STATE_FILE_VALUE'])
try:
    payload = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    payload = {}
if not isinstance(payload, dict) or payload.get('run_id') == os.environ.get('UPDATE_RUNTIME_RUN_ID_VALUE'):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
PYCLEARUPDATESTATE
}

cleanup_runtime_artifacts() {
  clear_update_runtime_state || true
  rm -f "$RUN_LOG_FILE"
  if [[ -n "${REMOTE_WORKTREE_DIR:-}" && -d "$REMOTE_WORKTREE_DIR" ]]; then
    sudo -u ubuntu -H git -C "$REPO_DIR" worktree remove --force "$REMOTE_WORKTREE_DIR" >/dev/null 2>&1 || rm -rf "$REMOTE_WORKTREE_DIR" 2>/dev/null || true
  fi
  if [[ -n "${UPDATER_RUNTIME_COPY:-}" && -f "$UPDATER_RUNTIME_COPY" ]]; then
    rm -f "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  fi
}

trim_alert_text() {
  local limit="${1:-4000}"
  # Importante: a versão anterior passava o script Python por heredoc em
  # `python3 -`, então o Python consumia o heredoc como stdin e NÃO lia o pipe.
  # Com `set -o pipefail`, o produtor do pipe recebia SIGPIPE e o updater caía
  # com código 141 durante `git status | trim_alert_text`. Usar `-c` preserva
  # stdin para o texto real e torna o helper seguro para pipelines.
  python3 -c 'import sys
limit = int(sys.argv[1])
text = sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n").strip()
if text and len(text) > limit:
    text = text[: max(0, limit - 1)].rstrip() + "…"
if text:
    sys.stdout.write(text + "\n")
' "$limit" || true
}

collect_run_log_excerpt() {
  if [[ -f "$RUN_LOG_FILE" ]]; then
    tail -n 40 "$RUN_LOG_FILE" 2>/dev/null | sed 's/\[[0-9;]*[A-Za-z]//g' | trim_alert_text 1500
  fi
}

service_unit_for_stage() {
  local stage_lc="${1,,}"
  if [[ "$stage_lc" == *"bot"* ]]; then
    printf '%s.service' "$SERVICE"
  else
    printf '%s' "$UPDATER_UNIT"
  fi
}

collect_journal_excerpt() {
  local unit="${1:-$UPDATER_UNIT}"
  local logs
  logs="$(journalctl -u "$unit" -n 40 --no-pager 2>/dev/null | tail -n 25 || true)"
  if [[ -z "${logs//[[:space:]]/}" ]]; then
    logs="nenhum log adicional encontrado"
  fi
  printf '%s' "$logs" | trim_alert_text 1500
}

phone_worker_log_summary_text() {
  if [[ ! -x "$REPO_DIR/scripts/phone-worker-client.py" && ! -f "$REPO_DIR/scripts/phone-worker-client.py" ]]; then
    printf 'indisponível: cliente ausente'
    return 0
  fi
  if ! env_truthy PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED && ! env_truthy PHONE_WORKER_ENABLED; then
    printf 'desativado'
    return 0
  fi
  local source_file="${1:-$RUN_LOG_FILE}"
  if [[ ! -s "$source_file" ]]; then
    printf 'sem logs para analisar'
    return 0
  fi
  local timeout_value="4"
  if [[ -f "$REPO_DIR/.env" ]]; then
    timeout_value="$(grep -E '^PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    [[ -n "$timeout_value" ]] || timeout_value="4"
  fi
  local raw summary
  raw="$(sudo -u ubuntu -H python3 "$REPO_DIR/scripts/phone-worker-client.py" log-summary "$source_file" --timeout "$timeout_value" 2>/dev/null || true)"
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    printf 'indisponível: sem resposta'
    return 0
  fi
  summary="$(PHONE_WORKER_RAW="$raw" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
try:
    data = json.loads(os.environ.get('PHONE_WORKER_RAW') or '{}')
except Exception:
    raise SystemExit
counts = data.get('counts') or {}
top = data.get('top_messages') or []
parts = []
for key in ('critical','error','warning','timeout','traceback','exception','failed','lavalink','yt_dlp','rate_limit'):
    value = int(counts.get(key) or 0)
    if value:
        parts.append(f'{key}={value}')
if not parts:
    parts.append('sem padrões críticos')
if top:
    msg = str((top[0] or {}).get('message') or '')[:140]
    cnt = (top[0] or {}).get('count') or 1
    if msg:
        parts.append(f'top({cnt}x): {msg}')
print(' | '.join(parts)[:900])
PYJSON
)"
  if [[ -n "${summary//[[:space:]]/}" ]]; then
    printf '%s' "$summary"
  else
    printf 'indisponível: resposta inválida'
  fi
}

register_error_context() {
  LAST_ERROR_EXIT_CODE="${1:-1}"
  LAST_ERROR_COMMAND="${2:-desconhecido}"
  LAST_ERROR_SERVICE_UNIT="$(service_unit_for_stage "$STAGE")"
  LAST_ERROR_STDERR="$(collect_run_log_excerpt)"
  LAST_ERROR_LOGS="$(collect_journal_excerpt "$LAST_ERROR_SERVICE_UNIT")"
  if [[ -z "${LAST_ERROR_STDERR//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="nenhuma saída adicional capturada"
  fi
}

LAST_ERROR_EXIT_CODE=""
LAST_ERROR_COMMAND=""
LAST_ERROR_SERVICE_UNIT=""
LAST_ERROR_STDERR=""
LAST_ERROR_LOGS=""
LAST_ERROR_LINE=""
LAST_ERROR_FUNCTION=""

prepare_update_delivery_dirs() {
  mkdir -p "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || return 1
  chown ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || true
  chmod 0775 "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || true
}

send_alert_reliably() {
  local alert_type="${1:-info}"
  local title="${2:-Auto update}"
  local body="${3:-}"
  local attach="${4:-}"
  local attach_name="${5:-}"
  local event_id="${6:-}"
  local receipt="" receipt_safe=""

  prepare_update_delivery_dirs || true
  if [[ -n "${event_id//[[:space:]]/}" ]]; then
    receipt_safe="$(printf '%s' "$event_id" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-120)"
    receipt="$UPDATE_DELIVERY_RECEIPTS_DIR/${receipt_safe}.alert.done"
    [[ -f "$receipt" ]] && return 0
  fi

  if sudo -u ubuntu /usr/bin/env bash "$REPO_DIR/alert.sh" "$alert_type" "$title" "$body" "$attach" "$attach_name"; then
    if [[ -n "$receipt" ]]; then
      local receipt_tmp="$receipt.tmp.$$"
      if printf '%s\n' "$(date -Iseconds)" > "$receipt_tmp"; then
        chown ubuntu:ubuntu "$receipt_tmp" 2>/dev/null || true
        chmod 0664 "$receipt_tmp" 2>/dev/null || true
        if ! mv -f "$receipt_tmp" "$receipt"; then
          rm -f "$receipt_tmp" 2>/dev/null || true
          logger -t "$LOG_TAG" "alerta entregue, mas recibo não pôde ser persistido: ${event_id:-$title}" 2>/dev/null || true
        fi
      else
        rm -f "$receipt_tmp" 2>/dev/null || true
        logger -t "$LOG_TAG" "alerta entregue, mas recibo não pôde ser criado: ${event_id:-$title}" 2>/dev/null || true
      fi
    fi
    return 0
  fi

  local queued_rc=0
  if ALERT_TYPE_VALUE="$alert_type" ALERT_TITLE_VALUE="$title" ALERT_BODY_VALUE="$body" \
  ALERT_ATTACH_VALUE="$attach" ALERT_ATTACH_NAME_VALUE="$attach_name" ALERT_EVENT_ID_VALUE="$event_id" \
  UPDATE_ALERT_OUTBOX_DIR="$UPDATE_ALERT_OUTBOX_DIR" python3 - <<'PYQUEUEALERT'
import datetime, hashlib, json, os, pathlib, shutil, time, uuid

root = pathlib.Path(os.environ['UPDATE_ALERT_OUTBOX_DIR'])
root.mkdir(parents=True, exist_ok=True)
event_id = (os.environ.get('ALERT_EVENT_ID_VALUE') or '').strip()
if event_id:
    safe = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in event_id)[:120]
else:
    digest = hashlib.sha256(
        ((os.environ.get('ALERT_TITLE_VALUE') or '') + '\0' + (os.environ.get('ALERT_BODY_VALUE') or '')).encode('utf-8', errors='ignore')
    ).hexdigest()[:16]
    safe = f"{int(time.time() * 1000)}-{digest}-{uuid.uuid4().hex[:8]}"
job_path = root / f"{safe}.json"
attachment = ''
source = pathlib.Path(os.environ.get('ALERT_ATTACH_VALUE') or '')
if source.is_file() and source.stat().st_size > 0:
    attachment = str(root / f"{safe}.attachment")
    shutil.copy2(source, attachment)
payload = {
    'schema_version': 1,
    'event_id': event_id or safe,
    'type': os.environ.get('ALERT_TYPE_VALUE') or 'info',
    'title': os.environ.get('ALERT_TITLE_VALUE') or 'Auto update',
    'body': os.environ.get('ALERT_BODY_VALUE') or '',
    'attachment': attachment,
    'attachment_name': os.environ.get('ALERT_ATTACH_NAME_VALUE') or '',
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'attempts': 0,
    'last_error': 'falha no envio direto',
}
tmp = job_path.with_name('.' + job_path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, job_path)
for path in (job_path, pathlib.Path(attachment) if attachment else None):
    if path:
        try:
            os.chmod(path, 0o664)
        except OSError:
            pass
PYQUEUEALERT
  then
    queued_rc=0
  else
    queued_rc=$?
  fi
  chown ubuntu:ubuntu "$UPDATE_ALERT_OUTBOX_DIR" 2>/dev/null || true
  if (( queued_rc == 0 )); then
    logger -t "$LOG_TAG" "alerta enfileirado para reenvio: ${event_id:-$title}" 2>/dev/null || true
    return 0
  fi
  logger -t "$LOG_TAG" "falha ao enviar e ao persistir alerta: ${event_id:-$title}" 2>/dev/null || true
  return 1
}

send_info() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  send_alert_reliably info "$title" "$body" "" "" "${3:-}" || true
}

send_warn() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  send_alert_reliably warn "$title" "$body" "" "" "${3:-}" || true
}

send_error() {
  local title="${1:-Falha no auto update}"
  local body="${2:-}"
  local attach=""
  if [[ -f "$RUN_LOG_FILE" && -s "$RUN_LOG_FILE" ]]; then
    attach="$RUN_LOG_FILE"
  fi
  local incident_id="${LOCAL_CANDIDATE_ID:-${ROLLBACK_REQUEST_ID:-${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}}}"
  [[ -n "${incident_id//[[:space:]]/}" ]] || incident_id="$(date +%Y%m%d%H%M%S)-$$"
  local event_id="${3:-error-${incident_id}-${FAILED_STAGE:-$STAGE}}"
  send_alert_reliably error "$title" "$body" "$attach" "tts-bot-updater.log" "$event_id" || true
}

human_duration() {
  local total="${1:-0}"
  local m=$((total / 60))
  local s=$((total % 60))
  if (( m > 0 )); then
    printf "%dmin %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
  fi
}

update_now_ms() {
  local value
  value="$(date +%s%3N 2>/dev/null || true)"
  if [[ "$value" =~ ^[0-9]{13}$ ]]; then
    printf '%s' "$value"
    return 0
  fi
  python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
}

format_update_duration_ms() {
  local total_ms="${1:-0}"
  [[ "$total_ms" =~ ^[0-9]+$ ]] || total_ms=0
  if (( total_ms <= 0 )); then
    printf '<1ms'
    return 0
  fi
  if (( total_ms < 1000 )); then
    printf '%dms' "$total_ms"
    return 0
  fi
  if (( total_ms < 60000 )); then
    local tenths=$(((total_ms + 50) / 100))
    local seconds=$((tenths / 10))
    local decimal=$((tenths % 10))
    if (( decimal == 0 )); then
      printf '%ds' "$seconds"
    else
      printf '%d,%ds' "$seconds" "$decimal"
    fi
    return 0
  fi
  local rounded_seconds=$(((total_ms + 500) / 1000))
  local minutes=$((rounded_seconds / 60))
  local seconds=$((rounded_seconds % 60))
  printf '%dmin %02ds' "$minutes" "$seconds"
}

format_update_file_count() {
  local count="${1:-0}"
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  if (( count == 1 )); then
    printf '1 arquivo'
  else
    printf '%d arquivos' "$count"
  fi
}

mark_update_timing() {
  local label="${1:-etapa}"
  local now="$SECONDS"
  local delta=$((now - UPDATER_STEP_LAST))
  UPDATER_STEP_LAST="$now"
  if [[ -n "$UPDATER_TIMINGS" ]]; then
    UPDATER_TIMINGS+=", "
  fi
  UPDATER_TIMINGS+="${label}=${delta}s"
  logger -t "$LOG_TAG" "timing ${label}=${delta}s total=${now}s"
}

format_changed_processes() {
  local items=()
  # Quando o patch é somente de cog recarregável, não trate como reinício de bot.
  # O estágio visual mostra "Recarregando cog/cogs" em vez de "Reiniciando processo".
  local fast_modules_for_process=""
  fast_modules_for_process="$(fast_reload_modules_for_changed_files 2>/dev/null || true)"
  if (( BOT_CHANGED == 1 || REQUIREMENTS_CHANGED == 1 )); then
    if [[ -n "${fast_modules_for_process//[[:space:]]/}" && "${FAST_RELOAD_STATUS:-não usado}" != *"fallback"* ]]; then
      :
    else
      items+=("bot")
    fi
  fi
  if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
    items+=("site")
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 || PHONE_WORKER_WATCH_CHANGED == 1 || PHONE_LAVALINK_WATCH_CHANGED == 1 || CORE_WORKER_APK_CHANGED == 1 || CORE_WORKER_AUTOMATION_REQUIRED == 1 )); then
    items+=("worker")
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 || CLEANUP_CHANGED == 1 )); then
    items+=("áudio")
  fi
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    items+=("sistema VPS")
  fi
  if ((${#items[@]} == 0)); then
    printf 'nenhum processo alterado'
    return 0
  fi
  local joined="" item
  for item in "${items[@]}"; do
    [[ -z "$joined" ]] || joined+=", "
    joined+="$item"
  done
  printf '%s' "$joined"
}

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

wait_for_health() {
  local url="${1:?}"
  local attempts="${2:-12}"
  local delay="${3:-5}"
  local i

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done

  return 1
}

fetch_bot_health_json() {
  curl -fsS --max-time 2 "$BOT_HEALTH_URL" 2>/dev/null || true
}

bot_health_python() {
  local mode="${1:?}"
  BOT_HEALTH_JSON_INPUT="${BOT_HEALTH_JSON:-}" python3 - "$mode" <<'PYHEALTH' 2>/dev/null
import json
import os
import sys

mode = sys.argv[1]
raw = os.environ.get("BOT_HEALTH_JSON_INPUT") or ""
try:
    data = json.loads(raw) if raw.strip() else {}
except Exception as exc:
    if mode == "is_healthy":
        raise SystemExit(1)
    print(f"health inválido: {type(exc).__name__}: {exc}")
    raise SystemExit(0)

if mode == "is_ready_healthy":
    critical = data.get("critical_failed_cogs") or []
    if (
        data.get("healthy") is True
        and data.get("status") == "ok"
        and data.get("discord_ready") is True
        and data.get("discord_closed") is not True
        and data.get("mongo_ok") is True
        and data.get("cog_loading_finished") is True
        and not critical
    ):
        raise SystemExit(0)
    raise SystemExit(1)

if mode == "is_healthy":
    if data.get("healthy") is True:
        raise SystemExit(0)
    raise SystemExit(1)

if mode == "status":
    status = data.get("status") or ("ok" if data.get("healthy") is True else "erro")
    ready = data.get("discord_ready")
    mongo = data.get("mongo_ok")
    latency = data.get("latency_ms")
    parts = [str(status)]
    if ready is not None:
        parts.append(f"discord={'online' if ready else 'não pronto'}")
    if mongo is not None:
        parts.append(f"mongo={'OK' if mongo else 'falhou'}")
    if latency is not None:
        parts.append(f"latência={latency}ms")
    print("; ".join(parts))
    raise SystemExit(0)

if mode == "warnings":
    warnings = data.get("warnings") or []
    warnings = [str(item).strip() for item in warnings if str(item).strip()]
    if warnings:
        print("; ".join(warnings)[:900])
    else:
        print("sem avisos")
    raise SystemExit(0)

if mode == "cogs":
    loaded = data.get("loaded_cogs_count")
    failed = data.get("failed_cogs_count")
    failed_cogs = data.get("failed_cogs") or {}
    critical = data.get("critical_failed_cogs") or []
    if loaded is None:
        loaded = len(data.get("loaded_extensions") or [])
    if failed is None:
        failed = len(failed_cogs)
    parts = [f"{loaded or 0} carregada(s)"]
    if failed:
        kind = "crítica(s)" if critical else "opcional(is)"
        names = []
        for name, details in list(failed_cogs.items())[:5]:
            summary = ""
            if isinstance(details, dict):
                summary = str(details.get("summary") or "").strip()
            names.append(f"{name}" + (f" — {summary}" if summary else ""))
        details = "; ".join(names)
        parts.append(f"{failed} com falha {kind}" + (f": {details}" if details else ""))
    else:
        parts.append("0 com falha")
    print("; ".join(parts)[:1200])
    raise SystemExit(0)

print("—")
PYHEALTH
}

refresh_bot_health_status() {
  BOT_HEALTH_JSON="$(fetch_bot_health_json)"
  if [[ -z "${BOT_HEALTH_JSON//[[:space:]]/}" ]]; then
    BOT_HEALTH_DETAIL_STATUS="HTTP sem resposta"
    BOT_COGS_STATUS="indisponível"
    BOT_WARNINGS_STATUS="health indisponível"
    return 1
  fi

  BOT_HEALTH_DETAIL_STATUS="$(bot_health_python status)"
  BOT_COGS_STATUS="$(bot_health_python cogs)"
  BOT_WARNINGS_STATUS="$(bot_health_python warnings)"

  if bot_health_python is_healthy >/dev/null; then
    return 0
  fi
  return 1
}


service_restart_count() {
  local unit="${1:?}"
  local value
  value="$(systemctl show "$unit" -p NRestarts --value 2>/dev/null || echo 0)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

journal_since_epoch() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  journalctl -u "$unit" --since "@${since_epoch}" --no-pager 2>/dev/null || true
}

fatal_boot_log_patterns() {
  cat <<'EOF'
SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|No module named|cannot import name|ExtensionFailed|ExtensionNotFound|Failed to load extension|discord\.ext\.commands\.errors\.ExtensionFailed|discord\.ext\.commands\.errors\.ExtensionNotFound|RuntimeError:.*(boot|startup|setup|load|cog)|AttributeError:.*(setup|load_extension|cog)|Start request repeated too quickly|Failed with result|Main process exited.*status=1
EOF
}

has_fatal_boot_logs() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  local logs patterns
  logs="$(journal_since_epoch "$unit" "$since_epoch")"
  [[ -n "${logs//[[:space:]]/}" ]] || return 1
  # O bot.py agora permite que cogs opcionais falhem sem derrubar o processo.
  # Essas linhas podem mencionar AttributeError/ImportError/etc. de forma
  # informativa; não devem acionar rollback se o próprio log diz que o bot
  # continuou online. Erros críticos continuam passando pelo filtro.
  logs="$(printf '%s\n' "$logs" | grep -Ev '\[cogs\].*(continuará online|boot continuou com aviso)' || true)"
  [[ -n "${logs//[[:space:]]/}" ]] || return 1
  patterns="$(fatal_boot_log_patterns)"
  printf '%s\n' "$logs" | grep -Eiq "$patterns"
}

run_preflight_checks() {
  local py="$REPO_DIR/.venv/bin/python"
  local file checked_py=0 checked_sh=0 import_checked=0 import_failed=0 import_output=""
  [[ -x "$py" ]] || py="$(command -v python3 || true)"

  if [[ -n "$py" ]]; then
    STAGE="preflight Python"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ -f "$REPO_DIR/$file" ]] || continue
      checked_py=1
      sudo -u ubuntu -H "$py" -m py_compile "$REPO_DIR/$file"
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity/' || true)

    if (( checked_py == 1 )); then
      PREFLIGHT_PY_STATUS="OK"
    else
      PREFLIGHT_PY_STATUS="sem arquivos Python alterados"
    fi

    # `py_compile` não pega erro executado no import, como discord.ui.StringSelect.
    # Para cogs alteradas, tentamos importar o módulo sem conectar ao Discord.
    # Falha aqui vira aviso, não rollback automático: o bot.py decide no boot se
    # a cog é opcional ou crítica.
    STAGE="preflight import de cogs"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ -f "$REPO_DIR/$file" ]] || continue
      [[ "$file" == cogs/*.py ]] || continue
      [[ "$(basename "$file")" == "__init__.py" ]] && continue
      import_checked=1
      module="${file%.py}"
      module="${module//\//.}"
      if ! line="$(cd "$REPO_DIR" && sudo -u ubuntu -H "$py" - <<PYIMPORT 2>&1
import importlib
module = ${module@Q}
importlib.import_module(module)
print(f"OK {module}")
PYIMPORT
)"; then
        import_failed=1
        import_output+="FAIL $module: $(printf '%s' "$line" | tail -n 1)"$'\n'
      fi
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '^cogs/.*\.py$' || true)

    if (( import_checked == 0 )); then
      PREFLIGHT_COG_IMPORT_STATUS="sem cogs Python alteradas"
    elif (( import_failed == 0 )); then
      PREFLIGHT_COG_IMPORT_STATUS="OK"
    else
      UPDATE_HAS_WARNINGS=1
      PREFLIGHT_COG_IMPORT_STATUS="aviso: ${import_failed} import(s) de cog falharam"
      logger -t "$LOG_TAG" "Preflight import de cogs com aviso: ${import_output//$'\n'/ | }"
    fi
  else
    PREFLIGHT_PY_STATUS="python indisponível"
    PREFLIGHT_COG_IMPORT_STATUS="não executado; python indisponível"
  fi

  STAGE="preflight Bash"
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$REPO_DIR/$file" ]] || continue
    checked_sh=1
    bash -n "$REPO_DIR/$file"
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.sh$' || true)

  if (( checked_sh == 1 )); then
    PREFLIGHT_BASH_STATUS="OK"
  else
    PREFLIGHT_BASH_STATUS="sem scripts Bash alterados"
  fi

  logger -t "$LOG_TAG" "Preflight: Python=$PREFLIGHT_PY_STATUS Bash=$PREFLIGHT_BASH_STATUS Cogs=$PREFLIGHT_COG_IMPORT_STATUS"
}

verify_bot_after_restart() {
  local restart_epoch="${1:?}"
  local restarts_before="${2:-0}"
  local allowed_restart_delta="${3:-1}"
  local timeout="${UPDATE_BOT_RESTART_TIMEOUT_SECONDS:-45}"
  local interval="${UPDATE_BOT_RESTART_POLL_SECONDS:-1}"
  local required_successes="${UPDATE_BOT_HEALTH_CONSECUTIVE_SUCCESSES:-3}"
  local stability_seconds="${UPDATE_BOT_HEALTH_STABILITY_SECONDS:-10}"
  local waited=0 restarts_after health_ok=0 last_log_check=0
  local consecutive=0 healthy_since=0 now_epoch stable_for=0

  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=45
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=1
  [[ "$required_successes" =~ ^[0-9]+$ ]] || required_successes=3
  [[ "$stability_seconds" =~ ^[0-9]+$ ]] || stability_seconds=10
  [[ "$allowed_restart_delta" =~ ^[0-9]+$ ]] || allowed_restart_delta=1
  (( timeout < stability_seconds + 8 )) && timeout=$((stability_seconds + 8))
  (( interval < 1 )) && interval=1
  (( required_successes < 2 )) && required_successes=2

  while (( waited <= timeout )); do
    if systemctl is-failed --quiet "$SERVICE"; then
      BOT_HEALTHCHECK_STATUS="falhou: serviço em failed"
      return 1
    fi

    if systemctl is-active --quiet "$SERVICE"; then
      restarts_after="$(service_restart_count "$SERVICE")"
      if [[ "$restarts_after" =~ ^[0-9]+$ && "$restarts_before" =~ ^[0-9]+$ ]]; then
        if (( restarts_after > restarts_before + allowed_restart_delta )); then
          BOT_HEALTHCHECK_STATUS="falhou: restart inesperado detectado (${restarts_before}→${restarts_after})"
          return 1
        fi
      fi

      if (( waited == 0 || waited - last_log_check >= 4 )); then
        last_log_check="$waited"
        if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
          BOT_HEALTHCHECK_STATUS="falhou: erro fatal de inicialização nos logs"
          return 1
        fi
      fi

      if refresh_bot_health_status && bot_health_python is_ready_healthy >/dev/null; then
        now_epoch="$(date +%s)"
        if (( consecutive == 0 )); then
          healthy_since="$now_epoch"
        fi
        consecutive=$((consecutive + 1))
        stable_for=$((now_epoch - healthy_since))
        BOT_HEALTHCHECK_STATUS="confirmando estabilidade (${stable_for}s/${stability_seconds}s; ${consecutive}/${required_successes})"
        if (( consecutive >= required_successes && stable_for >= stability_seconds )); then
          health_ok=1
          break
        fi
      else
        consecutive=0
        healthy_since=0
        stable_for=0
        if [[ -n "${BOT_HEALTH_DETAIL_STATUS//[[:space:]]/}" ]]; then
          logger -t "$LOG_TAG" "Health ainda não estável (${waited}s): $BOT_HEALTH_DETAIL_STATUS"
        fi
      fi
    elif (( waited >= 5 )); then
      BOT_HEALTHCHECK_STATUS="falhou: serviço não ficou active"
      return 1
    fi

    sleep "$interval"
    waited=$((waited + interval))
  done

  if (( health_ok == 1 )); then
    if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
      BOT_HEALTHCHECK_STATUS="falhou: erro fatal durante a janela de estabilidade"
      return 1
    fi
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="estável com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="estável (${stability_seconds}s)"
    fi
    return 0
  fi

  if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
    BOT_HEALTHCHECK_STATUS="falhou: erro fatal de inicialização nos logs"
    return 1
  fi
  if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
    BOT_HEALTHCHECK_STATUS="falhou: health HTTP sem resposta após ${timeout}s"
  else
    BOT_HEALTHCHECK_STATUS="falhou: não permaneceu saudável por ${stability_seconds}s ($BOT_HEALTH_DETAIL_STATUS)"
  fi
  return 1
}

is_placeholder_status_text() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  [[ -z "$lower" || "$lower" == "—" || "$lower" == "-" || "$lower" == "sem avisos" || "$lower" == "sem mudanças" || "$lower" == "não alterado" || "$lower" == "nao alterado" || "$lower" == "não verificado" || "$lower" == "nao verificado" ]]
}

cogs_have_failures() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  if [[ -z "$lower" || "$lower" == "—" || "$lower" == "-" || "$lower" == "não verificado" || "$lower" == "nao verificado" ]]; then
    return 1
  fi
  if [[ "$lower" =~ (^|[^0-9])0[[:space:]]+com[[:space:]]+falha ]]; then
    return 1
  fi
  if [[ "$lower" =~ ([1-9][0-9]*)[[:space:]]+com[[:space:]]+falha ]]; then
    return 0
  fi
  if [[ "$lower" == *"cog"* && ( "$lower" == *"falhou"* || "$lower" == *"erro"* || "$lower" == *"failed"* ) ]]; then
    return 0
  fi
  return 1
}

has_real_warning_text() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  if is_placeholder_status_text "$text"; then
    return 1
  fi
  case "$lower" in
    ok|success|sucesso|ativo|online|sincronizados|limpo|"unit instalada"|"timer ativo"|"sem cogs python alteradas"|"sem arquivos python alterados"|"sem scripts bash alterados")
      return 1
      ;;
  esac
  # Informativo: o sync do phone-worker pode ficar agendado após restart sem ser aviso.
  if [[ "$lower" == *"agendado para automação por jobs após restart"* ]]; then
    return 1
  fi
  if [[ "$lower" == aviso:* || "$lower" == *" com falha"* || "$lower" == falha* || "$lower" == failed* || "$lower" == *"sem resposta"* || "$lower" == *"degraded"* || "$lower" == *"restart loop"* ]]; then
    return 0
  fi
  return 1
}

normalize_final_health_warning_state() {
  # O status "OK com avisos" só pode permanecer se houver aviso real e exibível.
  # Caso contrário, a mensagem final vira contraditória: título amarelo com "Avisos: sem avisos".
  if [[ "$BOT_HEALTHCHECK_STATUS" == "OK com avisos" ]]; then
    if ! has_real_warning_text "$BOT_WARNINGS_STATUS" && ! cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  fi
}

recompute_update_warning_flag() {
  UPDATE_HAS_WARNINGS=0
  normalize_final_health_warning_state
  if has_real_warning_text "$PREFLIGHT_COG_IMPORT_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if has_real_warning_text "$BOT_WARNINGS_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if cogs_have_failures "$BOT_COGS_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == *"sem resposta"* ]]; then
    UPDATE_HAS_WARNINGS=1
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == "OK com avisos" ]]; then
    UPDATE_HAS_WARNINGS=1
  fi
  if has_real_warning_text "${VPS_SYSTEMD_UNITS_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${AUDIO_SERVICES_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${ALERT_UNIT_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CRONTAB_HEALTH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CLEANUP_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_LAVALINK_WATCH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_WORKER_WATCH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_WORKER_SYNC_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_AGENT_UPDATE_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_APK_BUILD_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_NOTIFY_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${FRONT_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${BACK_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${ACTIVITY_HEALTHCHECK_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
}


env_truthy() {
  local key="${1:?}"
  local value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    value="$(grep -E "^${key}=" "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  value="${value,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

wait_for_lavalink_ready() {
  # Arquitetura atual: Lavalink da VPS não é usado. Quando música usa phone worker,
  # esperar lavalink.service local só cria travamento/restart-loop.
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    return 0
  fi
  if [[ -x "$REPO_DIR/scripts/wait-audio-node-ready.py" ]]; then
    sudo -u ubuntu -H "$REPO_DIR/scripts/wait-audio-node-ready.py" --timeout "${AUDIO_NODE_STARTUP_WAIT_SECONDS:-20}"
    return $?
  fi
  return 0
}

short_commit() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    printf 'desconhecido'
  else
    printf '%s' "${value:0:7}"
  fi
}

marker_value() {
  local key="${1:?}"
  if [[ ! -f "$DIRTY_MARKER_FILE" ]]; then
    return 0
  fi
  awk -F= -v wanted="$key" '$1 == wanted { sub($1 "=", ""); print; exit }' "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

write_dirty_marker() {
  local failed_commit="${1:-}"
  local rollback_commit="${2:-}"
  local failed_stage="${3:-desconhecido}"
  local failed_command="${4:-desconhecido}"

  cat > "$DIRTY_MARKER_FILE" <<EOM
FAILED_REMOTE_COMMIT=$failed_commit
ROLLED_BACK_TO=$rollback_commit
FAILED_STAGE=$failed_stage
FAILED_COMMAND=$failed_command
FAILED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

clear_dirty_marker() {
  rm -f "$DIRTY_MARKER_FILE"
}

json_field_from_file() {
  local file="${1:?}"
  local field="${2:?}"
  python3 - "$file" "$field" <<'PYJSON'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(1)
value = data
for part in field.split('.'):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    raise SystemExit(0)
if isinstance(value, (list, dict)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(str(value))
PYJSON
}

read_app_command_sync_status() {
  APP_COMMAND_SYNC_SUMMARY="Comandos sem mudanças"
  APP_COMMAND_SYNC_WEBHOOK_BLOCK=""
  APP_COMMAND_SYNC_ADDED_COUNT=0
  APP_COMMAND_SYNC_REMOVED_COUNT=0
  APP_COMMAND_SYNC_CHANGED=0
  APP_COMMAND_SYNC_PERFORMED=0
  [[ -f "$APP_COMMAND_SYNC_STATUS_FILE" ]] || return 0
  local output
  output="$(python3 - "$APP_COMMAND_SYNC_STATUS_FILE" <<'PYCMD' 2>/dev/null || true
import json, shlex, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(0)
added = [str(x) for x in data.get('added') or []]
removed = [str(x) for x in data.get('removed') or []]
changed = bool(data.get('manifest_changed'))
performed = bool(data.get('sync_performed'))
reason = str(data.get('reason') or '')

def assign(name, value):
    print(f"{name}={shlex.quote(str(value))}")

assign('APP_COMMAND_SYNC_ADDED_COUNT', len(added))
assign('APP_COMMAND_SYNC_REMOVED_COUNT', len(removed))
assign('APP_COMMAND_SYNC_CHANGED', 1 if changed else 0)
assign('APP_COMMAND_SYNC_PERFORMED', 1 if performed else 0)
if added or removed:
    summary = f"Comandos sincronizados: +{len(added)} -{len(removed)}"
    lines = [f"Comandos: +{len(added)} -{len(removed)}"]
    if added:
        lines.append('Adicionados: ' + ', '.join(added[:12]) + (f", +{len(added)-12}" if len(added) > 12 else ''))
    if removed:
        lines.append('Removidos: ' + ', '.join(removed[:12]) + (f", +{len(removed)-12}" if len(removed) > 12 else ''))
    webhook = '\n'.join(lines)
else:
    if changed and performed:
        summary = 'Comandos sincronizados'
    elif changed:
        summary = 'Comandos revisados'
    else:
        summary = 'Comandos sem mudanças'
    webhook = ''
assign('APP_COMMAND_SYNC_SUMMARY', summary)
assign('APP_COMMAND_SYNC_WEBHOOK_BLOCK', webhook)
assign('APP_COMMAND_SYNC_REASON', reason)
PYCMD
)"
  [[ -n "${output//[[:space:]]/}" ]] || return 0
  eval "$output"
}

sanitize_commit_ref() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr -d '[:space:]')"
  if [[ "$value" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    printf '%s' "$value"
  fi
}

remote_commit_is_rejected() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" && -f "$REMOTE_REJECTED_FILE" ]] || return 1
  python3 - "$REMOTE_REJECTED_FILE" "$commit" <<'PYREJ' >/dev/null 2>&1
import json, sys
path, commit = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(1)
items = data.get('commits') if isinstance(data, dict) else None
if not isinstance(items, dict):
    raise SystemExit(1)
raise SystemExit(0 if commit in items else 1)
PYREJ
}

mark_remote_commit_rejected() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  local reason="${2:-rejeitado}"
  [[ -n "$commit" ]] || return 0
  mkdir -p "$(dirname "$REMOTE_REJECTED_FILE")" 2>/dev/null || true
  python3 - "$REMOTE_REJECTED_FILE" "$commit" "$reason" "$CURRENT_COMMIT" <<'PYREJ' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
commit, reason, live = sys.argv[2:5]
try:
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
items = data.get('commits') if isinstance(data.get('commits'), dict) else {}
items[commit] = {
    'reason': reason,
    'live_commit': live,
    'rejected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
data['commits'] = items
path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
PYREJ
  chown ubuntu:ubuntu "$REMOTE_REJECTED_FILE" 2>/dev/null || true
}

run_preflight_checks_in_dir() {
  local root="${1:?}"
  local py="$REPO_DIR/.venv/bin/python"
  local file checked_py=0 checked_sh=0 rc=0
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  [[ -n "$py" ]] || { PREFLIGHT_PY_STATUS="python indisponível"; PREFLIGHT_BASH_STATUS="não executado"; return 1; }

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$root/$file" ]] || continue
    checked_py=1
    if ! sudo -u ubuntu -H "$py" -m py_compile "$root/$file"; then
      rc=1
    fi
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity/' || true)
  if (( checked_py == 1 && rc == 0 )); then
    PREFLIGHT_PY_STATUS="OK"
  elif (( checked_py == 1 )); then
    PREFLIGHT_PY_STATUS="falhou"
  else
    PREFLIGHT_PY_STATUS="sem arquivos Python alterados"
  fi

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$root/$file" ]] || continue
    checked_sh=1
    if ! bash -n "$root/$file"; then
      rc=1
    fi
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.sh$' || true)
  if (( checked_sh == 1 && rc == 0 )); then
    PREFLIGHT_BASH_STATUS="OK"
  elif (( checked_sh == 1 )); then
    PREFLIGHT_BASH_STATUS="falhou"
  else
    PREFLIGHT_BASH_STATUS="sem scripts Bash alterados"
  fi
  PREFLIGHT_COG_IMPORT_STATUS="não executado no staging remoto"
  logger -t "$LOG_TAG" "Preflight staging: Python=$PREFLIGHT_PY_STATUS Bash=$PREFLIGHT_BASH_STATUS"
  return "$rc"
}

validate_remote_commit_in_staging() {
  local remote_commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$remote_commit" ]] || return 1
  REMOTE_WORKTREE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tts-bot-remote-candidate.XXXXXX")"
  rmdir "$REMOTE_WORKTREE_DIR" 2>/dev/null || true
  sudo -u ubuntu -H git -C "$REPO_DIR" worktree add --detach "$REMOTE_WORKTREE_DIR" "$remote_commit" >/dev/null || return 1
  run_preflight_checks_in_dir "$REMOTE_WORKTREE_DIR"
}

reject_remote_commit_without_live_apply() {
  local reason="${1:-validação local falhou}"
  mark_remote_commit_rejected "$REMOTE_COMMIT" "$reason"
  MANUAL_FAILURE_ALERT_SENT=1
  REMOTE_REJECT_REASON="$reason"
  local body
  body="Resumo: O commit do GitHub foi rejeitado antes de alterar a VPS.
Commit: $(short_commit "$REMOTE_COMMIT")
Estado preservado: $(short_commit "$CURRENT_COMMIT")
Motivo: $reason
Arquivos:
$(format_changed_files)
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  notify_zip_status_message "error" "❌ Atualização rejeitada" $'O commit do GitHub não passou na validação local.\nA VPS continuou no último estado saudável.' || true
  send_error "Atualização do GitHub rejeitada" "$body"
  logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") rejeitado antes do live: $reason"
  exit 0
}

load_pending_local_candidate() {
  local manifest active_file pending_file legacy_active legacy_pending active_payload
  LOCAL_CANDIDATE_PENDING_FILE=""
  LOCAL_CANDIDATE_DIR=""

  mkdir -p "$CANDIDATE_QUEUE_PENDING_DIR" "$CANDIDATE_QUEUE_ACTIVE_DIR" "$CANDIDATE_QUEUE_DONE_DIR" "$CANDIDATE_QUEUE_FAILED_DIR" "$CANDIDATE_QUEUE_CANCELLED_DIR" 2>/dev/null || true
  chown -R ubuntu:ubuntu "$CANDIDATE_QUEUE_ROOT" 2>/dev/null || true
  chmod 0775 "$CANDIDATE_QUEUE_ROOT" "$CANDIDATE_QUEUE_PENDING_DIR" "$CANDIDATE_QUEUE_ACTIVE_DIR" "$CANDIDATE_QUEUE_DONE_DIR" "$CANDIDATE_QUEUE_FAILED_DIR" "$CANDIDATE_QUEUE_CANCELLED_DIR" 2>/dev/null || true

  # Recuperação primeiro: se uma execução caiu com item ativo, retome esse item
  # antes de pegar outro pending. Isso evita aplicar fora de ordem.
  active_file="$(find "$CANDIDATE_QUEUE_ACTIVE_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort | head -n 1 || true)"
  if [[ -n "${active_file//[[:space:]]/}" ]]; then
    LOCAL_CANDIDATE_PENDING_FILE="$active_file"
    LOCAL_CANDIDATE_DIR="$(json_field_from_file "$active_file" candidate_dir 2>/dev/null || true)"
    if [[ -z "${LOCAL_CANDIDATE_DIR//[[:space:]]/}" || ! -d "$LOCAL_CANDIDATE_DIR" ]]; then
      mv "$active_file" "$CANDIDATE_QUEUE_FAILED_DIR/$(basename "$active_file").missing.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -f "$active_file" 2>/dev/null || true
      LOCAL_CANDIDATE_PENDING_FILE=""
      LOCAL_CANDIDATE_DIR=""
      return 1
    fi
    logger -t "$LOG_TAG" "Retomando item ativo da fila: $(basename "$active_file")"
  else
    # Compatibilidade com o formato antigo candidates/*/active.json.
    legacy_active="$(find "$CANDIDATE_ROOT" -mindepth 2 -maxdepth 2 -type f -name active.json 2>/dev/null | grep -v '/queue/' | sort | head -n 1 || true)"
    if [[ -n "${legacy_active//[[:space:]]/}" ]]; then
      LOCAL_CANDIDATE_PENDING_FILE="$legacy_active"
      LOCAL_CANDIDATE_DIR="$(dirname "$legacy_active")"
      logger -t "$LOG_TAG" "Retomando candidato local ativo legado: $(basename "$LOCAL_CANDIDATE_DIR")"
    else
      pending_file="$(find "$CANDIDATE_QUEUE_PENDING_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort | head -n 1 || true)"

      # Migração/compatibilidade: se o bot antigo ainda criou candidates/pending.json,
      # trate como um item de fila sem sobrescrever os novos pendentes.
      if [[ -z "${pending_file//[[:space:]]/}" && -f "$CANDIDATE_PENDING_FILE" ]]; then
        legacy_pending="$CANDIDATE_QUEUE_PENDING_DIR/legacy-$(date +%Y%m%d%H%M%S)-$(basename "$CANDIDATE_PENDING_FILE")"
        mv "$CANDIDATE_PENDING_FILE" "$legacy_pending" 2>/dev/null || true
        pending_file="$legacy_pending"
      fi

      if [[ -z "${pending_file//[[:space:]]/}" ]]; then
        return 1
      fi

      active_payload="$CANDIDATE_QUEUE_ACTIVE_DIR/$(basename "$pending_file")"
      if ! mv "$pending_file" "$active_payload" 2>/dev/null; then
        # Outro processo pode ter pego no mesmo instante. O flock torna isso raro,
        # mas falhar limpo evita duplicar aplicação.
        return 1
      fi
      LOCAL_CANDIDATE_PENDING_FILE="$active_payload"
      LOCAL_CANDIDATE_DIR="$(json_field_from_file "$active_payload" candidate_dir 2>/dev/null || true)"
      if [[ -z "${LOCAL_CANDIDATE_DIR//[[:space:]]/}" || ! -d "$LOCAL_CANDIDATE_DIR" ]]; then
        mv "$active_payload" "$CANDIDATE_QUEUE_FAILED_DIR/$(basename "$active_payload").missing.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -f "$active_payload" 2>/dev/null || true
        LOCAL_CANDIDATE_PENDING_FILE=""
        LOCAL_CANDIDATE_DIR=""
        return 1
      fi
      logger -t "$LOG_TAG" "Candidato local recebido da fila: $(basename "$LOCAL_CANDIDATE_DIR")"
    fi
  fi

  manifest="$LOCAL_CANDIDATE_DIR/manifest.json"
  if [[ ! -f "$manifest" ]] || ! python3 - "$manifest" <<'PYVALIDMANIFEST' >/dev/null 2>&1
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding='utf-8'))
if not isinstance(data, dict) or not str(data.get('id') or '').strip():
    raise SystemExit(1)
PYVALIDMANIFEST
  then
    logger -t "$LOG_TAG" "Candidato ativo inválido: manifest ausente ou corrompido em $LOCAL_CANDIDATE_DIR" 2>/dev/null || true
    LOCAL_CANDIDATE_MODE=1
    LOCAL_CANDIDATE_VERIFY_ERROR="manifest ausente ou corrompido"
    archive_local_candidate "failed"
    send_error "Candidato de update corrompido" "Resumo: Um item da fila tinha manifest ausente ou inválido e foi arquivado sem alterar a VPS.
Candidato: $(basename "$LOCAL_CANDIDATE_DIR")
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "candidate-corrupt-$(basename "$LOCAL_CANDIDATE_DIR")" || true
    LOCAL_CANDIDATE_MODE=0
    LOCAL_CANDIDATE_DIR=""
    LOCAL_CANDIDATE_PENDING_FILE=""
    return 1
  fi
  LOCAL_CANDIDATE_MODE=1
  LOCAL_CANDIDATE_ID="$(json_field_from_file "$manifest" id 2>/dev/null || true)"
  LOCAL_CANDIDATE_DISPLAY_ID="$(json_field_from_file "$manifest" display_id 2>/dev/null || true)"
  LOCAL_CANDIDATE_BASE_COMMIT="$(json_field_from_file "$manifest" base_commit 2>/dev/null || true)"
  LOCAL_CANDIDATE_COMMIT_MESSAGE="$(json_field_from_file "$manifest" commit_message 2>/dev/null || true)"
  LOCAL_CANDIDATE_ZIP_NAME="$(json_field_from_file "$manifest" zip_name 2>/dev/null || true)"
  LOCAL_CANDIDATE_ZIP_SHA256="$(json_field_from_file "$manifest" zip_sha256 2>/dev/null || true)"
  LOCAL_CANDIDATE_SOURCE_AUTHOR_ID="$(json_field_from_file "$manifest" discord_status.source_author_id 2>/dev/null || true)"
  LOCAL_CANDIDATE_FILES_DIR="$LOCAL_CANDIDATE_DIR/files"
  LOCAL_CANDIDATE_PATCH_FILE="$LOCAL_CANDIDATE_DIR/patch.diff"
  LOCAL_CANDIDATE_USE_PATCH=0
  BRANCH="$(json_field_from_file "$manifest" branch 2>/dev/null || true)"
  [[ -n "${BRANCH//[[:space:]]/}" ]] || BRANCH="main"
  [[ -n "${LOCAL_CANDIDATE_ID//[[:space:]]/}" ]] || LOCAL_CANDIDATE_ID="$(basename "$LOCAL_CANDIDATE_DIR")"
  [[ -n "${LOCAL_CANDIDATE_DISPLAY_ID//[[:space:]]/}" ]] || LOCAL_CANDIDATE_DISPLAY_ID="$LOCAL_CANDIDATE_ID"
  [[ -n "${LOCAL_CANDIDATE_COMMIT_MESSAGE//[[:space:]]/}" ]] || LOCAL_CANDIDATE_COMMIT_MESSAGE="update: aplicar $LOCAL_CANDIDATE_DISPLAY_ID"
  if [[ -f "$LOCAL_CANDIDATE_PENDING_FILE" ]]; then
    LOCAL_CANDIDATE_ATTEMPT="$(python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYATTEMPT' 2>/dev/null || echo 1
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
attempt = int(data.get('attempt') or 0) + 1
data.update({
    'state': 'active',
    'attempt': attempt,
    'started_at': data.get('started_at') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'heartbeat_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'last_error': None,
})
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
print(attempt)
PYATTEMPT
)"
  fi

  CHANGED_FILES_RAW="$(python3 - "$manifest" <<'PYFILES'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in data.get('changed_files') or []:
    item = str(item).strip()
    if item:
        print(item)
PYFILES
)"
  CHANGED_DIFF_NUMSTAT_RAW="$(python3 - "$manifest" <<'PYDIFF'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in ((data.get('diff_stats') or {}).get('entries') or []):
    path = str(item.get('path') or '').strip()
    if not path:
        continue
    if item.get('binary'):
        print(f'-\t-\t{path}')
    else:
        print(f"{int(item.get('added') or 0)}\t{int(item.get('removed') or 0)}\t{path}")
PYDIFF
)"
  zip_progress_hydrate_from_candidate || true
  return 0
}
verify_local_candidate_integrity() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  local py output rc max_age
  py="$REPO_DIR/.venv/bin/python"
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  [[ -n "$py" ]] || { LOCAL_CANDIDATE_VERIFY_ERROR="Python indisponível para validar o candidato"; return 1; }
  max_age="${DISCORD_AUTO_UPDATE_CANDIDATE_MAX_AGE_SECONDS:-86400}"
  [[ "$max_age" =~ ^[0-9]+$ ]] || max_age=86400
  set +e
  output="$(cd "$REPO_DIR" && sudo -u ubuntu -H "$py" -m utility.update_security verify-candidate "$LOCAL_CANDIDATE_DIR" --max-age-seconds "$max_age" 2>&1)"
  rc=$?
  set -e
  if (( rc == 0 )); then
    LOCAL_CANDIDATE_VERIFY_ERROR=""
    logger -t "$LOG_TAG" "Integridade confirmada para $LOCAL_CANDIDATE_DISPLAY_ID: $output"
    return 0
  fi
  LOCAL_CANDIDATE_VERIFY_ERROR="$(VERIFY_OUTPUT="$output" python3 - <<'PYVERIFY'
import json, os
raw = os.environ.get('VERIFY_OUTPUT') or ''
for line in reversed(raw.splitlines()):
    try:
        data = json.loads(line)
    except Exception:
        continue
    if isinstance(data, dict) and data.get('error'):
        print(str(data['error'])[:900])
        break
else:
    print(raw.strip()[-900:] or 'falha de integridade sem detalhes')
PYVERIFY
)"
  return 1
}

update_local_candidate_heartbeat() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -f "${LOCAL_CANDIDATE_PENDING_FILE:-}" ]] || return 0
  local state_name="${1:-active}"
  local error_text="${2:-}"
  local stage_name="${3:-}"
  STATE_NAME="$state_name" ERROR_TEXT="$error_text" STAGE_NAME="$stage_name" python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYHEART' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
data['state'] = os.environ.get('STATE_NAME') or 'active'
data['heartbeat_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
if os.environ.get('STAGE_NAME'):
    data['stage'] = os.environ['STAGE_NAME'][:300]
if os.environ.get('ERROR_TEXT'):
    data['last_error'] = os.environ['ERROR_TEXT'][:1200]
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYHEART
}

archive_local_candidate() {
  local status="${1:-done}" queue_archive_dir archived_candidate_dir stamp
  case "$status" in
    done) queue_archive_dir="$CANDIDATE_QUEUE_DONE_DIR" ;;
    cancelled) queue_archive_dir="$CANDIDATE_QUEUE_CANCELLED_DIR" ;;
    *) status="failed"; queue_archive_dir="$CANDIDATE_QUEUE_FAILED_DIR" ;;
  esac
  if [[ -z "${LOCAL_CANDIDATE_DIR:-}" ]]; then
    return 0
  fi
  stamp="$(date +%Y%m%d%H%M%S)"
  archived_candidate_dir="$CANDIDATE_ROOT/$status/$(basename "$LOCAL_CANDIDATE_DIR").$stamp"
  mkdir -p "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true
  chown ubuntu:ubuntu "$CANDIDATE_ROOT" "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true
  chmod 0775 "$CANDIDATE_ROOT" "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true

  update_local_candidate_heartbeat "$status" "${LOCAL_CANDIDATE_VERIFY_ERROR:-}" || true
  if [[ -f "${LOCAL_CANDIDATE_PENDING_FILE:-}" ]]; then
    ARCHIVE_STATUS="$status" ARCHIVED_CANDIDATE_DIR="$archived_candidate_dir" python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYARCHIVEQUEUE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
data.update({
    'state': os.environ.get('ARCHIVE_STATUS') or 'failed',
    'archived_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'archived_candidate_dir': os.environ.get('ARCHIVED_CANDIDATE_DIR') or '',
})
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYARCHIVEQUEUE
    mv "$LOCAL_CANDIDATE_PENDING_FILE" "$queue_archive_dir/$(basename "$LOCAL_CANDIDATE_PENDING_FILE").$stamp" 2>/dev/null || rm -f "$LOCAL_CANDIDATE_PENDING_FILE" 2>/dev/null || true
  fi
  if [[ -d "$LOCAL_CANDIDATE_DIR" ]]; then
    mv "$LOCAL_CANDIDATE_DIR" "$archived_candidate_dir" 2>/dev/null || rm -rf "$LOCAL_CANDIDATE_DIR" 2>/dev/null || true
    [[ -d "$archived_candidate_dir" ]] && touch "$archived_candidate_dir" 2>/dev/null || true
  fi
  refresh_pending_queue_messages || true
}

local_candidate_suspicion_reason() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ "${DISCORD_AUTO_UPDATE_ALLOW_FULL_REPO_ZIP:-0}" == "1" ]] && return 1
  [[ -f "${LOCAL_CANDIDATE_DIR:-}/manifest.json" ]] || return 1
  python3 - "$LOCAL_CANDIDATE_DIR/manifest.json" <<'PYSUSPECT'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(1)
zip_name = str(data.get('zip_name') or '').strip().lower()
changed = [str(x).strip() for x in (data.get('changed_files') or []) if str(x).strip()]
protected_prefixes = (
    '.git/', '.github/workflows/', 'data/', 'logs/', 'node_modules/',
    'secrets/', 'google-credentials', 'youtube-cookies',
)
safe_env_templates = {'.env.example', '.env.sample', '.env.template'}
reasons = []
if zip_name.startswith('repo-') or zip_name.startswith('tts-bot-main') or zip_name.startswith('tts-bot-base'):
    reasons.append('o arquivo parece uma base completa, não um patch')
if len(changed) > 120:
    reasons.append(f'muitos arquivos alterados para um patch normal ({len(changed)})')
for path in changed:
    low = path.lower()
    parts = pathlib.PurePosixPath(path.replace('\\', '/')).parts
    if any(part != part.strip() for part in parts):
        reasons.append(f'caminho suspeito ou inválido: {path}')
        break
    basename = parts[-1].lower() if parts else ''
    protected_env = basename == '.env' or (basename.startswith('.env.') and basename not in safe_env_templates)
    if protected_env or low.startswith(protected_prefixes) or '/node_modules/' in low or '/.git/' in low:
        reasons.append(f'caminho protegido/suspeito no ZIP: {path}')
        break
lockfiles = [p for p in changed if p.endswith('package-lock.json')]
if len(lockfiles) >= 2 and len(changed) > 20:
    reasons.append('parece conter árvore de projeto/frontend completa')
if reasons:
    print('; '.join(dict.fromkeys(reasons)))
PYSUSPECT
}

reject_local_candidate_safely() {
  local title="${1:-Atualização bloqueada}"
  local summary="${2:-O candidato foi arquivado sem alterar a VPS.}"
  local reason="${3:-candidato rejeitado}"
  MANUAL_FAILURE_ALERT_SENT=1
  trap - ERR
  set +e
  STAGE="candidato rejeitado"
  normalize_changed_file_permissions "antes de restaurar candidato rejeitado" || true
  cleanup_local_candidate_new_files_after_reset
  sudo -u ubuntu -H git reset --hard "${PREVIOUS_COMMIT:-HEAD}" >/dev/null 2>&1 || true
  cleanup_local_candidate_new_files_after_reset
  update_local_candidate_heartbeat "failed" "$reason" || true
  notify_zip_status_message "error" "$title" "$summary" || true
  archive_local_candidate "failed"
  send_error "$title" "Resumo: $summary
Motivo: $reason
Candidato: ${LOCAL_CANDIDATE_ID:-desconhecido}
ZIP: ${LOCAL_CANDIDATE_ZIP_NAME:-desconhecido}
Commit preservado: $(short_commit "${PREVIOUS_COMMIT:-$CURRENT_COMMIT}")
Arquivos:
$(format_changed_files)
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  logger -t "$LOG_TAG" "Candidato local ${LOCAL_CANDIDATE_ID:-desconhecido} rejeitado: $reason"
  exit 0
}

local_candidate_base_conflict_reason() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${LOCAL_CANDIDATE_BASE_COMMIT//[[:space:]]/}" ]] || return 1
  [[ -n "${REMOTE_COMMIT//[[:space:]]/}" ]] || return 1
  [[ "$LOCAL_CANDIDATE_BASE_COMMIT" != "$REMOTE_COMMIT" ]] || return 1

  if ! sudo -u ubuntu -H git cat-file -e "$LOCAL_CANDIDATE_BASE_COMMIT^{commit}" 2>/dev/null; then
    printf 'base original do ZIP não existe mais no repositório local'
    return 0
  fi
  if ! sudo -u ubuntu -H git merge-base --is-ancestor "$LOCAL_CANDIDATE_BASE_COMMIT" "$REMOTE_COMMIT" 2>/dev/null; then
    printf 'base original do ZIP não é ancestral do GitHub atual'
    return 0
  fi

  local remote_changed
  if ! remote_changed="$(sudo -u ubuntu -H git diff --name-only "$LOCAL_CANDIDATE_BASE_COMMIT" "$REMOTE_COMMIT" -- 2>/dev/null)"; then
    printf 'não foi possível comparar a base original do ZIP com o GitHub atual'
    return 0
  fi
  CANDIDATE_CHANGED="$CHANGED_FILES_RAW" REMOTE_CHANGED="$remote_changed" python3 - <<'PYBASE'
import os
candidate = {line.strip() for line in os.environ.get('CANDIDATE_CHANGED', '').splitlines() if line.strip()}
remote = {line.strip() for line in os.environ.get('REMOTE_CHANGED', '').splitlines() if line.strip()}
conflicts = sorted(candidate & remote)
if conflicts:
    shown = ', '.join(conflicts[:8])
    if len(conflicts) > 8:
        extra = len(conflicts) - 8
        shown += f', +{extra} ' + ('arquivo' if extra == 1 else 'arquivos')
    print(f'conflito com update anterior em: {shown}')
PYBASE
}

local_candidate_queue_has_pending() {
  find "$CANDIDATE_QUEUE_PENDING_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | grep -q .
}

trigger_updater_if_queue_pending() {
  if local_candidate_queue_has_pending; then
    (sleep 2; systemctl start --no-block "$UPDATER_UNIT" >/dev/null 2>&1 || true) &
  fi
}

refresh_pending_queue_messages() {
  [[ -d "$CANDIDATE_QUEUE_PENDING_DIR" ]] || return 0
  local payload
  while IFS= read -r payload; do
    [[ -n "${payload//[[:space:]]/}" ]] || continue
    send_update_status_payload "$payload" 0
  done < <(CANDIDATE_QUEUE_PENDING_DIR="$CANDIDATE_QUEUE_PENDING_DIR" CANDIDATE_QUEUE_ACTIVE_DIR="$CANDIDATE_QUEUE_ACTIVE_DIR" python3 - <<'PYQUEUESTATUS' 2>/dev/null || true
import datetime, json, os, pathlib
pending_root = pathlib.Path(os.environ['CANDIDATE_QUEUE_PENDING_DIR'])
active_root = pathlib.Path(os.environ['CANDIDATE_QUEUE_ACTIVE_DIR'])
active_count = sum(1 for p in active_root.glob('*.json') if p.is_file()) if active_root.is_dir() else 0
for index, queue_path in enumerate(sorted(pending_root.glob('*.json')), start=1):
    try:
        queue = json.loads(queue_path.read_text(encoding='utf-8'))
        candidate_dir = pathlib.Path(str(queue.get('candidate_dir') or ''))
        manifest = json.loads((candidate_dir / 'manifest.json').read_text(encoding='utf-8'))
        status = manifest.get('discord_status') or {}
        channel_id = str(status.get('channel_id') or '')
        message_id = str(status.get('message_id') or '')
        candidate_id = str(manifest.get('id') or queue.get('id') or '')
        display_id = str(manifest.get('display_id') or queue.get('display_id') or candidate_id)
        if not channel_id or not message_id or not candidate_id:
            continue
        position = active_count + index
        diff = manifest.get('diff_stats') or {}
        count = len(manifest.get('changed_files') or [])
        summary = str(diff.get('summary') or '').strip()
        position_at_enqueue = int(queue.get('queue_position_at_enqueue') or position)
        queue.update({
            'state': 'queued',
            'current_position': position,
            'position_updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        tmp = queue_path.with_name('.' + queue_path.name + '.tmp')
        tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        os.replace(tmp, queue_path)

        # O primeiro candidato recém-preparado já está com a animação ativa no
        # Discord. Não substitua esse painel por uma fila que não existe.
        if position == 1 and active_count == 0 and position_at_enqueue <= 1:
            continue

        file_line = (f'1 arquivo preparado' if count == 1 else f'{count} arquivos preparados') if count else ''
        if file_line and summary:
            file_line += f' · {summary}'

        if position == 1 and active_count == 0:
            lines = [f'Atualização `{display_id}`', 'Iniciando agora.']
            if file_line:
                lines.append(f'-# {file_line}')
            payload_status = 'applying'
            payload_title = '⚙️ Iniciando atualização'
        else:
            detail = f'1 atualização antes desta.' if position == 2 else f'{position - 1} atualizações antes desta.'
            lines = [f'Atualização `{display_id}`', f'Posição na fila: **{position}**', detail]
            if file_line:
                lines.append(f'-# {file_line}')
            payload_status = 'queued'
            payload_title = '📦 Atualização na fila'

        payload = {
            'channel_id': channel_id,
            'message_id': message_id,
            'status': payload_status,
            'title': payload_title,
            'description': '\n'.join(lines),
            'candidate_id': candidate_id,
            'display_id': display_id,
            'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'control': {'enabled': True, 'mode': 'cancel', 'candidate_id': candidate_id},
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        continue
PYQUEUESTATUS
  )
}

prune_archive_root() {
  local root="${1:?}" retention_days="${2:?}" keep_count="${3:?}"
  local -a entries=()
  local index path
  [[ -d "$root" && ! -L "$root" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || return 0
  [[ "$keep_count" =~ ^[0-9]+$ ]] || return 0
  (( keep_count < 1 )) && keep_count=1
  (( keep_count > 100 )) && keep_count=100

  find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$retention_days" -exec rm -rf -- {} + 2>/dev/null || true
  mapfile -t entries < <(
    find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%p\n' 2>/dev/null \
      | sort -t $'\t' -k1,1nr \
      | cut -f2-
  )
  for ((index=keep_count; index<${#entries[@]}; index++)); do
    path="${entries[$index]}"
    [[ -n "$path" && ! -L "$path" && "$(dirname -- "$path")" == "$root" ]] || continue
    rm -rf -- "$path" 2>/dev/null || true
  done
}

prune_updater_runtime_orphans() {
  local legacy_tmp="${TMPDIR:-/tmp}"
  [[ "$legacy_tmp" == /* && -d "$legacy_tmp" && ! -L "$legacy_tmp" ]] || return 0

  # Apenas prefixos privados do updater, sem recursão fora das raízes exatas.
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-update.*.run' -mmin +60 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-updater.*.log' -mmin +1440 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f \( -name 'tts-bot-git-add.*' -o -name 'tts-bot-git-add-retry.*' \) -mmin +360 -delete 2>/dev/null || true
  find "$legacy_tmp" -mindepth 1 -maxdepth 1 -type d \( -name 'tts-bot-remote-candidate.*' -o -name 'tts-bot-systemd-overlay.*' \) -mmin +360 -exec rm -rf -- {} + 2>/dev/null || true
  sudo -u ubuntu -H git -C "$REPO_DIR" worktree prune --expire=now >/dev/null 2>&1 || true
}

prune_rejected_remote_commits() {
  local retention_days="${DISCORD_AUTO_UPDATE_REJECTED_RETENTION_DAYS:-30}"
  local keep_count="${DISCORD_AUTO_UPDATE_REJECTED_KEEP_COUNT:-100}"
  [[ -f "$REMOTE_REJECTED_FILE" && ! -L "$REMOTE_REJECTED_FILE" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || retention_days=30
  [[ "$keep_count" =~ ^[0-9]+$ ]] || keep_count=100
  REJECTED_FILE_VALUE="$REMOTE_REJECTED_FILE" REJECTED_RETENTION_DAYS="$retention_days" REJECTED_KEEP_COUNT="$keep_count" \
    python3 - <<'PYPRUNEREJECTED' 2>/dev/null || true
import datetime, json, os, pathlib, tempfile

path = pathlib.Path(os.environ['REJECTED_FILE_VALUE'])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit
items = data.get('commits') if isinstance(data, dict) else None
if not isinstance(items, dict):
    raise SystemExit

now = datetime.datetime.now(datetime.timezone.utc)
cutoff = now - datetime.timedelta(days=max(0, int(os.environ['REJECTED_RETENTION_DAYS'])))
keep_count = max(1, min(1000, int(os.environ['REJECTED_KEEP_COUNT'])))
retained = []
for commit, record in items.items():
    if not isinstance(record, dict):
        retained.append((now, commit, record))
        continue
    raw = str(record.get('rejected_at') or '')
    try:
        stamp = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        stamp = now
    if stamp >= cutoff:
        retained.append((stamp, commit, record))
retained.sort(key=lambda item: (item[0], item[1]), reverse=True)
new_items = {commit: record for _stamp, commit, record in retained[:keep_count]}
if new_items == items:
    raise SystemExit
data['commits'] = new_items
fd, tmp_name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
tmp = pathlib.Path(tmp_name)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PYPRUNEREJECTED
  chown ubuntu:ubuntu "$REMOTE_REJECTED_FILE" 2>/dev/null || true
}

human_storage_bytes() {
  local value="${1:-0}"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$value" 2>/dev/null || printf '%s bytes' "$value"
  else
    printf '%s bytes' "$value"
  fi
}

guard_updater_disk_space() {
  local metrics total available hard_min hard_percent soft_percent hard_floor percent_floor event_id
  metrics="$(df -P -B1 "$REPO_DIR" 2>/dev/null | awk 'NR == 2 {print $2, $4}')"
  read -r total available <<< "$metrics"
  [[ "$total" =~ ^[0-9]+$ && "$available" =~ ^[0-9]+$ ]] || {
    logger -t "$LOG_TAG" "proteção de espaço: não foi possível ler o filesystem" 2>/dev/null || true
    return 0
  }

  hard_min="${TTS_BOT_UPDATER_DISK_HARD_MIN_BYTES:-2147483648}"
  hard_percent="${TTS_BOT_UPDATER_DISK_HARD_MIN_PERCENT:-5}"
  soft_percent="${TTS_BOT_UPDATER_DISK_SOFT_MIN_PERCENT:-15}"
  [[ "$hard_min" =~ ^[0-9]+$ ]] || hard_min=2147483648
  [[ "$hard_percent" =~ ^[0-9]+$ && "$hard_percent" -le 50 ]] || hard_percent=5
  [[ "$soft_percent" =~ ^[0-9]+$ && "$soft_percent" -le 80 ]] || soft_percent=15
  percent_floor=$((total * hard_percent / 100))
  hard_floor="$hard_min"
  (( percent_floor > hard_floor )) && hard_floor="$percent_floor"

  if (( available < hard_floor )); then
    event_id="updater-disk-critical-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "update pausado: espaço crítico, livre=$(human_storage_bytes "$available") mínimo=$(human_storage_bytes "$hard_floor")" 2>/dev/null || true
    send_error "Update pausado por pouco espaço" "Resumo: A atualização não foi iniciada para evitar uma aplicação parcial.
Espaço livre: $(human_storage_bytes "$available")
Mínimo seguro: $(human_storage_bytes "$hard_floor")
Bot: preservado e sem reinício
Ação: revise os artefatos de armazenamento antes de tentar novamente.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
    return 1
  fi

  if (( available * 100 < total * soft_percent )); then
    event_id="updater-disk-warning-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "proteção de espaço: nível preventivo, livre=$(human_storage_bytes "$available")" 2>/dev/null || true
    send_warn "Espaço da VPS abaixo do nível preventivo" "Resumo: O updater ainda pode funcionar, mas o espaço livre caiu abaixo de ${soft_percent}%.
Espaço livre: $(human_storage_bytes "$available")
Limite crítico atual: $(human_storage_bytes "$hard_floor")
Bot: preservado
Ação: revise os artefatos de armazenamento antes de atingir o bloqueio de segurança.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
  fi
  return 0
}

prune_update_artifacts() {
  local done_days="${DISCORD_AUTO_UPDATE_DONE_RETENTION_DAYS:-7}"
  local failed_days="${DISCORD_AUTO_UPDATE_FAILED_RETENTION_DAYS:-30}"
  local cancelled_days="${DISCORD_AUTO_UPDATE_CANCELLED_RETENTION_DAYS:-7}"
  local done_keep="${DISCORD_AUTO_UPDATE_DONE_KEEP_COUNT:-5}"
  local failed_keep="${DISCORD_AUTO_UPDATE_FAILED_KEEP_COUNT:-5}"
  local cancelled_keep="${DISCORD_AUTO_UPDATE_CANCELLED_KEEP_COUNT:-3}"
  local outbox_days="${DISCORD_AUTO_UPDATE_OUTBOX_RETENTION_DAYS:-7}"
  local receipt_days="${DISCORD_AUTO_UPDATE_DELIVERY_RECEIPT_RETENTION_DAYS:-30}"
  [[ "$done_days" =~ ^[0-9]+$ ]] || done_days=7
  [[ "$failed_days" =~ ^[0-9]+$ ]] || failed_days=30
  [[ "$cancelled_days" =~ ^[0-9]+$ ]] || cancelled_days=7
  [[ "$done_keep" =~ ^[0-9]+$ ]] || done_keep=5
  [[ "$failed_keep" =~ ^[0-9]+$ ]] || failed_keep=5
  [[ "$cancelled_keep" =~ ^[0-9]+$ ]] || cancelled_keep=3
  [[ "$outbox_days" =~ ^[0-9]+$ ]] || outbox_days=7
  [[ "$receipt_days" =~ ^[0-9]+$ ]] || receipt_days=30
  prune_archive_root "$CANDIDATE_ROOT/done" "$done_days" "$done_keep"
  prune_archive_root "$CANDIDATE_ROOT/failed" "$failed_days" "$failed_keep"
  prune_archive_root "$CANDIDATE_ROOT/cancelled" "$cancelled_days" "$cancelled_keep"
  find "$CANDIDATE_QUEUE_DONE_DIR" -type f -mtime "+$done_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_FAILED_DIR" -type f -mtime "+$failed_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_CANCELLED_DIR" -type f -mtime "+$cancelled_days" -delete 2>/dev/null || true
  find "$UPDATE_STATUS_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.attachment' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_DELIVERY_RECEIPTS_DIR" -type f -mtime "+$receipt_days" -delete 2>/dev/null || true
  prune_rejected_remote_commits
  prune_updater_runtime_orphans
}

git_add_changed_files_or_reject() {
  local context="${1:-git add}"
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add.XXXXXX")"
  if git_add_changed_files 2>"$errfile"; then
    rm -f "$errfile" 2>/dev/null || true
    return 0
  fi

  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true

  # Arquivos/pastas novos podem ser criados pelo updater root antes do commit.
  # O git add roda como ubuntu; se o path ficou root-owned, normalize e tente uma vez.
  if printf '%s' "$LAST_ERROR_STDERR" | grep -qiE 'Permission denied|unable to index file|adding files failed'; then
    normalize_changed_file_permissions "$context" || true
    errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add-retry.XXXXXX")"
    if git_add_changed_files 2>"$errfile"; then
      rm -f "$errfile" 2>/dev/null || true
      LAST_ERROR_STDERR=""
      return 0
    fi
    LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
    rm -f "$errfile" 2>/dev/null || true
  fi

  reject_local_candidate_safely \
    "Falha ao aplicar atualização" \
    "Não consegui preparar os arquivos do ZIP. A VPS foi restaurada e o candidato foi arquivado." \
    "$context: ${LAST_ERROR_STDERR:-git add falhou}"
}

write_local_candidate_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local state_name="${1:-state}"
  local extra_commit="${2:-}"
  python3 - "$LOCAL_CANDIDATE_DIR" "$state_name" "$extra_commit" <<'PYSTATE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
state = sys.argv[2]
commit = sys.argv[3]
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data.update({
    "state": state,
    "commit": commit,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, path)
PYSTATE
}

send_update_status_payload() {
  # `${1:-{}}` acrescenta uma chave `}` ao argumento em Bash, produzindo JSON
  # inválido e fazendo toda edição via /internal/update/zip-status ser descartada.
  local payload_json="${1:-}"
  [[ -n "${payload_json//[[:space:]]/}" ]] || payload_json='{}'
  local final_delivery="${2:-0}"
  prepare_update_delivery_dirs || true
  local rc=0
  if UPDATE_PAYLOAD_JSON="$payload_json" FINAL_DELIVERY="$final_delivery" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYSENDSTATUS'
import datetime, hashlib, json, os, pathlib, time, urllib.error, urllib.request, uuid

try:
    payload = json.loads(os.environ.get('UPDATE_PAYLOAD_JSON') or '{}')
except Exception:
    raise SystemExit(2)
if not isinstance(payload, dict) or not payload.get('channel_id') or not payload.get('message_id'):
    raise SystemExit(2)

payload.setdefault('event_at', datetime.datetime.now(datetime.timezone.utc).isoformat())
delivery_seed = '\0'.join(
    str(payload.get(key) or '')
    for key in ('channel_id', 'message_id', 'candidate_id', 'status', 'title', 'event_at')
)
delivery_id = str(payload.get('delivery_id') or '').strip() or hashlib.sha256(delivery_seed.encode('utf-8')).hexdigest()[:24]
payload['delivery_id'] = delivery_id

url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
final_delivery = str(os.environ.get('FINAL_DELIVERY') or '0').lower() in {'1', 'true', 'yes'}
try:
    attempts = max(1, min(10, int(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_ATTEMPTS') or (5 if final_delivery else 1))))
except (TypeError, ValueError):
    attempts = 5 if final_delivery else 1
try:
    base_delay = max(0.0, min(10.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_RETRY_DELAY_SECONDS') or 1.5)))
except (TypeError, ValueError):
    base_delay = 1.5
try:
    request_timeout = max(1.0, min(30.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_TIMEOUT_SECONDS') or 10)))
except (TypeError, ValueError):
    request_timeout = 10.0
last_error = ''
for attempt in range(attempts):
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            parsed = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if parsed.get('ok') and (parsed.get('delivered') or parsed.get('ignored')):
            raise SystemExit(0)
        last_error = str(parsed.get('error') or parsed or 'resposta sem confirmação')[:500]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode('utf-8', errors='ignore')
        except Exception:
            detail = ''
        last_error = f'HTTP {exc.code}: {detail[:400]}'
    except Exception as exc:
        last_error = f'{type(exc).__name__}: {exc}'[:500]
    if attempt + 1 < attempts:
        time.sleep(min(8.0, base_delay * (2 ** attempt)))

if not final_delivery:
    raise SystemExit(0)

outbox = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
try:
    outbox.mkdir(parents=True, exist_ok=True)
    safe_id = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in delivery_id)[:120]
    path = outbox / f'{safe_id}.json'
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    job = {
        'schema_version': 2,
        'delivery_id': delivery_id,
        'payload': payload,
        'created_at': existing.get('created_at') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'attempts': int(existing.get('attempts') or 0),
        'last_error': last_error or 'entrega indisponível',
        'next_attempt_at': 0,
    }
    tmp = path.with_name('.' + path.name + f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)
    os.chmod(path, 0o664)
except Exception as exc:
    print(f'falha ao persistir status final: {type(exc).__name__}: {exc}', file=os.sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)
PYSENDSTATUS
  then
    rc=0
  else
    rc=$?
  fi
  chown ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  if (( rc != 0 )); then
    logger -t "$LOG_TAG" "falha ao entregar ou persistir status final do update (rc=$rc)" 2>/dev/null || true
  fi
  return "$rc"
}

flush_update_status_outbox() {
  prepare_update_delivery_dirs || true
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYFLUSHSTATUS'
import datetime, json, os, pathlib, time, urllib.error, urllib.request

root = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
if not root.is_dir():
    raise SystemExit(0)
url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

now = time.time()

# Um processo pode morrer depois de renomear o job para .sending.*. Nesse caso,
# o glob normal não o encontra mais e a entrega ficava presa para sempre.
# Recoloque claims antigos na fila antes de buscar novos trabalhos.
for stale_claim in root.glob('.sending.*.json'):
    try:
        if now - stale_claim.stat().st_mtime < 120:
            continue
        parts = stale_claim.name.split('.', 3)
        original_name = parts[3] if len(parts) == 4 and parts[3] else f'recovered-{int(now)}.json'
        target = root / original_name
        if target.exists():
            target = root / f'recovered-{int(now)}-{os.getpid()}-{original_name}'
        os.replace(stale_claim, target)
    except OSError:
        pass

for path in sorted(root.glob('*.json'), key=lambda item: item.stat().st_mtime)[:100]:
    claim = path.with_name(f'.sending.{os.getpid()}.{path.name}')
    try:
        os.replace(path, claim)
    except OSError:
        continue
    data = {}
    try:
        data = json.loads(claim.read_text(encoding='utf-8'))
        payload = data.get('payload') if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise ValueError('job de status sem payload válido')
        attempts = int(data.get('attempts') or 0)
        next_attempt_at = float(data.get('next_attempt_at') or 0)
        if next_attempt_at > now:
            os.replace(claim, path)
            continue
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if result.get('ok') and (result.get('delivered') or result.get('ignored')):
            claim.unlink(missing_ok=True)
            continue
        raise RuntimeError(str(result.get('error') or result or 'sem confirmação'))
    except Exception as exc:
        try:
            permanent = isinstance(exc, (json.JSONDecodeError, ValueError, TypeError, AttributeError))
            attempts = 20 if permanent else (int(data.get('attempts') or 0) + 1 if isinstance(data, dict) else 1)
            if attempts >= 20:
                dead = root / 'failed'
                dead.mkdir(parents=True, exist_ok=True)
                failed_path = dead / path.name
                if isinstance(data, dict):
                    data['attempts'] = attempts
                    data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
                    data['failed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
                os.replace(claim, failed_path)
                continue
            if not isinstance(data, dict):
                data = {}
            data['attempts'] = attempts
            data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
            data['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data['next_attempt_at'] = now + min(300, 5 * (2 ** min(attempts, 6)))
            claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
            os.replace(claim, path)
        except Exception:
            try:
                os.replace(claim, path)
            except Exception:
                pass
PYFLUSHSTATUS
  local rc=$?
  chown -R ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  return "$rc"
}

flush_update_alert_outbox() {
  prepare_update_delivery_dirs || true
  UPDATE_ALERT_OUTBOX_DIR="$UPDATE_ALERT_OUTBOX_DIR" UPDATE_DELIVERY_RECEIPTS_DIR="$UPDATE_DELIVERY_RECEIPTS_DIR" \
  REPO_DIR="$REPO_DIR" python3 - <<'PYFLUSHALERT'
import datetime, json, os, pathlib, subprocess, time

root = pathlib.Path(os.environ['UPDATE_ALERT_OUTBOX_DIR'])
receipts = pathlib.Path(os.environ['UPDATE_DELIVERY_RECEIPTS_DIR'])
repo = pathlib.Path(os.environ.get('REPO_DIR') or '/home/ubuntu/bot')
if not root.is_dir():
    raise SystemExit(0)
receipts.mkdir(parents=True, exist_ok=True)
now = time.time()

# Um processo pode morrer depois de renomear o job para .sending.*. Nesse caso,
# o glob normal não o encontra mais e a entrega ficava presa para sempre.
# Recoloque claims antigos na fila antes de buscar novos trabalhos.
for stale_claim in root.glob('.sending.*.json'):
    try:
        if now - stale_claim.stat().st_mtime < 120:
            continue
        parts = stale_claim.name.split('.', 3)
        original_name = parts[3] if len(parts) == 4 and parts[3] else f'recovered-{int(now)}.json'
        target = root / original_name
        if target.exists():
            target = root / f'recovered-{int(now)}-{os.getpid()}-{original_name}'
        os.replace(stale_claim, target)
    except OSError:
        pass

for path in sorted(root.glob('*.json'), key=lambda item: item.stat().st_mtime)[:100]:
    claim = path.with_name(f'.sending.{os.getpid()}.{path.name}')
    try:
        os.replace(path, claim)
    except OSError:
        continue
    attachment = ''
    data = {}
    try:
        data = json.loads(claim.read_text(encoding='utf-8'))
        event_id = str(data.get('event_id') or path.stem)
        safe_id = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in event_id)[:120]
        receipt = receipts / f'{safe_id}.alert.done'
        attachment = str(data.get('attachment') or '')
        attachment_path = None
        if attachment:
            candidate_attachment = pathlib.Path(attachment).resolve(strict=False)
            try:
                candidate_attachment.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError('anexo do alerta fora do outbox') from exc
            attachment_path = candidate_attachment
            attachment = str(candidate_attachment)
        if receipt.is_file():
            claim.unlink(missing_ok=True)
            if attachment_path is not None:
                attachment_path.unlink(missing_ok=True)
            continue
        attempts = int(data.get('attempts') or 0)
        next_attempt_at = float(data.get('next_attempt_at') or 0)
        if next_attempt_at > now:
            os.replace(claim, path)
            continue
        args = [
            'sudo', '-u', 'ubuntu', '/usr/bin/env', 'bash', str(repo / 'alert.sh'),
            str(data.get('type') or 'info'),
            str(data.get('title') or 'Auto update'),
            str(data.get('body') or ''),
            attachment,
            str(data.get('attachment_name') or ''),
        ]
        completed = subprocess.run(args, cwd=str(repo), text=True, capture_output=True, timeout=35, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or f'return {completed.returncode}')[-800:])
        tmp = receipt.with_name('.' + receipt.name + f'.{os.getpid()}.tmp')
        tmp.write_text(datetime.datetime.now(datetime.timezone.utc).isoformat() + '\n', encoding='utf-8')
        os.replace(tmp, receipt)
        claim.unlink(missing_ok=True)
        if attachment_path is not None:
            attachment_path.unlink(missing_ok=True)
    except Exception as exc:
        try:
            permanent = isinstance(exc, (json.JSONDecodeError, ValueError, TypeError, AttributeError))
            attempts = 20 if permanent else (int(data.get('attempts') or 0) + 1 if isinstance(data, dict) else 1)
            if attempts >= 20:
                dead = root / 'failed'
                dead.mkdir(parents=True, exist_ok=True)
                if not isinstance(data, dict):
                    data = {}
                data['attempts'] = attempts
                data['last_error'] = f'{type(exc).__name__}: {exc}'[:800]
                data['failed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
                os.replace(claim, dead / path.name)
                continue
            if not isinstance(data, dict):
                data = {}
            data['attempts'] = attempts
            data['last_error'] = f'{type(exc).__name__}: {exc}'[:800]
            data['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data['next_attempt_at'] = now + min(300, 5 * (2 ** min(attempts, 6)))
            claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
            os.replace(claim, path)
        except Exception:
            try:
                os.replace(claim, path)
            except Exception:
                pass
PYFLUSHALERT
  local rc=$?
  chown -R ubuntu:ubuntu "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || true
  return "$rc"
}

notify_zip_status_message() {
  local status="${1:-info}"
  local title="${2:-Atualização}"
  local description="${3:-}"
  local final_delivery=0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1
  if (( REMOTE_CANDIDATE_MODE == 1 )); then
    post_direct_update_message "$REMOTE_STATUS_CHANNEL_ID" "$REMOTE_STATUS_MESSAGE_ID" "$status" "$title" "$description" "${ZIP_STATUS_CONTROL_JSON:-}"
    return 0
  fi
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]] || return 0
  local channel_id message_id payload
  channel_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.channel_id 2>/dev/null || true)"
  message_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.message_id 2>/dev/null || true)"
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="${ZIP_STATUS_CONTROL_JSON:-}" CANDIDATE_ID_VALUE="${LOCAL_CANDIDATE_ID:-}" DISPLAY_ID_VALUE="${LOCAL_CANDIDATE_DISPLAY_ID:-}" python3 - <<'PYBUILDPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'candidate_id': os.environ.get('CANDIDATE_ID_VALUE') or '',
    'display_id': os.environ.get('DISPLAY_ID_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYBUILDPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
}

post_direct_update_message() {
  local channel_id="${1:-}"
  local message_id="${2:-}"
  local status="${3:-info}"
  local title="${4:-Atualização}"
  local description="${5:-}"
  local control_json="${6:-}"
  local final_delivery=0 payload
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="$control_json" python3 - <<'PYDIRECTPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYDIRECTPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
}

create_direct_update_message() {
  local status="${1:-applying}"
  local title="${2:-$UPDATE_TITLE_EMOJI Aplicando atualização...}"
  local description="${3:-}"
  STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYCREATE' 2>/dev/null || true
import json, os, shlex, urllib.request
from pathlib import Path
payload = {
    "status": os.environ.get("STATUS_VALUE") or "applying",
    "title": os.environ.get("TITLE_VALUE") or "Atualização",
    "description": os.environ.get("DESCRIPTION_VALUE") or "",
}
url = (os.environ.get("BOT_HEALTH_URL") or "http://127.0.0.1:10000/health").replace("/health", "/internal/update/create-zip-status")
headers = {"Content-Type": "application/json"}
try:
    env_path = Path(os.environ.get("REPO_DIR", "/home/ubuntu/bot")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    headers["X-Update-Token"] = token
                break
except Exception:
    pass
try:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
    if data.get("ok"):
        print("REMOTE_STATUS_CHANNEL_ID=" + shlex.quote(str(data.get("channel_id") or "")))
        print("REMOTE_STATUS_MESSAGE_ID=" + shlex.quote(str(data.get("message_id") or "")))
except Exception:
    pass
PYCREATE
}

zip_progress_title() {
  local stage_label="${1:-Processando atualização}"
  local lowered="${stage_label,,}"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      printf '↪️ Reaplicando atualização'
    else
      printf '↩️ Revertendo atualização'
    fi
    return 0
  fi

  # O título acompanha a fase sem expor o subsistema alterado. A etapa detalhada
  # continua logo abaixo, enquanto o cabeçalho deixa claro que o painel avançou.
  case "$lowered" in
    *fila*|*aguard*)
      printf '📦 Atualização na fila'
      ;;
    *finalizando*)
      printf '✅ Finalizando atualização'
      ;;
    *fazendo\ commit*|*commit\ criado*|*registrando*)
      printf '📝 Registrando atualização'
      ;;
    *publicando\ no\ github*|*github*)
      printf '☁️ Publicando atualização'
      ;;
    *publicando*|*enviando*)
      printf '🚀 Publicando atualização'
      ;;
    *compilando*|*build*)
      printf '🛠️ Compilando atualização'
      ;;
    *instalando*|*preparando\ servidor*|*dependências*)
      printf '📦 Preparando atualização'
      ;;
    *reiniciando*)
      printf '🔄 Reiniciando serviços'
      ;;
    *recarregando*|*reload*)
      printf '🔄 Aplicando atualização'
      ;;
    *conferindo*|*validando*|*verificando*|*analisando*|*recuperando*)
      printf '🔎 Verificando atualização'
      ;;
    *aplicando*|*preparando\ arquivos*)
      printf '⚙️ Aplicando atualização'
      ;;
    *)
      printf '⚙️ Atualizando'
      ;;
  esac
}

zip_progress_status() {
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf 'progress'
  else
    printf 'applying'
  fi
}

zip_progress_identifier() {
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    printf '%s' "${LOCAL_CANDIDATE_DISPLAY_ID:-$LOCAL_CANDIDATE_ID}"
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf '%s' "${ROLLBACK_REQUEST_ID:-controle}"
  elif [[ -n "${SHORT_TO:-}" ]]; then
    printf 'commit %s' "$SHORT_TO"
  else
    printf 'atualização'
  fi
}

zip_progress_hydrate_from_candidate() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  (( ZIP_PROGRESS_HANDOFF_LOADED == 0 )) || return 0
  [[ -f "${LOCAL_CANDIDATE_DIR:-}/manifest.json" ]] || return 0

  local record_kind elapsed_ms encoded_label label total_ms=0 summed_ms=0 now_ms line index
  local -a handoff_labels=()
  local -a handoff_elapsed=()

  while IFS=$'\t' read -r record_kind elapsed_ms encoded_label; do
    case "$record_kind" in
      META)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && total_ms="$elapsed_ms"
        ;;
      STEP)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
        label="$(printf '%s' "$encoded_label" | base64 --decode 2>/dev/null || true)"
        label="$(printf '%s' "$label" | tr '\r\n\t' '   ' | tr -s ' ' | sed 's/^ //;s/ $//' | cut -c1-120)"
        [[ -n "${label//[[:space:]]/}" ]] || continue
        if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
          && printf '%s\n' "$ZIP_PROGRESS_DONE_LABELS" | grep -Fxq -- "$label"; then
          continue
        fi
        handoff_labels+=("$label")
        handoff_elapsed+=("$elapsed_ms")
        summed_ms=$((summed_ms + elapsed_ms))
        ;;
    esac
  done < <(python3 - "${LOCAL_CANDIDATE_DIR}/manifest.json" <<'PYHANDOFF' 2>/dev/null || true
import base64
import json
import pathlib
import sys

try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

handoff = data.get("progress_handoff")
if not isinstance(handoff, dict) or int(handoff.get("schema_version") or 0) != 1:
    raise SystemExit(0)

try:
    total_ms = max(0, min(int(handoff.get("preparation_total_ms") or 0), 15 * 60 * 1000))
except (TypeError, ValueError):
    total_ms = 0
print(f"META\t{total_ms}\t")

seen: set[str] = set()
steps = handoff.get("completed_steps")
if not isinstance(steps, list):
    steps = []
for raw in steps[:20]:
    if not isinstance(raw, dict):
        continue
    label = " ".join(str(raw.get("label") or "").split())[:120]
    if not label or label in seen:
        continue
    try:
        elapsed_ms = max(0, min(int(raw.get("elapsed_ms") or 0), 10 * 60 * 1000))
    except (TypeError, ValueError):
        elapsed_ms = 0
    seen.add(label)
    encoded = base64.b64encode(label.encode("utf-8")).decode("ascii")
    print(f"STEP\t{elapsed_ms}\t{encoded}")
PYHANDOFF
  )

  ((${#handoff_labels[@]} > 0)) || return 0
  (( total_ms < summed_ms )) && total_ms="$summed_ms"
  (( total_ms > 900000 )) && total_ms=900000

  for ((index=0; index<${#handoff_labels[@]}; index++)); do
    label="${handoff_labels[$index]}"
    elapsed_ms="${handoff_elapsed[$index]}"
    if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
      ZIP_PROGRESS_DONE_LABELS+=$'\n'
    fi
    ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${label}"
    line="-# ✅ $label · $(format_update_duration_ms "$elapsed_ms")"
    if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
      ZIP_PROGRESS_HISTORY+=$'\n'
    fi
    ZIP_PROGRESS_HISTORY+="$line"
    ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  done
  zip_progress_trim_history

  now_ms="$(update_now_ms)"
  if [[ "$now_ms" =~ ^[0-9]+$ ]] && (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    ZIP_PROGRESS_STARTED_MS=$((now_ms - total_ms))
    (( ZIP_PROGRESS_STARTED_MS < 0 )) && ZIP_PROGRESS_STARTED_MS=0
  fi
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
  ZIP_PROGRESS_HANDOFF_LOADED=1
}

zip_progress_trim_history() {
  local line_count
  line_count="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'NF {c++} END {print c+0}')"
  while (( line_count > ZIP_PROGRESS_MAX_VISIBLE_STEPS )); do
    ZIP_PROGRESS_HISTORY="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'BEGIN{removed=0} {if (!removed && NF) {removed=1; next} print}')"
    ZIP_PROGRESS_HIDDEN_COUNT=$((ZIP_PROGRESS_HIDDEN_COUNT + 1))
    line_count=$((line_count - 1))
  done
}

zip_progress_publish() {
  local stage_label="${1:-Processando atualização}"
  local detail="${2:-}"
  local title status description identifier now_ms elapsed_ms elapsed_text footer stage_changed=0
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    ZIP_PROGRESS_STARTED_MS="$now_ms"
  fi
  if [[ "$ZIP_PROGRESS_STAGE_LABEL" != "$stage_label" || "$ZIP_PROGRESS_STAGE_STARTED_MS" -le 0 ]]; then
    ZIP_PROGRESS_STAGE_LABEL="$stage_label"
    stage_changed=1
  fi
  if declare -F write_update_runtime_state >/dev/null 2>&1; then
    write_update_runtime_state "$stage_label"
  fi
  title="$(zip_progress_title "$stage_label")"
  status="$(zip_progress_status)"
  description=""
  if (( ZIP_PROGRESS_HIDDEN_COUNT > 0 )); then
    if (( ZIP_PROGRESS_HIDDEN_COUNT == 1 )); then
      description+="-# … 1 etapa anterior concluída"$'\n'
    else
      description+="-# … $ZIP_PROGRESS_HIDDEN_COUNT etapas anteriores concluídas"$'\n'
    fi
  fi
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    description+="$ZIP_PROGRESS_HISTORY"$'\n'
  fi
  description+="$UPDATE_STAGE_EMOJI **$stage_label**"
  if [[ -n "${detail//[[:space:]]/}" ]]; then
    description+=$'\n'"-# $detail"
  fi
  identifier="$(zip_progress_identifier)"
  elapsed_ms=$((now_ms - ZIP_PROGRESS_STARTED_MS))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  if (( ZIP_PROGRESS_COMPLETED_COUNT == 1 )); then
    footer="$identifier · 1 etapa concluída · $elapsed_text"
  else
    footer="$identifier · $ZIP_PROGRESS_COMPLETED_COUNT etapas concluídas · $elapsed_text"
  fi
  description+=$'\n'"-# $footer"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "$status" "$title" "$description" || true
  else
    notify_zip_status_message "$status" "$title" "$description" || true
  fi
  if (( stage_changed == 1 )); then
    ZIP_PROGRESS_STAGE_STARTED_MS="$(update_now_ms)"
  fi
  update_local_candidate_heartbeat "active" "" "$stage_label"
}

zip_progress_done() {
  local done_label="${1:-}"
  [[ -n "${done_label//[[:space:]]/}" ]] || return 0
  # Uma retomada ou uma transição repetida não pode recolocar a mesma microetapa
  # no histórico. O painel sempre avança de forma monotônica.
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
    && printf '%s\n' "${ZIP_PROGRESS_DONE_LABELS:-}" | grep -Fxq -- "$done_label"; then
    return 0
  fi
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
    ZIP_PROGRESS_DONE_LABELS+=$'\n'
  fi
  ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${done_label}"
  local now_ms elapsed_ms elapsed_text line
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STAGE_STARTED_MS > 0 )); then
    elapsed_ms=$((now_ms - ZIP_PROGRESS_STAGE_STARTED_MS))
  else
    elapsed_ms=0
  fi
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  line="-# ✅ $done_label · $elapsed_text"
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    ZIP_PROGRESS_HISTORY+=$'\n'
  fi
  ZIP_PROGRESS_HISTORY+="$line"
  zip_progress_trim_history
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
}

zip_progress_done_and_publish() {
  local done_label="${1:-}"
  local next_label="${2:-Processando atualização}"
  local detail="${3:-}"
  zip_progress_done "$done_label"
  zip_progress_publish "$next_label" "$detail"
}

zip_progress_heartbeat_seconds() {
  local interval="${DISCORD_AUTO_UPDATE_PROGRESS_HEARTBEAT_SECONDS:-12}"
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=12
  (( interval < 5 )) && interval=5
  (( interval > 60 )) && interval=60
  printf '%s' "$interval"
}

zip_progress_run_as_ubuntu() {
  local stage_label="${1:?}"
  local detail="${2:-Em andamento}"
  local command="${3:?}"
  local pid rc started_ms now_ms elapsed_ms interval next_publish_ms

  zip_progress_publish "$stage_label" "$detail"
  interval="$(zip_progress_heartbeat_seconds)"
  started_ms="$(update_now_ms)"
  next_publish_ms=$((started_ms + interval * 1000))

  # O comando roda em um shell filho sem herdar o trap ERR transacional. Assim,
  # o pai pode acompanhar o PID, publicar heartbeats e tratar o status uma vez.
  sudo -u ubuntu -H bash -lc "$command" &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
    now_ms="$(update_now_ms)"
    if (( now_ms >= next_publish_ms )); then
      elapsed_ms=$((now_ms - started_ms))
      (( elapsed_ms < 0 )) && elapsed_ms=0
      zip_progress_publish "$stage_label" "$detail · $(format_update_duration_ms "$elapsed_ms")"
      next_publish_ms=$((now_ms + interval * 1000))
    fi
  done

  local restore_errexit=0
  [[ $- == *e* ]] && restore_errexit=1
  set +e
  wait "$pid"
  rc=$?
  if (( restore_errexit == 1 )); then
    set -e
  else
    set +e
  fi
  return "$rc"
}

zip_progress_only_site_changed() {
  (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )) || return 1
  (( BOT_CHANGED == 0 )) || return 1
  (( REQUIREMENTS_CHANGED == 0 )) || return 1
  (( AUDIO_SYSTEMD_CHANGED == 0 )) || return 1
  (( CLEANUP_CHANGED == 0 )) || return 1
  (( PHONE_LAVALINK_WATCH_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_WATCH_CHANGED == 0 )) || return 1
  (( VPS_SYSTEMD_UNITS_CHANGED == 0 )) || return 1
  (( ALERT_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_SYNC_REQUIRED == 0 )) || return 1
  (( CORE_WORKER_APK_CHANGED == 0 )) || return 1
  (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )) || return 1
  return 0
}

zip_progress_process_detail() {
  local processes
  processes="$(format_changed_processes 2>/dev/null || true)"
  if [[ -n "${processes//[[:space:]]/}" && "$processes" != "nenhum processo alterado" ]]; then
    printf '%s' "$processes"
  fi
}

format_cog_module_names() {
  local modules_text="${1:-}"
  MODULES_TEXT="$modules_text" python3 - <<'PYCOGS'
import os
mods = []
for raw in (os.environ.get("MODULES_TEXT") or "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    name = raw
    if name.startswith("cogs."):
        name = name[5:]
    mods.append(name.replace("_", "-"))
print(", ".join(dict.fromkeys(mods)))
PYCOGS
}

fast_reload_stage_label() {
  local modules_text="${1:-}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local names count
  names="$(format_cog_module_names "$modules_text")"
  count="$(printf '%s\n' "$modules_text" | awk 'NF {c++} END {print c+0}')"
  if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
    printf 'Recarregando cogs: %s' "$names"
  else
    printf 'Recarregando cog: %s' "$names"
  fi
}

zip_progress_next_apply_stage() {
  local fast_modules process_detail
  fast_modules="$(fast_reload_modules_for_changed_files 2>/dev/null || true)"
  FAST_RELOAD_MODULES="$fast_modules"
  if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
    fast_reload_stage_label "$fast_modules"
    return 0
  fi
  # Mostre a primeira operação real, não um reinício genérico. Em patches do
  # site, npm ci/build é normalmente a parte mais longa e precisa ficar visível.
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 1 )); then
    printf 'Instalando dependências'
    return 0
  fi
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 0 && BACK_CHANGED == 1 )); then
    printf 'Preparando servidor'
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    if [[ "$process_detail" == *,* ]]; then
      printf 'Reiniciando processos: %s' "$process_detail"
    else
      printf 'Reiniciando processo: %s' "$process_detail"
    fi
    return 0
  fi
  printf 'Validando aplicação'
}

zip_progress_done_apply_stage() {
  # Frontend/backend já publicam suas próprias fases e health check. Não acrescente
  # depois uma etapa genérica de “processo reiniciado”, que fazia o painel parecer
  # voltar ao início justamente quando o build terminava.
  if zip_progress_only_site_changed; then
    return 0
  fi
  local process_detail names count
  if [[ "${FAST_RELOAD_STATUS:-}" == "OK"* && -n "${FAST_RELOAD_MODULES//[[:space:]]/}" ]]; then
    names="$(format_cog_module_names "$FAST_RELOAD_MODULES")"
    count="$(printf '%s\n' "$FAST_RELOAD_MODULES" | awk 'NF {c++} END {print c+0}')"
    if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
      zip_progress_done "Cogs recarregadas: **$names**"
    else
      zip_progress_done "Cog recarregada: **$names**"
    fi
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    zip_progress_done "Processos reiniciados: **$process_detail**"
  else
    zip_progress_done "Aplicação validada"
  fi
}

rollback_request_roots() {
  printf '%s\n' "$ROLLBACK_REQUEST_DEFAULT_ROOT"
  if [[ -n "${ROLLBACK_REQUEST_DATA_ROOT//[[:space:]]/}" && "$ROLLBACK_REQUEST_DATA_ROOT" != "$ROLLBACK_REQUEST_DEFAULT_ROOT" ]]; then
    printf '%s\n' "$ROLLBACK_REQUEST_DATA_ROOT"
  fi
  if [[ -n "${ROLLBACK_REQUEST_TMP_ROOT//[[:space:]]/}" && "$ROLLBACK_REQUEST_TMP_ROOT" != "$ROLLBACK_REQUEST_DEFAULT_ROOT" && "$ROLLBACK_REQUEST_TMP_ROOT" != "$ROLLBACK_REQUEST_DATA_ROOT" ]]; then
    printf '%s\n' "$ROLLBACK_REQUEST_TMP_ROOT"
  fi
}

ensure_rollback_request_dirs() {
  local root
  while IFS= read -r root; do
    [[ -n "${root//[[:space:]]/}" ]] || continue
    mkdir -p "$root" "$root/done" "$root/failed" 2>/dev/null || true
    chown ubuntu:ubuntu "$root" "$root/done" "$root/failed" 2>/dev/null || true
    chmod 0775 "$root" "$root/done" "$root/failed" 2>/dev/null || true
  done < <(rollback_request_roots)
}

select_rollback_request_root() {
  local root pending active
  ensure_rollback_request_dirs
  while IFS= read -r root; do
    [[ -n "${root//[[:space:]]/}" ]] || continue
    pending="$root/pending.json"
    active="$root/active.json"
    if [[ -f "$pending" ]]; then
      mv "$pending" "$active" 2>/dev/null || true
    fi
    if [[ -f "$active" ]]; then
      ROLLBACK_REQUEST_ROOT="$root"
      ROLLBACK_REQUEST_PENDING_FILE="$pending"
      ROLLBACK_REQUEST_ACTIVE_FILE="$active"
      return 0
    fi
  done < <(rollback_request_roots)
  return 1
}

load_pending_rollback_request() {
  ROLLBACK_CONTROL_MODE=0
  ROLLBACK_REQUEST_FILE=""
  select_rollback_request_root || return 1
  ROLLBACK_CONTROL_MODE=1
  ROLLBACK_REQUEST_FILE="$ROLLBACK_REQUEST_ACTIVE_FILE"
  ROLLBACK_REQUEST_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" id 2>/dev/null || true)"
  ROLLBACK_REQUEST_ACTION="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" mode 2>/dev/null || true)"
  ROLLBACK_REQUEST_BRANCH="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" branch 2>/dev/null || true)"
  ROLLBACK_EXPECTED_HEAD="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" expected_head 2>/dev/null || true)")"
  ROLLBACK_REVERT_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" revert_commit 2>/dev/null || true)")"
  ROLLBACK_UPDATE_FROM="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" update_from 2>/dev/null || true)")"
  ROLLBACK_UPDATE_TO="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" update_to 2>/dev/null || true)")"
  ROLLBACK_ROLLBACK_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" rollback_commit 2>/dev/null || true)")"
  ROLLBACK_REDO_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" redo_commit 2>/dev/null || true)")"
  ROLLBACK_MESSAGE_CHANNEL_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.channel_id 2>/dev/null || true)"
  ROLLBACK_MESSAGE_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.message_id 2>/dev/null || true)"
  ROLLBACK_SOURCE_AUTHOR_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" source_author_id 2>/dev/null || true)"
  ROLLBACK_REQUESTED_BY="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" requested_by 2>/dev/null || true)"
  ROLLBACK_PREVIOUS_RECORD_JSON="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" previous_record 2>/dev/null || echo '{}')"
  [[ -n "${ROLLBACK_REQUEST_ID//[[:space:]]/}" ]] || ROLLBACK_REQUEST_ID="rollback-$(date +%Y%m%d%H%M%S)"
  [[ -n "${ROLLBACK_REQUEST_BRANCH//[[:space:]]/}" ]] || ROLLBACK_REQUEST_BRANCH="main"
  BRANCH="$ROLLBACK_REQUEST_BRANCH"
  if [[ "$ROLLBACK_REQUEST_ACTION" != "rollback" && "$ROLLBACK_REQUEST_ACTION" != "redo" ]]; then
    ROLLBACK_REQUEST_ACTION="rollback"
  fi

  if [[ "$ROLLBACK_REQUEST_ACTION" == "rollback" ]]; then
    # O alvo técnico é o commit atual salvo no controle. `ROLLBACK_UPDATE_TO`
    # é metadado do update original e pode não ser mais o HEAD depois de uma
    # reaplicação; use apenas como fallback para estados antigos.
    ROLLBACK_EXPECTED_HEAD="${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_UPDATE_TO:-}}}"
    ROLLBACK_REVERT_COMMIT="${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_UPDATE_TO:-}}}"
  else
    # Para refazer, revertemos o commit de rollback.
    ROLLBACK_EXPECTED_HEAD="${ROLLBACK_ROLLBACK_COMMIT:-${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_REVERT_COMMIT:-}}}"
    ROLLBACK_REVERT_COMMIT="${ROLLBACK_ROLLBACK_COMMIT:-${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_REDO_COMMIT:-$ROLLBACK_EXPECTED_HEAD}}}"
  fi
  ROLLBACK_EXPECTED_HEAD="$(sanitize_commit_ref "$ROLLBACK_EXPECTED_HEAD")"
  ROLLBACK_REVERT_COMMIT="$(sanitize_commit_ref "$ROLLBACK_REVERT_COMMIT")"

  # Retorna sucesso sempre que existe um request ativo. Campos inválidos são
  # tratados por prepare_rollback_request_update, que arquiva o request e edita
  # a mensagem em vez de deixar o timer ignorar o estado e repetir forever.
  return 0
}

archive_rollback_request() {
  local status="${1:-done}"
  [[ -n "${ROLLBACK_REQUEST_FILE:-}" ]] || return 0
  mkdir -p "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  chown ubuntu:ubuntu "$ROLLBACK_REQUEST_ROOT" "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  chmod 0775 "$ROLLBACK_REQUEST_ROOT" "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  if [[ -f "$ROLLBACK_REQUEST_FILE" ]]; then
    mv "$ROLLBACK_REQUEST_FILE" "$ROLLBACK_REQUEST_ROOT/$status/${ROLLBACK_REQUEST_ID:-rollback}.$(date +%Y%m%d%H%M%S).json" 2>/dev/null || rm -f "$ROLLBACK_REQUEST_FILE" 2>/dev/null || true
  fi
}

commit_exists() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" ]] || return 1
  sudo -u ubuntu -H git -C "$REPO_DIR" rev-parse --verify "${commit}^{commit}" >/dev/null 2>&1
}

commits_have_same_tree() {
  local left="$(sanitize_commit_ref "${1:-}")"
  local right="$(sanitize_commit_ref "${2:-}")"
  [[ -n "$left" && -n "$right" ]] || return 1
  commit_exists "$left" || return 1
  commit_exists "$right" || return 1
  sudo -u ubuntu -H git -C "$REPO_DIR" diff --quiet "$left" "$right" --
}

rollback_desired_tree_commit() {
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    sanitize_commit_ref "${ROLLBACK_UPDATE_TO:-}"
  else
    sanitize_commit_ref "${ROLLBACK_UPDATE_FROM:-}"
  fi
}

rollback_control_json() {
  local mode="${1:-rollback}"
  local head_commit="$(sanitize_commit_ref "${2:-}")"
  local revert_commit="$(sanitize_commit_ref "${3:-$head_commit}")"
  local update_from="$(sanitize_commit_ref "${4:-${ROLLBACK_UPDATE_FROM:-}}")"
  local update_to="$(sanitize_commit_ref "${5:-${ROLLBACK_UPDATE_TO:-}}")"
  local rollback_commit="$(sanitize_commit_ref "${6:-${ROLLBACK_ROLLBACK_COMMIT:-}}")"
  local redo_commit="$(sanitize_commit_ref "${7:-${ROLLBACK_REDO_COMMIT:-}}")"
  python3 - "$mode" "$head_commit" "$revert_commit" "$BRANCH" "$ROLLBACK_SOURCE_AUTHOR_ID" "$update_from" "$update_to" "$rollback_commit" "$redo_commit" <<'PYCTRL'
import json, sys
mode, head, revert, branch, author, update_from, update_to, rollback_commit, redo_commit = sys.argv[1:10]
payload = {
    "enabled": True,
    "mode": mode,
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": revert or head,
    "head_commit": head,
    "source_author_id": author,
}
for key, value in {
    "update_from": update_from,
    "update_to": update_to,
    "rollback_commit": rollback_commit,
    "redo_commit": redo_commit,
}.items():
    if value:
        payload[key] = value
print(json.dumps(payload, ensure_ascii=False))
PYCTRL
}

prepare_rollback_request_update() {
  ROLLBACK_CONTROL_MODE=1
  LOCAL_CANDIDATE_MODE=0
  if [[ -z "${ROLLBACK_EXPECTED_HEAD//[[:space:]]/}" || -z "${ROLLBACK_REVERT_COMMIT//[[:space:]]/}" ]]; then
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "Não encontrei o commit de destino. Nenhuma alteração foi aplicada." || true
    logger -t "$LOG_TAG" "rollback/redo inválido: action=$ROLLBACK_REQUEST_ACTION expected=${ROLLBACK_EXPECTED_HEAD:-vazio} revert=${ROLLBACK_REVERT_COMMIT:-vazio} update_from=${ROLLBACK_UPDATE_FROM:-vazio} update_to=${ROLLBACK_UPDATE_TO:-vazio}"
    archive_rollback_request "failed"
    exit 0
  fi
  zip_progress_publish "Validando estado atual"
  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  mark_update_timing "fetch"

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" || "$CURRENT_COMMIT" != "$ROLLBACK_EXPECTED_HEAD" ]]; then
    local unavailable_title="Reversão indisponível"
    local desired_commit noop_title
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && unavailable_title="Reaplicação indisponível"
    desired_commit="$(rollback_desired_tree_commit)"
    if [[ -n "$desired_commit" ]] && commits_have_same_tree "$CURRENT_COMMIT" "$desired_commit"; then
      noop_title="Nenhuma alteração necessária"
      post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "warn" "$noop_title" "O estado atual já corresponde ao resultado esperado. Nada foi alterado." || true
      archive_rollback_request "done"
      exit 0
    fi
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$unavailable_title" "O estado atual mudou. Nenhuma alteração foi aplicada." || true
    archive_rollback_request "failed"
    exit 0
  fi

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  zip_progress_done_and_publish "Estado validado" "Aplicando reversão"

  STAGE="reversão local"
  if ! sudo -u ubuntu -H git -C "$REPO_DIR" rev-parse --verify "${ROLLBACK_REVERT_COMMIT}^{commit}" >/dev/null 2>&1; then
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    logger -t "$LOG_TAG" "commit de rollback/redo não encontrado: action=$ROLLBACK_REQUEST_ACTION expected=$ROLLBACK_EXPECTED_HEAD revert=$ROLLBACK_REVERT_COMMIT current=$CURRENT_COMMIT remote=$REMOTE_COMMIT"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "Não encontrei o commit de destino. Nenhuma alteração foi aplicada." "$retry_control" || true
    archive_rollback_request "failed"
    exit 0
  fi
  if ! sudo -u ubuntu -H git revert --no-commit "$ROLLBACK_REVERT_COMMIT"; then
    sudo -u ubuntu -H git revert --abort >/dev/null 2>&1 || true
    sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi restaurado. Nada foi publicado no GitHub." "$retry_control" || true
    archive_rollback_request "failed"
    exit 0
  fi
  UPDATE_APPLIED=1
  if ! CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --cached --name-only)"; then
    LAST_ERROR_STDERR="falha ao listar arquivos staged da reversão"
    return 1
  fi
  if ! CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --cached --numstat)"; then
    LAST_ERROR_STDERR="falha ao calcular o diff staged da reversão"
    return 1
  fi
  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "warn" "Nenhuma alteração" "O estado já estava equivalente." "$retry_control" || true
    archive_rollback_request "done"
    exit 0
  fi
  classify_changed_files
  COMMIT_SUBJECT="${ROLLBACK_REQUEST_ACTION} discord zip update"
  mark_update_timing "rollback_apply"
  zip_progress_done "Reversão preparada"
}

publish_rollback_request_after_validation() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 0
  STAGE="commit da reversão"
  local msg
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    msg="redo discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  else
    msg="rollback discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  fi
  zip_progress_publish "Fazendo commit..."
  git_add_changed_files
  sudo -u ubuntu -H git commit -m "$msg"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  ROLLBACK_NEW_COMMIT="$REMOTE_COMMIT"
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    ROLLBACK_REDO_COMMIT="$REMOTE_COMMIT"
  else
    ROLLBACK_ROLLBACK_COMMIT="$REMOTE_COMMIT"
  fi
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  mark_update_timing "commit"
  zip_progress_done "Commit criado"
  STAGE="push GitHub"
  zip_progress_publish "Publicando no GitHub..."
  sudo -u ubuntu -H git push origin "HEAD:$BRANCH"
  # O commit de reversão/reaplicação já está remoto; a notificação posterior
  # não pode transformar isso em rollback automático do rollback.
  mark_deployment_committed
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}

finalize_rollback_request_success() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 1
  local duration changed_files diff_summary apply_mode control_json title summary next_mode status_title file_count file_label altered_label
  duration="$(human_duration "$SECONDS")"
  changed_files="$(format_changed_files)"
  diff_summary="$(format_diff_total_summary)"
  file_count="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
  file_label="$(format_update_file_count "$file_count")"
  if (( file_count == 1 )); then altered_label="alterado"; else altered_label="alterados"; fi
  if [[ "$FAST_RELOAD_STATUS" == OK* ]]; then
    apply_mode="recarga controlada de cog"
  elif (( BOT_CHANGED == 0 )); then
    apply_mode="sem reinício do bot"
  else
    apply_mode="reinício completo"
  fi
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    title="↪️ Atualização reaplicada"
    summary="Atualização reaplicada e estabilidade confirmada."
    next_mode="rollback"
  else
    title="↩️ Atualização revertida"
    summary="Reversão aplicada e estabilidade confirmada."
    next_mode="redo"
  fi
  control_json="$(rollback_control_json "$next_mode" "$REMOTE_COMMIT" "$REMOTE_COMMIT" "$ROLLBACK_UPDATE_FROM" "$ROLLBACK_UPDATE_TO" "$ROLLBACK_ROLLBACK_COMMIT" "$ROLLBACK_REDO_COMMIT")"
  local desc
  desc="$summary

${SHORT_FROM} → ${SHORT_TO}
$file_label $altered_label · $diff_summary
Aplicação: $apply_mode
Tempo total: $duration"
  post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "success" "$title" "$desc" "$control_json" || true
  local body
  body="Resumo: $summary
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: $file_label · $diff_summary
Aplicação: $apply_mode
Processos alterados: $(format_changed_processes)
Arquivos:
$changed_files
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  send_alert_reliably success "$title" "$body" "" "" "rollback-${ROLLBACK_REQUEST_ID:-unknown}-${SHORT_TO:-unknown}" || true
  archive_rollback_request "done"
  logger -t "$LOG_TAG" "$title"
  exit 0
}

apply_local_candidate_patch_diff() {
  [[ -f "${LOCAL_CANDIDATE_PATCH_FILE:-}" ]] || return 1
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-apply.XXXXXX")"
  if sudo -u ubuntu -H git apply --3way --index "$LOCAL_CANDIDATE_PATCH_FILE" 2>"$errfile"; then
    rm -f "$errfile" 2>/dev/null || true
    normalize_changed_file_permissions "patch 3-way do candidato"
    return 0
  fi
  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true
  return 1
}

copy_local_candidate_files() {
  [[ -d "$LOCAL_CANDIDATE_FILES_DIR" ]] || return 1
  MANIFEST_PATH="$LOCAL_CANDIDATE_DIR/manifest.json" REPO_DIR="$REPO_DIR" FILES_DIR="$LOCAL_CANDIDATE_FILES_DIR" python3 - <<'PYCOPY'
import json, os, pathlib, shutil
repo = pathlib.Path(os.environ['REPO_DIR']).resolve()
files_dir = pathlib.Path(os.environ['FILES_DIR']).resolve()
data = json.loads(pathlib.Path(os.environ['MANIFEST_PATH']).read_text(encoding='utf-8'))
for raw in data.get('changed_files') or []:
    rel = pathlib.PurePosixPath(str(raw))
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido no candidato: {raw}')
    src = files_dir.joinpath(*rel.parts).resolve()
    dst = repo.joinpath(*rel.parts).resolve()
    if files_dir not in src.parents and src != files_dir:
        raise SystemExit(f'origem fora do candidato: {raw}')
    if repo not in dst.parents and dst != repo:
        raise SystemExit(f'destino fora do repo: {raw}')
    if not src.is_file():
        raise SystemExit(f'arquivo ausente no candidato: {raw}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
PYCOPY
  normalize_changed_file_permissions "arquivos copiados do candidato"
}

normalize_changed_file_permissions() {
  local context="${1:-permissões do candidato}"
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYPERM'
import os, pathlib, pwd, grp, stat
repo = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')).resolve()
raw_items = os.environ.get('CHANGED_FILES_RAW', '').splitlines()
try:
    uid = pwd.getpwnam('ubuntu').pw_uid
    gid = grp.getgrnam('ubuntu').gr_gid
except KeyError:
    raise SystemExit(0)

def inside_repo(path: pathlib.Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        return False
    return resolved == repo or repo in resolved.parents

def apply_owner_mode(path: pathlib.Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except Exception:
        pass
    try:
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISDIR(st.st_mode):
            os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        elif stat.S_ISREG(st.st_mode):
            os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

for raw in raw_items:
    raw = raw.strip()
    if not raw:
        continue
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido para permissões: {raw}')
    dst = repo.joinpath(*rel.parts)
    cur = repo
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            if not inside_repo(cur):
                raise SystemExit(f'parent fora do repo: {raw}')
            apply_owner_mode(cur)
    if dst.exists() or dst.is_symlink():
        if not inside_repo(dst):
            raise SystemExit(f'path fora do repo: {raw}')
        apply_owner_mode(dst)
PYPERM
  logger -t "$LOG_TAG" "permissões normalizadas para paths do candidato: $context" 2>/dev/null || true
}

prune_empty_candidate_dirs_after_reset() {
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYPRUNE'
import os, pathlib
repo = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')).resolve()
parents = []
for raw in os.environ.get('CHANGED_FILES_RAW', '').splitlines():
    raw = raw.strip()
    if not raw:
        continue
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or '..' in rel.parts:
        continue
    path = repo.joinpath(*rel.parts)
    parent = path.parent
    while parent != repo:
        try:
            resolved = parent.resolve(strict=False)
        except Exception:
            break
        if not (resolved == repo or repo in resolved.parents):
            break
        parents.append(parent)
        parent = parent.parent
for parent in sorted(set(parents), key=lambda p: len(p.parts), reverse=True):
    try:
        parent.rmdir()
    except OSError:
        pass
PYPRUNE
}

cleanup_local_candidate_new_files_after_reset() {
  if (( LOCAL_CANDIDATE_MODE == 0 )) || [[ -z "${PREVIOUS_COMMIT:-}" ]]; then
    return 0
  fi
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    if printf '%s
' "$rel" | grep -Eq '(^|/)\.\.(\/|$)|^/'; then
      continue
    fi
    if sudo -u ubuntu -H git cat-file -e "$PREVIOUS_COMMIT:$rel" 2>/dev/null; then
      continue
    fi
    sudo rm -rf -- "$REPO_DIR/$rel" 2>/dev/null || rm -rf -- "$REPO_DIR/$rel" 2>/dev/null || true
  done <<< "$CHANGED_FILES_RAW"
  prune_empty_candidate_dirs_after_reset || true
}

git_add_changed_files() {
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0

  # Stage seguro para criação, alteração e remoção. Um `git add path` direto
  # pode falhar com "pathspec did not match" quando o path não existe no
  # worktree; `git add -A` stageia deleções, mas ainda falha para paths que
  # nunca foram rastreados. Por isso só passamos paths existentes/symlinks ou
  # paths que o Git já conhece.
  local pathspec_file rel target tracked_any rc
  pathspec_file="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-pathspec.XXXXXX")"
  tracked_any=0

  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    if printf '%s\n' "$rel" | grep -Eq '(^|/)\.\.(\/|$)|^/'; then
      rm -f "$pathspec_file" 2>/dev/null || true
      LAST_ERROR_STDERR="path inválido para git add: $rel"
      return 1
    fi

    target="$REPO_DIR/$rel"
    if [[ -e "$target" || -L "$target" ]]; then
      printf '%s\0' "$rel" >> "$pathspec_file"
      tracked_any=1
    elif sudo -u ubuntu -H git ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
      # Arquivo removido pelo patch: stageia a deleção com `git add -A`.
      printf '%s\0' "$rel" >> "$pathspec_file"
      tracked_any=1
    else
      logger -t "$LOG_TAG" "ignorando path ausente/não rastreado no stage: $rel" 2>/dev/null || true
    fi
  done <<< "$CHANGED_FILES_RAW"

  if (( tracked_any == 0 )) || [[ ! -s "$pathspec_file" ]]; then
    rm -f "$pathspec_file" 2>/dev/null || true
    return 0
  fi

  # mktemp roda no usuário do updater (normalmente root), enquanto o Git roda
  # como ubuntu. Torne o pathspec legível antes de entregá-lo ao git add.
  chown ubuntu:ubuntu "$pathspec_file" 2>/dev/null || true
  chmod 0644 "$pathspec_file" 2>/dev/null || true
  sudo -u ubuntu -H git add -A --pathspec-from-file="$pathspec_file" --pathspec-file-nul
  rc=$?
  rm -f "$pathspec_file" 2>/dev/null || true
  return "$rc"
}

prepare_local_candidate_update() {
  LOCAL_CANDIDATE_MODE=1
  zip_progress_publish "Conferindo ZIP" "Checando arquivo recebido e base local."
  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  COMMIT_SUBJECT="$LOCAL_CANDIDATE_COMMIT_MESSAGE"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="local"
  mark_update_timing "fetch"
  zip_progress_done_and_publish "Base conferida" "Validando integridade do pacote"

  local max_attempts="${DISCORD_AUTO_UPDATE_MAX_ATTEMPTS:-3}"
  [[ "$max_attempts" =~ ^[0-9]+$ ]] || max_attempts=3
  (( max_attempts < 1 )) && max_attempts=1
  if [[ "${LOCAL_CANDIDATE_ATTEMPT:-0}" =~ ^[0-9]+$ ]] && (( LOCAL_CANDIDATE_ATTEMPT > max_attempts )); then
    reject_local_candidate_safely       "Atualização arquivada"       "O pacote excedeu o limite de tentativas automáticas e não foi aplicado."       "tentativa ${LOCAL_CANDIDATE_ATTEMPT}/${max_attempts}; reenvie o patch após revisar a falha anterior"
  fi

  STAGE="validação de integridade do candidato"
  if ! verify_local_candidate_integrity; then
    reject_local_candidate_safely \
      "Atualização bloqueada" \
      "O pacote não passou pela verificação de integridade ou expirou. Nenhuma alteração foi aplicada." \
      "$LOCAL_CANDIDATE_VERIFY_ERROR"
  fi
  update_local_candidate_heartbeat "validated"
  zip_progress_done_and_publish "Integridade confirmada" "Analisando segurança"

  STAGE="validação de segurança do ZIP"
  local suspicion_reason=""
  suspicion_reason="$(local_candidate_suspicion_reason 2>/dev/null || true)"
  if [[ -n "${suspicion_reason//[[:space:]]/}" ]]; then
    reject_local_candidate_safely \
      "Atualização bloqueada" \
      "Esse arquivo parece uma base completa ou contém caminhos suspeitos. Nenhuma alteração foi aplicada." \
      "$suspicion_reason"
  fi
  zip_progress_done_and_publish "Segurança confirmada" "Validando estado local"

  if [[ -n "$LOCAL_CANDIDATE_BASE_COMMIT" && "$LOCAL_CANDIDATE_BASE_COMMIT" != "$REMOTE_COMMIT" ]]; then
    if [[ -f "${LOCAL_CANDIDATE_PATCH_FILE:-}" ]]; then
      LOCAL_CANDIDATE_USE_PATCH=1
      logger -t "$LOG_TAG" "Candidato $LOCAL_CANDIDATE_ID preparado sobre $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT"); tentará rebase 3-way sobre $(short_commit "$REMOTE_COMMIT")."
    else
      base_conflict_reason="$(local_candidate_base_conflict_reason 2>/dev/null || true)"
      if [[ -n "${base_conflict_reason//[[:space:]]/}" ]]; then
        MANUAL_FAILURE_ALERT_SENT=1
        notify_zip_status_message "error" "Atualização com conflito" "Esta atualização ficou incompatível com outra aplicada antes dela. Nada foi aplicado neste item; os próximos permanecem na fila." || true
        archive_local_candidate "failed"
        send_error "Update com conflito na fila" "Resumo: O ZIP foi preparado sobre outro commit e conflitou com mudanças já aplicadas. Nada foi aplicado e nada foi enviado ao GitHub para este item.
Branch: $BRANCH
Base do ZIP: $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT")
GitHub atual: $(short_commit "$REMOTE_COMMIT")
Motivo: $base_conflict_reason
Candidato: ${LOCAL_CANDIDATE_ID:-desconhecido}
ZIP: ${LOCAL_CANDIDATE_ZIP_NAME:-desconhecido}
Arquivos:
$(format_changed_files)
Ação sugerida: gere esse patch novamente usando a base atual.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
        trigger_updater_if_queue_pending
        exit 0
      fi
      logger -t "$LOG_TAG" "Candidato $LOCAL_CANDIDATE_ID preparado sobre $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT"), aplicando sobre $(short_commit "$REMOTE_COMMIT") sem conflito de arquivos."
    fi
  fi

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  if candidate_local_changes_are_expected; then
    logger -t "$LOG_TAG" "Alterações locais correspondem ao candidato ativo; retomando aplicação segura."
  else
    local candidate_dirty_rc=$?
    if (( candidate_dirty_rc == 2 )); then
      LAST_ERROR_STDERR="não foi possível conferir as alterações locais do candidato como usuário ubuntu"
      return 1
    fi
    fail_local_changes_before_pull
  fi

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" ]]; then
    STAGE="sincronização com GitHub antes do candidato"
    sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"
    CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
    PREVIOUS_COMMIT="$CURRENT_COMMIT"
    SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
    mark_update_timing "sync"
  fi
  zip_progress_done_and_publish "Estado local validado" "Preparando arquivos"

  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    notify_zip_status_message "success" "Nenhuma alteração necessária" "O pacote já corresponde ao estado atual da VPS. Nenhum arquivo foi modificado." || true
    archive_local_candidate "done"
    logger -t "$LOG_TAG" "Candidato local sem arquivos alterados"
    trigger_updater_if_queue_pending
    exit 0
  fi

  classify_changed_files

  STAGE="limpeza de artefatos gerados"
  cleanup_known_generated_update_artifacts

  zip_progress_done_and_publish "ZIP conferido" "Aplicando na VPS"

  STAGE="aplicação local do candidato"
  UPDATE_APPLIED=1
  if (( LOCAL_CANDIDATE_USE_PATCH == 1 )); then
    if ! apply_local_candidate_patch_diff; then
      MANUAL_FAILURE_ALERT_SENT=1
      normalize_changed_file_permissions "falha no patch 3-way" || true
      sudo -u ubuntu -H git reset --hard "${PREVIOUS_COMMIT:-HEAD}" >/dev/null 2>&1 || true
      cleanup_local_candidate_new_files_after_reset
      notify_zip_status_message "error" "Atualização com conflito" "Esta atualização não pôde ser mesclada com as anteriores. Nada foi aplicado neste item; os próximos permanecem na fila." || true
      archive_local_candidate "failed"
      send_error "Update com conflito na fila" "Resumo: O updater tentou mesclar esse ZIP sobre a base atual usando 3-way, mas encontrou conflito. Nada foi aplicado e nada foi enviado ao GitHub para este item.
Branch: $BRANCH
Base do ZIP: $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT")
GitHub atual: $(short_commit "$REMOTE_COMMIT")
Candidato: ${LOCAL_CANDIDATE_ID:-desconhecido}
ZIP: ${LOCAL_CANDIDATE_ZIP_NAME:-desconhecido}
Erro:
${LAST_ERROR_STDERR:-git apply falhou}
Arquivos:
$(format_changed_files)
Ação sugerida: gere esse patch novamente usando a base atual.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
      trigger_updater_if_queue_pending
      exit 0
    fi
  else
    copy_local_candidate_files
    git_add_changed_files_or_reject "git add do candidato local"
  fi
  if ! CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --cached --name-only)"; then
    LAST_ERROR_STDERR="falha ao listar arquivos staged do candidato"
    return 1
  fi
  if ! CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --cached --numstat)"; then
    LAST_ERROR_STDERR="falha ao calcular o diff staged do candidato"
    return 1
  fi
  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    local head_message=""
    head_message="$(sudo -u ubuntu -H git log -1 --pretty=%B HEAD 2>/dev/null || true)"
    if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]] && printf '%s
' "$head_message" | grep -Fqx "Candidate-ID: $LOCAL_CANDIDATE_ID"; then
      LOCAL_CANDIDATE_PUBLISHED=1
      LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=1
      REMOTE_COMMIT="$CURRENT_COMMIT"
      SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
      mark_deployment_committed
      logger -t "$LOG_TAG" "Candidato local já publicado e validado; retomando somente a entrega final, sem novo restart." 2>/dev/null || true
      return 0
    fi
    notify_zip_status_message "success" "Nenhuma alteração necessária" "O pacote já corresponde ao estado atual da VPS. Nenhum arquivo foi modificado." || true
    archive_local_candidate "done"
    logger -t "$LOG_TAG" "Candidato local não mudou o repositório"
    trigger_updater_if_queue_pending
    exit 0
  fi
  classify_changed_files
  mark_update_timing "candidate_apply"
  zip_progress_done "Aplicado na VPS"
}

publish_local_candidate_after_validation() {
  if (( LOCAL_CANDIDATE_MODE == 0 )); then
    return 0
  fi
  if (( LOCAL_CANDIDATE_PUBLISHED == 1 )); then
    return 0
  fi
  STAGE="commit local validado"
  zip_progress_publish "Fazendo commit..."
  git_add_changed_files_or_reject "git add antes do commit"
  local commit_body
  commit_body="Candidate-ID: $LOCAL_CANDIDATE_ID
Update-ID: $LOCAL_CANDIDATE_DISPLAY_ID
Discord-Author-ID: ${LOCAL_CANDIDATE_SOURCE_AUTHOR_ID:-desconhecido}
Source-ZIP-SHA256: ${LOCAL_CANDIDATE_ZIP_SHA256:-indisponível}"
  sudo -u ubuntu -H git commit -m "$LOCAL_CANDIDATE_COMMIT_MESSAGE" -m "$commit_body"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  mark_update_timing "commit"
  zip_progress_done "Commit criado"

  write_local_candidate_state "committed" "$REMOTE_COMMIT"

  STAGE="push GitHub pós-validação"
  zip_progress_publish "Publicando no GitHub..."
  sudo -u ubuntu -H git push origin "HEAD:$BRANCH"
  LOCAL_CANDIDATE_PUBLISHED=1
  # A partir daqui o remoto já contém o commit validado. Qualquer falha
  # subsequente é de finalização e não pode resetar somente a VPS.
  mark_deployment_committed
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}

format_changed_files() {
  if [[ -n "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
lines = []
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem, path = parts[0], parts[1], parts[-1]
    if add == "-" or rem == "-":
        lines.append(f"• {path}  binário")
    else:
        lines.append(f"• {path}  +{int(add or 0)} -{int(rem or 0)}")
limit = 20
for line in lines[:limit]:
    print(line)
if len(lines) > limit:
    remaining = len(lines) - limit
    label = 'arquivo restante' if remaining == 1 else 'arquivos restantes'
    print(f"+{remaining} {label}")
if not lines:
    print("• nenhum arquivo listado")
PYDIFF
  elif [[ -n "$CHANGED_FILES_RAW" ]]; then
    printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/• /'
    local total
    total="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
    if [[ "$total" =~ ^[0-9]+$ && "$total" -gt 20 ]]; then
      local remaining=$((total - 20))
      if (( remaining == 1 )); then
        printf '+1 arquivo restante\n'
      else
        printf '+%s arquivos restantes\n' "$remaining"
      fi
    fi
  else
    printf '• nenhum arquivo listado'
  fi
}

format_diff_total_summary() {
  if [[ -z "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    printf 'diff indisponível'
    return 0
  fi
  CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
added = removed = binaries = 0
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem = parts[0], parts[1]
    if add == "-" or rem == "-":
        binaries += 1
        continue
    added += int(add or 0)
    removed += int(rem or 0)
out = f"+{added} -{removed}"
if binaries:
    out += f" · {binaries} " + ('binário' if binaries == 1 else 'binários')
print(out)
PYDIFF
}

classify_changed_files() {
  FRONT_CHANGED=0
  BACK_CHANGED=0
  BOT_CHANGED=0
  REQUIREMENTS_CHANGED=0
  AUDIO_SYSTEMD_CHANGED=0
  CLEANUP_CHANGED=0
  PHONE_LAVALINK_WATCH_CHANGED=0
  PHONE_WORKER_WATCH_CHANGED=0
  VPS_SYSTEMD_UNITS_CHANGED=0
  ALERT_CHANGED=0
  PHONE_WORKER_SYNC_REQUIRED=0
  CORE_WORKER_APK_CHANGED=0
  CORE_WORKER_AUTOMATION_REQUIRED=0
  APP_COMMANDS_MAY_HAVE_CHANGED=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(bot\.py|cogs/.*\.py|cogs/.*/.*\.py|utility/commands/.*\.py)$'; then
    APP_COMMANDS_MAY_HAVE_CHANGED=1
  fi

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity/sinuca/'; then
    FRONT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity/sinuca-server/'; then
    BACK_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(bot\.py|webserver\.py|config\.py|db\.py|start\.sh|requirements\.txt|cogs/|music_system/|utility/)'; then
    BOT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/systemd(/vps)?/tts-bot\.service$'; then
    BOT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^requirements\.txt$'; then
    REQUIREMENTS_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/systemd/(lavalink|tts-bot|tts-bot-alert@)\.service$'; then
    AUDIO_SYSTEMD_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(alert\.sh|deploy/systemd(/vps)?/tts-bot-alert@\.service)$'; then
    ALERT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(deploy/systemd/(vps/)?(tts-bot\.service|tts-bot-updater\.(service|timer)|tts-bot-alert@\.service|cleanup-audio-temp\.(service|timer)|sinuca-activity-server\.service|phone-worker-watch\.(service|timer)|tts-bot\.service\.d/)|deploy/(sudoers\.d|journald|tmpfiles\.d)/|scripts/install-vps-systemd-units\.sh$)'; then
    # O instalador sincroniza as units gerenciadas em uma única passagem. Inclua
    # também os templates na raiz deploy/systemd; ignorá-los deixava a VPS usando
    # uma unit antiga mesmo depois do commit ter sido aplicado.
    VPS_SYSTEMD_UNITS_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(cleanup-audio-temp\.sh|deploy/systemd/cleanup-audio-temp\.(service|timer))$'; then
    CLEANUP_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-lavalink-watch\.sh|deploy/systemd/phone-lavalink-watch\.(service|timer)|deploy/termux/phone-lavalink/)'; then
    PHONE_LAVALINK_WATCH_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-worker-watch\.sh|scripts/phone-worker-client\.py|deploy/systemd/phone-worker-watch\.(service|timer)|deploy/termux/phone-worker/)'; then
    PHONE_WORKER_WATCH_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/termux/phone-worker/'; then
    PHONE_WORKER_SYNC_REQUIRED=1
    CORE_WORKER_AUTOMATION_REQUIRED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^android/core-worker-app/'; then
    CORE_WORKER_APK_CHANGED=1
    CORE_WORKER_AUTOMATION_REQUIRED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/core-worker-automation\.py|utility/commands/workers_registry\.py|webserver\.py)$'; then
    # Mudanças no próprio orquestrador/registry também precisam reavaliar as
    # pendências; antes elas só entravam em vigor no próximo patch Android/Termux.
    CORE_WORKER_AUTOMATION_REQUIRED=1
  fi
}

fast_reload_modules_for_changed_files() {
  CHANGED_FILES_RAW_INPUT="$CHANGED_FILES_RAW" \
  HOT_RELOAD_ALLOW="${DISCORD_AUTO_UPDATE_HOT_RELOAD_ALLOW:-}" \
  HOT_RELOAD_DENY="${DISCORD_AUTO_UPDATE_HOT_RELOAD_DENY:-music,dashboard_sync,terminal_cmd}" \
  python3 - <<'PYFAST'
import os, pathlib, re
raw = [line.strip() for line in (os.environ.get("CHANGED_FILES_RAW_INPUT") or "").splitlines() if line.strip()]
if not raw:
    raise SystemExit(1)

def names(value):
    return {part.strip().removeprefix("cogs.").removesuffix(".py") for part in re.split(r"[,;\s]+", value or "") if part.strip()}

allow = names(os.environ.get("HOT_RELOAD_ALLOW") or "")
deny = names(os.environ.get("HOT_RELOAD_DENY") or "") | {"__init__"}
modules = []
for path in raw:
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) != 2 or parts[0] != "cogs" or not parts[1].endswith(".py"):
        raise SystemExit(1)
    name = parts[1][:-3]
    if name in deny or (allow and name not in allow):
        raise SystemExit(1)
    modules.append("cogs." + name)
print("\n".join(dict.fromkeys(modules)))
PYFAST
}

try_fast_cog_reload() {
  local modules_text="${1:-}"
  local check_app_commands="${2:-0}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local payload token header_args=() response http_code verification_epoch restarts_before
  verification_epoch="$(date +%s)"
  restarts_before="$(service_restart_count "$SERVICE")"
  payload="$(MODULES_TEXT="$modules_text" CHECK_APP_COMMANDS="$check_app_commands" python3 - <<'PYPAYLOAD'
import json, os
mods = [line.strip() for line in (os.environ.get("MODULES_TEXT") or "").splitlines() if line.strip()]
check = str(os.environ.get("CHECK_APP_COMMANDS") or "").strip().lower() in {"1", "true", "yes", "sim", "on"}
print(json.dumps({"modules": mods, "check_app_commands": check}, ensure_ascii=False))
PYPAYLOAD
)"
  token=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    token="$(grep -E '^BOT_INTERNAL_UPDATE_TOKEN=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  if [[ -n "$token" ]]; then
    header_args=(-H "X-Update-Token: $token")
  fi
  response="$(mktemp)"
  http_code="$(curl -sS -o "$response" -w '%{http_code}' --max-time 90 -H 'Content-Type: application/json' "${header_args[@]}" -d "$payload" http://127.0.0.1:10000/internal/update/reload-cogs 2>/dev/null || true)"
  if [[ "$http_code" != "200" ]]; then
    FAST_RELOAD_STATUS="falhou; fallback restart (${http_code:-sem HTTP})"
    logger -t "$LOG_TAG" "Fast reload falhou HTTP=${http_code:-sem HTTP}: $(cat "$response" 2>/dev/null | tail -c 300)"
    rm -f "$response"
    return 1
  fi
  if ! python3 - "$response" <<'PYOK' >/dev/null 2>&1; then
import json, sys
p = sys.argv[1]
data = json.load(open(p, encoding='utf-8'))
raise SystemExit(0 if data.get('ok') is True else 1)
PYOK
    FAST_RELOAD_STATUS="falhou; fallback restart"
    logger -t "$LOG_TAG" "Fast reload retornou falha: $(cat "$response" 2>/dev/null | tail -c 500)"
    rm -f "$response"
    return 1
  fi
  rm -f "$response"
  STAGE="estabilidade após reload rápido"
  if verify_bot_after_restart "$verification_epoch" "$restarts_before" 0; then
    FAST_RELOAD_STATUS="OK; estabilidade confirmada"
    return 0
  fi
  FAST_RELOAD_STATUS="reload executado; estabilidade falhou; fallback restart"
  return 1
}

cleanup_known_generated_update_artifacts() {
  # Estes arquivos/pastas são gerados por build/publicação do Core Worker e
  # não devem bloquear o auto updater. Não remove código fonte nem registry.
  rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || true
  rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || true
  rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || true
  # Não removemos android/core-worker-app/releases aqui: é onde latest.json/APKs
  # privados ficam publicados para os celulares. O auto updater já ignora essa
  # pasta ao criar commits e alterações não rastreadas não bloqueiam git pull.
}

local_changes_fingerprint() {
  {
    sudo -u ubuntu -H git status --short --untracked-files=no 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only --cached 2>/dev/null || true
  } | sha256sum | awk '{print $1}'
}

clear_local_changes_marker_if_clean() {
  local status_text
  status_text="$(collect_local_tracked_changes)"
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    rm -f "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true
  fi
}

collect_local_tracked_changes() {
  # Untracked locais como data/, cookies e healthcheck não bloqueiam o merge.
  # O que bloqueia o git pull são mudanças em arquivos rastreados.
  local status_text
  status_text="$(sudo -u ubuntu -H git status --short --untracked-files=no 2>/dev/null || true)"
  printf '%s' "$status_text" | trim_alert_text 1800
}

collect_local_tracked_files() {
  # Evita `head` em pipeline com pipefail: se houver muitos arquivos, o produtor
  # pode receber SIGPIPE e virar erro 141. A deduplicação/limite fica no Python.
  {
    sudo -u ubuntu -H git diff --name-only 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only --cached 2>/dev/null || true
  } | python3 -c 'import sys
seen = set()
rows = []
for raw in sys.stdin:
    item = raw.strip()
    if not item or item in seen:
        continue
    seen.add(item)
    if len(rows) < 40:
        rows.append("• " + item)
text = "\n".join(rows).strip()
limit = 1500
if text and len(text) > limit:
    text = text[: limit - 1].rstrip() + "…"
if text:
    sys.stdout.write(text + "\n")
' || true
}

candidate_local_changes_are_expected() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 1
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYCANDIDATE_DIRTY'
import os
import subprocess
import sys

expected = {
    line.strip()
    for line in os.environ.get("CHANGED_FILES_RAW", "").splitlines()
    if line.strip()
}
if not expected:
    raise SystemExit(1)

repo = os.environ.get("REPO_DIR", "/home/ubuntu/bot")

def git_lines(*args: str) -> set[str]:
    cp = subprocess.run(
        ["sudo", "-u", "ubuntu", "-H", "git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if cp.returncode != 0:
        message = (cp.stderr or "git falhou sem stderr").strip()
        print(message, file=sys.stderr)
        raise SystemExit(2)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}

dirty = set()
dirty |= git_lines("diff", "--name-only")
dirty |= git_lines("diff", "--name-only", "--cached")
if not dirty:
    raise SystemExit(0)

extra = dirty - expected
if extra:
    for item in sorted(extra):
        print(item)
    raise SystemExit(1)
raise SystemExit(0)
PYCANDIDATE_DIRTY
}

ensure_no_unstaged_tracked_changes() {
  local dirty
  if ! dirty="$(sudo -u ubuntu -H git diff --name-only 2>/dev/null)"; then
    LAST_ERROR_STDERR="não foi possível verificar alterações rastreadas após o build"
    return 1
  fi
  if [[ -z "${dirty//[[:space:]]/}" ]]; then
    return 0
  fi

  LAST_ERROR_STDERR="arquivos rastreados foram alterados durante build/deploy:
$(printf '%s\n' "$dirty" | trim_alert_text 1500)"
  logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
  return 1
}


fail_local_changes_before_pull() {
  local status_text files_text duration body
  status_text="$(collect_local_tracked_changes)"
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    return 0
  fi

  local fingerprint previous_fingerprint
  fingerprint="$(local_changes_fingerprint)"
  previous_fingerprint=""
  if [[ -f "$LOCAL_CHANGES_MARKER_FILE" ]]; then
    previous_fingerprint="$(awk -F= '$1 == "FINGERPRINT" { sub($1 "=", ""); print; exit }' "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true)"
  fi
  if [[ -n "$fingerprint" && "$fingerprint" == "$previous_fingerprint" ]]; then
    logger -t "$LOG_TAG" "Alterações locais ainda bloqueiam o update; alerta já enviado para este mesmo estado."
    exit 1
  fi
  MANUAL_FAILURE_ALERT_SENT=1
  files_text="$(collect_local_tracked_files)"
  duration="$(human_duration "$SECONDS")"
  cat > "$LOCAL_CHANGES_MARKER_FILE" <<EOM
FINGERPRINT=$fingerprint
REMOTE_COMMIT=$REMOTE_COMMIT
CURRENT_COMMIT=$CURRENT_COMMIT
AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true

  body="Resumo: O updater parou antes do git pull porque existem alterações locais em arquivos rastreados. O Git bloqueou o merge para não sobrescrever seus testes na VPS.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: $UPDATER_UNIT
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: verificação de alterações locais
Código: 1
Rollback: não foi necessário
Commit sujo: sim
Diagnóstico: existem mudanças locais que seriam sobrescritas pelo merge.
Arquivos locais:
${files_text:-nenhum arquivo listado}
Status git:
$status_text
Ação sugerida: deixe o repo limpo antes do updater. Normalmente: git restore <arquivo> e rm -rf android/core-worker-app/app/build android/core-worker-app/releases. Se a alteração for intencional, envie como patch oficial em ZIP.
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  send_error "Falha na atualização automática: alterações locais" "$body"
  exit 1
}


sanitize_vps_lavalink_units() {
  # Remove dependências locais do Lavalink. O node de áudio válido fica no phone worker/Music Agent.
  local file changed_any=0
  for file in /etc/systemd/system/tts-bot.service /etc/systemd/system/tts-bot.service.d/*.conf; do
    [[ -f "$file" ]] || continue
    if grep -q 'lavalink.service' "$file" 2>/dev/null; then
      cp -a "$file" "$file.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      python3 - "$file" <<'PY_SANITIZE_LAVALINK'
import sys
from pathlib import Path
p=Path(sys.argv[1])
text=p.read_text(encoding='utf-8', errors='replace')
out=[]
for line in text.splitlines():
    stripped=line.strip()
    if stripped.startswith(('Wants=', 'Requires=', 'After=', 'Before=')) and 'lavalink.service' in stripped:
        key, value = line.split('=', 1)
        parts=[part for part in value.split() if part != 'lavalink.service']
        if parts:
            out.append(key + '=' + ' '.join(parts))
        else:
            out.append('# ' + key + '=lavalink.service removido: Lavalink roda no phone worker/Music Agent')
        continue
    if stripped.startswith('ExecStartPre=') and 'wait-audio-node-ready.py' in stripped:
        out.append('# ExecStartPre wait-audio-node-ready.py removido: Lavalink local da VPS não é usado')
        continue
    out.append(line)
p.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_SANITIZE_LAVALINK
      changed_any=1
    fi
  done
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    systemctl stop lavalink.service >/dev/null 2>&1 || true
    systemctl disable lavalink.service >/dev/null 2>&1 || true
    systemctl reset-failed lavalink.service >/dev/null 2>&1 || true
    # systemctl mask falha quando /etc/systemd/system/lavalink.service é um arquivo
    # real. Fazemos a máscara idempotente manualmente para impedir restart-loop local.
    if [[ -e /etc/systemd/system/lavalink.service && ! -L /etc/systemd/system/lavalink.service ]]; then
      cp -a /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      mv /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.disabled.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    fi
    ln -sfn /dev/null /etc/systemd/system/lavalink.service 2>/dev/null || true
    changed_any=1
  fi
  if (( changed_any == 1 )); then
    systemctl daemon-reload || true
  fi
}


normalize_healthcheck_crontab() {
  # Corrige linhas temporárias quebradas criadas durante diagnóstico manual.
  # Não reativa healthcheck/resource-check automaticamente: eles continuam
  # pausados até a correção passar pelo patch e pelo operador.
  STAGE="normalização do crontab de emergência"
  local tmp current
  tmp="${TMPDIR:-/tmp}/tts-bot-cron.$$"
  current="${TMPDIR:-/tmp}/tts-bot-cron-current.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    CRONTAB_HEALTH_STATUS="sem crontab do usuário ubuntu"
    rm -f "$tmp" "$current" 2>/dev/null || true
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_CRON'
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')

HEALTH_DISABLED = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_DISABLED = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
HEALTH_ACTIVE = '* * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_ACTIVE = '*/5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'

out = []
has_disabled_health = False
has_active_health = False
has_disabled_resource = False
has_active_resource = False

def is_redirect_only(line):
    return line.strip() in {'>/dev/null 2>&1', '>>/dev/null 2>&1', '2>&1', '&>/dev/null'}

def kind_for(line):
    if 'healthcheck.sh' in line:
        return 'health'
    if 'resource-check.sh' in line:
        return 'resource'
    return None

def disabled(line):
    return line.lstrip().startswith('#') or 'TEMP_DISABLED' in line

for raw in text.splitlines():
    line = raw.rstrip('\r')
    if is_redirect_only(line):
        continue
    kind = kind_for(line)
    if kind == 'health':
        if disabled(line):
            if not has_disabled_health:
                out.append(HEALTH_DISABLED)
                has_disabled_health = True
        else:
            if not has_active_health:
                out.append(HEALTH_ACTIVE)
                has_active_health = True
        continue
    if kind == 'resource':
        if disabled(line):
            if not has_disabled_resource:
                out.append(RESOURCE_DISABLED)
                has_disabled_resource = True
        else:
            if not has_active_resource:
                out.append(RESOURCE_ACTIVE)
                has_active_resource = True
        continue
    out.append(line)

dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    cp -a /var/spool/cron/crontabs/ubuntu "$REPO_DIR/crontab.backup.auto-clean.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    sudo -u ubuntu -H crontab "$tmp" || true
    CRONTAB_HEALTH_STATUS="normalizado; healthcheck/resource-check seguem pausados se estavam pausados"
  else
    CRONTAB_HEALTH_STATUS="limpo"
  fi
  rm -f "$tmp" "$current" 2>/dev/null || true
}

managed_systemd_template_source() {
  local rel="${1:-}"
  local root_src="$REPO_DIR/deploy/systemd/$rel"
  local vps_src="$REPO_DIR/deploy/systemd/vps/$rel"

  # Quando o mesmo template existe nos dois diretórios, respeite o caminho que
  # realmente mudou. A preferência fixa por vps/ fazia patches na raiz serem
  # aceitos e commitados, mas a unit antiga continuava instalada na VPS.
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/vps/$rel"; then
    [[ -f "$vps_src" ]] && printf '%s' "$vps_src"
    return 0
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/$rel"; then
    [[ -f "$root_src" ]] && printf '%s' "$root_src"
    return 0
  fi

  if [[ -f "$vps_src" ]]; then
    printf '%s' "$vps_src"
  elif [[ -f "$root_src" ]]; then
    printf '%s' "$root_src"
  fi
}

build_vps_systemd_template_overlay() {
  local overlay="${1:?overlay obrigatório}"
  local rel src changed_path
  local -a managed_units=(
    tts-bot.service
    tts-bot-updater.service
    tts-bot-updater.timer
    tts-bot-alert@.service
    cleanup-audio-temp.service
    cleanup-audio-temp.timer
    sinuca-activity-server.service
    phone-worker-watch.service
    phone-worker-watch.timer
  )

  mkdir -p "$overlay"
  for rel in "${managed_units[@]}"; do
    src="$(managed_systemd_template_source "$rel")"
    [[ -n "$src" && -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done

  # Os drop-ins vivem normalmente em vps/. Mantenha a árvore completa e
  # sobreponha apenas arquivos da raiz explicitamente alterados no patch.
  if [[ -d "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d" ]]; then
    mkdir -p "$overlay/tts-bot.service.d"
    cp -a "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d/." "$overlay/tts-bot.service.d/"
  fi
  while IFS= read -r changed_path; do
    [[ "$changed_path" == deploy/systemd/tts-bot.service.d/* ]] || continue
    rel="${changed_path#deploy/systemd/}"
    src="$REPO_DIR/$changed_path"
    [[ -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done <<< "$CHANGED_FILES_RAW"
}


deploy_vps_systemd_units() {
  if (( VPS_SYSTEMD_UNITS_CHANGED == 0 )); then
    VPS_SYSTEMD_UNITS_STATUS="não alterado"
    return 0
  fi

  STAGE="sincronização dos units systemd da VPS"
  if [[ ! -x "$REPO_DIR/scripts/install-vps-systemd-units.sh" && ! -f "$REPO_DIR/scripts/install-vps-systemd-units.sh" ]]; then
    VPS_SYSTEMD_UNITS_STATUS="script ausente"
    LAST_ERROR_STDERR="scripts/install-vps-systemd-units.sh não foi encontrado"
    return 1
  fi

  local rc=0 template_overlay
  template_overlay="$(mktemp -d "${TMPDIR:-/tmp}/tts-bot-systemd-overlay.XXXXXX")"
  if ! build_vps_systemd_template_overlay "$template_overlay"; then
    rm -rf "$template_overlay"
    VPS_SYSTEMD_UNITS_STATUS="falha ao preparar templates"
    LAST_ERROR_STDERR="não foi possível preparar os templates systemd do patch"
    return 1
  fi

  if REPO_DIR="$REPO_DIR" TEMPLATE_DIR="$template_overlay" \
      bash "$REPO_DIR/scripts/install-vps-systemd-units.sh" --from-updater; then
    VPS_SYSTEMD_UNITS_STATUS="sincronizados"
    rm -rf "$template_overlay"
    return 0
  else
    rc=$?
  fi
  rm -rf "$template_overlay"

  VPS_SYSTEMD_UNITS_STATUS="falha ao sincronizar"
  LAST_ERROR_STDERR="o instalador das units systemd falhou; a VPS não foi deixada com templates parcialmente atualizados"
  return "$rc"
}

deploy_alert_unit() {
  STAGE="configuração do alerta systemd"
  local src=""
  src="$(managed_systemd_template_source "tts-bot-alert@.service")"
  if [[ -n "$src" ]]; then
    cp "$src" /etc/systemd/system/tts-bot-alert@.service
    systemctl daemon-reload || true
    ALERT_UNIT_STATUS="unit instalada"
  else
    ALERT_UNIT_STATUS="unit ausente no deploy"
  fi
}

deploy_audio_services() {
  sanitize_vps_lavalink_units
  if (( AUDIO_SYSTEMD_CHANGED == 0 )); then
    AUDIO_SERVICES_STATUS="não alterado; Lavalink VPS sanitizado"
    return 0
  fi

  STAGE="configuração dos serviços de áudio"
  local installed=0 lavalink_unit_changed=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/lavalink\.service$'; then
    lavalink_unit_changed=1
    if env_truthy VPS_LAVALINK_ENABLED && [[ -f "$REPO_DIR/deploy/systemd/lavalink.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/lavalink.service" /etc/systemd/system/lavalink.service
      installed=1
    fi
  fi

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/tts-bot\.service$'; then
    if [[ -f "$REPO_DIR/deploy/systemd/tts-bot.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/tts-bot.service" /etc/systemd/system/tts-bot.service
      installed=1
    fi
  fi

  if (( installed == 1 )); then
    systemctl daemon-reload
  fi

  if (( lavalink_unit_changed == 1 )); then
    if env_truthy VPS_LAVALINK_ENABLED; then
      systemctl enable "$LAVALINK_SERVICE" >/dev/null 2>&1 || true
      systemctl restart "$LAVALINK_SERVICE" || true
      if systemctl is-active --quiet "$LAVALINK_SERVICE"; then
        AUDIO_SERVICES_STATUS="Lavalink ativo"
      else
        AUDIO_SERVICES_STATUS="Lavalink configurado, mas não ficou ativo"
      fi
    else
      AUDIO_SERVICES_STATUS="Lavalink VPS não iniciado; node de áudio roda no phone worker/Music Agent"
    fi
  else
    AUDIO_SERVICES_STATUS="units atualizadas; Lavalink não alterado"
  fi
}


deploy_cleanup_timer() {
  if (( CLEANUP_CHANGED == 0 )); then
    CLEANUP_STATUS="não alterada"
    return 0
  fi

  STAGE="configuração da limpeza de temporários"
  local installed=0

  local cleanup_service_src="" cleanup_timer_src=""
  cleanup_service_src="$(managed_systemd_template_source "cleanup-audio-temp.service")"
  cleanup_timer_src="$(managed_systemd_template_source "cleanup-audio-temp.timer")"

  if [[ -f "$cleanup_service_src" ]]; then
    cp "$cleanup_service_src" /etc/systemd/system/cleanup-audio-temp.service
    installed=1
  fi
  if [[ -f "$cleanup_timer_src" ]]; then
    cp "$cleanup_timer_src" /etc/systemd/system/cleanup-audio-temp.timer
    installed=1
  fi

  if (( installed == 0 )); then
    CLEANUP_STATUS="units de limpeza não encontrados"
    return 0
  fi

  systemctl daemon-reload
  systemctl enable --now cleanup-audio-temp.timer >/dev/null 2>&1 || true
  systemctl start cleanup-audio-temp.service >/dev/null 2>&1 || true

  if systemctl is-active --quiet cleanup-audio-temp.timer; then
    CLEANUP_STATUS="timer ativo"
  else
    CLEANUP_STATUS="timer instalado, mas não ativo"
  fi
}


deploy_phone_lavalink_watch() {
  # Lavalink/NodeLink foi removido do worker. Esta etapa não instala mais unit
  # nova; ela só desativa qualquer timer/service antigo que ainda exista na VPS.
  if (( PHONE_LAVALINK_WATCH_CHANGED == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="removido do fluxo"
    return 0
  fi

  STAGE="desativando watcher legado do Lavalink"
  systemctl disable --now phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl reset-failed phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true
  PHONE_LAVALINK_WATCH_STATUS="desativado/removido do fluxo"
}


deploy_phone_worker_watch() {
  if (( PHONE_WORKER_WATCH_CHANGED == 0 )); then
    PHONE_WORKER_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do phone worker"
  local installed=0

  # Não chmod em script rastreado: systemd usa /usr/bin/env bash e isso evita repo sujo.
  local phone_worker_service_src="" phone_worker_timer_src=""
  phone_worker_service_src="$(managed_systemd_template_source "phone-worker-watch.service")"
  phone_worker_timer_src="$(managed_systemd_template_source "phone-worker-watch.timer")"

  if [[ -f "$phone_worker_service_src" ]]; then
    cp "$phone_worker_service_src" /etc/systemd/system/phone-worker-watch.service
    installed=1
  fi
  if [[ -f "$phone_worker_timer_src" ]]; then
    cp "$phone_worker_timer_src" /etc/systemd/system/phone-worker-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_WORKER_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local worker_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    worker_value="$(grep -E '^PHONE_WORKER_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    worker_value="${worker_value,,}"
  fi

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_WORKER_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "1" || "$watch_value" == "true" || "$watch_value" == "yes" || "$watch_value" == "on" || "$watch_value" == "sim" ]]; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-worker-watch.timer; then
      PHONE_WORKER_WATCH_STATUS="timer ativo"
    else
      PHONE_WORKER_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-worker-watch.timer phone-worker-watch.service >/dev/null 2>&1 || true
    PHONE_WORKER_WATCH_STATUS="instalado; inativo até PHONE_WORKER_WATCH_ENABLED=true"
  fi
}


deploy_phone_worker_sync() {
  if (( PHONE_WORKER_SYNC_REQUIRED == 0 )); then
    PHONE_WORKER_SYNC_STATUS="sem mudanças"
    return 0
  fi

  if ! env_truthy PHONE_WORKER_LEGACY_SSH_SYNC_ENABLED; then
    PHONE_WORKER_SYNC_STATUS="agendado para automação por jobs após restart"
    return 0
  fi

  STAGE="sincronização legada do phone-worker por SSH"

  if [[ ! -x "$REPO_DIR/scripts/sync-phone-worker.sh" ]]; then
    PHONE_WORKER_SYNC_STATUS="não executado: scripts/sync-phone-worker.sh ausente"
    return 0
  fi

  local output status_line
  output="$(sudo -u ubuntu -H bash "$REPO_DIR/scripts/sync-phone-worker.sh" 2>&1 || true)"
  status_line="$(printf '%s\n' "$output" | grep -E '\[phone-worker-sync\]' | tail -n 1 | sed -E 's/^\[phone-worker-sync\][[:space:]]*//' || true)"

  if [[ -n "${status_line//[[:space:]]/}" ]]; then
    PHONE_WORKER_SYNC_STATUS="$status_line"
  else
    PHONE_WORKER_SYNC_STATUS="executado; sem status legível"
  fi

  logger -t "$LOG_TAG" "Phone-worker sync legado: $PHONE_WORKER_SYNC_STATUS"
  return 0
}

run_core_worker_post_update_automation() {
  if (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )); then
    CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
    CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
    CORE_WORKER_NOTIFY_STATUS="sem mudanças"
    return 0
  fi

  STAGE="automação pós-update dos Core Workers"
  local py="$REPO_DIR/.venv/bin/python"
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  if [[ -z "$py" || ! -f "$REPO_DIR/scripts/core-worker-automation.py" ]]; then
    CORE_WORKER_AGENT_UPDATE_STATUS="não executado: core-worker-automation ausente"
    CORE_WORKER_APK_BUILD_STATUS="não executado"
    CORE_WORKER_NOTIFY_STATUS="não executado"
    return 0
  fi

  local output automation_rc=0
  output="$(sudo -u ubuntu -H env CORE_WORKER_CHANGED_FILES="$CHANGED_FILES_RAW" "$py" "$REPO_DIR/scripts/core-worker-automation.py" after-update 2>&1)" || automation_rc=$?
  logger -t "$LOG_TAG" "Core Worker automation: $output"

  local parsed
  parsed="$(CORE_WORKER_AUTOMATION_RAW="$output" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
raw = os.environ.get('CORE_WORKER_AUTOMATION_RAW') or '{}'
line = next((ln for ln in reversed(raw.splitlines()) if ln.strip().startswith('{')), '{}')
try:
    data = json.loads(line)
except Exception:
    data = {}
agent = data.get('agent_update') or {}
apk = data.get('apk_build') or {}
def brief_agent(obj):
    if not obj:
        return 'sem mudanças'
    queued = len(obj.get('queued') or [])
    skipped = len(obj.get('skipped') or [])
    errors = len(obj.get('errors') or [])
    version = obj.get('target_version') or '?'
    return f'agent {version}: {queued} job(s), {skipped} skip, {errors} erro(s)'
def brief_apk(obj):
    if not obj:
        return 'sem mudanças'
    if obj.get('ok'):
        job = obj.get('job') or {}
        if job.get('job_id'):
            return f"APK {obj.get('versionName') or '?'}: build job {job.get('job_id')}"
        if obj.get('already_published'):
            return f"APK {obj.get('versionName') or '?'}: já publicado para esta fonte"
        return f"APK {obj.get('versionName') or '?'}: {obj.get('message') or obj.get('phase') or 'pendente'}"
    return f"APK {obj.get('versionName') or '?'}: pendente ({obj.get('message') or obj.get('error') or 'sem builder'})"
print(brief_agent(agent))
print(brief_apk(apk))
print('apps verão banner/notify quando latest.json novo for publicado' if apk else 'sem notificação nova')
PYJSON
)"
  CORE_WORKER_AGENT_UPDATE_STATUS="$(printf '%s\n' "$parsed" | sed -n '1p')"
  CORE_WORKER_APK_BUILD_STATUS="$(printf '%s\n' "$parsed" | sed -n '2p')"
  CORE_WORKER_NOTIFY_STATUS="$(printf '%s\n' "$parsed" | sed -n '3p')"
  [[ -n "${CORE_WORKER_AGENT_UPDATE_STATUS//[[:space:]]/}" ]] || CORE_WORKER_AGENT_UPDATE_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_APK_BUILD_STATUS//[[:space:]]/}" ]] || CORE_WORKER_APK_BUILD_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_NOTIFY_STATUS//[[:space:]]/}" ]] || CORE_WORKER_NOTIFY_STATUS="executado"
  if (( automation_rc != 0 )); then
    CORE_WORKER_AGENT_UPDATE_STATUS="${CORE_WORKER_AGENT_UPDATE_STATUS} · automação degradada (rc=$automation_rc)"
    CORE_WORKER_APK_BUILD_STATUS="${CORE_WORKER_APK_BUILD_STATUS} · automação degradada (rc=$automation_rc)"
    CORE_WORKER_NOTIFY_STATUS="falha da automação registrada; updater principal preservado"
    logger -t "$LOG_TAG" "Core Worker automation terminou degradada (rc=$automation_rc)"
  fi
  return 0
}


restart_bot_service_once() {
  local phase="deploy"
  local count=0
  if (( ROLLBACK_IN_PROGRESS == 1 )); then
    phase="rollback"
    count="$BOT_RESTARTS_ROLLBACK"
  else
    count="$BOT_RESTARTS_DEPLOY"
  fi

  if (( count >= 1 )); then
    LAST_ERROR_STDERR="restart do bot bloqueado: limite de 1 reinício na fase $phase já foi consumido"
    logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
    return 75
  fi

  # Consuma o orçamento antes da chamada: mesmo uma tentativa que pare o
  # processo e falhe ao subir não pode ser repetida indefinidamente.
  if [[ "$phase" == "rollback" ]]; then
    BOT_RESTARTS_ROLLBACK=$((BOT_RESTARTS_ROLLBACK + 1))
  else
    BOT_RESTARTS_DEPLOY=$((BOT_RESTARTS_DEPLOY + 1))
  fi

  # Um start-limit-hit anterior não deve impedir um único reinício legítimo.
  # O limite por execução acima evita transformar reset-failed em loop.
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE" || return $?
  logger -t "$LOG_TAG" "restart do bot executado: fase=$phase deploy=$BOT_RESTARTS_DEPLOY rollback=$BOT_RESTARTS_ROLLBACK" 2>/dev/null || true
  return 0
}

mark_deployment_committed() {
  DEPLOYMENT_COMMITTED=1
  STAGE="finalização pós-deploy"
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "deployment_completed" "${REMOTE_COMMIT:-}"
  fi
  logger -t "$LOG_TAG" "limite transacional concluído em $(short_commit "${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}")" 2>/dev/null || true
}

build_final_status_description() {
  local summary="${1:-}"
  local display_id="${2:-}"
  local short_from="${3:-}"
  local short_to="${4:-}"
  local changed_count="${5:-0}"
  local diff_summary="${6:-diff indisponível}"
  local apply_mode="${7:-aplicação concluída}"
  local duration="${8:-tempo indisponível}"
  local health_status="${9:-health não informado}"
  local file_count_text=""

  if [[ "$changed_count" =~ ^[0-9]+$ ]] && (( changed_count == 1 )); then
    file_count_text="1 arquivo alterado"
  elif [[ "$changed_count" =~ ^[0-9]+$ ]]; then
    file_count_text="$changed_count arquivos alterados"
  else
    file_count_text="arquivos alterados"
  fi

  # A string de formato é literal; IDs e commits são somente argumentos. Isso
  # impede que crases de Markdown virem substituição de comando do Bash.
  printf -v ZIP_STATUS_DESCRIPTION '%s\n\nAtualização `%s`\n`%s` → `%s`\n%s · %s\n%s · duração total: %s' \
    "$summary" "$display_id" "$short_from" "$short_to" "$file_count_text" \
    "$diff_summary" "$apply_mode" "$duration"
  # “OK” isolado não comunica nada e aparecia como uma linha solta no cartão.
  # Só acrescente saúde quando houver informação diferente do sucesso padrão.
  if [[ -n "${health_status//[[:space:]]/}" && "$health_status" != "OK" ]]; then
    ZIP_STATUS_DESCRIPTION+=$'\n\n'"Saúde: $health_status"
  fi
}

deploy_bot() {
  # Caminho rápido: não reinstale systemd/watchers/áudio em todo update.
  # Cada rotina só roda quando os arquivos dela mudaram; isso reduz bastante
  # o custo dos patches comuns.
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    normalize_healthcheck_crontab
    deploy_vps_systemd_units
  fi
  if (( ALERT_CHANGED == 1 || VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    deploy_alert_unit
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 )); then
    deploy_audio_services
  fi
  if (( CLEANUP_CHANGED == 1 )); then
    deploy_cleanup_timer
  fi
  if (( PHONE_LAVALINK_WATCH_CHANGED == 1 )); then
    deploy_phone_lavalink_watch
  fi
  if (( PHONE_WORKER_WATCH_CHANGED == 1 )); then
    deploy_phone_worker_watch
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 )); then
    deploy_phone_worker_sync
  fi

  if (( REQUIREMENTS_CHANGED == 1 )); then
    STAGE="dependências do bot"
    if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
      sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    local fast_modules restart_epoch restarts_before
    fast_modules="$(fast_reload_modules_for_changed_files || true)"
    FAST_RELOAD_MODULES="$fast_modules"
    if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
      STAGE="reload rápido de cogs"
      if try_fast_cog_reload "$fast_modules" "$APP_COMMANDS_MAY_HAVE_CHANGED"; then
        return 0
      fi
      logger -t "$LOG_TAG" "Fast reload indisponível; usando restart completo seguro do bot principal. Status: $FAST_RELOAD_STATUS"
      if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
        zip_progress_done "Reload da cog falhou"
        zip_progress_publish "Reiniciando processo: bot"
      fi
      FAST_RELOAD_MODULES=""
    fi

    restarts_before="$(service_restart_count "$SERVICE")"

    STAGE="reinício do bot"
    restart_epoch="$(date +%s)"
    restart_bot_service_once

    if env_truthy LAVALINK_ENABLED; then
      STAGE="espera curta do Lavalink"
      wait_for_lavalink_ready || true
    fi

    STAGE="validação fatal do bot"
    verify_bot_after_restart "$restart_epoch" "$restarts_before"
    return $?
  fi

  STAGE="healthcheck do bot"
  if refresh_bot_health_status; then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  else
    if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
      BOT_HEALTHCHECK_STATUS="não alterado; health HTTP sem resposta"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="falhou: health não saudável ($BOT_HEALTH_DETAIL_STATUS)"
      return 1
    fi
  fi
  return 0
}


frontend_publication_is_healthy() {
  [[ -d "$FRONT_PUBLISH_DIR" ]] || return 1
  [[ -s "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  [[ -r "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  return 0
}

publish_frontend_atomically() {
  local source_dir="$1"
  local publish_parent release_dir backup_path had_previous=0

  publish_parent="$(dirname "$FRONT_PUBLISH_DIR")"
  mkdir -p "$publish_parent"
  chmod 0755 "$publish_parent" 2>/dev/null || true

  release_dir="$(mktemp -d "$publish_parent/.sinuca-release.XXXXXX")" || {
    FRONT_STATUS="não foi possível criar a área temporária da publicação"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  }
  backup_path="$publish_parent/.sinuca-previous.$$.${RANDOM}"

  if command -v rsync >/dev/null 2>&1; then
    if ! rsync -a --delete "$source_dir/" "$release_dir/"; then
      FRONT_STATUS="falha ao preparar a nova publicação do frontend"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      rm -rf -- "$release_dir" 2>/dev/null || true
      return 1
    fi
  elif ! cp -a "$source_dir/." "$release_dir/"; then
    FRONT_STATUS="falha ao preparar a nova publicação do frontend"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    rm -rf -- "$release_dir" 2>/dev/null || true
    return 1
  fi

  if [[ ! -s "$release_dir/index.html" ]]; then
    FRONT_STATUS="publicação temporária sem index.html"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    rm -rf -- "$release_dir" 2>/dev/null || true
    return 1
  fi

  # O Nginx precisa atravessar todos os diretórios e ler os arquivos. A cópia
  # anterior preservava permissões do build e podia publicar uma árvore 0700.
  find "$release_dir" -type d -exec chmod 0755 {} +
  find "$release_dir" -type f -exec chmod 0644 {} +
  chown -R root:root "$release_dir" 2>/dev/null || true

  if [[ -e "$FRONT_PUBLISH_DIR" || -L "$FRONT_PUBLISH_DIR" ]]; then
    if ! mv -- "$FRONT_PUBLISH_DIR" "$backup_path"; then
      FRONT_STATUS="não foi possível preservar a publicação atual"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      rm -rf -- "$release_dir" 2>/dev/null || true
      return 1
    fi
    had_previous=1
  fi

  if ! mv -- "$release_dir" "$FRONT_PUBLISH_DIR"; then
    FRONT_STATUS="não foi possível ativar a nova publicação"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    if (( had_previous == 1 )); then
      mv -- "$backup_path" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    rm -rf -- "$release_dir" 2>/dev/null || true
    return 1
  fi

  if ! frontend_publication_is_healthy; then
    FRONT_STATUS="a nova publicação não ficou legível para o servidor web"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    rm -rf -- "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    if (( had_previous == 1 )); then
      mv -- "$backup_path" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi

  if (( had_previous == 1 )); then
    rm -rf -- "$backup_path" 2>/dev/null || logger -t "$LOG_TAG" "aviso: backup antigo do frontend não pôde ser removido: $backup_path"
  fi
  return 0
}

deploy_frontend() {
  local repair_mode=0

  if (( FRONT_CHANGED == 0 )); then
    if frontend_publication_is_healthy; then
      FRONT_STATUS="não alterado"
      return 0
    fi
    # Mesmo sem arquivos do frontend no patch, restaura automaticamente uma
    # publicação ausente/corrompida. Isso evita manter o Nginx em 403 quando o
    # repositório está íntegro, mas /var/www/sinuca perdeu o index.html.
    FRONT_CHANGED=1
    repair_mode=1
    logger -t "$LOG_TAG" "publicação do frontend inválida; reconstrução automática solicitada"
  fi

  if [[ ! -d "$FRONT_DIR" ]]; then
    FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    return 1
  fi

  STAGE="dependências do frontend"
  zip_progress_run_as_ubuntu \
    "Instalando dependências" \
    "Verificando pacotes" \
    "cd \"$FRONT_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"
  zip_progress_done_and_publish \
    "Dependências instaladas" \
    "Compilando interface" \
    "Gerando os arquivos de produção"

  STAGE="build do frontend"
  zip_progress_run_as_ubuntu \
    "Compilando interface" \
    "Gerando os arquivos de produção" \
    "cd \"$FRONT_DIR\" && npm run build"
  zip_progress_done_and_publish \
    "Interface compilada" \
    "Publicando interface" \
    "Atualizando arquivos"

  STAGE="publicação do frontend"
  if [[ ! -s "$FRONT_DIR/dist/index.html" ]]; then
    FRONT_STATUS="build do frontend não produziu dist/index.html"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  fi
  if ! publish_frontend_atomically "$FRONT_DIR/dist"; then
    return 1
  fi

  STAGE="limpeza do frontend"
  zip_progress_run_as_ubuntu \
    "Publicando interface" \
    "Limpando temporários" \
    "cd \"$FRONT_DIR\" && rm -rf node_modules && { npm cache clean --force >/dev/null 2>&1 || true; }"
  zip_progress_done "Interface publicada"

  if (( repair_mode == 1 )); then
    FRONT_STATUS="frontend reconstruído e republicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  else
    FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  fi
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 && FRONT_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    ACTIVITY_HEALTHCHECK_STATUS="não alterada"
    return 0
  fi

  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    STAGE="healthcheck informativo do painel web"
    zip_progress_publish "Validando" "Confirmando disponibilidade"
    if wait_for_health "$BACK_HEALTH_URL" 3 2; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
      zip_progress_done "Validação concluída"
    else
      ACTIVITY_HEALTHCHECK_STATUS="indisponível; backend não foi alterado"
      zip_progress_done "Publicação concluída; validação indisponível"
    fi
    return 0
  fi

  if [[ ! -d "$BACK_DIR" ]]; then
    BACK_STATUS="backend não encontrado em $BACK_DIR"
    return 1
  fi

  STAGE="dependências do backend"
  zip_progress_run_as_ubuntu \
    "Preparando servidor" \
    "Verificando pacotes" \
    "cd \"$BACK_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"
  zip_progress_done_and_publish \
    "Servidor preparado" \
    "Compilando servidor" \
    "Gerando arquivos"

  STAGE="build do backend"
  zip_progress_run_as_ubuntu \
    "Compilando servidor" \
    "Gerando arquivos" \
    "cd \"$BACK_DIR\" && npm run build && npm prune --omit=dev && { npm cache clean --force >/dev/null 2>&1 || true; }"
  zip_progress_done_and_publish \
    "Servidor compilado" \
    "Reiniciando servidor" \
    "Aplicando nova versão"

  STAGE="reinício do backend"
  if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
    BACK_STATUS="serviço systemd ausente: $BACK_SERVICE"
    LAST_ERROR_STDERR="$BACK_STATUS"
    return 1
  fi
  systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$BACK_SERVICE"
  sleep 3
  if ! systemctl is-active --quiet "$BACK_SERVICE"; then
    BACK_STATUS="serviço não ficou ativo: $BACK_SERVICE"
    LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
    return 1
  fi
  BACK_STATUS="backend reiniciado pelo systemd na porta $BACK_PORT"
  zip_progress_done_and_publish \
    "Servidor reiniciado" \
    "Validando" \
    "Aguardando resposta"

  STAGE="healthcheck do painel web"
  if wait_for_health "$BACK_HEALTH_URL" 8 3; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    zip_progress_done "Validação concluída"
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
  return 1
}


rollback_after_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"
  register_error_context "$exit_code" "$failed_command"

  local rollback_bot_status="não executado"
  local rollback_front_status="não executado"
  local rollback_back_status="não executado"
  local rollback_activity_status="não executado"
  local rollback_git_status="não executado"
  local rollback_success=1
  local reset_status=1
  local head_after_reset=""

  trap - ERR
  set +e

  if (( ROLLBACK_DONE == 1 )); then
    exit "$exit_code"
  fi
  ROLLBACK_DONE=1
  ROLLBACK_IN_PROGRESS=1

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    logger -t "$LOG_TAG" "Erro fatal no candidato local. Tentando rollback para $(short_commit "$PREVIOUS_COMMIT") antes de push GitHub"
  elif (( REMOTE_CANDIDATE_MODE == 1 )); then
    logger -t "$LOG_TAG" "Erro fatal no commit do GitHub. Tentando rollback de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"
  else
    logger -t "$LOG_TAG" "Erro fatal após update. Tentando rollback de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"
  fi

  STAGE="rollback git"
  sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1
  reset_status=$?
  head_after_reset="$(sudo -u ubuntu -H git rev-parse HEAD 2>/dev/null || true)"

  if (( reset_status == 0 )) && [[ -n "$head_after_reset" && "$head_after_reset" == "$PREVIOUS_COMMIT" ]]; then
    cleanup_local_candidate_new_files_after_reset
    rollback_git_status="OK: repositório voltou para $(short_commit "$PREVIOUS_COMMIT")"
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      update_local_candidate_heartbeat "failed" "rollback após falha em $FAILED_STAGE" || true
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); GitHub não foi alterado"
    elif (( REMOTE_CANDIDATE_MODE == 1 )); then
      mark_remote_commit_rejected "$REMOTE_COMMIT" "health falhou após aplicar; rollback para $(short_commit "$PREVIOUS_COMMIT")"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); commit GitHub rejeitado"
    else
      write_dirty_marker "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$FAILED_STAGE" "$failed_command"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); commit remoto marcado como sujo"
    fi
  else
    rollback_success=0
    rollback_git_status="falhou: reset=$reset_status head=$(short_commit "$head_after_reset") esperado=$(short_commit "$PREVIOUS_COMMIT")"
    ROLLBACK_STATUS="falhou antes de restaurar o commit anterior"
  fi

  if (( rollback_success == 1 )); then
    if (( FRONT_CHANGED == 1 )); then
      if deploy_frontend; then
        rollback_front_status="${FRONT_STATUS:-}"
      else
        rollback_success=0
        rollback_front_status="falhou: $FRONT_STATUS"
      fi
    else
      rollback_front_status="não precisou republicar"
    fi

    if (( BACK_CHANGED == 1 )); then
      if deploy_backend; then
        rollback_back_status="${BACK_STATUS:-}"
        rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
      else
        rollback_success=0
        rollback_back_status="falhou: $BACK_STATUS"
        rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
      fi
    else
      if wait_for_health "$BACK_HEALTH_URL" 2 2; then
        rollback_activity_status="OK"
      else
        rollback_activity_status="não verificada/indisponível"
      fi
      rollback_back_status="não precisou reiniciar"
    fi

    if deploy_bot; then
      rollback_bot_status="$BOT_HEALTHCHECK_STATUS"
    else
      rollback_success=0
      rollback_bot_status="falhou: $BOT_HEALTHCHECK_STATUS"
    fi
  else
    rollback_front_status="não executado porque o git reset falhou"
    rollback_back_status="não executado porque o git reset falhou"
    rollback_activity_status="não executado porque o git reset falhou"
    rollback_bot_status="não executado porque o git reset falhou"
  fi

  local duration title summary commit_dirty
  duration="$(human_duration "$SECONDS")"
  if (( rollback_success == 1 )); then
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      title="Update revertido"
      summary="O ZIP foi testado na VPS, falhou na validação e o bot voltou ao estado anterior. Nenhum commit foi enviado ao GitHub."
      commit_dirty="não; GitHub não foi alterado"
    elif (( REMOTE_CANDIDATE_MODE == 1 )); then
      title="Update do GitHub revertido"
      summary="O commit do GitHub falhou depois da aplicação. A VPS voltou ao último estado saudável e esse commit foi rejeitado."
      commit_dirty="sim; commit GitHub rejeitado"
    else
      title="Rollback aplicado após erro fatal"
      summary="O update falhou, mas o rollback voltou o repositório para o commit anterior e os serviços foram validados."
      commit_dirty="sim"
    fi
  else
    title="Rollback falhou após erro fatal"
    summary="O update falhou e o rollback não conseguiu restaurar completamente o estado anterior. Verificação manual necessária."
    commit_dirty="não confirmado"
  fi

  local body
  body="Resumo: $summary
Host: $HOSTNAME
Branch: $BRANCH
Commit: $(short_commit "$PREVIOUS_COMMIT") ← $(short_commit "$REMOTE_COMMIT")
Mudança: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: ${FAILED_STAGE:-$STAGE}
Comando: $failed_command
Código: $exit_code
Validações:
• Git reset: $rollback_git_status
• Bot: $rollback_bot_status
• Cogs: $BOT_COGS_STATUS
• Health: $BOT_HEALTH_DETAIL_STATUS
Serviços:
• Frontend: $rollback_front_status
• Backend: $rollback_back_status
• Painel web: $rollback_activity_status
Rollback: $ROLLBACK_STATUS
Commit sujo: $commit_dirty
Ação sugerida: Se o rollback falhou, verifique o serviço manualmente antes de aplicar outro update. Se foi aplicado, faça um novo commit corrigido para liberar o updater novamente.
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  notify_zip_status_message "error" "$title" "$summary" || true
  send_error "$title" "$body"
  exit "$exit_code"
}

handle_post_deploy_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"
  local failed_line="${3:-?}"
  local failed_function="${4:-main}"
  local display_id="${LOCAL_CANDIDATE_DISPLAY_ID:-}"
  local current_head="${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}"
  local previous_head="${PREVIOUS_COMMIT:-${CURRENT_COMMIT:-}}"
  local safe_description="" body="" event_id=""

  trap - ERR
  set +e
  DELIVERY_PHASE=1
  [[ -n "${display_id//[[:space:]]/}" ]] || display_id="UPD-$(short_commit "$current_head" | tr '[:lower:]' '[:upper:]')"
  printf -v safe_description '%s\n\nIdentificador: %s\nCommit: %s → %s\n\nA confirmação detalhada será reenviada automaticamente.' \
    'A atualização foi aplicada e validada. Uma falha ocorreu somente na etapa de notificação; o código não foi revertido.' \
    "$display_id" "$(short_commit "$previous_head")" "$(short_commit "$current_head")"

  logger -t "$LOG_TAG" "falha pós-deploy ignorada para rollback: etapa=$STAGE função=$failed_function linha=$failed_line comando=$failed_command rc=$exit_code" 2>/dev/null || true

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "delivery_degraded" "$current_head" || true
    notify_zip_status_message "success" "✅ Atualização concluída" "$safe_description" || true
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "success" "✅ Alteração concluída" "$safe_description" "" || true
  else
    notify_zip_status_message "success" "✅ Atualização concluída" "$safe_description" || true
  fi

  body="Resumo: A atualização foi aplicada e permaneceu ativa; somente a finalização visual/log falhou.
Identificador: $display_id
Branch: $BRANCH
Commit: $(short_commit "$previous_head") → $(short_commit "$current_head")
Etapa: $STAGE
Função: $failed_function
Linha: $failed_line
Comando: $failed_command
Código: $exit_code
Rollback: bloqueado porque o deploy já havia sido validado/publicado
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  event_id="${display_id}-delivery-degraded-$(short_commit "$current_head")"
  send_alert_reliably "warn" "⚠️ Confirmação final pendente" "$body" "" "" "$event_id" || true
  flush_update_status_outbox || true
  flush_update_alert_outbox || true

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    archive_local_candidate "done" || true
    trigger_updater_if_queue_pending || true
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    archive_rollback_request "done" || true
  fi

  logger -t "$LOG_TAG" "deploy preservado apesar de falha pós-deploy: $display_id" 2>/dev/null || true
  exit 0
}

on_error() {
  local exit_code="$?"
  local failed_line="${1:-${BASH_LINENO[0]:-?}}"
  local failed_function="${2:-${FUNCNAME[1]:-main}}"
  if (( MANUAL_FAILURE_ALERT_SENT == 1 )); then
    exit "$exit_code"
  fi
  local failed_command="${BASH_COMMAND:-desconhecido}"
  FAILED_STAGE="$STAGE"
  LAST_ERROR_LINE="$failed_line"
  LAST_ERROR_FUNCTION="$failed_function"
  register_error_context "$exit_code" "$failed_command"

  # Depois do limite transacional, qualquer falha restante pertence apenas à
  # entrega da confirmação. Nunca faça git reset, restart ou marque o commit
  # remoto como rejeitado nessa fase.
  if (( DEPLOYMENT_COMMITTED == 1 )); then
    handle_post_deploy_failure "$exit_code" "$failed_command" "$failed_line" "$failed_function"
  fi

  if (( UPDATE_APPLIED == 1 )) && [[ -n "$PREVIOUS_COMMIT" ]]; then
    rollback_after_failure "$exit_code" "$failed_command"
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    cleanup_local_candidate_new_files_after_reset || true
    update_local_candidate_heartbeat "failed" "falha em $FAILED_STAGE" || true
  fi

  local dirty_status dirty_files
  dirty_status="não"
  dirty_files=""
  if [[ "$FAILED_STAGE" == "verificação de alterações locais" || "$STAGE" == "git pull" ]]; then
    dirty_files="$(collect_local_tracked_changes || true)"
    if [[ -n "${dirty_files//[[:space:]]/}" ]]; then
      dirty_status="sim"
    fi
  fi

  local body
  body="Resumo: O updater falhou antes de concluir a troca de commit.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: ${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: $STAGE
Função: $failed_function
Linha: $failed_line
Comando: $failed_command
Código: $exit_code
Rollback: $ROLLBACK_STATUS
Commit sujo: $dirty_status
Arquivos sujos:
${dirty_files:-nenhum arquivo rastreado sujo detectado}
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    retry_control=""
    if (( UPDATE_APPLIED == 0 )); then
      retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    fi
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi mantido quando possível. Verifique o webhook/log interno." "$retry_control" || true
    archive_rollback_request "failed"
  else
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      notify_zip_status_message "error" "Falha ao aplicar atualização" "A VPS foi restaurada quando possível e o candidato foi arquivado. Verifique o canal de logs." || true
      archive_local_candidate "failed"
    else
      notify_zip_status_message "error" "Falha na atualização" "O updater falhou antes de concluir a aplicação. Verifique o webhook/log interno." || true
    fi
  fi
  send_error "Falha na atualização automática" "$body"
  exit "$exit_code"
}

trap 'cleanup_runtime_artifacts' EXIT
trap 'on_error "$LINENO" "${FUNCNAME[0]:-main}"' ERR

SECONDS=0
cd "$REPO_DIR"
prepare_update_delivery_dirs || true
mkdir -p "$CANDIDATE_QUEUE_CANCELLED_DIR" "$CANDIDATE_ROOT/cancelled" 2>/dev/null || true
prune_update_artifacts || true
if ! guard_updater_disk_space; then
  exit 0
fi
flush_update_status_outbox || true
flush_update_alert_outbox || true
refresh_pending_queue_messages || true

if load_pending_rollback_request; then
  logger -t "$LOG_TAG" "Controle de update recebido: $ROLLBACK_REQUEST_ACTION $ROLLBACK_REQUEST_ID"
  prepare_rollback_request_update
elif load_pending_local_candidate; then
  logger -t "$LOG_TAG" "Candidato local recebido: $LOCAL_CANDIDATE_ID"
  prepare_local_candidate_update
else
  STAGE="commit atual"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"

  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  COMMIT_SUBJECT="$(sudo -u ubuntu -H git log -1 --pretty=%s "$REMOTE_COMMIT")"
  mark_update_timing "fetch"

  if [[ -f "$DIRTY_MARKER_FILE" ]]; then
    MARKED_FAILED_COMMIT="$(marker_value FAILED_REMOTE_COMMIT)"
    if [[ -n "$MARKED_FAILED_COMMIT" && "$REMOTE_COMMIT" == "$MARKED_FAILED_COMMIT" ]]; then
      logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") continua marcado como sujo após rollback fatal; aguardando um novo commit no GitHub."
      exit 0
    fi
    clear_dirty_marker
  fi

  if remote_commit_is_rejected "$REMOTE_COMMIT"; then
    logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") já foi rejeitado; aguardando novo commit no GitHub ou ZIP."
    exit 0
  fi

  if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
    logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
    exit 0
  fi

  REMOTE_CANDIDATE_MODE=1
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"

  CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT")"
  CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --numstat "$CURRENT_COMMIT" "$REMOTE_COMMIT")"
  mark_update_timing "diff"

  classify_changed_files

  eval "$(create_direct_update_message "applying" "$(zip_progress_title "Conferindo commit do GitHub")" "$UPDATE_STAGE_EMOJI **Conferindo commit do GitHub**")"
  zip_progress_publish "Conferindo commit do GitHub"

  STAGE="validação do commit remoto em staging"
  if ! validate_remote_commit_in_staging "$REMOTE_COMMIT"; then
    reject_remote_commit_without_live_apply "preflight falhou no staging remoto"
  fi
  mark_update_timing "remote_preflight"
  zip_progress_done_and_publish "Commit conferido" "Aplicando na VPS"

  STAGE="limpeza de artefatos gerados"
  cleanup_known_generated_update_artifacts

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  logger -t "$LOG_TAG" "Aplicando commit remoto validado de $CURRENT_COMMIT para $REMOTE_COMMIT"

  STAGE="aplicação do commit GitHub"
  sudo -u ubuntu -H git merge --ff-only "$REMOTE_COMMIT"
  UPDATE_APPLIED=1
  mark_update_timing "apply"
  zip_progress_done "Aplicado na VPS"
fi

FAILED_STAGE=""

if (( LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY == 1 )); then
  # O commit já foi validado e publicado numa execução anterior. Repetir o
  # pipeline aqui reiniciava o bot novamente apenas porque a confirmação final
  # tinha falhado. Nesta retomada, apenas confirmamos o estado atual e seguimos
  # para a entrega idempotente.
  mark_deployment_committed
  zip_progress_publish "Recuperando confirmação final"
  PREFLIGHT_PY_STATUS="validado na execução anterior"
  PREFLIGHT_BASH_STATUS="validado na execução anterior"
  PREFLIGHT_COG_IMPORT_STATUS="validado na execução anterior"
  if refresh_bot_health_status; then
    BOT_HEALTHCHECK_STATUS="OK"
  else
    BOT_HEALTHCHECK_STATUS="commit publicado; health indisponível na recuperação"
    UPDATE_HAS_WARNINGS=1
  fi
  read_app_command_sync_status
  zip_progress_done "Estado publicado confirmado"
else
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_publish "Validando arquivos"
  fi
  run_preflight_checks
  mark_update_timing "preflight"
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done "Arquivos validados"
    zip_progress_publish "$(zip_progress_next_apply_stage)"
  fi

  deploy_bot
  mark_update_timing "bot"
  deploy_frontend
  mark_update_timing "frontend"
  deploy_backend
  mark_update_timing "backend"
  run_core_worker_post_update_automation
  mark_update_timing "worker"
  STAGE="verificação pós-build do repositório"
  ensure_no_unstaged_tracked_changes
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done_apply_stage
    zip_progress_publish "Verificando comandos"
  fi
  read_app_command_sync_status
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done "$APP_COMMAND_SYNC_SUMMARY"
  fi

  publish_rollback_request_after_validation
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    mark_deployment_committed
    finalize_rollback_request_success
  fi

  publish_local_candidate_after_validation
  mark_deployment_committed
fi

DURATION="$(human_duration "$SECONDS")"
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES="$(format_changed_files)"
DIFF_TOTAL_SUMMARY="$(format_diff_total_summary)"
CHANGED_FILES_COUNT="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
# O resumo por phone-worker é caro e só deve ser chamado por fluxos de erro/anexo.
# No update saudável, o webhook compacto não precisa dessa análise.

# Atualiza o resumo do health no fim, mesmo que o bot não tenha reiniciado.
refresh_bot_health_status >/dev/null 2>&1 || true
normalize_final_health_warning_state

OVERALL_FATAL=0
if [[ "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
  OVERALL_FATAL=1
fi
if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
  [[ "${ACTIVITY_HEALTHCHECK_STATUS:-}" == "OK" ]] || OVERALL_FATAL=1
fi
recompute_update_warning_flag

# Trava final: a mensagem pública nunca deve ficar amarela quando a própria
# seção de avisos/saúde não tem aviso real. Detalhes informativos como
# "timer ativo", "unit instalada" ou "0 com falha" são sucesso, não warning.
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if ! has_real_warning_text "$BOT_WARNINGS_STATUS" \
    && ! cogs_have_failures "$BOT_COGS_STATUS" \
    && [[ "$BOT_HEALTHCHECK_STATUS" != falhou:* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != *"sem resposta"* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != "OK com avisos" ]]; then
    logger -t "$LOG_TAG" "auto-update: warning público normalizado para sucesso; sem warning exibível"
    UPDATE_HAS_WARNINGS=0
  fi
fi

if (( OVERALL_FATAL == 0 && UPDATE_HAS_WARNINGS == 0 )); then
  ALERT_TYPE="success"
  ALERT_TITLE="✅ Atualização concluída"
  ALERT_SUMMARY="Atualização aplicada e validada."
elif (( OVERALL_FATAL == 0 )); then
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Atualização concluída com avisos"
  ALERT_SUMMARY="A atualização foi aplicada, mas há avisos que precisam de revisão."
else
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Atualização concluída com alerta"
  ALERT_SUMMARY="A atualização terminou com um alerta. Verifique os pontos abaixo."
fi

APPLY_MODE="reinício completo"
if [[ "$FAST_RELOAD_STATUS" == OK* ]]; then
  APPLY_MODE="recarga controlada de cog"
elif (( BOT_CHANGED == 0 )); then
  APPLY_MODE="sem reinício do bot"
fi

PUBLIC_WARNINGS=""
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if has_real_warning_text "$BOT_WARNINGS_STATUS"; then
    PUBLIC_WARNINGS+="${BOT_WARNINGS_STATUS}"
  fi
  if cogs_have_failures "$BOT_COGS_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_COGS_STATUS"
  fi
  if has_real_warning_text "$PREFLIGHT_COG_IMPORT_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$PREFLIGHT_COG_IMPORT_STATUS"
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == *"sem resposta"* || "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_HEALTHCHECK_STATUS"
  fi
fi
[[ -n "${PUBLIC_WARNINGS//[[:space:]]/}" ]] || PUBLIC_WARNINGS="sem avisos"

read_app_command_sync_status
CHANGED_PROCESSES="$(format_changed_processes)"
UPDATE_DISPLAY_ID="${LOCAL_CANDIDATE_DISPLAY_ID:-}"
if [[ -z "${UPDATE_DISPLAY_ID//[[:space:]]/}" ]]; then
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    UPDATE_DISPLAY_ID="REV-$(short_commit "${REMOTE_COMMIT:-$ROLLBACK_NEW_COMMIT}")"
  else
    UPDATE_DISPLAY_ID="UPD-$(short_commit "${REMOTE_COMMIT:-$CURRENT_COMMIT}" | tr '[:lower:]' '[:upper:]')"
  fi
fi
CHECKS_TEXT="✓ Bot — ${BOT_HEALTHCHECK_STATUS}
✓ Python — ${PREFLIGHT_PY_STATUS}
✓ Bash — ${PREFLIGHT_BASH_STATUS}
✓ Cogs — ${PREFLIGHT_COG_IMPORT_STATUS}
✓ Comandos — ${APP_COMMAND_SYNC_SUMMARY}"
TIMINGS_TEXT="${UPDATER_TIMINGS:-sem etapas}, total=${DURATION}"
BODY="Resumo: $ALERT_SUMMARY
Identificador: $UPDATE_DISPLAY_ID
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: $(format_update_file_count "$CHANGED_FILES_COUNT") · $DIFF_TOTAL_SUMMARY
Aplicação: $APPLY_MODE
Processos alterados: $CHANGED_PROCESSES
Duração: $DURATION
Verificações:
$CHECKS_TEXT
Tempos: $TIMINGS_TEXT
Arquivos:
$CHANGED_FILES"

if (( UPDATE_HAS_WARNINGS == 1 || OVERALL_FATAL == 1 )); then
  BODY+=$'\n'"Bot: $BOT_HEALTHCHECK_STATUS"
  BODY+=$'\n'"Cogs: $BOT_COGS_STATUS"
  BODY+=$'\n'"Health: $BOT_HEALTH_DETAIL_STATUS"
fi
if (( UPDATE_HAS_WARNINGS == 1 )); then
  BODY+=$'\n'"Avisos: $PUBLIC_WARNINGS"
fi
if [[ -n "${APP_COMMAND_SYNC_WEBHOOK_BLOCK//[[:space:]]/}" ]]; then
  BODY+=$'\n'"$APP_COMMAND_SYNC_WEBHOOK_BLOCK"
fi
BODY+=$'\n'"Hora: $(date '+%d/%m/%Y %H:%M:%S')"
logger -t "$LOG_TAG" "timings: ${UPDATER_TIMINGS:-sem etapas}; total=$DURATION"
if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
  zip_progress_publish "Finalizando..."
fi
DELIVERY_PHASE=1

build_final_status_description \
  "$ALERT_SUMMARY" \
  "$UPDATE_DISPLAY_ID" \
  "$SHORT_FROM" \
  "$SHORT_TO" \
  "$CHANGED_FILES_COUNT" \
  "$DIFF_TOTAL_SUMMARY" \
  "$APPLY_MODE" \
  "$DURATION" \
  "$BOT_HEALTHCHECK_STATUS"
if (( UPDATE_HAS_WARNINGS == 1 || OVERALL_FATAL == 1 )); then
  ZIP_STATUS_DESCRIPTION+=$'\n\nDetalhes enviados no canal de logs.'
fi
ZIP_STATUS_CONTROL_JSON=""
if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && OVERALL_FATAL == 0 )); then
  source_author_id=""
  if (( LOCAL_CANDIDATE_MODE == 1 )) && [[ -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]]; then
    source_author_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.source_author_id 2>/dev/null || true)"
  fi
  ZIP_STATUS_CONTROL_JSON="$(python3 - "$BRANCH" "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$source_author_id" "$UPDATE_DISPLAY_ID" <<'PYCTRL'
import hashlib, json, sys
branch, head, previous, author, display_id = sys.argv[1:6]
print(json.dumps({
    "enabled": True,
    "mode": "rollback",
    "token": hashlib.sha256(f"{display_id}:{head}".encode()).hexdigest()[:16],
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": head,
    "head_commit": head,
    "update_from": previous,
    "update_to": head,
    "source_author_id": author,
}, ensure_ascii=False))
PYCTRL
)"
fi
if (( LOCAL_CANDIDATE_MODE == 1 )); then
  write_local_candidate_state "finalizing_delivery" "$REMOTE_COMMIT"
fi
FINAL_STATUS_DELIVERY_RC=0
notify_zip_status_message "$ALERT_TYPE" "$ALERT_TITLE" "$ZIP_STATUS_DESCRIPTION" || FINAL_STATUS_DELIVERY_RC=$?
flush_update_status_outbox || true

FINAL_ALERT_EVENT_ID="${UPDATE_DISPLAY_ID:-update}-final-${SHORT_TO:-unknown}"
FINAL_ALERT_DELIVERY_RC=0
send_alert_reliably "$ALERT_TYPE" "$ALERT_TITLE" "$BODY" "" "" "$FINAL_ALERT_EVENT_ID" || FINAL_ALERT_DELIVERY_RC=$?
flush_update_alert_outbox || true
if (( LOCAL_CANDIDATE_MODE == 1 )); then
  # "delivery_scheduled" significa que cada saída foi entregue ou persistida
  # em outbox. Se nem a entrega nem a persistência funcionarem, o candidato é
  # arquivado como degradado para o reconciliador do bot reconstruir a saída.
  if (( FINAL_STATUS_DELIVERY_RC == 0 && FINAL_ALERT_DELIVERY_RC == 0 )); then
    write_local_candidate_state "delivery_scheduled" "$REMOTE_COMMIT"
  else
    write_local_candidate_state "delivery_degraded" "$REMOTE_COMMIT"
    logger -t "$LOG_TAG" "entrega final degradada: status_rc=$FINAL_STATUS_DELIVERY_RC alert_rc=$FINAL_ALERT_DELIVERY_RC" 2>/dev/null || true
  fi
  archive_local_candidate "done"
  trigger_updater_if_queue_pending
fi
logger -t "$LOG_TAG" "$ALERT_TITLE"
