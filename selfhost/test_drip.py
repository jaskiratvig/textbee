"""Tests for drip.py. Run with: python3 selfhost/test_drip.py"""
import csv
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drip  # noqa: E402


class Gateway:
    def __init__(self):
        self.replies, self.posts, self.statuses, self.fail_posts = [], [], {}, False
        self.heartbeat = None
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def respond(self, code, body):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                if self.path.endswith("/gateway/devices/d"):
                    beat = gateway.heartbeat or drip.utcnow().isoformat().replace("+00:00", "Z")
                    self.respond(200, {"data": {"lastHeartbeat": beat}})
                elif "get-received-sms" in self.path:
                    self.respond(200, {"data": gateway.replies})
                else:
                    self.respond(200, {"data": [{"recipient": p, "status": s} for p, s in gateway.statuses.items()]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if gateway.fail_posts:
                    return self.respond(500, {})
                gateway.posts.append(body)
                self.respond(201, {"data": {"smsBatchId": f"b{len(gateway.posts)}"}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1"


class DripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base = ["--db", str(self.tmp / "d.sqlite3"), "--suppress", str(self.tmp / "s.txt"),
                     "--sent-log", str(self.tmp / "sent.csv")]
        self.gw = Gateway()
        os.environ["TEXTBEE_API_KEY"] = "k"
        self.set_time(2026, 11, 5, 16)  # 10am Central
        self.addCleanup(setattr, drip, "utcnow", drip.utcnow)

    def set_time(self, y, m, d, h):
        drip.utcnow = lambda: datetime(y, m, d, h, tzinfo=timezone.utc)

    def add(self, rows):
        path = self.tmp / "c.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["phone", "FirstName", "PropertyAddress"])
            w.writerows(rows)
        drip.main(self.base + ["import", "--csv", str(path)])

    def run_live(self, *extra):
        drip.main(self.base + ["run", "--api-url", self.gw.url, "--device-id", "d", "--live", *extra])

    def contact(self, phone):
        return drip.connect(self.tmp / "d.sqlite3").execute("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()

    def recipients(self):
        return [m["recipients"][0] for p in self.gw.posts for m in p["messages"]]

    def test_full_schedule_then_completed(self):
        self.add([["3125550100", "sam", "1 Main St"]])
        for day, month in [(5, 11), (10, 11), (26, 11), (4, 1)]:
            self.set_time(2027 if month == 1 else 2026, month, day, 16)
            self.run_live()
        bodies = [p["messages"][0]["message"] for p in self.gw.posts]
        steps = json.loads((Path(drip.__file__).parent / "drip_messages.json").read_text())
        self.assertEqual(bodies, [s["template"].format(FirstName="Sam", PropertyAddress="1 Main St") for s in steps])
        self.assertEqual(self.contact("+13125550100")["status"], "completed")

    def test_not_sent_before_due(self):
        self.add([["3125550100", "Sam", "1 Main St"]])
        self.run_live()
        self.set_time(2026, 11, 9, 16)
        self.run_live()
        self.assertEqual(len(self.gw.posts), 1)

    def test_any_reply_stops_and_stop_suppresses(self):
        self.add([["3125550100", "A", "1 St"], ["3125550101", "B", "2 St"], ["3125550102", "C", "3 St"]])
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "Stop"},
                           {"_id": "2", "sender": "+13125550101", "message": "how much?"},
                           {"_id": "3", "sender": "+13125550102", "message": "wrong number"}]
        self.run_live()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self.contact("+13125550100")["status"], "opted_out")
        self.assertEqual(self.contact("+13125550101")["status"], "replied")
        self.assertEqual(self.contact("+13125550102")["status"], "wrong_number")
        self.assertIn("+13125550100", (self.tmp / "s.txt").read_text())
        self.assertNotIn("+13125550101", (self.tmp / "s.txt").read_text())

    def test_stop_after_earlier_reply_still_suppresses(self):
        self.add([["3125550100", "A", "1 St"]])
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "who is this"}]
        self.run_live()
        self.gw.replies.append({"_id": "2", "sender": "+13125550100", "message": "STOP"})
        self.run_live()
        self.assertEqual(self.contact("+13125550100")["status"], "opted_out")

    def test_suppressed_never_enrolled_or_sent(self):
        (self.tmp / "s.txt").write_text("3125550100\n")
        self.add([["3125550100", "A", "1 St"], ["3125550101", "B", "2 St"]])
        self.run_live()
        self.assertEqual(self.recipients(), ["+13125550101"])

    def test_outside_hours_syncs_but_does_not_send(self):
        self.add([["3125550100", "A", "1 St"]])
        self.set_time(2026, 11, 5, 4)  # 10pm Central
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "STOP"}]
        self.run_live()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self.contact("+13125550100")["status"], "opted_out")

    def test_unreadable_replies_block_sending(self):
        self.add([["3125550100", "A", "1 St"]])
        self.gw.server.shutdown()
        self.gw.server.server_close()
        with self.assertRaises(SystemExit):
            self.run_live()
        self.assertEqual(self.contact("+13125550100")["step"], 0)

    def test_stale_heartbeat_blocks_sending(self):
        self.add([["3125550100", "A", "1 St"]])
        self.gw.heartbeat = "2026-11-05T13:00:00.000Z"  # 3 hours before the test clock
        with self.assertRaises(SystemExit):
            self.run_live()
        with self.assertRaises(SystemExit):
            drip.main(self.base + self.write_test_csv())
        self.assertEqual(self.gw.posts, [])
        self.gw.heartbeat = "2026-11-05T15:30:00.000Z"
        self.run_live()
        self.assertEqual(len(self.gw.posts), 1)

    def test_import_limit_and_no_reenrollment(self):
        path = self.tmp / "c.csv"
        path.write_text("phone,FirstName,PropertyAddress\n3125550100,A,1 St\n3125550101,B,2 St\n3125550102,C,3 St\n")
        drip.main(self.base + ["import", "--csv", str(path), "--limit", "2"])
        self.assertIsNone(self.contact("+13125550102"))
        self.run_live()
        drip.main(self.base + ["import", "--csv", str(path), "--limit", "1"])
        self.assertEqual(self.contact("+13125550100")["step"], 1)
        self.assertIsNotNone(self.contact("+13125550102"))

    def test_history_export(self):
        self.add([["3125550100", "Sam", "1 St"]])
        self.run_live()
        out = self.tmp / "h.csv"
        drip.main(self.base + ["history", "--out", str(out)])
        rows = list(csv.DictReader(out.open()))
        self.assertEqual(rows[0]["phone"], "+13125550100")
        self.assertEqual(rows[0]["direction"], "out")

    def write_raw(self, rows):
        fields = sorted({k for row in rows for k in row})
        path = self.tmp / "raw2.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(rows)
        return path

    def multi_row(self):
        row = {"OwnerType": "INDIVIDUAL", "PropertyType": "Single Family", "FirstName": "SAM", "PropertyAddress": "1 St",
               "Contact1Name": "Sam Lee", "Contact2Name": "Sam Lee Jr", "Contact3Name": "Pat Doe", "Contact4Name": "Sam Tenant", "Contact4Type": "Tenant"}
        for name, phone, kind, dnc in [("Contact1Phone_1", "3125550100", "Mobile", "False"), ("Contact1Phone_2", "3125550101", "mobile", "False"),
                                       ("Contact1Phone_3", "3125550102", "mobile", "True"), ("Contact2Phone_1", "3125550103", "mobile", "False"),
                                       ("Contact3Phone_1", "3125550104", "mobile", "False"), ("Contact4Phone_1", "3125550105", "mobile", "False")]:
            row.update({name: phone, f"{name}_Type": kind, f"{name}_DNC": dnc, f"{name}_Litigator": "False"})
        return row

    def test_all_numbers_import(self):
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row()])), "--all-numbers"])
        db = drip.connect(self.tmp / "d.sqlite3")
        self.assertEqual(sorted(r["phone"] for r in db.execute("SELECT phone FROM contacts")),
                         ["+13125550100", "+13125550101", "+13125550103"])

    def test_default_import_takes_one_number(self):
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row()]))])
        db = drip.connect(self.tmp / "d.sqlite3")
        self.assertEqual([r["phone"] for r in db.execute("SELECT phone FROM contacts")], ["+13125550100"])

    def test_limit_counts_records_with_all_numbers(self):
        second = self.multi_row()
        second["PropertyAddress"] = "2 St"
        for key in list(second):
            if key.endswith(("Phone_1", "Phone_2", "Phone_3")):
                second[key] = "312555" + str(int(second[key][6:]) + 100).zfill(4)
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row(), second])), "--all-numbers", "--limit", "1"])
        db = drip.connect(self.tmp / "d.sqlite3")
        self.assertEqual({r["property_address"] for r in db.execute("SELECT property_address FROM contacts")}, {"1 St"})

    def test_each_reply_stops_only_that_number(self):
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row()])), "--all-numbers"])
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "STOP"},
                           {"_id": "2", "sender": "+13125550101", "message": "who is this"}]
        self.run_live()
        self.assertEqual(self.contact("+13125550100")["status"], "opted_out")
        self.assertEqual(self.contact("+13125550101")["status"], "replied")
        self.assertEqual(self.contact("+13125550103")["status"], "active")
        self.assertEqual(self.recipients(), ["+13125550103"])
        self.assertNotIn("+13125550103", (self.tmp / "s.txt").read_text())

    def test_wrong_number_leaves_other_numbers_active(self):
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row()])), "--all-numbers"])
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "wrong number"}]
        self.run_live()
        self.assertEqual(self.contact("+13125550100")["status"], "wrong_number")
        self.assertEqual(self.contact("+13125550101")["status"], "active")

    def test_decline_stops_only_that_number(self):
        drip.main(self.base + ["import", "--csv", str(self.write_raw([self.multi_row()])), "--all-numbers"])
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "No thanks!"}]
        self.run_live()
        self.assertEqual(self.contact("+13125550100")["status"], "declined")
        self.assertEqual(self.contact("+13125550101")["status"], "active")
        self.assertIn("+13125550100", (self.tmp / "s.txt").read_text())

    def test_reply_classification(self):
        for text, expected in [("No", "declined"), ("no.", "declined"), ("not interested", "declined"),
                               ("Not selling, sorry", "declined"), ("No thanks, leave me alone", "opted_out"),
                               ("stop", "opted_out"), ("Wrong number", "wrong_number"),
                               ("No Sam here", "replied"), ("Not sure, how much?", "replied"),
                               ("Maybe", "replied"), ("Nobody lives there", "replied")]:
            self.assertEqual(drip.classify_reply(text), expected, text)

    def test_render_fixes_address_and_blank_name(self):
        steps = json.loads((Path(drip.__file__).parent / "drip_messages.json").read_text())
        self.assertIn("104th St", drip.render(steps[0]["template"], "Sam", "131 W 104Th St"))
        self.assertIn("71st", drip.fix_address("915 W 71St St"))
        self.assertEqual(drip.fix_address("12 S State St"), "12 S State St")
        self.assertTrue(drip.render(steps[0]["template"], "", "1 St").startswith("Hi, this is Jas"))
        self.assertTrue(drip.render(steps[1]["template"], "", "1 St").startswith("Hi, Jas again"))
        self.assertTrue(drip.render(steps[2]["template"], "", "1 St").startswith("Jas from Vig Estates"))
        self.assertTrue(drip.render(steps[3]["template"], "", "1 St").startswith("Jas from Vig Estates"))

    def test_greeting_name(self):
        self.assertEqual(drip.greeting_name({"Contact1Name": "Sam Lee"}, "Sam"), "Sam")
        self.assertEqual(drip.greeting_name({"Contact1Name": "M Jones"}, "M"), "")
        self.assertEqual(drip.greeting_name({"Contact1Name": "Keith Baines"}, "Baines"), "")
        self.assertEqual(drip.greeting_name({"Contact1Name": "Luis Cruz", "Contact2Name": "Patricia Cruz"}, "Patricia"), "Patricia")

    def test_limits(self):
        self.add([[f"31255501{i:02d}", "A", "1 St"] for i in range(40)])
        self.run_live("--daily-limit", "25", "--new-per-day", "5")
        self.assertEqual(len(self.recipients()), 5)
        self.run_live("--daily-limit", "25", "--new-per-day", "5")
        self.assertEqual(len(self.recipients()), 5)

    def test_failed_send_never_retries_on_its_own(self):
        self.add([["3125550100", "A", "1 St"]])
        self.gw.fail_posts = True
        with self.assertRaises(SystemExit):
            self.run_live()
        self.assertEqual(self.contact("+13125550100")["status"], "failed")
        self.gw.fail_posts = False
        self.run_live()
        self.assertEqual(self.gw.posts, [])
        drip.main(self.base + ["retry"])
        self.run_live()
        self.assertEqual(len(self.gw.posts), 1)

    def test_breaker_pauses_after_failures(self):
        self.add([[f"31255501{i:02d}", "A", "1 St"] for i in range(25)])
        self.run_live("--batch-size", "20", "--daily-limit", "40", "--new-per-day", "40")
        self.gw.statuses = {r: "failed" for r in self.recipients()}
        with self.assertRaises(SystemExit):
            self.run_live("--batch-size", "20", "--daily-limit", "40", "--new-per-day", "40")
        self.assertEqual(len(self.gw.posts), 1)

    def write_test_csv(self):
        path = self.tmp / "t.csv"
        path.write_text("phone,first_name,address\n3125550100,Sam,1 Main St\n")
        return ["test", "--csv", str(path), "--api-url", self.gw.url, "--device-id", "d"]

    def test_test_command_sends_all_steps_without_touching_campaign(self):
        drip.main(self.base + self.write_test_csv())
        self.assertEqual(len(self.gw.posts[0]["messages"]), 4)
        self.assertIn("Sam", self.gw.posts[0]["messages"][0]["message"])
        self.assertIsNone(self.contact("+13125550100"))

    def test_test_command_refuses_opted_out_number(self):
        self.gw.replies = [{"_id": "1", "sender": "+13125550100", "message": "STOP"}]
        with self.assertRaises(SystemExit):
            drip.main(self.base + self.write_test_csv())
        self.assertEqual(self.gw.posts, [])

    def test_test_command_respects_hours(self):
        self.set_time(2026, 11, 5, 4)
        with self.assertRaises(SystemExit):
            drip.main(self.base + self.write_test_csv())
        self.assertEqual(self.gw.posts, [])

    def test_raw_export_rules(self):
        fields = ["OwnerType", "PropertyType", "FirstName", "Contact1Name", "Contact1Type", "PropertyAddress",
                  "Contact1Phone_1", "Contact1Phone_1_Type", "Contact1Phone_1_DNC", "Contact1Phone_1_Litigator"]
        def row(phone, **kw):
            base = dict(zip(fields, ["INDIVIDUAL", "Single Family", "SAM", "Sam Lee", "", "1 St", phone, "mobile", "False", "False"]))
            base.update(kw)
            return base
        rows = [row("3125550100"), row("3125550101", Contact1Phone_1_DNC="True"),
                row("3125550102", Contact1Phone_1_Litigator=""), row("3125550103", Contact1Type="Tenant"),
                row("3125550104", OwnerType="BUSINESS"), row("3125550105", Contact1Name="Pat Doe"),
                row("3125550106", Contact1Phone_1_Type="Residential"), row("3125550107", PropertyType="Vacant Land")]
        path = self.tmp / "raw.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(rows)
        drip.main(self.base + ["import", "--csv", str(path)])
        db = drip.connect(self.tmp / "d.sqlite3")
        self.assertEqual([r["phone"] for r in db.execute("SELECT phone FROM contacts")], ["+13125550100"])
        self.assertEqual(self.contact("+13125550100")["first_name"], "Sam")


if __name__ == "__main__":
    unittest.main()
