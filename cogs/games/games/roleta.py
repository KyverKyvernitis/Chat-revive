import asyncio
import logging
import random
import time
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
    CHIPS_INITIAL,
    ROLETA_APOSTADOR_COST,
    ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS,
    ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS,
    ROLETA_COST,
    ROLETA_JACKPOT_CHIPS,
)


ROLETA_JOKERS = ("🃏",)
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
GAME_ANIMATION_LIMIT_PER_GUILD = 2
GAME_ANIMATION_STALE_SECONDS = 75.0
ROLETA_DYNAMIC_JACKPOT_BASE = ROLETA_JACKPOT_CHIPS
ROLETA_DYNAMIC_JACKPOT_MAX = 200
ROLETA_DYNAMIC_JACKPOT_LOSS_INCREMENT = 1
ROLETA_CYCLE_BONUS_CHIPS = 10
CARTA_ANIMATION_FRAME_SECONDS = 0.42
CARTA_ANIMATION_MIN_STOP_SECONDS = 2.05
CARTA_ANIMATION_MAX_SECONDS = 4.0
CARTA_ANIMATION_LAST_STOP_SECONDS = 3.80
ROLETA_ANIMATION_DURATION_SECONDS = 5.0
ROLETA_ANIMATION_INTERVAL_WEIGHTS = (0.18, 0.21, 0.24, 0.28, 0.33, 0.39, 0.47, 0.58, 0.72, 0.90, 1.05)
ROLETA_ANIMATION_MIN_STOP_SECONDS = 2.05
ROLETA_ANIMATION_LAST_STOP_SECONDS = 4.80


class _GameDebtConfirmView(discord.ui.LayoutView):
    def __init__(self, cog, *, owner_id: int, title: str, note: str, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.title = str(title)
        self.note = str(note)
        self.confirmed = False
        self.message: discord.Message | None = None
        self._finished = False
        self.confirm_button = discord.ui.Button(label="Continuar", style=discord.ButtonStyle.danger)
        self.cancel_button = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        self.confirm_button.callback = self._confirm
        self.cancel_button.callback = self._cancel
        self._rebuild()

    def _rebuild(self, status: str | None = None):
        for item in list(self.children):
            self.remove_item(item)
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"# {self.title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(status or self.note),
        ]
        if not self._finished:
            children.extend([
                discord.ui.Separator(),
                discord.ui.ActionRow(self.confirm_button, self.cancel_button),
            ])
        self.add_item(discord.ui.Container(*children, accent_color=discord.Color.red()))

    async def _reject_other_user(self, interaction: discord.Interaction):
        notice = self.cog._make_game_notice_view(
            "⚠️ Confirmação indisponível",
            "Essa confirmação não é para você",
            ok=False,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=notice, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(view=notice, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def _finish(self, interaction: discord.Interaction, *, confirmed: bool):
        if int(interaction.user.id) != self.owner_id:
            await self._reject_other_user(interaction)
            return
        self.confirmed = bool(confirmed)
        self._finished = True
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        self._rebuild("Entrada confirmada" if confirmed else "Entrada cancelada")
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
        self.stop()

    async def _confirm(self, interaction: discord.Interaction):
        await self._finish(interaction, confirmed=True)

    async def _cancel(self, interaction: discord.Interaction):
        await self._finish(interaction, confirmed=False)

    async def on_timeout(self):
        self._finished = True
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        self._rebuild("A confirmação expirou")
        try:
            if self.message is not None:
                await self.message.edit(view=self)
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
        def _spin_roleta_column(self, column: list[object], *, next_top: object | None = None):
            # A coluna sempre avança de cima para baixo. ``next_top`` permite
            # preparar o resultado na linha superior e, no frame seguinte,
            # deixá-lo descer naturalmente até a linha central.
            column.insert(0, self._random_roleta_digit() if next_top is None else next_top)
            del column[3:]
        def _roleta_partial_result_copy(self, middle_digits: list[object]) -> tuple[str, str]:
            values = list(middle_digits)
            if len(values) == 3 and len(set(values)) == 1:
                return (
                    "🎰 3 números iguais??",
                    "Incrível, mas você não vai ganhar nada especial por causa disso >:)",
                )

            seven_positions = {index for index, value in enumerate(values) if value == 7}
            if seven_positions == {0, 1}:
                return "🎰 Eeeeee... Nada!! haha", "Bem perto hein"
            if seven_positions in ({0, 2}, {1, 2}):
                return "🎰 Nossa foi quase hein", "Você acertou dois 7, parabains"

            return (
                "🎰 Números iguais",
                "Tem dois números iguais então vou te devolver um pouco da entrada",
            )

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
        def _make_game_layout_view(
            self,
            title: str,
            *,
            details: list[str] | tuple[str, ...] = (),
            board: str | None = None,
            summary: str | None = None,
            footer_text: str | None = None,
            color: discord.Color | None = None,
        ) -> discord.ui.LayoutView:
            children: list[discord.ui.Item] = [discord.ui.TextDisplay(f"# {title}")]
            clean_summary = str(summary or "").strip()
            if clean_summary:
                children.extend([discord.ui.Separator(), discord.ui.TextDisplay(clean_summary)])
            clean_details = [str(line).strip() for line in details if str(line).strip()]
            if clean_details:
                children.extend([discord.ui.Separator(), discord.ui.TextDisplay("\n".join(clean_details))])
            if board:
                children.extend([discord.ui.Separator(), discord.ui.TextDisplay(str(board))])
            clean_footer = str(footer_text or "").strip()
            if clean_footer:
                children.extend([discord.ui.Separator(), discord.ui.TextDisplay(f"-# {clean_footer}")])
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(discord.ui.Container(*children, accent_color=color or discord.Color.blurple()))
            return view


        def _make_game_notice_view(self, title: str, description: str, *, ok: bool = True) -> discord.ui.LayoutView:
            return self._make_game_layout_view(
                title,
                summary=str(description or "").strip() or "Nada mudou",
                color=discord.Color.green() if ok else discord.Color(OFF_COLOR),
            )


        def _entry_paid_amount(self, entry_spend: dict | None, fallback: int) -> int:
            if isinstance(entry_spend, dict):
                try:
                    return max(0, int(entry_spend.get("chips", 0) or 0) + int(entry_spend.get("bonus", 0) or 0))
                except Exception:
                    return max(0, int(fallback))
            return max(0, int(fallback))


        def _format_game_entry_value(self, paid_entry: int) -> str:
            if int(paid_entry) <= 0:
                return "**Jogada grátis**"
            return self._chip_text(int(paid_entry), kind="loss")


        def _format_game_result_value(self, result_delta: int) -> str:
            result = int(result_delta)
            if result > 0:
                return f"**+{result} {self._CHIP_GAIN_EMOJI}**"
            if result < 0:
                return f"**{result} {self._CHIP_LOSS_EMOJI}**"
            return f"**0 {self._CHIP_EMOJI}**"


        def _current_game_chip_total(self, guild_id: int, user_id: int) -> int:
            normal = int(self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL) or 0)
            bonus = int(self._get_user_bonus_chips(guild_id, user_id) or 0)
            return normal + bonus


        def _pick_game_loss_title(self, kind: str) -> str:
            self._ensure_game_animation_runtime()
            options = {
                "roleta": ("🎰 Nada neste giro", "🎰 Não foi dessa vez", "🎰 Sem prêmio", "🎰 Você ganhou... Nada!"),
                "cartas": ("🎴 Nada nesta mão", "🎴 As cartas não encaixaram", "🎴 Mão sem prêmio", "🎴 Passou em branco"),
            }.get(str(kind), ("Sem prêmio",))
            last = self._last_game_loss_titles.get(str(kind))
            available = [item for item in options if item != last] or list(options)
            chosen = random.choice(available)
            self._last_game_loss_titles[str(kind)] = chosen
            return chosen


        def _make_random_column_stop_plan(
            self,
            *,
            min_stop_seconds: float = CARTA_ANIMATION_MIN_STOP_SECONDS,
            first_stop_max_seconds: float = 2.30,
            last_stop_min_seconds: float = 3.45,
            last_stop_max_seconds: float = CARTA_ANIMATION_LAST_STOP_SECONDS,
            randomize_order: bool = True,
        ) -> list[tuple[float, int]]:
            column_order = [0, 1, 2]
            if randomize_order:
                random.shuffle(column_order)

            first_stop = random.uniform(float(min_stop_seconds), float(first_stop_max_seconds))
            last_stop_floor = max(first_stop + 0.60, float(last_stop_min_seconds))
            last_stop = random.uniform(last_stop_floor, float(last_stop_max_seconds))
            middle_ratio = random.uniform(0.35, 0.65)
            middle_stop = first_stop + ((last_stop - first_stop) * middle_ratio)

            return [
                (float(first_stop), int(column_order[0])),
                (float(middle_stop), int(column_order[1])),
                (float(last_stop), int(column_order[2])),
            ]

        def _compose_game_animation_columns(
            self,
            rolling_columns: list[list[object]],
            final_columns: list[list[object]],
            locked_columns: set[int],
        ) -> list[list[object]]:
            frame_columns = [list(column) for column in rolling_columns]
            for column_index in locked_columns:
                frame_columns[column_index] = list(final_columns[column_index])
            return frame_columns


        def _roleta_animation_intervals(self) -> list[float]:
            total_weight = sum(ROLETA_ANIMATION_INTERVAL_WEIGHTS) or 1.0
            scale = ROLETA_ANIMATION_DURATION_SECONDS / total_weight
            return [float(weight) * scale for weight in ROLETA_ANIMATION_INTERVAL_WEIGHTS]


        def _current_roleta_dynamic_jackpot(self, guild_id: int) -> int:
            try:
                doc = self.db._get_guild_doc(int(guild_id))
                value = int(doc.get("roleta_dynamic_jackpot", ROLETA_DYNAMIC_JACKPOT_BASE) or ROLETA_DYNAMIC_JACKPOT_BASE)
            except Exception:
                value = ROLETA_DYNAMIC_JACKPOT_BASE
            return max(ROLETA_DYNAMIC_JACKPOT_BASE, min(ROLETA_DYNAMIC_JACKPOT_MAX, value))


        def _roleta_jackpot_lock(self, guild_id: int) -> asyncio.Lock:
            self._ensure_game_animation_runtime()
            lock = self._roleta_jackpot_locks.get(int(guild_id))
            if lock is None:
                lock = asyncio.Lock()
                self._roleta_jackpot_locks[int(guild_id)] = lock
            return lock


        async def _claim_roleta_dynamic_jackpot(self, guild_id: int) -> int:
            async with self._roleta_jackpot_lock(guild_id):
                doc = self.db._get_guild_doc(int(guild_id))
                amount = self._current_roleta_dynamic_jackpot(guild_id)
                doc["roleta_dynamic_jackpot"] = ROLETA_DYNAMIC_JACKPOT_BASE
                await self.db._save_guild_doc(int(guild_id), doc)
                return int(amount)


        async def _increase_roleta_dynamic_jackpot(self, guild_id: int) -> int:
            async with self._roleta_jackpot_lock(guild_id):
                doc = self.db._get_guild_doc(int(guild_id))
                current = self._current_roleta_dynamic_jackpot(guild_id)
                updated = min(ROLETA_DYNAMIC_JACKPOT_MAX, current + ROLETA_DYNAMIC_JACKPOT_LOSS_INCREMENT)
                doc["roleta_dynamic_jackpot"] = int(updated)
                await self.db._save_guild_doc(int(guild_id), doc)
                return int(updated)


        def _roleta_cycle_bonus_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
            self._ensure_game_animation_runtime()
            key = (int(guild_id), int(user_id))
            lock = self._roleta_cycle_bonus_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._roleta_cycle_bonus_locks[key] = lock
            return lock


        async def _maybe_grant_roleta_cycle_bonus(self, guild_id: int, user_id: int) -> str | None:
            async with self._roleta_cycle_bonus_lock(guild_id, user_id):
                state = await self._sync_roleta_spin_window(guild_id, user_id)
                if int(state.get("used", 0) or 0) < ROLETA_SPIN_LIMIT:
                    return None
                started_at = float(state.get("started_at", 0.0) or 0.0)
                doc = self.db._get_user_doc(guild_id, user_id)
                try:
                    rewarded_window = float(doc.get("roleta_cycle_bonus_window_started_at", 0.0) or 0.0)
                except Exception:
                    rewarded_window = 0.0
                if started_at <= 0 or abs(rewarded_window - started_at) < 0.001:
                    return None
                doc["roleta_cycle_bonus_window_started_at"] = started_at
                doc["bonus_chips"] = max(0, int(doc.get("bonus_chips", 0) or 0)) + ROLETA_CYCLE_BONUS_CHIPS
                await self.db._save_user_doc(guild_id, user_id, doc)
                try:
                    await self._mark_chip_activity(guild_id, user_id)
                    await self.db.append_chip_history(
                        guild_id,
                        user_id,
                        delta=ROLETA_CYCLE_BONUS_CHIPS,
                        kind="bonus",
                        reason="Ciclo completo da roleta",
                    )
                except Exception:
                    pass
                return f"🎁 **Ciclo concluído:** +{ROLETA_CYCLE_BONUS_CHIPS} {self._CHIP_BONUS_EMOJI}"
        def _roleta_jackpot_preview(self, guild_id: int, user_id: int) -> int:
            if self._race_is(guild_id, user_id, "apostador"):
                return ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS
            return self._current_roleta_dynamic_jackpot(guild_id)
        def _roleta_outcome_for_user(self, guild_id: int, user_id: int) -> dict[str, object]:
            if self._race_is(guild_id, user_id, "apostador"):
                roll = random.random()
                if roll < 0.05:
                    return {"target_middle": [7, 7, 7], "forced_kind": "jackpot_mega", "forced_amount": ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS}
                if roll < 0.20:
                    return {"target_middle": [9, 9, 9], "forced_kind": "jackpot", "forced_amount": ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS}
                if random.random() < 0.25:
                    return {"target_middle": [6, 6, 6], "forced_kind": "beast", "forced_amount": ROLETA_APOSTADOR_COST}
                return {
                    "target_middle": self._roll_roleta_target_middle(success=False, excluded_special_triples={6, 9}),
                    "forced_kind": None,
                    "forced_amount": None,
                }
            success = random.randint(1, 10) == 1
            return {
                "target_middle": self._roll_roleta_target_middle(success=success),
                "forced_kind": "jackpot" if success else None,
                "forced_amount": None,
            }
        def _make_roleta_spin_view(
            self,
            board: str,
            *,
            balance_text: str,
            footer_text: str | None = None,
            paid_entry: int = ROLETA_COST,
            jackpot: int = ROLETA_JACKPOT_CHIPS,
        ) -> discord.ui.LayoutView:
            details = [
                f"**Entrada:** {self._format_game_entry_value(paid_entry)}",
            ]
            if int(jackpot) > ROLETA_DYNAMIC_JACKPOT_BASE:
                details.append(f"**Jackpot:** {self._chip_text(jackpot, kind='gain')}")
            details.append(f"**Saldo atual:** {balance_text}")
            return self._make_game_layout_view(
                "🎰 Girando...",
                details=details,
                board=board,
                footer_text=footer_text,
                color=discord.Color.blurple(),
            )
        def _make_roleta_result_view(
            self,
            title: str,
            summary: str,
            board: str,
            *,
            balance_text: str,
            success: bool,
            near: bool = False,
            footer_text: str | None = None,
            paid_entry: int = ROLETA_COST,
            gross_payout: int = 0,
            current_jackpot: int = ROLETA_JACKPOT_CHIPS,
            result_delta: int | None = None,
        ) -> discord.ui.LayoutView:
            details: list[str] = []
            effective_result = int(gross_payout) - int(paid_entry) if result_delta is None else int(result_delta)
            details.append(f"**Resultado:** {self._format_game_result_value(effective_result)}")
            if int(current_jackpot) > ROLETA_DYNAMIC_JACKPOT_BASE:
                details.append(f"**Jackpot:** {self._chip_text(current_jackpot, kind='gain')}")
            details.append(f"**Saldo atual:** {balance_text}")
            color = discord.Color.green() if success else (discord.Color.gold() if near else discord.Color(OFF_COLOR))
            return self._make_game_layout_view(
                title,
                details=details,
                board=board,
                summary=summary,
                footer_text=footer_text,
                color=color,
            )

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
                return "Seus giros acabaram, mas como você é staff você ainda pode girar"
            giro_text = "giro" if available == 1 else "giros"
            verb = "Resta" if available == 1 else "Restam"
            return f"{verb} {available} {giro_text} • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _roleta_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
            total = max(ROLETA_SPIN_LIMIT, int(state.get("total", ROLETA_SPIN_LIMIT) or ROLETA_SPIN_LIMIT))
            wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
            return "🎰 Sem giros por agora", f"Seus {total} giros acabaram\nReset em **{wait_text}**"

        async def _reserve_roleta_spin_state(self, guild_id: int, user_id: int, *, is_staff: bool) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            available = int(state.get("available", 0) or 0)
            if available <= 0:
                return bool(is_staff), state
            consumed = await self._consume_roleta_spin(guild_id, user_id)
            return True, consumed

        def _roll_roleta_target_middle(
            self,
            *,
            success: bool,
            excluded_special_triples: set[int] | None = None,
        ) -> list[object]:
            if success:
                return [7, 7, 7]
            roll = random.random()
            if roll < 0.05:
                base = random.randint(1, 9)
                middle = [base, self._random_roleta_joker(), base]
                random.shuffle(middle)
                return middle
            # Os antigos retornos de custo foram redistribuídos para combinações
            # parciais, preservando aproximadamente o retorno médio da roleta.
            if roll < 0.4626:
                # Mantém a mesma chance e o mesmo pagamento de resultado
                # parcial, mas permite a variação visual de três iguais. O 777
                # segue reservado ao jackpot; para o Apostador, 666 e 999 também
                # são excluídos aqui para não alterar as chances de seus efeitos.
                if random.random() < 0.12:
                    excluded = {7, *(excluded_special_triples or set())}
                    triple_choices = [digit for digit in range(1, 10) if digit not in excluded]
                    repeated = random.choice(triple_choices)
                    return [repeated, repeated, repeated]
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
                    return "beast", entry_cost
            jokers = [value for value in middle_digits if isinstance(value, str) and value in ROLETA_JOKERS]
            normals = [value for value in middle_digits if not (isinstance(value, str) and value in ROLETA_JOKERS)]
            if middle_digits == [7, 7, 7]:
                return "jackpot", ROLETA_JACKPOT_CHIPS
            if jokers and len(set(normals)) == 1 and len(normals) == 2:
                return "joker_premium", 50
            if max((middle_digits.count(v) for v in set(middle_digits)), default=0) >= 2:
                return "partial", max(3, entry_cost // 2)
            return "loss", 0
        def _ensure_game_animation_runtime(self):
            if not hasattr(self, "_game_animation_states"):
                self._game_animation_states: dict[int, dict[str, object]] = {}
            if not hasattr(self, "_roleta_trigger_cooldowns"):
                self._roleta_trigger_cooldowns: dict[tuple[int, int], float] = {}
            if not hasattr(self, "_game_message_edit_locks"):
                self._game_message_edit_locks: dict[int, asyncio.Lock] = {}
            if not hasattr(self, "_roleta_jackpot_locks"):
                self._roleta_jackpot_locks: dict[int, asyncio.Lock] = {}
            if not hasattr(self, "_roleta_cycle_bonus_locks"):
                self._roleta_cycle_bonus_locks: dict[tuple[int, int], asyncio.Lock] = {}
            if not hasattr(self, "_last_game_loss_titles"):
                self._last_game_loss_titles: dict[str, str] = {}
            if not hasattr(self, "_game_user_round_locks"):
                self._game_user_round_locks: dict[tuple[int, int], asyncio.Lock] = {}

        def _game_user_round_lock(self, guild_id: int, user_id: int) -> tuple[tuple[int, int], asyncio.Lock]:
            self._ensure_game_animation_runtime()
            key = (int(guild_id), int(user_id))
            lock = self._game_user_round_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._game_user_round_locks[key] = lock
            return key, lock

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

        async def _edit_game_message(self, message: discord.Message, *, view: discord.ui.LayoutView, final: bool = False) -> bool:
            attempts = 10 if final else 4
            delay = 0.75 if final else 0.30
            lock = self._game_message_edit_lock(message)
            async with lock:
                for attempt in range(attempts):
                    try:
                        await message.edit(view=view)
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

        async def _send_game_message(self, channel: discord.abc.Messageable, *, view: discord.ui.LayoutView, final: bool = False) -> discord.Message | None:
            attempts = 10 if final else 4
            delay = 0.75 if final else 0.30
            for attempt in range(attempts):
                try:
                    return await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
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

        async def _render_or_replace_game_message(
            self,
            source_message: discord.Message,
            target_message: discord.Message | None,
            *,
            view: discord.ui.LayoutView,
            final: bool = False,
        ) -> discord.Message | None:
            if target_message is not None and self._is_own_game_message(target_message):
                if await self._edit_game_message(target_message, view=view, final=final):
                    return target_message

            channel = getattr(target_message, "channel", None) or getattr(source_message, "channel", None)
            if channel is None:
                return None
            replacement = await self._send_game_message(channel, view=view, final=final)
            if replacement is None:
                return None
            if target_message is not None and target_message is not replacement:
                if self._is_own_game_message(target_message):
                    await self._delete_game_message(target_message)
                else:
                    self._drop_game_message_edit_lock(target_message)
            return replacement

        async def _deliver_game_result(
            self,
            source_message: discord.Message,
            target_message: discord.Message | None,
            *,
            view: discord.ui.LayoutView,
        ) -> discord.Message | None:
            target = target_message or source_message
            if target is None:
                return None
            delivered = await self._render_or_replace_game_message(source_message, target, view=view, final=True)
            if delivered is None:
                self._drop_game_message_edit_lock(target)
                logging.getLogger("gincana.roleta").warning(
                    "não foi possível entregar o resultado do jogo | guild=%s channel=%s message=%s",
                    getattr(getattr(source_message, "guild", None), "id", None),
                    getattr(getattr(source_message, "channel", None), "id", None),
                    getattr(target, "id", None),
                )
                return target
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
                await message.channel.send(
                    view=self._make_game_notice_view(
                        title,
                        "Já existem **2** animações ativas neste servidor\nTente novamente em instantes",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

        async def _confirm_game_negative_from_message(
            self,
            message: discord.Message,
            guild_id: int,
            user_id: int,
            amount: int,
            *,
            title: str,
        ) -> bool:
            note = self._negative_transition_note(guild_id, user_id, amount)
            if not note:
                return True
            view = _GameDebtConfirmView(self, owner_id=user_id, title=title, note=note)
            sent = None
            try:
                sent = await message.channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                view.message = sent
                await view.wait()
                return bool(view.confirmed)
            finally:
                if sent is not None:
                    try:
                        await sent.delete()
                    except Exception:
                        pass

        async def _animate_roleta_spin(
            self,
            message: discord.Message,
            *,
            target_middle: list[object],
            balance_text: str,
            footer_text: str | None = None,
            spin_message: discord.Message | None = None,
            owner_id: int | None = None,
            guild_id: int | None = None,
            session_id: str | None = None,
            paid_entry: int = ROLETA_COST,
            jackpot: int = ROLETA_JACKPOT_CHIPS,
        ) -> tuple[discord.Message | None, list[list[object]] | None]:
            rolling_columns = [self._build_roleta_column() for _ in range(3)]
            for idx in range(3):
                while rolling_columns[idx][1] == target_middle[idx]:
                    rolling_columns[idx] = self._build_roleta_column()

            opening_view = self._make_roleta_spin_view(
                self._render_roleta_board(rolling_columns),
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
                jackpot=jackpot,
            )
            spin_message = await self._render_or_replace_game_message(message, spin_message, view=opening_view, final=False)
            if spin_message is None:
                return None, None

            stop_plan = self._make_random_column_stop_plan(
                min_stop_seconds=ROLETA_ANIMATION_MIN_STOP_SECONDS,
                first_stop_max_seconds=2.45,
                last_stop_min_seconds=4.05,
                last_stop_max_seconds=ROLETA_ANIMATION_LAST_STOP_SECONDS,
                randomize_order=False,
            )
            intervals = self._roleta_animation_intervals()
            cumulative_times: list[float] = []
            accumulated = 0.0
            for delay in intervals:
                accumulated += float(delay)
                cumulative_times.append(accumulated)

            stop_frames: dict[int, int] = {}
            for stop_time, column_index in stop_plan:
                stop_frame = next(
                    (index for index, elapsed in enumerate(cumulative_times) if elapsed >= float(stop_time)),
                    len(intervals) - 1,
                )
                stop_frames[int(column_index)] = int(stop_frame)
            arm_frames = {column_index: max(0, stop_frame - 1) for column_index, stop_frame in stop_frames.items()}

            armed_columns: set[int] = set()
            locked_columns: set[int] = set()

            async def render_frame() -> None:
                nonlocal spin_message
                frame_view = self._make_roleta_spin_view(
                    self._render_roleta_board([list(column) for column in rolling_columns]),
                    balance_text=balance_text,
                    footer_text=footer_text,
                    paid_entry=paid_entry,
                    jackpot=jackpot,
                )
                rendered = await self._render_or_replace_game_message(message, spin_message, view=frame_view, final=False)
                if rendered is not None:
                    spin_message = rendered

            for frame_index, delay in enumerate(intervals):
                await asyncio.sleep(delay)
                has_turn = False
                try:
                    if guild_id is not None and session_id is not None:
                        has_turn = await self._wait_for_game_animation_turn(guild_id, session_id)
                        if not has_turn:
                            continue

                    for column_index, column in enumerate(rolling_columns):
                        if column_index in locked_columns:
                            continue

                        stop_frame = stop_frames.get(column_index, len(intervals) - 1)
                        arm_frame = arm_frames.get(column_index, max(0, stop_frame - 1))

                        if column_index in armed_columns and frame_index >= stop_frame:
                            # O valor preparado no topo desce para o meio e a
                            # coluna inteira congela sem trocar os números ao redor.
                            self._spin_roleta_column(column)
                            locked_columns.add(column_index)
                        elif column_index not in armed_columns and frame_index >= arm_frame:
                            # Este frame mostra o resultado entrando pelo topo.
                            # Somente no próximo frame ele chega à linha central.
                            self._spin_roleta_column(column, next_top=target_middle[column_index])
                            armed_columns.add(column_index)
                        else:
                            self._spin_roleta_column(column)

                    await render_frame()
                finally:
                    if has_turn and guild_id is not None and session_id is not None:
                        await self._advance_game_animation_turn(guild_id, session_id)

                if len(locked_columns) >= 3:
                    break

            # Em contenção de animações algum frame pode ser pulado. Finaliza
            # apenas as colunas pendentes em dois passos visíveis, preservando a
            # continuidade em vez de substituir a coluna por valores aleatórios.
            for column_index in range(3):
                if column_index in locked_columns:
                    continue
                if column_index not in armed_columns:
                    self._spin_roleta_column(rolling_columns[column_index], next_top=target_middle[column_index])
                    armed_columns.add(column_index)
                    await render_frame()
                self._spin_roleta_column(rolling_columns[column_index])
                locked_columns.add(column_index)
                await render_frame()

            final_columns = [list(column) for column in rolling_columns]
            return spin_message, final_columns
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
                return "Seus giros de cartas acabaram, mas como você é staff você ainda pode girar"
            giro_text = "giro de cartas" if available == 1 else "giros de cartas"
            verb = "Resta" if available == 1 else "Restam"
            return f"{verb} {available} {giro_text} • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _carta_spin_message_text(self, state: dict[str, float | int]) -> tuple[str, str]:
            total = max(CARTA_SPIN_LIMIT, int(state.get("total", CARTA_SPIN_LIMIT) or CARTA_SPIN_LIMIT))
            wait_text = self._format_roleta_reset_time(float(state.get("reset_in", 0.0) or 0.0))
            return "🎴 Sem giros por agora", f"Seus {total} giros de cartas acabaram\nReset em **{wait_text}**"

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
                    "Essa mão não rendeu nada",
                    "As cartas não encaixaram",
                    "Dessa vez a mão passou em branco",
                ],
                "partial": [
                    "Essa mão rendeu bem",
                    "As cartas encaixaram",
                    "Foi uma boa combinação",
                ],
                "premium": [
                    "O coringa completou a combinação",
                    "O coringa fechou a mão",
                    "O coringa puxou a melhor carta da rodada",
                ],
                "rare": [
                    "Essa mão veio forte",
                    "As cartas bateram bonito",
                    "Foi uma combinação rara",
                ],
                "jackpot": [
                    "A mão bateu o prêmio máximo",
                    "Você acertou a mão máxima",
                    "As cartas vieram perfeitas",
                ],
            }
            picks = options.get(result_kind)
            if picks:
                return random.choice(picks)
            return fallback or "Resultado das cartas"

        def _pick_carta_hot_streak_text(self) -> str:
            return random.choice([
                "Você entrou em boa fase",
                "Sua mão esquentou",
                "A sequência ficou forte",
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

        def _spin_carta_column(self, column: list[object]):
            column.insert(0, self._random_carta_symbol())
            del column[3:]

        def _make_carta_spin_view(
            self,
            board: str,
            *,
            balance_text: str,
            footer_text: str | None = None,
            paid_entry: int = CARTA_COST,
            jackpot: int = CARTA_JACKPOT_CHIPS,
        ) -> discord.ui.LayoutView:
            return self._make_game_layout_view(
                "🎴 Cartas embaralhando...",
                details=[
                    f"**Entrada:** {self._format_game_entry_value(paid_entry)}",
                    f"**Prêmio máximo:** {self._chip_text(jackpot, kind='gain')}",
                    f"**Saldo atual:** {balance_text}",
                ],
                board=board,
                footer_text=footer_text,
                color=discord.Color.from_rgb(111, 88, 242),
            )

        def _make_carta_result_view(
            self,
            title: str,
            summary: str,
            board: str,
            *,
            balance_text: str,
            success: bool,
            premium: bool = False,
            footer_text: str | None = None,
            paid_entry: int = CARTA_COST,
            gross_payout: int = 0,
            result_delta: int | None = None,
        ) -> discord.ui.LayoutView:
            details: list[str] = []
            effective_result = int(gross_payout) - int(paid_entry) if result_delta is None else int(result_delta)
            details.extend([
                f"**Resultado:** {self._format_game_result_value(effective_result)}",
                f"**Saldo atual:** {balance_text}",
            ])
            color = discord.Color.from_rgb(255, 201, 74) if premium else (discord.Color.green() if success else discord.Color(OFF_COLOR))
            return self._make_game_layout_view(
                title,
                details=details,
                board=board,
                summary=summary,
                footer_text=footer_text,
                color=color,
            )

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
                return "jackpot", CARTA_JACKPOT_CHIPS, "A mão bateu o prêmio máximo"
            if counts.get("🃏", 0) == 3:
                return "rare", 80, "Trinca de coringas na linha do meio"
            if any(count == 3 for symbol, count in counts.items() if symbol != "🃏"):
                triple_symbol = next(symbol for symbol, count in counts.items() if symbol != "🃏" and count == 3)
                values = {"👑": 50, "💎": 35, "🍀": 25, "⭐": 65}
                texts = {"👑": "Trinca de coroas", "💎": "Trinca de diamantes", "🍀": "Trinca de trevos", "⭐": "Trinca rara de estrelas"}
                return "rare", values.get(triple_symbol, 25), texts.get(triple_symbol, "Trinca premiada")
            pair_symbol = next((symbol for symbol, count in counts.items() if symbol != "🃏" and count == 2), None)
            if pair_symbol and joker_count == 1:
                values = {"⭐": 70, "👑": 40, "💎": 30, "🍀": 22}
                texts = {"⭐": "O coringa completou a mão máxima", "👑": "O coringa completou a combinação", "💎": "O coringa fechou a combinação", "🍀": "O coringa ajudou a fechar a mão"}
                return "premium", values.get(pair_symbol, 20), texts.get(pair_symbol, "O coringa completou a combinação")
            if joker_count == 2 and len(counts) == 2:
                other = next(symbol for symbol in counts if symbol != "🃏")
                values = {"⭐": 55, "👑": 32, "💎": 24, "🍀": 18}
                return "premium", values.get(other, 18), "Dois coringas puxaram a combinação"
            if pair_symbol:
                values = {"⭐": 20, "👑": 15, "💎": 12, "🍀": 10}
                texts = {"⭐": "Par raro na linha do meio", "👑": "Par de coroas", "💎": "Par de diamantes", "🍀": "Par de trevos"}
                return "partial", values.get(pair_symbol, 10), texts.get(pair_symbol, "Par premiado")
            if joker_count == 1 and len(counts) == 3:
                return "partial", 10, "O coringa formou uma combinação simples"
            if star_count == 2:
                return "partial", 18, "Quase bateu a mão mais rara"
            return "loss", 0, "Essa mão não rendeu nada"

        async def _animate_carta_spin(
            self,
            message: discord.Message,
            *,
            target_middle: list[object],
            balance_text: str,
            footer_text: str | None = None,
            spin_message: discord.Message | None = None,
            owner_id: int | None = None,
            guild_id: int | None = None,
            session_id: str | None = None,
            paid_entry: int = CARTA_COST,
            jackpot: int = CARTA_JACKPOT_CHIPS,
        ) -> tuple[discord.Message | None, list[list[object]] | None]:
            rolling_columns = [self._build_carta_column() for _ in range(3)]
            for idx in range(3):
                while rolling_columns[idx][1] == target_middle[idx]:
                    rolling_columns[idx] = self._build_carta_column()

            final_columns = [self._build_carta_column(target_middle[idx]) for idx in range(3)]
            opening_view = self._make_carta_spin_view(
                self._render_carta_board(rolling_columns),
                balance_text=balance_text,
                footer_text=footer_text,
                paid_entry=paid_entry,
                jackpot=jackpot,
            )
            spin_message = await self._render_or_replace_game_message(message, spin_message, view=opening_view, final=False)
            if spin_message is None:
                return None, None

            stop_plan = self._make_random_column_stop_plan()
            stop_cursor = 0
            locked_columns: set[int] = set()
            started_at = time.monotonic()
            deadline_at = started_at + CARTA_ANIMATION_MAX_SECONDS
            next_frame_at = started_at + CARTA_ANIMATION_FRAME_SECONDS
            display_columns = [list(column) for column in rolling_columns]

            while True:
                now = time.monotonic()
                wake_at = min(next_frame_at, deadline_at)
                if wake_at > now:
                    await asyncio.sleep(wake_at - now)

                has_turn = False
                try:
                    if guild_id is not None and session_id is not None:
                        has_turn = await self._wait_for_game_animation_turn(guild_id, session_id)
                        if not has_turn:
                            if time.monotonic() >= deadline_at:
                                break
                            continue

                    now = time.monotonic()
                    elapsed = now - started_at
                    for column_index, column in enumerate(rolling_columns):
                        if column_index not in locked_columns:
                            self._spin_carta_column(column)

                    if elapsed >= CARTA_ANIMATION_MAX_SECONDS:
                        locked_columns = {0, 1, 2}
                        stop_cursor = len(stop_plan)
                    else:
                        while stop_cursor < len(stop_plan) and stop_plan[stop_cursor][0] <= elapsed:
                            _, column_index = stop_plan[stop_cursor]
                            locked_columns.add(column_index)
                            stop_cursor += 1

                    display_columns = self._compose_game_animation_columns(
                        rolling_columns,
                        final_columns,
                        locked_columns,
                    )
                    frame_view = self._make_carta_spin_view(
                        self._render_carta_board(display_columns),
                        balance_text=balance_text,
                        footer_text=footer_text,
                        paid_entry=paid_entry,
                        jackpot=jackpot,
                    )
                    rendered = await self._render_or_replace_game_message(message, spin_message, view=frame_view, final=False)
                    if rendered is not None:
                        spin_message = rendered
                finally:
                    if has_turn and guild_id is not None and session_id is not None:
                        await self._advance_game_animation_turn(guild_id, session_id)

                if len(locked_columns) >= 3 or time.monotonic() >= deadline_at:
                    break
                next_frame_at += CARTA_ANIMATION_FRAME_SECONDS
                if next_frame_at <= time.monotonic():
                    next_frame_at = time.monotonic() + CARTA_ANIMATION_FRAME_SECONDS

            return spin_message, final_columns
        async def _execute_roleta_round(
            self,
            *,
            source_message: discord.Message,
            guild: discord.Guild,
            actor: discord.abc.User,
            roleta_footer: str,
            chip_note: str | None,
            voice_channel: discord.abc.Connectable | None,
            targets: list[discord.Member],
            session_id: str,
            spin_message: discord.Message | None = None,
            entry_cost: int = ROLETA_COST,
            entry_spend: dict | None = None,
        ) -> bool:
            outcome = self._roleta_outcome_for_user(guild.id, actor.id)
            forced_kind = str(outcome.get("forced_kind") or "")
            target_middle = list(outcome.get("target_middle") or [7, 7, 7])
            paid_entry = self._entry_paid_amount(entry_spend, entry_cost)
            round_start_total = self._current_game_chip_total(guild.id, actor.id) + paid_entry
            is_apostador = self._race_is(guild.id, actor.id, "apostador")

            reserved_jackpot: int | None = None
            if forced_kind == "jackpot" and not is_apostador:
                reserved_jackpot = await self._claim_roleta_dynamic_jackpot(guild.id)
            jackpot_preview = (
                int(reserved_jackpot)
                if reserved_jackpot is not None
                else self._roleta_jackpot_preview(guild.id, actor.id)
            )

            await self.db.add_user_game_stat(guild.id, actor.id, "roleta_spins", 1)
            spin_balance_text = self._format_compact_chip_balance(guild.id, actor.id)
            try:
                spin_message, final_columns = await self._animate_roleta_spin(
                    source_message,
                    target_middle=target_middle,
                    balance_text=spin_balance_text,
                    footer_text=roleta_footer,
                    spin_message=spin_message,
                    owner_id=actor.id,
                    guild_id=guild.id,
                    session_id=session_id,
                    paid_entry=paid_entry,
                    jackpot=jackpot_preview,
                )
            except Exception:
                logging.getLogger("gincana.roleta").exception(
                    "falha visual na animação da roleta | guild=%s user=%s", guild.id, actor.id
                )
                final_columns = None
            if final_columns is None:
                final_columns = [
                    self._build_roleta_column(target_middle[0]),
                    self._build_roleta_column(target_middle[1]),
                    self._build_roleta_column(target_middle[2]),
                ]

            board = self._render_roleta_board(final_columns)
            middle_digits = [column[1] for column in final_columns]
            result_kind, evaluated_amount = self._evaluate_roleta_middle(middle_digits, guild_id=guild.id, user_id=actor.id)
            if reserved_jackpot is not None and result_kind == "jackpot":
                result_amount = int(reserved_jackpot)
            elif outcome.get("forced_amount") is not None and forced_kind == result_kind:
                result_amount = int(outcome["forced_amount"])
            else:
                result_amount = int(evaluated_amount)

            race_won: bool | None = None
            race_payout = 0
            gross_payout = 0
            summary_lines: list[str] = []
            success = False
            near = False
            current_jackpot = self._current_roleta_dynamic_jackpot(guild.id)

            try:
                if result_kind in {"jackpot", "jackpot_mega"}:
                    race_won = True
                    race_payout = gross_payout = int(result_amount)
                    success = True
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
                    effect_note = ""
                    if is_apostador:
                        effect_note = self._race_effect_message(guild.id, actor.id, "all_in" if result_kind == "jackpot_mega" else "jackpot")
                    if effect_note:
                        summary_lines.append(effect_note)
                    title = "🎰 Jackpot 777!" if result_kind == "jackpot_mega" else ("🎰 Jackpot 999!" if is_apostador else "🎰 Jackpot!")
                elif result_kind == "joker_premium":
                    race_won = True
                    race_payout = gross_payout = int(result_amount)
                    near = True
                    await self._record_game_played(guild.id, actor.id, weekly_points=6)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    await self._grant_weekly_points(guild.id, actor.id, 8)
                    summary_lines.append("O coringa completou a combinação")
                    title = "🎰 Coringa premiado"
                elif result_kind == "partial":
                    race_won = True
                    race_payout = gross_payout = int(result_amount)
                    near = True
                    await self._record_game_played(guild.id, actor.id, weekly_points=4)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio da roleta")
                    await self._grant_weekly_points(guild.id, actor.id, 6)
                    title, partial_description = self._roleta_partial_result_copy(middle_digits)
                    summary_lines.append(partial_description)
                elif result_kind == "beast":
                    race_won = None
                    race_payout = gross_payout = int(result_amount)
                    near = True
                    await self._record_game_played(guild.id, actor.id, weekly_points=3)
                    await self._change_user_chips(guild.id, actor.id, result_amount, reason="Marca da Besta")
                    effect_note = self._race_effect_message(guild.id, actor.id, "666")
                    if effect_note:
                        summary_lines.append(effect_note)
                    title = "🎰 Marca da Besta"
                else:
                    race_won = False
                    race_payout = 0
                    await self._record_game_played(guild.id, actor.id, weekly_points=2)
                    current_jackpot = await self._increase_roleta_dynamic_jackpot(guild.id)
                    refund = await self._maybe_apply_coringa_cashback(guild.id, actor.id, entry_cost, chance=0.5)
                    gross_payout = int(refund)
                    if refund > 0:
                        effect_note = self._race_effect_message(
                            guild.id,
                            actor.id,
                            "redencao",
                            f"você recuperou {self._chip_text(refund, kind='gain')} do custo do giro",
                        )
                        summary_lines.append(effect_note or f"Você recuperou {self._chip_text(refund, kind='gain')}")
                    title = self._pick_game_loss_title("roleta")

                race_notes = await self._apply_new_race_result(
                    guild.id,
                    actor.id,
                    won=race_won,
                    entry_spend=entry_spend,
                    payout=race_payout,
                    opponent_ids=(),
                    # Resultados parciais pagam normalmente, mas não acionam efeitos de raça.
                    valid=result_kind != "partial",
                    allow_hunt=False,
                    glitch_progress=result_kind in {"jackpot", "jackpot_mega", "loss"},
                )
                if race_notes:
                    summary_lines = [*race_notes, *summary_lines]
                cycle_note = await self._maybe_grant_roleta_cycle_bonus(guild.id, actor.id)
                if cycle_note:
                    summary_lines.insert(0, cycle_note)
                if chip_note:
                    summary_lines.insert(0, chip_note)

                result_view = self._make_roleta_result_view(
                    title,
                    "\n".join(line for line in summary_lines if line),
                    board,
                    balance_text=self._format_compact_chip_balance(guild.id, actor.id),
                    success=success,
                    near=near,
                    footer_text=roleta_footer,
                    paid_entry=paid_entry,
                    gross_payout=gross_payout,
                    current_jackpot=current_jackpot,
                    result_delta=self._current_game_chip_total(guild.id, actor.id) - round_start_total,
                )
            except Exception:
                logging.getLogger("gincana.roleta").exception(
                    "falha ao consolidar resultado da roleta | guild=%s user=%s",
                    guild.id,
                    actor.id,
                )
                fallback_title = "🎰 Jackpot!" if forced_kind in {"jackpot", "jackpot_mega"} else self._pick_game_loss_title("roleta")
                fallback_lines = [chip_note] if chip_note else []
                fallback_lines.append("O resultado foi consolidado, mas parte dos detalhes não pôde ser exibida")
                result_view = self._make_roleta_result_view(
                    fallback_title,
                    "\n".join(fallback_lines),
                    board,
                    balance_text=self._format_compact_chip_balance(guild.id, actor.id),
                    success=forced_kind in {"jackpot", "jackpot_mega"},
                    footer_text=roleta_footer,
                    paid_entry=paid_entry,
                    gross_payout=gross_payout,
                    current_jackpot=self._current_roleta_dynamic_jackpot(guild.id),
                    result_delta=self._current_game_chip_total(guild.id, actor.id) - round_start_total,
                )
            first_game_unlocked = await self._unlock_achievement(guild.id, actor.id, "first_game")
            roulette_achievements = await self._record_roulette_achievement_result(
                guild.id,
                actor.id,
                jackpot=result_kind in {"jackpot", "jackpot_mega"},
                lost=result_kind == "loss",
            )
            await self._deliver_game_result(source_message, spin_message, view=result_view)
            if first_game_unlocked:
                await self._send_achievement_notice(source_message.channel, guild.id, actor.id, "first_game")
            for achievement_key in roulette_achievements:
                await self._send_achievement_notice(source_message.channel, guild.id, actor.id, achievement_key)
            return True

        async def _execute_carta_round(
            self,
            *,
            source_message: discord.Message,
            guild: discord.Guild,
            actor: discord.abc.User,
            carta_footer: str,
            chip_note: str | None,
            session_id: str,
            spin_message: discord.Message | None = None,
            entry_cost: int = CARTA_COST,
            entry_spend: dict | None = None,
        ) -> bool:
            target_middle = self._roll_carta_target_middle()
            paid_entry = self._entry_paid_amount(entry_spend, entry_cost)
            round_start_total = self._current_game_chip_total(guild.id, actor.id) + paid_entry
            spin_balance_text = self._format_compact_chip_balance(guild.id, actor.id)
            try:
                spin_message, final_columns = await self._animate_carta_spin(
                    source_message,
                    target_middle=target_middle,
                    balance_text=spin_balance_text,
                    footer_text=carta_footer,
                    spin_message=spin_message,
                    owner_id=actor.id,
                    guild_id=guild.id,
                    session_id=session_id,
                    paid_entry=paid_entry,
                    jackpot=CARTA_JACKPOT_CHIPS,
                )
            except Exception:
                logging.getLogger("gincana.roleta").exception(
                    "falha visual na animação das cartas | guild=%s user=%s", guild.id, actor.id
                )
                final_columns = None
            if final_columns is None:
                final_columns = [
                    self._build_carta_column(target_middle[0]),
                    self._build_carta_column(target_middle[1]),
                    self._build_carta_column(target_middle[2]),
                ]
            board = self._render_carta_board(final_columns)
            middle = [column[1] for column in final_columns]
            result_kind, result_amount, flavor = self._evaluate_carta_middle(middle)
            race_won: bool | None = None
            race_payout = 0
            gross_payout = 0
            summary_lines: list[str] = []
            success = False
            premium = False

            await self.db.add_user_game_stat(guild.id, actor.id, "carta_spins", 1)
            flavor = self._pick_carta_result_flavor(result_kind, fallback=flavor)
            _streak_value, streak_line = await self._advance_carta_hot_streak(guild.id, actor.id, result_kind=result_kind)

            if result_kind == "jackpot":
                race_won = True
                race_payout = gross_payout = CARTA_JACKPOT_CHIPS
                success = premium = True
                await self._record_game_played(guild.id, actor.id, weekly_points=12)
                await self._change_user_chips(guild.id, actor.id, CARTA_JACKPOT_CHIPS, reason="Prêmio das cartas")
                await self.db.add_user_game_stat(guild.id, actor.id, "cartas_jackpots", 1)
                await self._grant_weekly_points(guild.id, actor.id, 18)
                summary_lines.append(flavor)
                title = "🎴 Jackpot!"
            elif result_kind in {"rare", "premium", "partial"}:
                race_won = True
                race_payout = gross_payout = int(result_amount)
                success = True
                premium = result_kind in {"rare", "premium"}
                weekly_map = {"rare": 8, "premium": 7, "partial": 4}
                await self._record_game_played(guild.id, actor.id, weekly_points=weekly_map.get(result_kind, 3))
                await self._change_user_chips(guild.id, actor.id, result_amount, reason="Prêmio das cartas")
                if result_kind in {"rare", "premium"}:
                    await self._grant_weekly_points(guild.id, actor.id, 6)
                summary_lines.append(flavor)
                titles = {
                    "rare": "🎴 Mão rara",
                    "premium": "🎴 Coringa premiado",
                    "partial": "🎴 Mão premiada",
                }
                title = titles.get(result_kind, "🎴 Boa mão")
            else:
                race_won = False
                race_payout = 0
                await self._record_game_played(guild.id, actor.id, weekly_points=2)
                refund = await self._maybe_apply_coringa_cashback(guild.id, actor.id, entry_cost, chance=0.5)
                gross_payout = int(refund)
                if refund > 0:
                    effect_note = self._race_effect_message(
                        guild.id,
                        actor.id,
                        "redencao",
                        f"você recuperou {self._chip_text(refund, kind='gain')} do custo da mão",
                    )
                    summary_lines.append(effect_note or f"Você recuperou {self._chip_text(refund, kind='gain')}")
                else:
                    summary_lines.append(flavor)
                title = self._pick_game_loss_title("cartas")

            if streak_line:
                summary_lines.append(f"*{streak_line}*")
            race_notes = await self._apply_new_race_result(
                guild.id,
                actor.id,
                won=race_won,
                entry_spend=entry_spend,
                payout=race_payout,
                opponent_ids=(),
                # Resultados parciais pagam normalmente, mas não acionam efeitos de raça.
                valid=result_kind != "partial",
                allow_hunt=False,
                glitch_progress=True,
            )
            if race_notes:
                summary_lines = [*race_notes, *summary_lines]
            if chip_note:
                summary_lines.insert(0, chip_note)

            result_view = self._make_carta_result_view(
                title,
                "\n".join(line for line in summary_lines if line),
                board,
                balance_text=self._format_compact_chip_balance(guild.id, actor.id),
                success=success,
                premium=premium,
                footer_text=carta_footer,
                paid_entry=paid_entry,
                gross_payout=gross_payout,
                result_delta=self._current_game_chip_total(guild.id, actor.id) - round_start_total,
            )
            first_game_unlocked = await self._unlock_achievement(guild.id, actor.id, "first_game")
            roulette_achievements = await self._record_roulette_achievement_result(
                guild.id,
                actor.id,
                jackpot=result_kind == "jackpot",
                lost=result_kind == "loss",
            )
            await self._deliver_game_result(source_message, spin_message, view=result_view)
            if first_game_unlocked:
                await self._send_achievement_notice(source_message.channel, guild.id, actor.id, "first_game")
            for achievement_key in roulette_achievements:
                await self._send_achievement_notice(source_message.channel, guild.id, actor.id, achievement_key)
            return True
        async def _run_carta_trigger_locked(self, message: discord.Message) -> bool:
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
                title, desc = self._carta_spin_message_text(carta_state)
                try:
                    await message.channel.send(view=self._make_game_notice_view(title, desc, ok=False), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass
                return True

            entry_cost = CARTA_COST
            blessing_state = await self._sync_sortudo_blessings(guild.id, message.author.id)
            free_spin = self._race_is(guild.id, message.author.id, "sortudo") and int(blessing_state.get("charges", 0) or 0) > 0
            needs_negative_confirm = False if free_spin else self._needs_negative_confirmation(guild.id, message.author.id, entry_cost)
            if needs_negative_confirm:
                confirmed = await self._confirm_game_negative_from_message(
                    message,
                    guild.id,
                    message.author.id,
                    entry_cost,
                    title="🎴 Confirmar aposta",
                )
                if not confirmed:
                    return True

            session_id = self._next_game_animation_session_id(guild_id=guild.id, kind="cartas", owner_id=message.author.id)
            if not await self._try_acquire_game_animation_slot(guild.id, session_id):
                await self._send_animation_limit_message(message, title="🎴 Aguarde um pouco")
                return True

            try:
                can_spin, carta_state = await self._reserve_carta_spin_state(guild.id, message.author.id, is_staff=is_staff)
                if not can_spin:
                    title, desc = self._carta_spin_message_text(carta_state)
                    try:
                        await message.channel.send(view=self._make_game_notice_view(title, desc, ok=False), allowed_mentions=discord.AllowedMentions.none())
                    except Exception:
                        pass
                    return True
                carta_footer = self._carta_footer_text(state=carta_state, is_staff=is_staff)
                if free_spin:
                    await self._consume_sortudo_blessing(guild.id, message.author.id)
                    chip_note = self._sortudo_blessing_note(guild.id, message.author.id, kind="carta")
                    entry_spend = {"chips": 0, "bonus": 0}
                    paid = True
                else:
                    entry_spend = self._entry_spend_parts(guild.id, message.author.id, entry_cost)
                    paid, _balance, chip_note = await self._try_consume_chips(
                        guild.id,
                        message.author.id,
                        entry_cost,
                        reason="Entrada nas cartas",
                    )
                if needs_negative_confirm:
                    chip_note = None
                if not paid:
                    try:
                        await message.channel.send(
                            view=self._make_game_notice_view("🎴 Saldo insuficiente", chip_note or "Você não tem saldo suficiente", ok=False),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception:
                        pass
                    return True
                await self._unlock_and_send_achievement(
                    message.channel,
                    guild.id,
                    message.author.id,
                    "lets_go_gambling",
                )
                await self._execute_carta_round(
                    source_message=message,
                    guild=guild,
                    actor=message.author,
                    carta_footer=carta_footer,
                    chip_note=chip_note,
                    session_id=session_id,
                    entry_cost=entry_cost,
                    entry_spend=entry_spend,
                )
                return True
            finally:
                await self._release_game_animation_slot(guild.id, session_id)
        async def _run_roleta_trigger_locked(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False
            if not self._matches_exact_trigger(message.content or "", "roleta"):
                return False
            if not self.db.gincana_enabled(guild.id):
                return True
            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            cooldown_remaining = self._roleta_trigger_cooldown_remaining(guild.id, message.author.id)
            if cooldown_remaining > 0:
                try:
                    await message.channel.send(
                        view=self._make_game_notice_view(
                            "🎰 Aguarde um pouco",
                            f"Espere **{int(cooldown_remaining) + 1}s** para usar a roleta novamente",
                            ok=False,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass
                return True

            is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
            roleta_state = await self._sync_roleta_spin_window(guild.id, message.author.id)
            if int(roleta_state.get("available", 0) or 0) <= 0 and not is_staff:
                title, desc = self._roleta_spin_message_text(roleta_state)
                try:
                    await message.channel.send(view=self._make_game_notice_view(title, desc, ok=False), allowed_mentions=discord.AllowedMentions.none())
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
                confirmed = await self._confirm_game_negative_from_message(
                    message,
                    guild.id,
                    message.author.id,
                    entry_cost,
                    title="🎰 Confirmar aposta",
                )
                if not confirmed:
                    return True

            session_id = self._next_game_animation_session_id(guild_id=guild.id, kind="roleta", owner_id=message.author.id)
            if not await self._try_acquire_game_animation_slot(guild.id, session_id):
                await self._send_animation_limit_message(message, title="🎰 Aguarde um pouco")
                return True

            try:
                can_spin, roleta_state = await self._reserve_roleta_spin_state(guild.id, message.author.id, is_staff=is_staff)
                if not can_spin:
                    title, desc = self._roleta_spin_message_text(roleta_state)
                    try:
                        await message.channel.send(view=self._make_game_notice_view(title, desc, ok=False), allowed_mentions=discord.AllowedMentions.none())
                    except Exception:
                        pass
                    return True
                roleta_footer = self._roleta_footer_text(state=roleta_state, is_staff=is_staff)
                if free_spin:
                    await self._consume_sortudo_blessing(guild.id, message.author.id)
                    chip_note = self._sortudo_blessing_note(guild.id, message.author.id, kind="roleta")
                    entry_spend = {"chips": 0, "bonus": 0}
                    paid = True
                else:
                    entry_spend = self._entry_spend_parts(guild.id, message.author.id, entry_cost)
                    paid, _balance, chip_note = await self._try_consume_chips(
                        guild.id,
                        message.author.id,
                        entry_cost,
                        reason="Entrada na roleta",
                    )
                if needs_negative_confirm:
                    chip_note = None
                if not paid:
                    try:
                        await message.channel.send(
                            view=self._make_game_notice_view("🎰 Saldo insuficiente", chip_note or "Você não tem saldo suficiente", ok=False),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception:
                        pass
                    return True
                self._mark_roleta_trigger_used(guild.id, message.author.id)
                await self._unlock_and_send_achievement(
                    message.channel,
                    guild.id,
                    message.author.id,
                    "lets_go_gambling",
                )
                await self._execute_roleta_round(
                    source_message=message,
                    guild=guild,
                    actor=message.author,
                    roleta_footer=roleta_footer,
                    chip_note=chip_note,
                    voice_channel=voice_channel,
                    targets=targets,
                    session_id=session_id,
                    entry_cost=entry_cost,
                    entry_spend=entry_spend,
                )
                return True
            finally:
                await self._release_game_animation_slot(guild.id, session_id)
        async def _handle_carta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False
            if (message.content or "").strip().casefold() not in {"carta", "cartas"}:
                return False
            key, lock = self._game_user_round_lock(guild.id, message.author.id)
            if lock.locked():
                # A rodada anterior ainda está finalizando (inclusive suas conquistas).
                # Remover o novo trigger impede que ele apareça entre o resultado e
                # as notificações e evita iniciar uma segunda rodada fora de ordem.
                await self._delete_game_message(message)
                return True
            try:
                async with lock:
                    return await self._run_carta_trigger_locked(message)
            finally:
                if self._game_user_round_locks.get(key) is lock and not lock.locked():
                    self._game_user_round_locks.pop(key, None)

        async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False
            if not self._matches_exact_trigger(message.content or "", "roleta"):
                return False
            key, lock = self._game_user_round_lock(guild.id, message.author.id)
            if lock.locked():
                # A rodada anterior ainda está finalizando (inclusive suas conquistas).
                # Remover o novo trigger impede que ele apareça entre o resultado e
                # as notificações e evita iniciar uma segunda rodada fora de ordem.
                await self._delete_game_message(message)
                return True
            try:
                async with lock:
                    return await self._run_roleta_trigger_locked(message)
            finally:
                if self._game_user_round_locks.get(key) is lock and not lock.locked():
                    self._game_user_round_locks.pop(key, None)
