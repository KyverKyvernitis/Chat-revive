import discord


def make_embed(title: str, description: str, *, ok: bool = True) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())


def _clean_display_value(value: object, *, fallback: str = "Não definido") -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null"}:
        return fallback
    if text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    return text or fallback


def human_voice_name(value: object) -> str:
    text = _clean_display_value(value)
    if text == "Não definido":
        return text
    # pt-BR-FranciscaNeural -> Francisca
    parts = [part for part in str(text).replace("_", "-").split("-") if part]
    if len(parts) >= 3:
        name = parts[-1]
    else:
        name = str(text)
    for suffix in ("Neural", "Standard", "Wavenet"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or str(text)


def human_language_name(value: object) -> str:
    text = _clean_display_value(value)
    aliases = {
        "pt-br": "Português do Brasil",
        "pt-BR": "Português do Brasil",
        "pt": "Português",
        "en": "Inglês",
        "en-US": "Inglês",
        "es": "Espanhol",
        "fr": "Francês",
        "ja": "Japonês",
    }
    return aliases.get(text, text)


def human_rate(value: object) -> str:
    text = _clean_display_value(value, fallback="normal")
    lowered = text.lower()
    if lowered in {"+0%", "0%", "0", "1", "1.0", "normal"}:
        return "normal"
    try:
        numeric = float(str(text).replace("%", ""))
        if abs(numeric) < 0.001 or abs(numeric - 1.0) < 0.001:
            return "normal"
        if "%" in str(text):
            return "mais rápida" if numeric > 0 else "mais lenta"
        return "mais rápida" if numeric > 1.0 else "mais lenta"
    except Exception:
        return text


def human_pitch(value: object) -> str:
    text = _clean_display_value(value, fallback="normal")
    lowered = text.lower()
    if lowered in {"+0hz", "0hz", "0", "0.0", "normal"}:
        return "normal"
    try:
        numeric = float(str(text).lower().replace("hz", ""))
        if abs(numeric) < 0.001:
            return "normal"
        return "mais agudo" if numeric > 0 else "mais grave"
    except Exception:
        return text


def human_bool(value: object) -> str:
    text = str(value or "").strip().lower().replace("`", "")
    if text in {"ativado", "ativo", "true", "1", "sim", "on"}:
        return "ligado"
    if text in {"desativado", "inativo", "false", "0", "não", "nao", "off"}:
        return "desligado"
    return _clean_display_value(value, fallback="desligado")


def _field_value(embed: discord.Embed, *names: str, default: str = "Não definido") -> str:
    wanted = {name.lower() for name in names}
    for field in getattr(embed, "fields", []) or []:
        if str(getattr(field, "name", "") or "").lower() in wanted:
            return _clean_display_value(getattr(field, "value", ""), fallback=default)
    return default


def _has_field(embed: discord.Embed, *names: str) -> bool:
    wanted = {name.lower() for name in names}
    return any(str(getattr(field, "name", "") or "").lower() in wanted for field in (getattr(embed, "fields", []) or []))


def _prefix_example(prefix: object) -> str:
    value = _clean_display_value(prefix, fallback=".")
    return f"`{value}texto`"


def _human_reading(rate: object, pitch: object) -> str:
    rate_text = human_rate(rate)
    pitch_text = human_pitch(pitch)
    if pitch_text == "normal":
        return rate_text
    return f"{rate_text} · tom {pitch_text}"


def _atts_factor(value: object, default: str = "1.0") -> str:
    text = _clean_display_value(value, fallback=default).replace("x", "")
    try:
        number = float(str(text).replace(",", "."))
        return f"{number:.2f}".rstrip("0").rstrip(".") or "1"
    except Exception:
        return text or default


def build_settings_panel_text_from_embed(embed: discord.Embed, *, server: bool) -> str:
    """Renderiza o conteúdo principal do painel em texto para Components V2."""
    title = _clean_display_value(getattr(embed, "title", ""), fallback="TTS do servidor" if server else "TTS")
    description = _clean_display_value(getattr(embed, "description", ""), fallback="")

    atts_prefix = _field_value(embed, "Prefixo do ATTS", default="%")
    teto_prefix = _field_value(embed, "Prefixo da Kasane Teto", default="'")
    edge_prefix = _field_value(embed, "Prefixo do modo Edge", default=",")
    gtts_prefix = _field_value(embed, "Prefixo do modo gTTS", default=".")
    edge_voice = human_voice_name(_field_value(embed, "Voz do Edge"))
    edge_reading = _human_reading(
        _field_value(embed, "Velocidade do Edge", default="+0%"),
        _field_value(embed, "Tom do Edge", default="+0Hz"),
    )
    gtts_language = human_language_name(_field_value(embed, "Idioma do gTTS", default="pt-br"))

    lines: list[str] = [f"### {title}"]
    if description:
        lines.append(description)
    lines.extend([
        "",
        "Cada prefixo usa seus próprios ajustes.",
        "",
        "**ATTS**",
        f"Use: {_prefix_example(atts_prefix)}",
        "",
        "**Kasane Teto · experimental**",
        f"Use: {_prefix_example(teto_prefix)}",
        "Execução: phone worker, com fallback automático.",
        "",
        "**Edge**",
        f"Use: {_prefix_example(edge_prefix)}",
        f"Voz: {edge_voice}",
        f"Leitura: {edge_reading}",
        "",
        "**gTTS**",
        f"Use: {_prefix_example(gtts_prefix)}",
        f"Idioma: {gtts_language}",
    ])

    if server:
        bot_prefix = _field_value(embed, "Prefixo do bot", default="_")
        author = human_bool(_field_value(embed, "Autor antes da frase", default="Desativado"))
        ignored_role = _field_value(embed, "Cargo ignorado", default="nenhum")
        if ignored_role == "Não definido":
            ignored_role = "nenhum"
        lines.extend([
            "",
            "**Servidor**",
            f"Prefixo do bot: `{bot_prefix}`",
            f"Apelido falado: {author}",
            f"Cargo ignorado: {ignored_role}",
        ])
    elif _has_field(embed, "Apelido falado"):
        spoken_name = _field_value(embed, "Apelido falado", default="padrão")
        lines.extend([
            "",
            "**Apelido falado**",
            spoken_name,
        ])

    return "\n".join(lines).strip()


def build_expired_panel_embed(*, slash_mention: str, prefix_hint: str) -> discord.Embed:
    return make_embed("Esse painel expirou", f"Use o comando de barra {slash_mention} ou prefixo {prefix_hint} para abrir outro painel.", ok=False)


def build_toggle_embed(*, auto_leave_enabled: bool) -> discord.Embed:
    embed = discord.Embed(title="Painel de toggles do TTS", description="Use o botão abaixo para ligar ou desligar o Auto Leave do TTS.", color=discord.Color.blurple())
    embed.add_field(name="Auto leave", value="`Ativado`" if bool(auto_leave_enabled) else "`Desativado`", inline=True)
    return embed


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


def status_voice_channel_text(guild: discord.Guild | None, target_user_id: int) -> str:
    if guild is None:
        return "Não disponível"
    member = guild.get_member(int(target_user_id or 0))
    voice_state = getattr(member, "voice", None)
    channel = getattr(voice_state, "channel", None)
    if channel is None:
        return "Fora de call"
    return getattr(channel, "mention", None) or f"`{getattr(channel, 'name', 'Desconhecido')}`"


def spoken_name_status_text(*, active_name: str, active_source: str, custom_name: str = "") -> tuple[str, str]:
    if custom_name:
        return f"`{active_name}` (personalizado)", active_source
    if active_source == "apelido do servidor":
        return f"`{active_name}` (apelido do servidor)", active_source
    if active_source == "nome de usuário":
        return f"`{active_name}` (nome de usuário)", active_source
    return f"`{active_name}` (padrão)", active_source


def build_status_embed(*, member: discord.abc.User | None, target_name: str, user_id: int, viewer_user_id: int, public: bool, is_connected: bool, is_playing: bool, queue_size: int, resolved: dict, user_settings: dict, user_channel: str, bot_channel: str, spoken_name_text: str, google_language_default: str = "", google_voice_default: str = "", google_rate_default: str = "", google_pitch_default: str = "") -> discord.Embed:
    if public:
        title = f"📡 Status de TTS de {target_name}"
        description = f"Resumo público das configurações atuais de TTS de {target_name}."
    elif int(user_id or 0) != int(viewer_user_id or user_id or 0):
        title = f"📡 Status de TTS de {target_name}"
        description = f"Resumo das configurações atuais de TTS de {target_name}."
    else:
        title = "📡 Status do TTS"
        description = "Resumo das suas configurações atuais de TTS neste servidor."
    color = discord.Color.green() if is_playing else (discord.Color.blurple() if is_connected else discord.Color.orange())
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if member is not None:
        avatar = getattr(getattr(member, "display_avatar", None), "url", None)
        if avatar:
            embed.set_thumbnail(url=avatar)
    queue_label = f"{queue_size} item" + ("" if queue_size == 1 else "s")
    summary_bits = [status_badge(is_connected, on="Conectado", off="Desconectado"), status_badge(is_playing, on="Falando", off="Em espera"), f"📚 Fila: `{queue_label}`", f"🎙️ Engine: `{status_engine_label(str(resolved.get('engine', 'gtts'))).split(' ', 1)[-1]}`"]
    embed.add_field(name="Resumo rápido", value=" • ".join(summary_bits), inline=False)
    customized_keys = [label for key, label in (("engine", "engine"), ("android_voice", "voz do ATTS"), ("android_language", "idioma do ATTS"), ("android_rate", "velocidade do ATTS"), ("android_pitch", "tom do ATTS"), ("voice", "voz do Edge"), ("language", "idioma do gTTS"), ("rate", "velocidade do Edge"), ("pitch", "tom do Edge"), ("speaker_name", "apelido falado")) if str((user_settings or {}).get(key, "") or "").strip()]
    source_line = "**Origem:** usando padrões do servidor" if not customized_keys else "**Personalizado:** " + ", ".join(customized_keys)
    embed.add_field(name="🎛️ Configuração ativa", value=(f"**Engine:** {status_engine_label(str(resolved.get('engine', 'gtts')))}\n" f"**gTTS idioma:** `{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`\n" f"**Edge voz:** `{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`\n" f"**Edge velocidade:** `{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`\n" f"**Edge tom:** `{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`\n" f"**Apelido falado:** {spoken_name_text}\n" f"{source_line}"), inline=False)
    embed.add_field(name="🛰️ Estado atual", value=(f"**Você:** {user_channel}\n" f"**Bot:** {bot_channel}\n" f"**Conexão:** {status_badge(is_connected, on='Conectado', off='Desconectado')}\n" f"**Reprodução:** {status_badge(is_playing, on='Falando agora', off='Parado')}\n" f"**Fila:** `{queue_label}`"), inline=False)
    return embed


def build_settings_embed(*, title: str, description: str, resolved: dict, guild_defaults: dict, server: bool, panel_kind: str, spoken_name_text: str | None, google_language_default: str = "", google_voice_default: str = "", google_rate_default: str = "", google_pitch_default: str = "", google_prefix_default: str = "", ignored_tts_role_text: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`", inline=True)
    embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`", inline=True)
    embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
    embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
    embed.add_field(name="Prefixo do ATTS", value=f"`{guild_defaults.get('atts_prefix', '%')}`", inline=True)
    teto_prefix = str(guild_defaults.get("teto_prefix") or "'")
    embed.add_field(name="Prefixo da Kasane Teto", value=f"`{teto_prefix}`", inline=True)
    embed.add_field(name="Prefixo do modo gTTS", value=f"`{guild_defaults.get('gtts_prefix', guild_defaults.get('tts_prefix', '.'))}`", inline=True)
    embed.add_field(name="Prefixo do modo Edge", value=f"`{guild_defaults.get('edge_prefix', ',')}`", inline=True)
    if not server and spoken_name_text is not None:
        embed.add_field(name="Apelido falado", value=spoken_name_text, inline=True)
    if server:
        embed.add_field(name="Prefixo do bot", value=f"`{guild_defaults.get('bot_prefix', '_')}`", inline=True)
        embed.add_field(name="Autor antes da frase", value="`Ativado`" if bool(guild_defaults.get('announce_author', False)) else "`Desativado`", inline=True)
        embed.add_field(name="Cargo ignorado", value=ignored_tts_role_text or "`Nenhum`", inline=True)
    embed.set_footer(text="Detalhes técnicos preservados para o painel avançado." if server else "Ajustes salvos para este usuário neste servidor.")
    return embed

