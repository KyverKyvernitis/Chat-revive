"""Runtime separado do CallKeeper.

Este pacote é usado pelo serviço standalone `callkeeper_service.py` e pela cog
`cogs.call_keeper` apenas para compartilhar configuração/estado. A lógica de
voz fica fora do bot principal para que falhas fatais em outras cogs não derrubem
os bots auxiliares.
"""

from .settings import CALLKEEPER_OWNER_USER_ID, CallKeeperSettings, load_settings
from .store import CallKeeperStateStore
from .runtime import CallKeeperRuntime
from .rescue_cmd import CallKeeperRescueCommandService

__all__ = [
    "CALLKEEPER_OWNER_USER_ID",
    "CallKeeperSettings",
    "load_settings",
    "CallKeeperStateStore",
    "CallKeeperRuntime",
    "CallKeeperRescueCommandService",
]
