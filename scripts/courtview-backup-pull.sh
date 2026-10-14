#!/bin/zsh
# Pull the encrypted CourtView backups from the RPi to ~/Backups/courtview (keep 7).
# Run by launchd (no shell aliases), so the host is chosen by trying LAN then WAN ssh hosts.
set -euo pipefail

DEST="$HOME/Backups/courtview"
REMOTE_DIR="/root/projects/courtview/backups"
KEEP=7
STALE_HOURS=36
mkdir -p "$DEST"
chmod 700 "$DEST"
LOG="$DEST/pull.log"

log() { printf '%s %s\n' "$(TZ=Europe/London date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$LOG"; }

if ssh -o BatchMode=yes -o ConnectTimeout=5 pi-cmd true 2>/dev/null; then
    HOST=pi-cmd
elif ssh -o BatchMode=yes -o ConnectTimeout=10 pi-wan true 2>/dev/null; then
    HOST=pi-wan
else
    log "ERROR no RPi host reachable (pi-cmd, pi-wan)"
    exit 1
fi

remote_list=$(ssh -o BatchMode=yes "$HOST" "ls -1 $REMOTE_DIR/courtview-*.db.gpg 2>/dev/null" || true)
if [[ -z "$remote_list" ]]; then
    log "ERROR $HOST has no backups in $REMOTE_DIR"
    exit 1
fi

copied=0
for remote in ${(f)remote_list}; do
    name=${remote:t}
    if [[ ! -f "$DEST/$name" ]]; then
        scp -q -o BatchMode=yes "$HOST:$remote" "$DEST/$name.part"
        mv "$DEST/$name.part" "$DEST/$name"
        chmod 600 "$DEST/$name"
        copied=$((copied + 1))
    fi
done

# keep the 7 newest locally
local_files=(${(f)"$(ls -1 "$DEST"/courtview-*.db.gpg 2>/dev/null | sort)"})
while (( ${#local_files} > KEEP )); do
    rm -f "${local_files[1]}"
    shift local_files
done

newest=${local_files[-1]}
age_h=$(( ( $(date +%s) - $(stat -f %m "$newest") ) / 3600 ))
log "ok host=$HOST copied=$copied kept=${#local_files} newest=${newest:t} age_h=$age_h"
if (( age_h > STALE_HOURS )); then
    log "WARN newest backup is older than ${STALE_HOURS}h"
fi
