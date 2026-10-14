"""Club config tests: derivation from courtview_app/clubs.py, the /api/clubs contract and that
the dashboard HTML carries no hardcoded club ids.

Run on the RPi (needs Flask and curl_cffi): python3 -m unittest tests.test_clubs -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-clubs-test-")
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["COURTVIEW_DATA_DIR"] = _TMP

_spec = importlib.util.spec_from_file_location("courtview_clubs_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_clubs_under_test"] = cv
_spec.loader.exec_module(cv)

from courtview_app import clubs  # noqa: E402

TOKEN = "t" * 32
PADIUM = "47d2eb0db7194a9dbd29783c3a2a82ad"
ROCKET = "788fa2c66535421aabc60fd27f941c42"
RACKETEER = "5111764d9bb14be3adbdb8e133e8bd80"


class ClubConfigTests(unittest.TestCase):
    def test_derived_values_match_previous_literals(self):
        self.assertEqual(clubs.HEATMAP_CLUBS, [{"id": PADIUM, "courts": 7}, {"id": ROCKET, "courts": 12}])
        self.assertEqual(clubs.PAYMENT_SYNC_CLUB_IDS, frozenset({PADIUM, ROCKET}))
        self.assertEqual(clubs.PAYMENT_CLUB_IDS, clubs.PAYMENT_SYNC_CLUB_IDS)
        self.assertEqual(clubs.ARCHIVE_CLUB_IDS, frozenset({RACKETEER}))
        self.assertEqual(clubs.RACKETEER_CLUB_ID, RACKETEER)

    def test_courtview_uses_the_clubs_objects(self):
        self.assertIs(cv.HEATMAP_CLUBS, clubs.HEATMAP_CLUBS)
        self.assertIs(cv.PAYMENT_SYNC_CLUB_IDS, clubs.PAYMENT_SYNC_CLUB_IDS)
        self.assertIs(cv.PAYMENT_CLUB_IDS, clubs.PAYMENT_CLUB_IDS)
        self.assertIs(cv.ARCHIVE_CLUB_IDS, clubs.ARCHIVE_CLUB_IDS)
        self.assertEqual(cv.RACKETEER_CLUB_ID, clubs.RACKETEER_CLUB_ID)

    def test_public_clubs_shape(self):
        pub = clubs.public_clubs()
        self.assertEqual([c["id"] for c in pub], [RACKETEER, PADIUM, ROCKET])
        for c in pub:
            self.assertEqual(set(c), {"id", "name", "sub", "short", "courts", "archived", "compare"})
        self.assertEqual([c["archived"] for c in pub], [True, False, False])
        self.assertEqual([c["compare"] for c in pub], [False, True, True])
        pub[0]["name"] = "mutated"
        self.assertEqual(clubs.public_clubs()[0]["name"], "Racketeer")


class ApiClubsTests(unittest.TestCase):
    def setUp(self):
        self.token_path = os.path.join(_TMP, "token")
        with open(self.token_path, "w") as fh:
            fh.write(TOKEN + "\n")
        cv.TOKEN_PATH = self.token_path
        cv._token_cache.clear()
        cv._token_cache.update({"value": None, "key": None, "state": None})
        cv._AUTH_FAILS.clear()
        cv.UPSTREAM.reset()
        self.addCleanup(cv.UPSTREAM.reset)
        self.calls = []
        p = mock.patch.object(cv.UPSTREAM, "_new_session",
                              lambda: types.SimpleNamespace(get=self._get, close=lambda: None))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cv, "_resolve_ip", lambda ip: None)
        p.start()
        self.addCleanup(p.stop)
        self.client = cv.app.test_client()
        self.anon = cv.app.test_client()
        self.client.set_cookie(cv.COOKIE_NAME, TOKEN)

    def _get(self, url, **kw):
        self.calls.append(url)
        raise AssertionError("upstream must not be called by /api/clubs")

    def test_anonymous_403(self):
        self.assertEqual(self.anon.get("/api/clubs").status_code, 403)
        self.assertEqual(self.calls, [])

    def test_authenticated_contract(self):
        r = self.client.get("/api/clubs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["Content-Security-Policy"], cv.CSP_API)
        body = json.loads(r.data)
        self.assertEqual(set(body), {"clubs"})
        self.assertEqual(len(body["clubs"]), 3)
        for c in body["clubs"]:
            self.assertEqual(set(c), {"id", "name", "sub", "short", "courts", "archived", "compare"})
        self.assertEqual([c["id"] for c in body["clubs"]], [RACKETEER, PADIUM, ROCKET])
        self.assertNotIn(b"@", r.data)
        self.assertEqual(self.calls, [])

    def test_token_query_sets_cookie(self):
        r = self.anon.get("/api/clubs?token=" + TOKEN)
        self.assertEqual(r.status_code, 200)
        self.assertIn(cv.COOKIE_NAME, r.headers.get("Set-Cookie", ""))


class HtmlHasNoClubIdsTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "courtview.html"), encoding="utf-8") as fh:
            self.html = fh.read()

    def test_no_club_ids_in_html(self):
        for c in clubs.CLUBS:
            self.assertNotIn(c["id"], self.html, c["name"])

    def test_html_loads_clubs_from_api(self):
        self.assertIn("/api/clubs", self.html)


if __name__ == "__main__":
    unittest.main()
