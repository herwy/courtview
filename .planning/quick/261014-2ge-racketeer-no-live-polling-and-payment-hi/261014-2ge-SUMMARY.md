# Quick 261014-2ge Summary

Racketeer is archive-only (server 410 guard plus UI), and Revenue payment history now walks back by UK-local day with local timestamps.

## Commits (all pushed, main == origin/main)
- Task 1: `feat(quick-261014-2ge): 410 guard for archived Racketeer on all live /api routes` (courtview.py)
- Task 2 RED: `test(quick-261014-2ge): failing pay pager tests`
- Task 2 GREEN: `feat(quick-261014-2ge): day-walking payment history paging with local timestamps`
- Task 3: `feat(quick-261014-2ge): Racketeer archive-only dashboard, Best Times empty state`

## Verification
- Curl matrix on RPi: 410 on all 17 live Racketeer paths (13 routes, slot-prices, /api/club/, financial/v2, operational with club_ids). 200 on /api/archive/snapshots (financial, club_info, heatmap), archive/financial (revenue-summary and payment-history), archive/club-info, archive/heatmap. 200 on Padium /api/month, /api/club-info, /api/heatmap. 403 for Racketeer with no cookie. 410 body: `{"error":"archived","message":"Racketeer left Padelmates on 2026-07-01; only archived snapshots are available."}`
- Padium paging (2026-10-13, 100 payments on page 1): skip=0 count 100 has_more true (2026-10-12 23:07:54 to 2026-10-13 12:03:59); skip=100 count 100 has_more true (12:21:47 to 17:25:32); id overlap 0. The API returns oldest first, so page 2 continues forward in time within the day.
- `node --test tests/test_pay_pager.mjs`: 11 pass, 0 fail, under default TZ and TZ=UTC. RED run failed first (markers missing).
- Background loops: `_heatmap_refresh_loop` iterates HEATMAP_CLUBS (Padium, Rocket only); `_refresh_loop` SQL still excludes RACKETEER_CLUB_ID.
- Deploy: `cv-deploy` OK three times, systemd active. Deployed grep counts: RACKETEER_ARCHIVED_MSG 2, PAY-PAGER:BEGIN 1, fmtPayLocal 2, _block_archived_club 2 (courtview.py), `systemctl is-active` = active.
- Playwright (Python, Chromium 145 already installed, nothing installed). labs.doxx:8766 is unreachable off-LAN (timeout), so the e2e ran through an SSH port-forward made with the `rpi` wrapper (`rpi -N -L 18766:127.0.0.1:8766`), closed afterwards. All PASS, `RESULT: ALL PASS`:
  - Racketeer: 0 requests with the Racketeer id outside /api/archive/; 0 responses with 410; stopped-taking-bookings message; no "Live" option; 3 archive selectors; payment table rendered; Load More hidden; Compare has no Racketeer row and lists Padium and Rocket.
  - Padium: 101 payment rows, all Date cells YYYY-MM-DD HH:MM, newest first, meta "loaded · back to", Load More grew rows/moved the day earlier, Best Times not stuck on "Loading recommendations..." after 30 s.
- Journal `[archived]` lines during the e2e run: 0.

## Deviations
- Orchestrator addition: default club for new visitors is now the first non-archived club (Padium Canary Wharf). To avoid a temporal-dead-zone error, `RACKETEER_CLUB_ID` was moved above `CLUBS` in courtview.html. The e2e sets localStorage so the default path itself was not exercised by a test; the page loads fine on the deployed build.
- CLAUDE.md on disk differs from the plan's quoted text (it is condensed); the two wrong lines were rewritten in place.
- `~/.claude/projects/-Users-herwy-dev-courtview/memory/project_courtview.md` does not exist (MEMORY.md links it), so no memory edit was made.
- Stats for Racketeer shows "Not archived for Racketeer" for KPI/mix/coaching; only the DOW chart comes from the archive.

## Open items
- Revenue "Top spenders" for Racketeer shows "Not archived" (the earlier quick #25 live behaviour is intentionally reversed).
- Stale `Co-Authored-By` line is Opus 5.5 per orchestrator instruction.
- Untracked, not ours: `.claude/docs/bookandgo-jungle-padel-2026-05-23.md`, `.claude/docs/playtomic-2026-05-23.md`.

## Self-Check: PASSED
Files exist (courtview.py, courtview.html, tests/test_pay_pager.mjs, tests/e2e_dashboard.py, docs); all four commits pushed.
