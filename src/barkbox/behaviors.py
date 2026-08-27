"""Behavior model — *why* the dog barks this time.

Each cycle the scheduler picks one behavior (alert, response, chase,
noise_reaction, idle, ...) by weighted random choice. The weight is the
behavior's base ``weight`` scaled by its ``time_weights`` entry for the current
day-part, so the mix shifts naturally across the day.
"""

from __future__ import annotations

import random

__all__ = ["effective_weights", "pick_behavior", "episode_params"]


def effective_weights(behaviors_cfg: dict, day_part: str) -> dict[str, float]:
    """``{behavior_key: weight * time_weight}`` for every enabled behavior."""
    out: dict[str, float] = {}
    for key, b in behaviors_cfg.items():
        if not b.get("enabled"):
            continue
        base = float(b.get("weight", 0.0))
        tw = float(b.get("time_weights", {}).get(day_part, 1.0))
        out[key] = base * tw
    return out


def pick_behavior(behaviors_cfg: dict, day_part: str, rng: random.Random) -> str:
    """Return one behavior key, chosen in proportion to its effective weight.

    If the day-part zeroes out every behavior, falls back to a uniform pick among
    the enabled ones.
    """
    weights = effective_weights(behaviors_cfg, day_part)
    if not weights:
        raise ValueError("no enabled behaviors to choose from")
    keys = list(weights)
    vals = [weights[k] for k in keys]
    if sum(vals) <= 0:
        return rng.choice(keys)
    return rng.choices(keys, weights=vals, k=1)[0]


def episode_params(behaviors_cfg: dict, behavior_key: str) -> dict:
    """Episode shape for the chosen behavior.

    ``episode_duration_seconds`` is the target the episode plays toward (a random
    value in that range is drawn per episode); ``max_barks`` is only an upper
    safety bound. A behavior without ``episode_duration_seconds`` (e.g. idle)
    plays a single bark.
    """
    b = behaviors_cfg[behavior_key]
    dur = b.get("episode_duration_seconds")
    return {
        "max_barks": int(b["max_barks"]),
        "episode_duration_seconds": list(dur) if dur else None,
        "intra_gap_seconds": list(b["intra_gap_seconds"]),
        "clip_tags": list(b["clip_tags"]),
    }
