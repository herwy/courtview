---
phase: quick-260614-wjl
plan: 01
subsystem: process-supervision
tags: [systemd, deploy, supervision, gunicorn]
dependency_graph:
  requires: []
  provides: [courtview-systemd-service]
  affects: [deploy.sh, courtview.service]
tech_stack:
  added: [systemd unit (courtview.service)]
  patterns: [Type=simple + Restart=always, journal logging, WantedBy=multi-user.target]
key_files:
  created: [courtview.service]
  modified: [deploy.sh]
decisions:
  - "Type=simple (not forking): gunicorn must NOT use --daemon so systemd tracks the process"
  - "Removed --error-logfile from ExecStart: journal captures stdout/stderr via SyslogIdentifier=courtview"
  - "Binary check at deploy time: if /usr/local/bin/gunicorn missing, abort with clear message"
  - "Crontab cleanup idempotent: grep -v projects/courtview | crontab - removes @reboot entries safely"
metrics:
  duration: "2m 27s"
  completed: "2026-06-14"
  tasks: 3
  files: 2
---

# Phase quick-260614-wjl Plan 01: Fix CourtView Process Supervision Summary

Migrated CourtView process supervision from unsupervised shell watchdog to systemd with Restart=always and boot persistence via WantedBy=multi-user.target.

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Create courtview.service systemd unit | 0be4e1b |
| 2 | Update deploy.sh to use systemctl | 7607ef7 |
| 3 | Deploy, migrate, and verify on RPi | (deploy task, no code commit) |

## Verification Results (all RPi checks passed)

- `systemctl is-active courtview` - active
- `systemctl is-enabled courtview` - enabled
- `crontab -l | grep courtview` - no output (clean, @reboot entries removed)
- `pgrep -fa watchdog.sh` - only `dashboard_watchdog.sh`, NOT `/root/projects/courtview/watchdog.sh`
- `ss -tlnp | grep 8766` - LISTEN on 127.0.0.1:8766 (gunicorn worker pid 3828981)
- `grep -c "Restart=always" /etc/systemd/system/courtview.service` - 1
- `journalctl -u courtview -n 5` - gunicorn 26.0.0 startup lines present

## Deviations from Plan

None - plan executed exactly as written.

Note: `cv-deploy` was not used directly because it runs `~/dev/courtview/deploy.sh` (main repo). The worktree's updated deploy.sh was invoked directly: `zsh /Users/herwy/dev/courtview/.claude/worktrees/agent-abac5f3f7d7addd79/deploy.sh --rpi pi-cmd`. This is expected behaviour for worktree execution.

## Known Stubs

None.

## Threat Flags

None - no new network endpoints, auth paths, or trust boundaries introduced. systemd unit runs as root (same as prior gunicorn process). Service file deployed via authenticated SSH.

## Self-Check: PASSED

- courtview.service: created and committed (0be4e1b)
- deploy.sh: modified and committed (7607ef7)
- RPi: `systemctl is-active courtview` returns "active"
- RPi: `systemctl is-enabled courtview` returns "enabled"
- RPi: crontab courtview entries: 0
- RPi: `Restart=always` in /etc/systemd/system/courtview.service: 1
