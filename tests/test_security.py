"""Security hygiene tests: route-auth coverage, static-route traversal, body cap, response headers
and CSP, JSON-only relay, access labels, access page title, access.log rotation.

Run on the RPi (needs Flask and curl_cffi): python3 -m unittest tests.test_security -v
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="cv-sec-test-")
os.environ["COURTVIEW_SKIP_STARTUP"] = "1"
os.environ["COURTVIEW_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["COURTVIEW_DATA_DIR"] = _TMP

_spec = importlib.util.spec_from_file_location("courtview_sec_under_test", os.path.join(REPO, "courtview.py"))
cv = importlib.util.module_from_spec(_spec)
sys.modules["courtview_sec_under_test"] = cv
_spec.loader.exec_module(cv)

TOKEN = "t" * 32
PADIUM = cv.HEATMAP_CLUBS[0]["id"]
RACK = cv.RACKETEER_CLUB_ID

# Every route must be token-gated. Adding a route without updating this set fails the test.
PUBLIC_ENDPOINTS = frozenset()
EXPECTED_ENDPOINTS = frozenset({
    "index", "static_asset", "access_page", "access_js", "dashboard_css", "access_data", "proxy",
    "api_heatmap", "api_court_popularity", "api_month", "api_availability_fresh",
    "api_membership_members", "api_club_info", "api_booked_hours", "api_activity_summary",
    "api_day_activities", "api_revenue_summary", "api_payment_history", "api_payment_leaderboard",
    "api_archive_snapshots", "api_archive_heatmap", "api_archive_club_info", "api_archive_financial",
    "api_coach_stats", "api_health", "api_clubs",
})
SEC_HEADERS = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


class _Resp:
    def __init__(self, body=b'{"ok": true}', code=200, ct="application/json"):
        self.status_code = code
        self.content = body
        self.headers = {"content-type": ct}

    def json(self):
        return json.loads(self.content)


class SecBase(unittest.TestCase):
    def setUp(self):
        self.token_path = os.path.join(_TMP, "token")
        with open(self.token_path, "w") as fh:
            fh.write(TOKEN + "\n")
        cv.TOKEN_PATH = self.token_path
        cv._token_cache.clear()
        cv._token_cache.update({"value": None, "key": None, "state": None})
        cv._AUTH_FAILS.clear()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cv.DB_PATH + ext)
            except FileNotFoundError:
                pass
        cv.init_db()
        cv._load_dashboard()
        self.upstream = _Resp()
        self.calls = []
        cv.UPSTREAM.reset()
        self.addCleanup(cv.UPSTREAM.reset)
        p = mock.patch.object(cv.UPSTREAM, "_new_session",
                              lambda: types.SimpleNamespace(get=self._get, close=lambda: None))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cv, "_resolve_ip", lambda ip: None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cv.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.client = cv.app.test_client()
        self.anon = cv.app.test_client()
        self.client.set_cookie(cv.COOKIE_NAME, TOKEN)

    def _get(self, url, **kw):
        self.calls.append(url)
        if isinstance(self.upstream, Exception):
            raise self.upstream
        return self.upstream


class RouteAuthCoverageTests(SecBase):
    def sample_urls(self):
        urls = {}
        for rule in cv.app.url_map.iter_rules():
            url = rule.rule.replace("<path:filename>", "x.js").replace("<path:p>", "club/")
            urls.setdefault(rule.endpoint, []).append(url)
        return urls

    def test_endpoint_set_is_exact(self):
        self.assertEqual({r.endpoint for r in cv.app.url_map.iter_rules()}, EXPECTED_ENDPOINTS)

    def test_flask_builtin_static_route_removed(self):
        self.assertNotIn("static", {r.endpoint for r in cv.app.url_map.iter_rules()})

    def test_every_route_403_without_token(self):
        for endpoint, urls in self.sample_urls().items():
            if endpoint in PUBLIC_ENDPOINTS:
                continue
            for url in urls:
                for method in ("get", "head"):
                    cv._AUTH_FAILS.clear()  # the limiter would answer 429 on /access after 10 misses
                    r = getattr(self.anon, method)(url)
                    self.assertEqual(r.status_code, 403, f"{method} {url} ({endpoint})")
        self.assertEqual(self.calls, [])

    def test_wrong_token_403(self):
        c = cv.app.test_client()
        c.set_cookie(cv.COOKIE_NAME, "x" * 32)
        self.assertEqual(c.get("/api/health").status_code, 403)

    def test_unmatched_url_is_gated(self):
        self.assertEqual(self.anon.get("/x", environ_overrides={"PATH_INFO": "//etc/x.js"}).status_code, 403)
        self.assertEqual(self.client.get("/x", environ_overrides={"PATH_INFO": "//etc/x.js"}).status_code, 404)


class TraversalTests(SecBase):
    URLS = ["/..%2f..%2fetc%2fpasswd.js", "/%2e%2e/%2e%2e/x.js", "/a/../../x.js", "/..\\x.js",
            "/%5c..%5cx.js", "//etc/x.js", "/%252e%252e/x.js", "/../cv_sentinel_outside.js",
            "/..%2fcv_sentinel_outside.js", "/%2e%2e%2fcv_sentinel_outside.js", "/etc/passwd"]

    def setUp(self):
        super().setUp()
        self.sentinel = os.path.join(os.path.dirname(REPO), "cv_sentinel_outside.js")
        with open(self.sentinel, "w") as fh:
            fh.write("SENTINEL-OUTSIDE-APP-DIR")
        self.addCleanup(os.remove, self.sentinel)

    @staticmethod
    def get_raw(client, url):
        """The test client parses '//host/path' as a netloc, so pass such paths as PATH_INFO."""
        if url.startswith("//"):
            return client.get("/x", environ_overrides={"PATH_INFO": url})
        return client.get(url)

    def test_never_escapes_with_token(self):
        for url in self.URLS:
            r = self.get_raw(self.client, url)
            self.assertIn(r.status_code, (400, 404), url)
            self.assertNotIn(b"SENTINEL-OUTSIDE", r.get_data(), url)

    def test_403_without_token(self):
        for url in self.URLS:
            cv._AUTH_FAILS.clear()
            self.assertEqual(self.get_raw(self.anon, url).status_code, 403, url)

    def test_raw_uri_encoded_dots_rejected(self):
        r = self.client.get("/x.js", environ_overrides={"RAW_URI": "/%2E%2e/x.js"})
        self.assertEqual(r.status_code, 404)

    def test_legit_asset_still_served(self):
        name = "cv_sec_asset.css"
        path = os.path.join(REPO, name)
        with open(path, "w") as fh:
            fh.write("body{}")
        self.addCleanup(os.remove, path)
        r = self.client.get("/" + name)
        self.assertEqual((r.status_code, r.get_data()), (200, b"body{}"))
        self.assertEqual(r.mimetype, "text/css")

    def test_unknown_extension_404_with_token_403_without(self):
        self.assertEqual(self.client.get("/nope.php").status_code, 404)
        self.assertEqual(self.anon.get("/nope.php").status_code, 403)


class BodyCapTests(SecBase):
    def test_max_content_length(self):
        self.assertLessEqual(cv.app.config["MAX_CONTENT_LENGTH"], 1048576)

    def test_big_post_never_500(self):
        for client in (self.client, self.anon):
            r = client.post("/api/health", data=b"x" * (2 * 1024 * 1024))
            self.assertIn(r.status_code, (403, 413))


class HeaderTests(SecBase):
    def check_headers(self, r, label):
        for k, v in SEC_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, f"{label} {k}")
        self.assertTrue(r.headers.get("Content-Security-Policy"), label)  # 403 keeps _forbidden's own

    def test_headers_on_every_status(self):
        cases = {
            "200 html": self.client.get("/"),
            "200 json": self.client.get("/api/health"),
            "302": self.anon.get("/?token=" + TOKEN),
            "403": cv.app.test_client().get("/"),
            "404": self.client.get("/nope.php"),
            "410": self.client.get("/api/booked-hours?club_id=x"),
            "410 archived": self.client.get(f"/api/club/?club_id={RACK}"),
            "403 post": self.client.post("/api/health"),
            "405 head-only": self.client.open("/api/health", method="OPTIONS"),
        }
        self.assertEqual([cases[k].status_code for k in ("200 html", "200 json", "302", "403", "404", "410")],
                         [200, 200, 302, 403, 404, 410])
        for label, r in cases.items():
            self.check_headers(r, label)

    def test_500_has_headers(self):
        app = cv.app
        app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch.object(cv, "_job_states", side_effect=RuntimeError("boom")):
            r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 500)
        self.check_headers(r, "500")

    def test_dashboard_csp_hash_matches_inline_script(self):
        r = self.client.get("/")
        csp = r.headers["Content-Security-Policy"]
        body = open(os.path.join(REPO, "courtview.html"), "rb").read()
        scripts = re.findall(rb"<script>(.*?)</script>", body, flags=re.DOTALL)
        self.assertEqual(len(scripts), 1)
        want = "'sha256-" + base64.b64encode(hashlib.sha256(scripts[0]).digest()).decode() + "'"
        script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
        self.assertEqual(re.findall(r"'sha256-[^']+'", script_src), [want])
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", csp)

    def test_dashboard_has_no_inline_handlers(self):
        body = open(os.path.join(REPO, "courtview.html"), encoding="utf-8").read()
        self.assertIsNone(re.search(r"""[\s"'`]on[a-z]+\s*=""", body, flags=re.IGNORECASE))
        self.assertNotIn("javascript:", body)

    def test_forbidden_keeps_own_csp(self):
        r = self.anon.get("/")
        self.assertEqual(r.headers["Content-Security-Policy"],
                         "default-src 'none'; style-src 'unsafe-inline'; img-src data:")

    def test_access_and_api_csp_profiles(self):
        self.assertEqual(self.client.get("/access").headers["Content-Security-Policy"], cv.CSP_ACCESS)
        self.assertEqual(self.client.get("/api/health").headers["Content-Security-Policy"], cv.CSP_API)

    def test_cache_control_no_store(self):
        for url in ("/api/health", "/access/data", "/api/booked-hours?club_id=x", f"/api/club/?club_id={PADIUM}"):
            self.assertEqual(self.client.get(url).headers.get("Cache-Control"), "no-store", url)
        self.assertNotEqual(self.client.get("/").headers.get("Cache-Control"), "no-store")

    def test_booked_hours_410_after_gate_zero_upstream(self):
        self.assertEqual(self.anon.get("/api/booked-hours?club_id=x").status_code, 403)
        self.assertEqual(self.client.get("/api/booked-hours?club_id=x").status_code, 410)
        self.assertEqual(self.calls, [])


class RelayTests(SecBase):
    def test_json_looking_body_with_html_content_type_served_as_json(self):
        self.upstream = _Resp(b'{"a": 1}', ct="text/html")
        r = self.client.get(f"/api/club/?club_id={PADIUM}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.get_json(), {"a": 1})

    def test_non_json_body_becomes_502(self):
        self.upstream = _Resp(b"<script>alert(1)</script>", ct="text/html")
        r = self.client.get(f"/api/club/?club_id={PADIUM}")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json(), {"error": "upstream returned non-JSON"})
        self.assertNotIn(b"alert", r.get_data())

    def test_non_json_error_status_becomes_502(self):
        self.upstream = _Resp(b"Bad gateway page", code=422, ct="text/html")
        r = self.client.get(f"/api/club/?club_id={PADIUM}")
        self.assertEqual(r.status_code, 502)

    def test_exception_text_not_leaked(self):
        self.upstream = RuntimeError("secret-internal-detail https://host/path")
        r = self.client.get(f"/api/club/?club_id={PADIUM}")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json(), {"error": "upstream unavailable"})
        self.assertNotIn(b"secret", r.get_data())

    def test_every_api_response_object_uses_json_content_type(self):
        src = open(os.path.join(REPO, "courtview.py"), encoding="utf-8").read()
        for m in re.finditer(r"Response\(([^\n]*content_type=)([^\n)]*)", src):
            ctype = m.group(2)
            line = src[:m.start()].count("\n") + 1
            if "/api" in ctype or ctype.startswith("content_types.get("):
                continue
            self.assertTrue(ctype.startswith(('"application/json"', '"text/', '"application/javascript"', "'text/",
                                              '"text/html; charset=utf-8"')),
                            f"line {line}: {ctype}")


class AccessLabelTests(SecBase):
    def labels(self, client, url, **kw):
        with mock.patch.object(cv, "_log_access") as log:
            r = client.get(url, **kw)
        return r, [c.args[2] for c in log.call_args_list]

    def test_cookie_ok(self):
        r, labels = self.labels(self.client, "/api/health")
        self.assertEqual(labels, ["OK"])

    def test_query_token(self):
        _, labels = self.labels(self.anon, "/?token=" + TOKEN)
        self.assertEqual(labels, ["TOKEN"])

    def test_no_token_blocked(self):
        _, labels = self.labels(self.anon, "/api/health")
        self.assertEqual(labels, ["BLOCKED"])

    def test_rate_limited(self):
        for _ in range(cv.AUTH_RATE_LIMIT + 1):
            self.anon.get("/api/health")
        r, labels = self.labels(self.anon, "/api/health")
        self.assertEqual(labels, ["RATE_LIMITED"])

    def test_authenticated_404_is_ok(self):
        r, labels = self.labels(self.client, "/nope.php")
        self.assertEqual((r.status_code, labels), (404, ["OK"]))

    def test_unauthenticated_unknown_extension_blocked(self):
        r, labels = self.labels(self.anon, "/nope.php")
        self.assertEqual((r.status_code, labels), (403, ["BLOCKED"]))

    def test_racketeer_410_with_cookie_ok(self):
        r, labels = self.labels(self.client, f"/api/club/?club_id={RACK}")
        self.assertEqual((r.status_code, labels), (410, ["OK"]))

    def test_non_get_never_reaching_gate_blocked(self):
        with mock.patch.object(cv, "_log_access") as log:
            self.client.post("/api/health")
        self.assertEqual([c.args[2] for c in log.call_args_list], ["BLOCKED"])


class AccessAssetsTests(SecBase):
    """The access page, its script and the stylesheet are CourtView-owned files in web/."""

    @classmethod
    def setUpClass(cls):
        cls._saved = cv._ACCESS_HTML_BYTES, cv._ACCESS_JS_BYTES, cv._DASHBOARD_CSS_BYTES
        cv._load_web_assets()

    @classmethod
    def tearDownClass(cls):
        cv._ACCESS_HTML_BYTES, cv._ACCESS_JS_BYTES, cv._DASHBOARD_CSS_BYTES = cls._saved

    def test_assets_dir_is_repo_web(self):
        self.assertEqual(cv.WEB_ASSETS_DIR, os.path.join(REPO, "web"))
        for name in ("access.html", "access.js", "dashboard.css"):
            self.assertTrue(os.path.isfile(os.path.join(REPO, "web", name)), name)

    def test_title_and_no_wireslammer(self):
        html = cv._ACCESS_HTML_BYTES
        self.assertIn(b"<title>CourtView Access Log</title>", html)
        self.assertNotIn(b"Wireslammer", html)
        title = re.search(rb"<title>(.*?)</title>", html).group(1)
        self.assertNotIn(b"doxxnet", title.lower())
        self.assertNotIn(b"loadWS", cv._ACCESS_JS_BYTES)
        self.assertNotIn(b"Wireslammer", cv._ACCESS_JS_BYTES)

    def test_routes_serve_assets_with_csp(self):
        c = self.client
        page = c.get("/access")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Content-Security-Policy"], cv.CSP_ACCESS)
        for url in ("/access.js", "/dashboard.css"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertTrue(r.data, url)
            self.assertEqual(r.headers["Content-Security-Policy"], cv.CSP_API)

    def test_strip_function_gone_and_no_labs_reads(self):
        self.assertFalse(hasattr(cv, "_strip_wireslammer"))
        for name in os.listdir(REPO):
            if name.endswith(".py") and not name.startswith("."):
                with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                    self.assertNotIn("/root/labs", fh.read(), name)


class AccessRotationTests(unittest.TestCase):
    def test_rotation_keeps_n_files_0600(self):
        d = tempfile.mkdtemp(prefix="cv-rot-")
        path = os.path.join(d, "access.log")
        old = os.umask(0o022)
        try:
            with mock.patch.object(cv, "ACCESS_LOG_PATH", path), \
                    mock.patch.object(cv, "ACCESS_LOG_MAX_BYTES", 300), \
                    mock.patch.object(cv, "ACCESS_LOG_KEEP", 3), \
                    mock.patch.object(cv, "_resolve_ip", lambda ip: None), \
                    mock.patch.object(cv, "_ACCESS_FD", None):
                cv._ACCESS_FD = cv._open_access_log()
                try:
                    for i in range(60):
                        cv._log_access("10.0.0.1", f"/p{i}", "OK", "ua")
                finally:
                    try:
                        cv._ACCESS_FD.close()
                    except Exception:
                        pass
        finally:
            os.umask(old)
        names = sorted(os.listdir(d))
        self.assertEqual(names, ["access.log", "access.log.1", "access.log.2", "access.log.3"])
        for n in names:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode), 0o600, n)
        everything = "".join(open(os.path.join(d, n)).read() for n in names)
        self.assertIn('"/p59"', everything)
        self.assertNotIn('"/p0"', everything)  # oldest entries were dropped with the .4 file


if __name__ == "__main__":
    unittest.main()
