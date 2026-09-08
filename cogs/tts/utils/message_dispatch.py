from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .message_payload import MessageTTSPayload, build_message_tts_payload


@dataclass(slots=True)
class MessageDispatchResult:
    payload: MessageTTSPayload | None
    enqueued: bool
    dropped_count: int
    deduplicated: bool
    dispatch_ms: float
    payload_ms: float


async def dispatch_message_tts(cog: Any, message: Any, *, guild_defaults: dict | None, active_prefix: str, forced_engine: str) -> MessageDispatchResult:
    dispatch_started = time.monotonic()
    payload_started = time.monotonic()
    payload = await build_message_tts_payload(
        cog,
        message,
        guild_defaults=guild_defaults,
        active_prefix=active_prefix,
        forced_engine=forced_engine,
    )
    payload_ms = (time.monotonic() - payload_started) * 1000.0
    if hasattr(cog, "_record_message_payload_timing"):
        try:
            cog._record_message_payload_timing(payload_ms)
        except Exception:
            pass
    if payload is None:
        return MessageDispatchResult(None, False, 0, False, (time.monotonic() - dispatch_started) * 1000.0, payload_ms)

    payload.queue_item.message_id = int(getattr(message, "id", 0) or 0)
    payload.queue_item.enqueued_at_monotonic = dispatch_started
    state = cog._get_state(message.guild.id)
    state.last_text_channel_id = getattr(message.channel, "id", None)
    items = [payload.queue_item]
    expand = getattr(cog, "_expand_tts_queue_item", None)
    if callable(expand):
        try:
            items = list(expand(payload.queue_item)) or [payload.queue_item]
        except Exception:
            items = [payload.queue_item]

    enqueued = False
    dropped_count = 0
    deduplicated = False
    enqueue_group = getattr(cog, "_enqueue_tts_items", None)
    if callable(enqueue_group):
        enqueued, dropped_count, deduplicated = await enqueue_group(message.guild.id, items)
    else:
        for item in items:
            item_enqueued, item_dropped, item_dedup = await cog._enqueue_tts_item(message.guild.id, item)
            enqueued = enqueued or bool(item_enqueued)
            dropped_count += int(item_dropped or 0)
            deduplicated = deduplicated or bool(item_dedup)
    dispatch_ms = (time.monotonic() - dispatch_started) * 1000.0
    return MessageDispatchResult(payload, enqueued, dropped_count, deduplicated, dispatch_ms, payload_ms)
