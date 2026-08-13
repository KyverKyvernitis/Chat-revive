#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ubuntu/bot

# Garante diretórios runtime antes de carregar o cog TTS. O cleanup externo só
# remove arquivos, mas esta guarda evita falha total se a pasta foi apagada.
mkdir -p "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/runtime" \
         "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/cache" \
         "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/credentials" 2>/dev/null || true
chmod 700 "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/runtime" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/cache" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/credentials" 2>/dev/null || true
source /home/ubuntu/bot/.venv/bin/activate
set -a
source /home/ubuntu/bot/.env
set +a

# A VPS não inicia nem aguarda Lavalink local. Música pesada pertence ao phone worker/Music Agent.

# Mantém prints legados de diagnóstico em ordem cronológica no journald. Sem
# unbuffered, várias linhas de voz podiam aparecer juntas minutos depois.
exec python3 -u bot.py
