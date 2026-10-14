"""Archive routes: Racketeer snapshots (served from the archive tables, never upstream)."""

import json
import sqlite3
from datetime import datetime

from flask import request, Response, jsonify

from . import clubs, privacy, db, security, payments, routes_payments


def api_archive_snapshots():
    """List distinct captured_at timestamps for a given archive table and club."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    table   = request.args.get("table", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if table not in ("heatmap", "club_info", "financial"):
        return jsonify({"error": "invalid table"}), 400
    try:
        conn = db._db_connect()
        try:
            if table == "financial":
                rows = conn.execute(
                    "SELECT DISTINCT captured_at FROM archive_financial"
                    " WHERE club_id=? AND endpoint='revenue-summary'"
                    " ORDER BY captured_at DESC LIMIT 50",
                    (club_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT DISTINCT captured_at FROM archive_{table} WHERE club_id=? ORDER BY captured_at DESC LIMIT 50",
                    (club_id,),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    snapshots = [
        {
            "captured_at": row[0],
            "label": datetime.utcfromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M UTC"),
        }
        for row in rows
    ]
    resp = Response(json.dumps(snapshots), status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_archive_heatmap():
    """Return archived heatmap snapshot in same shape as /api/heatmap."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    try:
        conn = db._db_connect()
        try:
            rows = conn.execute(
                "SELECT dow, hour, avg_occ, samples, hour_norm, dow_norm FROM archive_heatmap"
                " WHERE club_id=? AND captured_at=?",
                (club_id, captured_at),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    if not rows:
        return jsonify({"error": "no snapshot"}), 404
    buckets: dict = {}
    hour_signal: dict = {}
    dow_signal: dict = {}
    for dow, hour, avg_occ, _samples, hour_norm, dow_norm in rows:
        buckets.setdefault(str(dow), {})[str(hour)] = round(avg_occ, 4)
        # hour_norm/dow_norm are unset on snapshots archived before this data was captured
        if hour_norm is not None:
            hour_signal[str(hour)] = round(hour_norm, 4)
        if dow_norm is not None:
            dow_signal[str(dow)] = round(dow_norm, 4)
    payload = json.dumps({
        "club_id": club_id,
        "fetched_at": captured_at,
        "buckets": buckets,
        "hour_signal": hour_signal,
        "dow_signal": dow_signal,
    })
    resp = Response(payload, status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def api_archive_club_info():
    """Return archived club-info snapshot payload."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    try:
        conn = db._db_connect()
        try:
            row = conn.execute(
                "SELECT payload FROM archive_club_info WHERE club_id=? AND captured_at=? LIMIT 1",
                (club_id, captured_at),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    if not row:
        return jsonify({"error": "no snapshot"}), 404
    resp = Response(privacy._scrub_json_text(row[0]), status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


def _archive_payments_text(club_id: str, text: str) -> str:
    """Archived payment-history payload -> {payments: [{id, createdAt, description, amount, status}]}."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return json.dumps({"payments": []})
    if not isinstance(doc, dict):
        return json.dumps({"payments": []})
    records = doc.get("payments")
    rows = []
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        clean = payments._sanitise_payment(rec) if ("player_email" in rec or "player_name" in rec) else rec
        rows.append({
            "id": routes_payments._payment_public_id(club_id, payments._payment_txn_id(clean)),
            "createdAt": clean.get("createdAt"),
            "description": privacy._redact_text(str(clean.get("description") or "")),
            "amount": clean.get("amount"),
            "status": clean.get("status"),
        })
    other = privacy._scrub_pii({k: v for k, v in doc.items() if k != "payments"})
    other["payments"] = rows
    return json.dumps(other)


def api_archive_financial():
    """Return archived financial snapshot payload."""
    passed, via_query = security._gate()
    if not passed:
        return security._forbidden()
    club_id = request.args.get("club_id", "")
    if not club_id:
        return jsonify({"error": "club_id required"}), 400
    if club_id not in clubs.ARCHIVE_CLUB_IDS:
        return jsonify({"error": "club not allowed"}), 400
    try:
        captured_at = int(request.args.get("captured_at", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "captured_at must be an integer"}), 400
    endpoint = request.args.get("endpoint", "")
    if endpoint not in ("revenue-summary", "payment-history"):
        return jsonify({"error": "invalid endpoint"}), 400
    try:
        conn = db._db_connect()
        try:
            row = conn.execute(
                "SELECT payload FROM archive_financial WHERE club_id=? AND captured_at=? AND endpoint=? LIMIT 1",
                (club_id, captured_at, endpoint),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return security._exc_error(exc, 500)
    if not row:
        return jsonify({"error": "no snapshot"}), 404
    body = _archive_payments_text(club_id, row[0]) if endpoint == "payment-history" else privacy._scrub_json_text(row[0])
    resp = Response(body, status=200, content_type="application/json")
    if via_query:
        security._set_cookie(resp, via_query)
    return resp


ROUTES = (
    ("/api/archive/snapshots", api_archive_snapshots, {"methods": ["GET"]}),
    ("/api/archive/heatmap", api_archive_heatmap, {"methods": ["GET"]}),
    ("/api/archive/club-info", api_archive_club_info, {"methods": ["GET"]}),
    ("/api/archive/financial", api_archive_financial, {"methods": ["GET"]}),
)
