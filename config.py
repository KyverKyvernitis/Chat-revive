import os


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_guild_ids(value: str) -> list[int]:
    if not value:
        return []

    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            pass
    return result


TOKEN = (os.getenv("DISCORD_TOKEN", "") or "").strip()

TARGET_ROLE_ID = _parse_int(os.getenv("ROLE_ID", "0"), 0)
DISABLE_TIME = _parse_int(os.getenv("DISABLE_TIME", "14400"), 14400)

TRIGGER_WORD = (os.getenv("TRIGGER_WORD", "pinto") or "pinto").lower().strip()
MUTE_TOGGLE_WORD = (os.getenv("MUTE_TOGGLE_WORD", "rola") or "rola").lower().strip()

TARGET_USER_ID = _parse_int(os.getenv("TARGET_USER_ID", "0"), 0)

TTS_ENABLED = _parse_bool(os.getenv("TTS_ENABLED", "true"), True)
BLOCK_VOICE_BOT_ID = _parse_int(os.getenv("BLOCK_VOICE_BOT_ID", "0"), 0)
ONLY_TTS_USER_ID = _parse_int(os.getenv("ONLY_TTS_USER_ID", "0"), 0)

# Usuário que recebe DM quando a primeira conexão de voz de uma guild falha no boot.
# Se vazio, o bot tenta usar o dono do aplicativo Discord automaticamente.
TTS_VOICE_FAILURE_DM_USER_ID = _parse_int(
    os.getenv("TTS_VOICE_FAILURE_DM_USER_ID", os.getenv("BOT_OWNER_ID", os.getenv("OWNER_ID", "0"))),
    0,
)

# Reentra automaticamente na última call lembrada depois de restart/queda.
# Desative apenas se quiser impedir qualquer restore automático de voz.
TTS_VOICE_AUTO_RESTORE_ENABLED = _parse_bool(os.getenv("TTS_VOICE_AUTO_RESTORE_ENABLED", "true"), True)

PORT = _parse_int(os.getenv("PORT", "10000"), 10000)

MONGODB_URI = (os.getenv("MONGODB_URI", "") or "").strip()
MONGODB_DB = (os.getenv("MONGODB_DB", "chat_revive") or "chat_revive").strip()
MONGODB_COLLECTION = (os.getenv("MONGODB_COLLECTION", "settings") or "settings").strip()

GUILD_IDS = _parse_guild_ids(os.getenv("GUILD_IDS", ""))

# -----------------------------------------------------------------------------
# Event loop / logging — defaults seguros para VPS pequena
# -----------------------------------------------------------------------------
BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", "1.0"), 1.0)
BOT_EVENT_LOOP_LAG_WARNING_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_WARNING_SECONDS", "1.5"), 1.5)
BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS", "30.0"), 30.0)
BOT_EVENT_LOOP_LAG_SEVERE_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_SEVERE_SECONDS", "8.0"), 8.0)
BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS = _parse_float(os.getenv("BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS", "90.0"), 90.0)

ON_COLOR = 0x57F287
OFF_COLOR = 0xED4245

GTTS_DEFAULT_LANGUAGE = os.getenv("GTTS_DEFAULT_LANGUAGE", "pt-br")

TTS_OPUS_PLAYBACK_ENABLED = _parse_bool(os.getenv("TTS_OPUS_PLAYBACK_ENABLED", "true"), True)
TTS_OPUS_PLAYBACK_COPY_CODEC = _parse_bool(os.getenv("TTS_OPUS_PLAYBACK_COPY_CODEC", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_GCLOUD = False
WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB = max(1, _parse_int(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB", "8"), 8))

# TTS tuning recomendado
# Mantém a call "quente" por mais tempo após falar, reduzindo reconexões
TTS_WARM_HOLD_SECONDS = _parse_int(os.getenv("TTS_WARM_HOLD_SECONDS", "30"), 30)

# Tempo máximo de inatividade antes de avaliar desconexão
TTS_IDLE_DISCONNECT_SECONDS = _parse_int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "240"), 240)

# Cache curto de áudio para textos repetidos
TTS_AUDIO_CACHE_SIZE = _parse_int(os.getenv("TTS_AUDIO_CACHE_SIZE", "256"), 256)

# Tempo de vida do cache de áudio
TTS_AUDIO_CACHE_TTL_SECONDS = _parse_int(os.getenv("TTS_AUDIO_CACHE_TTL_SECONDS", "1800"), 1800)

# Tamanho máximo de texto para considerar cacheável
TTS_CACHEABLE_TEXT_MAX_LENGTH = _parse_int(os.getenv("TTS_CACHEABLE_TEXT_MAX_LENGTH", "320"), 320)

# Intervalo mínimo entre podas completas da pasta tmp_audio
TTS_TEMP_PRUNE_INTERVAL_SECONDS = _parse_int(os.getenv("TTS_TEMP_PRUNE_INTERVAL_SECONDS", "20"), 20)

# Warmup conservador no boot do cog TTS
TTS_BOOT_WARMUP_ENABLED = _parse_bool(os.getenv("TTS_BOOT_WARMUP_ENABLED", "true"), True)

# Alertas informativos de engine (sem cooldown/fallback automático da engine)
TTS_ENGINE_ALERT_COOLDOWN_SECONDS = _parse_int(os.getenv("TTS_ENGINE_ALERT_COOLDOWN_SECONDS", "900"), 900)
TTS_ENGINE_FAILURE_ALERT_THRESHOLD = _parse_int(os.getenv("TTS_ENGINE_FAILURE_ALERT_THRESHOLD", "3"), 3)
TTS_ENGINE_SLOW_WARN_SECONDS = _parse_float(os.getenv("TTS_ENGINE_SLOW_WARN_SECONDS", "8.0"), 8.0)

# Pasta raiz para todos os arquivos temporários do TTS
TTS_TEMP_DIR = (os.getenv("TTS_TEMP_DIR", os.path.join(BASE_DIR, "tmp_audio")) or os.path.join(BASE_DIR, "tmp_audio")).strip()

# Limites globais da pasta tmp_audio
TTS_TEMP_MAX_MB = _parse_int(os.getenv("TTS_TEMP_MAX_MB", "256"), 256)
TTS_TEMP_MAX_FILES = _parse_int(os.getenv("TTS_TEMP_MAX_FILES", "256"), 256)

# Logs detalhados de debug do TTS
TTS_DEBUG_LOGS = _parse_bool(os.getenv("TTS_DEBUG_LOGS", "false"), False)

# Máximo de itens por fila de guild
TTS_QUEUE_MAXSIZE = _parse_int(os.getenv("TTS_QUEUE_MAXSIZE", "20"), 20)

# Quantas sínteses de áudio podem acontecer ao mesmo tempo no processo
TTS_SYNTH_CONCURRENCY = _parse_int(os.getenv("TTS_SYNTH_CONCURRENCY", "3"), 3)

# Tempo máximo de espera da síntese Edge antes de fallback
TTS_EDGE_TIMEOUT_SECONDS = _parse_int(os.getenv("TTS_EDGE_TIMEOUT_SECONDS", "10"), 10)

# Concorrência máxima do gTTS
TTS_GTTS_CONCURRENCY = _parse_int(os.getenv("TTS_GTTS_CONCURRENCY", "1"), 1)

# FFmpeg enxuto para reprodução
TTS_FFMPEG_BEFORE_OPTIONS = (os.getenv("TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin") or "-nostdin").strip()
TTS_FFMPEG_OPTIONS = (os.getenv("TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()

# -----------------------------------------------------------------------------
# Música — player leve integrado ao TTS
# -----------------------------------------------------------------------------
MUSIC_DEFAULT_VOLUME = _parse_float(os.getenv("MUSIC_DEFAULT_VOLUME", "0.55"), 0.55)
MUSIC_TTS_VOLUME = _parse_float(os.getenv("MUSIC_TTS_VOLUME", "1.0"), 1.0)
# Quando TTS toca junto com música local/yt-dlp, reduza a música para 5%
# do volume normal e restaure automaticamente ao fim do TTS.
MUSIC_TTS_LOCAL_DUCK_PERCENT = max(0.0, min(100.0, _parse_float(os.getenv("MUSIC_TTS_LOCAL_DUCK_PERCENT", "5"), 5.0)))
# Para música via Lavalink, o TTS usa o próprio node: pausa a faixa atual, toca
# o áudio curto e restaura a música na posição anterior.
MUSIC_LAVALINK_TTS_PAUSE_ENABLED = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_PAUSE_ENABLED", "true"), True)
MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS = max(0.2, _parse_float(os.getenv("MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS", "0.35"), 0.35))
MUSIC_TTS_PUBLIC_BASE_URL = (
    os.getenv("MUSIC_TTS_PUBLIC_BASE_URL", os.getenv("PUBLIC_BASE_URL", os.getenv("WEB_PUBLIC_BASE_URL", os.getenv("RENDER_EXTERNAL_URL", ""))))
    or ""
).strip().rstrip("/")
# URL local que o Lavalink usa para buscar TTS gerado pelo próprio bot.
# O padrão usa a porta do webserver/waitress no mesmo host, evitando DNS, HTTPS e
# proxy externo. A URL pública continua como fallback.
MUSIC_TTS_INTERNAL_BASE_URL = (
    os.getenv("MUSIC_TTS_INTERNAL_BASE_URL", f"http://127.0.0.1:{os.getenv('PORT', '10000')}")
    or ""
).strip().rstrip("/")
MUSIC_LAVALINK_TTS_INTERNAL_FIRST = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_INTERNAL_FIRST", "true"), True)
MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS = max(0.25, _parse_float(os.getenv("MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS", "1.75"), 1.75))
MUSIC_LAVALINK_TTS_FILE_FALLBACK = _parse_bool(os.getenv("MUSIC_LAVALINK_TTS_FILE_FALLBACK", "false"), False)
# Se o Lavalink recebe o TTS/arquivo mas perde o voice state (state=None/voice_keys=[]),
# o bot pode cair para o TTS local direto em vez de silenciar a mensagem.
MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK = _parse_bool(os.getenv("MUSIC_TTS_LAVALINK_FAILURE_LOCAL_FALLBACK", "true"), True)
MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_TTS_LAVALINK_LOCAL_FALLBACK_COOLDOWN_SECONDS", "45"), 45.0))
MUSIC_LAVALINK_TTS_URL_TTL_SECONDS = max(30, _parse_int(os.getenv("MUSIC_LAVALINK_TTS_URL_TTL_SECONDS", "240"), 240))
# Formato preferido para o áudio temporário de TTS usado pelo Lavalink.
# OGG/Opus é menor e costuma carregar mais rápido que MP3; MP3 fica como fallback
# por compatibilidade quando a conversão ou o loadtracks do Lavalink falhar.
MUSIC_TTS_AUDIO_FORMAT = (os.getenv("MUSIC_TTS_AUDIO_FORMAT", "opus") or "opus").strip().lower()
MUSIC_TTS_AUDIO_FALLBACK_FORMAT = (os.getenv("MUSIC_TTS_AUDIO_FALLBACK_FORMAT", "mp3") or "mp3").strip().lower()
MUSIC_TTS_OPUS_BITRATE = (os.getenv("MUSIC_TTS_OPUS_BITRATE", "48k") or "48k").strip()
MUSIC_TTS_OPUS_SAMPLE_RATE = max(8000, _parse_int(os.getenv("MUSIC_TTS_OPUS_SAMPLE_RATE", "48000"), 48000))
MUSIC_TTS_OPUS_CHANNELS = min(2, max(1, _parse_int(os.getenv("MUSIC_TTS_OPUS_CHANNELS", "1"), 1)))
MUSIC_TTS_CONVERT_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_TTS_CONVERT_TIMEOUT_SECONDS", "8.0"), 8.0))
# Suavização das transições do TTS tocado pelo Lavalink.
# O áudio curto recebe silêncio/fade antes de ser publicado, e a música faz uma
# rampa breve de volume antes/depois da interrupção para evitar clicks/flicker.
MUSIC_TTS_PREROLL_SILENCE_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_PREROLL_SILENCE_MS", "140"), 140))
MUSIC_TTS_POSTROLL_SILENCE_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_POSTROLL_SILENCE_MS", "180"), 180))
MUSIC_TTS_FADE_IN_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_FADE_IN_MS", "45"), 45))
MUSIC_TTS_FADE_OUT_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_FADE_OUT_MS", "70"), 70))
MUSIC_TTS_MP3_BITRATE = (os.getenv("MUSIC_TTS_MP3_BITRATE", "96k") or "96k").strip()
MUSIC_TTS_RESUME_SEEK_AHEAD_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_RESUME_SEEK_AHEAD_MS", "120"), 120))
MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED = _parse_bool(os.getenv("MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", "true"), True)
MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS = max(0, _parse_int(os.getenv("MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", "180"), 180))
MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT = max(0, min(100, _parse_int(os.getenv("MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", "5"), 5)))
MUSIC_IDLE_DISCONNECT_SECONDS = _parse_int(os.getenv("MUSIC_IDLE_DISCONNECT_SECONDS", "120"), 120)
MUSIC_QUEUE_MAXSIZE = min(100, max(1, _parse_int(os.getenv("MUSIC_QUEUE_MAXSIZE", "100"), 100)))
MUSIC_MAX_PLAYLIST_ITEMS = min(100, max(1, _parse_int(os.getenv("MUSIC_MAX_PLAYLIST_ITEMS", "100"), 100)))
MUSIC_SEARCH_RESULTS = _parse_int(os.getenv("MUSIC_SEARCH_RESULTS", "5"), 5)
MUSIC_YOUTUBE_SEARCH_API_FIRST = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_API_FIRST", "true"), True)
# Pesquisa textual do YouTube deve ser rápida: por padrão só lista metadata leve
# (API oficial se configurada, depois yt-dlp flat). Desative para permitir fallback
# pesado de busca completa quando a busca leve não encontrar nada.
MUSIC_YOUTUBE_SEARCH_FAST_ONLY = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_FAST_ONLY", "true"), True)
MUSIC_YOUTUBE_SEARCH_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_YOUTUBE_SEARCH_TIMEOUT_SECONDS", "7.0"), 7.0))
MUSIC_YOUTUBE_SEARCH_USE_COOKIES = _parse_bool(os.getenv("MUSIC_YOUTUBE_SEARCH_USE_COOKIES", "false"), False)
# Link direto do YouTube entra rápido no queue/painel e resolve o stream só na hora
# de tocar. Isso evita o comando travar em yt-dlp antes de responder.
MUSIC_YOUTUBE_DIRECT_FAST_ENQUEUE = _parse_bool(os.getenv("MUSIC_YOUTUBE_DIRECT_FAST_ENQUEUE", "true"), True)
MUSIC_YOUTUBE_DIRECT_METADATA_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("MUSIC_YOUTUBE_DIRECT_METADATA_TIMEOUT_SECONDS", "1.4"), 1.4))
# Resolução local rápida: reduz combinações de clients/formatos do yt-dlp.
MUSIC_LOCAL_YOUTUBE_FAST_RESOLVE = _parse_bool(os.getenv("MUSIC_LOCAL_YOUTUBE_FAST_RESOLVE", "true"), True)
MUSIC_LOCAL_YOUTUBE_CLIENTS = (os.getenv("MUSIC_LOCAL_YOUTUBE_CLIENTS", "ios,android,web") or "ios,android,web").strip()
# Na VPS, YouTube costuma bloquear extrações sem cookie com "confirm you are not a bot".
# Para link direto/resultado do YouTube, usar cookies primeiro evita duas tentativas lentas
# sem cookie antes de chegar no caminho que realmente funciona.
MUSIC_LOCAL_YOUTUBE_COOKIES_FIRST = _parse_bool(os.getenv("MUSIC_LOCAL_YOUTUBE_COOKIES_FIRST", "true"), True)
MUSIC_LOCAL_YOUTUBE_RESOLVE_ATTEMPT_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_RESOLVE_ATTEMPT_TIMEOUT_SECONDS", "5.0"), 5.0))
MUSIC_LOCAL_YOUTUBE_NO_COOKIE_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_NO_COOKIE_TIMEOUT_SECONDS", "1.6"), 1.6))
# Limite total do caminho local do YouTube por faixa. Sem isso, várias
# combinações de clients/formatos podem deixar o painel preso em “resolvendo”.
MUSIC_LOCAL_YOUTUBE_RESOLVE_TOTAL_TIMEOUT_SECONDS = max(6.0, _parse_float(os.getenv("MUSIC_LOCAL_YOUTUBE_RESOLVE_TOTAL_TIMEOUT_SECONDS", "14.0"), 14.0))
# Resultado escolhido no YouTube tenta mirror LavaSrc por pouco tempo. Se o
# espelho não bater/abrir rápido, cai para yt-dlp local sem segurar o usuário.
MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_YOUTUBE_LAVASRC_MIRROR_TIMEOUT_SECONDS", "2.5"), 2.5))
MUSIC_YTDLP_TIMEOUT_SECONDS = _parse_float(os.getenv("MUSIC_YTDLP_TIMEOUT_SECONDS", "20"), 20.0)
MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS", "8"), 8.0))
MUSIC_YTDLP_RETRIES = max(0, _parse_int(os.getenv("MUSIC_YTDLP_RETRIES", "1"), 1))
MUSIC_FRAGMENT_RETRIES = max(0, _parse_int(os.getenv("MUSIC_FRAGMENT_RETRIES", "1"), 1))
MUSIC_EXTRACTOR_RETRIES = max(0, _parse_int(os.getenv("MUSIC_EXTRACTOR_RETRIES", "1"), 1))
MUSIC_PLAYLIST_LAZY_LOAD = _parse_bool(os.getenv("MUSIC_PLAYLIST_LAZY_LOAD", "true"), True)
MUSIC_STREAM_START_RETRIES = max(0, _parse_int(os.getenv("MUSIC_STREAM_START_RETRIES", "1"), 1))
MUSIC_HISTORY_MAXSIZE = _parse_int(os.getenv("MUSIC_HISTORY_MAXSIZE", "25"), 25)
MUSIC_CONTROL_VOTE_SECONDS = _parse_int(os.getenv("MUSIC_CONTROL_VOTE_SECONDS", "45"), 45)
MUSIC_YTDLP_COOKIES_FILE = (os.getenv("MUSIC_YTDLP_COOKIES_FILE", os.getenv("YTDLP_COOKIES_FILE", "")) or "").strip()
MUSIC_API_SEARCH_ENABLED = _parse_bool(os.getenv("MUSIC_API_SEARCH_ENABLED", "true"), True)
MUSIC_API_TIMEOUT_SECONDS = _parse_float(os.getenv("MUSIC_API_TIMEOUT_SECONDS", "5.0"), 5.0)
MUSIC_METADATA_CACHE_TTL_SECONDS = _parse_int(os.getenv("MUSIC_METADATA_CACHE_TTL_SECONDS", "300"), 300)
MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS = max(0, _parse_int(os.getenv("MUSIC_LAVALINK_SEARCH_CACHE_TTL_SECONDS", "90"), 90))
MUSIC_STREAM_CACHE_TTL_SECONDS = _parse_int(os.getenv("MUSIC_STREAM_CACHE_TTL_SECONDS", "480"), 480)
MUSIC_CACHE_MAX_ITEMS = _parse_int(os.getenv("MUSIC_CACHE_MAX_ITEMS", "160"), 160)
MUSIC_PREFETCH_NEXT = _parse_bool(os.getenv("MUSIC_PREFETCH_NEXT", "true"), True)
# Em VPS fraca, pré-resolver a próxima música imediatamente pode disputar CPU
# com o áudio atual. Por padrão, o prefetch só roda depois de alguns segundos
# e, em músicas com duração conhecida, perto do fim.
MUSIC_PREFETCH_MIN_DELAY_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_PREFETCH_MIN_DELAY_SECONDS", "18"), 18.0))
MUSIC_PREFETCH_BEFORE_END_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_PREFETCH_BEFORE_END_SECONDS", "45"), 45.0))
MUSIC_LIMITER_ENABLED = _parse_bool(os.getenv("MUSIC_LIMITER_ENABLED", "true"), True)
MUSIC_YTDLP_FORMAT = (
    os.getenv(
        "MUSIC_YTDLP_FORMAT",
        # Alta qualidade real, mas sem filtros rígidos. O yt-dlp escolhe o
        # melhor áudio possível; fallbacks específicos ficam no extractor.
        "bestaudio/best",
    )
    or "bestaudio/best"
).strip()
YOUTUBE_API_KEY = (os.getenv("YOUTUBE_API_KEY", os.getenv("GOOGLE_YOUTUBE_API_KEY", "")) or "").strip()
SPOTIFY_CLIENT_ID = (os.getenv("SPOTIFY_CLIENT_ID", "") or "").strip()
SPOTIFY_CLIENT_SECRET = (os.getenv("SPOTIFY_CLIENT_SECRET", "") or "").strip()
# Refresh token de usuário para playlists/álbuns privados ou colaborativos do Spotify.
# Client Credentials continua sendo usado para busca/faixas públicas quando não houver refresh token.
SPOTIFY_REFRESH_TOKEN = (os.getenv("SPOTIFY_REFRESH_TOKEN", "") or "").strip()
SPOTIFY_REDIRECT_URI = (os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback") or "http://127.0.0.1:8888/callback").strip()
SPOTIFY_MARKET = (os.getenv("SPOTIFY_MARKET", "BR") or "BR").strip().upper()
# Fallback público/não oficial para ler metadata de links públicos do Spotify
# quando a Web API oficial retornar 403 para playlists públicas em apps novos.
SPOTIFY_PUBLIC_FALLBACK_ENABLED = _parse_bool(os.getenv("SPOTIFY_PUBLIC_FALLBACK_ENABLED", "true"), True)
SPOTIFY_PUBLIC_FALLBACK_MAX_TRACKS = max(1, _parse_int(os.getenv("SPOTIFY_PUBLIC_FALLBACK_MAX_TRACKS", "100"), 100))
DEEZER_API_ENABLED = _parse_bool(os.getenv("DEEZER_API_ENABLED", "false"), False)
SOUNDCLOUD_API_ENABLED = _parse_bool(os.getenv("SOUNDCLOUD_API_ENABLED", "false"), False)
SOUNDCLOUD_API_TOKEN = (os.getenv("SOUNDCLOUD_API_TOKEN", "") or "").strip()
SOUNDCLOUD_CLIENT_ID = (os.getenv("SOUNDCLOUD_CLIENT_ID", "") or "").strip()
SOUNDCLOUD_API_BASE_URL = (os.getenv("SOUNDCLOUD_API_BASE_URL", "https://api.soundcloud.com/tracks") or "https://api.soundcloud.com/tracks").strip()
MUSIC_FFMPEG_BEFORE_OPTIONS = (
    os.getenv("MUSIC_FFMPEG_BEFORE_OPTIONS", "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 10000000 -multiple_requests 1")
    or "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,408,429,5xx -reconnect_delay_max 5 -rw_timeout 10000000 -multiple_requests 1"
).strip()
MUSIC_FFMPEG_OPTIONS = (
    os.getenv("MUSIC_FFMPEG_OPTIONS", "-vn -sn -dn -loglevel error -ar 48000 -ac 2")
    or "-vn -sn -dn -loglevel error -ar 48000 -ac 2"
).strip()
MUSIC_TTS_FFMPEG_OPTIONS = (os.getenv("MUSIC_TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error").strip()
MUSIC_MAX_GLOBAL_EXTRACTORS = max(1, _parse_int(os.getenv("MUSIC_MAX_GLOBAL_EXTRACTORS", "1"), 1))
MUSIC_MAX_GLOBAL_PREFETCH = max(0, _parse_int(os.getenv("MUSIC_MAX_GLOBAL_PREFETCH", "1"), 1))
MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS = max(0, _parse_int(os.getenv("MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS", "2"), 2))
# Qualidade dinâmica: com só 1 servidor tocando, usa melhor áudio-only
# disponível sem teto de abr; com 2+ servidores, limita bitrate para poupar
# CPU/rede da VPS.
MUSIC_AUDIO_MODE = (os.getenv("MUSIC_AUDIO_MODE", "auto") or "auto").strip().lower()
MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS = max(1, _parse_int(os.getenv("MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS", "1"), 1))
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, _parse_int(os.getenv("MUSIC_HIGH_QUALITY_MAX_ABR", "256"), 256))  # mantido para compat/env antigo
MUSIC_MAX_AUDIO_BITRATE_STABLE = max(64, _parse_int(os.getenv("MUSIC_MAX_AUDIO_BITRATE_STABLE", "160"), 160))
MUSIC_HEAVY_LOAD_MAX_ABR = max(64, _parse_int(os.getenv("MUSIC_HEAVY_LOAD_MAX_ABR", "128"), 128))
MUSIC_AUTO_BITRATE_ENABLED = _parse_bool(os.getenv("MUSIC_AUTO_BITRATE_ENABLED", "true"), True)
MUSIC_AUTO_BITRATE_MAX = max(8000, _parse_int(os.getenv("MUSIC_AUTO_BITRATE_MAX", "384000"), 384000))
MUSIC_AUTO_BITRATE_MIN_GAIN = max(0, _parse_int(os.getenv("MUSIC_AUTO_BITRATE_MIN_GAIN", "16000"), 16000))
MUSIC_PANEL_UPDATE_THROTTLE_SECONDS = max(0.05, _parse_float(os.getenv("MUSIC_PANEL_UPDATE_THROTTLE_SECONDS", "2.0"), 2.0))
MUSIC_PANEL_REPOST_ON_TRACK_CHANGE = _parse_bool(os.getenv("MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", "true"), True)
MUSIC_VOICE_STATUS_ENABLED = _parse_bool(os.getenv("MUSIC_VOICE_STATUS_ENABLED", "true"), True)
MUSIC_VOICE_STATUS_TEMPLATE = (
    os.getenv(
        "MUSIC_VOICE_STATUS_TEMPLATE",
        "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})",
    )
    or "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})"
).strip()
MUSIC_VOICE_STATUS_IDLE = (os.getenv("MUSIC_VOICE_STATUS_IDLE", "") or "").strip()
MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS = max(15.0, _parse_float(os.getenv("MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS", "60"), 60.0))
MUSIC_MIN_LINK_METADATA_CONFIDENCE = (os.getenv("MUSIC_MIN_LINK_METADATA_CONFIDENCE", "medium") or "medium").strip().lower()
MUSIC_MAX_DURATION_MISMATCH_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_SECONDS", "45"), 45.0))
MUSIC_MAX_DURATION_MISMATCH_RATIO = max(0.0, _parse_float(os.getenv("MUSIC_MAX_DURATION_MISMATCH_RATIO", "0.25"), 0.25))
MUSIC_REJECT_WEAK_LINK_MATCHES = _parse_bool(os.getenv("MUSIC_REJECT_WEAK_LINK_MATCHES", "true"), True)
# Mirrors do LavaSrc usados para transformar metadata de Spotify/YouTube em áudio tocável.
# Spotify direto/spsearch fica fora do padrão porque o LavaSrc 4.8.x pode falhar com 403.
MUSIC_LAVASRC_MIRROR_PREFIXES = (os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "scsearch") or "scsearch").strip()

# O APK assume o runtime móvel. O Termux pode continuar temporariamente apenas
# como builder bootstrap do primeiro APK com toolchain interno; nunca buildamos na VPS.
CORE_WORKER_APK_REPLACES_TERMUX = _parse_bool(os.getenv("CORE_WORKER_APK_REPLACES_TERMUX", "true"), True)
CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED = _parse_bool(
    os.getenv("CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED", "true"), True
)

# Lavalink/phone worker — música agora deve rodar fora da VPS.
# MUSIC_BACKEND fica aceitando valor antigo por compatibilidade, mas o fluxo
# musical usa MUSIC_WORKER_ONLY_ENABLED para bloquear fallback local pesado.
MUSIC_BACKEND = "local" if CORE_WORKER_APK_REPLACES_TERMUX else (os.getenv("MUSIC_BACKEND", "worker") or "worker").strip().lower()
LAVALINK_ENABLED = _parse_bool(os.getenv("LAVALINK_ENABLED", "false"), False)
LAVALINK_MODE = (os.getenv("LAVALINK_MODE", "off") or "off").strip().lower()
LAVALINK_HOST = (os.getenv("LAVALINK_HOST", "") or "").strip()
LAVALINK_PORT = _parse_int(os.getenv("LAVALINK_PORT", "2333"), 2333)
LAVALINK_PASSWORD = (os.getenv("LAVALINK_PASSWORD", "") or "").strip()
LAVALINK_SECURE = _parse_bool(os.getenv("LAVALINK_SECURE", "false"), False)
LAVALINK_NODE_NAME = (os.getenv("LAVALINK_NODE_NAME", "main") or "main").strip() or "main"
LAVALINK_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("LAVALINK_TIMEOUT_SECONDS", "8.0"), 8.0))

# Lavalink auxiliar — pensado para o phone worker/celular via Tailscale.
# Em MUSIC_WORKER_ONLY_ENABLED=true, este node vira o node musical preferencial
# e não há fallback para execução pesada local na VPS.
AUX_LAVALINK_ENABLED = _parse_bool(os.getenv("AUX_LAVALINK_ENABLED", "false"), False)
AUX_LAVALINK_HOST = (os.getenv("AUX_LAVALINK_HOST", "") or "").strip()
AUX_LAVALINK_PORT = _parse_int(os.getenv("AUX_LAVALINK_PORT", "2333"), 2333)
AUX_LAVALINK_PASSWORD = (os.getenv("AUX_LAVALINK_PASSWORD", "") or "").strip()
AUX_LAVALINK_SECURE = _parse_bool(os.getenv("AUX_LAVALINK_SECURE", "false"), False)
AUX_LAVALINK_NODE_NAME = (os.getenv("AUX_LAVALINK_NODE_NAME", "phone") or "phone").strip() or "phone"
AUX_LAVALINK_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("AUX_LAVALINK_TIMEOUT_SECONDS", "3.0"), 3.0))
AUX_LAVALINK_COOLDOWN_SECONDS = max(10.0, _parse_float(os.getenv("AUX_LAVALINK_COOLDOWN_SECONDS", "300"), 300.0))


# Music Agent no phone worker — padrão da música. A VPS fica como plano de UI/status
# e o worker assume voz/player/Lavalink/yt-dlp quando disponível.
MUSIC_AGENT_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX) and _parse_bool(os.getenv("MUSIC_AGENT_ENABLED", "true"), True)
MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", "18.0"), 18.0))
MUSIC_AGENT_STATUS_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", "5.0"), 5.0))
MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", "30.0"), 30.0))
MUSIC_AGENT_STATUS_POLL_SECONDS = max(0.4, _parse_float(os.getenv("MUSIC_AGENT_STATUS_POLL_SECONDS", "0.75"), 0.75))
MUSIC_AGENT_IDLE_DISCONNECT_SECONDS = max(15.0, _parse_float(os.getenv("MUSIC_AGENT_IDLE_DISCONNECT_SECONDS", os.getenv("MUSIC_IDLE_DISCONNECT_SECONDS", "120")), 120.0))
MUSIC_LOADING_REACTION_EMOJI = (os.getenv("MUSIC_LOADING_REACTION_EMOJI", "<a:areia:1496606578395189473>") or "<a:areia:1496606578395189473>").strip()
MUSIC_AGENT_MIN_VERSION = (os.getenv("MUSIC_AGENT_MIN_VERSION", "0.3.23") or "0.3.23").strip()
MUSIC_AGENT_MAX_SESSIONS_PER_WORKER = max(1, _parse_int(os.getenv("MUSIC_AGENT_MAX_SESSIONS_PER_WORKER", "2"), 2))
MUSIC_AGENT_PLAYING_CONFIRM_SECONDS = max(2.0, _parse_float(os.getenv("MUSIC_AGENT_PLAYING_CONFIRM_SECONDS", "12.0"), 12.0))
MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT = max(0, min(100, _parse_int(os.getenv("MUSIC_AGENT_TTS_DUCK_VOLUME_PERCENT", "8"), 8)))
MUSIC_AGENT_TTS_ROUTE_ENABLED = _parse_bool(os.getenv("MUSIC_AGENT_TTS_ROUTE_ENABLED", "true"), True)
MUSIC_AGENT_TTS_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_AGENT_TTS_TIMEOUT_SECONDS", "30.0"), 30.0))
MUSIC_AGENT_DIRECT_CONFIRM_SECONDS = max(0.15, _parse_float(os.getenv("MUSIC_AGENT_DIRECT_CONFIRM_SECONDS", "0.35"), 0.35))
MUSIC_AGENT_PREFETCH_ENABLED = _parse_bool(os.getenv("MUSIC_AGENT_PREFETCH_ENABLED", "true"), True)
MUSIC_AGENT_PREFETCH_TOP_RESULTS = max(0, min(3, _parse_int(os.getenv("MUSIC_AGENT_PREFETCH_TOP_RESULTS", "2"), 2)))
MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_RESOLVE_CACHE_TTL_SECONDS", "300.0"), 300.0))
MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_METADATA_CACHE_TTL_SECONDS", "21600.0"), 21600.0))
MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_AGENT_STREAM_CACHE_TTL_SECONDS", "180.0"), 180.0))
MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_AGENT_PREFETCH_TIMEOUT_SECONDS", "18.0"), 18.0))
MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", "420.0"), 420.0))
MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS = max(0.0, _parse_float(os.getenv("MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", "90.0"), 90.0))
MUSIC_AGENT_BOOTSTRAP_ON_PLAY = _parse_bool(os.getenv("MUSIC_AGENT_BOOTSTRAP_ON_PLAY", "true"), True)
MUSIC_AGENT_MISSING_TOKEN_MESSAGE = (
    os.getenv("MUSIC_AGENT_MISSING_TOKEN_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
).strip()

# Phone-worker auxiliar — celular via Tailscale.
# Para música, o modo atual é worker-only: a VPS não deve usar yt-dlp/FFmpeg local
# nem fallback pesado. O worker turbo/phone-lavalink precisa estar online.
MUSIC_WORKER_ONLY_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX) and _parse_bool(os.getenv("MUSIC_WORKER_ONLY_ENABLED", "true"), True)
MUSIC_WORKER_UNAVAILABLE_MESSAGE = (
    os.getenv("MUSIC_WORKER_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker online")
    or "Sistema de música indisponível no momento: Nenhum worker online"
).strip()
MUSIC_WORKER_NO_CAPACITY_MESSAGE = (
    os.getenv("MUSIC_WORKER_NO_CAPACITY_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker disponível")
    or "Sistema de música indisponível no momento: Nenhum worker disponível"
).strip()
MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE = (
    os.getenv("MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
).strip()
MUSIC_WORKER_REQUIRE_TURBO = (not CORE_WORKER_APK_REPLACES_TERMUX) and _parse_bool(os.getenv("MUSIC_WORKER_REQUIRE_TURBO", "true"), True)
MUSIC_WORKER_REQUIRED_ROLES = (os.getenv("MUSIC_WORKER_REQUIRED_ROLES", "phone-worker") or "phone-worker").strip()
MUSIC_WORKER_REQUIRED_CAPABILITIES = (os.getenv("MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe") or "ffmpeg,ffprobe").strip()
# Segredos/cookies do yt-dlp em modo worker-only ficam no phone worker, não na VPS.
# Use um caminho local do celular, por exemplo:
# ~/phone-worker/secrets/youtube-cookies.txt
MUSIC_WORKER_YTDLP_COOKIES_FILE = (
    os.getenv("MUSIC_WORKER_YTDLP_COOKIES_FILE", os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE", ""))
    or ""
).strip()
MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS = max(5.0, _parse_float(os.getenv("MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", "32.0"), 32.0))
MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS = max(4.0, _parse_float(os.getenv("MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", "12.0"), 12.0))
MUSIC_WORKER_YTDLP_JS_RUNTIMES = (
    os.getenv("MUSIC_WORKER_YTDLP_JS_RUNTIMES", os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES", "node"))
    or "node"
).strip()
MUSIC_WORKER_YTDLP_DEFAULT_SEARCH = (
    os.getenv("MUSIC_WORKER_YTDLP_DEFAULT_SEARCH", os.getenv("PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH", "ytsearch"))
    or "ytsearch"
).strip()
MUSIC_WORKER_STREAM_CONNECT_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_CONNECT_TIMEOUT_SECONDS", "90.0"), 90.0))
MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_READY_TIMEOUT_SECONDS", "90.0"), 90.0))
MUSIC_WORKER_STREAM_PREBUFFER_SECONDS = max(0.1, _parse_float(os.getenv("MUSIC_WORKER_STREAM_PREBUFFER_SECONDS", "8.0"), 8.0))
MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_STREAM_MAX_BUFFER_SECONDS", "30.0"), 30.0))
MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY = max(1, _parse_int(os.getenv("MUSIC_WORKER_STREAM_UNDERRUN_LOG_EVERY", "25"), 25))
MUSIC_WORKER_LAVALINK_HOST = (os.getenv("MUSIC_WORKER_LAVALINK_HOST", "") or "").strip()
MUSIC_WORKER_LAVALINK_PORT = _parse_int(os.getenv("MUSIC_WORKER_LAVALINK_PORT", "2333"), 2333)
MUSIC_WORKER_LAVALINK_PASSWORD = (os.getenv("MUSIC_WORKER_LAVALINK_PASSWORD", "") or "").strip()
MUSIC_WORKER_LAVALINK_SECURE = _parse_bool(os.getenv("MUSIC_WORKER_LAVALINK_SECURE", "false"), False)
MUSIC_WORKER_LAVALINK_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("MUSIC_WORKER_LAVALINK_TIMEOUT_SECONDS", "3.0"), 3.0))
# Quando true, exige que o heartbeat do worker já informe status.music_node/lavalink saudável.
# Fica false por compatibilidade com agents antigos; o backend Lavalink ainda valida o node.
MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS = _parse_bool(os.getenv("MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS", "false"), False)
MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED = _parse_bool(os.getenv("MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED", "true"), True)
MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS = max(0.3, _parse_float(os.getenv("MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS", "3.5"), 3.5))
# Em worker-only, o node auxiliar do celular vira o node preferencial/único da música
# quando AUX_LAVALINK_* estiver configurado. Sem AUX, usa LAVALINK_* como node do worker.
MUSIC_WORKER_LAVALINK_USE_AUX = _parse_bool(os.getenv("MUSIC_WORKER_LAVALINK_USE_AUX", "true"), True)

PHONE_WORKER_HOST = (os.getenv("PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = _parse_int(os.getenv("PHONE_WORKER_PORT", "8766"), 8766)
PHONE_WORKER_SCHEME = (os.getenv("PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
PHONE_WORKER_TOKEN = (os.getenv("PHONE_WORKER_TOKEN", "") or "").strip()
# Mantém os consumidores existentes apontando para a API compatível do APK.
# Uma configuração antiga PHONE_WORKER_ENABLED=false não deve desligar o APK
# quando host e token diretos já estão presentes.
PHONE_WORKER_ENABLED = (
    CORE_WORKER_APK_REPLACES_TERMUX and bool(PHONE_WORKER_HOST and PHONE_WORKER_TOKEN)
) or _parse_bool(os.getenv("PHONE_WORKER_ENABLED", "false"), False)
PHONE_WORKER_ZIP_VALIDATE_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_ZIP_VALIDATE_ENABLED", "true"), True)
PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS", "5.0"), 5.0))
PHONE_WORKER_ZIP_VALIDATE_MAX_MB = max(1, _parse_int(os.getenv("PHONE_WORKER_ZIP_VALIDATE_MAX_MB", "24"), 24))
PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED", "true"), True)
PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS", "4.0"), 4.0))
PHONE_WORKER_MAINTENANCE_PLAN_ENABLED = _parse_bool(os.getenv("PHONE_WORKER_MAINTENANCE_PLAN_ENABLED", "true"), True)

# Benchmark restrito de TTS: usado só para provar se o worker turbo realmente
# sintetiza mais rápido que a VPS. Não altera o fluxo normal de TTS.
TTS_TURBO_BENCHMARK_ENABLED = _parse_bool(os.getenv("TTS_TURBO_BENCHMARK_ENABLED", "true"), True)
TTS_TURBO_BENCHMARK_GUILD_ID = _parse_int(os.getenv("TTS_TURBO_BENCHMARK_GUILD_ID", "927002914449424404"), 927002914449424404)
TTS_TURBO_BENCHMARK_TRIGGER_TEXT = (os.getenv("TTS_TURBO_BENCHMARK_TRIGGER_TEXT", "teste") or "teste").strip().lower()
TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS = max(1.5, _parse_float(os.getenv("TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS", "12.0"), 12.0))
TTS_TURBO_BENCHMARK_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TURBO_BENCHMARK_MAX_AUDIO_MB", "4"), 4))

# ATTS: Android TTS nativo do APK/phone-worker. O prefixo público oficial é %.
# Mantemos os nomes antigos TTS_PIPER_EXPERIMENT_* apenas como alias de migração.
TTS_ATTS_PREFIX = (os.getenv("TTS_ATTS_PREFIX") or os.getenv("TTS_PIPER_EXPERIMENT_PREFIX") or "%").strip() or "%"
TTS_ATTS_ENABLED = _parse_bool(os.getenv("TTS_ATTS_ENABLED", os.getenv("TTS_PIPER_EXPERIMENT_ENABLED", "true")), True)

# Kasane Teto: engine UTAU exclusiva do phone worker. O bot nunca tenta
# carregar voicebank/resampler na VPS; quando o worker não estiver pronto, usa
# a configuração normal do usuário como fallback.
TTS_TETO_ENABLED = _parse_bool(os.getenv("TTS_TETO_ENABLED", "true"), True)
TTS_TETO_PREFIX = (os.getenv("TTS_TETO_PREFIX", "'") or "'").strip() or "'"
TTS_TETO_ENGINE = "teto"
TTS_TETO_MAX_TEXT_LENGTH = max(16, _parse_int(os.getenv("TTS_TETO_MAX_TEXT_LENGTH", "180"), 180))
TTS_TETO_WORKER_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("TTS_TETO_WORKER_TIMEOUT_SECONDS", "25"), 25.0))
TTS_TETO_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TETO_MAX_AUDIO_MB", "8"), 8))
TTS_PIPER_EXPERIMENT_ENABLED = TTS_ATTS_ENABLED
TTS_PIPER_EXPERIMENT_GUILD_ID = _parse_int(os.getenv("TTS_PIPER_EXPERIMENT_GUILD_ID", "0"), 0)
TTS_PIPER_EXPERIMENT_PREFIX = TTS_ATTS_PREFIX
TTS_PIPER_EXPERIMENT_ENGINE = "android_native"
TTS_PIPER_WORKER_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("TTS_PIPER_WORKER_TIMEOUT_SECONDS", "6.0"), 6.0))
TTS_PIPER_MAX_TEXT_LENGTH = max(16, _parse_int(os.getenv("TTS_PIPER_MAX_TEXT_LENGTH", "600"), 600))
TTS_PIPER_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_PIPER_MAX_AUDIO_MB", "8"), 8))
TTS_PIPER_MODEL_NAME = (os.getenv("TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
TTS_PIPER_VPS_CACHE_SIZE = max(32, _parse_int(os.getenv("TTS_PIPER_VPS_CACHE_SIZE", "2048"), 2048))
TTS_PIPER_VPS_CACHE_MAX_MB = max(64, _parse_int(os.getenv("TTS_PIPER_VPS_CACHE_MAX_MB", "2048"), 2048))

# Cache do phone-worker turbo como segunda camada opcional do TTS.
# A VPS consulta esse cache com timeout curto; se falhar ou der miss, sintetiza localmente normalmente.
TTS_TURBO_WORKER_CACHE_ENABLED = _parse_bool(os.getenv("TTS_TURBO_WORKER_CACHE_ENABLED", "true"), True)
TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS = max(0.15, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS", "0.65"), 0.65))
TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS = max(0.5, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS", "2.5"), 2.5))
TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB", "8"), 8))
TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND = _parse_bool(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND", "true"), True)
# Upload de cache é secundário: mantém uma fila pequena e serializada para não
# competir com síntese, playback e memória da VPS durante bursts.
TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY = max(1, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY", "1"), 1))
TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING = max(TTS_TURBO_WORKER_CACHE_STORE_CONCURRENCY, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_STORE_MAX_PENDING", "6"), 6))
TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS = max(1.0, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS", "45.0"), 45.0))
TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS = max(1.0, _parse_float(os.getenv("TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS", "10.0"), 10.0))
TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES = max(128, _parse_int(os.getenv("TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES", "4096"), 4096))

# TTS Agent: rota normal do TTS pelo phone-worker quando ele estiver online/saudável.
# O health loop mantém o modo em cache; a fala não faz probe antes de sintetizar.
TTS_WORKER_AGENT_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_ENABLED", "true"), True)
TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS = max(5.0, _parse_float(os.getenv("TTS_WORKER_AGENT_HEALTH_INTERVAL_SECONDS", "20"), 20.0))
TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS = max(0.4, _parse_float(os.getenv("TTS_WORKER_AGENT_HEALTH_TIMEOUT_SECONDS", "2.5"), 2.5))
TTS_WORKER_AGENT_STALE_SECONDS = max(10.0, _parse_float(os.getenv("TTS_WORKER_AGENT_STALE_SECONDS", "75"), 75.0))
TTS_WORKER_AGENT_FAILURE_THRESHOLD = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_FAILURE_THRESHOLD", "2"), 2))
TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS = max(2.0, _parse_float(os.getenv("TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS", "10"), 10.0))
TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS = max(0, _parse_int(os.getenv("TTS_WORKER_AGENT_BUSY_RETRY_ATTEMPTS", "2"), 2))
TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS = max(0.05, _parse_float(os.getenv("TTS_WORKER_AGENT_BUSY_RETRY_DELAY_SECONDS", "0.35"), 0.35))
TTS_WORKER_AGENT_MAX_AUDIO_MB = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_MAX_AUDIO_MB", "8"), 8))
TTS_WORKER_AGENT_MAX_TEXT_LENGTH = max(64, _parse_int(os.getenv("TTS_WORKER_AGENT_MAX_TEXT_LENGTH", "1200"), 1200))
TTS_WORKER_AGENT_PREFERRED_ENGINE = (os.getenv("TTS_WORKER_AGENT_PREFERRED_ENGINE", "auto") or "auto").strip().lower().replace("-", "_") or "auto"
# Health leve + rota adaptativa: não deixa o TTS esperar worker offline/lento.
TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD = max(1, _parse_int(os.getenv("TTS_WORKER_AGENT_HEALTH_FAILURE_THRESHOLD", "3"), 3))
TTS_WORKER_AGENT_RAW_AUDIO_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", "true"), True)
TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED = _parse_bool(os.getenv("TTS_WORKER_AGENT_ADAPTIVE_ROUTING_ENABLED", "true"), True)
TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES = (os.getenv("TTS_WORKER_AGENT_ALWAYS_WORKER_ENGINES", "android_native,teto") or "android_native,teto").strip().lower().replace("-", "_")
TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS = max(0, _parse_int(os.getenv("TTS_WORKER_AGENT_GTTS_MIN_WORKER_CHARS", "120"), 120))
TTS_WORKER_AGENT_WORKER_SLOW_MARGIN = max(1.0, _parse_float(os.getenv("TTS_WORKER_AGENT_WORKER_SLOW_MARGIN", "1.15"), 1.15))
TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS = max(0.0, _parse_float(os.getenv("TTS_WORKER_AGENT_WORKER_MIN_ADVANTAGE_MS", "120"), 120.0))

# Worker Voice Agent roadmap: the VPS remains the bot/control plane, while the
# turbo worker becomes the direct audio/voice plane when this staged path is ready.
WORKER_VOICE_AGENT_ENABLED = (not CORE_WORKER_APK_REPLACES_TERMUX) and _parse_bool(os.getenv("WORKER_VOICE_AGENT_ENABLED", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED", "true"), True)
WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS = max(16, _parse_int(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS", "600"), 600))
WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS = max(3.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_TIMEOUT_SECONDS", "30"), 30.0))
WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_DIRECT_TTS_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", "true"), True)
WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_ENABLED", "true"), True)
WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_SESSION_TTL_SECONDS = max(30.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_TTL_SECONDS", "180"), 180.0))
WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS = max(3.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_SESSION_REPORT_MIN_INTERVAL_SECONDS", "15"), 15.0))
WORKER_VOICE_AGENT_HANDOFF_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_HANDOFF_ENABLED", "true"), True)
WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS = max(10.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_HANDOFF_TTL_SECONDS", "60"), 60.0))
WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_HANDOFF_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_TRANSFER_CONTROL_ENABLED", "true"), True)
WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_TRANSFER_PREPARE_ENABLED", "true"), True)
WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_TRANSFER_TIMEOUT_SECONDS", "1.5"), 1.5))
WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS = max(10.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_TRANSFER_LEASE_TTL_SECONDS", "45"), 45.0))
# Dry-run de conexão continua disponível para diagnóstico/manual, mas não deve abrir
# uma segunda conexão Voice WS/UDP automaticamente enquanto a VPS é dona da voz.
WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_CONNECTION_DRY_RUN_ENABLED", "true"), True)
WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED = _parse_bool(os.getenv("WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED", "false"), False)
WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS = max(1.0, _parse_float(os.getenv("WORKER_VOICE_AGENT_CONNECTION_TIMEOUT_SECONDS", "4.0"), 4.0))
WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS = max(0.6, _parse_float(os.getenv("WORKER_VOICE_AGENT_CONNECTION_REPORT_TIMEOUT_SECONDS", "1.5"), 1.5))

# Texto longo começa a tocar mais rápido: divide em blocos naturais e enfileira
# partes menores, permitindo prefetch do próximo áudio durante o playback atual.
TTS_LONG_TEXT_CHUNK_ENABLED = _parse_bool(os.getenv("TTS_LONG_TEXT_CHUNK_ENABLED", "true"), True)
TTS_LONG_TEXT_CHUNK_MAX_CHARS = max(160, _parse_int(os.getenv("TTS_LONG_TEXT_CHUNK_MAX_CHARS", "420"), 420))
TTS_LONG_TEXT_CHUNK_MAX_PARTS = max(1, _parse_int(os.getenv("TTS_LONG_TEXT_CHUNK_MAX_PARTS", "8"), 8))
# Índice do cache local é verificado periodicamente, em vez de varrer todos os
# arquivos após cada fala.
TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS = max(5.0, _parse_float(os.getenv("TTS_CACHE_INDEX_SWEEP_INTERVAL_SECONDS", "30"), 30.0))
TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES = max(4, _parse_int(os.getenv("TTS_CACHE_INDEX_SWEEP_MAX_ENTRIES", "32"), 32))

# Uso do phone-worker fora do /vps: preparação de áudio TTS para Lavalink.
# A VPS sempre mantém fallback local.
MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED = _parse_bool(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_ENABLED", "true"), True)
MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS = max(0.8, _parse_float(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_TIMEOUT_SECONDS", "3.5"), 3.5))
MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB = max(1, _parse_int(os.getenv("MUSIC_TTS_PHONE_WORKER_CONVERT_MAX_MB", "8"), 8))

# Node de áudio compatível com Lavalink API.
# Qualquer valor legado de MUSIC_NODE_PROVIDER cai para Lavalink.
_MUSIC_NODE_PROVIDER_RAW = (os.getenv("MUSIC_NODE_PROVIDER", "lavalink") or "lavalink").strip().lower()
MUSIC_NODE_PROVIDER = _MUSIC_NODE_PROVIDER_RAW if _MUSIC_NODE_PROVIDER_RAW in {"lavalink", "auto"} else "lavalink"
AUDIO_NODE_FAILURE_COOLDOWN_SECONDS = max(5.0, _parse_float(os.getenv("AUDIO_NODE_FAILURE_COOLDOWN_SECONDS", "45"), 45.0))
AUDIO_NODE_STARTUP_WAIT_SECONDS = max(0.0, _parse_float(os.getenv("AUDIO_NODE_STARTUP_WAIT_SECONDS", "90"), 90.0))
AUDIO_NODE_STARTUP_WAIT_REQUIRED = _parse_bool(os.getenv("AUDIO_NODE_STARTUP_WAIT_REQUIRED", "true"), True)

# -----------------------------------------------------------------------------
# Music/TTS recovery defaults
# -----------------------------------------------------------------------------

MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MIN_SECONDS", "45"))
MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS = float(os.getenv("MUSIC_LAVALINK_PREMATURE_END_REMAINING_SECONDS", "35"))
MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES = int(os.getenv("MUSIC_LAVALINK_PREMATURE_END_MAX_RECOVERIES", "1"))
MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS = float(os.getenv("MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS", "18"))
MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS = float(os.getenv("MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS", "1.5"))
MUSIC_RESOLVING_STALE_SECONDS = float(os.getenv("MUSIC_RESOLVING_STALE_SECONDS", "45"))
