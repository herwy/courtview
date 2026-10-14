"""Stubbed-upstream tests for the local payments table, sync, search and leaderboard."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-pay-test-")
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")

_spec = importlib.util.spec_from_file_location("courtview_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_under_test"] = cv
_spec.loader.exec_module(cv)

CLUB = cv.HEATMAP_CLUBS[0]["id"]
RACK = cv.RACKETEER_CLUB_ID


def ms(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def rec(tid, created, name="Ana Smith", email=None, amount="10.00 GBP", status="PAYMENT_SUCCESS"):
    return {
        "transaction_id": tid, "player_name": name,
        "player_email": email if email is not None else f"{name.split()[0].lower()}@example.com",
        "player_phone": "0000", "source_type": "booking", "description": "d",
        "amount": amount, "status": status, "receiptUrl": f"https://r/{tid}", "createdAt": created,
    }


class FakeUpstream:
    """Replaces cv.cffi_requests.get; serves in-memory records oldest first."""

    def __init__(self, records):
        self.records = records
        self.calls = []
        self.fail_on = None  # callable(call_index_within_window, skip) -> bool

    def __call__(self, url, **kw):
        q = parse_qs(urlparse(url).query)
        start, end = int(q["start_datetime"][0]), int(q["end_datetime"][0])
        skip, limit = int(q["skip"][0]), int(q["limit"][0])
        self.calls.append((url, kw, q))
        if self.fail_on and self.fail_on(skip):
            return _Resp(500, None)
        rows = [r for r in self.records if start <= cv._parse_created_utc(r["createdAt"]) <= end]
        rows.sort(key=lambda r: r["createdAt"])
        return _Resp(200, rows[skip:skip + limit])


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def fmt(ms_):
    return datetime.fromtimestamp(ms_ / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Base(unittest.TestCase):
    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cv.DB_PATH + ext)
            except FileNotFoundError:
                pass
        cv.init_db()
        cv.PAYMENTS_PAGE_DELAY = (0, 0)
        self.now = ms(2026, 9, 20, 12)
        p = mock.patch.object(cv, "_now", lambda: self.now / 1000)
        p.start()
        self.addCleanup(p.stop)

    def up(self, records):
        fake = FakeUpstream(records)
        p = mock.patch.object(cv.cffi_requests, "get", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def count(self, table="payments"):
        c = sqlite3.connect(cv.DB_PATH)
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()

    def sync_row(self, day):
        c = sqlite3.connect(cv.DB_PATH)
        try:
            return c.execute("SELECT synced_at, complete FROM payments_sync WHERE club_id=? AND local_day=?",
                             (CLUB, day)).fetchone()
        finally:
            c.close()


D = "2026-09-10"
D_START, D_NEXT = None, None


def day_window(day):
    return cv._london_day_bounds(day)


class SyncTests(Base):
    def test_london_day_bst(self):
        self.assertEqual(cv._london_day(cv._parse_created_utc("2026-09-17 23:03:30")), "2026-09-18")
        self.assertEqual(cv._london_day(cv._parse_created_utc("2026-01-17 23:03:30")), "2026-01-17")

    def test_upsert_idempotent_and_status_update(self):
        recs = [rec("a", "2026-09-10 10:00:00"), rec("b", "2026-09-10 11:00:00")]
        fake = self.up(recs)
        s, e = day_window(D)
        self.now = ms(2026, 9, 10, 15)  # synced during the day: not final
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(self.count(), 2)
        recs[0]["status"] = "REFUND_SUCCESS"
        self.now += 3 * 86400 * 1000  # past close: re-sync allowed once
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(self.count(), 2)
        c = sqlite3.connect(cv.DB_PATH)
        st = c.execute("SELECT status FROM payments WHERE transaction_id='a'").fetchone()[0]
        c.close()
        self.assertEqual(st, "REFUND_SUCCESS")

    def test_local_day_stored(self):
        self.up([rec("a", "2026-09-17 23:03:30")])
        s, e = day_window("2026-09-18")
        cv._sync_payments(CLUB, s, e - 1)
        c = sqlite3.connect(cv.DB_PATH)
        row = c.execute("SELECT local_day, created_utc FROM payments").fetchone()
        c.close()
        self.assertEqual(row[0], "2026-09-18")
        self.assertEqual(row[1], ms(2026, 9, 17, 23, 3, 30))

    def test_final_day_not_refetched(self):
        fake = self.up([rec("a", "2026-09-10 10:00:00")])
        s, e = day_window(D)
        self.now = e + 3 * 86400 * 1000
        res = cv._sync_payments(CLUB, s, e - 1)
        self.assertTrue(res["complete"])
        n = len(fake.calls)
        self.assertGreater(n, 0)
        self.now += 86400 * 1000
        res = cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(len(fake.calls), n)
        self.assertTrue(res["complete"])

    def test_today_freshness(self):
        fake = self.up([rec("a", "2026-09-20 09:00:00")])
        s, e = day_window("2026-09-20")
        cv._sync_payments(CLUB, s, e - 1)
        n = len(fake.calls)
        self.assertGreater(n, 0)
        self.now += 5 * 60 * 1000
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(len(fake.calls), n)
        self.now += 6 * 60 * 1000
        cv._sync_payments(CLUB, s, e - 1)
        self.assertGreater(len(fake.calls), n)

    def test_day_synced_before_close_refetched_once(self):
        fake = self.up([rec("a", "2026-09-20 09:00:00")])
        s, e = day_window("2026-09-20")
        cv._sync_payments(CLUB, s, e - 1)  # synced at 12:00 on the day itself
        n = len(fake.calls)
        self.now += 3 * 86400 * 1000
        cv._sync_payments(CLUB, s, e - 1)
        n2 = len(fake.calls)
        self.assertGreater(n2, n)
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(len(fake.calls), n2)

    def test_failure_incomplete_and_retry_floor(self):
        recs = [rec(f"t{i}", fmt(ms(2026, 9, 10, 8) + i * 1000)) for i in range(150)]
        fake = self.up(recs)
        fake.fail_on = lambda skip: skip >= 100
        s, e = day_window(D)
        res = cv._sync_payments(CLUB, s, e - 1)
        self.assertFalse(res["complete"])
        self.assertEqual(self.sync_row(D)[1], 0)
        n = len(fake.calls)
        self.assertEqual(n, 2)
        self.now += 5 * 60 * 1000
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(len(fake.calls), n)
        self.now += 6 * 60 * 1000
        fake.fail_on = None
        res = cv._sync_payments(CLUB, s, e - 1)
        self.assertGreater(len(fake.calls), n)
        self.assertTrue(res["complete"])
        self.assertEqual(self.count(), 150)

    def test_page_cap(self):
        recs = [rec(f"t{i}", fmt(ms(2026, 9, 10, 8) + i * 1000)) for i in range(500)]
        fake = self.up(recs)
        s, e = day_window(D)
        with mock.patch.object(cv, "PAYMENTS_MAX_PAGES_PER_DAY", 2):
            res = cv._sync_payments(CLUB, s, e - 1)
        self.assertFalse(res["complete"])
        self.assertEqual(len(fake.calls), 2)

    def test_racketeer_and_unknown_club_never_contact_upstream(self):
        fake = self.up([rec("a", "2026-09-10 10:00:00")])
        s, e = day_window(D)
        for club in (RACK, "someotherclub"):
            res = cv._sync_payments(club, s, e - 1)
            self.assertFalse(res["complete"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.count(), 0)

    def test_call_kwargs_and_limit(self):
        fake = self.up([rec(f"t{i}", fmt(ms(2026, 9, 10, 8) + i * 1000)) for i in range(250)])
        s, e = day_window("2026-09-10")
        cv._sync_payments(CLUB, s, e - 1)
        self.assertEqual(len(fake.calls), 3)
        for url, kw, q in fake.calls:
            self.assertIs(kw["headers"], cv.APP_HEADERS)
            self.assertEqual(kw["impersonate"], "chrome110")
            self.assertEqual(kw["timeout"], 15)
            self.assertLessEqual(int(q["limit"][0]), 100)
        self.assertEqual(self.count(), 250)


if __name__ == "__main__":
    unittest.main()
