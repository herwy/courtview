---
phase: quick
plan: 260522-ul6
subsystem: frontend
tags: [ui-bug, tooltip, payment-history, css, javascript]
dependency_graph:
  requires: []
  provides: [fixed-tooltip-positioning, sliding-payment-window]
  affects: [courtview.html]
tech_stack:
  added: []
  patterns: [event-delegation, getBoundingClientRect, sliding-date-window]
key_files:
  created: []
  modified:
    - courtview.html
decisions:
  - "Used position:fixed with JS event delegation instead of CSS :hover + position:absolute to avoid overflow:auto clipping"
  - "14-day sliding window for payment history rather than month-based fetch; Load More shifts backward not skip-forward"
metrics:
  duration: "~12 minutes"
  completed: "2026-05-22T22:07Z"
---

# Phase quick Plan 260522-ul6: Fix Tooltip Clipping and Payment History Summary

**One-liner:** Tooltip clipping fixed with `position:fixed` + JS `getBoundingClientRect` delegation; payment history now loads last 14 days first with backward-sliding Load More.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Fix tooltip clipping with position:fixed and JS delegation | a343298 | courtview.html |
| 2 | Sliding date window for payment history (newest first) | a343298 | courtview.html |

## Changes Made

**Task 1 - Tooltip (CSS + JS):**
- `.tt` rule: removed `position: absolute`, `bottom: calc(100% + 6px)`, `left: 50%`, `transform: translateX(-50%)`; added `position: fixed`
- Deleted `.cell:hover .tt { display: block; }` CSS rule
- Added IIFE with `document.addEventListener('mouseover'/'mouseout')` event delegation; `requestAnimationFrame` positions tooltip using `getBoundingClientRect`, clamped to viewport edges (8px margin)

**Task 2 - Payment history sliding window (JS):**
- Added `revPayWindowEnd` and `revPayWindowStart` variables (14-day window, reset on each `loadRevenue()` call with `resetPay=true`)
- `loadRevenue()` payment-history fetch now uses `revPayWindowStart.getTime()` / `revPayWindowEnd.getTime()` with `skip=0`
- `renderPayments()` uses `revPaySkip = payments.length` on reset, `+= payments.length` on append
- Load More handler shifts window backward: `revPayWindowEnd = prevStart - 1ms`, `revPayWindowStart = newEnd - 14 days`; fetches with `skip=0`

## Verification

| Check | Result |
|-------|--------|
| `grep -c "position: fixed"` on RPi | 1 (found) |
| `grep -c "revPayWindowEnd"` on RPi | 8 (found) |
| gunicorn process | Running (PID 82809/82821) |
| git push | main -> a343298 |

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None.

## Threat Flags

None - no new network endpoints, auth paths, or schema changes introduced.

## Self-Check: PASSED

- courtview.html modified and deployed
- Commit a343298 exists and pushed to origin
- Both grep verifications returned counts > 0
- Gunicorn confirmed running on RPi
