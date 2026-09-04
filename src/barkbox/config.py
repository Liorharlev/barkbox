"""Load, validate and persist ``config.yaml`` — the single source of truth.

The web UI and the scheduler both read/write this file, so every mutation goes
through :func:`save_config`, which serialises writes with a module-level lock and
replaces the file atomically.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

from .player import _AUTO_ORDER as _REAL_AUDIO_BACKENDS

__all__ = [
    "ConfigError",
    "DEFAULTS",
    "load_config",
    "save_config",
    "is_within_schedule",
    "get_day_part",
    "parse_hhmm",
    "in_window",
]

_SCHEDULE_MODES = ("always", "active_window", "quiet_window")
_PRESENCE_MODES = ("home", "away")
# Sourced from player._AUTO_ORDER (not hand-duplicated) so a backend added
# there can never again be rejected here as invalid before it's ever tried —
# that gap ("ffmpeg" missing from this tuple) crash-looped the service on the
# Pi on 2026-09-04. "auto"/"mock" aren't real player backends, so they're
# added on top.
_AUDIO_BACKENDS = ("auto", *_REAL_AUDIO_BACKENDS, "mock")

# Guards writes to any config file. A single lock is fine: there is exactly one
# config file per process and writes are rare.
_write_lock = threading.Lock()


class ConfigError(ValueError):
    """Raised when ``config.yaml`` is missing required data or is inconsistent."""


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "schedule": {
        "mode": "always",
        "active_window": {"start": "07:00", "end": "23:00"},
        "quiet_window": {"start": "02:00", "end": "05:00"},
    },
    "presence": {
        "mode": "away",
        "multipliers": {"home": 0.25, "away": 1.0},
    },
    "timing": {
        "day_parts": {
            "morning": ["06:00", "10:00"],
            "day": ["10:00", "17:00"],
            "evening": ["17:00", "22:00"],
            "night": ["22:00", "06:00"],
        },
        "frequency": "medium",
        "frequency_presets": {
            "low": {"min_gap_minutes": 45, "max_gap_minutes": 150},
            "medium": {"min_gap_minutes": 15, "max_gap_minutes": 60},
            "high": {"min_gap_minutes": 5, "max_gap_minutes": 20},
        },
    },
    "behaviors": {
        "alert": {
            "enabled": True,
            "weight": 1.0,
            "time_weights": {"morning": 1.0, "day": 0.8, "evening": 1.2, "night": 1.5},
            "episode_duration_seconds": [15, 45],
            "max_barks": 40,
            "intra_gap_seconds": [2, 12],
            "clip_tags": ["alert"],
        },
        "response": {
            "enabled": True,
            "weight": 2.0,
            "time_weights": {"morning": 1.2, "day": 1.0, "evening": 1.1, "night": 0.6},
            "episode_duration_seconds": [3, 10],
            "max_barks": 40,
            "intra_gap_seconds": [1, 4],
            "clip_tags": ["response"],
        },
        "chase": {
            "enabled": True,
            "weight": 0.7,
            "time_weights": {"morning": 1.5, "day": 0.8, "evening": 1.3, "night": 0.1},
            "episode_duration_seconds": [8, 20],
            "max_barks": 40,
            "intra_gap_seconds": [0.5, 2],
            "clip_tags": ["chase"],
        },
        "noise_reaction": {
            "enabled": True,
            "weight": 1.0,
            "time_weights": {"morning": 1.0, "day": 1.0, "evening": 1.0, "night": 0.7},
            "episode_duration_seconds": [5, 15],
            "max_barks": 40,
            "intra_gap_seconds": [1, 3],
            "clip_tags": ["noise"],
        },
        "idle": {
            "enabled": True,
            "weight": 0.3,
            "time_weights": {"morning": 1.0, "day": 1.0, "evening": 1.0, "night": 0.5},
            "max_barks": 1,
            "intra_gap_seconds": [0, 0],
            "clip_tags": ["idle"],
        },
    },
    "anti_repeat": {"no_repeat_last": 3},
    "audio": {"backend": "auto", "device": "default", "volume": 0.9, "master_volume": 100},
    "distance_simulation": {
        "enabled": True,
        "min_episode_duration_seconds": 6,
        "volume_range": [40, 100],
        "max_step": 20,
        "start_volume": None,
    },
    "paths": {"sounds_dir": "sounds", "tags_file": "sounds/tags.yaml"},
    "web": {"host": "0.0.0.0", "port": 8080, "auth_token": None},
    "alarm": {
        "enabled": False,
        "duration_minutes": 10,
        "episode_duration_seconds": [10, 25],
        "max_barks": 40,
        "episode_gap_seconds": [1, 4],
    },
}


def _migrate(cfg: dict) -> None:
    """In-place upgrade of superseded fields so old ``config.yaml`` files load.

    ``episode_barks: [lo, hi]`` was replaced by ``episode_duration_seconds`` +
    ``max_barks``; drop the stale key (defaults supply the new ones).
    """
    for b in cfg.get("behaviors", {}).values():
        if isinstance(b, dict):
            b.pop("episode_barks", None)
    if isinstance(cfg.get("alarm"), dict):
        cfg["alarm"].pop("episode_barks", None)


def _deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` recursively merged with ``override`` (override wins).

    Nested dicts are merged key-by-key; every other value (including lists) is
    replaced wholesale.
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def parse_hhmm(value: str) -> dt.time:
    """Parse a ``"HH:MM"`` string into a :class:`datetime.time`."""
    try:
        hh, mm = str(value).strip().split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"invalid time {value!r}, expected HH:MM") from exc


def in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """True if ``now`` falls in ``[start, end)``, handling windows past midnight.

    ``start == end`` is treated as an empty window (never inside).
    """
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def is_within_schedule(now: dt.datetime, schedule_cfg: dict) -> bool:
    """Whether the system should be active at ``now`` given the schedule block."""
    mode = schedule_cfg.get("mode", "always")
    t = now.time()
    if mode == "always":
        return True
    if mode == "active_window":
        w = schedule_cfg["active_window"]
        return in_window(t, parse_hhmm(w["start"]), parse_hhmm(w["end"]))
    if mode == "quiet_window":
        w = schedule_cfg["quiet_window"]
        return not in_window(t, parse_hhmm(w["start"]), parse_hhmm(w["end"]))
    raise ConfigError(f"unknown schedule.mode {mode!r}")


def get_day_part(now: dt.datetime, day_parts: dict) -> str:
    """Return the key of the ``day_parts`` window containing ``now``.

    Falls back to the first defined key if the windows leave a gap.
    """
    t = now.time()
    for name, (start, end) in day_parts.items():
        if in_window(t, parse_hhmm(start), parse_hhmm(end)):
            return name
    return next(iter(day_parts))


def _validate(cfg: dict) -> None:
    if not isinstance(cfg.get("enabled"), bool):
        raise ConfigError("enabled must be true or false")

    sched = cfg["schedule"]
    if sched.get("mode") not in _SCHEDULE_MODES:
        raise ConfigError(f"schedule.mode must be one of {_SCHEDULE_MODES}")
    for wname in ("active_window", "quiet_window"):
        w = sched.get(wname, {})
        parse_hhmm(w.get("start"))
        parse_hhmm(w.get("end"))

    pres = cfg["presence"]
    if pres.get("mode") not in _PRESENCE_MODES:
        raise ConfigError(f"presence.mode must be one of {_PRESENCE_MODES}")
    for k in _PRESENCE_MODES:
        m = pres["multipliers"].get(k)
        if not isinstance(m, (int, float)) or m < 0:
            raise ConfigError(f"presence.multipliers.{k} must be a number >= 0")

    timing = cfg["timing"]
    for name, pair in timing["day_parts"].items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ConfigError(f"timing.day_parts.{name} must be [start, end]")
        parse_hhmm(pair[0])
        parse_hhmm(pair[1])
    presets = timing["frequency_presets"]
    if timing.get("frequency") not in presets:
        raise ConfigError("timing.frequency must name an entry in frequency_presets")
    for name, preset in presets.items():
        lo = preset.get("min_gap_minutes")
        hi = preset.get("max_gap_minutes")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            raise ConfigError(f"frequency_presets.{name}: gaps must be numbers")
        if lo <= 0 or hi <= 0 or lo > hi:
            raise ConfigError(
                f"frequency_presets.{name}: need 0 < min_gap_minutes <= max_gap_minutes"
            )

    behaviors = cfg["behaviors"]
    if not isinstance(behaviors, dict) or not behaviors:
        raise ConfigError("behaviors must be a non-empty mapping")
    any_active = False
    for name, b in behaviors.items():
        if not isinstance(b.get("enabled"), bool):
            raise ConfigError(f"behaviors.{name}.enabled must be true or false")
        w = b.get("weight")
        if not isinstance(w, (int, float)) or w < 0:
            raise ConfigError(f"behaviors.{name}.weight must be a number >= 0")
        _validate_max_barks(b.get("max_barks"), f"behaviors.{name}.max_barks")
        if b.get("episode_duration_seconds") is not None:
            _validate_num_pair(
                b.get("episode_duration_seconds"),
                f"behaviors.{name}.episode_duration_seconds",
                minimum=0,
            )
        _validate_num_pair(
            b.get("intra_gap_seconds"), f"behaviors.{name}.intra_gap_seconds", minimum=0
        )
        if not isinstance(b.get("clip_tags"), list):
            raise ConfigError(f"behaviors.{name}.clip_tags must be a list")
        tw = b.get("time_weights", {})
        if not isinstance(tw, dict):
            raise ConfigError(f"behaviors.{name}.time_weights must be a mapping")
        for part, val in tw.items():
            if not isinstance(val, (int, float)) or val < 0:
                raise ConfigError(f"behaviors.{name}.time_weights.{part} must be >= 0")
        if b["enabled"] and w > 0:
            any_active = True
    if not any_active:
        raise ConfigError("at least one behavior must be enabled with weight > 0")

    nrl = cfg["anti_repeat"].get("no_repeat_last")
    if not isinstance(nrl, int) or nrl < 0:
        raise ConfigError("anti_repeat.no_repeat_last must be an int >= 0")

    audio = cfg["audio"]
    if audio.get("backend") not in _AUDIO_BACKENDS:
        raise ConfigError(f"audio.backend must be one of {_AUDIO_BACKENDS}")
    vol = audio.get("volume")
    if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not 0 <= vol <= 1:
        raise ConfigError("audio.volume must be a number in [0, 1]")
    mv = audio.get("master_volume")
    if not isinstance(mv, (int, float)) or isinstance(mv, bool) or not 0 <= mv <= 100:
        raise ConfigError("audio.master_volume must be a number in [0, 100]")

    ds = cfg["distance_simulation"]
    if not isinstance(ds.get("enabled"), bool):
        raise ConfigError("distance_simulation.enabled must be true or false")
    mineps = ds.get("min_episode_duration_seconds")
    if not isinstance(mineps, (int, float)) or isinstance(mineps, bool) or mineps < 0:
        raise ConfigError(
            "distance_simulation.min_episode_duration_seconds must be a number >= 0"
        )
    _validate_num_pair(
        ds.get("volume_range"), "distance_simulation.volume_range", minimum=0
    )
    v_lo, v_hi = ds["volume_range"]
    if v_hi > 100:
        raise ConfigError("distance_simulation.volume_range must lie within [0, 100]")
    step = ds.get("max_step")
    if not isinstance(step, (int, float)) or isinstance(step, bool) or step <= 0:
        raise ConfigError("distance_simulation.max_step must be a number > 0")
    sv = ds.get("start_volume")
    if sv is not None and (
        not isinstance(sv, (int, float)) or isinstance(sv, bool) or not v_lo <= sv <= v_hi
    ):
        raise ConfigError(
            "distance_simulation.start_volume must be null or a number within volume_range"
        )

    web = cfg["web"]
    if not isinstance(web.get("port"), int) or not 1 <= web["port"] <= 65535:
        raise ConfigError("web.port must be an int in [1, 65535]")

    alarm = cfg["alarm"]
    _validate_max_barks(alarm.get("max_barks"), "alarm.max_barks")
    _validate_num_pair(
        alarm.get("episode_duration_seconds"), "alarm.episode_duration_seconds", minimum=0
    )
    _validate_num_pair(alarm.get("episode_gap_seconds"), "alarm.episode_gap_seconds", minimum=0)
    if not isinstance(alarm.get("duration_minutes"), (int, float)) or alarm["duration_minutes"] <= 0:
        raise ConfigError("alarm.duration_minutes must be a number > 0")


def _validate_num_pair(value: Any, label: str, *, minimum: float) -> None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(x, (int, float)) for x in value)
    ):
        raise ConfigError(f"{label} must be [low, high] numbers")
    lo, hi = value
    if lo < minimum or hi < lo:
        raise ConfigError(f"{label}: need {minimum} <= low <= high")


def _validate_max_barks(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 500:
        raise ConfigError(f"{label} must be an int in [1, 500]")


def load_config(path: str | os.PathLike) -> dict:
    """Read ``path``, merge over :data:`DEFAULTS`, validate, and return the config.

    Raises :class:`ConfigError` if the file is unreadable or fails validation.
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    cfg = _deep_merge(DEFAULTS, raw)
    _migrate(cfg)
    _validate(cfg)
    return cfg


def save_config(path: str | os.PathLike, data: dict) -> None:
    """Validate ``data`` and write it to ``path`` atomically.

    Serialised with a module-level lock so a scheduler read never sees a
    half-written file.
    """
    _migrate(data)
    _validate(_deep_merge(DEFAULTS, data))
    p = Path(path)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    with _write_lock:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
