from __future__ import annotations


def chunk_lines(lines: list[str], max_chars: int = 3500) -> list[str]:
    chunks, current, size = [], [], 0
    for line in lines:
        extra = len(line) + 1
        if current and size + extra > max_chars:
            chunks.append("\n".join(current))
            current, size = [line], extra
        else:
            current.append(line)
            size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def status_bool(value: bool) -> str:
    return "Ativado" if bool(value) else "Desativado"


def status_badge(value: bool, *, on: str = "Ativo", off: str = "Inativo") -> str:
    return f"🟢 {on}" if bool(value) else f"⚫ {off}"


def status_source_badge(source: str) -> str:
    source = str(source or "Servidor")
    return f"👤 {source}" if source == "Usuário" else f"🏠 {source}"


def status_engine_label(engine: str) -> str:
    value = str(engine or "gtts").lower()
    if value in {"android_native", "atts", "android", "android_tts", "native"}:
        return "📱 ATTS"
    if value in {"teto", "kasane_teto", "teto_utau", "utau"}:
        return "🥖 Kasane Teto"
    if value == "edge":
        return "🗣️ Edge"
    return "🌐 gTTS"
