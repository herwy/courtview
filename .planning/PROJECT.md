# CourtView

## What This Is

A Flask proxy and dashboard for the Padelmates app API, served by gunicorn under systemd on the RPi (127.0.0.1:8766, TLS terminated in front). It shows court availability, booking heatmaps, court popularity, club info and revenue for Padium Canary Wharf and Rocket Padel Ilford. It also shows archived Racketeer data from 2026-06-15 to 06-30, before Racketeer left Padelmates.

## Core Value

All Padelmates traffic goes server-side with Android OkHttp headers, so the browser never contacts Padelmates. The service must stay up and alert in plain English when it doesn't.

## Requirements

### Validated

- ✓ Whitelisted `/api/*` proxy with token auth, per-IP rate limiting, SQLite cache and background refresh threads
- ✓ Heatmap, court popularity, revenue, anonymous top spenders and compare views
- ✓ systemd supervision with restart backoff and plain-English Telegram alerts (quick-28/29)
- ✓ No personal data stored or shown: allowlists, PII scrubbing, HMAC `customer_key`, encrypted nightly backups (Phase 1, quick-37)
- ✓ Load and reliability: shared `UpstreamClient`, persisted job schedules, read-only payment routes, status JSON and `/api/health` (Phase 2, quick-38)
- ✓ Security hygiene: CSP, safe static route, gated 404, 401 handling (Phase 3, quick-40)
- ✓ Non-root sandboxed service, release dirs with health-checked auto-rollback, pinned deps with pip-audit (Phase 4, quick-41)
- ✓ `courtview_app` package, single club config at `/api/clubs`, frontend `api()` data layer (Phase 5, quick-42)

### Active

(None: maintenance mode, ad-hoc quick tasks only.)

### Out of Scope

- Proxying PII endpoints (`/club/follower/crud`, `/club/member/`, `/club/membership/member`); see `.claude/docs/padelmates-disclosure-2026-05-21.md`
- Storing or showing personal data (payment search and coach bios removed in Phase 1)
- Endpoints that need Padelmates auth: `/club/club_extras` and `/home/activity/get_booked_hours` retired on 2026-10-14; no credentials will be added
- Non-Padelmates platforms (TPC Matchpoint removed 2026-07-24)

## Context

Navigation in CLAUDE.md; reference in `.claude/docs/courtview-internals.md`. Deploy with `cv-deploy`; the Mac is the canonical source.
