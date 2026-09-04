#!/usr/bin/env bash
# Move the in-RAM (tmpfs) barkbox log to the persistent copy on the SD card.
#
# Called once a day by barkbox-logsync.timer and once more by barkbox.service's
# ExecStopPost (so a clean stop / reboot never loses the day). Safe to run any
# time and safe to run twice at once (flock).
#
# Lossless: the running service uses logging.handlers.WatchedFileHandler, which
# recreates the log file after we mv it out of the way, so no lines are dropped
# and the service is not restarted.
set -euo pipefail

RUNTIME_DIR="${BARKBOX_LOG_DIR:-/run/barkbox}"
RUNTIME_LOG="${RUNTIME_DIR}/barkbox.log"
ARCHIVE_DIR="${BARKBOX_ARCHIVE_DIR:-/var/log/barkbox}"
ARCHIVE_LOG="${ARCHIVE_DIR}/barkbox.log"

MAX_ARCHIVE_BYTES=$((5 * 1024 * 1024))   # rotate the on-card log past ~5 MB
KEEP=5                                    # keep barkbox.log.1 .. .5

mkdir -p "$ARCHIVE_DIR"

exec 9>"${ARCHIVE_DIR}/.logsync.lock"
if command -v flock >/dev/null 2>&1; then
    flock -n 9 || exit 0    # another flush is already running
fi

[ -s "$RUNTIME_LOG" ] || exit 0

STAGED="${RUNTIME_LOG}.$$"
mv "$RUNTIME_LOG" "$STAGED"    # service's WatchedFileHandler makes a fresh barkbox.log on its next write
cat "$STAGED" >> "$ARCHIVE_LOG"
rm -f "$STAGED"

# Keep the on-card archive bounded.
size=$(stat -c '%s' "$ARCHIVE_LOG" 2>/dev/null || echo 0)
if [ "$size" -gt "$MAX_ARCHIVE_BYTES" ]; then
    i=$KEEP
    while [ "$i" -gt 1 ]; do
        [ -f "${ARCHIVE_LOG}.$((i - 1))" ] && mv -f "${ARCHIVE_LOG}.$((i - 1))" "${ARCHIVE_LOG}.${i}"
        i=$((i - 1))
    done
    mv -f "$ARCHIVE_LOG" "${ARCHIVE_LOG}.1"
fi
