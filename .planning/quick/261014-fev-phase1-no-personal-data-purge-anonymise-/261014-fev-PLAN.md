---
phase: quick-261014-fev
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.py
  - courtview.html
  - tests/test_pii.py
  - tests/test_payments.py
  - tests/test_pay_pager.mjs
  - cv_purge_pii.py
  - tests/test_purge.py
  - courtview.service
  - deploy.sh
  - tests/e2e_dashboard.py
  - cv_backup.py
  - courtview-backup.service
  - courtview-backup.timer
  - courtview-backup.pub.asc
  - scripts/courtview-backup-pull.sh
  - scripts/courtview-backup-verify.sh
  - launchd/net.doxx.courtview-backup-pull.plist
  - CLAUDE.md
  - .claude/docs/courtview-internals.md
  - .claude/docs/padelmates-disclosure-2026-05-21.md
  - .planning/STATE.md
autonomous: true
requirements: [PII-STOP, PII-KEY, PII-FEAT, PII-ALLOW, PII-TEST, PII-PURGE, PII-LOGS, PII-FILES, PII-BACKUP, PII-DOCS]

must_haves:
  truths:
    - "No upstream handler persists or returns a customer's name, email, phone, player/user/firebase id, personal address, receipt URL or booker name; stripping happens in memory before any DB write or response"
    - "Repeat customers are identified only by customer_key = HMAC-SHA256(secret, normalised email); the secret lives in /root/.courtview_hmac_key (0600), outside the repo and the DB, and is never logged or backed up"
    - "No table in courtview_cache.db has a name/email/phone/receipt column, and a byte scan of the DB and -wal finds 0 email and 0 UK-mobile matches"
    - "Payment search is gone (routes, UI box, tests); the payment table shows Date, Description, Amount, Status only"
    - "Top spenders shows 'Customer #XXXX · N bookings · £T' where XXXX is the first 4 hex of customer_key, uppercase, and is identical across repeated calls"
    - "/api/membership-members returns counts only; Club Info shows no member names and no contact email/phone"
    - "Court grid tooltips show booking type, time, spots and price only"
    - "Payment and membership routes reject club_ids outside {Padium, Rocket}; /api/archive/financial serves Racketeer only, sanitised"
    - "DB, -wal, -shm and access.log are 0600; courtview.service has UMask=0077; /root/projects/courtview/certs/ no longer exists"
    - "A nightly courtview-backup.timer writes gpg-encrypted courtview-YYYYMMDD.db.gpg (0600, keep 7); the Mac launchd job pulls them to ~/Backups/courtview/ (keep 7); a decrypted restore passes integrity_check with 0 PII"
  artifacts:
    - path: "courtview.py"
      provides: "EMAIL_RE, UK_PHONE_RE, _scrub_pii, _customer_key, _sanitise_payment, counts-only membership, local-table payment-history, anonymous leaderboard, club allowlists"
      contains: "def _customer_key"
    - path: "tests/test_pii.py"
      provides: "schema, url_map-wide response scan, anonymous stable leaderboard, cffi compliance"
      min_lines: 150
    - path: "cv_purge_pii.py"
      provides: "idempotent one-off purge: backfill customer_key, rebuild payments, rewrite archive/availability payloads, clear caches, secure_delete + checkpoint + VACUUM"
      contains: "secure_delete"
    - path: "tests/test_purge.py"
      provides: "purge on a legacy-schema fixture DB"
    - path: "cv_backup.py"
      provides: "sqlite backup API -> gpg encrypt with --recipient-file -> rotate 7"
    - path: "courtview-backup.timer"
      provides: "nightly schedule, Persistent=true"
    - path: "launchd/net.doxx.courtview-backup-pull.plist"
      provides: "Mac pull job, StartCalendarInterval + RunAtLoad"
  key_links:
    - from: "_fetch_payment_page"
      to: "_sanitise_payment"
      via: "every record is sanitised before the function returns, so no caller ever holds raw PII"
      pattern: "_sanitise_payment\\("
    - from: "cv_purge_pii.py"
      to: "courtview._customer_key"
      via: "same normalised-email basis and same secret as live ingest"
      pattern: "_customer_key"
    - from: "deploy.sh"
      to: "cv_purge_pii.py --check"
      via: "stop service, purge, restart only when legacy PII columns exist"
      pattern: "cv_purge_pii"
    - from: "launchd plist"
      to: "scripts/courtview-backup-pull.sh"
      via: "ProgramArguments absolute path"
---

<objective>
Phase 1 of the CourtView review: no personal data in CourtView, neither stored nor shown (UK GDPR). The records come from Padelmates endpoints that expose other clubs' customers without proper access control, so retrieving and displaying them is itself processing.

Purpose: stop collecting PII, purge what is stored (with secure deletion), keep the research value through an HMAC pseudonymous customer_key, lock it in with regression tests, harden file modes, and add an encrypted, Mac-pulled nightly backup of the clean DB.

Output: sanitised courtview.py/courtview.html, PII regression tests, an idempotent purge script run on the RPi, 0600 files + UMask, a backup timer + Mac pull job, and corrected docs.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md
@.claude/docs/courtview-internals.md
@.claude/docs/padelmates-disclosure-2026-05-21.md
@/Users/herwy/dev/doxxnet-labs/.claude/docs/research/courtview-review-2026-10-14/security-ops.md
@.planning/quick/261014-2xq-payment-search-with-local-payments-table/
@.planning/quick/261014-e6u-auth-hardening-token-last-good-and-get-o/261014-e6u-SUMMARY.md

## Hard rule for the executor (applies to every task)

Never print PII. Every live-data check (DB, journal, access.log, upstream samples, backups) reports counts, column/key NAMES, or booleans only. Never `SELECT player_*`, never echo a description, never print a JSON record. When checking upstream record shape, print `sorted(rec.keys())` only.

## PII surface map (verified from code at a05a910)

Stored:
- `payments` (init_db courtview.py:553-571): player_name, player_email, player_phone, receipt_url columns. ~35k rows, Padium + Rocket. No upstream player id is stored. `_payment_txn_id` (:1555) builds synthetic ids as "synthetic:" + createdAt|player_email|amount|status, so some PRIMARY KEY values embed an email.
- `archive_financial` rows with endpoint='payment-history' (Racketeer): JSON `{"payments": [...]}` with player_name/email/phone and receiptUrl per record.
- `archive_club_info` payloads (club-info shape: profile/memberships/credits/extras; may carry member data and the club contact email/phone).
- `membership_members_cache` payload: `{"counts":…, "members": {plan_id: [{name, email}]}}` (:1078-1088).
- `availability.payload` (proxy cache of all_courts_slot_prices_v3, 28-day TTL): `reservedBookings[].title` carries "PLAYER: <name>" and `reservedBookings[].open_match.players` carries names (consumed at courtview.html:1085-1086).
- `stats_cache` (coach-stats, coach-bios, activity-summary, revenue-summary payloads) and `club_info_cache` (club contact email/phone).

Returned:
- `/api/payment-history` (:1465) raw upstream records for ANY club_id, unbounded skip.
- `/api/payment-search` (:1764), `/api/archive/payment-search` (:1796): name/email/phone/receipt.
- `/api/payment-leaderboard` (:1839, `_aggregate_leaders` :1506): player_name.
- `/api/membership-members` (:960): member name + email, any club_id. Docstring falsely says PII stays server-side.
- `/api/day-activities` (:1357): raw activities incl. `players[].name`, `activity_title` "PLAYER: …".
- `/api/archive/financial?endpoint=payment-history` (:2001): raw archived records.
- `/api/club-info` (:1110) and `/api/archive/club-info` (:1969): club contact email/phone.
- Generic proxy `/api/<path:p>` (:2144) for ALLOWED_PATHS incl. the availability path (reservedBookings names).

Shown (courtview.html):
- Payment table `renderPayTable`/`payRow` (:2938-2967): Player, Email, Phone, Receipt columns; `payKey` (:2876) uses player_email.
- Search box `#revPaySearch` (:852-856), `paySearchQuery/paySearchActive/runPaySearch/onPaySearchChange` (:2695-2775), the `input` listener in `setupRevenue`, calls inside `loadRevenue`/`loadPayChunk`/`setupRevenue`.
- Top spenders `renderLeaders` (:2913-2934): player_name.
- Membership list `loadMemberCounts` (:1931-1984): `membersByPlan`, `.plan-toggle`, `.pml-row` name + email.
- Club Info header (:1997-1998): club email + phone.
- Court grid: `attachBookedByCell` (:1081+) name from reservedBookings; day-activity tooltip (:1193-1210) booker + `players` names.

Keep (not personal): coach names/bios/photos in coach-stats and coach-bios (the club's published staff listing), club name/address/website, court names, plan names/prices, amounts, dates, statuses.

<interfaces>
From courtview.py (current):
- `_fetch_payment_page(club_id, start_ms, end_ms, skip, limit=PAYMENTS_PAGE_SIZE) -> list | None` - the only caller of online_payment_history.
- `_upsert_payments(conn, club_id, records, synced_at) -> int`
- `_sync_payments(club_id, start_ms, end_ms) -> {"complete","days","pages","rows"}`
- `_parse_window_args() -> (club_id, start_ms, end_ms) | (Response, status)`
- `_parse_gbp_amount(raw) -> float | None`, `_parse_created_utc(raw) -> int | None`, `_london_day(ms)`, `_london_day_bounds(day)`
- `get_stats_cached(key)`, `set_stats_cached(key, payload)`, `store_cached(club_id, start, end, payload)`, `get_cached(...)`
- `_gate() -> (passed, via_query)`, `_set_cookie(resp, token)`, `_forbidden()`
- Constants: `HEATMAP_CLUBS`, `PAYMENT_SYNC_CLUB_IDS`, `RACKETEER_CLUB_ID`, `PAYMENTS_PAGE_SIZE=100`, `PAYMENTS_MAX_WINDOW_MS`, `DB_PATH` (env COURTVIEW_DB_PATH), `COURTVIEW_SKIP_STARTUP` env.
- Every upstream call: `cffi_requests.get(url, headers=APP_HEADERS, timeout=…, impersonate="chrome110")`.

From courtview.html: PAY-PAGER:BEGIN/END block (pager pure functions, tested by tests/test_pay_pager.mjs, rows are opaque objects); `loadPayChunk` fetches `/api/payment-history?club_id&start&end&limit&skip` and reads `{payments, has_more}`.

From tests/test_payments.py: module loaded via importlib with COURTVIEW_SKIP_STARTUP=1 and COURTVIEW_DB_PATH in a tempdir; `rec(tid, created, name, email, amount, status)` fixture helper; upstream mocked via `mock.patch.object(cv.cffi_requests, "get", …)`.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Stop collecting, anonymous features, PII regression tests</name>
  <files>courtview.py, courtview.html, tests/test_pii.py, tests/test_payments.py, tests/test_pay_pager.mjs</files>
  <behavior>
    - test_schema_has_no_pii_columns: after init_db on a fresh DB, no column in any table (PRAGMA table_info over sqlite_master) matches name|email|phone|receipt|firebase|player_id|user_id (case-insensitive; court_name in court_popularity is the one allowed *_name column, explicitly allowlisted).
    - test_every_route_leaks_no_pii: with cffi_requests.get mocked to return a universal PII fixture for every URL and the DB seeded (via the real ingest paths plus raw legacy-style archive/availability rows), GET every rule in app.url_map (cookie auth, fixture query args per route, every ALLOWED_PATHS entry for the proxy) and assert each body contains 0 matches of the email regex `[^\s@]+@[^\s@]+\.[^\s@]+` (on application/json bodies), 0 UK phone matches, and 0 occurrences of any sentinel value (sentinel name, email, phone, player id, receipt URL) on ALL bodies including HTML.
    - test_db_has_no_pii_after_ingest: after the fixture flows run, dump every TEXT value in every table and assert 0 sentinel occurrences and 0 email/phone regex matches.
    - test_leaderboard_anonymous_and_stable: two calls return identical leaders; each leader has exactly keys {customer_id, bookings, total, pct}; customer_id matches ^[0-9A-F]{4}$; two payments from the same email (different case/whitespace) aggregate to one leader; no sentinel in body.
    - test_payment_history_local_paged: reads the local table, newest first, rows have exactly {id, createdAt, description, amount, status}; limit capped at 100; has_more contract matches the pager; skip beyond cap -> 400.
    - test_club_allowlist: payment-history, payment-leaderboard and membership-members return 400 for an unknown club_id with zero upstream calls; Racketeer still 410; /api/archive/financial returns 400 for a non-Racketeer club.
    - test_membership_counts_only: body keys are {club_id, counts}; no members key; cache row holds counts only.
    - test_search_routes_gone: /api/payment-search and /api/archive/payment-search return 404 (they must not fall through to the proxy: the proxy 403s on non-allowlisted paths, which also counts as gone; assert status in {403, 404} and no upstream call).
    - test_hmac_key_missing_disables_storage: with COURTVIEW_HMAC_KEY_PATH pointing at a missing file, a payment sync writes 0 rows and the leaderboard route returns 503.
    - test_cffi_calls_compliant: AST-walk courtview.py; every call whose func is an attribute on cffi_requests passes headers=APP_HEADERS and impersonate="chrome110".
  </behavior>
  <action>
**Pseudonymisation primitives (per PII-KEY).** Add near the payments constants: `HMAC_KEY_PATH = os.environ.get("COURTVIEW_HMAC_KEY_PATH", "/root/.courtview_hmac_key")`; a lazily loaded, cached key reader (`_hmac_key() -> bytes | None`) that requires >= 32 bytes after strip, re-reads on (mtime_ns, size) change like `_read_token`, logs a single "[payments] HMAC key missing or short; payment storage disabled" line (never the key or its length), and never creates the file (deploy.sh owns creation, Task 2). `_customer_key(email) -> str | None` = hex HMAC-SHA256(key, email.strip().lower()) via `hmac`/`hashlib`; None if the email is empty or the key is unavailable. Basis decision: the normalised email, because stored rows hold no upstream player id and the basis must be identical for new rows and the backfill. During execution, sample one live upstream payment page on the RPi and record only `sorted(keys)` in the SUMMARY (whether a stable player id exists), without changing the basis.

**Regexes and scrubber (per PII-STOP).** Module-level `EMAIL_RE` (strict: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`) and `UK_PHONE_RE` (mobile `+44 7xxx`/`07xxx` and landline `+44`/`01`/`02` forms, digits with optional single spaces, guarded by `(?<![0-9A-Za-z])` and `(?![0-9A-Za-z])` so hex ids and epoch ms do not match). `_redact_text(s)` replaces both with "". `_scrub_pii(obj)` recursively copies JSON: drops dict keys matching a `_PII_KEY_RE` (email*, *_email, phone*, *_phone, mobile*, first_name, last_name, full_name, player_name, booker*, booked_by*, player_id, user_id, uid, firebase*, receipt*, receiptUrl) and person containers (players, participants, members, followers, attendees, users, owner, created_by); drops `title`/`activity_title` string values containing "PLAYER:" (case-insensitive); passes every remaining string through `_redact_text`. Keep `address` keys (club business address shown in Club Info; personal addresses only occur inside dropped person containers or payment records, which are allowlisted). Club contact email/phone are dropped too: one mechanical invariant (no email/phone anywhere) beats a business-vs-personal judgement per field, and the club contact may be a named person.

**Payments (per PII-STOP).** `_sanitise_payment(rec) -> dict | None` is an allowlist: transaction_id (real upstream id only), createdAt, customer_key (from player_email), source_type, description (cleaned, below), amount, status. `_fetch_payment_page` returns `[_sanitise_payment(r) for r in page]` (filtered) so no caller holds raw records. `_payment_txn_id` for records without a transaction_id becomes "synthetic:" + first 24 hex of sha256(createdAt|customer_key|amount|status). `_clean_description(desc, name, email, phone)` (called inside `_sanitise_payment` with the raw fields before they are discarded): `_redact_text`, then remove the full name and each name token of length >= 3 (case-insensitive, word-bounded, `re.escape`), collapse whitespace. Description scan on the RPi BEFORE finalising (temp copy of courtview.py, read-only sqlite against the live DB, counts only): rows whose raw description contains the row's own name token, email, or phone; distinct cleaned descriptions; and how many cleaned descriptions occur for fewer than 5 distinct emails. If that last count is 0, keep cleaned descriptions. If it is > 0, also generalise: a description is kept only if it matches a template set derived from the scan (cleaned strings used by >= 5 distinct customers, stored as a frozenset constant `DESCRIPTION_TEMPLATES` with digits normalised), otherwise replaced by `source_type` (or "Payment"). Record the counts and the rule chosen in the SUMMARY, never the strings themselves beyond templates shared by >= 5 customers.
New `payments` schema in init_db: club_id, transaction_id, created_utc, local_day, created_at, customer_key, source_type, description, amount, amount_gbp, status, synced_at; PK (club_id, transaction_id); keep idx_payments_club_created; add idx_payments_club_key (club_id, customer_key). Add `PRAGMA secure_delete=ON` to `_db_connect`. Add `_payments_schema_ok(conn)` (no legacy columns per PRAGMA table_info); when False or when `_hmac_key()` is None, `_sync_payment_day` writes nothing and logs once, and payment routes return 503 `{"error":"payments unavailable"}`. `_upsert_payments` writes the new column set only.

**Features (per PII-FEAT, PII-ALLOW).**
- Delete `api_payment_search`, `api_archive_payment_search`, `_like_escape`, `_parse_search_args`, `_payment_row_to_record`, `_search_response`, `PAYMENTS_SEARCH_LIMIT`.
- `PAYMENT_CLUB_IDS = PAYMENT_SYNC_CLUB_IDS` (Padium, Rocket) and `ARCHIVE_CLUB_IDS = frozenset({RACKETEER_CLUB_ID})`. payment-history, payment-leaderboard and membership-members return 400 `{"error":"club not allowed"}` for club_ids outside PAYMENT_CLUB_IDS (Racketeer is already 410 via `_block_archived_club`); all three archive routes that take club_id keep working, and `/api/archive/financial` returns 400 for club_ids outside ARCHIVE_CLUB_IDS.
- `/api/payment-history`: keep the pager contract (club_id, start, end, limit <= 100, skip; returns payments, skip, limit, has_more). Validate with `_parse_window_args`, cap skip at `PAYMENTS_MAX_SKIP = PAYMENTS_MAX_PAGES_PER_DAY * PAYMENTS_PAGE_SIZE` (400 beyond), call `_sync_payments` for the window (same inline behaviour as the leaderboard today; Phase 2 moves it to the background), then `SELECT created_at, description, amount, status, club_id, transaction_id FROM payments WHERE club_id=? AND created_utc BETWEEN ? AND ? ORDER BY created_utc DESC, transaction_id LIMIT ? OFFSET ?` (fetch limit+1 to compute has_more). Each row returns {id: first 12 hex of sha256(club_id|transaction_id), createdAt, description, amount, status}. transaction_id is kept in the DB only (PK for idempotent upserts), never returned.
- Leaderboard: `_aggregate_leaders(rows)` groups successful rows by customer_key (rows with NULL key are skipped), top 5 by total, each leader {customer_id: customer_key[:4].upper(), bookings: count, total: rounded, pct}. Query selects customer_key, amount, status only.
- `/api/membership-members`: first inspect the upstream plan listing for Padium and Rocket on the RPi (keys only) for a per-plan count field (`total_members`, `member_count`, or similar). If present, build counts from the plan listing and drop the `/club/membership/member` fan-out entirely (data minimisation: never fetch member lists). If absent, keep the fan-out but compute `len(raw_list)` only and discard the list in the same scope. Either way the payload is `{"club_id", "counts"}` and that is all that is cached. Fix the docstring. urlencode the plan id while there.
- `/api/day-activities`: return each activity reduced to an allowlist: start_datetime, end_datetime, courts ([{id, court_id, name}] only), activity_type, booking_type, no_of_participants, total_capacity, payment_per_person.
- `/api/club-info`, the generic proxy (JSON responses only; non-JSON bodies pass through unchanged), the availability cache write in the proxy (`store_cached` receives the scrubbed JSON), `/api/coach-stats`, `/api/coach-bios`, `/api/revenue-summary`, `/api/booked-hours`: run upstream JSON through `_scrub_pii` before caching or returning. The archive routes (`/api/archive/club-info`, `/api/archive/financial`) also scrub on read as defence in depth; for endpoint=payment-history the archived records go through `_sanitise_payment`-shaped output (records already sanitised by the purge keep their customer_key; strip customer_key from the response, return the same 5-field row shape with id derived from transaction_id).

**Dashboard (courtview.html).**
- Remove the `.pay-search` markup and `#revPaySearch`, `PAY_SEARCH_*` constants, `revSearchSeq/revSearchMonth/revSearchTimer`, `paySearchQuery`, `paySearchActive`, `runPaySearch`, `onPaySearchChange`, `restorePayView` if it becomes unused, and every call site (grep `paySearch|revSearch|runPaySearch` must be 0 afterwards). Keep `#revPayMeta` in the section header.
- `renderPayTable` headers: Date, Description, Amount, Status. `payRow` renders those four only; delete the `.pay-receipt` CSS and receipt anchor. `payKey(p)` returns `p.id`.
- `renderLeaders`: each row label `Customer #${esc(p.customer_id)} · ${n} booking(s)`, bar, `fmtGBP(p.total)`; coerce numbers with `Number()`.
- `loadMemberCounts`: remove `membersByPlan`, the toggle and member list injection; keep count text and bar. Remove the now-dead `.plan-toggle`/`.plan-member-list`/`.pml-row` CSS.
- `renderClubInfo`: remove the Email and Phone rows.
- Grid: `attachBookedByCell` stores a booking-type label ("Open match" when `bk.open_match`, else "Booked") instead of a name; the day-activity tooltip drops `booker` and `names`, shows the type label (same mapping as the server's activity-summary: Book Court + OPEN_MATCH -> Open Match, Book Court -> Private Booking, Match -> Club Match, else activity_type), time range, spots and price; aria-label says "<court> <time> <type>".
- Keep the PAY-PAGER block untouched unless a function there reads player fields; update tests/test_pay_pager.mjs only if its fixtures reference removed fields.

**Tests.** Create tests/test_pii.py (unittest, same importlib/env pattern as tests/test_payments.py, plus COURTVIEW_HMAC_KEY_PATH to a temp 0600 file with 64 hex chars). The universal fixture uses only obviously fake sentinels (e.g. name "Zed Sentinelson", an @example.test address, an Ofcom drama-range mobile 07700 900xxx, a "PLAYER: Zed Sentinelson" title, a fake firebase id, a receipt URL on example.test) inside every shape the routes read: payment records, activities with players/activity_title, plan listing + member list, coaches, club profile with email/phone, availability with reservedBookings/open_match players. Implement the behaviours above. Update tests/test_payments.py: delete the search tests (test_like_escaping, test_case_insensitive_name_and_email, test_cap_order_and_window, test_second_search_makes_no_upstream_calls, test_late_month_is_searchable, test_archive_search_dedupes_without_upstream, and search parts of test_validation), switch `rec()` assertions to customer_key/new columns, update the leaderboard sum test to compare (customer_id, total), keep every sync/finality/page-cap/410/call-kwargs test.

Run Python tests on the RPi from a temp copy (local Python 3.9 lacks deps): copy courtview.py and tests/ to a fresh `mktemp -d` dir on the RPi via scp to pi-cmd (or the `rpi` route), then run `python3 -m unittest tests.test_pii tests.test_payments tests.test_auth -v` there with COURTVIEW_SKIP_STARTUP=1 (the test modules set DB and key paths themselves). Run `node --test tests/test_pay_pager.mjs` locally. Remove the RPi temp dir afterwards.
  </action>
  <verify>
    <automated>rsync/scp courtview.py tests/ to an RPi mktemp dir; cd there && python3 -m unittest tests.test_pii tests.test_payments tests.test_auth -v (all OK); node --test tests/test_pay_pager.mjs (all pass); grep -cE 'paySearch|revSearch|payment-search|player_email|player_phone|receiptUrl|membersByPlan' courtview.html returns 0; grep -v '^\s*#' courtview.py | grep -cE 'player_phone|receipt_url|payment-search' returns 0</automated>
  </verify>
  <done>All behaviours above are green on the RPi temp copy; the pager tests pass; the HTML and Python greps return 0; the description-scan counts, the chosen description rule, the upstream payment key list and the membership count-source decision are noted for the SUMMARY (counts and key names only).</done>
</task>

<task type="auto">
  <name>Task 2: Purge stored PII, harden files, deploy and verify live</name>
  <files>cv_purge_pii.py, tests/test_purge.py, courtview.service, deploy.sh, tests/e2e_dashboard.py</files>
  <action>
**Purge script (per PII-PURGE).** Create cv_purge_pii.py (repo root, deployed next to courtview.py). It imports courtview with COURTVIEW_SKIP_STARTUP=1 (honour COURTVIEW_DB_PATH / COURTVIEW_HMAC_KEY_PATH) and reuses `_customer_key`, `_sanitise_payment`-equivalent cleaning (`_clean_description` + the description rule from Task 1), `_scrub_pii`, `_payment_txn_id`. Modes: `--check` exits 0 if a purge is needed (payments has any legacy column, OR a `schema_meta` row `pii_purge` is absent), 1 if clean; default mode purges. Refuse to run (exit 2, message without values) if the HMAC key is unavailable, or if `systemctl is-active courtview` reports active when run on the RPi (skip that check when COURTVIEW_DB_PATH is set, for tests). Steps, single connection, `PRAGMA secure_delete=ON` first:
1. payments: create payments_new with the Task 1 schema; stream legacy rows; per row compute customer_key from the stored email, cleaned description (using the stored name/email/phone), and rewrite transaction_id when it starts with "synthetic:" using the new synthetic formula (on PK collision keep the first); insert; DROP payments; rename; recreate both indexes.
2. archive_financial WHERE endpoint='payment-history': for each payload, replace `payments` with sanitised records (allowlist + customer_key), scrub all other keys; UPDATE in place.
3. archive_club_info: payload -> `_scrub_pii` (drops member data and club contact email/phone); UPDATE.
4. availability: payload -> `_scrub_pii` if JSON parses, else DELETE that row; UPDATE.
5. DELETE all rows from membership_members_cache, stats_cache, club_info_cache.
6. Create `schema_meta(key TEXT PRIMARY KEY, value TEXT)` and upsert ('pii_purge', ISO UTC timestamp). Commit.
7. `PRAGMA wal_checkpoint(TRUNCATE)`, `VACUUM`, `PRAGMA wal_checkpoint(TRUNCATE)`.
8. Print counts only: rows rebuilt, synthetic ids rewritten, rows with NULL customer_key, archive payloads rewritten, availability rows scrubbed/deleted, cache rows deleted.
It must be idempotent (second run with `--check` returns 1; a forced second run changes nothing).

Create tests/test_purge.py: build a temp DB with the LEGACY payments schema (copy the old CREATE TABLE text from git show a05a910:courtview.py into the test as a literal) plus legacy-shaped archive_financial, archive_club_info, availability, membership_members_cache and stats_cache rows holding fake sentinels; run the purge; assert no legacy column, 0 sentinel bytes in the DB file and -wal after the run (read raw bytes), customer_key equals courtview._customer_key(email) for a known row and matches across case variants, synthetic ids contain no "@", `--check` then reports clean, and the archive payment-history payload still holds amounts/dates/status.

**Files (per PII-FILES).** courtview.service: add `UMask=0077` in [Service]. deploy.sh changes, inside the existing single remote heredoc where possible (no extra round trips):
- scp list adds cv_purge_pii.py.
- Create the HMAC key once if absent: under `umask 077`, if `/root/.courtview_hmac_key` is missing or empty, write `secrets.token_hex(32)` via python3 to a temp file in /root and `mv` it into place; never echo it; `chmod 600`. Emit `HMACKEY:present` only.
- Before `systemctl restart courtview`: if `python3 /root/projects/courtview/cv_purge_pii.py --check` exits 0, then `systemctl stop courtview`, run the purge (print its counts lines prefixed `PURGE:`), and fail the deploy (exit 1, after `systemctl start courtview`) if it exits non-zero. Never pkill or hand-run gunicorn.
- After restart: `chmod 600` on courtview_cache.db, -wal, -shm (if present) and access.log; emit `MODES:` with `stat -c '%a %n'` for those four.
- Local side: echo the PURGE and MODES lines; fail if any mode is not 600.

**Deploy and live purge.** Run `cv-deploy` (or `zsh deploy.sh`). Then on the RPi:
- `systemctl is-active courtview` = active; `systemctl show courtview -p UMask` = 0077.
- PII column count across sqlite_master = 0 (python3 over PRAGMA table_info with the Task 1 column regex, court_name allowlisted).
- Byte scan of courtview_cache.db and -wal (if present) with the strict EMAIL_RE and UK mobile pattern: report counts only; both must be 0. Report the landline-pattern count too; if > 0, classify each hit by table/page context WITHOUT printing values (e.g. offset falls in which table via `dbstat`, or the 8 bytes around it reduced to a character-class shape) and confirm they are numeric artefacts, else treat as a failure.
- Other copies: `find /root /tmp -name 'courtview_cache*' -o -name '*.db.bak'` on the RPi and `find ~ -name 'courtview_cache*' -not -path '*/Library/*'` on the Mac; delete any copy other than the live DB (report counts only).
- journald: `journalctl -u courtview --no-pager | grep -cE` with the email and mobile patterns; access.log: same two counts. access.log hits > 0: rewrite the file without the matching lines (temp file + `mv`, keep 0600; the running process holds an append fd, so do it with the service stopped via systemctl). Journal hits > 0: do NOT vacuum the shared journal unilaterally (it is shared with doxxnet-labs units); report the count and the time span of the hits as an open item. Report counts only either way.
- certs: confirm `grep -rn 'projects/courtview/certs' /root/projects/courtview /etc/systemd/system /etc/caddy 2>/dev/null` and `crontab -l | grep -c courtview/certs` are empty/0 (also already 0 locally in courtview.py, courtview.service, deploy.sh, cv_notify_fail.py), then `rm -rf /root/projects/courtview/certs` and confirm it is gone.
- Sample anonymous leaderboard: curl `http://127.0.0.1:8766/api/payment-leaderboard?club_id=<Padium>&start=<Oct 1 London ms>&end=<now ms>` on the RPi with the cookie read from /root/.courtview_token (never print the token), twice; print customer_id, bookings, total only and confirm both calls match.
- Rerun the Task 1 test suite on an RPi temp copy plus tests/test_purge.py.
- AST compliance: the test_cffi_calls_compliant result (count of cffi calls, all compliant).

**E2E (tests/e2e_dashboard.py).** Remove `search_token` and every search check in both scenarios. Add: payment table header texts equal ["Date","Description","Amount","Status"]; `#revPaySearch` count 0; Top spenders rows match `^\d\. Customer #[0-9A-F]{4}` (Padium); attach `page.on("response")` for `/api/` URLs and count email-regex (`[^\s@]+@[^\s@]+\.[^\s@]+`) and UK mobile matches over all JSON bodies across both scenarios (must be 0; report counts only); visit Club Info and Book tabs so membership, club-info, day-activities and availability responses are captured. Run it from the Mac with CV_TOKEN read from the RPi token file into the env without printing it, against the default base URL; expect RESULT: ALL PASS.

Deploy greps (CLAUDE.md rule): `rpi 'grep -c "_customer_key" /root/projects/courtview/courtview.py'` > 0, `rpi 'grep -c "Customer #" /root/projects/courtview/courtview.html'` > 0, `rpi 'grep -c "revPaySearch" /root/projects/courtview/courtview.html'` = 0, `rpi 'grep -c UMask=0077 /etc/systemd/system/courtview.service'` = 1.
  </action>
  <verify>
    <automated>zsh deploy.sh (PURGE counts printed on first run, MODES all 600, active); RPi temp-copy python3 -m unittest tests.test_pii tests.test_payments tests.test_auth tests.test_purge (OK); CV_TOKEN=... python3 tests/e2e_dashboard.py prints RESULT: ALL PASS; RPi checks: PII column count 0, DB+WAL email/mobile byte-scan counts 0, certs dir absent, cv_purge_pii.py --check exits 1</automated>
  </verify>
  <done>Live DB holds no PII (0 PII columns, 0 email and 0 mobile byte matches in DB and WAL), caches cleared, archive payloads sanitised with customer_key; files are 0600 with UMask=0077 live; certs/ deleted; journal/access.log scan counts reported (and access.log cleaned if needed); the anonymous leaderboard sample is stable; all unit tests, purge tests and Playwright e2e pass; deployed greps pass and the service is active.</done>
</task>

<task type="auto">
  <name>Task 3: Encrypted nightly backup pulled by the Mac, docs, STATE</name>
  <files>cv_backup.py, courtview-backup.service, courtview-backup.timer, courtview-backup.pub.asc, scripts/courtview-backup-pull.sh, scripts/courtview-backup-verify.sh, launchd/net.doxx.courtview-backup-pull.plist, deploy.sh, CLAUDE.md, .claude/docs/courtview-internals.md, .claude/docs/padelmates-disclosure-2026-05-21.md, .planning/STATE.md</files>
  <action>
**Key (per PII-BACKUP).** On the Mac, generate a dedicated keypair non-interactively with /opt/homebrew/bin/gpg: `--batch --passphrase '' --quick-gen-key "CourtView Backup <courtview-backup@doxx.local>" future-default default never` (ed25519 sign + cv25519 encrypt subkey). No passphrase so restore checks run unattended; the private key stays only in the Mac keyring (FileVault-protected) and is never copied anywhere. Export the PUBLIC key armored to courtview-backup.pub.asc in the repo (public material, safe to commit). Record the fingerprint in the internals doc. Surface in the summary that losing the Mac keyring makes every backup unreadable, so the user may want an offline export of the secret key (do not do it).

**RPi job.** cv_backup.py (python3 stdlib only, runs as root under systemd): open the live DB read-only and use `sqlite3.Connection.backup` into a temp file created with `tempfile.mkstemp(dir=/root/projects/courtview/backups)` (dir created 0700 if missing; os.umask(0o077)); run `PRAGMA integrity_check` on the copy (abort non-"ok"); encrypt with `gpg --batch --yes --homedir <fresh mkdtemp> --trust-model always --recipient-file /root/projects/courtview/courtview-backup.pub.asc --output backups/courtview-YYYYMMDD.db.gpg.tmp --encrypt <tmp>` then `os.replace` to the final name (date in Europe/London), chmod 0600; always unlink the plaintext temp and remove the temp homedir in `finally`; keep the 7 newest `courtview-*.db.gpg`, delete older; print one line with size and kept count (no paths of plaintext). Exit non-zero on any failure. courtview-backup.service: Type=oneshot, User=root, UMask=0077, ExecStart=/usr/bin/python3 /root/projects/courtview/cv_backup.py, Nice=10, IOSchedulingClass=idle, SyslogIdentifier=courtview-backup. No OnFailure (the notify-telegram@ alerter is courtview-specific and would send a misleading "CourtView down"); staleness is visible in the Mac pull log. courtview-backup.timer: `OnCalendar=*-*-* 03:30:00`, `RandomizedDelaySec=10m`, `Persistent=true`, WantedBy=timers.target.

deploy.sh: add cv_backup.py, courtview-backup.service, courtview-backup.timer and courtview-backup.pub.asc to the scp list; in the remote heredoc install both units to /etc/systemd/system, `mkdir -p -m 700 /root/projects/courtview/backups`, daemon-reload (already present; keep one), `systemctl enable --now courtview-backup.timer`, and emit `BACKUPTIMER:$(systemctl is-enabled courtview-backup.timer)`; fail locally if not enabled.

**Mac pull.** scripts/courtview-backup-pull.sh (zsh/bash, `set -euo pipefail`): DEST=~/Backups/courtview (mkdir, chmod 700), log to $DEST/pull.log with UK-local timestamps. launchd has no zsh aliases, so do not use `rpi`; pick the host by trying `ssh -o BatchMode=yes -o ConnectTimeout=5 pi-cmd true`, else `pi-wan` (the same hosts deploy.sh/cv-deploy use). List remote `/root/projects/courtview/backups/courtview-*.db.gpg`, scp any not present locally (to a .part name then mv), chmod 600, keep the 7 newest locally, log copied/kept counts and the newest file's age; warn in the log if the newest remote file is older than 36h. launchd/net.doxx.courtview-backup-pull.plist: Label net.doxx.courtview-backup-pull, ProgramArguments [/bin/zsh, /Users/herwy/dev/courtview/scripts/courtview-backup-pull.sh], StartCalendarInterval Hour 9 Minute 15, RunAtLoad true, StandardOutPath/StandardErrorPath under ~/Backups/courtview/, and an EnvironmentVariables PATH including /opt/homebrew/bin. Install by copying to ~/Library/LaunchAgents/, `launchctl bootout` (ignore failure) then `launchctl bootstrap gui/$(id -u) <plist>`; read the GSD/launchd notes in ~/.claude/reference/incidents.md first (bootout vs RunAtLoad).

scripts/courtview-backup-verify.sh (Mac): decrypt the newest ~/Backups/courtview/*.db.gpg with gpg into a `mktemp -d` dir (0700), run `sqlite3 <file> 'PRAGMA integrity_check'` (must print ok), count PII columns across sqlite_master (python3, same regex as the tests) and byte-scan the file for the strict email and UK mobile patterns, print counts only, and delete the plaintext in a trap.

**Run it.** Deploy (`zsh deploy.sh`). On the RPi: `systemctl list-timers courtview-backup.timer` shows the next run; `systemctl start courtview-backup.service`, then `journalctl -u courtview-backup -n 5` and `stat -c '%a %s %n'` on the new file (0600, non-zero). On the Mac: install the plist, `launchctl kickstart -k gui/$(id -u)/net.doxx.courtview-backup-pull`, confirm the file arrived (0600) and the log line; run the verify script: integrity ok, PII columns 0, email/mobile byte counts 0.

**Docs (per PII-DOCS).**
- CLAUDE.md: replace the `courtview_cache.db` row text "holds payment PII" with "payments hold amounts/dates/status plus an HMAC customer_key; no personal data"; add the HMAC key path and backups dir to the RPi layout table; add under Rules exactly: "No personal data: never store or show names, emails, phones, ids, receipts. Repeat customers via customer_key HMAC only." plus a one-line pointer to the internals doc for backup/restore; keep the Upstream API PII bullets but note /club/membership/member is only ever used for counts (or not at all, per Task 1's decision).
- .claude/docs/courtview-internals.md: remove payment-search, rewrite the payments section (schema, customer_key basis and secret, description rule, payment-history local paging contract and caps, anonymous leaderboard, club allowlists), fix line 19 (membership: counts only), line 48 (no PII at rest) and line 63, add a "Data protection" section (scrubber, purge script, secure_delete, 0600 + UMask) and a "Backup and restore" section (timer, gpg fingerprint, pull job, verify script, HMAC key deliberately not backed up: losing it changes customer_key for new rows, and backing it up beside the data would weaken the pseudonymisation).
- Docstrings: membership-members, payment-history, leaderboard, `_fetch_payment_page`, module header in courtview.py if it mentions PII (this edit belongs with Task 1's courtview.py ownership if done there; otherwise make it here and include courtview.py in the commit).
- Disclosure doc: `git log --all --oneline -- .claude/docs/padelmates-disclosure-2026-05-21.md` (only 6075bc3 and 0c17567 exist, neither records sending) and the doc's "Disclosure plan" section carry no evidence it was sent. Add a status line under the title: "Status: not sent as of 2026-10-14." Surface this in the summary.
- STATE.md: add row 37 to Quick Tasks (`| 37 | Phase 1 no personal data: … | 2026-10-14 | <commit> | [261014-fev-…](.planning/quick/261014-fev-phase1-no-personal-data-purge-anonymise-/) |`), and add/update only this session's Session Continuity row with sed.

**Commit and push.** `git status` first; stage explicitly (`git add <each file>`), never -A (the two untracked .claude/docs/bookandgo-*.md and playtomic-*.md files are not this task's). Commit code and docs as feat/test/docs commits, write 261014-fev-SUMMARY.md, commit it, push. Run `gsd-sdk query validate.health` and report status/errors/warnings.
  </action>
  <verify>
    <automated>rpi 'systemctl is-enabled courtview-backup.timer' = enabled; rpi 'systemctl start courtview-backup.service && stat -c "%a" /root/projects/courtview/backups/courtview-$(TZ=Europe/London date +%Y%m%d).db.gpg' = 600; launchctl kickstart -k gui/$(id -u)/net.doxx.courtview-backup-pull then ls -l ~/Backups/courtview/*.db.gpg (present, -rw-------); zsh scripts/courtview-backup-verify.sh prints integrity ok, PII columns 0, email 0, mobile 0; grep -c 'No personal data' CLAUDE.md = 1; grep -c 'not sent as of 2026-10-14' .claude/docs/padelmates-disclosure-2026-05-21.md = 1; git status shows no staged/unstaged task files</automated>
  </verify>
  <done>Timer listed and enabled; one manual run produced a 0600 encrypted file on the RPi; the Mac pull copied it and logged; the decrypted restore passes integrity_check with 0 PII columns and 0 email/mobile matches; docs carry the new rule, corrected PII claims, backup/restore and the disclosure status; STATE row 37 added; everything committed with explicit paths and pushed; health reported.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Padelmates upstream -> courtview.py | Third-party JSON containing other clubs' customers' PII crosses here; must be stripped in memory before any write or response |
| courtview.py -> SQLite on RPi | Persistent store; must hold no PII, only pseudonymous customer_key |
| Browser -> /api/* | Authenticated dashboard user; must receive no PII and only allowlisted clubs on payment/membership routes |
| RPi -> Mac (backup pull over ssh) | Encrypted files only; private key never leaves the Mac |
| Logs (journald, access.log) | Shared journal with doxxnet-labs units; path-only access log |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-fev-01 | Information disclosure | payment sync / `_fetch_payment_page` | mitigate | `_sanitise_payment` allowlist inside `_fetch_payment_page`; schema has no PII columns; test_db_has_no_pii_after_ingest |
| T-fev-02 | Information disclosure | every route in url_map incl. generic proxy and availability cache | mitigate | `_scrub_pii` on all upstream JSON before cache/return; field allowlists on day-activities/payments; test_every_route_leaks_no_pii with sentinels + regex |
| T-fev-03 | Information disclosure | residual bytes after deletion (free pages, WAL) | mitigate | `PRAGMA secure_delete=ON` on every connection, wal_checkpoint(TRUNCATE) + VACUUM in the purge, byte scan of DB and -wal = 0 |
| T-fev-04 | Information disclosure | customer_key re-identification by hashing guessed emails | mitigate | keyed HMAC-SHA256 with a 32-byte CSPRNG secret, root 0600 outside repo/DB, never logged, not included in backups; only 4 hex shown to the browser |
| T-fev-05 | Information disclosure | backups on RPi and Mac | mitigate | gpg public-key encryption on the RPi (plaintext temp unlinked in finally, 0600, UMask=0077), private key only in the Mac keyring, ~/Backups/courtview 0700 |
| T-fev-06 | Elevation / info disclosure | payment and membership routes for arbitrary club_ids | mitigate | PAYMENT_CLUB_IDS / ARCHIVE_CLUB_IDS allowlists, 400 before any upstream call; test_club_allowlist |
| T-fev-07 | Information disclosure | file modes of DB/WAL/SHM/access.log | mitigate | chmod 600 in deploy.sh plus UMask=0077 in courtview.service and courtview-backup.service; deploy fails if mode != 600 |
| T-fev-08 | Information disclosure | journald / access.log historical lines | mitigate | regex scans with counts only; access.log rewritten without hits; shared journal not vacuumed unilaterally, surfaced as an open item if hits exist |
| T-fev-09 | Denial of service | purge locking the DB during service operation | mitigate | deploy.sh stops courtview via systemctl, purges, restarts; purge refuses to run while the unit is active |
| T-fev-10 | Tampering | backup files (unsigned encryption) | accept | single-operator RPi and Mac; restore verify runs integrity_check; signing adds a private key on the RPi, which is worse |
| T-fev-11 | Repudiation / availability | HMAC key loss | accept | customer_key continuity breaks for new rows only; documented in the internals doc; no PII is recoverable either way |
| T-fev-12 | Information disclosure | stale TLS key copy in certs/ | mitigate | references grepped (0), directory deleted, absence verified |
</threat_model>

<verification>
- PII column count across sqlite_master = 0 (live DB and restored backup).
- Byte scan of the live DB and -wal: email 0, UK mobile 0 (counts only); landline hits, if any, classified as numeric artefacts.
- Sample anonymous leaderboard printed as customer_id, bookings, total only; identical across two calls.
- Unit tests on the RPi temp copy: test_pii, test_payments (updated), test_auth, test_purge all OK; node pager tests pass.
- AST compliance: every cffi_requests call carries headers=APP_HEADERS and impersonate="chrome110".
- cv-deploy passes; deployed greps pass; `systemctl is-active courtview` = active; UMask 0077 live; modes 600.
- Playwright e2e: RESULT: ALL PASS with 0 email/mobile matches across captured /api/ JSON bodies.
- Backup: timer listed and enabled; manual run produces an encrypted 0600 file; Mac pull works; verify script: integrity ok, 0 PII.
- journal/access.log scan counts reported; certs/ absent; disclosure status recorded.
- Commits staged with explicit paths and pushed; `gsd-sdk query validate.health` reported.
</verification>

<success_criteria>
CourtView neither stores nor shows personal data: the live DB, its WAL, every API response and every backup contain no names, emails, phones, player ids or receipts; repeat-customer analysis works through an anonymous, stable "Customer #XXXX" leaderboard; payment search is gone; files are 0600 under UMask=0077; and a clean nightly encrypted backup reaches the Mac and restores successfully.
</success_criteria>

<output>
Create `.planning/quick/261014-fev-phase1-no-personal-data-purge-anonymise-/261014-fev-SUMMARY.md` when done. It must include, as counts/key names only: upstream payment record key list, description-scan counts and the chosen rule, membership count source, purge counts, byte-scan counts, journal/access.log counts, leaderboard sample (ids, bookings, totals), backup file size, gpg fingerprint, and the open items (disclosure not sent; offline export of the backup secret key; any journal hits).
</output>
