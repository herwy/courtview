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
rpi 'pgrep -fa courtview.py'
```

The grep count must be > 0 AND pgrep must show courtview.py running before reporting done.

---

## RPi layout

| Path | Purpose |
|------|---------|
| `/root/projects/courtview/courtview.py` | Flask server (port 8766) |
| `/root/projects/courtview/courtview.html` | Dashboard HTML |
| `/root/projects/courtview/courtview_cache.db` | SQLite availability cache (28-day TTL, 6h refresh thread) |
| `/root/projects/courtview/access.log` | JSONL access log |
| `/root/projects/courtview/courtview.log` | Flask stdout/stderr |
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

| Club | UUID |
|------|------|
| Racketeer | `5111764d9bb14be3adbdb8e133e8bd80` |
| Padium Canary Wharf | `47d2eb0db7194a9dbd29783c3a2a82ad` |

Selector lives in the dashboard nav (localStorage key `cv-club-id`).

---

## Upstream

`https://fastapi-production-fargate.padelmates.io` - unauthenticated public iOS app API. We forward iOS Safari headers (`X-Client-App-Version`, `X-Build-Number`, `X-Platform: ios`) via `curl_cffi`.

Allowed paths are whitelisted in `ALLOWED_PATHS` in courtview.py. Anything else returns 403.

---

## Rules

- Verify deploys on the remote host with `rpi 'grep -c ...'`, never just locally.
- The SQLite cache (`*.db`) and `access.log` are RPi runtime files - never commit them.
- Web assets in `/root/labs/web/` are shared with the nightwatch dashboard (separate repo: doxxnet-labs). Do not edit them from here; CourtView only consumes them.
- No nightwatch session concept here - the Flask server is killable and restartable at any time.
