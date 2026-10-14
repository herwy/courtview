---
phase: quick-261014-it2
plan: 01
subsystem: operations
tags: [systemd-sandbox, non-root, deploy, rollback, telegram, migration]
requires: []
provides:
  - courtview runs as user courtview, sandboxed, data in /var/lib/courtview
  - release-dir deploy with HTTP health check and automatic rollback
  - vendored Telegram sender and timer-armed recovery check
key-files:
  created: [cv_telegram.py, cv_closure.py, requirements.in, web/access.html, web/access.js, web/dashboard.css, courtview-recovery@.service, courtview-recovery@.timer, tests/test_cv_telegram.py, tests/test_paths.py]
  modified: [courtview.py, cv_notify_fail.py, cv_backup.py, courtview.service, courtview-backup.service, notify-telegram@.service, deploy.sh, requirements.txt, scripts/courtview-backup-pull.sh, scripts/courtview-restore.sh, CLAUDE.md, .claude/docs/courtview-internals.md, tests/*]
decisions:
  - MemoryDenyWriteExecute kept (probe and live service pass); no sandbox property dropped
  - gunicorn gets --no-control-socket (v26 control socket needs a writable home)
  - old data deletion split out of deploy into --finalize-migration, run only after all checks
metrics:
  completed: 2026-10-14
  tasks: 3
---

# Phase quick-261014-it2: Phase 4 operations Summary

CourtView now runs as the sandboxed `courtview` user from `/opt/courtview/releases/<ts>` with data in `/var/lib/courtview`, credentials via LoadCredential, no `/root/labs` dependency, and a deploy that health-checks over HTTP and rolls back by itself.

## Results (literal)

- **Suites:** 9 Python modules on the RPi, 194 tests, OK (before and after migration). Pager `node --test`: 11/11 pass. Playwright e2e: RESULT ALL PASS, 0 CSP errors, access title "CourtView Access Log" (run twice, second after finalize).
- **systemd-analyze security courtview:** 9.6 UNSAFE before, 1.5 OK after. Backup unit: 1.2 OK.
- **MainPID user:** courtview (checked by deploy on every release).
- **Migration:** integrity pre=ok new=ok; 15 tables, 72760 rows, every table count identical (payments 70223, payments_sync 180, archive_heatmap 1792, ...). Data files `600 courtview`. Safety copy pre.db and old data deleted by `--finalize-migration` only after all checks.
- **Credentials and token drill:** cookie /api/health 200, no cookie 403; /access 200 (title ok), /access.js 200, /dashboard.css 200. Rotation (tmp file + mv + restart): old token 403, new 200. Original token then restored the same way, so the user's cookie still works.
- **HMAC continuity:** match 3/3, and all 3 baseline rows were re-synced after the migration (synced_at newer, payments job_state last_run 13:09Z).
- **Status JSON via old path:** `/root/projects/courtview/status` -> `/var/lib/courtview/status`; payments last_ok 2026-10-14T13:09:26Z, availability 11:02:28Z, both ok. labs_heartbeat thresholds (7 h, 75 min) met; zero labs change.
- **Recovery drill (throwaway cvdrill):** exactly 1 red ("red loop alert sent", loud) and 1 green (silent, "green alert sent"); `courtview-recovery@cvdrill.timer` disarmed itself on the next check; unit, timer and state entry removed.
- **Backup:** `systemctl start courtview-backup.service` ran as User=courtview, result success, `courtview-20261014.db.gpg` 0600 courtview. Pull: `ok host=pi-cmd copied=1` after deleting the local copy. Restore verify: integrity ok.
- **Rollback drill:** broken courtview.py (SystemExit unless SKIP_STARTUP). Attempt 1 (unconditional SystemExit) rolled back at the PII purge check; attempt 2 passed audit, failed health ("TimeoutError"), printed `ROLLED BACK to 20261014T125214Z`, exit 1, failed releases renamed `.failed`, service restored; clean cv-deploy afterwards showed prev=20261014T125214Z.
- **/root/labs grep:** 0 in repo code, units, deploy.sh, scripts; 0 on the RPi in /opt/courtview/current and the units.
- **MemoryDenyWriteExecute:** kept. Pre-flight systemd-run probe with the full property set (imports, curl_cffi chrome110 GET status 200, gunicorn --check-config, gpg --recipient-file) passed, and the live service runs with it.
- **Deploy output:** first deploy (release 20261014T124852Z) ran the migration: OK lines for staging, pip-audit (pins match, no vulns), modes 600 courtview, both timers enabled, health OK, MainPID 2844488 user courtview. Later deploys prev-linked, all OK. validate.health: healthy, 0 errors, 0 warnings.

## Deviations from Plan

1. **[Rule 1] gunicorn control socket:** gunicorn 26 logged `Control server error: [Errno 30] ... /nonexistent` as the non-root user. Added `--no-control-socket` to ExecStart (commit c38059c).
2. **[Rule 2] Delete after checks:** the plan deleted pre.db and old data inside deploy once health passed; the task constraints require every post-migration check first. Added `deploy.sh --finalize-migration`, run after all drills.
3. **Backup units saved for abort:** the abort path also saves and restores courtview-backup.service/.timer (plan listed only two units).
4. **Alert logging:** cv_notify_fail now logs `[cv_notify_fail] <kind> alert sent|queued` so drill sends can be counted from the journal.
5. **Access page "doxxnet":** the plan test says the page has no "doxxnet"; web/access.html keeps a functional "doxxnet" origin filter button (ISP filter, not branding). The test asserts no Wireslammer and no "doxxnet" in the title.
6. **Rollback drill location:** the hook blocks direct `deploy.sh` runs, so the drill ran through `cv-deploy` with a temporary broken courtview.py in the working tree (reverted with `git checkout -- courtview.py`) instead of a temp copy.
7. **Notifier state (plan L5):** root-only `/var/lib/courtview-notify` outside the sandbox-owned data dir, as decided in the plan.
8. **Skipped by instruction:** STATE.md Quick Tasks row and STATE/SUMMARY commits (orchestrator).

## Side effects

The drills sent real Telegram messages (recovery drill: 1 red, 1 silent green). The rollback drill may have produced a crash-loop alert; the state file was empty afterwards.

## Commits

- e6ae008 feat: data dir and credentials, own web assets, vendored sender, timer arming
- c38059c feat: sandboxed user, releases, health, rollback, pins
- 2306bba feat: alert outcome logging and docs (main == origin/main)

## Known Stubs

None.

## Self-Check: PASSED

Commits e6ae008, c38059c, 2306bba exist and are pushed; service active as courtview on release 20261014T125830Z.
