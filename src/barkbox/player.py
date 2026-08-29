"""Audio playback.

A real :class:`Player` shells out to a lightweight CLI player (mpg123 / ffplay /
aplay); :class:`MockPlayer` just logs, for development on Windows and for tests.

Both hold an internal lock around the actual playback so concurrent callers — the
scheduler, ``/api/test-bark`` and alarm mode — queue up instead of fighting over
one ALSA device (which matters on a 1 GB Pi).
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("barkbox.player")

_MP3_SCALE_UNITY = 32768  # mpg123: this factor == original volume


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def detect_backend() -> str:
    """Pick the first available CLI player, or ``"mock"`` if none is installed."""
    for name in ("mpg123", "ffplay", "aplay"):
        if shutil.which(name):
            return name
    return "mock"


class _BasePlayer:
    def __init__(self) -> None:
        self._play_lock = threading.Lock()

    def play(self, path: str | Path, volume: float = 1.0) -> float:
        """Play ``path``, blocking until it finishes. Returns elapsed seconds.

        ``volume`` is a per-call gain in ``[0.0, 1.0]`` applied on top of the
        player's configured base volume — the scheduler uses it for master
        volume and the distance-simulation random walk. Manual test barks leave
        it at ``1.0``.
        """
        with self._play_lock:
            start = time.monotonic()
            self._play_impl(Path(path), _clamp01(volume))
            return time.monotonic() - start

    def _play_impl(self, path: Path, volume: float) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class Player(_BasePlayer):
    def __init__(self, backend: str, device: str = "default", volume: float = 1.0) -> None:
        super().__init__()
        self.backend = backend
        self.device = device
        self.volume = max(0.0, min(1.0, float(volume)))

    def _command(self, path: Path, volume: float = 1.0) -> list[str]:
        p = str(path)
        gain = _clamp01(self.volume * volume)
        if self.backend == "mpg123":
            scale = str(int(gain * _MP3_SCALE_UNITY))
            cmd = ["mpg123", "-q", "-f", scale]
            if self.device and self.device != "default":
                cmd += ["-a", self.device]
            return cmd + [p]
        if self.backend == "ffplay":
            return [
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-af", f"volume={gain:.4f}", p,
            ]
        if self.backend == "aplay":
            cmd = ["aplay", "-q"]  # no software volume control
            if self.device and self.device != "default":
                cmd += ["-D", self.device]
            return cmd + [p]
        raise ValueError(f"unsupported audio backend: {self.backend!r}")

    def _play_impl(self, path: Path, volume: float) -> None:
        cmd = self._command(path, volume)
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            logger.error("audio backend %r not found on PATH", self.backend)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="replace").strip()
            logger.error("playback failed (%s): %s", path.name, stderr)


class MockPlayer(_BasePlayer):
    """Logs instead of playing. Used on Windows and in tests."""

    backend = "mock"

    def __init__(
        self,
        rng: random.Random | None = None,
        simulate_sleep: bool = True,
        fixed_secs: float | None = None,
    ) -> None:
        super().__init__()
        self._rng = rng or random.Random()
        self._simulate_sleep = simulate_sleep
        self._fixed_secs = fixed_secs

    def _play_impl(self, path: Path, volume: float = 1.0) -> None:
        secs = self._fixed_secs if self._fixed_secs is not None else round(self._rng.uniform(0.3, 1.4), 2)
        logger.info("[MOCK] play %s (~%.1fs, vol %d%%)", path.name, secs, round(volume * 100))
        if self._simulate_sleep:
            time.sleep(secs)


def make_player(audio_cfg: dict) -> _BasePlayer:
    """Build a player from the ``audio`` config block."""
    backend = audio_cfg.get("backend", "auto")
    if backend == "auto":
        backend = detect_backend()
        logger.info("audio backend auto-detected: %s", backend)
    if backend == "mock":
        return MockPlayer()
    return Player(
        backend=backend,
        device=audio_cfg.get("device", "default"),
        volume=audio_cfg.get("volume", 1.0),
    )
