---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: active
last_updated: "2026-05-27T00:00:00Z"
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

## Session Continuity

| Area | Stopped At | Resume File |
|------|-----------|-------------|
| ul6 | Session 2026-05-22: tooltip portal fix, payment 1-day window, Rocket activity fallback, club switch UX, activity type renames | None |
| wfp | Session 2026-05-23: All fixes complete. Compare race fixed. HEATMAP_CLUBS courts corrected. Checklist clean. | None |
