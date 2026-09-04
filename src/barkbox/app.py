"""Process entry point: wire everything together and serve.

Runs the scheduler in a daemon thread and the Flask UI (via waitress) on the
main thread.
"""

from __future__ import annotations

import logging
import os
import threading
from logging.handlers import WatchedFileHandler
from pathlib import Path

import waitress

from .config import load_config
from .events import EventLog
from .player import make_player
from .scheduler import run as scheduler_run
from .state import AppState
from .web import create_app


def _default_config_path() -> Path:
    return Path(os.environ.get("BARKBOX_CONFIG", "config.yaml")).resolve()


def _setup_logging() -> logging.Logger:
    """Configure the root logger.

    Always writes to the console (journald on the Pi — itself volatile/RAM by
    default). If ``BARKBOX_LOG_DIR`` is set, also writes to
    ``<dir>/barkbox.log``. On the Pi that directory is a tmpfs (systemd
    ``RuntimeDirectory=barkbox`` -> ``/run/barkbox``), so runtime logging never
    hits the SD card; ``deploy/logsync.sh`` (a daily systemd timer, plus the
    service's ``ExecStopPost``) moves the accumulated lines to the persistent
    copy under ``/var/log/barkbox``.

    ``WatchedFileHandler`` reopens the file when it disappears, so ``logsync.sh``
    can rotate it away by a plain ``mv`` with no lost lines and no restart.
    """
    level = os.environ.get("BARKBOX_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_dir = os.environ.get("BARKBOX_LOG_DIR", "").strip()
    if log_dir:
        try:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            handlers.append(WatchedFileHandler(path / "barkbox.log"))
        except OSError as exc:  # fall back to console-only logging
            logging.getLogger("barkbox").warning(
                "file logging disabled: cannot use %s (%s)", log_dir, exc
            )

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)

    return logging.getLogger("barkbox")


def _make_cached_loader(path: Path):
    """A ``get_config`` that re-parses ``config.yaml`` only when its mtime changes.

    The returned dict is shared and read-only for callers (the scheduler).
    """
    cache: dict = {}
    lock = threading.Lock()

    def loader() -> dict:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        with lock:
            if cache.get("mtime") != mtime or "cfg" not in cache:
                cache["cfg"] = load_config(path)
                cache["mtime"] = mtime
            return cache["cfg"]

    return loader


def main() -> None:
    log = _setup_logging()

    config_path = _default_config_path()
    cfg = load_config(config_path)
    log.info("loaded config from %s", config_path)

    state = AppState(presence_mode=cfg["presence"]["mode"])
    events = EventLog()
    player = make_player(cfg["audio"])
    get_config = _make_cached_loader(config_path)

    stop_event = threading.Event()
    sched = threading.Thread(
        target=scheduler_run,
        args=(stop_event, get_config, state, events, player),
        name="scheduler",
        daemon=True,
    )
    sched.start()

    app = create_app(config_path, state, events, player)
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    log.info("serving control UI on http://%s:%s", host, port)
    try:
        waitress.serve(app, host=host, port=port, threads=4)
    finally:
        stop_event.set()
        state.request_wake()
        sched.join(timeout=5)


if __name__ == "__main__":
    main()
