import logging

import discord
from discord import app_commands

import config


log = logging.getLogger(__name__)


def _safe_display_text(value: object, *, fallback: str) -> str:
    raw = str(value or fallback).strip() or fallback
    return discord.utils.escape_mentions(discord.utils.escape_markdown(raw, as_needed=True))


def _label(text: str, component: discord.ui.Item, description: str | None = None) -> discord.ui.Label:
    return discord.ui.Label(
        text=str(text)[:45],
        description=(str(description)[:100] if description else None),
        component=component,
    )


class _EconomyStaffModal(discord.ui.Modal, title="Cargo da staff"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._get_staff_role(guild)
        role_kwargs = {
            "custom_id": "games_economy_staff_role",
            "placeholder": "Selecione o cargo da staff",
            "min_values": 0,
            "max_values": 1,
            "required": False,
        }
        if current is not None:
            role_kwargs["default_values"] = [current]
        self.role_select = discord.ui.RoleSelect(**role_kwargs)
        self.clear_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_staff_clear",
            default=False,
        )

        self.add_item(_label("Cargo", self.role_select, "Pode usar os controles administrativos"))
        self.add_item(
            _label(
                "Remover cargo atual",
                self.clear_checkbox,
                "Sem cargo definido, o acesso fica restrito",
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente"], ok=False),
                ephemeral=True,
            )
            return

        selected = list(self.role_select.values or [])
        clear = bool(self.clear_checkbox.value)
        if clear:
            await self.cog.db.set_gincana_staff_role_id(guild.id, 0)
            notice = "Cargo da staff removido"
        else:
            selected_role = selected[0] if selected else None
            role_id = int(getattr(selected_role, "id", 0) or 0)
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice(
                        "Cargo não selecionado",
                        ["Selecione um cargo ou marque a opção de remover"],
                        ok=False,
                    ),
                    ephemeral=True,
                )
                return
            if role.is_default():
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice(
                        "Cargo inválido",
                        ["O cargo padrão do servidor não pode ser usado como staff"],
                        ok=False,
                    ),
                    ephemeral=True,
                )
                return
            await self.cog.db.set_gincana_staff_role_id(guild.id, role.id)
            notice = "Cargo da staff atualizado"

        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyChannelModal(discord.ui.Modal, title="Canal dos jogos"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._get_gincana_channel(guild)
        channel_kwargs = {
            "custom_id": "games_economy_channel",
            "placeholder": "Selecione o canal exclusivo",
            "channel_types": [
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
                discord.ChannelType.stage_voice,
            ],
            "min_values": 0,
            "max_values": 1,
            "required": False,
        }
        if current is not None:
            channel_kwargs["default_values"] = [current]
        self.channel_select = discord.ui.ChannelSelect(**channel_kwargs)
        self.clear_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_channel_clear",
            default=False,
        )

        self.add_item(_label("Canal", self.channel_select, "Comandos e triggers começam somente aqui"))
        self.add_item(_label("Usar todos os canais", self.clear_checkbox, "Remove o canal exclusivo"))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente"], ok=False),
                ephemeral=True,
            )
            return

        selected = list(self.channel_select.values or [])
        clear = bool(self.clear_checkbox.value)
        if clear:
            await self.cog.db.set_gincana_channel_id(guild.id, 0)
            notice = "Todos os canais foram liberados"
        else:
            channel = selected[0] if selected else None
            channel_id = int(getattr(channel, "id", 0) or 0)
            if channel_id <= 0:
                await interaction.response.send_message(
                    view=self.cog._make_v2_notice(
                        "Canal não selecionado",
                        ["Selecione um canal ou marque a opção de liberar todos"],
                        ok=False,
                    ),
                    ephemeral=True,
                )
                return
            await self.cog.db.set_gincana_channel_id(guild.id, channel_id)
            notice = "Canal dos jogos atualizado"

        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyModeModal(discord.ui.Modal, title="Forma de uso"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        current = cog._gincana_input_mode(guild.id)
        prefix = cog._economy_prefix()
        self.mode_group = discord.ui.RadioGroup(
            custom_id="games_economy_input_mode",
            required=True,
        )
        self.mode_group.add_option(
            label="Triggers",
            value="triggers",
            description=f"Use roleta/daily ou {prefix}roleta/{prefix}daily",
            default=current == "triggers",
        )
        self.mode_group.add_option(
            label="Comandos",
            value="commands",
            description=f"Use somente comandos como {prefix}roleta e {prefix}daily",
            default=current == "commands",
        )
        self.add_item(_label("Forma de uso", self.mode_group, "Comandos permanecem disponíveis nos dois modos"))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente"], ok=False),
                ephemeral=True,
            )
            return

        mode = str(self.mode_group.value or "triggers").strip().casefold()
        if mode not in {"triggers", "commands"}:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Modo inválido", ["Selecione triggers ou comandos"], ok=False),
                ephemeral=True,
            )
            return
        await self.cog.db.set_gincana_input_mode(guild.id, mode)
        notice = (
            "Agora o servidor usa apenas comandos"
            if mode == "commands"
            else "Triggers ativados; comandos continuam disponíveis"
        )
        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice=notice)
        )


class _EconomyResetModal(discord.ui.Modal, title="Restaurar configurações"):
    def __init__(self, cog: "GincanaCommandMixin", *, guild_id: int, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.opener_id = int(opener_id)
        self.confirm_checkbox = discord.ui.Checkbox(
            custom_id="games_economy_reset_confirm",
            default=False,
        )
        self.add_item(
            _label(
                "Confirmar restauração",
                self.confirm_checkbox,
                "Remove cargo e canal exclusivos e volta para triggers",
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente"], ok=False),
                ephemeral=True,
            )
            return
        if not bool(self.confirm_checkbox.value):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "Nada alterado",
                    ["Marque a confirmação para restaurar as configurações"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await self.cog.db.set_gincana_staff_role_id(guild.id, 0)
        await self.cog.db.set_gincana_channel_id(guild.id, 0)
        await self.cog.db.set_gincana_input_mode(guild.id, "triggers")
        await interaction.response.edit_message(
            view=self.cog._make_economy_panel_view(guild, self.opener_id, notice="Configurações restauradas")
        )


class _EconomyPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCommandMixin", *, guild: discord.Guild, opener_id: int, notice: str | None = None):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = int(guild.id)
        self.opener_id = int(opener_id)

        staff_role = cog._get_staff_role(guild)
        channel = cog._get_gincana_channel(guild)
        mode = cog._gincana_input_mode(guild.id)
        prefix = cog._economy_prefix()

        staff_button = discord.ui.Button(
            label="Escolher cargo",
            emoji="🛡️",
            style=discord.ButtonStyle.secondary,
        )
        staff_button.callback = self._edit_staff
        channel_button = discord.ui.Button(
            label="Escolher canal",
            emoji="📍",
            style=discord.ButtonStyle.secondary,
        )
        channel_button.callback = self._edit_channel
        mode_button = discord.ui.Button(
            label="Mudar modo",
            emoji="🔁",
            style=discord.ButtonStyle.secondary,
        )
        mode_button.callback = self._edit_mode
        reset_button = discord.ui.Button(
            label="Restaurar configurações",
            emoji="↩️",
            style=discord.ButtonStyle.danger,
        )
        reset_button.callback = self._reset

        adjust_button = discord.ui.Button(
            label="Ajustar usuário",
            emoji="💰",
            style=discord.ButtonStyle.secondary,
        )
        adjust_button.callback = self._adjust_user
        race_button = discord.ui.Button(
            label="Gerenciar habilidades",
            emoji="🧬",
            style=discord.ButtonStyle.secondary,
        )
        race_button.callback = self._manage_race
        reset_user_button = discord.ui.Button(
            label="Resetar usuário",
            emoji="♻️",
            style=discord.ButtonStyle.secondary,
        )
        reset_user_button.callback = self._reset_user
        reset_server_button = discord.ui.Button(
            label="Resetar servidor",
            emoji="🧨",
            style=discord.ButtonStyle.danger,
        )
        reset_server_button.callback = self._reset_server

        staff_text = (
            _safe_display_text(getattr(staff_role, "name", None), fallback="Cargo")
            if staff_role is not None
            else "Não definido"
        )
        channel_text = (
            f"#{_safe_display_text(getattr(channel, 'name', None), fallback='canal')}"
            if channel is not None
            else "Todos os canais"
        )
        mode_text = "Somente comandos" if mode == "commands" else "Triggers + comandos"
        if mode == "commands":
            mode_example = f"Use `{prefix}roleta`, `{prefix}daily` e `{prefix}race`"
        else:
            mode_example = (
                f"Use `roleta`, `daily` e `race`, ou `{prefix}roleta`, `{prefix}daily` e `{prefix}race`"
            )

        config_items: list[discord.ui.Item] = [
            discord.ui.TextDisplay("# ⚙️ Economia\nGerencie jogos, fichas e habilidades neste servidor"),
        ]
        if notice:
            config_items.append(discord.ui.TextDisplay(f"{cog._EFFECT_EMOJI} {notice}"))
        config_items.extend(
            [
                discord.ui.Separator(),
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"## Cargo da staff\n{staff_text}\nPode usar os controles administrativos"
                    ),
                    accessory=staff_button,
                ),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"## Canal dos jogos\n{channel_text}\nComandos e triggers começam neste canal"
                    ),
                    accessory=channel_button,
                ),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.Section(
                    discord.ui.TextDisplay(f"## Forma de uso\n**{mode_text}**\n{mode_example}"),
                    accessory=mode_button,
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(reset_button),
            ]
        )
        self.add_item(discord.ui.Container(*config_items, accent_color=discord.Color.blurple()))

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Fichas e habilidades\nAjuste saldos, habilidades e resets de usuários"
                ),
                discord.ui.ActionRow(adjust_button, race_button),
                discord.ui.ActionRow(reset_user_button, reset_server_button),
                accent_color=discord.Color.orange(),
            )
        )

    async def _validate(self, interaction: discord.Interaction) -> discord.Guild | None:
        if not await self.cog._economy_validate_interaction(interaction, opener_id=self.opener_id):
            return None
        guild = interaction.guild
        if guild is None or int(guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Abra o painel novamente"], ok=False),
                ephemeral=True,
            )
            return None
        return guild

    async def _edit_staff(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(
                _EconomyStaffModal(self.cog, guild=guild, opener_id=self.opener_id)
            )

    async def _edit_channel(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(
                _EconomyChannelModal(self.cog, guild=guild, opener_id=self.opener_id)
            )

    async def _edit_mode(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(
                _EconomyModeModal(self.cog, guild=guild, opener_id=self.opener_id)
            )

    async def _reset(self, interaction: discord.Interaction):
        guild = await self._validate(interaction)
        if guild is not None:
            await interaction.response.send_modal(
                _EconomyResetModal(self.cog, guild_id=guild.id, opener_id=self.opener_id)
            )

    async def _adjust_user(self, interaction: discord.Interaction):
        if await self._validate(interaction) is not None:
            await interaction.response.send_modal(self.cog._make_chip_adjust_modal(self.opener_id))

    async def _manage_race(self, interaction: discord.Interaction):
        if await self._validate(interaction) is not None:
            await interaction.response.send_modal(self.cog._make_chip_race_modal(self.opener_id))

    async def _reset_user(self, interaction: discord.Interaction):
        if await self._validate(interaction) is not None:
            await interaction.response.send_modal(self.cog._make_chip_user_reset_modal(self.opener_id))

    async def _reset_server(self, interaction: discord.Interaction):
        if await self._validate(interaction) is not None:
            await interaction.response.send_modal(self.cog._make_chip_server_reset_modal(self.opener_id))


class GincanaCommandMixin:
    def _economy_prefix(self) -> str:
        for name in ("BOT_PREFIX", "PREFIX"):
            value = str(getattr(config, name, "") or "").strip()
            if value:
                return value
        return "_"

    async def _economy_is_bot_owner(self, user: discord.abc.User) -> bool:
        user_id = int(getattr(user, "id", 0) or 0)
        owner_id = int(getattr(self.bot, "owner_id", 0) or 0)
        owner_ids = {int(value) for value in (getattr(self.bot, "owner_ids", None) or set())}
        if user_id and (user_id == owner_id or user_id in owner_ids):
            return True
        try:
            return bool(await self.bot.is_owner(user))
        except Exception:
            return False

    async def _economy_user_allowed(self, guild: discord.Guild, user: discord.abc.User) -> bool:
        if await self._economy_is_bot_owner(user):
            return True
        if not isinstance(user, discord.Member):
            return False
        staff_role = self._get_staff_role(guild)
        return staff_role is not None and staff_role in getattr(user, "roles", [])

    async def _economy_validate_interaction(self, interaction: discord.Interaction, *, opener_id: int) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != int(opener_id):
            await interaction.response.send_message(
                view=self._make_v2_notice("Painel reservado", ["Abra seu próprio painel de economia"], ok=False),
                ephemeral=True,
            )
            return False
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor"], ok=False),
                ephemeral=True,
            )
            return False
        if not await self._economy_user_allowed(guild, interaction.user):
            await interaction.response.send_message(
                view=self._make_v2_notice(
                    "Sem permissão",
                    ["Você não pode alterar a economia deste servidor"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def _normalize_economy_config(self, guild: discord.Guild):
        role_id = int(self.db.get_gincana_staff_role_id(guild.id) or 0)
        if role_id and guild.get_role(role_id) is None:
            await self.db.set_gincana_staff_role_id(guild.id, 0)

        channel_id = self._gincana_channel_id(guild.id)
        if channel_id and self._get_gincana_channel(guild) is None:
            await self.db.set_gincana_channel_id(guild.id, 0)

        raw_mode = self._gincana_input_mode(guild.id)
        if raw_mode not in {"triggers", "commands"}:
            await self.db.set_gincana_input_mode(guild.id, "triggers")

    def _make_economy_panel_view(
        self,
        guild: discord.Guild,
        opener_id: int,
        *,
        notice: str | None = None,
    ) -> discord.ui.LayoutView:
        return _EconomyPanelView(self, guild=guild, opener_id=opener_id, notice=notice)

    async def _run_gincana_command(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=self._make_v2_notice(
                    "Servidor inválido",
                    ["Use o comando dentro de um servidor"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if not await self._economy_user_allowed(interaction.guild, interaction.user):
            await interaction.response.send_message(
                view=self._make_v2_notice(
                    "Sem permissão",
                    ["Você não pode alterar a economia deste servidor"],
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await self._normalize_economy_config(interaction.guild)
        await interaction.response.send_message(
            view=self._make_economy_panel_view(interaction.guild, interaction.user.id),
            ephemeral=True,
        )

    async def _run_gincana_prefix_command(self, ctx):
        guild = ctx.guild
        if guild is None:
            await ctx.reply(
                view=self._make_v2_notice(
                    "Servidor inválido",
                    ["Use o comando dentro de um servidor"],
                    ok=False,
                ),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._economy_user_allowed(guild, ctx.author):
            await ctx.reply(
                view=self._make_v2_notice(
                    "Sem permissão",
                    ["Você não pode alterar a economia deste servidor"],
                    ok=False,
                ),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._normalize_economy_config(guild)
        await ctx.reply(
            view=self._make_economy_panel_view(guild, ctx.author.id),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_gincana_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.error("games: erro no /economia: %r", error)
        view = self._make_v2_notice("Erro na economia", ["Não foi possível abrir o painel"], ok=False)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=view, ephemeral=True)
            else:
                await interaction.response.send_message(view=view, ephemeral=True)
        except Exception:
            pass
