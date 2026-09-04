# barkbox — session notes

_Working notes for picking the project back up quickly. Last updated: 2026-09-04._

## 2026-09-04 — deploy hardening (not yet committed)

- Deploy target path is now `/home/dogpi/barkbox`, user `dogpi` (was
  `/home/pi/dog-bark-deterrent`). `barkbox.service`: `Restart=on-failure`.
- **SD-card-friendly logging.** `src/barkbox/app.py` `_setup_logging()`: console
  always; if `BARKBOX_LOG_DIR` is set, also a `WatchedFileHandler` at
  `<dir>/barkbox.log`. The unit sets `BARKBOX_LOG_DIR=/run/barkbox` +
  `RuntimeDirectory=barkbox` (tmpfs/RAM, `RuntimeDirectoryPreserve=yes`).
- `deploy/logsync.sh` moves the RAM log to `/var/log/barkbox/barkbox.log`
  (`LogsDirectory=barkbox`, on the card) by `mv` + append — lossless because of
  WatchedFileHandler. Run by `barkbox-logsync.timer` (daily, `Persistent=true`)
  and `barkbox.service` `ExecStopPost`. Self-rotates the archive past ~5 MB.
- New files: `deploy/logsync.sh`, `deploy/barkbox-logsync.{service,timer}`,
  `deploy/INSTALL.md`. `install.sh` installs all three units + enables the timer.
- No `config.yaml` schema change — logging is env-driven like `BARKBOX_LOG_LEVEL`.
- 139 tests still pass.

## What this is

Dog **presence simulation** for a Raspberry Pi 3B + weatherproof outdoor speaker.
Goal: one believable dog with a plausible daily rhythm so anyone listening from
outside believes a dog lives here, even when the house is empty. Not an alarm
siren — alarm mode is a sub-case.

Status: **step 1 built and verified on Windows** (dev). Hardware not bought/installed yet.
Runs locally with `python run.py` → control UI on http://localhost:8080
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
- **Hardware** — microSD, PSU, case, outdoor speaker not bought. Pi OS not flashed. SSH not set up.
- **Clip tagging** — `sounds/tags.yaml` is empty. Works fine (fallback pool) but
  behaviors aren't differentiated by sound yet. Tag clips as the library grows.
- **`mpg123` path** — only `ffplay` tested (Windows). Verify `mpg123 -q -f <scale>`
  on the Pi with `speaker-test` first.
- **Web UI security** — shared token only, HTTP only. Fine for home LAN; revisit if exposed.
- No persistent history/log file (deliberate for now).

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
