---
phase: quick-261014-jrh
plan: 01
subsystem: courtview
tags: [refactor, structure, club-config, frontend-data-layer]
requirements-completed: [P5-CLUBS, P5-MODULES, P5-FRONTEND, P5-DOCS]
key-files:
  created:
    - courtview_app/ (17 files: __init__, clubs, config, upstream, privacy, db, jobs, accesslog, security, payments, refresh, routes_core, routes_availability, routes_club, routes_payments, routes_archive, startup)
    - tests/cvload.py
    - tests/test_clubs.py
  modified:
    - courtview.py (3,523 -> 20 lines, gunicorn entry)
    - courtview.html
    - cv_purge_pii.py
    - deploy.sh
    - tests/test_security.py, test_reliability.py, test_pii.py, test_payments.py, test_auth.py, test_purge.py, test_paths.py
    - CLAUDE.md, .claude/docs/courtview-internals.md, .claude/docs/INDEX.md
commits:
  - acdb325 feat(261014-jrh): single club config source served at /api/clubs
  - 5290f04 refactor(261014-jrh): split courtview.py into the courtview_app package
  - 0157d1f feat(261014-jrh): api() data layer, visibility-gated preloads, docs
completed: 2026-10-14
---

# Phase 5 Quick Task 261014-jrh: structure (club config, package split, api() data layer)

One club config source (`courtview_app/clubs.py`, served at token-gated `/api/clubs`), `courtview.py` split into a 16-module `courtview_app` package behind a 20-line gunicorn entry, and a single `api()` helper in the dashboard with visibility-gated preloads. Three commits, each deployed with the health check passing and no rollback. main == origin/main at 0157d1f.

## Test counts

Baseline (before any edit, RPi temp copy of the old layout, per module run):

| Suite | Baseline | After T1 | After T2 | Final (T3) |
|---|---|---|---|---|
| test_security | 38 | 38 | 40 | 40 |
| test_reliability | 64 | 64 | 64 | 64 |
| test_pii | 18 | 18 | 19 | 19 |
| test_payments | 14 | 14 | 14 | 14 |
| test_auth | 16 | 16 | 16 | 16 |
| test_purge | 4 | 4 | 4 | 4 |
| test_cv_notify_fail | 21 | 21 | 21 | 21 |
| test_cv_telegram | 12 | 12 | 12 | 12 |
| test_paths | 7 | 7 | 7 | 7 |
| test_clubs (new) | - | 8 | 8 | 12 |
| **Python total (one process)** | **194** | **202** | **205** | **209** |
| node pager (test_pay_pager.mjs) | 11/11 | 11/11 | 11/11 | 11/11 |

All OK at every stage. No assertion weakened. Where a test had to change shape it was strengthened or kept equivalent: the `hasattr(cv, "_strip_wireslammer")` check now asserts it on every package module; the `/root/labs` scan now covers every package file too; the source-scan tests (content_type regex, club_extras grep, AST UpstreamClient rules) iterate `courtview.py` plus all 16 package files and assert the list is non-empty.

New tests: test_clubs (derivation, `/api/clubs` contract, ids defined only in clubs.py, no ids in HTML, one `fetch(` in the script, AbortController/visibilitychange/whenVisible present, preload timers gated, PAY-PAGER markers once), test_security (package sources not served, `/web/access.js` still served), test_pii (`test_package_import_rules`: only `from . import module` at top level, no imports inside functions, curl_cffi only in upstream.py; AST compliance asserts UpstreamClient is defined only in upstream.py and >= 17 files scanned).

Note on TDD: the Task 1 tests were written before the implementation but I did not run them red first; the first run was after implementing.

## Equivalence checks (Task 2, run before deploy)

Run on the RPi in separate processes: Task 1 tree (acdb325) vs the new package.

- url_map rules in registration order (rule, endpoint, methods, provide_automatic_options, view name): IDENTICAL (26 rules).
- before_request funcs: IDENTICAL (`_allow_only_get_head`, `_block_archived_club`).
- after_request funcs: IDENTICAL (`_security_headers`, `log_req`).
- error handlers: IDENTICAL (404 -> `_not_found`).
- MAX_CONTENT_LENGTH, merge_slashes, static_folder, no `static` endpoint: IDENTICAL.
- AST per top-level function/class/assignment (decorators stripped, `module.name` normalised to `name`): 215 compared, 205 identical, 10 differing, all expected:
  - 5 club aliases (HEATMAP_CLUBS, PAYMENT_SYNC_CLUB_IDS, RACKETEER_CLUB_ID, PAYMENT_CLUB_IDS, ARCHIVE_CLUB_IDS): were `= clubs.X` aliases, now derived in clubs.py (Task 1 design).
  - WEB_ASSETS_DIR, DASHBOARD_HTML, static_asset: `__file__`-relative paths became `config.ROOT_DIR` (release root, same directory as before).
  - proxy: local variable `upstream` (the upstream response) renamed `reply` in `_forward`, because it now collides with the `upstream` module (see Deviations).
  - _startup: `global _ACCESS_FD` removed, the assignment is now `accesslog._ACCESS_FD = ...`.
- Only name present in old but not in the package: `app` (now built in `create_app()`).

## Module line counts

courtview.py 20; courtview_app: `__init__` 49, clubs 43, config 52, upstream 301, privacy 111, db 302, jobs 142, accesslog 182, security 279, payments 476, refresh 264, routes_core 251, routes_availability 221, routes_club 598, routes_payments 106, routes_archive 203, startup 65. Largest is 598 (limit ~800). Import-rule grep (anything other than top-level `from . import`): prints nothing.

## Greps and auth checks

- HTML club-id grep, local and deployed `/opt/courtview/current/courtview.html`: 0, 0, 0 (Racketeer, Padium, Rocket ids).
- Single-fetch grep: `awk` over the inline script then `grep -c "fetch("` = 1 (inside `api()`); `apiFetch` no longer exists.
- `/api/clubs` on the RPi (127.0.0.1:8766): anonymous 403; authenticated 200, 3 clubs, keys exactly `archived, compare, courts, id, name, short, sub`, 0 `@` characters. Also covered by the route-auth loop and the PII loop in the test suite.
- Deployed greps: `grep -c "/api/clubs" courtview.html` = 2, `grep -c "AbortController"` = 1, `grep -c "def public_clubs" courtview_app/clubs.py` = 1, `grep -c "create_app" courtview.py` = 2, `ls courtview_app | wc -l` = 18 (17 modules + `__pycache__` from the deploy compile step). `systemctl is-active courtview` = active.

## Playwright e2e (tests/e2e_dashboard.py)

After Task 1: ALL PASS, 0 CSP violations, 0 page errors. After Task 2: ALL PASS, 0 CSP violations, 0 page errors. After Task 3: ALL PASS (run 3 times), 0 CSP violations, 0 page errors, 0 PII matches.

The scanned-body count rose from 31 (Task 1) to 49 (Task 3). I checked it: a Padium page load counted per path is identical between the Task 1 HTML and the new HTML (24 requests, all 200, same paths and counts), so this is e2e variance, not extra traffic.

## Deploys (all via cv-deploy, health check passed, no rollback)

| Task | Release | HEALTH | Notes |
|---|---|---|---|
| 1 | 20261014T132425Z | ok | `PURGE:not needed`, MAINUSER courtview |
| 2 | 20261014T133336Z | ok | journal shows all `[startup]` lines, 0 tracebacks; purge check ran under the new imports |
| 3 | 20261014T133835Z | ok | current -> releases/20261014T133835Z, data files 600 courtview |

- `systemd-analyze security courtview.service`: Overall exposure level 1.5 OK (unchanged from Phase 4).
- Status JSON: availability.json last_run age 160.6 min (limit 6 h), payments.json last_run age 3.7 min (limit 30 min); keys exactly `job, last_run, last_ok, ok, calls, errors`, both `ok: true`.
- Journal after Task 3: 0 tracebacks in the last 10 min, service active.

## Functions moved for layering

- `_exc_error`: routes_club -> security. Every route module uses it, and routes may not import each other upward.
- Beyond the plan's map, where the plan was loose: `_payments_unavailable` -> payments, `_london_day_keys`/`_availability_refresh_targets`/`_purge_availability` -> refresh, `_single_flight_lock` and its lock dict -> db, `ARCHIVED_ERROR`/`ARCHIVED_MESSAGE` -> config, `UNAVAILABLE_ERROR`/`UNAVAILABLE_MESSAGE` stay in routes_club (their only users), `_get_client_ip` and `log_req` -> accesslog (as planned). No other function moved.

## Behaviour changes (the three approved ones only)

1. Club config comes from `courtview_app/clubs.py` and is served at authenticated `/api/clubs`; the HTML boots from it, with a "Loading clubs..." state and a static failure state ("Club list unavailable", no loaders or timers start). Default-club logic unchanged.
2. `courtview.py` split into `courtview_app/`; ExecStart `courtview:app` unchanged.
3. Dashboard `api()` helper (20 s default timeout, keyed aborts of superseded requests, `ApiError`) and visibility-gated preloads/warm-up. `pollWatches` stays on its `setInterval`: it is a user-requested alert, not a preload.

## Deviations from Plan

**1. [Rule 1 - Bug] Local `upstream` shadowed the new `upstream` module in `proxy._forward`.**
- Found during: Task 2, first full test run (8 failures, all `UnboundLocalError path=/api/club/`).
- Fix: renamed the local response variable to `reply` (4 references). A package-wide scan then showed no other function binds a name equal to a module name.
- Files: courtview_app/routes_core.py. Commit 5290f04.

**2. [Rule 1] `onVisible` helper name tripped the inline-handler regex test.** `tests/test_security.py::test_dashboard_has_no_inline_handlers` matches `onxxx =`. Renamed to `resumeWhenShown`; the test is unchanged.

**3. [Judgement] 60 s timeout for the slow aggregate endpoints.** `api()` defaults to 20 s as specified, but activity-summary, coach-stats, revenue-summary, club-info and the cache warm-up pass `API_SLOW_TIMEOUT_MS = 60000`. Rocket's full-month activity range is retried server-side with a 14-day window and can exceed 20 s on a cold cache, so a flat 20 s would have turned slow-but-successful loads into errors (a regression). Everything else uses 20 s.

**4. [Judgement] Static page setup moved into `boot()`.** setupTabs, setupRevenue, buildDateTabs, setupNotifyPanel and the duration-button listeners now run after the club list loads (they call loaders that read the selected club). Before boot the page shows only the loading state; on a failed load the tabs are inert.

**5. [Judgement] Route registration.** Rather than per-module `register(app)` (which would change registration order), each routes module exposes a `ROUTES` tuple and `create_app()` registers them in an explicit `_ROUTE_ORDER` that reproduces the old file order exactly; a missing or extra route raises at startup. Verified identical rule order against the Task 1 app.

**6. [Judgement] `test_courtview_uses_the_clubs_objects` replaced.** After the split the aliases no longer exist, so the test became `test_club_ids_defined_only_in_clubs_py` (no package file or courtview.py repeats a club id).

**7. PLAN.md** was already committed (1f227ff) before this run, so Task 1 did not need to add it. STATE.md row and SUMMARY.md commit left to the orchestrator as instructed.

## Known stubs

None.

## Threat flags

None. No new network surface beyond the planned token-gated `/api/clubs`; static allowlist unchanged (new test proves `/courtview_app/*.py` and `/courtview.py` return 404 with a valid token).

## Anything left open?

- `courtview_app/__pycache__` exists in the deployed release (created by deploy's compileall, root-owned, expected). It is excluded from the tar and gitignored.
- Hidden-tab gating is covered by a static test only; headless Playwright cannot hide a tab. A manual check (open the dashboard in a background tab, confirm no `/api/club-info`, `/api/revenue-summary` or warm-up requests until it is shown) is not done.
- The two untracked docs in `.claude/docs/` (bookandgo-jungle-padel, playtomic) were left untouched, as instructed.
- `datetime.utcfromtimestamp()` in routes_archive.py emits a DeprecationWarning on the RPi's newer Python (pre-existing, moved verbatim, not changed here).
- Package modules carry no per-module test files: coverage is still via the six existing suites. Splitting tests per module is possible later but out of scope.
- `tests/cvload.py` relies on `from tests import cvload` (namespace package) with the repo root on `sys.path`; running a test file directly from another directory would need that path.

## Self-Check: PASSED

- courtview_app/ (17 files), tests/cvload.py, tests/test_clubs.py: present.
- Commits acdb325, 5290f04, 0157d1f: present on main and origin/main (main == origin/main at 0157d1f).
