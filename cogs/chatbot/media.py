"""Utilitários de mídia: classificação de anexos, download, upload.

Este módulo centraliza o código que lida com os diferentes tipos de mídia
suportados pelo chatbot (imagem, áudio, voice msg). Todas as features de
mídia (visão, STT, TTS, imagegen) usam helpers daqui — evita duplicar
lógica de MIME/tamanho/etc em vários lugares.

Design: funções puras sempre que possível. Nada de estado. Se precisa de
sessão aiohttp pra baixar, é passada como argumento.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord

from . import constants as C

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaAttachment:
    """Um anexo que o bot consegue processar.

    Já filtrado por MIME + tamanho — se veio aqui, dá pra enviar ao provider.
    """
    url: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str  # "image" | "audio"


def classify_attachment(att: discord.Attachment) -> Optional[MediaAttachment]:
    """Classifica um anexo do Discord. Retorna None se não é processável.

    Regras:
    - MIME precisa estar nas listas suportadas
    - Tamanho dentro do limite do provider (imagens 20MB, áudio 25MB)
    - Se qualquer checagem falha, retorna None (caller ignora silenciosamente)

    Não levanta exceção — anexos não processáveis são esperados (ex: user
    manda um PDF, um .txt, etc) e devem ser ignorados sem barulho.
    """
    mime = (att.content_type or "").lower().strip()
    if not mime:
        # Algumas vezes content_type vem vazio. Tenta inferir pela extensão.
        ext = (att.filename or "").lower().rsplit(".", 1)[-1] if "." in (att.filename or "") else ""
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif",
            "ogg": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
            "wav": "audio/wav", "flac": "audio/flac", "opus": "audio/ogg",
        }.get(ext, "")

    size = int(att.size or 0)

    if mime in C.SUPPORTED_IMAGE_MIMES:
        if size > C.MAX_IMAGE_SIZE_BYTES:
            log.info("media: imagem %s muito grande (%s bytes)", att.filename, size)
            return None
        return MediaAttachment(
            url=att.url,
            filename=att.filename or "image",
            mime_type=mime,
            size_bytes=size,
            kind="image",
        )

    if mime in C.SUPPORTED_AUDIO_MIMES:
        if size > C.MAX_AUDIO_SIZE_BYTES:
            log.info("media: áudio %s muito grande (%s bytes)", att.filename, size)
            return None
        return MediaAttachment(
            url=att.url,
            filename=att.filename or "audio",
            mime_type=mime,
            size_bytes=size,
            kind="audio",
        )

    return None


def extract_attachments(message: discord.Message) -> tuple[list[MediaAttachment], list[MediaAttachment]]:
    """Extrai todos os anexos processáveis de uma mensagem.

    Retorna (imagens, áudios) separados. Já filtrados por
    MIME/tamanho/limite. Imagens são cortadas a MAX_IMAGES_PER_MESSAGE.
    """
    images: list[MediaAttachment] = []
    audios: list[MediaAttachment] = []
    for att in message.attachments:
        classified = classify_attachment(att)
        if classified is None:
            continue
        if classified.kind == "image":
            if len(images) < C.MAX_IMAGES_PER_MESSAGE:
                images.append(classified)
        elif classified.kind == "audio":
            audios.append(classified)
    return images, audios


def is_voice_message(message: discord.Message) -> bool:
    """True se a mensagem é uma voice note do Discord (não um áudio anexado).

    Discord marca voice notes com flag. Essas são diferentes de user-attached
    audio files — geralmente .ogg curtos, gravados no cliente. A distinção
    é útil pra UX (voice note = user "falando", trigger por default).
    """
    # `is_voice_message` é atributo do MessageFlags em discord.py 2.4+
    flags = getattr(message, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "voice", False))


async def download_attachment_bytes(
    session: aiohttp.ClientSession,
    media: MediaAttachment,
    *,
    max_bytes: Optional[int] = None,
) -> Optional[bytes]:
    """Baixa os bytes de um anexo pra processar localmente.

    Usado quando precisamos enviar o arquivo pro provider (ex: Whisper STT,
    que aceita multipart/form). Pra visão do Groq, passamos URL direto e
    NÃO precisamos baixar — poupa RAM.

    Retorna None se o download falhar ou exceder `max_bytes`.
    """
    limit = max_bytes or media.size_bytes
    timeout = aiohttp.ClientTimeout(
        total=C.MEDIA_READ_TIMEOUT_SECONDS,
        connect=C.MEDIA_CONNECT_TIMEOUT_SECONDS,
        sock_read=C.MEDIA_READ_TIMEOUT_SECONDS,
    )
    try:
        async with session.get(media.url, timeout=timeout) as resp:
            if resp.status >= 400:
                log.warning("media: download %s falhou HTTP %s", media.url, resp.status)
                return None
            # Lê com cap pra proteger memória
            buf = io.BytesIO()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    log.warning("media: download %s passou do limite %s", media.url, limit)
                    return None
            return buf.getvalue()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("media: erro de rede ao baixar %s: %s", media.url, e)
        return None
