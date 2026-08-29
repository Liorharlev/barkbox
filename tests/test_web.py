import copy
import time

import pytest

from barkbox.config import DEFAULTS, load_config, save_config
from barkbox.events import EventLog
from barkbox.player import MockPlayer
from barkbox.state import AppState
from barkbox.web import create_app


@pytest.fixture
def client(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    data = copy.deepcopy(DEFAULTS)
    for b in data["behaviors"].values():
        if "episode_duration_seconds" in b:
            b["episode_duration_seconds"] = [0, 0]   # keep the field, but instant
        b["intra_gap_seconds"] = [0, 0]
    save_config(cfg_path, data)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "bark_1.mp3").write_bytes(b"\x00")
    data["paths"]["sounds_dir"] = str(tmp_path / "sounds")
    data["paths"]["tags_file"] = str(tmp_path / "sounds" / "tags.yaml")
    save_config(cfg_path, data)

    state = AppState(presence_mode="away")
    app = create_app(cfg_path, state, EventLog(), MockPlayer(simulate_sleep=False))
    app.config.update(TESTING=True)
    client = app.test_client()
    client._cfg_path = cfg_path
    client._state = state
    return client


def test_status_ok(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True
    assert body["presence_mode"] == "away"
    assert body["clip_count"] == 1
    assert set(body["behaviors"]) == set(DEFAULTS["behaviors"])


def test_toggle_persists_and_wakes(client):
    assert client.post("/api/toggle").get_json()["enabled"] is False
    assert load_config(client._cfg_path)["enabled"] is False
    assert client._state.wake_event.is_set()


def test_presence_updates_state(client):
    r = client.post("/api/presence", json={"mode": "home"})
    assert r.get_json()["presence_mode"] == "home"
    assert client._state.presence_mode == "home"


def test_presence_rejects_bad_mode(client):
    assert client.post("/api/presence", json={"mode": "vacation"}).status_code == 400


def test_schedule_rejects_bad_time(client):
    r = client.post("/api/schedule", json={"mode": "active_window",
                                           "active_window": {"start": "25:00", "end": "26:00"}})
    assert r.status_code == 400


def test_schedule_updates_window_and_wakes(client):
    r = client.post("/api/schedule", json={"mode": "active_window",
                                           "active_window": {"start": "08:30", "end": "21:45"}})
    assert r.status_code == 200
    sched = load_config(client._cfg_path)["schedule"]
    assert sched["mode"] == "active_window"
    assert sched["active_window"] == {"start": "08:30", "end": "21:45"}
    assert client._state.wake_event.is_set()


def test_schedule_quiet_window_independent_of_active(client):
    client.post("/api/schedule", json={"mode": "quiet_window",
                                       "quiet_window": {"start": "01:00", "end": "05:30"}})
    sched = load_config(client._cfg_path)["schedule"]
    assert sched["quiet_window"] == {"start": "01:00", "end": "05:30"}
    assert sched["active_window"] == DEFAULTS["schedule"]["active_window"]  # untouched


def test_behavior_disable(client):
    r = client.post("/api/behaviors", json={"key": "chase", "enabled": False})
    assert r.get_json()["enabled"] is False
    assert load_config(client._cfg_path)["behaviors"]["chase"]["enabled"] is False


def test_behavior_unknown_key(client):
    assert client.post("/api/behaviors", json={"key": "zoomies"}).status_code == 400


def test_status_exposes_behavior_duration(client):
    b = client.get("/api/status").get_json()["behaviors"]
    assert "duration_max" in b["alert"]
    assert b["idle"]["duration_max"] is None       # idle has no duration target


def test_behavior_duration_max_update_persists(client):
    r = client.post("/api/behaviors", json={"key": "alert", "duration_max": 30})
    assert r.status_code == 200
    eds = load_config(client._cfg_path)["behaviors"]["alert"]["episode_duration_seconds"]
    assert eds[1] == 30


def test_behavior_duration_max_rejected_for_single_bark_behavior(client):
    r = client.post("/api/behaviors", json={"key": "idle", "duration_max": 10})
    assert r.status_code == 400


def test_behavior_duration_max_rejected_below_minimum(client):
    # raise the minimum first so there's something to violate
    cfg = load_config(client._cfg_path)
    cfg["behaviors"]["alert"]["episode_duration_seconds"] = [12, 20]
    save_config(client._cfg_path, cfg)
    r = client.post("/api/behaviors", json={"key": "alert", "duration_max": 5})
    assert r.status_code == 400


def test_disabling_last_behavior_is_refused(client):
    for key in list(DEFAULTS["behaviors"]):
        client.post("/api/behaviors", json={"key": key, "enabled": False})
    # at least one must remain enabled -> save_config validation -> 400
    cfg = load_config(client._cfg_path)
    assert any(b["enabled"] for b in cfg["behaviors"].values())


def test_status_exposes_master_volume(client):
    assert client.get("/api/status").get_json()["master_volume"] == 100


# -- currently-playing indicator ------------------------------------

def test_now_playing_endpoint_idle(client):
    r = client.get("/api/now-playing")
    assert r.status_code == 200
    assert r.get_json() == {"currently_playing": None, "alarm_active": False}


def test_status_includes_currently_playing(client):
    assert client.get("/api/status").get_json()["currently_playing"] is None


def test_now_playing_reflects_state(client):
    client._state.set_now_playing("chase")
    assert client.get("/api/now-playing").get_json()["currently_playing"] == "chase"
    client._state.set_now_playing(None)
    assert client.get("/api/now-playing").get_json()["currently_playing"] is None


def test_now_playing_exposes_alarm_active(client):
    assert client.get("/api/now-playing").get_json()["alarm_active"] is False
    client._state.enter_alarm_mode(5)
    assert client.get("/api/now-playing").get_json()["alarm_active"] is True


def test_alarm_stop_clears_active_alarm_and_wakes(client):
    client.post("/api/alarm-test")
    assert client._state.alarm_active()
    client._state.wake_event.clear()
    r = client.post("/api/alarm-stop")
    assert r.status_code == 200
    assert r.get_json()["alarm_active"] is False
    assert not client._state.alarm_active()
    assert client._state.wake_event.is_set()
    kinds = [e["kind"] for e in client.get("/api/events").get_json()["events"]]
    assert "alarm_stopped" in kinds


def test_alarm_stop_is_harmless_when_no_alarm(client):
    r = client.post("/api/alarm-stop")
    assert r.status_code == 200
    assert r.get_json()["alarm_active"] is False


def test_test_bark_clears_now_playing_when_done(client):
    # test-bark runs the episode on a worker thread; with the MockPlayer it
    # finishes fast, and now_playing must be back to None afterwards.
    r = client.post("/api/test-bark", json={"behavior": "alert"})
    assert r.status_code == 200
    for _ in range(50):
        if client._state.now_playing is None and \
           any(e["kind"] == "test_bark" for e in client.get("/api/events").get_json()["events"]):
            break
        time.sleep(0.02)
    assert client._state.now_playing is None


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _slow_test_bark_client(tmp_path, target_seconds=1.0, fixed_secs=0.02):
    """An app whose test barks run a ~1s episode with a slow player, so a second
    press can be observed interrupting the first. The player records
    ``state.now_playing`` at every bark."""
    data = copy.deepcopy(DEFAULTS)
    for b in data["behaviors"].values():
        b["episode_duration_seconds"] = [target_seconds, target_seconds]
        b["intra_gap_seconds"] = [0, 0]
        b["max_barks"] = 500   # config cap; the ~1s duration target ends it first
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "b1.mp3").write_bytes(b"\x00")
    (tmp_path / "sounds" / "b2.mp3").write_bytes(b"\x00")
    data["paths"]["sounds_dir"] = str(tmp_path / "sounds")
    data["paths"]["tags_file"] = str(tmp_path / "sounds" / "tags.yaml")
    save_config(tmp_path / "config.yaml", data)

    state = AppState(presence_mode="away")

    class Spy(MockPlayer):
        def __init__(self):
            super().__init__(fixed_secs=fixed_secs, simulate_sleep=True)
            self.log = []
            self.play_volumes = []

        def _play_impl(self, path, volume=1.0):
            self.log.append(state.now_playing)
            self.play_volumes.append(volume)
            super()._play_impl(path, volume)

    player = Spy()
    app = create_app(tmp_path / "config.yaml", state, EventLog(), player)
    app.config.update(TESTING=True)
    c = app.test_client()
    c._state = state
    c._player = player
    return c


def test_manual_test_bark_lights_the_behavior_while_playing(tmp_path):
    c = _slow_test_bark_client(tmp_path)
    c.post("/api/test-bark", json={"behavior": "chase"})
    _wait_until(lambda: c._state.now_playing == "chase")
    time.sleep(0.1)
    # every bark so far belonged to chase — the green light logic keys off this
    assert c._player.log and all(x == "chase" for x in c._player.log)


def test_new_manual_test_bark_interrupts_the_running_one(tmp_path):
    c = _slow_test_bark_client(tmp_path)
    c.post("/api/test-bark", json={"behavior": "alert"})
    _wait_until(lambda: c._state.now_playing == "alert")
    time.sleep(0.15)
    alert_barks_at_switch = c._player.log.count("alert")

    c.post("/api/test-bark", json={"behavior": "chase"})
    _wait_until(lambda: c._state.now_playing == "chase")

    # the alert episode really stopped — its bark count doesn't keep climbing,
    # and it never reached its full ~50-bark, 1-second target.
    time.sleep(0.3)
    assert c._player.log.count("alert") <= alert_barks_at_switch + 2
    assert c._player.log.count("alert") < 40
    assert "chase" in c._player.log


def test_manual_test_bark_is_scaled_by_master_volume_not_distance_walk(tmp_path):
    data = copy.deepcopy(DEFAULTS)
    data["audio"]["master_volume"] = 50
    for b in data["behaviors"].values():
        # long target — would engage the distance walk IF it applied to test barks
        b["episode_duration_seconds"] = [30, 30]
        b["intra_gap_seconds"] = [0, 0]
        b["max_barks"] = 20
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "b1.mp3").write_bytes(b"\x00")
    data["paths"]["sounds_dir"] = str(tmp_path / "sounds")
    data["paths"]["tags_file"] = str(tmp_path / "sounds" / "tags.yaml")
    save_config(tmp_path / "config.yaml", data)

    vols = []

    class VolSpy(MockPlayer):
        def _play_impl(self, path, volume=1.0):
            vols.append(volume)
            super()._play_impl(path, volume)

    app = create_app(tmp_path / "config.yaml", AppState(presence_mode="away"),
                     EventLog(), VolSpy(simulate_sleep=False))
    app.config.update(TESTING=True)
    c = app.test_client()

    c.post("/api/test-bark", json={"behavior": "alert"})
    _wait_until(lambda: len(vols) >= 10)
    # every bark: scaled by master_volume (0.5), and no per-bark drift
    assert all(v == 0.5 for v in vols)


def test_manual_test_bark_full_volume_when_master_is_100(tmp_path):
    c = _slow_test_bark_client(tmp_path)   # DEFAULTS -> master_volume 100
    c.post("/api/test-bark", json={"behavior": "idle"})
    _wait_until(lambda: c._player.play_volumes)
    assert all(v == 1.0 for v in c._player.play_volumes)


# -- editable frequency presets --------------------------------------

def test_status_exposes_presets_and_home_multiplier(client):
    body = client.get("/api/status").get_json()
    assert body["frequency_presets"] == DEFAULTS["timing"]["frequency_presets"]
    assert body["home_multiplier"] == DEFAULTS["presence"]["multipliers"]["home"]


def test_frequency_preset_update_persists_and_wakes(client):
    r = client.post("/api/frequency-preset", json={"key": "medium", "min": 8, "max": 25})
    assert r.status_code == 200
    preset = load_config(client._cfg_path)["timing"]["frequency_presets"]["medium"]
    assert preset == {"min_gap_minutes": 8, "max_gap_minutes": 25}
    assert client._state.wake_event.is_set()
    # other presets untouched
    assert load_config(client._cfg_path)["timing"]["frequency_presets"]["low"] == \
        DEFAULTS["timing"]["frequency_presets"]["low"]


def test_frequency_preset_rejects_min_over_max(client):
    r = client.post("/api/frequency-preset", json={"key": "low", "min": 100, "max": 10})
    assert r.status_code == 400
    assert load_config(client._cfg_path)["timing"]["frequency_presets"]["low"] == \
        DEFAULTS["timing"]["frequency_presets"]["low"]


def test_frequency_preset_rejects_non_positive(client):
    assert client.post("/api/frequency-preset",
                       json={"key": "low", "min": 0, "max": 10}).status_code == 400
    assert client.post("/api/frequency-preset",
                       json={"key": "low", "min": -5, "max": 10}).status_code == 400


def test_frequency_preset_rejects_unknown_key(client):
    assert client.post("/api/frequency-preset",
                       json={"key": "turbo", "min": 1, "max": 2}).status_code == 400


def test_frequency_preset_rejects_non_numbers(client):
    assert client.post("/api/frequency-preset",
                       json={"key": "low", "min": "soon", "max": 10}).status_code == 400
    assert client.post("/api/frequency-preset", json={"key": "low"}).status_code == 400


# -- editable home multiplier (sent as the inverse "slow down by X") ---

def test_home_multiplier_stores_reciprocal_and_wakes(client):
    r = client.post("/api/home-multiplier", json={"value": 4})
    assert r.status_code == 200
    assert r.get_json()["home_multiplier"] == 0.25
    assert load_config(client._cfg_path)["presence"]["multipliers"]["home"] == 0.25
    assert load_config(client._cfg_path)["presence"]["multipliers"]["away"] == 1.0
    assert client._state.wake_event.is_set()


def test_home_multiplier_rejects_zero_and_negative(client):
    assert client.post("/api/home-multiplier", json={"value": 0}).status_code == 400
    assert client.post("/api/home-multiplier", json={"value": -2}).status_code == 400
    assert client.post("/api/home-multiplier", json={"value": "lots"}).status_code == 400


def test_master_volume_persists_and_wakes(client):
    r = client.post("/api/master-volume", json={"value": 60})
    assert r.status_code == 200
    assert r.get_json()["master_volume"] == 60
    assert load_config(client._cfg_path)["audio"]["master_volume"] == 60
    assert client._state.wake_event.is_set()


def test_master_volume_rejects_out_of_range(client):
    assert client.post("/api/master-volume", json={"value": 150}).status_code == 400
    assert client.post("/api/master-volume", json={"value": -5}).status_code == 400
    assert client.post("/api/master-volume", json={"value": "x"}).status_code == 400


def test_alarm_test_sets_mode(client):
    r = client.post("/api/alarm-test")
    assert r.get_json()["status"] == "alarm"
    assert client._state.alarm_active()


def test_auth_token_enforced(client):
    cfg = load_config(client._cfg_path)
    cfg["web"]["auth_token"] = "secret"
    save_config(client._cfg_path, cfg)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status?token=secret").status_code == 200
