---
phase: quick
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.py
autonomous: true
requirements:
  - add-background-archive-refresh-thread

must_haves:
  truths:
    - "_archive_refresh_loop runs every 24h after a 60s startup delay"
    - "Each cycle appends one club_info row to archive_club_info for RACKETEER_CLUB_ID"
    - "Each cycle appends one financial row to archive_financial for RACKETEER_CLUB_ID (current month window)"
    - "Exceptions are caught per-fetch and logged with [archive-refresh] prefix; the loop continues"
    - "Thread is started as daemon=True from _startup()"
  artifacts:
    - path: courtview.py
      provides: "_archive_refresh_loop function and thread start in _startup()"
      contains: "_archive_refresh_loop"
  key_links:
    - from: "_archive_refresh_loop"
      to: "archive_club_info"
      via: "sqlite INSERT"
      pattern: "archive_club_info"
    - from: "_archive_refresh_loop"
      to: "archive_financial"
      via: "sqlite INSERT"
      pattern: "archive_financial"
---

<objective>
Add a background daemon thread that appends a daily snapshot of Racketeer's club info and current-month revenue to the archive tables.

Purpose: build a time-series archive of Racketeer data without manual intervention, enabling offline analysis and trend viewing from previously captured data.
Output: new function _archive_refresh_loop() in courtview.py, started as daemon thread in _startup().
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@/Users/herwy/dev/courtview/CLAUDE.md
@/Users/herwy/dev/courtview/.planning/STATE.md
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add _archive_refresh_loop and start the daemon thread</name>
  <files>courtview.py</files>
  <action>
Insert the new function _archive_refresh_loop() immediately after _heatmap_refresh_loop() ends (around line 2463, before the "Background cache refresh thread" comment block). Then start it in _startup().

Function structure:

ARCHIVE_REFRESH_INITIAL_DELAY = 60        # seconds
ARCHIVE_REFRESH_CYCLE_SECS = 24 * 3600   # 24 hours

def _archive_refresh_loop() -> None:
    """60s after startup, then every 24h: snapshot Racketeer club info + revenue into archive tables."""
    time.sleep(ARCHIVE_REFRESH_INITIAL_DELAY)
    while True:
        # --- club info ---
        try:
            paths = [
                f"/club/?club_id={RACKETEER_CLUB_ID}",
                f"/club/membership/?club_id={RACKETEER_CLUB_ID}",
                f"/club/creditpackage/?club_id={RACKETEER_CLUB_ID}",
                f"/club/club_extras?club_id={RACKETEER_CLUB_ID}",
            ]
            results = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(cffi_requests.get, TARGET + p, headers=APP_HEADERS, timeout=15, impersonate="chrome110"): p for p in paths}
                for fut in concurrent.futures.as_completed(futs):
                    path = futs[fut]
                    try:
                        r = fut.result()
                        results[path] = r.json() if r.status_code == 200 else {"error": r.status_code}
                    except Exception as exc:
                        results[path] = {"error": str(exc)}
            payload = json.dumps(results)
            conn = _db_connect()
            conn.execute(
                "INSERT INTO archive_club_info (club_id, payload, captured_at) VALUES (?, ?, ?)",
                (RACKETEER_CLUB_ID, payload, int(_now())),
            )
            conn.commit()
            conn.close()
            print(f"[archive-refresh] club_info snapshot saved for {RACKETEER_CLUB_ID}")
        except Exception as exc:
            print(f"[archive-refresh] club_info error: {exc}")

        # --- revenue ---
        try:
            now_dt = datetime.datetime.utcnow()
            month_start = datetime.datetime(now_dt.year, now_dt.month, 1)
            start_ms = int(month_start.timestamp() * 1000)
            end_ms = int(_now() * 1000)
            rev_path = f"/club/statistics/financial/v2?club_ids={RACKETEER_CLUB_ID}&start_time={start_ms}&end_time={end_ms}"
            r = cffi_requests.get(TARGET + rev_path, headers=APP_HEADERS, timeout=15, impersonate="chrome110")
            payload = r.text if r.status_code == 200 else json.dumps({"error": r.status_code})
            conn = _db_connect()
            conn.execute(
                "INSERT INTO archive_financial (club_id, start_time, end_time, payload, endpoint, captured_at) VALUES (?, ?, ?, ?, ?, ?)",
                (RACKETEER_CLUB_ID, str(start_ms), str(end_ms), payload, "revenue-summary", int(_now())),
            )
            conn.commit()
            conn.close()
            print(f"[archive-refresh] revenue snapshot saved for {RACKETEER_CLUB_ID} ({start_ms}-{end_ms})")
        except Exception as exc:
            print(f"[archive-refresh] revenue error: {exc}")

        time.sleep(ARCHIVE_REFRESH_CYCLE_SECS)

In _startup(), after the heatmap thread lines (after line 2590), add:

    archive_thread = threading.Thread(target=_archive_refresh_loop, daemon=True)
    archive_thread.start()
    print("[startup] archive refresh thread started (60s delay, 24h cycle, Racketeer only)")

Constants ARCHIVE_REFRESH_INITIAL_DELAY and ARCHIVE_REFRESH_CYCLE_SECS go near the top of the file with the other cycle/TTL constants (grep for HEATMAP_STALE_SECS to find the right zone).

Do NOT modify any existing request handlers, cache logic, or other threads. The only touched file is courtview.py.
  </action>
  <verify>
    <automated>cd /Users/herwy/dev/courtview && grep -c "_archive_refresh_loop" courtview.py</automated>
  </verify>
  <done>
    - _archive_refresh_loop() function present in courtview.py
    - Thread started in _startup() with daemon=True
    - cv-deploy succeeds
    - rpi 'grep -c "_archive_refresh_loop" /root/projects/courtview/courtview.py' returns >= 2
  </done>
</task>

<task type="auto">
  <name>Task 2: Deploy and verify</name>
  <files></files>
  <action>
Run cv-deploy. Then verify the function landed on the RPi.
  </action>
  <verify>
    <automated>cv-deploy && rpi 'grep -c "_archive_refresh_loop" /root/projects/courtview/courtview.py'</automated>
  </verify>
  <done>grep count on RPi is >= 2 (function definition + thread start + print call)</done>
</task>

</tasks>

<verification>
Local: grep -c "_archive_refresh_loop" courtview.py >= 3 (definition, thread start, print)
Remote: rpi 'grep -c "_archive_refresh_loop" /root/projects/courtview/courtview.py' >= 3
Remote: rpi 'pgrep -fa "gunicorn.*courtview"' shows gunicorn running after deploy
</verification>

<success_criteria>
- _archive_refresh_loop() exists in courtview.py
- Thread is started as daemon=True in _startup()
- Function appends to archive_club_info and archive_financial for RACKETEER_CLUB_ID only
- 60s initial delay, 24h cycle
- Each fetch exception is caught and logged with [archive-refresh] prefix
- Deployed to RPi and verified with grep
</success_criteria>

<output>
Create .planning/quick/260612-wym-add-background-archive-refresh-thread-fo/260612-wym-SUMMARY.md when done
</output>
