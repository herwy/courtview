"""Club routes: membership, club info, activity, revenue summary and coach stats."""

import json
import sqlite3
import concurrent.futures
import threading

from flask import request, Response, jsonify

from . import clubs, config, upstream, privacy, db, security


def _counts_payload(club_id: str, raw: str) -> str:
    """Reduce any stored membership payload to {club_id, counts}; member lists are never returned."""
    try:
        counts = json.loads(raw).get("counts", {})
    except (ValueError, AttributeError, TypeError):
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    return json.dumps({"club_id": club_id, "counts": counts})


def api_membership_members():
    """Return per-plan member counts: { club_id, counts: { plan_id: int } }.

    Counts come from the plan listing's total_members field. The per-plan member lists
    (/club/membership/member) are never fetched, stored or returned."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if club_id not in clubs.PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400

    force_refresh = request.args.get("refresh") == "1"

    # Serve from cache if fresh (6h TTL)
    if not force_refresh:
        try:
            conn = db._db_connect()
            row = conn.execute(
                "SELECT payload FROM membership_members_cache WHERE club_id=? AND fetched_at>?",
                (club_id, int(config._now()) - 6 * 3600),
            ).fetchone()
            conn.close()
            if row:
                resp = Response(_counts_payload(club_id, row[0]), status=200, content_type="application/json")
                if via_query:
                    security._set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    def _respond(body: str, stale: bool = False):
        resp = Response(body, status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            security._set_cookie(resp, via_query)
        return resp

    with db._single_flight_lock("members:" + club_id):
        if not force_refresh:
            try:
                conn = db._db_connect()
                row = conn.execute(
                    "SELECT payload FROM membership_members_cache WHERE club_id=? AND fetched_at>?",
                    (club_id, int(config._now()) - 6 * 3600),
                ).fetchone()
                conn.close()
                if row:
                    return _respond(_counts_payload(club_id, row[0]))
            except sqlite3.Error:
                pass

        counts: dict = {}
        ok = False
        try:
            plans_r = upstream.UPSTREAM.get("/club/membership/", {"club_id": club_id}, timeout=15)
            if plans_r.status_code == 200:
                plans_raw = plans_r.json()
                # Upstream returns either a list directly or {"memberships": [...]}
                if isinstance(plans_raw, dict):
                    plans_raw = plans_raw.get("memberships") or []
                if isinstance(plans_raw, list):
                    ok = True
                    for plan in plans_raw:
                        if not isinstance(plan, dict):
                            continue
                        pid = plan.get("_id") or plan.get("id")
                        total = plan.get("total_members")
                        if pid and isinstance(total, int) and not isinstance(total, bool):
                            counts[str(pid)] = total
        except Exception:
            ok = False

        if not ok:
            # Failure is never cached: serve the last stored counts of any age, else 502.
            try:
                conn = db._db_connect()
                row = conn.execute(
                    "SELECT payload FROM membership_members_cache WHERE club_id=?", (club_id,)).fetchone()
                conn.close()
                if row:
                    return _respond(_counts_payload(club_id, row[0]), stale=True)
            except sqlite3.Error:
                pass
            return jsonify({"error": "upstream error"}), 502

        payload = json.dumps({"club_id": club_id, "counts": counts})
        if counts:
            try:
                conn = db._db_connect()
                conn.execute(
                    "INSERT OR REPLACE INTO membership_members_cache (club_id, payload, fetched_at) VALUES (?,?,?)",
                    (club_id, payload, int(config._now())),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass
        return _respond(payload)


def _club_info_stored(raw: str) -> str:
    """Stored club_info_cache payload, scrubbed, minus the retired legacy 'extras' key."""
    text = privacy._scrub_json_text(raw)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, dict) and "extras" in data:
        data.pop("extras", None)
        return json.dumps(data)
    return text


def api_club_info():
    """Concurrent fan-out for the Club Info tab: profile + memberships + credits in one call."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400

    force_refresh = request.args.get("refresh") == "1"
    if not force_refresh:
        try:
            conn = db._db_connect()
            row = conn.execute(
                "SELECT payload FROM club_info_cache WHERE club_id=? AND fetched_at>?",
                (club_id, int(config._now()) - 600),
            ).fetchone()
            conn.close()
            if row:
                resp = Response(_club_info_stored(row[0]), status=200, content_type="application/json")
                if via_query:
                    security._set_cookie(resp, via_query)
                return resp
        except sqlite3.Error:
            pass

    def _respond(body: str, stale: bool = False):
        resp = Response(body, status=200, content_type="application/json")
        if stale:
            resp.headers["X-CourtView-Stale"] = "1"
        if via_query:
            security._set_cookie(resp, via_query)
        return resp

    with db._single_flight_lock("clubinfo:" + club_id):
        if not force_refresh:
            try:
                conn = db._db_connect()
                row = conn.execute(
                    "SELECT payload FROM club_info_cache WHERE club_id=? AND fetched_at>?",
                    (club_id, int(config._now()) - 600),
                ).fetchone()
                conn.close()
                if row:
                    return _respond(_club_info_stored(row[0]))
            except sqlite3.Error:
                pass

        results: dict = {"profile": None, "memberships": None, "credits": None}
        good: dict = {}
        params = {"club_id": club_id}
        targets = [
            ("profile",     "/club/"),
            ("memberships", "/club/membership/"),
            ("credits",     "/club/creditpackage/"),
        ]

        def _fetch(key: str, path: str) -> None:
            good[key] = False
            try:
                r = upstream.UPSTREAM.get(path, params, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    results[key] = d
                    good[key] = True
            except Exception:
                results[key] = None

        threads = [threading.Thread(target=_fetch, args=(k, p), daemon=True) for k, p in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        payload = json.dumps(privacy._scrub_pii(results))
        if all(good.get(k) for k, _ in targets):
            try:
                conn = db._db_connect()
                conn.execute(
                    "INSERT OR REPLACE INTO club_info_cache (club_id, payload, fetched_at) VALUES (?,?,?)",
                    (club_id, payload, int(config._now())),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass
            return _respond(payload)

        # A sub-fetch failed: never cache the partial result. Prefer the last complete row.
        try:
            conn = db._db_connect()
            row = conn.execute("SELECT payload FROM club_info_cache WHERE club_id=?", (club_id,)).fetchone()
            conn.close()
            if row:
                return _respond(_club_info_stored(row[0]), stale=True)
        except sqlite3.Error:
            pass
        return _respond(payload)


UNAVAILABLE_ERROR = "unavailable"
UNAVAILABLE_MESSAGE = "No longer available from Padelmates"


def api_booked_hours():
    """Retired: Padelmates now requires auth for the booked-hours upstream and CourtView never
    authenticates. Kept (not deleted) so callers get a clear 410, not the proxy's 403."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    resp = jsonify({"error": UNAVAILABLE_ERROR, "message": UNAVAILABLE_MESSAGE})
    resp.status_code = 410
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_activity_summary():
    """Fetch filtered_activities for a range and return counts grouped by type (max 5 pages)."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = db._stats_key("activity-summary", club_id, start, end)
    if ck:
        cached = db.get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: security._set_cookie(resp, via_query)
            return resp
        # Stampede guard: only one thread fetches per key
        with db._single_flight_lock(ck):
            cached = db.get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: security._set_cookie(resp, via_query)
                return resp
            # Still a miss under lock - this thread does the fetch
            payload = _do_fetch_activity_summary(club_id, start, end, ck)
            if isinstance(payload, tuple):
                return payload  # error response
    else:
        payload = _do_fetch_activity_summary(club_id, start, end, "")
        if isinstance(payload, tuple):
            return payload

    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def _do_fetch_activity_summary(club_id: str, start: str, end: str, ck: str):
    """Fetch and aggregate activity summary from upstream. Returns payload str or error tuple.
    Falls back to a 14-day window if the full range times out (large clubs like Rocket)."""
    PAGE_SIZE = 100
    FALLBACK_MS = 14 * 24 * 60 * 60 * 1000

    def _attempt(s: str, e: str):
        counts: dict = {}
        total = 0
        coach_rates: dict = {}

        def _fetch_mix():
            return upstream.UPSTREAM.get("/home/activity/filtered_activities",
                                {"club_id": club_id, "start": s, "end": e, "limit": PAGE_SIZE}, timeout=12)

        def _fetch_training():
            return upstream.UPSTREAM.get("/home/activity/filtered_activities",
                                {"club_id": club_id, "start": s, "end": e, "activity_type": "Training",
                                 "limit": 100}, timeout=12)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_mix = ex.submit(_fetch_mix)
            f_train = ex.submit(_fetch_training)
            r  = f_mix.result()
            rt = f_train.result()

        if r.status_code != 200:
            # A failed mix call must not become "zero bookings": raise so the 14-day fallback runs.
            raise upstream.UpstreamError(f"filtered_activities status {r.status_code}")
        if r.status_code == 200:
            for act in r.json().get("activities", []):
                atype = act.get("activity_type") or "Other"
                btype = act.get("booking_type") or ""
                if atype == "Book Court":
                    key = "Open Match" if btype == "OPEN_MATCH" else "Private Booking"
                elif atype == "Match":
                    key = "Club Match"
                else:
                    key = atype
                counts[key] = counts.get(key, 0) + 1
                total += 1

        if rt.status_code == 200:
            for act in rt.json().get("activities", []):
                cid = act.get("coach_id")
                if not cid:
                    continue
                entry = coach_rates.setdefault(cid, {"rates": []})
                ppp = act.get("payment_per_person")
                if ppp is not None:
                    try:
                        entry["rates"].append(float(ppp))
                    except (TypeError, ValueError):
                        pass

        return counts, total, coach_rates, rt.status_code == 200

    used_start = start
    try:
        counts, total, coach_rates, train_ok = _attempt(start, end)
    except Exception:
        # Full range failed or timed out - retry with last 14 days
        fallback_start = str(int(end) - FALLBACK_MS)
        try:
            counts, total, coach_rates, train_ok = _attempt(fallback_start, end)
            used_start = fallback_start
        except Exception:
            return (jsonify({"error": "upstream error"}), 502)

    coach_rate_summary = {
        privacy._pseudonym("coach", cid): {
            "avg_rate": round(sum(d["rates"]) / len(d["rates"]), 2) if d["rates"] else None,
            "sample": len(d["rates"]),
        }
        for cid, d in coach_rates.items()
    }

    range_days = round((int(end) - int(used_start)) / (24 * 60 * 60 * 1000))
    payload = json.dumps({"total": total, "counts": counts, "coach_rates": coach_rate_summary, "range_days": range_days})
    if ck and train_ok:
        db.set_stats_cached(ck, payload)
    return payload


def _activity_view(act) -> dict:
    """Allowlist one activity for the court grid: timing, courts, type, spots and price only."""
    if not isinstance(act, dict):
        return {}
    courts = []
    for c in act.get("courts") or []:
        if isinstance(c, dict):
            courts.append({k: c.get(k) for k in ("id", "court_id", "name") if k in c})
    out = {"courts": courts}
    for k in ("start_datetime", "end_datetime", "activity_type", "booking_type",
              "no_of_participants", "total_capacity", "payment_per_person"):
        if k in act:
            out[k] = act[k]
    return out


def api_day_activities():
    """Return all court bookings for a club+day by paginating /home/activity/filtered_activities."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400

    PAGE_SIZE = 50
    MAX_PAGES = 20  # cap at 1000 activities to prevent runaway upstream
    all_activities: list = []
    skip = 0
    try:
        for page_num in range(MAX_PAGES):
            r = upstream.UPSTREAM.get("/home/activity/filtered_activities",
                             {"club_id": club_id, "start": start, "end": end, "limit": PAGE_SIZE, "skip": skip},
                             timeout=15)
            if r.status_code != 200:
                return jsonify({"error": "upstream error"}), 502
            data = r.json()
            page = data.get("activities", [])
            all_activities.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
    except Exception as exc:
        return security._exc_error(exc, 502)

    resp = Response(
        json.dumps({"club_id": club_id, "activities": [_activity_view(a) for a in all_activities]}),
        status=200,
        content_type="application/json",
    )
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_revenue_summary():
    """Fetch /club/statistics/financial/v2 for a given time window."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    club_id   = request.args.get("club_id", "")
    start     = request.args.get("start", "")
    end       = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = db._stats_key("revenue-summary", club_id, start, end)
    if ck:
        cached = db.get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: security._set_cookie(resp, via_query)
            return resp
        with db._single_flight_lock(ck):
            cached = db.get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: security._set_cookie(resp, via_query)
                return resp
            try:
                r = upstream.UPSTREAM.get("/club/statistics/financial/v2",
                                 {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
                payload = privacy._scrub_json_text(r.content.decode("utf-8", errors="replace"))
                if r.status_code == 200:
                    db.set_stats_cached(ck, payload)
            except Exception as exc:
                return security._exc_error(exc, 502)
    else:
        try:
            r = upstream.UPSTREAM.get("/club/statistics/financial/v2",
                             {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
            payload = privacy._scrub_json_text(r.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            return security._exc_error(exc, 502)

    if r.status_code != 200:
        return jsonify({"error": "upstream error"}), 502
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


_COACH_TYPES = ("group", "private", "course")


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _anonymise_coach_stats(data):
    """Coach names are personal data once tied to per-person revenue and occupancy.

    Coaches become "Coach 1..n" (ordered by sessions desc) with a pseudonymous coach_id; only numeric
    statistics are kept per coach. Everything else in the document goes through _scrub_pii."""
    if not isinstance(data, dict):
        return privacy._scrub_pii(data)
    totals: dict = {}
    for t in _COACH_TYPES:
        block = data.get(t)
        if isinstance(block, dict):
            for c in block.get("coach_statistics") or []:
                if isinstance(c, dict):
                    cid = str(c.get("coach_id"))
                    totals[cid] = totals.get(cid, 0.0) + _num(c.get("total_sessions"))
    order = sorted(totals, key=lambda cid: (-totals[cid], privacy._pseudonym("coach", cid)))
    labels = {cid: f"Coach {i + 1}" for i, cid in enumerate(order)}
    rest = {}
    for k, v in data.items():
        if k in _COACH_TYPES and isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk != "coach_statistics"}
        rest[k] = v
    out = privacy._scrub_pii(rest)
    for t in _COACH_TYPES:
        block, orig = out.get(t), data.get(t)
        if isinstance(block, dict) and isinstance(orig, dict) and "coach_statistics" in orig:
            rows = []
            for c in orig.get("coach_statistics") or []:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("coach_id"))
                row = {k: v for k, v in c.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
                row["coach_id"] = privacy._pseudonym("coach", cid)
                row["coach_name"] = labels[cid]
                rows.append(row)
            rows.sort(key=lambda r: r["coach_name"].split()[1].zfill(4))
            block["coach_statistics"] = rows
    return out


def _coach_stats_payload(text: str) -> str:
    try:
        return json.dumps(_anonymise_coach_stats(json.loads(text)))
    except (ValueError, TypeError):
        return privacy._redact_text(text)


def api_coach_stats():
    """Proxy /club/statistics/coach with the 6h stats cache."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    start   = request.args.get("start", "")
    end     = request.args.get("end", "")
    if not club_id or not start or not end:
        return jsonify({"error": "club_id, start and end required"}), 400
    ck = db._stats_key("coach-stats", club_id, start, end)
    if ck:
        cached = db.get_stats_cached(ck)
        if cached:
            resp = Response(cached, status=200, content_type="application/json")
            if via_query: security._set_cookie(resp, via_query)
            return resp
        with db._single_flight_lock(ck):
            cached = db.get_stats_cached(ck)
            if cached:
                resp = Response(cached, status=200, content_type="application/json")
                if via_query: security._set_cookie(resp, via_query)
                return resp
            try:
                r = upstream.UPSTREAM.get("/club/statistics/coach",
                                 {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
                payload = _coach_stats_payload(r.content.decode("utf-8", errors="replace"))
                if r.status_code == 200:
                    db.set_stats_cached(ck, payload)
            except Exception as exc:
                return security._exc_error(exc, 502)
    else:
        try:
            r = upstream.UPSTREAM.get("/club/statistics/coach",
                             {"club_ids": club_id, "start_time": start, "end_time": end}, timeout=15)
            payload = _coach_stats_payload(r.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            return security._exc_error(exc, 502)
    if r.status_code != 200:
        return jsonify({"error": "upstream error"}), 502
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


ROUTES = (
    ("/api/membership-members", api_membership_members, {"methods": ["GET"]}),
    ("/api/club-info", api_club_info, {"methods": ["GET"]}),
    ("/api/booked-hours", api_booked_hours, {"methods": ["GET"]}),
    ("/api/activity-summary", api_activity_summary, {"methods": ["GET"]}),
    ("/api/day-activities", api_day_activities, {"methods": ["GET"]}),
    ("/api/revenue-summary", api_revenue_summary, {"methods": ["GET"]}),
    ("/api/coach-stats", api_coach_stats, {"methods": ["GET"]}),
)
