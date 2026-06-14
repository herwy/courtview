#!/bin/zsh
# deploy.sh - sync CourtView to RPi and restart the server.
#
# Usage: zsh deploy.sh [--rpi HOST]
set -o pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: deploy.sh must be run on macOS, not on the RPi" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RPI_HOST="pi-cmd"

while (( $# > 0 )); do
    case "$1" in
        --rpi) RPI_HOST="$2"; shift 2 ;;
        *)     shift ;;
    esac
done

ts()  { date '+%Y-%m-%dT%H:%M:%S'; }
log() { echo "[$(ts)] $*"; }
ok()  { echo "[$(ts)] OK  $*"; }
err() { echo "[$(ts)] ERR $*" >&2; }

log "=== CourtView deploy -> $RPI_HOST ==="

# 1. Ensure remote dir exists (one SSH call before scp)
ssh "$RPI_HOST" "mkdir -p /root/projects/courtview" || { err "mkdir failed"; exit 1; }

# 1b. pip-audit security gate
log ""
log "--- pip-audit: checking for known vulnerabilities ---"
_audit_rc=0
_audit_out=$(ssh "$RPI_HOST" 'bash -s' << 'REMOTE'
if [[ ! -f /root/projects/courtview/requirements.txt ]]; then
    echo "SKIP: /root/projects/courtview/requirements.txt not found (first deploy)"
    exit 2
fi
python3 -m pip install pip-audit -q --break-system-packages 2>/dev/null
if ! python3 -c "import pip_audit" 2>/dev/null; then
    echo "SKIP: pip-audit unavailable after install attempt"
    exit 2
fi
python3 -m pip_audit -r /root/projects/courtview/requirements.txt 2>&1
REMOTE
) || _audit_rc=$?
if [[ $_audit_rc -eq 0 ]]; then
    ok "pip-audit: no known vulnerabilities found"
elif [[ $_audit_rc -eq 1 ]]; then
    err "pip-audit: vulnerabilities found - blocking deploy"
    echo "$_audit_out"
    exit 1
else
    log "pip-audit: audit skipped or tool unavailable (rc=$_audit_rc) - continuing"
    [[ -n "$_audit_out" ]] && echo "$_audit_out" | sed 's/^/  /'
fi

# 2. Copy all files in one scp connection
scp -q \
    "$SCRIPT_DIR/courtview.py" \
    "$SCRIPT_DIR/courtview.html" \
    "$SCRIPT_DIR/courtview.service" \
    "$SCRIPT_DIR/notify-telegram@.service" \
    "$SCRIPT_DIR/requirements.txt" \
    "${RPI_HOST}:/root/projects/courtview/" \
    && ok "  courtview.py courtview.html courtview.service notify-telegram@.service requirements.txt" \
    || { err "  FAILED: scp"; exit 1; }

# 3. All remote operations in one SSH call
remote_out=$(ssh "$RPI_HOST" 'bash -s' << 'REMOTE'
set -e

# Verify gunicorn binary exists
if [[ ! -x /usr/local/bin/gunicorn ]]; then
    _gc=$(which gunicorn 2>/dev/null)
    if [[ -z "$_gc" ]]; then
        echo "ERROR: gunicorn not found at /usr/local/bin/gunicorn and not on PATH - aborting" >&2
        exit 1
    fi
fi

# Install service files and enable
cp /root/projects/courtview/courtview.service /etc/systemd/system/courtview.service
cp /root/projects/courtview/notify-telegram@.service /etc/systemd/system/notify-telegram@.service
systemctl daemon-reload
systemctl enable courtview

# One-time cleanup: stop legacy watchdog and gunicorn (idempotent)
pkill -f /root/projects/courtview/watchdog.sh 2>/dev/null || true
pkill -f "gunicorn.*courtview" 2>/dev/null || true
sleep 2

# Restart via systemctl
systemctl restart courtview
sleep 2

# Remove @reboot crontab entries for courtview (idempotent)
crontab -l 2>/dev/null | grep -v "projects/courtview" | crontab -

# Report status
echo "ACTIVE:$(systemctl is-active courtview)"
echo "ENABLED:$(systemctl is-enabled courtview)"
REMOTE
)

cv_active=$(echo "$remote_out" | grep "^ACTIVE:" | sed 's/^ACTIVE://')
cv_enabled=$(echo "$remote_out" | grep "^ENABLED:" | sed 's/^ENABLED://')

[[ "$cv_active" == "active" ]] && ok "courtview: systemd active" || { err "courtview systemd unit not active (got: $cv_active)"; exit 1; }
[[ "$cv_enabled" == "enabled" ]] && ok "courtview: enabled on boot" || err "courtview not enabled (got: $cv_enabled)"

log "=== CourtView deploy complete ==="
