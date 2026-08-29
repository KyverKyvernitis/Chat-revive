import asyncio
import random
import time
from pathlib import Path

import discord

from config import MUTE_TOGGLE_WORD, OFF_COLOR, TRIGGER_WORD

from ..constants import (
    _ALVO_WORD_RE,
    _ATIRAR_WORD_RE,
    _BUCKSHOT_WORD_RE,
    _DJ_DURATION_SECONDS,
    _DJ_TOGGLE_WORD_RE,
    _PICA_DURATION_SECONDS,
    _POKER_WORD_RE,
    _ROLETA_WORD_RE,
    _ROLE_TOGGLE_WORD_RE,
    ALVO_STAKE,
    BUCKSHOT_STAKE,
    ROLETA_COST,
    ROLETA_JACKPOT_CHIPS,
)


_BUCKSHOT_SURVIVOR_BONUS = 10
_BUCKSHOT_GOLDEN_SURVIVOR_BONUS = 25

class _BuckshotJoinView(discord.ui.View):
    def __init__(self, cog: "GincanaTriggerMixin", guild_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="Entrar na rodada (0)")
        self.join_button.callback = self._toggle_join
        self.add_item(self.join_button)

    async def _toggle_join(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_buckshot(self.guild_id, reason="timeout")
        except Exception:
            pass


class GincanaBuckshotMixin:
        def _get_buckshot_session(self, guild_id: int) -> dict | None:
            session = self._buckshot_sessions.get(guild_id)
            if not session or session.get("ended"):
                return None
            return session
        def _get_buckshot_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
            channel = guild.get_channel(int(session.get("voice_channel_id", 0) or 0))
            return channel if isinstance(channel, discord.VoiceChannel) else None
        def _get_buckshot_focus_participants(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
            participants = []
            seen: set[int] = set()
            for member in self._iter_focused_members(guild, voice_channel):
                if member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _get_buckshot_manual_participants(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, session: dict) -> list[discord.Member]:
            participants = []
            seen: set[int] = set()
            stored_ids = set(session.get("manual_participants", set()) or set())
            for user_id in stored_ids:
                member = guild.get_member(int(user_id))
                if member is None or member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _get_buckshot_participant_ids(self, guild: discord.Guild, session: dict) -> list[int]:
            participant_ids: set[int] = set()
            voice_channel = self._get_buckshot_voice_channel(guild, session)
            focus_ids = set(session.get("focus_participants", set()) or set())
            if voice_channel is not None:
                current_voice_ids = {member.id for member in getattr(voice_channel, "members", []) if not getattr(member, "bot", False)}
                participant_ids.update(current_voice_ids & focus_ids)
            participant_ids.update(int(user_id) for user_id in (session.get("manual_participants", set()) or set()))
            participant_ids.update(int(user_id) for user_id in (session.get("locked_participants", set()) or set()))
            return sorted(participant_ids)
        def _get_buckshot_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
            participants: list[discord.Member] = []
            seen: set[int] = set()
            for user_id in self._get_buckshot_participant_ids(guild, session):
                member = guild.get_member(int(user_id))
                if member is None or member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _make_buckshot_embed(self, guild: discord.Guild, session: dict, *, final_text: str | None = None) -> discord.Embed:
            participants = self._get_buckshot_participants(guild, session)
            payout_total = len(participants) * BUCKSHOT_STAKE
            title = "<:gunforward:1484655577836683434> Roleta russa"
            if final_text:
                description = final_text
                color = discord.Color.red()
            else:
                description = (
                    f"Entrada: {self._chip_amount(BUCKSHOT_STAKE)} por jogador\n"
                    f"Participantes: **{len(participants)}**\n"
                    f"{self._CHIP_GAIN_EMOJI} Pote atual: {self._chip_amount(payout_total)}\n\n"
                    "Entre na rodada e veja quem sai da call quando o disparo vier"
                )
                color = discord.Color.blurple()
            embed = discord.Embed(title=title, description=description, color=color)
            return embed
        async def _refresh_buckshot_message(self, guild_id: int):
            session = self._get_buckshot_session(guild_id)
            if session is None:
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            message = session.get("message")
            view = session.get("view")
            if message is None or view is None:
                return

            participants = self._get_buckshot_participants(guild, session)
            view.join_button.label = f"Entrar na rodada ({len(participants)})"
            view.join_button.style = discord.ButtonStyle.success
            try:
                await message.edit(embed=self._make_buckshot_embed(guild, session), view=view)
            except Exception:
                pass
        async def _handle_buckshot_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
            guild = interaction.guild
            if guild is None:
                try:
                    await interaction.response.send_message("Use esse botão dentro de um servidor", ephemeral=True)
                except Exception:
                    pass
                return

            session = self._get_buckshot_session(guild.id)
            if session is None:
                try:
                    await interaction.response.send_message("Essa rodada já terminou", ephemeral=True)
                except Exception:
                    pass
                return

            voice_channel = self._get_buckshot_voice_channel(guild, session)
            if voice_channel is None:
                try:
                    await interaction.response.send_message("A rodada perdeu o canal de voz de referência", ephemeral=True)
                except Exception:
                    pass
                return

            member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
            if not isinstance(member, discord.Member) or member.bot:
                try:
                    await interaction.response.send_message("Bots não podem participar dessa rodada", ephemeral=True)
                except Exception:
                    pass
                return

            if getattr(member.voice, "channel", None) != voice_channel:
                try:
                    await interaction.response.send_message("Você precisa estar na mesma call da rodada para participar", ephemeral=True)
                except Exception:
                    pass
                return

            locked_participants = set(session.get("locked_participants", set()) or set())
            if member.id in locked_participants:
                try:
                    await interaction.response.send_message("Você já entrou nessa rodada e sua vaga está travada", ephemeral=True)
                except Exception:
                    pass
                return

            needs_negative_confirm = self._needs_negative_confirmation(guild.id, member.id, BUCKSHOT_STAKE)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, member.id, BUCKSHOT_STAKE, title="💥 Confirmar entrada")
                if not confirmed:
                    return

            entry_text = self._entry_consume_text(guild.id, member.id, BUCKSHOT_STAKE)
            paid, _balance, note = await self._try_consume_chips(guild.id, member.id, BUCKSHOT_STAKE, reason="Entrada no buckshot")
            if needs_negative_confirm:
                note = None
            if not paid:
                try:
                    await interaction.response.send_message(note or "Você não tem saldo suficiente para entrar", ephemeral=True)
                except Exception:
                    pass
                return

            manual_participants = set(session.get("manual_participants", set()) or set())
            manual_participants.add(member.id)
            session["manual_participants"] = manual_participants
            locked_participants.add(member.id)
            session["locked_participants"] = locked_participants

            await self._refresh_buckshot_message(guild.id)

            reply_text = note or entry_text
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(reply_text, ephemeral=True)
                else:
                    await interaction.response.send_message(reply_text, ephemeral=True)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

        async def _finish_buckshot(self, guild_id: int, *, reason: str) -> bool:
            session = self._buckshot_sessions.get(guild_id)
            if not session or session.get("ended"):
                return False

            session["ended"] = True
            timeout_task = session.get("timeout_task")
            if timeout_task is not None and timeout_task is not asyncio.current_task() and not timeout_task.done():
                timeout_task.cancel()

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                if bool(session.get('changefate_forced')):
                    self._release_changefate_golden_reservation(
                        guild_id,
                        int(session.get('owner_id') or 0),
                        str(session.get('changefate_token') or ''),
                    )
                self._buckshot_sessions.pop(guild_id, None)
                return False

            message = session.get("message")
            view = session.get("view")
            if isinstance(view, discord.ui.View):
                for child in view.children:
                    child.disabled = True
                    if isinstance(child, discord.ui.Button):
                        child.style = discord.ButtonStyle.danger
                try:
                    view.stop()
                except Exception:
                    pass

            participants = self._get_buckshot_participants(guild, session)
            locked_participants = set(session.get("locked_participants", set()) or set())
            eligible = [member for member in participants if member.id in locked_participants]
            chosen = random.choice(eligible) if eligible else None
            if chosen is not None and chosen.voice and chosen.voice.channel:
                chosen_channel = chosen.voice.channel
                try:
                    await self._play_buckshot_sfx(guild, chosen_channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass
                try:
                    await chosen.move_to(None, reason="economia buckshot")
                except Exception:
                    pass

            winners = [member for member in eligible if chosen is not None and member.id != chosen.id]
            payout_total = max(0, BUCKSHOT_STAKE * len(eligible))
            payout_each = 0
            payout_remainder = 0
            if chosen is None:
                final_text = "O disparo aconteceu... mas ninguém com entrada paga ficou elegível na rodada"
            else:
                if winners:
                    payout_each = payout_total // len(winners)
                    payout_remainder = payout_total % len(winners)
                    for index, winner in enumerate(winners):
                        bonus = payout_each + (1 if index < payout_remainder else 0)
                        if bonus > 0:
                            await self._change_user_bonus_chips(guild.id, winner.id, bonus, reason="Vitória no buckshot")
                            await self.db.add_user_game_stat(guild.id, winner.id, "buckshot_survivals", 1)
                            await self._record_game_played(guild.id, winner.id, weekly_points=8)
                            await self._grant_weekly_points(guild.id, winner.id, max(3, bonus // 3))
                await self.db.add_user_game_stat(guild.id, chosen.id, "buckshot_eliminations", 1)
                await self._record_game_played(guild.id, chosen.id, weekly_points=3)
                chosen_text = chosen.mention if chosen is not None else "Alguém"
                if winners:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu\n{chosen_text} foi eliminado\n"
                        f"Cada sobrevivente recebeu **{payout_each} {self._CHIP_BONUS_EMOJI}** de bônus"
                    )
                else:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu, {chosen_text} foi eliminado\n"
                        f"{self._CHIP_LOSS_EMOJI} O pote de **{payout_total} {self._CHIP_EMOJI}** foi perdido"
                    )

            embed = self._make_buckshot_embed(guild, session, final_text=final_text)
            delivered = False
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                    delivered = True
                except Exception:
                    pass
            if not delivered and message is not None:
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass

            self._buckshot_sessions.pop(guild_id, None)
            return True
        async def _handle_buckshot_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "buckshot"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if self._get_buckshot_session(guild.id) is not None:
                return True

            voice_channel = getattr(getattr(message.author, "voice", None), "channel", None)

            view = _BuckshotJoinView(self, guild.id, timeout=30.0)
            session = {
                "voice_channel_id": getattr(voice_channel, "id", 0),
                "text_channel_id": message.channel.id,
                "manual_participants": {message.author.id},
                "focus_participants": set(),
                "locked_participants": {message.author.id},
                "message": None,
                "view": view,
                "ended": False,
                "timeout_task": None,
            }
            self._buckshot_sessions[guild.id] = session

            view.join_button.label = f"Entrar na rodada ({len(self._get_buckshot_participants(guild, session))})"
            view.join_button.style = discord.ButtonStyle.success
            embed = self._make_buckshot_embed(guild, session)
            try:
                panel_message = await message.channel.send(embed=embed, view=view)
            except Exception:
                self._buckshot_sessions.pop(guild.id, None)
                return True

            session["message"] = panel_message
            session["timeout_task"] = self.bot.loop.create_task(view.wait())
            await self._react_with_emoji(message, "<a:r_gun01:1484661880323838002>", keep=True)
            return True
        async def _handle_atirar_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "atirar"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            session = self._get_buckshot_session(guild.id)
            if session is None:
                return True

            await self._finish_buckshot(guild.id, reason="manual")
            await self._react_with_emoji(message, "💥", keep=True)
            return True


# --- V2 lobby overrides ---
class _BuckshotJoinView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaBuckshotMixin", guild_id: int, session: dict, guild: discord.Guild, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.session = session
        self.guild = guild
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Entrar (0)', emoji=self.cog._buckshot_join_emoji(self.session))
        self.join_button.callback = self._toggle_join
        self.start_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Atirar', emoji=self.cog._buckshot_start_emoji(self.session))
        self.start_button.callback = self._start_round
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        participants = self.cog._get_buckshot_participants(self.guild, self.session)
        stake = self.cog._buckshot_stake(self.session)
        payout_total = len(participants) * stake
        countdown = int(self.session.get('start_countdown') or 0)
        self.join_button.label = f"Entrar ({len(participants)})"
        if countdown > 0:
            self.start_button.label = f"Atirar ({countdown})"
            self.start_button.disabled = True
        else:
            self.start_button.label = 'Atirar'
            self.start_button.disabled = False
        lines1 = [f"# {self.cog._buckshot_title(self.session)}", f"**Entrada:** {self.cog._chip_amount(stake)}", f"**Pote atual:** {self.cog._chip_amount(payout_total)}", "**Inscrições:** 30s"]
        plist = [f"### Participantes ({len(participants)})"]
        if participants:
            plist.extend(f"• {m.mention}" for m in participants)
        else:
            plist.append('• Ninguém entrou ainda')
        foot = ['Use o botão abaixo para entrar', 'O criador da rodada ou a staff pode começar quando houver pelo menos 2 participantes']
        if countdown > 0:
            foot.append('A contagem começou e ainda dá tempo de entrar')
        row = discord.ui.ActionRow(self.join_button, self.start_button)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines1)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(plist)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(foot)),
            row,
            accent_color=self.cog._buckshot_color(self.session),
        ))

    async def _toggle_join(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_button(interaction, self)

    async def _start_round(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_start_button(interaction, self)

    async def on_timeout(self):
        try:
            session = self.cog._get_buckshot_session(self.guild_id)
            if session is None or session.get('starting'):
                return
            await self.cog._finish_buckshot(self.guild_id, reason='timeout')
        except Exception:
            pass

class _BuckshotLobbyClosedView(discord.ui.LayoutView):
    def __init__(self, title: str, lines: list[str], *, color: discord.Color | None = None):
        super().__init__(timeout=None)
        body = [f"# {title}", *[line for line in lines if line]]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(body)), accent_color=color or discord.Color.blurple()))

class GincanaBuckshotMixin(GincanaBuckshotMixin):
    def _get_buckshot_session(self, guild_id: int) -> dict | None:
        session = self._buckshot_sessions.get(guild_id)
        if not session:
            return None
        if session.get('ended'):
            self._buckshot_sessions.pop(guild_id, None)
            return None
        if self._runtime_state_is_stale(session, max_idle=120.0, max_age=300.0):
            if not session.get('_stale_cleanup_started'):
                session['_stale_cleanup_started'] = True
                session['ended'] = True
                self.bot.loop.create_task(self._cleanup_stale_buckshot_session(guild_id, session))
            return None
        self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
        return session

    async def _cleanup_stale_buckshot_session(self, guild_id: int, session: dict):
        async with self._runtime_lock(session):
            if self._buckshot_sessions.get(guild_id) is not session:
                return
            await self._safe_cancel_task(session.get('countdown_task'))
            guild = self.bot.get_guild(guild_id)
            locked_participants = {int(user_id) for user_id in (session.get('locked_participants', set()) or set())}
            if guild is not None:
                stake = self._buckshot_stake(session)
                for user_id in sorted(locked_participants):
                    try:
                        await self._buckshot_refund_entry(guild.id, session, int(user_id), stake)
                    except Exception:
                        pass
            lobby_message = session.get('lobby_message') or session.get('message')
            if lobby_message is not None:
                try:
                    await lobby_message.edit(view=_BuckshotLobbyClosedView(f'{self._EFFECT_EMOJI} Rodada dourada cancelada' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002> Rodada cancelada', [
                        'A rodada foi encerrada automaticamente porque ficou travada por tempo demais',
                        'Todas as entradas foram devolvidas',
                    ], color=self._buckshot_color(session)))
                except Exception:
                    pass
            if self._buckshot_sessions.get(guild_id) is session:
                self._buckshot_sessions.pop(guild_id, None)

    def _buckshot_render_key(self, session: dict, guild: discord.Guild) -> tuple:
        return (
            bool(session.get('ended')),
            bool(session.get('starting')),
            int(session.get('start_countdown', 0) or 0),
            tuple(sorted(int(x) for x in (session.get('locked_participants', set()) or set()))),
            tuple(int(member.id) for member in self._get_buckshot_participants(guild, session)),
            str(session.get('variant') or 'normal'),
        )

    def _buckshot_is_golden(self, session: dict | None) -> bool:
        return str((session or {}).get('variant') or 'normal').lower() == 'golden'

    def _buckshot_stake(self, session: dict | None) -> int:
        return 30 if self._buckshot_is_golden(session) else BUCKSHOT_STAKE

    def _buckshot_title(self, session: dict | None) -> str:
        return '<a:uzi:1487936659692458054> Roleta russa dourada' if self._buckshot_is_golden(session) else '<:gunforward:1484655577836683434> Roleta russa'

    def _buckshot_color(self, session: dict | None) -> discord.Color:
        return discord.Color.gold() if self._buckshot_is_golden(session) else discord.Color.blurple()

    def _buckshot_join_emoji(self, session: dict | None):
        return discord.PartialEmoji.from_str('<:gold:1487936417790431433>') if self._buckshot_is_golden(session) else discord.PartialEmoji.from_str('<:propergun:1485855162198396959>')

    def _buckshot_start_emoji(self, session: dict | None):
        return discord.PartialEmoji.from_str('<:star:1487936913431072780>') if self._buckshot_is_golden(session) else '💥'

    def _buckshot_reaction_emoji(self, session: dict | None) -> str:
        return '<a:uzi:1487936659692458054>' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002>'

    @staticmethod
    def _buckshot_reward_range(minimum: int, maximum: int, emoji: str) -> str | None:
        minimum = max(0, int(minimum or 0))
        maximum = max(minimum, int(maximum or 0))
        if maximum <= 0:
            return None
        if minimum == maximum:
            return f"**{minimum} {emoji}**"
        return f"**{minimum}–{maximum} {emoji}**"

    def _format_buckshot_reward_summary(
        self,
        *,
        normal_each: int,
        normal_remainder: int,
        eliminated_bonus_each: int,
        eliminated_bonus_remainder: int,
        round_bonus_each: int,
        round_bonus_remainder: int,
        fixed_bonus_each: int,
    ) -> str:
        normal_min = max(0, int(normal_each or 0))
        normal_max = normal_min + (1 if int(normal_remainder or 0) > 0 else 0)
        bonus_min = (
            max(0, int(eliminated_bonus_each or 0))
            + max(0, int(round_bonus_each or 0))
            + max(0, int(fixed_bonus_each or 0))
        )
        bonus_max = bonus_min
        if int(eliminated_bonus_remainder or 0) > 0:
            bonus_max += 1
        if int(round_bonus_remainder or 0) > 0:
            bonus_max += 1

        reward_parts = ['entrada devolvida']
        normal_text = self._buckshot_reward_range(normal_min, normal_max, self._CHIP_GAIN_EMOJI)
        bonus_text = self._buckshot_reward_range(bonus_min, bonus_max, self._CHIP_BONUS_EMOJI)
        if normal_text:
            reward_parts.append(normal_text)
        if bonus_text:
            reward_parts.append(bonus_text)
        return f"🏆 **Prêmio:** {' + '.join(reward_parts)} para cada sobrevivente"

    def _buckshot_entry_spend(self, session: dict, user_id: int, stake: int) -> dict[str, object]:
        spend_map = session.get('entry_spend') or {}
        raw = spend_map.get(int(user_id)) or {}
        bonus_spent = max(0, int(raw.get('bonus', 0) or 0))
        temporary_spent = min(bonus_spent, max(0, int(raw.get('temporary_bonus', 0) or 0)))
        normal_spent = max(0, int(raw.get('chips', 0) or 0))
        total = bonus_spent + normal_spent
        if total <= 0 and stake > 0:
            result: dict[str, object] = {'chips': int(stake), 'bonus': 0}
        else:
            if total > stake > 0:
                overflow = total - int(stake)
                reduce_bonus = min(bonus_spent, overflow)
                bonus_spent -= reduce_bonus
                overflow -= reduce_bonus
                if overflow > 0:
                    normal_spent = max(0, normal_spent - overflow)
            result = {'chips': normal_spent, 'bonus': bonus_spent}
        if temporary_spent > 0:
            result['temporary_bonus'] = min(temporary_spent, int(result.get('bonus', 0) or 0))
            result['_temporary_bonus_expires_at'] = float(raw.get('_temporary_bonus_expires_at', 0.0) or 0.0)
        period = str(raw.get('_race_period') or '').strip().lower()
        period_key = str(raw.get('_race_period_key') or '').strip()
        if period in {'day', 'night'} and period_key:
            result['_race_period'] = period
            result['_race_period_key'] = period_key
        return result

    async def _buckshot_refund_entry(self, guild_id: int, session: dict, user_id: int, stake: int):
        spend = self._buckshot_entry_spend(session, user_id, stake)
        await self._refund_entry_spend(
            guild_id,
            user_id,
            spend,
            reason='Devolução do buckshot',
        )

    async def _downgrade_buckshot_changefate(
        self,
        guild_id: int,
        session: dict,
        user_ids: list[int],
    ) -> None:
        refund_each = max(0, 30 - int(BUCKSHOT_STAKE))
        spend_map = session.get('entry_spend') or {}
        if refund_each <= 0:
            session['variant'] = 'normal'
            return
        for user_id in user_ids:
            raw = dict(spend_map.get(int(user_id)) or {})
            remaining = refund_each
            refund_chips = min(max(0, int(raw.get('chips', 0) or 0)), remaining)
            raw['chips'] = max(0, int(raw.get('chips', 0) or 0) - refund_chips)
            remaining -= refund_chips
            temporary = min(
                max(0, int(raw.get('bonus', 0) or 0)),
                max(0, int(raw.get('temporary_bonus', 0) or 0)),
            )
            persistent_bonus = max(0, int(raw.get('bonus', 0) or 0) - temporary)
            refund_bonus = min(persistent_bonus, remaining)
            raw['bonus'] = max(0, int(raw.get('bonus', 0) or 0) - refund_bonus)
            remaining -= refund_bonus
            refund_temporary = min(temporary, remaining)
            raw['bonus'] = max(0, int(raw.get('bonus', 0) or 0) - refund_temporary)
            raw['temporary_bonus'] = max(0, temporary - refund_temporary)
            await self._refund_entry_spend(
                guild_id,
                int(user_id),
                {
                    'chips': refund_chips,
                    'bonus': refund_bonus + refund_temporary,
                    'temporary_bonus': refund_temporary,
                    '_temporary_bonus_expires_at': float(raw.get('_temporary_bonus_expires_at', 0.0) or 0.0),
                },
                reason='Ajuste do Buckshot dourado',
            )
            spend_map[int(user_id)] = raw
        session['entry_spend'] = spend_map
        session['variant'] = 'normal'

    async def _refresh_buckshot_message(self, guild_id: int):
        session = self._get_buckshot_session(guild_id)
        if session is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = session.get('lobby_message') or session.get('message')
        view = session.get('view')
        if message is None or view is None:
            return
        if hasattr(view, '_build_layout'):
            view._build_layout()
        render_key = self._buckshot_render_key(session, guild)
        await self._safe_view_edit(message, view, state=session, render_key=render_key)

    async def _handle_buckshot_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
        guild = interaction.guild
        if guild is None:
            try: await interaction.response.send_message('Use esse botão dentro de um servidor', ephemeral=True)
            except Exception: pass
            return
        session = self._get_buckshot_session(guild.id)
        if session is None or session.get('view') is not view:
            try: await interaction.response.send_message('Essa rodada já terminou', ephemeral=True)
            except Exception: pass
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if not isinstance(member, discord.Member) or member.bot:
            try: await interaction.response.send_message('Bots não podem participar dessa rodada', ephemeral=True)
            except Exception: pass
            return
        async with self._runtime_lock(session):
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
            if session.get('ended') or session.get('view') is not view:
                try: await interaction.response.send_message('Essa rodada já terminou', ephemeral=True)
                except Exception: pass
                return
            locked = set(session.get('locked_participants', set()) or set())
            if member.id in locked:
                try: await interaction.response.send_message('Você já entrou nessa rodada e sua vaga está travada', ephemeral=True)
                except Exception: pass
                return
            stake = self._buckshot_stake(session)
            needs_negative_confirm = self._needs_negative_confirmation(guild.id, member.id, stake)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, member.id, stake, title="💥 Confirmar entrada")
                if not confirmed:
                    return
            entry_text = self._entry_consume_text(guild.id, member.id, stake)
            entry_spend = self._entry_spend_parts(guild.id, member.id, stake)
            paid, _balance, note = await self._try_consume_chips(guild.id, member.id, stake, reason="Entrada no buckshot")
            if needs_negative_confirm:
                note = None
            if not paid:
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(note or 'Você não tem saldo suficiente para entrar', ephemeral=True)
                    else:
                        await interaction.response.send_message(note or 'Você não tem saldo suficiente para entrar', ephemeral=True)
                except Exception:
                    pass
                return
            manual = set(session.get('manual_participants', set()) or set())
            manual.add(member.id)
            session['manual_participants'] = manual
            session.setdefault('entry_spend', {})[member.id] = entry_spend
            locked.add(member.id)
            session['locked_participants'] = locked
            session.setdefault('race_interactions', {})[member.id] = interaction
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        await self._refresh_buckshot_message(guild.id)
        await self._send_race_lobby_feedback(interaction, guild.id, member.id, note or entry_text)

    async def _handle_buckshot_start_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try: await interaction.response.send_message('Servidor inválido', ephemeral=True)
            except Exception: pass
            return
        session = self._get_buckshot_session(guild.id)
        if session is None or session.get('view') is not view or session.get('ended'):
            try: await interaction.response.send_message('Essa rodada já terminou', ephemeral=True)
            except Exception: pass
            return
        async with self._runtime_lock(session):
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
            if session.get('starting'):
                try: await interaction.response.send_message('A contagem já começou', ephemeral=True)
                except Exception: pass
                return
            is_owner = int(session.get('owner_id') or 0) == user.id
            if not is_owner and not self._is_staff_member(user):
                try: await interaction.response.send_message('Só o criador da rodada ou a staff pode começar', ephemeral=True)
                except Exception: pass
                return
            participants = self._get_buckshot_participants(guild, session)
            if len(participants) < 2:
                try: await interaction.response.send_message('A rodada precisa de pelo menos 2 participantes', ephemeral=True)
                except Exception: pass
                return
            session['starting'] = True
            session['start_countdown'] = 3
            await self._safe_cancel_task(session.get('countdown_task'))
            session['countdown_task'] = self.bot.loop.create_task(self._run_buckshot_start_countdown(guild.id, view))
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        try: await interaction.response.send_message('Contagem iniciada', ephemeral=True)
        except Exception: pass
        await self._refresh_buckshot_message(guild.id)

    async def _run_buckshot_start_countdown(self, guild_id: int, view: _BuckshotJoinView):
        for remaining in range(3,0,-1):
            session = self._get_buckshot_session(guild_id)
            if session is None or session.get('ended') or session.get('view') is not view:
                return
            session['start_countdown'] = remaining
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
            await self._refresh_buckshot_message(guild_id)
            await asyncio.sleep(1)
        session = self._get_buckshot_session(guild_id)
        if session is None or session.get('ended') or session.get('view') is not view:
            return
        session['start_countdown'] = 0
        await self._finish_buckshot(guild_id, reason='manual')

    async def _finish_buckshot(self, guild_id: int, *, reason: str) -> bool:
        session = self._buckshot_sessions.get(guild_id)
        if not session or session.get('ended'):
            return False
        async with self._runtime_lock(session):
            if session.get('ended'):
                return False
            session['ended'] = True
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
            self._buckshot_last_used[guild_id] = time.time()
            await self._safe_cancel_task(session.get('countdown_task'))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                if bool(session.get('changefate_forced')):
                    self._release_changefate_golden_reservation(
                        guild_id,
                        int(session.get('owner_id') or 0),
                        str(session.get('changefate_token') or ''),
                    )
                self._buckshot_sessions.pop(guild_id, None)
                return False
            lobby_message = session.get('lobby_message') or session.get('message')
            view = session.get('view')
            if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
                try: view.stop()
                except Exception: pass
            participants = self._get_buckshot_participants(guild, session)
            locked_participants = set(session.get('locked_participants', set()) or set())
            eligible = [member for member in participants if member.id in locked_participants]
            if len(eligible) < 2:
                for uid in locked_participants:
                    await self._buckshot_refund_entry(guild.id, session, int(uid), self._buckshot_stake(session))
                if bool(session.get('changefate_forced')):
                    self._release_changefate_golden_reservation(
                        guild.id,
                        int(session.get('owner_id') or 0),
                        str(session.get('changefate_token') or ''),
                    )
                if lobby_message is not None:
                    try:
                        await lobby_message.edit(view=_BuckshotLobbyClosedView(f'{self._EFFECT_EMOJI} Rodada dourada cancelada' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002> Rodada cancelada', [
                            'A rodada precisa de pelo menos **2 participantes**',
                            'Todas as entradas foram devolvidas',
                        ], color=self._buckshot_color(session)))
                    except Exception:
                        pass
                self._buckshot_sessions.pop(guild_id, None)
                return True

            if bool(session.get('changefate_forced')):
                consumed_changefate = await self._consume_changefate_golden(
                    guild.id,
                    int(session.get('owner_id') or 0),
                    reservation_token=str(session.get('changefate_token') or ''),
                )
                if not consumed_changefate:
                    session['changefate_forced'] = False
                    await self._downgrade_buckshot_changefate(
                        guild.id,
                        session,
                        [member.id for member in eligible],
                    )

            chosen = random.choice(eligible) if eligible else None
            if chosen is not None and chosen.voice and chosen.voice.channel:
                try:
                    await self._play_buckshot_sfx(guild, chosen.voice.channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass
                try:
                    await chosen.move_to(None, reason='gincana buckshot')
                except Exception:
                    pass

            winners = [member for member in eligible if chosen is not None and member.id != chosen.id]
            player_count = len(eligible)
            stake = self._buckshot_stake(session)
            if self._buckshot_is_golden(session):
                for participant in eligible:
                    await self.db.add_user_game_stat(guild.id, participant.id, 'buckshot_golden_games', 1)
            is_golden = self._buckshot_is_golden(session)
            eliminated_entry_total = stake if chosen is not None else 0
            bonus_total = 0
            if player_count >= 5:
                bonus_total = 10
            elif player_count >= 3:
                bonus_total = 5
            fixed_bonus_each = _BUCKSHOT_GOLDEN_SURVIVOR_BONUS if is_golden else _BUCKSHOT_SURVIVOR_BONUS
            lines: list[str] = []
            public_race_notices: list[str] = []
            owner_id = int(session.get('owner_id') or 0)
            race_payouts: dict[int, int] = {}
            if chosen is None:
                lines.append(('<a:uzi:1487936659692458054>' if is_golden else '<:gunforward:1484655577836683434>') + ' O disparo aconteceu\nNinguém foi eliminado')
            else:
                base_each = 0
                base_remainder = 0
                bonus_each = 0
                bonus_remainder = 0
                if winners:
                    chosen_spend = self._buckshot_entry_spend(session, chosen.id, stake)
                    eliminated_normal_total = max(0, int(chosen_spend.get('chips', 0) or 0))
                    eliminated_bonus_total = max(0, int(chosen_spend.get('bonus', 0) or 0))
                    base_each = eliminated_normal_total // len(winners) if eliminated_normal_total > 0 else 0
                    base_remainder = eliminated_normal_total % len(winners) if eliminated_normal_total > 0 else 0
                    eliminated_bonus_each = eliminated_bonus_total // len(winners) if eliminated_bonus_total > 0 else 0
                    eliminated_bonus_remainder = eliminated_bonus_total % len(winners) if eliminated_bonus_total > 0 else 0
                    bonus_each = bonus_total // len(winners) if bonus_total > 0 else 0
                    bonus_remainder = bonus_total % len(winners) if bonus_total > 0 else 0
                    for index, winner in enumerate(winners):
                        own_spend = self._buckshot_entry_spend(session, winner.id, stake)
                        returned_normal = max(0, int(own_spend.get('chips', 0) or 0))
                        returned_bonus = max(0, int(own_spend.get('bonus', 0) or 0))
                        normal_gain = returned_normal + base_each + (1 if index < base_remainder else 0)
                        bonus_gain = returned_bonus + eliminated_bonus_each + (1 if index < eliminated_bonus_remainder else 0) + bonus_each + (1 if index < bonus_remainder else 0) + fixed_bonus_each
                        race_payouts[winner.id] = normal_gain + bonus_gain
                        if normal_gain > 0:
                            await self._change_user_chips(guild.id, winner.id, normal_gain, reason="Vitória no buckshot")
                        if bonus_gain > 0:
                            await self._change_user_bonus_chips(guild.id, winner.id, bonus_gain, reason='Vitória no buckshot')
                        if normal_gain > 0 or bonus_gain > 0:
                            await self.db.add_user_game_stat(guild.id, winner.id, 'buckshot_survivals', 1)
                            if is_golden:
                                await self.db.add_user_game_stat(guild.id, winner.id, 'buckshot_golden_survivals', 1)
                            await self._record_game_played(guild.id, winner.id, weekly_points=8)
                            await self._grant_weekly_points(guild.id, winner.id, max(3, (normal_gain + bonus_gain) // 3))
                await self.db.add_user_game_stat(guild.id, chosen.id, 'buckshot_eliminations', 1)
                if is_golden:
                    await self.db.add_user_game_stat(guild.id, chosen.id, 'buckshot_golden_eliminations', 1)
                await self._record_game_played(guild.id, chosen.id, weekly_points=3)
                refund, refund_mode = await self._maybe_apply_coringa_loss_refund(
                    guild.id,
                    chosen.id,
                    stake,
                    chance=0.35,
                )
                lines.append(f"{'<a:uzi:1487936659692458054>💥' if is_golden else '<:gunforward:1484655577836683434>💥'} **Eliminado:** {chosen.mention}")
                if refund > 0:
                    effect_note = self._race_effect_message(
                        guild.id,
                        chosen.id,
                        'joker' if refund_mode == 'joker' else 'as',
                        (
                            self._skill_chip_value(refund, kind="bonus", movement="gain")
                            if refund_mode == 'joker'
                            else f"recuperou {self._chip_text(refund, kind='gain')} da entrada"
                        ),
                    )
                    await self._route_lobby_race_notices(
                        (session.get('race_interactions') or {}).get(chosen.id),
                        guild.id,
                        chosen.id,
                        owner_id,
                        [effect_note],
                        public_race_notices,
                    )
                if winners:
                    lines.append(
                        self._format_buckshot_reward_summary(
                            normal_each=base_each,
                            normal_remainder=base_remainder,
                            eliminated_bonus_each=eliminated_bonus_each,
                            eliminated_bonus_remainder=eliminated_bonus_remainder,
                            round_bonus_each=bonus_each,
                            round_bonus_remainder=bonus_remainder,
                            fixed_bonus_each=fixed_bonus_each,
                        )
                    )
                else:
                    lines.append(f"Ninguém sobreviveu para receber a entrada de **{eliminated_entry_total} {self._CHIP_LOSS_EMOJI}**")

            for member in eligible:
                round_won = None if chosen is None else member.id != chosen.id
                notes = await self._apply_new_race_result(
                    guild.id,
                    member.id,
                    won=round_won,
                    entry_spend=self._buckshot_entry_spend(session, member.id, stake),
                    payout=int(race_payouts.get(member.id, 0) or 0),
                    valid=True,
                )
                await self._route_lobby_race_notices(
                    (session.get('race_interactions') or {}).get(member.id),
                    guild.id,
                    member.id,
                    owner_id,
                    notes,
                    public_race_notices,
                )

            if public_race_notices:
                lines.extend(public_race_notices)

            first_game_user_ids = await self._unlock_first_game_for_users(
                guild.id,
                [member.id for member in eligible],
            )
            result_delivered = False
            if lobby_message is not None:
                final_view = _BuckshotLobbyClosedView(
                    f'{self._EFFECT_EMOJI} Resultado do Buckshot dourado' if is_golden else '<:gunforward:1484655577836683434> Resultado do Buckshot',
                    lines,
                    color=self._buckshot_color(session),
                )
                edit_state = await self._safe_view_edit(lobby_message, final_view)
                result_delivered = edit_state in {'ok', 'skipped'}
            if public_race_notices and not result_delivered:
                self._queue_private_race_notices(guild.id, owner_id, public_race_notices)
            achievement_channel = (
                getattr(lobby_message, "channel", None)
                or guild.get_channel(int(session.get("text_channel_id", 0) or 0))
            )
            await self._send_first_game_notices(achievement_channel, guild.id, first_game_user_ids)
            current = self._buckshot_sessions.get(guild_id)
            if current is session:
                self._buckshot_sessions.pop(guild_id, None)
            return True

    async def _handle_buckshot_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        content = (message.content or '')
        if not self._matches_exact_trigger(content, 'buckshot'):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        last_used = float(self._buckshot_last_used.get(guild.id, 0.0))
        cooldown_remaining = max(0.0, (last_used + 6.0) - time.time())
        if cooldown_remaining > 0:
            try:
                await message.channel.send(embed=self._make_embed('💥 Aguarde um pouco', f'Espere **{int(cooldown_remaining) + 1}s** para abrir outra rodada de buckshot', ok=False))
            except Exception:
                pass
            return True
        if self._get_buckshot_session(guild.id) is not None:
            return True
        changefate_token = f"buckshot:{guild.id}:{message.author.id}:{time.time_ns()}"
        changefate_forced = await self._reserve_changefate_golden(
            guild.id,
            message.author.id,
            changefate_token,
        )
        variant = (
            'golden'
            if changefate_forced or random.random() < self._special_variant_chance_for_user(guild.id, message.author.id)
            else 'normal'
        )
        stake = 30 if variant == 'golden' else BUCKSHOT_STAKE
        needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, stake)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, stake, title="💥 Confirmar entrada")
            if not confirmed:
                if changefate_forced:
                    self._release_changefate_golden_reservation(guild.id, message.author.id, changefate_token)
                return True
        entry_spend = self._entry_spend_parts(guild.id, message.author.id, stake)
        paid, _balance, note = await self._try_consume_chips(guild.id, message.author.id, stake, reason="Entrada no buckshot")
        if needs_negative_confirm:
            note = None
        if not paid:
            if changefate_forced:
                self._release_changefate_golden_reservation(guild.id, message.author.id, changefate_token)
            try: await message.channel.send(embed=self._make_embed('💥 Saldo insuficiente', note or 'Você não tem saldo suficiente para entrar', ok=False))
            except Exception: pass
            return True
        session = {
            'voice_channel_id': 0,
            'text_channel_id': message.channel.id,
            'owner_id': message.author.id,
            'manual_participants': {message.author.id},
            'focus_participants': set(),
            'locked_participants': {message.author.id},
            'entry_spend': {message.author.id: entry_spend},
            'lobby_message': None,
            'message': None,
            'view': None,
            'ended': False,
            'starting': False,
            'start_countdown': 0,
            'countdown_task': None,
            'variant': variant,
            'changefate_forced': bool(changefate_forced),
            'changefate_token': changefate_token if changefate_forced else '',
            '_last_render_key': None,
        }
        self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        self._buckshot_sessions[guild.id] = session
        view = _BuckshotJoinView(self, guild.id, session, guild, timeout=30.0)
        session['view'] = view
        try:
            panel_message = await message.channel.send(view=view)
        except Exception:
            self._buckshot_sessions.pop(guild.id, None)
            await self._buckshot_refund_entry(guild.id, session, message.author.id, stake)
            if changefate_forced:
                self._release_changefate_golden_reservation(guild.id, message.author.id, changefate_token)
            return True
        session['lobby_message'] = panel_message
        await self._react_with_emoji(message, self._buckshot_reaction_emoji(session), keep=True)
        return True

    async def _handle_atirar_trigger(self, message: discord.Message) -> bool:
        return False
