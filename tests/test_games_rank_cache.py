from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]

CREATED_DISCORD_STUB = "discord" not in sys.modules
if CREATED_DISCORD_STUB:
    discord_stub = types.ModuleType("discord")
    discord_stub.Client = object
    discord_stub.Member = object
    discord_stub.User = object
    discord_stub.Guild = object
    discord_stub.HTTPException = type("HTTPException", (Exception,), {})
    sys.modules["discord"] = discord_stub

package = types.ModuleType("games_rank_test_package")
package.__path__ = [str(ROOT / "cogs" / "games")]
services_package = types.ModuleType("games_rank_test_package.services")
services_package.__path__ = [str(ROOT / "cogs" / "games" / "services")]
sys.modules[package.__name__] = package
sys.modules[services_package.__name__] = services_package

renderer_spec = importlib.util.spec_from_file_location(
    "games_rank_test_package.rank_renderer",
    ROOT / "cogs" / "games" / "rank_renderer.py",
)
assert renderer_spec is not None and renderer_spec.loader is not None
renderer_module = importlib.util.module_from_spec(renderer_spec)
sys.modules[renderer_spec.name] = renderer_module
renderer_spec.loader.exec_module(renderer_module)

cache_spec = importlib.util.spec_from_file_location(
    "games_rank_test_package.services.rank_cache",
    ROOT / "cogs" / "games" / "services" / "rank_cache.py",
)
assert cache_spec is not None and cache_spec.loader is not None
cache_module = importlib.util.module_from_spec(cache_spec)
sys.modules[cache_spec.name] = cache_module
cache_spec.loader.exec_module(cache_module)
if CREATED_DISCORD_STUB:
    sys.modules.pop("discord", None)

ChipRankCache = cache_module.ChipRankCache
format_weekly_chip_summary = cache_module.format_weekly_chip_summary
rank_page_count = cache_module.rank_page_count
rank_page_target = cache_module.rank_page_target
rank_page_top = cache_module.rank_page_top


class _FakeAvatar:
    def __init__(self, key: str):
        self.url = f"https://cdn.invalid/{key}.png"

    def replace(self, **kwargs):
        return self

    async def read(self):
        raise OSError("CDN intentionally unavailable in unit test")


class _FakeMember:
    def __init__(self, guild, user_id: int, name: str, *, bot: bool = False):
        self.guild = guild
        self.id = user_id
        self.name = name
        self.display_name = f"Nick {name}"
        self.bot = bot
        self.avatar = _FakeAvatar(f"global-{user_id}")
        self.default_avatar = _FakeAvatar(f"default-{user_id}")
        self.guild_avatar = _FakeAvatar(f"guild-{user_id}")
        self.display_avatar = self.guild_avatar


class _FakeGuild:
    def __init__(self):
        self.id = 55
        self.name = "Servidor Games"
        self.members = {}

    def get_member(self, user_id: int):
        return self.members.get(int(user_id))


class _FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if int(guild_id) == self._guild.id else None


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.listeners = []

    def add_chip_change_listener(self, callback):
        self.listeners.append(callback)

    def remove_chip_change_listener(self, callback):
        self.listeners = [item for item in self.listeners if item != callback]

    def _current_week_key(self):
        return "2026-W35"

    def get_chip_rank_snapshot(self, guild_id: int):
        return list(self.rows)

    def get_user_chips(self, guild_id: int, user_id: int, *, default: int = 100):
        for row in self.rows:
            if int(row["user_id"]) == int(user_id):
                return int(row["chips"])
        return default

    def get_user_chip_week_delta(self, guild_id: int, user_id: int):
        for row in self.rows:
            if int(row["user_id"]) == int(user_id):
                return int(row["weekly_delta"])
        return 0


class GamesRankCacheTests(unittest.TestCase):
    def test_page_titles_use_the_real_cumulative_top(self) -> None:
        self.assertEqual(rank_page_count(0), 1)
        self.assertEqual(rank_page_count(10), 1)
        self.assertEqual(rank_page_count(12), 2)
        self.assertEqual(rank_page_top(0, 12), 10)
        self.assertEqual(rank_page_top(1, 12), 12)
        self.assertEqual(rank_page_top(1, 17), 17)
        self.assertEqual(rank_page_top(1, 20), 20)
        self.assertEqual(rank_page_top(2, 21), 21)

    def test_page_target_rejects_stale_and_boundary_clicks(self) -> None:
        self.assertEqual(rank_page_target(0, 3, 1, 0), 1)
        self.assertEqual(rank_page_target(1, 3, 1, 1), 2)
        self.assertEqual(rank_page_target(1, 3, -1, 1), 0)
        self.assertIsNone(rank_page_target(1, 3, 1, 0))
        self.assertIsNone(rank_page_target(0, 3, -1, 0))
        self.assertIsNone(rank_page_target(2, 3, 1, 2))

    def test_weekly_summary_uses_chip_emojis_and_hides_zero(self) -> None:
        self.assertEqual(
            format_weekly_chip_summary(37),
            "**+37** <:emoji_63:1485041721573249135> ganhas nessa semana",
        )
        self.assertEqual(
            format_weekly_chip_summary(-4),
            "**-4** <:emoji_65:1485043671077228786> perdidas nessa semana",
        )
        self.assertEqual(format_weekly_chip_summary(0), "")

    def test_profile_identity_ignores_guild_nickname_and_avatar(self) -> None:
        guild = _FakeGuild()
        member = _FakeMember(guild, 7, "core.cute")

        self.assertEqual(ChipRankCache._username_tag(member), "@core.cute")
        self.assertEqual(ChipRankCache._profile_avatar_asset(member).url, "https://cdn.invalid/global-7.png")
        member.avatar = None
        self.assertEqual(ChipRankCache._profile_avatar_asset(member).url, "https://cdn.invalid/default-7.png")

    def test_shared_image_filters_departed_users_and_preserves_ties(self) -> None:
        async def scenario() -> None:
            guild = _FakeGuild()
            for user_id, name, is_bot in (
                (1, "alpha", False),
                (2, "beta", False),
                (3, "gamma", False),
                (4, "bot", True),
            ):
                guild.members[user_id] = _FakeMember(guild, user_id, name, bot=is_bot)
            rows = [
                {"user_id": 99, "chips": 999, "bonus_chips": 0, "weekly_delta": 10},
                {"user_id": 4, "chips": 500, "bonus_chips": 0, "weekly_delta": 10},
                {"user_id": 1, "chips": 200, "bonus_chips": 5, "weekly_delta": 37},
                {"user_id": 2, "chips": 150, "bonus_chips": 8, "weekly_delta": -4},
                {"user_id": 3, "chips": 150, "bonus_chips": 0, "weekly_delta": 0},
            ]
            db = _FakeDB(rows)
            cache = ChipRankCache(_FakeBot(guild), db)
            original_renderer = cache_module.render_rank_image
            render_calls = 0

            def counting_renderer(*args, **kwargs):
                nonlocal render_calls
                render_calls += 1
                return original_renderer(*args, **kwargs)

            cache_module.render_rank_image = counting_renderer
            try:
                response = await cache.get_rank(guild, guild.members[2])
                self.assertEqual([row.user_id for row in response.top_rows], [1, 2, 3])
                self.assertEqual([row.position for row in response.top_rows], [1, 2, 2])
                self.assertEqual([row.display_name for row in response.top_rows], ["@alpha", "@beta", "@gamma"])
                self.assertEqual(response.top_rows[0].avatar_key, "https://cdn.invalid/global-1.png")
                self.assertNotIn("guild-1", response.top_rows[0].avatar_key)
                self.assertIn("**#2**", response.requester_line)
                self.assertIn(
                    "**-4** <:emoji_65:1485043671077228786> perdidas nessa semana",
                    response.requester_line,
                )
                self.assertNotIn("🔴", response.requester_line)
                with Image.open(BytesIO(response.image_bytes)) as image:
                    self.assertEqual(image.format, "PNG")
                gainer_response = await cache.get_rank(guild, guild.members[1])
                self.assertIn(
                    "**+37** <:emoji_63:1485041721573249135> ganhas nessa semana",
                    gainer_response.requester_line,
                )
                self.assertNotIn("🟢", gainer_response.requester_line)
                cached_response = await cache.get_rank(guild, guild.members[3])
                self.assertEqual(cached_response.image_bytes, response.image_bytes)
                self.assertEqual(cached_response.requester_line, "-# Você: **#2** • **150 fichas**")
                self.assertEqual(render_calls, 1)

                # Uma mudança econômica invalida o snapshot e troca a ordem sem
                # precisar procurar milhões de posições no comando.
                await asyncio.sleep(0.05)
                rows[2]["chips"] = 120
                rows[3]["chips"] = 260
                cache.invalidate(guild.id, 2)
                updated = await cache.get_rank(guild, guild.members[2])
                self.assertEqual([row.user_id for row in updated.top_rows], [2, 3, 1])
                self.assertIn("**#1**", updated.requester_line)
                self.assertEqual(render_calls, 2)
            finally:
                cache_module.render_rank_image = original_renderer
                await cache.close()
            self.assertEqual(db.listeners, [])

        asyncio.run(scenario())

    def test_rank_pages_are_sliced_cached_and_clamped(self) -> None:
        async def scenario() -> None:
            guild = _FakeGuild()
            rows = []
            for user_id in range(1, 13):
                guild.members[user_id] = _FakeMember(guild, user_id, f"user_{user_id:02d}")
                rows.append(
                    {
                        "user_id": user_id,
                        "chips": 1_000 - user_id,
                        "bonus_chips": 0,
                        "weekly_delta": 0,
                    }
                )

            cache = ChipRankCache(_FakeBot(guild), _FakeDB(rows))
            original_renderer = cache_module.render_rank_image
            render_calls = 0

            def counting_renderer(*args, **kwargs):
                nonlocal render_calls
                render_calls += 1
                return original_renderer(*args, **kwargs)

            cache_module.render_rank_image = counting_renderer
            try:
                first = await cache.get_rank(guild, guild.members[1], page_index=0)
                self.assertEqual(first.page_index, 0)
                self.assertEqual(first.page_count, 2)
                self.assertEqual(first.top_number, 10)
                self.assertEqual([row.user_id for row in first.top_rows], list(range(1, 11)))

                second = await cache.get_rank(guild, guild.members[1], page_index=1)
                self.assertEqual(second.page_index, 1)
                self.assertEqual(second.page_count, 2)
                self.assertEqual(second.top_number, 12)
                self.assertEqual([row.user_id for row in second.top_rows], [11, 12])

                clamped = await cache.get_rank(guild, guild.members[1], page_index=99)
                self.assertEqual(clamped.page_index, 1)
                self.assertEqual(clamped.image_bytes, second.image_bytes)
                first_again = await cache.get_rank(guild, guild.members[1], page_index=0)
                self.assertEqual(first_again.image_bytes, first.image_bytes)
                self.assertEqual(render_calls, 2)
            finally:
                cache_module.render_rank_image = original_renderer
                await cache.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
