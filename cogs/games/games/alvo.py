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

class _TargetJoinView(discord.ui.View):
    def __init__(self, cog: "GincanaTriggerMixin", guild_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="🎯 Entrar (0)")
        self.join_button.callback = self._join_round
        self.add_item(self.join_button)

    async def _join_round(self, interaction: discord.Interaction):
        await self.cog._handle_target_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_target_round(self.guild_id, reason="timeout")
        except Exception:
            pass


class GincanaAlvoMixin:
        def _get_target_session(self, guild_id: int) -> dict | None:
            session = self._target_sessions.get(guild_id)
            if session and session.get("ended"):
                self._target_sessions.pop(guild_id, None)
                return None
            return session
        def _get_target_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
            channel = guild.get_channel(int(session.get("voice_channel_id") or 0))
            return channel if isinstance(channel, discord.VoiceChannel) else None
        def _get_target_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
            participants: list[discord.Member] = []
            for user_id in sorted(session.get("locked_participants", set())):
                member = guild.get_member(int(user_id))
                if member is None or getattr(member, "bot", False):
                    continue
                participants.append(member)
            return participants
        def _describe_target_zone(self, score: int) -> str:
            return {3: "bullseye", 2: "anel interno", 1: "anel externo", 0: "fora"}.get(int(score), "fora")
        def _roll_target_modifier(self) -> dict:
            roll = random.random()
            if roll < 0.08:
                return {
                    "key": "small",
                    "name": "Alvo pequeno",
                    "description": "Centro mais raro, mas cada bullseye rende bônus extra",
                    "bullseye_bonus": 4,
                }
            if roll < 0.16:
                return {
                    "key": "unstable",
                    "name": "Alvo instável",
                    "description": "O alvo balança mais e aumenta a chance de erro",
                }
            if roll < 0.24:
                return {
                    "key": "generous",
                    "name": "Alvo generoso",
                    "description": "Fica mais fácil acertar os anéis internos",
                }
            if roll < 0.32:
                return {
                    "key": "windy",
                    "name": "Vento forte",
                    "description": "Rajadas atrapalham a precisão, mas o prêmio sobe",
                    "pot_bonus": 6,
                }
            if roll < 0.40:
                return {
                    "key": "golden",
                    "name": "Alvo dourado",
                    "description": "Bullseyes valem mais e a rodada fica brilhando",
                    "pot_bonus": 8,
                    "bullseye_bonus": 6,
                }
            return {
                "key": "normal",
                "name": "Alvo padrão",
                "description": "Rodada normal",
            }
        def _roll_target_score(self, modifier_key: str = "normal") -> int:
            roll = random.random()
            if modifier_key == "small":
                if roll < 0.04:
                    return 3
                if roll < 0.20:
                    return 2
                if roll < 0.52:
                    return 1
                return 0
            if modifier_key == "unstable":
                if roll < 0.05:
                    return 3
                if roll < 0.19:
                    return 2
                if roll < 0.44:
                    return 1
                return 0
            if modifier_key == "generous":
                if roll < 0.09:
                    return 3
                if roll < 0.31:
                    return 2
                if roll < 0.67:
                    return 1
                return 0
            if modifier_key == "windy":
                if roll < 0.05:
                    return 3
                if roll < 0.18:
                    return 2
                if roll < 0.46:
                    return 1
                return 0
            if modifier_key == "golden":
                if roll < 0.10:
                    return 3
                if roll < 0.27:
                    return 2
                if roll < 0.58:
                    return 1
                return 0
            if roll < 0.07:
                return 3
            if roll < 0.25:
                return 2
            if roll < 0.55:
                return 1
            return 0
        def _allocate_target_rewards(self, participants: list[discord.Member], scores: dict[int, int], pot_total: int) -> tuple[dict[int, int], list[tuple[str, list[discord.Member], int]]]:
            rewards: dict[int, int] = {}
            placement_groups: list[tuple[str, list[discord.Member], int]] = []
            if not participants or pot_total <= 0:
                return rewards, placement_groups

            if len(participants) == 2:
                best_score = max(scores.get(member.id, 0) for member in participants)
                top_members = [member for member in participants if scores.get(member.id, 0) == best_score]
                winner = random.choice(top_members)
                rewards[winner.id] = pot_total
                placement_groups.append(("🥇", [winner], pot_total))
                return rewards, placement_groups

            ordered_scores = sorted({scores.get(member.id, 0) for member in participants}, reverse=True)
            first_members = [member for member in participants if scores.get(member.id, 0) == ordered_scores[0]]
            remaining_pool = pot_total

            if len(ordered_scores) > 1:
                first_pool = int(round(pot_total * 0.6))
                second_pool = pot_total - first_pool
                second_members = [member for member in participants if scores.get(member.id, 0) == ordered_scores[1]]
            else:
                first_pool = pot_total
                second_pool = 0
                second_members = []

            def split_pool(members: list[discord.Member], total: int):
                if not members or total <= 0:
                    return
                each = total // len(members)
                remainder = total % len(members)
                for index, member in enumerate(members):
                    rewards[member.id] = rewards.get(member.id, 0) + each + (1 if index < remainder else 0)

            split_pool(first_members, first_pool)
            placement_groups.append(("🥇", first_members, first_pool))
            remaining_pool -= first_pool

            if second_members and second_pool > 0:
                split_pool(second_members, second_pool)
                placement_groups.append(("🥈", second_members, second_pool))
                remaining_pool -= second_pool

            if remaining_pool > 0 and first_members:
                split_pool(first_members, remaining_pool)
                placement_groups[0] = (placement_groups[0][0], placement_groups[0][1], placement_groups[0][2] + remaining_pool)

            return rewards, placement_groups
        def _target_zone_style(self, score: int) -> tuple[str, str]:
            score = int(score)
            if score >= 3:
                return "🎯", "CENTRO!"
            if score == 2:
                return "🟠", "anel interno"
            if score == 1:
                return "🟡", "anel externo"
            return "💨", "errou"
        def _format_target_participants(self, participants: list[discord.Member]) -> str:
            if not participants:
                return "Ninguém entrou ainda"
            mentions = [member.mention for member in participants[:8]]
            text = ", ".join(mentions)
            if len(participants) > 8:
                text += f" e mais **{len(participants) - 8}**"
            return text
        def _target_opening_text(self, participants: list[discord.Member]) -> str:
            if len(participants) >= 3:
                return "Os **2 melhores tiros** dividem a premiação"
            if len(participants) == 2:
                return "Com **2 participantes**, apenas o melhor tiro recebe o prêmio"
            return "Entre pelo botão e use **🏁 Iniciar** para começar antes do tempo"
        def _target_bonus_for_participants(self, count: int) -> int:
            if count >= 7:
                return 10
            if count >= 5:
                return 5
            return 0
        def _build_target_special_lines(self, participants: list[discord.Member], scores: dict[int, int], placements: list[tuple[str, list[discord.Member], int]]) -> list[str]:
            special: list[str] = []
            bullseyes = [member for member in participants if scores.get(member.id, 0) == 3]
            misses = [member for member in participants if scores.get(member.id, 0) <= 0]
            if len(misses) == len(participants):
                special.append("💨 Ninguém acertou o alvo\nA rodada virou um desastre completo")
            if len(bullseyes) >= 2:
                special.append(f"🎯 Chuva de bullseyes: {', '.join(member.mention for member in bullseyes)}!")
            elif len(bullseyes) == 1 and len(participants) >= 4:
                special.append(f"🏅 {bullseyes[0].mention} dominou a rodada com um bullseye raro")
            if placements:
                top_badge, top_members, _ = placements[0]
                if len(top_members) > 1:
                    special.append(f"🤝 O topo terminou empatado entre {', '.join(member.mention for member in top_members)}")
                elif top_members and scores.get(top_members[0].id, 0) >= 2 and all(scores.get(m.id, 0) < scores.get(top_members[0].id, 0) for m in participants if m.id != top_members[0].id):
                    special.append(f"🔥 {top_members[0].mention} levou a melhor com folga")
            return special
        def _make_target_embed(self, guild: discord.Guild, session: dict, *, final_text: str | None = None, aiming: bool = False) -> discord.Embed:
            participants = self._get_target_participants(guild, session)
            locked_ids = set(session.get("locked_participants", set()))
            pot_total = len(locked_ids) * ALVO_STAKE
            owner_id = int(session.get("owner_id") or 0)
            owner = guild.get_member(owner_id) if owner_id else None
            modifier = session.get("modifier") or {"key": "normal", "name": "Alvo padrão", "description": "Rodada normal"}
            bonus = int(session.get("bonus_chips") or 0)

            if final_text:
                embed = discord.Embed(
                    title="🎯 Resultado do alvo",
                    description=final_text,
                    color=discord.Color.blurple(),
                )
            elif aiming:
                embed = discord.Embed(
                    title="🎯 Mirando...",
                    description=(
                        f"Participantes: **{len(participants)}**\n"
                        f"{self._CHIP_GAIN_EMOJI} Pote: {self._chip_amount(pot_total)}\n\n"
                        "Os tiros estão sendo alinhados..."
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Na mira", value=self._format_target_participants(participants), inline=False)
            else:
                total_bonus = bonus + int(modifier.get("pot_bonus", 0) or 0)
                embed = discord.Embed(
                    title="🎯 Tiro ao alvo aberto",
                    description=(
                        f"Entrada: {self._chip_amount(ALVO_STAKE)} por jogador\n"
                        f"Participantes: **{len(participants)}**\n"
                        f"{self._CHIP_GAIN_EMOJI} Pote atual: {self._chip_amount(pot_total)}\n"
                        + (f"{self._CHIP_BONUS_EMOJI} Bônus da rodada: {self._bonus_chip_amount(total_bonus)}\n" if total_bonus > 0 else "")
                        + "\n"
                        f"{self._target_opening_text(participants)}"
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="🎯 Na mira", value=self._format_target_participants(participants), inline=False)
                embed.add_field(name="🌪️ Condição da rodada", value=f"**{modifier.get('name','Alvo padrão')}**\n{modifier.get('description','Rodada normal')}", inline=False)
                embed.add_field(name="Como começa", value="Entre pelo botão verde\nDepois clique em **🏁 Iniciar** ou espere o tempo acabar", inline=False)
                embed.set_footer(text="Entrou, pagou e a entrada fica travada até o fim")

            if owner is not None:
                embed.set_author(name=f"Rodada aberta por {owner.display_name}", icon_url=owner.display_avatar.url)
            return embed
        async def _refresh_target_message(self, guild_id: int):
            session = self._get_target_session(guild_id)
            if session is None or session.get("ended"):
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            message = session.get("message")
            view = session.get("view")
            if message is None or view is None:
                return
            participants = self._get_target_participants(guild, session)
            view.join_button.label = f"🎯 Entrar ({len(participants)})"
            view.join_button.style = discord.ButtonStyle.success
            try:
                await message.edit(embed=self._make_target_embed(guild, session), view=view)
            except Exception:
                pass
        async def _handle_target_button(self, interaction: discord.Interaction, view: _TargetJoinView):
            guild = interaction.guild
            user = interaction.user
            if guild is None or not isinstance(user, discord.Member):
                try:
                    await interaction.response.send_message("Não foi possível entrar nessa rodada agora", ephemeral=True)
                except Exception:
                    pass
                return

            session = self._get_target_session(guild.id)
            if session is None or session.get("view") is not view or session.get("ended"):
                try:
                    await interaction.response.send_message("Essa rodada já terminou", ephemeral=True)
                except Exception:
                    pass
                return


            locked = session.setdefault("locked_participants", set())
            if user.id in locked:
                try:
                    await interaction.response.send_message("Você já entrou nessa rodada e sua entrada ficou travada até o fim", ephemeral=True)
                except Exception:
                    pass
                return

            entry_text = self._entry_consume_text(guild.id, user.id, ALVO_STAKE)
            paid, _balance, chip_note = await self._try_consume_chips(guild.id, user.id, ALVO_STAKE, reason="Entrada no alvo")
            if not paid:
                try:
                    await interaction.response.send_message(chip_note or "Você não tem saldo suficiente para entrar nessa rodada", ephemeral=True)
                except Exception:
                    pass
                return

            locked.add(user.id)
            try:
                await interaction.response.send_message(chip_note or entry_text, ephemeral=True)
            except Exception:
                pass
            await self._refresh_target_message(guild.id)
        async def _finish_target_round(self, guild_id: int, *, reason: str) -> bool:
            session = self._get_target_session(guild_id)
            if session is None or session.get("ended"):
                return False
            session["ended"] = True
            self._target_last_used[guild_id] = time.time()

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._target_sessions.pop(guild_id, None)
                return False

            message = session.get("message")
            view = session.get("view")
            if isinstance(view, discord.ui.View):
                for child in view.children:
                    child.disabled = True
                try:
                    view.stop()
                except Exception:
                    pass

            participants = self._get_target_participants(guild, session)
            locked_ids = set(session.get("locked_participants", set()))

            if len(locked_ids) == 1:
                only_id = next(iter(locked_ids))
                await self._refund_target_entry(guild.id, session, only_id)
                only_member = guild.get_member(only_id)
                final_text = f"A rodada foi cancelada porque só {only_member.mention if only_member else '1 participante'} entrou\nA entrada foi reembolsada"
            elif len(participants) < 2:
                for user_id in locked_ids:
                    await self._refund_target_entry(guild.id, session, user_id)
                final_text = "A rodada foi cancelada porque não ficaram participantes suficientes\nAs entradas foram reembolsadas"
            else:
                pot_total = len(locked_ids) * ALVO_STAKE
                bonus_chips = int(session.get("bonus_chips") or 0)
                modifier = session.get("modifier") or {"key": "normal", "name": "Alvo padrão", "description": "Rodada normal"}
                bonus_chips += int(modifier.get("pot_bonus", 0) or 0)
                if message is not None:
                    try:
                        await message.edit(embed=self._make_target_embed(guild, session, aiming=True), view=view)
                        await asyncio.sleep(1.35)
                    except Exception:
                        pass

                scores = {member.id: self._roll_target_score(str(modifier.get("key", "normal"))) for member in participants}
                rewards, placements = self._allocate_target_rewards(participants, scores, pot_total)
                bonus_rewards, _bonus_placements = self._allocate_target_rewards(participants, scores, bonus_chips) if bonus_chips > 0 else ({}, [])
                result_lines = [f"💥 Os tiros foram disparados\n{self._CHIP_GAIN_EMOJI} Pote base: {self._chip_amount(pot_total)}"]
                if bonus_chips > 0:
                    result_lines.append(f"{self._CHIP_BONUS_EMOJI} Bônus da rodada: {self._bonus_chip_amount(bonus_chips)}")
                result_lines.append("")
                bullseye_members: list[discord.Member] = []
                for member in sorted(participants, key=lambda m: (-scores.get(m.id, 0), m.display_name.casefold())):
                    score = scores.get(member.id, 0)
                    icon, zone = self._target_zone_style(score)
                    await self.db.add_user_game_stat(guild.id, member.id, "alvo_shots", 1)
                    await self._record_game_played(guild.id, member.id, weekly_points=4 + score)
                    if score > 0:
                        await self.db.add_user_game_stat(guild.id, member.id, "alvo_hits", 1)
                    result_lines.append(f"{icon} {member.mention} acertou **{zone}**")
                    if score == 3:
                        bullseye_members.append(member)
                        await self.db.add_user_game_stat(guild.id, member.id, "alvo_bullseyes", 1)

                if bullseye_members:
                    names = ", ".join(member.mention for member in bullseye_members)
                    result_lines.append("")
                    result_lines.append(f"🎯 Bullseye de destaque: {names}!")
                    bull_bonus = int(modifier.get("bullseye_bonus", 0) or 0)
                    if bull_bonus > 0:
                        for member in bullseye_members:
                            await self._change_user_bonus_chips(guild.id, member.id, bull_bonus, reason="Mosca no alvo")
                            await self._grant_weekly_points(guild.id, member.id, bull_bonus)
                        result_lines.append(f"{self._EFFECT_EMOJI} **Bullseye bônus:** cada acerto perfeito recebeu {self._bonus_chip_amount(bull_bonus)}")

                if rewards:
                    result_lines.append("")
                    for badge, members, total in placements:
                        if not members or total <= 0:
                            continue
                        member_mentions = ", ".join(member.mention for member in members)
                        result_lines.append(f"{badge} {member_mentions} — {self._chip_amount(total)}")
                    for user_id, amount in rewards.items():
                        if amount > 0:
                            await self._change_user_chips(guild.id, user_id, amount, reason="Prêmio do alvo")
                            await self.db.add_user_game_stat(guild.id, user_id, "alvo_wins", 1)
                            await self._grant_weekly_points(guild.id, user_id, max(5, amount // 4))
                    for user_id, amount in bonus_rewards.items():
                        if amount > 0:
                            await self._change_user_bonus_chips(guild.id, user_id, amount, reason="Bônus do alvo")
                final_text = "\n".join(result_lines)

            embed = self._make_target_embed(guild, session, final_text=final_text)
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

            self._target_sessions.pop(guild_id, None)
            return True
        async def _handle_target_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "alvo"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if self._get_target_session(guild.id) is not None:
                return True

            last_used = float(self._target_last_used.get(guild.id, 0.0) or 0.0)
            cooldown_remaining = max(0.0, (last_used + 6.0) - time.time())
            if cooldown_remaining > 0:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Aguarde um pouco", f"Espere **{int(cooldown_remaining) + 1}s** para abrir outra rodada de alvo", ok=False))
                except Exception:
                    pass
                return True

            paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ALVO_STAKE, reason="Entrada no alvo")
            if not paid:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Saldo insuficiente", chip_note or "Você não tem saldo suficiente", ok=False))
                except Exception:
                    pass
                return True

            participants_now = 1
            session = {
                "text_channel_id": message.channel.id,
                "owner_id": message.author.id,
                "locked_participants": {message.author.id},
                "modifier": self._roll_target_modifier(),
                "bonus_chips": self._target_bonus_for_participants(participants_now),
                "message": None,
                "view": view,
                "ended": False,
                "timeout_task": None,
            }
            view = _TargetJoinView(self, guild.id, session, guild, timeout=30.0)
            session["view"] = view
            self._target_sessions[guild.id] = session

            embed = self._make_target_embed(guild, session)
            if chip_note:
                embed.set_footer(text=f"{chip_note} Entrou, pagou e a entrada fica travada até o fim")
            try:
                panel_message = await message.channel.send(embed=embed, view=view)
            except Exception:
                self._target_sessions.pop(guild.id, None)
                await self._refund_target_entry(guild.id, session, message.author.id)
                return True

            session["message"] = panel_message
            session["timeout_task"] = self.bot.loop.create_task(view.wait())
            await self._react_with_emoji(message, "🎯", keep=True)
            return True
        async def _handle_disparar_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "disparar"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            session = self._get_target_session(guild.id)
            if session is None:
                return True

            participants = self._get_target_participants(guild, session)
            if len(participants) < 2:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Ainda faltam jogadores", "O alvo precisa de pelo menos **2 participantes** na call para disparar", ok=False))
                except Exception:
                    pass
                return True

            await self._finish_target_round(guild.id, reason="manual")
            await self._react_with_emoji(message, "💥", keep=True)
            return True


# --- V2 lobby overrides ---
class _TargetJoinView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaAlvoMixin", guild_id: int, session: dict, guild: discord.Guild, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.session = session
        self.guild = guild
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="🎯 Entrar (0)")
        self.join_button.callback = self._join_round
        self.start_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="🏁 Iniciar")
        self.start_button.callback = self._start_round
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        participants = self.cog._get_target_participants(self.guild, self.session)
        modifier = self.session.get("modifier") or {"name": "Alvo padrão", "description": "Rodada normal"}
        pot_total = len(self.session.get("locked_participants", set())) * ALVO_STAKE
        bonus_total = int(self.session.get("bonus_chips") or 0) + int(modifier.get("pot_bonus", 0) or 0)
        countdown = int(self.session.get("start_countdown") or 0)
        if countdown > 0:
            self.start_button.label = f"🏁 Iniciar ({countdown})"
            self.start_button.disabled = True
        else:
            self.start_button.label = "🏁 Iniciar"
            self.start_button.disabled = False
        self.join_button.label = f"🎯 Entrar ({len(participants)})"

        header = [
            "# 🎯 Tiro ao alvo",
            f"**Entrada:** {self.cog._chip_amount(ALVO_STAKE)}",
            f"**Pote atual:** {self.cog._chip_amount(pot_total)}" + (f" • Bônus: {self.cog._bonus_chip_amount(bonus_total)}" if bonus_total > 0 else ""),
            "**Inscrições:** 30s",
        ]
        info = [f"**Condição:** {modifier.get('name','Alvo padrão')}", f"**Pote base:** {self.cog._chip_amount(pot_total)}" + (f" • Bônus: {self.cog._bonus_chip_amount(bonus_total)}" if bonus_total > 0 else "")]
        desc = str(modifier.get('description') or '').strip()
        if desc:
            info.append(desc)
        plist = [f"### Participantes ({len(participants)})"]
        if participants:
            plist.extend(f"• {m.mention}" for m in participants)
        else:
            plist.append("• Ninguém entrou ainda")
        foot = ["Use o botão verde para entrar", "O criador da rodada ou a staff pode iniciar com 🏁 quando houver pelo menos 2 participantes"]
        if countdown > 0:
            foot.append("A contagem começou e ainda dá tempo de entrar")
        row = discord.ui.ActionRow(self.join_button, self.start_button)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(info)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(plist)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(foot)),
            row,
            accent_color=discord.Color.blurple(),
        ))

    async def _join_round(self, interaction: discord.Interaction):
        await self.cog._handle_target_button(interaction, self)

    async def _start_round(self, interaction: discord.Interaction):
        await self.cog._handle_target_start_button(interaction, self)

    async def on_timeout(self):
        try:
            session = self.cog._get_target_session(self.guild_id)
            if session is None or session.get('starting'):
                return
            await self.cog._finish_target_round(self.guild_id, reason='timeout')
        except Exception:
            pass


class _TargetLobbyClosedView(discord.ui.LayoutView):
    def __init__(self, session: dict, guild: discord.Guild, title: str, detail: str):
        super().__init__(timeout=None)
        modifier = session.get('modifier') or {'name': 'Alvo padrão'}
        participants = len(session.get('locked_participants', set()) or [])
        lines = [f"# {title}", f"**Condição:** {modifier.get('name','Alvo padrão')}", f"**Participantes:** {participants}", detail]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), accent_color=discord.Color.blurple()))




class _TargetStateView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaAlvoMixin", guild: discord.Guild, session: dict, *, finished: bool = False):
        super().__init__(timeout=None)
        modifier = session.get('modifier') or {'name': 'Alvo padrão'}
        participants = cog._get_target_participants(guild, session)
        if finished:
            summary = session.get('summary_line') or f"<:boom:1485862099308804107> **Pote distribuído:** {cog._chip_amount(int(session.get('prize_total') or 0))}"
            hits = session.get('hit_lines') or ['A rodada terminou']
            podium = session.get('podium_lines') or []
            closing = session.get('closing_line') or None
            items = [
                discord.ui.TextDisplay('# 🎯 Resultado da rodada\n' + f"**Condição:** {modifier.get('name','Alvo padrão')}\n" + f"**Participantes:** {len(participants)}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(summary),
                discord.ui.Separator(),
                discord.ui.TextDisplay('\n'.join(hits)),
            ]
            if podium:
                items.extend([discord.ui.Separator(), discord.ui.TextDisplay('\n'.join(podium))])
            if closing:
                items.extend([discord.ui.Separator(), discord.ui.TextDisplay(closing)])
            self.add_item(discord.ui.Container(*items, accent_color=discord.Color.blurple()))
        else:
            lines = [
                '# 🎯 Disparos em andamento',
                f"**Condição:** {modifier.get('name','Alvo padrão')}",
                f"**Participantes:** {len(participants)}",
                '',
                'Os participantes estão ajustando a mira',
            ]
            self.add_item(discord.ui.Container(discord.ui.TextDisplay('\n'.join(lines)), accent_color=discord.Color.blurple()))


class GincanaAlvoMixin(GincanaAlvoMixin):
    async def _refund_target_entry(self, guild_id: int, session: dict, user_id: int) -> None:
        raw_spend = (session.get('entry_spend') or {}).get(int(user_id)) or {
            'chips': ALVO_STAKE,
            'bonus': 0,
        }
        await self._refund_entry_spend(
            guild_id,
            int(user_id),
            raw_spend,
            reason="Devolução do alvo",
        )

    def _get_target_session(self, guild_id: int) -> dict | None:
        session = self._target_sessions.get(guild_id)
        if session and session.get('ended'):
            self._target_sessions.pop(guild_id, None)
            return None
        if session and self._runtime_state_is_stale(session, max_idle=120.0, max_age=300.0):
            if not session.get('_stale_cleanup_started'):
                session['_stale_cleanup_started'] = True
                session['ended'] = True
                self.bot.loop.create_task(self._cleanup_stale_target_session(guild_id, session))
            return None
        if session is not None:
            self._touch_runtime_state(session, kind='alvo', guild_id=guild_id)
        return session

    async def _cleanup_stale_target_session(self, guild_id: int, session: dict):
        if self._target_sessions.get(guild_id) is not session:
            return
        await self._safe_cancel_task(session.get('countdown_task'))
        guild = self.bot.get_guild(guild_id)
        locked_ids = {int(user_id) for user_id in (session.get('locked_participants', set()) or set())}
        if guild is not None:
            for user_id in sorted(locked_ids):
                try:
                    await self._refund_target_entry(guild.id, session, int(user_id))
                except Exception:
                    pass
        lobby_message = session.get('lobby_message') or session.get('message')
        if guild is not None and lobby_message is not None:
            try:
                await lobby_message.edit(view=_TargetLobbyClosedView(session, guild, '🎯 Rodada cancelada', 'A rodada foi encerrada automaticamente porque ficou travada por tempo demais\nAs entradas foram devolvidas'))
            except Exception:
                pass
        if self._target_sessions.get(guild_id) is session:
            self._target_sessions.pop(guild_id, None)

    def _target_render_key(self, session: dict, guild: discord.Guild) -> tuple:
        return (
            bool(session.get('ended')),
            bool(session.get('starting')),
            int(session.get('start_countdown', 0) or 0),
            tuple(sorted(int(x) for x in (session.get('locked_participants', set()) or set()))),
            int(session.get('bonus_chips', 0) or 0),
            tuple(int(member.id) for member in self._get_target_participants(guild, session)),
            str((session.get('modifier') or {}).get('key') or 'normal'),
        )

    async def _refresh_target_message(self, guild_id: int):
        session = self._get_target_session(guild_id)
        if session is None or session.get('ended'):
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
        render_key = self._target_render_key(session, guild)
        await self._safe_view_edit(message, view, state=session, render_key=render_key)

    async def _handle_target_button(self, interaction: discord.Interaction, view: _TargetJoinView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try: await interaction.response.send_message('Não foi possível entrar nessa rodada agora', ephemeral=True)
            except Exception: pass
            return
        session = self._get_target_session(guild.id)
        if session is None or session.get('view') is not view or session.get('ended'):
            try: await interaction.response.send_message('Essa rodada já terminou', ephemeral=True)
            except Exception: pass
            return
        locked = session.setdefault('locked_participants', set())
        if user.id in locked:
            try: await interaction.response.send_message('Você já entrou nessa rodada e sua entrada ficou travada até o fim', ephemeral=True)
            except Exception: pass
            return
        needs_negative_confirm = self._needs_negative_confirmation(guild.id, user.id, ALVO_STAKE)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, user.id, ALVO_STAKE, title="🎯 Confirmar entrada")
            if not confirmed:
                return
        entry_text = self._entry_consume_text(guild.id, user.id, ALVO_STAKE)
        entry_spend = self._entry_spend_parts(guild.id, user.id, ALVO_STAKE)
        paid, _balance, chip_note = await self._try_consume_chips(guild.id, user.id, ALVO_STAKE, reason="Entrada no alvo")
        if needs_negative_confirm:
            chip_note = None
        if not paid:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(chip_note or 'Você não tem saldo suficiente para entrar nessa rodada', ephemeral=True)
                else:
                    await interaction.response.send_message(chip_note or 'Você não tem saldo suficiente para entrar nessa rodada', ephemeral=True)
            except Exception:
                pass
            return
        locked.add(user.id)
        session.setdefault('entry_spend', {})[user.id] = entry_spend
        session.setdefault('race_interactions', {})[user.id] = interaction
        session['bonus_chips'] = self._target_bonus_for_participants(len(locked))
        self._touch_runtime_state(session, kind='alvo', guild_id=guild.id)
        await self._send_race_lobby_feedback(interaction, guild.id, user.id, chip_note or entry_text)
        await self._refresh_target_message(guild.id)

    async def _handle_target_start_button(self, interaction: discord.Interaction, view: _TargetJoinView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try: await interaction.response.send_message('Servidor inválido', ephemeral=True)
            except Exception: pass
            return
        session = self._get_target_session(guild.id)
        if session is None or session.get('view') is not view or session.get('ended'):
            try: await interaction.response.send_message('Essa rodada já terminou', ephemeral=True)
            except Exception: pass
            return
        if session.get('starting'):
            try: await interaction.response.send_message('A contagem já começou', ephemeral=True)
            except Exception: pass
            return
        is_owner = int(session.get('owner_id') or 0) == user.id
        if not is_owner and not self._is_staff_member(user):
            try: await interaction.response.send_message('Só o criador da rodada ou a staff pode iniciar', ephemeral=True)
            except Exception: pass
            return
        participants = self._get_target_participants(guild, session)
        if len(participants) < 2:
            try: await interaction.response.send_message('A rodada precisa de pelo menos 2 participantes', ephemeral=True)
            except Exception: pass
            return
        session['starting'] = True
        session['start_countdown'] = 3
        await self._safe_cancel_task(session.get('countdown_task'))
        session['countdown_task'] = self.bot.loop.create_task(self._run_target_start_countdown(guild.id, view))
        self._touch_runtime_state(session, kind='alvo', guild_id=guild.id)
        try: await interaction.response.send_message('Contagem iniciada', ephemeral=True)
        except Exception: pass
        await self._refresh_target_message(guild.id)

    async def _run_target_start_countdown(self, guild_id: int, view: _TargetJoinView):
        for remaining in range(3, 0, -1):
            session = self._get_target_session(guild_id)
            if session is None or session.get('ended') or session.get('view') is not view:
                return
            session['start_countdown'] = remaining
            self._touch_runtime_state(session, kind='alvo', guild_id=guild_id)
            await self._refresh_target_message(guild_id)
            await asyncio.sleep(1)
        session = self._get_target_session(guild_id)
        if session is None or session.get('ended') or session.get('view') is not view:
            return
        session['start_countdown'] = 0
        await self._finish_target_round(guild_id, reason='manual')

    async def _finish_target_round(self, guild_id: int, *, reason: str) -> bool:
        session = self._get_target_session(guild_id)
        if session is None or session.get('ended'):
            return False
        session['ended'] = True
        self._touch_runtime_state(session, kind='alvo', guild_id=guild_id)
        await self._safe_cancel_task(session.get('countdown_task'))
        self._target_last_used[guild_id] = time.time()
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._target_sessions.pop(guild_id, None)
            return False
        lobby_message = session.get('lobby_message') or session.get('message')
        view = session.get('view')
        if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            try: view.stop()
            except Exception: pass
        locked_ids = set(session.get('locked_participants', set()))
        participants = self._get_target_participants(guild, session)
        if len(locked_ids) == 1:
            only_id = next(iter(locked_ids))
            await self._refund_target_entry(guild.id, session, only_id)
            if lobby_message is not None:
                try:
                    await lobby_message.edit(view=_TargetLobbyClosedView(session, guild, '🎯 Rodada cancelada', 'A rodada precisa de pelo menos **2 participantes**\nA entrada foi devolvida'))
                except Exception: pass
            self._target_sessions.pop(guild_id, None)
            return True
        if len(participants) < 2:
            for user_id in locked_ids:
                await self._refund_target_entry(guild.id, session, user_id)
            if lobby_message is not None:
                try:
                    await lobby_message.edit(view=_TargetLobbyClosedView(session, guild, '🎯 Rodada cancelada', 'Não restaram participantes suficientes\nTodas as entradas foram devolvidas'))
                except Exception: pass
            self._target_sessions.pop(guild_id, None)
            return True
        message = lobby_message
        session['message'] = message
        if message is not None:
            try:
                state_view = _TargetStateView(self, guild, session, finished=False)
                session['view'] = state_view
                await message.edit(view=state_view)
            except Exception:
                pass
        # --- original resolution block with slight adjustments ---
        pot_total = len(locked_ids) * ALVO_STAKE
        bonus_chips = int(session.get('bonus_chips') or 0)
        modifier = session.get('modifier') or {'key': 'normal', 'name': 'Alvo padrão', 'description': 'Rodada normal'}
        bonus_chips += int(modifier.get('pot_bonus', 0) or 0)
        if message is not None:
            try:
                await asyncio.sleep(1.35)
            except Exception:
                pass
        scores = {member.id: self._roll_target_score(str(modifier.get('key', 'normal'))) for member in participants}
        rewards, placements = self._allocate_target_rewards(participants, scores, pot_total)
        bonus_rewards, _bonus_placements = self._allocate_target_rewards(participants, scores, bonus_chips) if bonus_chips > 0 else ({}, [])
        prize_total = pot_total
        hit_lines = []
        bullseye_members = []
        for member in sorted(participants, key=lambda m: (-scores.get(m.id, 0), m.display_name.casefold())):
            score = scores.get(member.id, 0)
            icon, zone = self._target_zone_style(score)
            await self.db.add_user_game_stat(guild.id, member.id, 'alvo_games', 1)
            await self.db.add_user_game_stat(guild.id, member.id, 'alvo_shots', 1)
            await self._record_game_played(guild.id, member.id, weekly_points=4 + score)
            if score > 0:
                await self.db.add_user_game_stat(guild.id, member.id, 'alvo_hits', 1)
            hit_lines.append(f"{icon} {member.mention} — **{zone}**")
            if score == 3:
                bullseye_members.append(member)
                await self.db.add_user_game_stat(guild.id, member.id, 'alvo_bullseyes', 1)
        bonus_line = None
        if bullseye_members:
            bull_bonus = int(modifier.get('bullseye_bonus', 0) or 0)
            if bull_bonus > 0:
                for member in bullseye_members:
                    await self._change_user_bonus_chips(guild.id, member.id, bull_bonus, reason="Mosca no alvo")
                    await self._grant_weekly_points(guild.id, member.id, bull_bonus)
                bonus_line = f"{self._EFFECT_EMOJI} **Bullseye bônus:** {self._bonus_chip_amount(bull_bonus)} para cada acerto perfeito"
        podium_lines = []
        winner_mentions = []
        winning_reward = 0
        if rewards:
            for badge, members, total in placements:
                names = ', '.join(member.mention for member in members)
                amount_text = self._chip_text(total, kind='gain') if badge == '🥇' else self._chip_amount(total)
                bonus_total = int(bonus_rewards.get(members[0].id, 0) or 0) if len(members) == 1 else sum(int(bonus_rewards.get(member.id, 0) or 0) for member in members)
                if bonus_total > 0:
                    amount_text += f" + {self._bonus_chip_amount(bonus_total)}"
                podium_lines.append(f"{badge} {names} — {amount_text}")
                if badge == '🥇':
                    winner_mentions.extend(member.mention for member in members)
                    winning_reward = total
            for user_id, amount in rewards.items():
                if amount > 0:
                    await self._change_user_chips(guild.id, user_id, amount, reason="Prêmio do alvo")
                    await self.db.add_user_game_stat(guild.id, user_id, 'alvo_wins', 1)
                    await self._grant_weekly_points(guild.id, user_id, max(3, amount // 4))
            for user_id, amount in bonus_rewards.items():
                if amount > 0:
                    await self._change_user_bonus_chips(guild.id, user_id, amount, reason="Bônus do alvo")
        owner_id = int(session.get('owner_id') or 0)
        public_race_notices: list[str] = []
        entry_spend_map = session.get('entry_spend') or {}
        bull_bonus_value = int(modifier.get('bullseye_bonus', 0) or 0)
        bullseye_ids = {member.id for member in bullseye_members}
        for member in participants:
            payout = int(rewards.get(member.id, 0) or 0) + int(bonus_rewards.get(member.id, 0) or 0)
            if member.id in bullseye_ids:
                payout += bull_bonus_value
            notes = await self._apply_new_race_result(
                guild.id,
                member.id,
                won=int(rewards.get(member.id, 0) or 0) > 0,
                entry_spend=entry_spend_map.get(member.id) or {'chips': ALVO_STAKE, 'bonus': 0},
                payout=payout,
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
        for member in participants:
            if int(rewards.get(member.id, 0) or 0) > 0:
                continue
            refund, refund_mode = await self._maybe_apply_coringa_loss_refund(
                guild.id,
                member.id,
                ALVO_STAKE,
                chance=0.35,
            )
            if refund <= 0:
                continue
            refund_note = self._race_effect_message(
                guild.id,
                member.id,
                'joker' if refund_mode == 'joker' else 'as',
                (
                    f"recuperou **{refund}** {self._CHIP_BONUS_EMOJI} da entrada"
                    if refund_mode == 'joker'
                    else f"recuperou {self._chip_text(refund, kind='gain')} da entrada"
                ),
            )
            await self._route_lobby_race_notices(
                (session.get('race_interactions') or {}).get(member.id),
                guild.id,
                member.id,
                owner_id,
                [refund_note],
                public_race_notices,
            )

        closing_parts = []
        if winner_mentions and len(winner_mentions) > 1:
            closing_parts.append(f"🔥 {', '.join(winner_mentions)} dividiram a ponta")
        special_lines = self._build_target_special_lines(participants, scores, placements)
        if special_lines:
            for line in special_lines:
                if 'levou a melhor' in line or 'dividiram a ponta' in line:
                    continue
                if line not in closing_parts:
                    closing_parts.append(line)
        if bonus_line:
            closing_parts.append(bonus_line)
        public_race_notices = self._clean_race_notices(public_race_notices)
        closing_limit = max(0, 16 - len(public_race_notices))
        closing_parts = closing_parts[:closing_limit] + public_race_notices[:16]
        session['summary_line'] = f"<:boom:1485862099308804107> **Pote distribuído:** {self._chip_amount(prize_total)}"
        if bonus_chips > 0:
            session['summary_line'] += f" • Bônus: {self._bonus_chip_amount(bonus_chips)}"
        session['hit_lines'] = hit_lines
        if winner_mentions and len(winner_mentions) == 1:
            session['podium_lines'] = [f"🥇 **Vencedor:** {winner_mentions[0]} — {self._chip_text(winning_reward, kind='gain')}"]
        else:
            session['podium_lines'] = podium_lines
        session['closing_line'] = '\n'.join(closing_parts) if closing_parts else None
        session['prize_total'] = prize_total
        result_lines = [session['summary_line'], '', *hit_lines]
        if session['podium_lines']:
            result_lines += ['', *session['podium_lines']]
        if session['closing_line']:
            result_lines += ['', session['closing_line']]
        final_text = "\n".join(result_lines)
        session['result_lines'] = result_lines
        first_game_user_ids = await self._unlock_first_game_for_users(
            guild.id,
            [member.id for member in participants],
        )
        bullseye_achievement_user_ids: list[int] = []
        for member in bullseye_members:
            if await self._unlock_achievement(guild.id, member.id, "target_bullseye"):
                bullseye_achievement_user_ids.append(member.id)
        achievement_notices_by_user: dict[int, list[str]] = {}
        for user_id in first_game_user_ids:
            achievement_notices_by_user.setdefault(int(user_id), []).append("first_game")
        for user_id in bullseye_achievement_user_ids:
            achievement_notices_by_user.setdefault(int(user_id), []).append("target_bullseye")
        result_delivered = False
        if message is not None:
            final_view = _TargetStateView(self, guild, session, finished=True)
            session['view'] = final_view
            edit_state = await self._safe_view_edit(message, final_view)
            result_delivered = edit_state in {'ok', 'skipped'}
        if public_race_notices and not result_delivered:
            self._queue_private_race_notices(guild.id, owner_id, public_race_notices)
        achievement_channel = (
            getattr(message, "channel", None)
            or getattr(lobby_message, "channel", None)
            or guild.get_channel(int(session.get("text_channel_id", 0) or 0))
        )
        for user_id, achievement_keys in achievement_notices_by_user.items():
            await self._send_achievement_notices(
                achievement_channel,
                guild.id,
                user_id,
                achievement_keys,
            )
        current = self._target_sessions.get(guild_id)
        if current is session:
            self._target_sessions.pop(guild_id, None)
        return True

    async def _handle_target_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if not self._matches_exact_trigger(message.content or '', 'alvo'):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        if self._get_target_session(guild.id) is not None:
            return True
        needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, ALVO_STAKE)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, ALVO_STAKE, title="🎯 Confirmar entrada")
            if not confirmed:
                return True
        entry_spend = self._entry_spend_parts(guild.id, message.author.id, ALVO_STAKE)
        paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ALVO_STAKE, reason="Entrada no alvo")
        if needs_negative_confirm:
            chip_note = None
        if not paid:
            try: await message.channel.send(embed=self._make_embed('🎯 Saldo insuficiente', chip_note or 'Você não tem saldo suficiente', ok=False))
            except Exception: pass
            return True
        session = {
            'text_channel_id': message.channel.id,
            'owner_id': message.author.id,
            'locked_participants': {message.author.id},
            'entry_spend': {message.author.id: entry_spend},
            'modifier': self._roll_target_modifier(),
            'bonus_chips': self._target_bonus_for_participants(1),
            'lobby_message': None,
            'message': None,
            'view': None,
            'ended': False,
            'starting': False,
            'start_countdown': 0,
            'countdown_task': None,
            '_last_render_key': None,
        }
        self._touch_runtime_state(session, kind='alvo', guild_id=guild.id)
        self._target_sessions[guild.id] = session
        view = _TargetJoinView(self, guild.id, session, guild, timeout=30.0)
        session['view'] = view
        try:
            panel_message = await message.channel.send(view=view)
        except Exception:
            self._target_sessions.pop(guild.id, None)
            await self._refund_target_entry(guild.id, session, message.author.id)
            return True
        session['lobby_message'] = panel_message
        session['message'] = panel_message
        await self._react_with_emoji(message, '🎯', keep=True)
        return True

    async def _handle_disparar_trigger(self, message: discord.Message) -> bool:
        return False
