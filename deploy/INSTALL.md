# Installing barkbox on the Raspberry Pi

Assumes the repo is checked out at `/home/dogpi/barkbox` and you are logged in
over SSH as user `dogpi`.

The quickest path is `bash deploy/install.sh`, which does everything below. The
manual steps:

## 1. Get the code

```bash
git clone <repo-url> /home/dogpi/barkbox
cd /home/dogpi/barkbox
```

## 2. System packages + virtualenv

```bash
sudo apt-get update
sudo apt-get install -y python3-venv ffmpeg mpg123 alsa-utils
sudo usermod -aG audio "$(id -un)"    # the service needs to open /dev/snd/*
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

**Before going further, verify audio at the OS level** — this catches real
Raspberry Pi OS / hardware issues that are unrelated to barkbox and will bite
`speaker-test` too, not just the app (see "Audio troubleshooting" below if
either check fails):

```bash
aplay -l                          # the card should be listed, e.g. "card 0: Headphones"
speaker-test -t wav -c 2 -D plughw:0,0
```

## 3. Config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` if needed (the web UI also writes to it). It is git-ignored,
so your settings stay local to the Pi.

## 4. Install and start the units

```bash
chmod +x deploy/logsync.sh
sudo cp deploy/barkbox.service deploy/barkbox-logsync.service deploy/barkbox-logsync.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barkbox
sudo systemctl enable --now barkbox-logsync.timer
```

`enable --now barkbox` starts it immediately and every boot; `Restart=on-failure`
brings it back if it crashes. The timer flushes the RAM log to the SD card once a
day (see below).

If the checkout is not at `/home/dogpi/barkbox` or the user is not `dogpi`, edit
those paths in the three unit files first (or just run `deploy/install.sh`, which
rewrites them automatically).

## 5. Check it

```bash
systemctl status barkbox
journalctl -u barkbox -f
systemctl list-timers barkbox-logsync.timer
speaker-test -c2 -twav                        # verify the speaker
curl -X POST localhost:8080/api/test-bark     # one real bark
```

Web UI: `http://<pi-address>:8080`

## Logging — RAM at runtime, daily backup to the card

To spare the SD card, the running service writes its log **only to tmpfs (RAM)**:

| where | path | notes |
|-------|------|-------|
| runtime log | `/run/barkbox/barkbox.log` | tmpfs (`RuntimeDirectory=barkbox`); lives in RAM, gone on reboot |
| archived log | `/var/log/barkbox/barkbox.log` | on the SD card; `.1`..`.5` are rotated older copies |

`deploy/logsync.sh` moves the runtime log into the archive:

- **once a day** — `barkbox-logsync.timer` (`OnCalendar=daily`, `Persistent=true`,
  so a missed run catches up on next boot);
- **on every stop / restart / shutdown** — `ExecStopPost` in `barkbox.service`.

So a clean reboot loses nothing; only a hard power-cut loses the lines written
since the last daily flush (`journalctl -u barkbox` still has most of them —
Raspberry Pi OS keeps the journal in RAM by default too).

Watch or read the logs:

```bash
tail -F /run/barkbox/barkbox.log          # live, from RAM (-F survives the daily flush)
cat /var/log/barkbox/barkbox.log          # everything flushed to the card
sudo systemctl start barkbox-logsync      # force a flush now
```

`/run` is always a tmpfs on Raspberry Pi OS, so no `/etc/fstab` entry or extra
mount is needed — systemd creates `/run/barkbox` from the unit.

## Audio troubleshooting

The first real deploy (2026-09-04) hit a stack of five audio problems, none of
them barkbox bugs alone. If sound doesn't come out — from `speaker-test` *or*
from `curl -X POST localhost:8080/api/test-bark` — work through these in order;
full narrative + exact commands are in `SESSION_NOTES.md`.

1. **`speaker-test` itself fails** with `Playback open error: -524` — a
   `config.txt` conflict, not a barkbox issue. Check `cat /proc/cmdline` for
   doubled `snd_bcm2835.enable_headphones=`/`enable_hdmi=` values (the firmware
   injects these while expanding `config.txt` — they're never written there
   literally). Usually `dtparam=audio=on` fighting `dtoverlay=vc4-kms-v3d` over
   who owns HDMI audio. Fix: `dtoverlay=vc4-kms-v3d,noaudio` in
   `/boot/firmware/config.txt` (edit the existing line, don't add a second
   one), `sudo reboot`.
2. **Still `-524` after that**, or `aplay -l` looks fine but nothing plays —
   check for a stale `~/.asoundrc` pointing at a card number that doesn't exist
   (leftover from an old tutorial/image). Delete it.
3. **`speaker-test` works but barkbox is silent, no error in the log** —
   `audio.backend: auto` picked a backend that can't decode your clip format.
   `mpg123` is mp3-only; handed a `.wav` it exits 0 having decoded nothing. Set
   `audio.backend` explicitly (`ffmpeg` or `ffplay` — see `config.example.yaml`)
   or just let `auto` prefer `ffmpeg`/`ffplay` (it does by default; this only
   bites if `ffmpeg` isn't installed, which `install.sh`/step 2 above fixes).
4. **Still silent with `backend: ffplay` and still no error** — `ffplay` renders
   through SDL, which needs a Pulse/PipeWire session a plain systemd service
   doesn't have; a failed device-open can be swallowed below `-loglevel error`
   so `ffplay` exits 0 with nothing heard. Prefer `audio.backend: ffmpeg`
   instead (decodes + applies volume, then pipes straight into `aplay` — no SDL
   involved at all). If you must use `ffplay`, `player.py` pins
   `SDL_AUDIODRIVER=alsa` + `AUDIODEV=<device>` for it automatically as long as
   `audio.device` isn't `default`. Either way, set `BARKBOX_LOG_LEVEL=DEBUG`
   (`sudo systemctl edit barkbox`) to see the exact command run and its
   stdout/stderr on every play, success or not.
5. **`audio.backend: <something>` makes the service crash-loop** (`systemctl
   status barkbox` shows repeated restarts, `journalctl -u barkbox` shows a
   `ConfigError`) — the value isn't in `config.py`'s `_AUDIO_BACKENDS`. As of
   this writing that's derived straight from `player.py`'s own backend list, so
   this should no longer happen for a backend the code actually supports; if it
   still does, that's a real bug — check `python -c "from barkbox.player import
   _AUTO_ORDER; print(_AUTO_ORDER)"` against what `config.yaml` says.

**Robustness note:** `plughw:0,0` addresses the card *by index*. If a USB audio
device is ever added, card indices can shift and silently repoint `0` at the
wrong device. `plughw:CARD=Headphones,DEV=0` (see `aplay -l` for the exact
name) is equivalent today and immune to that.

## Updating later

```bash
cd /home/dogpi/barkbox
git pull
venv/bin/pip install -r requirements.txt
chmod +x deploy/logsync.sh
sudo cp deploy/barkbox.service deploy/barkbox-logsync.service deploy/barkbox-logsync.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart barkbox
```
