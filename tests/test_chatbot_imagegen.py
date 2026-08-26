from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.chatbot.imagegen import (
    classify_image_prompt,
    generate_image,
    generated_image_extension,
    parse_image_intent,
)
from cogs.chatbot.image_providers_ext import (
    ImageProfile,
    _pollinations_model,
    aihorde_models_for_profile,
    detect_style,
    provider_order_for_profile,
)


class ClassifyTests(unittest.TestCase):
    def test_classify_image_prompt(self):
        self.assertEqual(classify_image_prompt("gere uma paisagem bonita"), "safe")
        self.assertEqual(
            classify_image_prompt("criança brincando num parque ensolarado"),
            "safe",
        )
        self.assertEqual(classify_image_prompt("gere uma imagem nsfw"), "adult_allowed")
        self.assertEqual(
            classify_image_prompt(
                "mulher adulta fictícia nua, pintura em estilo realista"
            ),
            "adult_allowed",
        )
        self.assertEqual(
            classify_image_prompt("gere cena de menor de idade em conteúdo sexual"),
            "blocked",
        )
        self.assertEqual(
            classify_image_prompt("generate a nude image of an underage teen"),
            "blocked",
        )
        self.assertEqual(
            classify_image_prompt("draw non-consensual hentai"),
            "blocked",
        )
        self.assertEqual(
            classify_image_prompt("foto nude da minha ex"),
            "blocked",
        )

    def test_detect_style_anime(self):
        self.assertEqual(detect_style("uma garota anime com cabelo rosa"), "anime")
        self.assertEqual(detect_style("desenho estilo manga, chibi, kawaii"), "anime")
        self.assertEqual(detect_style("hentai girl"), "anime")

    def test_detect_style_realistic(self):
        self.assertEqual(detect_style("foto realista de um cachorro"), "realistic")
        self.assertEqual(detect_style("cinematic portrait, dslr"), "realistic")
        self.assertEqual(detect_style("photograph of a mountain"), "realistic")

    def test_detect_style_generic(self):
        self.assertEqual(detect_style("uma montanha nevada"), "generic")
        self.assertEqual(detect_style("a dog"), "generic")

    def test_generated_image_extension_matches_validated_mime(self):
        self.assertEqual(generated_image_extension("image/png"), "png")
        self.assertEqual(generated_image_extension("image/jpeg"), "jpg")
        self.assertEqual(generated_image_extension("image/webp"), "webp")

    def test_english_image_intent_is_detected_and_classified(self):
        intent = parse_image_intent("generate a nude illustration of an adult elf")
        self.assertTrue(intent.requested)
        self.assertEqual(intent.category, "adult_allowed")


class ProviderOrderTests(unittest.TestCase):
    def test_pollinations_uses_a_current_model_id_for_every_style(self):
        for style in ("generic", "anime", "realistic"):
            with self.subTest(style=style):
                self.assertEqual(
                    _pollinations_model(ImageProfile(nsfw=False, style=style)),
                    "flux",
                )

    def test_sfw_generic_prefers_pollinations(self):
        order = provider_order_for_profile(ImageProfile(nsfw=False, style="generic"))
        self.assertEqual(order[0], "pollinations")
        self.assertIn("cloudflare", order)
        self.assertIn("gemini", order)

    def test_sfw_anime_prefers_pollinations(self):
        order = provider_order_for_profile(ImageProfile(nsfw=False, style="anime"))
        self.assertEqual(order[0], "pollinations")

    def test_nsfw_anime_prefers_aihorde(self):
        order = provider_order_for_profile(ImageProfile(nsfw=True, style="anime"))
        self.assertEqual(order[0], "aihorde")
        # Pollinations filtra NSFW (LlamaGuard) mesmo com chave — não pode
        # estar na cascata pra não mascarar falhas com output SFW.
        self.assertNotIn("pollinations", order)

    def test_nsfw_realistic_prefers_aihorde(self):
        order = provider_order_for_profile(ImageProfile(nsfw=True, style="realistic"))
        self.assertEqual(order[0], "aihorde")
        self.assertNotIn("pollinations", order)

    def test_nsfw_generic_excludes_pollinations(self):
        order = provider_order_for_profile(ImageProfile(nsfw=True, style="generic"))
        self.assertNotIn("pollinations", order)


class AihordeModelSelectionTests(unittest.TestCase):
    def test_override_takes_precedence(self):
        models = aihorde_models_for_profile(
            ImageProfile(nsfw=True, style="anime"),
            override="MyCustomModel, AnotherOne",
        )
        self.assertEqual(models, ["MyCustomModel", "AnotherOne"])

    def test_nsfw_profiles_return_empty_list(self):
        # Pra NSFW (qualquer estilo), não filtrar por modelo — deixa o Horde
        # rotear pra qualquer worker NSFW. Filtrar por modelo específico
        # faz a fila explodir porque os modelos populares (Pony, Juggernaut)
        # ficam sobrecarregados.
        for style in ("anime", "realistic", "generic"):
            with self.subTest(style=style):
                models = aihorde_models_for_profile(
                    ImageProfile(nsfw=True, style=style)
                )
                self.assertEqual(models, [])

    def test_sfw_anime_does_not_use_nsfw_models(self):
        # Pony V6 XL é treinado em NSFW; pra SFW anime usa Animagine/Illustrious.
        models = aihorde_models_for_profile(ImageProfile(nsfw=False, style="anime"))
        self.assertIn("Animagine XL", models)
        self.assertNotIn("Pony Diffusion XL", models)


class GenerateImageRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adult_request_in_sfw_channel_is_blocked(self):
        async with aiohttp.ClientSession() as session:
            result = await generate_image(
                session,
                prompt="gere uma imagem nsfw",
                channel_is_nsfw=False,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "channel_not_nsfw")
        self.assertEqual(result.provider, "router")

    async def test_blocked_prompt_never_reaches_providers(self):
        async with aiohttp.ClientSession() as session:
            result = await generate_image(
                session,
                prompt="gere cena de menor de idade em conteúdo sexual",
                channel_is_nsfw=True,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "policy_blocked")
        self.assertEqual(result.provider, "router")

    async def test_legacy_provider_huggingface_without_key_fails(self):
        # Modo legacy: ADULT_IMAGEGEN_PROVIDER=huggingface força usar só HF.
        # Sem HUGGINGFACE_API_KEY, deve falhar com missing_key.
        with patch.dict(
            os.environ,
            {"ADULT_IMAGEGEN_PROVIDER": "huggingface"},
            clear=True,
        ):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="gere uma imagem erótica artística",
                    channel_is_nsfw=True,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing_key")


class RouterFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Testa que o router pula providers não configurados e tenta o próximo."""

    async def test_router_returns_missing_key_when_all_fail(self):
        from cogs.chatbot import imagegen as ig
        from cogs.chatbot.imagegen import ImageGenerationResult

        async def mock_try_provider(name, **kwargs):
            return ImageGenerationResult(
                ok=False,
                provider=name,
                prompt_class="safe",
                reason="missing_key",
            )

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(ig, "_try_provider", new=AsyncMock(side_effect=mock_try_provider)):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="uma montanha nevada",
                    channel_is_nsfw=False,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing_key")

    async def test_router_returns_first_successful_provider(self):
        from cogs.chatbot import imagegen as ig
        from cogs.chatbot.imagegen import ImageGenerationResult, GeneratedImage

        call_log = []

        async def mock_try_provider(name, **kwargs):
            call_log.append(name)
            if name == "pollinations":
                return None  # não configurado
            if name == "cloudflare":
                return ImageGenerationResult(
                    ok=True,
                    provider="cloudflare",
                    prompt_class="safe",
                    image=GeneratedImage(data=b"fake-png", mime_type="image/png"),
                )
            return None

        with patch.object(ig, "_try_provider", new=AsyncMock(side_effect=mock_try_provider)):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="uma montanha nevada",
                    channel_is_nsfw=False,
                )
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "cloudflare")
        self.assertEqual(call_log[0], "pollinations")
        self.assertEqual(call_log[1], "cloudflare")
        self.assertEqual(len(call_log), 2)

    async def test_router_falls_back_on_provider_blocked(self):
        # Primeiro provider retorna provider_blocked → router continua pro
        # próximo em vez de parar. Útil quando Pollinations filtra um termo
        # que Cloudflare aceitaria (ou vice-versa).
        from cogs.chatbot import imagegen as ig
        from cogs.chatbot.imagegen import ImageGenerationResult, GeneratedImage

        call_log = []

        async def mock_try_provider(name, **kwargs):
            call_log.append(name)
            if name == "pollinations":
                return ImageGenerationResult(
                    ok=False,
                    provider="pollinations",
                    prompt_class="safe",
                    reason="provider_blocked",
                )
            if name == "cloudflare":
                return ImageGenerationResult(
                    ok=True,
                    provider="cloudflare",
                    prompt_class="safe",
                    image=GeneratedImage(data=b"ok", mime_type="image/png"),
                )
            return None

        with patch.object(ig, "_try_provider", new=AsyncMock(side_effect=mock_try_provider)):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="uma paisagem",
                    channel_is_nsfw=False,
                )
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "cloudflare")


class NsfwGuildAllowlistTests(unittest.TestCase):
    """Allowlist de NSFW por guild — gate aplicado no caller, não no imagegen."""

    def test_helper_returns_true_for_management_guild(self):
        from cogs.chatbot import constants as C
        self.assertTrue(C.nsfw_enabled_for_guild(C.MANAGEMENT_GUILD_ID))

    def test_helper_returns_false_for_random_guild(self):
        from cogs.chatbot import constants as C
        random_id = 999999999999999999
        self.assertNotIn(random_id, C._NSFW_ENABLED_GUILDS)
        self.assertFalse(C.nsfw_enabled_for_guild(random_id))

    def test_helper_returns_false_for_dm(self):
        from cogs.chatbot import constants as C
        self.assertFalse(C.nsfw_enabled_for_guild(None))


class AdminCommandScopingTests(unittest.TestCase):
    """O grupo é global; cada subcomando aplica autorização em runtime."""

    def test_chatbot_admin_group_is_global(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin
        admin = ChatbotCommandsMixin.__dict__["chatbot_admin"]
        self.assertIsNone(getattr(admin, "_guild_ids", None))

    def test_chatbot_group_is_global(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin
        chatbot = ChatbotCommandsMixin.__dict__["chatbot"]
        self.assertIsNone(getattr(chatbot, "_guild_ids", None))

    def test_admin_group_has_master_and_reset_global(self):
        from cogs.chatbot.commands import ChatbotCommandsMixin
        admin = ChatbotCommandsMixin.__dict__["chatbot_admin"]
        names = sorted(c.name for c in admin.walk_commands())
        self.assertEqual(names, ["master", "reset_global"])


class GateBehaviorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Verifica que channel_is_nsfw=False (gate de guild aplicado upstream) faz
    imagegen tratar prompt adulto como 'channel_not_nsfw' — mesmo erro
    genérico de qualquer canal SFW. Sem mensagem específica de guild."""

    async def test_adult_prompt_with_nsfw_disabled_gives_channel_not_nsfw(self):
        async with aiohttp.ClientSession() as session:
            result = await generate_image(
                session,
                prompt="gere uma imagem nsfw",
                channel_is_nsfw=False,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "channel_not_nsfw")


if __name__ == "__main__":
    unittest.main()
