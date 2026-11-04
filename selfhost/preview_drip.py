#!/usr/bin/env python3
"""Print the texts a raw skip-trace export would produce. This never sends anything.

Applies its own eligibility rules: individual owners, no vacant land, and mobile
numbers on contacts that aren't tenants and match the owner's first name, with
DNC False and litigator False. The times shown are a simulation.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHONE_RE = re.compile(r"[2-9]\d{2}[2-9]\d{6}")


def normalize(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return "+1" + digits if PHONE_RE.fullmatch(digits) else None


def fix_address(address):
    return re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), address)


def greeting_name(row, first):
    if len(first) < 2:
        return ""
    for contact in range(1, 9):
        names = re.findall(r"[a-z'-]+", (row.get(f"Contact{contact}Name") or "").lower())
        if names and names[0] == first.lower():
            return first
    return ""


def render(template, first, address):
    if not first:
        template = template.replace("Hi {FirstName}, ", "Hi, ")
        if template.startswith("{FirstName}, "):
            template = template[len("{FirstName}, "):]
    return template.format(FirstName=first, PropertyAddress=fix_address(address))


def field(row, name):
    return (row.get(name) or "").strip().lower()


def eligible_numbers(row, skipped):
    """Return the record's usable numbers, tallying why each other number is left out."""
    if field(row, "OwnerType") != "individual":
        skipped["record: not an individual owner"] += 1
        return []
    if field(row, "PropertyType") == "vacant land":
        skipped["record: vacant land"] += 1
        return []
    parts = (row.get("FirstName") or "").split()
    if not parts or not (row.get("PropertyAddress") or "").strip():
        skipped["record: missing name or address"] += 1
        return []
    first = parts[0].lower()
    found = []
    for contact in range(1, 9):
        name = field(row, f"Contact{contact}Name")
        if not name:
            continue
        for i in (1, 2, 3):
            prefix = f"Contact{contact}Phone_{i}"
            phone = normalize(row.get(prefix))
            if not phone:
                continue
            if field(row, f"Contact{contact}Type") == "tenant":
                skipped["number: tenant"] += 1
            elif first not in re.findall(r"[a-z'-]+", name):
                skipped["number: contact name doesn't match owner"] += 1
            elif field(row, f"{prefix}_Type") != "mobile":
                skipped["number: not mobile"] += 1
            elif field(row, f"{prefix}_DNC") != "false":
                skipped["number: DNC or unknown"] += 1
            elif field(row, f"{prefix}_Litigator") != "false":
                skipped["number: litigator not screened"] += 1
            elif phone not in found:
                found.append(phone)
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--records", type=int, default=100, help="stop after this many records with a usable number")
    p.add_argument("--first-rows", type=int, help="only look at this many rows of the file")
    p.add_argument("--seconds-between", type=int, default=30)
    p.add_argument("--messages", type=Path, default=HERE / "drip_messages.json")
    args = p.parse_args()

    template = json.loads(args.messages.read_text())[0]["template"]
    skipped, seen, count, records = Counter(), set(), 0, 0
    clock = datetime.now().replace(microsecond=0)
    with args.csv.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if "Contact1Phone_1" not in (reader.fieldnames or []):
            sys.exit("This preview needs a raw skip-trace export")
        for index, row in enumerate(reader):
            if args.first_rows and index >= args.first_rows:
                break
            if records >= args.records:
                break
            numbers = [n for n in eligible_numbers(row, skipped) if n not in seen]
            if not numbers:
                continue
            records += 1
            first = greeting_name(row, row["FirstName"].split()[0].title())
            body = render(template, first, row["PropertyAddress"].strip())
            for phone in numbers:
                seen.add(phone)
                print(f"{clock:%H:%M:%S}  {phone}  {body}")
                clock += timedelta(seconds=args.seconds_between)
                count += 1

    print(f"\nPreview only, nothing sent. Records: {records}. Numbers: {count}.")
    print(f"At one text every {args.seconds_between}s, that's about {count * args.seconds_between / 60:.0f} minutes.")
    per_30 = 1800 / args.seconds_between
    if per_30 > 30:
        print(f"That's {per_30:.0f} texts per 30 minutes, over Android's usual limit of about 30.")
    print("Left out:")
    for reason, total in sorted(skipped.items()):
        print(f"  {reason}: {total}")


if __name__ == "__main__":
    main()
