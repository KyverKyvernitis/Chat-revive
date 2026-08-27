from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def _load_modules():
    package_name = "games_profile_cache_tests_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "cogs" / "games")]
    sys.modules[package_name] = package
    services_name = f"{package_name}.services"
    services = types.ModuleType(services_name)
    services.__path__ = [str(ROOT / "cogs" / "games" / "services")]
    sys.modules[services_name] = services

    discord_stub = types.ModuleType("discord")
    discord_stub.Client = object
    discord_stub.Member = object
    discord_stub.User = object
    discord_stub.HTTPException = type("HTTPException", (Exception,), {})
    previous_discord = sys.modules.get("discord")
    sys.modules["discord"] = discord_stub
    try:
        for module_name, module_path in (
            (f"{package_name}.rank_renderer", ROOT / "cogs" / "games" / "rank_renderer.py"),
            (
                f"{package_name}.chip_profile_renderer",
                ROOT / "cogs" / "games" / "chip_profile_renderer.py",
            ),
            (
                f"{services_name}.chip_profile_cache",
                ROOT / "cogs" / "games" / "services" / "chip_profile_cache.py",
            ),
        ):
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
    finally:
        if previous_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = previous_discord
    return (
        sys.modules[f"{package_name}.chip_profile_renderer"],
        sys.modules[f"{services_name}.chip_profile_cache"],
    )


RENDERER, CACHE_MODULE = _load_modules()
ChipProfileData = RENDERER.ChipProfileData
ChipProfileCache = CACHE_MODULE.ChipProfileCache


def _png(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class _Asset:
    def __init__(self, url: str, payload: bytes):
        self.url = url
        self.payload = payload
        self.read_count = 0

    def replace(self, **_kwargs):
        return self

    async def read(self):
        self.read_count += 1
        return self.payload


class _Guild:
    id = 99


class _Member:
    def __init__(self, avatar: _Asset):
        self.id = 7
        self.guild = _Guild()
        self.name = "username"
        self.global_name = "Nome global"
        self.avatar = avatar
        self.default_avatar = None


class _FetchedUser:
    def __init__(self, banner: _Asset):
        self.banner = banner


class _Bot:
    def __init__(self, banner: _Asset):
        self.banner = banner
        self.fetch_count = 0

    async def fetch_user(self, _user_id: int):
        self.fetch_count += 1
        return _FetchedUser(self.banner)


class _RankCache:
    def get_cached_token_icons(self):
        return {}


class GamesChipProfileCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_identity_and_png_until_visible_data_changes(self) -> None:
        avatar = _Asset("https://cdn/avatar.png", _png((40, 90, 230), (256, 256)))
        banner = _Asset("https://cdn/banner.png", _png((210, 70, 90), (640, 240)))
        bot = _Bot(banner)
        member = _Member(avatar)
        cache = ChipProfileCache(bot, _RankCache())
        first_data = ChipProfileData("Nome global", 190, 0, 0, 1)

        first = await cache.get_profile(member, first_data)
        second = await cache.get_profile(member, first_data)
        changed = await cache.get_profile(
            member,
            ChipProfileData("Nome global", 220, 0, 0, 1),
        )

        self.assertEqual(first.image_bytes, second.image_bytes)
        self.assertNotEqual(first.image_bytes, changed.image_bytes)
        self.assertEqual(bot.fetch_count, 1)
        self.assertEqual(avatar.read_count, 1)
        self.assertEqual(banner.read_count, 1)
        await cache.close()


if __name__ == "__main__":
    unittest.main()

