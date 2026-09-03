#!/usr/bin/env bash
# One-shot setup for a Raspberry Pi. Run from the repo root: bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo ">> installing system packages (mpg123, alsa-utils)"
sudo apt-get update -qq
sudo apt-get install -y mpg123 alsa-utils python3-venv

echo ">> creating virtualenv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  echo ">> creating config.yaml from example"
  cp config.example.yaml config.yaml
fi

echo ">> installing systemd service"
SERVICE=/etc/systemd/system/barkbox.service
sudo cp deploy/barkbox.service "$SERVICE"
# rewrite paths/user to match this checkout
sudo sed -i "s#/home/dogpi/barkbox#${REPO_DIR}#g" "$SERVICE"
sudo sed -i "s#^User=dogpi#User=$(id -un)#" "$SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable --now barkbox

echo
echo ">> done. check it with:"
echo "   systemctl status barkbox"
echo "   journalctl -u barkbox -f"
echo "   speaker-test -c2 -twav        # verify the speaker first"
