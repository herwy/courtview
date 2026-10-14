# CourtView internals

Verified against code 2026-10-14. Route list: `grep -n "@app.route" courtview.py`.

## Padelmates API

Base `https://fastapi-production-fargate.padelmates.io`, no auth. Every call goes through `UPSTREAM` (`UpstreamClient`, the only code that touches `cffi_requests` or `TARGET`; `AstTests` enforces it) with `impersonate="chrome110"` plus Android OkHttp headers (`com.padelmates/8.5.9`, build 1031, `X-Platform: android`) taken from the APK. iOS headers were never reverse-engineered, so don't invent them.

`UpstreamClient` rules:
- Pooled `curl_cffi` Sessions, at most 4 requests in flight (`BoundedSemaphore`, 20 s wait, else `UpstreamUnavailable`).
- Retries (max 2): connection errors, 502/503/504 and 429. Backoff 0.5 s then 2 s, each x uniform(0.5, 1.5). `Retry-After` (integer seconds) is honoured up to 10 s, a larger value returns the 429 unretried. A 500 and other 4xx are not retried. The slot is released during backoff.
- Breaker: 5 consecutive failed calls (exception, 5xx, 429 after retries) open it for 300 s, then one half-open probe. A non-429 4xx counts as reachable.
- Counters per path (calls, ok, non_200, errors, last_status, last_at) plus totals (retries, rejected) and breaker state in `/api/health`. Logs carry path and status only (`[upstream] non-200 status=401 path=/club/club_extras`), never the query string or an exception message.
- On 2026-10-14 `/club/club_extras` and `/home/activity/get_booked_hours` answered 401 upstream (same with a plain `cffi_requests.get`), so the Club Info extras and booked-hours KPIs come back empty or 502. Not caused by the client.
- Test seam: patch `cv.UPSTREAM._new_session` with an object whose `get(url, **kw)` returns the fake response, and call `cv.UPSTREAM.reset()` in `setUp`.

| Endpoint | Used for |
|---|---|
| `/player/player_booking/all_courts_slot_prices_v3` | Availability grid; `reservedBookings` covers all booking types (v2 had open matches only) |
| `/home/activity/filtered_activities` | Day bookings, activity mix, training rates |
| `/home/activity/get_booked_hours` | Monthly booked-hours KPIs |
| `/club/statistics/financial/v2` | Revenue cards, DOW bars, subscriptions |
| `/club/statistics/online_payment_history` | Payment sync into the local `payments` table (history paging and top spenders read it) |
| `/club/statistics/coach` | Coaching stats |
| `/club/statistics/operational` | Heatmap and court popularity |
| `/club/global_search` | Coach bios |
| `/club/membership/` | Plan listing; `total_members` gives per-plan member counts. `/club/membership/member` (member lists) is never called |

Quirks:
- `operational` needs `club_ids` (plural). `club_id` gives a MongoDB `$in needs an array` error.
- `filtered_activities` and `online_payment_history` reject `limit` > 100 with a 422.
- `online_payment_history` returns oldest first, about 100 per day per club, and rejects ranges over about 31 days. See Payment history paging below.
- `activity_type` must be `Book Court`, `Match`, `Tournament` or `Training`. `Match` returns `Book Court|OPEN_MATCH` records.
- `financial/v2`: `total_combined_subscription_payments` is always 0 and `total_combined_payments` excludes subscriptions. The real subscription figure is `total_current_subscription_amount` (= sum of `membership_subscription`). The dashboard's `revTotalInclSubs()` adds it to the total for the Revenue and Compare tabs.
- Rocket Padel Ilford times out on full-month activity ranges. The backend retries with the last 14 days.

## Racketeer guard

`@app.before_request _block_archived_club` returns 410 `{error: "archived"}` for any `/api/` path except `/api/archive/` when `club_id` or `club_ids` is Racketeer. Cache-only routes (`/api/month`, `/api/heatmap`, `/api/court-popularity`) are included. Racketeer payments are served from `archive_financial` only (`/api/archive/financial?endpoint=payment-history`), sanitised, never contacting upstream. The token is checked first without recording failures, so unauthenticated callers still get 403. Each 410 logs `[archived] 410 METHOD path` to the journal; a clean UI load produces none.

## Payment history paging

Revenue walks back by Europe/London day within the selected month, newest day first. Within a day it pages `skip += 100` while `has_more`. `/api/payment-history` reads the local `payments` table (limit <= 100, skip <= `PAYMENTS_MAX_SKIP` = 20,000, newest first); rows are `{id, createdAt, description, amount, status}` and `has_more` is exact. One chunk fetches until 25 rows, 7 requests or the month start; the same rule applies to the initial load and every Load More click. Rows are deduplicated, sorted newest first and shown in UK local time. The logic is the DOM-free block between `PAY-PAGER:BEGIN` and `PAY-PAGER:END` in courtview.html, tested by `node --test tests/test_pay_pager.mjs`. `tests/e2e_dashboard.py` (Playwright, needs `CV_TOKEN`) checks the Racketeer and Padium UI.

## Payments table and sync

- `payments`: PK `(club_id, transaction_id)`. Columns: `created_utc` (epoch ms), `local_day` (Europe/London), `created_at` (raw upstream string), `customer_key`, `source_type`, `description`, `amount` (raw string), `amount_gbp`, `status`, `synced_at`. Indexes `(club_id, created_utc)` and `(club_id, customer_key)`. A missing upstream id falls back to `synthetic:` + first 24 hex of sha256(createdAt|customer_key|amount|status), never an email.
- `customer_key` = hex HMAC-SHA256(secret, lower-cased stripped email). Upstream payment records carry no player id (keys: amount, createdAt, description, player_email, player_name, player_phone, receiptUrl, source_type, status, transaction_id), so the email is the only stable basis. The secret is `/root/.courtview_hmac_key` (0600, outside repo and DB, never logged, not in backups). If it is missing or short, payment storage is disabled and payment routes return 503. Losing it only changes keys for new rows.
- `description` is cleaned of the customer's own name tokens, email and phone, then kept only if its template (digits, month names and tz labels abstracted) is in `DESCRIPTION_TEMPLATES` (31 templates shared by >= 5 customers, scan of 2026-10-14); otherwise it becomes the `source_type`.
- `payments_sync`: one row per `(club_id, local_day)` with `synced_at`, `complete`, `pages`, `rows`.
- `_fetch_payment_page` is the only caller of `online_payment_history` (APP_HEADERS, chrome110, timeout 15, limit <= 100) and returns sanitised records only (`_sanitise_payment` allowlist), so raw name, email, phone and receipt never reach a caller.
- Sync unit is one London day, paged `skip += 100` with 0.1-0.25 s jitter; up to 3 days in parallel; 200 pages/day cap marks the day incomplete.
- Freshness: skip a day synced < 10 min ago. Otherwise sync unless final (`complete=1` and synced after day close + 10 min). Today/yesterday refresh at most every 10 min; failed days retry at most every 10 min.
- Background loop (`_payments_sync_loop`): runs previous + current London month per `HEATMAP_CLUBS` club every 30 min (schedule persisted in `job_state`, so a restart does not re-run it), touching only non-final days. It also drains the on-demand queue below.
- Club allowlists: payment-history, payment-leaderboard and membership-members accept `PAYMENT_CLUB_IDS` (Padium, Rocket) only (400 `club not allowed`; Racketeer gets 410 first). `/api/archive/financial` accepts `ARCHIVE_CLUB_IDS` (Racketeer) only.
- `/api/payment-history` and `/api/payment-leaderboard` never sync: they read the table and return `complete` (every London day in the window has `complete=1`), `synced_through` (ISO 8601 Z, oldest `synced_at` among non-final days, the window end when all are final, null when a day has no row) and `queued`. An incomplete window is queued (`_enqueue_payment_window`, max 16, deduplicated per club and day range) and the loop thread syncs it within seconds; the Revenue tab polls the leaderboard every 15 s (max 8) while `queued` and shows "Data up to HH:MM". `/api/payment-leaderboard` groups `PAYMENT_SUCCESS` rows by `customer_key` and returns the top 5 as `{customer_id, bookings, total, pct}`; `customer_id` is the first 4 hex of `customer_key`, upper-case (the dashboard shows "Customer #XXXX"). Rows without a key are skipped.
- Payment search and coach bios were removed (they needed names).
- Unit tests (`tests/test_payments.py`, `tests/test_pii.py`, `tests/test_purge.py`, `tests/test_reliability.py`, `tests/test_auth.py`, `tests/test_cv_notify_fail.py`) need Flask and curl_cffi on Python 3.10+, so run them on the RPi from a temp copy of every root `*.py` (courtview.py, cv_notify_fail.py, cv_purge_pii.py, cv_backup.py), courtview.html and `tests/`: `D=$(rpi 'mktemp -d'); tar cf - courtview.py courtview.html cv_*.py tests/*.py | rpi "tar xf - -C $D"; rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail"`, then remove the temp dir.
- `tests/test_reliability.py` covers the client (retries, breaker, cap, log hygiene), never-cache and single-flight, health, job_state, the availability window and purge, status JSON, read-only payment routes and the fresh-availability route.

## Data protection

- No personal data is stored or returned. `_scrub_pii` (recursive) drops name/email/phone/id/receipt keys, person containers (players, participants, members, coaches, ...) and `PLAYER:` titles, and `_redact_text` removes emails and UK phone numbers from every remaining string. It runs on upstream JSON before it is cached or returned (generic proxy, availability cache, club-info, booked-hours, revenue-summary) and again on read for availability, club-info and archive payloads.
- Allowlists instead of scrubbing where the shape is known: payment records (`_sanitise_payment`), `/api/day-activities` (`_activity_view`: timing, courts, type, spots, price), `/api/membership-members` (counts only).
- Coaches are anonymised in `/api/coach-stats`: "Coach 1..n" ordered by sessions desc, a pseudonymous `coach_id` (also the `coach_rates` key in activity-summary) and numeric stats only.
- `PRAGMA secure_delete=ON` on every connection. `cv_purge_pii.py` (run by deploy.sh while `--check` says it is needed; stops the service, rebuilds `payments`, rewrites archive payloads, scrubs or deletes availability, clears the stats/club-info/membership caches, then `wal_checkpoint(TRUNCATE)` + `VACUUM`) is idempotent and records `schema_meta.pii_purge`.
- Files: DB, `-wal`, `-shm` and `access.log` are 0600 (deploy.sh fixes existing modes and fails if any is not 600); `courtview.service` and `courtview-backup.service` set `UMask=0077`.
- Scrubbing cannot catch a personal name under an unknown key; new upstream shapes need a test in `tests/test_pii.py`.

## Backup and restore

- `courtview-backup.timer` (03:30 UK +- 10 min, `Persistent=true`) runs `cv_backup.py`: sqlite backup API, `integrity_check`, `gpg --encrypt --recipient-file courtview-backup.pub.asc`, then `backups/courtview-YYYYMMDD.db.gpg` (0600, keep 7). Only the public key is on the RPi; the plaintext temp file is always removed.
- gpg key "CourtView Backup 2026-10 RSA <courtview-backup@doxx.local>", fingerprint `CFA832B6B2983DD121CACB830317325FB7C4A6F9` (rsa4096 sign + rsa4096 encrypt, no passphrase, no expiry). The private key is in the Mac keyring and exported to the user's password manager. Never print it: export straight to a file.
- Why RSA: the first key (`0C6DF971…`, ed25519/cv25519, 2026-10-14) didn't survive export/import under GnuPG 2.5.24 (the cv25519 keygrip changes on import, leaving a `ssb#` stub that can't decrypt). It was also partly exposed in a session transcript, so it was rotated the same day and its backups deleted. Test any future key with a fresh-`GNUPGHOME` round trip before relying on it.
- The Mac pulls via launchd (`launchd/net.doxx.courtview-backup-pull.plist`, 09:15 daily plus RunAtLoad) running `scripts/courtview-backup-pull.sh`: tries `pi-cmd` then `pi-wan`, copies new files to `~/Backups/courtview/` (0700 dir, 0600 files, keep 7), logs to `pull.log` and warns when the newest remote file is older than 36 h. That log is the only staleness signal (the timer has no `OnFailure`).
- Restore check: `zsh scripts/courtview-backup-verify.sh` decrypts the newest backup in a temp dir, prints `integrity_check`, the PII column count and email/mobile byte counts, then deletes the plaintext.
- Restore: `scripts/courtview-restore.sh [--key KEYFILE] [BACKUP]` decrypts (newest in `~/Backups/courtview` by default) and checks integrity; add `--apply` to confirm, copy to the RPi, stop the service, swap the DB (dropping -wal/-shm), `chmod 600` and start it again.
- The HMAC key is deliberately not backed up: keeping it beside the data would weaken the pseudonymisation. After a restore on a new host a fresh key changes `customer_key` for new rows only.

## Heatmap

One `operational` call per club every 24h (background thread, schedule in `job_state`; Padium and Rocket only). The payload is validated before any write: all three arrays must be lists and not all empty, otherwise nothing is written and the run is retried after 30 min. It uses `hottest_time_slots` (30-min buckets aggregated per hour), `week_day_wise_activity_count_combined_graph` (DOW) and `court_wise_activity_count_combined_graph` (court popularity). The DOW x hour matrix is normalised hour signal x DOW signal.

## SQLite cache (`courtview_cache.db`, WAL)

| Table | TTL |
|---|---|
| `availability` | Only London days today..today+27 are kept. Refreshed every 6h (~56 calls: 28 days x 2 clubs); `_purge_availability` runs at thread start and every cycle and deletes past-day, non-numeric-key and Racketeer rows |
| `heatmap_cache`, `heatmap_hour_signal`, `heatmap_dow_signal`, `court_popularity` | 24h |
| `membership_members_cache` (counts only), `stats_cache` | 6h |
| `club_info_cache` | 10 min |
| `payments`, `payments_sync` | none (persistent; amounts, dates, status and an HMAC customer_key, no personal data) |

- `job_state` (job, last_run, ok, calls, errors, updated_at): schedule for `availability` (6h), `heatmap` (24h) and `payments` (30 min); a failed run is due again after 30 min. With no row it is seeded from data (MAX availability `fetched_at`, oldest club `heatmap_cache`, MAX `payments_sync.synced_at`), so the first deploy does not re-run a job; an empty DB is due now. Loops re-check every <= 5 min and log `[job] next run in Nm (persisted)`.
- Never cached: activity-summary when the mix call failed (502) or training failed (served, uncached), club-info with any failed sub-fetch (last stored row served with `X-CourtView-Stale: 1`, else the partial result uncached), membership failures (stored counts with the stale header, else 502), availability that is not a dict with `allSlots` or a non-empty list, a heatmap payload with a missing array. Club-info, membership and availability misses are single-flight (`_single_flight_lock`).
- `_stats_key()` rounds `end` to a 6h bucket. Without it, every millisecond-precision request misses the cache.
- `get_cached` treats an empty `[]` payload as a miss.
- `archive_heatmap`, `archive_club_info` and `archive_financial` hold Racketeer only, 2026-06-15 to 06-30, and nothing writes them any more. `archive_heatmap.hour_norm` and `dow_norm` are NULL on every row, so the archived signal chart is empty and can't be backfilled.

## Dashboard gotchas

- Court grid tooltips: the `.tt` inside `.cell` is a template. Show the body-level portal instead, because `.grid-scroll` overflow clips the inline one.
- Archive selector (Racketeer only, on the Club Info, Stats and Revenue tabs): CSS defaults it to `display:none`, so `showArchiveSelector` must set `'flex'`, not `''`. It has no Live option and auto-loads the latest snapshot. `switchClub()` must hide all three selectors. For Racketeer every `load*` function returns after its archive branch; falling through to a live fetch now just gets a 410.
- Membership plans: `hidden=True` plans still show, and `price=None` shows as "Free / partner". Rocket's paid plans are all hidden, and Padium has only corporate plans.
- `/?token=x` sets the cookie and redirects (302) to `/`, which strips the token from the URL and history.
- `_read_token` is keyed on `(st_mtime_ns, st_size)` and keeps the last good token when the file is empty, unreadable or missing (not cached, so the next request retries). Transitions log `[token] ...` to the journal (never the value). With no good token at startup it returns "" and everything is denied. A `before_request` guard (`_allow_only_get_head`) returns 403 for any method other than GET/HEAD, `?token=` is ignored on other methods, and no response carries `Access-Control-*` headers.
- Tabs pre-load in the background after availability loads. Other clubs warm up after 6s.

## Status JSON and health

- `/root/projects/courtview/status/availability.json` (every 6h, retry 30 min after a failed run) and `payments.json` (every 30 min): exactly `{job, last_run, ok, calls, errors}`, `last_run` ISO 8601 Z, written atomically (temp file + `os.replace`), mode 0644 in a 0755 dir (set explicitly because the unit has `UMask=0077`). `COURTVIEW_STATUS_DIR` overrides the directory. An availability run is `ok` unless the breaker stopped it or at least half the calls failed.
- `GET /api/health` (token): `started_at`, `breaker`, `totals`, `endpoints` (paths only) and `jobs` (from `job_state`). No personal data or query strings.
- `GET /api/availability-fresh?club_id&start_datetime&end_datetime`: availability at most 5 min old (single-flight), live clubs only, window start within now-36h..now+48h and span <= 26h. Used by the dashboard's watch polling instead of the 6h cache. On upstream failure it serves the stored row with `X-CourtView-Stale: 1`, else 502.
