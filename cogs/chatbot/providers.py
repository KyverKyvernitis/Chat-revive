"""Router HTTP leve com fallback real de texto e visão.

Estados são mantidos por provider/modelo, então um modelo removido não derruba
os demais. Há deadline total, respostas vazias são rejeitadas e o fallback
Gemini recebe os bytes das imagens — nunca afirma que viu algo que não recebeu.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(
        self, message: str, *, status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    pass


class AllProvidersExhausted(ProviderError):
    pass


@dataclass
class ChatMessage:
    role: str
    content: str
    image_urls: list[str] = field(default_factory=list)

    def to_openai_payload(self) -> dict:
        if not self.image_urls:
            return {"role": self.role, "content": self.content}
        blocks: list[dict] = [{"type": "text", "text": self.content}]
        for url in self.image_urls[:C.MAX_IMAGES_PER_MESSAGE]:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": self.role, "content": blocks}


@dataclass
class _ProviderState:
    next_allowed_monotonic: float = 0.0
    consecutive_failures: int = 0
    last_status: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.next_allowed_monotonic

    def mark_success(self) -> None:
        self.consecutive_failures = 0
        self.next_allowed_monotonic = 0.0
        self.last_status = 0

    def mark_failure(self, cooldown_seconds: float, *, status: int = 0) -> None:
        self.consecutive_failures += 1
        factor = 2 ** min(self.consecutive_failures - 1, 4)
        self.next_allowed_monotonic = time.monotonic() + min(900.0, cooldown_seconds * factor)
        self.last_status = int(status)


def _retry_after(resp: aiohttp.ClientResponse) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    try:
        return max(1.0, min(900.0, float(raw))) if raw else None
    except (TypeError, ValueError):
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("deadline total dos providers esgotado")
    return min(C.PROVIDER_TIMEOUT_SECONDS, remaining)


async def _read_limited_bytes(
    response: aiohttp.ClientResponse, *, limit: int,
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    try:
        declared = int(raw_length) if raw_length else 0
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise ProviderError("resposta do provider excede o limite")
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            raise ProviderError("resposta do provider excede o limite")
    return bytes(body)


async def _read_json_limited(response: aiohttp.ClientResponse):
    body = await _read_limited_bytes(
        response, limit=C.MAX_PROVIDER_RESPONSE_BYTES,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider retornou JSON inválido") from exc


async def _read_error_excerpt(response: aiohttp.ClientResponse) -> str:
    body = bytearray()
    async for chunk in response.content.iter_chunked(1024):
        body.extend(chunk)
        if len(body) >= 4096:
            break
    return bytes(body[:4096]).decode("utf-8", errors="replace")[:300]


class _GroqClient:
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float,
    ) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [message.to_openai_payload() for message in messages],
            "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
            "max_completion_tokens": C.MAX_RESPONSE_TOKENS,
            "stream": False,
        }
        # Evita gastar tokens de raciocínio oculto em conversa casual.
        if model.startswith("openai/gpt-oss"):
            payload.update({"reasoning_effort": "low", "include_reasoning": False})
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=max(0.1, timeout_seconds))
        try:
            async with self._session.post(
                self.BASE_URL, json=payload, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    raise RateLimitError(
                        f"Groq rate-limit ({model})", status=429,
                        retry_after=_retry_after(resp),
                    )
                if resp.status >= 400:
                    body = await _read_error_excerpt(resp)
                    raise ProviderError(
                        f"Groq HTTP {resp.status}: {body[:300]}", status=resp.status,
                    )
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Groq timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(f"Groq erro de rede: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Groq resposta malformada: {exc}") from exc
        reply = content.strip() if isinstance(content, str) else ""
        if not reply:
            raise ProviderError("Groq retornou resposta vazia")
        return reply


class _GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    _ALLOWED_IMAGE_HOST_SUFFIXES = (
        ".discordapp.com", ".discordapp.net", ".discord.com",
    )

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def _download_inline_image(self, url: str, deadline: float) -> dict:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host.endswith(suffix) for suffix in self._ALLOWED_IMAGE_HOST_SUFFIXES
        ):
            raise ProviderError("host de imagem não permitido")
        timeout = aiohttp.ClientTimeout(
            total=_remaining(deadline),
            connect=min(C.MEDIA_CONNECT_TIMEOUT_SECONDS, _remaining(deadline)),
            sock_read=min(C.MEDIA_READ_TIMEOUT_SECONDS, _remaining(deadline)),
        )
        try:
            async with self._session.get(url, timeout=timeout) as resp:
                if resp.status >= 400:
                    raise ProviderError(
                        f"download da imagem HTTP {resp.status}", status=resp.status,
                    )
                mime = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                if mime not in C.SUPPORTED_IMAGE_MIMES:
                    raise ProviderError(f"MIME de imagem inválido: {mime or 'ausente'}")
                try:
                    declared = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0
                if declared > C.MAX_GEMINI_IMAGE_BYTES:
                    raise ProviderError("imagem excede limite do fallback")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > C.MAX_GEMINI_IMAGE_BYTES:
                        raise ProviderError("imagem excede limite do fallback")
        except asyncio.TimeoutError as exc:
            raise ProviderError("timeout ao baixar imagem") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(f"erro ao baixar imagem: {exc}") from exc
        if not data:
            raise ProviderError("imagem vazia")
        return {
            "inlineData": {
                "mimeType": mime,
                "data": base64.b64encode(bytes(data)).decode("ascii"),
            }
        }

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float,
    ) -> str:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        contents: list[dict] = []
        for message in messages:
            parts: list[dict] = [{"text": message.content}]
            for url in message.image_urls[:C.MAX_IMAGES_PER_MESSAGE]:
                parts.append(await self._download_inline_image(url, deadline))
            contents.append({
                "role": "user" if message.role == "user" else "model",
                "parts": parts,
            })
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
                "maxOutputTokens": C.MAX_RESPONSE_TOKENS,
            },
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=_remaining(deadline))
        try:
            async with self._session.post(
                self.BASE_URL.format(model=model), json=payload,
                headers=headers, timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    raise RateLimitError(
                        f"Gemini rate-limit ({model})", status=429,
                        retry_after=_retry_after(resp),
                    )
                if resp.status >= 400:
                    body = await _read_error_excerpt(resp)
                    raise ProviderError(
                        f"Gemini HTTP {resp.status}: {body[:300]}", status=resp.status,
                    )
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Gemini timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(f"Gemini erro de rede: {exc}") from exc
        try:
            parts = data["candidates"][0]["content"]["parts"]
            reply = "".join(
                part.get("text", "") for part in parts if isinstance(part, dict)
            ).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Gemini resposta malformada: {exc}") from exc
        if not reply:
            raise ProviderError("Gemini retornou resposta vazia")
        return reply


class ProviderRouter:
    def __init__(
        self, session: aiohttp.ClientSession, *, groq_key: Optional[str] = None,
        gemini_key: Optional[str] = None,
    ) -> None:
        self._groq = _GroqClient(session, groq_key) if groq_key else None
        self._gemini = _GeminiClient(session, gemini_key) if gemini_key else None
        self._states: dict[tuple[str, str], _ProviderState] = {}
        if not self._groq and not self._gemini:
            log.warning("ProviderRouter: nenhuma API key configurada")

    def _state(self, provider: str, model: str) -> _ProviderState:
        return self._states.setdefault((provider, model), _ProviderState())

    def _mark_provider_failure(
        self,
        provider: str,
        models: tuple[str, ...],
        cooldown_seconds: float,
        *,
        status: int,
    ) -> None:
        """Abre o circuito de todos os modelos quando a falha é da conta/rede."""
        for candidate in models:
            self._state(provider, candidate).mark_failure(
                cooldown_seconds, status=status,
            )

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = time.monotonic()
        return {
            f"{provider}/{model}": {
                "available": state.is_available(),
                "cooldown_seconds": max(0.0, state.next_allowed_monotonic - now),
                "failures": state.consecutive_failures,
                "last_status": state.last_status,
            }
            for (provider, model), state in self._states.items()
        }

    async def chat(
        self, *, system: str, messages: list[ChatMessage],
        temperature: float = C.DEFAULT_TEMPERATURE,
    ) -> str:
        has_images = any(message.image_urls for message in messages)
        attempts: list[tuple[str, object, tuple[str, ...]]] = []
        if self._groq:
            attempts.append((
                "groq", self._groq,
                C.GROQ_VISION_MODELS if has_images else C.GROQ_MODELS,
            ))
        if self._gemini:
            # Uma única tentativa multimodal evita baixar os mesmos anexos duas
            # vezes; para texto mantemos a cadeia completa de modelos.
            attempts.append((
                "gemini", self._gemini,
                C.GEMINI_MODELS[:1] if has_images else C.GEMINI_MODELS,
            ))
        if not attempts:
            raise AllProvidersExhausted("nenhum provider configurado")

        deadline = time.monotonic() + C.PROVIDER_ROUTER_TIMEOUT_SECONDS
        last_error: Optional[Exception] = None
        attempted = 0
        for provider_name, client, models in attempts:
            for model in models:
                state = self._state(provider_name, model)
                if not state.is_available():
                    continue
                try:
                    attempted += 1
                    reply = await client.chat(
                        system=system, messages=messages, temperature=temperature,
                        model=model, timeout_seconds=_remaining(deadline),
                    )
                    state.mark_success()
                    return reply
                except RateLimitError as exc:
                    last_error = exc
                    cooldown = float(exc.retry_after or 30.0)
                    self._mark_provider_failure(
                        provider_name, models, cooldown, status=429,
                    )
                    # Rate limit tende a ser da conta/provider, então pula seus
                    # outros modelos e segue ao provider seguinte.
                    log.warning("chatbot: %s/%s rate-limited", provider_name, model)
                    break
                except ProviderError as exc:
                    last_error = exc
                    status = int(exc.status or 0)
                    if status in (401, 403):
                        self._mark_provider_failure(
                            provider_name, models, 900.0, status=status,
                        )
                        log.error("chatbot: credencial inválida em %s", provider_name)
                        break
                    if status in (400, 404, 422):
                        state.mark_failure(300.0, status=status)
                    elif status >= 500 or status == 0:
                        self._mark_provider_failure(
                            provider_name, models, 20.0, status=status,
                        )
                    log.warning("chatbot: %s/%s falhou: %s", provider_name, model, exc)
                    if status == 0 or status >= 500:
                        # Falha de rede/serviço costuma afetar o provider todo;
                        # preserva o deadline para o fallback seguinte.
                        break
                    if time.monotonic() >= deadline:
                        break
                    continue
            if time.monotonic() >= deadline:
                break
        if attempted == 0:
            raise AllProvidersExhausted("todos os modelos estão em cooldown")
        raise AllProvidersExhausted(f"todos providers falharam: {last_error}")
