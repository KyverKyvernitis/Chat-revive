from __future__ import annotations

import re

import discord


EDGE_DEFAULT_VOICE = "pt-BR-FranciscaNeural"
EDGE_FALLBACK_VOICE = "pt-BR-AntonioNeural"
GTTS_LANG = "pt-br"

RATE_RE = re.compile(r"^[+-]\\d+%$")
PITCH_RE = re.compile(r"^[+-]\\d+Hz$")


def clean_text(text: str) -> str:
    text = re.sub(r"<a?:\\w+:\\d+>", "", text)
    text = re.sub(r"<@!?\\d+>", "usuário", text)
    text = re.sub(r"<@&\\d+>", "cargo", text)
    text = re.sub(r"<#\\d+>", "canal", text)
    text = re.sub(r"https?://\\S+", "link", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return text[:350]


def make_embed(title: str, description: str, *, ok: bool, on_color: int, off_color: int) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=on_color if ok else off_color,
    )


def validate_engine(engine: str) -> str:
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
    return EDGE_DEFAULT_VOICE
