from .commands.admin import GincanaCommandMixin
from .commands.chip_admin import GincanaChipAdminMixin
from .games.alvo import GincanaAlvoMixin
from .games.buckshot import GincanaBuckshotMixin
from .games.corrida import GincanaCorridaMixin
from .games.poker import GincanaPokerMixin
from .games.roleta import GincanaRoletaMixin
from .games.slots import GincanaSlotsMixin
from .games.truco import GincanaTrucoMixin
from .handlers.focus import GincanaFocusMixin
from .handlers.message_router import GincanaMessageRouterMixin
from .services.audio import GincanaAudioMixin
from .services.payments import GincanaPaymentMixin
from .services.sinuca_service import GincanaSinucaMixin
from .services.base import GincanaBase
from .services.toggles import GincanaToggleMixin


class GamesCore(
    GincanaCommandMixin,
    GincanaChipAdminMixin,
    GincanaFocusMixin,
    GincanaPokerMixin,
    GincanaMessageRouterMixin,
    GincanaSlotsMixin,
    GincanaRoletaMixin,
    GincanaTrucoMixin,
    GincanaBuckshotMixin,
    GincanaCorridaMixin,
    GincanaAlvoMixin,
    GincanaToggleMixin,
    GincanaPaymentMixin,
    GincanaSinucaMixin,
    GincanaAudioMixin,
    GincanaBase,
):
    pass
