#!/usr/bin/env bash
# Run on the cloud server from the textbee folder, after migrate_import.sh:
#   ./selfhost/cloud/install.sh DEVICE_ID
# Installs timers that sync replies every 15 minutes, send every 30 minutes
# from 9am to 7:30pm Chicago time, and back up daily. Sending starts paused.
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 DEVICE_ID" >&2; exit 1; }
device="$1"
cd "$(dirname "$0")/../.."
repo="$(pwd)"
user="$(id -un)"
read -rsp "API key: " key; echo
[ -n "$key" ] || { echo "No API key entered" >&2; exit 1; }

sudo install -m 600 -o root -g root /dev/null /etc/textbee-drip.env
sudo tee /etc/textbee-drip.env >/dev/null <<ENV
TEXTBEE_API_KEY=$key
DEVICE_ID=$device
DRIP_ARGS=--batch-size 20 --daily-limit 200 --new-per-day 100
ENV

mkdir -p selfhost/data
python3 selfhost/drip.py pause >/dev/null

for f in selfhost/cloud/units/*; do
  sed -e "s|@REPO@|$repo|g" -e "s|@USER@|$user|g" "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now textbee-sync.timer textbee-drip.timer textbee-backup.timer
systemctl list-timers 'textbee-*' --no-pager
echo
echo "Installed with sending PAUSED. Replies sync every 15 minutes."
echo "When you're ready: python3 selfhost/drip.py resume"
