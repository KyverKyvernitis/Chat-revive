from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GamesChipProfileRouteTests(unittest.TestCase):
    def test_prefix_and_plain_text_profile_share_the_image_sender(self) -> None:
        command_source = (ROOT / "cogs" / "games" / "__init__.py").read_text(encoding="utf-8")
        router_source = (
            ROOT / "cogs" / "games" / "handlers" / "message_router.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "await self._send_chip_profile(ctx.reply, ctx.author, mention_author=False)",
            command_source,
        )
        self.assertIn(
            "await self._send_chip_profile(message.channel.send, message.author)",
            router_source,
        )

    def test_profile_component_is_neutral_image_only_with_text_fallback(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        view_source = base_source.split("def _make_chip_profile_view", 1)[1].split(
            "async def _send_chip_profile", 1
        )[0]
        sender_source = base_source.split("async def _send_chip_profile", 1)[1].split(
            "def _make_chip_balance_view", 1
        )[0]

        self.assertIn("discord.ui.MediaGallery", view_source)
        self.assertIn("attachment://{PROFILE_FILENAME}", view_source)
        self.assertNotIn("discord.ui.TextDisplay", view_source)
        self.assertNotIn("accent_color", view_source)
        self.assertIn("view=self._make_chip_balance_view(member)", sender_source)

    def test_global_identity_banner_and_cache_rules_are_explicit(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        cache_source = (
            ROOT / "cogs" / "games" / "services" / "chip_profile_cache.py"
        ).read_text(encoding="utf-8")
        renderer_source = (
            ROOT / "cogs" / "games" / "chip_profile_renderer.py"
        ).read_text(encoding="utf-8")

        self.assertIn('getattr(member, "global_name"', base_source)
        self.assertNotIn("member.display_name", base_source.split("def _chip_profile_global_name", 1)[1].split("def _build_chip_profile_data", 1)[0])
        self.assertIn('getattr(fetched_user, "banner", None)', cache_source)
        self.assertIn("opened.seek(0)", renderer_source)
        self.assertIn("MAX_PROFILE_IMAGES = 48", cache_source)
        self.assertIn("asyncio.to_thread", cache_source)


if __name__ == "__main__":
    unittest.main()

