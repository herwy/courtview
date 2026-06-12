---
phase: quick
plan: 260612-wym
subsystem: courtview-backend
tags: [background-thread, archive, sqlite, daemon]
key-files:
  modified:
    - courtview.py
decisions:
  - Use datetime.utcnow() not datetime.datetime.utcnow() (from-import style in courtview.py)
  - ARCHIVE_REFRESH_CYCLE_SECS placed adjacent to HEATMAP_STALE_SECS (same conceptual group)
  - Verified thread activity via sqlite row counts rather than log inspection (print goes to /dev/null in daemon mode)
metrics:
  duration: ~15 minutes
  completed: 2026-06-12
  tasks_completed: 2
  files_modified: 1
---

# Quick Task 260612-wym: Add Background Archive Refresh Thread - Summary

## One-liner

Daemon thread that snapshots Racketeer club info (4 endpoints) and current-month revenue into archive tables every 24h, starting 60s after gunicorn startup.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add _archive_refresh_loop and start the daemon thread | 12279c7 | courtview.py |
| 2 | Deploy and verify | (scp + gunicorn restart) | RPi: /root/projects/courtview/courtview.py |

## What Was Built

Added `_archive_refresh_loop()` function to `courtview.py`:

- New constants: `ARCHIVE_REFRESH_INITIAL_DELAY = 60` and `ARCHIVE_REFRESH_CYCLE_SECS = 24 * 3600`, placed beside `HEATMAP_STALE_SECS`
- New function `_archive_refresh_loop()` inserted immediately before the "Background cache refresh thread" comment block (after `_heatmap_refresh_loop()`)
- Each cycle fetches 4 club info endpoints in parallel via `ThreadPoolExecutor`, INSERTs result into `archive_club_info`
- Each cycle fetches current-month revenue via `/club/statistics/financial/v2`, INSERTs into `archive_financial`
- Exceptions are caught per-fetch and logged with `[archive-refresh]` prefix; the loop continues
- Thread started as `daemon=True` in `_startup()` with a startup print message

## Verification

- Local grep: `grep -c "_archive_refresh_loop" courtview.py` = 2 (def line + thread target)
- RPi grep: `rpi 'grep -c "_archive_refresh_loop" /root/projects/courtview/courtview.py'` = 2
- RPi gunicorn: pids 2886729/2886730 running after restart
- RPi archive_club_info rows inserted in last hour: 6 (verified via sqlite3)
- RPi archive_financial rows inserted in last hour: 4 (verified via sqlite3)

## Deviations from Plan

### Minor Fix Applied

The plan used `datetime.datetime.utcnow()` in the function body, but `courtview.py` uses `from datetime import datetime, timedelta` (not `import datetime`). Applied the correct call style `datetime.utcnow()` to avoid a `NameError` at runtime.

## Known Stubs

None.

## Threat Flags

None - this is an append-only internal archive write using `RACKETEER_CLUB_ID` only, behind the existing gunicorn process. No new network endpoints or auth paths introduced.

## Self-Check

- courtview.py modified in worktree: FOUND
- commit 12279c7 exists: FOUND
- RPi file contains _archive_refresh_loop: FOUND (grep count = 2)
- archive tables receiving data: FOUND (6 + 4 rows in last hour)

## Self-Check: PASSED
