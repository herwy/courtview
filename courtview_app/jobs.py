"""Persisted background-job schedule (job_state) and the status JSON files."""

import json
import os
import sqlite3
import tempfile
import time

from . import clubs, config, upstream, db


def _job_seed(conn, job: str) -> int:
    """First-deploy last_run, derived from existing data so the first deploy does not re-run a job
    that just ran. 0 means "never ran" (due now)."""
    if job == "availability":
        row = conn.execute("SELECT MAX(fetched_at) FROM availability WHERE club_id != ?",
                           (clubs.RACKETEER_CLUB_ID,)).fetchone()
    elif job == "heatmap":
        oldest = []
        for club in clubs.HEATMAP_CLUBS:
            r = conn.execute("SELECT MIN(fetched_at) FROM heatmap_cache WHERE club_id=?", (club["id"],)).fetchone()
            oldest.append(r[0] if r and r[0] else 0)
        row = (min(oldest) if oldest else 0,)
    elif job == "payments":
        row = conn.execute("SELECT MAX(synced_at) FROM payments_sync").fetchone()
    else:
        row = (0,)
    return int(row[0]) if row and row[0] else 0


def _job_due_in(job: str, period_s: int, retry_s: int) -> float:
    """Seconds until the job is due (<= 0 means now). Seeds job_state from data when absent."""
    try:
        conn = db._db_connect()
        try:
            row = conn.execute("SELECT last_run, ok FROM job_state WHERE job=?", (job,)).fetchone()
            if row is None:
                seed = _job_seed(conn, job)
                if not seed:
                    return 0.0
                conn.execute("INSERT OR IGNORE INTO job_state (job, last_run, ok, calls, errors, updated_at, "
                             "last_ok) VALUES (?,?,?,?,?,?,?)", (job, seed, 1, 0, 0, int(config._now()), seed))
                conn.commit()
                row = (seed, 1)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0
    last_run, ok = row
    return max(0.0, (last_run or 0) + (period_s if ok else retry_s) - config._now())


def _job_record(job: str, started_at: int, ok: bool, calls: int, errors: int):
    """Persist a run. Returns last_ok (start of the latest ok run, carried over on a failed run;
    None when the job never succeeded or the write failed)."""
    try:
        conn = db._db_connect()
        try:
            row = conn.execute("SELECT last_ok FROM job_state WHERE job=?", (job,)).fetchone()
            last_ok = int(started_at) if ok else (row[0] if row else None)
            conn.execute("INSERT OR REPLACE INTO job_state (job, last_run, ok, calls, errors, updated_at, "
                         "last_ok) VALUES (?,?,?,?,?,?,?)",
                         (job, int(started_at), int(bool(ok)), int(calls), int(errors), int(config._now()), last_ok))
            conn.commit()
            return last_ok
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[{job}] job_state write error: {type(exc).__name__}", flush=True)
        return None


def _wait_for_job(job: str, period_s: int, retry_s: int) -> None:
    """Block until the job is due, polling job_state at most every 5 min. Logs once per wait."""
    announced = False
    while True:
        due = _job_due_in(job, period_s, retry_s)
        if due <= 0:
            return
        if not announced:
            print(f"[{job}] next run in {int(due // 60)}m (persisted)", flush=True)
            announced = True
        time.sleep(min(due, 300))


def _write_job_status(job: str, last_run: int, ok: bool, calls: int, errors: int, last_ok=None) -> None:
    """Atomically write STATUS_DIR/<job>.json (0644, dir 0755) for labs_heartbeat. Six keys, no
    personal data. UMask=0077 on the unit means the modes must be set explicitly. Never raises."""
    tmp = None
    try:
        os.makedirs(config.STATUS_DIR, exist_ok=True)
        os.chmod(config.STATUS_DIR, 0o755)
        fd, tmp = tempfile.mkstemp(dir=config.STATUS_DIR, prefix="." + job + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"job": job, "last_run": upstream._iso_z(last_run),
                       "last_ok": upstream._iso_z(last_ok) if last_ok else None, "ok": bool(ok),
                       "calls": int(calls), "errors": int(errors)}, fh)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), 0o644)
        os.replace(tmp, os.path.join(config.STATUS_DIR, job + ".json"))
        tmp = None
    except Exception as exc:
        print(f"[{job}] status write failed: {type(exc).__name__}", flush=True)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rewrite_status_files() -> None:
    """Rewrite availability.json and payments.json from job_state (skips jobs with no row), so a
    deploy publishes last_ok without waiting for the next run."""
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute("SELECT job, last_run, ok, calls, errors, last_ok FROM job_state "
                                "WHERE job IN ('availability', 'payments')").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    for job, last_run, ok, calls, errors, last_ok in rows:
        if last_run:
            _write_job_status(job, last_run, bool(ok), calls or 0, errors or 0, last_ok)


def _job_states() -> dict:
    """job_state rows as {job: {last_run, last_ok, ok, calls, errors}} (empty when the table is missing)."""
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute("SELECT job, last_run, ok, calls, errors, last_ok FROM job_state").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {r[0]: {"last_run": upstream._iso_z(r[1]) if r[1] else None,
                   "last_ok": upstream._iso_z(r[5]) if r[5] else None, "ok": bool(r[2]),
                   "calls": r[3], "errors": r[4]} for r in rows}
