#!/usr/bin/env python3
import sys
import time
import datetime
import subprocess

unit = sys.argv[1] if len(sys.argv) > 1 else 'courtview.service'

# Wait for systemd to attempt a restart (RestartSec=5 + startup buffer).
# OnFailure= fires on every crash, not just permanent failures. If the service
# recovers on its own we don't need to page anyone.
time.sleep(12)

result = subprocess.run(['systemctl', 'is-failed', unit], capture_output=True)
if result.returncode != 0:
    sys.exit(0)  # recovered - no alert needed

sys.path.insert(0, '/root/labs')
from notify import send_telegram

now = datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M UTC')
send_telegram(
    f'<b>CourtView: FAILED</b>\n\n'
    f'<i>{unit} is down and requires manual intervention.</i>\n\n'
    f'{now}'
)
