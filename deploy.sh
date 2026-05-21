#!/bin/zsh
# deploy.sh - sync CourtView to RPi and restart the Flask server.
#
# Usage: zsh deploy.sh [--rpi HOST]
#
#   --rpi HOST     SSH host for the RPi (default: pi-cmd)
#
# Deploys courtview.py and courtview.html to /root/projects/courtview/,
# kills any running courtview.py, and restarts it via nohup.
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

# Remote dir
ssh "$RPI_HOST" "mkdir -p /root/projects/courtview" || { err "mkdir failed"; exit 1; }

# Deploy Python source
scp -q "$SCRIPT_DIR/courtview.py" "${RPI_HOST}:/root/projects/courtview/courtview.py" \
    && ok "  courtview.py" \
    || { err "  FAILED: courtview.py"; exit 1; }

# Deploy dashboard HTML
scp -q "$SCRIPT_DIR/courtview.html" "${RPI_HOST}:/root/projects/courtview/courtview.html" \
    && ok "  courtview.html" \
    || { err "  FAILED: courtview.html"; exit 1; }

# Deploy watchdog
scp -q "$SCRIPT_DIR/watchdog.sh" "${RPI_HOST}:/root/projects/courtview/watchdog.sh" \
    && ok "  watchdog.sh" \
    || { err "  FAILED: watchdog.sh"; exit 1; }
ssh "$RPI_HOST" "chmod +x /root/projects/courtview/watchdog.sh"

# Install gunicorn if missing
ssh "$RPI_HOST" "pip3 install gunicorn -q" || { err "gunicorn install failed"; exit 1; }

# Stop watchdog FIRST so it doesn't race the restart
ssh "$RPI_HOST" "pkill -f /root/projects/courtview/watchdog.sh 2>/dev/null; true"

# Stop any current courtview (gunicorn or legacy python)
ssh "$RPI_HOST" "pkill -f 'gunicorn.*courtview' 2>/dev/null; pkill -f '/root/projects/courtview/courtview.py' 2>/dev/null; true"
sleep 1

# Start via gunicorn (single worker, threaded model preserved by Flask + background daemon threads)
ssh "$RPI_HOST" "cd /root/projects/courtview && nohup gunicorn --workers 1 --bind 0.0.0.0:8766 --timeout 60 --log-level warning courtview:app >> /root/projects/courtview/courtview.log 2>&1 &"
sleep 2

_cv_pid=$(ssh "$RPI_HOST" "pgrep -fa 'gunicorn.*courtview|courtview.py'" 2>/dev/null)
if [[ -z "$_cv_pid" ]]; then
    err "courtview did not start - check /root/projects/courtview/courtview.log"
    exit 1
fi
ok "courtview running: $_cv_pid"

# Start watchdog AFTER courtview is confirmed up
ssh "$RPI_HOST" "nohup zsh /root/projects/courtview/watchdog.sh >/dev/null 2>&1 &"
sleep 1
_wd_pid=$(ssh "$RPI_HOST" "pgrep -fa watchdog.sh" 2>/dev/null)
if [[ -z "$_wd_pid" ]]; then
    err "watchdog did not start"
    exit 1
fi
ok "watchdog running: $_wd_pid"

log "=== CourtView deploy complete ==="
