"""Flask control UI — a single page plus a small JSON API.

Every mutating endpoint: load config -> change one thing -> ``save_config`` ->
``state.request_wake()`` so the scheduler picks it up immediately.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading

from flask import Flask, jsonify, render_template, request

from ..behaviors import episode_params
from ..clips import count_tagged, load_clips, load_tags
from ..config import ConfigError, load_config, parse_hhmm, save_config
from ..scheduler import _run_episode

logger = logging.getLogger("barkbox.web")

_SCHEDULE_MODES = ("always", "active_window", "quiet_window")
_PRESENCE_MODES = ("home", "away")
_FREQUENCIES = ("low", "medium", "high")


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def create_app(config_path, state, events, player, rng: random.Random | None = None) -> Flask:
    app = Flask(__name__)
    rng = rng or random.Random()
    fire_lock = threading.Lock()

    def _cfg() -> dict:
        return load_config(config_path)

    def _save(cfg: dict, kind: str, detail: str = "") -> None:
        save_config(config_path, cfg)
        events.add(kind, detail)
        state.request_wake()

    # -- auth --------------------------------------------------------------
    @app.before_request
    def _auth():
        if not request.path.startswith("/api/"):
            return None
        token = _cfg().get("web", {}).get("auth_token")
        if not token:
            return None
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        given = given or request.args.get("token", "")
        if given != token:
            return jsonify(error="unauthorized"), 401
        return None

    @app.errorhandler(ConfigError)
    def _bad_config(exc):  # noqa: ANN001
        return jsonify(error=str(exc)), 400

    # -- page ------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html")

    # -- read -----------------------------------------------------------
    @app.get("/api/status")
    def status():
        cfg = _cfg()
        clips = load_clips(cfg["paths"]["sounds_dir"])
        tags = load_tags(cfg["paths"]["tags_file"])
        return jsonify(
            enabled=cfg["enabled"],
            schedule=cfg["schedule"],
            presence_mode=state.presence_mode,
            frequency=cfg["timing"]["frequency"],
            behaviors={
                k: {"enabled": b["enabled"], "weight": b["weight"]}
                for k, b in cfg["behaviors"].items()
            },
            next_event_at=_iso(state.next_event_at),
            last_event=state.last_event,
            alarm_active=state.alarm_active(),
            alarm_until=_iso(state.alarm_until),
            clip_count=len(clips),
            tagged_clip_count=count_tagged(clips, tags),
            backend=getattr(player, "backend", "unknown"),
        )

    @app.get("/api/events")
    def event_list():
        return jsonify(events=events.recent(50))

    # -- mutate --------------------------------------------------------
    @app.post("/api/toggle")
    def toggle():
        cfg = _cfg()
        cfg["enabled"] = not cfg["enabled"]
        _save(cfg, "config_changed", f"enabled={cfg['enabled']}")
        return jsonify(enabled=cfg["enabled"])

    @app.post("/api/schedule")
    def set_schedule():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in _SCHEDULE_MODES:
            return jsonify(error=f"mode must be one of {_SCHEDULE_MODES}"), 400
        cfg = _cfg()
        cfg["schedule"]["mode"] = mode
        for key in ("active_window", "quiet_window"):
            if key in body:
                win = body[key] or {}
                start, end = win.get("start"), win.get("end")
                parse_hhmm(start)  # raises ConfigError -> 400
                parse_hhmm(end)
                cfg["schedule"][key] = {"start": start, "end": end}
        _save(cfg, "config_changed", f"schedule={mode}")
        return jsonify(schedule=cfg["schedule"])

    @app.post("/api/presence")
    def set_presence():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in _PRESENCE_MODES:
            return jsonify(error=f"mode must be one of {_PRESENCE_MODES}"), 400
        cfg = _cfg()
        cfg["presence"]["mode"] = mode
        state.set_presence(mode)
        _save(cfg, "presence_changed", mode)
        return jsonify(presence_mode=mode)

    @app.post("/api/frequency")
    def set_frequency():
        body = request.get_json(silent=True) or {}
        value = body.get("value")
        if value not in _FREQUENCIES:
            return jsonify(error=f"value must be one of {_FREQUENCIES}"), 400
        cfg = _cfg()
        cfg["timing"]["frequency"] = value
        _save(cfg, "config_changed", f"frequency={value}")
        return jsonify(frequency=value)

    @app.post("/api/behaviors")
    def set_behavior():
        body = request.get_json(silent=True) or {}
        key = body.get("key")
        cfg = _cfg()
        if key not in cfg["behaviors"]:
            return jsonify(error=f"unknown behavior {key!r}"), 400
        if "enabled" in body:
            cfg["behaviors"][key]["enabled"] = bool(body["enabled"])
        if "weight" in body:
            try:
                w = float(body["weight"])
            except (TypeError, ValueError):
                return jsonify(error="weight must be a number"), 400
            if w < 0:
                return jsonify(error="weight must be >= 0"), 400
            cfg["behaviors"][key]["weight"] = w
        _save(cfg, "config_changed", f"behavior:{key}")
        return jsonify(behavior=key, **cfg["behaviors"][key])

    @app.post("/api/test-bark")
    def test_bark():
        body = request.get_json(silent=True) or {}
        cfg = _cfg()
        behavior = body.get("behavior") or "alert"
        if behavior not in cfg["behaviors"]:
            return jsonify(error=f"unknown behavior {behavior!r}"), 400
        clips = load_clips(cfg["paths"]["sounds_dir"])
        tags = load_tags(cfg["paths"]["tags_file"])
        params = episode_params(cfg["behaviors"], behavior)
        no_repeat_last = cfg["anti_repeat"]["no_repeat_last"]

        def _worker():
            with fire_lock:
                _run_episode(
                    behavior, params, clips, tags, no_repeat_last,
                    player, state, events, rng, threading.Event(),
                )
            events.add("test_bark", behavior)

        threading.Thread(target=_worker, daemon=True).start()
        return jsonify(status="playing", behavior=behavior, clip_count=len(clips))

    @app.post("/api/alarm-test")
    def alarm_test():
        cfg = _cfg()
        state.enter_alarm_mode(cfg["alarm"]["duration_minutes"])
        events.add("alarm_triggered", "manual test")
        return jsonify(status="alarm", until=_iso(state.alarm_until))

    return app
