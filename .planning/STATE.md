---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: active
last_updated: "2026-07-10T23:46:00Z"
---

## Quick Tasks

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 1 | Fix tooltip clipping (position:fixed) + payment history sliding window | 2026-05-22 | a343298 | 260522-ul6-fix-tooltip-clipping-and-payment-history |
| 2 | Add Stratford Padel Club (TPC Matchpoint) as 4th club in CourtView | 2026-05-22 | c83f8b5 | 260522-wfp-add-stratford-padel-club-tpc-matchpoint- |
| 3 | Enrich Club Info tab for Stratford: live address, hours, courts from TPC API | 2026-05-22 | a827862 | 260522-x1q-enrich-club-info-tab-for-stratford-tpc-c |
| 4 | Fix Availability 404 + Compare pricing + Best Times for TPC (fetchDay guard, monthCache scan, 90min price fallback) | 2026-05-23 | 46836e0 | inline |
| 5 | Add TPC early-exit stubs to 6 unguarded backend endpoints (membership, booked-hours, day-activities, payment, coach-stats, coach-bios) | 2026-05-23 | d97d96c | inline |
| 6 | TLS on port 8766 via labs.doxx cert (watchdog.sh + deploy.sh, graceful fallback) | 2026-05-23 | b776a06 | inline |
| 7 | Fix CLAUDE.md: http://lab.doxx -> https://labs.doxx, remove incorrect Bitwarden token reference | 2026-05-23 | 56ee4be | inline |
| 8 | Fix Compare tab timing race: fetch /api/month directly for TPC if monthCache not yet populated | 2026-05-23 | 75bdb87 | inline |
| 9 | Remove dead code: fetchClubProfile, fetchMembership, fetchCreditPackages, fetchFinancialStats | 2026-05-23 | ebce753 | inline |
| 10 | Fix stale statsCoachMeta on club switch + booked-hours null for TPC + CLAUDE.md Stratford entry | 2026-05-23 | 6d959b3 | inline |
| 11 | Fix /access page always showing 127.0.0.1 - add X-Real-IP forwarding in Caddy, read in Flask | 2026-05-27 | 5437239 | [260527-mp8-fix-client-ip-x-real-ip-caddy-flask](.planning/quick/260527-mp8-fix-client-ip-x-real-ip-caddy-flask/) |
| 12 | Add impersonate=chrome110 to all 18 cffi_requests calls for TLS fingerprint impersonation | 2026-05-27 | bd15fd6 | [260527-n14-add-tls-impersonation-cffi-requests](.planning/quick/260527-n14-add-tls-impersonation-cffi-requests/) |
| 13 | Add pip-audit security gate to deploy.sh + requirements.txt (curl-cffi, flask, gunicorn) | 2026-06-02 | c821fc2 | [260602-3k7-pip-audit-gate-deploy-sh](.planning/quick/260602-3k7-pip-audit-gate-deploy-sh/) |
| 14 | Archive Racketeer data permanently for offline dashboard viewing | 2026-06-12 | 1429594 | [260612-vxi-archive-racketeer-data-permanently-for-o](.planning/quick/260612-vxi-archive-racketeer-data-permanently-for-o/) |
| 15 | Wire up archive data to dashboard UI and add DB indexes | 2026-06-12 | 5674e19 | [260612-we4-wire-up-archive-data-to-dashboard-ui-and](.planning/quick/260612-we4-wire-up-archive-data-to-dashboard-ui-and/) |
| 16 | Add background archive refresh thread for Racketeer club info and revenue | 2026-06-12 | 12279c7 | [260612-wym-add-background-archive-refresh-thread-fo](.planning/quick/260612-wym-add-background-archive-refresh-thread-fo/) |
| 17 | Fix CourtView process supervision: migrate to systemd, fix watchdog false positive, update deploy.sh | 2026-06-14 | e393636 | [260614-wjl-fix-courtview-process-supervision-migrat](.planning/quick/260614-wjl-fix-courtview-process-supervision-migrat/) |
| 18 | Stop background polling for Racketeer (migrated off Padelmates to a different app): removed from HEATMAP_CLUBS, excluded from availability refresh loop, removed archive refresh thread + dead helper code. Kept polling for other 3 clubs and on-demand /api/* proxying for Racketeer. | 2026-07-10 | 1b7b33c | inline |
| 19 | Fix /api/archive/heatmap 500 error: archive_heatmap rows have always had NULL hour_norm/dow_norm (writer never populated them since 2026-06-12); endpoint now skips signal entries instead of crashing on round(None) | 2026-07-10 | 9db59db | inline |
| 20 | Add "Top spenders" monthly leaderboard to Revenue tab: /api/payment-leaderboard aggregates successful payments per player (paginated, stats_cache-backed), rendered as a top-3 card between Revenue summary and Revenue by day of week | 2026-07-23 | db66eaa | inline |
| 21 | Add archived Top spenders for Racketeer (/api/archive/payment-leaderboard, aggregates all daily payment-history snapshots, dedup by transaction_id); speed up live leaderboard pagination via bounded ThreadPoolExecutor (~16-18s -> ~5s cold); URL-encode club_id/start/end in upstream request | 2026-07-23 | 5aae725 | inline |
| 22 | Fix Top spenders card race condition: loadRevenue() guarded against club changes mid-fetch but not month changes, so rapid month nav clicks could let a stale slower month's leaderboard response overwrite a fresher one. Added revReqSeq counter. | 2026-07-23 | acfc764 | inline |
| 23 | Fix Racketeer archive view overwrite bug: loadRevenue() never returned after the archive branch, so it fell through into the live /api/payment-leaderboard fetch too (empty for Racketeer since 2026-07-01 migration), which overwrote the correct archived leaderboard once it resolved. Added missing return; moved month-label update above the branch. | 2026-07-23 | 87f30dd | inline |
| 24 | Correct overcorrection from #23: unconditional return also broke the legitimate default "Live" rendering for revSummaryOut/revDowOut/revSubOut/revPayOut (the archive dropdown never auto-fires, so that fallthrough was the only thing populating them). Scoped the exclusion to just the leaderboard fetch/render instead of the whole fallthrough. | 2026-07-23 | 93da6c0 | inline |
| 25 | Remove the whole Racketeer-special-case for Top spenders (from #20/#21): verified live upstream still returns correct historical payment data for any past month for Racketeer, so the "no live data" premise behind the archive-only design was wrong. Top spenders now uses the same live per-month path as every club; deleted the now-dead /api/archive/payment-leaderboard endpoint. | 2026-07-23 | 70993b1 | inline |

## Session Continuity

| Area | Stopped At | Resume File |
|------|-----------|-------------|
| ul6 | Session 2026-05-22: tooltip portal fix, payment 1-day window, Rocket activity fallback, club switch UX, activity type renames | None |
| wfp | Session 2026-05-23: All fixes complete. Compare race fixed. HEATMAP_CLUBS courts corrected. Checklist clean. | None |
