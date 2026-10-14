"""Load a fresh copy of the courtview_app package for one test module.

Every test module sets its own environment (data dir, DB path, key paths) before importing the app
and expects its own independent module state. The package is cached in sys.modules, so each load
first removes it. This is safe because the package never looks itself up through sys.modules
(imports are `from . import module` at module top level only; AST-enforced in tests/test_pii.py).

    cv = cvload.load("courtview_sec_under_test")
    cv.app                       the Flask app
    cv.security._gate            any module attribute, patched in the module that owns it
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULES = ("config", "clubs", "upstream", "privacy", "db", "jobs", "accesslog", "security", "payments",
           "refresh", "routes_core", "routes_availability", "routes_club", "routes_payments",
           "routes_archive", "startup")


def purge():
    """Forget the package so the next import re-executes every module under the current environment."""
    for name in [n for n in sys.modules if n == "courtview_app" or n.startswith("courtview_app.")]:
        del sys.modules[name]


def snapshot(app=None, entry=None):
    """Namespace of the package modules currently in sys.modules."""
    ns = types.SimpleNamespace(app=app, entry=entry)
    for name in MODULES:
        setattr(ns, name, sys.modules["courtview_app." + name])
    return ns


def load(alias):
    """Import REPO/courtview.py under `alias` with a fresh package and return the namespace."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    purge()
    spec = importlib.util.spec_from_file_location(alias, os.path.join(REPO, "courtview.py"))
    entry = importlib.util.module_from_spec(spec)
    sys.modules[alias] = entry
    spec.loader.exec_module(entry)
    return snapshot(app=entry.app, entry=entry)


def source_files():
    """Every Python source file of the app: the entry module and the whole package."""
    pkg = os.path.join(REPO, "courtview_app")
    files = [os.path.join(REPO, "courtview.py")]
    files += sorted(os.path.join(pkg, n) for n in os.listdir(pkg) if n.endswith(".py"))
    return files
