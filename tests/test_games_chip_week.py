from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


CREATED_MOTOR_STUB = "motor.motor_asyncio" not in sys.modules
if CREATED_MOTOR_STUB:
    motor_package = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")
    motor_asyncio.AsyncIOMotorClient = object
    motor_package.motor_asyncio = motor_asyncio
    sys.modules["motor"] = motor_package
    sys.modules["motor.motor_asyncio"] = motor_asyncio

MODULE_PATH = Path(__file__).resolve().parents[1] / "db.py"
SPEC = importlib.util.spec_from_file_location("games_db_for_week_tests", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
DB_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DB_MODULE
SPEC.loader.exec_module(DB_MODULE)
if CREATED_MOTOR_STUB:
    sys.modules.pop("motor.motor_asyncio", None)
    sys.modules.pop("motor", None)


class _FakeCollection:
    async def update_one(self, *args, **kwargs):
        return types.SimpleNamespace(modified_count=1)

    async def update_many(self, *args, **kwargs):
        return types.SimpleNamespace(modified_count=1)


def _settings():
    settings = DB_MODULE.SettingsDB.__new__(DB_MODULE.SettingsDB)
    settings.user_cache = {}
    settings.guild_cache = {}
    settings._resolved_tts_cache = {}
    settings._chip_change_listeners = []
    settings.coll = _FakeCollection()
    settings._current_week_key = lambda: "2026-W35"
    return settings


class GamesChipWeekTests(unittest.TestCase):
    def test_bonus_spend_does_not_inflate_normal_week_loss(self) -> None:
        async def scenario() -> None:
            db = _settings()
            db.user_cache[(1, 11)] = {
                "chips": 100,
                "bonus_chips": 10,
                "has_chip_activity": True,
            }

            before_normal = db.get_user_chips(1, 11)
            before_bonus = db.get_user_bonus_chips(1, 11)
            after_normal = await db.add_user_chips(1, 11, -15)
            after_bonus = db.get_user_bonus_chips(1, 11)

            normal_delta = after_normal - before_normal
            bonus_delta = after_bonus - before_bonus
            await db.append_chip_history(1, 11, delta=normal_delta, kind="chips", reason="Aposta")
            await db.append_chip_history(1, 11, delta=bonus_delta, kind="bonus", reason="Aposta")

            self.assertEqual((after_normal, after_bonus), (95, 0))
            self.assertEqual(db.get_user_chip_week_delta(1, 11), -5)

        asyncio.run(scenario())

    def test_week_delta_counts_only_normal_history_and_rolls_over(self) -> None:
        async def scenario() -> None:
            db = _settings()
            db.user_cache[(1, 10)] = {
                "type": "user",
                "guild_id": 1,
                "user_id": 10,
                "chips": 100,
                "bonus_chips": 20,
                "has_chip_activity": True,
            }

            await db.append_chip_history(1, 10, delta=37, kind="chips", reason="Vitória")
            await db.append_chip_history(1, 10, delta=50, kind="bonus", reason="Prêmio bônus")
            await db.append_chip_history(1, 10, delta=-12, kind="chips", reason="Aposta")
            self.assertEqual(db.get_user_chip_week_delta(1, 10), 25)

            # Ajuste administrativo de saldo não é uma movimentação semanal.
            await db.set_user_chips(1, 10, 999)
            self.assertEqual(db.get_user_chip_week_delta(1, 10), 25)

            db.user_cache[(1, 10)]["chip_week_key"] = "2026-W34"
            db.user_cache[(1, 10)]["chip_week_delta"] = 400
            self.assertEqual(db.get_user_chip_week_delta(1, 10), 0)

            await db.append_chip_history(1, 10, delta=3, kind="chips", reason="Nova semana")
            self.assertEqual(db.get_user_chip_week_delta(1, 10), 3)
            self.assertEqual(db.user_cache[(1, 10)]["chip_week_key"], "2026-W35")

        asyncio.run(scenario())

    def test_week_reset_clears_delta_and_notifies_rank_cache(self) -> None:
        async def scenario() -> None:
            db = _settings()
            db.user_cache[(8, 20)] = {
                "type": "user",
                "guild_id": 8,
                "user_id": 20,
                "chips": 180,
                "bonus_chips": 7,
                "chip_week_key": "2026-W35",
                "chip_week_delta": -14,
                "has_chip_activity": True,
            }
            notifications = []
            db.add_chip_change_listener(lambda guild_id, user_id: notifications.append((guild_id, user_id)))

            await db.reset_user_chip_week_delta(8, 20)

            self.assertEqual(db.get_user_chip_week_delta(8, 20), 0)
            self.assertEqual(db.user_cache[(8, 20)]["chip_week_key"], "")
            self.assertEqual(notifications, [(8, 20)])

        asyncio.run(scenario())

    def test_rank_snapshot_includes_bonus_and_current_week_only(self) -> None:
        db = _settings()
        db.user_cache.update(
            {
                (3, 1): {
                    "chips": 200,
                    "bonus_chips": 9,
                    "chip_week_key": "2026-W35",
                    "chip_week_delta": 17,
                    "has_chip_activity": True,
                },
                (3, 2): {
                    "chips": -5,
                    "bonus_chips": 0,
                    "chip_week_key": "2026-W34",
                    "chip_week_delta": 99,
                    "has_chip_activity": True,
                },
                (3, 3): {"chips": 999, "has_chip_activity": False},
                (4, 4): {"chips": 999, "has_chip_activity": True},
            }
        )

        rows = sorted(db.get_chip_rank_snapshot(3), key=lambda row: row["user_id"])

        self.assertEqual(
            rows,
            [
                {"user_id": 1, "chips": 200, "bonus_chips": 9, "weekly_delta": 17},
                {"user_id": 2, "chips": -5, "bonus_chips": 0, "weekly_delta": 0},
            ],
        )


if __name__ == "__main__":
    unittest.main()
