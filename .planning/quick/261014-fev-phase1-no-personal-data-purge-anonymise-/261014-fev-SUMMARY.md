---
phase: quick-261014-fev
plan: 01
subsystem: privacy / data protection
tags: [pii, hmac, purge, backup, gdpr]
requirements: [PII-STOP, PII-KEY, PII-FEAT, PII-ALLOW, PII-TEST, PII-PURGE, PII-LOGS, PII-FILES, PII-BACKUP, PII-DOCS]
key-files:
  created: [cv_purge_pii.py, cv_backup.py, courtview-backup.service, courtview-backup.timer, courtview-backup.pub.asc, scripts/courtview-backup-pull.sh, scripts/courtview-backup-verify.sh, launchd/net.doxx.courtview-backup-pull.plist, tests/test_pii.py, tests/test_purge.py]
  modified: [courtview.py, courtview.html, courtview.service, deploy.sh, tests/test_payments.py, tests/e2e_dashboard.py, CLAUDE.md, .claude/docs/courtview-internals.md, .claude/docs/padelmates-disclosure-2026-05-21.md, .claude/docs/INDEX.md]
commits: [f69c1f5, 3496c52, aadba16]
completed: 2026-10-14
---

# Quick 261014-fev: Phase 1 no personal data - Summary

CourtView no longer stores or shows names, emails, phones, ids or receipts; repeat customers are an HMAC `customer_key`, the live DB was purged with secure deletion, and a gpg-encrypted nightly backup is pulled to the Mac.

## Commits (main == origin/main at aadba16)
- f69c1f5 Task 1: stop collecting, anonymous features, PII tests
- 3496c52 Task 2: purge script, deploy gate, UMask, e2e
- aadba16 Task 3: backup, docs, disclosure status

## Test results (RPi temp copy, Python 3.13)
- test_pii + test_payments + test_auth + test_purge: 52 tests, OK. Node pager tests: 11/11 pass.
- AST compliance: 15 cffi calls, 0 non-compliant.
- Playwright e2e (deployed): RESULT: ALL PASS; 53 /api JSON bodies scanned, email 0, mobile 0.
- tests/test_cv_notify_fail.py not run in the temp copy (needs cv_notify_fail.py, untouched by this plan).

## Decisions and findings
- Upstream payment record keys: amount, createdAt, description, player_email, player_name, player_phone, receiptUrl, source_type, status, transaction_id. No stable player id, so the normalised email is the customer_key basis.
- Membership count source: plan listing field `total_members` (present on Padium and Rocket plans). The `/club/membership/member` fan-out was removed entirely.
- Description scan (60,828 rows): 406 contained the row's own name/email/phone, 205 changed by cleaning, 0 left a PII pattern. 10,562 distinct cleaned strings, 6,494 used by < 5 customers, so rule chosen: generalise. Templates (digits, month names, tz abstracted) shared by >= 5 customers: 31, covering 60,740 rows; 88 rows fall back to `source_type`.
- Orchestrator override applied: `/api/coach-stats` returns "Coach 1..n" (sessions desc) with pseudonymous coach_id and numeric stats only; `/api/coach-bios` and its UI fetch removed; activity-summary `coach_rates` keyed by the same pseudonym; coach names never reach stats_cache. Tests assert no fixture coach name in any response or the DB.

## Purge
- Dry-run (`--check`) would change: payments rebuild 60,834; archive payment-history 16, other financial 16, club-info 16; availability 388 rows (75 contain PII patterns); club_info_cache 3, membership cache 3, stats_cache 9; synthetic ids 0.
- Actual (run by deploy.sh with the service stopped via systemctl): payments_rows_rebuilt=60834, rows_null_customer_key=0, synthetic_ids_rewritten=0, rows_dropped_on_collision=0, archive_payloads_rewritten=32, club_info_archives_scrubbed=16, archive_rows_deleted=0, availability_rows_scrubbed=388, availability_rows_deleted=0, cache_rows_deleted=15. `--check` afterwards: clean (exit 1).
- PII-pattern columns excluding the allowlisted court_name and sqlite_sequence.name: 0.
- DB byte scan (59,998,208 bytes): email 0, UK mobile 0, landline 2. WAL: 0 bytes, 0 matches. The 2 landline hits are in `availability` pages, preceded by overflow-page binary, 10 digits; 0 landline matches in 61,270 decoded rows (availability, archives, payments), so they are epoch digits split across an overflow page boundary (numeric artefacts).
- Other DB copies: RPi 0, Mac 0 (nothing deleted).
- journald (`-u courtview`, 4,904 lines since 2026-10-12): email 0, mobile 0. access.log (9,341 lines): email 0, mobile 0. No action needed.
- certs: 0 references in /root/projects/courtview, /etc/systemd/system, /etc/caddy, crontab; directory (2 files) removed and confirmed gone; service still active.
- File modes: db 600, access.log 600, -wal/-shm 600 when present, /root/.courtview_hmac_key 600; `systemctl show courtview -p UMask` = 0077.

## Sample anonymous leaderboard (October to date, two calls identical for both clubs)
- Padium: F2CD 20 bookings £1200.00; 3691 22 £880.00; C50C 25 £798.25; B041 18 £786.75; 351E 6 £770.00.
- Rocket: 16BB 18 £565.50; 35AA 8 £448.00; 9AA7 20 £442.75; 0A27 10 £424.70; 7BFD 6 £405.25.
- payment-history rows: keys {amount, createdAt, description, id, status}.

## Backup
- Timer `courtview-backup.timer` enabled, next run 2026-10-15 03:34 BST. Manual run: courtview-20261014.db.gpg, 7,730,878 bytes, mode 600, dir 700, kept 1, 4.6 s CPU.
- Mac pull via launchd kickstart: copied 1, kept 1, file mode 600, log line `ok host=pi-cmd copied=1 kept=1 age_h=0`.
- Restore verify: integrity_check ok, PII columns 0, email 0, mobile 0, payments rows 60,834.
- gpg fingerprint 0C6DF971E0D5A34FC9784FA271B04EBF6A997550 (ed25519 + cv25519, no passphrase, no expiry). Private key stays in the Mac keyring only.

## Deploy output and greps
Deploy (`cv-deploy`) OK: pip-audit clean, HMACKEY present, PURGE counts above, data file modes all 600, backup timer enabled, systemd active and enabled, :8766 owned by MainPID. Greps on RPi: `_customer_key` in courtview.py 2, `Customer #` in courtview.html 1, `revPaySearch` 0, `UMask=0077` in unit 1, courtview active.

## Deviations
- [Rule 2] Orchestrator coach anonymisation (above), including pseudonymised `coach_rates` keys.
- [Rule 2] `/api/month`, availability proxy reads, club-info cache and archive club-info/financial are scrubbed on read as well as at write (defence in depth, needed so legacy rows can never leak).
- [Rule 1] Description template normalisation also abstracts month names and BST/GMT after the first scan showed per-month templates would drop descriptions every winter.
- Plan verify grep for `player_phone` in courtview.py returns 0 by reading upstream fields through `_player_field`.
- Dry-run counted availability changes by parsed-equality (75), not by re-serialised text (388).
- STATE.md and SUMMARY commit left to the orchestrator as instructed (plan STATE row step skipped).

## Open items
- Disclosure to Padelmates: not sent as of 2026-10-14 (status line added to the disclosure doc).
- Losing the Mac gpg keyring makes every backup unreadable; consider an offline export of the secret key (not done).
- Journal hits: none, nothing to vacuum.
- Scrubbing is key-based; a personal name under an unknown upstream key would not be caught (documented; add a test for any new shape).
- The backup timer has no OnFailure alert; staleness shows only in `~/Backups/courtview/pull.log` (warn after 36 h).
- `validate.health`: status healthy, 0 errors, 0 warnings.

## Self-Check: PASSED
Commits f69c1f5, 3496c52, aadba16 exist and are pushed; created files verified present; live checks above.
