from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable

import discord
from discord.ext import commands

from utility.interaction_safety import safe_defer_interaction, safe_send_interaction_message

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - runtime guard only
    Image = None
    ImageDraw = None
    ImageFont = None


logger = logging.getLogger(__name__)

COLOR_PANEL_TIMEOUT = 600.0
COLOR_COMMAND_COOLDOWN = 20.0
COLOR_COMMAND_CLEANUP_DELAY = 6.0
COLOR_BLOCK_SIZE = 10
COLOR_BLOCK_COUNT = 3
COLOR_MAX_MESSAGES = 3
COLOR_PANEL_VARIABLES = [
    "{membro}",
    "{membro_nome}",
    "{membro_id}",
    "{numero}",
    "{cor_nome}",
    "{cor_adicionada}",
    "{cor_removida}",
    "{cargo}",
    "{cargo_nome}",
    "{servidor}",
]

DEFAULT_SLOTS: list[dict[str, Any]] = [
    {"number": 1, "name": "Vermelho escuro", "text_hex": "#b11212", "role_hex": "#8b0000"},
    {"number": 2, "name": "Amarelo escuro", "text_hex": "#c9a31a", "role_hex": "#b8860b"},
    {"number": 3, "name": "Verde escuro", "text_hex": "#0b5d30", "role_hex": "#006400"},
    {"number": 4, "name": "Azul escuro", "text_hex": "#1737d8", "role_hex": "#00008b"},
    {"number": 5, "name": "Rosa escuro", "text_hex": "#d61ea6", "role_hex": "#c71585"},
    {"number": 6, "name": "Roxo escuro", "text_hex": "#9a0ec7", "role_hex": "#800080"},
    {"number": 7, "name": "Laranja escuro", "text_hex": "#d98900", "role_hex": "#ff8c00"},
    {"number": 8, "name": "Bege escuro", "text_hex": "#b96d43", "role_hex": "#a0522d"},
    {"number": 9, "name": "Ciano escuro", "text_hex": "#008f98", "role_hex": "#008b8b"},
    {"number": 10, "name": "Preto", "text_hex": "#000000", "role_hex": "#1f1f1f"},
    {"number": 11, "name": "Vermelho", "text_hex": "#ff1b1b", "role_hex": "#ff0000"},
    {"number": 12, "name": "Amarelo", "text_hex": "#ffec1a", "role_hex": "#ffd700"},
    {"number": 13, "name": "Verde", "text_hex": "#11b611", "role_hex": "#00ff00"},
    {"number": 14, "name": "Azul", "text_hex": "#0e2fff", "role_hex": "#1e90ff"},
    {"number": 15, "name": "Rosa", "text_hex": "#ff62c3", "role_hex": "#ff69b4"},
    {"number": 16, "name": "Roxo", "text_hex": "#c020ff", "role_hex": "#9370db"},
    {"number": 17, "name": "Laranja", "text_hex": "#ffad13", "role_hex": "#ffa500"},
    {"number": 18, "name": "Bege", "text_hex": "#d6b694", "role_hex": "#f5deb3"},
    {"number": 19, "name": "Ciano", "text_hex": "#00ecff", "role_hex": "#00ffff"},
    {"number": 20, "name": "Cinza", "text_hex": "#8f8f8f", "role_hex": "#808080"},
    {"number": 21, "name": "Vermelho claro", "text_hex": "#ff8b8b", "role_hex": "#ff7f7f"},
    {"number": 22, "name": "Amarelo claro", "text_hex": "#fff38f", "role_hex": "#fff68f"},
    {"number": 23, "name": "Verde claro", "text_hex": "#9cff9c", "role_hex": "#90ee90"},
    {"number": 24, "name": "Azul claro", "text_hex": "#a6c7ff", "role_hex": "#87cefa"},
    {"number": 25, "name": "Rosa claro", "text_hex": "#ffb6d9", "role_hex": "#ffb6c1"},
    {"number": 26, "name": "Roxo claro", "text_hex": "#d6a5ff", "role_hex": "#d8bfd8"},
    {"number": 27, "name": "Laranja claro", "text_hex": "#ffd199", "role_hex": "#ffcc99"},
    {"number": 28, "name": "Bege claro", "text_hex": "#ffe8d0", "role_hex": "#f5f5dc"},
    {"number": 29, "name": "Ciano claro", "text_hex": "#d6ffff", "role_hex": "#e0ffff"},
    {"number": 30, "name": "Branco", "text_hex": "#ffffff", "role_hex": "#ffffff"},
]

_DEFAULT_MESSAGE = {"title": "", "subtitle": "", "footer": ""}

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "channel_id": 0,
    "message_ids": [],
    "panel_count": 3,
    "panel_layout": [
        {"id": f"panel-{index}", "slots": list(range((index - 1) * COLOR_BLOCK_SIZE + 1, index * COLOR_BLOCK_SIZE + 1))}
        for index in range(1, COLOR_BLOCK_COUNT + 1)
    ],
    "messages": {str(index): dict(_DEFAULT_MESSAGE) for index in range(1, COLOR_MAX_MESSAGES + 1)},
    "templates": {
        "apply": "cor {cor_adicionada} aplicada.",
        "remove": "cor {cor_removida} removida.",
        "switch": "cor alterada: {cor_removida} → {cor_adicionada}.",
        "no_role": "Essa cor ainda não está configurada.",
        "hierarchy": "não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",
        "missing_panel": "Esse painel de cores não é mais o oficial deste servidor.",
    },
    "slots": {str(item["number"]): {**item, "role_id": 0, "role_name": item["name"], "managed": False} for item in DEFAULT_SLOTS},
}


def _deepcopy_default_config() -> dict[str, Any]:
    return deepcopy(_DEFAULT_CONFIG)


def _normalize_panel_layout(raw: Any, *, fallback_count: int = COLOR_BLOCK_COUNT) -> list[dict[str, Any]]:
    source = raw if isinstance(raw, list) else deepcopy(_DEFAULT_CONFIG["panel_layout"][: max(1, min(COLOR_MAX_MESSAGES, int(fallback_count or COLOR_BLOCK_COUNT)))])
    used: set[int] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(source[:COLOR_MAX_MESSAGES], start=1):
        panel = dict(item or {}) if isinstance(item, dict) else {}
        normalized_slots: list[int] = []
        for raw_slot in panel.get("slots") or []:
            try:
                slot_number = int(raw_slot)
            except Exception:
                continue
            if slot_number < 1 or slot_number > 30 or slot_number in used:
                continue
            used.add(slot_number)
            normalized_slots.append(slot_number)
            if len(normalized_slots) >= COLOR_BLOCK_SIZE:
                break
        if not normalized_slots:
            continue
        panel_id = str(panel.get("id") or f"panel-{index}").strip()[:80]
        if not panel_id or any(existing["id"] == panel_id for existing in result):
            panel_id = f"panel-{index}"
        suffix = 2
        while any(existing["id"] == panel_id for existing in result):
            panel_id = f"panel-{index}-{suffix}"
            suffix += 1
        result.append({"id": panel_id, "slots": normalized_slots})
    return result or [{"id": "panel-1", "slots": [1]}]


def _next_unused_slot(panel_layout: list[dict[str, Any]]) -> int | None:
    used = {int(slot) for panel in panel_layout for slot in (panel.get("slots") or []) if str(slot).isdigit()}
    return next((number for number in range(1, 31) if number not in used), None)


def _panel_slots(config: dict[str, Any], block_index: int) -> list[int]:
    layout = _normalize_panel_layout(config.get("panel_layout"), fallback_count=int(config.get("panel_count") or COLOR_BLOCK_COUNT))
    if not (1 <= int(block_index) <= len(layout)):
        return []
    return [int(number) for number in layout[int(block_index) - 1].get("slots") or []]


def _clean_hex(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return fallback
    return raw.lower()


def _font(size: int, *, bold: bool = True, kind: str = "math"):
    if ImageFont is None:
        raise RuntimeError("Pillow não está disponível.")
    if kind == "mono":
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf",
            "/usr/share/fonts/opentype/stix-word/STIXMath-Regular.otf",
            "/usr/share/fonts/opentype/asana-math/Asana-Math.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _unicode_math_transform(text: str, *, style: str) -> str:
    value = str(text or "")
    transformed: list[str] = []
    for char in value:
        code = ord(char)
        if "A" <= char <= "Z":
            if style == "monospace":
                transformed.append(chr(0x1D670 + (code - ord("A"))))
                continue
            if style == "sans_bold":
                transformed.append(chr(0x1D5D4 + (code - ord("A"))))
                continue
        if "a" <= char <= "z":
            if style == "monospace":
                transformed.append(chr(0x1D68A + (code - ord("a"))))
                continue
            if style == "sans_bold":
                transformed.append(chr(0x1D5EE + (code - ord("a"))))
                continue
        if "0" <= char <= "9":
            if style == "monospace":
                transformed.append(chr(0x1D7F6 + (code - ord("0"))))
                continue
            if style == "sans_bold":
                transformed.append(chr(0x1D7EC + (code - ord("0"))))
                continue
        transformed.append(char)
    return "".join(transformed)


def _math_monospace(text: str) -> str:
    return _unicode_math_transform(text, style="monospace")


def _math_sans_bold(text: str) -> str:
    return _unicode_math_transform(text, style="sans_bold")


def _chunk_block(block_index: int) -> tuple[int, int]:
    start = (block_index - 1) * COLOR_BLOCK_SIZE + 1
    end = start + COLOR_BLOCK_SIZE - 1
    return start, end


def _block_title(block_index: int) -> str:
    start, end = _chunk_block(block_index)
    return f"{start}–{end}"


def _default_slot_payload(slot_number: int) -> dict[str, Any]:
    default = next((item for item in DEFAULT_SLOTS if item["number"] == int(slot_number)), None)
    if default is None:
        return {
            "number": int(slot_number),
            "name": f"Cor {slot_number}",
            "text_hex": "#ffffff",
            "role_hex": "#ffffff",
            "role_id": 0,
            "role_name": f"Cor {slot_number}",
            "managed": False,
        }
    return {**default, "role_id": 0, "role_name": str(default["name"]), "managed": False}




_LEGACY_TEMPLATE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "apply": ("{membro}, a cor {cor_adicionada} foi aplicada.",),
    "remove": ("{membro}, a cor {cor_removida} foi removida.",),
    "switch": ("{membro}, {cor_removida} foi removida e {cor_adicionada} foi aplicada.",),
    "hierarchy": ("Não consegui aplicar {cor_nome} por causa da hierarquia de cargos.",),
}


def _legacy_slot_payload(slot_number: int) -> dict[str, Any]:
    default = _default_slot_payload(slot_number)
    if int(slot_number) != 10:
        return default
    legacy = dict(default)
    legacy["name"] = "Preto escuro"
    legacy["text_hex"] = "#4a4a4a"
    legacy["role_hex"] = "#1f1f1f"
    legacy["role_name"] = "Preto escuro"
    return legacy


def _normalize_color_name(value: str | None) -> str:
    return str(value or "").strip().lower()


def _is_default_black_slot(slot_number: int, slot: dict[str, Any]) -> bool:
    if int(slot_number) != 10:
        return False
    default = _default_slot_payload(10)
    return (
        str(slot.get("name") or "") == str(default["name"])
        and _clean_hex(str(slot.get("text_hex") or ""), default["text_hex"]) == default["text_hex"]
    )


def _cleared_slot_payload(slot_number: int) -> dict[str, Any]:
    # Ao reutilizar uma posição removida, volte ao preset correspondente.
    # O branco genérico era apenas placeholder e deixava a imagem inteira sem cor.
    return _default_slot_payload(slot_number)


def _slot_payload_signature(slot: dict[str, Any], *, fallback_slot_number: int) -> dict[str, Any]:
    default = _default_slot_payload(fallback_slot_number)
    return {
        "name": str(slot.get("name") or default["name"]),
        "text_hex": _clean_hex(str(slot.get("text_hex") or ""), default["text_hex"]),
        "role_hex": _clean_hex(str(slot.get("role_hex") or ""), default["role_hex"]),
        "role_id": int(slot.get("role_id") or 0),
        "role_name": str(slot.get("role_name") or slot.get("name") or default["role_name"]),
        "managed": bool(slot.get("managed", False)),
    }


def _block_looks_like_default_source(slots: dict[str, Any], block_index: int, source_block_index: int) -> bool:
    target_start, target_end = _chunk_block(block_index)
    source_start, source_end = _chunk_block(source_block_index)
    if (target_end - target_start) != (source_end - source_start):
        return False
    for offset, slot_number in enumerate(range(target_start, target_end + 1)):
        current = dict(slots.get(str(slot_number), {}) or _default_slot_payload(slot_number))
        source_default = _default_slot_payload(source_start + offset)
        comparable_current = _slot_payload_signature(current, fallback_slot_number=slot_number)
        comparable_source = _slot_payload_signature(source_default, fallback_slot_number=source_start + offset)
        if comparable_current != comparable_source:
            return False
    return True


def _message_supports_slots(message_index: int) -> bool:
    return 1 <= int(message_index) <= COLOR_BLOCK_COUNT


def _message_label(message_index: int) -> str:
    return f"Painel {int(message_index)}"


def _compose_block_text(block_cfg: dict[str, Any]) -> str | None:
    lines: list[str] = []
    title = str(block_cfg.get("title") or "").strip()
    subtitle = str(block_cfg.get("subtitle") or "").strip()
    footer = str(block_cfg.get("footer") or "").strip()
    if title:
        lines.append(title)
    if subtitle:
        if lines:
            lines.append("")
        lines.append(subtitle)
    if footer:
        if lines:
            lines.append("")
        lines.append(footer)
    text = "\n".join(lines).strip()
    return text or None


class _ColorContentEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(title=f"Editar conteúdo • {_message_label(message_index)}")
        self.view_ref = view
        self.message_index = int(message_index)
        cfg = self.view_ref.cog._get_message_block_config(self.view_ref.guild_id, self.message_index)
        self.title_input = discord.ui.TextInput(
            label="Título",
            default=str(cfg.get("title") or ""),
            required=False,
            style=discord.TextStyle.short,
            max_length=250,
        )
        self.subtitle_input = discord.ui.TextInput(
            label="Descrição",
            default=str(cfg.get("subtitle") or ""),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer",
            default=str(cfg.get("footer") or ""),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=300,
        )
        self.add_item(self.title_input)
        self.add_item(self.subtitle_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_message_block_config(
            self.view_ref.guild_id,
            self.message_index,
            title=str(self.title_input.value or "").strip(),
            subtitle=str(self.subtitle_input.value or "").strip(),
            footer=str(self.footer_input.value or "").strip(),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.message_index])
        await self.view_ref.refresh_editor_message(interaction)


class _ColorTemplatesEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(title="Editar respostas do painel")
        self.view_ref = view
        cfg = self.view_ref.cog._get_templates(self.view_ref.guild_id)
        self.apply_input = discord.ui.TextInput(label="Quando aplica cor", default=str(cfg.get("apply") or ""), style=discord.TextStyle.paragraph, max_length=300)
        self.remove_input = discord.ui.TextInput(label="Quando remove cor", default=str(cfg.get("remove") or ""), style=discord.TextStyle.paragraph, max_length=300)
        self.switch_input = discord.ui.TextInput(label="Quando troca cor", default=str(cfg.get("switch") or ""), style=discord.TextStyle.paragraph, max_length=300)
        self.add_item(self.apply_input)
        self.add_item(self.remove_input)
        self.add_item(self.switch_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_templates(
            self.view_ref.guild_id,
            apply=str(self.apply_input.value or "").strip(),
            remove=str(self.remove_input.value or "").strip(),
            switch=str(self.switch_input.value or "").strip(),
        )
        await self.view_ref.refresh_editor_message(interaction)


class _ColorSlotEditModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, slot_number: int):
        panel_slots = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        position = panel_slots.index(int(slot_number)) + 1 if int(slot_number) in panel_slots else 1
        super().__init__(title=f"Editar nome • opção {position}")
        self.view_ref = view
        self.block_index = int(block_index)
        self.slot_number = int(slot_number)
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        self.name_input = discord.ui.TextInput(
            label="Nome exibido",
            default=str(slot.get("name") or ""),
            max_length=80,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            self.slot_number,
            name=str(self.name_input.value or "").strip() or f"Cor {self.slot_number}",
        )
        await self.view_ref.cog._refresh_public_panel_messages(
            self.view_ref.guild_id,
            block_indices=[self.block_index],
        )
        await self.view_ref.refresh_editor_message(interaction)


class _ColorRoleLinkModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, slot_number: int):
        super().__init__(title=f"Vincular cargo • slot {slot_number}")
        self.view_ref = view
        self.block_index = int(block_index)
        self.slot_number = int(slot_number)
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        current_role_id = int(slot.get("role_id") or 0)
        default_value = f"<@&{current_role_id}>" if current_role_id else ""
        self.role_input = discord.ui.TextInput(
            label="Cargo existente (menção ou ID)",
            default=default_value,
            required=False,
            max_length=64,
            placeholder="Ex.: @Cargo ou 1234567890",
        )
        self.add_item(self.role_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Isso só funciona dentro de um servidor.", ephemeral=True)
            return
        raw = str(self.role_input.value or "").strip()
        if not raw:
            await interaction.response.send_message("Informe um cargo existente para vincular ao slot.", ephemeral=True)
            return
        match = re.search(r"(\d{6,})", raw)
        if not match:
            await interaction.response.send_message("Não consegui identificar o cargo informado.", ephemeral=True)
            return
        role = guild.get_role(int(match.group(1)))
        if role is None:
            await interaction.response.send_message("Não encontrei esse cargo neste servidor.", ephemeral=True)
            return
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, self.slot_number)
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            self.slot_number,
            role_id=int(role.id),
            role_name=str(role.name),
            managed=False,
            name=str(slot.get("name") or f"Cor {self.slot_number}"),
            text_hex=str(slot.get("text_hex") or "#ffffff"),
            role_hex=str(slot.get("role_hex") or "#ffffff"),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _ColorPickerButton(discord.ui.Button):
    def __init__(self, cog: "ColorRolesCog", guild_id: int, slot_number: int, position: int, *, disabled: bool = False):
        super().__init__(label=_math_sans_bold(str(position)), style=discord.ButtonStyle.secondary, custom_id=f"color:pick:{guild_id}:{slot_number}", disabled=disabled)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.slot_number = int(slot_number)

    async def callback(self, interaction: discord.Interaction):
        await self.cog._handle_public_pick(interaction, self.slot_number)


class _ColorPublicPanelView(discord.ui.View):
    def __init__(self, cog: "ColorRolesCog", guild_id: int, block_index: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.block_index = int(block_index)
        cfg = self.cog._get_config(self.guild_id)
        feature_enabled = bool(cfg.get("enabled", False) and int(cfg.get("channel_id") or 0))
        if _message_supports_slots(block_index):
            for position, slot_number in enumerate(self.cog._get_panel_slot_numbers(self.guild_id, block_index), start=1):
                self.add_item(_ColorPickerButton(self.cog, self.guild_id, slot_number, position, disabled=not feature_enabled))


class _ConfirmActionView(discord.ui.View):
    def __init__(self, owner_id: int, action: Callable[[], Awaitable[None]], success_text: str):
        super().__init__(timeout=90)
        self.owner_id = int(owner_id)
        self.action = action
        self.success_text = success_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu o editor pode confirmar essa ação.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.action()
        await interaction.response.edit_message(content=self.success_text, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ação cancelada.", view=None)
        self.stop()


class _EditContentButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(label="Editar conteúdo", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.message_index = int(message_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorContentEditModal(self.view_ref, self.message_index))


class _EditTemplatesButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Editar respostas", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        await interaction.response.send_modal(_ColorTemplatesEditModal(self.view_ref))


class _AddMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Adicionar painel", style=discord.ButtonStyle.success)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.view_ref.cog._get_panel_count(self.view_ref.guild_id) >= COLOR_MAX_MESSAGES:
            await interaction.response.send_message("O painel já está no máximo de 3 painéis.", ephemeral=True)
            return
        new_count = await self.view_ref.cog._add_extra_message_live(self.view_ref.guild_id)
        self.view_ref.active_block = int(new_count)
        await self.view_ref.refresh_editor_message(interaction)


class _RemoveMessageModal(discord.ui.Modal):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(title="Remover painel")
        self.view_ref = view
        self.number_input = discord.ui.TextInput(
            label="Número do painel",
            placeholder="Ex.: 2",
            required=True,
            max_length=2,
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.number_input.value or "").strip()
        if not raw.isdigit():
            await interaction.response.send_message("Informe um número válido de painel.", ephemeral=True)
            return
        message_index = int(raw)
        panel_count = self.view_ref.cog._get_panel_count(self.view_ref.guild_id)
        if panel_count <= 1 or not (1 <= message_index <= panel_count):
            await interaction.response.send_message("Informe um painel existente. O primeiro e único painel não pode ser removido.", ephemeral=True)
            return
        await self.view_ref.cog._remove_extra_message_live(self.view_ref.guild_id, message_index)
        self.view_ref.active_block = min(self.view_ref.active_block, self.view_ref.cog._get_panel_count(self.view_ref.guild_id))
        await self.view_ref.refresh_editor_message(interaction)


class _RemoveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView"):
        super().__init__(label="Remover painel", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.view_ref.cog._get_panel_count(self.view_ref.guild_id) <= 1:
            await interaction.response.send_message("O único painel não pode ser removido.", ephemeral=True)
            return
        await interaction.response.send_modal(_RemoveMessageModal(self.view_ref))


class _MessageSelect(discord.ui.Select):
    def __init__(self, view: "_ColorUnifiedEditView"):
        self.view_ref = view
        options: list[discord.SelectOption] = []
        panel_count = view.cog._get_panel_count(view.guild_id)
        for message_index in range(1, panel_count + 1):
            option_count = len(view.cog._get_panel_slot_numbers(view.guild_id, message_index))
            options.append(
                discord.SelectOption(
                    label=_message_label(message_index)[:100],
                    value=str(message_index),
                    description=f"{option_count} de 10 opções",
                    default=view.active_block == message_index,
                )
            )
        super().__init__(placeholder="Escolha o painel para editar", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        self.view_ref.active_block = int(self.values[0])
        await self.view_ref.refresh_editor_message(interaction)


class _MoveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int, direction: int):
        label = "↑" if direction < 0 else "↓"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, emoji="⬆️" if direction < 0 else "⬇️")
        self.view_ref = view
        self.message_index = int(message_index)
        self.direction = -1 if direction < 0 else 1
        self.disabled = not view.cog._can_move_message(view.guild_id, self.message_index, self.direction)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if not self.view_ref.cog._can_move_message(self.view_ref.guild_id, self.message_index, self.direction):
            await interaction.response.send_message("Esse painel não pode ser movido nessa direção.", ephemeral=True)
            return
        await self.view_ref.cog._swap_messages(self.view_ref.guild_id, self.message_index, self.message_index + self.direction)
        self.view_ref.active_block = self.message_index + self.direction
        await self.view_ref.refresh_editor_message(interaction)
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id)


class _ClearMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", message_index: int):
        super().__init__(label="Limpar mensagem", style=discord.ButtonStyle.danger)
        self.view_ref = view
        self.message_index = int(message_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return

        async def action():
            await self.view_ref.cog._clear_message_text(self.view_ref.guild_id, self.message_index)
            await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.message_index])
            await self.view_ref.force_refresh_from_background()

        await interaction.response.send_message(
            "Confirmar limpeza do conteúdo desta mensagem?",
            ephemeral=True,
            view=_ConfirmActionView(self.view_ref.owner_id, action, "Conteúdo da mensagem limpo."),
        )


class _BlockSlotSelect(discord.ui.Select):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        self.view_ref = view
        self.block_index = int(block_index)
        slot_numbers = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        current = view.selected_slots.get(block_index, slot_numbers[0] if slot_numbers else 1)
        options = []
        for position, slot_number in enumerate(slot_numbers, start=1):
            slot = view.cog._get_slot_config(view.guild_id, slot_number)
            options.append(discord.SelectOption(label=f"{position}. {slot.get('name')}", value=str(slot_number), default=current == slot_number))
        super().__init__(placeholder="Escolha a opção deste painel", options=options or [discord.SelectOption(label="Sem opções", value="0")], min_values=1, max_values=1, disabled=not bool(options))

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        self.view_ref.selected_slots[self.block_index] = int(self.values[0])
        await self.view_ref.refresh_editor_message(interaction)


class _BlockRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(placeholder="Vincular um cargo existente ao slot atual", min_values=1, max_values=1)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        panel_slots = self.view_ref.cog._get_panel_slot_numbers(self.view_ref.guild_id, self.block_index)
        selected_slot = self.view_ref.selected_slots.get(self.block_index, panel_slots[0] if panel_slots else 1)
        if not self.values:
            await interaction.response.send_message("Escolha um cargo para vincular ao slot.", ephemeral=True)
            return
        role = self.values[0]
        slot = self.view_ref.cog._get_slot_config(self.view_ref.guild_id, selected_slot)
        await self.view_ref.cog._update_slot_config(
            self.view_ref.guild_id,
            selected_slot,
            role_id=int(role.id),
            role_name=str(role.name),
            managed=False,
            name=str(slot.get("name") or f"Cor {selected_slot}"),
            text_hex=str(slot.get("text_hex") or "#ffffff"),
            role_hex=str(slot.get("role_hex") or "#ffffff"),
        )
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _AutoRoleButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Usar cargo automático", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Isso só funciona dentro de um servidor.", ephemeral=True)
            return
        panel_slots = self.view_ref.cog._get_panel_slot_numbers(self.view_ref.guild_id, self.block_index)
        selected_slot = self.view_ref.selected_slots.get(self.block_index, panel_slots[0] if panel_slots else 1)
        await self.view_ref.cog._update_slot_config(self.view_ref.guild_id, selected_slot, role_id=0, managed=True)
        await self.view_ref.cog._ensure_slot_role(guild, selected_slot)
        await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
        await self.view_ref.refresh_editor_message(interaction)


class _ChangeActiveMessageButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", direction: int):
        is_prev = direction < 0
        current = int(view.active_block)
        panel_count = view.cog._get_panel_count(view.guild_id)
        target = current - 1 if is_prev else current + 1
        super().__init__(label="Mensagem anterior" if is_prev else "Próxima mensagem", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.target = target
        self.disabled = not (1 <= target <= panel_count)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.disabled:
            await interaction.response.send_message("Não há outra mensagem nessa direção.", ephemeral=True)
            return
        self.view_ref.active_block = self.target
        await self.view_ref.refresh_editor_message(interaction)


class _ChangeActiveSlotButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, direction: int):
        slot_numbers = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        current = int(view.selected_slots.get(block_index, slot_numbers[0] if slot_numbers else 1))
        try:
            current_index = slot_numbers.index(current)
        except ValueError:
            current_index = 0
        target_index = current_index + (-1 if direction < 0 else 1)
        super().__init__(label="Opção anterior" if direction < 0 else "Próxima opção", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)
        self.target = slot_numbers[target_index] if 0 <= target_index < len(slot_numbers) else current
        self.disabled = not (0 <= target_index < len(slot_numbers))

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        if self.disabled:
            await interaction.response.send_message("Não há outro slot nessa direção.", ephemeral=True)
            return
        self.view_ref.selected_slots[self.block_index] = self.target
        await self.view_ref.refresh_editor_message(interaction)


class _MoveOptionButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int, direction: int):
        slot_numbers = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        selected_slot = view.selected_slot_for(block_index)
        try:
            current_index = slot_numbers.index(selected_slot)
        except ValueError:
            current_index = 0
        target = current_index + (-1 if direction < 0 else 1)
        super().__init__(
            label="Mover antes" if direction < 0 else "Mover depois",
            style=discord.ButtonStyle.secondary,
            disabled=not (0 <= target < len(slot_numbers)),
        )
        self.view_ref = view
        self.block_index = int(block_index)
        self.direction = -1 if direction < 0 else 1

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        selected_slot = self.view_ref.selected_slot_for(self.block_index)
        moved = await self.view_ref.cog._move_panel_option(
            self.view_ref.guild_id,
            self.block_index,
            selected_slot,
            self.direction,
        )
        if not moved:
            await interaction.response.send_message("Essa opção não pode ser movida nessa direção.", ephemeral=True)
            return
        await self.view_ref.cog._refresh_public_panel_messages(
            self.view_ref.guild_id,
            block_indices=[self.block_index],
        )
        await self.view_ref.refresh_editor_message(interaction)


class _AddOptionButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        slot_numbers = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        super().__init__(
            label="Adicionar opção",
            style=discord.ButtonStyle.success,
            disabled=len(slot_numbers) >= COLOR_BLOCK_SIZE,
        )
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        slot_number = await self.view_ref.cog._add_panel_option(self.view_ref.guild_id, self.block_index)
        if slot_number is None:
            await interaction.response.send_message("Esse painel já tem 10 opções ou não há outro slot disponível.", ephemeral=True)
            return
        self.view_ref.selected_slots[self.block_index] = int(slot_number)
        await self.view_ref.cog._refresh_public_panel_messages(
            self.view_ref.guild_id,
            block_indices=[self.block_index],
        )
        await self.view_ref.refresh_editor_message(interaction)


class _RemoveOptionButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        slot_numbers = view.cog._get_panel_slot_numbers(view.guild_id, block_index)
        super().__init__(
            label="Remover opção",
            style=discord.ButtonStyle.danger,
            disabled=len(slot_numbers) <= 1,
        )
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        selected_slot = self.view_ref.selected_slot_for(self.block_index)

        async def action():
            next_slot = await self.view_ref.cog._remove_panel_option(
                self.view_ref.guild_id,
                self.block_index,
                selected_slot,
            )
            if next_slot is not None:
                self.view_ref.selected_slots[self.block_index] = int(next_slot)
            await self.view_ref.cog._refresh_public_panel_messages(
                self.view_ref.guild_id,
                block_indices=[self.block_index],
            )
            await self.view_ref.force_refresh_from_background()

        await interaction.response.send_message(
            "Remover esta opção do painel? O cargo não será excluído do servidor.",
            ephemeral=True,
            view=_ConfirmActionView(self.view_ref.owner_id, action, "Opção removida do painel."),
        )


class _LinkExistingRoleButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Vincular cargo", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        panel_slots = self.view_ref.cog._get_panel_slot_numbers(self.view_ref.guild_id, self.block_index)
        selected_slot = self.view_ref.selected_slots.get(self.block_index, panel_slots[0] if panel_slots else 1)
        await interaction.response.send_modal(_ColorRoleLinkModal(self.view_ref, self.block_index, selected_slot))


class _EditSlotButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Editar nome", style=discord.ButtonStyle.secondary)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return
        panel_slots = self.view_ref.cog._get_panel_slot_numbers(self.view_ref.guild_id, self.block_index)
        selected_slot = self.view_ref.selected_slots.get(self.block_index, panel_slots[0] if panel_slots else 1)
        await interaction.response.send_modal(_ColorSlotEditModal(self.view_ref, self.block_index, selected_slot))


class _SlotPresetButton(discord.ui.Button):
    def __init__(self, view: "_ColorUnifiedEditView", block_index: int):
        super().__init__(label="Resetar preset da faixa", style=discord.ButtonStyle.danger)
        self.view_ref = view
        self.block_index = int(block_index)

    async def callback(self, interaction: discord.Interaction):
        if not await self.view_ref.ensure_owner(interaction):
            return

        async def action():
            await self.view_ref.cog._reset_slot_block_to_preset(self.view_ref.guild_id, self.block_index)
            await self.view_ref.cog._refresh_public_panel_messages(self.view_ref.guild_id, block_indices=[self.block_index])
            await self.view_ref.force_refresh_from_background()

        await interaction.response.send_message(
            "Confirmar reset desta faixa para o preset? Isso também zera os vínculos de cargo dessa faixa.",
            ephemeral=True,
            view=_ConfirmActionView(self.view_ref.owner_id, action, "Faixa resetada para o preset."),
        )


class _ColorUnifiedEditView(discord.ui.LayoutView):
    def __init__(self, cog: "ColorRolesCog", *, guild_id: int, owner_id: int):
        super().__init__(timeout=COLOR_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id)
        self.active_block = 1
        self.selected_slots = {1: 1, 2: 11, 3: 21}
        self.message: discord.Message | None = None
        self._build_layout()

    async def ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            await interaction.response.send_message("Só quem abriu esse painel pode mexer nele.", ephemeral=True)
            return False
        return True

    def selected_slot_for(self, block_index: int) -> int:
        slot_numbers = self.cog._get_panel_slot_numbers(self.guild_id, block_index)
        if not slot_numbers:
            return 1
        current = int(self.selected_slots.get(block_index, slot_numbers[0]))
        if current not in slot_numbers:
            current = slot_numbers[0]
            self.selected_slots[block_index] = current
        return current

    def _editor_preview_files(self) -> list[discord.File]:
        active = self.active_block
        if not _message_supports_slots(active):
            return []
        return [self.cog._make_block_image(self.guild_id, active, filename=f"colors-editor-{active}.png")]

    def editor_message_payload(self) -> dict[str, Any]:
        return {
            "view": self,
            "attachments": self._editor_preview_files(),
        }

    async def force_refresh_from_background(self):
        self._build_layout()
        if self.message is not None:
            await self.message.edit(**self.editor_message_payload())

    async def refresh_editor_message(self, interaction: discord.Interaction):
        self._build_layout()
        payload = self.editor_message_payload()
        if not interaction.response.is_done():
            await interaction.response.defer()
        target = interaction.message or self.message
        if target is not None:
            await target.edit(**payload)
            self.message = target

    def _header_lines(self) -> list[str]:
        return [
            "# 🎨 Editor do painel de cores",
            f"Painel ativo: {_message_label(self.active_block)}",
        ]

    def _block_lines(self, message_index: int) -> list[str]:
        option_count = len(self.cog._get_panel_slot_numbers(self.guild_id, message_index))
        return [
            f"## Painel {message_index}",
            f"**Opções:** {option_count} de 10",
            "A imagem e o seletor são gerados automaticamente a partir das opções abaixo.",
        ]

    def _slot_editor_lines(self, block_index: int) -> list[str]:
        panel_slots = self.cog._get_panel_slot_numbers(self.guild_id, block_index)
        selected_slot = self.selected_slot_for(block_index)
        slot = self.cog._get_slot_config(self.guild_id, selected_slot)
        role_id = int(slot.get("role_id") or 0)
        managed = bool(slot.get("managed", False))
        role_repr = f"<@&{role_id}>" if role_id else ("Preset automático" if managed else "Não vinculado")
        managed_text = "sim" if managed else "não"
        return [
            f"## Painel {block_index}",
            f"**Opção:** {(panel_slots.index(selected_slot) + 1) if selected_slot in panel_slots else 1}",
            f"**Nome:** {slot.get('name')}",
            f"**Cargo:** {role_repr}",
            f"**Cor:** {self.cog._slot_effective_hex(self.guild_id, slot)}",
            f"**Cargo automático:** {managed_text}",
        ]

    def _build_layout(self):
        self.clear_items()
        panel_count = self.cog._get_panel_count(self.guild_id)
        top_controls: list[discord.ui.Item[Any]] = [
            _EditTemplatesButton(self),
            _AddMessageButton(self),
        ]
        if panel_count > 1:
            top_controls.append(_RemoveMessageButton(self))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._header_lines())),
            discord.ui.ActionRow(_MessageSelect(self)),
            discord.ui.ActionRow(*top_controls),
            accent_color=discord.Colour.green(),
        ))

        active = self.active_block
        block_children: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join(self._block_lines(active)))]
        if _message_supports_slots(active):
            block_children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        f"attachment://colors-editor-{active}.png",
                        description=f"Prévia do Painel {active}",
                    )
                )
            )
        self.add_item(discord.ui.Container(
            *block_children,
            accent_color=discord.Colour.green(),
        ))

        if _message_supports_slots(active):
            slot_rows: list[discord.ui.Item[Any]] = [
                discord.ui.TextDisplay("\n".join(self._slot_editor_lines(active))),
                discord.ui.ActionRow(_BlockSlotSelect(self, active)),
                discord.ui.ActionRow(
                    _EditSlotButton(self, active),
                    _LinkExistingRoleButton(self, active),
                    _AutoRoleButton(self, active),
                ),
                discord.ui.ActionRow(
                    _MoveOptionButton(self, active, -1),
                    _MoveOptionButton(self, active, 1),
                    _AddOptionButton(self, active),
                    _RemoveOptionButton(self, active),
                ),
            ]
            if self.cog._slot_block_changed_from_preset(self.guild_id, active):
                slot_rows.append(discord.ui.ActionRow(_SlotPresetButton(self, active)))
            self.add_item(discord.ui.Container(
                *slot_rows,
                accent_color=discord.Colour.blurple(),
            ))




class ColorRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_edit_messages: dict[tuple[int, int], int] = {}
        self._public_views_registered: set[tuple[int, int, int]] = set()
        self._color_panel_cd: dict[int, float] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._restore_public_panel_views()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_public_panel_views()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._restore_public_panel_views(guild_ids=[int(guild.id)])

    async def _restore_public_panel_views(self, guild_ids: list[int] | None = None):
        ids: set[int] = set(int(gid) for gid in (guild_ids or []) if gid)
        db = self.db
        if db is not None and hasattr(db, "guild_cache"):
            ids.update(int(gid) for gid in getattr(db, "guild_cache", {}).keys())
        ids.update(int(getattr(guild, "id", 0) or 0) for guild in getattr(self.bot, "guilds", []) if getattr(guild, "id", 0))
        for gid in sorted(gid for gid in ids if gid):
            cfg = self._get_config(int(gid))
            message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
            for block_index, message_id in enumerate(message_ids, start=1):
                if not _message_supports_slots(block_index):
                    continue
                key = (int(gid), block_index, int(message_id))
                if key in self._public_views_registered:
                    continue
                view = _ColorPublicPanelView(self, int(gid), block_index)
                try:
                    self.bot.add_view(view, message_id=int(message_id))
                    self._public_views_registered.add(key)
                except Exception:
                    pass

    def _sanitize_config(self, guild_id: int, config: dict[str, Any]) -> dict[str, Any]:
        base = _deepcopy_default_config()
        payload = deepcopy(config or {})
        if "enabled" in payload:
            base["enabled"] = bool(payload.get("enabled"))
        else:
            base["enabled"] = bool(int(payload.get("channel_id") or 0) and list(payload.get("message_ids") or []))
        base["channel_id"] = int(payload.get("channel_id") or 0)
        base["message_ids"] = [int(mid) for mid in (payload.get("message_ids") or []) if str(mid).isdigit()]
        raw_count = int(payload.get("panel_count") or COLOR_BLOCK_COUNT)
        base["panel_layout"] = _normalize_panel_layout(payload.get("panel_layout"), fallback_count=raw_count)
        base["panel_count"] = len(base["panel_layout"])
        raw_messages = payload.get("messages") or {}
        for key in [str(idx) for idx in range(1, COLOR_MAX_MESSAGES + 1)]:
            block = raw_messages.get(key) or {}
            base["messages"][key] = {
                "title": str(block.get("title") or ""),
                "subtitle": str(block.get("subtitle") or ""),
                "footer": str(block.get("footer") or ""),
            }
        raw_templates = payload.get("templates") or {}
        for key in list(base["templates"].keys()):
            raw_value = raw_templates.get(key)
            if raw_value is None:
                continue
            text = str(raw_value)
            if text in _LEGACY_TEMPLATE_DEFAULTS.get(key, ()):
                continue
            base["templates"][key] = text
        raw_slots = payload.get("slots") or {}
        for slot_number in range(1, 31):
            key = str(slot_number)
            default_slot = _default_slot_payload(slot_number)
            legacy_slot = _legacy_slot_payload(slot_number)
            merged = dict(default_slot)
            merged.update(dict(raw_slots.get(key) or {}))
            merged["number"] = int(slot_number)
            merged["role_id"] = int(merged.get("role_id") or 0)
            merged["managed"] = bool(merged.get("managed", False))
            merged["name"] = str(merged.get("name") or default_slot["name"])
            merged["role_name"] = str(merged.get("role_name") or merged["name"])
            merged["text_hex"] = _clean_hex(str(merged.get("text_hex") or ""), default_slot["text_hex"])
            merged["role_hex"] = _clean_hex(str(merged.get("role_hex") or ""), default_slot["role_hex"])
            if (
                slot_number != 30
                and int(merged.get("role_id") or 0) == 0
                and merged["text_hex"] == "#ffffff"
                and merged["role_hex"] == "#ffffff"
            ):
                merged["text_hex"] = default_slot["text_hex"]
                merged["role_hex"] = default_slot["role_hex"]
            comparable_current = {
                "name": str(merged.get("name") or ""),
                "text_hex": merged["text_hex"],
                "role_hex": merged["role_hex"],
                "role_id": int(merged.get("role_id") or 0),
                "role_name": str(merged.get("role_name") or ""),
                "managed": bool(merged.get("managed", False)),
            }
            comparable_legacy = {
                "name": str(legacy_slot["name"]),
                "text_hex": legacy_slot["text_hex"],
                "role_hex": legacy_slot["role_hex"],
                "role_id": 0,
                "role_name": str(legacy_slot["role_name"]),
                "managed": False,
            }
            if comparable_current == comparable_legacy:
                merged = dict(default_slot)
            elif slot_number == 10 and int(merged.get("role_id") or 0) >= 0:
                legacy_name = {"Preto escuro", "Preto"}
                if (
                    str(merged.get("name") or "") in legacy_name
                    and merged["text_hex"] in {"#4a4a4a", "#000000"}
                    and merged["role_hex"] in {"#1f1f1f", "#000000"}
                    and (bool(merged.get("managed", False)) or int(merged.get("role_id") or 0) == 0)
                ):
                    merged["name"] = default_slot["name"]
                    merged["text_hex"] = default_slot["text_hex"]
                    merged["role_hex"] = default_slot["role_hex"]
                    if str(merged.get("role_name") or "") in {"", "Preto escuro", "Preto"}:
                        merged["role_name"] = default_slot["role_name"]
            base["slots"][key] = merged
        if _block_looks_like_default_source(base["slots"], 1, 2) and _block_looks_like_default_source(base["slots"], 2, 1):
            repaired_slots = dict(base["slots"])
            first_defaults = [_default_slot_payload(number) for number in range(1, 11)]
            second_defaults = [_default_slot_payload(number) for number in range(11, 21)]
            for offset, slot_number in enumerate(range(1, 11)):
                repaired_slots[str(slot_number)] = dict(first_defaults[offset])
            for offset, slot_number in enumerate(range(11, 21)):
                repaired_slots[str(slot_number)] = dict(second_defaults[offset])
            base["slots"] = repaired_slots
        return base

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_color_roles_config"):
            return _deepcopy_default_config()
        return self._sanitize_config(guild_id, db.get_color_roles_config(int(guild_id)))

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "set_color_roles_config"):
            return
        await db.set_color_roles_config(int(guild_id), self._sanitize_config(guild_id, config))

    def _panel_exists(self, guild_id: int) -> bool:
        cfg = self._get_config(guild_id)
        return bool(int(cfg.get("channel_id") or 0) and list(cfg.get("message_ids") or []))

    def _get_panel_count(self, guild_id: int) -> int:
        cfg = self._get_config(guild_id)
        return len(_normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT)))

    def _get_panel_slot_numbers(self, guild_id: int, block_index: int) -> list[int]:
        return _panel_slots(self._get_config(guild_id), block_index)

    async def _add_panel_option(self, guild_id: int, block_index: int) -> int | None:
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT))
        if not (1 <= int(block_index) <= len(layout)):
            return None
        panel = layout[int(block_index) - 1]
        current = [int(number) for number in panel.get("slots") or []]
        if len(current) >= COLOR_BLOCK_SIZE:
            return None
        slot_number = _next_unused_slot(layout)
        if slot_number is None:
            return None
        panel["slots"] = [*current, slot_number]
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)
        return slot_number

    async def _remove_panel_option(self, guild_id: int, block_index: int, slot_number: int) -> int | None:
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT))
        if not (1 <= int(block_index) <= len(layout)):
            return None
        panel = layout[int(block_index) - 1]
        current = [int(number) for number in panel.get("slots") or []]
        if len(current) <= 1 or int(slot_number) not in current:
            return None
        removed_index = current.index(int(slot_number))
        current.remove(int(slot_number))
        panel["slots"] = current
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)
        return current[min(removed_index, len(current) - 1)]

    async def _move_panel_option(self, guild_id: int, block_index: int, slot_number: int, direction: int) -> bool:
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT))
        if not (1 <= int(block_index) <= len(layout)):
            return False
        panel = layout[int(block_index) - 1]
        current = [int(number) for number in panel.get("slots") or []]
        if int(slot_number) not in current:
            return False
        index = current.index(int(slot_number))
        target = index + (-1 if int(direction) < 0 else 1)
        if not (0 <= target < len(current)):
            return False
        current[index], current[target] = current[target], current[index]
        panel["slots"] = current
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)
        return True

    async def _set_panel_count(self, guild_id: int, count: int):
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT))
        target = max(1, min(COLOR_MAX_MESSAGES, int(count)))
        while len(layout) < target:
            slot_number = _next_unused_slot(layout)
            if slot_number is None:
                break
            layout.append({"id": f"panel-{time.time_ns()}", "slots": [slot_number]})
        layout = layout[:target]
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)

    def _get_templates(self, guild_id: int) -> dict[str, str]:
        return dict(self._get_config(guild_id).get("templates") or {})

    async def _update_templates(self, guild_id: int, **kwargs: str):
        cfg = self._get_config(guild_id)
        templates = dict(cfg.get("templates") or {})
        for key, value in kwargs.items():
            templates[key] = str(value or "")
        cfg["templates"] = templates
        await self._save_config(guild_id, cfg)

    def _get_message_block_config(self, guild_id: int, block_index: int) -> dict[str, str]:
        cfg = self._get_config(guild_id)
        return dict((cfg.get("messages") or {}).get(str(block_index), {}) or {})

    async def _update_message_block_config(self, guild_id: int, block_index: int, *, title: str, subtitle: str, footer: str):
        cfg = self._get_config(guild_id)
        messages = dict(cfg.get("messages") or {})
        messages[str(block_index)] = {"title": title, "subtitle": subtitle, "footer": footer}
        cfg["messages"] = messages
        await self._save_config(guild_id, cfg)

    async def _reset_message_text_to_preset(self, guild_id: int, block_index: int):
        await self._update_message_block_config(guild_id, block_index, title="", subtitle="", footer="")

    async def _clear_message_text(self, guild_id: int, block_index: int):
        await self._update_message_block_config(guild_id, block_index, title="", subtitle="", footer="")

    def _message_text_changed_from_preset(self, guild_id: int, block_index: int) -> bool:
        block = self._get_message_block_config(guild_id, block_index)
        return any(str(block.get(field) or "").strip() for field in ("title", "subtitle", "footer"))

    def _get_slot_config(self, guild_id: int, slot_number: int) -> dict[str, Any]:
        cfg = self._get_config(guild_id)
        slot = dict((cfg.get("slots") or {}).get(str(slot_number), {}) or {})
        if not slot:
            slot = _default_slot_payload(slot_number)
        return slot

    async def _update_slot_config(self, guild_id: int, slot_number: int, **updates: Any):
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        slot = dict(slots.get(str(slot_number), {}) or _default_slot_payload(slot_number))
        slot.update(updates)
        slot["number"] = int(slot_number)
        if not slot.get("role_name"):
            slot["role_name"] = str(slot.get("name") or f"Cor {slot_number}")
        slots[str(slot_number)] = slot
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    def _slot_block_changed_from_preset(self, guild_id: int, block_index: int) -> bool:
        if not _message_supports_slots(block_index):
            return False
        for slot_number in self._get_panel_slot_numbers(guild_id, block_index):
            current = self._get_slot_config(guild_id, slot_number)
            default = _default_slot_payload(slot_number)
            comparable_current = {
                "name": str(current.get("name") or ""),
                "text_hex": _clean_hex(str(current.get("text_hex") or ""), default["text_hex"]),
                "role_hex": _clean_hex(str(current.get("role_hex") or ""), default["role_hex"]),
                "role_id": int(current.get("role_id") or 0),
                "role_name": str(current.get("role_name") or ""),
                "managed": bool(current.get("managed", False)),
            }
            comparable_default = {
                "name": str(default["name"]),
                "text_hex": default["text_hex"],
                "role_hex": default["role_hex"],
                "role_id": 0,
                "role_name": str(default["role_name"]),
                "managed": False,
            }
            if comparable_current != comparable_default:
                return True
        return False

    async def _reset_slot_block_to_preset(self, guild_id: int, block_index: int):
        if not _message_supports_slots(block_index):
            return
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        for slot_number in self._get_panel_slot_numbers(guild_id, block_index):
            slots[str(slot_number)] = dict(_default_slot_payload(slot_number))
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    async def _clear_slot_block(self, guild_id: int, block_index: int):
        if not _message_supports_slots(block_index):
            return
        cfg = self._get_config(guild_id)
        slots = dict(cfg.get("slots") or {})
        for slot_number in self._get_panel_slot_numbers(guild_id, block_index):
            slots[str(slot_number)] = dict(_cleared_slot_payload(slot_number))
        cfg["slots"] = slots
        await self._save_config(guild_id, cfg)

    async def _add_extra_message(self, guild_id: int):
        count = self._get_panel_count(guild_id)
        if count >= COLOR_MAX_MESSAGES:
            return 0
        new_count = count + 1
        await self._set_panel_count(guild_id, new_count)
        return new_count

    async def _add_extra_message_live(self, guild_id: int) -> int:
        old_count = self._get_panel_count(guild_id)
        new_count = await self._add_extra_message(guild_id)
        if not new_count:
            return old_count
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        if channel_id and len(message_ids) == old_count:
            channel = self.bot.get_channel(channel_id)
            guild = self.bot.get_guild(guild_id)
            if channel is not None and guild is not None:
                kwargs = self._public_message_kwargs(guild_id, new_count)
                try:
                    message = await channel.send(**kwargs)
                    message_ids.append(int(message.id))
                    cfg["message_ids"] = message_ids
                    await self._save_config(guild_id, cfg)
                except Exception:
                    pass
        return self._get_panel_count(guild_id)

    async def _remove_extra_message(self, guild_id: int, message_index: int):
        count = self._get_panel_count(guild_id)
        if count <= 1 or not (1 <= int(message_index) <= count):
            return False
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=count)
        del layout[int(message_index) - 1]
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)
        return True

    async def _remove_extra_message_live(self, guild_id: int, message_index: int) -> bool:
        count = self._get_panel_count(guild_id)
        if count <= 1 or not (1 <= int(message_index) <= count):
            return False
        cfg_before = self._get_config(guild_id)
        channel_id = int(cfg_before.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg_before.get("message_ids") or []) if mid]
        removed_message_id = message_ids[int(message_index) - 1] if len(message_ids) >= int(message_index) else 0
        ok = await self._remove_extra_message(guild_id, message_index)
        if not ok:
            return False
        cfg = self._get_config(guild_id)
        if removed_message_id and channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                try:
                    target = await channel.fetch_message(removed_message_id)
                    await target.delete()
                except Exception:
                    pass
        if len(message_ids) >= int(message_index):
            del message_ids[int(message_index) - 1]
            cfg["message_ids"] = message_ids[: self._get_panel_count(guild_id)]
            await self._save_config(guild_id, cfg)
        await self._refresh_public_panel_messages(guild_id)
        return True

    def _can_move_message(self, guild_id: int, block_index: int, direction: int) -> bool:
        count = self._get_panel_count(guild_id)
        target = int(block_index) + int(direction)
        return 1 <= int(block_index) <= count and 1 <= target <= count

    async def _swap_messages(self, guild_id: int, left: int, right: int):
        cfg = self._get_config(guild_id)
        layout = _normalize_panel_layout(cfg.get("panel_layout"), fallback_count=int(cfg.get("panel_count") or COLOR_BLOCK_COUNT))
        if not (1 <= int(left) <= len(layout) and 1 <= int(right) <= len(layout)):
            return
        layout[int(left) - 1], layout[int(right) - 1] = layout[int(right) - 1], layout[int(left) - 1]
        cfg["panel_layout"] = layout
        cfg["panel_count"] = len(layout)
        await self._save_config(guild_id, cfg)

    async def _ensure_slot_role(self, guild: discord.Guild, slot_number: int) -> discord.Role | None:
        slot = self._get_slot_config(guild.id, slot_number)
        role_id = int(slot.get("role_id") or 0)
        existing = guild.get_role(role_id) if role_id else None
        desired_name = str(slot.get("role_name") or slot.get("name") or f"Cor {slot_number}")
        desired_colour = discord.Colour.from_str(_clean_hex(str(slot.get("role_hex") or ""), "#ffffff"))
        if existing and not bool(slot.get("managed")):
            return existing
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if me is None or not me.guild_permissions.manage_roles:
            return existing
        try:
            if existing is None:
                existing = await guild.create_role(name=desired_name, colour=desired_colour, reason="Criando cargo da paleta de cores")
            else:
                await existing.edit(name=desired_name, colour=desired_colour, reason="Atualizando cargo da paleta de cores")
            await self._update_slot_config(guild.id, slot_number, role_id=int(existing.id), role_name=existing.name, managed=True, role_hex=_clean_hex(str(slot.get("role_hex") or ""), "#ffffff"))
            return existing
        except Exception:
            return existing

    def _render_template(self, template: str, *, member: discord.Member, slot: dict[str, Any], added_name: str = "", removed_name: str = "") -> str:
        guild = member.guild
        role_id = int(slot.get("role_id") or 0)
        role = guild.get_role(role_id) if guild and role_id else None
        payload = {
            "membro": member.mention,
            "membro_nome": member.display_name,
            "membro_id": str(member.id),
            "numero": str(slot.get("number") or ""),
            "cor_nome": _normalize_color_name(str(slot.get("name") or "")),
            "cor_adicionada": _normalize_color_name(str(added_name or slot.get("name") or "")),
            "cor_removida": _normalize_color_name(str(removed_name or "")),
            "cargo": role.mention if role else "",
            "cargo_nome": role.name if role else str(slot.get("role_name") or slot.get("name") or ""),
            "servidor": guild.name if guild else "",
        }
        text = str(template or "").strip()
        for key, value in payload.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text

    def _all_color_role_ids(self, guild_id: int) -> list[int]:
        cfg = self._get_config(guild_id)
        result = []
        for slot in (cfg.get("slots") or {}).values():
            try:
                rid = int(slot.get("role_id") or 0)
            except Exception:
                rid = 0
            if rid:
                result.append(rid)
        return result

    def _all_color_role_names(self, guild_id: int) -> set[str]:
        cfg = self._get_config(guild_id)
        result: set[str] = set()
        for slot_num_str, raw_slot in (cfg.get("slots") or {}).items():
            try:
                slot_number = int(slot_num_str)
            except Exception:
                slot_number = 0
            fallback = _default_slot_payload(slot_number or 1)
            slot = dict(raw_slot or {})
            for candidate in (
                slot.get("role_name"),
                slot.get("name"),
                fallback.get("role_name"),
                fallback.get("name"),
            ):
                label = str(candidate or "").strip().casefold()
                if label:
                    result.add(label)
        return result

    def _member_color_roles(self, guild: discord.Guild, member: discord.Member) -> list[discord.Role]:
        known_ids = set(self._all_color_role_ids(guild.id))
        known_names = self._all_color_role_names(guild.id)
        matches: list[discord.Role] = []
        seen_ids: set[int] = set()
        for role in member.roles:
            if role.is_default():
                continue
            role_name = str(role.name or "").strip().casefold()
            if role.id in known_ids or (role_name and role_name in known_names):
                if role.id not in seen_ids:
                    matches.append(role)
                    seen_ids.add(role.id)
        return matches

    def _member_current_color_slot(self, guild: discord.Guild, member: discord.Member) -> tuple[int, dict[str, Any] | None]:
        member_role_ids = {role.id for role in self._member_color_roles(guild, member)}
        cfg = self._get_config(guild.id)
        for slot_num_str, slot in (cfg.get("slots") or {}).items():
            try:
                rid = int(slot.get("role_id") or 0)
                slot_num = int(slot_num_str)
            except Exception:
                continue
            if rid and rid in member_role_ids:
                return slot_num, dict(slot)
        return 0, None

    def _dispatch_member_color_changed(self, member: discord.Member, color_hex: str | None) -> None:
        try:
            clean = _clean_hex(str(color_hex or ""), "") if color_hex else None
            self.bot.dispatch("member_color_changed", member, clean or None)
        except Exception:
            logger.debug("[color_roles] falha ao emitir evento member_color_changed", exc_info=True)

    async def _handle_public_pick(self, interaction: discord.Interaction, slot_number: int):
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None

        async def reply(text: str) -> None:
            await safe_send_interaction_message(
                interaction,
                text,
                ephemeral=True,
                log=logger,
                label="color_roles.public_pick",
            )

        # Responde/defer imediatamente. Em dias de lag na VPS, aplicar/remover cargos
        # antes da primeira resposta deixa a interação expirar e o Discord registra
        # 10062 Unknown interaction mesmo que a ação tenha sido aplicada.
        if not await safe_defer_interaction(
            interaction,
            thinking=False,
            ephemeral=True,
            log=logger,
            label="color_roles.public_pick",
        ):
            return

        if guild is None or member is None:
            await reply("Esse painel só funciona dentro de um servidor.")
            return
        cfg = self._get_config(guild.id)
        if not bool(cfg.get("enabled", False) and int(cfg.get("channel_id") or 0)):
            await reply("A função de cargos de cor está desativada neste servidor.")
            return
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        interaction_message_id = int(getattr(interaction.message, "id", 0) or 0)
        if interaction_message_id not in message_ids:
            await reply(str((cfg.get("templates") or {}).get("missing_panel") or "Esse painel não é mais o oficial."))
            return
        panel_index = message_ids.index(interaction_message_id) + 1
        if int(slot_number) not in self._get_panel_slot_numbers(guild.id, panel_index):
            await reply(str((cfg.get("templates") or {}).get("missing_panel") or "Essa opção não faz mais parte deste painel."))
            return
        slot = self._get_slot_config(guild.id, slot_number)
        role_id = int(slot.get("role_id") or 0)
        target_role = guild.get_role(role_id) if role_id else None
        if target_role is None:
            target_role = await self._ensure_slot_role(guild, slot_number)
            role_id = int(target_role.id) if target_role else 0
        if role_id <= 0 or target_role is None:
            await reply(str((cfg.get("templates") or {}).get("no_role") or "Essa cor ainda não está configurada."))
            return
        me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if me is None or target_role >= me.top_role:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await reply(text or "não consegui aplicar essa cor por causa da hierarquia de cargos.")
            return
        current_slot_number, current_slot = self._member_current_color_slot(guild, member)
        roles_to_remove = self._member_color_roles(guild, member)
        has_target_role = any(role.id == target_role.id for role in roles_to_remove)
        unmanageable_roles = [role for role in roles_to_remove if me is None or role >= me.top_role]
        if unmanageable_roles:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await reply(text or "não consegui aplicar essa cor por causa da hierarquia de cargos.")
            return
        try:
            if has_target_role or current_slot_number == int(slot_number):
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Remoção de cor pelo painel")
                template = str((cfg.get("templates") or {}).get("remove") or "")
                text = self._render_template(template, member=member, slot=slot, removed_name=str(slot.get("name") or ""))
                removed_name = _normalize_color_name(str(slot.get("name") or ""))
                await reply(text or f"cor {removed_name} removida.")
                self._dispatch_member_color_changed(member, None)
                return
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Troca de cor pelo painel")
            await member.add_roles(target_role, reason="Cor escolhida pelo painel")
        except discord.Forbidden:
            text = self._render_template(str((cfg.get("templates") or {}).get("hierarchy") or ""), member=member, slot=slot)
            await reply(text or "não consegui aplicar essa cor por causa da hierarquia de cargos.")
            return
        except Exception as exc:
            logger.exception("[color_roles] falha ao aplicar cor pública")
            await reply(f"não consegui aplicar essa cor agora ({type(exc).__name__}).")
            return
        if current_slot:
            template = str((cfg.get("templates") or {}).get("switch") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""), removed_name=str(current_slot.get("name") or ""))
        else:
            template = str((cfg.get("templates") or {}).get("apply") or "")
            text = self._render_template(template, member=member, slot=slot, added_name=str(slot.get("name") or ""))
        applied_name = _normalize_color_name(str(slot.get("name") or ""))
        await reply(text or f"cor {applied_name} aplicada.")
        self._dispatch_member_color_changed(member, self._slot_effective_hex(guild.id, slot))

    async def _delete_existing_panel_messages(self, guild_id: int):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        for message_id in message_ids:
            if channel is None:
                break
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except Exception:
                pass

    def _slot_effective_hex(self, guild_id: int, slot: dict[str, Any]) -> str:
        guild = self.bot.get_guild(int(guild_id))
        role_id = int(slot.get("role_id") or 0)
        role = guild.get_role(role_id) if guild is not None and role_id else None
        if role is not None:
            value = int(getattr(getattr(role, "colour", None), "value", 0) or 0)
            if value > 0:
                return f"#{value:06x}"
            return "#99aab5"
        role_hex = _clean_hex(str(slot.get("role_hex") or ""), "")
        text_hex = _clean_hex(str(slot.get("text_hex") or ""), "")
        slot_number = int(slot.get("number") or 0)
        preset_hex = str(_default_slot_payload(slot_number or 1).get("role_hex") or "#5865f2")
        if slot_number != 30 and role_id == 0 and role_hex == "#ffffff" and text_hex == "#ffffff":
            return preset_hex
        return role_hex or text_hex or preset_hex

    def _make_block_image(self, guild_id: int, block_index: int, *, filename: str | None = None) -> discord.File:
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow não está disponível para gerar as imagens do painel de cores.")
        slot_numbers = self._get_panel_slot_numbers(guild_id, block_index)
        if not slot_numbers:
            raise ValueError("O painel precisa ter pelo menos uma opção.")
        cfg = self._get_config(guild_id)
        slots = cfg.get("slots") or {}
        rows = max(1, (len(slot_numbers) + 1) // 2)
        width, height = 900, max(92, rows * 65 + 18)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        number_font = _font(34, bold=True, kind="math")
        name_font = _font(34, bold=True, kind="mono")
        x_left, x_right = 18, 465
        shadow = (0, 0, 0, 190)

        for position, slot_number in enumerate(slot_numbers, start=1):
            slot = dict(slots.get(str(slot_number), {}) or _default_slot_payload(slot_number))
            name = str(slot.get("name") or f"Cor {position}").strip()
            number_label = _math_sans_bold(str(position))
            prefix_label = f"{number_label}."
            hex_color = self._slot_effective_hex(guild_id, slot)
            item_index = position - 1
            x = x_left if item_index % 2 == 0 else x_right
            y = 10 + (item_index // 2) * 65

            def _measure_width(label_text: str, font_obj) -> int:
                try:
                    bbox = draw.textbbox((0, 0), label_text, font=font_obj)
                    return max(0, int(bbox[2] - bbox[0]))
                except Exception:
                    return int(draw.textlength(label_text, font=font_obj))

            name_x = x + _measure_width(prefix_label, number_font) + 12
            draw.text((x + 2, y + 2), prefix_label, font=number_font, fill=shadow)
            draw.text((x, y), prefix_label, font=number_font, fill=hex_color)
            if name:
                draw.text((name_x + 2, y + 2), name, font=name_font, fill=shadow)
                draw.text((name_x, y), name, font=name_font, fill=hex_color)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=filename or f"colors-{block_index}.png")

    def _public_message_kwargs(self, guild_id: int, block_index: int) -> dict[str, Any]:
        filename = f"colors-{block_index}.png"
        return {
            "file": self._make_block_image(guild_id, block_index, filename=filename),
            "view": _ColorPublicPanelView(self, guild_id, block_index),
        }

    async def _post_public_panel(self, channel: discord.abc.Messageable, guild: discord.Guild) -> list[int]:
        message_ids: list[int] = []
        panel_count = self._get_panel_count(guild.id)
        for block_index in range(1, panel_count + 1):
            kwargs = self._public_message_kwargs(guild.id, block_index)
            message = await channel.send(**kwargs)
            message_ids.append(int(message.id))
            if _message_supports_slots(block_index):
                key = (guild.id, block_index, int(message.id))
                try:
                    self.bot.add_view(kwargs["view"], message_id=int(message.id))
                except Exception:
                    pass
                self._public_views_registered.add(key)
        return message_ids

    async def _rebuild_public_panel(self, guild_id: int):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        guild = self.bot.get_guild(guild_id)
        if channel is None or guild is None:
            return
        await self._delete_existing_panel_messages(guild_id)
        message_ids = await self._post_public_panel(channel, guild)
        cfg["message_ids"] = message_ids
        await self._save_config(guild_id, cfg)

    async def _refresh_public_panel_messages(self, guild_id: int, *, block_indices: list[int] | None = None):
        cfg = self._get_config(guild_id)
        channel_id = int(cfg.get("channel_id") or 0)
        message_ids = [int(mid) for mid in (cfg.get("message_ids") or []) if mid]
        panel_count = self._get_panel_count(guild_id)
        if not channel_id or not message_ids:
            return
        if len(message_ids) != panel_count:
            await self._rebuild_public_panel(guild_id)
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        targets = block_indices or list(range(1, panel_count + 1))
        for block_index in targets:
            if block_index < 1 or block_index > len(message_ids):
                continue
            message_id = message_ids[block_index - 1]
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                continue
            try:
                filename = f"colors-{block_index}.png"
                file = self._make_block_image(guild_id, block_index, filename=filename)
                view = _ColorPublicPanelView(self, guild_id, block_index)
                await message.edit(content=None, embed=None, embeds=[], attachments=[file], view=view)
                key = (guild_id, block_index, message_id)
                try:
                    self.bot.add_view(view, message_id=message_id)
                except Exception:
                    pass
                self._public_views_registered.add(key)
            except Exception:
                pass

    def _is_admin(self, member: discord.Member | None) -> bool:
        return bool(member and member.guild_permissions.administrator)

    async def _consume_color_command_cooldown(self, guild_id: int) -> float:
        now = time.monotonic()
        last = float(self._color_panel_cd.get(guild_id, 0.0) or 0.0)
        if now - last < COLOR_COMMAND_COOLDOWN:
            return COLOR_COMMAND_COOLDOWN - (now - last)
        self._color_panel_cd[guild_id] = now
        return 0.0

    async def _delete_message_after(self, message: discord.Message | None, delay: float = COLOR_COMMAND_CLEANUP_DELAY):
        if message is None:
            return
        try:
            await asyncio.sleep(max(0.0, float(delay)))
            await message.delete()
        except Exception:
            pass

    @commands.command(name="color")
    @commands.guild_only()
    async def color_command(self, ctx: commands.Context):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.send("Só administradores podem convocar o painel de cores.")
            return
        remaining = await self._consume_color_command_cooldown(ctx.guild.id)
        if remaining > 0:
            await ctx.send(f"Espere {remaining:.0f}s para convocar o painel de cores de novo.")
            return
        await self._delete_existing_panel_messages(ctx.guild.id)
        message_ids = await self._post_public_panel(ctx.channel, ctx.guild)
        cfg = self._get_config(ctx.guild.id)
        cfg["enabled"] = True
        cfg["channel_id"] = int(ctx.channel.id)
        cfg["message_ids"] = message_ids
        await self._save_config(ctx.guild.id, cfg)
        await self._refresh_public_panel_messages(ctx.guild.id)
        confirmation = await ctx.send(f"Painel de cores publicado com {self._get_panel_count(ctx.guild.id)} mensagem(ns).")
        asyncio.create_task(self._delete_message_after(confirmation))
        asyncio.create_task(self._delete_message_after(getattr(ctx, "message", None)))

    @commands.command(name="coloredit")
    @commands.guild_only()
    async def coloredit_command(self, ctx: commands.Context):
        if not self._is_admin(getattr(ctx, "author", None)):
            await ctx.send("Só administradores podem abrir o editor de cores.")
            return
        key = (ctx.guild.id, ctx.author.id)
        old_id = self._active_edit_messages.get(key)
        if old_id:
            try:
                old_msg = await ctx.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
        try:
            view = _ColorUnifiedEditView(self, guild_id=ctx.guild.id, owner_id=ctx.author.id)
            payload = view.editor_message_payload()
            msg = await ctx.channel.send(view=view, files=payload["attachments"])
        except Exception as e:
            await ctx.send(f"não consegui abrir o editor de cores: {e}")
            return
        view.message = msg
        self._active_edit_messages[key] = int(msg.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ColorRolesCog(bot))
