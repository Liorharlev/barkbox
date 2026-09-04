# barkbox — session notes

_Working notes for picking the project back up quickly. Last updated: 2026-09-04._

## 2026-09-04 (final) — audio confirmed working end-to-end. Full bug chain + lessons.

**Confirmed on the Pi (`dogpi@10.0.0.8`), by the user, this session:**
`audio.backend: ffplay`, `device: plughw:0,0` → `curl -X POST
localhost:8080/api/test-bark` produces real sound, **survives a full
`sudo reboot`** (service auto-starts, audio still works), and several different
behaviors tested through the UI all play correctly. **Do not change
`audio.backend` away from `ffplay` on this Pi without re-testing** — it's the
one combination actually heard coming out of the speaker.

### The five-bug chain, in the order they were found

| # | Symptom | Root cause | Where it's fixed | Tracked in git? |
|---|---|---|---|---|
| a | `speaker-test` → `Playback open error: -524` | `dtparam=audio=on` vs `dtoverlay=vc4-kms-v3d` both claiming audio; firmware injects contradictory `snd_bcm2835.*` kernel params not visible in `config.txt`/`cmdline.txt` | Manual edit: `dtoverlay=vc4-kms-v3d,noaudio` in `/boot/firmware/config.txt` | **No — a Pi system file, not in this repo.** Documented in `deploy/INSTALL.md` "Audio troubleshooting" §1 |
| b | Still `-524` / wrong card | Stale `~/.asoundrc` pointing at a nonexistent card 1 | Deleted on the Pi | **No — a dotfile.** Documented in INSTALL.md §2 |
| c | `speaker-test` OK, barkbox silent, no error logged | `mpg123` (auto-picked) can't decode `.wav`; the whole clip library is `.wav` | `player.py` `detect_backend()` order + per-file fallback (`_resolve_backend`); `install.sh` installs `ffmpeg` | **Yes** — `src/barkbox/player.py`, `deploy/install.sh` |
| d | `ffplay` set explicitly, still silent, no error | `ffplay` renders via SDL, which needs a Pulse/PipeWire session a plain systemd service doesn't have; a failed device-open can log below `-loglevel error` and still exit 0 | `SDL_AUDIODRIVER=alsa`/`AUDIODEV=<device>` pinned for ffplay (this is what actually fixed sound on the Pi); output-inspection (`_looks_like_silent_failure`) as a safety net if it ever silently fails again; `audio.backend: ffmpeg` (ffmpeg-decode piped into `aplay`, no SDL at all) as a more robust alternative, now usable but not yet the one running | **Yes** — `src/barkbox/player.py` |
| e | `audio.backend: ffmpeg` → service **crash-loops**, `journalctl` shows `ConfigError` | My mistake this session: I added the `"ffmpeg"` backend to `player.py` but never added it to `config.py`'s `_AUDIO_BACKENDS` validation allowlist. `app.main()`'s `load_config()` call is unguarded, so the `ConfigError` killed the process every boot and `Restart=on-failure` looped it every 5s until fixed by hand. | `_AUDIO_BACKENDS` in `config.py` is now `("auto", *player._AUTO_ORDER, "mock")` — **derived from `player.py`, not hand-duplicated** — plus a regression test (`test_every_real_player_backend_is_a_valid_config_value`) that walks every real backend through `save_config`/`load_config` | **Yes** — `src/barkbox/config.py`, `tests/test_config.py` |

(a) and (b) are Pi/OS state, outside this repo by nature — a config.txt
overlay flag and a stray dotfile aren't things `git pull` can fix. (c), (d),
(e) are all in application code, covered by tests, and ship with every
`git pull`.

### Does the fix survive reboot / `git pull` / `apt upgrade`?

- **Reboot** — confirmed directly (see top). (a)/(b) are boot-time firmware/OS
  config, unaffected by an app restart; (c)/(d)/(e) are code, unaffected by
  reboot.
- **Future `git pull`** — (c)/(d)/(e) ship in the repo and apply automatically.
  (a)/(b) do **not** — a fresh SD card / re-flash starts over on those two.
  `deploy/INSTALL.md`'s new "Audio troubleshooting" section is the durable
  record for that case (this file is developer notes, not shipped to a fresh
  install the same way).
- **`apt upgrade`** — installed packages (`ffmpeg`, `mpg123`, `alsa-utils`)
  aren't removed by upgrades. `config.txt`/`.asoundrc` aren't touched by
  package upgrades either (not dpkg-managed). The one real fragility: `plughw:
  0,0` addresses the sound card **by index** — plugging in a USB audio device
  later could shift indices and silently repoint it. Noted in
  `config.example.yaml` and INSTALL.md; not fixed proactively since it's not
  the current setup, but worth revisiting (`plughw:CARD=Headphones,DEV=0` is
  the stable form) if hardware ever changes.

### What we learned (so we don't get stuck on this again)

1. **A subprocess exiting 0 is not proof of anything acoustic.** Both `mpg123`
   (wrong format) and `ffplay` (SDL with no audio session) exited 0 while
   producing no sound. `player.py` now inspects `stdout`/`stderr` on *every*
   exit code, not just non-zero ones — that's the actual fix for "the log says
   success but there's no sound," independent of which specific backend is in
   use.
2. **When you add a new value to one allowlist, grep for every other place the
   same value has to be accepted.** Bug (e) happened because `player.py` and
   `config.py` each kept their own list of valid backends. Fixed by making one
   derive from the other; the general lesson — a "backend"/"mode"/"type" enum
   almost always exists in more than one file, and only a test that actually
   exercises the full one (`config.py` validation, not just `player.py`
   directly) catches the mismatch. `tests/test_player.py` had 152 passing
   tests and never once ran a config value through `config.py`.
3. **A crash-looping systemd service can look exactly like a silently-broken
   one from the UI/API side** — `curl` to a dead service just fails to
   connect, and `Restart=on-failure` can make `systemctl status` flicker
   between "activating" and "failed" fast enough to be easy to misread.
   `journalctl -u barkbox -n50` is the first move whenever a config change
   doesn't do what's expected, before assuming the *logic* is wrong.
4. **OS-level audio config (config.txt, `.asoundrc`, ALSA card numbering) and
   application-level playback (which CLI player, which arguments) are two
   independent layers that both have to work**, and a failure in either one
   looks identical from the app's side ("no sound"). `speaker-test -D
   <device>` in a plain shell is the fastest way to tell which layer you're
   debugging.
5. **`config.example.yaml`'s comments and `deploy/INSTALL.md` are the two
   places future-us (or future deploys) will actually read** — this session's
   fixes are only as durable as those docs, which is why the "Audio
   troubleshooting" section in INSTALL.md restates the whole chain, not just
   this file.

### Branch / commit status

`fix/pi-audio-playback` (Bugs c+d fix) was reviewed, tested (152 tests) and
**merged to `main` at the user's request** (fast-forward, commit `46ceac3`,
pushed to `origin/main`). This session's follow-up (Bug e — the `config.py`
validation gap the merge itself exposed — plus the INSTALL.md troubleshooting
section) is a further commit on top of `main`; see the top of `git log` for the
hash. No open branch needs merging as of this writing.

## 2026-09-04 (pm, cont'd) — Bug 3: ffplay "succeeds" with zero process running

After Bugs 1 (ALSA `-524`) and 2 (`mpg123` can't play `.wav`) below were both
fixed — `ffmpeg` installed, `audio.backend: ffplay`, `device: plughw:0,0`,
service restarted — **still silent**. The decisive check: press "test bark" in
the UI and immediately `ps aux | grep -iE "ffplay|mpg123|aplay"` → **no process
at all**, while `journalctl -u barkbox` still shows a clean
`played:chase 15 bark(s), 32s` / `test_bark chase`, no error.

**Where in `player.py` this happens, traced line by line (pre-fix code):**
`Player._play_impl` → `subprocess.run(cmd, check=True, capture_output=True)`.
This line *is* reached and *does* spawn `ffplay` for all 15 barks — if it
weren't, an unhandled exception from `_command()`/`subprocess.run()` would
propagate out of `_run_episode`'s bark loop (nothing there catches it) and
`events.add("played:...")`, which runs *after* the loop, would never fire. Since
it did fire cleanly 15 times, every `subprocess.run()` call returned rc 0. So
`ffplay` really did run — the `ps aux` miss is just timing (each invocation is
short; the "32s" is mostly the `intra_gap_seconds` sleeps *between* barks, not
ffplay itself running long).

**The actual bug:** on `check=True` success, the code **never looked at
`stdout`/`stderr` at all** — `capture_output=True` was capturing them into an
object that was then discarded. `ffplay` renders audio through **SDL**; as a
plain `systemd` service (`Type=simple`, no login session) there is no
PulseAudio/PipeWire session for SDL to attach to. Depending on the ffmpeg
build, a failed `SDL_OpenAudioDevice` is either non-fatal (ffplay decodes to
EOF with no audio callback and exits 0 almost immediately) or logged as a
`WARNING`-level line that `-loglevel error`/`-loglevel quiet` both suppress —
either way, **nothing failed loudly enough for `check=True` to notice**, and
the discarded stderr took the actual reason with it. This is the "exception
swallowed silently" the request suspected, except it wasn't a `try/except` at
fault — it was **output nobody read**.

**Fix — `src/barkbox/player.py`, two changes:**

1. **Every backend's `stdout`/`stderr` is now inspected, on every exit code.**
   `_looks_like_silent_failure()` scans (case-insensitively) for phrases like
   `"could not open audio device"`, `"device or resource busy"`,
   `"playback open error"` — if any show up, that play counts as a **failure
   even when the process exited 0**, feeding the same consecutive-failure
   counter/CRITICAL escalation from Bug 2's fix. `BARKBOX_LOG_LEVEL=DEBUG` now
   logs the exact argv + full stdout/stderr for *every* play, success or not.
2. **New `audio.backend: ffmpeg`** (now the top of the `auto` preference
   order): `ffmpeg -af volume=... -f wav -` piped directly into
   `aplay -D <device>`. This is `Player._play_ffmpeg_piped` — two
   `subprocess.Popen`s joined by a pipe. It removes SDL (and therefore
   Pulse/PipeWire) from the picture **entirely**; the only thing actually
   opening the sound card is `aplay`, on the exact same ALSA device that
   `speaker-test -D plughw:0,0` already proved works. `ffplay` stays available
   (with `SDL_AUDIODRIVER=alsa`/`AUDIODEV=<device>` pinned in its env, from the
   earlier fix) as a fallback for a system without `aplay`, which won't happen
   here since `alsa-utils` is always installed.
3. `deploy/install.sh` now also does `usermod -aG audio <user>` (a permission
   gap would show the same "silent success" symptom) and prints a tip to set
   `BARKBOX_LOG_LEVEL=DEBUG` when audio is still silent with no error.
4. `+11` tests (piped success/failure, exit-0-but-failure-phrase, clean exit-0)
   → **152 total**, all green.

**On the Pi:** `git pull`, set `audio.backend: ffmpeg` in `config.yaml` (device
stays `plughw:0,0`), `sudo systemctl restart barkbox`, then `curl -X POST
localhost:8080/api/test-bark` — **I could not run `ps aux` / listen myself, no
SSH access from this session** (`Permission denied (publickey,password)` on
`dogpi@10.0.0.8`) — so this still needs a human ear on the speaker to close the
loop.

> **⚠ Correction (see the final summary at the top of this file):** this
> instruction was wrong — `"ffmpeg"` was missing from `config.py`'s
> `_AUDIO_BACKENDS` allowlist, so setting it crash-looped the service. Fixed
> now (allowlist derived from `player.py` directly); `ffplay` is what's
> actually confirmed working on the Pi.

If `ffmpeg` is *still* silent: `sudo systemctl edit barkbox` → add
```
[Service]
Environment=BARKBOX_LOG_LEVEL=DEBUG
```
`sudo systemctl restart barkbox`, test-bark again, `journalctl -u barkbox -n50`
— it will now print the exact `ffmpeg | aplay` argv and both processes' stderr
even on "success".

## 2026-09-04 (pm) — "no sound on the Pi" was TWO stacked bugs, not a regression

The Pi never actually made a sound. "It worked before" is **Windows dev only** —
there `detect_backend()` picks `ffplay` (ffmpeg installed) which plays the `.wav`
clips fine. On the Pi two independent problems sat on top of each other, and
**no git commit caused either**: `git grep` confirms nothing in the repo has
ever touched `/boot`, `cmdline`, `config.txt`, `modprobe`, overlayfs or kernel
params, and `d805741` is pure user-space (tmpfs logging + systemd
`RuntimeDirectory=`/`LogsDirectory=`, which are not sandboxing options — no
`PrivateDevices`/`DeviceAllow`). `d805741` is just the commit where we first ran
`deploy/install.sh` on real hardware.

### Bug 1 — ALSA `-524` (`ENOTSUPP`) opening the PCM  →  FIXED on the Pi

`speaker-test -t wav -c 2` failed with `Playback open error: -524`. `aplay -l`
showed `card 0: Headphones [bcm2835 Headphones]` (card healthy); `dmesg` showed
the **kernel command line** carrying contradictory `snd_bcm2835.enable_hdmi=1 …
=0` / `enable_headphones=0 … =1` — none of it literally in
`config.txt`/`cmdline.txt` because the **firmware appends it** while expanding
`config.txt`. Cause: `dtparam=audio=on` (analog via `snd_bcm2835`) **and**
`dtoverlay=vc4-kms-v3d` (full KMS, also claims HDMI audio) each emit an audio
param set and they collide, leaving `snd_bcm2835` half-initialised so the
default PCM opens `-524`. Compounded by a stale `~/.asoundrc` pointing at a
non-existent `card 1`.

Resolution applied on the Pi (2026-09-04):
1. `config.txt`: added `,noaudio` to the existing line → `dtoverlay=vc4-kms-v3d,noaudio`
   (did **not** add a second line). Kept `dtparam=audio=on`.
2. Deleted the stale `~/.asoundrc`.
3. Verified `speaker-test -t wav -c 2 -D plughw:0,0` → audible.

If it regresses: `/etc/asound.conf` with `defaults.pcm.card 0` /
`defaults.ctl.card 0`; `amixer -c 0 cset numid=3 1` to force the jack; check
`/etc/modprobe.d/*.conf` for a stray `options snd_bcm2835 …`; confirm `dogpi` is
in the `audio` group (`groups dogpi`).

### Bug 2 — `mpg123` cannot play the `.wav` library  →  FIXED in code this session

ALSA now worked, still silence, but `journalctl` said `played:chase 14 bark(s)`
with **no error**. Cause: **every clip in `sounds/` is a `.wav`** (Freesound +
the `anton_bark_*` recordings — zero `.mp3`s), and `install.sh` installed only
`mpg123` + `alsa-utils`, so `detect_backend()` returned `mpg123`. **`mpg123` is
an MPEG-audio decoder only** — handed a `.wav` it finds no MPEG frames, decodes
nothing, and *still exits 0* (silently, with `-q`). Fire-and-forget `play()` +
`state.now_playing` → the UI shows a healthy green bark.

The `audio.device` guess (`plughw:0,0`) was **not** the problem — `mpg123 -a
plughw:0,0` is valid; it was never going to emit audio for a WAV regardless.

Code fix (`src/barkbox/player.py`, `deploy/install.sh`, `config.example.yaml`):
- `detect_backend()` order is now **`ffplay` → `aplay` → `mpg123`** (ffplay is
  the only one that plays both formats AND can attenuate).
- **Per-file backend resolution** (`Player._resolve_backend`): if the configured
  backend can't decode a file's extension, transparently fall back to one that
  can (`_BACKEND_FORMATS`: mpg123=`.mp3`, aplay=`.wav`, ffplay=both), with a
  one-time `WARNING`. So `mpg123`+`.wav` → `aplay`; `aplay`+`.mp3` → mpg123/ffplay.
- `aplay` path logs a one-time `WARNING` when gain <1.0 is asked for (aplay has
  no volume — plays at 100%; `master_volume` + distance simulation need ffplay).
- `ffplay` runs with `SDL_AUDIODRIVER=alsa` + `AUDIODEV=<device>` in its env
  when `audio.device` != `default` (headless service has no Pulse/PipeWire for
  SDL), and `-loglevel error` (was `quiet`, which hid real errors).
- `install.sh` now also installs **`ffmpeg`**.
- `Player._play_impl` counts consecutive failures → `CRITICAL` at 3 / 30 / every
  300 ("audio output is DOWN") instead of one quiet `ERROR`.
- `+9` tests → **148 total**, all green.

### Do this on the Pi to finish

```bash
cd /home/dogpi/barkbox && git pull
sudo apt install -y ffmpeg
```
`config.yaml` → `audio: {backend: ffplay, device: plughw:0,0}` (or `default`
now that HDMI audio is off). `sudo systemctl restart barkbox`, then
`curl -X POST localhost:8080/api/test-bark` and **confirm audible sound**, not
just exit 0. Without ffmpeg it still plays via `aplay` (auto), at fixed 100%.

### "Why now?" — for future reference

Not a PipeWire/firmware regression. To rule that out anyway, on the Pi:
`grep -iE 'pipewire|wireplumber|linux-image|raspi-firmware|alsa' /var/log/apt/history.log`
(and `zgrep` the rotated `.gz`). Even if something updated around Sept 4–5,
Bug 2 explains the silence on its own — mpg123 was never the right player for an
all-WAV library. **Lesson: `install.sh` must install a backend matching the clip
formats in `sounds/`, and a player's "exit 0" is not proof of sound.**

<details><summary>original -524 triage notes (superseded by the summary above)</summary>

**Symptom.** After the deploy, UI is green ("barking now"), no errors on screen,
but nothing comes out of the speaker. On the Pi, `speaker-test -t wav -c 2`
fails with `Playback open error: -524, Unknown error 524` (that's `ENOTSUPP`).
`aplay -l` shows `card 0: Headphones [bcm2835 Headphones]` (card is fine).
`dmesg` shows the **kernel command line** carrying contradictory
`snd_bcm2835.enable_headphones=0 … enable_headphones=1` /
`enable_hdmi=1 … enable_hdmi=0`, even though `/boot/firmware/config.txt` and
`cmdline.txt` don't contain that text.

**`git show d805741` — cleared.** That commit is purely user-space: a Python
`WatchedFileHandler` to a tmpfs path (`_setup_logging()` in `src/barkbox/app.py`),
systemd `RuntimeDirectory=`/`LogsDirectory=` (neither is a sandboxing option —
no `PrivateDevices`, `ProtectSystem`, `DeviceAllow` anywhere), `deploy/logsync.sh`
(a `mv`+`cat` of a log file), and docs. **Nothing** in the repo touches
`/boot`, `cmdline`, `config.txt`, `modprobe`, overlayfs, a `/boot` remount, or
kernel params — `git grep` confirms. And `speaker-test` fails in a plain SSH
shell with the service stopped, so the unit can't be the cause. The timing is
coincidental: this was the **first run of `deploy/install.sh` on real hardware**
(`apt-get update` + `apt-get install alsa-utils` + a fresh-image reboot), which
is when a pre-existing `config.txt` / KMS audio misconfiguration first took
effect.

**Root cause of `-524`.** The contradictory `snd_bcm2835.*` params are **appended
to the kernel command line by the firmware** (`start*.elf`) as it expands
`config.txt` — they are not meant to appear literally in `cmdline.txt`. Two
directives each generate an audio param set and they collide: `dtparam=audio=on`
(analog / headphones via `snd_bcm2835`) **plus** `dtoverlay=vc4-kms-v3d` (full
KMS, which also claims HDMI audio). With both active the `snd_bcm2835` route ends
up half-initialised and the ALSA *default* PCM opens with `-524`.

**Fix (on the Pi, `dogpi@10.0.0.8`) — do these in order, test after each:**

1. Confirm the collision:
   `cat /proc/cmdline` (see the doubled `snd_bcm2835.*`),
   `cat /boot/firmware/config.txt | grep -nE 'audio|vc4|dtoverlay|dtparam'`.
2. In `/boot/firmware/config.txt`, make the two directives stop fighting — keep
   analog, tell KMS to leave audio alone:
   ```
   dtparam=audio=on
   dtoverlay=vc4-kms-v3d,noaudio
   ```
   (i.e. add `,noaudio` to the existing `vc4-kms-v3d` line; don't add a second
   line). `sudo reboot`, then `speaker-test -t wav -c 2`.
3. If step 2 doesn't clear it, force the analog card as the ALSA default —
   `/etc/asound.conf`:
   ```
   defaults.pcm.card 0
   defaults.ctl.card 0
   ```
   and route to the jack: `amixer -c 0 cset numid=3 1` (1 = headphones,
   2 = HDMI, 0 = auto).
4. Check `/etc/modprobe.d/*.conf` for a stray `options snd_bcm2835 …` line
   (an old tutorial's leftover) and delete it if present.
5. Bookworm audio goes through PipeWire/WirePlumber. If `speaker-test` now works
   but only when nothing else is open, the service user may be racing the user
   session — run barkbox against the card directly: set `audio.device` in
   `config.yaml` to `plughw:CARD=Headphones,0` (bypasses the default PCM).
   Also make sure `dogpi` is in the `audio` group: `groups dogpi`,
   `sudo usermod -aG audio dogpi` if not (needs a service restart / relogin).

**Code change made this session (`src/barkbox/player.py`).** `Player._play_impl`
still swallows a failed playback (so one bad clip can't kill an episode), but now
counts consecutive failures and logs `CRITICAL` at 3 / 30 / every 300 —
`journalctl -u barkbox` (and `/var/log/barkbox/barkbox.log`) will now shout
"audio output is DOWN" instead of a quiet single `ERROR` line. This is why the
UI looked healthy: the scheduler treats `play()` as fire-and-forget and
`state.now_playing` goes green regardless of the subprocess exit code.
`+3` tests (142 total).

</details>

## 2026-09-04 — deployed to the Pi + deploy hardening

**The Pi is now live.** Step 1 is no longer "dev only" — the service runs on the
hardware.

### On the Pi (done + verified)

- **systemd service installed and running.** `barkbox.service` is
  `enable`d (auto-starts at boot) and active. `Restart=on-failure` +
  `RestartSec=5` — verified it comes back on its own after a crash / `kill`.
  Checkout at `/home/dogpi/barkbox`, runs as user `dogpi`
  (was `/home/pi/dog-bark-deterrent`).
- **Static IP** `10.0.0.8/24` on `wlan0`, gateway + DNS `10.0.0.138`, set
  client-side in `/etc/dhcpcd.conf` on the Pi (not a router-side DHCP
  reservation). Control UI: `http://10.0.0.8:8080`. SSH: `ssh dogpi@10.0.0.8`.
- **RAM logging + daily SD backup — installed and verified.** Runtime log lives
  in tmpfs at `/run/barkbox/barkbox.log` (no SD wear); `deploy/logsync.sh`
  appends it to `/var/log/barkbox/barkbox.log` on the card once a day
  (`barkbox-logsync.timer`, `Persistent=true`) and on every stop/reboot
  (`ExecStopPost`). Confirmed: runtime log fills in RAM, archive gets the lines
  after a manual `systemctl start barkbox-logsync`, no lines lost across a
  restart.

### Code / repo changes (committed? check `git log`)

- `src/barkbox/app.py` `_setup_logging()`: console always; if `BARKBOX_LOG_DIR`
  is set, also a `WatchedFileHandler` at `<dir>/barkbox.log`. The unit sets
  `BARKBOX_LOG_DIR=/run/barkbox` + `RuntimeDirectory=barkbox` (tmpfs,
  `RuntimeDirectoryPreserve=yes`), `LogsDirectory=barkbox` → `/var/log/barkbox`.
- `deploy/logsync.sh` moves the RAM log by `mv` + append — lossless because
  `WatchedFileHandler` recreates the file. Self-rotates the on-card archive past
  ~5 MB (`barkbox.log.1`..`.5`).
- New files: `deploy/logsync.sh`, `deploy/barkbox-logsync.{service,timer}`,
  `deploy/INSTALL.md`. `install.sh` installs all three units + enables the timer.
- No `config.yaml` schema change — logging is env-driven like `BARKBOX_LOG_LEVEL`.
- 139 tests still pass.

### Still open

- `dhcpcd.conf` static IP is client-side only — if the Pi is reimaged, redo it
  (or move it to a router reservation). `dhcpcd` is also deprecated on Pi OS
  Bookworm (NetworkManager is default); worked here, but revisit if it regresses.
- Journald persistence: Raspberry Pi OS keeps the journal in RAM by default
  (`/var/log/journal` absent) — fine, leave it; revisit only if you want boot
  history to survive power cuts.

## What this is

Dog **presence simulation** for a Raspberry Pi 3B + weatherproof outdoor speaker.
Goal: one believable dog with a plausible daily rhythm so anyone listening from
outside believes a dog lives here, even when the house is empty. Not an alarm
siren — alarm mode is a sub-case.

Status: **deployed and running on the Pi** (2026-09-04) — `barkbox.service` active,
auto-restarts on failure, auto-starts at boot; static IP in `/etc/dhcpcd.conf`;
RAM logging + daily SD backup in place. See the 2026-09-04 section above.
Still dev on Windows too: `python run.py` → control UI on http://localhost:8080
(or double-click `start_server.bat`, which cd's in, launches the server and opens
the browser ~4 s later).
Audio backend auto-detects (`ffplay` on this machine via ffmpeg; `mpg123` planned on the Pi; `mock` = log only).

---

## 2026-08-29 session — what changed (all in one commit)

Feature work on top of `24eef6c`. Details in the sections below; quick index:

1. **Distance simulation** (`src/barkbox/volume.py`, new) — per-bark volume random
   walk for scheduled episodes whose target duration > 6 s, so the dog sounds like
   it's moving around. Config-only (`distance_simulation` block).
2. **Master volume** (`audio.master_volume`, 0–100, UI slider) — global level in
   place of a knob on the speaker. Applies to scheduled episodes **and manual test
   barks** (so a test previews the real level). Alarm still plays full volume — a
   deliberate hold, revisit later. ffplay switched from `-volume` to `-af volume=`.
3. **Frequency presets + Home multiplier are UI-editable** — "✎ Edit ranges" table
   per preset, "slow down by ×N" field, live effective-range readout.
4. **Behavior weights as a qualitative slider** — `Rare / Occasional / Frequent /
   Constant` (→ 1/2/4/8), no raw numbers; plus a read-only "out of every 10 events" line.
5. **Live "barking now" light** (green) per behavior row + **red "alarm active"
   dot** next to Trigger alarm — both off `GET /api/now-playing`, polled 250 ms.
6. **Master switch now stops an active alarm** immediately, and a **"Stop alarm"**
   button (`POST /api/alarm-stop`) ends one early.
7. **Manual test barks interrupt each other** — a fresh press cancels the running
   one right away (`_run_episode` got a `cancelled` hook).
8. **Schedule "Always" mode** fully hides the from/to row (was showing it, due to a
   CSS specificity bug where `#schedule-times`'s `display:flex` beat `[hidden]`).

139 tests pass (`venv\Scripts\pytest -q`), up from 83.

---

## What exists today

### Control model — three independent layers

| layer | field | values | effect |
|---|---|---|---|
| master switch | `enabled` | true / false | false = total silence |
| schedule | `schedule.mode` | `always` / `active_window` / `quiet_window` | is the system active at this time of day at all |
| presence | `presence.mode` | `home` / `away` | activity-rate multiplier (`home: 0.25` → 4× longer gaps; `away: 1.0`) |

- `active_window` = active **only** between start–end. `quiet_window` = silent
  **only** between start–end, active the rest of the day. Both windows are stored
  independently; only the one matching `mode` is in effect.
- All three are editable from the Web UI, saved to `config.yaml`, applied
  immediately (`state.request_wake()` interrupts the scheduler's current sleep).

### Timing / frequency

- `timing.frequency` = `low` / `medium` / `high` → picks a `frequency_presets`
  entry giving `min_gap_minutes` / `max_gap_minutes`. A random gap in that range
  is drawn each cycle, then divided by the presence multiplier.
- `timing.day_parts` (morning/day/evening/night) — used only to scale behavior weights.

### Frequency presets + Home multiplier are UI-editable  ← 2026-08-29

The three presets keep their names / one-click role; only the minute ranges
behind them became editable, plus the Home slow-down factor:

- **Frequency card**: an "✎ Edit ranges" toggle reveals a per-preset
  `From (min)` / `To (min)` table (rows ordered low→medium→high, matching the
  buttons — `presetOrder()` reads the button order, since Flask `jsonify`
  alphabetises the JSON keys). Each row → `POST /api/frequency-preset`
  `{key, min, max}` (validates min>0, max>0, min≤max; `save_config` is the
  backstop). Values persist as floats.
- **Presence card**: a "slow the frequency down by ×N" number field.
  `POST /api/home-multiplier {value: N}` stores `round(1/N, 6)` into the
  existing `presence.multipliers.home` (away stays 1.0, not exposed).
- **Live effective-range display** in the Frequency card (`renderEffectiveRange()`,
  pure client-side, recomputed on every `input`/`change`): collapsed → one line
  for the selected preset; while editing → one line per preset
  (`Low — Away: every X–Y min · Home: every X·N–Y·N min`), so each row edit
  gives immediate feedback.
- `/api/status` now also returns `frequency_presets` (full dict) and
  `home_multiplier`.
- No config structure change; `config.example.yaml` comments updated.

### Behavior model

Each cycle the scheduler picks one **behavior** by weighted random choice.
Effective weight = `weight` × `time_weights[current_day_part]`.

| behavior | default weight | episode_duration_seconds | notes |
|---|---|---|---|
| `alert` | 1.0 | [15, 45] | guarding / territory — the long one |
| `response` | 2.0 | [3, 10] | answering a distant dog — most frequent, "background" |
| `chase` | 0.7 | [8, 20] | chasing a bird / cat |
| `noise_reaction` | 1.0 | [5, 15] | reacting to an outside noise |
| `idle` | 0.3 | (none) | single bark, no trigger |

### Episode model — target DURATION, not fixed bark count  ← 2026-08-27 (`24eef6c`)

- Each behavior has `episode_duration_seconds: [min, max]`. At the start of an
  episode a random target in that range is chosen.
- The episode plays whole clips back to back with random `intra_gap_seconds`
  pauses. After **each whole clip** (never mid-clip) it checks whether elapsed
  time (playback + gaps so far) has reached the target; if so, stop.
- Because the check is post-clip, an episode can overshoot the target by up to
  one gap + one clip. That is inherent and acceptable.
- `max_barks` (default 40) is a **safety cap only** — stops a runaway loop if
  clips are very short. Hitting it is logged as `(max_barks cap)`.
- A behavior with no `episode_duration_seconds` (idle) plays exactly one bark.
- Legacy `episode_barks: [lo, hi]` in an old config is silently dropped by
  `config._migrate()` on load.

### Playback volume — master volume + distance simulation  ← 2026-08-29

Two independent knobs, combined multiplicatively into the 0.0–1.0 gain passed
to the player (`src/barkbox/volume.py`):

- **`audio.master_volume`** (0–100) — a global level set from the UI slider in
  place of a physical knob on the speaker. Applies to scheduled episodes **and
  manual test barks** (changed 2026-08-29 — `/api/test-bark` now passes
  `master_gain=master_scale(cfg["audio"])`), so a test plays at the level the
  device would actually use. The alarm still plays at full volume.
  (`audio.volume`, the older 0.0–1.0 base gain, still applies to everything.)
- **`distance_simulation`** — for an episode whose *drawn target duration*
  exceeds `min_episode_duration_seconds` (6), each bark's volume drifts from
  the previous bark's by `uniform(-max_step, +max_step)` (default step 20),
  clamped to `volume_range` (40–100 %). A bounded random walk — the dog moving
  around, not standing still. `start_volume: null` → random within range, or a
  fixed percent. Shorter episodes / single-bark behaviors: no drift. **Never
  applies to manual test barks** (`/api/test-bark` passes `master_gain` but not
  `distance_cfg`) — only master volume reaches them.
- `final_gain = (master_volume/100) * (distance_volume/100)`; `distance_volume`
  is 100 when the walk isn't active. Applied per-bark in `_run_episode` via the
  new `player.play(clip, volume=...)` argument.
- ffplay now takes `-af "volume=<gain>"` (was `-volume`); mpg123 folds the
  per-call gain into its `-f` scale.
- The `played:<behavior>` event gains a `vol <lo>–<hi>%` note when the walk ran.
- UI: a "Master volume" slider card (range input, 0–100), `POST /api/master-volume`,
  and `master_volume` in `/api/status`. Distance sim is config-only (no UI).

### Sound clips

- `sounds/*.mp3` / `sounds/*.wav`, scanned (non-recursive) each cycle. 18 clips
  currently (9 `alexzavesa` single barks ~1–2s, 10 `anton_bark_*` cut from a
  20s recording; original in `sounds/_raw/`, ignored by git and the scanner).
- `sounds/tags.yaml` maps `filename: [behavior tag, ...]`. **Currently empty** —
  every clip is an untagged general-pool fallback, so all behaviors draw from
  all clips. `clips.resolve_clips()` fallback chain: tagged for this behavior →
  untagged pool → everything.
- `anti_repeat.no_repeat_last` (default 3) — don't reuse the last N clips.

### Alarm mode (stub)

- `POST /api/alarm-test` → `state.enter_alarm_mode(duration_minutes)`.
- While active, pre-empts the **schedule** and **presence** gates: repeated bursts
  (`alarm.episode_duration_seconds` [10,25]) separated by `alarm.episode_gap_seconds`
  [1,4], for `alarm.duration_minutes` (10).
- **Master switch beats it** (this session): the scheduler checks `enabled`
  *before* the alarm branch — `enabled:false` clears an active alarm
  (`alarm_stopped` event) and the running burst is cut short via the
  `cancelled` hook (`lambda: not alarm_active or not enabled`).
- **`POST /api/alarm-stop`** (this session): drops `alarm_until`, wakes the
  scheduler → current burst ends within one bark, back to normal logic.
  UI: a "Stop alarm" button + red dot next to "Trigger alarm mode", shown only
  while `alarm_active` (from `/api/now-playing`, which the page already polls).
- **No real external trigger yet** — see open items.

### Web UI (`src/barkbox/web/`)

Single page, vanilla JS, polls `/api/status` + `/api/events` every 5s and
`/api/now-playing` every 250ms.
Controls: master toggle + **master-volume slider** (both in the top card),
presence + Home slow-down, frequency preset + editable ranges, schedule mode +
active/quiet HH:MM fields (whole row `display:none` in Always mode, back in full
for the window modes — saved values never reset). Needs
`#schedule-times[hidden]{display:none}` in CSS because the `#schedule-times`
`display:flex` rule outweighs the bare `[hidden]` UA rule.
Per-behavior on/off + test-bark, alarm test.
Optional shared-token auth via `web.auth_token` (default off).

Behaviors table (2026-08-29):
- **Live "barking now" light** — a dot per row, grey→green while that behavior's
  episode plays. Driven by `state.now_playing` (set in `_run_episode` for the
  whole episode — scheduled, alarm or test bark — cleared in a `finally`),
  exposed by the cheap `GET /api/now-playing` the page polls 4×/s. Verified it
  fires for manual test barks too. Additive to Recent events, not a replacement.
  (`/api/now-playing` also returns `alarm_active` for the red alarm dot.)
- **Manual test barks interrupt each other** — a fresh press cancels the running
  one immediately and starts the new one. `/api/test-bark` bumps a monotonic
  `test_current` generation; `_run_episode(cancelled=lambda: superseded)` bails
  between barks (only the in-flight ~1-2 s clip finishes). Scheduled episodes /
  alarm are untouched by this.
- **Qualitative weight slider** — 4 stops `Rare / Occasional / Frequent /
  Constant` → stored weights `1 / 2 / 4 / 8` (doubling scale). No raw numbers,
  no percentages. `weightToStop()` picks the nearest stop in **log space** for
  an arbitrary stored weight (3 → Frequent). Each slider independent; the stored
  number is unchanged in meaning, only the presentation.
- **`#behavior-mix`** read-only line: "Roughly, out of every 10 events: 6 alert,
  2 response, …" — computed client-side from the enabled weights, not editable.

### Persistence / ops

- `config.yaml` is the single source of truth (gitignored; copy from
  `config.example.yaml`). Web UI reads/writes it; every write is atomic +
  `request_wake()`. The file gets rewritten as plain expanded YAML (comments lost)
  on the first UI change — that's expected.
- Events: in-memory ring buffer, last 50, also emitted to stdout/journald. No log file, no DB.
- `deploy/barkbox.service` (systemd) + `deploy/install.sh` for the Pi.
- Tests: **139 passing** (`venv\Scripts\pytest -q`). Note: the qualitative
  weight-slider label↔number mapping is JS-only (no JS test runner) — verified
  in-browser both directions incl. non-default weights.

---

## Config state as of end of this session

`config.yaml` was **reset to `config.example.yaml`** at end of session — it had
accumulated experiment values from the day's testing (shrunk `frequency_presets`,
`frequency: high`, bumped weights, `master_volume: 86`, `chase` duration `[8, 35]`).
It is gitignored, so none of that was ever in a commit. If you want the old
schedule windows back: `active_window` was `08:00–22:30`, `quiet_window`
`01:15–05:00` (both inactive under `mode: always`).

---

## Open / not finished

- **Ajax alarm integration** — the real deal. Still undecided: webhook (Pi runs
  an endpoint Ajax/automation POSTs to) vs. smart-plug power sensing. Only the
  internal `enter_alarm_mode()` stub exists.
- ~~**Hardware**~~ — done: Pi OS flashed, SSH up, service deployed and running
  (2026-09-04, see top). Outdoor speaker wiring/placement still to finalize.
- **Clip tagging** — `sounds/tags.yaml` is empty. Works fine (fallback pool) but
  behaviors aren't differentiated by sound yet. Tag clips as the library grows.
- **`mpg123` path** — only `ffplay` tested (Windows). Verify `mpg123 -q -f <scale>`
  on the Pi with `speaker-test` first.
- **Web UI security** — shared token only, HTTP only. Fine for home LAN; revisit if exposed.
- No persistent *history*/event database (deliberate). There *is* now a
  persistent text log on the Pi (`/var/log/barkbox/barkbox.log`, RAM-buffered,
  flushed daily) — see the 2026-09-04 section.

## Known TODO for next session

1. **Possible `chase.episode_duration_seconds` bug** — at some earlier point it
   was suspected to be stored reversed (`[20, 8]` instead of `[8, 20]`). Not
   re-checked this session. `_validate_num_pair` *should* reject `hi < lo` on
   save, and DEFAULTS/example are `[8, 20]` — but confirm the UI `duration_max`
   path can't produce an inverted pair, and sanity-check any live config.
2. **Real Ajax alarm integration** — still only the internal `enter_alarm_mode()`
   stub. Undecided: webhook (Pi runs an endpoint Ajax/automation POSTs to) vs.
   smart-plug power sensing. Build `alarm_listener` for real.
3. **Should the alarm respect `master_volume`?** Right now it does **not** — alarm
   bursts always play at full volume, on purpose (an alarm should be loud). Left
   that way deliberately for now; revisit if it turns out you want the master
   slider to tame test alarms too.
4. Consider a "test whole cycle" / fast-forward button in the UI so behavior can
   be observed without temporarily shrinking `frequency_presets` by hand.
5. Maybe surface `episode_duration_seconds[0]` (the min) in the UI too — right
   now only the max is editable there, min is config-only.

## Handy commands

```
cd "C:\Users\Lior\Claude Code session\dog-bark-deterrent"
venv\Scripts\pytest -q
copy config.example.yaml config.yaml          # clean reset
venv\Scripts\python run.py                     # UI on :8080  (needs ffplay/mpg123 on PATH for real audio)
```
On the Pi: `bash deploy/install.sh`, then `journalctl -u barkbox -f`.
