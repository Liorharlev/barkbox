import copy
import random

import pytest

from barkbox.behaviors import effective_weights, episode_params, pick_behavior
from barkbox.config import DEFAULTS

BEHAVIORS = DEFAULTS["behaviors"]


def test_effective_weights_scales_by_day_part():
    w = effective_weights(BEHAVIORS, "night")
    # alert: 1.0 * 1.5, response: 2.0 * 0.6
    assert w["alert"] == pytest.approx(1.5)
    assert w["response"] == pytest.approx(1.2)


def test_effective_weights_skips_disabled():
    cfg = copy.deepcopy(BEHAVIORS)
    cfg["chase"]["enabled"] = False
    assert "chase" not in effective_weights(cfg, "day")


def test_pick_behavior_distribution_tracks_weights():
    rng = random.Random(42)
    counts = {k: 0 for k in BEHAVIORS}
    for _ in range(20000):
        counts[pick_behavior(BEHAVIORS, "day", rng)] += 1
    # response has the highest weight at midday; idle among the lowest
    assert counts["response"] > counts["alert"] > counts["idle"]
    assert counts["response"] > counts["chase"]


def test_pick_behavior_uniform_fallback_when_all_zero():
    cfg = copy.deepcopy(BEHAVIORS)
    for b in cfg.values():
        b["time_weights"] = {"day": 0.0}
    rng = random.Random(1)
    picks = {pick_behavior(cfg, "day", rng) for _ in range(50)}
    assert len(picks) > 1  # still spreads across behaviors


def test_pick_behavior_raises_without_enabled():
    cfg = copy.deepcopy(BEHAVIORS)
    for b in cfg.values():
        b["enabled"] = False
    with pytest.raises(ValueError):
        pick_behavior(cfg, "day", random.Random())


def test_episode_params_returns_copies():
    p = episode_params(BEHAVIORS, "chase")
    assert p["episode_barks"] == [3, 6]
    assert p["clip_tags"] == ["chase"]
    p["episode_barks"].append(99)
    assert BEHAVIORS["chase"]["episode_barks"] == [3, 6]
