"""PII scrubbing and HMAC pseudonymisation helpers."""

import hashlib
import hmac
import json
import os
import re
import threading

from . import config


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
        st = os.stat(config.HMAC_KEY_PATH)
        sig = (config.HMAC_KEY_PATH, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (config.HMAC_KEY_PATH, None, None)
    with _HMAC_LOCK:
        if _HMAC_STATE["sig"] == sig:
            return _HMAC_STATE["key"]
        key = None
        if sig[1] is not None:
            try:
                with open(config.HMAC_KEY_PATH, "rb") as fh:
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
