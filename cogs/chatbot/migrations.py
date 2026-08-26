"""Migrações V2 pequenas, idempotentes e sem apagar dados legados."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import constants as C

log = logging.getLogger(__name__)

MIGRATION_ID = "chatbot-v2-safe-memory-and-config"


@dataclass(frozen=True)
class MigrationReport:
    already_applied: bool = False
    profiles_updated: int = 0
    configs_created: int = 0


async def run_migrations(coll) -> MigrationReport:
    """Prepara metadados V2; memória V1 permanece intacta para rollback."""
    if coll is None:
        return MigrationReport()
    marker_query = {"type": C.DOC_TYPE_MIGRATION, "migration_id": MIGRATION_ID}
    marker = await coll.find_one(marker_query)
    if marker and marker.get("status") == "complete":
        return MigrationReport(already_applied=True)

    now = time.time()
    profiles_updated = 0
    configs_created = 0
    guild_active: dict[int, str] = {}
    guild_ids: set[int] = set()
    cursor = coll.find({"type": C.DOC_TYPE_PROFILE})
    async for doc in cursor:
        gid = int(doc.get("guild_id") or 0)
        pid = str(doc.get("profile_id") or "")
        if gid <= 0 or not pid:
            continue
        guild_ids.add(gid)
        if bool(doc.get("active")) and gid not in guild_active:
            guild_active[gid] = pid
        if not doc.get("revision") or doc.get("schema_version") != C.CHATBOT_SCHEMA_VERSION:
            result = await coll.update_one(
                {"_id": doc.get("_id"), "type": C.DOC_TYPE_PROFILE},
                {"$set": {
                    "revision": str(doc.get("revision") or f"legacy:{pid}"),
                    "schema_version": C.CHATBOT_SCHEMA_VERSION,
                }},
            )
            profiles_updated += int(result.modified_count)

    for gid in guild_ids:
        active_id = guild_active.get(gid, "")
        result = await coll.update_one(
            {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid},
            {"$setOnInsert": {
                "type": C.DOC_TYPE_GUILD_CONFIG,
                "schema_version": C.CHATBOT_SCHEMA_VERSION,
                "guild_id": gid,
                "enabled": bool(active_id),
                "active_profile_id": active_id,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
        configs_created += int(getattr(result, "upserted_id", None) is not None)

    await coll.update_one(
        marker_query,
        {"$set": {
            "type": C.DOC_TYPE_MIGRATION,
            "migration_id": MIGRATION_ID,
            "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "status": "complete",
            "profiles_updated": profiles_updated,
            "configs_created": configs_created,
            "completed_at": time.time(),
        }},
        upsert=True,
    )
    log.info(
        "chatbot: migração V2 concluída (profiles=%s configs=%s)",
        profiles_updated, configs_created,
    )
    return MigrationReport(
        profiles_updated=profiles_updated, configs_created=configs_created
    )
