"""PII regression tests: no personal data is stored or returned by any route.

Run on the RPi (needs Flask and curl_cffi): python3 -m unittest tests.test_pii -v
All sentinels are obviously fake (example.test addresses, Ofcom drama-range numbers).
"""
import ast
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-pii-test-")
_KEY = os.path.join(_TMP, "hmac_key")
with open(_KEY, "w") as _fh:
    _fh.write("ab" * 32)
os.chmod(_KEY, 0o600)
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["COURTVIEW_DATA_DIR"] = _TMP
os.environ["COURTVIEW_HMAC_KEY_PATH"] = _KEY

_spec = importlib.util.spec_from_file_location("courtview_pii_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_pii_under_test"] = cv
_spec.loader.exec_module(cv)

CLUB = cv.HEATMAP_CLUBS[0]["id"]
RACK = cv.RACKETEER_CLUB_ID

S_NAME = "Zed Sentinelson"
S_FIRST = "Sentinelson"
S_EMAIL = "zed.sentinelson@example.test"
S_PHONE = "07700 900123"
S_PID = "firebase-sentinel-0001"
S_RECEIPT = "https://receipts.example.test/zed-sentinelson-0001.pdf"
S_COACH = "Coachy Sentinelcoach"
SENTINELS = [S_NAME, S_FIRST, S_EMAIL, S_PHONE, S_PID, S_RECEIPT, S_COACH, "Sentinelcoach", "900123"]

EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
PHONE_RE = re.compile(r"(?<![0-9A-Za-z])(?:\+44 ?\(?0?\)?|0)7\d{3} ?\d{3} ?\d{3}(?![0-9A-Za-z])")


def ms(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def pay_rec(tid, created, email=S_EMAIL, amount="10.00 GBP", status="PAYMENT_SUCCESS", name=S_NAME,
            desc=None):
    return {
        "transaction_id": tid, "player_name": name, "player_email": email, "player_phone": S_PHONE,
        "source_type": "booking", "description": desc if desc is not None else f"Court booking for {name}",
        "amount": amount, "status": status, "receiptUrl": S_RECEIPT, "createdAt": created,
    }


class Resp:
    def __init__(self, body, code=200):
        self.status_code = code
        self._body = body
        self.content = json.dumps(body).encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class Upstream:
    """Universal PII fixture: every upstream URL answers with sentinel-laden data."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        u = urlparse(url)
        q = parse_qs(u.query)
        path = u.path
        if path.endswith("/online_payment_history"):
            skip = int(q["skip"][0])
            start, end = int(q["start_datetime"][0]), int(q["end_datetime"][0])
            recs = [pay_rec("t1", "2026-09-10 10:00:00"), pay_rec("t2", "2026-09-10 11:00:00", amount="5.00 GBP"),
                    pay_rec("t3", "2026-09-11 11:00:00", email=" Zed.Sentinelson@Example.TEST ", amount="7.50 GBP"),
                    pay_rec("t4", "2026-09-12 09:00:00", email="other.person@example.test", name="Other Person",
                            amount="3.00 GBP")]
            recs = [r for r in recs if start <= cv._parse_created_utc(r["createdAt"]) <= end]
            return Resp(recs[skip:skip + 100])
        if path == "/club/membership/member":
            return Resp([{"name": S_NAME, "email": S_EMAIL}])
        if path == "/club/membership/":
            return Resp({"memberships": [{"_id": "plan1", "name": "Gold", "price": 10, "total_members": 3,
                                          "created_by": S_NAME, "members": [{"name": S_NAME}]}]})
        if path == "/club/":
            return Resp({"name": "Test Club", "email": S_EMAIL, "phone": S_PHONE, "address": "1 High St",
                         "owner": S_NAME, "coaches": [{"name": S_COACH}], "website": "club.example.test"})
        if path == "/club/creditpackage/":
            return Resp([{"name": "Pack", "contact_email": S_EMAIL}])
        if path == "/home/activity/filtered_activities":
            return Resp({"activities": [{
                "start_datetime": 1000, "end_datetime": 5000, "activity_type": "Book Court",
                "booking_type": "OPEN_MATCH", "no_of_participants": 2, "total_capacity": 4,
                "payment_per_person": 6.5, "activity_title": f"PLAYER: {S_NAME}",
                "courts": [{"id": "c1", "court_id": "c1", "name": "Court 1", "owner": S_NAME}],
                "players": [{"name": S_NAME, "email": S_EMAIL}], "coach_id": "cc1", "coach_name": S_COACH,
                "booker_name": S_NAME}]})
        if path == "/club/statistics/financial/v2":
            return Resp({"total_combined_payments": 100, "contact": S_EMAIL, "revenue_data": [{"x": "Mon", "y": "5"}]})
        if path == "/club/statistics/coach":
            def row(cid, name, sessions):
                return {"coach_id": cid, "coach_name": name, "total_sessions": sessions, "total_revenue": 10 * sessions,
                        "total_participants": sessions, "total_training_hours": 1.5, "email": S_EMAIL,
                        "photo_url": S_RECEIPT}
            return Resp({"total_sessions": 8, "overall_revenue": "80",
                         "group": {"total_sessions": 8, "coach_statistics": [row("cc1", S_COACH, 3), row("cc2", "Other Coach", 5)]},
                         "private": {"total_sessions": 0, "coach_statistics": []}})
        if path == "/club/statistics/operational":
            return Resp({"hottest_time_slots": [{"x": "10:00", "y": 3}],
                         "week_day_wise_activity_count_combined_graph": [{"x": "Mon", "y": 2}],
                         "court_wise_activity_count_combined_graph": [{"x": "Court 1", "y": 4}]})
        if path == "/player/player_booking/all_courts_slot_prices_v3":
            return Resp({"allSlots": [], "reservedBookings": [{
                "start_datetime": 1000, "end_datetime": 5000, "court_ids": ["c1"],
                "title": f"PLAYER: {S_NAME}", "courts": [{"court_id": "c1", "court_name": "Court 1"}],
                "open_match": {"players": {"a": {"name": S_NAME, "user_id": S_PID}}}}]})
        if path == "/player/player_booking/search_clubs":
            return Resp([{"name": "Test Club", "owner_email": S_EMAIL}])
        return Resp({"note": "default"})


class Base(unittest.TestCase):
    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cv.DB_PATH + ext)
            except FileNotFoundError:
                pass
        cv.init_db()
        cv._load_dashboard()
        cv.PAYMENTS_PAGE_DELAY = (0, 0)
        self.now = ms(2026, 9, 20, 12)
        for p in (mock.patch.object(cv, "_now", lambda: self.now / 1000),
                  mock.patch.object(cv, "_gate", lambda: (True, "")),
                  mock.patch.object(cv, "_resolve_ip", lambda ip: None),
                  mock.patch.object(cv.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)
        self.up = Upstream()
        cv.UPSTREAM.reset()
        self.addCleanup(cv.UPSTREAM.reset)
        p = mock.patch.object(cv.UPSTREAM, "_new_session",
                              lambda: types.SimpleNamespace(get=self.up, close=lambda: None))
        p.start()
        self.addCleanup(p.stop)
        self.client = cv.app.test_client()

    def window(self):
        return f"club_id={CLUB}&start={ms(2026, 9, 1)}&end={ms(2026, 9, 19, 23, 59)}"

    def db(self):
        return sqlite3.connect(cv.DB_PATH)


def has_pii(text):
    low = text.lower()
    return [s for s in SENTINELS if s.lower() in low]


class SchemaTests(Base):
    BAD = re.compile(r"name|email|phone|receipt|firebase|player_id|user_id", re.IGNORECASE)
    ALLOWED = {("court_popularity", "court_name"), ("sqlite_sequence", "name")}

    def test_schema_has_no_pii_columns(self):
        c = self.db()
        try:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            bad = []
            for t in tables:
                for col in c.execute(f'PRAGMA table_info("{t}")'):
                    if self.BAD.search(col[1]) and (t, col[1]) not in self.ALLOWED:
                        bad.append((t, col[1]))
        finally:
            c.close()
        self.assertEqual(bad, [])


class AstTests(unittest.TestCase):
    def test_cffi_calls_compliant(self):
        """All upstream traffic goes through UpstreamClient: cffi_requests and TARGET appear nowhere
        else, and inside the class every Session and session.get carries APP_HEADERS and chrome110."""
        tree = ast.parse(open(os.path.join(REPO, "courtview.py")).read())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "UpstreamClient")
        inside = {id(n) for n in ast.walk(cls)}
        outside = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "cffi_requests" and id(node) not in inside:
                if not isinstance(node.ctx, ast.Store):
                    outside.append(("cffi_requests", node.lineno))
            if isinstance(node, ast.Name) and node.id == "TARGET" and isinstance(node.ctx, ast.Load) \
                    and id(node) not in inside:
                outside.append(("TARGET", node.lineno))
            if isinstance(node, ast.Attribute) and node.attr == "TARGET" and id(node) not in inside:
                outside.append(("TARGET", node.lineno))
        sess = gets = bad = 0
        for node in ast.walk(cls):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            recv = node.func.value
            is_ctor = isinstance(recv, ast.Name) and recv.id == "cffi_requests" and node.func.attr == "Session"
            is_get = isinstance(recv, ast.Name) and recv.id == "session" and node.func.attr == "get"
            if not (is_ctor or is_get):
                continue
            sess += is_ctor
            gets += is_get
            kws = {k.arg: k.value for k in node.keywords}
            ok = isinstance(kws.get("headers"), ast.Name) and kws["headers"].id == "APP_HEADERS" \
                and isinstance(kws.get("impersonate"), ast.Constant) and kws["impersonate"].value == "chrome110"
            bad += 0 if ok else 1
        print(f"\nAST: {sess} Session ctor, {gets} session.get calls, {bad} non-compliant, "
              f"{len(outside)} cffi_requests/TARGET uses outside UpstreamClient")
        self.assertEqual(outside, [])
        self.assertGreaterEqual(sess, 1)
        self.assertGreaterEqual(gets, 1)
        self.assertEqual(bad, 0)


class PrimitiveTests(unittest.TestCase):
    def test_scrubber(self):
        out = cv._scrub_pii({
            "title": f"PLAYER: {S_NAME}", "activity_title": "Open match", "players": [{"name": S_NAME}],
            "player_email": S_EMAIL, "note": f"mail {S_EMAIL} or call {S_PHONE}", "address": "1 High St",
            "n": 3, "members": 5, "nested": [{"phoneNumber": S_PHONE, "ok": "fine"}], "userId": S_PID})
        self.assertEqual(out, {"activity_title": "Open match", "note": "mail  or call ", "address": "1 High St",
                               "n": 3, "members": 5, "nested": [{"ok": "fine"}]})

    def test_customer_key_normalised(self):
        a = cv._customer_key("Zed@Example.test ")
        b = cv._customer_key("zed@example.test")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        self.assertIsNone(cv._customer_key(""))

    def test_regexes_ignore_ids_and_epochs(self):
        for text in ("5f3a9c0b7d4e2a1b8c6d9e0f", "1789012345678", "ab07700900123cd"):
            self.assertEqual(cv._redact_text(text), text)
        self.assertEqual(cv._redact_text("call 07700 900123 now"), "call  now")
        self.assertEqual(cv._redact_text("call +44 7700 900123 now"), "call  now")


class IngestTests(Base):
    def sync(self):
        return cv._sync_payments(CLUB, ms(2026, 9, 1), ms(2026, 9, 19, 23, 59))

    def test_db_has_no_pii_after_ingest(self):
        self.sync()
        for url in (f"/api/club-info?club_id={CLUB}", f"/api/membership-members?club_id={CLUB}",
                    f"/api/coach-stats?{self.window()}", f"/api/revenue-summary?{self.window()}",
                    f"/api/activity-summary?{self.window()}",
                    f"/api/player/player_booking/all_courts_slot_prices_v3?club_id={CLUB}&start_datetime=1&end_datetime=2"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get(f"/api/booked-hours?{self.window()}").status_code, 410)
        cv._fetch_heatmap_for_club(CLUB, 7)
        c = self.db()
        try:
            hits, rows = [], 0
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for row in c.execute(f'SELECT * FROM "{t}"'):
                    rows += 1
                    for v in row:
                        if isinstance(v, str):
                            if has_pii(v) or EMAIL_RE.search(v) or PHONE_RE.search(v):
                                hits.append(t)
        finally:
            c.close()
        self.assertGreater(rows, 10)
        self.assertEqual(hits, [])

    def test_payments_hold_pseudonym_only(self):
        self.sync()
        c = self.db()
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(payments)")]
            rows = c.execute("SELECT customer_key, description, transaction_id FROM payments").fetchall()
        finally:
            c.close()
        self.assertEqual(len(rows), 4)
        self.assertNotIn("player_email", cols)
        keys = {r[0] for r in rows}
        self.assertEqual(len(keys), 2)  # case/whitespace variants of one email collapse
        for _, desc, tid in rows:
            self.assertNotIn(S_FIRST.lower(), desc.lower())
            self.assertNotIn("@", tid)

    def test_description_template_rule(self):
        keep = "Booked court of Padium Canary Wharf: September 10, 2026 10:00 - 11:00 BST"
        kept = cv._sanitise_payment(pay_rec("a", "2026-09-10 10:00:00", desc=keep))
        self.assertEqual(kept["description"], keep)
        winter = keep.replace("September", "December").replace("BST", "GMT")
        self.assertEqual(cv._sanitise_payment(pay_rec("a", "2026-09-10 10:00:00", desc=winter))["description"], winter)
        odd = cv._sanitise_payment(pay_rec("a", "2026-09-10 10:00:00", desc="Court with Bob Rareperson"))
        self.assertEqual(odd["description"], "booking")

    def test_synthetic_ids_have_no_email(self):
        rec = pay_rec("x", "2026-09-10 10:00:00")
        del rec["transaction_id"]
        clean = cv._sanitise_payment(rec)
        tid = cv._payment_txn_id(clean)
        self.assertTrue(tid.startswith("synthetic:"))
        self.assertNotIn("@", tid)
        self.assertEqual(set(clean), {"createdAt", "customer_key", "source_type", "description", "amount", "status"})


class LeaderboardTests(Base):
    def test_leaderboard_anonymous_and_stable(self):
        cv._sync_payments(CLUB, ms(2026, 9, 1), ms(2026, 9, 19, 23, 59))  # routes only read the table
        url = f"/api/payment-leaderboard?{self.window()}"
        a = self.client.get(url)
        b = self.client.get(url)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.get_json(), b.get_json())
        leaders = a.get_json()["leaders"]
        self.assertEqual(len(leaders), 2)
        for leader in leaders:
            self.assertEqual(set(leader), {"customer_id", "bookings", "total", "pct"})
            self.assertRegex(leader["customer_id"], r"^[0-9A-F]{4}$")
        top = leaders[0]
        self.assertEqual((top["bookings"], top["total"]), (3, 22.5))  # t1+t2+t3 for the one customer
        self.assertEqual(top["customer_id"], cv._customer_key(S_EMAIL)[:4].upper())
        self.assertEqual(has_pii(a.get_data(as_text=True)), [])


class HistoryTests(Base):
    def test_payment_history_local_paged(self):
        recs = [pay_rec(f"p{i}", f"2026-09-10 {8 + i // 60:02d}:{i % 60:02d}:00") for i in range(130)]
        with mock.patch.object(Upstream, "__call__", lambda s, url, **kw: self._page(recs, url)):
            base = f"/api/payment-history?club_id={CLUB}&start={ms(2026, 9, 10)}&end={ms(2026, 9, 10, 23, 59)}"
            cv._sync_payments(CLUB, ms(2026, 9, 10), ms(2026, 9, 10, 23, 59))  # routes only read the table
            r1 = self.client.get(base + "&limit=100&skip=0").get_json()
            r2 = self.client.get(base + "&limit=100&skip=100").get_json()
            big = self.client.get(base + "&limit=500&skip=0").get_json()
            far = self.client.get(base + f"&limit=100&skip={cv.PAYMENTS_MAX_SKIP + 1}")
        self.assertEqual(len(r1["payments"]), 100)
        self.assertTrue(r1["has_more"])
        self.assertEqual(len(r2["payments"]), 30)
        self.assertFalse(r2["has_more"])
        self.assertEqual(len(big["payments"]), 100)
        self.assertEqual(far.status_code, 400)
        for row in r1["payments"] + r2["payments"]:
            self.assertEqual(set(row), {"id", "createdAt", "description", "amount", "status"})
        created = [p["createdAt"] for p in r1["payments"]]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertEqual(len({p["id"] for p in r1["payments"] + r2["payments"]}), 130)

    @staticmethod
    def _page(recs, url):
        q = parse_qs(urlparse(url).query)
        start, end = int(q["start_datetime"][0]), int(q["end_datetime"][0])
        skip, limit = int(q["skip"][0]), int(q["limit"][0])
        rows = [r for r in recs if start <= cv._parse_created_utc(r["createdAt"]) <= end]
        return Resp(rows[skip:skip + limit])


class AllowlistTests(Base):
    def test_club_allowlist(self):
        for path in ("payment-history", "payment-leaderboard"):
            r = self.client.get(f"/api/{path}?club_id=zzz&start=1&end=2")
            self.assertEqual(r.status_code, 400, path)
        self.assertEqual(self.client.get("/api/membership-members?club_id=zzz").status_code, 400)
        self.assertEqual(self.up.calls, [])
        for path in ("payment-history", "payment-leaderboard"):
            with mock.patch.object(cv, "_read_token", lambda: "t"):
                r = self.client.get(f"/api/{path}?club_id={RACK}&start=1&end=2&token=t")
            self.assertEqual(r.status_code, 410, path)
        self.assertEqual(self.up.calls, [])
        r = self.client.get(f"/api/archive/financial?club_id={CLUB}&captured_at=1&endpoint=payment-history")
        self.assertEqual(r.status_code, 400)

    def test_membership_counts_only(self):
        r = self.client.get(f"/api/membership-members?club_id={CLUB}")
        d = r.get_json()
        self.assertEqual(set(d), {"club_id", "counts"})
        self.assertEqual(d["counts"], {"plan1": 3})
        self.assertFalse(any("/club/membership/member" in u and "membership/member?" in u for u in self.up.calls))
        c = self.db()
        try:
            payload = c.execute("SELECT payload FROM membership_members_cache").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(set(json.loads(payload)), {"club_id", "counts"})

    def test_search_routes_gone(self):
        for path in ("/api/payment-search", "/api/archive/payment-search", "/api/coach-bios"):
            r = self.client.get(f"{path}?{self.window()}&q=ab&names=a")
            self.assertIn(r.status_code, (403, 404), path)
        self.assertEqual(self.up.calls, [])


class KeyTests(Base):
    def test_hmac_key_missing_disables_storage(self):
        with mock.patch.object(cv, "HMAC_KEY_PATH", os.path.join(_TMP, "missing")):
            res = cv._sync_payments(CLUB, ms(2026, 9, 1), ms(2026, 9, 19, 23, 59))
            r = self.client.get(f"/api/payment-leaderboard?{self.window()}")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(res["complete"])
        c = self.db()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
        finally:
            c.close()


class CoachTests(Base):
    def test_coaches_are_anonymous(self):
        d = self.client.get(f"/api/coach-stats?{self.window()}").get_json()
        rows = d["group"]["coach_statistics"]
        self.assertEqual([r["coach_name"] for r in rows], ["Coach 1", "Coach 2"])  # sessions desc
        self.assertEqual([r["total_sessions"] for r in rows], [5, 3])
        for r in rows:
            self.assertNotIn("email", r)
            self.assertNotIn("photo_url", r)
        self.assertEqual(has_pii(json.dumps(d)), [])
        a = self.client.get(f"/api/activity-summary?{self.window()}").get_json()
        self.assertEqual(has_pii(json.dumps(a)), [])


class RouteScanTests(Base):
    def seed_legacy(self):
        raw = {"payments": [pay_rec("a", "2026-06-16 10:00:00")], "has_more": False}
        c = self.db()
        try:
            c.execute("INSERT INTO archive_financial (club_id,start_time,end_time,payload,endpoint,captured_at) "
                      "VALUES (?,?,?,?,?,?)", (RACK, "s", "e", json.dumps(raw), "payment-history", 100))
            c.execute("INSERT INTO archive_financial (club_id,start_time,end_time,payload,endpoint,captured_at) "
                      "VALUES (?,?,?,?,?,?)", (RACK, "s", "e", json.dumps({"total": 1, "contact": S_EMAIL}),
                                               "revenue-summary", 100))
            c.execute("INSERT INTO archive_club_info (club_id,payload,captured_at) VALUES (?,?,?)",
                      (RACK, json.dumps({"profile": {"name": "C", "email": S_EMAIL, "phone": S_PHONE},
                                         "members": [{"name": S_NAME}]}), 100))
            avail = {"allSlots": [], "reservedBookings": [{
                "title": f"PLAYER: {S_NAME}", "open_match": {"players": {"a": {"name": S_NAME}}}}]}
            now = int(self.now / 1000)
            for i in range(28):
                c.execute("INSERT INTO availability (club_id,start_datetime,end_datetime,payload,fetched_at) "
                          "VALUES (?,?,?,?,?)", (CLUB, str(i), str(i + 1), json.dumps(avail), now))
            c.execute("INSERT INTO membership_members_cache (club_id,payload,fetched_at) VALUES (?,?,?)",
                      (CLUB, json.dumps({"club_id": CLUB, "counts": {"p": 1},
                                         "members": {"p": [{"name": S_NAME, "email": S_EMAIL}]}}), now))
            c.execute("INSERT INTO club_info_cache (club_id,payload,fetched_at) VALUES (?,?,?)",
                      (CLUB, json.dumps({"profile": {"email": S_EMAIL, "name": "C"}}), now))
            c.commit()
        finally:
            c.close()

    def urls(self):
        win = self.window()
        out = ["/", "/access", "/access.js", "/dashboard.css", "/access/data", "/x.js", "/nothing.txt"]
        for rule in cv.app.url_map.iter_rules():
            if "GET" not in rule.methods:
                continue
            r = rule.rule
            if r.startswith("/api/archive/"):
                out += [f"{r}?club_id={RACK}&captured_at=100&endpoint=payment-history&table=financial",
                        f"{r}?club_id={RACK}&captured_at=100&endpoint=revenue-summary&table=club_info"]
            elif r.startswith("/api/") and "<" not in r:
                out.append(f"{r}?{win}&club_ids={CLUB}&names=a&limit=100&skip=0&q=ab&start_datetime=1&end_datetime=2")
        for p in sorted(cv.ALLOWED_PATHS):
            out.append(f"/api{p}?club_id={CLUB}&start_datetime=0&end_datetime=1")
            out.append(f"/api{p}?club_id={CLUB}&start_datetime=5&end_datetime=6")
        out.append("/api/not/allowed?x=1")
        return sorted(set(out))

    def test_every_route_leaks_no_pii(self):
        self.seed_legacy()
        cv._sync_payments(CLUB, ms(2026, 9, 1), ms(2026, 9, 19, 23, 59))
        urls = self.urls()
        self.assertGreater(len(urls), 25)
        seen_json = 0
        for url in urls:
            r = self.client.get(url)
            body = r.get_data(as_text=True)
            self.assertEqual(has_pii(body), [], url)
            if "json" in (r.content_type or ""):
                seen_json += 1
                self.assertIsNone(EMAIL_RE.search(body), url)
                self.assertIsNone(PHONE_RE.search(body), url)
        self.assertGreater(seen_json, 15)

    def test_archive_payment_rows_sanitised(self):
        self.seed_legacy()
        d = self.client.get(f"/api/archive/financial?club_id={RACK}&captured_at=100&endpoint=payment-history").get_json()
        self.assertEqual(len(d["payments"]), 1)
        self.assertEqual(set(d["payments"][0]), {"id", "createdAt", "description", "amount", "status"})


if __name__ == "__main__":
    unittest.main()
