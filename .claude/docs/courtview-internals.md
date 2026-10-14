# CourtView internals

Verified against code 2026-10-14. Route list: `grep -n "@app.route" courtview.py`.

## Padelmates API

Base `https://fastapi-production-fargate.padelmates.io`, no auth. Every call is `cffi_requests` with `impersonate="chrome110"` plus Android OkHttp headers (`com.padelmates/8.5.9`, build 1031, `X-Platform: android`) taken from the APK. iOS headers were never reverse-engineered, so don't invent them.

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
- Background loop (`_payments_sync_loop`): 20 s after startup, backfills previous + current London month per `HEATMAP_CLUBS` club, then repeats every 30 min (touching only non-final days).
- Club allowlists: payment-history, payment-leaderboard and membership-members accept `PAYMENT_CLUB_IDS` (Padium, Rocket) only (400 `club not allowed`; Racketeer gets 410 first). `/api/archive/financial` accepts `ARCHIVE_CLUB_IDS` (Racketeer) only.
- `/api/payment-leaderboard` syncs the window, groups `PAYMENT_SUCCESS` rows by `customer_key` and returns the top 5 as `{customer_id, bookings, total, pct}`; `customer_id` is the first 4 hex of `customer_key`, upper-case (the dashboard shows "Customer #XXXX"). Rows without a key are skipped.
- Payment search and coach bios were removed (they needed names).
- Unit tests (`tests/test_payments.py`, `tests/test_pii.py`, `tests/test_purge.py`) need Flask and curl_cffi on Python 3.10+, so run them on the RPi from a temp copy.

## Data protection

- No personal data is stored or returned. `_scrub_pii` (recursive) drops name/email/phone/id/receipt keys, person containers (players, participants, members, coaches, ...) and `PLAYER:` titles, and `_redact_text` removes emails and UK phone numbers from every remaining string. It runs on upstream JSON before it is cached or returned (generic proxy, availability cache, club-info, booked-hours, revenue-summary) and again on read for availability, club-info and archive payloads.
- Allowlists instead of scrubbing where the shape is known: payment records (`_sanitise_payment`), `/api/day-activities` (`_activity_view`: timing, courts, type, spots, price), `/api/membership-members` (counts only).
- Coaches are anonymised in `/api/coach-stats`: "Coach 1..n" ordered by sessions desc, a pseudonymous `coach_id` (also the `coach_rates` key in activity-summary) and numeric stats only.
- `PRAGMA secure_delete=ON` on every connection. `cv_purge_pii.py` (run by deploy.sh while `--check` says it is needed; stops the service, rebuilds `payments`, rewrites archive payloads, scrubs or deletes availability, clears the stats/club-info/membership caches, then `wal_checkpoint(TRUNCATE)` + `VACUUM`) is idempotent and records `schema_meta.pii_purge`.
- Files: DB, `-wal`, `-shm` and `access.log` are 0600 (deploy.sh fixes existing modes and fails if any is not 600); `courtview.service` and `courtview-backup.service` set `UMask=0077`.
- Scrubbing cannot catch a personal name under an unknown key; new upstream shapes need a test in `tests/test_pii.py`.

## Backup and restore

- `courtview-backup.timer` (03:30 UK +- 10 min, `Persistent=true`) runs `cv_backup.py`: sqlite backup API, `integrity_check`, `gpg --encrypt --recipient-file courtview-backup.pub.asc`, then `backups/courtview-YYYYMMDD.db.gpg` (0600, keep 7). Only the public key is on the RPi; the plaintext temp file is always removed.
- gpg key "CourtView Backup <courtview-backup@doxx.local>", fingerprint `0C6DF971E0D5A34FC9784FA271B04EBF6A997550` (ed25519 sign, cv25519 encrypt, no passphrase, no expiry). The private key lives only in the Mac keyring; losing it makes every backup unreadable. It is not exported anywhere.
- The Mac pulls via launchd (`launchd/net.doxx.courtview-backup-pull.plist`, 09:15 daily plus RunAtLoad) running `scripts/courtview-backup-pull.sh`: tries `pi-cmd` then `pi-wan`, copies new files to `~/Backups/courtview/` (0700 dir, 0600 files, keep 7), logs to `pull.log` and warns when the newest remote file is older than 36 h. That log is the only staleness signal (the timer has no `OnFailure`).
- Restore check: `zsh scripts/courtview-backup-verify.sh` decrypts the newest backup in a temp dir, prints `integrity_check`, the PII column count and email/mobile byte counts, then deletes the plaintext.
- Restore: `gpg --decrypt courtview-YYYYMMDD.db.gpg > courtview_cache.db` on the Mac, then `systemctl stop courtview`, copy over, `chmod 600`, `systemctl start courtview`.
- The HMAC key is deliberately not backed up: keeping it beside the data would weaken the pseudonymisation. After a restore on a new host a fresh key changes `customer_key` for new rows only.

## Heatmap

One `operational` call per club every 24h (background thread; Padium and Rocket only). It uses `hottest_time_slots` (30-min buckets aggregated per hour), `week_day_wise_activity_count_combined_graph` (DOW) and `court_wise_activity_count_combined_graph` (court popularity). The DOW x hour matrix is normalised hour signal x DOW signal.

## SQLite cache (`courtview_cache.db`, WAL)

| Table | TTL |
|---|---|
| `availability` | 28d. No eviction; rows not re-fetched just go stale. Refreshed every 6h, excluding Racketeer |
| `heatmap_cache`, `heatmap_hour_signal`, `heatmap_dow_signal`, `court_popularity` | 24h |
| `membership_members_cache` (counts only), `stats_cache` | 6h |
| `club_info_cache` | 10 min |
| `payments`, `payments_sync` | none (persistent; amounts, dates, status and an HMAC customer_key, no personal data) |

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
