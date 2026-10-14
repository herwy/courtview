#!/usr/bin/env python3
"""cv_closure.py - print the installed dependency closure of a requirements file.

Usage: cv_closure.py REQFILE    prints name==version, one per line, sorted.

REQFILE holds top-level requirement specifiers (requirements.in). Each is resolved
against the installed distributions, following Requires-Dist transitively (markers
evaluated with no extras). Used by deploy.sh (drift check, pip-audit input) and by the
upgrade procedure in requirements.txt. Exits non-zero on a missing package or an empty closure.
"""
import sys
from importlib.metadata import PackageNotFoundError, distribution

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def closure(lines):
    todo = [Requirement(line) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    seen = {}
    while todo:
        req = todo.pop()
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(req.name)
        if name in seen:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            sys.exit(f"MISSING {name}")
        seen[name] = dist.version
        todo.extend(Requirement(r) for r in dist.requires or [])
    if not seen:
        sys.exit("no packages resolved")
    return seen


def main(argv):
    if len(argv) != 1:
        sys.exit("usage: cv_closure.py REQFILE")
    with open(argv[0]) as fh:
        seen = closure(fh.read().split("\n"))
    for name, version in sorted(seen.items()):
        print(f"{name}=={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
