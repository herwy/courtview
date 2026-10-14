"""Payment sanitising, the local payments table sync engine and its on-demand queue."""

import hashlib
import random
import re
import sqlite3
import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import request, jsonify

from . import clubs, config, upstream, privacy, db, jobs


PAYMENTS_PAGE_SIZE        = 100
PAYMENTS_MAX_PAGES_PER_DAY = 200            # 20,000 payments/day safety cap
PAYMENTS_SYNC_WORKERS     = 3
PAYMENTS_PAGE_DELAY       = (0.1, 0.25)     # jitter between pages of one day
PAYMENTS_FRESH_SECS       = 600
PAYMENTS_LOOP_SECS        = 1800
PAYMENTS_MAX_WINDOW_MS    = 32 * 86400 * 1000
PAYMENTS_MAX_SKIP         = PAYMENTS_MAX_PAGES_PER_DAY * PAYMENTS_PAGE_SIZE
PAYMENTS_QUEUE_MAX = 16                  # queued on-demand payment windows

# Descriptions are kept only when their template (see _norm_desc) was shared by >= 5 distinct customers in
# the 2026-10-14 scan of the stored data (60,828 rows; 31 templates cover 60,740). Anything else, e.g. a
# description carrying a third party's name, is replaced by the payment's source_type.
DESCRIPTION_TEMPLATES = frozenset({
    "booked court of padium canary wharf: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of padium canary wharf: <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "booked court of padium canary wharf: <month> #, #### ##:## - ##:## <tz>",
    "booked court of padium canary wharf: <month> #, #### ##:## - ##:## <tz> by credit pay",
    "booked court of rocket ilford: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> ##, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "booked court of rocket padel ilford: <month> #, #### ##:## - ##:## <tz>",
    "booked court of rocket padel ilford: <month> #, #### ##:## - ##:## <tz> by credit pay",
    "booking in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "booking in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "match in rocket padel ilford - ####-##-## ##:## - ####-##-## ##:## <tz>",
    "match in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "match in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "tournament in padium canary wharf - <month> ##, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "tournament in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "tournament in rocket padel ilford - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "training in padium canary wharf - <month> ##, #### ##:## - ##:## <tz>",
    "training in padium canary wharf - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "training in padium canary wharf - <month> #, #### ##:## - ##:## <tz>",
    "training in padium canary wharf - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "training in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz>",
    "training in rocket padel ilford - <month> ##, #### ##:## - ##:## <tz> by credit pay",
    "training in rocket padel ilford - <month> #, #### ##:## - ##:## <tz>",
    "training in rocket padel ilford - <month> #, #### ##:## - ##:## <tz> by credit pay",
    "£## for padium canary wharf - <month> ##, #### ##:## <tz>",
    "£## for padium canary wharf - <month> #, #### ##:## <tz>",
    "£### for padium canary wharf - <month> ##, #### ##:## <tz>",
    "£### for padium canary wharf - <month> #, #### ##:## <tz>",
})

_MONTH_RE = re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b")
_TZ_RE = re.compile(r"\b(?:bst|gmt|utc)\b")


def _norm_desc(desc: str) -> str:
    """Template form of a description: digits, month names and timezone labels are abstracted."""
    t = _MONTH_RE.sub("<month>", desc.strip().lower())
    t = _TZ_RE.sub("<tz>", t)
    return re.sub(r"\d", "#", t)


def _clean_description(desc, name=None, email=None, phone=None) -> str:
    """Strip emails, phones and the customer's own name tokens from a payment description."""
    text = privacy._redact_text(str(desc or ""))
    for tok in {str(name or "").strip(), *str(name or "").split()}:
        if len(tok) >= 3:
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", "", text, flags=re.IGNORECASE)
    for raw in (email, phone):
        if raw and len(str(raw).strip()) >= 3:
            text = text.replace(str(raw).strip(), "")
    return re.sub(r"\s+", " ", text).strip()


def _final_description(cleaned: str, source_type) -> str:
    if DESCRIPTION_TEMPLATES is None:
        return cleaned
    if cleaned and _norm_desc(cleaned) in DESCRIPTION_TEMPLATES:
        return cleaned
    return str(source_type or "Payment")


def _player_field(rec: dict, field: str):
    """Read an upstream player_* field (name, email, phone) so it can be hashed or stripped, never stored."""
    return rec.get(f"player_{field}")


def _sanitise_payment(rec):
    """Allowlist one upstream payment record. Name, email, phone, receipt URL are discarded here."""
    if not isinstance(rec, dict):
        return None
    cleaned = _clean_description(rec.get("description"), _player_field(rec, "name"),
                                 _player_field(rec, "email"), _player_field(rec, "phone"))
    out = {
        "createdAt": rec.get("createdAt"),
        "customer_key": privacy._customer_key(_player_field(rec, "email")),
        "source_type": rec.get("source_type"),
        "description": _final_description(cleaned, rec.get("source_type")),
        "amount": rec.get("amount"),
        "status": rec.get("status"),
    }
    if rec.get("transaction_id"):
        out["transaction_id"] = str(rec["transaction_id"])
    return out


def _fetch_payment_page(club_id: str, start_ms, end_ms, skip: int, limit: int = PAYMENTS_PAGE_SIZE):
    """The only caller of online_payment_history. Returns sanitised records on 200, None otherwise.
    Every record is reduced by _sanitise_payment before returning, so no caller ever holds a name,
    email, phone or receipt URL. List length equals the upstream page length (paging relies on it).
    Exceptions (network, JSON) propagate to the caller."""
    r = upstream.UPSTREAM.get("/club/statistics/online_payment_history",
                     {"club_id": club_id, "start_datetime": start_ms, "end_datetime": end_ms,
                      "limit": limit, "skip": skip}, timeout=15)
    if r.status_code != 200:
        return None
    page = r.json()
    if not isinstance(page, list):
        return None
    return [_sanitise_payment(rec) or {} for rec in page]


def _payments_ready() -> bool:
    """Payments need the HMAC secret and the pseudonymised schema; otherwise nothing is stored."""
    if privacy._hmac_key() is None:
        return False
    conn = db._db_connect()
    try:
        return db._payments_schema_ok(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _payments_unavailable():
    return jsonify({"error": "payments unavailable"}), 503


def _parse_gbp_amount(raw) -> float | None:
    """Parse upstream amount strings like "40.00 GBP" -> 40.0. None if unparseable."""
    try:
        return float(str(raw).split()[0])
    except (ValueError, IndexError):
        return None


def _aggregate_leaders(rows: list) -> list:
    """Top 5 customers by successful spend. rows = [(customer_key, amount, status)].
    Output carries a short anonymous id only (first 4 hex of the HMAC key, upper-case)."""
    totals: dict = {}
    for customer_key, amount_raw, status in rows:
        if status != "PAYMENT_SUCCESS" or not customer_key:
            continue
        amount = _parse_gbp_amount(amount_raw)
        if amount is None:
            continue
        entry = totals.setdefault(customer_key, {"total": 0.0, "bookings": 0})
        entry["total"] += amount
        entry["bookings"] += 1

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1]["total"], kv[0]))[:5]
    max_total = ranked[0][1]["total"] if ranked else 0
    return [
        {"customer_id": ck[:4].upper(), "bookings": e["bookings"], "total": round(e["total"], 2),
         "pct": round(e["total"] / max_total * 100, 1) if max_total else 0}
        for ck, e in ranked
    ]


def _london_day(ms: int) -> str:
    """Epoch ms -> 'YYYY-MM-DD' in Europe/London."""
    return datetime.fromtimestamp(ms / 1000, tz=config._LONDON_TZ).strftime("%Y-%m-%d")


def _london_day_bounds(day: str) -> tuple:
    """'YYYY-MM-DD' -> (start_ms, next_day_start_ms) in Europe/London (DST-safe)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    start = datetime(d.year, d.month, d.day, tzinfo=config._LONDON_TZ)
    nxt_d = d + timedelta(days=1)
    nxt = datetime(nxt_d.year, nxt_d.month, nxt_d.day, tzinfo=config._LONDON_TZ)
    return int(start.timestamp() * 1000), int(nxt.timestamp() * 1000)


def _parse_created_utc(raw) -> int | None:
    """Upstream createdAt 'YYYY-MM-DD HH:MM:SS' (UTC) -> epoch ms, None if unparseable."""
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1000)


def _payment_txn_id(rec: dict) -> str:
    """Real upstream transaction id, else a synthetic id built from non-personal fields only."""
    tid = rec.get("transaction_id")
    if tid:
        return str(tid)
    basis = "|".join(str(rec.get(k)) for k in ("createdAt", "customer_key", "amount", "status"))
    return "synthetic:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _upsert_payments(conn, club_id: str, records: list, synced_at: int) -> int:
    """Upsert sanitised payment records. Returns the number of rows written."""
    n = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        created = _parse_created_utc(rec.get("createdAt"))
        if created is None:
            continue
        conn.execute(
            """INSERT INTO payments (club_id, transaction_id, created_utc, local_day, created_at, customer_key,
                   source_type, description, amount, amount_gbp, status, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(club_id, transaction_id) DO UPDATE SET
                   created_utc=excluded.created_utc, local_day=excluded.local_day,
                   created_at=excluded.created_at, customer_key=excluded.customer_key,
                   source_type=excluded.source_type, description=excluded.description,
                   amount=excluded.amount, amount_gbp=excluded.amount_gbp, status=excluded.status,
                   synced_at=excluded.synced_at""",
            (club_id, _payment_txn_id(rec), created, _london_day(created), rec.get("createdAt"),
             rec.get("customer_key"), rec.get("source_type"), rec.get("description"), rec.get("amount"),
             _parse_gbp_amount(rec.get("amount")), rec.get("status"), synced_at),
        )
        n += 1
    return n


def _day_needs_sync(row, day_close_ms: int, now_s: float) -> bool:
    """row = (synced_at, complete) or None. Implements the freshness/finality rule."""
    if row is None:
        return True
    synced_at, complete = row
    synced_at = synced_at or 0
    if now_s - synced_at < PAYMENTS_FRESH_SECS:
        return False
    final = bool(complete) and synced_at * 1000 >= day_close_ms + PAYMENTS_FRESH_SECS * 1000
    return not final


def _sync_payment_day(club_id: str, day: str) -> tuple:
    """Sync one London day. Returns (pages, rows, complete)."""
    if not _payments_ready():
        return (0, 0, 0)
    day_start, day_next = _london_day_bounds(day)
    key = f"paysync:{club_id}:{day}"
    with db._single_flight_lock(key):
        conn = db._db_connect()
        try:
            row = conn.execute(
                "SELECT synced_at, complete FROM payments_sync WHERE club_id=? AND local_day=?", (club_id, day)
            ).fetchone()
            if not _day_needs_sync(row, day_next, config._now()):
                return (0, 0, int(row[1]) if row else 0)
            pages = rows = 0
            complete = 0
            skip = 0
            while True:
                if pages >= PAYMENTS_MAX_PAGES_PER_DAY:
                    print(f"[payments] WARNING club={club_id[:8]} day={day} hit page cap", flush=True)
                    break
                try:
                    page = _fetch_payment_page(club_id, day_start, day_next - 1, skip, PAYMENTS_PAGE_SIZE)
                except Exception as exc:
                    print(f"[payments] club={club_id[:8]} day={day} fetch error: {type(exc).__name__}", flush=True)
                    page = None
                if page is None:
                    break
                pages += 1
                rows += _upsert_payments(conn, club_id, page, int(config._now()))
                conn.commit()
                if len(page) < PAYMENTS_PAGE_SIZE:
                    complete = 1
                    break
                skip += PAYMENTS_PAGE_SIZE
                time.sleep(random.uniform(*PAYMENTS_PAGE_DELAY))
            conn.execute(
                "INSERT OR REPLACE INTO payments_sync (club_id, local_day, synced_at, complete, pages, rows) "
                "VALUES (?,?,?,?,?,?)", (club_id, day, int(config._now()), complete, pages, rows))
            conn.commit()
            print(f"[payments] club={club_id[:8]} day={day} pages={pages} rows={rows} complete={complete}", flush=True)
            return (pages, rows, complete)
        finally:
            conn.close()


def _london_days(start_ms: int, end_ms: int) -> list:
    """London days ('YYYY-MM-DD') covering [start_ms, end_ms], with end clamped to now."""
    end_ms = min(int(end_ms), int(config._now() * 1000))
    if end_ms < start_ms:
        return []
    days = []
    d = _london_day(int(start_ms))
    last = _london_day(end_ms)
    while d <= last:
        days.append(d)
        d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return days


def _window_sync_status(club_id: str, start_ms: int, end_ms: int) -> dict:
    """Read-only sync state of a window: {"complete", "synced_through"}.

    complete: every London day in the window (clamped to now) has complete=1.
    synced_through: ISO 8601 Z of the oldest synced_at among non-final days (the window end when
    every day is final), clamped to min(end, now); None when any day has no sync row."""
    days = _london_days(start_ms, end_ms)
    if not days:
        return {"complete": True, "synced_through": None}
    conn = db._db_connect()
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT local_day, synced_at, complete FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ?",
            (club_id, days[0], days[-1]))}
    finally:
        conn.close()
    if any(d not in rows for d in days):
        return {"complete": False, "synced_through": None}
    # Final days are complete for good; only a non-final day limits how fresh the window is.
    open_days = []
    for d in days:
        synced_at, complete = rows[d]
        synced_ms = (synced_at or 0) * 1000
        final = bool(complete) and synced_ms >= _london_day_bounds(d)[1] + PAYMENTS_FRESH_SECS * 1000
        if not final:
            open_days.append(synced_ms)
    through = min(min(open_days) if open_days else int(end_ms), int(end_ms), int(config._now() * 1000))
    return {"complete": all(rows[d][1] == 1 for d in days),
            "synced_through": upstream._iso_z(through / 1000)}


_PAY_QUEUE: dict = {}
_PAY_QUEUE_LOCK = threading.Lock()
_PAY_WAKE = threading.Event()


def _enqueue_payment_window(club_id: str, start_ms: int, end_ms: int) -> bool:
    """Queue a window for the background loop. True when queued (or already queued); False for
    Racketeer, unknown clubs, empty windows and when the queue is full."""
    if club_id == clubs.RACKETEER_CLUB_ID or club_id not in clubs.PAYMENT_SYNC_CLUB_IDS:
        return False
    days = _london_days(start_ms, end_ms)
    if not days:
        return False
    key = (club_id, days[0], days[-1])
    with _PAY_QUEUE_LOCK:
        if key not in _PAY_QUEUE and len(_PAY_QUEUE) >= PAYMENTS_QUEUE_MAX:
            return False
        _PAY_QUEUE[key] = True
    _PAY_WAKE.set()
    return True


def _drain_payment_queue() -> int:
    """Sync every queued window (called from the payments loop thread). Returns windows synced."""
    with _PAY_QUEUE_LOCK:
        keys = list(_PAY_QUEUE)
        _PAY_QUEUE.clear()
    for club_id, first, last in keys:
        print(f"[payments] queued window club={club_id[:8]} days={first}..{last}", flush=True)
        try:
            _sync_payments(club_id, _london_day_bounds(first)[0], _london_day_bounds(last)[1] - 1)
        except Exception as exc:
            print(f"[payments] queued sync error club={club_id[:8]}: {type(exc).__name__}", flush=True)
    return len(keys)


def _sync_payments(club_id: str, start_ms: int, end_ms: int) -> dict:
    """Make sure every London day in [start_ms, end_ms] is synced (per the freshness rule)."""
    result = {"complete": False, "days": 0, "pages": 0, "rows": 0, "errors": 0}
    if club_id == clubs.RACKETEER_CLUB_ID or club_id not in clubs.PAYMENT_SYNC_CLUB_IDS:
        return result
    now_s = config._now()
    days = _london_days(start_ms, end_ms)
    if not days:
        return result
    conn = db._db_connect()
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT local_day, synced_at, complete FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ?",
            (club_id, days[0], days[-1]))}
    finally:
        conn.close()
    todo = [x for x in days if _day_needs_sync(rows.get(x), _london_day_bounds(x)[1], now_s)]
    t0 = config._now()
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PAYMENTS_SYNC_WORKERS) as pool:
            for pages, nrows, _c in pool.map(lambda x: _sync_payment_day(club_id, x), todo):
                result["pages"] += pages
                result["rows"] += nrows
        result["days"] = len(todo)
        print(f"[payments] sync club={club_id[:8]} days={len(todo)} pages={result['pages']} "
              f"rows={result['rows']} in {round(config._now() - t0, 1)}s", flush=True)
    conn = db._db_connect()
    try:
        done = conn.execute(
            "SELECT COUNT(*) FROM payments_sync WHERE club_id=? AND local_day BETWEEN ? AND ? AND complete=1",
            (club_id, days[0], days[-1])).fetchone()[0]
    finally:
        conn.close()
    result["complete"] = done == len(days)
    result["errors"] = len(days) - done
    return result


def _endpoint_calls(path: str) -> int:
    return upstream.UPSTREAM.snapshot()["endpoints"].get(path, {}).get("calls", 0)


def _payments_scheduled_run() -> None:
    """Sync previous + current London month for each live club; record state and status JSON."""
    started = int(config._now())
    calls0 = _endpoint_calls("/club/statistics/online_payment_history")
    now_ms = int(config._now() * 1000)
    today = _london_day(now_ms)
    prev = datetime.strptime(today[:7] + "-01", "%Y-%m-%d") - timedelta(days=1)
    start_ms = _london_day_bounds(prev.strftime("%Y-%m-") + "01")[0]
    errors = 0
    for club in clubs.HEATMAP_CLUBS:
        try:
            res = _sync_payments(club["id"], start_ms, now_ms)
            errors += res["errors"]
            print(f"[payments] scheduled club={club['id'][:8]} days={res['days']} "
                  f"pages={res['pages']} rows={res['rows']} complete={int(res['complete'])}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"[payments] loop error club={club['id'][:8]}: {type(exc).__name__}: {exc}", flush=True)
    calls = _endpoint_calls("/club/statistics/online_payment_history") - calls0
    last_ok = jobs._job_record("payments", started, errors == 0, calls, errors)
    jobs._write_job_status("payments", started, errors == 0, calls, errors, last_ok)


def _payments_sync_loop() -> None:
    """Drain on-demand windows queued by the routes, and run the scheduled prev+current-month pass
    every 30 min. The schedule is persisted in job_state (a restart does not re-run it)."""
    announced = False
    while True:
        _PAY_WAKE.clear()
        _drain_payment_queue()
        due = jobs._job_due_in("payments", PAYMENTS_LOOP_SECS, config.JOB_RETRY_SECS)
        if due <= 0:
            announced = False
            _payments_scheduled_run()
            continue
        if not announced:
            print(f"[payments] next run in {int(due // 60)}m (persisted)", flush=True)
            announced = True
        _PAY_WAKE.wait(min(due, 300))


def _parse_window_args():
    """Validate club_id/start/end. Returns (club_id, start_ms, end_ms) or (Response, status)."""
    club_id = request.args.get("club_id", "").strip()
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        start_ms = int(request.args.get("start", ""))
        end_ms = int(request.args.get("end", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "start and end must be integer ms"}), 400
    if end_ms < start_ms or end_ms - start_ms > PAYMENTS_MAX_WINDOW_MS:
        return jsonify({"error": "invalid window"}), 400
    return club_id, start_ms, end_ms
