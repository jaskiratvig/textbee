#!/usr/bin/env python3
"""Send a CSV of texts through your own gateway in paced batches.

Dry run by default; nothing is sent without --live.

The CSV needs a `phone` column. Other columns can be used in the template,
e.g. {first_name}.

Safeguards:
  * skips numbers in the suppression file (your DNC scrub results and opt-outs)
  * before each live batch, reads replies from the phone and adds opt-outs
    (such as STOP) to the suppression file; stops if replies can't be read
  * only sends inside the allowed hours
  * logs each number before sending, so a rerun never texts it twice
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OPT_OUT_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
OPT_OUT_PHRASES = ("opt out", "remove me", "take me off", "do not text", "don't text",
                   "dont text", "stop texting", "stop messaging", "wrong number")


def normalize(raw):
    """Return a US number as +1XXXXXXXXXX, or None if it isn't a valid one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if not re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", digits):
        return None
    return "+1" + digits


def is_opt_out(text):
    text = (text or "").strip()
    words = re.findall(r"[A-Za-z']+", text.upper())
    if words and words[0] in OPT_OUT_WORDS:
        return True
    lowered = text.lower().replace("’", "'")
    return any(phrase in lowered for phrase in OPT_OUT_PHRASES)


def read_numbers(path):
    if not path.exists():
        return set()
    found = set()
    for line in path.read_text().splitlines():
        number = normalize(line.split(",")[0])
        if number:
            found.add(number)
    return found


def add_numbers(path, numbers):
    with path.open("a") as f:
        for number in sorted(numbers):
            f.write(number + "\n")


def api(base, key, method, path, body=None):
    request = urllib.request.Request(
        base.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_opt_outs(base, key, device):
    found, page = set(), 1
    while True:
        result = api(base, key, "GET", f"/gateway/devices/{device}/get-received-sms?page={page}&limit=100")
        rows = result.get("data") or []
        for row in rows:
            if is_opt_out(row.get("message")):
                number = normalize(row.get("sender"))
                if number:
                    found.add(number)
        if len(rows) < 100:
            return found
        page += 1


def load_recipients(args, suppressed, already_sent):
    stats = {"invalid": 0, "suppressed": 0, "already_sent": 0, "duplicate": 0}
    recipients, seen = [], set()
    with args.csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "phone" not in (reader.fieldnames or []):
            sys.exit("CSV is missing the phone column")
        for row in reader:
            number = normalize(row.get("phone"))
            if number is None:
                stats["invalid"] += 1
            elif number in suppressed:
                stats["suppressed"] += 1
            elif number in already_sent:
                stats["already_sent"] += 1
            elif number in seen:
                stats["duplicate"] += 1
            else:
                seen.add(number)
                recipients.append((number, row))
    return recipients, stats


def wait_for_window(args, zone):
    while True:
        now = datetime.now(zone)
        if args.start_hour <= now.hour < args.end_hour:
            return
        print(f"{now:%a %H:%M %Z}: outside sending hours, waiting")
        time.sleep(300)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--template", required=True, help="e.g. 'Hi {first_name}, ... Reply STOP to opt out.'")
    p.add_argument("--device-id", help="required with --live")
    p.add_argument("--api-url", default="http://127.0.0.1:3001/api/v1")
    p.add_argument("--suppress", type=Path, default=Path("selfhost/data/suppressed.txt"))
    p.add_argument("--sent-log", type=Path, default=Path("selfhost/data/sent.csv"))
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--minutes-between", type=float, default=30)
    p.add_argument("--timezone", default="America/Chicago")
    p.add_argument("--start-hour", type=int, default=9, help="first allowed hour, 24h clock")
    p.add_argument("--end-hour", type=int, default=20, help="sending stops at this hour, 24h clock")
    p.add_argument("--live", action="store_true", help="actually send")
    return p.parse_args()


def main():
    args = parse_args()
    if not 1 <= args.batch_size <= 30:
        sys.exit("--batch-size must be between 1 and 30")
    if not 8 <= args.start_hour < args.end_hour <= 21:
        sys.exit("Sending hours must fall between 8 and 21")
    if "stop" not in args.template.lower():
        sys.exit("The template must tell people how to opt out, e.g. 'Reply STOP to opt out.'")
    key = os.environ.get("TEXTBEE_API_KEY", "")
    if args.live and not (key and args.device_id):
        sys.exit("--live needs --device-id and the TEXTBEE_API_KEY environment variable")

    zone = ZoneInfo(args.timezone)
    args.suppress.parent.mkdir(parents=True, exist_ok=True)
    args.sent_log.parent.mkdir(parents=True, exist_ok=True)

    already_sent = read_numbers(args.sent_log)
    recipients, stats = load_recipients(args, read_numbers(args.suppress), already_sent)
    try:
        previews = [(number, args.template.format(**row)) for number, row in recipients]
    except KeyError as e:
        sys.exit(f"The template uses {e}, which isn't a CSV column")

    print(f"Ready: {len(previews)} | skipped: {stats}")
    if not args.live:
        for number, message in previews[:3]:
            print(f"  {number}: {message}")
        print("Dry run: nothing sent. Add --live to send.")
        return

    remaining = previews
    while remaining:
        wait_for_window(args, zone)
        new_opt_outs = fetch_opt_outs(args.api_url, key, args.device_id)
        suppressed = read_numbers(args.suppress)
        fresh = new_opt_outs - suppressed
        if fresh:
            add_numbers(args.suppress, fresh)
            print(f"Added {len(fresh)} opt-out(s) to the suppression file")
        suppressed |= new_opt_outs
        remaining = [item for item in remaining if item[0] not in suppressed]
        batch, remaining = remaining[:args.batch_size], remaining[args.batch_size:]
        if not batch:
            break

        # Logged first: after a failed or unclear request, a number is skipped
        # on rerun rather than risk texting it twice.
        stamp = datetime.now(zone).isoformat(timespec="seconds")
        with args.sent_log.open("a") as f:
            for number, _ in batch:
                f.write(f"{number},{stamp}\n")
        body = {
            "messageTemplate": args.template,
            "messages": [{"message": message, "recipients": [number]} for number, message in batch],
        }
        try:
            api(args.api_url, key, "POST", f"/gateway/devices/{args.device_id}/send-bulk-sms", body)
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"Send failed, stopping. Check these {len(batch)} numbers at the end of {args.sent_log}: {e}")
        print(f"{stamp}: queued {len(batch)}, {len(remaining)} left")
        if remaining:
            time.sleep(args.minutes_between * 60)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"Couldn't read replies from the gateway, so nothing more was sent: {e}")
