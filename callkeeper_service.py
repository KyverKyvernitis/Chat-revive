from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import signal
from pathlib import Path

import config
from callkeeper_runtime import CallKeeperRuntime, CallKeeperStateStore, load_settings
from db import SettingsDB

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "callkeeper.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.WARNING)
logging.getLogger("discord.voice_state").setLevel(logging.WARNING)
logging.getLogger("discord.player").setLevel(logging.WARNING)

log = logging.getLogger("callkeeper_service")


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


async def main() -> None:
    mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
    mongo_db_name = _cfg("MONGODB_DB", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
    mongo_collection_name = _cfg("MONGODB_COLLECTION", "MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")
    if not mongo_uri:
        raise RuntimeError("Nenhuma URI do MongoDB encontrada no config.py (MONGODB_URI/MONGO_URI).")

    settings = load_settings()
    if settings.guild_id <= 0:
        raise RuntimeError("CALLKEEPER_GUILD_ID não está configurado.")
    if len(settings.bot_tokens) < 3:
        raise RuntimeError("Configure CALLKEEPER_BOT_1_TOKEN, CALLKEEPER_BOT_2_TOKEN e CALLKEEPER_BOT_3_TOKEN.")

    db = SettingsDB(mongo_uri, mongo_db_name, mongo_collection_name)
    await db.init()

    store = CallKeeperStateStore(db, default_channel_id=settings.default_channel_id)
    runtime = CallKeeperRuntime(settings=settings, store=store)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await runtime.start()
    try:
        await stop_event.wait()
    finally:
        await runtime.stop()
        with contextlib.suppress(Exception):
            db.client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("[callkeeper] serviço parou com erro fatal")
        raise
