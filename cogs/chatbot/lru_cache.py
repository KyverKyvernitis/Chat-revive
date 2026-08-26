"""Cache LRU com TTL — dict + deque, zero deps.

Usado pra 3 caches do chatbot (webhooks, profile ativo, profile data).
Cada entrada expira em TTL segundos, e se o cache atingir o limite de
entradas, evicta a mais antiga.

Trade-off: operações O(n) para o eviction de TTL na leitura. Para os
tamanhos que usamos (<200 entradas), isso é negligível. Para caches maiores,
trocaria por cachetools.TTLCache — mas evitamos essa dep.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LRUCacheTTL(Generic[K, V]):
    """Cache LRU + TTL.

    - `max_entries`: quando estoura, remove a entrada menos recentemente usada.
    - `ttl_seconds`: entradas mais antigas que isso são tratadas como miss.
    """

    __slots__ = ("_data", "_ttl", "_max")

    def __init__(self, *, max_entries: int, ttl_seconds: float):
        # OrderedDict preserva ordem de inserção; move_to_end promove LRU→MRU
        self._data: "OrderedDict[K, tuple[float, V]]" = OrderedDict()
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)

    def get(self, key: K) -> Optional[V]:
        entry = self._data.get(key)
        if entry is None:
            return None
        inserted_at, value = entry
        if time.monotonic() - inserted_at > self._ttl:
            # Expirou — remove e retorna miss
            self._data.pop(key, None)
            return None
        # Promove pra fim (MRU)
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        now = time.monotonic()
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (now, value)
        # Evict LRU se estourou
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def pop(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: K) -> bool:
        # Respeita TTL
        return self.get(key) is not None
