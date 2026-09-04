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
sudo apt-get install -y python3-venv mpg123 alsa-utils
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
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
