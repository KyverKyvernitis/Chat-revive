def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
    description = (
        f"{title}\n\n"
        + "\n".join(lines)
        + f"\n\n{footer}"
    )
    return self._make_embed(title, description, ok=True)


@app_commands.command(
    name="voices_edge",
    description="Mostra vozes disponíveis do Edge TTS para usar em /set_voice"
)
async def voices_edge(self, interaction: discord.Interaction):
    if not self.edge_voice_cache:
        await self._load_edge_voices()

    voices = [v for v in self.edge_voice_cache if v.startswith("pt-")] or self.edge_voice_cache[:40]
    lines = [f"- `{v}`" for v in voices[:40]]

    embed = self._format_list_block(
        "Vozes Edge",
        lines,
        "Use `/set_voice` para definir sua voz do Edge."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(
    name="voices_gtts",
    description="Mostra idiomas disponíveis do gTTS para usar em /set_language"
)
async def voices_gtts(self, interaction: discord.Interaction):
    if not self.gtts_languages:
        self.gtts_languages = get_gtts_languages()

    items = list(self.gtts_languages.items())[:80]
    lines = [f"- `{code}` — {name}" for code, name in items]

    embed = self._format_list_block(
        "Idiomas gTTS",
        lines,
        "Use `/set_language` para definir seu idioma do gTTS."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(
    name="set_tts_engine",
    description="Escolhe qual engine de TTS você quer usar: gTTS ou Edge"
)
@app_commands.describe(engine="Escolha `gtts` ou `edge`")
async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    engine = validate_engine(engine)
    await db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine)

    extra = (
        "O `gtts` usa idioma (`/set_language`).\n"
        "O `edge` permite voz, velocidade e tom."
    )

    await interaction.response.send_message(
        embed=self._make_embed(
            "Engine atualizada",
            f"Sua engine de TTS agora é `{engine}`.\n\n{extra}",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_server_tts_engine",
    description="Define a engine de TTS padrão do servidor"
)
@app_commands.describe(engine="Escolha `gtts` ou `edge`")
@app_commands.default_permissions(manage_guild=True)
async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    engine = validate_engine(engine)
    await db.set_guild_tts_defaults(interaction.guild.id, engine=engine)

    extra = (
        "Esse valor será usado como padrão para membros sem configuração própria."
    )

    await interaction.response.send_message(
        embed=self._make_embed(
            "Engine padrão atualizada",
            f"A engine padrão do servidor agora é `{engine}`.\n\n{extra}",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_voice",
    description="Define sua voz do Edge TTS"
)
@app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
async def set_voice(self, interaction: discord.Interaction, voice: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    if not self.edge_voice_cache:
        await self._load_edge_voices()

    voice = voice.strip()
    if voice not in self.edge_voice_names:
        await interaction.response.send_message(
            embed=self._make_embed(
                "Voz inválida",
                "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver opções válidas.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    await db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice)
    await interaction.response.send_message(
        embed=self._make_embed(
            "Voz Edge atualizada",
            f"Sua voz do Edge foi definida para `{voice}`.",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_server_voice",
    description="Define a voz padrão do Edge TTS no servidor"
)
@app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
@app_commands.default_permissions(manage_guild=True)
async def set_server_voice(self, interaction: discord.Interaction, voice: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    if not self.edge_voice_cache:
        await self._load_edge_voices()

    voice = voice.strip()
    if voice not in self.edge_voice_names:
        await interaction.response.send_message(
            embed=self._make_embed(
                "Voz inválida",
                "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver opções válidas.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    await db.set_guild_tts_defaults(interaction.guild.id, voice=voice)
    await interaction.response.send_message(
        embed=self._make_embed(
            "Voz Edge padrão atualizada",
            f"A voz padrão do servidor foi definida para `{voice}`.",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_language",
    description="Define seu idioma do gTTS"
)
@app_commands.describe(language="Exemplo: pt-br, en, es, fr")
async def set_language(self, interaction: discord.Interaction, language: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    if not self.gtts_languages:
        self.gtts_languages = get_gtts_languages()

    language = language.strip().lower()
    if language not in self.gtts_languages:
        await interaction.response.send_message(
            embed=self._make_embed(
                "Idioma inválido",
                "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver opções válidas.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    await db.set_user_tts(interaction.guild.id, interaction.user.id, language=language)
    await interaction.response.send_message(
        embed=self._make_embed(
            "Idioma gTTS atualizado",
            f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_server_language",
    description="Define o idioma padrão do gTTS no servidor"
)
@app_commands.describe(language="Exemplo: pt-br, en, es, fr")
@app_commands.default_permissions(manage_guild=True)
async def set_server_language(self, interaction: discord.Interaction, language: str):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    if not self.gtts_languages:
        self.gtts_languages = get_gtts_languages()

    language = language.strip().lower()
    if language not in self.gtts_languages:
        await interaction.response.send_message(
            embed=self._make_embed(
                "Idioma inválido",
                "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver opções válidas.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    await db.set_guild_tts_defaults(interaction.guild.id, language=language)
    await interaction.response.send_message(
        embed=self._make_embed(
            "Idioma gTTS padrão atualizado",
            f"O idioma padrão do servidor foi definido para `{language}` — {self.gtts_languages[language]}.",
            ok=True,
        ),
        ephemeral=True,
    )


@app_commands.command(
    name="set_rate",
    description="Define sua velocidade de fala no Edge TTS"
)
@app_commands.describe(rate="Formato: +0%, +25%, -10%")
async def set_rate(self, interaction: discord.Interaction, rate: str):
    await self._set_rate_common(interaction, rate=rate, server=False)


@app_commands.command(
    name="set_speed",
    description="Alias de /set_rate para velocidade de fala"
)
@app_commands.describe(speed="Formato: +0%, +25%, -10%")
async def set_speed(self, interaction: discord.Interaction, speed: str):
    await self._set_rate_common(interaction, rate=speed, server=False)


@app_commands.command(
    name="set_server_rate",
    description="Define a velocidade de fala padrão do servidor no Edge TTS"
)
@app_commands.describe(rate="Formato: +0%, +25%, -10%")
@app_commands.default_permissions(manage_guild=True)
async def set_server_rate(self, interaction: discord.Interaction, rate: str):
    await self._set_rate_common(interaction, rate=rate, server=True)


@app_commands.command(
    name="set_server_speed",
    description="Alias de /set_server_rate para velocidade padrão"
)
@app_commands.describe(speed="Formato: +0%, +25%, -10%")
@app_commands.default_permissions(manage_guild=True)
async def set_server_speed(self, interaction: discord.Interaction, speed: str):
    await self._set_rate_common(interaction, rate=speed, server=True)


async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    if server and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    value = rate.strip()
    if not RATE_RE.fullmatch(value):
        await interaction.response.send_message(
            embed=self._make_embed(
                "Velocidade inválida",
                "Use o formato `+0%`, `+25%` ou `-10%`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    if server:
        await db.set_guild_tts_defaults(interaction.guild.id, rate=value)
        title = "Velocidade padrão atualizada"
        desc = (
            f"A velocidade padrão do servidor foi definida para `{value}`.\n\n"
            "Esse ajuste só funciona com engine `edge`."
        )
    else:
        await db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value)
        title = "Velocidade atualizada"
        desc = (
            f"Sua velocidade foi definida para `{value}`.\n\n"
            "Esse ajuste só funciona com engine `edge`."
        )

    await interaction.response.send_message(
        embed=self._make_embed(title, desc, ok=True),
        ephemeral=True,
    )


@app_commands.command(
    name="set_pitch",
    description="Define seu tom de voz no Edge TTS"
)
@app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
async def set_pitch(self, interaction: discord.Interaction, pitch: str):
    await self._set_pitch_common(interaction, pitch=pitch, server=False)


@app_commands.command(
    name="set_server_pitch",
    description="Define o tom de voz padrão do servidor no Edge TTS"
)
@app_commands.describe(pitch="Formato: +0Hz, +20Hz, -10Hz")
@app_commands.default_permissions(manage_guild=True)
async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
    await self._set_pitch_common(interaction, pitch=pitch, server=True)


async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
    if not interaction.guild:
        await interaction.response.send_message("Esse comando só pode ser usado em servidor.", ephemeral=True)
        return

    if server and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Você precisa de `Gerenciar Servidor`.", ephemeral=True)
        return

    db = getattr(self.bot, "settings_db", None)
    if db is None:
        await interaction.response.send_message("Banco de dados indisponível.", ephemeral=True)
        return

    value = pitch.strip()
    if not PITCH_RE.fullmatch(value):
        await interaction.response.send_message(
            embed=self._make_embed(
                "Tom inválido",
                "Use o formato `+0Hz`, `+20Hz` ou `-10Hz`.\n\nEsse ajuste só funciona com engine `edge`.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    if server:
        await db.set_guild_tts_defaults(interaction.guild.id, pitch=value)
        title = "Tom padrão atualizado"
        desc = (
            f"O tom padrão do servidor foi definido para `{value}`.\n\n"
            "Esse ajuste só funciona com engine `edge`."
        )
    else:
        await db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value)
        title = "Tom atualizado"
        desc = (
            f"Seu tom foi definido para `{value}`.\n\n"
            "Esse ajuste só funciona com engine `edge`."
        )

    await interaction.response.send_message(
        embed=self._make_embed(title, desc, ok=True),
        ephemeral=True,
    )
