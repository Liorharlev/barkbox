"""The scheduling loop: decide *when* something happens, pick a behavior, play it.

Three independent gates, checked every cycle:

1. ``enabled``            — master switch; off means total silence.
2. ``schedule``           — is the system active at this time of day at all?
3. ``presence`` multiplier — how much activity (home = quiet, away = full).

Alarm mode is an external override that pre-empts all three.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
from pathlib import Path

from .behaviors import episode_params, pick_behavior
from .clips import load_clips, load_tags, resolve_clips
from .config import get_day_part, is_within_schedule

logger = logging.getLogger("barkbox.scheduler")

# How long to sleep before re-checking when a gate is closed (disabled / off
# schedule / presence multiplier of 0). Kept short so the UI feels responsive
# even without a wake, and it is wake-interruptible anyway.
_IDLE_RECHECK_SECONDS = 60.0


def _wait(stop_event: threading.Event, wake_event: threading.Event, seconds: float) -> str:
    """Sleep up to ``seconds``. Returns ``"stop"``, ``"wake"`` or ``"timeout"``.

    ``stop_event`` wins over ``wake_event`` if both are set.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if stop_event.is_set():
            return "stop"
        if wake_event.is_set():
            return "wake"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        wake_event.wait(min(remaining, 1.0))


def pick_clip(
    clips: list[Path],
    recent: list[str],
    no_repeat_last: int,
    rng: random.Random,
) -> Path:
    """Choose a clip, avoiding the last ``no_repeat_last`` played (by filename)."""
    if not clips:
        raise ValueError("no clips to choose from")
    banned = set(recent[-no_repeat_last:]) if no_repeat_last else set()
    pool = [c for c in clips if c.name not in banned] or list(clips)
    return rng.choice(pool)


def _gap_seconds(cfg: dict, presence_mode: str, rng: random.Random) -> float | None:
    """Seconds until the next episode, or ``None`` if presence says 'stay quiet'.

    The presence value is an activity-rate multiplier: a smaller number means a
    longer gap, so we divide.
    """
    preset = cfg["timing"]["frequency_presets"][cfg["timing"]["frequency"]]
    base_min = rng.uniform(preset["min_gap_minutes"], preset["max_gap_minutes"])
    mult = float(cfg["presence"]["multipliers"].get(presence_mode, 1.0))
    if mult <= 0:
        return None
    return base_min * 60.0 / mult


def _run_episode(
    behavior: str,
    params: dict,
    all_clips: list[Path],
    tags: dict[str, list[str]],
    no_repeat_last: int,
    player,
    state,
    events,
    rng: random.Random,
    stop_event: threading.Event,
) -> None:
    clips = resolve_clips(all_clips, tags, params["clip_tags"])
    if not clips:
        events.add("skipped_no_clips", behavior)
        return
    lo, hi = params["episode_barks"]
    n = rng.randint(int(lo), int(hi))
    g_lo, g_hi = params["intra_gap_seconds"]
    for i in range(n):
        if stop_event.is_set():
            return
        clip = pick_clip(clips, state.recent_clips, no_repeat_last, rng)
        player.play(clip)
        state.note_played(clip.name)
        if i < n - 1 and g_hi > 0:
            if stop_event.wait(rng.uniform(g_lo, g_hi)):
                return
    events.add(f"played:{behavior}", f"{n} bark(s)")


def run(stop_event, get_config, state, events, player, rng: random.Random | None = None) -> None:
    """Main scheduler loop. Runs until ``stop_event`` is set."""
    rng = rng or random.Random()
    wake = state.wake_event
    events.add("startup")
    last_skip: str | None = None

    while not stop_event.is_set():
        try:
            cfg = get_config()
        except Exception:  # keep the loop alive on a bad edit
            logger.exception("could not load config; retrying")
            if _wait(stop_event, wake, _IDLE_RECHECK_SECONDS) == "stop":
                break
            wake.clear()
            continue

        now = dt.datetime.now()
        sounds_dir = cfg["paths"]["sounds_dir"]
        all_clips = load_clips(sounds_dir)
        tags = load_tags(cfg["paths"]["tags_file"])
        no_repeat_last = cfg["anti_repeat"]["no_repeat_last"]

        # --- alarm override -------------------------------------------------
        if state.alarm_active(now):
            if last_skip != "alarm":
                events.add("alarm_triggered", f"until {state.alarm_until:%H:%M:%S}")
                last_skip = "alarm"
            alarm = cfg["alarm"]
            _run_episode(
                "alarm",
                {
                    "episode_barks": alarm["episode_barks"],
                    "intra_gap_seconds": alarm["episode_gap_seconds"],
                    "clip_tags": [],
                },
                all_clips, tags, no_repeat_last, player, state, events, rng, stop_event,
            )
            gap = rng.uniform(*alarm["episode_gap_seconds"])
            if _wait(stop_event, wake, gap) == "stop":
                break
            wake.clear()
            continue
        if last_skip == "alarm":
            state.clear_alarm_mode()
            last_skip = None

        # --- gate 1: master switch ---------------------------------------
        if not cfg["enabled"]:
            if last_skip != "disabled":
                events.add("skipped_disabled")
                last_skip = "disabled"
            state.set_next_event_at(None)
            if _wait(stop_event, wake, _IDLE_RECHECK_SECONDS) == "stop":
                break
            wake.clear()
            continue

        # --- gate 2: schedule window -----------------------------------
        if not is_within_schedule(now, cfg["schedule"]):
            if last_skip != "schedule":
                events.add("skipped_schedule", cfg["schedule"]["mode"])
                last_skip = "schedule"
            state.set_next_event_at(None)
            if _wait(stop_event, wake, _IDLE_RECHECK_SECONDS) == "stop":
                break
            wake.clear()
            continue

        # --- gate 3: presence multiplier -------------------------------
        gap = _gap_seconds(cfg, state.presence_mode, rng)
        if gap is None:
            if last_skip != "presence":
                events.add("skipped_presence", state.presence_mode)
                last_skip = "presence"
            state.set_next_event_at(None)
            if _wait(stop_event, wake, _IDLE_RECHECK_SECONDS) == "stop":
                break
            wake.clear()
            continue

        last_skip = None
        state.set_next_event_at(now + dt.timedelta(seconds=gap))
        result = _wait(stop_event, wake, gap)
        if result == "stop":
            break
        if result == "wake":
            wake.clear()
            continue

        # --- fire -----------------------------------------------------------
        day_part = get_day_part(dt.datetime.now(), cfg["timing"]["day_parts"])
        behavior = pick_behavior(cfg["behaviors"], day_part, rng)
        params = episode_params(cfg["behaviors"], behavior)
        _run_episode(
            behavior, params, all_clips, tags, no_repeat_last,
            player, state, events, rng, stop_event,
        )

    logger.info("scheduler stopped")
