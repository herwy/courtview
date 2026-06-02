---
phase: quick
plan: 13
subsystem: deploy
tags: [security, pip-audit, deploy, requirements]
key-files:
  created:
    - requirements.txt
  modified:
    - deploy.sh
decisions:
  - No version pins in requirements.txt (pip-audit resolves installed versions on the RPi)
  - Gate uses exit 2 for skip conditions so deploy proceeds on first deploy or tool unavailability
  - Gate positioned after mkdir (step 1) and before scp (step 2) so the local requirements.txt
    on the RPi is checked before new files overwrite it
metrics:
  duration: "3m"
  completed: "2026-06-02"
  tasks: 2
  files: 2
---

# Phase quick Plan 13: pip-audit gate in deploy.sh Summary

Added pip-audit CVE scanning gate to deploy.sh with requirements.txt covering the three non-stdlib dependencies (curl-cffi, flask, gunicorn).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create requirements.txt | 60a9e5c | requirements.txt |
| 2 | Update deploy.sh - pip-audit gate + scp inclusion | c821fc2 | deploy.sh |

## What Was Built

- `requirements.txt` at repo root with three unpinned packages: `curl-cffi`, `flask`, `gunicorn`
- `deploy.sh` updated with:
  - Step 1b pip-audit gate: installs pip-audit quietly on RPi, scans requirements.txt, blocks on exit 1 (known CVEs), skips gracefully on exit 2 (first deploy or tool unavailable)
  - scp block now includes `requirements.txt` so subsequent deploys keep the remote copy current

## Gate Behaviour

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | No vulnerabilities | ok log, deploy continues |
| 1 | Vulnerabilities found | err log + output, deploy exits 1 (blocked) |
| 2 | requirements.txt missing (first deploy) or pip-audit unavailable | skip log, deploy continues |

## Deviations from Plan

None - plan executed exactly as written.

## Self-Check: PASSED

- requirements.txt: `grep -c "flask" /Users/herwy/dev/courtview/requirements.txt` = 1
- deploy.sh pip-audit count: `grep -c "pip.audit" /Users/herwy/dev/courtview/deploy.sh` = 9
- Commits 60a9e5c and c821fc2 verified in git log
