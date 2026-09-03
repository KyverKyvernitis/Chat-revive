#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do Core Worker/phone-worker.
# Mantém o agent vivo no Termux e é o alvo oficial do Termux:Boot.
set -u

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

INTERVAL="${PHONE_WORKER_WATCH_INTERVAL_SECONDS:-60}"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
START_SCRIPT="$WORKER_DIR/start-phone-worker.sh"
WATCH_LOG="${PHONE_WORKER_WATCH_LOG_FILE:-$WORKER_DIR/phone-worker-watch.log}"
WATCH_PID_FILE="${PHONE_WORKER_WATCH_PID_FILE:-$WORKER_DIR/phone-worker-watch.pid}"
WATCH_LOCK_DIR="${PHONE_WORKER_WATCH_LOCK_DIR:-$WORKER_DIR/.phone-worker-watch.lock}"
STATUS_FILE="${PHONE_WORKER_STATUS_FILE:-$WORKER_DIR/phone-worker.status}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"
SSHD_AUTO_START="${PHONE_WORKER_SSHD_AUTO_START:-true}"
SSHD_PORT="${PHONE_WORKER_SSH_PORT:-8022}"
RUNTIME_ROOT="${PHONE_WORKER_RUNTIME_ROOT:-$HOME/.core-worker-runtime}"
BOOTSTRAP_INTERVAL="${PHONE_WORKER_BOOTSTRAP_INTERVAL_SECONDS:-300}"
BOOTSTRAP_JITTER="${PHONE_WORKER_BOOTSTRAP_JITTER_SECONDS:-30}"
BOOTSTRAP_MAX_BACKOFF="${PHONE_WORKER_BOOTSTRAP_MAX_BACKOFF_SECONDS:-1800}"

# Não use fallback para ~/start-phone-worker.sh: versões antigas nesse atalho
# já causaram loops de pip/clang e aquecimento. O instalador cria um wrapper
# em ~/ que aponta para o script oficial dentro de ~/phone-worker.

mkdir -p "$WORKER_DIR"

log() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  printf '[phone-worker-watch] %s\n' "$*" | tee -a "$WATCH_LOG" >/dev/null
}

now_iso() {
  date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

write_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '%s\n' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

rotate_watch_log_if_needed() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  if [[ -f "$WATCH_LOG" ]]; then
    size=$(wc -c < "$WATCH_LOG" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$MAX_LOG_BYTES" ]]; then
      mv -f "$WATCH_LOG" "$WATCH_LOG.1" 2>/dev/null || true
      : > "$WATCH_LOG"
    fi
  fi
}

truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

sshd_listening() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt 2>/dev/null | grep -Eq "[:.]${SSHD_PORT}[[:space:]]|:${SSHD_PORT}$"
}

ensure_sshd_running() {
  truthy "$SSHD_AUTO_START" || return 0
  command -v sshd >/dev/null 2>&1 || return 0
  if sshd_listening; then
    return 0
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -f 'sshd' >/dev/null 2>&1; then
    log "sshd rodando, mas porta ${SSHD_PORT} não apareceu; mantendo processo existente"
    return 0
  fi
  log "sshd parado; tentando iniciar porta ${SSHD_PORT}"
  sshd -p "$SSHD_PORT" >/dev/null 2>&1 || sshd >/dev/null 2>&1 || true
}

# Impede múltiplos watchdogs competindo entre si. Lock velho é removido se o PID
# gravado nele não existir mais.
watch_pid_is_ours() {
  local pid="${1:-}" cmdline=""
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"watch-phone-worker.sh"* ]]
}

if ! mkdir "$WATCH_LOCK_DIR" 2>/dev/null; then
  old_pid=""
  [[ -f "$WATCH_LOCK_DIR/owner.pid" ]] && old_pid="$(head -n 1 "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null || true)"
  if watch_pid_is_ours "$old_pid"; then
    log "watchdog já ativo; pid=$old_pid"
    write_status "watchdog_already_online pid=$old_pid $(now_iso)"
    exit 0
  fi
  log "lock do watchdog órfão confirmado; recuperando"
  rm -rf "$WATCH_LOCK_DIR" 2>/dev/null || exit 1
  mkdir "$WATCH_LOCK_DIR" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null || true
trap 'if [[ -f "$WATCH_LOCK_DIR/owner.pid" ]] && [[ "$(cat "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null)" == "$$" ]]; then rm -rf "$WATCH_LOCK_DIR" "$WATCH_PID_FILE" 2>/dev/null || true; fi' EXIT INT TERM

printf '%s\n' "$$" > "$WATCH_PID_FILE" 2>/dev/null || true
termux-wake-lock 2>/dev/null || true
write_status "watchdog_running pid=$$ $(now_iso)"
log "watchdog ativo; worker_dir=$WORKER_DIR intervalo=${INTERVAL}s"

bootstrap_script() {
  local active=""
  if [[ -L "$RUNTIME_ROOT/current" || -d "$RUNTIME_ROOT/current" ]]; then
    active="$(cd "$RUNTIME_ROOT/current" 2>/dev/null && pwd -P || true)"
    if [[ -n "$active" && -f "$active/phone_worker_bootstrap.py" ]]; then
      printf '%s\n' "$active/phone_worker_bootstrap.py"
      return 0
    fi
  fi
  [[ -f "$WORKER_DIR/phone_worker_bootstrap.py" ]] && printf '%s\n' "$WORKER_DIR/phone_worker_bootstrap.py"
}

run_bootstrap_check() {
  local script="$(bootstrap_script)"
  [[ -n "$script" ]] || return 0  # protocolo antigo ainda em migração
  command -v python >/dev/null 2>&1 || return 1
  log "verificação pull do bootstrap iniciada"
  if python "$script" --check >> "$WATCH_LOG" 2>&1; then
    log "verificação pull do bootstrap concluída"
    return 0
  fi
  log "verificação pull do bootstrap falhou; worker atual será preservado"
  return 1
}

next_bootstrap=0
bootstrap_failures=0
failures=0
while true; do
  rotate_watch_log_if_needed
  termux-wake-lock 2>/dev/null || true
  ensure_sshd_running
  now_epoch="$(date +%s 2>/dev/null || echo 0)"
  if [[ "$now_epoch" =~ ^[0-9]+$ && "$now_epoch" -ge "$next_bootstrap" ]]; then
    if run_bootstrap_check; then
      bootstrap_failures=0
      backoff="$BOOTSTRAP_INTERVAL"
    else
      bootstrap_failures=$((bootstrap_failures + 1))
      backoff=$((BOOTSTRAP_INTERVAL * (1 << (bootstrap_failures > 3 ? 3 : bootstrap_failures))))
      [[ "$backoff" -gt "$BOOTSTRAP_MAX_BACKOFF" ]] && backoff="$BOOTSTRAP_MAX_BACKOFF"
    fi
    jitter=0
    if [[ "$BOOTSTRAP_JITTER" =~ ^[0-9]+$ && "$BOOTSTRAP_JITTER" -gt 0 ]]; then jitter=$((RANDOM % (BOOTSTRAP_JITTER + 1))); fi
    next_bootstrap=$((now_epoch + backoff + jitter))
  fi
  if [[ -x "$START_SCRIPT" ]]; then
    if "$START_SCRIPT" >> "$WATCH_LOG" 2>&1; then
      failures=0
      write_status "watchdog_ok pid=$$ $(now_iso)"
    else
      failures=$((failures + 1))
      log "start falhou; falhas=$failures; nova tentativa em ${INTERVAL}s"
      write_status "watchdog_start_failed failures=$failures $(now_iso)"
    fi
  else
    failures=$((failures + 1))
    log "start script não encontrado: $START_SCRIPT"
    write_status "watchdog_missing_start failures=$failures $(now_iso)"
  fi
  sleep "$INTERVAL"
done
