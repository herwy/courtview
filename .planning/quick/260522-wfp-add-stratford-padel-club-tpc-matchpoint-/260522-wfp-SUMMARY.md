---
phase: quick
plan: 260522-wfp
subsystem: courtview
tags: [tpc, heatmap, availability, frontend, backend]
dependency_graph:
  requires: []
  provides: [stratford-tpc-backend, stratford-tpc-frontend]
  affects: [courtview.py, courtview.html]
tech_stack:
  added: [TPC Matchpoint API (urllib.request, no new deps)]
  patterns: [platform-dispatch on HEATMAP_CLUBS, TPC early-exit branches in api_month/api_club_info/api_activity_summary/api_revenue_summary]
key_files:
  created: []
  modified:
    - courtview.py
    - courtview.html
decisions:
  - "Used stdlib urllib.request for TPC calls - no new dependencies"
  - "TPC static token 'autorizado' stored as constant - public API passcode per APK analysis, no user PII"
  - "court_popularity for TPC derived from ObtenerPistasDisponibles3 available_slot counts (slot count as proxy since hourly endpoint lacks court names)"
  - "api_club_info TPC branch returns immediately without force_refresh check - static data always fresh"
metrics:
  duration_minutes: 45
  completed_date: "2026-05-22"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 2
---

# Phase quick Plan 260522-wfp: Add Stratford Padel Club TPC Integration Summary

**One-liner:** TPC Matchpoint integration for Stratford Padel Club using urllib.request, with 28-day availability cache, 14-day heatmap from ObtenerHorariosDisponibles, and frontend guards for Padelmates-only stats sections.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | TPC backend: fetch helpers, api_month routing, heatmap, club-info stub | d0b4b2d | courtview.py |
| 2 | Frontend: Stratford in CLUBS/COMPARE_CLUBS, guard TPC sections | ec37ff9 | courtview.html |

## What Was Built

**courtview.py additions:**
- `TPC_BASE_URL`, `TPC_AUTH_URL`, `TPC_TOKEN`, `TPC_CENTRO_ID`, `TPC_CUADRO_ID`, `TPC_CLUB_ID` constants
- `stratfordpadelclub` entry added to `HEATMAP_CLUBS` with `"platform": "tpc"`
- `_tpc_post(endpoint_path, body_dict)` - thin urllib.request POST wrapper with TPC auth headers
- `_tpc_date(date_obj)` - converts date to DD/MM/YYYY Spanish format
- `_fetch_tpc_day(date_obj)` - fetches ObtenerPistasDisponibles3, groups by court, returns v1 court list
- `_api_month_tpc(club_id, via_query)` - 28-day cache loop matching api_month pattern
- TPC routing branch in `api_month()` (club_id == TPC_CLUB_ID early exit)
- `_fetch_tpc_heatmap()` - 14-day ObtenerHorariosDisponibles aggregation into heatmap tables + court_popularity
- `_heatmap_refresh_loop()` updated to dispatch on `club.get("platform") == "tpc"`
- `_api_club_info_tpc()` - static Stratford contact info, cached in club_info_cache
- TPC early-exit stubs in `api_activity_summary` (returns `{total:0, ...}`) and `api_revenue_summary` (returns `{platform_not_supported: true}`)

**courtview.html additions:**
- Stratford entry in CLUBS array: `{ id: 'stratfordpadelclub', name: 'Stratford Padel Club', sub: 'Stratford, London', courts: 9, platform: 'tpc' }`
- Stratford entry in COMPARE_CLUBS array
- `isTPCClub(id)` helper function
- `loadStats()` guard: skips activity-summary and coach-stats fetches for TPC clubs, renders empty-state messages instead

## Deviations from Plan

None - plan executed exactly as written.

## Deploy Verification

Files deployed manually via scp from worktree (cv-deploy deploys from main repo which lacked the worktree commits):

- `grep -c "stratfordpadelclub" /root/projects/courtview/courtview.html` returned **3** [verified: grep output]
- `grep -c "TPC_CLUB_ID" /root/projects/courtview/courtview.py` returned **12** [verified: grep output]
- gunicorn process confirmed running: PID 92869 [verified: pgrep output]

## Known Stubs

None. All data flows are wired:
- Availability: `_fetch_tpc_day` -> `store_cached` -> `_api_month_tpc` -> frontend
- Heatmap: `_fetch_tpc_heatmap` -> heatmap_cache tables -> `/api/heatmap` -> frontend
- Club info: `_api_club_info_tpc` -> club_info_cache -> frontend
- Activity mix / coach stats: explicit empty-state messages (not blank/null crashes)

## Threat Surface Scan

No new threat surface beyond what the plan's threat_model covers:
- T-tpc-01 (exact string compare for TPC_CLUB_ID): applied - all branches use `club_id == TPC_CLUB_ID`
- T-tpc-04 (timeout + [] on error): applied - timeout=15 in _tpc_post, _fetch_tpc_day returns [] on any exception

## Self-Check: PASSED

- courtview.py syntax: `python3 -c "import ast; ast.parse(...)"` -> `syntax OK`
- `grep -c "TPC_CLUB_ID" courtview.py` -> 12 (expected >= 1)
- `grep -c "stratfordpadelclub" courtview.html` -> 3 (expected >= 2)
- Commits d0b4b2d and ec37ff9 verified in git log
- Remote: `grep -c "stratfordpadelclub" /root/projects/courtview/courtview.html` -> 3
- Remote: `grep -c "TPC_CLUB_ID" /root/projects/courtview/courtview.py` -> 12
