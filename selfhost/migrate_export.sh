#!/usr/bin/env bash
# Run on the old machine. Packs the database, settings and campaign data into one file.
# The file holds secrets and contact data, so keep it private.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
out="${1:-$HOME/textbee-migration.tar.gz}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
docker exec textbee-db mongodump --quiet -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASS" \
  --authenticationDatabase admin --db textbee --archive --gzip > "$work/mongo.archive.gz"
[ -s "$work/mongo.archive.gz" ] || { echo "Database export is empty" >&2; exit 1; }
cp .env "$work/root.env"
cp api/.env "$work/api.env"
cp web/.env "$work/web.env"
mkdir -p selfhost/data
cp -R selfhost/data "$work/data"
tar -czf "$out" -C "$work" .
chmod 600 "$out"
echo "Wrote $out"
