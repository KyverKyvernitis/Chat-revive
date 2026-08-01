import asyncio
import re
import time

import discord

from ..constants import CHIPS_INITIAL


class GincanaPaymentMixin:
    _PAY_CONFIRM_EMOJI = "✅"
    _PAY_TIMEOUT_SECONDS = 300.0

    def _parse_pay_request(self, message: discord.Message):
        raw_content = str(message.content or "").strip()
        if not raw_content.casefold().startswith("pay"):
            return None, None
        mentions = [m for m in getattr(message, "mentions", []) if not getattr(m, "bot", False)]
        if len(mentions) != 1:
            return None, None
        target = mentions[0]

        remainder = re.sub(r"^\s*pay\b", "", raw_content, flags=re.IGNORECASE).strip()
        remainder = re.sub(rf"<@!?{target.id}>", "", remainder).strip()
        amount_match = re.search(r"\b(\d+)\b", remainder)
        inline_amount = None
        if amount_match:
            try:
                inline_amount = int(amount_match.group(1))
            except Exception:
                inline_amount = None
        return target, inline_amount

    def _build_payment_confirm_embed(self, guild: discord.Guild, payer_id: int, session: dict) -> discord.Embed:
        payer = guild.get_member(payer_id)
        target = guild.get_member(int(session.get("target_id") or 0))
        gross_amount = int(session.get("amount") or 0)
        fee = int(session.get("fee") or 0)
        net_amount = int(session.get("net_amount") or 0)
        payer_ok = " ✅" if session.get("payer_confirmed") else ""
        target_ok = " ✅" if session.get("target_confirmed") else ""

        payer_text = (payer.mention if payer else "Pagador") + payer_ok
        target_text = (target.mention if target else "Destinatário") + target_ok
        desc = (
            f"{payer_text} → {target_text}\n\n"
            f"Transferência de: {self._chip_text(gross_amount, kind='balance')}\n"
            f"Imposto: {self._chip_text(fee, kind='loss')}\n"
            f"{target.mention if target else 'Recebe'} recebe: {self._chip_text(net_amount, kind='gain')}"
        )
        embed = discord.Embed(title="💸 Confirmar pagamento", description=desc, color=discord.Color.blurple())
        embed.set_footer(text="Pagador e destinatário precisam confirmar com ✅")
        return embed

    async def _send_pay_usage(self, message: discord.Message) -> bool:
        await message.channel.send(
            embed=self._make_embed(
                "💸 Pagamento",
                "Use **pay @usuário valor**",
                ok=False,
            )
        )
        return True

    async def _start_payment_confirmation(self, message: discord.Message, *, target: discord.Member, amount: int) -> bool:
        guild = message.guild
        if guild is None:
            return True
        if amount <= 0:
            await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor precisa ser maior que zero", ok=False))
            return True
        fee = max(1, int(round(amount * 0.02)))
        net_amount = amount - fee
        if net_amount <= 0:
            await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor é muito baixo para essa transferência", ok=False))
            return True

        total = amount
        payer_chips = self.db.get_user_chips(guild.id, message.author.id, default=CHIPS_INITIAL)
        if not self._user_has_played_any_game(guild.id, message.author.id):
            await message.channel.send(
                embed=self._make_embed(
                    "💸 Pagamento bloqueado",
                    "Você precisa participar de pelo menos **1 jogo** antes de enviar pagamentos",
                    ok=False,
                )
            )
            self._payment_sessions.pop((guild.id, message.author.id), None)
            return True
        if not self._user_has_played_any_game(guild.id, target.id):
            await message.channel.send(
                embed=self._make_embed(
                    "💸 Pagamento bloqueado",
                    f"{target.mention} precisa participar de pelo menos **1 jogo** antes de receber pagamentos",
                    ok=False,
                )
            )
            self._payment_sessions.pop((guild.id, message.author.id), None)
            return True
        if payer_chips < total:
            await message.channel.send(embed=self._make_embed("💸 Saldo insuficiente", "Esse pagamento usa só fichas normais\nFichas bônus não entram no pay", ok=False))
            self._payment_sessions.pop((guild.id, message.author.id), None)
            return True

        session_key = (guild.id, message.author.id)
        previous = self._payment_sessions.pop(session_key, None)
        if previous is not None:
            await self._cleanup_payment_message(previous, clear_reactions=True)

        pending = {
            "target_id": target.id,
            "amount": amount,
            "fee": fee,
            "net_amount": net_amount,
            "total": total,
            "state": "awaiting_both_confirm",
            "payer_confirmed": False,
            "target_confirmed": False,
            "channel_id": message.channel.id,
            "created_at": time.time(),
            "expires_at": time.time() + self._PAY_TIMEOUT_SECONDS,
        }
        confirm_message = await message.channel.send(embed=self._build_payment_confirm_embed(guild, message.author.id, pending))
        pending["confirm_message"] = confirm_message
        pending["confirm_message_id"] = confirm_message.id
        self._payment_sessions[session_key] = pending

        try:
            await confirm_message.add_reaction(self._PAY_CONFIRM_EMOJI)
        except Exception:
            pass

        async def _expire_later(key: tuple[int, int], expected_message_id: int):
            await asyncio.sleep(self._PAY_TIMEOUT_SECONDS)
            session = self._payment_sessions.get(key)
            if not session:
                return
            if int(session.get("confirm_message_id") or 0) != int(expected_message_id):
                return
            await self._expire_payment_session(key)

        asyncio.create_task(_expire_later(session_key, confirm_message.id))
        return True

    async def _handle_payment_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or message.author.bot:
            return False
        target, inline_amount = self._parse_pay_request(message)
        if target is not None:
            if target.id == message.author.id:
                await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Você não pode enviar fichas para si mesmo", ok=False))
                return True
            if target.bot:
                await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Bots não podem receber fichas", ok=False))
                return True
            if inline_amount is None:
                return await self._send_pay_usage(message)
            return await self._start_payment_confirmation(message, target=target, amount=inline_amount)

        return False

    async def _handle_payment_reaction_event(self, payload: discord.RawReactionActionEvent, *, added: bool):
        if str(getattr(payload.emoji, "name", "")) != self._PAY_CONFIRM_EMOJI:
            return
        if payload.guild_id is None or payload.user_id is None:
            return
        if getattr(self.bot, "user", None) is not None and payload.user_id == self.bot.user.id:
            return

        session_key, session = self._find_payment_session_by_message_id(payload.message_id)
        if not session_key or not session:
            return
        if int(session_key[0]) != int(payload.guild_id):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        payer_id = int(session_key[1])
        target_id = int(session.get("target_id") or 0)
        if payload.user_id not in {payer_id, target_id}:
            return
        if session.get("state") != "awaiting_both_confirm":
            return

        if payload.user_id == payer_id:
            session["payer_confirmed"] = bool(added)
        elif payload.user_id == target_id:
            session["target_confirmed"] = bool(added)

        await self._refresh_payment_confirm_message(guild, payer_id, session)
        if session.get("payer_confirmed") and session.get("target_confirmed"):
            await self._finalize_payment(session_key, guild)

    def _find_payment_session_by_message_id(self, message_id: int):
        for key, session in list(self._payment_sessions.items()):
            if int(session.get("confirm_message_id") or 0) == int(message_id):
                return key, session
        return None, None

    async def _refresh_payment_confirm_message(self, guild: discord.Guild, payer_id: int, session: dict):
        message = session.get("confirm_message")
        if message is None:
            return
        try:
            await message.edit(embed=self._build_payment_confirm_embed(guild, payer_id, session))
        except Exception:
            pass

    async def _cleanup_payment_message(self, session: dict, *, clear_reactions: bool = False):
        message = session.get("confirm_message")
        if message is None:
            return
        if clear_reactions:
            try:
                await message.clear_reactions()
            except Exception:
                pass

    async def _finalize_payment(self, session_key: tuple[int, int], guild: discord.Guild):
        session = self._payment_sessions.get(session_key)
        if not session:
            return
        payer_id = int(session_key[1])
        target_id = int(session.get("target_id") or 0)
        payer = guild.get_member(payer_id)
        target = guild.get_member(target_id)
        if payer is None or target is None:
            await self._expire_payment_session(session_key, reason="Pagamento cancelado porque um dos usuários não está mais disponível")
            return

        amount = int(session.get("amount") or 0)
        fee = int(session.get("fee") or max(1, int(round(amount * 0.02))))
        net_amount = int(session.get("net_amount") or max(0, amount - fee))
        total = int(session.get("total") or amount)
        payer_chips = self.db.get_user_chips(guild.id, payer.id, default=CHIPS_INITIAL)
        if not self._user_has_played_any_game(guild.id, payer.id):
            await self._expire_payment_session(session_key, title="💸 Pagamento bloqueado", reason=f"{payer.mention} precisa participar de pelo menos **1 jogo** antes de enviar pagamentos")
            return
        if not self._user_has_played_any_game(guild.id, target.id):
            await self._expire_payment_session(session_key, title="💸 Pagamento bloqueado", reason=f"{target.mention} precisa participar de pelo menos **1 jogo** antes de receber pagamentos")
            return
        if payer_chips < total:
            await self._expire_payment_session(session_key, title="💸 Saldo insuficiente", reason="Esse pagamento usa só fichas normais\nFichas bônus não entram no pay")
            return

        await self._transfer_user_chips(guild.id, payer.id, target.id, total=total, net_amount=net_amount)
        await self.db.add_user_game_stat(guild.id, payer.id, "payments_sent", 1)
        await self.db.add_user_game_stat(guild.id, target.id, "payments_received", 1)
        await self.db.add_user_game_stat(guild.id, payer.id, "chips_sent_total", total)
        await self.db.add_user_game_stat(guild.id, target.id, "chips_received_total", net_amount)

        self._payment_sessions.pop(session_key, None)
        message = session.get("confirm_message")
        if message is not None:
            try:
                embed = discord.Embed(
                    title="✅ Pagamento concluído",
                    description=(
                        f"{payer.mention} enviou {self._chip_text(amount, kind='balance')} para {target.mention}\n"
                        f"Imposto: {self._chip_text(fee, kind='loss')}\n"
                        f"{target.mention} recebeu {self._chip_text(net_amount, kind='gain')}"
                    ),
                    color=discord.Color.green(),
                )
                await message.edit(embed=embed)
            except Exception:
                pass
            try:
                await message.clear_reactions()
            except Exception:
                pass

    async def _expire_payment_session(self, session_key: tuple[int, int], *, title: str = "💸 Pagamento expirado", reason: str = "O pagamento não foi confirmado a tempo"):
        session = self._payment_sessions.pop(session_key, None)
        if not session:
            return
        message = session.get("confirm_message")
        if message is not None:
            try:
                await message.edit(embed=self._make_embed(title, reason, ok=False))
            except Exception:
                pass
            try:
                await message.clear_reactions()
            except Exception:
                pass
