import copy

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
        b["episode_barks"] = [1, 1]
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


def test_behavior_disable(client):
    r = client.post("/api/behaviors", json={"key": "chase", "enabled": False})
    assert r.get_json()["enabled"] is False
    assert load_config(client._cfg_path)["behaviors"]["chase"]["enabled"] is False


def test_behavior_unknown_key(client):
    assert client.post("/api/behaviors", json={"key": "zoomies"}).status_code == 400


def test_disabling_last_behavior_is_refused(client):
    for key in list(DEFAULTS["behaviors"]):
        client.post("/api/behaviors", json={"key": key, "enabled": False})
    # at least one must remain enabled -> save_config validation -> 400
    cfg = load_config(client._cfg_path)
    assert any(b["enabled"] for b in cfg["behaviors"].values())


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
