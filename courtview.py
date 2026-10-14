#!/usr/bin/env python3
"""
courtview.py - Padelmates API proxy server for RPi.

Serves the CourtView dashboard HTML at / and proxies allowed Padelmates API
paths at /api/* using Android OkHttp headers. Includes token auth,
per-IP rate limiting, SQLite availability cache, and a 6-hour refresh thread.

No personal data is stored or returned: upstream JSON is stripped in memory (_scrub_pii,
_sanitise_payment) before any DB write or response, and repeat customers are identified only by
an HMAC customer_key (see "Privacy" below).

Run: gunicorn via courtview.service (systemd); python3 courtview.py is a dev fallback
Deployed to: /root/projects/courtview/courtview.py
"""

import base64
import hashlib
import hmac
import html
import json
import os
import random
import re
import sqlite3
import concurrent.futures
import threading
import tempfile
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from time import time as _now
from urllib.parse import urlencode, parse_qsl

from flask import Flask, Response, g, jsonify, make_response, request
from werkzeug.security import safe_join
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Constants - carried over verbatim from padelmates-proxy.py
# ---------------------------------------------------------------------------

TARGET      = "https://fastapi-production-fargate.padelmates.io"
APP_VERSION = "8.5.9"
BUILD       = "1031"
ANDROID_UA  = f"com.padelmates/{APP_VERSION} (Linux; Android 14) OkHttp/4.9.0"
# Headers derived from Android APK analysis (reverse-engineered via jadx)

APP_HEADERS = {
    "User-Agent":           ANDROID_UA,
    "Accept":               "application/json",
    "Accept-Language":      "en-GB,en;q=0.9",
    "Accept-Encoding":      "gzip, deflate",
    "X-Client-App-Version": APP_VERSION,
    "X-Build-Number":       BUILD,
    "X-Platform":           "android",
}

# Endpoints allowed to proxy - whitelist prevents open proxy abuse
ALLOWED_PATHS = {
    "/player/player_booking/all_courts_slot_prices_v3",
    "/player/player_booking/search_clubs",
    "/club/",
    "/club/membership/",
    "/club/creditpackage/",
    "/club/statistics/financial",
    "/club/statistics/financial/v2",
}

# Path that uses the SQLite availability cache
CACHED_PATH = "/player/player_booking/all_courts_slot_prices_v3"


# ---------------------------------------------------------------------------
# Upstream client: the only code that talks to Padelmates
# ---------------------------------------------------------------------------

class UpstreamError(Exception):
    """Network failure talking to Padelmates (after retries)."""


class UpstreamUnavailable(UpstreamError):
    """Breaker open, or no concurrency slot obtained in time. Nothing was sent."""


def _iso_z(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpstreamClient:
    """Pooled curl_cffi sessions with a concurrency cap, bounded retries (429/5xx, jittered
    backoff), a circuit breaker and per-endpoint counters. Logs path and status only, never
    the query string (it carries club ids and time windows) or an exception message."""

    BACKOFF = (0.5, 2.0)

    def __init__(self, base=None, max_concurrency=4, max_retries=2, breaker_threshold=5,
                 breaker_cooldown=300.0, retry_after_max=10.0, acquire_timeout=20.0):
        self._base = base or TARGET
        self._max_concurrency = max_concurrency
        self._max_retries = max_retries
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._retry_after_max = retry_after_max
        self._acquire_timeout = acquire_timeout
        self._sleep = time.sleep
        self._clock = time.monotonic
        self._rand = random.uniform
        self._lock = threading.Lock()
        self._pool_lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Clear sessions, counters and breaker (tests, and the constructor)."""
        with self._pool_lock:
            old = list(getattr(self, "_pool", []))
            self._pool = []
        for session in old:
            try:
                session.close()
            except Exception:
                pass
        self._sem = threading.BoundedSemaphore(self._max_concurrency)
        with self._lock:
            self._started_at = time.time()
            self._eps = {}
            self._totals = {"calls": 0, "ok": 0, "non_200": 0, "errors": 0, "retries": 0, "rejected": 0,
                            "unauthorized": 0}
            self._auth_logged = set()
            self._failures = 0
            self._opened_at = None
            self._probing = False

    def _new_session(self):
        return cffi_requests.Session(impersonate="chrome110", headers=APP_HEADERS)

    # -- pool ---------------------------------------------------------------
    def _checkout(self):
        with self._pool_lock:
            if self._pool:
                return self._pool.pop()
        return self._new_session()

    def _checkin(self, session) -> None:
        with self._pool_lock:
            if len(self._pool) < self._max_concurrency:
                self._pool.append(session)
                return
        try:
            session.close()
        except Exception:
            pass

    # -- breaker ------------------------------------------------------------
    def _admit(self) -> bool:
        """Raise UpstreamUnavailable when open. Returns True when this call is the half-open probe."""
        with self._lock:
            if self._opened_at is None:
                return False
            if self._clock() - self._opened_at < self._breaker_cooldown or self._probing:
                self._totals["rejected"] += 1
                raise UpstreamUnavailable("circuit open")
            self._probing = True
        print("[upstream] breaker half-open, probing", flush=True)
        return True

    def _breaker_result(self, failure: bool) -> None:
        msg = None
        with self._lock:
            if self._probing:
                self._probing = False
                if failure:
                    self._opened_at = self._clock()
                    msg = "[upstream] breaker re-opened after failed probe"
                else:
                    self._opened_at = None
                    self._failures = 0
                    msg = "[upstream] breaker closed"
            elif failure:
                self._failures += 1
                if self._opened_at is None and self._failures >= self._breaker_threshold:
                    self._opened_at = self._clock()
                    msg = f"[upstream] breaker opened after {self._failures} failures"
            else:
                self._failures = 0
        if msg:
            print(msg, flush=True)

    def _probe_abort(self, probe: bool) -> None:
        if probe:
            with self._lock:
                self._probing = False

    # -- one attempt --------------------------------------------------------
    def _attempt(self, url: str, key: str, timeout: float):
        """Returns (response, exception). Holds a slot only while the request is in flight."""
        if not self._sem.acquire(timeout=self._acquire_timeout):
            with self._lock:
                self._totals["rejected"] += 1
            raise UpstreamUnavailable("no upstream slot")
        session = None
        try:
            session = self._checkout()
            with self._lock:
                ep = self._eps.setdefault(key, {"calls": 0, "ok": 0, "non_200": 0, "errors": 0,
                                                "unauthorized": 0, "last_status": None, "last_at": None})
                ep["calls"] += 1
                self._totals["calls"] += 1
            try:
                resp = session.get(url, headers=APP_HEADERS, impersonate="chrome110", timeout=timeout)
            except Exception as exc:
                try:
                    session.close()
                except Exception:
                    pass
                session = None
                with self._lock:
                    ep["errors"] += 1
                    ep["last_at"] = _iso_z(time.time())
                    self._totals["errors"] += 1
                print(f"[upstream] error {type(exc).__name__} path={key}", flush=True)
                return None, exc
            status = resp.status_code
            first_auth = False
            with self._lock:
                ep["last_status"] = status
                ep["last_at"] = _iso_z(time.time())
                if status == 200:
                    ep["ok"] += 1
                    self._totals["ok"] += 1
                else:
                    ep["non_200"] += 1
                    self._totals["non_200"] += 1
                if status == 401:
                    ep["unauthorized"] += 1
                    self._totals["unauthorized"] += 1
                    if key not in self._auth_logged:
                        self._auth_logged.add(key)
                        first_auth = True
            if status == 401:
                # Padelmates now wants auth on some endpoints. We never authenticate or work
                # around it: count it, log once per endpoint per process, never retry or cache.
                if first_auth:
                    print(f"[upstream] auth required status=401 path={key}", flush=True)
            elif status != 200:
                print(f"[upstream] non-200 status={status} path={key}", flush=True)
            return resp, None
        finally:
            if session is not None:
                self._checkin(session)
            self._sem.release()

    @staticmethod
    def _retry_after(resp):
        hdrs = getattr(resp, "headers", None)
        if not hdrs:
            return None
        raw = None
        for name in ("Retry-After", "retry-after"):
            try:
                raw = hdrs[name]
                break
            except (KeyError, TypeError, LookupError):
                continue
        try:
            return max(0, int(str(raw).strip()))
        except (TypeError, ValueError):
            return None

    def get(self, path: str, params=None, *, timeout: float = 15):
        """GET base+path(+params). Returns the response (any status); raises UpstreamError
        on network failure after retries and UpstreamUnavailable when nothing was sent."""
        url = self._base + path
        if params:
            url += "?" + urlencode(params)
        probe = self._admit()
        retries = 0
        while True:
            try:
                resp, exc = self._attempt(url, path, timeout)
            except UpstreamUnavailable:
                self._probe_abort(probe)
                raise
            delay = None
            if exc is not None:
                retryable = True
            else:
                status = resp.status_code
                if status == 429:
                    ra = self._retry_after(resp)
                    if ra is None:
                        retryable = True
                    elif ra <= self._retry_after_max:
                        retryable, delay = True, float(ra)
                    else:
                        retryable = False
                elif status == 401:
                    retryable = False  # auth required: never retried, never a breaker failure
                else:
                    retryable = status in (502, 503, 504)
            if retryable and retries < self._max_retries:
                if delay is None:
                    delay = self.BACKOFF[min(retries, len(self.BACKOFF) - 1)] * self._rand(0.5, 1.5)
                retries += 1
                with self._lock:
                    self._totals["retries"] += 1
                self._sleep(delay)
                continue
            failure = exc is not None or resp.status_code >= 500 or resp.status_code == 429
            self._breaker_result(failure)
            if exc is not None:
                raise UpstreamError(type(exc).__name__)
            return resp

    def snapshot(self) -> dict:
        with self._lock:
            if self._probing:
                state = "half_open"
            elif self._opened_at is not None:
                state = "open"
            else:
                state = "closed"
            opened_until = None
            if self._opened_at is not None:
                remaining = max(0.0, self._breaker_cooldown - (self._clock() - self._opened_at))
                opened_until = _iso_z(time.time() + remaining)
            return {
                "started_at": _iso_z(self._started_at),
                "breaker": {"state": state, "consecutive_failures": self._failures,
                            "opened_until": opened_until},
                "totals": dict(self._totals),
                "endpoints": {k: dict(v) for k, v in self._eps.items()},
            }


UPSTREAM = UpstreamClient()

# Clubs and court counts for server-side heatmap fetching
# Racketeer excluded: migrated off Padelmates to a different booking app (2026-07-01)
HEATMAP_CLUBS = [
    {"id": "47d2eb0db7194a9dbd29783c3a2a82ad", "courts": 7},   # Padium Canary Wharf
    {"id": "788fa2c66535421aabc60fd27f941c42",  "courts": 12},  # Rocket Padel Ilford
]

# Clubs whose payments are synced into the local `payments` table (never Racketeer)
PAYMENT_SYNC_CLUB_IDS     = frozenset(c["id"] for c in HEATMAP_CLUBS)
PAYMENTS_PAGE_SIZE        = 100
PAYMENTS_MAX_PAGES_PER_DAY = 200            # 20,000 payments/day safety cap
PAYMENTS_SYNC_WORKERS     = 3
PAYMENTS_PAGE_DELAY       = (0.1, 0.25)     # jitter between pages of one day
PAYMENTS_FRESH_SECS       = 600
PAYMENTS_LOOP_SECS        = 1800
PAYMENTS_MAX_WINDOW_MS    = 32 * 86400 * 1000
PAYMENTS_MAX_SKIP         = PAYMENTS_MAX_PAGES_PER_DAY * PAYMENTS_PAGE_SIZE

HEATMAP_HOURS      = list(range(7, 23))   # 07:00-22:00
HEATMAP_STALE_SECS = 24 * 3600           # refresh every 24h (1 API call per club)

# Background job schedule (persisted in job_state, so a restart neither resets nor re-runs a job)
AVAIL_REFRESH_SECS = 6 * 3600
JOB_RETRY_SECS     = 1800                # a failed run is retried after 30 min
WATCH_FRESH_SECS   = 300                 # /api/availability-fresh max cache age
PAYMENTS_QUEUE_MAX = 16                  # queued on-demand payment windows
STATUS_DIR = os.environ.get("COURTVIEW_STATUS_DIR", "/root/projects/courtview/status")

# Racketeer migrated off Padelmates (2026-07-01). The club_id excludes it from background
# polling and drives the 410 guard (_block_archived_club); only /api/archive/* serves it.
RACKETEER_CLUB_ID = "5111764d9bb14be3adbdb8e133e8bd80"
ARCHIVED_ERROR    = "archived"
ARCHIVED_MESSAGE  = "Racketeer left Padelmates on 2026-07-01; only archived snapshots are available."

# Payment and membership routes serve these clubs only; the archive financial route serves Racketeer only.
PAYMENT_CLUB_IDS = PAYMENT_SYNC_CLUB_IDS
ARCHIVE_CLUB_IDS = frozenset({RACKETEER_CLUB_ID})

# Runtime paths on RPi
TOKEN_PATH      = "/root/.courtview_token"
HMAC_KEY_PATH   = os.environ.get("COURTVIEW_HMAC_KEY_PATH", "/root/.courtview_hmac_key")
DB_PATH         = os.environ.get("COURTVIEW_DB_PATH", "/root/projects/courtview/courtview_cache.db")
ACCESS_LOG_PATH = "/root/projects/courtview/access.log"

# ---------------------------------------------------------------------------
# SQLite connection helper - unified WAL/timeout settings
# ---------------------------------------------------------------------------

def _db_connect() -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, synchronous=NORMAL, and a 10s busy timeout.
    Every sqlite3.connect call site in this module must route through here so the pragmas
    are applied consistently and connection settings live in one place."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA secure_delete=ON")
    return conn

# ---------------------------------------------------------------------------
# Privacy: no personal data is stored or returned (UK GDPR). Upstream JSON is
# stripped in memory before any DB write or response.
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UK_MOBILE_RE = re.compile(r"(?<![0-9A-Za-z])(?:\+44 ?\(?0?\)?|0)7\d{3} ?\d{3} ?\d{3}(?![0-9A-Za-z])")
UK_LANDLINE_RE = re.compile(r"(?<![0-9A-Za-z])(?:\+44 ?\(?0?\)?|0)[12]\d{2,3} ?\d{3} ?\d{3,4}(?![0-9A-Za-z])")

# Keys are compared lower-cased with underscores and hyphens removed.
_PII_KEY_RE = re.compile(
    r"^(email.*|.*email|phone.*|.*phone|mobile.*|firstname|lastname|fullname|playername|"
    r"booker.*|bookedby.*|playerid|userid|uid|firebase.*|receipt.*|coachname|owner|createdby)$")
_PII_CONTAINERS = frozenset({"players", "participants", "members", "followers", "attendees",
                             "users", "coaches", "coach"})
_PLAYER_TITLE_KEYS = frozenset({"title", "activitytitle"})
_PLAYER_TITLE_RE = re.compile(r"PLAYER:", re.IGNORECASE)


def _redact_text(s: str) -> str:
    """Remove email addresses and UK phone numbers from free text."""
    s = EMAIL_RE.sub("", s)
    s = UK_MOBILE_RE.sub("", s)
    return UK_LANDLINE_RE.sub("", s)


def _scrub_pii(obj):
    """Recursively copy JSON-like data, dropping personal fields and redacting free text."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = str(k).lower().replace("_", "").replace("-", "")
            if _PII_KEY_RE.match(nk):
                continue
            if nk in _PII_CONTAINERS and isinstance(v, (list, dict)):
                continue
            if nk in _PLAYER_TITLE_KEYS and isinstance(v, str) and _PLAYER_TITLE_RE.search(v):
                continue
            out[k] = _scrub_pii(v)
        return out
    if isinstance(obj, list):
        return [_scrub_pii(x) for x in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    return obj


def _scrub_json_text(text: str) -> str:
    """Scrub a JSON document held as text. Non-JSON text is only regex-redacted."""
    try:
        return json.dumps(_scrub_pii(json.loads(text)))
    except (ValueError, TypeError):
        return _redact_text(text)


_HMAC_STATE = {"sig": None, "key": None, "warned": False}
_HMAC_LOCK = threading.Lock()


def _hmac_key():
    """HMAC secret bytes (>= 32), or None when missing/short. Never created or logged here."""
    try:
        st = os.stat(HMAC_KEY_PATH)
        sig = (HMAC_KEY_PATH, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (HMAC_KEY_PATH, None, None)
    with _HMAC_LOCK:
        if _HMAC_STATE["sig"] == sig:
            return _HMAC_STATE["key"]
        key = None
        if sig[1] is not None:
            try:
                with open(HMAC_KEY_PATH, "rb") as fh:
                    raw = fh.read().strip()
                if len(raw) >= 32:
                    key = raw
            except OSError:
                key = None
        _HMAC_STATE["sig"], _HMAC_STATE["key"] = sig, key
        if key is None and not _HMAC_STATE["warned"]:
            _HMAC_STATE["warned"] = True
            print("[payments] HMAC key missing or short; payment storage disabled", flush=True)
        return key


def _customer_key(email) -> str | None:
    """Pseudonymous repeat-customer id: HMAC-SHA256(secret, normalised email), hex."""
    key = _hmac_key()
    norm = str(email or "").strip().lower()
    if key is None or not norm:
        return None
    return hmac.new(key, norm.encode("utf-8"), hashlib.sha256).hexdigest()


def _pseudonym(namespace: str, value, n: int = 8) -> str:
    """Stable opaque id for a non-customer identifier (e.g. a coach id)."""
    msg = f"{namespace}:{value}".encode("utf-8")
    key = _hmac_key()
    digest = hmac.new(key, msg, hashlib.sha256).hexdigest() if key else hashlib.sha256(msg).hexdigest()
    return digest[:n]


# Descriptions are kept only when their template (see _norm_desc) was shared by >= 5 distinct customers in
# the 2026-10-14 scan of the stored data (60,828 rows; 31 templates cover 60,740). Anything else, e.g. a
# description carrying a third party's name, is replaced by the payment's source_type.
DESCRIPTION_TEMPLATES = frozenset({
    "booked court of padium canary wharf: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of padium canary wharf: <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "booked court of padium canary wharf: <month> #, #### ##:## - ##:## <tz>",
    "booked court of padium canary wharf: <month> #, #### ##:## - ##:## <tz> by credit pay",
    "booked court of rocket ilford: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "booked court of rocket padel ilford: <month> #, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> #, #### ##:## - ##:## <tz> by credit pay",
    "booking in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "booking in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "match in rocket padel ilford - ####-##-## ##:## - ####-##-## ##:## <tz>",
    "match in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "match in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "tournament in padium canary wharf - <month> ##, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "tournament in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "training in padium canary wharf - <month> ##, #### ##:## - ##:## <tz>",
    "training in padium canary wharf - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "training in padium canary wharf - <month> #, #### ##:## - ##:## <tz>",
    "training in padium canary wharf - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "training in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "training in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "training in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "training in rocket padel ilford - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "£## for padium canary wharf - <month> ##, #### ##:## <tz>",
    "£## for padium canary wharf - <month> #, #### ##:## <tz>",
    "£### for padium canary wharf - <month> ##, #### ##:## <tz>",
    "£### for padium canary wharf - <month> #, #### ##:## <tz>",
})


_MONTH_RE = re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b")
_TZ_RE = re.compile(r"\b(?:bst|gmt|utc)\b")


def _norm_desc(desc: str) -> str:
    """Template form of a description: digits, month names and timezone labels are abstracted."""
    t = _MONTH_RE.sub("<month>", desc.strip().lower())
    t = _TZ_RE.sub("<tz>", t)
    return re.sub(r"\d", "#", t)


def _clean_description(desc, name=None, email=None, phone=None) -> str:
    """Strip emails, phones and the customer's own name tokens from a payment description."""
    text = _redact_text(str(desc or ""))
    for tok in {str(name or "").strip(), *str(name or "").split()}:
        if len(tok) >= 3:
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", "", text, flags=re.IGNORECASE)
    for raw in (email, phone):
        if raw and len(str(raw).strip()) >= 3:
            text = text.replace(str(raw).strip(), "")
    return re.sub(r"\s+", " ", text).strip()


def _final_description(cleaned: str, source_type) -> str:
    if DESCRIPTION_TEMPLATES is None:
        return cleaned
    if cleaned and _norm_desc(cleaned) in DESCRIPTION_TEMPLATES:
        return cleaned
    return str(source_type or "Payment")


def _player_field(rec: dict, field: str):
    """Read an upstream player_* field (name, email, phone) so it can be hashed or stripped, never stored."""
    return rec.get(f"player_{field}")


def _sanitise_payment(rec):
    """Allowlist one upstream payment record. Name, email, phone, receipt URL are discarded here."""
    if not isinstance(rec, dict):
        return None
    cleaned = _clean_description(rec.get("description"), _player_field(rec, "name"),
                                 _player_field(rec, "email"), _player_field(rec, "phone"))
    out = {
        "createdAt": rec.get("createdAt"),
        "customer_key": _customer_key(_player_field(rec, "email")),
        "source_type": rec.get("source_type"),
        "description": _final_description(cleaned, rec.get("source_type")),
        "amount": rec.get("amount"),
        "status": rec.get("status"),
    }
    if rec.get("transaction_id"):
        out["transaction_id"] = str(rec["transaction_id"])
    return out


# Shared web assets served from the nightwatch dashboard directory
WEB_ASSETS_DIR  = "/root/labs/web"
DASHBOARD_HTML = os.path.join(os.path.dirname(__file__), "courtview.html")

# Cookie name and max-age (90 days)
COOKIE_NAME    = "courtview_token"
COOKIE_MAX_AGE = 7776000
CACHE_TTL      = 28 * 86400  # 28-day availability cache TTL
_LONDON_TZ     = ZoneInfo("Europe/London")

# ---------------------------------------------------------------------------
# Styled 403 page - copied pattern from dashboard.py _FORBIDDEN_HTML
# ---------------------------------------------------------------------------

_FORBIDDEN_HTML = b'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" href="data:,">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Access Required</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.card{max-width:380px;width:100%;border:1px solid rgba(167,139,250,.18);border-radius:12px;
      padding:40px 32px;text-align:center;background:rgba(167,139,250,.04)}
.icon{width:48px;height:48px;border-radius:50%;background:rgba(167,139,250,.12);
      display:flex;align-items:center;justify-content:center;margin:0 auto 24px;font-size:22px}
h1{font-size:15px;font-weight:600;color:#e6edf3;margin-bottom:10px}
p{font-size:13px;color:#8b949e;line-height:1.65;margin-bottom:14px}

</style>
</head>
<body>
<div class="card">
  <div class="icon">&#x1F512;</div>
  <h1>Access Restricted</h1>
  <p>This page requires authentication.</p>
</div>
</body>
</html>'''

# ---------------------------------------------------------------------------
# IP resolution - mirrors dashboard.py _resolve_ip / _IP_CACHE pattern
# ---------------------------------------------------------------------------

_IP_CACHE       = {}
_IP_LOCK        = threading.Lock()
_LOOKUP_PENDING = object()  # sentinel for in-progress lookups


def _is_rfc1918(ip: str) -> bool:
    return (
        ip.startswith("10.") or
        ip.startswith("192.168.") or
        ip.startswith("127.") or
        any(ip.startswith(f"172.{i}.") for i in range(16, 32))
    )


def _resolve_ip(ip: str) -> None:
    """Async GeoIP lookup via ipinfo.io — runs in a daemon thread per unique IP."""
    result = {"rdns": ip, "country": "", "city": "", "isp": "", "asn": ""}
    private = _is_rfc1918(ip) or ip == "::1"
    if private:
        result["isp"] = "doxxnet private" if ip.startswith("10.") else ("LAN" if not ip.startswith("127.") and ip != "::1" else "")
    else:
        try:
            req = urllib.request.Request(
                f"https://ipinfo.io/{ip}/json",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read())
            if not d.get("bogon"):
                org = d.get("org", "")
                isp = re.sub(r"^AS\d+\s+", "", org).strip()
                asn_m = re.match(r"^(AS\d+)", org)
                result.update({
                    "rdns":    d.get("hostname") or ip,
                    "country": d.get("country", ""),
                    "city":    d.get("city", ""),
                    "isp":     isp,
                    "asn":     asn_m.group(1) if asn_m else "",
                })
        except Exception:
            pass
    with _IP_LOCK:
        if len(_IP_CACHE) >= 5000:
            for k in list(_IP_CACHE.keys())[:500]:
                del _IP_CACHE[k]
        _IP_CACHE[ip] = result


def _enrich_access_log(entries: list, cache_snap: dict) -> list:
    """Add rdns/country/city/isp/asn to each entry from the IP cache snapshot."""
    enriched = []
    for entry in entries:
        eip  = entry["ip"]
        raw  = cache_snap.get(eip)
        info = raw if isinstance(raw, dict) else {}
        enriched.append({**entry,
            "rdns":    info.get("rdns", eip),
            "country": info.get("country", ""),
            "city":    info.get("city", ""),
            "isp":     info.get("isp", ""),
            "asn":     info.get("asn", ""),
        })
    return enriched


# ---------------------------------------------------------------------------
# Access log - JSONL file + rolling in-memory deque
# ---------------------------------------------------------------------------

_ACCESS_LOG  = deque(maxlen=500)
_ACCESS_LOCK = threading.Lock()
_ACCESS_FD   = None  # opened at startup

_ACCESS_PATHS = {"/access", "/access/data"}

ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024   # rotate access.log past this size
ACCESS_LOG_KEEP      = 5                 # access.log.1 .. access.log.5

# Web assets loaded from the nightwatch dashboard directory at startup
_ACCESS_HTML_BYTES: bytes = b""
_ACCESS_JS_BYTES:   bytes = b""
_DASHBOARD_CSS_BYTES: bytes = b""


def _load_web_assets() -> None:
    global _ACCESS_HTML_BYTES, _ACCESS_JS_BYTES, _DASHBOARD_CSS_BYTES
    for attr, filename in (
        ("_ACCESS_HTML_BYTES",    "access.html"),
        ("_ACCESS_JS_BYTES",      "access.js"),
        ("_DASHBOARD_CSS_BYTES",  "dashboard.css"),
    ):
        path = os.path.join(WEB_ASSETS_DIR, filename)
        try:
            with open(path, "rb") as fh:
                globals()[attr] = fh.read()
            print(f"[startup] loaded {path} ({len(globals()[attr])} bytes)")
        except OSError as exc:
            print(f"[startup] WARNING: could not load {path}: {exc}")
    _strip_wireslammer()


def _strip_wireslammer() -> None:
    """Patch in-memory access page for CourtView: remove Wireslammer, retitle, strip logo."""
    global _ACCESS_HTML_BYTES, _ACCESS_JS_BYTES

    # Retitle: replace whatever the first <title> says (labs' page reads "Access Log - doxxnet Labs")
    _ACCESS_HTML_BYTES, n_title = re.subn(
        rb'<title>.*?</title>', b'<title>CourtView Access Log</title>',
        _ACCESS_HTML_BYTES, count=1, flags=re.IGNORECASE | re.DOTALL)
    if not n_title:
        print("[startup] WARNING: access page title not found, not retitled")

    # Remove logo div (<!-- Logo --> comment through its closing </div>)
    _ACCESS_HTML_BYTES = re.sub(
        rb'[ \t]*<!-- Logo -->.*?</div>\s*',
        b'\n',
        _ACCESS_HTML_BYTES,
        flags=re.DOTALL,
    )

    # Remove the tab button
    _ACCESS_HTML_BYTES = re.sub(
        rb'\s*<button[^>]*id="tab-ws"[^>]*>Wireslammer</button>',
        b'',
        _ACCESS_HTML_BYTES,
    )

    # Remove panel-ws using a nesting counter to find the true matching </div>
    start_marker = b'<div class="tab-panel hidden" id="panel-ws"'
    start_pos = _ACCESS_HTML_BYTES.find(start_marker)
    if start_pos >= 0:
        depth = 0
        i = start_pos
        buf = _ACCESS_HTML_BYTES
        while i < len(buf):
            if buf[i:i+4] == b'<div':
                depth += 1
                i += 4
            elif buf[i:i+6] == b'</div>':
                depth -= 1
                if depth == 0:
                    end_pos = i + 6
                    if end_pos < len(buf) and buf[end_pos:end_pos+1] == b'\n':
                        end_pos += 1
                    _ACCESS_HTML_BYTES = buf[:start_pos] + buf[end_pos:]
                    break
                i += 6
            else:
                i += 1

    # Remove Wireslammer JS section from the section marker to end, keeping load() calls
    ws_marker = b'// \xe2\x94\x80\xe2\x94\x80 Wireslammer tab'
    pos = _ACCESS_JS_BYTES.find(ws_marker)
    if pos < 0:
        pos = _ACCESS_JS_BYTES.find(b'\nfunction loadWS()')
    if pos > 0:
        _ACCESS_JS_BYTES = (
            _ACCESS_JS_BYTES[:pos]
            + b'\nload();\nsetInterval(tick, 1000);\nsetInterval(load, 30000);\n'
        )


def _open_access_log():
    """Append-open ACCESS_LOG_PATH at 0600 (line buffered)."""
    fd = os.open(ACCESS_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a", buffering=1)


def _rotate_access_log() -> None:
    """Shift access.log -> .1 -> ... -> .KEEP (oldest dropped), all 0600, then reopen.
    Caller holds _ACCESS_LOCK. gunicorn runs one worker, so an in-process lock is enough."""
    global _ACCESS_FD
    try:
        _ACCESS_FD.close()
    except OSError:
        pass
    _ACCESS_FD = None
    try:
        for i in range(ACCESS_LOG_KEEP, 0, -1):
            src = ACCESS_LOG_PATH if i == 1 else f"{ACCESS_LOG_PATH}.{i - 1}"
            dst = f"{ACCESS_LOG_PATH}.{i}"
            if os.path.exists(src):
                os.replace(src, dst)
                os.chmod(dst, 0o600)
    except OSError as exc:
        print(f"[access] rotate error: {type(exc).__name__}", flush=True)
    try:
        _ACCESS_FD = _open_access_log()
    except OSError as exc:
        print(f"[access] reopen error: {type(exc).__name__}", flush=True)


def _log_access(ip: str, path: str, auth: str, ua: str = "") -> None:
    global _ACCESS_FD
    entry = {
        "ts":   datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ip":   ip,
        "path": path,
        "auth": auth,
        "ua":   ua[:120],
    }
    with _ACCESS_LOCK:
        _ACCESS_LOG.appendleft(entry)
        if _ACCESS_FD is not None:
            try:
                _ACCESS_FD.write(json.dumps(entry) + "\n")
                _ACCESS_FD.flush()
                if os.fstat(_ACCESS_FD.fileno()).st_size > ACCESS_LOG_MAX_BYTES:
                    _rotate_access_log()
            except OSError as exc:
                print(f"[access] log write error: {type(exc).__name__}", flush=True)
    with _IP_LOCK:
        if ip not in _IP_CACHE:
            _IP_CACHE[ip] = _LOOKUP_PENDING
            threading.Thread(target=_resolve_ip, args=(ip,), daemon=True).start()


# ---------------------------------------------------------------------------
# Auth rate limiting - per-IP brute-force protection (mirrors dashboard.py)
# ---------------------------------------------------------------------------

_AUTH_FAILS: dict = {}   # {ip: [timestamps of recent auth failures]}
_AUTH_LOCK        = threading.Lock()
AUTH_RATE_WINDOW  = 60   # seconds
AUTH_RATE_LIMIT   = 10   # failures within window before lockout


def _is_rate_limited(ip: str) -> bool:
    """Return True if this IP has exceeded AUTH_RATE_LIMIT failures in AUTH_RATE_WINDOW seconds."""
    now = _now()
    with _AUTH_LOCK:
        if len(_AUTH_FAILS) > 1000:
            _AUTH_FAILS.clear()
        recent = [t for t in _AUTH_FAILS.get(ip, []) if now - t < AUTH_RATE_WINDOW]
        if recent:
            _AUTH_FAILS[ip] = recent
        else:
            _AUTH_FAILS.pop(ip, None)  # prune stale entries
        return len(recent) >= AUTH_RATE_LIMIT


def _record_auth_fail(ip: str) -> None:
    """Record a failed auth attempt for rate limiting."""
    with _AUTH_LOCK:
        _AUTH_FAILS.setdefault(ip, []).append(_now())


# ---------------------------------------------------------------------------
# Token reader - keyed on (st_mtime_ns, st_size), keeps the last good token
# ---------------------------------------------------------------------------

_token_cache: dict = {"value": None, "key": None, "state": None}
_token_lock = threading.Lock()


def _token_log(state: str, msg: str) -> None:
    """Log a token reader state transition (never the value or its length)."""
    if _token_cache["state"] != state:
        _token_cache["state"] = state
        print(f"[token] {msg}", flush=True)


def _read_token() -> str:
    """Return the current token, re-reading the file when (mtime_ns, size) changes.

    A transient empty or unreadable file (non-atomic rotation) keeps the last good
    token and is NOT cached, so the next request retries. With no previous good
    token (startup) this returns "" and every gate denies (fail closed).
    """
    with _token_lock:
        last = _token_cache["value"]
        try:
            st = os.stat(TOKEN_PATH)
        except OSError:
            _token_log("degraded", "reload failed (missing), "
                       + ("keeping last good token" if last else "no valid token, denying all"))
            return last or ""
        key = (st.st_mtime_ns, st.st_size)
        if key == _token_cache["key"]:
            return last or ""
        try:
            with open(TOKEN_PATH) as fh:
                value = fh.read().strip()
            reason = "empty"
        except OSError:
            value = ""
            reason = "unreadable"
        if not value:
            _token_log("degraded", f"reload failed ({reason}), "
                       + ("keeping last good token" if last else "no valid token, denying all"))
            return last or ""
        _token_cache["value"] = value
        _token_cache["key"] = key
        _token_log("ok", "loaded")
        return value


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to resist timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _set_cookie(response, token: str) -> None:
    """Attach a long-lived HttpOnly cookie to response when token came via query param."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


# ---------------------------------------------------------------------------
# SQLite availability cache helpers
# ---------------------------------------------------------------------------

PAYMENTS_DDL = """CREATE TABLE IF NOT EXISTS payments (
                   club_id        TEXT NOT NULL,
                   transaction_id TEXT NOT NULL,
                   created_utc    INTEGER NOT NULL,
                   local_day      TEXT NOT NULL,
                   created_at     TEXT,
                   customer_key   TEXT,
                   source_type    TEXT,
                   description    TEXT,
                   amount         TEXT,
                   amount_gbp     REAL,
                   status         TEXT,
                   synced_at      INTEGER,
                   PRIMARY KEY (club_id, transaction_id)
               )"""
_PAYMENTS_COLUMNS = ("club_id", "transaction_id", "created_utc", "local_day", "created_at", "customer_key",
                     "source_type", "description", "amount", "amount_gbp", "status", "synced_at")


def _payments_schema_ok(conn) -> bool:
    """True when payments exists with exactly the pseudonymised column set (no legacy PII columns)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
    return cols == set(_PAYMENTS_COLUMNS)


def init_db() -> None:
    """Create the cache database and tables if they do not exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _db_connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS availability (
                   club_id        TEXT,
                   start_datetime TEXT,
                   end_datetime   TEXT,
                   payload        TEXT,
                   fetched_at     INTEGER,
                   PRIMARY KEY (club_id, start_datetime, end_datetime)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS heatmap_cache (
                   club_id    TEXT,
                   dow        INTEGER,
                   hour       INTEGER,
                   avg_occ    REAL,
                   samples    INTEGER,
                   fetched_at INTEGER,
                   PRIMARY KEY (club_id, dow, hour)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS heatmap_hour_signal (
                   club_id    TEXT,
                   hour       INTEGER,
                   norm       REAL,
                   fetched_at INTEGER,
                   PRIMARY KEY (club_id, hour)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS heatmap_dow_signal (
                   club_id    TEXT,
                   dow        INTEGER,
                   norm       REAL,
                   fetched_at INTEGER,
                   PRIMARY KEY (club_id, dow)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS court_popularity (
                   club_id    TEXT,
                   court_name TEXT,
                   count      INTEGER,
                   fetched_at INTEGER,
                   PRIMARY KEY (club_id, court_name)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS membership_members_cache (
                   club_id    TEXT PRIMARY KEY,
                   payload    TEXT,
                   fetched_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS club_info_cache (
                   club_id    TEXT PRIMARY KEY,
                   payload    TEXT,
                   fetched_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS stats_cache (
                   cache_key  TEXT PRIMARY KEY,
                   payload    TEXT,
                   fetched_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_heatmap (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   club_id     TEXT,
                   dow         INTEGER,
                   hour        INTEGER,
                   avg_occ     REAL,
                   samples     INTEGER,
                   hour_norm   REAL,
                   dow_norm    REAL,
                   captured_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_club_info (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   club_id     TEXT,
                   payload     TEXT,
                   captured_at INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_financial (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   club_id     TEXT,
                   start_time  TEXT,
                   end_time    TEXT,
                   payload     TEXT,
                   endpoint    TEXT,
                   captured_at INTEGER
               )"""
        )
        conn.execute(PAYMENTS_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_club_created ON payments (club_id, created_utc)")
        if _payments_schema_ok(conn):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_club_key ON payments (club_id, customer_key)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS payments_sync (
                   club_id   TEXT NOT NULL,
                   local_day TEXT NOT NULL,
                   synced_at INTEGER,
                   complete  INTEGER,
                   pages     INTEGER,
                   rows      INTEGER,
                   PRIMARY KEY (club_id, local_day)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS job_state (
                   job        TEXT PRIMARY KEY,
                   last_run   INTEGER,
                   ok         INTEGER,
                   calls      INTEGER,
                   errors     INTEGER,
                   updated_at INTEGER,
                   last_ok    INTEGER
               )"""
        )
        # Migration: last_ok = start time of the latest ok run. Seeded from last_run where ok=1.
        if "last_ok" not in [r[1] for r in conn.execute("PRAGMA table_info(job_state)")]:
            conn.execute("ALTER TABLE job_state ADD COLUMN last_ok INTEGER")
            conn.execute("UPDATE job_state SET last_ok=last_run WHERE ok=1 AND last_ok IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_heatmap_club   ON archive_heatmap   (club_id, captured_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_club_info_club  ON archive_club_info  (club_id, captured_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_financial_club  ON archive_financial  (club_id, captured_at)")
        conn.commit()
    finally:
        conn.close()


STATS_CACHE_TTL = 21600  # 6 hours

def _stats_key(endpoint: str, club_id: str, start: str, end: str) -> str:
    """Round end to 6h bucket so all requests within the same window share a key.
    Returns "" if end is not a valid integer; callers must treat "" as "no caching"."""
    try:
        end_int = int(end)
    except (TypeError, ValueError):
        return ""
    end_bucket = end_int // (STATS_CACHE_TTL * 1000) * (STATS_CACHE_TTL * 1000)
    return f"{endpoint}:{club_id}:{start}:{end_bucket}"

def get_stats_cached(key: str) -> str | None:
    cutoff = int(_now()) - STATS_CACHE_TTL
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT payload FROM stats_cache WHERE cache_key=? AND fetched_at>?", (key, cutoff)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

def set_stats_cached(key: str, payload: str) -> None:
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO stats_cache (cache_key, payload, fetched_at) VALUES (?,?,?)",
            (key, payload, int(_now()))
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _cache_key(req_args: dict) -> tuple:
    """Extract (club_id, start_datetime, end_datetime) from query string."""
    return (
        req_args.get("club_id", ""),
        req_args.get("start_datetime", req_args.get("start", "")),
        req_args.get("end_datetime", req_args.get("end", "")),
    )


def get_cached(club_id: str, start: str, end: str) -> str | None:
    """Return cached JSON payload if present and within TTL, else None."""
    if not club_id:
        return None
    cutoff = int(_now()) - CACHE_TTL
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT payload FROM availability WHERE club_id=? AND start_datetime=? AND end_datetime=? AND fetched_at>?",
            (club_id, start, end, cutoff),
        ).fetchone()
        if not row:
            return None
        if row[0].strip() == "[]":
            return None
        return row[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _is_availability(parsed) -> bool:
    """Real availability only: a dict with "allSlots" or a non-empty list. An error dict such as
    {"detail": ...} or an empty list is never stored or served as data."""
    return (isinstance(parsed, dict) and "allSlots" in parsed) or (isinstance(parsed, list) and bool(parsed))


def store_cached(club_id: str, start: str, end: str, payload: str) -> None:
    """Insert or replace a cache entry."""
    if not club_id:
        return
    # Don't cache empty-list v1 responses - they pre-date the v2 switch and are useless
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return
    if not _is_availability(parsed):
        return
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO availability (club_id, start_datetime, end_datetime, payload, fetched_at) VALUES (?,?,?,?,?)",
            (club_id, start, end, payload, int(_now())),
        )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[cache] store error: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persisted job schedule, status JSON, availability window helpers
# ---------------------------------------------------------------------------

def _job_seed(conn, job: str) -> int:
    """First-deploy last_run, derived from existing data so the first deploy does not re-run a job
    that just ran. 0 means "never ran" (due now)."""
    if job == "availability":
        row = conn.execute("SELECT MAX(fetched_at) FROM availability WHERE club_id != ?",
                           (RACKETEER_CLUB_ID,)).fetchone()
    elif job == "heatmap":
        oldest = []
        for club in HEATMAP_CLUBS:
            r = conn.execute("SELECT MIN(fetched_at) FROM heatmap_cache WHERE club_id=?", (club["id"],)).fetchone()
            oldest.append(r[0] if r and r[0] else 0)
        row = (min(oldest) if oldest else 0,)
    elif job == "payments":
        row = conn.execute("SELECT MAX(synced_at) FROM payments_sync").fetchone()
    else:
        row = (0,)
    return int(row[0]) if row and row[0] else 0


def _job_due_in(job: str, period_s: int, retry_s: int) -> float:
    """Seconds until the job is due (<= 0 means now). Seeds job_state from data when absent."""
    try:
        conn = _db_connect()
        try:
            row = conn.execute("SELECT last_run, ok FROM job_state WHERE job=?", (job,)).fetchone()
            if row is None:
                seed = _job_seed(conn, job)
                if not seed:
                    return 0.0
                conn.execute("INSERT OR IGNORE INTO job_state (job, last_run, ok, calls, errors, updated_at, "
                             "last_ok) VALUES (?,?,?,?,?,?,?)", (job, seed, 1, 0, 0, int(_now()), seed))
                conn.commit()
                row = (seed, 1)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0
    last_run, ok = row
    return max(0.0, (last_run or 0) + (period_s if ok else retry_s) - _now())


def _job_record(job: str, started_at: int, ok: bool, calls: int, errors: int):
    """Persist a run. Returns last_ok (start of the latest ok run, carried over on a failed run;
    None when the job never succeeded or the write failed)."""
    try:
        conn = _db_connect()
        try:
            row = conn.execute("SELECT last_ok FROM job_state WHERE job=?", (job,)).fetchone()
            last_ok = int(started_at) if ok else (row[0] if row else None)
            conn.execute("INSERT OR REPLACE INTO job_state (job, last_run, ok, calls, errors, updated_at, "
                         "last_ok) VALUES (?,?,?,?,?,?,?)",
                         (job, int(started_at), int(bool(ok)), int(calls), int(errors), int(_now()), last_ok))
            conn.commit()
            return last_ok
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[{job}] job_state write error: {type(exc).__name__}", flush=True)
        return None


def _wait_for_job(job: str, period_s: int, retry_s: int) -> None:
    """Block until the job is due, polling job_state at most every 5 min. Logs once per wait."""
    announced = False
    while True:
        due = _job_due_in(job, period_s, retry_s)
        if due <= 0:
            return
        if not announced:
            print(f"[{job}] next run in {int(due // 60)}m (persisted)", flush=True)
            announced = True
        time.sleep(min(due, 300))


def _write_job_status(job: str, last_run: int, ok: bool, calls: int, errors: int, last_ok=None) -> None:
    """Atomically write STATUS_DIR/<job>.json (0644, dir 0755) for labs_heartbeat. Six keys, no
    personal data. UMask=0077 on the unit means the modes must be set explicitly. Never raises."""
    tmp = None
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        os.chmod(STATUS_DIR, 0o755)
        fd, tmp = tempfile.mkstemp(dir=STATUS_DIR, prefix="." + job + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"job": job, "last_run": _iso_z(last_run),
                       "last_ok": _iso_z(last_ok) if last_ok else None, "ok": bool(ok),
                       "calls": int(calls), "errors": int(errors)}, fh)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), 0o644)
        os.replace(tmp, os.path.join(STATUS_DIR, job + ".json"))
        tmp = None
    except Exception as exc:
        print(f"[{job}] status write failed: {type(exc).__name__}", flush=True)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rewrite_status_files() -> None:
    """Rewrite availability.json and payments.json from job_state (skips jobs with no row), so a
    deploy publishes last_ok without waiting for the next run."""
    try:
        conn = _db_connect()
        try:
            rows = conn.execute("SELECT job, last_run, ok, calls, errors, last_ok FROM job_state "
                                "WHERE job IN ('availability', 'payments')").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    for job, last_run, ok, calls, errors, last_ok in rows:
        if last_run:
            _write_job_status(job, last_run, bool(ok), calls or 0, errors or 0, last_ok)


def _london_day_keys(today, n: int = 28) -> list:
    """[(date_str, start_ms, end_ms)] for n London days from `today` (the keys /api/month reads)."""
    keys = []
    for i in range(n):
        d = today + timedelta(days=i)
        start_ms = int(datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=_LONDON_TZ).timestamp() * 1000)
        end_ms = int(datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=_LONDON_TZ).timestamp() * 1000)
        keys.append((str(d), str(start_ms), str(end_ms)))
    return keys


def _availability_refresh_targets(now_s: float) -> list:
    """(club_id, start_ms, end_ms) to refresh: the 28 London days today..today+27 for each live
    club, plus any other existing non-Racketeer row inside that window. Never past days."""
    today = datetime.fromtimestamp(now_s, _LONDON_TZ).date()
    keys = _london_day_keys(today)
    lo = int(keys[0][1])
    hi = int(_london_day_keys(today + timedelta(days=28), 1)[0][1])
    targets: dict = {}
    for club in HEATMAP_CLUBS:
        if club["id"] == RACKETEER_CLUB_ID:
            continue
        for _d, start, end in keys:
            targets[(club["id"], start, end)] = None
    try:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT club_id, start_datetime, end_datetime FROM availability "
                "WHERE club_id != ? AND CAST(start_datetime AS INTEGER) >= ? AND CAST(start_datetime AS INTEGER) < ?",
                (RACKETEER_CLUB_ID, lo, hi)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] db read error: {exc}", flush=True)
        rows = []
    for club_id, start, end in rows:
        targets.setdefault((club_id, start, end), None)
    return list(targets)


def _purge_availability(now_s: float) -> int:
    """Delete availability rows that end before today's London midnight, rows with non-numeric
    keys and every Racketeer row. Returns the number of rows deleted."""
    today = datetime.fromtimestamp(now_s, _LONDON_TZ).date()
    midnight = int(_london_day_keys(today, 1)[0][1])
    try:
        conn = _db_connect()
        try:
            cur = conn.execute(
                "DELETE FROM availability WHERE club_id=? OR end_datetime IS NULL "
                "OR CAST(end_datetime AS INTEGER) < ?", (RACKETEER_CLUB_ID, midnight))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] purge error: {exc}", flush=True)
        return 0


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

# static_folder=None: Flask's built-in /static route would bypass the token gate.
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MiB (Caddy already caps requests at 1 MB)
app.url_map.merge_slashes = False

# Content-Security-Policy profiles. style-src keeps 'unsafe-inline' because courtview.html has
# ~40 static style attributes plus many generated inside innerHTML templates, which no hash or
# nonce can cover without a rewrite. script-src is hash-only (no 'unsafe-inline'), so injected
# markup cannot run script.
CSP_DASHBOARD_TMPL = (
    "default-src 'self'; script-src 'self' {script}; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
CSP_ACCESS = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CSP_API = "default-src 'none'; frame-ancestors 'none'"
_dashboard_csp: str = CSP_DASHBOARD_TMPL.format(script="")

# Load dashboard HTML into memory at startup
_dashboard_bytes: bytes = b""


def _build_dashboard_csp(html_bytes: bytes) -> str:
    """CSP whose script-src allows exactly the inline <script> body in the served bytes (sha256).
    If it cannot be found, script-src stays 'self' only: the page breaks visibly, never weakens."""
    found = re.findall(rb"<script>(.*?)</script>", html_bytes, flags=re.DOTALL)
    if len(found) != 1:
        print(f"[startup] WARNING: expected 1 inline script in dashboard, found {len(found)}; "
              "script-src falls back to 'self'")
        return CSP_DASHBOARD_TMPL.format(script="")
    digest = base64.b64encode(hashlib.sha256(found[0]).digest()).decode("ascii")
    return CSP_DASHBOARD_TMPL.format(script=f"'sha256-{digest}'")


def _load_dashboard() -> None:
    global _dashboard_bytes, _dashboard_csp
    try:
        with open(DASHBOARD_HTML, "rb") as fh:
            _dashboard_bytes = fh.read()
        print(f"[startup] loaded {DASHBOARD_HTML} ({len(_dashboard_bytes)} bytes)")
        _dashboard_csp = _build_dashboard_csp(_dashboard_bytes)
    except OSError as exc:
        print(f"[startup] WARNING: could not load dashboard HTML: {exc}")


def _get_client_ip() -> str:
    """Return real client IP forwarded by Caddy via X-Real-IP, with remote_addr fallback."""
    return request.headers.get("X-Real-IP") or request.remote_addr or "0.0.0.0"


def _forbidden() -> Response:
    """Return 403 with styled forbidden page."""
    return Response(_FORBIDDEN_HTML, status=403, content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})


def _provided_token() -> tuple[str, str]:
    """Return (query_token, cookie_token). ?token= counts on GET/HEAD only."""
    query_token = request.args.get("token", "") if request.method in ("GET", "HEAD") else ""
    return query_token, request.cookies.get(COOKIE_NAME, "")


def _gate() -> tuple[bool, str]:
    """Check rate limit and token. Return (passed, token_value)."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        g.cv_auth = "RATE_LIMITED"
        return False, ""
    query_token, cookie_token = _provided_token()
    provided = query_token or cookie_token
    expected = _read_token()
    if not expected or not provided:
        g.cv_auth = "BLOCKED"
        _record_auth_fail(ip)
        return False, ""
    if not _constant_time_eq(provided, expected):
        g.cv_auth = "BLOCKED"
        _record_auth_fail(ip)
        print(f"[auth] fail from {html.escape(ip)}")
        return False, ""
    g.cv_auth = "TOKEN" if query_token else "OK"
    return True, query_token  # return query_token so caller knows if cookie should be set


@app.before_request
def _allow_only_get_head():
    """Defence in depth: the dashboard never issues methods other than GET/HEAD."""
    if request.method not in ("GET", "HEAD"):
        return _forbidden()
    return None


@app.before_request
def _block_archived_club():
    """Return 410 for any live /api route targeting the archived Racketeer club.

    Runs only for authenticated callers (no failure recording); unauthenticated
    requests fall through so the route's _gate still returns 403.
    """
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/archive/"):
        return None
    ids = {request.args.get("club_id", "").strip()}
    ids.update(c.strip() for c in request.args.get("club_ids", "").split(","))
    if RACKETEER_CLUB_ID not in ids:
        return None
    query_token, cookie_token = _provided_token()
    provided = query_token or cookie_token
    expected = _read_token()
    if not provided or not expected or not _constant_time_eq(provided, expected):
        return None
    g.cv_auth = "OK"
    print(f"[archived] 410 {request.method} {path}", flush=True)
    return jsonify({"error": ARCHIVED_ERROR, "message": ARCHIVED_MESSAGE}), 410


@app.route("/", methods=["GET"])
def index():
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    if not _dashboard_bytes:
        return Response(b"Dashboard not loaded.", status=503, content_type="text/plain")
    if via_query:
        resp = Response(status=302)
        resp.headers["Location"] = "/"
        _set_cookie(resp, via_query)
        return resp
    return Response(_dashboard_bytes, status=200, content_type="text/html; charset=utf-8")


@app.route("/<path:filename>", methods=["GET"])
def static_asset(filename):
    """Serve static files (.css, .js) from the same directory as this script. Auth-gated first
    (no 404 oracle for unauthenticated callers), then traversal-hardened."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    raw = (request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI") or "").lower()
    if (".." in filename or "\\" in filename or "%" in filename or "\x00" in filename
            or filename.startswith("/") or any(t in raw for t in ("%2e", "%2f", "%5c"))):
        return Response(b"Not found.", status=404, content_type="text/plain")

    allowed_exts = {".css", ".js", ".ico", ".png", ".svg", ".woff2"}
    _, ext = os.path.splitext(filename)
    if ext.lower() not in allowed_exts:
        return Response(b"Not found.", status=404, content_type="text/plain")

    asset_path = safe_join(os.path.dirname(os.path.abspath(__file__)), filename)
    if asset_path is None:
        return Response(b"Not found.", status=404, content_type="text/plain")
    try:
        with open(asset_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return Response(b"Not found.", status=404, content_type="text/plain")

    content_types = {
        ".css": "text/css", ".js": "application/javascript",
        ".ico": "image/x-icon", ".png": "image/png",
        ".svg": "image/svg+xml", ".woff2": "font/woff2",
    }
    resp = Response(data, status=200, content_type=content_types.get(ext.lower(), "application/octet-stream"))
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/heatmap", methods=["GET"])
def api_heatmap():
    """Return pre-built DOW occupancy matrix from SQLite. Served from server-side cache."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT dow, hour, avg_occ, samples, fetched_at FROM heatmap_cache WHERE club_id=?",
                (club_id,),
            ).fetchall()
            hour_rows = conn.execute(
                "SELECT hour, norm FROM heatmap_hour_signal WHERE club_id=?", (club_id,)
            ).fetchall()
            dow_rows = conn.execute(
                "SELECT dow, norm FROM heatmap_dow_signal WHERE club_id=?", (club_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)

    if not rows:
        return jsonify({"club_id": club_id, "fetched_at": None, "buckets": {}, "hour_signal": {}, "dow_signal": {}}), 200

    buckets: dict = {}
    fetched_at = 0
    for dow, hour, avg_occ, samples, fa in rows:
        buckets.setdefault(str(dow), {})[str(hour)] = round(avg_occ, 4)
        if fa > fetched_at:
            fetched_at = fa

    hour_signal = {str(h): round(n, 4) for h, n in hour_rows}
    dow_signal  = {str(d): round(n, 4) for d, n in dow_rows}

    resp = Response(
        json.dumps({"club_id": club_id, "fetched_at": fetched_at,
                    "buckets": buckets, "hour_signal": hour_signal, "dow_signal": dow_signal}),
        status=200, content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/court-popularity", methods=["GET"])
def api_court_popularity():
    """Per-court booking counts for a club, sorted desc with pct of max."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT court_name, count, fetched_at FROM court_popularity WHERE club_id=? ORDER BY count DESC",
            (club_id,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)

    if not rows:
        return jsonify({"club_id": club_id, "fetched_at": None, "courts": []}), 200

    max_count = max((r[1] for r in rows), default=0) or 1
    fetched_at = max((r[2] for r in rows), default=0)
    courts = [
        {"name": name, "count": int(count), "pct": round(count / max_count * 100, 1)}
        for name, count, _ in rows
    ]
    resp = Response(
        json.dumps({"club_id": club_id, "fetched_at": fetched_at, "courts": courts}),
        status=200, content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/month", methods=["GET"])
def api_month():
    """Return cached availability for 28 days in one response — replaces 28 serial fetchDay calls."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400

    cutoff = int(_now()) - CACHE_TTL
    today  = datetime.now(_LONDON_TZ).date()

    date_params = _london_day_keys(today)

    days: dict = {dp[0]: None for dp in date_params}
    conn = _db_connect()
    try:
        # Single batched query: fetch all 28 days in one round-trip
        placeholders = ",".join(["(?,?)"] * len(date_params))
        flat_pairs   = [v for dp in date_params for v in (dp[1], dp[2])]
        rows = conn.execute(
            f"SELECT start_datetime, end_datetime, payload FROM availability"
            f" WHERE club_id=? AND fetched_at>?"
            f" AND (start_datetime, end_datetime) IN ({placeholders})",
            [club_id, cutoff] + flat_pairs,
        ).fetchall()
        # Build reverse lookup: (start_ms, end_ms) -> date_str
        pair_to_date = {(dp[1], dp[2]): dp[0] for dp in date_params}
        for start_dt, end_dt, payload_str in rows:
            date_str = pair_to_date.get((start_dt, end_dt))
            if date_str:
                days[date_str] = _scrub_pii(json.loads(payload_str))
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    finally:
        conn.close()

    resp = Response(
        json.dumps({"club_id": club_id, "days": days}),
        status=200, content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _availability_row(club_id: str, start: str, end: str, max_age_s=None):
    """Stored availability payload, optionally only when fetched within max_age_s. None if absent."""
    sql = "SELECT payload FROM availability WHERE club_id=? AND start_datetime=? AND end_datetime=?"
    args = [club_id, start, end]
    if max_age_s is not None:
        sql += " AND fetched_at>=?"
        args.append(int(_now()) - max_age_s)
    try:
        conn = _db_connect()
        try:
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


@app.route("/api/availability-fresh", methods=["GET"])
def api_availability_fresh():
    """Availability for one day, at most WATCH_FRESH_SECS old (watch notifications; the normal
    availability cache can be 6 h old). Live clubs and a window near now only, single-flight."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "").strip()
    if club_id not in PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    start = request.args.get("start_datetime", "")
    end = request.args.get("end_datetime", "")
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return jsonify({"error": "start_datetime and end_datetime must be integer ms"}), 400
    now_ms = int(_now() * 1000)
    if not (now_ms - 36 * 3600000 <= start_i <= now_ms + 48 * 3600000) \
            or end_i < start_i or end_i - start_i > 26 * 3600000:
        return jsonify({"error": "window outside allowed range"}), 400

    def _respond(body: str, stale: bool = False):
        resp = Response(_scrub_json_text(body), status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            _set_cookie(resp, via_query)
        return resp

    fresh = _availability_row(club_id, start, end, WATCH_FRESH_SECS)
    if fresh is not None:
        return _respond(fresh)
    with _single_flight_lock(f"availfresh:{club_id}:{start}:{end}"):
        fresh = _availability_row(club_id, start, end, WATCH_FRESH_SECS)
        if fresh is not None:
            return _respond(fresh)
        try:
            r = UPSTREAM.get(CACHED_PATH, [("club_id", club_id), ("start_datetime", start),
                                           ("end_datetime", end)], timeout=15)
            if r.status_code == 200:
                body = _scrub_json_text(r.content.decode("utf-8", errors="replace"))
                try:
                    good = _is_availability(json.loads(body))
                except ValueError:
                    good = False
                if good:
                    store_cached(club_id, start, end, body)
                    return _respond(body)
        except Exception:
            pass
        stale = _availability_row(club_id, start, end)
        if stale is not None:
            return _respond(stale, stale=True)
        return jsonify({"error": "upstream error"}), 502


def _counts_payload(club_id: str, raw: str) -> str:
    """Reduce any stored membership payload to {club_id, counts}; member lists are never returned."""
    try:
        counts = json.loads(raw).get("counts", {})
    except (ValueError, AttributeError, TypeError):
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    return json.dumps({"club_id": club_id, "counts": counts})


@app.route("/api/membership-members", methods=["GET"])
def api_membership_members():
    """Return per-plan member counts: { club_id, counts: { plan_id: int } }.

    Counts come from the plan listing's total_members field. The per-plan member lists
    (/club/membership/member) are never fetched, stored or returned."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if club_id not in PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400

    force_refresh = request.args.get("refresh") == "1"

    # Serve from cache if fresh (6h TTL)
    if not force_refresh:
        try:
            conn = _db_connect()
            row = conn.execute(
                "SELECT payload FROM membership_members_cache WHERE club_id=? AND fetched_at>?",
                (club_id, int(_now()) - 6 * 3600),
            ).fetchone()
            conn.close()
            if row:
                resp = Response(_counts_payload(club_id, row[0]), status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    def _respond(body: str, stale: bool = False):
        resp = Response(body, status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            _set_cookie(resp, via_query)
        return resp

    with _single_flight_lock("members:" + club_id):
        if not force_refresh:
            try:
                conn = _db_connect()
                row = conn.execute(
                    "SELECT payload FROM membership_members_cache WHERE club_id=? AND fetched_at>?",
                    (club_id, int(_now()) - 6 * 3600),
                ).fetchone()
                conn.close()
                if row:
                    return _respond(_counts_payload(club_id, row[0]))
            except sqlite3.Error:
                pass

        counts: dict = {}
        ok = False
        try:
            plans_r = UPSTREAM.get("/club/membership/", {"club_id": club_id}, timeout=15)
            if plans_r.status_code == 200:
                plans_raw = plans_r.json()
                # Upstream returns either a list directly or {"memberships": [...]}
                if isinstance(plans_raw, dict):
                    plans_raw = plans_raw.get("memberships") or []
                if isinstance(plans_raw, list):
                    ok = True
                    for plan in plans_raw:
                        if not isinstance(plan, dict):
                            continue
                        pid = plan.get("_id") or plan.get("id")
                        total = plan.get("total_members")
                        if pid and isinstance(total, int) and not isinstance(total, bool):
                            counts[str(pid)] = total
        except Exception:
            ok = False

        if not ok:
            # Failure is never cached: serve the last stored counts of any age, else 502.
            try:
                conn = _db_connect()
                row = conn.execute(
                    "SELECT payload FROM membership_members_cache WHERE club_id=?", (club_id,)).fetchone()
                conn.close()
                if row:
                    return _respond(_counts_payload(club_id, row[0]), stale=True)
            except sqlite3.Error:
                pass
            return jsonify({"error": "upstream error"}), 502

        payload = json.dumps({"club_id": club_id, "counts": counts})
        if counts:
            try:
                conn = _db_connect()
                conn.execute(
                    "INSERT OR REPLACE INTO membership_members_cache (club_id, payload, fetched_at) VALUES (?,?,?)",
                    (club_id, payload, int(_now())),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass
        return _respond(payload)


def _exc_error(exc, status: int):
    """Generic JSON error for an unexpected exception (F16): the exception type goes to the
    journal, never its message (it can carry URLs, paths or upstream text) and never to the client."""
    print(f"[error] {type(exc).__name__} path={request.path}", flush=True)
    msg = "upstream unavailable" if status == 502 else "internal error"
    resp = jsonify({"error": msg})
    resp.status_code = status
    return resp


def _club_info_stored(raw: str) -> str:
    """Stored club_info_cache payload, scrubbed, minus the retired legacy 'extras' key."""
    text = _scrub_json_text(raw)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, dict) and "extras" in data:
        data.pop("extras", None)
        return json.dumps(data)
    return text


@app.route("/api/club-info", methods=["GET"])
def api_club_info():
    """Concurrent fan-out for the Club Info tab: profile + memberships + credits in one call."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400

    force_refresh = request.args.get("refresh") == "1"
    if not force_refresh:
        try:
            conn = _db_connect()
            row = conn.execute(
                "SELECT payload FROM club_info_cache WHERE club_id=? AND fetched_at>?",
                (club_id, int(_now()) - 600),
            ).fetchone()
            conn.close()
            if row:
                resp = Response(_club_info_stored(row[0]), status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    def _respond(body: str, stale: bool = False):
        resp = Response(body, status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            _set_cookie(resp, via_query)
        return resp

    with _single_flight_lock("clubinfo:" + club_id):
        if not force_refresh:
            try:
                conn = _db_connect()
                row = conn.execute(
                    "SELECT payload FROM club_info_cache WHERE club_id=? AND fetched_at>?",
                    (club_id, int(_now()) - 600),
                ).fetchone()
                conn.close()
                if row:
                    return _respond(_club_info_stored(row[0]))
            except sqlite3.Error:
                pass

        results: dict = {"profile": None, "memberships": None, "credits": None}
        good: dict = {}
        params = {"club_id": club_id}
        targets = [
            ("profile",     "/club/"),
            ("memberships", "/club/membership/"),
            ("credits",     "/club/creditpackage/"),
        ]

        def _fetch(key: str, path: str) -> None:
            good[key] = False
            try:
                r = UPSTREAM.get(path, params, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    results[key] = d
                    good[key] = True
            except Exception:
                results[key] = None

        threads = [threading.Thread(target=_fetch, args=(k, p), daemon=True) for k, p in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        payload = json.dumps(_scrub_pii(results))
        if all(good.get(k) for k, _ in targets):
            try:
                conn = _db_connect()
                conn.execute(
                    "INSERT OR REPLACE INTO club_info_cache (club_id, payload, fetched_at) VALUES (?,?,?)",
                    (club_id, payload, int(_now())),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass
            return _respond(payload)

        # A sub-fetch failed: never cache the partial result. Prefer the last complete row.
        try:
            conn = _db_connect()
            row = conn.execute("SELECT payload FROM club_info_cache WHERE club_id=?", (club_id,)).fetchone()
            conn.close()
            if row:
                return _respond(_club_info_stored(row[0]), stale=True)
        except sqlite3.Error:
            pass
        return _respond(payload)


UNAVAILABLE_ERROR = "unavailable"
UNAVAILABLE_MESSAGE = "No longer available from Padelmates"


@app.route("/api/booked-hours", methods=["GET"])
def api_booked_hours():
    """Retired: Padelmates now requires auth for the booked-hours upstream and CourtView never
    authenticates. Kept (not deleted) so callers get a clear 410, not the proxy's 403."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    resp = jsonify({"error": UNAVAILABLE_ERROR, "message": UNAVAILABLE_MESSAGE})
    resp.status_code = 410
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/activity-summary", methods=["GET"])
def api_activity_summary():
    """Fetch filtered_activities for a range and return counts grouped by type (max 5 pages)."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = _stats_key("activity-summary", club_id, start, end)
    if ck:
        cached = get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: _set_cookie(resp, via_query)
            return resp
        # Stampede guard: only one thread fetches per key
        with _single_flight_lock(ck):
            cached = get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: _set_cookie(resp, via_query)
                return resp
            # Still a miss under lock - this thread does the fetch
            payload = _do_fetch_activity_summary(club_id, start, end, ck)
            if isinstance(payload, tuple):
                return payload  # error response
    else:
        payload = _do_fetch_activity_summary(club_id, start, end, "")
        if isinstance(payload, tuple):
            return payload

    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _do_fetch_activity_summary(club_id: str, start: str, end: str, ck: str):
    """Fetch and aggregate activity summary from upstream. Returns payload str or error tuple.
    Falls back to a 14-day window if the full range times out (large clubs like Rocket)."""
    PAGE_SIZE = 100
    FALLBACK_MS = 14 * 24 * 60 * 60 * 1000

    def _attempt(s: str, e: str):
        counts: dict = {}
        total = 0
        coach_rates: dict = {}

        def _fetch_mix():
            return UPSTREAM.get("/home/activity/filtered_activities",
                                {"club_id": club_id, "start": s, "end": e, "limit": PAGE_SIZE}, timeout=12)

        def _fetch_training():
            return UPSTREAM.get("/home/activity/filtered_activities",
                                {"club_id": club_id, "start": s, "end": e, "activity_type": "Training",
                                 "limit": 100}, timeout=12)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_mix = ex.submit(_fetch_mix)
            f_train = ex.submit(_fetch_training)
            r  = f_mix.result()
            rt = f_train.result()

        if r.status_code != 200:
            # A failed mix call must not become "zero bookings": raise so the 14-day fallback runs.
            raise UpstreamError(f"filtered_activities status {r.status_code}")
        if r.status_code == 200:
            for act in r.json().get("activities", []):
                atype = act.get("activity_type") or "Other"
                btype = act.get("booking_type") or ""
                if atype == "Book Court":
                    key = "Open Match" if btype == "OPEN_MATCH" else "Private Booking"
                elif atype == "Match":
                    key = "Club Match"
                else:
                    key = atype
                counts[key] = counts.get(key, 0) + 1
                total += 1

        if rt.status_code == 200:
            for act in rt.json().get("activities", []):
                cid = act.get("coach_id")
                if not cid:
                    continue
                entry = coach_rates.setdefault(cid, {"rates": []})
                ppp = act.get("payment_per_person")
                if ppp is not None:
                    try:
                        entry["rates"].append(float(ppp))
                    except (TypeError, ValueError):
                        pass

        return counts, total, coach_rates, rt.status_code == 200

    used_start = start
    try:
        counts, total, coach_rates, train_ok = _attempt(start, end)
    except Exception:
        # Full range failed or timed out - retry with last 14 days
        fallback_start = str(int(end) - FALLBACK_MS)
        try:
            counts, total, coach_rates, train_ok = _attempt(fallback_start, end)
            used_start = fallback_start
        except Exception:
            return (jsonify({"error": "upstream error"}), 502)

    coach_rate_summary = {
        _pseudonym("coach", cid): {
            "avg_rate": round(sum(d["rates"]) / len(d["rates"]), 2) if d["rates"] else None,
            "sample": len(d["rates"]),
        }
        for cid, d in coach_rates.items()
    }

    range_days = round((int(end) - int(used_start)) / (24 * 60 * 60 * 1000))
    payload = json.dumps({"total": total, "counts": counts, "coach_rates": coach_rate_summary, "range_days": range_days})
    if ck and train_ok:
        set_stats_cached(ck, payload)
    return payload


def _activity_view(act) -> dict:
    """Allowlist one activity for the court grid: timing, courts, type, spots and price only."""
    if not isinstance(act, dict):
        return {}
    courts = []
    for c in act.get("courts") or []:
        if isinstance(c, dict):
            courts.append({k: c.get(k) for k in ("id", "court_id", "name") if k in c})
    out = {"courts": courts}
    for k in ("start_datetime", "end_datetime", "activity_type", "booking_type",
              "no_of_participants", "total_capacity", "payment_per_person"):
        if k in act:
            out[k] = act[k]
    return out


@app.route("/api/day-activities", methods=["GET"])
def api_day_activities():
    """Return all court bookings for a club+day by paginating /home/activity/filtered_activities."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400

    PAGE_SIZE = 50
    MAX_PAGES = 20  # cap at 1000 activities to prevent runaway upstream
    all_activities: list = []
    skip = 0
    try:
        for page_num in range(MAX_PAGES):
            r = UPSTREAM.get("/home/activity/filtered_activities",
                             {"club_id": club_id, "start": start, "end": end, "limit": PAGE_SIZE, "skip": skip},
                             timeout=15)
            if r.status_code != 200:
                return jsonify({"error": "upstream error"}), 502
            data = r.json()
            page = data.get("activities", [])
            all_activities.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
    except Exception as exc:
        return _exc_error(exc, 502)

    resp = Response(
        json.dumps({"club_id": club_id, "activities": [_activity_view(a) for a in all_activities]}),
        status=200,
        content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/revenue-summary", methods=["GET"])
def api_revenue_summary():
    """Fetch /club/statistics/financial/v2 for a given time window."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    club_id   = request.args.get("club_id", "")
    start     = request.args.get("start", "")
    end       = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = _stats_key("revenue-summary", club_id, start, end)
    if ck:
        cached = get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: _set_cookie(resp, via_query)
            return resp
        with _single_flight_lock(ck):
            cached = get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: _set_cookie(resp, via_query)
                return resp
            try:
                r = UPSTREAM.get("/club/statistics/financial/v2",
                                 {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
                payload = _scrub_json_text(r.content.decode("utf-8", errors="replace"))
                if r.status_code == 200:
                    set_stats_cached(ck, payload)
            except Exception as exc:
                return _exc_error(exc, 502)
    else:
        try:
            r = UPSTREAM.get("/club/statistics/financial/v2",
                             {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
            payload = _scrub_json_text(r.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            return _exc_error(exc, 502)

    if r.status_code != 200:
        return jsonify({"error": "upstream error"}), 502
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _fetch_payment_page(club_id: str, start_ms, end_ms, skip: int, limit: int = PAYMENTS_PAGE_SIZE):
    """The only caller of online_payment_history. Returns sanitised records on 200, None otherwise.
    Every record is reduced by _sanitise_payment before returning, so no caller ever holds a name,
    email, phone or receipt URL. List length equals the upstream page length (paging relies on it).
    Exceptions (network, JSON) propagate to the caller."""
    r = UPSTREAM.get("/club/statistics/online_payment_history",
                     {"club_id": club_id, "start_datetime": start_ms, "end_datetime": end_ms,
                      "limit": limit, "skip": skip}, timeout=15)
    if r.status_code != 200:
        return None
    page = r.json()
    if not isinstance(page, list):
        return None
    return [_sanitise_payment(rec) or {} for rec in page]


def _payments_ready() -> bool:
    """Payments need the HMAC secret and the pseudonymised schema; otherwise nothing is stored."""
    if _hmac_key() is None:
        return False
    conn = _db_connect()
    try:
        return _payments_schema_ok(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _payments_unavailable():
    return jsonify({"error": "payments unavailable"}), 503


def _payment_public_id(club_id: str, transaction_id: str) -> str:
    return hashlib.sha256(f"{club_id}|{transaction_id}".encode("utf-8")).hexdigest()[:12]


@app.route("/api/payment-history", methods=["GET"])
def api_payment_history():
    """Page the local payments table (synced from upstream, pseudonymised).

    Rows hold date, description, amount and status only; transaction ids never leave the DB."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    win = _parse_window_args()
    if len(win) == 2:
        return win
    club_id, start_ms, end_ms = win
    if club_id not in PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    try:
        limit = min(int(request.args.get("limit", "100")), PAYMENTS_PAGE_SIZE)
        skip = int(request.args.get("skip", "0"))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and skip must be integers"}), 400
    if limit < 1 or skip < 0 or skip > PAYMENTS_MAX_SKIP:
        return jsonify({"error": "invalid limit or skip"}), 400
    if not _payments_ready():
        return _payments_unavailable()

    sync = _window_sync_status(club_id, start_ms, end_ms)
    queued = False if sync["complete"] else _enqueue_payment_window(club_id, start_ms, end_ms)
    try:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT created_at, description, amount, status, club_id, transaction_id FROM payments "
                "WHERE club_id=? AND created_utc BETWEEN ? AND ? "
                "ORDER BY created_utc DESC, transaction_id LIMIT ? OFFSET ?",
                (club_id, start_ms, end_ms, limit + 1, skip)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    has_more = len(rows) > limit
    items = [{"id": _payment_public_id(r[4], r[5]), "createdAt": r[0], "description": r[1],
              "amount": r[2], "status": r[3]} for r in rows[:limit]]
    resp = Response(
        json.dumps({"payments": items, "skip": skip, "limit": limit, "has_more": has_more,
                    "complete": sync["complete"], "synced_through": sync["synced_through"], "queued": queued}),
        status=200,
        content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _parse_gbp_amount(raw) -> float | None:
    """Parse upstream amount strings like "40.00 GBP" -> 40.0. None if unparseable."""
    try:
        return float(str(raw).split()[0])
    except (ValueError, IndexError):
        return None


def _aggregate_leaders(rows: list) -> list:
    """Top 5 customers by successful spend. rows = [(customer_key, amount, status)].
    Output carries a short anonymous id only (first 4 hex of the HMAC key, upper-case)."""
    totals: dict = {}
    for customer_key, amount_raw, status in rows:
        if status != "PAYMENT_SUCCESS" or not customer_key:
            continue
        amount = _parse_gbp_amount(amount_raw)
        if amount is None:
            continue
        entry = totals.setdefault(customer_key, {"total": 0.0, "bookings": 0})
        entry["total"] += amount
        entry["bookings"] += 1

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1]["total"], kv[0]))[:5]
    max_total = ranked[0][1]["total"] if ranked else 0
    return [
        {"customer_id": ck[:4].upper(), "bookings": e["bookings"], "total": round(e["total"], 2),
         "pct": round(e["total"] / max_total * 100, 1) if max_total else 0}
        for ck, e in ranked
    ]


# ---------------------------------------------------------------------------
# Local payments table: per-London-day sync
# ---------------------------------------------------------------------------

def _london_day(ms: int) -> str:
    """Epoch ms -> 'YYYY-MM-DD' in Europe/London."""
    return datetime.fromtimestamp(ms / 1000, tz=_LONDON_TZ).strftime("%Y-%m-%d")


def _london_day_bounds(day: str) -> tuple:
    """'YYYY-MM-DD' -> (start_ms, next_day_start_ms) in Europe/London (DST-safe)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    start = datetime(d.year, d.month, d.day, tzinfo=_LONDON_TZ)
    nxt_d = d + timedelta(days=1)
    nxt = datetime(nxt_d.year, nxt_d.month, nxt_d.day, tzinfo=_LONDON_TZ)
    return int(start.timestamp() * 1000), int(nxt.timestamp() * 1000)


def _parse_created_utc(raw) -> int | None:
    """Upstream createdAt 'YYYY-MM-DD HH:MM:SS' (UTC) -> epoch ms, None if unparseable."""
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1000)


def _payment_txn_id(rec: dict) -> str:
    """Real upstream transaction id, else a synthetic id built from non-personal fields only."""
    tid = rec.get("transaction_id")
    if tid:
        return str(tid)
    basis = "|".join(str(rec.get(k)) for k in ("createdAt", "customer_key", "amount", "status"))
    return "synthetic:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _upsert_payments(conn, club_id: str, records: list, synced_at: int) -> int:
    """Upsert sanitised payment records. Returns the number of rows written."""
    n = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        created = _parse_created_utc(rec.get("createdAt"))
        if created is None:
            continue
        conn.execute(
            """INSERT INTO payments (club_id, transaction_id, created_utc, local_day, created_at, customer_key,
                   source_type, description, amount, amount_gbp, status, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(club_id, transaction_id) DO UPDATE SET
                   created_utc=excluded.created_utc, local_day=excluded.local_day,
                   created_at=excluded.created_at, customer_key=excluded.customer_key,
                   source_type=excluded.source_type, description=excluded.description,
                   amount=excluded.amount, amount_gbp=excluded.amount_gbp, status=excluded.status,
                   synced_at=excluded.synced_at""",
            (club_id, _payment_txn_id(rec), created, _london_day(created), rec.get("createdAt"),
             rec.get("customer_key"), rec.get("source_type"), rec.get("description"), rec.get("amount"),
             _parse_gbp_amount(rec.get("amount")), rec.get("status"), synced_at),
        )
        n += 1
    return n


def _day_needs_sync(row, day_close_ms: int, now_s: float) -> bool:
    """row = (synced_at, complete) or None. Implements the freshness/finality rule."""
    if row is None:
        return True
    synced_at, complete = row
    synced_at = synced_at or 0
    if now_s - synced_at < PAYMENTS_FRESH_SECS:
        return False
    final = bool(complete) and synced_at * 1000 >= day_close_ms + PAYMENTS_FRESH_SECS * 1000
    return not final


def _sync_payment_day(club_id: str, day: str) -> tuple:
    """Sync one London day. Returns (pages, rows, complete)."""
    if not _payments_ready():
        return (0, 0, 0)
    day_start, day_next = _london_day_bounds(day)
    key = f"paysync:{club_id}:{day}"
    with _single_flight_lock(key):
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT synced_at, complete FROM payments_sync WHERE club_id=? AND local_day=?", (club_id, day)
            ).fetchone()
            if not _day_needs_sync(row, day_next, _now()):
                return (0, 0, int(row[1]) if row else 0)
            pages = rows = 0
            complete = 0
            skip = 0
            while True:
                if pages >= PAYMENTS_MAX_PAGES_PER_DAY:
                    print(f"[payments] WARNING club={club_id[:8]} day={day} hit page cap", flush=True)
                    break
                try:
                    page = _fetch_payment_page(club_id, day_start, day_next - 1, skip, PAYMENTS_PAGE_SIZE)
                except Exception as exc:
                    print(f"[payments] club={club_id[:8]} day={day} fetch error: {type(exc).__name__}", flush=True)
                    page = None
                if page is None:
                    break
                pages += 1
                rows += _upsert_payments(conn, club_id, page, int(_now()))
                conn.commit()
                if len(page) < PAYMENTS_PAGE_SIZE:
                    complete = 1
                    break
                skip += PAYMENTS_PAGE_SIZE
                time.sleep(random.uniform(*PAYMENTS_PAGE_DELAY))
            conn.execute(
                "INSERT OR REPLACE INTO payments_sync (club_id, local_day, synced_at, complete, pages, rows) "
                "VALUES (?,?,?,?,?,?)", (club_id, day, int(_now()), complete, pages, rows))
            conn.commit()
            print(f"[payments] club={club_id[:8]} day={day} pages={pages} rows={rows} complete={complete}", flush=True)
            return (pages, rows, complete)
        finally:
            conn.close()


def _london_days(start_ms: int, end_ms: int) -> list:
    """London days ('YYYY-MM-DD') covering [start_ms, end_ms], with end clamped to now."""
    end_ms = min(int(end_ms), int(_now() * 1000))
    if end_ms < start_ms:
        return []
    days = []
    d = _london_day(int(start_ms))
    last = _london_day(end_ms)
    while d <= last:
        days.append(d)
        d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return days


def _window_sync_status(club_id: str, start_ms: int, end_ms: int) -> dict:
    """Read-only sync state of a window: {"complete", "synced_through"}.

    complete: every London day in the window (clamped to now) has complete=1.
    synced_through: ISO 8601 Z of the oldest synced_at among non-final days (the window end when
    every day is final), clamped to min(end, now); None when any day has no sync row."""
    days = _london_days(start_ms, end_ms)
    if not days:
        return {"complete": True, "synced_through": None}
    conn = _db_connect()
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT local_day, synced_at, complete FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ?",
            (club_id, days[0], days[-1]))}
    finally:
        conn.close()
    if any(d not in rows for d in days):
        return {"complete": False, "synced_through": None}
    # Final days are complete for good; only a non-final day limits how fresh the window is.
    open_days = []
    for d in days:
        synced_at, complete = rows[d]
        synced_ms = (synced_at or 0) * 1000
        final = bool(complete) and synced_ms >= _london_day_bounds(d)[1] + PAYMENTS_FRESH_SECS * 1000
        if not final:
            open_days.append(synced_ms)
    through = min(min(open_days) if open_days else int(end_ms), int(end_ms), int(_now() * 1000))
    return {"complete": all(rows[d][1] == 1 for d in days),
            "synced_through": _iso_z(through / 1000)}


_PAY_QUEUE: dict = {}
_PAY_QUEUE_LOCK = threading.Lock()
_PAY_WAKE = threading.Event()


def _enqueue_payment_window(club_id: str, start_ms: int, end_ms: int) -> bool:
    """Queue a window for the background loop. True when queued (or already queued); False for
    Racketeer, unknown clubs, empty windows and when the queue is full."""
    if club_id == RACKETEER_CLUB_ID or club_id not in PAYMENT_SYNC_CLUB_IDS:
        return False
    days = _london_days(start_ms, end_ms)
    if not days:
        return False
    key = (club_id, days[0], days[-1])
    with _PAY_QUEUE_LOCK:
        if key not in _PAY_QUEUE and len(_PAY_QUEUE) >= PAYMENTS_QUEUE_MAX:
            return False
        _PAY_QUEUE[key] = True
    _PAY_WAKE.set()
    return True


def _drain_payment_queue() -> int:
    """Sync every queued window (called from the payments loop thread). Returns windows synced."""
    with _PAY_QUEUE_LOCK:
        keys = list(_PAY_QUEUE)
        _PAY_QUEUE.clear()
    for club_id, first, last in keys:
        print(f"[payments] queued window club={club_id[:8]} days={first}..{last}", flush=True)
        try:
            _sync_payments(club_id, _london_day_bounds(first)[0], _london_day_bounds(last)[1] - 1)
        except Exception as exc:
            print(f"[payments] queued sync error club={club_id[:8]}: {type(exc).__name__}", flush=True)
    return len(keys)


def _sync_payments(club_id: str, start_ms: int, end_ms: int) -> dict:
    """Make sure every London day in [start_ms, end_ms] is synced (per the freshness rule)."""
    result = {"complete": False, "days": 0, "pages": 0, "rows": 0, "errors": 0}
    if club_id == RACKETEER_CLUB_ID or club_id not in PAYMENT_SYNC_CLUB_IDS:
        return result
    now_s = _now()
    days = _london_days(start_ms, end_ms)
    if not days:
        return result
    conn = _db_connect()
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT local_day, synced_at, complete FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ?",
            (club_id, days[0], days[-1]))}
    finally:
        conn.close()
    todo = [x for x in days if _day_needs_sync(rows.get(x), _london_day_bounds(x)[1], now_s)]
    t0 = _now()
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PAYMENTS_SYNC_WORKERS) as pool:
            for pages, nrows, _c in pool.map(lambda x: _sync_payment_day(club_id, x), todo):
                result["pages"] += pages
                result["rows"] += nrows
        result["days"] = len(todo)
        print(f"[payments] sync club={club_id[:8]} days={len(todo)} pages={result['pages']} "
              f"rows={result['rows']} in {round(_now() - t0, 1)}s", flush=True)
    conn = _db_connect()
    try:
        done = conn.execute(
            "SELECT COUNT(*) FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ? AND complete=1",
            (club_id, days[0], days[-1])).fetchone()[0]
    finally:
        conn.close()
    result["complete"] = done == len(days)
    result["errors"] = len(days) - done
    return result


def _endpoint_calls(path: str) -> int:
    return UPSTREAM.snapshot()["endpoints"].get(path, {}).get("calls", 0)


def _payments_scheduled_run() -> None:
    """Sync previous + current London month for each live club; record state and status JSON."""
    started = int(_now())
    calls0 = _endpoint_calls("/club/statistics/online_payment_history")
    now_ms = int(_now() * 1000)
    today = _london_day(now_ms)
    prev = datetime.strptime(today[:7] + "-01", "%Y-%m-%d") - timedelta(days=1)
    start_ms = _london_day_bounds(prev.strftime("%Y-%m-") + "01")[0]
    errors = 0
    for club in HEATMAP_CLUBS:
        try:
            res = _sync_payments(club["id"], start_ms, now_ms)
            errors += res["errors"]
            print(f"[payments] scheduled club={club['id'][:8]} days={res['days']} "
                  f"pages={res['pages']} rows={res['rows']} complete={int(res['complete'])}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"[payments] loop error club={club['id'][:8]}: {type(exc).__name__}: {exc}", flush=True)
    calls = _endpoint_calls("/club/statistics/online_payment_history") - calls0
    last_ok = _job_record("payments", started, errors == 0, calls, errors)
    _write_job_status("payments", started, errors == 0, calls, errors, last_ok)


def _payments_sync_loop() -> None:
    """Drain on-demand windows queued by the routes, and run the scheduled prev+current-month pass
    every 30 min. The schedule is persisted in job_state (a restart does not re-run it)."""
    announced = False
    while True:
        _PAY_WAKE.clear()
        _drain_payment_queue()
        due = _job_due_in("payments", PAYMENTS_LOOP_SECS, JOB_RETRY_SECS)
        if due <= 0:
            announced = False
            _payments_scheduled_run()
            continue
        if not announced:
            print(f"[payments] next run in {int(due // 60)}m (persisted)", flush=True)
            announced = True
        _PAY_WAKE.wait(min(due, 300))


def _parse_window_args():
    """Validate club_id/start/end. Returns (club_id, start_ms, end_ms) or (Response, status)."""
    club_id = request.args.get("club_id", "").strip()
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        start_ms = int(request.args.get("start", ""))
        end_ms = int(request.args.get("end", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "start and end must be integer ms"}), 400
    if end_ms < start_ms or end_ms - start_ms > PAYMENTS_MAX_WINDOW_MS:
        return jsonify({"error": "invalid window"}), 400
    return club_id, start_ms, end_ms


@app.route("/api/payment-leaderboard", methods=["GET"])
def api_payment_leaderboard():
    """Top spenders for a club+window, aggregated from the local payments table.
    Customers are anonymous ("Customer #XXXX"); the id is a prefix of an HMAC key."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    win = _parse_window_args()
    if len(win) == 2:
        return win
    club_id, start_ms, end_ms = win
    if club_id not in PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    if not _payments_ready():
        return _payments_unavailable()
    sync = _window_sync_status(club_id, start_ms, end_ms)
    queued = False if sync["complete"] else _enqueue_payment_window(club_id, start_ms, end_ms)
    try:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT customer_key, amount, status FROM payments "
                "WHERE club_id=? AND created_utc BETWEEN ? AND ? AND status='PAYMENT_SUCCESS'",
                (club_id, start_ms, end_ms)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    resp = Response(json.dumps({"leaders": _aggregate_leaders(rows), "complete": sync["complete"],
                                "synced_through": sync["synced_through"], "queued": queued}),
                    status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


# ---------------------------------------------------------------------------
# Archive read endpoints
# ---------------------------------------------------------------------------

@app.route("/api/archive/snapshots", methods=["GET"])
def api_archive_snapshots():
    """List distinct captured_at timestamps for a given archive table and club."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    table   = request.args.get("table", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if table not in ("heatmap", "club_info", "financial"):
        return jsonify({"error": "invalid table"}), 400
    try:
        conn = _db_connect()
        try:
            if table == "financial":
                rows = conn.execute(
                    "SELECT DISTINCT captured_at FROM archive_financial"
                    " WHERE club_id=? AND endpoint='revenue-summary'"
                    " ORDER BY captured_at DESC LIMIT 50",
                    (club_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT DISTINCT captured_at FROM archive_{table} WHERE club_id=? ORDER BY captured_at DESC LIMIT 50",
                    (club_id,),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    snapshots = [
        {
            "captured_at": row[0],
            "label": datetime.utcfromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M UTC"),
        }
        for row in rows
    ]
    resp = Response(json.dumps(snapshots), status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/archive/heatmap", methods=["GET"])
def api_archive_heatmap():
    """Return archived heatmap snapshot in same shape as /api/heatmap."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    try:
        conn = _db_connect()
        try:
            rows = conn.execute(
                "SELECT dow, hour, avg_occ, samples, hour_norm, dow_norm FROM archive_heatmap"
                " WHERE club_id=? AND captured_at=?",
                (club_id, captured_at),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    if not rows:
        return jsonify({"error": "no snapshot"}), 404
    buckets: dict = {}
    hour_signal: dict = {}
    dow_signal: dict = {}
    for dow, hour, avg_occ, _samples, hour_norm, dow_norm in rows:
        buckets.setdefault(str(dow), {})[str(hour)] = round(avg_occ, 4)
        # hour_norm/dow_norm are unset on snapshots archived before this data was captured
        if hour_norm is not None:
            hour_signal[str(hour)] = round(hour_norm, 4)
        if dow_norm is not None:
            dow_signal[str(dow)] = round(dow_norm, 4)
    payload = json.dumps({
        "club_id": club_id,
        "fetched_at": captured_at,
        "buckets": buckets,
        "hour_signal": hour_signal,
        "dow_signal": dow_signal,
    })
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/archive/club-info", methods=["GET"])
def api_archive_club_info():
    """Return archived club-info snapshot payload."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    try:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT payload FROM archive_club_info WHERE club_id=? AND captured_at=? LIMIT 1",
                (club_id, captured_at),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    if not row:
        return jsonify({"error": "no snapshot"}), 404
    resp = Response(_scrub_json_text(row[0]), status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _archive_payments_text(club_id: str, text: str) -> str:
    """Archived payment-history payload -> {payments: [{id, createdAt, description, amount, status}]}."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return json.dumps({"payments": []})
    if not isinstance(doc, dict):
        return json.dumps({"payments": []})
    records = doc.get("payments")
    rows = []
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        clean = _sanitise_payment(rec) if ("player_email" in rec or "player_name" in rec) else rec
        rows.append({
            "id": _payment_public_id(club_id, _payment_txn_id(clean)),
            "createdAt": clean.get("createdAt"),
            "description": _redact_text(str(clean.get("description") or "")),
            "amount": clean.get("amount"),
            "status": clean.get("status"),
        })
    other = _scrub_pii({k: v for k, v in doc.items() if k != "payments"})
    other["payments"] = rows
    return json.dumps(other)


@app.route("/api/archive/financial", methods=["GET"])
def api_archive_financial():
    """Return archived financial snapshot payload."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if club_id not in ARCHIVE_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    endpoint = request.args.get("endpoint", "")
    if endpoint not in ("revenue-summary", "payment-history"):
        return jsonify({"error": "invalid endpoint"}), 400
    try:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT payload FROM archive_financial WHERE club_id=? AND captured_at=? AND endpoint=? LIMIT 1",
                (club_id, captured_at, endpoint),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _exc_error(exc, 500)
    if not row:
        return jsonify({"error": "no snapshot"}), 404
    body = _archive_payments_text(club_id, row[0]) if endpoint == "payment-history" else _scrub_json_text(row[0])
    resp = Response(body, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


_COACH_TYPES = ("group", "private", "course")


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _anonymise_coach_stats(data):
    """Coach names are personal data once tied to per-person revenue and occupancy.

    Coaches become "Coach 1..n" (ordered by sessions desc) with a pseudonymous coach_id; only numeric
    statistics are kept per coach. Everything else in the document goes through _scrub_pii."""
    if not isinstance(data, dict):
        return _scrub_pii(data)
    totals: dict = {}
    for t in _COACH_TYPES:
        block = data.get(t)
        if isinstance(block, dict):
            for c in block.get("coach_statistics") or []:
                if isinstance(c, dict):
                    cid = str(c.get("coach_id"))
                    totals[cid] = totals.get(cid, 0.0) + _num(c.get("total_sessions"))
    order = sorted(totals, key=lambda cid: (-totals[cid], _pseudonym("coach", cid)))
    labels = {cid: f"Coach {i + 1}" for i, cid in enumerate(order)}
    rest = {}
    for k, v in data.items():
        if k in _COACH_TYPES and isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk != "coach_statistics"}
        rest[k] = v
    out = _scrub_pii(rest)
    for t in _COACH_TYPES:
        block, orig = out.get(t), data.get(t)
        if isinstance(block, dict) and isinstance(orig, dict) and "coach_statistics" in orig:
            rows = []
            for c in orig.get("coach_statistics") or []:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("coach_id"))
                row = {k: v for k, v in c.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
                row["coach_id"] = _pseudonym("coach", cid)
                row["coach_name"] = labels[cid]
                rows.append(row)
            rows.sort(key=lambda r: r["coach_name"].split()[1].zfill(4))
            block["coach_statistics"] = rows
    return out


def _coach_stats_payload(text: str) -> str:
    try:
        return json.dumps(_anonymise_coach_stats(json.loads(text)))
    except (ValueError, TypeError):
        return _redact_text(text)


@app.route("/api/coach-stats", methods=["GET"])
def api_coach_stats():
    """Proxy /club/statistics/coach with 1h SQLite cache."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = _stats_key("coach-stats", club_id, start, end)
    if ck:
        cached = get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: _set_cookie(resp, via_query)
            return resp
        with _single_flight_lock(ck):
            cached = get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: _set_cookie(resp, via_query)
                return resp
            try:
                r = UPSTREAM.get("/club/statistics/coach",
                                 {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
                payload = _coach_stats_payload(r.content.decode("utf-8", errors="replace"))
                if r.status_code == 200:
                    set_stats_cached(ck, payload)
            except Exception as exc:
                return _exc_error(exc, 502)
    else:
        try:
            r = UPSTREAM.get("/club/statistics/coach",
                             {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
            payload = _coach_stats_payload(r.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            return _exc_error(exc, 502)
    if r.status_code != 200:
        return jsonify({"error": "upstream error"}), 502
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


def _job_states() -> dict:
    """job_state rows as {job: {last_run, last_ok, ok, calls, errors}} (empty when the table is missing)."""
    try:
        conn = _db_connect()
        try:
            rows = conn.execute("SELECT job, last_run, ok, calls, errors, last_ok FROM job_state").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {r[0]: {"last_run": _iso_z(r[1]) if r[1] else None,
                   "last_ok": _iso_z(r[5]) if r[5] else None, "ok": bool(r[2]),
                   "calls": r[3], "errors": r[4]} for r in rows}


@app.route("/api/health", methods=["GET"])
def api_health():
    """Upstream client counters, breaker state and background job state. No personal data."""
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    body = UPSTREAM.snapshot()
    body["jobs"] = _job_states()
    resp = Response(json.dumps(body), status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/<path:p>", methods=["GET"], provide_automatic_options=False)
def proxy(p):
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    full_path = "/" + p
    if full_path not in ALLOWED_PATHS:
        resp = jsonify({"error": "path not proxied"})
        resp.status_code = 403
        if via_query:
            _set_cookie(resp, via_query)
        return resp

    # Check SQLite cache for availability path
    if full_path == CACHED_PATH:
        club_id, start, end = _cache_key(request.args.to_dict())
        cached = get_cached(club_id, start, end)
        if cached is not None:
            resp = Response(_scrub_json_text(cached), status=200, content_type="application/json")
            if via_query:
                _set_cookie(resp, via_query)
            return resp
        # Single-flight: concurrent misses for one window share one upstream fetch
        lock = _single_flight_lock(f"avail:{club_id}:{start}:{end}")
    else:
        lock = None

    qs_pairs = []
    if request.query_string:
        # Strip our own token param before forwarding
        qs_pairs = [(k, v) for k, v in parse_qsl(request.query_string.decode()) if k != "token"]

    def _forward():
        # Random jitter before forwarding - breaks timing correlation
        time.sleep(random.uniform(0.05, 0.18))
        try:
            upstream = UPSTREAM.get(full_path, qs_pairs, timeout=15)
            if upstream.status_code == 401:
                # Never relay an upstream 401 (it would look like CourtView's own auth) or cache it.
                resp401 = jsonify({"error": "upstream_auth_required"})
                resp401.status_code = 502
                return resp401

            # Always relay as application/json (never the upstream content-type), and only
            # when the body really is JSON.
            text = upstream.content.decode("utf-8", errors="replace")
            try:
                json.loads(text)
            except ValueError:
                bad = jsonify({"error": "upstream returned non-JSON"})
                bad.status_code = 502
                return bad
            body = _scrub_json_text(text).encode("utf-8")

            # Store the scrubbed payload only when it is real availability (never an error dict)
            if full_path == CACHED_PATH and upstream.status_code == 200:
                club_id, start, end = _cache_key(request.args.to_dict())
                store_cached(club_id, start, end, body.decode("utf-8", errors="replace"))

            return Response(body, status=upstream.status_code, content_type="application/json")
        except Exception as exc:
            return _exc_error(exc, 502)

    if lock is None:
        resp = _forward()
    else:
        with lock:
            club_id, start, end = _cache_key(request.args.to_dict())
            cached = get_cached(club_id, start, end)
            if cached is not None:
                resp = Response(_scrub_json_text(cached), status=200, content_type="application/json")
            else:
                resp = _forward()

    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.after_request
def _security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    path = request.path
    if path == "/":
        csp = _dashboard_csp
    elif path == "/access":
        csp = CSP_ACCESS
    else:
        csp = CSP_API
    response.headers.setdefault("Content-Security-Policy", csp)  # _forbidden keeps its own
    if path.startswith("/api/") or path == "/access/data":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(404)
def _not_found(_exc):
    """Unmatched URLs: still token-gated so unauthenticated callers cannot probe the URL space."""
    passed, _ = _gate()
    if not passed:
        return _forbidden()
    return Response(b"Not found.", status=404, content_type="text/plain")


@app.after_request
def log_req(response):
    path = request.path
    if path not in _ACCESS_PATHS:
        ip = _get_client_ip()
        # Label comes from the gate result; requests that never reached the gate
        # (e.g. non-GET 403s) fall back to the status code.
        auth = g.get("cv_auth") or ("RATE_LIMITED" if response.status_code == 429 else "BLOCKED")
        ua = request.headers.get("User-Agent", "")
        _log_access(ip, path, auth, ua)
    return response


@app.route("/access", methods=["GET"])
def access_page():
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        g.cv_auth = "RATE_LIMITED"
        return Response(b"Too Many Requests", status=429, content_type="text/plain")
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    if via_query:
        resp = Response(status=302)
        resp.headers["Location"] = "/access"
        resp.set_cookie(COOKIE_NAME, via_query, max_age=COOKIE_MAX_AGE, path="/", httponly=True, secure=True, samesite="Strict")
        return resp
    body = _ACCESS_HTML_BYTES or b"<p>access.html not loaded</p>"
    return Response(body, status=200, content_type="text/html; charset=utf-8")


@app.route("/access.js", methods=["GET"])
def access_js():
    passed, _ = _gate()
    if not passed:
        return _forbidden()
    body = _ACCESS_JS_BYTES or b""
    return Response(body, status=200, content_type="application/javascript")


@app.route("/dashboard.css", methods=["GET"])
def dashboard_css():
    passed, _ = _gate()
    if not passed:
        return _forbidden()
    body = _DASHBOARD_CSS_BYTES or b""
    return Response(body, status=200, content_type="text/css")


@app.route("/access/data", methods=["GET"])
def access_data():
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        g.cv_auth = "RATE_LIMITED"
        return Response(b"Too Many Requests", status=429, content_type="text/plain")
    passed, _ = _gate()
    if not passed:
        return _forbidden()
    with _ACCESS_LOCK:
        entries = list(_ACCESS_LOG)
    # Trigger resolution for any IPs not yet in cache
    with _IP_LOCK:
        for e in entries:
            eip = e["ip"]
            if eip not in _IP_CACHE:
                _IP_CACHE[eip] = _LOOKUP_PENDING
                threading.Thread(target=_resolve_ip, args=(eip,), daemon=True).start()
        cache_snap = dict(_IP_CACHE)
    log = _enrich_access_log(entries, cache_snap)
    blocked     = sum(1 for e in log if e["auth"] == "BLOCKED")
    authed      = sum(1 for e in log if e["auth"] == "OK")
    token_visits = sum(1 for e in log if e["auth"] == "TOKEN")
    unique      = len({e["ip"] for e in log})
    return Response(
        json.dumps({
            "total":        len(log),
            "unique_ips":   unique,
            "blocked":      blocked,
            "authed":       authed,
            "token_visits": token_visits,
            "log":          log,
            "server_date":  datetime.now().strftime("%Y-%m-%d"),
        }),
        status=200, content_type="application/json",
    )




# ---------------------------------------------------------------------------
# Heatmap: /club/statistics/operational (hottest_time_slots, DOW and court arrays)
# ---------------------------------------------------------------------------

_HEATMAP_ARRAYS = ("hottest_time_slots", "week_day_wise_activity_count_combined_graph",
                   "court_wise_activity_count_combined_graph")


def _fetch_heatmap_for_club(club_id: str, n_courts: int) -> bool:
    """One call to /club/statistics/operational; build DOW x hour matrix from hottest_time_slots
    (hour-of-day signal) × week_day_wise_activity_count_combined_graph (DOW signal)."""
    print(f"[heatmap] fetching operational stats for club {club_id}")
    now_ms   = int(_now() * 1000)
    start_ms = now_ms - 30 * 24 * 3600 * 1000  # 30 days
    try:
        r    = UPSTREAM.get("/club/statistics/operational",
                            {"club_ids": club_id, "selected_duration": "month", "start_time": start_ms,
                             "end_time": now_ms, "user_timezone": "Europe/London"}, timeout=15)
        data = r.json()
    except Exception as exc:
        print(f"[heatmap] fetch error for club {club_id}: {type(exc).__name__}")
        return False

    # Validate before any DB write: an error shape or empty payload must never overwrite real data.
    if not isinstance(data, dict) or "detail" in data:
        print(f"[heatmap] API error for club {club_id}")
        return False
    if r.status_code != 200 or any(not isinstance(data.get(k), list) for k in _HEATMAP_ARRAYS) \
            or not any(data[k] for k in _HEATMAP_ARRAYS):
        print(f"[heatmap] invalid payload for club {club_id} (status {r.status_code}), nothing written")
        return False

    # --- Hour-of-day signal from hottest_time_slots (30-min buckets → aggregate per hour) ---
    hour_counts: dict = {}
    for entry in data.get("hottest_time_slots", []):
        try:
            hr  = int(entry["x"].split(":")[0])
            val = float(entry.get("y") or 0)
            hour_counts[hr] = hour_counts.get(hr, 0.0) + val
        except (ValueError, KeyError):
            pass
    max_hour = max(hour_counts.values()) if hour_counts else 1.0

    # --- DOW signal from week_day_wise_activity_count_combined_graph ---
    dow_map    = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    dow_counts: dict = {}
    for entry in data.get("week_day_wise_activity_count_combined_graph", []):
        dow_idx = dow_map.get(entry.get("x"))
        if dow_idx is not None:
            try:
                dow_counts[dow_idx] = float(entry.get("y") or 0)
            except ValueError:
                pass
    max_dow = max(dow_counts.values()) if dow_counts else 1.0

    # --- Per-court booking counts from court_wise_activity_count_combined_graph ---
    court_rows: list = []
    for entry in data.get("court_wise_activity_count_combined_graph", []):
        name = entry.get("x")
        if not name:
            continue
        try:
            count = int(float(entry.get("y") or 0))
        except (TypeError, ValueError):
            continue
        court_rows.append((str(name), count))

    # --- Build DOW x hour occupancy matrix (product of normalised signals) ---
    now_ts = int(_now())
    conn = _db_connect()
    try:
        # Product matrix
        for dow in range(7):
            dow_w = dow_counts.get(dow, 0.0) / max_dow
            for hr in HEATMAP_HOURS:
                hr_w = hour_counts.get(hr, 0.0) / max_hour
                occ  = round(dow_w * hr_w, 4)
                conn.execute(
                    "INSERT OR REPLACE INTO heatmap_cache (club_id, dow, hour, avg_occ, samples, fetched_at) VALUES (?,?,?,?,?,?)",
                    (club_id, dow, hr, occ, 30, now_ts),
                )
        # Raw hour signal (all hours 0-23 for completeness, clipped to HEATMAP_HOURS)
        for hr in HEATMAP_HOURS:
            norm = round(hour_counts.get(hr, 0.0) / max_hour, 4)
            conn.execute(
                "INSERT OR REPLACE INTO heatmap_hour_signal (club_id, hour, norm, fetched_at) VALUES (?,?,?,?)",
                (club_id, hr, norm, now_ts),
            )
        # Raw DOW signal
        for dow in range(7):
            norm = round(dow_counts.get(dow, 0.0) / max_dow, 4)
            conn.execute(
                "INSERT OR REPLACE INTO heatmap_dow_signal (club_id, dow, norm, fetched_at) VALUES (?,?,?,?)",
                (club_id, dow, norm, now_ts),
            )
        # Court popularity - wipe-and-replace so renamed/removed courts disappear
        conn.execute("DELETE FROM court_popularity WHERE club_id=?", (club_id,))
        for name, count in court_rows:
            conn.execute(
                "INSERT OR REPLACE INTO court_popularity (club_id, court_name, count, fetched_at) VALUES (?,?,?,?)",
                (club_id, name, count, now_ts),
            )
        conn.commit()
        print(f"[heatmap] stored 30-day operational data for club {club_id} ({len(court_rows)} courts)")
        return True
    except sqlite3.Error as exc:
        print(f"[heatmap] db write error: {exc}")
        return False
    finally:
        conn.close()


def _heatmap_refresh_loop() -> None:
    """Refresh every club once per 24h. The schedule lives in job_state, so a restart neither
    resets nor immediately re-runs it; a failed run is retried after 30 min."""
    while True:
        _wait_for_job("heatmap", HEATMAP_STALE_SECS, JOB_RETRY_SECS)
        started = int(_now())
        calls = errors = 0
        for club in HEATMAP_CLUBS:
            calls += 1
            try:
                good = _fetch_heatmap_for_club(club["id"], club["courts"])
            except Exception as exc:
                print(f"[heatmap] refresh failed for club {club['id']}: {type(exc).__name__}")
                good = False
            if not good:
                errors += 1
        _job_record("heatmap", started, errors == 0, calls, errors)


# ---------------------------------------------------------------------------
# Background cache refresh thread
# ---------------------------------------------------------------------------

def _availability_cycle() -> tuple:
    """One refresh cycle: purge, then fetch London days today..today+27 for the live clubs.
    Returns (ok, attempted, refreshed). ok is False when the breaker stopped the cycle or at
    least half the attempts failed (it is then retried after 30 min)."""
    t0 = _now()
    purged = _purge_availability(t0)
    targets = _availability_refresh_targets(t0)
    refreshed = errors = 0
    attempted = 0
    aborted = False
    for club_id, start, end in targets:
        params = [("club_id", club_id)]
        if start:
            params.append(("start_datetime", start))
        if end:
            params.append(("end_datetime", end))
        attempted += 1
        try:
            r = UPSTREAM.get(CACHED_PATH, params, timeout=15)
            if r.status_code == 200:
                store_cached(club_id, start, end,
                             _scrub_json_text(r.content.decode("utf-8", errors="replace")))
                refreshed += 1
            else:
                errors += 1
        except UpstreamUnavailable:
            errors += 1
            aborted = True
            break
        except Exception as exc:
            errors += 1
            print(f"[refresh] fetch error: {type(exc).__name__}", flush=True)
        time.sleep(random.uniform(0.05, 0.18))

    ok = not aborted and errors * 2 <= attempted
    print(f"[refresh] {refreshed}/{len(targets)} refreshed, purged {purged} in {round(_now() - t0, 1)}s", flush=True)

    # Periodically evict stale stats_cache entries (no TTL enforced at write time)
    stats_cutoff = int(_now()) - STATS_CACHE_TTL
    try:
        conn = _db_connect()
        try:
            conn.execute("DELETE FROM stats_cache WHERE fetched_at < ?", (stats_cutoff,))
            conn.commit()
            print("[refresh] stats_cache stale entries evicted")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] stats_cache cleanup error: {exc}")

    last_ok = _job_record("availability", int(t0), ok, attempted, errors)
    _write_job_status("availability", int(t0), ok, attempted, errors, last_ok)
    return ok, attempted, refreshed


def _refresh_loop() -> None:
    """Every 6 hours (persisted in job_state), refresh availability for today..today+27 only."""
    purged = _purge_availability(_now())
    print(f"[refresh] start-up purge removed {purged} past or archived rows", flush=True)
    while True:
        _wait_for_job("availability", AVAIL_REFRESH_SECS, JOB_RETRY_SECS)
        try:
            _availability_cycle()
        except Exception as exc:
            print(f"[refresh] cycle failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(JOB_RETRY_SECS)


# ---------------------------------------------------------------------------
# Module-level startup (runs at import time so gunicorn picks it up)
# ---------------------------------------------------------------------------

_STARTUP_LOCK = threading.Lock()
_STARTUP_DONE = False

# Per-key single-flight locks (stats cache, club-info, membership, availability, payment days)
_STATS_FETCH_LOCKS: dict = {}
_STATS_FETCH_MUTEX = threading.Lock()


def _single_flight_lock(key: str) -> threading.Lock:
    """One lock per key: concurrent misses queue up and re-check the cache under the lock."""
    with _STATS_FETCH_MUTEX:
        return _STATS_FETCH_LOCKS.setdefault(key, threading.Lock())


def _startup() -> None:
    """Initialise DB, load assets, open access log, start refresh threads.
    Called once at module import time so gunicorn (which never runs __main__) picks it up.
    Safe to call multiple times: init_db is idempotent; thread start is guarded by a module flag."""
    global _STARTUP_DONE, _ACCESS_FD
    with _STARTUP_LOCK:
        if _STARTUP_DONE:
            return
        _STARTUP_DONE = True

    print("[startup] courtview starting (bind address set by gunicorn ExecStart in courtview.service)")
    print(f"[startup] UA profile  : Android OkHttp (from APK analysis)")
    print(f"[startup] User-Agent  : {ANDROID_UA}")
    print(f"[startup] DB path     : {DB_PATH}")

    init_db()
    _rewrite_status_files()
    _load_dashboard()
    _load_web_assets()

    # Open access log file and pre-load recent entries into memory
    try:
        _ACCESS_FD = _open_access_log()
        try:
            with open(ACCESS_LOG_PATH, "r") as _fh:
                _lines = _fh.readlines()
            for _line in _lines[-500:]:
                try:
                    _ACCESS_LOG.append(json.loads(_line.strip()))
                except (json.JSONDecodeError, ValueError):
                    pass
            _tmp = list(_ACCESS_LOG)
            _ACCESS_LOG.clear()
            for _e in reversed(_tmp):
                _ACCESS_LOG.appendleft(_e)
            print(f"[startup] access log: loaded {len(_ACCESS_LOG)} entries from {ACCESS_LOG_PATH}")
        except FileNotFoundError:
            pass
    except OSError as _exc:
        print(f"[startup] WARNING: could not open access log: {_exc}")

    refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
    refresh_thread.start()
    print("[startup] availability refresh thread started (6h cycle, persisted)")

    heatmap_thread = threading.Thread(target=_heatmap_refresh_loop, daemon=True)
    heatmap_thread.start()
    payments_thread = threading.Thread(target=_payments_sync_loop, daemon=True)
    payments_thread.start()
    print("[startup] payments sync thread started")
    print(f"[startup] heatmap refresh thread started ({len(HEATMAP_CLUBS)} clubs, 24h cycle)")

    print("[startup] archive refresh thread disabled (Racketeer migrated off Padelmates)")


if os.environ.get("COURTVIEW_SKIP_STARTUP") != "1":
    _startup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # gunicorn runs courtview:app and triggers _startup() at import.
    # This block is the dev fallback (python3 courtview.py).
    app.run(host="0.0.0.0", port=8766, debug=False, threaded=True, use_reloader=False)
