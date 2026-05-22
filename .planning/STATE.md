---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: active
last_updated: "2026-05-23T00:20:00Z"
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

## Session Continuity

| Area | Stopped At | Resume File |
|------|-----------|-------------|
| ul6 | Session 2026-05-22: tooltip portal fix, payment 1-day window, Rocket activity fallback, club switch UX, activity type renames | None |
| wfp | Session 2026-05-23: Stratford fully integrated - Availability, Stats heatmap, Club Info, Compare all working. TLS live. All TPC endpoint stubs in place. | None |
