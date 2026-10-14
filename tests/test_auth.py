"""Auth hardening tests: last-good token reader, GET/HEAD-only surface, no CORS."""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-auth-test-")
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")

_spec = importlib.util.spec_from_file_location("courtview_auth_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_auth_under_test"] = cv
_spec.loader.exec_module(cv)

PADIUM = "47d2eb0db7194a9dbd29783c3a2a82ad"
TOK_A = "a" * 32
TOK_B = "b" * 32
EVIL = {"Origin": "https://evil.example"}
API = "/api/club/?club_id=" + PADIUM


class _FakeResp:
    status_code = 200
    content = b'{"ok": true}'
    headers = {"content-type": "application/json"}


class AuthBase(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_TMP, "token")
        self._clock = 1_000_000_000_000_000_000
        cv.TOKEN_PATH = self.path
        cv._token_cache.clear()
        cv._token_cache.update({"value": None, "key": None, "state": None})
        cv._AUTH_FAILS.clear()
        cv._dashboard_bytes = b"<html>dash</html>"
        self.client = cv.app.test_client()
        self.urls = []
        self.get_patch = mock.patch.object(cv.cffi_requests, "get", side_effect=self._fake_get)
        self.post_patch = mock.patch.object(cv.cffi_requests, "post")
        self.get_patch.start()
        self.post = self.post_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.addCleanup(self.post_patch.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _fake_get(self, url, **kw):
        self.urls.append(url)
        return _FakeResp()

    def write(self, text):
        """Write the token file and give it a distinct, deterministic mtime."""
        with open(self.path, "w") as fh:
            fh.write(text)
        self._clock += 1_000_000_000
        os.utime(self.path, ns=(self._clock, self._clock))

    def cookie(self, tok=TOK_A):
        self.client.set_cookie(cv.COOKIE_NAME, tok)


class TestReader(AuthBase):
    def test_valid_file(self):
        self.write(TOK_A + "\n")
        self.assertEqual(cv._read_token(), TOK_A)

    def test_empty_keeps_last_good_and_retries(self):
        self.write(TOK_A)
        self.assertEqual(cv._read_token(), TOK_A)
        good_key = cv._token_cache["key"]
        self.write("")
        self.assertEqual(cv._read_token(), TOK_A)
        self.assertEqual(cv._token_cache["key"], good_key)  # empty state not recorded
        self.write(TOK_A)
        self.assertEqual(cv._read_token(), TOK_A)

    def test_oserror_on_open_keeps_last_good(self):
        self.write(TOK_A)
        self.assertEqual(cv._read_token(), TOK_A)
        self.write(TOK_B)
        real_open = open

        def boom(path, *a, **k):
            if path == self.path:
                raise OSError("denied")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=boom):
            self.assertEqual(cv._read_token(), TOK_A)
        self.assertEqual(cv._read_token(), TOK_B)  # retried after the fault clears

    def test_stat_failure_keeps_last_good(self):
        self.write(TOK_A)
        self.assertEqual(cv._read_token(), TOK_A)
        os.remove(self.path)
        self.assertEqual(cv._read_token(), TOK_A)

    def test_new_token_replaces_old(self):
        self.write(TOK_A)
        self.cookie(TOK_A)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.write(TOK_B)
        self.assertEqual(self.client.get("/").status_code, 403)
        self.cookie(TOK_B)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_missing_file_at_startup_denies(self):
        self.assertEqual(cv._read_token(), "")
        self.cookie("")
        self.assertEqual(self.client.get("/").status_code, 403)
        self.cookie(TOK_A)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_empty_file_at_startup_denies(self):
        self.write("")
        self.assertEqual(cv._read_token(), "")
        self.cookie(TOK_A)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_logs_transitions_not_requests_and_never_token(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.write(TOK_A)
            cv._read_token()
            self.write("")
            for _ in range(5):
                cv._read_token()
            self.write(TOK_B)
            cv._read_token()
            cv._read_token()
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("[token]")]
        self.assertEqual(len(lines), 3, lines)  # loaded, degraded, loaded
        out = buf.getvalue()
        self.assertNotIn(TOK_A, out)
        self.assertNotIn(TOK_B, out)


class TestSurface(AuthBase):
    def setUp(self):
        super().setUp()
        self.write(TOK_A)
        self.cookie()

    def test_non_get_methods_rejected(self):
        for method in ("post", "put", "delete", "patch"):
            r = getattr(self.client, method)("/api/club/?club_id=" + PADIUM)
            self.assertIn(r.status_code, (403, 405), method)
        self.post.assert_not_called()
        self.assertEqual(self.urls, [])

    def test_options_has_no_cors(self):
        r = self.client.options("/api/club/", headers=EVIL)
        self.assertIn(r.status_code, (403, 405))
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_query_token_ignored_on_post(self):
        self.client.delete_cookie(cv.COOKIE_NAME)
        r = self.client.post("/api/club/?token=" + TOK_A)
        self.assertIn(r.status_code, (403, 405))
        self.assertIsNone(r.headers.get("Set-Cookie"))

    def test_no_access_control_headers(self):
        for url in ("/", API, "/access"):
            r = self.client.get(url, headers=EVIL)
            names = [h.lower() for h in r.headers.keys()]
            self.assertFalse([n for n in names if n.startswith("access-control-")], url)

    def test_cookie_get_ok(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_query_token_get_redirects_with_cookie(self):
        self.client.delete_cookie(cv.COOKIE_NAME)
        r = self.client.get("/?token=" + TOK_A)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/"))
        self.assertIn(cv.COOKIE_NAME, r.headers.get("Set-Cookie", ""))

    def test_api_query_token_get(self):
        self.client.delete_cookie(cv.COOKIE_NAME)
        r = self.client.get(API + "&token=" + TOK_A)
        self.assertEqual(r.status_code, 200)
        self.assertIn(cv.COOKIE_NAME, r.headers.get("Set-Cookie", ""))
        self.assertEqual(len(self.urls), 1)
        self.assertNotIn("token=", self.urls[0])

    def test_head_allowed(self):
        self.assertEqual(self.client.head("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
