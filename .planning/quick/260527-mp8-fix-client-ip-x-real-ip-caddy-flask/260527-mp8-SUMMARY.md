---
phase: quick
plan: 260527-mp8
subsystem: courtview
tags: [proxy, caddy, flask, access-log, client-ip]
dependency_graph:
  requires: []
  provides: [real-client-ip-in-access-log]
  affects: [courtview.py, /etc/caddy/Caddyfile]
tech_stack:
  added: []
  patterns: [X-Real-IP header forwarding via Caddy header_up]
key_files:
  created: []
  modified:
    - courtview.py
    - /etc/caddy/Caddyfile (RPi only, no local copy)
decisions:
  - Used X-Real-IP (not X-Forwarded-For) because Caddy's {remote_host} is a single value and X-Real-IP is the conventional single-IP header for reverse proxy scenarios
metrics:
  duration: ~5 minutes
  completed: 2026-05-27T19:15:00Z
  tasks_completed: 2
  tasks_total: 2
---

# Phase quick Plan 260527-mp8: Fix Client IP X-Real-IP Caddy Flask Summary

**One-liner:** Read X-Real-IP header in Flask _get_client_ip() and inject it via Caddy header_up in both vhosts, fixing /access page showing 127.0.0.1 for all external requests.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Update courtview.py _get_client_ip to read X-Real-IP | 5437239 | courtview.py |
| 2 | Update Caddyfile to forward X-Real-IP on both vhosts, reload Caddy | (RPi direct write) | /etc/caddy/Caddyfile |

## Changes Made

**courtview.py** (`_get_client_ip`, line 1000):
- Updated docstring: "no proxy in front of this server" -> "real client IP forwarded by Caddy via X-Real-IP, with remote_addr fallback"
- Return value: `request.remote_addr or "0.0.0.0"` -> `request.headers.get("X-Real-IP") or request.remote_addr or "0.0.0.0"`

**/etc/caddy/Caddyfile** (RPi):
- Both vhosts (`dashboard.labs.doxx:47200` and `courtview.labs.doxx:47200`) now have a `header_up X-Real-IP {remote_host}` directive inside the `reverse_proxy` block
- Caddy reloaded via `systemctl reload caddy`

## Verification Results

| Check | Expected | Result |
|-------|----------|--------|
| `grep -c "X-Real-IP" courtview.py` | >= 1 | 2 (docstring + return) |
| `grep -c "X-Real-IP" /etc/caddy/Caddyfile` | 2 | 2 |
| `systemctl is-active caddy` | active | active |
| `pgrep -fa "gunicorn.*courtview"` | running process | confirmed |

## Deviations from Plan

None - plan executed exactly as written.

## Threat Surface Scan

No new network endpoints, auth paths, or schema changes introduced. The `header_up X-Real-IP {remote_host}` directive overwrites any client-supplied X-Real-IP value, so clients cannot forge the header through the proxy (T-mp8-01 from plan threat register, accepted).

## Self-Check: PASSED

- courtview.py committed at 5437239 (verified with git log)
- X-Real-IP present in deployed courtview.py on RPi (grep count 2)
- Caddyfile updated and confirmed via cat on RPi (grep count 2)
- Caddy active and reloaded without error
