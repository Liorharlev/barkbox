"""Audio playback.

A real :class:`Player` shells out to a lightweight CLI player (ffmpeg-piped-to-
aplay / ffplay / aplay / mpg123); :class:`MockPlayer` just logs, for development
on Windows and for tests.

Both hold an internal lock around the actual playback so concurrent callers — the
scheduler, ``/api/test-bark`` and alarm mode — queue up instead of fighting over
one ALSA device (which matters on a 1 GB Pi).

**A subprocess exiting 0 is not proof that anything was heard.** ``mpg123`` given
a ``.wav`` decodes no MPEG frames and exits 0. ``ffplay`` renders through SDL; on
a headless systemd service there is no PulseAudio/PipeWire session for SDL to
find, and on some ffmpeg builds a failed ``SDL_OpenAudioDevice`` is logged as a
*warning* (dropped by ``-loglevel error``) while ffplay still runs to EOF and
exits 0. Either way ``subprocess.run(..., check=True)`` sees success. So every
backend here has its output actually inspected — see ``_record_result`` and
``_SILENT_FAILURE_PHRASES`` — not just its exit code.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("barkbox.player")

_MP3_SCALE_UNITY = 32768  # mpg123: this factor == original volume

# Which backends can actually decode which container. mpg123 is an MPEG-audio
# decoder *only* — handed a .wav it finds no MPEG frames, decodes nothing and
# still exits 0 (silent success). aplay is the reverse: PCM/WAV only, no mp3.
_BACKEND_FORMATS = {
    "mpg123": {".mp3"},
    "aplay": {".wav"},
    "ffplay": {".mp3", ".wav"},
    "ffmpeg": {".mp3", ".wav"},  # ffmpeg decode -> pipe -> aplay, see below
}
# Preference order for ``backend: auto``.
#   ffmpeg — ffmpeg decodes + applies -af volume, piped as WAV straight into
#            `aplay -D <device>`. No SDL, no Pulse/PipeWire in the path at all —
#            the exact ALSA route `speaker-test -D <device>` already proved
#            works. Needs both `ffmpeg` and `aplay` installed.
#   ffplay — universal + has volume, but goes through SDL (see module docstring).
#   aplay  — .wav only, no volume, but plain ALSA (ships with alsa-utils).
#   mpg123 — .mp3 only.
_AUTO_ORDER = ("ffmpeg", "ffplay", "aplay", "mpg123")

# Substrings (checked lowercased) that mean "no audio actually came out" even
# when the process's exit code was 0 — the silent-success trap this module
# exists to close.
_SILENT_FAILURE_PHRASES = (
    "could not open audio device",
    "failed to open audio device",
    "no such device",
    "no such file or directory",  # a bad -D/-a device name, from aplay/mpg123
    "device or resource busy",
    "unable to open slave",
    "playback open error",
)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def _looks_like_silent_failure(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in _SILENT_FAILURE_PHRASES)


def _backend_installed(name: str) -> bool:
    if name == "ffmpeg":
        return bool(shutil.which("ffmpeg")) and bool(shutil.which("aplay"))
    return bool(shutil.which(name))


def detect_backend() -> str:
    """Pick the best available CLI player, or ``"mock"`` if none is installed."""
    for name in _AUTO_ORDER:
        if _backend_installed(name):
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
        self._consecutive_failures = 0
        self._warned_fallback: set[str] = set()
        self._warned_no_gain = False

    def _resolve_backend(self, suffix: str) -> str:
        """The backend to actually use for a ``suffix`` file.

        The configured backend wins when it can decode the format; otherwise we
        transparently fall back to one that can (installed, and format-capable).
        This is the guard against the silent-no-sound trap — e.g. ``mpg123``
        (the default when only ``alsa-utils`` + ``mpg123`` are installed) handed
        a ``.wav``, which "succeeds" while producing nothing.
        """
        if suffix in _BACKEND_FORMATS.get(self.backend, {".mp3", ".wav"}):
            return self.backend
        for cand in _AUTO_ORDER:
            if cand != self.backend and suffix in _BACKEND_FORMATS[cand] and _backend_installed(cand):
                if cand not in self._warned_fallback:
                    self._warned_fallback.add(cand)
                    logger.warning(
                        "backend %r cannot play %s files - falling back to %r "
                        "for those (set audio.backend explicitly to silence this)",
                        self.backend, suffix, cand,
                    )
                return cand
        return self.backend  # nothing better available; let it fail loudly

    def _command(self, path: Path, volume: float = 1.0, backend: str | None = None) -> list[str]:
        """Build the argv for the single-process backends (not ``ffmpeg`` — that
        one is a two-process pipe, built in :meth:`_play_ffmpeg_piped`)."""
        p = str(path)
        gain = _clamp01(self.volume * volume)
        backend = backend or self.backend
        if backend == "mpg123":
            scale = str(int(gain * _MP3_SCALE_UNITY))
            cmd = ["mpg123", "-q", "-f", scale]
            if self.device and self.device != "default":
                cmd += ["-a", self.device]
            return cmd + [p]
        if backend == "ffplay":
            return [
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                "-af", f"volume={gain:.4f}", p,
            ]
        if backend == "aplay":
            cmd = ["aplay", "-q"]  # PCM straight through; no software gain
            if gain < 0.995 and not self._warned_no_gain:
                self._warned_no_gain = True
                logger.warning(
                    "aplay has no volume control - playing at 100%% and ignoring "
                    "gain %.2f (master_volume + distance simulation need "
                    "audio.backend: ffmpeg)", gain,
                )
            if self.device and self.device != "default":
                cmd += ["-D", self.device]
            return cmd + [p]
        raise ValueError(f"unsupported audio backend: {backend!r}")

    def _aplay_command(self) -> list[str]:
        cmd = ["aplay", "-q"]
        if self.device and self.device != "default":
            cmd += ["-D", self.device]
        return cmd

    def _play_env(self, backend: str) -> dict[str, str] | None:
        """Env for the ffplay subprocess. It renders through SDL; as a headless
        systemd service there is no PulseAudio/PipeWire session, so pin SDL to
        ALSA and point it at our device."""
        if backend == "ffplay" and self.device and self.device != "default":
            return {**os.environ, "SDL_AUDIODRIVER": "alsa", "AUDIODEV": self.device}
        return None

    def _play_ffmpeg_piped(self, path: Path, gain: float) -> tuple[bool, str]:
        """Decode + apply volume with ``ffmpeg``, stream raw WAV straight into
        ``aplay``. Bypasses ffplay/SDL (and therefore Pulse/PipeWire) entirely,
        landing on the exact ALSA device that ``speaker-test -D <device>``
        already proved works.
        """
        enc_cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-af", f"volume={gain:.4f}", "-f", "wav", "-",
        ]
        dec_cmd = self._aplay_command()
        logger.debug("piping: %s | %s", enc_cmd, dec_cmd)
        try:
            enc = subprocess.Popen(enc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return False, "ffmpeg not found on PATH"
        try:
            dec = subprocess.Popen(dec_cmd, stdin=enc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            enc.kill()
            enc.communicate()
            return False, "aplay not found on PATH"
        enc.stdout.close()  # only `dec` reads it now; lets `enc` get EPIPE if `dec` dies early
        dec_out, dec_err = dec.communicate()
        enc_err = enc.stderr.read()
        enc.wait()

        parts = []
        enc_err_s = (enc_err or b"").decode(errors="replace").strip()
        dec_err_s = (dec_err or b"").decode(errors="replace").strip()
        if enc_err_s:
            parts.append(f"ffmpeg: {enc_err_s}")
        if dec_err_s:
            parts.append(f"aplay: {dec_err_s}")
        stderr = " | ".join(parts)
        ok = enc.returncode == 0 and dec.returncode == 0
        if not ok:
            stderr = f"ffmpeg rc={enc.returncode} aplay rc={dec.returncode}" + (f": {stderr}" if stderr else "")
        return ok, stderr

    def _record_result(self, ok: bool, stderr: str, path: Path, backend: str) -> None:
        if ok:
            if stderr:
                # exit 0 but the process still said something - log it even
                # though we're not treating it as a failure, so it's visible
                # before it (maybe) turns into one.
                logger.info("playback ok (%s) via %s, stderr: %s", path.name, backend, stderr)
            if self._consecutive_failures:
                logger.warning("playback recovered after %d failed attempt(s)", self._consecutive_failures)
                self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        logger.error(
            "playback failed (%s) via %s [%d in a row]: %s",
            path.name, backend, self._consecutive_failures,
            stderr or "(process exited 0 but produced no recognizable audio)",
        )
        # A run of failures means the ALSA device is unusable (wrong card,
        # busy, permissions, or a broken snd_bcm2835 route -> "-524"). The
        # scheduler and the UI otherwise show a healthy "barking now" with no
        # sound, because a subprocess exiting 0 was treated as success.
        if self._consecutive_failures in (3, 30) or self._consecutive_failures % 300 == 0:
            logger.critical(
                "%d consecutive playback failures on device %r via %r - audio "
                "output is DOWN (check `aplay -l`, `speaker-test -D %s`, and "
                "that the service user is in the `audio` group)",
                self._consecutive_failures, self.device, backend, self.device,
            )

    def _play_impl(self, path: Path, volume: float) -> None:
        backend = self._resolve_backend(path.suffix.lower())
        gain = _clamp01(self.volume * volume)

        if backend == "ffmpeg":
            ok, stderr = self._play_ffmpeg_piped(path, gain)
            self._record_result(ok, stderr, path, backend)
            return

        cmd = self._command(path, volume, backend)
        try:
            proc = subprocess.run(cmd, capture_output=True, env=self._play_env(backend))
        except FileNotFoundError:
            self._record_result(False, f"{backend!r} not found on PATH", path, backend)
            return

        out = (proc.stdout or b"").decode(errors="replace").strip()
        err = (proc.stderr or b"").decode(errors="replace").strip()
        logger.debug("ran %s -> rc=%d stdout=%r stderr=%r", cmd, proc.returncode, out, err)

        combined = "\n".join(s for s in (err, out) if s)
        ok = proc.returncode == 0 and not _looks_like_silent_failure(combined)
        if not ok and proc.returncode == 0:
            combined = f"exit 0 but stderr looked like a failure: {combined}"
        elif not ok and not combined:
            combined = f"exit {proc.returncode}, no output"
        self._record_result(ok, combined, path, backend)


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
    device = audio_cfg.get("device", "default")
    logger.info("audio player: backend=%s device=%s", backend, device)
    return Player(
        backend=backend,
        device=device,
        volume=audio_cfg.get("volume", 1.0),
    )
