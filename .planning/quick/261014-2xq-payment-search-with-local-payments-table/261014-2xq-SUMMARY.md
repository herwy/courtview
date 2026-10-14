---
phase: quick-261014-2xq
plan: 01
subsystem: courtview payments
tags: [sqlite, payments, search, leaderboard, padelmates]
requirements-completed: [PAY-TABLE, PAY-SYNC, PAY-SEARCH, PAY-LEADERS, PAY-UI, PAY-DOCS, PAY-HEADERS]
key-files:
  modified: [courtview.py, courtview.html, tests/e2e_dashboard.py, .claude/docs/courtview-internals.md, CLAUDE.md]
  created: [tests/test_payments.py]
completed: 2026-10-14
---

# Quick 261014-2xq: Payment search with local payments table

Padelmates payments are now persisted per London day in a local SQLite `payments` table. The Revenue tab has a name/email search over the whole month, and Top spenders sums the full month from the table instead of stopping at 2,000 payments.

## Commits (all pushed, main == origin/main at a0e2549)
- ca2f90d: tables, shared page fetcher, per-day sync, backfill loop
- 1a88e50: live and archive payment-search, table-backed leaderboard
- a0e2549: search UI, e2e checks, docs

## Pre-check (D-G)
One full Padium day (2026-09-17): 275 records, 275 distinct transaction_id, 0 missing. PK is (club_id, transaction_id). Cost 3 upstream pages.

## Tests
- `tests/test_payments.py`: 20 tests, OK. Local Python is 3.9 without Flask, so they were run on the RPi (Python 3.13) from a temp copy in /tmp/cvt with COURTVIEW_DB_PATH pointing at a temp dir (prod DB untouched).
- `node --test tests/test_pay_pager.mjs`: 11 pass, 0 fail.
- Old leaderboard grep: 0.
- Header scan (AST): **18 cffi_requests calls, 18 compliant** (headers=APP_HEADERS and impersonate="chrome110"), no BAD lines.

## Deploy
`cv-deploy` at 2026-10-14 01:16:19 UTC: pip-audit clean, files synced, systemd active, enabled, :8766 owned by MainPID.
Remote greps: `revPaySearch` in courtview.html = 4, `_sync_payments` in courtview.py = 4, `systemctl is-active` = active.

## Backfill (previous + current London month, Sep and Oct)
Padium done in 54.8 s (154 pages), Rocket in 135.5 s (251 pages). Both complete=1, 0 incomplete days (payments_sync complete=0 count = 0).

| Club | Month | Rows |
|---|---|---|
| Padium (47d2eb0d) | 2026-09 | 8,906 |
| Padium | 2026-10 | 4,324 |
| Rocket (788fa2c6) | 2026-09 | 15,907 |
| Rocket | 2026-10 | 6,884 |

- Upstream pages fetched in backfill (sum of `pages=` per-day journal lines since deploy): **405** (88 day lines). Plus 3 in the pre-check = 408. No later calls (2nd leaderboard and searches were served from the table).
- Padium September vs the earlier >= 6,100: 8,906 rows in total, of which 5,776 are PAYMENT_SUCCESS. The 6,100 figure is met by the all-status row count; I could not tell which definition the earlier number used, so the success-only count is a little below it. Not investigated further.

## Leaderboard (Padium September, bounds 1788217200000 to 1790809199999)
- Journal `[payments] club=` lines: before 88, after 1st call 88, after 2nd call 88. **Second-call new sync lines = 0** (responses byte-identical).
- Top spender (Vi*** Lu*** Ha***): 1543.0 over 19 payments, equal to direct SQL SUM 1543.0 / 19. Top 5 totals: 1543.0, 1430.0, 1260.0, 1260.0, 1010.75.

## Late-month search proof (Padium, September)
Surname masked "co***": total 3, returned 3, complete=true, truncated=false, dates 2026-09-01 to 2026-09-22 (max >= 20).

## Racketeer
`/api/archive/payment-search` June 2026, surname "Ro***": total 1, complete=true, date 2026-06-14. `[archived]` journal lines since deploy: **0**. Live route returns 410 and sync returns early for Racketeer (unit-tested, 0 upstream calls).

## Playwright e2e (via SSH tunnel, tunnel closed afterwards)
**RESULT: ALL PASS** (Racketeer: search matches, clear restores 'archived', 0 live requests, 0 410s; Padium: 101 rows paged, Load More, search returned rows matching the token, newest first, Load More hidden, single char leaves no 'Searching...', clear restores paged view).

## Deviations from Plan
- [Rule 3] Unit tests cannot run locally (Python 3.9, no Flask/curl_cffi); they were executed on the RPi from a temp copy. Documented in the internals doc.
- Task 3 deploy ran before the Task 3 commit (files identical to what was committed); commit pushed afterwards.
- Plan's "STATE Quick Tasks row 35" and validate.health left to the orchestrator per the instructions.

## Known Stubs
None.

## Threat Flags
None beyond the plan's threat model. Payment PII (name, email, phone) is now stored persistently in `payments` (user-approved, T-2xq-02); logs carry only club prefix, day and counts.

## Self-Check: PASSED
Files exist (courtview.py, courtview.html, tests/test_payments.py, tests/e2e_dashboard.py, docs, CLAUDE.md); commits ca2f90d, 1a88e50, a0e2549 on origin/main.
