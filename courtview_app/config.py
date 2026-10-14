"""Runtime paths, credentials, cookie and timing constants. Reads the environment once at import."""

import os
from time import time as _now  # noqa: F401  (patched by tests as config._now; callers use config._now())
from zoneinfo import ZoneInfo

# Release root: the directory holding courtview.py, courtview.html and web/ (parent of this package)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


HEATMAP_HOURS      = list(range(7, 23))   # 07:00-22:00
HEATMAP_STALE_SECS = 24 * 3600           # refresh every 24h (1 API call per club)

# Background job schedule (persisted in job_state, so a restart neither resets nor re-runs a job)
AVAIL_REFRESH_SECS = 6 * 3600
JOB_RETRY_SECS     = 1800                # a failed run is retried after 30 min
WATCH_FRESH_SECS   = 300                 # /api/availability-fresh max cache age
ARCHIVED_ERROR    = "archived"
ARCHIVED_MESSAGE  = "Racketeer left Padelmates on 2026-07-01; only archived snapshots are available."

# Runtime paths on RPi. COURTVIEW_DATA_DIR drives the data files; a per-path env var wins.
# Secrets arrive via systemd LoadCredential ($CREDENTIALS_DIRECTORY/<name>), with the legacy
# /root source files as the last fallback.
DATA_DIR = os.environ.get("COURTVIEW_DATA_DIR", "/var/lib/courtview")


def _cred_path(env_name: str, cred_name: str, legacy: str) -> str:
    """Explicit env > $CREDENTIALS_DIRECTORY/<cred_name> > legacy path."""
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        return os.path.join(cred_dir, cred_name)
    return legacy


TOKEN_PATH      = _cred_path("COURTVIEW_TOKEN_PATH", "token", "/root/.courtview_token")
HMAC_KEY_PATH   = _cred_path("COURTVIEW_HMAC_KEY_PATH", "hmac_key", "/root/.courtview_hmac_key")
DB_PATH         = os.environ.get("COURTVIEW_DB_PATH", os.path.join(DATA_DIR, "courtview_cache.db"))
ACCESS_LOG_PATH = os.environ.get("COURTVIEW_ACCESS_LOG_PATH", os.path.join(DATA_DIR, "access.log"))
STATUS_DIR      = os.environ.get("COURTVIEW_STATUS_DIR", os.path.join(DATA_DIR, "status"))

# CourtView-owned access page assets (web/ next to this file)
WEB_ASSETS_DIR  = os.path.join(ROOT_DIR, "web")
DASHBOARD_HTML = os.path.join(ROOT_DIR, "courtview.html")

# Cookie name and max-age (90 days)
COOKIE_NAME    = "courtview_token"
COOKIE_MAX_AGE = 7776000
CACHE_TTL      = 28 * 86400  # 28-day availability cache TTL
_LONDON_TZ     = ZoneInfo("Europe/London")
