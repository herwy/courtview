"""Core routes: dashboard, static assets, health, club list, generic proxy and the access page."""

import json
import os
import random
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl

from flask import g, request, Response, jsonify
from werkzeug.security import safe_join

from . import clubs, config, upstream, privacy, db, jobs, accesslog, security


def index():
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    if not security._dashboard_bytes:
        return Response(b"Dashboard not loaded.", status=503, content_type="text/plain")
    if via_query:
        resp = Response(status=302)
        resp.headers["Location"] = "/"
        security._set_cookie(resp, via_query)
        return resp
    return Response(security._dashboard_bytes, status=200, content_type="text/html; charset=utf-8")


def static_asset(filename):
    """Serve static files (.css, .js) from the same directory as this script. Auth-gated first
    (no 404 oracle for unauthenticated callers), then traversal-hardened."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    raw = (request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI") or "").lower()
    if (".." in filename or "\\" in filename or "%" in filename or "\x00" in filename
            or filename.startswith("/") or any(t in raw for t in ("%2e", "%2f", "%5c"))):
        return Response(b"Not found.", status=404, content_type="text/plain")

    allowed_exts = {".css", ".js", ".ico", ".png", ".svg", ".woff2"}
    _, ext = os.path.splitext(filename)
    if ext.lower() not in allowed_exts:
        return Response(b"Not found.", status=404, content_type="text/plain")

    asset_path = safe_join(config.ROOT_DIR, filename)
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
        security._set_cookie(resp, via_query)
    return resp


def api_health():
    """Upstream client counters, breaker state and background job state. No personal data."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    body = upstream.UPSTREAM.snapshot()
    body["jobs"] = jobs._job_states()
    resp = Response(json.dumps(body), status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_clubs():
    """Club list for the dashboard (courtview_app/clubs.py). Static config, no personal data."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    resp = Response(json.dumps({"clubs": clubs.public_clubs()}), status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def proxy(p):
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    full_path = "/" + p
    if full_path not in upstream.ALLOWED_PATHS:
        resp = jsonify({"error": "path not proxied"})
        resp.status_code = 403
        if via_query:
            security._set_cookie(resp, via_query)
        return resp

    # Check SQLite cache for availability path
    if full_path == upstream.CACHED_PATH:
        club_id, start, end = db._cache_key(request.args.to_dict())
        cached = db.get_cached(club_id, start, end)
        if cached is not None:
            resp = Response(privacy._scrub_json_text(cached), status=200, content_type="application/json")
            if via_query:
                security._set_cookie(resp, via_query)
            return resp
        # Single-flight: concurrent misses for one window share one upstream fetch
        lock = db._single_flight_lock(f"avail:{club_id}:{start}:{end}")
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
            reply = upstream.UPSTREAM.get(full_path, qs_pairs, timeout=15)
            if reply.status_code == 401:
                # Never relay an upstream 401 (it would look like CourtView's own auth) or cache it.
                resp401 = jsonify({"error": "upstream_auth_required"})
                resp401.status_code = 502
                return resp401

            # Always relay as application/json (never the upstream content-type), and only
            # when the body really is JSON.
            text = reply.content.decode("utf-8", errors="replace")
            try:
                json.loads(text)
            except ValueError:
                bad = jsonify({"error": "upstream returned non-JSON"})
                bad.status_code = 502
                return bad
            body = privacy._scrub_json_text(text).encode("utf-8")

            # Store the scrubbed payload only when it is real availability (never an error dict)
            if full_path == upstream.CACHED_PATH and reply.status_code == 200:
                club_id, start, end = db._cache_key(request.args.to_dict())
                db.store_cached(club_id, start, end, body.decode("utf-8", errors="replace"))

            return Response(body, status=reply.status_code, content_type="application/json")
        except Exception as exc:
            return security._exc_error(exc, 502)

    if lock is None:
        resp = _forward()
    else:
        with lock:
            club_id, start, end = db._cache_key(request.args.to_dict())
            cached = db.get_cached(club_id, start, end)
            if cached is not None:
                resp = Response(privacy._scrub_json_text(cached), status=200, content_type="application/json")
            else:
                resp = _forward()

    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def access_page():
    ip = accesslog._get_client_ip()
    if security._is_rate_limited(ip):
        g.cv_auth = "RATE_LIMITED"
        return Response(b"Too Many Requests", status=429, content_type="text/plain")
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    if via_query:
        resp = Response(status=302)
        resp.headers["Location"] = "/access"
        resp.set_cookie(config.COOKIE_NAME, via_query, max_age=config.COOKIE_MAX_AGE, path="/", httponly=True, secure=True, samesite="Strict")
        return resp
    body = accesslog._ACCESS_HTML_BYTES or b"<p>access.html not loaded</p>"
    return Response(body, status=200, content_type="text/html; charset=utf-8")


def access_js():
    passed, _ = security._gate()
    if not passed:
        return security._forbidden()
    body = accesslog._ACCESS_JS_BYTES or b""
    return Response(body, status=200, content_type="application/javascript")


def dashboard_css():
    passed, _ = security._gate()
    if not passed:
        return security._forbidden()
    body = accesslog._DASHBOARD_CSS_BYTES or b""
    return Response(body, status=200, content_type="text/css")


def access_data():
    ip = accesslog._get_client_ip()
    if security._is_rate_limited(ip):
        g.cv_auth = "RATE_LIMITED"
        return Response(b"Too Many Requests", status=429, content_type="text/plain")
    passed, _ = security._gate()
    if not passed:
        return security._forbidden()
    with accesslog._ACCESS_LOCK:
        entries = list(accesslog._ACCESS_LOG)
    # Trigger resolution for any IPs not yet in cache
    with accesslog._IP_LOCK:
        for e in entries:
            eip = e["ip"]
            if eip not in accesslog._IP_CACHE:
                accesslog._IP_CACHE[eip] = accesslog._LOOKUP_PENDING
                threading.Thread(target=accesslog._resolve_ip, args=(eip,), daemon=True).start()
        cache_snap = dict(accesslog._IP_CACHE)
    log = accesslog._enrich_access_log(entries, cache_snap)
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


ROUTES = (
    ("/", index, {"methods": ["GET"]}),
    ("/<path:filename>", static_asset, {"methods": ["GET"]}),
    ("/api/health", api_health, {"methods": ["GET"]}),
    ("/api/clubs", api_clubs, {"methods": ["GET"]}),
    ("/api/<path:p>", proxy, {"methods": ["GET"], "provide_automatic_options": False}),
    ("/access", access_page, {"methods": ["GET"]}),
    ("/access.js", access_js, {"methods": ["GET"]}),
    ("/dashboard.css", dashboard_css, {"methods": ["GET"]}),
    ("/access/data", access_data, {"methods": ["GET"]}),
)
