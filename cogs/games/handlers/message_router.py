import asyncio
import logging
import os
import re

import discord

from config import MUTE_TOGGLE_WORD, TRIGGER_WORD

_ROB_TRIGGER_RE = re.compile(r"^\s*(?:roubar|rob)\s+<@!?\d+>\s*$", re.IGNORECASE)


class GincanaMessageRouterMixin:
    def _message_router_timeout_seconds(self) -> float:
        try:
            return max(1.0, float(os.getenv("GINCANA_MESSAGE_HANDLER_TIMEOUT_SECONDS", "8.0") or "8.0"))
        except Exception:
            return 8.0

    async def _safe_route_call(self, handler_name: str, message: discord.Message) -> bool:
        handler = getattr(self, handler_name, None)
        if handler is None:
            return False
        heavy_handlers = {
            "_handle_race_trigger",
            "_handle_buckshot_trigger",
            "_handle_corrida_trigger",
            "_handle_poker_trigger",
            "_handle_truco_trigger",
            "_handle_carta_trigger",
            "_handle_roleta_trigger",
        }
        try:
            if handler_name in heavy_handlers:
                return bool(await handler(message))
            return bool(await asyncio.wait_for(handler(message), timeout=self._message_router_timeout_seconds()))
        except asyncio.TimeoutError:
            logging.getLogger("gincana.router").warning(
                "handler de mensagem ignorado por timeout | handler=%s guild=%s channel=%s author=%s",
                handler_name,
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "channel", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
            )
            return False
        except Exception as e:
            logging.getLogger("gincana.router").warning("%s falhou: %r", handler_name, e)
            return False

    def _matches_exact_trigger(self, content: str | None, trigger: str) -> bool:
        if not trigger:
            return False
        return str(content or "").strip().casefold() == str(trigger).strip().casefold()

    async def _handle_text_profile_commands(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip().casefold()
        if not content or content.startswith("_"):
            return False
        if content not in {"ficha", "fichas", "rank", "leaderboard", "daily", "bonus", "login", "recarga", "recarrega", "extrato"}:
            return False
        if message.guild is None:
            return True
        if content in {"ficha", "fichas"}:
            await self._send_chip_profile(message.channel.send, message.author)
            return True
        if content == "extrato":
            await message.channel.send(view=self._make_chip_history_view(message.author, limit=10))
            return True
        if content in {"rank", "leaderboard"}:
            await self._send_chip_rank(
                message.channel.send,
                message.guild,
                message.author,
            )
            return True
        if content in {"recarga", "recarrega"}:
            used, new_balance, note = await self._try_use_chip_recharge(message.guild.id, message.author.id)
            await message.channel.send(view=self._make_chip_recharge_view(message.guild.id, message.author.id, used, new_balance, note))
            return True

        await message.channel.send(
            view=await self._claim_daily_view(message.guild.id, message.author.id)
        )
        return True

    async def _handle_rob_trigger(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip()
        if content.casefold().startswith("_"):
            return False
        if not _ROB_TRIGGER_RE.fullmatch(content):
            return False
        if message.guild is None:
            return True
        mentions = [member for member in getattr(message, "mentions", []) if isinstance(member, discord.Member)]
        if len(mentions) != 1:
            return False
        target = mentions[0]
        await self._run_robbery(message.channel, message.guild, message.author, target)
        return True

    async def _handle_call_control_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
            return False

        voice_channel = self._call_trigger_channel(message)
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            return False

        normalized_content = str(message.content or "").strip().casefold()
        trigger_match = bool(TRIGGER_WORD and normalized_content == TRIGGER_WORD.casefold())
        mute_match = bool(MUTE_TOGGLE_WORD and normalized_content == MUTE_TOGGLE_WORD.casefold())
        if not trigger_match and not mute_match:
            return False

        targets = self._resolve_targets(guild, voice_channel)
        if not targets:
            return True

        target_ids = {member.id for member in targets}
        author_is_target = message.author.id in target_ids
        author_is_focused_non_staff = self._is_focused_non_staff_member(message.author) if mute_match else False
        did_trigger_action = False

        if trigger_match:
            did_trigger_action = True
            trigger_voice_channel = voice_channel if isinstance(voice_channel, discord.VoiceChannel) else None

            if trigger_voice_channel is not None:
                try:
                    await self._play_disconnect_trigger_sfx(
                        guild,
                        trigger_voice_channel,
                        target_count=len(targets),
                    )
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="economia disconnect")
                    except Exception:
                        pass

        if mute_match:
            if author_is_focused_non_staff:
                return True
            did_trigger_action = True
            if author_is_target:
                return True

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        new_muted = not bool(target.voice.mute)
                        await target.edit(mute=new_muted, reason="economia toggle mute")
                    except Exception:
                        pass

            await self._refresh_targets_suffix_nicknames(guild, targets)

        if did_trigger_action:
            await self._react_success_temporarily(message)
        return did_trigger_action

    async def _handle_gincana_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not self._games_trigger_entry_allowed(message.guild, message.channel):
            return

        if await self._safe_route_call("_handle_payment_message", message):
            return

        if await self._safe_route_call("_handle_text_profile_commands", message):
            return

        if await self._safe_route_call("_handle_race_trigger", message):
            return

        if await self._safe_route_call("_handle_rob_trigger", message):
            return

        if await self._safe_route_call("_handle_mendigar_trigger", message):
            return

        if await self._safe_route_call("_handle_focus_trigger", message):
            return

        if await self._safe_route_call("_handle_rola_toggle_trigger", message):
            return

        if await self._safe_route_call("_handle_role_toggle_trigger", message):
            return

        if await self._safe_route_call("_handle_dj_toggle_trigger", message):
            return

        if await self._safe_route_call("_handle_buckshot_trigger", message):
            return

        if await self._safe_route_call("_handle_target_trigger", message):
            return

        if await self._safe_route_call("_handle_corrida_trigger", message):
            return

        if await self._safe_route_call("_handle_poker_trigger", message):
            return

        if await self._safe_route_call("_handle_truco_trigger", message):
            return

        if await self._safe_route_call("_handle_carta_trigger", message):
            return

        if await self._safe_route_call("_handle_roleta_trigger", message):
            return


        await self._safe_route_call("_handle_call_control_trigger", message)
