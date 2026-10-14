"""Start-up sequence: schema, status files, web assets and the background threads."""

import json
import threading

from . import clubs, config, upstream, db, jobs, accesslog, security, payments, refresh


_STARTUP_LOCK = threading.Lock()
_STARTUP_DONE = False


def _startup() -> None:
    """Initialise DB, load assets, open access log, start refresh threads.
    Called once at module import time so gunicorn (which never runs __main__) picks it up.
    Safe to call multiple times: init_db is idempotent; thread start is guarded by a module flag."""
    global _STARTUP_DONE
    with _STARTUP_LOCK:
        if _STARTUP_DONE:
            return
        _STARTUP_DONE = True

    print("[startup] courtview starting (bind address set by gunicorn ExecStart in courtview.service)")
    print(f"[startup] UA profile  : Android OkHttp (from APK analysis)")
    print(f"[startup] User-Agent  : {upstream.ANDROID_UA}")
    print(f"[startup] DB path     : {config.DB_PATH}")

    db.init_db()
    jobs._rewrite_status_files()
    security._load_dashboard()
    accesslog._load_web_assets()

    # Open access log file and pre-load recent entries into memory
    try:
        accesslog._ACCESS_FD = accesslog._open_access_log()
        try:
            with open(config.ACCESS_LOG_PATH, "r") as _fh:
                _lines = _fh.readlines()
            for _line in _lines[-500:]:
                try:
                    accesslog._ACCESS_LOG.append(json.loads(_line.strip()))
                except (json.JSONDecodeError, ValueError):
                    pass
            _tmp = list(accesslog._ACCESS_LOG)
            accesslog._ACCESS_LOG.clear()
            for _e in reversed(_tmp):
                accesslog._ACCESS_LOG.appendleft(_e)
            print(f"[startup] access log: loaded {len(accesslog._ACCESS_LOG)} entries from {config.ACCESS_LOG_PATH}")
        except FileNotFoundError:
            pass
    except OSError as _exc:
        print(f"[startup] WARNING: could not open access log: {_exc}")

    refresh_thread = threading.Thread(target=refresh._refresh_loop, daemon=True)
    refresh_thread.start()
    print("[startup] availability refresh thread started (6h cycle, persisted)")

    heatmap_thread = threading.Thread(target=refresh._heatmap_refresh_loop, daemon=True)
    heatmap_thread.start()
    payments_thread = threading.Thread(target=payments._payments_sync_loop, daemon=True)
    payments_thread.start()
    print("[startup] payments sync thread started")
    print(f"[startup] heatmap refresh thread started ({len(clubs.HEATMAP_CLUBS)} clubs, 24h cycle)")

    print("[startup] archive refresh thread disabled (Racketeer migrated off Padelmates)")
