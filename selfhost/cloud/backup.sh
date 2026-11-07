#!/usr/bin/env bash
# Daily backup of the database and campaign data. Keeps the last 14.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
dest="$HOME/textbee-backups"
mkdir -p "$dest" && chmod 700 "$dest"
day="$(date +%F)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
docker exec textbee-db mongodump --quiet -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASS" \
  --authenticationDatabase admin --db textbee --archive --gzip > "$work/mongo.archive.gz"
[ ! -f selfhost/data/drip.sqlite3 ] || python3 - "$work/drip.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect("selfhost/data/drip.sqlite3")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
PY
cp -R selfhost/data "$work/data"
tar -czf "$dest/textbee-$day.tar.gz" -C "$work" .
chmod 600 "$dest/textbee-$day.tar.gz"
ls -1t "$dest"/textbee-*.tar.gz | tail -n +15 | xargs -r rm --
echo "Backed up to $dest/textbee-$day.tar.gz"
