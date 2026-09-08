"""Helpers compartilhados do TTS: limpeza de texto e validadores das configs.

Tudo aqui é puro/sem estado pra poder ser usado tanto no path de mensagem
(audio.py) quanto na UI dos painéis (ui.py) sem se preocupar com guilds.
"""
from __future__ import annotations

import re
import discord
from gtts.lang import tts_langs

EDGE_DEFAULT_VOICE = "pt-BR-FranciscaNeural"
EDGE_FALLBACK_VOICE = "pt-BR-AntonioNeural"
GTTS_DEFAULT_LANGUAGE = "pt"

# Formatos aceitos pelo Edge para rate/pitch (ex: "+25%", "-10Hz").
RATE_RE = re.compile(r"^[+-]\d+%$")
PITCH_RE = re.compile(r"^[+-]\d+Hz$")

def clean_text(text: str) -> str:
    # Tira marcadores do Discord que viram lixo quando lidos em voz alta:
    # emojis custom, mentions, cargos, canais e links viram palavra ou somem.
    # Limita em 350 chars pra não estourar timeout das engines.
    text = re.sub(r"<a?:\w+:\d+>", "", text)
    text = re.sub(r"<@!?\d+>", "usuário", text)
    text = re.sub(r"<@&\d+>", "cargo", text)
    text = re.sub(r"<#\d+>", "canal", text)
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:350]

def make_embed(title: str, description: str, *, ok: bool, on_color: int, off_color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=on_color if ok else off_color)

def validate_engine(engine: str) -> str:
    # Retorna gtts se vier algo fora da lista — evita crash por engine inválida.
    engine = engine.strip().lower()
    return engine if engine in ("gtts", "edge") else "gtts"

def validate_rate(value: str) -> str:
    value = value.strip()
    return value if RATE_RE.fullmatch(value) else "+0%"

def validate_pitch(value: str) -> str:
    value = value.strip()
    return value if PITCH_RE.fullmatch(value) else "+0Hz"

def validate_voice(voice: str, edge_voice_names: set[str]) -> str:
    voice = voice.strip()
    if not voice:
        return EDGE_DEFAULT_VOICE
    if voice in edge_voice_names:
        return voice
    # Offline catalogue means unknown, not invalid. Preserve plausible saved
    # provider IDs until a successful catalogue refresh can validate them.
    if not edge_voice_names and re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}(?:-[A-Za-z0-9]+)+Neural", voice):
        return voice
    return EDGE_DEFAULT_VOICE

def get_gtts_languages() -> dict[str, str]:
    # Ordenado por código pra ficar previsível na UI dos painéis.
    langs = tts_langs()
    return dict(sorted(langs.items(), key=lambda item: item[0].lower()))

def validate_language(language: str, gtts_languages: dict[str, str]) -> str:
    language = language.strip().lower()
    if not language:
        return GTTS_DEFAULT_LANGUAGE
    if language in gtts_languages:
        return language
    return GTTS_DEFAULT_LANGUAGE
