from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GamesRankRouteTests(unittest.TestCase):
    def test_prefix_and_plain_text_rank_share_the_image_sender(self) -> None:
        command_source = (ROOT / "cogs" / "games" / "__init__.py").read_text(encoding="utf-8")
        router_source = (ROOT / "cogs" / "games" / "handlers" / "message_router.py").read_text(encoding="utf-8")

        self.assertIn("await self._send_chip_rank(\n            ctx.reply", command_source)
        self.assertIn("await self._send_chip_rank(\n                message.channel.send", router_source)
        self.assertNotIn("send(embed=await self._make_chip_leaderboard_embed_async", router_source)

    def test_rank_view_has_image_without_redundant_update_text(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        renderer_source = (ROOT / "cogs" / "games" / "rank_renderer.py").read_text(encoding="utf-8")
        cache_source = (ROOT / "cogs" / "games" / "services" / "rank_cache.py").read_text(encoding="utf-8")
        command_source = (ROOT / "cogs" / "games" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn("discord.ui.MediaGallery", base_source)
        self.assertIn("attachment://{RANK_FILENAME}", base_source)
        self.assertIn(
            'discord.ui.TextDisplay(f"# {guild_name} • Top {response.top_number}")',
            base_source,
        )
        self.assertNotIn("description=response.accessible_description", base_source)
        self.assertNotIn("accent_color=discord.Color.teal()", base_source)
        self.assertNotIn("Atualização automática", base_source)
        self.assertNotIn("semana de segunda a domingo", base_source)
        self.assertNotIn("display_avatar", cache_source)
        self.assertNotIn("member_changed", command_source)
        self.assertIn('f"@{member.name}"', base_source)
        self.assertIn("format_weekly_chip_summary(requester_weekly)", base_source)
        self.assertNotIn('weekly_marker = "🟢"', cache_source)
        self.assertNotIn("RANK DE FICHAS", renderer_source)
        self.assertNotIn("NORMAIS", renderer_source)
        self.assertNotIn("BÔNUS", renderer_source)
        self.assertNotIn("SEMANA", renderer_source)

    def test_rank_pagination_is_public_emoji_only_and_replaces_the_same_image(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        pagination_source = base_source.split("class _ChipRankPaginationView", 1)[1].split(
            "class GincanaBase", 1
        )[0]

        self.assertIn(
            'RANK_PREVIOUS_EMOJI = "<a:k0_SetaE:1542282885153816596>"',
            base_source,
        )
        self.assertIn(
            'RANK_NEXT_EMOJI = "<a:k0_SetaD:1542282957966802986>"',
            base_source,
        )
        self.assertIn("discord.ui.ActionRow(previous_button, next_button)", pagination_source)
        self.assertIn("if include_controls and self.response.page_count > 1:", pagination_source)
        self.assertIn("disabled=source_page <= 0", pagination_source)
        self.assertIn(
            "disabled=source_page >= self.response.page_count - 1",
            pagination_source,
        )
        self.assertIn("async with self._page_lock:", pagination_source)
        self.assertIn("target_page = rank_page_target(", pagination_source)
        self.assertIn("if target_page is None:", pagination_source)
        self.assertIn("interaction.edit_original_response(", pagination_source)
        self.assertIn("attachments=[image]", pagination_source)
        self.assertNotIn("owner_id", pagination_source)
        self.assertNotIn("interaction.user.id", pagination_source)
        self.assertNotIn("Página", pagination_source)
        self.assertNotIn("page_index + 1", pagination_source)


if __name__ == "__main__":
    unittest.main()
