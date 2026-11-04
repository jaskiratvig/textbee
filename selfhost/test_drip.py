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
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def respond(self, code, body):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                if "get-received-sms" in self.path:
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
