"""Access log: IP geo lookup, in-memory ring buffer, rotating JSONL file, web assets for the access page."""

import json
import os
import re
import threading
import urllib.request
from collections import deque
from datetime import datetime

from flask import g, request

from . import config


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


_ACCESS_LOG  = deque(maxlen=500)
_ACCESS_LOCK = threading.Lock()
_ACCESS_FD   = None  # opened at startup

_ACCESS_PATHS = {"/access", "/access/data"}

ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024   # rotate access.log past this size
ACCESS_LOG_KEEP      = 5                 # access.log.1 .. access.log.5

# Access page assets loaded from WEB_ASSETS_DIR at startup
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
        path = os.path.join(config.WEB_ASSETS_DIR, filename)
        try:
            with open(path, "rb") as fh:
                globals()[attr] = fh.read()
            print(f"[startup] loaded {path} ({len(globals()[attr])} bytes)")
        except OSError as exc:
            print(f"[startup] WARNING: could not load {path}: {exc}")


def _open_access_log():
    """Append-open ACCESS_LOG_PATH at 0600 (line buffered)."""
    fd = os.open(config.ACCESS_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
            src = config.ACCESS_LOG_PATH if i == 1 else f"{config.ACCESS_LOG_PATH}.{i - 1}"
            dst = f"{config.ACCESS_LOG_PATH}.{i}"
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


def _get_client_ip() -> str:
    """Return real client IP forwarded by Caddy via X-Real-IP, with remote_addr fallback."""
    return request.headers.get("X-Real-IP") or request.remote_addr or "0.0.0.0"


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
