#!/usr/bin/env python3
"""
courtview.py - Padelmates API proxy server for RPi.

Serves the Racketeer dashboard HTML at / and proxies allowed Padelmates API
paths at /api/* using Android OkHttp headers. Includes token auth,
per-IP rate limiting, SQLite availability cache, and a 6-hour refresh thread.

Run: python3 courtview.py
Deployed to: /root/projects/courtview/courtview.py
"""

import html
import json
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from time import time as _now
from urllib.parse import urlencode, parse_qsl

from flask import Flask, Response, jsonify, make_response, request
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
    "/player/player_booking/all_courts_slot_prices_v2",
    "/player/player_booking/search_clubs",
    "/club/",
    "/club/membership/",
    "/club/creditpackage/",
    "/club/statistics/financial",
    "/club/statistics/financial/v2",
    "/club/club_extras",
}

# Path that uses the SQLite availability cache
CACHED_PATH = "/player/player_booking/all_courts_slot_prices_v2"

# Clubs and court counts for server-side heatmap fetching
HEATMAP_CLUBS = [
    {"id": "5111764d9bb14be3adbdb8e133e8bd80", "courts": 11},  # Racketeer
    {"id": "47d2eb0db7194a9dbd29783c3a2a82ad", "courts": 7},   # Padium Canary Wharf
    {"id": "788fa2c66535421aabc60fd27f941c42",  "courts": 12},  # Rocket Padel Ilford
]
HEATMAP_HOURS      = list(range(7, 23))   # 07:00-22:00
HEATMAP_STALE_SECS = 24 * 3600           # refresh every 24h (1 API call per club)

# Runtime paths on RPi
TOKEN_PATH      = "/root/.courtview_token"
DB_PATH         = "/root/projects/courtview/courtview_cache.db"
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
    return conn

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

    # Retitle
    _ACCESS_HTML_BYTES = _ACCESS_HTML_BYTES.replace(
        b'<title>doxxnet Labs</title>',
        b'<title>CourtView Access Log</title>',
    )

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


def _log_access(ip: str, path: str, auth: str, ua: str = "") -> None:
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
            except OSError:
                pass
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
# Token reader - mtime-cached so disk is not read on every request
# ---------------------------------------------------------------------------

_token_cache: dict = {"value": None, "mtime": -1.0}
_token_lock = threading.Lock()


def _read_token() -> str:
    """Return the current token, re-reading the file if it has changed on disk."""
    with _token_lock:
        try:
            mtime = os.path.getmtime(TOKEN_PATH)
        except OSError:
            return ""
        if mtime != _token_cache["mtime"]:
            try:
                with open(TOKEN_PATH) as fh:
                    _token_cache["value"] = fh.read().strip()
                _token_cache["mtime"] = mtime
            except OSError:
                return ""
        return _token_cache["value"] or ""


def _check_token(req) -> bool:
    """Return True if the request carries a valid courtview token."""
    expected = _read_token()
    if not expected:
        return False
    query_token  = req.args.get("token", "")
    cookie_token = req.cookies.get(COOKIE_NAME, "")
    provided = query_token or cookie_token
    # Constant-time comparison to resist timing attacks
    if len(provided) != len(expected):
        return False
    result = 0
    for a, b in zip(provided, expected):
        result |= ord(a) ^ ord(b)
    return result == 0


def _set_cookie(response, token: str) -> None:
    """Attach a long-lived HttpOnly cookie to response when token came via query param."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="Strict",
    )


# ---------------------------------------------------------------------------
# SQLite availability cache helpers
# ---------------------------------------------------------------------------

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
        conn.commit()
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
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT payload FROM availability WHERE club_id=? AND start_datetime=? AND end_datetime=? AND fetched_at>?",
            (club_id, start, end, cutoff),
        ).fetchone()
        conn.close()
        if not row:
            return None
        if row[0].strip() == "[]":
            return None
        return row[0]
    except sqlite3.Error:
        return None


def store_cached(club_id: str, start: str, end: str, payload: str) -> None:
    """Insert or replace a cache entry."""
    if not club_id:
        return
    # Don't cache empty-list v1 responses - they pre-date the v2 switch and are useless
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, list) and len(parsed) == 0:
            return
    except (ValueError, TypeError):
        pass
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO availability (club_id, start_datetime, end_datetime, payload, fetched_at) VALUES (?,?,?,?,?)",
            (club_id, start, end, payload, int(_now())),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[cache] store error: {exc}")


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Load dashboard HTML into memory at startup
_dashboard_bytes: bytes = b""


def _load_dashboard() -> None:
    global _dashboard_bytes
    try:
        with open(DASHBOARD_HTML, "rb") as fh:
            _dashboard_bytes = fh.read()
        print(f"[startup] loaded {DASHBOARD_HTML} ({len(_dashboard_bytes)} bytes)")
    except OSError as exc:
        print(f"[startup] WARNING: could not load dashboard HTML: {exc}")


def _get_client_ip() -> str:
    """Return the real client IP (no proxy in front of this server)."""
    return request.remote_addr or "0.0.0.0"


def _forbidden() -> Response:
    """Return 403 with styled forbidden page."""
    return Response(_FORBIDDEN_HTML, status=403, content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})


def _gate() -> tuple[bool, str]:
    """Check rate limit and token. Return (passed, token_value)."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return False, ""
    query_token  = request.args.get("token", "")
    cookie_token = request.cookies.get(COOKIE_NAME, "")
    provided = query_token or cookie_token
    expected = _read_token()
    if not expected or not provided:
        _record_auth_fail(ip)
        return False, ""
    if len(provided) != len(expected):
        _record_auth_fail(ip)
        return False, ""
    result = 0
    for a, b in zip(provided, expected):
        result |= ord(a) ^ ord(b)
    if result != 0:
        _record_auth_fail(ip)
        print(f"[auth] fail from {html.escape(ip)}")
        return False, ""
    return True, query_token  # return query_token so caller knows if cookie should be set


@app.route("/", methods=["GET"])
def index():
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    if not _dashboard_bytes:
        return Response(b"Dashboard not loaded.", status=503, content_type="text/plain")
    resp = Response(_dashboard_bytes, status=200, content_type="text/html; charset=utf-8")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/<path:filename>", methods=["GET"])
def static_asset(filename):
    """Serve static files (.css, .js) from the same directory as this script. Auth-gated."""
    allowed_exts = {".css", ".js", ".ico", ".png", ".svg", ".woff2"}
    _, ext = os.path.splitext(filename)
    if ext.lower() not in allowed_exts:
        # Not a recognised static asset and not an /api/ path - 404
        return Response(b"Not found.", status=404, content_type="text/plain")

    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    asset_path = os.path.join(os.path.dirname(__file__), filename)
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
        return jsonify({"error": str(exc)}), 500

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
        return jsonify({"error": str(exc)}), 500

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
    days: dict = {}
    try:
        conn = _db_connect()
        for i in range(28):
            d        = today + timedelta(days=i)
            start_ms = int(datetime(d.year, d.month, d.day, 0, 0, 0,
                                    tzinfo=_LONDON_TZ).timestamp() * 1000)
            end_ms   = int(datetime(d.year, d.month, d.day, 23, 59, 59, 999000,
                                    tzinfo=_LONDON_TZ).timestamp() * 1000)
            row = conn.execute(
                "SELECT payload FROM availability"
                " WHERE club_id=? AND start_datetime=? AND end_datetime=? AND fetched_at>?",
                (club_id, str(start_ms), str(end_ms), cutoff),
            ).fetchone()
            days[str(d)] = json.loads(row[0]) if row else None
        conn.close()
    except sqlite3.Error as exc:
        return jsonify({"error": str(exc)}), 500

    resp = Response(
        json.dumps({"club_id": club_id, "days": days}),
        status=200, content_type="application/json",
    )
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/membership-members", methods=["GET"])
def api_membership_members():
    """Fan out one request per membership plan and return per-plan member counts.

    Returns { club_id, counts: { plan_id: int, ... } }.
    /club/membership/member is NOT in ALLOWED_PATHS - this route keeps PII server-side only.
    """
    passed, via_query = _gate()
    if not passed:
        return _forbidden()

    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400

    force_refresh = request.args.get("refresh") == "1"
    empty_resp = json.dumps({"club_id": club_id, "counts": {}})

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
                resp = Response(row[0], status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    # H-02: per-club fan-out lock - only one thread fetches at a time per club
    with _MEMBERSHIP_FETCH_MUTEX:
        lock = _MEMBERSHIP_FETCH_LOCKS.setdefault(club_id, threading.Lock())
    if not lock.acquire(blocking=False):
        # Another thread is fetching for this club; serve stale cache if available
        try:
            conn = _db_connect()
            row = conn.execute(
                "SELECT payload FROM membership_members_cache WHERE club_id=?",
                (club_id,),
            ).fetchone()
            conn.close()
            if row:
                resp = Response(row[0], status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass
        resp = Response(empty_resp, status=200, content_type="application/json")
        if via_query:
            _set_cookie(resp, via_query)
        return resp

    try:
        # Fetch the list of membership plans for this club
        try:
            plans_r = cffi_requests.get(
                f"{TARGET}/club/membership/?club_id={club_id}",
                headers=APP_HEADERS,
                timeout=15,
            )
            if plans_r.status_code != 200:
                resp = Response(empty_resp, status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
            plans_raw = plans_r.json()
            # Upstream returns either a list directly or {"memberships": [...]}
            if isinstance(plans_raw, list):
                plans = plans_raw
            elif isinstance(plans_raw, dict):
                plans = plans_raw.get("memberships") or []
                if not isinstance(plans, list):
                    plans = []
            else:
                plans = []
            if not plans:
                resp = Response(empty_resp, status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502

        counts: dict = {}
        members_by_plan: dict = {}
        for plan in plans:
            pid = plan.get("_id") or plan.get("id")
            if not pid:
                continue
            pid = str(pid)
            time.sleep(random.uniform(0.15, 0.30))
            count = 0
            names: list = []
            try:
                r = cffi_requests.get(
                    f"{TARGET}/club/membership/member?membership_id={pid}",
                    headers=APP_HEADERS,
                    timeout=15,
                )
                if r.status_code == 200:
                    parsed = r.json()
                    raw_list: list = []
                    if isinstance(parsed, list):
                        raw_list = parsed
                    elif isinstance(parsed, dict):
                        for key in ("members", "results", "data", "items"):
                            val = parsed.get(key)
                            if isinstance(val, list):
                                raw_list = val
                                break
                    count = len(raw_list)
                    names = sorted(
                        [{"name": m.get("name", ""), "email": m.get("email", "")}
                         for m in raw_list if m.get("name")],
                        key=lambda x: x["name"].lower(),
                    )
            except Exception:
                count = 0
            counts[pid] = count
            members_by_plan[pid] = names

        payload = json.dumps({"club_id": club_id, "counts": counts, "members": members_by_plan})
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

        resp = Response(payload, status=200, content_type="application/json")
        if via_query:
            _set_cookie(resp, via_query)
        return resp
    finally:
        lock.release()


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
                resp = Response(row[0], status=200, content_type="application/json")
                if via_query:
                    _set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    results: dict = {"profile": None, "memberships": None, "credits": None, "extras": None}
    targets = [
        ("profile",     f"/club/?club_id={club_id}"),
        ("memberships", f"/club/membership/?club_id={club_id}"),
        ("credits",     f"/club/creditpackage/?club_id={club_id}"),
        ("extras",      f"/club/club_extras?club_id={club_id}"),
    ]

    def _fetch(key: str, path: str) -> None:
        try:
            r = cffi_requests.get(TARGET + path, headers=APP_HEADERS, timeout=15)
            if r.status_code == 200:
                d = r.json()
                if key == "extras" and isinstance(d, dict):
                    results[key] = d.get("data", [])
                else:
                    results[key] = d
        except Exception:
            results[key] = None

    threads = [threading.Thread(target=_fetch, args=(k, p), daemon=True) for k, p in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    payload = json.dumps(results)
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

    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.route("/api/<path:p>", methods=["GET", "POST", "OPTIONS"])
def proxy(p):
    if request.method == "OPTIONS":
        resp = Response(status=204)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

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
            resp = Response(cached, status=200, content_type="application/json")
            if via_query:
                _set_cookie(resp, via_query)
            return resp

    # Random jitter before forwarding - breaks timing correlation
    time.sleep(random.uniform(0.05, 0.18))

    # Build upstream URL
    url = TARGET + full_path
    if request.query_string:
        # Strip our own token param before forwarding
        qs_pairs = [(k, v) for k, v in parse_qsl(request.query_string.decode()) if k != "token"]
        if qs_pairs:
            url += "?" + urlencode(qs_pairs)

    try:
        if request.method == "POST":
            upstream = cffi_requests.post(
                url,
                json=request.get_json(silent=True),
                headers=APP_HEADERS,
                timeout=15,
            )
        else:
            upstream = cffi_requests.get(
                url,
                headers=APP_HEADERS,
                timeout=15,
            )

        # Store in cache if this is the availability path
        if full_path == CACHED_PATH and upstream.status_code == 200:
            club_id, start, end = _cache_key(request.args.to_dict())
            store_cached(club_id, start, end, upstream.content.decode("utf-8", errors="replace"))

        ct = upstream.headers.get("content-type", "application/json")
        resp = Response(upstream.content, status=upstream.status_code, content_type=ct)

    except Exception as exc:
        resp = jsonify({"error": str(exc)})
        resp.status_code = 502

    if via_query:
        _set_cookie(resp, via_query)
    return resp


@app.after_request
def log_req(response):
    path = request.path
    if path not in _ACCESS_PATHS:
        ip = _get_client_ip()
        if response.status_code == 429:
            auth = "RATE_LIMITED"
        elif response.status_code == 403:
            auth = "BLOCKED"
        elif request.args.get("token"):
            auth = "TOKEN"
        else:
            auth = "OK"
        ua = request.headers.get("User-Agent", "")
        _log_access(ip, path, auth, ua)
    return response


@app.route("/access", methods=["GET"])
def access_page():
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return Response(b"Too Many Requests", status=429, content_type="text/plain")
    passed, via_query = _gate()
    if not passed:
        return _forbidden()
    if via_query:
        resp = Response(status=302)
        resp.headers["Location"] = "/access"
        resp.set_cookie(COOKIE_NAME, via_query, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="Strict")
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
# Heatmap: server-side get_booked_hours aggregation
# ---------------------------------------------------------------------------

def _heatmap_is_stale(club_id: str) -> bool:
    """Return True if heatmap data for this club is absent or older than HEATMAP_STALE_SECS."""
    cutoff = int(_now()) - HEATMAP_STALE_SECS
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT MIN(fetched_at) FROM heatmap_cache WHERE club_id=?", (club_id,)
        ).fetchone()
        conn.close()
        oldest = row[0] if row and row[0] is not None else 0
        return oldest < cutoff
    except sqlite3.Error:
        return True


def _fetch_heatmap_for_club(club_id: str, n_courts: int) -> None:
    """One call to /club/statistics/operational; build DOW x hour matrix from hottest_time_slots
    (hour-of-day signal) × week_day_wise_activity_count_combined_graph (DOW signal)."""
    print(f"[heatmap] fetching operational stats for club {club_id}")
    now_ms   = int(_now() * 1000)
    start_ms = now_ms - 30 * 24 * 3600 * 1000  # 30 days
    url = (
        f"{TARGET}/club/statistics/operational"
        f"?club_ids={club_id}"
        f"&selected_duration=month"
        f"&start_time={start_ms}&end_time={now_ms}"
        f"&user_timezone=Europe/London"
    )
    try:
        r    = cffi_requests.get(url, headers=APP_HEADERS, timeout=15)
        data = r.json()
    except Exception as exc:
        print(f"[heatmap] fetch error for club {club_id}: {exc}")
        return

    if "detail" in data:
        print(f"[heatmap] API error for club {club_id}: {data['detail']}")
        return

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
    except sqlite3.Error as exc:
        print(f"[heatmap] db write error: {exc}")
    finally:
        conn.close()


def _heatmap_refresh_loop() -> None:
    """On startup refresh any stale clubs, then refresh all clubs every 24h."""
    # Staggered startup: refresh each club that needs it
    for club in HEATMAP_CLUBS:
        if _heatmap_is_stale(club["id"]):
            try:
                _fetch_heatmap_for_club(club["id"], club["courts"])
            except Exception as exc:
                print(f"[heatmap] startup refresh failed for club {club['id']}: {exc}")
    # Daily refresh loop
    while True:
        time.sleep(HEATMAP_STALE_SECS)
        for club in HEATMAP_CLUBS:
            try:
                _fetch_heatmap_for_club(club["id"], club["courts"])
            except Exception as exc:
                print(f"[heatmap] daily refresh failed for club {club['id']}: {exc}")


# ---------------------------------------------------------------------------
# Background cache refresh thread
# ---------------------------------------------------------------------------

def _refresh_loop() -> None:
    """Every 6 hours, re-fetch all availability entries from the past 28 days."""
    while True:
        time.sleep(6 * 3600)
        cutoff = int(_now()) - CACHE_TTL
        try:
            conn = _db_connect()
            rows = conn.execute(
                "SELECT club_id, start_datetime, end_datetime FROM availability WHERE fetched_at > ?",
                (cutoff,),
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            print(f"[refresh] db read error: {exc}")
            continue

        if not rows:
            print("[refresh] no entries to refresh")
            continue

        t0 = _now()
        refreshed = 0
        for club_id, start, end in rows:
            try:
                params = [("club_id", club_id)]
                if start:
                    params.append(("start_datetime", start))
                if end:
                    params.append(("end_datetime", end))
                url = TARGET + CACHED_PATH + "?" + urlencode(params)
                r = cffi_requests.get(
                    url,
                    headers=APP_HEADERS,
                        timeout=15,
                )
                if r.status_code == 200:
                    store_cached(club_id, start, end, r.content.decode("utf-8", errors="replace"))
                    refreshed += 1
                time.sleep(random.uniform(0.05, 0.18))
            except Exception as exc:
                print(f"[refresh] error for club_id={club_id}: {exc}")

        elapsed = round(_now() - t0, 1)
        print(f"[refresh] {refreshed} entries refreshed in {elapsed}s")


# ---------------------------------------------------------------------------
# Module-level startup (runs at import time so gunicorn picks it up)
# ---------------------------------------------------------------------------

_STARTUP_LOCK = threading.Lock()
_STARTUP_DONE = False

_MEMBERSHIP_FETCH_LOCKS: dict = {}
_MEMBERSHIP_FETCH_MUTEX = threading.Lock()


def _startup() -> None:
    """Initialise DB, load assets, open access log, start refresh threads.
    Called once at module import time so gunicorn (which never runs __main__) picks it up.
    Safe to call multiple times: init_db is idempotent; thread start is guarded by a module flag."""
    global _STARTUP_DONE, _ACCESS_FD
    with _STARTUP_LOCK:
        if _STARTUP_DONE:
            return
        _STARTUP_DONE = True

    print(f"[startup] courtview on 0.0.0.0:8766")
    print(f"[startup] UA profile  : Android OkHttp (from APK analysis)")
    print(f"[startup] User-Agent  : {ANDROID_UA}")
    print(f"[startup] DB path     : {DB_PATH}")

    init_db()
    _load_dashboard()
    _load_web_assets()

    # Open access log file and pre-load recent entries into memory
    try:
        _ACCESS_FD = open(ACCESS_LOG_PATH, "a", buffering=1)
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
    print("[startup] availability refresh thread started (6h cycle)")

    heatmap_thread = threading.Thread(target=_heatmap_refresh_loop, daemon=True)
    heatmap_thread.start()
    print(f"[startup] heatmap refresh thread started ({len(HEATMAP_CLUBS)} clubs, 24h cycle)")


_startup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # gunicorn runs courtview:app and triggers _startup() at import.
    # This block is the dev fallback (python3 courtview.py).
    app.run(host="0.0.0.0", port=8766, debug=False, threaded=True, use_reloader=False)
