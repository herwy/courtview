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

# Restart Flask server
ssh "$RPI_HOST" "pkill -f '/root/projects/courtview/courtview.py' 2>/dev/null; true"
sleep 1
ssh "$RPI_HOST" "nohup python3 -u /root/projects/courtview/courtview.py >> /root/projects/courtview/courtview.log 2>&1 &"
sleep 2

_cv_pid=$(ssh "$RPI_HOST" "pgrep -fa courtview.py" 2>/dev/null)
if [[ -z "$_cv_pid" ]]; then
    err "courtview.py did not start - check /root/projects/courtview/courtview.log"
    exit 1
fi
ok "courtview running: $_cv_pid"
log "=== CourtView deploy complete ==="
