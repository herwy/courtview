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
| `courtview_cache.db` | SQLite cache and Racketeer archive, plus `payments` + `payments_sync` (persistent, holds payment PII). Tables: see internals doc |
| `access.log` | JSONL access log |
| `cv_notify_fail.py` | OnFailure notifier: 🔴 "down" (still failed after 15s) or 🔴 "crash-looping" (3+ crashes in 10 min, 1h cooldown), plus a silent 🟢 "back" via `--check-recovery` (ExecStartPost, 90s after start, needs 60s up, once per incident). Style: `~/dev/doxxnet-labs/.claude/docs/telegram-style.md`. State `unit_failure_state.json` (0600) |
| `/etc/systemd/system/courtview.service` | `Restart=always` with backoff (`RestartSteps=5`, max 5 min), `StartLimitIntervalSec=0`, `OnFailure=notify-telegram@%p`, `ExecStartPost` recovery check. Logs go to the journal |
| `/root/.courtview_token` | Auth token (`?token=...` sets the `courtview_token` cookie) |
| `/root/labs/web/` | `access.html`, `access.js`, `dashboard.css`, owned by doxxnet-labs and loaded at startup |

System Python only, no venv: `python3 -m pip install --break-system-packages`.

## Access

`https://labs.doxx:8766/` (dashboard), `/access` (access log), `/api/*` (proxy, whitelisted by `ALLOWED_PATHS`). All need the token.

## Clubs

The `CLUBS` array in courtview.html is Racketeer, Padium Canary Wharf and Rocket Padel Ilford; `COMPARE_CLUBS` is Padium and Rocket only. The selected club is stored in localStorage `cv-club-id`.
- **Racketeer** left Padelmates on 2026-07-01 (booking revenue fell to £0). Background polling is off. Every live `/api/*` route returns 410 for it; only `/api/archive/*` serves it (archive views cover 2026-06-15 to 06-30).
- **Stratford (TPC Matchpoint)** was removed on 2026-07-24: a second platform with no revenue data, not worth maintaining.

## Upstream

Only Flask contacts Padelmates; the browser never does. Padelmates sees the RPi's IP and Android OkHttp headers. Security findings: `.claude/docs/padelmates-disclosure-2026-05-21.md`.
- `/club/follower/crud` (15,308+ users' name, email and phone) and `/club/member/` are NOT proxied.
- Revenue (`/club/statistics/financial*`) is proxied for local research.

## Rules

- Never commit `*.db` or `access.log` (RPi runtime files).
- Never edit `/root/labs/web/` from here.
- Restart only with `systemctl restart courtview`; logs are in `journalctl -u courtview`. Never `pkill` gunicorn or hand-run `gunicorn --daemon`: a stray process on :8766 crash-loops the unit (44 min on 2026-10-13). After a restart, MainPID must be among the :8766 listener PIDs.
