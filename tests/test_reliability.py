"""Reliability tests: UpstreamClient, never-cache-failures, single-flight, health, job state,
bounded availability refresh, read-only payment routes, status JSON and the fresh-availability route.

Run on the RPi (needs Flask and curl_cffi): python3 -m unittest tests.test_reliability -v
"""
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-rel-test-")
_KEY = os.path.join(_TMP, "hmac_key")
with open(_KEY, "w") as _fh:
    _fh.write("ef" * 32)
os.chmod(_KEY, 0o600)
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["COURTVIEW_HMAC_KEY_PATH"] = _KEY
os.environ["COURTVIEW_STATUS_DIR"] = os.path.join(_TMP, "status")

_spec = importlib.util.spec_from_file_location("courtview_rel_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_rel_under_test"] = cv
_spec.loader.exec_module(cv)

CLUB = cv.HEATMAP_CLUBS[0]["id"]
CLUB2 = cv.HEATMAP_CLUBS[1]["id"]
RACK = cv.RACKETEER_CLUB_ID
REAL_SLEEP = time.sleep
S_EMAIL = "zed.sentinelson@example.test"
S_NAME = "Zed Sentinelson"


class Resp:
    def __init__(self, code=200, body=None, headers=None):
        self.status_code = code
        self._body = {} if body is None else body
        self.content = json.dumps(self._body).encode("utf-8")
        self.headers = {"content-type": "application/json"}
        if headers:
            self.headers.update(headers)

    def json(self):
        return self._body


def session_of(fn):
    return types.SimpleNamespace(get=fn, close=lambda: None)


class Script:
    """Session.get stand-in that plays back a list of outcomes (int, Resp or Exception)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        o = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(o, Exception):
            raise o
        if isinstance(o, int):
            return Resp(o)
        return o


def make_client(outcomes=(), **kw):
    c = cv.UpstreamClient(base="https://up.example.test", **kw)
    script = Script(outcomes)
    c._new_session = lambda: session_of(script)
    c.sleeps = []
    c._sleep = c.sleeps.append
    c._rand = lambda a, b: 1.0
    c.now = [1000.0]
    c._clock = lambda: c.now[0]
    return c, script


class ClientTests(unittest.TestCase):
    def test_429_retry_after_then_ok(self):
        c, script = make_client([Resp(429, headers={"Retry-After": "1"}), 200])
        r = c.get("/p", {"club_id": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.sleeps, [1.0])
        snap = c.snapshot()
        ep = snap["endpoints"]["/p"]
        self.assertEqual((ep["calls"], ep["ok"], ep["non_200"], ep["last_status"]), (2, 1, 1, 200))
        self.assertEqual(snap["totals"]["retries"], 1)

    def test_retry_after_too_large_returns_429(self):
        c, script = make_client([Resp(429, headers={"Retry-After": "99"})])
        self.assertEqual(c.get("/p").status_code, 429)
        self.assertEqual(c.sleeps, [])
        self.assertEqual(len(script.calls), 1)

    def test_5xx_backoff_jitter_and_attempt_cap(self):
        c, script = make_client([503, 502, 200])
        self.assertEqual(c.get("/p").status_code, 200)
        self.assertEqual(c.sleeps, [0.5, 2.0])
        self.assertEqual(len(script.calls), 3)
        c2, s2 = make_client([504, 504, 504, 200])
        self.assertEqual(c2.get("/p").status_code, 504)
        self.assertEqual(len(s2.calls), 3)  # at most 3 attempts

    def test_jitter_range(self):
        c, _ = make_client([503, 200])
        seen = []
        c._rand = lambda a, b: (seen.append((a, b)), 1.5)[1]
        c.get("/p")
        self.assertEqual(seen, [(0.5, 1.5)])
        self.assertEqual(c.sleeps, [0.75])

    def test_500_and_4xx_not_retried(self):
        for code in (500, 404, 422):
            c, script = make_client([code, 200])
            self.assertEqual(c.get("/p").status_code, code)
            self.assertEqual(len(script.calls), 1, code)

    def test_exceptions_retried_then_raise(self):
        c, script = make_client([ConnectionError("boom club_id=SECRET"), 200])
        self.assertEqual(c.get("/p").status_code, 200)
        c2, s2 = make_client([OSError("a"), OSError("b"), OSError("c"), 200])
        with self.assertRaises(cv.UpstreamError) as cm:
            c2.get("/p", {"club_id": "SECRET"})
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertEqual(len(s2.calls), 3)

    def test_breaker_open_probe_close_and_reopen(self):
        c, script = make_client([500] * 5 + [200, 500, 200])
        for _ in range(5):
            self.assertEqual(c.get("/p").status_code, 500)
        self.assertEqual(c.snapshot()["breaker"]["state"], "open")
        before = len(script.calls)
        with self.assertRaises(cv.UpstreamUnavailable):
            c.get("/p")
        self.assertEqual(len(script.calls), before)  # session untouched
        c.now[0] += 299
        with self.assertRaises(cv.UpstreamUnavailable):
            c.get("/p")
        c.now[0] += 2  # past 300 s: exactly one half-open probe
        self.assertEqual(c.get("/p").status_code, 200)
        self.assertEqual(c.snapshot()["breaker"]["state"], "closed")
        for _ in range(4):
            c.get("/p")  # 500 (4 failures) then 200 resets: stays closed
        self.assertEqual(c.snapshot()["breaker"]["state"], "closed")

    def test_breaker_probe_failure_reopens(self):
        c, script = make_client([500] * 6 + [200])
        for _ in range(5):
            c.get("/p")
        c.now[0] += 301
        self.assertEqual(c.get("/p").status_code, 500)  # failed probe
        self.assertEqual(c.snapshot()["breaker"]["state"], "open")
        with self.assertRaises(cv.UpstreamUnavailable):
            c.get("/p")

    def test_4xx_counts_as_reachable(self):
        c, _ = make_client([500] * 4 + [404] + [500] * 4)
        for _ in range(9):
            c.get("/p")
        self.assertEqual(c.snapshot()["breaker"]["state"], "closed")
        self.assertEqual(c.snapshot()["breaker"]["consecutive_failures"], 4)

    def test_429_after_retries_counts_as_failure(self):
        c, _ = make_client([Resp(429, headers={"Retry-After": "99"})] * 5)
        for _ in range(5):
            c.get("/p")
        self.assertEqual(c.snapshot()["breaker"]["state"], "open")

    def test_session_kwargs_and_constructor(self):
        c, script = make_client([200])
        c.get("/p", [("a", "1")], timeout=7)
        url, kw = script.calls[0]
        self.assertEqual(url, "https://up.example.test/p?a=1")
        self.assertEqual(kw, {"headers": cv.APP_HEADERS, "impersonate": "chrome110", "timeout": 7})
        real = cv.UpstreamClient(base="https://up.example.test")
        with mock.patch.object(cv.cffi_requests, "Session") as sess:
            real._new_session()
        sess.assert_called_once_with(impersonate="chrome110", headers=cv.APP_HEADERS)

    def test_concurrency_cap(self):
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def fake(url, **kw):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            REAL_SLEEP(0.03)
            with lock:
                state["now"] -= 1
            return Resp(200)

        c = cv.UpstreamClient(base="https://up.example.test", max_concurrency=4)
        c._new_session = lambda: session_of(fake)
        threads = [threading.Thread(target=c.get, args=("/p",)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(c.snapshot()["totals"]["ok"], 10)
        self.assertLessEqual(state["peak"], 4)
        self.assertGreaterEqual(state["peak"], 2)

    def test_backoff_sleep_does_not_hold_a_slot(self):
        c, script = make_client([503, 200, 200], max_concurrency=1, acquire_timeout=0.5)
        seen = []

        def nested_sleep(s):
            seen.append(c.get("/other").status_code)  # needs the only slot: deadlocks if held

        c._sleep = nested_sleep
        self.assertEqual(c.get("/p").status_code, 200)
        self.assertEqual(seen, [200])

    def test_slot_timeout_is_unavailable_and_not_a_breaker_failure(self):
        c, _ = make_client([], max_concurrency=1, acquire_timeout=0.05)
        c._sem.acquire()
        with self.assertRaises(cv.UpstreamUnavailable):
            c.get("/p")
        self.assertEqual(c.snapshot()["totals"]["rejected"], 1)
        self.assertEqual(c.snapshot()["breaker"]["consecutive_failures"], 0)

    def test_log_lines_carry_path_and_status_only(self):
        import contextlib
        import io
        c, _ = make_client([500, ConnectionError("failed https://x/p?club_id=SECRETCLUB")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            c.get("/p", {"club_id": "SECRETCLUB"})
            try:
                c.get("/p", {"club_id": "SECRETCLUB"})
            except cv.UpstreamError:
                pass
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("[upstream]")]
        self.assertTrue(any("non-200 status=500 path=/p" in ln for ln in lines), lines)
        self.assertTrue(any("error ConnectionError path=/p" in ln for ln in lines), lines)
        for ln in lines:
            self.assertNotIn("?", ln)
            self.assertNotIn("SECRETCLUB", ln)


# ---------------------------------------------------------------------------
# Route-level tests (Flask test client, stubbed session)
# ---------------------------------------------------------------------------

def has_pii(text):
    return [s for s in (S_EMAIL, S_NAME, "Sentinelson") if s.lower() in text.lower()]


class RouteBase(unittest.TestCase):
    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cv.DB_PATH + ext)
            except FileNotFoundError:
                pass
        cv.init_db()
        cv._load_dashboard()
        cv.PAYMENTS_PAGE_DELAY = (0, 0)
        self.calls = []
        self.handler = lambda path, q: Resp(200, {})
        cv.UPSTREAM.reset()
        self.addCleanup(cv.UPSTREAM.reset)
        saved = cv.UPSTREAM._sleep
        cv.UPSTREAM._sleep = lambda s: None
        self.addCleanup(setattr, cv.UPSTREAM, "_sleep", saved)
        for p in (mock.patch.object(cv, "_gate", lambda: (True, "")),
                  mock.patch.object(cv, "_resolve_ip", lambda ip: None),
                  mock.patch.object(cv.time, "sleep", lambda s: None),
                  mock.patch.object(cv.UPSTREAM, "_new_session", lambda: session_of(self._fake))):
            p.start()
            self.addCleanup(p.stop)
        self.client = cv.app.test_client()

    def _fake(self, url, **kw):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.path, q))
        return self.handler(u.path, q)

    def paths(self, path):
        return [c for c in self.calls if c[0] == path]

    def db(self):
        return sqlite3.connect(cv.DB_PATH)

    def count(self, table):
        c = self.db()
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()


class NeverCacheTests(RouteBase):
    def window(self):
        end = int(time.time() * 1000)
        return f"club_id={CLUB}&start={end - 86400000 * 5}&end={end}"

    def test_activity_summary_mix_failure_is_502_and_uncached(self):
        self.handler = lambda path, q: Resp(500)
        r = self.client.get(f"/api/activity-summary?{self.window()}")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.count("stats_cache"), 0)

    def test_activity_summary_training_failure_not_cached(self):
        def h(path, q):
            if q.get("activity_type") == "Training":
                return Resp(500)
            return Resp(200, {"activities": [{"activity_type": "Match"}]})
        self.handler = h
        r = self.client.get(f"/api/activity-summary?{self.window()}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["counts"], {"Club Match": 1})
        self.assertEqual(self.count("stats_cache"), 0)

    def test_activity_summary_success_is_cached(self):
        self.handler = lambda path, q: Resp(200, {"activities": [{"activity_type": "Match"}]})
        self.assertEqual(self.client.get(f"/api/activity-summary?{self.window()}").status_code, 200)
        self.assertEqual(self.count("stats_cache"), 1)

    def test_club_info_partial_not_cached_and_stale_served(self):
        def h(path, q):
            if path == "/club/creditpackage/":
                return Resp(500)
            return Resp(200, {"name": "x"})
        self.handler = h
        r = self.client.get(f"/api/club-info?club_id={CLUB}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.count("club_info_cache"), 0)
        c = self.db()
        c.execute("INSERT INTO club_info_cache VALUES (?,?,?)", (CLUB, json.dumps({"profile": {"name": "old"}}), 5))
        c.commit()
        c.close()
        r = self.client.get(f"/api/club-info?club_id={CLUB}&refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-CourtView-Stale"), "1")
        self.assertEqual(r.get_json()["profile"]["name"], "old")
        c = self.db()
        self.assertEqual(c.execute("SELECT fetched_at FROM club_info_cache").fetchone()[0], 5)
        c.close()

    def test_membership_failure_502_or_stale_never_cached(self):
        self.handler = lambda path, q: Resp(500)
        r = self.client.get(f"/api/membership-members?club_id={CLUB}")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.count("membership_members_cache"), 0)
        c = self.db()
        c.execute("INSERT INTO membership_members_cache VALUES (?,?,?)",
                  (CLUB, json.dumps({"club_id": CLUB, "counts": {"p": 4}}), 5))
        c.commit()
        c.close()
        r = self.client.get(f"/api/membership-members?club_id={CLUB}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-CourtView-Stale"), "1")
        self.assertEqual(r.get_json()["counts"], {"p": 4})
        c = self.db()
        self.assertEqual(c.execute("SELECT fetched_at FROM membership_members_cache").fetchone()[0], 5)
        c.close()

    def test_membership_plan_without_count_omitted(self):
        self.handler = lambda path, q: Resp(200, [{"_id": "a", "total_members": 2}, {"_id": "b"},
                                                  {"_id": "c", "total_members": "7"}])
        r = self.client.get(f"/api/membership-members?club_id={CLUB}")
        self.assertEqual(r.get_json()["counts"], {"a": 2})
        self.assertEqual(self.count("membership_members_cache"), 1)

    def test_availability_error_shape_not_stored(self):
        url = f"/api/player/player_booking/all_courts_slot_prices_v3?club_id={CLUB}&start_datetime=1&end_datetime=2"
        self.handler = lambda path, q: Resp(200, {"detail": "boom"})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.count("availability"), 0)
        self.handler = lambda path, q: Resp(200, {"allSlots": [], "reservedBookings": []})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.count("availability"), 1)

    def test_day_activities_mid_pagination_failure_is_502(self):
        def h(path, q):
            if q.get("skip") == "0":
                return Resp(200, {"activities": [{"activity_type": "Match"}] * 50})
            return Resp(500)
        self.handler = h
        r = self.client.get(f"/api/day-activities?club_id={CLUB}&start=1&end=2")
        self.assertEqual(r.status_code, 502)


class SingleFlightTests(RouteBase):
    def test_club_info_concurrent_misses_share_one_fetch(self):
        gate = threading.Event()

        def h(path, q):
            gate.wait(2)
            REAL_SLEEP(0.05)
            return Resp(200, {"name": "x"})
        self.handler = h
        out = []

        def hit():
            out.append(cv.app.test_client().get(f"/api/club-info?club_id={CLUB}").status_code)

        threads = [threading.Thread(target=hit) for _ in range(2)]
        for t in threads:
            t.start()
        REAL_SLEEP(0.1)
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual(out, [200, 200])
        self.assertEqual(len(self.calls), 4)

    def test_availability_concurrent_misses_share_one_fetch(self):
        def h(path, q):
            REAL_SLEEP(0.1)
            return Resp(200, {"allSlots": []})
        self.handler = h
        url = f"/api/player/player_booking/all_courts_slot_prices_v3?club_id={CLUB}&start_datetime=1&end_datetime=2"
        threads = [threading.Thread(target=lambda: cv.app.test_client().get(url)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.calls), 1)


class HealthTests(RouteBase):
    def test_health_gate_and_no_pii(self):
        self.handler = lambda path, q: Resp(200, {"allSlots": []})
        self.client.get(f"/api/club-info?club_id={S_EMAIL}")
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(set(body), {"started_at", "breaker", "totals", "endpoints", "jobs"})
        self.assertEqual(body["breaker"]["state"], "closed")
        self.assertIn("/club/", body["endpoints"])
        self.assertEqual(has_pii(r.get_data(as_text=True)), [])
        self.assertNotIn("?", r.get_data(as_text=True))
        with mock.patch.object(cv, "_gate", lambda: (False, "")):
            self.assertEqual(self.client.get("/api/health").status_code, 403)


# ---------------------------------------------------------------------------
# Availability window, purge, job_state, status JSON
# ---------------------------------------------------------------------------

def ms_utc(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


class WindowTests(RouteBase):
    def put(self, club, start, end, fetched_at=None):
        c = self.db()
        c.execute("INSERT OR REPLACE INTO availability VALUES (?,?,?,?,?)",
                  (club, str(start), str(end), json.dumps({"allSlots": []}), fetched_at or int(time.time())))
        c.commit()
        c.close()

    def test_refresh_targets_are_today_forward_only(self):
        now = time.time()
        today = datetime.fromtimestamp(now, cv._LONDON_TZ).date()
        keys = cv._london_day_keys(today)
        lo = int(keys[0][1])
        day = 86400 * 1000
        self.put(CLUB, lo - 3 * day, lo - 3 * day + 86399999)       # past
        self.put(RACK, lo + day, lo + day + 86399999)               # Racketeer
        self.put(CLUB, lo + 40 * day, lo + 40 * day + 86399999)     # beyond +28 d
        self.put("other-live", lo + 2 * day, lo + 2 * day + 86399999)  # extra in-window row: kept
        self.put(CLUB, keys[3][1], keys[3][2])                      # duplicate of a generated key
        targets = cv._availability_refresh_targets(now)
        self.assertEqual(len(targets), len(set(targets)))
        expected = {(c["id"], k[1], k[2]) for c in cv.HEATMAP_CLUBS for k in keys}
        self.assertEqual(len(expected), 56)
        self.assertEqual(set(targets), expected | {("other-live", str(lo + 2 * day), str(lo + 2 * day + 86399999))})
        self.assertFalse(any(t[0] == RACK for t in targets))
        self.assertTrue(all(int(t[1]) >= lo for t in targets))
        self.assertTrue(all(int(t[1]) < lo + 28 * day + 3600000 for t in targets))

    def test_purge_removes_past_nonnumeric_and_racketeer(self):
        now = time.time()
        today = datetime.fromtimestamp(now, cv._LONDON_TZ).date()
        keys = cv._london_day_keys(today, 3)
        lo = int(keys[0][1])
        day = 86400 * 1000
        self.put(CLUB, lo - day, lo - 1)
        self.put(CLUB, "abc", "def")
        self.put(RACK, keys[1][1], keys[1][2])
        self.put(CLUB, keys[0][1], keys[0][2])   # today stays
        self.put(CLUB, keys[2][1], keys[2][2])   # future stays
        self.assertEqual(cv._purge_availability(now), 3)
        c = self.db()
        left = c.execute("SELECT club_id, start_datetime FROM availability ORDER BY start_datetime").fetchall()
        c.close()
        self.assertEqual(left, [(CLUB, keys[0][1]), (CLUB, keys[2][1])])

    def test_cycle_fetches_only_targets_and_records_state_and_status(self):
        self.handler = lambda path, q: Resp(200, {"allSlots": [], "reservedBookings": []})
        now = time.time()
        keys = cv._london_day_keys(datetime.fromtimestamp(now, cv._LONDON_TZ).date())
        self.put(CLUB, int(keys[0][1]) - 86400000, int(keys[0][1]) - 1)  # past: purged, not fetched
        ok, attempted, refreshed = cv._availability_cycle()
        self.assertEqual((ok, attempted, refreshed), (True, 56, 56))
        self.assertEqual(len(self.calls), 56)
        self.assertEqual(self.count("availability"), 56)
        st = cv._job_states()["availability"]
        self.assertEqual((st["ok"], st["calls"], st["errors"]), (True, 56, 0))
        status = json.load(open(os.path.join(cv.STATUS_DIR, "availability.json")))
        self.assertEqual(status["job"], "availability")
        self.assertEqual(status["calls"], 56)

    def test_cycle_stops_when_breaker_opens(self):
        self.handler = lambda path, q: Resp(500)
        ok, attempted, refreshed = cv._availability_cycle()
        self.assertFalse(ok)
        self.assertEqual(refreshed, 0)
        self.assertLess(attempted, 56)
        self.assertFalse(cv._job_states()["availability"]["ok"])


class JobStateTests(RouteBase):
    T = 1_790_000_000

    def setUp(self):
        super().setUp()
        self.clock = [self.T]
        p = mock.patch.object(cv, "_now", lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def test_empty_db_is_due_now(self):
        for job in ("availability", "heatmap", "payments"):
            self.assertEqual(cv._job_due_in(job, 21600, 1800), 0.0)

    def test_persisted_run_survives_restart(self):
        cv._job_record("availability", self.T, True, 56, 0)
        self.clock[0] = self.T + 3600
        self.assertAlmostEqual(cv._job_due_in("availability", 21600, 1800), 18000, delta=1)
        # a restart re-reads job_state only: no in-memory timer exists to reset
        self.assertAlmostEqual(cv._job_due_in("availability", 21600, 1800), 18000, delta=1)

    def test_failed_run_retries_after_30_min(self):
        cv._job_record("heatmap", self.T, False, 2, 1)
        self.clock[0] = self.T + 1000
        self.assertAlmostEqual(cv._job_due_in("heatmap", 86400, 1800), 800, delta=1)
        self.clock[0] = self.T + 1801
        self.assertEqual(cv._job_due_in("heatmap", 86400, 1800), 0.0)

    def test_seeds_from_existing_data(self):
        c = self.db()
        c.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (CLUB, "1", "2", "{}", self.T - 100))
        c.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (RACK, "1", "2", "{}", self.T - 5))  # ignored
        for club, fa in ((CLUB, self.T - 7200), (CLUB2, self.T - 3600)):
            c.execute("INSERT INTO heatmap_cache VALUES (?,?,?,?,?,?)", (club, 0, 8, 0.5, 30, fa))
        c.execute("INSERT INTO payments_sync VALUES (?,?,?,?,?,?)", (CLUB, "2026-10-01", self.T - 600, 1, 1, 1))
        c.commit()
        c.close()
        self.assertAlmostEqual(cv._job_due_in("availability", 21600, 1800), 21600 - 100, delta=1)
        self.assertAlmostEqual(cv._job_due_in("heatmap", 86400, 1800), 86400 - 7200, delta=1)  # oldest club
        self.assertAlmostEqual(cv._job_due_in("payments", 1800, 1800), 1800 - 600, delta=1)
        self.assertEqual(self.count("job_state"), 3)

    def test_heatmap_with_one_club_never_fetched_is_due(self):
        c = self.db()
        c.execute("INSERT INTO heatmap_cache VALUES (?,?,?,?,?,?)", (CLUB, 0, 8, 0.5, 30, self.T - 60))
        c.commit()
        c.close()
        self.assertEqual(cv._job_due_in("heatmap", 86400, 1800), 0.0)

    def test_wait_for_job_returns_immediately_when_due_and_logs_persisted_wait(self):
        import contextlib
        import io
        cv._job_record("availability", self.T, True, 1, 0)
        sleeps = []

        def fake_sleep(n):
            sleeps.append(n)
            self.clock[0] += n

        buf = io.StringIO()
        with mock.patch.object(cv.time, "sleep", fake_sleep), contextlib.redirect_stdout(buf):
            cv._wait_for_job("availability", 700, 100)
        self.assertEqual(sleeps, [300, 300, 100])
        self.assertEqual(buf.getvalue().count("next run in 11m (persisted)"), 1)

    def test_heatmap_loop_records_failure(self):
        self.handler = lambda path, q: Resp(200, {"hottest_time_slots": [{"x": "10:00", "y": 3}]})  # arrays missing
        calls = []

        def waiter(job, period, retry):
            calls.append(job)
            if len(calls) > 1:
                raise SystemExit

        with mock.patch.object(cv, "_wait_for_job", waiter):
            with self.assertRaises(SystemExit):
                cv._heatmap_refresh_loop()
        st = cv._job_states()["heatmap"]
        self.assertEqual((st["ok"], st["calls"], st["errors"]), (False, 2, 2))
        self.assertEqual(self.count("heatmap_cache"), 0)

    def test_heatmap_validation(self):
        arrays = {"hottest_time_slots": [{"x": "10:00", "y": 3}],
                  "week_day_wise_activity_count_combined_graph": [{"x": "Mon", "y": 2}],
                  "court_wise_activity_count_combined_graph": [{"x": "Court 1", "y": 4}]}
        for bad in ({k: v for k, v in arrays.items() if k != "court_wise_activity_count_combined_graph"},
                    {k: [] for k in arrays}, {"detail": "boom"}):
            self.handler = lambda path, q, b=bad: Resp(200, b)
            self.assertFalse(cv._fetch_heatmap_for_club(CLUB, 7))
            self.assertEqual(self.count("heatmap_cache"), 0)
        self.handler = lambda path, q: Resp(200, arrays)
        self.assertTrue(cv._fetch_heatmap_for_club(CLUB, 7))
        self.assertGreater(self.count("heatmap_cache"), 0)


class StatusFileTests(unittest.TestCase):
    def test_atomic_mode_keys_and_no_leftovers(self):
        d = os.path.join(_TMP, "status-test")
        old = os.umask(0o077)
        try:
            with mock.patch.object(cv, "STATUS_DIR", d):
                cv._write_job_status("availability", 1_790_000_000, True, 56, 1)
                cv._write_job_status("availability", 1_790_000_100, False, 2, 2)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o755)
        path = os.path.join(d, "availability.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        body = json.load(open(path))
        self.assertEqual(set(body), {"job", "last_run", "ok", "calls", "errors"})
        self.assertRegex(body["last_run"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(body, {"job": "availability", "last_run": cv._iso_z(1_790_000_100), "ok": False,
                                "calls": 2, "errors": 2})
        self.assertEqual(os.listdir(d), ["availability.json"])

    def test_failure_does_not_raise(self):
        with mock.patch.object(cv, "STATUS_DIR", "/proc/definitely/not/writable"):
            cv._write_job_status("payments", 1, True, 0, 0)


# ---------------------------------------------------------------------------
# Payment routes are read-only; the background queue does the syncing
# ---------------------------------------------------------------------------

class PaymentRouteTests(RouteBase):
    NOW = ms_utc(2026, 9, 20, 12)

    def setUp(self):
        super().setUp()
        p = mock.patch.object(cv, "_now", lambda: self.NOW / 1000)
        p.start()
        self.addCleanup(p.stop)
        cv._PAY_QUEUE.clear()
        self.addCleanup(cv._PAY_QUEUE.clear)

    def window(self):
        return f"club_id={CLUB}&start={ms_utc(2026, 9, 10)}&end={ms_utc(2026, 9, 12)}"

    def test_routes_never_sync_inline_and_queue_the_window(self):
        boom = mock.Mock(side_effect=AssertionError("inline sync"))
        with mock.patch.object(cv, "_sync_payments", boom), mock.patch.object(cv, "_sync_payment_day", boom), \
                mock.patch.object(cv, "_fetch_payment_page", boom):
            for route in ("payment-leaderboard", "payment-history"):
                r = self.client.get(f"/api/{route}?{self.window()}")
                self.assertEqual(r.status_code, 200, route)
                body = r.get_json()
                self.assertFalse(body["complete"])
                self.assertIsNone(body["synced_through"])
                self.assertTrue(body["queued"])
        self.assertEqual(self.calls, [])
        self.assertEqual(list(cv._PAY_QUEUE), [(CLUB, "2026-09-10", "2026-09-12")])

    def test_complete_window_is_not_queued(self):
        c = self.db()
        for d in ("2026-09-10", "2026-09-11", "2026-09-12"):
            c.execute("INSERT INTO payments_sync VALUES (?,?,?,?,?,?)", (CLUB, d, self.NOW // 1000, 1, 1, 0))
        c.commit()
        c.close()
        body = self.client.get(f"/api/payment-leaderboard?{self.window()}").get_json()
        self.assertTrue(body["complete"])
        self.assertFalse(body["queued"])
        self.assertEqual(cv._PAY_QUEUE, {})

    def test_enqueue_rules(self):
        s, e = ms_utc(2026, 9, 10), ms_utc(2026, 9, 11)
        self.assertFalse(cv._enqueue_payment_window(RACK, s, e))
        self.assertFalse(cv._enqueue_payment_window("unknown", s, e))
        self.assertTrue(cv._enqueue_payment_window(CLUB, s, e))
        self.assertTrue(cv._enqueue_payment_window(CLUB, s, e))  # same window: still one entry
        self.assertEqual(len(cv._PAY_QUEUE), 1)
        for i in range(1, 30):
            cv._enqueue_payment_window(CLUB2, ms_utc(2026, 8, 1) + i * 86400000, ms_utc(2026, 8, 1) + i * 86400000)
        self.assertEqual(len(cv._PAY_QUEUE), cv.PAYMENTS_QUEUE_MAX)
        self.assertFalse(cv._enqueue_payment_window(CLUB2, ms_utc(2026, 7, 1), ms_utc(2026, 7, 1)))

    def test_drain_syncs_each_window_once(self):
        cv._enqueue_payment_window(CLUB, ms_utc(2026, 9, 10), ms_utc(2026, 9, 12))
        seen = []
        with mock.patch.object(cv, "_sync_payments", lambda club, s, e: seen.append((club, s, e)) or {}):
            self.assertEqual(cv._drain_payment_queue(), 1)
            self.assertEqual(cv._drain_payment_queue(), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], CLUB)
        self.assertEqual(cv._london_day(seen[0][1]), "2026-09-10")
        self.assertEqual(cv._london_day(seen[0][2]), "2026-09-12")

    def test_window_sync_status(self):
        s, e = ms_utc(2026, 9, 10, 6), ms_utc(2026, 9, 11, 21)
        self.assertEqual(cv._window_sync_status(CLUB, s, e), {"complete": False, "synced_through": None})
        c = self.db()
        close_0910 = cv._london_day_bounds("2026-09-10")[1]
        c.execute("INSERT INTO payments_sync VALUES (?,?,?,?,?,?)",
                  (CLUB, "2026-09-10", close_0910 // 1000 + 3600, 1, 1, 0))      # final: counts as its close
        c.execute("INSERT INTO payments_sync VALUES (?,?,?,?,?,?)",
                  (CLUB, "2026-09-11", ms_utc(2026, 9, 11, 10) // 1000, 1, 1, 0))  # not final
        c.commit()
        res = cv._window_sync_status(CLUB, s, e)
        self.assertTrue(res["complete"])
        self.assertEqual(res["synced_through"], cv._iso_z(ms_utc(2026, 9, 11, 10) / 1000))  # the open day
        self.assertRegex(res["synced_through"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        done = cv._window_sync_status(CLUB, s, ms_utc(2026, 9, 10, 12))  # only the final day: window end
        self.assertEqual(done["synced_through"], cv._iso_z(ms_utc(2026, 9, 10, 12) / 1000))
        c.execute("UPDATE payments_sync SET complete=0 WHERE local_day='2026-09-11'")
        c.commit()
        self.assertFalse(cv._window_sync_status(CLUB, s, e)["complete"])
        # clamped to the window end
        early_end = ms_utc(2026, 9, 10, 6, 30)
        c.execute("UPDATE payments_sync SET synced_at=? WHERE local_day='2026-09-10'", (ms_utc(2026, 9, 10, 12) // 1000,))
        c.execute("UPDATE payments_sync SET complete=0 WHERE local_day='2026-09-10'")
        c.commit()
        c.close()
        self.assertEqual(cv._window_sync_status(CLUB, s, early_end)["synced_through"], cv._iso_z(early_end / 1000))


# ---------------------------------------------------------------------------
# /api/availability-fresh
# ---------------------------------------------------------------------------

class FreshRouteTests(RouteBase):
    def setUp(self):
        super().setUp()
        now = time.time()
        today = datetime.fromtimestamp(now, cv._LONDON_TZ).date()
        _d, self.start, self.end = cv._london_day_keys(today, 1)[0]
        self.url = f"/api/availability-fresh?club_id={CLUB}&start_datetime={self.start}&end_datetime={self.end}"

    def seed(self, age):
        c = self.db()
        c.execute("INSERT OR REPLACE INTO availability VALUES (?,?,?,?,?)",
                  (CLUB, self.start, self.end, json.dumps({"allSlots": [], "marker": age}), int(time.time()) - age))
        c.commit()
        c.close()

    def test_fresh_row_served_without_upstream(self):
        self.seed(100)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["marker"], 100)
        self.assertEqual(self.calls, [])

    def test_old_row_one_call_even_when_concurrent(self):
        self.seed(1000)

        def h(path, q):
            REAL_SLEEP(0.1)
            return Resp(200, {"allSlots": [], "marker": "new"})
        self.handler = h
        out = []
        threads = [threading.Thread(target=lambda: out.append(cv.app.test_client().get(self.url).get_json()))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([o["marker"] for o in out], ["new", "new"])

    def test_rejects_bad_windows_and_clubs(self):
        now_ms = int(time.time() * 1000)
        for start, end, club in ((now_ms - 40 * 3600000, now_ms - 39 * 3600000, CLUB),
                                 (now_ms + 50 * 3600000, now_ms + 51 * 3600000, CLUB),
                                 (now_ms, now_ms + 27 * 3600000, CLUB),
                                 (now_ms, now_ms - 1, CLUB),
                                 (now_ms, now_ms + 1000, "zzz")):
            r = self.client.get(f"/api/availability-fresh?club_id={club}&start_datetime={start}&end_datetime={end}")
            self.assertEqual(r.status_code, 400, (start, end, club))
        self.assertEqual(self.client.get(
            f"/api/availability-fresh?club_id={CLUB}&start_datetime=x&end_datetime=y").status_code, 400)
        self.assertEqual(self.calls, [])

    def test_failure_serves_stale_or_502(self):
        self.handler = lambda path, q: Resp(500)
        self.assertEqual(self.client.get(self.url).status_code, 502)
        self.seed(5000)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-CourtView-Stale"), "1")
        self.assertEqual(r.get_json()["marker"], 5000)

    def test_error_shape_never_replaces_the_row(self):
        self.seed(5000)
        self.handler = lambda path, q: Resp(200, {"detail": "boom"})
        r = self.client.get(self.url)
        self.assertEqual(r.headers.get("X-CourtView-Stale"), "1")
        self.assertEqual(r.get_json()["marker"], 5000)


if __name__ == "__main__":
    unittest.main()
