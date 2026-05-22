---
phase: quick
plan: 260522-x1q
subsystem: api
tags: [tpc, matchpoint, club-info, flask, activities]

requires:
  - phase: quick/260522-wfp
    provides: TPC Matchpoint backend integration, _tpc_post helper, TPC_CENTRO_ID constant

provides:
  - Live club profile for Stratford TPC via ObtenerInformacionCentro (name, address, email, courts, hours)
  - Activities & Programs section in renderClubInfo (graceful empty state when endpoint unavailable)

affects: [club-info, tpc, stratfordpadelclub]

tech-stack:
  added: []
  patterns:
    - "Per-call try/except wrapping for independent TPC API fallback"
    - "Map-based group preservation for activity grouping in JS"

key-files:
  created: []
  modified:
    - courtview.py
    - courtview.html

key-decisions:
  - "ObtenerConfiguracionSistemaReservaPlazas returns HTTP 404 for this TPC installation - activities = [] is the correct fallback, not a bug"
  - "Profile parsing extracts Horario field (HH:MM-HH:MM) into 7-day opening_hours array"
  - "Activities grouped using JS Map to preserve server sort order by group_id"

patterns-established:
  - "Pattern: wrap each _tpc_post call individually so profile failure does not block activities"

requirements-completed: [x1q-enrich-tpc-club-info]

duration: 15min
completed: 2026-05-22
---

# Quick Task 260522-x1q: Enrich Club Info Tab for Stratford TPC Summary

**Live ObtenerInformacionCentro call replacing the static stub, returning real address/courts/hours for Stratford TPC with Activities & Programs section wired in renderClubInfo**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-05-22T22:40:00Z
- **Completed:** 2026-05-22T22:56:00Z
- **Tasks:** 3 (2 code + 1 deploy/verify)
- **Files modified:** 2

## Accomplishments

- `_api_club_info_tpc` now calls `ObtenerInformacionCentro` live: real name ("Stratford Padel Club"), address ("221 High Street E15 2 LONDON London"), email, courts (14), and opening hours (08:00-23:00) all returned from the TPC API
- Profile and activity calls wrapped individually - a 404 on activities does not prevent profile from loading
- `renderClubInfo` updated to accept and render a 5th `activities` parameter, grouped by `group_name` using a Map to preserve insertion order; empty-state guard includes activities check

## Task Commits

1. **Task 1: Replace _api_club_info_tpc stub with live TPC API calls** - `ad4f84b` (feat)
2. **Task 2: Add Activities & Programs section to renderClubInfo** - `a827862` (feat)
3. **Task 3: Deploy and verify** - deployed via scp + gunicorn restart; no separate commit

## Files Created/Modified

- `courtview.py` - `_api_club_info_tpc` replaced with live ObtenerInformacionCentro + ObtenerConfiguracionSistemaReservaPlazas calls with individual try/except fallbacks
- `courtview.html` - `loadClubInfo` passes `data.activities` as 5th arg; `renderClubInfo` accepts activities and renders grouped list; empty-state guard updated

## Decisions Made

- `ObtenerConfiguracionSistemaReservaPlazas` returns HTTP 404 on this TPC installation - the endpoint simply isn't enabled. Multiple alternative endpoints were probed (ObtenerActividades, ObtenerEscuelas, ObtenerGruposActividades, v2 variants) - all 404. The except branch fires correctly and returns `activities: []`. This is not a bug.
- `idCentro` value: used `TPC_CENTRO_ID` (the constant already defined from the prior wfp task) for the ObtenerInformacionCentro call, consistent with how `_fetch_tpc_day` calls it.

## Deviations from Plan

### Auto-fixed Issues

None for code. One deviation during deploy:

**1. [Rule 3 - Blocking] cv-deploy reads from main repo, not worktree**
- **Found during:** Task 3 (Deploy)
- **Issue:** `cv-deploy` runs `deploy.sh` from `~/dev/courtview/` using `$SCRIPT_DIR`, deploying the main repo's (unmodified) files instead of the worktree's modified files
- **Fix:** Deployed directly via `scp` from the worktree path, then restarted gunicorn manually via `rpi`
- **Files deployed:** courtview.py, courtview.html from `/Users/herwy/dev/courtview/.claude/worktrees/agent-a8d431bcd3e37531e/`
- **Verification:** `grep -c "ObtenerInformacionCentro"` returned 2 on RPi; endpoint returned live data

---

**Total deviations:** 1 (deploy routing, handled inline)
**Impact on plan:** No scope change. All plan goals met except activities (endpoint not available on this TPC installation).

## Issues Encountered

- `ObtenerConfiguracionSistemaReservaPlazas` HTTP 404 - not a code bug. TPC Matchpoint is a white-label platform; feature modules are enabled per-club. The `Hay_Actividades_Colectivas` flag in the Centro response may indicate this module is not enabled for Stratford TPC. The fallback `activities: []` is correct behaviour.
- cv-deploy reads from main repo path, not worktree - worked around by direct scp from worktree.

## Verification Results

- `grep -c "ObtenerInformacionCentro" /root/projects/courtview/courtview.py` = **2** [verified]
- `grep -c "activity-list|Activities" /root/projects/courtview/courtview.html` = **7** [verified]
- Endpoint response: `status 200`, `name: Stratford Padel Club`, `address: 221 High Street E15 2 LONDON London`, `courts: 14`, `hours[0]: {'open': '08:00', 'close': '23:00'}` [verified]
- Gunicorn running on port 8766 [verified]

## Known Stubs

None. All data is live from the TPC API. Activities section renders an empty state when the activities endpoint is unavailable.

## Next Phase Readiness

- Club Info tab for Stratford TPC now shows live data (name, address, email, 14 courts, 08:00-23:00 hours)
- Activities section is wired and will populate if/when the TPC activities module is enabled for the club
- The `activities` field is in the JSON payload, ready for the frontend

## Self-Check

- [x] courtview.py on RPi contains `ObtenerInformacionCentro` (grep count: 2)
- [x] courtview.html on RPi contains `activity-list` and `Activities` (grep count: 7)
- [x] Commits exist: `ad4f84b`, `a827862`
- [x] Live endpoint returns real club data

## Self-Check: PASSED

---
*Phase: quick*
*Completed: 2026-05-22*
