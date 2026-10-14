---
phase: quick-261014-ifv
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.py
  - courtview.html
  - tests/test_reliability.py
  - tests/test_pii.py
  - tests/test_security.py
  - tests/e2e_dashboard.py
  - .claude/docs/courtview-internals.md
  - CLAUDE.md
  - .planning/STATE.md
autonomous: true
requirements: [P3-AUTH401, P3-LASTOK, P3-STATIC, P3-MAXLEN, P3-HEADERS, P3-JSONCT, P3-ROUTEAUTH, P3-ACCESSLOG]

must_haves:
  truths:
    - "No code path calls /club/club_extras or /home/activity/get_booked_hours; neither is in ALLOWED_PATHS; no credential or auth header is sent to Padelmates anywhere"
    - "/api/booked-hours returns 410 {error:'unavailable', message:'No longer available from Padelmates'} after the token gate, with zero upstream calls"
    - "Club Info renders without an extras section, the Stats KPI area shows 'No longer available from Padelmates', and Compare has no Activities/Occupancy columns (footnote says why); no spinner is left running and the browser makes no /api/booked-hours request"
    - "A 401 from upstream is never retried, never cached, logged once per endpoint per process, and counted (totals.unauthorized and per-endpoint unauthorized) in /api/health"
    - "status/availability.json and payments.json have exactly the keys job, last_run, last_ok, ok, calls, errors; last_ok is the start time of the latest ok run, carried unchanged on failed runs, persisted in job_state, seeded from last_run where ok=1 on first deploy; files 0644 and written atomically"
    - "Every response carries X-Frame-Options DENY, X-Content-Type-Options nosniff and Referrer-Policy no-referrer; HTML responses carry a strict CSP (script-src by sha256 hash, no 'unsafe-inline' for scripts); /api/* and /access/data carry Cache-Control no-store"
    - "The static route returns 403 without a token and 404 with a token for '..', absolute paths, backslashes, %2e%2e, %2f and %5c variants, using werkzeug safe_join"
    - "MAX_CONTENT_LENGTH is 1 MiB; proxied upstream JSON always goes out as application/json (non-JSON upstream bodies become a 502)"
    - "A url_map coverage test proves every route returns 403 without a token, with an explicit (empty) public allowlist"
    - "Access log labels come from the gate result (OK/TOKEN/BLOCKED/RATE_LIMITED); the access page title really reads 'CourtView Access Log'; access.log rotates by size, keeping N files at 0600"
    - "Phase 1/2 invariants hold: no PII stored or returned, AstTests pass, GET/HEAD only, Racketeer archive-only, job_state schedules"
  artifacts:
    - path: "courtview.py"
      provides: "401 rule in UpstreamClient, last_ok in job_state/status JSON, safe static route, security headers, JSON relay, gate-derived access labels, access.log rotation"
      contains: "safe_join"
    - path: "tests/test_security.py"
      provides: "route-auth coverage, traversal, headers/CSP, MAX_CONTENT_LENGTH, JSON content type, access labels, rotation, retitle tests"
      min_lines: 150
    - path: "tests/test_reliability.py"
      provides: "401 rule tests, last_ok status tests, removed-endpoint tests"
      contains: "last_ok"
    - path: "tests/e2e_dashboard.py"
      provides: "console CSP violation check, /access visit, no /api/booked-hours request"
      contains: "console"
  key_links:
    - from: "courtview.py _job_record"
      to: "job_state.last_ok"
      via: "SELECT previous last_ok, INSERT OR REPLACE with carried value"
      pattern: "last_ok"
    - from: "courtview.py _load_dashboard"
      to: "Content-Security-Policy script-src"
      via: "sha256 of the inline <script> body computed from the served bytes"
      pattern: "sha256-"
    - from: "courtview.py _gate"
      to: "log_req access label"
      via: "flask.g.cv_auth"
      pattern: "cv_auth"
---

<objective>
Phase 3 of the CourtView review (security hygiene) plus two user-approved additions: drop the two Padelmates endpoints that now demand auth, and add `last_ok` to the status JSON for the doxxnet-labs heartbeat.

Purpose: close the remaining review findings F4, F10, F11, F13 and F17 (and F5's body cap), stop calling auth-only upstream endpoints without ever trying to authenticate, and give labs a freshness signal that survives failed runs and restarts.
Output: updated courtview.py and courtview.html, new tests/test_security.py, extended reliability/pii/e2e tests, internals doc and STATE row, deployed and verified on the RPi.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@./CLAUDE.md
@.planning/STATE.md
@.claude/docs/courtview-internals.md
@/Users/herwy/dev/doxxnet-labs/.claude/docs/research/courtview-review-2026-10-14/security-ops.md
@/Users/herwy/dev/doxxnet-labs/tests/test_auth_policy.py

Current code map (re-verified 2026-10-14 against HEAD c7efaf9; line numbers drift, grep the symbol):
- courtview.py:60 `ALLOWED_PATHS` (contains "/club/club_extras"); :91 `class UpstreamClient` with `reset()`, `_attempt(url, key, timeout)` (per-endpoint dict `calls/ok/non_200/errors/last_status/last_at`, prints `[upstream] non-200 status=... path=...` on every non-200), `get(path, params, *, timeout)` (retryable = connection error, 429 within Retry-After cap, 502/503/504; breaker failure = exception, >=500, 429), `snapshot()` (started_at, breaker, totals, endpoints).
- :1057 `job_state` CREATE TABLE (job, last_run, ok, calls, errors, updated_at); :1197 `_job_due_in` (seed INSERT OR IGNORE); :1219 `_job_record(job, started_at, ok, calls, errors)`; :1246 `_write_job_status(job, last_run, ok, calls, errors)`; callers :2560-2561 (payments) and :3305-3306 (availability); heatmap calls `_job_record` only (:3248). :2929 `_job_states()` feeds /api/health.
- :692-811 access log: `_ACCESS_LOG` deque(500), `_ACCESS_FD`, `_load_web_assets`, `_strip_wireslammer` (replaces the literal `<title>doxxnet Labs</title>`, but labs' access.html title is `<title>Access Log - doxxnet Labs</title>`, so the retitle no-ops), `_log_access`.
- :1337 `app = Flask(__name__)`; :1343 `_load_dashboard`; :1358 `_forbidden()` (403 HTML with its own CSP); :1370 `_gate()` returns (passed, query_token); :1388 `_allow_only_get_head`; :1396 `_block_archived_club` (validates the token itself, then 410).
- :1434 `static_asset(filename)`: extension check BEFORE `_gate` (404 oracle), then `os.path.join(dirname, filename)` (F4 traversal).
- :1790 `api_club_info` (targets include ("extras", "/club/club_extras"); caches only when all targets succeed); :1896 `api_booked_hours`.
- :2957 `proxy(p)` `_forward()` relays upstream `content-type` raw and returns `{"error": str(exc)}` on exceptions.
- :3028 `log_req` after_request: label from status code (429 RATE_LIMITED, 403 BLOCKED, ?token TOKEN, else OK), so 404/410 are logged OK.
- :3046 `/access`, :3063 `/access.js`, :3072 `/dashboard.css`, :3081 `/access/data`. :3121 stale comment "Heatmap: server-side get_booked_hours aggregation" (heatmap uses /club/statistics/operational).
- :3341 `_startup` opens access.log with `open(..., "a")` and `readlines()` the whole file. gunicorn runs 1 worker, gthread x4 (courtview.service), so in-process locking is enough for rotation.
- courtview.html: one inline `<style>` (line 9) and one inline `<script>` (line 850); Google Fonts `<link>` at lines 7-8; 4 inline `onclick="pickDur(this)"` handlers (lines 697-700); ~40 static `style="..."` attributes plus generated ones. booked-hours callers: Compare (~2161, cache key `act-<club>-<mk>`, columns "Activities (mo)" and "Occupancy (mo)" rendered in `renderCompareTable` ~2205-2230, header ~2114), Stats KPIs (~2280-2300, "Activities this month" / "Booked hours this month"), warm-up URL list (~3050). Extras: `loadClubInfo` ~1872-1875 and `renderClubInfo` ~2025-2059.
- Labs access page (RPi /root/labs/web, read-only from here): access.html links /favicon.ico, /favicon.svg, /dashboard.css and `<script src="/access.js">`; access.js fetches /access/data only after Wireslammer stripping; its labels are BLOCKED/TOKEN/OK/RATE_LIMITED.
- Tests: tests/test_pii.py fake handler serves /club/club_extras and /home/activity/get_booked_hours (~105-116) and `IngestTests` expects 200 from /api/booked-hours (~268); `AstTests` at ~197. tests/test_reliability.py `StatusFileTests` (~626) asserts the five-key set. tests/test_auth.py `AuthBase` shows the token/cookie fixture pattern (cv.TOKEN_PATH, cv._token_cache, cv._AUTH_FAILS, UPSTREAM._new_session patch).
- Status files live now on the RPi: availability.json `{"job","last_run":"2026-10-14T11:02:28Z","ok":true,"calls":56,"errors":0}`, payments.json similar, both 0644 in a 0755 dir.

Test run (from internals doc): copy root *.py, courtview.html and tests/ to an RPi temp dir, run with COURTVIEW_SKIP_STARTUP=1, remove the dir afterwards.
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Retire auth-only upstream endpoints, add the generic 401 rule, and add last_ok to job_state and status JSON</name>
  <files>courtview.py, courtview.html, tests/test_reliability.py, tests/test_pii.py</files>
  <behavior>
    - "/club/club_extras" and "/home/activity/get_booked_hours" are absent from ALLOWED_PATHS and from courtview.py code lines (comments excluded).
    - GET /api/booked-hours with a valid cookie returns 410 with exactly {error:"unavailable", message:"No longer available from Padelmates"} and the fake upstream records zero calls; without a token it returns 403; with the Racketeer club_id it still returns the existing archived 410.
    - /api/club-info makes exactly 3 upstream calls (/club/, /club/membership/, /club/creditpackage/), caches when those 3 succeed, and its JSON has no "extras" key, including when a stored legacy row containing "extras" is served.
    - A fake upstream answering 401 on the generic proxy: one upstream call (no retry), response is 502 JSON {error:"upstream_auth_required"}, nothing written to the availability cache or stats_cache, the journal gets exactly one "[upstream] auth required path=..." line for two calls to the same path and a second line for a different path, health totals.unauthorized == calls and the endpoint entry has unauthorized == calls, and the breaker stays closed.
    - _job_record ok=True sets last_ok=started_at; a following ok=False keeps the previous last_ok; a fresh connection (restart) reads the same last_ok; a never-ok job has last_ok null.
    - Migration: a legacy job_state table without last_ok gains the column on init_db and rows with ok=1 get last_ok=last_run; rows with ok=0 stay null; running init_db twice is a no-op.
    - _write_job_status output keys are exactly job, last_run, last_ok, ok, calls, errors, file mode 0644 under umask 077, dir 0755, no temp leftovers; last_ok is ISO 8601 Z or null.
    - _rewrite_status_files() rewrites availability.json and payments.json from job_state rows (skipping jobs with no row) so the first deploy has last_ok without waiting for a run.
  </behavior>
  <action>
Write the tests first in tests/test_reliability.py (new classes for the 401 rule, retired endpoints and last_ok; update StatusFileTests to the six-key set) and run them red, then implement in courtview.py.

Per user decision A (no auth, no credentials, stop calling): delete "/club/club_extras" from ALLOWED_PATHS. In api_club_info drop the ("extras", ...) target, the extras special-case in `_fetch`, and the "extras" key from `results`; the all-good check now covers the remaining three. Add a small helper that loads a stored club_info_cache payload, pops "extras", and re-serialises it, and use it on every stored-row read path in api_club_info (fresh hit, single-flight re-check and stale fallback) after `_scrub_json_text`. Replace the body of api_booked_hours with: `_gate()` (403 on failure), then return 410 JSON {"error": "unavailable", "message": "No longer available from Padelmates"} via module constants UNAVAILABLE_ERROR / UNAVAILABLE_MESSAGE, setting the cookie when via_query as the other routes do. Keep the route (chosen over deletion because deletion would fall into the generic proxy and answer 403 "path not proxied", which reads like an auth failure). Fix the stale comment above `_HEATMAP_ARRAYS` to say the heatmap uses /club/statistics/operational.

401 rule in UpstreamClient: in `reset()` add `self._auth_logged = set()` and totals key "unauthorized": 0; per-endpoint dict gains "unauthorized": 0. In `_attempt`, when status == 401 increment both counters and print "[upstream] auth required status=401 path={key}" only if key is not yet in `_auth_logged` (add it), and skip the generic non-200 print for 401 so it is truly logged once per endpoint per process. In `get()`, make 401 explicitly non-retryable (it already falls in "other 4xx", but write the explicit branch so a future edit cannot retry it) and not a breaker failure. Never cache: in proxy `_forward`, if upstream.status_code == 401 return 502 jsonify {"error": "upstream_auth_required"} before any scrub or store (so the browser never sees an upstream 401 that looks like CourtView auth). Verify by grep that every other caller only caches on status 200 (club-info, membership, activity-summary, revenue-summary, coach-stats, day-activities, heatmap, payments, availability refresh) and note the result in the SUMMARY.

last_ok per user decision B: in init_db, after the CREATE TABLE, read PRAGMA table_info(job_state); if last_ok is missing, ALTER TABLE job_state ADD COLUMN last_ok INTEGER and UPDATE job_state SET last_ok=last_run WHERE ok=1 AND last_ok IS NULL (this is the first-deploy seed). Include last_ok in the CREATE TABLE for new DBs. In `_job_due_in` the seed INSERT also sets last_ok=seed (seed rows have ok=1). `_job_record` reads the existing last_ok in the same connection, sets it to started_at when ok, keeps the old value otherwise, writes it with INSERT OR REPLACE, and returns it (None on sqlite error). `_write_job_status(job, last_run, ok, calls, errors, last_ok)` writes keys in the order job, last_run, last_ok, ok, calls, errors, with last_ok `_iso_z(last_ok)` or null; keep the existing atomic temp file + fsync + fchmod 0644 + os.replace + dir 0755 logic. Update both callers to pass the value returned by `_job_record`. Add `_rewrite_status_files()` that reads job_state for "availability" and "payments" and calls `_write_job_status` for each existing row; call it in `_startup()` right after `init_db()`. Add last_ok (ISO Z or null) to `_job_states()` so /api/health shows it.

tests/test_pii.py: remove the fake-handler branches for the two retired paths, and in IngestTests take /api/booked-hours out of the 200 list and assert it returns 410 instead. Keep AstTests untouched and passing.

courtview.html per decision A (UI): remove the extras parameter and section from renderClubInfo and its call in loadClubInfo (also drop extrasList from the empty-state condition). Stats: delete the two booked-hours fetches and the KPI loop's data dependency; render the KPI area as a single muted `.empty` block reading "Activities and booked hours: No longer available from Padelmates" so no spinner remains. Compare: delete the booked-hours fetch and the act-* cache use, remove the "Activities (mo)" and "Occupancy (mo)" header and body cells (keep column indices in renderCompareTable consistent), and add one footnote line under the table: "Activities and occupancy: No longer available from Padelmates". Remove the booked-hours URL from the warm-up list. After editing, `grep -n "booked-hours\|extras" courtview.html` must return nothing.
  </action>
  <verify>
    <automated>cd /Users/herwy/dev/courtview && D=$(rpi 'mktemp -d') && tar cf - courtview.py courtview.html cv_*.py tests/*.py | rpi "tar xf - -C $D" && rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail; rc=\$?; rm -rf $D; exit \$rc" && grep -v '^\s*#' courtview.py | grep -c 'club_extras\|get_booked_hours' | grep -qx 0 && ! grep -n 'booked-hours\|extras' courtview.html</automated>
  </verify>
  <done>All six existing suites pass with the new tests; courtview.py code lines and courtview.html contain no reference to either retired endpoint; status writer emits the six keys with a carried-forward last_ok; 401s are counted, logged once per endpoint, never retried or cached.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Security hygiene: safe static route, body cap, security headers and CSP, JSON relay, route-auth coverage test, access log labels and rotation</name>
  <files>courtview.py, courtview.html, tests/test_security.py</files>
  <behavior>
    - Route coverage: for every rule in cv.app.url_map (sample URL substitutes `<path:filename>` with "x.js" and `<path:p>` with "club/"), GET and HEAD without a token return 403; PUBLIC_ENDPOINTS = frozenset() is explicit and the set of endpoints equals a hardcoded EXPECTED_ENDPOINTS set, so adding a route without updating the test fails.
    - Traversal (with a valid cookie, a sentinel file created outside the app dir): "/..%2f..%2fetc%2fpasswd.js", "/%2e%2e/%2e%2e/x.js", "/a/../../x.js", "/..\\x.js", "/%5c..%5cx.js", "//etc/x.js", "/%252e%252e/x.js" all return 400 or 404 and never the sentinel bytes; the same URLs without a token return 403.
    - app.config["MAX_CONTENT_LENGTH"] <= 1048576; a POST with a 2 MB body returns 403 or 413, never 500.
    - Headers on 200 HTML (/), 200 JSON (/api/health), 302 (/?token=), 403, 404, 410 (/api/booked-hours) and 405/500: X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy no-referrer. / has the dashboard CSP whose script-src contains exactly one 'sha256-...' equal to base64(sha256(inline script body)) and no 'unsafe-inline' in script-src; 403 keeps the _forbidden CSP; /api/* and /access/data have Cache-Control no-store.
    - Proxy: upstream 200 with content-type text/html and a JSON-looking body is served as application/json only if it parses as JSON, else 502 {"error":"upstream returned non-JSON"}; an exception gives 502 {"error":"upstream unavailable"} with no exception text.
    - Access labels: valid cookie request logged OK; valid ?token= logged TOKEN; no token BLOCKED; rate-limited request RATE_LIMITED; authenticated 404 logged OK; unauthenticated unknown extension BLOCKED; Racketeer 410 with valid cookie logged OK.
    - Retitle: given an access.html fixture with `<title>Access Log - doxxnet Labs</title>`, the served page has `<title>CourtView Access Log</title>`; when no title tag exists a "[startup] WARNING" line is printed.
    - Rotation: with ACCESS_LOG_MAX_BYTES patched small and ACCESS_LOG_KEEP=3, repeated _log_access produces access.log, .1, .2, .3 at most (no .4), all mode 0600, newest entries in access.log.
  </behavior>
  <action>
Write tests/test_security.py first (unittest, same fixture style as tests/test_auth.py AuthBase: temp TOKEN_PATH, cleared _token_cache and _AUTH_FAILS per test, UPSTREAM._new_session patched, COURTVIEW_SKIP_STARTUP=1), modelled on doxxnet-labs tests/test_auth_policy.py `test_every_endpoint_has_a_policy` and `test_matrix_no_credential_403`. Run red, then implement.

Static route (F4, decision C): import safe_join from werkzeug.security. Call `_gate()` first so every route needs auth (removes the unauthenticated 404 oracle). Then return 404 when the filename contains "..", "\\", "%", a NUL, or starts with "/", or when the raw request URI (request.environ RAW_URI, falling back to REQUEST_URI, lower-cased) contains "%2e", "%2f" or "%5c"; otherwise resolve with safe_join(app_dir, filename) and 404 on None, then keep the extension allowlist and content-type map.

Body cap: app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 right after app creation (Caddy already caps at 1 MB).

Security headers: add one `@app.after_request` hook `_security_headers` that sets X-Frame-Options DENY, X-Content-Type-Options nosniff and Referrer-Policy no-referrer on every response; uses setdefault for Content-Security-Policy so _forbidden's CSP survives; sets Cache-Control no-store for paths starting /api/ and for /access/data. CSP values as module constants: CSP_DASHBOARD = "default-src 'self'; script-src 'self' 'sha256-<hash>'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" (the hash filled at `_load_dashboard` from the exact bytes between the inline `<script>` and `</script>` of the served HTML; if extraction fails, log a WARNING and fall back to script-src 'self' only, which breaks the page visibly rather than weakening it). CSP_ACCESS for /access (labs page: external script and css, inline style attributes): "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'". CSP_API for everything else: "default-src 'none'; frame-ancestors 'none'". style-src keeps 'unsafe-inline' because courtview.html has ~40 static style attributes plus many generated in innerHTML templates, which no hash or nonce can cover without a rewrite; script-src is hash-only. Record this rationale in the code comment above the constants. In courtview.html replace the four onclick="pickDur(this)" attributes with a single delegated listener registered inside the inline script (querySelectorAll('.dbtn[data-dur]') addEventListener click calling pickDur), and grep the whole file for any other inline on*= attribute or javascript: URL, including inside template strings, converting each the same way.

JSON relay (F11): in proxy `_forward`, ignore the upstream content-type. For status 200, try json.loads on the decoded body; on failure return 502 {"error": "upstream returned non-JSON"}; otherwise scrub as today and return with content_type application/json. Non-200 upstream bodies are relayed as application/json only if they parse, else 502 with the same message. The exception branch returns 502 {"error": "upstream unavailable"} and prints only type(exc).__name__. The 401 branch from Task 1 stays first. Grep every other Response(...) in /api routes and confirm content_type is application/json; fix any that are not.

Access log (F13, decision C): `_gate()` records its outcome in flask.g.cv_auth: "RATE_LIMITED" when `_is_rate_limited` rejects, "BLOCKED" on missing or bad token, "TOKEN" when passed via ?token=, "OK" when passed via cookie. `_block_archived_club` sets g.cv_auth = "OK" before its 410. The /access and /access/data 429 branches set "RATE_LIMITED". `log_req` uses g.get("cv_auth"), defaulting to "RATE_LIMITED" for a 429 and "BLOCKED" otherwise (requests that never reached the gate, such as non-GET 403s). In `_strip_wireslammer` replace the literal title swap with a regex on the first `<title>...</title>` (case-insensitive, DOTALL) and print a "[startup] WARNING: access page title not found" line when nothing was replaced. Rotation: module constants ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024 and ACCESS_LOG_KEEP = 5. Add `_open_access_log()` that opens with os.open(O_WRONLY|O_CREAT|O_APPEND, 0o600), fchmods 0600 and wraps with os.fdopen(..., "a", buffering=1); use it in `_startup`. In `_log_access`, under _ACCESS_LOCK after the write, if the file size exceeds the cap: close the fd, shift access.log.(KEEP-1)..1 up by one (deleting .KEEP), rename access.log to access.log.1, chmod every rotated file 0600, reopen via `_open_access_log`. Any OSError is printed with the type name only and never breaks the request. The startup preload keeps reading only the current access.log (now bounded). Do not edit files under /root/labs/web (CLAUDE.md rule); Phase 4 owns the access page.
  </action>
  <verify>
    <automated>cd /Users/herwy/dev/courtview && D=$(rpi 'mktemp -d') && tar cf - courtview.py courtview.html cv_*.py tests/*.py | rpi "tar xf - -C $D" && rpi "cd $D && COURTVIEW_SKIP_STARTUP=1 python3 -m unittest tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail; rc=\$?; rm -rf $D; exit \$rc" && node --test tests/test_pay_pager.mjs && ! grep -nE '\son[a-z]+=' courtview.html</automated>
  </verify>
  <done>tests/test_security.py passes alongside all existing suites and the pager test; every route is token-gated with an explicit empty public allowlist; traversal variants never escape the app dir; every response carries the security headers with a hash-based script CSP on the dashboard; access labels follow the gate result and access.log rotates at 0600.</done>
</task>

<task type="auto">
  <name>Task 3: E2E CSP check, docs, deploy and on-RPi verification</name>
  <files>tests/e2e_dashboard.py, .claude/docs/courtview-internals.md, CLAUDE.md, .planning/STATE.md</files>
  <action>
E2E: in tests/e2e_dashboard.py add page.on("console") and page.on("pageerror") collectors; fail a new check "no CSP violations" when any console message text contains "Content Security Policy" or "Refused to", and "no page errors" when pageerror fired. Add checks that no request URL contains /api/booked-hours, that the Stats tab shows "No longer available from Padelmates", that the Compare tab shows the footnote, and that no ".sp" spinner is visible on the Club Info, Stats and Compare tabs after the existing waits. Add a scenario that visits /access with the cookie set and asserts the title is "CourtView Access Log" and no CSP console errors. Never print the token.

Docs (.claude/docs/courtview-internals.md): replace the 2026-10-14 401 bullet with the current state: both endpoints retired (not in ALLOWED_PATHS, /api/booked-hours answers 410 "unavailable", UI shows "No longer available from Padelmates"), the 401 rule (never retried or cached, logged once per endpoint per process, `unauthorized` counters in /api/health, relayed as 502 upstream_auth_required), and the note that Padelmates is adding auth and other endpoints may follow; CourtView must not add credentials. Remove the get_booked_hours row from the endpoint table. Update "Status JSON and health" to the six keys with last_ok semantics (seeded from ok rows, carried on failure, persisted in job_state.last_ok, rewritten at startup) and job_state's new column. Add a "Security headers" section (header list, the three CSP profiles, why style-src keeps 'unsafe-inline' and script-src is hash-only, safe_join static route, MAX_CONTENT_LENGTH 1 MiB, JSON-only relay, tests/test_security.py route coverage with its explicit public allowlist), and update the access log facts (gate-derived labels, rotation 1 MiB x 5 at 0600, title regex). Add tests.test_security to the unit test command. CLAUDE.md: add access.log rotation to the RPi layout row; no other changes unless a fact there is now wrong (grep it for booked-hours and club_extras).

Commit with explicit paths only: git add courtview.py courtview.html tests/test_security.py tests/test_reliability.py tests/test_pii.py tests/e2e_dashboard.py .claude/docs/courtview-internals.md CLAUDE.md .planning/quick/261014-ifv-phase3-security-hygiene-and-auth-require/261014-ifv-PLAN.md (never -A; the two untracked .claude/docs research files are not ours). Push.

Deploy with cv-deploy, then run every on-RPi check below and paste the outputs (counts and keys only, never the token):
1. rpi 'systemctl is-active courtview' is active; rpi 'grep -c "safe_join" /root/projects/courtview/courtview.py' and rpi 'grep -c "No longer available from Padelmates" /root/projects/courtview/courtview.html' both > 0.
2. Security headers: on the RPi, read the token into a shell variable (never echo it) and curl -sI with Cookie courtview_token against https://127.0.0.1:8766 or the local bind (http://127.0.0.1:8766) for /, /api/club/ (proxy path, append ?club_id=<Padium id>) and /access; show the X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Content-Security-Policy and Cache-Control lines.
3. Traversal: with the cookie, curl -s -o /dev/null -w '%{http_code}' --path-as-is for /..%2f..%2fetc%2fpasswd.js, /%2e%2e/%2e%2e/x.js, /..\\x.js and /a/../../x.js; all 400 or 404. Without the cookie, 403.
4. rpi 'journalctl -u courtview --since "<deploy time>" | grep -c "club_extras\|get_booked_hours\|status=401"' is 0, re-checked after the dashboard warm-up (run the e2e first).
5. /api/health (with cookie) piped through python3 -c to print sorted endpoint paths: neither retired path present; totals contains unauthorized; jobs show last_ok.
6. rpi 'for f in availability payments; do stat -c "%a %n" /root/projects/courtview/status/$f.json; python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(list(d), d[\"last_ok\"], d[\"ok\"])" /root/projects/courtview/status/$f.json; done' shows 644, keys [job, last_run, last_ok, ok, calls, errors] and a non-null last_ok (both current runs were ok, so seeding must have filled it). Also stat access.log shows 600.
7. Playwright: CV_TOKEN="$(rpi 'cat /root/.courtview_token')" python3 tests/e2e_dashboard.py prints RESULT: ALL PASS including the new CSP, booked-hours and /access checks.
8. AST compliance is part of tests.test_pii (already run); state the AstTests result explicitly.

Finally add STATE.md Quick Tasks row 40 (| 40 | description | 2026-10-14 | <commit> | [261014-ifv-phase3-security-hygiene-and-auth-require](.planning/quick/261014-ifv-phase3-security-hygiene-and-auth-require/) |), write the SUMMARY.md (include the status JSON keys verbatim for the labs session and the cache-on-200 grep result from Task 1), commit both with explicit paths, push, and run gsd-sdk query validate.health.
  </action>
  <verify>
    <automated>cd /Users/herwy/dev/courtview && rpi 'systemctl is-active courtview && grep -c "safe_join" /root/projects/courtview/courtview.py && python3 -c "import json; [print(list(json.load(open(f\"/root/projects/courtview/status/{j}.json\")))) for j in (\"availability\",\"payments\")]"' && CV_TOKEN="$(rpi 'cat /root/.courtview_token')" python3 tests/e2e_dashboard.py | tail -1 | grep -q "ALL PASS"</automated>
  </verify>
  <done>Deployed and active; all eight on-RPi checks shown with results; status JSON keys exactly [job, last_run, last_ok, ok, calls, errors] at 0644 with last_ok populated; Playwright ALL PASS with zero CSP console errors; docs, STATE row and SUMMARY committed with explicit paths and pushed; health check reported.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| browser -> Flask (via Caddy) | URL paths, query strings and cookies are untrusted |
| Padelmates -> Flask | third-party, unauthenticated upstream; body, status and content-type untrusted |
| Flask -> filesystem | static route and access.log writes |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-ifv-01 | Information disclosure | static_asset | mitigate | gate first, reject .., \\, %, absolute, raw %2e/%2f/%5c, safe_join, extension allowlist; test_security traversal matrix |
| T-ifv-02 | Elevation of privilege | new or existing routes | mitigate | url_map coverage test with explicit empty public allowlist and exact endpoint set |
| T-ifv-03 | Tampering (XSS) | dashboard and access page | mitigate | hash-only script-src, no inline handlers, frame-ancestors 'none', X-Frame-Options DENY, nosniff |
| T-ifv-04 | Tampering | proxy relay | mitigate | force application/json, reject non-JSON upstream bodies with 502, scrub unchanged |
| T-ifv-05 | Denial of service | request bodies | mitigate | MAX_CONTENT_LENGTH 1 MiB plus GET/HEAD-only guard |
| T-ifv-06 | Denial of service | access.log growth | mitigate | size rotation 1 MiB x 5, preload reads only the current file |
| T-ifv-07 | Spoofing | upstream auth | accept | Padelmates 401s are not worked around; endpoints retired, no credentials stored or sent |
| T-ifv-08 | Information disclosure | caches/logs | mitigate | 401 never cached, log lines carry path and status only, error JSON carries no exception text |
| T-ifv-09 | Information disclosure | style-src 'unsafe-inline' | accept | style injection only; script-src stays hash-only; rationale documented |
</threat_model>

<verification>
- Unit suites on the RPi temp copy: test_security, test_reliability, test_pii (incl. AstTests), test_payments, test_auth, test_purge, test_cv_notify_fail all OK.
- node --test tests/test_pay_pager.mjs 11/11.
- Playwright e2e ALL PASS with zero CSP console errors and zero /api/booked-hours requests.
- Post-deploy RPi checks 1-8 from Task 3 shown in the SUMMARY.
</verification>

<success_criteria>
- Retired endpoints gone from ALLOWED_PATHS and every code path; UI shows "No longer available from Padelmates" with no broken spinners.
- Generic 401 rule live and visible in /api/health.
- Status JSON keys exactly job, last_run, last_ok, ok, calls, errors; last_ok persisted and carried forward.
- Security headers on /, /api/club/ and /access; traversal 404/400; route coverage test green.
- Access labels correct, page title correct, access.log rotating at 0600.
- Commit and push with explicit paths; STATE row 40; health reported.
</success_criteria>

<output>
Create `.planning/quick/261014-ifv-phase3-security-hygiene-and-auth-require/261014-ifv-SUMMARY.md` when done
</output>
