import threading
import time


class RateLimiter:
    def __init__(self, min_interval_ms: int = 0) -> None:
        self.min_interval_ms = max(0, int(min_interval_ms))
        self._lock = threading.Lock()
        self._last_time = 0.0

    def wait(self) -> None:
        if self.min_interval_ms <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed_ms = (now - self._last_time) * 1000
            if elapsed_ms < self.min_interval_ms:
                time.sleep((self.min_interval_ms - elapsed_ms) / 1000)
            self._last_time = time.monotonic()
