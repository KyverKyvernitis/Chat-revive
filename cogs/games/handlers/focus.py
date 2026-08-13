import re

import discord

from config import OFF_COLOR, ON_COLOR

from ..constants import _FOCUS_WORD_RE


_FOCUS_EMOJI = "<:alvo:1501014211571220543>"
_FOCUS_ALLOWED_MENTIONS = discord.AllowedMentions.none()
_FOCUS_COMMAND_RE = re.compile(r"^\s*focus(?:\s+|$)", re.IGNORECASE)
_FOCUS_ALL_RE = re.compile(r"^\s*focus\s+all\s*$", re.IGNORECASE)
_FOCUS_SYNC_RE = re.compile(r"^\s*focus\s+sync(?:\s+|$)", re.IGNORECASE)
_USER_ID_RE = re.compile(r"(?<!\d)(\d{15,22})(?!\d)")


class GincanaFocusMixin:
    def _focus_mention(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(int(user_id))
        return member.mention if member else f"<@{int(user_id)}>"

    def _format_focus_list(self, guild: discord.Guild) -> str:
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return "Ninguém por enquanto"

        lines = []
        for uid in sorted(self._expand_gincana_focus_ids(guild.id, focus_map.keys())):
            member = guild.get_member(int(uid))
            if member is not None and self._is_callkeeper_bot(member):
                continue
            if int(uid) in self._get_callkeeper_bot_ids():
                continue
            if self.bot.user is not None and int(uid) == int(self.bot.user.id):
                continue
            lines.append(self._focus_mention(guild, int(uid)))
        return "\n".join(lines) if lines else "Ninguém por enquanto"

    def _format_focus_mentions(self, guild: discord.Guild, user_ids: list[int], *, compact_after: int = 5) -> str:
        cleaned: list[int] = []
        seen: set[int] = set()
        for raw_uid in user_ids or []:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid in seen:
                continue
            seen.add(uid)
            cleaned.append(uid)

        if not cleaned:
            return "ninguém"
        if len(cleaned) > compact_after:
            return f"**{len(cleaned)}** membros"
        mentions = [self._focus_mention(guild, uid) for uid in cleaned]
        if len(mentions) == 1:
            return mentions[0]
        return f"{', '.join(mentions[:-1])} e {mentions[-1]}"

    def _build_focus_notice_view(
        self,
        guild: discord.Guild,
        *,
        title: str,
        lines: list[str],
        ok: bool = True,
        show_current: bool = True,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        items: list = [
            discord.ui.TextDisplay(f"# {_FOCUS_EMOJI} {title}"),
            discord.ui.TextDisplay("\n".join(line for line in lines if line).strip() or "Nada mudou por enquanto"),
        ]
        if show_current:
            items.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"## Agora em foco\n{self._format_focus_list(guild)}"),
            ])
        view.add_item(discord.ui.Container(
            *items,
            accent_color=discord.Color(ON_COLOR if ok else OFF_COLOR),
        ))
        return view

    async def _send_focus_notice(
        self,
        message: discord.Message,
        *,
        title: str,
        lines: list[str],
        ok: bool = True,
        show_current: bool = True,
    ):
        guild = message.guild
        if guild is None:
            return
        await message.channel.send(
            view=self._build_focus_notice_view(guild, title=title, lines=lines, ok=ok, show_current=show_current),
            allowed_mentions=_FOCUS_ALLOWED_MENTIONS,
        )

    async def _send_focus_feedback(
        self,
        message: discord.Message,
        *,
        added_ids: list[int],
        removed_ids: list[int],
        errored_ids: list[int] | None = None,
        reset: bool = False,
    ):
        guild = message.guild
        if guild is None:
            return

        if reset:
            await self._send_focus_notice(
                message,
                title="Foco limpo",
                lines=["A lista de foco foi esvaziada"],
                ok=False,
                show_current=False,
            )
            return

        lines: list[str] = []
        if added_ids:
            mentions = self._format_focus_mentions(guild, added_ids)
            verb = "entrou" if len(added_ids) == 1 else "entraram"
            lines.append(f"{mentions} {verb} no modo foco")

        if removed_ids:
            mentions = self._format_focus_mentions(guild, removed_ids)
            verb = "saiu" if len(removed_ids) == 1 else "saíram"
            lines.append(f"{mentions} {verb} do modo foco")

        if errored_ids:
            mentions = self._format_focus_mentions(guild, errored_ids)
            lines.append(f"Não posso colocar {mentions} na lista de foco")

        if not lines:
            lines.append("Nada mudou por enquanto")

        await self._send_focus_notice(
            message,
            title="Foco atualizado",
            lines=lines,
            ok=bool(added_ids or removed_ids),
            show_current=True,
        )

    def _extract_focus_user_ids(self, message: discord.Message) -> list[int]:
        content = str(message.content or "")
        ordered: list[int] = []
        seen: set[int] = set()

        def add(raw_id):
            try:
                uid = int(raw_id)
            except (TypeError, ValueError):
                return
            if uid <= 0 or uid in seen:
                return
            seen.add(uid)
            ordered.append(uid)

        for raw_id in getattr(message, "raw_mentions", []) or []:
            add(raw_id)
        for match in _USER_ID_RE.finditer(content):
            add(match.group(1))
        for member in getattr(message, "mentions", []) or []:
            add(getattr(member, "id", 0))
        return ordered

    async def _get_focus_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            fetched = await guild.fetch_member(int(user_id))
        except Exception:
            return None
        return fetched if isinstance(fetched, discord.Member) else None

    def _focus_member_is_valid(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        if self.bot.user is not None and int(member.id) == int(self.bot.user.id):
            return False
        if self._is_callkeeper_bot(member):
            return False
        return True

    async def _valid_focus_ids(self, guild: discord.Guild, user_ids) -> list[int]:
        valid: list[int] = []
        seen: set[int] = set()
        for raw_uid in user_ids or []:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid in seen:
                continue
            member = await self._get_focus_member(guild, uid)
            if not self._focus_member_is_valid(member):
                continue
            seen.add(uid)
            valid.append(uid)
        return valid

    async def _valid_expanded_focus_ids(self, guild: discord.Guild, user_ids) -> list[int]:
        expanded = self._expand_gincana_focus_ids(guild.id, user_ids)
        return await self._valid_focus_ids(guild, expanded)

    async def _set_focus_final_ids(self, guild: discord.Guild, final_ids: set[int]) -> tuple[list[int], list[int]]:
        current_ids = set(int(uid) for uid in self.db.get_gincana_focus_map(guild.id).keys())
        cleaned = await self._valid_focus_ids(guild, sorted(final_ids))
        cleaned_set = set(cleaned)
        setter = getattr(self.db, "set_gincana_focus_users", None)
        if callable(setter):
            await setter(guild.id, cleaned)
        else:
            # Compatibilidade com bases antigas: reconstrução por clear + toggle.
            await self.db.clear_gincana_focus_users(guild.id)
            if cleaned:
                await self.db.toggle_gincana_focus_users(guild.id, cleaned)
        return sorted(cleaned_set - current_ids), sorted(current_ids - cleaned_set)

    async def _handle_focus_sync(self, message: discord.Message) -> bool:
        guild = message.guild
        assert guild is not None

        raw_ids = self._extract_focus_user_ids(message)
        valid_ids = await self._valid_focus_ids(guild, raw_ids)
        if len(valid_ids) < 2:
            await self._send_focus_notice(
                message,
                title="Sincronização incompleta",
                lines=["Mencione ou envie o ID de pelo menos **2 membros**"],
                ok=False,
                show_current=False,
            )
            return True

        syncer = getattr(self.db, "sync_gincana_focus_users", None)
        if callable(syncer):
            merged_ids = await syncer(guild.id, valid_ids)
        else:
            merged_ids = valid_ids
        merged_ids = await self._valid_focus_ids(guild, merged_ids)

        current_ids = set(int(uid) for uid in self.db.get_gincana_focus_map(guild.id).keys())
        sync_added: list[int] = []
        if current_ids & set(merged_ids):
            sync_added, _ = await self._set_focus_final_ids(guild, current_ids | set(merged_ids))

        mention_text = self._format_focus_mentions(guild, merged_ids, compact_after=4)
        if len(merged_ids) == 2:
            detail = f"{mention_text} agora compartilham os efeitos de foco"
        else:
            detail = f"**{len(merged_ids)}** membros agora compartilham os efeitos de foco"

        lines = [detail]
        if sync_added:
            lines.append("Quem já estava em foco puxou o grupo junto")

        await self._send_focus_notice(
            message,
            title="Sincronização criada",
            lines=lines,
            ok=True,
            show_current=bool(sync_added),
        )
        return True

    async def _handle_focus_all(self, message: discord.Message) -> bool:
        guild = message.guild
        assert guild is not None

        voice_channel = self._call_trigger_channel(message)
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return True

        call_ids: list[int] = []
        for member in getattr(voice_channel, "members", []) or []:
            if self._focus_member_is_valid(member):
                call_ids.append(int(member.id))

        if not call_ids:
            await self._send_focus_notice(
                message,
                title="Ninguém para focar",
                lines=["Não encontrei membros válidos na sua call"],
                ok=False,
                show_current=False,
            )
            return True

        expanded_ids = await self._valid_expanded_focus_ids(guild, call_ids)
        expanded_set = set(expanded_ids)
        call_set = set(call_ids)
        current_ids = set(int(uid) for uid in self.db.get_gincana_focus_map(guild.id).keys())

        removing = bool(expanded_set) and expanded_set.issubset(current_ids)
        if removing:
            _, removed_ids = await self._set_focus_final_ids(guild, current_ids - expanded_set)
            removed_set = set(removed_ids)
            removed_total = len(removed_set)
            removed_synced = max(0, len(removed_set - call_set))
            if removed_total <= 0:
                lines = ["Nada mudou por enquanto"]
            else:
                who = "membro saiu" if removed_total == 1 else "membros saíram"
                lines = [f"**{removed_total}** {who} do modo foco"]
                if removed_synced:
                    lines.append(f"**{removed_synced}** {('sincronizado saiu junto' if removed_synced == 1 else 'sincronizados saíram junto')}")
            await self._send_focus_notice(
                message,
                title="Foco removido",
                lines=lines,
                ok=removed_total > 0,
                show_current=True,
            )
            return True

        added_ids, _ = await self._set_focus_final_ids(guild, current_ids | expanded_set)
        added_set = set(added_ids)
        added_call_count = len(added_set & call_set)
        added_synced_count = max(0, len(added_set - call_set))
        lines: list[str] = []
        if added_call_count:
            who = "membro da call entrou" if added_call_count == 1 else "membros da call entraram"
            lines.append(f"**{added_call_count}** {who} no modo foco")
        if added_synced_count:
            lines.append(f"**{added_synced_count}** {('sincronizado também foi incluído' if added_synced_count == 1 else 'sincronizados também foram incluídos')}")
        if not lines:
            lines = ["Todo mundo válido dessa call já estava em foco"]

        await self._send_focus_notice(
            message,
            title="Foco ativado",
            lines=lines,
            ok=bool(added_ids),
            show_current=True,
        )
        return True

    async def _handle_focus_toggle(self, message: discord.Message) -> bool:
        guild = message.guild
        assert guild is not None

        raw_ids = self._extract_focus_user_ids(message)
        valid_user_ids = await self._valid_focus_ids(guild, raw_ids)

        if not valid_user_ids:
            await self.db.clear_gincana_focus_users(guild.id)
            await self._send_focus_feedback(message, added_ids=[], removed_ids=[], errored_ids=[], reset=True)
            return True

        current_ids = set(int(uid) for uid in self.db.get_gincana_focus_map(guild.id).keys())
        final_ids = set(current_ids)

        for uid in valid_user_ids:
            expanded_ids = set(await self._valid_expanded_focus_ids(guild, [uid]))
            if not expanded_ids:
                continue
            if int(uid) in current_ids:
                final_ids.difference_update(expanded_ids)
            else:
                final_ids.update(expanded_ids)

        added, removed = await self._set_focus_final_ids(guild, final_ids)
        await self._send_focus_feedback(
            message,
            added_ids=added,
            removed_ids=removed,
            errored_ids=[],
            reset=False,
        )
        return True

    async def _handle_focus_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = str(message.content or "")
        if not _FOCUS_WORD_RE.search(content):
            return False
        if not _FOCUS_COMMAND_RE.search(content):
            return False

        if not isinstance(message.author, discord.Member):
            return True

        if not self._is_staff_member(message.author):
            return True

        if not self._is_call_trigger_context(message):
            return True

        if _FOCUS_SYNC_RE.search(content):
            return await self._handle_focus_sync(message)
        if _FOCUS_ALL_RE.fullmatch(content):
            return await self._handle_focus_all(message)
        return await self._handle_focus_toggle(message)
