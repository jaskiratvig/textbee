#!/usr/bin/env bash
# Creates the plan records the API expects and waives email verification for one
# account, so no SMTP server is needed. Run after registering in the dashboard.
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 you@example.com" >&2; exit 1; }
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
docker exec -i -e TARGET_EMAIL="$1" textbee-db mongosh --quiet \
  -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASS" --authenticationDatabase admin textbee <<'JS'
for (const name of ['free', 'pro']) {
  db.plans.updateOne({ name }, {
    $set: { dailyLimit: -1, monthlyLimit: -1, bulkSendLimit: -1, deviceLimit: -1, monthlyPrice: 0, yearlyPrice: 0, isActive: true },
    $setOnInsert: { createdAt: new Date() },
  }, { upsert: true })
}
const email = process.env.TARGET_EMAIL
const r = db.users.updateOne({ email }, { $set: { emailVerificationWaivedAt: new Date(), emailVerifiedAt: new Date() } })
if (r.matchedCount !== 1) { print('No account found for ' + email + '. Register in the dashboard first.'); quit(1) }
print('Plans ready; email marked verified for ' + email)
JS
