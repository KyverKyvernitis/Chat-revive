from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

import config
import discord
from discord import app_commands
from discord.ext import commands

from music_system import AudioRouter
from music_system.errors import MusicExtractionError
from music_system.models import ExtractedBatch, MusicTrack
from music_system.providers import describe_url
from music_system.ui import SearchResultView, QueueView, VoiceStatusSettingsView, build_queue_embed, build_now_playing_embeds
from music_system.worker_node import music_agent_command, music_agent_status, resolve_music_tracks_on_worker
from music_system.loading_reaction import MusicLoadingReaction

logger = logging.getLogger(__name__)

def _get_router(bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


def _agent_not_ready_transient(exc: Exception | str) -> bool:
    text = str(exc or "").strip().lower()
    return bool(text) and (
        "a música ainda não está pronta" in text
        or "musica ainda nao esta pronta" in text
        or "timed out" in text
        or "timeout" in text
        or "connect" in text
        or "connection" in text
        or "refused" in text
    )


class Music(commands.Cog):
    """Player de música modular integrado ao TTS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.router = _get_router(bot)

    async def cog_unload(self) -> None:
        with contextlib.suppress(Exception):
            await self.router.close()

    async def _voice_channel_from_ctx(self, ctx: commands.Context) -> discord.VoiceChannel | discord.StageChannel | None:
        voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if isinstance(voice, (discord.VoiceChannel, discord.StageChannel)):
            return voice
        return None

    async def _ensure_music_action_voice(self, ctx: commands.Context) -> bool:
        user_channel = await self._voice_channel_from_ctx(ctx)
        if user_channel is None:
            await self._reply(ctx, "Entre em um canal de voz primeiro.")
            return False
        if getattr(self.router, "music_worker_only_enabled", lambda: False)():
            selection = await self.router.ensure_music_worker_available()
            if not getattr(selection, "available", False):
                logger.info("[music/worker] ação bloqueada: %s", getattr(selection, "reason", "worker indisponível"))
                await self._reply(ctx, getattr(selection, "message", "") or getattr(self.router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online"))
                return False
        state = self.router.get_state(ctx.guild.id)
        vc = getattr(ctx.guild, "voice_client", None)
        bot_channel = getattr(vc, "channel", None) if vc is not None else None
        if bot_channel is None:
            player = getattr(state, "current_lavalink_player", None)
            bot_channel = getattr(player, "channel", None) if player is not None else None
        if bot_channel is not None and getattr(bot_channel, "id", None) != getattr(user_channel, "id", None):
            await self._reply(ctx, "Entre no mesmo canal de voz do bot para usar isso.")
            return False
        return True

    def _music_agent_default_enabled(self) -> bool:
        return bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)()

    def _schedule_music_agent_prefetch(
        self,
        guild_id: int,
        tracks: list[MusicTrack],
        *,
        voice_channel_id: int = 0,
        text_channel_id: int = 0,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> None:
        if not self._music_agent_default_enabled() or not bool(getattr(config, "MUSIC_AGENT_PREFETCH_ENABLED", True)):
            return
        try:
            limit = max(0, min(3, int(getattr(config, "MUSIC_AGENT_PREFETCH_TOP_RESULTS", 2) or 0)))
        except Exception:
            limit = 2
        if limit <= 0 or not tracks:
            return

        def serialize(track: MusicTrack) -> dict:
            return {
                "title": track.title,
                "webpage_url": track.webpage_url,
                "original_url": track.original_url,
                "stream_url": track.stream_url,
                "duration": track.duration,
                "uploader": track.uploader,
                "thumbnail": track.thumbnail,
                "source": track.source,
                "extractor": track.extractor,
                "requester_id": requester_id or track.requester_id,
                "requester_name": requester_name or track.requester_name,
            }

        payload_tracks = [serialize(track) for track in tracks[:limit]]

        async def runner() -> None:
            started = time.monotonic()
            try:
                result = await music_agent_command(
                    "prefetch",
                    guild_id=guild_id,
                    voice_channel_id=voice_channel_id,
                    text_channel_id=text_channel_id,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    tracks=payload_tracks,
                    limit=len(payload_tracks),
                    timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0),
                )
                logger.info(
                    "[music/timing] prefetch solicitado | guild=%s tracks=%s accepted=%s elapsed_ms=%.1f",
                    guild_id,
                    len(payload_tracks),
                    result.get("accepted") if isinstance(result, dict) else "?",
                    (time.monotonic() - started) * 1000.0,
                )
            except Exception as exc:
                logger.debug("[music/timing] prefetch ignorado | guild=%s erro=%s", guild_id, exc)

        asyncio.create_task(runner())


    def _music_agent_guild_state(self, payload: dict, guild_id: int) -> dict:
        guilds = payload.get("guilds") if isinstance(payload, dict) else {}
        if not isinstance(guilds, dict):
            return {}
        state = guilds.get(str(guild_id)) or guilds.get(guild_id)
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _music_agent_confirmed_playing(state: dict) -> bool:
        if str(state.get("status") or "").lower() != "playing":
            return False
        if "confirmed_playing" in state:
            return bool(state.get("confirmed_playing"))
        if "voice_connected" in state or "player_present" in state:
            return bool(state.get("voice_connected")) and bool(state.get("player_present"))
        return False

    def _music_agent_play_message(self, track: MusicTrack, result: dict | None = None) -> str:
        result = result or {}
        queued = bool(result.get("queued"))
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        current = state.get("current") if isinstance(state.get("current"), dict) else {}

        def useful(value: object) -> str:
            text = str(value or "").strip()
            lower = text.lower()
            if lower in {"youtube", "link", "música", "musica", "desconhecida", "unknown"}:
                return ""
            if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
                return ""
            if lower.startswith("youtube •") or lower.startswith("youtube -"):
                return ""
            return text

        # Quando o Music Agent responde queued=True, ``state.current`` continua
        # sendo a música que já estava tocando. A confirmação de "adicionada ao
        # queue" precisa usar a faixa selecionada/adicionada, não now-playing.
        queued_track = result.get("track") if isinstance(result.get("track"), dict) else {}
        queue_preview = state.get("queue") if isinstance(state.get("queue"), list) else []
        if queued and not queued_track and queue_preview:
            last = queue_preview[-1]
            queued_track = last if isinstance(last, dict) else {}
        source_payload = queued_track if queued else current
        title = (
            useful(source_payload.get("display_title"))
            or useful(source_payload.get("title"))
            or useful(source_payload.get("name"))
            or useful(getattr(track, "display_title", ""))
            or useful(track.short_title)
        )
        if len(title) > 90:
            title = title[:87].rstrip() + "..."
        duration_label = track.duration_label
        try:
            duration_source = source_payload if isinstance(source_payload, dict) else {}
            duration = duration_source.get("duration")
            if duration is None or str(duration).strip() == "":
                duration = getattr(track, "duration", None)
            if duration is not None and str(duration).strip() != "":
                total = max(0, int(float(duration)))
                minutes, seconds = divmod(total, 60)
                hours, minutes = divmod(minutes, 60)
                duration_label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        except Exception:
            pass
        status = str(state.get("status") or "").lower()
        if queued:
            if not title or title.lower() in {"youtube", "desconhecida", "música", "musica"}:
                return "`🎶` **Música adicionada ao queue.** Carregando detalhes..."
            return f"`🎶` **Adicionada ao queue:** {title} • `{duration_label}`"
        if self._music_agent_confirmed_playing(state):
            return f"`🎧` **Tocando:** {title} • `{duration_label}`"
        if status in {"failed", "error"}:
            error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:180]
            lower_error = error.lower()
            if any(token in lower_error for token in ("music agent", "configure music_agent", "sem token", "pynacl", "davey", "dependency", "unauthorized")):
                error = "backend musical ainda não está pronto"
            return f"`⚠️` Não consegui iniciar **{title}**: `{error}`"
        return f"`🎧` **Preparando para tocar:** {title} • `{duration_label}`"

    async def _sync_music_agent_panel(self, guild_id: int, track: MusicTrack, result: dict | None, *, voice_channel_id: int = 0, text_channel_id: int = 0, queued: bool = False) -> None:
        state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        syncer = getattr(self.router, "sync_music_agent_state", None)
        if callable(syncer):
            await syncer(
                guild_id,
                track,
                state,
                voice_channel_id=voice_channel_id,
                text_channel_id=text_channel_id,
                queued=queued,
                create_panel=True,
            )

    async def _watch_music_agent_message(self, message, guild_id: int, track: MusicTrack, *, voice_channel_id: int = 0, text_channel_id: int = 0, seconds: float | None = None, loading_reaction: MusicLoadingReaction | None = None) -> None:
        limit = float(seconds or getattr(config, "MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", 30.0) or 30.0)
        deadline = asyncio.get_running_loop().time() + max(5.0, limit)
        last_status = ""
        poll = max(0.4, min(1.5, float(getattr(config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.75) or 0.75)))
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll)
            try:
                payload = await music_agent_status(timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 3.5))
                state = self._music_agent_guild_state(payload, guild_id)
                await self._sync_music_agent_panel(guild_id, track, {"state": state}, voice_channel_id=voice_channel_id, text_channel_id=text_channel_id)
                status = str(state.get("status") or "").lower()
                if not status or status == last_status:
                    continue
                last_status = status
                if self._music_agent_confirmed_playing(state):
                    await message.edit(content=self._music_agent_play_message(track, {"state": state}))
                    if loading_reaction is not None:
                        await loading_reaction.finish()
                    return
                if status in {"failed", "error"}:
                    error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:220]
                    lower_error = error.lower()
                    if any(token in lower_error for token in ("music agent", "configure music_agent", "sem token", "pynacl", "davey", "dependency", "unauthorized")):
                        error = "backend musical ainda não está pronto"
                    await message.edit(content=f"`⚠️` Não consegui iniciar **{track.short_title}**: `{error}`")
                    if loading_reaction is not None:
                        await loading_reaction.finish()
                    return
                if status in {"idle", "stopped"} and not state.get("current"):
                    # Durante wake/playlist/resolve o worker pode piscar idle antes
                    # de publicar accepted/playing. Não envie erro público antes do
                    # timeout final; isso causava falso "worker não está pronto"
                    # enquanto a música começava logo depois.
                    continue
            except Exception:
                logger.debug("[music/agent] falha ao acompanhar estado remoto", exc_info=True)
        with contextlib.suppress(Exception):
            await message.edit(content=f"`⚠️` Demorei para confirmar o início de **{track.short_title}**. Tente novamente se não tocar.")
        if loading_reaction is not None:
            await loading_reaction.finish()

    async def _send_music_agent_control(self, ctx: commands.Context, action: str, success_message: str) -> bool:
        if not self._music_agent_default_enabled():
            return False
        if action in {"stop", "skip", "shuffle"}:
            with contextlib.suppress(Exception):
                self.router.cancel_pending_music_operations(ctx.guild.id, reason=f"agent_{action}")
        try:
            result = await music_agent_command(action, guild_id=ctx.guild.id, requester_id=ctx.author.id, requester_name=getattr(ctx.author, "display_name", str(ctx.author)))
        except Exception as exc:
            await self._reply(ctx, self._music_error_message(exc))
            return True
        state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        if state:
            with contextlib.suppress(Exception):
                await self.router.sync_music_agent_state(
                    ctx.guild.id,
                    None,
                    state,
                    voice_channel_id=state.get("voice_channel_id") or getattr(getattr(ctx.author, "voice", None), "channel", None) and getattr(getattr(ctx.author, "voice", None).channel, "id", 0),
                    text_channel_id=getattr(ctx.channel, "id", 0),
                    create_panel=True,
                )
        await self._reply(ctx, success_message)
        return True

    async def _reply(self, ctx: commands.Context, content: str | None = None, **kwargs):
        # Todas as mensagens novas da música são silenciosas por padrão para não notificar o servidor.
        kwargs.setdefault("silent", True)
        try:
            return await ctx.reply(content, mention_author=False, **kwargs)
        except Exception:
            return await ctx.send(content, **kwargs)

    def _music_error_message(self, exc: Exception) -> str:
        raw = str(exc or "")
        clean = raw.strip()
        worker_unavailable = getattr(self.router, "music_worker_unavailable_message", "")
        worker_engine_unavailable = getattr(getattr(self.router, "backends", None), "music_worker_engine_error_message", lambda _guild_id=None: "")(None)
        if clean and (clean in {worker_unavailable, worker_engine_unavailable} or clean.startswith("Sistema de música indisponível no momento:")):
            return clean
        lower = raw.lower()
        if any(
            needle in lower
            for needle in (
                "invalid status code for soundcloud stream",
                "failed to load tracks",
                "trackexception",
                "something broke when playing the track",
                "não conseguiu tocar nenhuma fonte candidata",
            )
        ):
            return "`⚠️` Não consegui tocar essa fonte agora. Tente outro link ou outra pesquisa."
        if getattr(self.router, "music_worker_only_enabled", lambda: False)() and (
            "lavalink indisponível" in lower
            or "lavalink em cooldown" in lower
            or "cannot connect to" in lower
            or "node musical" in lower
            or ("wavelink" in lower and "trackexception" not in lower)
        ):
            return worker_engine_unavailable or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
        if "sign in to confirm" in lower or "not a bot" in lower:
            return "`⚠️` O YouTube bloqueou a extração pedindo login/cookies. Confira `cookies.txt`, Deno e `yt-dlp[default]`."
        if "signature" in lower or "n challenge" in lower or "only images are available" in lower:
            return "`⚠️` O YouTube recusou o stream de áudio. Atualize `yt-dlp[default]` e confirme se o Deno está instalado."
        if "drm" in lower:
            return "`⚠️` Essa fonte usa DRM. Tente outro link ou pesquise pelo nome da música."
        if ("trackexception" in lower or "invalid status code" in lower or "stream: 404" in lower or "perdeu o track" in lower or "nenhuma fonte candidata" in lower):
            if "soundcloud" in lower or "scsearch" in lower or "404" in lower:
                return "`⚠️` A fonte encontrada não entregou um stream tocável. Tente novamente ou use um link direto."
            return "`⚠️` O worker encontrou a música, mas a fonte de áudio falhou ao iniciar. Tente outra busca ou link."
        if "failed to load tracks" in lower or "lavalinkloadexception" in lower or "something went wrong while looking up" in lower:
            if "spotify" in lower or "spsearch" in lower or "response code from channel info is 403" in lower:
                return "`⚠️` Não consegui resolver esse link do Spotify no node. Vou evitar erro cru: confira a API do Spotify no `.env` e use modo `auto` para cair no player local quando o LavaSrc falhar."
            if "soundcloud" in lower or "scsearch" in lower or "invalid status code" in lower or "404" in lower:
                return "`⚠️` O SoundCloud respondeu metadados, mas recusou o stream no node. Tente novamente; em modo `auto`, o bot tenta uma fonte local equivalente."
            return "`⚠️` O Lavalink não encontrou uma faixa tocável para essa busca. Tente pesquisar com nome e artista."
        if "timed out" in lower or "timeout" in lower:
            return "`⚠️` A fonte demorou demais para responder. Tente novamente em alguns segundos."
        if not raw:
            return "`⚠️` Não consegui iniciar essa música."
        return f"`⚠️` {raw[:220]}"


    def _is_lavalink_real_enabled(self, guild_id: int | None) -> bool:
        try:
            return bool(self.router.backends.should_use_lavalink_real(guild_id))
        except Exception:
            logger.debug("[music/lavalink] falha ao checar modo real", exc_info=True)
            return False

    def _query_profile(self, query: str):
        return describe_url((query or "").strip())

    def _is_youtube_link(self, query: str) -> bool:
        return bool(self._query_profile(query).is_youtube)

    def _is_youtube_text_search(self, query: str) -> bool:
        raw = (query or "").strip()
        if not raw:
            return False
        lower = raw.lower()
        if lower.startswith(("ytsearch:", "ytmsearch:")):
            return True
        if lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:")):
            return False
        profile = describe_url(raw)
        return not profile.is_url

    def _should_use_lavalink_for_input(self, query: str, guild_id: int | None) -> bool:
        if getattr(self.router, "music_worker_only_enabled", lambda: False)():
            # Worker-only é yt-dlp/Music Agent first. Spotify/álbuns/playlists são
            # convertidos para metadata + busca YouTube no worker, não para Lavalink.
            return False
        if not self._is_lavalink_real_enabled(guild_id):
            return False
        profile = self._query_profile(query)
        # YouTube direto nunca deve ir para Lavalink. O node fica reservado
        # para LavaSrc/SoundCloud; YouTube direto é player local yt-dlp.
        if profile.is_youtube:
            return False
        # Spotify/Deezer/Apple são links de metadata: primeiro o bot lê título,
        # artista e duração pela API. Só na hora do playback ele tenta espelhar
        # no LavaSrc/SoundCloud; assim não joga URL do Spotify crua no node nem
        # mostra erro inglês de `spsearch`/SpotifySourceManager no chat.
        if profile.is_metadata_only:
            return False
        # Pesquisa textual sempre mostra resultados do YouTube. A escolha do
        # usuário tenta espelho LavaSrc por autor+título exatos no playback e, se
        # não bater, cai para yt-dlp local.
        if self._is_youtube_text_search(query):
            return False
        return True

    def _lavalink_identifier_for_query(self, query: str) -> tuple[str, str, str, str]:
        """Cria uma faixa leve para o node resolver, sem chamar yt-dlp no comando.

        Em modo node de áudio, o node deve receber a URL/busca crua. Fazer
        ``_play`` passar antes pelo extractor local bloqueia o event loop
        (``voice heartbeat blocked``) e ainda pode trocar YouTube por SoundCloud.
        """
        raw = (query or "").strip()
        lower = raw.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        for prefix in known_prefixes:
            if lower.startswith(prefix):
                body = raw[len(prefix):].strip()
                identifier = f"{prefix}{body}" if body else raw
                source = prefix[:-1]
                return identifier, (body or raw), source, ""

        profile = describe_url(raw)
        if profile.is_url:
            source = profile.platform or "lavalink"
            title = "YouTube" if profile.is_youtube else f"{source.title()} link" if source != "lavalink" else "Link"
            return profile.canonical or raw, title, source, profile.canonical or raw

        # Busca textual normal em node de áudio: usa SoundCloud/LavaSrc primeiro.
        # YouTube direto continua local; texto sem prefixo ganha seleção via Lavalink.
        return f"scsearch:{raw}", raw, "scsearch", ""

    def _lavalink_batch_for_query(self, query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
        identifier, title, source, webpage_url = self._lavalink_identifier_for_query(query)
        track = MusicTrack(
            title=title or identifier or "Música",
            webpage_url=webpage_url,
            original_url=identifier,
            requester_id=requester_id,
            requester_name=requester_name,
            source=source or "lavalink",
            extractor="lavalink",
        )
        return ExtractedBatch(tracks=[track], query=identifier, is_playlist=False)

    def _worker_direct_url_batch(self, query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
        profile = self._query_profile(query)
        title = "YouTube" if profile.is_youtube else "Link"
        track = MusicTrack(
            title=title,
            webpage_url=profile.canonical or query,
            original_url=query,
            requester_id=requester_id,
            requester_name=requester_name or "",
            source="YouTube" if profile.is_youtube else (profile.platform or "link"),
            extractor="worker-ytdlp",
        )
        return ExtractedBatch(tracks=[track], query=query, is_playlist=False)

    def _worker_url_should_flatten_first(self, query: str) -> bool:
        profile = self._query_profile(query)
        if not profile.is_url:
            return False
        if profile.is_youtube and profile.resource_type == "playlist":
            return True
        if profile.resource_type in {"playlist", "album"}:
            return True
        return False

    def _music_agent_query_for_track(self, track: MusicTrack, fallback: str = "") -> str:
        extractor = str(getattr(track, "extractor", "") or "").lower()
        source = str(getattr(track, "source", "") or getattr(track, "display_source", "") or "").lower()
        is_metadata = extractor == "metadata" or any(token in source for token in ("spotify", "deezer", "apple", "metadata"))
        if is_metadata:
            base = str(getattr(track, "display_title", "") or getattr(track, "title", "") or fallback or "").strip()
            uploader = str(getattr(track, "display_uploader", "") or getattr(track, "uploader", "") or "").strip()
            if uploader and uploader.lower() not in base.lower():
                base = f"{uploader} {base}".strip()
            if base and "official" not in base.lower():
                base = f"{base} official audio"
            return base or fallback or getattr(track, "title", "") or ""
        return str(getattr(track, "webpage_url", "") or getattr(track, "original_url", "") or getattr(track, "stream_url", "") or getattr(track, "title", "") or fallback or "").strip()

    def _music_agent_tracks_payload(self, tracks: list[MusicTrack], *, requester_id: int = 0, requester_name: str = "") -> list[dict]:
        payload: list[dict] = []
        for track in tracks:
            payload.append({
                "title": track.title,
                "webpage_url": track.webpage_url,
                "original_url": track.original_url,
                "stream_url": track.stream_url,
                "duration": track.duration,
                "uploader": track.uploader,
                "thumbnail": track.thumbnail,
                "source": track.source,
                "extractor": track.extractor,
                "display_title": getattr(track, "display_title", ""),
                "display_uploader": getattr(track, "display_uploader", ""),
                "display_source": getattr(track, "display_source", ""),
                "query": self._music_agent_query_for_track(track),
                "requester_id": requester_id or track.requester_id,
                "requester_name": requester_name or track.requester_name,
            })
        return payload

    def _is_lavalink_search_request(self, query: str) -> bool:
        raw = (query or "").strip()
        if not raw:
            return False
        lower = raw.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        if lower.startswith(known_prefixes):
            return True
        return not describe_url(raw).is_url

    async def _lavalink_search_batch_for_query(
        self,
        ctx: commands.Context,
        query: str,
        *,
        requester_id: int,
        requester_name: str,
    ) -> ExtractedBatch:
        # Busca textual em node de áudio: consulta o node e preserva a lista
        # de resultados para o usuário escolher. Não usa yt-dlp nem escolhe
        # automaticamente o primeiro resultado.
        return await self.router.backends.search_lavalink_tracks(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=getattr(ctx.guild, "id", None),
            limit=max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5) or 5))),
        )

    async def _run_play(self, ctx: commands.Context, query: str) -> None:
        """Implementação compartilhada de `_play` e da alias roteada `_p <música>`."""
        query = (query or "").strip()
        command_started = time.monotonic()
        if not query:
            await self._reply(ctx, "Use `_play <link ou pesquisa>`.")
            return

        voice_channel = await self._voice_channel_from_ctx(ctx)
        if voice_channel is None:
            await self._reply(ctx, "Entre em um canal de voz primeiro.")
            return

        operation_generation = self.router.current_music_operation_generation(ctx.guild.id)
        loading_reaction = MusicLoadingReaction(getattr(ctx, "message", None))
        await loading_reaction.start()
        finish_loading_reaction = True
        try:
            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                worker_check_started = time.monotonic()
                selection = await self.router.ensure_music_worker_available()
                logger.info("[music/timing] worker checado | guild=%s elapsed_ms=%.1f available=%s", ctx.guild.id, (time.monotonic() - worker_check_started) * 1000.0, getattr(selection, "available", False))
                if not getattr(selection, "available", False):
                    logger.info("[music/worker] play bloqueado: %s", getattr(selection, "reason", "worker indisponível"))
                    await self._reply(ctx, getattr(selection, "message", "") or getattr(self.router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online"))
                    return

            input_profile = self._query_profile(query)

            # Shadow mode Lavalink: consulta o node em paralelo, mas mantém o áudio real
            # no player local atual. YouTube direto fica totalmente fora do LavaSrc/node
            # para não criar atraso nem mirror desnecessário.
            if not getattr(self.router, "music_worker_only_enabled", lambda: False)() and not input_profile.is_youtube:
                self.router.schedule_lavalink_shadow_search(
                    ctx.guild.id,
                    query,
                    requester_id=ctx.author.id,
                    requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
                    reason="play_command",
                )

            requester_name = getattr(ctx.author, "display_name", str(ctx.author))

            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                try:
                    youtube_text_search = self._is_youtube_text_search(query)
                    if input_profile.is_metadata_only:
                        # Spotify/Deezer/Apple são metadata-only: lemos título/artista
                        # rapidamente e o Music Agent resolve cada faixa via yt-dlp/YouTube
                        # no worker. Nunca envie URL crua dessas plataformas ao Lavalink.
                        resolve_started = time.monotonic()
                        batch = await self.router.extractor.extract(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                        logger.info("[music/worker] metadata-only resolvido sem lavalink | guild=%s source=%s tracks=%s elapsed_ms=%.1f", ctx.guild.id, input_profile.platform, len(batch.tracks), (time.monotonic() - resolve_started) * 1000.0)
                    elif self._worker_url_should_flatten_first(query):
                        resolve_started = time.monotonic()
                        batch = await resolve_music_tracks_on_worker(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 100) or 100)),
                            metadata_only=True,
                            allow_playlist=True,
                        )
                        logger.info("[music/worker] playlist/link flat resolvido no worker | guild=%s tracks=%s elapsed_ms=%.1f", ctx.guild.id, len(batch.tracks), (time.monotonic() - resolve_started) * 1000.0)
                    elif input_profile.is_url and not youtube_text_search:
                        # Link direto de faixa: o Music Agent resolve o stream no phone worker
                        # no momento do play usando o caminho rápido de URL direta.
                        batch = self._worker_direct_url_batch(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        resolve_started = time.monotonic()
                        batch = await resolve_music_tracks_on_worker(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            limit=(max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5) or 5))) if youtube_text_search else 1),
                            metadata_only=youtube_text_search,
                        )
                        logger.info("[music/worker] resolve etapa concluída | guild=%s metadata_only=%s elapsed_ms=%.1f", ctx.guild.id, youtube_text_search, (time.monotonic() - resolve_started) * 1000.0)
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music/worker] erro ao resolver música no worker")
                    await self._reply(ctx, self._music_error_message(exc))
                    return

            elif self._should_use_lavalink_for_input(query, ctx.guild.id):
                # LavaSrc/Lavalink fica responsável por SoundCloud/scsearch e por
                # links que o node resolve com segurança. Spotify cru passa antes
                # pela API do bot para não cair em erro bruto do SpotifySourceManager.
                try:
                    if self._is_lavalink_search_request(query):
                        try:
                            batch = await self._lavalink_search_batch_for_query(
                                ctx,
                                query,
                                requester_id=ctx.author.id,
                                requester_name=requester_name,
                            )
                        except Exception as lavalink_exc:
                            raw_lower = query.lower().strip()
                            explicit_lavalink = raw_lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:"))
                            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                                # Worker-only é uma fronteira dura: busca textual também
                                # fica no engine musical do worker. Nada de yt-dlp local na VPS.
                                raise lavalink_exc
                            if not self._is_youtube_text_search(query) or explicit_lavalink:
                                raise
                            logger.warning(
                                "[music/lavalink] busca textual no node falhou; fallback local yt-dlp | guild=%s query=%r erro=%s",
                                ctx.guild.id,
                                query,
                                lavalink_exc,
                            )
                            batch = await self.router.extractor.search_youtube(
                                query,
                                requester_id=ctx.author.id,
                                requester_name=requester_name,
                            )
                        if not batch.tracks and self._is_youtube_text_search(query):
                            raw_lower = query.lower().strip()
                            explicit_lavalink = raw_lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:"))
                            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                                batch = ExtractedBatch(tracks=[], query=query, is_playlist=False)
                            elif not explicit_lavalink:
                                logger.info(
                                    "[music/lavalink] busca textual no node vazia; fallback local yt-dlp | guild=%s query=%r",
                                    ctx.guild.id,
                                    query,
                                )
                                batch = await self.router.extractor.search_youtube(
                                    query,
                                    requester_id=ctx.author.id,
                                    requester_name=requester_name,
                                )
                    else:
                        batch = await self.router.backends.resolve_lavalink_direct_tracks(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            guild_id=getattr(ctx.guild, "id", None),
                            limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 25) or 25)),
                        )
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music/lavalink] erro ao buscar no node")
                    await self._reply(ctx, self._music_error_message(exc))
                    return
            else:
                # YouTube direto e pesquisa textual usam yt-dlp/local para metadata.
                # Links do YouTube tocam direto pelo local; pesquisas abrem seleção
                # e, na reprodução, tentam mirror LavaSrc antes do fallback local.
                try:
                    if self._is_youtube_text_search(query):
                        batch = await self.router.extractor.search_youtube(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        batch = await self.router.extractor.extract(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music] erro inesperado na extração")
                    await self._reply(ctx, self._music_error_message(exc))
                    return

            if self.router.current_music_operation_generation(ctx.guild.id) != operation_generation:
                logger.info("[music] play ignorado: operação antiga cancelada | guild=%s query=%r", ctx.guild.id, query)
                return

            if not batch.tracks:
                await self._reply(ctx, "`📭` Não encontrei nada tocável.")
                return

            should_open_selection = bool(
                (self._should_use_lavalink_for_input(query, ctx.guild.id) and self._is_lavalink_search_request(query))
                or (self._is_youtube_text_search(query) and len(batch.tracks) > 1)
                or (not self.router.extractor.looks_like_url(query) and len(batch.tracks) > 1)
            )
            if should_open_selection:
                logger.info("[music/timing] resultados prontos | guild=%s elapsed_ms=%.1f tracks=%s", ctx.guild.id, (time.monotonic() - command_started) * 1000.0, len(batch.tracks))
                embed = discord.Embed(
                    title="🔎 Escolha a música",
                    description="Selecione um dos resultados abaixo.",
                    color=discord.Color.blurple(),
                )
                for idx, track in enumerate(batch.tracks[:10], start=1):
                    embed.add_field(
                        name=f"{idx}. {track.short_title}",
                        value=f"{track.uploader or track.source or 'resultado'} • `{track.duration_label}`",
                        inline=False,
                    )
                await self._reply(
                    ctx,
                    embed=embed,
                    view=SearchResultView(self.router, ctx.guild.id, voice_channel.id, ctx.channel.id, batch.tracks[:10], ctx.author.id),
                )
                self._schedule_music_agent_prefetch(
                    ctx.guild.id,
                    batch.tracks[:10],
                    voice_channel_id=voice_channel.id,
                    text_channel_id=ctx.channel.id,
                    requester_id=ctx.author.id,
                    requester_name=requester_name,
                )
                return

            if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
                track = batch.tracks[0]
                is_multi = bool(len(batch.tracks) > 1)
                try:
                    agent_started = time.monotonic()
                    if is_multi:
                        result = await music_agent_command(
                            "enqueue_many",
                            guild_id=ctx.guild.id,
                            voice_channel_id=voice_channel.id,
                            text_channel_id=ctx.channel.id,
                            query=query,
                            track=track,
                            tracks=self._music_agent_tracks_payload(batch.tracks, requester_id=ctx.author.id, requester_name=requester_name),
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        result = await music_agent_command(
                            "play",
                            guild_id=ctx.guild.id,
                            voice_channel_id=voice_channel.id,
                            text_channel_id=ctx.channel.id,
                            query=self._music_agent_query_for_track(track, query),
                            track=track,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    logger.info("[music/agent] play etapa concluída | guild=%s elapsed_ms=%.1f queued=%s added=%s cancelled=%s", ctx.guild.id, (time.monotonic() - agent_started) * 1000.0, bool(result.get("queued")), result.get("added"), bool(result.get("cancelled")))
                    if isinstance(result, dict) and result.get("cancelled"):
                        logger.info("[music/agent] play cancelado antes de publicar resposta | guild=%s query=%r", ctx.guild.id, query)
                        return
                    if isinstance(result, dict) and result.get("ok") is False and result.get("error"):
                        await self._reply(ctx, self._music_error_message(Exception(str(result.get("error")))))
                        return
                except Exception as exc:
                    logger.warning("[music/agent] falha ao enviar play direto | guild=%s erro=%s", ctx.guild.id, exc)
                    if _agent_not_ready_transient(exc):
                        msg = await self._reply(ctx, self._music_agent_play_message(track, {"state": {"status": "preparing"}}))
                        if msg is not None:
                            finish_loading_reaction = False
                            asyncio.create_task(self._watch_music_agent_message(msg, ctx.guild.id, track, voice_channel_id=voice_channel.id, text_channel_id=ctx.channel.id, loading_reaction=loading_reaction))
                        return
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                await self._sync_music_agent_panel(
                    ctx.guild.id,
                    track,
                    result,
                    voice_channel_id=voice_channel.id,
                    text_channel_id=ctx.channel.id,
                    queued=bool(result.get("queued")),
                )
                if is_multi:
                    added = int(result.get("added") or len(batch.tracks))
                    title = (batch.playlist_title or "playlist").strip()
                    label = f" de **{discord.utils.escape_markdown(title[:80])}**" if title else ""
                    count_label = "música" if added == 1 else "músicas"
                    state_payload = result.get("state") if isinstance(result.get("state"), dict) else {}
                    try:
                        queue_total = int(state_payload.get("queue_size") or 0)
                    except Exception:
                        queue_total = 0
                    if bool(result.get("queued")):
                        total_line = f"\n`🎶` Queue agora: `{queue_total}` música(s)." if queue_total else ""
                        msg = await self._reply(
                            ctx,
                            f"`📑` **Playlist adicionada ao final do queue:** `{added}` {count_label}{label}.{total_line}",
                        )
                    else:
                        msg = await self._reply(
                            ctx,
                            f"`📑` **Playlist adicionada ao queue:** `{added}` {count_label}{label}.\n`🎧` Preparando a primeira faixa...",
                        )
                else:
                    msg = await self._reply(ctx, self._music_agent_play_message(track, result))
                state = result.get("state") if isinstance(result.get("state"), dict) else {}
                status = str(state.get("status") or "").lower()
                confirmed = self._music_agent_confirmed_playing(state)
                if msg is not None and not result.get("queued") and not confirmed and status not in {"failed", "error"}:
                    finish_loading_reaction = False
                    asyncio.create_task(self._watch_music_agent_message(msg, ctx.guild.id, track, voice_channel_id=voice_channel.id, text_channel_id=ctx.channel.id, loading_reaction=loading_reaction))
                logger.info("[music/timing] play enviado | guild=%s elapsed_ms=%.1f queued=%s confirmed=%s", ctx.guild.id, (time.monotonic() - command_started) * 1000.0, bool(result.get("queued")), confirmed)
                return

            state_before = self.router.get_state(ctx.guild.id)
            was_session_active = bool(
                state_before.current
                or state_before.queue_size() > 0
                or getattr(state_before, "current_status", "") in {"resolving", "starting", "playing", "paused", "queued"}
            )
            added, dropped = await self.router.enqueue(ctx.guild, voice_channel, ctx.channel, batch.tracks)
            if added <= 0:
                await self._reply(ctx, "`⚠️` Não adicionei nada: o queue está cheio ou essa música já está no queue/tocando.")
                return

            if batch.is_playlist:
                count_label = "música" if added == 1 else "músicas"
                playlist_title = (batch.playlist_title or "").strip()
                if playlist_title:
                    desc = f"`📑` **Playlist adicionada:** `{added}` {count_label} de **{playlist_title}**"
                else:
                    desc = f"`📑` **Adicionadas ao queue:** `{added}` {count_label}"
                if batch.truncated:
                    desc += f"\n`⚠️` Playlist limitada aos primeiros `{getattr(config, 'MUSIC_MAX_PLAYLIST_ITEMS', 100)}` itens para não pesar o bot."
                if dropped:
                    desc += f"\n`⚠️` `{dropped}` item(ns) não entraram porque já estavam no queue/tocando ou porque o queue está cheio."
                await self._reply(ctx, desc)
            else:
                track = batch.tracks[0]
                state = self.router.get_state(ctx.guild.id)
                position = state.queue_size() + (1 if state.current else 0)
                if was_session_active:
                    await self._reply(ctx, f"`🎶` **Adicionada ao queue:** {track.short_title} • `{track.duration_label}` • posição `{max(1, position)}`")
                else:
                    # O worker pode pegar a primeira música imediatamente após enqueue,
                    # fazendo ``state.current`` existir antes da resposta do comando. Isso
                    # não significa posição 2; ainda é a faixa que está iniciando agora.
                    await self._reply(ctx, f"`🎧` **Preparando para tocar:** {track.short_title} • `{track.duration_label}`")
        finally:
            if finish_loading_reaction:
                await loading_reaction.finish()

    @commands.command(name="play", aliases=["tocar", "music", "musica"])
    @commands.guild_only()
    @commands.cooldown(1, 3.0, commands.BucketType.user)
    async def play(self, ctx: commands.Context, *, query: str = ""):
        """Toca link ou pesquisa música por texto."""
        await self._run_play(ctx, query)

    @commands.command(name="pause", aliases=["pausar", "pa"])
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "pause", "`⏸️` Música pausada."):
            return
        ok = await self.router.pause(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música tocando para pausar.")

    @commands.command(name="resume", aliases=["retomar", "continuar", "r"])
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "resume", "`▶️` Música retomada."):
            return
        ok = await self.router.resume(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música pausada.")

    @commands.command(name="skip", aliases=["s", "pular"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "skip", "`⏭️` Pulando música."):
            return
        _ok, message = await self.router.request_skip(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="back", aliases=["b", "previous", "voltar", "anterior"])
    @commands.guild_only()
    async def back(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        ok = await self.router.previous(ctx.guild.id)
        await self._reply(ctx, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    @commands.command(name="stop", aliases=["st", "pararmusica", "musicstop"])
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "stop", "`⏹️` Player encerrado e desconectado."):
            return
        _ok, message = await self.router.request_stop(ctx.guild.id, ctx.author, disconnect=True)
        await self._reply(ctx, message)

    @commands.command(name="queue", aliases=["fila", "q"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        await self._reply(ctx, embed=build_queue_embed(state, 0), view=QueueView(self.router, ctx.guild.id, 0, owner_id=ctx.author.id))

    @commands.command(name="np", aliases=["now", "nowplaying", "tocando"])
    @commands.guild_only()
    async def now_playing(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if state.current is None and state.queue.empty():
            await self._reply(ctx, "Nada tocando agora.")
            return
        state.last_text_channel_id = ctx.channel.id
        await self.router.update_panel(ctx.guild.id, create=True)

    @commands.command(name="volume", aliases=["v", "vol"])
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, value: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if value is None:
            await self._reply(ctx, f"`🔊` Volume atual: `{int(round(state.volume * 100))}%`.")
            return
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode alterar o volume do player.")
            return
        volume = await self.router.set_volume(ctx.guild.id, value)
        await self._reply(ctx, f"`🔊` Volume da música ajustado para `{int(round(volume * 100))}%`.")

    @commands.command(name="shuffle", aliases=["sh", "embaralhar"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_shuffle(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="loop", aliases=["l", "repeat", "repetir"])
    @commands.guild_only()
    async def loop(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_loop(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="remove", aliases=["rm", "remover"])
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_remove <posição>`.")
            return
        removed = await self.router.remove_at(ctx.guild.id, position)
        if removed is None:
            await self._reply(ctx, "Essa posição não existe no queue.")
            return
        await self._reply(ctx, f"`🗑️` Removido do queue: **{removed.short_title}**.")

    @commands.command(name="move", aliases=["mv", "mover"])
    @commands.guild_only()
    async def move(self, ctx: commands.Context, from_pos: Optional[int] = None, to_pos: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if from_pos is None or to_pos is None:
            await self._reply(ctx, "Use `_move <posição atual> <nova posição>`.")
            return
        ok = await self.router.move(ctx.guild.id, from_pos, to_pos)
        await self._reply(ctx, "`↪️` Posição atualizada." if ok else "Não consegui mover: confira as posições do queue.")

    @commands.command(name="skipto", aliases=["goto", "jump", "jumpto", "tocarfila"])
    @commands.guild_only()
    async def skipto(self, ctx: commands.Context, position: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_skipto <posição>`.")
            return
        ok = await self.router.skip_to(ctx.guild.id, position)
        await self._reply(ctx, "`▶️` Tocando a posição escolhida." if ok else "Não encontrei essa posição no queue.")

    @commands.command(name="readd", aliases=["ra", "readicionar", "historicofila", "historicoqueue"])
    @commands.guild_only()
    async def readd(self, ctx: commands.Context):
        added = await self.router.readd_history(ctx.guild.id)
        await self._reply(ctx, f"`🎶` Readicionei `{added}` música(s) do histórico." if added else "O histórico está vazio.")

    @commands.command(name="history", aliases=["h", "historico", "played"])
    @commands.guild_only()
    async def history(self, ctx: commands.Context):
        history = self.router.history_snapshot(ctx.guild.id)
        if not history:
            await self._reply(ctx, "Histórico vazio.")
            return
        lines = []
        for idx, track in enumerate(reversed(history[-10:]), start=1):
            lines.append(f"`{idx:02d}.` **{discord.utils.escape_markdown(track.short_title)}** • `{track.duration_label}`")
        embed = discord.Embed(title="↩️ Histórico de músicas", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text="Use _readd para colocar o histórico de volta no queue.")
        await self._reply(ctx, embed=embed)

    @commands.command(name="clearqueue", aliases=["cq", "limparfila", "limparqueue", "clearq"])
    @commands.guild_only()
    async def clearqueue(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        await self.router.replace_queue(ctx.guild.id, [])
        await self._reply(ctx, "`🧹` Queue limpo.")



    def _format_backend_status(self, health, *, runtime: dict | None = None) -> str:
        icon = "🟢" if getattr(health, "available", False) else ("🟡" if getattr(health, "configured", False) else "🔴")
        mode = getattr(health, "mode", "worker") or "worker"
        message = getattr(health, "message", "") or ""
        lines = [
            f"{icon} **{getattr(health, 'name', 'backend')}**",
            f"• modo: `{mode}`",
        ]
        if message:
            lines.append(f"• detalhe: {discord.utils.escape_markdown(str(message)[:220])}")
        return "\n".join(lines)



    @commands.command(name="voicestatus", aliases=["voice_status", "vstatus", "statusvoz", "canalstatus", "setvoicestatus"])
    @commands.guild_only()
    async def voicestatus(self, ctx: commands.Context, action: str = "", *, value: str = ""):
        """Configura o status automático do canal de voz com Components V2."""
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode configurar o status do canal de voz.")
            return

        action_norm = (action or "").strip().lower()
        if action_norm in {"on", "ativar", "ligar", "enable", "enabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, True)
        elif action_norm in {"off", "desativar", "desligar", "disable", "disabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, False)
        elif action_norm in {"template", "modelo", "status", "tocando"}:
            if value.strip():
                await self.router.set_voice_status_template(ctx.guild.id, value)
        elif action_norm in {"idle", "parado", "vazio"}:
            idle = value.strip()
            if idle in {"-", "clear", "limpar", "reset", "vazio"}:
                idle = ""
            await self.router.set_voice_status_idle(ctx.guild.id, idle)
        elif action_norm in {"reset", "padrao", "padrão", "default"}:
            await self.router.reset_voice_status_settings(ctx.guild.id)
        elif action_norm and action_norm not in {"painel", "panel", "config", "configurar"}:
            await self._reply(ctx, "Use `_voicestatus` para abrir o painel, ou `_voicestatus template <modelo>` para alterar direto.")
            return

        await self._reply(ctx, view=VoiceStatusSettingsView(self.router, ctx.guild.id, owner_id=ctx.author.id))


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if getattr(getattr(message, "author", None), "bot", False) or message.guild is None:
            return
        antibot_guard = getattr(self.bot, "antibot_should_block_message", None)
        if callable(antibot_guard) and bool(antibot_guard(message)):
            return

        raw = str(getattr(message, "content", "") or "").strip()
        if not raw:
            return

        prefixes = []
        for value in (getattr(config, "BOT_PREFIX", "_"), getattr(config, "PREFIX", "_"), "_"):
            value = str(value or "_").strip() or "_"
            if value not in prefixes:
                prefixes.append(value)

        lowered = raw.lower()
        for prefix in prefixes:
            alias = f"{prefix}p"
            # `_p` sozinho é reservado para o painel do TTS. Música só assume `_p <busca/link>`.
            if lowered.startswith(alias.lower() + " "):
                query = raw[len(alias):].strip()
                if not query:
                    return
                ctx = await self.bot.get_context(message)
                await self._run_play(ctx, query)
                return


    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = getattr(member, "guild", None)
        bot_user = getattr(self.bot, "user", None)
        if guild is None or bot_user is None or int(getattr(member, "id", 0) or 0) != int(getattr(bot_user, "id", 0) or 0):
            return
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        if before_channel is not None and after_channel is None:
            await self.router.handle_bot_voice_disconnect(guild, before_channel, after_channel)
            return
        if before_channel is not None and after_channel is not None and getattr(before_channel, "id", None) != getattr(after_channel, "id", None):
            await self.router.handle_bot_voice_move(guild, before_channel, after_channel)

    @play.error
    async def play_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandOnCooldown):
            await self._reply(ctx, f"Espere `{error.retry_after:.1f}s` antes de usar `_play` de novo.")
            return
        raise error


async def setup(bot: commands.Bot):
    _get_router(bot)
    await bot.add_cog(Music(bot))
