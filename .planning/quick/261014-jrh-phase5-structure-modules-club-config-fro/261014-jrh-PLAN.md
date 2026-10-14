---
phase: quick-261014-jrh
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview_app/__init__.py
  - courtview_app/clubs.py
  - courtview_app/config.py
  - courtview_app/upstream.py
  - courtview_app/privacy.py
  - courtview_app/db.py
  - courtview_app/jobs.py
  - courtview_app/accesslog.py
  - courtview_app/security.py
  - courtview_app/payments.py
  - courtview_app/refresh.py
  - courtview_app/routes_core.py
  - courtview_app/routes_availability.py
  - courtview_app/routes_club.py
  - courtview_app/routes_payments.py
  - courtview_app/routes_archive.py
  - courtview_app/startup.py
  - courtview.py
  - courtview.html
  - cv_purge_pii.py
  - deploy.sh
  - tests/cvload.py
  - tests/test_clubs.py
  - tests/test_auth.py
  - tests/test_payments.py
  - tests/test_pii.py
  - tests/test_purge.py
  - tests/test_reliability.py
  - tests/test_security.py
  - tests/test_paths.py
  - CLAUDE.md
  - .claude/docs/courtview-internals.md
  - .claude/docs/INDEX.md
autonomous: true
requirements: [P5-CLUBS, P5-MODULES, P5-FRONTEND, P5-DOCS]

must_haves:
  truths:
    - "Club data (id, name, sub, short, courts, status, payments/heatmap/compare flags) is defined once, in courtview_app/clubs.py; HEATMAP_CLUBS, PAYMENT_SYNC_CLUB_IDS, PAYMENT_CLUB_IDS, ARCHIVE_CLUB_IDS and RACKETEER_CLUB_ID are derived from it with values identical to today"
    - "GET /api/clubs returns the club list to an authenticated caller, 403 without a token, no PII, CSP_API + no-store headers"
    - "courtview.html contains none of the three club ids; it loads the list from /api/clubs at startup, shows a loading state, and shows a safe error state if the load fails"
    - "New visitors still land on the first non-archived club; a stored cv-club-id is still honoured"
    - "courtview.py is a thin gunicorn entry exposing app; ExecStart (courtview:app) is unchanged"
    - "No courtview_app module exceeds ~800 lines"
    - "Every existing test passes with the same intent (194 baseline) plus new tests; pager node tests 11/11; Playwright e2e ALL PASS with 0 CSP errors"
    - "All upstream traffic still goes through UpstreamClient, proven by the AST test scanning the whole package"
    - "Every fetch in courtview.html goes through one api() helper with a 20 s AbortController timeout, JSON parsing, a consistent ApiError and keyed stale-response guards"
    - "Warm-up of other clubs and the background tab preloads run only while the page is visible and resume when it becomes visible again"
    - "cv-deploy passes (health check, no rollback), service active, systemd-analyze security still about 1.5, status JSON fresh"
  artifacts:
    - path: "courtview_app/clubs.py"
      provides: "Single club config source plus derived sets and public_clubs()"
      contains: "def public_clubs"
    - path: "courtview_app/__init__.py"
      provides: "create_app() registering hooks and routes in the original order"
      contains: "def create_app"
    - path: "courtview.py"
      provides: "Thin gunicorn entry"
      contains: "create_app"
    - path: "tests/cvload.py"
      provides: "Fresh per-test-module import of the package (isolation of env-driven paths)"
      contains: "def load"
    - path: "tests/test_clubs.py"
      provides: "Club config derivation, /api/clubs contract, no ids in HTML"
    - path: "courtview.html"
      provides: "api() helper, /api/clubs boot, visibility-gated warm-up"
      contains: "AbortController"
  key_links:
    - from: "courtview.html"
      to: "/api/clubs"
      via: "boot() before any club-dependent loader"
      pattern: "/api/clubs"
    - from: "courtview.py"
      to: "courtview_app.create_app"
      via: "app = create_app()"
      pattern: "app = create_app\\(\\)"
    - from: "deploy.sh FILES"
      to: "courtview_app/"
      via: "tar list"
      pattern: "courtview_app"
    - from: "tests/test_pii.py AstTests"
      to: "courtview_app/*.py"
      via: "AST scan over every package file"
      pattern: "courtview_app"
---

<objective>
Phase 5 of the CourtView review (structure), user-approved. A behaviour-preserving refactor with exactly three behaviour changes:
1. Club config comes from one server-side source and is served at authenticated /api/clubs; the HTML stops hardcoding clubs.
2. courtview.py (3,513 lines) is split into a courtview_app/ package; courtview.py stays the gunicorn entry.
3. A single frontend api() helper (timeout, JSON, error object, stale guards) and visibility-gated warm-up/preloads.

Purpose: review findings #7, #8 and #14 (architecture.md), without weakening any Phase 1-4 invariant (PII, auth, CSP, UpstreamClient-only traffic, sandbox, rollback).
Output: courtview_app/ package, thin courtview.py, tests/cvload.py + tests/test_clubs.py, updated tests, courtview.html, deploy.sh, docs. Three commits (one per task), pushed, deployed after each task.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md
@.claude/docs/courtview-internals.md
@/Users/herwy/dev/doxxnet-labs/.claude/docs/research/courtview-review-2026-10-14/architecture.md

<facts_verified_while_planning>
- Base commit: 5709313. courtview.py 3,513 lines, courtview.html 3,049 lines, single inline script starting at line 850.
- Baseline 194 Python tests: auth 16, cv_notify_fail 21, cv_telegram 12, paths 7, payments 14, pii 18, purge 4, reliability 64, security 38 (def test_ counts; Task 1 must confirm with a real run).
- Every courtview test module sets COURTVIEW_SKIP_STARTUP/COURTVIEW_DB_PATH/COURTVIEW_DATA_DIR (and some HMAC/STATUS env) at import, then loads courtview.py via importlib.util.spec_from_file_location under its OWN alias (courtview_sec_under_test, courtview_rel_under_test, ...). All modules run in ONE process (python3 -m unittest tests.test_security tests.test_reliability ...), so each module currently gets its own independent copy of all module state. A package import would be cached in sys.modules and shared across test modules unless purged (see Task 2, tests/cvload.py).
- test_purge loads cv_purge_pii.py, which does `import courtview as cv` and uses cv._db_connect, _hmac_key, _payment_txn_id, _PAYMENTS_COLUMNS, _redact_text, _sanitise_payment, _scrub_pii, datetime, DB_PATH, PAYMENTS_DDL, timezone; the test reads purge.cv.HEATMAP_CLUBS and purge.cv.RACKETEER_CLUB_ID.
- Patched/assigned attributes in tests: _now, _gate, _resolve_ip, _read_token, time.sleep, UPSTREAM._new_session, cffi_requests.Session, _job_states, _job_record, _job_due_in, _wait_for_job, _write_job_status, _rewrite_status_files, STATUS_DIR, TOKEN_PATH, HMAC_KEY_PATH, DB_PATH, ACCESS_LOG_PATH, ACCESS_LOG_MAX_BYTES, ACCESS_LOG_KEEP, _ACCESS_FD, _ACCESS_HTML_BYTES, _ACCESS_JS_BYTES, _DASHBOARD_CSS_BYTES, _dashboard_bytes, _log_access, _sync_payments, _sync_payment_day, _fetch_payment_page, _enqueue_payment_window, _drain_payment_queue, _PAY_QUEUE, PAYMENTS_PAGE_DELAY, PAYMENTS_MAX_PAGES_PER_DAY, _token_cache, _AUTH_FAILS.
- Source-reading tests: tests/test_pii.py:198 (AST compliance over courtview.py only), tests/test_security.py:288 (Response content_type regex over courtview.py), tests/test_reliability.py:712 (club_extras/get_booked_hours grep over courtview.py). test_security.py:36 EXPECTED_ENDPOINTS is the route-auth list.
- AST test requires inside class UpstreamClient: headers=Name("APP_HEADERS") and impersonate="chrome110"; outside it, no Name cffi_requests (Load) and no Name/Attribute TARGET (Load). So TARGET, APP_HEADERS, ANDROID_UA, APP_VERSION, BUILD, ALLOWED_PATHS, CACHED_PATH must live in upstream.py as bare module names.
- __file__-relative paths in courtview.py: WEB_ASSETS_DIR (line 610), DASHBOARD_HTML (611), static_asset safe_join root (1528). All mean "the release root" (dir of courtview.py), not the package dir.
- Hooks (registration order matters; Flask runs after_request in reverse order): before_request _allow_only_get_head (1463) then _block_archived_club (1471); after_request _security_headers (3110) then log_req (3137); errorhandler(404) _not_found (3128). The proxy route uses provide_automatic_options=False.
- `global` statements: _load_web_assets (741), _open_access_log/_rotate (766, 788), _load_dashboard (1414), _startup (3451: _STARTUP_DONE, _ACCESS_FD).
- courtview.service: WorkingDirectory=/opt/courtview/current, PYTHONDONTWRITEBYTECODE=1, ExecStart gunicorn ... courtview:app. deploy.sh FILES at line 41; staging tar at line 70, then compileall on the RPi.
- HTML: club config at 852-861 (RACKETEER_CLUB_ID, CLUBS, CLUB default logic, clubInfo) and 880-883 (COMPARE_CLUBS); apiFetch wrapper at 864; fetch( call sites at 942, 957, 1379, 1626, 1803, 1890, 1904, 1918, 2134, 2212, 2235, 2237, 2369, 2613, 2614, 2663, 2665, 2687, 2723 (inside loadPayChunk: fetchPage passed to payPagerRun, returns json-or-null), 2991 (warmOtherClubs); INIT at 2997-3011 (Promise.all then setTimeouts 200/500/3000 ms and warmOtherClubs at 6000 ms; setInterval(pollWatches)). PAY-PAGER block 2378-2455 must stay byte-identical in contract (tests/test_pay_pager.mjs slices it).
- CSP for the dashboard already allows connect-src 'self'.
- e2e (tests/e2e_dashboard.py) selects clubs via localStorage init script; default base https://courtview.labs.doxx:47200; needs CV_TOKEN.
</facts_verified_while_planning>

<interfaces>
Club table (Task 1, courtview_app/clubs.py). Values copied from today's HTML/Python, in this order:
- Racketeer: id 5111764d9bb14be3adbdb8e133e8bd80, name "Racketeer", sub "North Acton, London", short "Racketeer", courts 11, status "archived", payments False, heatmap False, compare False
- Padium: id 47d2eb0db7194a9dbd29783c3a2a82ad, name "Padium Canary Wharf", sub "Canary Wharf, London", short "Padium · Canary Wharf", courts 7, status "live", payments True, heatmap True, compare True
- Rocket: id 788fa2c66535421aabc60fd27f941c42, name "Rocket Padel Ilford", sub "Ilford, London", short "Rocket Padel · Ilford", courts 12, status "live", payments True, heatmap True, compare True

Derived (must equal today's values exactly):
- HEATMAP_CLUBS = list of {"id", "courts"} for heatmap clubs, in table order (Padium, Rocket) - same shape as courtview.py:342
- PAYMENT_SYNC_CLUB_IDS = frozenset of payments clubs; PAYMENT_CLUB_IDS = PAYMENT_SYNC_CLUB_IDS
- ARCHIVE_CLUB_IDS = frozenset of status "archived"
- RACKETEER_CLUB_ID = the single archived club id (assert exactly one archived club at import)
- public_clubs() -> list of {"id","name","sub","short","courts","archived","compare"} (archived/compare booleans). No other fields.

/api/clubs response: {"clubs": public_clubs()} as application/json, gated by _gate like api_health, cookie refresh on ?token= exactly like api_health.

Package module map (Task 2). Lines are source ranges in courtview.py at 5709313; a guide, the executor confirms by reading:
| Module | Contents | Source |
|---|---|---|
| clubs.py | club table + derived sets (Task 1) | 340-375 |
| config.py | ROOT_DIR (parent of the package dir), DATA_DIR, _cred_path, TOKEN_PATH, HMAC_KEY_PATH, DB_PATH, ACCESS_LOG_PATH, STATUS_DIR, WEB_ASSETS_DIR, DASHBOARD_HTML, COOKIE_*, CACHE_TTL, _LONDON_TZ, job/heatmap/watch timing constants, _now | 349-398 (non-payment), 610-617 |
| upstream.py | TARGET, APP_VERSION, BUILD, ANDROID_UA, APP_HEADERS, ALLOWED_PATHS, CACHED_PATH, UpstreamError, UpstreamUnavailable, _iso_z, UpstreamClient, UPSTREAM | 41-338 |
| privacy.py | regexes, _redact_text, _scrub_pii, _scrub_json_text, HMAC state, _hmac_key, _customer_key, _pseudonym | 414-517 |
| db.py | _db_connect, PAYMENTS_DDL, _PAYMENTS_COLUMNS, _payments_schema_ok, init_db, stats cache, availability cache (_cache_key..store_cached), _single_flight_lock + its lock dict | 400-412, 915-1186, 3436-3445 |
| jobs.py | _job_seed, _job_due_in, _job_record, _wait_for_job, _write_job_status, _rewrite_status_files, _job_states | 1188-1308, 3001-3014 |
| accesslog.py | IP geo cache, _resolve_ip, _enrich_access_log, ring buffer, _ACCESS_* bytes, _load_web_assets, _open/_rotate/_log_access, _get_client_ip, log_req hook | 652-811, 1424, 3137-3148 |
| security.py | _FORBIDDEN_HTML, rate limit, token cache/_read_token, _constant_time_eq, _set_cookie, CSP constants, _build_dashboard_csp, _load_dashboard + _dashboard_bytes/_dashboard_csp, _forbidden, _provided_token, _gate, hooks _allow_only_get_head, _block_archived_club, _security_headers, _not_found | 619-651, 812-913, 1380-1494, 3110-3135 |
| payments.py | payment constants (349-356, 365), description cleanup, _sanitise_payment, _fetch_payment_page, _payments_ready, _parse_gbp_amount, _aggregate_leaders, London-day helpers, upsert/sync engine, queue, scheduled run, loop, _parse_window_args | 519-608, 2248-2283, 2338-2668 |
| refresh.py | availability targets/purge/cycle/_refresh_loop, heatmap fetch + loop | 1310-1369, 3226-3427 |
| routes_core.py | index, static_asset, api_clubs (new), api_health, proxy, access_page, access_js, dashboard_css, access_data | 1495-1546, 3016-3108, 3150-3224 |
| routes_availability.py | api_heatmap, api_court_popularity, api_month, _availability_row, api_availability_fresh | 1548-1755 |
| routes_club.py | _counts_payload, membership, _exc_error, club-info, booked-hours, activity-summary (+_do_fetch), _activity_view, day-activities, revenue-summary, coach-stats (+anonymiser) | 1756-2247, 2899-2999 |
| routes_payments.py | _payment_public_id, api_payment_history, api_payment_leaderboard | 2281-2337, 2669-2704 |
| routes_archive.py | api_archive_* + _archive_payments_text | 2705-2898 |
| startup.py | _STARTUP_LOCK/_STARTUP_DONE, _startup() | 3433-3500 |
| __init__.py | create_app() | new |
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Baseline, single club config source, /api/clubs, HTML loads clubs at startup</name>
  <files>courtview_app/__init__.py, courtview_app/clubs.py, courtview.py, courtview.html, tests/test_clubs.py, tests/test_security.py, deploy.sh</files>
  <behavior>
    - clubs.HEATMAP_CLUBS == [{"id": Padium, "courts": 7}, {"id": Rocket, "courts": 12}] (order and shape identical to today)
    - clubs.PAYMENT_SYNC_CLUB_IDS == clubs.PAYMENT_CLUB_IDS == {Padium, Rocket}; ARCHIVE_CLUB_IDS == {Racketeer}; RACKETEER_CLUB_ID == Racketeer id
    - courtview module constants (HEATMAP_CLUBS, PAYMENT_*, ARCHIVE_CLUB_IDS, RACKETEER_CLUB_ID) are the clubs.py objects
    - GET /api/clubs anonymous -> 403 and zero upstream calls (covered by the existing route-auth loop once api_clubs is in EXPECTED_ENDPOINTS)
    - GET /api/clubs with cookie -> 200, application/json, Cache-Control no-store, Content-Security-Policy == CSP_API, body {"clubs": [...]} with 3 entries whose key sets are exactly {id,name,sub,short,courts,archived,compare}, no "@" in the body, order Racketeer/Padium/Rocket
    - courtview.html contains none of the ids in clubs.CLUBS (test reads every id from the module, not literals) and contains "/api/clubs"
    - The existing test_every_route_leaks_no_pii loop (test_pii) automatically covers the new route; confirm it does
  </behavior>
  <action>
Step 0 (baseline, before any edit): run the full suite on the RPi from a temp copy of the CURRENT layout and record per-module counts in the SUMMARY: D=$(rpi 'mktemp -d'); COPYFILE_DISABLE=1 tar cf - courtview.py courtview.html cv_*.py web tests/*.py | rpi "tar xf - -C $D"; rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest -v tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail tests.test_cv_telegram tests.test_paths 2>&1 | tail -5"; also get per-module counts (run each module separately, or count "ok" lines per module from -v output); then rpi "rm -rf $D". Run node --test tests/test_pay_pager.mjs locally (expect 11/11). Record all numbers. Expected total 194.

Step 1: create courtview_app/__init__.py (module docstring only for now; Task 2 fills it) and courtview_app/clubs.py per the interfaces block. The club table is a tuple of dicts (CLUBS) with keys id, name, sub, short, courts, status ("live"/"archived"), payments, heatmap, compare. Derive the sets listed in interfaces; RACKETEER_CLUB_ID via the single archived club with a module-level check that raises RuntimeError if the count of archived clubs is not 1 (the 410 guard and archive routes assume one archived club). public_clubs() returns fresh dicts with only the 7 public keys. No imports from the rest of the package (clubs.py is the bottom layer), stdlib only.

Step 2: in courtview.py replace the literal HEATMAP_CLUBS (342-346), PAYMENT_SYNC_CLUB_IDS (348), RACKETEER_CLUB_ID (369), PAYMENT_CLUB_IDS and ARCHIVE_CLUB_IDS (374-375) with assignments from clubs (add `from courtview_app import clubs` near the imports). Keep ARCHIVED_ERROR/ARCHIVED_MESSAGE where they are. Every other use is unchanged. Add route api_clubs right after api_health (same gate + via_query cookie pattern as api_health, json.dumps of {"clubs": clubs.public_clubs()}, content_type application/json). No PII: the table holds none. The path /api/clubs is a static rule, so it wins over the /api/<path:p> proxy.

Step 3: tests. Add "api_clubs" to EXPECTED_ENDPOINTS in tests/test_security.py (never remove anything). Create tests/test_clubs.py, loading courtview.py exactly the way test_security does today (own alias, own temp env, COURTVIEW_SKIP_STARTUP=1, token file + _read_token handling copied from test_security's SecBase, upstream stubbed so any call is recorded and asserted empty), implementing the behaviour list above. Write the tests first, watch the HTML-id and /api/clubs tests fail, then implement.

Step 4: courtview.html (per requirement 1, keep default-club logic). Replace the CONFIG literals (852-861, 880-883) with: let RACKETEER_CLUB_ID = null; let CLUBS = []; let COMPARE_CLUBS = []; let CLUB = null; let clubInfo = null; isArchivedClub becomes a lookup of the archived flag in CLUBS (keep the function name). Add an async boot(): fetch('/api/clubs') (plain fetch here; Task 3 swaps in api()), on success set CLUBS = clubs.map to {id,name,sub,courts,archived}, COMPARE_CLUBS = clubs with compare true mapped to {id, name: short}, RACKETEER_CLUB_ID = first archived id or null, then the unchanged default logic: stored cv-club-id if it is in CLUBS, else the first non-archived club, else CLUBS[0]; clubInfo as before. Then run the club-dependent part of INIT in the same order as today (setupClubSelector and the Promise.all chain with its timers and warmOtherClubs, and setInterval(pollWatches)). Before boot completes: the club button shows "Loading clubs..." via textContent and gridOut keeps its spinner. On failure (non-2xx, bad JSON, empty list): clubBtnName/clubBtnSub get "Club list unavailable"/"Reload to try again" via textContent, gridOut gets a static empty-state div (no interpolated data), and no loaders, warm-up or pollWatches start. Audit every top-level statement executed before boot (setupTabs, setupRevenue, buildDateTabs, setupNotifyPanel, pickDur listeners, the tooltip IIFE) with grep for CLUB, clubInfo, CLUBS, COMPARE_CLUBS, RACKETEER_CLUB_ID and isArchivedClub: anything that reads club state at setup time moves after boot; event handlers that only read it on user action may stay but must tolerate CLUB null (tabs activated before boot should no-op, check activate()). Archive call sites keep using RACKETEER_CLUB_ID (now from config). Do not touch the PAY-PAGER block. Keep the single inline script (CSP hash is computed at load).

Step 5: deploy.sh: add courtview_app to FILES (line 41) and add --exclude '__pycache__' and --exclude '*.pyc' to the staging tar at line 70 (bsdtar accepts both). From now on the RPi test copy is: COPYFILE_DISABLE=1 tar --exclude '__pycache__' -cf - courtview.py courtview.html cv_*.py courtview_app web tests/*.py, and the unittest list gains tests.test_clubs.

Step 6: run the full RPi suite (new layout copy, plus tests.test_clubs), node pager tests, then cv-deploy, the CLAUDE.md remote greps (rpi 'grep -c "/api/clubs" /opt/courtview/current/courtview.html', rpi 'grep -c "def public_clubs" /opt/courtview/current/courtview_app/clubs.py', rpi 'systemctl is-active courtview'), and the Playwright e2e (CV_TOKEN read from the RPi into an env var, never printed). Commit with explicit paths: git add courtview_app/__init__.py courtview_app/clubs.py courtview.py courtview.html tests/test_clubs.py tests/test_security.py deploy.sh; commit "feat(261014-jrh): single club config source served at /api/clubs"; push.
  </action>
  <verify>
    <automated>D=$(rpi 'mktemp -d'); COPYFILE_DISABLE=1 tar --exclude '__pycache__' -cf - courtview.py courtview.html cv_*.py courtview_app web tests/*.py | rpi "tar xf - -C $D"; rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail tests.test_cv_telegram tests.test_paths tests.test_clubs 2>&1 | tail -3; rm -rf $D"; node --test tests/test_pay_pager.mjs 2>&1 | grep -E '^# (pass|fail)'; for id in 5111764d9bb14be3adbdb8e133e8bd80 47d2eb0db7194a9dbd29783c3a2a82ad 788fa2c66535421aabc60fd27f941c42; do grep -c "$id" courtview.html; done</automated>
  </verify>
  <done>Baseline counts recorded (expect 194 + 11 pager). Suite OK with 194 + new test_clubs tests; pager 11/11; each club-id grep on courtview.html prints 0; deployed (health ok, no rollback), remote greps > 0, service active; e2e ALL PASS, 0 CSP errors; commit pushed.</done>
</task>

<task type="auto">
  <name>Task 2: Split courtview.py into the courtview_app package; thin entry; tests patch the real modules</name>
  <files>courtview_app/*.py (all modules in the map), courtview.py, cv_purge_pii.py, tests/cvload.py, tests/test_auth.py, tests/test_payments.py, tests/test_pii.py, tests/test_purge.py, tests/test_reliability.py, tests/test_security.py, tests/test_paths.py, tests/test_clubs.py</files>
  <action>
Move code, do not rewrite it (per requirement 2). Function bodies stay byte-for-byte except for the mechanical rules below.

Rules (these make every name patchable at exactly one place and keep behaviour identical):
R1 Intra-package imports are only `from . import modname` at module top level. Never `from .mod import name`, never imports inside functions. Every cross-module reference is qualified (config.DB_PATH, security._gate(), upstream.UPSTREAM, payments._sync_payments, accesslog._resolve_ip, jobs._job_states, config._now()). Same-module references stay bare. This is what lets a test patch payments._sync_payments and have every caller see it.
R2 Layering, lowest first: clubs, config, upstream, privacy, db, jobs, accesslog, security, payments, refresh, routes_*, startup, __init__. A module imports only lower layers. No import-time attribute access across modules except to clubs, config and upstream constants. If a function sits better in another module to keep this acyclic (e.g. _get_client_ip in accesslog because security's rate limiter and log_req both use it), move it and note it in the SUMMARY.
R3 `global X` where X now lives in another module becomes a qualified assignment (startup sets accesslog._ACCESS_FD = accesslog._open_access_log(); _STARTUP_DONE stays global in startup.py). No module constant may be captured as a default argument or copied into another module at import time (grep for `=DB_PATH`, `= config.` at module level outside config).
R4 _now: config.py keeps `from time import time as _now`; every former bare `_now()` call outside config becomes config._now(). Plain time.time()/time.sleep calls stay as they are.
R5 Paths: config.ROOT_DIR = parent of the package dir (os.path.dirname(os.path.dirname(os.path.abspath(__file__)))). WEB_ASSETS_DIR, DASHBOARD_HTML and the static_asset safe_join root use ROOT_DIR, so they resolve to the release root exactly as before. static_asset's extension allowlist means package .py files stay unservable; add a test for it (below).
R6 upstream.py keeps TARGET, APP_HEADERS, ANDROID_UA, APP_VERSION, BUILD, ALLOWED_PATHS, CACHED_PATH as bare module names and is the only file importing curl_cffi. routes_core.proxy uses upstream.ALLOWED_PATHS / upstream.CACHED_PATH and calls through upstream.UPSTREAM only.
R7 create_app() in __init__.py: Flask(__name__, static_folder=None), MAX_CONTENT_LENGTH 1 MiB, url_map.merge_slashes False, then register in the original order: before_request security._allow_only_get_head, security._block_archived_club; every route via a register(app) function per routes module using app.add_url_rule with the ORIGINAL rule string, endpoint name = original function name, the original methods and provide_automatic_options=False for proxy, in original source order (core index/static_asset first, then availability, club, payments, archive, coach-stats, health, clubs, proxy, access routes - match the old file's order); then after_request security._security_headers, after_request accesslog.log_req, errorhandler(404) security._not_found. No blueprints (endpoint names must stay unprefixed).
R8 courtview.py becomes the entry (target under 30 lines): keep the module docstring (updated), `from courtview_app import create_app, startup`, app = create_app(), the COURTVIEW_SKIP_STARTUP check calling startup._startup(), and the __main__ dev fallback. ExecStart courtview:app is unchanged.
R9 Keep the 6-line section comments from the monolith at the top of each module as the module docstring; no dead code, no commented-out code.

Method: create modules per the interfaces map, cut-and-paste the ranges, apply R1-R6, delete them from courtview.py. Check line counts as you go (wc -l courtview_app/*.py); none above ~800. Update cv_purge_pii.py to import from courtview_app (config, db, privacy, payments, clubs) instead of `import courtview`, qualified per R1, keeping its os.environ.setdefault line; keep `cv` out of its namespace and update test_purge accordingly (it reads purge.<module> instead of purge.cv).

Tests (prefer real module paths, never weaken an assertion):
- Create tests/cvload.py with load(alias): remove "courtview_app" and every "courtview_app.*" key from sys.modules, load REPO/courtview.py via spec_from_file_location under alias (as the tests do today), and return a types.SimpleNamespace with app=entry.app, entry=entry module, and one attribute per submodule (config, clubs, upstream, privacy, db, jobs, accesslog, security, payments, refresh, routes_core, routes_availability, routes_club, routes_payments, routes_archive, startup) taken from sys.modules right after the load. Also purge() (the removal step alone) for test_purge. This gives each test module its own fresh package bound to the env it set, which is today's isolation. It is safe because R1 forbids runtime sys.modules lookups inside the package.
- In test_security, test_reliability, test_pii, test_payments, test_auth, test_clubs: replace the spec-loading block with cv = cvload.load("<same alias as before>"). Then rewrite each cv.NAME to cv.<owning module>.NAME. Do this with a throwaway script in /tmp (not committed): build name -> module from the fresh package (names defined at module top level per AST of each courtview_app file), assert every cv.NAME in the six files maps to exactly one module (fail loudly on unknown or ambiguous names), and rewrite. cv.app stays. cv.time.sleep patches become patching time.sleep on the stdlib time module (import time in the test), which is the same object as today. cv.cffi_requests.Session becomes cv.upstream.cffi_requests.Session. Assignments like cv.TOKEN_PATH = x become cv.config.TOKEN_PATH = x, which works because security reads config.TOKEN_PATH at call time (R1).
- test_paths: load via cvload.load inside its controlled env and assert on ns.config.TOKEN_PATH etc. (same expected values).
- test_purge: call cvload.purge() before exec_module of cv_purge_pii so the package binds to test_purge's env, as `import courtview` did before.
- Source-scan tests cover the whole package: test_pii AstTests parses every courtview_app/*.py plus courtview.py; asserts UpstreamClient is defined exactly once (in upstream.py), applies the unchanged inside/outside rules per file, keeps assertGreaterEqual(sess, 1)/(gets, 1), and asserts the scan saw at least 16 package files. Add in the same class: test_package_import_rules (AST: no ImportFrom with level >= 1 and names other than modules - i.e. `from . import x` only, where every x is a package module; no Import/ImportFrom inside any FunctionDef; curl_cffi imported only in upstream.py). test_security.py:288 content_type regex and test_reliability.py:712 club_extras grep iterate every package file plus courtview.py (assert the file list is non-empty).
- Add to test_security: GET /courtview_app/config.py and /courtview_app/__init__.py with a valid cookie return 404 (allowlist), and an existing .js asset under web/ still serves 200 as before (find the existing static test and mirror it).
- Structural equivalence check (throwaway, /tmp, run on the RPi, result pasted into SUMMARY): (a) git show 5709313:courtview.py > /tmp/old/courtview.py, import both old and new with COURTVIEW_SKIP_STARTUP=1 and temp env, and compare sorted (rule, endpoint, sorted methods) for every url_map rule (expect identical plus /api/clubs from Task 1 - compare against the Task 1 commit instead to get an exact match), the names in app.before_request_funcs[None], app.after_request_funcs[None] and the 404 handler; (b) AST equivalence: for every top-level FunctionDef/ClassDef in the Task 1 courtview.py, find the same name in the package, strip decorators, normalise Attribute(Name(<package module>), attr) to Name(attr), and compare ast.dump. List every differing name with a one-line reason (expected only: global-to-qualified changes, ROOT_DIR paths, _startup). Any other difference is a bug: fix it.

Run the full RPi suite, node pager tests, then cv-deploy (watch for HEALTH:ok and no rollback), remote checks (rpi 'grep -c "create_app" /opt/courtview/current/courtview.py', rpi 'ls /opt/courtview/current/courtview_app | wc -l', rpi 'systemctl is-active courtview', rpi 'journalctl -u courtview --since "-5 min" --no-pager | tail -20' showing the [startup] lines and no traceback), Playwright e2e. Commit with explicit paths: git add courtview_app/*.py courtview.py cv_purge_pii.py tests/cvload.py tests/test_auth.py tests/test_payments.py tests/test_pii.py tests/test_purge.py tests/test_reliability.py tests/test_security.py tests/test_paths.py tests/test_clubs.py (check git status first; never -A; no __pycache__); commit "refactor(261014-jrh): split courtview.py into the courtview_app package"; push.
  </action>
  <verify>
    <automated>wc -l courtview.py courtview_app/*.py; grep -rnE "^\s+(from|import) |from \.[a-z_]+ import" courtview_app/ | grep -v "^courtview_app/[a-z_]*.py:[0-9]*:from \. import" ; D=$(rpi 'mktemp -d'); COPYFILE_DISABLE=1 tar --exclude '__pycache__' -cf - courtview.py courtview.html cv_*.py courtview_app web tests/*.py | rpi "tar xf - -C $D"; rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail tests.test_cv_telegram tests.test_paths tests.test_clubs 2>&1 | tail -3; rm -rf $D"; node --test tests/test_pay_pager.mjs 2>&1 | grep -E '^# (pass|fail)'</automated>
  </verify>
  <done>courtview.py under 30 lines exposing app; every package module at or below ~800 lines (counts in SUMMARY); the import-rule grep prints nothing; suite OK with count >= Task 1 count (no test removed, new structure/static tests added); pager 11/11; url_map, hook order and AST equivalence report clean apart from listed expected differences; deployed with HEALTH:ok and no rollback; service active; e2e ALL PASS, 0 CSP errors; commit pushed.</done>
</task>

<task type="auto">
  <name>Task 3: Frontend api() helper and visibility-gated preloads; final deploy and docs</name>
  <files>courtview.html, tests/test_clubs.py, CLAUDE.md, .claude/docs/courtview-internals.md, .claude/docs/INDEX.md</files>
  <action>
HTML data layer (per requirement 3). Replace apiFetch (line 864) with one api(url, opts) helper near the CONFIG block, outside the PAY-PAGER markers:
- opts: timeoutMs (default API_TIMEOUT_MS = 20000, a named const), key (optional stale-guard key).
- Uses an AbortController; a timer aborts after timeoutMs. If key is given, api keeps a Map key -> controller; starting a new request with the same key aborts the previous one.
- Resolves with parsed JSON for 2xx. Rejects with an ApiError (class extending Error) with fields kind ('http' | 'timeout' | 'network' | 'parse' | 'stale'), status (number or 0), error (the body's "error" string when the body is JSON with one, else null), url, and message (body "message" when present, else a short generic text per kind). Non-2xx bodies are parsed as JSON when possible so callers keep access to 410 archived / 503 / "unavailable" codes.
- isStale(e) helper returns true for kind 'stale'; callers return silently on stale.
Convert every fetch( call site listed in context (and boot()'s /api/clubs fetch from Task 1) to api(). Preserve each call site's existing behaviour exactly: read the surrounding code first and map today's checks (r.ok, r.status === 410/503, data.error === 'unavailable', etc.) onto e.status / e.error; keep the same UI messages (where a site displayed e.message, ApiError.message must give equivalent text); keep every existing seq/club guard (revReqSeq, revPaySeq, statsLoaded checks, CLUB !== clubId checks) - the helper's key guard is in addition, never a replacement. Use key for the per-tab loaders where a newer request supersedes an older one (day grid, month, heatmap, court popularity, club info, membership, stats mix/coach, revenue summary/leaderboard, archive snapshot loads), scoped so requests that legitimately run in parallel (Compare's per-club revenue, the two archive financial fetches, warm-up) do not share a key. loadPayChunk keeps the PAY-PAGER contract: fetchPage = (win, skip) => api(url).catch(() => null), so payPagerRun still receives JSON or null. Warm-up keeps fire-and-forget semantics (.catch(() => {})).
After conversion, grep must show exactly one fetch( in the script (inside api()).

Visibility (per requirement 3): add whenVisible(fn, delayMs): after delayMs, run fn if document.visibilityState === 'visible', otherwise run it once on the next visibilitychange to visible. Use it for the INIT preloads (club info 200 ms, compare 500 ms, revenue 3000 ms, warmOtherClubs 6000 ms) and the same timers in switchClub. warmOtherClubs checks visibility before each staggered club batch and defers the remaining batches via whenVisible when hidden. pollWatches (watch notifications) stays on its setInterval unchanged: it is a user-requested alert, not a preload; note this in the SUMMARY. Foreground loads (loadDay, loadMonth, loadRealHeatmap, user tab activation) are not gated.

Tests: add to tests/test_clubs.py (HTML static checks, no browser): the inline script has exactly one "fetch(" occurrence; it contains "AbortController", "visibilitychange" and "/api/clubs"; the PAY-PAGER markers are still present once each. Run node pager tests (11/11 unchanged). Run the full RPi suite.

Deploy and final verification: cv-deploy (HEALTH:ok, no rollback); rpi 'grep -c "AbortController" /opt/courtview/current/courtview.html' > 0; rpi 'grep -c "/api/clubs" /opt/courtview/current/courtview.html' > 0; three club-id greps on the deployed courtview.html = 0; rpi 'systemctl is-active courtview' = active; rpi 'systemd-analyze security courtview.service --no-pager | tail -1' (expect about 1.5, same as Phase 4); status JSON fresh (python3 on the RPi reading /var/lib/courtview/status/availability.json and payments.json mtimes or their timestamp fields, payments within 30 min, availability within 6 h); /api/clubs anonymous 403 and authenticated 200 with no "@" via curl to 127.0.0.1:8766 on the RPi (token from the file into a variable, never echoed); Playwright e2e ALL PASS with 0 CSP errors (also open with the tab hidden is not testable headless; rely on the static check). Diff review: git diff 5709313..HEAD --stat plus the Task 2 equivalence report; list in the SUMMARY every behaviour change, which must be only the three listed items.

Docs (per requirement 4, keep tight): CLAUDE.md - Clubs section says the club list lives in courtview_app/clubs.py and is served at /api/clubs (replace the "CLUBS array and COMPARE_CLUBS in courtview.html" sentence); "Allowed proxy paths ... ALLOWED_PATHS in courtview.py" -> courtview_app/upstream.py; add a short Code layout line (courtview.py entry, courtview_app/ package, see internals for the module map); fix any other courtview.py function references (grep CLAUDE.md for "courtview.py"). courtview-internals.md - add a "Module map" section (a table, one line per module, under 20 lines), update the unit-test command at line 57 to the new tar list (courtview_app, --exclude __pycache__) and module list (tests.test_clubs), mention tests/cvload.py in one sentence, and grep the doc for "courtview.py" references that now point at a package module and fix them. INDEX.md - extend the courtview-internals.md description with "module map"; no new doc. Apply the writing rules: British English, no em dashes in CLAUDE.md is not required but keep consistent with the file.

Commit with explicit paths: git add courtview.html tests/test_clubs.py CLAUDE.md .claude/docs/courtview-internals.md .claude/docs/INDEX.md (never the untracked bookandgo/playtomic docs); commit "feat(261014-jrh): api() data layer, visibility-gated preloads, docs"; push.
  </action>
  <verify>
    <automated>awk '/<script>/{f=1} /<\/script>/{f=0} f' courtview.html | grep -c "fetch("; grep -c "AbortController\|visibilitychange" courtview.html; node --test tests/test_pay_pager.mjs 2>&1 | grep -E '^# (pass|fail)'; D=$(rpi 'mktemp -d'); COPYFILE_DISABLE=1 tar --exclude '__pycache__' -cf - courtview.py courtview.html cv_*.py courtview_app web tests/*.py | rpi "tar xf - -C $D"; rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail tests.test_cv_telegram tests.test_paths tests.test_clubs 2>&1 | tail -3; rm -rf $D"</automated>
  </verify>
  <done>Exactly one fetch( in the script (inside api()); suite OK at or above the Task 2 count; pager 11/11; deployed with HEALTH:ok, no rollback, service active; deployed greps pass and the three id greps are 0; systemd-analyze security about 1.5; status JSON fresh; /api/clubs 403 anon / 200 authed without PII; e2e ALL PASS, 0 CSP errors; docs updated; commit pushed.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| browser -> Flask | Untrusted requests; every route token-gated by _gate, GET/HEAD only |
| Flask -> Padelmates | Impersonated upstream traffic; must stay inside UpstreamClient |
| repo -> /opt/courtview release | deploy.sh tar list decides what code ships and what static_asset can see |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-jrh-01 | Information disclosure | /api/clubs | mitigate | _gate like api_health; route in EXPECTED_ENDPOINTS so the route-auth loop proves 403; test_pii no-PII loop covers it; public_clubs() returns 7 whitelisted keys only |
| T-jrh-02 | Elevation of privilege | create_app hook order | mitigate | before_request order (GET/HEAD guard, then 410 guard) and after_request order (security headers, access log) reproduced exactly; Task 2 url_map/hook comparison against the Task 1 app |
| T-jrh-03 | Information disclosure | static_asset after move | mitigate | ROOT_DIR keeps the same root; extension allowlist unchanged; new test that /courtview_app/*.py returns 404 with a valid token |
| T-jrh-04 | Tampering | upstream traffic escaping UpstreamClient after split | mitigate | AST test scans every package file, curl_cffi imported only in upstream.py |
| T-jrh-05 | Tampering | test isolation masking a regression | mitigate | tests/cvload.py fresh package per test module; R1 forbids runtime sys.modules lookups and function-level imports (AST-enforced) |
| T-jrh-06 | Information disclosure | HTML rendering of club data | mitigate | club names rendered through esc()/textContent as today; failure state uses static markup only |
| T-jrh-07 | Denial of service | browser fan-out | mitigate | 20 s timeout, keyed aborts of superseded requests, warm-up and preloads paused while hidden |
| T-jrh-SC | Tampering | package installs | accept | no new dependencies in this plan |
</threat_model>

<verification>
- Baseline counts recorded before any edit (expected 194 Python + 11 pager).
- After each task: full RPi suite from a temp copy of the new layout, count >= previous, all OK; node pager 11/11; cv-deploy HEALTH:ok with no rollback; remote greps; service active; Playwright e2e ALL PASS, 0 CSP errors.
- Final: wc -l per module (none above ~800); 0 hits for each club id in courtview.html (local and deployed); /api/clubs 403/200 without PII; systemd-analyze security about 1.5; status JSON fresh; diff review lists only the three intended behaviour changes; Task 2 equivalence report attached to the SUMMARY.
</verification>

<success_criteria>
- One club config source; Python sets derived from it; HTML loads it from /api/clubs with loading and error states; default-club logic unchanged.
- courtview_app/ package with the module map above, courtview.py thin entry, ExecStart unchanged.
- Tests patch real module paths, no assertion weakened, AST compliance covers the package.
- api() helper used by every fetch; warm-up and preloads visibility-gated; PAY-PAGER contract intact.
- Docs updated (CLAUDE.md, internals module map and test command, INDEX.md line).
- Three commits with explicit git add, pushed; deployed and verified on the RPi.
</success_criteria>

<output>
Create `.planning/quick/261014-jrh-phase5-structure-modules-club-config-fro/261014-jrh-SUMMARY.md` with: baseline and final test counts per module, module line counts, the Task 2 equivalence report, deploy/health/security/status results, e2e result, behaviour-change list, and an "Anything left open?" section. Commit it, add the STATE.md Quick Tasks row 42 (| 42 | Phase 5 structure ... | 2026-10-14 | <commit> | 261014-jrh-phase5-structure-modules-club-config-fro |), push, and run gsd-sdk query validate.health.
</output>
