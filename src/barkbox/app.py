"""Process entry point: wire everything together and serve.

Runs the scheduler in a daemon thread and the Flask UI (via waitress) on the
main thread.
"""

from __future__ import annotations

import logging
import os
import threading
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
    logging.basicConfig(
        level=os.environ.get("BARKBOX_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("barkbox")

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
