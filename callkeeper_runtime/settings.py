from __future__ import annotations

from dataclasses import dataclass

import config

CALLKEEPER_OWNER_USER_ID = 394316054433628160


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


@dataclass(frozen=True)
class CallKeeperSettings:
    guild_id: int
    default_channel_id: int
    bot_tokens: tuple[str, ...]
    watchdog_interval: float = 5.0
    event_debounce: float = 0.20
    disconnect_cooldown: float = 3.0

    @property
    def is_configured(self) -> bool:
        return self.guild_id > 0 and len(self.bot_tokens) >= 3


def load_settings() -> CallKeeperSettings:
    tokens = tuple(str(token).strip() for token in (getattr(config, "CALLKEEPER_BOT_TOKENS", []) or []) if str(token).strip())[:3]
    return CallKeeperSettings(
        guild_id=_safe_int(getattr(config, "CALLKEEPER_GUILD_ID", 0), 0),
        default_channel_id=_safe_int(getattr(config, "CALLKEEPER_CHANNEL_ID", 0), 0),
        bot_tokens=tokens,
        watchdog_interval=max(2.0, _safe_float(getattr(config, "CALLKEEPER_WATCHDOG_INTERVAL_SECONDS", 5.0), 5.0)),
        event_debounce=max(0.05, _safe_float(getattr(config, "CALLKEEPER_EVENT_DEBOUNCE_SECONDS", 0.20), 0.20)),
        disconnect_cooldown=max(0.0, _safe_float(getattr(config, "CALLKEEPER_DISCONNECTED_BOT_COOLDOWN_SECONDS", 3.0), 3.0)),
    )
