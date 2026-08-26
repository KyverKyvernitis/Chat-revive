"""System prompt "mestre" — instruções globais aplicadas antes das
personalidades dos profiles.

Modelo:
- 1 doc com `type="chatbot_master"` na coleção dedicada do chatbot.
- Doc tem 2 campos principais:
    * `prompt`: texto das instruções globais
    * `config_guild_id`: qual guild é o "server de configuração" que pode
      editar. Nenhum outro server pode mexer.

Por que 1 doc só?
- O master prompt é global — mesmo texto aplicado a TODOS os profiles em
  TODOS os servers onde o bot está. Isso permite ao dono do bot garantir
  segurança básica e qualidade (anti-repetição, tom, etc) sem depender de
  cada staff saber escrever prompt.

Segurança:
- Só membros com Manage Guild do `config_guild_id` atual conseguem editar.
- Transferência e reset global são autorizados separadamente pelo dono do bot
  ou pelos IDs em `CHATBOT_OPERATOR_IDS`; o store não decide autorização.
- Bootstrap: se o doc não existir, usa `DEFAULT_MASTER_PROMPT` hardcoded
  e `DEFAULT_MASTER_CONFIG_GUILD_ID` como dono inicial.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import constants as C

log = logging.getLogger(__name__)


@dataclass
class MasterPrompt:
    prompt: str
    config_guild_id: int
    updated_at: float = field(default_factory=time.time)
    updated_by: int = 0  # user_id da última edição

    @classmethod
    def default(cls) -> "MasterPrompt":
        """Valor usado quando o doc ainda não existe no banco."""
        return cls(
            prompt=C.DEFAULT_MASTER_PROMPT,
            config_guild_id=C.DEFAULT_MASTER_CONFIG_GUILD_ID,
            updated_at=0.0,  # 0 sinaliza "nunca editado, está no default"
            updated_by=0,
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_MASTER,
            "prompt": self.prompt,
            "config_guild_id": int(self.config_guild_id),
            "updated_at": self.updated_at,
            "updated_by": int(self.updated_by),
        }

    @classmethod
    def from_doc(cls, doc: dict) -> "MasterPrompt":
        return cls(
            prompt=str(doc.get("prompt") or C.DEFAULT_MASTER_PROMPT),
            config_guild_id=int(doc.get("config_guild_id") or C.DEFAULT_MASTER_CONFIG_GUILD_ID),
            updated_at=float(doc.get("updated_at") or 0.0),
            updated_by=int(doc.get("updated_by") or 0),
        )

    @property
    def is_default(self) -> bool:
        """True se ainda é o prompt padrão (nunca editado)."""
        return self.updated_at == 0.0


class MasterPromptStore:
    """Camada de persistência do master prompt.

    Cache em RAM simples — o doc muda raramente (staff manual), então
    cachear economiza 1 Mongo read por mensagem do bot em todos os servers.
    TTL de 60s pra propagar edições rapidamente sem ser instantâneo.
    """

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._cached: Optional[MasterPrompt] = None
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 60.0

    async def get(self) -> MasterPrompt:
        """Retorna o master prompt atual. Usa cache de 60s."""
        now = time.time()
        if self._cached is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cached

        doc = await self._coll.find_one({"type": C.DOC_TYPE_MASTER})
        mp = MasterPrompt.from_doc(doc) if doc else MasterPrompt.default()

        self._cached = mp
        self._cache_ts = now
        return mp

    def invalidate_cache(self) -> None:
        """Força refresh no próximo get() — chamar após update."""
        self._cached = None
        self._cache_ts = 0.0

    async def update_prompt(
        self, new_prompt: str, *, updated_by: int
    ) -> MasterPrompt:
        """Atualiza só o texto do prompt. config_guild_id permanece.

        Caller já deve ter validado: (a) editor tem permissão, (b) texto
        dentro do limite. Aqui aplicamos o cap defensivamente mesmo.
        """
        safe = (new_prompt or "").strip()[:C.MAX_MASTER_PROMPT_LENGTH]
        if not safe:
            safe = C.DEFAULT_MASTER_PROMPT  # proteção contra reset acidental
        now = time.time()

        await self._coll.update_one(
            {"type": C.DOC_TYPE_MASTER},
            {
                "$set": {
                    "prompt": safe,
                    "updated_at": now,
                    "updated_by": int(updated_by),
                },
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MASTER,
                    "config_guild_id": int(C.DEFAULT_MASTER_CONFIG_GUILD_ID),
                },
            },
            upsert=True,
        )
        self.invalidate_cache()
        return await self.get()

    async def set_config_guild(
        self, new_guild_id: int, *, updated_by: int
    ) -> MasterPrompt:
        """Transfere o "server de configuração" pra outro guild.

        Use com cuidado — depois que mudar, quem está no novo server é
        quem pode editar o master. Caller DEVE validar que o editor atual
        tem permissão (ver `can_edit` no commands).
        """
        now = time.time()
        await self._coll.update_one(
            {"type": C.DOC_TYPE_MASTER},
            {
                "$set": {
                    "config_guild_id": int(new_guild_id),
                    "updated_at": now,
                    "updated_by": int(updated_by),
                },
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MASTER,
                    "prompt": C.DEFAULT_MASTER_PROMPT,
                },
            },
            upsert=True,
        )
        self.invalidate_cache()
        return await self.get()

    async def can_edit(self, member_guild_id: int) -> bool:
        """True se o membro no `member_guild_id` pode editar o master.
        Apenas staff do `config_guild_id` atual pode."""
        mp = await self.get()
        return int(member_guild_id) == mp.config_guild_id
