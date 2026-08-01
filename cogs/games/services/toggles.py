import asyncio
import time
from typing import Any

import discord

from ..constants import (
    _DJ_DURATION_SECONDS,
    _PICA_DURATION_SECONDS,
    _ROLA_DURATION_SECONDS,
)


class GincanaToggleMixin:
    _DJ_PERMISSION_FLAGS = ("use_soundboard", "use_external_sounds")

    def _pica_effect_key(self, user_id: int) -> str:
        return str(int(user_id))

    def _rola_effect_key(self, user_id: int) -> str:
        return str(int(user_id))

    def _dj_effect_key(self, channel_id: int, user_id: int) -> str:
        return f"{int(channel_id)}:{int(user_id)}"

    def _get_gincana_timed_effects(self, guild_id: int) -> dict[str, dict[str, dict[str, Any]]]:
        getter = getattr(self.db, "get_gincana_timed_effects", None)
        if callable(getter):
            return getter(int(guild_id))

        guild_cache = getattr(self.db, "guild_cache", {}) or {}
        raw = (guild_cache.get(int(guild_id), {}) or {}).get("gincana_timed_effects", {}) or {}
        result: dict[str, dict[str, dict[str, Any]]] = {"pica": {}, "dj": {}, "rola": {}}
        for effect_name in ("pica", "dj", "rola"):
            effect_map = raw.get(effect_name, {}) if isinstance(raw, dict) else {}
            if not isinstance(effect_map, dict):
                continue
            for key, record in effect_map.items():
                if isinstance(record, dict):
                    result[effect_name][str(key)] = dict(record)
        return result

    async def _save_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str, record: dict[str, Any]):
        setter = getattr(self.db, "set_gincana_timed_effect", None)
        if callable(setter):
            await setter(int(guild_id), str(effect_name), str(key), dict(record))
            return

        doc = self.db._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}
        bucket = effects.get(str(effect_name), {}) or {}
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[str(key)] = dict(record)
        effects[str(effect_name)] = bucket
        doc["gincana_timed_effects"] = effects
        await self.db._save_guild_doc(int(guild_id), doc)

    async def _remove_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str):
        remover = getattr(self.db, "remove_gincana_timed_effect", None)
        if callable(remover):
            await remover(int(guild_id), str(effect_name), str(key))
            return

        doc = self.db._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}
        bucket = effects.get(str(effect_name), {}) or {}
        if isinstance(bucket, dict):
            bucket.pop(str(key), None)
            effects[str(effect_name)] = bucket
        doc["gincana_timed_effects"] = effects
        await self.db._save_guild_doc(int(guild_id), doc)

    async def _send_toggle_notice(self, message: discord.Message, title: str, lines: list[str], *, ok: bool = True, color: discord.Color | None = None):
        try:
            await message.channel.send(view=self._make_v2_notice(title, lines, ok=ok, accent_color=color))
        except Exception:
            pass

    def _get_overwrite_flag(self, overwrite: discord.PermissionOverwrite, flag: str):
        try:
            return getattr(overwrite, flag)
        except Exception:
            return None

    def _set_overwrite_flag(self, overwrite: discord.PermissionOverwrite, flag: str, value) -> bool:
        try:
            setattr(overwrite, flag, value)
            return True
        except Exception:
            return False

    def _sound_overwrite_snapshot(self, overwrite: discord.PermissionOverwrite) -> dict[str, bool | None]:
        return {flag: self._get_overwrite_flag(overwrite, flag) for flag in self._DJ_PERMISSION_FLAGS}

    async def _get_effect_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            fetched = await guild.fetch_member(int(user_id))
            if isinstance(fetched, discord.Member):
                return fetched
        except Exception:
            return None
        return None

    def _target_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | discord.StageChannel | None:
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    def _voice_state_is_server_muted(self, member: discord.Member) -> bool:
        voice_state = getattr(member, "voice", None)
        try:
            return bool(getattr(voice_state, "mute", False))
        except Exception:
            return False

    def _get_ignored_tts_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = 0
        try:
            role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            role_id = 0
        return guild.get_role(role_id) if role_id else None

    async def _focused_members_for_pica(self, guild: discord.Guild) -> list[discord.Member]:
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return []

        members: list[discord.Member] = []
        seen: set[int] = set()
        own_bot_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        for user_id in sorted(self._expand_gincana_focus_ids(guild.id, focus_map.keys())):
            try:
                uid = int(user_id)
            except Exception:
                continue
            if uid in seen:
                continue
            member = guild.get_member(uid)
            if member is None:
                try:
                    fetched = await guild.fetch_member(uid)
                except Exception:
                    fetched = None
                member = fetched if isinstance(fetched, discord.Member) else None
            if member is None or self._is_callkeeper_bot(member):
                continue
            if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
                continue
            seen.add(uid)
            members.append(member)
        return members

    def _count_text(self, count: int, singular: str, plural: str) -> str:
        return singular if int(count) == 1 else plural

    def _record_expires_at(self, record: dict[str, Any]) -> float:
        try:
            return float(record.get("expires_at", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _record_is_active(self, record: dict[str, Any], *, now: float | None = None) -> bool:
        if not isinstance(record, dict):
            return False
        return self._record_expires_at(record) > float(now if now is not None else time.time())

    def _record_is_legacy_pica_mute(self, record: dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        return "previous_mute" in record or ("channel_id" in record and "ignored_tts_role_id" not in record)

    # -----------------------------
    # pica: cargo de ignorar TTS
    # -----------------------------

    async def _apply_pica_effect(self, guild: discord.Guild, member: discord.Member, record: dict[str, Any]) -> bool:
        role = self._get_ignored_tts_role(guild)
        if role is None:
            return False
        if role in getattr(member, "roles", []):
            return False
        try:
            await member.add_roles(role, reason="economia pica: ignorar TTS por 6 horas")
        except Exception:
            return False
        try:
            await self._refresh_targets_suffix_nicknames(guild, [member])
        except Exception:
            pass
        return True

    async def _cleanup_pica_effect(self, guild: discord.Guild, user_id: int, record: dict[str, Any], *, force_remove_record: bool = False) -> bool:
        key = self._pica_effect_key(user_id)
        self._pica_expirations.pop((int(guild.id), int(user_id)), None)

        member = await self._get_effect_member(guild, int(user_id))
        if member is None:
            if force_remove_record:
                await self._remove_gincana_timed_effect(guild.id, "pica", key)
                return True
            return False

        role = self._get_ignored_tts_role(guild)
        previous_had_role = bool(record.get("previous_had_role", False))
        if role is not None and not previous_had_role and role in getattr(member, "roles", []):
            try:
                await member.remove_roles(role, reason="economia pica expirado")
            except Exception:
                if not force_remove_record:
                    return False

        try:
            await self._refresh_targets_suffix_nicknames(guild, [member])
        except Exception:
            pass

        await self._remove_gincana_timed_effect(guild.id, "pica", key)
        return True

    async def _expire_pica_later(self, guild_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        except Exception:
            return

        key = (int(guild_id), int(user_id))
        expires_at = float(self._pica_expirations.get(key, 0.0) or 0.0)
        now = time.time()
        if expires_at <= 0 or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            self._pica_expirations.pop(key, None)
            return

        record = self._get_gincana_timed_effects(guild.id).get("pica", {}).get(self._pica_effect_key(user_id), {})
        if not record:
            self._pica_expirations.pop(key, None)
            return
        await self._cleanup_pica_effect(guild, int(user_id), record, force_remove_record=True)

    async def _send_pica_feedback(self, message: discord.Message, *, applied: int, removed: int, skipped: int):
        lines: list[str] = []
        if applied:
            who = self._count_text(applied, "focado vai", "focados vão")
            lines.append(f"**{applied}** {who} ser ignorado pelo TTS por **6 horas**")
        if removed:
            who = self._count_text(removed, "focado voltou", "focados voltaram")
            lines.append(f"**{removed}** {who} a ser lido pelo TTS")
        if not lines:
            if skipped:
                lines.append("Os focados já estavam ignorados pelo TTS")
            else:
                lines.append("Nada mudou por enquanto")
        await self._send_toggle_notice(message, "🔕 TTS ignorado", lines, ok=bool(applied or removed), color=discord.Color.dark_green() if applied or removed else discord.Color.red())

    async def _handle_role_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = message.content or ""
        if not self._matches_exact_trigger(content, "pica"):
            return False

        if not self._is_call_trigger_context(message):
            return True

        if not self.db.gincana_enabled(guild.id):
            return True

        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        if self._is_focused_non_staff_member(message.author):
            return True

        role = self._get_ignored_tts_role(guild)
        if role is None:
            await self._send_toggle_notice(
                message,
                "🔕 Cargo não configurado",
                ["Configure o cargo de ignorar TTS antes de usar pica"],
                ok=False,
                color=discord.Color.red(),
            )
            return True

        targets = await self._focused_members_for_pica(guild)
        if not targets:
            await self._send_toggle_notice(
                message,
                "🔕 Ninguém em foco",
                ["Não tem ninguém na lista de foco agora"],
                ok=False,
                color=discord.Color.red(),
            )
            return True

        now = time.time()
        expires_at = now + float(_PICA_DURATION_SECONDS)
        current_records = self._get_gincana_timed_effects(guild.id).get("pica", {})
        applied = 0
        removed = 0
        skipped = 0

        for target in targets:
            try:
                key = self._pica_effect_key(target.id)
                existing = current_records.get(key, {}) or {}
                if isinstance(existing, dict) and self._record_is_legacy_pica_mute(existing):
                    existing = {}

                if isinstance(existing, dict) and self._record_is_active(existing, now=now):
                    await self._cleanup_pica_effect(guild, target.id, existing, force_remove_record=True)
                    removed += 1
                    continue

                if role in getattr(target, "roles", []):
                    skipped += 1
                    continue

                record = {
                    "user_id": int(target.id),
                    "ignored_tts_role_id": int(role.id),
                    "expires_at": float(expires_at),
                    "previous_had_role": False,
                    "created_at": float(now),
                    "updated_at": float(now),
                }
                changed = await self._apply_pica_effect(guild, target, record)
                if not changed:
                    continue
                await self._save_gincana_timed_effect(guild.id, "pica", key, record)
                self._pica_expirations[(guild.id, target.id)] = expires_at
                self.bot.loop.create_task(self._expire_pica_later(guild.id, target.id, _PICA_DURATION_SECONDS))
                applied += 1
            except Exception:
                pass

        await self._send_pica_feedback(message, applied=applied, removed=removed, skipped=skipped)
        if applied or removed:
            await self._react_with_emoji(message, "✅", keep=False)
        return True

    # -----------------------------
    # rola: mute de voz por 6 horas
    # -----------------------------

    async def _apply_rola_effect(self, guild: discord.Guild, member: discord.Member, record: dict[str, Any]) -> bool:
        voice_channel = self._target_voice_channel(member)
        if voice_channel is None:
            record["pending_mute"] = True
            record["applied_mute"] = False
            return False
        if self._voice_state_is_server_muted(member):
            record["pending_mute"] = False
            record["applied_mute"] = False
            return False
        try:
            await member.edit(mute=True, reason="economia rola: mute por 6 horas")
        except Exception:
            return False
        record["channel_id"] = int(getattr(voice_channel, "id", record.get("channel_id", 0)) or 0)
        record["pending_mute"] = False
        record["applied_mute"] = True
        record["updated_at"] = float(time.time())
        try:
            await self._refresh_targets_suffix_nicknames(guild, [member])
        except Exception:
            pass
        return True

    async def _cleanup_rola_effect(self, guild: discord.Guild, user_id: int, record: dict[str, Any], *, force_remove_record: bool = False) -> bool:
        key = self._rola_effect_key(user_id)
        self._rola_expirations.pop((int(guild.id), int(user_id)), None)

        member = await self._get_effect_member(guild, int(user_id))
        if member is None:
            if force_remove_record:
                await self._remove_gincana_timed_effect(guild.id, "rola", key)
                return True
            return False

        previous_mute = bool(record.get("previous_mute", False))
        applied_mute = bool(record.get("applied_mute", True))
        voice_channel = self._target_voice_channel(member)
        can_finish = True

        if applied_mute and not previous_mute:
            if voice_channel is None:
                can_finish = bool(force_remove_record)
            else:
                try:
                    if self._voice_state_is_server_muted(member):
                        await member.edit(mute=False, reason="economia rola encerrado")
                except Exception:
                    can_finish = bool(force_remove_record)

        try:
            await self._refresh_targets_suffix_nicknames(guild, [member])
        except Exception:
            pass

        if can_finish or force_remove_record:
            await self._remove_gincana_timed_effect(guild.id, "rola", key)
            return True
        return False

    async def _expire_rola_mute_later(self, guild_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        except Exception:
            return

        key = (int(guild_id), int(user_id))
        expires_at = float(self._rola_expirations.get(key, 0.0) or 0.0)
        now = time.time()
        if expires_at <= 0 or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            self._rola_expirations.pop(key, None)
            return

        record = self._get_gincana_timed_effects(guild.id).get("rola", {}).get(self._rola_effect_key(user_id), {})
        if not record:
            self._rola_expirations.pop(key, None)
            return
        await self._cleanup_rola_effect(guild, int(user_id), record, force_remove_record=True)

    async def _send_rola_feedback(self, message: discord.Message, *, applied: int, removed: int, skipped: int, pending: int = 0):
        lines: list[str] = []
        affected = int(applied) + int(pending)
        if affected:
            who = self._count_text(affected, "membro vai", "membros vão")
            lines.append(f"**{affected}** {who} ficar mutado por **6 horas**")
        if pending:
            who = self._count_text(pending, "entra", "entram")
            lines.append(f"**{pending}** {who} no mute assim que chegar na call")
        if removed:
            who = self._count_text(removed, "membro foi", "membros foram")
            lines.append(f"**{removed}** {who} desmutado")
        if not lines:
            if skipped:
                lines.append("Os alvos já estavam mutados por fora")
            else:
                lines.append("Não encontrei ninguém elegível nessa call")
        title = "🔇 Rola atualizada" if affected and removed else ("🔇 Mute aplicado" if affected else ("🔊 Mute removido" if removed else "🔇 Nada mudou"))
        await self._send_toggle_notice(message, title, lines, ok=bool(affected or removed), color=discord.Color.dark_green() if affected or removed else discord.Color.red())

    async def _handle_rola_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = message.content or ""
        if not self._matches_exact_trigger(content, "rola"):
            return False

        if not self.db.gincana_enabled(guild.id):
            return True

        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        if self._is_focused_non_staff_member(message.author):
            return True

        voice_channel = self._call_trigger_channel(message)
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return True

        own_bot_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        targets = [
            target
            for target in self._resolve_targets(guild, voice_channel)
            if not (own_bot_id and int(getattr(target, "id", 0) or 0) == own_bot_id)
        ]
        if not targets:
            await self._send_rola_feedback(message, applied=0, pending=0, removed=0, skipped=0)
            return True

        now = time.time()
        expires_at = now + float(_ROLA_DURATION_SECONDS)
        current_records = self._get_gincana_timed_effects(guild.id).get("rola", {})
        applied = 0
        pending = 0
        removed = 0
        skipped = 0

        for target in targets:
            try:
                key = self._rola_effect_key(target.id)
                existing = current_records.get(key, {}) or {}
                if isinstance(existing, dict) and self._record_is_active(existing, now=now):
                    await self._cleanup_rola_effect(guild, target.id, existing, force_remove_record=True)
                    removed += 1
                    continue

                target_voice_channel = self._target_voice_channel(target)
                if target_voice_channel is not None and self._voice_state_is_server_muted(target):
                    skipped += 1
                    continue

                record = {
                    "user_id": int(target.id),
                    "channel_id": int(getattr(target_voice_channel, "id", voice_channel.id) or voice_channel.id),
                    "expires_at": float(expires_at),
                    "previous_mute": False,
                    "pending_mute": target_voice_channel is None,
                    "applied_mute": False,
                    "created_at": float(now),
                    "updated_at": float(now),
                }
                changed = await self._apply_rola_effect(guild, target, record)
                should_save = bool(changed or record.get("pending_mute"))
                if not should_save:
                    continue
                await self._save_gincana_timed_effect(guild.id, "rola", key, record)
                self._rola_expirations[(guild.id, target.id)] = expires_at
                self.bot.loop.create_task(self._expire_rola_mute_later(guild.id, target.id, _ROLA_DURATION_SECONDS))
                if changed:
                    applied += 1
                else:
                    pending += 1
            except Exception:
                pass

        await self._send_rola_feedback(message, applied=applied, pending=pending, removed=removed, skipped=skipped)
        if applied or pending or removed:
            await self._react_with_emoji(message, "✅", keep=False)
        return True

    # -----------------------------
    # dj: bloqueio de efeitos sonoros
    # -----------------------------

    async def _apply_dj_effect(self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member, record: dict[str, Any]) -> bool:
        try:
            overwrite = channel.overwrites_for(member)
            changed = False
            for flag in self._DJ_PERMISSION_FLAGS:
                if self._set_overwrite_flag(overwrite, flag, False):
                    changed = True
            if not changed:
                return False
            await channel.set_permissions(member, overwrite=overwrite, reason="economia dj trigger")
            return True
        except Exception:
            return False

    async def _cleanup_dj_effect(self, guild: discord.Guild, channel_id: int, user_id: int, record: dict[str, Any], *, force_remove_record: bool = False) -> bool:
        key = self._dj_effect_key(channel_id, user_id)
        self._dj_expirations.pop((int(guild.id), int(channel_id), int(user_id)), None)

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            await self._remove_gincana_timed_effect(guild.id, "dj", key)
            return True

        member = await self._get_effect_member(guild, int(user_id))
        if member is None:
            if force_remove_record:
                await self._remove_gincana_timed_effect(guild.id, "dj", key)
                return True
            return False

        previous = record.get("previous_overwrite", {}) or {}
        if not isinstance(previous, dict):
            previous = {}

        try:
            overwrite = channel.overwrites_for(member)
            for flag in self._DJ_PERMISSION_FLAGS:
                if flag in previous:
                    self._set_overwrite_flag(overwrite, flag, previous.get(flag))
            if overwrite.is_empty():
                await channel.set_permissions(member, overwrite=None, reason="economia dj expirado")
            else:
                await channel.set_permissions(member, overwrite=overwrite, reason="economia dj expirado")
        except Exception:
            if not force_remove_record:
                return False

        await self._remove_gincana_timed_effect(guild.id, "dj", key)
        return True

    async def _expire_dj_block_later(self, guild_id: int, channel_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        except Exception:
            return

        key = (int(guild_id), int(channel_id), int(user_id))
        expires_at = float(self._dj_expirations.get(key, 0.0) or 0.0)
        now = time.time()
        if expires_at <= 0 or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            self._dj_expirations.pop(key, None)
            return

        record = self._get_gincana_timed_effects(guild.id).get("dj", {}).get(self._dj_effect_key(channel_id, user_id), {})
        if not record:
            self._dj_expirations.pop(key, None)
            return
        await self._cleanup_dj_effect(guild, int(channel_id), int(user_id), record)

    async def _send_dj_toggle_feedback(self, message: discord.Message, affected_count: int, voice_channel: discord.VoiceChannel):
        await self._send_toggle_notice(
            message,
            "🎛️ Efeitos bloqueados",
            [f"**{affected_count}** {self._count_text(affected_count, 'membro ficou', 'membros ficaram')} sem efeitos sonoros por **6 horas**"],
            ok=True,
            color=discord.Color.dark_green(),
        )

    async def _handle_dj_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = message.content or ""
        if not self._matches_exact_trigger(content, "dj"):
            return False

        if not self.db.gincana_enabled(guild.id):
            return True

        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        voice_channel = self._call_trigger_channel(message)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        targets = [
            member
            for member in self._resolve_targets(guild, voice_channel)
            if not getattr(member, "bot", False) and not self._is_staff_member(member)
        ]

        if not targets:
            await self._send_toggle_notice(
                message,
                "🎛️ Nenhum alvo encontrado",
                ["Só tinha staff ou ninguém elegível nessa call"],
                ok=False,
                color=discord.Color.red(),
            )
            return True

        now = time.time()
        expires_at = now + float(_DJ_DURATION_SECONDS)
        affected = 0
        current_records = self._get_gincana_timed_effects(guild.id).get("dj", {})

        for target in targets:
            try:
                key = self._dj_effect_key(voice_channel.id, target.id)
                existing = current_records.get(key, {}) or {}
                overwrite = voice_channel.overwrites_for(target)
                previous_overwrite = existing.get("previous_overwrite") if isinstance(existing, dict) else None
                if not isinstance(previous_overwrite, dict):
                    previous_overwrite = self._sound_overwrite_snapshot(overwrite)
                record = {
                    "channel_id": int(voice_channel.id),
                    "user_id": int(target.id),
                    "expires_at": float(expires_at),
                    "previous_overwrite": previous_overwrite,
                    "created_at": float(existing.get("created_at", now) or now) if isinstance(existing, dict) else float(now),
                    "updated_at": float(now),
                }
                applied = await self._apply_dj_effect(guild, voice_channel, target, record)
                if not applied:
                    continue
                await self._save_gincana_timed_effect(guild.id, "dj", key, record)
                self._dj_expirations[(guild.id, voice_channel.id, target.id)] = expires_at
                self.bot.loop.create_task(self._expire_dj_block_later(guild.id, voice_channel.id, target.id, _DJ_DURATION_SECONDS))
                affected += 1
            except Exception:
                pass

        if affected:
            await self._send_dj_toggle_feedback(message, affected, voice_channel)
            await self._react_success_temporarily(message)
        else:
            await self._send_toggle_notice(
                message,
                "🎛️ Nada mudou",
                ["Não consegui bloquear efeitos sonoros para ninguém"],
                ok=False,
                color=discord.Color.red(),
            )
        return True

    # -----------------------------
    # restauração e autocorreção
    # -----------------------------

    async def _migrate_legacy_pica_mute_records(self, guild: discord.Guild, effects: dict[str, dict[str, dict[str, Any]]]):
        pica_records = effects.get("pica") or {}
        if not pica_records:
            return
        for key, record in list(pica_records.items()):
            try:
                if not self._record_is_legacy_pica_mute(record):
                    continue
                user_id = int(record.get("user_id") or key)
                rola_key = self._rola_effect_key(user_id)
                migrated = dict(record)
                migrated["user_id"] = int(user_id)
                migrated["migrated_from"] = "pica"
                await self._save_gincana_timed_effect(guild.id, "rola", rola_key, migrated)
                await self._remove_gincana_timed_effect(guild.id, "pica", str(key))
            except Exception:
                pass

    async def _rehydrate_gincana_timed_effects(self):
        now = time.time()
        for guild in list(getattr(self.bot, "guilds", []) or []):
            effects = self._get_gincana_timed_effects(guild.id)
            await self._migrate_legacy_pica_mute_records(guild, effects)
            effects = self._get_gincana_timed_effects(guild.id)

            for key, record in list((effects.get("pica") or {}).items()):
                try:
                    user_id = int(record.get("user_id") or key)
                    expires_at = self._record_expires_at(record)
                    member = await self._get_effect_member(guild, user_id)
                    if expires_at <= now:
                        await self._cleanup_pica_effect(guild, user_id, record, force_remove_record=True)
                        continue
                    if member is None:
                        self._pica_expirations[(guild.id, user_id)] = expires_at
                        self.bot.loop.create_task(self._expire_pica_later(guild.id, user_id, expires_at - now))
                        continue
                    role = self._get_ignored_tts_role(guild)
                    if role is None or (not bool(record.get("previous_had_role", False)) and role not in getattr(member, "roles", [])):
                        await self._cleanup_pica_effect(guild, user_id, record, force_remove_record=True)
                        continue
                    self._pica_expirations[(guild.id, user_id)] = expires_at
                    self.bot.loop.create_task(self._expire_pica_later(guild.id, user_id, expires_at - now))
                except Exception:
                    pass

            for key, record in list((effects.get("rola") or {}).items()):
                try:
                    user_id = int(record.get("user_id") or key)
                    expires_at = self._record_expires_at(record)
                    member = await self._get_effect_member(guild, user_id)
                    if expires_at <= now:
                        await self._cleanup_rola_effect(guild, user_id, record, force_remove_record=True)
                        continue
                    if member is not None and self._target_voice_channel(member) is not None:
                        applied_mute = bool(record.get("applied_mute", True))
                        if applied_mute:
                            if not self._voice_state_is_server_muted(member):
                                await self._cleanup_rola_effect(guild, user_id, record, force_remove_record=True)
                                continue
                        else:
                            if not self._voice_state_is_server_muted(member):
                                changed = await self._apply_rola_effect(guild, member, record)
                                if changed:
                                    await self._save_gincana_timed_effect(guild.id, "rola", self._rola_effect_key(user_id), record)
                    self._rola_expirations[(guild.id, user_id)] = expires_at
                    self.bot.loop.create_task(self._expire_rola_mute_later(guild.id, user_id, expires_at - now))
                except Exception:
                    pass

            for key, record in list((effects.get("dj") or {}).items()):
                try:
                    user_id = int(record.get("user_id") or 0)
                    channel_id = int(record.get("channel_id") or 0)
                    expires_at = self._record_expires_at(record)
                    if not user_id or not channel_id:
                        await self._remove_gincana_timed_effect(guild.id, "dj", str(key))
                        continue
                    if expires_at <= now:
                        await self._cleanup_dj_effect(guild, channel_id, user_id, record)
                        continue

                    channel = guild.get_channel(channel_id)
                    member = await self._get_effect_member(guild, user_id)
                    if isinstance(channel, discord.VoiceChannel) and member is not None:
                        await self._apply_dj_effect(guild, channel, member, record)
                    self._dj_expirations[(guild.id, channel_id, user_id)] = expires_at
                    self.bot.loop.create_task(self._expire_dj_block_later(guild.id, channel_id, user_id, expires_at - now))
                except Exception:
                    pass

    async def _handle_gincana_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = getattr(member, "guild", None)
        own_bot_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        if guild is None or self._is_callkeeper_bot(member):
            return
        if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
            return

        after_channel = getattr(after, "channel", None)
        if after_channel is None:
            return

        now = time.time()
        effects = self._get_gincana_timed_effects(guild.id)

        rola_record = (effects.get("rola") or {}).get(self._rola_effect_key(member.id))
        if isinstance(rola_record, dict):
            expires_at = self._record_expires_at(rola_record)
            if expires_at <= now:
                await self._cleanup_rola_effect(guild, member.id, rola_record, force_remove_record=True)
            else:
                applied_mute = bool(rola_record.get("applied_mute", True))
                if applied_mute:
                    if not self._voice_state_is_server_muted(member):
                        # Desmute manual: o bot não reaplica, só encerra o efeito salvo.
                        await self._cleanup_rola_effect(guild, member.id, rola_record, force_remove_record=True)
                    else:
                        self._rola_expirations[(guild.id, member.id)] = expires_at
                else:
                    if not self._voice_state_is_server_muted(member):
                        changed = await self._apply_rola_effect(guild, member, rola_record)
                        if changed:
                            await self._save_gincana_timed_effect(guild.id, "rola", self._rola_effect_key(member.id), rola_record)
                    self._rola_expirations[(guild.id, member.id)] = expires_at

        for key, record in list((effects.get("dj") or {}).items()):
            try:
                if int(record.get("user_id") or 0) != int(member.id):
                    continue
                channel_id = int(record.get("channel_id") or 0)
                expires_at = self._record_expires_at(record)
                if expires_at <= now:
                    await self._cleanup_dj_effect(guild, channel_id, member.id, record)
                    continue
                if int(getattr(after_channel, "id", 0) or 0) == channel_id and isinstance(after_channel, discord.VoiceChannel):
                    await self._apply_dj_effect(guild, after_channel, member, record)
                    self._dj_expirations[(guild.id, channel_id, member.id)] = expires_at
            except Exception:
                pass

    async def _handle_gincana_member_update(self, before: discord.Member, after: discord.Member):
        guild = getattr(after, "guild", None)
        own_bot_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        if guild is None or self._is_callkeeper_bot(after):
            return
        if own_bot_id and int(getattr(after, "id", 0) or 0) == own_bot_id:
            return

        role = self._get_ignored_tts_role(guild)
        if role is None:
            return

        before_role_ids = {int(getattr(item, "id", 0) or 0) for item in (getattr(before, "roles", []) or [])}
        after_role_ids = {int(getattr(item, "id", 0) or 0) for item in (getattr(after, "roles", []) or [])}
        if int(role.id) not in before_role_ids or int(role.id) in after_role_ids:
            return

        record = (self._get_gincana_timed_effects(guild.id).get("pica") or {}).get(self._pica_effect_key(after.id))
        if isinstance(record, dict) and self._record_is_active(record):
            await self._cleanup_pica_effect(guild, after.id, record, force_remove_record=True)
