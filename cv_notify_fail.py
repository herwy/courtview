#!/usr/bin/env python3
"""
cv_notify_fail.py - Telegram alert for courtview.service failures.

Run by notify-telegram@courtview.service (OnFailure= in courtview.service).
systemd starts OnFailure= units on every crash, including each one that
Restart= recovers from, so this script decides whether to alert:

  1. Wait WAIT_S seconds (longer than RestartSec plus start time).
  2. Still failed (`systemctl is-failed`): send a FAILED alert.
  3. Otherwise record the invocation in STATE_FILE. A single recovered crash
     sends nothing. LOOP_COUNT or more invocations within LOOP_WINDOW_S send
     ONE crash-looping alert, then nothing more for LOOP_ALERT_COOLDOWN_S.

courtview.service sets StartLimitIntervalSec=0, so systemd never gives up on
a restart loop; the invocation count is what makes a loop visible.
Ported from doxxnet-labs scripts/notify_unit_failure.py.

Usage: cv_notify_fail.py <unit>   (the %i of the template, e.g. courtview)
"""
import html
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, '/root/labs')

WAIT_S = 15
SUBPROCESS_TIMEOUT_S = 10
JOURNAL_LINES = 20
MAX_CAUSE_CHARS = 200
PORT = 8766
LOOP_COUNT = 3
LOOP_WINDOW_S = 600
LOOP_ALERT_COOLDOWN_S = 3600
STATE_RETENTION_S = 3600
STATE_FILE = '/root/projects/courtview/unit_failure_state.json'
STATE_MODE = 0o600

_UNIT_RE = re.compile(r'^[A-Za-z0-9:_.\-@]+$')


def display_name(unit):
    return 'CourtView' if unit == 'courtview' else unit


def journal_tail(unit):
    try:
        return subprocess.run(['journalctl', '-u', unit, '-n', str(JOURNAL_LINES), '--no-pager', '-o', 'cat'],
                              capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def likely_cause(lines):
    """Translate the last journal lines into one plain-English sentence."""
    if any('Address already in use' in line for line in lines):
        return f'Another program is holding port {PORT}, so CourtView cannot start.'
    errors = [line for line in lines if 'error' in line.lower() or 'traceback' in line.lower()]
    if errors:
        return 'An error on startup: <code>' + html.escape(errors[-1].strip()[:MAX_CAUSE_CHARS]) + '</code>'
    return 'Unknown.'


def failed_message(unit, lines):
    name = html.escape(display_name(unit))
    return (
        f'<b>{name} is down</b>\n\n'
        f'<b>What\'s happening:</b> {name} has stopped and will not restart by itself.\n'
        f'<b>Likely cause:</b> {likely_cause(lines)}\n'
        f'<b>To fix:</b> <code>systemctl restart {html.escape(unit)}</code>\n\n'
        f'<i>Logs: journalctl -u {html.escape(unit)} -n 50</i>'
    )


def loop_message(unit, count, lines):
    name = html.escape(display_name(unit))
    return (
        f'<b>{name} keeps crashing</b>\n\n'
        f'<b>What\'s happening:</b> {name} crashed {count} times in the last 10 minutes. '
        f'The dashboard is probably down.\n'
        f'<b>Likely cause:</b> {likely_cause(lines)}\n'
        f'<b>What happens next:</b> It keeps restarting by itself, waiting longer each time '
        f'(up to 5 minutes). No more alerts about this for 1 hour.\n\n'
        f'<i>Logs: journalctl -u {html.escape(unit)} -n 50</i>'
    )


def is_failed(unit):
    try:
        return subprocess.run(['systemctl', 'is-failed', '--quiet', unit],
                              capture_output=True, timeout=SUBPROCESS_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    from atomic_io import atomic_write_json
    try:
        atomic_write_json(STATE_FILE, state, mode=STATE_MODE)
    except OSError as exc:
        print(f'[cv_notify_fail] could not write state: {exc}', file=sys.stderr)


def _prune(state, now):
    cutoff = now - STATE_RETENTION_S
    pruned = {}
    for name, entry in state.items():
        if not isinstance(entry, dict):
            continue
        calls = [t for t in entry.get('calls', []) if isinstance(t, (int, float)) and t > cutoff]
        alerted = entry.get('alerted')
        if not isinstance(alerted, (int, float)) or alerted <= cutoff:
            alerted = None
        if calls or alerted is not None:
            pruned[name] = {'calls': calls, 'alerted': alerted}
    return pruned


def main(argv):
    unit = argv[0] if len(argv) == 1 else 'courtview'
    if not _UNIT_RE.match(unit) or '..' in unit:
        print('usage: cv_notify_fail.py <unit>', file=sys.stderr)
        return 2

    time.sleep(WAIT_S)
    from notify import send_telegram

    if is_failed(unit):
        send_telegram(failed_message(unit, journal_tail(unit)))
        return 0

    now = time.time()
    state = _prune(_load_state(), now)
    entry = state.setdefault(unit, {'calls': [], 'alerted': None})
    entry['calls'].append(now)
    recent = [t for t in entry['calls'] if t > now - LOOP_WINDOW_S]
    in_cooldown = entry['alerted'] is not None and entry['alerted'] > now - LOOP_ALERT_COOLDOWN_S

    rc = 0
    if len(recent) >= LOOP_COUNT and not in_cooldown:
        if send_telegram(loop_message(unit, len(recent), journal_tail(unit))):
            entry['alerted'] = now
        else:
            rc = 1
    else:
        print('recovered, no alert')
    _save_state(state)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
