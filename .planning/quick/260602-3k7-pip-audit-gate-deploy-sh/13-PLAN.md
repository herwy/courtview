---
phase: quick
plan: 13
type: execute
wave: 1
depends_on: []
files_modified:
  - requirements.txt
  - deploy.sh
autonomous: true
requirements:
  - security-gate-pip-audit
must_haves:
  truths:
    - "requirements.txt exists at repo root with curl-cffi, flask, gunicorn"
    - "deploy.sh scp step includes requirements.txt"
    - "deploy.sh runs pip-audit on the remote requirements.txt before scp, blocking on vulnerabilities"
  artifacts:
    - path: "requirements.txt"
      provides: "Pinned package list for pip-audit scanning"
      contains: "curl-cffi"
    - path: "deploy.sh"
      provides: "Updated deploy with pip-audit gate between step 1 and step 2"
      contains: "pip-audit"
  key_links:
    - from: "deploy.sh step 1 (mkdir)"
      to: "deploy.sh step 1b (pip-audit gate)"
      via: "sequential shell execution"
    - from: "deploy.sh step 1b (pip-audit gate)"
      to: "deploy.sh step 2 (scp)"
      via: "gate must pass before transfer"
---

<objective>
Add a pip-audit security gate to courtview's deploy.sh and create requirements.txt from actual project imports.

Purpose: Block deploys when known CVEs exist in the three non-stdlib dependencies (curl-cffi, flask, gunicorn).
Output: requirements.txt at repo root, scp step updated to transfer it, pip-audit gate inserted between mkdir and scp.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@/Users/herwy/dev/courtview/CLAUDE.md
@/Users/herwy/dev/courtview/.planning/STATE.md
@/Users/herwy/dev/courtview/deploy.sh
</context>

<tasks>

<task type="auto">
  <name>Task 1: Create requirements.txt</name>
  <files>requirements.txt</files>
  <action>Create `/Users/herwy/dev/courtview/requirements.txt` with three lines, one package per line, no version pins (pip-audit resolves installed versions remotely):
    curl-cffi
    flask
    gunicorn
  These are the only non-stdlib imports used in courtview.py per the context notes.</action>
  <verify>
    <automated>grep -c "flask" /Users/herwy/dev/courtview/requirements.txt</automated>
  </verify>
  <done>File exists with exactly 3 lines: curl-cffi, flask, gunicorn.</done>
</task>

<task type="auto">
  <name>Task 2: Update deploy.sh - add pip-audit gate and requirements.txt to scp</name>
  <files>deploy.sh</files>
  <action>Make two edits to `/Users/herwy/dev/courtview/deploy.sh`:

  **Edit A - scp step:** Add `requirements.txt` to the scp file list. The current scp block (lines 33-39) lists `courtview.py`, `courtview.html`, `watchdog.sh`. Insert `"$SCRIPT_DIR/requirements.txt"` as a new line after `watchdog.sh` and before the remote destination `"${RPI_HOST}:/root/projects/courtview/"`. Update the ok log to include `requirements.txt`.

  **Edit B - pip-audit gate:** Insert a new step 1b block between the existing `# 1. Ensure remote dir` block and the `# 2. Copy all files` block. Model it exactly on the doxxnet-labs reference implementation from context, adapting only the path from `/root/labs/requirements.txt` to `/root/projects/courtview/requirements.txt`.

  The gate block:
  - Label it `# 1b. pip-audit security gate`
  - Runs via `ssh "$RPI_HOST" 'bash -s' << 'REMOTE'` heredoc
  - SKIP (exit 2) if requirements.txt not found (first deploy - file not yet on RPi)
  - Installs pip-audit quietly with `--break-system-packages`
  - SKIP (exit 2) if import fails after install
  - Runs `python3 -m pip_audit -r /root/projects/courtview/requirements.txt 2>&1`
  - Exit code 0: call `ok "pip-audit: no known vulnerabilities found"`
  - Exit code 1: call `err "pip-audit: vulnerabilities found - blocking deploy"`, echo output, `exit 1`
  - Any other non-zero: log skip warning with indented output, continue

  The gate must execute AFTER the mkdir (step 1) and BEFORE the scp (step 2).</action>
  <verify>
    <automated>grep -c "pip.audit" /Users/herwy/dev/courtview/deploy.sh</automated>
  </verify>
  <done>deploy.sh contains pip-audit gate referencing /root/projects/courtview/requirements.txt, and scp block includes requirements.txt.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| RPi pip install | pip-audit installed from PyPI on RPi - potential supply chain risk |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-3k7-01 | Tampering | pip-audit install on RPi | mitigate | gate only runs install if not already present; import check guards before scan |
| T-3k7-SC | Tampering | npm/pip/cargo installs | accept | requirements.txt contains 3 known packages (curl-cffi, flask, gunicorn) verified from APK reverse-engineering context |
</threat_model>

<verification>
After both tasks complete, run deploy manually and confirm:
- pip-audit gate appears in deploy output between mkdir and scp
- No blocking on a clean install
- `grep -c "pip.audit" /root/projects/courtview/../deploy.sh` on RPi side not needed - local file is the source
</verification>

<success_criteria>
- requirements.txt exists with curl-cffi, flask, gunicorn
- deploy.sh scp block transfers requirements.txt to RPi
- deploy.sh pip-audit gate runs between mkdir and scp, blocks on exit code 1, skips gracefully on exit code 2
</success_criteria>

<output>
Create `.planning/quick/260602-3k7-pip-audit-gate-deploy-sh/13-SUMMARY.md` when done.
</output>
