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


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


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


class _RecordingPlayer(MockPlayer):
    def __init__(self):
        super().__init__(simulate_sleep=False)
        self.volumes = []

    def _play_impl(self, path, volume=1.0):
        self.volumes.append(volume)
        super()._play_impl(path, volume)


def test_run_episode_defaults_to_full_volume():
    player = _RecordingPlayer()
    params = {"max_barks": 5, "episode_duration_seconds": [999, 999],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode("alert", params, CLIPS, {}, 2, player, AppState(), EventLog(),
                 random.Random(1), threading.Event())
    assert player.volumes == [1.0] * 5


def test_run_episode_applies_master_gain():
    player = _RecordingPlayer()
    params = {"max_barks": 4, "episode_duration_seconds": [999, 999],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode("alert", params, CLIPS, {}, 2, player, AppState(), EventLog(),
                 random.Random(1), threading.Event(), master_gain=0.5)
    assert player.volumes == [0.5] * 4


def test_run_episode_distance_walk_drifts_volume_within_bounds():
    player = _RecordingPlayer()
    events = EventLog()
    sim = {"enabled": True, "min_episode_duration_seconds": 6,
           "volume_range": [40, 100], "max_step": 20, "start_volume": 70}
    params = {"max_barks": 30, "episode_duration_seconds": [999, 999],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode("alert", params, CLIPS, {}, 2, player, AppState(), events,
                 random.Random(3), threading.Event(), distance_cfg=sim)
    assert player.volumes[0] == 0.70
    assert all(0.40 <= v <= 1.00 for v in player.volumes)
    assert len(set(player.volumes)) > 1                      # it actually moved
    assert "vol" in events.recent()[0]["detail"]


def test_run_episode_no_walk_for_short_target():
    player = _RecordingPlayer()
    sim = {"enabled": True, "min_episode_duration_seconds": 6,
           "volume_range": [40, 100], "max_step": 20, "start_volume": 70}
    params = {"max_barks": 4, "episode_duration_seconds": [3, 3],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    _run_episode("response", params, CLIPS, {}, 2, player, AppState(), EventLog(),
                 random.Random(1), threading.Event(),
                 master_gain=0.8, distance_cfg=sim)
    # short episode: no drift, just the master gain
    assert player.volumes and all(v == pytest.approx(0.8) for v in player.volumes)


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


# -- now_playing -----------------------------------------------------

class _NowPlayingSpy(MockPlayer):
    """Records state.now_playing at each bark so we can prove it stays set for
    the *whole* episode, not just the first bark."""

    def __init__(self, state):
        super().__init__(simulate_sleep=False)
        self._state = state
        self.seen = []

    def _play_impl(self, path, volume=1.0):
        self.seen.append(self._state.now_playing)
        super()._play_impl(path, volume)


def test_run_episode_sets_now_playing_for_every_bark_then_clears():
    state = AppState()
    player = _NowPlayingSpy(state)
    params = {"max_barks": 4, "episode_duration_seconds": [999, 999],
              "intra_gap_seconds": [0, 0], "clip_tags": []}
    assert state.now_playing is None
    _run_episode("chase", params, CLIPS, {}, 2, player, state, EventLog(),
                 random.Random(1), threading.Event())
    assert player.seen == ["chase"] * 4
    assert state.now_playing is None


def test_run_episode_no_clips_leaves_now_playing_none():
    state = AppState()
    _run_episode(
        "chase",
        {"max_barks": 40, "episode_duration_seconds": [1, 2],
         "intra_gap_seconds": [0, 0], "clip_tags": ["chase"]},
        [], {}, 3, MockPlayer(simulate_sleep=False), state, EventLog(),
        random.Random(), threading.Event(),
    )
    assert state.now_playing is None


def test_run_episode_clears_now_playing_even_if_playback_raises():
    class Boom(MockPlayer):
        def _play_impl(self, path, volume=1.0):
            raise RuntimeError("audio device vanished")

    state = AppState()
    with pytest.raises(RuntimeError):
        _run_episode(
            "alert",
            {"max_barks": 4, "episode_duration_seconds": [999, 999],
             "intra_gap_seconds": [0, 0], "clip_tags": []},
            CLIPS, {}, 2, Boom(simulate_sleep=False), state, EventLog(),
            random.Random(1), threading.Event(),
        )
    assert state.now_playing is None


# -- cancellation (manual-test interrupt / alarm cut short) ----------

def test_run_episode_breaks_promptly_when_cancelled_between_barks():
    state, events = AppState(), EventLog()
    calls = []
    flag = threading.Event()

    class FlipOnSecondBark(MockPlayer):
        def _play_impl(self, path, volume=1.0):
            calls.append(1)
            if len(calls) == 2:
                flag.set()

    _run_episode(
        "alert",
        {"max_barks": 100, "episode_duration_seconds": [999, 999],
         "intra_gap_seconds": [0, 0], "clip_tags": []},
        CLIPS, {}, 2, FlipOnSecondBark(simulate_sleep=False), state, events,
        random.Random(1), threading.Event(), cancelled=flag.is_set,
    )
    assert len(calls) == 2                       # stopped the moment the flag flipped
    assert state.now_playing is None
    assert "interrupted" in events.recent()[0]["detail"]


def test_run_episode_cancel_cuts_the_inter_bark_gap_short():
    state, events = AppState(), EventLog()
    flag = threading.Event()
    threading.Timer(0.1, flag.set).start()
    started = time.monotonic()
    _run_episode(
        "alert",
        {"max_barks": 5, "episode_duration_seconds": [999, 999],
         "intra_gap_seconds": [30, 30], "clip_tags": []},   # 30s between barks
        CLIPS, {}, 2, MockPlayer(simulate_sleep=False), state, events,
        random.Random(1), threading.Event(), cancelled=flag.is_set,
    )
    assert time.monotonic() - started < 5        # didn't sit through the 30s gap
    assert "interrupted" in events.recent()[0]["detail"]


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


# -- master switch beats an active alarm ----------------------------

def test_master_switch_off_stops_an_active_alarm(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "_IDLE_RECHECK_SECONDS", 0.02)
    cfg = _fast_config(tmp_path)
    cfg["enabled"] = False                       # master switch OFF
    cfg["alarm"]["episode_duration_seconds"] = [50, 50]   # would bark for ages
    cfg["alarm"]["episode_gap_seconds"] = [0.01, 0.01]

    state, events = AppState(), EventLog()
    state.enter_alarm_mode(10)
    assert state.alarm_active()

    stop = threading.Event()
    th = threading.Thread(
        target=run, args=(stop, lambda: cfg, state, events, MockPlayer(simulate_sleep=False)),
        daemon=True,
    )
    th.start()
    time.sleep(0.2)
    stop.set()
    state.request_wake()
    th.join(timeout=3)

    kinds = [e["kind"] for e in events.recent()]
    assert not state.alarm_active()              # cleared by the master-switch gate
    assert "alarm_stopped" in kinds
    assert "played:alarm" not in kinds           # never got to bark


def test_turning_enabled_off_ends_an_in_progress_alarm_burst(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "_IDLE_RECHECK_SECONDS", 0.02)
    cfg = _fast_config(tmp_path)
    cfg["enabled"] = True
    cfg["alarm"]["max_barks"] = 100000
    cfg["alarm"]["episode_duration_seconds"] = [50, 50]   # long burst
    cfg["alarm"]["episode_gap_seconds"] = [0.01, 0.01]

    state, events = AppState(), EventLog()
    state.enter_alarm_mode(10)
    player = MockPlayer(fixed_secs=0.01, simulate_sleep=True)

    stop = threading.Event()
    th = threading.Thread(
        target=run, args=(stop, lambda: cfg, state, events, player), daemon=True,
    )
    th.start()
    _wait_until(lambda: state.now_playing == "alarm")

    cfg["enabled"] = False                       # flip the master switch mid-burst
    state.request_wake()
    _wait_until(lambda: state.now_playing is None)
    _wait_until(lambda: not state.alarm_active())

    stop.set()
    state.request_wake()
    th.join(timeout=3)
    assert "alarm_stopped" in [e["kind"] for e in events.recent()]


# -- AppState alarm ---------------------------------------------------

def test_enter_alarm_mode_sets_window_and_wake():
    state = AppState()
    assert not state.alarm_active()
    state.enter_alarm_mode(10)
    assert state.alarm_active()
    assert state.wake_event.is_set()
