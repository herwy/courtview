---
phase: quick-261014-ifv
plan: 01
subsystem: security-hygiene
tags: [csp, security-headers, traversal, upstream-401, status-json, access-log]
requires: [quick-261014-g80]
provides:
  - retired club_extras and get_booked_hours (no credentials ever sent)
  - generic upstream 401 rule with health counters
  - last_ok in job_state and status JSON
  - security headers, hash-only script CSP, hardened static route, JSON-only relay
  - gate-derived access labels, access.log rotation
key-files:
  created: [tests/test_security.py]
  modified: [courtview.py, courtview.html, tests/test_reliability.py, tests/test_pii.py, tests/e2e_dashboard.py, .claude/docs/courtview-internals.md, CLAUDE.md]
decisions:
  - "/api/booked-hours kept as a 410 route (deleting it would fall into the proxy and answer a 403 that reads like an auth failure)"
  - "style-src keeps 'unsafe-inline' (about 40 static plus generated style attributes); script-src is hash-only"
  - "Flask's built-in /static route disabled and a 404 handler gates unmatched URLs"
metrics:
  completed: 2026-10-14
  commits: [96ea93c, a4fcf56, 402e34f]
---

# Phase quick-261014-ifv: Phase 3 security hygiene and auth-required endpoints

Retired the two Padelmates endpoints that now return 401, added a generic 401 rule, `last_ok` in the status JSON, and the Phase 3 hygiene fixes (F4, F5 body cap, F10, F11, F13, F17) plus the F16 override (no `str(exc)` in any error JSON). Deployed and verified on the RPi.

## Commits

- 96ea93c: retire endpoints, 401 rule, last_ok, generic errors (Task 1)
- a4fcf56: static route, headers/CSP, JSON relay, access labels and rotation, tests/test_security.py (Task 2)
- 402e34f: e2e CSP checks, internals doc, CLAUDE.md (Task 3). The plan's PLAN.md was already committed (d45e657). SUMMARY.md and STATE.md are left for the orchestrator.
- main == origin/main after every push (final 402e34f).

## Test results (RPi temp copy, COURTVIEW_SKIP_STARTUP=1)

- `tests.test_security tests.test_reliability tests.test_pii tests.test_payments tests.test_auth tests.test_purge tests.test_cv_notify_fail`: Ran 167 tests, OK (131 before test_security and the new reliability tests were added; 36 in test_security).
- `node --test tests/test_pay_pager.mjs`: 11 pass, 0 fail.
- AST compliance: `tests.test_pii.AstTests.test_cffi_calls_compliant` Ran 1 test, OK (UpstreamClient is still the only code touching cffi_requests/TARGET).
- Retired endpoint grep: non-comment lines of courtview.py matching `club_extras|get_booked_hours` = 0; `grep 'booked-hours\|extras' courtview.html` = nothing; `grep -E '\son[a-z]+=' courtview.html` = nothing.
- `gsd-sdk query validate.health`: status healthy, 0 errors, 0 warnings.

## Deploy

`cv-deploy` output (tail): pip-audit OK (no known vulnerabilities), files copied, `data file modes all 600`, `courtview-backup.timer enabled`, `courtview: systemd active`, `enabled on boot`, `:8766 owned by systemd MainPID 2842241`, `=== CourtView deploy complete ===`. Deploy time 12:29:25 UTC (13:29 BST).

Deployed greps: `systemctl is-active courtview` = active; `grep -c safe_join courtview.py` = 2; `grep -c "No longer available from Padelmates" courtview.html` = 2.

## Response headers (curl -sI on the RPi, cookie auth, token never printed)

`/` (200 text/html):
```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-PkiUUVSLWCN6vvaN30v0AA0TVPR84J/TRjguqMZi8Hs='; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```
`/api/club/?club_id=<Padium>` (200 application/json):
```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
Cache-Control: no-store
```
`/access` (200 text/html):
```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

## Traversal (curl --path-as-is on the RPi)

| URL | with cookie | without cookie |
|---|---|---|
| `/..%2f..%2fetc%2fpasswd.js` | 404 | 403 |
| `/%2e%2e/%2e%2e/x.js` | 404 | 403 |
| `/..\x.js` | 404 | 403 |
| `/a/../../x.js` | 404 | 403 |

The unit matrix in tests/test_security.py also covers `%5c`, `//etc/x.js`, `%252e` and a sentinel file outside the app dir (never served).

## Journal and health after the e2e warm-up

- `journalctl -u courtview --since "2026-10-14 12:29:25 UTC"` count of `club_extras|get_booked_hours|status=401` = 0. Count of `Traceback|[error]` = 0.
- `/api/health` endpoints: `/club/`, `/club/creditpackage/`, `/club/membership/`, `/club/statistics/coach`, `/club/statistics/financial/v2`, `/home/activity/filtered_activities`. Retired paths present: none. `totals` = `{calls: 17, ok: 17, non_200: 0, errors: 0, retries: 0, rejected: 0, unauthorized: 0}`; breaker closed. `jobs` carry `last_run`, `last_ok`, `ok`, `calls`, `errors` (availability last_ok 2026-10-14T11:02:28Z, payments 2026-10-14T12:09:26Z, heatmap 2026-10-13T12:33:45Z).

## Status JSON (for the labs session)

Keys, in this order: `job, last_run, last_ok, ok, calls, errors`. Both files 0644 in a 0755 dir. `last_run` and `last_ok` are ISO 8601 Z; `last_ok` is the start time of the latest ok run, carried unchanged on a failed run, persisted in `job_state.last_ok`, null only if the job never succeeded. First deploy seeded it from `last_run` where `ok=1` and `_rewrite_status_files()` rewrote both files at startup.

```
644 availability.json {"job": "availability", "last_run": "2026-10-14T11:02:28Z", "last_ok": "2026-10-14T11:02:28Z", "ok": true, "calls": 56, "errors": 0}
644 payments.json     {"job": "payments", "last_run": "2026-10-14T12:09:26Z", "last_ok": "2026-10-14T12:09:26Z", "ok": true, "calls": 3, "errors": 0}
```
`access.log` mode 600.

## Playwright (tests/e2e_dashboard.py against https://courtview.labs.doxx:47200)

`RESULT: ALL PASS`. Includes: no CSP violations (console errors = 0), no page errors (0), no /api/booked-hours request (0), Stats shows "No longer available from Padelmates", Compare footnote present with no Activities/Occupancy columns, no spinner left on Club Info/Stats/Compare, `/access` title "CourtView Access Log" with 0 CSP console errors, PII scan over 29 /api bodies: email=0, mobile=0.

## Cache-on-200 grep (Task 1)

Every caller that writes a cache does so only on a 200 or after validating the payload: availability (proxy `_forward` and `_refresh`), club-info (all three targets must be good), membership (`ok` flag), activity-summary (`train_ok` and mix call), revenue-summary, coach-stats, day-activities, heatmap (arrays validated, status 200), payments (`_fetch_payment_page` returns None on non-200). The proxy returns 502 `upstream_auth_required` for a 401 before any scrub or store; tests assert nothing lands in `availability` or `stats_cache`.

## Deviations from Plan

1. **[Rule 1 - Bug/Security] Flask's built-in `/static/<path:filename>` route was unauthenticated.** `Flask(__name__)` registers it by default, bypassing the token gate. Fixed with `static_folder=None`; test_security asserts the endpoint is gone.
2. **[Rule 2 - Missing] 404 handler gates unmatched URLs and `merge_slashes=False`.** Without them `//etc/x.js` was redirected or 404 before auth, so unauthenticated callers could probe the URL space. Now 403 without a token, 404 with one.
3. **[Override F16]** All `{"error": str(exc)}` responses (every site, 500 and 502) now go through `_exc_error`: generic `internal error` / `upstream unavailable`, journal gets `[error] <ExceptionType> path=...`. Covered by `ErrorLeakTests`.
4. **Note:** first start after deploy rotated an existing 2.1 MB `access.log` into `access.log.1` (0600), as designed.
5. TDD order: tests and implementation were written together per task and committed once per task (no separate red commits).
6. The 403 page keeps its original CSP (no `frame-ancestors`); X-Frame-Options DENY covers framing there.

## Auth gates

None. No credentials or auth headers were added anywhere.

## Known Stubs

None. The "No longer available from Padelmates" blocks are intentional (endpoints retired upstream).

## Threat Flags

None beyond the plan's register (T-ifv-01 to 09 mitigated or accepted as planned; deviations 1 and 2 close two extra probe paths).

## Self-Check: PASSED

- Commits 96ea93c, a4fcf56, 402e34f exist on main and origin/main.
- tests/test_security.py exists; SUMMARY not committed (orchestrator).
