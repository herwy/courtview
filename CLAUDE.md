# CLAUDE.md - CourtView

Padelmates API proxy server + dashboard. Flask app on RPi port 8766.

---

## Deploy

```bash
cv-deploy
```

Auto-selects pi-cmd (LAN) or pi-wan (off LAN). No session check - CourtView is always restartable.

After any change to courtview.py or courtview.html: `cv-deploy`, then verify the change actually landed on the RPi:

```bash
rpi 'grep -c "<expected_string>" /root/projects/courtview/courtview.html'
rpi 'pgrep -fa "gunicorn.*courtview\|courtview.py"'
```

The grep count must be > 0 AND pgrep must show gunicorn/courtview.py running before reporting done.

---

## RPi layout

| Path | Purpose |
|------|---------|
| `/root/projects/courtview/courtview.py` | WSGI app (served via gunicorn, port 8766) |
| `/root/projects/courtview/courtview.html` | Dashboard HTML |
| `/root/projects/courtview/courtview_cache.db` | SQLite cache: availability (28-day TTL), heatmap matrices, court popularity |
| `/root/projects/courtview/access.log` | JSONL access log |
| `/root/projects/courtview/courtview.log` | gunicorn stdout/stderr |
| `/root/projects/courtview/watchdog.sh` | Crash recovery watchdog (loops every 60s) |
| `/root/.courtview_token` | Auth token (cookie: courtview_token) |
| `/root/labs/web/` | Shared with nightwatch dashboard - access.html, access.js, dashboard.css loaded at startup |

---

## Access

| URL | Purpose |
|-----|---------|
| `http://lab.doxx:8766/` | Dashboard (token required) |
| `http://lab.doxx:8766/access` | Access log page |
| `http://lab.doxx:8766/api/*` | Padelmates API proxy (whitelist in `ALLOWED_PATHS` in courtview.py) |

Token: in Bitwarden under "courtview". Pass via `?token=...` (sets cookie) or with the cookie set.

---

## Clubs

| Club | UUID | Courts |
|------|------|--------|
| Racketeer | `5111764d9bb14be3adbdb8e133e8bd80` | 11 |
| Padium Canary Wharf | `47d2eb0db7194a9dbd29783c3a2a82ad` | 7 |
| Rocket Padel Ilford | `788fa2c66535421aabc60fd27f941c42` | 12 |

Selector lives in the dashboard nav (localStorage key `cv-club-id`). All three clubs are in both the main `CLUBS` array and `COMPARE_CLUBS` in courtview.html.

---

## Heatmap (Stats tab)

Data source: `/club/statistics/operational?club_ids=X` (1 API call per club per 24h).

**Key discovery (2026-05-21):** The param must be `club_ids` (plural). Passing `club_id` returns a MongoDB `$in needs an array` error for some duration values.

Response fields used:
- `hottest_time_slots` - hour-of-day booking density (30-min buckets, aggregated per hour)
- `week_day_wise_activity_count_combined_graph` - DOW booking counts
- `court_wise_activity_count_combined_graph` - per-court booking counts (stored in `court_popularity` table)

The heatmap DOW x hour matrix is the product of normalised hour signal x DOW signal. Stored in `heatmap_cache` table. Raw signals in `heatmap_hour_signal` and `heatmap_dow_signal` tables. Court popularity in `court_popularity` table.

Stale threshold: 24h. Background thread refreshes on startup (if stale) then every 24h.

---

## Headers

Android OkHttp headers, derived from Android APK reverse engineering (jadx). No TLS impersonation.
```
User-Agent: com.padelmates/8.5.9 (Linux; Android 14) OkHttp/4.9.0
X-Platform: android
X-Client-App-Version: 8.5.9
X-Build-Number: 1031
Accept-Encoding: gzip, deflate
```

Rationale: endpoints discovered via Android APK (com.padelmates). Version 8.5.9 / build 1031 verified from that APK. iOS was never reverse-engineered so iOS headers would be invented. Android headers are what we actually know. Both platform values return HTTP 200 from the Padelmates API.

---

## Upstream API

`https://fastapi-production-fargate.padelmates.io` - unauthenticated iOS app API.

Allowed proxy paths are whitelisted in `ALLOWED_PATHS` in courtview.py.

**Security findings (2026-05-21):** See `.claude/docs/padelmates-disclosure-2026-05-21.md`. Key facts:
- `/club/follower/crud` exposes 15,308+ user records (name, email, phone) per club with no auth. NOT proxied.
- `/club/statistics/financial` exposes revenue data. Intentionally proxied for local research use.
- `/club/member/` - 504 on full load, suspected large PII dataset. NOT proxied.

---

## Rules

- Verify deploys on the remote host with `rpi 'grep -c ...'`, never just locally.
- The SQLite cache (`*.db`) and `access.log` are RPi runtime files - never commit them.
- Web assets in `/root/labs/web/` are shared with the nightwatch dashboard (separate repo: doxxnet-labs). Do not edit them from here; CourtView only consumes them.
- No nightwatch session concept here - the Flask server is killable and restartable at any time.
- `cv-deploy` installs gunicorn if not present (`pip3 install gunicorn`) and starts via gunicorn.
