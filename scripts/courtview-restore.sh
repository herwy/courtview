#!/bin/zsh
# Restore a CourtView backup.
#   courtview-restore.sh [--key KEYFILE] [BACKUP.db.gpg]          decrypt + integrity check only
#   courtview-restore.sh --apply [--key KEYFILE] [BACKUP.db.gpg]  ...then replace the DB on the RPi
# KEYFILE is the exported secret key (only needed on a machine that doesn't have it yet).
# BACKUP defaults to the newest file in ~/Backups/courtview.
set -euo pipefail

FPR=CFA832B6B2983DD121CACB830317325FB7C4A6F9
DEST="$HOME/Backups/courtview"
REMOTE_DB=/var/lib/courtview/courtview_cache.db
GPG=$(command -v gpg)

apply=0; key=""; backup=""
while (( $# > 0 )); do
    case "$1" in
        --apply) apply=1; shift ;;
        --key)   key="$2"; shift 2 ;;
        *)       backup="$1"; shift ;;
    esac
done

if ! $GPG --list-secret-keys "$FPR" >/dev/null 2>&1; then
    [[ -n "$key" ]] || { echo "Backup key not in keyring: pass --key KEYFILE" >&2; exit 1; }
    $GPG --batch --import "$key"
fi

[[ -n "$backup" ]] || backup=$(ls -1 "$DEST"/courtview-*.db.gpg 2>/dev/null | sort | tail -1)
[[ -f "$backup" ]] || { echo "Backup not found: ${backup:-none in $DEST}" >&2; exit 1; }

work=$(mktemp -d); chmod 700 "$work"
trap 'rm -rf "$work"' EXIT
$GPG --batch --quiet --output "$work/restore.db" --decrypt "$backup"
check=$(sqlite3 "$work/restore.db" 'PRAGMA integrity_check')
echo "backup: ${backup:t}  integrity: $check"
[[ "$check" == "ok" ]] || { echo "Integrity check failed, not restoring" >&2; exit 1; }
(( apply )) || { echo "Verified. Re-run with --apply to restore it to the RPi."; exit 0; }

read -q "?Replace the live CourtView database with ${backup:t}? [y/N] " || { echo; exit 1; }
echo
if ssh -o BatchMode=yes -o ConnectTimeout=5 pi-cmd true 2>/dev/null; then host=pi-cmd
elif ssh -o BatchMode=yes -o ConnectTimeout=10 pi-wan true 2>/dev/null; then host=pi-wan
else echo "RPi unreachable (pi-cmd, pi-wan)" >&2; exit 1; fi

scp -q "$work/restore.db" "$host:$REMOTE_DB.restore"
ssh "$host" "set -e; chown courtview:courtview $REMOTE_DB.restore; chmod 600 $REMOTE_DB.restore; systemctl stop courtview; \
  rm -f $REMOTE_DB-wal $REMOTE_DB-shm; mv $REMOTE_DB.restore $REMOTE_DB; \
  systemctl start courtview; sleep 3; systemctl is-active courtview"
echo "Restored ${backup:t} on $host."
