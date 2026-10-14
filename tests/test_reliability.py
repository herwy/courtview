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


if __name__ == "__main__":
    unittest.main()
