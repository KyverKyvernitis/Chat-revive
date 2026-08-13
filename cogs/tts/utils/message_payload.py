"""Monta o payload de TTS a partir de uma mensagem do Discord.

Aplica o engine forçado pelo prefixo (gTTS/Edge/ATTS/Teto), preenche valores
default quando o user não configurou, renderiza o texto final (limpo +
prefixo de autor se ligado) e devolve o `QueueItem` pronto pro dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import config

from ..audio import QueueItem, _has_speakable_tts_text


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MessageTTSPayload:
    text: str
    resolved: dict[str, Any]
    queue_item: QueueItem
    forced_gtts: bool


async def build_message_tts_payload(
    cog,
    message,
    *,
    guild_defaults: dict | None,
    active_prefix: str,
    forced_engine: str,
) -> MessageTTSPayload | None:
    db = cog._get_db()
    if db is None:
        print("[tts_voice] ignorado | settings_db indisponível")
        return None

    # `resolve_tts` retorna a configuração efetiva do user (mistura
    # config pessoal + defaults da guild). Pode falhar se o Mongo cair.
    try:
        resolved = await cog._maybe_await(db.resolve_tts(message.guild.id, message.author.id))
    except Exception as e:
        print(f"[tts_voice] erro em resolve_tts | guild={message.guild.id} user={message.author.id} erro={e}")
        return None

    resolved = dict(resolved or {})
    forced_gtts = False
    fallback_engine = str(resolved.get("engine") or "gtts").strip().lower() or "gtts"
    fallback_voice = str(resolved.get("voice") or "")
    fallback_language = str(resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br"))
    fallback_rate = str(resolved.get("rate") or "+0%")
    fallback_pitch = str(resolved.get("pitch") or "+0Hz")

    # Override pelo prefixo: se o user usou o prefixo de Edge, ele quer Edge
    # nessa fala mesmo que o engine padrão dele seja gTTS (e idem pros outros).
    if forced_engine == "gtts":
        resolved["engine"] = "gtts"
        resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
    elif forced_engine == "edge":
        resolved["engine"] = "edge"
        resolved["voice"] = resolved.get("voice") or "pt-BR-FranciscaNeural"
        resolved["rate"] = resolved.get("rate") or "+0%"
        resolved["pitch"] = resolved.get("pitch") or "+0Hz"
    elif forced_engine == "android_native":
        # ATTS usa o Android TTS nativo do APK/phone-worker. Guardamos a engine
        # efetiva antiga para fallback local se o celular estiver offline/lento.
        resolved["engine"] = "android_native"
        resolved["language"] = resolved.get("android_language") or "pt-BR"
        resolved["voice"] = resolved.get("android_voice") or ""
        resolved["rate"] = resolved.get("android_rate") or "1.0"
        resolved["pitch"] = resolved.get("android_pitch") or "1.0"
    elif forced_engine == "teto":
        # Teto é exclusiva do phone worker. Os campos normais continuam
        # guardados no QueueItem como fallback caso o renderer esteja offline,
        # ocupado, aquecido ou sem assets válidos.
        resolved["engine"] = "teto"
        resolved["language"] = "pt-BR"
        resolved["voice"] = "kasane-teto-standard"
        resolved["rate"] = "1.0"
        resolved["pitch"] = "C4"

    # Texto final: tira o prefixo de fala, limpa marcadores e prepende o
    # nome falado do autor quando o servidor tem essa opção ligada.
    text = cog._render_tts_text(message, message.content[len(active_prefix):].strip())
    text = cog._apply_author_prefix_if_needed(
        message.guild.id,
        message.author,
        text,
        enabled=cog._guild_announce_author_enabled(guild_defaults),
    )
    if not _has_speakable_tts_text(text):
        logger.info(
            "[tts_voice] mensagem ignorada | texto sem caracteres faláveis após renderização | guild=%s user=%s",
            message.guild.id,
            message.author.id,
        )
        return None

    # Sem voice channel não tem onde tocar — descarta cedo.
    author_voice = getattr(message.author, "voice", None)
    voice_channel = getattr(author_voice, "channel", None)
    if voice_channel is None:
        return None

    queue_item = QueueItem(
        guild_id=message.guild.id,
        channel_id=voice_channel.id,
        author_id=message.author.id,
        text=text,
        engine=str(resolved.get("engine") or "gtts"),
        voice=str(resolved.get("voice") or ""),
        language=str(resolved.get("language") or ""),
        rate=str(resolved.get("rate") or "+0%"),
        pitch=str(resolved.get("pitch") or "+0Hz"),
        piper_fallback_engine=fallback_engine,
        piper_fallback_voice=fallback_voice,
        piper_fallback_language=fallback_language,
        piper_fallback_rate=fallback_rate,
        piper_fallback_pitch=fallback_pitch,
        piper_model=str(resolved.get("piper_model") or getattr(config, "TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default"),
    )
    return MessageTTSPayload(text=text, resolved=resolved, queue_item=queue_item, forced_gtts=forced_gtts)
