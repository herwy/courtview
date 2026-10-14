"""Availability routes: heatmap, court popularity, month batch and fresh availability."""

import json
import sqlite3
from datetime import datetime

from flask import request, Response, jsonify

from . import clubs, config, upstream, privacy, db, security, refresh


def api_heatmap():
    """Return pre-built DOW occupancy matrix from SQLite. Served from server-side cache."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute(
                "SELECT dow, hour, avg_occ, samples, fetched_at FROM heatmap_cache WHERE club_id=?",
                (club_id,),
            ).fetchall()
            hour_rows = conn.execute(
                "SELECT hour, norm FROM heatmap_hour_signal WHERE club_id=?", (club_id,)
            ).fetchall()
            dow_rows = conn.execute(
                "SELECT dow, norm FROM heatmap_dow_signal WHERE club_id=?", (club_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)

    if not rows:
        return jsonify({"club_id": club_id, "fetched_at": None, "buckets": {}, "hour_signal": {}, "dow_signal": {}}), 200

    buckets: dict = {}
    fetched_at = 0
    for dow, hour, avg_occ, samples, fa in rows:
        buckets.setdefault(str(dow), {})[str(hour)] = round(avg_occ, 4)
        if fa > fetched_at:
            fetched_at = fa

    hour_signal = {str(h): round(n, 4) for h, n in hour_rows}
    dow_signal  = {str(d): round(n, 4) for d, n in dow_rows}

    resp = Response(
        json.dumps({"club_id": club_id, "fetched_at": fetched_at,
                    "buckets": buckets, "hour_signal": hour_signal, "dow_signal": dow_signal}),
        status=200, content_type="application/json",
    )
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_court_popularity():
    """Per-court booking counts for a club, sorted desc with pct of max."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        conn = db._db_connect()
        rows = conn.execute(
            "SELECT court_name, count, fetched_at FROM court_popularity WHERE club_id=? ORDER BY count DESC",
            (club_id,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)

    if not rows:
        return jsonify({"club_id": club_id, "fetched_at": None, "courts": []}), 200

    max_count = max((r[1] for r in rows), default=0) or 1
    fetched_at = max((r[2] for r in rows), default=0)
    courts = [
        {"name": name, "count": int(count), "pct": round(count / max_count * 100, 1)}
        for name, count, _ in rows
    ]
    resp = Response(
        json.dumps({"club_id": club_id, "fetched_at": fetched_at, "courts": courts}),
        status=200, content_type="application/json",
    )
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_month():
    """Return cached availability for 28 days in one response — replaces 28 serial fetchDay calls."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400

    cutoff = int(config._now()) - config.CACHE_TTL
    today  = datetime.now(config._LONDON_TZ).date()

    date_params = refresh._london_day_keys(today)

    days: dict = {dp[0]: None for dp in date_params}
    conn = db._db_connect()
    try:
        # Single batched query: fetch all 28 days in one round-trip
        placeholders = ",".join(["(?,?)"] * len(date_params))
        flat_pairs   = [v for dp in date_params for v in (dp[1], dp[2])]
        rows = conn.execute(
            f"SELECT start_datetime, end_datetime, payload FROM availability"
            f" WHERE club_id=? AND fetched_at>?"
            f" AND (start_datetime, end_datetime) IN ({placeholders})",
            [club_id, cutoff] + flat_pairs,
        ).fetchall()
        # Build reverse lookup: (start_ms, end_ms) -> date_str
        pair_to_date = {(dp[1], dp[2]): dp[0] for dp in date_params}
        for start_dt, end_dt, payload_str in rows:
            date_str = pair_to_date.get((start_dt, end_dt))
            if date_str:
                days[date_str] = privacy._scrub_pii(json.loads(payload_str))
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    finally:
        conn.close()

    resp = Response(
        json.dumps({"club_id": club_id, "days": days}),
        status=200, content_type="application/json",
    )
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def _availability_row(club_id: str, start: str, end: str, max_age_s=None):
    """Stored availability payload, optionally only when fetched within max_age_s. None if absent."""
    sql = "SELECT payload FROM availability WHERE club_id=? AND start_datetime=? AND end_datetime=?"
    args = [club_id, start, end]
    if max_age_s is not None:
        sql += " AND fetched_at>=?"
        args.append(int(config._now()) - max_age_s)
    try:
        conn = db._db_connect()
        try:
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def api_availability_fresh():
    """Availability for one day, at most WATCH_FRESH_SECS old (watch notifications; the normal
    availability cache can be 6 h old). Live clubs and a window near now only, single-flight."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "").strip()
    if club_id not in clubs.PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    start = request.args.get("start_datetime", "")
    end = request.args.get("end_datetime", "")
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return jsonify({"error": "start_datetime and end_datetime must be integer ms"}), 400
    now_ms = int(config._now() * 1000)
    if not (now_ms - 36 * 3600000 <= start_i <= now_ms + 48 * 3600000) \
            or end_i < start_i or end_i - start_i > 26 * 3600000:
        return jsonify({"error": "window outside allowed range"}), 400

    def _respond(body: str, stale: bool = False):
        resp = Response(privacy._scrub_json_text(body), status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            security._set_cookie(resp, via_query)
        return resp

    fresh = _availability_row(club_id, start, end, config.WATCH_FRESH_SECS)
    if fresh is not None:
        return _respond(fresh)
    with db._single_flight_lock(f"availfresh:{club_id}:{start}:{end}"):
        fresh = _availability_row(club_id, start, end, config.WATCH_FRESH_SECS)
        if fresh is not None:
            return _respond(fresh)
        try:
            r = upstream.UPSTREAM.get(upstream.CACHED_PATH, [("club_id", club_id), ("start_datetime", start),
                                           ("end_datetime", end)], timeout=15)
            if r.status_code == 200:
                body = privacy._scrub_json_text(r.content.decode("utf-8", errors="replace"))
                try:
                    good = db._is_availability(json.loads(body))
                except ValueError:
                    good = False
                if good:
                    db.store_cached(club_id, start, end, body)
                    return _respond(body)
        except Exception:
            pass
        stale = _availability_row(club_id, start, end)
        if stale is not None:
            return _respond(stale, stale=True)
        return jsonify({"error": "upstream error"}), 502


ROUTES = (
    ("/api/heatmap", api_heatmap, {"methods": ["GET"]}),
    ("/api/court-popularity", api_court_popularity, {"methods": ["GET"]}),
    ("/api/month", api_month, {"methods": ["GET"]}),
    ("/api/availability-fresh", api_availability_fresh, {"methods": ["GET"]}),
)
