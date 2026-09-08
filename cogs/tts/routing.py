"""Recent, comparable routing measurements. No health I/O on speech admission."""
from collections import OrderedDict, deque
from statistics import median
import time

class RouteMeasurements:
    def __init__(self, *, ttl=300.0, samples=32, buckets=96):
        self.ttl, self.samples, self.buckets = ttl, samples, buckets
        self.values = OrderedDict()
        self.decisions = {}

    @staticmethod
    def size_bucket(text):
        size = len(text)
        return 0 if size <= 80 else 1 if size <= 240 else 2 if size <= 700 else 3

    def record(self, engine, text, route, phase, milliseconds, *, cached=False, now=None):
        if milliseconds is None or milliseconds < 0:
            return
        now = time.monotonic() if now is None else now
        key = (engine, self.size_bucket(text), route, phase, bool(cached))
        rows = self.values.setdefault(key, deque(maxlen=self.samples))
        rows.append((now, float(milliseconds)))
        self.values.move_to_end(key)
        while len(self.values) > self.buckets:
            self.values.popitem(last=False)

    def estimate(self, engine, text, route, phase='first_audio', *, cached=False, now=None):
        now = time.monotonic() if now is None else now
        rows = self.values.get((engine, self.size_bucket(text), route, phase, bool(cached)))
        if not rows:
            return None
        while rows and now - rows[0][0] > self.ttl:
            rows.popleft()
        return median(value for _, value in rows) if len(rows) >= 3 else None

    def exploration_due(self, engine, text):
        key = (engine, self.size_bucket(text))
        if len(self.decisions) >= 32 and key not in self.decisions:
            self.decisions.pop(next(iter(self.decisions)))
        self.decisions[key] = self.decisions.get(key, 0) + 1
        return self.decisions[key] % 8 == 0
