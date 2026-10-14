#!/usr/bin/env python3
"""cv_purge_pii.py - one-off, idempotent purge of personal data from the CourtView cache DB.

Usage:
  python3 cv_purge_pii.py --check   read-only; prints what a purge would change.
                                    Exit 0 = purge needed, 1 = already clean.
  python3 cv_purge_pii.py           purge (service must be stopped; the script refuses otherwise).

Steps: rebuild `payments` without name/email/phone/receipt (adding an HMAC customer_key), sanitise
archived payment/club-info/financial payloads, scrub or delete cached availability, clear the stats,
club-info and membership caches, record a schema_meta marker, then secure-delete the freed pages with
wal_checkpoint(TRUNCATE) + VACUUM. Output is counts only, never values.
"""
import json
import os
import subprocess
import sys

os.environ.setdefault("COURTVIEW_SKIP_STARTUP", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import courtview as cv  # noqa: E402

EXIT_NEEDED, EXIT_CLEAN, EXIT_REFUSED = 0, 1, 2
CACHE_TABLES = ("membership_members_cache", "stats_cache", "club_info_cache")
_RAW_KEYS = ("player_name", "player_email", "player_phone", "receiptUrl")


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _payment_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(payments)")}


def _marker_present(conn) -> bool:
    if "schema_meta" not in _tables(conn):
        return False
    return conn.execute("SELECT 1 FROM schema_meta WHERE key='pii_purge'").fetchone() is not None


def _legacy_payments(conn) -> bool:
    if "payments" not in _tables(conn):
        return False
    return _payment_columns(conn) != set(cv._PAYMENTS_COLUMNS)


def needs_purge(conn) -> bool:
    return _legacy_payments(conn) or not _marker_present(conn)


def _sanitised_archive_payment(rec):
    """Allowlisted archive record. A synthetic upstream-less id never keeps its embedded email."""
    if not isinstance(rec, dict):
        return None
    raw = any(k in rec for k in _RAW_KEYS)
    tid = rec.get("transaction_id")
    if raw:
        src = dict(rec)
        if str(tid or "").startswith("synthetic:"):
            src.pop("transaction_id", None)
        clean = cv._sanitise_payment(src)
    else:
        clean = {k: rec.get(k) for k in ("createdAt", "customer_key", "source_type", "amount", "status")}
        clean["description"] = cv._redact_text(str(rec.get("description") or ""))
        if tid and not str(tid).startswith("synthetic:"):
            clean["transaction_id"] = str(tid)
    return clean


def _rewrite_payment_history(text):
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    records = doc.get("payments")
    rows = [r for r in (_sanitised_archive_payment(x) for x in (records if isinstance(records, list) else []))
            if r is not None]
    out = cv._scrub_pii({k: v for k, v in doc.items() if k != "payments"})
    out["payments"] = rows
    return json.dumps(out)


def _scrubbed_or_none(text):
    try:
        return json.dumps(cv._scrub_pii(json.loads(text)))
    except (ValueError, TypeError):
        return None


def plan_counts(conn) -> dict:
    """Read-only: how many rows each purge step would touch."""
    tables = _tables(conn)
    counts = {}
    if _legacy_payments(conn):
        counts["payments_rows_to_rebuild"] = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        counts["payments_synthetic_ids_to_rewrite"] = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE transaction_id LIKE 'synthetic:%'").fetchone()[0]
    else:
        counts["payments_rows_to_rebuild"] = 0
        counts["payments_synthetic_ids_to_rewrite"] = 0
    for label, table, where in (("archive_payment_history", "archive_financial", "WHERE endpoint='payment-history'"),
                                ("archive_other_financial", "archive_financial", "WHERE endpoint!='payment-history'"),
                                ("archive_club_info", "archive_club_info", "")):
        counts[label + "_payloads"] = (conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
                                       if table in tables else 0)
    changed = 0
    total = 0
    if "availability" in tables:
        for (payload,) in conn.execute("SELECT payload FROM availability"):
            total += 1
            try:
                doc = json.loads(payload or "")
            except (ValueError, TypeError):
                changed += 1  # unparseable payloads are deleted
                continue
            if cv._scrub_pii(doc) != doc:
                changed += 1
    counts["availability_rows"] = total
    counts["availability_rows_that_change"] = changed
    for t in CACHE_TABLES:
        counts[t + "_rows_to_delete"] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in tables else 0
    return counts


def rebuild_payments(conn) -> dict:
    stats = {"payments_rows_rebuilt": 0, "synthetic_ids_rewritten": 0, "rows_null_customer_key": 0,
             "payments_rows_dropped_on_collision": 0}
    if not _legacy_payments(conn):
        return stats
    conn.execute("DROP TABLE IF EXISTS payments_new")
    conn.execute(cv.PAYMENTS_DDL.replace("CREATE TABLE IF NOT EXISTS payments (", "CREATE TABLE payments_new ("))
    cur = conn.execute("SELECT * FROM payments")
    names = [d[0] for d in cur.description]
    for values in cur:
        r = dict(zip(names, values))
        rec = {"createdAt": r.get("created_at"), "player_name": r.get("player_name"),
               "player_email": r.get("player_email"), "player_phone": r.get("player_phone"),
               "source_type": r.get("source_type"), "description": r.get("description"),
               "amount": r.get("amount"), "status": r.get("status")}
        tid = str(r.get("transaction_id") or "")
        synthetic = tid.startswith("synthetic:")
        if tid and not synthetic:
            rec["transaction_id"] = tid
        clean = cv._sanitise_payment(rec)
        new_tid = cv._payment_txn_id(clean)
        if synthetic:
            stats["synthetic_ids_rewritten"] += 1
        if not clean.get("customer_key"):
            stats["rows_null_customer_key"] += 1
        cur2 = conn.execute(
            "INSERT OR IGNORE INTO payments_new (club_id, transaction_id, created_utc, local_day, created_at, "
            "customer_key, source_type, description, amount, amount_gbp, status, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["club_id"], new_tid, r["created_utc"], r["local_day"], r.get("created_at"), clean.get("customer_key"),
             clean.get("source_type"), clean.get("description"), clean.get("amount"), r.get("amount_gbp"),
             clean.get("status"), r.get("synced_at")))
        if cur2.rowcount:
            stats["payments_rows_rebuilt"] += 1
        else:
            stats["payments_rows_dropped_on_collision"] += 1
    conn.execute("DROP TABLE payments")
    conn.execute("ALTER TABLE payments_new RENAME TO payments")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_club_created ON payments (club_id, created_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_club_key ON payments (club_id, customer_key)")
    return stats


def rewrite_archives(conn) -> dict:
    stats = {"archive_payloads_rewritten": 0, "archive_rows_deleted": 0, "club_info_archives_scrubbed": 0,
             "availability_rows_scrubbed": 0, "availability_rows_deleted": 0}
    tables = _tables(conn)
    if "archive_financial" in tables:
        for rid, endpoint, payload in conn.execute("SELECT id, endpoint, payload FROM archive_financial").fetchall():
            new = (_rewrite_payment_history(payload or "") if endpoint == "payment-history"
                   else _scrubbed_or_none(payload or ""))
            if new is None:
                conn.execute("DELETE FROM archive_financial WHERE id=?", (rid,))
                stats["archive_rows_deleted"] += 1
            else:
                conn.execute("UPDATE archive_financial SET payload=? WHERE id=?", (new, rid))
                stats["archive_payloads_rewritten"] += 1
    if "archive_club_info" in tables:
        for rid, payload in conn.execute("SELECT id, payload FROM archive_club_info").fetchall():
            new = _scrubbed_or_none(payload or "")
            if new is None:
                conn.execute("DELETE FROM archive_club_info WHERE id=?", (rid,))
                stats["archive_rows_deleted"] += 1
            else:
                conn.execute("UPDATE archive_club_info SET payload=? WHERE id=?", (new, rid))
                stats["club_info_archives_scrubbed"] += 1
    if "availability" in tables:
        rows = conn.execute("SELECT club_id, start_datetime, end_datetime, payload FROM availability").fetchall()
        for club_id, start, end, payload in rows:
            new = _scrubbed_or_none(payload or "")
            key = (club_id, start, end)
            if new is None:
                conn.execute("DELETE FROM availability WHERE club_id=? AND start_datetime=? AND end_datetime=?", key)
                stats["availability_rows_deleted"] += 1
            else:
                conn.execute("UPDATE availability SET payload=? WHERE club_id=? AND start_datetime=? "
                             "AND end_datetime=?", (new,) + key)
                stats["availability_rows_scrubbed"] += 1
    return stats


def purge(conn) -> dict:
    conn.execute("PRAGMA secure_delete=ON")
    stats = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        stats.update(rebuild_payments(conn))
        stats.update(rewrite_archives(conn))
        deleted = 0
        for t in CACHE_TABLES:
            if t in _tables(conn):
                deleted += conn.execute(f"DELETE FROM {t}").rowcount
        stats["cache_rows_deleted"] = deleted
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('pii_purge', ?)",
                     (cv.datetime.now(cv.timezone.utc).isoformat(timespec="seconds"),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return stats


def _service_active() -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", "courtview"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "active"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    if not os.path.exists(cv.DB_PATH):
        print("nothing to purge: database does not exist")
        return EXIT_CLEAN
    conn = cv._db_connect()
    conn.isolation_level = None
    try:
        if check:
            needed = needs_purge(conn)
            print("purge needed" if needed else "clean")
            if needed:
                for k, v in sorted(plan_counts(conn).items()):
                    print(f"WOULD {k}={v}")
            return EXIT_NEEDED if needed else EXIT_CLEAN
        if cv._hmac_key() is None:
            print("refusing: HMAC key missing or short")
            return EXIT_REFUSED
        if "COURTVIEW_DB_PATH" not in os.environ and _service_active():
            print("refusing: courtview service is active (stop it with systemctl first)")
            return EXIT_REFUSED
        stats = purge(conn)
        for k, v in sorted(stats.items()):
            print(f"{k}={v}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
