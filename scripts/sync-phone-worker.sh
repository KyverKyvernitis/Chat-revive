#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() {
  printf '[phone-worker-sync] %s\n' "$*"
}

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

PHONE_HOST="${PHONE_WORKER_HOST:-${PHONE_LAVALINK_HOST:-}}"
PHONE_USER="${PHONE_WORKER_SSH_USER:-}"
PHONE_PORT="${PHONE_WORKER_SSH_PORT:-8022}"
PHONE_START_COMMAND="${PHONE_WORKER_START_COMMAND:-/data/data/com.termux/files/home/phone-worker/start-phone-worker.sh}"

if [ -z "$PHONE_HOST" ] || [ -z "$PHONE_USER" ]; then
  log "config incompleta: PHONE_WORKER_HOST/PHONE_WORKER_SSH_USER ausente"
  exit 2
fi

SRC_DIR="$ROOT_DIR/deploy/termux/phone-worker"
REMOTE_HOME="/data/data/com.termux/files/home"
REMOTE_DIR="$REMOTE_HOME/phone-worker"

if [ ! -d "$SRC_DIR" ]; then
  log "diretório não encontrado: $SRC_DIR"
  exit 3
fi

SSH_BASE=(ssh -p "$PHONE_PORT" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$PHONE_USER@$PHONE_HOST")
SCP_BASE=(scp -P "$PHONE_PORT" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new)

log "testando SSH em $PHONE_USER@$PHONE_HOST:$PHONE_PORT"
if ! "${SSH_BASE[@]}" 'echo ok' >/dev/null 2>&1; then
  log "celular offline ou SSH indisponível"
  mkdir -p "$ROOT_DIR/data"
  date -Is > "$ROOT_DIR/data/phone_worker_sync_pending.flag"
  exit 10
fi

log "preparando diretórios no celular"
"${SSH_BASE[@]}" "mkdir -p '$REMOTE_DIR'"

log "copiando arquivos do phone-worker"
"${SCP_BASE[@]}" \
  "$SRC_DIR/phone_worker.py" \
  "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/phone_worker.py"
"${SCP_BASE[@]}" \
  "$SRC_DIR/apk_identity.py" \
  "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/apk_identity.py"
scp -P "$PHONE_PORT" \
  "$SRC_DIR/music_agent.py" \
  "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/music_agent.py"

# Scripts reais ficam apenas em ~/phone-worker. Em ~/ ficam wrappers pequenos
# para não preservar cópias antigas que possam disparar pip/clang pesado.
for f in phone_worker_bootstrap.py repair-phone-worker.sh accept-core-worker-on-device.sh start-phone-worker.sh start-phone-music-agent.sh watch-phone-worker.sh pair-phone-worker.sh bootstrap-phone-worker.sh install.sh README.md phone-worker.env.example; do
  if [ -f "$SRC_DIR/$f" ]; then
    "${SCP_BASE[@]}" "$SRC_DIR/$f" "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/$f"
  fi
done

log "ajustando permissões no celular"
"${SSH_BASE[@]}" "
chmod +x '$REMOTE_DIR/phone_worker.py' '$REMOTE_DIR/phone_worker_bootstrap.py' '$REMOTE_DIR/repair-phone-worker.sh' '$REMOTE_DIR/accept-core-worker-on-device.sh' '$REMOTE_DIR/music_agent.py' '$REMOTE_DIR/start-phone-worker.sh' '$REMOTE_DIR/start-phone-music-agent.sh' '$REMOTE_DIR/watch-phone-worker.sh' '$REMOTE_DIR/pair-phone-worker.sh' '$REMOTE_DIR/bootstrap-phone-worker.sh' 2>/dev/null || true
for f in start-phone-worker.sh start-phone-music-agent.sh watch-phone-worker.sh pair-phone-worker.sh bootstrap-phone-worker.sh; do
  cat > '$REMOTE_HOME/'\$f <<EOF_WRAPPER
#!/data/data/com.termux/files/usr/bin/bash
# Wrapper de compatibilidade gerenciado pelo Core Worker.
exec /data/data/com.termux/files/usr/bin/bash '$REMOTE_DIR/'\$f "\\\$@"
EOF_WRAPPER
  chmod +x '$REMOTE_HOME/'\$f 2>/dev/null || true
done
mkdir -p '$REMOTE_HOME/.termux/boot'
printf '%s\n' '#!/data/data/com.termux/files/usr/bin/sh' '# Auto-start do Core Worker pelo Termux:Boot.' '# Criado/reparado pelo sync do phone-worker. Não coloque segredos aqui.' 'termux-wake-lock 2>/dev/null || true' 'sleep "\${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}"' 'cd "\$HOME/phone-worker" || exit 0' 'if [ -x "\$HOME/phone-worker/watch-phone-worker.sh" ]; then' '  nohup /data/data/com.termux/files/usr/bin/bash "\$HOME/phone-worker/watch-phone-worker.sh" >> "\$HOME/phone-worker/phone-worker-watch.boot.log" 2>&1 &' '  exit 0' 'fi' 'echo "[core-worker-boot] watch-phone-worker.sh não encontrado" >> "\$HOME/phone-worker.log"' > '$REMOTE_HOME/.termux/boot/10-core-worker'
chmod +x '$REMOTE_HOME/.termux/boot/10-core-worker' 2>/dev/null || true
"

log "reiniciando phone-worker no celular"
"${SSH_BASE[@]}" 'bash -s' <<'REMOTE_RESTART'
set -u
pid_file="$HOME/.local/state/core-worker-phone-worker/phone-worker.pid"
start_command="$HOME/phone-worker/start-phone-worker.sh"
if [ -f "$pid_file" ]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) pid='' ;;
  esac
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    cmdline="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
      *phone_worker.py*)
        kill "$pid" 2>/dev/null || true
        for _i in 1 2 3 4 5; do
          kill -0 "$pid" 2>/dev/null || break
          sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
          cmdline="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
          case "$cmdline" in
            *phone_worker.py*) kill -9 "$pid" 2>/dev/null || true ;;
          esac
        fi
        ;;
      *) echo '[phone-worker-sync] pid file aponta para processo não confirmado; não encerrado' >&2 ;;
    esac
  fi
fi
sleep 1
exec /data/data/com.termux/files/usr/bin/bash "$start_command"
REMOTE_RESTART

sleep 2

log "testando health pela VPS"
if python3 "$ROOT_DIR/scripts/phone-worker-client.py" health >/dev/null 2>&1; then
  rm -f "$ROOT_DIR/data/phone_worker_sync_pending.flag"
  python3 - <<'PY_MARK_SYNC' 2>/dev/null || true
import json
import subprocess
from utility.commands.workers_registry import get_core_workers_registry

worker_id = ""
try:
    raw = subprocess.check_output(["python3", "scripts/phone-worker-client.py", "status"], text=True, timeout=6)
    data = json.loads(raw) if raw.strip().startswith("{") else {}
    worker_id = str(data.get("worker_id") or data.get("id") or "").strip()
except Exception:
    worker_id = ""
try:
    result = get_core_workers_registry().mark_worker_update_jobs_superseded(worker_id, reason="sync manual saudável")
    count = int(result.get("superseded") or 0)
    if count:
        print(f"[phone-worker-sync] jobs antigos de update marcados como superados: {count}")
except Exception:
    pass
PY_MARK_SYNC
  log "sincronizado e saudável"
  exit 0
fi

log "sincronizou, mas health falhou"
mkdir -p "$ROOT_DIR/data"
date -Is > "$ROOT_DIR/data/phone_worker_sync_pending.flag"
exit 11
