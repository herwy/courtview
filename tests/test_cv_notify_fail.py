"""Stub tests for cv_notify_fail.py (no systemctl, journalctl, Telegram or /root/labs needed)."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENT = []


def _stub_send(msg, *args, silent=False, **kw):
    SENT.append((msg, silent))
    return True


def _stub_atomic(path, obj, mode=0o600):
    with open(path, 'w') as fh:
        json.dump(obj, fh)
    os.chmod(path, mode)


sys.modules['notify'] = types.SimpleNamespace(send_telegram=_stub_send)
sys.modules['atomic_io'] = types.SimpleNamespace(atomic_write_json=_stub_atomic)

_spec = importlib.util.spec_from_file_location('cv_notify_fail', os.path.join(REPO, 'cv_notify_fail.py'))
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

DOTS = '🔴🟠🟢🔵'
T_BST = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc).timestamp()
T_GMT = datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc).timestamp()


def first_line_visible(msg):
    return re.sub(r'<[^>]+>', '', msg.split('\n')[0])


class FormatTests(unittest.TestCase):
    def test_dots_and_silence(self):
        for kind, dot, silent in (('problem', '🔴', False), ('warning', '🟠', False),
                                  ('recovery', '🟢', True), ('info', '🔵', True)):
            msg, s = cv.format_alert(kind, 'X', source='x')
            self.assertTrue(msg.startswith(dot + ' '))
            self.assertEqual(s, silent)
            self.assertEqual(sum(msg.count(d) for d in DOTS), 1)

    def test_first_line_cap(self):
        for msg, _ in (cv.down_message('courtview', []),
                       cv.loop_message('courtview', 999, T_BST, []),
                       cv.back_message('courtview', 1440 * 60),
                       cv.down_message('a' * 80, [])):
            self.assertLessEqual(len(first_line_visible(msg)), cv.FIRST_LINE_MAX, msg)

    def test_truncation_marker(self):
        msg, _ = cv.format_alert('problem', 'T' * 100, source='x')
        self.assertIn('…', first_line_visible(msg))

    def test_fact_order_and_cap(self):
        facts = [('Where', 'w'), ('Fix', 'f'), ('Since', 's'), ('Cause', 'c')]
        msg, _ = cv.format_alert('problem', 'X', facts=facts, source='x')
        lines = [ln for ln in msg.split('\n') if ln.startswith('<b>') and ln[3:].split('</b>')[0] != 'X']
        labels = [re.match(r'<b>(\w+)</b>', ln).group(1) for ln in lines]
        self.assertEqual(labels, ['Cause', 'Since', 'Fix'])

    def test_logs_last_five_escaped(self):
        logs = [f'line{i}' for i in range(18)] + ['<script>', 'a & b']
        with mock.patch.object(cv.time, 'time', return_value=T_BST):
            msg, _ = cv.format_alert('problem', 'X', logs=logs, source='x')
        quote = msg.split('<blockquote expandable>')[1].split('</blockquote>')[0]
        self.assertEqual(len(quote.splitlines()), 5)
        self.assertNotIn('<script>', msg)
        self.assertIn('&lt;script&gt;', quote)
        self.assertIn('a &amp; b', quote)
        self.assertNotIn('line14', quote)

    def test_footer_bst_and_gmt(self):
        with mock.patch.object(cv.time, 'time', return_value=T_BST):
            msg, _ = cv.format_alert('problem', 'X', source='courtview')
        self.assertTrue(msg.endswith('<i>13:00 BST · courtview</i>'))
        with mock.patch.object(cv.time, 'time', return_value=T_GMT):
            msg, _ = cv.format_alert('problem', 'X', source='courtview')
        self.assertTrue(msg.endswith('<i>12:00 GMT · courtview</i>'))

    def test_cause_and_fix(self):
        cause, fix = cv.cause_and_fix('courtview', ['x', 'OSError: Address already in use'])
        self.assertEqual(cause, '<code>port 8766 in use</code>')
        self.assertEqual(fix, "<code>ss -ltnp 'sport = :8766'</code>")
        cause, _ = cv.cause_and_fix('courtview', ['[ERROR] Connection in use: (127.0.0.1, 8766)'])
        self.assertIn('port 8766 in use', cause)
        cause, fix = cv.cause_and_fix('courtview', ['ok', 'ModuleNotFoundError: <curl_cffi> ' + 'z' * 200 + ' error'])
        self.assertTrue(cause.startswith('<code>'))
        self.assertIn('&lt;curl_cffi&gt;', cause)
        self.assertIn('…', cause)
        self.assertEqual(fix, '<code>journalctl -u courtview -n 50</code>')
        cause, fix = cv.cause_and_fix('courtview', ['all fine'])
        self.assertEqual(cause, 'unknown')
        self.assertEqual(fix, '<code>journalctl -u courtview -n 50</code>')

    def test_old_wording_gone(self):
        with open(os.path.join(REPO, 'cv_notify_fail.py')) as fh:
            src = fh.read()
        for s in ("What's" + ' happening', 'Likely' + ' cause', 'keeps' + ' crashing', 'is' + ' down'):
            self.assertNotIn(s, src)


class MainTests(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        self.clock = [T_BST]
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)
        self.patches = [
            mock.patch.object(cv, 'STATE_FILE', self.path),
            mock.patch.object(cv.time, 'sleep', lambda s: None),
            mock.patch.object(cv.time, 'time', lambda: self.clock[0]),
            mock.patch.object(cv, 'journal_tail', lambda u: ['boom error']),
            mock.patch.object(cv, 'is_failed', lambda u: self.failed),
            mock.patch.object(cv, 'is_active', lambda u: self.active),
            mock.patch.object(cv, 'active_seconds', lambda u: self.uptime),
        ]
        self.failed = False
        self.active = True
        self.uptime = 100
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if os.path.exists(self.path):
            os.remove(self.path)

    def state(self):
        with open(self.path) as fh:
            return json.load(fh)

    def test_loop_flow(self):
        start = self.clock[0]
        self.assertEqual(cv.main(['courtview']), 0)
        self.clock[0] += 60
        cv.main(['courtview'])
        self.assertEqual(SENT, [])
        self.clock[0] += 60
        cv.main(['courtview'])
        self.assertEqual(len(SENT), 1)
        msg, silent = SENT[0]
        self.assertFalse(silent)
        self.assertIn('crash-looping', msg)
        self.assertEqual(self.state()['courtview']['incident_since'], start)
        self.clock[0] += 60
        cv.main(['courtview'])
        self.assertEqual(len(SENT), 1)
        self.assertEqual(self.state()['courtview']['incident_since'], start)

    def test_failed_path(self):
        self.failed = True
        cv.main(['courtview'])
        self.assertEqual(len(SENT), 1)
        self.assertIn('CourtView down', SENT[0][0])
        self.assertFalse(SENT[0][1])
        self.assertEqual(self.state()['courtview']['incident_since'], T_BST)

    def test_failed_keeps_earlier_incident(self):
        with open(self.path, 'w') as fh:
            json.dump({'courtview': {'calls': [], 'alerted': None, 'incident_since': T_BST - 100}}, fh)
        self.failed = True
        cv.main(['courtview'])
        self.assertEqual(self.state()['courtview']['incident_since'], T_BST - 100)

    def test_recovery_flow(self):
        self.assertEqual(cv.main(['--check-recovery', 'courtview']), 0)
        self.assertEqual(SENT, [])
        with open(self.path, 'w') as fh:
            json.dump({'courtview': {'calls': [], 'alerted': None, 'incident_since': T_BST - 43 * 60}}, fh)
        self.active = False
        cv.main(['--check-recovery', 'courtview'])
        self.assertEqual(SENT, [])
        self.active, self.uptime = True, 30
        cv.main(['--check-recovery', 'courtview'])
        self.assertEqual(SENT, [])
        self.assertIsNotNone(self.state()['courtview']['incident_since'])
        self.uptime = 60
        self.assertEqual(cv.main(['--check-recovery', 'courtview']), 0)
        self.assertEqual(len(SENT), 1)
        msg, silent = SENT[0]
        self.assertTrue(silent)
        self.assertIn('CourtView back</b> · down 43 min', msg)
        self.assertIsNone(self.state().get('courtview', {}).get('incident_since'))
        cv.main(['--check-recovery', 'courtview'])
        self.assertEqual(len(SENT), 1)

    def test_bad_unit(self):
        self.assertEqual(cv.main(['--check-recovery', '../x']), 2)
        self.assertEqual(cv.main(['bad unit']), 2)

    def test_prune_keeps_incident(self):
        now = T_BST
        out = cv._prune({'u': {'calls': [], 'alerted': None, 'incident_since': now - 99999}}, now)
        self.assertEqual(out['u']['incident_since'], now - 99999)
        out = cv._prune({'u': {'calls': [now - 5], 'alerted': None, 'incident_since': 5.0}}, now)
        self.assertEqual(out['u']['incident_since'], 5.0)
        self.assertEqual(cv._prune({'u': {'calls': [], 'alerted': None}}, now), {})


if __name__ == '__main__':
    unittest.main()
