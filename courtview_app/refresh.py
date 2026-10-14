"""Background refresh loops: availability window and the heatmap."""

import random
import sqlite3
import time
from datetime import datetime, timedelta

from . import clubs, config, upstream, privacy, db, jobs


def _london_day_keys(today, n: int = 28) -> list:
    """[(date_str, start_ms, end_ms)] for n London days from `today` (the keys /api/month reads)."""
    keys = []
    for i in range(n):
        d = today + timedelta(days=i)
        start_ms = int(datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=config._LONDON_TZ).timestamp() * 1000)
        end_ms = int(datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=config._LONDON_TZ).timestamp() * 1000)
        keys.append((str(d), str(start_ms), str(end_ms)))
    return keys


def _availability_refresh_targets(now_s: float) -> list:
    """(club_id, start_ms, end_ms) to refresh: the 28 London days today..today+27 for each live
    club, plus any other existing non-Racketeer row inside that window. Never past days."""
    today = datetime.fromtimestamp(now_s, config._LONDON_TZ).date()
    keys = _london_day_keys(today)
    lo = int(keys[0][1])
    hi = int(_london_day_keys(today + timedelta(days=28), 1)[0][1])
    targets: dict = {}
    for club in clubs.HEATMAP_CLUBS:
        if club["id"] == clubs.RACKETEER_CLUB_ID:
            continue
        for _d, start, end in keys:
            targets[(club["id"], start, end)] = None
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute(
                "SELECT club_id, start_datetime, end_datetime FROM availability "
                "WHERE club_id != ? AND CAST(start_datetime AS INTEGER) >= ? AND CAST(start_datetime AS INTEGER) < ?",
                (clubs.RACKETEER_CLUB_ID, lo, hi)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] db read error: {exc}", flush=True)
        rows = []
    for club_id, start, end in rows:
        targets.setdefault((club_id, start, end), None)
    return list(targets)


def _purge_availability(now_s: float) -> int:
    """Delete availability rows that end before today's London midnight, rows with non-numeric
    keys and every Racketeer row. Returns the number of rows deleted."""
    today = datetime.fromtimestamp(now_s, config._LONDON_TZ).date()
    midnight = int(_london_day_keys(today, 1)[0][1])
    try:
        conn = db._db_connect()
        try:
            cur = conn.execute(
                "DELETE FROM availability WHERE club_id=? OR end_datetime IS NULL "
                "OR CAST(end_datetime AS INTEGER) < ?", (clubs.RACKETEER_CLUB_ID, midnight))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] purge error: {exc}", flush=True)
        return 0


_HEATMAP_ARRAYS = ("hottest_time_slots", "week_day_wise_activity_count_combined_graph",
                   "court_wise_activity_count_combined_graph")


def _fetch_heatmap_for_club(club_id: str, n_courts: int) -> bool:
    """One call to /club/statistics/operational; build DOW x hour matrix from hottest_time_slots
    (hour-of-day signal) × week_day_wise_activity_count_combined_graph (DOW signal)."""
    print(f"[heatmap] fetching operational stats for club {club_id}")
    now_ms   = int(config._now() * 1000)
    start_ms = now_ms - 30 * 24 * 3600 * 1000  # 30 days
    try:
        r    = upstream.UPSTREAM.get("/club/statistics/operational",
                            {"club_ids": club_id, "selected_duration": "month", "start_time": start_ms,
                             "end_time": now_ms, "user_timezone": "Europe/London"}, timeout=15)
        data = r.json()
    except Exception as exc:
        print(f"[heatmap] fetch error for club {club_id}: {type(exc).__name__}")
        return False

    # Validate before any DB write: an error shape or empty payload must never overwrite real data.
    if not isinstance(data, dict) or "detail" in data:
        print(f"[heatmap] API error for club {club_id}")
        return False
    if r.status_code != 200 or any(not isinstance(data.get(k), list) for k in _HEATMAP_ARRAYS) \
            or not any(data[k] for k in _HEATMAP_ARRAYS):
        print(f"[heatmap] invalid payload for club {club_id} (status {r.status_code}), nothing written")
        return False

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
    now_ts = int(config._now())
    conn = db._db_connect()
    try:
        # Product matrix
        for dow in range(7):
            dow_w = dow_counts.get(dow, 0.0) / max_dow
            for hr in config.HEATMAP_HOURS:
                hr_w = hour_counts.get(hr, 0.0) / max_hour
                occ  = round(dow_w * hr_w, 4)
                conn.execute(
                    "INSERT OR REPLACE INTO heatmap_cache (club_id, dow, hour, avg_occ, samples, fetched_at) VALUES (?,?,?,?,?,?)",
                    (club_id, dow, hr, occ, 30, now_ts),
                )
        # Raw hour signal (all hours 0-23 for completeness, clipped to HEATMAP_HOURS)
        for hr in config.HEATMAP_HOURS:
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
        return True
    except sqlite3.Error as exc:
        print(f"[heatmap] db write error: {exc}")
        return False
    finally:
        conn.close()


def _heatmap_refresh_loop() -> None:
    """Refresh every club once per 24h. The schedule lives in job_state, so a restart neither
    resets nor immediately re-runs it; a failed run is retried after 30 min."""
    while True:
        jobs._wait_for_job("heatmap", config.HEATMAP_STALE_SECS, config.JOB_RETRY_SECS)
        started = int(config._now())
        calls = errors = 0
        for club in clubs.HEATMAP_CLUBS:
            calls += 1
            try:
                good = _fetch_heatmap_for_club(club["id"], club["courts"])
            except Exception as exc:
                print(f"[heatmap] refresh failed for club {club['id']}: {type(exc).__name__}")
                good = False
            if not good:
                errors += 1
        jobs._job_record("heatmap", started, errors == 0, calls, errors)


def _availability_cycle() -> tuple:
    """One refresh cycle: purge, then fetch London days today..today+27 for the live clubs.
    Returns (ok, attempted, refreshed). ok is False when the breaker stopped the cycle or at
    least half the attempts failed (it is then retried after 30 min)."""
    t0 = config._now()
    purged = _purge_availability(t0)
    targets = _availability_refresh_targets(t0)
    refreshed = errors = 0
    attempted = 0
    aborted = False
    for club_id, start, end in targets:
        params = [("club_id", club_id)]
        if start:
            params.append(("start_datetime", start))
        if end:
            params.append(("end_datetime", end))
        attempted += 1
        try:
            r = upstream.UPSTREAM.get(upstream.CACHED_PATH, params, timeout=15)
            if r.status_code == 200:
                db.store_cached(club_id, start, end,
                             privacy._scrub_json_text(r.content.decode("utf-8", errors="replace")))
                refreshed += 1
            else:
                errors += 1
        except upstream.UpstreamUnavailable:
            errors += 1
            aborted = True
            break
        except Exception as exc:
            errors += 1
            print(f"[refresh] fetch error: {type(exc).__name__}", flush=True)
        time.sleep(random.uniform(0.05, 0.18))

    ok = not aborted and errors * 2 <= attempted
    print(f"[refresh] {refreshed}/{len(targets)} refreshed, purged {purged} in {round(config._now() - t0, 1)}s", flush=True)

    # Periodically evict stale stats_cache entries (no TTL enforced at write time)
    stats_cutoff = int(config._now()) - db.STATS_CACHE_TTL
    try:
        conn = db._db_connect()
        try:
            conn.execute("DELETE FROM stats_cache WHERE fetched_at < ?", (stats_cutoff,))
            conn.commit()
            print("[refresh] stats_cache stale entries evicted")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[refresh] stats_cache cleanup error: {exc}")

    last_ok = jobs._job_record("availability", int(t0), ok, attempted, errors)
    jobs._write_job_status("availability", int(t0), ok, attempted, errors, last_ok)
    return ok, attempted, refreshed


def _refresh_loop() -> None:
    """Every 6 hours (persisted in job_state), refresh availability for today..today+27 only."""
    purged = _purge_availability(config._now())
    print(f"[refresh] start-up purge removed {purged} past or archived rows", flush=True)
    while True:
        jobs._wait_for_job("availability", config.AVAIL_REFRESH_SECS, config.JOB_RETRY_SECS)
        try:
            _availability_cycle()
        except Exception as exc:
            print(f"[refresh] cycle failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(config.JOB_RETRY_SECS)
