"""Token gate, rate limiting, CSP and security headers, and the request hooks that enforce them."""

import base64
import hashlib
import html
import os
import re
import threading

from flask import g, request, Response, jsonify

from . import clubs, config, accesslog


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

_AUTH_FAILS: dict = {}   # {ip: [timestamps of recent auth failures]}
_AUTH_LOCK        = threading.Lock()
AUTH_RATE_WINDOW  = 60   # seconds
AUTH_RATE_LIMIT   = 10   # failures within window before lockout


def _is_rate_limited(ip: str) -> bool:
    """Return True if this IP has exceeded AUTH_RATE_LIMIT failures in AUTH_RATE_WINDOW seconds."""
    now = config._now()
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
        _AUTH_FAILS.setdefault(ip, []).append(config._now())


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
            st = os.stat(config.TOKEN_PATH)
        except OSError:
            _token_log("degraded", "reload failed (missing), "
                       + ("keeping last good token" if last else "no valid token, denying all"))
            return last or ""
        key = (st.st_mtime_ns, st.st_size)
        if key == _token_cache["key"]:
            return last or ""
        try:
            with open(config.TOKEN_PATH) as fh:
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
        config.COOKIE_NAME,
        token,
        max_age=config.COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


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
        with open(config.DASHBOARD_HTML, "rb") as fh:
            _dashboard_bytes = fh.read()
        print(f"[startup] loaded {config.DASHBOARD_HTML} ({len(_dashboard_bytes)} bytes)")
        _dashboard_csp = _build_dashboard_csp(_dashboard_bytes)
    except OSError as exc:
        print(f"[startup] WARNING: could not load dashboard HTML: {exc}")


def _forbidden() -> Response:
    """Return 403 with styled forbidden page."""
    return Response(_FORBIDDEN_HTML, status=403, content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})


def _provided_token() -> tuple[str, str]:
    """Return (query_token, cookie_token). ?token= counts on GET/HEAD only."""
    query_token = request.args.get("token", "") if request.method in ("GET", "HEAD") else ""
    return query_token, request.cookies.get(config.COOKIE_NAME, "")


def _gate() -> tuple[bool, str]:
    """Check rate limit and token. Return (passed, token_value)."""
    ip = accesslog._get_client_ip()
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


def _allow_only_get_head():
    """Defence in depth: the dashboard never issues methods other than GET/HEAD."""
    if request.method not in ("GET", "HEAD"):
        return _forbidden()
    return None


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
    if clubs.RACKETEER_CLUB_ID not in ids:
        return None
    query_token, cookie_token = _provided_token()
    provided = query_token or cookie_token
    expected = _read_token()
    if not provided or not expected or not _constant_time_eq(provided, expected):
        return None
    g.cv_auth = "OK"
    print(f"[archived] 410 {request.method} {path}", flush=True)
    return jsonify({"error": config.ARCHIVED_ERROR, "message": config.ARCHIVED_MESSAGE}), 410


def _exc_error(exc, status: int):
    """Generic JSON error for an unexpected exception (F16): the exception type goes to the
    journal, never its message (it can carry URLs, paths or upstream text) and never to the client."""
    print(f"[error] {type(exc).__name__} path={request.path}", flush=True)
    msg = "upstream unavailable" if status == 502 else "internal error"
    resp = jsonify({"error": msg})
    resp.status_code = status
    return resp


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


def _not_found(_exc):
    """Unmatched URLs: still token-gated so unauthenticated callers cannot probe the URL space."""
    passed, _ = _gate()
    if not passed:
        return _forbidden()
    return Response(b"Not found.", status=404, content_type="text/plain")
