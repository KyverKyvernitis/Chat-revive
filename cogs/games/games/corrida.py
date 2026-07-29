import asyncio
import datetime
import random

import discord


CORRIDA_STAKE = 40
CORRIDA_RODADA_CHEIA_THRESHOLD = 5
CORRIDA_RODADA_CHEIA_BONUS = 40
CORRIDA_RODADA_CHEIA_BONUS_LARGE = 50
CORRIDA_RODADA_CHEIA_LARGE_THRESHOLD = 6
_CORRIDA_TRACK_LENGTH = 18
_CORRIDA_UPDATES = 10
_CORRIDA_UPDATE_SECONDS = 2.0
_CORRIDA_DURATION_SECONDS = int(_CORRIDA_UPDATES * _CORRIDA_UPDATE_SECONDS)
_CORRIDA_LOBBY_SECONDS = 20.0

_HORSE_START = "<:horse1:1485794648239636647>"
_HORSE_BOOST = "<:horse2:1485795177401417799>"
_HORSE_RUN = "<:horse2:1485795705745444995>"
_HORSE_TRIP = "<:horse2:1485795938990821547>"
_HORSE_FINISH = "<:Mine:1485797167494070524>"
_HORSE_DASH = "<:aaa:1486376725838430248>"

_RACE_CONDITIONS = [
    {"name": "Pista seca", "boost": 0.0, "trip": 0.0, "speed": 0.0},
    {"name": "Pista molhada", "boost": -0.02, "trip": 0.08, "speed": -0.15},
    {"name": "Pista pesada", "boost": -0.03, "trip": 0.04, "speed": -0.25},
    {"name": "Pista rápida", "boost": 0.08, "trip": -0.02, "speed": 0.2},
]
_RACE_CONDITION_WEIGHTS = (0.34, 0.28, 0.26, 0.12)

_RACE_SPECIALS = [
    {"name": "Corrida turbo", "boost": 0.12, "trip": -0.03, "speed": 0.35, "bonus_pool": 0, "color": discord.Color.dark_magenta()},
    {"name": "Corrida pesada", "boost": -0.05, "trip": 0.1, "speed": -0.25, "bonus_pool": 0, "color": discord.Color.dark_orange()},
    {"name": "Corrida de zebra", "boost": 0.04, "trip": 0.0, "speed": 0.0, "bonus_pool": 0, "zebra": True, "color": discord.Color.purple()},
    {"name": "Grande prêmio", "boost": 0.02, "trip": 0.0, "speed": 0.15, "bonus_pool": 10, "color": discord.Color.gold()},
]

_RACE_IMPULSE_WINDOWS_NORMAL = ((3, "Largada"), (7, "Sprint final"))
_RACE_IMPULSE_WINDOWS_FAST = ((2, "Largada"), (5, "Meio"), (8, "Sprint final"))
_RACE_IMPULSE_INITIAL_DELAY = 0.0
_RACE_IMPULSE_STEP_SECONDS = 1.0
_RACE_IMPULSE_BUTTON_COUNT = 3
_RACE_IMPULSE_STAGE_COUNT = 3
_RACE_IMPULSE_EMOJI = "⚡"
_RACE_IMPULSE_DELETE_DELAY_SECONDS = 1.0


def _shared_rank_map(arrival_groups: list[list[int]]) -> dict[int, int]:
    rank_map: dict[int, int] = {}
    next_rank = 1
    for group in arrival_groups:
        for user_id in group:
            rank_map[int(user_id)] = next_rank
        next_rank += len(group)
    return rank_map


class _RaceLobbyView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild_id: int, session: dict, guild: discord.Guild, *, timeout: float = _CORRIDA_LOBBY_SECONDS):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.session = session
        self.guild = guild
        self.view_token = str(self.session.setdefault("_view_token", f"race:{guild_id}:{random.getrandbits(32):08x}"))
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label=f"🐎 Entrar ({len(cog._get_race_participants(guild, session))})", custom_id=f"{self.view_token}:join")
        self.join_button.callback = self._join_race
        self.start_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Iniciar", emoji="🏁", custom_id=f"{self.view_token}:start")
        self.start_button.callback = self._start_race
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        condition_name = str((self.session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((self.session.get("special") or {}).get("name") or "")
        participants = self.cog._get_race_participants(self.guild, self.session)
        pot_total = self.cog._race_pot_total(self.session)
        bonus_pool = int(self.session.get("bonus_pool", 0) or 0)
        pending_bonus = self.cog._race_rodada_cheia_pending_bonus(self.session)
        effective_bonus = bonus_pool + pending_bonus
        rodada_cheia_active = pending_bonus > 0

        header_lines = [
            "# 🐎 Corrida de cavalos",
            f"**Condição:** {condition_name}",
        ]
        if special_name:
            header_lines.append(f"**Especial:** {special_name}")
        if rodada_cheia_active:
            header_lines.append(f"🎉 **Rodada cheia ativada** (+{pending_bonus} fichas bônus)")
        header_lines.append(f"**Entrada:** {self.cog._chip_amount(CORRIDA_STAKE)}")
        header_lines.append(f"**Pote atual:** {self.cog._chip_amount(pot_total)}" + (f" • Bônus: {self.cog._bonus_chip_amount(effective_bonus)}" if effective_bonus > 0 else ""))
        header_lines.append(f"**Tempo de corrida:** {_CORRIDA_DURATION_SECONDS}s")

        participants_lines = [f"### Participantes ({len(participants)})"]
        if participants:
            participants_lines.extend(f"• {member.mention}" for member in participants)
        else:
            participants_lines.append("• Ninguém entrou ainda.")

        info_lines = ["Use o botão abaixo para entrar."]
        info_lines.append("O criador da corrida ou a staff pode iniciar com 🏁 quando houver pelo menos 2 participantes.")

        row = discord.ui.ActionRow(self.join_button, self.start_button)
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(participants_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(info_lines)),
            row,
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)

    async def _join_race(self, interaction: discord.Interaction):
        await self.cog._handle_race_button(interaction, self)

    async def _start_race(self, interaction: discord.Interaction):
        await self.cog._handle_race_start_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_race_lobby(self.guild_id, reason="timeout", source_view=self)
        except Exception:
            pass


class _RaceLobbyClosedView(discord.ui.LayoutView):
    def __init__(self, session: dict, guild: discord.Guild, title: str, detail: str):
        super().__init__(timeout=None)
        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        participants = int(len(session.get("locked_participants", set()) or []))

        lines = [f"# {title}", f"**Condição:** {condition_name}"]
        if special_name:
            lines.append(f"**Especial:** {special_name}")
        lines.append(f"**Participantes:** {participants}")
        lines.append(detail)

        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)


class _RaceImpulseButton(discord.ui.Button):
    def __init__(self, view: "_RaceImpulseEventView", index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=str(index + 1), disabled=True, custom_id=f"race_impulse:{view.event_token}:{index}")
        self._view = view
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self._view.handle_press(interaction, self.index)


class _RaceImpulseEventView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, stage_name: str, *, event_token: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.stage_name = stage_name
        self.event_token = str(event_token)
        self.last_best_user_id: int | None = None
        self.last_best_target_bonus: float = 0.0
        self.last_best_tier: str | None = None
        self.message: discord.Message | None = None
        self.finished = False
        self.step_index = -1
        self.active_index: int | None = None
        self.order = random.sample(range(_RACE_IMPULSE_BUTTON_COUNT), _RACE_IMPULSE_STAGE_COUNT)
        self.participant_ids = tuple(sorted(int(user_id) for user_id in (self.session.get("locked_participants", set()) or [])))
        self.participant_id_set = set(self.participant_ids)
        self.results: dict[int, dict] = {}
        self.edit_lock = asyncio.Lock()
        self._last_render_signature = None
        self._results_applied = False
        self._pending_render_signature = None
        self.activated_indices: set[int] = set()
        # Timestamp UTC de quando o step atual começou (setado em _activate_step,
        # limpo em _close_current_step). Usado no handle_press para detectar e
        # descartar cliques atrasados: interactions cujo `created_at` é anterior
        # a esse instante vieram de um step anterior que já foi fechado, e
        # contabilizá-las no step atual altera o hits count aleatoriamente.
        self.step_start_time: datetime.datetime | None = None
        self.buttons = [_RaceImpulseButton(self, idx) for idx in range(_RACE_IMPULSE_BUTTON_COUNT)]
        self._rebuild()

    def _render_signature(self):
        return (
            bool(self.finished),
            int(self.step_index),
            self.active_index,
            tuple((button.disabled, button.label, int(button.style)) for button in self.buttons),
        )

    def _build_header_lines(self) -> list[str]:
        if self.finished:
            return [
                "# ⚡ Evento de impulso encerrado",
                f"**Fase:** {self.stage_name}",
                "Impulsos calculados e aplicados.",
            ]
        if self.step_index < 0:
            return [
                "# ⚡ Evento de impulso",
                f"**Fase:** {self.stage_name}",
                "Prepare-se. O primeiro botão pode acender a qualquer momento.",
            ]
        return [
            "# ⚡ Evento de impulso",
            f"**Fase:** {self.stage_name}",
            f"Aperte o botão cinza que acendeu. **Etapa {self.step_index + 1}/{_RACE_IMPULSE_STAGE_COUNT}**",
        ]

    def _rebuild(self):
        self.clear_items()
        row1 = discord.ui.ActionRow(self.buttons[0])
        row2 = discord.ui.ActionRow(self.buttons[1])
        row3 = discord.ui.ActionRow(self.buttons[2])
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._build_header_lines())),
            discord.ui.Separator(),
            row1,
            row2,
            row3,
            accent_color=discord.Color.dark_grey(),
        )
        self.add_item(container)

    async def refresh_message(self):
        if self.message is None:
            return
        signature = self._render_signature()
        self._pending_render_signature = signature
        if self.edit_lock.locked():
            return
        async with self.edit_lock:
            while self.message is not None:
                current_signature = self._pending_render_signature
                if current_signature == self._last_render_signature:
                    break
                self._rebuild()
                edit_state = await self.cog._safe_edit_message_view(self.message, self)
                if edit_state == "ok":
                    self._last_render_signature = current_signature
                elif edit_state == "missing":
                    self.message = None
                    break
                else:
                    break
                latest_signature = self._render_signature()
                self._pending_render_signature = latest_signature
                if latest_signature == self._last_render_signature:
                    break

    def _make_result_entry(self) -> dict:
        return {"times": [None] * _RACE_IMPULSE_STAGE_COUNT, "success": [False] * _RACE_IMPULSE_STAGE_COUNT}

    async def handle_press(self, interaction: discord.Interaction, button_index: int):
        ack_needed = not interaction.response.is_done()
        try:
            user = interaction.user
            current_step = int(self.step_index)
            active_index = self.active_index
            finished = bool(self.finished)
            step_start = self.step_start_time

            if finished or active_index is None or current_step < 0:
                return
            if interaction.guild is None or user is None:
                return

            # Descarta cliques atrasados. Com STEP_SECONDS curto (1.0s), é comum
            # que um clique do step N chegue ao bot DEPOIS que _close_current_step
            # e _activate_step(N+1) já tenham rodado. Sem esse filtro, o clique é
            # contabilizado no step errado: times[N+1] é preenchido antes do
            # usuário ver o novo botão acender, o que (a) pode dar um "acerto" ou
            # "erro" aleatório no step N+1 e (b) bloqueia o clique correto do
            # usuário no step N+1 via o guard `times[current_step] is not None`.
            # interaction.created_at é timezone-aware UTC, step_start também.
            interaction_time = getattr(interaction, "created_at", None)
            if (
                step_start is not None
                and interaction_time is not None
                and interaction_time < step_start
            ):
                return

            user_id = int(getattr(user, "id", 0) or 0)
            if user_id <= 0 or user_id not in self.participant_id_set:
                return
            if current_step >= _RACE_IMPULSE_STAGE_COUNT:
                return

            entry = self.results.get(user_id)
            if entry is None:
                entry = self._make_result_entry()
                self.results[user_id] = entry

            times = entry["times"]
            success = entry["success"]
            if times[current_step] is not None:
                return

            times[current_step] = 0.0
            success[current_step] = int(button_index) == int(active_index)
        except Exception:
            return
        finally:
            if ack_needed and not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

    def _activate_step(self, index: int):
        self.step_index = index
        self.active_index = self.order[index]
        self.activated_indices.add(int(self.active_index))
        # Marca o instante em que o step começou. Cliques cujo `created_at` seja
        # anterior a isso são descartados em handle_press como atrasados.
        self.step_start_time = datetime.datetime.now(datetime.timezone.utc)
        for idx, button in enumerate(self.buttons):
            button.disabled = idx != self.active_index
            button.label = _RACE_IMPULSE_EMOJI if idx in self.activated_indices else str(idx + 1)
            button.style = discord.ButtonStyle.secondary

    def _close_current_step(self):
        if self.active_index is not None:
            self.activated_indices.add(int(self.active_index))
        for button in self.buttons:
            button.disabled = True
            button.label = _RACE_IMPULSE_EMOJI if button.index in self.activated_indices else str(button.index + 1)
            button.style = discord.ButtonStyle.secondary
        self.active_index = None
        # Limpa o timestamp do step ao fechar. Qualquer clique que chegar entre
        # _close_current_step e _activate_step do próximo step é bloqueado pelo
        # check `active_index is None` em handle_press.
        self.step_start_time = None
        current_step = self.step_index
        if current_step < 0:
            return
        for user_id in self.participant_ids:
            entry = self.results.get(int(user_id))
            if entry is None:
                entry = self._make_result_entry()
                self.results[int(user_id)] = entry
            if entry["times"][current_step] is None:
                entry["times"][current_step] = _RACE_IMPULSE_STEP_SECONDS
                entry["success"][current_step] = False

    def _successful_steps(self, entry: dict) -> int:
        success = list(entry.get("success") or [])
        if success:
            return sum(1 for ok in success if bool(ok))
        times = list(entry.get("times") or [])
        return sum(1 for reaction_time in times if reaction_time is not None and float(reaction_time) < _RACE_IMPULSE_STEP_SECONDS)

    def _impulse_award_chance(self, user_id: int, hits: int) -> float:
        if hits >= _RACE_IMPULSE_STAGE_COUNT:
            chance = 0.35
        elif hits == _RACE_IMPULSE_STAGE_COUNT - 1:
            chance = 0.20
        else:
            chance = 0.0
        if chance > 0.0 and self.cog._race_is(self.guild.id, int(user_id), "sortudo"):
            chance += 0.20
        return max(0.0, min(1.0, chance))

    def _random_impulse_tier(self, hits: int, user_id: int) -> str | None:
        chance = self._impulse_award_chance(user_id, hits)
        if chance <= 0.0 or random.random() >= chance:
            return None
        roll = random.random()
        if roll < 0.40:
            return "pequeno"
        if roll < 0.75:
            return "medio"
        return "grande"

    def _tier_bonus(self, tier: str, stage_name: str) -> float:
        stage_key = stage_name.strip().lower()
        bonus_table = {
            "largada": {"pequeno": (1.25, 1.65), "medio": (1.95, 2.55), "grande": (2.95, 3.75)},
            "meio": {"pequeno": (1.45, 1.90), "medio": (2.20, 2.95), "grande": (3.25, 4.20)},
            "sprint final": {"pequeno": (1.75, 2.25), "medio": (2.55, 3.35), "grande": (3.70, 4.80)},
        }
        ranges = bonus_table.get(stage_key, bonus_table["meio"])
        start, end = ranges.get(tier, (0.0, 0.0))
        return round(random.uniform(start, end), 4)

    def _tier_emoji(self, tier: str | None) -> str:
        return _HORSE_DASH if tier == "grande" else _HORSE_BOOST

    def _tier_label(self, tier: str | None) -> str:
        return {"pequeno": "impulso pequeno", "medio": "impulso médio", "grande": "impulso grande"}.get(str(tier or "").lower(), "impulso")

    def _apply_results(self, *, up_to_step: int | None = None):
        if self._results_applied:
            return list(self.session.get("recent_impulse_awards") or [])
        active_impulses = self.session.setdefault("active_impulses", {})
        state_map = self.session.setdefault("state_map", {})
        participants = list(self.participant_ids)

        awarded: list[tuple[int, str, float, int]] = []
        self.last_best_user_id = None
        self.last_best_target_bonus = 0.0
        self.last_best_tier = None

        for user_id in participants:
            entry = self.results.setdefault(user_id, {"times": [None] * _RACE_IMPULSE_STAGE_COUNT, "success": [False] * _RACE_IMPULSE_STAGE_COUNT})
            times = list(entry.get("times") or [])
            if len(times) < _RACE_IMPULSE_STAGE_COUNT:
                times.extend([None] * (_RACE_IMPULSE_STAGE_COUNT - len(times)))
            hits = self._successful_steps(entry)
            award_chance = self._impulse_award_chance(user_id, hits)
            tier = self._random_impulse_tier(hits, user_id)
            entry["hits"] = hits
            entry["award_chance"] = award_chance
            entry["sortudo_bonus_applied"] = bool(award_chance > 0.35 and self.cog._race_is(self.guild.id, int(user_id), "sortudo"))
            entry["tier"] = tier
            entry["failed_trigger"] = bool(award_chance > 0.0 and not tier)
            if not tier:
                continue
            bonus = self._tier_bonus(tier, self.stage_name)
            entry["bonus"] = bonus
            active_impulses[user_id] = {"kind": str(tier), "ticks_left": 2, "per_tick": float(bonus)}
            state_map[user_id] = self._tier_emoji(tier)
            awarded.append((user_id, tier, bonus, hits))

        awarded.sort(key=lambda item: ({"grande": 3, "medio": 2, "pequeno": 1}.get(item[1], 0), item[3], item[2], -item[0]), reverse=True)
        self.session["impulse_flash_users"] = {int(user_id) for user_id, _tier, _bonus, _hits in awarded}
        self.session["impulse_flash_levels"] = {int(user_id): str(tier) for user_id, tier, _bonus, _hits in awarded}
        self.session["recent_impulse_awards"] = [
            {"user_id": int(user_id), "tier": str(tier), "bonus": float(bonus), "stage": self.stage_name, "hits": int(hits)}
            for user_id, tier, bonus, hits in awarded[:3]
        ]
        if awarded:
            best_user_id, best_tier, best_bonus, _best_hits = awarded[0]
            self.last_best_user_id = int(best_user_id)
            self.last_best_target_bonus = float(best_bonus)
            self.last_best_tier = str(best_tier)
            mention_lines = []
            for user_id, tier, _bonus, _hits in awarded[:3]:
                member = self.guild.get_member(int(user_id))
                if member is not None:
                    mention_lines.append(f"{self._tier_emoji(tier)} {member.mention} recebeu {self._tier_label(tier)}.")
            if mention_lines:
                self.session["narration"] = "\n".join(mention_lines)
                self.session["narration_hold_ticks"] = 3
        else:
            self.session["recent_impulse_awards"] = []
            self.session["narration"] = ""
            self.session["narration_hold_ticks"] = 0

        self._results_applied = True
        return list(self.session.get("recent_impulse_awards") or [])



class _RaceStateView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, *, finished: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.finished = finished

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or ("🏁 Todos cruzaram a linha." if finished else ""))
        lines = cog._build_race_lines(guild, session)

        if finished:
            title = "# 🏁 Resultado da corrida"
        else:
            title = "# 🔥 Reta final" if session.get("final_stretch") else "# 🐎 Corrida em andamento"

        header_lines = [title, f"**Condição:** {condition_name}"]
        if special_name:
            header_lines.append(f"**Especial:** {special_name}")
        if session.get("rodada_cheia"):
            header_lines.append("🎉 **Rodada cheia**")

        track_lines = list(lines)
        if narration:
            track_lines += ["", narration]
        impulse_status = str(session.get("impulse_status") or "").strip()
        if impulse_status:
            track_lines.append(impulse_status)

        items = [
            discord.ui.TextDisplay("\n".join(header_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(track_lines)),
        ]

        result_lines = session.get("result_lines") or []
        if finished and result_lines:
            items.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(result_lines)),
            ])

        container = discord.ui.Container(*items, accent_color=cog._race_color(session, finished=finished))
        self.add_item(container)


class GincanaCorridaMixin:
    _RACE_STALE_LOBBY_SECONDS = _CORRIDA_LOBBY_SECONDS + 45.0
    _RACE_STALE_ACTIVE_SECONDS = _CORRIDA_DURATION_SECONDS + 120.0

    async def _safe_edit_message_view(self, message: discord.Message | None, view: discord.ui.View | discord.ui.LayoutView) -> str:
        return await self._safe_view_edit(message, view)

    async def _cleanup_stale_race_session(self, guild_id: int, session: dict):
        if session.get("_cleanup_started"):
            return
        session["_cleanup_started"] = True
        self._touch_runtime_state(session, kind='corrida', guild_id=guild_id)
        try:
            await self._stop_active_impulse_event(session)
        except Exception:
            pass
        refund_ids = [int(user_id) for user_id in set(session.get("locked_participants", set()) or [])]
        for user_id in refund_ids:
            try:
                await self._change_user_chips(guild_id, int(user_id), CORRIDA_STAKE, reason="Devolução da corrida (cancelada)")
            except Exception:
                pass
        session["ended"] = True
        session["starting"] = False
        session["started"] = False
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            try:
                await self._close_lobby_message(session, guild, title="🐎 Corrida cancelada", detail="A corrida ficou inativa por tempo demais e foi encerrada automaticamente. Todas as entradas foram devolvidas.")
            except Exception:
                pass
        self._race_sessions.pop(guild_id, None)

    def _get_race_session(self, guild_id: int) -> dict | None:
        session = self._race_sessions.get(guild_id)
        if session and session.get("ended"):
            self._race_sessions.pop(guild_id, None)
            return None
        if session is not None:
            max_idle = self._RACE_STALE_ACTIVE_SECONDS if session.get("started") or session.get("starting") else self._RACE_STALE_LOBBY_SECONDS
            if self._runtime_state_is_stale(session, max_idle=max_idle, max_age=max_idle * 3):
                if not session.get("_cleanup_started"):
                    asyncio.create_task(self._cleanup_stale_race_session(guild_id, session))
                self._race_sessions.pop(guild_id, None)
                return None
        return session

    def _get_race_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
        channel = guild.get_channel(int(session.get("voice_channel_id") or 0))
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _get_race_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
        participants: list[discord.Member] = []
        for user_id in sorted(session.get("locked_participants", set())):
            member = guild.get_member(int(user_id))
            if member is None or getattr(member, "bot", False):
                continue
            participants.append(member)
        return participants

    def _get_race_impulse_schedule(self, session: dict) -> tuple[tuple[int, str], ...]:
        condition_name = str((session.get("condition") or {}).get("name") or "").strip().lower()
        if condition_name == "pista rápida":
            return _RACE_IMPULSE_WINDOWS_FAST
        return _RACE_IMPULSE_WINDOWS_NORMAL

    def _race_pot_total(self, session: dict) -> int:
        participant_count = len(set(session.get("locked_participants", set()) or []))
        return max(0, participant_count - 1) * CORRIDA_STAKE

    def _race_rodada_cheia_pending_bonus(self, session: dict) -> int:
        """Bonus that will be added at race start based on participant count tier."""
        if session.get("started"):
            return 0
        participant_count = len(set(session.get("locked_participants", set()) or []))
        if participant_count >= CORRIDA_RODADA_CHEIA_LARGE_THRESHOLD:
            return CORRIDA_RODADA_CHEIA_BONUS_LARGE
        if participant_count >= CORRIDA_RODADA_CHEIA_THRESHOLD:
            return CORRIDA_RODADA_CHEIA_BONUS
        return 0

    def _race_is_rodada_cheia(self, session: dict) -> bool:
        if session.get("rodada_cheia"):
            return True
        return self._race_rodada_cheia_pending_bonus(session) > 0

    def _race_lobby_view_matches(self, session: dict, source_view: discord.ui.LayoutView | None) -> bool:
        if source_view is None:
            return True
        current_view = session.get("view")
        if current_view is source_view:
            return True
        if isinstance(current_view, _RaceLobbyView) and isinstance(source_view, _RaceLobbyView):
            return str(current_view.view_token) == str(source_view.view_token)
        return False

    def _race_placement_emoji(self, index: int) -> str:
        return {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, "🔘")

    def _race_color(self, session: dict, *, finished: bool = False) -> discord.Color:
        if finished:
            return discord.Color.green()
        if not session.get("started"):
            special = session.get("special") or {}
            return special.get("color") or discord.Color.blurple()
        if session.get("final_stretch"):
            return discord.Color.red()
        return discord.Color.orange()

    def _render_race_track(self, pos: float, state_emoji: str) -> str:
        visual_pos = int(pos)
        visual_pos = max(0, min(_CORRIDA_TRACK_LENGTH - 1, visual_pos))
        before = "▰" * visual_pos
        after = "▱" * max(0, _CORRIDA_TRACK_LENGTH - visual_pos - 1)
        return f"{before}{state_emoji}{after}"

    def _ordered_race_members(self, guild: discord.Guild, session: dict) -> list[tuple[int, discord.Member]]:
        participants = self._get_race_participants(guild, session)
        progress_map = session.get("progress", {}) or {}
        arrival_groups = [list(group) for group in (session.get("arrival_groups") or []) if group]
        rank_map = _shared_rank_map(arrival_groups)
        ordered = sorted(
            participants,
            key=lambda m: (rank_map.get(m.id, 9999), -float(progress_map.get(m.id, 0.0)), m.display_name.casefold()),
        )
        return [(rank_map.get(member.id, 9999), member) for member in ordered]

    def _build_race_lines(self, guild: discord.Guild, session: dict) -> list[str]:
        ordered_with_ranks = self._ordered_race_members(guild, session)
        if not ordered_with_ranks:
            return ["🔘 Ninguém entrou ainda."]

        progress_map = session.get("progress", {}) or {}
        state_map = session.get("state_map", {}) or {}
        active_impulses = {int(user_id): data for user_id, data in (session.get("active_impulses") or {}).items()}
        flash_users = {int(user_id) for user_id in (session.get("impulse_flash_users") or set())}
        flash_levels = {int(user_id): str(level) for user_id, level in (session.get("impulse_flash_levels") or {}).items()}
        rank_map = {member.id: rank for rank, member in ordered_with_ranks}
        lines: list[str] = []
        for _index, (rank, member) in enumerate(ordered_with_ranks, start=1):
            medal = self._race_placement_emoji(rank)
            pos = float(progress_map.get(member.id, 0.0))
            state_emoji = str(state_map.get(member.id) or _HORSE_START)
            impulse_state = active_impulses.get(member.id) or {}
            impulse_kind = str((impulse_state or {}).get("kind") or "").lower()
            if int((impulse_state or {}).get("ticks_left") or 0) > 0 and state_emoji not in {_HORSE_FINISH, _HORSE_TRIP}:
                state_emoji = _HORSE_DASH if impulse_kind == "grande" else _HORSE_BOOST
            elif member.id in flash_users and state_emoji not in {_HORSE_FINISH, _HORSE_TRIP}:
                flash_level = str(flash_levels.get(member.id) or "").lower()
                if flash_level == "grande":
                    state_emoji = _HORSE_DASH
                elif flash_level in {"medio", "médio", "pequeno"}:
                    state_emoji = _HORSE_BOOST
            if rank_map.get(member.id, 9999) != 9999 and state_emoji == _HORSE_FINISH:
                pos = _CORRIDA_TRACK_LENGTH - 1
            lines.append(f"{medal} {member.mention}")
            lines.append(self._render_race_track(pos, state_emoji))
            lines.append("")
        if lines and not lines[-1]:
            lines.pop()
        return lines

    def _make_race_embed(self, guild: discord.Guild, session: dict, *, finished: bool = False) -> discord.Embed:
        pot_total = self._race_pot_total(session)
        bonus_pool = int(session.get("bonus_pool", 0) or 0)
        pending_bonus = self._race_rodada_cheia_pending_bonus(session)
        effective_bonus = bonus_pool + pending_bonus
        title = "🐎 Corrida aberta"
        if session.get("started"):
            title = "🏁 Corrida encerrada" if finished else ("🔥 Reta final" if session.get("final_stretch") else "🐎 Corrida em andamento")

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or ("" if session.get("started") else "📣 A corrida vai começar."))
        lines = self._build_race_lines(guild, session)
        description_parts = [f"Condição: **{condition_name}**"]
        if special_name:
            description_parts.append(f"Especial: **{special_name}**")
        if self._race_is_rodada_cheia(session):
            description_parts.append("🎉 **Rodada cheia**")
        description_parts.append("")
        description_parts.extend(lines)
        description_parts.append("")
        description_parts.append("────────")
        description_parts.append(narration)
        embed = discord.Embed(title=title, description="\n".join(description_parts), color=self._race_color(session, finished=finished))

        if not session.get("started"):
            embed.add_field(name="Entrada", value=self._chip_amount(CORRIDA_STAKE), inline=True)
            embed.add_field(name="Pote atual", value=self._chip_amount(pot_total) + (f" • Bônus: {self._bonus_chip_amount(effective_bonus)}" if effective_bonus > 0 else ""), inline=True)
            embed.add_field(name="Duração", value=f"**{_CORRIDA_DURATION_SECONDS}s**", inline=True)
            embed.set_footer(text="Entre no lobby. O criador ou a staff pode iniciar com 🏁 quando houver pelo menos 2 participantes.")
        return embed

    async def _close_lobby_message(self, session: dict, guild: discord.Guild, *, title: str, detail: str):
        lobby_message = session.get("message")
        if lobby_message is None:
            return
        old_view = session.get("view")
        closed_view = _RaceLobbyClosedView(session, guild, title, detail)
        session["view"] = closed_view
        edit_state = await self._safe_edit_message_view(lobby_message, closed_view)
        if edit_state == "missing":
            session["message"] = None
        elif old_view is not None and old_view is not closed_view:
            self._schedule_race_view_retire(session, old_view, delay=2.0)

    async def _handle_race_button(self, interaction: discord.Interaction, view: _RaceLobbyView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await self._send_component_feedback(interaction, "Servidor inválido.")
            return

        session = self._get_race_session(guild.id)
        if session is None or session.get("ended") or session.get("started"):
            await self._send_component_feedback(interaction, "Essa corrida não está mais aceitando entradas.")
            return

        locked = session.setdefault("locked_participants", set())
        if user.id in locked:
            await self._send_component_feedback(interaction, "Você já entrou nessa corrida.")
            return

        await self._safe_defer_component_interaction(interaction)

        needs_negative_confirm = self._needs_negative_confirmation(guild.id, user.id, CORRIDA_STAKE)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, user.id, CORRIDA_STAKE, title="🐎 Confirmar entrada")
            if not confirmed:
                return

        entry_text = self._entry_consume_text(guild.id, user.id, CORRIDA_STAKE)
        entry_spend = self._entry_spend_parts(guild.id, user.id, CORRIDA_STAKE)
        paid, _balance, chip_note = await self._try_consume_chips(guild.id, user.id, CORRIDA_STAKE, reason="Entrada na corrida")
        if needs_negative_confirm:
            chip_note = None
        if not paid:
            await self._send_component_feedback(interaction, chip_note or "Você não tem saldo suficiente para entrar nessa corrida.")
            return

        locked.add(user.id)
        session.setdefault("entry_spend", {})[user.id] = entry_spend
        session.setdefault("race_interactions", {})[user.id] = interaction
        session.setdefault("progress", {})[user.id] = 0.0
        session.setdefault("state_map", {})[user.id] = _HORSE_START
        view.join_button.label = f"🐎 Entrar ({len(self._get_race_participants(guild, session))})"
        await self._send_race_lobby_feedback(interaction, guild.id, user.id, chip_note or entry_text)
        await self._refresh_race_message(guild.id)

    async def _handle_race_start_button(self, interaction: discord.Interaction, view: _RaceLobbyView):
        guild = interaction.guild
        user = interaction.user
        if not await self._safe_defer_component_interaction(interaction):
            return
        if guild is None or not isinstance(user, discord.Member):
            await self._send_component_feedback(interaction, "Servidor inválido.")
            return

        session = self._get_race_session(guild.id)
        if session is None or session.get("ended") or session.get("started") or session.get("starting"):
            await self._send_component_feedback(interaction, "Essa corrida já foi iniciada.")
            return

        is_owner = int(session.get("owner_id") or 0) == user.id
        if not is_owner and not self._is_staff_member(user):
            await self._send_component_feedback(interaction, "Só o criador da corrida ou a staff pode iniciar.")
            return

        participants = self._get_race_participants(guild, session)
        if len(participants) < 2:
            await self._send_component_feedback(interaction, "A corrida precisa de pelo menos 2 participantes para começar.")
            return

        session["starting"] = True
        try:
            started_ok = await self._finish_race_lobby(guild.id, reason="manual_start", source_view=view, allow_when_starting=True)
            if not started_ok:
                fresh_session = self._race_sessions.get(guild.id)
                if fresh_session is not None and not fresh_session.get("ended"):
                    fresh_session["starting"] = False
                await self._send_component_feedback(interaction, "Não foi possível iniciar a corrida agora.")
                return
        except Exception:
            fresh_session = self._race_sessions.get(guild.id)
            if fresh_session is not None and not fresh_session.get("ended"):
                fresh_session["starting"] = False
                fresh_session["started"] = False
                fresh_session["impulse_status"] = ""
                fresh_session["active_impulse_task"] = None
                fresh_session["active_impulse_message"] = None
                fresh_session["_last_render_key"] = None
                try:
                    self._touch_runtime_state(fresh_session, kind='corrida', guild_id=guild.id)
                    await self._refresh_race_message(guild.id)
                except Exception:
                    pass
            await self._send_component_feedback(interaction, "Não foi possível iniciar a corrida agora.")

    def _race_render_key(self, session: dict):
        return (
            bool(session.get("started")),
            bool(session.get("ended")),
            bool(session.get("final_stretch")),
            str(session.get("narration") or ""),
            str(session.get("impulse_status") or ""),
            tuple(sorted((int(k), round(float(v), 4)) for k, v in (session.get("progress") or {}).items())),
            tuple(sorted((int(k), str(v)) for k, v in (session.get("state_map") or {}).items())),
            tuple(sorted((int(k), str(v.get("kind") or ""), int(v.get("ticks_left") or 0), round(float(v.get("per_tick") or 0.0), 4)) for k, v in (session.get("active_impulses") or {}).items())),
            tuple(sorted((int(k), str(v)) for k, v in (session.get("impulse_flash_levels") or {}).items())),
            tuple(tuple(int(user_id) for user_id in group) for group in (session.get("arrival_groups") or [])),
            tuple(str(line) for line in (session.get("result_lines") or [])),
            tuple(sorted(int(x) for x in (session.get("locked_participants") or set()))),
        )

    def _track_race_aux_task(self, session: dict, task: asyncio.Task | None):
        if task is None:
            return None
        tasks = session.setdefault("_aux_tasks", set())
        tasks.add(task)

        def _cleanup(done_task: asyncio.Task):
            try:
                tasks.discard(done_task)
            except Exception:
                pass

        task.add_done_callback(_cleanup)
        return task

    async def _safe_defer_component_interaction(self, interaction: discord.Interaction) -> bool:
        try:
            if interaction.response.is_done():
                return True
            await interaction.response.defer()
            return True
        except Exception:
            return bool(interaction.response.is_done())

    async def _send_component_feedback(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = True) -> bool:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
            return True
        except Exception:
            return False

    async def _retire_race_view_later(self, session: dict, view: discord.ui.View | discord.ui.LayoutView | None, *, delay: float = 4.0):
        if view is None or not isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            return
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            view.stop()
        except Exception:
            pass

    def _schedule_race_view_retire(self, session: dict, view: discord.ui.View | discord.ui.LayoutView | None, *, delay: float = 4.0):
        if view is None or not isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            return None
        return self._track_race_aux_task(session, asyncio.create_task(self._retire_race_view_later(session, view, delay=delay)))

    def _schedule_impulse_message_delete(self, session: dict, message: discord.Message | None, *, immediate: bool = False):
        if message is None:
            return None
        return self._track_race_aux_task(session, asyncio.create_task(self._delete_impulse_message(message, immediate=immediate)))

    async def _refresh_race_message(self, guild_id: int):
        session = self._get_race_session(guild_id)
        if session is not None:
            self._touch_runtime_state(session, kind='corrida', guild_id=guild_id)
        if session is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = session.get("message")
        if message is None:
            return
        session["_pending_render_key"] = self._race_render_key(session)
        edit_lock = session.setdefault("_edit_lock", asyncio.Lock())
        if edit_lock.locked():
            return
        async with edit_lock:
            while True:
                try:
                    message = session.get("message")
                    if message is None:
                        return
                    render_key = session.get("_pending_render_key")
                    if render_key is None:
                        render_key = self._race_render_key(session)
                        session["_pending_render_key"] = render_key
                    if render_key == session.get("_last_render_key"):
                        return
                    old_view = session.get("view")
                    if not session.get("started"):
                        view = _RaceLobbyView(self, guild_id, session, guild, timeout=_CORRIDA_LOBBY_SECONDS)
                    else:
                        view = _RaceStateView(self, guild, session, finished=bool(session.get("ended")))
                    session["view"] = view
                    edit_state = await self._safe_edit_message_view(message, view)
                    if edit_state == "ok":
                        session["_last_render_key"] = render_key
                        if old_view is not None and old_view is not view:
                            retire_delay = 15.0 if isinstance(old_view, _RaceLobbyView) and not session.get("started") else 4.0
                            self._schedule_race_view_retire(session, old_view, delay=retire_delay)
                    elif edit_state == "missing":
                        session["message"] = None
                        return
                    else:
                        return
                    latest_key = self._race_render_key(session)
                    session["_pending_render_key"] = latest_key
                    if latest_key == session.get("_last_render_key"):
                        return
                except Exception:
                    return

    def _nominal_race_pools(self, participant_count: int, pot_total: int) -> list[int]:
        if participant_count <= 0 or pot_total <= 0:
            return []
        if participant_count == 2:
            return [pot_total]
        if participant_count <= 5:
            first_pool = int(round(pot_total * 0.75))
            second_pool = max(0, pot_total - first_pool)
            return [first_pool, second_pool]
        first_pool = int(round(pot_total * 0.6))
        second_pool = int(round(pot_total * 0.25))
        third_pool = max(0, pot_total - first_pool - second_pool)
        return [first_pool, second_pool, third_pool]

    def _allocate_race_rewards(self, arrival_groups: list[list[discord.Member]], pot_total: int) -> tuple[dict[int, int], list[tuple[str, list[discord.Member], int]]]:
        rewards: dict[int, int] = {}
        placements: list[tuple[str, list[discord.Member], int]] = []
        flat_participants = [member for group in arrival_groups for member in group]
        if not flat_participants or pot_total <= 0:
            return rewards, placements

        pools = self._nominal_race_pools(len(flat_participants), pot_total)
        if not pools:
            return rewards, placements

        next_rank = 1
        for group in arrival_groups:
            if not group:
                continue
            start_index = next_rank - 1
            end_index = min(len(pools), start_index + len(group))
            occupied_pool = sum(pools[start_index:end_index])
            if occupied_pool > 0:
                base_share = occupied_pool // len(group)
                remainder = occupied_pool % len(group)
                ordered_group = sorted(group, key=lambda m: m.display_name.casefold())
                for idx, member in enumerate(ordered_group):
                    rewards[member.id] = rewards.get(member.id, 0) + base_share + (1 if idx < remainder else 0)
                placements.append((self._race_placement_emoji(next_rank), ordered_group, occupied_pool))
            next_rank += len(group)
            if next_rank > len(pools):
                break
        return rewards, placements

    def _pick_race_narration(self, guild: discord.Guild, session: dict, participants: list[discord.Member], tick_events: list[tuple[str, discord.Member]], *, tick: int, final_tick: bool = False) -> str:
        if final_tick:
            return "🏁 Todos cruzaram a linha."
        event_lines: list[str] = []
        for event_key, member in tick_events:
            if event_key == "boost_pequeno":
                event_lines.append(f"{_HORSE_BOOST} {member.mention} recebeu impulso pequeno.")
            elif event_key == "boost_medio":
                event_lines.append(f"{_HORSE_BOOST} {member.mention} recebeu impulso médio.")
            elif event_key == "boost_grande":
                event_lines.append(f"{_HORSE_DASH} {member.mention} disparou com impulso grande.")
            elif event_key == "trip":
                event_lines.append(f"💥 {member.mention} tropeçou.")
            elif event_key == "lead":
                event_lines.append(f"👑 {member.mention} assumiu a liderança.")
            if len(event_lines) >= 3:
                break
        return "\n".join(event_lines[:3])

    def _has_impulse_event(self, tick_events: list[tuple[str, discord.Member]]) -> bool:
        return any(str(event_key).startswith("boost_") for event_key, _member in tick_events)


    def _build_finalized_order(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
        return [member for _rank, member in self._ordered_race_members(guild, session)]

    def _build_arrival_member_groups(self, guild: discord.Guild, session: dict) -> list[list[discord.Member]]:
        participants = self._get_race_participants(guild, session)
        participant_ids = {member.id for member in participants}
        progress = session.get("progress", {}) or {}
        groups: list[list[discord.Member]] = []
        for raw_group in session.get("arrival_groups", []) or []:
            members = [guild.get_member(int(user_id)) for user_id in raw_group]
            valid_members = [member for member in members if member is not None and member.id in participant_ids]
            if valid_members:
                groups.append(valid_members)
        arrived_ids = {member.id for group in groups for member in group}
        leftovers = sorted(
            [member for member in participants if member.id not in arrived_ids],
            key=lambda m: (-float(progress.get(m.id, 0.0)), m.display_name.casefold()),
        )
        for member in leftovers:
            groups.append([member])
        return groups

    async def _delete_impulse_message(self, message: discord.Message | None, *, immediate: bool = False):
        if message is None:
            return
        try:
            if not immediate:
                await asyncio.sleep(_RACE_IMPULSE_DELETE_DELAY_SECONDS)
            await message.delete()
        except discord.NotFound:
            pass
        except Exception:
            pass

    async def _stop_active_impulse_event(self, session: dict, *, keep_status: bool = False):
        active_task = session.get("active_impulse_task")
        if active_task is not None:
            session["active_impulse_task"] = None
            if not active_task.done():
                active_task.cancel()
                try:
                    await active_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        active_message = session.get("active_impulse_message")
        if active_message is not None:
            session["active_impulse_message"] = None
            self._schedule_impulse_message_delete(session, active_message, immediate=True)
        if not keep_status:
            session["impulse_status"] = ""

    async def _run_race_impulse_event(self, guild: discord.Guild, session: dict, stage_name: str):
        self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
        if session.get("ended"):
            return []
        channel = guild.get_channel(int(session.get("text_channel_id") or 0))
        if channel is None or not hasattr(channel, "send"):
            return []

        event_counter = int(session.get("_impulse_event_counter", 0) or 0) + 1
        session["_impulse_event_counter"] = event_counter
        event_token = f"{guild.id}:{event_counter}:{random.getrandbits(24):06x}"
        event_view = _RaceImpulseEventView(self, guild, session, stage_name, event_token=event_token)
        session["impulse_status"] = f"⏸ Evento de impulso ({stage_name.lower()}) em andamento."
        await self._refresh_race_message(guild.id)
        event_message = None
        try:
            event_message = await channel.send(view=event_view)
            session["active_impulse_message"] = event_message
            event_view.message = event_message
            event_view._last_render_signature = event_view._render_signature()
            if event_view.message is None:
                raise discord.NotFound(response=None, message="Impulse event message disappeared")
            if _RACE_IMPULSE_INITIAL_DELAY > 0:
                await asyncio.sleep(_RACE_IMPULSE_INITIAL_DELAY)
            completed_all_steps = True
            for step_index in range(_RACE_IMPULSE_STAGE_COUNT):
                if session.get("ended") or event_view.message is None:
                    completed_all_steps = False
                    break
                event_view._activate_step(step_index)
                await event_view.refresh_message()
                await asyncio.sleep(_RACE_IMPULSE_STEP_SECONDS)
                event_view._close_current_step()
                if step_index + 1 >= _RACE_IMPULSE_STAGE_COUNT:
                    event_view.finished = True
                    await event_view.refresh_message()

            if not event_view.finished:
                event_view.finished = True
                event_view._close_current_step()
                await event_view.refresh_message()
            elif not completed_all_steps and event_view.active_index is not None:
                event_view._close_current_step()
            awards = list(event_view._apply_results() or [])
            if event_view.last_best_user_id is not None and event_view.last_best_target_bonus > float((session.get("best_impulse") or {}).get("bonus", 0.0) or 0.0):
                session["best_impulse"] = {
                    "user_id": int(event_view.last_best_user_id),
                    "stage": stage_name,
                    "bonus": float(event_view.last_best_target_bonus),
                    "tier": str(event_view.last_best_tier or ""),
                }
            if session.get("active_impulse_message") is event_message:
                session["impulse_status"] = ""
                if not awards and not str(session.get("narration") or "").strip():
                    session["narration"] = ""
            self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
            await self._refresh_race_message(guild.id)
            return awards
        except asyncio.CancelledError:
            event_view.finished = True
            if event_view.active_index is not None:
                event_view._close_current_step()
            await event_view.refresh_message()
            raise
        except Exception:
            session["impulse_status"] = ""
            self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
            await self._refresh_race_message(guild.id)
        finally:
            if session.get("active_impulse_message") is event_message:
                session["active_impulse_message"] = None
            if session.get("active_impulse_task") is asyncio.current_task():
                session["active_impulse_task"] = None
            if event_message is not None:
                self._schedule_impulse_message_delete(session, event_message)

    async def _finish_race_lobby(self, guild_id: int, *, reason: str, source_view: discord.ui.LayoutView | None = None, allow_when_starting: bool = False) -> bool:
        session = self._get_race_session(guild_id)
        if session is not None:
            self._touch_runtime_state(session, kind='corrida', guild_id=guild_id)
        if session is None or session.get("ended") or session.get("started"):
            return False
        if session.get("starting") and not allow_when_starting:
            return False
        if not self._race_lobby_view_matches(session, source_view):
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._race_sessions.pop(guild_id, None)
            return False

        participants = self._get_race_participants(guild, session)
        locked_ids = set(session.get("locked_participants", set()))
        if len(locked_ids) == 1:
            only_id = next(iter(locked_ids))
            await self._change_user_chips(guild.id, only_id, CORRIDA_STAKE, reason="Devolução da corrida (1 jogador)")
            session["starting"] = False
            session["ended"] = True
            await self._close_lobby_message(session, guild, title="🐎 Corrida cancelada", detail="A corrida precisa de pelo menos **2 participantes**. A entrada foi devolvida.")
            self._race_sessions.pop(guild_id, None)
            return True
        if len(participants) < 2:
            for user_id in locked_ids:
                await self._change_user_chips(guild.id, user_id, CORRIDA_STAKE, reason="Devolução da corrida (cancelada)")
            session["starting"] = False
            session["ended"] = True
            await self._close_lobby_message(session, guild, title="🐎 Corrida cancelada", detail="Não restaram participantes suficientes. Todas as entradas foram devolvidas.")
            self._race_sessions.pop(guild_id, None)
            return True

        progress = session.setdefault("progress", {})
        state_map = session.setdefault("state_map", {})
        for member in participants:
            progress[member.id] = 0.0
            state_map[member.id] = _HORSE_START
            await self._record_game_played(guild.id, member.id, weekly_points=4)

        rodada_cheia_bonus = self._race_rodada_cheia_pending_bonus(session)
        if rodada_cheia_bonus > 0:
            session["bonus_pool"] = int(session.get("bonus_pool", 0) or 0) + rodada_cheia_bonus
            session["rodada_cheia"] = True

        session["starting"] = True
        session["started"] = True
        session["narration"] = ""
        session["arrival_groups"] = []
        session["active_impulses"] = {}
        session["impulse_flash_users"] = set()
        session["impulse_flash_levels"] = {}
        session["narration_hold_ticks"] = 0
        session["finish_meta"] = {}
        session["early_rank_snapshot"] = {}
        session["best_impulse"] = None
        session["stale_ticks"] = 0
        session["impulse_status"] = ""
        session["impulse_tasks"] = []
        session["impulse_ticks_fired"] = set()
        session["impulse_schedule"] = self._get_race_impulse_schedule(session)
        session["active_impulse_message"] = None
        session["active_impulse_task"] = None
        session["_visible_before_progress"] = {member.id: float(progress.get(member.id, 0.0)) for member in participants}
        session["_last_render_key"] = None
        session["_pending_render_key"] = None
        view = session.get("view")
        if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            try:
                view.stop()
            except Exception:
                pass
        session["view"] = None

        await self._refresh_race_message(guild.id)
        session["starting"] = False
        await asyncio.sleep(1.0)

        condition = session.get("condition") or {}
        special = session.get("special") or {}
        tick = 0
        track_end = float(_CORRIDA_TRACK_LENGTH - 1)

        while True:
            if session.get("ended"):
                return False
            participants = self._get_race_participants(guild, session)
            if len(participants) < 2:
                break
            arrival_groups: list[list[int]] = session.setdefault("arrival_groups", [])
            arrived_ids = {int(user_id) for group in arrival_groups for user_id in group}
            if len(arrived_ids) >= len(participants):
                break

            schedule = tuple(session.get("impulse_schedule") or ())
            impulse_awards: list[dict] = []
            if schedule:
                next_event = next(((event_tick, event_stage) for event_tick, event_stage in schedule if event_tick == tick and event_tick not in session.setdefault("impulse_ticks_fired", set())), None)
                if next_event is not None:
                    event_tick, stage_name = next_event
                    await self._stop_active_impulse_event(session, keep_status=True)
                    session.setdefault("impulse_ticks_fired", set()).add(event_tick)
                    impulse_awards = list(await self._run_race_impulse_event(guild, session, stage_name) or [])

            tick_events: list[tuple[str, discord.Member]] = []
            recent_awards = impulse_awards or list(session.get("recent_impulse_awards", []) or [])
            session["recent_impulse_awards"] = []
            for award in recent_awards:
                member = guild.get_member(int(award.get("user_id") or 0))
                tier = str(award.get("tier") or "").lower()
                if member is not None and tier:
                    tick_events.append((f"boost_{tier}", member))
            if not tick_events:
                for user_id, impulse_state in dict(session.get("active_impulses") or {}).items():
                    if int((impulse_state or {}).get("ticks_left") or 0) >= 2:
                        member = guild.get_member(int(user_id))
                        tier = str((impulse_state or {}).get("kind") or "").lower()
                        if member is not None and tier:
                            tick_events.append((f"boost_{tier}", member))
            ordered_before = self._build_finalized_order(guild, session)
            leader_before = ordered_before[0].id if ordered_before else 0
            finishers_this_tick: list[tuple[int, float]] = []

            for member in participants:
                if member.id in arrived_ids:
                    progress[member.id] = track_end
                    state_map[member.id] = _HORSE_FINISH
                    continue

                cur = float(progress.get(member.id, 0.0))
                trip_chance = 0.12 + float(condition.get("trip", 0.0)) + float(special.get("trip", 0.0))
                speed_bonus = float(condition.get("speed", 0.0)) + float(special.get("speed", 0.0))

                if session.get("final_stretch"):
                    trip_chance = max(0.02, trip_chance - 0.03)
                if tick >= _CORRIDA_UPDATES:
                    trip_chance = max(0.01, trip_chance - 0.05)

                active_impulses = session.setdefault("active_impulses", {})
                impulse_state = dict(active_impulses.get(member.id) or {})
                impulse_ticks_left = int(impulse_state.get("ticks_left") or 0)
                impulse_kind = str(impulse_state.get("kind") or "").lower()
                impulse_per_tick = float(impulse_state.get("per_tick") or 0.0) if impulse_ticks_left > 0 else 0.0
                if impulse_ticks_left > 0:
                    trip_chance = max(0.01, trip_chance - 0.06)

                if random.random() < trip_chance and cur < track_end - 0.5:
                    move = max(0.18, impulse_per_tick * 0.8)
                    state_map[member.id] = _HORSE_TRIP
                    tick_events.append(("trip", member))
                else:
                    base_move = random.uniform(1.02, 1.58)
                    if speed_bonus > 0:
                        base_move += min(0.26, speed_bonus * 0.44)
                    elif speed_bonus < 0:
                        base_move += max(-0.26, speed_bonus * 0.38)
                    if session.get("final_stretch"):
                        base_move += 0.22
                    if tick >= _CORRIDA_UPDATES:
                        base_move += 0.30
                    move = max(0.0, min(4.9, base_move + impulse_per_tick))
                    if impulse_ticks_left > 0:
                        state_map[member.id] = _HORSE_DASH if impulse_kind == "grande" else _HORSE_BOOST
                    else:
                        state_map[member.id] = _HORSE_RUN

                raw_finish_score = cur + move
                new_pos = min(track_end, raw_finish_score)
                progress[member.id] = new_pos
                if impulse_ticks_left > 0:
                    remaining_ticks = impulse_ticks_left - 1
                    if remaining_ticks > 0:
                        active_impulses[member.id] = {"kind": impulse_kind, "ticks_left": remaining_ticks, "per_tick": impulse_per_tick}
                    else:
                        active_impulses.pop(member.id, None)
                if new_pos >= track_end - 1e-9:
                    finish_score = raw_finish_score + random.random() * 1e-6
                    finishers_this_tick.append((member.id, finish_score))

            visible_before = session.get("_visible_before_progress") or {}
            visible_before = {member.id: int(float(visible_before.get(member.id, 0.0))) for member in participants}
            visible_after = {member.id: int(float(progress.get(member.id, 0.0))) for member in participants}
            stale_active_ids = [member.id for member in participants if member.id not in arrived_ids]
            if stale_active_ids and not finishers_this_tick and all(visible_before.get(user_id, -1) == visible_after.get(user_id, -2) for user_id in stale_active_ids):
                session["stale_ticks"] = int(session.get("stale_ticks", 0) or 0) + 1
                for member in participants:
                    if member.id in arrived_ids:
                        continue
                    nudged = min(track_end, float(progress.get(member.id, 0.0)) + 0.35)
                    progress[member.id] = nudged
                    if state_map.get(member.id) == _HORSE_START:
                        state_map[member.id] = _HORSE_RUN
                    if nudged >= track_end - 1e-9:
                        finish_score = nudged + random.random() * 1e-6
                        finishers_this_tick.append((member.id, finish_score))
                        state_map[member.id] = _HORSE_FINISH
            else:
                session["stale_ticks"] = 0

            if finishers_this_tick:
                finish_meta = session.setdefault("finish_meta", {})
                already_arrived = {int(user_id) for group in arrival_groups for user_id in group}
                ordered_finishers = [(user_id, score) for user_id, score in sorted(finishers_this_tick, key=lambda item: (-item[1], item[0])) if int(user_id) not in already_arrived]
                primary_finisher = ordered_finishers[:1]
                delayed_finishers = ordered_finishers[1:]
                for user_id, score in primary_finisher:
                    arrival_groups.append([int(user_id)])
                    finish_meta[int(user_id)] = {"tick": tick, "score": float(score)}
                    progress[int(user_id)] = track_end
                    state_map[int(user_id)] = _HORSE_FINISH
                for user_id, _score in delayed_finishers:
                    fallback_gap = random.uniform(0.35, 1.10)
                    progress[int(user_id)] = max(track_end - fallback_gap, track_end - 1.20)
                    impulse_state = dict((session.get("active_impulses") or {}).get(int(user_id)) or {})
                    delayed_kind = str(impulse_state.get("kind") or "").lower()
                    if int(impulse_state.get("ticks_left") or 0) > 0:
                        state_map[int(user_id)] = _HORSE_DASH if delayed_kind == "grande" else _HORSE_BOOST
                    elif state_map.get(int(user_id)) == _HORSE_TRIP:
                        pass
                    else:
                        state_map[int(user_id)] = _HORSE_RUN
                arrived_ids = {int(user_id) for group in arrival_groups for user_id in group}

            leader_progress = max((float(progress.get(member.id, 0.0)) for member in participants), default=0.0)
            session["final_stretch"] = leader_progress >= track_end * 0.72 or tick >= _CORRIDA_UPDATES - 3
            ordered_after = self._build_finalized_order(guild, session)
            leader_after = ordered_after[0].id if ordered_after else 0
            session["_visible_before_progress"] = {member.id: float(progress.get(member.id, 0.0)) for member in participants}
            if tick == 2 and not session.get("early_rank_snapshot"):
                session["early_rank_snapshot"] = {member.id: rank for rank, member in self._ordered_race_members(guild, session)}
            hold_ticks = int(session.get("narration_hold_ticks", 0) or 0)
            impulse_event_this_tick = self._has_impulse_event(tick_events)
            if len(arrived_ids) >= len(participants):
                session["narration"] = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick, final_tick=True)
                session["narration_hold_ticks"] = 0
            elif impulse_event_this_tick:
                impulse_text = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick)
                if impulse_text.strip():
                    session["narration"] = impulse_text
                    session["narration_hold_ticks"] = 3
                else:
                    session["narration_hold_ticks"] = 0
            elif finishers_this_tick:
                ordered_finishers = [user_id for user_id, _score in sorted(finishers_this_tick, key=lambda item: (-item[1], item[0]))][:1]
                finisher = guild.get_member(int(ordered_finishers[0])) if ordered_finishers else None
                session["narration"] = f"🏁 {finisher.mention} cruzou a linha." if finisher else "🏁 Um corredor cruzou a linha."
                session["narration_hold_ticks"] = 0
            elif hold_ticks > 0 and str(session.get("narration") or "").strip():
                session["narration_hold_ticks"] = hold_ticks - 1
            else:
                if leader_after and leader_after != leader_before:
                    leader = guild.get_member(leader_after)
                    if leader is not None:
                        tick_events.append(("lead", leader))
                session["narration"] = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick)
                session["narration_hold_ticks"] = 0
            self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
            await self._refresh_race_message(guild.id)
            if int(session.get("narration_hold_ticks", 0) or 0) <= 0 and not session.get("active_impulses"):
                session["impulse_flash_users"] = set()
                session["impulse_flash_levels"] = {}
            tick += 1
            self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
            await asyncio.sleep(_CORRIDA_UPDATE_SECONDS)

        await self._stop_active_impulse_event(session)

        final_groups = self._build_arrival_member_groups(guild, session)
        final_order = [member for group in final_groups for member in group]
        for member in final_order:
            progress[member.id] = _CORRIDA_TRACK_LENGTH - 1
            state_map[member.id] = _HORSE_FINISH

        session["ended"] = True
        total_pot = self._race_pot_total(session)
        bonus_pool = int(session.get("bonus_pool", 0) or 0)
        participant_count = len(set(session.get("locked_participants", set()) or []))
        prize_pot = max(0, participant_count) * CORRIDA_STAKE
        rewards, placements = self._allocate_race_rewards(final_groups, prize_pot)
        bonus_rewards, _bonus_placements = self._allocate_race_rewards(final_groups, bonus_pool) if bonus_pool > 0 else ({}, [])
        result_lines: list[str] = []
        if final_groups:
            first_group = final_groups[0]
            winner = first_group[0]
            winner_amount = int(rewards.get(winner.id, 0) or 0)
            winner_net = max(0, winner_amount - CORRIDA_STAKE)
            winner_bonus = int(bonus_rewards.get(winner.id, 0) or 0)
            winner_text = self._chip_text(winner_net, kind='gain')
            if winner_bonus > 0:
                winner_text += f" + {self._bonus_chip_amount(winner_bonus)}"
            result_lines.append(f"🏆 **Vencedor:** {winner.mention} — **ganho líquido:** {winner_text}")
        finish_meta = session.get("finish_meta") or {}
        if len(final_order) >= 2:
            leader = final_order[0]
            runner_up = final_order[1]
            leader_meta = finish_meta.get(leader.id) or {}
            runner_meta = finish_meta.get(runner_up.id) or {}
            if leader_meta and runner_meta and int(leader_meta.get("tick", -99)) == int(runner_meta.get("tick", -98)):
                diff = abs(float(leader_meta.get("score", 0.0)) - float(runner_meta.get("score", 0.0)))
                if diff <= 0.18:
                    result_lines.append(f"📸 Chegada apertadíssima entre {leader.mention} e {runner_up.mention}!")

        best_impulse = session.get("best_impulse") or {}
        best_impulse_user = guild.get_member(int(best_impulse.get("user_id") or 0)) if best_impulse else None
        if best_impulse_user is not None:
            stage = str(best_impulse.get("stage") or "impulso").lower()
            tier = str(best_impulse.get("tier") or "").lower()
            tier_text = {"pequeno": "impulso pequeno", "medio": "impulso médio", "grande": "impulso grande"}.get(tier, "impulso")
            result_lines.append(f"⚡ Melhor impulso: {best_impulse_user.mention} ({tier_text}, {stage}).")

        early_rank_snapshot = session.get("early_rank_snapshot") or {}
        final_rank_map = _shared_rank_map([[member.id for member in group] for group in final_groups])
        recovery_candidates: list[tuple[int, discord.Member]] = []
        for member in final_order:
            start_rank = int(early_rank_snapshot.get(member.id, 999))
            end_rank = int(final_rank_map.get(member.id, 999))
            gain = start_rank - end_rank
            if gain >= 2:
                recovery_candidates.append((gain, member))
        if recovery_candidates:
            recovery_candidates.sort(key=lambda item: (-item[0], item[1].display_name.casefold()))
            recovery_member = recovery_candidates[0][1]
            result_lines.append(f"🚀 Recuperação da corrida: {recovery_member.mention}.")
        session["narration"] = "🏁 Todos cruzaram a linha."
        session["impulse_status"] = ""

        rank_map = _shared_rank_map([[member.id for member in group] for group in final_groups])
        for member in final_order:
            rank = rank_map.get(member.id, 9999)
            if rank <= 3:
                await self.db.add_user_game_stat(guild.id, member.id, "corrida_podiums", 1)
                await self._grant_weekly_points(guild.id, member.id, max(3, 5 - rank))
        if final_groups:
            for first_place_member in final_groups[0]:
                await self.db.add_user_game_stat(guild.id, first_place_member.id, "corrida_wins", 1)
        losing_ids = set(locked_ids)
        if final_groups:
            for winner in final_groups[0]:
                losing_ids.discard(winner.id)
        coringa_refunds: list[tuple[int, int]] = []
        for user_id in losing_ids:
            await self.db.add_user_game_stat(guild.id, int(user_id), "corrida_losses", 1)
            refund = await self._maybe_apply_coringa_lobby_refund(guild.id, int(user_id), CORRIDA_STAKE)
            if refund > 0:
                coringa_refunds.append((int(user_id), int(refund)))
        winner_ids = {member.id for member in (final_groups[0] if final_groups else [])}
        runner_up_ids = {member.id for member in (final_groups[1] if len(final_groups) > 1 else [])}
        third_ids = {member.id for member in (final_groups[2] if len(final_groups) > 2 else [])}

        def _race_reward_reason(uid: int) -> str:
            if uid in winner_ids:
                return "Prêmio da corrida (1º lugar)"
            if uid in runner_up_ids:
                return "Prêmio da corrida (2º lugar)"
            if uid in third_ids:
                return "Prêmio da corrida (3º lugar)"
            return "Prêmio da corrida"

        for user_id, amount in rewards.items():
            if amount > 0:
                await self._change_user_chips(guild.id, user_id, amount, reason=_race_reward_reason(int(user_id)))
                await self._grant_weekly_points(guild.id, user_id, max(4, amount // 4))
        for user_id, amount in bonus_rewards.items():
            if amount > 0:
                bonus_reason = "Bônus da corrida (rodada cheia)" if session.get("rodada_cheia") else "Bônus da corrida"
                await self._change_user_bonus_chips(guild.id, user_id, amount, reason=bonus_reason)

        entry_spend_map = session.get("entry_spend") or {}
        participant_ids = [member.id for member in final_order]
        owner_id = int(session.get("owner_id") or 0)
        public_race_notices: list[str] = []
        for member in final_order:
            notes = await self._apply_new_race_result(
                guild.id,
                member.id,
                won=member.id in winner_ids,
                entry_spend=entry_spend_map.get(member.id) or {"chips": CORRIDA_STAKE, "bonus": 0},
                payout=int(rewards.get(member.id, 0) or 0) + int(bonus_rewards.get(member.id, 0) or 0),
                opponent_ids=[user_id for user_id in participant_ids if user_id != member.id],
                valid=True,
                allow_hunt=True,
            )
            await self._route_lobby_race_notices(
                (session.get("race_interactions") or {}).get(member.id),
                guild.id,
                member.id,
                owner_id,
                notes,
                public_race_notices,
            )

        for user_id, refund in coringa_refunds:
            refund_note = self._race_effect_message(
                guild.id,
                user_id,
                'as',
                f"recuperou {self._chip_text(refund, kind='gain')} da entrada.",
            )
            await self._route_lobby_race_notices(
                (session.get("race_interactions") or {}).get(user_id),
                guild.id,
                user_id,
                owner_id,
                [refund_note],
                public_race_notices,
            )
        public_race_notices = self._clean_race_notices(public_race_notices)

        first_game_user_ids = await self._unlock_first_game_for_users(
            guild.id,
            [member.id for member in final_order],
        )
        session["starting"] = False
        result_limit = max(0, 20 - len(public_race_notices))
        session["result_lines"] = result_lines[:result_limit] + public_race_notices[:20]
        message = session.get("message")
        result_delivered = False
        if message is not None:
            final_view = _RaceStateView(self, guild, session, finished=True)
            session["view"] = final_view
            edit_state = await self._safe_edit_message_view(message, final_view)
            result_delivered = edit_state in {"ok", "skipped"}
            if edit_state == "missing":
                session["message"] = None
        if public_race_notices and not result_delivered:
            self._queue_private_race_notices(guild.id, owner_id, public_race_notices)
        achievement_channel = (
            getattr(message, "channel", None)
            or guild.get_channel(int(session.get("text_channel_id", 0) or 0))
        )
        await self._send_first_game_notices(achievement_channel, guild.id, first_game_user_ids)

        self._race_sessions.pop(guild_id, None)
        return True

    async def _handle_corrida_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if not self._matches_exact_trigger(message.content or "", "corrida"):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        if self._get_race_session(guild.id) is not None:
            return True

        voice_channel = getattr(getattr(message.author, "voice", None), "channel", None)

        needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, CORRIDA_STAKE)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, CORRIDA_STAKE, title="🐎 Confirmar entrada")
            if not confirmed:
                return True

        entry_spend = self._entry_spend_parts(guild.id, message.author.id, CORRIDA_STAKE)
        paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, CORRIDA_STAKE, reason="Entrada na corrida")
        if needs_negative_confirm:
            chip_note = None
        if not paid:
            try:
                await message.channel.send(embed=self._make_embed("🐎 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
            except Exception:
                pass
            return True

        condition = random.choices(_RACE_CONDITIONS, weights=_RACE_CONDITION_WEIGHTS, k=1)[0]
        special = random.choice(_RACE_SPECIALS) if random.random() < 0.18 else None
        bonus_pool = int((special or {}).get("bonus_pool", 0) or 0)

        session = {
            "voice_channel_id": getattr(voice_channel, "id", 0),
            "text_channel_id": message.channel.id,
            "owner_id": message.author.id,
            "locked_participants": {message.author.id},
            "entry_spend": {message.author.id: entry_spend},
            "progress": {message.author.id: 0.0},
            "state_map": {message.author.id: _HORSE_START},
            "arrival_groups": [],
            "active_impulses": {},
            "impulse_flash_users": set(),
            "impulse_flash_levels": {},
            "narration_hold_ticks": 0,
            "finish_meta": {},
            "early_rank_snapshot": {},
            "best_impulse": None,
            "stale_ticks": 0,
            "message": None,
            "view": None,
            "ended": False,
            "started": False,
            "final_stretch": False,
            "narration": "📣 A corrida vai começar.",
            "condition": dict(condition),
            "special": dict(special) if special else None,
            "bonus_pool": bonus_pool,
            "starting": False,
            "impulse_status": "",
            "impulse_tasks": [],
            "impulse_ticks_fired": set(),
            "active_impulse_message": None,
            "active_impulse_task": None,
            "_visible_before_progress": {message.author.id: 0.0},
            "_edit_lock": asyncio.Lock(),
            "_last_render_key": None,
            "_pending_render_key": None,
            "_aux_tasks": set(),
        }
        self._touch_runtime_state(session, kind='corrida', guild_id=guild.id)
        self._race_sessions[guild.id] = session
        view = _RaceLobbyView(self, guild.id, session, guild, timeout=_CORRIDA_LOBBY_SECONDS)
        session["view"] = view
        try:
            panel_message = await message.channel.send(view=view)
        except Exception:
            self._race_sessions.pop(guild.id, None)
            await self._change_user_chips(guild.id, message.author.id, CORRIDA_STAKE, reason="Devolução da corrida (erro)")
            return True

        session["message"] = panel_message
        await self._react_with_emoji(message, "🐎", keep=True)
        return True
