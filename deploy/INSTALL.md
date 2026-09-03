# Installing barkbox on the Raspberry Pi

Assumes the repo is checked out at `/home/dogpi/barkbox` and you are logged in
over SSH as user `dogpi`.

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

## 4. Install and start the service

```bash
sudo cp deploy/barkbox.service /etc/systemd/system/barkbox.service
sudo systemctl daemon-reload
sudo systemctl enable --now barkbox
```

`enable --now` starts it immediately and every boot. `Restart=on-failure` in the
unit brings it back automatically if it crashes.

## 5. Check it

```bash
systemctl status barkbox
journalctl -u barkbox -f
speaker-test -c2 -twav                        # verify the speaker
curl -X POST localhost:8080/api/test-bark     # one real bark
```

Web UI: `http://<pi-address>:8080`

## Updating later

```bash
cd /home/dogpi/barkbox
git pull
venv/bin/pip install -r requirements.txt
sudo cp deploy/barkbox.service /etc/systemd/system/barkbox.service
sudo systemctl daemon-reload
sudo systemctl restart barkbox
```
