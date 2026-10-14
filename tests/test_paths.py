"""Path resolution: data dir, credentials directory, explicit env precedence."""
import importlib.util
import os
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH_ENV = ("COURTVIEW_DATA_DIR", "COURTVIEW_DB_PATH", "COURTVIEW_ACCESS_LOG_PATH", "COURTVIEW_STATUS_DIR",
            "COURTVIEW_TOKEN_PATH", "COURTVIEW_HMAC_KEY_PATH", "CREDENTIALS_DIRECTORY")


def load(**env):
    """Import courtview.py fresh under a controlled environment."""
    clean = {k: v for k, v in os.environ.items() if k not in PATH_ENV}
    clean["COURTVIEW_SKIP_STARTUP"] = "1"
    clean.update(env)
    with mock.patch.dict(os.environ, clean, clear=True):
        spec = importlib.util.spec_from_file_location("courtview_paths_under_test", os.path.join(REPO, "courtview.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["courtview_paths_under_test"] = mod
        spec.loader.exec_module(mod)
    return mod


class CredentialPathTests(unittest.TestCase):
    def test_credentials_directory(self):
        cv = load(CREDENTIALS_DIRECTORY="/x")
        self.assertEqual(cv.TOKEN_PATH, "/x/token")
        self.assertEqual(cv.HMAC_KEY_PATH, "/x/hmac_key")

    def test_explicit_env_wins(self):
        cv = load(CREDENTIALS_DIRECTORY="/x", COURTVIEW_TOKEN_PATH="/t", COURTVIEW_HMAC_KEY_PATH="/h")
        self.assertEqual((cv.TOKEN_PATH, cv.HMAC_KEY_PATH), ("/t", "/h"))

    def test_legacy_defaults(self):
        cv = load()
        self.assertEqual(cv.TOKEN_PATH, "/root/.courtview_token")
        self.assertEqual(cv.HMAC_KEY_PATH, "/root/.courtview_hmac_key")


class DataDirTests(unittest.TestCase):
    def test_default_data_dir(self):
        cv = load()
        self.assertEqual(cv.DB_PATH, "/var/lib/courtview/courtview_cache.db")
        self.assertEqual(cv.ACCESS_LOG_PATH, "/var/lib/courtview/access.log")
        self.assertEqual(cv.STATUS_DIR, "/var/lib/courtview/status")

    def test_data_dir_env(self):
        cv = load(COURTVIEW_DATA_DIR="/d")
        self.assertEqual(cv.DB_PATH, "/d/courtview_cache.db")
        self.assertEqual(cv.ACCESS_LOG_PATH, "/d/access.log")
        self.assertEqual(cv.STATUS_DIR, "/d/status")

    def test_per_path_env_wins(self):
        cv = load(COURTVIEW_DATA_DIR="/d", COURTVIEW_DB_PATH="/a.db", COURTVIEW_ACCESS_LOG_PATH="/a.log",
                  COURTVIEW_STATUS_DIR="/s")
        self.assertEqual((cv.DB_PATH, cv.ACCESS_LOG_PATH, cv.STATUS_DIR), ("/a.db", "/a.log", "/s"))


class BackupDefaultsTests(unittest.TestCase):
    def test_backup_defaults_follow_data_dir(self):
        spec = importlib.util.spec_from_file_location("cv_backup_paths", os.path.join(REPO, "cv_backup.py"))
        with mock.patch.dict(os.environ, {"COURTVIEW_DATA_DIR": "/d"}):
            for k in ("COURTVIEW_DB_PATH", "COURTVIEW_BACKUP_DIR", "COURTVIEW_BACKUP_PUBKEY"):
                os.environ.pop(k, None)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        self.assertEqual(mod.DB_PATH, "/d/courtview_cache.db")
        self.assertEqual(mod.BACKUP_DIR, "/d/backups")
        self.assertEqual(mod.PUBKEY, os.path.join(REPO, "courtview-backup.pub.asc"))


if __name__ == "__main__":
    unittest.main()
