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

        self.assertIn("discord.ui.MediaGallery", base_source)
        self.assertIn("attachment://{RANK_FILENAME}", base_source)
        self.assertNotIn("Atualização automática", base_source)
        self.assertNotIn("semana de segunda a domingo", base_source)


if __name__ == "__main__":
    unittest.main()
