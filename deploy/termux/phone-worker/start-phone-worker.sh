#!/data/data/com.termux/files/usr/bin/bash
# Supervisor local do Core Worker/phone-worker em Termux.
# Garante um único processo, rotaciona logs e inicia sem depender de tmux.
set -u

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
RUNTIME_ROOT="${PHONE_WORKER_RUNTIME_ROOT:-$HOME/.core-worker-runtime}"
ACTIVE_RELEASE_LINK="$RUNTIME_ROOT/current"
RUNTIME_STATE_DIR="${PHONE_WORKER_STATE_DIR:-$HOME/.local/state/core-worker-phone-worker}"
RUNTIME_STATUS_JSON="$RUNTIME_STATE_DIR/runtime-status.json"
MUSIC_AGENT_ENV_FILE="${MUSIC_AGENT_ENV:-$WORKER_DIR/secrets/music-agent.env}"
if [[ -f "$MUSIC_AGENT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MUSIC_AGENT_ENV_FILE"
  set +a
fi
PORT="${PHONE_WORKER_PORT:-8766}"
HOST="${PHONE_WORKER_HOST:-0.0.0.0}"
TOKEN="${PHONE_WORKER_TOKEN:-}"
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
START_WAIT="${PHONE_WORKER_START_WAIT_SECONDS:-3}"
LOG_FILE="${PHONE_WORKER_LOG_FILE:-$WORKER_DIR/phone-worker.log}"
PID_FILE="${PHONE_WORKER_PID_FILE:-$WORKER_DIR/phone-worker.pid}"
LOCK_DIR="${PHONE_WORKER_LOCK_DIR:-$WORKER_DIR/.phone-worker-start.lock}"
STATUS_FILE="${PHONE_WORKER_STATUS_FILE:-$WORKER_DIR/phone-worker.status}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"
KILL_DUPLICATES="${PHONE_WORKER_START_KILL_DUPLICATES:-true}"
SSHD_AUTO_START="${PHONE_WORKER_SSHD_AUTO_START:-true}"
SSHD_PORT="${PHONE_WORKER_SSH_PORT:-8022}"
# Serviços pesados do perfil turbo respeitam modo seguro explícito. Auto-install
# agora é seguro/condicional: só tenta pacote ausente, com cooldown/timeout.
# Para bloquear serviços, use PHONE_WORKER_SAFE_MODE=true ou START_* = off.
# Lavalink/NodeLink não fazem mais parte do worker; música usa Music Agent + yt-dlp/ffmpeg.
MUSIC_AGENT_AUTO_START="${PHONE_WORKER_START_MUSIC_AGENT:-${MUSIC_AGENT_ENABLED:-auto}}"
MUSIC_AGENT_START_COMMAND="${MUSIC_AGENT_START_COMMAND:-$WORKER_DIR/start-phone-music-agent.sh}"
MAINT_LOCK_DIR="${PHONE_WORKER_MAINT_LOCK_DIR:-$WORKER_DIR/.phone-worker-maintenance.lock}"
MAINT_LOG_FILE="${PHONE_WORKER_MAINT_LOG_FILE:-$WORKER_DIR/phone-worker-maintenance.log}"
DEPS_STATE_DIR="${PHONE_WORKER_DEPS_STATE_DIR:-$WORKER_DIR/.dependency-install}"
APK_BUILDER_ENV_CHANGED=0
FORCE_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --force-restart|--restart) FORCE_RESTART=1 ;;
  esac
done

log() {
  printf '[phone-worker-start] %s\n' "$*"
}

truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

falsey() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  [[ "$value" == "0" || "$value" == "false" || "$value" == "no" || "$value" == "n" || "$value" == "off" || "$value" == "nao" || "$value" == "não" ]]
}

normalize_boolish() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  printf '%s' "$value"
}

safe_mode_enabled() {
  truthy "${PHONE_WORKER_SAFE_MODE:-${PHONE_WORKER_BASIC_ONLY:-${PHONE_WORKER_LIGHT_MODE:-false}}}" && return 0
  truthy "${PHONE_WORKER_DISABLE_HEAVY_SERVICES:-false}" && return 0
  return 1
}

autostart_enabled() {
  local raw="$(normalize_boolish "${1:-}")"
  case "$raw" in
    1|true|yes|y|on|sim) return 0 ;;
    0|false|no|n|off|nao|não) return 1 ;;
    auto|"") safe_mode_enabled && return 1; return 0 ;;
    *) safe_mode_enabled && return 1; return 0 ;;
  esac
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

now_iso() {
  date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

mkdir -p "$WORKER_DIR" "$RUNTIME_STATE_DIR"

lock_owner_alive() {
  local owner="" cmdline=""
  [[ -f "$LOCK_DIR/owner.pid" ]] && owner="$(head -n 1 "$LOCK_DIR/owner.pid" 2>/dev/null || true)"
  [[ "$owner" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$owner" 2>/dev/null || return 1
  [[ -r "/proc/$owner/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$owner/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"start-phone-worker.sh"* ]]
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if lock_owner_alive; then
    log "start já em andamento; lock pertence a processo vivo"
    exit 0
  fi
  log "lock órfão confirmado; recuperando"
  rm -rf "$LOCK_DIR" 2>/dev/null || exit 1
  mkdir "$LOCK_DIR" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$LOCK_DIR/owner.pid"
trap 'if [[ -f "$LOCK_DIR/owner.pid" ]] && [[ "$(cat "$LOCK_DIR/owner.pid" 2>/dev/null)" == "$$" ]]; then rm -rf "$LOCK_DIR" 2>/dev/null || true; fi' EXIT INT TERM

termux-wake-lock 2>/dev/null || true
ensure_sshd_running

active_release_dir() {
  if [[ -L "$ACTIVE_RELEASE_LINK" || -d "$ACTIVE_RELEASE_LINK" ]]; then
    local resolved
    resolved="$(cd "$ACTIVE_RELEASE_LINK" 2>/dev/null && pwd -P || true)"
    if [[ -n "$resolved" && -f "$resolved/phone_worker.py" ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  fi
  printf '%s\n' "$WORKER_DIR"
}

pid_is_official_worker() {
  local pid="${1:-}" cmdline="" cwd="" release=""
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ "$pid" != "1" && "$pid" != "$$" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"python"* && "$cmdline" == *"phone_worker.py"* ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  release="$(active_release_dir)"
  # Processos de releases anteriores continuam sendo nossos. Antes, o
  # supervisor aceitava apenas o `current`, então agents antigos podiam ficar
  # vivos segurando 8766/8768 depois de uma promoção transacional.
  [[ "$cwd" == "$WORKER_DIR" \
    || "$cwd" == "$release" \
    || "$cwd" == "$RUNTIME_ROOT"/releases/* \
    || "$cwd" == "$WORKER_DIR"/.releases/* ]]
}

pid_from_file() {
  local pid=""
  [[ -f "$PID_FILE" ]] && pid="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
  if pid_is_official_worker "$pid"; then printf '%s\n' "$pid"; fi
}

list_worker_pids() {
  local proc pid
  for proc in /proc/[0-9]*; do
    [[ -d "$proc" ]] || continue
    pid="${proc##*/}"
    pid_is_official_worker "$pid" && printf '%s\n' "$pid"
  done | awk 'NF && !seen[$0]++'
}

worker_pid_count() { list_worker_pids | awk 'NF {c++} END {print c+0}'; }

kill_worker_processes() {
  local pid
  while read -r pid; do
    pid_is_official_worker "$pid" || continue
    log "encerrando phone-worker confirmado pid=$pid"
    kill "$pid" 2>/dev/null || true
  done < <(list_worker_pids)
  sleep 1
  while read -r pid; do
    pid_is_official_worker "$pid" || continue
    kill -9 "$pid" 2>/dev/null || true
  done < <(list_worker_pids)
}

health_json_on_port() {
  local port="${1:-}"
  [[ "$port" =~ ^[0-9]+$ ]] || return 0
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 3 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$port/health" 2>/dev/null || true
  else
    curl --max-time 3 -fsS "http://127.0.0.1:$port/health" 2>/dev/null || true
  fi
}

strict_health_for_pid() {
  local pid="$1" port="$2" expected_worker="${CORE_WORKER_ID:-${CORE_WORKER_WORKER_ID:-}}"
  pid_is_official_worker "$pid" || return 1
  health_json_on_port "$port" | "$PYTHON_BIN" -c 'import json,sys,time
pid=int(sys.argv[1]); expected=sys.argv[2].strip()
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(1)
kind=str(d.get("runtime_kind") or "").lower(); source=str(d.get("source") or ""); mode=str(d.get("runtime_mode") or "")
wid=str(d.get("worker_id") or d.get("physical_worker_id") or "")
try: got_pid=int(d.get("pid") or -1); updated=float(d.get("status_updated_at") or time.time())
except Exception: raise SystemExit(1)
version=str(d.get("version") or ""); source_hash=str(d.get("source_hash") or "")
ok=(kind=="termux" and mode=="termux" and source=="termux-phone-worker" and got_pid==pid and bool(version) and len(source_hash)==64 and abs(time.time()-updated)<=45)
if expected: ok=ok and wid==expected
raise SystemExit(0 if ok else 1)' "$pid" "$expected_worker"
}

runtime_status_for_pid() {
  local pid="$1" expected_worker="${CORE_WORKER_ID:-${CORE_WORKER_WORKER_ID:-}}"
  pid_is_official_worker "$pid" || return 1
  [[ -s "$RUNTIME_STATUS_JSON" ]] || return 1
  "$PYTHON_BIN" - "$RUNTIME_STATUS_JSON" "$pid" "$expected_worker" <<'PYSTATUS'
import json,sys,time
p,pid,expected=sys.argv[1],int(sys.argv[2]),sys.argv[3].strip()
try: d=json.load(open(p,encoding='utf-8'))
except Exception: raise SystemExit(1)
try: got_pid=int(d.get('pid') or -1); updated=float(d.get('updated_at') or 0); hb=float(d.get('last_heartbeat_ok_at') or 0)
except Exception: raise SystemExit(1)
ok=(d.get('runtime_kind')=='termux' and d.get('runtime_mode')=='termux' and d.get('source')=='termux-phone-worker' and got_pid==pid and d.get('control_plane_alive') is True and time.time()-updated<=45 and bool(d.get('version')) and len(str(d.get('source_hash') or ''))==64)
if expected: ok=ok and str(d.get('worker_id') or '')==expected
# No modo sem HTTP, uma conexão de saída recente é a prova de control plane real.
if not d.get('http_port'): ok=ok and hb>0 and time.time()-hb<=180
raise SystemExit(0 if ok else 1)
PYSTATUS
}

runtime_http_port() {
  [[ -s "$RUNTIME_STATUS_JSON" ]] || return 0
  "$PYTHON_BIN" - "$RUNTIME_STATUS_JSON" <<'PYPORT' 2>/dev/null || true
import json,sys
try: d=json.load(open(sys.argv[1],encoding='utf-8')); p=d.get('http_port'); print(int(p) if p else '')
except Exception: pass
PYPORT
}

worker_healthy_for_pid() {
  local pid="$1" port=""
  runtime_status_for_pid "$pid" && return 0
  port="$(runtime_http_port)"
  [[ -n "$port" ]] && strict_health_for_pid "$pid" "$port" && return 0
  strict_health_for_pid "$pid" "$PORT"
}

running_version_for_pid() {
  local pid="$1"
  [[ -s "$RUNTIME_STATUS_JSON" ]] || return 0
  "$PYTHON_BIN" - "$RUNTIME_STATUS_JSON" "$pid" <<'PYVER' 2>/dev/null || true
import json,sys
try:
 d=json.load(open(sys.argv[1],encoding='utf-8'))
 if int(d.get('pid') or -1)==int(sys.argv[2]): print(str(d.get('version') or ''))
except Exception: pass
PYVER
}

file_version() {
  local release="$(active_release_dir)"
  "$PYTHON_BIN" - "$release/phone_worker.py" <<'PYVER' 2>/dev/null || true
import re, sys
try: text=open(sys.argv[1], encoding='utf-8', errors='ignore').read()
except Exception: text=''
m=re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
print(m.group(1) if m else '')
PYVER
}

version_lt() {
  "$PYTHON_BIN" - "$1" "$2" <<'PYVERCMP' 2>/dev/null
import re, sys
def parts(v):
    xs=[int(x) for x in re.findall(r"\d+", v or "")[:4]]
    return tuple(xs or [0])
sys.exit(0 if parts(sys.argv[1]) < parts(sys.argv[2]) else 1)
PYVERCMP
}

rotate_log_if_needed() {
  mkdir -p "$(dirname "$LOG_FILE")"
  if [[ -f "$LOG_FILE" ]]; then
    size=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$MAX_LOG_BYTES" ]]; then
      mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
      : > "$LOG_FILE"
    fi
  fi
}

write_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '%s\n' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

if ! command -v curl >/dev/null 2>&1; then
  log "curl não encontrado. Rode: pkg install curl -y"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  log "python não encontrado. Rode: pkg install python -y"
  exit 1
fi
if [[ ! -f "$WORKER_DIR/phone_worker.py" ]]; then
  log "phone_worker.py não encontrado em $WORKER_DIR"
  exit 1
fi

upsert_env_value() {
  local key="$1"
  local value="$2"
  mkdir -p "$(dirname "$ENV_FILE")"
  if [[ -f "$ENV_FILE" ]] && grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local tmp="${ENV_FILE}.tmp.$$"
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} $0 ~ "^" k "=" {print k "=" v; done=1; next} {print} END{if(!done) print k "=" v}' "$ENV_FILE" > "$tmp" && mv -f "$tmp" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  export "$key=$value"
}

append_csv_env_value() {
  local key="$1"
  local item="$2"
  local current="${!key:-}"
  local normalized
  normalized="$(printf '%s' "$current" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -Fx "$item" || true)"
  if [[ -n "$normalized" ]]; then
    return 0
  fi
  if [[ -n "$current" ]]; then
    upsert_env_value "$key" "${current},${item}"
  else
    upsert_env_value "$key" "$item"
  fi
}

csv_env_has() {
  local value="${1:-}"
  local item="${2:-}"
  printf '%s' "$value" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -Fxq "$item"
}

apk_builder_toolchain_ready() {
  local cmd
  for cmd in java javac jar gradle aapt2; do
    command -v "$cmd" >/dev/null 2>&1 || return 1
  done
  local sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/android-sdk}}"
  [[ -s "$sdk/platforms/android-34/android.jar" ]]
}

ensure_apk_builder_env_if_ready() {
  truthy "${PHONE_WORKER_APK_BUILD_ENABLED:-true}" || return 0
  truthy "${PHONE_WORKER_AUTO_APK_BUILDER_CAPABILITY:-true}" || return 0
  safe_mode_enabled && return 0
  apk_builder_toolchain_ready || return 0
  if csv_env_has "${CORE_WORKER_ROLES:-}" apk-builder && csv_env_has "${CORE_WORKER_CAPABILITIES:-}" apk-builder; then
    return 0
  fi
  append_csv_env_value CORE_WORKER_ROLES apk-builder
  append_csv_env_value CORE_WORKER_CAPABILITIES apk-builder
  APK_BUILDER_ENV_CHANGED=1
  log "toolchain Android validado; capacidade apk-builder reparada sem trocar o perfil atual"
}

ensure_music_worker_env_if_needed() {
  is_turbo_profile || return 0
  for role in music music-ytdlp music-agent; do
    append_csv_env_value CORE_WORKER_ROLES "$role"
  done
  for capability in music music-ytdlp music-ytdlp-resolve music-agent music-agent-control music-voice; do
    append_csv_env_value CORE_WORKER_CAPABILITIES "$capability"
  done
  local cookies="${PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE:-${MUSIC_WORKER_YTDLP_COOKIES_FILE:-}}"
  if [[ -z "$cookies" ]]; then
    cookies="$WORKER_DIR/secrets/youtube-cookies.txt"
  fi
  if [[ -s "$cookies" ]]; then
    upsert_env_value PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE "$cookies"
    upsert_env_value MUSIC_WORKER_YTDLP_COOKIES_FILE "$cookies"
    log "perfil turbo: cookies yt-dlp do worker configurados"
  else
    mkdir -p "$(dirname "$cookies")" 2>/dev/null || true
    log "perfil turbo: cookies yt-dlp do worker não encontrados em $cookies; worker tentará sem cookies"
  fi
  upsert_env_value PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES "${PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES:-node}"
  upsert_env_value MUSIC_WORKER_YTDLP_JS_RUNTIMES "${MUSIC_WORKER_YTDLP_JS_RUNTIMES:-node}"
  upsert_env_value PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH "${PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH:-ytsearch}"
  upsert_env_value MUSIC_WORKER_YTDLP_DEFAULT_SEARCH "${MUSIC_WORKER_YTDLP_DEFAULT_SEARCH:-ytsearch}"
}

is_turbo_profile() {
  local profile="${CORE_WORKER_PROFILE:-${PHONE_WORKER_PROFILE:-}}"
  profile="$(printf '%s' "$profile" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$profile" == "turbo" ]]
}

deps_install_enabled() {
  safe_mode_enabled && return 1
  # *_MODE=off foi usado como freio de emergência contra loops pesados. A partir
  # deste patch, off/manual continuam permitindo instalações leves e idempotentes
  # somente quando o pacote está ausente. Para bloquear tudo use disabled/never.
  local mode="${PHONE_WORKER_TURBO_DEPS_INSTALL_MODE:-${PHONE_WORKER_DEPS_INSTALL_MODE:-${PHONE_WORKER_TURBO_DEPS_INSTALL:-${PHONE_WORKER_TTS_DEPS_INSTALL:-safe}}}}"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$mode" == "disabled" || "$mode" == "disable" || "$mode" == "never" || "$mode" == "none" || "$mode" == "bloqueado" ]] && return 1
  return 0
}

heavy_python_deps_enabled() {
  local mode="${PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL:-manual}"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$mode" == "1" || "$mode" == "true" || "$mode" == "on" || "$mode" == "yes" || "$mode" == "sim" ]]
}

pip_source_builds_enabled() {
  truthy "${PHONE_WORKER_ALLOW_PIP_SOURCE_BUILDS:-false}"
}



install_attempt_allowed() {
  local key="$1"
  local cooldown="${2:-900}"
  mkdir -p "$DEPS_STATE_DIR" 2>/dev/null || true
  local safe_key
  safe_key="$(printf '%s' "$key" | tr -c 'A-Za-z0-9_.-' '_')"
  local file="$DEPS_STATE_DIR/$safe_key.last"
  local now last
  now="$(date +%s 2>/dev/null || echo 0)"
  last="0"
  [[ -f "$file" ]] && last="$(cat "$file" 2>/dev/null || echo 0)"
  if [[ "$last" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ && $((now - last)) -lt "$cooldown" ]]; then
    log "auto-install em cooldown: $key"
    return 1
  fi
  printf '%s' "$now" > "$file" 2>/dev/null || true
  return 0
}

python_module_ok() {
  local module="$1"
  "$PYTHON_BIN" - "$module" <<'PYMODCHECK' >/dev/null 2>&1
import importlib, importlib.metadata, sys
importlib.import_module(sys.argv[1])
pins = {"edge_tts": ("edge-tts", "7.2.8"), "gtts": ("gTTS", "2.5.4")}
if sys.argv[1] in pins:
    package, required = pins[sys.argv[1]]
    if importlib.metadata.version(package) != required:
        raise RuntimeError("versão TTS precisa ser alinhada à VPS")
PYMODCHECK
}

safe_pip_install_module() {
  local label="$1"
  local module="$2"
  local package="$3"
  local kind="${4:-light}"
  python_module_ok "$module" && return 0
  deps_install_enabled || { log "dependência ausente: $label; auto-install seguro desativado"; return 1; }
  if [[ "$kind" == "heavy" ]] && ! heavy_python_deps_enabled; then
    log "dependência pesada opcional ausente: $label; não instalando sem opt-in explícito"
    return 1
  fi
  local cooldown timeout
  cooldown="${PHONE_WORKER_DEPS_INSTALL_COOLDOWN_SECONDS:-900}"
  timeout="${PHONE_WORKER_DEPS_INSTALL_TIMEOUT_SECONDS:-240}"
  [[ "$kind" == "heavy" ]] && timeout="${PHONE_WORKER_HEAVY_DEPS_INSTALL_TIMEOUT_SECONDS:-240}"
  install_attempt_allowed "pip-$label" "$cooldown" || return 1
  cleanup_stale_heavy_dependency_builds
  log "auto-install seguro: $label ausente; instalando $package"
  local pip_args=(--disable-pip-version-check --no-input install --upgrade --prefer-binary)
  if [[ "$kind" == "heavy" ]] || ! pip_source_builds_enabled; then
    pip_args+=(--only-binary=:all:)
  fi
  local cmd=("$PYTHON_BIN" -m pip "${pip_args[@]}" "$package")
  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout" "${cmd[@]}" >/dev/null 2>&1 || log "auto-install falhou/expirou para $label"
  else
    "${cmd[@]}" >/dev/null 2>&1 || log "auto-install falhou para $label"
  fi
  python_module_ok "$module"
}

safe_pkg_install_missing() {
  local label="$1"; shift
  deps_install_enabled || { log "pacote Termux ausente: $label; auto-install seguro desativado"; return 1; }
  command -v pkg >/dev/null 2>&1 || return 1
  [[ "$#" -gt 0 ]] || return 0
  local cooldown timeout
  cooldown="${PHONE_WORKER_DEPS_INSTALL_COOLDOWN_SECONDS:-900}"
  timeout="${PHONE_WORKER_TERMUX_DEPS_INSTALL_TIMEOUT_SECONDS:-180}"
  install_attempt_allowed "pkg-$label" "$cooldown" || return 1
  log "auto-install seguro: pacote(s) Termux ausentes para $label: $*"
  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout" pkg install -y "$@" >/dev/null 2>&1 || log "não consegui instalar pacote(s) Termux para $label dentro do timeout"
  else
    pkg install -y "$@" >/dev/null 2>&1 || log "não consegui instalar pacote(s) Termux para $label"
  fi
}

cleanup_stale_heavy_dependency_builds() {
  truthy "${PHONE_WORKER_KILL_STALE_HEAVY_DEP_BUILDS:-true}" || return 0
  heavy_python_deps_enabled && return 0
  command -v ps >/dev/null 2>&1 || return 0
  local pattern='pyb/temp\.android|pip/_vendor|pyproject_hooks|build_wheel|python -m pip install|pip install|aarch64-linux-android-clang|clang\+\+'
  local round pid
  for round in 1 2 3; do
    ps -ef 2>/dev/null | grep -Ei "$pattern" | grep -v grep | grep -v 'phone_worker.py' | awk '{print $2}' | while read -r pid; do
      case "$pid" in ''|*[!0-9]*) continue ;; esac
      [[ "$pid" == "$$" ]] && continue
      log "encerrando build pesado opcional preso; pid=$pid"
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  done
}

cleanup_heavy_services_for_safe_mode() {
  safe_mode_enabled || return 0
  truthy "${PHONE_WORKER_KEEP_HEAVY_SERVICES_IN_SAFE_MODE:-false}" && return 0
  command -v pkill >/dev/null 2>&1 || return 0
  log "modo seguro ativo; encerrando serviços pesados opcionais do worker"
  pkill -f '[m]usic_agent.py' 2>/dev/null || true
}


ensure_turbo_termux_packages_if_needed() {
  is_turbo_profile || return 0
  command -v pkg >/dev/null 2>&1 || return 0
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  command -v wget >/dev/null 2>&1 || missing+=(wget)
  command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v ffprobe >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v node >/dev/null 2>&1 || missing+=(nodejs)
  command -v sox >/dev/null 2>&1 || missing+=(sox)
  command -v espeak >/dev/null 2>&1 || missing+=(espeak)
  if [[ "${#missing[@]}" -gt 0 ]]; then
    mapfile -t missing < <(printf '%s\n' "${missing[@]}" | awk 'NF && !seen[$0]++')
    safe_pkg_install_missing "turbo-base" "${missing[@]}" || true
  fi
}

ensure_turbo_python_tts_deps_if_needed() {
  is_turbo_profile || return 0
  safe_pip_install_module "edge-tts" "edge_tts" "edge-tts==7.2.8" light || true
  safe_pip_install_module "gTTS" "gtts" "gTTS==2.5.4" light || true
}

ensure_music_ytdlp_deps_if_needed() {
  is_turbo_profile || return 0
  safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp[default]" light || true
  safe_pip_install_module "yt-dlp-ejs" "yt_dlp_ejs" "yt-dlp-ejs" light || true
}

ensure_music_agent_deps_if_needed() {
  is_turbo_profile || return 0
  autostart_enabled "$MUSIC_AGENT_AUTO_START" || { log "perfil turbo: Music Agent não será iniciado automaticamente (modo seguro/auto-start off)"; return 0; }
  safe_pip_install_module "aiohttp" "aiohttp" "aiohttp" light || true
  safe_pip_install_module "discord.py" "discord" "discord.py>=2.7.1,<2.8" light || true
  safe_pip_install_module "PyNaCl" "nacl" "PyNaCl" light || true
  safe_pip_install_module "davey" "davey" "davey" light || true
  safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp[default]" light || true
  safe_pip_install_module "edge-tts" "edge_tts" "edge-tts==7.2.8" light || true
  safe_pip_install_module "gTTS" "gtts" "gTTS==2.5.4" light || true
  "$PYTHON_BIN" - <<'PYMUSICAGENTCHECK' >/dev/null 2>&1 && log "perfil turbo: dependências do Music Agent prontas" || log "perfil turbo: Music Agent ainda possui dependências ausentes; será reportado no health"
import aiohttp, discord, nacl, yt_dlp, davey, edge_tts, gtts  # noqa: F401
PYMUSICAGENTCHECK
}

ensure_turbo_piper_cli_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  if command -v piper >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${PHONE_WORKER_PIPER_COMMAND:-}" && -x "${PHONE_WORKER_PIPER_COMMAND:-}" ]] && command -v piper >/dev/null 2>&1; then
    return 0
  fi
  command -v apt >/dev/null 2>&1 || return 0
  command -v wget >/dev/null 2>&1 || return 0
  log "perfil turbo: piper CLI ausente; tentando instalar pacote .deb do Piper para Termux"
  local tmpdir="${TMPDIR:-/tmp}/piper-termux-install"
  mkdir -p "$tmpdir"
  local deb_url="${PHONE_WORKER_PIPER_DEB_URL:-}"
  if [[ -z "$deb_url" ]]; then
    deb_url="$($PYTHON_BIN - <<'PYPIPERDEB' 2>/dev/null || true
import json, urllib.request
url='https://api.github.com/repos/gyroing/piper-tts-for-termux/releases/latest'
data=json.load(urllib.request.urlopen(url, timeout=20))
for a in data.get('assets') or []:
    u=str(a.get('browser_download_url') or '')
    if u.endswith('.deb'):
        print(u)
        break
PYPIPERDEB
)"
  fi
  if [[ -z "$deb_url" ]]; then
    log "não encontrei URL .deb do Piper; configure PHONE_WORKER_PIPER_DEB_URL se quiser auto-instalar"
    return 0
  fi
  wget -q -O "$tmpdir/piper-termux.deb" "$deb_url" && \
    apt install -y "$tmpdir/piper-termux.deb" >/dev/null 2>&1 || \
    log "não consegui instalar Piper .deb automaticamente"
}

ensure_turbo_piper_model_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  local auto_model="${PHONE_WORKER_PIPER_MODEL_AUTO_DOWNLOAD:-true}"
  auto_model="$(printf '%s' "$auto_model" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$auto_model" == "0" || "$auto_model" == "false" || "$auto_model" == "off" || "$auto_model" == "no" ]] && return 0
  local model="${PHONE_WORKER_PIPER_MODEL:-$HOME/piper-models/pt_BR/edresson-low/pt_BR-edresson-low.onnx}"
  local config="${PHONE_WORKER_PIPER_CONFIG:-${model}.json}"
  local model_url="${PHONE_WORKER_PIPER_MODEL_URL:-https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx?download=true}"
  local config_url="${PHONE_WORKER_PIPER_CONFIG_URL:-https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx.json?download=true}"
  command -v wget >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$model")"
  if [[ ! -s "$model" ]]; then
    log "perfil turbo: baixando modelo Piper padrão"
    wget -q -c -O "$model" "$model_url" || log "não consegui baixar modelo Piper padrão"
  fi
  if [[ ! -s "$config" ]]; then
    log "perfil turbo: baixando config do modelo Piper padrão"
    wget -q -c -O "$config" "$config_url" || log "não consegui baixar config Piper padrão"
  fi
  [[ -s "$model" ]] && upsert_env_value PHONE_WORKER_PIPER_MODEL "$model"
  [[ -s "$config" ]] && upsert_env_value PHONE_WORKER_PIPER_CONFIG "$config"
  if [[ -z "${PHONE_WORKER_PIPER_MODEL_NAME:-}" ]]; then
    upsert_env_value PHONE_WORKER_PIPER_MODEL_NAME "pt_BR-edresson-low"
  fi
}

ensure_turbo_piper_wrapper_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  local wrapper="$WORKER_DIR/bin/piper-worker"
  if [[ -x "$wrapper" ]]; then
    if [[ -z "${PHONE_WORKER_PIPER_COMMAND:-}" ]]; then
      upsert_env_value PHONE_WORKER_PIPER_COMMAND "$wrapper"
    fi
    return 0
  fi
  command -v piper >/dev/null 2>&1 || return 0
  command -v ffmpeg >/dev/null 2>&1 || return 0
  mkdir -p "$WORKER_DIR/bin"
  cat > "$wrapper" <<'PIPERWRAP'
#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
MODEL=""
CONFIG=""
OUTPUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    --config) CONFIG="${2:-}"; shift 2 ;;
    --output_file|--output-file) OUTPUT="${2:-}"; shift 2 ;;
    --) shift; break ;;
    *) shift ;;
  esac
done
if [ -z "$MODEL" ] || [ ! -s "$MODEL" ]; then echo "modelo Piper inválido: $MODEL" >&2; exit 2; fi
if [ -z "$OUTPUT" ]; then echo "output_file não informado" >&2; exit 2; fi
VOICE_DIR="$(dirname "$MODEL")"
VOICE_NAME="$(basename "$MODEL")"
VOICE_NAME="${VOICE_NAME%.onnx}"
TEXT="$(cat)"
if [ -z "${TEXT// }" ]; then echo "texto vazio" >&2; exit 2; fi
RAW="$(mktemp "$TMPDIR/piper-raw-XXXXXX.raw")"
trap 'rm -f "$RAW"' EXIT
export PIPER_VOICE_PATH="$VOICE_DIR"
piper -m "$VOICE_NAME" -f "$RAW" -- "$TEXT"
ffmpeg -y -hide_banner -loglevel error -f f32le -ar 22050 -ac 1 -i "$RAW" "$OUTPUT"
PIPERWRAP
  chmod +x "$wrapper"
  upsert_env_value PHONE_WORKER_PIPER_COMMAND "$wrapper"
  log "perfil turbo: wrapper Piper criado em $wrapper"
}


ensure_music_agent_for_turbo_if_needed() {
  is_turbo_profile || return 0
  autostart_enabled "$MUSIC_AGENT_AUTO_START" || { log "perfil turbo: Music Agent não será iniciado automaticamente (modo seguro/auto-start off)"; return 0; }
  if [[ ! -x "$MUSIC_AGENT_START_COMMAND" ]]; then
    log "perfil turbo: start do Music Agent não encontrado em $MUSIC_AGENT_START_COMMAND"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_BOT_TOKEN:-${DISCORD_TOKEN:-${BOT_TOKEN:-}}}" ]]; then
    log "perfil turbo: Music Agent habilitado, mas token do bot não está configurado no worker"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_TOKEN:-}" && -n "${PHONE_WORKER_TOKEN:-}" ]]; then
    upsert_env_value MUSIC_AGENT_TOKEN "$PHONE_WORKER_TOKEN"
  fi
  log "perfil turbo: garantindo Music Agent do worker"
  "$MUSIC_AGENT_START_COMMAND" >/dev/null 2>&1 || \
    log "não consegui iniciar Music Agent automaticamente; música direta no worker pode ficar indisponível"
}

ensure_turbo_deps_if_needed() {
  ensure_turbo_termux_packages_if_needed
  ensure_turbo_python_tts_deps_if_needed
  ensure_music_ytdlp_deps_if_needed
  ensure_music_agent_deps_if_needed
  ensure_turbo_piper_cli_if_needed
  ensure_turbo_piper_model_if_needed
  ensure_turbo_piper_wrapper_if_needed
}

run_post_start_maintenance_async() {
  is_turbo_profile || return 0
  mkdir -p "$(dirname "$MAINT_LOG_FILE")" 2>/dev/null || true
  (
    if ! mkdir "$MAINT_LOCK_DIR" 2>/dev/null; then
      log "manutenção pós-start já em andamento"
      exit 0
    fi
    trap 'rm -rf "$MAINT_LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
    log "manutenção pós-start iniciada"
    cleanup_stale_heavy_dependency_builds
    cleanup_heavy_services_for_safe_mode
    ensure_turbo_deps_if_needed
    ensure_music_agent_for_turbo_if_needed
    log "manutenção pós-start finalizada"
  ) >> "$MAINT_LOG_FILE" 2>&1 &
}

ensure_music_worker_env_if_needed
ensure_apk_builder_env_if_ready
cleanup_stale_heavy_dependency_builds
cleanup_heavy_services_for_safe_mode

count="$(worker_pid_count)"
existing_pid="$(pid_from_file)"
if [[ -n "$existing_pid" && "$count" -le 1 ]] && worker_healthy_for_pid "$existing_pid"; then
  running_ver="$(running_version_for_pid "$existing_pid")"
  file_ver="$(file_version)"
  if [[ "$FORCE_RESTART" == "1" ]]; then
    log "restart forçado solicitado; encerrando processo Termux confirmado pid=$existing_pid"
    write_status "restart_forced pid=$existing_pid $(now_iso)"
    kill_worker_processes
  elif [[ "$APK_BUILDER_ENV_CHANGED" == "1" ]]; then
    log "capacidade apk-builder alterada; reiniciando processo Termux confirmado"
    write_status "restart_for_apk_builder_capability pid=$existing_pid $(now_iso)"
    kill_worker_processes
  elif [[ -n "$running_ver" && -n "$file_ver" ]] && version_lt "$running_ver" "$file_ver"; then
    log "worker Termux confirmado está desatualizado; runtime=$running_ver arquivo=$file_ver"
    write_status "restart_for_update pid=$existing_pid runtime=$running_ver file=$file_ver $(now_iso)"
    kill_worker_processes
  else
    run_post_start_maintenance_async
    log "worker Termux já saudável; pid=$existing_pid"
    write_status "ok already_online pid=$existing_pid $(now_iso)"
    exit 0
  fi
fi

# Duplicatas só são encerradas depois de confirmação por /proc/cmdline + cwd.
if [[ "$KILL_DUPLICATES" != "false" && "$(worker_pid_count)" -gt 0 ]]; then
  log "limpando somente processos phone-worker confirmados"
  kill_worker_processes
fi

rm -f "$PID_FILE" 2>/dev/null || true
rotate_log_if_needed
ACTIVE_DIR="$(active_release_dir)"
if [[ ! -f "$ACTIVE_DIR/phone_worker.py" ]]; then
  log "phone_worker.py oficial não encontrado em $ACTIVE_DIR"
  exit 1
fi

log "iniciando Termux control plane; release=$ACTIVE_DIR porta_preferida=$PORT"
(
  cd "$ACTIVE_DIR" || exit 1
  export PHONE_WORKER_RELEASE_DIR="$ACTIVE_DIR"
  exec "$PYTHON_BIN" phone_worker.py --host "$HOST" --port "$PORT"
) >> "$LOG_FILE" 2>&1 &
child_pid=$!
printf '%s\n' "$child_pid" > "$PID_FILE" 2>/dev/null || true
write_status "starting pid=$child_pid release=$ACTIVE_DIR $(now_iso)"

sleep "$START_WAIT"

if ! kill -0 "$child_pid" 2>/dev/null || ! pid_is_official_worker "$child_pid"; then
  log "processo recém-iniciado morreu ou não corresponde ao agent oficial; nenhuma resposta de outra porta será aceita"
  write_status "failed child_dead_or_identity_mismatch pid=$child_pid $(now_iso)"
  rm -f "$PID_FILE" 2>/dev/null || true
  exit 1
fi

if worker_healthy_for_pid "$child_pid"; then
  effective_port="$(runtime_http_port)"
  log "worker Termux iniciado e validado; pid=$child_pid http_port=${effective_port:-none}"
  write_status "ok pid=$child_pid http_port=${effective_port:-none} $(now_iso)"
  run_post_start_maintenance_async
  exit 0
fi

# O filho ainda está vivo, mas o supervisor não conseguiu provar identidade +
# control plane dentro da janela de start. Nunca devolva sucesso só porque o PID
# existe: isso permitiria outro processo/HTTP mascarar um agent quebrado.
log "processo Termux vivo, mas identidade/control-plane não foram confirmados dentro da janela; pid=$child_pid"
write_status "failed verification_timeout pid=$child_pid $(now_iso)"
exit 1

