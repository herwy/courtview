"""cv_purge_pii.py on a legacy-schema fixture DB. Run on the RPi: python3 -m unittest tests.test_purge -v"""
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-purge-test-")
_KEY = os.path.join(_TMP, "hmac_key")
with open(_KEY, "w") as _fh:
    _fh.write("ef" * 32)
os.chmod(_KEY, 0o600)
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["COURTVIEW_HMAC_KEY_PATH"] = _KEY
sys.path.insert(0, REPO)

_spec = importlib.util.spec_from_file_location("cv_purge_under_test", os.path.join(REPO, "cv_purge_pii.py"))
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)
cv = purge.cv

S_NAME = "Zed Sentinelson"
S_EMAIL = "zed.sentinelson@example.test"
S_PHONE = "07700 900123"
S_RECEIPT = "https://receipts.example.test/zed-sentinelson-0001.pdf"
RAW_SENTINELS = [S_NAME.encode(), b"Sentinelson", S_EMAIL.encode(), S_PHONE.encode(), b"example.test", b"900123"]
CLUB = cv.HEATMAP_CLUBS[0]["id"]
RACK = cv.RACKETEER_CLUB_ID

# Legacy payments schema, copied literally from git show a05a910:courtview.py
LEGACY_PAYMENTS = """CREATE TABLE IF NOT EXISTS payments (
                   club_id        TEXT NOT NULL,
                   transaction_id TEXT NOT NULL,
                   created_utc    INTEGER NOT NULL,
                   local_day      TEXT NOT NULL,
                   created_at     TEXT,
                   player_name    TEXT,
                   player_email   TEXT,
                   player_phone   TEXT,
                   source_type    TEXT,
                   description    TEXT,
                   amount         TEXT,
                   amount_gbp     REAL,
                   status         TEXT,
                   receipt_url    TEXT,
                   synced_at      INTEGER,
                   PRIMARY KEY (club_id, transaction_id)
               )"""


class PurgeTests(unittest.TestCase):
    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cv.DB_PATH + ext)
            except FileNotFoundError:
                pass
        cv.init_db()
        c = sqlite3.connect(cv.DB_PATH)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("DROP TABLE payments")
        c.execute(LEGACY_PAYMENTS)
        rows = [
            ("t1", "2026-09-10 10:00:00", S_EMAIL, f"Court for {S_NAME}"),
            ("t2", "2026-09-11 10:00:00", " ZED.Sentinelson@Example.test", "Court"),
            ("synthetic:2026-09-12 10:00:00|" + S_EMAIL + "|5.00 GBP|PAYMENT_SUCCESS", "2026-09-12 10:00:00", S_EMAIL, "x"),
            ("t4", "2026-09-13 10:00:00", "", "No email row"),
        ]
        for tid, created, email, desc in rows:
            c.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (CLUB, tid, 1, "2026-09-10", created, S_NAME, email, S_PHONE, "booking", desc,
                       "10.00 GBP", 10.0, "PAYMENT_SUCCESS", S_RECEIPT, 5))
        rec = {"transaction_id": "a", "player_name": S_NAME, "player_email": S_EMAIL, "player_phone": S_PHONE,
               "source_type": "booking", "description": f"for {S_NAME}", "amount": "12.00 GBP",
               "status": "PAYMENT_SUCCESS", "receiptUrl": S_RECEIPT, "createdAt": "2026-06-16 10:00:00"}
        for payload, ep in ((json.dumps({"payments": [rec], "has_more": False}), "payment-history"),
                            ("not json " + S_NAME, "payment-history"),
                            (json.dumps({"total": 5, "contact": S_EMAIL}), "revenue-summary")):
            c.execute("INSERT INTO archive_financial (club_id,start_time,end_time,payload,endpoint,captured_at) "
                      "VALUES (?,?,?,?,?,?)", (RACK, "s", "e", payload, ep, 100))
        c.execute("INSERT INTO archive_club_info (club_id,payload,captured_at) VALUES (?,?,?)",
                  (RACK, json.dumps({"profile": {"name": "C", "email": S_EMAIL, "phone": S_PHONE}}), 100))
        avail = {"allSlots": [], "reservedBookings": [{"title": f"PLAYER: {S_NAME}", "court_ids": ["c1"]}]}
        c.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (CLUB, "1", "2", json.dumps(avail), 5))
        c.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (CLUB, "3", "4", S_NAME + " not json", 5))
        c.execute("INSERT INTO membership_members_cache VALUES (?,?,?)",
                  (CLUB, json.dumps({"counts": {}, "members": {"p": [{"name": S_NAME, "email": S_EMAIL}]}}), 5))
        c.execute("INSERT INTO stats_cache VALUES (?,?,?)", ("k", json.dumps({"coach_name": S_NAME}), 5))
        c.execute("INSERT INTO club_info_cache VALUES (?,?,?)", (CLUB, json.dumps({"email": S_EMAIL}), 5))
        c.commit()
        c.close()

    def run_main(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = purge.main(list(args))
        return rc, buf.getvalue()

    def raw_bytes(self):
        out = b""
        for ext in ("", "-wal"):
            try:
                with open(cv.DB_PATH + ext, "rb") as fh:
                    out += fh.read()
            except FileNotFoundError:
                pass
        return out

    def test_check_reports_needed_with_counts(self):
        rc, out = self.run_main("--check")
        self.assertEqual(rc, 0)
        self.assertIn("WOULD payments_rows_to_rebuild=4", out)
        self.assertIn("WOULD payments_synthetic_ids_to_rewrite=1", out)
        for s in (S_NAME, S_EMAIL, S_PHONE):
            self.assertNotIn(s, out)

    def test_purge_removes_pii_and_keeps_data(self):
        self.assertTrue(any(s in self.raw_bytes() for s in RAW_SENTINELS))
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("payments_rows_rebuilt=4", out)
        self.assertIn("synthetic_ids_rewritten=1", out)

        c = sqlite3.connect(cv.DB_PATH)
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(payments)")}
            self.assertEqual(cols, set(cv._PAYMENTS_COLUMNS))
            rows = {r[0]: r for r in c.execute("SELECT transaction_id, customer_key, description, amount FROM payments")}
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows["t1"][1], cv._customer_key(S_EMAIL))
            self.assertEqual(rows["t2"][1], rows["t1"][1])  # case/whitespace variants match
            self.assertNotIn("Sentinelson", rows["t1"][2])
            self.assertEqual(rows["t1"][3], "10.00 GBP")
            self.assertIsNone(rows["t4"][1])
            self.assertTrue(all("@" not in tid for tid in rows))
            self.assertTrue(any(tid.startswith("synthetic:") for tid in rows))
            for t in ("membership_members_cache", "stats_cache", "club_info_cache"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0)
            pay = [r[0] for r in c.execute("SELECT payload FROM archive_financial WHERE endpoint='payment-history'")]
            self.assertEqual(len(pay), 1)  # the unparseable payload row is deleted
            doc = json.loads(pay[0])
            self.assertEqual(doc["payments"][0]["amount"], "12.00 GBP")
            self.assertEqual(doc["payments"][0]["createdAt"], "2026-06-16 10:00:00")
            self.assertEqual(doc["payments"][0]["status"], "PAYMENT_SUCCESS")
            self.assertEqual(doc["payments"][0]["customer_key"], cv._customer_key(S_EMAIL))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM availability").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='pii_purge'").fetchone() is not None, True)
        finally:
            c.close()

        data = self.raw_bytes()
        self.assertEqual([s for s in RAW_SENTINELS if s in data], [])
        self.assertIsNone(re.search(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", data))

        rc, out = self.run_main("--check")
        self.assertEqual((rc, out.strip()), (1, "clean"))

    def test_forced_second_run_changes_nothing(self):
        self.run_main()
        c = sqlite3.connect(cv.DB_PATH)
        before = c.execute("SELECT customer_key, transaction_id, description FROM payments ORDER BY 2").fetchall()
        c.close()
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("payments_rows_rebuilt=0", out)
        c = sqlite3.connect(cv.DB_PATH)
        after = c.execute("SELECT customer_key, transaction_id, description FROM payments ORDER BY 2").fetchall()
        c.close()
        self.assertEqual(before, after)

    def test_refuses_without_key(self):
        old = cv.HMAC_KEY_PATH
        cv.HMAC_KEY_PATH = os.path.join(_TMP, "missing")
        try:
            rc, out = self.run_main()
        finally:
            cv.HMAC_KEY_PATH = old
        self.assertEqual(rc, 2)
        c = sqlite3.connect(cv.DB_PATH)
        try:
            self.assertIn("player_name", {r[1] for r in c.execute("PRAGMA table_info(payments)")})
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
