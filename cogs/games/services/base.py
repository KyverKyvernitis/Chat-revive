from __future__ import annotations

import asyncio
import io
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from config import OFF_COLOR, ON_COLOR
from ..chip_profile_renderer import ChipProfileData
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
from ..rank_renderer import format_number, format_weekly_delta
from .achievement_notices import AchievementNoticeBurst, merge_achievement_keys
from .chip_profile_cache import ChipProfileCache, PROFILE_FILENAME
from .rank_cache import (
    ChipRankCache,
    ChipRankResponse,
    RANK_FILENAME,
    format_weekly_chip_summary,
    rank_page_target,
)
from .session_registry import GameSessionRegistry, MAX_ACTIVE_GAME_USERS_PER_GUILD


SORTUDO_BLESSING_INTERVAL_SECONDS = 7 * 60 * 60
SORTUDO_DAILY_EXTRA_BONUS = 5
SORTUDO_STREAK_EXTRA_BONUS = 5
RANK_PREVIOUS_EMOJI = "<a:k0_SetaE:1542282885153816596>"
RANK_NEXT_EMOJI = "<a:k0_SetaD:1542282957966802986>"
RANK_PAGINATION_TIMEOUT_SECONDS = 10 * 60
RACE_SKILL_REBORN_COOLDOWN_SECONDS = 6 * 60 * 60
RACE_SKILL_COINFLIP_BONUS = 50
RACE_SKILL_COINFLIP_SECONDS = 10
RACE_SKILL_JACKPOT_BONUS = 20
RACE_SKILL_JOKER_SECONDS = 60
RACE_SKILL_JOKER_REFUND_CAP = 50
RACE_SKILL_0TO1_LIMIT = 50


class _NegativeDebtConfirmView(discord.ui.LayoutView):
    def __init__(self, *, owner_id: int, projected_chips: int, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.projected_chips = int(projected_chips)
        self.confirmed = False
        self.message: discord.Message | None = None
        self.confirm_button = discord.ui.Button(label="Continuar", style=discord.ButtonStyle.danger)
        self.confirm_button.callback = self._confirm
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"# <:emoji_65:1485043671077228786> Saldo negativo\n\n"
                    f"Este jogo deixará seu saldo em {self.projected_chips}\n"
                    f"-# Continue para permitir saldo negativo até recuperá-lo, ou use _recarga para adicionar "
                    f"+{CHIPS_DEFAULT} <:laranja:1487076933819830443> à sua conta"
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(self.confirm_button),
                accent_color=discord.Color.red(),
            )
        )

    async def _confirm(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.owner_id:
            try:
                await interaction.response.send_message("Essa confirmação não é para você", ephemeral=True)
            except Exception:
                pass
            return
        self.confirmed = True
        self.confirm_button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass
        self.stop()

    async def on_timeout(self):
        self.confirm_button.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass
        self.stop()


class _ChipRankPageButton(discord.ui.Button):
    def __init__(
        self,
        panel: "_ChipRankPaginationView",
        *,
        direction: int,
        source_page: int,
        disabled: bool,
    ):
        self.panel = panel
        self.direction = -1 if int(direction) < 0 else 1
        self.source_page = int(source_page)
        emoji = RANK_PREVIOUS_EMOJI if self.direction < 0 else RANK_NEXT_EMOJI
        custom_direction = "previous" if self.direction < 0 else "next"
        super().__init__(
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            disabled=bool(disabled),
            custom_id=f"games:rank:{custom_direction}:{self.source_page}",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.panel.change_page(
            interaction,
            direction=self.direction,
            source_page=self.source_page,
        )


class _ChipRankPaginationView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "GincanaBase",
        *,
        guild: discord.Guild,
        requester: discord.Member | None,
        response: ChipRankResponse,
    ):
        super().__init__(timeout=float(RANK_PAGINATION_TIMEOUT_SECONDS))
        self.cog = cog
        self.guild = guild
        self.requester = requester
        self.response = response
        self.message: discord.Message | None = None
        self._page_lock = asyncio.Lock()
        self._rebuild()

    def _rebuild(self, *, include_controls: bool = True) -> None:
        self.clear_items()
        controls = None
        if include_controls and self.response.page_count > 1:
            source_page = int(self.response.page_index)
            previous_button = _ChipRankPageButton(
                self,
                direction=-1,
                source_page=source_page,
                disabled=source_page <= 0,
            )
            next_button = _ChipRankPageButton(
                self,
                direction=1,
                source_page=source_page,
                disabled=source_page >= self.response.page_count - 1,
            )
            controls = discord.ui.ActionRow(previous_button, next_button)
        self.add_item(
            discord.ui.Container(
                *self.cog._chip_rank_components(self.response, self.guild, controls=controls)
            )
        )

    async def change_page(
        self,
        interaction: discord.Interaction,
        *,
        direction: int,
        source_page: int,
    ) -> None:
        try:
            await interaction.response.defer()
        except Exception:
            pass

        async with self._page_lock:
            # Dois cliques feitos sobre a mesma página não podem avançar duas vezes.
            target_page = rank_page_target(
                self.response.page_index,
                self.response.page_count,
                direction,
                source_page,
            )
            if target_page is None:
                return

            previous_response = self.response
            try:
                response = await self.cog._chip_rank_cache.get_rank(
                    self.guild,
                    self.requester,
                    page_index=target_page,
                )
                self.response = response
                self._rebuild()
                image = discord.File(io.BytesIO(response.image_bytes), filename=RANK_FILENAME)
                edited = await interaction.edit_original_response(
                    attachments=[image],
                    view=self,
                )
                if edited is not None:
                    self.message = edited
                if response.page_count <= 1:
                    self.stop()
            except Exception as exc:
                self.response = previous_response
                self._rebuild()
                print(
                    f"[games] erro ao paginar rank guild={getattr(self.guild, 'id', 0)} "
                    f"page={target_page}: {exc!r}"
                )
                try:
                    await interaction.followup.send(
                        view=self.cog._make_v2_notice(
                            "Rank indisponível",
                            ["Não foi possível abrir essa página agora"],
                            ok=False,
                        ),
                        ephemeral=True,
                    )
                except Exception:
                    pass

    async def on_timeout(self) -> None:
        async with self._page_lock:
            self._rebuild(include_controls=False)
            try:
                if self.message is not None:
                    await self.message.edit(view=self)
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
    _RACE_RUNTIME_FIELDS = (
        "race_free_roleta_spins",
        "race_free_carta_spins",
        "race_sortudo_blessing_charges",
        "race_sortudo_blessing_started_at",
        "race_robbery_window_started_at",
        "race_robbery_uses",
        "race_mendigar_window_started_at",
        "race_mendigar_uses",
        "race_skill_coinflip_temp_bonus",
        "race_skill_coinflip_temp_expires_at",
        "race_skill_coinflip_jackpot_bonus",
        "race_skill_coinflip_jackpot_expires_at",
        "race_skill_changefate_golden_until",
        "race_skill_joker_until",
        "race_state",
    )
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
        self._race_panel_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._race_rerolls_in_progress: set[tuple[int, int]] = set()
        self._race_reroll_confirmation_versions: dict[tuple[int, int], int] = {}
        self._changefate_golden_reservations: dict[tuple[int, int], str] = {}
        self._achievement_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._achievement_notice_groups: dict[tuple[int, int, int], AchievementNoticeBurst] = {}
        self._achievement_notice_cleanup_task: asyncio.Task | None = None
        self._race_private_notices: dict[tuple[int, int], list[str]] = {}
        self._negative_debt_message_gates: dict[tuple[int, int], dict] = {}
        self._negative_debt_gate_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._chip_economy_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._chip_rank_cache = ChipRankCache(bot, db)
        self._chip_profile_cache = ChipProfileCache(bot, self._chip_rank_cache)


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

    def _make_skill_notice(
        self,
        title: str,
        lines: list[str],
        *,
        state: str = "success",
        accent_color: discord.Color | None = None,
    ) -> discord.ui.LayoutView:
        """Cria respostas compactas e consistentes para skills de raça."""
        view = discord.ui.LayoutView(timeout=None)
        normalized_state = str(state or "success").strip().lower()
        if accent_color is not None:
            color = accent_color
        elif normalized_state == "error":
            color = discord.Color(OFF_COLOR)
        elif normalized_state == "neutral":
            color = discord.Color.dark_grey()
        else:
            color = discord.Color(ON_COLOR)
        body = [f"## {title}"]
        body.extend([str(line) for line in lines if str(line).strip()])
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(body)), accent_color=color))
        return view

    def _skill_chip_value(
        self,
        amount: int,
        *,
        kind: str = "normal",
        movement: str = "neutral",
    ) -> str:
        """Formata fichas das skills com a mesma semântica visual do extrato."""
        value = abs(int(amount))
        normalized_kind = str(kind or "normal").strip().lower()
        normalized_movement = str(movement or "neutral").strip().lower()
        if normalized_kind == "bonus":
            emoji = self._CHIP_BONUS_EMOJI
        elif normalized_movement == "gain":
            emoji = self._CHIP_GAIN_EMOJI
        elif normalized_movement == "loss":
            emoji = self._CHIP_LOSS_EMOJI
        else:
            emoji = self._CHIP_EMOJI
        sign = "+" if normalized_movement == "gain" else "-" if normalized_movement == "loss" else ""
        return f"{emoji} **{sign}{value}**"

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

    def _coinflip_temp_bonus_available(
        self,
        guild_id: int,
        user_id: int,
        *,
        now: float | None = None,
    ) -> int:
        doc = self.db._get_user_doc(guild_id, user_id)
        expires_at = float(doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
        if expires_at <= float(time.time() if now is None else now):
            return 0
        return max(0, int(doc.get("race_skill_coinflip_temp_bonus", 0) or 0))

    async def _change_user_bonus_chips(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        mark_activity: bool = True,
        reason: str | None = None,
        history_metadata: dict | None = None,
    ) -> int:
        requested_delta = int(amount)
        async with self._chip_economy_lock(guild_id, user_id):
            old_bonus = self._get_user_bonus_chips(guild_id, user_id)
            new_bonus = await self.db.add_user_bonus_chips(guild_id, user_id, requested_delta)
            actual_delta = int(new_bonus) - int(old_bonus)
        if actual_delta != 0:
            if mark_activity:
                await self._mark_chip_activity(guild_id, user_id)
            try:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=actual_delta,
                    kind="bonus",
                    reason=reason,
                    **dict(history_metadata or {}),
                )
            except Exception:
                pass
        return int(new_bonus)

    async def _change_user_chips(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        mark_activity: bool = True,
        reason: str | None = None,
        history_metadata: dict | None = None,
    ) -> int:
        requested_delta = int(amount)
        async with self._chip_economy_lock(guild_id, user_id):
            old_balance = int(self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL) or 0)
            old_bonus = self._get_user_bonus_chips(guild_id, user_id)
            new_balance = await self.db.add_user_chips(guild_id, user_id, requested_delta)
            new_bonus = self._get_user_bonus_chips(guild_id, user_id)
            normal_delta = int(new_balance) - old_balance
            bonus_delta = int(new_bonus) - old_bonus
        if normal_delta != 0 or bonus_delta != 0:
            if mark_activity:
                await self._mark_chip_activity(guild_id, user_id)
            try:
                if normal_delta != 0:
                    await self.db.append_chip_history(
                        guild_id,
                        user_id,
                        delta=normal_delta,
                        kind="chips",
                        reason=reason,
                        **dict(history_metadata or {}),
                    )
                if bonus_delta != 0:
                    await self.db.append_chip_history(
                        guild_id,
                        user_id,
                        delta=bonus_delta,
                        kind="bonus",
                        reason=reason,
                        **dict(history_metadata or {}),
                    )
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
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["chips"] = int(amount)
            doc["bonus_chips"] = 0
            doc["has_chip_activity"] = True
            doc["last_chip_reset_at"] = 0.0
            doc["chip_recharge_manual_initialized"] = False
            doc["negative_balance_authorized"] = False
            doc["chip_week_key"] = ""
            doc["chip_week_delta"] = 0
            doc.pop("race_key", None)
            doc.pop("race_active", None)
            doc.pop("game_achievements", None)
            for field in self._RACE_RUNTIME_FIELDS:
                doc.pop(field, None)
            await self.db._save_user_doc(
                guild_id,
                user_id,
                doc,
                unset_fields=("race_key", "race_active", *self._RACE_RUNTIME_FIELDS),
            )
            await self.db.clear_user_game_achievements(guild_id, user_id)
            self._forget_achievement_notice_groups(guild_id, user_id)
            return int(amount)

    async def _force_full_reset_ficha_profile(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        async with self._achievement_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["chips"] = int(amount)
            doc["bonus_chips"] = 0
            doc["last_chip_reset_at"] = 0.0
            doc["chip_recharge_manual_initialized"] = False
            doc["negative_balance_authorized"] = False
            doc["daily_last_claim_key"] = ""
            doc["daily_streak"] = 0
            doc["weekly_points_week"] = ""
            doc["weekly_points"] = 0
            doc["chip_week_key"] = ""
            doc["chip_week_delta"] = 0
            doc["game_stats"] = {}
            doc["has_chip_activity"] = False
            doc.pop("race_key", None)
            doc.pop("race_active", None)
            doc.pop("game_achievements", None)
            for field in self._RACE_RUNTIME_FIELDS:
                doc.pop(field, None)
            await self.db._save_user_doc(
                guild_id,
                user_id,
                doc,
                unset_fields=("race_key", "race_active", *self._RACE_RUNTIME_FIELDS),
            )
            await self.db.clear_user_game_achievements(guild_id, user_id)
            self._forget_achievement_notice_groups(guild_id, user_id)
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

    def _achievement_notice_key(self, channel, guild_id: int, user_id: int) -> tuple[int, int, int]:
        try:
            channel_id = int(getattr(channel, "id", 0) or 0)
        except (TypeError, ValueError):
            channel_id = 0
        if channel_id <= 0:
            channel_id = id(channel)
        return int(guild_id), channel_id, int(user_id)

    def _prune_achievement_notice_groups(self, now: float) -> None:
        for key, burst in list(self._achievement_notice_groups.items()):
            if burst.is_expired(now):
                self._achievement_notice_groups.pop(key, None)

    def _ensure_achievement_notice_cleanup_task(self) -> None:
        task = self._achievement_notice_cleanup_task
        if task is None or task.done():
            self._achievement_notice_cleanup_task = asyncio.create_task(
                self._achievement_notice_cleanup_loop(),
                name="games-achievement-notice-cleanup",
            )

    async def _achievement_notice_cleanup_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while self._achievement_notice_groups:
                now = time.monotonic()
                self._prune_achievement_notice_groups(now)
                if not self._achievement_notice_groups:
                    return
                next_expiration = min(
                    burst.expires_at()
                    for burst in self._achievement_notice_groups.values()
                )
                await asyncio.sleep(max(0.05, next_expiration - now))
        finally:
            if self._achievement_notice_cleanup_task is current_task:
                self._achievement_notice_cleanup_task = None

    async def _close_achievement_notice_groups(self) -> None:
        task = self._achievement_notice_cleanup_task
        self._achievement_notice_cleanup_task = None
        self._achievement_notice_groups.clear()
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _forget_achievement_notice_groups(self, guild_id: int, user_id: int) -> None:
        guild_id = int(guild_id)
        user_id = int(user_id)
        for key in list(self._achievement_notice_groups):
            if key[0] == guild_id and key[2] == user_id:
                self._achievement_notice_groups.pop(key, None)

    def _forget_guild_achievement_notice_groups(self, guild_id: int) -> None:
        guild_id = int(guild_id)
        for key in list(self._achievement_notice_groups):
            if key[0] == guild_id:
                self._achievement_notice_groups.pop(key, None)

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
        achievement_keys,
        mention: str,
        *,
        unlocked_count: int,
        total_count: int,
        thumbnail_url: str | None = None,
    ) -> discord.ui.LayoutView | None:
        raw_keys = (achievement_keys,) if isinstance(achievement_keys, str) else tuple(achievement_keys or ())
        catalog = self._achievement_catalog()
        items = [catalog[key] for key in merge_achievement_keys((), raw_keys) if key in catalog]
        if not items:
            return None
        total = max(1, int(total_count or 0))
        count = max(1, min(total, int(unlocked_count or 0)))
        if len(items) == 1:
            title = "Conquista desbloqueada"
        else:
            title = f"{len(items)} conquistas desbloqueadas"
        blocks = []
        for item in items:
            description = str(item["description"]).format(mention=str(mention or "Alguém"))
            blocks.append(f"{item['emoji']} **{item['name']}**\n-# {description}")
        content = f"### 🏆 {title} ({count}/{total})\n\n" + "\n\n".join(blocks)
        body = discord.ui.TextDisplay(content)
        if thumbnail_url:
            body = discord.ui.Section(
                body,
                accessory=discord.ui.Thumbnail(
                    str(thumbnail_url),
                    description=title,
                ),
            )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            body,
            accent_color=discord.Color.gold(),
        ))
        return view

    async def _dispatch_achievement_notice(
        self,
        channel,
        achievement_keys,
        mention: str,
        *,
        unlocked_count: int,
        total_count: int,
    ) -> tuple[bool, object | None]:
        thumbnail_path = self._ACHIEVEMENT_THUMBNAIL_PATH
        use_thumbnail = thumbnail_path.is_file()
        attachment_url = (
            f"attachment://{self._ACHIEVEMENT_THUMBNAIL_FILENAME}"
            if use_thumbnail
            else None
        )
        view = self._make_achievement_view(
            achievement_keys,
            mention,
            unlocked_count=unlocked_count,
            total_count=total_count,
            thumbnail_url=attachment_url,
        )
        if view is None:
            return False, None

        if use_thumbnail:
            image_file = None
            try:
                image_file = discord.File(
                    str(thumbnail_path),
                    filename=self._ACHIEVEMENT_THUMBNAIL_FILENAME,
                )
                sent_message = await channel.send(
                    view=view,
                    file=image_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True, sent_message
            except Exception:
                pass
            finally:
                if image_file is not None:
                    try:
                        image_file.close()
                    except Exception:
                        pass

        fallback_view = self._make_achievement_view(
            achievement_keys,
            mention,
            unlocked_count=unlocked_count,
            total_count=total_count,
        )
        if fallback_view is None:
            return False, None
        try:
            sent_message = await channel.send(
                view=fallback_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True, sent_message
        except Exception:
            return False, None

    async def _delete_replaced_achievement_notice(self, message) -> bool:
        if message is None or not hasattr(message, "delete"):
            return False
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except Exception:
            return False

    async def _send_achievement_notices(
        self,
        channel,
        guild_id: int,
        user_id: int,
        achievement_keys,
    ) -> bool:
        if channel is None or not hasattr(channel, "send"):
            guild = self.bot.get_guild(int(guild_id)) if getattr(self, "bot", None) is not None else None
            channel = self._get_gincana_channel(guild) if guild is not None else None
        if channel is None or not hasattr(channel, "send"):
            return False

        raw_keys = (achievement_keys,) if isinstance(achievement_keys, str) else (achievement_keys or ())
        incoming_keys = merge_achievement_keys((), raw_keys)
        if not incoming_keys:
            return False

        guild_id = int(guild_id)
        user_id = int(user_id)
        notice_key = self._achievement_notice_key(channel, guild_id, user_id)
        async with self._achievement_lock(guild_id, user_id):
            now = time.monotonic()
            self._prune_achievement_notice_groups(now)
            previous_burst = self._achievement_notice_groups.get(notice_key)
            can_merge = bool(
                previous_burst is not None
                and previous_burst.message is not None
                and previous_burst.can_merge(now)
            )

            progress_by_key: dict[str, tuple[int, int]] = {}
            valid_incoming: list[str] = []
            for achievement_key in incoming_keys:
                progress = self._achievement_progress_for_key(guild_id, user_id, achievement_key)
                progress_by_key[achievement_key] = progress
                if progress[0] > 0:
                    valid_incoming.append(achievement_key)
            if not valid_incoming:
                return False

            previous_keys = previous_burst.achievement_keys if can_merge and previous_burst is not None else ()
            combined_keys = merge_achievement_keys(previous_keys, valid_incoming)
            for achievement_key in combined_keys:
                progress_by_key.setdefault(
                    achievement_key,
                    self._achievement_progress_for_key(guild_id, user_id, achievement_key),
                )
            combined_keys = tuple(
                sorted(
                    (key for key in combined_keys if progress_by_key[key][0] > 0),
                    key=lambda key: (progress_by_key[key][0], key),
                )
            )
            if not combined_keys:
                return False
            if can_merge and previous_burst is not None and combined_keys == previous_burst.achievement_keys:
                return True

            unlocked_count = max(progress_by_key[key][0] for key in combined_keys)
            total_count = max(progress_by_key[key][1] for key in combined_keys)
            sent_ok, sent_message = await self._dispatch_achievement_notice(
                channel,
                combined_keys,
                f"<@{user_id}>",
                unlocked_count=unlocked_count,
                total_count=total_count,
            )
            if not sent_ok:
                return False
            if sent_message is None:
                return True

            previous_message = previous_burst.message if can_merge and previous_burst is not None else None
            self._achievement_notice_groups[notice_key] = AchievementNoticeBurst(
                achievement_keys=combined_keys,
                started_at=previous_burst.started_at if can_merge and previous_burst is not None else now,
                last_at=time.monotonic(),
                message=sent_message,
            )
            self._ensure_achievement_notice_cleanup_task()
            # Confirma o novo envio antes de apagar o anterior para uma falha de API
            # nunca fazer uma conquista já desbloqueada desaparecer do canal.
            if previous_message is not None and previous_message is not sent_message:
                await self._delete_replaced_achievement_notice(previous_message)
            return True

    async def _send_achievement_notice(
        self,
        channel,
        guild_id: int,
        user_id: int,
        achievement_key: str,
    ) -> bool:
        return await self._send_achievement_notices(
            channel,
            guild_id,
            user_id,
            (achievement_key,),
        )

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

    def _negative_balance_authorized(self, guild_id: int, user_id: int) -> bool:
        getter = getattr(self.db, "get_negative_balance_authorized", None)
        if callable(getter):
            try:
                return bool(getter(int(guild_id), int(user_id)))
            except Exception:
                pass
        doc = getattr(self.db, "user_cache", {}).get((int(guild_id), int(user_id)), {}) or {}
        return bool(doc.get("negative_balance_authorized", False))

    async def _set_negative_balance_authorized(self, guild_id: int, user_id: int, value: bool) -> None:
        setter = getattr(self.db, "set_negative_balance_authorized", None)
        if callable(setter):
            await setter(int(guild_id), int(user_id), bool(value))
            return
        doc = self.db._get_user_doc(int(guild_id), int(user_id))
        doc["negative_balance_authorized"] = bool(value)
        await self.db._save_user_doc(int(guild_id), int(user_id), doc)

    def _negative_transition_note(self, guild_id: int, user_id: int, amount: int) -> str | None:
        if self._negative_balance_authorized(guild_id, user_id):
            return None
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        projected_chips = int(state["projected_chips"])
        if projected_chips >= 0 or projected_chips >= chips:
            return None
        return f"Este jogo deixará seu saldo em **{projected_chips}** {self._CHIP_LOSS_EMOJI}"

    def _needs_negative_confirmation(self, guild_id: int, user_id: int, amount: int) -> bool:
        if self._negative_balance_authorized(guild_id, user_id):
            return False
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        projected_chips = int(state["projected_chips"])
        if projected_chips < -self._MAX_CHIP_DEBT:
            return False
        return projected_chips < 0 and projected_chips < chips

    def _chip_economy_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._chip_economy_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._chip_economy_locks[key] = lock
        return lock

    @asynccontextmanager
    async def _ordered_chip_economy_locks(self, guild_id: int, *user_ids: int):
        """Adquire locks de múltiplas carteiras sempre na mesma ordem."""
        locks = [
            self._chip_economy_lock(guild_id, user_id)
            for user_id in sorted({int(value) for value in user_ids})
        ]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def _negative_debt_gate_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._negative_debt_gate_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._negative_debt_gate_locks[key] = lock
        return lock

    @staticmethod
    async def _delete_negative_gate_message(message: discord.Message | None) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except Exception:
            pass

    async def _show_negative_message_gate(self, key: tuple[int, int], generation: int) -> None:
        try:
            await asyncio.sleep(0.45)
            lock = self._negative_debt_gate_lock(*key)
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is None or int(state.get("generation", -1)) != int(generation):
                    return
                before_show = state.get("before_show")
            if callable(before_show):
                await before_show()
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is None or int(state.get("generation", -1)) != int(generation):
                    return
                amount = int(state["amount"])
                if not self._needs_negative_confirmation(key[0], key[1], amount):
                    future = state.get("future")
                    self._negative_debt_message_gates.pop(key, None)
                    if future is not None and not future.done():
                        future.set_result(True)
                    return
                projected = int(self._negative_cost_projection(key[0], key[1], amount)["projected_chips"])
                view = _NegativeDebtConfirmView(owner_id=key[1], projected_chips=projected)
                state["view"] = view
                channel = state["channel"]
            sent = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            view.message = sent
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is None or int(state.get("generation", -1)) != int(generation):
                    await self._delete_negative_gate_message(sent)
                    return
                state["confirmation_message"] = sent
            await view.wait()
            confirmed = bool(view.confirmed)
            if confirmed and self._needs_negative_confirmation(key[0], key[1], amount):
                await self._set_negative_balance_authorized(key[0], key[1], True)
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is None or int(state.get("generation", -1)) != int(generation):
                    await self._delete_negative_gate_message(sent)
                    return
                future = state.get("future")
                self._negative_debt_message_gates.pop(key, None)
                if future is not None and not future.done():
                    future.set_result(confirmed)
            await self._delete_negative_gate_message(sent)
        except asyncio.CancelledError:
            return
        except Exception:
            lock = self._negative_debt_gate_lock(*key)
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is not None and int(state.get("generation", -1)) == int(generation):
                    future = state.get("future")
                    self._negative_debt_message_gates.pop(key, None)
                    if future is not None and not future.done():
                        future.set_result(False)

    async def _confirm_negative_via_message(
        self,
        channel: discord.abc.Messageable,
        *,
        user_id: int,
        guild_id: int,
        amount: int,
        title: str = "",
        note: str = "",
    ) -> bool:
        if not self._needs_negative_confirmation(guild_id, user_id, amount):
            return True
        projected = int(self._negative_cost_projection(guild_id, user_id, amount)["projected_chips"])
        view = _NegativeDebtConfirmView(owner_id=user_id, projected_chips=projected)
        sent = None
        try:
            sent = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            view.message = sent
            await view.wait()
            confirmed = bool(view.confirmed)
            if confirmed and self._needs_negative_confirmation(guild_id, user_id, amount):
                await self._set_negative_balance_authorized(guild_id, user_id, True)
            return confirmed
        finally:
            await self._delete_negative_gate_message(sent)

    async def _confirm_negative_ephemeral(self, interaction: discord.Interaction, guild_id: int, user_id: int, amount: int, *, title: str = "") -> bool:
        if not self._needs_negative_confirmation(guild_id, user_id, amount):
            return True
        projected = int(self._negative_cost_projection(guild_id, user_id, amount)["projected_chips"])
        view = _NegativeDebtConfirmView(owner_id=user_id, projected_chips=projected)
        sent = None
        try:
            if interaction.response.is_done():
                sent = await interaction.followup.send(view=view, ephemeral=True, wait=True)
            else:
                await interaction.response.send_message(view=view, ephemeral=True)
                try:
                    sent = await interaction.original_response()
                except Exception:
                    sent = None
            view.message = sent
            await view.wait()
            confirmed = bool(view.confirmed)
            if confirmed and self._needs_negative_confirmation(guild_id, user_id, amount):
                await self._set_negative_balance_authorized(guild_id, user_id, True)
            return confirmed
        except Exception:
            channel = getattr(interaction, "channel", None)
            if channel is None:
                return False
            return await self._confirm_negative_via_message(
                channel,
                user_id=user_id,
                guild_id=guild_id,
                amount=amount,
            )

    async def _confirm_negative_from_message(
        self,
        message: discord.Message,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        title: str = "",
        before_show=None,
    ) -> bool:
        if not self._needs_negative_confirmation(guild_id, user_id, amount):
            return True
        key = (int(guild_id), int(user_id))
        lock = self._negative_debt_gate_lock(*key)
        owner = False
        future = None
        old_confirmation = None
        async with lock:
            if not self._needs_negative_confirmation(guild_id, user_id, amount):
                return True
            state = self._negative_debt_message_gates.get(key)
            if state is None:
                owner = True
                future = asyncio.get_running_loop().create_future()
                state = {
                    "future": future,
                    "channel": message.channel,
                    "amount": int(amount),
                    "generation": 1,
                    "confirmation_message": None,
                    "task": None,
                    "before_show": before_show,
                }
                self._negative_debt_message_gates[key] = state
            else:
                future = state.get("future")
                active_view = state.get("view")
                if active_view is not None and bool(getattr(active_view, "confirmed", False)):
                    generation = int(state.get("generation", 1))
                else:
                    state["generation"] = int(state.get("generation", 0)) + 1
                    old_confirmation = state.get("confirmation_message")
                    state["confirmation_message"] = None
                    old_task = state.get("task")
                    if old_task is not None and not old_task.done():
                        old_task.cancel()
                    generation = int(state["generation"])
                    task = asyncio.create_task(self._show_negative_message_gate(key, generation))
                    state["task"] = task
            if owner:
                generation = int(state["generation"])
                task = asyncio.create_task(self._show_negative_message_gate(key, generation))
                state["task"] = task

        # Preserve the trigger that owns the pending negative-balance confirmation.
        # Only duplicate triggers from the same spam burst are discarded so the
        # final confirmation stays associated with the command that caused it.
        if not owner:
            await self._delete_negative_gate_message(message)
        if old_confirmation is not None:
            await self._delete_negative_gate_message(old_confirmation)
        if not owner:
            return False
        try:
            return bool(await future)
        finally:
            async with lock:
                state = self._negative_debt_message_gates.get(key)
                if state is not None and state.get("future") is future and future.done():
                    task = state.get("task")
                    if task is not None and not task.done():
                        task.cancel()
                    self._negative_debt_message_gates.pop(key, None)

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
        temporary_bonus = self._coinflip_temp_bonus_available(guild_id, user_id)
        if chips < 0:
            primary = f"**{chips}** {self._CHIP_LOSS_EMOJI}"
        else:
            primary = f"**{chips}** {self._CHIP_EMOJI}"
        if bonus + temporary_bonus > 0:
            primary += f" • **{bonus + temporary_bonus}** {self._CHIP_BONUS_EMOJI}"
        if temporary_bonus > 0:
            primary += f" _({temporary_bonus} temporárias)_"
        return primary

    def _format_compact_chip_balance(self, guild_id: int, user_id: int) -> str:
        return self._format_primary_chip_balance(guild_id, user_id)

    def _chip_spend_breakdown_text(self, guild_id: int, user_id: int, amount: int) -> str:
        spend = max(0, int(amount))
        temporary = self._coinflip_temp_bonus_available(guild_id, user_id)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        use_temporary = min(temporary, spend)
        use_bonus = min(bonus, spend - use_temporary)
        use_normal = spend - use_temporary - use_bonus
        if use_temporary > 0:
            parts = [f"{self._bonus_chip_amount(use_temporary)} temporárias"]
            if use_bonus > 0:
                parts.append(self._bonus_chip_amount(use_bonus))
            if use_normal > 0:
                parts.append(self._chip_amount(use_normal))
            return f"Você entrou usando {' e '.join(parts)}"
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
        use_temporary = min(self._coinflip_temp_bonus_available(guild_id, user_id), spend)
        use_bonus = min(bonus, spend - use_temporary)
        remaining = spend - use_temporary - use_bonus
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
                    {
                        "key": "forcerob",
                        "emoji": "🥷🏿",
                        "title": "Forcerob",
                        "desc": (
                            "**_forcerob @usuário** · roubo garantido de **5–20 fichas**\n"
                            f"-# Usa {self._CHIP_BONUS_EMOJI} primeiro · 1 uso por dia"
                        ),
                    },
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
                    {
                        "key": "coinflip",
                        "emoji": "🪙",
                        "title": "Coinflip",
                        "desc": (
                            f"**_coinflip** · Coroa: **50** {self._CHIP_BONUS_EMOJI} por **10s** · "
                            f"Cara: **+20** {self._CHIP_BONUS_EMOJI} no próximo jackpot\n"
                            "-# 1 uso por dia"
                        ),
                    },
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
                    {
                        "key": "changefate",
                        "emoji": "🍀",
                        "title": "Change Fate",
                        "desc": (
                            "**_changefate** · recupera o último roubo\n"
                            "Sem roubo pendente, garante o próximo Buckshot ou Truco dourado\n"
                            "-# 1 uso por dia"
                        ),
                    },
                    {
                        "key": "midas",
                        "emoji": self._EFFECT_EMOJI,
                        "title": "Midas",
                        "desc": (
                            "Buckshot e Truco têm "
                            f"**{self._format_percent_text(RACE_SPECIAL_SORTUDO_CHANCE)} de chance** "
                            "de começar dourados"
                        ),
                    },
                    {"key": "premio_extra", "emoji": "🎁", "title": "Prêmio Extra", "desc": f"Seu Daily rende **+5** {self._CHIP_BONUS_EMOJI}\nQuando a ofensiva aumenta o prêmio, você recebe **mais 5** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "bencao", "emoji": "🙏", "title": "Bênção", "desc": "A cada **7h**, você recebe uma jogada grátis\nPode guardar até **2** e usar na Roleta ou em Cartas"},
                    {"key": "wind_boost", "emoji": "🍃", "title": "Wind Boost", "desc": "Na Corrida, cada botão acertado tem **14% de chance** de gerar um impulso, em vez de **9%**"},
                ],
            },
            "coringa": {
                "name": "Coringa",
                "emoji": "🃏",
                "effects": [
                    {
                        "key": "joker",
                        "emoji": "🃏",
                        "title": "Joker",
                        "desc": (
                            "**_joker** · a próxima derrota em **1min** devolve a entrada em "
                            f"{self._CHIP_BONUS_EMOJI}\n-# Até **50 fichas** · 1 uso por dia"
                        ),
                    },
                    {"key": "as", "emoji": "🂡", "title": "Ás", "desc": f"Ao perder um jogo com lobby, você tem **{self._format_percent_text(0.35)} de chance** de recuperar metade da entrada"},
                    {"key": "trapaceiro", "emoji": "🎭", "title": "Trapaceiro", "desc": f"Quando um roubo dá errado, há **{self._format_percent_text(0.25)} de chance** de você não perder nenhuma ficha"},
                    {"key": "redencao", "emoji": "🃏", "title": "Redenção", "desc": f"Ao perder na Roleta ou em Cartas, você tem **{self._format_percent_text(0.5)} de chance** de recuperar metade do custo"},
                ],
            },
            "fenix": {
                "name": "Fênix",
                "emoji": "🐦‍🔥",
                "effects": [
                    {
                        "key": "reborn_skill",
                        "emoji": "🐦‍🔥",
                        "title": "Reborn",
                        "desc": (
                            f"**_reborn** · alterna todo o saldo entre {self._CHIP_EMOJI} "
                            f"e {self._CHIP_BONUS_EMOJI}\n-# Só de dia · cooldown de **6h**"
                        ),
                    },
                    {"key": "sunrise", "emoji": "🔥", "title": "Sunrise", "desc": "Durante o dia, suas duas primeiras derrotas pagas geram Brasas\nVocê pode guardar até **2**"},
                    {"key": "rebirth", "emoji": "❤️‍🔥", "title": "Rebirth", "desc": f"Vença com Brasas guardadas para receber fichas bônus: **1 Brasa** rende **30** {self._CHIP_BONUS_EMOJI}; **2 Brasas** rendem **40** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "second_dawn", "emoji": "🐦‍🔥", "title": "Second Dawn", "desc": f"Uma vez por dia, se uma derrota deixar seu saldo abaixo de **30**, você recebe **30** {self._CHIP_BONUS_EMOJI}"},
                ],
            },
            "glitch": {
                "name": "Glitch",
                "emoji": "👁️⃤",
                "effects": [
                    {
                        "key": "0to1",
                        "emoji": "<a:eyeglitch:1531116300645175436>",
                        "title": "0to1",
                        "desc": (
                            "**_0to1** · inverte a última perda ou bônus do extrato\n"
                            "-# Até **50 fichas** · cada valor vale uma vez"
                        ),
                    },
                    {"key": "desync", "emoji": "<a:eyeglitch:1531116300645175436>", "title": "Desync", "desc": "A cada partida paga, o sistema fica mais instável\nNa terceira, o estado **ERROR** é ativado"},
                    {"key": "overflow", "emoji": "✴️", "title": "Overload", "desc": f"Vença durante o **ERROR** para receber entre **30 e 45** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "rollback", "emoji": "👁️⃤", "title": "Rollback", "desc": "Perca durante o **ERROR** para recuperar **75% da entrada**, com limite de **20 fichas**"},
                    {"key": "memory_leak", "emoji": "🔧", "title": "Bugfix", "desc": "Às vezes, o próximo ciclo começa com **1 fragmento** preservado"},
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
        if effect == "sunrise":
            count = min(2, max(1, int(emoji_count or 1)))
            return "🔥" * count
        overrides = {
            "rebirth": "❤️‍🔥",
            "second_dawn": "🐦‍🔥",
            "desync": "<a:eyeglitch:1531116300645175436>",
            "overflow": "✴️",
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
            "midas": "rodada dourada",
            "premio_extra": f"seu Daily rendeu fichas bônus extras",
            "joker": "entrada devolvida em fichas bônus",
            "as": "você recuperou metade da entrada",
            "redencao": "você recuperou metade do custo",
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
        if not suffix:
            return f"{emoji} **{title}**"
        separator = " · " if effect_key in {"joker", "midas"} else ": "
        return f"{emoji} **{title}**{separator}{suffix}"

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

    def _reset_race_runtime_doc(self, doc: dict, race_key: str) -> None:
        key = str(race_key or "").strip().lower()
        for field in self._RACE_RUNTIME_FIELDS:
            doc.pop(field, None)
        if key == "sortudo":
            doc["race_sortudo_blessing_charges"] = 1
            doc["race_sortudo_blessing_started_at"] = float(time.time())

    def _pick_race_key(self, current: str = "", *, exclude_current: bool = False) -> str:
        choices = list(self._race_catalog().keys())
        current_key = str(current or "").strip().lower()
        if exclude_current and current_key in choices and len(choices) > 1:
            choices.remove(current_key)
        if not choices:
            raise RuntimeError("Nenhuma raça disponível para sorteio")
        return random.choice(choices)

    async def _set_user_race_key(self, guild_id: int, user_id: int, race_key: str | None, *, reset_state: bool = False):
        doc = self.db._get_user_doc(guild_id, user_id)
        key = str(race_key or "").strip().lower()
        unset_fields: list[str] = []
        if key and key in self._race_catalog():
            doc["race_key"] = key
            doc["race_active"] = True
        else:
            doc.pop("race_key", None)
            doc.pop("race_active", None)
            unset_fields.extend(("race_key", "race_active"))
        if reset_state:
            self._race_private_notices.pop((int(guild_id), int(user_id)), None)
            self._changefate_golden_reservations.pop((int(guild_id), int(user_id)), None)
            self._reset_race_runtime_doc(doc, key)
            unset_fields.extend(self._RACE_RUNTIME_FIELDS)
        await self.db._save_user_doc(
            guild_id,
            user_id,
            doc,
            unset_fields=tuple(unset_fields),
        )

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
        current = self._get_user_race_key(guild_id, user_id)
        chosen = self._pick_race_key(current, exclude_current=exclude_current)
        await self._set_user_race_key(guild_id, user_id, chosen, reset_state=True)
        return chosen

    async def _reroll_user_race(
        self,
        guild_id: int,
        user_id: int,
        *,
        cost: int = RACE_REROLL_COST,
    ) -> tuple[bool, str, int]:
        """Cobra fichas normais e troca a raça no mesmo documento persistido."""
        guild_id = int(guild_id)
        user_id = int(user_id)
        reroll_cost = max(0, int(cost))
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                normal_chips = int(self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL) or 0)
                if normal_chips < reroll_cost:
                    return False, "", normal_chips

                current = self._get_user_race_key(guild_id, user_id)
                chosen = self._pick_race_key(current, exclude_current=bool(current))
                previous_doc = self.db._get_user_doc(guild_id, user_id)
                doc = dict(previous_doc)
                doc["chips"] = normal_chips - reroll_cost
                doc["has_chip_activity"] = True
                doc["race_key"] = chosen
                doc["race_active"] = True
                self._reset_race_runtime_doc(doc, chosen)
                self._race_private_notices.pop((guild_id, user_id), None)
                self._changefate_golden_reservations.pop((guild_id, user_id), None)
                try:
                    await self.db._save_user_doc(
                        guild_id,
                        user_id,
                        doc,
                        unset_fields=self._RACE_RUNTIME_FIELDS,
                    )
                except Exception:
                    cache = getattr(self.db, "user_cache", None)
                    if isinstance(cache, dict):
                        cache[(guild_id, user_id)] = previous_doc
                    raise

            try:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=-reroll_cost,
                    kind="chips",
                    reason="Reroll de raça",
                )
            except Exception:
                pass
            return True, chosen, normal_chips - reroll_cost

    def _race_is(self, guild_id: int, user_id: int, race_key: str) -> bool:
        return self._get_user_race_key(guild_id, user_id) == str(race_key or "").strip().lower() and self._is_user_race_active(guild_id, user_id)

    def _race_progress_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._race_progress_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._race_progress_locks[key] = lock
        return lock

    def _race_skill_daily_key(self) -> str:
        return self._race_now().date().isoformat()

    def _race_skill_daily_used(self, doc: dict, skill_key: str) -> bool:
        uses = dict(doc.get("race_skill_daily_last_use") or {})
        return str(uses.get(str(skill_key), "") or "") == self._race_skill_daily_key()

    def _mark_race_skill_daily_used(self, doc: dict, skill_key: str) -> None:
        uses = dict(doc.get("race_skill_daily_last_use") or {})
        uses[str(skill_key)] = self._race_skill_daily_key()
        doc["race_skill_daily_last_use"] = uses

    def _race_skill_daily_wait_text(self) -> str:
        return self._format_daily_reset_remaining(self._daily_reset_remaining_seconds())

    async def _activate_coinflip_skill(self, guild_id: int, user_id: int) -> dict[str, object]:
        guild_id, user_id = int(guild_id), int(user_id)
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                if not self._race_is(guild_id, user_id, "apostador"):
                    return {"ok": False, "code": "race"}
                if self._race_skill_daily_used(doc, "coinflip"):
                    return {"ok": False, "code": "cooldown"}
                now = float(time.time())
                result = random.choice(("cara", "coroa"))
                self._mark_race_skill_daily_used(doc, "coinflip")
                if result == "coroa":
                    doc["race_skill_coinflip_temp_bonus"] = RACE_SKILL_COINFLIP_BONUS
                    doc["race_skill_coinflip_temp_expires_at"] = now + RACE_SKILL_COINFLIP_SECONDS
                    doc.pop("race_skill_coinflip_jackpot_bonus", None)
                    doc.pop("race_skill_coinflip_jackpot_expires_at", None)
                else:
                    doc["race_skill_coinflip_jackpot_bonus"] = RACE_SKILL_JACKPOT_BONUS
                    doc["race_skill_coinflip_jackpot_expires_at"] = now + self._daily_reset_remaining_seconds()
                    doc.pop("race_skill_coinflip_temp_bonus", None)
                    doc.pop("race_skill_coinflip_temp_expires_at", None)
                await self.db._save_user_doc(
                    guild_id,
                    user_id,
                    doc,
                    unset_fields=(
                        "race_skill_coinflip_temp_bonus",
                        "race_skill_coinflip_temp_expires_at",
                        "race_skill_coinflip_jackpot_bonus",
                        "race_skill_coinflip_jackpot_expires_at",
                    ),
                )
                return {"ok": True, "result": result}

    async def _activate_joker_skill(self, guild_id: int, user_id: int) -> dict[str, object]:
        guild_id, user_id = int(guild_id), int(user_id)
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                if not self._race_is(guild_id, user_id, "coringa"):
                    return {"ok": False, "code": "race"}
                if self._race_skill_daily_used(doc, "joker"):
                    return {"ok": False, "code": "cooldown"}
                self._mark_race_skill_daily_used(doc, "joker")
                doc["race_skill_joker_until"] = float(time.time()) + RACE_SKILL_JOKER_SECONDS
                await self.db._save_user_doc(guild_id, user_id, doc)
                return {"ok": True, "expires_in": RACE_SKILL_JOKER_SECONDS}

    def _changefate_golden_is_armed(self, guild_id: int, user_id: int) -> bool:
        if not self._race_is(guild_id, user_id, "sortudo"):
            return False
        if (int(guild_id), int(user_id)) in self._changefate_golden_reservations:
            return False
        doc = self.db._get_user_doc(guild_id, user_id)
        return float(doc.get("race_skill_changefate_golden_until", 0.0) or 0.0) > time.time()

    async def _reserve_changefate_golden(
        self,
        guild_id: int,
        user_id: int,
        reservation_token: str,
    ) -> bool:
        guild_id, user_id = int(guild_id), int(user_id)
        key = (guild_id, user_id)
        token = str(reservation_token or "").strip()
        if not token:
            return False
        async with self._race_progress_lock(guild_id, user_id):
            if key in self._changefate_golden_reservations:
                return False
            doc = self.db._get_user_doc(guild_id, user_id)
            if (
                not self._race_is(guild_id, user_id, "sortudo")
                or float(doc.get("race_skill_changefate_golden_until", 0.0) or 0.0) <= time.time()
            ):
                return False
            self._changefate_golden_reservations[key] = token
            return True

    def _release_changefate_golden_reservation(
        self,
        guild_id: int,
        user_id: int,
        reservation_token: str,
    ) -> None:
        key = (int(guild_id), int(user_id))
        if self._changefate_golden_reservations.get(key) == str(reservation_token or ""):
            self._changefate_golden_reservations.pop(key, None)

    async def _consume_changefate_golden(
        self,
        guild_id: int,
        user_id: int,
        *,
        reservation_token: str | None = None,
    ) -> bool:
        guild_id, user_id = int(guild_id), int(user_id)
        key = (guild_id, user_id)
        expected_token = str(reservation_token or "").strip()
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                reserved_token = self._changefate_golden_reservations.get(key, "")
                if expected_token and reserved_token != expected_token:
                    return False
                if not expected_token and reserved_token:
                    return False
                if (
                    not self._race_is(guild_id, user_id, "sortudo")
                    or float(doc.get("race_skill_changefate_golden_until", 0.0) or 0.0) <= time.time()
                ):
                    if expected_token and reserved_token == expected_token:
                        self._changefate_golden_reservations.pop(key, None)
                    return False
                doc.pop("race_skill_changefate_golden_until", None)
                self._changefate_golden_reservations.pop(key, None)
                await self.db._save_user_doc(
                    guild_id,
                    user_id,
                    doc,
                    unset_fields=("race_skill_changefate_golden_until",),
                )
                return True

    async def _restore_changefate_golden(self, guild_id: int, user_id: int) -> bool:
        """Restaura a garantia quando uma rodada falha antes de começar."""
        guild_id, user_id = int(guild_id), int(user_id)
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                if not self._race_is(guild_id, user_id, "sortudo"):
                    return False
                self._changefate_golden_reservations.pop((guild_id, user_id), None)
                doc["race_skill_changefate_golden_until"] = float(time.time()) + self._daily_reset_remaining_seconds()
                await self.db._save_user_doc(guild_id, user_id, doc)
                return True

    async def _claim_coinflip_jackpot_bonus(self, guild_id: int, user_id: int) -> int:
        guild_id, user_id = int(guild_id), int(user_id)
        awarded = 0
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                if (
                    self._race_is(guild_id, user_id, "apostador")
                    and float(doc.get("race_skill_coinflip_jackpot_expires_at", 0.0) or 0.0) > time.time()
                ):
                    awarded = max(0, int(doc.get("race_skill_coinflip_jackpot_bonus", 0) or 0))
                doc.pop("race_skill_coinflip_jackpot_bonus", None)
                doc.pop("race_skill_coinflip_jackpot_expires_at", None)
                if awarded > 0:
                    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + awarded
                    doc["has_chip_activity"] = True
                await self.db._save_user_doc(
                    guild_id,
                    user_id,
                    doc,
                    unset_fields=(
                        "race_skill_coinflip_jackpot_bonus",
                        "race_skill_coinflip_jackpot_expires_at",
                    ),
                )
        if awarded > 0:
            try:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=awarded,
                    kind="bonus",
                    reason="Coinflip · jackpot",
                    event_type="race_skill",
                    skill_eligible=False,
                )
            except Exception:
                pass
        return awarded

    def _reborn_skill_preview(self, guild_id: int, user_id: int) -> dict[str, object]:
        doc = self.db._get_user_doc(guild_id, user_id)
        mode = str(doc.get("race_skill_reborn_next_mode", "normal_to_bonus") or "normal_to_bonus")
        if mode not in {"normal_to_bonus", "bonus_to_normal"}:
            mode = "normal_to_bonus"
        used_at = float(doc.get("race_skill_reborn_used_at", 0.0) or 0.0)
        remaining = max(0.0, used_at + RACE_SKILL_REBORN_COOLDOWN_SECONDS - time.time())
        amount = (
            max(0, int(doc.get("bonus_chips", 0) or 0))
            if mode == "bonus_to_normal"
            else max(0, int(doc.get("chips", CHIPS_INITIAL) or 0))
        )
        period, _period_key = self._race_period_info()
        return {
            "mode": mode,
            "amount": amount,
            "remaining": remaining,
            "daytime": period == "day",
        }

    async def _execute_reborn_skill(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_mode: str,
        expected_amount: int,
    ) -> dict[str, object]:
        guild_id, user_id = int(guild_id), int(user_id)
        history: list[tuple[int, str]] = []
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                if not self._race_is(guild_id, user_id, "fenix"):
                    return {"ok": False, "code": "race"}
                period, _period_key = self._race_period_info()
                if period != "day":
                    return {"ok": False, "code": "night"}
                used_at = float(doc.get("race_skill_reborn_used_at", 0.0) or 0.0)
                remaining = max(0.0, used_at + RACE_SKILL_REBORN_COOLDOWN_SECONDS - time.time())
                if remaining > 0:
                    return {"ok": False, "code": "cooldown", "remaining": remaining}
                mode = str(doc.get("race_skill_reborn_next_mode", "normal_to_bonus") or "normal_to_bonus")
                if mode not in {"normal_to_bonus", "bonus_to_normal"}:
                    mode = "normal_to_bonus"
                amount = (
                    max(0, int(doc.get("bonus_chips", 0) or 0))
                    if mode == "bonus_to_normal"
                    else max(0, int(doc.get("chips", CHIPS_INITIAL) or 0))
                )
                if mode != str(expected_mode) or amount != max(0, int(expected_amount)):
                    return {"ok": False, "code": "changed", "mode": mode, "amount": amount}
                if amount <= 0:
                    return {"ok": False, "code": "empty", "mode": mode}
                if mode == "normal_to_bonus":
                    doc["chips"] = int(doc.get("chips", CHIPS_INITIAL) or 0) - amount
                    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + amount
                    doc["race_skill_reborn_next_mode"] = "bonus_to_normal"
                    history = [(-amount, "chips"), (amount, "bonus")]
                else:
                    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) - amount
                    doc["chips"] = int(doc.get("chips", CHIPS_INITIAL) or 0) + amount
                    doc["race_skill_reborn_next_mode"] = "normal_to_bonus"
                    history = [(-amount, "bonus"), (amount, "chips")]
                doc["race_skill_reborn_used_at"] = float(time.time())
                doc["has_chip_activity"] = True
                await self.db._save_user_doc(guild_id, user_id, doc)

        for delta, kind in history:
            try:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=delta,
                    kind=kind,
                    reason="Reborn · conversão",
                    event_type="race_skill",
                    skill_eligible=False,
                )
            except Exception:
                pass
        return {"ok": True, "mode": expected_mode, "amount": max(0, int(expected_amount))}

    async def _execute_ordinary_robbery_transfer(
        self,
        guild_id: int,
        thief_id: int,
        victim_id: int,
        amount: int,
        *,
        thief_name: str,
        victim_name: str,
    ) -> dict[str, object]:
        """Move um roubo comum e cria o vínculo usado pelo Change Fate/0to1."""
        guild_id, thief_id, victim_id = int(guild_id), int(thief_id), int(victim_id)
        amount = max(0, int(amount))
        if amount <= 0 or thief_id == victim_id:
            return {"ok": False}
        event_id = f"robbery:{time.time_ns()}"
        event_ts = float(time.time())
        normal_taken = bonus_taken = 0
        async with self._ordered_chip_economy_locks(guild_id, thief_id, victim_id):
            old_thief = self.db._get_user_doc(guild_id, thief_id)
            old_victim = self.db._get_user_doc(guild_id, victim_id)
            thief_doc = dict(old_thief)
            victim_doc = dict(old_victim)
            victim_bonus = max(0, int(victim_doc.get("bonus_chips", 0) or 0))
            victim_normal = int(victim_doc.get("chips", CHIPS_INITIAL) or 0)
            bonus_taken = min(victim_bonus, amount)
            normal_taken = amount - bonus_taken
            if normal_taken > max(0, victim_normal):
                return {"ok": False}

            victim_doc["bonus_chips"] = victim_bonus - bonus_taken
            victim_doc["chips"] = victim_normal - normal_taken
            victim_doc["has_chip_activity"] = True
            thief_doc["chips"] = int(thief_doc.get("chips", CHIPS_INITIAL) or 0) + amount
            thief_doc["has_chip_activity"] = True
            robberies = list(victim_doc.get("race_ordinary_robberies", []) or [])
            robberies.append(
                {
                    "event_id": event_id,
                    "ts": event_ts,
                    "thief_id": thief_id,
                    "amount": amount,
                    "normal": normal_taken,
                    "bonus": bonus_taken,
                    "resolved": False,
                }
            )
            victim_doc["race_ordinary_robberies"] = robberies[-10:]
            try:
                await self.db._save_user_doc(guild_id, victim_id, victim_doc)
                await self.db._save_user_doc(guild_id, thief_id, thief_doc)
            except Exception:
                try:
                    await self.db._save_user_doc(guild_id, victim_id, old_victim)
                    await self.db._save_user_doc(guild_id, thief_id, old_thief)
                except Exception:
                    pass
                raise

        metadata_victim = {
            "event_type": "ordinary_robbery",
            "event_id": event_id,
            "other_user_id": thief_id,
            "skill_eligible": True,
        }
        metadata_thief = {
            "event_type": "ordinary_robbery",
            "event_id": event_id,
            "other_user_id": victim_id,
            "skill_eligible": True,
        }
        try:
            await self.db.append_chip_history(
                guild_id,
                victim_id,
                delta=-amount,
                kind=(
                    "bonus"
                    if bonus_taken == amount
                    else "chips"
                    if normal_taken == amount
                    else "mixed"
                ),
                reason=f"Roubado por {thief_name}",
                ts=event_ts,
                normal_delta=-normal_taken,
                bonus_delta=-bonus_taken,
                **metadata_victim,
            )
            await self.db.append_chip_history(
                guild_id,
                thief_id,
                delta=amount,
                kind="chips",
                reason=f"Roubo bem-sucedido em {victim_name}",
                ts=event_ts,
                **metadata_thief,
            )
        except Exception:
            pass
        return {
            "ok": True,
            "event_id": event_id,
            "amount": amount,
            "normal": normal_taken,
            "bonus": bonus_taken,
        }

    @staticmethod
    def _race_skill_0to1_entry(doc: dict) -> dict | None:
        cutoff = float(doc.get("race_skill_0to1_cutoff_ts", 0.0) or 0.0)
        resolved_robberies = {
            str(event.get("event_id", "") or "")
            for event in list(doc.get("race_ordinary_robberies", []) or [])
            if bool(event.get("resolved", False))
        }
        blocked_types = {"admin", "reset", "race_skill", "forced_robbery"}
        blocked_reason_parts = (
            "reroll",
            "reset",
            "admin",
            "0to1",
            "reborn",
            "coinflip",
            "change fate",
            "changefate",
            "joker",
            "roubo forçado",
        )
        history = list(doc.get("chip_history", []) or [])
        for entry in reversed(history):
            try:
                ts = float(entry.get("ts", 0.0) or 0.0)
                delta = int(entry.get("delta", 0) or 0)
            except (TypeError, ValueError):
                continue
            kind = str(entry.get("kind", "chips") or "chips").strip().lower()
            event_type = str(entry.get("event_type", "") or "").strip().lower()
            reason = str(entry.get("reason", "") or "").strip().lower()
            if ts <= cutoff or entry.get("skill_eligible") is False:
                continue
            if event_type in blocked_types or any(part in reason for part in blocked_reason_parts):
                continue
            if event_type == "ordinary_robbery" and str(entry.get("event_id", "") or "") in resolved_robberies:
                continue
            if delta < 0 or (kind == "bonus" and delta > 0):
                return dict(entry)
        return None

    async def _execute_0to1_skill(self, guild_id: int, user_id: int) -> dict[str, object]:
        guild_id, user_id = int(guild_id), int(user_id)
        result: dict[str, object] = {"ok": False, "code": "empty"}
        history_rows: list[tuple[int, int, str, str]] = []
        async with self._race_progress_lock(guild_id, user_id):
            initial_doc = self.db._get_user_doc(guild_id, user_id)
            if not self._race_is(guild_id, user_id, "glitch"):
                return {"ok": False, "code": "race"}
            initial_entry = self._race_skill_0to1_entry(initial_doc)
            if initial_entry is None:
                return result
            linked_user_id = 0
            if (
                str(initial_entry.get("event_type", "") or "") == "ordinary_robbery"
                and int(initial_entry.get("delta", 0) or 0) < 0
            ):
                linked_user_id = int(initial_entry.get("other_user_id", 0) or 0)
            lock_ids = (user_id, linked_user_id) if linked_user_id > 0 and linked_user_id != user_id else (user_id,)
            async with self._ordered_chip_economy_locks(guild_id, *lock_ids):
                old_user = self.db._get_user_doc(guild_id, user_id)
                user_doc = dict(old_user)
                entry = self._race_skill_0to1_entry(user_doc)
                if entry is None:
                    return result
                delta = int(entry.get("delta", 0) or 0)
                kind = str(entry.get("kind", "chips") or "chips").strip().lower()
                source_amount = abs(delta) if delta < 0 else delta
                amount = min(RACE_SKILL_0TO1_LIMIT, max(0, source_amount))
                if delta > 0 and kind == "bonus":
                    amount = min(amount, max(0, int(user_doc.get("bonus_chips", 0) or 0)))
                user_doc["race_skill_0to1_cutoff_ts"] = float(entry.get("ts", time.time()) or time.time())
                user_doc["race_skill_0to1_last_entry_id"] = str(entry.get("entry_id", "") or "")
                if amount <= 0:
                    await self.db._save_user_doc(guild_id, user_id, user_doc)
                    return {"ok": False, "code": "empty_balance"}

                if delta > 0 and kind == "bonus":
                    user_doc["bonus_chips"] = max(0, int(user_doc.get("bonus_chips", 0) or 0)) - amount
                    history_rows.append((user_id, -amount, "bonus", "0to1 · conversão"))
                user_doc["chips"] = int(user_doc.get("chips", CHIPS_INITIAL) or 0) + amount
                user_doc["has_chip_activity"] = True
                history_rows.append((user_id, amount, "chips", "0to1 · conversão"))

                if delta < 0 and str(entry.get("event_type", "") or "") == "ordinary_robbery":
                    source_event_id = str(entry.get("event_id", "") or "")
                    robberies = list(user_doc.get("race_ordinary_robberies", []) or [])
                    for index, robbery in enumerate(robberies):
                        if str(robbery.get("event_id", "") or "") != source_event_id:
                            continue
                        updated_robbery = dict(robbery)
                        updated_robbery["resolved"] = True
                        updated_robbery["resolved_at"] = float(time.time())
                        updated_robbery["resolved_by"] = "0to1"
                        robberies[index] = updated_robbery
                        user_doc["race_ordinary_robberies"] = robberies
                        break

                linked_doc = None
                old_linked = None
                actual_linked_id = 0
                if (
                    delta < 0
                    and str(entry.get("event_type", "") or "") == "ordinary_robbery"
                ):
                    candidate = int(entry.get("other_user_id", 0) or 0)
                    if candidate in lock_ids and candidate != user_id:
                        actual_linked_id = candidate
                        old_linked = self.db._get_user_doc(guild_id, candidate)
                        linked_doc = dict(old_linked)
                        linked_doc["chips"] = int(linked_doc.get("chips", CHIPS_INITIAL) or 0) - amount
                        linked_doc["has_chip_activity"] = True
                        history_rows.append((candidate, -amount, "chips", "0to1 · roubo revertido"))
                try:
                    await self.db._save_user_doc(guild_id, user_id, user_doc)
                    if linked_doc is not None and actual_linked_id > 0:
                        await self.db._save_user_doc(guild_id, actual_linked_id, linked_doc)
                except Exception:
                    try:
                        await self.db._save_user_doc(guild_id, user_id, old_user)
                        if old_linked is not None and actual_linked_id > 0:
                            await self.db._save_user_doc(guild_id, actual_linked_id, old_linked)
                    except Exception:
                        pass
                    raise
                result = {
                    "ok": True,
                    "amount": amount,
                    "source_delta": delta,
                    "source_kind": kind,
                    "linked_user_id": actual_linked_id,
                }

        for target_id, delta, kind, reason in history_rows:
            try:
                await self.db.append_chip_history(
                    guild_id,
                    target_id,
                    delta=delta,
                    kind=kind,
                    reason=reason,
                    event_type="race_skill",
                    other_user_id=(user_id if target_id != user_id else None),
                    skill_eligible=False,
                )
            except Exception:
                pass
        return result

    @staticmethod
    def _recent_unresolved_robbery(doc: dict, *, now: float, max_age: float = 24 * 60 * 60) -> dict | None:
        for event in reversed(list(doc.get("race_ordinary_robberies", []) or [])):
            try:
                event_ts = float(event.get("ts", 0.0) or 0.0)
                amount = int(event.get("amount", 0) or 0)
                thief_id = int(event.get("thief_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if (
                not bool(event.get("resolved", False))
                and amount > 0
                and thief_id > 0
                and 0.0 <= now - event_ts <= max_age
            ):
                return dict(event)
        return None

    async def _execute_changefate_skill(self, guild_id: int, user_id: int) -> dict[str, object]:
        guild_id, user_id = int(guild_id), int(user_id)
        now = float(time.time())
        history_rows: list[tuple[int, int, str, str, int | None]] = []
        result: dict[str, object]
        async with self._race_progress_lock(guild_id, user_id):
            initial_doc = self.db._get_user_doc(guild_id, user_id)
            if not self._race_is(guild_id, user_id, "sortudo"):
                return {"ok": False, "code": "race"}
            if self._race_skill_daily_used(initial_doc, "changefate"):
                return {"ok": False, "code": "cooldown"}
            event = self._recent_unresolved_robbery(initial_doc, now=now)

            if event is None:
                async with self._chip_economy_lock(guild_id, user_id):
                    doc = self.db._get_user_doc(guild_id, user_id)
                    if not self._race_is(guild_id, user_id, "sortudo"):
                        return {"ok": False, "code": "race"}
                    if self._race_skill_daily_used(doc, "changefate"):
                        return {"ok": False, "code": "cooldown"}
                    self._mark_race_skill_daily_used(doc, "changefate")
                    doc["race_skill_changefate_golden_until"] = now + self._daily_reset_remaining_seconds()
                    await self.db._save_user_doc(guild_id, user_id, doc)
                return {"ok": True, "mode": "golden"}

            thief_id = int(event.get("thief_id", 0) or 0)
            event_id = str(event.get("event_id", "") or "")
            async with self._ordered_chip_economy_locks(guild_id, user_id, thief_id):
                old_user = self.db._get_user_doc(guild_id, user_id)
                old_thief = self.db._get_user_doc(guild_id, thief_id)
                user_doc = dict(old_user)
                thief_doc = dict(old_thief)
                if not self._race_is(guild_id, user_id, "sortudo"):
                    return {"ok": False, "code": "race"}
                if self._race_skill_daily_used(user_doc, "changefate"):
                    return {"ok": False, "code": "cooldown"}
                robberies = list(user_doc.get("race_ordinary_robberies", []) or [])
                event_index = next(
                    (
                        index
                        for index, current in enumerate(robberies)
                        if str(current.get("event_id", "") or "") == event_id
                        and not bool(current.get("resolved", False))
                    ),
                    None,
                )
                if event_index is None:
                    return {"ok": False, "code": "changed"}
                current_event = dict(robberies[event_index])
                amount = max(0, int(current_event.get("amount", 0) or 0))
                normal_amount = max(0, int(current_event.get("normal", 0) or 0))
                bonus_amount = max(0, int(current_event.get("bonus", 0) or 0))
                if amount <= 0 or normal_amount + bonus_amount != amount:
                    return {"ok": False, "code": "changed"}

                user_doc["chips"] = int(user_doc.get("chips", CHIPS_INITIAL) or 0) + normal_amount
                user_doc["bonus_chips"] = max(0, int(user_doc.get("bonus_chips", 0) or 0)) + bonus_amount
                user_doc["has_chip_activity"] = True
                thief_doc["chips"] = int(thief_doc.get("chips", CHIPS_INITIAL) or 0) - amount - 10
                thief_doc["has_chip_activity"] = True
                current_event["resolved"] = True
                current_event["resolved_at"] = now
                current_event["resolved_by"] = "changefate"
                robberies[event_index] = current_event
                user_doc["race_ordinary_robberies"] = robberies
                user_doc["race_skill_0to1_cutoff_ts"] = max(
                    float(user_doc.get("race_skill_0to1_cutoff_ts", 0.0) or 0.0),
                    float(current_event.get("ts", 0.0) or 0.0),
                )
                self._mark_race_skill_daily_used(user_doc, "changefate")
                try:
                    await self.db._save_user_doc(guild_id, user_id, user_doc)
                    await self.db._save_user_doc(guild_id, thief_id, thief_doc)
                except Exception:
                    try:
                        await self.db._save_user_doc(guild_id, user_id, old_user)
                        await self.db._save_user_doc(guild_id, thief_id, old_thief)
                    except Exception:
                        pass
                    raise
                if normal_amount > 0:
                    history_rows.append((user_id, normal_amount, "chips", "Change Fate · devolução", thief_id))
                if bonus_amount > 0:
                    history_rows.append((user_id, bonus_amount, "bonus", "Change Fate · devolução", thief_id))
                history_rows.append((thief_id, -(amount + 10), "chips", "Change Fate · polícia", user_id))
                result = {
                    "ok": True,
                    "mode": "police",
                    "amount": amount,
                    "penalty": 10,
                    "thief_id": thief_id,
                    "normal": normal_amount,
                    "bonus": bonus_amount,
                }

        for target_id, delta, kind, reason, other_id in history_rows:
            try:
                await self.db.append_chip_history(
                    guild_id,
                    target_id,
                    delta=delta,
                    kind=kind,
                    reason=reason,
                    event_type="race_skill",
                    event_id=event_id,
                    other_user_id=other_id,
                    skill_eligible=False,
                )
            except Exception:
                pass
        return result

    async def _execute_forcerob_skill(
        self,
        guild_id: int,
        user_id: int,
        target_id: int,
        *,
        user_name: str,
        target_name: str,
    ) -> dict[str, object]:
        guild_id, user_id, target_id = int(guild_id), int(user_id), int(target_id)
        if user_id == target_id:
            return {"ok": False, "code": "self"}
        event_id = f"forcerob:{time.time_ns()}"
        event_ts = float(time.time())
        async with self._race_progress_lock(guild_id, user_id):
            initial_doc = self.db._get_user_doc(guild_id, user_id)
            if not self._race_is(guild_id, user_id, "preto"):
                return {"ok": False, "code": "race"}
            if self._race_skill_daily_used(initial_doc, "forcerob"):
                return {"ok": False, "code": "cooldown"}
            async with self._ordered_chip_economy_locks(guild_id, user_id, target_id):
                old_user = self.db._get_user_doc(guild_id, user_id)
                old_target = self.db._get_user_doc(guild_id, target_id)
                user_doc = dict(old_user)
                target_doc = dict(old_target)
                if not self._race_is(guild_id, user_id, "preto"):
                    return {"ok": False, "code": "race"}
                if self._race_skill_daily_used(user_doc, "forcerob"):
                    return {"ok": False, "code": "cooldown"}
                target_bonus = max(0, int(target_doc.get("bonus_chips", 0) or 0))
                target_normal = max(0, int(target_doc.get("chips", CHIPS_INITIAL) or 0))
                available = target_bonus + target_normal
                if available < 20:
                    return {"ok": False, "code": "poor", "available": available}
                amount = random.randint(5, min(20, available))
                bonus_taken = min(target_bonus, amount)
                normal_taken = amount - bonus_taken
                target_doc["bonus_chips"] = target_bonus - bonus_taken
                target_doc["chips"] = int(target_doc.get("chips", CHIPS_INITIAL) or 0) - normal_taken
                target_doc["has_chip_activity"] = True
                user_doc["bonus_chips"] = max(0, int(user_doc.get("bonus_chips", 0) or 0)) + bonus_taken
                user_doc["chips"] = int(user_doc.get("chips", CHIPS_INITIAL) or 0) + normal_taken
                user_doc["has_chip_activity"] = True
                self._mark_race_skill_daily_used(user_doc, "forcerob")
                try:
                    await self.db._save_user_doc(guild_id, target_id, target_doc)
                    await self.db._save_user_doc(guild_id, user_id, user_doc)
                except Exception:
                    try:
                        await self.db._save_user_doc(guild_id, target_id, old_target)
                        await self.db._save_user_doc(guild_id, user_id, old_user)
                    except Exception:
                        pass
                    raise

        metadata_user = {
            "event_type": "forced_robbery",
            "event_id": event_id,
            "other_user_id": target_id,
            "skill_eligible": False,
        }
        metadata_target = {
            "event_type": "forced_robbery",
            "event_id": event_id,
            "other_user_id": user_id,
            "skill_eligible": False,
        }
        try:
            if bonus_taken > 0:
                await self.db.append_chip_history(
                    guild_id,
                    target_id,
                    delta=-bonus_taken,
                    kind="bonus",
                    reason=f"Forcerob · roubado por {user_name}",
                    ts=event_ts,
                    **metadata_target,
                )
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=bonus_taken,
                    kind="bonus",
                    reason=f"Forcerob · roubo em {target_name}",
                    ts=event_ts,
                    **metadata_user,
                )
            if normal_taken > 0:
                await self.db.append_chip_history(
                    guild_id,
                    target_id,
                    delta=-normal_taken,
                    kind="chips",
                    reason=f"Forcerob · roubado por {user_name}",
                    ts=event_ts,
                    **metadata_target,
                )
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=normal_taken,
                    kind="chips",
                    reason=f"Forcerob · roubo em {target_name}",
                    ts=event_ts,
                    **metadata_user,
                )
        except Exception:
            pass
        return {
            "ok": True,
            "amount": amount,
            "normal": normal_taken,
            "bonus": bonus_taken,
            "event_id": event_id,
        }

    def _race_panel_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        key = (int(guild_id), int(user_id))
        lock = self._race_panel_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._race_panel_locks[key] = lock
        return lock

    def _new_race_reroll_confirmation(self, guild_id: int, user_id: int) -> int:
        key = (int(guild_id), int(user_id))
        token = int(self._race_reroll_confirmation_versions.get(key, 0)) + 1
        self._race_reroll_confirmation_versions[key] = token
        return token

    def _race_reroll_confirmation_is_current(
        self,
        guild_id: int,
        user_id: int,
        token: int,
    ) -> bool:
        key = (int(guild_id), int(user_id))
        return int(self._race_reroll_confirmation_versions.get(key, 0)) == int(token)

    def _invalidate_race_reroll_confirmation(
        self,
        guild_id: int,
        user_id: int,
        *,
        token: int | None = None,
    ) -> None:
        key = (int(guild_id), int(user_id))
        current = int(self._race_reroll_confirmation_versions.get(key, 0))
        if token is not None and current != int(token):
            return
        self._race_reroll_confirmation_versions[key] = current + 1

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
        if race_key not in {"fenix", "glitch"} or not self._is_user_race_active(guild_id, user_id):
            return ""
        doc = self.db._get_user_doc(guild_id, user_id)
        state = dict((doc.get("race_state") or {}).get(race_key) or {})
        period, period_key = self._race_period_info()
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
        temporary_used = min(self._coinflip_temp_bonus_available(guild_id, user_id), spend)
        source_doc = self.db._get_user_doc(guild_id, user_id)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        bonus_used = min(bonus, spend - temporary_used)
        period, period_key = self._race_period_info()
        return {
            "bonus": bonus_used + temporary_used,
            "chips": spend - bonus_used - temporary_used,
            "temporary_bonus": temporary_used,
            "_temporary_bonus_expires_at": (
                float(source_doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
                if temporary_used > 0
                else 0.0
            ),
            "_race_period": period,
            "_race_period_key": period_key,
        }

    def _normalize_entry_spend(self, entry_spend: dict | None) -> dict[str, int]:
        raw = entry_spend or {}
        return {
            "chips": max(0, int(raw.get("chips", 0) or 0)),
            "bonus": max(0, int(raw.get("bonus", 0) or 0)),
            "temporary_bonus": max(0, int(raw.get("temporary_bonus", 0) or 0)),
        }

    async def _consume_coinflip_temporary_amount(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> int:
        requested = max(0, int(amount))
        if requested <= 0:
            return 0
        async with self._chip_economy_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            expires_at = float(doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
            available = (
                max(0, int(doc.get("race_skill_coinflip_temp_bonus", 0) or 0))
                if expires_at > time.time()
                else 0
            )
            consumed = min(requested, available)
            remaining = available - consumed
            if remaining > 0:
                doc["race_skill_coinflip_temp_bonus"] = remaining
            else:
                doc.pop("race_skill_coinflip_temp_bonus", None)
                doc.pop("race_skill_coinflip_temp_expires_at", None)
            await self.db._save_user_doc(
                guild_id,
                user_id,
                doc,
                unset_fields=(
                    "race_skill_coinflip_temp_bonus",
                    "race_skill_coinflip_temp_expires_at",
                ),
            )
            return consumed

    async def _restore_coinflip_temporary_amount(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        expires_at: float,
    ) -> int:
        restore = max(0, int(amount))
        original_expiry = float(expires_at or 0.0)
        if restore <= 0 or original_expiry <= time.time():
            return 0
        async with self._chip_economy_lock(guild_id, user_id):
            doc = self.db._get_user_doc(guild_id, user_id)
            current_expiry = float(doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
            current_amount = (
                max(0, int(doc.get("race_skill_coinflip_temp_bonus", 0) or 0))
                if current_expiry > time.time()
                else 0
            )
            doc["race_skill_coinflip_temp_bonus"] = current_amount + restore
            doc["race_skill_coinflip_temp_expires_at"] = max(current_expiry, original_expiry)
            await self.db._save_user_doc(guild_id, user_id, doc)
        return restore

    async def _refund_entry_spend(
        self,
        guild_id: int,
        user_id: int,
        entry_spend: dict | None,
        *,
        reason: str,
    ) -> None:
        raw = dict(entry_spend or {})
        spend = self._normalize_entry_spend(raw)
        temporary = min(spend["bonus"], spend["temporary_bonus"])
        persistent_bonus = spend["bonus"] - temporary
        if persistent_bonus > 0:
            await self._change_user_bonus_chips(
                guild_id,
                user_id,
                persistent_bonus,
                reason=reason,
            )
        if spend["chips"] > 0:
            await self._change_user_chips(
                guild_id,
                user_id,
                spend["chips"],
                reason=reason,
            )
        if temporary > 0:
            await self._restore_coinflip_temporary_amount(
                guild_id,
                user_id,
                temporary,
                expires_at=float(raw.get("_temporary_bonus_expires_at", 0.0) or 0.0),
            )

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
        valid: bool = True,
        glitch_progress: bool = True,
    ) -> list[str]:
        """Aplica progressão das raças novas sem interferir na resolução do jogo."""
        try:
            race_key = self._get_user_race_key(guild_id, user_id)
            if race_key not in {"fenix", "glitch"} or not self._is_user_race_active(guild_id, user_id):
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

            async with self._race_progress_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                state_root = dict(doc.get("race_state") or {})
                state = dict(state_root.get(race_key) or {})
                notes: list[str] = []
                normal_delta = 0
                bonus_delta = 0
                normal_reason = ""
                bonus_reason = ""

                if race_key == "fenix":
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
                                bonus_reason = "Overload do Glitch"
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
                    old_normal_chips = int(doc.get("chips", CHIPS_INITIAL) or 0)
                    new_normal_chips = old_normal_chips + normal_delta
                    doc["chips"] = new_normal_chips
                    if old_normal_chips < 0 <= new_normal_chips:
                        doc["negative_balance_authorized"] = False
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

    async def _maybe_apply_coringa_loss_refund(
        self,
        guild_id: int,
        user_id: int,
        entry_cost: int,
        *,
        chance: float = 0.35,
    ) -> tuple[int, str]:
        """Retorna ``(valor, modo)``; o Joker ativo substitui o passivo."""
        cost = max(0, int(entry_cost))
        if cost <= 0 or not self._race_is(guild_id, user_id, "coringa"):
            return 0, ""

        joker_refund = 0
        async with self._race_progress_lock(guild_id, user_id):
            async with self._chip_economy_lock(guild_id, user_id):
                doc = self.db._get_user_doc(guild_id, user_id)
                active_until = float(doc.get("race_skill_joker_until", 0.0) or 0.0)
                if active_until > time.time() and self._race_is(guild_id, user_id, "coringa"):
                    joker_refund = min(RACE_SKILL_JOKER_REFUND_CAP, cost)
                    doc.pop("race_skill_joker_until", None)
                    doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + joker_refund
                    doc["has_chip_activity"] = True
                    await self.db._save_user_doc(
                        guild_id,
                        user_id,
                        doc,
                        unset_fields=("race_skill_joker_until",),
                    )
                elif active_until > 0.0 and active_until <= time.time():
                    doc.pop("race_skill_joker_until", None)
                    await self.db._save_user_doc(
                        guild_id,
                        user_id,
                        doc,
                        unset_fields=("race_skill_joker_until",),
                    )

        if joker_refund > 0:
            try:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=joker_refund,
                    kind="bonus",
                    reason="Joker · reembolso",
                    event_type="race_skill",
                    skill_eligible=False,
                )
            except Exception:
                pass
            return joker_refund, "joker"

        if random.random() >= float(chance):
            return 0, ""
        refund = max(1, int(round(cost * 0.5)))
        await self._change_user_chips(
            guild_id,
            user_id,
            refund,
            mark_activity=True,
            reason="Reembolso Coringa",
        )
        return refund, "passive"

    async def _maybe_apply_coringa_cashback(self, guild_id: int, user_id: int, entry_cost: int, *, chance: float = 0.35) -> int:
        refund, _mode = await self._maybe_apply_coringa_loss_refund(
            guild_id,
            user_id,
            entry_cost,
            chance=chance,
        )
        return refund

    async def _maybe_apply_coringa_lobby_refund(self, guild_id: int, user_id: int, entry_cost: int) -> int:
        refund, _mode = await self._maybe_apply_coringa_loss_refund(
            guild_id,
            user_id,
            entry_cost,
            chance=0.35,
        )
        return refund

    def _coringa_avoids_robbery_penalty(self, guild_id: int, user_id: int) -> bool:
        return self._race_is(guild_id, user_id, "coringa") and random.random() < 0.25

    def _chip_rank_position_text(self, guild: discord.Guild, user_id: int) -> str | None:
        position = self._chip_rank_cache.get_cached_position(guild.id, user_id)
        return f"🏆 Rank: **#{position}**" if position is not None else None

    @staticmethod
    def _chip_profile_global_name(member: discord.Member) -> str:
        """Nome visível completo, sem transformar o usuário em @tag."""
        candidates = (
            getattr(member, "display_name", None),
            getattr(member, "global_name", None),
            getattr(member, "name", None),
        )
        for candidate in candidates:
            normalized = " ".join(str(candidate or "").split()).strip()
            if normalized:
                return normalized.lstrip("@").strip() or "Usuário"
        return "Usuário"

    def _build_chip_profile_data(self, member: discord.Member) -> ChipProfileData:
        guild_id = int(member.guild.id)
        user_id = int(member.id)
        weekly_getter = getattr(self.db, "get_user_chip_week_delta", None)
        try:
            weekly_delta = int(weekly_getter(guild_id, user_id) if callable(weekly_getter) else 0)
        except (TypeError, ValueError):
            weekly_delta = 0

        race_info = self._get_user_race_info(guild_id, user_id) or {}
        race_name = " ".join(str(race_info.get("name") or "").split()) or None
        if race_name and not self._is_user_race_active(guild_id, user_id):
            race_name = f"{race_name} (inativa)"

        try:
            daily_status = self.db.get_user_daily_status(guild_id, user_id) or {}
            daily_available = bool(daily_status.get("available"))
        except Exception:
            daily_available = False
        try:
            recharge_available = bool(self._chip_recharge_state(guild_id, user_id).get("available"))
        except Exception:
            recharge_available = False

        unlocked_achievement_count = len(
            self._get_unlocked_achievement_keys(guild_id, user_id)
        )
        achievement_total = len(self._achievement_catalog())

        return ChipProfileData(
            display_name=self._chip_profile_global_name(member),
            chips=int(self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL) or 0),
            bonus_chips=int(self._get_user_bonus_chips(guild_id, user_id) or 0),
            weekly_delta=weekly_delta,
            rank_position=self._chip_rank_cache.get_position(member.guild, user_id),
            race_name=race_name,
            achievement_count=unlocked_achievement_count,
            achievement_total=achievement_total,
            daily_available=daily_available,
            recharge_available=recharge_available,
        )

    async def _send_chip_profile(self, sender, member: discord.Member, **send_kwargs):
        """Envia o PNG diretamente, sem contêiner de Components V2."""
        try:
            data = self._build_chip_profile_data(member)
            response = await self._chip_profile_cache.get_profile(member, data)
            image = discord.File(io.BytesIO(response.image_bytes), filename=PROFILE_FILENAME)
            return await sender(
                file=image,
                **send_kwargs,
            )
        except Exception as exc:
            print(
                f"[games] erro ao montar imagem do perfil "
                f"guild={getattr(getattr(member, 'guild', None), 'id', 0)} "
                f"user={getattr(member, 'id', 0)}: {exc!r}"
            )
            return await sender(
                view=self._make_chip_balance_view(member),
                **send_kwargs,
            )

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
            reason = str(entry.get("reason") or "").strip() or "Transação"
            ts = entry.get("ts") or 0
            when = self._format_chip_history_relative_time(ts)
            if kind == "mixed":
                normal_delta = int(entry.get("normal_delta", 0) or 0)
                bonus_delta = int(entry.get("bonus_delta", 0) or 0)
                chips_total += normal_delta
                bonus_total += bonus_delta
                mixed_parts: list[str] = []
                if normal_delta != 0:
                    sign = "+" if normal_delta > 0 else ""
                    icon = self._CHIP_GAIN_EMOJI if normal_delta > 0 else self._CHIP_LOSS_EMOJI
                    mixed_parts.append(f"{icon} **{sign}{normal_delta}**")
                if bonus_delta != 0:
                    sign = "+" if bonus_delta > 0 else ""
                    mixed_parts.append(f"{self._CHIP_BONUS_EMOJI} **{sign}{bonus_delta}**")
                movement_lines.append(f"{' + '.join(mixed_parts)} • {reason} · _{when}_")
                continue
            is_bonus = (kind == "bonus")
            if is_bonus:
                chip_emoji = self._CHIP_BONUS_EMOJI
                bonus_total += delta
            else:
                chip_emoji = self._CHIP_GAIN_EMOJI if delta > 0 else self._CHIP_LOSS_EMOJI
                chips_total += delta
            amount_text = f"+{delta}" if delta > 0 else f"{delta}"
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

    async def _make_chip_leaderboard_embed_async(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        return self._make_chip_leaderboard_embed(guild, requester)

    def _make_chip_leaderboard_embed(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        rows = [
            row
            for row in self.db.get_chip_leaderboard(guild.id, limit=100)
            if guild.get_member(int(row.get("user_id", 0) or 0)) is not None
        ][:10]
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
            previous_chips = None
            shared_position = 0
            for index, row in enumerate(rows, start=1):
                member = guild.get_member(int(row["user_id"]))
                if member is None:
                    continue
                name = member.display_name
                chips_val = int(row.get('chips', row.get('points', 0)) or 0)
                if previous_chips is None or chips_val != previous_chips:
                    shared_position = index
                    previous_chips = chips_val
                prefix = medals.get(shared_position, f"`#{shared_position}`")
                bonus_val = self._get_user_bonus_chips(guild.id, int(row["user_id"]))
                emoji = self._CHIP_LOSS_EMOJI if chips_val < 0 else self._CHIP_EMOJI
                balance_text = f"**{chips_val}** {emoji}"
                if bonus_val > 0:
                    balance_text += f" • **{bonus_val}** {self._CHIP_BONUS_EMOJI}"
                ranking_lines.append(f"{prefix} **{name}** — {balance_text}")
            embed.add_field(name="Top 10", value="\n".join(ranking_lines), inline=False)

        return embed

    @staticmethod
    def _safe_chip_rank_markdown(value: object, *, fallback: str) -> str:
        normalized = " ".join(str(value or fallback).split()) or fallback
        escaped = discord.utils.escape_markdown(normalized)
        return discord.utils.escape_mentions(escaped)

    def _chip_rank_components(
        self,
        response: ChipRankResponse,
        guild: discord.Guild,
        *,
        controls: discord.ui.ActionRow | None = None,
    ) -> list[discord.ui.Item]:
        guild_name = self._safe_chip_rank_markdown(getattr(guild, "name", None), fallback="Servidor")
        components = [
            discord.ui.TextDisplay(f"# {guild_name} • Top {response.top_number}"),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(f"attachment://{RANK_FILENAME}")
            ),
        ]
        if controls is not None:
            components.append(controls)
        if response.requester_line:
            components.extend(
                [
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(response.requester_line),
                ]
            )
        return components

    def _make_chip_rank_view(
        self,
        response: ChipRankResponse,
        guild: discord.Guild,
        requester: discord.Member | None,
    ) -> discord.ui.LayoutView:
        if response.page_count > 1:
            return _ChipRankPaginationView(
                self,
                guild=guild,
                requester=requester,
                response=response,
            )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                *self._chip_rank_components(response, guild),
            )
        )
        return view

    async def _send_chip_rank(self, sender, guild: discord.Guild, requester: discord.Member | None, **send_kwargs):
        """Envia o mesmo rank visual pelos comandos com prefixo e por texto livre."""
        try:
            response = await self._chip_rank_cache.get_rank(guild, requester)
            image = discord.File(io.BytesIO(response.image_bytes), filename=RANK_FILENAME)
            view = self._make_chip_rank_view(response, guild, requester)
            message = await sender(
                file=image,
                view=view,
                **send_kwargs,
            )
            if isinstance(view, _ChipRankPaginationView):
                view.message = message
            return message
        except Exception as exc:
            print(
                f"[games] erro ao montar imagem do rank "
                f"guild={getattr(guild, 'id', 0)} user={getattr(requester, 'id', 0)}: {exc!r}"
            )
            return await sender(
                view=self._make_chip_rank_fallback_view(guild, requester),
                **send_kwargs,
            )

    def _make_chip_rank_fallback_view(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.ui.LayoutView:
        snapshot_getter = getattr(self.db, "get_chip_rank_snapshot", None)
        rows = list(snapshot_getter(guild.id) if callable(snapshot_getter) else ())
        visible: list[tuple[discord.Member, dict]] = []
        for row in rows:
            member = guild.get_member(int(row.get("user_id", 0) or 0))
            if member is None or member.bot:
                continue
            visible.append((member, row))
        visible.sort(key=lambda item: (-int(item[1].get("chips", 0) or 0), item[0].name.casefold(), item[0].id))

        guild_name = self._safe_chip_rank_markdown(getattr(guild, "name", None), fallback="Servidor")
        lines = [f"# {guild_name} • Top {min(10, len(visible))}", ""]
        previous_chips = None
        shared_position = 0
        requester_position = None
        for index, (member, row) in enumerate(visible, start=1):
            chips = int(row.get("chips", 0) or 0)
            if previous_chips is None or chips != previous_chips:
                shared_position = index
                previous_chips = chips
            if requester is not None and member.id == requester.id:
                requester_position = shared_position
            if index > 10:
                continue
            bonus = max(0, int(row.get("bonus_chips", 0) or 0))
            weekly = int(row.get("weekly_delta", 0) or 0)
            chip_icon = self._CHIP_LOSS_EMOJI if chips < 0 else self._CHIP_EMOJI
            member_tag = self._safe_chip_rank_markdown(f"@{member.name}", fallback="@usuario")
            parts = [
                f"**#{shared_position}** {member_tag}",
                f"**{format_number(chips)}** {chip_icon}",
            ]
            if bonus > 0:
                parts.append(f"**{format_number(bonus)}** {self._CHIP_BONUS_EMOJI}")
            if weekly != 0:
                weekly_icon = self._CHIP_EMOJI if weekly > 0 else self._CHIP_LOSS_EMOJI
                parts.append(f"**{format_weekly_delta(weekly)}** {weekly_icon}")
            lines.append(" • ".join(parts))
        if len(lines) == 2:
            lines.append("Ainda não há jogadores com movimentação de fichas")
        if requester is not None:
            requester_chips = int(self.db.get_user_chips(guild.id, requester.id, default=CHIPS_INITIAL) or 0)
            weekly_getter = getattr(self.db, "get_user_chip_week_delta", None)
            requester_weekly = int(weekly_getter(guild.id, requester.id) if callable(weekly_getter) else 0)
            requester_prefix = (
                f"Você: **#{requester_position}**"
                if requester_position is not None
                else "Você ainda não entrou no rank"
            )
            requester_line = f"-# {requester_prefix} • **{format_number(requester_chips)} fichas**"
            requester_weekly_summary = format_weekly_chip_summary(requester_weekly)
            if requester_weekly_summary:
                requester_line += f" • {requester_weekly_summary}"
            lines.extend(["", requester_line])

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines))))
        return view

    def _format_chip_reset_remaining(self, remaining_seconds: float) -> str:
        remaining = max(0, int(remaining_seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _try_consume_chips(self, guild_id: int, user_id: int, amount: int, *, reason: str | None = None) -> tuple[bool, int, str | None]:
        spend = max(0, int(amount))
        history_normal = 0
        history_bonus = 0
        async with self._chip_economy_lock(guild_id, user_id):
            current_before = int(self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL) or 0)
            current_bonus = self._get_user_bonus_chips(guild_id, user_id)
            doc = self.db._get_user_doc(guild_id, user_id)
            now = float(time.time())
            temporary_expires_at = float(doc.get("race_skill_coinflip_temp_expires_at", 0.0) or 0.0)
            current_temporary = (
                max(0, int(doc.get("race_skill_coinflip_temp_bonus", 0) or 0))
                if temporary_expires_at > now
                else 0
            )
            use_temporary = min(current_temporary, spend)
            use_bonus = min(current_bonus, spend - use_temporary)
            remaining = spend - use_temporary - use_bonus
            projected_chips = current_before - remaining
            projected_bonus = current_bonus - use_bonus
            note = self._negative_transition_note(guild_id, user_id, spend)

            if self._needs_negative_confirmation(guild_id, user_id, spend):
                return False, current_before, note or "Confirme o saldo negativo antes de continuar"
            if projected_chips < -self._MAX_CHIP_DEBT:
                return False, current_before, self._insufficient_chips_text(guild_id, user_id, spend)

            # Reserva econômica em um único documento. Isso evita que spam de
            # jogos diferentes intercale o débito de bônus e o débito normal.
            doc["chips"] = int(projected_chips)
            doc["bonus_chips"] = int(projected_bonus)
            if current_temporary - use_temporary > 0 and temporary_expires_at > now:
                doc["race_skill_coinflip_temp_bonus"] = int(current_temporary - use_temporary)
            else:
                doc.pop("race_skill_coinflip_temp_bonus", None)
                doc.pop("race_skill_coinflip_temp_expires_at", None)
            doc["has_chip_activity"] = True
            await self.db._save_user_doc(
                guild_id,
                user_id,
                doc,
                unset_fields=(
                    "race_skill_coinflip_temp_bonus",
                    "race_skill_coinflip_temp_expires_at",
                ),
            )
            history_normal = int(remaining)
            history_bonus = int(use_bonus)

        # Histórico não participa da seção crítica: o saldo já foi reservado e
        # novas rodadas podem entrar enquanto estes registros são persistidos.
        try:
            if history_normal > 0:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=-history_normal,
                    kind="chips",
                    reason=reason,
                )
            if history_bonus > 0:
                await self.db.append_chip_history(
                    guild_id,
                    user_id,
                    delta=-history_bonus,
                    kind="bonus",
                    reason=reason,
                )
        except Exception:
            pass
        return True, int(projected_chips), note

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

    def _is_bot_member(self, member: discord.Member | None) -> bool:
        return bool(member is not None and getattr(member, "bot", False))

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
            if member is None or self._is_bot_member(member):
                continue
            if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
                continue
            result[int(member.id)] = member
            seed_ids.append(int(member.id))

        for uid in self._expand_gincana_focus_ids(guild.id, seed_ids):
            if uid in result:
                continue
            member = guild.get_member(int(uid))
            if member is None or self._is_bot_member(member):
                continue
            if own_bot_id and int(getattr(member, "id", 0) or 0) == own_bot_id:
                continue
            result[int(member.id)] = member
        return list(result.values())

    def _is_focused_non_staff_member(self, member: discord.Member) -> bool:
        guild = getattr(member, "guild", None)
        if guild is None or self._is_staff_member(member) or self._is_bot_member(member):
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
            if self._is_bot_member(member):
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
            if self._is_bot_member(member):
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
