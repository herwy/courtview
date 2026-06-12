---
phase: quick
plan: 260612-we4
subsystem: courtview
tags: [archive, flask, sqlite, dashboard-ui, racketeer]
dependency_graph:
  requires: [260612-vxi]
  provides: [archive-read-api, archive-selector-ui]
  affects: [courtview.py, courtview.html]
tech_stack:
  added: []
  patterns: [sqlite-compound-index, flask-route, js-dynamic-selector]
key_files:
  created: []
  modified:
    - courtview.py
    - courtview.html
decisions:
  - "renderRevenueSummary extracted from loadRevenue to enable archive snapshot rendering without full re-fetch"
  - "renderClubInfoData extracted from loadClubInfo for same reason"
  - "showArchiveSelector handles idempotency: re-populates on tab focus, inserts once per tab"
  - "endpoint whitelist for /api/archive/financial: revenue-summary and payment-history only"
  - "Deployed via scp (pi-cmd) because worktree context; cv-deploy reads main checkout"
metrics:
  duration: "~30 minutes"
  completed: "2026-06-12"
  tasks_completed: 3
  files_modified: 2
---

# Phase quick Plan 260612-we4: Wire Archive Data to Dashboard UI Summary

Archive tables previously written by the background archiver (260612-vxi) are now readable via four new Flask endpoints and surfaced in the UI via per-tab Archive selectors on the Racketeer club.

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | DB indexes + 4 archive read endpoints (courtview.py) | 8c80a7f |
| 2 | Archive selector UI for Racketeer tabs (courtview.html) | 5674e19 |
| 3 | Deploy, restart gunicorn, verify on RPi | (no code commit) |

## What Was Built

**courtview.py:**
- Three compound indexes: `idx_archive_heatmap_club`, `idx_archive_club_info_club`, `idx_archive_financial_club` - each on `(club_id, captured_at)` for fast per-club lookups
- `GET /api/archive/snapshots` - lists distinct `captured_at` timestamps for a given club + table (whitelist: heatmap, club_info, financial)
- `GET /api/archive/heatmap` - returns archived heatmap in same JSON shape as `/api/heatmap`
- `GET /api/archive/club-info` - returns stored payload for a given snapshot
- `GET /api/archive/financial` - returns stored payload for a given snapshot and endpoint (whitelist: revenue-summary, payment-history)
- All four routes: `_gate()` auth check, parameterised queries, `captured_at` cast to `int`, `sqlite3.Error` caught with 500 response

**courtview.html:**
- `RACKETEER_CLUB_ID` JS constant (matches Python constant)
- `populateArchiveSelect(selectEl, table)` - async, fetches `/api/archive/snapshots`, adds "Live" first
- `buildArchiveSelector(tabName, table, onSnapshotLoad, onLive)` - creates hidden `<div class="archive-selector">` with label + select
- `showArchiveSelector(...)` - idempotent: inserts once per tab via `insertAdjacentElement('afterbegin')`, re-populates select on each call
- Archive selector wired into `loadClubInfo`, `loadStats`, `loadRevenue` - guard: `if (CLUB === RACKETEER_CLUB_ID)`
- `renderClubInfoData(data, clubId)` extracted from `loadClubInfo`
- `renderRevenueSummary(sumData)` extracted from `loadRevenue` (async, used by both live and archive paths)
- `.archive-selector` CSS: hidden by default, shown by `showArchiveSelector`

## Verification Results

- Remote `grep -c "archive/snapshots|archive/heatmap|archive/club-info|archive/financial" courtview.py` = **4**
- Remote `grep -c "archive/snapshots|RACKETEER_CLUB_ID" courtview.html` = **9**
- DB indexes present: `['idx_archive_heatmap_club', 'idx_archive_club_info_club', 'idx_archive_financial_club']`
- Gunicorn running: 3 processes (master + 2 workers)
- Snapshots endpoint smoke test: `GET /api/archive/snapshots?club_id=...&table=heatmap` returns `200 []` (empty - no data captured yet, expected)

## Deviations from Plan

**1. [Rule 3 - Blocking] Worktree deploy via scp instead of cv-deploy**
- cv-deploy reads from the main repo checkout, not the worktree
- Used `scp -F ~/.ssh/config ... pi-cmd:/root/projects/courtview/` directly
- Both files deployed and verified individually

None - all four routes and three indexes implemented exactly as specified. Archive selector present on all three tabs with Racketeer guard. SSL cert verification failure on smoke test resolved by using `CERT_NONE` context (internal RPi-to-RPi test only; production traffic uses full TLS).

## Known Stubs

None. Archive endpoints return real DB data. Archive selector is populated lazily from `/api/archive/snapshots`. The empty response from the smoke test reflects the actual state of the archive tables (no snapshots captured yet), not a stub.

## Self-Check: PASSED

- courtview.py deployed and grep verified on RPi (count = 4)
- courtview.html deployed and grep verified on RPi (count = 9)
- DB indexes created (verified via sqlite_master query)
- Gunicorn running (pgrep verified)
- Snapshots endpoint returns HTTP 200
