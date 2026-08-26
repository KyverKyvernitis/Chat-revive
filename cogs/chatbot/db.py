"""Acesso ao banco de dados do chatbot.

Usamos uma coleção DEDICADA (`chatbot_data`) no mesmo database do bot, ao
invés de compartilhar a `settings` que já tem índices UNIQUE sobre
`(guild_id, user_id, type)` incompatíveis com profiles (user_id=null em
profile, e múltiplos profiles por guild violariam).

Índices criados aqui:
- `(type, guild_id)` — suporta list_profiles, get_active_profile, list de memória
- `(type, guild_id, profile_id)` UNIQUE quando type=chatbot_profile —
  garante que profile_id não duplica dentro do guild
- `(type, guild_id, scope, profile_id, user_id)` sparse — busca de memória
  pessoal por profile

Índices NÃO-UNIQUE são seguros (não disparam erro ao inserir). O UNIQUE em
profile_id é só pra integridade de dados, nunca conflita com outros cogs.

O `ensure_indexes()` é idempotente — chamado no `cog_load`. Se os índices
já existem, a call é no-op.
"""
from __future__ import annotations

import logging

from . import constants as C

log = logging.getLogger(__name__)


def get_chatbot_collection(settings_db):
    """Retorna o Motor AsyncIOMotorCollection dedicada ao chatbot.

    `settings_db` é o SettingsDB do bot. Usamos apenas `.db` (database obj)
    pra acessar nossa coleção isolada. Zero risco de colidir com outros cogs.
    """
    if settings_db is None or not hasattr(settings_db, "db"):
        return None
    return settings_db.db[C.CHATBOT_COLLECTION_NAME]


async def ensure_indexes(coll) -> None:
    """Cria índices necessários. Idempotente.

    Separamos em 2 funções (esta + get_chatbot_collection) pra poder testar
    cada uma isolada, e pra o setup do cog falhar graciosamente se o índice
    não puder ser criado por algum motivo (log warning, sistema continua).
    """
    if coll is None:
        return

    specs = [
        (
            [("type", 1), ("guild_id", 1)],
            {"name": "type_1_guild_id_1"},
        ),
        (
            [("type", 1), ("guild_id", 1), ("profile_id", 1)],
            {
                "name": "chatbot_profile_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_PROFILE},
            },
        ),
        # Legado: não-unique de propósito. A memória V1 deixa de ser lida, mas
        # o índice continua ajudando comandos de limpeza/rollback.
        (
            [("type", 1), ("guild_id", 1), ("scope", 1),
             ("profile_id", 1), ("user_id", 1)],
            {"name": "chatbot_memory_lookup", "sparse": True},
        ),
        (
            [
                ("type", 1), ("scope", 1), ("guild_id", 1),
                ("profile_id", 1), ("profile_revision", 1),
                ("channel_id", 1), ("visibility_scope", 1),
                ("global_generation", 1), ("guild_generation", 1),
                ("user_id", 1), ("user_generation", 1),
            ],
            {
                "name": "chatbot_memory_v2_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MEMORY_V2},
            },
        ),
        (
            [("type", 1), ("epoch_key", 1)],
            {
                "name": "chatbot_memory_epoch_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MEMORY_EPOCH},
            },
        ),
        (
            [("type", 1), ("guild_id", 1)],
            {
                "name": "chatbot_guild_config_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_GUILD_CONFIG},
            },
        ),
        (
            [("type", 1)],
            {
                "name": "chatbot_master_singleton",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MASTER},
            },
        ),
        (
            [("type", 1), ("message_id", 1)],
            {
                "name": "chatbot_msg_map_lookup",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MESSAGE_MAP},
            },
        ),
        (
            [("expires_at", 1)],
            {
                "name": "chatbot_msg_map_ttl",
                "expireAfterSeconds": 0,
                "partialFilterExpression": {"type": C.DOC_TYPE_MESSAGE_MAP},
            },
        ),
        (
            [("type", 1), ("guild_id", 1)],
            {
                "name": "chatbot_extrovert_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_EXTROVERT},
            },
        ),
        (
            [("type", 1), ("channel_id", 1)],
            {
                "name": "chatbot_webhook_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_WEBHOOK},
            },
        ),
        (
            [("type", 1), ("migration_id", 1)],
            {
                "name": "chatbot_migration_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MIGRATION},
            },
        ),
    ]

    failures = 0
    for keys, kwargs in specs:
        try:
            await coll.create_index(keys, **kwargs)
        except Exception:
            failures += 1
            # Cada índice é isolado: um legado duplicado não impede os índices
            # V2 e TTL seguintes de serem criados.
            log.exception("chatbot: falha ao criar índice %s", kwargs.get("name"))

    if failures:
        log.warning("chatbot: índices verificados com %s falha(s)", failures)
    else:
        log.info("chatbot: índices do %s verificados", C.CHATBOT_COLLECTION_NAME)
