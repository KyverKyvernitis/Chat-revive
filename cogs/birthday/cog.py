from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from .constants import (
    BIRTHDAY_DOC_CONFIG,
    BIRTHDAY_DOC_ENTRY,
    BIRTHDAY_DOC_SENT,
    BIRTHDAY_THREAD_NAME,
    DEFAULT_DELETE_AFTER,
    DEFAULT_REACTION,
    DEFAULT_TEMPLATES,
    DEFAULT_TIMEZONE,
)
from .helpers import (
    _age_for,
    _announcement_time_from_config,
    _birthday_date,
    _birthday_date_full,
    _birthday_timestamp,
    _channel_mention,
    _clean_public_calendar_body,
    _display_sort_key,
    _is_leap,
    _make_notice_view,
    _member_display,
    _next_occurrence,
    _parse_date,
    _replace_vars,
    _trim,
    _utcnow,
    _valid_birthday,
)
from .models import CalendarEntry
from .calendar_renderer import render_calendar_entries
from .ui import BirthdayAdminView

log = logging.getLogger(__name__)


class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_locks: dict[int, asyncio.Lock] = {}
        self._last_tick_key: str | None = None

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._ensure_indexes()
        self.birthday_daily_loop.start()

    async def cog_unload(self):
        self.birthday_daily_loop.cancel()

    async def _ensure_indexes(self):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            await db.coll.create_index([("type", 1), ("guild_id", 1)], name="birthday_type_guild")
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("user_id", 1)], name="birthday_entry_user")
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("month", 1), ("day", 1)], name="birthday_entry_date")
        except Exception as exc:
            log.warning("falha ao criar índices de aniversário: %s", exc)

    def _normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        cfg = dict(config or {})
        if "enabled" in cfg:
            cfg["enabled"] = bool(cfg.get("enabled"))
        else:
            cfg["enabled"] = bool(int(cfg.get("register_channel_id") or 0) or int(cfg.get("announce_channel_id") or 0))
        cfg.setdefault("type", BIRTHDAY_DOC_CONFIG)
        cfg.setdefault("templates", {})
        templates = dict(DEFAULT_TEMPLATES)
        templates.update({k: str(v) for k, v in dict(cfg.get("templates") or {}).items() if k in DEFAULT_TEMPLATES})
        cfg["templates"] = templates
        opts = {
            "allow_update": True,
            "show_age": True,
            "group_announcements": True,
            "delete_on_leave": True,
            "leap_day_mode": "feb28",
            "valid_reaction": DEFAULT_REACTION,
        }
        opts.update(dict(cfg.get("options") or {}))
        cfg["options"] = opts
        announce_hour, announce_minute = _announcement_time_from_config(cfg)
        cfg["announce_hour"] = announce_hour
        cfg["announce_minute"] = announce_minute
        cfg.setdefault("timezone", DEFAULT_TIMEZONE)
        return cfg

    async def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return self._normalize_config({"guild_id": int(guild_id)})
        doc = await db.coll.find_one({"type": BIRTHDAY_DOC_CONFIG, "guild_id": int(guild_id)}, {"_id": 0})
        cfg = self._normalize_config(doc or {"guild_id": int(guild_id)})
        try:
            cfg["birthday_count"] = await db.coll.count_documents({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id)})
        except Exception:
            cfg["birthday_count"] = 0
        return cfg

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        cfg = self._normalize_config(config)
        cfg["guild_id"] = int(guild_id)
        cfg["type"] = BIRTHDAY_DOC_CONFIG
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_CONFIG, "guild_id": int(guild_id)},
            {"$set": cfg},
            upsert=True,
        )

    async def _update_config(self, guild_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        cfg = await self._get_config(int(guild_id))
        if any(key in updates and int(updates.get(key) or 0) > 0 for key in ("register_channel_id", "announce_channel_id")):
            cfg["enabled"] = True
        for key, value in updates.items():
            if key == "options":
                opts = dict(cfg.get("options") or {})
                opts.update(dict(value or {}))
                cfg["options"] = opts
            elif key == "templates":
                templates = dict(cfg.get("templates") or {})
                templates.update(dict(value or {}))
                cfg["templates"] = templates
            else:
                cfg[key] = value
        await self._save_config(int(guild_id), cfg)
        return cfg

    async def _set_template(self, guild: discord.Guild, key: str, template: str):
        cfg = await self._get_config(int(guild.id))
        templates = dict(cfg.get("templates") or {})
        templates[str(key)] = str(template or "")
        await self._update_config(int(guild.id), {"templates": templates})
        if key == "calendar":
            await self._sync_public_calendar(guild)

    def _can_manage(self, user: discord.abc.User) -> bool:
        perms = getattr(user, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        gid = int(guild_id)
        lock = self._sync_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[gid] = lock
        return lock


    async def _resolve_text_channel(self, guild: discord.Guild | None, selected: Any) -> discord.TextChannel | None:
        if guild is None or selected is None:
            return None
        if isinstance(selected, discord.TextChannel):
            return selected
        try:
            channel_id = int(getattr(selected, "id", 0) or 0)
        except Exception:
            channel_id = 0
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _missing_channel_permissions(self, channel: discord.TextChannel, *, for_register: bool) -> str:
        guild = channel.guild
        me = guild.me or (guild.get_member(int(self.bot.user.id)) if self.bot.user else None)
        if me is None:
            return "Não consegui verificar minhas permissões nesse canal."
        perms = channel.permissions_for(me)
        needed = [
            ("view_channel", "ver o canal"),
            ("send_messages", "enviar mensagens"),
        ]
        if for_register:
            needed.extend([
                ("create_public_threads", "criar threads públicas"),
                ("send_messages_in_threads", "enviar mensagens em threads"),
                ("read_message_history", "ler histórico de mensagens"),
                ("add_reactions", "adicionar reações"),
                ("manage_messages", "apagar mensagens inválidas"),
            ])
        missing = [label for attr, label in needed if not bool(getattr(perms, attr, False))]
        if not missing:
            return ""
        return "Não consigo usar esse canal ainda. Permissões necessárias: " + ", ".join(missing) + "."

    async def _set_register_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        cfg = await self._get_config(int(guild.id))
        current_channel_id = int(cfg.get("register_channel_id") or 0)
        message_id = int(cfg.get("register_message_id") or 0)
        thread_id = int(cfg.get("birthday_thread_id") or 0)
        cfg["register_channel_id"] = int(channel.id)
        cfg["enabled"] = True
        if message_id and current_channel_id == int(channel.id):
            await self._save_config(int(guild.id), cfg)
            await self._sync_public_calendar(guild)
            return

        # Primeira publicação do sistema, ou troca explícita de canal. O painel não
        # oferece fluxo de recriação; quando o local muda, o novo local vira a fonte
        # salva e as próximas mudanças são sempre edições desse registro.
        view = await self._make_calendar_view(guild, cfg)
        msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        cfg["register_message_id"] = int(msg.id)
        try:
            thread = await msg.create_thread(name=BIRTHDAY_THREAD_NAME, auto_archive_duration=10080)
            cfg["birthday_thread_id"] = int(thread.id)
        except discord.HTTPException as exc:
            log.warning("não consegui criar thread de aniversários: %s", exc)
            cfg["birthday_thread_id"] = thread_id or 0
        await self._save_config(int(guild.id), cfg)
        await self._sync_public_calendar(guild)

    async def _calendar_entries(self, guild: discord.Guild, *, month_filter: int | None = None, cleanup_missing: bool = True) -> list[CalendarEntry]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return []
        cfg = await self._get_config(int(guild.id))
        opts = cfg["options"]
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        query: dict[str, Any] = {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild.id)}
        if month_filter:
            query["month"] = int(month_filter)
        docs = []
        cursor = db.coll.find(query, {"_id": 0}).sort([("month", 1), ("day", 1)])
        async for doc in cursor:
            docs.append(doc)
        result: list[CalendarEntry] = []
        stale: list[int] = []
        for doc in docs:
            try:
                uid = int(doc.get("user_id") or 0)
                day = int(doc.get("day") or 0)
                month = int(doc.get("month") or 0)
            except Exception:
                continue
            if not uid or not _valid_birthday(day, month, None):
                continue
            member = guild.get_member(uid)
            missing_confirmed = False
            if member is None and cleanup_missing:
                try:
                    member = await guild.fetch_member(uid)
                except discord.NotFound:
                    missing_confirmed = True
                except discord.HTTPException as exc:
                    # Falha de fetch não significa que o usuário saiu. Manter o
                    # registro evita apagar aniversários válidos quando o cache de
                    # membros não está completo ou a API falha temporariamente.
                    log.debug("não consegui buscar membro do aniversário guild=%s user=%s: %r", guild.id, uid, exc)
                    member = None
            if missing_confirmed:
                stale.append(uid)
                continue
            fallback_name = str(doc.get("display_name") or doc.get("username") or "").strip() or None
            year_raw = doc.get("year")
            try:
                year = int(year_raw) if year_raw else None
            except Exception:
                year = None
            next_dt = _next_occurrence(day, month, now=now, leap_mode=str(opts.get("leap_day_mode") or "feb28"))
            result.append(CalendarEntry(
                user_id=uid,
                day=day,
                month=month,
                year=year,
                display_name=_member_display(member, uid, fallback_name),
                mention=f"<@{uid}>",
                next_dt=next_dt,
            ))
        if stale and cleanup_missing:
            await db.coll.delete_many({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild.id), "user_id": {"$in": stale}})
        result.sort(key=lambda e: (e.next_dt, _display_sort_key(e.display_name)))
        return result

    async def _render_calendar(self, guild: discord.Guild, *, limit: int | None = None, compact: bool = False) -> str:
        entries = await self._calendar_entries(guild, cleanup_missing=False)
        return render_calendar_entries(entries, limit=limit, compact=compact)

    def _base_values(self, guild: discord.Guild, cfg: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        return {
            "guildname": getattr(guild, "name", "Servidor"),
            "guildid": int(getattr(guild, "id", 0) or 0),
            "guildmembercount": int(getattr(guild, "member_count", 0) or 0),
            "announcechannel": _channel_mention(cfg.get("announce_channel_id")),
            "registerchannel": _channel_mention(cfg.get("register_channel_id")),
            "nowtimestamp": int(now.timestamp()),
            "nowdate": now.strftime("%d/%m/%Y"),
            "nowtime": now.strftime("%H:%M"),
        }

    async def _calendar_values(self, guild: discord.Guild, cfg: dict[str, Any]) -> dict[str, Any]:
        entries = await self._calendar_entries(guild, cleanup_missing=False)
        values = self._base_values(guild, cfg)
        values["birthdaycount"] = len(entries)
        values["birthdaycalendar"] = render_calendar_entries(entries)
        values["birthdaycalendarcompact"] = render_calendar_entries(entries, compact=True)
        values["birthdaycalendarnext10"] = render_calendar_entries(entries, limit=10)
        values["birthdaycalendarnext20"] = render_calendar_entries(entries, limit=20)
        calendar_block = render_calendar_entries(entries, include_summary=True)
        values["birthdaycalendarblock"] = f"\n\n{calendar_block}" if calendar_block else ""
        if entries:
            first = min(entries, key=lambda e: (e.next_dt, _display_sort_key(e.display_name)))
            values.update({
                "nextbirthdayname": first.display_name,
                "nextbirthdaydate": _birthday_date(first.day, first.month),
                "nextbirthdaymention": first.mention,
            })
        else:
            values.update({"nextbirthdayname": "", "nextbirthdaydate": "", "nextbirthdaymention": ""})
        return values

    async def _make_calendar_view(self, guild: discord.Guild, cfg: dict[str, Any] | None = None) -> discord.ui.LayoutView:
        cfg = self._normalize_config(cfg or await self._get_config(int(guild.id)))
        values = await self._calendar_values(guild, cfg)
        body = _replace_vars(cfg["templates"].get("calendar") or DEFAULT_TEMPLATES["calendar"], values)
        body = _clean_public_calendar_body(body)
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim(body)),
            accent_color=discord.Color.blurple(),
        ))
        return view

    async def _fetch_public_message(self, guild: discord.Guild, cfg: dict[str, Any]) -> discord.Message | None:
        channel_id = int(cfg.get("register_channel_id") or 0)
        message_id = int(cfg.get("register_message_id") or 0)
        if not channel_id or not message_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException:
            return None

    async def _sync_public_calendar(self, guild: discord.Guild | None) -> bool:
        if guild is None:
            return False
        async with self._lock_for(int(guild.id)):
            cfg = await self._get_config(int(guild.id))
            msg = await self._fetch_public_message(guild, cfg)
            if msg is None:
                return False
            view = await self._make_calendar_view(guild, cfg)
            try:
                await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return True
            except discord.HTTPException as exc:
                log.warning("falha ao editar calendário de aniversários guild=%s: %s", guild.id, exc)
                return False

    async def _send_temp_view(self, message: discord.Message, template_key: str, values: dict[str, Any], *, ok: bool = True):
        cfg = await self._get_config(int(message.guild.id))
        opts = cfg["options"]
        template = cfg["templates"].get(template_key) or DEFAULT_TEMPLATES.get(template_key, "")
        body = _replace_vars(template, values)
        view = _make_notice_view("🎂 Aniversários" if ok else "Aniversários", body, ok=ok)
        try:
            sent = await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return
        if bool(opts.get("temporary_reply", True)):
            delay = int(opts.get("delete_after_seconds") or DEFAULT_DELETE_AFTER)
            asyncio.create_task(self._delete_later(sent, delay=delay))

    async def _delete_later(self, message: discord.Message, *, delay: int):
        await asyncio.sleep(max(1, int(delay)))
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    def _member_values(self, member: discord.Member | discord.User, *, day: int, month: int, year: int | None, cfg: dict[str, Any], user_message: str = "") -> dict[str, Any]:
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        opts = cfg["options"]
        display_name = str(getattr(member, "display_name", None) or getattr(member, "name", None) or member.id)
        values = {
            "usermention": getattr(member, "mention", f"<@{int(member.id)}>") or f"<@{int(member.id)}>",
            "userid": int(member.id),
            "username": str(getattr(member, "name", "") or ""),
            "userdisplayname": display_name,
            "usernickname": display_name,
            "usermessage": user_message,
            "validexample": "23/09",
            "birthdayday": f"{int(day):02d}",
            "birthdaymonth": f"{int(month):02d}",
            "birthdayyear": str(year or ""),
            "birthdaydate": _birthday_date(day, month),
            "birthdaydatefull": _birthday_date_full(day, month, year),
            "birthdayage": _age_for(year, day=day, month=month, now=now),
            "birthdaytimestamp": _birthday_timestamp(day, month, now=now, leap_mode=str(opts.get("leap_day_mode") or "feb28")),
            "nowtimestamp": int(now.timestamp()),
        }
        return values

    async def _send_template_preview(self, interaction: discord.Interaction, key: str, *, use_default: bool = False):
        cfg = await self._get_config(int(interaction.guild.id))
        template = DEFAULT_TEMPLATES.get(key, "") if use_default else cfg["templates"].get(key, DEFAULT_TEMPLATES.get(key, ""))
        values = await self._preview_values(interaction.guild, interaction.user, cfg)
        rendered = _replace_vars(template, values)
        await interaction.response.send_message(
            view=_make_notice_view("Prévia", rendered, ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _preview_values(self, guild: discord.Guild, user: discord.Member | discord.User, cfg: dict[str, Any]) -> dict[str, Any]:
        values = await self._calendar_values(guild, cfg)
        values.update(self._member_values(user, day=23, month=9, year=2006, cfg=cfg, user_message="23/09"))
        values.update({
            "birthdaymentions": getattr(user, "mention", f"<@{int(user.id)}>"),
            "birthdaynames": str(getattr(user, "display_name", None) or getattr(user, "name", "")),
            "birthdaylist": f"• {getattr(user, 'mention', f'<@{int(user.id)}>')} — 23/09",
            "birthdaylistnumbered": f"1. {getattr(user, 'mention', f'<@{int(user.id)}>')} — 23/09",
            "birthdaycount": values.get("birthdaycount") or 1,
        })
        return values

    async def _handle_test_action(self, interaction: discord.Interaction, action: str):
        cfg = await self._get_config(int(interaction.guild.id))
        if action == "send":
            channel_id = int(cfg.get("announce_channel_id") or 0)
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    view=_make_notice_view("Canal não configurado", "Escolha o canal de avisos antes de enviar um teste.", ok=False),
                    ephemeral=True,
                )
                return
            values = await self._preview_values(interaction.guild, interaction.user, cfg)
            body = _replace_vars(cfg["templates"].get("announce_single") or DEFAULT_TEMPLATES["announce_single"], values)
            await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            await interaction.response.send_message(view=_make_notice_view("Teste enviado", f"Enviei a prévia em {channel.mention}.", ok=True), ephemeral=True)
            return
        key_map = {"calendar": "calendar", "single": "announce_single", "group": "announce_group"}
        await self._send_template_preview(interaction, key_map.get(action, "calendar"))

    async def _upsert_birthday(self, guild_id: int, user_id: int, *, day: int, month: int, year: int | None, member: discord.Member | discord.User | None = None) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return False
        existing = await db.coll.find_one({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)}, {"_id": 0})
        now_iso = _utcnow().isoformat()
        update = {
            "type": BIRTHDAY_DOC_ENTRY,
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "day": int(day),
            "month": int(month),
            "year": int(year) if year else None,
            "updated_at": now_iso,
        }
        if member is not None:
            update["display_name"] = str(getattr(member, "display_name", None) or getattr(member, "name", None) or user_id)
            update["username"] = str(getattr(member, "name", "") or "")
        # created_at não pode aparecer ao mesmo tempo em $set e $setOnInsert.
        # Em MongoDB isso causa conflito no upsert quando o registro ainda não existe.
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)},
            {
                "$set": update,
                "$setOnInsert": {
                    "created_at": now_iso,
                },
            },
            upsert=True,
        )
        return bool(existing)

    async def _remove_birthday(self, guild_id: int, user_id: int) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return False
        res = await db.coll.delete_many({"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)})
        await db.coll.delete_many({"type": BIRTHDAY_DOC_SENT, "guild_id": int(guild_id), "user_id": int(user_id)})
        return bool(getattr(res, "deleted_count", 0))

    def _is_birthday_thread(self, thread: discord.Thread, cfg: dict[str, Any]) -> bool:
        thread_id = int(cfg.get("birthday_thread_id") or 0)
        channel_id = int(getattr(thread, "id", 0) or 0)
        if thread_id and channel_id == thread_id:
            return True

        register_channel_id = int(cfg.get("register_channel_id") or 0)
        if not register_channel_id:
            return False
        if str(getattr(thread, "name", "") or "") != BIRTHDAY_THREAD_NAME:
            return False

        parent_id = int(getattr(thread, "parent_id", 0) or 0)
        if parent_id == register_channel_id:
            return True

        parent = getattr(thread, "parent", None)
        try:
            if int(getattr(parent, "id", 0) or 0) == register_channel_id:
                return True
        except Exception:
            pass
        return False

    @commands.Cog.listener("on_message")
    async def birthday_thread_message_listener(self, message: discord.Message):
        if getattr(getattr(message, "author", None), "bot", False) or message.guild is None:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        cfg = await self._get_config(int(message.guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        channel_id = int(getattr(message.channel, "id", 0) or 0)
        if not self._is_birthday_thread(message.channel, cfg):
            return

        saved_thread_id = int(cfg.get("birthday_thread_id") or 0)
        if channel_id and saved_thread_id != channel_id:
            await self._update_config(int(message.guild.id), {"birthday_thread_id": channel_id})
            cfg = await self._get_config(int(message.guild.id))

        parsed = _parse_date(message.content or "")
        if parsed is None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

        day, month, year = parsed
        try:
            await self._upsert_birthday(message.guild.id, message.author.id, day=day, month=month, year=year, member=message.author)
        except Exception as exc:
            log.warning("falha ao salvar aniversário guild=%s user=%s: %r", message.guild.id, message.author.id, exc)
            return
        opts = cfg["options"]
        reaction = str(opts.get("valid_reaction") or DEFAULT_REACTION).strip() or DEFAULT_REACTION
        try:
            await message.add_reaction(reaction)
        except discord.HTTPException as exc:
            log.debug("não consegui reagir à data válida guild=%s message=%s: %r", message.guild.id, message.id, exc)
        await self._sync_public_calendar(message.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        if not bool(cfg.get("options", {}).get("delete_on_leave", True)):
            return
        removed = await self._remove_birthday(int(member.guild.id), int(member.id))
        if removed:
            await self._sync_public_calendar(member.guild)

    @commands.command(name="birthday")
    @commands.guild_only()
    async def birthday_panel(self, ctx: commands.Context):
        if not self._can_manage(ctx.author):
            await ctx.reply(view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False), mention_author=False)
            return
        cfg = await self._get_config(int(ctx.guild.id))
        view = BirthdayAdminView(self, owner_id=int(ctx.author.id), guild_id=int(ctx.guild.id), config=cfg)
        msg = await ctx.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        view.message = msg

    @tasks.loop(minutes=1)
    async def birthday_daily_loop(self):
        tick = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        if tick == self._last_tick_key:
            return
        self._last_tick_key = tick
        for guild in list(self.bot.guilds):
            try:
                await self._maybe_send_daily_announcements(guild)
            except Exception as exc:
                log.warning("falha no envio diário de aniversários guild=%s: %s", getattr(guild, "id", None), exc)

    @birthday_daily_loop.before_loop
    async def before_birthday_daily_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_send_daily_announcements(self, guild: discord.Guild):
        cfg = await self._get_config(int(guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        channel_id = int(cfg.get("announce_channel_id") or 0)
        if not channel_id:
            return
        tz = ZoneInfo(str(cfg.get("timezone") or DEFAULT_TIMEZONE))
        now = datetime.now(tz)
        announce_hour, announce_minute = _announcement_time_from_config(cfg)
        if announce_hour != now.hour or announce_minute != now.minute:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        entries = await self._entries_for_today(guild, now=now, cfg=cfg)
        if not entries:
            return
        unsent = []
        for entry in entries:
            if not await self._sent_this_year(guild.id, entry.user_id, now.year):
                unsent.append(entry)
        if not unsent:
            return
        opts = cfg["options"]
        if bool(opts.get("group_announcements", True)) and len(unsent) > 1:
            values = await self._announcement_group_values(guild, unsent, cfg)
            body = _replace_vars(cfg["templates"].get("announce_group") or DEFAULT_TEMPLATES["announce_group"], values)
            await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            for entry in unsent:
                await self._mark_sent(guild.id, entry.user_id, now.year)
        else:
            for entry in unsent:
                member = guild.get_member(entry.user_id)
                if member is None:
                    continue
                values = self._member_values(member, day=entry.day, month=entry.month, year=entry.year, cfg=cfg)
                values.update(self._base_values(guild, cfg))
                body = _replace_vars(cfg["templates"].get("announce_single") or DEFAULT_TEMPLATES["announce_single"], values)
                await channel.send(content=body, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                await self._mark_sent(guild.id, entry.user_id, now.year)

    async def _entries_for_today(self, guild: discord.Guild, *, now: datetime, cfg: dict[str, Any]) -> list[CalendarEntry]:
        entries = await self._calendar_entries(guild, cleanup_missing=True)
        opts = cfg["options"]
        result = []
        for entry in entries:
            day = entry.day
            month = entry.month
            if month == 2 and day == 29 and not _is_leap(now.year):
                if str(opts.get("leap_day_mode") or "feb28") == "mar01":
                    day, month = 1, 3
                else:
                    day, month = 28, 2
            if day == now.day and month == now.month:
                result.append(entry)
        return result

    async def _announcement_group_values(self, guild: discord.Guild, entries: list[CalendarEntry], cfg: dict[str, Any]) -> dict[str, Any]:
        values = self._base_values(guild, cfg)
        mentions = [entry.mention for entry in entries]
        names = [entry.display_name for entry in entries]
        values.update({
            "birthdaycount": len(entries),
            "birthdaymentions": ", ".join(mentions),
            "birthdaynames": ", ".join(names),
            "birthdaylist": "\n".join(f"• {entry.mention} — {_birthday_date(entry.day, entry.month)}" for entry in entries),
            "birthdaylistnumbered": "\n".join(f"{i}. {entry.mention} — {_birthday_date(entry.day, entry.month)}" for i, entry in enumerate(entries, 1)),
        })
        return values

    async def _sent_this_year(self, guild_id: int, user_id: int, year: int) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return True
        doc = await db.coll.find_one(
            {
                "type": BIRTHDAY_DOC_ENTRY,
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "sent_years": int(year),
            },
            {"_id": 0, "user_id": 1},
        )
        return bool(doc)

    async def _mark_sent(self, guild_id: int, user_id: int, year: int):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        await db.coll.update_one(
            {"type": BIRTHDAY_DOC_ENTRY, "guild_id": int(guild_id), "user_id": int(user_id)},
            {
                "$addToSet": {"sent_years": int(year)},
                "$set": {"last_sent_at": _utcnow().isoformat()},
            },
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
