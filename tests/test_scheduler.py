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

def test_run_episode_single_bark_when_no_duration():
    state, events = AppState(), EventLog()
    params = {"max_barks": 40, "episode_duration_seconds": None,
              "intra_gap_seconds": [0, 0], "clip_tags": ["idle"]}
    _run_episode(
        "idle", params, CLIPS, {}, no_repeat_last=2,
        player=MockPlayer(simulate_sleep=False), state=state, events=events,
        rng=random.Random(3), stop_event=threading.Event(),
    )
    assert len(state.recent_clips) == 1
    assert "played:idle" in [e["kind"] for e in events.recent()]


def test_run_episode_stops_after_full_bark_near_target():
    state, events = AppState(), EventLog()
    player = MockPlayer(fixed_secs=0.1, simulate_sleep=True)   # each bark ~0.1s
    params = {"max_barks": 40, "episode_duration_seconds": [1.0, 1.0],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode(
        "alert", params, CLIPS, {}, 2, player, state, events,
        random.Random(1), threading.Event(),
    )
    n = len(state.recent_clips)
    # ~10 barks of 0.1s to reach the 1.0s target; wide band for CI timing jitter
    assert 5 <= n < 40
    detail = events.recent()[0]["detail"]
    assert "cap" not in detail            # stopped on the target, not the safety cap


def test_run_episode_max_barks_is_a_hard_cap():
    state, events = AppState(), EventLog()
    params = {"max_barks": 6, "episode_duration_seconds": [999, 999],  # target never reached
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode(
        "alert", params, CLIPS, {}, 2, MockPlayer(simulate_sleep=False),
        state, events, random.Random(1), threading.Event(),
    )
    assert len(state.recent_clips) == 6
    assert "cap" in events.recent()[0]["detail"]


def test_run_episode_no_clips_is_noted():
    events = EventLog()
    _run_episode(
        "chase",
        {"max_barks": 40, "episode_duration_seconds": [1, 2],
         "intra_gap_seconds": [0, 0], "clip_tags": ["chase"]},
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
        b.pop("episode_duration_seconds", None)   # single bark per episode -> fast
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
