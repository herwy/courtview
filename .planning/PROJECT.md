# CourtView

## What This Is

A Flask proxy and dashboard for the Padelmates app API, served by gunicorn under systemd on the RPi (127.0.0.1:8766, TLS terminated in front). It shows court availability, booking heatmaps, court popularity, club info and revenue for Padium Canary Wharf and Rocket Padel Ilford. It also shows archived Racketeer data from 2026-06-15 to 06-30, before Racketeer left Padelmates.

## Core Value

All Padelmates traffic goes server-side with Android OkHttp headers, so the browser never contacts Padelmates. The service must stay up and alert in plain English when it doesn't.

## Requirements

### Validated

- ✓ Whitelisted `/api/*` proxy with token auth, per-IP rate limiting, SQLite cache and background refresh threads
- ✓ Heatmap, court popularity, revenue, top spenders and compare views across clubs
- ✓ systemd supervision with restart backoff, a deploy-time MainPID port check and plain-English Telegram failure alerts (quick-28/29)

### Active

(None: maintenance mode, ad-hoc quick tasks only.)

### Out of Scope

- Proxying PII endpoints (`/club/follower/crud`, `/club/member/`); see `.claude/docs/padelmates-disclosure-2026-05-21.md`
- Non-Padelmates platforms (TPC Matchpoint removed 2026-07-24)

## Context

Details live in CLAUDE.md (deploy, RPi layout, clubs, headers). Deploy with `cv-deploy`; the Mac is the canonical source.
