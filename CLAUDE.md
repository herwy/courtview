# CLAUDE.md - CourtView

A Flask proxy and dashboard over the Padelmates app API, running on the RPi. Internals: `.claude/docs/courtview-internals.md` (index: `.claude/docs/INDEX.md`).

## Deploy

`cv-deploy` picks pi-cmd (LAN) or pi-wan. It runs a pip-audit of the installed packages, copies the files, restarts through systemd, and fails unless MainPID owns :8766. Before reporting done, verify on the RPi:

```bash
rpi 'grep -c "<expected_string>" /root/projects/courtview/courtview.html'   # must be > 0
rpi 'systemctl is-active courtview'                                          # must be active
```

## RPi layout (`/root/projects/courtview/` unless noted)

| Path | Purpose |
|------|---------|
| `courtview.py`, `courtview.html` | App and dashboard. gunicorn serves plain HTTP on 127.0.0.1:8766, with TLS terminated in front. For on-RPi curl use `http://127.0.0.1:8766/...?token=$(cat /root/.courtview_token)`; `https://localhost` hangs |
| `courtview_cache.db` | SQLite cache and Racketeer archive, plus `payments` + `payments_sync` (persistent: amounts, dates, status and an HMAC `customer_key`; no personal data). Tables: see internals doc. Availability keeps only today..today+27 (past rows purged each cycle). `job_state` persists the availability (6h), heatmap (24h) and payments (30 min) schedules, so a restart never resets or re-runs them |
| `access.log` | JSONL access log |
| `status/` | `availability.json` (every 6h, retry 30 min) and `payments.json` (every 30 min), `{job,last_run,ok,calls,errors}`, mode 0644 in a 0755 dir, read by labs_heartbeat. `GET /api/health` (token) has upstream counters, breaker state and `job_state` |
| `cv_notify_fail.py` | OnFailure notifier: 🔴 "down" (still failed after 15s) or 🔴 "crash-looping" (3+ crashes in 10 min, 1h cooldown), plus a silent 🟢 "back" via `--check-recovery` (ExecStartPost, 90s after start, needs 60s up, once per incident). Style: `~/dev/doxxnet-labs/.claude/docs/telegram-style.md`. State `unit_failure_state.json` (0600) |
| `/etc/systemd/system/courtview.service` | `Restart=always` with backoff (`RestartSteps=5`, max 5 min), `StartLimitIntervalSec=0`, `OnFailure=notify-telegram@%p`, `ExecStartPost` recovery check. Logs go to the journal |
| `/root/.courtview_token` | Auth token (`?token=...` sets the `courtview_token` cookie). Rotate safely: write a temp file in /root, `chmod 600`, then `mv` it over the token. A transient empty or unreadable file keeps the last good token |
| `/root/.courtview_hmac_key` | HMAC secret for `customer_key` (0600, created by deploy.sh; never logged, never in backups) |
| `backups/` | Nightly gpg-encrypted DB backups (0700, keep 7), pulled to the Mac's `~/Backups/courtview/` by launchd. See internals doc |
| `/root/labs/web/` | `access.html`, `access.js`, `dashboard.css`, owned by doxxnet-labs and loaded at startup |

System Python only, no venv: `python3 -m pip install --break-system-packages`.

## Access

`https://courtview.labs.doxx:47200/` (Caddy -> 127.0.0.1:8766): dashboard, `/access` (access log), `/api/*` (proxy, whitelisted by `ALLOWED_PATHS`). All need the token. The app is GET/HEAD-only (other methods get 403/405, no CORS headers), and `?token=` is honoured on GET/HEAD only.

## Clubs

The `CLUBS` array in courtview.html is Racketeer, Padium Canary Wharf and Rocket Padel Ilford; `COMPARE_CLUBS` is Padium and Rocket only. The selected club is stored in localStorage `cv-club-id`.
- **Racketeer** left Padelmates on 2026-07-01 (booking revenue fell to £0). Background polling is off. Every live `/api/*` route returns 410 for it; only `/api/archive/*` serves it (archive views cover 2026-06-15 to 06-30).
- **Stratford (TPC Matchpoint)** was removed on 2026-07-24: a second platform with no revenue data, not worth maintaining.

## Upstream

Only Flask contacts Padelmates; the browser never does. Padelmates sees the RPi's IP and Android OkHttp headers. Security findings: `.claude/docs/padelmates-disclosure-2026-05-21.md`.
- `/club/follower/crud` (15,308+ users' name, email and phone) and `/club/member/` are NOT proxied. `/club/membership/member` (member lists) is never called: member counts come from `total_members` in the plan listing.
- Revenue (`/club/statistics/financial*`) is proxied for local research.
- Every call goes through `UPSTREAM` (`UpstreamClient`: 4 in flight, bounded retries, breaker, counters); `tests/test_pii.py` fails on any other `cffi_requests` or `TARGET` use. Failures, error shapes and empty fallbacks are never cached.

## Rules

- No personal data: never store or show names, emails, phones, ids, receipts. Repeat customers via customer_key HMAC only.
- Data protection, backup and restore: `.claude/docs/courtview-internals.md` (Data protection, Backup and restore).
- Payment routes only read the table and queue missing windows; the background loop does the syncing. Never sync on a request thread.
- Never commit `*.db` or `access.log` (RPi runtime files).
- Never edit `/root/labs/web/` from here.
- Restart only with `systemctl restart courtview`; logs are in `journalctl -u courtview`. Never `pkill` gunicorn or hand-run `gunicorn --daemon`: a stray process on :8766 crash-loops the unit (44 min on 2026-10-13). After a restart, MainPID must be among the :8766 listener PIDs.
