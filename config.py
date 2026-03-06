import os

TOKEN = os.getenv("DISCORD_TOKEN")

# Função do cargo (cooldown de menção)
TARGET_ROLE_ID = int(os.getenv("ROLE_ID", "0"))
DISABLE_TIME = int(os.getenv("DISABLE_TIME", "14400"))

# Anti-mzk (gatilhos no chat do canal de voz)
TRIGGER_WORD = (os.getenv("TRIGGER_WORD", "") or "").lower().strip()
MUTE_TOGGLE_WORD = (os.getenv("MUTE_TOGGLE_WORD", "rola") or "rola").lower().strip()
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))

# TTS por vírgula
TTS_ENABLED = (os.getenv("TTS_ENABLED", "true") or "true").lower().strip() in ("1", "true", "yes", "y", "on")

# Bloquear TTS se esse bot estiver na call (por ID)
BLOCK_VOICE_BOT_ID = int((os.getenv("BLOCK_VOICE_BOT_ID", "0") or "0").strip())

# ✅ Edge-TTS (Microsoft) — voz/“tom”
# Exemplos de vozes:
# - pt-BR-FranciscaNeural
# - pt-BR-AntonioNeural
TTS_VOICE = (os.getenv("TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural").strip()

# Formato do edge-tts: "+10%", "-10%", "+0%"
TTS_RATE = (os.getenv("TTS_RATE", "+0%") or "+0%").strip()
TTS_VOLUME = (os.getenv("TTS_VOLUME", "+0%") or "+0%").strip()

# Web server (Render)
PORT = int(os.getenv("PORT", "10000"))

# MongoDB
MONGODB_URI = (os.getenv("MONGODB_URI", "") or "").strip()
MONGODB_DB = (os.getenv("MONGODB_DB", "chat_revive") or "chat_revive").strip()
MONGODB_COLLECTION = (os.getenv("MONGODB_COLLECTION", "settings") or "settings").strip()

# Servidores para sync rápido dos slash commands
GUILD_IDS = [
    1313883930637762560,
    1349910251117350923,
]

# Cores dos embeds (hex)
ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245
