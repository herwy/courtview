# CLAUDE.md - CourtView

A Flask proxy and dashboard over the Padelmates app API, running on the RPi. `courtview.py` is the gunicorn entry; the app is the `courtview_app/` package. Reference (modules, endpoints, tests, sandbox): `.claude/docs/courtview-internals.md`. Index: `.claude/docs/INDEX.md`.

## Deploy

`cv-deploy` (picks pi-cmd on the LAN, else pi-wan). It stages a release, gates on pip-audit and pinned deps, health-checks, and rolls back automatically on failure ("ROLLED BACK to <ts>"). Gates, manual rollback and dependency upgrades: internals doc, Deploy and releases. Before reporting done:

```bash
rpi 'grep -c "<expected_string>" /opt/courtview/current/courtview.html'   # must be > 0
rpi 'systemctl is-active courtview'                                        # must be active
```

## RPi layout

Runs as the unprivileged `courtview` user in a systemd sandbox.

| Path | Purpose |
|------|---------|
| `/opt/courtview/current` | Symlink to the live release in `/opt/courtview/releases/<ts>/` (root-owned, read-only code) |
| `/var/lib/courtview/` | Data dir (0700 courtview): `courtview_cache.db`, `access.log*`, `status/`, `backups/` |
| `/root/projects/courtview/status` | Symlink to `/var/lib/courtview/status`, read by labs_heartbeat: do not move it |
| `/var/lib/courtview-notify/` | Root-only notifier state (incident file, Telegram outbox) |
| `/root/.courtview_token`, `/root/.courtview_hmac_key`, `/root/.telegram_token` | Root-only secrets (0600), passed to units via `LoadCredential` |
| `127.0.0.1:8766` | gunicorn, plain HTTP. On-RPi curl: `http://127.0.0.1:8766/...?token=$(cat /root/.courtview_token)` |

## Access

`https://courtview.labs.doxx:47200/` (Caddy -> 127.0.0.1:8766): dashboard, `/access`, `/api/*` (whitelist: `ALLOWED_PATHS` in `courtview_app/upstream.py`). Every URL needs the token. GET/HEAD only.

## Clubs

Defined once in `courtview_app/clubs.py`, served at `/api/clubs`. Padium Canary Wharf and Rocket Padel Ilford are live. Racketeer left Padelmates on 2026-07-01: live routes return 410 for it and only `/api/archive/*` serves it. Stratford (TPC Matchpoint) was removed on 2026-07-24.

## Rules

- No personal data: never store or show names, emails, phones, ids or receipts. Repeat customers via the `customer_key` HMAC only.
- Never proxy `/club/follower/crud`, `/club/member/` or `/club/membership/member` (see `.claude/docs/padelmates-disclosure-2026-05-21.md`). Never add credentials to reach endpoints that now need Padelmates auth.
- All upstream calls go through `upstream.UPSTREAM`; `tests/test_pii.py` enforces it.
- Payment routes only read the table and queue missing windows. Never sync on a request thread.
- Never commit `*.db` or `access.log` (RPi runtime files).
- Nothing here may read `/root/labs` (`tests/test_security.py` enforces it).
- Restart only with `systemctl restart courtview` (always safe); logs: `journalctl -u courtview`. Never `pkill` or hand-run gunicorn: a stray process on :8766 crash-loops the unit.
- Rotate the token or HMAC key: write a new file in /root, `chmod 600`, `mv` it over the source, restart. Credentials are copied at start.
- Never run the app or `cv_purge_pii.py` as root against `/var/lib/courtview` (files turn root-owned and the service can't open them).
