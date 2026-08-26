"""Slash commands do chatbot.

Organização:
- `/chatbot profile <acao> [profile?]` — acao=criar|listar|editar|apagar|ativar|desativar
- `/chatbot memoria` — reseta toda a memória do servidor
- `/chatbotadmin master <acao>` — ver/editar no config server; transferir por operador
- `/chatbotadmin reset_global` — apaga memória cross-guild (dono/operador)
- Todos exigem permissão Manage Guild (staff).
- `/reset` — qualquer membro, limpa a memória PESSOAL dele.

Em vez de subgroups (que ficam poluídos no autocomplete do Discord), usamos
comandos diretos com `app_commands.Choice` pra escolher a ação. Fica apenas 2
entradas enxutas em `/chatbot` em vez de subgroups espalhados.

Comandos globais sensíveis ficam no grupo separado `/chatbotadmin`. A
autorização é feita por subcomando no runtime: staff do config server pode
ver/editar, enquanto transferência e reset exigem dono/operador do bot.

Implementação como Mixin: a classe `ChatbotCommandsMixin` é herdada pelo
`ChatbotCog` em `cog.py`. Isso mantém o `ChatbotCog` como UM cog só (um único
extension do discord.py), mas divide a responsabilidade entre arquivos.

Todas as respostas são ephemeral (só quem rodou vê), exceto casos onde
a feedback faz sentido ser público (p.ex. ativar — server todo se beneficia
de saber que mudou o profile).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands

from . import constants as C
from .profiles import ProfileLimitReached
from .persona import (
    build_persona_generation_payload,
    build_user_style_system_prompt,
    collect_user_persona_samples,
    parse_persona_generation_response,
    resolve_member_avatar_url,
    resolve_member_display_name,
    PersonaModalConfig,
)
from .providers import AllProvidersExhausted, ProviderError
from .extrovert import ExtrovertModalConfig
from .views import (
    ConfirmView,
    ExtrovertConfigModal,
    MasterEditModal,
    PersonaConfigModal,
    ProfileCreateModal,
    ProfileEditModal,
)

log = logging.getLogger(__name__)


async def _staff_check(interaction: discord.Interaction) -> bool:
    """Check adicional em runtime — pra casos onde o Discord mostra o comando
    mesmo sem permissão (admins do server, ou se o default_permissions não
    sincronizou). Retorna True se user tem Manage Guild OU é owner do server."""
    member = interaction.user
    guild = interaction.guild
    if guild is None:
        return False
    if not isinstance(member, discord.Member):
        return False
    if member.id == guild.owner_id:
        return True
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


async def _operator_check(interaction: discord.Interaction) -> bool:
    """Dono do bot ou operador explicitamente configurado no ambiente."""
    try:
        if await interaction.client.is_owner(interaction.user):
            return True
    except Exception:
        pass
    configured: set[int] = set()
    for raw in os.environ.get("CHATBOT_OPERATOR_IDS", "").split(","):
        try:
            configured.add(int(raw.strip()))
        except ValueError:
            continue
    return int(getattr(interaction.user, "id", 0) or 0) in configured


async def _profile_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete de profile_id → mostra o NOME do profile, salva o ID.

    Acessa a instância da cog via interaction.client — precisa que o cog
    esteja carregado com self.bot.get_cog("Chatbot") disponível.

    Qualquer exceção aqui é silenciada — autocomplete nunca pode crashar
    (o Discord mostra "No options found" em vez de erro pro user).
    """
    try:
        cog = interaction.client.get_cog("Chatbot")
        if cog is None or interaction.guild is None:
            return []
        profiles = getattr(cog, "_profiles", None)
        if profiles is None:
            return []
        all_profiles = await profiles.list_profiles(interaction.guild.id)
    except Exception:
        log.exception("chatbot: falha no autocomplete")
        return []

    current_lower = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for p in all_profiles:
        label = p.name
        if p.active:
            label = f"⭐ {label}"  # indicador do ativo
        if current_lower and current_lower not in p.name.lower() and current_lower not in p.profile_id.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=p.profile_id))
        if len(out) >= 25:  # limite do Discord
            break
    return out


def _safe_slash(func):
    """Decorator: captura qualquer exceção em um slash command e responde ao
    usuário com erro amigável em vez de deixar "aplicativo não respondeu".

    O Discord dá só 3 segundos pra responder uma interaction, e se o comando
    ainda não chamou `interaction.response.*`, o user vê "aplicativo não
    respondeu". Com esse wrapper:
      - Exceção antes de responder → tenta responder com erro.
      - Exceção depois de responder → tenta followup.
      - Exceção no followup → só loga. Nada mais a fazer.

    Uso: aplica em cima de `@chatbot.command(...)` nos handlers.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except Exception:
            log.exception("chatbot: exceção em %s", func.__name__)
            err_msg = (
                "❌ Erro interno no comando. Já anotei nos logs, tenta de novo "
                "em alguns segundos."
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(err_msg, ephemeral=True)
                else:
                    await interaction.followup.send(err_msg, ephemeral=True)
            except Exception:
                pass  # último recurso: se nem o aviso de erro funciona, desiste

    return wrapper


class ChatbotCommandsMixin:
    """Mixin que adiciona todos os slash commands ao cog.

    A classe que herda DEVE ter os atributos: self.bot, self._profiles,
    self._memory, self._webhooks — todos preenchidos em cog_load. Os
    comandos validam isso e respondem com erro se o cog não estiver pronto.
    """

    # O grupo raiz /chatbot + subgrupos. Declarados como class-level —
    # discord.py automaticamente registra os subcomandos definidos com
    # decorator neles.
    #
    # Discord limita a 2 níveis de nesting: `/root subgroup command` é o
    # Discord tem um limite de 25 subcomandos por grupo, mas UX sofre muito antes.
    # Ao invés de subgroups aninhados (/chatbot profile criar, /chatbot master ver),
    # usamos comandos DIRETOS no grupo /chatbot, com as ações diferentes expostas
    # como Choice. Isso dá autocomplete enxuto (3 entradas em vez de 10) e URLs
    # mais curtas pra chamar.
    chatbot = app_commands.Group(
        name="chatbot",
        description="Gerenciamento do chatbot do servidor",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # Grupo global separado para operações cross-guild. Não usamos
    # default_permissions aqui porque isso bloquearia até um operador explícito
    # sem Manage Guild; cada handler aplica o check correto no runtime.
    chatbot_admin = app_commands.Group(
        name="chatbotadmin",
        description="Operações administrativas globais do bot",
    )

    # --- Verificação de estado do cog -----------------------------------------

    def _require_ready(self, interaction: discord.Interaction) -> bool:
        """Retorna True se cog está pronto. Caso contrário responde e retorna False."""
        profiles = getattr(self, "_profiles", None)
        memory = getattr(self, "_memory", None)
        if profiles is None or memory is None:
            # A interaction.response pode já ter sido usada em contextos raros;
            # tentamos responder como followup se for o caso.
            try:
                if not interaction.response.is_done():
                    # NOTA: não awaited — é só pra sinalizar defensivamente.
                    # O caller vai ver False e simplesmente retornar.
                    pass
            except Exception:
                pass
            return False
        return True

    # --- /chatbot criar -------------------------------------------------------

    async def _do_profile_criar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode criar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        # IMPORTANTE: abrir o modal PRIMEIRO (antes de ir ao Mongo).
        # Discord dá 3s pra responder a interaction; se formos ao Mongo antes
        # do send_modal e demorar >3s, aparece "aplicativo não respondeu".
        # A checagem de limite é feita lá no on_submit do modal (também dentro
        # do próprio create_profile, defensivamente).
        modal = ProfileCreateModal(store=self._profiles, guild_limit=C.MAX_PROFILES_PER_GUILD)
        await interaction.response.send_modal(modal)

    # --- /chatbot editar <profile> --------------------------------------------

    async def _do_profile_editar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode editar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        prof = await self._profiles.get_profile(guild.id, profile)
        if prof is None:
            await interaction.response.send_message(
                "Profile não encontrado. Use autocomplete para escolher.",
                ephemeral=True,
            )
            return

        modal = ProfileEditModal(store=self._profiles, profile=prof)
        await interaction.response.send_modal(modal)

    # --- /chatbot apagar <profile> --------------------------------------------

    async def _do_profile_apagar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode apagar profiles.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        prof = await self._profiles.get_profile(guild.id, profile)
        if prof is None:
            await interaction.followup.send(
                "Profile não encontrado.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=f"Apagar **{discord.utils.escape_markdown(prof.name)}**? Essa ação é irreversível.",
            confirm_label="Apagar",
        )
        await interaction.followup.send(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        ok = await self._profiles.delete_profile(guild.id, profile)
        if not ok:
            await interaction.followup.send(
                "❌ Profile não encontrado (pode ter sido apagado por outro admin).",
                ephemeral=True,
            )
            return

        # Limpa as memórias órfãs daquele profile antes de confirmar a operação.
        memory_count = 0
        try:
            memory_count = await self._memory.clear_profile_memory(
                guild.id, profile
            )
        except Exception:
            log.exception("chatbot: falha ao limpar memória do profile apagado")

        msg_tail = f" ({memory_count} memórias removidas)" if memory_count else ""
        await interaction.followup.send(
            f"🗑️ Profile **{discord.utils.escape_markdown(prof.name)}** "
            f"apagado.{msg_tail}",
            ephemeral=True,
        )

    # --- /chatbot listar ------------------------------------------------------

    async def _do_profile_listar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode ver a lista.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        profiles = await self._profiles.list_profiles(guild.id)
        if not profiles:
            await interaction.followup.send(
                f"Nenhum profile ainda. Use `/chatbot profile criar` para o primeiro "
                f"(limite: {C.MAX_PROFILES_PER_GUILD} por servidor).",
                ephemeral=True,
            )
            return

        lines = [
            f"**Profiles do servidor** ({len(profiles)}/{C.MAX_PROFILES_PER_GUILD})"
        ]
        for p in profiles:
            status = "⭐ ativo" if p.active else "inativo"
            prompt_preview = (p.system_prompt or "").strip().replace("\n", " ")
            if len(prompt_preview) > 80:
                prompt_preview = prompt_preview[:77] + "..."
            lines.append(
                f"\n**{discord.utils.escape_markdown(p.name)}** — {status}\n"
                f"`ID: {p.profile_id}` · temp `{p.temperature:.2f}` · memória `{p.history_size}`\n"
                f"> {discord.utils.escape_markdown(prompt_preview) or '(sem prompt)'}"
            )

        # Nota sobre comportamento variável por canal
        lines.append(
            "\n-# 💡 Comportamento varia por canal: em canais com age-restriction "
            "os profiles têm mais liberdade; em canais normais, tom é controlado. "
            "Proibições absolutas (menores, crimes reais) sempre valem."
        )

        text = "\n".join(lines)
        await interaction.followup.send(text[:2000], ephemeral=True)

    # --- /chatbot ativar <profile> --------------------------------------------

    async def _do_profile_ativar(self, interaction: discord.Interaction, profile: str):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode mudar o profile ativo.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        activated = await self._profiles.set_active_profile(guild.id, profile)
        if activated is None:
            await interaction.response.send_message(
                "Profile não encontrado.", ephemeral=True
            )
            return

        # Resposta PÚBLICA (não ephemeral) — o server todo se beneficia de
        # saber qual profile está ativo agora.
        # Embed pra ficar visualmente agradável e mostrar o avatar do profile.
        display_name = activated.name
        display_avatar = activated.avatar_url
        resolver = getattr(self, "_resolve_profile_identity", None)
        if callable(resolver):
            try:
                display_name, display_avatar = await resolver(guild, activated)
            except Exception:
                log.exception("chatbot: falha ao resolver identidade dinâmica")
        safe_name = discord.utils.escape_markdown(display_name)
        embed = discord.Embed(
            title=f"⭐ Chatbot ativo: {display_name}",
            description=(
                f"Mencione o bot no início de uma mensagem "
                f"(ex: {interaction.client.user.mention} oi) ou responda a "
                f"uma mensagem do **{safe_name}** para conversar."
            ),
            color=discord.Color.blurple(),
        )
        if display_avatar:
            try:
                embed.set_thumbnail(url=display_avatar)
            except Exception:
                pass  # URL inválida — só ignora o thumbnail
        await interaction.response.send_message(embed=embed)

    # --- /chatbot desativar ---------------------------------------------------

    async def _do_profile_desativar(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode desativar.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        count = await self._profiles.deactivate_all(guild.id)
        if count == 0:
            await interaction.response.send_message(
                "Nenhum profile estava ativo.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🚫 Chatbot desativado. Menções e replies não serão respondidos "
            "até reativar com `/chatbot profile acao:Ativar`.",
            ephemeral=False,
        )


    # --- /chatbot persona -----------------------------------------------------

    async def _resolve_selected_persona_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> Optional[discord.abc.Messageable]:
        guild = interaction.guild
        if guild is None:
            return None
        channel = None
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        if callable(get_channel_or_thread):
            channel = get_channel_or_thread(int(channel_id))
        if channel is None:
            channel = guild.get_channel(int(channel_id))
        if channel is None:
            channel = interaction.client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    async def _resolve_persona_member_or_user(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> Optional[discord.abc.User]:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            user = self.bot.get_user(int(user_id))
            if user is not None:
                return user
            try:
                return await self.bot.fetch_user(int(user_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

    def _persona_channel_permissions_ok(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
    ) -> bool:
        me = guild.me
        if me is None or not hasattr(channel, "permissions_for"):
            return False
        perms = channel.permissions_for(me)  # type: ignore[attr-defined]
        return bool(getattr(perms, "view_channel", False) and getattr(perms, "read_message_history", False))

    async def _do_persona(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode criar personas.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return
        if getattr(self, "_router", None) is None:
            await interaction.response.send_message(
                "Chatbot não está com provider de IA pronto.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        modal = PersonaConfigModal(
            requester_id=interaction.user.id,
            on_submit_config=self._handle_persona_modal,
        )
        await interaction.response.send_modal(modal)

    async def _handle_persona_modal(
        self,
        interaction: discord.Interaction,
        config: PersonaModalConfig,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Só funciona em servidor.", ephemeral=True)
            return
        if self._profiles is None or getattr(self, "_router", None) is None:
            await interaction.followup.send("Chatbot não está pronto.", ephemeral=True)
            return

        target = await self._resolve_persona_member_or_user(guild, config.target_user_id)
        if target is None:
            await interaction.followup.send(
                "Não encontrei esse usuário no servidor.", ephemeral=True
            )
            return
        if getattr(target, "bot", False):
            await interaction.followup.send(
                "Persona de bot não é suportada.", ephemeral=True
            )
            return

        channel = await self._resolve_selected_persona_channel(interaction, config.channel_id)
        if channel is None:
            await interaction.followup.send(
                "Escolha um canal de texto ou thread válido.", ephemeral=True
            )
            return
        if not self._persona_channel_permissions_ok(channel, guild):
            await interaction.followup.send(
                "Não tenho permissão para ler histórico nesse canal.", ephemeral=True
            )
            return

        display_name = resolve_member_display_name(target)
        avatar_url = resolve_member_avatar_url(target)

        if config.action == "remove":
            existing_persona = await self._profiles.get_user_style_profile(
                guild.id, config.target_user_id,
            )
            deleted = await self._profiles.delete_user_style_profile(guild.id, config.target_user_id)
            if deleted:
                if existing_persona is not None and self._memory is not None:
                    await self._memory.clear_profile_memory(
                        guild.id, existing_persona.profile_id,
                    )
                await interaction.followup.send(
                    f"Persona de **{discord.utils.escape_markdown(display_name)}** removida.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "Esse usuário não tinha persona criada.", ephemeral=True,
                )
            return

        lease = await self._admission.try_admit(
            "persona", guild_id=guild.id, user_id=interaction.user.id,
        )
        if lease is None:
            await interaction.followup.send(
                "⏳ Já há uma persona sua em processamento ou a fila está cheia.",
                ephemeral=True,
            )
            return
        async with lease:
            await self._generate_persona_profile(
                interaction=interaction,
                config=config,
                guild=guild,
                channel=channel,
                display_name=display_name,
                avatar_url=avatar_url,
            )

    async def _generate_persona_profile(
        self,
        *,
        interaction: discord.Interaction,
        config: PersonaModalConfig,
        guild: discord.Guild,
        channel,
        display_name: str,
        avatar_url: str,
    ) -> None:
        try:
            sample_result = await collect_user_persona_samples(
                channel=channel,
                user_id=config.target_user_id,
                sample_limit=config.sample_limit,
                options=config.options,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Não tenho permissão para ler esse canal.", ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("chatbot: falha ao coletar mensagens para persona")
            await interaction.followup.send(
                "Falha ao ler histórico do canal.", ephemeral=True
            )
            return

        if len(sample_result.samples) < C.PERSONA_MIN_GOOD_MESSAGES:
            await interaction.followup.send(
                f"Não encontrei mensagens suficientes desse usuário nesse canal. "
                f"Boas: {len(sample_result.samples)} · mínimo: {C.PERSONA_MIN_GOOD_MESSAGES}.",
                ephemeral=True,
            )
            return

        system, messages = build_persona_generation_payload(
            display_name=display_name,
            samples=sample_result.samples,
            avoid_exact_copy=config.options.avoid_exact_copy,
        )
        try:
            raw = await asyncio.wait_for(
                self._router.chat(
                    system=system,
                    messages=messages,
                    temperature=C.PERSONA_GENERATION_TEMPERATURE,
                ),
                timeout=C.PERSONA_GENERATION_TIMEOUT_SECONDS,
            )
        except AllProvidersExhausted:
            await interaction.followup.send(
                "Nenhum provider de IA está disponível agora.", ephemeral=True
            )
            return
        except (ProviderError, asyncio.TimeoutError):
            log.exception("chatbot: falha ao gerar persona")
            await interaction.followup.send(
                "Falha ao gerar a persona com IA. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return

        parsed = parse_persona_generation_response(raw)
        if not parsed.style_prompt:
            await interaction.followup.send(
                "A IA não retornou uma personalidade válida.", ephemeral=True
            )
            return

        system_prompt = build_user_style_system_prompt(parsed.style_prompt)
        try:
            profile, created = await self._profiles.upsert_user_style_profile(
                guild_id=guild.id,
                source_user_id=config.target_user_id,
                source_channel_id=config.channel_id,
                created_by=interaction.user.id,
                fallback_name=display_name,
                fallback_avatar_url=avatar_url,
                system_prompt=system_prompt,
                sample_count=len(sample_result.samples),
                activate=config.options.activate_after_create,
            )
        except ProfileLimitReached:
            await interaction.followup.send(
                f"Limite de {C.MAX_PROFILES_PER_GUILD} profiles atingido. "
                "Apague um profile antes de criar uma persona nova.",
                ephemeral=True,
            )
            return

        action_text = "criada" if created else "atualizada"
        active_text = " e ativada" if profile.active else ""
        channel_mention = getattr(channel, "mention", f"`{config.channel_id}`")
        await interaction.followup.send(
            f"Persona de **{discord.utils.escape_markdown(display_name)}** {action_text}{active_text}.\n"
            f"Base: {len(sample_result.samples)} mensagens em {channel_mention}.\n"
            f"`ID: {profile.profile_id}`",
            ephemeral=True,
        )

    # --- /chatbot extrovert ---------------------------------------------------

    async def _do_extrovert(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode configurar o extrovert.", ephemeral=True
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto. Tenta de novo em alguns segundos.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return
        extrovert_store = getattr(self, "_extrovert", None)
        if extrovert_store is None:
            await interaction.response.send_message(
                "Extrovert não está pronto.", ephemeral=True
            )
            return

        profiles = await self._profiles.list_profiles(interaction.guild.id)
        if not profiles:
            await interaction.response.send_message(
                "Crie pelo menos um profile antes de ativar o extrovert.",
                ephemeral=True,
            )
            return

        current = await extrovert_store.get_config(interaction.guild.id)
        modal = ExtrovertConfigModal(
            requester_id=interaction.user.id,
            profiles=profiles,
            current_config=current,
            on_submit_config=self._handle_extrovert_modal,
        )
        await interaction.response.send_modal(modal)

    async def _handle_extrovert_modal(
        self,
        interaction: discord.Interaction,
        config: ExtrovertModalConfig,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Só funciona em servidor.", ephemeral=True)
            return
        if self._profiles is None or getattr(self, "_extrovert", None) is None:
            await interaction.followup.send("Chatbot não está pronto.", ephemeral=True)
            return

        extrovert_store = self._extrovert
        selected_profiles = list(config.profile_ids)
        selected_channels = list(config.channel_ids)

        all_profiles = await self._profiles.list_profiles(guild.id)
        valid_profile_ids = {p.profile_id for p in all_profiles}
        selected_profiles = [pid for pid in selected_profiles if pid in valid_profile_ids]

        valid_channels = []
        for channel_id in selected_channels:
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
                if callable(get_channel_or_thread):
                    channel = get_channel_or_thread(int(channel_id))
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)):
                valid_channels.append(int(channel_id))

        if config.enabled:
            if not selected_profiles:
                await interaction.followup.send(
                    "Escolha pelo menos um profile válido para ativar o extrovert.",
                    ephemeral=True,
                )
                return
            if not valid_channels:
                await interaction.followup.send(
                    "Escolha pelo menos um canal válido para ativar o extrovert.",
                    ephemeral=True,
                )
                return
            if not (config.options.allow_text_channels or config.options.allow_voice_channels):
                await interaction.followup.send(
                    "Deixe canais de texto ou canais de voz marcados.",
                    ephemeral=True,
                )
                return

        saved = await extrovert_store.save_config(
            guild_id=guild.id,
            enabled=config.enabled,
            chance_percent=config.chance_percent,
            profile_ids=selected_profiles,
            channel_ids=valid_channels,
            options=config.options,
            updated_by=interaction.user.id,
        )

        if not saved.enabled:
            await interaction.followup.send("Extrovert desativado.", ephemeral=True)
            return

        await interaction.followup.send(
            f"Extrovert ativado com {saved.chance_percent}% de chance "
            f"em {len(saved.channel_ids)} canal(is) para {len(saved.profile_ids)} profile(s).",
            ephemeral=True,
        )


    # --- /chatbot reset_server ------------------------------------------------

    async def _do_memoria_reset_server(self, interaction: discord.Interaction):
        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode resetar memória do servidor.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                "⚠️ Isso apaga **toda** a memória do chatbot neste servidor — "
                "pessoal de cada membro + coletiva. Operação irreversível. "
                "Confirma?"
            ),
            confirm_label="Apagar tudo",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        count = await self._memory.clear_all_guild_memory(guild.id)
        await interaction.followup.send(
            f"🧹 Memória do chatbot resetada. ({count} registros removidos)",
            ephemeral=True,
        )

    # --- /chatbot master ------------------------------------------------------
    # Comandos de staff, mas com check adicional: só funcionam se a pessoa
    # está no config_guild_id atual. Quem controla o master prompt é o dono
    # do bot, e ele define qual server tem essa "autoridade".

    async def _master_check(
        self, interaction: discord.Interaction
    ) -> Optional[object]:
        """Valida que o comando pode rodar aqui.

        Retorna a MasterConfig atual se tudo ok (e o caller prossegue),
        ou None se falhou (e já respondeu com erro ao user).

        Checks:
          1. Cog pronto (master store inicializado)
          2. Usuário é staff NESTE server
          3. Este server é o config_guild_id atual
        """
        master = getattr(self, "_master", None)
        if master is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return None

        if not await _staff_check(interaction):
            await interaction.response.send_message(
                "Só staff (Manage Server) pode mexer no prompt mestre.",
                ephemeral=True,
            )
            return None

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return None

        cfg = await master.get()
        if int(guild.id) != int(cfg.config_guild_id):
            await interaction.response.send_message(
                "❌ Este servidor não é o **config server** do bot. "
                f"O prompt mestre só pode ser editado no servidor de "
                f"configuração (ID atualmente: `{cfg.config_guild_id}`).\n"
                f"-# Pra transferir a config pra este server, rode "
                f"`/chatbotadmin master acao:Transferir` a partir do server atual.",
                ephemeral=True,
            )
            return None

        return cfg

    async def _do_master_ver(self, interaction: discord.Interaction):
        cfg = await self._master_check(interaction)
        if cfg is None:
            return

        import datetime as _dt
        updated_str = "nunca"
        if cfg.updated_at > 0:
            updated_str = _dt.datetime.fromtimestamp(
                cfg.updated_at
            ).strftime("%Y-%m-%d %H:%M")

        # Se muito longo, usa code block numa mensagem só. Se ultra longo,
        # anexaria arquivo — mas como limite é 4000 chars e Discord aceita
        # 2000 na mensagem, talvez precise truncar.
        content = cfg.prompt or "(vazio — usando default)"
        preview = content[:1700]
        if len(content) > 1700:
            preview += "\n... (truncado no preview)"

        await interaction.response.send_message(
            f"**Prompt mestre atual** ({len(content)} chars, "
            f"última edição: {updated_str}):\n```\n{preview}\n```",
            ephemeral=True,
        )

    async def _do_master_editar(self, interaction: discord.Interaction):
        cfg = await self._master_check(interaction)
        if cfg is None:
            return

        modal = MasterEditModal(
            master_store=self._master,
            current_content=cfg.prompt,
        )
        await interaction.response.send_modal(modal)

    async def _do_master_transferir(
        self, interaction: discord.Interaction, destino: str,
    ):
        master = getattr(self, "_master", None)
        if master is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        if not await _operator_check(interaction):
            await interaction.response.send_message(
                "Só o dono do bot ou um operador autorizado pode transferir a config.",
                ephemeral=True,
            )
            return
        try:
            target_id = int(str(destino or "").strip())
        except ValueError:
            target_id = 0
        target = self.bot.get_guild(target_id) if target_id > 0 else None
        if target is None:
            await interaction.response.send_message(
                "Informe em `destino` o ID de um servidor em que o bot esteja.",
                ephemeral=True,
            )
            return
        cfg = await master.get()
        if target_id == int(cfg.config_guild_id):
            await interaction.response.send_message(
                "Este servidor **já é** o config server. Nada a fazer.",
                ephemeral=True,
            )
            return

        # Confirmação: ação destrutiva (perda de autoridade do server antigo)
        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                f"⚠️ Transferir autoridade do prompt mestre pra **{target.name}** "
                f"(`{target.id}`)? O server antigo (`{cfg.config_guild_id}`) "
                f"perderá o acesso ao `/chatbotadmin master`."
            ),
            confirm_label="Transferir",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True,
        )
        await view.wait()
        if view.result is not True:
            return

        new_cfg = await master.set_config_guild(
            target.id,
            updated_by=interaction.user.id,
        )
        await interaction.followup.send(
            f"✅ Config server atualizado pra **{target.name}** "
            f"(`{new_cfg.config_guild_id}`).",
            ephemeral=True,
        )

    # =========================================================================
    # Entrypoints diretos — evita o sufixo `acao` nos comandos.
    # =========================================================================

    # --- /chatbot profile <acao> [profile?] -----------------------------------
    # Um comando só que cobre as 6 ações de profile. Profile é opcional na UI
    # porque "criar", "listar" e "desativar" não precisam dele; o runtime valida
    # que "editar", "apagar" e "ativar" receberam um profile.
    @chatbot.command(
        name="profile",
        description="Criar, listar, editar, apagar, ativar ou desativar profiles",
    )
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Criar novo profile", value="criar"),
            app_commands.Choice(name="Listar todos os profiles", value="listar"),
            app_commands.Choice(name="Editar um profile", value="editar"),
            app_commands.Choice(name="Apagar um profile", value="apagar"),
            app_commands.Choice(name="Ativar um profile no servidor", value="ativar"),
            app_commands.Choice(name="Desativar o chatbot no servidor", value="desativar"),
        ]
    )
    @app_commands.autocomplete(profile=_profile_autocomplete)
    @_safe_slash
    async def chatbot_profile(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        profile: Optional[str] = None,
    ):
        action = acao.value
        # Valida que as ações que exigem profile receberam um.
        if action in ("editar", "apagar", "ativar") and not profile:
            await interaction.response.send_message(
                f"❌ A ação **{acao.name}** exige que você escolha um profile "
                f"no campo `profile`.",
                ephemeral=True,
            )
            return
        if action == "criar":
            await self._do_profile_criar(interaction)
        elif action == "listar":
            await self._do_profile_listar(interaction)
        elif action == "desativar":
            await self._do_profile_desativar(interaction)
        elif action == "editar":
            await self._do_profile_editar(interaction, profile)  # type: ignore[arg-type]
        elif action == "apagar":
            await self._do_profile_apagar(interaction, profile)  # type: ignore[arg-type]
        elif action == "ativar":
            await self._do_profile_ativar(interaction, profile)  # type: ignore[arg-type]

    # --- /chatbot persona -----------------------------------------------------
    @chatbot.command(
        name="persona",
        description="Criar, atualizar ou remover persona baseada em um usuário",
    )
    @_safe_slash
    async def chatbot_persona(self, interaction: discord.Interaction):
        await self._do_persona(interaction)

    # --- /chatbot extrovert ---------------------------------------------------
    @chatbot.command(
        name="extrovert",
        description="Configurar chance de profiles responderem sem menção",
    )
    @_safe_slash
    async def chatbot_extrovert(self, interaction: discord.Interaction):
        await self._do_extrovert(interaction)

    # --- /chatbot memoria -----------------------------------------------------
    # Antes era um subgroup com 1 só comando (`reset_server`). Agora é comando
    # direto porque não faz sentido subgroup pra 1 ação.
    @chatbot.command(
        name="memoria",
        description="Resetar toda a memória do servidor (destrutivo)",
    )
    @_safe_slash
    async def chatbot_memoria(self, interaction: discord.Interaction):
        await self._do_memoria_reset_server(interaction)

    # --- /chatbotadmin master <acao> ------------------------------------------
    # Grupo global: autorização granular ocorre nos handlers abaixo.
    @chatbot_admin.command(
        name="master",
        description="Ver, editar ou transferir o prompt mestre global",
    )
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Ver prompt mestre", value="ver"),
            app_commands.Choice(name="Editar prompt mestre", value="editar"),
            app_commands.Choice(name="Transferir config para outro servidor", value="transferir"),
        ]
    )
    @app_commands.describe(
        destino="ID do servidor de destino (obrigatório ao transferir)",
    )
    @_safe_slash
    async def chatbotadmin_master(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        destino: str = "",
    ):
        if acao.value == "ver":
            await self._do_master_ver(interaction)
        elif acao.value == "editar":
            await self._do_master_editar(interaction)
        elif acao.value == "transferir":
            await self._do_master_transferir(interaction, destino)

    # --- /chatbotadmin reset_global -------------------------------------------
    # Apaga TODA memória do chatbot — todas as guilds, todos os profiles,
    # pessoal e coletiva. O runtime exige dono/operador e confirmação explícita.
    @chatbot_admin.command(
        name="reset_global",
        description="Apagar TODA a memória do chatbot em TODAS as guilds (irreversível)",
    )
    @_safe_slash
    async def chatbotadmin_reset_global(self, interaction: discord.Interaction):
        if not await _operator_check(interaction):
            await interaction.response.send_message(
                "Só o dono do bot ou um operador autorizado pode rodar isso.",
                ephemeral=True,
            )
            return
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        view = ConfirmView(
            requester_id=interaction.user.id,
            prompt=(
                "🚨 **RESET GLOBAL** 🚨\n\n"
                "Isso apaga **toda** a memória do chatbot em **todas as guilds** "
                "onde o bot está presente — pessoal de cada membro + coletiva, "
                "todos os profiles. Operação **irreversível**.\n\n"
                "Confirma?"
            ),
            confirm_label="Apagar tudo (cross-guild)",
        )
        await interaction.response.send_message(
            view.prompt, view=view, ephemeral=True
        )
        await view.wait()
        if view.result is not True:
            return

        count = await self._memory.clear_all_memory_everywhere()
        log.warning(
            "chatbot: reset_global executado | requester=%s registros_apagados=%s",
            interaction.user.id, count,
        )
        await interaction.followup.send(
            f"🧹 Memória global do chatbot apagada. ({count} registros removidos "
            f"em todas as guilds)",
            ephemeral=True,
        )

    # --- /imagem <prompt> — gera imagem via Gemini (qualquer membro) ---------
    # Top-level (não em /chatbot) porque /chatbot é staff-only via
    # default_permissions. Imagegen é liberado pra qualquer membro.

    async def _run_image_command(
        self, interaction: discord.Interaction, *, prompt: str
    ) -> None:
        import io as _io
        from . import imagegen as _imagegen

        guild = interaction.guild
        if guild is None or self._image_service is None:
            return
        active = await self._profiles.get_active_profile(guild.id)
        if active is None:
            await interaction.followup.send(
                "Não há profile ativo neste servidor. A staff precisa ativar "
                "um com `/chatbot profile acao:Ativar` antes.", ephemeral=True,
            )
            return
        active = await self._profile_with_resolved_identity(guild, active)
        channel_nsfw = bool(getattr(interaction.channel, "nsfw", False))
        effective_nsfw = channel_nsfw and C.nsfw_enabled_for_guild(guild.id)
        generated = await self._image_service.generate(
            prompt=prompt, channel_is_nsfw=effective_nsfw, slot_acquired=True,
        )
        if not generated.ok or generated.image is None:
            await interaction.followup.send(
                _imagegen.build_image_failure_message(generated), ephemeral=True,
            )
            return

        ext = _imagegen.generated_image_extension(generated.image.mime_type)
        safe_name = "".join(c for c in active.name if c.isalnum())[:20] or "image"
        filename = f"{safe_name}.{ext}"
        caption = f"🖼️ Imagem gerada para: *{prompt[:200]}*"
        channel = interaction.channel
        if not isinstance(
            channel,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread),
        ):
            await interaction.followup.send(
                content=caption,
                file=discord.File(_io.BytesIO(generated.image.data), filename=filename),
            )
            return
        sent = await self._webhooks.send_as_profile(
            channel=channel,
            profile_name=active.name,
            avatar_url=active.avatar_url,
            content=caption,
            files=[discord.File(_io.BytesIO(generated.image.data), filename=filename)],
        )
        if sent is None:
            await interaction.followup.send(
                content=caption,
                file=discord.File(_io.BytesIO(generated.image.data), filename=filename),
            )
            return
        await self._remember_sent_profile_message(
            guild_id=guild.id, channel_id=channel.id,
            message_id=sent.id, profile_id=active.profile_id,
        )
        if self._memory is not None:
            is_private = bool(
                isinstance(channel, discord.Thread)
                and getattr(channel, "is_private", lambda: False)()
            )
            from .memory import visibility_scope_for
            visibility = visibility_scope_for(
                channel.id, is_nsfw=effective_nsfw, is_private=is_private,
            )
            epoch = await self._memory.capture_epoch(guild.id, interaction.user.id)
            await self._persist_turn(
                guild_id=guild.id,
                profile_id=active.profile_id,
                profile_revision=active.revision,
                channel_id=channel.id,
                visibility_scope=visibility,
                epoch=epoch,
                user_id=interaction.user.id,
                user_name=str(getattr(interaction.user, "display_name", interaction.user.name)),
                user_message=prompt,
                assistant_message=f"[imagem gerada: {prompt[:500]}]",
                user_history_size=active.history_size,
            )
        await interaction.followup.send("✅ Imagem enviada no canal.", ephemeral=True)

    @app_commands.command(
        name="imagem",
        description="Gera uma imagem a partir da descrição (usa o profile ativo)",
    )
    @app_commands.describe(
        prompt="Descrição da imagem que você quer gerar (ex: 'gato siamês surfando')",
    )
    @_safe_slash
    async def imagem(
        self,
        interaction: discord.Interaction,
        prompt: str,
    ):
        prompt = prompt.strip()
        if not prompt:
            await interaction.response.send_message(
                "Descreva a imagem que você quer gerar.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando só funciona em servidor.", ephemeral=True
            )
            return
        if self._profiles is None or self._webhooks is None or self._image_service is None:
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        if C.SAFE_MODE:
            await interaction.response.send_message(
                "Geração de imagem está temporariamente em modo de recuperação.",
                ephemeral=True,
            )
            return
        if self._is_user_on_cooldown(guild.id, interaction.user.id):
            await interaction.response.send_message(
                "⌛ Espera um pouco antes de pedir outra imagem.", ephemeral=True
            )
            return
        lease = await self._admission.try_admit(
            "image", guild_id=guild.id, user_id=interaction.user.id,
        )
        if lease is None:
            await interaction.response.send_message(
                "⏳ A fila de imagens está cheia ou você já tem um pedido em andamento.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        async with lease:
            self._apply_user_cooldown(guild.id, interaction.user.id)
            await self._run_image_command(
                interaction, prompt=prompt[:1000],
            )

    # --- /reset (sem /chatbot) — qualquer membro ------------------------------

    @app_commands.command(
        name="reset",
        description="Reseta sua memória pessoal com o chatbot",
    )
    @_safe_slash
    async def reset(self, interaction: discord.Interaction):
        if not self._require_ready(interaction):
            await interaction.response.send_message(
                "Chatbot não está pronto.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Só funciona em servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Apaga memória do user com TODOS os profiles do server (sem
        # profile_id = wildcard). É o que o user espera: "esquece tudo
        # que sabe sobre mim aqui".
        count = await self._memory.clear_user_history(
            guild.id, interaction.user.id
        )
        if count > 0:
            msg = (
                f"✅ Sua memória pessoal com o chatbot foi resetada "
                f"({count} registros). O bot vai começar do zero com você.\n"
                f"-# As conversas dos outros membros não foram apagadas."
            )
        else:
            msg = "Você ainda não tinha memória salva com o chatbot."
        await interaction.followup.send(msg, ephemeral=True)
