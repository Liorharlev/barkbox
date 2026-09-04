#!/usr/bin/env bash
# One-shot setup for a Raspberry Pi. Run from the repo root: bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
RUN_USER="$(id -un)"

# ffmpeg gives us the `ffmpeg` backend (decode+volume piped straight into
# `aplay` -> pure ALSA, no SDL/Pulse/PipeWire) and `ffplay` as a fallback.
# mpg123 is mp3-only (it "plays" a .wav silently with exit 0); aplay is
# wav-only and has no volume. See audio.backend in config.example.yaml.
echo ">> installing system packages (ffmpeg, mpg123, alsa-utils)"
sudo apt-get update -qq
sudo apt-get install -y ffmpeg mpg123 alsa-utils python3-venv

# The service needs to actually open /dev/snd/* — make sure its user can.
sudo usermod -aG audio "$RUN_USER" || true

echo ">> creating virtualenv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  echo ">> creating config.yaml from example"
  cp config.example.yaml config.yaml
fi

chmod +x deploy/logsync.sh

echo ">> installing systemd units"
for unit in barkbox.service barkbox-logsync.service barkbox-logsync.timer; do
  DEST="/etc/systemd/system/${unit}"
  sudo cp "deploy/${unit}" "$DEST"
  # rewrite the packaged paths/user to match this checkout
  sudo sed -i "s#/home/dogpi/barkbox#${REPO_DIR}#g" "$DEST"
  sudo sed -i "s#^User=dogpi#User=${RUN_USER}#" "$DEST"
done

sudo systemctl daemon-reload
sudo systemctl enable --now barkbox
sudo systemctl enable --now barkbox-logsync.timer

echo
echo ">> done."
echo "   runtime log (RAM):   /run/barkbox/barkbox.log"
echo "   archived log (card): /var/log/barkbox/barkbox.log   (flushed daily + on stop)"
echo
echo "   systemctl status barkbox"
echo "   journalctl -u barkbox -f"
echo "   systemctl list-timers barkbox-logsync.timer"
echo "   speaker-test -c2 -twav        # verify the speaker first"
echo "   curl -X POST localhost:8080/api/test-bark   # then a real clip"
echo "   (audio still silent with no error in the log?"
echo "    sudo systemctl edit barkbox   # add [Service] / Environment=BARKBOX_LOG_LEVEL=DEBUG"
echo "    then restart + test-bark: journalctl -u barkbox will show the exact"
echo "    player command and its stdout/stderr, success or not)"
