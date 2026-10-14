#!/usr/bin/env python3
"""
cv_telegram.py - minimal Telegram sender for CourtView alerts (stdlib only, runs as root).

Vendored and trimmed so CourtView depends on nothing outside its own release. Used by
cv_notify_fail.py (notify-telegram@ and courtview-recovery@ units).

send_telegram(message, *, silent=False) tries the default route, then the eth0 device
(SO_BINDTODEVICE, needs root, for hosts whose policy routing breaks the default route). The
whole send is retried once after RETRY_DELAY_S. On final failure it logs one stderr line (the
exception type only, never the token or the message text) and appends the alert to the outbox,
which flush_outbox() resends oldest first. Entries older than OUTBOX_MAX_AGE_S are dropped.

State lives in STATE_DIR (env CV_NOTIFY_STATE_DIR, default /var/lib/courtview-notify, root-only).
The bot token is read per call: env CV_TELEGRAM_TOKEN_PATH, then $CREDENTIALS_DIRECTORY/telegram_token,
then /root/.telegram_token.
"""
import fcntl
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request

CHAT_ID = "6404996954"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
ETH0_FALLBACK_DEVICE = "eth0"
REQUEST_TIMEOUT_S = 10
RETRY_DELAY_S = 3
OUTBOX_MAX_AGE_S = 24 * 3600
FILE_MODE = 0o600
HTTP_CLIENT_ERROR_MIN = 400
HTTP_SERVER_ERROR_MIN = 500
HTTP_TOO_MANY_REQUESTS = 429
LEGACY_TOKEN_PATH = "/root/.telegram_token"
STATE_DIR = os.environ.get("CV_NOTIFY_STATE_DIR", "/var/lib/courtview-notify")


def _outbox_path():
    return os.path.join(STATE_DIR, "alert_outbox.json")


def atomic_write_json(path, obj, mode=FILE_MODE):
    """Write obj as JSON via a temp file in the same dir, fsync, os.replace, chmod."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class _DeviceBoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection forced onto one network interface (SO_BINDTODEVICE, root only)."""

    def __init__(self, host, device, **kwargs):
        super().__init__(host, **kwargs)
        self._device = device

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self._device.encode())
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port or 443))
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


class _DeviceBoundHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, device):
        super().__init__()
        self._device = device

    def https_open(self, req):
        return self.do_open(lambda host, **kw: _DeviceBoundHTTPSConnection(host, self._device, **kw), req)


def _do_request(req, *, device=None):
    """Execute req, optionally on one device. Returns the decoded JSON body."""
    if device:
        ctx = urllib.request.build_opener(_DeviceBoundHTTPSHandler(device)).open(req, timeout=REQUEST_TIMEOUT_S)
    else:
        ctx = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S)
    with ctx as resp:
        return json.loads(resp.read())


def _request_with_fallback(req):
    """Default route first; on a network-level failure retry on eth0. HTTPError is re-raised."""
    try:
        return _do_request(req)
    except urllib.error.HTTPError:
        raise
    except Exception:
        return _do_request(req, device=ETH0_FALLBACK_DEVICE)


def _token():
    """Return the bot token or None. Never logged."""
    candidates = []
    explicit = os.environ.get("CV_TELEGRAM_TOKEN_PATH")
    if explicit:
        candidates.append(explicit)
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        candidates.append(os.path.join(cred_dir, "telegram_token"))
    candidates.append(LEGACY_TOKEN_PATH)
    for path in candidates:
        try:
            with open(path) as fh:
                token = fh.read().strip()
        except OSError:
            continue
        if token:
            return token
    return None


def _attempt(message, silent, parse_mode):
    """One send attempt. Returns (ok, permanent). Logs the exception type only."""
    token = _token()
    if token is None:
        print("[cv_telegram] send failed: no token", file=sys.stderr)
        return False, False
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_notification": bool(silent),
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(TELEGRAM_API.format(token=token), data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        result = _request_with_fallback(req)
    except urllib.error.HTTPError as exc:
        print(f"[cv_telegram] send failed: HTTP {exc.code}", file=sys.stderr)
        permanent = (HTTP_CLIENT_ERROR_MIN <= exc.code < HTTP_SERVER_ERROR_MIN
                     and exc.code != HTTP_TOO_MANY_REQUESTS)
        return False, permanent
    except Exception as exc:
        print(f"[cv_telegram] send failed: {type(exc).__name__}", file=sys.stderr)
        return False, False
    if isinstance(result, dict) and result.get("ok"):
        return True, False
    print("[cv_telegram] send failed: API error", file=sys.stderr)
    return False, False


def _send_with_retry(message, silent, parse_mode):
    ok, permanent = _attempt(message, silent, parse_mode)
    if ok or permanent:
        return ok, permanent
    time.sleep(RETRY_DELAY_S)
    return _attempt(message, silent, parse_mode)


def _locked(fn):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(STATE_DIR, "alert_outbox.lock"), os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        os.close(fd)


def _load_outbox():
    try:
        with open(_outbox_path()) as fh:
            data = json.load(fh)
        return [e for e in data if isinstance(e, dict) and "message" in e] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _queue(message, silent):
    def add():
        entries = _load_outbox()
        entries.append({"message": message, "silent": bool(silent), "ts": time.time()})
        atomic_write_json(_outbox_path(), entries)
    try:
        _locked(add)
    except OSError as exc:
        print(f"[cv_telegram] could not queue alert: {type(exc).__name__}", file=sys.stderr)


def outbox_count():
    """Number of queued alerts."""
    return len(_load_outbox())


def send_telegram(message, *, silent=False, parse_mode="HTML"):
    """Send message. On total failure queue it in the outbox and return False."""
    ok, permanent = _send_with_retry(message, silent, parse_mode)
    if ok:
        return True
    if not permanent:
        _queue(message, silent)
    return False


def flush_outbox():
    """Resend queued alerts oldest first. Returns the number sent."""
    def run():
        entries = _load_outbox()
        if not entries:
            return 0
        now = time.time()
        fresh = [e for e in entries if now - float(e.get("ts", now)) <= OUTBOX_MAX_AGE_S]
        if len(fresh) != len(entries):
            print(f"[cv_telegram] dropped {len(entries) - len(fresh)} expired outbox alert(s)", file=sys.stderr)
        sent = 0
        remaining = list(fresh)
        for entry in fresh:
            ok, permanent = _send_with_retry(entry["message"], entry.get("silent", False), "HTML")
            if ok or permanent:
                remaining.remove(entry)
                sent += ok
            else:
                break
        if remaining != entries:
            atomic_write_json(_outbox_path(), remaining)
        return sent

    try:
        return _locked(run)
    except OSError as exc:
        print(f"[cv_telegram] outbox flush failed: {type(exc).__name__}", file=sys.stderr)
        return 0
