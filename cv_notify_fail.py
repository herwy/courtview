#!/usr/bin/env python3
import sys
import datetime

sys.path.insert(0, '/root/labs')
from notify import send_telegram

unit = sys.argv[1] if len(sys.argv) > 1 else 'courtview.service'
now = datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M UTC')

send_telegram(
    f'<b>CourtView: FAILED</b>\n\n'
    f'<i>{unit} crashed on RPi.</i>\n\n'
    f'{now}'
)
