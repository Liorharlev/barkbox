"""Per-bark playback gain for scheduled episodes.

Two independent controls combine multiplicatively into the 0.0-1.0 gain handed
to the player:

* **master volume** — ``audio.master_volume`` (0-100), a global level for
  *scheduled* playback set from the web UI in place of a knob on the speaker.
  Manual test barks ignore it and always play at full volume.
* **distance simulation** — in an episode long enough to imply the dog is
  moving around (its target duration exceeds
  ``distance_simulation.min_episode_duration_seconds``), each bark's volume
  drifts from the previous bark's by a bounded random step — a random walk
  inside ``volume_range``. Shorter episodes play every bark at full volume
  (the dog is standing in one place).

::

    final_gain = (master_volume / 100) * (distance_volume / 100)

where ``distance_volume`` is 100 whenever the walk is not active.
"""

from __future__ import annotations

import random

__all__ = ["DistanceWalk", "master_scale", "make_distance_walk"]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def master_scale(audio_cfg: dict) -> float:
    """``audio.master_volume`` as a 0.0-1.0 multiplier (default: full)."""
    try:
        mv = float(audio_cfg.get("master_volume", 100))
    except (TypeError, ValueError):
        return 1.0
    return _clamp(mv / 100.0, 0.0, 1.0)


class DistanceWalk:
    """A bounded random walk over *percent* volume — one step per bark.

    The first :meth:`next_scale` returns the start volume unchanged; each later
    call moves it by ``uniform(-max_step, +max_step)`` and clamps back into
    ``[lo, hi]``.
    """

    def __init__(
        self,
        lo: float,
        hi: float,
        max_step: float,
        start: float,
        rng: random.Random,
    ) -> None:
        self.lo = lo
        self.hi = hi
        self.max_step = max_step
        self._rng = rng
        self.current = _clamp(start, lo, hi)
        self._first = True

    def next_scale(self) -> float:
        """The next bark's distance volume as a 0.0-1.0 multiplier."""
        if self._first:
            self._first = False
        else:
            self.current = _clamp(
                self.current + self._rng.uniform(-self.max_step, self.max_step),
                self.lo,
                self.hi,
            )
        return self.current / 100.0


def make_distance_walk(
    sim_cfg: dict | None,
    target_duration: float | None,
    rng: random.Random,
) -> DistanceWalk | None:
    """Build a walk for one episode, or ``None`` if its volume shouldn't drift.

    Returns ``None`` when the simulation is disabled, the episode has no target
    duration (a single-bark behaviour), or that target does not exceed
    ``min_episode_duration_seconds``.
    """
    if not sim_cfg or not sim_cfg.get("enabled", False):
        return None
    if target_duration is None:
        return None
    if target_duration <= float(sim_cfg.get("min_episode_duration_seconds", 6)):
        return None
    lo, hi = sim_cfg.get("volume_range", (40, 100))
    lo, hi = float(lo), float(hi)
    max_step = float(sim_cfg.get("max_step", 20))
    start = sim_cfg.get("start_volume")
    start = rng.uniform(lo, hi) if start is None else float(start)
    return DistanceWalk(lo, hi, max_step, start, rng)
