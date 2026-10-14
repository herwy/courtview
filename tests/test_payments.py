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


class RouteBase(Base):
    def setUp(self):
        super().setUp()
        self.client = cv.app.test_client()
        p = mock.patch.object(cv, "_gate", lambda: (True, ""))
        p.start()
        self.addCleanup(p.stop)

    def search(self, q, club=CLUB, start=None, end=None, path="/api/payment-search"):
        start = ms(2026, 9, 1) if start is None else start
        end = ms(2026, 9, 19, 23, 59) if end is None else end
        qs = f"club_id={club}&start={start}&end={end}&q={q}"
        return self.client.get(f"{path}?{qs}")


class SearchTests(RouteBase):
    def test_validation(self):
        self.up([])
        s, e = ms(2026, 9, 1), ms(2026, 9, 19)
        for q in ("", "a", "%20a%20", "%20%20"):
            self.assertEqual(self.search(q).status_code, 400, q)
        base = f"/api/payment-search?club_id={CLUB}&q=ab"
        self.assertEqual(self.client.get(base).status_code, 400)
        self.assertEqual(self.client.get(f"{base}&start=x&end={e}").status_code, 400)
        self.assertEqual(self.client.get(f"{base}&start={e}&end={s}").status_code, 400)
        self.assertEqual(self.client.get(f"{base}&start={s}&end={s + 33 * 86400 * 1000}").status_code, 400)
        r = self.search("ab", club="othercl")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "club not synced")
        self.assertEqual(self.search("a" * 101).status_code, 400)

    def test_like_escaping(self):
        self.up([rec("1", "2026-09-05 10:00:00", name="50% Off Promo", email="x1@e.com"),
                 rec("2", "2026-09-05 11:00:00", name="500 Club", email="x2@e.com"),
                 rec("3", "2026-09-05 12:00:00", name="Ana_b", email="x3@e.com"),
                 rec("4", "2026-09-05 13:00:00", name="Anab", email="x4@e.com"),
                 rec("5", "2026-09-05 14:00:00", name="Back\\slash", email="x5@e.com")])
        names = lambda r: sorted(p["player_name"] for p in r.get_json()["payments"])
        self.assertEqual(names(self.search("0%25")), ["50% Off Promo"])
        self.assertEqual(names(self.search("a_")), ["Ana_b"])
        self.assertEqual(names(self.search("k%5Cs")), ["Back\\slash"])

    def test_case_insensitive_name_and_email(self):
        self.up([rec("1", "2026-09-05 10:00:00", name="Zed Smith", email="zed@example.com"),
                 rec("2", "2026-09-05 11:00:00", name="Other", email="smith@example.com")])
        d = self.search("SMITH").get_json()
        self.assertEqual(d["total"], 2)

    def test_cap_order_and_window(self):
        recs = [rec(f"t{i:03d}", fmt(ms(2026, 9, 3, 8) + i * 60000), name="Bob Match") for i in range(250)]
        recs.append(rec("out", "2026-09-25 10:00:00", name="Bob Match"))
        self.up(recs)
        d = self.search("bob").get_json()
        self.assertEqual(len(d["payments"]), 200)
        self.assertEqual(d["total"], 250)
        self.assertTrue(d["truncated"])
        self.assertTrue(d["complete"])
        ca = [p["createdAt"] for p in d["payments"]]
        self.assertEqual(ca, sorted(ca, reverse=True))
        self.assertIn("receiptUrl", d["payments"][0])
        self.assertNotIn("out", [p["transaction_id"] for p in d["payments"]])

    def test_second_search_makes_no_upstream_calls(self):
        fake = self.up([rec("1", "2026-09-05 10:00:00", name="Zed Smith")])
        end = ms(2026, 9, 12, 23, 59)
        self.search("zed", end=end)
        n = len(fake.calls)
        self.assertGreater(n, 0)
        self.search("zed", end=end)
        self.assertEqual(len(fake.calls), n)

    def test_late_month_is_searchable(self):
        self.up([rec("1", "2026-09-02 10:00:00", name="Zed Smith"),
                 rec("2", "2026-09-18 10:00:00", name="Zed Smith")])
        d = self.search("zed").get_json()
        self.assertEqual(sorted(p["createdAt"][:10] for p in d["payments"]), ["2026-09-02", "2026-09-18"])


class LeaderboardTests(RouteBase):
    def test_full_month_sum_and_no_second_upstream(self):
        recs, expected = [], {}
        players = ["Ann One", "Bob Two", "Cy Three", "Di Four", "Ed Five", "Flo Six", "Gus Seven"]
        for i in range(2500):
            name = players[i % 7]
            amt = 5 + (i % 11)
            day = 1 + (i % 25)
            created = fmt(ms(2026, 9, day, 8) + i)
            recs.append(rec(f"s{i}", created, name=name, amount=f"{amt}.00 GBP"))
            expected[name] = expected.get(name, 0) + amt
        for i in range(30):
            recs.append(rec(f"r{i}", fmt(ms(2026, 9, 3, 9) + i), name="Ann One", amount="99.00 GBP",
                            status="REFUND_SUCCESS"))
        fake = self.up(recs)
        url = f"/api/payment-leaderboard?club_id={CLUB}&start={ms(2026, 8, 31, 23)}&end={ms(2026, 9, 27, 22)}"
        self.now = ms(2026, 9, 28, 12)
        r = self.client.get(url)
        d = r.get_json()
        self.assertEqual(list(d.keys()), ["leaders"])
        top = sorted(expected.items(), key=lambda kv: kv[1], reverse=True)[:5]
        self.assertEqual([(l["player_name"], l["total"]) for l in d["leaders"]], [(n, float(t)) for n, t in top])
        self.assertEqual(sum(1 for _ in fake.calls) > 25, True)
        n = len(fake.calls)
        self.client.get(url)
        self.assertEqual(len(fake.calls), n)

    def test_bad_club(self):
        r = self.client.get(f"/api/payment-leaderboard?club_id=zzz&start=1&end=2")
        self.assertEqual(r.status_code, 400)


class RacketeerTests(Base):
    def setUp(self):
        super().setUp()
        self.client = cv.app.test_client()
        p = mock.patch.object(cv, "_read_token", lambda: "t")
        p.start()
        self.addCleanup(p.stop)
        c = sqlite3.connect(cv.DB_PATH)
        snap1 = {"payments": [rec("a", "2026-06-16 10:00:00", name="Rae Jones"),
                              rec("b", "2026-06-17 10:00:00", name="Other One")], "has_more": False}
        snap2 = {"payments": [rec("a", "2026-06-16 10:00:00", name="Rae Jones"),
                              rec("c", "2026-06-20 10:00:00", name="Rae Jones")], "has_more": False}
        for i, snap in enumerate((snap1, snap2)):
            c.execute("INSERT INTO archive_financial (club_id,start_time,end_time,payload,endpoint,captured_at) "
                      "VALUES (?,?,?,?,?,?)", (RACK, "s", "e", json.dumps(snap), "payment-history", 100 + i))
        c.execute("INSERT INTO archive_financial (club_id,start_time,end_time,payload,endpoint,captured_at) "
                  "VALUES (?,?,?,?,?,?)", (RACK, "s", "e", "not json", "payment-history", 103))
        c.commit()
        c.close()

    def test_live_route_410_no_upstream(self):
        fake = self.up([])
        r = self.client.get(f"/api/payment-search?club_id={RACK}&start=1&end=2&q=ab&token=t")
        self.assertEqual(r.status_code, 410)
        self.assertEqual(fake.calls, [])

    def test_archive_search_dedupes_without_upstream(self):
        fake = self.up([])
        r = self.client.get(f"/api/archive/payment-search?club_id={RACK}&start={ms(2026, 6, 1)}"
                            f"&end={ms(2026, 6, 30, 23)}&q=rae&token=t")
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(d["total"], 2)
        self.assertTrue(d["complete"])
        self.assertEqual([p["transaction_id"] for p in d["payments"]], ["c", "a"])
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
