#!/bin/zsh
# Restore check for the newest pulled backup: decrypt, integrity_check, PII scan. Prints counts only.
set -euo pipefail

DEST="$HOME/Backups/courtview"
GPG=/opt/homebrew/bin/gpg
newest=$(ls -1 "$DEST"/courtview-*.db.gpg | sort | tail -1)
work=$(mktemp -d)
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

$GPG --batch --quiet --output "$work/restore.db" --decrypt "$newest"
echo "file: ${newest:t}"
echo "integrity_check: $(sqlite3 "$work/restore.db" 'PRAGMA integrity_check')"
python3 -I - "$work/restore.db" <<'PY'
import re, sqlite3, sys
path = sys.argv[1]
bad = re.compile(r"name|email|phone|receipt|firebase|player_id|user_id", re.I)
allowed = {("court_popularity", "court_name"), ("sqlite_sequence", "name")}
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
cols = [(t, col[1]) for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for col in c.execute(f'PRAGMA table_info("{t}")') if bad.search(col[1]) and (t, col[1]) not in allowed]
data = open(path, "rb").read()
email = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
mobile = re.compile(rb"(?<![0-9A-Za-z])(?:\+44 ?\(?0?\)?|0)7\d{3} ?\d{3} ?\d{3}(?![0-9A-Za-z])")
print("PII columns:", len(cols))
print("email byte matches:", len(email.findall(data)))
print("mobile byte matches:", len(mobile.findall(data)))
print("payments rows:", c.execute("SELECT COUNT(*) FROM payments").fetchone()[0])
PY
