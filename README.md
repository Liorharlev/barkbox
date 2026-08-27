# barkbox

Dog **presence simulation** for a Raspberry Pi + an outdoor speaker. The goal is
not an alarm siren — it's one believable dog with a plausible daily rhythm, so
anyone listening from outside believes a dog lives here even when the house is
empty. Alarm mode is a sub-case, not the point.

## How it works

Three independent control layers, each checked every scheduler cycle:

1. **Master switch** (`enabled`) — off means total silence.
2. **Schedule** — is the system active at this time of day at all
   (`always` / `active_window` / `quiet_window`).
3. **Presence** (`home` / `away`) — an activity-rate multiplier; `home` stretches
   the gaps so the dog is mostly quiet when you're in.

When all three pass, the scheduler waits a random gap, then picks a **behavior**
by weighted random choice — `alert`, `response`, `chase`, `noise_reaction`,
`idle` — each with its own clip tags and a target **episode duration**
(`episode_duration_seconds`). The episode plays whole clips back to back, with
random `intra_gap_seconds` pauses, until the elapsed time reaches the target
(checked only between clips, never mid-clip); `max_barks` is just a safety cap.
`idle` has no target and plays a single bark. Behavior weights are scaled by the
current day-part (`morning` / `day` / `evening` / `night`).

Clips live in `sounds/`. Tagging is optional and gradual: `sounds/tags.yaml`
maps a filename to the behaviors it fits; untagged clips are a general fallback.

`config.yaml` is the single source of truth — the web UI only ever reads and
writes that file, and the scheduler reloads it (and applies changes
immediately via a wake signal).

## Run locally (Windows / dev)

Needs Python 3.12 (`winget install Python.Python.3.12`).

```
cd dog-bark-deterrent
python -m venv venv
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\pytest -q
copy config.example.yaml config.yaml
venv\Scripts\python run.py
```

Open <http://localhost:8080>. With no audio player installed the backend
auto-detects as `mock` and playback is logged, not heard. Drop a few `.mp3`
files into `sounds/` to exercise clip selection.

## Deploy to the Pi

```
git clone <repo> ~/dog-bark-deterrent
cd ~/dog-bark-deterrent
bash deploy/install.sh
```

The script installs `mpg123` + `alsa-utils`, builds the venv, copies
`config.example.yaml` to `config.yaml`, and installs + starts the
`barkbox` systemd service.

Before trusting it, verify audio out:

```
speaker-test -c2 -twav          # should come out of the outdoor speaker
sudo raspi-config                # System Options -> Audio, if it's on the wrong sink
curl -X POST localhost:8080/api/test-bark   # one real bark
journalctl -u barkbox -f
```

## Project layout

| path | purpose |
|------|---------|
| `src/barkbox/config.py` | load / validate / atomically save `config.yaml`; schedule + day-part helpers |
| `src/barkbox/state.py` | shared runtime state + `wake_event` |
| `src/barkbox/events.py` | in-memory ring buffer of recent events |
| `src/barkbox/player.py` | CLI audio backends + `MockPlayer`, with a playback lock |
| `src/barkbox/clips.py` | clip discovery, tag loading, behavior-aware resolution |
| `src/barkbox/behaviors.py` | weighted behavior selection |
| `src/barkbox/scheduler.py` | the main loop |
| `src/barkbox/web/` | Flask control UI |
| `deploy/` | systemd unit + install script |

## Not in this phase

- Real Ajax integration (webhook / smart plug) — only the internal alarm stub.
- HTTPS / real auth (shared token only).
- Persistent log file / database / graphs.
- Automatic adaptation of behavior weights (weather, season) — manual via config.
