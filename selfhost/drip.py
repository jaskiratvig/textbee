#!/usr/bin/env python3
"""Run a scheduled text drip through your own gateway.

Commands:
  import    add contacts from a cleaned CSV or a raw skip-trace export
  run       send what's due (dry run unless --live); schedule this hourly
  status    show campaign counts
  replies   list people who replied and need a human follow-up
  history   export every message sent and received to a CSV
  retry     put contacts marked failed back in the queue
  test      send every message right away to a few of your own numbers

Any reply stops automated texts to that person. Opt-outs and wrong numbers are
suppressed permanently, and also added to the file send_batches.py reads.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OPT_OUT_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
OPT_OUT_PHRASES = ("opt out", "remove me", "take me off", "do not text", "don't text", "dont text",
                   "do not contact", "don't contact", "stop texting", "stop messaging", "leave me alone")
WRONG_NUMBER_PHRASES = ("wrong number", "wrong person", "not the owner", "don't own", "dont own")
FINAL_FAILED = {"failed"}
FINAL_OK = {"sent", "delivered"}
BREAKER_MIN_RESULTS = 10
BREAKER_FAIL_RATE = 0.2
TEST_MAX_RECIPIENTS = 5
MAX_HEARTBEAT_AGE_MINUTES = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  phone TEXT PRIMARY KEY,
  first_name TEXT NOT NULL,
  property_address TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  step INTEGER NOT NULL DEFAULT 0,
  first_sent_on TEXT,
  next_send_on TEXT,
  enrolled_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL,
  direction TEXT NOT NULL,
  step INTEGER,
  body TEXT,
  status TEXT NOT NULL,
  batch_id TEXT,
  gateway_id TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppressions (
  phone TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_due ON contacts (status, next_send_on);
CREATE INDEX IF NOT EXISTS messages_batch ON messages (batch_id);
"""


def utcnow():
    return datetime.now(timezone.utc)


def stamp():
    return utcnow().isoformat(timespec="seconds")


def normalize(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if not re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", digits):
        return None
    return "+1" + digits


def classify_reply(text):
    text = (text or "").strip()
    lowered = text.lower().replace("’", "'")
    words = re.findall(r"[A-Za-z']+", text.upper())
    if words and words[0] in OPT_OUT_WORDS:
        return "opted_out"
    if any(phrase in lowered for phrase in OPT_OUT_PHRASES):
        return "opted_out"
    if any(phrase in lowered for phrase in WRONG_NUMBER_PHRASES):
        return "wrong_number"
    return "replied"


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def read_number_file(path):
    if not path.exists():
        return set()
    return {n for n in (normalize(line.split(",")[0]) for line in path.read_text().splitlines()) if n}


def load_steps(path):
    steps = json.loads(path.read_text())
    days = [s["day"] for s in steps]
    if not steps or days[0] != 0 or days != sorted(set(days)):
        sys.exit("Message steps must start at day 0 and increase")
    for s in steps:
        if "stop" not in s["template"].lower():
            sys.exit(f"The day {s['day']} message must tell people how to opt out")
        s["template"].format(FirstName="x", PropertyAddress="x")
    return steps


def suppress(db, phone, reason, suppress_file):
    cur = db.execute("INSERT OR IGNORE INTO suppressions VALUES (?, ?, ?)", (phone, reason, stamp()))
    if cur.rowcount:
        suppress_file.parent.mkdir(parents=True, exist_ok=True)
        with suppress_file.open("a") as f:
            f.write(phone + "\n")


def absorb_suppress_file(db, suppress_file):
    for phone in read_number_file(suppress_file):
        db.execute("INSERT OR IGNORE INTO suppressions VALUES (?, 'suppression file', ?)", (phone, stamp()))
    db.execute("""UPDATE contacts SET status='suppressed', updated_at=?
                  WHERE status='active' AND phone IN (SELECT phone FROM suppressions)""", (stamp(),))


def first_word(value):
    parts = (value or "").strip().split()
    return parts[0] if parts else ""


def pick_from_raw(row, litigator_scrubbed):
    """Choose one number from a raw export row, or return a reason to skip the row."""
    if (row.get("OwnerType") or "").strip().upper() != "INDIVIDUAL":
        return None, None, "not_individual_owner"
    if (row.get("PropertyType") or "").strip().lower() == "vacant land":
        return None, None, "vacant_land"
    first = first_word(row.get("FirstName")).title()
    if not first:
        return None, None, "no_name"
    if (row.get("Contact1Type") or "").strip().lower() == "tenant":
        return None, None, "tenant"
    if first.lower() not in re.findall(r"[a-z'-]+", (row.get("Contact1Name") or "").lower()):
        return None, None, "contact_name_mismatch"
    reason = "no_usable_mobile"
    for i in (1, 2, 3):
        prefix = f"Contact1Phone_{i}"
        phone = normalize(row.get(prefix))
        if not phone or (row.get(f"{prefix}_Type") or "").strip().lower() != "mobile":
            continue
        if (row.get(f"{prefix}_DNC") or "").strip().lower() != "false":
            reason = "dnc"
            continue
        if not litigator_scrubbed and (row.get(f"{prefix}_Litigator") or "").strip().lower() != "false":
            reason = "litigator_not_screened"
            continue
        return phone, first, None
    return None, None, reason


def cmd_import(args):
    db = connect(args.db)
    absorb_suppress_file(db, args.suppress)
    sent_before = read_number_file(args.sent_log)
    stats = {"added": 0, "invalid": 0, "suppressed": 0, "already_texted": 0, "already_enrolled": 0, "no_address": 0}
    with args.csv.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        raw = "Contact1Phone_1" in fields
        if not raw and not {"phone", "FirstName", "PropertyAddress"} <= fields:
            sys.exit("CSV needs phone, FirstName and PropertyAddress columns, or a raw skip-trace export")
        for row in reader:
            if raw:
                phone, first, reason = pick_from_raw(row, args.litigator_scrubbed)
                if reason:
                    stats[reason] = stats.get(reason, 0) + 1
                    continue
            else:
                phone, first = normalize(row.get("phone")), first_word(row.get("FirstName")).title()
                if not phone or not first:
                    stats["invalid"] += 1
                    continue
            address = (row.get("PropertyAddress") or "").strip()
            if not address:
                stats["no_address"] += 1
            elif db.execute("SELECT 1 FROM suppressions WHERE phone=?", (phone,)).fetchone():
                stats["suppressed"] += 1
            elif phone in sent_before:
                stats["already_texted"] += 1
            else:
                cur = db.execute(
                    """INSERT OR IGNORE INTO contacts
                       (phone, first_name, property_address, enrolled_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (phone, first, address, stamp(), stamp()))
                stats["added" if cur.rowcount else "already_enrolled"] += 1
                if args.limit and stats["added"] >= args.limit:
                    break
    db.commit()
    print(stats)


def api(args, key, method, path, body=None):
    request = urllib.request.Request(
        args.api_url.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def check_phone_online(args, key):
    """Exit if the phone hasn't checked in, because replies can't arrive without that link."""
    device = api(args, key, "GET", f"/gateway/devices/{args.device_id}").get("data") or {}
    raw = device.get("lastHeartbeat")
    if not raw:
        sys.exit("Paused: the phone has never checked in with the server, so replies can't be trusted.")
    seen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    age = (utcnow() - seen).total_seconds() / 60
    if age > MAX_HEARTBEAT_AGE_MINUTES:
        sys.exit(f"Paused: the phone last checked in {age:.0f} minutes ago, so replies may not be arriving. "
                 "Check Tailscale on the phone and the Mac.")


def sync_replies(db, args, key):
    counts = {"opted_out": 0, "wrong_number": 0, "replied": 0}
    page = 1
    while True:
        result = api(args, key, "GET", f"/gateway/devices/{args.device_id}/get-received-sms?page={page}&limit=100")
        rows = result.get("data") or []
        for row in rows:
            gateway_id = str(row.get("_id") or "")
            phone = normalize(row.get("sender"))
            if not gateway_id or not phone:
                continue
            seen = db.execute("SELECT 1 FROM messages WHERE gateway_id=?", (gateway_id,)).fetchone()
            if seen:
                continue
            outcome = classify_reply(row.get("message"))
            db.execute(
                "INSERT INTO messages (phone, direction, body, status, gateway_id, created_at) VALUES (?, 'in', ?, ?, ?, ?)",
                (phone, row.get("message"), outcome, gateway_id, row.get("receivedAt") or stamp()))
            if outcome in ("opted_out", "wrong_number"):
                suppress(db, phone, outcome, args.suppress)
                cur = db.execute("UPDATE contacts SET status=?, updated_at=? WHERE phone=?", (outcome, stamp(), phone))
            else:
                cur = db.execute(
                    "UPDATE contacts SET status='replied', updated_at=? WHERE phone=? AND status IN ('active', 'completed')",
                    (stamp(), phone))
            if cur.rowcount:
                counts[outcome] += 1
        if len(rows) < 100:
            break
        page += 1
    db.commit()
    return counts


def sync_delivery(db, args, key):
    batches = db.execute(
        """SELECT DISTINCT batch_id FROM messages
           WHERE direction='out' AND batch_id IS NOT NULL AND status='queued'""").fetchall()
    for batch in batches:
        result = api(args, key, "GET", f"/gateway/messages?smsBatchId={batch['batch_id']}&limit=100")
        for row in result.get("data") or []:
            status = (row.get("status") or "").lower()
            phone = normalize(row.get("recipient"))
            if phone and status in FINAL_FAILED | FINAL_OK:
                db.execute("UPDATE messages SET status=? WHERE batch_id=? AND phone=? AND direction='out'",
                           (status, batch["batch_id"], phone))
    db.commit()


def breaker_tripped(db):
    rows = db.execute(
        """SELECT status FROM messages WHERE direction='out' AND status IN ('sent', 'delivered', 'failed')
           ORDER BY id DESC LIMIT 50""").fetchall()
    if len(rows) < BREAKER_MIN_RESULTS:
        return None
    failed = sum(r["status"] == "failed" for r in rows)
    return (failed, len(rows)) if failed / len(rows) >= BREAKER_FAIL_RATE else None


def select_due(db, args, today):
    day_start = datetime.combine(today, datetime.min.time(), ZoneInfo(args.timezone)).astimezone(timezone.utc)
    start = day_start.isoformat(timespec="seconds")
    sent_today = db.execute(
        "SELECT COUNT(*) c FROM messages WHERE direction='out' AND created_at>=?", (start,)).fetchone()["c"]
    new_today = db.execute(
        "SELECT COUNT(*) c FROM messages WHERE direction='out' AND step=0 AND created_at>=?", (start,)).fetchone()["c"]
    room = min(args.batch_size, args.daily_limit - sent_today)
    if room <= 0:
        return []
    active = """status='active' AND phone NOT IN (SELECT phone FROM suppressions)"""
    follow_ups = db.execute(
        f"SELECT * FROM contacts WHERE {active} AND step>0 AND next_send_on<=? ORDER BY next_send_on, phone LIMIT ?",
        (today.isoformat(), room)).fetchall()
    room_new = min(room - len(follow_ups), args.new_per_day - new_today)
    new = []
    if room_new > 0:
        new = db.execute(
            f"SELECT * FROM contacts WHERE {active} AND step=0 ORDER BY enrolled_at, phone LIMIT ?",
            (room_new,)).fetchall()
    return list(follow_ups) + list(new)


def advance(db, contact, steps, today):
    next_step = contact["step"] + 1
    first_sent = contact["first_sent_on"] or today.isoformat()
    if next_step >= len(steps):
        db.execute(
            "UPDATE contacts SET status='completed', step=?, first_sent_on=?, next_send_on=NULL, updated_at=? WHERE phone=?",
            (next_step, first_sent, stamp(), contact["phone"]))
        return
    due = date.fromisoformat(first_sent) + timedelta(days=steps[next_step]["day"])
    due = max(due, today + timedelta(days=1))
    db.execute(
        "UPDATE contacts SET step=?, first_sent_on=?, next_send_on=?, updated_at=? WHERE phone=?",
        (next_step, first_sent, due.isoformat(), stamp(), contact["phone"]))


def cmd_run(args):
    if not 8 <= args.start_hour < args.end_hour <= 21:
        sys.exit("Sending hours must fall between 8 and 21")
    if not 1 <= args.batch_size <= 30:
        sys.exit("--batch-size must be between 1 and 30")
    steps = load_steps(args.messages)
    db = connect(args.db)
    key = os.environ.get("TEXTBEE_API_KEY", "")
    if args.live and not (key and args.device_id):
        sys.exit("--live needs --device-id and the TEXTBEE_API_KEY environment variable")
    absorb_suppress_file(db, args.suppress)
    db.commit()

    if args.live:
        try:
            check_phone_online(args, key)
            replies = sync_replies(db, args, key)
            print(f"Replies processed: {replies}")
            sync_delivery(db, args, key)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            sys.exit(f"Couldn't read replies or delivery results, so nothing was sent: {e}")
        tripped = breaker_tripped(db)
        if tripped:
            sys.exit(f"Paused: {tripped[0]} of the last {tripped[1]} sends failed. Check the phone and carrier.")

    now = utcnow().astimezone(ZoneInfo(args.timezone))
    if not args.start_hour <= now.hour < args.end_hour:
        print(f"{now:%a %H:%M %Z}: outside sending hours, nothing sent")
        return

    today = now.date()
    due = select_due(db, args, today)
    batch = [(c, steps[c["step"]]["template"].format(
        FirstName=c["first_name"], PropertyAddress=c["property_address"])) for c in due]
    if not batch:
        print("Nothing due right now")
        return
    if not args.live:
        for contact, body in batch:
            print(f"  day {steps[contact['step']]['day']} {contact['phone']}: {body}")
        print(f"Dry run: {len(batch)} would send. Add --live to send.")
        return

    # Logged before sending so a crash can't lead to a second text.
    ids = {}
    created = stamp()
    for contact, body in batch:
        cur = db.execute(
            "INSERT INTO messages (phone, direction, step, body, status, created_at) VALUES (?, 'out', ?, ?, 'sending', ?)",
            (contact["phone"], contact["step"], body, created))
        ids[contact["phone"]] = cur.lastrowid
        db.execute("UPDATE contacts SET status='sending', updated_at=? WHERE phone=?", (stamp(), contact["phone"]))
    db.commit()

    payload = {
        "messageTemplate": "drip",
        "messages": [{"message": body, "recipients": [contact["phone"]]} for contact, body in batch],
    }
    try:
        result = api(args, key, "POST", f"/gateway/devices/{args.device_id}/send-bulk-sms", payload)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        for contact, _ in batch:
            db.execute("UPDATE messages SET status='unknown' WHERE id=?", (ids[contact["phone"]],))
            db.execute("UPDATE contacts SET status='failed', updated_at=? WHERE phone=?", (stamp(), contact["phone"]))
        db.commit()
        sys.exit(f"Send failed. {len(batch)} contacts marked failed; see `status`, then use `retry` if they weren't texted: {e}")

    batch_id = str((result.get("data") or {}).get("smsBatchId") or "") or None
    for contact, _ in batch:
        db.execute("UPDATE messages SET status='queued', batch_id=? WHERE id=?", (batch_id, ids[contact["phone"]]))
        db.execute("UPDATE contacts SET status='active' WHERE phone=?", (contact["phone"],))
        advance(db, contact, steps, today)
    db.commit()
    print(f"Queued {len(batch)}")


def cmd_status(args):
    db = connect(args.db)
    for row in db.execute("SELECT status, step, COUNT(*) c FROM contacts GROUP BY status, step ORDER BY status, step"):
        print(f"{row['status']:<14} next step {row['step']}: {row['c']}")
    row = db.execute("SELECT COUNT(*) c FROM suppressions").fetchone()
    print(f"suppressed numbers: {row['c']}")
    for row in db.execute("SELECT status, COUNT(*) c FROM messages WHERE direction='out' GROUP BY status"):
        print(f"sends {row['status']}: {row['c']}")


def cmd_replies(args):
    db = connect(args.db)
    rows = db.execute(
        """SELECT c.first_name, c.property_address, m.phone, m.body, m.created_at
           FROM contacts c JOIN messages m ON m.phone=c.phone AND m.direction='in'
           WHERE c.status='replied' ORDER BY m.created_at""").fetchall()
    for row in rows:
        print(f"{row['created_at']}  {row['first_name']}  {row['phone']}  {row['property_address']}\n    {row['body']}")
    if not rows:
        print("No replies waiting")


def cmd_history(args):
    db = connect(args.db)
    rows = db.execute(
        """SELECT m.created_at, m.phone, c.first_name, c.property_address, m.direction, m.step, m.status, m.body
           FROM messages m JOIN contacts c ON c.phone=m.phone ORDER BY m.created_at, m.id""").fetchall()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_utc", "phone", "first_name", "property_address", "direction", "step", "status", "message"])
        writer.writerows(tuple(row) for row in rows)
    print(f"Wrote {len(rows)} messages to {args.out}")


def cmd_retry(args):
    db = connect(args.db)
    cur = db.execute("""UPDATE contacts SET status='active', updated_at=?
                        WHERE status IN ('failed', 'sending') AND phone NOT IN (SELECT phone FROM suppressions)""", (stamp(),))
    db.commit()
    print(f"Requeued {cur.rowcount}")


def cmd_test(args):
    """Send every step at once to a few of your own numbers, without touching the campaign."""
    if not 8 <= args.start_hour < args.end_hour <= 21:
        sys.exit("Sending hours must fall between 8 and 21")
    steps = load_steps(args.messages)
    key = os.environ.get("TEXTBEE_API_KEY", "")
    if not (key and args.device_id):
        sys.exit("test needs --device-id and the TEXTBEE_API_KEY environment variable")
    now = utcnow().astimezone(ZoneInfo(args.timezone))
    if not args.start_hour <= now.hour < args.end_hour:
        sys.exit(f"{now:%a %H:%M %Z}: outside sending hours, nothing sent")
    with args.csv.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not 1 <= len(rows) <= TEST_MAX_RECIPIENTS:
        sys.exit(f"Test CSV must have 1 to {TEST_MAX_RECIPIENTS} rows of your own numbers")
    db = connect(args.db)
    absorb_suppress_file(db, args.suppress)
    db.commit()
    try:
        check_phone_online(args, key)
        sync_replies(db, args, key)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        sys.exit(f"Couldn't read replies, so nothing was sent: {e}")
    messages = []
    for row in rows:
        phone = normalize(row.get("phone"))
        first = first_word(row.get("FirstName") or row.get("first_name")).title()
        address = (row.get("PropertyAddress") or row.get("address") or "").strip()
        if not phone or not first or not address:
            sys.exit("Each test row needs a valid phone, a first name and an address")
        if db.execute("SELECT 1 FROM suppressions WHERE phone=?", (phone,)).fetchone():
            sys.exit(f"{phone} is opted out, so nothing was sent. Clear it first if it's your own test number.")
        for step in steps:
            messages.append({"message": step["template"].format(FirstName=first, PropertyAddress=address),
                             "recipients": [phone]})
    try:
        api(args, key, "POST", f"/gateway/devices/{args.device_id}/send-bulk-sms",
            {"messageTemplate": "drip test", "messages": messages})
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        sys.exit(f"Test send failed: {e}")
    print(f"Queued {len(messages)} test texts. The phone app's Send Delay sets the gap between them.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=HERE / "data/drip.sqlite3")
    p.add_argument("--suppress", type=Path, default=HERE / "data/suppressed.txt")
    p.add_argument("--sent-log", type=Path, default=HERE / "data/sent.csv")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("import")
    i.add_argument("--csv", type=Path, required=True)
    i.add_argument("--litigator-scrubbed", action="store_true",
                   help="you've screened this list yourself, so allow blank litigator fields")
    i.add_argument("--limit", type=int, help="stop after adding this many new contacts")
    i.set_defaults(func=cmd_import)

    r = sub.add_parser("run")
    r.add_argument("--device-id")
    r.add_argument("--api-url", default="http://127.0.0.1:3001/api/v1")
    r.add_argument("--messages", type=Path, default=HERE / "drip_messages.json")
    r.add_argument("--batch-size", type=int, default=20)
    r.add_argument("--daily-limit", type=int, default=25)
    r.add_argument("--new-per-day", type=int, default=15)
    r.add_argument("--timezone", default="America/Chicago")
    r.add_argument("--start-hour", type=int, default=9)
    r.add_argument("--end-hour", type=int, default=20)
    r.add_argument("--live", action="store_true")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("test")
    t.add_argument("--csv", type=Path, required=True)
    t.add_argument("--device-id")
    t.add_argument("--api-url", default="http://127.0.0.1:3001/api/v1")
    t.add_argument("--messages", type=Path, default=HERE / "drip_messages.json")
    t.add_argument("--timezone", default="America/Chicago")
    t.add_argument("--start-hour", type=int, default=9)
    t.add_argument("--end-hour", type=int, default=20)
    t.set_defaults(func=cmd_test)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("replies").set_defaults(func=cmd_replies)
    h = sub.add_parser("history")
    h.add_argument("--out", type=Path, default=HERE / "data/history.csv")
    h.set_defaults(func=cmd_history)
    sub.add_parser("retry").set_defaults(func=cmd_retry)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
