---
phase: quick
plan: 260612-vxi
subsystem: courtview-backend
tags: [archive, sqlite, racketeer, heatmap, club-info, financial]
dependency_graph:
  requires: []
  provides: [archive_heatmap, archive_club_info, archive_financial tables]
  affects: [courtview.py, courtview_cache.db]
tech_stack:
  added: []
  patterns: [append-only archive tables, plain INSERT alongside INSERT OR REPLACE]
key_files:
  modified:
    - courtview.py
decisions:
  - "No DELETE guard needed on availability table: eviction is passive (TTL miss in get_cached), no active DELETE exists anywhere in the file"
  - "Archive INSERTs added inside the product matrix loop (per dow x hr cell) so each heatmap refresh appends all 112 cells (7 DOW x 16 hours)"
  - "Financial archive for payment-history stores JSON-serialised items list (not the paginated wrapper) since the raw upstream response is what has research value"
  - "Deployed worktree file directly via scp (cv-deploy reads from main repo checkout; worktree branch not yet merged)"
metrics:
  duration: 103s
  completed: 2026-06-12
  tasks_completed: 3
  files_modified: 1
---

# Phase quick Plan 260612-vxi: Archive Racketeer Data Permanently Summary

Append-only archive tables added for heatmap, club info, and financial data. Every periodic refresh now writes a timestamped row to the archive alongside the existing INSERT OR REPLACE into the live cache tables. Racketeer availability rows confirmed never deleted (no DELETE on availability table - eviction is TTL-passive only).

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Add RACKETEER_CLUB_ID constant and three archive tables in init_db | 0483890 |
| 2 | Add archive INSERT writes alongside all live-table writes (6 locations) | 1429594 |
| 3 | Deploy to RPi and verify archive tables created, gunicorn running | (deploy only) |

## Archive Points Added

| Archive Table | Locations | Trigger |
|---|---|---|
| `archive_heatmap` | TPC heatmap product matrix, Padelmates `_fetch_heatmap_for_club` product matrix | Every 24h heatmap refresh per club |
| `archive_club_info` | `_api_club_info_tpc` after live cache write, Padelmates club info fetch after live cache write | Every request when cache is stale |
| `archive_financial` | `api_revenue_summary` after `r.status_code != 200` guard, `api_payment_history` inside `if r.status_code == 200` | Every successful upstream API response |

## Availability Eviction Analysis

Confirmed: no `DELETE` statement targets the `availability` table anywhere in courtview.py. The 28-day TTL is enforced passively in `get_cached` (returns None for rows older than cutoff, triggering a fresh fetch). The 6-hour `_refresh_loop` updates `fetched_at` on every row, keeping Racketeer rows perpetually fresh. No eviction guard is needed.

## Deviations from Plan

**1. [Rule 3 - Blocking] Deploy script copies from main repo, not worktree**

- **Found during:** Task 3
- **Issue:** `cv-deploy` resolves `SCRIPT_DIR` from `deploy.sh`'s own location in `/Users/herwy/dev/courtview/` (main checkout), not from the worktree. The modified `courtview.py` only exists in the worktree branch.
- **Fix:** Deployed directly via `scp /Users/herwy/dev/courtview/.claude/worktrees/agent-aaff428e136b906dc/courtview.py pi-cmd:/root/projects/courtview/courtview.py`
- **Note:** Once the worktree branch is merged to main, `cv-deploy` will work normally for future deploys.

**2. [Rule 3 - Blocking] gunicorn restart via compound SSH command exited 255**

- **Issue:** `pkill -f "gunicorn.*courtview" && sleep 2 && gunicorn --daemon ...` - pkill kills gunicorn but the bash process (which is the SSH session's command) exits 255 as part of the SIGTERM cascade.
- **Fix:** Used Python `subprocess.Popen` with `start_new_session=True` to start gunicorn as per rpi-ssh.md pattern. Gunicorn started cleanly as confirmed by pgrep.

## Verification Results

- `grep -c "RACKETEER_CLUB_ID\|archive_heatmap\|archive_club_info\|archive_financial" /root/projects/courtview/courtview.py` returned **10** (verified)
- `python3 -c "... sqlite_master ..."` returned all three archive tables present: `archive_club_info`, `archive_financial`, `archive_heatmap` (verified)
- `pgrep -fa "gunicorn.*courtview"` shows two gunicorn workers running (verified)

## Known Stubs

None - all archive tables are live and will receive rows on the next heatmap/club-info/financial refresh cycle.

## Self-Check: PASSED

- Commits 0483890 and 1429594 verified in git log
- Archive tables confirmed present in live DB on RPi
- All 6 archive INSERT locations confirmed via grep
