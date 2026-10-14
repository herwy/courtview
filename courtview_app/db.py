"""SQLite connection helper, schema, stats and availability caches, single-flight locks."""

import json
import os
import sqlite3
import threading

from . import config


def _db_connect() -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, synchronous=NORMAL, and a 10s busy timeout.
    Every sqlite3.connect call site in this module must route through here so the pragmas
    are applied consistently and connection settings live in one place."""
    conn = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA secure_delete=ON")
    return conn


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
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
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
    cutoff = int(config._now()) - STATS_CACHE_TTL
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
            (key, payload, int(config._now()))
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
    cutoff = int(config._now()) - config.CACHE_TTL
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
            (club_id, start, end, payload, int(config._now())),
        )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[cache] store error: {exc}")
    finally:
        conn.close()


# Per-key single-flight locks (stats cache, club-info, membership, availability, payment days)
_STATS_FETCH_LOCKS: dict = {}
_STATS_FETCH_MUTEX = threading.Lock()


def _single_flight_lock(key: str) -> threading.Lock:
    """One lock per key: concurrent misses queue up and re-check the cache under the lock."""
    with _STATS_FETCH_MUTEX:
        return _STATS_FETCH_LOCKS.setdefault(key, threading.Lock())
