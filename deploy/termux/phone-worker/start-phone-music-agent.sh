#!/data/data/com.termux/files/usr/bin/bash
# Supervisor simples do Music Agent do phone worker.
set -u

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
MUSIC_AGENT_ENV_FILE="${MUSIC_AGENT_ENV:-$WORKER_DIR/secrets/music-agent.env}"
if [[ -f "$MUSIC_AGENT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MUSIC_AGENT_ENV_FILE"
  set +a
fi
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
HOST="${MUSIC_AGENT_HOST:-127.0.0.1}"
PORT="${MUSIC_AGENT_PORT:-8780}"
TOKEN="${MUSIC_AGENT_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  mkdir -p "$(dirname "$MUSIC_AGENT_ENV_FILE")"
  if command -v python >/dev/null 2>&1; then
    TOKEN="$(python - <<'PYTOKEN'
import secrets
print(secrets.token_urlsafe(32))
PYTOKEN
)"
  else
    TOKEN="$(date +%s%N)-$RANDOM-$RANDOM"
  fi
  if grep -qE '^MUSIC_AGENT_TOKEN=' "$MUSIC_AGENT_ENV_FILE" 2>/dev/null; then
    tmp_file="${MUSIC_AGENT_ENV_FILE}.tmp"
    sed -E "s|^MUSIC_AGENT_TOKEN=.*|MUSIC_AGENT_TOKEN=$TOKEN|" "$MUSIC_AGENT_ENV_FILE" > "$tmp_file" && mv "$tmp_file" "$MUSIC_AGENT_ENV_FILE"
  else
    printf '\nMUSIC_AGENT_TOKEN=%s\n' "$TOKEN" >> "$MUSIC_AGENT_ENV_FILE"
  fi
  chmod 600 "$MUSIC_AGENT_ENV_FILE" 2>/dev/null || true
  export MUSIC_AGENT_TOKEN="$TOKEN"
fi
LOG_FILE="${MUSIC_AGENT_LOG_FILE:-$WORKER_DIR/music_agent.log}"
PID_FILE="${MUSIC_AGENT_PID_FILE:-$WORKER_DIR/music_agent.pid}"
START_WAIT="${MUSIC_AGENT_START_WAIT_SECONDS:-5}"
KILL_DUPLICATES="${MUSIC_AGENT_KILL_DUPLICATES:-true}"
DEPS_STATE_DIR="${MUSIC_AGENT_DEPS_STATE_DIR:-$WORKER_DIR/.dependency-install}"

log() { printf '[music-agent-start] %s\n' "$*"; }
truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

falsey() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$value" == "0" || "$value" == "false" || "$value" == "no" || "$value" == "n" || "$value" == "off" || "$value" == "nao" || "$value" == "não" ]]
}

agent_safe_mode_enabled() {
  truthy "${MUSIC_AGENT_SAFE_MODE:-${PHONE_WORKER_SAFE_MODE:-${PHONE_WORKER_BASIC_ONLY:-${PHONE_WORKER_LIGHT_MODE:-false}}}}" && return 0
  truthy "${PHONE_WORKER_DISABLE_HEAVY_SERVICES:-false}" && return 0
  return 1
}

agent_deps_install_enabled() {
  agent_safe_mode_enabled && return 1
  local mode="${MUSIC_AGENT_DEPS_INSTALL_MODE:-${PHONE_WORKER_TURBO_DEPS_INSTALL_MODE:-${PHONE_WORKER_DEPS_INSTALL_MODE:-safe}}}"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$mode" == "disabled" || "$mode" == "disable" || "$mode" == "never" || "$mode" == "none" || "$mode" == "bloqueado" ]] && return 1
  return 0
}

pip_source_builds_enabled() {
  truthy "${MUSIC_AGENT_ALLOW_PIP_SOURCE_BUILDS:-${PHONE_WORKER_ALLOW_PIP_SOURCE_BUILDS:-false}}"
}



install_attempt_allowed() {
  local key="$1"
  local cooldown="${2:-900}"
  mkdir -p "$DEPS_STATE_DIR" 2>/dev/null || true
  local safe_key file now last
  safe_key="$(printf '%s' "$key" | tr -c 'A-Za-z0-9_.-' '_')"
  file="$DEPS_STATE_DIR/$safe_key.last"
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

safe_pkg_install_missing() {
  local label="$1"; shift
  agent_deps_install_enabled || { log "pacote Termux ausente: $label; auto-install seguro desativado"; return 1; }
  command -v pkg >/dev/null 2>&1 || return 1
  [[ "$#" -gt 0 ]] || return 0
  local cooldown timeout
  cooldown="${MUSIC_AGENT_DEPS_INSTALL_COOLDOWN_SECONDS:-${PHONE_WORKER_DEPS_INSTALL_COOLDOWN_SECONDS:-900}}"
  timeout="${MUSIC_AGENT_TERMUX_DEPS_INSTALL_TIMEOUT_SECONDS:-${PHONE_WORKER_TERMUX_DEPS_INSTALL_TIMEOUT_SECONDS:-180}}"
  install_attempt_allowed "music-agent-pkg-$label" "$cooldown" || return 1
  log "auto-install seguro: pacote(s) Termux ausentes para $label: $*"
  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout" pkg install -y "$@" >/dev/null 2>&1 || log "pkg install falhou/expirou para $label"
  else
    pkg install -y "$@" >/dev/null 2>&1 || log "pkg install falhou para $label"
  fi
}

safe_pip_install_module() {
  local label="$1"
  local module="$2"
  local package="$3"
  local kind="${4:-light}"
  python_module_ok "$module" && return 0
  agent_deps_install_enabled || { log "dependência ausente: $label; auto-install seguro desativado"; return 1; }
  if [[ "$kind" == "heavy" ]] && ! heavy_python_deps_enabled; then
    log "dependência pesada opcional ausente: $label; não instalando sem opt-in explícito"
    return 1
  fi
  local timeout cooldown
  timeout="${MUSIC_AGENT_DEPS_INSTALL_TIMEOUT_SECONDS:-240}"
  [[ "$kind" == "heavy" ]] && timeout="${MUSIC_AGENT_HEAVY_DEPS_INSTALL_TIMEOUT_SECONDS:-240}"
  cooldown="${MUSIC_AGENT_DEPS_INSTALL_COOLDOWN_SECONDS:-900}"
  install_attempt_allowed "music-agent-pip-$label" "$cooldown" || return 1
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

heavy_python_deps_enabled() {
  local mode="${MUSIC_AGENT_HEAVY_PYTHON_DEPS_INSTALL:-${PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL:-false}}"
  truthy "$mode"
}

cleanup_stale_heavy_dependency_builds() {
  truthy "${MUSIC_AGENT_KILL_STALE_HEAVY_DEP_BUILDS:-${PHONE_WORKER_KILL_STALE_HEAVY_DEP_BUILDS:-true}}" || return 0
  heavy_python_deps_enabled && return 0
  command -v ps >/dev/null 2>&1 || return 0
  local pattern='pyb/temp\.android|pip/_vendor|pyproject_hooks|build_wheel|python -m pip install|pip install|aarch64-linux-android-clang|clang\+\+'
  local round pid
  for round in 1 2 3; do
    ps -ef 2>/dev/null | grep -Ei "$pattern" | grep -v grep | grep -v 'music_agent.py' | awk '{print $2}' | while read -r pid; do
      case "$pid" in ''|*[!0-9]*) continue ;; esac
      [[ "$pid" == "$$" ]] && continue
      log "encerrando build pesado opcional preso; pid=$pid"
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  done
}

cleanup_agent_for_safe_mode() {
  agent_safe_mode_enabled || return 0
  log "modo seguro ativo; encerrando Music Agent se estiver rodando"
  pkill -f '[m]usic_agent.py' 2>/dev/null || true
}


if [[ ! -f "$WORKER_DIR/music_agent.py" ]]; then
  log "music_agent.py não encontrado em $WORKER_DIR"
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  log "python não encontrado"
  exit 1
fi

health_ok() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  fi
}

health_json() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" 2>/dev/null || true
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || true
  fi
}

running_version() {
  health_json | "$PYTHON_BIN" -c 'import json,sys;
try:
 data=json.load(sys.stdin); print(str(data.get("version") or ""))
except Exception: pass' 2>/dev/null || true
}

file_version() {
  "$PYTHON_BIN" - "$WORKER_DIR/music_agent.py" <<'PYVER' 2>/dev/null || true
import re, sys
try:
    text=open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception:
    text=""
m=re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
print(m.group(1) if m else "")
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

list_pids() {
  pgrep -f 'music_agent.py' 2>/dev/null || true
}

kill_agent() {
  list_pids | while read -r pid; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [[ "$pid" == "$$" ]] && continue
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  list_pids | while read -r pid; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [[ "$pid" == "$$" ]] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
}

ensure_termux_packages() {
  truthy "${MUSIC_AGENT_AUTO_INSTALL_TERMUX_PACKAGES:-true}" || return 0
  agent_deps_install_enabled || return 0
  command -v pkg >/dev/null 2>&1 || return 0
  local missing=()
  command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v pkg-config >/dev/null 2>&1 || missing+=(pkg-config)
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  mapfile -t missing < <(printf '%s\n' "${missing[@]}" | awk 'NF && !seen[$0]++')
  install_attempt_allowed "music-agent-pkg-base" "${MUSIC_AGENT_DEPS_INSTALL_COOLDOWN_SECONDS:-900}" || return 0
  log "pacotes Termux ausentes (${missing[*]}); instalando apenas o necessário"
  local timeout="${MUSIC_AGENT_TERMUX_DEPS_INSTALL_TIMEOUT_SECONDS:-180}"
  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout" pkg install -y "${missing[@]}" >/dev/null 2>&1 || log "não consegui instalar todos os pacotes Termux dentro do timeout"
  else
    pkg install -y "${missing[@]}" >/dev/null 2>&1 || log "não consegui instalar todos os pacotes Termux automaticamente"
  fi
}

ensure_deps() {
  ensure_termux_packages
  local missing=0
  safe_pip_install_module "aiohttp" "aiohttp" "aiohttp" light || missing=1
  safe_pip_install_module "discord.py" "discord" "discord.py>=2.7.1,<2.8" light || missing=1
  safe_pip_install_module "PyNaCl" "nacl" "PyNaCl" light || missing=1
  safe_pip_install_module "davey" "davey" "davey" light || missing=1
  safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp[default]" light || missing=1
  safe_pip_install_module "gTTS" "gtts" "gTTS==2.5.4" light || true
  safe_pip_install_module "edge-tts" "edge_tts" "edge-tts==7.2.8" light || true
  if "$PYTHON_BIN" - <<'PYDEPS' >/dev/null 2>&1; then
import aiohttp, discord, nacl, davey, yt_dlp  # noqa: F401
PYDEPS
    return 0
  fi
  log "dependências críticas do Music Agent ainda ausentes; não iniciando para evitar loop de crash"
  return 1
}

mkdir -p "$(dirname "$LOG_FILE")"
cleanup_stale_heavy_dependency_builds
cleanup_agent_for_safe_mode

if agent_safe_mode_enabled; then
  log "modo seguro ativo; Music Agent não será iniciado automaticamente"
  exit 0
fi

if health_ok; then
  running_ver="$(running_version)"
  file_ver="$(file_version)"
  if [[ -n "$running_ver" && -n "$file_ver" ]] && version_lt "$running_ver" "$file_ver"; then
    log "Music Agent online está desatualizado; runtime=$running_ver arquivo=$file_ver; reiniciando"
    kill_agent
  else
    log "Music Agent já está online em $HOST:$PORT versão=${running_ver:-?}"
    exit 0
  fi
fi

# Só verifica/instala dependências quando o Music Agent não está online ou será reiniciado.
# Se faltar algo crítico, sai sem iniciar para evitar loop de crash e aquecimento.
if ! ensure_deps; then
  exit 0
fi

if truthy "$KILL_DUPLICATES"; then
  kill_agent
fi

log "iniciando Music Agent em $HOST:$PORT"
(
  cd "$WORKER_DIR" || exit 1
  exec "$PYTHON_BIN" music_agent.py
) >> "$LOG_FILE" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE" 2>/dev/null || true
sleep "$START_WAIT"

if health_ok; then
  log "Music Agent iniciado com sucesso; pid=$pid"
  exit 0
fi
if kill -0 "$pid" 2>/dev/null; then
  log "Music Agent iniciou, mas health ainda não respondeu; veja $LOG_FILE"
  exit 0
fi
log "falha ao iniciar Music Agent; veja: tail -80 '$LOG_FILE'"
exit 1
