import os


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_guild_ids(value: str) -> list[int]:
    if not value:
        return []

    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            pass
    return result


TOKEN = (os.getenv("DISCORD_TOKEN", "") or "").strip()

# Função do cargo (cooldown de menção)
TARGET_ROLE_ID = _parse_int(os.getenv("ROLE_ID", "0"), 0)
DISABLE_TIME = _parse_int(os.getenv("DISABLE_TIME", "14400"), 14400)

# Anti-mzk
TRIGGER_WORD = (os.getenv("TRIGGER_WORD", "") or "").lower().strip()
MUTE_TOGGLE_WORD = (os.getenv("MUTE_TOGGLE_WORD", "rola") or "rola").lower().strip()
TARGET_USER_ID = _parse_int(os.getenv("TARGET_USER_ID", "0"), 0)

# TTS
TTS_ENABLED = _parse_bool(os.getenv("TTS_ENABLED", "true"), True)
BLOCK_VOICE_BOT_ID = _parse_int(os.getenv("BLOCK_VOICE_BOT_ID", "0"), 0)

# Web server (Render)
PORT = _parse_int(os.getenv("PORT", "10000"), 10000)

# MongoDB
MONGODB_URI = (os.getenv("MONGODB_URI", "") or "").strip()
MONGODB_DB = (os.getenv("MONGODB_DB", "chat_revive") or "chat_revive").strip()
MONGODB_COLLECTION = (os.getenv("MONGODB_COLLECTION", "settings") or "settings").strip()

# Slash command sync
# Exemplo: GUILD_IDS=123,456,789
GUILD_IDS = _parse_guild_ids(os.getenv("GUILD_IDS", ""))

# Cores de embed
ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245
