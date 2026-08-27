from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "cogs" / "games" / "services" / "achievement_notices.py"
SPEC = importlib.util.spec_from_file_location("games_achievement_notices_for_tests", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ACHIEVEMENT_NOTICES = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ACHIEVEMENT_NOTICES
SPEC.loader.exec_module(ACHIEVEMENT_NOTICES)


class GamesAchievementGroupingTests(unittest.TestCase):
    def test_merge_preserves_unlock_order_and_removes_duplicates(self) -> None:
        merged = ACHIEVEMENT_NOTICES.merge_achievement_keys(
            ("lets_go_gambling", "first_game"),
            ("first_game", "roulette_first_loss", ""),
        )

        self.assertEqual(
            merged,
            ("lets_go_gambling", "first_game", "roulette_first_loss"),
        )

    def test_burst_uses_sliding_window_with_a_hard_lifetime_cap(self) -> None:
        burst = ACHIEVEMENT_NOTICES.AchievementNoticeBurst(
            achievement_keys=("first_game",),
            started_at=100.0,
            last_at=100.0,
            message=object(),
        )
        self.assertTrue(burst.can_merge(115.0))
        self.assertTrue(burst.is_expired(115.001))

        burst.last_at = 144.0
        self.assertTrue(burst.can_merge(145.0))
        self.assertTrue(burst.is_expired(145.001))

    def test_sender_resends_safely_without_waiting_in_the_game_handler(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        sender_source = base_source.split("async def _send_achievement_notices", 1)[1].split(
            "async def _send_achievement_notice",
            1,
        )[0]

        self.assertIn("_achievement_notice_key(channel, guild_id, user_id)", sender_source)
        self.assertIn("previous_burst.can_merge(now)", sender_source)
        self.assertIn("AchievementNoticeBurst(", sender_source)
        self.assertIn("self._ensure_achievement_notice_cleanup_task()", sender_source)
        self.assertNotIn("asyncio.sleep", sender_source)
        self.assertLess(
            sender_source.index("sent_ok, sent_message = await self._dispatch_achievement_notice"),
            sender_source.index("await self._delete_replaced_achievement_notice(previous_message)"),
        )
        self.assertIn('name="games-achievement-notice-cleanup"', base_source)
        cog_source = (ROOT / "cogs" / "games" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("await self._close_achievement_notice_groups()", cog_source)

    def test_single_and_grouped_titles_keep_final_progress(self) -> None:
        base_source = (ROOT / "cogs" / "games" / "services" / "base.py").read_text(encoding="utf-8")
        self.assertIn('title = "Conquista desbloqueada"', base_source)
        self.assertIn('title = f"{len(items)} conquistas desbloqueadas"', base_source)
        self.assertIn('content = f"### 🏆 {title} ({count}/{total})', base_source)

    def test_same_result_is_sent_as_one_batch_per_user(self) -> None:
        roulette_source = (ROOT / "cogs" / "games" / "games" / "roleta.py").read_text(encoding="utf-8")
        target_source = (ROOT / "cogs" / "games" / "games" / "alvo.py").read_text(encoding="utf-8")

        self.assertEqual(
            roulette_source.count(
                'achievement_keys = (["first_game"] if first_game_unlocked else []) + roulette_achievements'
            ),
            2,
        )
        self.assertIn("achievement_notices_by_user", target_source)
        self.assertIn("await self._send_achievement_notices(", target_source)


if __name__ == "__main__":
    unittest.main()
