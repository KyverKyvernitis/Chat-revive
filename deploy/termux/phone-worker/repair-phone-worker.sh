#!/data/data/com.termux/files/usr/bin/bash
# Resgate idempotente de uma etapa para a migração ao bootstrap persistente.
# Não imprime tokens, não mata processos desconhecidos e deixa rollback ao bootstrap.
set -Eeuo pipefail

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
BOOTSTRAP="$WORKER_DIR/phone_worker_bootstrap.py"
START_SCRIPT="$WORKER_DIR/start-phone-worker.sh"
STATE_DIR="${PHONE_WORKER_STATE_DIR:-$HOME/.local/state/core-worker-phone-worker}"

log(){ printf '[phone-worker-repair] %s\n' "$*"; }

if [[ ! -f "$ENV_FILE" ]]; then log "env ausente: $ENV_FILE"; exit 2; fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
for key in CORE_WORKER_VPS_URL CORE_WORKER_ID CORE_WORKER_TOKEN; do
  if [[ -z "${!key:-}" ]]; then log "configuração bloqueada: $key ausente"; exit 3; fi
done
command -v "$PYTHON_BIN" >/dev/null 2>&1 || { log "python ausente"; exit 4; }
[[ -f "$BOOTSTRAP" ]] || { log "bootstrap ausente em $BOOTSTRAP; sincronize este patch uma vez antes do resgate"; exit 5; }
[[ -x "$START_SCRIPT" ]] || chmod +x "$START_SCRIPT" 2>/dev/null || true

log "env válido; worker=${CORE_WORKER_ID}; token=presente (oculto)"
for port in 8766 8767 8768; do
  owner="livre"
  if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -Eq ":${port}([[:space:]]|$)"; then
    owner="listener_presente"
    # Identificação é best-effort e nunca vira critério para matar algo.
    body="$(curl --max-time 1 -fsS "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [[ -n "$body" ]]; then
      kind="$(printf '%s' "$body" | "$PYTHON_BIN" -c 'import json,sys; data=json.load(sys.stdin); print(str(data.get("runtime_kind") or "unknown"))' 2>/dev/null || echo unknown)"
      owner="health_${kind}"
    fi
  fi
  log "porta $port: $owner"
done

mkdir -p "$STATE_DIR"
log "consultando manifesto persistente e aplicando release transacional"
if ! "$PYTHON_BIN" "$BOOTSTRAP" --check --force; then
  log "bootstrap falhou; a release anterior foi preservada/rollback será refletido no estado"
  "$PYTHON_BIN" "$BOOTSTRAP" --status 2>/dev/null || true
  exit 6
fi

log "garantindo supervisor"
bash "$START_SCRIPT" || { log "supervisor não confirmou o runtime"; "$PYTHON_BIN" "$BOOTSTRAP" --status 2>/dev/null || true; exit 7; }

summary="$($PYTHON_BIN "$BOOTSTRAP" --status 2>/dev/null || true)"
if [[ -n "$summary" ]]; then
  printf '%s\n' "$summary" | "$PYTHON_BIN" -c 'import json,sys
try:
 d=json.load(sys.stdin)
 print("[phone-worker-repair] estado=%s target=%s hash=%s" % (d.get("state","unknown"), d.get("target_version","?"), str(d.get("target_source_hash") or "")[:12]))
except Exception: pass'
fi
log "resgate concluído; futuras atualizações passam pelo pull persistente"
