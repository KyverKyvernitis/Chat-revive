from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def _load_renderer():
    package_name = "games_profile_renderer_tests_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "cogs" / "games")]
    sys.modules[package_name] = package

    for module_name, module_path in (
        (f"{package_name}.rank_renderer", ROOT / "cogs" / "games" / "rank_renderer.py"),
        (
            f"{package_name}.chip_profile_renderer",
            ROOT / "cogs" / "games" / "chip_profile_renderer.py",
        ),
    ):
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.chip_profile_renderer"]


RENDERER = _load_renderer()
ChipProfileData = RENDERER.ChipProfileData
build_profile_badges = RENDERER.build_profile_badges
build_profile_metrics = RENDERER.build_profile_metrics
prepare_profile_assets = RENDERER.prepare_profile_assets
render_chip_profile = RENDERER.render_chip_profile


def _png(color: tuple[int, int, int], size: tuple[int, int] = (320, 240)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _two_frame_gif() -> bytes:
    output = BytesIO()
    first = Image.new("RGB", (400, 180), (240, 30, 20))
    second = Image.new("RGB", (400, 180), (20, 230, 40))
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return output.getvalue()


class GamesChipProfileRendererTests(unittest.TestCase):
    def test_banner_animation_uses_first_frame_and_avatar_controls_accent(self) -> None:
        assets = prepare_profile_assets(
            _png((25, 80, 235)),
            _two_frame_gif(),
            "Perfil global",
        )
        with Image.open(BytesIO(assets.banner_png)) as banner:
            red, green, blue, _alpha = banner.convert("RGBA").getpixel((480, 80))

        self.assertGreater(red, green)
        self.assertGreater(red, blue)
        self.assertGreater(assets.accent_rgb[2], assets.accent_rgb[0])

    def test_optional_zero_values_and_non_actionable_badges_are_hidden(self) -> None:
        basic = ChipProfileData("Nome global", 190, 0, 0, 1)
        complete = ChipProfileData(
            "Nome global",
            190,
            12,
            -7,
            1,
            race_name="Sortudo",
            achievement_count=2,
            daily_available=True,
            recharge_available=True,
        )

        self.assertEqual(
            [(metric.kind, metric.value) for metric in build_profile_metrics(basic)],
            [("chips", "190"), ("rank", "#1")],
        )
        self.assertEqual(build_profile_badges(basic), ())
        self.assertEqual(
            [metric.kind for metric in build_profile_metrics(complete)],
            ["chips", "rank", "bonus", "weekly"],
        )
        self.assertEqual(build_profile_metrics(complete)[-1].label, "SEMANAL")
        self.assertEqual(len(build_profile_badges(complete)), 4)

    def test_complete_profile_name_is_not_cut_when_it_fits(self) -> None:
        font = RENDERER._load_font(40, bold=False)
        fallback_fonts = RENDERER._load_name_fallback_fonts(40)
        draw = RENDERER.ImageDraw.Draw(Image.new("RGB", (960, 120)))
        name = "C.❂.R.E ₍^. .^₎Ⳋ"

        safe = RENDERER.sanitize_profile_name(name, font, fallback_fonts=fallback_fonts)
        fitted = RENDERER._fit_mixed_text(draw, safe, font, fallback_fonts, 660)

        self.assertTrue(fallback_fonts)
        self.assertEqual(safe, name)
        self.assertEqual(fitted, name)
        self.assertFalse(RENDERER._font_supports_character(font, "Ⳋ"))
        self.assertTrue(RENDERER._font_supports_character(fallback_fonts[0], "Ⳋ"))
        self.assertTrue(
            any(
                run == "Ⳋ" and run_font is fallback_fonts[0]
                for run, run_font in RENDERER._mixed_text_runs(
                    name,
                    font,
                    fallback_fonts,
                )
            )
        )

    def test_render_is_valid_with_no_banner_long_unicode_name_and_int64_values(self) -> None:
        data = ChipProfileData(
            "Nome global muito longo \ufe0f\u200d com caracteres diferentes Ω Ж",
            -9_223_372_036_854_775_808,
            9_223_372_036_854_775_807,
            -9_223_372_036_854_775_808,
            12,
            achievement_count=3,
        )
        assets = prepare_profile_assets(_png((215, 86, 170)), None, data.display_name)
        payload = render_chip_profile(data, assets)

        with Image.open(BytesIO(payload)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (960, 460))

    def test_name_uses_regular_white_font_instead_of_avatar_accent(self) -> None:
        source = (ROOT / "cogs" / "games" / "chip_profile_renderer.py").read_text(encoding="utf-8")
        self.assertIn("name_font = _load_font(40, bold=False)", source)
        self.assertIn("fill=(247, 248, 251, 255)", source)
        self.assertGreater(RENDERER.NAME_BASELINE_Y, 207)


if __name__ == "__main__":
    unittest.main()
