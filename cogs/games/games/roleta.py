import asyncio
import logging
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
    ROLETA_APOSTADOR_COST,
    ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS,
    ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS,
    ROLETA_COST,
    ROLETA_JACKPOT_CHIPS,
)


ROLETA_JOKERS = ("🃏", "⭐")
ROLETA_SPIN_LIMIT = 10
ROLETA_WINDOW_SECONDS = 6 * 60 * 60
ROLETA_DAILY_EXTRA_CAP = 1

CARTA_COST = 15
CARTA_JACKPOT_CHIPS = 100
CARTA_SYMBOLS = ("🍀", "💎", "👑", "🃏", "⭐")
CARTA_WEIGHTS = (40, 28, 18, 10, 4)
CARTA_SPIN_LIMIT = 5
CARTA_WINDOW_SECONDS = ROLETA_WINDOW_SECONDS
CARTA_DAILY_EXTRA_CAP = ROLETA_DAILY_EXTRA_CAP
ROLETA_TRIGGER_COOLDOWN_SECONDS = 5.0
ROLETA_REPLAY_WINDOW_SECONDS = 20.0
GAME_ANIMATION_LIMIT_PER_GUILD = 2
GAME_ANIMATION_STALE_SECONDS = 75.0


class _GameReplayView(discord.ui.View):
    def __init__(self, cog, *, owner_id: int, kind: str, enabled: bool):
        super().__init__(timeout=ROLETA_REPLAY_WINDOW_SECONDS if enabled else None)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.kind = str(kind)
        self.message: discord.Message | None = None
        self.replay_button = discord.ui.Button(emoji="🔄", style=discord.ButtonStyle.secondary, disabled=not enabled)
        self.replay_button.callback = self._handle_replay
        self.add_item(self.replay_button)

    async def _handle_replay(self, interaction: discord.Interaction):
        await self.cog._handle_game_replay_button(interaction, self)

    def set_enabled(self, enabled: bool):
        self.replay_button.disabled = not enabled
        self.timeout = ROLETA_REPLAY_WINDOW_SECONDS if enabled else None

    async def on_timeout(self):
        try:
            if self.message is not None:
                await self.message.edit(view=None)
        except Exception:
            pass


class GincanaRoletaMixin:
        def _random_roleta_digit(self, exclude: set[object] | None = None) -> int:
            exclude = exclude or set()
            choices = [digit for digit in range(1, 10) if digit not in exclude]
            if not choices:
                choices = list(range(1, 10))
            return random.choice(choices)

        def _random_roleta_joker(self) -> str:
            return random.choice(ROLETA_JOKERS)
        def _build_roleta_column(self, middle: object | None = None) -> list[object]:
            return [
                self._random_roleta_digit(),
                middle if middle is not None else self._random_roleta_digit(),
                self._random_roleta_digit(),
            ]
        def _spin_roleta_column(self, column: list[object], next_top: object | None = None):
            column.insert(0, self._random_roleta_digit() if next_top is None else next_top)
            del column[3:]
        def _make_roleta_stop_plan(self, column: list[object], target_middle: object) -> list[object]:
            first_top = self._random_roleta_digit(exclude={target_middle, column[0], column[1], column[2]})
            final_top = self._random_roleta_digit(exclude={target_middle, first_top})
            return [first_top, target_middle, final_top]
        def _format_roleta_row(self, row: list[object], *, compact: bool = False) -> str:
            cells = [str(cell) for cell in row]
            if compact:
                return f" {cells[0]}  {cells[1]}  {cells[2]} "
            return f"  {cells[0]}  {cells[1]}  {cells[2]}  "
        def _render_roleta_board(self, columns: list[list[int]]) -> str:
            rows = [[columns[0][i], columns[1][i], columns[2][i]] for i in range(3)]
            top_row = self._format_roleta_row(rows[0])
            middle_row = self._format_roleta_row(rows[1], compact=True)
            bottom_row = self._format_roleta_row(rows[2])
            lines = [
                "┌───────────┐",
                f"│{top_row}│",
                "├───────────┤",
                f"»│{middle_row}│«",
                "├───────────┤",
                f"│{bottom_row}│",
                "└───────────┘",
            ]
            return "```text\n" + "\n".join(lines) + "\n```"
        def _build_game_flow_description(self, *, entry_cost: int, jackpot: int, balance_text: str, board: str, summary: str | None = None) -> str:
            lines: list[str] = []
            if summary:
                lines.extend([summary, ""])
            lines.extend([
                f"**Entrada:** {entry_cost} {self._CHIP_LOSS_EMOJI}",
                f"**Jackpot:** {jackpot} {self._CHIP_GAIN_EMOJI}",
                f"**Saldo atual:** {balance_text}",
                "",
                board,
            ])
            return "\n".join(lines)
        def _roleta_jackpot_preview(self, guild_id: int, user_id: int) -> int:
            return ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS if self._race_is(guild_id, user_id, "apostador") else ROLETA_JACKPOT_CHIPS
        def _roleta_outcome_for_user(self, guild_id: int, user_id: int) -> dict[str, object]:
            if self._race_is(guild_id, user_id, "apostador"):
                roll = random.random()
                if roll < 0.05:
                    return {"target_middle": [7, 7, 7], "forced_kind": "jackpot_mega", "forced_amount": ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS}
                if roll < 0.20:
                    return {"target_middle": [9, 9, 9], "forced_kind": "jackpot", "forced_amount": ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS}
                if random.random() < 0.25:
                    return {"target_middle": [6, 6, 6], "forced_kind": "return", "forced_amount": ROLETA_APOSTADOR_COST}
                target_middle = self._roll_roleta_target_middle(success=False)
                return {"target_middle": target_middle, "forced_kind": None, "forced_amount": None}
            success = random.randint(1, 10) == 1
            target_middle = self._roll_roleta_target_middle(success=success)
            return {"target_middle": target_middle, "forced_kind": "jackpot" if success else None, "forced_amount": ROLETA_JACKPOT_CHIPS if success else None}
        def _make_roleta_spin_embed(self, board: str, *, balance_text: str, footer_text: str | None = None, entry_cost: int = ROLETA_COST, jackpot: int = ROLETA_JACKPOT_CHIPS) -> discord.Embed:
            embed = discord.Embed(
                title="🎰 Girando...",
                description=self._build_game_flow_description(entry_cost=entry_cost, jackpot=jackpot, balance_text=balance_text, board=board),
                color=discord.Color.blurple(),
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed
        def _make_roleta_result_embed(self, title: str, summary: str, board: str, *, balance_text: str, success: bool, near: bool = False, footer_text: str | None = None, entry_cost: int = ROLETA_COST, jackpot: int = ROLETA_JACKPOT_CHIPS) -> discord.Embed:
            color = discord.Color.blurple() if success or near else discord.Color(OFF_COLOR)
            embed = discord.Embed(
                title=title,
                description=self._build_game_flow_description(entry_cost=entry_cost, jackpot=jackpot, balance_text=balance_text, board=board, summary=summary),
                color=color,
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _roleta_window_total(self, bonus_spins: int = 0) -> int:
            return ROLETA_SPIN_LIMIT + max(0, min(ROLETA_DAILY_EXTRA_CAP, int(bonus_spins or 0)))

        def _format_roleta_reset_time(self, remaining_seconds: float) -> str:
            try:
                total_minutes = max(1, int((float(remaining_seconds) + 59) // 60))
            except Exception:
                total_minutes = 1
            hours, minutes = divmod(total_minutes, 60)
            if hours > 0:
                return f"{hours}h {minutes}min"
            return f"{minutes}min"

        async def _sync_roleta_spin_window(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            now = time.time()
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                started_at = float(doc.get("roleta_window_started_at", 0) or 0.0)
            except Exception:
                started_at = 0.0
            try:
                used = max(0, int(doc.get("roleta_spins_used", 0) or 0))
            except Exception:
                used = 0
            try:
                bonus = max(0, min(ROLETA_DAILY_EXTRA_CAP, int(doc.get("roleta_bonus_spins", 0) or 0)))
            except Exception:
                bonus = 0
            changed = False
            if started_at <= 0 or (started_at + ROLETA_WINDOW_SECONDS) <= now:
                started_at = now
                used = 0
                bonus = 0
                doc["roleta_window_started_at"] = float(started_at)
                doc["roleta_spins_used"] = 0
                doc["roleta_bonus_spins"] = 0
                changed = True
            total = self._roleta_window_total(bonus)
            available = max(0, total - used)
            reset_in = max(0.0, (started_at + ROLETA_WINDOW_SECONDS) - now)
            if changed:
                await self.db._save_user_doc(guild_id, user_id, doc)
            return {
                "started_at": float(started_at),
                "used": int(used),
                "bonus": int(bonus),
                "total": int(total),
                "available": int(available),
                "reset_in": float(reset_in),
            }

        async def _consume_roleta_spin(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            if int(state["available"]) <= 0:
                return state
            doc = self.db._get_user_doc(guild_id, user_id)
            used = int(state["used"]) + 1
            doc["roleta_window_started_at"] = float(state["started_at"])
            doc["roleta_spins_used"] = used
            doc["roleta_bonus_spins"] = int(state["bonus"])
            await self.db._save_user_doc(guild_id, user_id, doc)
            total = int(state["total"])
            return {
                "started_at": float(state["started_at"]),
                "used": used,
                "bonus": int(state["bonus"]),
                "total": total,
                "available": max(0, total - used),
                "reset_in": float(max(0.0, (float(state["started_at"]) + ROLETA_WINDOW_SECONDS) - time.time())),
            }

        async def _grant_daily_roleta_spin(self, guild_id: int, user_id: int) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            current_bonus = int(state["bonus"])
            if current_bonus >= ROLETA_DAILY_EXTRA_CAP:
                return False, state
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["roleta_window_started_at"] = float(state["started_at"])
            doc["roleta_spins_used"] = int(state["used"])
            doc["roleta_bonus_spins"] = min(ROLETA_DAILY_EXTRA_CAP, current_bonus + 1)
            await self.db._save_user_doc(guild_id, user_id, doc)
            new_state = await self._sync_roleta_spin_window(guild_id, user_id)
            return True, new_state

        def _roleta_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
            available = int(state.get("available", 0) or 0)
            if available <= 0 and is_staff:
                return "Seus giros acabaram, mas como você é staff você ainda pode girar."
            giro_text = "giro" if available == 1 else "giros"
            return f"Resta {available} {giro_text} • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _roleta_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
            total = max(ROLETA_SPIN_LIMIT, int(state.get("total", ROLETA_SPIN_LIMIT) or ROLETA_SPIN_LIMIT))
            wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
            return "🎰 Sem giros por agora", f"Seus {total} giros acabaram. Reset em **{wait_text}**."

        async def _reserve_roleta_spin_state(self, guild_id: int, user_id: int, *, is_staff: bool) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            available = int(state.get("available", 0) or 0)
            if available <= 0:
                return bool(is_staff), state
            consumed = await self._consume_roleta_spin(guild_id, user_id)
            return True, consumed

        def _roll_roleta_target_middle(self, *, success: bool) -> list[object]:
            if success:
                return [7, 7, 7]
            roll = random.random()
            if roll < 0.05:
                base = random.randint(1, 9)
                joker = self._random_roleta_joker()
                middle = [base, joker, base]
                random.shuffle(middle)
                return middle
            if roll < 0.13:
                digits = random.sample(range(1, 10), 2)
                middle = [digits[0], digits[1], self._random_roleta_joker()]
                random.shuffle(middle)
                return middle
            if roll < 0.43:
                repeated = random.randint(1, 9)
                other = self._random_roleta_digit(exclude={repeated})
                middle = [repeated, repeated, other]
                random.shuffle(middle)
                return middle
            while True:
                middle = [random.randint(1, 9) for _ in range(3)]
                if middle != [7, 7, 7] and len(set(middle)) == 3:
                    return middle

        def _evaluate_roleta_middle(self, middle_digits: list[object], *, guild_id: int | None = None, user_id: int | None = None) -> tuple[str, int]:
            entry_cost = self._roleta_cost_for_user(int(guild_id or 0), int(user_id or 0)) if guild_id and user_id else ROLETA_COST
            if guild_id and user_id and self._race_is(int(guild_id), int(user_id), "apostador"):
                if middle_digits == [7, 7, 7]:
                    return "jackpot_mega", ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS
                if middle_digits == [9, 9, 9]:
                    return "jackpot", ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS
                if middle_digits == [6, 6, 6]:
                    return "return", entry_cost
            jokers = [value for value in middle_digits if isinstance(value, str) and value in ROLETA_JOKERS]
            normals = [value for value in middle_digits if not (isinstance(value, str) and value in ROLETA_JOKERS)]
            if middle_digits == [7, 7, 7]:
                return "jackpot", ROLETA_JACKPOT_CHIPS
            if jokers:
                if len(set(normals)) == 1 and len(normals) == 2:
                    return "joker_premium", 50
                return "return", max(3, entry_cost // 2)
            if max((middle_digits.count(v) for v in set(middle_digits)), default=0) >= 2:
                return "partial", max(3, entry_cost // 2)
            if random.random() < 0.08:
                return "return", max(2, entry_cost // 3)
            return "loss", 0
        def _ensure_game_animation_runtime(self):
            if not hasattr(self, "_game_animation_states"):
                self._game_animation_states: dict[int, dict[str, object]] = {}
            if not hasattr(self, "_roleta_trigger_cooldowns"):
                self._roleta_trigger_cooldowns: dict[tuple[int, int], float] = {}
            if not hasattr(self, "_game_message_edit_locks"):
                self._game_message_edit_locks: dict[int, asyncio.Lock] = {}

        def _game_animation_state(self, guild_id: int) -> dict[str, object]:
            self._ensure_game_animation_runtime()
            state = self._game_animation_states.get(guild_id)
            if state is None:
                state = {"lock": asyncio.Lock(), "order": [], "entries": {}}
                self._game_animation_states[guild_id] = state
            return state

        def _next_game_animation_session_id(self, *, guild_id: int, kind: str, owner_id: int) -> str:
            return f"{kind}:{guild_id}:{owner_id}:{time.monotonic_ns()}"

        def _touch_game_animation_entry(self, entry: dict[str, object] | None):
            if entry is None:
                return
            now = time.monotonic()
            entry.setdefault("created_at", now)
            entry["last_progress_at"] = now

        async def _cleanup_stale_game_animation_slots(self, guild_id: int):
            state = self._game_animation_state(guild_id)
            lock: asyncio.Lock = state["lock"]
            async with lock:
                order: list[str] = state["order"]
                entries: dict[str, dict[str, object]] = state["entries"]
                if not entries:
                    self._game_animation_states.pop(guild_id, None)
                    return
                now = time.monotonic()
                stale_ids: list[str] = []
                for queued_session_id, entry in list(entries.items()):
                    last_progress = float(entry.get("last_progress_at") or entry.get("created_at") or now)
                    created_at = float(entry.get("created_at") or last_progress)
                    if (now - last_progress) > GAME_ANIMATION_STALE_SECONDS or (now - created_at) > (GAME_ANIMATION_STALE_SECONDS * 2.0):
                        stale_ids.append(queued_session_id)
                if not stale_ids:
                    return
                front_removed = False
                for queued_session_id in stale_ids:
                    if order and order[0] == queued_session_id:
                        front_removed = True
                    while queued_session_id in order:
                        order.remove(queued_session_id)
                    entries.pop(queued_session_id, None)
                if not order:
                    self._game_animation_states.pop(guild_id, None)
                    return
                if front_removed:
                    nxt = entries.get(order[0])
                    if nxt is not None:
                        self._touch_game_animation_entry(nxt)
                        nxt["event"].set()

        async def _try_acquire_game_animation_slot(self, guild_id: int, session_id: str) -> bool:
            await self._cleanup_stale_game_animation_slots(guild_id)
            state = self._game_animation_state(guild_id)
            lock: asyncio.Lock = state["lock"]
            async with lock:
                order: list[str] = state["order"]
                entries: dict[str, dict[str, object]] = state["entries"]
                if session_id in entries:
                    self._touch_game_animation_entry(entries.get(session_id))
                    return True
                if len(order) >= GAME_ANIMATION_LIMIT_PER_GUILD:
                    return False
                event = asyncio.Event()
                entry = {"event": event}
                self._touch_game_animation_entry(entry)
                entries[session_id] = entry
                order.append(session_id)
                if len(order) == 1:
                    event.set()
                return True

        async def _wait_for_game_animation_turn(self, guild_id: int, session_id: str) -> bool:
            await self._cleanup_stale_game_animation_slots(guild_id)
            state = self._game_animation_state(guild_id)
            entry = state["entries"].get(session_id)
            if entry is None:
                return False
            self._touch_game_animation_entry(entry)
            event: asyncio.Event = entry["event"]
            await event.wait()
            event.clear()
            self._touch_game_animation_entry(entry)
            return True

        async def _advance_game_animation_turn(self, guild_id: int, session_id: str):
            state = self._game_animation_state(guild_id)
            lock: asyncio.Lock = state["lock"]
            async with lock:
                order: list[str] = state["order"]
                entries: dict[str, dict[str, object]] = state["entries"]
                if session_id not in entries or not order:
                    return
                current_entry = entries.get(session_id)
                self._touch_game_animation_entry(current_entry)
                if order[0] != session_id:
                    current = entries.get(order[0])
                    if current is not None:
                        self._touch_game_animation_entry(current)
                        current["event"].set()
                    return
                if len(order) == 1:
                    solo = entries.get(session_id)
                    if solo is not None:
                        self._touch_game_animation_entry(solo)
                        solo["event"].set()
                    return
                order.append(order.pop(0))
                nxt = entries.get(order[0])
                if nxt is not None:
                    self._touch_game_animation_entry(nxt)
                    nxt["event"].set()

        async def _release_game_animation_slot(self, guild_id: int, session_id: str):
            state = self._game_animation_state(guild_id)
            lock: asyncio.Lock = state["lock"]
            async with lock:
                order: list[str] = state["order"]
                entries: dict[str, dict[str, object]] = state["entries"]
                was_front = bool(order and order[0] == session_id)
                if session_id in order:
                    order.remove(session_id)
                entries.pop(session_id, None)
                if not order:
                    self._game_animation_states.pop(guild_id, None)
                    return
                if was_front or len(order) == 1:
                    nxt = entries.get(order[0])
                    if nxt is not None:
                        self._touch_game_animation_entry(nxt)
                        nxt["event"].set()

        def _is_edit_rate_limited(self, exc: Exception) -> bool:
            if getattr(exc, "status", None) == 429:
                return True
            if getattr(exc, "retry_after", None) is not None:
                return True
            return "rate limit" in str(exc).casefold()

        def _is_permanent_game_message_error(self, exc: Exception) -> bool:
            if isinstance(exc, (discord.Forbidden, discord.NotFound)):
                return True
            try:
                status = int(getattr(exc, "status", 0) or 0)
            except Exception:
                status = 0
            return 400 <= status < 500 and status != 429

        def _game_message_edit_lock(self, message: discord.Message) -> asyncio.Lock:
            self._ensure_game_animation_runtime()
            message_id = int(getattr(message, "id", 0) or id(message))
            lock = self._game_message_edit_locks.get(message_id)
            if lock is None:
                lock = asyncio.Lock()
                self._game_message_edit_locks[message_id] = lock
            return lock

        def _drop_game_message_edit_lock(self, message: discord.Message | None):
            if message is None:
                return
            self._ensure_game_animation_runtime()
            message_id = int(getattr(message, "id", 0) or id(message))
            self._game_message_edit_locks.pop(message_id, None)

        async def _edit_game_message(self, message: discord.Message, *, embed: discord.Embed, view: discord.ui.View | None = None, final: bool = False) -> bool:
            attempts = 10 if final else 4
            delay = 0.75 if final else 0.30
            lock = self._game_message_edit_lock(message)
            async with lock:
                for attempt in range(attempts):
                    try:
                        if view is None:
                            await message.edit(embed=embed)
                        else:
                            await message.edit(embed=embed, view=view)
                        return True
                    except Exception as exc:
                        if self._is_permanent_game_message_error(exc):
                            return False
                        if attempt >= attempts - 1:
                            break
                        if self._is_edit_rate_limited(exc):
                            retry_after = getattr(exc, "retry_after", None)
                            try:
                                sleep_for = float(retry_after) if retry_after is not None else delay
                            except Exception:
                                sleep_for = delay
                            await asyncio.sleep(max(0.35, min(sleep_for, 5.0)))
                            delay = min(delay * 1.6, 5.0)
                            continue
                        await asyncio.sleep(max(0.20, min(delay, 2.0)))
                        delay = min(delay * 1.5, 2.5)
            return False

        async def _send_game_message(self, channel: discord.abc.Messageable, *, embed: discord.Embed, view: discord.ui.View | None = None, final: bool = False) -> discord.Message | None:
            attempts = 10 if final else 4
            delay = 0.75 if final else 0.30
            for attempt in range(attempts):
                try:
                    return await channel.send(embed=embed, view=view)
                except Exception as exc:
                    if self._is_permanent_game_message_error(exc):
                        return None
                    if attempt >= attempts - 1:
                        break
                    if self._is_edit_rate_limited(exc):
                        retry_after = getattr(exc, "retry_after", None)
                        try:
                            sleep_for = float(retry_after) if retry_after is not None else delay
                        except Exception:
                            sleep_for = delay
                        await asyncio.sleep(max(0.35, min(sleep_for, 5.0)))
                        delay = min(delay * 1.6, 5.0)
                        continue
                    await asyncio.sleep(max(0.20, min(delay, 2.0)))
                    delay = min(delay * 1.5, 2.5)
            return None

        async def _delete_game_message(self, message: discord.Message | None):
            if message is None:
                return
            try:
                await message.delete()
            except Exception:
                pass
            finally:
                self._drop_game_message_edit_lock(message)

        def _is_own_game_message(self, message: discord.Message | None) -> bool:
            if message is None:
                return False
            bot_user = getattr(getattr(self, "bot", None), "user", None)
            bot_user_id = getattr(bot_user, "id", None)
            return bot_user_id is not None and getattr(getattr(message, "author", None), "id", None) == bot_user_id

        async def _render_or_replace_game_message(self, source_message: discord.Message, target_message: discord.Message | None, *, embed: discord.Embed, view: discord.ui.View | None = None, final: bool = False) -> discord.Message | None:
            if target_message is not None:
                edited = await self._edit_game_message(target_message, embed=embed, view=view, final=final)
                if edited:
                    if isinstance(view, _GameReplayView):
                        view.message = target_message
                    return target_message

            channel = getattr(target_message, "channel", None) or getattr(source_message, "channel", None)
            if channel is None:
                return None
            replacement = await self._send_game_message(channel, embed=embed, view=view, final=final)
            if replacement is None:
                return None
            if isinstance(view, _GameReplayView):
                view.message = replacement
            if target_message is not None and target_message is not replacement:
                if self._is_own_game_message(target_message):
                    await self._delete_game_message(target_message)
                else:
                    self._drop_game_message_edit_lock(target_message)
            return replacement

        async def _deliver_game_result(self, source_message: discord.Message, target_message: discord.Message | None, *, embed: discord.Embed, view: discord.ui.View | None = None) -> discord.Message | None:
            target = target_message or source_message
            if target is None:
                return None

            disabled_view = None
            if isinstance(view, _GameReplayView):
                disabled_view = _GameReplayView(self, owner_id=view.owner_id, kind=view.kind, enabled=False)

            delivered = await self._render_or_replace_game_message(source_message, target, embed=embed, view=disabled_view, final=True)
            if delivered is None:
                if isinstance(disabled_view, _GameReplayView):
                    disabled_view.stop()
                self._drop_game_message_edit_lock(target)
                logging.getLogger("gincana.roleta").warning(
                    "não foi possível entregar o resultado do jogo | guild=%s channel=%s message=%s",
                    getattr(getattr(source_message, "guild", None), "id", None),
                    getattr(getattr(source_message, "channel", None), "id", None),
                    getattr(target, "id", None),
                )
                return target

            await asyncio.sleep(2.0)

            if isinstance(view, _GameReplayView):
                view.set_enabled(True)
                enabled_message = None
                try:
                    enabled_message = await self._render_or_replace_game_message(source_message, delivered, embed=embed, view=view, final=True)
                finally:
                    if isinstance(disabled_view, _GameReplayView):
                        disabled_view.stop()
                final_message = enabled_message or delivered
                self._drop_game_message_edit_lock(final_message)
                return final_message

            self._drop_game_message_edit_lock(delivered)
            return delivered

        def _roleta_trigger_cooldown_remaining(self, guild_id: int, user_id: int) -> float:
            self._ensure_game_animation_runtime()
            last_used = float(self._roleta_trigger_cooldowns.get((guild_id, user_id), 0.0) or 0.0)
            return max(0.0, (last_used + ROLETA_TRIGGER_COOLDOWN_SECONDS) - time.time())

        def _mark_roleta_trigger_used(self, guild_id: int, user_id: int):
            self._ensure_game_animation_runtime()
            self._roleta_trigger_cooldowns[(guild_id, user_id)] = time.time()

        async def _send_animation_limit_message(self, message: discord.Message, *, title: str):
            try:
                await message.channel.send(embed=self._make_embed(title, "Já existem **2** animações ativas neste servidor. Tente novamente em instantes.", ok=False))
            except Exception:
                pass

        async def _send_replay_owner_error(self, interaction: discord.Interaction, kind: str):
            text = "Essa roleta não é sua." if kind == "roleta" else "Essas cartas não são suas."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except Exception:
                pass

        async def _handle_game_replay_button(self, interaction: discord.Interaction, view: _GameReplayView):
            if int(interaction.user.id) != view.owner_id:
                await self._send_replay_owner_error(interaction, view.kind)
                return
            guild = interaction.guild
            message = interaction.message
            if guild is None or message is None:
                return
            session_id = self._next_game_animation_session_id(guild_id=guild.id, kind=view.kind, owner_id=interaction.user.id)
            if not await self._try_acquire_game_animation_slot(guild.id, session_id):
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send("Já existem 2 animações ativas neste servidor. Tente novamente em instantes.", ephemeral=True)
                    else:
                        await interaction.response.send_message("Já existem 2 animações ativas neste servidor. Tente novamente em instantes.", ephemeral=True)
                except Exception:
                    pass
                return
            try:
                is_staff = isinstance(interaction.user, discord.Member) and self._is_staff_member(interaction.user)
                if view.kind == "roleta":
                    state = await self._sync_roleta_spin_window(guild.id, interaction.user.id)
                    if int(state.get("available", 0) or 0) <= 0 and not is_staff:
                        title, desc = self._roleta_spin_message_text(state)
                        embed = self._make_embed(title, desc, ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    entry_cost = self._roleta_cost_for_user(guild.id, interaction.user.id)
                    blessing_state = await self._sync_sortudo_blessings(guild.id, interaction.user.id)
                    free_spin = self._race_is(guild.id, interaction.user.id, "sortudo") and int(blessing_state.get("charges", 0) or 0) > 0
                    needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(guild.id, interaction.user.id, entry_cost)
                    if needs_negative_confirm:
                        confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, interaction.user.id, entry_cost, title="🎰 Confirmar aposta")
                        if not confirmed:
                            return
                    can_spin, state = await self._reserve_roleta_spin_state(guild.id, interaction.user.id, is_staff=is_staff)
                    if not can_spin:
                        title, desc = self._roleta_spin_message_text(state)
                        embed = self._make_embed(title, desc, ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    footer = self._roleta_footer_text(state=state, is_staff=is_staff)
                    if free_spin:
                        await self._consume_sortudo_blessing(guild.id, interaction.user.id)
                        chip_note = self._sortudo_blessing_note(guild.id, interaction.user.id, kind="roleta")
                        entry_spend = {"chips": 0, "bonus": 0}
                        paid, _balance = True, self.db.get_user_chips(guild.id, interaction.user.id, default=100)
                    else:
                        entry_spend = self._entry_spend_parts(guild.id, interaction.user.id, entry_cost)
                        paid, _balance, chip_note = await self._try_consume_chips(guild.id, interaction.user.id, entry_cost, reason="Entrada na roleta")
                    if needs_negative_confirm:
                        chip_note = None
                    if not paid:
                        embed = self._make_embed("🎰 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    view.set_enabled(False)
                    view.stop()
                    try:
                        if interaction.response.is_done():
                            await message.edit(view=view)
                        else:
                            await interaction.response.edit_message(view=view)
                    except Exception:
                        try:
                            if not interaction.response.is_done():
                                await interaction.response.defer()
                        except Exception:
                            pass
                    author_voice = getattr(interaction.user, "voice", None)
                    voice_channel = getattr(author_voice, "channel", None)
                    targets = self._resolve_targets(guild, voice_channel) if isinstance(voice_channel, discord.VoiceChannel) else []
                    await self._execute_roleta_round(source_message=message, guild=guild, actor=interaction.user, roleta_footer=footer, chip_note=chip_note, voice_channel=voice_channel, targets=targets, session_id=session_id, spin_message=message, entry_cost=entry_cost, entry_spend=entry_spend)
                else:
                    state = await self._sync_carta_spin_window(guild.id, interaction.user.id)
                    if int(state.get("available", 0) or 0) <= 0 and not is_staff:
                        title, desc = self._carta_spin_message_text(state)
                        embed = self._make_embed(title, desc, ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    entry_cost = CARTA_COST
                    blessing_state = await self._sync_sortudo_blessings(guild.id, interaction.user.id)
                    free_spin = self._race_is(guild.id, interaction.user.id, "sortudo") and int(blessing_state.get("charges", 0) or 0) > 0
                    needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(guild.id, interaction.user.id, entry_cost)
                    if needs_negative_confirm:
                        confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, interaction.user.id, entry_cost, title="🎴 Confirmar aposta")
                        if not confirmed:
                            return
                    can_spin, state = await self._reserve_carta_spin_state(guild.id, interaction.user.id, is_staff=is_staff)
                    if not can_spin:
                        title, desc = self._carta_spin_message_text(state)
                        embed = self._make_embed(title, desc, ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    footer = self._carta_footer_text(state=state, is_staff=is_staff)
                    if free_spin:
                        await self._consume_sortudo_blessing(guild.id, interaction.user.id)
                        chip_note = self._sortudo_blessing_note(guild.id, interaction.user.id, kind="carta")
                        entry_spend = {"chips": 0, "bonus": 0}
                        paid, _balance = True, self.db.get_user_chips(guild.id, interaction.user.id, default=100)
                    else:
                        entry_spend = self._entry_spend_parts(guild.id, interaction.user.id, entry_cost)
                        paid, _balance, chip_note = await self._try_consume_chips(guild.id, interaction.user.id, entry_cost, reason="Entrada nas cartas")
                    if needs_negative_confirm:
                        chip_note = None
                    if not paid:
                        embed = self._make_embed("🎴 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False)
                        if interaction.response.is_done():
                            await interaction.followup.send(embed=embed, ephemeral=True)
                        else:
                            await interaction.response.send_message(embed=embed, ephemeral=True)
                        return
                    view.set_enabled(False)
                    view.stop()
                    try:
                        if interaction.response.is_done():
                            await message.edit(view=view)
                        else:
                            await interaction.response.edit_message(view=view)
                    except Exception:
                        try:
                            if not interaction.response.is_done():
                                await interaction.response.defer()
                        except Exception:
                            pass
                    await self._execute_carta_round(source_message=message, guild=guild, actor=interaction.user, carta_footer=footer, chip_note=chip_note, session_id=session_id, spin_message=message, entry_cost=entry_cost, entry_spend=entry_spend)
            finally:
                await self._release_game_animation_slot(guild.id, session_id)
        async def _animate_roleta_spin(self, message: discord.Message, *, target_middle: list[int], balance_text: str, footer_text: str | None = None, spin_message: discord.Message | None = None, owner_id: int | None = None, guild_id: int | None = None, session_id: str | None = None, entry_cost: int = ROLETA_COST, jackpot: int = ROLETA_JACKPOT_CHIPS) -> tuple[discord.Message | None, list[list[int]] | None]:
            columns = [self._build_roleta_column() for _ in range(3)]
            for idx in range(3):
                if columns[idx][1] == target_middle[idx]:
                    reroll = self._build_roleta_column()
                    while reroll[1] == target_middle[idx]:
                        reroll = self._build_roleta_column()
                    columns[idx] = reroll
            disabled_view = None
            opening_embed = self._make_roleta_spin_embed(self._render_roleta_board(columns), balance_text=balance_text, footer_text=footer_text, entry_cost=entry_cost, jackpot=jackpot)
            spin_message = await self._render_or_replace_game_message(message, spin_message, embed=opening_embed, view=disabled_view, final=False)
            if spin_message is None:
                return None, None

            target_duration = 5.0
            intervals = [0.18, 0.21, 0.24, 0.28, 0.33, 0.39, 0.47, 0.58, 0.72, 0.90, 1.05]
            scale = target_duration / sum(intervals)
            intervals = [step * scale for step in intervals]
            stop_plan_starts = {0: max(0, len(intervals) - 5), 1: max(0, len(intervals) - 4), 2: max(0, len(intervals) - 3)}
            active_stop_plans: dict[int, list[int]] = {}
            locked_columns: set[int] = set()
            previous_board = None

            for index, delay in enumerate(intervals):
                await asyncio.sleep(delay)
                has_turn = False
                try:
                    if guild_id is not None and session_id is not None:
                        has_turn = await self._wait_for_game_animation_turn(guild_id, session_id)
                        if not has_turn:
                            continue
                    for column_index, start_index in stop_plan_starts.items():
                        if index == start_index and column_index not in active_stop_plans and column_index not in locked_columns:
                            active_stop_plans[column_index] = self._make_roleta_stop_plan(columns[column_index], target_middle[column_index])
                    for column_index in range(3):
                        if column_index in locked_columns:
                            continue
                        if column_index in active_stop_plans:
                            plan = active_stop_plans[column_index]
                            self._spin_roleta_column(columns[column_index], next_top=plan.pop(0))
                            if not plan:
                                active_stop_plans.pop(column_index, None)
                                locked_columns.add(column_index)
                        else:
                            self._spin_roleta_column(columns[column_index])
                    board = self._render_roleta_board(columns)
                    if board == previous_board:
                        for column_index in range(3):
                            if column_index in locked_columns:
                                continue
                            if column_index in active_stop_plans:
                                plan = active_stop_plans[column_index]
                                self._spin_roleta_column(columns[column_index], next_top=plan.pop(0))
                                if not plan:
                                    active_stop_plans.pop(column_index, None)
                                    locked_columns.add(column_index)
                            else:
                                self._spin_roleta_column(columns[column_index])
                        board = self._render_roleta_board(columns)
                    previous_board = board
                    frame_embed = self._make_roleta_spin_embed(board, balance_text=balance_text, footer_text=footer_text, entry_cost=entry_cost, jackpot=jackpot)
                    rendered_message = await self._render_or_replace_game_message(message, spin_message, embed=frame_embed, view=disabled_view, final=False)
                    if rendered_message is not None:
                        spin_message = rendered_message
                finally:
                    if has_turn and guild_id is not None and session_id is not None:
                        await self._advance_game_animation_turn(guild_id, session_id)

            return spin_message, columns
        def _carta_window_total(self, bonus_spins: int = 0) -> int:
            return CARTA_SPIN_LIMIT + max(0, min(CARTA_DAILY_EXTRA_CAP, int(bonus_spins or 0)))

        async def _sync_carta_spin_window(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            now = time.time()
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                started_at = float(doc.get("carta_window_started_at", 0) or 0.0)
            except Exception:
                started_at = 0.0
            try:
                used = max(0, int(doc.get("carta_spins_used", 0) or 0))
            except Exception:
                used = 0
            try:
                bonus = max(0, min(CARTA_DAILY_EXTRA_CAP, int(doc.get("carta_bonus_spins", 0) or 0)))
            except Exception:
                bonus = 0
            changed = False
            if started_at <= 0 or (started_at + CARTA_WINDOW_SECONDS) <= now:
                started_at = now
                used = 0
                bonus = 0
                doc["carta_window_started_at"] = float(started_at)
                doc["carta_spins_used"] = 0
                doc["carta_bonus_spins"] = 0
                changed = True
            total = self._carta_window_total(bonus)
            available = max(0, total - used)
            reset_in = max(0.0, (started_at + CARTA_WINDOW_SECONDS) - now)
            if changed:
                await self.db._save_user_doc(guild_id, user_id, doc)
            return {
                "started_at": float(started_at),
                "used": int(used),
                "bonus": int(bonus),
                "total": int(total),
                "available": int(available),
                "reset_in": float(reset_in),
            }

        async def _consume_carta_spin(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            state = await self._sync_carta_spin_window(guild_id, user_id)
            if int(state["available"]) <= 0:
                return state
            doc = self.db._get_user_doc(guild_id, user_id)
            used = int(state["used"]) + 1
            doc["carta_window_started_at"] = float(state["started_at"])
            doc["carta_spins_used"] = used
            doc["carta_bonus_spins"] = int(state["bonus"])
            await self.db._save_user_doc(guild_id, user_id, doc)
            total = int(state["total"])
            return {
                "started_at": float(state["started_at"]),
                "used": used,
                "bonus": int(state["bonus"]),
                "total": total,
                "available": max(0, total - used),
                "reset_in": float(max(0.0, (float(state["started_at"]) + CARTA_WINDOW_SECONDS) - time.time())),
            }

        async def _grant_daily_carta_spin(self, guild_id: int, user_id: int) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_carta_spin_window(guild_id, user_id)
            current_bonus = int(state["bonus"])
            if current_bonus >= CARTA_DAILY_EXTRA_CAP:
                return False, state
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["carta_window_started_at"] = float(state["started_at"])
            doc["carta_spins_used"] = int(state["used"])
            doc["carta_bonus_spins"] = min(CARTA_DAILY_EXTRA_CAP, current_bonus + 1)
            await self.db._save_user_doc(guild_id, user_id, doc)
            return True, await self._sync_carta_spin_window(guild_id, user_id)

        def _carta_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
            available = int(state.get("available", 0) or 0)
            if available <= 0 and is_staff:
                return "Seus giros de cartas acabaram, mas como você é staff você ainda pode girar."
            giro_text = "giro de cartas" if available == 1 else "giros de cartas"
            return f"Resta {available} {giro_text} • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _carta_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
            total = max(CARTA_SPIN_LIMIT, int(state.get("total", CARTA_SPIN_LIMIT) or CARTA_SPIN_LIMIT))
            wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
            return "🎴 Sem giros por agora", f"Seus {total} giros de cartas acabaram. Reset em **{wait_text}**."

        async def _reserve_carta_spin_state(self, guild_id: int, user_id: int, *, is_staff: bool) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_carta_spin_window(guild_id, user_id)
            available = int(state.get("available", 0) or 0)
            if available <= 0:
                return bool(is_staff), state
            consumed = await self._consume_carta_spin(guild_id, user_id)
            return True, consumed

        def _pick_carta_result_flavor(self, result_kind: str, *, fallback: str = "") -> str:
            options = {
                "loss": [
                    "Essa mão não rendeu nada.",
                    "As cartas não encaixaram.",
                    "Dessa vez a mão passou em branco.",
                ],
                "return": [
                    "O coringa salvou parte da aposta.",
                    "O coringa evitou a perda completa.",
                    "O coringa segurou parte da rodada.",
                ],
                "partial": [
                    "Essa mão rendeu bem.",
                    "As cartas encaixaram.",
                    "Foi uma boa combinação.",
                ],
                "premium": [
                    "O coringa completou a combinação.",
                    "O coringa fechou a mão.",
                    "O coringa puxou a melhor carta da rodada.",
                ],
                "rare": [
                    "Essa mão veio forte.",
                    "As cartas bateram bonito.",
                    "Foi uma combinação rara.",
                ],
                "jackpot": [
                    "A mão bateu o prêmio máximo.",
                    "Você acertou a mão máxima.",
                    "As cartas vieram perfeitas.",
                ],
            }
            picks = options.get(result_kind)
            if picks:
                return random.choice(picks)
            return fallback or "Resultado das cartas."

        def _pick_carta_hot_streak_text(self) -> str:
            return random.choice([
                "Você entrou em boa fase.",
                "Sua mão esquentou.",
                "A sequência ficou forte.",
            ])

        async def _advance_carta_hot_streak(self, guild_id: int, user_id: int, *, result_kind: str) -> tuple[int, str | None]:
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                current = max(0, int(doc.get("carta_hot_streak", 0) or 0))
            except Exception:
                current = 0
            counts_for_streak = result_kind in {"partial", "premium", "rare", "jackpot"}
            new_value = current + 1 if counts_for_streak else 0
            doc["carta_hot_streak"] = int(new_value)
            await self.db._save_user_doc(guild_id, user_id, doc)
            if counts_for_streak and new_value >= 2:
                return new_value, self._pick_carta_hot_streak_text()
            return new_value, None

        def _format_carta_row(self, row: list[object], *, middle: bool = False) -> str:
            cells = [str(cell) for cell in row]
            row_text = f"{cells[0]}  {cells[1]}  {cells[2]}"
            if middle:
                return f" »{row_text}«"
            return f"│ {row_text}  "

        def _render_carta_board(self, columns: list[list[object]]) -> str:
            rows = [[columns[0][i], columns[1][i], columns[2][i]] for i in range(3)]
            lines = [
                "┌────────────┐",
                self._format_carta_row(rows[0]),
                "├────────────┤",
                self._format_carta_row(rows[1], middle=True),
                "├────────────┤",
                self._format_carta_row(rows[2]),
                "└────────────┘",
            ]
            return "```text\n" + "\n".join(lines) + "\n```"

        def _random_carta_symbol(self, exclude: set[object] | None = None) -> str:
            exclude = exclude or set()
            choices = [symbol for symbol in CARTA_SYMBOLS if symbol not in exclude]
            if not choices:
                choices = list(CARTA_SYMBOLS)
            weights = [CARTA_WEIGHTS[CARTA_SYMBOLS.index(symbol)] for symbol in choices]
            return random.choices(choices, weights=weights, k=1)[0]

        def _build_carta_column(self, middle: object | None = None) -> list[object]:
            return [
                self._random_carta_symbol(),
                middle if middle is not None else self._random_carta_symbol(),
                self._random_carta_symbol(),
            ]

        def _spin_carta_column(self, column: list[object], next_top: object | None = None):
            column.insert(0, self._random_carta_symbol() if next_top is None else next_top)
            del column[3:]

        def _make_carta_stop_plan(self, column: list[object], target_middle: object) -> list[object]:
            first_top = self._random_carta_symbol(exclude={target_middle, column[0], column[1], column[2]})
            final_top = self._random_carta_symbol(exclude={target_middle, first_top})
            return [first_top, target_middle, final_top]

        def _make_carta_spin_embed(self, board: str, *, balance_text: str, footer_text: str | None = None, entry_cost: int = CARTA_COST, jackpot: int = CARTA_JACKPOT_CHIPS) -> discord.Embed:
            embed = discord.Embed(
                title="🎴 Cartas embaralhando...",
                description=self._build_game_flow_description(entry_cost=entry_cost, jackpot=jackpot, balance_text=balance_text, board=board),
                color=discord.Color.from_rgb(111, 88, 242),
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _make_carta_result_embed(self, title: str, summary: str, board: str, *, balance_text: str, success: bool, premium: bool = False, footer_text: str | None = None, entry_cost: int = CARTA_COST, jackpot: int = CARTA_JACKPOT_CHIPS) -> discord.Embed:
            color = discord.Color.from_rgb(255, 201, 74) if premium else (discord.Color.from_rgb(88, 179, 104) if success else discord.Color(OFF_COLOR))
            embed = discord.Embed(
                title=title,
                description=self._build_game_flow_description(entry_cost=entry_cost, jackpot=jackpot, balance_text=balance_text, board=board, summary=summary),
                color=color,
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _roll_carta_target_middle(self) -> list[object]:
            roll = random.random()
            if roll < 0.02:
                return ["⭐", "⭐", "⭐"]
            if roll < 0.035:
                return ["🃏", "🃏", "🃏"]
            if roll < 0.065:
                base = random.choice(["👑", "💎", "🍀"])
                return [base, base, base]
            if roll < 0.14:
                base = random.choice(["⭐", "👑", "💎", "🍀"])
                middle = [base, base, "🃏"]
                random.shuffle(middle)
                return middle
            if roll < 0.30:
                base = random.choice(["⭐", "👑", "💎", "🍀"])
                other = self._random_carta_symbol(exclude={base, "🃏"})
                middle = [base, base, other]
                random.shuffle(middle)
                return middle
            if roll < 0.40:
                others = random.sample(["⭐", "👑", "💎", "🍀"], 2)
                middle = [others[0], others[1], "🃏"]
                random.shuffle(middle)
                return middle
            while True:
                middle = [self._random_carta_symbol() for _ in range(3)]
                if len(set(middle)) == 3 and middle.count("🃏") <= 1:
                    return middle

        def _evaluate_carta_middle(self, middle_symbols: list[object]) -> tuple[str, int, str]:
            symbols = [str(v) for v in middle_symbols]
            counts = {symbol: symbols.count(symbol) for symbol in set(symbols)}
            joker_count = counts.get("🃏", 0)
            star_count = counts.get("⭐", 0)
            if symbols == ["⭐", "⭐", "⭐"]:
                return "jackpot", CARTA_JACKPOT_CHIPS, "A mão bateu o prêmio máximo."
            if counts.get("🃏", 0) == 3:
                return "rare", 80, "Trinca de coringas na linha do meio."
            if any(count == 3 for symbol, count in counts.items() if symbol != "🃏"):
                triple_symbol = next(symbol for symbol, count in counts.items() if symbol != "🃏" and count == 3)
                values = {"👑": 50, "💎": 35, "🍀": 25, "⭐": 65}
                texts = {"👑": "Trinca de coroas.", "💎": "Trinca de diamantes.", "🍀": "Trinca de trevos.", "⭐": "Trinca rara de estrelas."}
                return "rare", values.get(triple_symbol, 25), texts.get(triple_symbol, "Trinca premiada.")
            pair_symbol = next((symbol for symbol, count in counts.items() if symbol != "🃏" and count == 2), None)
            if pair_symbol and joker_count == 1:
                values = {"⭐": 70, "👑": 40, "💎": 30, "🍀": 22}
                texts = {"⭐": "O coringa completou a mão máxima.", "👑": "O coringa completou a combinação.", "💎": "O coringa fechou a combinação.", "🍀": "O coringa ajudou a fechar a mão."}
                return "premium", values.get(pair_symbol, 20), texts.get(pair_symbol, "O coringa completou a combinação.")
            if joker_count == 2 and len(counts) == 2:
                other = next(symbol for symbol in counts if symbol != "🃏")
                values = {"⭐": 55, "👑": 32, "💎": 24, "🍀": 18}
                return "premium", values.get(other, 18), "Dois coringas puxaram a combinação."
            if pair_symbol:
                values = {"⭐": 20, "👑": 15, "💎": 12, "🍀": 10}
                texts = {"⭐": "Par raro na linha do meio.", "👑": "Par de coroas.", "💎": "Par de diamantes.", "🍀": "Par de trevos."}
                return "partial", values.get(pair_symbol, 10), texts.get(pair_symbol, "Par premiado.")
            if joker_count == 1 and len(counts) == 3:
                return "return", 10, "O coringa salvou parte da aposta."
            if star_count == 2:
                return "partial", 18, "Quase bateu a mão mais rara."
            return "loss", 0, "Essa mão não rendeu nada."

        async def _animate_carta_spin(self, message: discord.Message, *, target_middle: list[object], balance_text: str, footer_text: str | None = None, spin_message: discord.Message | None = None, owner_id: int | None = None, guild_id: int | None = None, session_id: str | None = None, entry_cost: int = CARTA_COST, jackpot: int = CARTA_JACKPOT_CHIPS) -> tuple[discord.Message | None, list[list[object]] | None]:
            columns = [self._build_carta_column() for _ in range(3)]
            for idx in range(3):
                if columns[idx][1] == target_middle[idx]:
                    reroll = self._build_carta_column()
                    while reroll[1] == target_middle[idx]:
                        reroll = self._build_carta_column()
                    columns[idx] = reroll
            disabled_view = _GameReplayView(self, owner_id=owner_id or getattr(message.author, "id", 0), kind="cartas", enabled=False) if owner_id else None
            opening_embed = self._make_carta_spin_embed(self._render_carta_board(columns), balance_text=balance_text, footer_text=footer_text, entry_cost=entry_cost, jackpot=jackpot)
            spin_message = await self._render_or_replace_game_message(message, spin_message, embed=opening_embed, view=disabled_view, final=False)
            if spin_message is None:
                return None, None
            target_duration = 4.6
            intervals = [0.18, 0.21, 0.25, 0.30, 0.36, 0.44, 0.55, 0.70, 0.90, 1.05]
            scale = target_duration / sum(intervals)
            intervals = [step * scale for step in intervals]
            stop_plan_starts = {0: max(0, len(intervals)-5), 1: max(0, len(intervals)-4), 2: max(0, len(intervals)-3)}
            active_stop_plans: dict[int, list[object]] = {}
            locked_columns: set[int] = set()
            previous_board = None
            for index, delay in enumerate(intervals):
                await asyncio.sleep(delay)
                has_turn = False
                try:
                    if guild_id is not None and session_id is not None:
                        has_turn = await self._wait_for_game_animation_turn(guild_id, session_id)
                        if not has_turn:
                            continue
                    for column_index, start_index in stop_plan_starts.items():
                        if index == start_index and column_index not in active_stop_plans and column_index not in locked_columns:
                            active_stop_plans[column_index] = self._make_carta_stop_plan(columns[column_index], target_middle[column_index])
                    for column_index in range(3):
                        if column_index in locked_columns:
                            continue
                        if column_index in active_stop_plans:
                            plan = active_stop_plans[column_index]
                            self._spin_carta_column(columns[column_index], next_top=plan.pop(0))
                            if not plan:
                                active_stop_plans.pop(column_index, None)
                                locked_columns.add(column_index)
                        else:
                            self._spin_carta_column(columns[column_index])
                    board = self._render_carta_board(columns)
                    if board == previous_board:
                        for column_index in range(3):
                            if column_index in locked_columns:
                                continue
                            if column_index in active_stop_plans:
                                plan = active_stop_plans[column_index]
                                self._spin_carta_column(columns[column_index], next_top=plan.pop(0))
                                if not plan:
                                    active_stop_plans.pop(column_index, None)
                                    locked_columns.add(column_index)
                            else:
                                self._spin_carta_column(columns[column_index])
                        board = self._render_carta_board(columns)
                    previous_board = board
                    frame_embed = self._make_carta_spin_embed(board, balance_text=balance_text, footer_text=footer_text, entry_cost=entry_cost, jackpot=jackpot)
                    rendered_message = await self._render_or_replace_game_message(message, spin_message, embed=frame_embed, view=disabled_view, final=False)
                    if rendered_message is not None:
                        spin_message = rendered_message
                finally:
                    if has_turn and guild_id is not None and session_id is not None:
                        await self._advance_game_animation_turn(guild_id, session_id)
            return spin_message, columns
        async def _execute_roleta_round(self, *, source_message: discord.Message, guild: discord.Guild, actor: discord.abc.User, roleta_footer: str, chip_note: str | None, voice_channel: discord.abc.Connectable | None, targets: list[discord.Member], session_id: str, spin_message: discord.Message | None = None, entry_cost: int = ROLETA_COST, entry_spend: dict | None = None) -> bool:
            success = False
            outcome = self._roleta_outcome_for_user(guild.id, actor.id)
            success = str(outcome.get("forced_kind") or "") in {"jackpot", "jackpot_mega"}
            await self.db.add_user_game_stat(guild.id, actor.id, "roleta_spins", 1)
            target_middle = list(outcome.get("target_middle") or [7, 7, 7])
            spin_balance_text = self._format_compact_chip_balance(guild.id, actor.id)
            jackpot_preview = self._roleta_jackpot_preview(guild.id, actor.id)
            spin_message, final_columns = await self._animate_roleta_spin(source_message, target_middle=target_middle, balance_text=spin_balance_text, footer_text=roleta_footer, spin_message=spin_message, owner_id=actor.id, guild_id=guild.id, session_id=session_id, entry_cost=entry_cost, jackpot=jackpot_preview)
            if final_columns is None:
                final_columns = [self._build_roleta_column(target_middle[0]), self._build_roleta_column(target_middle[1]), self._build_roleta_column(target_middle[2])]
            try:
                board = self._render_roleta_board(final_columns)
                middle_digits = [column[1] for column in final_columns]
                result_kind, result_amount = self._evaluate_roleta_middle(middle_digits, guild_id=guild.id, user_id=actor.id)
                race_won: bool | None = None
                race_payout = 0
                if result_kind in {"jackpot", "jackpot_mega"}:
                    race_won = True
                    race_payout = int(result_amount)
                    chosen_channel = voice_channel if targets and isinstance(voice_channel, discord.VoiceChannel) else None
                    if chosen_channel is not None:
                        try:
                            await self._play_roleta_sfx(guild, chosen_channel)
                        except Exception:
                            pass
                        await asyncio.sleep(0.20)
                    for target in targets:
                        if target.voice and target.voice.channel:
                            try:
                                await target.move_to(None, reason="economia roleta")
                            except Exception:
                                pass
                    await self._record_game_played(guild.id, actor.id, weekly_points=12)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    await self.db.add_user_game_stat(guild.id, actor.id, "roleta_jackpots", 1)
                    await self._grant_weekly_points(guild.id, actor.id, 20)
                    summary = f"**Prêmio:** {self._chip_text(result_amount, kind='gain')}."
                    effect_note = ""
                    if self._race_is(guild.id, actor.id, "apostador"):
                        effect_note = self._race_effect_message(guild.id, actor.id, "all_in" if result_kind == "jackpot_mega" else "jackpot")
                    if effect_note:
                        summary = f"{effect_note}\n{summary}"
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed("🎰 Jackpot 777!" if result_kind == "jackpot_mega" else ("🎰 Jackpot 999!" if self._race_is(guild.id, actor.id, "apostador") else "🎰 Jackpot!"), summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=True, footer_text=roleta_footer, entry_cost=entry_cost, jackpot=jackpot_preview)
                elif result_kind == "joker_premium":
                    race_won = True
                    race_payout = int(result_amount)
                    await self._record_game_played(guild.id, actor.id, weekly_points=6)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    await self._grant_weekly_points(guild.id, actor.id, 8)
                    summary = f"O símbolo coringa completou a combinação.\n**Prêmio:** {self._chip_text(result_amount, kind='gain')}."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed("🎰 Coringa premiado", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=False, near=True, footer_text=roleta_footer, entry_cost=entry_cost, jackpot=jackpot_preview)
                elif result_kind == "partial":
                    race_won = True
                    race_payout = int(result_amount)
                    await self._record_game_played(guild.id, actor.id, weekly_points=4)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    await self._grant_weekly_points(guild.id, actor.id, 6)
                    summary = f"Você acertou uma combinação parcial.\n**Prêmio:** {self._chip_text(result_amount, kind='gain')}."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed("🎰 Giro parcial", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=False, near=True, footer_text=roleta_footer, entry_cost=entry_cost, jackpot=jackpot_preview)
                elif result_kind == "return":
                    race_won = None
                    race_payout = int(result_amount)
                    await self._record_game_played(guild.id, actor.id, weekly_points=3)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    summary = f"**Custo recuperado:** {self._chip_text(result_amount, kind='gain')}."
                    effect_note = self._race_effect_message(guild.id, actor.id, "666") if self._race_is(guild.id, actor.id, "apostador") else ""
                    if effect_note:
                        summary = f"{effect_note}\n{summary}"
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed("🎰 Custo recuperado", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=False, near=True, footer_text=roleta_footer, entry_cost=entry_cost, jackpot=jackpot_preview)
                else:
                    race_won = False
                    race_payout = 0
                    await self._record_game_played(guild.id, actor.id, weekly_points=2)
                    refund = await self._maybe_apply_coringa_cashback(guild.id, actor.id, entry_cost, chance=0.5)
                    summary = ""
                    if refund > 0:
                        effect_note = self._race_effect_message(guild.id, actor.id, "redencao", f"você recuperou {self._chip_text(refund, kind='gain')} do custo do giro.")
                        final_loss = max(0, int(entry_cost) - int(refund))
                        summary = f"{effect_note or ('Você recuperou ' + self._chip_text(refund, kind='gain') + '.')}\n**Perda final:** {self._chip_text(final_loss, kind='loss')}."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}" if summary else chip_note
                    embed = self._make_roleta_result_embed("🎰 Sem prêmio neste giro", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=False, footer_text=roleta_footer, entry_cost=entry_cost, jackpot=jackpot_preview)
                race_notes = await self._apply_new_race_result(
                    guild.id,
                    actor.id,
                    won=race_won,
                    entry_spend=entry_spend,
                    payout=race_payout,
                    opponent_ids=(),
                    valid=True,
                    allow_hunt=False,
                )
                if race_notes:
                    current_balance = self._format_compact_chip_balance(guild.id, actor.id)
                    description_lines = str(embed.description or "").splitlines()
                    description_lines = [f"**Saldo atual:** {current_balance}" if line.startswith("**Saldo atual:**") else line for line in description_lines]
                    embed.description = "\n".join([*race_notes, "", *description_lines])
            except Exception:
                fallback_title = "🎰 Jackpot!" if success else "🎰 Sem prêmio neste giro"
                fallback_text = f"**Prêmio:** {self._chip_text(jackpot_preview, kind='gain')}." if success else f"**Custo do giro:** {self._chip_text(entry_cost, kind='loss')}."
                if chip_note:
                    fallback_text = f"{chip_note}\n{fallback_text}"
                embed = self._make_embed(fallback_title, fallback_text, ok=success)
                try:
                    embed.set_footer(text=roleta_footer)
                except Exception:
                    pass
            await self._deliver_game_result(source_message, spin_message, embed=embed, view=None)
            return True

        async def _execute_carta_round(self, *, source_message: discord.Message, guild: discord.Guild, actor: discord.abc.User, carta_footer: str, chip_note: str | None, session_id: str, spin_message: discord.Message | None = None, entry_cost: int = CARTA_COST, entry_spend: dict | None = None) -> bool:
            target_middle = self._roll_carta_target_middle()
            spin_balance_text = self._format_compact_chip_balance(guild.id, actor.id)
            spin_message, final_columns = await self._animate_carta_spin(source_message, target_middle=target_middle, balance_text=spin_balance_text, footer_text=carta_footer, spin_message=spin_message, owner_id=actor.id, guild_id=guild.id, session_id=session_id, entry_cost=entry_cost, jackpot=CARTA_JACKPOT_CHIPS)
            if final_columns is None:
                final_columns = [self._build_carta_column(target_middle[0]), self._build_carta_column(target_middle[1]), self._build_carta_column(target_middle[2])]
            board = self._render_carta_board(final_columns)
            middle = [column[1] for column in final_columns]
            result_kind, result_amount, flavor = self._evaluate_carta_middle(middle)
            race_won: bool | None = None
            race_payout = 0
            await self.db.add_user_game_stat(guild.id, actor.id, "carta_spins", 1)
            flavor = self._pick_carta_result_flavor(result_kind, fallback=flavor)
            _streak_value, streak_line = await self._advance_carta_hot_streak(guild.id, actor.id, result_kind=result_kind)
            if result_kind == "jackpot":
                race_won = True
                race_payout = CARTA_JACKPOT_CHIPS
                await self._record_game_played(guild.id, actor.id, weekly_points=12)
                await self._change_user_chips(guild.id, actor.id, CARTA_JACKPOT_CHIPS, reason="Prêmio das cartas")
                await self.db.add_user_game_stat(guild.id, actor.id, "cartas_jackpots", 1)
                await self._grant_weekly_points(guild.id, actor.id, 18)
                summary = f"{flavor}\n**Prêmio:** {self._chip_text(CARTA_JACKPOT_CHIPS, kind='gain')}."
                if streak_line:
                    summary = f"{summary}\n*{streak_line}*"
                if chip_note:
                    summary = f"{chip_note}\n{summary}"
                embed = self._make_carta_result_embed("🎴 Jackpot!", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=True, premium=True, footer_text=carta_footer, entry_cost=entry_cost, jackpot=CARTA_JACKPOT_CHIPS)
            elif result_kind in {"rare", "premium", "partial", "return"}:
                race_won = None if result_kind == "return" else True
                race_payout = int(result_amount)
                weekly_map = {"rare": 8, "premium": 7, "partial": 4, "return": 2}
                await self._record_game_played(guild.id, actor.id, weekly_points=weekly_map.get(result_kind, 3))
                await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio das cartas")
                if result_kind in {"rare", "premium"}:
                    await self._grant_weekly_points(guild.id, actor.id, 6)
                line = f"{flavor}\n**Prêmio:** {self._chip_text(result_amount, kind='gain')}."
                if result_kind == "return":
                    line = f"{flavor}\n**Custo recuperado:** {self._chip_text(result_amount, kind='gain')}."
                    if self._race_is(guild.id, actor.id, "apostador"):
                        effect_note = self._race_effect_message(guild.id, actor.id, "666")
                        if effect_note:
                            line = f"{effect_note}\n{line}"
                elif streak_line:
                    line = f"{line}\n*{streak_line}*"
                if chip_note:
                    line = f"{chip_note}\n{line}"
                titles = {"rare": "🎴 Mão rara", "premium": "🎴 Coringa premiado", "partial": "🎴 Mão premiada", "return": "🎴 Custo recuperado"}
                embed = self._make_carta_result_embed(titles.get(result_kind, "🎴 Boa mão"), line, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=True, premium=result_kind in {"rare", "premium"}, footer_text=carta_footer, entry_cost=entry_cost, jackpot=CARTA_JACKPOT_CHIPS)
            else:
                race_won = False
                race_payout = 0
                await self._record_game_played(guild.id, actor.id, weekly_points=2)
                refund = await self._maybe_apply_coringa_cashback(guild.id, actor.id, entry_cost, chance=0.5)
                summary = flavor
                if refund > 0:
                    effect_note = self._race_effect_message(guild.id, actor.id, "redencao", f"você recuperou {self._chip_text(refund, kind='gain')} do custo da mão.")
                    final_loss = max(0, int(entry_cost) - int(refund))
                    summary = f"{effect_note or ('Você recuperou ' + self._chip_text(refund, kind='gain') + '.')}\n**Perda final:** {self._chip_text(final_loss, kind='loss')}."
                if chip_note:
                    summary = f"{chip_note}\n{summary}" if summary else chip_note
                embed = self._make_carta_result_embed("🎴 Sem prêmio nesta mão", summary, board, balance_text=self._format_compact_chip_balance(guild.id, actor.id), success=False, premium=False, footer_text=carta_footer, entry_cost=entry_cost, jackpot=CARTA_JACKPOT_CHIPS)
            race_notes = await self._apply_new_race_result(
                guild.id,
                actor.id,
                won=race_won,
                entry_spend=entry_spend,
                payout=race_payout,
                opponent_ids=(),
                valid=True,
                allow_hunt=False,
            )
            if race_notes:
                current_balance = self._format_compact_chip_balance(guild.id, actor.id)
                description_lines = str(embed.description or "").splitlines()
                description_lines = [f"**Saldo atual:** {current_balance}" if line.startswith("**Saldo atual:**") else line for line in description_lines]
                embed.description = "\n".join([*race_notes, "", *description_lines])
            replay_view = _GameReplayView(self, owner_id=actor.id, kind="cartas", enabled=True)
            await self._deliver_game_result(source_message, spin_message, embed=embed, view=replay_view)
            return True
        async def _handle_carta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False
            content = (message.content or "").strip().casefold()
            if content not in {"carta", "cartas"}:
                return False
            if not self.db.gincana_enabled(guild.id):
                return True
            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
            carta_state = await self._sync_carta_spin_window(guild.id, message.author.id)
            if int(carta_state.get("available", 0) or 0) <= 0 and not is_staff:
                try:
                    title, desc = self._carta_spin_message_text(carta_state)
                    embed = discord.Embed(title=title, description=desc, color=discord.Color(OFF_COLOR))
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                return True

            entry_cost = CARTA_COST
            blessing_state = await self._sync_sortudo_blessings(guild.id, message.author.id)
            free_spin = self._race_is(guild.id, message.author.id, "sortudo") and int(blessing_state.get("charges", 0) or 0) > 0
            needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(guild.id, message.author.id, entry_cost)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, entry_cost, title="🎴 Confirmar aposta")
                if not confirmed:
                    return True

            session_id = self._next_game_animation_session_id(guild_id=guild.id, kind="cartas", owner_id=message.author.id)
            if not await self._try_acquire_game_animation_slot(guild.id, session_id):
                await self._send_animation_limit_message(message, title="🎴 Aguarde um pouco")
                return True

            try:
                can_spin, carta_state = await self._reserve_carta_spin_state(guild.id, message.author.id, is_staff=is_staff)
                if not can_spin:
                    try:
                        title, desc = self._carta_spin_message_text(carta_state)
                        await message.channel.send(embed=self._make_embed(title, desc, ok=False))
                    except Exception:
                        pass
                    return True
                carta_footer = self._carta_footer_text(state=carta_state, is_staff=is_staff)
                if free_spin:
                    await self._consume_sortudo_blessing(guild.id, message.author.id)
                    chip_note = self._sortudo_blessing_note(guild.id, message.author.id, kind="carta")
                    entry_spend = {"chips": 0, "bonus": 0}
                    paid, _balance = True, self.db.get_user_chips(guild.id, message.author.id, default=100)
                else:
                    entry_spend = self._entry_spend_parts(guild.id, message.author.id, entry_cost)
                    paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, entry_cost, reason="Entrada na roleta")
                if needs_negative_confirm:
                    chip_note = None
                if not paid:
                    try:
                        await message.channel.send(embed=self._make_embed("🎴 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
                    except Exception:
                        pass
                    return True
                await self._execute_carta_round(source_message=message, guild=guild, actor=message.author, carta_footer=carta_footer, chip_note=chip_note, session_id=session_id, entry_cost=entry_cost, entry_spend=entry_spend)
                return True
            finally:
                await self._release_game_animation_slot(guild.id, session_id)
        async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "roleta"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True
            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            cooldown_remaining = self._roleta_trigger_cooldown_remaining(guild.id, message.author.id)
            if cooldown_remaining > 0:
                try:
                    await message.channel.send(embed=self._make_embed("🎰 Aguarde um pouco", f"Espere **{int(cooldown_remaining) + 1}s** para usar a roleta novamente.", ok=False))
                except Exception:
                    pass
                return True

            is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
            roleta_state = await self._sync_roleta_spin_window(guild.id, message.author.id)
            if int(roleta_state.get("available", 0) or 0) <= 0 and not is_staff:
                try:
                    title, desc = self._roleta_spin_message_text(roleta_state)
                    embed = discord.Embed(title=title, description=desc, color=discord.Color(OFF_COLOR))
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                return True

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            targets = self._resolve_targets(guild, voice_channel) if isinstance(voice_channel, discord.VoiceChannel) else []

            entry_cost = self._roleta_cost_for_user(guild.id, message.author.id)
            blessing_state = await self._sync_sortudo_blessings(guild.id, message.author.id)
            free_spin = self._race_is(guild.id, message.author.id, "sortudo") and int(blessing_state.get("charges", 0) or 0) > 0
            needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(guild.id, message.author.id, entry_cost)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, entry_cost, title="🎰 Confirmar aposta")
                if not confirmed:
                    return True

            session_id = self._next_game_animation_session_id(guild_id=guild.id, kind="roleta", owner_id=message.author.id)
            if not await self._try_acquire_game_animation_slot(guild.id, session_id):
                await self._send_animation_limit_message(message, title="🎰 Aguarde um pouco")
                return True

            try:
                can_spin, roleta_state = await self._reserve_roleta_spin_state(guild.id, message.author.id, is_staff=is_staff)
                if not can_spin:
                    try:
                        title, desc = self._roleta_spin_message_text(roleta_state)
                        await message.channel.send(embed=self._make_embed(title, desc, ok=False))
                    except Exception:
                        pass
                    return True
                roleta_footer = self._roleta_footer_text(state=roleta_state, is_staff=is_staff)
                if free_spin:
                    await self._consume_sortudo_blessing(guild.id, message.author.id)
                    chip_note = self._sortudo_blessing_note(guild.id, message.author.id, kind="roleta")
                    entry_spend = {"chips": 0, "bonus": 0}
                    paid, _balance = True, self.db.get_user_chips(guild.id, message.author.id, default=100)
                else:
                    entry_spend = self._entry_spend_parts(guild.id, message.author.id, entry_cost)
                    paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, entry_cost, reason="Entrada nas cartas")
                if needs_negative_confirm:
                    chip_note = None
                if not paid:
                    try:
                        await message.channel.send(embed=self._make_embed("🎰 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
                    except Exception:
                        pass
                    return True
                self._mark_roleta_trigger_used(guild.id, message.author.id)
                await self._execute_roleta_round(source_message=message, guild=guild, actor=message.author, roleta_footer=roleta_footer, chip_note=chip_note, voice_channel=voice_channel, targets=targets, session_id=session_id, entry_cost=entry_cost, entry_spend=entry_spend)
                return True
            finally:
                await self._release_game_animation_slot(guild.id, session_id)
