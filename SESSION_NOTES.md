# barkbox — session notes

_Working notes for picking the project back up quickly. Last updated: 2026-08-27._

## What this is

Dog **presence simulation** for a Raspberry Pi 3B + weatherproof outdoor speaker.
Goal: one believable dog with a plausible daily rhythm so anyone listening from
outside believes a dog lives here, even when the house is empty. Not an alarm
siren — alarm mode is a sub-case.

Status: **step 1 built and verified on Windows** (dev). Hardware not bought/installed yet.
Runs locally with `python run.py` → control UI on http://localhost:8080.
Audio backend auto-detects (`ffplay` on this machine via ffmpeg; `mpg123` planned on the Pi; `mock` = log only).

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

### Episode model — target DURATION, not fixed bark count  ← added this session

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
- While active, pre-empts all three gates: repeated bursts
  (`alarm.episode_duration_seconds` [10,25]) separated by `alarm.episode_gap_seconds`
  [1,4], for `alarm.duration_minutes` (10).
- **No real external trigger yet** — see open items.

### Web UI (`src/barkbox/web/`)

Single page, vanilla JS, polls `/api/status` + `/api/events` every 5s.
Controls: master toggle, presence, frequency, schedule mode + **active/quiet
window HH:MM time fields** (added this session), per-behavior weight + max
episode-duration + on/off, per-behavior test-bark, alarm test.
Optional shared-token auth via `web.auth_token` (default off).

### Persistence / ops

- `config.yaml` is the single source of truth (gitignored; copy from
  `config.example.yaml`). Web UI reads/writes it; every write is atomic +
  `request_wake()`. The file gets rewritten as plain expanded YAML (comments lost)
  on the first UI change — that's expected.
- Events: in-memory ring buffer, last 50, also emitted to stdout/journald. No log file, no DB.
- `deploy/barkbox.service` (systemd) + `deploy/install.sh` for the Pi.
- Tests: **83 passing** (`venv\Scripts\pytest -q`).

---

## Config state as of end of this session

`config.yaml` restored to sane defaults EXCEPT a few test tweaks left in place
(not reverted — revert manually or `copy config.example.yaml config.yaml` if you
want a clean commented file):

- `behaviors.alert.weight: 1.2` (default 1.0)
- `behaviors.idle.weight: 0.7` (default 0.3)
- `behaviors.chase.episode_duration_seconds: [8, 35.0]` (default [8, 20])
- `schedule.active_window: 08:00–22:30`, `quiet_window: 01:15–05:00` (kept on purpose; mode is `always` so inactive)

---

## Open / not finished

- **Ajax alarm integration** — the real deal. Still undecided: webhook (Pi runs
  an endpoint Ajax/automation POSTs to) vs. smart-plug power sensing. Only the
  internal `enter_alarm_mode()` stub exists.
- **Hardware** — microSD, PSU, case, outdoor speaker not bought. Pi OS not flashed. SSH not set up.
- **Clip tagging** — `sounds/tags.yaml` is empty. Works fine (fallback pool) but
  behaviors aren't differentiated by sound yet. Tag clips as the library grows.
- **`mpg123` path** — only `ffplay` tested (Windows). Verify `mpg123 -q -f <scale>`
  on the Pi with `speaker-test` first.
- **Web UI security** — shared token only, HTTP only. Fine for home LAN; revisit if exposed.
- No persistent history/log file (deliberate for now).

## Known TODO for next session

1. **Schedule UI clarity** — when `mode` is `Always`, the start/end time fields
   are hidden but it's a bit abrupt. Consider dimming/disabling them instead, or
   a clearer "not used in Always mode" affordance. (Currently: `#schedule-times`
   is `hidden` unless mode is a window.)
2. Decide Ajax integration method and build `alarm_listener` for real.
3. Consider a "test whole cycle" / fast-forward button in the UI so behavior can
   be observed without temporarily shrinking `frequency_presets` by hand.
4. Maybe surface `episode_duration_seconds[0]` (the min) in the UI too — right
   now only the max is editable there, min is config-only.

## Handy commands

```
cd "C:\Users\Lior\Claude Code session\dog-bark-deterrent"
venv\Scripts\pytest -q
copy config.example.yaml config.yaml          # clean reset
venv\Scripts\python run.py                     # UI on :8080  (needs ffplay/mpg123 on PATH for real audio)
```
On the Pi: `bash deploy/install.sh`, then `journalctl -u barkbox -f`.
