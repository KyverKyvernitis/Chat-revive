import asyncio
import base64
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from config import CALLKEEPER_BOT_TOKENS, OFF_COLOR, ON_COLOR
from ..constants import (
    CHIPS_DEFAULT,
    CHIPS_INITIAL,
    CHIPS_RECHARGE_THRESHOLD,
    CHIPS_RESET_HOURS,
    CHIPS_RESET_SECONDS,
    CHIPS_MENDIGAR_COOLDOWN_SECONDS,
    RACE_REROLL_COST,
    RACE_SPECIAL_DEFAULT_CHANCE,
    RACE_SPECIAL_SORTUDO_CHANCE,
    ROLETA_APOSTADOR_COST,
    ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS,
    ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS,
    ROLETA_COST,
    TRUCO_GOLDEN_BONUS_EXTRA,
)
from db import SettingsDB
from .session_registry import GameSessionRegistry, MAX_ACTIVE_GAME_USERS_PER_GUILD


SORTUDO_BLESSING_INTERVAL_SECONDS = 7 * 60 * 60
SORTUDO_DAILY_EXTRA_BONUS = 5
SORTUDO_STREAK_EXTRA_BONUS = 5


class _NegativeDebtConfirmView(discord.ui.View):
    def __init__(self, *, owner_id: int, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.confirmed = False
        self.message = None

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Essa confirmação não é para você", ephemeral=True)
            return
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Essa confirmação não é para você", ephemeral=True)
            return
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.edit(content="Entrada cancelada", view=None)
            else:
                await interaction.edit_original_response(content="Entrada cancelada", view=None)
        except Exception:
            try:
                await interaction.edit_original_response(content="Entrada cancelada", view=None)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self):
        try:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                await self.message.edit(view=None)
        except Exception:
            pass


class GincanaBase:
    _GINCANA_SUFFIXES = (" [ultra-censurado]", " [censurado]", " [antitts]")
    _CHIP_EMOJI = "<:emoji_63:1485041721573249135>"
    _CHIP_GAIN_EMOJI = "<:emoji_64:1485043651292827788>"
    _CHIP_LOSS_EMOJI = "<:emoji_65:1485043671077228786>"
    _CHIP_BONUS_EMOJI = "<:laranja:1487076933819830443>"
    _EFFECT_EMOJI = "<:star:1487936913431072780>"
    _MAX_CHIP_DEBT = 100
    _ACHIEVEMENT_THUMBNAIL_FILENAME = "achievement-unlocked.gif"
    _ACHIEVEMENT_THUMBNAIL_PATH = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / _ACHIEVEMENT_THUMBNAIL_FILENAME
    )

    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db
        self._pica_expirations: dict[tuple[int, int], float] = {}
        self._rola_expirations: dict[tuple[int, int], float] = {}
        self._dj_expirations: dict[tuple[int, int, int], float] = {}
        self._gincana_timed_effects_rehydrated: bool = False
        self._roleta_last_used: dict[int, float] = {}
        self._roleta_running_guilds: set[int] = set()
        self._buckshot_sessions: dict[int, dict] = {}
        self._target_sessions: dict[int, dict] = {}
        self._target_last_used: dict[int, float] = {}
        self._buckshot_last_used: dict[int, float] = {}
        self._poker_games: dict[int, object] = {}
        self._payment_sessions: dict[tuple[int, int], dict] = {}
        self._race_sessions: dict[int, dict] = {}
        self._race_panel_messages: dict[tuple[int, int], tuple[int, int]] = {}
        self._game_sessions = GameSessionRegistry(
            max_active_users_per_guild=MAX_ACTIVE_GAME_USERS_PER_GUILD
        )
        self._truco_games: dict[str, object] = {}
        self._truco_guild_sessions: dict[int, set[str]] = {}
        self._gincana_message_edit_locks: dict[int, asyncio.Lock] = {}
        self._race_progress_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._achievement_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._race_private_notices: dict[tuple[int, int], list[str]] = {}


    def _touch_runtime_state(self, state: dict | None, *, kind: str | None = None, guild_id: int | None = None) -> float:
        now = time.monotonic()
        if state is None:
            return now
        if not isinstance(state, dict):
            return now
        state.setdefault("_created_at", now)
        state["_heartbeat_at"] = now
        if kind and not state.get("_runtime_kind"):
            state["_runtime_kind"] = str(kind)
        if guild_id is not None and not state.get("_runtime_guild_id"):
            state["_runtime_guild_id"] = int(guild_id)
        return now

    def _runtime_state_age(self, state: dict | None) -> float:
        if not isinstance(state, dict):
            return 0.0
        now = time.monotonic()
        created_at = float(state.get("_created_at") or now)
        return max(0.0, now - created_at)

    def _runtime_state_idle_for(self, state: dict | None) -> float:
        if not isinstance(state, dict):
            return 0.0
        now = time.monotonic()
        heartbeat_at = float(state.get("_heartbeat_at") or state.get("_created_at") or now)
        return max(0.0, now - heartbeat_at)

    def _runtime_state_is_stale(self, state: dict | None, *, max_idle: float, max_age: float | None = None) -> bool:
        if not isinstance(state, dict):
            return False
        if max_idle > 0 and self._runtime_state_idle_for(state) > float(max_idle):
            return True
        if max_age is not None and max_age > 0 and self._runtime_state_age(state) > float(max_age):
            return True
        return False

    def _runtime_lock(self, state: dict, *, key: str = "_runtime_lock") -> asyncio.Lock:
        lock = state.get(key)
        if isinstance(lock, asyncio.Lock):
            return lock
        lock = asyncio.Lock()
        state[key] = lock
        return lock

    async def _safe_cancel_task(self, task) -> bool:
        if task is None:
            return False
        if task is asyncio.current_task():
            return False
        if getattr(task, "done", None) is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        return True

    def _message_edit_lock(self, message: discord.Message | None) -> asyncio.Lock:
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            return asyncio.Lock()
        lock = self._gincana_message_edit_locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self._gincana_message_edit_locks[message_id] = lock
        return lock

    async def _safe_view_edit(self, message: discord.Message | None, view: discord.ui.View | discord.ui.LayoutView, *, state: dict | None = None, render_key=None) -> str:
        if message is None:
            return "missing"
        if state is not None and render_key is not None and state.get("_last_render_key") == render_key:
            return "skipped"
        lock = self._message_edit_lock(message)
        async with lock:
            if state is not None and render_key is not None and state.get("_last_render_key") == render_key:
                return "skipped"
            try:
                await message.edit(view=view)
                if state is not None and render_key is not None:
                    state["_last_render_key"] = render_key
                    self._touch_runtime_state(state)
                return "ok"
            except discord.NotFound:
                if state is not None:
                    state["message"] = None
                    state["lobby_message"] = None
                return "missing"
            except discord.HTTPException:
                return "error"
            except Exception:
                return "error"

    def _strip_gincana_suffix(self, name: str) -> str:
        base = str(name or "").rstrip()
        lowered = base.casefold()
        for suffix in self._GINCANA_SUFFIXES:
            if lowered.endswith(suffix.casefold()):
                return base[: -len(suffix)].rstrip()
        return base

    def _target_suffix(self, member: discord.Member, ignored_tts_role: discord.Role | None) -> str:
        is_muted = False
        voice_state = getattr(member, "voice", None)
        if voice_state is not None:
            try:
                is_muted = bool(getattr(voice_state, "mute", False))
            except Exception:
                is_muted = False

        ignores_tts = ignored_tts_role is not None and ignored_tts_role in getattr(member, "roles", [])
        if is_muted and ignores_tts:
            return " [ultra-censurado]"
        if is_muted:
            return " [censurado]"
        if ignores_tts:
            return " [antitts]"
        return ""

    async def _refresh_target_suffix_nickname(self, member: discord.Member, ignored_tts_role: discord.Role | None):
        me = member.guild.me
        if me is None:
            return

        perms = getattr(me.guild_permissions, "manage_nicknames", False)
        if not perms:
            return

        try:
            if member == member.guild.owner:
                return
            if getattr(me, "top_role", None) is not None and getattr(member, "top_role", None) is not None:
                if me.top_role <= member.top_role:
                    return
        except Exception:
            pass

        current_nick = member.nick
        current_display_name = str(getattr(member, "display_name", "") or "").strip()
        current_name = current_nick if current_nick is not None else current_display_name or member.name
        base_name = self._strip_gincana_suffix(current_name) or self._strip_gincana_suffix(current_display_name) or member.name
        suffix = self._target_suffix(member, ignored_tts_role)
        desired_full = f"{base_name}{suffix}".strip()

        current_nick_has_managed_suffix = bool(current_nick and self._strip_gincana_suffix(current_nick) != current_nick)

        if current_nick is None:
            if not suffix:
                return
            if desired_full == current_display_name:
                return
            new_nick = desired_full
        else:
            if not suffix:
                if current_nick_has_managed_suffix:
                    new_nick = None
                elif base_name == member.name:
                    new_nick = None
                else:
                    return
            else:
                new_nick = desired_full

        if isinstance(new_nick, str) and len(new_nick) > 32:
            allowed = max(0, 32 - len(suffix))
            trimmed = base_name[:allowed].rstrip()
            new_nick = f"{trimmed}{suffix}".strip() if suffix else (trimmed or None)
            if current_nick is None and new_nick == member.name:
                return

        if new_nick == current_nick:
            return

        try:
            await member.edit(nick=new_nick, reason="economia atualizar sufixo do alvo")
        except Exception:
            pass

    async def _refresh_targets_suffix_nicknames(self, guild: discord.Guild, targets: list[discord.Member]):
        ignored_tts_role = None
        ignored_tts_role_id = 0
        try:
            ignored_tts_role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            ignored_tts_role_id = 0
        if ignored_tts_role_id:
            ignored_tts_role = guild.get_role(ignored_tts_role_id)

        for target in targets:
            await self._refresh_target_suffix_nickname(target, ignored_tts_role)

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR),
        )


    def _make_v2_notice(self, title: str, lines: list[str], *, ok: bool = True, accent_color: discord.Color | None = None) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        color = accent_color or (discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR))
        body = [f"# {title}"]
        body.extend([str(x) for x in lines if str(x).strip()])
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(body)), accent_color=color))
        return view

    def _gincana_input_mode(self, guild_id: int) -> str:
        getter = getattr(self.db, "get_gincana_input_mode", None)
        if not callable(getter):
            return "triggers"
        try:
            value = str(getter(int(guild_id)) or "triggers").strip().casefold()
        except Exception:
            return "triggers"
        return "commands" if value == "commands" else "triggers"

    def _gincana_channel_id(self, guild_id: int) -> int:
        getter = getattr(self.db, "get_gincana_channel_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter(int(guild_id)) or 0))
        except Exception:
            return 0

    def _get_gincana_channel(self, guild: discord.Guild):
        channel_id = self._gincana_channel_id(guild.id)
        if not channel_id:
            return None
        getter = getattr(guild, "get_channel_or_thread", None)
        if callable(getter):
            channel = getter(channel_id)
            if channel is not None:
                return channel
        return guild.get_channel(channel_id)

    def _gincana_channel_matches(self, guild: discord.Guild, channel) -> bool:
        configured_id = self._gincana_channel_id(guild.id)
        if not configured_id:
            return True
        configured = self._get_gincana_channel(guild)
        if configured is None:
            # Canal removido: o servidor volta ao comportamento padrão até a
            # configuração ser aberta e o ID antigo ser limpo.
            return True
        try:
            channel_id = int(getattr(channel, "id", 0) or 0)
            parent_id = int(getattr(channel, "parent_id", 0) or 0)
        except Exception:
            return False
        return channel_id == configured_id or parent_id == configured_id

    def _games_trigger_entry_allowed(self, guild: discord.Guild, channel) -> bool:
        if self._gincana_input_mode(guild.id) != "triggers":
            return False
        return self._gincana_channel_matches(guild, channel)

    async def _ensure_games_command_entry(
        self,
        ctx: commands.Context,
        *,
        trigger_hint: str | None = None,
    ) -> bool:
        guild = ctx.guild
        if guild is None:
            await ctx.reply(
                view=self._make_v2_notice("Servidor inválido", ["Use esse comando dentro de um servidor"], ok=False),
                mention_author=False,
            )
            return False

        if self._gincana_input_mode(guild.id) != "commands":
            lines = ["Este servidor usa triggers"]
            if trigger_hint:
                lines.append(f"Use `{trigger_hint}` sem prefixo")
            await ctx.reply(
                view=self._make_v2_notice("Modo por triggers", lines, ok=False),
                mention_author=False,
            )
            return False

        if not self._gincana_channel_matches(guild, ctx.channel):
            channel = self._get_gincana_channel(guild)
            channel_text = getattr(channel, "mention", "o canal configurado")
            await ctx.reply(
                view=self._make_v2_notice(
                    "Canal exclusivo",
                    [f"Use os comandos de jogos em {channel_text}"],
                    ok=False,
                ),
                mention_author=False,
            )
            return False
        return True

    def _call_trigger_channel(self, message: discord.Message) -> discord.VoiceChannel | discord.StageChannel | None:
        channel = getattr(message, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    def _is_call_trigger_context(self, message: discord.Message) -> bool:
        return self._call_trigger_channel(message) is not None

    def _chip_text(self, amount: int | str, *, kind: str = "balance") -> str:
        emoji = self._CHIP_EMOJI
        if kind == "gain":
            emoji = self._CHIP_GAIN_EMOJI
        elif kind == "loss":
            emoji = self._CHIP_LOSS_EMOJI
        return f"**{amount} {emoji}**"

    def _chip_amount(self, amount: int | str) -> str:
        return f"**{amount} {self._CHIP_EMOJI}**"

    def _bonus_chip_amount(self, amount: int | str) -> str:
        return f"**{amount} {self._CHIP_BONUS_EMOJI}**"

    def _chip_label(self) -> str:
        return f"{self._CHIP_EMOJI} Fichas"

    def _format_rate_decimal(self, value: float) -> str:
        return f"{round(float(value), 1):.1f}".replace('.', ',') + '%'

    def _chip_summary_stats(self, stats: dict) -> tuple[int, int, int, str]:
        wins = (
            int(stats.get('poker_wins', 0) or 0)
            + int(stats.get('alvo_wins', 0) or 0)
            + int(stats.get('corrida_wins', 0) or 0)
            + int(stats.get('buckshot_survivals', 0) or 0)
            + int(stats.get('truco_wins', 0) or 0)
        )
        losses = (
            int(stats.get('poker_losses', 0) or 0)
            + int(stats.get('corrida_losses', 0) or 0)
            + int(stats.get('buckshot_eliminations', 0) or 0)
            + int(stats.get('truco_losses', 0) or 0)
        )
        games = wins + losses
        rate = self._format_rate_decimal((wins / games) * 100.0) if games > 0 else '0,0%'
        return wins, losses, games, rate

    def _has_meaningful_chip_profile(self, guild_id: int, user_id: int) -> bool:
        return bool(self.db.user_has_chip_activity(guild_id, user_id))

    async def _mark_chip_activity(self, guild_id: int, user_id: int):
        await self.db.mark_user_chip_activity(guild_id, user_id)

    async def _clear_chip_activity(self, guild_id: int, user_id: int):
        await self.db.set_user_chip_activity(guild_id, user_id, False)

    async def _set_user_chips_value(self, guild_id: int, user_id: int, chips: int, *, mark_activity: bool = True) -> int:
        await self.db.set_user_chips(guild_id, user_id, int(chips))
        if mark_activity:
            await self._mark_chip_activity(guild_id, user_id)
        return self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)

    def _get_user_bonus_chips(self, guild_id: int, user_id: int) -> int:
        try:
            return max(0, int(self.db.get_user_bonus_chips(guild_id, user_id) or 0))
        except Exception:
            return 0

    async def _change_user_bonus_chips(self, guild_id: int, user_id: int, amount: int, *, mark_activity: bool = True, reason: str | None = None) -> int:
        delta = int(amount)
        new_bonus = await self.db.add_user_bonus_chips(guild_id, user_id, delta)
        if delta != 0:
            if mark_activity:
                await self._mark_chip_activity(guild_id, user_id)
            try:
                await self.db.append_chip_history(guild_id, user_id, delta=delta, kind="bonus", reason=reason)
            except Exception:
                pass
        return int(new_bonus)

    async def _change_user_chips(self, guild_id: int, user_id: int, amount: int, *, mark_activity: bool = True, reason: str | None = None) -> int:
        delta = int(amount)
        new_balance = await self.db.add_user_chips(guild_id, user_id, delta)
        if delta != 0:
            if mark_activity:
                await self._mark_chip_activity(guild_id, user_id)
            try:
                await self.db.append_chip_history(guild_id, user_id, delta=delta, kind="chips", reason=reason)
            except Exception:
                pass
        return int(new_balance)

    async def _transfer_user_chips(self, guild_id: int, payer_id: int, target_id: int, *, total: int, net_amount: int, payer_reason: str | None = None, target_reason: str | None = None) -> tuple[int, int]:
        payer_balance = await self._change_user_chips(guild_id, payer_id, -int(total), mark_activity=True, reason=payer_reason)
        target_balance = await self._change_user_chips(guild_id, target_id, int(net_amount), mark_activity=True, reason=target_reason)
        return payer_balance, target_balance

    async def _claim_daily_bonus_with_activity(self, guild_id: int, user_id: int, *, base_amount: int = 10) -> tuple[bool, int, int, int, int]:
        claimed, new_balance, bonus, streak = await self.db.claim_daily_bonus(guild_id, user_id, base_amount=base_amount)
        bonus_bonus = 10
        sortudo_extra = 0
        if claimed and self._race_is(guild_id, user_id, "sortudo"):
            sortudo_extra += SORTUDO_DAILY_EXTRA_BONUS
            if int(bonus) > int(base_amount):
                sortudo_extra += SORTUDO_STREAK_EXTRA_BONUS
            if sortudo_extra > 0:
                await self.db.add_user_bonus_chips(guild_id, user_id, sortudo_extra)
                bonus_bonus += sortudo_extra
        if claimed:
            await self._mark_chip_activity(guild_id, user_id)
            try:
                if int(bonus) > 0:
                    await self.db.append_chip_history(guild_id, user_id, delta=int(bonus), kind="chips", reason="Bônus diário")
                await self.db.append_chip_history(guild_id, user_id, delta=10, kind="bonus", reason="Bônus diário")
                if sortudo_extra > 0:
                    await self.db.append_chip_history(
                        guild_id,
                        user_id,
                        delta=sortudo_extra,
                        kind="bonus",
                        reason="Prêmio Extra do Sortudo",
                    )
            except Exception:
                pass
        return claimed, new_balance, bonus, bonus_bonus, streak

    def _daily_streak_label(self, streak: int) -> str:
        value = max(0, int(streak or 0))
        return "1 dia consecutivo" if value == 1 else f"{value} dias consecutivos"

    def _daily_streak_progress(self, streak: int) -> str:
        filled = min(7, max(0, int(streak or 0)))
        return f"`{'▰' * filled}{'▱' * (7 - filled)}`  **{filled}/7**"

    def _daily_reset_remaining_seconds(self) -> float:
        try:
            now = self.db._sao_paulo_now()
            next_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return max(0.0, float((next_reset - now).total_seconds()))
        except Exception:
            return 24 * 60 * 60

    def _format_daily_reset_remaining(self, seconds: float) -> str:
        try:
            total_minutes = max(1, int((float(seconds) + 59) // 60))
        except Exception:
            total_minutes = 1
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{hours}h {minutes}min"
        if hours:
            return f"{hours}h"
        return f"{minutes}min"

    def _daily_streak_transition(self, status: dict) -> str:
        last_key = str(status.get("last_claim_key", "") or "")
        current_streak = max(0, int(status.get("streak", 0) or 0))
        if not last_key or current_streak <= 0:
            return "started"
        try:
            today = datetime.strptime(str(status.get("today_key", "")), "%Y-%m-%d").date()
            last_claim = datetime.strptime(last_key, "%Y-%m-%d").date()
            return "continued" if today - last_claim == timedelta(days=1) else "restarted"
        except Exception:
            return "restarted"

    def _make_daily_view(
        self,
        guild_id: int,
        user_id: int,
        *,
        claimed: bool,
        streak: int,
        bonus: int = 0,
        bonus_bonus: int = 0,
        spin_granted: bool = False,
        carta_spin_granted: bool = False,
        streak_transition: str = "continued",
        race_note: str = "",
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        streak_value = max(0, int(streak or 0))
        streak_label = self._daily_streak_label(streak_value)
        streak_progress = self._daily_streak_progress(streak_value)

        if not claimed:
            remaining = self._format_daily_reset_remaining(self._daily_reset_remaining_seconds())
            view.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("# ⏳ Bônus diário já resgatado"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        "## 🔥 Ofensiva\n"
                        f"**{streak_label}**\n"
                        f"**Progresso:** {streak_progress}"
                    ),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(
                        "## Próximo resgate\n"
                        f"Disponível em **{remaining}**"
                    ),
                    accent_color=discord.Color.orange(),
                )
            )
            return view

        reward_lines = [
            "## Recompensas de hoje",
            f"• **+{int(bonus)}** {self._CHIP_EMOJI} fichas",
            f"• **+{int(bonus_bonus)}** {self._CHIP_BONUS_EMOJI} fichas bônus",
            (
                "• 🎰 **+1 giro de roleta**"
                if spin_granted
                else "• 🎰 Giro extra da roleta **já disponível**"
            ),
            (
                "• 🎴 **+1 giro de cartas**"
                if carta_spin_granted
                else "• 🎴 Giro extra de cartas **já disponível**"
            ),
        ]
        extra_streak_bonus = max(0, int(bonus) - 10)
        if extra_streak_bonus > 0:
            reward_lines.append(
                f"-# A recompensa inclui +{extra_streak_bonus} {self._CHIP_EMOJI} pela ofensiva"
            )
        if race_note:
            reward_lines.extend(["", str(race_note).strip()])
        streak_lines = [
            "## 🔥 Ofensiva",
            f"**{streak_label}**",
            f"**Progresso:** {streak_progress}",
        ]
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🎁 Bônus diário resgatado"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(reward_lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(streak_lines)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "## Saldo atual\n"
                    f"{self._format_compact_chip_balance(guild_id, user_id)}"
                ),
                accent_color=discord.Color.green(),
            )
        )
        return view

    async def _claim_daily_view(self, guild_id: int, user_id: int) -> discord.ui.LayoutView:
        previous_status = self.db.get_user_daily_status(guild_id, user_id)
        streak_transition = self._daily_streak_transition(previous_status)
        claimed, _new_balance, bonus, bonus_bonus, streak = await self._claim_daily_bonus_with_activity(guild_id, user_id)
        if not claimed:
            return self._make_daily_view(guild_id, user_id, claimed=False, streak=streak)

        await self._grant_weekly_points(guild_id, user_id, max(3, bonus // 2))
        spin_granted, _spin_state = await self._grant_daily_roleta_spin(guild_id, user_id)
        carta_spin_granted, _carta_spin_state = await self._grant_daily_carta_spin(guild_id, user_id)
        race_note = ""
        if self._race_is(guild_id, user_id, "sortudo") and int(bonus_bonus) > 10:
            extra = int(bonus_bonus) - 10
            detail = (
                f"+{extra} {self._CHIP_BONUS_EMOJI}: +5 no Daily e +5 pela ofensiva"
                if extra >= 10
                else f"+{extra} {self._CHIP_BONUS_EMOJI} no Daily"
            )
            race_note = self._race_effect_message(guild_id, user_id, "premio_extra", detail)
        return self._make_daily_view(
            guild_id,
            user_id,
            claimed=True,
            streak=streak,
            bonus=bonus,
            bonus_bonus=bonus_bonus,
            spin_granted=spin_granted,
            carta_spin_granted=carta_spin_granted,
            streak_transition=streak_transition,
            race_note=race_note,
        )

    async def _force_reset_chips(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        async with self._achievement_lock(guild_id, user_id):
            await self._set_user_chips_value(guild_id, user_id, int(amount), mark_activity=True)
            await self.db.set_user_bonus_chips(guild_id, user_id, 0)
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["last_chip_reset_at"] = 0.0
            doc["chip_recharge_manual_initialized"] = False
            doc.pop("race_key", None)
            doc.pop("race_active", None)
            doc.pop("game_achievements", None)
            for field in ("race_free_roleta_spins", "race_free_carta_spins", "race_sortudo_blessing_charges", "race_sortudo_blessing_started_at", "race_robbery_window_started_at", "race_robbery_uses", "race_mendigar_window_started_at", "race_mendigar_uses", "race_state"):
                doc.pop(field, None)
            await self.db._save_user_doc(guild_id, user_id, doc)
            await self.db.clear_user_game_achievements(guild_id, user_id)
            return int(amount)

    async def _force_full_reset_ficha_profile(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        async with self._achievement_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["chips"] = int(amount)
            doc["bonus_chips"] = 0
            doc["last_chip_reset_at"] = 0.0
            doc["chip_recharge_manual_initialized"] = False
            doc["daily_last_claim_key"] = ""
            doc["daily_streak"] = 0
            doc["weekly_points_week"] = ""
            doc["weekly_points"] = 0
            doc["game_stats"] = {}
            doc["has_chip_activity"] = False
            doc.pop("race_key", None)
            doc.pop("race_active", None)
            doc.pop("game_achievements", None)
            for field in ("race_free_roleta_spins", "race_free_carta_spins", "race_sortudo_blessing_charges", "race_sortudo_blessing_started_at", "race_robbery_window_started_at", "race_robbery_uses", "race_mendigar_window_started_at", "race_mendigar_uses", "race_state"):
                doc.pop(field, None)
            await self.db._save_user_doc(guild_id, user_id, doc)
            await self.db.clear_user_game_achievements(guild_id, user_id)
            return int(doc["chips"])

    def _iter_active_chip_user_ids(self, guild_id: int) -> list[int]:
        user_ids = {int(user_id) for user_id in self.db.get_chip_activity_user_ids(guild_id)}
        for (stored_guild_id, user_id), doc in list(self.db.user_cache.items()):
            if int(stored_guild_id) != int(guild_id):
                continue
            achievement_data = doc.get("game_achievements") or {}
            unlocked = achievement_data.get("unlocked") or {}
            if isinstance(unlocked, dict) and unlocked:
                user_ids.add(int(user_id))
        return sorted(user_ids)

    def _achievement_catalog(self) -> dict[str, dict[str, str]]:
        return {
            "first_game": {
                "name": "O começo",
                "emoji": "🏆",
                "description": "{mention} jogou pela primeira vez",
            },
            "lets_go_gambling": {
                "name": "Let's go gambling!",
                "emoji": "🎰",
                "description": "{mention} girou pela primeira vez",
            },
            "roulette_first_loss": {
                "name": "aw dang it...",
                "emoji": "💥",
                "description": "{mention} perdeu pela primeira vez",
            },
            "roulette_first_jackpot": {
                "name": "I won... I actually won!",
                "emoji": "🍀",
                "description": "{mention} jackpot!!",
            },
            "roulette_double_jackpot": {
                "name": "I CAN'T STOP WINNING",
                "emoji": "🔥",
                "description": "{mention} conseguiu dois jackpots seguidos",
            },
            "target_bullseye": {
                "name": "Na mosca",
                "emoji": "🎯",
                "description": "{mention} acertou exatamente o centro do alvo",
            },
        }

    def _achievement_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._achievement_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._achievement_locks[key] = lock
        return lock

    def _normalize_game_achievements(self, raw: object) -> dict:
        source = raw if isinstance(raw, dict) else {}
        unlocked_source = source.get("unlocked") if isinstance(source.get("unlocked"), dict) else {}
        catalog = self._achievement_catalog()
        catalog_order = {key: index for index, key in enumerate(catalog)}
        parsed: list[tuple[str, float, int]] = []

        for key, value in unlocked_source.items():
            key_text = str(key or "")
            if key_text not in catalog:
                continue
            value_map = value if isinstance(value, dict) else {}
            try:
                unlocked_at = max(0.0, float(value_map.get("unlocked_at", 0.0) or 0.0))
            except Exception:
                unlocked_at = 0.0
            try:
                ordinal = max(0, int(value_map.get("ordinal", 0) or 0))
            except Exception:
                ordinal = 0
            parsed.append((key_text, unlocked_at, ordinal))

        ordinals = [ordinal for _key, _unlocked_at, ordinal in parsed]
        has_valid_ordinals = bool(parsed) and all(ordinal > 0 for ordinal in ordinals) and len(set(ordinals)) == len(ordinals)
        if has_valid_ordinals:
            parsed.sort(key=lambda item: (item[2], item[1], catalog_order[item[0]]))
        else:
            parsed.sort(key=lambda item: (item[1], catalog_order[item[0]]))

        unlocked: dict[str, dict[str, float | int]] = {}
        for ordinal, (key, unlocked_at, _stored_ordinal) in enumerate(parsed, start=1):
            unlocked[key] = {
                "unlocked_at": unlocked_at,
                "ordinal": ordinal,
            }

        try:
            jackpot_streak = max(0, int(source.get("roulette_jackpot_streak", 0) or 0))
        except Exception:
            jackpot_streak = 0
        return {
            "schema_version": 2,
            "unlocked": unlocked,
            "roulette_jackpot_streak": min(2, jackpot_streak),
        }

    def _get_unlocked_achievement_keys(self, guild_id: int, user_id: int) -> list[str]:
        doc = self.db.user_cache.get((int(guild_id), int(user_id)), {})
        state = self._normalize_game_achievements(doc.get("game_achievements"))
        unlocked = state["unlocked"]
        return sorted(
            unlocked,
            key=lambda key: (
                int(unlocked[key].get("ordinal", 0) or 0),
                float(unlocked[key].get("unlocked_at", 0.0) or 0.0),
                key,
            ),
        )

    def _get_unlocked_achievements(self, guild_id: int, user_id: int) -> list[str]:
        catalog = self._achievement_catalog()
        return [
            f"{catalog[key]['emoji']} {catalog[key]['name']}"
            for key in self._get_unlocked_achievement_keys(guild_id, user_id)
            if key in catalog
        ]

    def _achievement_progress_for_key(self, guild_id: int, user_id: int, achievement_key: str) -> tuple[int, int]:
        catalog = self._achievement_catalog()
        total = len(catalog)
        key = str(achievement_key or "").strip()
        if key not in catalog:
            return 0, total
        doc = self.db.user_cache.get((int(guild_id), int(user_id)), {})
        state = self._normalize_game_achievements(doc.get("game_achievements"))
        entry = state["unlocked"].get(key)
        if not isinstance(entry, dict):
            return 0, total
        try:
            count = int(entry.get("ordinal", 0) or 0)
        except Exception:
            count = 0
        return max(0, min(total, count)), total

    def _next_achievement_ordinal(self, unlocked: dict) -> int:
        current = 0
        for value in unlocked.values():
            if not isinstance(value, dict):
                continue
            try:
                current = max(current, int(value.get("ordinal", 0) or 0))
            except Exception:
                continue
        return current + 1

    async def _unlock_achievement(self, guild_id: int, user_id: int, achievement_key: str) -> bool:
        key = str(achievement_key or "").strip()
        if key not in self._achievement_catalog():
            return False
        async with self._achievement_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            state = self._normalize_game_achievements(doc.get("game_achievements"))
            if key in state["unlocked"]:
                return False
            state["unlocked"][key] = {
                "unlocked_at": float(time.time()),
                "ordinal": self._next_achievement_ordinal(state["unlocked"]),
            }
            doc["game_achievements"] = state
            await self.db._save_user_doc(guild_id, user_id, doc)
            return True

    async def _record_roulette_achievement_result(
        self,
        guild_id: int,
        user_id: int,
        *,
        jackpot: bool,
        lost: bool,
    ) -> list[str]:
        unlocked_now: list[str] = []
        async with self._achievement_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            state = self._normalize_game_achievements(doc.get("game_achievements"))
            unlocked = state["unlocked"]
            now = float(time.time())

            def unlock(key: str) -> None:
                if key in unlocked:
                    return
                unlocked[key] = {
                    "unlocked_at": now,
                    "ordinal": self._next_achievement_ordinal(unlocked),
                }
                unlocked_now.append(key)

            if jackpot:
                state["roulette_jackpot_streak"] = min(2, int(state["roulette_jackpot_streak"]) + 1)
                unlock("roulette_first_jackpot")
                if state["roulette_jackpot_streak"] >= 2:
                    unlock("roulette_double_jackpot")
            else:
                state["roulette_jackpot_streak"] = 0
                if lost:
                    unlock("roulette_first_loss")

            doc["game_achievements"] = state
            await self.db._save_user_doc(guild_id, user_id, doc)
        return unlocked_now

    def _make_achievement_view(
        self,
        achievement_key: str,
        mention: str,
        *,
        unlocked_count: int,
        total_count: int,
        thumbnail_url: str | None = None,
    ) -> discord.ui.LayoutView | None:
        item = self._achievement_catalog().get(str(achievement_key or ""))
        if item is None:
            return None
        total = max(1, int(total_count or 0))
        count = max(1, min(total, int(unlocked_count or 0)))
        description = str(item["description"]).format(mention=str(mention or "Alguém"))
        content = (
            f"### 🏆 Conquista desbloqueada ({count}/{total})\n\n"
            f"{item['emoji']} **{item['name']}**\n"
            f"-# {description}"
        )
        body = discord.ui.TextDisplay(content)
        if thumbnail_url:
            body = discord.ui.Section(
                body,
                accessory=discord.ui.Thumbnail(
                    str(thumbnail_url),
                    description="Conquista desbloqueada",
                ),
            )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            body,
            accent_color=discord.Color.gold(),
        ))
        return view

    async def _send_achievement_notice(
        self,
        channel,
        guild_id: int,
        user_id: int,
        achievement_key: str,
    ) -> bool:
        if channel is None or not hasattr(channel, "send"):
            guild = self.bot.get_guild(int(guild_id)) if getattr(self, "bot", None) is not None else None
            channel = self._get_gincana_channel(guild) if guild is not None else None
        if channel is None or not hasattr(channel, "send"):
            return False
        unlocked_count, total_count = self._achievement_progress_for_key(guild_id, user_id, achievement_key)
        if unlocked_count <= 0:
            return False
        mention = f"<@{int(user_id)}>"
        thumbnail_path = self._ACHIEVEMENT_THUMBNAIL_PATH
        use_thumbnail = thumbnail_path.is_file()
        attachment_url = (
            f"attachment://{self._ACHIEVEMENT_THUMBNAIL_FILENAME}"
            if use_thumbnail
            else None
        )
        view = self._make_achievement_view(
            achievement_key,
            mention,
            unlocked_count=unlocked_count,
            total_count=total_count,
            thumbnail_url=attachment_url,
        )
        if view is None:
            return False

        if use_thumbnail:
            image_file = None
            try:
                image_file = discord.File(
                    str(thumbnail_path),
                    filename=self._ACHIEVEMENT_THUMBNAIL_FILENAME,
                )
                await channel.send(
                    view=view,
                    file=image_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except Exception:
                pass
            finally:
                if image_file is not None:
                    try:
                        image_file.close()
                    except Exception:
                        pass

        fallback_view = self._make_achievement_view(
            achievement_key,
            mention,
            unlocked_count=unlocked_count,
            total_count=total_count,
        )
        if fallback_view is None:
            return False
        try:
            await channel.send(
                view=fallback_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except Exception:
            return False

    async def _unlock_and_send_achievement(
        self,
        channel,
        guild_id: int,
        user_id: int,
        achievement_key: str,
    ) -> bool:
        unlocked = await self._unlock_achievement(guild_id, user_id, achievement_key)
        if unlocked:
            await self._send_achievement_notice(channel, guild_id, user_id, achievement_key)
        return unlocked

    async def _unlock_first_game_for_users(self, guild_id: int, user_ids) -> list[int]:
        unlocked_user_ids: list[int] = []
        seen: set[int] = set()
        for raw_user_id in user_ids:
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or user_id in seen:
                continue
            seen.add(user_id)
            if await self._unlock_achievement(guild_id, user_id, "first_game"):
                unlocked_user_ids.append(user_id)
        return unlocked_user_ids

    async def _send_first_game_notices(self, channel, guild_id: int, user_ids) -> None:
        for user_id in user_ids:
            await self._send_achievement_notice(channel, guild_id, int(user_id), "first_game")

    async def _grant_weekly_points(self, guild_id: int, user_id: int, amount: int):
        if amount > 0:
            await self.db.add_user_weekly_points(guild_id, user_id, int(amount))

    async def _record_game_played(self, guild_id: int, user_id: int, *, weekly_points: int = 0):
        await self.db.add_user_game_stat(guild_id, user_id, "games_played", 1)
        await self._mark_chip_activity(guild_id, user_id)
        if weekly_points > 0:
            await self._grant_weekly_points(guild_id, user_id, weekly_points)

    def _daily_bonus_text(self, guild_id: int, user_id: int) -> str:
        status = self.db.get_user_daily_status(guild_id, user_id)
        streak = max(0, int(status.get("streak", 0) or 0))
        streak_text = self._daily_streak_label(streak)
        if status.get("available"):
            transition = self._daily_streak_transition(status)
            if transition == "continued":
                return f"Disponível agora • Ofensiva atual: **{streak_text}**"
            if transition == "restarted":
                return "Disponível agora • O próximo resgate inicia uma nova ofensiva"
            return "Disponível agora • Comece sua ofensiva diária"
        return f"Resgatado hoje • Ofensiva: **{streak_text}**"

    def _best_game_summary(self, stats: dict) -> str | None:
        roleta_wins = int(stats.get('roleta_jackpots', 0) or 0) + int(stats.get('cartas_jackpots', 0) or 0)
        candidates = [
            ((int(stats.get('truco_wins', 0) or 0), -int(stats.get('truco_losses', 0) or 0)), f"**Truco** — **{int(stats.get('truco_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('corrida_wins', 0) or 0), -int(stats.get('corrida_losses', 0) or 0)), f"**Corrida** — **{int(stats.get('corrida_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('alvo_wins', 0) or 0), -max(0, int(stats.get('alvo_games', 0) or 0) - int(stats.get('alvo_wins', 0) or 0))), f"**Alvo** — **{int(stats.get('alvo_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('poker_wins', 0) or 0), -int(stats.get('poker_losses', 0) or 0)), f"**Poker** — **{int(stats.get('poker_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('buckshot_survivals', 0) or 0), -int(stats.get('buckshot_eliminations', 0) or 0)), f"**Buckshot** — **{int(stats.get('buckshot_survivals', 0) or 0)}** vitórias"),
            ((roleta_wins, 0), f"**Roleta** — **{roleta_wins}** vitórias"),
        ]
        best_score, best_text = max(candidates, key=lambda item: item[0])
        if best_score[0] <= 0:
            return None
        return best_text

    def _build_chip_game_stat_lines(self, stats: dict) -> list[str]:
        lines: list[str] = []

        buckshot_total = int(stats.get('buckshot_survivals', 0) or 0) + int(stats.get('buckshot_eliminations', 0) or 0)
        buckshot_deaths = int(stats.get('buckshot_eliminations', 0) or 0)
        if buckshot_total > 0:
            line = f"<:propergun:1485855162198396959> **Buckshots**: **{buckshot_total}**"
            if buckshot_deaths > 0:
                line += f" (Morreu: **{buckshot_deaths}×**)"
            lines.append(line)

        truco_wins = int(stats.get('truco_wins', 0) or 0)
        truco_losses = int(stats.get('truco_losses', 0) or 0)
        truco_games = int(stats.get('truco_games', 0) or 0)
        if truco_games <= 0:
            truco_games = truco_wins + truco_losses
        if truco_games > 0:
            line = f"🃏 **Jogos de truco**: **{truco_games}**"
            parts: list[str] = []
            if truco_wins > 0:
                parts.append(f"Vitórias: **{truco_wins}**")
            if truco_losses > 0:
                parts.append(f"Derrotas: **{truco_losses}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        roleta_spins = int(stats.get('roleta_spins', 0) or 0) + int(stats.get('carta_spins', 0) or 0)
        roleta_jackpots = int(stats.get('roleta_jackpots', 0) or 0) + int(stats.get('cartas_jackpots', 0) or 0)
        if roleta_spins <= 0 and roleta_jackpots > 0:
            roleta_spins = roleta_jackpots
        if roleta_spins > 0 or roleta_jackpots > 0:
            parts = []
            if roleta_spins > 0:
                parts.append(f"🎰 **Giros**: **{roleta_spins}**")
            if roleta_jackpots > 0:
                parts.append(f"Jackpots: **{roleta_jackpots}**")
            if parts:
                lines.append(" • ".join(parts))

        corrida_wins = int(stats.get('corrida_wins', 0) or 0)
        corrida_losses = int(stats.get('corrida_losses', 0) or 0)
        corrida_games = corrida_wins + corrida_losses
        corrida_podiums = int(stats.get('corrida_podiums', 0) or 0)
        if corrida_games > 0 or corrida_podiums > 0:
            line = f"🏇 **Corridas**: **{corrida_games if corrida_games > 0 else corrida_podiums}**"
            parts = []
            if corrida_wins > 0:
                parts.append(f"Vitórias: **{corrida_wins}**")
            if corrida_podiums > 0:
                parts.append(f"Pódios: **{corrida_podiums}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        alvo_games = int(stats.get('alvo_games', 0) or 0)
        alvo_wins = int(stats.get('alvo_wins', 0) or 0)
        alvo_bullseyes = int(stats.get('alvo_bullseyes', 0) or 0)
        if alvo_games > 0 or alvo_wins > 0 or alvo_bullseyes > 0:
            left_total = alvo_games if alvo_games > 0 else alvo_wins
            line = f"🎯 **Alvos**: **{left_total}**"
            parts = []
            if alvo_wins > 0:
                parts.append(f"Vitórias: **{alvo_wins}**")
            if alvo_bullseyes > 0:
                parts.append(f"Bullseyes: **{alvo_bullseyes}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        poker_games = int(stats.get('poker_rounds', 0) or 0)
        poker_wins = int(stats.get('poker_wins', 0) or 0)
        poker_losses = int(stats.get('poker_losses', 0) or 0)
        if poker_games > 0 or poker_wins > 0 or poker_losses > 0:
            left_total = poker_games if poker_games > 0 else (poker_wins + poker_losses)
            line = f"🂡 **Pokers**: **{left_total}**"
            parts = []
            if poker_wins > 0:
                parts.append(f"Vitórias: **{poker_wins}**")
            if poker_losses > 0:
                parts.append(f"Derrotas: **{poker_losses}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        return lines

    def _chip_recharge_state(self, guild_id: int, user_id: int) -> dict:
        import time

        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        doc = getattr(self.db, "user_cache", {}).get((guild_id, user_id), {}) or {}
        initialized = bool(doc.get("chip_recharge_manual_initialized", False))
        last_reset = self.db.get_user_chip_reset_at(guild_id, user_id)
        now = time.time()
        if not initialized or last_reset <= 0:
            remaining = 0.0
        else:
            remaining = max(0.0, (float(last_reset) + float(CHIPS_RESET_SECONDS)) - now)
        below_threshold = (chips + bonus) < CHIPS_RECHARGE_THRESHOLD
        available = below_threshold and remaining <= 0.0
        return {
            "chips": int(chips),
            "bonus": int(bonus),
            "remaining": float(remaining),
            "below_threshold": bool(below_threshold),
            "available": bool(available),
            "initialized": bool(initialized),
        }

    def _chip_recharge_text(self, guild_id: int, user_id: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        total = chips + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Use **recarga** quando seu saldo total ficar abaixo de **{CHIPS_RECHARGE_THRESHOLD}**\n"
                f"Ela entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus e tem cooldown de **{CHIPS_RESET_HOURS} horas**"
            )
        if remaining > 0:
            return (
                f"Disponível em **{self._format_chip_reset_remaining(remaining)}** com o trigger **recarga**\n"
                f"Seu saldo total já está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** e ela vai entregar {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus"
            )
        return (
            f"Disponível agora em **recarga**\nSeu saldo total está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** "
            f"e ela entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus"
        )

    def _chip_recharge_compact_text(self, guild_id: int, user_id: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        remaining = float(state["remaining"])
        total = int(state["chips"]) + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return f"Use **_recarga** quando ficar abaixo de **{CHIPS_RECHARGE_THRESHOLD}** • +{CHIPS_DEFAULT} bônus"
        if remaining > 0:
            return f"Volta em **{self._format_chip_reset_remaining(remaining)}** • +{CHIPS_DEFAULT} bônus"
        return f"Use **_recarga** agora • +{CHIPS_DEFAULT} bônus"

    async def _try_use_chip_recharge(self, guild_id: int, user_id: int) -> tuple[bool, int, str]:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        total = chips + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return False, chips, (
                f"Use **_recarga** apenas abaixo de **{CHIPS_RECHARGE_THRESHOLD}**"
            )
        if remaining > 0:
            return False, chips, (
                f"Sua recarga volta em **{self._format_chip_reset_remaining(remaining)}**"
            )
        await self._change_user_bonus_chips(guild_id, user_id, int(CHIPS_DEFAULT), mark_activity=True, reason="Recarga manual")
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["last_chip_reset_at"] = float(__import__("time").time())
        doc["chip_recharge_manual_initialized"] = True
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True, self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL), (
            f"Você recebeu **{CHIPS_DEFAULT}** {self._CHIP_BONUS_EMOJI}"
        )


    def _make_chip_recharge_view(self, guild_id: int, user_id: int, used: bool, new_balance: int, note: str) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        if used:
            lines = [
                "# 🔋 Recarga usada",
                note,
                f"Novo saldo: {self._format_primary_chip_balance(guild_id, user_id)}",
            ]
            color = discord.Color.dark_green()
        else:
            lines = ["# 🔋 Recarga indisponível", note]
            state = self._chip_recharge_state(guild_id, user_id)
            total = int(state["chips"]) + int(state.get("bonus", 0) or 0)
            if total >= CHIPS_RECHARGE_THRESHOLD:
                lines.append(f"Saldo atual: {self._format_compact_chip_balance(guild_id, user_id)}")
            else:
                remaining = float(state["remaining"])
                lines.append(f"Saldo atual: {self._format_compact_chip_balance(guild_id, user_id)}")
                if remaining > 0:
                    lines.append(f"Volta em: **{self._format_chip_reset_remaining(remaining)}**")
            color = discord.Color.red()
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_color=color,
        ))
        return view

    def _negative_cost_projection(self, guild_id: int, user_id: int, amount: int) -> dict:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        projected_chips, projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, amount)
        return {
            "chips": int(chips),
            "bonus": int(bonus),
            "projected_chips": int(projected_chips),
            "projected_bonus": int(projected_bonus),
        }

    def _negative_transition_note(self, guild_id: int, user_id: int, amount: int) -> str | None:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        if projected_chips >= 0:
            return None
        first_negative = chips >= 0 and projected_chips < 0
        debt_increases = chips < 0 and projected_chips < chips
        if first_negative:
            return (
                f"Se continuar, você vai ser negativado\n"
                f"Você vai ficar com **{projected_chips}** {self._CHIP_LOSS_EMOJI}"
            )
        if debt_increases:
            if bonus <= 0:
                return (
                    f"Você já está negativado e não tem fichas bônus\n"
                    f"Se continuar, sua dívida vai para **{projected_chips}** {self._CHIP_LOSS_EMOJI}"
                )
            return (
                f"As fichas bônus não cobrem toda essa aposta\n"
                f"Sua dívida vai para **{projected_chips}** {self._CHIP_LOSS_EMOJI}"
            )
        return None

    def _needs_negative_confirmation(self, guild_id: int, user_id: int, amount: int) -> bool:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        projected_bonus = int(state["projected_bonus"])
        if projected_bonus > 0:
            return False
        first_negative = chips >= 0 and projected_chips < 0
        debt_increases = chips < 0 and projected_chips < chips
        return bonus <= 0 and (first_negative or debt_increases)

    async def _confirm_negative_via_message(self, channel: discord.abc.Messageable, *, user_id: int, title: str, note: str) -> bool:
        view = _NegativeDebtConfirmView(owner_id=user_id)
        embed = self._make_embed(title, note, ok=False)
        sent = None
        try:
            sent = await channel.send(embed=embed, view=view)
            view.message = sent
            await view.wait()
            return bool(view.confirmed)
        finally:
            if sent is not None:
                try:
                    await sent.delete()
                except Exception:
                    pass

    async def _confirm_negative_ephemeral(self, interaction: discord.Interaction, guild_id: int, user_id: int, amount: int, *, title: str = "⚠️ Confirmar entrada") -> bool:
        note = self._negative_transition_note(guild_id, user_id, amount)
        if not note:
            return True
        view = _NegativeDebtConfirmView(owner_id=user_id)
        embed = self._make_embed(title, note, ok=False)
        sent = None
        try:
            if interaction.response.is_done():
                sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
            else:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                try:
                    sent = await interaction.original_response()
                except Exception:
                    sent = None
            view.message = sent
            await view.wait()
            return bool(view.confirmed)
        except Exception:
            channel = getattr(interaction, "channel", None)
            if channel is None:
                return False
            return await self._confirm_negative_via_message(channel, user_id=user_id, title=title, note=note)

    async def _confirm_negative_from_message(self, message: discord.Message, guild_id: int, user_id: int, amount: int, *, title: str = "⚠️ Confirmar entrada") -> bool:
        note = self._negative_transition_note(guild_id, user_id, amount)
        if not note:
            return True
        return await self._confirm_negative_via_message(message.channel, user_id=user_id, title=title, note=note)

    def _insufficient_chips_text(self, guild_id: int, user_id: int, amount: int) -> str:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        note = self._negative_transition_note(guild_id, user_id, amount)
        if projected_chips >= -self._MAX_CHIP_DEBT and note:
            return note
        state = self._chip_recharge_state(guild_id, user_id)
        remaining = float(state["remaining"])
        total = chips + bonus
        if state["available"]:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}\n"
                f"Como ele está abaixo de **{CHIPS_RECHARGE_THRESHOLD}**, você já pode usar **recarga** para receber {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus"
            )
        if total < CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}\n"
                f"Sua **recarga** volta em **{self._format_chip_reset_remaining(remaining)}** e entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus"
            )
        return (
            f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}"
        )


    def _format_primary_chip_balance(self, guild_id: int, user_id: int) -> str:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        if chips < 0:
            primary = f"**{chips}** {self._CHIP_LOSS_EMOJI}"
        else:
            primary = f"**{chips}** {self._CHIP_EMOJI}"
        if bonus > 0:
            primary += f" • **{bonus}** {self._CHIP_BONUS_EMOJI}"
        return primary

    def _format_compact_chip_balance(self, guild_id: int, user_id: int) -> str:
        return self._format_primary_chip_balance(guild_id, user_id)

    def _chip_spend_breakdown_text(self, guild_id: int, user_id: int, amount: int) -> str:
        spend = max(0, int(amount))
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        use_bonus = min(bonus, spend)
        use_normal = spend - use_bonus
        if use_bonus > 0 and use_normal > 0:
            return f"Você entrou usando {self._bonus_chip_amount(use_bonus)} e {self._chip_amount(use_normal)}"
        if use_bonus > 0:
            return f"Você entrou usando {self._bonus_chip_amount(use_bonus)}"
        return f"Você entrou usando {self._chip_amount(use_normal)}"

    def _entry_consume_text(self, guild_id: int, user_id: int, amount: int) -> str:
        spend_text = self._chip_spend_breakdown_text(guild_id, user_id, amount)
        note = self._negative_transition_note(guild_id, user_id, amount)
        if note:
            return f"{spend_text}\n{note}"
        return spend_text

    def _project_chip_state_after_cost(self, guild_id: int, user_id: int, amount: int) -> tuple[int,int]:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        spend = max(0, int(amount))
        use_bonus = min(bonus, spend)
        remaining = spend - use_bonus
        return chips - remaining, bonus - use_bonus

    def _user_has_played_any_game(self, guild_id: int, user_id: int) -> bool:
        stats = self.db.get_user_game_stats(guild_id, user_id)
        try:
            return int(stats.get("games_played", 0) or 0) > 0
        except Exception:
            return False

    def _format_percent_text(self, value: float) -> str:
        try:
            number = float(value) * 100.0 if float(value) <= 1.0 else float(value)
        except Exception:
            number = 0.0
        text = f"{number:.2f}".rstrip("0").rstrip(".")
        return text.replace(".", ",") + "%"

    def _race_catalog(self) -> dict[str, dict[str, object]]:
        return {
            "preto": {
                "name": "Preto",
                "emoji": "🥷🏿",
                "effects": [
                    {"key": "mao_negra", "emoji": "🖐🏿", "title": "Mão Negra", "desc": "Você pode roubar **2 vezes** a cada **4h**"},
                    {"key": "labia", "emoji": "🗣️", "title": "Lábia", "desc": "Você pode pedir esmola **2 vezes** a cada **3h**"},
                    {"key": "sangue_frio", "emoji": "🧊", "title": "Sangue Frio", "desc": f"Quando um roubo dá errado, você perde apenas **5** {self._CHIP_LOSS_EMOJI}"},
                    {"key": "mao_grande", "emoji": "💰", "title": "Cariocagem", "desc": f"Quando o roubo dá certo, você pode levar até **40** {self._CHIP_EMOJI}"},
                ],
            },
            "apostador": {
                "name": "Apostador",
                "emoji": "🎰",
                "effects": [
                    {"key": "jackpot", "emoji": "🎰", "title": "Jackpot 999", "desc": f"Na Roleta, você tem **{self._format_percent_text(0.15)} de chance** de acertar **999** e ganhar **100** {self._CHIP_GAIN_EMOJI}"},
                    {"key": "all_in", "emoji": "🎲", "title": "All-in 777", "desc": f"Há **{self._format_percent_text(0.05)} de chance** de acertar **777** e ganhar **200** {self._CHIP_GAIN_EMOJI}"},
                    {"key": "666", "emoji": "😈", "title": "Marca da Besta", "desc": f"Quando o jackpot não vem, há **{self._format_percent_text(0.25)} de chance** de cair **666** e ganhar **{ROLETA_APOSTADOR_COST}** {self._CHIP_GAIN_EMOJI}"},
                    {"key": "mesa_alta", "emoji": "💸", "title": "Mesa Alta", "desc": f"Você joga mais alto: cada giro da Roleta custa **25** {self._CHIP_LOSS_EMOJI}"},
                ],
            },
            "sortudo": {
                "name": "Sortudo",
                "emoji": "🍀",
                "effects": [
                    {"key": "midas", "emoji": "✨", "title": "Midas", "desc": f"Ao abrir um Buckshot ou Truco, a partida tem **{self._format_percent_text(RACE_SPECIAL_SORTUDO_CHANCE)} de chance** de começar dourada"},
                    {"key": "premio_extra", "emoji": "🎁", "title": "Prêmio Extra", "desc": f"Seu Daily rende **+5** {self._CHIP_BONUS_EMOJI}\nQuando a ofensiva aumenta o prêmio, você recebe **mais 5** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "bencao", "emoji": "🙏", "title": "Bênção", "desc": "A cada **7h**, você recebe uma jogada grátis\nPode guardar até **2** e usar na Roleta ou em Cartas"},
                    {"key": "wind_boost", "emoji": "🍃", "title": "Wind Boost", "desc": "Na Corrida, cada botão acertado tem **14% de chance** de gerar um impulso, em vez de **9%**"},
                ],
            },
            "coringa": {
                "name": "Coringa",
                "emoji": "🃏",
                "effects": [
                    {"key": "as", "emoji": "🂡", "title": "Ás", "desc": f"Ao perder um jogo com lobby, você tem **{self._format_percent_text(0.35)} de chance** de recuperar metade da entrada"},
                    {"key": "trapaceiro", "emoji": "🎭", "title": "Trapaceiro", "desc": f"Quando um roubo dá errado, há **{self._format_percent_text(0.25)} de chance** de você não perder nenhuma ficha"},
                    {"key": "redencao", "emoji": "🃏", "title": "Redenção", "desc": f"Ao perder na Roleta ou em Cartas, você tem **{self._format_percent_text(0.5)} de chance** de recuperar metade do custo"},
                ],
            },
            "vampiro": {
                "name": "Vampiro",
                "emoji": "🧛",
                "effects": [
                    {"key": "hunt", "emoji": "🩸", "title": "Hunt", "desc": "Durante a noite, suas vitórias contra outros jogadores rendem Sangue\nA primeira vale **2 cargas**; as próximas valem **1**\nVocê pode guardar até **3**"},
                    {"key": "harvest", "emoji": "🍷", "title": "Harvest", "desc": f"Junte **3 cargas de Sangue** para receber **35** {self._CHIP_BONUS_EMOJI}\nPode acontecer até **2 vezes por noite**"},
                ],
            },
            "fenix": {
                "name": "Fênix",
                "emoji": "🐦‍🔥",
                "effects": [
                    {"key": "sunrise", "emoji": "🔥", "title": "Sunrise", "desc": "Durante o dia, suas duas primeiras derrotas pagas geram Brasas\nVocê pode guardar até **2**"},
                    {"key": "rebirth", "emoji": "❤️‍🔥", "title": "Rebirth", "desc": f"Vença com Brasas guardadas para receber fichas bônus: **1 Brasa** rende **30** {self._CHIP_BONUS_EMOJI}; **2 Brasas** rendem **40** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "second_dawn", "emoji": "🐦‍🔥", "title": "Second Dawn", "desc": f"Uma vez por dia, se uma derrota deixar seu saldo abaixo de **30**, você recebe **30** {self._CHIP_BONUS_EMOJI}"},
                ],
            },
            "glitch": {
                "name": "Glitch",
                "emoji": "👁️⃤",
                "effects": [
                    {"key": "desync", "emoji": "<a:eyeglitch:1531116300645175436>", "title": "Desync", "desc": "A cada partida paga, o sistema fica mais instável\nNa terceira, o estado **ERROR** é ativado"},
                    {"key": "overflow", "emoji": "💠", "title": "Overflow", "desc": f"Vença durante o **ERROR** para receber entre **30 e 45** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "rollback", "emoji": "👁️⃤", "title": "Rollback", "desc": "Perca durante o **ERROR** para recuperar **75% da entrada**, com limite de **20 fichas**"},
                    {"key": "memory_leak", "emoji": "🔧", "title": "Memory Leak", "desc": "Às vezes, o próximo ciclo começa com **1 fragmento** preservado"},
                ],
            },
        }

    def _get_race_effects(self, race_key: str) -> list[dict[str, str]]:
        info = self._get_race_info_by_key(race_key) or {}
        effects = []
        for effect in list(info.get("effects") or []):
            if isinstance(effect, dict):
                key = str(effect.get("key") or "").strip().lower()
                emoji = str(effect.get("emoji") or "").strip()
                title = str(effect.get("title") or "").strip()
                desc = str(effect.get("desc") or "").strip()
                if key and title and desc:
                    effects.append({"key": key, "emoji": emoji, "title": title, "desc": desc})
        return effects

    def _get_race_effect_title(self, race_key: str, effect_key: str) -> str:
        target = str(effect_key or "").strip().lower()
        for effect in self._get_race_effects(race_key):
            if effect.get("key") == target:
                return str(effect.get("title") or "")
        return ""

    def _race_effect_emoji(
        self,
        guild_id: int,
        user_id: int,
        effect_key: str,
        *,
        emoji_count: int | None = None,
    ) -> str:
        race_key = self._get_user_race_key(guild_id, user_id)
        effect = str(effect_key or "").strip().lower()
        if effect == "hunt":
            count = min(3, max(1, int(emoji_count or 1)))
            return "🩸" * count
        if effect == "sunrise":
            count = min(2, max(1, int(emoji_count or 1)))
            return "🔥" * count
        overrides = {
            "harvest": "🍷",
            "rebirth": "❤️‍🔥",
            "second_dawn": "🐦‍🔥",
            "desync": "<a:eyeglitch:1531116300645175436>",
            "overflow": "💠",
            "memory_leak": "🔧",
            "rollback": "👁️⃤",
        }
        if effect in overrides:
            return overrides[effect]
        for race_effect in self._get_race_effects(race_key):
            if race_effect.get("key") == effect:
                effect_emoji = str(race_effect.get("emoji") or "").strip()
                if effect_emoji:
                    return effect_emoji
        info = self._get_race_info_by_key(race_key) or {}
        return str(info.get("emoji") or self._EFFECT_EMOJI).strip() or self._EFFECT_EMOJI

    def _race_effect_message(
        self,
        guild_id: int,
        user_id: int,
        effect_key: str,
        detail: str | None = None,
        *,
        emoji_count: int | None = None,
    ) -> str:
        effect_key = str(effect_key or "").strip().lower()
        title = self._get_race_effect_title(self._get_user_race_key(guild_id, user_id), effect_key)
        if not title:
            return ""
        detail_map = {
            "labia": "2º pedido de esmola do período",
            "bencao": "uma carga pagou esta jogada",
            "mao_negra": "2º roubo do período",
            "mao_grande": "roubo acima do limite comum",
            "sangue_frio": "a perda do roubo ficou em 5 fichas",
            "trapaceiro": "você escapou da penalidade do roubo",
            "jackpot": f"você acertou **999** e recebeu **100** {self._CHIP_GAIN_EMOJI}",
            "all_in": f"você acertou **777** e recebeu **200** {self._CHIP_GAIN_EMOJI}",
            "666": f"você acertou **666** e recebeu **{ROLETA_APOSTADOR_COST}** {self._CHIP_GAIN_EMOJI}",
            "midas": "a versão dourada foi ativada",
            "premio_extra": f"seu Daily rendeu fichas bônus extras",
            "as": "você recuperou metade da entrada",
            "redencao": "você recuperou metade do custo",
            "hunt": "Sangue acumulado",
            "harvest": f"você recebeu **35** {self._CHIP_BONUS_EMOJI}",
            "sunrise": "Brasa armazenada",
            "rebirth": "as Brasas viraram fichas bônus",
            "second_dawn": f"você recebeu **30** {self._CHIP_BONUS_EMOJI}",
            "desync": "fragmento registrado",
            "overflow": "o ERROR premiou a vitória",
            "rollback": "parte da entrada foi restaurada",
            "memory_leak": "1 fragmento foi preservado",
        }
        suffix = str(detail or detail_map.get(effect_key, "")).strip()
        emoji = self._race_effect_emoji(
            guild_id,
            user_id,
            effect_key,
            emoji_count=emoji_count,
        )
        return f"{emoji} **{title}:** {suffix}" if suffix else f"{emoji} **{title}**"

    def _race_effect_marker(self, guild_id: int, user_id: int, effect_key: str) -> str:
        return self._race_effect_message(guild_id, user_id, effect_key)


    def _format_race_identity(self, guild_id: int, user_id: int) -> str:
        info = self._get_user_race_info(guild_id, user_id) or {}
        if not info:
            return ""
        emoji = str(info.get("emoji") or "").strip()
        name = str(info.get("name") or "").strip()
        active = self._is_user_race_active(guild_id, user_id)
        label = f"{emoji}{name}" if emoji else name
        if label and not active:
            label += " (desativada)"
        return label

    def _remember_race_panel_message(self, guild_id: int, user_id: int, message: discord.Message | None):
        if message is None:
            return
        self._race_panel_messages[(int(guild_id), int(user_id))] = (int(message.channel.id), int(message.id))

    def _forget_race_panel_message(self, guild_id: int, user_id: int, *, message_id: int | None = None):
        key = (int(guild_id), int(user_id))
        current = self._race_panel_messages.get(key)
        if not current:
            return
        if message_id is None or int(current[1]) == int(message_id):
            self._race_panel_messages.pop(key, None)

    async def _delete_previous_race_panel_message(self, guild_id: int, user_id: int, channel: discord.abc.Messageable | None = None):
        key = (int(guild_id), int(user_id))
        stored = self._race_panel_messages.pop(key, None)
        if not stored:
            return
        channel_id, message_id = stored
        target_message = None
        try:
            if channel is not None and int(getattr(channel, "id", 0) or 0) == int(channel_id) and hasattr(channel, "fetch_message"):
                target_message = await channel.fetch_message(int(message_id))
            else:
                fetched_channel = self.bot.get_channel(int(channel_id))
                if fetched_channel is None:
                    try:
                        fetched_channel = await self.bot.fetch_channel(int(channel_id))
                    except Exception:
                        fetched_channel = None
                if fetched_channel is not None and hasattr(fetched_channel, "fetch_message"):
                    target_message = await fetched_channel.fetch_message(int(message_id))
            if target_message is not None:
                await target_message.delete()
        except Exception:
            pass

    def _get_user_race_key(self, guild_id: int, user_id: int) -> str:
        try:
            raw = str((self.db._get_user_doc(guild_id, user_id) or {}).get("race_key", "") or "").strip().lower()
        except Exception:
            raw = ""
        return raw if raw in self._race_catalog() else ""

    def _is_user_race_active(self, guild_id: int, user_id: int) -> bool:
        race_key = self._get_user_race_key(guild_id, user_id)
        if not race_key:
            return False
        try:
            return bool((self.db._get_user_doc(guild_id, user_id) or {}).get("race_active", True))
        except Exception:
            return True

    def _get_user_race_info(self, guild_id: int, user_id: int) -> dict[str, object] | None:
        key = self._get_user_race_key(guild_id, user_id)
        return self._race_catalog().get(key) if key else None

    def _get_race_info_by_key(self, race_key: str) -> dict[str, object] | None:
        return self._race_catalog().get(str(race_key or "").strip().lower())

    def _get_race_name(self, race_key: str) -> str:
        info = self._get_race_info_by_key(race_key)
        return str(info.get("name")) if info else "Sem raça"

    def _format_user_race(self, guild_id: int, user_id: int) -> str:
        return self._get_race_name(self._get_user_race_key(guild_id, user_id))

    async def _set_user_race_key(self, guild_id: int, user_id: int, race_key: str | None, *, reset_state: bool = False):
        doc = self.db._get_user_doc(guild_id, user_id)
        key = str(race_key or "").strip().lower()
        if key and key in self._race_catalog():
            doc["race_key"] = key
            doc["race_active"] = True
        else:
            doc.pop("race_key", None)
            doc.pop("race_active", None)
        if reset_state:
            self._race_private_notices.pop((int(guild_id), int(user_id)), None)
            for field in (
                "race_free_roleta_spins",
                "race_free_carta_spins",
                "race_sortudo_blessing_charges",
                "race_sortudo_blessing_started_at",
                "race_robbery_window_started_at",
                "race_robbery_uses",
                "race_mendigar_window_started_at",
                "race_mendigar_uses",
                "race_state",
            ):
                doc.pop(field, None)
            if key == "sortudo":
                doc["race_sortudo_blessing_charges"] = 1
                doc["race_sortudo_blessing_started_at"] = float(time.time())
        await self.db._save_user_doc(guild_id, user_id, doc)

    async def _set_user_race_active(self, guild_id: int, user_id: int, active: bool):
        doc = self.db._get_user_doc(guild_id, user_id)
        if not self._get_user_race_key(guild_id, user_id):
            doc.pop("race_active", None)
        else:
            doc["race_active"] = bool(active)
        await self.db._save_user_doc(guild_id, user_id, doc)

    async def _clear_user_race(self, guild_id: int, user_id: int):
        await self._set_user_race_key(guild_id, user_id, None, reset_state=True)

    async def _roll_user_race(self, guild_id: int, user_id: int, *, exclude_current: bool = False) -> str:
        choices = list(self._race_catalog().keys())
        current = self._get_user_race_key(guild_id, user_id)
        if exclude_current and current in choices and len(choices) > 1:
            choices.remove(current)
        chosen = random.choice(choices)
        await self._set_user_race_key(guild_id, user_id, chosen, reset_state=True)
        return chosen

    def _race_is(self, guild_id: int, user_id: int, race_key: str) -> bool:
        return self._get_user_race_key(guild_id, user_id) == str(race_key or "").strip().lower() and self._is_user_race_active(guild_id, user_id)

    def _race_progress_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._race_progress_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._race_progress_locks[key] = lock
        return lock

    @staticmethod
    def _clean_race_notices(notes) -> list[str]:
        if isinstance(notes, str):
            source = (notes,)
        else:
            source = notes or ()
        clean: list[str] = []
        seen: set[str] = set()
        for note in source:
            text = str(note or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            clean.append(text)
        return clean

    def _queue_private_race_notices(self, guild_id: int, user_id: int, notes) -> None:
        clean = self._clean_race_notices(notes)
        if not clean:
            return
        key = (int(guild_id), int(user_id))
        queued = self._race_private_notices.setdefault(key, [])
        for note in clean:
            if note not in queued:
                queued.append(note)
        if len(queued) > 12:
            del queued[:-12]

    def _append_public_race_notices(self, target: list[str], notes) -> None:
        for note in self._clean_race_notices(notes):
            if note not in target:
                target.append(note)

    @staticmethod
    def _identify_public_race_notice(user_id: int, note: str) -> str:
        text = str(note or "").strip()
        if not text:
            return ""
        mention = f"<@{int(user_id)}>"
        if mention in text:
            return text
        emoji, separator, detail = text.partition(" ")
        if separator and detail:
            return f"{emoji} {mention} — {detail}"
        return f"{mention} — {text}"

    def _take_private_race_notices(self, guild_id: int, user_id: int) -> list[str]:
        return list(self._race_private_notices.pop((int(guild_id), int(user_id)), []))

    def _race_lobby_status_line(self, guild_id: int, user_id: int) -> str:
        race_key = self._get_user_race_key(guild_id, user_id)
        if race_key not in {"vampiro", "fenix", "glitch"} or not self._is_user_race_active(guild_id, user_id):
            return ""
        doc = self.db._get_user_doc(guild_id, user_id)
        state = dict((doc.get("race_state") or {}).get(race_key) or {})
        period, period_key = self._race_period_info()
        if race_key == "vampiro":
            if period != "night":
                return "🩸 **Hunt:** indisponível durante o dia"
            if str(state.get("night_key") or "") != period_key:
                blood = 0
                harvests = 0
            else:
                blood = min(3, max(0, int(state.get("blood", 0) or 0)))
                harvests = min(2, max(0, int(state.get("harvests", 0) or 0)))
            return f"🩸 **Hunt:** {blood}/3 Sangues • Harvest {harvests}/2"
        if race_key == "fenix":
            if period != "day":
                return "🐦‍🔥 **Habilidades:** indisponíveis durante a noite"
            if str(state.get("day_key") or "") != period_key:
                embers = 0
                dawn_used = False
            else:
                embers = min(2, max(0, int(state.get("embers", 0) or 0)))
                dawn_used = bool(state.get("second_dawn_used", False))
            dawn_status = "usado" if dawn_used else "disponível"
            return f"🔥 **Sunrise:** {embers}/2 Brasas • Second Dawn {dawn_status}"
        fragments = min(2, max(0, int(state.get("fragments", 0) or 0)))
        if fragments >= 2:
            return "<a:eyeglitch:1531116300645175436> **Desync:** 2/3 fragmentos • ERROR no próximo resultado decisivo"
        return f"<a:eyeglitch:1531116300645175436> **Desync:** {fragments}/3 fragmentos"

    async def _send_race_lobby_feedback(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
        base_text: str,
    ) -> bool:
        pending = self._take_private_race_notices(guild_id, user_id)
        parts = [str(base_text or "").strip()]
        parts.extend(note for note in pending if note)
        try:
            status = self._race_lobby_status_line(guild_id, user_id)
        except Exception:
            status = ""
        if status:
            parts.append(status)
        text = "\n\n".join(part for part in parts if part)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
            return True
        except Exception:
            if getattr(interaction, "guild", None) is None:
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(text)
                    else:
                        await interaction.response.send_message(text)
                    return True
                except Exception:
                    pass
            self._queue_private_race_notices(guild_id, user_id, pending)
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            return False

    async def _deliver_or_queue_private_race_notices(
        self,
        interaction: discord.Interaction | None,
        guild_id: int,
        user_id: int,
        notes,
    ) -> bool:
        clean = self._clean_race_notices(notes)
        if not clean:
            return True
        if interaction is not None:
            try:
                text = "\n".join(clean)
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
                return True
            except Exception:
                if getattr(interaction, "guild", None) is None:
                    try:
                        text = "\n".join(clean)
                        if interaction.response.is_done():
                            await interaction.followup.send(text)
                        else:
                            await interaction.response.send_message(text)
                        return True
                    except Exception:
                        pass
        self._queue_private_race_notices(guild_id, user_id, clean)
        return False

    async def _route_lobby_race_notices(
        self,
        interaction: discord.Interaction | None,
        guild_id: int,
        user_id: int,
        owner_id: int,
        notes,
        public_notes: list[str],
    ) -> bool:
        clean = self._clean_race_notices(notes)
        if not clean:
            return True
        if int(owner_id or 0) > 0 and int(user_id) == int(owner_id):
            identified = [self._identify_public_race_notice(user_id, note) for note in clean]
            self._append_public_race_notices(public_notes, identified)
            return True
        return await self._deliver_or_queue_private_race_notices(
            interaction,
            guild_id,
            user_id,
            clean,
        )

    def _race_now(self) -> datetime:
        try:
            now = self.db._sao_paulo_now()
            if isinstance(now, datetime):
                return now
        except Exception:
            pass
        return datetime.now(ZoneInfo("America/Sao_Paulo"))

    def _race_period_info(self, now: datetime | None = None) -> tuple[str, str]:
        current = now or self._race_now()
        if current.hour >= 18 or current.hour < 6:
            night_date = (current - timedelta(days=1)).date() if current.hour < 6 else current.date()
            return "night", night_date.isoformat()
        return "day", current.date().isoformat()

    def _entry_spend_parts(self, guild_id: int, user_id: int, amount: int) -> dict[str, object]:
        spend = max(0, int(amount or 0))
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        bonus_used = min(bonus, spend)
        period, period_key = self._race_period_info()
        return {
            "bonus": bonus_used,
            "chips": spend - bonus_used,
            "_race_period": period,
            "_race_period_key": period_key,
        }

    def _normalize_entry_spend(self, entry_spend: dict | None) -> dict[str, int]:
        raw = entry_spend or {}
        return {
            "chips": max(0, int(raw.get("chips", 0) or 0)),
            "bonus": max(0, int(raw.get("bonus", 0) or 0)),
        }

    def _split_refund_by_entry(self, entry_spend: dict[str, int], amount: int) -> dict[str, int]:
        spend = self._normalize_entry_spend(entry_spend)
        total = spend["chips"] + spend["bonus"]
        target = min(max(0, int(amount or 0)), total)
        if target <= 0 or total <= 0:
            return {"chips": 0, "bonus": 0}
        bonus_share = int(round(target * spend["bonus"] / total))
        bonus_share = min(spend["bonus"], max(0, bonus_share))
        normal_share = target - bonus_share
        if normal_share > spend["chips"]:
            overflow = normal_share - spend["chips"]
            normal_share = spend["chips"]
            bonus_share = min(spend["bonus"], bonus_share + overflow)
        return {"chips": normal_share, "bonus": bonus_share}

    async def _apply_new_race_result(
        self,
        guild_id: int,
        user_id: int,
        *,
        won: bool | None,
        entry_spend: dict | None,
        payout: int = 0,
        opponent_ids=(),
        valid: bool = True,
        allow_hunt: bool = True,
        glitch_progress: bool = True,
    ) -> list[str]:
        """Aplica progressão das raças novas sem interferir na resolução do jogo."""
        try:
            race_key = self._get_user_race_key(guild_id, user_id)
            if race_key not in {"vampiro", "fenix", "glitch"} or not self._is_user_race_active(guild_id, user_id):
                return []
            if not valid:
                return []
            raw_entry = dict(entry_spend or {})
            spend = self._normalize_entry_spend(raw_entry)
            entry_total = spend["chips"] + spend["bonus"]
            now = self._race_now()
            current_period, current_period_key = self._race_period_info(now)
            stored_period = str(raw_entry.get("_race_period") or "").strip().lower()
            stored_period_key = str(raw_entry.get("_race_period_key") or "").strip()
            if stored_period in {"day", "night"} and stored_period_key:
                period, period_key = stored_period, stored_period_key
            else:
                period, period_key = current_period, current_period_key
            timestamp = float(now.timestamp())
            opponents = sorted({int(value) for value in (opponent_ids or ()) if int(value) > 0 and int(value) != int(user_id)})

            async with self._race_progress_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                state_root = dict(doc.get("race_state") or {})
                state = dict(state_root.get(race_key) or {})
                notes: list[str] = []
                normal_delta = 0
                bonus_delta = 0
                normal_reason = ""
                bonus_reason = ""

                if race_key == "vampiro":
                    if won is not True or period != "night" or not allow_hunt or not opponents:
                        return []
                    if str(state.get("night_key") or "") != period_key:
                        state = {"night_key": period_key, "blood": 0, "wins": 0, "harvests": 0, "opponents": {}}
                    harvests = max(0, int(state.get("harvests", 0) or 0))
                    if harvests >= 2:
                        return []
                    opponent_log = dict(state.get("opponents") or {})
                    eligible_opponents = [opponent_id for opponent_id in opponents if timestamp - float(opponent_log.get(str(opponent_id), 0) or 0.0) >= 4 * 60 * 60]
                    if not eligible_opponents:
                        return []
                    for opponent_id in eligible_opponents:
                        opponent_log[str(opponent_id)] = timestamp
                    wins = max(0, int(state.get("wins", 0) or 0))
                    gain = 2 if wins == 0 else 1
                    blood = min(3, max(0, int(state.get("blood", 0) or 0)) + gain)
                    state.update({"night_key": period_key, "blood": blood, "wins": wins + 1, "opponents": opponent_log})
                    notes.append(
                        self._race_effect_message(
                            guild_id,
                            user_id,
                            "hunt",
                            f"+{gain} Sangue{'s' if gain != 1 else ''} ({blood}/3)",
                            emoji_count=blood,
                        )
                    )
                    if blood >= 3:
                        state["blood"] = 0
                        state["harvests"] = harvests + 1
                        bonus_delta = 35
                        bonus_reason = "Harvest do Vampiro"
                        notes.append(self._race_effect_message(guild_id, user_id, "harvest", f"+35 {self._CHIP_BONUS_EMOJI} ({state['harvests']}/2 nesta noite)"))

                elif race_key == "fenix":
                    if period != "day" or entry_total <= 0 or won is None:
                        return []
                    if str(state.get("day_key") or "") != period_key:
                        state = {"day_key": period_key, "embers": 0, "second_dawn_used": False}
                    embers = min(2, max(0, int(state.get("embers", 0) or 0)))
                    if won:
                        if embers <= 0:
                            return []
                        reward = 30 if embers == 1 else 40
                        state["embers"] = 0
                        bonus_delta = reward
                        bonus_reason = "Rebirth da Fênix"
                        notes.append(self._race_effect_message(guild_id, user_id, "rebirth", f"{embers} Brasa{'s' if embers != 1 else ''} consumida{'s' if embers != 1 else ''}; +{reward} {self._CHIP_BONUS_EMOJI}"))
                    else:
                        if embers < 2:
                            embers += 1
                            state["embers"] = embers
                            notes.append(self._race_effect_message(guild_id, user_id, "sunrise", f"1 Brasa armazenada ({embers}/2)", emoji_count=embers))
                        if not bool(state.get("second_dawn_used", False)):
                            chips_now = int(doc.get("chips", CHIPS_INITIAL) or 0)
                            bonus_now = max(0, int(doc.get("bonus_chips", 0) or 0))
                            total_now = chips_now + bonus_now
                            if total_now < 30:
                                bonus_delta += 30
                                bonus_reason = "Second Dawn da Fênix"
                                state["second_dawn_used"] = True
                                notes.append(self._race_effect_message(guild_id, user_id, "second_dawn", f"+30 {self._CHIP_BONUS_EMOJI}"))

                elif race_key == "glitch":
                    if entry_total <= 0 or not glitch_progress:
                        return []
                    fragments = min(2, max(0, int(state.get("fragments", 0) or 0)))
                    if won is None:
                        if fragments < 2:
                            fragments += 1
                            state["fragments"] = fragments
                            notes.append(self._race_effect_message(guild_id, user_id, "desync", f"{fragments}/3 fragmentos"))
                        else:
                            state["fragments"] = 2
                            notes.append(self._race_effect_message(guild_id, user_id, "desync", "ERROR pendente até um resultado decisivo"))
                    else:
                        fragments += 1
                        if fragments < 3:
                            state["fragments"] = fragments
                            notes.append(self._race_effect_message(guild_id, user_id, "desync", f"{fragments}/3 fragmentos"))
                        else:
                            notes.append(self._race_effect_message(guild_id, user_id, "desync", "3/3 fragmentos • ERROR"))
                            if won:
                                net_profit = max(0, int(payout or 0) - entry_total)
                                reward = min(45, max(30, 20 + int(net_profit * 0.5)))
                                bonus_delta = reward
                                bonus_reason = "Overflow do Glitch"
                                notes.append(self._race_effect_message(guild_id, user_id, "overflow", f"+{reward} {self._CHIP_BONUS_EMOJI}"))
                            else:
                                refund_total = min(20, int(entry_total * 0.75))
                                refund = self._split_refund_by_entry(spend, refund_total)
                                normal_delta = refund["chips"]
                                bonus_delta = refund["bonus"]
                                normal_reason = "Rollback do Glitch"
                                bonus_reason = "Rollback do Glitch"
                                parts = []
                                if normal_delta:
                                    parts.append(f"{normal_delta} {self._CHIP_EMOJI}")
                                if bonus_delta:
                                    parts.append(f"{bonus_delta} {self._CHIP_BONUS_EMOJI}")
                                notes.append(self._race_effect_message(guild_id, user_id, "rollback", "+" + " + ".join(parts) if parts else "nenhuma ficha devolvida"))
                            leaked = random.random() < 0.25
                            state["fragments"] = 1 if leaked else 0
                            if leaked:
                                notes.append(self._race_effect_message(guild_id, user_id, "memory_leak", "1 fragmento preservado"))

                state_root[race_key] = state
                doc["race_state"] = state_root
                if normal_delta:
                    doc["chips"] = int(doc.get("chips", CHIPS_INITIAL) or 0) + normal_delta
                if bonus_delta:
                    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + bonus_delta
                if normal_delta or bonus_delta:
                    doc["has_chip_activity"] = True
                await self.db._save_user_doc(guild_id, user_id, doc)
                if normal_delta:
                    try:
                        await self.db.append_chip_history(guild_id, user_id, delta=normal_delta, kind="chips", reason=normal_reason or "Efeito de raça")
                    except Exception:
                        pass
                if bonus_delta:
                    try:
                        await self.db.append_chip_history(guild_id, user_id, delta=bonus_delta, kind="bonus", reason=bonus_reason or "Efeito de raça")
                    except Exception:
                        pass
                return [note for note in notes if note]
        except Exception as exc:
            print(f"[games:races] Falha ao aplicar raça para {guild_id}/{user_id}: {exc}")
            return []

    def _roleta_cost_for_user(self, guild_id: int, user_id: int) -> int:
        return ROLETA_APOSTADOR_COST if self._race_is(guild_id, user_id, "apostador") else ROLETA_COST

    def _special_variant_chance_for_user(self, guild_id: int, user_id: int) -> float:
        return RACE_SPECIAL_SORTUDO_CHANCE if self._race_is(guild_id, user_id, "sortudo") else RACE_SPECIAL_DEFAULT_CHANCE

    def _truco_bonus_reward_for_variant(self, variant: str) -> int:
        return 10 + TRUCO_GOLDEN_BONUS_EXTRA if str(variant or "normal").lower() == "golden" else 10

    def _limited_action_config(self, guild_id: int, user_id: int, *, action: str) -> tuple[int, float]:
        action = str(action or "").strip().lower()
        if action == "robbery":
            if self._race_is(guild_id, user_id, "preto"):
                return 2, float(4 * 60 * 60)
            return 1, float(6 * 60 * 60)
        if action == "mendigar":
            if self._race_is(guild_id, user_id, "preto"):
                return 2, float(CHIPS_MENDIGAR_COOLDOWN_SECONDS)
            return 1, float(CHIPS_MENDIGAR_COOLDOWN_SECONDS)
        return 1, 0.0

    def _limited_action_state(self, guild_id: int, user_id: int, *, storage_key: str, limit: int, window_seconds: float) -> dict[str, float | int]:
        now = time.time()
        doc = self.db._get_user_doc(guild_id, user_id)
        try:
            started_at = float(doc.get(f"{storage_key}_window_started_at", 0) or 0.0)
        except Exception:
            started_at = 0.0
        try:
            used = max(0, int(doc.get(f"{storage_key}_uses", 0) or 0))
        except Exception:
            used = 0
        window = max(0.0, float(window_seconds or 0.0))
        if started_at <= 0 or (window > 0 and (started_at + window) <= now):
            started_at = 0.0
            used = 0
        available = max(0, int(limit) - used)
        remaining = max(0.0, (started_at + window) - now) if available <= 0 and started_at > 0 and window > 0 else 0.0
        return {
            "started_at": float(started_at),
            "used": int(used),
            "available": int(available),
            "limit": int(limit),
            "window_seconds": float(window),
            "remaining": float(remaining),
        }

    async def _consume_limited_action(self, guild_id: int, user_id: int, *, storage_key: str, limit: int, window_seconds: float, legacy_field: str | None = None) -> tuple[bool, dict[str, float | int]]:
        state = self._limited_action_state(guild_id, user_id, storage_key=storage_key, limit=limit, window_seconds=window_seconds)
        if int(state.get("available", 0) or 0) <= 0:
            return False, state
        now = time.time()
        doc = self.db._get_user_doc(guild_id, user_id)
        started_at = float(state.get("started_at", 0.0) or 0.0)
        if started_at <= 0:
            started_at = now
        doc[f"{storage_key}_window_started_at"] = float(started_at)
        doc[f"{storage_key}_uses"] = int(state.get("used", 0) or 0) + 1
        if legacy_field:
            doc[str(legacy_field)] = float(now)
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True, self._limited_action_state(guild_id, user_id, storage_key=storage_key, limit=limit, window_seconds=window_seconds)

    async def _sync_sortudo_blessings(self, guild_id: int, user_id: int) -> dict[str, float | int]:
        now = float(time.time())
        doc = self.db._get_user_doc(guild_id, user_id)
        has_new_fields = "race_sortudo_blessing_charges" in doc or "race_sortudo_blessing_started_at" in doc
        is_sortudo = self._get_user_race_key(guild_id, user_id) == "sortudo"
        changed = False

        if not is_sortudo:
            for field in ("race_free_roleta_spins", "race_free_carta_spins", "race_sortudo_blessing_charges", "race_sortudo_blessing_started_at"):
                if field in doc:
                    changed = True
                    doc.pop(field, None)
            if changed:
                await self.db._save_user_doc(guild_id, user_id, doc)
            return {"charges": 0, "capacity": 2, "started_at": 0.0, "remaining": 0.0}

        try:
            charges = int(doc.get("race_sortudo_blessing_charges", 0) or 0)
        except Exception:
            charges = 0
        try:
            started_at = float(doc.get("race_sortudo_blessing_started_at", 0.0) or 0.0)
        except Exception:
            started_at = 0.0

        charges = max(0, min(2, charges))
        if not has_new_fields:
            charges = 1
            started_at = now
            changed = True

        if charges >= 2:
            if started_at != 0.0:
                started_at = 0.0
                changed = True
        else:
            if started_at <= 0.0:
                started_at = now
                changed = True
            elapsed = max(0.0, now - started_at)
            gained = int(elapsed // SORTUDO_BLESSING_INTERVAL_SECONDS)
            if gained > 0:
                charges = min(2, charges + gained)
                changed = True
                if charges >= 2:
                    started_at = 0.0
                else:
                    started_at = float(started_at + (gained * SORTUDO_BLESSING_INTERVAL_SECONDS))

        for field in ("race_free_roleta_spins", "race_free_carta_spins"):
            if field in doc:
                changed = True
                doc.pop(field, None)

        if doc.get("race_sortudo_blessing_charges") != charges:
            doc["race_sortudo_blessing_charges"] = int(charges)
            changed = True
        if float(doc.get("race_sortudo_blessing_started_at", 0.0) or 0.0) != float(started_at):
            doc["race_sortudo_blessing_started_at"] = float(started_at)
            changed = True

        if changed:
            await self.db._save_user_doc(guild_id, user_id, doc)

        remaining = 0.0
        if charges < 2 and started_at > 0.0:
            remaining = max(0.0, SORTUDO_BLESSING_INTERVAL_SECONDS - max(0.0, now - started_at))
        return {"charges": int(charges), "capacity": 2, "started_at": float(started_at), "remaining": float(remaining)}

    async def _consume_sortudo_blessing(self, guild_id: int, user_id: int) -> bool:
        state = await self._sync_sortudo_blessings(guild_id, user_id)
        charges = int(state.get("charges", 0) or 0)
        if charges <= 0:
            return False
        now = float(time.time())
        doc = self.db._get_user_doc(guild_id, user_id)
        started_at = float(doc.get("race_sortudo_blessing_started_at", 0.0) or 0.0)
        doc["race_sortudo_blessing_charges"] = charges - 1
        if charges >= 2 and started_at <= 0.0:
            doc["race_sortudo_blessing_started_at"] = now
        elif started_at <= 0.0:
            doc["race_sortudo_blessing_started_at"] = now
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True

    def _sortudo_blessing_note(self, guild_id: int, user_id: int, *, kind: str) -> str:
        kind_key = str(kind or "").strip().lower()
        detail = "uma carga pagou este giro" if kind_key == "roleta" else "uma carga pagou esta mão"
        marker = self._race_effect_message(guild_id, user_id, "bencao", detail)
        if marker:
            return marker
        return "Uma bênção pagou este giro" if kind_key == "roleta" else "Uma bênção pagou esta mão"

    async def _maybe_apply_coringa_cashback(self, guild_id: int, user_id: int, entry_cost: int, *, chance: float = 0.35) -> int:
        if entry_cost <= 0 or not self._race_is(guild_id, user_id, "coringa"):
            return 0
        if random.random() >= float(chance):
            return 0
        refund = max(1, int(round(int(entry_cost) * 0.5)))
        await self._change_user_chips(guild_id, user_id, refund, mark_activity=True, reason="Reembolso Coringa")
        return refund

    async def _maybe_apply_coringa_lobby_refund(self, guild_id: int, user_id: int, entry_cost: int) -> int:
        return await self._maybe_apply_coringa_cashback(guild_id, user_id, entry_cost, chance=0.35)

    def _coringa_avoids_robbery_penalty(self, guild_id: int, user_id: int) -> bool:
        return self._race_is(guild_id, user_id, "coringa") and random.random() < 0.25

    def _chip_rank_position_text(self, guild: discord.Guild, user_id: int) -> str | None:
        rows = self.db.get_chip_leaderboard(guild.id, limit=1000000)
        for index, row in enumerate(rows, start=1):
            try:
                if int(row.get("user_id", 0)) == int(user_id):
                    return f"🏆 Rank: **#{index}**"
            except Exception:
                continue
        return None

    def _make_chip_balance_view(self, member: discord.Member) -> discord.ui.LayoutView:
        guild_id = member.guild.id
        chips = self.db.get_user_chips(guild_id, member.id, default=CHIPS_INITIAL)
        stats = self.db.get_user_game_stats(guild_id, member.id)

        _, _, _, rate = self._chip_summary_stats(stats)
        summary_lines = self._build_chip_game_stat_lines(stats)
        summary_lines.append(f"📈 **Taxa**: **{rate}**")

        primary_balance = self._format_primary_chip_balance(guild_id, member.id)
        race_identity = self._format_race_identity(guild_id, member.id)
        if race_identity:
            primary_balance = f"{primary_balance} • **Raça:** {race_identity}"
        balance_lines = [
            f"# {self._CHIP_EMOJI} Fichas",
            primary_balance,
        ]
        rank_text = self._chip_rank_position_text(member.guild, member.id)
        if rank_text:
            balance_lines.append(rank_text)
        balance_lines.append(f"🎁 **Diário**: {self._daily_bonus_text(guild_id, member.id)}")
        balance_lines.append(f"⏳ **Recarga**: {self._chip_recharge_compact_text(guild_id, member.id)}")
        achievements = self._get_unlocked_achievements(guild_id, member.id)
        if achievements:
            balance_lines.append(f"🏆 **Conquistas:** {' • '.join(achievements)}")
        if not race_identity:
            balance_lines.append("**🧬 Raça:** Use **race** pra definir sua raça")
        if chips < 0:
            balance_lines.append("Ganhos futuros quitam a dívida primeiro")

        detail_lines: list[str] = []
        best_game = self._best_game_summary(stats)
        if best_game:
            detail_lines.append(f"🎮 **Melhor jogo**: {best_game}")
        detail_lines.extend([
            "# 📊 Resumo",
            "\n".join(summary_lines),
            "Dica: **_rank** mostra sua posição • **_daily** pega seu bônus diário",
        ])

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(balance_lines)),
            accent_color=discord.Color.blurple(),
        ))
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(detail_lines)),
            accent_color=discord.Color.dark_green(),
        ))
        return view

    def _format_chip_history_relative_time(self, ts: float) -> str:
        try:
            delta = max(0.0, time.time() - float(ts))
        except Exception:
            return "—"
        if delta < 60:
            return "agora"
        if delta < 3600:
            return f"há {int(delta // 60)}min"
        if delta < 86400:
            return f"há {int(delta // 3600)}h"
        days = int(delta // 86400)
        if days == 1:
            return "ontem"
        if days < 7:
            return f"há {days}d"
        weeks = days // 7
        if weeks < 5:
            return f"há {weeks}sem"
        return f"há {days}d"

    def _make_chip_history_view(self, member: discord.Member, *, limit: int = 10) -> discord.ui.LayoutView:
        guild_id = member.guild.id
        try:
            entries = self.db.get_chip_history(guild_id, member.id, limit=int(limit))
        except Exception:
            entries = []

        header_text = (
            f"# 📒 Extrato — {member.display_name}\n"
            f"Últimas **{int(limit)}** movimentações de fichas"
        )

        view = discord.ui.LayoutView(timeout=None)

        if not entries:
            empty_lines = [
                header_text,
                "",
                "Nenhuma movimentação registrada ainda",
                "Entre em uma rodada e o histórico começa a contar",
            ]
            view.add_item(discord.ui.Container(
                discord.ui.TextDisplay("\n".join(empty_lines)),
                accent_color=discord.Color.gold(),
            ))
            return view

        movement_lines: list[str] = []
        chips_total = 0
        bonus_total = 0
        for entry in entries:
            try:
                delta = int(entry.get("delta", 0) or 0)
            except Exception:
                delta = 0
            if delta == 0:
                continue
            kind = str(entry.get("kind") or "chips").lower()
            is_bonus = (kind == "bonus")
            if is_bonus:
                chip_emoji = self._CHIP_BONUS_EMOJI
                bonus_total += delta
            else:
                chip_emoji = self._CHIP_GAIN_EMOJI if delta > 0 else self._CHIP_LOSS_EMOJI
                chips_total += delta
            amount_text = f"+{delta}" if delta > 0 else f"{delta}"
            reason = str(entry.get("reason") or "").strip() or "Transação"
            ts = entry.get("ts") or 0
            when = self._format_chip_history_relative_time(ts)
            movement_lines.append(f"{chip_emoji} **{amount_text}** • {reason} · _{when}_")

        if not movement_lines:
            movement_lines = ["Nenhuma movimentação registrada ainda"]

        net_parts: list[str] = []
        if chips_total != 0:
            sign = "+" if chips_total > 0 else ""
            chip_icon = self._CHIP_GAIN_EMOJI if chips_total > 0 else self._CHIP_LOSS_EMOJI
            net_parts.append(f"{chip_icon} **{sign}{chips_total}**")
        if bonus_total != 0:
            sign = "+" if bonus_total > 0 else ""
            net_parts.append(f"{self._CHIP_BONUS_EMOJI} **{sign}{bonus_total}**")
        net_text = " • ".join(net_parts) if net_parts else f"{self._CHIP_EMOJI} **0**"
        balance_text = f"💰 **Saldo atual:** {self._format_primary_chip_balance(guild_id, member.id)}"

        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(header_text),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(movement_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"📊 **Saldo destas {len(entries)} movimentações:** {net_text}"),
            discord.ui.TextDisplay(balance_text),
            accent_color=discord.Color.gold(),
        ))
        return view
    def _make_chip_balance_embed(self, member: discord.Member) -> discord.Embed:
        guild_id = member.guild.id
        stats = self.db.get_user_game_stats(guild_id, member.id)
        _, _, _, rate = self._chip_summary_stats(stats)
        embed = discord.Embed(color=discord.Color.blurple())
        embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)
        embed.description = f"{self._format_primary_chip_balance(guild_id, member.id)}\n📈 **Taxa de vitórias**: **{rate}**"
        return embed

    async def _maybe_execute_due_chip_season_reset(self, guild_id: int) -> dict | None:
        return None

    async def _prepare_chip_leaderboard_state(self, guild: discord.Guild, requester: discord.Member | None = None) -> dict:
        return self.db.get_chip_season_state(guild.id)

    def _build_chip_rank_footer(self, guild_id: int) -> str:
        state = self.db.get_chip_season_state(guild_id)
        season = max(1, int(state.get("season", 1) or 1))
        return f"Temporada {season}"

    async def _make_chip_leaderboard_embed_async(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        embed = self._make_chip_leaderboard_embed(guild, requester)
        embed.set_footer(text=self._build_chip_rank_footer(guild.id))
        return embed

    def _make_chip_leaderboard_embed(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        rows = self.db.get_chip_leaderboard(guild.id, limit=10)
        embed = discord.Embed(
            title="🏆 Rank do servidor",
            description="Os maiores saldos deste servidor",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.add_field(name="Top 10", value="Ainda não há jogadores com movimentação nas fichas", inline=False)
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            ranking_lines = []
            for index, row in enumerate(rows, start=1):
                member = guild.get_member(int(row["user_id"]))
                name = member.display_name if member is not None else f"Usuário {row['user_id']}"
                prefix = medals.get(index, f"`#{index}`")
                chips_val = int(row.get('chips', row.get('points', 0)) or 0)
                bonus_val = self._get_user_bonus_chips(guild.id, int(row["user_id"]))
                emoji = self._CHIP_LOSS_EMOJI if chips_val < 0 else self._CHIP_EMOJI
                balance_text = f"**{chips_val}** {emoji}"
                if bonus_val > 0:
                    balance_text += f" • **{bonus_val}** {self._CHIP_BONUS_EMOJI}"
                ranking_lines.append(f"{prefix} **{name}** — {balance_text}")
            embed.add_field(name="Top 10", value="\n".join(ranking_lines), inline=False)

        embed.set_footer(text=self._build_chip_rank_footer(guild.id))
        return embed

    def _format_chip_reset_remaining(self, remaining_seconds: float) -> str:
        remaining = max(0, int(remaining_seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _try_consume_chips(self, guild_id: int, user_id: int, amount: int, *, reason: str | None = None) -> tuple[bool, int, str | None]:
        spend = max(0, int(amount))
        projected_chips, projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, spend)
        current_before = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        note = self._negative_transition_note(guild_id, user_id, spend)
        if projected_chips < -self._MAX_CHIP_DEBT:
            return False, current_before, self._insufficient_chips_text(guild_id, user_id, spend)
        current_bonus = self._get_user_bonus_chips(guild_id, user_id)
        use_bonus = min(current_bonus, spend)
        remaining = spend - use_bonus
        if use_bonus > 0:
            await self._change_user_bonus_chips(guild_id, user_id, -use_bonus, mark_activity=True, reason=reason)
        if remaining > 0:
            await self._change_user_chips(guild_id, user_id, -remaining, mark_activity=True, reason=reason)
        return True, projected_chips, note

    async def _ensure_action_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        projected_chips, _projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, amount)
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        if projected_chips < -self._MAX_CHIP_DEBT:
            return False, current, self._insufficient_chips_text(guild_id, user_id, amount)
        note = self._negative_transition_note(guild_id, user_id, amount)
        return True, current, note

    async def _reject_if_not_allowed_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            embed = self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False)
        else:
            return False

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)
        return True

    def _gincana_only_kick_members(self, guild_id: int) -> bool:
        # O modo "somente staff" foi removido do painel da economia. Mantemos
        # o helper para que handlers antigos não precisem de migração em massa.
        return False

    def _get_staff_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = 0
        try:
            role_id = max(0, int(self.db.get_gincana_staff_role_id(guild.id) or 0))
        except Exception:
            role_id = 0
        return guild.get_role(role_id) if role_id else None

    def _get_callkeeper_bot_ids(self) -> set[int]:
        cached = getattr(self, "_gincana_callkeeper_bot_ids_cache", None)
        if cached is not None:
            return set(cached)

        ids: set[int] = set()
        for raw_token in CALLKEEPER_BOT_TOKENS or []:
            token = str(raw_token or "").strip()
            if not token or "." not in token:
                continue
            head = token.split(".", 1)[0].strip()
            if not head:
                continue
            try:
                padded = head + ("=" * (-len(head) % 4))
                decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii", "ignore").strip()
                bot_id = int(decoded)
            except Exception:
                continue
            if bot_id > 0:
                ids.add(bot_id)

        frozen = frozenset(ids)
        setattr(self, "_gincana_callkeeper_bot_ids_cache", frozen)
        return set(frozen)

    def _is_callkeeper_bot(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        try:
            member_id = int(getattr(member, "id", 0) or 0)
        except Exception:
            return False
        return member_id in self._get_callkeeper_bot_ids()

    def _is_staff_member(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.kick_members:
            return True

        guild = member.guild
        staff_role = self._get_staff_role(guild)
        return staff_role is not None and staff_role in getattr(member, "roles", [])

    def _gincana_focus_sync_groups(self, guild_id: int) -> list[list[int]]:
        getter = getattr(self.db, "get_gincana_focus_sync_groups", None)
        if not callable(getter):
            return []
        try:
            raw_groups = getter(int(guild_id)) or []
        except Exception:
            return []

        groups: list[list[int]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, (list, tuple, set)):
                continue
            group: list[int] = []
            seen: set[int] = set()
            for raw_uid in raw_group:
                try:
                    uid = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if uid <= 0 or uid in seen:
                    continue
                seen.add(uid)
                group.append(uid)
            if len(group) >= 2:
                groups.append(group)
        return groups

    def _gincana_focus_sync_map(self, guild_id: int) -> dict[int, set[int]]:
        sync_map: dict[int, set[int]] = {}
        for group in self._gincana_focus_sync_groups(guild_id):
            group_set = {int(uid) for uid in group if int(uid) > 0}
            if len(group_set) < 2:
                continue
            for uid in group_set:
                sync_map.setdefault(uid, set()).update(group_set)
        return sync_map

    def _expand_gincana_focus_ids(self, guild_id: int, user_ids) -> list[int]:
        expanded: dict[int, None] = {}
        sync_map = self._gincana_focus_sync_map(int(guild_id))
        for raw_uid in user_ids or []:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            for candidate_id in sorted(sync_map.get(uid, {uid})):
                expanded[int(candidate_id)] = None
        return list(expanded.keys())

    def _expand_gincana_target_members(self, guild: discord.Guild, members: list[discord.Member]) -> list[discord.Member]:
        result: dict[int, discord.Member] = {}
        seed_ids: list[int] = []
        own_bot_id = int(getattr(getattr(self.bot, "user", None), "id", 0) or 0)
        for member in members or []:
            if member is None or self._is_callkeeper_bot(member):
                continue
            if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
                continue
            result[int(member.id)] = member
            seed_ids.append(int(member.id))

        for uid in self._expand_gincana_focus_ids(guild.id, seed_ids):
            if uid in result:
                continue
            member = guild.get_member(int(uid))
            if member is None or self._is_callkeeper_bot(member):
                continue
            if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
                continue
            result[int(member.id)] = member
        return list(result.values())

    def _is_focused_non_staff_member(self, member: discord.Member) -> bool:
        guild = getattr(member, "guild", None)
        if guild is None or self._is_staff_member(member) or self._is_callkeeper_bot(member):
            return False
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return False
        focused_ids = set(self._expand_gincana_focus_ids(guild.id, focus_map.keys()))
        return int(member.id) in focused_ids

    async def _set_gincana_only_kick_members(self, guild_id: int, value: bool):
        if hasattr(self.db, "_get_guild_doc") and hasattr(self.db, "_save_guild_doc"):
            doc = self.db._get_guild_doc(guild_id)
            doc["gincana_only_kick_members"] = bool(value)
            doc["anti_mzk_only_kick_members"] = bool(value)
            await self.db._save_guild_doc(guild_id, doc)
            return

        guild_cache = getattr(self.db, "guild_cache", None)
        coll = getattr(self.db, "coll", None)
        if guild_cache is not None:
            doc = guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
            doc["gincana_only_kick_members"] = bool(value)
            doc["anti_mzk_only_kick_members"] = bool(value)
            guild_cache[guild_id] = doc
            if coll is not None:
                await coll.update_one(
                    {"type": "guild", "guild_id": guild_id},
                    {"$set": doc},
                    upsert=True,
                )

    def _iter_target_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {}
        role_ids = set(self.db.get_gincana_role_ids(guild.id))

        if not role_ids:
            return []

        for member in voice_channel.members:
            if self._is_callkeeper_bot(member):
                continue
            member_role_ids = {role.id for role in getattr(member, "roles", [])}
            if member_role_ids & role_ids:
                targets[member.id] = member

        return list(targets.values())

    def _iter_focused_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> list[discord.Member]:
        focus_map = self.db.get_gincana_focus_map(guild.id)
        if not focus_map:
            return []

        focused_ids = set(self._expand_gincana_focus_ids(guild.id, focus_map.keys()))
        targets: dict[int, discord.Member] = {}
        for member in voice_channel.members:
            if self._is_callkeeper_bot(member):
                continue
            if member.id in focused_ids:
                targets[member.id] = member
        return list(targets.values())

    def _resolve_targets(self, guild: discord.Guild, voice_channel: discord.VoiceChannel | discord.StageChannel) -> list[discord.Member]:
        focused = self._iter_focused_members(guild, voice_channel)
        if focused:
            return self._expand_gincana_target_members(guild, focused)
        return self._expand_gincana_target_members(guild, self._iter_target_members(guild, voice_channel))

    async def _react_success_temporarily(self, message: discord.Message):
        try:
            await message.add_reaction("✅")
        except Exception:
            return

        async def _cleanup():
            await asyncio.sleep(3)
            try:
                await message.remove_reaction("✅", self.bot.user)
            except Exception:
                pass

        asyncio.create_task(_cleanup())
