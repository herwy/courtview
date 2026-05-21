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

# 2. Copy all files in one scp connection
scp -q \
    "$SCRIPT_DIR/courtview.py" \
    "$SCRIPT_DIR/courtview.html" \
    "$SCRIPT_DIR/watchdog.sh" \
    "${RPI_HOST}:/root/projects/courtview/" \
    && ok "  courtview.py courtview.html watchdog.sh" \
    || { err "  FAILED: scp"; exit 1; }

# 3. All remote operations in one SSH call
remote_out=$(ssh "$RPI_HOST" 'bash -s' << 'REMOTE'
set -e
chmod +x /root/projects/courtview/watchdog.sh

# Install gunicorn only if missing
python3 -c "import gunicorn" 2>/dev/null \
    || pip3 install gunicorn -q --break-system-packages 2>/dev/null \
    || pip3 install gunicorn -q

# Stop watchdog first (avoids respawn race)
pkill -f /root/projects/courtview/watchdog.sh 2>/dev/null || true

# Stop courtview and wait for it to actually exit
pkill -f "gunicorn.*courtview" 2>/dev/null || true
pkill -f "/root/projects/courtview/courtview.py" 2>/dev/null || true
for i in 1 2 3 4 5; do
    pgrep -f "gunicorn.*courtview" > /dev/null 2>&1 || break
    sleep 1
done

# Start gunicorn and wait for it to appear
cd /root/projects/courtview
nohup gunicorn --workers 1 --bind 0.0.0.0:8766 --timeout 60 --log-level warning courtview:app \
    >> /root/projects/courtview/courtview.log 2>&1 &
for i in 1 2 3 4 5; do
    pgrep -f "gunicorn.*courtview" > /dev/null 2>&1 && break
    sleep 1
done

# Start watchdog and wait for it to appear
nohup zsh /root/projects/courtview/watchdog.sh > /dev/null 2>&1 &
for i in 1 2 3; do
    pgrep -f watchdog.sh > /dev/null 2>&1 && break
    sleep 1
done

# Report status
CV_PID=$(pgrep -fa "gunicorn.*courtview" | head -1)
WD_PID=$(pgrep -fa watchdog.sh | head -1)
echo "CV:${CV_PID}"
echo "WD:${WD_PID}"
REMOTE
)

cv_pid=$(echo "$remote_out" | grep "^CV:" | sed 's/^CV://')
wd_pid=$(echo "$remote_out" | grep "^WD:" | sed 's/^WD://')

[[ -n "$cv_pid" ]] && ok "courtview: $cv_pid" || { err "courtview did not start"; exit 1; }
[[ -n "$wd_pid" ]] && ok "watchdog:   $wd_pid" || { err "watchdog did not start"; exit 1; }

log "=== CourtView deploy complete ==="
