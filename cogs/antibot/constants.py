from __future__ import annotations


# Mesmo emoji usado pela identidade visual atual do bot nos painéis de TTS.
CANCEL_EMOJI = "<:osaka:1539137127852539944>"
CANCEL_EMOJI_ID = 1539137127852539944

COUNTDOWN_SECONDS = 10
DELETE_MESSAGE_SECONDS = 24 * 60 * 60
RENDER_INTERVAL_SECONDS = 1.0
CANCELLED_VISIBLE_SECONDS = 4.0
FAILED_VISIBLE_SECONDS = 8.0

# Mantém o payload bem abaixo do limite de texto dos Components V2. Se um raid
# ultrapassar o limite, uma segunda mensagem compartilhada é aberta; nunca
# truncamos a conta de alguém que ainda precisa reagir.
MAX_ENTRIES_PER_BATCH = 28
MAX_RENDER_TEXT = 3900

STATE_WAITING = "waiting"
STATE_BANNING = "banning"
STATE_CANCELLED = "cancelled"
STATE_BANNED = "banned"
STATE_FAILED = "failed"
