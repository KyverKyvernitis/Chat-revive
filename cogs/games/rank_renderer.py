from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Iterable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


CANVAS_WIDTH = 1200
ROW_HEIGHT = 84
ROW_GAP = 8
MAX_ROWS = 10
AVATAR_SIZE = 62
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


@dataclass(frozen=True, slots=True)
class RankRenderRow:
    position: int
    user_id: int
    display_name: str
    chips: int
    bonus_chips: int
    weekly_delta: int
    avatar_png: bytes | None = None


def format_number(value: int) -> str:
    """Formata inteiros no padrão visual usado pelo bot (1.234)."""
    return f"{int(value):,}".replace(",", ".")


def format_weekly_delta(value: int) -> str:
    amount = int(value)
    if amount > 0:
        return f"+{format_number(amount)}"
    return format_number(amount)


def assign_competition_positions(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Ordena por fichas e atribui posições com empate (1, 2, 2, 4)."""
    normalized: list[dict[str, object]] = []
    for row in rows:
        try:
            user_id = int(row.get("user_id", 0) or 0)
            chips = int(row.get("chips", 0) or 0)
            bonus_chips = max(0, int(row.get("bonus_chips", 0) or 0))
            weekly_delta = int(row.get("weekly_delta", 0) or 0)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        display_name = " ".join(str(row.get("display_name") or "Jogador").split()) or "Jogador"
        normalized.append(
            {
                "user_id": user_id,
                "display_name": display_name[:80],
                "chips": chips,
                "bonus_chips": bonus_chips,
                "weekly_delta": weekly_delta,
                "avatar_key": str(row.get("avatar_key") or ""),
                "member": row.get("member"),
            }
        )

    normalized.sort(
        key=lambda item: (
            -int(item["chips"]),
            str(item["display_name"]).casefold(),
            int(item["user_id"]),
        )
    )
    previous_chips: int | None = None
    current_position = 0
    for index, row in enumerate(normalized, start=1):
        chips = int(row["chips"])
        if previous_chips is None or chips != previous_chips:
            current_position = index
            previous_chips = chips
        row["position"] = current_position
    return normalized


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    family = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = (
        family,
        f"/usr/share/fonts/truetype/dejavu/{family}",
        f"/usr/local/share/fonts/{family}",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=int(size))
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _initials(display_name: str) -> str:
    parts = [part for part in str(display_name or "").strip().split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def prepare_avatar_thumbnail(source: bytes | None, display_name: str, *, size: int = AVATAR_SIZE) -> bytes:
    """Normaliza um avatar para PNG circular; bytes inválidos viram iniciais."""
    target_size = max(24, int(size))
    avatar: Image.Image | None = None
    if source:
        try:
            with Image.open(BytesIO(source)) as opened:
                avatar = ImageOps.fit(
                    opened.convert("RGBA"),
                    (target_size, target_size),
                    method=LANCZOS,
                    centering=(0.5, 0.5),
                )
        except Exception:
            avatar = None

    if avatar is None:
        palette = (
            (49, 174, 160, 255),
            (77, 116, 210, 255),
            (171, 92, 182, 255),
            (214, 119, 72, 255),
        )
        seed = sum(ord(char) for char in str(display_name or ""))
        avatar = Image.new("RGBA", (target_size, target_size), palette[seed % len(palette)])
        draw = ImageDraw.Draw(avatar)
        initials = _initials(display_name)
        font = _load_font(max(12, int(target_size * 0.32)), bold=True)
        box = draw.textbbox((0, 0), initials, font=font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        draw.text(
            ((target_size - width) / 2, (target_size - height) / 2 - box[1]),
            initials,
            font=font,
            fill=(245, 248, 252, 255),
        )

    mask = Image.new("L", (target_size, target_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, target_size - 1, target_size - 1), fill=255)
    avatar.putalpha(mask)
    output = BytesIO()
    avatar.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _open_prepared_png(source: bytes | None, size: tuple[int, int]) -> Image.Image | None:
    if not source:
        return None
    try:
        with Image.open(BytesIO(source)) as opened:
            return ImageOps.contain(opened.convert("RGBA"), size, LANCZOS)
    except Exception:
        return None


def _draw_token_fallback(size: int, color: tuple[int, int, int, int], *, debt: bool = False) -> Image.Image:
    token = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(token)
    inset = max(1, size // 14)
    draw.ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=color)
    draw.ellipse(
        (size * 0.24, size * 0.24, size * 0.76, size * 0.76),
        outline=(245, 248, 252, 210),
        width=max(2, size // 10),
    )
    if debt:
        y = size // 2
        draw.line((size * 0.28, y, size * 0.72, y), fill=(255, 255, 255, 240), width=max(2, size // 10))
    return token


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    value = str(text or "Jogador")
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


def _draw_right_aligned(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    width = int(draw.textlength(text, font=font))
    draw.text((xy[0] - width, xy[1]), text, font=font, fill=fill)


def render_rank_image(
    rows: Sequence[RankRenderRow],
    *,
    guild_name: str,
    week_label: str,
    normal_icon_png: bytes | None = None,
    bonus_icon_png: bytes | None = None,
    debt_icon_png: bytes | None = None,
) -> bytes:
    """Renderiza todo o Top 10 em uma única imagem, sem barras de progresso."""
    visible_rows = list(rows[:MAX_ROWS])
    row_count = max(1, len(visible_rows))
    top_y = 194
    height = top_y + (row_count * ROW_HEIGHT) + (max(0, row_count - 1) * ROW_GAP) + 38

    canvas = Image.new("RGBA", (CANVAS_WIDTH, height), (9, 13, 19, 255))
    draw = ImageDraw.Draw(canvas)

    # Fundo discreto no mesmo clima escuro da referência, com ciano como cor do sistema.
    for y in range(height):
        blend = y / max(1, height - 1)
        color = (
            int(11 + 7 * blend),
            int(16 + 8 * blend),
            int(23 + 9 * blend),
            255,
        )
        draw.line((0, y, CANVAS_WIDTH, y), fill=color)

    title_font = _load_font(42, bold=True)
    guild_font = _load_font(21)
    header_font = _load_font(17, bold=True)
    rank_font = _load_font(27, bold=True)
    name_font = _load_font(24, bold=True)
    value_font = _load_font(23, bold=True)
    empty_font = _load_font(24)

    draw.rounded_rectangle((24, 22, CANVAS_WIDTH - 24, height - 22), radius=30, fill=(15, 21, 29, 246), outline=(43, 57, 69, 255), width=2)
    draw.rounded_rectangle((24, 22, 31, 150), radius=4, fill=(48, 205, 184, 255))
    draw.text((56, 42), "RANK DE FICHAS", font=title_font, fill=(244, 247, 251, 255))
    safe_guild_name = " ".join(str(guild_name or "Servidor").split()) or "Servidor"
    guild_line = f"{safe_guild_name[:80]}  •  {week_label}"
    draw.text((58, 101), guild_line, font=guild_font, fill=(151, 164, 179, 255))

    token_size = 27
    normal_icon = _open_prepared_png(normal_icon_png, (token_size, token_size)) or _draw_token_fallback(token_size, (41, 197, 181, 255))
    bonus_icon = _open_prepared_png(bonus_icon_png, (token_size, token_size)) or _draw_token_fallback(token_size, (244, 143, 59, 255))
    debt_icon = _open_prepared_png(debt_icon_png, (token_size, token_size)) or _draw_token_fallback(
        token_size,
        (235, 76, 88, 255),
        debt=True,
    )

    draw.text((52, 158), "POS.", font=header_font, fill=(126, 140, 156, 255))
    draw.text((202, 158), "JOGADOR", font=header_font, fill=(126, 140, 156, 255))
    canvas.alpha_composite(normal_icon, (702, 153))
    draw.text((737, 158), "NORMAIS", font=header_font, fill=(126, 140, 156, 255))
    canvas.alpha_composite(bonus_icon, (886, 153))
    draw.text((921, 158), "BÔNUS", font=header_font, fill=(126, 140, 156, 255))
    _draw_right_aligned(draw, (1148, 158), "SEMANA", font=header_font, fill=(126, 140, 156, 255))

    if not visible_rows:
        y1 = top_y
        y2 = top_y + ROW_HEIGHT
        draw.rounded_rectangle((42, y1, CANVAS_WIDTH - 42, y2), radius=22, fill=(27, 35, 44, 255))
        message = "Ainda não há jogadores com movimentação de fichas"
        text_width = int(draw.textlength(message, font=empty_font))
        draw.text(((CANVAS_WIDTH - text_width) / 2, y1 + 25), message, font=empty_font, fill=(177, 188, 199, 255))
    else:
        position_colors = {
            1: (255, 213, 74, 255),
            2: (205, 215, 225, 255),
            3: (225, 139, 70, 255),
        }
        for index, row in enumerate(visible_rows):
            y1 = top_y + index * (ROW_HEIGHT + ROW_GAP)
            y2 = y1 + ROW_HEIGHT
            row_fill = (31, 40, 49, 255) if index % 2 == 0 else (27, 35, 44, 255)
            if row.position == 1:
                row_fill = (35, 45, 51, 255)
            draw.rounded_rectangle((42, y1, CANVAS_WIDTH - 42, y2), radius=22, fill=row_fill, outline=(46, 59, 70, 220), width=1)
            accent = position_colors.get(row.position)
            if accent:
                draw.rounded_rectangle((42, y1, 49, y2), radius=4, fill=accent)

            rank_color = position_colors.get(row.position, (183, 194, 205, 255))
            draw.text((62, y1 + 24), f"#{row.position}", font=rank_font, fill=rank_color)

            avatar_bytes = row.avatar_png or prepare_avatar_thumbnail(None, row.display_name)
            avatar = _open_prepared_png(avatar_bytes, (AVATAR_SIZE, AVATAR_SIZE))
            if avatar is None:
                avatar = _open_prepared_png(prepare_avatar_thumbnail(None, row.display_name), (AVATAR_SIZE, AVATAR_SIZE))
            if avatar is not None:
                avatar_x = 121
                avatar_y = y1 + (ROW_HEIGHT - AVATAR_SIZE) // 2
                draw.ellipse((avatar_x - 2, avatar_y - 2, avatar_x + AVATAR_SIZE + 1, avatar_y + AVATAR_SIZE + 1), fill=(57, 75, 88, 255))
                canvas.alpha_composite(avatar, (avatar_x, avatar_y))

            name = _fit_text(draw, row.display_name, name_font, 410)
            draw.text((202, y1 + 27), name, font=name_font, fill=(240, 244, 248, 255))

            active_normal_icon = debt_icon if int(row.chips) < 0 else normal_icon
            canvas.alpha_composite(active_normal_icon, (704, y1 + 28))
            normal_color = (244, 103, 112, 255) if int(row.chips) < 0 else (230, 237, 243, 255)
            _draw_right_aligned(draw, (858, y1 + 27), format_number(row.chips), font=value_font, fill=normal_color)

            canvas.alpha_composite(bonus_icon, (888, y1 + 28))
            _draw_right_aligned(draw, (1022, y1 + 27), format_number(row.bonus_chips), font=value_font, fill=(230, 237, 243, 255))

            weekly = int(row.weekly_delta)
            weekly_color = (65, 209, 122, 255) if weekly > 0 else ((244, 86, 98, 255) if weekly < 0 else (151, 162, 174, 255))
            _draw_right_aligned(draw, (1148, y1 + 27), format_weekly_delta(weekly), font=value_font, fill=weekly_color)

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True, compress_level=7)
    return output.getvalue()
