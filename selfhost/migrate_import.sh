#!/usr/bin/env bash
# Run on the new server from the textbee folder: ./selfhost/migrate_import.sh ~/textbee-migration.tar.gz
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 textbee-migration.tar.gz" >&2; exit 1; }
archive="$(realpath "$1")"
cd "$(dirname "$0")/.."
for f in .env api/.env web/.env selfhost/data/drip.sqlite3; do
  [ ! -e "$f" ] || { echo "Refusing to overwrite $f" >&2; exit 1; }
done
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$archive" -C "$work"
cp "$work/root.env" .env
cp "$work/api.env" api/.env
cp "$work/web.env" web/.env
mkdir -p selfhost/data
cp -R "$work/data/." selfhost/data/
chmod 600 .env api/.env web/.env
set -a; . ./.env; set +a
docker compose up -d --build
for _ in $(seq 1 60); do
  docker exec textbee-db mongosh --quiet -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASS" \
    --authenticationDatabase admin --eval 'db.adminCommand("ping").ok' 2>/dev/null | grep -q 1 && break
  sleep 5
done
docker exec -i textbee-db mongorestore --quiet -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASS" \
  --authenticationDatabase admin --drop --archive --gzip < "$work/mongo.archive.gz"
docker compose restart textbee-api
docker compose ps
echo "Restored. Next: set up Tailscale Serve and the schedule."
