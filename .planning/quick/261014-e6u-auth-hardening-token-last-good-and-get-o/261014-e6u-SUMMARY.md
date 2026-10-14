---
phase: quick-261014-e6u
plan: 01
subsystem: auth
tags: [auth, token, cors, flask]
key-files:
  modified: [courtview.py, CLAUDE.md, .claude/docs/courtview-internals.md, tests/e2e_dashboard.py]
  created: [tests/test_auth.py]
requirements-completed: [AUTH-LASTGOOD, AUTH-GETONLY, AUTH-NOCORS, DOCS-URL]
completed: 2026-10-14
---

# Quick 261014-e6u: Auth hardening Summary

Token reader keyed on (st_mtime_ns, st_size) that keeps the last good token across empty/unreadable files, plus a GET/HEAD-only app with no CORS headers and no upstream POST.

## Commits
- 79b6497 test: failing auth tests (RED)
- cb59ed5 feat: reader, method guard, proxy GET-only, `_provided_token`, dead `_check_token` removed
- 02acc51 docs: Caddy URL, rotation procedure, internals, e2e base URL

## Verification
- tests.test_auth + tests.test_payments: 36 tests OK (16 auth + 20 payments), run on RPi (local Python is 3.9, code needs 3.10+); pager: 11 pass, 0 fail.
- Compliance scan: 17 cffi_requests calls, 17/17 with APP_HEADERS + chrome110; `.post(` count 0; Access-Control count 0 (local and RPi); `_check_token` 0; `"POST"` 0; `st_mtime_ns` 2.
- Deploy ok (pip-audit clean, systemd active, enabled, :8766 owned by MainPID). RPi grep: `_allow_only_get_head` 1, `Access-Control` 0, `active`.
- Loopback: POST 403, PUT 403, OPTIONS (evil Origin) 403, GET 200; Access-Control headers 0 on `/`, `/api/club/`, `/access`.
- Via Caddy (courtview.labs.doxx:47200): all three 200 with 0 Access-Control headers; POST 403, OPTIONS 403.
- Rotation drill: 77 requests, 77 x 200, 0 x 403; 20 sequential 200s. Journal: `loaded`, `reload failed (empty), keeping last good token`, `loaded`. Token sha256 and mode/owner (600 root:root) match the original; backup, tmp, header file removed.
- Playwright e2e: ALL PASS.

## Deviations
None to the plan. Tests ran on an RPi temp copy because local Python is 3.9. The first `[token] loaded` line appears on first request after restart, not at startup.

## Self-Check: PASSED
