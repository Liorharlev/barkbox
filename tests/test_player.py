import logging
import subprocess
import threading
import time

import pytest

from barkbox.player import MockPlayer, Player, _BasePlayer, make_player


def test_make_player_mock():
    assert isinstance(make_player({"backend": "mock"}), MockPlayer)


def test_make_player_real():
    p = make_player({"backend": "mpg123", "device": "default", "volume": 0.5})
    assert isinstance(p, Player)
    assert p.backend == "mpg123"


@pytest.mark.parametrize(
    "backend, path, expected_head",
    [
        ("mpg123", "a.mp3", ["mpg123", "-q", "-f"]),
        ("ffplay", "a.mp3", ["ffplay", "-nodisp", "-autoexit"]),
        ("aplay", "a.wav", ["aplay", "-q"]),
    ],
)
def test_command_shapes(backend, path, expected_head):
    cmd = Player(backend, volume=1.0)._command(__import__("pathlib").Path(path))
    assert cmd[: len(expected_head)] == expected_head
    assert cmd[-1] == path


def test_mpg123_volume_scaling():
    cmd = Player("mpg123", volume=0.5)._command(__import__("pathlib").Path("a.mp3"))
    assert cmd[cmd.index("-f") + 1] == str(16384)


def test_mpg123_combines_base_and_per_call_volume():
    cmd = Player("mpg123", volume=0.5)._command(__import__("pathlib").Path("a.mp3"), 0.5)
    assert cmd[cmd.index("-f") + 1] == str(8192)   # 0.5 * 0.5 * 32768


def test_ffplay_uses_volume_filter():
    cmd = Player("ffplay", volume=1.0)._command(__import__("pathlib").Path("a.mp3"), 0.4)
    assert cmd[cmd.index("-af") + 1] == "volume=0.4000"
    assert "-volume" not in cmd


def test_ffplay_default_volume_is_base_gain():
    cmd = Player("ffplay", volume=0.9)._command(__import__("pathlib").Path("a.mp3"))
    assert cmd[cmd.index("-af") + 1] == "volume=0.9000"


def test_device_included_only_when_set():
    assert "-a" not in Player("mpg123", device="default")._command(__import__("pathlib").Path("a.mp3"))
    assert "-a" in Player("mpg123", device="hw:1,0")._command(__import__("pathlib").Path("a.mp3"))


def test_play_lock_serialises_concurrent_callers():
    class Tracker(_BasePlayer):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        def _play_impl(self, path, volume=1.0):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            time.sleep(0.03)
            self.active -= 1

    t = Tracker()
    threads = [threading.Thread(target=t.play, args=("x.mp3",)) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert t.max_active == 1


def test_detect_backend_prefers_ffplay(monkeypatch):
    import barkbox.player as mod

    monkeypatch.setattr(mod.shutil, "which", lambda n: n in {"ffplay", "aplay", "mpg123"})
    assert mod.detect_backend() == "ffplay"
    monkeypatch.setattr(mod.shutil, "which", lambda n: n in {"aplay", "mpg123"})
    assert mod.detect_backend() == "aplay"
    monkeypatch.setattr(mod.shutil, "which", lambda n: False)
    assert mod.detect_backend() == "mock"


def test_wav_never_dispatched_to_mpg123(monkeypatch):
    """The silent-no-sound bug: mpg123 handed a .wav 'succeeds' with no audio."""
    import barkbox.player as mod

    monkeypatch.setattr(mod.shutil, "which", lambda n: n in {"mpg123", "aplay"})
    p = Player("mpg123", device="plughw:0,0")
    seen = {}

    def capture(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", capture)
    p.play(__import__("pathlib").Path("bark.wav"))
    assert seen["cmd"][0] == "aplay"
    assert "-D" in seen["cmd"] and "plughw:0,0" in seen["cmd"]


def test_mp3_falls_back_off_aplay(monkeypatch):
    import barkbox.player as mod

    monkeypatch.setattr(mod.shutil, "which", lambda n: n in {"aplay", "mpg123"})
    assert Player("aplay")._resolve_backend(".mp3") == "mpg123"


def test_configured_backend_kept_when_format_fits(monkeypatch):
    import barkbox.player as mod

    monkeypatch.setattr(mod.shutil, "which", lambda n: True)
    assert Player("ffplay")._resolve_backend(".wav") == "ffplay"
    assert Player("mpg123")._resolve_backend(".mp3") == "mpg123"


def test_aplay_warns_once_when_gain_requested(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 0))
    p = Player("aplay", volume=0.5)
    with caplog.at_level(logging.WARNING, logger="barkbox.player"):
        p.play("a.wav")
        p.play("a.wav")
    assert sum("no volume control" in r.message for r in caplog.records) == 1


def test_ffplay_pins_sdl_to_alsa_device(monkeypatch):
    seen = {}

    def capture(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", capture)
    Player("ffplay", device="plughw:0,0").play("a.wav")
    assert seen["env"]["AUDIODEV"] == "plughw:0,0"
    assert seen["env"]["SDL_AUDIODRIVER"] == "alsa"


def _fail(cmd, **_k):
    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"ALSA lib ... Playback open error: -524")


def test_playback_failure_is_logged_and_swallowed(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", _fail)
    p = Player("mpg123")
    with caplog.at_level(logging.ERROR, logger="barkbox.player"):
        p.play("bark.mp3")  # must not raise
    assert p._consecutive_failures == 1
    assert "-524" in caplog.text


def test_repeated_failures_escalate_to_critical(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", _fail)
    p = Player("mpg123")
    with caplog.at_level(logging.CRITICAL, logger="barkbox.player"):
        for _ in range(3):
            p.play("bark.mp3")
    assert p._consecutive_failures == 3
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_failure_counter_resets_on_success(monkeypatch):
    calls = {"n": 0}

    def flaky(cmd, **_k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"boom")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", flaky)
    p = Player("mpg123")
    p.play("a.mp3")
    p.play("a.mp3")
    assert p._consecutive_failures == 2
    p.play("a.mp3")
    assert p._consecutive_failures == 0


def test_exit_zero_with_failure_phrase_still_counts_as_failed(monkeypatch, caplog):
    """The exact silent-success trap: rc=0 but the device never actually opened."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"Could not open audio device"),
    )
    p = Player("ffplay")
    with caplog.at_level(logging.ERROR, logger="barkbox.player"):
        p.play("a.wav")
    assert p._consecutive_failures == 1
    assert "Could not open audio device" in caplog.text


def test_exit_zero_clean_stderr_is_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b""))
    p = Player("ffplay")
    p.play("a.wav")
    assert p._consecutive_failures == 0


def test_ffmpeg_backend_pipes_into_aplay(monkeypatch):
    """audio.backend: ffmpeg — decode+volume via ffmpeg, output via aplay, no SDL."""
    import barkbox.player as mod

    seen = {}

    class _FakeStream:
        def __init__(self, data=b""):
            self._data = data

        def read(self):
            return self._data

        def close(self):
            pass

    class FakeProc:
        def __init__(self, cmd, **kw):
            seen[cmd[0]] = cmd
            self.returncode = 0
            self.stdout = _FakeStream() if kw.get("stdout") == subprocess.PIPE else None
            self.stderr = _FakeStream()

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd, **kw: FakeProc(cmd, **kw))
    p = Player("ffmpeg", device="plughw:0,0", volume=0.8)
    p.play("a.wav")
    assert seen["ffmpeg"][0] == "ffmpeg"
    assert "volume=0.8000" in seen["ffmpeg"]
    assert seen["aplay"][:2] == ["aplay", "-q"]
    assert "plughw:0,0" in seen["aplay"]
    assert p._consecutive_failures == 0


def test_ffmpeg_backend_failure_is_recorded(monkeypatch):
    import barkbox.player as mod

    class FakeStream:
        def __init__(self, data=b""):
            self._data = data

        def read(self):
            return self._data

        def close(self):
            pass

    class FakeEnc:
        def __init__(self, *_a, **_k):
            self.stdout = FakeStream()
            self.stderr = FakeStream(b"Input/output error")
            self.returncode = 1

        def wait(self):
            return None

    class FakeDec:
        def __init__(self, *_a, **_k):
            self.returncode = 1

        def communicate(self):
            return b"", b"aplay: main:828: audio open error: Device or resource busy"

    calls = iter([FakeEnc(), FakeDec()])
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: next(calls))
    p = Player("ffmpeg")
    p.play("a.wav")
    assert p._consecutive_failures == 1


def test_mock_player_returns_elapsed():
    elapsed = MockPlayer(simulate_sleep=False).play("bark.mp3")
    assert elapsed >= 0


def test_mock_player_fixed_secs():
    # fixed_secs drives the simulated playback time (timer granularity can wake
    # a hair early on Windows, so don't assert the exact value)
    fast = MockPlayer(fixed_secs=0.0, simulate_sleep=True).play("bark.mp3")
    slow = MockPlayer(fixed_secs=0.2, simulate_sleep=True).play("bark.mp3")
    assert slow > fast
    assert slow >= 0.15
