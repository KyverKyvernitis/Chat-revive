import discord

from ..common import validate_mode
from ..prefix import validate_prefix_values

async def _apply_server_prefix_from_modal(
    cog,
    interaction: discord.Interaction,
    *,
    prefix_kind: str,
    prefix: str,
    panel_message: discord.Message,
):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar os prefixos do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    cleaned = (prefix or "").strip()
    if not cleaned:
        await interaction.response.send_message(
            embed=cog._make_embed("Prefixo inválido", "O prefixo não pode ficar vazio.", ok=False),
            ephemeral=True,
        )
        return

    cleaned = cleaned[:8]
    defaults = dict(db.get_guild_tts_defaults(interaction.guild.id) or {})
    proposed = {
        "bot_prefix": str(defaults.get("bot_prefix") or "_"),
        "atts_prefix": str(defaults.get("atts_prefix") or "%"),
        "teto_prefix": str(defaults.get("teto_prefix") or "'"),
        "edge_prefix": str(defaults.get("edge_prefix") or ","),
        "gtts_prefix": str(defaults.get("gtts_prefix") or defaults.get("tts_prefix") or "."),
    }
    target_key = "gtts_prefix" if prefix_kind not in {"bot", "atts", "teto", "edge"} else f"{prefix_kind}_prefix"
    proposed[target_key] = cleaned
    migration_updates = {}
    if prefix_kind != "gtts":
        occupied_without_gtts = {
            proposed["bot_prefix"],
            proposed["atts_prefix"],
            proposed["teto_prefix"],
            proposed["edge_prefix"],
        }
        if proposed["gtts_prefix"] in occupied_without_gtts:
            replacement = next(
                (candidate for candidate in (".", "!", ";", "~", "?") if candidate not in occupied_without_gtts),
                ".",
            )
            proposed["gtts_prefix"] = replacement
            migration_updates = {"gtts_prefix": replacement, "tts_prefix": replacement}
    valid, validation_error = validate_prefix_values(**proposed)
    if not valid:
        await interaction.response.send_message(
            embed=cog._make_embed("Prefixo inválido", validation_error, ok=False),
            ephemeral=True,
        )
        return

    if prefix_kind == "bot":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, bot_prefix=cleaned, **migration_updates))
        desc = f"O prefixo do bot do servidor agora é `{cleaned}`"
        title = "Prefixo do bot atualizado"
    elif prefix_kind == "atts":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, atts_prefix=cleaned, **migration_updates))
        desc = f"O prefixo do ATTS do servidor agora é `{cleaned}`"
        title = "Prefixo do ATTS atualizado"
    elif prefix_kind == "teto":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, teto_prefix=cleaned, **migration_updates))
        desc = f"O prefixo da Kasane Teto do servidor agora é `{cleaned}`"
        title = "Prefixo da Kasane Teto atualizado"
    elif prefix_kind == "edge":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, edge_prefix=cleaned, **migration_updates))
        desc = f"O prefixo do modo Edge do servidor agora é `{cleaned}`"
        title = "Prefixo do modo Edge atualizado"
    else:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gtts_prefix=cleaned, tts_prefix=cleaned))
        desc = f"O prefixo do modo gTTS do servidor agora é `{cleaned}`"
        title = "Prefixo do modo gTTS atualizado"

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        interaction.user.id,
        server=True,
        panel_kind="server",
    )
    view = cog._build_panel_view(0 if getattr(panel_message, "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
    view.message = panel_message
    edited = False
    try:
        if getattr(interaction, "message", None) is not None and getattr(interaction.message, "id", None) == getattr(panel_message, "id", None):
            content, edit_embed, edit_view = cog._prepare_panel_payload(embed=embed, view=view)
            await interaction.response.edit_message(content=content, embed=edit_embed, view=edit_view)
            edited = True
        else:
            await cog._edit_panel_message_payload(panel_message, embed=embed, view=view)
            edited = True
    except discord.NotFound:
        print("[tts_panel] painel antigo não existe mais; seguindo sem editar")
    except Exception as e:
        print(f"[tts_panel] falha ao editar painel: {e!r}")

    if edited:
        await interaction.followup.send(
            embed=cog._make_embed(title, desc, ok=True),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            embed=cog._make_embed(title, desc, ok=True),
            ephemeral=True,
        )
    await cog._announce_panel_change(
        interaction,
        title=title,
        description=desc,
        target_message=panel_message,
    )

async def _apply_mode_from_panel(cog, interaction: discord.Interaction, mode: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    value = validate_mode(mode)
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
        desc = f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem."
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, engine=value)
        desc = f"O modo de TTS de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos ATTS, Kasane Teto, Edge e gTTS continuam escolhendo o motor por mensagem."

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo atualizado",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Modo atualizado", description=desc)

async def _apply_voice_from_panel(cog, interaction: discord.Interaction, voice: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    if voice not in cog.edge_voice_names and voice not in cog.edge_voice_cache:
        await interaction.response.send_message(
            embed=cog._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge.", ok=False),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
        desc = f"A voz padrão do servidor agora é `{voice}`."
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, voice=voice)
        desc = f"A voz do Edge de {effective_user_name} agora é `{voice}`." if effective_user_id != interaction.user.id else f"A sua voz do Edge agora é `{voice}`."

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_language_from_panel(cog, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
        desc = f"O idioma padrão do servidor agora é `{language}`."
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, language=language)
        desc = f"O idioma do gtts de {effective_user_name} agora é `{language}`." if effective_user_id != interaction.user.id else f"O seu idioma do gtts agora é `{language}`."

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_speed_from_panel(cog, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=speed))
        desc = f"A velocidade padrão do servidor agora é `{speed}`."
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, rate=speed)
        desc = f"A velocidade do Edge de {effective_user_name} agora é `{speed}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Edge agora é `{speed}`."

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_pitch_from_panel(cog, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch))
        desc = f"O tom padrão do servidor agora é `{pitch}`."
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, pitch=pitch)
        desc = f"O tom do Edge de {effective_user_name} agora é `{pitch}`." if effective_user_id != interaction.user.id else f"O seu tom do Edge agora é `{pitch}`."

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)


async def _apply_spoken_name_from_modal(
    cog,
    interaction: discord.Interaction,
    spoken_name: str,
    *,
    panel_message: discord.Message | None = None,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(
        interaction,
        server=False,
        message_id=message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
    )

    validated_name, validation_error = cog._validate_spoken_name_input(spoken_name)
    if validation_error:
        await interaction.response.send_message(
            embed=cog._make_embed("Apelido inválido", validation_error, ok=False),
            ephemeral=True,
        )
        return

    current_name = cog._get_saved_spoken_name(interaction.guild.id, effective_user_id)
    if str(validated_name or "") == str(current_name or ""):
        await interaction.response.send_message(
            embed=cog._make_embed("Nada mudou", "Nenhum ajuste foi alterado.", ok=True),
            ephemeral=True,
        )
        return

    if validated_name:
        await cog._set_user_tts_and_refresh(
            interaction.guild.id,
            effective_user_id,
            speaker_name=validated_name,
        )
        desc = "• Apelido atualizado"
    else:
        await cog._set_user_tts_and_refresh(
            interaction.guild.id,
            effective_user_id,
            speaker_name="",
        )
        desc = "• Apelido limpo"

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id,
        server=False,
        panel_kind="user",
        target_user_name=effective_user_name,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if is_public_user_panel else effective_user_id
    view_target_user_name = None if is_public_user_panel else effective_user_name
    view = cog._build_panel_view(
        0 if message_id in cog._public_panel_states else interaction.user.id,
        interaction.guild.id,
        server=False,
        target_user_id=view_target_user_id,
        target_user_name=view_target_user_name,
    )
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Apelido falado atualizado",
        description=desc,
        target_message=panel_message,
    )

async def _apply_announce_author_from_panel(cog, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, announce_author=bool(enabled)))
    desc = "Autor antes da frase ativado." if enabled else "Autor antes da frase desativado."
    embed = await cog._build_settings_embed(
        interaction.guild.id,
        interaction.user.id,
        server=True,
        panel_kind="server",
        viewer_user_id=interaction.user.id,
    )
    view = cog._build_panel_view(
        0 if message_id in cog._public_panel_states else interaction.user.id,
        interaction.guild.id,
        server=True,
    )
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo de TTS atualizado",
        description=desc,
        target_message=panel_message,
    )
    await cog._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)


async def _apply_auto_leave_from_panel(cog, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para usar esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message = source_panel_message or getattr(interaction, "message", None)
    await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, auto_leave=bool(enabled)))
    desc = f"Auto leave {'ativado' if enabled else 'desativado'}."
    embed = await cog._build_toggle_embed(interaction.guild.id, interaction.user.id)
    view = cog._build_toggle_view(0 if getattr(getattr(interaction, "message", None), "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id)
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo de TTS atualizado",
        description=desc,
        target_message=panel_message,
    )
    await cog._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)

