"""In-memory event log — the last N things the system did.

Cheap enough for a Pi 3B: a bounded deque, no file, no database. Every entry is
also emitted to :mod:`logging` so ``journalctl -u barkbox`` shows the same story.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import deque
from typing import Any

logger = logging.getLogger("barkbox.events")

# Known event kinds (free-form strings are still accepted; this is documentation):
#   startup, played:<behavior>, alarm_triggered, test_bark,
#   skipped_disabled, skipped_schedule, config_changed, presence_changed
DEFAULT_CAPACITY = 50


class EventLog:
    """Thread-safe ring buffer of recent events."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, kind: str, detail: str = "") -> dict[str, Any]:
        """Record an event and log it. Returns the stored entry."""
        entry = {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "detail": detail,
        }
        with self._lock:
            self._events.append(entry)
        if detail:
            logger.info("%s %s", kind, detail)
        else:
            logger.info("%s", kind)
        return entry

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return recent events, newest first."""
        with self._lock:
            items = list(self._events)
        items.reverse()
        return items[:limit] if limit else items

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
