---
status: complete
commit: a2df05f
---

# Quick Task 261014-0yr: CourtView supervision hardening - Summary

**Root cause:** on 2026-10-13 a doxxnet-labs session used a stale memory recipe. It ran `pkill` on the systemd gunicorn and then started `gunicorn --daemon` on 127.0.0.1:8766. courtview.service crash-looped with "Address already in use" from 18:44 to 19:27 BST (245 restarts, 122 notifier runs). That session has since fixed its memory rule.

**Changes (a2df05f), deployed and verified on the RPi:**
- `courtview.service`: `StartLimitIntervalSec=0`, `RestartSteps=5`, `RestartMaxDelaySec=300`. systemd shows `RestartMaxDelayUSec=5min`; `systemd-analyze verify` is clean.
- `cv_notify_fail.py`: crash-loop alerting ported from doxxnet-labs `notify_unit_failure.py`. Stub tests passed for single crash (no alert), 3rd crash (one loop alert), cooldown (silent), after 1h (re-arms), stays failed (FAILED alert), bad unit (rc 2), state file 0600. A live run on the RPi with a dummy unit confirmed the imports and the 0600 state write.
- `deploy.sh`: removed the legacy `pkill` cleanup. The deploy now fails unless MainPID is among the :8766 listener PIDs (gunicorn master and worker both hold the socket).
- `courtview.py`: accurate startup line.

**Docs:** CLAUDE.md now has the real cache table names, drops the stale `courtview.log` row, documents the notifier and adds a never-pkill restart rule.

**Live test (2026-10-14 00:46-00:47 BST):** a throwaway `cv-loop-test.service` (`ExecStart=/bin/false`, same OnFailure and backoff settings) crash-looped. Notifier runs 1 and 2 logged "recovered, no alert"; run 3 sent one Telegram CRASH-LOOPING alert. Crashes that land while the notifier is still running are merged into that run, so with backoff the alert arrives after about 1 minute. Unit and state file removed afterwards.
