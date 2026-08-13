import inspect
import contextlib
import asyncio
import time
import re
import weakref
import unicodedata
import traceback
import json
import urllib.request
from urllib.parse import urlparse
from typing import Optional, Callable

import discord
from discord import app_commands
from discord.ext import commands

import config
from .common import _shorten, validate_mode
from .prefix import validate_prefix_values
from .utils.embed import build_settings_panel_text_from_embed, human_voice_name, human_language_name, human_rate, human_pitch

TTS_PANEL_EXPIRE_AFTER_SECONDS = 180.0
TTS_PANEL_DISPATCH_TIMEOUT_SECONDS = 86400.0

class _BaseTTSView(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        requested_timeout = max(1.0, float(timeout or TTS_PANEL_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, TTS_PANEL_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + requested_timeout

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                message = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                message = (
                    "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
                    "Para continuar, abra o comando novamente e gere um painel novo."
                )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[tts_panel_error] falha ao responder erro: {e!r}")

    async def on_timeout(self) -> None:
        pass


class _SimpleSelectView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        title: str,
        description: str,
        select: discord.ui.Select,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.title = title
        self.description = description
        self.source_panel_message: discord.Message | None = source_panel_message
        try:
            select.guild_id = guild_id
            select.owner_id = owner_id
            select.target_user_id = target_user_id
            select.target_user_name = target_user_name
        except Exception:
            pass
        self.add_item(select)

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(self.title, self.description, ok=True)
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg


class ModeSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="ATTS", description="Android TTS nativo do worker", value="android_native", emoji="📱"),
            discord.SelectOption(label="gtts", description="Mais simples e compatível", value="gtts", emoji="🗣️"),
            discord.SelectOption(label="edge", description="Voz natural com voice, speed e pitch", value="edge", emoji="✨"),
        ]
        super().__init__(
            placeholder="Escolha o modo de TTS",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_mode_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class LanguageSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = []
        for code, name in list(sorted(cog.gtts_languages.items()))[:25]:
            options.append(
                discord.SelectOption(
                    label=_shorten(f"{code} — {name}"),
                    description="Idioma do modo gtts",
                    value=code,
                )
            )
        super().__init__(
            placeholder="Escolha um idioma do gtts",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_language_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class SpeedSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-100%", description="Extremamente devagar", value="-100%"),
            discord.SelectOption(label="-75%", description="Muito mais devagar", value="-75%"),
            discord.SelectOption(label="-50%", description="Bem mais devagar", value="-50%"),
            discord.SelectOption(label="-25%", description="Mais devagar", value="-25%"),
            discord.SelectOption(label="+0%", description="Velocidade normal", value="+0%"),
            discord.SelectOption(label="+25%", description="Mais rápido", value="+25%"),
            discord.SelectOption(label="+50%", description="Bem mais rápido", value="+50%"),
            discord.SelectOption(label="+75%", description="Muito mais rápido", value="+75%"),
            discord.SelectOption(label="+100%", description="Extremamente rápido", value="+100%"),
        ]
        super().__init__(placeholder="Escolha uma velocidade", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_speed_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class PitchSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-100Hz", description="Extremamente grave", value="-100Hz"),
            discord.SelectOption(label="-75Hz", description="Muito grave", value="-75Hz"),
            discord.SelectOption(label="-50Hz", description="Mais grave", value="-50Hz"),
            discord.SelectOption(label="-25Hz", description="Levemente grave", value="-25Hz"),
            discord.SelectOption(label="+0Hz", description="Tom normal", value="+0Hz"),
            discord.SelectOption(label="+25Hz", description="Levemente agudo", value="+25Hz"),
            discord.SelectOption(label="+50Hz", description="Mais agudo", value="+50Hz"),
            discord.SelectOption(label="+75Hz", description="Muito agudo", value="+75Hz"),
            discord.SelectOption(label="+100Hz", description="Extremamente agudo", value="+100Hz"),
        ]
        super().__init__(placeholder="Escolha um tom", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_pitch_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))






class VoiceRegionSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        regions = sorted({voice.rsplit("-", 1)[0] for voice in (cog.edge_voice_cache or []) if voice.lower().startswith("pt-")})
        if not regions:
            regions = ["pt-BR"]
        options = [
            discord.SelectOption(
                label=_shorten(region),
                description="Abre a lista de vozes dessa região",
                value=region,
            )
            for region in regions[:25]
        ]
        super().__init__(placeholder="Escolha a região da voz", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        voices = [v for v in (self.cog.edge_voice_cache or []) if v.startswith(region + "-")]
        if not voices:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Nenhuma voz encontrada", "Não encontrei vozes para essa região.", ok=False),
                ephemeral=True,
            )
            return
        view = _SimpleSelectView(
            self.cog,
            interaction.user.id,
            self.guild_id if hasattr(self, "guild_id") else interaction.guild.id,
            "Escolha a voz",
            f"Região selecionada: `{region}`",
            VoiceSelect(self.cog, server=self.server, voices=voices),
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )
        try:
            view.source_panel_message = getattr(self.view, "source_panel_message", None)
        except Exception:
            pass
        await interaction.response.send_message(
            embed=self.cog._make_embed("Escolha a voz", f"Região selecionada: `{region}`", ok=True),
            view=view,
            ephemeral=True,
        )


class VoiceSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool, voices: list[str]):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(
                label=_shorten(voice),
                description="Voz do modo edge",
                value=voice,
            )
            for voice in voices[:25]
        ]
        super().__init__(placeholder="Escolha uma voz do edge", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_voice_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class ToggleSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", toggle_name: str):
        self.cog = cog
        self.toggle_name = toggle_name
        desc = "Ativar" if toggle_name == "only_target_user" else "Ativar"
        options = [
            discord.SelectOption(label="Ativar", description="Liga essa função", value="true", emoji="✅"),
            discord.SelectOption(label="Desativar", description="Desliga essa função", value="false", emoji="⛔"),
        ]
        super().__init__(placeholder="Escolha se quer ativar ou desativar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        enabled = self.values[0] == "true"
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        if self.toggle_name == "announce_author":
            await self.cog._apply_announce_author_from_panel(interaction, enabled, source_panel_message=source_panel_message)
        else:
            await self.cog._apply_auto_leave_from_panel(interaction, enabled, source_panel_message=source_panel_message)




class LanguageCodeModal(discord.ui.Modal, title="Selecionar idioma"):
    language_code = discord.ui.TextInput(
        label="Digite um dos códigos",
        placeholder="pt-br, en, es, fr, ja",
        required=True,
        min_length=2,
        max_length=10,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = server
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_language_from_panel(
            interaction,
            str(self.language_code).strip(),
            server=self.server,
            source_panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class LanguageHelpView(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool = False,
        source_panel_message: discord.Message | None = None,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.server = server
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        target_owner = interaction.user.id if self.owner_id == 0 else self.owner_id
        if interaction.user.id != target_owner:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Esse painel pertence a outro usuário.", ok=False),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Ver lista de idiomas", style=discord.ButtonStyle.secondary, emoji="📚", row=0)
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = sorted(self.cog.gtts_languages.items())
        if not items:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idiomas disponíveis", "Nenhum idioma encontrado.", ok=False),
                ephemeral=True,
            )
            return

        rows = []
        for i in range(0, len(items), 2):
            left_code, left_name = items[i]
            left = f"`{left_code}` — {left_name}"
            if i + 1 < len(items):
                right_code, right_name = items[i + 1]
                right = f"`{right_code}` — {right_name}"
                rows.append(f"{left}  |  {right}")
            else:
                rows.append(left)

        description = "\n".join(rows)
        embed = discord.Embed(
            title="Idiomas disponíveis",
            description=description,
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Selecionar idioma", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def select_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            LanguageCodeModal(
                self.cog,
                self.source_panel_message,
                server=self.server,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            )
        )


class BotPrefixModal(discord.ui.Modal, title="Alterar prefixo do bot"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do bot",
        placeholder="Ex.: _",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="bot",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class ATTSPrefixModal(discord.ui.Modal, title="Alterar prefixo do ATTS"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do ATTS",
        placeholder="Ex.: %",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="atts",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class TetoPrefixModal(discord.ui.Modal, title="Alterar prefixo da Kasane Teto"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo da Kasane Teto",
        placeholder="Ex.: '",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="teto",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class GTTSPrefixModal(discord.ui.Modal, title="Alterar prefixo do modo gTTS"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo gTTS",
        placeholder="Ex.: .",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="gtts",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class EdgePrefixModal(discord.ui.Modal, title="Alterar prefixo do modo Edge"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo Edge",
        placeholder="Ex.: ,",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="edge",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )





class IgnoredRoleSelect(discord.ui.RoleSelect):
    def __init__(self, cog: "TTSVoice"):
        self.cog = cog
        super().__init__(
            placeholder="Selecione um cargo para ignorar no TTS",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        selected_role = self.values[0] if getattr(self, "values", None) else None
        if not isinstance(selected_role, discord.Role):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Cargo inválido", "Não consegui identificar o cargo selecionado.", ok=False),
                ephemeral=True,
            )
            return
        await self.cog._apply_ignored_tts_role_from_panel(
            interaction,
            selected_role,
            source_panel_message=source_panel_message,
        )


class IgnoreRoleConfigView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "server"
        self.source_panel_message = source_panel_message
        self.add_item(IgnoredRoleSelect(cog))

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(
            "Cargo ignorado no TTS",
            "Selecione um cargo para ativar a regra. Desativar mantém o cargo salvo.",
            ok=True,
        )
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg

    @discord.ui.button(label="Desativar", style=discord.ButtonStyle.danger, emoji="⛔", row=1)
    async def remove_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._remove_ignored_tts_role_from_panel(
            interaction,
            source_panel_message=self.source_panel_message,
        )


class SpokenNameModal(discord.ui.Modal, title="Alterar apelido falado"):
    spoken_name = discord.ui.TextInput(
        label="Apelido falado",
        placeholder="Digite um apelido pronunciável ou deixe vazio para limpar",
        required=False,
        max_length=32,
    )

    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        current_value: str | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        if current_value is not None:
            self.spoken_name.default = str(current_value or "")[:32]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_spoken_name_from_modal(
            interaction,
            str(self.spoken_name),
            panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )



def _current_tts_value(cog: "TTSVoice", guild_id: int, user_id: int, key: str, default: str = "", *, server: bool = False) -> str:
    db = cog._get_db()
    try:
        if server and db is not None and hasattr(db, "get_guild_tts_defaults"):
            data = db.get_guild_tts_defaults(guild_id) or {}
            return str((data or {}).get(key) or default or "")
        if db is not None and hasattr(db, "resolve_tts"):
            data = db.resolve_tts(guild_id, user_id) or {}
            return str((data or {}).get(key) or default or "")
    except Exception:
        pass
    return str(default or "")


def _select_values(item) -> list[str]:
    try:
        values = []
        for value in (getattr(item, "values", None) or []):
            if value is None:
                continue
            if hasattr(value, "id") and not isinstance(value, str):
                value = str(getattr(value, "id", "") or "")
            value = str(value or "").strip()
            if value:
                values.append(value)
        return values
    except Exception:
        return []


def _selected_roles(item) -> list[discord.Role]:
    roles: list[discord.Role] = []
    try:
        for value in (getattr(item, "values", None) or []):
            if isinstance(value, discord.Role):
                roles.append(value)
    except Exception:
        pass
    return roles


def _first_selected_role(item) -> discord.Role | None:
    roles = _selected_roles(item)
    return roles[0] if roles else None


def _item_value(item, default: str = "") -> str:
    try:
        value = getattr(item, "value", None)
        if value is None:
            return str(default or "")
        return str(value or "").strip()
    except Exception:
        return str(default or "")


def _experimental_modal_components_enabled() -> bool:
    # Selects/radio/checkbox dentro de modal ainda variam bastante entre
    # versões da lib/cliente. Mantemos desligado por padrão para não quebrar
    # a interação do painel; o painel continua usando selects na mensagem e
    # modais seguros com TextInput.
    return bool(getattr(config, "TTS_EXPERIMENTAL_MODAL_COMPONENTS", False))


def _maybe_add_radio_group(modal, attr_name: str, *, label: str, options: list[tuple[str, str, str]], default_value: str) -> bool:
    if not _experimental_modal_components_enabled():
        return False
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=True, options=[])
        for opt_label, value, description in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=(str(value) == str(default_value)),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return False


def _maybe_add_checkbox_group(modal, attr_name: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None) -> bool:
    if not _experimental_modal_components_enabled():
        return False
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for opt_label, value, description, selected in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=bool(selected),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        return False


def _make_optional_select(*, placeholder: str, options: list[discord.SelectOption]):
    kwargs = dict(placeholder=placeholder, min_values=0, max_values=1, options=options[:25])
    try:
        return discord.ui.Select(required=False, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def _single_component_value(item, default: str = "") -> str:
    values = _select_values(item)
    if values:
        return str(values[0] or "").strip()
    return _item_value(item, default)


def _with_default_option(options: list[discord.SelectOption], current: str) -> list[discord.SelectOption]:
    current = str(current or "").strip()
    seen: set[str] = set()
    fixed: list[discord.SelectOption] = []
    matched_current = False

    for option in options or []:
        value = str(getattr(option, "value", "") or "").strip()
        if not value or value in seen:
            continue
        try:
            option.default = bool(current and value == current)
            matched_current = matched_current or bool(option.default)
        except Exception:
            pass
        fixed.append(option)
        seen.add(value)
        if len(fixed) >= 25:
            break

    if current and not matched_current:
        fixed.insert(0, discord.SelectOption(label=_shorten(current, 100), description="Valor atual", value=current, default=True))

    return fixed[:25]


def _modal_label_available() -> bool:
    return bool(hasattr(discord.ui, "Label"))


def _make_modal_text_input(*, label: str | None, placeholder: str, current: str = "", max_length: int = 80, required: bool = False):
    kwargs = {
        "placeholder": placeholder,
        "required": required,
        "max_length": max_length,
    }
    # Components V2 proíbe label duplicado: quando o TextInput é filho de um
    # discord.ui.Label, somente o contêiner pode fornecer o texto do rótulo.
    if label is not None:
        kwargs["label"] = label
    item = discord.ui.TextInput(**kwargs)
    try:
        item.default = str(current or "")[:max_length]
    except Exception:
        try:
            item.value = str(current or "")[:max_length]
        except Exception:
            pass
    return item


def _add_modal_text_input(modal, attr_name: str, *, label: str, placeholder: str, current: str = "", max_length: int = 80, required: bool = False) -> None:
    item = _make_modal_text_input(label=label, placeholder=placeholder, current=current, max_length=max_length, required=required)
    modal.add_item(item)
    setattr(modal, attr_name, item)


def _add_modal_label_item(modal, attr_name: str, *, text: str, description: str = "", component=None) -> bool:
    label_cls = getattr(discord.ui, "Label", None)
    if label_cls is None or component is None:
        return False
    try:
        modal.add_item(label_cls(text=str(text or "")[:45], description=(str(description or "")[:100] or None), component=component))
        setattr(modal, attr_name, component)
        return True
    except Exception as e:
        print(f"[tts_modal] Label desativado/falhou: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        return False


def _make_modal_select(custom_id: str, *, placeholder: str, options: list[discord.SelectOption], required: bool = True):
    kwargs = dict(custom_id=custom_id, placeholder=str(placeholder or "")[:150], min_values=1 if required else 0, max_values=1, options=(options or [])[:25])
    try:
        return discord.ui.Select(required=required, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def _role_select_default_values(default_role: discord.Role | None) -> list[object]:
    if default_role is None:
        return []
    default_value_cls = getattr(discord, "SelectDefaultValue", None)
    if default_value_cls is None:
        return []
    from_role = getattr(default_value_cls, "from_role", None)
    if callable(from_role):
        try:
            return [from_role(default_role)]
        except Exception as e:
            print(f"[tts_modal] SelectDefaultValue.from_role falhou: {e!r}")
    try:
        value_type = getattr(getattr(discord, "SelectDefaultValueType", None), "role", None)
        if value_type is not None:
            return [default_value_cls(id=int(getattr(default_role, "id", 0) or 0), type=value_type)]
    except Exception as e:
        print(f"[tts_modal] SelectDefaultValue manual falhou: {e!r}")
    return []


def _make_modal_role_select(custom_id: str, *, placeholder: str, required: bool = False, default_role: discord.Role | None = None):
    role_select_cls = getattr(discord.ui, "RoleSelect", None)
    if role_select_cls is None:
        return None
    default_values = _role_select_default_values(default_role)
    kwargs = dict(
        custom_id=custom_id,
        placeholder=str(placeholder or "")[:150],
        min_values=1 if required else 0,
        max_values=1,
    )
    if default_values:
        kwargs["default_values"] = default_values
    try:
        return role_select_cls(required=required, **kwargs)
    except TypeError:
        kwargs.pop("default_values", None)
        try:
            select = role_select_cls(required=required, **kwargs)
        except TypeError:
            select = role_select_cls(**kwargs)
        if default_values:
            try:
                select.default_values = default_values
            except Exception as e:
                print(f"[tts_modal] RoleSelect default_values indisponível: {e!r}")
        return select


def _radio_value_matches(left: object, right: object) -> bool:
    a = str(left or "").strip().replace("+", "")
    b = str(right or "").strip().replace("+", "")
    if a == b:
        return True
    try:
        na = float(a.lower().replace("hz", "").replace("%", ""))
        nb = float(b.lower().replace("hz", "").replace("%", ""))
        return abs(na - nb) < 0.001
    except Exception:
        return False


def _make_modal_radio(custom_id: str, *, options: list[tuple[str, str, str]], default_value: str):
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=True, options=[])
        default_seen = any(_radio_value_matches(value, default_value) for _, value, _ in options)
        for label, value, description in options:
            is_default = _radio_value_matches(value, default_value) if default_seen else label.casefold() == "normal"
            group.add_option(
                label=label[:100],
                value=str(value)[:100],
                description=(description or None),
                default=is_default,
            )
        return group
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return None


def _add_modal_radio(modal, attr_name: str, *, text: str, description: str, options: list[tuple[str, str, str]], current: str) -> bool:
    group = _make_modal_radio(attr_name, options=options, default_value=str(current or ""))
    if group is None:
        return False
    return _add_modal_label_item(modal, attr_name, text=text, description=description, component=group)


def _make_modal_checkbox_group(custom_id: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None):
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for label, value, description, default in options:
            group.add_option(label=label[:100], value=str(value)[:100], description=(description or None), default=bool(default))
        return group
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        return None


def _edge_language_from_voice(voice: str, default: str = "pt-BR") -> str:
    voice = str(voice or "").strip()
    parts = voice.split("-")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}-{parts[1]}"
    return str(default or "pt-BR")


def _edge_voice_matches_language(voice: str, language: str) -> bool:
    voice = str(voice or "").strip()
    language = str(language or "").strip()
    return bool(voice and language and voice.startswith(language + "-"))


def _edge_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    current = str(current or "pt-BR").strip() or "pt-BR"
    preferred = [current, "pt-BR", "pt-PT", "en-US", "en-GB", "es-ES", "es-MX", "fr-FR", "de-DE", "it-IT", "ja-JP", "ko-KR", "zh-CN"]
    discovered = sorted({
        _edge_language_from_voice(v)
        for v in list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
        if str(v or "").strip()
    })
    seen: set[str] = set()
    options: list[discord.SelectOption] = []
    for code in preferred + discovered:
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        options.append(discord.SelectOption(label=_shorten(code, 100), description="Idioma Edge", value=code, default=(code == current)))
        if len(options) >= 25:
            break
    return options or [discord.SelectOption(label="pt-BR", description="Idioma Edge", value="pt-BR", default=True)]


def _edge_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "") -> list[discord.SelectOption]:
    language = str(language or _edge_language_from_voice(current)).strip() or "pt-BR"
    current = str(current or "").strip()
    voices_source = list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
    preferred = [
        "pt-BR-FranciscaNeural",
        "pt-BR-AntonioNeural",
        "pt-BR-BrendaNeural",
        "pt-BR-DonatoNeural",
        "pt-BR-ElzaNeural",
        "pt-BR-FabioNeural",
        "pt-BR-GiovannaNeural",
        "pt-BR-HumbertoNeural",
        "pt-BR-JulioNeural",
        "pt-BR-LeilaNeural",
        "pt-BR-LeticiaNeural",
        "pt-BR-ManuelaNeural",
        "pt-BR-NicolauNeural",
        "pt-BR-ValerioNeural",
        "pt-BR-YaraNeural",
    ]
    candidates: list[str] = []
    if current and _edge_voice_matches_language(current, language):
        candidates.append(current)
    candidates.extend([v for v in preferred if _edge_voice_matches_language(v, language)])
    candidates.extend([v for v in voices_source if _edge_voice_matches_language(str(v), language)])
    seen: set[str] = set()
    voices: list[str] = []
    for voice in candidates:
        voice = str(voice or "").strip()
        if not voice or voice in seen:
            continue
        seen.add(voice)
        voices.append(voice)
        if len(voices) >= 25:
            break
    if not voices and current:
        voices = [current]
    if not voices:
        voices = ["pt-BR-FranciscaNeural"]
    has_current = current and any(v == current for v in voices)
    options: list[discord.SelectOption] = []
    for idx, voice in enumerate(voices[:25]):
        options.append(
            discord.SelectOption(
                label=_shorten(voice, 100),
                description="Voz Edge",
                value=voice,
                default=(voice == current if has_current else idx == 0),
            )
        )
    return options


def _pick_first_edge_voice_for_language(cog: "TTSVoice", language: str, current: str = "") -> str:
    options = _edge_voice_options_for_language(cog, language=language, current=current)
    if not options:
        return str(current or "pt-BR-FranciscaNeural")
    return str(getattr(options[0], "value", None) or getattr(options[0], "label", None) or current or "pt-BR-FranciscaNeural")


def _top_edge_voice_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    language = _edge_language_from_voice(current)
    return _edge_voice_options_for_language(cog, language=language, current=current)


def _top_gtts_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    preferred = [
        "pt-br", "pt", "en", "es", "fr", "de", "it", "ja", "ko", "zh-cn",
        "ru", "ar", "hi", "id", "tr", "pl", "nl", "sv", "no", "da",
    ]
    items: list[discord.SelectOption] = []
    seen: set[str] = set()
    langs = dict(cog.gtts_languages or {})
    for code in ([current] if current else []) + preferred + sorted(langs):
        code = str(code or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        name = langs.get(code) or code
        label = _shorten(f"{name} ({code})", 100) if name != code else _shorten(code, 100)
        items.append(discord.SelectOption(label=label, description="Idioma gTTS", value=code, default=(code == str(current or "").strip().lower())))
        if len(items) >= 25:
            break
    return items or [discord.SelectOption(label="Português Brasil (pt-br)", description="Idioma gTTS", value="pt-br", default=True)]






def _atts_locale_matches(voice_locale: str, language: str) -> bool:
    voice_locale = _normalize_atts_locale(voice_locale or "", "").strip()
    language = _normalize_atts_locale(language or "pt-BR", "pt-BR").strip()
    if not voice_locale or not language:
        return False
    if voice_locale.casefold() == language.casefold():
        return True
    return voice_locale.split("-", 1)[0].casefold() == language.split("-", 1)[0].casefold()


def _atts_voice_locale_from_name(name: str) -> str:
    raw = str(name or "").strip()
    match = re.match(r"^([a-z]{2})[-_]([A-Za-z]{2})", raw)
    if match:
        return _normalize_atts_locale(f"{match.group(1)}-{match.group(2)}", "")
    match = re.match(r"^([a-z]{2})[-_]", raw)
    if match:
        return match.group(1).lower()
    return ""


def _atts_common_language_options(current: str = "pt-BR", voices: list[dict[str, object]] | None = None) -> list[discord.SelectOption]:
    current = _normalize_atts_locale(current, "pt-BR")
    labels = {
        "pt-BR": "Português Brasil",
        "pt-PT": "Português Portugal",
        "en-US": "Inglês EUA",
        "en-GB": "Inglês Reino Unido",
        "es-ES": "Espanhol Espanha",
        "es-US": "Espanhol EUA",
        "fr-FR": "Francês",
        "de-DE": "Alemão",
        "it-IT": "Italiano",
        "ja-JP": "Japonês",
        "ko-KR": "Coreano",
        "zh-CN": "Chinês",
    }
    ordered: list[str] = [current, "pt-BR", "pt-PT", "en-US", "en-GB", "es-ES", "es-US", "fr-FR", "de-DE", "it-IT", "ja-JP", "ko-KR", "zh-CN"]
    for voice in voices or []:
        locale = _normalize_atts_locale(str((voice or {}).get("locale") or _atts_voice_locale_from_name(str((voice or {}).get("name") or "")) or ""), "")
        if locale and locale not in ordered:
            ordered.append(locale)
    seen: set[str] = set()
    options: list[discord.SelectOption] = []
    for code in ordered:
        code = _normalize_atts_locale(code, "")
        if not code or code in seen:
            continue
        seen.add(code)
        label = labels.get(code) or code
        options.append(discord.SelectOption(label=_shorten(f"{label} ({code})", 100), description="Idioma ATTS", value=code, default=(code == current)))
        if len(options) >= 25:
            break
    return options or [discord.SelectOption(label="Português Brasil (pt-BR)", description="Idioma ATTS", value="pt-BR", default=True)]


def _atts_voice_cache_key(locale: str = "") -> str:
    normalized = _normalize_atts_locale(locale or "", "").strip()
    return normalized.casefold() if normalized else "all"


def _fetch_atts_voice_catalog_sync(locale: str = "", *, limit: int = 500, timeout: float = 2.2, use_cache: bool = True) -> list[dict[str, object]]:
    """Busca o catálogo de vozes do ATTS antes de abrir o modal.

    O modal do Discord não atualiza opções depois de aberto; por isso esta
    função precisa retornar as vozes do idioma salvo atual antes do send_modal.
    """
    now = time.monotonic()
    key = _atts_voice_cache_key(locale)
    by_locale = _ATTS_VOICE_CATALOG_CACHE.setdefault("by_locale", {})
    if isinstance(by_locale, dict) and use_cache:
        cached = by_locale.get(key)
        if isinstance(cached, dict) and now - float(cached.get("at") or 0.0) <= 180.0:
            cached_voices = cached.get("voices")
            if isinstance(cached_voices, list) and cached_voices:
                return [v for v in cached_voices if isinstance(v, dict)]
    try:
        enabled = bool(getattr(config, "PHONE_WORKER_ENABLED", False))
        host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
        token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
        if not enabled or not host or not token:
            _ATTS_VOICE_CATALOG_CACHE["last_error"] = "worker_unavailable"
            return []
        scheme = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
        if scheme not in {"http", "https"}:
            scheme = "http"
        port = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
        url = f"{scheme}://{host}:{port}/task"
        payload = {"task": "tts_android_voices", "locale": str(locale or ""), "limit": int(limit or 500)}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "CoreBot/ATTSModal",
        })
        with urllib.request.urlopen(req, timeout=max(0.2, float(timeout))) as response:
            raw = response.read(1024 * 1024)
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        if not isinstance(parsed, dict) or not bool(parsed.get("ok", True)):
            _ATTS_VOICE_CATALOG_CACHE["last_error"] = str((parsed or {}).get("error") if isinstance(parsed, dict) else "invalid_response")[:180]
            return []
        voices = parsed.get("voices")
        if not isinstance(voices, list):
            _ATTS_VOICE_CATALOG_CACHE["last_error"] = "missing_voices"
            return []
        normalized = [v for v in voices if isinstance(v, dict) and str(v.get("name") or "").strip()]
        if isinstance(by_locale, dict) and normalized:
            by_locale[key] = {"at": now, "voices": normalized}
            _ATTS_VOICE_CATALOG_CACHE["last_error"] = ""
        elif not normalized:
            _ATTS_VOICE_CATALOG_CACHE["last_error"] = "empty_voices"
        return normalized
    except Exception as e:
        _ATTS_VOICE_CATALOG_CACHE["last_error"] = f"{type(e).__name__}: {_shorten(str(e), 160)}"
        print(f"[tts_modal] catálogo ATTS indisponível: {e!r}")
        return []


def _atts_voice_score(voice: dict[str, object], language: str) -> int:
    name = str(voice.get("name") or "")
    locale = str(voice.get("locale") or _atts_voice_locale_from_name(name) or "")
    score = 0
    if _normalize_atts_locale(locale, "").casefold() == _normalize_atts_locale(language, "pt-BR").casefold():
        score += 100
    elif _atts_locale_matches(locale, language):
        score += 50
    lowered = name.casefold()
    if "local" in lowered:
        score += 25
    if bool(voice.get("network_required")):
        score -= 40
    try:
        score += int(voice.get("quality") or 0) // 100
    except Exception:
        pass
    try:
        score -= int(voice.get("latency") or 0) // 100
    except Exception:
        pass
    return score


def _atts_matching_voices_for_language(catalog: list[dict[str, object]] | None, language: str) -> list[dict[str, object]]:
    language = _normalize_atts_locale(language, "pt-BR")
    matching: list[dict[str, object]] = []
    for voice in catalog or []:
        if not isinstance(voice, dict):
            continue
        name = str(voice.get("name") or "").strip()
        if not name:
            continue
        locale = str(voice.get("locale") or _atts_voice_locale_from_name(name) or "")
        # O Android às vezes coloca o idioma apenas no nome da voz.
        if _atts_locale_matches(locale, language) or _atts_locale_matches(_atts_voice_locale_from_name(name), language):
            matching.append(voice)
    matching.sort(key=lambda v: (-_atts_voice_score(v, language), str(v.get("name") or "")))
    return matching


def _atts_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "", catalog: list[dict[str, object]] | None = None) -> list[discord.SelectOption]:
    language = _normalize_atts_locale(language, "pt-BR")
    current = str(current or "").strip()
    resolved_catalog = list(catalog) if catalog is not None else _fetch_atts_voice_catalog_sync(language)
    matching = _atts_matching_voices_for_language(resolved_catalog, language)
    if current and current.casefold() not in {"auto", "default", "padrao", "padrão"} and not any(str(v.get("name") or "") == current for v in matching):
        matching.insert(0, {"name": current, "locale": _atts_voice_locale_from_name(current), "network_required": False, "quality": 0, "latency": 0})
    options: list[discord.SelectOption] = [
        discord.SelectOption(label="Automática rápida", description="Prefere voz local", value="auto", default=(not current or current.casefold() in {"auto", "automatica", "automática"})),
        discord.SelectOption(label="Padrão", description="Voz padrão do sistema", value="default", default=(current.casefold() in {"default", "padrao", "padrão"})),
    ]
    seen = {"auto", "default"}
    for voice in matching:
        name = str(voice.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        locale = _normalize_atts_locale(str(voice.get("locale") or _atts_voice_locale_from_name(name) or language), language)
        network = bool(voice.get("network_required")) or "network" in name.casefold()
        local = "local" in name.casefold() or not network
        desc_bits = [locale]
        desc_bits.append("local" if local else "online")
        if network:
            desc_bits.append("usa internet")
        options.append(discord.SelectOption(label=_shorten(name, 100), description=_shorten(" · ".join(desc_bits), 100), value=name[:100], default=(name == current)))
        if len(options) >= 25:
            break
    return _with_default_option(options, current if current else "auto")


def _atts_voice_matches_language(voice: str, language: str) -> bool:
    voice = str(voice or "").strip()
    if not voice or voice.casefold() in {"auto", "default", "padrao", "padrão", "automatica", "automática"}:
        return True
    catalog = _fetch_atts_voice_catalog_sync(language)
    for item in catalog:
        if str(item.get("name") or "") == voice:
            return _atts_locale_matches(str(item.get("locale") or _atts_voice_locale_from_name(voice) or ""), language)
    return _atts_locale_matches(_atts_voice_locale_from_name(voice), language)


def _pick_atts_voice_for_language(cog: "TTSVoice", language: str, current: str = "") -> str:
    options = _atts_voice_options_for_language(cog, language=language, current=current)
    for option in options:
        value = str(getattr(option, "value", "") or "").strip()
        if value and value not in {"auto", "default"}:
            return value
    return ""


def _atts_modal_current_language(cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None) -> str:
    user_id = int(target_user_id or 0)
    guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
    return _normalize_atts_locale(_current_tts_value(cog, guild_id, user_id, "android_language", "pt-BR", server=server), "pt-BR")


def _atts_catalog_ready_for_language(catalog: list[dict[str, object]] | None, language: str) -> bool:
    return bool(_atts_matching_voices_for_language(catalog or [], language))


async def _load_atts_voice_catalog_for_modal(language: str) -> list[dict[str, object]]:
    language = _normalize_atts_locale(language, "pt-BR")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_atts_voice_catalog_sync, language, limit=500, timeout=2.2, use_cache=True),
            timeout=2.7,
        )
    except Exception as e:
        _ATTS_VOICE_CATALOG_CACHE["last_error"] = f"{type(e).__name__}: {_shorten(str(e), 160)}"
        print(f"[tts_modal] catálogo ATTS indisponível antes do modal: {e!r}")
        return []


async def _send_minimal_atts_unavailable(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(ATTS_LOAD_ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(ATTS_LOAD_ERROR_MESSAGE, ephemeral=True)
    except Exception:
        pass


async def _send_atts_settings_modal(
    interaction: discord.Interaction,
    cog: "TTSVoice",
    panel_message: discord.Message | None,
    *,
    server: bool,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
    context: str = "atts",
) -> None:
    language = _atts_modal_current_language(cog, panel_message, server=server, target_user_id=target_user_id)
    catalog = await _load_atts_voice_catalog_for_modal(language)
    if not _atts_catalog_ready_for_language(catalog, language):
        print(f"[tts_modal] ATTS indisponível em {context}: language={language} voices={len(catalog or [])} error={_ATTS_VOICE_CATALOG_CACHE.get('last_error')!r}")
        await _send_minimal_atts_unavailable(interaction)
        return
    try:
        await interaction.response.send_modal(
            AndroidSettingsModal(
                cog,
                panel_message,
                server=server,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                voice_catalog=catalog,
            )
        )
    except Exception as e:
        print(f"[tts_modal] modal ATTS falhou em {context}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        await _send_minimal_atts_unavailable(interaction)


async def _save_tts_modal_updates(
    cog: "TTSVoice",
    interaction: discord.Interaction,
    *,
    source_panel_message: discord.Message | None,
    server: bool,
    updates: dict[str, object],
    history_label: str,
    history_value: str,
    success_title: str,
    success_description: str,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
            ephemeral=True,
        )
        return
    if server and not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
        await interaction.response.send_message(
            embed=cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o TTS do servidor.", ok=False),
            ephemeral=True,
        )
        return
    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    clean_updates = {k: v for k, v in (updates or {}).items() if v is not None}
    if not clean_updates:
        await interaction.response.send_message(
            embed=cog._make_embed("Nada mudou", "Nenhum ajuste foi alterado.", ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(
        interaction,
        server=server,
        message_id=message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
    )

    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, **clean_updates))
        history_entry = cog._server_history_text(interaction, history_label, history_value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        history_user_id = interaction.user.id
        panel_kind = "server"
    else:
        history_entry = cog._user_history_text(
            interaction,
            history_label,
            history_value,
            message_id=message_id,
            target_user_id=effective_user_id,
            target_user_name=effective_user_name,
        )
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, history_entry=history_entry, **clean_updates)
        cog._append_public_panel_history(message_id, history_entry)
        history_user_id = effective_user_id
        panel_kind = "user"

    state = cog._public_panel_states.get(message_id or 0, {}) if message_id else {}
    if panel_message is not None and state.get("panel_kind") == "launcher":
        history_text = cog._format_history_entries(
            cog._get_public_panel_history(message_id),
            viewer_user_id=None,
            message_id=message_id,
        ) or "• Nada recente."
        view = cog._build_public_tts_launcher_view(interaction.guild.id, timeout=300, history_text=history_text)
        view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=cog._make_embed("TTS", "Cada prefixo escolhe um modo de voz.", ok=True),
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
        if server:
            await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)
        return

    should_edit_panel = bool(panel_message is not None)
    if should_edit_panel:
        panel_history = await cog._maybe_await(db.get_panel_history(interaction.guild.id, history_user_id)) if hasattr(db, "get_panel_history") else {}
        key = "server_last_changes" if server else "user_last_changes"
        last_changes = list((panel_history or {}).get(key, []) or [])
        embed = await cog._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind=panel_kind,
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = cog._build_panel_view(
            0 if message_id in cog._public_panel_states else interaction.user.id,
            interaction.guild.id,
            server=server,
            target_user_id=view_target_user_id,
            target_user_name=view_target_user_name,
        )
        if panel_message is not None:
            view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
    else:
        await interaction.response.send_message(
            embed=cog._make_embed(success_title, success_description, ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    if server:
        await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)


class EdgeSettingsModal(discord.ui.Modal, title="Editar Edge"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_voice = _current_tts_value(cog, guild_id, user_id, "voice", str(getattr(config, "EDGE_TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural"), server=server)
        self.current_language = _edge_language_from_voice(self.current_voice)
        self.current_rate = _current_tts_value(cog, guild_id, user_id, "rate", "+0%", server=server)
        self.current_pitch = _current_tts_value(cog, guild_id, user_id, "pitch", "+0Hz", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            language_select = _make_modal_select(
                "edge_language",
                placeholder="Idioma Edge",
                options=_edge_language_options(self.cog, self.current_language),
            )
            voice_select = _make_modal_select(
                "edge_voice",
                placeholder="Escolha a voz Edge",
                options=_edge_voice_options_for_language(self.cog, language=self.current_language, current=self.current_voice),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma Edge",
                description="",
                component=language_select,
            )
            ok = ok and _add_modal_label_item(
                self,
                "voice",
                text="Voz Edge",
                description="",
                component=voice_select,
            )
            ok = ok and _add_modal_radio(
                self,
                "rate",
                text="Velocidade Edge",
                description="",
                current=self.current_rate,
                options=[
                    ("Bem mais lenta", "-50%", ""),
                    ("Mais lenta", "-25%", ""),
                    ("Normal", "+0%", ""),
                    ("Mais rápida", "+25%", ""),
                    ("Bem mais rápida", "+50%", ""),
                ],
            )
            ok = ok and _add_modal_radio(
                self,
                "pitch",
                text="Tom Edge",
                description="",
                current=self.current_pitch,
                options=[
                    ("Bem mais grave", "-50Hz", ""),
                    ("Mais grave", "-25Hz", ""),
                    ("Normal", "+0Hz", ""),
                    ("Mais agudo", "+25Hz", ""),
                    ("Bem mais agudo", "+50Hz", ""),
                ],
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] Edge guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma Edge",
            placeholder="Ex.: pt-BR, en-US, es-ES",
            current=self.current_language,
            max_length=16,
        )
        _add_modal_text_input(
            self,
            "voice",
            label="Voz Edge",
            placeholder="Voz usada com ,texto. Ex.: pt-BR-FranciscaNeural",
            current=self.current_voice,
            max_length=80,
        )
        _add_modal_text_input(
            self,
            "rate",
            label="Velocidade Edge",
            placeholder="Use +0% normal, -25% lenta ou +25% rápida",
            current=self.current_rate,
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "pitch",
            label="Tom Edge",
            placeholder="Use +0Hz normal, -25Hz grave ou +25Hz agudo",
            current=self.current_pitch,
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []
        history_bits: list[str] = []

        language = _single_component_value(getattr(self, "language", None), self.current_language)
        selected_language = str(language or self.current_language or "pt-BR").strip() or "pt-BR"
        voice = _single_component_value(getattr(self, "voice", None), self.current_voice)
        selected_voice = str(voice or self.current_voice or "").strip()
        adjusted_voice = ""
        if selected_language and selected_voice and not _edge_voice_matches_language(selected_voice, selected_language):
            adjusted_voice = _pick_first_edge_voice_for_language(self.cog, selected_language, self.current_voice)
            selected_voice = adjusted_voice
        if selected_voice and str(selected_voice) != str(self.current_voice):
            if selected_voice not in self.cog.edge_voice_names and selected_voice not in self.cog.edge_voice_cache:
                await interaction.response.send_message(embed=self.cog._make_embed("Voz inválida", "Essa voz Edge não foi encontrada.", ok=False), ephemeral=True)
                return
            updates["voice"] = selected_voice
            if selected_language != self.current_language and adjusted_voice:
                details.append(f"• Idioma: {selected_language}")
                details.append(f"• Voz ajustada: {human_voice_name(selected_voice)}")
                history_bits.append("idioma e voz atualizados")
            else:
                details.append(f"• Voz: {human_voice_name(selected_voice)}")
                history_bits.append(f"voz {human_voice_name(selected_voice)}")
        elif selected_language != self.current_language:
            picked_voice = _pick_first_edge_voice_for_language(self.cog, selected_language, self.current_voice)
            if picked_voice and picked_voice != self.current_voice:
                updates["voice"] = picked_voice
                details.append(f"• Idioma: {selected_language}")
                details.append(f"• Voz ajustada: {human_voice_name(picked_voice)}")
                history_bits.append("idioma e voz atualizados")

        rate = _single_component_value(getattr(self, "rate", None), self.current_rate)
        if rate:
            normalized = self.cog._normalize_rate_value(rate)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use opções como `+0%`, `-25%` ou `+25%`.", ok=False), ephemeral=True)
                return
            current_rate = self.cog._normalize_rate_value(self.current_rate) or self.current_rate
            if str(normalized) != str(current_rate):
                updates["rate"] = normalized
                details.append(f"• Velocidade: {human_rate(normalized)}")
                history_bits.append(f"velocidade {human_rate(normalized)}")

        pitch = _single_component_value(getattr(self, "pitch", None), self.current_pitch)
        if pitch:
            normalized = self.cog._normalize_pitch_value(pitch)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use opções como `+0Hz`, `-25Hz` ou `+25Hz`.", ok=False), ephemeral=True)
                return
            current_pitch = self.cog._normalize_pitch_value(self.current_pitch) or self.current_pitch
            if str(normalized) != str(current_pitch):
                updates["pitch"] = normalized
                details.append(f"• Tom: {human_pitch(normalized)}")
                history_bits.append(f"tom {human_pitch(normalized)}")

        if len(history_bits) == 1 and history_bits[0] != "idioma e voz atualizados":
            key = history_bits[0].split(' ', 1)[0]
            article = {"voz": "a", "velocidade": "a", "tom": "o"}.get(key, "o")
            history_label = f"{article} {'própria ' if not self.server else ''}{key} do Edge"
            history_value = history_bits[0].split(' ', 1)[1] if ' ' in history_bits[0] else "atualizado"
        else:
            history_label = "o Edge" if self.server else "o próprio Edge"
            if "idioma e voz atualizados" in history_bits:
                history_value = "idioma e voz atualizados"
            else:
                history_value = "leitura atualizada" if updates and all(k in updates for k in ("rate", "pitch")) and "voice" not in updates else "configurações atualizadas"

        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label=history_label,
            history_value=history_value,
            success_title="Edge atualizado",
            success_description="\n".join(details) if details else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GTTSSettingsModal(discord.ui.Modal, title="Editar gTTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = _current_tts_value(cog, guild_id, user_id, "language", "pt-br", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            language_select = _make_modal_select(
                "gtts_language",
                placeholder="Idioma gTTS",
                options=_with_default_option(_top_gtts_language_options(self.cog, self.current_language), self.current_language),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma gTTS",
                description="",
                component=language_select,
            )
            manual_input = _make_modal_text_input(
                label=None,
                placeholder="Opcional: pt-br, en, es, ja",
                current="",
                max_length=10,
                required=False,
            )
            ok = ok and _add_modal_label_item(
                self,
                "manual_language",
                text="Outro idioma",
                description="Opcional. Substitui a seleção acima.",
                component=manual_input,
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] gTTS guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma gTTS",
            placeholder="Ex.: pt-br, en, es, fr, ja",
            current=self.current_language,
            max_length=10,
        )

    async def on_submit(self, interaction: discord.Interaction):
        selected = _single_component_value(getattr(self, "language", None), self.current_language)
        manual = _item_value(getattr(self, "manual_language", None))
        raw_value = manual or selected
        code, _language_name = self.cog._resolve_gtts_language_input(raw_value)
        if code is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idioma inválido", "Use algo como `pt-br`, `en`, `es` ou `ja`.", ok=False),
                ephemeral=True,
            )
            return
        updates = {"language": code} if str(code) != str(self.current_language) else {}
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o idioma do modo gTTS" if self.server else "o próprio idioma do gTTS",
            history_value=code,
            success_title="gTTS atualizado",
            success_description=f"• Idioma: {human_language_name(code)}" if updates else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )



def _normalize_atts_locale(value: object, default: str = "pt-BR") -> str:
    raw = str(value or default or "").strip().replace("_", "-")
    if not raw:
        return str(default or "").strip()
    parts = [p for p in raw.split("-") if p]
    if len(parts) == 1:
        return parts[0].lower()
    return f"{parts[0].lower()}-{parts[1].upper()}"


def _normalize_atts_factor(value: object, default: str = "1.0") -> str | None:
    raw = str(value or default or "1.0").strip().lower().replace("x", "")
    raw = raw.replace(",", ".")
    try:
        number = float(raw)
    except Exception:
        return None
    number = max(0.5, min(2.0, number))
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text if text else "1"


def _normalize_atts_custom_factor(value: object) -> str | None:
    raw = str(value or "").strip().lower().replace("x", "").replace(",", ".")
    if not raw:
        return None
    try:
        number = float(raw)
    except Exception:
        return None
    if number < 0.5 or number > 2.0:
        return None
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text if text else "1"


def _atts_modal_radio_default(current: str, presets: set[str]) -> str:
    normalized = _normalize_atts_factor(current, "1.0") or "1"
    return normalized if normalized in presets else "custom"


def _parse_atts_custom_values(value: object, *, default_rate: str = "1.0", default_pitch: str = "1.0") -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return str(default_rate or "1.0"), str(default_pitch or "1.0")
    if "/" in raw:
        parts = raw.split("/", 1)
    elif ";" in raw:
        parts = raw.split(";", 1)
    else:
        parts = raw.split(None, 1)
    if len(parts) == 1:
        first = parts[0].strip()
        return first, first
    return parts[0].strip(), parts[1].strip()


class AndroidSettingsModal(discord.ui.Modal, title="Editar ATTS"):
    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        server: bool,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        force_text_fallback: bool = False,
        voice_catalog: list[dict[str, object]] | None = None,
        allow_text_fallback: bool = False,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        self.allow_text_fallback = bool(allow_text_fallback)
        self.voice_catalog = list(voice_catalog or [])
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = _normalize_atts_locale(_current_tts_value(cog, guild_id, user_id, "android_language", "pt-BR", server=server), "pt-BR")
        self.current_voice = str(_current_tts_value(cog, guild_id, user_id, "android_voice", "", server=server) or "").strip()
        self.current_rate = _normalize_atts_factor(_current_tts_value(cog, guild_id, user_id, "android_rate", "1.0", server=server), "1.0") or "1.0"
        self.current_pitch = _normalize_atts_factor(_current_tts_value(cog, guild_id, user_id, "android_pitch", "1.0", server=server), "1.0") or "1.0"

        guided_ok = False if self.force_text_fallback else self._build_guided_modal()
        if guided_ok:
            return

        # O ATTS depende do catálogo do worker para montar as opções.
        # Se o modal guiado não puder ser montado, não abrimos o formulário antigo
        # de campos livres para o usuário comum; mostramos a mensagem mínima no
        # chamador. O fallback textual fica só para chamadas legadas explícitas.
        if self.force_text_fallback or self.allow_text_fallback:
            self._build_text_fallback()
            return

        raise RuntimeError("atts_guided_modal_unavailable")

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            catalog = list(self.voice_catalog or [])
            if not _atts_catalog_ready_for_language(catalog, self.current_language):
                return False
            language_select = _make_modal_select(
                "android_language",
                placeholder="Idioma ATTS",
                options=_with_default_option(_atts_common_language_options(self.current_language, catalog), self.current_language),
            )
            voice_select = _make_modal_select(
                "android_voice",
                placeholder="Voz ATTS",
                options=_atts_voice_options_for_language(self.cog, language=self.current_language, current=self.current_voice, catalog=catalog),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma ATTS",
                description="",
                component=language_select,
            )
            ok = ok and _add_modal_label_item(
                self,
                "voice",
                text="Voz ATTS",
                description="",
                component=voice_select,
            )
            rate_presets = {"0.75", "1.0", "1.25", "1.5"}
            pitch_presets = {"0.8", "1.0", "1.2", "1.4"}
            ok = ok and _add_modal_radio(
                self,
                "rate",
                text="Velocidade ATTS",
                description="",
                current=_atts_modal_radio_default(self.current_rate, rate_presets),
                options=[
                    ("Mais lenta", "0.75", ""),
                    ("Normal", "1.0", ""),
                    ("Mais rápida", "1.25", ""),
                    ("Bem mais rápida", "1.5", ""),
                    ("Custom", "custom", "Usa o valor custom abaixo"),
                ],
            )
            ok = ok and _add_modal_radio(
                self,
                "pitch",
                text="Tom ATTS",
                description="",
                current=_atts_modal_radio_default(self.current_pitch, pitch_presets),
                options=[
                    ("Mais grave", "0.8", ""),
                    ("Normal", "1.0", ""),
                    ("Mais agudo", "1.2", ""),
                    ("Bem mais agudo", "1.4", ""),
                    ("Custom", "custom", "Usa o valor custom abaixo"),
                ],
            )
            custom_values = _make_modal_text_input(
                label=None,
                placeholder="Velocidade / tom — ex.: 1.0 / 1.0",
                current=f"{self.current_rate or '1.0'} / {self.current_pitch or '1.0'}",
                max_length=32,
                required=False,
            )
            ok = ok and _add_modal_label_item(
                self,
                "custom_values",
                text="Valores custom",
                description="Use quando escolher Custom em velocidade ou tom.",
                component=custom_values,
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] ATTS guiado falhou: {e!r}")
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma ATTS",
            placeholder="Ex.: pt-BR, en-US, es-ES",
            current=self.current_language or "pt-BR",
            max_length=16,
        )
        _add_modal_text_input(
            self,
            "voice",
            label="Voz ATTS",
            placeholder="Vazio/auto usa a voz padrão do Android",
            current=self.current_voice or "",
            max_length=96,
            required=False,
        )
        _add_modal_text_input(
            self,
            "rate",
            label="Velocidade ATTS",
            placeholder="1.0 normal, 0.8 lenta, 1.25 rápida",
            current=self.current_rate or "1.0",
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "pitch",
            label="Tom ATTS",
            placeholder="1.0 normal, 0.8 grave, 1.2 agudo",
            current=self.current_pitch or "1.0",
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []
        history_bits: list[str] = []

        language = _normalize_atts_locale(_single_component_value(getattr(self, "language", None), self.current_language), "pt-BR")
        current_language = _normalize_atts_locale(self.current_language, "pt-BR")
        if language != current_language:
            updates["android_language"] = language
            details.append(f"• Idioma: `{language}`")
            history_bits.append(f"idioma {language}")

        raw_voice = _single_component_value(getattr(self, "voice", None), self.current_voice).strip()
        raw_lower = raw_voice.casefold()
        if raw_lower in {"", "auto", "automatica", "automática", "rapida", "rápida", "automatica rapida", "automática rápida"}:
            voice = ""
            voice_label = "Automática rápida"
        elif raw_lower in {"default", "padrao", "padrão", "voz padrao", "voz padrão"}:
            voice = "default"
            voice_label = "Padrão do Android"
        else:
            voice = raw_voice[:96]
            voice_label = voice

        # Se o usuário trocou o idioma no mesmo modal, o Discord ainda mostra
        # as vozes do idioma anterior até o próximo modal. Evitamos salvar uma
        # voz incompatível e voltamos para automática rápida.
        if voice and voice != "default" and not _atts_voice_matches_language(voice, language):
            voice = ""
            voice_label = "Automática rápida"

        current_voice = str(self.current_voice or "").strip()
        current_norm = "" if current_voice.casefold() in {"", "auto", "automatica", "automática"} else current_voice
        if voice != current_norm:
            updates["android_voice"] = voice
            details.append(f"• Voz: `{voice_label}`")
            history_bits.append(f"voz {voice_label}")

        rate_choice = _single_component_value(getattr(self, "rate", None), self.current_rate)
        pitch_choice = _single_component_value(getattr(self, "pitch", None), self.current_pitch)
        custom_rate_raw, custom_pitch_raw = _parse_atts_custom_values(
            _single_component_value(getattr(self, "custom_values", None), f"{self.current_rate or '1.0'} / {self.current_pitch or '1.0'}"),
            default_rate=self.current_rate or "1.0",
            default_pitch=self.current_pitch or "1.0",
        )

        if str(rate_choice or "").strip().casefold() == "custom":
            rate = _normalize_atts_custom_factor(custom_rate_raw)
            if rate is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use um número entre 0.5 e 2.0.", ok=False), ephemeral=True)
                return
        else:
            rate = _normalize_atts_factor(rate_choice, "1.0")
            if rate is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use uma opção de velocidade do ATTS.", ok=False), ephemeral=True)
                return
        current_rate = _normalize_atts_factor(self.current_rate, "1.0") or "1"
        if rate != current_rate:
            updates["android_rate"] = rate
            details.append(f"• Velocidade: `{rate}x`")
            history_bits.append(f"velocidade {rate}x")

        if str(pitch_choice or "").strip().casefold() == "custom":
            pitch = _normalize_atts_custom_factor(custom_pitch_raw)
            if pitch is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use um número entre 0.5 e 2.0.", ok=False), ephemeral=True)
                return
        else:
            pitch = _normalize_atts_factor(pitch_choice, "1.0")
            if pitch is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use uma opção de tom do ATTS.", ok=False), ephemeral=True)
                return
        current_pitch = _normalize_atts_factor(self.current_pitch, "1.0") or "1"
        if pitch != current_pitch:
            updates["android_pitch"] = pitch
            details.append(f"• Tom: `{pitch}x`")
            history_bits.append(f"tom {pitch}x")

        history_label = "o ATTS" if self.server else "o próprio ATTS"
        if len(history_bits) == 1:
            history_value = history_bits[0]
        elif updates:
            history_value = "configurações atualizadas"
        else:
            history_value = "sem alterações"

        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label=history_label,
            history_value=history_value,
            success_title="ATTS atualizado",
            success_description="\n".join(details) if details else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class ServerPrefixesModal(discord.ui.Modal, title="Prefixos do servidor"):
    bot_prefix = discord.ui.TextInput(label="Prefixo do bot", placeholder="Ex.: _", required=True, max_length=8)
    atts_prefix = discord.ui.TextInput(label="Prefixo do ATTS", placeholder="Ex.: %", required=True, max_length=8)
    teto_prefix = discord.ui.TextInput(label="Prefixo da Kasane Teto", placeholder="Ex.: '", required=True, max_length=8)
    gtts_prefix = discord.ui.TextInput(label="Prefixo do gTTS", placeholder="Ex.: .", required=True, max_length=8)
    edge_prefix = discord.ui.TextInput(label="Prefixo do Edge", placeholder="Ex.: ,", required=True, max_length=8)

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        bot_prefix = str((defaults or {}).get("bot_prefix") or getattr(config, "PREFIX", "_") or "_")[:8]
        atts_prefix = str((defaults or {}).get("atts_prefix") or getattr(config, "TTS_ATTS_PREFIX", "%") or "%")[:8]
        teto_prefix = str((defaults or {}).get("teto_prefix") or getattr(config, "TTS_TETO_PREFIX", "'") or "'")[:8]
        edge_prefix = str((defaults or {}).get("edge_prefix") or getattr(config, "EDGE_TTS_PREFIX", ",") or ",")[:8]
        gtts_prefix = str((defaults or {}).get("gtts_prefix") or (defaults or {}).get("tts_prefix") or getattr(config, "TTS_PREFIX", ".") or ".")[:8]
        # Guilds antigas podiam herdar o mesmo `tts_prefix` para gTTS e Edge.
        # O modal já abre com um valor migrável, sem exigir que a staff descubra
        # manualmente por que uma configuração histórica ficou inválida.
        occupied = {bot_prefix, atts_prefix, teto_prefix, edge_prefix}
        if not gtts_prefix or gtts_prefix in occupied:
            gtts_prefix = next((candidate for candidate in (".", "!", ";", "~", "?") if candidate not in occupied), ".")
        self.bot_prefix.default = bot_prefix
        self.atts_prefix.default = atts_prefix
        self.teto_prefix.default = teto_prefix
        self.gtts_prefix.default = gtts_prefix
        self.edge_prefix.default = edge_prefix

    async def on_submit(self, interaction: discord.Interaction):
        values = {
            "bot_prefix": _item_value(self.bot_prefix)[:8],
            "atts_prefix": _item_value(self.atts_prefix)[:8],
            "teto_prefix": _item_value(self.teto_prefix)[:8],
            "gtts_prefix": _item_value(self.gtts_prefix)[:8],
            "edge_prefix": _item_value(self.edge_prefix)[:8],
        }
        valid, validation_error = validate_prefix_values(**values)
        if not valid:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Prefixo inválido", validation_error, ok=False),
                ephemeral=True,
            )
            return
        updates = dict(values)
        updates["tts_prefix"] = values["gtts_prefix"]
        parts = [
            f"bot: {values['bot_prefix']}",
            f"ATTS: {values['atts_prefix']}",
            f"Teto: {values['teto_prefix']}",
            f"gTTS: {values['gtts_prefix']}",
            f"Edge: {values['edge_prefix']}",
        ]
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            history_label="os prefixos do servidor",
            history_value=", ".join(parts),
            success_title="Prefixos atualizados",
            success_description="Salvo: " + ", ".join(parts) + ".",
        )


class TTSServerRulesModal(discord.ui.Modal, title="Regras do TTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.force_text_fallback = bool(force_text_fallback)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        self.current_announce_author = bool((defaults or {}).get("announce_author"))
        role_id = int((defaults or {}).get("ignored_tts_role_id") or 0)
        self.current_ignored_role_id = role_id
        if "ignored_tts_role_enabled" in (defaults or {}):
            self.current_ignored_role_enabled = bool((defaults or {}).get("ignored_tts_role_enabled", False))
        else:
            # Migração suave: se já havia cargo salvo antes da flag, ele começa ativo.
            self.current_ignored_role_enabled = bool(role_id)
        self.current_ignored_role = str(role_id) if role_id else ""
        self.current_ignored_role_name = ""
        self.current_ignored_role_obj = None
        try:
            guild = getattr(panel_message, "guild", None)
            role = guild.get_role(role_id) if guild is not None and role_id else None
            self.current_ignored_role_obj = role
            self.current_ignored_role_name = str(getattr(role, "name", "") or "")
        except Exception:
            self.current_ignored_role_name = ""
            self.current_ignored_role_obj = None
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            rules = _make_modal_checkbox_group(
                "tts_rules",
                options=[
                    (
                        "Autor antes da frase",
                        "announce_author",
                        "Fala o nome de quem mandou.",
                        self.current_announce_author,
                    ),
                    (
                        "Cargo ignorado",
                        "ignored_role_enabled",
                        "",
                        self.current_ignored_role_enabled,
                    ),
                ],
                min_values=0,
                max_values=2,
            )
            if rules is None:
                return False
            ok = _add_modal_label_item(
                self,
                "rules",
                text="Regras",
                component=rules,
            )

            role_select = _make_modal_role_select(
                "ignored_role",
                placeholder="Escolha o cargo ignorado",
                required=False,
                default_role=self.current_ignored_role_obj,
            )
            if role_select is not None:
                ok = ok and _add_modal_label_item(
                    self,
                    "ignored_role",
                    text="Cargo ignorado",
                    component=role_select,
                )
            else:
                role_input = _make_modal_text_input(
                    label=None,
                    placeholder="ID/nome. Vazio mantém; off desliga sem apagar.",
                    current=self.current_ignored_role,
                    max_length=80,
                    required=False,
                )
                ok = ok and _add_modal_label_item(
                    self,
                    "ignored_role",
                    text="Cargo ignorado",
                    component=role_input,
                )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] regras guiadas falharam: {e!r}")
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "announce_author",
            label="Autor antes da frase",
            placeholder="sim para ligar; não para desligar",
            current="sim" if self.current_announce_author else "não",
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "ignored_role_enabled",
            label="Cargo ignorado ativo",
            placeholder="sim para ligar; não para desligar",
            current="sim" if self.current_ignored_role_enabled else "não",
            max_length=8,
            required=False,
        )
        _add_modal_text_input(
            self,
            "ignored_role",
            label="Cargo ignorado",
            placeholder="ID/nome. Vazio mantém; off desliga sem apagar.",
            current=self.current_ignored_role,
            max_length=80,
            required=False,
        )

    def _find_role(self, guild: discord.Guild | None, raw: str) -> discord.Role | None:
        if guild is None:
            return None
        value = str(raw or "").strip()
        if not value:
            return None
        match = re.fullmatch(r"<@&(\d+)>", value)
        role_id = int(match.group(1)) if match else int(value) if value.isdigit() else 0
        if role_id:
            return guild.get_role(role_id)
        lowered = value.casefold()
        for role in getattr(guild, "roles", []) or []:
            if str(getattr(role, "name", "")).casefold() == lowered:
                return role
        return None

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        parts: list[str] = []

        if hasattr(self, "rules"):
            values = set(_select_values(getattr(self, "rules", None)))
            enabled = "announce_author" in values
            ignored_enabled = "ignored_role_enabled" in values
            if enabled != self.current_announce_author:
                updates["announce_author"] = enabled
                parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
            if ignored_enabled != self.current_ignored_role_enabled:
                updates["ignored_tts_role_enabled"] = ignored_enabled
                parts.append("cargo ignorado ligado" if ignored_enabled else "cargo ignorado desligado")
        else:
            text = _item_value(getattr(self, "announce_author", None)).lower()
            if text:
                enabled = text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado", "ligado"}
                if enabled != self.current_announce_author:
                    updates["announce_author"] = enabled
                    parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
            role_enabled_text = _item_value(getattr(self, "ignored_role_enabled", None)).lower()
            if role_enabled_text:
                ignored_enabled = role_enabled_text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado", "ligado"}
                if ignored_enabled != self.current_ignored_role_enabled:
                    updates["ignored_tts_role_enabled"] = ignored_enabled
                    parts.append("cargo ignorado ligado" if ignored_enabled else "cargo ignorado desligado")

        def _mark_ignored_role_changed(role: discord.Role) -> None:
            role_id = int(getattr(role, "id", 0) or 0)
            if not role_id:
                return
            role_text = getattr(role, "mention", None) or getattr(role, "name", None) or "cargo"
            if role_id != self.current_ignored_role_id:
                updates["ignored_tts_role_id"] = role_id
                parts.append(f"cargo ignorado {role_text}")
                # Trocar/definir cargo liga a regra automaticamente. Isso não
                # deve acontecer apenas por causa do cargo salvo vindo como
                # default_values no RoleSelect.
                updates["ignored_tts_role_enabled"] = True
                parts[:] = [part for part in parts if part != "cargo ignorado desligado"]
                if "cargo ignorado ligado" not in parts:
                    parts.append("cargo ignorado ligado")

        selected_role = _first_selected_role(getattr(self, "ignored_role", None))
        if selected_role is not None:
            _mark_ignored_role_changed(selected_role)
        else:
            raw_role = _item_value(getattr(self, "ignored_role", None))
            if raw_role:
                raw_lower = raw_role.strip().lower()
                if raw_lower in {"0", "nenhum", "remover", "remove", "off", "desativar", "desligar", "não", "nao"}:
                    if self.current_ignored_role_enabled:
                        updates["ignored_tts_role_enabled"] = False
                        parts.append("cargo ignorado desligado")
                else:
                    role = self._find_role(getattr(interaction, "guild", None), raw_role)
                    if role is None:
                        await interaction.response.send_message(
                            embed=self.cog._make_embed("Cargo não encontrado", "Use menção, ID ou nome exato do cargo.", ok=False),
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    _mark_ignored_role_changed(role)

        if updates.get("ignored_tts_role_enabled") is True and not int(updates.get("ignored_tts_role_id") or self.current_ignored_role_id or 0):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Cargo obrigatório", "Escolha um cargo antes de ligar o cargo ignorado.", ok=False),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        history_label = "as regras do TTS"
        history_value = "regras atualizadas" if parts else "sem alterações"
        if len(parts) == 1:
            only = str(parts[0])
            if only.startswith("autor antes da frase"):
                history_label = "o autor antes da frase"
                history_value = "desligado" if "desligado" in only else "ligado"
            elif only.startswith("cargo ignorado"):
                history_label = "o cargo ignorado"
                history_value = only.replace("cargo ignorado", "", 1).strip() or "atualizado"

        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            history_label=history_label,
            history_value=history_value,
            success_title="Regras atualizadas",
            success_description="\n".join(f"• {part}" for part in parts) if parts else "Nada mudou.",
        )


async def _send_settings_modal_with_fallback(interaction: discord.Interaction, guided_factory, fallback_factory, *, context: str) -> None:
    try:
        await interaction.response.send_modal(guided_factory())
        return
    except Exception as e:
        print(f"[tts_modal] modal guiado falhou em {context}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        if interaction.response.is_done():
            try:
                await interaction.followup.send("Não consegui abrir esse formulário agora.", ephemeral=True)
            except Exception:
                pass
            return
    try:
        await interaction.response.send_modal(fallback_factory())
    except Exception as e:
        print(f"[tts_modal] fallback também falhou em {context}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.response.send_message("Não consegui abrir esse formulário agora.", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send("Não consegui abrir esse formulário agora.", ephemeral=True)
            except Exception:
                pass



async def _reset_public_launcher_select(interaction: discord.Interaction, panel) -> None:
    """Re-renderiza o launcher público para limpar a opção marcada no select.

    No mobile do Discord, depois de escolher uma opção, o select pode ficar preso
    visualmente no último valor. Como abrir modal consome a resposta da interação,
    esse reset precisa ser feito com message.edit normal, sem usar interaction.response.
    """
    message = getattr(interaction, "message", None)
    guild = getattr(interaction, "guild", None)
    cog = getattr(panel, "cog", None)
    if message is None or guild is None or cog is None:
        return
    message_id = int(getattr(message, "id", 0) or 0)
    state = getattr(cog, "_public_panel_states", {}).get(message_id, {}) if message_id else {}
    if state.get("panel_kind") != "launcher":
        return

    try:
        history_text = cog._format_history_entries(
            cog._get_public_panel_history(message_id),
            viewer_user_id=None,
            message_id=message_id,
        ) or getattr(panel, "history_text", "") or "• Nada recente."
        view = cog._build_public_tts_launcher_view(guild.id, timeout=300, history_text=history_text)
        view.message = message
        await cog._edit_panel_message_payload(
            message,
            embed=cog._make_embed("TTS", "Cada prefixo escolhe um modo de voz.", ok=True),
            view=view,
        )
    except Exception as e:
        print(f"[tts_panel] falha ao limpar select do launcher: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)


class TTSPublicLauncherButton(discord.ui.Button):
    def __init__(self, *, action: str, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.action = str(action)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None or interaction.guild is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        await panel._open_action(interaction, self.action)


_TTS_LAYOUT_VIEW_CLS = getattr(discord.ui, "LayoutView", discord.ui.View)


class _BaseTTSLayoutView(_TTS_LAYOUT_VIEW_CLS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        requested_timeout = max(1.0, float(timeout or TTS_PANEL_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, TTS_PANEL_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + requested_timeout

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                message = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                message = (
                    "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
                    "Para continuar, abra o comando novamente e gere um painel novo."
                )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[tts_panel_error] falha ao responder erro: {e!r}")

    async def on_timeout(self) -> None:
        pass


class TTSPublicLauncherView(_BaseTTSLayoutView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 300, history_text: str = ""):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "launcher"
        self.history_text = str(history_text or "").strip()
        self._rebuild_items()

    def is_components_v2_panel(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
        )

    def _spoken_name_enabled(self) -> bool:
        try:
            db = self.cog._get_db()
            defaults = db.get_guild_tts_defaults(self.guild_id) if db is not None else {}
            return bool((defaults or {}).get("announce_author", False))
        except Exception as e:
            print(f"[tts_panel] falha ao verificar apelido falado do launcher: {e!r}")
            return False

    @staticmethod
    def _separator():
        try:
            return discord.ui.Separator(visible=True)
        except TypeError:
            return discord.ui.Separator()

    @staticmethod
    def _button_row(button: discord.ui.Button):
        row = discord.ui.ActionRow()
        row.add_item(button)
        return row

    def _intro_text(self) -> str:
        return "### TTS\n**Como funciona**\nCada prefixo escolhe um modo de voz."

    def _history_text(self) -> str:
        return f"**Últimas alterações**\n{self.history_text or '• Nada recente.'}"

    def _rebuild_items(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        edge_button = TTSPublicLauncherButton(action="edge", label="Configurar Edge", emoji="🔊")
        gtts_button = TTSPublicLauncherButton(action="gtts", label="Configurar gTTS", emoji="🔤")
        spoken_button = TTSPublicLauncherButton(action="spoken_name", label="Alterar apelido", emoji="🪪")
        spoken_name_enabled = self._spoken_name_enabled()

        if self.is_components_v2_panel():
            container = discord.ui.Container(
                discord.ui.TextDisplay(self._intro_text()),
                self._separator(),
                discord.ui.TextDisplay("**Edge**\nVoz natural. Use: `,texto`"),
                self._button_row(edge_button),
                self._separator(),
                discord.ui.TextDisplay("**gTTS**\nVoz simples. Use: `.texto`"),
                self._button_row(gtts_button),
                accent_color=discord.Color.blurple(),
            )
            if spoken_name_enabled:
                container.add_item(self._separator())
                container.add_item(discord.ui.TextDisplay(
                    "**Apelido falado**\nEscolha o nome anunciado antes das suas mensagens."
                ))
                container.add_item(self._button_row(spoken_button))
            container.add_item(self._separator())
            container.add_item(discord.ui.TextDisplay(self._history_text()))
            self.add_item(container)
            return

        self.add_item(edge_button)
        self.add_item(gtts_button)
        if spoken_name_enabled:
            self.add_item(spoken_button)

    async def _open_action(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Esse painel só pode ser usado dentro de um servidor.", ephemeral=True)
            return

        target_name = self.cog._member_panel_name(interaction.user)
        panel_message = getattr(interaction, "message", None)
        if action == "edge":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: EdgeSettingsModal(
                    self.cog,
                    panel_message,
                    server=False,
                    target_user_id=interaction.user.id,
                    target_user_name=target_name,
                ),
                lambda: EdgeSettingsModal(
                    self.cog,
                    panel_message,
                    server=False,
                    target_user_id=interaction.user.id,
                    target_user_name=target_name,
                    force_text_fallback=True,
                ),
                context="public-edge",
            )
            return

        if action == "gtts":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GTTSSettingsModal(
                    self.cog,
                    panel_message,
                    server=False,
                    target_user_id=interaction.user.id,
                    target_user_name=target_name,
                ),
                lambda: GTTSSettingsModal(
                    self.cog,
                    panel_message,
                    server=False,
                    target_user_id=interaction.user.id,
                    target_user_name=target_name,
                    force_text_fallback=True,
                ),
                context="public-gtts",
            )
            return

        if action == "spoken_name" and self._spoken_name_enabled():
            current_value = self.cog._get_saved_spoken_name(interaction.guild.id, interaction.user.id)
            await interaction.response.send_modal(
                SpokenNameModal(
                    self.cog,
                    panel_message,
                    target_user_id=interaction.user.id,
                    target_user_name=target_name,
                    current_value=current_value,
                )
            )
            return

        await interaction.response.send_message("Opção indisponível.", ephemeral=True)

    async def _open_my_tts(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        target_name = self.cog._member_panel_name(interaction.user)
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            target_user_name=target_name,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=interaction.user.id,
            target_user_name=target_name,
        )
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    async def _open_server_tts(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor.", ok=False),
                ephemeral=True,
            )
            return
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    async def _open_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Ajuda do TTS",
                "Edge e gTTS são modos de voz. O prefixo é só o símbolo digitado antes da frase.\n\n"
                "Exemplos:\n"
                "• `,bom dia` usa Edge.\n"
                "• `.bom dia` usa gTTS.",
                ok=True,
            ),
            ephemeral=True,
        )


class TTSReadingQuickView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.source_panel_message = source_panel_message
        self.add_item(SpeedSelect(cog, server=server))
        self.add_item(PitchSelect(cog, server=server))
        for item in self.children:
            try:
                item.source_panel_message = source_panel_message
                item.target_user_id = target_user_id
                item.target_user_name = target_user_name
            except Exception:
                pass

    async def send(self, interaction: discord.Interaction):
        embed = self.cog._make_embed(
            "Leitura",
            "Muda velocidade e tom do modo Edge. O painel principal será atualizado depois de salvar.",
            ok=True,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)


class TTSModeActionsView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        mode: str,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.mode = str(mode or "edge")
        self.server = server
        self.source_panel_message = source_panel_message
        self.panel_kind = "server" if server else "user"
        self._build_buttons()

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: int | None = None) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        async def wrapped(interaction: discord.Interaction):
            result = callback(interaction)
            if inspect.isawaitable(result):
                await result
        button.callback = wrapped
        return button

    def _build_buttons(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass
        self.add_item(TTSModeActionSelect(self.mode))
        self.add_item(self._make_button("Voltar", self._back_to_main_panel, emoji="⬅️", row=1))

    def _prefix_for_mode(self) -> str:
        # Texto curto de orientação. O painel principal mostra os prefixos reais
        # vindos do banco; aqui usamos os padrões para evitar consulta assíncrona
        # dentro de uma função de renderização simples.
        if self.mode == "atts" or self.mode == "android_native":
            return "%"
        if self.mode == "edge":
            return ","
        return "."

    def _mode_title_description(self) -> tuple[str, str]:
        prefix = self._prefix_for_mode()
        target = "do servidor" if self.server else "seus"
        if self.mode == "atts" or self.mode == "android_native":
            return "ATTS", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."
        if self.mode == "edge":
            return "Edge", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."
        if self.mode == "gtts":
            return "gTTS", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."
        return "gTTS", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."

    async def send(self, interaction: discord.Interaction):
        title, description = self._mode_title_description()
        embed = self.cog._make_embed(title, description, ok=True)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _open_edge_voice(self, interaction: discord.Interaction):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Voz Edge",
            "Muda a voz usada pelo modo Edge.",
            VoiceRegionSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    async def _open_edge_reading(self, interaction: discord.Interaction):
        await TTSReadingQuickView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    async def _open_gtts_language(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=LanguageHelpView(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=self.source_panel_message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )




    async def _back_to_main_panel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        target_id = int(self.target_user_id or interaction.user.id)
        target_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            target_id if not self.server else interaction.user.id,
            server=self.server,
            panel_kind="server" if self.server else "user",
            target_user_name=target_name if not self.server else None,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            self._target_owner(interaction),
            interaction.guild.id,
            server=self.server,
            target_user_id=None if self.server else target_id,
            target_user_name=None if self.server else target_name,
        )
        content, edit_embed, edit_view = self.cog._prepare_panel_payload(embed=embed, view=view)
        await interaction.response.edit_message(content=content, embed=edit_embed, view=edit_view)
        view.message = getattr(interaction, "message", None)






class PrefixTargetSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice"):
        self.cog = cog
        options = [
            discord.SelectOption(label="Bot", description="Símbolo usado nos comandos do bot. Exemplo: _panel", value="bot", emoji="🤖"),
            discord.SelectOption(label="ATTS", description="Símbolo antes da frase para usar ATTS. Exemplo: %bom dia", value="atts", emoji="📱"),
            discord.SelectOption(label="Kasane Teto", description="Símbolo antes da frase para usar a Teto. Exemplo: 'bom dia", value="teto", emoji="🥖"),
            discord.SelectOption(label="gTTS", description="Símbolo antes da frase para usar gTTS. Exemplo: .bom dia", value="gtts", emoji="🔤"),
            discord.SelectOption(label="Edge", description="Símbolo antes da frase para usar Edge. Exemplo: ,bom dia", value="edge", emoji="🔊"),
        ]
        super().__init__(placeholder="Escolha qual prefixo alterar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None) or getattr(interaction, "message", None)
        owner_id = getattr(getattr(self, "view", None), "owner_id", interaction.user.id)
        guild_id = getattr(getattr(self, "view", None), "guild_id", interaction.guild.id if interaction.guild else 0)
        value = self.values[0]
        if value == "bot":
            await interaction.response.send_modal(BotPrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        elif value == "atts":
            await interaction.response.send_modal(ATTSPrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        elif value == "teto":
            await interaction.response.send_modal(TetoPrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        elif value == "edge":
            await interaction.response.send_modal(EdgePrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        else:
            await interaction.response.send_modal(GTTSPrefixModal(self.cog, source_panel_message, owner_id, guild_id))


class TTSAdvancedActionsView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.source_panel_message = source_panel_message
        self.panel_kind = "server" if server else "user"
        if not server:
            self.remove_item(self.ignored_role_button)
            # O apelido já fica no painel principal; aqui deixam só ajustes técnicos.
            self.remove_item(self.spoken_name_button)
        else:
            self.remove_item(self.spoken_name_button)

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Idioma gTTS", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def gtts_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=LanguageHelpView(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=self.source_panel_message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )




    @discord.ui.button(label="Cargo ignorado", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def ignored_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = IgnoreRoleConfigView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            source_panel_message=self.source_panel_message,
        )
        await view.send(interaction)


    @discord.ui.button(label="Modo de TTS", style=discord.ButtonStyle.secondary, emoji="🎛️", row=2)
    async def mode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Modo de TTS",
            "Escolhe o motor padrão usado por comandos antigos. Os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem.",
            ModeSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    @discord.ui.button(label="Apelido", style=discord.ButtonStyle.secondary, emoji="🪪", row=1)
    async def spoken_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_saved_spoken_name(self.guild_id, current_target_user_id)
        await interaction.response.send_modal(
            SpokenNameModal(
                self.cog,
                self.source_panel_message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=current_value,
            )
        )


class TTSMainPanelSelect(discord.ui.Select):
    def __init__(self, *, server: bool):
        self.server = bool(server)
        if self.server:
            options = [
                discord.SelectOption(label="Prefixos", description="Símbolos do bot, ATTS, Teto, Edge e gTTS", value="prefixes", emoji="⌨️"),
                discord.SelectOption(label="ATTS", description="Android TTS padrão do servidor", value="atts", emoji="📱"),
                discord.SelectOption(label="Edge", description="Idioma, voz e leitura Edge padrão do servidor", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Idioma gTTS padrão do servidor", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Regras", description="Autor antes da frase e cargo ignorado", value="rules", emoji="☑️"),
            ]
            placeholder = "Escolha o ajuste do servidor"
        else:
            options = [
                discord.SelectOption(label="ATTS", description="Android nativo: idioma, voz, velocidade e tom", value="atts", emoji="📱"),
                discord.SelectOption(label="Edge", description="Voz natural: idioma, voz, velocidade e tom", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Voz simples: idioma usado no gTTS", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
            ]
            placeholder = "Escolha o que editar"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        value = self.values[0]
        if value == "atts":
            await panel._open_atts_panel(interaction)
        elif value == "edge":
            await panel._open_edge_panel(interaction)
        elif value == "gtts":
            await panel._open_gtts_panel(interaction)
        elif value == "spoken_name":
            await panel._open_spoken_name_modal(interaction)
        elif value == "prefixes":
            await panel._open_prefixes_panel(interaction)
        elif value == "rules":
            await panel._open_rules_panel(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)


class TTSModeActionSelect(discord.ui.Select):
    def __init__(self, mode: str):
        self.mode = str(mode or "edge")
        if self.mode == "atts" or self.mode == "android_native":
            options = [
                discord.SelectOption(label="Configurar ATTS", description="Idioma, voz, velocidade e tom", value="atts_settings", emoji="📱"),
            ]
            placeholder = "Editar ATTS"
        elif self.mode == "edge":
            options = [
                discord.SelectOption(label="Voz Edge", description="Escolhe a voz usada no prefixo Edge", value="edge_voice", emoji="🎙️"),
                discord.SelectOption(label="Leitura Edge", description="Velocidade e tom do Edge", value="edge_reading", emoji="🎚️"),
            ]
            placeholder = "Editar Edge"
        elif self.mode == "gtts":
            options = [
                discord.SelectOption(label="Idioma gTTS", description="Idioma usado no prefixo gTTS", value="gtts_language", emoji="🌐"),
            ]
            placeholder = "Editar gTTS"
        else:
            options = [
                discord.SelectOption(label="Idioma gTTS", description="Idioma usado no prefixo gTTS", value="gtts_language", emoji="🌐"),
            ]
            placeholder = "Editar gTTS"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        value = self.values[0]
        if value == "atts_settings":
            await _send_atts_settings_modal(
                interaction,
                panel.cog,
                panel.source_panel_message,
                server=panel.server,
                target_user_id=panel.target_user_id,
                target_user_name=panel.target_user_name,
                context="mode-atts",
            )
        elif value == "edge_voice":
            await panel._open_edge_voice(interaction)
        elif value == "edge_reading":
            await panel._open_edge_reading(interaction)
        elif value == "gtts_language":
            await panel._open_gtts_language(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)


class TTSMainPanelView(_BaseTTSLayoutView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.panel_kind = "server" if server else "user"
        self._panel_embed: discord.Embed | None = None
        self._fallback_buttons_ready = False
        self._rebuild_items()

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def is_components_v2_panel(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
        )

    def set_panel_embed(self, embed: discord.Embed | None) -> None:
        self._panel_embed = embed
        self._rebuild_items()

    def _panel_text(self) -> str:
        if self._panel_embed is None:
            return "### TTS do servidor\nCarregando painel." if self.server else "### TTS\nCarregando painel."
        try:
            return build_settings_panel_text_from_embed(self._panel_embed, server=self.server)
        except Exception as e:
            print(f"[tts_panel] falha ao renderizar painel v2: {e!r}")
            return str(getattr(self._panel_embed, "description", "") or "Painel de TTS")[:4000]

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style)
        async def wrapped(interaction: discord.Interaction):
            result = callback(interaction)
            if inspect.isawaitable(result):
                await result
        button.callback = wrapped
        return button

    def _make_action_row(self, *buttons: discord.ui.Button):
        if self.is_components_v2_panel():
            row = discord.ui.ActionRow()
            for button in buttons:
                row.add_item(button)
            return row
        return list(buttons)

    def _add_control_row(self, container, *buttons: discord.ui.Button) -> None:
        row = self._make_action_row(*buttons)
        if self.is_components_v2_panel():
            container.add_item(row)
        else:
            for button in row:
                self.add_item(button)

    def _spoken_name_enabled(self) -> bool:
        if self.server:
            return False
        try:
            db = self.cog._get_db()
            defaults = db.get_guild_tts_defaults(self.guild_id) if db is not None else {}
            return bool((defaults or {}).get("announce_author", False))
        except Exception as e:
            print(f"[tts_panel] falha ao verificar apelido falado do painel: {e!r}")
            return False

    def _panel_buttons(self) -> list[discord.ui.Button]:
        if self.server:
            return [
                self._make_button("Configurar prefixos", self._open_prefixes_panel, emoji="⌨️"),
                self._make_button("Configurar Edge", self._open_edge_panel, emoji="🔊"),
                self._make_button("Configurar gTTS", self._open_gtts_panel, emoji="🔤"),
                self._make_button("Configurar regras", self._open_rules_panel, emoji="☑️"),
            ]

        buttons = [
            self._make_button("Configurar Edge", self._open_edge_panel, emoji="🔊"),
            self._make_button("Configurar gTTS", self._open_gtts_panel, emoji="🔤"),
        ]
        if self._spoken_name_enabled():
            buttons.append(self._make_button("Alterar apelido", self._open_spoken_name_modal, emoji="🪪"))
        return buttons

    def _rebuild_items(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        buttons = self._panel_buttons()
        if self.is_components_v2_panel():
            container = discord.ui.Container(
                discord.ui.TextDisplay(self._panel_text()),
                accent_color=discord.Color.blurple(),
            )
            try:
                container.add_item(discord.ui.Separator(visible=True))
            except TypeError:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("**Ajustes**\nEscolha o que deseja configurar."))
            for button in buttons:
                self._add_control_row(container, button)
            self.add_item(container)
            return

        # Fallback se a lib em produção ainda não tiver LayoutView/Components V2.
        for button in buttons:
            self.add_item(button)

    async def _open_mode_panel(self, interaction: discord.Interaction, mode: str):
        print(f"[tts_panel] mode_select | mode={mode} user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        panel_message = getattr(interaction, "message", None)
        target_user_id = self.target_user_id
        target_user_name = self.target_user_name
        if not self.server and target_user_id is None:
            target_user_id = interaction.user.id
            target_user_name = self.cog._member_panel_name(interaction.user)

        if mode == "atts" or mode == "android_native":
            await _send_atts_settings_modal(
                interaction,
                self.cog,
                panel_message,
                server=self.server,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                context="panel-atts",
            )
        elif mode == "edge":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: EdgeSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: EdgeSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-edge",
            )
        elif mode == "gtts":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-gtts",
            )
        else:
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-gtts",
            )


    async def _open_atts_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "atts")

    async def _open_edge_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "edge")

    async def _open_gtts_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "gtts")


    async def _open_voice_panel(self, interaction: discord.Interaction):
        print(f"[tts_panel] voice_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Voz",
            "Escolha a região e depois a voz. Os nomes técnicos ficam só dentro desta lista.",
            VoiceRegionSelect(self.cog, server=self.server),
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    async def _open_reading_panel(self, interaction: discord.Interaction):
        print(f"[tts_panel] reading_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await TTSReadingQuickView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    async def _open_spoken_name_modal(self, interaction: discord.Interaction):
        print(f"[tts_panel] spoken_name_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_saved_spoken_name(self.guild_id, current_target_user_id)
        await interaction.response.send_modal(
            SpokenNameModal(
                self.cog,
                interaction.message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=current_value,
            )
        )

    async def _open_prefixes_panel(self, interaction: discord.Interaction):
        if not self.server:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Indisponível", "Prefixos são ajustes do servidor.", ok=False),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ServerPrefixesModal(self.cog, getattr(interaction, "message", None)))

    async def _open_rules_panel(self, interaction: discord.Interaction):
        if not self.server:
            return await self._open_advanced_panel(interaction)
        await _send_settings_modal_with_fallback(
            interaction,
            lambda: TTSServerRulesModal(self.cog, getattr(interaction, "message", None)),
            lambda: TTSServerRulesModal(self.cog, getattr(interaction, "message", None), force_text_fallback=True),
            context="server-rules",
        )

    async def _open_advanced_panel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Opção removida",
                "Esse painel foi simplificado. Use Edge, gTTS, Apelido ou o comando separado do TTS do servidor.",
                ok=True,
            ),
            ephemeral=True,
        )


class TTSStatusView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.panel_kind = "status"

    def attach_message(self, message: discord.Message | None) -> None:
        self.message = message
        self.cog._register_status_view(self)

    async def refresh_from_config_change(self) -> None:
        if self.message is None or self.is_finished():
            return
        try:
            guild = self.cog.bot.get_guild(self.guild_id)
            if guild is None:
                self.cog._unregister_status_view(self)
                return
            target_user_id = int(self.target_user_id or self.owner_id or 0)
            member = guild.get_member(target_user_id) if target_user_id else None
            target_user_name = str(self.target_user_name or self.cog._member_panel_name(member))
            refreshed = await self.cog._build_status_embed(
                self.guild_id,
                target_user_id,
                viewer_user_id=self.owner_id,
                target_user_name=target_user_name,
                public=False,
            )
            await self.message.edit(embed=refreshed, view=self)
        except discord.NotFound:
            self.cog._unregister_status_view(self)
            self.stop()
        except Exception as e:
            print(f"[tts_status_refresh] falha ao atualizar status: {e!r}")

    async def on_timeout(self) -> None:
        self.cog._unregister_status_view(self)
        await super().on_timeout()

    @discord.ui.button(label="Abrir painel", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0)
    async def open_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            target_user_id,
            server=False,
            panel_kind="user",
            target_user_name=target_user_name,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    @discord.ui.button(label="Resetar para o padrão do servidor", style=discord.ButtonStyle.danger, emoji="♻️", row=0)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self.cog._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as suas configurações.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        history_entry = f"{self.cog._panel_actor_name(interaction)} resetou as próprias configurações de TTS para os padrões do servidor"
        await self.cog._reset_user_tts_and_refresh(interaction.guild.id, target_user_id, history_entry=history_entry)

        refreshed = await self.cog._build_status_embed(
            interaction.guild.id,
            target_user_id,
            viewer_user_id=interaction.user.id,
            target_user_name=target_user_name,
            public=False,
        )
        await interaction.response.edit_message(embed=refreshed, view=self)
        await interaction.followup.send(
            embed=self.cog._make_embed("Configurações resetadas", f"As suas configurações de TTS agora seguem os padrões do servidor.", ok=True),
            ephemeral=True,
        )

class TTSTogglePanelView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "toggle"

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Auto leave", style=discord.ButtonStyle.secondary, emoji="⏏️", row=1)
    async def auto_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Auto leave", "Escolha se o bot deve sair da call quando ficar sozinho ou só com bots.", ToggleSelect(self.cog, "auto_leave")).send(interaction)
