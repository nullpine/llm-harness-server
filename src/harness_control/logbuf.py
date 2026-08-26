"""A bounded ring buffer of upstream output, drained by `GET /admin/logs`.

Bounded because a chatty model on a long-running server would otherwise be an
unbounded memory leak. 2000 lines is SPEC §5.1; the route caps its answer at 1000.
"""

import threading
from collections import deque

DEFAULT_CAPACITY = 2000
MAX_REQUESTABLE_LINES = 1000


class LogBuffer:
    """Thread-safe because the writer is an asyncio task and readers are requests."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._lines.maxlen or 0

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def extend(self, lines: list[str]) -> None:
        with self._lock:
            self._lines.extend(lines)

    def tail(self, lines: int) -> list[str]:
        """The last `lines` entries, oldest first. Capped at `MAX_REQUESTABLE_LINES`."""
        count = max(0, min(lines, MAX_REQUESTABLE_LINES))
        with self._lock:
            if count == 0:
                return []
            return list(self._lines)[-count:]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)
