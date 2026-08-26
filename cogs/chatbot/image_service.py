"""Fronteira única para imagegen: slot, deadline e validação de bytes."""
from __future__ import annotations

import asyncio

import aiohttp

from . import constants as C
from .imagegen import (
    ImageGenerationResult,
    classify_image_prompt,
    generate_image,
    validate_generated_result,
)
from .runtime import AdmissionController


class ImageService:
    def __init__(
        self, session: aiohttp.ClientSession, admission: AdmissionController
    ) -> None:
        self._session = session
        self._admission = admission

    async def generate(
        self, *, prompt: str, channel_is_nsfw: bool,
        slot_acquired: bool = False,
    ) -> ImageGenerationResult:
        async def run() -> ImageGenerationResult:
            try:
                raw = await asyncio.wait_for(
                    generate_image(
                        self._session,
                        prompt=prompt,
                        channel_is_nsfw=channel_is_nsfw,
                        timeout_seconds=C.IMAGE_JOB_TIMEOUT_SECONDS,
                    ),
                    timeout=C.IMAGE_JOB_TIMEOUT_SECONDS + 1.0,
                )
            except asyncio.TimeoutError:
                return ImageGenerationResult(
                    ok=False, provider="router",
                    prompt_class=classify_image_prompt(prompt),
                    reason="timeout", detail="service_deadline",
                )
            return validate_generated_result(raw)

        if slot_acquired:
            return await run()
        async with self._admission.resource("image"):
            return await run()
