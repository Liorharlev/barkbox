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
