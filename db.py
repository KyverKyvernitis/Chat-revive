from __future__ import annotations

import asyncio

from datetime import datetime, timezone, timedelta
from copy import deepcopy
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import time
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

import config


class SettingsDB:
    def __init__(self, uri: str, db_name: str, coll_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.coll = self.db[coll_name]
        self.truco_history_coll = self.db[f"{coll_name}_truco_history"]
        self.guild_cache: Dict[int, Dict[str, Any]] = {}
        self.user_cache: Dict[tuple[int, int], Dict[str, Any]] = {}
        self._resolved_tts_cache: Dict[tuple[int, int], Dict[str, str]] = {}
        self._truco_history_lock = asyncio.Lock()
        self._chip_change_listeners: list[Any] = []

    def add_chip_change_listener(self, callback) -> None:
        if callable(callback) and callback not in self._chip_change_listeners:
            self._chip_change_listeners.append(callback)

    def remove_chip_change_listener(self, callback) -> None:
        self._chip_change_listeners = [item for item in self._chip_change_listeners if item != callback]

    @staticmethod
    def _chip_rank_doc_signature(doc: Dict[str, Any] | None) -> tuple[int, int, bool, str, int]:
        data = doc or {}
        try:
            chips = int(data.get("chips", 100) if data.get("chips", None) is not None else 100)
        except (TypeError, ValueError):
            chips = 100
        try:
            bonus = max(0, int(data.get("bonus_chips", 0) or 0))
        except (TypeError, ValueError):
            bonus = 0
        try:
            weekly_delta = int(data.get("chip_week_delta", 0) or 0)
        except (TypeError, ValueError):
            weekly_delta = 0
        return (
            chips,
            bonus,
            bool(data.get("has_chip_activity", False)),
            str(data.get("chip_week_key", "") or ""),
            weekly_delta,
        )

    def _notify_chip_change(self, guild_id: int, user_id: int) -> None:
        for callback in tuple(self._chip_change_listeners):
            try:
                callback(int(guild_id), int(user_id))
            except Exception as exc:
                print(f"[db] Falha ao notificar mudança de fichas: {exc}")

    async def init(self):
        await self._ensure_indexes()
        await self._cleanup_removed_tts_panel_history()
        await self._cleanup_removed_game_races()
        await self.load_cache()

    async def _cleanup_removed_tts_panel_history(self):
        """Remove o histórico legado dos painéis TTS antes de popular os caches."""
        try:
            result = await self.coll.update_many(
                {"panel_history": {"$exists": True}},
                {"$unset": {"panel_history": ""}},
            )
            modified = int(getattr(result, "modified_count", 0) or 0)
            if modified:
                print(f"[db] Histórico legado dos painéis TTS removido de {modified} documento(s).")
        except Exception as e:
            # A limpeza de compatibilidade não deve impedir o bot de iniciar.
            print(f"[db] Falha ao remover histórico legado dos painéis TTS: {e}")

    async def _cleanup_removed_game_races(self):
        """Aposenta raças removidas sem alterar saldo, estatísticas ou histórico."""
        race_fields = (
            "race_key",
            "race_active",
            "race_free_roleta_spins",
            "race_free_carta_spins",
            "race_sortudo_blessing_charges",
            "race_sortudo_blessing_started_at",
            "race_robbery_window_started_at",
            "race_robbery_uses",
            "race_mendigar_window_started_at",
            "race_mendigar_uses",
            "race_state",
        )
        try:
            result = await self.coll.update_many(
                {
                    "user_id": {"$exists": True},
                    "race_key": "vampiro",
                },
                {"$unset": {field: "" for field in race_fields}},
            )
            modified = int(getattr(result, "modified_count", 0) or 0)
            if modified:
                print(f"[db] Raça Vampiro aposentada de {modified} perfil(is).")
        except Exception as e:
            # O catálogo também rejeita a raça removida, então uma falha nesta
            # limpeza não reativa seus efeitos nem impede o bot de iniciar.
            print(f"[db] Falha ao limpar a raça Vampiro removida: {e}")

    async def _ensure_indexes(self):
        try:
            indexes = await self.coll.index_information()

            old_index = indexes.get("guild_id_1")
            if old_index and old_index.get("unique"):
                key = old_index.get("key", [])
                if key == [("guild_id", 1)]:
                    try:
                        await self.coll.drop_index("guild_id_1")
                        print("[db] Índice antigo guild_id_1 removido.")
                    except Exception as e:
                        print(f"[db] Falha ao remover índice guild_id_1: {e}")

            idx = indexes.get("guild_id_1_type_1")
            if idx:
                try:
                    await self.coll.drop_index("guild_id_1_type_1")
                    print("[db] Índice antigo guild_id_1_type_1 removido.")
                except Exception as e:
                    print(f"[db] Falha ao remover índice guild_id_1_type_1: {e}")

            await self.coll.create_index("type")
            await self.coll.create_index(
                [("guild_id", 1), ("type", 1)],
                unique=True,
                name="guild_id_1_type_1",
                partialFilterExpression={"type": "guild"},
            )
            await self.coll.create_index([("guild_id", 1), ("user_id", 1), ("type", 1)], unique=True, name="guild_id_1_user_id_1_type_1")

            # Índices do sistema de aniversários. Eles ficam no SettingsDB para
            # não depender de sincronização slash nem de estado local do cog, e
            # são compatíveis com o índice único legado (guild_id, user_id, type).
            await self.coll.create_index([("type", 1), ("guild_id", 1)], name="birthday_type_guild")
            await self.coll.create_index([("type", 1), ("guild_id", 1), ("user_id", 1)], name="birthday_entry_user")
            await self.coll.create_index([("type", 1), ("guild_id", 1), ("month", 1), ("day", 1)], name="birthday_entry_date")

            await self.truco_history_coll.create_index(
                [("guild_id", 1), ("player_low_id", 1), ("player_high_id", 1)],
                unique=True,
                name="truco_history_guild_pair",
            )
            await self.truco_history_coll.create_index(
                [("guild_id", 1), ("updated_at", -1)],
                name="truco_history_guild_updated",
            )
        except Exception as e:
            print(f"[db] Erro ao garantir índices: {e}")

    async def load_cache(self):
        self.guild_cache.clear()
        self.user_cache.clear()
        if not hasattr(self, "_resolved_tts_cache"):
            self._resolved_tts_cache = {}
        self._resolved_tts_cache.clear()
        cursor = self.coll.find({}, {"_id": 0})
        async for doc in cursor:
            doc_type = doc.get("type")
            gid = int(doc.get("guild_id", 0) or 0)
            has_user_id = doc.get("user_id") is not None

            if (doc_type == "guild" or (not doc_type and gid and not has_user_id)) and gid:
                normalized = dict(doc)
                normalized["type"] = "guild"
                existing = self.guild_cache.get(gid)
                if existing:
                    # Bases antigas podem ter um documento legado sem `type` e um
                    # documento novo `type=guild` para a mesma guild. O Mongo não
                    # garante qual vem primeiro no cursor; o merge preserva os
                    # campos do documento tipado sem perder configurações legadas.
                    if doc_type == "guild":
                        merged = {**existing, **normalized}
                    else:
                        merged = {**normalized, **existing}
                    merged["type"] = "guild"
                    self.guild_cache[gid] = merged
                else:
                    self.guild_cache[gid] = normalized
            elif (doc_type == "user" or (not doc_type and gid and has_user_id)) and gid and has_user_id:
                uid = int(doc["user_id"])
                doc.setdefault("type", "user")
                self.user_cache[(gid, uid)] = doc

    def _get_guild_doc(self, guild_id: int) -> Dict[str, Any]:
        return self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})

    async def reload_guild_cache(self, guild_id: int) -> Dict[str, Any]:
        gid = int(guild_id or 0)
        if gid <= 0:
            return {}

        query = {
            "guild_id": gid,
            "$or": [
                {"type": "guild"},
                {"type": {"$exists": False}, "user_id": {"$exists": False}},
            ],
        }
        merged: Dict[str, Any] | None = None
        cursor = self.coll.find(query, {"_id": 0})
        async for doc in cursor:
            doc_type = doc.get("type")
            normalized = dict(doc)
            normalized["type"] = "guild"
            if merged is None:
                merged = normalized
            elif doc_type == "guild":
                merged = {**merged, **normalized}
            else:
                merged = {**normalized, **merged}

        if merged is None:
            self.guild_cache.pop(gid, None)
            self._invalidate_resolved_tts_cache(guild_id=gid)
            return {}

        self.guild_cache[gid] = merged
        self._invalidate_resolved_tts_cache(guild_id=gid)
        return merged

    async def _save_guild_doc(self, guild_id: int, doc: Dict[str, Any]):
        doc["type"] = "guild"
        doc["guild_id"] = guild_id
        self.guild_cache[guild_id] = doc
        self._invalidate_resolved_tts_cache(guild_id=guild_id)
        await self.coll.update_one({"type": "guild", "guild_id": guild_id}, {"$set": doc}, upsert=True)

    def get_tts_voice_channel_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("tts_voice_channel_id", 0)
        try:
            return max(0, int(raw or 0))
        except Exception:
            return 0

    def iter_tts_voice_channel_ids(self) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for guild_id in list(self.guild_cache.keys()):
            channel_id = self.get_tts_voice_channel_id(guild_id)
            if channel_id > 0:
                result[int(guild_id)] = channel_id
        return result

    async def set_tts_voice_channel_id(self, guild_id: int, channel_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            parsed = max(0, int(channel_id or 0))
        except Exception:
            parsed = 0
        if parsed > 0:
            doc["tts_voice_channel_id"] = parsed
        else:
            doc.pop("tts_voice_channel_id", None)
        await self._save_guild_doc(guild_id, doc)

    def _invalidate_resolved_tts_cache(self, *, guild_id: int | None = None, user_id: int | None = None):
        if guild_id is None and user_id is None:
            self._resolved_tts_cache.clear()
            return

        if guild_id is not None and user_id is not None:
            self._resolved_tts_cache.pop((guild_id, user_id), None)
            return

        if guild_id is not None:
            stale = [key for key in self._resolved_tts_cache if key[0] == guild_id]
            for key in stale:
                self._resolved_tts_cache.pop(key, None)


    def gincana_enabled(self, guild_id: int) -> bool:
        # A economia não possui mais uma chave geral de liga/desliga. O método
        # continua existindo para compatibilidade com os handlers antigos.
        return True

    async def set_gincana_enabled(self, guild_id: int, value: bool):
        doc = self._get_guild_doc(guild_id)
        enabled = bool(value)
        doc["gincana_enabled"] = enabled
        doc["anti_mzk_enabled"] = enabled
        await self._save_guild_doc(guild_id, doc)

    def get_gincana_input_mode(self, guild_id: int) -> str:
        g = self.guild_cache.get(int(guild_id), {}) or {}
        value = str(g.get("gincana_input_mode", "triggers") or "triggers").strip().casefold()
        return "commands" if value == "commands" else "triggers"

    async def set_gincana_input_mode(self, guild_id: int, value: str):
        mode = str(value or "triggers").strip().casefold()
        if mode not in {"triggers", "commands"}:
            mode = "triggers"
        doc = self._get_guild_doc(int(guild_id))
        doc["gincana_input_mode"] = mode
        await self._save_guild_doc(int(guild_id), doc)

    def get_gincana_channel_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(int(guild_id), {}) or {}
        try:
            return max(0, int(g.get("gincana_channel_id", 0) or 0))
        except (TypeError, ValueError):
            return 0

    async def set_gincana_channel_id(self, guild_id: int, channel_id: int | None):
        try:
            parsed = max(0, int(channel_id or 0))
        except (TypeError, ValueError):
            parsed = 0
        doc = self._get_guild_doc(int(guild_id))
        if parsed:
            doc["gincana_channel_id"] = parsed
        else:
            doc.pop("gincana_channel_id", None)
        await self._save_guild_doc(int(guild_id), doc)

    def get_gincana_role_ids(self, guild_id: int) -> list[int]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("gincana_role_ids")
        if raw is None:
            raw = g.get("anti_mzk_role_ids", []) or []
        result: list[int] = []
        for value in raw or []:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                pass
        return result

    async def add_gincana_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_gincana_role_ids(guild_id)
        role_id = int(role_id)
        if role_id in role_ids:
            return False
        role_ids.append(role_id)
        doc["gincana_role_ids"] = role_ids
        doc["anti_mzk_role_ids"] = role_ids
        await self._save_guild_doc(guild_id, doc)
        return True

    async def remove_gincana_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_gincana_role_ids(guild_id)
        role_id = int(role_id)
        if role_id not in role_ids:
            return False
        new_role_ids = [rid for rid in role_ids if rid != role_id]
        doc["gincana_role_ids"] = new_role_ids
        doc["anti_mzk_role_ids"] = new_role_ids
        await self._save_guild_doc(guild_id, doc)
        return True

    def get_gincana_staff_role_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        try:
            return max(0, int(g.get("gincana_staff_role_id", g.get("anti_mzk_staff_role_id", 0)) or 0))
        except Exception:
            return 0

    async def set_gincana_staff_role_id(self, guild_id: int, role_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            parsed = max(0, int(role_id or 0))
        except Exception:
            parsed = 0
        doc["gincana_staff_role_id"] = parsed
        doc["anti_mzk_staff_role_id"] = parsed
        await self._save_guild_doc(guild_id, doc)

    def get_gincana_focus_map(self, guild_id: int) -> Dict[int, int]:
        return dict(self._get_modo_censura_focus_map(guild_id))

    async def toggle_gincana_focus_users(self, guild_id: int, user_ids: list[int]) -> tuple[list[int], list[int], Dict[int, int]]:
        added, removed, current = await self.toggle_modo_censura_focus_users(guild_id, user_ids)
        return added, removed, current

    async def clear_gincana_focus_users(self, guild_id: int) -> Dict[int, int]:
        return await self.clear_modo_censura_focus_users(guild_id)

    async def set_gincana_focus_users(self, guild_id: int, user_ids: list[int]) -> Dict[int, int]:
        return await self.set_modo_censura_focus_users(guild_id, user_ids)

    def get_gincana_focus_sync_groups(self, guild_id: int) -> list[list[int]]:
        return self.get_modo_censura_focus_sync_groups(guild_id)

    async def sync_gincana_focus_users(self, guild_id: int, user_ids: list[int]) -> list[int]:
        return await self.sync_modo_censura_focus_users(guild_id, user_ids)

    def get_gincana_timed_effects(self, guild_id: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
        g = self.guild_cache.get(int(guild_id), {}) or {}
        raw = g.get("gincana_timed_effects", {}) or {}
        result: Dict[str, Dict[str, Dict[str, Any]]] = {"pica": {}, "dj": {}, "rola": {}}
        if not isinstance(raw, dict):
            return result

        for effect_name in ("pica", "dj", "rola"):
            effect_map = raw.get(effect_name, {}) or {}
            if not isinstance(effect_map, dict):
                continue
            for key, record in effect_map.items():
                if not isinstance(record, dict):
                    continue
                cleaned = dict(record)
                try:
                    expires_at = float(cleaned.get("expires_at", 0.0) or 0.0)
                except Exception:
                    expires_at = 0.0
                if expires_at <= 0:
                    continue

                try:
                    user_id = int(cleaned.get("user_id") or 0)
                except Exception:
                    user_id = 0
                if user_id <= 0 and effect_name in {"pica", "rola"}:
                    try:
                        user_id = int(key)
                    except Exception:
                        user_id = 0
                if user_id <= 0:
                    continue

                if effect_name == "dj":
                    try:
                        channel_id = int(cleaned.get("channel_id") or 0)
                    except Exception:
                        channel_id = 0
                    if channel_id <= 0:
                        continue
                    cleaned["channel_id"] = channel_id
                    cleaned["user_id"] = user_id
                    cleaned["expires_at"] = expires_at
                    result[effect_name][str(key)] = cleaned
                else:
                    cleaned["user_id"] = user_id
                    cleaned["expires_at"] = expires_at
                    result[effect_name][str(user_id)] = cleaned
        return result

    async def set_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str, record: Dict[str, Any]):
        doc = self._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}

        effect_name = str(effect_name or "").strip()
        if effect_name not in {"pica", "dj", "rola"}:
            return

        bucket = effects.get(effect_name, {}) or {}
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[str(key)] = dict(record or {})
        effects[effect_name] = bucket
        doc["gincana_timed_effects"] = effects
        await self._save_guild_doc(int(guild_id), doc)

    async def remove_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str):
        doc = self._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}

        effect_name = str(effect_name or "").strip()
        bucket = effects.get(effect_name, {}) or {}
        if isinstance(bucket, dict):
            bucket.pop(str(key), None)
            effects[effect_name] = bucket

        if all(not (v or {}) for v in effects.values() if isinstance(v, dict)):
            doc.pop("gincana_timed_effects", None)
        else:
            doc["gincana_timed_effects"] = effects
        await self._save_guild_doc(int(guild_id), doc)

    def anti_mzk_enabled(self, guild_id: int) -> bool:
        g = self.guild_cache.get(guild_id, {})
        return bool(g.get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        doc = self._get_guild_doc(guild_id)
        doc["anti_mzk_enabled"] = bool(value)
        await self._save_guild_doc(guild_id, doc)

    def get_anti_mzk_role_ids(self, guild_id: int) -> list[int]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("anti_mzk_role_ids", []) or []
        result: list[int] = []
        for value in raw:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                pass
        return result

    async def add_anti_mzk_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_anti_mzk_role_ids(guild_id)

        role_id = int(role_id)
        if role_id in role_ids:
            return False

        role_ids.append(role_id)
        doc["anti_mzk_role_ids"] = role_ids
        await self._save_guild_doc(guild_id, doc)
        return True

    async def remove_anti_mzk_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_anti_mzk_role_ids(guild_id)

        role_id = int(role_id)
        if role_id not in role_ids:
            return False

        doc["anti_mzk_role_ids"] = [rid for rid in role_ids if rid != role_id]
        await self._save_guild_doc(guild_id, doc)
        return True

    def get_anti_mzk_staff_role_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        try:
            return max(0, int(g.get("anti_mzk_staff_role_id", 0) or 0))
        except Exception:
            return 0

    async def set_anti_mzk_staff_role_id(self, guild_id: int, role_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            doc["anti_mzk_staff_role_id"] = max(0, int(role_id or 0))
        except Exception:
            doc["anti_mzk_staff_role_id"] = 0
        await self._save_guild_doc(guild_id, doc)


    def _get_modo_censura_focus_map(self, guild_id: int) -> Dict[int, int]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("modo_censura_focus_users", {}) or {}
        cleaned: Dict[int, int] = {}
        changed = False

        for key, value in raw.items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                changed = True
                continue

            try:
                stored_value = int(value)
            except (TypeError, ValueError):
                stored_value = 1
                changed = True

            cleaned[uid] = max(1, stored_value)

        if changed:
            doc = self._get_guild_doc(guild_id)
            doc["modo_censura_focus_users"] = {str(uid): stored for uid, stored in cleaned.items()}
            self.guild_cache[guild_id] = doc

        return cleaned

    def get_modo_censura_focus_map(self, guild_id: int) -> Dict[int, int]:
        return dict(self._get_modo_censura_focus_map(guild_id))

    async def toggle_modo_censura_focus_users(self, guild_id: int, user_ids: list[int]) -> tuple[list[int], list[int], Dict[int, int]]:
        current = self._get_modo_censura_focus_map(guild_id)
        doc = self._get_guild_doc(guild_id)
        added: list[int] = []
        removed: list[int] = []

        for raw_uid in user_ids:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue

            if uid in current:
                current.pop(uid, None)
                removed.append(uid)
            else:
                current[uid] = 1
                added.append(uid)

        doc["modo_censura_focus_users"] = {str(uid): stored for uid, stored in current.items()}
        await self._save_guild_doc(guild_id, doc)
        return added, removed, dict(current)

    async def clear_modo_censura_focus_users(self, guild_id: int) -> Dict[int, int]:
        doc = self._get_guild_doc(guild_id)
        doc["modo_censura_focus_users"] = {}
        await self._save_guild_doc(guild_id, doc)
        return {}

    async def set_modo_censura_focus_users(self, guild_id: int, user_ids: list[int]) -> Dict[int, int]:
        doc = self._get_guild_doc(guild_id)
        cleaned: Dict[int, int] = {}
        for raw_uid in user_ids or []:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            cleaned[uid] = 1
        doc["modo_censura_focus_users"] = {str(uid): stored for uid, stored in cleaned.items()}
        await self._save_guild_doc(guild_id, doc)
        return dict(cleaned)

    def get_modo_censura_focus_sync_groups(self, guild_id: int) -> list[list[int]]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("modo_censura_focus_sync_groups", []) or []
        groups: list[set[int]] = []

        if isinstance(raw, dict):
            iterable = raw.values()
        else:
            iterable = raw

        for item in iterable:
            if isinstance(item, dict):
                values = item.get("members") or item.get("user_ids") or []
            else:
                values = item
            if not isinstance(values, (list, tuple, set)):
                continue

            group: set[int] = set()
            for raw_uid in values:
                try:
                    uid = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if uid > 0:
                    group.add(uid)
            if len(group) >= 2:
                groups.append(group)

        merged: list[set[int]] = []
        for group in groups:
            overlaps = [existing for existing in merged if existing & group]
            if not overlaps:
                merged.append(set(group))
                continue
            combined = set(group)
            for existing in overlaps:
                combined.update(existing)
                merged.remove(existing)
            merged.append(combined)

        cleaned = [sorted(group) for group in merged if len(group) >= 2]
        cleaned.sort(key=lambda group: (group[0], len(group), group))
        return cleaned

    async def sync_modo_censura_focus_users(self, guild_id: int, user_ids: list[int]) -> list[int]:
        new_ids: set[int] = set()
        for raw_uid in user_ids or []:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid > 0:
                new_ids.add(uid)

        if len(new_ids) < 2:
            return sorted(new_ids)

        groups = [set(group) for group in self.get_modo_censura_focus_sync_groups(guild_id)]
        merged = set(new_ids)
        remaining: list[set[int]] = []
        for group in groups:
            if group & merged:
                merged.update(group)
            else:
                remaining.append(group)
        remaining.append(merged)

        cleaned = [sorted(group) for group in remaining if len(group) >= 2]
        cleaned.sort(key=lambda group: (group[0], len(group), group))
        doc = self._get_guild_doc(guild_id)
        doc["modo_censura_focus_sync_groups"] = cleaned
        await self._save_guild_doc(guild_id, doc)
        return sorted(merged)

    def get_guild_tts_defaults(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        tts = g.get("tts_defaults", {}) or {}
        return {
            "enabled": bool(g.get("tts_enabled", True)),
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
            "android_voice": str(tts.get("android_voice", "") or ""),
            "android_language": str(tts.get("android_language", "") or ""),
            "android_rate": str(tts.get("android_rate", "") or ""),
            "android_pitch": str(tts.get("android_pitch", "") or ""),
            "bot_prefix": str(g.get("bot_prefix", "_") or "_"),
            "tts_prefix": str(g.get("tts_prefix", ",") or ","),
            "atts_prefix": str(g.get("atts_prefix", getattr(config, "TTS_ATTS_PREFIX", "%")) or getattr(config, "TTS_ATTS_PREFIX", "%")),
            "teto_prefix": str(g.get("teto_prefix", getattr(config, "TTS_TETO_PREFIX", "'")) or getattr(config, "TTS_TETO_PREFIX", "'")),
            "gtts_prefix": str(g.get("gtts_prefix", g.get("tts_prefix", ".")) or "."),
            "edge_prefix": str(g.get("edge_prefix", ",") or ","),
            "speech_limit_seconds": int(g.get("speech_limit_seconds", 30) or 30),
            "announce_author": bool(g.get("announce_author_enabled", False)),
            "auto_leave": bool(g.get("auto_leave_enabled", True)),
            "ignored_tts_role_id": int(g.get("ignored_tts_role_id", 0) or 0),
            # Migração suave: servidores antigos tinham apenas o ID.
            # Se já havia cargo salvo e não existe flag explícita, mantemos ativo
            # para preservar o comportamento anterior.
            "ignored_tts_role_enabled": bool(g.get("ignored_tts_role_enabled", bool(int(g.get("ignored_tts_role_id", 0) or 0)))),
        }

    async def set_guild_tts_defaults(
        self,
        guild_id: int,
        *,
        enabled: Optional[bool] = None,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        android_voice: Optional[str] = None,
        android_language: Optional[str] = None,
        android_rate: Optional[str] = None,
        android_pitch: Optional[str] = None,
        bot_prefix: Optional[str] = None,
        tts_prefix: Optional[str] = None,
        atts_prefix: Optional[str] = None,
        teto_prefix: Optional[str] = None,
        gtts_prefix: Optional[str] = None,
        edge_prefix: Optional[str] = None,
        speech_limit_seconds: Optional[int] = None,
        announce_author: Optional[bool] = None,
        auto_leave: Optional[bool] = None,
        ignored_tts_role_id: Optional[int] = None,
        ignored_tts_role_enabled: Optional[bool] = None,
    ):
        doc = self._get_guild_doc(guild_id)
        tts = doc.get("tts_defaults", {}) or {}

        if enabled is not None:
            doc["tts_enabled"] = bool(enabled)
        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if language is not None:
            tts["language"] = language
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch
        if android_voice is not None:
            tts["android_voice"] = android_voice
        if android_language is not None:
            tts["android_language"] = android_language
        if android_rate is not None:
            tts["android_rate"] = android_rate
        if android_pitch is not None:
            tts["android_pitch"] = android_pitch
        if bot_prefix is not None:
            doc["bot_prefix"] = str(bot_prefix or "_")[:8]
        if tts_prefix is not None:
            doc["tts_prefix"] = str(tts_prefix or ",")[:8]
        if atts_prefix is not None:
            doc["atts_prefix"] = str(atts_prefix or getattr(config, "TTS_ATTS_PREFIX", "%"))[:8]
        if teto_prefix is not None:
            doc["teto_prefix"] = str(teto_prefix or getattr(config, "TTS_TETO_PREFIX", "'"))[:8]
        if gtts_prefix is not None:
            doc["gtts_prefix"] = str(gtts_prefix or ".")[:8]
        if edge_prefix is not None:
            doc["edge_prefix"] = str(edge_prefix or ",")[:8]
        if speech_limit_seconds is not None:
            try:
                doc["speech_limit_seconds"] = max(1, min(600, int(speech_limit_seconds)))
            except Exception:
                doc["speech_limit_seconds"] = 30
        if announce_author is not None:
            doc["announce_author_enabled"] = bool(announce_author)
        if auto_leave is not None:
            doc["auto_leave_enabled"] = bool(auto_leave)
        if ignored_tts_role_id is not None:
            try:
                doc["ignored_tts_role_id"] = max(0, int(ignored_tts_role_id or 0))
            except Exception:
                doc["ignored_tts_role_id"] = 0
        if ignored_tts_role_enabled is not None:
            doc["ignored_tts_role_enabled"] = bool(ignored_tts_role_enabled)

        doc["tts_defaults"] = tts
        self._invalidate_resolved_tts_cache(guild_id=guild_id)
        await self._save_guild_doc(guild_id, doc)

    def get_ignored_tts_role_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        try:
            return max(0, int(g.get("ignored_tts_role_id", 0) or 0))
        except Exception:
            return 0

    def get_ignored_tts_role_enabled(self, guild_id: int) -> bool:
        g = self.guild_cache.get(guild_id, {})
        # Migração suave: se a flag ainda não existe, um cargo salvo antigo fica ativo.
        if "ignored_tts_role_enabled" not in g:
            return bool(self.get_ignored_tts_role_id(guild_id))
        return bool(g.get("ignored_tts_role_enabled", False))

    async def set_ignored_tts_role_id(self, guild_id: int, role_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            doc["ignored_tts_role_id"] = max(0, int(role_id or 0))
        except Exception:
            doc["ignored_tts_role_id"] = 0
        if doc["ignored_tts_role_id"] > 0:
            doc["ignored_tts_role_enabled"] = True
        await self._save_guild_doc(guild_id, doc)

    async def set_ignored_tts_role_enabled(self, guild_id: int, enabled: bool):
        doc = self._get_guild_doc(guild_id)
        doc["ignored_tts_role_enabled"] = bool(enabled)
        await self._save_guild_doc(guild_id, doc)


    def get_voice_moderation_settings(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("voice_moderation", {}) or {}

        def _get_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(raw.get(key, default) or default)))
            except Exception:
                return default

        def _get_float(key: str, default: float, minimum: float, maximum: float) -> float:
            try:
                return max(minimum, min(maximum, float(raw.get(key, default) or default)))
            except Exception:
                return default

        return {
            "enabled": bool(raw.get("enabled", False)),
            "disconnect_enabled": bool(raw.get("disconnect_enabled", True)),
            "threshold_rms": _get_int("threshold_rms", 4500, 500, 30000),
            "hits_to_trigger": _get_int("hits_to_trigger", 3, 1, 20),
            "window_seconds": _get_float("window_seconds", 1.2, 0.2, 10.0),
            "cooldown_seconds": _get_float("cooldown_seconds", 12.0, 1.0, 600.0),
            "max_intensity": _get_int("max_intensity", 11752, 3000, 32768),
        }

    async def update_voice_moderation_settings(
        self,
        guild_id: int,
        *,
        enabled: Optional[bool] = None,
        disconnect_enabled: Optional[bool] = None,
        threshold_rms: Optional[int] = None,
        hits_to_trigger: Optional[int] = None,
        window_seconds: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        max_intensity: Optional[int] = None,
    ):
        doc = self._get_guild_doc(guild_id)
        current = self.get_voice_moderation_settings(guild_id)
        data = doc.get("voice_moderation", {}) or {}

        if enabled is not None:
            data["enabled"] = bool(enabled)
        else:
            data.setdefault("enabled", bool(current.get("enabled", False)))

        if disconnect_enabled is not None:
            data["disconnect_enabled"] = bool(disconnect_enabled)
        else:
            data.setdefault("disconnect_enabled", bool(current.get("disconnect_enabled", True)))

        if threshold_rms is not None:
            try:
                data["threshold_rms"] = max(500, min(30000, int(threshold_rms)))
            except Exception:
                data["threshold_rms"] = int(current.get("threshold_rms", 4500) or 4500)
        else:
            data.setdefault("threshold_rms", int(current.get("threshold_rms", 4500) or 4500))

        if hits_to_trigger is not None:
            try:
                data["hits_to_trigger"] = max(1, min(20, int(hits_to_trigger)))
            except Exception:
                data["hits_to_trigger"] = int(current.get("hits_to_trigger", 3) or 3)
        else:
            data.setdefault("hits_to_trigger", int(current.get("hits_to_trigger", 3) or 3))

        if window_seconds is not None:
            try:
                data["window_seconds"] = max(0.2, min(10.0, float(window_seconds)))
            except Exception:
                data["window_seconds"] = float(current.get("window_seconds", 1.2) or 1.2)
        else:
            data.setdefault("window_seconds", float(current.get("window_seconds", 1.2) or 1.2))

        if cooldown_seconds is not None:
            try:
                data["cooldown_seconds"] = max(1.0, min(600.0, float(cooldown_seconds)))
            except Exception:
                data["cooldown_seconds"] = float(current.get("cooldown_seconds", 12.0) or 12.0)
        else:
            data.setdefault("cooldown_seconds", float(current.get("cooldown_seconds", 12.0) or 12.0))

        if max_intensity is not None:
            try:
                data["max_intensity"] = max(3000, min(32768, int(max_intensity)))
            except Exception:
                data["max_intensity"] = int(current.get("max_intensity", 11752) or 11752)
        else:
            data.setdefault("max_intensity", int(current.get("max_intensity", 11752) or 11752))

        doc["voice_moderation"] = data
        await self._save_guild_doc(guild_id, doc)

    async def set_voice_moderation_enabled(self, guild_id: int, value: bool):
        await self.update_voice_moderation_settings(guild_id, enabled=bool(value))



    def _get_user_doc(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        return dict(self.user_cache.get((guild_id, user_id), {"type": "user", "guild_id": guild_id, "user_id": user_id}))

    async def _save_user_doc(
        self,
        guild_id: int,
        user_id: int,
        doc: Dict[str, Any],
        *,
        unset_fields: tuple[str, ...] = (),
    ):
        key = (guild_id, user_id)
        previous_signature = self._chip_rank_doc_signature(self.user_cache.get(key))
        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id
        clean_unsets = tuple(
            dict.fromkeys(
                str(field).strip()
                for field in unset_fields
                if str(field).strip() and str(field).strip() not in doc
            )
        )
        self.user_cache[key] = doc
        self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
        update = {"$set": doc}
        if clean_unsets:
            update["$unset"] = {field: "" for field in clean_unsets}
        await self.coll.update_one(
            {"type": "user", "guild_id": guild_id, "user_id": user_id},
            update,
            upsert=True,
        )
        if previous_signature != self._chip_rank_doc_signature(doc):
            self._notify_chip_change(guild_id, user_id)

    async def clear_user_game_achievements(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        cached = dict(self.user_cache.get(key, {}))
        cached.pop("game_achievements", None)
        if cached:
            self.user_cache[key] = cached
        await self.coll.update_one(
            {"type": "user", "guild_id": int(guild_id), "user_id": int(user_id)},
            {"$unset": {"game_achievements": ""}},
            upsert=False,
        )

    def get_user_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        u = self.user_cache.get((guild_id, user_id), {})
        tts = u.get("tts", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
            "android_voice": str(tts.get("android_voice", "") or ""),
            "android_language": str(tts.get("android_language", "") or ""),
            "android_rate": str(tts.get("android_rate", "") or ""),
            "android_pitch": str(tts.get("android_pitch", "") or ""),
            "speaker_name": str(tts.get("speaker_name", "") or ""),
        }

    async def set_user_tts(
        self,
        guild_id: int,
        user_id: int,
        *,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        android_voice: Optional[str] = None,
        android_language: Optional[str] = None,
        android_rate: Optional[str] = None,
        android_pitch: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ):
        key = (guild_id, user_id)
        doc = self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id})
        tts = doc.get("tts", {}) or {}

        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if language is not None:
            tts["language"] = language
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch
        if android_voice is not None:
            tts["android_voice"] = android_voice
        if android_language is not None:
            tts["android_language"] = android_language
        if android_rate is not None:
            tts["android_rate"] = android_rate
        if android_pitch is not None:
            tts["android_pitch"] = android_pitch
        if speaker_name is not None:
            cleaned_speaker_name = str(speaker_name or "").strip()
            if cleaned_speaker_name:
                tts["speaker_name"] = cleaned_speaker_name
            else:
                tts.pop("speaker_name", None)

        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id
        doc["tts"] = tts
        self.user_cache[key] = doc
        self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
        await self.coll.update_one(
            {"type": "user", "guild_id": guild_id, "user_id": user_id},
            {"$set": doc},
            upsert=True,
        )

    async def reset_user_tts(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        doc = dict(self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id}))
        had_tts = bool((doc.get("tts", {}) or {}))
        doc.pop("tts", None)

        def _has_meaningful_value(value: Any) -> bool:
            if value in (None, "", [], {}):
                return False
            if isinstance(value, dict):
                return any(_has_meaningful_value(v) for v in value.values())
            if isinstance(value, (list, tuple, set)):
                return any(_has_meaningful_value(v) for v in value)
            return True

        keep_doc = False
        for field, value in doc.items():
            if field in {"type", "guild_id", "user_id"}:
                continue
            if _has_meaningful_value(value):
                keep_doc = True
                break

        if keep_doc:
            doc["type"] = "user"
            doc["guild_id"] = guild_id
            doc["user_id"] = user_id
            self.user_cache[key] = doc
            self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
            await self.coll.update_one(
                {"type": "user", "guild_id": guild_id, "user_id": user_id},
                {"$unset": {"tts": ""}, "$set": doc},
                upsert=True,
            )
        else:
            self.user_cache.pop(key, None)
            self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
            await self.coll.delete_one({"type": "user", "guild_id": guild_id, "user_id": user_id})

        return had_tts

    def resolve_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        cache_key = (guild_id, user_id)
        cached = self._resolved_tts_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        user = self.get_user_tts(guild_id, user_id)
        guild = self.get_guild_tts_defaults(guild_id)

        def pick(key: str, fallback: str) -> str:
            return (user.get(key) or "").strip() or (guild.get(key) or "").strip() or fallback

        engine = pick("engine", "gtts").lower().replace("-", "_")
        if engine in {"atts", "android", "android_tts", "native", "native_android"}:
            engine = "android_native"
        if engine not in ("android_native", "edge", "gtts"):
            engine = "gtts"

        resolved = {
            "engine": engine,
            "voice": pick("voice", "pt-BR-FranciscaNeural"),
            "language": pick("language", "pt-br"),
            "rate": pick("rate", "+0%"),
            "pitch": pick("pitch", "+0Hz"),
            "android_voice": pick("android_voice", ""),
            "android_language": pick("android_language", "pt-BR"),
            "android_rate": pick("android_rate", "1.0"),
            "android_pitch": pick("android_pitch", "1.0"),
            "speaker_name": str(user.get("speaker_name", "") or ""),
            "bot_prefix": str(guild.get("bot_prefix", "_") or "_"),
            "tts_prefix": str(guild.get("tts_prefix", ",") or ","),
            "atts_prefix": str(guild.get("atts_prefix", getattr(config, "TTS_ATTS_PREFIX", "%")) or getattr(config, "TTS_ATTS_PREFIX", "%")),
            "teto_prefix": str(guild.get("teto_prefix", getattr(config, "TTS_TETO_PREFIX", "'")) or getattr(config, "TTS_TETO_PREFIX", "'")),
            "gtts_prefix": str(guild.get("gtts_prefix", guild.get("tts_prefix", ".")) or "."),
            "edge_prefix": str(guild.get("edge_prefix", ",") or ","),
            "speech_limit_seconds": int(guild.get("speech_limit_seconds", 30) or 30),
        }
        self._resolved_tts_cache[cache_key] = dict(resolved)
        return resolved




def _sao_paulo_now(self) -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return datetime.now(timezone.utc)

def _current_daily_key(self) -> str:
    return self._sao_paulo_now().strftime("%Y-%m-%d")

def _current_week_key(self) -> str:
    now = self._sao_paulo_now()
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"

def get_user_daily_status(self, guild_id: int, user_id: int) -> Dict[str, int | bool | str]:
    doc = self.user_cache.get((guild_id, user_id), {})
    today = self._current_daily_key()
    last_key = str(doc.get("daily_last_claim_key", "") or "")
    streak = 0
    try:
        streak = max(0, int(doc.get("daily_streak", 0) or 0))
    except Exception:
        streak = 0
    available = last_key != today
    return {
        "today_key": today,
        "last_claim_key": last_key,
        "streak": streak,
        "available": available,
    }

async def claim_daily_bonus(self, guild_id: int, user_id: int, *, base_amount: int = 10) -> tuple[bool, int, int, int]:
    status = self.get_user_daily_status(guild_id, user_id)
    today = str(status["today_key"])
    last_key = str(status["last_claim_key"])
    current_streak = int(status["streak"])
    if last_key == today:
        return False, self.get_user_chips(guild_id, user_id, default=100), 0, current_streak

    new_streak = 1
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").date()
        if last_key:
            last_dt = datetime.strptime(last_key, "%Y-%m-%d").date()
            if today_dt - last_dt == timedelta(days=1):
                new_streak = current_streak + 1
    except Exception:
        new_streak = 1

    bonus = int(base_amount)
    if new_streak >= 7:
        bonus += 10
    elif new_streak >= 3:
        bonus += 5

    doc = self._get_user_doc(guild_id, user_id)
    doc["daily_last_claim_key"] = today
    doc["daily_streak"] = new_streak
    current = self.get_user_chips(guild_id, user_id, default=100)
    doc["chips"] = max(0, current + bonus)
    await self._save_user_doc(guild_id, user_id, doc)
    return True, int(doc["chips"]), bonus, new_streak

def get_user_weekly_points(self, guild_id: int, user_id: int) -> int:
    doc = self.user_cache.get((guild_id, user_id), {})
    if str(doc.get("weekly_points_week", "") or "") != self._current_week_key():
        return 0
    try:
        return max(0, int(doc.get("weekly_points", 0) or 0))
    except Exception:
        return 0

async def add_user_weekly_points(self, guild_id: int, user_id: int, amount: int) -> int:
    week_key = self._current_week_key()
    doc = self._get_user_doc(guild_id, user_id)
    if str(doc.get("weekly_points_week", "") or "") != week_key:
        doc["weekly_points_week"] = week_key
        doc["weekly_points"] = 0
    current = 0
    try:
        current = max(0, int(doc.get("weekly_points", 0) or 0))
    except Exception:
        current = 0
    doc["weekly_points"] = max(0, current + int(amount))
    await self._save_user_doc(guild_id, user_id, doc)
    return int(doc["weekly_points"])

def get_weekly_points_leaderboard(self, guild_id: int, *, limit: int = 10) -> list[Dict[str, int]]:
    rows: list[Dict[str, int]] = []
    week_key = self._current_week_key()
    for (gid, uid), doc in self.user_cache.items():
        if gid != guild_id:
            continue
        if str(doc.get("weekly_points_week", "") or "") != week_key:
            continue
        try:
            points = max(0, int(doc.get("weekly_points", 0) or 0))
        except Exception:
            points = 0
        if points <= 0:
            continue
        rows.append({"user_id": uid, "points": points})
    rows.sort(key=lambda item: (-item["points"], item["user_id"]))
    return rows[: max(1, int(limit))]

    def user_has_chip_activity(self, guild_id: int, user_id: int) -> bool:
        doc = self.user_cache.get((guild_id, user_id), {})
        return bool(doc.get("has_chip_activity", False))

    async def set_user_chip_activity(self, guild_id: int, user_id: int, value: bool):
        doc = self._get_user_doc(guild_id, user_id)
        doc["has_chip_activity"] = bool(value)
        await self._save_user_doc(guild_id, user_id, doc)

    async def mark_user_chip_activity(self, guild_id: int, user_id: int):
        if self.user_has_chip_activity(guild_id, user_id):
            return
        await self.set_user_chip_activity(guild_id, user_id, True)

    def get_chip_activity_user_ids(self, guild_id: int) -> list[int]:
        rows: list[int] = []
        for (gid, uid), doc in self.user_cache.items():
            if gid != guild_id:
                continue
            if not bool(doc.get("has_chip_activity", False)):
                continue
            rows.append(int(uid))
        rows.sort()
        return rows

    def get_user_chips(self, guild_id: int, user_id: int, *, default: int = 100) -> int:
        doc = self.user_cache.get((guild_id, user_id), {})
        try:
            return max(0, int(doc.get("chips", default) or default))
        except Exception:
            return int(default)

    def get_user_chip_reset_at(self, guild_id: int, user_id: int) -> float:
        doc = self.user_cache.get((guild_id, user_id), {})
        raw = doc.get("last_chip_reset_at", 0)
        try:
            return float(raw or 0)
        except Exception:
            return 0.0

    async def set_user_chips(self, guild_id: int, user_id: int, chips: int):
        doc = self._get_user_doc(guild_id, user_id)
        doc["chips"] = max(0, int(chips))
        await self._save_user_doc(guild_id, user_id, doc)

    async def add_user_chips(self, guild_id: int, user_id: int, amount: int) -> int:
        current = self.get_user_chips(guild_id, user_id)
        new_value = max(0, current + int(amount))
        await self.set_user_chips(guild_id, user_id, new_value)
        return new_value

    async def set_user_chip_reset_at(self, guild_id: int, user_id: int, timestamp: float):
        doc = self._get_user_doc(guild_id, user_id)
        try:
            doc["last_chip_reset_at"] = float(timestamp or 0)
        except Exception:
            doc["last_chip_reset_at"] = 0.0
        await self._save_user_doc(guild_id, user_id, doc)

    async def maybe_reset_user_chips(self, guild_id: int, user_id: int, *, amount: int = 100, cooldown_seconds: int = 21600) -> tuple[bool, int, float]:
        now = time.time()
        last_reset = self.get_user_chip_reset_at(guild_id, user_id)
        if last_reset <= 0:
            doc = self._get_user_doc(guild_id, user_id)
            doc["chips"] = max(0, int(amount))
            doc["last_chip_reset_at"] = float(now)
            await self._save_user_doc(guild_id, user_id, doc)
            return True, int(amount), 0.0
        remaining = max(0.0, (last_reset + float(cooldown_seconds)) - now)
        if remaining > 0:
            return False, self.get_user_chips(guild_id, user_id, default=100), remaining
        doc = self._get_user_doc(guild_id, user_id)
        doc["chips"] = max(0, int(amount))
        doc["last_chip_reset_at"] = float(now)
        await self._save_user_doc(guild_id, user_id, doc)
        return True, int(amount), 0.0



    def get_user_game_stats(self, guild_id: int, user_id: int) -> Dict[str, int]:
        doc = self.user_cache.get((guild_id, user_id), {})
        stats = doc.get("game_stats", {}) or {}

        def _int(key: str) -> int:
            try:
                return max(0, int(stats.get(key, 0) or 0))
            except Exception:
                return 0

        return {
            "games_played": _int("games_played"),
            "poker_wins": _int("poker_wins"),
            "poker_losses": _int("poker_losses"),
            "poker_rounds": _int("poker_rounds"),
            "buckshot_survivals": _int("buckshot_survivals"),
            "buckshot_eliminations": _int("buckshot_eliminations"),
            "roleta_spins": _int("roleta_spins"),
            "carta_spins": _int("carta_spins"),
            "roleta_jackpots": _int("roleta_jackpots"),
            "cartas_jackpots": _int("cartas_jackpots"),
            "alvo_games": _int("alvo_games"),
            "alvo_wins": _int("alvo_wins"),
            "alvo_bullseyes": _int("alvo_bullseyes"),
            "alvo_shots": _int("alvo_shots"),
            "alvo_hits": _int("alvo_hits"),
            "corrida_wins": _int("corrida_wins"),
            "corrida_losses": _int("corrida_losses"),
            "corrida_podiums": _int("corrida_podiums"),
            "truco_games": _int("truco_games"),
            "truco_wins": _int("truco_wins"),
            "truco_losses": _int("truco_losses"),
            "payments_sent": _int("payments_sent"),
            "payments_received": _int("payments_received"),
            "chips_sent_total": _int("chips_sent_total"),
            "chips_received_total": _int("chips_received_total"),
        }

    async def add_user_game_stat(self, guild_id: int, user_id: int, key: str, amount: int = 1) -> int:
        doc = self._get_user_doc(guild_id, user_id)
        stats = doc.get("game_stats", {}) or {}
        current = 0
        try:
            current = int(stats.get(key, 0) or 0)
        except Exception:
            current = 0
        new_value = max(0, current + int(amount))
        stats[key] = new_value
        doc["game_stats"] = stats
        await self._save_user_doc(guild_id, user_id, doc)
        return new_value

    def get_chip_leaderboard(self, guild_id: int, *, limit: int = 10) -> list[Dict[str, int]]:
        rows: list[Dict[str, int]] = []
        default_chips = 100
        for (gid, uid), doc in self.user_cache.items():
            if gid != guild_id:
                continue
            if not bool(doc.get("has_chip_activity", False)):
                continue
            try:
                chips = max(0, int(doc.get("chips", default_chips) or default_chips))
            except Exception:
                chips = default_chips
            rows.append({"user_id": uid, "chips": chips})
        rows.sort(key=lambda item: (-item["chips"], item["user_id"]))
        return rows[: max(1, int(limit))]

    def get_game_stat_leaderboard(self, guild_id: int, stat_key: str, *, limit: int = 3) -> list[Dict[str, int]]:
        rows: list[Dict[str, int]] = []
        for (gid, uid), _doc in self.user_cache.items():
            if gid != guild_id:
                continue
            value = self.get_user_game_stats(guild_id, uid).get(stat_key, 0)
            if value <= 0:
                continue
            rows.append({"user_id": uid, "value": value})
        rows.sort(key=lambda item: (-item["value"], item["user_id"]))
        return rows[: max(1, int(limit))]

    def get_role_cooldown(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        data = g.get("role_cooldown", {}) or {}
        return {
            "active": bool(data.get("active", False)),
            "ends_at": str(data.get("ends_at", "") or ""),
            "role_id": int(data.get("role_id", 0) or 0),
            "role_was_mentionable": data.get("role_was_mentionable", None),
        }

    async def set_role_cooldown(
        self,
        guild_id: int,
        *,
        active: bool,
        ends_at: Optional[str] = None,
        role_id: Optional[int] = None,
        role_was_mentionable: Optional[bool] = None,
    ):
        doc = self._get_guild_doc(guild_id)
        cooldown = doc.get("role_cooldown", {}) or {}
        cooldown["active"] = bool(active)
        if ends_at is not None:
            cooldown["ends_at"] = ends_at
        if role_id is not None:
            cooldown["role_id"] = int(role_id)
        if role_was_mentionable is not None:
            cooldown["role_was_mentionable"] = bool(role_was_mentionable)
        doc["role_cooldown"] = cooldown
        await self._save_guild_doc(guild_id, doc)

    async def clear_role_cooldown(self, guild_id: int):
        doc = self._get_guild_doc(guild_id)
        doc["role_cooldown"] = {"active": False, "ends_at": "", "role_id": 0, "role_was_mentionable": None}
        await self._save_guild_doc(guild_id, doc)

    @staticmethod
    def utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


# ---- gincana economy helpers rebound as SettingsDB methods ----
def _settingsdb_user_has_chip_activity(self, guild_id: int, user_id: int) -> bool:
    doc = self.user_cache.get((guild_id, user_id), {})
    return bool(doc.get("has_chip_activity", False))

def _settingsdb_get_chip_activity_user_ids(self, guild_id: int) -> list[int]:
    rows: list[int] = []
    for (gid, uid), doc in self.user_cache.items():
        if gid != guild_id:
            continue
        if not bool(doc.get("has_chip_activity", False)):
            continue
        rows.append(int(uid))
    rows.sort()
    return rows

async def _settingsdb_set_user_chip_activity(self, guild_id: int, user_id: int, value: bool):
    doc = self._get_user_doc(guild_id, user_id)
    doc["has_chip_activity"] = bool(value)
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_mark_user_chip_activity(self, guild_id: int, user_id: int):
    if self.user_has_chip_activity(guild_id, user_id):
        return
    await self.set_user_chip_activity(guild_id, user_id, True)

def _settingsdb_get_user_chips(self, guild_id: int, user_id: int, *, default: int = 100) -> int:
    doc = self.user_cache.get((guild_id, user_id), {})
    try:
        return max(0, int(doc.get("chips", default) or default))
    except Exception:
        return int(default)

def _settingsdb_get_user_chip_reset_at(self, guild_id: int, user_id: int) -> float:
    doc = self.user_cache.get((guild_id, user_id), {})
    raw = doc.get("last_chip_reset_at", 0)
    try:
        return float(raw or 0)
    except Exception:
        return 0.0

async def _settingsdb_set_user_chips(self, guild_id: int, user_id: int, chips: int):
    doc = self._get_user_doc(guild_id, user_id)
    doc["chips"] = max(0, int(chips))
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_add_user_chips(self, guild_id: int, user_id: int, amount: int) -> int:
    current = self.get_user_chips(guild_id, user_id)
    new_value = max(0, current + int(amount))
    await self.set_user_chips(guild_id, user_id, new_value)
    return new_value

async def _settingsdb_set_user_chip_reset_at(self, guild_id: int, user_id: int, timestamp: float):
    doc = self._get_user_doc(guild_id, user_id)
    try:
        doc["last_chip_reset_at"] = float(timestamp or 0)
    except Exception:
        doc["last_chip_reset_at"] = 0.0
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_maybe_reset_user_chips(self, guild_id: int, user_id: int, *, amount: int = 100, cooldown_seconds: int = 21600) -> tuple[bool, int, float]:
    now = time.time()
    last_reset = self.get_user_chip_reset_at(guild_id, user_id)
    if last_reset <= 0:
        doc = self._get_user_doc(guild_id, user_id)
        doc["chips"] = max(0, int(amount))
        doc["negative_balance_authorized"] = False
        doc["last_chip_reset_at"] = float(now)
        await self._save_user_doc(guild_id, user_id, doc)
        return True, int(amount), 0.0
    remaining = max(0.0, (last_reset + float(cooldown_seconds)) - now)
    if remaining > 0:
        return False, self.get_user_chips(guild_id, user_id, default=100), remaining
    doc = self._get_user_doc(guild_id, user_id)
    doc["chips"] = max(0, int(amount))
    doc["negative_balance_authorized"] = False
    doc["last_chip_reset_at"] = float(now)
    await self._save_user_doc(guild_id, user_id, doc)
    return True, int(amount), 0.0

def _settingsdb_get_user_game_stats(self, guild_id: int, user_id: int) -> Dict[str, int]:
    doc = self.user_cache.get((guild_id, user_id), {})
    stats = doc.get("game_stats", {}) or {}
    def _int(key: str) -> int:
        try:
            return max(0, int(stats.get(key, 0) or 0))
        except Exception:
            return 0
    return {
        "games_played": _int("games_played"),
        "poker_wins": _int("poker_wins"),
        "poker_losses": _int("poker_losses"),
        "poker_rounds": _int("poker_rounds"),
        "buckshot_survivals": _int("buckshot_survivals"),
        "buckshot_eliminations": _int("buckshot_eliminations"),
        "roleta_spins": _int("roleta_spins"),
        "carta_spins": _int("carta_spins"),
        "roleta_jackpots": _int("roleta_jackpots"),
        "cartas_jackpots": _int("cartas_jackpots"),
        "alvo_games": _int("alvo_games"),
        "alvo_wins": _int("alvo_wins"),
        "alvo_bullseyes": _int("alvo_bullseyes"),
        "alvo_shots": _int("alvo_shots"),
        "alvo_hits": _int("alvo_hits"),
        "corrida_wins": _int("corrida_wins"),
        "corrida_podiums": _int("corrida_podiums"),
        "corrida_losses": _int("corrida_losses"),
        "truco_games": _int("truco_games"),
        "truco_wins": _int("truco_wins"),
        "truco_losses": _int("truco_losses"),
        "payments_sent": _int("payments_sent"),
        "payments_received": _int("payments_received"),
        "chips_sent_total": _int("chips_sent_total"),
        "chips_received_total": _int("chips_received_total"),
    }

async def _settingsdb_add_user_game_stat(self, guild_id: int, user_id: int, key: str, amount: int = 1) -> int:
    doc = self._get_user_doc(guild_id, user_id)
    stats = doc.get("game_stats", {}) or {}
    try:
        current = int(stats.get(key, 0) or 0)
    except Exception:
        current = 0
    new_value = max(0, current + int(amount))
    stats[key] = new_value
    doc["game_stats"] = stats
    await self._save_user_doc(guild_id, user_id, doc)
    return new_value

def _settingsdb_normalize_truco_history_doc(doc: Dict[str, Any] | None) -> Dict[str, int]:
    source = doc or {}

    def _int(key: str) -> int:
        try:
            return max(0, int(source.get(key, 0) or 0))
        except Exception:
            return 0

    return {
        "player_low_id": _int("player_low_id"),
        "player_high_id": _int("player_high_id"),
        "low_wins": _int("low_wins"),
        "high_wins": _int("high_wins"),
        "total_games": _int("total_games"),
        "streak_winner_id": _int("streak_winner_id"),
        "streak_count": _int("streak_count"),
    }

async def _settingsdb_get_truco_match_history(
    self,
    guild_id: int,
    player_a_id: int,
    player_b_id: int,
) -> Dict[str, int]:
    low_id, high_id = sorted((int(player_a_id), int(player_b_id)))
    doc = await self.truco_history_coll.find_one(
        {
            "guild_id": int(guild_id),
            "player_low_id": low_id,
            "player_high_id": high_id,
        },
        {"_id": 0},
    )
    return _settingsdb_normalize_truco_history_doc(doc)

async def _settingsdb_record_truco_match(
    self,
    guild_id: int,
    player_a_id: int,
    player_b_id: int,
    winner_id: int,
    *,
    match_id: str,
) -> Dict[str, int]:
    gid = int(guild_id)
    player_a = int(player_a_id)
    player_b = int(player_b_id)
    winner = int(winner_id)
    if player_a <= 0 or player_b <= 0 or player_a == player_b:
        raise ValueError("Jogadores inválidos para o histórico de Truco.")
    if winner not in {player_a, player_b}:
        raise ValueError("O vencedor precisa participar da partida de Truco.")

    low_id, high_id = sorted((player_a, player_b))
    match_key = str(match_id or "").strip()
    query = {
        "guild_id": gid,
        "player_low_id": low_id,
        "player_high_id": high_id,
    }

    # O registro de sessões impede que o mesmo confronto termine em paralelo.
    # O lock também protege contra callbacks duplicados no mesmo processo.
    async with self._truco_history_lock:
        existing = await self.truco_history_coll.find_one(query, {"_id": 0}) or {}
        recorded_ids = [str(value) for value in (existing.get("recorded_match_ids", []) or [])]
        if match_key and match_key in recorded_ids:
            return _settingsdb_normalize_truco_history_doc(existing)

        previous_streak_winner = int(existing.get("streak_winner_id", 0) or 0)
        previous_streak_count = max(0, int(existing.get("streak_count", 0) or 0))
        streak_count = previous_streak_count + 1 if previous_streak_winner == winner else 1
        now = datetime.now(timezone.utc)

        update: Dict[str, Any] = {
            "$inc": {
                "total_games": 1,
                ("low_wins" if winner == low_id else "high_wins"): 1,
            },
            "$set": {
                "streak_winner_id": winner,
                "streak_count": streak_count,
                "updated_at": now,
            },
            "$setOnInsert": {
                "guild_id": gid,
                "player_low_id": low_id,
                "player_high_id": high_id,
                "created_at": now,
            },
        }
        if match_key:
            update["$push"] = {
                "recorded_match_ids": {
                    "$each": [match_key],
                    "$slice": -50,
                }
            }

        await self.truco_history_coll.update_one(query, update, upsert=True)
        current = await self.truco_history_coll.find_one(query, {"_id": 0})
        return _settingsdb_normalize_truco_history_doc(current)

def _settingsdb_get_chip_leaderboard(self, guild_id: int, *, limit: int = 10) -> list[Dict[str, int]]:
    rows: list[Dict[str, int]] = []
    default_chips = 100
    for (gid, uid), doc in self.user_cache.items():
        if gid != guild_id:
            continue
        if not bool(doc.get("has_chip_activity", False)):
            continue
        try:
            chips = max(0, int(doc.get("chips", default_chips) or default_chips))
        except Exception:
            chips = default_chips
        rows.append({"user_id": int(uid), "chips": chips})
    rows.sort(key=lambda item: (-item["chips"], item["user_id"]))
    return rows[: max(1, int(limit))]

def _settingsdb_get_game_stat_leaderboard(self, guild_id: int, stat_key: str, *, limit: int = 3) -> list[Dict[str, int]]:
    rows: list[Dict[str, int]] = []
    for (gid, uid), _doc in self.user_cache.items():
        if gid != guild_id:
            continue
        value = self.get_user_game_stats(guild_id, uid).get(stat_key, 0)
        if value <= 0:
            continue
        rows.append({"user_id": uid, "value": value})
    rows.sort(key=lambda item: (-item["value"], item["user_id"]))
    return rows[: max(1, int(limit))]

# Daily/weekly helpers were accidentally left outside the class during refactor.
# Bind them back onto SettingsDB so gincana commands/triggers can use them.
SettingsDB._sao_paulo_now = _sao_paulo_now
SettingsDB._current_daily_key = _current_daily_key
SettingsDB._current_week_key = _current_week_key
SettingsDB.get_user_daily_status = get_user_daily_status
SettingsDB.claim_daily_bonus = claim_daily_bonus
SettingsDB.get_user_weekly_points = get_user_weekly_points
SettingsDB.add_user_weekly_points = add_user_weekly_points
SettingsDB.get_weekly_points_leaderboard = get_weekly_points_leaderboard
SettingsDB.user_has_chip_activity = _settingsdb_user_has_chip_activity
SettingsDB.get_chip_activity_user_ids = _settingsdb_get_chip_activity_user_ids
SettingsDB.set_user_chip_activity = _settingsdb_set_user_chip_activity
SettingsDB.mark_user_chip_activity = _settingsdb_mark_user_chip_activity
SettingsDB.get_user_chips = _settingsdb_get_user_chips
SettingsDB.get_user_chip_reset_at = _settingsdb_get_user_chip_reset_at
SettingsDB.set_user_chips = _settingsdb_set_user_chips
SettingsDB.add_user_chips = _settingsdb_add_user_chips
SettingsDB.set_user_chip_reset_at = _settingsdb_set_user_chip_reset_at
SettingsDB.maybe_reset_user_chips = _settingsdb_maybe_reset_user_chips
SettingsDB.get_user_game_stats = _settingsdb_get_user_game_stats
SettingsDB.add_user_game_stat = _settingsdb_add_user_game_stat
SettingsDB.get_truco_match_history = _settingsdb_get_truco_match_history
SettingsDB.record_truco_match = _settingsdb_record_truco_match
SettingsDB.get_chip_leaderboard = _settingsdb_get_chip_leaderboard
SettingsDB.get_game_stat_leaderboard = _settingsdb_get_game_stat_leaderboard


# ---- bonus chips / debt overrides ----
def _settingsdb_get_user_bonus_chips(self, guild_id: int, user_id: int) -> int:
    doc = self.user_cache.get((guild_id, user_id), {})
    try:
        return max(0, int(doc.get("bonus_chips", 0) or 0))
    except Exception:
        return 0

async def _settingsdb_set_user_bonus_chips(self, guild_id: int, user_id: int, chips: int):
    doc = self._get_user_doc(guild_id, user_id)
    doc["bonus_chips"] = max(0, int(chips))
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_add_user_bonus_chips(self, guild_id: int, user_id: int, amount: int) -> int:
    current = self.get_user_bonus_chips(guild_id, user_id)
    new_value = max(0, current + int(amount))
    await self.set_user_bonus_chips(guild_id, user_id, new_value)
    return new_value

def _settingsdb_get_user_chips(self, guild_id: int, user_id: int, *, default: int = 100) -> int:
    doc = self.user_cache.get((guild_id, user_id), {})
    try:
        return int(doc.get("chips", default) if doc.get("chips", None) is not None else default)
    except Exception:
        return int(default)

def _settingsdb_get_negative_balance_authorized(self, guild_id: int, user_id: int) -> bool:
    doc = self.user_cache.get((guild_id, user_id), {})
    return bool(doc.get("negative_balance_authorized", False))

async def _settingsdb_set_negative_balance_authorized(self, guild_id: int, user_id: int, value: bool):
    doc = self._get_user_doc(guild_id, user_id)
    doc["negative_balance_authorized"] = bool(value)
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_set_user_chips(self, guild_id: int, user_id: int, chips: int):
    old_chips = self.get_user_chips(guild_id, user_id)
    new_chips = int(chips)
    doc = self._get_user_doc(guild_id, user_id)
    doc["chips"] = new_chips
    if old_chips < 0 <= new_chips:
        doc["negative_balance_authorized"] = False
    await self._save_user_doc(guild_id, user_id, doc)

async def _settingsdb_add_user_chips(self, guild_id: int, user_id: int, amount: int) -> int:
    current = self.get_user_chips(guild_id, user_id)
    bonus = self.get_user_bonus_chips(guild_id, user_id)
    delta = int(amount)
    if delta >= 0:
        # pay debt first, then normal chips
        new_chips = current + delta
        doc = self._get_user_doc(guild_id, user_id)
        doc["chips"] = int(new_chips)
        doc["bonus_chips"] = int(bonus)
        if current < 0 <= new_chips:
            doc["negative_balance_authorized"] = False
        await self._save_user_doc(guild_id, user_id, doc)
        return int(new_chips)
    spend = -delta
    use_bonus = min(bonus, spend)
    remaining = spend - use_bonus
    new_bonus = bonus - use_bonus
    new_chips = current - remaining
    doc = self._get_user_doc(guild_id, user_id)
    doc["chips"] = int(new_chips)
    doc["bonus_chips"] = int(new_bonus)
    await self._save_user_doc(guild_id, user_id, doc)
    return int(new_chips)

async def claim_daily_bonus(self, guild_id: int, user_id: int, *, base_amount: int = 10) -> tuple[bool, int, int, int]:
    status = self.get_user_daily_status(guild_id, user_id)
    today = str(status["today_key"])
    last_key = str(status["last_claim_key"])
    current_streak = int(status["streak"])
    if last_key == today:
        return False, self.get_user_chips(guild_id, user_id, default=100), 0, current_streak

    new_streak = 1
    try:
        from datetime import datetime, timedelta
        today_dt = datetime.strptime(today, "%Y-%m-%d").date()
        if last_key:
            last_dt = datetime.strptime(last_key, "%Y-%m-%d").date()
            if today_dt - last_dt == timedelta(days=1):
                new_streak = current_streak + 1
    except Exception:
        new_streak = 1

    bonus = int(base_amount)
    if new_streak >= 7:
        bonus += 10
    elif new_streak >= 3:
        bonus += 5

    doc = self._get_user_doc(guild_id, user_id)
    doc["daily_last_claim_key"] = today
    doc["daily_streak"] = new_streak
    current = self.get_user_chips(guild_id, user_id, default=100)
    new_chips = int(current + bonus)
    doc["chips"] = new_chips
    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + 10
    if current < 0 <= new_chips:
        doc["negative_balance_authorized"] = False
    await self._save_user_doc(guild_id, user_id, doc)
    return True, int(doc["chips"]), bonus, new_streak


def _settingsdb_get_chip_leaderboard(self, guild_id: int, *, limit: int = 10) -> list[dict]:
    rows=[]
    default_chips=100
    for (gid,uid),doc in self.user_cache.items():
        if gid!=guild_id: continue
        if not bool(doc.get("has_chip_activity", False)): continue
        try:
            chips=int(doc.get("chips", default_chips) if doc.get("chips", None) is not None else default_chips)
        except Exception:
            chips=default_chips
        rows.append({"user_id": int(uid), "chips": chips})
    rows.sort(key=lambda item: (-item["chips"], item["user_id"]))
    return rows[: max(1, int(limit))]

SettingsDB.get_user_bonus_chips = _settingsdb_get_user_bonus_chips
SettingsDB.set_user_bonus_chips = _settingsdb_set_user_bonus_chips
SettingsDB.add_user_bonus_chips = _settingsdb_add_user_bonus_chips
SettingsDB.get_negative_balance_authorized = _settingsdb_get_negative_balance_authorized
SettingsDB.set_negative_balance_authorized = _settingsdb_set_negative_balance_authorized
SettingsDB.get_user_chips = _settingsdb_get_user_chips
SettingsDB.set_user_chips = _settingsdb_set_user_chips
SettingsDB.add_user_chips = _settingsdb_add_user_chips
SettingsDB.claim_daily_bonus = claim_daily_bonus
SettingsDB.get_chip_leaderboard = _settingsdb_get_chip_leaderboard


CHIP_HISTORY_MAX_ENTRIES = 25


async def _settingsdb_append_chip_history(
    self,
    guild_id: int,
    user_id: int,
    *,
    delta: int,
    kind: str,
    reason: str | None = None,
    ts: float | None = None,
    entry_id: str | None = None,
    event_type: str | None = None,
    event_id: str | None = None,
    other_user_id: int | None = None,
    skill_eligible: bool | None = None,
    normal_delta: int | None = None,
    bonus_delta: int | None = None,
):
    delta_int = int(delta)
    if delta_int == 0:
        return
    doc = self._get_user_doc(guild_id, user_id)
    kind_key = str(kind or "chips").strip().lower()
    history = list(doc.get("chip_history", []) or [])
    entry = {
        "entry_id": str(entry_id or uuid.uuid4().hex),
        "ts": float(ts) if ts is not None else time.time(),
        "delta": delta_int,
        "kind": kind_key,
        "reason": (str(reason).strip() if reason else "")[:80],
    }
    if event_type:
        entry["event_type"] = str(event_type).strip().lower()[:40]
    if event_id:
        entry["event_id"] = str(event_id).strip()[:80]
    if other_user_id is not None:
        entry["other_user_id"] = int(other_user_id)
    if skill_eligible is not None:
        entry["skill_eligible"] = bool(skill_eligible)
    if normal_delta is not None:
        entry["normal_delta"] = int(normal_delta)
    if bonus_delta is not None:
        entry["bonus_delta"] = int(bonus_delta)
    history.append(entry)
    if len(history) > CHIP_HISTORY_MAX_ENTRIES:
        history = history[-CHIP_HISTORY_MAX_ENTRIES:]
    doc["chip_history"] = history
    weekly_delta = delta_int if kind_key == "chips" else int(normal_delta or 0)
    if weekly_delta != 0:
        week_key = self._current_week_key()
        if str(doc.get("chip_week_key", "") or "") != week_key:
            doc["chip_week_key"] = week_key
            doc["chip_week_delta"] = 0
        try:
            current_week_delta = int(doc.get("chip_week_delta", 0) or 0)
        except (TypeError, ValueError):
            current_week_delta = 0
        doc["chip_week_delta"] = current_week_delta + weekly_delta
    await self._save_user_doc(guild_id, user_id, doc)


def _settingsdb_get_chip_history(self, guild_id: int, user_id: int, *, limit: int = 7) -> list[dict]:
    doc = self.user_cache.get((guild_id, user_id), {})
    history = list(doc.get("chip_history", []) or [])
    if limit > 0:
        history = history[-limit:]
    return list(reversed(history))


def _settingsdb_get_user_chip_week_delta(self, guild_id: int, user_id: int) -> int:
    doc = self.user_cache.get((guild_id, user_id), {})
    if str(doc.get("chip_week_key", "") or "") != self._current_week_key():
        return 0
    try:
        return int(doc.get("chip_week_delta", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def _settingsdb_reset_user_chip_week_delta(self, guild_id: int, user_id: int) -> None:
    doc = self._get_user_doc(guild_id, user_id)
    doc["chip_week_key"] = ""
    doc["chip_week_delta"] = 0
    await self._save_user_doc(guild_id, user_id, doc)


def _settingsdb_get_chip_rank_snapshot(self, guild_id: int) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    week_key = self._current_week_key()
    for (stored_guild_id, user_id), doc in self.user_cache.items():
        if int(stored_guild_id) != int(guild_id) or not bool(doc.get("has_chip_activity", False)):
            continue
        try:
            chips = int(doc.get("chips", 100) if doc.get("chips", None) is not None else 100)
        except (TypeError, ValueError):
            chips = 100
        try:
            bonus_chips = max(0, int(doc.get("bonus_chips", 0) or 0))
        except (TypeError, ValueError):
            bonus_chips = 0
        weekly_delta = 0
        if str(doc.get("chip_week_key", "") or "") == week_key:
            try:
                weekly_delta = int(doc.get("chip_week_delta", 0) or 0)
            except (TypeError, ValueError):
                weekly_delta = 0
        rows.append(
            {
                "user_id": int(user_id),
                "chips": chips,
                "bonus_chips": bonus_chips,
                "weekly_delta": weekly_delta,
            }
        )
    return rows


SettingsDB.append_chip_history = _settingsdb_append_chip_history
SettingsDB.get_chip_history = _settingsdb_get_chip_history
SettingsDB.get_user_chip_week_delta = _settingsdb_get_user_chip_week_delta
SettingsDB.reset_user_chip_week_delta = _settingsdb_reset_user_chip_week_delta
SettingsDB.get_chip_rank_snapshot = _settingsdb_get_chip_rank_snapshot


# ---- chip season / global reset helpers ----
def _settingsdb_get_chip_season_state(self, guild_id: int) -> Dict[str, Any]:
    doc = self._get_guild_doc(guild_id)
    try:
        season = max(1, int(doc.get("chip_season", 1) or 1))
    except Exception:
        season = 1
    try:
        reset_at = float(doc.get("chip_season_reset_at", 0) or 0)
    except Exception:
        reset_at = 0.0
    try:
        triggered_at = float(doc.get("chip_season_reset_triggered_at", 0) or 0)
    except Exception:
        triggered_at = 0.0
    try:
        triggered_by = int(doc.get("chip_season_reset_triggered_by", 0) or 0)
    except Exception:
        triggered_by = 0
    try:
        last_reset_at = float(doc.get("chip_season_last_reset_at", 0) or 0)
    except Exception:
        last_reset_at = 0.0
    active = bool(doc.get("chip_season_reset_active", False) and reset_at > 0)
    executing = bool(doc.get("chip_season_reset_executing", False))
    return {
        "season": season,
        "active": active,
        "executing": executing,
        "reset_at": reset_at,
        "triggered_at": triggered_at,
        "triggered_by": triggered_by,
        "last_reset_at": last_reset_at,
    }

async def _settingsdb_schedule_chip_season_reset(self, guild_id: int, *, reset_at: float, triggered_by: int = 0) -> tuple[bool, Dict[str, Any]]:
    state = self.get_chip_season_state(guild_id)
    if bool(state.get("active", False)) and float(state.get("reset_at", 0.0) or 0.0) > time.time():
        return False, state
    doc = self._get_guild_doc(guild_id)
    doc["chip_season"] = max(1, int(doc.get("chip_season", 1) or 1))
    doc["chip_season_reset_active"] = True
    doc["chip_season_reset_executing"] = False
    doc["chip_season_reset_at"] = float(reset_at or 0)
    doc["chip_season_reset_triggered_at"] = float(time.time())
    doc["chip_season_reset_triggered_by"] = int(triggered_by or 0)
    await self._save_guild_doc(guild_id, doc)
    return True, self.get_chip_season_state(guild_id)

async def _settingsdb_try_acquire_chip_season_reset_lock(self, guild_id: int, *, now: float | None = None) -> bool:
    now_ts = float(now or time.time())
    result = await self.coll.update_one(
        {
            "type": "guild",
            "guild_id": guild_id,
            "chip_season_reset_active": True,
            "chip_season_reset_executing": {"$ne": True},
            "chip_season_reset_at": {"$lte": now_ts},
        },
        {"$set": {"chip_season_reset_executing": True}},
        upsert=False,
    )
    if getattr(result, "modified_count", 0):
        doc = self._get_guild_doc(guild_id)
        doc["chip_season_reset_executing"] = True
        self.guild_cache[guild_id] = doc
        return True
    return False

async def _settingsdb_reset_guild_chip_economy(self, guild_id: int, *, chips: int = 100) -> int:
    target_chips = int(chips)
    update_payload = {
        "$set": {
            "chips": target_chips,
            "bonus_chips": 0,
            "chip_week_delta": 0,
            "chip_week_key": "",
            "weekly_points": 0,
            "weekly_points_week": "",
            "game_stats": {},
            "daily_last_claim_key": "",
            "daily_streak": 0,
            "last_chip_reset_at": 0.0,
            "chip_recharge_manual_initialized": False,
            "negative_balance_authorized": False,
            "last_robbery_at": 0.0,
            "last_mendigar_at": 0.0,
            "last_esmola_at": 0.0,
            "race_key": "",
            "race_active": False,
            "race_free_roleta_spins": 0,
            "race_free_carta_spins": 0,
            "race_sortudo_blessing_charges": 0,
            "race_sortudo_blessing_started_at": 0.0,
            "race_robbery_window_started_at": 0.0,
            "race_robbery_uses": 0,
            "race_mendigar_window_started_at": 0.0,
            "race_mendigar_uses": 0,
        },
        "$unset": {
            "game_achievements": "",
            "chip_history": "",
            "race_skill_0to1_cutoff_ts": "",
            "race_skill_0to1_last_entry_id": "",
            "race_ordinary_robberies": "",
        },
    }
    await self.coll.update_many({"type": "user", "guild_id": guild_id}, update_payload)
    affected = 0
    for key, doc in list(self.user_cache.items()):
        gid, _uid = key
        if int(gid) != int(guild_id):
            continue
        updated = dict(doc)
        updated["chips"] = target_chips
        updated["bonus_chips"] = 0
        updated["chip_week_delta"] = 0
        updated["chip_week_key"] = ""
        updated["weekly_points"] = 0
        updated["weekly_points_week"] = ""
        updated["game_stats"] = {}
        updated["daily_last_claim_key"] = ""
        updated["daily_streak"] = 0
        updated["last_chip_reset_at"] = 0.0
        updated["chip_recharge_manual_initialized"] = False
        updated["negative_balance_authorized"] = False
        updated["last_robbery_at"] = 0.0
        updated["last_mendigar_at"] = 0.0
        updated["last_esmola_at"] = 0.0
        updated["race_key"] = ""
        updated["race_active"] = False
        updated["race_free_roleta_spins"] = 0
        updated["race_free_carta_spins"] = 0
        updated["race_sortudo_blessing_charges"] = 0
        updated["race_sortudo_blessing_started_at"] = 0.0
        updated["race_robbery_window_started_at"] = 0.0
        updated["race_robbery_uses"] = 0
        updated["race_mendigar_window_started_at"] = 0.0
        updated["race_mendigar_uses"] = 0
        updated.pop("game_achievements", None)
        updated.pop("chip_history", None)
        updated.pop("race_skill_0to1_cutoff_ts", None)
        updated.pop("race_skill_0to1_last_entry_id", None)
        updated.pop("race_ordinary_robberies", None)
        self.user_cache[key] = updated
        self._notify_chip_change(guild_id, _uid)
        affected += 1
    return affected

async def _settingsdb_complete_chip_season_reset(self, guild_id: int, *, executed_at: float | None = None) -> int:
    executed = float(executed_at or time.time())
    doc = self._get_guild_doc(guild_id)
    next_season = max(1, int(doc.get("chip_season", 1) or 1)) + 1
    doc["chip_season"] = next_season
    doc["chip_season_reset_active"] = False
    doc["chip_season_reset_executing"] = False
    doc["chip_season_reset_at"] = 0.0
    doc["chip_season_reset_triggered_at"] = 0.0
    doc["chip_season_reset_triggered_by"] = 0
    doc["chip_season_last_reset_at"] = executed
    await self._save_guild_doc(guild_id, doc)
    return int(next_season)

SettingsDB.get_chip_season_state = _settingsdb_get_chip_season_state
SettingsDB.schedule_chip_season_reset = _settingsdb_schedule_chip_season_reset
SettingsDB.try_acquire_chip_season_reset_lock = _settingsdb_try_acquire_chip_season_reset_lock
SettingsDB.reset_guild_chip_economy = _settingsdb_reset_guild_chip_economy
SettingsDB.complete_chip_season_reset = _settingsdb_complete_chip_season_reset


def _settingsdb_color_roles_defaults() -> dict[str, Any]:
    slots: dict[str, dict[str, Any]] = {}
    default_slots = [
        (1, "Vermelho escuro", "#b11212", "#8b0000"),
        (2, "Amarelo escuro", "#c9a31a", "#b8860b"),
        (3, "Verde escuro", "#0b5d30", "#006400"),
        (4, "Azul escuro", "#1737d8", "#00008b"),
        (5, "Rosa escuro", "#d61ea6", "#c71585"),
        (6, "Roxo escuro", "#9a0ec7", "#800080"),
        (7, "Laranja escuro", "#d98900", "#ff8c00"),
        (8, "Bege escuro", "#b96d43", "#a0522d"),
        (9, "Ciano escuro", "#008f98", "#008b8b"),
        (10, "Preto", "#000000", "#1f1f1f"),
        (11, "Vermelho", "#ff1b1b", "#ff0000"),
        (12, "Amarelo", "#ffec1a", "#ffd700"),
        (13, "Verde", "#11b611", "#00ff00"),
        (14, "Azul", "#0e2fff", "#1e90ff"),
        (15, "Rosa", "#ff62c3", "#ff69b4"),
        (16, "Roxo", "#c020ff", "#9370db"),
        (17, "Laranja", "#ffad13", "#ffa500"),
        (18, "Bege", "#d6b694", "#f5deb3"),
        (19, "Ciano", "#00ecff", "#00ffff"),
        (20, "Cinza", "#8f8f8f", "#808080"),
        (21, "Vermelho claro", "#ff8b8b", "#ff7f7f"),
        (22, "Amarelo claro", "#fff38f", "#fff68f"),
        (23, "Verde claro", "#9cff9c", "#90ee90"),
        (24, "Azul claro", "#a6c7ff", "#87cefa"),
        (25, "Rosa claro", "#ffb6d9", "#ffb6c1"),
        (26, "Roxo claro", "#d6a5ff", "#d8bfd8"),
        (27, "Laranja claro", "#ffd199", "#ffcc99"),
        (28, "Bege claro", "#ffe8d0", "#f5f5dc"),
        (29, "Ciano claro", "#d6ffff", "#e0ffff"),
        (30, "Branco", "#ffffff", "#ffffff"),
    ]
    for number, name, text_hex, role_hex in default_slots:
        slots[str(number)] = {
            "number": number,
            "name": name,
            "text_hex": text_hex,
            "role_hex": role_hex,
            "role_id": 0,
            "role_name": name,
            "managed": False,
        }
    return {
        "enabled": False,
        "channel_id": 0,
        "message_ids": [],
        "panel_count": 3,
        "panel_layout": [
            {"id": f"panel-{index}", "slots": list(range((index - 1) * 10 + 1, index * 10 + 1))}
            for index in range(1, 4)
        ],
        "messages": {
            "1": {"title": "", "subtitle": "", "footer": ""},
            "2": {"title": "", "subtitle": "", "footer": ""},
            "3": {"title": "", "subtitle": "", "footer": ""},
            "4": {"title": "", "subtitle": "", "footer": ""},
            "5": {"title": "", "subtitle": "", "footer": ""},
        },
        "templates": {
            "apply": "cor {cor_adicionada} aplicada.",
            "remove": "cor {cor_removida} removida.",
            "switch": "cor alterada: {cor_removida} → {cor_adicionada}.",
            "no_role": "Essa cor ainda não está configurada.",
            "hierarchy": "não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
            "missing_panel": "Esse painel de cores não é mais o oficial deste servidor.",
        },
        "slots": slots,
    }




def _settingsdb_normalize_color_panel_layout(raw: Any, *, fallback_count: int = 3) -> list[dict[str, Any]]:
    defaults = _settingsdb_color_roles_defaults()["panel_layout"]
    try:
        count = max(1, min(3, int(fallback_count or 3)))
    except Exception:
        count = 3
    source = raw if isinstance(raw, list) else deepcopy(defaults[:count])
    used_slots: set[int] = set()
    used_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(source[:3], start=1):
        panel = dict(item or {}) if isinstance(item, dict) else {}
        slots: list[int] = []
        for raw_slot in panel.get("slots") or []:
            try:
                slot_number = int(raw_slot)
            except Exception:
                continue
            if not 1 <= slot_number <= 30 or slot_number in used_slots:
                continue
            used_slots.add(slot_number)
            slots.append(slot_number)
            if len(slots) >= 10:
                break
        if not slots:
            continue
        panel_id = str(panel.get("id") or f"panel-{index}").strip()[:80]
        if not panel_id or panel_id in used_ids:
            panel_id = f"panel-{index}"
            suffix = 2
            while panel_id in used_ids:
                panel_id = f"panel-{index}-{suffix}"
                suffix += 1
        used_ids.add(panel_id)
        result.append({"id": panel_id, "slots": slots})
    return result or [{"id": "panel-1", "slots": [1]}]


def _settingsdb_color_roles_legacy_slot_payload(slot_number: int) -> dict[str, Any]:
    defaults = _settingsdb_color_roles_defaults()
    slot = dict((defaults.get("slots") or {}).get(str(slot_number), {}) or {})
    if int(slot_number) != 10:
        return slot
    slot["name"] = "Preto escuro"
    slot["text_hex"] = "#4a4a4a"
    slot["role_hex"] = "#1f1f1f"
    slot["role_name"] = "Preto escuro"
    return slot


def _settingsdb_color_roles_legacy_templates() -> dict[str, tuple[str, ...]]:
    return {
        "apply": ("{membro}, a cor {cor_adicionada} foi aplicada.",),
        "remove": ("{membro}, a cor {cor_removida} foi removida.",),
        "switch": ("{membro}, {cor_removida} foi removida e {cor_adicionada} foi aplicada.",),
        "hierarchy": ("Não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",),
    }


def _settingsdb_get_color_roles_config(self, guild_id: int) -> Dict[str, Any]:
    doc = self._get_guild_doc(guild_id)
    raw = deepcopy(doc.get("color_roles") or {})
    base = _settingsdb_color_roles_defaults()
    if "enabled" in raw:
        base["enabled"] = bool(raw.get("enabled"))
    else:
        base["enabled"] = bool(int(raw.get("channel_id") or 0) and list(raw.get("message_ids") or []))
    base["channel_id"] = int(raw.get("channel_id", base["channel_id"]) or 0)
    base["message_ids"] = [int(mid) for mid in (raw.get("message_ids") or []) if str(mid).isdigit()]
    try:
        legacy_panel_count = max(1, min(3, int(raw.get("panel_count") or base.get("panel_count") or 3)))
    except Exception:
        legacy_panel_count = 3
    base["panel_layout"] = _settingsdb_normalize_color_panel_layout(raw.get("panel_layout"), fallback_count=legacy_panel_count)
    base["panel_count"] = len(base["panel_layout"])
    messages = raw.get("messages") or {}
    for key in ("1", "2", "3", "4", "5"):
        payload = messages.get(key) or {}
        base["messages"][key] = {
            "title": str(payload.get("title") or ""),
            "subtitle": str(payload.get("subtitle") or ""),
            "footer": str(payload.get("footer") or ""),
        }
    templates = raw.get("templates") or {}
    legacy_templates = _settingsdb_color_roles_legacy_templates()
    for key, value in templates.items():
        if key in base["templates"] and value is not None:
            text = str(value)
            if text in legacy_templates.get(key, ()): 
                continue
            base["templates"][key] = text
    slots = raw.get("slots") or {}
    for key, payload in slots.items():
        if key not in base["slots"]:
            continue
        merged = dict(base["slots"][key])
        merged.update(dict(payload or {}))
        merged["number"] = int(merged.get("number") or int(key))
        merged["role_id"] = int(merged.get("role_id") or 0)
        merged["managed"] = bool(merged.get("managed", False))
        merged["name"] = str(merged.get("name") or base["slots"][key]["name"])
        merged["role_name"] = str(merged.get("role_name") or merged["name"])
        merged["text_hex"] = str(merged.get("text_hex") or base["slots"][key]["text_hex"])
        merged["role_hex"] = str(merged.get("role_hex") or base["slots"][key]["role_hex"])
        if (
            int(key) != 30
            and int(merged.get("role_id") or 0) == 0
            and str(merged.get("text_hex") or "").lower() == "#ffffff"
            and str(merged.get("role_hex") or "").lower() == "#ffffff"
        ):
            merged["text_hex"] = str(base["slots"][key]["text_hex"])
            merged["role_hex"] = str(base["slots"][key]["role_hex"])
        legacy = _settingsdb_color_roles_legacy_slot_payload(int(key))
        comparable_current = {
            "name": str(merged.get("name") or ""),
            "text_hex": str(merged.get("text_hex") or ""),
            "role_hex": str(merged.get("role_hex") or ""),
            "role_id": int(merged.get("role_id") or 0),
            "role_name": str(merged.get("role_name") or ""),
            "managed": bool(merged.get("managed", False)),
        }
        comparable_legacy = {
            "name": str(legacy.get("name") or ""),
            "text_hex": str(legacy.get("text_hex") or ""),
            "role_hex": str(legacy.get("role_hex") or ""),
            "role_id": 0,
            "role_name": str(legacy.get("role_name") or ""),
            "managed": False,
        }
        if comparable_current == comparable_legacy:
            merged = dict(base["slots"][key])
        elif int(key) == 10:
            legacy_name = {"Preto escuro", "Preto"}
            if (
                str(merged.get("name") or "") in legacy_name
                and str(merged.get("text_hex") or "") in {"#4a4a4a", "#000000"}
                and str(merged.get("role_hex") or "") in {"#1f1f1f", "#000000"}
                and (bool(merged.get("managed", False)) or int(merged.get("role_id") or 0) == 0)
            ):
                merged["name"] = str(base["slots"][key]["name"])
                merged["text_hex"] = str(base["slots"][key]["text_hex"])
                merged["role_hex"] = str(base["slots"][key]["role_hex"])
                if str(merged.get("role_name") or "") in {"", "Preto escuro", "Preto"}:
                    merged["role_name"] = str(base["slots"][key]["role_name"])
        base["slots"][key] = merged
    return base


async def _settingsdb_set_color_roles_config(self, guild_id: int, config: Dict[str, Any]):
    doc = self._get_guild_doc(guild_id)
    doc["color_roles"] = deepcopy(config)
    await self._save_guild_doc(guild_id, doc)


SettingsDB.get_color_roles_config = _settingsdb_get_color_roles_config
SettingsDB.set_color_roles_config = _settingsdb_set_color_roles_config



# ===== Role icons cog =====
# Persistência da cog cogs/role_icons/. Mantém o padrão de configs por guild.

def _settingsdb_role_icons_defaults() -> Dict[str, Any]:
    return {
        "connections": [],
        "revision": 0,
    }


def _settingsdb_get_role_icons_config(self, guild_id: int) -> Dict[str, Any]:
    doc = self._get_guild_doc(int(guild_id))
    raw = deepcopy(doc.get("role_icons") or {})
    base = _settingsdb_role_icons_defaults()
    try:
        base["revision"] = max(0, int(raw.get("revision") or 0))
    except Exception:
        base["revision"] = 0
    connections: list[Dict[str, Any]] = []
    seen_roles: set[int] = set()
    for item in raw.get("connections") or []:
        if not isinstance(item, dict):
            continue
        try:
            user_id = int(item.get("user_id") or 0)
            role_id = int(item.get("role_id") or 0)
        except Exception:
            continue
        if role_id <= 0 or role_id in seen_roles:
            continue
        user_id = max(0, user_id)
        seen_roles.add(role_id)
        connections.append({
            "id": str(item.get("id") or str(role_id))[:80],
            "user_id": user_id,
            "role_id": role_id,
            "enabled": bool(item.get("enabled", True)),
            "original_icon_path": str(item.get("original_icon_path") or "")[:500],
            "original_icon_hash": str(item.get("original_icon_hash") or "")[:96],
            "last_color": str(item.get("last_color") or "")[:16],
            "last_rendered_hash": str(item.get("last_rendered_hash") or "")[:96],
            "last_status": str(item.get("last_status") or "")[:220],
            "last_updated_at": str(item.get("last_updated_at") or "")[:80],
            "created_at": str(item.get("created_at") or "")[:80],
            "updated_at": str(item.get("updated_at") or "")[:80],
        })
        if len(connections) >= 10:
            break
    base["connections"] = connections
    return base


async def _settingsdb_set_role_icons_config(self, guild_id: int, config: Dict[str, Any]):
    doc = self._get_guild_doc(int(guild_id))
    doc["role_icons"] = deepcopy(config)
    await self._save_guild_doc(int(guild_id), doc)


SettingsDB.get_role_icons_config = _settingsdb_get_role_icons_config
SettingsDB.set_role_icons_config = _settingsdb_set_role_icons_config


# ===== Forms cog =====
# Persistência da cog cogs/forms/. Segue o padrão monkey-patch usado pra
# color_roles acima — mantém o SettingsDB original limpo e adiciona métodos
# por extensão.

def _settingsdb_forms_defaults() -> Dict[str, Any]:
    return {
        "enabled": False,
        "form_channel_id": 0,
        "responses_channel_id": 0,
        "active_message_id": 0,
        "active_c_trigger": {"channel_id": 0, "message_id": 0},
        "active_c_panel": {"channel_id": 0, "message_id": 0},
        "pending_reviews": [],
        "panel": {
            "title": "📝 Formulário de verificação",
            "description": "Clique no botão abaixo pra preencher sua verificação.",
            "button_label": "Preencher formulário",
            "button_emoji": "📝",
            "button_style": "primary",
            "media_url": "",
            "accent_color": "#5865F2",
        },
        "modal": {
            "title": "Nova verificação",
            "fields": [
                {
                    "id": "field1",
                    "label": "Nome",
                    "placeholder": "Leonardo",
                    "response_label": "Nome",
                    "required": True,
                    "long": False,
                    "show_in_response": True,
                    "enabled": True,
                    "min_length": 0,
                    "max_length": 120,
                },
                {
                    "id": "field2",
                    "label": "Idade e pronome",
                    "placeholder": "17, ele/dele",
                    "response_label": "Idade e pronome",
                    "required": True,
                    "long": False,
                    "show_in_response": True,
                    "enabled": True,
                    "min_length": 0,
                    "max_length": 120,
                },
                {
                    "id": "field3",
                    "label": "Descrição",
                    "placeholder": "Conta um pouco sobre você...",
                    "response_label": "Descrição",
                    "required": True,
                    "long": True,
                    "show_in_response": True,
                    "enabled": True,
                    "min_length": 0,
                    "max_length": 1000,
                },
            ],
            "field1_label": "Nome",
            "field1_placeholder": "Leonardo",
            "field1_required": True,
            "field2_label": "Idade e pronome",
            "field2_placeholder": "17, ele/dele",
            "field2_required": True,
            "field3_label": "Descrição",
            "field3_placeholder": "Conta um pouco sobre você...",
            "field3_required": True,
        },
        "response": {
            "title": "Nova Verificação",
            "intro": "",
            "footer": "Enviado por {user} • ID `{user_id}`",
            "media_url": "",
            "accent_color": "#5865F2",
        },
        "approval": {
            "enabled": False,
            "role_id": 0,
            "approve_label": "Aprovar",
            "approve_emoji": "✅",
            "approve_style": "success",
            "reject_label": "Rejeitar",
            "reject_emoji": "❌",
            "reject_style": "danger",
            "approve_dm": "✅ **Você foi aprovado em {guild}!**\nO cargo de aprovado foi aplicado, quando configurado pela staff.",
            "reject_dm": "❌ **Você foi rejeitado em {guild}.**\nConfira as regras e tente novamente se a staff permitir.",
        },
    }


def _settingsdb_get_forms_config(self, guild_id: int) -> Dict[str, Any]:
    """Lê a config da cog forms pra uma guild, mesclando com defaults.

    Sanitiza tipos pra resistir a documentos antigos sem todos os campos.
    """
    doc = self._get_guild_doc(guild_id)
    raw = deepcopy(doc.get("forms") or {})
    base = _settingsdb_forms_defaults()

    if "enabled" in raw:
        base["enabled"] = bool(raw.get("enabled"))
    else:
        base["enabled"] = bool(
            int(raw.get("form_channel_id") or 0)
            and int(raw.get("responses_channel_id") or 0)
            and int(raw.get("active_message_id") or 0)
        )

    base["form_channel_id"] = int(raw.get("form_channel_id") or 0)
    base["responses_channel_id"] = int(raw.get("responses_channel_id") or 0)
    base["active_message_id"] = int(raw.get("active_message_id") or 0)

    for key in ("active_c_trigger", "active_c_panel"):
        entry = raw.get(key) or {}
        base[key] = {
            "channel_id": int(entry.get("channel_id") or 0),
            "message_id": int(entry.get("message_id") or 0),
        }

    pending_reviews = raw.get("pending_reviews") or []
    if isinstance(pending_reviews, list):
        normalized_pending = []
        seen_messages = set()
        for item in pending_reviews:
            if not isinstance(item, dict):
                continue
            message_id = int(item.get("message_id") or 0)
            channel_id = int(item.get("channel_id") or 0)
            applicant_id = int(item.get("applicant_id") or 0)
            if not (message_id and channel_id and applicant_id) or message_id in seen_messages:
                continue
            seen_messages.add(message_id)
            values = item.get("field_values") or {}
            if not isinstance(values, dict):
                values = {}
            normalized_pending.append({
                "message_id": message_id,
                "channel_id": channel_id,
                "applicant_id": applicant_id,
                "field_values": {str(k): str(v or "") for k, v in values.items()},
                "status": "pending",
            })
        base["pending_reviews"] = normalized_pending[-250:]

    panel = raw.get("panel") or {}
    for k in ("title", "description", "button_label", "button_emoji", "button_style", "media_url", "accent_color"):
        v = panel.get(k)
        if v is not None:
            base["panel"][k] = str(v)

    modal = raw.get("modal") or {}
    # Migração leve do modelo antigo (idade/pronome + descrição) para 3 campos.
    if "field1_label" not in modal and any(k in modal for k in ("age_label", "desc_label")):
        old_age_label = str(modal.get("age_label") or base["modal"]["field2_label"])
        old_age_ph = str(modal.get("age_placeholder") or base["modal"]["field2_placeholder"])
        old_desc_label = str(modal.get("desc_label") or base["modal"]["field3_label"])
        old_desc_ph = str(modal.get("desc_placeholder") or base["modal"]["field3_placeholder"])
        base["modal"].update({
            "title": str(modal.get("title") or base["modal"]["title"]),
            "field2_label": old_age_label,
            "field2_placeholder": old_age_ph,
            "field3_label": old_desc_label,
            "field3_placeholder": old_desc_ph,
        })
    if isinstance(modal.get("fields"), list):
        base["modal"]["fields"] = deepcopy(modal.get("fields") or [])

    for k in (
        "title",
        "field1_label", "field1_placeholder", "field2_label", "field2_placeholder", "field3_label", "field3_placeholder",
    ):
        v = modal.get(k)
        if v is not None:
            base["modal"][k] = str(v)
    for k in ("field1_required", "field2_required", "field3_required"):
        if k in modal:
            base["modal"][k] = bool(modal.get(k))
    if not isinstance(modal.get("fields"), list):
        # Deixa o FormsCog converter field1/field2/field3 legados para a lista dinâmica.
        base["modal"].pop("fields", None)

    response = raw.get("response") or {}
    # Migração leve do template antigo para intro/footer, sem quebrar o layout novo.
    if "title" not in response and any(k in response for k in ("header", "body")):
        body = str(response.get("body") or "").strip()
        if body and body != "{descricao}":
            base["response"]["intro"] = body
    for k in ("title", "intro", "footer", "media_url", "accent_color"):
        v = response.get(k)
        if v is not None:
            base["response"][k] = str(v)

    approval = raw.get("approval") or {}
    for k in ("approve_label", "approve_emoji", "approve_style", "reject_label", "reject_emoji", "reject_style", "approve_dm", "reject_dm"):
        v = approval.get(k)
        if v is not None:
            base["approval"][k] = str(v)
    base["approval"]["enabled"] = bool(approval.get("enabled", base["approval"]["enabled"]))
    base["approval"]["role_id"] = int(approval.get("role_id") or 0)

    return base


async def _settingsdb_set_forms_config(self, guild_id: int, config: Dict[str, Any]):
    doc = self._get_guild_doc(guild_id)
    doc["forms"] = deepcopy(config)
    await self._save_guild_doc(guild_id, doc)


SettingsDB.get_forms_config = _settingsdb_get_forms_config
SettingsDB.set_forms_config = _settingsdb_set_forms_config


# -----------------------------------------------------------------------------
# Estatísticas persistentes de TTS sintetizado por guild/engine.
# Usado pelo /vps > Servidores. Fica no mesmo documento de guild do Mongo para
# sobreviver a reinícios sem criar outro storage.
# -----------------------------------------------------------------------------

_TTS_SYNT_ENGINE_ALIASES = {
    "edge": "edge",
    "edge_tts": "edge",
    "microsoft": "edge",
    "microsoft_edge": "edge",
    "gtts": "gtts",
    "google": "gtts",
    "gcloud": "gtts",
    "google_cloud": "gtts",
    "googlecloud": "gtts",
    "google_tts": "gtts",
    "google_translate": "gtts",
    "google_translate_tts": "gtts",
    "android_native": "android_native",
    "android": "android_native",
    "android_tts": "android_native",
    "native_android": "android_native",
    "atts": "android_native",
    "piper": "piper",
    "piper_tts": "piper",
    "teto": "teto",
    "teto_utau": "teto",
    "kasane_teto": "teto",
    "utau": "teto",
}
_TTS_SYNT_ENGINE_KEYS = ("android_native", "edge", "gtts", "piper", "teto", "other")


def _normalize_tts_synt_engine(engine: object) -> str:
    value = str(engine or "gtts").strip().lower().replace("-", "_").replace(" ", "_")
    if value.startswith("tts_agent:"):
        value = value.split(":", 1)[1] or "other"
    return _TTS_SYNT_ENGINE_ALIASES.get(value, "other")


def _normalize_tts_synt_stats(raw: object) -> Dict[str, int]:
    data = raw if isinstance(raw, dict) else {}
    engines = data.get("engines") if isinstance(data.get("engines"), dict) else data
    result = {key: 0 for key in _TTS_SYNT_ENGINE_KEYS}
    for key, value in dict(engines or {}).items():
        if str(key).strip().lower() in {"total", "updated_at"}:
            continue
        engine = _normalize_tts_synt_engine(key)
        try:
            amount = max(0, int(value or 0))
        except Exception:
            amount = 0
        result[engine] = int(result.get(engine, 0) or 0) + amount
    return result


async def _settingsdb_increment_tts_synt_count(self, guild_id: int, engine: str, amount: int = 1):
    try:
        gid = int(guild_id)
        delta = max(1, int(amount or 1))
    except Exception:
        return
    if gid <= 0:
        return

    engine_key = _normalize_tts_synt_engine(engine)
    lock = getattr(self, "_tts_synts_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(self, "_tts_synts_lock", lock)

    async with lock:
        doc = self.guild_cache.get(gid)
        if doc is None:
            doc = {"type": "guild", "guild_id": gid}
            self.guild_cache[gid] = doc

        current = _normalize_tts_synt_stats(doc.get("tts_synts"))
        current[engine_key] = int(current.get(engine_key, 0) or 0) + delta
        total = sum(int(current.get(key, 0) or 0) for key in _TTS_SYNT_ENGINE_KEYS)
        doc["tts_synts"] = {
            "engines": current,
            "total": total,
            "updated_at": time.time(),
        }

        await self.coll.update_one(
            {"type": "guild", "guild_id": gid},
            {
                "$set": {
                    "type": "guild",
                    "guild_id": gid,
                    "tts_synts.updated_at": doc["tts_synts"]["updated_at"],
                },
                "$inc": {
                    f"tts_synts.engines.{engine_key}": delta,
                    "tts_synts.total": delta,
                },
            },
            upsert=True,
        )


def _settingsdb_get_tts_synt_stats(self, guild_id: int) -> Dict[str, int]:
    try:
        gid = int(guild_id)
    except Exception:
        return {key: 0 for key in _TTS_SYNT_ENGINE_KEYS}
    doc = self.guild_cache.get(gid, {})
    return _normalize_tts_synt_stats(doc.get("tts_synts"))


def _settingsdb_get_all_tts_synt_stats(self) -> Dict[int, Dict[str, int]]:
    result: Dict[int, Dict[str, int]] = {}
    for guild_id, doc in list(self.guild_cache.items()):
        try:
            gid = int(guild_id)
        except Exception:
            continue
        result[gid] = _normalize_tts_synt_stats((doc or {}).get("tts_synts"))
    return result


SettingsDB.increment_tts_synt_count = _settingsdb_increment_tts_synt_count
SettingsDB.get_tts_synt_stats = _settingsdb_get_tts_synt_stats
SettingsDB.get_all_tts_synt_stats = _settingsdb_get_all_tts_synt_stats


# -----------------------------------------------------------------------------
# Tickets / atendimento modular.
# Mantém a configuração por guild no mesmo documento usado pelas outras cogs.
# -----------------------------------------------------------------------------

def _settingsdb_tickets_defaults() -> Dict[str, Any]:
    try:
        from cogs.tickets.constants import default_ticket_config
        return default_ticket_config()
    except Exception:
        return {
            "feature_enabled": False,
            "panel": {"channel_id": 0, "message_id": 0, "title": "🎫 Atendimento", "description": "Escolha abaixo o tipo de atendimento.", "placeholder": "Escolha uma opção", "accent_color": "#5865F2"},
            "channels": {"category_id": 0, "logs_channel_id": 0, "suggestions_channel_id": 0},
            "roles": {"staff_role_id": 0, "partnership_staff_role_id": 0, "report_staff_role_id": 0, "other_staff_role_id": 0},
            "enabled": {"partnership": True, "report": True, "suggestion": True, "other": True},
            "options": {"allow_multiple_open_tickets": False, "transcript_on_close": True, "use_server_webhook": False},
            "permissions": {
                "everyone": {"view_channel": False, "send_messages": False, "read_message_history": False, "attach_files": False, "embed_links": False, "add_reactions": False},
                "staff": {"view_channel": True, "send_messages": True, "read_message_history": True, "attach_files": True, "embed_links": True, "add_reactions": True, "manage_messages": True, "manage_channels": False},
                "creator": {"view_channel": True, "send_messages": True, "read_message_history": True, "attach_files": True, "embed_links": True, "add_reactions": True, "mention_everyone": False},
            },
            "texts": {},
            "report_types": ["Spam", "Flood", "Ofensa", "Assédio", "Golpe", "Divulgação indevida", "Conteúdo impróprio", "Raid", "Fake account", "Outro"],
            "next_ticket_number": 1,
            "active_tickets": [],
        }


def _settingsdb_get_tickets_config(self, guild_id: int) -> Dict[str, Any]:
    doc = self._get_guild_doc(guild_id)
    raw = deepcopy(doc.get("tickets") or {})
    try:
        from cogs.tickets.utils import sanitize_config
        return sanitize_config(raw)
    except Exception:
        base = _settingsdb_tickets_defaults()
        for key, value in raw.items() if isinstance(raw, dict) else []:
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key].update(value)
            else:
                base[key] = deepcopy(value)
        return base


async def _settingsdb_set_tickets_config(self, guild_id: int, config: Dict[str, Any]):
    doc = self._get_guild_doc(guild_id)
    try:
        from cogs.tickets.utils import sanitize_config
        doc["tickets"] = sanitize_config(config)
    except Exception:
        doc["tickets"] = deepcopy(config)
    await self._save_guild_doc(guild_id, doc)


SettingsDB.get_tickets_config = _settingsdb_get_tickets_config
SettingsDB.set_tickets_config = _settingsdb_set_tickets_config


# -----------------------------------------------------------------------------
# Antibot / canal armadilha.
# Configuração pequena por guild; a cog mantém o canal ativo em memória e não
# consulta o Mongo no caminho comum das mensagens.
# -----------------------------------------------------------------------------

def _settingsdb_normalize_antibot_config(raw: Any) -> Dict[str, Any]:
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    try:
        channel_id = max(0, int(raw.get("channel_id") or 0))
    except Exception:
        channel_id = 0
    try:
        warning_message_id = max(0, int(raw.get("warning_message_id") or 0))
    except Exception:
        warning_message_id = 0
    try:
        updated_by = max(0, int(raw.get("updated_by") or 0))
    except Exception:
        updated_by = 0
    try:
        revision = max(0, int(raw.get("revision") or 0))
    except Exception:
        revision = 0
    return {
        "enabled": bool(raw.get("enabled")) and channel_id > 0,
        "channel_id": channel_id,
        "warning_message_id": warning_message_id,
        "updated_by": updated_by,
        "revision": revision,
    }


def _settingsdb_get_antibot_config(self, guild_id: int) -> Dict[str, Any]:
    doc = self._get_guild_doc(int(guild_id))
    return _settingsdb_normalize_antibot_config(doc.get("antibot"))


def _settingsdb_iter_antibot_configs(self) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for guild_id, doc in list(self.guild_cache.items()):
        config = _settingsdb_normalize_antibot_config((doc or {}).get("antibot"))
        if config.get("enabled") and int(config.get("channel_id") or 0) > 0:
            result[int(guild_id)] = config
    return result


async def _settingsdb_set_antibot_config(self, guild_id: int, config: Dict[str, Any]):
    guild_id = int(guild_id)
    normalized = _settingsdb_normalize_antibot_config(config)
    # Persistimos primeiro e só então publicamos no cache. Assim uma falha do
    # Mongo nunca deixa painel e caminho quente discordando do estado durável.
    await self.coll.update_one(
        {"type": "guild", "guild_id": guild_id},
        {
            "$set": {
                "type": "guild",
                "guild_id": guild_id,
                "antibot": normalized,
            }
        },
        upsert=True,
    )
    doc = self._get_guild_doc(guild_id)
    doc["type"] = "guild"
    doc["guild_id"] = guild_id
    doc["antibot"] = normalized
    self.guild_cache[guild_id] = doc


SettingsDB.get_antibot_config = _settingsdb_get_antibot_config
SettingsDB.iter_antibot_configs = _settingsdb_iter_antibot_configs
SettingsDB.set_antibot_config = _settingsdb_set_antibot_config
