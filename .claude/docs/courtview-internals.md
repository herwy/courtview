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
| `/club/statistics/online_payment_history` | Payment history (live paging), plus the `payments` sync behind search and top spenders |
| `/club/statistics/coach` | Coaching stats |
| `/club/statistics/operational` | Heatmap and court popularity |
| `/club/global_search` | Coach bios |
| `/club/membership/member` | Per-plan member counts (server-side only; PII not forwarded) |

Quirks:
- `operational` needs `club_ids` (plural). `club_id` gives a MongoDB `$in needs an array` error.
- `filtered_activities` and `online_payment_history` reject `limit` > 100 with a 422.
- `online_payment_history` returns oldest first, about 100 per day per club, and rejects ranges over about 31 days. See Payment history paging below.
- `activity_type` must be `Book Court`, `Match`, `Tournament` or `Training`. `Match` returns `Book Court|OPEN_MATCH` records.
- `financial/v2`: `total_combined_subscription_payments` is always 0 and `total_combined_payments` excludes subscriptions. The real subscription figure is `total_current_subscription_amount` (= sum of `membership_subscription`). The dashboard's `revTotalInclSubs()` adds it to the total for the Revenue and Compare tabs.
- Rocket Padel Ilford times out on full-month activity ranges. The backend retries with the last 14 days.

## Racketeer guard

`@app.before_request _block_archived_club` returns 410 `{error: "archived"}` for any `/api/` path except `/api/archive/` when `club_id`, `club_ids` or a POST JSON `club_id` is Racketeer. Cache-only routes (`/api/month`, `/api/heatmap`, `/api/court-popularity`) are included. Racketeer payment search goes through `/api/archive/payment-search`, which reads `archive_financial` only and never contacts upstream. The token is checked first without recording failures, so unauthenticated callers still get 403. Each 410 logs `[archived] 410 METHOD path` to the journal; a clean UI load produces none.

## Payment history paging

Revenue walks back by Europe/London day within the selected month, newest day first. Within a day it pages `skip += 100` while `has_more`. One chunk fetches until 25 rows, 7 requests or the month start; the same rule applies to the initial load and every Load More click. Rows are deduplicated, sorted newest first and shown in UK local time. The logic is the DOM-free block between `PAY-PAGER:BEGIN` and `PAY-PAGER:END` in courtview.html, tested by `node --test tests/test_pay_pager.mjs`. `tests/e2e_dashboard.py` (Playwright, needs `CV_TOKEN`) checks the Racketeer and Padium UI.

## Payments table and sync

- `payments`: PK `(club_id, transaction_id)` (checked 2026-10-14: a full Padium day had 275 records, 275 distinct ids; a missing id falls back to a `synthetic:` key). Columns keep the raw `createdAt`/`amount` strings plus `created_utc` (epoch ms), `local_day` (Europe/London) and `amount_gbp`. Index `(club_id, created_utc)`.
- `payments_sync`: one row per `(club_id, local_day)` with `synced_at`, `complete`, `pages`, `rows`.
- `_fetch_payment_page` is the only caller of `online_payment_history` (APP_HEADERS, chrome110, timeout 15, limit <= 100).
- Sync unit is one London day, paged `skip += 100` with 0.1-0.25 s jitter; up to 3 days in parallel; 200 pages/day cap marks the day incomplete.
- Freshness: skip a day synced < 10 min ago. Otherwise sync unless final (`complete=1` and synced after day close + 10 min). Today/yesterday refresh at most every 10 min; failed days retry at most every 10 min.
- Background loop (`_payments_sync_loop`): 20 s after startup, backfills previous + current London month per `HEATMAP_CLUBS` club, then repeats every 30 min (touching only non-final days).
- Only `HEATMAP_CLUBS` clubs are synced (`PAYMENT_SYNC_CLUB_IDS`); others get 400 `club not synced`. Racketeer is never synced.
- `/api/payment-search`: `q` 2-100 chars, window <= 32 days, case-insensitive `LIKE ... ESCAPE '\'` on name or email (`\`, `%`, `_` escaped), newest first, max 200 rows, with `total`, `truncated`, `complete`.
- `/api/payment-leaderboard` syncs the window then sums `PAYMENT_SUCCESS` rows from `payments` (no row cap, no stats_cache).
- The table stores name, email and phone at rest on the RPi, behind the token gate. Logs carry club prefix, day and counts only.
- Unit tests (`tests/test_payments.py`) need Flask and curl_cffi on Python 3.10+, so run them on the RPi.

## Heatmap

One `operational` call per club every 24h (background thread; Padium and Rocket only). It uses `hottest_time_slots` (30-min buckets aggregated per hour), `week_day_wise_activity_count_combined_graph` (DOW) and `court_wise_activity_count_combined_graph` (court popularity). The DOW x hour matrix is normalised hour signal x DOW signal.

## SQLite cache (`courtview_cache.db`, WAL)

| Table | TTL |
|---|---|
| `availability` | 28d. No eviction; rows not re-fetched just go stale. Refreshed every 6h, excluding Racketeer |
| `heatmap_cache`, `heatmap_hour_signal`, `heatmap_dow_signal`, `court_popularity` | 24h |
| `membership_members_cache`, `stats_cache` | 6h |
| `club_info_cache` | 10 min |
| `payments`, `payments_sync` | none (persistent; holds payment PII) |

- `_stats_key()` rounds `end` to a 6h bucket. Without it, every millisecond-precision request misses the cache.
- `get_cached` treats an empty `[]` payload as a miss.
- `archive_heatmap`, `archive_club_info` and `archive_financial` hold Racketeer only, 2026-06-15 to 06-30, and nothing writes them any more. `archive_heatmap.hour_norm` and `dow_norm` are NULL on every row, so the archived signal chart is empty and can't be backfilled.

## Dashboard gotchas

- Court grid tooltips: the `.tt` inside `.cell` is a template. Show the body-level portal instead, because `.grid-scroll` overflow clips the inline one.
- Archive selector (Racketeer only, on the Club Info, Stats and Revenue tabs): CSS defaults it to `display:none`, so `showArchiveSelector` must set `'flex'`, not `''`. It has no Live option and auto-loads the latest snapshot. `switchClub()` must hide all three selectors. For Racketeer every `load*` function returns after its archive branch; falling through to a live fetch now just gets a 410.
- Membership plans: `hidden=True` plans still show, and `price=None` shows as "Free / partner". Rocket's paid plans are all hidden, and Padium has only corporate plans.
- `/?token=x` sets the cookie and redirects (302) to `/`, which strips the token from the URL and history.
- Tabs pre-load in the background after availability loads. Other clubs warm up after 6s.
