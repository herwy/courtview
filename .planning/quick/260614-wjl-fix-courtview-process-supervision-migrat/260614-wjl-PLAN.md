---
phase: quick-260614-wjl
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.service
  - deploy.sh
autonomous: true
requirements:
  - fix-process-supervision

must_haves:
  truths:
    - "systemd manages courtview - restarts it automatically on crash"
    - "deploy.sh verifies with systemctl is-active, not pgrep"
    - "no false-positive watchdog pgrep matching dashboard_watchdog.sh"
    - "boot persistence via WantedBy=multi-user.target, not @reboot crontab"
  artifacts:
    - path: "courtview.service"
      provides: "systemd unit file for gunicorn"
      contains: "Restart=always"
    - path: "deploy.sh"
      provides: "updated deploy script using systemctl"
      contains: "systemctl restart courtview"
  key_links:
    - from: "deploy.sh"
      to: "/etc/systemd/system/courtview.service"
      via: "scp + systemctl daemon-reload"
    - from: "courtview.service"
      to: "/usr/local/bin/gunicorn"
      via: "ExecStart"
---

<objective>
Migrate CourtView process supervision from shell watchdog to systemd.

Purpose: The current watchdog is unsupervised - when it exits, gunicorn dies and stays dead. Gunicorn ran unmonitored for ~23h after a crash. systemd provides crash restart, boot persistence, and journal logging without any of those failure modes.

Output: courtview.service (new), deploy.sh (updated), crontab @reboot entries removed on RPi.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@/Users/herwy/dev/courtview/CLAUDE.md
@/Users/herwy/dev/courtview/deploy.sh
@/Users/herwy/dev/courtview/watchdog.sh
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create courtview.service systemd unit</name>
  <files>/Users/herwy/dev/courtview/courtview.service</files>
  <action>
Create /Users/herwy/dev/courtview/courtview.service with this content:

```
[Unit]
Description=CourtView Flask/gunicorn server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/courtview
ExecStart=/usr/local/bin/gunicorn --workers 1 --worker-class gthread --threads 4 --bind 127.0.0.1:8766 --timeout 60 courtview:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=courtview

[Install]
WantedBy=multi-user.target
```

Key decisions:
- Type=simple: gunicorn must NOT use --daemon (daemon mode forks and systemd loses track of the process). The service file removes --daemon from ExecStart.
- --bind 127.0.0.1:8766: matches deploy.sh (the @reboot crontab used 0.0.0.0:8766 but 127.0.0.1 is correct - Caddy proxies in front).
- StandardOutput/StandardError=journal: replaces --error-logfile. Use `journalctl -u courtview` to view logs.
- No --log-level warning: journal captures everything; warnings-only suppressed crash evidence.
- /usr/local/bin/gunicorn: verify this path exists on the RPi before deploying (deploy.sh will check).
  </action>
  <verify>
    <automated>grep -c "Restart=always" /Users/herwy/dev/courtview/courtview.service && grep "Type=simple" /Users/herwy/dev/courtview/courtview.service && grep -v "daemon" /Users/herwy/dev/courtview/courtview.service | grep -c "ExecStart"</automated>
  </verify>
  <done>courtview.service exists with Restart=always, Type=simple, ExecStart without --daemon or --error-logfile.</done>
</task>

<task type="auto">
  <name>Task 2: Update deploy.sh to use systemctl</name>
  <files>/Users/herwy/dev/courtview/deploy.sh</files>
  <action>
Rewrite the remote operations section of deploy.sh. Replace the block between the scp call and the final status log with the following logic:

1. Add courtview.service to the scp call (alongside courtview.py, courtview.html, requirements.txt). Remove watchdog.sh from scp.

2. Replace the entire `remote_out=$(ssh "$RPI_HOST" 'bash -s' << 'REMOTE' ... REMOTE)` block with a new one that:

   a. Verifies gunicorn binary exists at /usr/local/bin/gunicorn; if not found, try `which gunicorn` and abort with a clear message if missing.

   b. Installs the service file:
      ```
      cp /root/projects/courtview/courtview.service /etc/systemd/system/courtview.service
      systemctl daemon-reload
      systemctl enable courtview
      ```

   c. Stops the legacy watchdog and gunicorn (one-time cleanup on first deploy):
      ```
      pkill -f /root/projects/courtview/watchdog.sh 2>/dev/null || true
      pkill -f "gunicorn.*courtview" 2>/dev/null || true
      sleep 2
      ```

   d. Restarts via systemctl:
      ```
      systemctl restart courtview
      sleep 2
      ```

   e. Removes the @reboot crontab entries for courtview (idempotent):
      ```
      crontab -l 2>/dev/null | grep -v "projects/courtview" | crontab -
      ```

   f. Reports status:
      ```
      echo "ACTIVE:$(systemctl is-active courtview)"
      echo "ENABLED:$(systemctl is-enabled courtview)"
      ```

3. Replace the cv_pid/wd_pid parsing block at the end with:
   ```
   cv_active=$(echo "$remote_out" | grep "^ACTIVE:" | sed 's/^ACTIVE://')
   cv_enabled=$(echo "$remote_out" | grep "^ENABLED:" | sed 's/^ENABLED://')

   [[ "$cv_active" == "active" ]] && ok "courtview: systemd active" || { err "courtview systemd unit not active (got: $cv_active)"; exit 1; }
   [[ "$cv_enabled" == "enabled" ]] && ok "courtview: enabled on boot" || err "courtview not enabled (got: $cv_enabled)"
   ```

4. Remove the gunicorn install block (pip3 install gunicorn) - gunicorn must already be present; if not, the binary check in step (a) will catch it.

5. Remove the `chmod +x watchdog.sh` line.

The pip-audit block (step 1b in the existing script) is unchanged. The mkdir -p at the top is unchanged.
  </action>
  <verify>
    <automated>grep -c "systemctl restart courtview" /Users/herwy/dev/courtview/deploy.sh && grep -c "systemctl is-active" /Users/herwy/dev/courtview/deploy.sh && grep -v "^#" /Users/herwy/dev/courtview/deploy.sh | grep -c "pgrep.*watchdog"</automated>
  </verify>
  <done>
  deploy.sh contains `systemctl restart courtview` and `systemctl is-active`, and the pgrep watchdog check count is 0.
  The verify command returns: two lines of "1" followed by "0".
  </done>
</task>

<task type="auto">
  <name>Task 3: Deploy, migrate, and verify on RPi</name>
  <files></files>
  <action>
Run cv-deploy to push the new files and execute the migration. Then verify each requirement on the RPi:

1. Run: `cv-deploy`

2. Verify systemd unit is active:
   ```
   rpi 'systemctl is-active courtview'
   ```
   Expected: `active`

3. Verify enabled on boot:
   ```
   rpi 'systemctl is-enabled courtview'
   ```
   Expected: `enabled`

4. Verify @reboot entries are gone:
   ```
   rpi 'crontab -l | grep courtview'
   ```
   Expected: no output (exit 1 from grep is acceptable - means no match)

5. Verify watchdog.sh is NOT running:
   ```
   rpi 'pgrep -fa watchdog.sh'
   ```
   Expected: only dashboard_watchdog.sh if anything, NOT /root/projects/courtview/watchdog.sh

6. Verify gunicorn is listening on 127.0.0.1:8766:
   ```
   rpi 'ss -tlnp | grep 8766'
   ```
   Expected: line showing LISTEN on 127.0.0.1:8766

7. Verify service file deployed correctly:
   ```
   rpi 'grep -c "Restart=always" /etc/systemd/system/courtview.service'
   ```
   Expected: 1

All seven checks must pass before reporting done. If cv-deploy reports "courtview systemd unit not active", read `rpi 'journalctl -u courtview -n 30'` to diagnose.
  </action>
  <verify>
    <automated>
rpi 'systemctl is-active courtview && systemctl is-enabled courtview && echo "CRONTAB_CLEAN:$(crontab -l 2>/dev/null | grep -c courtview)" && ss -tlnp | grep -c 8766' # verified: CLAUDE.md - deploy + RPi layout
    </automated>
  </verify>
  <done>
  systemctl is-active returns "active", is-enabled returns "enabled", crontab grep count is 0, ss shows 8766 listening. courtview.service exists at /etc/systemd/system/ with Restart=always.
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| deploy.sh -> RPi | SSH exec of systemctl commands as root |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-wjl-01 | Tampering | /etc/systemd/system/courtview.service | accept | File deployed by deploy.sh over authenticated SSH; root-only writable |
| T-wjl-02 | Denial of Service | systemctl restart courtview in deploy.sh | accept | Deploy is already authenticated SSH; same risk as previous pkill approach |
| T-wjl-SC | Tampering | npm/pip/cargo installs | accept | No new package installs in this task |
</threat_model>

<verification>
After deploy:
- `systemctl is-active courtview` returns "active"
- `systemctl is-enabled courtview` returns "enabled"
- `crontab -l | grep courtview` returns nothing
- `ss -tlnp | grep 8766` shows LISTEN on 127.0.0.1:8766
- `journalctl -u courtview -n 5` shows recent gunicorn startup lines
- `pgrep -fa watchdog.sh` does NOT match /root/projects/courtview/watchdog.sh
</verification>

<success_criteria>
CourtView is supervised by systemd with Restart=always. A process crash will be detected and restarted within 5 seconds. Boot persistence is handled by WantedBy=multi-user.target. deploy.sh verifies with systemctl is-active and contains no pgrep watchdog false-positive check. The @reboot crontab entries are removed.
</success_criteria>

<output>
Create `.planning/quick/260614-wjl-fix-courtview-process-supervision-migrat/260614-wjl-SUMMARY.md` when done.
</output>
