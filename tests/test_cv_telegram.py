"""Tests for cv_telegram.py: retry, outbox, token lookup, log hygiene (no network)."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import cv_telegram as tg  # noqa: E402

SECRET = "123456:SECRET-TOKEN"
BODY = "<b>hello secret-body</b>"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cv-tg-")
        self.token = os.path.join(self.dir, "tok")
        with open(self.token, "w") as fh:
            fh.write(SECRET + "\n")
        self.requests = []
        self.outcomes = []  # per _do_request call: dict result or Exception
        env = {"CV_TELEGRAM_TOKEN_PATH": self.token}
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(tg, "STATE_DIR", os.path.join(self.dir, "state")),
                  mock.patch.object(tg, "_do_request", self._fake_request),
                  mock.patch.object(tg.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def _fake_request(self, req, device=None):
        self.requests.append((json.loads(req.data), device))
        out = self.outcomes.pop(0) if self.outcomes else {"ok": True}
        if isinstance(out, Exception):
            raise out
        return out

    def send(self, *a, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = tg.send_telegram(*a, **kw)
        return result, err.getvalue()


class SendTests(Base):
    def test_silent_sets_disable_notification(self):
        self.assertTrue(self.send(BODY, silent=True)[0])
        self.assertIs(self.requests[0][0]["disable_notification"], True)
        self.assertTrue(self.send(BODY)[0])
        self.assertIs(self.requests[1][0]["disable_notification"], False)

    def test_first_attempt_failure_is_retried(self):
        self.outcomes = [OSError("net"), OSError("net on eth0"), {"ok": True}]
        result, _ = self.send(BODY)
        self.assertTrue(result)
        self.assertEqual(len(self.requests), 3)

    def test_total_failure_logs_queues_and_hides_secrets(self):
        self.outcomes = [OSError("x")] * 4
        result, err = self.send(BODY, silent=True)
        self.assertFalse(result)
        self.assertIn("[cv_telegram] send failed", err)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("secret-body", err)
        path = os.path.join(tg.STATE_DIR, "alert_outbox.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            queued = json.load(fh)
        self.assertEqual([(e["message"], e["silent"]) for e in queued], [(BODY, True)])
        self.assertEqual(tg.outbox_count(), 1)

    def test_missing_token_returns_false_and_logs(self):
        os.environ["CV_TELEGRAM_TOKEN_PATH"] = os.path.join(self.dir, "missing")
        with mock.patch.object(tg, "LEGACY_TOKEN_PATH", os.path.join(self.dir, "nolegacy")):
            result, err = self.send(BODY)
        self.assertFalse(result)
        self.assertIn("[cv_telegram] send failed", err)
        self.assertEqual(self.requests, [])

    def test_token_from_credentials_directory(self):
        os.environ.pop("CV_TELEGRAM_TOKEN_PATH")
        creds = os.path.join(self.dir, "creds")
        os.makedirs(creds)
        with open(os.path.join(creds, "telegram_token"), "w") as fh:
            fh.write("cred-token")
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": creds}):
            self.assertEqual(tg._token(), "cred-token")

    def test_token_falls_back_to_legacy_path(self):
        os.environ.pop("CV_TELEGRAM_TOKEN_PATH")
        legacy = os.path.join(self.dir, "legacy")
        with open(legacy, "w") as fh:
            fh.write("legacy-token")
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": os.path.join(self.dir, "nocreds")}), \
                mock.patch.object(tg, "LEGACY_TOKEN_PATH", legacy):
            self.assertEqual(tg._token(), "legacy-token")

    def test_permanent_http_error_is_not_queued(self):
        err400 = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))
        self.outcomes = [err400]
        result, err = self.send(BODY)
        self.assertFalse(result)
        self.assertEqual(tg.outbox_count(), 0)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(SECRET, err)


class OutboxTests(Base):
    def test_flush_resends_and_empties(self):
        self.outcomes = [OSError("x")] * 4
        self.send("first", silent=False)
        self.outcomes = [OSError("x")] * 4
        self.send("second", silent=True)
        self.requests.clear()
        self.assertEqual(tg.flush_outbox(), 2)
        self.assertEqual([r[0]["text"] for r in self.requests], ["first", "second"])
        self.assertEqual(tg.outbox_count(), 0)

    def test_flush_keeps_failures(self):
        self.outcomes = [OSError("x")] * 4
        self.send("keep")
        self.outcomes = [OSError("x")] * 4
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tg.flush_outbox(), 0)
        self.assertEqual(tg.outbox_count(), 1)

    def test_flush_drops_expired(self):
        os.makedirs(tg.STATE_DIR, mode=0o700)
        old = {"message": "old", "silent": False, "ts": 1.0}
        with open(os.path.join(tg.STATE_DIR, "alert_outbox.json"), "w") as fh:
            json.dump([old], fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(tg.flush_outbox(), 0)
        self.assertEqual(tg.outbox_count(), 0)
        self.assertEqual(self.requests, [])
        self.assertIn("dropped 1", err.getvalue())

    def test_flush_on_empty_outbox_is_noop(self):
        self.assertEqual(tg.flush_outbox(), 0)
        self.assertEqual(self.requests, [])


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_json_mode_and_content(self):
        d = tempfile.mkdtemp(prefix="cv-aw-")
        path = os.path.join(d, "s.json")
        tg.atomic_write_json(path, {"a": 1}, mode=0o600)
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(d), ["s.json"])


if __name__ == "__main__":
    unittest.main()
