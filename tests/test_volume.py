import random

from barkbox.volume import DistanceWalk, make_distance_walk, master_scale

SIM = {
    "enabled": True,
    "min_episode_duration_seconds": 6,
    "volume_range": [40, 100],
    "max_step": 20,
    "start_volume": None,
}


# -- master_scale ---------------------------------------------------------

def test_master_scale_maps_percent_to_fraction():
    assert master_scale({"master_volume": 100}) == 1.0
    assert master_scale({"master_volume": 50}) == 0.5
    assert master_scale({"master_volume": 0}) == 0.0


def test_master_scale_defaults_to_full_and_clamps():
    assert master_scale({}) == 1.0
    assert master_scale({"master_volume": 250}) == 1.0
    assert master_scale({"master_volume": "nonsense"}) == 1.0


# -- make_distance_walk: when it engages --------------------------------

def test_no_walk_when_disabled():
    cfg = {**SIM, "enabled": False}
    assert make_distance_walk(cfg, 30, random.Random(0)) is None


def test_no_walk_for_short_episode():
    assert make_distance_walk(SIM, 6, random.Random(0)) is None
    assert make_distance_walk(SIM, 5.9, random.Random(0)) is None


def test_no_walk_without_target_duration():
    assert make_distance_walk(SIM, None, random.Random(0)) is None


def test_walk_engages_for_long_episode():
    assert isinstance(make_distance_walk(SIM, 6.1, random.Random(0)), DistanceWalk)


def test_no_walk_when_cfg_missing():
    assert make_distance_walk(None, 30, random.Random(0)) is None


# -- DistanceWalk behaviour --------------------------------------------

def test_first_bark_uses_start_volume():
    walk = make_distance_walk({**SIM, "start_volume": 70}, 30, random.Random(0))
    assert walk.next_scale() == 0.70


def test_random_start_lands_in_range():
    for seed in range(20):
        walk = make_distance_walk(SIM, 30, random.Random(seed))
        assert 0.40 <= walk.next_scale() <= 1.00


def test_walk_stays_in_range_and_steps_are_bounded():
    walk = make_distance_walk({**SIM, "start_volume": 70}, 30, random.Random(1))
    prev = walk.next_scale() * 100
    for _ in range(500):
        cur = walk.next_scale() * 100
        assert 40 <= cur <= 100
        assert abs(cur - prev) <= 20 + 1e-9
        prev = cur


def test_walk_actually_moves():
    walk = make_distance_walk({**SIM, "start_volume": 70}, 30, random.Random(2))
    vals = {round(walk.next_scale(), 4) for _ in range(30)}
    assert len(vals) > 1
