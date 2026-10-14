"""Payment routes: history and top-spender leaderboard (read the local payments table only)."""

import hashlib
import json
import sqlite3

from flask import request, Response, jsonify

from . import clubs, db, security, payments


def _payment_public_id(club_id: str, transaction_id: str) -> str:
    return hashlib.sha256(f"{club_id}|{transaction_id}".encode("utf-8")).hexdigest()[:12]


def api_payment_history():
    """Page the local payments table (synced from upstream, pseudonymised).

    Rows hold date, description, amount and status only; transaction ids never leave the DB."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()

    win = payments._parse_window_args()
    if len(win) == 2:
        return win
    club_id, start_ms, end_ms = win
    if club_id not in clubs.PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    try:
        limit = min(int(request.args.get("limit", "100")), payments.PAYMENTS_PAGE_SIZE)
        skip = int(request.args.get("skip", "0"))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and skip must be integers"}), 400
    if limit < 1 or skip < 0 or skip > payments.PAYMENTS_MAX_SKIP:
        return jsonify({"error": "invalid limit or skip"}), 400
    if not payments._payments_ready():
        return payments._payments_unavailable()

    sync = payments._window_sync_status(club_id, start_ms, end_ms)
    queued = False if sync["complete"] else payments._enqueue_payment_window(club_id, start_ms, end_ms)
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute(
                "SELECT created_at, description, amount, status, club_id, transaction_id FROM payments "
                "WHERE club_id=? AND created_utc BETWEEN ? AND ? "
                "ORDER BY created_utc DESC, transaction_id LIMIT ? OFFSET ?",
                (club_id, start_ms, end_ms, limit + 1, skip)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    has_more = len(rows) > limit
    items = [{"id": _payment_public_id(r[4], r[5]), "createdAt": r[0], "description": r[1],
              "amount": r[2], "status": r[3]} for r in rows[:limit]]
    resp = Response(
        json.dumps({"payments": items, "skip": skip, "limit": limit, "has_more": has_more,
                    "complete": sync["complete"], "synced_through": sync["synced_through"], "queued": queued}),
        status=200,
        content_type="application/json",
    )
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_payment_leaderboard():
    """Top spenders for a club+window, aggregated from the local payments table.
    Customers are anonymous ("Customer #XXXX"); the id is a prefix of an HMAC key."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    win = payments._parse_window_args()
    if len(win) == 2:
        return win
    club_id, start_ms, end_ms = win
    if club_id not in clubs.PAYMENT_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    if not payments._payments_ready():
        return payments._payments_unavailable()
    sync = payments._window_sync_status(club_id, start_ms, end_ms)
    queued = False if sync["complete"] else payments._enqueue_payment_window(club_id, start_ms, end_ms)
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute(
                "SELECT customer_key, amount, status FROM payments "
                "WHERE club_id=? AND created_utc BETWEEN ? AND ? AND status='PAYMENT_SUCCESS'",
                (club_id, start_ms, end_ms)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    resp = Response(json.dumps({"leaders": payments._aggregate_leaders(rows), "complete": sync["complete"],
                                "synced_through": sync["synced_through"], "queued": queued}),
                    status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


ROUTES = (
    ("/api/payment-history", api_payment_history, {"methods": ["GET"]}),
    ("/api/payment-leaderboard", api_payment_leaderboard, {"methods": ["GET"]}),
)
