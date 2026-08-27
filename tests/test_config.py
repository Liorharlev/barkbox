import copy
import datetime as dt

import pytest

from barkbox.config import (
    DEFAULTS,
    ConfigError,
    get_day_part,
    in_window,
    is_within_schedule,
    load_config,
    save_config,
)


def _dt(hour, minute=0):
    return dt.datetime(2026, 8, 27, hour, minute)


# -- in_window ---------------------------------------------------------------

def test_in_window_same_day():
    assert in_window(dt.time(8, 0), dt.time(7, 0), dt.time(23, 0))
    assert not in_window(dt.time(6, 0), dt.time(7, 0), dt.time(23, 0))
    assert not in_window(dt.time(23, 0), dt.time(7, 0), dt.time(23, 0))  # end exclusive


def test_in_window_past_midnight():
    w = (dt.time(22, 0), dt.time(6, 0))
    assert in_window(dt.time(23, 30), *w)
    assert in_window(dt.time(2, 0), *w)
    assert not in_window(dt.time(12, 0), *w)


def test_in_window_empty_when_equal():
    assert not in_window(dt.time(9, 0), dt.time(9, 0), dt.time(9, 0))


# -- is_within_schedule ----------------------------------------------------

def test_schedule_always():
    assert is_within_schedule(_dt(3), {"mode": "always"})


def test_schedule_active_window():
    cfg = {"mode": "active_window", "active_window": {"start": "07:00", "end": "23:00"}}
    assert is_within_schedule(_dt(12), cfg)
    assert not is_within_schedule(_dt(2), cfg)


def test_schedule_quiet_window_inverts():
    cfg = {"mode": "quiet_window", "quiet_window": {"start": "02:00", "end": "05:00"}}
    assert not is_within_schedule(_dt(3), cfg)     # inside quiet -> not active
    assert is_within_schedule(_dt(12), cfg)         # outside quiet -> active


def test_schedule_bad_mode():
    with pytest.raises(ConfigError):
        is_within_schedule(_dt(12), {"mode": "weekends"})


# -- get_day_part ----------------------------------------------------------

def test_get_day_part():
    parts = DEFAULTS["timing"]["day_parts"]
    assert get_day_part(_dt(7), parts) == "morning"
    assert get_day_part(_dt(13), parts) == "day"
    assert get_day_part(_dt(20), parts) == "evening"
    assert get_day_part(_dt(23), parts) == "night"
    assert get_day_part(_dt(3), parts) == "night"      # wraps midnight


# -- load / save ----------------------------------------------------------

def test_load_merges_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("enabled: false\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg["enabled"] is False
    assert cfg["timing"]["frequency"] == "medium"       # from defaults
    assert "alert" in cfg["behaviors"]


def test_load_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_legacy_episode_barks_is_migrated_away(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "behaviors:\n"
        "  alert:\n"
        "    episode_barks: [2, 5]\n",
        encoding="utf-8",
    )
    cfg = load_config(p)  # must not raise
    assert "episode_barks" not in cfg["behaviors"]["alert"]
    assert cfg["behaviors"]["alert"]["episode_duration_seconds"] == [15, 45]


def test_save_roundtrip_is_atomic(tmp_path):
    p = tmp_path / "config.yaml"
    data = copy.deepcopy(DEFAULTS)
    data["timing"]["frequency"] = "high"
    save_config(p, data)
    assert load_config(p)["timing"]["frequency"] == "high"
    # no leftover temp files
    assert [f.name for f in tmp_path.iterdir()] == ["config.yaml"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(enabled="yes"),
        lambda c: c["schedule"].update(mode="sometimes"),
        lambda c: c["presence"].update(mode="vacation"),
        lambda c: c["presence"]["multipliers"].update(home=-1),
        lambda c: c["timing"].update(frequency="turbo"),
        lambda c: c["timing"]["frequency_presets"]["low"].update(min_gap_minutes=999),
        lambda c: c["behaviors"]["alert"].update(weight=-2),
        lambda c: c["behaviors"]["alert"].update(max_barks=0),
        lambda c: c["behaviors"]["alert"].update(max_barks=1.5),
        lambda c: c["behaviors"]["alert"].update(episode_duration_seconds=[30, 5]),
        lambda c: c["behaviors"]["alert"].update(episode_duration_seconds=[-1, 5]),
        lambda c: _disable_all_behaviors(c),
        lambda c: c["audio"].update(volume=3),
    ],
)
def test_validation_rejects(tmp_path, mutate):
    data = copy.deepcopy(DEFAULTS)
    mutate(data)
    with pytest.raises(ConfigError):
        save_config(tmp_path / "config.yaml", data)


def _disable_all_behaviors(c):
    for b in c["behaviors"].values():
        b["enabled"] = False
