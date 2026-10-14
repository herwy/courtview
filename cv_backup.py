#!/usr/bin/env python3
"""cv_backup.py - nightly encrypted backup of the CourtView cache DB (stdlib only; runs as the courtview user).

sqlite backup API -> integrity_check -> gpg public-key encryption -> rotate (keep 7).
Only the public key lives on the RPi, so a stolen backup is unreadable without the Mac keyring.
The HMAC secret is deliberately NOT included. The plaintext copy exists only in a 0700 dir and is
always removed. Exit status is non-zero on any failure.
"""
import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("COURTVIEW_DATA_DIR", "/var/lib/courtview")
DB_PATH = os.environ.get("COURTVIEW_DB_PATH", os.path.join(DATA_DIR, "courtview_cache.db"))
BACKUP_DIR = os.environ.get("COURTVIEW_BACKUP_DIR", os.path.join(DATA_DIR, "backups"))
PUBKEY = os.environ.get("COURTVIEW_BACKUP_PUBKEY",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "courtview-backup.pub.asc"))
KEEP = 7


def main() -> int:
    os.umask(0o077)
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now(ZoneInfo("Europe/London")).strftime("%Y%m%d")
    final = os.path.join(BACKUP_DIR, f"courtview-{stamp}.db.gpg")
    fd, plain = tempfile.mkstemp(dir=BACKUP_DIR, prefix=".plain-", suffix=".db")
    os.close(fd)
    homedir = tempfile.mkdtemp(prefix="cv-gpg-")
    enc_tmp = final + ".tmp"
    try:
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(plain)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if result != "ok":
            print("backup aborted: integrity_check failed", file=sys.stderr)
            return 1
        subprocess.run(
            ["gpg", "--batch", "--yes", "--homedir", homedir, "--trust-model", "always",
             "--recipient-file", PUBKEY, "--output", enc_tmp, "--encrypt", plain],
            check=True, capture_output=True, timeout=600)
        os.chmod(enc_tmp, 0o600)
        os.replace(enc_tmp, final)
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "courtview-*.db.gpg")))
        for old in files[:-KEEP]:
            os.unlink(old)
        kept = len(glob.glob(os.path.join(BACKUP_DIR, "courtview-*.db.gpg")))
        print(f"backup ok: courtview-{stamp}.db.gpg {os.path.getsize(final)} bytes, kept {kept}")
        return 0
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"backup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        for path in (plain, enc_tmp):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        shutil.rmtree(homedir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
