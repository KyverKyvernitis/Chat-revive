from __future__ import annotations

import colorsys
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .rank_renderer import format_number, format_weekly_delta


CANVAS_WIDTH = 960
CANVAS_HEIGHT = 460
BANNER_HEIGHT = 268
AVATAR_SIZE = 196
TOKEN_SIZE = 34
NAME_BASELINE_Y = 221
BADGE_FONT_SIZE = 16
BADGE_ROWS = ((394, 424), (429, 459))
_NAME_FALLBACK_FONT_PATH = (
    Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansCoptic-Regular.ttf"
)
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
MEDIANCUT = getattr(
    getattr(Image, "Quantize", Image),
    "MEDIANCUT",
    getattr(Image, "MEDIANCUT", 0),
)


@dataclass(frozen=True, slots=True)
class ChipProfileData:
    display_name: str
    chips: int
    bonus_chips: int
    weekly_delta: int
    rank_position: int | None
    race_name: str | None = None
    achievement_count: int = 0
    daily_available: bool = False
    recharge_available: bool = False
    achievement_total: int = 0


@dataclass(frozen=True, slots=True)
class PreparedProfileAssets:
    avatar_png: bytes
    banner_png: bytes
    accent_rgb: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class ProfileMetric:
    kind: str
    label: str
    value: str
    icon_kind: str | None = None


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    family = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for candidate in (
        family,
        f"/usr/share/fonts/truetype/dejavu/{family}",
        f"/usr/local/share/fonts/{family}",
    ):
        try:
            return ImageFont.truetype(candidate, size=max(8, int(size)))
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _glyph_signature(font: ImageFont.ImageFont, value: str) -> tuple[tuple[int, int], bytes] | None:
    try:
        mask = font.getmask(value)
        return mask.size, bytes(mask)
    except Exception:
        return None


def _font_supports_character(font: ImageFont.ImageFont, char: str) -> bool:
    if char.isascii() and char.isprintable():
        return True
    signature = _glyph_signature(font, char)
    missing = _glyph_signature(font, "\U0010ffff")
    return signature is not None and (missing is None or signature != missing)


@lru_cache(maxsize=16)
def _load_name_fallback_fonts(size: int) -> tuple[ImageFont.ImageFont, ...]:
    """Fontes pequenas e locais usadas apenas nos glifos ausentes da fonte principal."""
    try:
        return (ImageFont.truetype(str(_NAME_FALLBACK_FONT_PATH), size=max(8, int(size))),)
    except (OSError, ValueError):
        return ()


def sanitize_profile_name(
    value: object,
    font: ImageFont.ImageFont,
    *,
    fallback: str = "Usuário",
    fallback_fonts: Sequence[ImageFont.ImageFont] = (),
) -> str:
    """Mantém o nome global legível sem deixar controles ou glifos quebrados."""
    cleaned: list[str] = []
    for char in unicodedata.normalize("NFC", str(value or "")):
        codepoint = ord(char)
        if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
            continue
        if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            continue
        if char.isascii() and char.isprintable():
            cleaned.append(char)
            continue
        if not _font_supports_character(font, char) and not any(
            _font_supports_character(candidate, char) for candidate in fallback_fonts
        ):
            continue
        cleaned.append(char)
    return " ".join("".join(cleaned).split()).strip() or fallback


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    value = str(text or "Usuário")
    if draw.textlength(value, font=font) <= max_width:
        return value
    suffix = "…"
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle].rstrip() + suffix
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + suffix


def _font_for_character(
    char: str,
    primary_font: ImageFont.ImageFont,
    fallback_fonts: Sequence[ImageFont.ImageFont],
) -> ImageFont.ImageFont:
    if _font_supports_character(primary_font, char):
        return primary_font
    for font in fallback_fonts:
        if _font_supports_character(font, char):
            return font
    return primary_font


def _mixed_text_runs(
    text: str,
    primary_font: ImageFont.ImageFont,
    fallback_fonts: Sequence[ImageFont.ImageFont],
) -> tuple[tuple[str, ImageFont.ImageFont], ...]:
    runs: list[tuple[str, ImageFont.ImageFont]] = []
    for char in str(text or ""):
        font = _font_for_character(char, primary_font, fallback_fonts)
        if runs and runs[-1][1] is font:
            previous, previous_font = runs[-1]
            runs[-1] = (previous + char, previous_font)
        else:
            runs.append((char, font))
    return tuple(runs)


def _mixed_textlength(
    draw: ImageDraw.ImageDraw,
    text: str,
    primary_font: ImageFont.ImageFont,
    fallback_fonts: Sequence[ImageFont.ImageFont],
) -> float:
    return sum(
        float(draw.textlength(run, font=font))
        for run, font in _mixed_text_runs(text, primary_font, fallback_fonts)
    )


def _fit_mixed_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    primary_font: ImageFont.ImageFont,
    fallback_fonts: Sequence[ImageFont.ImageFont],
    max_width: int,
) -> str:
    value = str(text or "Usuário")
    if _mixed_textlength(draw, value, primary_font, fallback_fonts) <= max_width:
        return value
    suffix = "…"
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle].rstrip() + suffix
        if _mixed_textlength(draw, candidate, primary_font, fallback_fonts) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + suffix


def _draw_mixed_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    primary_font: ImageFont.ImageFont,
    fallback_fonts: Sequence[ImageFont.ImageFont],
    *,
    fill: tuple[int, int, int, int],
) -> None:
    cursor_x, baseline_y = position
    for run, font in _mixed_text_runs(text, primary_font, fallback_fonts):
        draw.text((cursor_x, baseline_y), run, font=font, fill=fill, anchor="ls")
        cursor_x += float(draw.textlength(run, font=font))


def _initials(display_name: str) -> str:
    parts = [part for part in str(display_name or "").strip().split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def _decode_first_frame(source: bytes | None) -> Image.Image | None:
    if not source:
        return None
    try:
        with Image.open(BytesIO(source)) as opened:
            opened.seek(0)
            return opened.convert("RGBA")
    except Exception:
        return None


def _fallback_avatar(display_name: str, size: int = 256) -> Image.Image:
    palette = (
        (48, 180, 166),
        (83, 122, 224),
        (185, 94, 193),
        (224, 126, 76),
    )
    seed = sum(ord(char) for char in str(display_name or "Usuário"))
    color = palette[seed % len(palette)]
    image = Image.new("RGBA", (size, size), (*color, 255))
    draw = ImageDraw.Draw(image)
    font = _load_font(max(20, int(size * 0.31)), bold=True)
    initials = sanitize_profile_name(_initials(display_name), font, fallback="?")
    box = draw.textbbox((0, 0), initials, font=font)
    width = box[2] - box[0]
    height = box[3] - box[1]
    draw.text(
        ((size - width) / 2, (size - height) / 2 - box[1]),
        initials,
        font=font,
        fill=(248, 250, 253, 255),
    )
    return image


def _dominant_avatar_color(avatar: Image.Image) -> tuple[int, int, int]:
    sample = avatar.convert("RGBA")
    sample.thumbnail((72, 72), LANCZOS)
    candidates: list[tuple[int, int, int]] = []
    fallback: list[tuple[int, int, int]] = []
    pixel_data = (
        sample.get_flattened_data()
        if hasattr(sample, "get_flattened_data")
        else sample.getdata()
    )
    for red, green, blue, alpha in pixel_data:
        if alpha < 96:
            continue
        fallback.append((red, green, blue))
        light = max(red, green, blue)
        dark = min(red, green, blue)
        if 28 <= light <= 246 and dark <= 232 and light - dark >= 18:
            candidates.append((red, green, blue))
    pixels = candidates or fallback
    if not pixels:
        return (77, 206, 190)

    palette_image = Image.new("RGB", (len(pixels), 1))
    palette_image.putdata(pixels)
    quantized = palette_image.quantize(colors=min(10, len(pixels)), method=MEDIANCUT)
    palette = quantized.getpalette() or []
    weighted: list[tuple[float, tuple[int, int, int]]] = []
    for count, index in quantized.getcolors(maxcolors=256) or []:
        offset = int(index) * 3
        if offset + 2 >= len(palette):
            continue
        rgb = tuple(int(value) for value in palette[offset : offset + 3])
        chroma = max(rgb) - min(rgb)
        brightness = sum(rgb) / 3
        usability = 0.65 + min(1.0, chroma / 96) * 0.45
        if brightness < 32 or brightness > 242:
            usability *= 0.25
        weighted.append((float(count) * usability, rgb))
    raw = max(weighted, default=(0.0, (77, 206, 190)), key=lambda item: item[0])[1]

    hue, lightness, saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in raw))
    lightness = min(0.73, max(0.61, lightness))
    saturation = min(0.88, max(0.52, saturation))
    adjusted = colorsys.hls_to_rgb(hue, lightness, saturation)
    return tuple(int(round(channel * 255)) for channel in adjusted)


def _circle_png(image: Image.Image, size: int) -> bytes:
    avatar = ImageOps.fit(image.convert("RGBA"), (size, size), method=LANCZOS, centering=(0.5, 0.5))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    avatar.putalpha(mask)
    output = BytesIO()
    avatar.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _prepare_banner(image: Image.Image) -> bytes:
    banner = ImageOps.fit(
        image.convert("RGB"),
        (CANVAS_WIDTH, BANNER_HEIGHT),
        method=LANCZOS,
        centering=(0.5, 0.5),
    )
    banner = ImageEnhance.Brightness(banner).enhance(0.44).convert("RGBA")
    overlay = Image.new("RGBA", banner.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for y in range(BANNER_HEIGHT):
        progress = y / max(1, BANNER_HEIGHT - 1)
        alpha = int(22 + 76 * (progress ** 1.8))
        overlay_draw.line((0, y, CANVAS_WIDTH, y), fill=(8, 10, 16, alpha))
    banner = Image.alpha_composite(banner, overlay)
    output = BytesIO()
    banner.save(output, format="PNG", compress_level=3)
    return output.getvalue()


def prepare_profile_assets(
    avatar_source: bytes | None,
    banner_source: bytes | None,
    display_name: str,
) -> PreparedProfileAssets:
    """Prepara avatar e banner uma vez; animações usam deliberadamente o frame zero."""
    avatar = _decode_first_frame(avatar_source) or _fallback_avatar(display_name)
    accent = _dominant_avatar_color(avatar)
    banner = _decode_first_frame(banner_source)
    if banner is None:
        banner = ImageOps.fit(
            avatar.convert("RGB"),
            (CANVAS_WIDTH, BANNER_HEIGHT),
            method=LANCZOS,
            centering=(0.5, 0.5),
        ).filter(ImageFilter.GaussianBlur(radius=24))
    return PreparedProfileAssets(
        avatar_png=_circle_png(avatar, AVATAR_SIZE),
        banner_png=_prepare_banner(banner),
        accent_rgb=accent,
    )


def build_profile_metrics(data: ChipProfileData) -> tuple[ProfileMetric, ...]:
    normal_icon = "debt" if int(data.chips) < 0 else "normal"
    metrics: list[ProfileMetric] = [
        ProfileMetric("chips", "FICHAS", format_number(data.chips), normal_icon),
    ]
    if int(data.bonus_chips) > 0:
        metrics.append(ProfileMetric("bonus", "BÔNUS", format_number(data.bonus_chips), "bonus"))
    metrics.append(
        ProfileMetric(
            "rank",
            "RANK",
            f"#{int(data.rank_position)}" if data.rank_position is not None else "—",
        )
    )
    if int(data.weekly_delta) != 0:
        weekly_icon = "normal" if int(data.weekly_delta) > 0 else "debt"
        metrics.append(
            ProfileMetric("weekly", "SEMANAL", format_weekly_delta(data.weekly_delta), weekly_icon)
        )
    return tuple(metrics)


def build_profile_badges(data: ChipProfileData) -> tuple[str, ...]:
    badges: list[str] = []
    if data.race_name:
        badges.append(f"RAÇA · {data.race_name}")
    achievement_count = max(0, int(data.achievement_count))
    achievement_total = max(achievement_count, int(data.achievement_total))
    if achievement_count > 0:
        badges.append(f"Conquistas • {achievement_count} de {achievement_total}")
    if data.daily_available:
        badges.append("_daily disponível")
    if data.recharge_available:
        badges.append("_recarga disponível")
    return tuple(badges)


def build_profile_accessible_description(data: ChipProfileData) -> str:
    parts = [f"Perfil de fichas de {data.display_name}", f"{format_number(data.chips)} fichas"]
    if data.rank_position is not None:
        parts.append(f"posição {int(data.rank_position)} no rank")
    if int(data.bonus_chips) > 0:
        parts.append(f"{format_number(data.bonus_chips)} fichas bônus")
    if int(data.weekly_delta) != 0:
        parts.append(f"{format_weekly_delta(data.weekly_delta)} nesta semana")
    return "; ".join(parts)[:256]


def _open_prepared(source: bytes, size: tuple[int, int]) -> Image.Image | None:
    try:
        with Image.open(BytesIO(source)) as opened:
            return ImageOps.contain(opened.convert("RGBA"), size, LANCZOS)
    except Exception:
        return None


def _token_image(source: bytes | None, kind: str) -> Image.Image:
    if source:
        opened = _open_prepared(source, (TOKEN_SIZE, TOKEN_SIZE))
        if opened is not None:
            return opened
    colors = {
        "normal": (197, 205, 214, 255),
        "bonus": (245, 139, 52, 255),
        "debt": (235, 81, 94, 255),
    }
    color = colors.get(kind, colors["normal"])
    token = Image.new("RGBA", (TOKEN_SIZE, TOKEN_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(token)
    inset = 2
    draw.ellipse((inset, inset, TOKEN_SIZE - inset - 1, TOKEN_SIZE - inset - 1), fill=color)
    draw.ellipse(
        (TOKEN_SIZE * 0.25, TOKEN_SIZE * 0.25, TOKEN_SIZE * 0.75, TOKEN_SIZE * 0.75),
        outline=(245, 248, 252, 220),
        width=3,
    )
    if kind == "debt":
        draw.line(
            (TOKEN_SIZE * 0.29, TOKEN_SIZE / 2, TOKEN_SIZE * 0.71, TOKEN_SIZE / 2),
            fill=(255, 255, 255, 245),
            width=3,
        )
    return token


def _metric_value_font(
    draw: ImageDraw.ImageDraw,
    metric: ProfileMetric,
    *,
    cell_width: int,
) -> ImageFont.ImageFont:
    icon_width = TOKEN_SIZE + 8 if metric.icon_kind else 0
    max_text_width = max(48, cell_width - icon_width - 18)
    for size in range(42, 8, -1):
        font = _load_font(size, bold=True)
        if draw.textlength(metric.value, font=font) <= max_text_width:
            return font
    return _load_font(9, bold=True)


def _draw_badges(
    draw: ImageDraw.ImageDraw,
    badges: Sequence[str],
    accent: tuple[int, int, int],
) -> None:
    if not badges:
        return
    font = _load_font(BADGE_FONT_SIZE, bold=True)
    left = 278
    max_x = CANVAS_WIDTH - 32
    horizontal_padding = 16
    gap = 8
    row_index = 0
    x = left
    for badge in badges:
        safe = sanitize_profile_name(badge, font, fallback="")
        if not safe:
            continue

        max_badge_width = max_x - left
        safe = _fit_text(
            draw,
            safe,
            font,
            max_badge_width - (horizontal_padding * 2),
        )
        width = math.ceil(draw.textlength(safe, font=font)) + (horizontal_padding * 2)
        if x + width > max_x:
            row_index += 1
            if row_index >= len(BADGE_ROWS):
                break
            x = left

        y1, y2 = BADGE_ROWS[row_index]
        draw.rounded_rectangle(
            (x, y1, x + width, y2),
            radius=16,
            fill=(*accent, 54),
            outline=(*accent, 165),
            width=1,
        )
        box = draw.textbbox((0, 0), safe, font=font)
        text_y = y1 + ((y2 - y1) - (box[3] - box[1])) / 2 - box[1]
        draw.text(
            (x + horizontal_padding, text_y),
            safe,
            font=font,
            fill=(242, 245, 249, 255),
        )
        x += width + gap


def render_chip_profile(
    data: ChipProfileData,
    assets: PreparedProfileAssets,
    *,
    normal_icon_png: bytes | None = None,
    bonus_icon_png: bytes | None = None,
    debt_icon_png: bytes | None = None,
) -> bytes:
    """Renderiza o perfil compacto sem inventar XP, nível ou reputação."""
    canvas = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (23, 25, 32, 255))
    draw = ImageDraw.Draw(canvas, "RGBA")

    banner = _open_prepared(assets.banner_png, (CANVAS_WIDTH, BANNER_HEIGHT))
    if banner is not None:
        canvas.alpha_composite(banner, (0, 0))

    # Corpo no mesmo preto azulado da referência, sem cor de componente externa.
    draw.rectangle((0, BANNER_HEIGHT, CANVAS_WIDTH, CANVAS_HEIGHT), fill=(24, 26, 34, 255))
    draw.line((0, BANNER_HEIGHT, CANVAS_WIDTH, BANNER_HEIGHT), fill=(53, 57, 68, 220), width=2)

    avatar_x, avatar_y = 50, 82
    avatar = _open_prepared(assets.avatar_png, (AVATAR_SIZE, AVATAR_SIZE))
    draw.ellipse(
        (avatar_x - 7, avatar_y - 7, avatar_x + AVATAR_SIZE + 6, avatar_y + AVATAR_SIZE + 6),
        fill=(14, 16, 22, 255),
        outline=(77, 82, 94, 255),
        width=3,
    )
    if avatar is not None:
        canvas.alpha_composite(avatar, (avatar_x, avatar_y))

    name_font = _load_font(40, bold=False)
    name_fallback_fonts = _load_name_fallback_fonts(40)
    safe_name = sanitize_profile_name(
        data.display_name,
        name_font,
        fallback_fonts=name_fallback_fonts,
    )
    fitted_name = _fit_mixed_text(
        draw,
        safe_name,
        name_font,
        name_fallback_fonts,
        CANVAS_WIDTH - 300,
    )
    _draw_mixed_text(
        draw,
        (276, NAME_BASELINE_Y),
        fitted_name,
        name_font,
        name_fallback_fonts,
        fill=(247, 248, 251, 255),
    )

    accent = tuple(int(max(0, min(255, value))) for value in assets.accent_rgb)
    metrics = build_profile_metrics(data)
    metrics_left = 278
    metrics_right = CANVAS_WIDTH - 30
    metric_weights = [1.65 if metric.kind == "chips" else 1.0 for metric in metrics]
    width_unit = (metrics_right - metrics_left) / max(1.0, sum(metric_weights))
    label_font = _load_font(16, bold=True)
    icons = {
        "normal": _token_image(normal_icon_png, "normal"),
        "bonus": _token_image(bonus_icon_png, "bonus"),
        "debt": _token_image(debt_icon_png, "debt"),
    }
    cell_x = float(metrics_left)
    for metric, weight in zip(metrics, metric_weights):
        cell_width = width_unit * weight
        center_x = cell_x + cell_width / 2
        label = sanitize_profile_name(metric.label, label_font, fallback=metric.label)
        label_width = draw.textlength(label, font=label_font)
        draw.text((center_x - label_width / 2, 291), label, font=label_font, fill=(163, 170, 183, 255))

        value_font = _metric_value_font(draw, metric, cell_width=int(cell_width))
        value_width = draw.textlength(metric.value, font=value_font)
        icon_width = TOKEN_SIZE + 8 if metric.icon_kind else 0
        group_width = value_width + icon_width
        cursor_x = center_x - group_width / 2
        value_box = draw.textbbox((0, 0), metric.value, font=value_font)
        value_y = 360 - (value_box[3] - value_box[1]) - value_box[1]
        draw.text((cursor_x, value_y), metric.value, font=value_font, fill=(*accent, 255))
        if metric.icon_kind:
            icon = icons[metric.icon_kind]
            canvas.alpha_composite(icon, (int(cursor_x + value_width + 8), 324))
        cell_x += cell_width

    draw.line((278, 382, CANVAS_WIDTH - 32, 382), fill=(61, 65, 76, 180), width=1)
    _draw_badges(draw, build_profile_badges(data), accent)

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", compress_level=4)
    return output.getvalue()
