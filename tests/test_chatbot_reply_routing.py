from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.chatbot import constants as C
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.message_index import MessageProfileRef
from cogs.chatbot.profiles import ChatbotProfile


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.names: list[str] = []

    def create(self, coro, *, name: str):
        self.names.append(name)
        # O listener cria a coroutine antes de entregá-la ao supervisor. Como
        # este fake não executa tasks, fechamos explicitamente para não vazar.
        coro.close()
        return None


def _cog() -> ChatbotCog:
    cog = object.__new__(ChatbotCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog._router = object()
    cog._profiles = object()
    cog._extrovert = None
    cog._supervisor = _RecordingSupervisor()
    cog._append_cached_channel_message = lambda _message: None
    return cog


def _target(*, author_id: int, webhook_id: int | None = None):
    target = Mock(spec=discord.Message)
    target.author = SimpleNamespace(id=author_id)
    target.webhook_id = webhook_id
    return target


def _message(*, message_type, resolved, content: str = "Sla"):
    channel = Mock(spec=discord.TextChannel)
    channel.id = 20
    return SimpleNamespace(
        id=30,
        author=SimpleNamespace(id=40, bot=False),
        webhook_id=None,
        type=message_type,
        guild=SimpleNamespace(id=10),
        channel=channel,
        content=content,
        reference=SimpleNamespace(message_id=50, resolved=resolved),
    )


class ReplyListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_reply_to_webhook_is_scheduled_once(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123, webhook_id=456),
        )

        await cog.on_message(message)

        self.assertEqual(
            cog._supervisor.names,
            ["chatbot-turn:10:30"],
        )

    async def test_native_reply_to_ordinary_user_is_ignored(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123),
        )

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, [])

    async def test_native_reply_to_bot_fallback_is_scheduled(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=999),
        )

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])

    async def test_unresolved_reply_reaches_persisted_index_fallback(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=None,
        )

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])

    async def test_system_message_is_still_ignored(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.pins_add,
            resolved=_target(author_id=123, webhook_id=456),
        )

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, [])

    async def test_incoming_webhook_reply_is_still_ignored(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123, webhook_id=456),
        )
        message.webhook_id = 777

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, [])

    async def test_incoming_bot_reply_is_still_ignored(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123, webhook_id=456),
        )
        message.author.bot = True

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, [])

    async def test_default_bot_mention_keeps_working(self):
        cog = _cog()
        message = _message(
            message_type=discord.MessageType.default,
            resolved=None,
            content="<@999> oi",
        )
        message.reference = None

        await cog.on_message(message)

        self.assertEqual(cog._supervisor.names, ["chatbot-turn:10:30"])


class ReplyProfileResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_index_resolves_profile_without_using_webhook_name(self):
        cog = _cog()
        profile = ChatbotProfile(
            guild_id=10,
            profile_id="persona-osaka",
            name="Nome salvo diferente do webhook",
            profile_kind=C.PROFILE_KIND_USER_STYLE,
        )
        cog._profiles = SimpleNamespace(
            is_enabled=AsyncMock(return_value=True),
            list_profiles=AsyncMock(return_value=[profile]),
        )
        cog._message_index = SimpleNamespace(
            resolve=AsyncMock(return_value=MessageProfileRef(
                guild_id=10,
                channel_id=20,
                message_id=50,
                profile_id="persona-osaka",
            )),
        )
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123, webhook_id=456),
        )

        trigger = await cog._resolve_trigger(message)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.profile.profile_id, "persona-osaka")
        self.assertEqual(trigger.via, "reply")
        self.assertEqual(trigger.content, "Sla")

    async def test_message_index_rejects_mapping_from_another_channel(self):
        cog = _cog()
        profile = ChatbotProfile(
            guild_id=10,
            profile_id="persona-osaka",
            name="Osaka",
            profile_kind=C.PROFILE_KIND_USER_STYLE,
        )
        cog._message_index = SimpleNamespace(
            resolve=AsyncMock(return_value=MessageProfileRef(
                guild_id=10,
                channel_id=999,
                message_id=50,
                profile_id="persona-osaka",
            )),
        )
        message = _message(
            message_type=discord.MessageType.reply,
            resolved=_target(author_id=123),
        )

        resolved = await cog._resolve_reply_profile_by_index(message, [profile])

        self.assertIsNone(resolved)


class PersonaDisplayIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_uses_current_member_nick_without_prefix(self):
        cog = _cog()
        avatar = SimpleNamespace(url="https://cdn.example/avatar.png")
        member = SimpleNamespace(
            display_name="Osaka",
            name="osaka",
            display_avatar=avatar,
            avatar=avatar,
        )
        guild = SimpleNamespace(get_member=lambda _user_id: member)
        profile = ChatbotProfile(
            guild_id=10,
            profile_id="persona-osaka",
            name="Nome antigo",
            profile_kind=C.PROFILE_KIND_USER_STYLE,
            source_user_id=123,
            dynamic_identity=True,
        )

        name, avatar_url = await cog._resolve_profile_identity(guild, profile)

        self.assertEqual(name, "Osaka")
        self.assertEqual(avatar_url, "https://cdn.example/avatar.png")

    async def test_persona_name_keeps_safety_and_length_limits(self):
        cog = _cog()
        display_name = "@everyone" + ("x" * C.MAX_NAME_LENGTH)
        member = SimpleNamespace(
            display_name=display_name,
            name="fallback",
            display_avatar=None,
            avatar=None,
        )
        guild = SimpleNamespace(get_member=lambda _user_id: member)
        profile = ChatbotProfile(
            guild_id=10,
            profile_id="persona-safe",
            name="Nome antigo",
            profile_kind=C.PROFILE_KIND_USER_STYLE,
            source_user_id=123,
            dynamic_identity=True,
        )

        name, _avatar_url = await cog._resolve_profile_identity(guild, profile)

        self.assertTrue(name.startswith("@\u200beveryone"))
        self.assertEqual(len(name), C.MAX_NAME_LENGTH)
        self.assertFalse(name.startswith("Persona ·"))

    def test_legacy_prefixed_names_remain_reply_compatible(self):
        cog = _cog()
        member = SimpleNamespace(display_name="Osaka", name="osaka")
        guild = SimpleNamespace(get_member=lambda _user_id: member)
        profile = ChatbotProfile(
            guild_id=10,
            profile_id="persona-osaka",
            name="Nome antigo",
            fallback_name="Nome salvo",
            profile_kind=C.PROFILE_KIND_USER_STYLE,
            source_user_id=123,
            dynamic_identity=True,
        )

        candidates = cog._profile_name_candidates(guild, profile)

        self.assertIn("Osaka", candidates)
        self.assertIn("Persona · Osaka", candidates)
        self.assertIn("Nome antigo", candidates)
        self.assertIn("Persona · Nome antigo", candidates)


if __name__ == "__main__":
    unittest.main()
