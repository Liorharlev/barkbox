"""Shared runtime state between the scheduler thread and the Flask thread."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

_RECENT_CLIPS_CAP = 20


class AppState:
    """Mutable runtime state, guarded by a re-entrant lock.

    ``wake_event`` is separate from the scheduler's ``stop_event``: setting it
    makes the scheduler abandon its current sleep and recompute everything with
    fresh config. Any setting change (enabled / schedule / presence / frequency /
    behaviors) and any alarm trigger calls :meth:`request_wake`.
    """

    def __init__(self, presence_mode: str = "away") -> None:
        self._lock = threading.RLock()
        self.wake_event = threading.Event()
        self._recent_clips: list[str] = []
        self._alarm_until: dt.datetime | None = None
        self._next_event_at: dt.datetime | None = None
        self._last_event: dict[str, Any] | None = None
        self._now_playing: str | None = None
        self._presence_mode = presence_mode

    # -- wake -----------------------------------------------------------------
    def request_wake(self) -> None:
        self.wake_event.set()

    # -- anti-repeat --------------------------------------------------------
    def note_played(self, clip: str) -> None:
        with self._lock:
            self._recent_clips.append(clip)
            del self._recent_clips[:-_RECENT_CLIPS_CAP]
            self._last_event = {
                "clip": clip,
                "time": dt.datetime.now().isoformat(timespec="seconds"),
            }

    @property
    def recent_clips(self) -> list[str]:
        with self._lock:
            return list(self._recent_clips)

    # -- currently playing -------------------------------------------------
    def set_now_playing(self, behavior: str | None) -> None:
        """Mark which behavior's episode is playing right now (``None`` = silent).

        Set at the start of every episode — scheduled, alarm or a manual test
        bark — and cleared when it finishes (after the last bark, not the first).
        """
        with self._lock:
            self._now_playing = behavior

    @property
    def now_playing(self) -> str | None:
        with self._lock:
            return self._now_playing

    # -- alarm mode --------------------------------------------------------
    def enter_alarm_mode(self, duration_minutes: float) -> None:
        with self._lock:
            self._alarm_until = dt.datetime.now() + dt.timedelta(minutes=duration_minutes)
        self.request_wake()

    def clear_alarm_mode(self) -> None:
        with self._lock:
            self._alarm_until = None

    def alarm_active(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now()
        with self._lock:
            return self._alarm_until is not None and now < self._alarm_until

    @property
    def alarm_until(self) -> dt.datetime | None:
        with self._lock:
            return self._alarm_until

    # -- presence --------------------------------------------------------
    @property
    def presence_mode(self) -> str:
        with self._lock:
            return self._presence_mode

    def set_presence(self, mode: str) -> None:
        with self._lock:
            self._presence_mode = mode

    # -- scheduling display --------------------------------------------------
    def set_next_event_at(self, when: dt.datetime | None) -> None:
        with self._lock:
            self._next_event_at = when

    @property
    def next_event_at(self) -> dt.datetime | None:
        with self._lock:
            return self._next_event_at

    @property
    def last_event(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_event) if self._last_event else None
