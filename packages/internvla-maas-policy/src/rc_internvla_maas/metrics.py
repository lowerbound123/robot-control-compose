"""Lifetime observation-request statistics; snapshots never touch transport."""
import heapq
from threading import Lock
from time import monotonic


class RequestMetrics:
    def __init__(self):
        self._lock = Lock()
        self._started = None
        self._count = self._failed = 0
        self._mean = self._last = 0.
        # Exact running median: largest lower value and smallest upper value.
        self._lower, self._upper = [], []

    def start(self):
        with self._lock:
            self._started = monotonic()

    def cancel(self):
        with self._lock:
            self._started = None

    def finish(self, *, failed):
        with self._lock:
            if self._started is None:
                return  # Pre-send failure or already accounted timeout.
            elapsed = (monotonic() - self._started) * 1000.
            self._started = None
            self._count += 1
            self._failed += int(failed)
            self._last = elapsed
            self._mean += (elapsed - self._mean) / self._count
            heapq.heappush(self._lower, -elapsed)
            heapq.heappush(self._upper, -heapq.heappop(self._lower))
            if len(self._upper) > len(self._lower):
                heapq.heappush(self._lower, -heapq.heappop(self._upper))

    def snapshot(self):
        with self._lock:
            median = '—'
            if self._count:
                median = -self._lower[0]
                if len(self._lower) == len(self._upper):
                    median = (median + self._upper[0]) / 2.
            return {'avg_latency_ms': self._mean if self._count else '—',
                    'median_latency_ms': median,
                    'last_latency_ms': self._last if self._count else '—',
                    'failed_requests': self._failed}
