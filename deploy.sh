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
if ! python3 -c "import pip_audit" 2>/dev/null; then
    python3 -m pip install pip-audit -q --break-system-packages 2>/dev/null
fi
if ! python3 -c "import pip_audit" 2>/dev/null; then
    echo "SKIP: pip-audit unavailable after install attempt"
    exit 2
fi
pins=$(mktemp)
trap 'rm -f "$pins"' EXIT
python3 - /root/projects/courtview/requirements.txt > "$pins" << 'CVPINS'
import sys
from importlib.metadata import distribution, PackageNotFoundError
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
todo = [Requirement(l) for l in open(sys.argv[1]).read().split("\n") if l.strip() and not l.startswith("#")]
seen = {}
while todo:
    req = todo.pop()
    if req.marker and not req.marker.evaluate({"extra": ""}): continue
    name = canonicalize_name(req.name)
    if name in seen: continue
    try: dist = distribution(name)
    except PackageNotFoundError: sys.exit(f"MISSING {name}")
    seen[name] = dist.version
    todo.extend(Requirement(r) for r in dist.requires or [])
if not seen: sys.exit("no packages resolved")
for n, v in sorted(seen.items()): print(f"{n}=={v}")
CVPINS
resolve_rc=$?
if [[ $resolve_rc -ne 0 || ! -s "$pins" ]]; then
    echo "BLOCK: could not resolve installed dependency closure (missing package or empty closure)"
    exit 1
fi
echo "Auditing $(wc -l < "$pins") installed packages"
out=$(python3 -m pip_audit --no-deps --disable-pip -r "$pins" 2>&1)
rc=$?
printf '%s\n' "$out" | grep -v 'WARNING:pip_audit._cli' || true
if [[ $rc -eq 0 ]]; then
    exit 0
fi
exit 1
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
    "$SCRIPT_DIR/cv_notify_fail.py" \
    "$SCRIPT_DIR/cv_purge_pii.py" \
    "$SCRIPT_DIR/cv_backup.py" \
    "$SCRIPT_DIR/courtview-backup.service" \
    "$SCRIPT_DIR/courtview-backup.timer" \
    "$SCRIPT_DIR/courtview-backup.pub.asc" \
    "$SCRIPT_DIR/requirements.txt" \
    "${RPI_HOST}:/root/projects/courtview/" \
    && ok "  courtview.py courtview.html courtview.service notify-telegram@.service cv_notify_fail.py cv_purge_pii.py cv_backup.py courtview-backup.{service,timer,pub.asc} requirements.txt" \
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
cp /root/projects/courtview/courtview-backup.service /etc/systemd/system/courtview-backup.service
cp /root/projects/courtview/courtview-backup.timer /etc/systemd/system/courtview-backup.timer
mkdir -p -m 700 /root/projects/courtview/backups
systemctl daemon-reload
systemctl enable courtview
systemctl enable --now courtview-backup.timer
echo "BACKUPTIMER:$(systemctl is-enabled courtview-backup.timer)"

# HMAC secret for pseudonymous customer keys: created once, never echoed, never backed up.
HMAC_KEY=/root/.courtview_hmac_key
if [[ ! -s "$HMAC_KEY" ]]; then
    ( umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "${HMAC_KEY}.tmp" )
    mv "${HMAC_KEY}.tmp" "$HMAC_KEY"
fi
chmod 600 "$HMAC_KEY"
[[ -s "$HMAC_KEY" ]] && echo "HMACKEY:present" || echo "HMACKEY:missing"

# One-off PII purge: runs only while legacy PII columns exist or the purge marker is absent.
# The service is stopped via systemctl for the purge (it refuses to run against a live service).
set +e
chk_out=$(python3 /root/projects/courtview/cv_purge_pii.py --check 2>&1)
chk_rc=$?
set -e
chk_first=$(echo "$chk_out" | head -1)
if [[ $chk_rc -eq 0 ]]; then
    systemctl stop courtview
    set +e
    purge_out=$(python3 /root/projects/courtview/cv_purge_pii.py 2>&1)
    purge_rc=$?
    set -e
    echo "$purge_out" | sed 's/^/PURGE:/'
    if [[ $purge_rc -ne 0 ]]; then
        systemctl start courtview
        echo "PURGEFAIL:$purge_rc"
        exit 1
    fi
elif [[ $chk_rc -eq 1 && ( "$chk_first" == "clean" || "$chk_first" == nothing\ to\ purge* ) ]]; then
    echo "PURGE:not needed"
else
    echo "PURGEFAIL:check rc=$chk_rc"
    exit 1
fi

# Restart via systemctl only. Never pkill or hand-run gunicorn: a stray
# process holding :8766 crash-loops the unit (2026-10-13 outage).
systemctl restart courtview
sleep 2

# Data files must be owner-only (UMask=0077 covers new files; this fixes existing ones)
for f in courtview_cache.db courtview_cache.db-wal courtview_cache.db-shm access.log; do
    [[ -e "/root/projects/courtview/$f" ]] && chmod 600 "/root/projects/courtview/$f"
done
for f in courtview_cache.db courtview_cache.db-wal courtview_cache.db-shm access.log; do
    [[ -e "/root/projects/courtview/$f" ]] && echo "MODES:$(stat -c '%a %n' "/root/projects/courtview/$f")"
done

# The :8766 listener must be systemd's MainPID, not a stray gunicorn
_main_pid=$(systemctl show courtview -p MainPID --value)
# gunicorn master and worker share the socket, so MainPID must be among them
_listen_pids=$(ss -ltnpH "sport = :8766" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | tr '\n' ',')
echo "LISTENER:${_listen_pids:-none}:${_main_pid}"

# Report status
echo "ACTIVE:$(systemctl is-active courtview)"
echo "ENABLED:$(systemctl is-enabled courtview)"
REMOTE
)

echo "$remote_out" | grep -E "^(HMACKEY|PURGE|PURGEFAIL|MODES|BACKUPTIMER):" | sed 's/^/  /'
if echo "$remote_out" | grep -q "^PURGEFAIL:"; then err "PII purge failed - see output above"; exit 1; fi
bad_modes=$(echo "$remote_out" | grep "^MODES:" | grep -vE "^MODES:600 " || true)
[[ -z "$bad_modes" ]] && ok "data file modes all 600" || { err "data files not 0600: $bad_modes"; exit 1; }

[[ "$(echo "$remote_out" | grep "^BACKUPTIMER:" | sed 's/^BACKUPTIMER://')" == "enabled" ]] \
    && ok "courtview-backup.timer enabled" || { err "courtview-backup.timer not enabled"; exit 1; }

cv_active=$(echo "$remote_out" | grep "^ACTIVE:" | sed 's/^ACTIVE://')
cv_enabled=$(echo "$remote_out" | grep "^ENABLED:" | sed 's/^ENABLED://')

[[ "$cv_active" == "active" ]] && ok "courtview: systemd active" || { err "courtview systemd unit not active (got: $cv_active)"; exit 1; }
[[ "$cv_enabled" == "enabled" ]] && ok "courtview: enabled on boot" || err "courtview not enabled (got: $cv_enabled)"

listener=$(echo "$remote_out" | grep "^LISTENER:" | sed 's/^LISTENER://')
listen_pids=${listener%%:*}
main_pid=${listener##*:}
if [[ -n "$main_pid" && "$main_pid" != "0" && ",$listen_pids" == *",$main_pid,"* ]]; then
    ok "courtview: :8766 owned by systemd MainPID $main_pid"
else
    err ":8766 listener pids ($listen_pids) exclude systemd MainPID ($main_pid) - stray process on the port?"
    exit 1
fi

log "=== CourtView deploy complete ==="
