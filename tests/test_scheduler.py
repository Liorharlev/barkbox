import copy
import random
import threading
import time
from pathlib import Path

import pytest

from barkbox import scheduler
from barkbox.config import DEFAULTS
from barkbox.events import EventLog
from barkbox.player import MockPlayer
from barkbox.scheduler import _gap_seconds, _run_episode, _wait, pick_clip, run
from barkbox.state import AppState

CLIPS = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3"), Path("d.mp3")]


# -- pick_clip -------------------------------------------------------------

def test_pick_clip_avoids_recent():
    rng = random.Random(0)
    recent = ["a.mp3", "b.mp3", "c.mp3"]
    for _ in range(50):
        assert pick_clip(CLIPS, recent, 3, rng).name == "d.mp3"


def test_pick_clip_relaxes_when_all_banned():
    rng = random.Random(0)
    recent = [c.name for c in CLIPS]
    assert pick_clip(CLIPS, recent, 10, rng) in CLIPS


def test_pick_clip_no_repeat_zero_disables_ban():
    rng = random.Random(0)
    picks = {pick_clip(CLIPS, ["a.mp3"], 0, rng).name for _ in range(50)}
    assert "a.mp3" in picks


# -- _wait ---------------------------------------------------------------

def test_wait_timeout():
    assert _wait(threading.Event(), threading.Event(), 0.0) == "timeout"


def test_wait_stop_wins():
    stop, wake = threading.Event(), threading.Event()
    stop.set()
    wake.set()
    assert _wait(stop, wake, 5) == "stop"


def test_wait_wakes_early():
    stop, wake = threading.Event(), threading.Event()
    t = threading.Timer(0.05, wake.set)
    t.start()
    start = time.monotonic()
    assert _wait(stop, wake, 5) == "wake"
    assert time.monotonic() - start < 2


# -- _gap_seconds --------------------------------------------------------

def test_gap_seconds_within_preset_bounds():
    rng = random.Random(1)
    for _ in range(200):
        gap = _gap_seconds(DEFAULTS, "away", rng)
        assert 5 * 60 <= gap <= 60 * 60      # medium preset, multiplier 1.0


def test_gap_seconds_presence_multiplier_lengthens_gap():
    away = _gap_seconds(DEFAULTS, "away", random.Random(7))
    home = _gap_seconds(DEFAULTS, "home", random.Random(7))
    assert home == pytest.approx(away / 0.25)


def test_gap_seconds_zero_multiplier_means_quiet():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["presence"]["multipliers"]["home"] = 0.0
    assert _gap_seconds(cfg, "home", random.Random()) is None


# -- _run_episode ------------------------------------------------------

def test_run_episode_plays_and_records():
    state, events = AppState(), EventLog()
    params = {"episode_barks": [3, 3], "intra_gap_seconds": [0, 0], "clip_tags": ["alert"]}
    _run_episode(
        "alert", params, CLIPS, {}, no_repeat_last=2,
        player=MockPlayer(simulate_sleep=False), state=state, events=events,
        rng=random.Random(3), stop_event=threading.Event(),
    )
    assert len(state.recent_clips) == 3
    kinds = [e["kind"] for e in events.recent()]
    assert "played:alert" in kinds


def test_run_episode_no_clips_is_noted():
    events = EventLog()
    _run_episode(
        "chase", {"episode_barks": [1, 2], "intra_gap_seconds": [0, 0], "clip_tags": ["chase"]},
        [], {}, 3, MockPlayer(simulate_sleep=False), AppState(), events,
        random.Random(), threading.Event(),
    )
    assert events.recent()[0]["kind"] == "skipped_no_clips"


# -- run() integration ------------------------------------------------

def _fast_config(tmp_path):
    """DEFAULTS with instant episodes and a couple of real clips."""
    for name in ("bark_1.mp3", "bark_2.mp3", "bark_3.mp3"):
        (tmp_path / name).write_bytes(b"\x00")
    cfg = copy.deepcopy(DEFAULTS)
    cfg["paths"]["sounds_dir"] = str(tmp_path)
    cfg["paths"]["tags_file"] = str(tmp_path / "tags.yaml")
    for b in cfg["behaviors"].values():
        b["episode_barks"] = [1, 1]
        b["intra_gap_seconds"] = [0, 0]
    return cfg


def test_run_loop_fires_episodes_then_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "_gap_seconds", lambda *a, **k: 0.02)
    cfg = _fast_config(tmp_path)

    state, events = AppState(presence_mode="away"), EventLog()
    stop = threading.Event()
    th = threading.Thread(
        target=run,
        args=(stop, lambda: cfg, state, events, MockPlayer(simulate_sleep=False)),
        kwargs={"rng": random.Random(0)},
        daemon=True,
    )
    th.start()
    time.sleep(0.3)
    stop.set()
    state.request_wake()
    th.join(timeout=3)
    assert not th.is_alive()
    kinds = [e["kind"] for e in events.recent()]
    assert any(k.startswith("played:") for k in kinds)
    assert state.next_event_at is not None


def test_run_loop_respects_disabled(monkeypatch):
    monkeypatch.setattr(scheduler, "_IDLE_RECHECK_SECONDS", 0.02)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["enabled"] = False

    state, events = AppState(), EventLog()
    stop = threading.Event()
    th = threading.Thread(
        target=run, args=(stop, lambda: cfg, state, events, MockPlayer(simulate_sleep=False)),
        daemon=True,
    )
    th.start()
    time.sleep(0.15)
    stop.set()
    state.request_wake()
    th.join(timeout=3)
    kinds = [e["kind"] for e in events.recent()]
    assert "skipped_disabled" in kinds
    assert kinds.count("skipped_disabled") == 1     # logged once, not every recheck


# -- AppState alarm ---------------------------------------------------

def test_enter_alarm_mode_sets_window_and_wake():
    state = AppState()
    assert not state.alarm_active()
    state.enter_alarm_mode(10)
    assert state.alarm_active()
    assert state.wake_event.is_set()
