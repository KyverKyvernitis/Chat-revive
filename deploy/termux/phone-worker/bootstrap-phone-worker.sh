#!/data/data/com.termux/files/usr/bin/bash
# Bootstrap/repair local para transformar um Termux em Core Worker pareado.
# Uso:
#   bash bootstrap-phone-worker.sh CORE-XXXX http://100.x.x.x:10000 "Xiaomi Worker 2" midia
set -Eeuo pipefail


install_core_worker_shell_autostart() {
  local block file tmp
  block='# >>> core-worker-autostart >>>
# Bloco gerenciado pelo Core Worker. Não coloque segredos aqui.
if [ -z "${CORE_WORKER_SHELL_AUTOSTART_DONE:-}" ]; then
  export CORE_WORKER_SHELL_AUTOSTART_DONE=1
  if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then
    (
      termux-wake-lock >/dev/null 2>&1 || true
      cd "$HOME/phone-worker" >/dev/null 2>&1 || exit 0
      nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.shell.log" 2>&1 &
    ) >/dev/null 2>&1 &
  fi
fi
# <<< core-worker-autostart <<<
'
  for file in "$HOME/.bashrc" "$HOME/.profile"; do
    mkdir -p "$(dirname "$file")"
    tmp="$file.core-worker.tmp"
    if [ -f "$file" ]; then
      sed '/# >>> core-worker-autostart >>>/,/# <<< core-worker-autostart <<</d' "$file" > "$tmp" 2>/dev/null || cp "$file" "$tmp"
    else
      : > "$tmp"
    fi
    if [ -s "$tmp" ]; then
      printf '\n%s\n' "$block" >> "$tmp"
    else
      printf '%s\n' "$block" > "$tmp"
    fi
    mv "$tmp" "$file"
  done
}


write_compat_wrapper() {
  local name="$1"
  local target="$WORKER_DIR/$name"
  local wrapper="$HOME/$name"
  cat > "$wrapper" <<EOF_WRAPPER
#!/data/data/com.termux/files/usr/bin/bash
# Wrapper de compatibilidade gerenciado pelo Core Worker.
# O script real fica em \$HOME/phone-worker/$name para evitar versões antigas em ~/.
exec /data/data/com.termux/files/usr/bin/bash "\$HOME/phone-worker/$name" "\$@"
EOF_WRAPPER
  chmod +x "$wrapper" 2>/dev/null || true
  chmod +x "$target" 2>/dev/null || true
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
CODE="${1:-}"
VPS_URL="${2:-${CORE_WORKER_VPS_URL:-}}"
WORKER_NAME="${3:-${CORE_WORKER_NAME:-}}"
PROFILE="${4:-${CORE_WORKER_PROFILE:-midia}}"

log() { printf '[core-worker-bootstrap] %s\n' "$*"; }

install_core_worker_boot() {
  mkdir -p "$HOME/.termux/boot"
  printf '%s\n' \
'#!/data/data/com.termux/files/usr/bin/sh' \
'# Auto-start do Core Worker pelo Termux:Boot.' \
'# Criado/reparado pelo instalador do phone-worker. Não coloque segredos aqui.' \
'termux-wake-lock 2>/dev/null || true' \
'sleep "${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}"' \
'cd "$HOME/phone-worker" || exit 0' \
'if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then' \
'  nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.boot.log" 2>&1 &' \
'  exit 0' \
'fi' \
'echo "[core-worker-boot] watch-phone-worker.sh não encontrado" >> "$HOME/phone-worker.log"' \
> "$HOME/.termux/boot/10-core-worker"
  chmod +x "$HOME/.termux/boot/10-core-worker"
}



if [[ -z "$CODE" ]]; then
  read -r -p "Código CORE-XXXX: " CODE
fi
if [[ -z "$VPS_URL" ]]; then
  read -r -p "URL da VPS/Tailscale (ex: http://100.x.x.x:10000): " VPS_URL
fi
if [[ -z "$WORKER_NAME" ]]; then
  MANUFACTURER="$(getprop ro.product.manufacturer 2>/dev/null || true)"
  MODEL="$(getprop ro.product.model 2>/dev/null || true)"
  WORKER_NAME="${MANUFACTURER} ${MODEL}"
  WORKER_NAME="${WORKER_NAME# }"
  WORKER_NAME="${WORKER_NAME% }"
  [[ -n "$WORKER_NAME" ]] || WORKER_NAME="Core Phone Worker"
fi

log "instalando dependências básicas"
pkg install python tmux curl -y || true
pkg install termux-api -y || true

log "copiando/reparando scripts em $WORKER_DIR"
mkdir -p "$WORKER_DIR"
for f in phone_worker.py phone_worker_bootstrap.py repair-phone-worker.sh accept-core-worker-on-device.sh apk_identity.py music_agent.py start-phone-worker.sh watch-phone-worker.sh start-phone-music-agent.sh pair-phone-worker.sh bootstrap-phone-worker.sh install.sh README.md phone-worker.env.example; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$WORKER_DIR/$f"
  fi
done
for f in start-phone-worker.sh watch-phone-worker.sh start-phone-music-agent.sh pair-phone-worker.sh bootstrap-phone-worker.sh; do
  if [[ -f "$WORKER_DIR/$f" ]]; then
    write_compat_wrapper "$f"
  fi
done
chmod +x "$WORKER_DIR/phone_worker.py" "$WORKER_DIR/phone_worker_bootstrap.py" "$WORKER_DIR/repair-phone-worker.sh" "$WORKER_DIR/accept-core-worker-on-device.sh" "$WORKER_DIR/music_agent.py" "$WORKER_DIR/start-phone-worker.sh" "$WORKER_DIR/watch-phone-worker.sh" "$WORKER_DIR/start-phone-music-agent.sh" "$WORKER_DIR/install.sh" 2>/dev/null || true

log "criando/reparando inicialização automática do Termux:Boot"
install_core_worker_boot || true
install_core_worker_shell_autostart || true

if [[ ! -f "$HOME/.phone-worker.env" && -f "$WORKER_DIR/phone-worker.env.example" ]]; then
  cp "$WORKER_DIR/phone-worker.env.example" "$HOME/.phone-worker.env"
  chmod 600 "$HOME/.phone-worker.env"
fi

log "pareando como '$WORKER_NAME' perfil '$PROFILE'"
bash "$WORKER_DIR/pair-phone-worker.sh" "$CODE" "$VPS_URL" "$WORKER_NAME" "$PROFILE"

log "reiniciando worker pelo supervisor validado"
bash "$WORKER_DIR/start-phone-worker.sh" || true
nohup bash "$WORKER_DIR/watch-phone-worker.sh" >> "$WORKER_DIR/phone-worker-watch.log" 2>&1 &
sleep 2

log "teste de heartbeat"
cd "$WORKER_DIR"
python phone_worker.py --heartbeat-once || true
log "pronto. Abra o painel workers e aperte Atualizar."
