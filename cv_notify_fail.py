#!/usr/bin/env python3
"""
cv_notify_fail.py - Telegram alerts for courtview.service (house style).

Message shape follows ~/dev/doxxnet-labs/.claude/docs/telegram-style.md
(one status dot, short first line, labelled facts, expandable logs,
Europe/London footer).

Run as root by notify-telegram@courtview.service (OnFailure= in courtview.service) and by
courtview-recovery@courtview.service (timer-driven, see below).
systemd starts OnFailure= units on every crash, including each one that
Restart= recovers from, so this script decides whether to alert:

  1. Wait WAIT_S seconds (longer than RestartSec plus start time).
  2. Still failed (`systemctl is-failed`): send a loud red "down" alert.
  3. Otherwise record the invocation in STATE_FILE. A single recovered crash
     sends nothing. LOOP_COUNT or more invocations within LOOP_WINDOW_S send
     ONE loud red "crash-looping" alert, then nothing more for
     LOOP_ALERT_COOLDOWN_S.

Either alert opens an incident (incident_since in STATE_FILE) and arms
courtview-recovery@<unit>.timer (the sandboxed service has no systemd privilege, so
the root notifier does it). The timer runs `--check-recovery` every minute: if an
incident is open and the unit has been active for RECOVERY_MIN_ACTIVE_S, one
silent green "back" message is sent and the incident is cleared. When no incident is
open and the alert outbox is empty the check disarms the timer. An alert whose send
fails is queued in the cv_telegram outbox and flushed by the next run, so it still
counts as raised.

courtview.service sets StartLimitIntervalSec=0, so systemd never gives up on
a restart loop; the invocation count is what makes a loop visible.
Ported from doxxnet-labs scripts/notify_unit_failure.py.

Usage: cv_notify_fail.py <unit>                   (the %i of the template)
       cv_notify_fail.py --check-recovery <unit>
"""
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import cv_telegram

WAIT_S = 15
SUBPROCESS_TIMEOUT_S = 10
JOURNAL_LINES = 20
MAX_CAUSE_CHARS = 80
PORT = 8766
LOOP_COUNT = 3
LOOP_WINDOW_S = 600
LOOP_ALERT_COOLDOWN_S = 3600
STATE_RETENTION_S = 3600
STATE_DIR = os.environ.get('CV_NOTIFY_STATE_DIR', '/var/lib/courtview-notify')
STATE_FILE = os.path.join(STATE_DIR, 'unit_failure_state.json')
STATE_MODE = 0o600
RECOVERY_MIN_ACTIVE_S = 60
LOG_QUOTE_LINES = 5
MAX_FACTS = 3
FIRST_LINE_MAX = 40
SECONDS_PER_MIN = 60
USEC_PER_S = 1_000_000
TZ = ZoneInfo('Europe/London')
DOTS = {'problem': '🔴', 'warning': '🟠', 'recovery': '🟢', 'info': '🔵'}
SILENT_KINDS = ('recovery', 'info')
FACT_ORDER = ('Cause', 'Since', 'Fix', 'Where')
PORT_IN_USE_MARKERS = ('Address already in use', 'Connection in use')

_UNIT_RE = re.compile(r'^[A-Za-z0-9:_.\-@]+$')


def display_name(unit):
    return 'CourtView' if unit == 'courtview' else unit


def journal_tail(unit):
    try:
        return subprocess.run(['journalctl', '-u', unit, '-n', str(JOURNAL_LINES), '--no-pager', '-o', 'cat'],
                              capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def _esc(text):
    return html.escape(text, quote=False)


def _trunc(text, limit):
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + '…'


def _hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%H:%M')


def format_alert(kind, title, *, context=None, impact=None, facts=(), logs=(), source):
    """Build a house-style Telegram HTML message. Returns (message, silent)."""
    dot = DOTS[kind]
    budget = FIRST_LINE_MAX - len(dot) - 1
    if context:
        room = budget - len(title) - len(' · ')
        if room >= 1:
            context = _trunc(context, room)
        else:
            context = None
    title = _trunc(title, budget)
    head = f'{dot} <b>{_esc(title)}</b>'
    if context:
        head += f' · {_esc(context)}'
    lines = [head]
    if impact:
        lines.append(_esc(impact))
    ordered = sorted(facts, key=lambda f: FACT_ORDER.index(f[0]))[:MAX_FACTS]
    if ordered:
        lines.append('')
        lines.extend(f'<b>{label}</b>   {value}' for label, value in ordered)
    quoted = [ln for ln in logs if ln.strip()][-LOG_QUOTE_LINES:]
    if quoted:
        lines.append('')
        lines.append('<blockquote expandable>' + '\n'.join(_esc(ln) for ln in quoted) + '</blockquote>')
    stamp = datetime.fromtimestamp(time.time(), TZ).strftime('%H:%M %Z')
    lines.append(f'<i>{stamp} · {_esc(source)}</i>')
    return '\n'.join(lines), kind in SILENT_KINDS


def cause_and_fix(unit, lines):
    """Return (cause_html, fix_html) from the last journal lines."""
    if any(m in line for line in lines for m in PORT_IN_USE_MARKERS):
        return (f'<code>port {PORT} in use</code>',
                f"<code>ss -ltnp 'sport = :{PORT}'</code>")
    fix = f'<code>journalctl -u {html.escape(unit)} -n 50</code>'
    errors = [line for line in lines if 'error' in line.lower() or 'traceback' in line.lower()]
    if errors:
        return '<code>' + _esc(_trunc(errors[-1].strip(), MAX_CAUSE_CHARS)) + '</code>', fix
    return 'unknown', fix


def down_message(unit, lines, *, source=None):
    cause, _ = cause_and_fix(unit, lines)
    return format_alert(
        'problem', f'{display_name(unit)} down',
        impact="Stopped and won't restart by itself.",
        facts=[('Cause', cause), ('Fix', f'<code>systemctl restart {html.escape(unit)}</code>')],
        logs=lines, source=source or unit)


def loop_message(unit, count, since_ts, lines, *, source=None):
    cause, fix = cause_and_fix(unit, lines)
    return format_alert(
        'problem', f'{display_name(unit)} crash-looping', context=f'{count} restarts',
        impact='Dashboard probably down. Retrying with backoff, up to 5 min apart.',
        facts=[('Cause', cause), ('Since', _hhmm(since_ts)), ('Fix', fix)],
        logs=lines, source=source or unit)


def back_message(unit, down_s, *, source=None):
    minutes = max(1, round(down_s / SECONDS_PER_MIN))
    return format_alert('recovery', f'{display_name(unit)} back', context=f'down {minutes} min',
                        source=source or unit)


def is_failed(unit):
    try:
        return subprocess.run(['systemctl', 'is-failed', '--quiet', unit],
                              capture_output=True, timeout=SUBPROCESS_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_active(unit):
    try:
        return subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                              capture_output=True, timeout=SUBPROCESS_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def active_seconds(unit):
    """Seconds the unit has been active; 0 if not active or unparsable."""
    try:
        out = subprocess.run(['systemctl', 'show', '-p', 'ActiveEnterTimestampMonotonic', '--value', unit],
                             capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S).stdout.strip()
        entered = int(out)
        if entered <= 0:
            return 0
        with open('/proc/uptime') as fh:
            uptime = float(fh.read().split()[0])
        return max(0.0, uptime - entered / USEC_PER_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        cv_telegram.atomic_write_json(STATE_FILE, state, mode=STATE_MODE)
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
        since = entry.get('incident_since')
        if not isinstance(since, (int, float)):
            since = None
        if calls or alerted is not None or since is not None:
            pruned[name] = {'calls': calls, 'alerted': alerted, 'incident_since': since}
    return pruned


def _timer(unit):
    return f'courtview-recovery@{unit}.timer'


def _systemctl(verb, unit):
    """Start/stop the recovery timer. Errors are logged, never raised."""
    try:
        subprocess.run(['systemctl', verb, _timer(unit)], capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'[cv_notify_fail] systemctl {verb} {_timer(unit)} failed: {type(exc).__name__}', file=sys.stderr)


def _send(send_telegram, msg, silent, label):
    """Send one alert and log the outcome (counted from the journal in drills)."""
    sent = send_telegram(msg, silent=silent)
    print(f'[cv_notify_fail] {label} alert {"sent" if sent else "queued"}')
    return sent


def _open_incident(entry, since):
    if entry.get('incident_since') is None:
        entry['incident_since'] = since


def check_recovery(unit, send_telegram):
    # Every start schedules its own check timer, and a crash loop leaves several
    # pending that fire together. Serialise them so only one sends the "back" message.
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(STATE_FILE + '.lock', os.O_CREAT | os.O_RDWR, STATE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _check_recovery_locked(unit, send_telegram)
    finally:
        os.close(fd)


def _check_recovery_locked(unit, send_telegram):
    now = time.time()
    state = _prune(_load_state(), now)
    entry = state.get(unit)
    if not entry or entry.get('incident_since') is None:
        if cv_telegram.outbox_count() == 0:
            _systemctl('stop', unit)
        return 0
    if not is_active(unit) or active_seconds(unit) < RECOVERY_MIN_ACTIVE_S:
        return 0
    msg, silent = back_message(unit, now - entry['incident_since'])
    sent = _send(send_telegram, msg, silent, 'green')
    # A failed send is already queued in the outbox, so the incident closes either way
    # (otherwise every retry would queue another green).
    entry['incident_since'] = None
    _save_state(state)
    return 0 if sent else 1


def main(argv):
    recovery = len(argv) == 2 and argv[0] == '--check-recovery'
    if recovery:
        unit = argv[1]
    elif len(argv) == 1:
        unit = argv[0]
    else:
        unit = ''
    if not unit or not _UNIT_RE.match(unit) or '..' in unit:
        print('usage: cv_notify_fail.py <unit> | --check-recovery <unit>', file=sys.stderr)
        return 2

    if not recovery:
        time.sleep(WAIT_S)
    send_telegram = cv_telegram.send_telegram
    cv_telegram.flush_outbox()

    if recovery:
        return check_recovery(unit, send_telegram)

    now = time.time()
    state = _prune(_load_state(), now)
    entry = state.setdefault(unit, {'calls': [], 'alerted': None, 'incident_since': None})

    if is_failed(unit):
        msg, silent = down_message(unit, journal_tail(unit))
        sent = _send(send_telegram, msg, silent, 'red down')
        window = [t for t in entry['calls'] if t > now - LOOP_WINDOW_S]
        _open_incident(entry, min(window) if window else now)
        _save_state(state)
        _systemctl('start', unit)
        return 0 if sent else 1

    entry['calls'].append(now)
    recent = [t for t in entry['calls'] if t > now - LOOP_WINDOW_S]
    in_cooldown = entry['alerted'] is not None and entry['alerted'] > now - LOOP_ALERT_COOLDOWN_S

    rc = 0
    if len(recent) >= LOOP_COUNT and not in_cooldown:
        msg, silent = loop_message(unit, len(recent), min(recent), journal_tail(unit))
        entry['alerted'] = now
        _open_incident(entry, min(recent))
        _systemctl('start', unit)
        if not _send(send_telegram, msg, silent, 'red loop'):
            rc = 1
    else:
        print('recovered, no alert')
    _save_state(state)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
