from __future__ import annotations

import importlib.util
import sys
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


MODULE_PATH = Path(__file__).resolve().parents[1] / "cogs" / "games" / "rank_renderer.py"
SPEC = importlib.util.spec_from_file_location("games_rank_renderer_for_tests", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
RANK_RENDERER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RANK_RENDERER
SPEC.loader.exec_module(RANK_RENDERER)

RankRenderRow = RANK_RENDERER.RankRenderRow
assign_competition_positions = RANK_RENDERER.assign_competition_positions
format_number = RANK_RENDERER.format_number
format_weekly_delta = RANK_RENDERER.format_weekly_delta
prepare_avatar_thumbnail = RANK_RENDERER.prepare_avatar_thumbnail
render_rank_image = RANK_RENDERER.render_rank_image
sanitize_for_font = RANK_RENDERER._sanitize_for_font
load_font = RANK_RENDERER._load_font


class GamesRankRendererTests(unittest.TestCase):
    def test_competition_rank_keeps_ties_and_stable_order(self) -> None:
        rows = assign_competition_positions(
            [
                {"user_id": 4, "display_name": "Zeta", "chips": 80},
                {"user_id": 2, "display_name": "Beta", "chips": 100},
                {"user_id": 1, "display_name": "Alpha", "chips": 120},
                {"user_id": 3, "display_name": "Ana", "chips": 100},
            ]
        )

        self.assertEqual([row["user_id"] for row in rows], [1, 3, 2, 4])
        self.assertEqual([row["position"] for row in rows], [1, 2, 2, 4])

    def test_number_and_week_delta_format_are_integer_only(self) -> None:
        self.assertEqual(format_number(1234567), "1.234.567")
        self.assertEqual(format_number(-1200), "-1.200")
        self.assertEqual(format_weekly_delta(37), "+37")
        self.assertEqual(format_weekly_delta(-8), "-8")
        self.assertEqual(format_weekly_delta(0), "0")

    def test_render_top_ten_is_one_valid_image_with_week_colors(self) -> None:
        rows = []
        weekly_values = (37, -12, 0, 5, -2, 8, 1, -1, 22, 3)
        for index, weekly in enumerate(weekly_values, start=1):
            rows.append(
                RankRenderRow(
                    position=index,
                    user_id=index,
                    display_name=("Nome extremamente longo para testar corte visual " if index == 1 else "Jogador ") + str(index),
                    chips=250 - index * 13 if index != 10 else -20,
                    bonus_chips=index * 4,
                    weekly_delta=weekly,
                    avatar_png=prepare_avatar_thumbnail("imagem inválida".encode(), f"Jogador {index}"),
                )
            )

        payload = render_rank_image(rows)
        with Image.open(BytesIO(payload)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size[0], 960)
            self.assertLess(image.size[1], 1000)
            color_counts = image.convert("RGB").getcolors(maxcolors=2_000_000) or []
            colors = {color for _count, color in color_counts}

        self.assertIn((65, 209, 122), colors)
        self.assertIn((244, 86, 98), colors)
        self.assertIn((151, 162, 174), colors)

    def test_render_empty_rank_remains_valid(self) -> None:
        payload = render_rank_image([])
        with Image.open(BytesIO(payload)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (960, 112))

    def test_unsupported_name_glyphs_are_removed_instead_of_becoming_boxes(self) -> None:
        font = load_font(25, bold=True)
        self.assertEqual(sanitize_for_font("Core\ufe0f\u200d", font), "Core")


if __name__ == "__main__":
    unittest.main()
