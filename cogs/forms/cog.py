"""FormsCog — sistema de formulário com botão persistente.

==============================================================================
TRIGGERS (mensagem inteira após strip().lower(), staff-only)
==============================================================================

- form / formulário / formulario
    - Setup incompleto (canais não configurados): em qualquer canal abre o
      wizard de setup (ChannelSelect x2 + Confirmar).
    - Setup completo: só funciona dentro do canal de formulário. Apaga a
      palavra do staff, apaga a mensagem do form anterior, posta uma nova.

- c
    - Setup incompleto: ignorado.
    - Setup completo: funciona no canal de form OU canal de respostas.
      Apaga a sessão 'c' anterior (independente de quem disparou), apaga
      a palavra 'c' atual, posta o painel de customização.

==============================================================================
PERSISTÊNCIA
==============================================================================

O botão do form (FormView) é persistente — sobrevive reboots porque:
1. Tem custom_id estável: `forms:submit:{guild_id}`.
2. É registrado via bot.add_view(view, message_id=...) em cog_load + on_ready.
3. Active message_id fica salvo em forms.active_message_id no DB.

==============================================================================
SESSÃO 'c' E O BOTÃO "APAGAR"
==============================================================================

Cada `c` apaga a sessão `c` anterior (mensagem trigger + painel) **antes**
de criar a nova sessão. Isso é independente de quem disparou — staff A pode
apagar painel aberto pelo staff B.

O botão "Apagar" e o on_timeout do painel chamam o mesmo método
(_purge_previous_c_session), então o comportamento é simétrico.

==============================================================================
PERMISSÕES E FALLBACK DE DELETE
==============================================================================

Pra apagar mensagens (palavra do trigger, form antigo, painel antigo) o bot
precisa de Manage Messages no canal. Quando falta:
1. Tenta delete normal.
2. Em Forbidden, manda DM pro autor da mensagem alertando.
3. Se DM falhar, posta aviso no canal com auto-delete em 30s.
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any

import discord
from discord.ext import commands

from .constants import (
    DEFAULT_APPROVAL,
    DEFAULT_MODAL,
    DEFAULT_PANEL,
    DEFAULT_RESPONSE,
    MODAL_FIELD_LIMIT,
    TRIGGER_WORD_CUSTOMIZE,
    TRIGGER_WORDS_FORM,
)
from .fields import (
    get_field_value,
    next_field_id,
    normalize_form_fields,
    normalize_modal_config,
    slugify_placeholder,
    sync_legacy_field_keys,
)
from .modals import FormSubmissionModal
from .views import CustomizationPanelView, FormView, ResponseReviewView, SetupView


log = logging.getLogger(__name__)


def _is_staff_member(member: discord.Member) -> bool:
    """Critério de staff: kick_members OR manage_guild OR administrator.

    Espelha o helper de gincana (kick_members) mas inclui manage_guild
    pra alcançar staff de admin/mod sem permissões de kick. Forms não
    define um staff_role próprio no DB então não dá pra reusar 1:1
    o helper que depende de gincana_staff_role_id.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(perms.kick_members or perms.manage_guild or perms.administrator)


class FormsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Lock por guild pra serializar processamento de triggers — evita
        # corrida com 2 staffs digitando "form" ao mesmo tempo.
        self._guild_locks: dict[int, asyncio.Lock] = {}
        # Registro de quais (guild_id, message_id) já tiveram view persistente
        # registrada, pra evitar registrar 2x em on_ready/cog_load reentrant.
        self._registered_persistent_views: set[tuple[int, int]] = set()
        self._registered_review_views: set[tuple[int, int]] = set()

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    # ===== Lifecycle =====

    async def cog_load(self):
        await self._restore_persistent_form_views()
        await self._restore_persistent_review_views()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_persistent_form_views()
        await self._restore_persistent_review_views()

    async def _restore_persistent_form_views(self):
        """Itera todas as guilds conhecidas e re-registra a FormView ativa.

        Idempotente — já evita re-registro via _registered_persistent_views.
        Erros individuais são logados e não bloqueiam outras guilds.
        """
        db = self.db
        if db is None:
            return
        for gid in sorted(self._known_guild_ids()):
            cfg = self._get_config(gid)
            mid = int(cfg.get("active_message_id") or 0)
            if not mid:
                continue
            key = (gid, mid)
            if key in self._registered_persistent_views:
                continue
            try:
                view = FormView(self, gid)
                self.bot.add_view(view, message_id=mid)
                self._registered_persistent_views.add(key)
            except Exception as e:
                log.warning("[forms] falha ao registrar view persistente gid=%s mid=%s: %r", gid, mid, e)

    def _known_guild_ids(self) -> set[int]:
        db = self.db
        guild_ids: set[int] = set()
        if db is not None and hasattr(db, "guild_cache"):
            guild_ids.update(int(gid) for gid in db.guild_cache.keys() if gid)
        for guild in getattr(self.bot, "guilds", []):
            gid = int(getattr(guild, "id", 0) or 0)
            if gid:
                guild_ids.add(gid)
        return guild_ids

    async def _restore_persistent_review_views(self):
        """Re-registra botões Aprovar/Rejeitar de submissões pendentes.

        Sem isso, mensagens de verificação enviadas antes de um restart continuam
        aparecendo no Discord, mas os botões perdem o handler Python e o cliente
        mostra "Esta interação falhou".
        """
        if self.db is None:
            return

        for gid in sorted(self._known_guild_ids()):
            cfg = self._get_config(gid)
            pending = self._normalize_pending_reviews(cfg.get("pending_reviews"))
            if pending != cfg.get("pending_reviews"):
                cfg["pending_reviews"] = pending
                await self._save_config(gid, cfg)

            for entry in pending:
                message_id = int(entry.get("message_id") or 0)
                channel_id = int(entry.get("channel_id") or 0)
                applicant_id = int(entry.get("applicant_id") or 0)
                if not (message_id and channel_id and applicant_id):
                    continue
                key = (gid, message_id)
                if key in self._registered_review_views:
                    continue
                try:
                    view = ResponseReviewView(
                        self,
                        guild_id=gid,
                        applicant_id=applicant_id,
                        field_values=dict(entry.get("field_values") or {}),
                        force_buttons=True,
                    )
                    self.bot.add_view(view, message_id=message_id)
                    self._registered_review_views.add(key)
                except Exception as e:
                    log.warning("[forms] falha ao registrar review persistente gid=%s mid=%s: %r", gid, message_id, e)

    # ===== Config helpers =====

    def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_forms_config"):
            return self._normalize_config(self._default_config())
        try:
            return self._normalize_config(db.get_forms_config(int(guild_id)))
        except Exception as e:
            log.warning("[forms] erro ao ler config gid=%s: %r", guild_id, e)
            return self._normalize_config(self._default_config())

    @staticmethod
    def _normalize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
        """Completa chaves novas e corrige defaults legados sem mexer em textos realmente customizados."""
        base = FormsCog._default_config()
        if not isinstance(cfg, dict):
            return base

        legacy_enabled = None
        if "enabled" not in cfg:
            legacy_enabled = bool(
                int(cfg.get("form_channel_id") or 0)
                and int(cfg.get("responses_channel_id") or 0)
                and int(cfg.get("active_message_id") or 0)
            )

        for key, value in base.items():
            if key not in cfg:
                cfg[key] = deepcopy(value)

        cfg["enabled"] = bool(legacy_enabled if legacy_enabled is not None else cfg.get("enabled", False))

        for section in ("panel", "modal", "response", "approval"):
            current = cfg.get(section)
            if not isinstance(current, dict):
                current = {}
            merged = deepcopy(base.get(section) or {})
            merged.update(current)
            if section == "modal" and "fields" not in current:
                # Sem isso, os fields default do base impedem a migração correta
                # de configs antigas que só têm field1_label/field2_label/field3_label.
                merged.pop("fields", None)
            cfg[section] = merged

        cfg["modal"] = normalize_modal_config(cfg.get("modal") or {})
        return cfg

    async def _save_config(self, guild_id: int, cfg: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "set_forms_config"):
            log.warning("[forms] settings_db sem set_forms_config — config NÃO foi salva")
            return
        try:
            await db.set_forms_config(int(guild_id), cfg)
        except Exception:
            log.exception("[forms] falha ao salvar config gid=%s", guild_id)

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "enabled": False,
            "form_channel_id": 0,
            "responses_channel_id": 0,
            "active_message_id": 0,
            "active_c_trigger": {"channel_id": 0, "message_id": 0},
            "active_c_panel": {"channel_id": 0, "message_id": 0},
            "pending_reviews": [],
            "panel": deepcopy(DEFAULT_PANEL),
            "modal": deepcopy(DEFAULT_MODAL),
            "response": deepcopy(DEFAULT_RESPONSE),
            "approval": deepcopy(DEFAULT_APPROVAL),
        }

    def _get_form_fields(self, guild_id: int) -> list[dict[str, Any]]:
        cfg = self._get_config(int(guild_id))
        return normalize_form_fields(cfg.get("modal") or {})

    @staticmethod
    def _get_form_fields_from_config(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        return normalize_form_fields((cfg or {}).get("modal") or {})

    async def _save_modal_fields(
        self,
        guild_id: int,
        fields: list[dict[str, Any]],
        *,
        title: str | None = None,
    ):
        cfg = self._get_config(int(guild_id))
        modal = dict(cfg.get("modal") or DEFAULT_MODAL)
        if title is not None:
            modal["title"] = str(title or DEFAULT_MODAL["title"]).strip() or DEFAULT_MODAL["title"]
        modal["fields"] = normalize_form_fields(fields)
        cfg["modal"] = sync_legacy_field_keys(modal)
        await self._save_config(int(guild_id), cfg)
        return cfg["modal"]["fields"]

    # ===== Permission helper =====

    def _is_staff(self, member: discord.Member) -> bool:
        return _is_staff_member(member)

    @staticmethod
    def _feature_active(cfg: dict[str, Any]) -> bool:
        return bool(
            (cfg or {}).get("enabled", False)
            and int((cfg or {}).get("form_channel_id") or 0)
            and int((cfg or {}).get("responses_channel_id") or 0)
        )

    # ===== Delete with fallback =====

    async def _delete_with_fallback(
        self,
        message: discord.Message,
        *,
        actor: discord.Member | None = None,
    ):
        """Apaga mensagem; em Forbidden, DM o actor; senão posta no canal.

        actor: usuário que vai receber a DM se a deleção falhar. Se None,
        cai pra message.author quando ele é Member.
        """
        try:
            await message.delete()
            return
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            pass

        if actor is None and isinstance(message.author, discord.Member):
            actor = message.author

        channel_mention = getattr(message.channel, "mention", "este canal")
        warning = (
            f"⚠️ Não consegui apagar uma mensagem em {channel_mention}. "
            f"Eu preciso da permissão `Gerenciar Mensagens` lá pra que o "
            f"sistema de formulário funcione direito."
        )

        if actor is not None:
            try:
                await actor.send(warning)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            await message.channel.send(warning, delete_after=30)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ===== on_message dispatch =====

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Filtros baratos antes de qualquer DB call
        if message.author.bot or message.guild is None or not message.content:
            return
        antibot_guard = getattr(self.bot, "antibot_should_block_message", None)
        if callable(antibot_guard) and bool(antibot_guard(message)):
            return
        if not isinstance(message.author, discord.Member):
            return

        content_norm = message.content.strip().lower()
        is_form_trigger = content_norm in TRIGGER_WORDS_FORM
        is_c_trigger = content_norm == TRIGGER_WORD_CUSTOMIZE
        if not (is_form_trigger or is_c_trigger):
            return

        if not self._is_staff(message.author):
            return  # ignora silenciosamente — não-staff não dispara

        guild_id = int(message.guild.id)
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)
        setup_complete = bool(form_ch_id and resp_ch_id)

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            try:
                if is_form_trigger:
                    if not setup_complete:
                        await self._start_setup_flow(message)
                    elif message.channel.id == form_ch_id:
                        await self._refresh_form_message(message)
                    # senão: trigger 'form' fora do canal configurado, ignora
                elif is_c_trigger:
                    if not setup_complete:
                        return  # 'c' sem setup não faz nada
                    if message.channel.id not in (form_ch_id, resp_ch_id):
                        return
                    await self._open_customization_panel(message)
            except Exception:
                log.exception(
                    "[forms] erro processando trigger gid=%s ch=%s msg=%s",
                    guild_id, message.channel.id, message.id,
                )

    # ===== Setup flow =====

    async def _start_setup_flow(self, message: discord.Message):
        """Posta o SetupView no canal onde o staff disparou. A palavra é apagada."""
        await self._delete_with_fallback(message, actor=message.author)

        view = SetupView(
            self,
            guild_id=int(message.guild.id),
            staff_id=int(message.author.id),
        )
        try:
            sent = await message.channel.send(view=view)
            view.message = sent
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar setup view gid=%s: %r", message.guild.id, e)

    async def _finalize_setup(
        self,
        interaction: discord.Interaction,
        *,
        setup_view: SetupView,
        form_channel_id: int,
        resp_channel_id: int,
    ):
        """Salva config + posta form no canal escolhido + atualiza msg de setup."""
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        cfg["enabled"] = True
        cfg["form_channel_id"] = int(form_channel_id)
        cfg["responses_channel_id"] = int(resp_channel_id)
        await self._save_config(guild_id, cfg)

        form_channel = self.bot.get_channel(int(form_channel_id))
        posted_ok = False
        if isinstance(form_channel, discord.TextChannel):
            posted_ok = await self._post_form_message(guild_id, form_channel)

        confirmation = self._build_setup_confirmation_view(
            form_channel_id, resp_channel_id, posted_ok
        )
        if setup_view.message is not None:
            try:
                await setup_view.message.edit(view=confirmation)
                return
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        # Fallback: msg original sumiu, manda como followup ephemeral
        try:
            await interaction.followup.send(view=confirmation, ephemeral=True)
        except discord.HTTPException:
            pass

    def _build_setup_confirmation_view(
        self,
        form_channel_id: int,
        resp_channel_id: int,
        posted_ok: bool,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        status_line = (
            f"✅ Form postado em <#{form_channel_id}>."
            if posted_ok
            else (
                f"⚠️ Configuração salva, mas falhei ao postar o form em "
                f"<#{form_channel_id}>. Digite `form` no canal para postar novamente."
            )
        )
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ✅ Configuração salva"),
                discord.ui.TextDisplay(
                    f"**Canal de formulário:** <#{form_channel_id}>\n"
                    f"**Canal de respostas:** <#{resp_channel_id}>"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(status_line),
                accent_color=discord.Color.green(),
            )
        )
        return view

    # ===== Form refresh (trigger 'form/formulário' no canal de form) =====

    async def _refresh_form_message(self, message: discord.Message):
        """Apaga palavra, apaga form antigo, posta novo."""
        guild_id = int(message.guild.id)
        cfg = self._get_config(guild_id)

        await self._delete_with_fallback(message, actor=message.author)

        old_mid = int(cfg.get("active_message_id") or 0)
        if old_mid:
            try:
                old_msg = await message.channel.fetch_message(old_mid)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await self._post_form_message(guild_id, message.channel)

    async def _refresh_published_form(self, guild_id: int) -> tuple[bool, str]:
        """Atualiza o painel persistente após mudanças vindas do dashboard."""
        cfg = self._get_config(int(guild_id))
        channel_id = int(cfg.get("form_channel_id") or 0)
        message_id = int(cfg.get("active_message_id") or 0)
        if not (channel_id and message_id):
            return False, "Nenhum formulário publicado para atualizar."
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False, "Canal do formulário não encontrado."
        try:
            message = await channel.fetch_message(message_id)
            view = FormView(self, int(guild_id))
            await message.edit(view=view)
            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception:
                pass
            self._registered_persistent_views.add((int(guild_id), message_id))
            return True, "Formulário publicado atualizado."
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[forms] falha ao atualizar formulário publicado gid=%s: %r", guild_id, exc)
            return False, "Não consegui atualizar o formulário publicado."

    async def _post_form_message(
        self,
        guild_id: int,
        channel: discord.abc.Messageable,
    ) -> bool:
        """Posta a FormView no canal e atualiza active_message_id no DB.

        Retorna True se posted com sucesso. Em caso de erro, loga e retorna
        False sem alterar DB (mantém o active_message_id antigo, que pode
        estar válido ainda).
        """
        try:
            view = FormView(self, guild_id)
            sent = await channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar form gid=%s: %r", guild_id, e)
            return False

        cfg = self._get_config(guild_id)
        cfg["active_message_id"] = int(sent.id)
        await self._save_config(guild_id, cfg)

        try:
            self.bot.add_view(view, message_id=int(sent.id))
            self._registered_persistent_views.add((guild_id, int(sent.id)))
        except Exception as e:
            log.warning("[forms] falha ao registrar view persistente gid=%s mid=%s: %r",
                        guild_id, sent.id, e)

        return True

    # ===== Pending review persistence =====

    @staticmethod
    def _normalize_pending_reviews(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in value:
            if not isinstance(raw, dict):
                continue
            message_id = int(raw.get("message_id") or 0)
            channel_id = int(raw.get("channel_id") or 0)
            applicant_id = int(raw.get("applicant_id") or 0)
            if not (message_id and channel_id and applicant_id) or message_id in seen:
                continue
            seen.add(message_id)
            field_values_raw = raw.get("field_values") or {}
            field_values = {str(k): str(v or "") for k, v in dict(field_values_raw).items()} if isinstance(field_values_raw, dict) else {}
            normalized.append({
                "message_id": message_id,
                "channel_id": channel_id,
                "applicant_id": applicant_id,
                "field_values": field_values,
                "status": "pending",
            })
        return normalized

    def _find_pending_review(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        cfg = self._get_config(int(guild_id))
        for entry in self._normalize_pending_reviews(cfg.get("pending_reviews")):
            if int(entry.get("message_id") or 0) == int(message_id or 0):
                return entry
        return None

    async def _save_pending_review(
        self,
        guild_id: int,
        *,
        channel_id: int,
        message_id: int,
        applicant_id: int,
        field_values: dict[str, str],
    ):
        cfg = self._get_config(int(guild_id))
        pending = [
            entry for entry in self._normalize_pending_reviews(cfg.get("pending_reviews"))
            if int(entry.get("message_id") or 0) != int(message_id)
        ]
        pending.append({
            "message_id": int(message_id),
            "channel_id": int(channel_id),
            "applicant_id": int(applicant_id),
            "field_values": {str(k): str(v or "") for k, v in dict(field_values or {}).items()},
            "status": "pending",
        })
        # Evita crescimento infinito caso um servidor receba muitas submissões
        # e a staff apague mensagens manualmente sem revisar. Mantém as mais novas.
        cfg["pending_reviews"] = pending[-250:]
        await self._save_config(int(guild_id), cfg)
        self._registered_review_views.add((int(guild_id), int(message_id)))

    async def _remove_pending_review(self, guild_id: int, message_id: int):
        cfg = self._get_config(int(guild_id))
        pending = [
            entry for entry in self._normalize_pending_reviews(cfg.get("pending_reviews"))
            if int(entry.get("message_id") or 0) != int(message_id or 0)
        ]
        cfg["pending_reviews"] = pending
        await self._save_config(int(guild_id), cfg)
        self._registered_review_views.discard((int(guild_id), int(message_id or 0)))

    # ===== Submission handling =====

    async def _handle_submit_click(self, interaction: discord.Interaction, guild_id: int):
        """Callback do botão do form: abre o modal de submissão."""
        if not self._feature_active(self._get_config(int(guild_id))):
            await self._safe_send_ephemeral(interaction, "A função de formulários está desativada neste servidor.")
            return
        try:
            await interaction.response.send_modal(FormSubmissionModal(self, guild_id))
        except discord.HTTPException as e:
            log.warning("[forms] falha ao abrir modal gid=%s: %r", guild_id, e)

    async def _handle_submission(
        self,
        interaction: discord.Interaction,
        *,
        field_values: dict[str, str] | None = None,
        age_pronoun: str | None = None,
        description: str | None = None,
    ):
        """Recebe o submit do FormSubmissionModal e posta no canal de respostas."""
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        if not self._feature_active(cfg):
            await self._safe_send_ephemeral(interaction, "A função de formulários está desativada neste servidor.")
            return
        resp_ch_id = int(cfg.get("responses_channel_id") or 0)

        fields = self._get_form_fields_from_config(cfg)
        if field_values is None:
            # Compatibilidade com chamadas antigas.
            field_values = {
                "field1": getattr(interaction.user, "display_name", str(interaction.user)),
                "field2": str(age_pronoun or "").strip(),
                "field3": str(description or "").strip(),
            }
        else:
            field_values = {str(k): str(v or "").strip() for k, v in dict(field_values or {}).items()}

        # Garante aliases field1/field2/... mesmo quando os IDs internos forem customizados.
        for index, field in enumerate(fields, start=1):
            field_id = str(field.get("id") or f"field{index}")
            value = field_values.get(field_id, field_values.get(f"field{index}", ""))
            field_values[field_id] = str(value or "").strip()
            field_values[f"field{index}"] = str(value or "").strip()

        if not resp_ch_id:
            await self._safe_send_ephemeral(
                interaction,
                "❌ Configuração inválida — peça pra um staff reconfigurar.",
            )
            return

        resp_channel = self.bot.get_channel(resp_ch_id)
        if not isinstance(resp_channel, discord.TextChannel):
            await self._safe_send_ephemeral(
                interaction,
                "❌ Canal de respostas indisponível — peça pra um staff reconfigurar.",
            )
            return

        response_view = self._build_response_view(
            guild_id, interaction.user, field_values
        )

        try:
            sent = await resp_channel.send(view=response_view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar resposta gid=%s: %r", guild_id, e)
            await self._safe_send_ephemeral(
                interaction,
                "❌ Não consegui postar no canal de respostas (talvez sem permissão). "
                "Avisa um staff.",
            )
            return

        if bool((cfg.get("approval") or {}).get("enabled", False)):
            await self._save_pending_review(
                guild_id,
                channel_id=int(resp_channel.id),
                message_id=int(sent.id),
                applicant_id=int(getattr(interaction.user, "id", 0) or 0),
                field_values=field_values,
            )

        await self._safe_send_ephemeral(interaction, "✅ Formulário enviado!")

    @staticmethod
    async def _safe_defer_ephemeral(interaction: discord.Interaction) -> bool:
        """Responde rápido à interação para evitar o alerta vermelho do Discord.

        Retorna True quando esta chamada conseguiu dar ACK agora. Se a interação
        já tinha resposta, retorna False. Erros de rede/interação expirada são
        engolidos para não quebrar a alteração que já foi salva.
        """
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
                return True
        except (discord.HTTPException, discord.InteractionResponded):
            pass
        return False

    @staticmethod
    async def _safe_send_ephemeral(interaction: discord.Interaction, content: str):
        """Manda mensagem ephemeral sem levantar se interaction já foi respondida."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except (discord.HTTPException, discord.InteractionResponded):
            pass

    def _build_response_view(
        self,
        guild_id: int,
        user,
        field_values: dict[str, str],
    ) -> discord.ui.LayoutView:
        """Constrói a mensagem Components V2 com campos separados."""
        return ResponseReviewView(
            self,
            guild_id=int(guild_id),
            applicant_id=int(getattr(user, "id", 0) or 0),
            field_values=field_values,
        )

    def _build_template_ctx(
        self,
        guild_id: int,
        user_id: int,
        field_values: dict[str, str] | None,
        *,
        sample: bool = False,
    ) -> dict[str, str]:
        """Contexto de placeholders para resposta/DMs."""
        guild = self.bot.get_guild(int(guild_id))
        member = guild.get_member(int(user_id)) if guild is not None and user_id else None
        user_mention = f"<@{int(user_id)}>" if user_id else "<@123456789>"
        display_name = getattr(member, "display_name", "Leonardo" if sample else str(user_id or "Usuário"))
        guild_name = getattr(guild, "name", "Servidor" if sample else "este servidor")
        values = dict(field_values or {})
        cfg = self._get_config(guild_id)
        fields = self._get_form_fields_from_config(cfg)
        ctx = {
            "user": user_mention,
            "membro": user_mention,
            "user_id": str(user_id or "123456789"),
            "user_name": display_name,
            "nome_usuario": display_name,
            "guild": guild_name,
            "servidor": guild_name,
        }
        samples = ["Leonardo", "17, ele/dele", "Não sei", "Exemplo", "Observação"]
        resolved: list[str] = []
        for index, field in enumerate(fields, start=1):
            value = get_field_value(values, field, index - 1)
            if not value and sample:
                value = samples[index - 1] if index - 1 < len(samples) else "Exemplo"
            field_key = str(field.get("id") or f"field{index}")
            ctx[field_key] = value
            ctx[f"field{index}"] = value
            ctx[f"campo{index}"] = value
            slug = slugify_placeholder(field.get("response_label") or field.get("label") or "")
            if slug and slug not in ctx:
                ctx[slug] = value
            resolved.append(value)

        field1 = resolved[0] if len(resolved) > 0 else ("Leonardo" if sample else "")
        field2 = resolved[1] if len(resolved) > 1 else ("17, ele/dele" if sample else "")
        field3 = resolved[2] if len(resolved) > 2 else ("Não sei" if sample else "")
        ctx.update({
            "nome": field1,
            "idade": field2,
            "pronome": field2,
            "idade_pronome": field2,
            "descricao": field3,
            "motivo": field3,  # alias antigo
        })
        return ctx

    @staticmethod
    def _safe_format(template: str, ctx: dict) -> str:
        """format_map com fallback: placeholders desconhecidos ficam intactos."""

        class _SafeDict(dict):
            def __missing__(self, key):
                return "{" + key + "}"

        try:
            return str(template or "").format_map(_SafeDict(ctx))
        except Exception:
            return str(template or "")

    # ===== Customization panel (trigger 'c') =====

    async def _open_customization_panel(self, message: discord.Message):
        """Apaga sessão `c` anterior e posta painel novo; a palavra atual fica até apagar."""
        guild_id = int(message.guild.id)

        # Limpeza global: independente de quem disparou a sessão anterior.
        # Isso apaga o `c` anterior e o painel anterior, mas não apaga o `c` atual.
        await self._purge_previous_c_session(guild_id)

        view = CustomizationPanelView(
            self, guild_id=guild_id, staff_id=int(message.author.id)
        )
        try:
            panel_msg = await message.channel.send(view=view)
            view.message = panel_msg
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[forms] falha ao postar customization panel gid=%s: %r", guild_id, e)
            return

        cfg = self._get_config(guild_id)
        cfg["active_c_trigger"] = {
            "channel_id": int(message.channel.id),
            "message_id": int(message.id),
        }
        cfg["active_c_panel"] = {
            "channel_id": int(message.channel.id),
            "message_id": int(panel_msg.id),
        }
        await self._save_config(guild_id, cfg)

    async def _purge_previous_c_session(self, guild_id: int):
        """Apaga mensagem trigger e painel da sessão 'c' atual + zera o DB."""
        cfg = self._get_config(guild_id)
        for key in ("active_c_trigger", "active_c_panel"):
            entry = cfg.get(key) or {}
            ch_id = int(entry.get("channel_id") or 0)
            msg_id = int(entry.get("message_id") or 0)
            if not (ch_id and msg_id):
                continue
            ch = self.bot.get_channel(ch_id)
            if ch is None:
                continue
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        cfg["active_c_trigger"] = {"channel_id": 0, "message_id": 0}
        cfg["active_c_panel"] = {"channel_id": 0, "message_id": 0}
        await self._save_config(guild_id, cfg)

    # ===== Update config (chamado pelos modais de edição) =====

    async def _update_panel_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        button_label: str,
        button_emoji: str = "",
        media_url: str = "",
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = cfg.get("panel") or {}
        cfg["panel"] = {
            "title": title,
            "description": description,
            "button_label": button_label,
            "button_emoji": button_emoji,
            "button_style": str(old.get("button_style") or DEFAULT_PANEL.get("button_style") or "primary"),
            "media_url": media_url,
            "accent_color": str(old.get("accent_color") or DEFAULT_PANEL.get("accent_color") or "#5865F2"),
        }
        await self._save_config(guild_id, cfg)
        await self._rerender_active_form(guild_id)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Painel atualizado.")

    async def _update_modal_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        field1_label: str,
        field1_placeholder: str,
        field2_label: str,
        field2_placeholder: str,
        field3_label: str,
        field3_placeholder: str,
    ):
        """Compatibilidade com o modal legado de 3 campos."""
        guild_id = int(interaction.guild_id or 0)
        fields = self._get_form_fields(guild_id)
        while len(fields) < 3:
            fields.append({
                "id": next_field_id(fields),
                "label": f"Campo {len(fields) + 1}",
                "placeholder": "",
                "response_label": f"Campo {len(fields) + 1}",
                "required": True,
                "long": False,
                "show_in_response": True,
                "enabled": True,
                "min_length": 0,
                "max_length": 120,
            })
        updates = [
            (field1_label, field1_placeholder),
            (field2_label, field2_placeholder),
            (field3_label, field3_placeholder),
        ]
        for index, (label, placeholder) in enumerate(updates):
            fields[index]["label"] = str(label or fields[index].get("label") or f"Campo {index + 1}")
            fields[index]["placeholder"] = str(placeholder or "")
            fields[index]["response_label"] = fields[index]["label"]
        await self._update_modal_title_and_fields(
            interaction,
            title=title,
            fields=fields,
            success_message="✅ Campos do modal atualizados.",
        )

    async def _update_modal_title_and_fields(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        fields: list[dict[str, Any]],
        success_message: str = "✅ Campos atualizados.",
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        await self._save_modal_fields(guild_id, fields, title=title)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, success_message)

    async def _upsert_form_field(
        self,
        interaction: discord.Interaction,
        *,
        index: int | None,
        field: dict[str, Any],
        staff_id: int,
    ) -> int:
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        fields = self._get_form_fields(guild_id)
        if index is None:
            if len(fields) >= MODAL_FIELD_LIMIT:
                return max(0, len(fields) - 1)
            field = dict(field)
            field["id"] = str(field.get("id") or next_field_id(fields))
            fields.append(field)
            selected_index = len(fields) - 1
        else:
            selected_index = max(0, min(int(index), len(fields) - 1))
            current = dict(fields[selected_index])
            current.update(field)
            current["id"] = str(current.get("id") or fields[selected_index].get("id") or f"field{selected_index + 1}")
            fields[selected_index] = current
        await self._save_modal_fields(guild_id, fields)
        await self._rerender_customization_panel(guild_id, int(staff_id or getattr(interaction.user, "id", 0) or 0))
        return selected_index

    async def _remove_form_field(
        self,
        interaction: discord.Interaction,
        *,
        index: int,
        staff_id: int,
    ) -> int:
        guild_id = int(interaction.guild_id or 0)
        fields = self._get_form_fields(guild_id)
        if len(fields) <= 1:
            return 0
        index = max(0, min(int(index), len(fields) - 1))
        fields.pop(index)
        await self._save_modal_fields(guild_id, fields)
        await self._rerender_customization_panel(guild_id, int(staff_id or getattr(interaction.user, "id", 0) or 0))
        return max(0, min(index, len(fields) - 1))

    async def _move_form_field(
        self,
        interaction: discord.Interaction,
        *,
        index: int,
        direction: int,
        staff_id: int,
    ) -> int:
        guild_id = int(interaction.guild_id or 0)
        fields = self._get_form_fields(guild_id)
        if not fields:
            return 0
        index = max(0, min(int(index), len(fields) - 1))
        new_index = max(0, min(index + int(direction), len(fields) - 1))
        if new_index == index:
            return index
        fields[index], fields[new_index] = fields[new_index], fields[index]
        await self._save_modal_fields(guild_id, fields)
        await self._rerender_customization_panel(guild_id, int(staff_id or getattr(interaction.user, "id", 0) or 0))
        return new_index

    async def _update_response_config(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        intro: str,
        footer: str,
        media_url: str,
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = dict(cfg.get("response") or DEFAULT_RESPONSE)
        cfg["response"] = {
            "title": title,
            "intro": intro,
            "footer": footer,
            "media_url": media_url,
            "accent_color": str(old.get("accent_color") or DEFAULT_RESPONSE.get("accent_color") or "#5865F2"),
        }
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Resposta atualizada.")

    async def _toggle_approval(self, interaction: discord.Interaction):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        approval["enabled"] = not bool(approval.get("enabled", False))
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0), interaction=interaction)

    async def _set_button_style(self, interaction: discord.Interaction, *, target: str, style_name: str):
        guild_id = int(interaction.guild_id or 0)
        style_name = str(style_name or "primary").strip().lower()
        if style_name not in {"primary", "secondary", "success", "danger"}:
            style_name = "primary"

        cfg = self._get_config(guild_id)
        target = str(target or "").strip().lower()
        if target == "panel":
            panel = dict(cfg.get("panel") or DEFAULT_PANEL)
            panel["button_style"] = style_name
            cfg["panel"] = panel
            await self._save_config(guild_id, cfg)
            await self._rerender_active_form(guild_id)
        elif target in {"approve", "reject"}:
            approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
            approval[f"{target}_style"] = style_name
            cfg["approval"] = approval
            await self._save_config(guild_id, cfg)
        else:
            await self._safe_send_ephemeral(interaction, "❌ Botão inválido.")
            return

        await self._rerender_customization_panel(
            guild_id,
            int(getattr(interaction.user, "id", 0) or 0),
            interaction=interaction,
        )

    async def _set_approval_role(self, interaction: discord.Interaction, role_id: int):
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        approval["role_id"] = int(role_id)
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0), interaction=interaction)

    async def _update_approval_options(
        self,
        interaction: discord.Interaction,
        *,
        enabled: bool,
        role_id: int,
        panel_style: str,
        approve_style: str,
        reject_style: str,
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)

        def clean_style(value: str, fallback: str) -> str:
            value = str(value or fallback).strip().lower()
            return value if value in {"primary", "secondary", "success", "danger"} else fallback

        cfg = self._get_config(guild_id)
        panel = dict(cfg.get("panel") or DEFAULT_PANEL)
        approval = dict(cfg.get("approval") or DEFAULT_APPROVAL)

        panel["button_style"] = clean_style(panel_style, "primary")
        approval["enabled"] = bool(enabled)
        approval["role_id"] = max(0, int(role_id or 0))
        approval["approve_style"] = clean_style(approve_style, "success")
        approval["reject_style"] = clean_style(reject_style, "danger")

        cfg["panel"] = panel
        cfg["approval"] = approval
        await self._save_config(guild_id, cfg)
        await self._rerender_active_form(guild_id)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Opções atualizadas.")



    async def _update_accent_colors(
        self,
        interaction: discord.Interaction,
        *,
        panel_accent_color: str,
        response_accent_color: str,
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)

        def clean_hex(value: str, fallback: str) -> str:
            raw = str(value or fallback).strip()
            if raw.startswith("#"):
                raw = raw[1:]
            elif raw.lower().startswith("0x"):
                raw = raw[2:]
            if len(raw) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in raw):
                return f"#{raw.upper()}"
            return fallback

        cfg = self._get_config(guild_id)
        panel = dict(cfg.get("panel") or DEFAULT_PANEL)
        response = dict(cfg.get("response") or DEFAULT_RESPONSE)

        panel["accent_color"] = clean_hex(panel_accent_color, str(DEFAULT_PANEL.get("accent_color") or "#5865F2"))
        response["accent_color"] = clean_hex(response_accent_color, str(DEFAULT_RESPONSE.get("accent_color") or "#5865F2"))

        cfg["panel"] = panel
        cfg["response"] = response
        await self._save_config(guild_id, cfg)
        await self._rerender_active_form(guild_id)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Cores dos cards atualizadas.")

    async def _update_approval_config(
        self,
        interaction: discord.Interaction,
        *,
        approve_label: str,
        approve_emoji: str,
        reject_label: str,
        reject_emoji: str,
        approve_dm: str,
        reject_dm: str,
    ):
        await self._safe_defer_ephemeral(interaction)
        guild_id = int(interaction.guild_id or 0)
        cfg = self._get_config(guild_id)
        old = dict(cfg.get("approval") or DEFAULT_APPROVAL)
        old.update({
            "approve_label": approve_label,
            "approve_emoji": approve_emoji,
            "reject_label": reject_label,
            "reject_emoji": reject_emoji,
            "approve_dm": approve_dm,
            "reject_dm": reject_dm,
        })
        cfg["approval"] = old
        await self._save_config(guild_id, cfg)
        await self._rerender_customization_panel(guild_id, int(getattr(interaction.user, "id", 0) or 0))
        await self._safe_send_ephemeral(interaction, "✅ Aprovação atualizada.")

    async def _rerender_customization_panel(
        self,
        guild_id: int,
        staff_id: int,
        *,
        interaction: discord.Interaction | None = None,
    ):
        """Atualiza o painel `c` ativo para refletir as configs atuais."""
        if interaction is not None and not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
        cfg = self._get_config(guild_id)
        entry = cfg.get("active_c_panel") or {}
        ch_id = int(entry.get("channel_id") or 0)
        msg_id = int(entry.get("message_id") or 0)
        if not (ch_id and msg_id):
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado.")
            return
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado.")
            return
        try:
            msg = await channel.fetch_message(msg_id)
            new_view = CustomizationPanelView(self, guild_id=guild_id, staff_id=staff_id)
            await msg.edit(view=new_view)
            new_view.message = msg
            if interaction is not None:
                try:
                    await interaction.followup.send("✅ Atualizado.", ephemeral=True)
                except discord.HTTPException:
                    pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            if interaction is not None:
                await self._safe_send_ephemeral(interaction, "✅ Atualizado, mas não consegui redesenhar o painel `c`.")

    async def _handle_review_action(self, interaction: discord.Interaction, view: ResponseReviewView, *, approved: bool):
        if not isinstance(interaction.user, discord.Member) or not self._is_staff(interaction.user):
            await self._safe_send_ephemeral(interaction, "❌ Só staff pode revisar formulários.")
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        guild_id = int(interaction.guild_id or view.guild_id)
        message_id = int(getattr(getattr(interaction, "message", None), "id", 0) or 0)
        pending_entry = self._find_pending_review(guild_id, message_id) if message_id else None
        if message_id and pending_entry is None and not view.status:
            await self._safe_send_ephemeral(
                interaction,
                "⚠️ Esta verificação não está mais pendente ou já foi finalizada.",
            )
            return

        # Depois de restart, a view é reconstruída do banco. Usa os dados salvos
        # como fonte da verdade para não depender de estado antigo em memória.
        if pending_entry is not None:
            view.applicant_id = int(pending_entry.get("applicant_id") or view.applicant_id)
            view.field_values = dict(pending_entry.get("field_values") or view.field_values)

        cfg = self._get_config(guild_id)
        approval = cfg.get("approval") or DEFAULT_APPROVAL
        applicant = None
        if interaction.guild is not None:
            applicant = interaction.guild.get_member(int(view.applicant_id))
            if applicant is None:
                try:
                    applicant = await interaction.guild.fetch_member(int(view.applicant_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    applicant = None

        role_warning = ""
        if approved:
            role_id = int(approval.get("role_id") or 0)
            if role_id and isinstance(applicant, discord.Member):
                role = interaction.guild.get_role(role_id) if interaction.guild is not None else None
                if role is None:
                    role_warning = "\n⚠️ Cargo configurado não encontrado."
                else:
                    try:
                        await applicant.add_roles(role, reason=f"Formulário aprovado por {interaction.user}")
                    except (discord.Forbidden, discord.HTTPException):
                        role_warning = "\n⚠️ Não consegui dar o cargo. Verifique minha hierarquia/permissões."

        dm_ok = await self._send_review_dm(
            guild_id,
            int(view.applicant_id),
            view.field_values,
            approved=approved,
        )
        dm_warning = "" if dm_ok else "\n⚠️ Não consegui enviar DM ao membro."

        new_status = "approved" if approved else "rejected"
        new_view = ResponseReviewView(
            self,
            guild_id=guild_id,
            applicant_id=int(view.applicant_id),
            field_values=view.field_values,
            status=new_status,
            reviewer_mention=getattr(interaction.user, "mention", ""),
        )
        edit_ok = False
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=new_view)
                edit_ok = True
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Se a mensagem foi atualizada, os botões sumiram e a submissão não
        # precisa mais ser reativada no próximo boot. Se a edição falhar,
        # mantemos no banco para pelo menos registrar o handler novamente.
        if message_id and edit_ok:
            await self._remove_pending_review(guild_id, message_id)

        label = "aprovado" if approved else "rejeitado"
        try:
            await interaction.followup.send(f"✅ Formulário {label}.{role_warning}{dm_warning}", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _send_review_dm(
        self,
        guild_id: int,
        applicant_id: int,
        field_values: dict[str, str],
        *,
        approved: bool,
    ) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        user = None
        if guild is not None:
            user = guild.get_member(int(applicant_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(applicant_id))
            except (discord.NotFound, discord.HTTPException):
                return False
        cfg = self._get_config(guild_id)
        approval = cfg.get("approval") or DEFAULT_APPROVAL
        tpl_raw = approval.get("approve_dm") if approved else approval.get("reject_dm")
        tpl = str(tpl_raw or "")
        if not tpl:
            tpl = DEFAULT_APPROVAL["approve_dm" if approved else "reject_dm"]
        ctx = self._build_template_ctx(guild_id, applicant_id, field_values)
        body = self._safe_format(tpl, ctx)
        title = "✅ Verificação aprovada" if approved else "❌ Verificação rejeitada"
        dm_view = discord.ui.LayoutView(timeout=None)
        dm_view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.TextDisplay(body),
                accent_color=discord.Color.green() if approved else discord.Color.red(),
            )
        )
        try:
            await user.send(view=dm_view)
            return True
        except (discord.Forbidden, discord.HTTPException):
            try:
                await user.send(body)
                return True
            except (discord.Forbidden, discord.HTTPException):
                return False

    async def _rerender_active_form(self, guild_id: int):
        """Edita a mensagem ativa do form pra refletir novos textos.

        Best-effort: se a mensagem foi apagada (NotFound) ou bot perdeu
        permissão (Forbidden), silenciosamente ignora — próximo trigger
        'form' vai criar uma nova com a config atualizada.
        """
        cfg = self._get_config(guild_id)
        form_ch_id = int(cfg.get("form_channel_id") or 0)
        active_mid = int(cfg.get("active_message_id") or 0)
        if not (form_ch_id and active_mid):
            return
        channel = self.bot.get_channel(form_ch_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(active_mid)
            new_view = FormView(self, guild_id)
            await msg.edit(view=new_view)
            try:
                self.bot.add_view(new_view, message_id=active_mid)
            except Exception:
                pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ===== Slash commands =====

    # Os antigos /form_* foram removidos. O fluxo atual usa os triggers
    # de mensagem `form` (publicar/repostar) e `c` (customizar).


async def setup(bot: commands.Bot):
    await bot.add_cog(FormsCog(bot))
