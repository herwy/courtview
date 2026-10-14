#!/bin/zsh
# deploy.sh - stage a CourtView release on the RPi, audit it, install it, health-check it and
# roll back automatically on failure.
#
# Usage: zsh deploy.sh [--rpi HOST]
#        zsh deploy.sh [--rpi HOST] --finalize-migration
#
# Layout on the RPi (see .claude/docs/courtview-internals.md):
#   /opt/courtview/releases/<UTC ts>/   root-owned code, one dir per deploy
#   /opt/courtview/current              symlink to the live release
#   /var/lib/courtview                  data, owned by the sandboxed courtview user
#
# --finalize-migration deletes the pre-migration copy of the data and code under
# /root/projects/courtview (and the safety copy in /root/courtview-migrate-*). Run it only
# after the post-migration checks have passed.
set -o pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: deploy.sh must be run on macOS, not on the RPi" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RPI_HOST="pi-cmd"
MODE="deploy"

while (( $# > 0 )); do
    case "$1" in
        --rpi) RPI_HOST="$2"; shift 2 ;;
        --finalize-migration) MODE="finalize"; shift ;;
        *)     shift ;;
    esac
done

ts()  { date '+%Y-%m-%dT%H:%M:%S'; }
log() { echo "[$(ts)] $*"; }
ok()  { echo "[$(ts)] OK  $*"; }
err() { echo "[$(ts)] ERR $*" >&2; }

RELEASE_TS="$(date -u '+%Y%m%dT%H%M%SZ')"
FILES=(courtview.py courtview.html web cv_*.py(N)
       courtview.service notify-telegram@.service courtview-recovery@.service courtview-recovery@.timer
       courtview-backup.service courtview-backup.timer courtview-backup.pub.asc
       requirements.in requirements.txt)

log "=== CourtView deploy -> $RPI_HOST (release $RELEASE_TS, mode $MODE) ==="

if [[ "$MODE" == "finalize" ]]; then
    fin_out=$(ssh "$RPI_HOST" "bash -s -- $RELEASE_TS finalize" <<'REMOTE'
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OLD=/root/projects/courtview
DATA=/var/lib/courtview
[[ -f $DATA/.migrated ]] || { echo "FAIL:not migrated (no $DATA/.migrated)"; exit 1; }
[[ "$(readlink $OLD/status)" == "$DATA/status" ]] || { echo "FAIL:$OLD/status is not the symlink to $DATA/status"; exit 1; }
systemctl is-active --quiet courtview || { echo "FAIL:courtview not active"; exit 1; }
find "$OLD" -mindepth 1 -maxdepth 1 ! -name status ! -name README -exec rm -rf -- {} +
echo "CourtView moved on 2026-10-14. Code: /opt/courtview/current. Data: /var/lib/courtview. This dir only holds the status/ symlink read by labs_heartbeat." > "$OLD/README"
rm -rf /root/courtview-migrate-*
echo "FINALIZED:$(ls -A $OLD | tr '\n' ' ')"
REMOTE
    )
    fin_rc=$?
    echo "$fin_out" | sed 's/^/  /'
    (( fin_rc == 0 )) && ok "migration finalized" || { err "finalize failed"; exit 1; }
    exit 0
fi

# 1. Stage the release (one ssh): upload, normalise ownership and modes, precompile, then rename into place
log "--- staging release ---"
( cd "$SCRIPT_DIR" && COPYFILE_DISABLE=1 tar --no-xattrs -cf - "${FILES[@]}" ) 2>/dev/null \
  | ssh "$RPI_HOST" "set -e; umask 022; mkdir -p /opt/courtview/releases; chmod 755 /opt/courtview /opt/courtview/releases; \
      P=/opt/courtview/releases/$RELEASE_TS.partial; rm -rf \$P; mkdir \$P; tar xf - -C \$P --no-same-owner 2>/dev/null; \
      chown -R root:root \$P; find \$P -type d -exec chmod 755 {} +; find \$P -type f -exec chmod 644 {} +; \
      python3 -m compileall -q \$P >/dev/null; \
      mv \$P /opt/courtview/releases/$RELEASE_TS" \
  && ok "staged /opt/courtview/releases/$RELEASE_TS" \
  || { err "FAILED: staging release"; exit 1; }

drop_staged() { ssh "$RPI_HOST" "rm -rf /opt/courtview/releases/$RELEASE_TS /opt/courtview/releases/$RELEASE_TS.partial" >/dev/null 2>&1; }

# 2. pip-audit gate: pins must match the installed closure and pip-audit must pass. Never auto-installs.
log ""
log "--- pip-audit: pins and known vulnerabilities ---"
_audit_rc=0
_audit_out=$(ssh "$RPI_HOST" "bash -s -- $RELEASE_TS" << 'REMOTE'
REL=/opt/courtview/releases/$1
if ! python3 -c "import pip_audit" 2>/dev/null; then
    echo "BLOCK: pip-audit is not installed on the RPi. Install it once, then redeploy:"
    echo "  python3 -m pip install --break-system-packages pip-audit"
    exit 1
fi
live=$(mktemp); pins=$(mktemp)
trap 'rm -f "$live" "$pins"' EXIT
if ! python3 -I "$REL/cv_closure.py" "$REL/requirements.in" > "$live" || [[ ! -s "$live" ]]; then
    echo "BLOCK: could not resolve the installed dependency closure (missing package or empty closure)"
    exit 1
fi
grep -v -e '^#' -e '^[[:space:]]*$' "$REL/requirements.txt" | sort > "$pins"
sort -o "$live" "$live"
if ! diff_out=$(diff "$live" "$pins"); then
    echo "BLOCK: requirements.txt pins differ from the installed packages (< installed, > pinned):"
    echo "$diff_out" | grep '^[<>]'
    exit 1
fi
echo "Auditing $(wc -l < "$pins") pinned packages"
out=$(python3 -m pip_audit --no-deps --disable-pip -r "$pins" 2>&1)
rc=$?
printf '%s\n' "$out" | grep -v 'WARNING:pip_audit._cli' || true
exit $(( rc == 0 ? 0 : 1 ))
REMOTE
) || _audit_rc=$?
if [[ $_audit_rc -eq 0 ]]; then
    ok "pip-audit: pins match installed packages, no known vulnerabilities"
else
    err "pip-audit gate failed (rc=$_audit_rc) - blocking deploy"
    echo "$_audit_out" | sed 's/^/  /'
    drop_staged
    exit 1
fi

# 3. Install, migrate (first time only), restart, health-check, roll back on failure (one ssh)
remote_out=$(ssh "$RPI_HOST" "bash -s -- $RELEASE_TS deploy" << 'REMOTE'
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TS=$1
BASE=/opt/courtview
REL=$BASE/releases/$TS
OLD=/root/projects/courtview
DATA=/var/lib/courtview
NOTIFY=/var/lib/courtview-notify
WORK=/root/courtview-migrate-$TS
UNITS="courtview.service notify-telegram@.service courtview-recovery@.service courtview-recovery@.timer courtview-backup.service courtview-backup.timer"
MIGRATED=0
FLIPPED=0
PREV=""
EPOCH=0

DBINFO_PY=$(cat << 'PY'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=30)
ic = c.execute("pragma integrity_check").fetchone()[0]
tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by 1")]
counts = {t: c.execute('select count(*) from "%s"' % t).fetchone()[0] for t in tabs}
print(json.dumps({"integrity": ic, "counts": counts}, sort_keys=True))
PY
)

HEALTH_PY=$(cat << 'PY'
import datetime, json, sys, time, urllib.request
epoch = float(sys.argv[1])
token = open("/root/.courtview_token").read().strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.time() + 30
last = "no response"
while time.time() < deadline:
    try:
        req = urllib.request.Request("http://127.0.0.1:8766/api/health", headers={"Cookie": "courtview_token=" + token})
        with opener.open(req, timeout=5) as resp:
            if resp.status == 200:
                started = datetime.datetime.strptime(json.load(resp)["started_at"][:19], "%Y-%m-%dT%H:%M:%S")
                if started.replace(tzinfo=datetime.timezone.utc).timestamp() >= epoch - 1:
                    print("HEALTH:ok")
                    sys.exit(0)
                last = "started_at older than the restart"
            else:
                last = "status %d" % resp.status
    except Exception as exc:
        last = type(exc).__name__
    time.sleep(1)
print("HEALTH:fail " + last)
sys.exit(1)
PY
)

health() { python3 -I -c "$HEALTH_PY" "$EPOCH"; }

install_units() {  # dir
    for u in $UNITS; do
        [[ -f "$1/$u" ]] && install -m 0644 -o root -g root "$1/$u" "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
}

flip_to() {  # relative target, e.g. releases/<ts>
    ln -sfn "$1" $BASE/current.tmp && mv -T $BASE/current.tmp $BASE/current
}

mark_failed() { [[ -d $REL ]] && mv "$REL" "$REL.failed"; }

migration_abort() {  # reason
    echo "MIGRATE:ABORT:$1"
    systemctl stop courtview
    systemctl disable --now courtview-recovery@courtview.timer >/dev/null 2>&1
    for u in courtview.service notify-telegram@.service courtview-backup.service courtview-backup.timer; do
        [[ -f $WORK/$u.orig ]] && install -m 0644 -o root -g root "$WORK/$u.orig" "/etc/systemd/system/$u"
    done
    rm -f /etc/systemd/system/courtview-recovery@.service /etc/systemd/system/courtview-recovery@.timer
    systemctl daemon-reload
    if [[ -L $OLD/status ]]; then
        rm -f $OLD/status
        [[ -d $WORK/status.orig ]] && mv $WORK/status.orig $OLD/status
    fi
    [[ -d $DATA ]] && mv $DATA $WORK/data.aborted
    [[ -d $NOTIFY ]] && mv $NOTIFY $WORK/notify.aborted
    rm -f $BASE/current $BASE/current.tmp
    mark_failed
    systemctl start courtview
    for _ in $(seq 1 30); do systemctl is-active --quiet courtview && break; sleep 1; done
    echo "MIGRATE:ROLLED_BACK:$1 (old root unit is $(systemctl is-active courtview), safety copy in $WORK)"
    exit 1
}

rollback() {  # reason
    echo "ROLLBACK:begin:$1"
    if [[ -n $PREV && -d $BASE/$PREV ]]; then
        flip_to "$PREV"
        install_units "$BASE/$PREV"
        EPOCH=$(date +%s)
        systemctl restart courtview
        if health >/dev/null 2>&1; then
            mark_failed
            echo "ROLLEDBACK:${PREV#releases/}:$1"
            exit 1
        fi
        echo "ROLLBACKFAIL:previous release ${PREV#releases/} did not become healthy"
        exit 1
    fi
    echo "ROLLBACKFAIL:no previous release to roll back to"
    exit 1
}

fail_deploy() {  # reason
    if [[ -n $PREV ]]; then
        rollback "$1"
    elif (( MIGRATED )); then
        migration_abort "$1"
    fi
    echo "FAIL:$1"
    mark_failed
    exit 1
}

# --- prerequisites
id courtview >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin courtview
[[ -x /usr/local/bin/gunicorn ]] || { echo "FAIL:gunicorn not found at /usr/local/bin/gunicorn"; mark_failed; exit 1; }
PREV=$(readlink $BASE/current 2>/dev/null || true)

# --- one-off migration from /root/projects/courtview to /var/lib/courtview
if [[ -f $OLD/courtview_cache.db && ! -L $OLD/courtview_cache.db && ! -e $DATA/.migrated ]]; then
    echo "MIGRATE:start (work dir $WORK)"
    MIGRATED=1
    ( umask 077; mkdir -p "$WORK" ) || { echo "FAIL:cannot create $WORK"; mark_failed; exit 1; }
    chmod 700 "$WORK"
    cp /etc/systemd/system/courtview.service "$WORK/courtview.service.orig"
    cp /etc/systemd/system/notify-telegram@.service "$WORK/notify-telegram@.service.orig"
    cp /etc/systemd/system/courtview-backup.service "$WORK/courtview-backup.service.orig"
    cp /etc/systemd/system/courtview-backup.timer "$WORK/courtview-backup.timer.orig"
    systemctl stop courtview
    for _ in $(seq 1 120); do systemctl is-active --quiet courtview-backup.service || break; sleep 1; done
    systemctl is-active --quiet courtview-backup.service && migration_abort "backup job still running after 120 s"

    # safety copy through the sqlite backup API, then record its integrity and counts
    python3 -I - "$OLD/courtview_cache.db" "$WORK/pre.db" << 'PY' || migration_abort "safety copy failed"
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
    chmod 600 "$WORK/pre.db"
    python3 -I -c "$DBINFO_PY" "$WORK/pre.db" > "$WORK/pre.json" || migration_abort "pre.db unreadable"
    python3 -I -c "import json,sys; sys.exit(0 if json.load(open('$WORK/pre.json'))['integrity']=='ok' else 1)" || migration_abort "pre.db integrity_check not ok"
    python3 -I -c "import sqlite3; c=sqlite3.connect('$OLD/courtview_cache.db', timeout=30); print('MIGRATE:wal_checkpoint', c.execute('pragma wal_checkpoint(TRUNCATE)').fetchone())" || migration_abort "wal_checkpoint failed"

    # copy (never move) the data into the new layout
    install -d -o courtview -g courtview -m 0700 $DATA || migration_abort "cannot create $DATA"
    cp --preserve=mode,timestamps $OLD/courtview_cache.db $DATA/ || migration_abort "copy of the database failed"
    for f in $OLD/access.log*; do [[ -f $f ]] && { cp --preserve=mode,timestamps "$f" $DATA/ || migration_abort "copy of $f failed"; }; done
    [[ -d $OLD/backups ]] && { cp -r --preserve=mode,timestamps $OLD/backups $DATA/ || migration_abort "copy of backups failed"; }
    install -d -m 0755 $DATA/status
    for f in $OLD/status/*.json; do [[ -f $f ]] && { cp --preserve=mode,timestamps "$f" $DATA/status/ || migration_abort "copy of $f failed"; }; done
    chown -R courtview:courtview $DATA
    install -d -o root -g root -m 0700 $NOTIFY
    for f in unit_failure_state.json unit_failure_state.json.lock; do
        [[ -f $OLD/$f ]] && install -m 0600 -o root -g root $OLD/$f $NOTIFY/$f
    done

    # verify the copy as the courtview user: integrity and per-table counts must match pre.db
    ( cd / && runuser -u courtview -- python3 -I -c "$DBINFO_PY" $DATA/courtview_cache.db ) > "$WORK/new.json" || migration_abort "new database unreadable by courtview"
    python3 -I - "$WORK/pre.json" "$WORK/new.json" << 'PY' || migration_abort "integrity or per-table counts differ"
import json, sys
pre, new = (json.load(open(p)) for p in sys.argv[1:3])
print("MIGRATE:integrity pre=%s new=%s" % (pre["integrity"], new["integrity"]))
bad = pre["integrity"] != "ok" or new["integrity"] != "ok" or pre["counts"] != new["counts"]
for t in sorted(set(pre["counts"]) | set(new["counts"])):
    print("MIGRATE:count %s pre=%s new=%s" % (t, pre["counts"].get(t), new["counts"].get(t)))
print("MIGRATE:counts %s (%d tables, %d rows)" % ("DIFFER" if bad else "identical", len(new["counts"]), sum(new["counts"].values())))
sys.exit(1 if bad else 0)
PY

    # status swap for labs: /root/projects/courtview/status becomes a symlink to the new status dir
    ln -s $DATA/status $OLD/status.new || migration_abort "cannot create the status symlink"
    mv $OLD/status $WORK/status.orig || migration_abort "cannot move the old status dir"
    mv -T $OLD/status.new $OLD/status || migration_abort "cannot swap in the status symlink"
    python3 -I -c "import json; json.load(open('$OLD/status/payments.json')); print('MIGRATE:status via old path parses')" || migration_abort "status JSON not readable via the old path"
fi

# --- install units and enable timers
install_units "$REL"
systemctl enable courtview >/dev/null 2>&1
systemctl enable --now courtview-backup.timer >/dev/null 2>&1
systemctl enable courtview-recovery@courtview.timer >/dev/null 2>&1
echo "BACKUPTIMER:$(systemctl is-enabled courtview-backup.timer)"
echo "RECOVERYTIMER:$(systemctl is-enabled courtview-recovery@courtview.timer)"

# --- HMAC secret for pseudonymous customer keys: created once, never echoed, never backed up
HMAC_KEY=/root/.courtview_hmac_key
if [[ ! -s "$HMAC_KEY" ]]; then
    ( umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "${HMAC_KEY}.tmp" )
    mv "${HMAC_KEY}.tmp" "$HMAC_KEY"
fi
chmod 600 "$HMAC_KEY"
[[ -s "$HMAC_KEY" ]] && echo "HMACKEY:present" || echo "HMACKEY:missing"

# --- one-off PII purge, run as courtview with a throwaway courtview-owned copy of the HMAC key
hk=$(mktemp)
install -m 0600 -o courtview -g courtview "$HMAC_KEY" "$hk"
purge() { ( cd / && runuser -u courtview -- env COURTVIEW_DATA_DIR=$DATA COURTVIEW_HMAC_KEY_PATH="$hk" PYTHONDONTWRITEBYTECODE=1 python3 -I "$REL/cv_purge_pii.py" "$@" 2>&1 ); }
chk_out=$(purge --check)
chk_rc=$?
chk_first=$(echo "$chk_out" | head -1)
if [[ $chk_rc -eq 0 ]]; then
    systemctl stop courtview
    purge_out=$(purge)
    purge_rc=$?
    echo "$purge_out" | sed 's/^/PURGE:/'
    if [[ $purge_rc -ne 0 ]]; then
        rm -f "$hk"
        fail_deploy "PII purge failed (rc=$purge_rc)"
    fi
elif [[ $chk_rc -eq 1 && ( "$chk_first" == "clean" || "$chk_first" == nothing\ to\ purge* ) ]]; then
    echo "PURGE:not needed"
else
    rm -f "$hk"
    fail_deploy "PII purge check failed (rc=$chk_rc)"
fi
rm -f "$hk"

# --- flip current and restart. Restart via systemctl only: never pkill or hand-run gunicorn
# (a stray process holding :8766 crash-loops the unit, 2026-10-13 outage).
flip_to "releases/$TS"
FLIPPED=1
EPOCH=$(date +%s)
systemctl restart courtview
echo "RELEASE:$TS prev=${PREV#releases/}"

# --- health: authenticated GET /api/health over HTTP, started_at must be newer than the restart
health_out=$(health 2>&1) || fail_deploy "health check failed: ${health_out#HEALTH:fail }"
echo "$health_out"

# --- process and file checks
for f in courtview_cache.db courtview_cache.db-wal courtview_cache.db-shm; do
    [[ -e $DATA/$f ]] && { chown courtview:courtview $DATA/$f; chmod 600 $DATA/$f; }
done
for f in $DATA/access.log*; do [[ -e $f ]] && { chown courtview:courtview "$f"; chmod 600 "$f"; }; done
for f in $DATA/courtview_cache.db $DATA/courtview_cache.db-wal $DATA/courtview_cache.db-shm $DATA/access.log*; do
    [[ -e $f ]] && echo "MODES:$(stat -c '%a %U' "$f") $(basename "$f")"
done
MAIN=$(systemctl show courtview -p MainPID --value)
LISTEN=$(ss -ltnpH "sport = :8766" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | tr '\n' ',')
echo "LISTENER:${LISTEN:-none}:${MAIN}"
echo "MAINUSER:$(ps -o user:32= -p "$MAIN" 2>/dev/null | tr -d ' ')"
echo "ACTIVE:$(systemctl is-active courtview)"
echo "ENABLED:$(systemctl is-enabled courtview)"
echo "CURRENT:$(readlink $BASE/current)"
DEFECT=""
[[ "$(systemctl is-active courtview)" == "active" ]] || DEFECT="courtview not active"
[[ -n $MAIN && $MAIN != 0 && ",$LISTEN" == *",$MAIN,"* ]] || DEFECT="${DEFECT:-:8766 listener ($LISTEN) excludes systemd MainPID ($MAIN)}"
[[ "$(ps -o user:32= -p "$MAIN" 2>/dev/null | tr -d ' ')" == "courtview" ]] || DEFECT="${DEFECT:-MainPID $MAIN does not run as courtview}"
for f in $DATA/courtview_cache.db $DATA/courtview_cache.db-wal $DATA/courtview_cache.db-shm $DATA/access.log*; do
    [[ -e $f && "$(stat -c '%a %U' "$f")" != "600 courtview" ]] && DEFECT="${DEFECT:-$f is not 600 courtview}"
done
[[ -n $DEFECT ]] && fail_deploy "$DEFECT"

# --- success: mark the migration, keep the old data until the post-migration checks pass, prune releases
if (( MIGRATED )); then
    touch $DATA/.migrated
    echo "MIGRATE:done (old data kept in $OLD and the safety copy in $WORK until --finalize-migration)"
fi
keep=$(ls -1 $BASE/releases | sort -r | head -3; basename "$(readlink $BASE/current)"; [[ -n $PREV ]] && basename "$PREV")
for d in $(ls -1 $BASE/releases); do
    echo "$keep" | grep -qx "$d" || rm -rf "$BASE/releases/$d"
done
echo "RELEASES:$(ls -1 $BASE/releases | tr '\n' ' ')"
REMOTE
)
remote_rc=$?

echo "$remote_out" | grep -E "^(MIGRATE|PURGE|HMACKEY|BACKUPTIMER|RECOVERYTIMER|RELEASE|HEALTH|MODES|LISTENER|MAINUSER|ACTIVE|ENABLED|CURRENT|RELEASES|ROLLBACK|ROLLEDBACK|ROLLBACKFAIL|FAIL):" | sed 's/^/  /'

if echo "$remote_out" | grep -q "^MIGRATE:ROLLED_BACK:"; then
    err "migration aborted and rolled back: $(echo "$remote_out" | grep '^MIGRATE:ROLLED_BACK:' | sed 's/^MIGRATE:ROLLED_BACK://')"
    exit 1
fi
rb=$(echo "$remote_out" | grep "^ROLLEDBACK:" | head -1 | sed 's/^ROLLEDBACK://')
if [[ -n "$rb" ]]; then
    err "ROLLED BACK to ${rb%%:*} (${rb#*:})"
    exit 1
fi
if echo "$remote_out" | grep -q "^ROLLBACKFAIL:"; then
    err "rollback failed: $(echo "$remote_out" | grep '^ROLLBACKFAIL:' | sed 's/^ROLLBACKFAIL://')"
    exit 1
fi
if echo "$remote_out" | grep -q "^FAIL:"; then
    err "deploy failed: $(echo "$remote_out" | grep '^FAIL:' | head -1 | sed 's/^FAIL://')"
    exit 1
fi
if (( remote_rc != 0 )); then
    err "remote deploy step exited $remote_rc"
    exit 1
fi

bad_modes=$(echo "$remote_out" | grep "^MODES:" | grep -vE "^MODES:600 courtview " || true)
[[ -z "$bad_modes" ]] && ok "data file modes all 600 courtview" || { err "data files not 0600 courtview: $bad_modes"; exit 1; }

[[ "$(echo "$remote_out" | grep "^BACKUPTIMER:" | sed 's/^BACKUPTIMER://')" == "enabled" ]] \
    && ok "courtview-backup.timer enabled" || { err "courtview-backup.timer not enabled"; exit 1; }
[[ "$(echo "$remote_out" | grep "^RECOVERYTIMER:" | sed 's/^RECOVERYTIMER://')" == "enabled" ]] \
    && ok "courtview-recovery@courtview.timer enabled" || { err "courtview-recovery@courtview.timer not enabled"; exit 1; }

ok "health: authenticated /api/health answered 200 with a fresh started_at"
ok "courtview: systemd active, enabled on boot"
listener=$(echo "$remote_out" | grep "^LISTENER:" | sed 's/^LISTENER://')
main_pid=${listener##*:}
ok "courtview: :8766 owned by systemd MainPID $main_pid, user $(echo "$remote_out" | grep '^MAINUSER:' | sed 's/^MAINUSER://')"
ok "current -> $(echo "$remote_out" | grep '^CURRENT:' | sed 's/^CURRENT://')"

log "=== CourtView deploy complete ==="
