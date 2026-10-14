"""The Padelmates upstream client: the only code that touches curl_cffi or the upstream host."""

import random
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from curl_cffi import requests as cffi_requests


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
