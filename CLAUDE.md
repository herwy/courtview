# CLAUDE.md - CourtView

A Flask proxy and dashboard over the Padelmates app API, running on the RPi. Internals: `.claude/docs/courtview-internals.md` (index: `.claude/docs/INDEX.md`).

## Deploy

`cv-deploy` picks pi-cmd (LAN) or pi-wan. It stages a release in `/opt/courtview/releases/<UTC ts>/`, then gates on pip-audit (it must be installed and clean; deploy never installs anything) and on `requirements.txt` pins matching the installed packages. It flips `/opt/courtview/current`, restarts through systemd, and health-checks `GET /api/health` over HTTP with the token (`started_at` newer than the restart), MainPID owning :8766, the MainPID user being `courtview`, and data files being `600 courtview`. A failed check rolls back to the previous release automatically and exits 1 with "ROLLED BACK to <ts> (reason)". Before reporting done, verify on the RPi:

```bash
rpi 'grep -c "<expected_string>" /opt/courtview/current/courtview.html'   # must be > 0
rpi 'systemctl is-active courtview'                                        # must be active
```

Manual rollback (a bad release that passed the checks): `rpi 'cd /opt/courtview && ln -sfn releases/<previous ts> current.tmp && mv -T current.tmp current && cp current/*.service current/*.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl restart courtview'`.

Dependencies: `requirements.in` lists the top-level packages and `requirements.txt` pins the full closure (upgrade steps in its header; regenerate with `python3 -I cv_closure.py requirements.in` on the RPi).

## RPi layout

CourtView runs as the unprivileged system user `courtview` (nologin, no home) in a systemd sandbox. `systemd-analyze security courtview`: 9.6 UNSAFE before the move (2026-10-14), 1.5 OK after.

| Path | Purpose |
|------|---------|
| `/opt/courtview/releases/<ts>/`, `/opt/courtview/current` | Root-owned, read-only code (`courtview.py`, `courtview.html`, `web/`, `cv_*.py`, units). `current` is a symlink to the live release; the newest 3 plus current and previous are kept. gunicorn serves plain HTTP on 127.0.0.1:8766, with TLS terminated in front. For on-RPi curl use `http://127.0.0.1:8766/...?token=$(cat /root/.courtview_token)`; `https://localhost` hangs |
| `/var/lib/courtview/` | Data dir (courtview:courtview 0700; env `COURTVIEW_DATA_DIR`). Holds `courtview_cache.db` (SQLite cache and Racketeer archive, plus `payments` + `payments_sync`: amounts, dates, status and an HMAC `customer_key`, no personal data; tables in the internals doc; availability keeps only today..today+27; `job_state` persists the availability (6h), heatmap (24h) and payments (30 min) schedules so a restart never resets or re-runs them), `access.log` (JSONL, rotates at 1 MiB, keeps `.1` to `.5`), `status/` and `backups/`. DB, WAL and logs are 0600 |
| `/root/projects/courtview/status` | Symlink to `/var/lib/courtview/status`. `availability.json` (every 6h, retry 30 min) and `payments.json` (every 30 min), `{job,last_run,last_ok,ok,calls,errors}`, mode 0644 in a 0755 dir. labs_heartbeat (root) reads them at this path: do not move it. `GET /api/health` (token) has upstream counters, breaker state and `job_state` |
| `/var/lib/courtview-notify/` | Root-only notifier state: `unit_failure_state.json` and the Telegram alert outbox. Kept apart from the data dir so the sandboxed user cannot tamper with it |
| `/root/.courtview_token`, `/root/.courtview_hmac_key`, `/root/.telegram_token` | Root-only secret sources (0600). `LoadCredential` hands them to the units as `$CREDENTIALS_DIRECTORY/token`, `hmac_key`, `telegram_token`. The HMAC key (created by deploy.sh; never logged, never in backups) pseudonymises `customer_key` |
| `/etc/systemd/system/` | `courtview.service`, `courtview-backup.service` + `.timer` (User=courtview), `notify-telegram@.service`, `courtview-recovery@.service` + `.timer` (root). Installed from the release by deploy.sh |
| Notifier | `cv_notify_fail.py` (OnFailure of courtview.service): red "down" (still failed after 15s) or red "crash-looping" (3+ crashes in 10 min, 1h cooldown). Either opens an incident and arms `courtview-recovery@courtview.timer`, which sends one silent green "back" (needs 60s up, once per incident) and disarms itself. `cv_telegram.py` sends (retry, eth0 fallback, outbox in `/var/lib/courtview-notify`). Style: `~/dev/doxxnet-labs/.claude/docs/telegram-style.md` |
| Backups | Nightly gpg-encrypted DB backups in `/var/lib/courtview/backups/` (keep 7), pulled to the Mac's `~/Backups/courtview/` by launchd. See internals doc |

System Python only, no venv: `python3 -m pip install --break-system-packages`.

## Access

`https://courtview.labs.doxx:47200/` (Caddy -> 127.0.0.1:8766): dashboard, `/access` (access log, its html/js/css are CourtView's own files in `web/`), `/api/*` (proxy, whitelisted by `ALLOWED_PATHS`). All need the token. The app is GET/HEAD-only (other methods get 403/405, no CORS headers), and `?token=` is honoured on GET/HEAD only.

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
- Nothing here may read `/root/labs`: the access page assets live in `web/` and the Telegram sender is the vendored `cv_telegram.py`. `tests/test_security.py` fails if a root `*.py` mentions it.
- Rotate the token (or HMAC key) by writing a new file in /root, `chmod 600`, `mv` it over the source, then `systemctl restart courtview`: credentials are copied at start, and restarting CourtView is always safe. A new HMAC key only changes `customer_key` for new rows.
- Restart only with `systemctl restart courtview`; logs are in `journalctl -u courtview`. Never `pkill` gunicorn or hand-run `gunicorn --daemon`: a stray process on :8766 crash-loops the unit (44 min on 2026-10-13). After a restart, MainPID must be among the :8766 listener PIDs.
- The service can write only `/var/lib/courtview`. Never run the app or `cv_purge_pii.py` as root against it (files would turn root-owned and the service could not open them); deploy.sh runs the purge as `courtview`.
