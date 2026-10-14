# CourtView internals

Verified against code and the RPi on 2026-10-14. Route list: `grep -n "ROUTES" -A12 courtview_app/routes_*.py` (registration order: `_ROUTE_ORDER` in `courtview_app/__init__.py`).

## Module map

`courtview.py` is the gunicorn entry (`app = create_app()`, then `startup._startup()` unless `COURTVIEW_SKIP_STARTUP=1`). Layers, lowest first. A module imports only lower ones, always as `from . import module` at the top, and refers to other modules qualified (`security._gate()`), so a test patches a name in the module that owns it. `tests/test_pii.py` enforces the import rule.

| Module | Holds |
|---|---|
| `clubs` | `CLUBS` table, derived `HEATMAP_CLUBS`, `PAYMENT_CLUB_IDS`, `ARCHIVE_CLUB_IDS`, `RACKETEER_CLUB_ID`, `public_clubs()` |
| `config` | `ROOT_DIR` (release root), data/credential paths, cookie and timing constants, `_now` |
| `upstream` | `UpstreamClient`, `UPSTREAM`, `ALLOWED_PATHS`, `APP_HEADERS` (the only `curl_cffi` importer) |
| `privacy` | PII scrubbing, HMAC key and `customer_key` |
| `db` | SQLite connection, schema, stats and availability caches, single-flight locks |
| `jobs` | `job_state` schedule and status JSON files |
| `accesslog` | IP geo, ring buffer, rotating `access.log`, `web/` assets, `log_req` hook |
| `security` | token gate, rate limit, CSP and headers, request hooks, `_exc_error` |
| `payments` | payment sanitising, sync engine, queue, background loop |
| `refresh` | availability and heatmap refresh loops |
| `routes_core`, `routes_availability`, `routes_club`, `routes_payments`, `routes_archive` | views plus a `ROUTES` table each |
| `startup` | `_startup()` (schema, assets, threads) |

`create_app()` registers hooks in this order: before `_allow_only_get_head`, `_block_archived_club`; after `_security_headers`, `log_req`; 404 handler `_not_found`. `/api/clubs` returns `{"clubs": [...]}` with only `id, name, sub, short, courts, archived, compare`. The dashboard loads it in `boot()` before anything club-dependent runs; courtview.html holds no club ids.

Dashboard data layer: every request goes through `api(url, {key, timeoutMs})` in courtview.html. Timeouts are 20 s, or 60 s for the slow aggregate endpoints. It parses JSON and rejects with `ApiError` (`kind/status/error`). A new request with the same `key` aborts the previous one. Preloads and warm-up run through `whenVisible()` and wait while the tab is hidden; `pollWatches` keeps its interval.

## Padelmates API

Base `https://fastapi-production-fargate.padelmates.io`. Every call goes through `UPSTREAM` (`UpstreamClient`, the only code that touches `cffi_requests` or `TARGET`; `AstTests` in `tests/test_pii.py` enforces it). It sends `impersonate="chrome110"` plus Android OkHttp headers (`com.padelmates/8.5.9`, build 1031, `X-Platform: android`) taken from the APK. iOS headers were never reverse-engineered, so don't invent them. The browser never contacts Padelmates.

`UpstreamClient` rules:
- Pooled `curl_cffi` Sessions, at most 4 requests in flight (`BoundedSemaphore`, 20 s wait, else `UpstreamUnavailable`).
- Retries (max 2): connection errors, 502/503/504 and 429. Backoff 0.5 s then 2 s, each x uniform(0.5, 1.5); the slot is released during backoff. `Retry-After` (integer seconds) is honoured up to 10 s; a larger value returns the 429 unretried. A 500 and other 4xx are not retried.
- Breaker: 5 consecutive failed calls (exception, 5xx, 429 after retries) open it for 300 s, then one half-open probe. A non-429 4xx counts as reachable.
- Counters per path (calls, ok, non_200, errors, unauthorized, last_status, last_at) plus totals (retries, rejected, unauthorized) and breaker state, all in `/api/health`. Logs carry path and status only, never the query string or an exception message.
- 401: never retried, cached or counted as a breaker failure. It is logged once per endpoint per process (`[upstream] auth required status=401 path=...`), counted as `unauthorized`, and relayed by the generic proxy as 502 `{error: "upstream_auth_required"}`. CourtView must not add credentials to reach such endpoints.
- Retired on 2026-10-14 when Padelmates started requiring auth: `/club/club_extras` and `/home/activity/get_booked_hours`. No code calls them, and `/api/booked-hours` answers 410 `{error: "unavailable"}` after the token gate. The UI shows "No longer available from Padelmates".
- Test seam: patch `cv.upstream.UPSTREAM._new_session` with an object whose `get(url, **kw)` returns the fake response, and call `cv.upstream.UPSTREAM.reset()` in `setUp`.

| Endpoint | Used for |
|---|---|
| `/player/player_booking/all_courts_slot_prices_v3` | Availability grid; `reservedBookings` covers all booking types |
| `/home/activity/filtered_activities` | Day bookings, activity mix, training rates |
| `/club/statistics/financial/v2` | Revenue cards, DOW bars, subscriptions |
| `/club/statistics/online_payment_history` | Payment sync into the local `payments` table |
| `/club/statistics/coach` | Coaching stats |
| `/club/statistics/operational` | Heatmap and court popularity |
| `/club/`, `/club/creditpackage/` | Club Info profile and credit packages |
| `/club/membership/` | Plan listing; `total_members` gives per-plan member counts |

The generic proxy (`/api/<path>`) passes only exact `ALLOWED_PATHS` matches: the above plus `search_clubs` and `/club/statistics/financial`.

Quirks:
- `operational` needs `club_ids` (plural). `club_id` gives a MongoDB `$in needs an array` error.
- `filtered_activities` and `online_payment_history` reject `limit` > 100 with a 422.
- `online_payment_history` returns oldest first, about 100 per day per club, and rejects ranges over about 31 days.
- `activity_type` must be `Book Court`, `Match`, `Tournament` or `Training`. `Match` returns `Book Court|OPEN_MATCH` records.
- `financial/v2`: `total_combined_subscription_payments` is always 0 and `total_combined_payments` excludes subscriptions. The real figure is `total_current_subscription_amount`; the dashboard's `revTotalInclSubs()` adds it for the Revenue and Compare tabs.
- Rocket Padel Ilford times out on full-month activity ranges. The backend retries with the last 14 days.

## Racketeer guard

`security._block_archived_club` (`before_request`) returns 410 `{error: "archived"}` for any `/api/` path except `/api/archive/` when `club_id` or `club_ids` is Racketeer, cache-only routes included. The token is checked first without recording failures, so unauthenticated callers still get 403. Each 410 logs `[archived] 410 METHOD path`; a clean UI load produces none. Racketeer payments come from `archive_financial` only (`/api/archive/financial?endpoint=payment-history`), sanitised.

## Payment history paging

Revenue walks back by Europe/London day within the selected month, newest day first, paging `skip += 100` within a day while `has_more`. `/api/payment-history` reads the local `payments` table (limit <= 100, skip <= `PAYMENTS_MAX_SKIP` = 20,000, newest first). Rows are `{id, createdAt, description, amount, status}` and `has_more` is exact. One chunk fetches until 25 rows, 7 requests or the month start, on the initial load and on every Load More. The logic is the DOM-free block between `PAY-PAGER:BEGIN` and `PAY-PAGER:END` in courtview.html.

## Payments table and sync

- `payments`: PK `(club_id, transaction_id)`. Columns: `created_utc` (epoch ms), `local_day` (Europe/London), `created_at` (raw upstream string), `customer_key`, `source_type`, `description`, `amount` (raw string), `amount_gbp`, `status`, `synced_at`. Indexes `(club_id, created_utc)` and `(club_id, customer_key)`. A missing upstream id falls back to `synthetic:` + first 24 hex of sha256(createdAt|customer_key|amount|status).
- `customer_key` = hex HMAC-SHA256(secret, lower-cased stripped email). Upstream payments carry no player id, so the email is the only stable basis. The secret is `/root/.courtview_hmac_key` (created by deploy.sh, never logged, not backed up), read as `$CREDENTIALS_DIRECTORY/hmac_key`. If it is missing or short, payment storage is disabled and payment routes return 503. A new key changes `customer_key` for new rows only.
- `description` is cleaned of the customer's name tokens, email and phone. It is kept only if its template (digits, month names and tz labels abstracted) is in `DESCRIPTION_TEMPLATES` (31 templates); otherwise it becomes the `source_type`.
- `payments_sync`: one row per `(club_id, local_day)` with `synced_at`, `complete`, `pages`, `rows`.
- `_fetch_payment_page` is the only caller of `online_payment_history` and returns `_sanitise_payment` allowlisted records only.
- Sync unit is one London day, paged `skip += 100` with 0.1-0.25 s jitter; up to 3 days in parallel; a 200-page cap marks the day incomplete.
- Freshness: a day synced < 10 min ago is skipped. Otherwise it syncs unless final (`complete=1` and synced after day close + 10 min).
- Background loop (`_payments_sync_loop`): previous + current London month per `HEATMAP_CLUBS` club every 30 min, non-final days only. It also drains the on-demand queue.
- Club allowlists: payment-history, payment-leaderboard and membership-members accept `PAYMENT_CLUB_IDS` (Padium, Rocket) only (400 `club not allowed`). `/api/archive/financial` accepts `ARCHIVE_CLUB_IDS` (Racketeer) only.
- `/api/payment-history` and `/api/payment-leaderboard` never sync. They return `complete`, `synced_through` (oldest `synced_at` among non-final days, ISO 8601 Z) and `queued`. An incomplete window is queued (`_enqueue_payment_window`, max 16, deduplicated) and the loop syncs it within seconds. The Revenue tab polls the leaderboard every 15 s (max 8) while `queued`.
- `/api/payment-leaderboard` groups `PAYMENT_SUCCESS` rows by `customer_key` and returns the top 5 as `{customer_id, bookings, total, pct}`. `customer_id` is the first 4 hex of `customer_key`, upper-case ("Customer #XXXX").
- Payment search and coach bios were removed because they needed names.

## Tests

Unit tests need Flask and curl_cffi on Python 3.10+, so run them on the RPi from a temp copy:

```bash
D=$(rpi 'mktemp -d'); COPYFILE_DISABLE=1 tar --exclude __pycache__ -cf - courtview.py courtview.html cv_*.py courtview_app web tests/*.py | rpi "tar xf - -C $D"
rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail tests.test_cv_telegram tests.test_paths tests.test_clubs; rm -rf $D"
```

- Each module sets `COURTVIEW_DATA_DIR` to a temp dir. `tests/cvload.py` loads a fresh package copy per module (`cv = cvload.load(alias)`).
- Pager: `node --test tests/test_pay_pager.mjs`. UI: `tests/e2e_dashboard.py` (Playwright, needs `CV_TOKEN`).
- New upstream shapes need a test in `tests/test_pii.py`: scrubbing cannot catch a name under an unknown key.

## Data protection

- No personal data is stored or returned. `_scrub_pii` (recursive) drops name/email/phone/id/receipt keys, person containers (players, participants, members, coaches, ...) and `PLAYER:` titles. `_redact_text` removes emails and UK phone numbers from remaining strings. Both run on upstream JSON before it is cached or returned, and again on read for availability, club-info and archive payloads.
- Allowlists where the shape is known: payment records (`_sanitise_payment`), `/api/day-activities` (`_activity_view`), `/api/membership-members` (counts only; member lists are never fetched).
- `/api/coach-stats` anonymises coaches as "Coach 1..n" by sessions desc, with a pseudonymous `coach_id` and numeric stats only.
- `PRAGMA secure_delete=ON` on every connection. `cv_purge_pii.py` is idempotent and records `schema_meta.pii_purge`; deploy.sh runs it as `courtview` while `--check` says it is needed.
- DB, `-wal`, `-shm` and `access.log*` are `600 courtview`; both CourtView units set `UMask=0077`.

## Backup and restore

- `courtview-backup.timer` (03:30 +- 10 min, `Persistent=true`) runs `cv_backup.py` as `courtview` (no network, exposure 1.2): sqlite backup API, `integrity_check`, `gpg --encrypt --recipient-file courtview-backup.pub.asc`, then `/var/lib/courtview/backups/courtview-YYYYMMDD.db.gpg` (0600, keep 7). Only the public key is on the RPi.
- gpg key "CourtView Backup 2026-10 RSA", fingerprint `CFA832B6B2983DD121CACB830317325FB7C4A6F9` (rsa4096, no passphrase, no expiry). The private key is in the Mac keyring and the user's password manager. Never print it.
- RSA because the earlier cv25519 key failed export/import under GnuPG 2.5.24. Test any new key with a fresh-`GNUPGHOME` round trip.
- The Mac pulls via launchd (`launchd/net.doxx.courtview-backup-pull.plist`, 09:15 daily plus RunAtLoad) running `scripts/courtview-backup-pull.sh`. It tries pi-cmd then pi-wan and copies new files to `~/Backups/courtview/` (keep 7). `pull.log` warns when the newest file is older than 36 h; it is the only staleness signal.
- Restore check: `zsh scripts/courtview-backup-verify.sh` decrypts the newest backup in a temp dir and prints `integrity_check` and PII counts.
- Restore: `scripts/courtview-restore.sh [--key KEYFILE] [BACKUP]` decrypts and checks; `--apply` then swaps the DB on the RPi (stop, drop -wal/-shm, chmod 600, start).
- The HMAC key is deliberately not backed up: keeping it beside the data would weaken the pseudonymisation.

## Heatmap

One `operational` call per club every 24h (Padium and Rocket). The payload is validated first: all three arrays must be lists and not all empty, else nothing is written and the run retries after 30 min. It uses `hottest_time_slots` (30-min buckets aggregated per hour), `week_day_wise_activity_count_combined_graph` (DOW) and `court_wise_activity_count_combined_graph` (court popularity). The DOW x hour matrix is normalised hour signal x DOW signal.

## SQLite cache (`courtview_cache.db`, WAL)

| Table | TTL |
|---|---|
| `availability` | London days today..today+27 only. Refreshed every 6h (28 days x 2 clubs); `_purge_availability` deletes past-day, non-numeric-key and Racketeer rows each cycle |
| `heatmap_cache`, `heatmap_hour_signal`, `heatmap_dow_signal`, `court_popularity` | 24h |
| `membership_members_cache` (counts only), `stats_cache` | 6h |
| `club_info_cache` | 10 min |
| `payments`, `payments_sync` | persistent |

- `job_state` (job, last_run, ok, calls, errors, updated_at, last_ok) persists the `availability` (6h), `heatmap` (24h) and `payments` (30 min) schedules, so a restart neither resets nor re-runs them. A failed run is due again after 30 min. With no row it is seeded from data; an empty DB is due now. `last_ok` is the start of the latest ok run, carried unchanged on a failed run. Loops re-check every <= 5 min and log `[job] next run in Nm (persisted)`.
- Never cached: failures, error shapes and empty fallbacks. Club-info and membership failures serve the last stored row with `X-CourtView-Stale: 1`, else the partial result uncached (club-info) or 502 (membership). Club-info, membership and availability misses are single-flight (`_single_flight_lock`).
- `_stats_key()` rounds `end` to a 6h bucket; without it every millisecond-precision request misses.
- `get_cached` treats an empty `[]` payload as a miss.
- `archive_heatmap`, `archive_club_info` and `archive_financial` hold Racketeer only, 2026-06-15 to 06-30; nothing writes them. `archive_heatmap.hour_norm` and `dow_norm` are NULL, so the archived signal chart is empty.

## Dashboard gotchas

- Court grid tooltips: the `.tt` inside `.cell` is a template. Show the body-level portal, because `.grid-scroll` overflow clips the inline one.
- Archive selector (Racketeer only, Club Info, Stats and Revenue tabs): CSS defaults it to `display:none`, so `showArchiveSelector` must set `'flex'`, not `''`. It has no Live option and auto-loads the latest snapshot. `switchClub()` must hide all three selectors. For Racketeer every `load*` returns after its archive branch.
- Membership plans: `hidden=True` plans still show, and `price=None` shows as "Free / partner". Rocket's paid plans are all hidden; Padium has only corporate plans.
- `/?token=x` sets the cookie and redirects (302) to `/`, stripping the token from the URL.
- `_read_token` is keyed on `(st_mtime_ns, st_size)` and keeps the last good token when the file is empty, unreadable or missing. Transitions log `[token] ...` (never the value). With no good token everything is denied.
- Tabs preload after availability loads; other clubs warm up after 6 s.

## Status JSON and health

- `status/availability.json` (every 6h, retry 30 min) and `status/payments.json` (every 30 min) hold exactly `{job, last_run, last_ok, ok, calls, errors}`, timestamps ISO 8601 Z, `last_ok` null if never ok. `_rewrite_status_files()` regenerates both from `job_state` at startup. Writes are atomic, mode 0644 in a 0755 dir (set explicitly because of `UMask=0077`). `COURTVIEW_STATUS_DIR` overrides the directory.
- labs_heartbeat (root) reads them via `/root/projects/courtview/status`, a symlink to `/var/lib/courtview/status`.
- An availability run is `ok` unless the breaker stopped it or at least half the calls failed.
- `GET /api/health` (token): `started_at`, `breaker`, `totals`, `endpoints` (paths only) and `jobs`. No personal data or query strings.
- `GET /api/availability-fresh?club_id&start_datetime&end_datetime`: availability at most 5 min old (single-flight), live clubs only, start within now-36h..now+48h, span <= 26h. Watches use it instead of the 6h cache. On upstream failure it serves the stored row with `X-CourtView-Stale: 1`, else 502.

## Security headers and access log

- GET/HEAD only: `_allow_only_get_head` returns 403 for other methods, `?token=` is ignored on them, and no response carries `Access-Control-*` headers. Failed token checks are rate-limited per IP (10 per 60 s).
- Every response carries `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. `/api/*` and `/access/data` also get `Cache-Control: no-store`.
- CSP has three profiles, set with `setdefault` so `_forbidden`'s own CSP survives: `/` gets `CSP_DASHBOARD_TMPL`, `/access` gets `CSP_ACCESS`, everything else `CSP_API` (`default-src 'none'`).
- Dashboard `script-src` is `'self'` plus one `'sha256-...'` computed at load from the single inline `<script>`. A second inline script drops it to `'self'`; the CSP never loosens. There are no inline `on*=` handlers and `tests/test_security.py` fails if one comes back. `style-src` keeps `'unsafe-inline'` because of many static and generated `style=` attributes.
- Static route (`/<path:filename>`) is token-gated first (no 404 oracle), rejects `..`, backslash, `%`, NUL, leading `/` and raw `%2e/%2f/%5c`, resolves with `safe_join`, then applies an extension allowlist. Flask's `/static` is disabled (`static_folder=None`), the 404 handler is gated, and `merge_slashes` is off, so every URL answers 403 without a token.
- `MAX_CONTENT_LENGTH` is 1 MiB.
- The generic proxy serves a body that parses as JSON as `application/json`; anything else becomes 502 `{error: "upstream returned non-JSON"}`. Error JSON never carries exception text: `_exc_error` returns 500 `internal error` or 502 `upstream unavailable` and logs `[error] <ExceptionType> path=...`.
- `tests/test_security.py` walks `app.url_map`: every endpoint must be in `EXPECTED_ENDPOINTS` and return 403 without a token. `PUBLIC_ENDPOINTS` is an empty frozenset, so a new or public route fails until someone decides.
- Access log labels come from the gate result (`g.cv_auth`: OK, TOKEN, BLOCKED, RATE_LIMITED). `access.log` is 0600, rotated at 1 MiB keeping `.1` to `.5`. The access page, `access.js` and `dashboard.css` are CourtView's own files in `web/`, loaded at startup.

## Runtime user and sandbox

- `courtview.service` runs as `courtview` (system user, nologin, home `/nonexistent`) from `/opt/courtview/current` with `PYTHONDONTWRITEBYTECODE=1`. gunicorn: 1 gthread worker, 4 threads, `127.0.0.1:8766`, plain HTTP behind Caddy.
- Data: `StateDirectory=courtview` (`/var/lib/courtview`, 0700). Secrets: `LoadCredential` copies `/root/.courtview_token` and `/root/.courtview_hmac_key` at start. Lookup order: `COURTVIEW_TOKEN_PATH`/`COURTVIEW_HMAC_KEY_PATH` env, then `$CREDENTIALS_DIRECTORY/<name>`, then the /root path.
- Sandbox: ProtectSystem=strict (only `/var/lib/courtview` writable), ProtectHome, PrivateTmp/Devices, kernel and cgroup protections, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `SystemCallFilter=@system-service`, empty capability sets, NoNewPrivileges, MemoryDenyWriteExecute, MemoryMax=512M. `systemd-analyze security courtview`: 1.5 OK.
- Re-run a `systemd-run` probe with the full property set (imports, a curl_cffi chrome110 GET, `gunicorn --check-config`, gpg) after upgrading curl_cffi or Python: MemoryDenyWriteExecute could break them.
- ExecStart passes `--no-control-socket`: gunicorn 26 otherwise tries to create a socket under the home dir.
- Restart backoff: `RestartSec=5`, `RestartSteps=5`, `RestartMaxDelaySec=300`, `StartLimitIntervalSec=0` (never gives up).
- The notifier units (`notify-telegram@`, `courtview-recovery@`) stay root: they need systemctl, journalctl and SO_BINDTODEVICE. Their state is `/var/lib/courtview-notify` (root 0700), outside the data dir the sandboxed user owns.

## Deploy and releases

- `deploy.sh` (via `cv-deploy`) uploads to `/opt/courtview/releases/<UTC ts>.partial`, normalises owner and modes, compiles, and renames it to `<ts>`.
- Gates: pip-audit must be installed and clean, and `requirements.txt` pins must match the installed packages. Deploy never installs anything.
- It then installs units, runs the PII purge check as `courtview`, flips `/opt/courtview/current` (ln + `mv -T`) and restarts.
- Health checks: authenticated `GET /api/health` over HTTP with `started_at` newer than the restart, MainPID among the :8766 listeners, MainPID user `courtview`, data files `600 courtview`.
- Any failure renames the release `<ts>.failed`, restores the previous symlink and units, restarts, and exits 1 with "ROLLED BACK to <ts> (reason)".
- Kept releases: the newest 3 plus current and previous.
- Manual rollback (a bad release that passed the checks): `rpi 'cd /opt/courtview && ln -sfn releases/<previous ts> current.tmp && mv -T current.tmp current && cp current/*.service current/*.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl restart courtview'`.
- Dependencies: system Python, no venv (`python3 -m pip install --break-system-packages`). `requirements.in` lists top-level packages and `requirements.txt` pins the full closure; the upgrade steps are in its header (regenerate with `python3 -I cv_closure.py requirements.in` on the RPi).
- The 2026-10-14 migration from `/root/projects/courtview` is finalised. Only a README and the `status` symlink remain there.

## Notifier

- `cv_notify_fail.py` runs as `OnFailure` of courtview.service. It sends a red "down" alert if the unit is still failed after 15 s, or a red "crash-looping" alert on 3+ failures in 10 min (1h cooldown).
- A red alert (sent or queued) opens an incident and starts `courtview-recovery@courtview.timer` (`OnBootSec=2min`, `OnUnitActiveSec=1min`). Its `--check-recovery` run sends one silent green "back" once the unit has been active 60 s. The next run, with no incident and an empty outbox, stops the timer.
- `cv_telegram.send_telegram(message, silent=...)` tries the default route, then eth0 (SO_BINDTODEVICE), and retries the whole send once after 3 s. A 4xx other than 429 is permanent. Other final failures are logged (`[cv_telegram] send failed: ...`, never the token or text) and queued in `alert_outbox.json` (0600, entries older than 24 h dropped). `flush_outbox()` runs at the start of every notifier invocation.
- Alert outcomes log as `[cv_notify_fail] <label> alert <sent|queued>`.
- Message style: `~/dev/doxxnet-labs/.claude/docs/telegram-style.md`.
